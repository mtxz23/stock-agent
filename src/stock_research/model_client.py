"""通过可替换的模型接口调用云端 Chat Completions 服务。"""

import asyncio
import json
from time import perf_counter
from typing import Any, Protocol

import httpx

from stock_research.diagnostics import DiagnosticRecorder
from stock_research.timings import RequestClock


class ModelError(RuntimeError):
    """表示云端请求、响应协议或工具调用格式不符合要求。"""

    def __init__(self, message: str, *, diagnostic_id: str = "") -> None:
        """保存用户可见说明与本地诊断编号，不附带原始响应正文。"""
        super().__init__(message)
        self.diagnostic_id = diagnostic_id


class ResearchModel(Protocol):
    """隔离研究执行与云端厂商协议，后续适配器实现相同接口。"""

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """返回一个规范化的 assistant 消息。"""
        ...


class CloudChatModel:
    """实现支持 function tools 的 Chat Completions 兼容协议。"""

    def __init__(
        self,
        client: httpx.AsyncClient,
        config: dict[str, Any],
        api_key: str,
        diagnostics: DiagnosticRecorder | None = None,
    ) -> None:
        """绑定本次任务的连接快照，避免设置修改影响正在运行的任务。"""
        self._client = client
        self._config = config
        self._api_key = api_key
        self._diagnostics = diagnostics
        if diagnostics:
            diagnostics.add_secret(api_key)

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """记录每次云端调用的耗时、参数摘要、脱敏错误正文及追踪编号。"""
        started = perf_counter()
        exchange: dict[str, Any] = {}
        try:
            result = await self._complete(messages, tools, exchange)
        except asyncio.CancelledError:
            self._record("cancelled", started, exchange)
            raise
        except Exception as exc:
            identifier = self._record("failed", started, exchange, exc)
            if isinstance(exc, ModelError):
                exc.diagnostic_id = identifier
            raise
        self._record("succeeded", started, exchange)
        return result

    def _record(
        self,
        outcome: str,
        started: float,
        exchange: dict[str, Any],
        exception: BaseException | None = None,
    ) -> str:
        """将一次交换转换为诊断记录，推理字段和凭据由记录器统一过滤。"""
        if not self._diagnostics:
            return ""
        response = exchange.get("response")
        response_data: dict[str, Any] = {"http_status": None}
        if response is not None:
            response_data["http_status"] = response.status_code
            response_data["provider_headers"] = {
                name: response.headers[name]
                for name in (
                    "x-request-id",
                    "request-id",
                    "x-siliconcloud-trace-id",
                    "x-trace-id",
                    "traceparent",
                    "retry-after",
                    "cf-ray",
                )
                if name in response.headers
            }
            if outcome == "failed":
                try:
                    response_data["provider_error"] = response.json()
                except (ValueError, RecursionError):
                    response_data["provider_error"] = response.text
                # 失败也可能是 HTTP 200 的输出截断，保留用量及长度信息方便定位。
                error_body = response_data.get("provider_error")
                if isinstance(error_body, dict):
                    response_data["usage"] = error_body.get("usage", {})
                    choices = error_body.get("choices") or []
                    if choices and isinstance(choices[0], dict):
                        message = choices[0].get("message") or {}
                        if isinstance(message, dict):
                            response_data["response_summary"] = {
                                "finish_reason": choices[0].get("finish_reason"),
                                "content_characters": len(message.get("content") or "")
                                if isinstance(message.get("content"), str)
                                else 0,
                                "reasoning_characters": len(message.get("reasoning_content") or "")
                                if isinstance(message.get("reasoning_content"), str)
                                else 0,
                            }
            else:
                try:
                    body = response.json()
                    response_data["usage"] = body.get("usage", {})
                    choice = body["choices"][0]
                    message = choice["message"]
                    response_data["response_summary"] = {
                        "finish_reason": choice.get("finish_reason"),
                        "content_characters": len(message.get("content") or ""),
                        "tool_calls": [
                            {"id": call.get("id"), "name": call.get("function", {}).get("name")}
                            for call in message.get("tool_calls") or []
                        ],
                    }
                except (
                    ValueError,
                    AttributeError,
                    KeyError,
                    IndexError,
                    TypeError,
                    RecursionError,
                ):
                    response_data["usage"] = {"unavailable": True}
        payload = exchange.get("payload", {})
        summary = {key: value for key, value in payload.items() if key != "messages"}
        history = payload.get("messages", [])
        summary["message_count"] = len(history)
        # 汇总全部输入，不保存全文；方便定位工具结果膨胀和历史推理累积。
        summary["input_statistics"] = {
            "content_characters_by_role": {
                role: sum(
                    len(item.get("content") or "") for item in history if item.get("role") == role
                )
                for role in ("system", "user", "assistant", "tool")
            },
            "reasoning_characters": sum(
                len(item.get("reasoning_content") or "") for item in history
            ),
        }
        summary["messages"] = (
            history[-8:]
            if outcome == "failed"
            else [
                {
                    "role": item.get("role"),
                    "content_characters": len(item.get("content") or ""),
                    "tool_call_id": item.get("tool_call_id"),
                }
                for item in history[-8:]
            ]
        )
        return self._diagnostics.record(
            "model.call",
            outcome,
            message=str(exception) if exception else "",
            exception=exception,
            model_id=self._config.get("id"),
            model=self._config["model"],
            endpoint=exchange.get("url", self._config["base_url"]),
            duration_ms=round((perf_counter() - started) * 1000, 2),
            request=summary,
            network_timings=exchange.get("network_timings", {}),
            **response_data,
        )

    async def _complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], exchange: dict[str, Any]
    ) -> dict[str, Any]:
        """发送云端请求并校验响应，原始交换仅在本次调用内保存。"""
        base = self._config["base_url"].rstrip("/")
        url = base if base.endswith("/chat/completions") else base + "/chat/completions"
        payload: dict[str, Any] = {
            "model": self._config["model"],
            "messages": messages,
            "stream": False,
        }
        # 不同兼容服务使用不同的输出预算参数，按连接配置发送。
        payload[self._config.get("token_parameter", "max_tokens")] = self._config["max_tokens"]
        if tools:
            payload.update(tools=tools, tool_choice="auto")
        exchange.update(url=url, payload=payload)
        clock = RequestClock()
        exchange["network_timings"] = clock.values
        try:
            response = await self._client.post(
                url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
                extensions={"trace": clock.trace},
            )
            exchange["response"] = response
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            detail = {
                401: "API 密钥无效或未授权",
                403: "连接没有访问权限",
                404: "API 地址或模型名称不存在",
                429: "请求限流或账户额度不足",
            }.get(code, "云端请求被拒绝，请检查模型协议、参数及服务状态")
            raise ModelError(f"HTTP {code}：{detail}") from exc
        except httpx.TimeoutException as exc:
            raise ModelError("云端模型请求超时；可稍后恢复研究") from exc
        except httpx.HTTPError as exc:
            raise ModelError("无法连接云端模型，请检查网络和 API 地址") from exc
        try:
            body = response.json()
            choice = body["choices"][0]
            if choice.get("finish_reason") == "length":
                message = choice.get("message") or {}
                detail = "，模型尚未返回正文" if not message.get("content") else "，回复不完整"
                raise ModelError(
                    "模型输出被长度限制截断"
                    f"（输出上限{self._config['max_tokens']} token{detail}）；"
                    "请调整最大输出长度或更换模型后重试"
                )
            message = choice["message"]
            content = message.get("content")
            if content is not None and not isinstance(content, str):
                raise ModelError("当前连接返回了不支持的 content 格式")
            normalized: dict[str, Any] = {"role": "assistant", "content": content}
            # 保留服务商要求的连续工具调用字段，仅供消息往返和检查点使用。
            # 不将这些协议数据写入面向前端的进展事件。
            for field in ("reasoning_content", "reasoning"):
                if field in message:
                    value = message[field]
                    if value is not None and not isinstance(value, str):
                        raise ModelError(f"当前连接返回了不支持的 {field} 格式")
                    normalized[field] = value
            if "reasoning_details" in message:
                details = message["reasoning_details"]
                if details is not None and (
                    not isinstance(details, list)
                    or any(not isinstance(item, dict) for item in details)
                ):
                    raise ModelError("当前连接返回了不支持的 reasoning_details 格式")
                normalized["reasoning_details"] = details
            calls = message.get("tool_calls") or []
            if calls:
                normalized["tool_calls"] = _validate_calls(calls)
            if not content and not calls:
                raise ModelError("模型没有返回文本或工具调用")
            return normalized
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
            raise ModelError("云端响应不符合 Chat Completions 协议") from exc


def _validate_calls(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """保留规范字段并验证工具调用标识与参数字符串。"""
    result = []
    identifiers: set[str] = set()
    for call in calls:
        function = call["function"]
        identifier = call["id"]
        if (
            call.get("type") != "function"
            or not isinstance(identifier, str)
            or not identifier
            or identifier in identifiers
            or not isinstance(function["name"], str)
            or not isinstance(function["arguments"], str)
        ):
            raise ModelError("模型返回了无效或重复的工具调用")
        identifiers.add(identifier)
        # 语法错误由工具节点反馈给模型修正，不提前丢弃调用记录。
        result.append(
            {
                "id": identifier,
                "type": "function",
                "function": {"name": function["name"], "arguments": function["arguments"]},
            }
        )
    return result


def decode_arguments(value: str) -> dict[str, Any]:
    """将工具参数解析为对象，拒绝数组或标量。"""
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("工具参数必须是 JSON 对象")
    return parsed
