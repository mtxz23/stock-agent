"""只读同花顺远航版本机缓存，解析自选、分组及证券名称。"""

import hashlib
import json
import logging
import os
import sqlite3
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
MAX_CACHE_BYTES = 5 * 1024 * 1024
# 已核实的常用指数别名仅在同花顺对应市场下生效，断网也可显示名称。
INDEX_NAMES = {
    ("88", "IXIC"): "纳斯达克综合指数",
    ("88", "DJI"): "道琼斯工业指数",
    ("88", "SPX"): "标普500指数",
    ("88", "FTSE"): "英国富时100指数",
    ("88", "N225"): "日经225指数",
    ("88", "KS11"): "韩国综合指数",
    ("120", "000985"): "中证全指",
    ("144", "899050"): "北证50",
}


class WatchlistSourceError(ValueError):
    """表示客户端缓存尚不可用或格式发生变化。"""


class TonghuashunReader:
    """发现远航版安装目录，拒绝在多个账号之间自行猜测。"""

    def __init__(self, install_dir: Path | None = None, account: str = "") -> None:
        """支持自动发现和环境变量指定来源，不访问登录凭据。"""
        self.install_dir = install_dir
        self.account = account

    def read(self, previous: str = "") -> tuple[dict[str, Any] | None, str]:
        """读取稳定的一组缓存；文件未变时只检查元数据。"""
        install = self.install_dir or discover_installation()
        if install is None:
            return None, ""
        users = install / "bin" / "users"
        if self.account:
            if Path(self.account).name != self.account or self.account in {".", ".."}:
                raise WatchlistSourceError("同花顺账号目录设置不合法")
            folder = users / self.account
        else:
            candidates = sorted(
                path
                for path in users.iterdir()
                if path.is_dir() and (path / "SelfStockCache.json").is_file()
            )
            if len(candidates) != 1:
                raise WatchlistSourceError(
                    "未找到唯一的同花顺自选缓存；请登录客户端，多个账号时需指定来源账号"
                )
            folder = candidates[0]
        files = [folder / "SelfStockCache.json", folder / "blockstockV3.xml"]
        names_db = install / "bin" / "stockname" / "stocknameV2.db"
        stocks_db = install / "bin" / "stockname" / "stocks.db"
        tracked = files + [
            names_db,
            stocks_db,
            Path(str(names_db) + "-wal"),
            Path(str(stocks_db) + "-wal"),
        ]
        signature = _signature(tracked)
        if signature == previous:
            return None, signature
        raw = [_read_file(path) for path in files]
        snapshot = parse_cache(raw[0], raw[1])
        _attach_names(snapshot, names_db, stocks_db)
        if signature != _signature(tracked):
            raise WatchlistSourceError("同花顺正在更新缓存，稍后自动重试")
        snapshot.update(
            source="同花顺远航版本机缓存",
            self_updated_at=_file_time(files[0]),
            groups_updated_at=_file_time(files[1]),
        )
        return snapshot, signature


def parse_cache(self_text: str, group_text: str) -> dict[str, Any]:
    """分别解析代码序列和市场序列，分组保留市场、顺序及空分组。"""
    payload = json.loads(self_text.lstrip("\ufeff"))
    if not isinstance(payload, dict) or payload.get("StatusCode") != 0:
        raise WatchlistSourceError("同花顺自选缓存尚未成功同步")
    data = payload.get("Data")
    if not isinstance(data, dict) or not isinstance(data.get("Selfstock"), str):
        raise WatchlistSourceError("同花顺自选缓存格式不支持")
    sections = data["Selfstock"].split(",")
    if len(sections) != 2:
        raise WatchlistSourceError("同花顺自选代码与市场序列格式不支持")
    codes, markets = [[part for part in text.split("|") if part] for text in sections]
    if len(codes) != len(markets) or len(codes) != data.get("Count"):
        raise WatchlistSourceError("同花顺自选数量不完整，稍后自动重试")
    items = [_security(code, market) for code, market in zip(codes, markets, strict=True)]
    if "<!DOCTYPE" in group_text.upper() or "<!ENTITY" in group_text.upper():
        raise WatchlistSourceError("同花顺分组缓存包含不支持的 XML 声明")
    document = ET.fromstring(group_text.lstrip("\ufeff"))
    if document.tag != "hevo":
        raise WatchlistSourceError("同花顺分组缓存格式不支持")
    groups = []
    identifiers = set()
    for block in document.findall("Block"):
        identifier, name = block.get("id", ""), block.get("name", "")
        if not identifier or not name or identifier in identifiers:
            raise WatchlistSourceError("同花顺分组标识缺失或重复")
        identifiers.add(identifier)
        members = [
            _security(row.get("code", ""), row.get("market", ""))
            for row in block.findall("security")
        ]
        groups.append({"id": identifier, "name": name, "items": _unique(members)})
    # 远航版 sort_list 使用十六进制编号，Block.id 使用十进制编号。
    order = {
        str(int(value, 16)): index
        for index, value in enumerate(
            part for part in document.get("sort_list", "").split(",") if part
        )
    }
    groups.sort(key=lambda group: order.get(group["id"], len(order)))
    return {
        "items": _unique(items),
        "groups": groups,
        "client_modified_at": str(data.get("ModifyTime", "")),
    }


