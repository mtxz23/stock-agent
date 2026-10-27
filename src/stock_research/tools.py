"""提供股票数据、联网搜索、原文读取和受限计算，记录实际取得的研究证据。"""

import ast
import asyncio
import hashlib
import io
import ipaddress
import logging
import socket
from decimal import Decimal, DecimalException, localcontext
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
import trafilatura
from ddgs import DDGS
from pydantic import BaseModel, Field
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from stock_research.history import HistorySearcher, HistoryStore
from stock_research.history_tools import HistoryTools, history_definitions
from stock_research.market_data import StockDataError, StockDataService
from stock_research.market_tools import STOCK_TOOL_NAMES, StockTools, stock_definitions
from stock_research.memory_tools import MemoryTools, memory_definitions
from stock_research.storage import Repository
from stock_research.watchlist import WatchlistInput, WatchlistService, watchlist_definition

logger = logging.getLogger(__name__)
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
MAX_TEXT_CHARACTERS = 14000


class ToolError(RuntimeError):
    """表示可反馈给模型的资料访问或计算失败。"""


class BeginResearchInput(BaseModel):
    """仅在用户要求实际公司研究且对象明确时启用证据要求。"""

    company: str = Field(min_length=1, max_length=200, pattern=r"\S")


class SearchInput(BaseModel):
    """限定搜索词及结果条数。"""

    query: str = Field(min_length=2, max_length=500)
    max_results: int = Field(default=5, ge=1, le=8)


class ReadInput(BaseModel):
    """描述网页或 PDF 的阅读范围，页码从一开始。"""

    url: str = Field(min_length=8, max_length=2000)
    start_page: int = Field(default=1, ge=1, le=3000)
    page_count: int = Field(default=5, ge=1, le=8)


class CalculateInput(BaseModel):
    """接收纯算术表达式，不允许执行 Python 代码。"""

    expression: str = Field(min_length=1, max_length=300)


