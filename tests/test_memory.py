"""验证记忆自动管理、时效、跨模型检索及旧数据迁移。"""

import json
from datetime import date, timedelta

import aiosqlite
import pytest
from fastapi.testclient import TestClient
from test_app import MODEL, wait_terminal

from stock_research.app import create_app
from stock_research.config import Settings
from stock_research.memory import MemoryConflict, MemoryNotFound, MemoryStore
from stock_research.memory_schemas import MemoryInput
from stock_research.memory_tools import MemoryTools
from stock_research.storage import Repository


async def test_store_all_entries_and_versions(tmp_path):
    """全部记忆和日期保留，重复保存复用条目，版本冲突不会覆盖。"""
    repository = Repository(tmp_path)
    await repository.initialize()
    store = MemoryStore(repository.path)
    personal = await store.create(MemoryInput(content="我不使用杠杆", category="rule"))
    company = await store.create(
        MemoryInput(content="需要核查云业务利润质量", category="question", company="阿里巴巴")
    )
    expired = await store.create(
        MemoryInput(
            content="旧的投资限制", category="rule", expires_on=date.today() - timedelta(days=1)
        )
    )
    other = await store.create(
        MemoryInput(content="收入结构待核实", category="opinion", company="微软")
    )
    assert {row["id"] for row in await store.all_entries()} == {
        personal["id"],
        company["id"],
        expired["id"],
        other["id"],
    }
    duplicate = await store.create(MemoryInput(content="我不使用杠杆", category="rule"))
    assert duplicate["id"] == personal["id"]
    updated = await store.update(personal["id"], MemoryInput(content="我只买现货"), 1)
    assert updated["version"] == 2
    with pytest.raises(MemoryConflict):
        await store.delete(personal["id"], 1)
    await store.delete(personal["id"], 2)
    with pytest.raises(MemoryNotFound):
        await store.get(personal["id"])


async def test_tools_automatically_manage_memory_and_validate_sources(tmp_path):
    """系统可以自行保存、更新和删除，虚构用户引用仍被拒绝。"""
    repository = Repository(tmp_path)
    await repository.initialize()
    task = await repository.create_task(
        "", "继续研究测试公司", {"id": "test", "max_steps": 4}, kind="chat"
    )
    tools = MemoryTools(repository, task)
    with pytest.raises(ValueError, match="未知记忆工具"):
        await tools.execute("search_memory", {"content": "不能被误保存"})
    saved = await tools.execute(
        "save_memory",
        {
            "content": "需要持续跟踪经营现金流",
            "category": "question",
            "company": "测试公司",
        },
    )
    entry = saved["memory"]
    assert "requires_confirmation" not in saved and "status" not in entry
    assert entry["id"] in {row["id"] for row in await tools.store.all_entries()}
    with pytest.raises(ValueError, match="当前消息"):
        await tools.execute(
            "save_memory",
            {
                "content": "允许杠杆",
                "source_quote": "记住：允许杠杆",
            },
        )
    updated = await tools.execute(
        "update_memory",
        {
            "id": entry["id"],
            "version": 1,
            "content": "进一步核查自由现金流",
            "category": "question",
            "company": "测试公司",
        },
    )
    assert updated["memory"]["version"] == 2
    await tools.execute("delete_memory", {"id": entry["id"], "version": 2})
    assert not await tools.store.all_entries()


class MemoryModel:
    """模拟结构化记忆调用，保存实际模型请求供断言。"""

    def __init__(self, save=False):
        """指定首次消息是否调用保存工具。"""
        self.save = save
        self.requests = []

    async def complete(self, messages, tools):
        """隔离云端，不额外产生模型调用费用。"""
        self.requests.append(messages)
        if self.save and len(self.requests) == 1:
            names = {row["function"]["name"] for row in tools}
            assert {"save_memory", "update_memory", "delete_memory"} <= names
            assert "search_memory" not in names
            assert "search_memory" not in messages[0]["content"]
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "save-1",
                        "type": "function",
                        "function": {
                            "name": "save_memory",
                            "arguments": json.dumps(
                                {
                                    "content": "我不使用杠杆",
                                    "category": "rule",
                                    "source_quote": "我不使用杠杆",
                                },
                                ensure_ascii=False,
                            ),
                        },
                    }
                ],
            }
        return {"role": "assistant", "content": "测试回复"}


