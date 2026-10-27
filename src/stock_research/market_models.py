"""定义股票基本面及跨市场行情参数，明确证券身份、日期和复权方式。"""

import re
from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator, model_validator

from stock_research.market_catalog import INDEX_NAMES as INDEX_NAMES
from stock_research.market_catalog import AssetType, resolve_instrument

Section = Literal["profile", "business", "indicators", "income", "balance", "cashflow"]
Period = Literal["day", "week", "month"]
Adjustment = Literal["none", "qfq", "hfq"]


class StockInput(BaseModel):
    """只查询明确的内地股票，暂不将指数、ETF和海外证券当作公司。"""

    symbol: str = Field(description="A股代码，如600519.SH、SH600519或600519；公司名请先确认代码")
    refresh: bool = Field(default=False, description="强制更新；默认优先使用有效缓存")

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        """统一证券代码，同时拒绝前缀与代码所属股票市场冲突。"""
        return normalize_symbol(value)


class FundamentalsInput(StockInput):
    """按栏目读取公司资料或报告期财务，不把报告期当公告日。"""

    section: Section = "profile"
    report_date: date | None = Field(
        default=None, description="指定报告期，如2026-06-30；不是历史可知日期"
    )
    limit: int = Field(
        default=4, ge=1, le=24, description="财务报告期数量；主营构成保留所选报告期全部分类"
    )
    fields: list[str] = Field(
        default_factory=list,
        max_length=30,
        description="默认返回常用标准字段；可选原始字段，如CONTRACT_LIAB，*表示整张报表",
    )

    @field_validator("fields")
    @classmethod
    def validate_fields(cls, values: list[str]) -> list[str]:
        """字段只做结果裁剪，不允许注入上游查询条件。"""
        if any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}|\*", value) for value in values):
            raise ValueError("字段名称不合法")
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def validate_report(self):
        """报告日期只能用于按报告期返回的栏目。"""
        if self.report_date and (self.report_date > market_today() or self.section == "profile"):
            raise ValueError("报告日期不能是未来日期，也不能用于公司概况")
        return self


class BarsInput(BaseModel):
    """查询股票、境内外指数及已接入商品行情，基本面独立校验。"""

    symbol: str = Field(
        min_length=1,
        max_length=80,
        description="A股或目录中的指数/商品代码，如000001.SH、SPX.US、GC.COMEX、BRN.ICE、PS0.GFEX、LC0.GFEX、LH0.DCE；具体合约如PS2611.GFEX；先用search_market_symbols确认品种",
    )
    refresh: bool = Field(default=False, description="强制更新；默认优先使用有效缓存")
    period: Period = "day"
    adjust: Adjustment | None = Field(
        default=None, description="股票默认qfq；指数、期货和现货默认且仅支持none，不进行股票复权"
    )
    start_date: date | None = None
    end_date: date | None = None
    limit: int = Field(default=250, ge=1, le=640)

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        """目录中的品种精确匹配，其他代码沿用内地股票规则。"""
        instrument = resolve_instrument(value)
        if instrument:
            return instrument.symbol
        try:
            return normalize_symbol(value)
        except ValueError as exc:
            raise ValueError(
                "未识别或尚未接入该品种，请先调用search_market_symbols确认名称、代码与市场"
            ) from exc

    @property
    def security_type(self) -> Literal["stock", "index", "future", "spot"]:
        """从标准市场身份确定类别，不接受模型混淆公司、指数和商品。"""
        instrument = resolve_instrument(self.symbol)
        return instrument.security_type if instrument else "stock"

    @model_validator(mode="after")
    def validate_dates(self):
        """确定默认复权并拒绝指数及商品复权、反向区间和未来查询日期。"""
        if self.security_type != "stock":
            if self.adjust not in {None, "none"}:
                label = {"index": "指数", "future": "期货", "spot": "现货"}[self.security_type]
                raise ValueError(label + "不进行股票复权，请将adjust设为none")
            self.adjust = "none"
        elif self.adjust is None:
            self.adjust = "qfq"
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("开始日期不能晚于结束日期")
        if any(value and value > market_today() for value in (self.start_date, self.end_date)):
            raise ValueError("行情日期不能晚于今天")
        return self


class InstrumentSearchInput(BaseModel):
    """按名称、代码或类别查找已接入品种，目录不冒充行情。"""

    query: str = Field(
        default="",
        max_length=100,
        description="名称或代码，如多晶硅、黄金、纳斯达克；空值分页查看所有品种",
    )
    security_type: AssetType | None = Field(
        default=None, description="可筛选index指数、future期货、spot现货"
    )
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


def market_today() -> date:
    """股票日期按北京时间判断，部署时不依赖主机本地时区。"""
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def normalize_symbol(value: str) -> str:
    """统一内地股票身份，避免同码指数与股票或不同市场之间误匹配。"""
    value = value.strip().upper()
    match = re.fullmatch(r"(?:(SH|SZ|BJ))?(\d{6})(?:\.(SH|SZ|BJ))?", value)
    if not match or (match[1] and match[3] and match[1] != match[3]):
        raise ValueError("当前只支持内地A股，请使用600519.SH等明确股票代码")
    code = match[2]
    expected = (
        "SH"
        if code.startswith(("600", "601", "603", "605", "688", "689"))
        else "SZ"
        if code.startswith(("000", "001", "002", "003", "300", "301"))
        else "BJ"
        if code.startswith(("43", "83", "87", "92"))
        else ""
    )
    market = match[1] or match[3] or expected
    if not expected or market != expected:
        raise ValueError("股票代码与市场不匹配；本工具暂不支持ETF、指数、港美股或期货")
    return code + "." + market
