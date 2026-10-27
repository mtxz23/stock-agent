"""定义接口输入、模型连接配置和研究任务状态。"""

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator

TaskStatus = Literal["queued", "running", "completed", "partial", "paused", "interrupted", "failed"]
ACTIVE_STATUSES = {"queued", "running"}


class ModelInput(BaseModel):
    """描述一个支持工具调用的云端 Chat Completions 兼容连接。"""

    name: str = Field(min_length=1, max_length=80)
    protocol: Literal["chat_completions"] = "chat_completions"
    base_url: str = Field(min_length=8, max_length=500)
    model: str = Field(min_length=1, max_length=150)
    api_key: str = Field(default="", max_length=2000)
    max_tokens: int = Field(default=4096, ge=256, le=32768)
    token_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    max_steps: int = Field(default=12, ge=2, le=30)
    # 本地估算预算包含输入、工具定义和输出预留，并非服务商能力声明。
    context_budget: int = Field(default=64000, ge=8192, le=262144)

    @model_validator(mode="after")
    def check_context_budget(self):
        """输出预留不能占满本地上下文预算。"""
        if self.max_tokens + 1024 >= self.context_budget:
            raise ValueError("上下文预算需大于输出预算并预留输入空间")
        return self

    @field_validator("name", "model", "base_url")
    @classmethod
    def strip_required(cls, value: str) -> str:
        """去除输入首尾空白，禁止只包含空白的必填项。"""
        value = value.strip()
        if not value:
            raise ValueError("内容不能为空")
        return value

    @field_validator("base_url")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        """限定云端 HTTPS 地址，避免把凭据嵌入 URL。"""
        parts = urlsplit(value)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("云端 API 地址必须使用 HTTPS")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("API 地址不能包含凭据、查询参数或片段")
        return value.rstrip("/")


class MessageInput(BaseModel):
    """接收普通聊天或研究请求，通过父消息关联对话。"""

    message: str = Field(min_length=1, max_length=4000)
    model_id: str = Field(min_length=1, max_length=80)
    parent_id: str | None = Field(default=None, min_length=1, max_length=80)

    @field_validator("message", "model_id", "parent_id")
    @classmethod
    def strip_required(cls, value: str | None) -> str | None:
        """去除空白，拒绝空消息和空连接标识。"""
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("内容不能为空")
        return value


class ConversationTitleInput(BaseModel):
    """接收整段对话的显示标题，不改变原始提问或研究对象。"""

    title: str = Field(min_length=1, max_length=80)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        """合并空白并拒绝空标题。"""
        value = " ".join(value.split())
        if not value:
            raise ValueError("标题不能为空")
        return value
