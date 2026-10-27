"""验证证券名称的基础字典回退、跨市场匹配、缓存及失败处理。"""

import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from stock_research.diagnostics import DiagnosticRecorder
from stock_research.security_names import SecurityNames, decode_eastmoney, decode_sina_nasdaq
from stock_research.storage import Repository

pytestmark = pytest.mark.security_names


def snapshot():
    """包含国际指数、美股以及同码不同市场的测试快照。"""
    rows = [
        ("88", "IXIC"),
        ("88", "SPX"),
        ("185", "NVDA"),
        ("169", "IQV"),
        ("17", "600519"),
        ("USHA", "600519"),
        ("UNKNOWN", "SPX"),
    ]
    return {
        "items": [{"market": market, "code": code, "name": ""} for market, code in rows],
        "groups": [],
    }


@pytest.fixture
def mock_names(monkeypatch):
    """只模拟固定的公开名称服务，不访问网络或模型。"""
    requests = []
    original = httpx.AsyncClient

    def handler(request):
        """返回名称字段，包含一个不可接受的跨市场结果。"""
        requests.append(request)
        if request.url.host == "hq.sinajs.cn":
            return httpx.Response(
                200, content='var hq_str_int_nasdaq="纳斯达克,123,1";'.encode("gb18030")
            )
        names = {
            "100.SPX": "标普500",
            "105.NVDA": "英伟达",
            "106.IQV": "艾昆纬",
            "1.600519": "贵州茅台",
        }
        rows = []
        for key in request.url.params["secids"].split(","):
            if key in names:
                market, code = key.split(".", 1)
                rows.append({"f13": int(market), "f12": code, "f14": names[key]})
        rows.append({"f13": 0, "f12": "600519", "f14": "错误市场的证券"})
        return httpx.Response(200, json={"rc": 0, "data": {"diff": rows}})

    def client(**kwargs):
        """校验独立直连，不让此适配器依赖模型接口代理。"""
        assert kwargs["trust_env"] is False
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return requests


async def test_names_fill_aliases_cache_restart_and_local_priority(tmp_path, mock_names):
    """同一查询填充全部受支持别名，持久化结果可重启复用，本地名称优先。"""
    repo = Repository(tmp_path)
    await repo.initialize()
    recorder = DiagnosticRecorder(tmp_path / "logs")
    service = SecurityNames(repo.path, recorder)
    await service.start()
    data = snapshot()
    assert await service.enrich(data)
    assert [item["name"] for item in data["items"]] == [
        "纳斯达克",
        "标普500",
        "英伟达",
        "艾昆纬",
        "贵州茅台",
        "贵州茅台",
        "",
    ]
    assert data["items"][0]["name_provider"] == "新浪财经"
    assert data["items"][1]["name_provider"] == "东方财富"
    assert len(mock_names) == 2
    assert not await service.enrich(data)
    assert len(mock_names) == 2
    restarted = SecurityNames(repo.path, recorder)
    await restarted.start()
    new = snapshot()
    new["items"][2]["name"] = "本地英伟达名称"
    assert await restarted.enrich(new)
    assert new["items"][2]["name"] == "本地英伟达名称"
    assert len(mock_names) == 2
    # 缓存过期后重新查询，不把完整旧名称当作无需更新的本地字典。
    service._next_attempt = 0
    for entry in service._entries.values():
        entry["updated_at"] = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    assert await service.enrich(data)
    assert len(mock_names) == 4


async def test_provider_failure_preserves_names_and_retries_later(tmp_path, monkeypatch):
    """接口失败不抹掉已有名称，不在每次自选检查时重复联网。"""
    repo = Repository(tmp_path)
    await repo.initialize()
    recorder = DiagnosticRecorder(tmp_path / "logs")
    service = SecurityNames(repo.path, recorder)
    await service.start()
    original = httpx.AsyncClient
    calls = []

    def fail(request):
        """模拟服务暂不可用。"""
        calls.append(request)
        return httpx.Response(503)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(fail), **kwargs),
    )
    data = snapshot()
    data["items"][2]["name"] = "本地名称"
    before = deepcopy(data)
    assert not await service.enrich(data)
    assert data == before
    assert len(calls) == 2
    assert not await service.enrich(data)
    assert len(calls) == 2
    assert "watchlist.names" in recorder.path.read_text(encoding="utf-8")


