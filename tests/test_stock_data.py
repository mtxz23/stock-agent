"""验证股票数据身份、字段口径、缓存故障、证据归档及AI工具调用。"""

import asyncio
import json
import sqlite3
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from stock_research.app import create_app
from stock_research.config import Settings
from stock_research.diagnostics import DiagnosticRecorder
from stock_research.market_data import StockDataError, StockDataService
from stock_research.market_models import BarsInput, FundamentalsInput, normalize_symbol
from stock_research.market_sources import PublicStockSource, render_fundamentals
from stock_research.market_tools import STOCK_DATA_PROMPT, StockTools
from stock_research.storage import Repository, now
from stock_research.technicals import analyze_bars

pytestmark = pytest.mark.stock_data


class FakeSource:
    """提供独立于真实市场日期和用户账户的离线数据源。"""

    def __init__(self):
        """记录调用次数，支持模拟网络失败与取消。"""
        self.calls = 0
        self.failure = None

    async def fundamentals(self, request):
        """稳定字段来自模拟原始接口，不调用公开服务。"""
        self.calls += 1
        if self.failure:
            raise self.failure
        raw = {
            "SECUCODE": request.symbol,
            "gsmc": "测试公司",
            "agjc": "测试股票",
            "TOTALOPERATEREVE": 100,
            "PARENTNETPROFIT": None,
            "REPORT_DATE": "2025-12-31",
            "NOTICE_DATE": "2026-03-01",
        }
        return {
            "symbol": request.symbol,
            "name": "测试股票",
            "section": request.section,
            "provider": "离线来源",
            "currency": "CNY",
            "fetched_at": now(),
            "source_urls": ["https://mock.example/finance"],
            "available_fields": list(raw),
            "raw_rows": [raw],
            "basis": "报告期累计",
            "coverage": {"report_periods": ["2025-12-31"]},
            "evidence": [{"url": "https://mock.example/finance", "content": {"data": [raw]}}],
        }

    async def bars(self, request):
        """生成足够长的单调上升序列，便于独立验证指标。"""
        self.calls += 1
        if self.failure:
            raise self.failure
        rows = price_rows(request.limit)
        return {
            "symbol": request.symbol,
            "name": "测试股票",
            "section": "bars",
            "provider": "离线来源",
            "fetched_at": now(),
            "source_urls": ["https://mock.example/bars"],
            "rows": rows,
            "period": request.period,
            "adjust": request.adjust,
            "currency": "CNY",
            "coverage": {"returned": len(rows)},
            "fields": {"volume": {"unit": "股"}},
            "evidence": [{"url": "https://mock.example/bars", "content": rows}],
        }


@pytest.fixture
async def service(tmp_path):
    """每项测试使用独立数据库和离线供应商。"""
    repo = Repository(tmp_path)
    await repo.initialize()
    source = FakeSource()
    manager = StockDataService(repo.path, DiagnosticRecorder(tmp_path / "logs"), source=source)
    await manager.start()
    yield manager, source, repo
    await manager.close()


@pytest.mark.parametrize(
    "symbol,expected",
    [
        ("600519", "600519.SH"),
        ("sh600519", "600519.SH"),
        ("000001.SZ", "000001.SZ"),
        ("920185", "920185.BJ"),
    ],
)
def test_normalizes_stock_identity(symbol, expected):
    """股票前导零与市场身份不因字符串处理而丢失。"""
    assert normalize_symbol(symbol) == expected


@pytest.mark.parametrize(
    "symbol",
    ["000001.SH", "510300", "SH600519.SZ", "HK00700", "NVDA", '600519" OR 1=1', "贵州茅台"],
)
def test_rejects_indices_etfs_other_markets_and_injection(symbol):
    """公司工具不能误把其他证券或非法查询字符串作为A股。"""
    with pytest.raises(ValidationError):
        FundamentalsInput(symbol=symbol)


