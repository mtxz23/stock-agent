"""管理历史检索的 embedding 配置，测试连接并重建派生索引。"""

from fastapi import APIRouter, HTTPException, Request

from stock_research.embeddings import EmbeddingConfig, EmbeddingError

router = APIRouter(prefix="/api/embeddings", tags=["历史向量检索"])


@router.get("/config")
async def config(request: Request):
    """读取独立模型配置和已索引的业务记录数量。"""
    return await _invoke(request, "config")


@router.put("/config")
async def save_config(request: Request, payload: EmbeddingConfig):
    """引用已有连接，接口不接收或返回新的明文密钥。"""
    return await _invoke(request, "save_config", payload)


@router.post("/test")
async def test(request: Request):
    """调用已保存的 embedding 模型，验证实际返回的向量。"""
    return await _invoke(request, "test")


@router.post("/reindex")
async def rebuild(request: Request):
    """为历史原文重建当前 embedding 模型的索引。"""
    return await _invoke(request, "rebuild")


async def _invoke(request: Request, method: str, *args):
    """统一转换可恢复错误，取消和未知异常交由应用处理。"""
    try:
        return await getattr(request.app.state.runtime.history_searcher, method)(*args)
    except EmbeddingError as exc:
        # HTTP异常处理器会把失败写入现有每日诊断日志。
        raise HTTPException(status_code=502, detail=str(exc)) from exc
