"""验证云端协议往返、错误脱敏和工具调用校验。"""

import json

import httpx
import pytest

from stock_research.model_client import CloudChatModel, ModelError

CONFIG = {"base_url": "https://model.example/v1", "model": "research", "max_tokens": 4096}


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"reasoning_content": "测试协议文本"},
        {
            "reasoning_details": [
                {"type": "reasoning.encrypted", "data": "test-signature", "index": 0}
            ]
        },
    ],
)
async def test_tool_messages_preserve_call_identifier(metadata):
    """确认工具消息与调用标识按云端协议往返。"""
    requests = []

    def respond(request):
        """返回第一次工具调用及第二次报告。"""
        requests.append(json.loads(request.content))
        assert request.headers["Authorization"] == "Bearer test-secret"
        assert request.url.path == "/v1/chat/completions"
        message = (
            {"content": "报告"}
            if len(requests) == 2
            else {
                **metadata,
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "calculate", "arguments": '{"expression":"1+2"}'},
                    }
                ],
            }
        )
        return httpx.Response(200, json={"choices": [{"message": message}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model = CloudChatModel(client, CONFIG, "test-secret")
        messages = [{"role": "user", "content": "研究"}]
        reply = await model.complete(messages, [{"type": "function"}])
        messages.extend([reply, {"role": "tool", "tool_call_id": "call-1", "content": "3"}])
        assert (await model.complete(messages, []))["content"] == "报告"
    assert requests[1]["messages"][-1]["tool_call_id"] == "call-1"
    for field, value in metadata.items():
        assert requests[1]["messages"][-2][field] == value
    assert "tools" not in requests[1]


@pytest.mark.parametrize("status", [401, 429, 500])
async def test_http_errors_do_not_echo_provider_body(status):
    """服务商错误内容与凭据不能进入用户可见的错误。"""
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, text="test-secret private body")
        )
    ) as client:
        with pytest.raises(ModelError) as error:
            await CloudChatModel(client, CONFIG, "test-secret").complete([], [])
    assert str(status) in str(error.value)
    assert "test-secret" not in str(error.value)
    assert "private body" not in str(error.value)


@pytest.mark.parametrize(
    "message",
    [
        None,
        {"content": []},
        {"content": None},
        {
            "tool_calls": [
                {"id": "same", "type": "function", "function": {"name": "x", "arguments": "{}"}}
            ]
            * 2
        },
    ],
)
async def test_malformed_reply_is_model_error(message):
    """不合规响应统一转为模型异常。"""
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"choices": [{"message": message}]})
        )
    ) as client:
        with pytest.raises(ModelError):
            await CloudChatModel(client, CONFIG, "key").complete([], [])
