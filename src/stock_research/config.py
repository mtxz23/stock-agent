"""管理应用路径、运行限制和本地凭据加密。"""

import logging
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """从环境变量读取部署配置，默认使用项目内的本地数据目录。"""

    model_config = SettingsConfigDict(env_prefix="STOCK_", env_file=".env", extra="ignore")
    data_dir: Path = PROJECT_ROOT / "data"
    log_dir: Path = PROJECT_ROOT / "logs"
    frontend_dir: Path = PROJECT_ROOT / "frontend" / "dist"
    # 自选来源自动发现；多个客户端或账号时可通过环境变量指定。
    ths_install_dir: Path | None = None
    ths_account: str = ""
    watchlist_poll_interval: float = Field(default=10, ge=1, le=300)
    # 股票数据独立直连，整次查询超时不影响模型连接配置。
    market_data_timeout: float = Field(default=30, ge=3, le=120)
    qdrant_url: str = ""
    qdrant_api_key: SecretStr = SecretStr("")
    max_parallel_tasks: int = Field(default=2, ge=1, le=8)
    request_timeout: float = Field(default=120, ge=5, le=600)
    # 模型、向量生成和原文读取默认直连，避免继承桌面工具的环境代理。
    use_env_proxy: bool = False


class CredentialVault:
    """使用本地密钥加密数据库中的 API 凭据，不向前端返回明文。"""

    def __init__(self, data_dir: Path) -> None:
        """加载或创建当前数据目录的凭据密钥。"""
        data_dir.mkdir(parents=True, exist_ok=True)
        key_path = data_dir / "credentials.key"
        if not key_path.exists():
            try:
                with key_path.open("xb") as handle:
                    handle.write(Fernet.generate_key())
                logger.info("已创建本地凭据密钥")
            except FileExistsError:
                logger.info("本地凭据密钥已由另一启动进程创建")
        self._cipher = Fernet(key_path.read_bytes())

    def encrypt(self, value: str) -> str:
        """加密待保存的 API 密钥。"""
        return self._cipher.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        """还原调用所需凭据，损坏时明确报告错误。"""
        try:
            return self._cipher.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError) as exc:
            raise ValueError("凭据无法解密，请在模型设置中重新填写 API 密钥") from exc
