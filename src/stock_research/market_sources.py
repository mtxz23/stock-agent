"""只读公开F10和腾讯历史K线，验证证券身份与数据口径，不依赖模型或交易接口。"""

import json
import math
from datetime import date
from decimal import Decimal
from html.parser import HTMLParser
from typing import Any, Protocol

import httpx

from stock_research.market_catalog import resolve_instrument
from stock_research.market_fields import FIELDS
from stock_research.market_global import CrossMarketSource
from stock_research.market_models import INDEX_NAMES, BarsInput, FundamentalsInput, market_today
from stock_research.storage import now

F10 = "https://emweb.securities.eastmoney.com/PC_HSF10/"
DATACENTER = "https://datacenter.eastmoney.com/api/data/v1/get"
TENCENT = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
STATEMENTS = {"income": "lrb", "balance": "zcfzb", "cashflow": "xjllb"}


class StockSource(Protocol):
    """供应商适配层输出统一数据，业务查询和AI工具不依赖上游函数名。"""

    async def fundamentals(self, request: FundamentalsInput) -> dict[str, Any]:
        """返回结构化基本面及原始响应来源。"""
        ...

    async def bars(self, request: BarsInput) -> dict[str, Any]:
        """返回按日期排序、注明复权的历史K线及来源。"""
        ...


