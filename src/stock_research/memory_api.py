"""长期记忆管理接口，复用应用的统一错误诊断。"""

from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request

from stock_research.memory import MemoryConflict, MemoryNotFound, MemoryStore
from stock_research.memory_schemas import (
    MemoryCategory,
    MemoryInput,
    MemoryUpdate,
)

router = APIRouter(prefix="/api/memories", tags=["memories"])


@router.get("")
async def list_memories(
    request: Request,
    q: str = "",
    category: MemoryCategory | Literal[""] = "",
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
):
    """分页查看记忆。"""
    return await _store(request).list(q, category, offset, limit)


@router.post("", status_code=201)
async def create_memory(request: Request, payload: MemoryInput):
    """用户直接添加一条记忆。"""
    return await _store(request).create(payload)


@router.put("/{identifier}")
async def update_memory(request: Request, identifier: str, payload: MemoryUpdate):
    """编辑记忆并检查版本，原始出处保持可追溯。"""
    try:
        return await _store(request).update(
            identifier, MemoryInput(**payload.model_dump(exclude={"version"})), payload.version
        )
    except (MemoryNotFound, MemoryConflict) as exc:
        raise _http_error(exc) from exc


@router.delete("/{identifier}")
async def delete_memory(
    request: Request,
    identifier: str,
    version: Annotated[int, Query(ge=1)],
):
    """删除独立记忆，不改写历史对话中的使用记录。"""
    try:
        await _store(request).delete(identifier, version)
        return {"deleted": True}
    except (MemoryNotFound, MemoryConflict) as exc:
        raise _http_error(exc) from exc


def _store(request: Request) -> MemoryStore:
    """读取应用启动时创建的共享存储。"""
    return request.app.state.runtime.memory_store


def _http_error(exc: MemoryNotFound | MemoryConflict) -> HTTPException:
    """将缺失与版本冲突映射到统一接口错误处理。"""
    return HTTPException(
        status_code=404 if isinstance(exc, MemoryNotFound) else 409, detail=str(exc)
    )
