"""提供只读自选列表接口，数据由后台服务自动同步。"""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


@router.get("")
async def get_watchlist(
    request: Request,
    group: Annotated[str, Query(max_length=200)] = "",
    q: Annotated[str, Query(max_length=200)] = "",
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
):
    """查询自选快照，错误分组明确返回校验失败。"""
    try:
        return await request.app.state.runtime.watchlist.query(group, q, offset, limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
