"""验证按日运行日志、诊断历史兼容和倒序读取性能边界。"""

import json
import logging
from datetime import datetime

from stock_research.diagnostics import read_diagnostics
from stock_research.logging_setup import BeijingFormatter, DailyFileHandler


def test_runtime_log_changes_at_beijing_midnight_and_appends_after_restart(tmp_path):
    """午夜前后分到不同文件，重新创建处理器也不会覆盖当天内容。"""
    formatter = BeijingFormatter("%(asctime)s %(message)s")
    handler = DailyFileHandler(tmp_path)
    handler.setFormatter(formatter)
    for timestamp, text in [
        ("2026-10-11T15:59:59+00:00", "午夜之前"),
        ("2026-10-11T16:00:00+00:00", "午夜之后"),
    ]:
        record = logging.makeLogRecord(
            {"msg": text, "created": datetime.fromisoformat(timestamp).timestamp()}
        )
        handler.handle(record)
    handler.close()
    restarted = DailyFileHandler(tmp_path)
    restarted.setFormatter(formatter)
    restarted.handle(
        logging.makeLogRecord(
            {
                "msg": "重启之后",
                "created": datetime.fromisoformat("2026-10-12T04:00:00+00:00").timestamp(),
            }
        )
    )
    restarted.close()
    first = (tmp_path / "research-2026-10-11.log").read_text(encoding="utf-8")
    second = (tmp_path / "research-2026-10-12.log").read_text(encoding="utf-8")
    assert "2026-10-11 23:59:59" in first and "午夜之前" in first
    assert "午夜之后" not in first
    assert "2026-10-12 00:00:00" in second
    assert "午夜之后" in second and "重启之后" in second


def test_daily_and_legacy_diagnostics_read_together(tmp_path):
    """新旧目录的记录都可查询，保留旧文件，重复诊断编号只返回一次。"""
    logs = tmp_path / "logs"
    legacy = tmp_path / "data" / "diagnostics"
    logs.mkdir()
    legacy.mkdir(parents=True)
    older = {
        "diagnostic_id": "old-1",
        "created_at": "2026-10-10T18:00:00+00:00",
        "outcome": "failed",
    }
    later = {
        "diagnostic_id": "old-2",
        "created_at": "2026-10-11T18:00:00+00:00",
        "outcome": "failed",
    }
    newest = {
        "diagnostic_id": "new-1",
        "created_at": "2026-10-12T04:00:00+00:00",
        "outcome": "failed",
    }
    (legacy / "requests.jsonl.1").write_text(json.dumps(older) + "\n", encoding="utf-8")
    (legacy / "requests.jsonl").write_text(json.dumps(later) + "\n", encoding="utf-8")
    (logs / "requests-2026-10-12.jsonl").write_text(
        json.dumps(later) + "\n" + json.dumps(newest) + "\n", encoding="utf-8"
    )
    records = read_diagnostics(logs, legacy_directory=legacy)
    assert [record["diagnostic_id"] for record in records] == ["new-1", "old-2", "old-1"]
    assert read_diagnostics(logs, day="2026-10-11", legacy_directory=legacy) == [older]
    assert (legacy / "requests.jsonl").is_file()


def test_reverse_read_preserves_long_utf8_lines(tmp_path):
    """一条中文记录跨越多个读取块，仍能完整解析并保持新记录在前。"""
    older = {"diagnostic_id": "old", "outcome": "failed", "message": "中文资料" * 50000}
    newer = {"diagnostic_id": "new", "outcome": "failed", "message": "最新记录"}
    (tmp_path / "requests-2026-10-12.jsonl").write_text(
        json.dumps(older, ensure_ascii=False) + "\n" + json.dumps(newer, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    assert read_diagnostics(tmp_path, limit=2) == [newer, older]


def test_recent_query_stops_before_older_files(tmp_path, monkeypatch):
    """已找到足够的新记录时不扫描旧的大文件。"""
    from stock_research import diagnostics

    for day in ["2026-10-11", "2026-10-12"]:
        (tmp_path / f"requests-{day}.jsonl").write_text(
            json.dumps({"diagnostic_id": day, "outcome": "failed"}) + "\n", encoding="utf-8"
        )
    original = diagnostics._reverse_lines
    opened = []

    def observe(path):
        """记录实际打开的文件，仍使用真实倒序解析。"""
        opened.append(path.name)
        yield from original(path)

    monkeypatch.setattr(diagnostics, "_reverse_lines", observe)
    assert len(read_diagnostics(tmp_path, limit=1)) == 1
    assert opened == ["requests-2026-10-12.jsonl"]
