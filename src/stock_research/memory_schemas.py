"""定义记忆内容及模型工具输入，页面只需填写文字。"""

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MemoryCategory = Literal["profile", "preference", "rule", "fact", "opinion", "question"]


class MemoryInput(BaseModel):
    """记忆正文为必填项，其余信息由系统按需维护。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    content: str = Field(min_length=1, max_length=2000)
    category: MemoryCategory = "preference"
    company: str = Field(default="", max_length=200)
    tags: list[str] = Field(default_factory=list, max_length=12)
    as_of: date | None = None
    expires_on: date | None = None

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, tags: list[str]) -> list[str]:
        """去除重复标签，限制单个标签长度。"""
        if any(not tag.strip() or len(tag.strip()) > 60 for tag in tags):
            raise ValueError("标签不能为空或超过60字")
        return list(dict.fromkeys(tag.strip() for tag in tags))

    @model_validator(mode="after")
    def check_dates(self):
        """到期时间不能早于信息所属日期。"""
        if self.as_of and self.expires_on and self.expires_on < self.as_of:
            raise ValueError("到期时间不能早于信息日期")
        return self


class MemoryUpdate(MemoryInput):
    """通过版本号防止过时页面覆盖新的修改。"""

    version: int = Field(ge=1)


class SaveMemoryInput(MemoryInput):
    """模型自动保存记忆，可附上本轮用户引用或原文来源。"""

    source_quote: str = Field(default="", max_length=4000)
    source_id: str | None = Field(default=None, max_length=80)


class UpdateMemoryInput(SaveMemoryInput):
    """模型整理现有记忆，使用上下文中提供的当前版本。"""

    id: str = Field(min_length=1, max_length=80)
    version: int = Field(ge=1)


class DeleteMemoryInput(BaseModel):
    """删除重复、错误或用户要求忘记的条目。"""

    id: str = Field(min_length=1, max_length=80)
    version: int = Field(ge=1)
