"""验证跨市场身份、来源协议、周期聚合、数据质量及聊天取证流程。"""

import json
from datetime import date, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from stock_research.app import create_app
from stock_research.config import Settings
from stock_research.diagnostics import DiagnosticRecorder
from stock_research.market_catalog import resolve_instrument, search_instruments
from stock_research.market_data import StockDataService
from stock_research.market_models import BarsInput, FundamentalsInput
from stock_research.market_series import prepare_series
from stock_research.market_sources import PublicStockSource
from stock_research.storage import Repository
from stock_research.technicals import analyze_bars, analyze_market_bars

pytestmark = pytest.mark.stock_data


@pytest.mark.parametrize(
    "value,expected",
    [
        ("多晶硅", "PS0.GFEX"),
        ("lc0", "LC0.GFEX"),
        ("生猪期货", "LH0.DCE"),
        ("ps2611", "PS2611.GFEX"),
        ("lc2611.gfex", "LC2611.GFEX"),
        ("纽约金", "GC.COMEX"),
        ("布伦特原油", "BRN.ICE"),
        ("纳斯达克", "IXIC.US"),
        ("日经225", "N225.JP"),
    ],
)
def test_market_identity_aliases_and_stock_finance_boundary(value, expected):
    """唯一别名匹配规范身份，公司财务不能接受任何非股票品种。"""
    request = BarsInput(symbol=value)
    assert request.symbol == expected and request.adjust == "none"
    with pytest.raises(ValidationError):
        FundamentalsInput(symbol=value)
    with pytest.raises(ValidationError, match="不进行股票复权"):
        BarsInput(symbol=value, adjust="hfq")


@pytest.mark.parametrize(
    "value",
    [
        "NDX",
        "NDX.US",
        "SI",
        "黄金",
        "原油",
        "PS0.DCE",
        "LH2611.GFEX",
        "PS2613.GFEX",
        "LC2600",
        "PS0;DROP TABLE",
        "GC2612.COMEX",
    ],
)
def test_ambiguous_or_conflicting_market_identity_is_rejected(value):
    """市场和合约冲突不能靠模糊匹配绕过，外盘月合约未适配时明确拒绝。"""
    with pytest.raises(ValidationError):
        BarsInput(symbol=value)


def test_catalog_pagination_and_ambiguous_product_discovery():
    """目录将工业硅与白银分开，黄金泛称可以列出不同口径而非擅自选一项。"""
    si = search_instruments("SI", "future", 100)
    assert {item["symbol"] for item in si["items"]} >= {"SI0.GFEX", "SI.COMEX"}
    gold = search_instruments("黄金", limit=100)
    assert {item["symbol"] for item in gold["items"]} == {"AU0.SHFE", "GC.COMEX", "XAU.SPOT"}
    first = search_instruments(limit=3)
    second = search_instruments(limit=3, offset=3)
    assert first["has_more"] and first["total"] > 40
    assert {item["symbol"] for item in first["items"]}.isdisjoint(
        {item["symbol"] for item in second["items"]}
    )
    assert resolve_instrument("AU0").price_unit == "元/克"
    assert resolve_instrument("AG0").price_unit == "元/千克"
    assert resolve_instrument("SC0").price_unit == "元/桶"
    assert resolve_instrument("JD0").price_unit == "元/500千克"


def test_unavailable_index_is_not_replaced_with_composite():
    """供应商身份冲突的品种注明未启用，不返回其他指数冒充。"""
    result = search_instruments("纳斯达克100")
    assert result["items"] == []
    assert result["unavailable"][0]["symbol"] == "NDX.US"
    assert "身份不一致" in result["unavailable"][0]["reason"]


def daily_rows(count=60):
    """生成含真实成交量、结算价和持仓的独立过去日期样本。"""
    start = date(2025, 1, 1)
    return [
        {
            "d": (start + timedelta(days=i)).isoformat(),
            "o": str(100 + i),
            "c": str(101 + i),
            "h": str(102 + i),
            "l": str(99 + i),
            "v": "10",
            "p": "20",
            "s": str(100.5 + i),
        }
        for i in range(count)
    ]


