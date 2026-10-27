"""验证历史中文检索、分段读取、更新和删除同步。"""

import asyncio
import json
from datetime import date, timedelta

import httpx
import pytest
from embedding_fakes import FakeEmbedding

from stock_research.config import CredentialVault, Settings
from stock_research.diagnostics import DiagnosticRecorder, diagnostic_context, read_diagnostics
from stock_research.embeddings import EmbeddingConfig, EmbeddingError
from stock_research.history import HistoryStore
from stock_research.history_tools import HistoryTools
from stock_research.rag import VectorHistory
from stock_research.storage import Repository


@pytest.fixture
async def rag(tmp_path):
    """真实磁盘 Qdrant 配合离线 embedding，退出时释放文件锁。"""
    repository = Repository(tmp_path)
    await repository.initialize()
    vault = CredentialVault(tmp_path)
    identifier = await repository.save_model(
        {
            "base_url": "https://example.test/v1",
            "api_key_encrypted": vault.encrypt("test-secret"),
        }
    )
    models = {}

    def factory(config, key):
        """相同模型空间复用替身，便于观测重复编码。"""
        model = FakeEmbedding(config)
        return models.setdefault(model.namespace, model)

    async with httpx.AsyncClient() as client:
        service = VectorHistory(
            Settings(data_dir=tmp_path),
            repository,
            vault,
            client,
            DiagnosticRecorder(tmp_path / "logs"),
            factory,
        )
        await service.save_config(EmbeddingConfig(connection_id=identifier, dimensions=64))
        service.test_models = models
        try:
            yield service
        finally:
            await service.close()


async def test_history_chinese_search_read_updates_and_delete(tmp_path, rag):
    """旧对话可找回且标记日期，改名、修改回复及删除均同步索引。"""
    repository = Repository(tmp_path)
    await repository.initialize()
    identifier = await repository.create_task(
        "阿里巴巴",
        "为什么担忧云业务现金流？",
        {"id": "mock", "api_key_encrypted": "secret"},
    )
    report = "2025年财报分析：云业务利润增长与经营现金流需要对照核实。\n" * 60
    await repository.update_task(identifier, "completed", report=report)
    store = HistoryStore(repository.path, rag)
    result = await store.search("企业的造血能力", company="阿里巴巴")
    assert any(
        row["task_id"] == identifier and row["section"] == "report" for row in result["items"]
    )
    assert "secret" not in json.dumps(result)
    assert not (await store.search("现金流", company="微软"))["items"]
    assert not (await store.search("现金流", after=date.today() + timedelta(days=1)))["items"]
    first = await store.read(identifier, max_chars=500)
    second = await store.read(identifier, start=first["next_start"], max_chars=500)
    assert first["text"] + second["text"] == report[:1000]
    assert first["total_chars"] == len(report) and first["next_start"] == 500
    await repository.rename_conversation([identifier], "独特的新标题")
    assert all(row["title"] == "独特的新标题" for row in (await store.search("造血能力"))["items"])
    await repository.update_task(identifier, "completed", report="新的反证：毛利率变化尚需核查。")
    updated = await store.search("反证 毛利率")
    assert any("新的反证" in row["text"] for row in updated["items"])
    await store.save_summary("cache", identifier, 2, "历史摘要")
    await repository.delete_conversation([identifier])
    assert not (await store.search("毛利率"))["items"]
    assert await store.get_summary("cache") is None
    with pytest.raises(ValueError, match="已删除"):
        await store.read(identifier)


async def test_history_tools_read_archived_tool_results(tmp_path):
    """当前任务的工具原文可回读，读取自身不导致递归累积。"""
    repository = Repository(tmp_path)
    await repository.initialize()
    identifier = await repository.create_task("", "继续研究", {"id": "mock"}, kind="chat")
    await repository.add_event(
        identifier,
        "tool_finished",
        "读取完成",
        {
            "name": "read_source",
            "result": {"source_id": "S-test", "text": "收入123万元"},
        },
    )
    await repository.add_event(
        identifier,
        "tool_finished",
        "读取历史",
        {
            "name": "read_history",
            "result": {"text": "不要再次拼接这份读取结果"},
        },
    )
    tools = HistoryTools(HistoryStore(repository.path), identifier)
    raw = await tools.execute("read_history", {"task_id": "current", "section": "tools"})
    assert "收入123万元" in raw["text"]
    assert "不要再次拼接" not in raw["text"]
    with pytest.raises(ValueError):
        await tools.execute("read_history", {"task_id": identifier, "start": -1})
    with pytest.raises(ValueError):
        await tools.execute(
            "search_history",
            {
                "after": "2026-10-13",
                "before": "2026-10-12",
            },
        )
    assert (await tools.execute("search_history", {"query": ""}))["items"] == []


