"""验证自选缓存协议、自动同步、失败保留、分页及模型工具集成。"""

import asyncio
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from stock_research.app import create_app
from stock_research.config import Settings
from stock_research.diagnostics import DiagnosticRecorder
from stock_research.storage import Repository
from stock_research.watchlist import WatchlistService
from stock_research.watchlist_source import TonghuashunReader, WatchlistSourceError, parse_cache


@pytest.fixture
def client_files(tmp_path):
    """构造临时客户端，不读写真实同花顺目录。"""
    install = tmp_path / "同花顺"
    folder = install / "bin" / "users" / "mock-user"
    folder.mkdir(parents=True)
    write_cache(folder)
    dictionary = install / "bin" / "stockname"
    dictionary.mkdir()
    with sqlite3.connect(dictionary / "stocknameV2.db") as db:
        db.execute("CREATE TABLE tablestock(CODE,NAME,MARKET,MARKETNAME,DATATAG)")
        db.executemany(
            "INSERT INTO tablestock VALUES (?,?,?,?,?)",
            [
                ("600519", "贵州茅台|贵州茅台@f", "USHA", "name_16_17", "Realtime"),
                ("000001", "平安银行", "USZA", "name_32_33", "Realtime"),
                ("000001", "上证指数", "USHI", "name_16_16", "Realtime"),
            ],
        )
    return install, folder


def write_cache(folder, codes=None, markets=None, groups=None):
    """生成代码序列、市场序列和分组，空列表也是有效快照。"""
    codes = ["600519", "000001", "000001"] if codes is None else codes
    markets = ["17", "33", "16"] if markets is None else markets
    data = {
        "StatusCode": 0,
        "Data": {
            "Selfstock": "|".join(codes) + "," + "|".join(markets),
            "Count": len(codes),
            "ModifyTime": "20261014090000",
        },
    }
    (folder / "SelfStockCache.json").write_text(json.dumps(data), encoding="utf-8")
    group_xml = (
        groups
        if groups is not None
        else (
            '<Block id="35" name="银行"><security market="USZA" code="000001" /></Block>'
            '<Block id="36" name="消费"><security market="USHA" code="600519" /></Block>'
            '<Block id="37" name="空分组" />'
        )
    )
    (folder / "blockstockV3.xml").write_text(
        '<hevo sort_list="24,23,25,">' + group_xml + "</hevo>", encoding="utf-8"
    )


def test_parses_parallel_sequences_and_preserves_market_identity(client_files):
    """不把市场序列当成股票，名称和分组关联严格遵守市场代码。"""
    install, folder = client_files
    files_before = {file.name: file.read_bytes() for file in folder.iterdir()}
    snapshot, fingerprint = TonghuashunReader(install).read()
    assert len(snapshot["items"]) == 3
    assert [row["name"] for row in snapshot["items"]] == ["贵州茅台", "平安银行", "上证指数"]
    assert snapshot["items"][1]["id"] != snapshot["items"][2]["id"]
    assert snapshot["items"][1]["code"] == "000001"
    assert snapshot["items"][1]["groups"] == ["银行"]
    assert snapshot["items"][2]["groups"] == []
    assert [group["name"] for group in snapshot["groups"]] == ["消费", "银行", "空分组"]
    assert snapshot["groups"][2]["items"] == []
    assert TonghuashunReader(install).read(fingerprint) == (None, fingerprint)
    assert files_before == {file.name: file.read_bytes() for file in folder.iterdir()}


@pytest.mark.parametrize(
    "payload",
    [
        {"StatusCode": 1},
        {"StatusCode": 0, "Data": {"Selfstock": "600519|17", "Count": 1}},
        {"StatusCode": 0, "Data": {"Selfstock": "600519|000001,17", "Count": 2}},
        {"StatusCode": 0, "Data": {"Selfstock": "600519,17", "Count": 2}},
    ],
)
def test_rejects_incomplete_self_cache(payload):
    """失败响应或并行序列缺失不能覆盖已同步列表。"""
    with pytest.raises(WatchlistSourceError):
        parse_cache(json.dumps(payload), "<hevo />")


def test_rejects_xml_entities_and_multiple_accounts(client_files):
    """不解析 XML 实体，多个客户端账号不能自动混合。"""
    install, folder = client_files
    with pytest.raises(WatchlistSourceError):
        parse_cache(
            (folder / "SelfStockCache.json").read_text(encoding="utf-8"),
            '<!DOCTYPE hevo [<!ENTITY x "test">]><hevo />',
        )
    second = folder.parent / "other-user"
    second.mkdir()
    write_cache(second)
    with pytest.raises(WatchlistSourceError, match="唯一"):
        TonghuashunReader(install).read()
    assert TonghuashunReader(install, "mock-user").read()[0] is not None
    with pytest.raises(WatchlistSourceError):
        TonghuashunReader(install, "../other-user").read()


