"""验证普通聊天、历史上下文、按需研究及旧数据迁移。"""

import asyncio
import json
import sqlite3
from functools import partial

import httpx
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from stock_research.app import create_app
from stock_research.config import CredentialVault, Settings
from stock_research.recall import RECALL_PREFIX
from stock_research.research import ResearchRuntime
from stock_research.storage import Repository

MODEL = {
    "name": "对话模型",
    "base_url": "https://model.example/v1",
    "model": "mock",
    "api_key": "test-secret",
    "max_steps": 4,
}
HTML = (
    "<html><head><title>测试公司财报</title></head><body><article>"
    "<h1>测试财报</h1><p>这些是自动化测试数据，不代表真实公司。"
    "公司收入为120万元，上一年度为100万元，主营业务为测试设备制造。</p>"
    "<p>本期经营现金流为30万元，测试报告用于验证资料解析和证据归档。"
    "所有金额单位为人民币万元，数据仅供测试使用。</p></article></body></html>"
).encode()


class ChatModel:
    """记录模型收到的历史并返回普通回复，不请求外部工具。"""

    def __init__(self):
        """初始化请求记录。"""
        self.requests = []

    async def complete(self, messages, tools):
        """无需原文也可正常完成日常对话。"""
        self.requests.append([dict(item) for item in messages])
        return {"role": "assistant", "content": f"测试回复{len(self.requests)}"}


def test_history_snapshots_branches_and_model_switch(tmp_path):
    """三轮聊天继承历史快照，分支隔离，当前消息可选择另一个模型。"""
    model = ChatModel()
    chosen = []

    def factory(config):
        """记录每轮实际使用的模型连接。"""
        chosen.append(config["name"])
        return model

    with TestClient(create_app(Settings(data_dir=tmp_path), factory)) as client:
        first_model = client.post("/api/models", json=MODEL).json()["id"]
        second_model = client.post("/api/models", json={**MODEL, "name": "另一个模型"}).json()["id"]
        root = send(client, "我叫小李。", first_model)
        assert root["task"]["kind"] == "chat"
        assert root["task"]["company"] == ""
        assert root["task"]["status"] == "completed"
        assert not root["sources"]
        assert len(model.requests) == 1
        root_id = root["task"]["id"]
        child = send(client, "我刚才说叫什么？", second_model, root_id)
        assert child["task"]["title"] == root["task"]["title"]
        assert child["task"]["model_snapshot"]["id"] == second_model
        client.portal.call(
            partial(
                client.app.state.repository.update_task,
                root_id,
                "completed",
                report="后续修改后的回复",
            )
        )
        send(client, "前面的两个问题是什么？", second_model, child["task"]["id"])
        # 自动召回是独立参考消息，不改变当前分支的原始对话快照。
        recalled = [
            item for item in model.requests[2][1:] if item["content"].startswith(RECALL_PREFIX)
        ]
        assert len(recalled) == 1
        history = [
            item for item in model.requests[2][1:] if not item["content"].startswith(RECALL_PREFIX)
        ]
        assert [item["role"] for item in history] == [
            "user",
            "assistant",
            "user",
            "assistant",
            "user",
        ]
        assert history[0]["content"] == "我叫小李。"
        assert history[1]["content"] == "测试回复1"
        assert history[2]["content"] == "我刚才说叫什么？"
        assert history[3]["content"] == "测试回复2"
        send(client, "从第一条消息另开一个分支。", first_model, root_id)
        branch = model.requests[3]
        assert not any(item["content"] == "我刚才说叫什么？" for item in branch)
        assert chosen == ["对话模型", "另一个模型", "另一个模型", "对话模型"]
        assert "test-secret" not in json.dumps(model.requests)


def test_unclear_company_can_be_clarified_without_forcing_research(tmp_path):
    """没有公司名称时允许模型先询问，不要求填写表单或空转到预算上限。"""

    class ClarifyingModel(ChatModel):
        """模拟模型根据意图询问缺失的研究对象。"""

        async def complete(self, messages, tools):
            """直接向用户澄清，不请求研究工具。"""
            self.requests.append([dict(item) for item in messages])
            return {"role": "assistant", "content": "你想研究哪家公司？"}

    model = ClarifyingModel()
    with TestClient(create_app(Settings(data_dir=tmp_path), lambda config: model)) as client:
        connection = client.post("/api/models", json=MODEL).json()["id"]
        result = send(client, "帮我研究一下。", connection)
        assert result["task"]["report"] == "你想研究哪家公司？"
        assert result["task"]["status"] == "completed"
        assert len(model.requests) == 1
        assert not any(event["kind"] == "tool_started" for event in result["events"])


