"""维护已验证的指数与商品身份，明确交易所、报价单位和连续行情口径。"""

import re
from dataclasses import asdict, dataclass, replace
from typing import Literal

AssetType = Literal["index", "future", "spot"]
INDEX_NAMES = {
    "000001.SH": "上证指数",
    "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",
    "000300.SH": "沪深300",
    "399300.SZ": "沪深300",
    "000016.SH": "上证50",
    "000905.SH": "中证500",
    "000852.SH": "中证1000",
    "000688.SH": "科创50",
    "399005.SZ": "中小100",
}
UNAVAILABLE_INSTRUMENTS = (
    {
        "symbol": "NDX.US",
        "name": "纳斯达克100",
        "aliases": ("NDX", "纳斯达克100指数"),
        "reason": "公开来源附带身份不一致，暂未启用；不能以纳斯达克综合指数替代",
    },
)
# 国内商品仅登记已核实连续日线的品种；单位按品种而非统一套用元/吨。
DOMESTIC_PRODUCTS = {
    "PS": ("多晶硅", "GFEX", "元/吨"),
    "LC": ("碳酸锂", "GFEX", "元/吨"),
    "SI": ("工业硅", "GFEX", "元/吨"),
    "LH": ("生猪", "DCE", "元/吨"),
    "AU": ("沪金", "SHFE", "元/克"),
    "AG": ("沪银", "SHFE", "元/千克"),
    "CU": ("铜", "SHFE", "元/吨"),
    "AL": ("铝", "SHFE", "元/吨"),
    "ZN": ("锌", "SHFE", "元/吨"),
    "NI": ("镍", "SHFE", "元/吨"),
    "AO": ("氧化铝", "SHFE", "元/吨"),
    "RB": ("螺纹钢", "SHFE", "元/吨"),
    "RU": ("天然橡胶", "SHFE", "元/吨"),
    "SC": ("上海原油", "INE", "元/桶"),
    "I": ("铁矿石", "DCE", "元/吨"),
    "J": ("焦炭", "DCE", "元/吨"),
    "JM": ("焦煤", "DCE", "元/吨"),
    "M": ("豆粕", "DCE", "元/吨"),
    "C": ("玉米", "DCE", "元/吨"),
    "P": ("棕榈油", "DCE", "元/吨"),
    "Y": ("豆油", "DCE", "元/吨"),
    "JD": ("鸡蛋", "DCE", "元/500千克"),
    "CF": ("棉花", "CZCE", "元/吨"),
    "SR": ("白糖", "CZCE", "元/吨"),
    "TA": ("PTA", "CZCE", "元/吨"),
    "MA": ("甲醇", "CZCE", "元/吨"),
    "SA": ("纯碱", "CZCE", "元/吨"),
    "FG": ("玻璃", "CZCE", "元/吨"),
    "UR": ("尿素", "CZCE", "元/吨"),
}


@dataclass(frozen=True, slots=True)
class Instrument:
    """绑定规范身份与供应商代码，名称和市场不能互相模糊替代。"""

    symbol: str
    name: str
    security_type: AssetType
    market: str
    provider: str
    vendor: str
    time_zone: str
    price_unit: str
    currency: str | None = None
    contract_kind: str | None = None
    aliases: tuple[str, ...] = ()


