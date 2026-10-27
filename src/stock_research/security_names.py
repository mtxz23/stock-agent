"""为本地字典缺失的证券补充公开名称，独立于行情及财务数据接入。"""

import asyncio
import json
import re
from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Any

import aiosqlite
import httpx

from stock_research.diagnostics import DiagnosticRecorder
from stock_research.storage import now

SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist_names (
    market TEXT NOT NULL, code TEXT NOT NULL, name TEXT NOT NULL,
    provider TEXT NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY(market,code)
);
"""
EASTMONEY_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"
SINA_URL = "https://hq.sinajs.cn/list="
# 同花顺数字市场及已有本地标准市场分别映射，不根据六位代码猜市场。
EASTMONEY_MARKETS = {
    "17": "1",
    "USHA": "1",
    "33": "0",
    "USZA": "0",
    "120": "1",
    "144": "0",
    "151": "0",
    "177": "116",
    "UHKM": "116",
    "169": "106",
    "185": "105",
}
SINA_INDICES = {
    "IXIC": "int_nasdaq",
    "DJI": "int_dji",
    "SPX": "int_sp500",
    "FTSE": "int_ftse",
    "N225": "int_nikkei",
    "KS11": "int_ks11",
}
GLOBAL_CODES = {"DJI": "DJIA", "SPX": "SPX", "FTSE": "FTSE", "N225": "N225", "KS11": "KS11"}
NAME_TTL = timedelta(days=7)
RETRY_SECONDS = 300
MAX_RESPONSE_BYTES = 512 * 1024


class SecurityNames:
    """本地名称优先，公开名称按市场代码缓存，失败不影响自选同步。"""

    def __init__(self, database: Path, diagnostics: DiagnosticRecorder) -> None:
        """保存名称字典位置和重试状态，不访问云端模型或客户端凭据。"""
        self._database = database
        self._diagnostics = diagnostics
        self._entries: dict[tuple[str, str], dict[str, str]] = {}
        self._next_attempt = 0.0

    @property
    def due(self) -> bool:
        """避免每十秒同步或每次页面查询都重复访问公开服务。"""
        return monotonic() >= self._next_attempt

    async def start(self) -> None:
        """加载持久化名称，断网及重启后仍能使用之前取得的结果。"""
        async with aiosqlite.connect(self._database, timeout=15) as db:
            await db.executescript(SCHEMA)
            async with db.execute(
                "SELECT market,code,name,provider,updated_at FROM watchlist_names"
            ) as cursor:
                rows = await cursor.fetchall()
        self._entries = {
            (row[0], row[1]): {"name": row[2], "provider": row[3], "updated_at": row[4]}
            for row in rows
        }

    async def enrich(self, snapshot: dict[str, Any]) -> bool:
        """填充缺失名称，过期远端名称可更新，本地名称不被覆盖。"""
        items = snapshot["items"] + [
            item for group in snapshot["groups"] for item in group["items"]
        ]
        wanted = {}
        deadline = datetime.now(UTC) - NAME_TTL
        changed = False
        for item in items:
            key = (item["market"], item["code"])
            local = bool(item["name"]) and not item.get("name_provider")
            if local:
                continue
            entry = self._entries.get(key)
            if entry:
                changed |= _apply_name(item, entry)
            if not entry or datetime.fromisoformat(entry["updated_at"]) < deadline:
                wanted[key] = item
        if not self.due:
            return changed
        self._next_attempt = monotonic() + RETRY_SECONDS
        requests: dict[str, list[tuple[str, str]]] = {}
        for key in wanted:
            identifier = _eastmoney_key(*key)
            if identifier:
                requests.setdefault(identifier, []).append(key)
        results: dict[tuple[str, str], dict[str, str]] = {}
        # 国际指数使用显式标识，不把纳斯达克综合指数替换成纳斯达克100。
        sina_requests = {
            token: ("88", code) for code, token in SINA_INDICES.items() if ("88", code) in wanted
        }
        if not requests and not sina_requests:
            return changed
        try:
            # 这两个公开服务采用独立直连；不改变模型连接的代理配置。
            async with httpx.AsyncClient(timeout=8, trust_env=False) as client:
                jobs = []
                if requests:
                    jobs.append(self._provider(self._eastmoney(client, requests)))
                if sina_requests:
                    jobs.append(self._provider(self._sina(client, sina_requests)))
                for outcome in await asyncio.gather(*jobs):
                    # 东方财富优先；新浪补缺，单个服务超时也不阻断另一个。
                    for key, entry in outcome.items():
                        results.setdefault(key, entry)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failure(exc)
        if results:
            async with aiosqlite.connect(self._database, timeout=15) as db:
                await db.executemany(
                    "INSERT INTO watchlist_names VALUES (?,?,?,?,?) ON CONFLICT(market,code) "
                    "DO UPDATE SET name=excluded.name,provider=excluded.provider,"
                    "updated_at=excluded.updated_at",
                    [
                        (market, code, row["name"], row["provider"], row["updated_at"])
                        for (market, code), row in results.items()
                    ],
                )
                await db.commit()
            self._entries.update(results)
            for item in items:
                key = (item["market"], item["code"])
                if key in results and (not item["name"] or item.get("name_provider")):
                    changed |= _apply_name(item, results[key])
        return changed

    async def _provider(self, request: Awaitable[dict]) -> dict:
        """独立限制每个服务的等待时间，失败只影响该服务的名称。"""
        try:
            async with asyncio.timeout(12):
                return await request
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failure(exc)
            return {}

    async def _eastmoney(
        self, client: httpx.AsyncClient, requests: dict[str, list[tuple[str, str]]]
    ) -> dict:
        """批量取明确证券身份的名称，市场和代码必须同时匹配。"""
        output = {}
        identifiers = list(requests)
        for start in range(0, len(identifiers), 50):
            batch = identifiers[start : start + 50]
            response = await client.get(
                EASTMONEY_URL + "?secids=" + ",".join(batch) + "&fields=f12,f13,f14",
                headers={"Referer": "https://quote.eastmoney.com/"},
            )
            response.raise_for_status()
            for identifier, name in decode_eastmoney(response.content, set(batch)).items():
                for key in requests[identifier]:
                    output[key] = {"name": name, "provider": "东方财富", "updated_at": now()}
        return output

    async def _sina(self, client: httpx.AsyncClient, requests: dict[str, tuple[str, str]]) -> dict:
        """国际指数只用显式别名，不用纳斯达克100替代综合指数。"""
        response = await client.get(
            SINA_URL + ",".join(requests), headers={"Referer": "https://finance.sina.com.cn/"}
        )
        response.raise_for_status()
        return {
            requests[token]: {"name": name, "provider": "新浪财经", "updated_at": now()}
            for token, name in decode_sina(response.content, set(requests)).items()
        }

    def _failure(self, exc: Exception) -> None:
        """记录独立名称服务的可恢复错误，不改变已取得的列表。"""
        self._diagnostics.record(
            "watchlist.names",
            "failed",
            exception=exc,
            message="公开证券名称暂不可用，保留本地和已缓存名称",
            recoverable=True,
        )


def decode_eastmoney(raw: bytes, requested: set[str]) -> dict[str, str]:
    """只接受本批次请求的市场与代码，拒绝错误响应及无名称占位符。"""
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("公开名称响应超过大小限制")
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("rc") != 0:
        raise ValueError("公开名称接口返回失败")
    data = value.get("data")
    if data is None:
        return {}
    rows = data.get("diff")
    if not isinstance(rows, list):
        raise ValueError("公开名称响应格式不支持")
    output = {}
    for row in rows:
        identifier = f"{row.get('f13')}.{row.get('f12')}"
        name = row.get("f14")
        if identifier in requested and isinstance(name, str) and _valid_name(name):
            output[identifier] = name.strip()
    return output


def decode_sina_nasdaq(raw: bytes) -> str:
    """解析指定指数的名称字段，不执行接口返回的 JavaScript。"""
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("公开名称响应超过大小限制")
    values = decode_sina(raw, {"int_nasdaq"})
    if "int_nasdaq" not in values:
        raise ValueError("新浪指数名称响应格式不支持")
    return values["int_nasdaq"]


def decode_sina(raw: bytes, requested: set[str]) -> dict[str, str]:
    """只提取已请求的指数名称，不执行接口返回的脚本。"""
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("公开名称响应超过大小限制")
    output = {}
    for token, content in re.findall(
        r'var hq_str_([A-Za-z0-9_]+)="([^"\r\n]*)";', raw.decode("gb18030")
    ):
        name = content.split(",", 1)[0]
        if token in requested and _valid_name(name):
            output[token] = name
    return output


def _eastmoney_key(market: str, code: str) -> str:
    """映射明确支持的市场；未知品种保留原代码，不进行模糊搜索。"""
    if market == "88":
        alias = GLOBAL_CODES.get(code)
        return "100." + alias if alias else ""
    target = EASTMONEY_MARKETS.get(market)
    if not target or not re.fullmatch(r"[A-Za-z0-9._-]{1,30}", code):
        return ""
    if target == "116":
        code = code.removeprefix("HK").zfill(5)
    return f"{target}.{code}"


def _apply_name(item: dict[str, Any], entry: dict[str, str]) -> bool:
    """只更新名称及出处信息，证券身份与自选顺序保持不变。"""
    changed = (
        item["name"] != entry["name"]
        or item.get("name_provider") != entry["provider"]
        or item.get("name_updated_at") != entry["updated_at"]
    )
    item.update(
        name=entry["name"], name_provider=entry["provider"], name_updated_at=entry["updated_at"]
    )
    return changed


def _valid_name(name: str) -> bool:
    """过滤空值、占位符及过长内容，不为无匹配证券编造名称。"""
    return bool(name.strip()) and name.strip() not in {"-", "--", "null"} and len(name) <= 120
