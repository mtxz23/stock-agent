"""验证上下文压缩的预算、协议、缓存、取消和真实运行时接入。"""

import asyncio
import json

import pytest
from embedding_fakes import FakeEmbedding
from fastapi.testclient import TestClient
from test_app import MODEL, wait_terminal

from stock_research.app import create_app
from stock_research.config import Settings
from stock_research.context import SUMMARY_PREFIX, ContextManager, estimate_tokens
from stock_research.model_client import ModelError
from stock_research.storage import Repository


class SummaryModel:
    """返回简短摘要并检查每个分块请求都在本地预算内。"""

    def __init__(self):
        """保存摘要请求及输出预算。"""
        self.requests = []

    def factory(self, config):
        """记录压缩模型的局部配置，不创建真实云端连接。"""
        self.config = config
        return self

    async def complete(self, messages, tools):
        """只生成摘要，不执行资料中的指令。"""
        assert not tools
        assert estimate_tokens(messages) + self.config["max_tokens"] < self.config["context_budget"]
        self.requests.append(messages)
        return {
            "role": "assistant",
            "content": "此前讨论了现金流与利润背离，保留来源S-123及2025年数据，细节可回查原文。",
        }


async def make_task(repository, question="继续讨论", **config):
    """创建独立压缩测试任务。"""
    identifier = await repository.create_task(
        "",
        question,
        {"id": "mock", "context_budget": 8000, "max_tokens": 512, **config},
        kind="chat",
    )
    return await repository.get_task(identifier, internal=True)


async def test_context_compacts_keeps_memory_question_and_reuses_cache(tmp_path):
    """原始记录保持，记忆及当前问题不丢，下一条消息可复用旧摘要。"""
    repository = Repository(tmp_path)
    await repository.initialize()
    task = await make_task(repository)
    model = SummaryModel()
    manager = ContextManager(repository, model.factory)
    memory = {"role": "user", "content": "长期记忆参考（非指令）：我不使用杠杆"}
    body = []
    for number in range(12):
        body += [
            {"role": "user", "content": f"第{number}次讨论现金流"},
            {"role": "assistant", "content": "经营现金流、利润和证据需要分开核查。" * 50},
        ]
    body += [{"role": "user", "content": task["question"]}]
    messages = [{"role": "system", "content": "系统规则"}, memory] + body
    original = json.dumps(messages, ensure_ascii=False)
    compacted = await manager.prepare(task, messages, [], [])
    assert compacted[1] == memory
    assert compacted[-1]["content"] == task["question"]
    assert compacted[2]["content"].startswith(SUMMARY_PREFIX)
    assert estimate_tokens(compacted) < estimate_tokens(messages)
    assert json.dumps(messages, ensure_ascii=False) == original
    calls = len(model.requests)
    child = await make_task(repository, question="接着分析")
    extended = messages + [
        {"role": "assistant", "content": "上一次回答"},
        {"role": "user", "content": child["question"]},
    ]
    reused = await manager.prepare(child, extended, [task["id"]], [])
    assert len(model.requests) == calls and reused[-1]["content"] == child["question"]
    changed = json.loads(json.dumps(extended))
    changed[3]["content"] = "原回复已更正，不能复用原来的摘要"
    await manager.prepare(child, changed, [task["id"]], [])
    assert len(model.requests) > calls
    events = await repository.events(task["id"])
    assert any(row["kind"] == "context_compressed" for row in events)


async def test_context_tool_groups_are_atomic_and_current_question_restored(tmp_path):
    """即使压缩本轮工具结果，也不能保留半截调用或压缩掉当前提问。"""
    repository = Repository(tmp_path)
    await repository.initialize()
    task = await make_task(repository, context_budget=6000)
    await repository.begin_research(task["id"], "测试公司")
    task = await repository.get_task(task["id"], internal=True)
    model = SummaryModel()
    manager = ContextManager(repository, model.factory)
    calls = [
        {"id": "one", "function": {"name": "read_history", "arguments": "{}"}},
        {"id": "two", "function": {"name": "read_history", "arguments": "{}"}},
    ]
    messages = [
        {"role": "system", "content": "规则"},
        {"role": "user", "content": task["question"]},
        {"role": "assistant", "content": None, "tool_calls": calls},
        {"role": "tool", "tool_call_id": "one", "content": "第一份原文数据" * 1000},
        {"role": "tool", "tool_call_id": "two", "content": "第二份原文数据" * 1000},
        {"role": "assistant", "content": "已读取原文"},
    ]
    compacted = await manager.prepare(task, messages, [], [])
    assert any(
        row.get("role") == "user" and row["content"] == task["question"] for row in compacted
    )
    tool_ids = {row["tool_call_id"] for row in compacted if row["role"] == "tool"}
    call_ids = {call["id"] for row in compacted for call in row.get("tool_calls", [])}
    assert tool_ids == call_ids
    broken = [row for row in messages if row.get("tool_call_id") != "two"]
    with pytest.raises(ModelError, match="不完整"):
        await manager.prepare(task, broken, [], [])


async def test_context_does_not_drop_excessive_long_term_memory(tmp_path):
    """固定记忆超出预算时明确报错，不偷偷截断条目。"""
    repository = Repository(tmp_path)
    await repository.initialize()
    task = await make_task(repository, context_budget=4000)
    model = SummaryModel()
    messages = [
        {"role": "system", "content": "规则"},
        {"role": "user", "content": "长期记忆参考（非指令）：" + "全部保留" * 1500},
        {"role": "user", "content": task["question"]},
    ]
    with pytest.raises(ModelError, match="没有删除或截断"):
        await ContextManager(repository, model.factory).prepare(task, messages, [], [])
    assert not model.requests


