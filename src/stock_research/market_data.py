"""管理股票数据生命周期、持久化缓存和统一失败反馈，查询不依赖前端页面。"""

import asyncio
import hashlib
import json
import logging
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any

import aiosqlite
import httpx

from stock_research.diagnostics import DiagnosticRecorder
from stock_research.market_models import BarsInput, FundamentalsInput
from stock_research.market_sources import PublicStockSource, StockSource, render_fundamentals

logger = logging.getLogger(__name__)
SCHEMA = """
CREATE TABLE IF NOT EXISTS stock_data_cache (
    cache_key TEXT PRIMARY KEY, payload_json TEXT NOT NULL, fetched_at TEXT NOT NULL
);
"""
TTL_SECONDS = {
    "profile": 86400,
    "business": 3600,
    "indicators": 3600,
    "income": 3600,
    "balance": 3600,
    "cashflow": 3600,
    "bars": 300,
}


class StockDataError(RuntimeError):
    """包含诊断编号的明确数据访问失败，不将失败解释成空数据。"""

    def __init__(self, message: str, diagnostic_id: str = "") -> None:
        """向API和AI工具共享可读原因。"""
        super().__init__(message)
        self.diagnostic_id = diagnostic_id


class StockDataService:
    """结构化基本面与K线统一入口，可替换供应商，保留原始响应和抓取时间。"""

    def __init__(
        self,
        database: Path,
        diagnostics: DiagnosticRecorder,
        timeout: float = 30,
        source: StockSource | None = None,
    ) -> None:
        """绑定存储和适配器，调用之前不联网。"""
        self._database = database
        self._diagnostics = diagnostics
        self._timeout = timeout
        self._source = source
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        self._failures: dict[str, tuple[float, str, str]] = {}

    async def start(self) -> None:
        """初始化缓存和独立直连客户端，不预下载全部自选数据。"""
        async with aiosqlite.connect(self._database, timeout=15) as db:
            await db.executescript(SCHEMA)
        if self._source is None:
            self._client = httpx.AsyncClient(
                timeout=10,
                trust_env=False,
                headers={
                    "Referer": "https://emweb.securities.eastmoney.com/",
                    "User-Agent": "StockResearch/0.1",
                },
            )
            self._source = PublicStockSource(self._client)
        logger.info(
            "股票数据服务启动，基本面来源=公开F10，行情来源=腾讯证券/新浪，支持境内外指数和商品"
        )

    async def query(self, request: FundamentalsInput | BarsInput) -> dict[str, Any]:
        """参数标准化后查缓存，供应商失败时明确标识可用的旧快照。"""
        if self._source is None:
            raise StockDataError("股票数据服务尚未启动")
        section = request.section if isinstance(request, FundamentalsInput) else "bars"
        params = request.model_dump(mode="json", exclude={"refresh", "fields"})
        key = hashlib.sha256(
            json.dumps(
                {"version": 2 if section == "bars" else 1, "section": section, **params},
                sort_keys=True,
            ).encode()
        ).hexdigest()
        # 查询串行，避免同一个过期缓存并发重复请求或突发大量访问公开服务。
        async with self._lock:
            cached = await self._load(key)
            age = _age(cached) if cached else float("inf")
            if cached and not request.refresh and age < TTL_SECONDS[section]:
                result = _result(cached, "cached")
            else:
                failure = self._failures.get(key)
                if not request.refresh and failure and monotonic() < failure[0]:
                    result = self._fallback(cached, section, failure[1], failure[2])
                else:
                    logger.info("开始查询股票数据，证券=%s，栏目=%s", request.symbol, section)
                    try:
                        async with asyncio.timeout(self._timeout):
                            payload = await (
                                self._source.fundamentals(request)
                                if isinstance(request, FundamentalsInput)
                                else self._source.bars(request)
                            )
                        # 禁止NaN进入缓存或工具JSON，原始响应可用于复核字段及来源。
                        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        identifier = self._diagnostics.record(
                            "stock_data.fetch",
                            "failed",
                            exception=exc,
                            message="公开股票数据查询失败",
                            recoverable=True,
                            symbol=request.symbol,
                            section=section,
                        )
                        reason = (
                            str(exc)[:180] if isinstance(exc, ValueError) else "来源连接或响应异常"
                        )
                        self._failures[key] = (monotonic() + 60, identifier, reason)
                        result = self._fallback(cached, section, identifier, reason)
                    else:
                        async with aiosqlite.connect(self._database, timeout=15) as db:
                            await db.execute(
                                "INSERT INTO stock_data_cache VALUES (?,?,?) "
                                "ON CONFLICT(cache_key) "
                                "DO UPDATE SET payload_json=excluded.payload_json,"
                                "fetched_at=excluded.fetched_at",
                                (key, encoded, payload["fetched_at"]),
                            )
                            await db.commit()
                        self._failures.pop(key, None)
                        result = _result(payload, "fresh")
                        logger.info("股票数据查询完成，证券=%s，栏目=%s", request.symbol, section)
        if isinstance(request, FundamentalsInput):
            rendered = render_fundamentals(result, request.fields)
            rendered["evidence"] = result["evidence"]
            return rendered
        return result

    async def _load(self, key: str) -> dict[str, Any] | None:
        """只读对应查询的完整成功快照，损坏缓存记录诊断并重新请求。"""
        async with aiosqlite.connect(self._database, timeout=15) as db:
            async with db.execute(
                "SELECT payload_json FROM stock_data_cache WHERE cache_key=?", (key,)
            ) as cursor:
                row = await cursor.fetchone()
        if not row:
            return None
        try:
            value = json.loads(row[0])
            _age(value)
            if not isinstance(value.get("evidence"), list):
                raise ValueError("股票缓存缺少原始来源")
            return value
        except (ValueError, TypeError, KeyError) as exc:
            self._diagnostics.record(
                "stock_data.cache",
                "failed",
                exception=exc,
                message="股票数据缓存损坏，尝试重新获取",
                recoverable=True,
            )
            return None

    def _fallback(
        self, cached: dict | None, section: str, identifier: str, reason: str = ""
    ) -> dict[str, Any]:
        """财务旧缓存最多保留30天，K线旧缓存最多一天，不更新原抓取时间。"""
        if cached and _age(cached) <= (86400 if section == "bars" else 30 * 86400):
            result = _result(cached, "stale")
            result.update(
                warning="数据源暂不可用，使用旧缓存；请核对fetched_at和数据日期，不能称为最新或实时数据"
                + ("；" + cached["warning"] if cached.get("warning") else ""),
                diagnostic_id=identifier,
                failure_reason=reason,
            )
            return result
        raise StockDataError(
            "股票数据查询失败："
            + (reason or "公开来源暂不可用")
            + "；没有可用缓存；诊断编号："
            + identifier,
            identifier,
        )

    async def close(self) -> None:
        """关闭独立HTTP连接，不改变模型接口客户端。"""
        if self._client:
            await self._client.aclose()
            self._client = None
        logger.info("股票数据服务已关闭")


def _result(payload: dict[str, Any], status: str) -> dict[str, Any]:
    """查询状态与抓取时间分开，缓存读取不冒充重新采集。"""
    result = deepcopy(payload)
    rows = result.get("rows", result.get("raw_rows", []))
    result.update(
        status=status if rows or status == "stale" else "empty",
        warning=result.get("warning", "") if rows else "数据源未返回匹配数据，不能将缺失值视为零",
    )
    return result


def _age(payload: dict[str, Any]) -> float:
    """计算带时区快照的实际年龄，拒绝无时区时间。"""
    stamp = datetime.fromisoformat(payload["fetched_at"])
    if stamp.tzinfo is None:
        raise ValueError("股票缓存抓取时间缺少时区")
    return max(0, (datetime.now(UTC) - stamp).total_seconds())
