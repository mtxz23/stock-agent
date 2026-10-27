"""验证云端向量协议、错误诊断及配置接口，不访问付费服务。"""

import asyncio
import hashlib
import json

import httpx
import pytest
from embedding_fakes import FakeEmbedding
from fastapi.testclient import TestClient
from test_app import MODEL

from stock_research.app import create_app
from stock_research.config import Settings
from stock_research.diagnostics import DiagnosticRecorder
from stock_research.embeddings import (
    QUERY_INSTRUCTION,
    CloudEmbeddingModel,
    EmbeddingError,
    embedding_namespace,
)

CONFIG = {
    "base_url": "https://example.test/v1",
    "model": "Qwen/Qwen3-Embedding-8B",
    "dimensions": 64,
}


def test_paired_history_uses_new_index_namespace():
    """问答编码升级必须隔离旧的独立问句和回复向量，触发历史补建。"""
    signature = [CONFIG["base_url"], CONFIG["model"], CONFIG["dimensions"], QUERY_INSTRUCTION]
    old_namespace = hashlib.sha256(
        json.dumps([*signature, "history-v1"]).encode("utf-8")
    ).hexdigest()
    assert embedding_namespace(CONFIG) != old_namespace


async def test_embedding_request_order_and_query_instruction():
    """请求包含指定维度，仅查询附加说明，响应按index还原顺序。"""
    requests = []

    def handler(request):
        """模拟乱序返回，检查密钥在请求头而不是正文中。"""
        body = json.loads(request.content)
        requests.append(body)
        assert request.url.path == "/v1/embeddings"
        assert request.headers["authorization"] == "Bearer test-secret"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": index, "embedding": [float(index + 1)] + [0.0] * 63}
                    for index in reversed(range(len(body["input"])))
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        model = CloudEmbeddingModel(client, CONFIG, "test-secret")
        vectors = await model.embed_documents(["第一份报告", "第二份报告"])
        assert [vector[0] for vector in vectors] == [1, 2]
        await model.embed_query("公司的现金创造能力")
    assert requests[0]["input"] == ["第一份报告", "第二份报告"]
    assert requests[1]["input"][0].startswith("Instruct:")
    assert requests[1]["input"][0].endswith("Query: 公司的现金创造能力")
    assert requests[0]["dimensions"] == 64 and "test-secret" not in json.dumps(requests)


@pytest.mark.parametrize(
    "payload",
    [
        {"data": []},
        {"data": [{"index": 0, "embedding": [1.0]}]},
        {"data": [{"index": 1, "embedding": [1.0] * 64}]},
        {"data": [{"index": 0, "embedding": [0.0] * 64}]},
        {"data": [{"index": 0, "embedding": [True] * 64}]},
    ],
)
async def test_embedding_rejects_invalid_vectors(payload):
    """错误的维度、下标或数值不能进入向量数据库。"""
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        model = CloudEmbeddingModel(client, CONFIG, "secret")
        with pytest.raises(EmbeddingError):
            await model.embed_documents(["报告"])


async def test_embedding_failure_diagnostics_are_redacted(tmp_path):
    """服务商错误有诊断编号，日志保留错误原因并过滤真实密钥。"""
    recorder = DiagnosticRecorder(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(401, json={"error": "invalid test-secret"})
        )
    ) as client:
        model = CloudEmbeddingModel(client, CONFIG, "test-secret", recorder)
        with pytest.raises(EmbeddingError, match="401") as error:
            await model.embed_query("私有研究内容")
    assert error.value.diagnostic_id
    records = "\n".join(path.read_text(encoding="utf-8") for path in tmp_path.glob("*.jsonl"))
    assert "test-secret" not in records and "私有研究内容" not in records
    assert "401" in records and "[REDACTED]" in records


async def test_embedding_retry_and_cancellation(monkeypatch):
    """限流可恢复，等待重试时停止任务也能传播取消。"""
    calls = 0

    async def sleep(seconds):
        """测试不等待真实退避时间。"""
        return None

    monkeypatch.setattr("stock_research.embeddings.asyncio.sleep", sleep)

    def handler(request):
        """首次限流，第二次返回有效向量。"""
        nonlocal calls
        calls += 1
        return (
            httpx.Response(429)
            if calls == 1
            else httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0] * 64}]})
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await CloudEmbeddingModel(client, CONFIG, "secret").embed_query("报告")
    assert calls == 2

    async def cancelled(request):
        """模拟HTTP传输被取消。"""
        raise asyncio.CancelledError

    async with httpx.AsyncClient(transport=httpx.MockTransport(cancelled)) as client:
        with pytest.raises(asyncio.CancelledError):
            await CloudEmbeddingModel(client, CONFIG, "secret").embed_query("报告")


def test_embedding_settings_index_and_conversation_delete(tmp_path):
    """API保存独立模型，重建和对话删除同步清理Qdrant中的历史。"""
    with TestClient(
        create_app(Settings(data_dir=tmp_path), embedding_factory=FakeEmbedding)
    ) as client:
        assert client.get("/api/embeddings/config").json()["config"] is None
        connection = client.post("/api/models", json=MODEL).json()
        payload = {"connection_id": connection["id"], "model": CONFIG["model"], "dimensions": 64}
        saved = client.put("/api/embeddings/config", json=payload)
        assert saved.status_code == 200 and "test-secret" not in saved.text
        assert client.post("/api/embeddings/test").status_code == 200

        async def seed():
            """只写业务历史，不触发主模型。"""
            repo = client.app.state.repository
            identifier = await repo.create_task("", "现金流", connection)
            await repo.update_task(identifier, "completed", report="经营现金流良好")
            return identifier

        identifier = client.portal.call(seed)
        result = client.post("/api/embeddings/reindex")
        assert result.status_code == 200 and result.json()["indexed_tasks"] == 1
        assert client.delete("/api/conversations/" + identifier).status_code == 200
        service = client.app.state.runtime.history_searcher
        assert not client.portal.call(service.search, "造血能力")["items"]

        async def count():
            """独立检查物理向量已清理，而非仅在查询时隐藏。"""
            return await service.vectors._run(
                lambda store: sum(
                    store.count(collection.name).count
                    for collection in store.get_collections().collections
                )
            )

        assert client.portal.call(count) == 0
        assert (
            client.put(
                "/api/embeddings/config", json={**payload, "connection_id": "missing"}
            ).status_code
            == 502
        )
