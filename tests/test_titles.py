"""验证自动标题的更新边界、手动覆盖、失败回退和后台任务清理。"""

import asyncio
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from stock_research.app import create_app
from stock_research.config import Settings
from stock_research.diagnostics import DiagnosticRecorder
from stock_research.storage import Repository
from stock_research.titles import TITLE_PROMPT, ConversationTitles

pytestmark = pytest.mark.automatic_titles
MODEL = {
    "name": "测试连接", "base_url": "https://model.example/v1",
    "model": "mock", "api_key": "test-secret", "max_steps": 4,
}


class TitleModel:
    """区分正常回复和独立标题请求，支持控制命名请求的并发窗口。"""

    def __init__(self, *, waiting=False, reply=None):
        """初始化请求观测和离线结果。"""
        self.questions = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.waiting = waiting
        self.cancelled = False
        self.reply = reply
        self.configs = []

    async def complete(self, messages, tools):
        """回复不受命名阻塞，标题请求只接收前三次问题和独立规则。"""
        if messages[0]["content"] != TITLE_PROMPT:
            return {"role": "assistant", "content": "正常回复"}
        assert tools == []
        assert len(messages) == 2
        questions = json.loads(messages[1]["content"])["questions"]
        self.questions.append(questions)
        self.started.set()
        if self.waiting:
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply or {"role": "assistant", "content": f"公司研究主题{len(questions)}"}

    def factory(self, config):
        """记录命名配置，确认独立请求限制输出预算。"""
        self.configs.append(config)
        return self


def submit(client, model_id, question, parent=None):
    """只等待回复任务，独立命名可能仍在进行。"""
    response = client.post(
        "/api/tasks", json={"message": question, "model_id": model_id, "parent_id": parent}
    )
    assert response.status_code == 201, response.text
    task_id = response.json()["id"]

    async def wait():
        """等待运行时的回复协程。"""
        task = client.app.state.runtime._tasks.get(task_id)
        if task:
            await asyncio.wait_for(asyncio.shield(task), 5)

    client.portal.call(wait)
    return task_id


def wait_titles(client):
    """等待全部离线命名任务完成，不进行时间轮询。"""
    async def wait():
        """等待当前后台任务。"""
        jobs = list(client.app.state.runtime._titles._jobs.values())
        if jobs:
            await asyncio.wait_for(asyncio.gather(*jobs), 5)

    client.portal.call(wait)


def test_updates_first_three_questions_and_keeps_manual_title(tmp_path):
    """前三次提问逐步更新，第四次冻结，手动标题永久保留且作用于全部分支。"""
    model = TitleModel()
    with TestClient(create_app(Settings(data_dir=tmp_path), model.factory)) as client:
        model_id = client.post("/api/models", json=MODEL).json()["id"]
        parent = None
        ids = []
        for count in range(1, 5):
            parent = submit(client, model_id, f"提问{count}", parent)
            ids.append(parent)
            wait_titles(client)
            tasks = client.get("/api/tasks").json()
            assert {row["title"] for row in tasks} == {f"公司研究主题{min(count, 3)}"}
            assert not any(row["title_pending"] for row in tasks)
        assert model.questions == [["提问1"], ["提问1", "提问2"], ["提问1", "提问2", "提问3"]]
        assert any(config["max_tokens"] == 512 for config in model.configs)
        assert client.patch(
            f"/api/conversations/{ids[1]}", json={"title": "我自己的标题"}
        ).status_code == 200
        submit(client, model_id, "新分支", ids[0])
        wait_titles(client)
        assert len(model.questions) == 3
        assert {row["title"] for row in client.get("/api/tasks").json()} == {"我自己的标题"}


