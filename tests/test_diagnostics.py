"""验证故障记录的关联、脱敏、取消语义、轮转和读取入口。"""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from stock_research.app import create_app
from stock_research.config import Settings
from stock_research.diagnostics import DiagnosticRecorder, diagnostic_context, read_diagnostics
from stock_research.logging_setup import log_day
from stock_research.model_client import CloudChatModel, ModelError

CONFIG = {
    "id": "model-1",
    "base_url": "https://model.example/v1",
    "model": "test-model",
    "max_tokens": 8192,
}


async def test_provider_error_is_actionable_but_redacted(tmp_path, caplog):
    """保存状态、正文、追踪号及堆栈，同时清除服务商回显和嵌套字符串中的密钥。"""
    recorder = DiagnosticRecorder(tmp_path)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            400,
            json={
                "code": 20012,
                "message": "max_tokens is unsupported; test-secret",
                "api_key": "test-secret",
                "other": {"access_token": "another-private-token"},
            },
            headers={"x-siliconcloud-trace-id": "provider-trace-1", "set-cookie": "session-secret"},
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        model = CloudChatModel(client, CONFIG, "test-secret", recorder)
        with diagnostic_context(request_id="page-1", task_id="task-1", round=2):
            with pytest.raises(ModelError) as error:
                await model.complete(
                    [
                        {"role": "user", "content": "研究问题"},
                        {
                            "role": "assistant",
                            "reasoning_content": "private-reasoning-text",
                            "content": None,
                            "tool_calls": [
                                {"function": {"arguments": '{"api_key":"nested-private-token"}'}}
                            ],
                        },
                    ],
                    [],
                )
    record = read_diagnostics(tmp_path)[0]
    assert record["diagnostic_id"] == error.value.diagnostic_id
    assert record["http_status"] == 400
    assert record["provider_error"]["code"] == 20012
    assert "max_tokens" in record["provider_error"]["message"]
    assert record["provider_headers"] == {"x-siliconcloud-trace-id": "provider-trace-1"}
    assert record["request_id"] == "page-1"
    assert record["task_id"] == "task-1"
    assert record["round"] == 2
    assert "HTTPStatusError" in record["traceback"]
    assert record["duration_ms"] >= 0
    saved = recorder.path.read_text(encoding="utf-8") + caplog.text
    for secret in [
        "test-secret",
        "another-private-token",
        "session-secret",
        "private-reasoning-text",
        "nested-private-token",
    ]:
        assert secret not in saved


async def test_success_records_usage_without_copying_message_text(tmp_path):
    """成功请求保留用量与耗时，不反复复制研究正文。"""
    recorder = DiagnosticRecorder(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "报告"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 20},
                },
            )
        )
    ) as client:
        await CloudChatModel(client, CONFIG, "test-secret", recorder).complete(
            [{"role": "user", "content": "private-research-content"}], []
        )
    assert read_diagnostics(tmp_path) == []
    record = read_diagnostics(tmp_path, failed_only=False)[0]
    assert record["usage"]["completion_tokens"] == 20
    assert record["outcome"] == "succeeded"
    assert "private-research-content" not in recorder.path.read_text(encoding="utf-8")


async def test_protocol_failure_and_plain_text_error_body_preserved(tmp_path):
    """HTTP 200 的协议错误与非 JSON 错误响应也有可定位的诊断记录。"""
    recorder = DiagnosticRecorder(tmp_path)
    responses = [
        httpx.Response(200, json={"wrong_field": True}),
        httpx.Response(503, text="upstream unavailable Bearer test-secret"),
    ]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: responses.pop(0))
    ) as client:
        model = CloudChatModel(client, CONFIG, "test-secret", recorder)
        for _ in range(2):
            with pytest.raises(ModelError):
                await model.complete([], [])
    records = read_diagnostics(tmp_path)
    assert records[0]["http_status"] == 503
    assert "upstream unavailable" in records[0]["provider_error"]
    assert records[1]["http_status"] == 200
    assert records[1]["provider_error"] == {"wrong_field": True}
    assert "test-secret" not in recorder.path.read_text(encoding="utf-8")


async def test_length_failure_preserves_usage_and_reasoning_length_without_content(tmp_path):
    """输出预算用尽且正文为空时，用量和推理字符数仍能用于排查。"""
    recorder = DiagnosticRecorder(tmp_path)
    thought = "private-model-reasoning" * 100
    body = {
        "choices": [
            {
                "finish_reason": "length",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": thought,
                },
            }
        ],
        "usage": {"prompt_tokens": 35649, "completion_tokens": 8192},
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    ) as client:
        with pytest.raises(ModelError, match="尚未返回正文"):
            await CloudChatModel(client, CONFIG, "test-secret", recorder).complete([], [])
    record = read_diagnostics(tmp_path)[0]
    assert record["usage"]["completion_tokens"] == 8192
    assert record["response_summary"]["finish_reason"] == "length"
    assert record["response_summary"]["content_characters"] == 0
    assert record["response_summary"]["reasoning_characters"] == len(thought)
    assert thought not in recorder.path.read_text(encoding="utf-8")


