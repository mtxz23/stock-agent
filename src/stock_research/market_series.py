"""统一跨市场日线、周月聚合和质量标注，保留供应商原值与交易日期。"""

import logging
import math
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from stock_research.market_catalog import Instrument
from stock_research.market_models import BarsInput, market_today

logger = logging.getLogger(__name__)


def prepare_series(
    raw_rows: list[dict[str, Any]], instrument: Instrument, request: BarsInput
) -> dict[str, Any]:
    """校验日期及有限数值后聚合，不修正异常OHLC或把缺失成交量填零。"""
    rows = [_normalize_row(raw, instrument) for raw in raw_rows]
    rows.sort(key=lambda row: row["date"])
    if len({row["date"] for row in rows}) != len(rows):
        raise ValueError("来源日线包含重复交易日期，不能进行指标计算")
    source_end = rows[-1]["date"] if rows else None
    source_start = rows[0]["date"] if rows else None
    # 先截断结束日期再聚合，防止查询历史周/月时泄漏结束日之后的数据。
    if request.end_date:
        rows = [row for row in rows if row["date"] <= request.end_date.isoformat()]
    if request.period != "day":
        rows = _aggregate(rows, request.period)
    if request.start_date:
        rows = [row for row in rows if row["date"] >= request.start_date.isoformat()]
    available = len(rows)
    rows = rows[-request.limit :]
    invalid_dates = [row["date"] for row in rows if row["quality_flags"]]
    warnings = []
    if invalid_dates:
        warnings.append(
            "来源存在OHLC不一致等记录，保留原值和quality_flags；技术分析停用受影响的突破判断"
        )
        logger.warning(
            "市场K线质量异常，品种=%s，返回异常周期数=%s，日期=%s",
            instrument.symbol,
            len(invalid_dates),
            invalid_dates[:5],
        )
    if rows and any(row["volume"] is None for row in rows):
        warnings.append("来源成交量缺失或口径未核实；缺失值为null，不用占位零生成量比")
    if any(row["date"] > market_today().isoformat() for row in rows):
        warnings.append("夜盘数据按来源下一交易日标记，不能称为该日已收盘数据")
    return {
        "rows": rows,
        "warning": "；".join(warnings),
        "quality": {
            "flagged_periods": len(invalid_dates),
            "flagged_dates": invalid_dates,
            "volume_missing": sum(row["volume"] is None for row in rows),
        },
        "coverage": {
            "returned": len(rows),
            "available": available,
            "requested": request.limit,
            "start_date": rows[0]["date"] if rows else None,
            "end_date": rows[-1]["date"] if rows else None,
            "source_start_date": source_start,
            "source_end_date": source_end,
            "short_history": len(rows) < request.limit,
            "truncated": available > request.limit,
        },
    }


def _normalize_row(raw: dict[str, Any], instrument: Instrument) -> dict[str, Any]:
    """股票指数为点位，期货允许负价格；夜盘日期和占位零单独说明。"""
    if not isinstance(raw, dict):
        raise ValueError("来源日线记录格式不支持")
    day = date.fromisoformat(str(raw.get("date")))
    today = market_today()
    night = datetime.now(ZoneInfo("Asia/Shanghai")).hour >= 20
    allowed = (
        today + timedelta(days=3) if night and instrument.provider == "sina_domestic" else today
    )
    if day > allowed:
        raise ValueError("行情包含未来交易日期")
    values = {key: _number(raw.get(key)) for key in ("open", "close", "high", "low")}
    if instrument.security_type != "future" and values["close"] <= 0:
        raise ValueError("指数或现货收盘数值非正")
    flags = []
    if (
        values["low"] > min(values["open"], values["close"])
        or values["high"] < max(values["open"], values["close"])
        or values["low"] > values["high"]
    ):
        flags.append("来源OHLC关系不一致，保留原值")
    if instrument.security_type != "future" and min(values.values()) <= 0:
        flags.append("来源部分OHLC数值非正")
    quantity = _optional_number(raw.get("volume"), nonnegative=True)
    domestic = instrument.provider == "sina_domestic"
    # 海外指数数量可能是成交额，外盘商品的零量常为占位，保留原字段供复核。
    volume = (
        quantity
        if domestic
        or (instrument.security_type == "future" and quantity is not None and quantity > 0)
        else None
    )
    settlement = _optional_number(raw.get("settlement")) if domestic else None
    if settlement == 0:
        settlement = None
    return {
        "date": day.isoformat(),
        **values,
        "volume": volume,
        "source_quantity": quantity,
        "open_interest": _optional_number(raw.get("open_interest"), nonnegative=True)
        if domestic
        else None,
        "settlement": settlement,
        "quality_flags": flags,
    }


def _aggregate(rows: list[dict[str, Any]], period: str) -> list[dict[str, Any]]:
    """以来源交易日期按ISO周或自然月聚合，结算价和持仓取末值。"""
    groups: dict[tuple, list[dict[str, Any]]] = {}
    for row in rows:
        day = date.fromisoformat(row["date"])
        key = tuple(day.isocalendar()[:2]) if period == "week" else (day.year, day.month)
        groups.setdefault(key, []).append(row)
    result = []
    for group in groups.values():
        result.append(
            {
                "date": group[-1]["date"],
                "open": group[0]["open"],
                "close": group[-1]["close"],
                "high": max(row["high"] for row in group),
                "low": min(row["low"] for row in group),
                "volume": _sum_optional(group, "volume"),
                "source_quantity": _sum_optional(group, "source_quantity"),
                "open_interest": group[-1]["open_interest"],
                "settlement": group[-1]["settlement"],
                "quality_flags": list(
                    dict.fromkeys(flag for row in group for flag in row["quality_flags"])
                ),
                "period_start_date": group[0]["date"],
                "trading_days": len(group),
            }
        )
    return result


def _sum_optional(rows: list[dict[str, Any]], key: str) -> float | None:
    """周期中任何一日缺失时总量未知，不把部分合计当作完整成交量。"""
    values = [row[key] for row in rows]
    return sum(values) if all(value is not None for value in values) else None


def _number(value: Any) -> float:
    """必要价格必须存在且有限，非法数字不能流入缓存或指标。"""
    if value is None or isinstance(value, bool):
        raise ValueError("来源缺少有效价格字段")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("市场数据含非有限数值")
    return number


def _optional_number(value: Any, *, nonnegative: bool = False) -> float | None:
    """缺失和占位字符串保留为空，真实负量或非有限值明确拒绝。"""
    if value is None or value in ("", "--", "-"):
        return None
    number = _number(value)
    if nonnegative and number < 0:
        raise ValueError("来源数量字段为负值")
    return number
