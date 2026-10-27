"""验证算术边界、原文归档和资料地址限制。"""

import io

import httpx
import pytest
from pypdf import PdfWriter

from stock_research.storage import Repository
from stock_research.tools import ResearchTools, ToolError, calculate, validate_public_url

HTML = (
    "<html><head><title>测试公司年度报告</title></head><body><article><h1>年度报告</h1>"
    "<p>本文件是自动化测试原文，不代表真实公司。2025年度主营业务收入为120万元，"
    "2024年度主营业务收入为100万元。金额均以人民币万元列示。</p>"
    "<p>测试公司主要提供设备服务，经营现金流量净额为30万元。"
    "以下数字仅用于验证解析、计算及引用流程。</p></article></body></html>"
).encode()


@pytest.mark.parametrize(
    ("expression", "expected"),
    [("0.1+0.2", "0.3"), ("(120-100)/100*100", "20.0"), ("-(2**3)", "-8")],
)
def test_decimal_calculation(expression, expected):
    """财务小数使用 Decimal，支持受限算术。"""
    assert calculate(expression) == expected


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').getcwd()",
        "x+1",
        "1/0",
        "True+1",
        "2**1000",
        "1e101",
        "[1,2]",
        "1+" * 160 + "1",
    ],
)
def test_unsafe_or_unbounded_expression_rejected(expression):
    """禁止代码、过大数值和超长表达式。"""
    with pytest.raises(ToolError):
        calculate(expression)


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1/x", "http://[::1]/x", "file:///test.pdf", "https://user:pass@example.com"],
)
async def test_private_or_non_http_source_rejected(url):
    """资料读取拒绝本机、文件和嵌入凭据地址。"""
    with pytest.raises(ToolError):
        await validate_public_url(url)


async def test_original_is_archived_and_deduplicated(tmp_path, monkeypatch):
    """同一原文版本保留一个证据编号，并能读回原始字节。"""

    async def allow(url):
        """测试环境禁用实际 DNS，HTTP 内容由 MockTransport 提供。"""

    monkeypatch.setattr("stock_research.tools.validate_public_url", allow)
    repository = Repository(tmp_path)
    await repository.initialize()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=HTML, headers={"content-type": "text/html"})
        )
    ) as client:
        tools = ResearchTools(client, repository, tmp_path, "task")
        first = await tools.execute("read_source", {"url": "https://company.example/report"})
        second = await tools.execute("read_source", {"url": "https://company.example/report"})
    assert first["source_id"] == second["source_id"]
    assert "120" in first["text"]
    source = (await repository.sources("task"))[0]
    assert (tmp_path / "sources" / "task" / source["archive_name"]).read_bytes() == HTML
    assert len(source["content_hash"]) == 64


async def test_blank_pdf_is_not_evidence(tmp_path, monkeypatch):
    """仅有页码标签的扫描或空白 PDF 不能登记为已读取证据。"""

    async def allow(url):
        """跳过测试域名解析。"""

    monkeypatch.setattr("stock_research.tools.validate_public_url", allow)
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    output = io.BytesIO()
    writer.write(output)
    repository = Repository(tmp_path)
    await repository.initialize()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, content=output.getvalue(), headers={"content-type": "application/pdf"}
            )
        )
    ) as client:
        with pytest.raises(ToolError, match="没有可读取"):
            await ResearchTools(client, repository, tmp_path, "task").execute(
                "read_source", {"url": "https://company.example/report.pdf"}
            )
    assert await repository.sources("task") == []
