"""保存脱敏诊断记录，关联页面请求、云端调用和后台研究。"""

import argparse
import json
import logging
import re
import sys
import threading
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date
from pathlib import Path
from typing import Any
from uuid import uuid4

from stock_research.config import Settings
from stock_research.logging_setup import daily_log_path, log_day
from stock_research.storage import now

logger = logging.getLogger(__name__)
CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar("diagnostic_context", default=None)
SECRET_FIELDS = {
    "authorization",
    "apikey",
    "apikeyencrypted",
    "password",
    "secret",
    "token",
    "accesstoken",
    "refreshtoken",
    "cookie",
    "setcookie",
    "xapikey",
}
HIDDEN_FIELDS = {"reasoning", "reasoningcontent", "reasoningdetails"}
MAX_TEXT = 16000


class DiagnosticRecorder:
    """按 JSONL 持久化有限长度的诊断记录，写入失败时回退到应用日志。"""

    def __init__(self, directory: Path) -> None:
        """指定独立日志目录，初始化锁和仅在内存中保存的脱敏词。"""
        self.directory = directory
        self._lock = threading.RLock()
        self._secrets: set[str] = set()

    @property
    def path(self) -> Path:
        """返回今天的诊断文件位置，跨天后自动变化。"""
        return daily_log_path(self.directory, "requests", ".jsonl")

    def add_secret(self, value: str) -> None:
        """注册当前调用使用的密钥，避免服务商在错误正文中回显它。"""
        if value:
            with self._lock:
                self._secrets.add(value)

    def record(
        self,
        stage: str,
        outcome: str,
        *,
        diagnostic_id: str | None = None,
        message: str = "",
        exception: BaseException | None = None,
        **data: Any,
    ) -> str:
        """保存上下文、错误及堆栈，返回可与页面关联的诊断编号。"""
        identifier = diagnostic_id or uuid4().hex
        record = {
            "diagnostic_id": identifier,
            "created_at": now(),
            "stage": stage,
            "outcome": outcome,
            **(CONTEXT.get() or {}),
            **data,
            "message": message,
        }
        if exception is not None:
            record["exception_type"] = type(exception).__name__
            record["traceback"] = "".join(traceback.format_exception(exception))
        safe = self.redact(record)
        line = json.dumps(safe, ensure_ascii=False, default=str)
        try:
            with self._lock:
                self.directory.mkdir(parents=True, exist_ok=True)
                path = daily_log_path(self.directory, "requests", ".jsonl", record["created_at"])
                # 每次写完关闭文件，便于 Windows 下读取、轮转和测试目录清理。
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except OSError:
            logger.exception("诊断文件写入失败，编号=%s", identifier)
            logger.error("诊断记录回退：%s", line)
        if outcome == "failed":
            if data.get("recoverable"):
                logger.warning(
                    "可恢复失败，阶段=%s，诊断=%s，原因=%s", stage, identifier, safe["message"]
                )
            elif exception is not None:
                # 原异常可能回显凭据，应用日志中只输出经过过滤的堆栈。
                logger.exception(
                    "请求失败，阶段=%s，诊断=%s，原因=%s\n%s",
                    stage,
                    identifier,
                    safe["message"],
                    safe["traceback"],
                    exc_info=False,
                )
            else:
                logger.error(
                    "请求失败，阶段=%s，诊断=%s，原因=%s", stage, identifier, safe["message"]
                )
        return identifier

    def redact(self, value: Any, *, depth: int = 0) -> Any:
        """递归过滤凭据与推理内容，限制诊断正文、列表和嵌套深度。"""
        if depth > 12:
            return "[嵌套内容已截断]"
        if isinstance(value, dict):
            output = {}
            for key, item in list(value.items())[:100]:
                normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
                if normalized in SECRET_FIELDS:
                    output[str(key)] = "[REDACTED]"
                elif normalized in HIDDEN_FIELDS:
                    output[str(key)] = {"present": item is not None, "characters": len(str(item))}
                else:
                    output[str(key)] = self.redact(item, depth=depth + 1)
            return output
        if isinstance(value, (list, tuple)):
            items = [self.redact(item, depth=depth + 1) for item in value[:50]]
            return items + ([{"truncated_items": len(value) - 50}] if len(value) > 50 else [])
        if isinstance(value, str):
            # 工具参数可能以 JSON 字符串嵌套；先按字段过滤，避免漏掉其中的凭据。
            if value.lstrip().startswith(("{", "[")):
                try:
                    parsed = json.loads(value)
                except (ValueError, RecursionError):
                    parsed = None
                if isinstance(parsed, (dict, list)):
                    value = json.dumps(self.redact(parsed, depth=depth + 1), ensure_ascii=False)
            with self._lock:
                secrets = sorted(self._secrets, key=len, reverse=True)
            for secret in secrets:
                value = value.replace(secret, "[REDACTED]")
            value = re.sub(r"(?i)\bBearer\s+[^\s\"'<>;,}]+", "Bearer [REDACTED]", value)
            value = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", "[REDACTED]", value)
            value = re.sub(
                r"(?i)((?:api[_-]?key|access[_-]?token|password|token|signature)[\"']?"
                r"\s*[=:]\s*[\"']?)"
                r"[^\s&\"'<>;,}]+",
                r"\1[REDACTED]",
                value,
            )
            return value[:MAX_TEXT] + ("\n[正文已截断]" if len(value) > MAX_TEXT else "")
        if value is None or isinstance(value, (int, float, bool)):
            return value
        return self.redact(str(value), depth=depth + 1)


