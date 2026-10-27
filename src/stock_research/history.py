"""检索历史提问和回复，按编号读取原文，缓存可复用的对话摘要。"""

import hashlib
import json
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Protocol

import aiosqlite

logger = logging.getLogger(__name__)
HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS embedding_settings (
    id INTEGER PRIMARY KEY CHECK(id=1), config_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS history_vector_state (
    namespace TEXT NOT NULL, task_id TEXT NOT NULL, updated_at TEXT NOT NULL,
    source_hash TEXT NOT NULL, PRIMARY KEY(namespace, task_id)
);
DROP TABLE IF EXISTS history_search;
DROP TABLE IF EXISTS history_index_state;
CREATE TABLE IF NOT EXISTS context_summaries (
    cache_key TEXT PRIMARY KEY, owner_id TEXT NOT NULL, covered_count INTEGER NOT NULL,
    summary TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS context_summaries_owner ON context_summaries(owner_id);
"""


class HistorySearcher(Protocol):
    """历史语义检索的可替换接口。"""

    async def search(self, query: str, **filters: Any) -> dict[str, Any]:
        """按自然语言问题召回原文片段。"""
        ...


class HistoryStore:
    """只索引业务文字，不读取模型配置、凭据或内部推理。"""

    def __init__(self, path: Path, searcher: HistorySearcher | None = None) -> None:
        """复用业务数据库，删除对话时同步清理索引及摘要。"""
        self.path = path
        self.searcher = searcher

    async def search(
        self,
        query: str,
        *,
        company: str = "",
        after: date | None = None,
        before: date | None = None,
        limit: int = 5,
        exclude_task: str = "",
    ) -> dict[str, Any]:
        """有问题时使用向量检索；空查询直接浏览近期原始提问。"""
        if query.strip():
            if self.searcher is None:
                raise ValueError("历史向量检索尚未配置，请在模型连接中配置embedding")
            return await self.searcher.search(
                query,
                company=company,
                after=after,
                before=before,
                limit=limit,
                exclude_task=exclude_task,
            )
        filters = ["status NOT IN ('queued','running')", "id<>?"]
        parameters: list[Any] = [exclude_task]
        if company:
            filters.append("company=?")
            parameters.append(company)
        if after:
            filters.append("created_at>=?")
            parameters.append(after.isoformat())
        if before:
            filters.append("created_at<?")
            parameters.append((before + timedelta(days=1)).isoformat())
        rows = await self._rows(
            "SELECT id AS task_id,title,company,created_at,status,'question' AS section,"
            "0 AS start,question AS content FROM tasks WHERE "
            + " AND ".join(filters)
            + " ORDER BY created_at DESC LIMIT ?",
            (*parameters, limit),
        )
        for row in rows:
            row["text"] = row.pop("content")[:900]
            row["end"] = len(row["text"])
        return {
            "items": rows,
            "retrieval": "recent",
            "note": "近期历史记录；可用read_history核查原文",
        }

    async def read(
        self,
        task_id: str,
        section: str = "report",
        start: int = 0,
        max_chars: int = 6000,
    ) -> dict[str, Any]:
        """分页读取历史原文或工具结果，不把片段误标为完整记录。"""
        rows = await self._rows(
            "SELECT id,title,company,question,report,status,created_at FROM tasks WHERE id=?",
            (task_id,),
        )
        if not rows:
            raise ValueError("历史记录不存在或已删除")
        task = rows[0]
        if section == "tools":
            events = await self._rows(
                "SELECT kind,data_json,created_at FROM events WHERE task_id=? "
                "AND kind IN ('tool_finished','tool_error') ORDER BY id",
                (task_id,),
            )
            # 历史读取本身不重复归档进读取内容，避免自引用无限增长。
            values = [
                event
                for event in events
                if json.loads(event["data_json"]).get("name")
                not in {"read_history", "search_history"}
            ]
            text = "\n".join(
                json.dumps(
                    {
                        "kind": event["kind"],
                        "created_at": event["created_at"],
                        "data": json.loads(event["data_json"]),
                    },
                    ensure_ascii=False,
                )
                for event in values
            )
        else:
            text = task[section]
        if not text:
            raise ValueError("这部分尚无原文，可以改读提问或工具记录")
        if start >= len(text):
            raise ValueError(f"起始位置超出原文范围，原文共{len(text)}字")
        end = min(len(text), start + max_chars)
        return {
            "task_id": task_id,
            "title": task["title"],
            "company": task["company"],
            "created_at": task["created_at"],
            "status": task["status"],
            "section": section,
            "text": text[start:end],
            "start": start,
            "end": end,
            "total_chars": len(text),
            "next_start": end if end < len(text) else None,
            "note": "这是历史记录，时效和结论仍需核实",
        }

    async def summaries(self, owners: list[str]) -> list[dict[str, Any]]:
        """只查当前分支祖先生成的摘要，防止其他分支上下文混入。"""
        if not owners:
            return []
        placeholders = ",".join("?" for _ in owners)
        return await self._rows(
            "SELECT * FROM context_summaries WHERE owner_id IN ("
            + placeholders
            + ") ORDER BY covered_count DESC,created_at DESC",
            tuple(owners),
        )

    async def get_summary(self, cache_key: str) -> str | None:
        """取得对应原始片段的摘要缓存。"""
        rows = await self._rows(
            "SELECT summary FROM context_summaries WHERE cache_key=?",
            (cache_key,),
        )
        return rows[0]["summary"] if rows else None

    async def save_summary(
        self,
        cache_key: str,
        owner_id: str,
        covered_count: int,
        summary: str,
    ) -> None:
        """保存可复用摘要，原始消息保持不变。"""
        async with aiosqlite.connect(self.path, timeout=15) as connection:
            await connection.execute(
                "INSERT INTO context_summaries(cache_key,owner_id,covered_count,summary) "
                "VALUES (?,?,?,?) ON CONFLICT(cache_key) DO NOTHING",
                (cache_key, owner_id, covered_count, summary),
            )
            await connection.commit()

    async def _rows(self, sql: str, parameters: tuple = ()) -> list[dict[str, Any]]:
        """统一读取业务字段，数据库异常交由现有诊断处理。"""
        async with aiosqlite.connect(self.path, timeout=15) as connection:
            connection.row_factory = aiosqlite.Row
            async with connection.execute(sql, parameters) as cursor:
                return [dict(row) for row in await cursor.fetchall()]


def summary_key(messages: list[dict[str, Any]]) -> str:
    """摘要版本和原文共同决定缓存键，修改后的回复不会误用旧摘要。"""
    raw = json.dumps(messages, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(("context-v1:" + raw).encode("utf-8")).hexdigest()
