"""提供只读股票查询接口，用于研究调用及排查，不增加独立展示页面。"""

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from stock_research.market_catalog import AssetType, search_instruments
from stock_research.market_data import StockDataError
from stock_research.market_models import BarsInput, FundamentalsInput
from stock_research.technicals import analyze_market_bars

router = APIRouter(prefix="/api/stocks", tags=["stocks"])
market_router = APIRouter(prefix="/api/market", tags=["market"])


@router.post("/fundamentals")
async def fundamentals(payload: FundamentalsInput, request: Request) -> dict[str, Any]:
    """读取基本面栏目，参数由Pydantic统一校验。"""
    return await _query(request, payload)


@market_router.post("/bars")
@router.post("/bars")
async def bars(payload: BarsInput, request: Request) -> dict[str, Any]:
    """查询历史K线，不进行证券交易。"""
    return await _query(request, payload)


@market_router.post("/technicals")
@router.post("/technicals")
async def technicals(payload: BarsInput, request: Request) -> dict[str, Any]:
    """基于同一缓存的真实K线计算技术指标。"""
    result = await _query(request, payload)
    result["analysis"] = analyze_market_bars(result)
    result.pop("rows")
    return result


@market_router.get("/instruments")
async def instruments(
    query: str = Query(default="", max_length=100),
    security_type: AssetType | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """检索已接入目录，不联网获取报价或把目录当作行情来源。"""
    return search_instruments(query, security_type, limit, offset)


async def _query(request: Request, payload: FundamentalsInput | BarsInput) -> dict[str, Any]:
    """错误保持统一状态码，原始响应保留在缓存及任务归档中。"""
    try:
        result = await request.app.state.runtime.stock_data.query(payload)
    except StockDataError as exc:
        raise HTTPException(502, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    result.pop("evidence")
    return result
