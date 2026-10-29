"""数据访问层（Issue #7，《MVP技术方案》5.4）。

对王一霖（C1）：
- create_processing_document / save_parsed_pages / mark_document_ready /
  mark_document_failed / list_documents / delete_document / next_document_id

对孙忠其（检索与引用校验，Issue #11 / #12）：
- search_similar(query_vector, top_k) -> list[SearchHit]
- get_documents(ids) -> dict[str, DocumentRecord]

返回格式见 backend/app/contracts.py。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.contracts import DocumentRecord, DocumentStatus, SearchHit
from app.database import SessionFactory
from app.storage.models import Chunk, Document, ParsedPage
from app.storage.schemas import ChunkData

SessionMaker = Callable[[], AsyncSession]


def to_document_record(document: Document) -> DocumentRecord:
    """ORM → contracts.DocumentRecord（C1 对外格式）。"""
    uploaded_at = document.uploaded_at
    if uploaded_at is not None and uploaded_at.tzinfo is None:
        uploaded_at = uploaded_at.replace(tzinfo=timezone.utc)
    return DocumentRecord(
        document_id=document.document_id,
        filename=document.filename,
        status=DocumentStatus(document.status),
        error_message=document.error_message,
        page_count=document.page_count,
        uploaded_at=uploaded_at,
    )


async def create_processing_document(
    document_id: str,
    filename: str,
    session_factory: SessionMaker = SessionFactory,
) -> Document:
    """创建 PROCESSING 记录，客户端处理期间可轮询。"""
    async with session_factory() as session:
        document = Document(
            document_id=document_id,
            filename=filename,
            status="PROCESSING",
            error_message=None,
            page_count=None,
        )
        session.add(document)
        await session.commit()
        await session.refresh(document)
        return document


async def save_parsed_pages(
    document_id: str,
    filename: str,
    pages: list,
    session_factory: SessionMaker = SessionFactory,
) -> bool:
    """落库解析结果：C1 页数 + ParsedPage 全量页。

    只写页数与页内容，不改状态——整份 READY 要等切分/向量化完成
    （见 mark_document_ready）。

    C1 由上传时 create_processing_document 建立；这里只在 C1 已存在时更新。
    若 C1 不存在（文件在处理期间被删除）→ 不重建、回传 False（Bug2 修复，
    避免删除后的文件被后台任务复活成 READY）。
    """
    async with session_factory() as session:
        document = await session.get(Document, document_id)
        if document is None:
            # 文件已被删除（或从未建立）→ 不重建 C1
            return False

        document.filename = filename
        document.page_count = len(pages)

        await session.execute(
            delete(ParsedPage).where(ParsedPage.document_id == document_id)
        )
        session.add_all(
            [
                ParsedPage(
                    document_id=document_id,
                    page_index=page.page_index,
                    page_number=page.page_number,
                    text=page.text,
                    language=page.language,
                    content_hash=page.content_hash,
                    char_count=page.char_count,
                    schema_version=page.schema_version,
                    parser_version=page.parser_version,
                    blocks=page.blocks,
                )
                for page in pages
            ]
        )
        await session.commit()
        return True


async def mark_document_ready(
    document_id: str,
    session_factory: SessionMaker = SessionFactory,
) -> None:
    """整份处理（解析→切分→向量化→入库）成功后置 READY。"""
    async with session_factory() as session:
        document = await session.get(Document, document_id)
        if document is not None:
            document.status = "READY"
            document.error_message = None
            await session.commit()


async def mark_document_failed(
    document_id: str,
    reason: str,
    session_factory: SessionMaker = SessionFactory,
) -> None:
    """整份处理失败：置 FAILED 并写 error_message。"""
    async with session_factory() as session:
        document = await session.get(Document, document_id)
        if document is not None:
            document.status = "FAILED"
            document.error_message = reason
            document.page_count = None
            await session.commit()


async def get_document(
    document_id: str,
    session_factory: SessionMaker = SessionFactory,
) -> Document | None:
    async with session_factory() as session:
        statement = (
            select(Document)
            .where(Document.document_id == document_id)
            .options(selectinload(Document.pages), selectinload(Document.chunks))
        )
        return await session.scalar(statement)


async def list_documents(
    session_factory: SessionMaker = SessionFactory,
) -> list[DocumentRecord]:
    """GET /api/documents：全部 C1 记录，最新在前。"""
    async with session_factory() as session:
        documents = list(
            (
                await session.scalars(
                    select(Document).order_by(
                        Document.uploaded_at.desc(), Document.document_id
                    )
                )
            ).all()
        )
        return [to_document_record(document) for document in documents]


async def get_documents(
    ids: list[str],
    session_factory: SessionMaker = SessionFactory,
) -> dict[str, DocumentRecord]:
    """按 document_id 批量查 C1（引用校验用，Issue #12）。

    不存在的 id 不报错，只是不出现在结果里。
    """
    if not ids:
        return {}
    async with session_factory() as session:
        rows = (
            await session.scalars(
                select(Document).where(Document.document_id.in_(set(ids)))
            )
        ).all()
        return {document.document_id: to_document_record(document) for document in rows}


async def delete_document(
    document_id: str,
    session_factory: SessionMaker = SessionFactory,
) -> bool:
    """删除 C1 及级联的 ParsedPage / C2 记录。"""
    async with session_factory() as session:
        document = await session.get(Document, document_id)
        if document is None:
            return False
        await session.delete(document)
        await session.commit()
        return True


async def next_document_id(
    session_factory: SessionMaker = SessionFactory,
) -> str:
    """分配并发安全、可读的 document id（Postgres 序列）。"""
    async with session_factory() as session:
        number = await session.scalar(
            func.nextval("document_number_seq")
        )
        if number is None:
            raise RuntimeError("document_number_seq 不存在，请先运行 init_db()")
        return f"DOC{number:03d}"


def build_search_similar_stmt(query_vector: list[float], top_k: int):
    """构造向量检索语句（仅 Postgres/pgvector）。

    只查 READY 文件；score = 1 - 余弦距离，按 score 从高到低（距离升序）
    取前 top_k 条。
    """
    distance = Chunk.embedding.cosine_distance(query_vector)
    score = (1 - distance).label("score")
    return (
        select(
            Chunk.chunk_id,
            Chunk.document_id,
            Chunk.page_number,
            Chunk.text,
            score,
        )
        .join(Document, Document.document_id == Chunk.document_id)
        .where(
            Document.status == DocumentStatus.READY.value,
            Chunk.embedding.is_not(None),
        )
        .order_by(distance.asc())
        .limit(top_k)
    )


async def search_similar(
    query_vector: list[float],
    top_k: int,
    session_factory: SessionMaker = SessionFactory,
) -> list[SearchHit]:
    """按向量距离在 C2 里取最近的 top_k 个文本块，只查 READY 文件（Issue #11）。"""
    statement = build_search_similar_stmt(query_vector, top_k)
    async with session_factory() as session:
        rows = (await session.execute(statement)).all()
        return [
            SearchHit(
                chunk_id=row.chunk_id,
                document_id=row.document_id,
                page_number=row.page_number,
                text=row.text,
                score=float(row.score),
            )
            for row in rows
        ]


async def replace_document_chunks(
    document_id: str,
    chunks: list[ChunkData],
    session_factory: SessionMaker = SessionFactory,
) -> None:
    """整份替换一个文档的全部 C2 记录（单事务）。"""
    if any(chunk.document_id != document_id for chunk in chunks):
        raise ValueError("Every chunk must belong to the requested document_id")

    async with session_factory() as session:
        if await session.get(Document, document_id) is None:
            raise ValueError(f"Document {document_id} does not exist")

        await session.execute(delete(Chunk).where(Chunk.document_id == document_id))
        session.add_all([Chunk(**chunk.model_dump()) for chunk in chunks])
        await session.commit()


async def get_document_chunks(
    document_id: str,
    session_factory: SessionMaker = SessionFactory,
) -> list[Chunk]:
    """按 chunk_index 顺序取一个文档的全部文本块。"""
    async with session_factory() as session:
        statement = (
            select(Chunk)
            .where(Chunk.document_id == document_id)
            .order_by(Chunk.chunk_index)
        )
        return list((await session.scalars(statement)).all())