def market_response(request):
    """按固定服务返回不同协议，不连接公开网络或交易系统。"""
    rows = daily_rows()
    if "gtimg.cn" in request.url.host:
        vendor = request.url.params["param"].split(",")[0]
        assert request.url.params["param"].endswith(",day,,,640,")
        data = [[row["d"], row["o"], row["c"], row["h"], row["l"], row["v"]] for row in rows]
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {vendor: {"day": data, "qt": {vendor: ["200", "测试", vendor[2:]]}}},
            },
        )
    if "gi.finance" in request.url.host:
        assert request.url.params["num"] == "1000"
        return httpx.Response(200, json={"code": 0, "result": {"data": rows}})
    assert request.url.path.endswith(
        ("InnerFuturesNewService.getDailyKLine", "GlobalFuturesService.getGlobalFuturesDailyKLine")
    )
    if request.url.path.endswith("GlobalFuturesService.getGlobalFuturesDailyKLine"):
        rows = [
            {
                "date": row["d"],
                "open": row["o"],
                "close": row["c"],
                "high": row["h"],
                "low": row["l"],
                "volume": "0",
                "position": "0",
                "settlement": "0",
            }
            for row in rows
        ]
    return httpx.Response(200, text="/*supplier comment*/var _market=(" + json.dumps(rows) + ");")


@pytest.mark.parametrize(
    "symbol",
    [
        "PS0.GFEX",
        "LC2611.GFEX",
        "LH0.DCE",
        "SPX.US",
        "IXIC.US",
        "HSI.HK",
        "N225.JP",
        "FTSE.UK",
        "GC.COMEX",
        "BRN.ICE",
        "XAU.SPOT",
    ],
)
async def test_market_provider_units_quantity_and_source_identity(symbol):
    """五类来源遵循身份与口径契约，海外占位量不冒充有效量或结算价。"""
    async with httpx.AsyncClient(transport=httpx.MockTransport(market_response)) as client:
        result = await PublicStockSource(client).bars(BarsInput(symbol=symbol, limit=30))
    instrument = resolve_instrument(symbol)
    assert result["symbol"] == symbol and result["adjust"] == "none"
    assert result["fields"]["close"]["unit"] == instrument.price_unit
    assert result["time_zone"] == instrument.time_zone
    assert result["coverage"]["returned"] == 30
    assert result["rows"][-1]["close"] == 160
    assert result["evidence"][0]["url"]
    if instrument.provider == "sina_domestic":
        assert result["rows"][-1]["volume"] == 10
        assert result["rows"][-1]["settlement"] == 159.5
        assert result["rows"][-1]["open_interest"] == 20
        assert result["fields"]["volume"]["unit"] == "手"
    else:
        assert result["rows"][-1]["volume"] is None
        assert result["rows"][-1]["settlement"] is None
        assert "成交量缺失" in result["warning"]
    if instrument.provider == "tencent":
        assert result["coverage"]["upstream_daily_limit"] == 640


@pytest.mark.parametrize(
    "bad",
    [
        "var _market=window.alert(1);",
        'var _market=({"error":"failed"});',
        'var _market=([{ "d":"2025-01-01", "c":NaN }]);',
    ],
)
async def test_bad_jsonp_or_nonfinite_response_is_rejected(bad):
    """JSONP只解析固定包装里的JSON，脚本、业务对象与NaN均失败。"""
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=bad))
    ) as client:
        with pytest.raises(ValueError):
            await PublicStockSource(client).bars(BarsInput(symbol="PS0"))


async def test_wrong_global_index_identity_is_rejected():
    """不能用同一市场另一个指数冒充纳斯达克综合指数。"""

    def handler(request):
        """故意返回纳斯达克100的附带身份。"""
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"us.IXIC": {"day": [], "qt": {"us.IXIC": ["200", "错误指数", ".NDX"]}}},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="身份不匹配"):
            await PublicStockSource(client).bars(BarsInput(symbol="IXIC.US"))