async def test_sync_filter_failure_restart_and_empty_snapshot(client_files, tmp_path):
    """动态更新持久化快照，读取失败保留结果，完整空快照能正常清空。"""
    install, folder = client_files
    repo = Repository(tmp_path / "app")
    await repo.initialize()
    settings = Settings(ths_install_dir=install)
    manager = WatchlistService(settings, repo.path, DiagnosticRecorder(tmp_path / "logs"))
    await manager.start()
    try:
        page = await manager.query(limit=2)
        assert page["status"] == "ok" and page["total"] == 3 and page["has_more"]
        assert len((await manager.query(offset=2, limit=2))["items"]) == 1
        assert (await manager.query(q="茅台"))["items"][0]["code"] == "600519"
        assert (await manager.query(group="35"))["items"][0]["name"] == "平安银行"
        assert (await manager.query(group="消费"))["items"][0]["name"] == "贵州茅台"
        with pytest.raises(ValueError):
            await manager.query(group="不存在")
        (folder / "SelfStockCache.json").write_text('{"incomplete":', encoding="utf-8")
        cached = await manager.query()
        assert cached["status"] == "cached" and cached["total"] == 3
        assert cached["synced_at"] == page["synced_at"]
        assert cached["warning"]
    finally:
        await manager.close()
    # 缓存损坏情况下重启仍能展示数据库中的上次成功结果。
    recovered = WatchlistService(settings, repo.path, DiagnosticRecorder(tmp_path / "logs"))
    await recovered.start()
    try:
        assert (await recovered.query())["status"] == "cached"
        assert (await recovered.query())["watchlist_total"] == 3
        write_cache(folder, codes=[], markets=[], groups='<Block id="35" name="银行" />')
        empty = await recovered.query()
        assert empty["status"] == "ok" and empty["total"] == 0
        assert empty["groups"] == [{"id": "35", "name": "银行", "count": 0}]
    finally:
        await recovered.close()


async def test_background_poll_updates_without_frontend_requests(client_files, tmp_path):
    """浏览器关闭时轮询仍会读到修改，并在服务关闭时正确取消。"""
    install, folder = client_files
    repo = Repository(tmp_path / "app")
    await repo.initialize()
    manager = WatchlistService(
        Settings(ths_install_dir=install, watchlist_poll_interval=1),
        repo.path,
        DiagnosticRecorder(tmp_path / "logs"),
    )
    await manager.start()
    observed = asyncio.Event()
    original = manager.refresh

    async def observe():
        """在后台同步完成后通知测试，不用时间轮询数据库。"""
        await original()
        observed.set()

    manager.refresh = observe
    job = manager._job
    try:
        write_cache(folder, codes=["600519"], markets=["17"])
        await asyncio.wait_for(observed.wait(), 3)
        with sqlite3.connect(repo.path) as db:
            payload = json.loads(
                db.execute("SELECT payload_json FROM watchlist_snapshots").fetchone()[0]
            )
        assert len(payload["items"]) == 1
    finally:
        await manager.close()
    assert job.cancelled()
    assert manager._job is None


async def test_sync_cancellation_propagates_without_saving_snapshot(
    client_files, tmp_path, monkeypatch
):
    """取消读取不被包装成成功，也不替换已有快照。"""
    install, _ = client_files
    repo = Repository(tmp_path / "app")
    await repo.initialize()
    manager = WatchlistService(
        Settings(ths_install_dir=install), repo.path, DiagnosticRecorder(tmp_path / "logs")
    )
    await manager.start()

    async def cancel(*args):
        """模拟读取线程等待被上层取消。"""
        raise asyncio.CancelledError

    previous = manager._synced_at
    try:
        with monkeypatch.context() as isolated:
            isolated.setattr(asyncio, "to_thread", cancel)
            with pytest.raises(asyncio.CancelledError):
                await manager.refresh()
        assert manager._synced_at == previous
        assert manager._snapshot["items"]
    finally:
        await manager.close()


class WatchlistModel:
    """要求查询自选并检查真实工具结果，不调用外部模型。"""

    def __init__(self):
        """记录实际看到的工具结果。"""
        self.result = None

    async def complete(self, messages, tools):
        """先调用只读工具，再使用返回的证券名称回答。"""
        if messages[-1]["role"] == "tool":
            self.result = json.loads(messages[-1]["content"])
            return {"role": "assistant", "content": "消费分组中有贵州茅台"}
        assert any(tool["function"]["name"] == "get_watchlist" for tool in tools)
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "watchlist-1",
                    "type": "function",
                    "function": {"name": "get_watchlist", "arguments": '{"group":"消费"}'},
                }
            ],
        }


