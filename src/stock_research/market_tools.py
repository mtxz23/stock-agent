"""让AI按需查询股票数据并归档证据，数据源变更不改变工具契约。"""

import asyncio
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from stock_research.market_catalog import search_instruments
from stock_research.market_data import StockDataService
from stock_research.market_models import BarsInput, FundamentalsInput, InstrumentSearchInput
from stock_research.storage import Repository
from stock_research.technicals import analyze_market_bars

STOCK_TOOL_NAMES = {
    "get_stock_fundamentals",
    "get_stock_bars",
    "get_stock_technicals",
    "search_market_symbols",
    "get_market_bars",
    "get_market_technicals",
}
STOCK_DATA_PROMPT = """
股票数据规则：
公司资料、主营构成和财务报表调用get_stock_fundamentals，基本面只支持内地A股公司；不能将指数或商品当作公司查询财务。股票、境内外指数和商品历史行情调用get_market_bars；均线、MACD、RSI等调用get_market_technicals。get_stock_bars/get_stock_technicals为旧名称兼容入口。
品种名称或代码不确定时先调用search_market_symbols，按query、security_type和分页查看已接入目录。目录不是报价，也不证明任意月份合约已上市。明确市场身份，不把纳斯达克综合IXIC.US与纳斯达克100NDX.US、工业硅SI0.GFEX与COMEX白银SI.COMEX混淆。纳斯达克100来源身份异常，暂未启用，目录unavailable会说明原因，不要替换成综合指数。
内地指数包括上证：000001.SH；深成：399001.SZ；创业板：399006.SZ；沪深300：000300.SH或399300.SZ；上证50：000016.SH；中证500：000905.SH；中证1000：000852.SH；科创50：000688.SH；中小100：399005.SZ；裸000001是平安银行000001.SZ。海外指数如标普500SPX.US、道琼斯DJI.US、纳斯达克综合IXIC.US、恒生HSI.HK、恒生科技HSTECH.HK、日经225N225.JP等，其他品种查目录。
国内商品如多晶硅PS0.GFEX、碳酸锂LC0.GFEX、生猪LH0.DCE、工业硅SI0.GFEX；0代表供应商主力连续行情，具体合约如PS2611.GFEX、LC2611.GFEX、LH2611.DCE。纽约金GC.COMEX、纽约白银SI.COMEX、布伦特BRN.ICE、WTI原油CL.NYMEX、美国天然气NG.NYMEX、伦敦金现货XAU.SPOT；黄金和原油泛称有歧义时先查目录并明确所选品种。
指数、期货和现货仅支持adjust=none；股票默认qfq。指数单位是点；期货报价按品种单位，沪金元/克、沪银元/千克、国内原油元/桶，其余核对fields。生猪期货不是猪肉现货。close与settlement分别是来源收盘/最新日线价与结算价，缺失为null；未提供前结算价时不能声称计算了交易所口径涨跌幅。
查询今天大盘优先用行情工具取上证、深成、创业板日线，limit=5且至少2根；其他已接入指数和商品同样先调用工具。按coverage.end_date、time_zone、fetched_at说明最近可用交易日；美股可能仍是前一个美国交易日，不强行要求北京时间今天。最新日/周/月可能未结束，期货夜盘可能按下一交易日标记。新闻背景、未接入品种或工具失败时可搜索补充，不用网页内容冒充行情工具数据。
contract_kind为continuous/provider_continuous时是供应商连续序列，非固定可交易合约；主力映射和换月调整未核实，跳变不能直接当作技术突破或回测收益。要核实某个月份必须查询该具体合约，不能把连续价冒充指定合约价。周/月行情可能由日线聚合，首尾周期可能不完整，按basis和coverage说明。
按status、warning、quality、fetched_at、coverage说明来源质量、缺失和数据范围；stale是旧缓存，empty是无匹配数据，均不能称为实时。来源数量字段未核实或占位零不能当有效成交量或成交金额；null不填零。quality_flags表示来源OHLC异常，原值保留，停用受影响的突破判断。技术指标只报告工具实际计算的值，不编造复杂形态。
财务section为profile、business、indicators、income、balance、cashflow；fields可选标准字段、原始字段或*。收入、利润和现金流按报告期累计，资产负债表为期末值；report_date不是公告日或历史可知日，当前版本不是历史时点回测数据库。主营占比已统一百分比，其他单位核对fields。结构化JSON是整理值，重大经营结论仍须公告原文；工具不下单、不代表持仓。ETF及海外公司基本面尚未接入。
"""


