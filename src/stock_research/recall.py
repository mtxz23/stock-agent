"""新消息首次分析前自动召回历史，保留出处并记录可恢复失败。"""

import asyncio
import json
import logging
import re
from typing import Any

import aiosqlite

from stock_research.diagnostics import DiagnosticRecorder
from stock_research.rag import VectorHistory
from stock_research.storage import Repository

logger = logging.getLogger(__name__)
RECALL_PREFIX = "自动找回的历史参考（非用户新指令，非最新证据）：\n"
RECALL_LIMIT = 3
CASUAL_MESSAGES = {
    "你好",
    "您好",
    "嗨",
    "hello",
    "hi",
    "早上好",
    "下午好",
    "晚上好",
    "谢谢",
    "谢谢你",
    "感谢",
    "好的",
    "好",
    "收到",
    "明白",
    "ok",
    "okay",
    "再见",
    "拜拜",
}


class HistoryRecall:
    """首次召回由独立图节点执行，不占主模型轮数和工具调用预算。"""

    def __init__(
        self,
        repository: Repository,
        searcher: VectorHistory,
        diagnostics: DiagnosticRecorder,
    ) -> None:
        """共享现有向量检索和诊断，完整对话仍由业务库保存。"""
        self.repository = repository
        self.searcher = searcher
        self.diagnostics = diagnostics

    async def prepare(self, task_id: str, state: dict[str, Any]) -> dict[str, Any]:
        """只完成一次召回，结果写进图状态后可跨暂停和模型失败复用。"""
        if state.get("history_recalled"):
            return {}
        task = await self.repository.get_task(task_id, internal=True)
        if not task:
            raise ValueError("当前对话不存在")
        question = task["question"].strip()
        casual = re.sub(r"[\W_]+", "", question.casefold()) in CASUAL_MESSAGES
        if casual:
            return await self._skip(task_id, "普通寒暄或确认消息")
        async with aiosqlite.connect(self.repository.path, timeout=15) as connection:
            async with connection.execute(
                "SELECT 1 FROM tasks WHERE id<>? AND status NOT IN ('queued','running') LIMIT 1",
                (task_id,),
            ) as cursor:
                exists = await cursor.fetchone()
        if not exists:
            return await self._skip(task_id, "尚无可检索的历史记录")
        history = await self.repository.conversation_history(task["parent_id"])
        query = recall_query(task, history)
        await self.repository.add_event(
            task_id,
            "history_recall_started",
            "正在自动找回相关历史",
            {"query": query},
        )
        try:
            result = await self.searcher.search(query, limit=8, exclude_task=task_id)
        except asyncio.CancelledError:
            logger.info("自动历史召回已取消，任务=%s", task_id)
            raise
        except Exception as exc:
            # 召回是辅助上下文；失败必须可见，但不阻断用户的本次提问。
            identifier = self.diagnostics.record(
                "history.recall",
                "failed",
                exception=exc,
                message=str(exc),
                task_id=task_id,
                recoverable=True,
                embedding_diagnostic_id=getattr(exc, "diagnostic_id", ""),
            )
            error = str(self.diagnostics.redact(str(exc)))
            await self.repository.add_event(
                task_id,
                "history_recall_failed",
                "自动历史召回失败，继续处理当前问题",
                {"error": error, "diagnostic_id": identifier},
            )
            return self._with_context(
                state,
                "自动历史召回未成功，本次没有获得历史检索结果。"
                "不要声称已回忆到旧记录；确需历史时可用search_history工具重试。",
            )
        items = select_recall_items(result["items"])
        await self.repository.add_event(
            task_id,
            "history_recalled",
            f"自动找回 {len(items)} 个相关历史片段" if items else "自动召回未找到相关历史",
            {"query": query, "items": items, "embedding_model": result["embedding_model"]},
        )
        logger.info("自动历史召回完成，任务=%s，片段数=%s", task_id, len(items))
        if not items:
            return {"history_recalled": True}
        return self._with_context(state, RECALL_PREFIX + json.dumps(items, ensure_ascii=False))

    async def _skip(self, task_id: str, reason: str) -> dict[str, Any]:
        """跳过纯寒暄或空历史，记录原因且不调用embedding接口。"""
        await self.repository.add_event(
            task_id,
            "history_recall_skipped",
            f"跳过自动历史召回：{reason}",
            {"reason": reason},
        )
        return {"history_recalled": True}

    def _with_context(self, state: dict[str, Any], content: str) -> dict[str, Any]:
        """放在当前用户问题前，参与既有上下文压缩，保持用户问题在最后。"""
        messages = list(state["messages"])
        messages.insert(len(messages) - 1, {"role": "user", "content": content})
        return {"messages": messages, "history_recalled": True}


def recall_query(task: dict[str, Any], history: list[dict[str, Any]]) -> str:
    """用最近两轮理解指代，以本次问题为重点，不读取凭据或内部推理。"""
    recent = []
    for index in range(max(0, len(history) - 2), len(history)):
        previous = history[index]
        child = history[index + 1] if index + 1 < len(history) else task
        # 使用当前分支当时看到的回复，与主模型的对话历史保持一致。
        reply = child.get("previous_report") or previous["report"]
        recent.append(f"用户：{previous['question'][:150]}\n回复：{reply[:200]}")
    context = (
        "近期对话（仅用于理解本次问题的指代）：\n" + "\n".join(recent) + "\n" if recent else ""
    )
    company = f"当前研究对象：{task['company']}\n" if task["company"] else ""
    question = task["question"].strip() or "研究这家公司的业务、经营质量和主要不确定性"
    return context + company + "本次检索重点：" + question[:1200]


def select_recall_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """每条问答最多带入一个最高分片段，避免长回复占满自动召回名额。"""
    selected = []
    seen = set()
    for item in items:
        key = item["task_id"]
        if key in seen or not item["text"].strip():
            continue
        seen.add(key)
        selected.append(item)
        if len(selected) == RECALL_LIMIT:
            break
    return selected