def test_weekly_aggregation_has_no_future_leak_and_does_not_sum_positions():
    """历史截至周中时只能聚合截至该日的数据，缺失一天则周期总量未知。"""
    raw = [
        {
            "date": f"2025-01-{day:02d}",
            "open": day,
            "close": day + 1,
            "high": day + 2,
            "low": day - 1,
            "volume": None if day == 7 else 10,
            "settlement": day + 0.5,
            "open_interest": 100 + day,
        }
        for day in range(6, 11)
    ]
    request = BarsInput(symbol="PS0", period="week", start_date="2025-01-08", end_date="2025-01-08")
    result = prepare_series(raw, resolve_instrument(request.symbol), request)
    row = result["rows"][0]
    assert row["date"] == "2025-01-08" and row["trading_days"] == 3
    assert row["open"] == 6 and row["close"] == 9 and row["high"] == 10
    assert row["volume"] is None
    assert row["settlement"] == 8.5 and row["open_interest"] == 108
    assert result["coverage"]["source_end_date"] == "2025-01-10"


def test_duplicate_dates_negative_prices_and_ohlc_quality_are_distinct():
    """重复和非有限价格失败，期货负价可用，OHLC异常保留原值并注明。"""
    instrument = resolve_instrument("CL.NYMEX")
    row = {"date": "2020-04-20", "open": -10, "close": -37, "high": -9, "low": -40, "volume": 0}
    request = BarsInput(symbol="CL.NYMEX")
    result = prepare_series([row], instrument, request)
    assert result["rows"][0]["close"] == -37
    with pytest.raises(ValueError, match="重复"):
        prepare_series([row, row], instrument, request)
    with pytest.raises(ValueError, match="非有限"):
        prepare_series([{**row, "close": float("inf")}], instrument, request)
    anomaly = prepare_series([{**row, "high": -20}], instrument, request)
    assert anomaly["rows"][0]["high"] == -20
    assert anomaly["quality"]["flagged_periods"] == 1 and anomaly["warning"]


def test_missing_volume_and_roll_quality_do_not_generate_false_signals():
    """价格指标可独立计算，缺量、异常高低价与未核实换月不能生成对应信号。"""
    rows = [
        {
            "date": (date(2025, 1, 1) + timedelta(days=i)).isoformat(),
            "open": 100 + i,
            "close": 100 + i,
            "high": 100 + i,
            "low": 100 + i,
            "volume": None,
        }
        for i in range(200)
    ]
    direct = analyze_bars(rows)
    assert direct["indicators"]["ma5"] == 297
    assert direct["indicators"]["rsi14"] == 100
    assert direct["indicators"]["volume_ratio_20"] is None
    assert any("突破" in signal for signal in direct["signals"])
    rows[-10]["quality_flags"] = ["来源OHLC关系异常"]
    flagged = analyze_bars(rows)
    assert not any("突破" in signal for signal in flagged["signals"])
    rows[-10]["quality_flags"] = []
    continuous = analyze_market_bars(
        {"rows": rows, "contract_kind": "continuous", "fields": {"close": {"unit": "元/吨"}}}
    )
    assert continuous["price_unit"] == "元/吨"
    assert not any("突破" in signal for signal in continuous["signals"])
    assert "换月" in continuous["warning"]


async def test_continuous_specific_cache_and_quality_survive_failures(tmp_path):
    """连续与月合约分别缓存，缓存及失败回退不能丢掉来源质量警告。"""
    calls = []
    failing = False

    def handler(request):
        """对连续与具体合约返回不同值，并模拟后续断网。"""
        if failing:
            raise httpx.ConnectError("offline")
        calls.append(request.url.params["symbol"])
        rows = daily_rows()
        rows[-10]["h"] = "1"
        if request.url.params["symbol"] == "PS2611":
            rows[-1]["c"] = "159"
        return httpx.Response(200, text="var _market=(" + json.dumps(rows) + ");")

    repo = Repository(tmp_path)
    await repo.initialize()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = StockDataService(
            repo.path, DiagnosticRecorder(tmp_path / "logs"), source=PublicStockSource(client)
        )
        await service.start()
        try:
            continuous = BarsInput(symbol="PS0", limit=30)
            first = await service.query(continuous)
            specific = await service.query(BarsInput(symbol="PS2611", limit=30))
            assert first["rows"][-1]["close"] == 160 and specific["rows"][-1]["close"] == 159
            cached = await service.query(continuous)
            assert cached["status"] == "cached" and cached["warning"] == first["warning"]
            assert calls == ["PS0", "PS2611"]
            failing = True
            stale = await service.query(continuous.model_copy(update={"refresh": True}))
            assert stale["status"] == "stale" and "OHLC" in stale["warning"]
            assert stale["fetched_at"] == first["fetched_at"]
        finally:
            await service.close()