def discover_installation() -> Path | None:
    """从 Windows 卸载登记中发现远航版，不扫描其他应用的用户数据。"""
    if os.name != "nt":
        return None
    import winreg

    roots = [
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
        ),
    ]
    found = set()
    for hive, key_path in roots:
        try:
            parent = winreg.OpenKey(hive, key_path)
        except FileNotFoundError:
            continue
        with parent:
            for index in range(winreg.QueryInfoKey(parent)[0]):
                with winreg.OpenKey(parent, winreg.EnumKey(parent, index)) as child:
                    try:
                        display_name = winreg.QueryValueEx(child, "DisplayName")[0]
                        location = winreg.QueryValueEx(child, "InstallLocation")[0]
                    except FileNotFoundError:
                        continue
                    if "同花顺远航版" in display_name and location:
                        found.add(Path(location).resolve())
    if len(found) > 1:
        raise WatchlistSourceError("检测到多个远航版安装目录，请指定自选数据来源")
    return next(iter(found), None)


def _attach_names(snapshot: dict[str, Any], names_db: Path, stocks_db: Path) -> None:
    """只读本地名称字典，缺失指数用已核实的市场代码映射补全。"""
    securities = snapshot["items"] + [
        item for group in snapshot["groups"] for item in group["items"]
    ]
    codes = list(dict.fromkeys(item["code"] for item in securities))
    names: dict[tuple[str, str], str] = {}
    market_symbols: dict[str, str] = {}
    try:
        if names_db.is_file():
            with sqlite3.connect(
                names_db.resolve().as_uri() + "?mode=ro", uri=True, timeout=1
            ) as db:
                for start in range(0, len(codes), 300):
                    batch = codes[start : start + 300]
                    placeholders = ",".join("?" for _ in batch)
                    for code, name, market, market_name in db.execute(
                        f"SELECT CODE,NAME,MARKET,MARKETNAME FROM tablestock "
                        f"WHERE CODE IN ({placeholders}) AND DATATAG IN ('Realtime','Base') "
                        "ORDER BY CASE DATATAG WHEN 'Realtime' THEN 0 ELSE 1 END",
                        batch,
                    ):
                        if name:
                            names.setdefault((market, code), name.split("|")[0].split("@")[0])
                        parts = (market_name or "").split("_")
                        if len(parts) == 3 and parts[2].isdigit():
                            market_symbols[parts[2]] = market
        if stocks_db.is_file():
            with sqlite3.connect(
                stocks_db.resolve().as_uri() + "?mode=ro", uri=True, timeout=1
            ) as db:
                keys = [f"{item['market']}:{item['code']}" for item in snapshot["items"]]
                for start in range(0, len(keys), 300):
                    batch = keys[start : start + 300]
                    placeholders = ",".join("?" for _ in batch)
                    for market, code, name in db.execute(
                        f"SELECT MarketID,Code,Name FROM Stocks WHERE Code IN ({placeholders})",
                        batch,
                    ):
                        names[(market, code.split(":", 1)[-1])] = name
    except (OSError, sqlite3.Error) as exc:
        logger.warning("同花顺证券名称字典暂不可用，保留代码，类型=%s", type(exc).__name__)
    for item in securities:
        original = item["market"]
        symbol = market_symbols.get(original, original)
        item["name"] = names.get((original, item["code"]), names.get((symbol, item["code"]), ""))
        if not item["name"]:
            item["name"] = INDEX_NAMES.get((original, item["code"]), "")
        item["market"] = symbol
        item["id"] = f"ths:{symbol}:{item['code']}"
    memberships: dict[str, list[str]] = {}
    for group in snapshot["groups"]:
        for item in group["items"]:
            memberships.setdefault(item["id"], []).append(group["name"])
    for item in securities:
        item["groups"] = memberships.get(item["id"], [])


def _security(code: str, market: str) -> dict[str, Any]:
    """验证原始证券身份，原样保留前导零和市场信息。"""
    if not code or not market or len(code) > 80 or len(market) > 80:
        raise WatchlistSourceError("同花顺证券代码或市场标识不完整")
    return {"id": f"ths:{market}:{code}", "code": code, "market": market, "name": "", "groups": []}


def _unique(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按市场和代码去重，保留客户端中的首次出现顺序。"""
    return list({item["id"]: item for item in items}.values())


def _read_file(path: Path) -> str:
    """限制缓存大小，按 UTF-8 读取，不修改客户端文件。"""
    with path.open("rb") as handle:
        raw = handle.read(MAX_CACHE_BYTES + 1)
    if len(raw) > MAX_CACHE_BYTES:
        raise WatchlistSourceError("同花顺自选缓存超过大小限制")
    return raw.decode("utf-8-sig")


def _signature(files: list[Path]) -> str:
    """指纹覆盖来源路径、文件时间和大小，避免不同账号复用快照。"""
    # 解析和名称匹配规则升级时重建快照，不沿用旧版本的缺失名称。
    values: list[Any] = ["watchlist-reader-v3"]
    for path in files:
        try:
            stat = path.stat()
            values.append((str(path.resolve()), stat.st_mtime_ns, stat.st_size))
        except FileNotFoundError:
            # 两个名称字典可选，核心缓存缺失则由读取步骤明确报错。
            values.append((str(path.resolve()), None, None))
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


def _file_time(path: Path) -> str:
    """将文件写入时间转换为 UTC；它不代表云端行情时间。"""
    return datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()
