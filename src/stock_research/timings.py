"""记录任务和内部阶段耗时，提供按任务查看的终端入口，不写入页面事件。"""

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from time import perf_counter
from typing import Any
from uuid import uuid4

from stock_research.config import Settings
from stock_research.diagnostics import DiagnosticRecorder, diagnostic_context, read_diagnostics

logger = logging.getLogger(__name__)
ACTIVE_SPAN: ContextVar[str | None] = ContextVar("active_timing_span", default=None)


@asynccontextmanager
async def measure_stage(
    recorder: DiagnosticRecorder, stage: str, label: str, **fields: Any
) -> AsyncIterator[dict[str, Any]]:
    """用单调时钟计时；嵌套阶段和并发任务隔离，异常及取消正常传播。"""
    span_id = uuid4().hex
    metadata = {"span_id": span_id, "parent_span_id": ACTIVE_SPAN.get(), "label": label, **fields}
    started = perf_counter()
    recorder.record("timing." + stage, "started", **metadata)
    token = ACTIVE_SPAN.set(span_id)
    details: dict[str, Any] = {}
    outcome = "succeeded"
    try:
        with diagnostic_context(timing_span_id=span_id):
            yield details
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    except Exception as exc:
        outcome = "failed"
        details["exception_type"] = type(exc).__name__
        raise
    finally:
        ACTIVE_SPAN.reset(token)
        duration = round((perf_counter() - started) * 1000, 2)
        # 耗时与业务错误分开，避免“最近失败”被父阶段计时覆盖。
        recorder.record(
            "timing." + stage,
            "finished",
            status=outcome,
            duration_ms=duration,
            **metadata,
            **details,
        )
        logger.info(
            "阶段耗时，阶段=%s，状态=%s，耗时=%.2f毫秒，编号=%s", label, outcome, duration, span_id
        )


@asynccontextmanager
async def measured_lock(
    lock: asyncio.Lock | asyncio.Semaphore,
    recorder: DiagnosticRecorder,
    stage: str,
    label: str,
) -> AsyncIterator[None]:
    """单独计算排队等待，不把持有锁期间的工作计入等待时长。"""
    async with measure_stage(recorder, stage, label):
        await lock.acquire()
    try:
        yield
    finally:
        lock.release()


class RequestClock:
    """只记录 HTTP 传输阶段时间，不收集请求头、密钥、正文或推理内容。"""

    def __init__(self) -> None:
        """相对本次请求记录阶段标记，复用连接时缺失的握手时间不伪造为零。"""
        self.started = perf_counter()
        self.values: dict[str, float | str] = {}
        self._phase_starts: dict[str, float] = {}

    async def trace(self, name: str, info: dict[str, Any]) -> None:
        """兼容 HTTP/1、HTTP/2 和代理 CONNECT，最终响应以最后一次发送为准。"""
        elapsed = round((perf_counter() - self.started) * 1000, 2)
        phase, outcome = name.rsplit(".", 1)
        if outcome == "started":
            self._phase_starts[phase] = elapsed
        if outcome == "complete":
            if phase.endswith(("connect_tcp", "start_tls")):
                key = "connect_ms" if phase.endswith("connect_tcp") else "tls_ms"
                self.values[key] = round(elapsed - self._phase_starts.get(phase, elapsed), 2)
            elif phase.endswith("send_request_body"):
                self.values["request_sent_ms"] = elapsed
                # CONNECT 响应不能误当作最终 API 响应。
                for key in ("headers_received_ms", "body_received_ms", "response_wait_ms"):
                    self.values.pop(key, None)
            elif phase.endswith("receive_response_headers"):
                self.values["headers_received_ms"] = elapsed
                self.values["response_wait_ms"] = round(
                    elapsed - float(self.values.get("request_sent_ms", elapsed)), 2
                )
            elif phase.endswith("receive_response_body"):
                self.values["body_received_ms"] = elapsed
                self.values["body_read_ms"] = round(
                    elapsed - float(self.values.get("headers_received_ms", elapsed)), 2
                )
        elif outcome == "failed":
            self.values["failed_phase"] = phase
            if phase.endswith("receive_response_headers"):
                self.values["response_wait_ms"] = round(
                    elapsed - float(self.values.get("request_sent_ms", elapsed)), 2
                )


def main() -> None:
    """查看指定任务或最近有计时记录的任务，子阶段缩进展示而不重复相加。"""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="查看任务各阶段耗时")
    parser.add_argument("--task-id")
    parser.add_argument("--date")
    parser.add_argument("--json", action="store_true", help="输出结构化计时记录")
    args = parser.parse_args()
    records = read_diagnostics(
        Settings().log_dir, limit=200, failed_only=False, task_id=args.task_id, day=args.date
    )
    task_id = args.task_id or next(
        (row.get("task_id") for row in records if row["stage"].startswith("timing.")), None
    )
    rows = (
        list(
            reversed(
                [
                    row
                    for row in records
                    if row.get("task_id") == task_id and row["stage"].startswith("timing.")
                ]
            )
        )
        if task_id
        else []
    )
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    if not rows:
        print("未找到分阶段计时记录；机制启用后的新请求才有完整耗时分解。")
        return
    print(f"任务：{task_id}（父阶段包含子阶段耗时，不重复相加）")
    spans = {row["span_id"]: row for row in rows if row["outcome"] == "started"}
    completed = {row["span_id"]: row for row in rows if row["outcome"] != "started"}
    exchanges: dict[str, list[dict[str, Any]]] = {}
    for record in reversed(records):
        if record.get("task_id") == task_id and record.get("timing_span_id"):
            if record["stage"] in {"model.call", "embedding.request"}:
                exchanges.setdefault(record["timing_span_id"], []).append(
                    {
                        key: record[key]
                        for key in (
                            "stage",
                            "outcome",
                            "duration_ms",
                            "network_timings",
                            "usage",
                            "response_summary",
                        )
                        if key in record
                    }
                )
    for span_id, start in spans.items():
        row = completed.get(span_id)
        depth = 0
        parent = start.get("parent_span_id")
        while parent in spans:
            depth += 1
            parent = spans[parent].get("parent_span_id")
        elapsed = (
            f"{row['duration_ms'] / 1000:.3f}秒 [{row.get('status', row['outcome'])}]"
            if row
            else "尚无结束记录"
        )
        extra = {
            key: value
            for key, value in (row or {}).items()
            if key
            in {
                "indexed_tasks",
                "document_count",
                "batch_size",
                "hit_count",
                "model",
                "tool",
                "network_timings",
            }
        }
        if span_id in exchanges:
            extra["http_calls"] = exchanges[span_id]
        print(
            f"{'  ' * depth}{start['label']}：{elapsed}"
            + (f" {json.dumps(extra, ensure_ascii=False)}" if extra else "")
        )


if __name__ == "__main__":
    main()