@contextmanager
def diagnostic_context(**values: Any) -> Iterator[None]:
    """绑定当前协程上下文，退出时恢复，避免并发任务混用编号。"""
    token = CONTEXT.set({**(CONTEXT.get() or {}), **values})
    try:
        yield
    finally:
        CONTEXT.reset(token)


def read_diagnostics(
    directory: Path,
    *,
    limit: int = 20,
    failed_only: bool = True,
    task_id: str | None = None,
    request_id: str | None = None,
    day: str | None = None,
    legacy_directory: Path | None = None,
) -> list[dict[str, Any]]:
    """从最近日期倒序读取，可按北京时间日期筛选并兼容旧日志目录。"""
    if day is not None:
        day = date.fromisoformat(day).isoformat()
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    limit = max(1, min(limit, 200))
    for file in _diagnostic_files(directory, day, legacy_directory):
        try:
            for line in _reverse_lines(file):
                try:
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        raise ValueError("诊断记录不是对象")
                    if day and log_day(record["created_at"]) != day:
                        continue
                except (ValueError, KeyError):
                    logger.warning("诊断文件含未完成或损坏的记录，文件=%s", file.name)
                    continue
                if failed_only and record.get("outcome") != "failed":
                    continue
                if task_id and record.get("task_id") != task_id:
                    continue
                if request_id and record.get("request_id") != request_id:
                    continue
                identifier = record.get("diagnostic_id")
                if identifier and identifier in seen:
                    continue
                if identifier:
                    seen.add(identifier)
                records.append(record)
                if len(records) >= limit:
                    return records
        except FileNotFoundError:
            continue
    return records


def main() -> None:
    """提供直接读取最近失败记录的终端入口，无需浏览器操作。"""
    parser = argparse.ArgumentParser(description="读取本地请求诊断记录")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--task-id")
    parser.add_argument("--request-id")
    parser.add_argument("--all", action="store_true", help="包含成功和取消请求")
    settings = Settings()
    parser.add_argument("--log-dir", type=Path, default=settings.log_dir)
    parser.add_argument(
        "--data-dir", type=Path, default=settings.data_dir, help="旧版本诊断目录所在的数据目录"
    )
    parser.add_argument(
        "--date", type=date.fromisoformat, dest="day", help="北京时间日期 YYYY-MM-DD"
    )
    args = parser.parse_args()
    records = read_diagnostics(
        args.log_dir,
        limit=args.limit,
        failed_only=not args.all,
        task_id=args.task_id,
        request_id=args.request_id,
        day=args.day.isoformat() if args.day else None,
        legacy_directory=args.data_dir / "diagnostics",
    )
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(records, ensure_ascii=False, indent=2))


def _diagnostic_files(
    directory: Path, day: str | None, legacy_directory: Path | None
) -> Iterator[Path]:
    """优先枚举每日新文件，再枚举旧格式备份，读取时不移动或覆盖旧文件。"""
    pattern = f"requests-{day}.jsonl" if day else "requests-????-??-??.jsonl"
    yield from sorted(directory.glob(pattern), reverse=True)
    roots = [directory]
    if legacy_directory is not None and legacy_directory.resolve() != directory.resolve():
        roots.append(legacy_directory)
    for root in roots:
        candidates = [
            file
            for file in root.glob("requests.jsonl*")
            if re.fullmatch(r"requests\.jsonl(?:\.[1-9][0-9]*)?", file.name)
        ]
        yield from sorted(
            candidates,
            key=lambda file: (
                int(file.name.rsplit(".", 1)[-1]) if file.name != "requests.jsonl" else 0
            ),
        )


def _reverse_lines(path: Path) -> Iterator[str]:
    """按块倒序读取完整 UTF-8 行，找到足够记录后即可停止，不加载整个大文件。"""
    with path.open("rb") as handle:
        position = handle.seek(0, 2)
        pending = b""
        while position > 0:
            size = min(position, 65536)
            position -= size
            handle.seek(position)
            pieces = (handle.read(size) + pending).split(b"\n")
            pending = pieces[0]
            for line in reversed(pieces[1:]):
                if line:
                    yield line.decode("utf-8", errors="replace")
        if pending:
            yield pending.decode("utf-8", errors="replace")


if __name__ == "__main__":
    main()
