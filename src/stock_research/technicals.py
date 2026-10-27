"""从已取得的K线计算可复核技术指标，不使用模型推测数值或下单。"""

from statistics import fmean
from typing import Any


def analyze_bars(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """计算最新均线、MACD、Wilder RSI和量价信号，短历史明确标注。"""
    if not rows:
        return {
            "as_of": None,
            "bars_used": 0,
            "indicators": {},
            "signals": [],
            "warning": "没有可计算的K线",
        }
    closes = [float(row["close"]) for row in rows]
    averages = {
        f"ma{period}": fmean(closes[-period:]) if len(closes) >= period else None
        for period in (5, 10, 20, 60)
    }
    macd = None
    signals = []
    if len(rows) >= 35:
        short, long = _ema(closes, 12), _ema(closes, 26)
        dif = [a - b for a, b in zip(short, long, strict=True)]
        dea = _ema(dif, 9)
        macd = {"dif": dif[-1], "dea": dea[-1], "histogram": 2 * (dif[-1] - dea[-1])}
        if dif[-2] <= dea[-2] and dif[-1] > dea[-1]:
            signals.append("MACD本周期金叉")
        elif dif[-2] >= dea[-2] and dif[-1] < dea[-1]:
            signals.append("MACD本周期死叉")
    volume_ratio = None
    if len(rows) >= 21:
        previous = rows[-21:-1]
        if all(row.get("volume") is not None for row in rows[-21:]):
            average_volume = fmean(float(row["volume"]) for row in previous)
            volume_ratio = (
                float(rows[-1]["volume"]) / average_volume if average_volume > 0 else None
            )
        valid_range = not any(row.get("quality_flags") for row in rows[-21:])
        if valid_range and closes[-1] > max(float(row["high"]) for row in previous):
            signals.append("收盘价突破此前20周期最高价")
        if valid_range and closes[-1] < min(float(row["low"]) for row in previous):
            signals.append("收盘价跌破此前20周期最低价")
        if volume_ratio is not None and volume_ratio >= 2:
            signals.append("成交量达到此前20周期均量的2倍或以上")
    return {
        "as_of": rows[-1]["date"],
        "bars_used": len(rows),
        "parameters": {
            "ma": [5, 10, 20, 60],
            "macd": [12, 26, 9],
            "rsi": 14,
            "volume_ratio": "本周期成交量/此前20周期均量",
        },
        "indicators": {
            **averages,
            "macd": macd,
            "rsi14": _rsi(closes, 14),
            "volume_ratio_20": volume_ratio,
        },
        "signals": signals,
        "warning": (
            "EMA以窗口首个收盘价初始化，历史不足130周期时MACD可能受初始化影响；"
            "最近周期可能尚未完成；信号不是买卖指令"
            + (
                "；成交量缺失，未计算相关量比"
                if any(row.get("volume") is None for row in rows[-21:])
                else ""
            )
            + (
                "；来源存在OHLC异常，受影响的突破信号停用，价格指标仅按来源收盘值计算"
                if any(row.get("quality_flags") for row in rows)
                else ""
            )
        ),
    }


def analyze_market_bars(data: dict[str, Any]) -> dict[str, Any]:
    """按同一价格序列计算，并把连续换月限制和价格单位带入分析。"""
    result = analyze_bars(data["rows"])
    result["price_unit"] = data.get("fields", {}).get("close", {}).get("unit")
    if data.get("contract_kind") in {"continuous", "provider_continuous"}:
        result["signals"] = [
            signal for signal in result["signals"] if "突破" not in signal and "跌破" not in signal
        ]
        result["warning"] += (
            "；连续行情的换月映射与调整未核实，停用突破判断；价格指标可受换月跳变影响"
        )
    return result


def _ema(values: list[float], period: int) -> list[float]:
    """按2/(周期+1)平滑，显式使用第一个数作为初值。"""
    result = [values[0]]
    weight = 2 / (period + 1)
    for value in values[1:]:
        result.append(weight * value + (1 - weight) * result[-1])
    return result


def _rsi(closes: list[float], period: int) -> float | None:
    """先取首个窗口平均涨跌幅，再用Wilder平滑；全平盘定义为50。"""
    if len(closes) <= period:
        return None
    changes = [b - a for a, b in zip(closes[:-1], closes[1:], strict=True)]
    gain = fmean(max(value, 0) for value in changes[:period])
    loss = fmean(max(-value, 0) for value in changes[:period])
    for value in changes[period:]:
        gain = (gain * (period - 1) + max(value, 0)) / period
        loss = (loss * (period - 1) + max(-value, 0)) / period
    if not loss:
        return 100.0 if gain else 50.0
    return 100 - 100 / (1 + gain / loss)
