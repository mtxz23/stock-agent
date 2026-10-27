"""通过 python -m stock_research 启动单进程本地服务。"""

import logging

import uvicorn

from stock_research.config import Settings
from stock_research.logging_setup import BeijingFormatter, DailyFileHandler


def main() -> None:
    """初始化 UTF-8 日志并启动本机 HTTP 服务。"""
    log_dir = Settings().log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = BeijingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    file_handler = DailyFileHandler(log_dir)
    stream_handler = logging.StreamHandler()
    for handler in (file_handler, stream_handler):
        handler.setFormatter(formatter)
    logging.basicConfig(
        level=logging.INFO,
        handlers=[stream_handler, file_handler],
    )
    # HTTP 客户端日志包含资料 URL，避免以 INFO 级别重复记录每次请求。
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger(__name__).info("准备启动本地工作台：http://127.0.0.1:8765")
    # Uvicorn 启停和异常日志使用相同处理器；访问记录已由请求诊断保存。
    uvicorn.run(
        "stock_research.app:app",
        host="127.0.0.1",
        port=8765,
        workers=1,
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