def test_cross_conversation_model_and_provenance_survives_delete(tmp_path):
    """记忆跨新对话及模型保留，来源对话删除后不再提供无效跳转。"""
    writer, reader = MemoryModel(save=True), MemoryModel()
    application = create_app(
        Settings(data_dir=tmp_path),
        lambda config: writer if config["model"] == "writer" else reader,
    )
    with TestClient(application) as client:
        first_model = client.post("/api/models", json={**MODEL, "model": "writer"}).json()
        second_model = client.post("/api/models", json={**MODEL, "model": "reader"}).json()
        first = client.post(
            "/api/tasks",
            json={
                "message": "记住：我不使用杠杆",
                "model_id": first_model["id"],
            },
        ).json()["id"]
        assert wait_terminal(client, first)["task"]["status"] == "completed"
        entry = client.get("/api/memories").json()["items"][0]
        assert entry["source_available"]
        second = client.post(
            "/api/tasks",
            json={
                "message": "谈谈投资方式",
                "model_id": second_model["id"],
            },
        ).json()["id"]
        detail = wait_terminal(client, second)
        assert detail["task"]["parent_id"] is None
        assert any(event["kind"] == "memories_recalled" for event in detail["events"])
        assert "我不使用杠杆" in reader.requests[0][1]["content"]
        assert client.delete("/api/conversations/" + first).status_code == 200
        stored = client.get("/api/memories").json()["items"][0]
        assert stored["content"] == entry["content"]
        assert stored["source_available"] is False
        assert client.delete("/api/memories/" + entry["id"] + "?version=1").status_code == 200
        third = client.post(
            "/api/tasks",
            json={
                "message": "你好",
                "model_id": second_model["id"],
            },
        ).json()["id"]
        wait_terminal(client, third)
        assert "我不使用杠杆" not in json.dumps(reader.requests[-1], ensure_ascii=False)


def test_memory_management_api_text_only_and_conflicts(tmp_path):
    """用户只填写内容即可增改，空内容和旧版本均明确报错。"""
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        created = client.post("/api/memories", json={"content": "关注经营现金流"})
        assert created.status_code == 201
        entry = created.json()
        assert "status" not in entry
        assert client.post("/api/memories", json={"content": " "}).status_code == 422
        updated = client.put(
            "/api/memories/" + entry["id"],
            json={
                "content": "重点关注现金流质量",
                "version": 1,
            },
        ).json()
        assert updated["version"] == 2
        assert (
            client.put(
                "/api/memories/" + entry["id"],
                json={
                    "content": "不要覆盖",
                    "version": 1,
                },
            ).status_code
            == 409
        )
        page = client.get("/api/memories?q=现金流&limit=1").json()
        assert page["total"] == 1 and page["items"][0]["id"] == entry["id"]
        assert client.delete("/api/memories/" + entry["id"] + "?version=2").status_code == 200
        assert client.get("/api/memories").json()["total"] == 0


def test_deleted_memory_is_refreshed_between_model_rounds(tmp_path):
    """管理页面删掉的条目不会残留在下一轮注入的参考消息中。"""

    class ChangingModel:
        """第一轮模拟用户通过管理页删除记忆。"""

        def __init__(self):
            """记录当前模型调用次数。"""
            self.calls = 0

        async def complete(self, messages, tools):
            """删除后调用计算器，触发下一轮真实图节点。"""
            self.calls += 1
            if self.calls == 1:
                assert "我不使用杠杆" in messages[1]["content"]
                store = application.state.runtime.memory_store
                rows = await store.list()
                entry = rows["items"][0]
                await store.delete(entry["id"], entry["version"])
                return {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "calc",
                            "type": "function",
                            "function": {
                                "name": "calculate",
                                "arguments": '{"expression":"1+1"}',
                            },
                        }
                    ],
                }
            assert not any("我不使用杠杆" in str(row.get("content")) for row in messages)
            return {"role": "assistant", "content": "已刷新参考"}

    model = ChangingModel()
    application = create_app(Settings(data_dir=tmp_path), lambda config: model)
    with TestClient(application) as client:
        client.post("/api/memories", json={"content": "我不使用杠杆", "category": "rule"})
        identifier = client.post("/api/models", json=MODEL).json()["id"]
        task_id = client.post(
            "/api/tasks",
            json={
                "message": "你好",
                "model_id": identifier,
            },
        ).json()["id"]
        detail = wait_terminal(client, task_id)
        assert detail["task"]["status"] == "completed"
        recalled = [row for row in detail["events"] if row["kind"] == "memories_recalled"]
        assert len(recalled[0]["data"]["items"]) == 1
        assert recalled[-1]["data"]["items"] == []
        assert model.calls == 2


async def test_old_memory_schema_migration_keeps_entries(tmp_path):
    """移除旧确认字段时保留记忆和版本，迁移可重复执行。"""
    repository = Repository(tmp_path)
    await repository.initialize()
    store = MemoryStore(repository.path)
    entry = await store.create(MemoryInput(content="已有记忆"))
    async with aiosqlite.connect(repository.path) as connection:
        await connection.execute(
            "ALTER TABLE memories ADD COLUMN status TEXT NOT NULL DEFAULT 'candidate'"
        )
        await connection.execute("CREATE INDEX memories_status ON memories(status,category)")
        await connection.commit()
    await repository.initialize()
    await repository.initialize()
    stored = await store.get(entry["id"])
    assert stored["content"] == "已有记忆"
    assert stored["version"] == entry["version"] and "status" not in stored
    assert entry["id"] in {row["id"] for row in await store.all_entries()}
