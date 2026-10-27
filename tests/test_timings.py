"""验证分阶段计时、并发关联、取消传播和代理响应的时间归属。"""

import asyncio

import pytest

from stock_research.diagnostics import DiagnosticRecorder, diagnostic_context, read_diagnostics
from stock_research.timings import ACTIVE_SPAN, RequestClock, main, measure_stage, measured_lock


async def test_nested_and_parallel_timings_keep_task_and_parent_associations(
    tmp_path, monkeypatch, capsys
):
    """并发任务各有父子编号，终端展示能对应同一任务的完整阶段。"""
    recorder = DiagnosticRecorder(tmp_path)

    async def run(task_id):
        """让两个任务交错执行，防止仅串行时关联正确。"""
        with diagnostic_context(task_id=task_id):
            async with measure_stage(recorder, "task", "任务"):
                async with measure_stage(recorder, "rag", "历史检索") as details:
                    await asyncio.sleep(0)
                    details["hit_count"] = 3

    await asyncio.gather(run("a"), run("b"))
    assert ACTIVE_SPAN.get() is None
    records = read_diagnostics(tmp_path, failed_only=False)
    for task_id in ("a", "b"):
        completed = [
            r for r in records if r.get("task_id") == task_id and r.get("status") == "succeeded"
        ]
        parent = next(r for r in completed if r["stage"] == "timing.task")
        child = next(r for r in completed if r["stage"] == "timing.rag")
        assert child["parent_span_id"] == parent["span_id"]
        assert parent["duration_ms"] >= child["duration_ms"] >= 0
        assert child["hit_count"] == 3
    monkeypatch.setenv("STOCK_LOG_DIR", str(tmp_path))
    monkeypatch.setattr("sys.argv", ["timings", "--task-id", "a"])
    main()
    output = capsys.readouterr().out
    assert "任务：a" in output and "  历史检索：" in output
    assert "不重复相加" in output


async def test_failure_and_cancellation_are_recorded_without_swallowing(tmp_path):
    """失败保留结束耗时，取消锁等待不泄漏锁，也不误标成功。"""
    recorder = DiagnosticRecorder(tmp_path)
    with pytest.raises(ValueError, match="模拟失败"):
        async with measure_stage(recorder, "test", "失败阶段"):
            raise ValueError("模拟失败")
    lock = asyncio.Lock()
    await lock.acquire()

    async def wait():
        """取消发生在取得锁之前，不得释放另一个调用持有的锁。"""
        async with measured_lock(lock, recorder, "queue", "等待锁"):
            raise AssertionError("不应取得锁")

    task = asyncio.create_task(wait())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert lock.locked()
    lock.release()
    async with measured_lock(lock, recorder, "queue", "重新取得锁"):
        assert lock.locked()
    assert not lock.locked()
    records = read_diagnostics(tmp_path, failed_only=False)
    assert any(r.get("status") == "cancelled" and "duration_ms" in r for r in records)
    assert any(r.get("status") == "failed" and r["exception_type"] == "ValueError" for r in records)
    assert ACTIVE_SPAN.get() is None


async def test_http_timings_ignore_connect_response_and_preserve_timeout_phase(monkeypatch):
    """代理 CONNECT 成功不能被误记为模型返回；无响应时定位到等待响应头。"""
    current = [0.0]
    monkeypatch.setattr("stock_research.timings.perf_counter", lambda: current[0])
    clock = RequestClock()
    events = [
        (0.0, "connection.connect_tcp.started"),
        (0.1, "connection.connect_tcp.complete"),
        (0.15, "http11.send_request_body.complete"),
        (0.2, "http11.receive_response_headers.complete"),
        (0.2, "proxy.start_tls.started"),
        (0.3, "proxy.start_tls.complete"),
        (0.4, "http11.send_request_body.complete"),
        (15.8, "http11.receive_response_headers.failed"),
    ]
    for current[0], event in events:
        await clock.trace(event, {"secret": "不应收集的正文和密钥"})
    assert clock.values["connect_ms"] == 100
    assert clock.values["tls_ms"] == 100
    assert clock.values["response_wait_ms"] == 15400
    assert clock.values["failed_phase"] == "http11.receive_response_headers"
    assert "headers_received_ms" not in clock.values and "secret" not in clock.values