def test_background_naming_coalesces_followups_and_manual_rename_wins(tmp_path):
    """命名未完成时允许追问，合并最新问题，手动改名不会被迟到结果覆盖。"""
    model = TitleModel(waiting=True)
    with TestClient(create_app(Settings(data_dir=tmp_path), model.factory)) as client:
        model_id = client.post("/api/models", json=MODEL).json()["id"]
        root = submit(client, model_id, "先聊公司")
        client.portal.call(model.started.wait)
        root_task = client.get(f"/api/tasks/{root}").json()["task"]
        assert root_task["report"] == "正常回复"
        assert root_task["title_pending"] is True
        child = submit(client, model_id, "讨论利润", root)
        branch = submit(client, model_id, "现金流", root)
        client.portal.call(model.release.set)
        wait_titles(client)
        assert model.questions == [["先聊公司"], ["先聊公司", "讨论利润", "现金流"]]
        assert {row["title"] for row in client.get("/api/tasks").json()} == {"公司研究主题3"}
        assert client.get(f"/api/tasks/{child}").json()["task"]["title"] == "公司研究主题3"
        assert client.get(f"/api/tasks/{branch}").json()["task"]["title"] == "公司研究主题3"
        # 在另一段新对话的标题请求中执行手动改名。
        client.portal.call(model.release.clear)
        client.portal.call(model.started.clear)
        second = submit(client, model_id, "新的公司研究")
        client.portal.call(model.started.wait)
        assert client.patch(
            f"/api/conversations/{second}", json={"title": "用户决定的标题"}
        ).status_code == 200
        client.portal.call(model.release.set)
        wait_titles(client)
        submit(client, model_id, "后续问题", second)
        wait_titles(client)
        assert client.get(f"/api/tasks/{second}").json()["task"]["title"] == "用户决定的标题"
        assert len(model.questions) == 3


@pytest.mark.parametrize("reply", [
    ValueError("离线服务暂不可用"),
    {"content": "这是一个标题\n以及解释"},
    {"content": "过" * 33},
    {"content": "标题", "tool_calls": [{"id": "unexpected"}]},
])
def test_title_failure_preserves_reply_and_retries_on_next_question(tmp_path, reply):
    """命名异常或无效输出保持正常回复，下一次早期提问可以重新命名。"""
    model = TitleModel(reply=reply)
    with TestClient(create_app(Settings(data_dir=tmp_path), model.factory)) as client:
        model_id = client.post("/api/models", json=MODEL).json()["id"]
        root = submit(client, model_id, "原始公司问题")
        wait_titles(client)
        task = client.get(f"/api/tasks/{root}").json()["task"]
        assert task["title"] == "原始公司问题"
        assert task["status"] == "completed"
        assert task["report"] == "正常回复"
        assert not task["title_pending"]
        assert "conversation.title" in client.app.state.diagnostics.path.read_text(encoding="utf-8")
        model.reply = None
        submit(client, model_id, "继续讨论", root)
        wait_titles(client)
        assert client.get(f"/api/tasks/{root}").json()["task"]["title"] == "公司研究主题2"


def test_delete_and_shutdown_cancel_pending_title_requests(tmp_path):
    """删除和关闭取消模型等待，清除数据库中的命名状态。"""
    model = TitleModel(waiting=True)
    with TestClient(create_app(Settings(data_dir=tmp_path), model.factory)) as client:
        model_id = client.post("/api/models", json=MODEL).json()["id"]
        root = submit(client, model_id, "删除这段对话")
        client.portal.call(model.started.wait)
        assert client.delete(f"/api/conversations/{root}").status_code == 200
        assert model.cancelled
        assert not client.app.state.runtime._titles._jobs
        assert client.get("/api/tasks").json() == []
        model.cancelled = False
        client.portal.call(model.started.clear)
        submit(client, model_id, "保留但关闭服务")
        client.portal.call(model.started.wait)
    assert model.cancelled
    with sqlite3.connect(tmp_path / "research.sqlite") as connection:
        assert connection.execute("SELECT title_pending FROM tasks").fetchall() == [(0,)]


