"""后台处理流水线（Issue #7）：解析 → 切分 → 向量化 → 入库。

parse_pdf 来自 Issue #6（app.ingestion.parser）；
chunk_pages / embed_chunks 由孙忠其提供（Issue #11 / #10），
以参数注入，兼容同步或异步实现；同步实现 CPU 密集（如真实 BGE-M3），
在工作线程（run_in_threadpool）中执行，不阻塞事件循环；异步实现直接
await。RAG 侧 chunk_pages() / embed_chunks() 公共接口不修改。

失败即整份 FAILED 并写 error_message（C1）：
- 解析失败：保留已合并 Issue #6 的分类提示；详细堆栈只写 parser 日志
- chunk_pages 返回空列表（整份没有可用文字）→「未提取到文字」
  （10/25 范围变更；此时 C2 不写入该文件的任何文本块）
- embed_chunks 有 chunk 缺失向量（含返回空列表）→「向量化不完整（缺失向量）」
  （Bug1：否则文件会被置 READY 但向量缺失、无法被检索）
- 文件在处理期间被删除 → 中止（Bug2：不重建 C1、不置 READY）
- 切分/向量化/入库任何一步异常
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.ingestion.parser import PdfParseError, parse_pdf
from app.storage.repo import (
    mark_document_failed,
    mark_document_ready,
    replace_document_chunks,
    save_parsed_pages,
)
from app.storage.schemas import ChunkData

SessionMaker = Callable[[], Any]


class ProcessingResult(BaseModel):
    status: Literal["READY", "FAILED"]
    reason: str | None = None


ChunkPagesFn = Callable[[list], "list[Any] | Awaitable[list[Any]]"]
EmbedChunksFn = Callable[[list], "list[Any] | Awaitable[list[Any]]"]


async def _call_chunk_or_embed(fn, *args: Any) -> Any:
    """调用切分/向量化函数，兼容同步与异步实现：

    - 同步实现（CPU 密集，如真实 BGE-M3 切分/推理）→ 放到工作线程执行，
      不阻塞事件循环（否则整份文档处理期间 FastAPI 完全无响应）；
    - 异步实现 → 直接 await。

    RAG 侧 chunk_pages() / embed_chunks() 公共接口不需要修改。
    """
    if inspect.iscoroutinefunction(fn):
        return await fn(*args)
    result = await run_in_threadpool(fn, *args)
    if inspect.isawaitable(result):
        # 兜底：被包装的异步函数（如 partial）在线程中返回协程
        return await result
    return result


async def process_document(
    document_id: str,
    pdf_path: Path,
    filename: str,
    chunk_pages: ChunkPagesFn,
    embed_chunks: EmbedChunksFn,
    session_factory=None,
) -> ProcessingResult:
    """跑完整条后台流水线，返回整份结果。"""
    kwargs = {} if session_factory is None else {"session_factory": session_factory}
    try:
        # 1) 解析（Issue #6）
        try:
            pages = await run_in_threadpool(parse_pdf, pdf_path)
        except PdfParseError as exc:
            # 保留 Issue #6 分类提示（无法打开、无文字、空页过半、提取异常）
            await mark_document_failed(document_id, str(exc), **kwargs)
            return ProcessingResult(status="FAILED", reason=str(exc))

        # 2) 解析结果入库（C1 页数 + ParsedPage；状态保持 PROCESSING）
        for page in pages:
            # parser 默认用文件名当 document_id；这里统一覆盖为正式 id
            page.document_id = document_id
        saved = await save_parsed_pages(
            document_id, filename, pages, **kwargs
        )
        if not saved:
            # Bug2：文件在处理期间被删除 → 中止，不重建、不 READY
            return ProcessingResult(status="FAILED", reason="文件处理期间已被删除")

        # 3) 切分（Issue #11）
        chunks = await _call_chunk_or_embed(chunk_pages, pages)
        if not chunks:
            # 10/25 范围变更：整份没有可用文字 → FAILED，C2 不写入任何文本块
            await mark_document_failed(document_id, "未提取到文字", **kwargs)
            return ProcessingResult(status="FAILED", reason="未提取到文字")

        # 4) 向量化（Issue #10）
        embeddings = await _call_chunk_or_embed(embed_chunks, chunks)
        vector_by_chunk_id = {record.chunk_id: record.vector for record in embeddings}
        # Bug1：每个 chunk 都必须有向量；缺失/空 → 整份 FAILED（否则 READY 但无法检索）
        missing = [
            chunk.chunk_id
            for chunk in chunks
            if chunk.chunk_id not in vector_by_chunk_id
            or not vector_by_chunk_id[chunk.chunk_id]
        ]
        if missing:
            await mark_document_failed(
                document_id, "向量化不完整（缺失向量）", **kwargs
            )
            return ProcessingResult(status="FAILED", reason="向量化不完整（缺失向量）")

        # 5) 文本块入库（C2）
        chunk_data = [
            ChunkData(**{**chunk.model_dump(), "embedding": vector_by_chunk_id.get(chunk.chunk_id)})
            for chunk in chunks
        ]
        await replace_document_chunks(document_id, chunk_data, **kwargs)

        # 6) 整份成功 → READY
        await mark_document_ready(document_id, **kwargs)
        return ProcessingResult(status="READY")
    except Exception as exc:
        reason = str(exc) or type(exc).__name__
        try:
            await mark_document_failed(document_id, reason, **kwargs)
        except Exception:
            pass
        return ProcessingResult(status="FAILED", reason=reason)


def build_process_document(
    chunk_pages: ChunkPagesFn,
    embed_chunks: EmbedChunksFn,
    session_factory=None,
):
    """把切分/向量化函数绑定进流水线，返回可直接交给 API 路由的处理函数。"""

    async def _process(document_id: str, pdf_path: Path, filename: str) -> ProcessingResult:
        return await process_document(
            document_id,
            pdf_path,
            filename,
            chunk_pages,
            embed_chunks,
            session_factory=session_factory,
        )

    return _process
