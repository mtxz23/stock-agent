"""使用 SQLite 保存模型配置、研究任务、进展事件和来源证据。"""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite

from stock_research.history import HISTORY_SCHEMA
from stock_research.memory import MEMORY_SCHEMA, migrate_memories

logger = logging.getLogger(__name__)
SCHEMA = """
CREATE TABLE IF NOT EXISTS models (
    id TEXT PRIMARY KEY, config_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY, company TEXT NOT NULL, question TEXT NOT NULL,
    model_id TEXT NOT NULL, model_snapshot TEXT NOT NULL, status TEXT NOT NULL,
    report TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '', parent_id TEXT,
    previous_report TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT 'research',
    title_auto INTEGER NOT NULL DEFAULT 0, title_attempt INTEGER NOT NULL DEFAULT 0,
    title_pending INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
    kind TEXT NOT NULL, message TEXT NOT NULL, data_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_task ON events(task_id, id);
CREATE INDEX IF NOT EXISTS tasks_parent ON tasks(parent_id);
CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, url TEXT NOT NULL, title TEXT NOT NULL,
    content_hash TEXT NOT NULL, archive_name TEXT NOT NULL, media_type TEXT NOT NULL,
    fetched_at TEXT NOT NULL, UNIQUE(task_id, url, content_hash)
);
CREATE INDEX IF NOT EXISTS sources_task ON sources(task_id);
"""


