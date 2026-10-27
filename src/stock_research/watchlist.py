"""定时同步自选快照，向查询接口和模型提供统一的分页结果。"""

import asyncio
import json
import logging
from copy import deepcopy
from pathlib import Path
from typing import Any

import aiosqlite
from pydantic import BaseModel, Field

from stock_research.config import Settings
from stock_research.diagnostics import DiagnosticRecorder
from stock_research.security_names import SecurityNames
from stock_research.storage import now
from stock_research.watchlist_source import TonghuashunReader

logger = logging.getLogger(__name__)
WATCHLIST_PROMPT = """
自选股规则：
用户询问我的自选股、关注列表或自选分组时，先调用 get_watchlist 获取本次实际结果。
可按分组、证券名称或代码筛选；用 offset、limit 分页，不把一页当作完整列表。
自选包含股票、ETF、指数及其他品种，不等于持仓，不代表已买入；市场标识不可猜测。
列表来自同花顺电脑端缓存。区分缓存更新时间、系统同步时间和云端更新时间；
同步失败时结果会标为 cached 或 unavailable，必须说明限制，不能称为实时数据。
当前工具只提供关注列表与分组，不提供价格、估值或财报；需要这些数据另行取证。"""
SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist_snapshots (
    id INTEGER PRIMARY KEY CHECK(id=1), payload_json TEXT NOT NULL,
    fingerprint TEXT NOT NULL, synced_at TEXT NOT NULL
);
"""


class WatchlistInput(BaseModel):
    """筛选或分页读取自选，避免一次把全部证券塞入上下文。"""

    group: str = Field(
        default="", max_length=200, description="分组编号或完整分组名称；空值看全部自选"
    )
    q: str = Field(default="", max_length=200, description="证券代码或名称的搜索词")
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=200)


class WatchlistService:
    """将客户端作为列表来源，保留上次成功快照以应对临时读取失败。"""

    def __init__(self, settings: Settings, database: Path, diagnostics: DiagnosticRecorder) -> None:
        """建立独立的短事务、同步锁和可取消的轮询任务。"""
        self.reader = TonghuashunReader(settings.ths_install_dir, settings.ths_account)
        self._database = database
        self._names = SecurityNames(database, diagnostics)
        self._diagnostics = diagnostics
        self._interval = settings.watchlist_poll_interval
        self._lock = asyncio.Lock()
        self._job: asyncio.Task[None] | None = None
        self._snapshot: dict[str, Any] | None = None
        self._fingerprint = ""
        self._synced_at = ""
        self._checked_at = ""
        self._error = ""
        self._last_failure: tuple[str, str] | None = None

    async def start(self) -> None:
        """加载已持久化列表，首次同步后启动后台检查。"""
        async with aiosqlite.connect(self._database, timeout=15) as db:
            await db.executescript(SCHEMA)
            async with db.execute(
                "SELECT payload_json,fingerprint,synced_at FROM watchlist_snapshots WHERE id=1"
            ) as cursor:
                row = await cursor.fetchone()
            if row:
                self._snapshot = json.loads(row[0])
                self._fingerprint, self._synced_at = row[1], row[2]
        await self._names.start()
        await self.refresh()
        self._job = asyncio.create_task(self._poll(), name="watchlist-sync")
        logger.info("自选股自动同步启动，检查间隔=%s秒", self._interval)

    async def refresh(self) -> None:
        """检查文件变化，成功才替换快照；失败明确保留已有数据。"""
        async with self._lock:
            try:
                snapshot, fingerprint = await asyncio.to_thread(self.reader.read, self._fingerprint)
                self._checked_at = now()
                if not fingerprint:
                    self._error = "未找到同花顺远航版，请安装并登录，或配置客户端安装目录"
                    return
                if snapshot is None and self._names.due and self._snapshot is not None:
                    candidate = deepcopy(self._snapshot)
                    if await self._names.enrich(candidate):
                        snapshot = candidate
                elif snapshot is not None:
                    await self._names.enrich(snapshot)
                if snapshot is not None:
                    timestamp = now()
                    async with aiosqlite.connect(self._database, timeout=15) as db:
                        await db.execute(
                            "INSERT INTO watchlist_snapshots VALUES (1,?,?,?) "
                            "ON CONFLICT(id) DO UPDATE SET payload_json=excluded.payload_json,"
                            "fingerprint=excluded.fingerprint,synced_at=excluded.synced_at",
                            (json.dumps(snapshot, ensure_ascii=False), fingerprint, timestamp),
                        )
                        await db.commit()
                    self._snapshot, self._fingerprint, self._synced_at = (
                        snapshot,
                        fingerprint,
                        timestamp,
                    )
                    logger.info(
                        "自选股同步完成，标的=%s，分组=%s",
                        len(snapshot["items"]),
                        len(snapshot["groups"]),
                    )
                self._error = ""
                self._last_failure = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                message = "同花顺自选同步暂不可用，保留上次结果；请确认客户端已登录且缓存完整"
                failure = (type(exc).__name__, str(exc))
                if self._last_failure != failure:
                    self._diagnostics.record(
                        "watchlist.sync", "failed", exception=exc, message=message, recoverable=True
                    )
                self._last_failure = failure
                self._checked_at, self._error = now(), message

    async def query(
        self, group: str = "", q: str = "", offset: int = 0, limit: int = 100
    ) -> dict[str, Any]:
        """查询前检查本机缓存，返回来源、时间、全部分组及匹配页。"""
        request = WatchlistInput(group=group, q=q, offset=offset, limit=limit)
        await self.refresh()
        snapshot = self._snapshot or {"items": [], "groups": []}
        items = snapshot["items"]
        if request.group:
            matches = [
                row for row in snapshot["groups"] if request.group in {row["id"], row["name"]}
            ]
            if len(matches) != 1:
                raise ValueError("自选分组不存在或名称不唯一，请使用分组编号")
            items = matches[0]["items"]
        needle = request.q.strip().casefold()
        if needle:
            items = [
                row
                for row in items
                if needle in row["code"].casefold() or needle in row["name"].casefold()
            ]
        return {
            "status": "cached"
            if self._error and self._snapshot
            else "unavailable"
            if self._error
            else "ok",
            "warning": self._error,
            "source": snapshot.get("source", "同花顺远航版本机缓存"),
            "synced_at": self._synced_at,
            "checked_at": self._checked_at,
            "self_updated_at": snapshot.get("self_updated_at", ""),
            "groups_updated_at": snapshot.get("groups_updated_at", ""),
            "client_modified_at": snapshot.get("client_modified_at", ""),
            "note": "自选不等于持仓；手机改动需先同步到电脑端。此结果不包含行情或财务数据。",
            "groups": [
                {"id": row["id"], "name": row["name"], "count": len(row["items"])}
                for row in snapshot["groups"]
            ],
            "watchlist_total": len(snapshot["items"]),
            "total": len(items),
            "offset": request.offset,
            "limit": request.limit,
            "has_more": request.offset + request.limit < len(items),
            "items": items[request.offset : request.offset + request.limit],
        }

    async def close(self) -> None:
        """取消轮询，正常向上传递任务的取消异常。"""
        if self._job:
            self._job.cancel()
            try:
                await self._job
            except asyncio.CancelledError:
                # 这里只消费由本关闭方法发起的子任务取消。
                if asyncio.current_task().cancelling():
                    raise
            self._job = None
        logger.info("自选股自动同步已关闭")

    async def _poll(self) -> None:
        """后台同步不依赖浏览器打开，不使用大模型或付费接口。"""
        try:
            while True:
                await asyncio.sleep(self._interval)
                await self.refresh()
        except asyncio.CancelledError:
            raise


def watchlist_definition() -> dict[str, Any]:
    """描述只读的自选工具，金融数据源后续独立接入。"""
    return {
        "type": "function",
        "function": {
            "name": "get_watchlist",
            "description": "读取用户同花顺自选列表和自定义分组，支持名称、代码筛选及分页。"
            "用户提到我的自选时必须查询；这不是持仓、行情或财报数据。",
            "parameters": WatchlistInput.model_json_schema(),
        },
    }