class ResearchTools:
    """将模型工具调用连接到可替换的数据能力及证据归档。"""

    def __init__(
        self,
        client: httpx.AsyncClient,
        repository: Repository,
        data_dir: Path,
        task_id: str,
        history_searcher: HistorySearcher | None = None,
        watchlist: WatchlistService | None = None,
        stock_data: StockDataService | None = None,
    ) -> None:
        """绑定当前研究任务，确保来源和原始文件归属明确。"""
        self._client = client
        self._repository = repository
        self._archive_dir = data_dir / "sources" / task_id
        self._task_id = task_id
        self._watchlist = watchlist
        self._stocks = StockTools(stock_data, repository, data_dir, task_id) if stock_data else None
        self._memory = MemoryTools(repository, task_id)
        self._history = HistoryTools(HistoryStore(repository.path, history_searcher), task_id)

    async def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """验证工具参数并执行，保留异步取消异常的传播。"""
        if name in STOCK_TOOL_NAMES:
            if self._stocks is None:
                raise ToolError("股票数据服务尚未启动")
            try:
                return await self._stocks.execute(name, arguments)
            except StockDataError as exc:
                raise ToolError(str(exc)) from exc
        if name == "get_watchlist":
            if self._watchlist is None:
                raise ToolError("自选股同步服务尚未启动")
            request = WatchlistInput.model_validate(arguments)
            try:
                return await self._watchlist.query(**request.model_dump())
            except ValueError as exc:
                raise ToolError(str(exc)) from exc
        if name in {"search_history", "read_history"}:
            return await self._history.execute(name, arguments)
        if name in {"save_memory", "update_memory", "delete_memory"}:
            return await self._memory.execute(name, arguments)
        if name == "begin_research":
            request = BeginResearchInput.model_validate(arguments)
            await self._repository.begin_research(self._task_id, request.company.strip())
            logger.info("启用公司研究，任务=%s", self._task_id)
            return {"company": request.company.strip(), "research_started": True}
        if name == "search_web":
            request = SearchInput.model_validate(arguments)
            return await self._search(request)
        if name == "read_source":
            request = ReadInput.model_validate(arguments)
            return await self._read(request)
        if name == "calculate":
            request = CalculateInput.model_validate(arguments)
            return {"expression": request.expression, "result": calculate(request.expression)}
        raise ToolError(f"未知工具：{name}，请使用已提供的工具")

    async def _search(self, request: SearchInput) -> dict[str, Any]:
        """通过公开搜索引擎定位资料，搜索摘要不能充当原文证据。"""
        logger.info("开始搜索，任务=%s", self._task_id)
        try:
            results = await asyncio.to_thread(_search_sync, request)
        except Exception as exc:
            # 搜索依赖的后端异常类型不统一，统一转为明确的可恢复错误。
            logger.warning("公开搜索不可用，任务=%s，类型=%s", self._task_id, type(exc).__name__)
            raise ToolError(
                "公开搜索暂不可用或被限流，请调整搜索词或尝试已知的官方原文链接"
            ) from exc
        return {"results": results, "verified": False, "note": "仅搜索摘要，请读取原文核实"}

    async def _read(self, request: ReadInput) -> dict[str, Any]:
        """下载并归档公开原文，返回实际阅读范围和来源编号。"""
        try:
            raw, final_url, media_type = await self._download(request.url)
            parsed = await asyncio.to_thread(_extract_source, raw, media_type, request)
        except (httpx.HTTPError, OSError, PdfReadError, ValueError) as exc:
            raise ToolError(
                "原文访问或解析失败，请换一个有效来源；扫描版 PDF 暂不支持 OCR"
            ) from exc
        if not parsed["text"].strip():
            raise ToolError("原文没有可读取的正文；可能是扫描件、登录页或动态加载页面")
        digest = hashlib.sha256(raw).hexdigest()
        suffix = ".pdf" if parsed["is_pdf"] else ".html"
        archive_name = digest + suffix
        self._archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = self._archive_dir / archive_name
        if not archive_path.exists():
            await asyncio.to_thread(archive_path.write_bytes, raw)
        source = await self._repository.save_source(
            self._task_id,
            {
                "url": final_url,
                "title": parsed["title"] or final_url,
                "content_hash": digest,
                "archive_name": archive_name,
                "media_type": "application/pdf" if parsed["is_pdf"] else "text/html",
            },
        )
        return {
            "source_id": source["id"],
            "url": final_url,
            "title": source["title"],
            "fetched_at": source["fetched_at"],
            "publication_time": "未核实，需从原文确认",
            "text": parsed["text"][:MAX_TEXT_CHARACTERS],
            "truncated": len(parsed["text"]) > MAX_TEXT_CHARACTERS,
            "total_pages": parsed["total_pages"],
            "read_pages": parsed["read_pages"],
        }

    async def _download(self, url: str) -> tuple[bytes, str, str]:
        """逐次检查重定向目标，限制下载大小和网络访问范围。"""
        for _ in range(5):
            await validate_public_url(url)
            async with self._client.stream(
                "GET",
                url,
                follow_redirects=False,
                timeout=35,
                headers={"User-Agent": "StockResearch/0.1 (public-company-research)"},
            ) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise ToolError("原文重定向缺少目标地址")
                    url = urljoin(url, location)
                    continue
                response.raise_for_status()
                pieces: list[bytes] = []
                size = 0
                async for piece in response.aiter_bytes():
                    size += len(piece)
                    if size > MAX_DOWNLOAD_BYTES:
                        raise ToolError("原文超过 20 MB，请使用较小文件或其他来源")
                    pieces.append(piece)
                return b"".join(pieces), str(response.url), response.headers.get("content-type", "")
        raise ToolError("原文重定向次数过多")


def tool_definitions(
    *,
    include_research_control: bool = False,
    include_memory: bool = False,
    include_history: bool = False,
    include_watchlist: bool = False,
    include_stock_data: bool = False,
) -> list[dict[str, Any]]:
    """返回规范工具描述，让模型按研究问题选择工具。"""
    descriptions = [
        (
            "search_web",
            "搜索公开公司资料、公告、财报或行业信息。摘要仅用于定位原文。",
            SearchInput,
        ),
        (
            "read_source",
            "读取公开网页或 PDF 并归档。PDF 可用 start_page/page_count 分段阅读；页码从1开始。",
            ReadInput,
        ),
        (
            "calculate",
            "计算纯算术表达式，支持 + - * / ** 和括号。输入须明确统一单位。",
            CalculateInput,
        ),
    ]
    if include_research_control:
        descriptions.insert(
            0,
            (
                "begin_research",
                "用户要求实际调查一家明确的公司时调用，启用公司研究的原文证据要求。"
                "日常聊天、概念解释或解释已有报告时不要调用；研究对象不明确时先询问用户。",
                BeginResearchInput,
            ),
        )
    return (
        [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": schema.model_json_schema(),
                },
            }
            for name, description, schema in descriptions
        ]
        + (memory_definitions() if include_memory else [])
        + (history_definitions() if include_history else [])
        + ([watchlist_definition()] if include_watchlist else [])
        + (stock_definitions() if include_stock_data else [])
    )


