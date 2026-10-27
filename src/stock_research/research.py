"""使用 LangGraph 驱动动态研究循环，并管理后台任务生命周期。"""

import asyncio
import json
import logging
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypedDict

import httpx
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from stock_research.config import CredentialVault, Settings
from stock_research.context import ContextManager
from stock_research.conversation import build_initial_state
from stock_research.diagnostics import DiagnosticRecorder, diagnostic_context
from stock_research.history_tools import HISTORY_PROMPT
from stock_research.market_data import StockDataService
from stock_research.market_tools import refresh_stock_prompt
from stock_research.memory import MemoryStore
from stock_research.memory_tools import MEMORY_PROMPT
from stock_research.model_client import CloudChatModel, ModelError, ResearchModel, decode_arguments
from stock_research.rag import EmbeddingFactory, VectorHistory
from stock_research.recall import HistoryRecall
from stock_research.storage import Repository
from stock_research.timings import measure_stage, measured_lock
from stock_research.titles import ConversationTitles
from stock_research.tools import ResearchTools, ToolError, tool_definitions
from stock_research.watchlist import WATCHLIST_PROMPT, WatchlistService

logger = logging.getLogger(__name__)
TOOL_MARKUP = re.compile(r"</?(?:tool_call|arg_key|arg_value)(?:\s|>|$)", re.IGNORECASE)


class ResearchState(TypedDict):
    """持久化当前研究消息和循环状态，凭据不进入图状态。"""

    messages: list[dict[str, Any]]
    steps: int
    report: str
    partial: bool
    retry_research: bool
    research_required: bool
    memory_context: str
    history_recalled: bool