async def test_cancellation_is_recorded_and_propagates(tmp_path):
    """异步取消记录为 cancelled，不能误记为云端失败。"""
    recorder = DiagnosticRecorder(tmp_path)
    started = asyncio.Event()

    async def pending(request):
        """等待上层取消模拟请求。"""
        started.set()
        await asyncio.Event().wait()

    async with httpx.AsyncClient(transport=httpx.MockTransport(pending)) as client:
        task = asyncio.create_task(
            CloudChatModel(client, CONFIG, "test-secret", recorder).complete([], [])
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert read_diagnostics(tmp_path) == []
    assert read_diagnostics(tmp_path, failed_only=False)[0]["outcome"] == "cancelled"


async def test_parallel_contexts_do_not_mix(tmp_path):
    """两个任务同时失败仍关联各自的请求与任务编号。"""
    recorder = DiagnosticRecorder(tmp_path)

    async def fail(request):
        """让两个请求在不同协程内同时等待一次。"""
        await asyncio.sleep(0.01)
        return httpx.Response(429, json={"error": "rate limited"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:

        async def invoke(index):
            """绑定独立上下文并触发可诊断错误。"""
            with diagnostic_context(request_id=f"request-{index}", task_id=f"task-{index}"):
                with pytest.raises(ModelError):
                    await CloudChatModel(client, CONFIG, "test-secret", recorder).complete([], [])

        await asyncio.gather(invoke(1), invoke(2))
    records = read_diagnostics(tmp_path)
    assert {(record["request_id"], record["task_id"]) for record in records} == {
        ("request-1", "task-1"),
        ("request-2", "task-2"),
    }
    with diagnostic_context(request_id="request-3"):
        recorder.record("test", "failed")
    assert "task_id" not in read_diagnostics(tmp_path)[0]


def test_daily_files_filtering_and_partial_record_recovery(tmp_path, monkeypatch, caplog):
    """跨北京时间午夜自动换文件，日期筛选和损坏末行不影响历史读取。"""
    timestamps = iter(
        [
            "2026-10-10T00:00:00+00:00",
            "2026-10-10T15:59:59+00:00",
            "2026-10-10T16:00:00+00:00",
            "2026-10-11T15:59:59+00:00",
            "2026-10-11T16:00:00+00:00",
            "2026-10-12T00:00:00+00:00",
        ]
    )
    monkeypatch.setattr("stock_research.diagnostics.now", lambda: next(timestamps))
    recorder = DiagnosticRecorder(tmp_path)
    for index in range(6):
        recorder.record("test", "failed", task_id=f"task-{index}", request_id=f"request-{index}")
    assert len(list(tmp_path.glob("requests-*.jsonl"))) == 3
    assert [record["task_id"] for record in read_diagnostics(tmp_path)] == [
        "task-5",
        "task-4",
        "task-3",
        "task-2",
        "task-1",
        "task-0",
    ]
    assert len(read_diagnostics(tmp_path, task_id="task-4")) == 1
    assert len(read_diagnostics(tmp_path, request_id="request-3")) == 1
    assert [record["task_id"] for record in read_diagnostics(tmp_path, day="2026-10-11")] == [
        "task-3",
        "task-2",
    ]
    with (tmp_path / "requests-2026-10-12.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"unfinished":')
    assert len(read_diagnostics(tmp_path)) == 6
    assert "损坏" in caplog.text


def test_write_failure_falls_back_without_hiding_error(tmp_path, caplog):
    """诊断写入失败明确记录，并将脱敏记录回退到应用日志。"""
    occupied = tmp_path / "occupied"
    occupied.write_text("file", encoding="utf-8")
    recorder = DiagnosticRecorder(occupied)
    recorder.add_secret("test-secret")
    identifier = recorder.record("test", "failed", message="test-secret failed")
    assert identifier in caplog.text
    assert "诊断文件写入失败" in caplog.text
    assert "诊断记录回退" in caplog.text
    assert "test-secret" not in caplog.text


def test_api_failure_links_to_saved_record(tmp_path):
    """页面收到的诊断编号能直接在本机接口和 JSONL 中找到。"""
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        response = client.post("/api/tasks", json={"message": "测试公司", "model_id": "missing"})
        assert response.status_code == 404
        request_id = response.headers["X-Request-ID"]
        assert response.json()["request_id"] == request_id
        records = client.get(f"/api/diagnostics?request_id={request_id}").json()
        assert len(records) == 1
        assert records[0]["diagnostic_id"] == response.json()["diagnostic_id"]
        assert records[0]["http_status"] == 404
        assert records[0]["api_path"] == "/api/tasks"
        day = log_day(records[0]["created_at"])
        assert client.get(f"/api/diagnostics?request_id={request_id}&date={day}").json() == records
        assert client.get(f"/api/diagnostics?request_id={request_id}&date=2000-01-01").json() == []
        assert client.get("/api/diagnostics?date=invalid").status_code == 422
    assert list((tmp_path / "logs").glob("requests-*.jsonl"))


def test_validation_diagnostics_do_not_echo_key(tmp_path):
    """配置输入校验失败也能诊断，同时不能保存原始密钥输入。"""
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        response = client.post("/api/models", json={"api_key": "unregistered-private-key"})
        assert response.status_code == 422
        assert response.json()["diagnostic_id"]
        assert "unregistered-private-key" not in client.get("/api/diagnostics").text
    assert "unregistered-private-key" not in "".join(
        file.read_text(encoding="utf-8") for file in (tmp_path / "logs").glob("requests-*.jsonl")
    )


async def test_timeout_records_exception_chain_without_response(tmp_path):
    """超时没有 HTTP 响应，仍保留异常链、地址及诊断编号。"""
    recorder = DiagnosticRecorder(tmp_path)

    async def timeout(request):
        """模拟等待响应超时。"""
        raise httpx.ReadTimeout("gateway timeout", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
        with pytest.raises(ModelError) as error:
            await CloudChatModel(client, CONFIG, "test-secret", recorder).complete([], [])
    record = read_diagnostics(tmp_path)[0]
    assert record["http_status"] is None
    assert "ReadTimeout" in record["traceback"]
    assert record["endpoint"] == "https://model.example/v1/chat/completions"
    assert record["diagnostic_id"] == error.value.diagnostic_id


async def test_connection_error_and_background_task_link_to_cloud_record(tmp_path):
    """连接测试及后台研究的失败编号，均能对应真实云端交换诊断。"""
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                401,
                json={"code": "invalid_key", "message": "test-secret denied"},
                headers={"x-request-id": "provider-1"},
            )
        )
    ) as cloud:
        with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
            client.app.state.runtime._model_factory = lambda config: CloudChatModel(
                cloud, config, "test-secret", client.app.state.diagnostics
            )
            model = client.post(
                "/api/models",
                json={"name": "测试模型", **CONFIG, "api_key": "test-secret", "max_steps": 3},
            ).json()
            response = client.post(f"/api/models/{model['id']}/test")
            assert response.status_code == 502
            assert "test-secret" not in response.text
            records = client.get(
                f"/api/diagnostics?request_id={response.headers['X-Request-ID']}"
            ).json()
            assert records[0]["diagnostic_id"] == response.json()["diagnostic_id"]
            assert records[0]["provider_error"]["code"] == "invalid_key"
            assert records[0]["http_status"] == 401
            created = client.post(
                "/api/tasks", json={"message": "测试公司", "model_id": model["id"]}
            )
            task_id = created.json()["id"]

            async def wait_task():
                """等待运行时持有的研究协程落盘。"""
                task = client.app.state.runtime._tasks.get(task_id)
                if task:
                    await task

            client.portal.call(wait_task)
            detail = client.get(f"/api/tasks/{task_id}").json()
            assert detail["task"]["status"] == "failed"
            record = client.get(f"/api/diagnostics?task_id={task_id}").json()[0]
            assert record["request_id"] == created.headers["X-Request-ID"]
            assert record["round"] == 1
            assert record["model_id"] == model["id"]
            assert record["diagnostic_id"] in detail["task"]["error"]
            assert detail["events"][-1]["data"]["diagnostic_id"] == record["diagnostic_id"]


def test_unexpected_api_exception_is_safe_and_diagnosable(tmp_path):
    """内部异常统一返回诊断编号，脱敏堆栈保存在本机。"""

    class ExplodingModel:
        """模拟连接测试中的未预期内部异常。"""

        async def complete(self, messages, tools):
            """抛出包含敏感 token 的异常，以验证兜底过滤。"""
            raise RuntimeError("unexpected failure sk-privatecredential123456")

    with TestClient(
        create_app(Settings(data_dir=tmp_path), lambda config: ExplodingModel())
    ) as client:
        model = client.post(
            "/api/models", json={"name": "测试模型", **CONFIG, "api_key": "test-secret"}
        ).json()
        response = client.post(f"/api/models/{model['id']}/test")
        assert response.status_code == 500
        records = client.get(
            f"/api/diagnostics?request_id={response.headers['X-Request-ID']}"
        ).json()
        assert records[0]["diagnostic_id"] == response.json()["diagnostic_id"]
        assert records[0]["exception_type"] == "RuntimeError"
        assert records[0]["http_status"] == 500
        assert "sk-privatecredential123456" not in json.dumps(records)
