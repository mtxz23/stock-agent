"""将历史原文增量编码进 Qdrant，按语义回忆并校验原文有效性。"""

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

import aiosqlite
import httpx

from stock_research.config import CredentialVault, Settings
from stock_research.diagnostics import DiagnosticRecorder
from stock_research.embeddings import (
    CloudEmbeddingModel,
    EmbeddingConfig,
    EmbeddingError,
    EmbeddingModel,
    embedding_namespace,
)
from stock_research.history import HistoryStore
from stock_research.storage import Repository
from stock_research.timings import measure_stage, measured_lock
from stock_research.vector_store import VectorStore

logger = logging.getLogger(__name__)
EmbeddingFactory = Callable[[dict[str, Any], str], EmbeddingModel]
SOURCE_FIELDS = ("title", "company", "question", "report")
ANSWER_CHUNK_SIZE = 2000
ANSWER_CHUNK_OVERLAP = 200


class VectorHistory:
    """原文以 SQLite 为准，向量失败不会影响对话保存。"""

    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        vault: CredentialVault,
        client: httpx.AsyncClient,
        diagnostics: DiagnosticRecorder,
        factory: EmbeddingFactory | None = None,
    ) -> None:
        """共享 embedding 配置和索引锁，多个研究任务不重复编码。"""
        self.repository = repository
        self.vault = vault
        self.client = client
        self.diagnostics = diagnostics
        self.factory = factory or self._create_model
        self.history = HistoryStore(repository.path)
        self.vectors = VectorStore(
            settings.data_dir / "vectors",
            settings.qdrant_url,
            settings.qdrant_api_key.get_secret_value(),
        )
        self._lock = asyncio.Lock()

    async def config(self) -> dict[str, Any]:
        """返回公开配置；默认复用硅基流动连接，不返回其密钥。"""
        rows = await self.history._rows("SELECT config_json FROM embedding_settings WHERE id=1")
        if rows:
            config = EmbeddingConfig.model_validate_json(rows[0]["config_json"])
        else:
            connections = [row for row in await self.repository.list_models() if row["has_api_key"]]
            if not connections:
                return {"config": None, "indexed_tasks": 0, "database": "Qdrant"}
            connection = next(
                (row for row in connections if "siliconflow.cn" in row["base_url"]), connections[0]
            )
            config = EmbeddingConfig(connection_id=connection["id"])
        resolved, _ = await self._resolve(config)
        namespace = embedding_namespace(resolved)
        counts = await self.history._rows(
            "SELECT count(*) AS count FROM history_vector_state WHERE namespace=?", (namespace,)
        )
        return {
            "config": config.model_dump(),
            "indexed_tasks": counts[0]["count"],
            "database": "Qdrant",
        }

    async def save_config(self, config: EmbeddingConfig) -> dict[str, Any]:
        """保存独立模型选择，凭据仍由原 API 连接管理。"""
        await self._resolve(config)
        async with aiosqlite.connect(self.repository.path, timeout=15) as connection:
            await connection.execute(
                "INSERT INTO embedding_settings(id,config_json) VALUES (1,?) "
                "ON CONFLICT(id) DO UPDATE SET config_json=excluded.config_json",
                (config.model_dump_json(),),
            )
            await connection.commit()
        logger.info("历史embedding配置已保存，模型=%s，维度=%s", config.model, config.dimensions)
        return await self.config()

    async def _resolve(self, config: EmbeddingConfig) -> tuple[dict[str, Any], str]:
        """读取连接的当前地址及加密凭据，拒绝缺失配置。"""
        connection = await self.repository.get_model(config.connection_id)
        if not connection or not connection.get("api_key_encrypted"):
            raise EmbeddingError("历史向量检索需要选择已填写密钥的API连接")
        return config.model_dump() | {"base_url": connection["base_url"]}, self.vault.decrypt(
            connection["api_key_encrypted"]
        )

    async def _model(self) -> tuple[EmbeddingModel, dict[str, Any]]:
        """每次操作固定一份配置，更换模型不会混用旧向量。"""
        public = await self.config()
        if public["config"] is None:
            raise EmbeddingError("请先在模型连接中配置历史向量检索")
        config, key = await self._resolve(EmbeddingConfig.model_validate(public["config"]))
        return self.factory(config, key), config

    def _create_model(self, config: dict[str, Any], key: str) -> EmbeddingModel:
        """默认使用当前应用的 HTTP 连接和脱敏诊断。"""
        return CloudEmbeddingModel(self.client, config, key, self.diagnostics)

    async def test(self) -> dict[str, Any]:
        """测试真实查询和文档向量，避免仅凭模型名判断接口可用。"""
        model, config = await self._model()
        await model.embed_documents(["经营现金流用于观察公司的现金创造能力。"])
        await model.embed_query("企业的造血能力")
        return {
            "message": "向量接口测试成功",
            "model": config["model"],
            "dimensions": model.dimensions,
        }

    async def rebuild(self) -> dict[str, Any]:
        """重建当前模型索引，其他模型集合和原文均保留。"""
        async with self._lock:
            model, _ = await self._model()
            await self.vectors.reset(model.namespace)
            async with aiosqlite.connect(self.repository.path, timeout=15) as connection:
                await connection.execute(
                    "DELETE FROM history_vector_state WHERE namespace=?", (model.namespace,)
                )
                await connection.commit()
            count = await self._sync(model)
        return {"message": f"历史向量索引已重建，共处理{count}条记录", "indexed_tasks": count}

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
        """按问答语义检索；命中后返回原问题和回答片段，核实原文仍有效。"""
        async with (
            measure_stage(self.diagnostics, "rag.total", "历史检索总耗时") as timing,
            measured_lock(self._lock, self.diagnostics, "rag.queue", "等待历史检索队列"),
        ):
            async with measure_stage(self.diagnostics, "rag.config", "读取向量模型配置"):
                model, config = await self._model()
            async with measure_stage(self.diagnostics, "rag.sync", "更新历史索引") as sync:
                sync["indexed_tasks"] = await self._sync(model)
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
            async with measure_stage(self.diagnostics, "rag.filter", "筛选可用历史记录"):
                tasks = await self.history._rows(
                    "SELECT id FROM tasks WHERE " + " AND ".join(filters), tuple(parameters)
                )
            if not tasks:
                timing["hit_count"] = 0
                return self._result([], config)
            async with measure_stage(
                self.diagnostics, "rag.query_embedding", "生成查询向量", model=config["model"]
            ):
                vector = await model.embed_query(query)
            # 分页跳过生成向量期间发生变化的旧片段，不向模型返回失效内容。
            items: list[dict[str, Any]] = []
            offset = 0
            while len(items) < limit:
                async with measure_stage(self.diagnostics, "rag.vector_query", "Qdrant相似度查询"):
                    hits = await self.vectors.query(
                        model.namespace,
                        model.dimensions,
                        vector,
                        [task["id"] for task in tasks],
                        config["min_score"],
                        32,
                        offset,
                    )
                async with measure_stage(
                    self.diagnostics, "rag.validate", "核对命中原文"
                ) as validation:
                    validation["hit_count"] = len(hits)
                    for hit in hits:
                        rows = await self.history._rows(
                            "SELECT id,title,company,question,report,created_at,status "
                            "FROM tasks WHERE id=?",
                            (hit["task_id"],),
                        )
                        if (
                            not rows
                            or rows[0]["status"] in {"queued", "running"}
                            or source_hash(rows[0]) != hit["source_hash"]
                        ):
                            continue
                        hit.pop("source_hash")
                        content = hit.pop("content")
                        hit["text"] = paired_text(hit["question"], content, hit["section"])
                        # 阅读位置对应原始回复，不计入附加的问题和标签。
                        hit["total_chars"] = len(rows[0][hit["section"]])
                        hit["next_start"] = hit["end"] if hit["end"] < hit["total_chars"] else None
                        items.append(hit)
                        if len(items) == limit:
                            break
                if len(hits) < 32:
                    break
                offset += len(hits)
            logger.info("历史问答向量检索完成，命中片段=%s，模型=%s", len(items), config["model"])
            timing["hit_count"] = len(items)
            return self._result(items, config)

    def _result(self, items: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
        """评分用于相关性排序，不代表历史结论真实或有效。"""
        return {
            "items": items,
            "retrieval": "vector",
            "embedding_model": config["model"],
            "note": "每项包含原问题和相关回答；start/end指向section原文。"
            "历史内容不代表当前事实，可用read_history核查原文",
        }

    async def _sync(self, model: EmbeddingModel) -> int:
        """只编码新增或变化的原文，网络调用不持有 SQLite 写事务。"""
        if await self.vectors.ensure(model.namespace, model.dimensions):
            async with aiosqlite.connect(self.repository.path, timeout=15) as connection:
                await connection.execute(
                    "DELETE FROM history_vector_state WHERE namespace=?", (model.namespace,)
                )
                await connection.commit()
        changed = await self.history._rows(
            "SELECT t.id,t.title,t.company,t.question,t.report,t.created_at,t.status,t.updated_at,"
            "i.source_hash AS indexed_hash FROM tasks t LEFT JOIN history_vector_state i "
            "ON i.task_id=t.id AND i.namespace=? WHERE t.status NOT IN ('queued','running') "
            "AND (i.task_id IS NULL OR i.updated_at<>t.updated_at)",
            (model.namespace,),
        )
        count = 0
        for task in changed:
            fingerprint = source_hash(task)
            if fingerprint != task["indexed_hash"]:
                records = history_chunks(task, fingerprint)
                vectors = []
                for start in range(0, len(records), 8):
                    documents = [
                        f"对话：{record['title']}\n公司：{record['company']}\n"
                        + paired_text(record["question"], record["content"], record["section"])
                        for record in records[start : start + 8]
                    ]
                    async with measure_stage(
                        self.diagnostics,
                        "rag.document_embedding",
                        "生成历史文档向量",
                        indexed_task_id=task["id"],
                        batch_size=len(documents),
                        model=model.__class__.__name__,
                    ):
                        vectors.extend(await model.embed_documents(documents))
                # API请求期间可能被删除或编辑；写入前和写入后都检查权威原文。
                current = await self.repository.get_task(task["id"])
                if not current or source_hash(current) != fingerprint:
                    logger.warning("历史记录编码期间已变更，跳过旧索引，任务=%s", task["id"])
                    continue
                async with measure_stage(
                    self.diagnostics,
                    "rag.vector_write",
                    "写入历史向量",
                    document_count=len(records),
                ):
                    await self.vectors.replace(
                        model.namespace, model.dimensions, task["id"], records, vectors
                    )
            async with aiosqlite.connect(self.repository.path, timeout=15) as connection:
                await connection.execute("BEGIN IMMEDIATE")
                connection.row_factory = aiosqlite.Row
                async with connection.execute(
                    "SELECT * FROM tasks WHERE id=?", (task["id"],)
                ) as cursor:
                    current = await cursor.fetchone()
                if (
                    current
                    and source_hash(dict(current)) == fingerprint
                    and current["status"] not in {"queued", "running"}
                ):
                    await connection.execute(
                        "INSERT INTO history_vector_state"
                        "(namespace,task_id,updated_at,source_hash) VALUES (?,?,?,?) "
                        "ON CONFLICT(namespace,task_id) DO UPDATE SET "
                        "updated_at=excluded.updated_at,source_hash=excluded.source_hash",
                        (model.namespace, task["id"], current["updated_at"], fingerprint),
                    )
                    count += 1
                await connection.commit()
        if changed:
            logger.info("历史向量索引已更新，记录数=%s", count)
        return count

    async def delete_tasks(self, identifiers: list[str]) -> None:
        """等待正在编码的任务结束，再清除所有模型空间中的向量。"""
        async with self._lock:
            await self.vectors.delete_tasks(identifiers)

    async def close(self) -> None:
        """在运行时关闭阶段释放向量数据库。"""
        await self.vectors.close()


def source_hash(task: dict[str, Any]) -> str:
    """标题、公司和原文共同决定片段有效性，避免同时间戳更新遗漏。"""
    content = json.dumps([task[field] for field in SOURCE_FIELDS], ensure_ascii=False)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def history_chunks(task: dict[str, Any], fingerprint: str) -> list[dict[str, Any]]:
    """一问一答共同编码；长回复分段但每段携带原问题，保留原文位置。"""
    records = []
    # 失败或取消的历史可能没有最终回复，此时仍保留可检索的原问题。
    section = "report" if task["report"].strip() else "question"
    text = task[section]
    start = 0
    while start < len(text):
        end = min(start + ANSWER_CHUNK_SIZE, len(text))
        records.append(
            {
                "task_id": task["id"],
                "title": task["title"],
                "company": task["company"],
                "created_at": task["created_at"],
                "question": task["question"],
                "section": section,
                "start": start,
                "end": end,
                "content": text[start:end],
                "source_hash": fingerprint,
            }
        )
        if end == len(text):
            break
        start = end - ANSWER_CHUNK_OVERLAP
    return records


def paired_text(question: str, content: str, section: str) -> str:
    """明确区分用户提问与模型观点，无回复时不编造回答。"""
    if section == "question":
        return f"用户提问：{content}\n模型回答：（该记录没有最终回复）"
    return f"用户提问：{question}\n模型回答：{content}"