def test_rejects_reversed_dates_and_invalid_fields():
    """反向时间或注入字段在联网之前拒绝。"""
    with pytest.raises(ValidationError):
        BarsInput(symbol="600519", start_date="2025-03-01", end_date="2025-01-01")
    with pytest.raises(ValidationError):
        FundamentalsInput(symbol="600519", fields=["foo;DROP TABLE"])


async def test_cache_restart_projection_failure_and_expiry(service, tmp_path):
    """字段裁剪复用同一缓存，断网不抹数据，抓取日期保持原样。"""
    manager, source, repo = service
    request = FundamentalsInput(symbol="600519", section="indicators")
    first = await manager.query(request)
    assert first["status"] == "fresh"
    assert first["rows"][0]["revenue"] == 100
    assert first["rows"][0]["net_profit_parent"] is None
    assert "net_profit_parent" in first["rows"][0]["missing_fields"]
    projected = await manager.query(request.model_copy(update={"fields": ["revenue"]}))
    assert projected["status"] == "cached"
    assert "roe_pct" not in projected["rows"][0]
    assert source.calls == 1
    restarted = StockDataService(repo.path, DiagnosticRecorder(tmp_path / "logs2"), source=source)
    await restarted.start()
    try:
        assert (await restarted.query(request))["status"] == "cached"
        assert source.calls == 1
        source.failure = httpx.ConnectError("offline")
        stale = await restarted.query(request.model_copy(update={"refresh": True}))
        assert stale["status"] == "stale" and stale["warning"]
        assert stale["fetched_at"] == first["fetched_at"]
        assert source.calls == 2
        # 真正过期的缓存与重试保护一起验证，避免每次查询都继续联网。
        with sqlite3.connect(repo.path) as db:
            payload = json.loads(
                db.execute("SELECT payload_json FROM stock_data_cache").fetchone()[0]
            )
            payload["fetched_at"] = (datetime.now(UTC) - timedelta(days=2)).isoformat()
            db.execute("UPDATE stock_data_cache SET payload_json=?", (json.dumps(payload),))
        assert (await restarted.query(request))["status"] == "stale"
        assert source.calls == 2
        payload["fetched_at"] = (datetime.now(UTC) - timedelta(days=31)).isoformat()
        with sqlite3.connect(repo.path) as db:
            db.execute("UPDATE stock_data_cache SET payload_json=?", (json.dumps(payload),))
        with pytest.raises(StockDataError):
            await restarted.query(request)
    finally:
        await restarted.close()


async def test_failure_without_cache_and_cancellation_do_not_write_success(service):
    """无缓存失败不是空数据成功，取消异常正常传播且不保存结果。"""
    manager, source, repo = service
    source.failure = httpx.ConnectError("offline")
    with pytest.raises(StockDataError):
        await manager.query(BarsInput(symbol="600519"))
    source.failure = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await manager.query(BarsInput(symbol="600519", refresh=True))
    with sqlite3.connect(repo.path) as db:
        assert db.execute("SELECT COUNT(*) FROM stock_data_cache").fetchone()[0] == 0


