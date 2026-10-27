"""验证整段对话的标题、分支删除和运行中任务清理。"""

import asyncio
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from stock_research.app import create_app
from stock_research.config import Settings
from stock_research.storage import Repository

MODEL = {
    "name": "测试连接",
    "base_url": "https://model.example/v1",
    "model": "mock",
    "api_key": "test-secret",
    "max_steps": 4,
}


class ReplyModel:
    """返回确定回复，隔离云端调用。"""

    async def complete(self, messages, tools):
        """直接完成当前提问。"""
        return {"role": "assistant", "content": "测试回复"}


class WaitingModel:
    """标记模型请求已经开始，并等待外部取消。"""

    def __init__(self):
        """初始化任务启动和取消观测信号。"""
        self.started = asyncio.Event()
        self.cancelled = False

    async def complete(self, messages, tools):
        """观察取消后继续向上传递异常。"""
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def test_rename_updates_all_branches_and_future_messages(tmp_path):
    """从分支改名后，所有现有及新增提问共享标题且原始消息保留。"""
    with TestClient(create_app(Settings(data_dir=tmp_path), lambda config: ReplyModel())) as client:
        model_id = client.post("/api/models", json=MODEL).json()["id"]
        root = submit(client, model_id, "原始提问")
        child = submit(client, model_id, "第二条", root)
        branch = submit(client, model_id, "另一个分支", root)
        response = client.patch(f"/api/conversations/{branch}", json={"title": "  自定标题  "})
        assert response.status_code == 200
        assert response.json()["title"] == "自定标题"
        for identifier in (root, child, branch):
            assert client.get(f"/api/tasks/{identifier}").json()["task"]["title"] == "自定标题"
        assert client.get(f"/api/tasks/{root}").json()["task"]["question"] == "原始提问"
        next_id = submit(client, model_id, "继续提问", child)
        assert client.get(f"/api/tasks/{next_id}").json()["task"]["title"] == "自定标题"
        assert all(row["title"] == "自定标题" for row in client.get("/api/tasks").json())


@pytest.mark.parametrize("title", ["", "   ", "x" * 81])
def test_invalid_title_does_not_change_conversation(tmp_path, title):
    """空白及超长标题返回字段错误，不覆盖已保存标题。"""
    with TestClient(create_app(Settings(data_dir=tmp_path), lambda config: ReplyModel())) as client:
        model_id = client.post("/api/models", json=MODEL).json()["id"]
        root = submit(client, model_id, "原标题")
        assert client.patch(f"/api/conversations/{root}", json={"title": title}).status_code == 422
        assert client.get(f"/api/tasks/{root}").json()["task"]["title"] == "原标题"


def test_delete_stops_active_branch_and_removes_only_its_conversation(tmp_path):
    """删除整段对话清理业务数据、检查点和原文，其他对话及模型保留。"""
    waiting = WaitingModel()
    with TestClient(
        create_app(
            Settings(data_dir=tmp_path),
            lambda config: waiting if config["name"] == "等待连接" else ReplyModel(),
        )
    ) as client:
        model_id = client.post("/api/models", json=MODEL).json()["id"]
        waiting_id = client.post("/api/models", json={**MODEL, "name": "等待连接"}).json()["id"]
        root = submit(client, model_id, "要删除的对话")
        child = submit(client, model_id, "第二条", root)
        branch = submit(client, model_id, "分支", root)
        unrelated = submit(client, model_id, "其他对话")
        active = client.post(
            "/api/tasks", json={"message": "等待中", "model_id": waiting_id, "parent_id": child}
        ).json()["id"]
        client.portal.call(waiting.started.wait)
        archive = tmp_path / "sources" / child
        archive.mkdir(parents=True)
        (archive / "test.html").write_text("测试原文", encoding="utf-8")
        source = client.portal.call(
            client.app.state.repository.save_source,
            child,
            {
                "url": "https://company.example/report",
                "title": "测试来源",
                "content_hash": "test",
                "archive_name": "test.html",
                "media_type": "text/html",
            },
        )
        preserved = tmp_path / "sources" / unrelated
        preserved.mkdir(parents=True)
        (preserved / "other.html").write_text("其他对话", encoding="utf-8")
        assert client.delete(f"/api/conversations/{branch}").status_code == 200
        assert waiting.cancelled
        assert active not in client.app.state.runtime._tasks
        assert not archive.exists()
        assert (preserved / "other.html").exists()
        for identifier in (root, child, branch, active):
            assert client.get(f"/api/tasks/{identifier}").status_code == 404
            assert client.post(f"/api/tasks/{identifier}/resume").status_code == 404
        assert client.get(f"/api/sources/{source['id']}/download").status_code == 404
        assert [row["id"] for row in client.get("/api/tasks").json()] == [unrelated]
        assert len(client.get("/api/models").json()) == 2
        with sqlite3.connect(tmp_path / "research.sqlite") as db:
            assert not db.execute("SELECT 1 FROM sources").fetchall()
            assert {row[0] for row in db.execute("SELECT task_id FROM events")} == {unrelated}
        with sqlite3.connect(tmp_path / "checkpoints.sqlite") as db:
            for table in ("checkpoints", "writes"):
                assert {row[0] for row in db.execute(f"SELECT thread_id FROM {table}")} == {
                    unrelated
                }
        assert client.delete(f"/api/conversations/{root}").status_code == 404
        missing = client.patch(f"/api/conversations/{root}", json={"title": "新标题"})
        assert missing.status_code == 404