async def validate_public_url(url: str) -> None:
    """拒绝非 HTTP 链接、嵌入凭据及解析到本机或私网的资料地址。"""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ToolError("资料地址必须是公开 HTTP 或 HTTPS 链接")
    if parts.username or parts.password:
        raise ToolError("资料地址不能包含凭据")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
        addresses = await asyncio.get_running_loop().getaddrinfo(
            parts.hostname, port, type=socket.SOCK_STREAM
        )
    except (OSError, ValueError) as exc:
        raise ToolError("资料地址无法解析") from exc
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ToolError("资料工具只允许访问公网地址")


def calculate(expression: str) -> str:
    """通过 AST 白名单与 Decimal 计算，禁止名称、函数、属性及任意代码执行。"""
    try:
        if len(expression) > 300:
            raise ValueError("表达式超过长度上限")
        tree = ast.parse(expression, mode="eval")
        with localcontext() as context:
            context.prec = 28
            result = _calculate_node(tree.body, expression)
        if not result.is_finite() or abs(result.adjusted()) > 100:
            raise ValueError("结果超出计算范围")
        return format(result, "f")
    except (SyntaxError, DecimalException, ValueError, OverflowError, RecursionError) as exc:
        raise ToolError("算术表达式无效、除数为零或数值超出范围") from exc


def _calculate_node(node: ast.AST, expression: str) -> Decimal:
    """递归计算受限算术节点，并检查中间结果大小。"""
    if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
        value = Decimal(ast.get_source_segment(expression, node) or str(node.value))
    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        operand = _calculate_node(node.operand, expression)
        value = operand if isinstance(node.op, ast.UAdd) else -operand
    elif isinstance(node, ast.BinOp):
        left = _calculate_node(node.left, expression)
        right = _calculate_node(node.right, expression)
        if isinstance(node.op, ast.Add):
            value = left + right
        elif isinstance(node.op, ast.Sub):
            value = left - right
        elif isinstance(node.op, ast.Mult):
            value = left * right
        elif isinstance(node.op, ast.Div):
            value = left / right
        elif (
            isinstance(node.op, ast.Pow) and right == right.to_integral_value() and abs(right) <= 12
        ):
            value = left ** int(right)
        else:
            raise ValueError("不支持该运算")
    else:
        raise ValueError("只允许算术表达式")
    if not value.is_finite() or abs(value.adjusted()) > 100:
        raise ValueError("数值超出范围")
    return value


def _search_sync(request: SearchInput) -> list[dict[str, str]]:
    """调用搜索库，执行在线程内进行，避免阻塞异步任务。"""
    results = DDGS(timeout=15).text(request.query, max_results=request.max_results)
    return [
        {
            "title": item.get("title", ""),
            "url": item.get("href", ""),
            "snippet": item.get("body", "")[:1200],
        }
        for item in results
    ]


def _extract_source(raw: bytes, media_type: str, request: ReadInput) -> dict[str, Any]:
    """解析 HTML 或指定 PDF 页码，保留实际阅读范围。"""
    is_pdf = raw.startswith(b"%PDF-") or "application/pdf" in media_type
    if is_pdf:
        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted:
            raise ValueError("暂不读取加密 PDF")
        start = request.start_page - 1
        if start >= len(reader.pages):
            raise ValueError("阅读页码超出 PDF 范围")
        end = min(start + request.page_count, len(reader.pages))
        pages = [
            (index + 1, reader.pages[index].extract_text() or "") for index in range(start, end)
        ]
        # 空白页的页码标签不能当作成功读取的正文证据。
        text = "\n\n".join(
            f"[PDF 第 {page} 页]\n{content}" for page, content in pages if content.strip()
        )
        title = str((reader.metadata or {}).get("/Title", ""))
        return {
            "is_pdf": True,
            "title": title,
            "text": text,
            "total_pages": len(reader.pages),
            "read_pages": [start + 1, end],
        }
    metadata = trafilatura.extract_metadata(raw)
    text = trafilatura.extract(raw, include_tables=True, include_comments=False) or ""
    return {
        "is_pdf": False,
        "title": metadata.title if metadata else "",
        "text": text,
        "total_pages": None,
        "read_pages": None,
    }