def test_api_and_model_query_current_watchlist(client_files, tmp_path):
    """页面分页与聊天工具共享实时检查的快照，参数错误清晰反馈。"""
    install, _ = client_files
    model = WatchlistModel()
    with TestClient(
        create_app(
            Settings(data_dir=tmp_path / "app", ths_install_dir=install), lambda config: model
        )
    ) as client:
        response = client.get("/api/watchlist?limit=2")
        assert response.status_code == 200
        assert response.json()["watchlist_total"] == 3
        assert len(response.json()["items"]) == 2
        assert client.get("/api/watchlist?group=bad").status_code == 422
        assert client.get("/api/watchlist?limit=201").status_code == 422
        config = client.post(
            "/api/models",
            json={
                "name": "测试",
                "base_url": "https://mock.example/v1",
                "model": "mock",
                "api_key": "test",
                "max_steps": 4,
            },
        ).json()
        task_id = client.post(
            "/api/tasks", json={"message": "我的消费自选有哪些？", "model_id": config["id"]}
        ).json()["id"]

        async def finish():
            """等待离线模型回复完成。"""
            task = client.app.state.runtime._tasks.get(task_id)
            if task:
                await task

        client.portal.call(finish)
        task = client.get(f"/api/tasks/{task_id}").json()["task"]
        assert task["status"] == "completed"
        assert model.result["total"] == 1
        assert model.result["items"][0]["name"] == "贵州茅台"
        assert model.result["status"] == "ok"


async def test_missing_client_is_unavailable_without_fake_empty_success(tmp_path):
    """没有安装客户端时明确报告不可用，而不是谎称自选为空。"""
    repo = Repository(tmp_path)
    await repo.initialize()
    manager = WatchlistService(Settings(), repo.path, DiagnosticRecorder(tmp_path / "logs"))
    await manager.start()
    try:
        result = await manager.query()
        assert result["status"] == "unavailable"
        assert result["warning"]
    finally:
        await manager.close()


def test_base_dictionary_fills_missing_realtime_without_historical_names(client_files):
    """优先实时名称，缺失时读取基础名称，不使用过时的历史名称。"""
    install, _ = client_files
    with sqlite3.connect(install / "bin" / "stockname" / "stocknameV2.db") as db:
        db.execute("DELETE FROM tablestock WHERE CODE='600519'")
        db.executemany(
            "INSERT INTO tablestock VALUES (?,?,?,?,?)",
            [
                ("600519", "贵州茅台基础名称", "USHA", "name_16_17", "Base"),
                ("600519", "历史旧名称", "USHA", "name_16_17", "History"),
                ("000001", "银行基础名称", "USZA", "name_32_33", "Base"),
            ],
        )
    data, _ = TonghuashunReader(install).read()
    assert data["items"][0]["name"] == "贵州茅台基础名称"
    assert data["items"][1]["name"] == "平安银行"


def test_known_indices_fill_offline_without_matching_other_markets(client_files):
    """常用指数不依赖联网，同代码股票不会使用指数名称。"""
    install, folder = client_files
    write_cache(
        folder,
        codes=["IXIC", "DJI", "SPX", "FTSE", "N225", "KS11", "000985", "899050", "000985", "SPX"],
        markets=["88", "88", "88", "88", "88", "88", "120", "144", "33", "UNKNOWN"],
        groups="",
    )
    data, _ = TonghuashunReader(install).read()
    assert [row["name"] for row in data["items"]] == [
        "纳斯达克综合指数",
        "道琼斯工业指数",
        "标普500指数",
        "英国富时100指数",
        "日经225指数",
        "韩国综合指数",
        "中证全指",
        "北证50",
        "",
        "",
    ]


async def test_name_only_change_persists_without_client_file_change(
    client_files, tmp_path, monkeypatch
):
    """客户端文件不变时仍能保存后续补全的名称，并支持名称搜索。"""
    install, folder = client_files
    write_cache(folder, codes=["NVDA"], markets=["185"], groups="")
    repo = Repository(tmp_path / "app")
    await repo.initialize()
    manager = WatchlistService(
        Settings(ths_install_dir=install), repo.path, DiagnosticRecorder(tmp_path / "logs")
    )
    await manager.start()
    fingerprint = manager._fingerprint

    async def fill(snapshot):
        """模拟名称服务后续恢复，不改写客户端来源。"""
        item = snapshot["items"][0]
        if item["name"]:
            return False
        item.update(
            name="英伟达", name_provider="测试名称源", name_updated_at="2026-10-14T00:00:00+00:00"
        )
        return True

    monkeypatch.setattr(manager._names, "enrich", fill)
    try:
        page = await manager.query(q="英伟达")
        assert page["total"] == 1
        assert page["items"][0]["code"] == "NVDA"
        assert manager._fingerprint == fingerprint
        with sqlite3.connect(repo.path) as db:
            saved = json.loads(
                db.execute("SELECT payload_json FROM watchlist_snapshots").fetchone()[0]
            )
        assert saved["items"][0]["name"] == "英伟达"
        assert not TonghuashunReader(install).read()[0]["items"][0]["name"]
    finally:
        await manager.close()
