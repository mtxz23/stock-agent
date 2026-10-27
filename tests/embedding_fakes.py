"""隔离付费服务，用同义概念向量验证真实 Qdrant 检索链路。"""

from stock_research.embeddings import embedding_namespace


class FakeEmbedding:
    """同义词共享方向，无关概念正交，测试不依赖字面命中。"""

    def __init__(self, config, key=""):
        """测试也遵守真实模型的命名空间及维度约束。"""
        self.namespace = embedding_namespace(config)
        self.dimensions = config["dimensions"]
        self.document_calls = 0
        self.query_calls = 0

    async def embed_documents(self, texts):
        """生成确定性的测试文档向量，记录编码次数。"""
        self.document_calls += 1
        return [self.vector(text) for text in texts]

    async def embed_query(self, query):
        """查询走与文档一致的概念空间。"""
        self.query_calls += 1
        return self.vector(query)

    def vector(self, text):
        """现金创造、竞争优势和利润质量分别属于不同语义方向。"""
        values = [0.0] * self.dimensions
        concepts = (("现金流", "造血"), ("护城河", "竞争壁垒"), ("毛利率", "盈利质量"))
        for index, words in enumerate(concepts):
            if any(word in text for word in words):
                values[index] = 1.0
        if not any(values):
            values[3] = 1.0
        return values
