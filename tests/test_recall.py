"""验证首次自动召回、语义筛选、补查、检查点复用和可恢复失败。"""

import asyncio
import json

from embedding_fakes import FakeEmbedding
from fastapi.testclient import TestClient
from test_app import MODEL, wait_terminal

from stock_research.app import create_app
from stock_research.config import Settings
from stock_research.model_client import ModelError
from stock_research.recall import RECALL_PREFIX, recall_query, select_recall_items


class CaptureModel:
    """记录真实请求，按需执行多轮工具而不使用付费模型。"""

    def __init__(self, tool_rounds=0, fail=False):
        """工具轮数用于检查自动召回是否被反复执行。"""
        self.requests = []
        self.tool_rounds = tool_rounds
        self.fail = fail

    async def complete(self, messages, tools):
        """首次请求已能看见旧记录，之后保留已有召回内容。"""
        self.requests.append(messages)
        if self.fail:
            raise ModelError("模拟主模型临时失败")
        if len(self.requests) <= self.tool_rounds:
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"calc-{len(self.requests)}",
                        "type": "function",
                        "function": {"name": "calculate", "arguments": '{"expression":"1+1"}'},
                    }
                ],
            }
        return {"role": "assistant", "content": "这是结合历史参考的测试回复。"}


def seed(client):
    """保存独立旧对话，测试必须通过RAG找回而不是依赖当前分支。"""
    connection = client.post("/api/models", json=MODEL).json()
    assert (
        client.put(
            "/api/embeddings/config",
            json={
                "connection_id": connection["id"],
                "dimensions": 64,
            },
        ).status_code
        == 200
    )

    async def save():
        """原文保存在SQLite，索引由自动召回增量建立。"""
        repository = client.app.state.repository
        identifier = await repository.create_task(
            "测试公司",
            "经营现金流存在什么风险？",
            connection,
        )
        await repository.update_task(
            identifier, "completed", report="现金流与利润背离，应检查回款。"
        )
        return identifier

    return connection, client.portal.call(save)


def submit(client, connection, message, parent_id=None):
    """提交正常用户消息，通过实际运行时等到处理完成。"""
    response = client.post(
        "/api/tasks",
        json={
            "message": message,
            "model_id": connection["id"],
            "parent_id": parent_id,
        },
    )
    assert response.status_code == 201
    return wait_terminal(client, response.json()["id"])


def test_automatic_recall_precedes_model_and_runs_once_across_tool_rounds(tmp_path):
    """无需模型先调用search_history，第一次分析已经获得相关历史。"""
    model = CaptureModel(tool_rounds=2)
    instances = {}

    def factory(config, key):
        """复用替身计数，验证多轮模型分析不重复编码查询。"""
        embedding = FakeEmbedding(config)
        return instances.setdefault(embedding.namespace, embedding)

    with TestClient(
        create_app(Settings(data_dir=tmp_path), lambda config: model, embedding_factory=factory)
    ) as client:
        connection, historical_id = seed(client)
        detail = submit(client, connection, "企业的造血能力怎么样？")
        assert detail["task"]["status"] == "completed" and len(model.requests) == 3
        for request in model.requests:
            contexts = [
                message["content"]
                for message in request
                if str(message.get("content", "")).startswith(RECALL_PREFIX)
            ]
            assert len(contexts) == 1 and historical_id in contexts[0]
            assert "现金流与利润背离" in contexts[0]
            assert "经营现金流存在什么风险？" in contexts[0]
        kinds = [event["kind"] for event in detail["events"]]
        assert kinds.index("history_recalled") < kinds.index("model_request")
        assert kinds.count("history_recalled") == 1
        assert sum(instance.query_calls for instance in instances.values()) == 1
        assert "test-secret" not in json.dumps(model.requests)
        next_detail = submit(client, connection, "以前讨论的现金创造能力有什么风险？")
        assert next_detail["task"]["status"] == "completed"
        assert sum(instance.query_calls for instance in instances.values()) == 2


def test_casual_messages_and_missing_history_skip_embedding(tmp_path):
    """纯寒暄和没有旧记录时不产生云端查询，带问题的寒暄仍会检索。"""
    model = CaptureModel()

    def forbidden(config, key):
        """任何embedding请求都说明跳过条件失效。"""
        raise AssertionError("不应调用embedding")

    with TestClient(
        create_app(Settings(data_dir=tmp_path), lambda config: model, embedding_factory=forbidden)
    ) as client:
        connection = client.post("/api/models", json=MODEL).json()
        detail = submit(client, connection, "分析经营现金流")
        assert detail["task"]["status"] == "completed"
        assert any(event["kind"] == "history_recall_skipped" for event in detail["events"])
        detail = submit(client, connection, "你好！👋")
        assert detail["task"]["status"] == "completed"
        assert any(event["kind"] == "history_recall_skipped" for event in detail["events"])
        assert not any(
            str(message.get("content", "")).startswith(RECALL_PREFIX)
            for message in model.requests[-1]
        )
        detail = submit(client, connection, "你好，帮我分析现金流")
        assert any(event["kind"] == "history_recall_failed" for event in detail["events"])


