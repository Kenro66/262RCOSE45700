"""storage/repo.py 数据访问层（Issue #7，SQLite 验证；search_similar 做 SQL 编译检查）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.storage.models import Chunk, Document, ParsedPage
from app.storage.repo import (
    build_search_similar_stmt,
    create_processing_document,
    delete_document,
    get_documents,
    has_ready_documents,
    list_documents,
    mark_document_failed,
    mark_document_ready,
    replace_document_chunks,
)
from app.storage.schemas import ChunkData


async def _make_chunk(document_id: str, index: int = 0) -> ChunkData:
    return ChunkData(
        schema_version="1.0",
        chunk_id=f"{document_id}_C{index:06d}",
        document_id=document_id,
        chunk_index=index,
        page_index=0,
        page_number=1,
        text=f"chunk {index}",
        section_title=None,
        language="en",
        token_count=1,
        char_count=7,
        content_hash="sha256:test",
        embedding=[0.2] * 1024,
    )


@pytest.mark.asyncio
async def test_get_documents_skips_missing_ids(db) -> None:
    """get_documents：存在的 id 有结果；不存在的 id 不报错，只是不在结果里。"""
    await create_processing_document("DOC001", "a.pdf", session_factory=db)
    await create_processing_document("DOC002", "b.pdf", session_factory=db)

    result = await get_documents(["DOC001", "DOC002", "NOPE"], session_factory=db)

    assert set(result.keys()) == {"DOC001", "DOC002"}
    assert result["DOC001"].filename == "a.pdf"
    assert result["DOC002"].status.value == "PROCESSING"

    assert await get_documents([], session_factory=db) == {}


@pytest.mark.asyncio
async def test_list_documents_newest_first(db) -> None:
    """list_documents：C1 列表，最新在前。"""
    await create_processing_document("DOC001", "a.pdf", session_factory=db)
    await create_processing_document("DOC002", "b.pdf", session_factory=db)

    now = datetime.now(timezone.utc)
    async with db() as session:
        old = await session.get(Document, "DOC001")
        old.uploaded_at = now - timedelta(hours=1)
        await session.commit()

    listing = await list_documents(session_factory=db)

    assert [record.document_id for record in listing] == ["DOC002", "DOC001"]


@pytest.mark.asyncio
async def test_delete_document_cascades_pages_and_chunks(db) -> None:
    """delete_document：C1、ParsedPage、C2 一起删掉。"""
    await create_processing_document("DOC001", "a.pdf", session_factory=db)
    async with db() as session:
        session.add(
            ParsedPage(
                document_id="DOC001",
                page_index=0,
                page_number=1,
                text="page one",
            )
        )
        await session.commit()
    await replace_document_chunks(
        "DOC001", [await _make_chunk("DOC001", 0), await _make_chunk("DOC001", 1)],
        session_factory=db,
    )

    assert await delete_document("DOC001", session_factory=db) is True
    assert await delete_document("DOC001", session_factory=db) is False

    async with db() as session:
        assert (await session.get(Document, "DOC001")) is None
        pages = await session.scalars(select(ParsedPage))
        chunks = await session.scalars(select(Chunk))
        assert list(pages.all()) == []
        assert list(chunks.all()) == []


@pytest.mark.asyncio
async def test_mark_ready_and_failed(db) -> None:
    await create_processing_document("DOC001", "a.pdf", session_factory=db)

    await mark_document_failed("DOC001", "boom", session_factory=db)
    async with db() as session:
        document = await session.get(Document, "DOC001")
    assert document.status == "FAILED"
    assert document.error_message == "boom"

    await mark_document_ready("DOC001", session_factory=db)
    async with db() as session:
        document = await session.get(Document, "DOC001")
    assert document.status == "READY"
    assert document.error_message is None


@pytest.mark.asyncio
async def test_replace_document_chunks_replaces_all(db) -> None:
    await create_processing_document("DOC001", "a.pdf", session_factory=db)
    await replace_document_chunks(
        "DOC001", [await _make_chunk("DOC001", 0), await _make_chunk("DOC001", 1)],
        session_factory=db,
    )
    await replace_document_chunks(
        "DOC001", [await _make_chunk("DOC001", 0)], session_factory=db
    )

    async with db() as session:
        rows = await session.scalars(select(Chunk))
    assert [row.chunk_id for row in rows.all()] == ["DOC001_C000000"]

    with pytest.raises(ValueError):
        await replace_document_chunks(
            "DOC001", [await _make_chunk("DOC002")], session_factory=db
    )
    with pytest.raises(ValueError):
        await replace_document_chunks("NOPE", [], session_factory=db)


@pytest.mark.asyncio
async def test_has_ready_documents(db) -> None:
    """has_ready_documents：只有 READY 文件才算（Issue #9 chat 前置检查）。"""
    assert await has_ready_documents(session_factory=db) is False

    # PROCESSING 不算
    await create_processing_document("DOC001", "a.pdf", session_factory=db)
    assert await has_ready_documents(session_factory=db) is False

    # READY 算
    await mark_document_ready("DOC001", session_factory=db)
    assert await has_ready_documents(session_factory=db) is True

    # 另一份 FAILED 不影响
    await create_processing_document("DOC002", "b.pdf", session_factory=db)
    await mark_document_failed("DOC002", "boom", session_factory=db)
    assert await has_ready_documents(session_factory=db) is True

    # 全部删掉后不算
    await delete_document("DOC001", session_factory=db)
    await delete_document("DOC002", session_factory=db)
    assert await has_ready_documents(session_factory=db) is False


@pytest.mark.asyncio
async def test_search_stmt_compiles_for_postgres(db) -> None:
    """search_similar 的语句按 Postgres/pgvector 编译：cosine 距离、只查 READY、limit。"""
    stmt = build_search_similar_stmt([0.0] * 8, top_k=3)
    sql = str(
        stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )

    assert "<=>" in sql  # pgvector cosine distance 运算符
    assert "'READY'" in sql
    assert "LIMIT 3" in sql
