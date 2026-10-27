"""验证自主工具循环、可恢复失败及模型请求取消。"""

import asyncio
import json

import httpx
import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from stock_research.config import CredentialVault, Settings
from stock_research.model_client import ModelError
from stock_research.research import ResearchRuntime
from stock_research.storage import Repository

HTML = (
    "<html><head><title>自动化测试报告</title></head><body><article><h1>测试报告</h1>"
    "<p>此处为测试用的公司业务描述与财务数据，不代表真实公司。"
    "本期收入120万元，上期收入100万元，金额单位均为人民币万元。</p>"
    "<p>报告数据仅供自动化测试使用，主营业务是测试设备生产，"
    "本期经营现金流净额30万元，请结合证据计算收入同比变化。</p></article></body></html>"
).encode()


class ScriptedModel:
    """模拟模型动态选择读取与计算，保存看到的协议消息。"""

    def __init__(self):
        """初始化请求记录。"""
        self.requests = []

    async def complete(self, messages, tools):
        """先取原文与错误计算，再根据错误反馈修正并报告。"""
        self.requests.append(messages)
        round_number = len(self.requests)
        if round_number == 1:
            return {
                "role": "assistant",
                "content": "先核实原文。",
                "tool_calls": [
                    tool_call("read-1", "read_source", {"url": "https://company.example/report"}),
                    tool_call("bad-calc", "calculate", {"expression": "1/0"}),
                ],
            }
        if round_number == 2:
            assert messages[-1]["tool_call_id"] == "bad-calc"
            assert json.loads(messages[-1]["content"])["recoverable"]
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    tool_call("calc-2", "calculate", {"expression": "(120-100)/100*100"})
                ],
            }
        assert messages[-1]["tool_call_id"] == "calc-2"
        assert json.loads(messages[-1]["content"])["result"] == "20.0"
        evidence = json.loads(messages[3]["content"])
        return {
            "role": "assistant",
            "content": f"测试报告：收入增长20%，来源 {evidence['source_id']}。",
        }


async def test_research_reads_calculates_and_reports(tmp_path, monkeypatch):
    """真实执行图、原文解析、归档与计算，测试模型只替代云端推理。"""

    async def allow(url):
        """MockTransport 隔离网络，测试域名不执行 DNS。"""

    monkeypatch.setattr("stock_research.tools.validate_public_url", allow)
    repository = Repository(tmp_path)
    await repository.initialize()
    model = ScriptedModel()
    identifier = await repository.create_task(
        "测试公司",
        "收入变化",
        {"id": "test", "max_steps": 4, "api_key_encrypted": "not-a-real-key"},
    )
    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, content=HTML, headers={"content-type": "text/html"}
                )
            )
        ) as client,
        AsyncSqliteSaver.from_conn_string(str(tmp_path / "graph.sqlite")) as checkpoint,
    ):
        await checkpoint.setup()
        runtime = ResearchRuntime(
            Settings(data_dir=tmp_path),
            repository,
            CredentialVault(tmp_path),
            client,
            checkpoint,
            lambda config: model,
        )
        runtime.submit(identifier)
        await runtime._tasks[identifier]
    task = await repository.get_task(identifier)
    assert task["status"] == "completed"
    source = (await repository.sources(identifier))[0]
    assert source["id"] in task["report"]
    assert "20%" in task["report"]
    events = await repository.events(identifier)
    assert sum(event["kind"] == "tool_finished" for event in events) == 2
    assert sum(event["kind"] == "tool_error" for event in events) == 1


class FailingModel:
    """模拟不可恢复模型错误。"""

    async def complete(self, messages, tools):
        """通过受控错误停止任务。"""
        raise ModelError("HTTP 401：API 密钥无效或未授权")