def test_irrelevant_history_is_not_injected(tmp_path):
    """无关向量低于相关性门槛，不把历史内容硬塞进上下文。"""
    model = CaptureModel()
    with TestClient(create_app(Settings(data_dir=tmp_path), lambda config: model)) as client:
        connection, _ = seed(client)
        detail = submit(client, connection, "讨论竞争壁垒")
        recalled = [event for event in detail["events"] if event["kind"] == "history_recalled"]
        assert len(recalled) == 1 and recalled[0]["data"]["items"] == []
        assert not any(
            str(message.get("content", "")).startswith(RECALL_PREFIX)
            for message in model.requests[0]
        )


def test_automatic_recall_failure_is_visible_and_chat_continues(tmp_path):
    """辅助检索失败留诊断与页面事件，主模型仍可回答并用工具重试。"""
    model = CaptureModel()

    def failed(config, key):
        """模拟embedding配置或服务不可用。"""
        raise ValueError("模拟向量服务不可用")

    with TestClient(
        create_app(Settings(data_dir=tmp_path), lambda config: model, embedding_factory=failed)
    ) as client:
        connection, _ = seed(client)
        detail = submit(client, connection, "企业的造血能力")
        assert detail["task"]["status"] == "completed"
        event = next(
            event for event in detail["events"] if event["kind"] == "history_recall_failed"
        )
        assert event["data"]["diagnostic_id"]
        assert "自动历史召回未成功" in json.dumps(model.requests[0], ensure_ascii=False)
        assert len(client.get("/api/tasks").json()) == 2


def test_recall_checkpoint_is_reused_after_model_failure_and_restart(tmp_path):
    """首次模型请求失败后，在新应用恢复也不会重复自动召回。"""
    failed = CaptureModel(fail=True)
    with TestClient(create_app(Settings(data_dir=tmp_path), lambda config: failed)) as client:
        connection, historical_id = seed(client)
        detail = submit(client, connection, "企业的造血能力")
        assert detail["task"]["status"] == "failed"
        identifier = detail["task"]["id"]
    resumed = CaptureModel()

    def forbidden(config, key):
        """恢复路径必须复用已保存的召回结果。"""
        raise AssertionError("恢复后不应重复embedding调用")

    with TestClient(
        create_app(Settings(data_dir=tmp_path), lambda config: resumed, embedding_factory=forbidden)
    ) as client:
        assert client.post(f"/api/tasks/{identifier}/resume").status_code == 200
        detail = wait_terminal(client, identifier)
        assert detail["task"]["status"] == "completed"
        assert sum(event["kind"] == "history_recalled" for event in detail["events"]) == 1
        assert historical_id in json.dumps(resumed.requests[0])
        assert not any(event["kind"] == "history_recall_failed" for event in detail["events"])


def test_recall_cancellation_can_resume_and_complete(tmp_path):
    """召回请求被取消不标成功，恢复后可重试而不吞掉取消异常。"""
    model = CaptureModel()
    started = asyncio.Event()

    class BlockingEmbedding(FakeEmbedding):
        """模拟正在等待的查询向量服务。"""

        async def embed_query(self, query):
            """暂停任务应取消此等待。"""
            started.set()
            await asyncio.Future()

    with TestClient(
        create_app(
            Settings(data_dir=tmp_path), lambda config: model, embedding_factory=BlockingEmbedding
        )
    ) as client:
        connection, _ = seed(client)
        identifier = client.post(
            "/api/tasks",
            json={
                "message": "企业的造血能力",
                "model_id": connection["id"],
            },
        ).json()["id"]
        client.portal.call(started.wait)
        assert client.post(f"/api/tasks/{identifier}/pause").status_code == 200
        assert not model.requests
        client.app.state.runtime.history_searcher.factory = FakeEmbedding
        assert client.post(f"/api/tasks/{identifier}/resume").status_code == 200
        detail = wait_terminal(client, identifier)
        assert detail["task"]["status"] == "completed"
        assert sum(event["kind"] == "history_recalled" for event in detail["events"]) == 1
        assert not any(event["kind"] == "history_recall_failed" for event in detail["events"])


def test_query_uses_recent_context_and_recall_deduplicates_fragments():
    """省略公司名的追问保留最近语境，自动上下文最多三份不同原文。"""
    history = [
        {"question": "非常久以前的无关话题", "report": "旧回复"},
        {"question": "研究腾讯", "report": "讨论现金流"},
        {"question": "回款情况", "report": "应收款增加"},
    ]
    query = recall_query({"question": "那家公司后来怎么样了？", "company": ""}, history)
    assert "腾讯" in query and "那家公司" in query and "非常久以前" not in query
    snapshot = recall_query(
        {"question": "那家公司后来怎么样了？", "company": "", "previous_report": "当时的判断"},
        history,
    )
    assert "当时的判断" in snapshot and "应收款增加" not in snapshot
    items = [
        {"task_id": "a", "section": "report", "text": "片段一"},
        {"task_id": "a", "section": "report", "text": "重叠片段"},
        {"task_id": "a", "section": "question", "text": "同一问答的重复命中"},
        {"task_id": "b", "section": "report", "text": "片段二"},
        {"task_id": "c", "section": "report", "text": "片段三"},
        {"task_id": "d", "section": "report", "text": "片段四"},
    ]
    assert [item["text"] for item in select_recall_items(items)] == ["片段一", "片段二", "片段三"]