async def test_legacy_titles_and_interrupted_state_are_preserved(tmp_path):
    """升级数据库不改写旧标题，重启清除失效命名状态，追问继承最新标题。"""
    repository = Repository(tmp_path)
    await repository.initialize()
    legacy = await repository.create_task("原有公司", "旧问题", {"id": "mock"})
    modern = await repository.create_task("", "新问题", {"id": "mock"}, kind="chat")
    with sqlite3.connect(repository.path) as connection:
        connection.execute("UPDATE tasks SET title_pending=1 WHERE id=?", (modern,))
        # 模拟升级前的数据库，旧表没有三个自动命名字段。
        for name in ("title_auto", "title_attempt", "title_pending"):
            connection.execute(f"ALTER TABLE tasks DROP COLUMN {name}")
    await repository.initialize()
    old = await repository.get_task(legacy)
    assert old["title"] == "原有公司"
    assert not old["title_auto"]
    model = TitleModel()
    titles = ConversationTitles(repository, model.factory, DiagnosticRecorder(tmp_path / "logs"))
    await titles.schedule(legacy, {"id": "mock"})
    assert not titles._jobs
    fresh = await repository.create_task("", "新的问题", {"id": "mock"}, kind="chat")
    await repository.rename_conversation([fresh], "最新手动标题")
    child = await repository.create_task(
        "", "追问", {"id": "mock"}, fresh, kind="chat", title="请求里的过期标题"
    )
    assert (await repository.get_task(child))["title"] == "最新手动标题"
    with sqlite3.connect(repository.path) as connection:
        connection.execute("UPDATE tasks SET title_pending=1 WHERE id=?", (fresh,))
    await repository.initialize()
    assert not (await repository.get_task(fresh))["title_pending"]
    await titles.close()


async def test_cancel_before_worker_starts_clears_pending(tmp_path):
    """刚创建就取消的任务也会清除持久化标记，不留下永远轮询的状态。"""
    repository = Repository(tmp_path)
    await repository.initialize()
    task = await repository.create_task("", "测试取消", {"id": "mock"}, kind="chat")
    model = TitleModel(waiting=True)
    titles = ConversationTitles(repository, model.factory, DiagnosticRecorder(tmp_path / "logs"))
    await titles.schedule(task, {"id": "mock"})
    await titles.cancel([task])
    assert not titles._jobs
    assert not (await repository.get_task(task))["title_pending"]
    await titles.close()


async def test_timeout_keeps_fallback_and_invalidates_index_on_success(tmp_path, monkeypatch):
    """超时取消模型等待；下次成功命名后，全部分支的旧向量状态失效。"""
    from stock_research import titles as title_module

    monkeypatch.setattr(title_module, "TITLE_TIMEOUT", 0.02)
    repository = Repository(tmp_path)
    await repository.initialize()
    model = TitleModel(waiting=True)
    manager = ConversationTitles(repository, model.factory, DiagnosticRecorder(tmp_path / "logs"))
    root = await repository.create_task("", "公司利润", {"id": "mock"}, kind="chat")
    await manager.schedule(root, {"id": "mock"})
    await asyncio.gather(*list(manager._jobs.values()))
    assert model.cancelled
    assert (await repository.get_task(root))["title"] == "公司利润"
    assert not (await repository.get_task(root))["title_pending"]
    child = await repository.create_task("", "经营现金流", {"id": "mock"}, root, kind="chat")
    with sqlite3.connect(repository.path) as connection:
        connection.executemany(
            "INSERT INTO history_vector_state VALUES (?,?,?,?)",
            [("mock", identifier, "old", "old") for identifier in (root, child)],
        )
    model.waiting = False
    await manager.schedule(child, {"id": "mock"})
    await asyncio.gather(*list(manager._jobs.values()))
    assert (await repository.get_task(root))["title"] == "公司研究主题2"
    with sqlite3.connect(repository.path) as connection:
        assert connection.execute("SELECT * FROM history_vector_state").fetchall() == []
    await manager.close()