async def test_model_failure_persisted(tmp_path):
    """不可恢复异常应记录失败状态和原因，不能静默退出。"""
    repository = Repository(tmp_path)
    await repository.initialize()
    identifier = await repository.create_task("测试公司", "", {"id": "test", "max_steps": 3})
    async with (
        httpx.AsyncClient() as client,
        AsyncSqliteSaver.from_conn_string(str(tmp_path / "graph.sqlite")) as checkpoint,
    ):
        await checkpoint.setup()
        runtime = ResearchRuntime(
            Settings(data_dir=tmp_path),
            repository,
            CredentialVault(tmp_path),
            client,
            checkpoint,
            lambda config: FailingModel(),
        )
        runtime.submit(identifier)
        await runtime._tasks[identifier]
    task = await repository.get_task(identifier)
    assert task["status"] == "failed"
    assert "401" in task["error"]
    assert (await repository.events(identifier))[-1]["kind"] == "failed"


async def test_cancelled_model_request_propagates():
    """取消等待时必须传播 CancelledError，而不是返回业务成功或失败消息。"""
    from stock_research.model_client import CloudChatModel

    started = asyncio.Event()

    async def pending(request):
        """一直等待，直到上层取消请求。"""
        started.set()
        await asyncio.Event().wait()

    async with httpx.AsyncClient(transport=httpx.MockTransport(pending)) as client:
        model = CloudChatModel(
            client,
            {"base_url": "https://model.example", "model": "test", "max_tokens": 4096},
            "key",
        )
        task = asyncio.create_task(model.complete([], []))
        await started.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            assert task.cancelled()
        else:
            raise AssertionError("取消异常未向上传递")


def tool_call(identifier, name, arguments):
    """生成符合云端协议的模型工具请求。"""
    return {
        "id": identifier,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


@pytest.mark.parametrize("corrected", [True, False])
@pytest.mark.parametrize("structured", [True, False])
async def test_final_tool_request_is_corrected_or_partial(
    tmp_path, monkeypatch, corrected, structured
):
    """复现末轮工具请求：最多纠正一次，不能执行禁用工具或误报完成。"""

    async def allow(url):
        """隔离测试域名的 DNS 查询。"""

    class FinalToolModel(ScriptedModel):
        """先正常取证与计算，收尾时模拟服务商返回未执行的工具请求。"""

        async def complete(self, messages, tools):
            """纠正成功时给出报告，否则重复错误输出。"""
            if len(self.requests) < 2:
                return await super().complete(messages, tools)
            self.requests.append(messages)
            assert not tools
            if len(self.requests) == 4 and corrected:
                assert "上一条输出不是完整报告" in messages[-1]["content"]
                return {"role": "assistant", "content": "完整测试报告：收入增长20%，仍需关注风险。"}
            if structured:
                return {
                    "role": "assistant",
                    "content": "尚未完成的报告。",
                    "tool_calls": [tool_call("not-allowed", "calculate", {"expression": "7+8"})],
                }
            return {
                "role": "assistant",
                "content": (
                    "报告草稿\n<tool_call>calculate<arg_key>expression</arg_key>"
                    '="7+8"</arg_value></tool_call>'
                ),
            }

    monkeypatch.setattr("stock_research.tools.validate_public_url", allow)
    repository = Repository(tmp_path)
    await repository.initialize()
    identifier = await repository.create_task("测试公司", "", {"id": "test", "max_steps": 3})
    model = FinalToolModel()
    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, content=HTML, headers={"content-type": "text/html"}
                )
            )
        ) as client,
        AsyncSqliteSaver.from_conn_string(str(tmp_path / "graph.sqlite")) as checkpoint,
    ):
        await checkpoint.setup()
        runtime = ResearchRuntime(
            Settings(data_dir=tmp_path),
            repository,
            CredentialVault(tmp_path),
            client,
            checkpoint,
            lambda config: model,
        )
        runtime.submit(identifier)
        await runtime._tasks[identifier]
    task = await repository.get_task(identifier)
    assert task["status"] == ("completed" if corrected else "partial")
    assert "<tool_call>" not in task["report"]
    assert "完整测试报告" in task["report"] if corrected else "本次报告未完成" in task["report"]
    assert len(model.requests) == 4
    events = await repository.events(identifier)
    assert not any(
        event["kind"] == "tool_started"
        and event["data"].get("arguments", {}).get("expression") == "7+8"
        for event in events
    )