class PublicStockSource:
    """使用公开东财F10和腾讯证券接口，不假定它们具有商业服务保障。"""

    def __init__(self, client: httpx.AsyncClient) -> None:
        """绑定独立数据客户端，不使用模型接口凭据。"""
        self._client = client
        self._types: dict[str, str] = {}
        self._cross_market = CrossMarketSource(self._get)

    async def fundamentals(self, request: FundamentalsInput) -> dict[str, Any]:
        """按栏目获取资料、主营构成、指标或完整报告期报表。"""
        symbol = request.symbol
        code, market = symbol.split(".")
        vendor_code = market + code
        evidence = []
        if request.section == "profile":
            value, source = await self._json(
                F10 + "CompanySurvey/CompanySurveyAjax", {"code": vendor_code}
            )
            evidence.append(source)
            if value.get("SecuCode") != symbol or not isinstance(value.get("jbzl"), dict):
                raise ValueError("公司资料没有返回匹配的证券身份")
            rows = [{**value["jbzl"], **value.get("fxxg", {})}]
            name = value.get("SecurityShortName", "")
        elif request.section == "business":
            value, source = await self._json(
                F10 + "BusinessAnalysis/PageAjax", {"code": vendor_code}
            )
            evidence.append(source)
            rows = _rows(value.get("zygcfx"), symbol)
            periods = sorted({str(row["REPORT_DATE"])[:10] for row in rows}, reverse=True)
            wanted = (
                [request.report_date.isoformat()]
                if request.report_date
                else periods[: request.limit]
            )
            rows = [row for row in rows if str(row["REPORT_DATE"])[:10] in wanted]
            name = ""
        elif request.section == "indicators":
            query = {
                "reportName": "RPT_F10_FINANCE_MAINFINADATA",
                "columns": "ALL",
                "filter": '(SECUCODE="'
                + symbol
                + '")'
                + (
                    f"(REPORT_DATE='{request.report_date.isoformat()}')"
                    if request.report_date
                    else ""
                ),
                "pageNumber": "1",
                "pageSize": str(request.limit),
                "sortTypes": "-1",
                "sortColumns": "REPORT_DATE",
            }
            value, source = await self._json(DATACENTER, query)
            evidence.append(source)
            if value.get("success") is not True or not isinstance(value.get("result"), dict):
                raise ValueError("财务指标接口返回失败或未提供匹配数据")
            rows = _rows(value["result"].get("data"), symbol)
            name = rows[0].get("SECURITY_NAME_ABBR", "") if rows else ""
        else:
            # 公司类型由F10页面提供，银行、保险和证券不套用通用报表类型。
            if symbol not in self._types:
                raw, url = await self._get(
                    F10 + "NewFinanceAnalysis/Index", {"type": "web", "code": vendor_code}
                )
                parser = CompanyType()
                parser.feed(raw.decode("utf-8"))
                if parser.value not in {"1", "2", "3", "4"}:
                    raise ValueError("无法确认公司财务报表类型")
                self._types[symbol] = parser.value
                evidence.append({"url": url, "content": raw.decode("utf-8")})
            kind = STATEMENTS[request.section]
            params = {
                "companyType": self._types[symbol],
                "reportDateType": "0",
                "code": vendor_code,
            }
            value, source = await self._json(
                F10 + "NewFinanceAnalysis/" + kind + "DateAjaxNew", params
            )
            evidence.append(source)
            if not isinstance(value.get("data"), list):
                raise ValueError("财务报告期列表格式不支持")
            dates = sorted({str(row["REPORT_DATE"])[:10] for row in value["data"]}, reverse=True)
            dates = (
                [item for item in dates if item == request.report_date.isoformat()]
                if request.report_date
                else dates[: request.limit]
            )
            rows = []
            for start in range(0, len(dates), 5):
                value, source = await self._json(
                    F10 + "NewFinanceAnalysis/" + kind + "AjaxNew",
                    {
                        **params,
                        "reportType": "1",
                        "dates": ",".join(dates[start : start + 5]),
                    },
                )
                evidence.append(source)
                rows.extend(_rows(value.get("data"), symbol))
            name = rows[0].get("SECURITY_NAME_ABBR", "") if rows else ""
        rows.sort(key=lambda row: str(row.get("REPORT_DATE", "")), reverse=True)
        if request.report_date and any(
            str(row.get("REPORT_DATE", ""))[:10] != request.report_date.isoformat() for row in rows
        ):
            raise ValueError("财务接口返回了其他报告期的数据")
        return {
            "symbol": symbol,
            "name": name,
            "section": request.section,
            "provider": "东方财富公开F10",
            "fetched_at": now(),
            "source_urls": [item["url"] for item in evidence],
            "basis": (
                "按报告期累计（资产负债表为期末值），非单季度；来源当前版本，不是历史时点数据库"
            ),
            "currency": "CNY",
            "available_fields": sorted({key for row in rows for key in row}),
            "raw_rows": rows,
            "evidence": evidence,
            "coverage": {
                "report_periods": sorted(
                    {str(row["REPORT_DATE"])[:10] for row in rows if row.get("REPORT_DATE")},
                    reverse=True,
                )
            },
        }

    async def bars(self, request: BarsInput) -> dict[str, Any]:
        """严格保留请求的周期与复权，不静默使用不同口径的返回。"""
        instrument = resolve_instrument(request.symbol)
        if instrument and instrument.provider != "tencent_cn":
            return await self._cross_market.bars(request, instrument)
        code, market = request.symbol.split(".")
        vendor = market.lower() + code
        is_index = request.security_type == "index"
        # 指数成交量保持来源原始单位，不套用股票板块的股/手转换。
        volume_factor = 1 if is_index or vendor.startswith(("sh688", "sh689")) else 100
        price_unit = "点" if is_index else "元/股"
        adjust = "" if request.adjust == "none" else request.adjust
        query = {
            "param": ",".join(
                [
                    vendor,
                    request.period,
                    request.start_date.isoformat() if request.start_date else "",
                    request.end_date.isoformat() if request.end_date else "",
                    "640",
                    adjust,
                ]
            )
        }
        value, source = await self._json(TENCENT, query)
        if value.get("code") != 0 or not isinstance(value.get("data"), dict):
            raise ValueError("历史K线接口返回失败")
        data = value["data"].get(vendor)
        key = adjust + request.period
        if not isinstance(data, dict) or key not in data:
            raise ValueError("数据源未提供该证券指定周期及复权的K线，请明确更换复权方式或数据源")
        raw_rows = data[key]
        if not isinstance(raw_rows, list):
            raise ValueError("K线响应格式不支持")
        rows = []
        for raw in raw_rows:
            if not isinstance(raw, list) or len(raw) < 6:
                raise ValueError("K线字段不完整")
            day = date.fromisoformat(raw[0])
            if day > market_today():
                raise ValueError("K线包含未来日期")
            if (request.start_date and day < request.start_date) or (
                request.end_date and day > request.end_date
            ):
                continue
            values = [float(item) for item in raw[1:6]]
            opening, close, high, low, volume = values
            if (
                not all(math.isfinite(item) for item in values)
                or volume < 0
                or (request.adjust == "none" and min(values[:4]) <= 0)
                or low > min(opening, close)
                or high < max(opening, close)
                or low > high
            ):
                raise ValueError("K线数值或高低价关系异常")
            rows.append(
                {
                    "date": day.isoformat(),
                    "open": opening,
                    "close": close,
                    "high": high,
                    "low": low,
                    "volume": volume * volume_factor,
                }
            )
        rows.sort(key=lambda row: row["date"])
        if len({row["date"] for row in rows}) != len(rows):
            raise ValueError("K线包含重复日期，不能进行指标计算")
        available = len(rows)
        rows = rows[-request.limit :]
        quote = data.get("qt", {}).get(vendor, [])
        if len(quote) > 2 and quote[2] != code:
            raise ValueError("K线附带行情的证券身份不匹配")
        return {
            "symbol": request.symbol,
            "name": quote[1] if len(quote) > 1 else INDEX_NAMES.get(request.symbol, ""),
            "security_type": request.security_type,
            "market": market,
            "time_zone": "Asia/Shanghai",
            "section": "bars",
            "provider": "腾讯证券公开行情",
            "fetched_at": now(),
            "source_urls": [source["url"]],
            "currency": None if is_index else "CNY",
            "period": request.period,
            "adjust": request.adjust,
            "fields": {
                "open": {"unit": price_unit},
                "close": {"unit": price_unit},
                "high": {"unit": price_unit},
                "low": {"unit": price_unit},
                "volume": {"unit": "来源原始成交量单位（未归一化）" if is_index else "股"},
            },
            "rows": rows,
            "evidence": [source],
            "coverage": {
                "returned": len(rows),
                "available": available,
                "requested": request.limit,
                "start_date": rows[0]["date"] if rows else None,
                "end_date": rows[-1]["date"] if rows else None,
                "truncated": available > request.limit or len(raw_rows) >= 640,
                "short_history": len(rows) < request.limit,
            },
            "basis": (
                "历史K线，非实时行情；每次最多获取640根，最近一根可能尚未收盘；"
                + (
                    "指数点位不复权；成交量保留来源原始口径，不能换算为成交金额"
                    if is_index
                    else "复权基准为来源当前口径"
                )
            ),
        }

    async def _json(self, url: str, params: dict[str, str]) -> tuple[dict, dict]:
        """读取有限响应，不执行来源脚本，拒绝非对象JSON。"""
        raw, final = await self._get(url, params)
        value = json.loads(raw, parse_constant=_invalid_number)
        if not isinstance(value, dict):
            raise ValueError("公开股票接口没有返回JSON对象")
        return value, {"url": final, "content": value}

    async def _get(self, url: str, params: dict[str, str]) -> tuple[bytes, str]:
        """限制下载大小和重定向，只使用固定的公开数据地址。"""
        async with self._client.stream(
            "GET",
            url,
            params=params,
            follow_redirects=False,
            headers={"Referer": "https://finance.sina.com.cn/"} if "sina.com.cn/" in url else None,
        ) as response:
            response.raise_for_status()
            chunks = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise ValueError("公开股票接口响应超过4MB限制")
                chunks.append(chunk)
            return b"".join(chunks), str(response.url)


