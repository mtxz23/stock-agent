"""按北京时间为运行日志生成每日文件，并保持 UTF-8 编码。"""

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

LOG_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")


class DailyFileHandler(logging.Handler):
    """每条日志按创建日期写入，不需要服务在午夜重启。"""

    def __init__(self, directory: Path, prefix: str = "research") -> None:
        """指定日志目录和文件名前缀，文件在首次写入当天记录时创建。"""
        super().__init__()
        self.directory = directory
        self.prefix = prefix

    def emit(self, record: logging.LogRecord) -> None:
        """按记录时间选择文件，追加后关闭，避免跨天占用旧文件句柄。"""
        try:
            path = daily_log_path(self.directory, self.prefix, ".log", record.created)
            self.directory.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(self.format(record) + "\n")
        except OSError:
            # 使用标准日志处理器的错误出口明确报告文件写入失败。
            self.handleError(record)


class BeijingFormatter(logging.Formatter):
    """使运行日志时间与每日文件的北京时间日期一致。"""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        """按 UTC+8 格式化记录时间。"""
        moment = datetime.fromtimestamp(record.created, LOG_TIMEZONE)
        if datefmt:
            return moment.strftime(datefmt)
        return moment.strftime("%Y-%m-%d %H:%M:%S") + f",{moment.microsecond // 1000:03d}"


def daily_log_path(
    directory: Path,
    prefix: str,
    suffix: str,
    timestamp: float | str | None = None,
) -> Path:
    """为当前时间、Unix 时间戳或带时区 ISO 时间生成每日文件名。"""
    return directory / f"{prefix}-{log_day(timestamp)}{suffix}"


def log_day(timestamp: float | str | None = None) -> str:
    """将记录时间转换为北京时间日期，用于写入与日期筛选。"""
    if isinstance(timestamp, str):
        moment = datetime.fromisoformat(timestamp)
        if moment.tzinfo is None:
            raise ValueError("日志记录时间必须包含时区")
        moment = moment.astimezone(LOG_TIMEZONE)
    elif timestamp is not None:
        moment = datetime.fromtimestamp(timestamp, LOG_TIMEZONE)
    else:
        moment = datetime.now(LOG_TIMEZONE)
    return moment.date().isoformat()
