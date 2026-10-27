"""读取海外指数和境内外商品历史行情，不执行JSONP脚本或连接交易接口。"""

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from stock_research.market_catalog import Instrument
from stock_research.market_models import BarsInput
from stock_research.market_series import prepare_series
from stock_research.storage import now

TENCENT_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
SINA_INDEX = "https://gi.finance.sina.com.cn/hq/daily"
SINA_FUTURES = "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_market=/"
Fetch = Callable[[str, dict[str, str]], Awaitable[tuple[bytes, str]]]


class CrossMarketSource:
    """通过统一限流与下载限制读取固定端点，供应商代码由目录确定。"""

    def __init__(self, fetch: Fetch) -> None:
        """复用市场服务独立客户端及统一异常处理，不另建网络资源。"""
        self._fetch = fetch

    async def bars(self, request: BarsInput, instrument: Instrument) -> dict[str, Any]:
        """先获取日线，再按来源交易日期生成周月线及质量说明。"""
        if instrument.provider == "tencent":
            rows, evidence = await self._tencent(instrument)
            upstream_cap = 640
            provider = "腾讯证券公开行情"
        elif instrument.provider == "sina_index":
            rows, evidence = await self._index(instrument)
            upstream_cap = 1000
            provider = "新浪全球指数公开行情"
        else:
            rows, evidence = await self._futures(instrument)
            upstream_cap = None
            provider = "新浪期货公开行情"
        result = prepare_series(rows, instrument, request)
        coverage = result["coverage"]
        coverage["upstream_daily_limit"] = upstream_cap
        coverage["source_daily_rows"] = len(rows)
        coverage["source_limited"] = upstream_cap is not None and len(rows) >= upstream_cap
        coverage["truncated"] |= coverage["source_limited"]
        volume_unit = (
            "手" if instrument.provider == "sina_domestic" else "来源原始成交量单位（未归一化）"
        )
        basis = (
            "历史行情，非实时逐笔报价；date为来源标注的交易日期，不等同于北京时间自然日；"
            "未复权，最新日/周/月可能尚未结束；周月线由日线按ISO周/自然月聚合，首尾周期可能不完整"
        )
        if instrument.contract_kind in {"continuous", "provider_continuous"}:
            basis += (
                "；供应商连续行情，非固定可交易合约，未核实每日主力映射和换月调整；"
                "换月跳变不能直接当作技术突破或回测收益"
            )
        if instrument.security_type == "future":
            basis += (
                "；close为来源收盘/最新日线价，settlement为来源结算价，不能混用；"
                "未提供前结算价时不能声称计算了交易所口径涨跌幅"
            )
        return {
            "symbol": instrument.symbol,
            "name": instrument.name,
            "security_type": instrument.security_type,
            "market": instrument.market,
            "contract_kind": instrument.contract_kind,
            "time_zone": instrument.time_zone,
            "date_basis": "来源交易日期；需结合市场时区和数据日期判断时效",
            "section": "bars",
            "provider": provider,
            "fetched_at": now(),
            "source_urls": [evidence["url"]],
            "currency": instrument.currency,
            "period": request.period,
            "adjust": "none",
            "fields": {
                **{
                    key: {"unit": instrument.price_unit}
                    for key in ("open", "close", "high", "low", "settlement")
                },
                "volume": {"unit": volume_unit, "missing": "null，不能填零或据此推算成交金额"},
                "source_quantity": {"unit": "来源原始数量字段，不能自动认定为成交量或金额"},
                "open_interest": {
                    "unit": "手" if instrument.provider == "sina_domestic" else "未提供"
                },
            },
            **result,
            "basis": basis,
            "evidence": [evidence],
        }

    async def _tencent(self, instrument: Instrument) -> tuple[list[dict], dict]:
        """海外指数使用确切的市场代码，附带行情身份也须匹配。"""
        raw, url = await self._fetch(TENCENT_KLINE, {"param": instrument.vendor + ",day,,,640,"})
        value = _json(raw)
        if not isinstance(value, dict) or value.get("code") != 0:
            raise ValueError("海外指数来源返回失败")
        data = value.get("data", {}).get(instrument.vendor)
        if not isinstance(data, dict) or not isinstance(data.get("day"), list):
            raise ValueError("海外指数没有返回匹配代码的日线")
        quote = data.get("qt", {}).get(instrument.vendor, [])
        expected = instrument.vendor[2:]
        if len(quote) > 2 and quote[2] != expected:
            raise ValueError("海外指数附带行情身份不匹配")
        rows = []
        for item in data["day"]:
            if not isinstance(item, list) or len(item) < 6:
                raise ValueError("海外指数K线字段不完整")
            rows.append(
                dict(zip(("date", "open", "close", "high", "low", "volume"), item[:6], strict=True))
            )
        return rows, {"url": url, "content": value}

    async def _index(self, instrument: Instrument) -> tuple[list[dict], dict]:
        """欧洲与亚洲指数读取固定名称映射，拒绝上游业务失败。"""
        raw, url = await self._fetch(SINA_INDEX, {"symbol": instrument.vendor, "num": "1000"})
        value = _json(raw)
        if (
            not isinstance(value, dict)
            or value.get("code") != 0
            or not isinstance(value.get("result"), dict)
        ):
            raise ValueError("全球指数来源返回失败或未知指数")
        data = value["result"].get("data")
        if not isinstance(data, list):
            raise ValueError("全球指数未返回日线列表")
        return _rename_rows(data, domestic=True), {"url": url, "content": value}

    async def _futures(self, instrument: Instrument) -> tuple[list[dict], dict]:
        """境内商品与外盘分别使用独立服务，JSONP只解析数据不执行脚本。"""
        domestic = instrument.provider == "sina_domestic"
        service = (
            "InnerFuturesNewService.getDailyKLine"
            if domestic
            else "GlobalFuturesService.getGlobalFuturesDailyKLine"
        )
        params = {"symbol": instrument.vendor}
        if not domestic:
            params["source"] = "web"
        raw, url = await self._fetch(SINA_FUTURES + service, params)
        text = raw.decode("utf-8")
        match = re.fullmatch(
            r"\s*(?:/\*.*?\*/\s*)?var\s+_market\s*=\s*\((.*)\)\s*;?\s*", text, re.DOTALL
        )
        if not match:
            raise ValueError("期货来源JSONP包装不支持，未执行任何来源脚本")
        data = _json(match[1].encode("utf-8"))
        if not isinstance(data, list):
            raise ValueError("期货来源未返回日线列表")
        return _rename_rows(data, domestic), {"url": url, "content": data}


def _rename_rows(rows: list, domestic: bool) -> list[dict]:
    """字段改名保持原值；没有成交量、持仓或结算价时保留缺失。"""
    mapping = (
        {
            "date": "d",
            "open": "o",
            "close": "c",
            "high": "h",
            "low": "l",
            "volume": "v",
            "open_interest": "p",
            "settlement": "s",
        }
        if domestic
        else {
            "date": "date",
            "open": "open",
            "close": "close",
            "high": "high",
            "low": "low",
            "volume": "volume",
        }
    )
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("来源日线行格式不支持")
        result.append({key: row.get(native) for key, native in mapping.items()})
    return result


def _json(raw: bytes) -> Any:
    """拒绝JSON中的NaN和Infinity，不用执行脚本的方式解析数据。"""
    return json.loads(raw, parse_constant=_reject_constant)


def _reject_constant(value: str) -> None:
    """非有限数值不能进入市场数据缓存。"""
    raise ValueError("市场来源JSON包含非有限值：" + value)