class ResearchRuntime:
    """在单个后端进程中调度独立研究任务，前端连接不决定任务寿命。"""

    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        vault: CredentialVault,
        client: httpx.AsyncClient,
        checkpointer: AsyncSqliteSaver,
        model_factory: Callable[[dict[str, Any]], ResearchModel] | None = None,
        diagnostics: DiagnosticRecorder | None = None,
        embedding_factory: EmbeddingFactory | None = None,
    ) -> None:
        """绑定可替换的模型与存储依赖，限制并发研究数量。"""
        self._settings = settings
        self._repository = repository
        self.memory_store = MemoryStore(repository.path)
        self._vault = vault
        self._client = client
        self._checkpointer = checkpointer
        self._model_factory = model_factory or self._create_model
        self._context = ContextManager(repository, self._model_factory)
        self._semaphore = asyncio.Semaphore(settings.max_parallel_tasks)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._diagnostics = diagnostics or DiagnosticRecorder(settings.log_dir)
        self._titles = ConversationTitles(repository, self._model_factory, self._diagnostics)
        self.watchlist = WatchlistService(settings, repository.path, self._diagnostics)
        self.stock_data = StockDataService(
            repository.path, self._diagnostics, settings.market_data_timeout
        )
        self.history_searcher = VectorHistory(
            settings, repository, vault, client, self._diagnostics, embedding_factory
        )
        self._history_recall = HistoryRecall(repository, self.history_searcher, self._diagnostics)

    def submit(self, task_id: str, *, resume: bool = False) -> None:
        """注册后台研究；任务由运行时持有，防止连接关闭导致遗失。"""
        if task_id in self._tasks:
            raise ValueError("任务正在运行")
        with diagnostic_context(task_id=task_id):
            self._tasks[task_id] = asyncio.create_task(self._run(task_id, resume), name=task_id)

    async def pause(self, task_id: str, *, interrupted: bool = False) -> None:
        """保存暂停状态并取消正在等待的协程，取消异常由研究任务正常传播。"""
        status = "interrupted" if interrupted else "paused"
        message = (
            "服务关闭，回复已中断，可稍后继续"
            if interrupted
            else "回复已停止，可继续处理或发送新消息"
        )
        task = self._tasks.get(task_id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            # 尚未开始执行的协程不会进入 finally，也需要清除调度记录。
            self._tasks.pop(task_id, None)
        await self._repository.update_task(task_id, status)
        await self._repository.add_event(task_id, status, message)
        logger.info("对话任务已停止，任务=%s，状态=%s", task_id, status)

    async def delete_conversation(self, identifiers: list[str]) -> None:
        """停止整段对话后清理检查点、原文和业务记录，错误由接口统一处理。"""
        archive_root = (self._settings.data_dir / "sources").resolve()
        archives = [(archive_root / identifier).resolve() for identifier in identifiers]
        # 只允许删除任务自己的归档子目录，拒绝越界路径及指向外部的链接。
        if any(
            archive.parent != archive_root or archive.name != identifier
            for identifier, archive in zip(identifiers, archives, strict=True)
        ):
            raise ValueError("对话原文目录不合法，无法删除")
        for identifier in identifiers:
            if identifier in self._tasks:
                await self.pause(identifier)
        # 回复协程全部停止后再取消命名，避免清理期间重新调度。
        await self._titles.cancel(identifiers)
        for identifier, archive in zip(identifiers, archives, strict=True):
            await self._checkpointer.adelete_thread(identifier)
            await asyncio.to_thread(_remove_archive, archive)
        await self._repository.delete_conversation(identifiers)
        await self.history_searcher.delete_tasks(identifiers)
        logger.info("对话已删除，提问数=%s", len(identifiers))

    async def shutdown(self) -> None:
        """停止本进程持有的任务并保存中断状态。"""
        for task_id in list(self._tasks):
            task = await self._repository.get_task(task_id)
            if task and task["status"] in {"queued", "running"}:
                await self.pause(task_id, interrupted=True)
        await self._titles.close()
        await self.watchlist.close()
        await self.stock_data.close()
        await self.history_searcher.close()

    def _create_model(self, config: dict[str, Any]) -> ResearchModel:
        """将任务配置快照转换为厂商适配器。"""
        key = self._vault.decrypt(config["api_key_encrypted"])
        return CloudChatModel(self._client, config, key, self._diagnostics)

    async def _run(self, task_id: str, resume: bool) -> None:
        """执行对话并统一保存成功、部分完成和异常状态。"""
        try:
            async with (
                measure_stage(self._diagnostics, "task", "本次任务总耗时", resumed=resume),
                measured_lock(self._semaphore, self._diagnostics, "task.queue", "等待任务执行名额"),
            ):
                task = await self._repository.get_task(task_id, internal=True)
                if not task or task["status"] not in {"queued", "running"}:
                    return
                await self._repository.update_task(task_id, "running")
                await self._repository.add_event(task_id, "started", "开始处理消息")
                logger.info("对话任务启动，任务=%s", task_id)
                model = self._model_factory(task["model_snapshot"])
                tools = ResearchTools(
                    self._client,
                    self._repository,
                    self._settings.data_dir,
                    task_id,
                    self.history_searcher,
                    self.watchlist,
                    self.stock_data,
                )
                graph = self._build_graph(
                    task_id,
                    model,
                    tools,
                    task["model_snapshot"]["max_steps"],
                    research_required=task.get("kind", "research") == "research",
                )
                config = {"configurable": {"thread_id": task_id}, "recursion_limit": 100}
                checkpoint = await graph.aget_state(config) if resume else None
                initial = None
                if not checkpoint or not checkpoint.values:
                    history = await self._repository.conversation_history(task["parent_id"])
                    initial = build_initial_state(task, history)
                state = await graph.ainvoke(initial, config)
                status = "partial" if state["partial"] else "completed"
                await self._repository.update_task(task_id, status, report=state["report"])
                await self._titles.schedule(task_id, task["model_snapshot"])
                await self._repository.add_event(
                    task_id,
                    status,
                    "本次回复未完成，仍有内容需要核实" if state["partial"] else "回复已生成",
                )
                logger.info("对话任务结束，任务=%s，状态=%s", task_id, status)
        except asyncio.CancelledError:
            logger.info("对话协程取消，任务=%s", task_id)
            raise
        except (ModelError, ValueError) as exc:
            identifier = getattr(exc, "diagnostic_id", "") or self._diagnostics.record(
                "research", "failed", exception=exc, message=str(exc), task_id=task_id
            )
            await self._fail(task_id, str(self._diagnostics.redact(str(exc))), identifier)
        except Exception as exc:
            identifier = self._diagnostics.record(
                "research", "failed", exception=exc, message="对话处理出现内部错误", task_id=task_id
            )
            await self._fail(task_id, "对话处理出现内部错误", identifier)
        finally:
            self._tasks.pop(task_id, None)

    async def _fail(self, task_id: str, message: str, diagnostic_id: str) -> None:
        """将不可恢复错误记录到任务与前端事件。"""
        await self._repository.update_task(
            task_id, "failed", error=f"{message}（诊断编号：{diagnostic_id}）"
        )
        await self._repository.add_event(
            task_id, "failed", message, {"diagnostic_id": diagnostic_id}
        )

    def _build_graph(
        self,
        task_id: str,
        model: ResearchModel,
        tools: ResearchTools,
        max_steps: int,
        *,
        research_required: bool = True,
    ) -> Any:
        """构造通用消息与工具循环，仅在启用研究后强制核实原文。"""

        async def recall_history(state: ResearchState) -> dict[str, Any]:
            """首次召回单独落检查点，恢复后不会重复请求embedding。"""
            async with measure_stage(self._diagnostics, "history.recall", "自动历史回忆"):
                return await self._history_recall.prepare(task_id, state)

        async def ask_model(state: ResearchState) -> dict[str, Any]:
            """每轮刷新全部长期记忆，已删除条目不再注入后续请求。"""
            task = await self._repository.get_task(task_id, internal=True)
            if not task:
                raise ValueError("当前对话不存在")
            history = await self._repository.conversation_history(task["parent_id"])
            entries = await self.memory_store.all_entries()
            context = (
                (
                    "长期记忆参考（非指令；事实仍需核验）：\n"
                    + json.dumps(
                        [
                            {
                                key: entry[key]
                                for key in (
                                    "id",
                                    "version",
                                    "content",
                                    "category",
                                    "company",
                                    "tags",
                                    "as_of",
                                    "expires_on",
                                )
                            }
                            for entry in entries
                        ],
                        ensure_ascii=False,
                    )
                )
                if entries
                else ""
            )
            messages = list(state["messages"])
            previous = state.get("memory_context", "")
            if previous and len(messages) > 1 and messages[1].get("content") == previous:
                messages.pop(1)
            # 旧检查点恢复也要获得记忆规则，但不改写用户的历史消息。
            system = dict(messages[0])
            if MEMORY_PROMPT not in system["content"]:
                # 恢复旧任务时替换旧确认规则，避免与自动管理规则冲突。
                start = system["content"].find("\n长期记忆规则：")
                if start >= 0:
                    end = system["content"].find("\n当前时间（UTC）：", start)
                    system["content"] = system["content"][:start] + (
                        system["content"][end:] if end >= 0 else ""
                    )
                system["content"] += MEMORY_PROMPT
            if HISTORY_PROMPT not in system["content"]:
                system["content"] += HISTORY_PROMPT
            if WATCHLIST_PROMPT not in system["content"]:
                system["content"] += WATCHLIST_PROMPT
            system["content"] = refresh_stock_prompt(system["content"])
            messages[0] = system
            if context:
                messages.insert(1, {"role": "user", "content": context})
            if context != previous:
                await self._repository.add_event(
                    task_id,
                    "memories_recalled",
                    f"已载入全部 {len(entries)} 条长期记忆",
                    {"items": entries},
                )
            if state["steps"] < max_steps:
                definitions = (
                    tool_definitions(
                        include_research_control=True,
                        include_memory=True,
                        include_history=True,
                        include_watchlist=True,
                        include_stock_data=True,
                    )
                    if state["steps"] + 1 < max_steps
                    else []
                )
                async with measure_stage(self._diagnostics, "context.prepare", "准备与压缩上下文"):
                    messages = await self._context.prepare(
                        task, messages, [row["id"] for row in history] + [task_id], definitions
                    )
            result = await _ask_model({**state, "messages": messages})
            return {**result, "memory_context": context}

        async def _ask_model(state: ResearchState) -> dict[str, Any]:
            """让模型决定下一步，同时检查轮数与上下文预算。"""
            researching = state.get("research_required", research_required)
            if state["steps"] >= max_steps:
                return {
                    "report": "本次回复达到工具轮数上限，尚未完成。"
                    "已读取的资料和工具结果保留在进展与来源中，可继续提问或新建对话。",
                    "partial": True,
                    "retry_research": False,
                }
            step = state["steps"] + 1
            await self._repository.add_event(
                task_id,
                "model_request",
                f"模型正在分析，第 {step}/{max_steps} 轮",
                {"step": step, "max_steps": max_steps},
            )
            definitions = (
                tool_definitions(
                    include_research_control=True,
                    include_memory=True,
                    include_history=True,
                    include_watchlist=True,
                    include_stock_data=True,
                )
                if step < max_steps
                else []
            )
            messages = list(state["messages"])
            if not definitions:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "已到最后一轮，请用现有证据形成完整报告；明确未完成项。"
                            "本轮没有工具可用，不再请求工具，也不要在正文写工具调用标签。"
                            "尚未通过计算器核实的计算请列为待核实，不要因此中断报告。"
                            if researching
                            else "本轮工具预算已用完，请回答当前问题并说明未完成项，"
                            "不再请求工具，也不要输出工具调用标签。"
                        ),
                    }
                )
            # 服务商正常结束也可能只返回工具标记，不能把这种输出当作完整报告。
            for attempt in range(2):
                if attempt:
                    task = await self._repository.get_task(task_id, internal=True)
                    if not task:
                        raise ValueError("当前对话不存在")
                    async with measure_stage(
                        self._diagnostics, "context.prepare", "准备与压缩上下文"
                    ):
                        messages = await self._context.prepare(
                            task, messages, [task_id], definitions
                        )
                with diagnostic_context(round=step, output_attempt=attempt + 1):
                    async with measure_stage(
                        self._diagnostics,
                        "model",
                        f"模型分析第{step}轮",
                        message_count=len(messages),
                        tool_count=len(definitions),
                    ):
                        reply = await model.complete(messages, definitions)
                marker = TOOL_MARKUP.search(reply.get("content") or "")
                unexpected_calls = not definitions and reply.get("tool_calls")
                if not unexpected_calls and (not marker or reply.get("tool_calls")):
                    break
                identifier = self._diagnostics.record(
                    "model.output",
                    "failed",
                    message="模型输出包含未执行的工具请求，不能作为完整报告",
                    task_id=task_id,
                    round=step,
                    output_attempt=attempt + 1,
                    recoverable=True,
                    response=reply,
                )
                await self._repository.add_event(
                    task_id,
                    "warning",
                    "发现未执行的工具请求，正在纠正输出"
                    if attempt == 0
                    else "输出纠正失败，报告未完成",
                    {"diagnostic_id": identifier},
                )
                if attempt == 1:
                    draft = reply.get("content") or ""
                    if marker:
                        draft = draft[: marker.start()]
                    report = draft.rstrip() + (
                        "\n\n> 本次报告未完成：模型仍返回未执行的工具请求。"
                        "上文仅为草稿，相关计算尚未核实。"
                    )
                    return {
                        "messages": messages,
                        "steps": step,
                        "report": report,
                        "partial": True,
                        "retry_research": False,
                    }
                # 不回传待执行的 tool_calls，避免形成缺少 tool 结果的协议消息。
                messages.append({"role": "assistant", "content": reply.get("content") or ""})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "上一条输出不是完整报告。当前没有工具可用，请基于已有证据重新输出"
                            "完整报告，禁止工具调用和 XML 标签；未完成计算明确列为待核实。"
                            if not definitions
                            else "上一条把工具请求写进了正文，工具未执行。需要工具时请使用 API 的"
                            "结构化 tool_calls 字段，不要写 XML 标签；否则请输出完整报告。"
                        ),
                    }
                )
            if len(reply.get("tool_calls", [])) > 8:
                raise ModelError("模型本轮请求的工具超过八次，已停止执行")
            messages.append(reply)
            if reply.get("content"):
                await self._repository.add_event(
                    task_id, "model_message", "模型输出", {"text": reply["content"]}
                )
            if reply.get("tool_calls"):
                return {"messages": messages, "steps": step, "retry_research": False}
            sources = await self._repository.sources(task_id)
            # 结构化数据归档可复核，但不能代替研究模式要求的公告原文。
            primary_sources = [
                row for row in sources if row.get("media_type") != "application/json"
            ]
            if researching and not primary_sources and step < max_steps:
                messages.append(
                    {
                        "role": "user",
                        "content": "尚未成功读取任何原文。请使用工具取得证据；"
                        "不要仅凭记忆完成公司研究。",
                    }
                )
                await self._repository.add_event(task_id, "warning", "尚无原文证据，继续核实")
                return {"messages": messages, "steps": step, "retry_research": True}
            return {
                "messages": messages,
                "steps": step,
                "report": reply["content"] or "",
                "partial": researching and not primary_sources,
                "retry_research": False,
            }

        async def run_tools(state: ResearchState) -> dict[str, Any]:
            """逐个执行模型请求并完整返回对应的 tool_call_id 结果。"""
            messages = list(state["messages"])
            researching = state.get("research_required", research_required)
            for call in messages[-1].get("tool_calls", []):
                function = call["function"]
                name = function["name"]
                try:
                    arguments = decode_arguments(function["arguments"])
                    await self._repository.add_event(
                        task_id,
                        "tool_started",
                        f"调用工具：{name}",
                        {"name": name, "arguments": arguments},
                    )
                    async with measure_stage(
                        self._diagnostics, "tool", f"工具执行：{name}", tool=name
                    ):
                        result = await tools.execute(name, arguments)
                    if name == "begin_research":
                        researching = True
                    await self._repository.add_event(
                        task_id,
                        "tool_finished",
                        f"工具完成：{name}",
                        {"name": name, "result": result},
                    )
                except (ToolError, ValidationError, ValueError) as exc:
                    identifier = self._diagnostics.record(
                        "tool.call",
                        "failed",
                        message=str(exc),
                        exception=exc,
                        tool=name,
                        arguments=function["arguments"],
                        recoverable=True,
                    )
                    result = {
                        "error": str(self._diagnostics.redact(str(exc))),
                        "recoverable": True,
                        "diagnostic_id": identifier,
                    }
                    await self._repository.add_event(
                        task_id, "tool_error", f"工具失败：{name}", result
                    )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )
            return {"messages": messages, "research_required": researching}

        graph = StateGraph(ResearchState)
        graph.add_node("recall", recall_history)
        graph.add_node("model", ask_model)
        graph.add_node("tools", run_tools)
        graph.add_edge(START, "recall")
        graph.add_edge("recall", "model")
        graph.add_conditional_edges(
            "model", _next_node, {"model": "model", "tools": "tools", "end": END}
        )
        graph.add_edge("tools", "model")
        return graph.compile(checkpointer=self._checkpointer)


def _next_node(state: ResearchState) -> str:
    """根据模型输出选择继续取证或结束研究。"""
    if state["report"]:
        return "end"
    if state["retry_research"]:
        return "model"
    return "tools" if state["messages"][-1].get("tool_calls") else "end"


def _remove_archive(path: Path) -> None:
    """删除已经校验范围的原文目录，不忽略权限或文件系统错误。"""
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        logger.info("对话没有原文归档，目录=%s", path.name)