async def test_name_cancellation_propagates(tmp_path, monkeypatch):
    """异步请求取消不保存成功结果，也不被当作普通网络失败。"""
    repo = Repository(tmp_path)
    await repo.initialize()
    service = SecurityNames(repo.path, DiagnosticRecorder(tmp_path / "logs"))
    await service.start()
    original = httpx.AsyncClient

    async def cancel(request):
        """模拟上层取消网络请求。"""
        raise asyncio.CancelledError

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(cancel), **kwargs),
    )
    with pytest.raises(asyncio.CancelledError):
        await service.enrich(snapshot())
    assert not service._entries


def test_response_validation_and_sina_decoding():
    """名称只有在明确请求的市场代码下才有效，不执行返回的脚本。"""
    raw = json.dumps(
        {
            "rc": 0,
            "data": {
                "diff": [
                    {"f13": 105, "f12": "NVDA", "f14": "英伟达"},
                    {"f13": 106, "f12": "NVDA", "f14": "不同市场"},
                    {"f13": 105, "f12": "EMPTY", "f14": "--"},
                ]
            },
        }
    ).encode()
    assert decode_eastmoney(raw, {"105.NVDA", "105.EMPTY"}) == {"105.NVDA": "英伟达"}
    with pytest.raises(ValueError):
        decode_eastmoney(b'{"rc":1}', {"105.NVDA"})
    assert (
        decode_sina_nasdaq('var hq_str_int_nasdaq="纳斯达克,123";'.encode("gb18030")) == "纳斯达克"
    )
    with pytest.raises(ValueError):
        decode_sina_nasdaq(b'alert("not a name")')


async def test_one_provider_failure_keeps_other_provider_names(tmp_path, monkeypatch):
    """东方财富失败时，新浪成功结果仍写入缓存并显示。"""
    repo = Repository(tmp_path)
    await repo.initialize()
    recorder = DiagnosticRecorder(tmp_path / "logs")
    service = SecurityNames(repo.path, recorder)
    await service.start()
    original = httpx.AsyncClient

    def handler(request):
        """模拟两个服务不同的可用状态。"""
        if request.url.host == "hq.sinajs.cn":
            return httpx.Response(
                200, content='var hq_str_int_nasdaq="纳斯达克,123";'.encode("gb18030")
            )
        return httpx.Response(503)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )
    data = snapshot()
    assert await service.enrich(data)
    assert data["items"][0]["name"] == "纳斯达克"
    assert data["items"][1]["name"] == ""
    assert ("88", "IXIC") in service._entries
    assert "watchlist.names" in recorder.path.read_text(encoding="utf-8")


async def test_provider_timeout_preserves_other_provider_success(tmp_path, monkeypatch):
    """一个服务超时不会丢弃另一个服务已经返回的成功名称。"""
    repo = Repository(tmp_path)
    await repo.initialize()
    service = SecurityNames(repo.path, DiagnosticRecorder(tmp_path / "logs"))
    await service.start()
    original_client = httpx.AsyncClient
    original_timeout = asyncio.timeout

    async def handler(request):
        """东方财富持续等待，新浪正常返回。"""
        if request.url.host != "hq.sinajs.cn":
            await asyncio.Future()
        return httpx.Response(
            200, content='var hq_str_int_nasdaq="纳斯达克,123";'.encode("gb18030")
        )

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    monkeypatch.setattr(asyncio, "timeout", lambda seconds: original_timeout(0.02))
    data = snapshot()
    assert await service.enrich(data)
    assert data["items"][0]["name"] == "纳斯达克"
    assert data["items"][1]["name"] == ""
