"""提供模型配置、研究任务和事件订阅接口，托管本地前端。"""

import asyncio
import json
import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import date
from time import perf_counter
from typing import Annotated, Any
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from starlette.exceptions import HTTPException as StarletteHTTPException

from stock_research.config import CredentialVault, Settings
from stock_research.diagnostics import DiagnosticRecorder, diagnostic_context, read_diagnostics
from stock_research.embedding_api import router as embedding_router
from stock_research.market_api import market_router
from stock_research.market_api import router as stock_router
from stock_research.memory_api import router as memory_router
from stock_research.model_client import ModelError, ResearchModel, decode_arguments
from stock_research.rag import EmbeddingFactory
from stock_research.research import ResearchRuntime
from stock_research.schemas import (
    ACTIVE_STATUSES,
    ConversationTitleInput,
    MessageInput,
    ModelInput,
)
from stock_research.storage import Repository, public_model
from stock_research.watchlist_api import router as watchlist_router

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    model_factory: Callable[[dict[str, Any]], ResearchModel] | None = None,
    embedding_factory: EmbeddingFactory | None = None,
) -> FastAPI:
    """组装单进程应用，支持通过依赖替换验证研究流程。"""
    settings = settings or Settings()
    # 串行协调新增、停止、恢复和管理操作，避免删除期间新增关联提问。
    conversation_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        """初始化持久化资源，关闭时先保存任务状态再释放连接。"""
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        repository = Repository(settings.data_dir)
        await repository.initialize()
        vault = CredentialVault(settings.data_dir)
        diagnostics = DiagnosticRecorder(settings.log_dir)
        async with httpx.AsyncClient(
            timeout=settings.request_timeout, trust_env=settings.use_env_proxy
        ) as client:
            async with AsyncSqliteSaver.from_conn_string(
                str(settings.data_dir / "checkpoints.sqlite")
            ) as checkpointer:
                await checkpointer.setup()
                runtime = ResearchRuntime(
                    settings,
                    repository,
                    vault,
                    client,
                    checkpointer,
                    model_factory,
                    diagnostics,
                    embedding_factory,
                )
                application.state.repository = repository
                application.state.vault = vault
                application.state.runtime = runtime
                application.state.diagnostics = diagnostics
                logger.info("对话服务启动，数据目录=%s", settings.data_dir)
                logger.info(
                    "模型、向量生成和原文读取的网络模式=%s",
                    "使用环境代理配置" if settings.use_env_proxy else "直连",
                )
                try:
                    await runtime.watchlist.start()
                    await runtime.stock_data.start()
                    yield
                finally:
                    await runtime.shutdown()
                    logger.info("对话服务已关闭")

    application = FastAPI(title="公司研究工作台", lifespan=lifespan)
    application.include_router(memory_router)
    application.include_router(embedding_router)
    application.include_router(watchlist_router)
    application.include_router(stock_router)
    application.include_router(market_router)

    @application.middleware("http")
    async def trace_request(request: Request, call_next):
        """为页面请求生成内部编号，并将异常和后台任务关联到同一上下文。"""
        request_id = uuid4().hex
        request.state.request_id = request_id
        started = perf_counter()
        recorder = application.state.diagnostics
        with diagnostic_context(
            request_id=request_id, api_path=request.url.path, api_method=request.method
        ):
            try:
                response = await call_next(request)
            except asyncio.CancelledError:
                if request.url.path.startswith("/api/"):
                    recorder.record("api.request", "cancelled")
                raise
            except Exception as exc:
                identifier = recorder.record(
                    "api.exception",
                    "failed",
                    exception=exc,
                    message="服务内部错误",
                    http_status=500,
                    duration_ms=round((perf_counter() - started) * 1000, 2),
                )
                response = JSONResponse(
                    status_code=500,
                    content={
                        "detail": "服务内部错误，诊断记录已保存",
                        "diagnostic_id": identifier,
                        "request_id": request_id,
                    },
                )
            else:
                # 错误处理器已记录详细失败；健康检查和诊断查询不产生查询噪声。
                if (
                    request.url.path.startswith("/api/")
                    and request.url.path not in {"/api/health", "/api/diagnostics"}
                    and not getattr(request.state, "diagnostic_id", None)
                ):
                    recorder.record(
                        "api.request",
                        "failed" if response.status_code >= 400 else "succeeded",
                        http_status=response.status_code,
                        duration_ms=round((perf_counter() - started) * 1000, 2),
                    )
            response.headers["X-Request-ID"] = request_id
            return response

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        """返回字段位置与原因，避免将 API Key 等原始输入反射到响应。"""
        errors = [
            {"field": ".".join(map(str, item["loc"])), "message": item["msg"]}
            for item in exc.errors()
        ]
        identifier = application.state.diagnostics.record(
            "api.validation",
            "failed",
            message="接口输入校验失败",
            errors=errors,
            http_status=422,
            recoverable=True,
        )
        request.state.diagnostic_id = identifier
        return JSONResponse(
            status_code=422,
            content={
                "detail": errors,
                "diagnostic_id": identifier,
                "request_id": request.state.request_id,
            },
        )

    @application.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """记录接口拒绝原因，不保存包含凭据的原始请求体。"""
        recorder = application.state.diagnostics
        identifier = recorder.record(
            "api.rejected",
            "failed",
            message=str(exc.detail),
            http_status=exc.status_code,
            recoverable=exc.status_code < 500,
        )
        request.state.diagnostic_id = identifier
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "detail": recorder.redact(exc.detail),
                "diagnostic_id": identifier,
                "request_id": request.state.request_id,
            },
            headers=exc.headers,
        )

    @application.exception_handler(ModelError)
    async def model_error(request: Request, exc: ModelError) -> JSONResponse:
        """统一呈现已经脱敏的模型连接错误。"""
        recorder = application.state.diagnostics
        identifier = exc.diagnostic_id or recorder.record(
            "model.validation",
            "failed",
            message=str(exc),
            exception=exc,
        )
        request.state.diagnostic_id = identifier
        return JSONResponse(
            status_code=502,
            content={
                "detail": recorder.redact(str(exc)),
                "diagnostic_id": identifier,
                "request_id": request.state.request_id,
            },
        )

    @application.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception) -> JSONResponse:
        """记录内部异常堆栈，前端只收到稳定的错误说明。"""
        identifier = application.state.diagnostics.record(
            "api.exception",
            "failed",
            message="接口处理失败",
            exception=exc,
            request_id=request.state.request_id,
            api_path=request.url.path,
        )
        return JSONResponse(
            status_code=500,
            content={
                "detail": "服务内部错误，诊断记录已保存",
                "diagnostic_id": identifier,
                "request_id": request.state.request_id,
            },
        )

    @application.get("/api/health")
    async def health() -> dict[str, str]:
        """返回可用于启动检查的健康状态。"""
        return {"status": "ok"}

    @application.get("/api/diagnostics")
    async def diagnostics(
        limit: int = 20,
        task_id: str | None = None,
        request_id: str | None = None,
        failed_only: bool = True,
        day: Annotated[date | None, Query(alias="date")] = None,
    ) -> list[dict[str, Any]]:
        """返回本机可直接调试的脱敏记录，默认仅显示最近失败。"""
        if not 1 <= limit <= 200:
            raise HTTPException(422, "诊断记录条数应在 1–200 之间")
        return await asyncio.to_thread(
            read_diagnostics,
            settings.log_dir,
            limit=limit,
            failed_only=failed_only,
            task_id=task_id,
            request_id=request_id,
            day=day.isoformat() if day else None,
            legacy_directory=settings.data_dir / "diagnostics",
        )

    @application.get("/api/models")
    async def models() -> list[dict[str, Any]]:
        """读取脱敏模型连接列表。"""
        return await application.state.repository.list_models()

    @application.post("/api/models", status_code=201)
    async def add_model(payload: ModelInput) -> dict[str, Any]:
        """保存基础连接配置，已填写的密钥加密存储。"""
        return await save_model(payload)

    @application.put("/api/models/{identifier}")
    async def edit_model(identifier: str, payload: ModelInput) -> dict[str, Any]:
        """更新连接，密钥留空时保留原有凭据。"""
        existing = await application.state.repository.get_model(identifier)
        if not existing:
            raise HTTPException(404, "模型连接不存在")
        return await save_model(payload, identifier, existing)

    async def save_model(
        payload: ModelInput, identifier: str | None = None, existing: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """将凭据与公开配置分开处理，禁止向调用方回传密钥。"""
        config = payload.model_dump(exclude={"api_key"})
        config["api_key_encrypted"] = (
            application.state.vault.encrypt(payload.api_key.strip())
            if payload.api_key.strip()
            else (existing or {}).get("api_key_encrypted", "")
        )
        identifier = await application.state.repository.save_model(config, identifier)
        logger.info("模型连接已保存，连接=%s", identifier)
        return public_model({"id": identifier, **config})

    @application.post("/api/models/{identifier}/test")
    async def test_model(identifier: str) -> dict[str, str]:
        """检查认证及工具调用往返，避免只验证普通聊天。"""
        config = await application.state.repository.get_model(identifier)
        if not config:
            raise HTTPException(404, "模型连接不存在")
        if not config.get("api_key_encrypted"):
            raise HTTPException(409, "请先在模型连接中填写 API Key 并保存，再测试连接")
        model = application.state.runtime._model_factory(config)
        definition = {
            "type": "function",
            "function": {
                "name": "connection_probe",
                "description": "连接测试工具，返回连接成功标志。",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        }
        messages = [{"role": "user", "content": "请调用 connection_probe 工具检查连接。"}]
        reply = await model.complete(messages, [definition])
        calls = reply.get("tool_calls", [])
        if len(calls) != 1 or calls[0]["function"]["name"] != "connection_probe":
            raise ModelError("模型未返回预期工具调用，请确认该模型支持 function calling")
        try:
            arguments = decode_arguments(calls[0]["function"]["arguments"])
        except ValueError as exc:
            raise ModelError("模型返回的工具参数不是有效 JSON") from exc
        if arguments:
            raise ModelError("连接测试工具的参数应为空对象")
        messages.extend(
            [
                reply,
                {"role": "tool", "tool_call_id": calls[0]["id"], "content": '{"connected":true}'},
            ]
        )
        final = await model.complete(messages, [])
        if not final.get("content") or final.get("tool_calls"):
            raise ModelError("模型未正确处理工具返回结果")
        return {"message": "认证、模型响应与工具调用往返检查通过"}

    @application.get("/api/tasks")
    async def tasks() -> list[dict[str, Any]]:
        """列出最近对话消息及状态。"""
        return await application.state.repository.list_tasks()

    @application.post("/api/tasks", status_code=201)
    async def add_task(payload: MessageInput) -> dict[str, str]:
        """保存一条消息，普通聊天和研究共用后台执行及历史关联。"""
        async with conversation_lock:
            model = await application.state.repository.get_model(payload.model_id)
            if not model:
                raise HTTPException(404, "请先配置有效的模型连接")
            if not model.get("api_key_encrypted"):
                raise HTTPException(409, "该模型尚未填写 API Key，请在模型连接中补充并保存")
            parent = (
                await require_task(payload.parent_id, internal=True) if payload.parent_id else None
            )
            if parent and (
                parent["status"] in ACTIVE_STATUSES
                or payload.parent_id in application.state.runtime._tasks
            ):
                raise HTTPException(409, "上一条回复正在处理，请等待或暂停后再发送")
            identifier = await application.state.repository.create_task(
                "",
                payload.message,
                model,
                payload.parent_id,
                parent["report"] if parent else "",
                kind="chat",
                title=parent["title"] if parent else payload.message.replace("\n", " ")[:80],
            )
            application.state.runtime.submit(identifier)
            return {"id": identifier}

    @application.patch("/api/conversations/{identifier}")
    async def rename_conversation(
        identifier: str, payload: ConversationTitleInput
    ) -> dict[str, str]:
        """修改整段对话的标题，包含既有分支并保留原始消息。"""
        async with conversation_lock:
            await require_task(identifier)
            rows = await application.state.repository.conversation_tasks(identifier)
            await application.state.repository.rename_conversation(
                [row["id"] for row in rows], payload.title
            )
            logger.info("对话标题已修改，提问数=%s", len(rows))
            return {"title": payload.title}

    @application.delete("/api/conversations/{identifier}")
    async def delete_conversation(identifier: str) -> dict[str, str]:
        """删除整段对话及其分支，先取消后台执行再清理持久化内容。"""
        async with conversation_lock:
            await require_task(identifier)
            rows = await application.state.repository.conversation_tasks(identifier)
            await application.state.runtime.delete_conversation([row["id"] for row in rows])
            return {"status": "deleted"}

    @application.get("/api/tasks/{identifier}")
    async def task_detail(identifier: str) -> dict[str, Any]:
        """读取报告、已有进展及证据归档。"""
        task = await require_task(identifier)
        # 读取全部历史页，后续增量通过 SSE 获取。
        events: list[dict[str, Any]] = []
        while batch := await application.state.repository.events(
            identifier, events[-1]["id"] if events else 0
        ):
            events.extend(batch)
        return {
            "task": task,
            "events": events,
            "sources": await application.state.repository.sources(identifier),
        }

    @application.post("/api/tasks/{identifier}/pause")
    async def pause_task(identifier: str) -> dict[str, str]:
        """停止正在处理或排队的回复。"""
        async with conversation_lock:
            task = await require_task(identifier)
            if task["status"] not in ACTIVE_STATUSES:
                raise HTTPException(409, "当前任务已经停止")
            await application.state.runtime.pause(identifier)
            return {"status": "paused"}

    @application.post("/api/tasks/{identifier}/resume")
    async def resume_task(identifier: str) -> dict[str, str]:
        """从持久化检查点继续未完成的任务。"""
        async with conversation_lock:
            task = await require_task(identifier)
            if task["status"] not in {"paused", "interrupted", "failed"}:
                raise HTTPException(409, "当前任务不能继续")
            if identifier in application.state.runtime._tasks:
                raise HTTPException(409, "当前任务仍在停止，请稍后重试")
            await application.state.repository.update_task(identifier, "queued")
            await application.state.repository.add_event(identifier, "queued", "回复等待继续")
            application.state.runtime.submit(identifier, resume=True)
            return {"status": "queued"}

    async def require_task(identifier: str, *, internal: bool = False) -> dict[str, Any]:
        """统一检查任务存在性。"""
        task = await application.state.repository.get_task(identifier, internal=internal)
        if not task:
            raise HTTPException(404, "研究任务不存在")
        return task

    @application.get("/api/tasks/{identifier}/events")
    async def event_stream(identifier: str, request: Request, after: int = 0) -> StreamingResponse:
        """回放并订阅事件；断开订阅不会取消后台研究。"""
        await require_task(identifier)
        try:
            cursor = max(after, int(request.headers.get("last-event-id", "0")), 0)
        except ValueError as exc:
            raise HTTPException(422, "事件编号无效") from exc

        async def stream():
            """持续补读持久化事件，在任务结束后关闭订阅。"""
            nonlocal cursor
            while not await request.is_disconnected():
                batch = await application.state.repository.events(identifier, cursor)
                for event in batch:
                    cursor = event["id"]
                    yield f"id: {cursor}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                task = await application.state.repository.get_task(identifier)
                if not task or (task["status"] not in ACTIVE_STATUSES and not batch):
                    break
                if not batch:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(0.4)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @application.get("/api/sources/{identifier}/download")
    async def download_source(identifier: str) -> FileResponse:
        """以附件方式提供原文，避免归档 HTML 在工作台源中执行。"""
        source = await application.state.repository.get_source(identifier)
        if not source:
            raise HTTPException(404, "来源不存在")
        root = (settings.data_dir / "sources").resolve()
        path = (root / source["task_id"] / source["archive_name"]).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise HTTPException(404, "来源归档文件不存在")
        return FileResponse(
            path,
            media_type=source["media_type"],
            filename=path.name,
            headers={"X-Content-Type-Options": "nosniff"},
        )

    # API 注册完成后再挂载静态页面，确保路由不被根路径覆盖。
    if settings.frontend_dir.is_dir():
        application.mount("/", StaticFiles(directory=settings.frontend_dir, html=True), name="ui")
    return application


app = create_app()