class MarketModel:
    """离线模拟品种检索、行情查询、技术计算和最后回答。"""

    async def complete(self, messages, tools):
        """逐轮验证实际返回，不调用收费模型或搜索引擎。"""
        definitions = {item["function"]["name"] for item in tools}
        assert {"search_market_symbols", "get_market_bars", "get_market_technicals"} <= definitions
        tool_rows = [item for item in messages if item.get("role") == "tool"]
        step = len(tool_rows)
        if step == 0:
            name, arguments = "search_market_symbols", {"query": "多晶硅"}
        elif step == 1:
            result = json.loads(tool_rows[-1]["content"])
            assert result["items"][0]["symbol"] == "PS0.GFEX"
            assert "source_id" not in result
            name, arguments = "get_market_bars", {"symbol": "PS0.GFEX", "limit": 60}
        elif step == 2:
            result = json.loads(tool_rows[-1]["content"])
            assert result["contract_kind"] == "continuous"
            assert result["rows"][-1]["close"] != result["rows"][-1]["settlement"]
            assert result["source_id"]
            name, arguments = "get_market_technicals", {"symbol": "PS0.GFEX", "limit": 60}
        else:
            result = json.loads(tool_rows[-1]["content"])
            assert result["analysis"]["indicators"]["ma5"] == 158
            assert result["source_id"]
            return {
                "role": "assistant",
                "content": "已查询多晶硅主力连续行情，并注明价格和换月口径。",
            }
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "market-" + str(step),
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ],
        }


def test_api_and_ai_market_tools_share_cache_and_archive(tmp_path):
    """目录无虚假研究证据，行情和指标归档来源，旧HTTP入口继续兼容。"""
    with TestClient(
        create_app(Settings(data_dir=tmp_path), lambda config: MarketModel())
    ) as client:
        http = httpx.AsyncClient(transport=httpx.MockTransport(market_response))
        client.app.state.runtime.stock_data._source = PublicStockSource(http)
        try:
            assert (
                client.get("/api/market/instruments", params={"query": "生猪"}).json()["items"][0][
                    "symbol"
                ]
                == "LH0.DCE"
            )
            assert client.get("/api/market/instruments", params={"limit": 101}).status_code == 422
            assert client.post("/api/market/bars", json={"symbol": "LC0"}).status_code == 200
            assert client.post("/api/stocks/bars", json={"symbol": "PS0"}).status_code == 200
            assert (
                client.post("/api/stocks/fundamentals", json={"symbol": "PS0"}).status_code == 422
            )
            technical = client.post("/api/market/technicals", json={"symbol": "GC.COMEX"}).json()
            assert technical["analysis"]["indicators"]["volume_ratio_20"] is None
            config = client.post(
                "/api/models",
                json={
                    "name": "测试",
                    "base_url": "https://mock.example/v1",
                    "model": "mock",
                    "api_key": "test",
                    "max_steps": 6,
                },
            ).json()
            task_id = client.post(
                "/api/tasks",
                json={"message": "查一下多晶硅期货价格和技术指标", "model_id": config["id"]},
            ).json()["id"]

            async def finish():
                """等待离线对话实际完成并检查证据数。"""
                task = client.app.state.runtime._tasks.get(task_id)
                if task:
                    await task

            client.portal.call(finish)
            detail = client.get("/api/tasks/" + task_id).json()
            assert detail["task"]["status"] == "completed"
            assert len(detail["sources"]) == 2
            archives = list((tmp_path / "sources" / task_id).glob("*.json"))
            assert len(archives) == 2
            assert all(
                json.loads(path.read_text(encoding="utf-8"))["provider_responses"]
                for path in archives
            )
            assert "search_web" not in json.dumps(detail["events"])
        finally:
            client.portal.call(http.aclose)