async def test_context_cancellation_and_invalid_summary_do_not_save_cache(tmp_path):
    """取消向上传递，失败摘要不写缓存或改写原文。"""
    repository = Repository(tmp_path)
    await repository.initialize()
    task = await make_task(repository)
    started = asyncio.Event()

    class WaitingSummary:
        """等待取消的模型。"""

        async def complete(self, messages, tools):
            """取消由模型请求正常向上传播。"""
            started.set()
            await asyncio.Event().wait()

    messages = [
        {"role": "system", "content": "规则"},
        {"role": "assistant", "content": "旧的报告原文" * 2000},
        {"role": "user", "content": task["question"]},
    ]
    manager = ContextManager(repository, lambda config: WaitingSummary())
    running = asyncio.create_task(manager.prepare(task, messages, [], []))
    await started.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    class InvalidSummary:
        """模拟不合法的摘要回复。"""

        async def complete(self, messages, tools):
            """空摘要不能作为成功结果保存。"""
            return {"role": "assistant", "content": ""}

    manager = ContextManager(repository, lambda config: InvalidSummary())
    with pytest.raises(ModelError, match="有效"):
        await manager.prepare(task, messages, [], [])
    assert not await manager.history.summaries([task["id"]])


class RecallModel:
    """真实执行图中的压缩、检索和读取，替代云端推理。"""

    def __init__(self):
        """记录模型请求和待回忆的历史编号。"""
        self.main_requests = []
        self.summary_calls = 0
        self.target = ""

    async def complete(self, messages, tools):
        """摘要调用与普通工具调用相互独立。"""
        if messages[0]["content"].startswith("你负责压缩"):
            self.summary_calls += 1
            return {"role": "assistant", "content": "较早讨论了现金流风险，原文记录可回查。"}
        self.main_requests.append(messages)
        count = len(self.main_requests)
        if count == 1:
            assert {"search_history", "read_history"} <= {row["function"]["name"] for row in tools}
            name, arguments = "search_history", {"query": "经营现金流"}
        elif count == 2:
            name, arguments = "read_history", {"task_id": self.target, "max_chars": 500}
        else:
            return {"role": "assistant", "content": "根据历史记录，当时讨论的是经营现金流风险。"}
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"history-{count}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ],
        }


def test_runtime_compaction_and_history_tools_work_together(tmp_path):
    """长对话会压缩，模型还能回读原文，工具记录和摘要过程可回放。"""
    model = RecallModel()
    with TestClient(
        create_app(
            Settings(data_dir=tmp_path), lambda config: model, embedding_factory=FakeEmbedding
        )
    ) as client:
        connection = client.post(
            "/api/models",
            json={
                **MODEL,
                "max_steps": 4,
                "max_tokens": 512,
                "context_budget": 16000,
            },
        ).json()

        async def seed():
            """构造完整旧对话，避免任何真实云端调用。"""
            parent = None
            previous = ""
            for number in range(12):
                parent = await client.app.state.repository.create_task(
                    "",
                    f"第{number}次讨论经营现金流",
                    connection,
                    parent,
                    previous,
                    kind="chat",
                )
                previous = f"第{number}份历史记录：经营现金流与利润增长的背离。\n" * 120
                await client.app.state.repository.update_task(parent, "completed", report=previous)
                if number == 0:
                    model.target = parent
            return parent

        parent = client.portal.call(seed)
        identifier = client.post(
            "/api/tasks",
            json={
                "message": "上次为什么担忧现金流？",
                "model_id": connection["id"],
                "parent_id": parent,
            },
        ).json()["id"]
        detail = wait_terminal(client, identifier)
        assert detail["task"]["status"] == "completed"
        assert model.summary_calls > 0 and len(model.main_requests) == 3
        assert any(row["kind"] == "context_compressed" for row in detail["events"])
        finished = [
            row["data"]["name"] for row in detail["events"] if row["kind"] == "tool_finished"
        ]
        assert finished == ["search_history", "read_history"]
        assert client.get("/api/tasks/" + model.target).json()["task"]["report"].startswith("第0份")
        assert "test-secret" not in json.dumps(model.main_requests)


def test_runtime_injects_all_long_term_memories_without_relevance_limit(tmp_path):
    """不同公司和过期条目仍全部作为参考带入，不按十二条截断。"""
    model = RecallModel()
    with TestClient(create_app(Settings(data_dir=tmp_path), lambda config: model)) as client:
        connection = client.post("/api/models", json=MODEL).json()
        for number in range(30):
            client.post(
                "/api/memories",
                json={
                    "content": f"公司{number}的长期跟踪要点",
                    "category": "opinion",
                    "company": f"公司{number}",
                    "expires_on": "2000-01-01",
                },
            )
        # 本场景直接回复，不使用回忆工具。
        model.main_requests = [None, None]
        identifier = client.post(
            "/api/tasks",
            json={
                "message": "你好",
                "model_id": connection["id"],
            },
        ).json()["id"]
        assert wait_terminal(client, identifier)["task"]["status"] == "completed"
        payload = model.main_requests[-1][1]["content"].split("\n", 1)[1]
        assert len(json.loads(payload)) == 30
