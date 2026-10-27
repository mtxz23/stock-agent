"""安装并启动官方 Windows Qdrant 独立服务，输出按日保存的运行日志。"""

import argparse
import hashlib
import io
import json
import logging
import os
import re
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import httpx

from stock_research.config import Settings
from stock_research.logging_setup import BeijingFormatter, DailyFileHandler

logger = logging.getLogger(__name__)
LOCAL_URL = "http://127.0.0.1:6333"
VERSION = "1.19.2"
PACKAGES = (
    (
        f"https://github.com/qdrant/qdrant/releases/download/v{VERSION}/"
        "qdrant-x86_64-pc-windows-msvc.zip",
        "7d86596f16c6e85d45a50312f5e16ccb51e059b62e3d7308110cb65b4c799a4d",
        "bin",
    ),
    (
        "https://github.com/qdrant/qdrant-web-ui/releases/download/v0.2.19/dist-qdrant.zip",
        "be50e32d84aa41c249cad5d2b109d7538cf283fbdab71dee5f5bfa40ca29fd53",
        "static",
    ),
)


def main() -> None:
    """启动或检查服务；失败输出明确原因及日志位置，不吞掉异常。"""
    parser = argparse.ArgumentParser(description="管理本机Qdrant独立服务")
    parser.add_argument("action", choices=("ensure", "serve"))
    arguments = parser.parse_args()
    settings = Settings()
    handler = DailyFileHandler(settings.log_dir, prefix="qdrant")
    handler.setFormatter(BeijingFormatter("%(asctime)s %(levelname)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        if arguments.action == "serve":
            serve(settings)
        else:
            ensure_service(settings)
            print(f"Qdrant管理界面：{LOCAL_URL}/dashboard")
    except KeyboardInterrupt:
        logger.info("Qdrant服务启动操作已取消")
        raise
    except Exception:
        logger.exception("Qdrant服务操作失败")
        raise


def ensure_service(settings: Settings) -> None:
    """已有健康服务直接复用，否则安装并在隐藏进程中启动。"""
    if healthy():
        logger.info("Qdrant独立服务已就绪，地址=%s", LOCAL_URL)
        return
    if sys.platform != "win32":
        raise RuntimeError("此启动器使用Windows官方发布包；其他系统请单独部署Qdrant")
    directory = (settings.data_dir / "qdrant").resolve()
    install(directory)
    write_config(directory)
    # 带上明确的数据路径，后台进程不依赖调用者的工作目录。
    environment = os.environ.copy()
    environment.update(
        STOCK_DATA_DIR=str(settings.data_dir.resolve()),
        STOCK_LOG_DIR=str(settings.log_dir.resolve()),
        PYTHONUTF8="1",
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "stock_research.qdrant_service", "serve"],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        creationflags=subprocess.CREATE_NO_WINDOW,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    logger.info("已启动Qdrant后台进程，监护进程=%s", process.pid)
    for _ in range(80):
        if healthy():
            return
        if process.poll() is not None:
            raise RuntimeError(f"Qdrant启动失败，请查看{settings.log_dir}中的当日日志")
        time.sleep(0.25)
    raise RuntimeError("Qdrant未在预期时间内就绪，请查看当日日志")


def install(directory: Path) -> None:
    """首次下载固定版本，SHA-256校验完成后才解压官方发布包。"""
    binary = directory / "bin" / "qdrant.exe"
    dashboard = directory / "static" / "dist" / "index.html"
    if binary.exists() and dashboard.exists():
        return
    directory.mkdir(parents=True, exist_ok=True)
    for url, digest, subdirectory in PACKAGES:
        destination = directory / subdirectory
        if (binary if subdirectory == "bin" else dashboard).exists():
            continue
        logger.info("下载官方Qdrant组件，组件=%s", subdirectory)
        with httpx.Client(timeout=120, follow_redirects=True) as client:
            response = client.get(url)
            response.raise_for_status()
        extract_package(response.content, digest, destination)
    logger.info("Qdrant官方程序与管理界面安装完成，版本=%s", VERSION)


def extract_package(content: bytes, digest: str, destination: Path) -> None:
    """拒绝校验失败及越界路径，解压范围限定在组件目录内。"""
    if hashlib.sha256(content).hexdigest() != digest:
        raise ValueError("Qdrant发布包SHA-256校验失败")
    root = destination.resolve()
    with zipfile.ZipFile(io.BytesIO(content)) as package:
        if any(
            not (root / entry.filename).resolve().is_relative_to(root)
            for entry in package.infolist()
        ):
            raise ValueError("Qdrant发布包包含越界路径")
        package.extractall(root)


def serve(settings: Settings) -> None:
    """运行官方服务，合并标准输出与错误输出并按北京时间每日轮转日志。"""
    directory = (settings.data_dir / "qdrant").resolve()
    config = directory / "config.yaml"
    binary = directory / "bin" / "qdrant.exe"
    process = subprocess.Popen(
        [str(binary), "--config-path", str(config)],
        cwd=directory,
        creationflags=subprocess.CREATE_NO_WINDOW,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    (directory / "service.json").write_text(
        json.dumps(
            {
                "helper_pid": os.getpid(),
                "server_pid": process.pid,
                "binary": str(binary),
                "config": str(config),
                "version": VERSION,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    logger.info("Qdrant独立服务正在启动，进程=%s，目录=%s", process.pid, directory)
    try:
        for line in process.stdout:
            text = re.sub(r"\x1b\[[0-9;]*m", "", line).rstrip()
            if text:
                level = (
                    logging.ERROR
                    if "ERROR" in text
                    else (logging.WARNING if "WARN" in text else logging.INFO)
                )
                logger.log(level, text)
        code = process.wait()
        if code:
            raise RuntimeError(f"Qdrant独立服务异常退出，退出码={code}")
        logger.info("Qdrant独立服务已关闭")
    finally:
        # 包装器正常退出或被Ctrl+C打断时不遗留子进程。
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        process.stdout.close()


def write_config(directory: Path) -> None:
    """数据、快照及管理界面使用独立目录，只监听本机回环地址。"""

    def quoted(path: Path) -> str:
        """JSON字符串也是合法YAML标量，兼容空格和中文路径。"""
        return json.dumps(path.as_posix(), ensure_ascii=False)

    content = (
        "log_level: INFO\ntelemetry_disabled: true\n"
        "service:\n  host: 127.0.0.1\n  http_port: 6333\n  grpc_port: null\n"
        "  max_workers: 2\n  enable_static_content: true\n"
        f"  static_content_dir: {quoted(directory / 'static' / 'dist')}\n"
        "storage:\n"
        f"  storage_path: {quoted(directory / 'storage')}\n"
        f"  snapshots_path: {quoted(directory / 'snapshots')}\n"
        "  performance:\n    max_search_threads: 2\n    optimizer_cpu_budget: 2\n"
    )
    (directory / "config.yaml").write_text(content, encoding="utf-8")


def healthy() -> bool:
    """只访问本机端口，区分未启动、Qdrant就绪和其他程序占用。"""
    try:
        with httpx.Client(timeout=1, trust_env=False) as client:
            response = client.get(LOCAL_URL)
            response.raise_for_status()
            body = response.json()
            if "qdrant" not in str(body.get("title", "")).casefold():
                raise RuntimeError("6333端口已被其他服务占用")
            return client.get(LOCAL_URL + "/readyz").status_code == 200
    except (httpx.ConnectError, httpx.TimeoutException):
        return False


if __name__ == "__main__":
    main()