# 纳斯达克综合指数与纳斯达克100明确分开；仅适配指数，不扩展为海外公司财务。
GLOBAL_INDICES = (
    ("DJI.US", "道琼斯工业指数", "tencent", "us.DJI", "America/New_York", ("道琼斯", "DJI")),
    ("SPX.US", "标普500", "tencent", "us.INX", "America/New_York", ("SPX", "INX", "标普500指数")),
    (
        "IXIC.US",
        "纳斯达克综合指数",
        "tencent",
        "us.IXIC",
        "America/New_York",
        ("IXIC", "纳斯达克", "纳指"),
    ),
    ("HSI.HK", "恒生指数", "tencent", "hkHSI", "Asia/Hong_Kong", ("HSI", "恒指")),
    ("HSTECH.HK", "恒生科技指数", "tencent", "hkHSTECH", "Asia/Hong_Kong", ("HSTECH", "恒生科技")),
    ("N225.JP", "日经225", "sina_index", "NKY", "Asia/Tokyo", ("N225", "NKY", "日经225指数")),
    ("KOSPI.KR", "韩国综合指数", "sina_index", "KOSPI", "Asia/Seoul", ("KOSPI", "韩国综指")),
    ("FTSE.UK", "英国富时100", "sina_index", "UKX", "Europe/London", ("FTSE", "UKX", "富时100")),
    ("DAX.DE", "德国DAX", "sina_index", "DAX", "Europe/Berlin", ("DAX", "德国DAX指数")),
    ("CAC.FR", "法国CAC40", "sina_index", "CAC", "Europe/Paris", ("CAC", "CAC40")),
    (
        "STOXX50.EU",
        "欧洲斯托克50",
        "sina_index",
        "SX5E",
        "Europe/Berlin",
        ("SX5E", "STOXX50", "欧洲Stoxx50"),
    ),
)
FOREIGN_COMMODITIES = (
    (
        "GC.COMEX",
        "COMEX黄金",
        "GC",
        "美元/金衡盎司",
        "future",
        "America/New_York",
        ("GC", "纽约金", "纽约黄金", "黄金", "黄金期货"),
    ),
    (
        "SI.COMEX",
        "COMEX白银",
        "SI",
        "美元/金衡盎司",
        "future",
        "America/New_York",
        ("SI", "纽约白银"),
    ),
    (
        "CL.NYMEX",
        "WTI原油",
        "CL",
        "美元/桶",
        "future",
        "America/New_York",
        ("CL", "WTI", "纽约原油", "原油"),
    ),
    (
        "BRN.ICE",
        "布伦特原油",
        "OIL",
        "美元/桶",
        "future",
        "Europe/London",
        ("BRN", "OIL", "BRENT", "布伦特", "原油"),
    ),
    (
        "NG.NYMEX",
        "NYMEX天然气",
        "NG",
        "美元/百万英热单位",
        "future",
        "America/New_York",
        ("NG", "美国天然气"),
    ),
    (
        "XAU.SPOT",
        "伦敦金现货",
        "XAU",
        "美元/金衡盎司",
        "spot",
        "Europe/London",
        ("XAU", "伦敦金", "现货黄金", "黄金"),
    ),
)


def resolve_instrument(value: str) -> Instrument | None:
    """精确解析代码或唯一别名，拒绝歧义、注入和错误交易所。"""
    value = value.strip().upper()
    for unavailable in UNAVAILABLE_INSTRUMENTS:
        if value in {unavailable["symbol"], unavailable["name"], *unavailable["aliases"]}:
            raise ValueError(unavailable["name"] + "：" + unavailable["reason"])
    # 保留旧版国内指数的市场前缀写法，仍拒绝前后市场冲突。
    prefixed = re.fullmatch(r"(SH|SZ)(\d{6})(?:\.(SH|SZ))?", value)
    if prefixed:
        if prefixed[3] and prefixed[1] != prefixed[3]:
            raise ValueError("证券市场前缀与后缀不匹配")
        value = prefixed[2] + "." + prefixed[1]
    if value in INSTRUMENTS:
        return INSTRUMENTS[value]
    candidates = [
        item
        for item in INSTRUMENTS.values()
        if value in {item.name.upper(), *(name.upper() for name in item.aliases)}
    ]
    if len(candidates) > 1:
        raise ValueError("品种名称或代码有歧义，请先调用search_market_symbols确认市场和具体品种")
    if candidates:
        return candidates[0]
    match = re.fullmatch(r"([A-Z]{1,3})(0|\d{3,4})(?:\.([A-Z]+))?", value)
    if match and match[1] in DOMESTIC_PRODUCTS:
        product, month, market = match.groups()
        expected = DOMESTIC_PRODUCTS[product][1]
        if market and market != expected:
            raise ValueError("期货品种与交易所不匹配")
        if len(month) == 3 and expected != "CZCE":
            raise ValueError("该交易所合约请使用四位年月，如PS2611.GFEX")
        if month != "0" and not 1 <= int(month[-2:]) <= 12:
            raise ValueError("期货合约月份必须为01至12")
        base = INSTRUMENTS[product + "0." + expected]
        if month == "0":
            return base
        return replace(
            base,
            symbol=product + month + "." + expected,
            vendor=product + month,
            name=DOMESTIC_PRODUCTS[product][0] + month,
            contract_kind="specific",
            aliases=(),
        )
    return None


