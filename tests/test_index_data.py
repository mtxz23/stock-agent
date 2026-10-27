"""验证指数行情、公司身份隔离、旧规则替换及完整AI工具调用。"""

import json
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from stock_research.app import create_app
from stock_research.config import Settings
from stock_research.diagnostics import DiagnosticRecorder
from stock_research.market_data import StockDataService
from stock_research.market_models import INDEX_NAMES, BarsInput, FundamentalsInput, market_today
from stock_research.market_sources import PublicStockSource
from stock_research.market_tools import STOCK_DATA_PROMPT, refresh_stock_prompt
from stock_research.storage import Repository

pytestmark = pytest.mark.stock_data


@pytest.mark.parametrize("symbol", list(INDEX_NAMES))
def test_index_identity_and_default_adjustment(symbol):
    """明确的指数代码自动采用不复权，公司工具仍拒绝指数。"""
    request = BarsInput(symbol=symbol)
    assert request.security_type == "index"
    assert request.adjust == "none"
    code, market = symbol.split(".")
    assert BarsInput(symbol=market.lower() + code).symbol == symbol
    with pytest.raises(ValidationError):
        FundamentalsInput(symbol=symbol)
    with pytest.raises(ValidationError, match="指数不进行股票复权"):
        BarsInput(symbol=symbol, adjust="qfq")
    with pytest.raises(ValidationError, match="指数不进行股票复权"):
        BarsInput(symbol=symbol, adjust="hfq")


@pytest.mark.parametrize(
    "symbol", ["399001", "SH399001", "SH000001.SZ", "510300.SH", "UNKNOWN_INDEX"]
)
def test_index_market_is_not_guessed(symbol):
    """指数必须明确市场，冲突代码和未接入品种不能被宽松匹配。"""
    with pytest.raises(ValidationError):
        BarsInput(symbol=symbol)


def test_same_code_remains_a_bank_without_sh_market():
    """000001裸码与SZ明确代码始终是平安银行，不因接入上证而改义。"""
    assert BarsInput(symbol="000001").symbol == "000001.SZ"
    assert BarsInput(symbol="000001").security_type == "stock"
    assert BarsInput(symbol="000001").adjust == "qfq"
    assert FundamentalsInput(symbol="000001").symbol == "000001.SZ"


@pytest.mark.parametrize("suffix", ["", "\n当前时间（UTC）：2026-10-14", "\n\n其他规则：保留规则"])
def test_old_stock_prompt_is_replaced_without_changing_other_rules(suffix):
    """恢复旧对话移除不支持指数的旧规则，并保留其他规则及时间。"""
    old = "对话规则：保留前文\n股票数据规则：\n当前工具只支持内地A股，不支持指数。\n"
    updated = refresh_stock_prompt(old + suffix)
    assert "不支持指数" not in updated
    assert updated.startswith("对话规则：保留前文")
    assert suffix in updated
    assert updated.count("股票数据规则：") == 1
    assert STOCK_DATA_PROMPT in updated
    assert refresh_stock_prompt(updated) == updated


@pytest.mark.parametrize("period", ["day", "week", "month"])
async def test_index_provider_uses_points_and_preserves_raw_volume(period):
    """三种周期均请求不复权，不将指数原始成交量乘100或误写为股价。"""

    def handler(request):
        """模拟来源支持的指数口径，明确检查实际请求参数。"""
        assert request.url.params["param"] == f"sh000001,{period},,,640,"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "sh000001": {
                        period: [["2025-01-02", "3000", "3010", "3020", "2990", "1000"]],
                        "qt": {"sh000001": ["1", "上证指数", "000001"]},
                    }
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await PublicStockSource(client).bars(BarsInput(symbol="000001.SH", period=period))
    assert result["name"] == "上证指数"
    assert result["security_type"] == "index"
    assert result["adjust"] == "none"
    assert result["currency"] is None
    assert result["fields"]["close"]["unit"] == "点"
    assert "未归一化" in result["fields"]["volume"]["unit"]
    assert result["rows"][0]["volume"] == 1000
    assert result["rows"][0]["close"] == 3010


