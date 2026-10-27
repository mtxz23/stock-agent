"""用 Qdrant 保存可重建的历史向量，支持本地磁盘和远程服务。"""

import asyncio
import logging
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

from qdrant_client import QdrantClient, models

from stock_research.embeddings import EmbeddingError

logger = logging.getLogger(__name__)


class VectorStore:
    """单进程共享客户端，在线程中串行执行同步 SDK，避免阻塞事件循环。"""

    def __init__(self, path: Path, url: str = "", api_key: str = "") -> None:
        """延迟打开数据库，没有检索需求时不占用本地文件锁。"""
        self.path = path
        self.url = url
        self.api_key = api_key
        self._client: QdrantClient | None = None
        self._lock = RLock()
        self._closed = False

    async def ensure(self, namespace: str, dimensions: int) -> bool:
        """返回是否新建集合，原文库恢复后可以自动补建丢失的向量。"""

        def operation(client: QdrantClient):
            """在同一线程锁内检查并创建集合。"""
            missing = not client.collection_exists("history_" + namespace)
            self._collection(client, namespace, dimensions)
            return missing

        return await self._run(operation)

    async def replace(
        self,
        namespace: str,
        dimensions: int,
        task_id: str,
        records: list[dict[str, Any]],
        vectors: list[list[float]],
    ) -> None:
        """替换一条历史的全部片段；失败时调用方不会记录索引成功。"""

        def operation(client: QdrantClient):
            """确定性编号使失败重试不会重复追加。"""
            name = self._collection(client, namespace, dimensions)
            points = [
                models.PointStruct(
                    id=str(
                        uuid5(
                            NAMESPACE_URL,
                            f"{namespace}:{task_id}:{record['section']}:{record['start']}",
                        )
                    ),
                    vector=vector,
                    payload=record,
                )
                for record, vector in zip(records, vectors, strict=True)
            ]
            client.delete(name, models.FilterSelector(filter=_tasks_filter([task_id])), wait=True)
            if points:
                client.upsert(name, points, wait=True)

        await self._run(operation)

    async def query(
        self,
        namespace: str,
        dimensions: int,
        vector: list[float],
        task_ids: list[str],
        threshold: float,
        limit: int,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """只查询 SQLite 中仍存在且符合范围的记录，按余弦相似度排序。"""
        if not task_ids:
            return []

        def operation(client: QdrantClient):
            """不读取向量本体，只取评分及原文定位字段。"""
            name = self._collection(client, namespace, dimensions)
            result = client.query_points(
                name,
                query=vector,
                query_filter=_tasks_filter(task_ids),
                score_threshold=threshold,
                limit=limit,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            return [dict(point.payload or {}) | {"score": point.score} for point in result.points]

        return await self._run(operation)

    async def delete_tasks(self, task_ids: list[str]) -> None:
        """清理各模型索引中的已删除对话。"""
        if not task_ids:
            return

        def operation(client: QdrantClient):
            """仅处理本系统的历史集合。"""
            for collection in client.get_collections().collections:
                if collection.name.startswith("history_"):
                    client.delete(
                        collection.name,
                        models.FilterSelector(filter=_tasks_filter(task_ids)),
                        wait=True,
                    )

        await self._run(operation)

    async def reset(self, namespace: str) -> None:
        """仅重建当前模型的派生索引，保留原始对话。"""

        def operation(client: QdrantClient):
            """删除已有集合，下一次写入会重新创建。"""
            name = "history_" + namespace
            if client.collection_exists(name):
                client.delete_collection(name)

        await self._run(operation)

    async def close(self) -> None:
        """等待已开始的 SDK 操作结束后释放本地文件锁。"""

        def operation():
            """取消协程不会中止线程，因此关闭也遵守同一把锁。"""
            with self._lock:
                self._closed = True
                if self._client:
                    self._client.close()
                    self._client = None
                    logger.info("Qdrant向量数据库已关闭")

        await asyncio.to_thread(operation)

    async def _run(self, operation):
        """统一包装 SDK 错误；取消异常正常传播。"""

        def guarded():
            """本地客户端仅供本进程使用，锁保护跨工作线程访问。"""
            with self._lock:
                if self._closed:
                    raise EmbeddingError("向量数据库已关闭")
                if self._client is None:
                    self._client = (
                        QdrantClient(
                            url=self.url,
                            api_key=self.api_key or None,
                            trust_env=urlsplit(self.url).hostname
                            not in {"127.0.0.1", "localhost", "::1"},
                        )
                        if self.url
                        else QdrantClient(path=str(self.path), force_disable_check_same_thread=True)
                    )
                    logger.info("Qdrant向量数据库已打开，模式=%s", "服务" if self.url else "本地")
                return operation(self._client)

        try:
            return await asyncio.to_thread(guarded)
        except asyncio.CancelledError:
            raise
        except EmbeddingError:
            raise
        except Exception as exc:
            logger.exception("Qdrant操作失败")
            raise EmbeddingError("向量数据库操作失败，请查看当天诊断日志") from exc

    def _collection(self, client: QdrantClient, namespace: str, dimensions: int) -> str:
        """模型和维度对应不同集合，已有集合必须匹配配置。"""
        name = "history_" + namespace
        if not client.collection_exists(name):
            client.create_collection(
                name,
                vectors_config=models.VectorParams(
                    size=dimensions, distance=models.Distance.COSINE
                ),
            )
            if self.url:
                client.create_payload_index(
                    name, "task_id", models.PayloadSchemaType.KEYWORD, wait=True
                )
        elif client.get_collection(name).config.params.vectors.size != dimensions:
            raise EmbeddingError("向量集合维度不一致，请重建索引")
        return name


def _tasks_filter(task_ids: list[str]) -> models.Filter:
    """以业务记录编号做元数据过滤，不对查询文本做关键词匹配。"""
    return models.Filter(
        must=[models.FieldCondition(key="task_id", match=models.MatchAny(any=task_ids))]
    )
