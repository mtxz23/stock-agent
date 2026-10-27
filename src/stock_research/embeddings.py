"""调用可配置的云端 embedding API，验证向量并记录脱敏诊断。"""

import asyncio
import hashlib
import json
import logging
import math
from time import perf_counter
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from stock_research.diagnostics import DiagnosticRecorder
from stock_research.timings import RequestClock

logger = logging.getLogger(__name__)
DEFAULT_EMBEDDING_MODEL = "Qwen/Qwen3-Embedding-8B"
QUERY_INSTRUCTION = (
    "Retrieve relevant past conversations and company research that answer the query."
)


class EmbeddingError(ValueError):
    """可恢复的向量配置、服务调用或响应格式错误。"""

    def __init__(self, message: str, diagnostic_id: str = "") -> None:
        """保留诊断编号，不附带密钥或原始向量。"""
        super().__init__(message)
        self.diagnostic_id = diagnostic_id


class EmbeddingConfig(BaseModel):
    """embedding 独立选模型，API地址和密钥复用指定连接。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    connection_id: str = Field(min_length=1, max_length=80)
    model: str = Field(default=DEFAULT_EMBEDDING_MODEL, min_length=1, max_length=150)
    dimensions: int = Field(default=1024, ge=64, le=4096)
    min_score: float = Field(default=0.3, ge=-1, le=1)

    @field_validator("dimensions")
    @classmethod
    def check_dimensions(cls, value: int) -> int:
        """使用硅基流动 Qwen embedding 接口列出的输出维度。"""
        if value not in {64, 128, 256, 512, 768, 1024, 1536, 2048, 2560, 4096}:
            raise ValueError("请选择接口支持的输出维度")
        return value


class EmbeddingModel(Protocol):
    """隔离云端 embedding 实现，测试可以替换且不产生费用。"""

    namespace: str
    dimensions: int

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """将原始片段转成向量。"""
        ...

    async def embed_query(self, query: str) -> list[float]:
        """将完整问题转成检索向量。"""
        ...


class CloudEmbeddingModel:
    """实现 OpenAI 兼容的 /embeddings 请求，保留取消传播。"""

    def __init__(
        self,
        client: httpx.AsyncClient,
        config: dict[str, Any],
        api_key: str,
        diagnostics: DiagnosticRecorder | None = None,
    ) -> None:
        """绑定不可变配置；模型和维度共同确定索引空间。"""
        self.client = client
        self.config = dict(config)
        self.api_key = api_key
        self.dimensions = config["dimensions"]
        self.namespace = embedding_namespace(config)
        self.diagnostics = diagnostics
        if diagnostics:
            diagnostics.add_secret(api_key)
        parts = urlsplit(config["base_url"])
        if parts.scheme != "https" or not parts.hostname:
            raise EmbeddingError("embedding API必须使用有效的HTTPS地址")

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """批量编码片段，输入不是空白文本。"""
        if not texts or any(not text.strip() for text in texts):
            raise EmbeddingError("embedding 输入不能为空")
        return await self._request(texts)

    async def embed_query(self, query: str) -> list[float]:
        """按 Qwen 官方建议，查询侧附加英文检索任务说明。"""
        text = query
        if self.config["model"].startswith("Qwen/Qwen3-Embedding"):
            text = f"Instruct: {QUERY_INSTRUCTION}\nQuery: {query}"
        return (await self._request([text]))[0]

    async def _request(self, texts: list[str]) -> list[list[float]]:
        """校验数量、下标和向量数值；瞬时限流最多重试两次。"""
        for attempt in range(3):
            started = perf_counter()
            response = None
            clock = RequestClock()
            try:
                response = await self.client.post(
                    self.config["base_url"].rstrip("/") + "/embeddings",
                    headers={"Authorization": "Bearer " + self.api_key},
                    json={
                        "model": self.config["model"],
                        "input": texts,
                        "dimensions": self.dimensions,
                        "encoding_format": "float",
                    },
                    extensions={"trace": clock.trace},
                )
                if response.status_code in {429, 502, 503, 504} and attempt < 2:
                    self._record(
                        "retry", started, response, len(texts), network_timings=clock.values
                    )
                    logger.warning("embedding服务暂不可用，将重试，状态=%s", response.status_code)
                    await asyncio.sleep(attempt + 1)
                    continue
                if response.is_error:
                    raise EmbeddingError(f"embedding服务请求失败（HTTP {response.status_code}）")
                vectors = self._decode(response.json(), len(texts))
            except asyncio.CancelledError:
                self._record(
                    "cancelled", started, response, len(texts), network_timings=clock.values
                )
                raise
            except (httpx.HTTPError, ValueError, KeyError, TypeError, OverflowError) as exc:
                identifier = self._record(
                    "failed", started, response, len(texts), exc, network_timings=clock.values
                )
                if isinstance(exc, EmbeddingError):
                    exc.diagnostic_id = identifier
                    raise
                raise EmbeddingError(
                    "embedding请求或响应格式异常，请检查连接和诊断记录", identifier
                ) from exc
            self._record("succeeded", started, response, len(texts), network_timings=clock.values)
            return vectors
        raise EmbeddingError("embedding服务重试后仍不可用")

    def _decode(self, payload: dict[str, Any], count: int) -> list[list[float]]:
        """拒绝遗漏、重复下标、错误维度及非有限或全零向量。"""
        rows = payload["data"]
        if not isinstance(rows, list) or len(rows) != count:
            raise EmbeddingError("embedding返回数量与输入不一致")
        ordered: dict[int, list[float]] = {}
        for row in rows:
            index = row["index"]
            if isinstance(index, bool) or not isinstance(index, int) or index in ordered:
                raise EmbeddingError("embedding返回下标无效或重复")
            if not 0 <= index < count:
                raise EmbeddingError("embedding返回下标超出范围")
            raw = row["embedding"]
            if not isinstance(raw, list) or len(raw) != self.dimensions:
                raise EmbeddingError("embedding维度与配置不一致，不能混用向量")
            if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in raw):
                raise EmbeddingError("embedding包含非数值内容")
            vector = [float(value) for value in raw]
            if not all(math.isfinite(value) for value in vector) or not any(vector):
                raise EmbeddingError("embedding包含无效数值或全零向量")
            ordered[index] = vector
        return [ordered[index] for index in range(count)]

    def _record(
        self,
        outcome: str,
        started: float,
        response: httpx.Response | None,
        count: int,
        exception: BaseException | None = None,
        *,
        network_timings: dict[str, Any] | None = None,
    ) -> str:
        """不记录输入正文及向量，凭据由统一诊断过滤。"""
        if not self.diagnostics:
            return ""
        return self.diagnostics.record(
            "embedding.request",
            outcome,
            exception=exception,
            message=str(exception) if exception else "",
            model=self.config["model"],
            input_count=count,
            dimensions=self.dimensions,
            duration_ms=round((perf_counter() - started) * 1000, 2),
            network_timings=network_timings or {},
            http_status=response.status_code if response is not None else None,
            provider_error=response.text[:4000]
            if response is not None and response.is_error
            else "",
            provider_request_id=response.headers.get("x-siliconcloud-trace-id")
            if response is not None
            else "",
        )


def embedding_namespace(config: dict[str, Any]) -> str:
    """模型、服务地址、维度或问答编码规则变化后使用独立索引空间。"""
    signature = [
        config["base_url"].rstrip("/"),
        config["model"],
        config["dimensions"],
        QUERY_INSTRUCTION,
        "history-qa-v2",
    ]
    return hashlib.sha256(json.dumps(signature).encode("utf-8")).hexdigest()
