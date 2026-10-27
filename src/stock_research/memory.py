"""在现有 SQLite 中保存长期记忆，提供全量上下文与管理列表。"""

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite

from stock_research.memory_schemas import MemoryInput

logger = logging.getLogger(__name__)
MEMORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY, content TEXT NOT NULL, category TEXT NOT NULL,
    company TEXT NOT NULL, tags TEXT NOT NULL,
    as_of TEXT, expires_on TEXT, source_kind TEXT NOT NULL,
    source_task_id TEXT, source_title TEXT NOT NULL, source_quote TEXT NOT NULL,
    source_url TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1, dedup_key TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS memories_category ON memories(category);
"""
SELECT_MEMORY = """
SELECT m.*, EXISTS(SELECT 1 FROM tasks t WHERE t.id=m.source_task_id) AS source_available
FROM memories m
"""


class MemoryError(ValueError):
    """可向用户或模型说明的记忆操作错误。"""


class MemoryNotFound(MemoryError):
    """条目不存在或已经删除。"""


class MemoryConflict(MemoryError):
    """条目在页面打开或模型读取后已经被修改。"""


class MemoryStore:
    """持久化记忆；删除对话不会连带删除独立的长期记忆。"""

    def __init__(self, path: Path) -> None:
        """复用业务数据库，不保存模型密钥。"""
        self.path = path

    async def create(
        self,
        payload: MemoryInput,
        *,
        source_kind: str = "manual",
        source_task_id: str | None = None,
        source_title: str = "",
        source_quote: str = "",
        source_url: str = "",
    ) -> dict[str, Any]:
        """创建条目，重复保存返回原条目而不覆盖来源信息。"""
        values = payload.model_dump(mode="json")
        identifier, timestamp = uuid4().hex, _now()
        async with aiosqlite.connect(self.path, timeout=15) as connection:
            await connection.execute(
                "INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(dedup_key) DO NOTHING",
                (
                    identifier,
                    values["content"],
                    values["category"],
                    values["company"],
                    json.dumps(values["tags"], ensure_ascii=False),
                    values["as_of"],
                    values["expires_on"],
                    source_kind,
                    source_task_id,
                    source_title,
                    source_quote,
                    source_url,
                    timestamp,
                    timestamp,
                    1,
                    _key(values),
                ),
            )
            await connection.commit()
        rows = await self._rows(" WHERE m.dedup_key=?", (_key(values),))
        logger.info("记忆保存完成，条目=%s", rows[0]["id"])
        return rows[0]

    async def get(self, identifier: str) -> dict[str, Any]:
        """返回条目及来源对话是否仍存在。"""
        rows = await self._rows(" WHERE m.id=?", (identifier,))
        if not rows:
            raise MemoryNotFound("记忆不存在或已删除")
        return rows[0]

    async def update(
        self,
        identifier: str,
        payload: MemoryInput,
        version: int,
    ) -> dict[str, Any]:
        """原子更新内容，保留最初出处并检测版本冲突。"""
        values = payload.model_dump(mode="json")
        try:
            async with aiosqlite.connect(self.path, timeout=15) as connection:
                cursor = await connection.execute(
                    "UPDATE memories SET content=?, category=?, company=?, tags=?, "
                    "as_of=?, expires_on=?, updated_at=?, version=version+1, dedup_key=? "
                    "WHERE id=? AND version=?",
                    (
                        values["content"],
                        values["category"],
                        values["company"],
                        json.dumps(values["tags"], ensure_ascii=False),
                        values["as_of"],
                        values["expires_on"],
                        _now(),
                        _key(values),
                        identifier,
                        version,
                    ),
                )
                await connection.commit()
        except aiosqlite.IntegrityError as exc:
            raise MemoryConflict("已有相同内容的记忆，请编辑原条目") from exc
        if not cursor.rowcount:
            await self.get(identifier)
            raise MemoryConflict("记忆已被修改，请刷新后重试")
        logger.info("记忆已更新，条目=%s", identifier)
        return await self.get(identifier)

    async def delete(self, identifier: str, version: int) -> None:
        """删除前检查版本；历史对话中的记忆使用快照仍保留。"""
        async with aiosqlite.connect(self.path, timeout=15) as connection:
            cursor = await connection.execute(
                "DELETE FROM memories WHERE id=? AND version=?", (identifier, version)
            )
            await connection.commit()
        if not cursor.rowcount:
            await self.get(identifier)
            raise MemoryConflict("记忆已被修改，请刷新后重试")
        logger.info("记忆已删除，条目=%s", identifier)

    async def list(
        self,
        query: str = "",
        category: str = "",
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        """管理页面检索全部条目，包括过期信息。"""
        rows = await self._rows(" ORDER BY m.updated_at DESC, m.id")
        rows = [row for row in rows if not category or row["category"] == category]
        if query.strip():
            rows = _rank(rows, query)
        return {"items": rows[offset : offset + limit], "total": len(rows)}

    async def all_entries(self) -> list[dict[str, Any]]:
        """自动上下文使用全部长期记忆，不按条数或相关性截断。"""
        return await self._rows(" ORDER BY m.created_at, m.id")

    async def _rows(self, suffix: str, parameters: tuple = ()) -> list[dict[str, Any]]:
        """统一读取和解码；数据库异常交给现有接口或任务诊断处理。"""
        async with aiosqlite.connect(self.path, timeout=15) as connection:
            connection.row_factory = aiosqlite.Row
            async with connection.execute(SELECT_MEMORY + suffix, parameters) as cursor:
                rows = [dict(row) for row in await cursor.fetchall()]
        for row in rows:
            row["tags"] = json.loads(row["tags"])
            row["source_available"] = bool(row["source_available"])
            row.pop("dedup_key")
        return rows


async def migrate_memories(connection: aiosqlite.Connection) -> None:
    """取消旧确认字段但保留所有条目，迁移纳入初始化事务。"""
    async with connection.execute("PRAGMA table_info(memories)") as cursor:
        columns = {row[1] for row in await cursor.fetchall()}
    if "status" in columns:
        await connection.execute("DROP INDEX IF EXISTS memories_status")
        await connection.execute("ALTER TABLE memories DROP COLUMN status")
        logger.info("记忆已迁移为自动管理，原有条目保留")


def _rank(rows: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """中文二字词片和英文词匹配，明确公司命中优先。"""
    terms = _terms(query)
    scored = []
    for row in rows:
        searchable = row["content"] + " " + row["company"] + " " + " ".join(row["tags"])
        matches = terms & _terms(searchable)
        score = len(matches) / max(1, len(terms))
        company = row["company"].casefold()
        if company and company in query.casefold():
            score += 3
        if matches or (company and company in query.casefold()):
            scored.append((score, row))
    return [row for _, row in sorted(scored, key=lambda pair: pair[0], reverse=True)]


def _terms(value: str) -> set[str]:
    """提取汉字二元词片及字母数字词，避免单字噪声。"""
    result = set(re.findall(r"[a-z0-9_.-]+", value.casefold()))
    for segment in re.findall(r"[\u4e00-\u9fff]+", value):
        result.update(segment[index : index + 2] for index in range(len(segment) - 1))
    return result


def _key(values: dict[str, Any]) -> str:
    """内容、类别、公司及日期相同视为同一条记忆。"""
    return json.dumps(
        [
            re.sub(r"\s+", "", values["content"]).casefold(),
            values["category"],
            values["company"].casefold(),
            values["as_of"],
        ],
        ensure_ascii=False,
    )


def _now() -> str:
    """存储带时区的时间。"""
    return datetime.now(UTC).isoformat()
