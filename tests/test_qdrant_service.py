"""验证服务部署包校验、向量迁移与每日独立日志，测试不接触正式库。"""

import hashlib
import io
import logging
import zipfile
from datetime import datetime

import pytest
from qdrant_client import QdrantClient, models

from stock_research.logging_setup import LOG_TIMEZONE, DailyFileHandler
from stock_research.qdrant_migration import migrate_collections
from stock_research.qdrant_service import extract_package, write_config


@pytest.fixture
def vector_clients(tmp_path):
    """两个独立向量库替代服务，保留SDK真实的持久化和向量转换。"""
    source = QdrantClient(path=str(tmp_path / "source"))
    target = QdrantClient(path=str(tmp_path / "target"))
    source.create_collection(
        "history_test", models.VectorParams(size=3, distance=models.Distance.COSINE)
    )
    source.upsert(
        "history_test",
        [
            models.PointStruct(
                id=1,
                vector=[1.0, 2.0, 3.0],
                payload={"task_id": "old", "content": "历史提问"},
            )
        ],
        wait=True,
    )
    try:
        yield source, target
    finally:
        source.close()
        target.close()


def test_migration_preserves_vectors_metadata_and_supports_retry(vector_clients):
    """复制后逐点验证内容、维度和数量，重复迁移不会新增重复记录。"""
    source, target = vector_clients
    report = migrate_collections(source, target, payload_index=False)
    assert report == [{"name": "history_test", "points": 1, "dimensions": 3}]
    assert migrate_collections(source, target, payload_index=False) == report
    assert source.count("history_test").count == target.count("history_test").count == 1
    assert target.retrieve("history_test", [1])[0].payload["content"] == "历史提问"


def test_migration_refuses_overwriting_changed_target(vector_clients):
    """目标已经有不同内容时停下，保留双方记录而不覆盖新数据。"""
    source, target = vector_clients
    migrate_collections(source, target, payload_index=False)
    target.set_payload("history_test", {"content": "独立服务更新后的原文"}, [1], wait=True)
    with pytest.raises(ValueError, match="冲突"):
        migrate_collections(source, target, payload_index=False)
    assert target.retrieve("history_test", [1])[0].payload["content"] == "独立服务更新后的原文"
    assert source.retrieve("history_test", [1])[0].payload["content"] == "历史提问"


def test_official_package_digest_and_extraction_paths(tmp_path):
    """校验失败和越界路径不会写入程序目录，合法包可安装。"""

    def package(name):
        """生成很小的离线发布包替身。"""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(name, "测试资源")
        return buffer.getvalue()

    data = package("dist/index.html")
    extract_package(data, hashlib.sha256(data).hexdigest(), tmp_path / "static")
    assert (tmp_path / "static/dist/index.html").read_text(encoding="utf-8") == "测试资源"
    with pytest.raises(ValueError, match="SHA-256"):
        extract_package(data, "incorrect", tmp_path / "wrong")
    outside = package("../outside.txt")
    with pytest.raises(ValueError, match="越界"):
        extract_package(outside, hashlib.sha256(outside).hexdigest(), tmp_path / "wrong")
    assert not (tmp_path / "outside.txt").exists()


def test_standalone_config_and_logs_are_local_and_daily(tmp_path):
    """独立服务只监听本机，并按跨日的记录时间拆分日志。"""
    write_config(tmp_path)
    text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert "host: 127.0.0.1" in text and "static_content_dir:" in text
    handler = DailyFileHandler(tmp_path, prefix="qdrant")
    for hour, day in ((23, 14), (0, 15)):
        record = logging.LogRecord("qdrant", logging.INFO, "", 0, "服务状态", (), None)
        record.created = datetime(2026, 10, day, hour, tzinfo=LOG_TIMEZONE).timestamp()
        handler.emit(record)
    handler.close()
    assert (tmp_path / "qdrant-2026-10-14.log").read_text(encoding="utf-8").strip() == "服务状态"
    assert (tmp_path / "qdrant-2026-10-15.log").read_text(encoding="utf-8").strip() == "服务状态"