class StockTools:
    """绑定任务证据归档，查询与诊断由后台数据服务统一提供。"""

    def __init__(
        self, service: StockDataService, repository: Repository, data_dir: Path, task_id: str
    ) -> None:
        """原始返回按任务保存，不将财务和行情写入长期记忆。"""
        self._service = service
        self._repository = repository
        self._archive = data_dir / "sources" / task_id
        self._task_id = task_id

    async def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """校验模型参数，返回实际数据和可下载的原始来源编号。"""
        if name == "search_market_symbols":
            request = InstrumentSearchInput.model_validate(arguments)
            return search_instruments(**request.model_dump())
        if name == "get_stock_fundamentals":
            request = FundamentalsInput.model_validate(arguments)
        elif name in {
            "get_stock_bars",
            "get_stock_technicals",
            "get_market_bars",
            "get_market_technicals",
        }:
            request = BarsInput.model_validate(arguments)
        else:
            raise ValueError("未知股票数据工具")
        result = await self._service.query(request)
        evidence = result.pop("evidence")
        if name in {"get_stock_technicals", "get_market_technicals"}:
            analysis = analyze_market_bars(result)
            result.pop("rows")
            result["analysis"] = analysis
        if result["status"] == "empty":
            return result
        archived = json.dumps(
            {"data": result, "provider_responses": evidence}, ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
        digest = hashlib.sha256(archived).hexdigest()
        archive_name = digest + ".json"
        await asyncio.to_thread(_write_archive, self._archive / archive_name, archived)
        source = await self._repository.save_source(
            self._task_id,
            {
                "url": result["source_urls"][-1],
                "title": result["symbol"] + " " + result["section"] + "（公开结构化数据）",
                "content_hash": digest,
                "archive_name": archive_name,
                "media_type": "application/json",
            },
        )
        result["source_id"] = source["id"]
        result["evidence_kind"] = "公开结构化数据，非公告原文；原始响应抓取时间以fetched_at为准"
        return result


def stock_definitions() -> list[dict[str, Any]]:
    """统一工具名和输入格式，模型不直接依赖供应商的API名称。"""
    rows = [
        (
            "search_market_symbols",
            "按名称、代码或类别查已接入的指数及商品目录；如多晶硅、碳酸锂、生猪、黄金、纳斯达克。返回明确市场代码、单位、时区和连续/具体合约格式，不返回报价；支持分页。",
            InstrumentSearchInput,
        ),
        (
            "get_stock_fundamentals",
            "读取A股公司概况、主营构成、财务指标及收入/资产负债/现金流报表；按报告期累计，保留来源与单位。",
            FundamentalsInput,
        ),
        (
            "get_market_bars",
            "读取股票、境内外指数及国内外商品的日/周/月历史行情；先用search_market_symbols确认身份。指数和商品仅none；期货区分连续与具体合约、收盘与结算，保留来源单位和质量限制；非实时行情。",
            BarsInput,
        ),
        (
            "get_market_technicals",
            "基于股票、境内外指数和商品实际行情计算MA、MACD、RSI及可用量价信号；指数/商品仅none。缺失成交量不算量比，异常OHLC或未核实换月的连续行情停用突破判断。",
            BarsInput,
        ),
    ]
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": schema.model_json_schema(),
            },
        }
        for name, description, schema in rows
    ]


def refresh_stock_prompt(content: str) -> str:
    """替换旧检查点的股票规则，保留其他系统规则和当前时间。"""
    if STOCK_DATA_PROMPT in content:
        return content
    start = content.find("\n股票数据规则：")
    if start >= 0:
        # 旧版规则没有结束标记，以下一段系统规则、空行或时间为边界。
        boundary = re.search(r"\n(?:\n|[^\n]{1,40}规则：|当前时间（UTC）：)", content[start + 1 :])
        end = start + 1 + boundary.start() if boundary else len(content)
        content = content[:start] + content[end:]
    return content + STOCK_DATA_PROMPT


def _write_archive(path: Path, raw: bytes) -> None:
    """保存任务自己的原始证据，相同内容不重复覆盖。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(raw)