class CompanyType(HTMLParser):
    """只解析F10隐藏的公司类型字段，不执行页面脚本。"""

    def __init__(self) -> None:
        """初始化标准HTML解析器。"""
        super().__init__()
        self.value = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """属性顺序不影响隐藏字段的提取。"""
        values = dict(attrs)
        if tag == "input" and values.get("id") == "hidctype":
            self.value = values.get("value") or ""


def render_fundamentals(data: dict[str, Any], requested: list[str]) -> dict[str, Any]:
    """标准字段保持稳定；扩展字段明确保留来源口径，不替未知单位做推断。"""
    definitions = FIELDS[data["section"]]
    selected = requested or list(definitions)
    native = (
        data["available_fields"]
        if "*" in selected
        else [key for key in selected if key not in definitions]
    )
    unknown = [key for key in native if key not in data["available_fields"]]
    if unknown and data["raw_rows"]:
        raise ValueError("该栏目没有这些字段：" + ",".join(unknown))
    fields = {
        key: {"label": value[1], "unit": value[2], "provider_field": value[0]}
        for key, value in definitions.items()
        if key in selected
    }
    fields.update(
        {
            key: {"label": key, "unit": "来源原始口径未归一化", "provider_field": key}
            for key in native
        }
    )
    rows = []
    for raw in data["raw_rows"]:
        row = {
            "report_date": str(raw["REPORT_DATE"])[:10] if raw.get("REPORT_DATE") else None,
            "announcement_date": str(raw["NOTICE_DATE"])[:10] if raw.get("NOTICE_DATE") else None,
            "provider_updated_at": raw.get("UPDATE_DATE"),
            "currency": raw.get("CURRENCY") or data["currency"],
        }
        missing = []
        for key, field in fields.items():
            value = raw.get(field["provider_field"])
            if value is None or value == "--" or value == "-" or value == "":
                value = None
                missing.append(key)
            if (
                data["section"] == "business"
                and key in {"revenue_share_pct", "gross_margin_pct"}
                and value is not None
            ):
                value = float(Decimal(str(value)) * 100)
            row[key] = value
        row["missing_fields"] = missing
        rows.append(row)
    return {key: value for key, value in data.items() if key not in {"evidence", "raw_rows"}} | {
        "fields": fields,
        "rows": rows,
    }


def _rows(value: Any, symbol: str) -> list[dict[str, Any]]:
    """跨市场同码不能混入结果，缺失身份也不视为成功。"""
    if not isinstance(value, list) or any(
        not isinstance(row, dict) or row.get("SECUCODE") != symbol for row in value
    ):
        raise ValueError("财务响应格式异常或证券身份不匹配")
    for row in value:
        if not isinstance(row.get("REPORT_DATE"), str):
            raise ValueError("财务响应缺少明确的报告期")
        date.fromisoformat(row["REPORT_DATE"][:10])
    return value


def _invalid_number(value: str) -> None:
    """JSON中的NaN和Infinity不作为可用金融数值。"""
    raise ValueError("股票数据包含非有限JSON数值：" + value)
