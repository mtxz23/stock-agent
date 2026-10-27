"""将嵌入式历史向量迁移到本机Qdrant服务，不重新调用embedding模型。"""

import json
import logging
import math
from datetime import UTC, datetime
from pathlib import Path

from qdrant_client import QdrantClient, models

from stock_research.config import Settings
from stock_research.logging_setup import BeijingFormatter, DailyFileHandler
from stock_research.qdrant_service import LOCAL_URL

logger = logging.getLogger(__name__)


def main() -> None:
    """首次切换复制并核对全部历史集合，完成后记录标记避免覆盖新数据。"""
    settings = Settings()
    handler = DailyFileHandler(settings.log_dir, prefix="qdrant")
    handler.setFormatter(BeijingFormatter("%(asctime)s %(levelname)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        report = migrate(settings.data_dir)
        print("历史向量迁移已就绪，集合数=" + str(len(report["collections"])))
    except Exception:
        logger.exception("历史向量迁移失败，尚未切换业务连接")
        raise


def migrate(data_dir: Path) -> dict:
    """只有首次执行才读取旧库，成功标记以后不重复导入旧的向量快照。"""
    marker = data_dir / "qdrant" / "migration.json"
    if marker.exists():
        report = json.loads(marker.read_text(encoding="utf-8"))
        if report["server_url"] != LOCAL_URL:
            raise ValueError("历史向量迁移标记的目标地址不一致")
        logger.info("历史向量此前已迁移，不重复覆盖独立服务中的新数据")
        return report
    source_path = data_dir / "vectors"
    target = QdrantClient(url=LOCAL_URL, trust_env=False, timeout=30)
    try:
        if (source_path / "meta.json").exists():
            # 本地库会锁文件；迁移前应关闭后端，禁止边编码边搬迁。
            source = QdrantClient(path=str(source_path))
            try:
                collections = migrate_collections(source, target)
            finally:
                source.close()
        else:
            collections = []
    finally:
        target.close()
    report = {
        "server_url": LOCAL_URL,
        "source": str(source_path.resolve()),
        "collections": collections,
        "completed_at": datetime.now(UTC).isoformat(),
    }
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("历史向量迁移完成，集合数=%s；旧库仍保留在原目录", len(collections))
    return report


def migrate_collections(
    source: QdrantClient,
    target: QdrantClient,
    *,
    payload_index: bool = True,
) -> list[dict]:
    """逐批复制历史点，拒绝覆盖冲突数据，逐点核对向量及原文元数据。"""
    result = []
    for collection in source.get_collections().collections:
        name = collection.name
        if not name.startswith("history_"):
            continue
        vectors = source.get_collection(name).config.params.vectors
        if not isinstance(vectors, models.VectorParams):
            raise ValueError("历史集合的向量结构不符合单向量索引约定")
        if target.collection_exists(name):
            existing = target.get_collection(name).config.params.vectors
            if not isinstance(existing, models.VectorParams) or (
                existing.size != vectors.size or existing.distance != vectors.distance
            ):
                raise ValueError("目标历史集合的向量维度或距离算法不一致")
        else:
            target.create_collection(name, vectors_config=vectors)
        if payload_index:
            target.create_payload_index(
                name, "task_id", models.PayloadSchemaType.KEYWORD, wait=True
            )
        count = source.count(name, exact=True).count
        offset = None
        copied = 0
        while True:
            points, offset = source.scroll(
                name, limit=64, offset=offset, with_vectors=True, with_payload=True
            )
            if points:
                # 允许失败后重试同批次，禁止覆写已被独立服务修改的同编号记录。
                previous = target.retrieve(
                    name, [point.id for point in points], with_vectors=True, with_payload=True
                )
                expected = {str(point.id): point for point in points}
                verify_points(expected, previous)
                target.upsert(
                    name,
                    [
                        models.PointStruct(
                            id=point.id,
                            vector=point.vector,
                            payload=point.payload,
                        )
                        for point in points
                    ],
                    wait=True,
                )
                copied_points = target.retrieve(
                    name, [point.id for point in points], with_vectors=True, with_payload=True
                )
                if len(copied_points) != len(points):
                    raise ValueError("目标向量记录数量不完整")
                verify_points(expected, copied_points)
                copied += len(points)
            if offset is None:
                break
        if copied != count or target.count(name, exact=True).count != count:
            raise ValueError("源与目标向量集合的记录数量不一致，迁移未完成")
        result.append({"name": name, "points": count, "dimensions": vectors.size})
        logger.info("向量集合已迁移并核对，集合=%s，记录数=%s", name, count)
    return result


def verify_points(expected: dict, points: list[models.Record]) -> None:
    """余弦向量可能重新归一化，允许浮点误差；业务元数据必须完全相同。"""
    for actual in points:
        source = expected[str(actual.id)]
        if actual.payload != source.payload or len(actual.vector) != len(source.vector):
            raise ValueError("目标集合存在冲突的历史记录，迁移已停止")
        if any(
            not math.isclose(left, right, rel_tol=1e-5, abs_tol=1e-6)
            for left, right in zip(actual.vector, source.vector, strict=True)
        ):
            raise ValueError("迁移前后向量值不一致，迁移已停止")


if __name__ == "__main__":
    main()