async def test_bank_and_index_cache_are_separate(tmp_path):
    """同码两个市场即使都不复权，也不能交叉复用缓存或价格单位。"""
    calls = []

    def handler(request):
        """证券响应按请求市场返回，模拟相同数值以暴露错误转换。"""
        vendor = request.url.params["param"].split(",")[0]
        calls.append(vendor)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    vendor: {
                        "day": [["2025-01-02", "10", "11", "12", "9", "1000"]],
                    }
                },
            },
        )

    repo = Repository(tmp_path)
    await repo.initialize()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = StockDataService(
            repo.path, DiagnosticRecorder(tmp_path / "logs"), source=PublicStockSource(client)
        )
        await service.start()
        try:
            index = BarsInput(symbol="000001.SH")
            stock = BarsInput(symbol="000001.SZ", adjust="none")
            index_result = await service.query(index)
            stock_result = await service.query(stock)
            assert index_result["rows"][0]["volume"] == 1000
            assert stock_result["rows"][0]["volume"] == 100000
            assert stock_result["fields"]["close"]["unit"] == "元/股"
            assert (await service.query(index))["status"] == "cached"
            assert (await service.query(stock))["status"] == "cached"
            assert calls == ["sh000001", "sz000001"]
        finally:
            await service.close()


class IndexModel:
    """模拟最新问题的三个指数调用，不访问收费模型或网页搜索。"""

    async def complete(self, messages, tools):
        """确认行情工具结果进入模型上下文后再回答。"""
        assert STOCK_DATA_PROMPT in messages[0]["content"]
        results = [json.loads(item["content"]) for item in messages if item.get("role") == "tool"]
        if results:
            assert len(results) == 3
            assert {item["symbol"] for item in results} == {"000001.SH", "399001.SZ", "399006.SZ"}
            assert all(item["security_type"] == "index" and item["source_id"] for item in results)
            return {"role": "assistant", "content": "已用指数行情工具获取三大指数。"}
        assert any(item["function"]["name"] == "get_market_bars" for item in tools)
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "index-" + symbol,
                    "type": "function",
                    "function": {
                        "name": "get_stock_bars",
                        "arguments": json.dumps(
                            {
                                "symbol": symbol,
                                "period": "day",
                                "start_date": (market_today() - timedelta(days=7)).isoformat(),
                                "end_date": market_today().isoformat(),
                            }
                        ),
                    },
                }
                for symbol in ["000001.SH", "399001.SZ", "399006.SZ"]
            ],
        }


def test_latest_market_question_can_use_index_tools_without_search(tmp_path):
    """参数、行情适配、缓存、AI循环和来源归档完整重现三大指数查询。"""

    def handler(request):
        """只允许历史行情请求，失败转搜索无法通过此测试。"""
        vendor = request.url.params["param"].split(",")[0]
        day = market_today()
        rows = [
            [(day - timedelta(days=i)).isoformat(), "3000", "3010", "3020", "2990", "1000"]
            for i in [1, 0]
        ]
        symbol = vendor[2:] + "." + vendor[:2].upper()
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    vendor: {
                        "day": rows,
                        "qt": {vendor: ["1", INDEX_NAMES[symbol], vendor[2:]]},
                    }
                },
            },
        )

    with TestClient(create_app(Settings(data_dir=tmp_path), lambda config: IndexModel())) as client:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client.app.state.runtime.stock_data._source = PublicStockSource(http)
        try:
            assert (
                client.post("/api/stocks/fundamentals", json={"symbol": "000001.SH"}).status_code
                == 422
            )
            technical = client.post("/api/stocks/technicals", json={"symbol": "399006.SZ"})
            assert technical.status_code == 200
            assert technical.json()["analysis"]["bars_used"] == 2
            assert technical.json()["fields"]["close"]["unit"] == "点"
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
                "/api/tasks", json={"message": "今天市场行情怎么样", "model_id": config["id"]}
            ).json()["id"]

            async def finish():
                """等待离线任务实际完成，不只验证函数存在。"""
                task = client.app.state.runtime._tasks.get(task_id)
                if task:
                    await task

            client.portal.call(finish)
            detail = client.get("/api/tasks/" + task_id).json()
            assert detail["task"]["status"] == "completed"
            assert len(detail["sources"]) == 3
            assert len(list((tmp_path / "sources" / task_id).glob("*.json"))) == 3
            assert "search_web" not in json.dumps(detail["events"])
        finally:
            client.portal.call(http.aclose)