class Repository:
    """封装本地业务存储，保持调用方不依赖 SQL 和数据库文件布局。"""

    def __init__(self, data_dir: Path) -> None:
        """指定业务数据库位置。"""
        self.path = data_dir / "research.sqlite"

    async def initialize(self) -> None:
        """初始化表结构，恢复前一次非正常退出时留下的任务状态。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as connection:
            await connection.execute("PRAGMA journal_mode=WAL")
            async with connection.execute(
                "SELECT name FROM sqlite_master WHERE name='history_search'"
            ) as cursor:
                retired_index = await cursor.fetchone()
            await connection.executescript(SCHEMA + MEMORY_SCHEMA + HISTORY_SCHEMA)
            if retired_index:
                logger.info("旧关键词历史索引已移除，保留完整对话并改用Qdrant向量检索")
            await migrate_memories(connection)
            # 原有任务默认为研究，新增标题和类型不会覆盖历史报告或来源。
            async with connection.execute("PRAGMA table_info(tasks)") as cursor:
                columns = {row[1] for row in await cursor.fetchall()}
            if "title" not in columns:
                await connection.execute(
                    "ALTER TABLE tasks ADD COLUMN title TEXT NOT NULL DEFAULT ''"
                )
            if "kind" not in columns:
                await connection.execute(
                    "ALTER TABLE tasks ADD COLUMN kind TEXT NOT NULL DEFAULT 'research'"
                )
            # 旧对话保持现有标题，仅新建聊天启用自动命名。
            for name in ("title_auto", "title_attempt", "title_pending"):
                if name not in columns:
                    await connection.execute(
                        f"ALTER TABLE tasks ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0"
                    )
            # 上次退出留下的命名任务不再运行，保留已有标题，后续提问可重试。
            await connection.execute("UPDATE tasks SET title_pending=0")
            await connection.execute("UPDATE tasks SET title=company WHERE title=''")
            await connection.execute(
                "UPDATE tasks SET status='interrupted', error=?, updated_at=? "
                "WHERE status IN ('running','queued')",
                ("服务上次退出时回复尚未结束，可继续处理", now()),
            )
            await connection.commit()

    async def save_model(self, config: dict[str, Any], identifier: str | None = None) -> str:
        """新增或替换一个模型连接，凭据须先由调用方加密。"""
        identifier = identifier or uuid4().hex
        await self._execute(
            "INSERT INTO models VALUES (?, ?, ?) ON CONFLICT(id) "
            "DO UPDATE SET config_json=excluded.config_json",
            (identifier, dump(config), now()),
        )
        return identifier

    async def get_model(self, identifier: str) -> dict[str, Any] | None:
        """读取完整连接，仅供后端内部调用。"""
        rows = await self._query("SELECT * FROM models WHERE id=?", (identifier,))
        if not rows:
            return None
        return {"id": identifier, **json.loads(rows[0]["config_json"])}

    async def list_models(self) -> list[dict[str, Any]]:
        """列出脱敏连接，永远不返回加密凭据字段。"""
        rows = await self._query("SELECT * FROM models ORDER BY created_at DESC")
        return [public_model({"id": row["id"], **json.loads(row["config_json"])}) for row in rows]

    async def create_task(
        self,
        company: str,
        question: str,
        model: dict[str, Any],
        parent_id: str | None = None,
        previous_report: str = "",
        *,
        kind: str = "research",
        title: str | None = None,
    ) -> str:
        """保存一轮聊天或研究及模型快照，历史报告快照用于恢复对话。"""
        identifier = uuid4().hex
        timestamp = now()
        async with aiosqlite.connect(self.path, timeout=15) as connection:
            await connection.execute("BEGIN IMMEDIATE")
            inherited = None
            if parent_id:
                async with connection.execute(
                    "SELECT title,title_auto,title_attempt,title_pending FROM tasks WHERE id=?",
                    (parent_id,),
                ) as cursor:
                    inherited = await cursor.fetchone()
                if inherited is None:
                    raise ValueError("对话历史记录缺失，请新建对话")
            # 与自动命名共享写事务，避免追问继承请求开始时的过期标题。
            values = inherited or (
                title or company or question.replace("\n", " ")[:80],
                int(kind == "chat"),
                0,
                0,
            )
            await connection.execute(
                "INSERT INTO tasks(id,company,question,model_id,model_snapshot,status,parent_id,"
                "previous_report,created_at,updated_at,title,kind,title_auto,title_attempt,"
                "title_pending) VALUES (?,?,?,?,?,'queued',?,?,?,?,?,?,?,?,?)",
                (
                    identifier, company, question, model["id"], dump(model), parent_id,
                    previous_report, timestamp, timestamp, values[0], kind, *values[1:],
                ),
            )
            await connection.commit()
        await self.add_event(identifier, "queued", "消息已提交，等待处理")
        return identifier

    async def get_task(self, identifier: str, *, internal: bool = False) -> dict[str, Any] | None:
        """读取任务，默认屏蔽模型凭据快照。"""
        rows = await self._query("SELECT * FROM tasks WHERE id=?", (identifier,))
        if not rows:
            return None
        task = rows[0]
        task["title_pending"] = bool(task["title_pending"])
        model = json.loads(task.pop("model_snapshot"))
        task["model_snapshot"] = model if internal else public_model(model)
        if not internal:
            task.pop("previous_report", None)
        return task

    async def list_tasks(self) -> list[dict[str, Any]]:
        """返回任务摘要，避免列表加载完整报告和内部快照。"""
        rows = await self._query(
            "SELECT id,company,question,model_id,status,parent_id,created_at,updated_at,title,kind,"
            "title_pending FROM tasks ORDER BY created_at DESC"
        )
        for row in rows:
            row["title_pending"] = bool(row["title_pending"])
        return rows

    async def conversation_tasks(self, identifier: str) -> list[dict[str, Any]]:
        """从任意提问找到对话根节点，返回全部提问和分支的摘要。"""
        history = await self.conversation_history(identifier)
        root_id = history[0]["id"]
        return await self._query(
            "WITH RECURSIVE conversation(id) AS ("
            "SELECT id FROM tasks WHERE id=? UNION "
            "SELECT tasks.id FROM tasks JOIN conversation ON tasks.parent_id=conversation.id) "
            "SELECT id,status FROM tasks WHERE id IN (SELECT id FROM conversation)",
            (root_id,),
        )

    async def rename_conversation(self, identifiers: list[str], title: str) -> None:
        """手动命名同时关闭自动命名，后续提问继承用户的标题。"""
        async with aiosqlite.connect(self.path, timeout=15) as connection:
            await connection.executemany(
                "UPDATE tasks SET title=?,title_auto=0,title_pending=0 WHERE id=?",
                [(title, identifier) for identifier in identifiers],
            )
            # 标题参与历史索引；改名后由下一次检索重新索引。
            await connection.executemany(
                "DELETE FROM history_vector_state WHERE task_id=?",
                [(identifier,) for identifier in identifiers],
            )
            await connection.commit()

    async def delete_conversation(self, identifiers: list[str]) -> None:
        """在后台任务和归档停止使用后，事务删除整段对话及关联业务记录。"""
        async with aiosqlite.connect(self.path, timeout=15) as connection:
            await connection.execute("BEGIN IMMEDIATE")
            for table, column in (
                ("events", "task_id"),
                ("sources", "task_id"),
                ("history_vector_state", "task_id"),
                ("context_summaries", "owner_id"),
                ("tasks", "id"),
            ):
                await connection.executemany(
                    f"DELETE FROM {table} WHERE {column}=?",
                    [(identifier,) for identifier in identifiers],
                )
            await connection.commit()

    async def conversation_history(self, parent_id: str | None) -> list[dict[str, Any]]:
        """按时间恢复当前分支，排除其他分支并检测错误关联。"""
        history: list[dict[str, Any]] = []
        visited: set[str] = set()
        while parent_id:
            if parent_id in visited:
                raise ValueError("对话关联存在循环，无法恢复上下文")
            visited.add(parent_id)
            task = await self.get_task(parent_id, internal=True)
            if not task:
                raise ValueError("对话历史记录缺失，请新建对话")
            history.append(task)
            parent_id = task["parent_id"]
        return list(reversed(history))

    async def begin_research(self, identifier: str, company: str) -> None:
        """记录模型已明确的研究对象，保留原始消息作为对话标题。"""
        await self._execute(
            "UPDATE tasks SET company=?,kind='research',updated_at=? WHERE id=?",
            (company, now(), identifier),
        )

    async def update_task(
        self, identifier: str, status: str, *, report: str | None = None, error: str = ""
    ) -> None:
        """更新任务生命周期，保留未被替换的已有报告。"""
        if report is None:
            await self._execute(
                "UPDATE tasks SET status=?,error=?,updated_at=? WHERE id=?",
                (status, error, now(), identifier),
            )
        else:
            await self._execute(
                "UPDATE tasks SET status=?,report=?,error=?,updated_at=? WHERE id=?",
                (status, report, error, now(), identifier),
            )

    async def add_event(
        self, task_id: str, kind: str, message: str, data: dict[str, Any] | None = None
    ) -> None:
        """追加可回放事件，不将凭据或内部推理写入进展记录。"""
        await self._execute(
            "INSERT INTO events(task_id,kind,message,data_json,created_at) VALUES (?,?,?,?,?)",
            (task_id, kind, message, dump(data or {}), now()),
        )

    async def events(self, task_id: str, after: int = 0) -> list[dict[str, Any]]:
        """按事件编号分页返回研究轨迹，供 SSE 重连补读。"""
        rows = await self._query(
            "SELECT * FROM events WHERE task_id=? AND id>? ORDER BY id LIMIT 200",
            (task_id, after),
        )
        for row in rows:
            row["data"] = json.loads(row.pop("data_json"))
        return rows

    async def save_source(self, task_id: str, source: dict[str, str]) -> dict[str, Any]:
        """去重保存实际读取的原文版本，返回可引用的唯一证据编号。"""
        existing = await self._query(
            "SELECT * FROM sources WHERE task_id=? AND url=? AND content_hash=?",
            (task_id, source["url"], source["content_hash"]),
        )
        if existing:
            return existing[0]
        identifier = "S-" + uuid4().hex[:10]
        await self._execute(
            "INSERT INTO sources VALUES (?,?,?,?,?,?,?,?)",
            (
                identifier,
                task_id,
                source["url"],
                source["title"],
                source["content_hash"],
                source["archive_name"],
                source["media_type"],
                now(),
            ),
        )
        rows = await self._query("SELECT * FROM sources WHERE id=?", (identifier,))
        return rows[0]

    async def sources(self, task_id: str) -> list[dict[str, Any]]:
        """列出本次实际读取并归档的证据。"""
        return await self._query(
            "SELECT * FROM sources WHERE task_id=? ORDER BY fetched_at", (task_id,)
        )

    async def get_source(self, identifier: str) -> dict[str, Any] | None:
        """通过证据标识取得本地归档元数据。"""
        rows = await self._query("SELECT * FROM sources WHERE id=?", (identifier,))
        return rows[0] if rows else None

    async def _query(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """读取记录并按列名返回。"""
        async with aiosqlite.connect(self.path, timeout=15) as connection:
            connection.row_factory = aiosqlite.Row
            async with connection.execute(sql, parameters) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def _execute(self, sql: str, parameters: tuple[Any, ...]) -> None:
        """在短事务内提交写入，避免研究期间长期占用 SQLite 写锁。"""
        async with aiosqlite.connect(self.path, timeout=15) as connection:
            await connection.execute(sql, parameters)
            await connection.commit()


def public_model(model: dict[str, Any]) -> dict[str, Any]:
    """只返回公开配置和凭据存在标志。"""
    return {key: value for key, value in model.items() if key != "api_key_encrypted"} | {
        "has_api_key": bool(model.get("api_key_encrypted")),
        "context_budget": model.get("context_budget", 64000),
    }


def now() -> str:
    """生成带时区的 UTC 存储时间。"""
    return datetime.now(UTC).isoformat()


def dump(value: Any) -> str:
    """序列化中文业务数据，保持可读性。"""
    return json.dumps(value, ensure_ascii=False)