async def test_company_type_and_full_statement_protocol(tmp_path):
    """从HTML读取银行类型，按可用报告期获取报表并验证市场身份。"""
    seen = []

    def handler(request):
        """模拟银行报表端点及必要的类型、日期响应。"""
        seen.append(request)
        if request.url.path.endswith("/Index"):
            return httpx.Response(200, text='<input value="3" type="hidden" id="hidctype">')
        assert request.url.params["companyType"] == "3"
        if "DateAjaxNew" in request.url.path:
            return httpx.Response(200, json={"data": [{"REPORT_DATE": "2025-12-31"}]})
        assert request.url.params["dates"] == "2025-12-31"
        assert request.url.params["reportType"] == "1"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "SECUCODE": "000001.SZ",
                        "REPORT_DATE": "2025-12-31",
                        "NOTICE_DATE": "2026-03-01",
                        "OPERATE_INCOME": 123,
                        "TOTAL_OPERATE_INCOME": None,
                        "CUSTOM_FIELD": 77,
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = PublicStockSource(client)
        payload = await source.fundamentals(FundamentalsInput(symbol="000001", section="income"))
        rendered = render_fundamentals(payload, [])
        assert rendered["rows"][0]["revenue"] is None
        assert rendered["rows"][0]["operating_revenue"] == 123
        full = render_fundamentals(payload, ["*"])
        assert full["rows"][0]["CUSTOM_FIELD"] == 77
        assert full["fields"]["CUSTOM_FIELD"]["unit"] == "来源原始口径未归一化"
        with pytest.raises(ValueError, match="字段"):
            render_fundamentals(payload, ["MISSING_FIELD"])
    assert len(seen) == 3


async def test_provider_rejects_wrong_security_and_adjustment():
    """不能接受同码其他市场，不能以不复权数据冒充前复权。"""

    def handler(request):
        """返回错市场公司或缺失指定复权的K线。"""
        if "CompanySurvey" in request.url.path:
            return httpx.Response(200, json={"SecuCode": "600519.SZ", "jbzl": {}})
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"sh600519": {"day": [["2025-01-02", "10", "11", "12", "9", "3"]]}},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = PublicStockSource(client)
        with pytest.raises(ValueError, match="身份"):
            await source.fundamentals(FundamentalsInput(symbol="600519"))
        with pytest.raises(ValueError, match="复权"):
            await source.bars(BarsInput(symbol="600519", adjust="qfq"))
        rows = (await source.bars(BarsInput(symbol="600519", adjust="none")))["rows"]
        assert rows[0]["volume"] == 300
        assert rows[0]["open"] == 10 and rows[0]["close"] == 11


async def test_adjusted_negative_prices_and_duplicate_dates():
    """长期前复权负价可有效，重复日期和非有限价格则拒绝。"""
    raw = [["2025-01-02", "-10", "-9", "-8", "-11", "3"]]

    def handler(request):
        """保留指定证券与复权的原始价格序列。"""
        return httpx.Response(200, json={"code": 0, "data": {"sh600519": {"qfqday": raw}}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = PublicStockSource(client)
        assert (await source.bars(BarsInput(symbol="600519")))["rows"][0]["close"] == -9
        raw.append(deepcopy(raw[0]))
        with pytest.raises(ValueError, match="重复"):
            await source.bars(BarsInput(symbol="600519"))
        raw[:] = [["2025-01-02", "10", "nan", "12", "9", "3"]]
        with pytest.raises(ValueError, match="异常"):
            await source.bars(BarsInput(symbol="600519"))


def test_business_percent_and_missing_fields_have_correct_units():
    """主营比例从小数转成百分比，金融公司缺失毛利率不填零。"""
    payload = {
        "section": "business",
        "currency": "CNY",
        "raw_rows": [{"MBI_RATIO": 0.125, "GROSS_RPOFIT_RATIO": None}],
        "available_fields": ["MBI_RATIO", "GROSS_RPOFIT_RATIO"],
        "evidence": [],
    }
    row = render_fundamentals(payload, ["revenue_share_pct", "gross_margin_pct"])["rows"][0]
    assert row["revenue_share_pct"] == 12.5
    assert row["gross_margin_pct"] is None


def test_technical_indicators_and_breakout_use_previous_bars_only():
    """独立验证线性序列均线、RSI与突破，历史不足不造指标。"""
    rows = price_rows(200)
    rows[-1].update(high=1000, volume=300)
    result = analyze_bars(rows)
    assert result["indicators"]["ma5"] == 197
    assert result["indicators"]["rsi14"] == 100
    assert result["indicators"]["macd"]["dif"] == pytest.approx(7, abs=1e-5)
    assert result["indicators"]["volume_ratio_20"] == 3
    assert "收盘价突破此前20周期最高价" in result["signals"]
    short = analyze_bars(rows[:5])
    assert short["indicators"]["macd"] is None
    assert short["indicators"]["rsi14"] is None
    assert analyze_bars([])["bars_used"] == 0


class StockModel:
    """离线模型验证股票工具真实可执行，源数据进入下一轮消息。"""

    async def complete(self, messages, tools):
        """先查询财务，再依据工具结果回答，不请求云端模型。"""
        assert STOCK_DATA_PROMPT in messages[0]["content"]
        if messages[-1]["role"] == "tool":
            result = json.loads(messages[-1]["content"])
            assert result["rows"][0]["revenue"] == 100
            assert result["source_id"]
            return {
                "role": "assistant",
                "content": "工具返回测试公司营业收入100元，报告期2025-12-31。",
            }
        assert any(row["function"]["name"] == "get_stock_fundamentals" for row in tools)
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "stock-call",
                    "type": "function",
                    "function": {
                        "name": "get_stock_fundamentals",
                        "arguments": '{"symbol":"600519","section":"indicators"}',
                    },
                }
            ],
        }