def test_cleanup_failure_is_reported_and_can_be_retried(tmp_path, monkeypatch):
    """检查点清理失败时返回错误并保留业务记录，修复后允许重试删除。"""
    with TestClient(
        create_app(Settings(data_dir=tmp_path), lambda config: ReplyModel()),
        raise_server_exceptions=False,
    ) as client:
        model_id = client.post("/api/models", json=MODEL).json()["id"]
        root = submit(client, model_id, "待清理对话")
        saver = client.app.state.runtime._checkpointer
        original = saver.adelete_thread

        async def fail(identifier):
            """模拟检查点数据库写入失败。"""
            raise OSError("测试清理失败")

        monkeypatch.setattr(saver, "adelete_thread", fail)
        assert client.delete(f"/api/conversations/{root}").status_code == 500
        assert client.get(f"/api/tasks/{root}").status_code == 200
        monkeypatch.setattr(saver, "adelete_thread", original)
        assert client.delete(f"/api/conversations/{root}").status_code == 200


def test_delete_serializes_new_messages_without_leaving_orphans(tmp_path, monkeypatch):
    """删除过程中并发发来的追问必须等待，删除后明确拒绝已不存在的父记录。"""
    with TestClient(create_app(Settings(data_dir=tmp_path), lambda config: ReplyModel())) as client:
        model_id = client.post("/api/models", json=MODEL).json()["id"]
        root = submit(client, model_id, "删除时并发追问")
        saver = client.app.state.runtime._checkpointer
        original = saver.adelete_thread
        deleting = threading.Event()
        releasing = asyncio.Event()

        async def controlled_delete(identifier):
            """在清理阶段建立确定的并发窗口。"""
            deleting.set()
            await releasing.wait()
            await original(identifier)

        monkeypatch.setattr(saver, "adelete_thread", controlled_delete)
        with ThreadPoolExecutor(max_workers=2) as executor:
            deletion = executor.submit(client.delete, f"/api/conversations/{root}")
            assert deleting.wait(timeout=5)
            sending = executor.submit(
                client.post,
                "/api/tasks",
                json={"message": "并发追问", "model_id": model_id, "parent_id": root},
            )
            client.portal.call(releasing.set)
            assert deletion.result(timeout=5).status_code == 200
            assert sending.result(timeout=5).status_code == 404
        assert client.get("/api/tasks").json() == []
        assert not client.app.state.runtime._tasks


async def test_long_conversation_list_keeps_root_and_all_branches(tmp_path):
    """超过旧列表上限时仍返回根节点，管理操作覆盖完整对话。"""
    repository = Repository(tmp_path)
    await repository.initialize()
    model = {"id": "mock"}
    root = await repository.create_task("", "根消息", model, kind="chat")
    parent = root
    for _ in range(201):
        parent = await repository.create_task("", "后续消息", model, parent, kind="chat")
    tasks = await repository.list_tasks()
    assert len(tasks) == 202
    assert root in {row["id"] for row in tasks}
    assert len(await repository.conversation_tasks(parent)) == 202


def submit(client, model_id, message, parent_id=None):
    """等待一轮测试对话完成并返回任务标识。"""
    response = client.post(
        "/api/tasks", json={"message": message, "model_id": model_id, "parent_id": parent_id}
    )
    assert response.status_code == 201, response.text
    identifier = response.json()["id"]

    async def wait():
        """等待运行时持有的任务，已经完成时直接返回。"""
        task = client.app.state.runtime._tasks.get(identifier)
        if task:
            await task

    client.portal.call(wait)
    return identifier
