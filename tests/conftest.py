"""隔离测试日志和付费向量调用，避免污染正式记录。"""

import pytest
from embedding_fakes import FakeEmbedding

from stock_research import watchlist_source
from stock_research.rag import VectorHistory
from stock_research.security_names import SecurityNames
from stock_research.titles import ConversationTitles


@pytest.fixture(autouse=True)
def isolated_log_directory(tmp_path, monkeypatch):
    """为每个测试设置独立日志目录，保留正式模型和诊断配置。"""
    monkeypatch.setenv("STOCK_LOG_DIR", str(tmp_path / "logs"))
    # 正式工作台使用独立服务，测试继续使用各自临时目录中的嵌入式库。
    monkeypatch.setenv("STOCK_QDRANT_URL", "")
    monkeypatch.setenv("STOCK_QDRANT_API_KEY", "")


@pytest.fixture(autouse=True)
def isolated_history_embeddings(monkeypatch):
    """自动召回使用离线向量；云端协议测试仍单独验证真实适配器。"""
    monkeypatch.setattr(
        VectorHistory, "_create_model", lambda self, config, key: FakeEmbedding(config, key)
    )


@pytest.fixture(autouse=True)
def isolated_title_generation(monkeypatch, request):
    """业务测试隔离额外模型调用，自动命名测试单独验证真实调度流程。"""
    if request.node.get_closest_marker("automatic_titles"):
        return

    async def skip(self, task_id, config):
        """不让研究模型的测试脚本被独立标题请求消耗。"""

    monkeypatch.setattr(ConversationTitles, "schedule", skip)


@pytest.fixture(autouse=True)
def isolated_watchlist_discovery(monkeypatch):
    """测试只读取显式提供的临时客户端，避免同步真实用户自选。"""
    monkeypatch.delenv("STOCK_THS_INSTALL_DIR", raising=False)
    monkeypatch.setenv("STOCK_THS_ACCOUNT", "")
    monkeypatch.setattr(watchlist_source, "discover_installation", lambda: None)


@pytest.fixture(autouse=True)
def isolated_public_security_names(monkeypatch, request):
    """普通业务测试不访问公开名称接口，名称适配器测试使用离线响应。"""
    if request.node.get_closest_marker("security_names"):
        return

    async def skip(self, snapshot):
        """只保留测试客户端的本地名称。"""
        return False

    monkeypatch.setattr(SecurityNames, "enrich", skip)


@pytest.fixture(autouse=True)
def isolated_stock_data_network(monkeypatch, request):
    """普通业务测试禁止意外访问股票接口，股票测试显式使用离线适配器。"""
    if request.node.get_closest_marker("stock_data"):
        return
    from stock_research.market_sources import PublicStockSource

    async def blocked(self, url, params):
        """禁止测试访问真实公开数据，避免消耗限额及依赖联网。"""
        raise RuntimeError("业务测试未配置股票数据模拟响应")

    monkeypatch.setattr(PublicStockSource, "_get", blocked)