def search_instruments(
    query: str = "", security_type: AssetType | None = None, limit: int = 20, offset: int = 0
) -> dict:
    """分页检索本地已接入目录，不把目录名称当作实时行情或上市合约证明。"""
    query = query.strip().upper()
    rows = [
        item
        for item in INSTRUMENTS.values()
        if (not security_type or item.security_type == security_type)
        and (
            not query
            or query in " ".join((item.symbol, item.name, item.market, *item.aliases)).upper()
        )
    ]
    items = []
    for item in rows[offset : offset + limit]:
        payload = asdict(item)
        payload.pop("vendor")
        payload.pop("provider")
        payload["supports"] = ["day", "week", "month", "technicals"]
        if item.provider == "sina_domestic":
            product = re.match(r"[A-Z]+", item.vendor)[0]
            payload["contract_pattern"] = product + "YYMM." + item.market
        items.append(payload)
    return {
        "items": items,
        "unavailable": [
            item
            for item in UNAVAILABLE_INSTRUMENTS
            if security_type in {None, "index"}
            and (
                not query
                or query in " ".join((item["symbol"], item["name"], *item["aliases"])).upper()
            )
        ],
        "total": len(rows),
        "offset": offset,
        "has_more": offset + len(items) < len(rows),
        "note": "已接入品种目录；具体月份合约是否有数据，以实际查询结果为准；目录不是报价",
    }


def _build_catalog() -> dict[str, Instrument]:
    """集中生成规范身份，国内商品与海外白银SI不共用市场。"""
    entries = []
    for symbol, name in INDEX_NAMES.items():
        code, market = symbol.split(".")
        entries.append(
            Instrument(
                symbol,
                name,
                "index",
                market,
                "tencent_cn",
                market.lower() + code,
                "Asia/Shanghai",
                "点",
                aliases=(name,),
            )
        )
    for symbol, name, provider, vendor, time_zone, aliases in GLOBAL_INDICES:
        entries.append(
            Instrument(
                symbol,
                name,
                "index",
                symbol.split(".")[1],
                provider,
                vendor,
                time_zone,
                "点",
                aliases=aliases,
            )
        )
    for product, (name, market, unit) in DOMESTIC_PRODUCTS.items():
        aliases = (product, product + "0", name, name + "期货", name + "主力", name + "主力连续")
        if product == "AU":
            aliases += ("黄金", "黄金期货", "上海黄金")
        if product == "SC":
            aliases += ("原油", "国内原油")
        entries.append(
            Instrument(
                product + "0." + market,
                name + "主力连续",
                "future",
                market,
                "sina_domestic",
                product + "0",
                "Asia/Shanghai",
                unit,
                "CNY",
                "continuous",
                aliases,
            )
        )
    for symbol, name, vendor, unit, kind, time_zone, aliases in FOREIGN_COMMODITIES:
        entries.append(
            Instrument(
                symbol,
                name,
                kind,
                symbol.split(".")[1],
                "sina_foreign",
                vendor,
                time_zone,
                unit,
                "USD",
                "provider_continuous" if kind == "future" else "spot",
                aliases,
            )
        )
    return {item.symbol: item for item in entries}


INSTRUMENTS = _build_catalog()