def test_active_parent_rejected_and_paused_parent_accepts_new_message(tmp_path):
    """处理中禁止重复提交，停止后可换话题而不必先恢复原研究。"""

    class BlockingModel:
        """发出模型请求后等待取消。"""

        def __init__(self):
            """提供确定的启动信号。"""
            self.started = asyncio.Event()

        async def complete(self, messages, tools):
            """保留异步取消的正常传播。"""
            self.started.set()
            await asyncio.Event().wait()

    blocked = BlockingModel()
    chat = ChatModel()
    with TestClient(
        create_app(
            Settings(data_dir=tmp_path),
            lambda config: blocked if config["name"] == "阻塞模型" else chat,
        )
    ) as client:
        first_model = client.post("/api/models", json={**MODEL, "name": "阻塞模型"}).json()["id"]
        next_model = client.post("/api/models", json=MODEL).json()["id"]
        identifier = client.post(
            "/api/tasks", json={"message": "一条尚未完成的消息", "model_id": first_model}
        ).json()["id"]
        client.portal.call(blocked.started.wait)
        conflict = client.post(
            "/api/tasks",
            json={"message": "另一条消息", "model_id": next_model, "parent_id": identifier},
        )
        assert conflict.status_code == 409
        assert client.post(f"/api/tasks/{identifier}/pause").status_code == 200
        result = send(client, "先不处理刚才的问题了。", next_model, identifier)
        assert result["task"]["status"] == "completed"
        assert result["task"]["parent_id"] == identifier


@pytest.mark.parametrize("with_source", [True, False])
async def test_begin_research_enables_evidence_requirement(tmp_path, monkeypatch, with_source):
    """模型明确对象后启用研究，取得原文才完成，缺证据时仍标为部分完成。"""

    async def allow(url):
        """测试隔离网络，不查询示例域名。"""

    class ResearchModel:
        """模拟从聊天入口选择启用研究并读取原文。"""

        def __init__(self):
            """初始化模型调用计数。"""
            self.calls = 0

        async def complete(self, messages, tools):
            """普通入口通过结构化工具调用切换到证据研究。"""
            self.calls += 1
            if self.calls == 1:
                return call("begin", "begin_research", {"company": "测试公司"})
            if self.calls == 2 and with_source:
                return call("read", "read_source", {"url": "https://company.example/report"})
            return {
                "role": "assistant",
                "content": "测试研究报告。" if with_source else "原文不足。",
            }

    monkeypatch.setattr("stock_research.tools.validate_public_url", allow)
    repository = Repository(tmp_path)
    await repository.initialize()
    identifier = await repository.create_task(
        "", "帮我研究测试公司的经营质量。", {"id": "mock", "max_steps": 4}, kind="chat"
    )
    model = ResearchModel()
    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, content=HTML, headers={"content-type": "text/html"}
                )
            )
        ) as client,
        AsyncSqliteSaver.from_conn_string(str(tmp_path / "graph.sqlite")) as saver,
    ):
        await saver.setup()
        runtime = ResearchRuntime(
            Settings(data_dir=tmp_path),
            repository,
            CredentialVault(tmp_path),
            client,
            saver,
            lambda config: model,
        )
        runtime.submit(identifier)
        await runtime._tasks[identifier]
    task = await repository.get_task(identifier)
    assert task["kind"] == "research"
    assert task["company"] == "测试公司"
    assert task["status"] == ("completed" if with_source else "partial")
    assert len(await repository.sources(identifier)) == (1 if with_source else 0)


async def test_legacy_records_migrate_without_losing_report(tmp_path):
    """为旧数据库添加类型和标题，重复初始化不改变已有报告。"""
    with sqlite3.connect(tmp_path / "research.sqlite") as db:
        db.execute(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, company TEXT, question TEXT, model_id TEXT,"
            "model_snapshot TEXT, status TEXT, report TEXT DEFAULT '', error TEXT DEFAULT '',"
            "parent_id TEXT, previous_report TEXT DEFAULT '', created_at TEXT, updated_at TEXT)"
        )
        db.execute(
            "INSERT INTO tasks(id,company,question,model_id,model_snapshot,status,report,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "old",
                "旧公司",
                "旧问题",
                "model",
                '{"id":"model"}',
                "completed",
                "原始报告",
                "old",
                "old",
            ),
        )
    repository = Repository(tmp_path)
    await repository.initialize()
    await repository.initialize()
    task = await repository.get_task("old")
    assert task["title"] == "旧公司"
    assert task["kind"] == "research"
    assert task["report"] == "原始报告"
    assert task["status"] == "completed"


def send(client, message, model_id, parent_id=None):
    """通过真实消息接口提交，等待后台执行完成后读取持久化结果。"""
    response = client.post(
        "/api/tasks", json={"message": message, "model_id": model_id, "parent_id": parent_id}
    )
    assert response.status_code == 201, response.text
    identifier = response.json()["id"]

    async def wait():
        """等待本轮任务，任务已完成时直接读取结果。"""
        task = client.app.state.runtime._tasks.get(identifier)
        if task:
            await task

    client.portal.call(wait)
    return client.get(f"/api/tasks/{identifier}").json()


def call(identifier, name, arguments):
    """生成模拟服务商的规范工具消息。"""
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": identifier,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }
