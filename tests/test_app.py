"""验证配置脱敏、后台研究、检查点恢复及事件回放。"""

import asyncio
import json

from fastapi.testclient import TestClient

from stock_research.app import create_app
from stock_research.config import Settings
from stock_research.storage import Repository

MODEL = {
    "name": "测试连接",
    "base_url": "https://model.example/v1",
    "model": "test-model",
    "api_key": "test-secret",
    "max_steps": 3,
}


def test_credentials_encrypted_and_never_returned(tmp_path):
    """新增、列表、更新及输入校验均不回传密钥。"""
    application = create_app(Settings(data_dir=tmp_path))
    with TestClient(application) as client:
        created = client.post("/api/models", json=MODEL)
        assert created.status_code == 201
        identifier = created.json()["id"]
        assert "test-secret" not in created.text
        assert "api_key_encrypted" not in created.text
        updated = client.put(
            f"/api/models/{identifier}", json={**MODEL, "api_key": "", "name": "新名称"}
        )
        assert updated.json()["has_api_key"]
        assert "test-secret" not in client.get("/api/models").text
        invalid = client.post("/api/models", json={**MODEL, "api_key": "test-secret" * 250})
        assert invalid.status_code == 422
        assert "test-secret" not in invalid.text
    assert b"test-secret" not in (tmp_path / "research.sqlite").read_bytes()


def test_pending_connection_cannot_run_until_key_saved(tmp_path):
    """无密钥配置可保存；补齐前不允许测试或创建任务，留空编辑保持未启用。"""
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        created = client.post("/api/models", json={**MODEL, "api_key": ""})
        assert created.status_code == 201
        pending = created.json()
        assert pending["has_api_key"] is False
        identifier = pending["id"]
        updated = client.put(f"/api/models/{identifier}", json={**MODEL, "api_key": ""})
        assert updated.json()["has_api_key"] is False
        assert client.post(f"/api/models/{identifier}/test").status_code == 409
        blocked = client.post("/api/tasks", json={"message": "测试公司", "model_id": identifier})
        assert blocked.status_code == 409
        assert client.get("/api/tasks").json() == []
        enabled = client.put(f"/api/models/{identifier}", json=MODEL)
        assert enabled.json()["has_api_key"] is True
        assert "test-secret" not in enabled.text


def test_missing_model_and_blank_message_rejected(tmp_path):
    """无连接或空消息时不创建后台任务。"""
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        assert (
            client.post("/api/tasks", json={"message": "公司", "model_id": "missing"}).status_code
            == 404
        )
        assert (
            client.post("/api/tasks", json={"message": "   ", "model_id": "missing"}).status_code
            == 422
        )
        assert client.get("/api/tasks").json() == []


class PlainReplyModel:
    """模拟无需工具的普通对话回复。"""

    async def complete(self, messages, tools):
        """返回没有工具调用的测试回答。"""
        return {"role": "assistant", "content": "这是普通对话的测试回复。"}


def test_plain_chat_completes_and_sse_replays(tmp_path):
    """普通聊天无需原文即可完成，事件可补读，后续消息关联历史。"""
    with TestClient(
        create_app(Settings(data_dir=tmp_path), lambda config: PlainReplyModel())
    ) as client:
        model = client.post("/api/models", json=MODEL).json()
        identifier = client.post(
            "/api/tasks", json={"message": "测试公司", "model_id": model["id"]}
        ).json()["id"]
        detail = wait_terminal(client, identifier)
        assert detail["task"]["status"] == "completed"
        assert detail["sources"] == []
        assert sum(event["kind"] == "model_request" for event in detail["events"]) == 1
        assert "test-secret" not in json.dumps(detail)
        after = detail["events"][-2]["id"]
        response = client.get(f"/api/tasks/{identifier}/events?after={after}")
        assert response.status_code == 200
        assert f"id: {detail['events'][-1]['id']}" in response.text
        assert f"id: {detail['events'][0]['id']}\n" not in response.text
        child = client.post(
            "/api/tasks",
            json={"message": "核实业务", "model_id": model["id"], "parent_id": identifier},
        ).json()["id"]
        assert wait_terminal(client, child)["task"]["parent_id"] == identifier


async def test_restart_marks_inflight_interrupted(tmp_path):
    """进程重启后保留任务与事件，将未完成任务变为可继续状态。"""
    repository = Repository(tmp_path)
    await repository.initialize()
    identifier = await repository.create_task("测试公司", "", {"id": "model"})
    await repository.update_task(identifier, "running")
    await Repository(tmp_path).initialize()
    assert (await repository.get_task(identifier))["status"] == "interrupted"
    assert len(await repository.events(identifier)) == 1


class BlockingModel:
    """保持模型请求挂起，用于验证暂停和检查点恢复。"""

    async def complete(self, messages, tools):
        """等待取消，取消异常应直接传播。"""
        await asyncio.Event().wait()


def test_pause_then_resume_with_fresh_application(tmp_path):
    """暂停后在新进程运行时恢复持久化图状态，不依赖原来的内存。"""
    with TestClient(
        create_app(Settings(data_dir=tmp_path), lambda config: BlockingModel())
    ) as client:
        model = client.post("/api/models", json=MODEL).json()
        identifier = client.post(
            "/api/tasks", json={"message": "测试公司", "model_id": model["id"]}
        ).json()["id"]
        client.portal.call(wait_kind, client.app.state.repository, identifier, "model_request")
        assert client.post(f"/api/tasks/{identifier}/pause").status_code == 200
        assert client.get(f"/api/tasks/{identifier}").json()["task"]["status"] == "paused"
    with TestClient(
        create_app(Settings(data_dir=tmp_path), lambda config: PlainReplyModel())
    ) as client:
        assert client.post(f"/api/tasks/{identifier}/resume").status_code == 200
        detail = wait_terminal(client, identifier)
        assert detail["task"]["status"] == "completed"
        assert sum(event["kind"] == "started" for event in detail["events"]) == 2


async def wait_kind(repository, identifier, kind):
    """等待确定的持久化事件，不假设后台执行速度。"""
    async with asyncio.timeout(10):
        while not any(event["kind"] == kind for event in await repository.events(identifier)):
            await asyncio.sleep(0.01)


def wait_terminal(client, identifier):
    """通过测试客户端的运行循环等待任务进入终止状态。"""

    async def wait():
        """异步轮询存储，避免跨线程使用数据库连接。"""
        async with asyncio.timeout(10):
            while (await client.app.state.repository.get_task(identifier))["status"] in {
                "queued",
                "running",
            }:
                await asyncio.sleep(0.01)

    client.portal.call(wait)
    return client.get(f"/api/tasks/{identifier}").json()