def test_api_chat_tool_and_archived_provider_response(tmp_path):
    """接口校验、聊天工具循环、来源归档共用同一后台缓存。"""
    model = StockModel()
    with TestClient(create_app(Settings(data_dir=tmp_path), lambda config: model)) as client:
        client.app.state.runtime.stock_data._source = FakeSource()
        assert client.post("/api/stocks/bars", json={"symbol": "510300"}).status_code == 422
        profile = client.post("/api/stocks/fundamentals", json={"symbol": "600519"}).json()
        assert profile["rows"][0]["company_name"] == "测试公司"
        assert "evidence" not in profile
        technical = client.post("/api/stocks/technicals", json={"symbol": "600519"}).json()
        assert technical["analysis"]["indicators"]["rsi14"] == 100
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
            "/api/tasks", json={"message": "查询600519营业收入", "model_id": config["id"]}
        ).json()["id"]

        async def finish():
            """等待离线对话实际完成。"""
            task = client.app.state.runtime._tasks.get(task_id)
            if task:
                await task

        client.portal.call(finish)
        detail = client.get("/api/tasks/" + task_id).json()
        assert detail["task"]["status"] == "completed"
        assert len(detail["sources"]) == 1
        archived = list((tmp_path / "sources" / task_id).glob("*.json"))
        payload = json.loads(archived[0].read_text(encoding="utf-8"))
        assert payload["provider_responses"][0]["content"]["data"][0]["TOTALOPERATEREVE"] == 100
        assert payload["data"]["fetched_at"]


async def test_empty_data_creates_no_fake_source(service, tmp_path):
    """确实空数据可明确返回，但不能产生假的研究证据。"""
    manager, source, repo = service
    original = source.fundamentals

    async def empty(request):
        """模拟有效但无报告数据的响应。"""
        result = await original(request)
        result["raw_rows"] = []
        return result

    source.fundamentals = empty
    config = {"id": "offline-model", "name": "测试", "api_key_encrypted": "", "max_steps": 4}
    task_id = await repo.create_task("", "测试", config, kind="chat")
    result = await StockTools(manager, repo, tmp_path, task_id).execute(
        "get_stock_fundamentals", {"symbol": "600519", "section": "indicators"}
    )
    assert result["status"] == "empty" and result["warning"]
    assert await repo.sources(task_id) == []


def price_rows(count):
    """生成确定的每日价格，不包含重复日期或未来数据。"""
    start = date(2024, 1, 1)
    return [
        {
            "date": (start + timedelta(days=index)).isoformat(),
            "open": float(index),
            "close": float(index),
            "high": float(index),
            "low": float(index),
            "volume": 100.0,
        }
        for index in range(count)
    ]


