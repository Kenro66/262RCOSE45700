"""文档 API 装配（Issue #7）。

POST/GET/DELETE /api/documents 的路由在这里装配好，
chunk_pages（Issue #11）/ embed_chunks（Issue #10）由孙忠其提供，
由 app/main.py 传入真实实现。同步或异步实现均可（pipeline 自适应）。
"""

from __future__ import annotations

from app.database import SessionFactory
from app.documents.pipeline import build_process_document
from app.documents.service import create_documents_router


def build_documents_router(chunk_pages, embed_chunks):
    """构建 /api/documents 路由（POST / GET / DELETE）。

    chunk_pages:  (pages: list[ParsedPage]) -> list[Chunk]          （Issue #11）
    embed_chunks: (chunks: list[Chunk]) -> list[EmbeddingRecord]    （Issue #10）
    """
    process_document = build_process_document(
        chunk_pages, embed_chunks, session_factory=SessionFactory
    )
    return create_documents_router(process_document)