async def test_history_index_backfills_existing_records_idempotently(tmp_path, rag):
    """检索无需用户先保存为记忆，多次检索不会重复建立片段。"""
    repository = Repository(tmp_path)
    await repository.initialize()
    identifier = await repository.create_task("", "以前讨论过护城河", {"id": "mock"}, kind="chat")
    await repository.update_task(identifier, "completed", report="护城河需要结合竞争格局核实。")
    store = HistoryStore(repository.path, rag)
    first = await store.search("竞争壁垒")
    second = await store.search("竞争壁垒")
    assert first == second
    assert sum(model.document_calls for model in rag.test_models.values()) == 1
    assert {row["task_id"] for row in first["items"]} == {identifier}
    recent = await store.search("")
    assert len(recent["items"]) == 1 and recent["items"][0]["section"] == "question"


async def test_short_pair_is_one_point_and_returns_question_with_full_answer(rag):
    """仅问题含检索概念时也能命中对应回复，不再截掉短回答的后半段。"""
    question = "为什么担忧这家公司的经营现金流？"
    answer = "主要由于应收账款增加。" * 100 + "完整回答的末尾。"
    identifier = await rag.repository.create_task("", question, {"id": "mock"})
    await rag.repository.update_task(identifier, "completed", report=answer)
    model, _ = await rag._model()
    documents = []
    encode = model.embed_documents

    async def capture(texts):
        """检查真正送入嵌入模型的内容包含双方原文。"""
        documents.extend(texts)
        return await encode(texts)

    model.embed_documents = capture
    items = (await rag.search("企业的造血能力"))["items"]
    assert len(items) == len(documents) == 1
    assert question in documents[0] and answer in documents[0]
    item = items[0]
    assert item["question"] == question and question in item["text"] and answer in item["text"]
    assert item["section"] == "report" and item["start"] == 0 and item["end"] == len(answer)
    assert item["next_start"] is None
    assert (
        await rag.vectors._run(
            lambda client: client.count("history_" + model.namespace, exact=True).count
        )
        == 1
    )


async def test_long_answer_chunks_keep_question_and_original_read_positions(rag):
    """长回复每段携带原问题，重叠和续读位置仍严格对应回答原文。"""
    question = "这家公司的经营现金流有什么风险？"
    answer = "应收账款风险需要核实。" * 500
    identifier = await rag.repository.create_task("", question, {"id": "mock"})
    await rag.repository.update_task(identifier, "completed", report=answer)
    items = (await rag.search("造血能力", limit=8))["items"]
    assert len(items) > 1
    ordered = sorted(items, key=lambda item: item["start"])
    assert ordered[0]["start"] == 0 and ordered[-1]["end"] == len(answer)
    for previous, current in zip(ordered, ordered[1:], strict=False):
        assert current["start"] == previous["end"] - 200
    for item in items:
        fragment = answer[item["start"] : item["end"]]
        assert item["question"] == question
        assert item["text"] == f"用户提问：{question}\n模型回答：{fragment}"
        assert item["total_chars"] == len(answer)
        assert item["next_start"] == (item["end"] if item["end"] < len(answer) else None)
        raw = await rag.history.read(identifier, section=item["section"], start=item["start"])
        assert raw["text"].startswith(fragment)


async def test_question_without_answer_remains_searchable_without_fake_reply(rag):
    """失败记录保留提问检索，明确没有最终回复，不复制提问充当回答。"""
    question = "帮我研究公司的经营现金流"
    identifier = await rag.repository.create_task("", question, {"id": "mock"})
    await rag.repository.update_task(identifier, "failed")
    items = (await rag.search("造血能力"))["items"]
    assert len(items) == 1
    assert items[0]["section"] == "question" and items[0]["end"] == len(question)
    assert items[0]["text"].count(question) == 1
    assert "没有最终回复" in items[0]["text"]


async def test_rag_timings_separate_index_query_and_database_without_page_events(rag):
    """冷索引编码和查询分别计时；再次检索不重复编码，页面事件不增加计时内容。"""
    identifier = await rag.repository.create_task("", "经营现金流", {"id": "mock"})
    await rag.repository.update_task(identifier, "completed", report="需要核实回款")
    with diagnostic_context(task_id=identifier):
        assert (await rag.search("造血能力"))["items"]
    records = read_diagnostics(
        rag.diagnostics.directory, task_id=identifier, failed_only=False, limit=200
    )
    completed = {r["stage"] for r in records if r.get("status") == "succeeded"}
    assert {
        "timing.rag.total",
        "timing.rag.queue",
        "timing.rag.sync",
        "timing.rag.document_embedding",
        "timing.rag.query_embedding",
        "timing.rag.vector_write",
        "timing.rag.vector_query",
        "timing.rag.validate",
    } <= completed
    assert not await rag.history._rows(
        "SELECT 1 FROM events WHERE task_id=? AND kind LIKE 'timing%'", (identifier,)
    )
    before = sum(r["stage"] == "timing.rag.document_embedding" for r in records)
    with diagnostic_context(task_id=identifier):
        assert (await rag.search("造血能力"))["items"]
    later = read_diagnostics(
        rag.diagnostics.directory, task_id=identifier, failed_only=False, limit=200
    )
    assert sum(r["stage"] == "timing.rag.document_embedding" for r in later) == before


async def test_vectors_survive_restart_and_are_isolated_by_model(tmp_path, rag):
    """本地向量重启后可用，更换embedding不复用原模型的方向。"""
    repository = rag.repository
    identifier = await repository.create_task("", "竞争壁垒", {"id": "mock"})
    await repository.update_task(identifier, "completed", report="公司的护城河")
    assert (await rag.search("竞争壁垒"))["items"]
    first_calls = sum(model.document_calls for model in rag.test_models.values())
    await rag.vectors.close()
    from stock_research.vector_store import VectorStore

    rag.vectors = VectorStore(tmp_path / "vectors")
    assert (await rag.search("竞争壁垒"))["items"]
    assert sum(model.document_calls for model in rag.test_models.values()) == first_calls
    config = (await rag.config())["config"]
    config["model"] = "Qwen/Qwen3-VL-Embedding-8B"
    await rag.save_config(EmbeddingConfig.model_validate(config))
    assert (await rag.search("竞争壁垒"))["items"]
    assert len(rag.test_models) == 2
    assert sum(model.document_calls for model in rag.test_models.values()) == first_calls + 1
    assert not (await rag.search("造血能力"))["items"]
    assert not (await rag.search("竞争壁垒", exclude_task=identifier))["items"]


async def test_index_failure_preserves_original_and_can_retry(tmp_path, rag):
    """云端失败不记索引成功，也不丢失已保存的完整对话。"""
    repository = rag.repository
    identifier = await repository.create_task("", "经营现金流", {"id": "mock"})
    await repository.update_task(identifier, "completed", report="现金流需要核查")
    original_factory = rag.factory
    model, _ = await rag._model()
    original_encode = model.embed_documents

    async def fail(texts):
        """模拟服务商接口失败。"""
        raise EmbeddingError("HTTP 503")

    model.embed_documents = fail
    with pytest.raises(EmbeddingError):
        await rag.search("造血能力")
    assert (await repository.get_task(identifier))["report"] == "现金流需要核查"
    assert (await rag.config())["indexed_tasks"] == 0
    model.embed_documents = original_encode
    rag.factory = original_factory
    assert (await rag.search("造血能力"))["items"]


async def test_delete_during_encoding_does_not_resurrect_history(tmp_path, rag):
    """编码时删除原文后，不写回旧索引或向模型返回已删除资料。"""
    repository = rag.repository
    identifier = await repository.create_task("", "经营现金流", {"id": "mock"})
    await repository.update_task(identifier, "completed", report="现金流良好")
    model, _ = await rag._model()
    encode = model.embed_documents

    async def deleting(texts):
        """用真实删除模拟网络调用期间的并发管理操作。"""
        await repository.delete_conversation([identifier])
        return await encode(texts)

    model.embed_documents = deleting
    assert not (await rag.search("造血能力"))["items"]
    assert (await rag.config())["indexed_tasks"] == 0
    assert await repository.get_task(identifier) is None


async def test_index_cancellation_propagates_without_success_marker(tmp_path, rag):
    """任务停止能取消embedding请求，之后可正常重试。"""
    repository = rag.repository
    identifier = await repository.create_task("", "现金流", {"id": "mock"})
    await repository.update_task(identifier, "completed", report="经营现金流")
    model, _ = await rag._model()
    started = asyncio.Event()
    original = model.embed_documents

    async def blocked(texts):
        """模拟等待中的云端编码。"""
        started.set()
        await asyncio.Future()

    model.embed_documents = blocked
    task = asyncio.create_task(rag.search("造血能力"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await rag.config())["indexed_tasks"] == 0
    model.embed_documents = original
    assert (await rag.search("造血能力"))["items"]