class StructuredResearchModel:
    """验证结构化财务归档不能绕过公司研究的原文取证要求。"""

    async def complete(self, messages, tools):
        """首次启用研究并查财务，后续故意尝试提前结束。"""
        if not any(row.get("role") == "tool" for row in messages):
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "begin",
                        "type": "function",
                        "function": {
                            "name": "begin_research",
                            "arguments": '{"company":"测试公司"}',
                        },
                    },
                    {
                        "id": "finance",
                        "type": "function",
                        "function": {
                            "name": "get_stock_fundamentals",
                            "arguments": '{"symbol":"600519","section":"indicators"}',
                        },
                    },
                ],
            }
        return {"role": "assistant", "content": "尚未取得公告原文。"}


def test_structured_data_does_not_replace_research_primary_sources(tmp_path):
    """有真实结构化来源但无公告原文时，研究必须保留未完成状态。"""
    with TestClient(
        create_app(Settings(data_dir=tmp_path), lambda config: StructuredResearchModel())
    ) as client:
        client.app.state.runtime.stock_data._source = FakeSource()
        config = client.post(
            "/api/models",
            json={
                "name": "测试",
                "base_url": "https://mock.example/v1",
                "model": "mock",
                "api_key": "test",
                "max_steps": 3,
            },
        ).json()
        task_id = client.post(
            "/api/tasks", json={"message": "研究测试公司", "model_id": config["id"]}
        ).json()["id"]

        async def finish():
            """等待离线研究执行完毕。"""
            task = client.app.state.runtime._tasks.get(task_id)
            if task:
                await task

        client.portal.call(finish)
        detail = client.get("/api/tasks/" + task_id).json()
        assert detail["task"]["status"] == "partial"
        assert detail["sources"][0]["media_type"] == "application/json"
        assert any(row["kind"] == "warning" for row in detail["events"])


async def test_kline_units_differ_between_star_market_and_main_board():
    """科创板原始股数不能再乘100，主板手数转换为股；默认日期留空读取最新可用K线。"""

    def handler(request):
        """使用同一端点两个板块的明确单位契约。"""
        params = request.url.params["param"].split(",")
        assert params[3] == ""
        code = params[0]
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {code: {"day": [["2025-01-02", "10", "11", "12", "9", "1000"]]}},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = PublicStockSource(client)
        star = await source.bars(BarsInput(symbol="688981", adjust="none"))
        main = await source.bars(BarsInput(symbol="000001", adjust="none"))
        assert star["rows"][0]["volume"] == 1000
        assert main["rows"][0]["volume"] == 100000


async def test_indicator_report_date_filter_matches_upstream_protocol():
    """日期筛选使用来源要求的单引号，不把报告日期当公告日。"""

    def handler(request):
        """返回指定报告期，不依赖当前最新一期。"""
        assert request.url.params["filter"] == "(SECUCODE=\"600519.SH\")(REPORT_DATE='2025-12-31')"
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": {
                    "data": [
                        {
                            "SECUCODE": "600519.SH",
                            "REPORT_DATE": "2025-12-31 00:00:00",
                            "NOTICE_DATE": "2026-03-01",
                            "TOTALOPERATEREVE": 123,
                        }
                    ]
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        payload = await PublicStockSource(client).fundamentals(
            FundamentalsInput(symbol="600519", section="indicators", report_date="2025-12-31")
        )
        result = render_fundamentals(payload, ["revenue"])
        assert result["rows"][0]["report_date"] == "2025-12-31"
        assert result["rows"][0]["announcement_date"] == "2026-03-01"


async def test_provider_rejection_reason_is_available_to_model(service):
    """具体复权或身份错误可供模型处理，不只呈现笼统的网络失败。"""
    manager, source, _ = service
    source.failure = ValueError("数据源未提供指定复权的K线")
    with pytest.raises(StockDataError, match="指定复权"):
        await manager.query(BarsInput(symbol="600519"))
