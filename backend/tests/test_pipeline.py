"""后台流水线：解析 → 切分 → 向量化 → 入库（Issue #7，真实 parse_pdf + 假切分/向量）。"""

from __future__ import annotations

import hashlib
import random
from pathlib import Path

import pymupdf
import pytest
from sqlalchemy import select

from app.contracts import Chunk, EmbeddingRecord
from app.documents.pipeline import process_document
from app.storage.models import ParsedPage
from app.storage.repo import (
    create_processing_document,
    delete_document,
    get_document,
    get_document_chunks,
)

EMBEDDING_DIMENSION = 1024


def write_text_pdf(tmp_path: Path, name: str, *page_texts: str) -> Path:
    document = pymupdf.open()
    for page_text in page_texts:
        page = document.new_page()
        if page_text:
            page.insert_text((72, 72), page_text)
    path = tmp_path / name
    document.save(str(path))
    document.close()
    return path


def fake_chunk_pages(pages) -> list[Chunk]:
    """每页一个 chunk；空页不生成（与《Chunking 接口文档》一致）。"""
    chunks = []
    for index, page in enumerate(pages):
        if not page.text.strip():
            continue
        chunks.append(
            Chunk(
                schema_version="1.0",
                chunk_id=f"{page.document_id}_C{len(chunks):06d}",
                document_id=page.document_id,
                chunk_index=len(chunks),
                page_index=page.page_index,
                page_number=page.page_number,
                text=page.text.strip(),
                section_title=None,
                language=page.language,
                token_count=max(1, len(page.text) // 2),
                char_count=len(page.text),
                content_hash="sha256:test-chunk",
            )
        )
    return chunks


def fake_embed_chunks(chunks) -> list[EmbeddingRecord]:
    """确定性伪向量：同一 text 永远得到同一向量（检索测试可复现）。"""
    records = []
    for chunk in chunks:
        seed = int.from_bytes(
            hashlib.sha256(chunk.text.encode("utf-8")).digest()[:8], "big"
        )
        rng = random.Random(seed)
        records.append(
            EmbeddingRecord(
                chunk_id=chunk.chunk_id,
                vector=[round(rng.random(), 6) for _ in range(EMBEDDING_DIMENSION)],
            )
        )
    return records


@pytest.mark.asyncio
async def test_pipeline_ready_on_text_pdf(db, tmp_path) -> None:
    """3 页文本 PDF：READY、page_count=3、ParsedPage/C2 各 3 条且带向量。"""
    pdf_path = write_text_pdf(tmp_path, "doc.pdf", "first page", "second", "third")

    # 与 API 流程一致：上传时先建 C1(PROCESSING)，后台流水线才跑
    await create_processing_document("DOC001", "doc.pdf", session_factory=db)

    result = await process_document(
        "DOC001",
        pdf_path,
        "doc.pdf",
        fake_chunk_pages,
        fake_embed_chunks,
        session_factory=db,
    )

    assert result.status == "READY"
    assert result.reason is None

    document = await get_document("DOC001", session_factory=db)
    assert document is not None
    assert document.status == "READY"
    assert document.page_count == 3

    async with db() as session:
        pages = list(
            (
                await session.scalars(
                    select(ParsedPage)
                    .where(ParsedPage.document_id == "DOC001")
                    .order_by(ParsedPage.page_index)
                )
            ).all()
        )
    assert [page.page_number for page in pages] == [1, 2, 3]

    chunks = await get_document_chunks("DOC001", session_factory=db)
    assert [chunk.page_number for chunk in chunks] == [1, 2, 3]
    assert all(chunk.embedding is not None and len(chunk.embedding) == 1024 for chunk in chunks)


@pytest.mark.asyncio
async def test_pipeline_empty_chunks_failed_no_c2(db, tmp_path) -> None:
    """chunk_pages 返回空列表：整份 FAILED「未提取到文字」，C2 没有该文件的文本块。"""
    pdf_path = write_text_pdf(tmp_path, "doc.pdf", "some text")

    def empty_chunk_pages(pages):
        return []

    # 与 API 流程一致：上传时先建 C1(PROCESSING)
    await create_processing_document("DOC002", "doc.pdf", session_factory=db)

    result = await process_document(
        "DOC002",
        pdf_path,
        "doc.pdf",
        empty_chunk_pages,
        fake_embed_chunks,
        session_factory=db,
    )

    assert result.status == "FAILED"
    assert result.reason == "未提取到文字"

    document = await get_document("DOC002", session_factory=db)
    assert document.status == "FAILED"
    assert document.error_message == "未提取到文字"
    assert document.page_count is None
    assert await get_document_chunks("DOC002", session_factory=db) == []


@pytest.mark.asyncio
async def test_pipeline_parse_failed_blank_pdf(db, tmp_path) -> None:
    """空白扫描件：Issue #6 的解析失败规则 → 整份 FAILED（保留分类提示），不写 ParsedPage/C2。"""
    document = pymupdf.open()
    for _ in range(3):
        document.new_page()
    pdf_path = tmp_path / "blank.pdf"
    document.save(str(pdf_path))
    document.close()

    # 與 API 流程一致：上傳時先建 C1(PROCESSING)，背景流水線才開始跑
    await create_processing_document("DOC003", "blank.pdf", session_factory=db)

    result = await process_document(
        "DOC003",
        pdf_path,
        "blank.pdf",
        fake_chunk_pages,
        fake_embed_chunks,
        session_factory=db,
    )

    assert result.status == "FAILED"
    # 保留已合并Issue6分类提示；不修改parser来适配旧断言
    from app.ingestion.parser import ERROR_NO_TEXT
    assert result.reason == ERROR_NO_TEXT

    doc = await get_document("DOC003", session_factory=db)
    assert doc.status == "FAILED"
    assert doc.error_message
    async with db() as session:
        page_rows = await session.scalars(
            select(ParsedPage).where(ParsedPage.document_id == "DOC003")
        )
        assert list(page_rows.all()) == []
    assert await get_document_chunks("DOC003", session_factory=db) == []


@pytest.mark.asyncio
async def test_pipeline_embed_raises_failed(db, tmp_path) -> None:
    """向量化异常：整份 FAILED，error_message 写异常信息。"""
    pdf_path = write_text_pdf(tmp_path, "doc.pdf", "text here")

    def broken_embed(chunks):
        raise RuntimeError("embedding service down")

    # 与 API 流程一致：上传时先建 C1(PROCESSING)
    await create_processing_document("DOC004", "doc.pdf", session_factory=db)

    result = await process_document(
        "DOC004",
        pdf_path,
        "doc.pdf",
        fake_chunk_pages,
        broken_embed,
        session_factory=db,
    )

    assert result.status == "FAILED"
    assert result.reason == "embedding service down"
    doc = await get_document("DOC004", session_factory=db)
    assert doc.status == "FAILED"
    assert doc.error_message == "embedding service down"
    assert await get_document_chunks("DOC004", session_factory=db) == []


@pytest.mark.asyncio
async def test_pipeline_accepts_async_chunk_and_embed(db, tmp_path) -> None:
    """孙忠其的模块可能是 async 实现：流水线两种签名都兼容。"""
    pdf_path = write_text_pdf(tmp_path, "doc.pdf", "async page")

    async def async_chunk_pages(pages):
        return fake_chunk_pages(pages)

    async def async_embed(chunks):
        return fake_embed_chunks(chunks)

    # 与 API 流程一致：上传时先建 C1(PROCESSING)
    await create_processing_document("DOC005", "doc.pdf", session_factory=db)

    result = await process_document(
        "DOC005",
        pdf_path,
        "doc.pdf",
        async_chunk_pages,
        async_embed,
        session_factory=db,
    )

    assert result.status == "READY"
    chunks = await get_document_chunks("DOC005", session_factory=db)
    assert len(chunks) == 1


@pytest.mark.asyncio
async def test_pipeline_embed_returns_empty_failed(db, tmp_path) -> None:
    """Bug1 验收：embed_chunks 返回空列表 → 整份 FAILED（缺失向量），不置 READY、无法检索。"""
    pdf_path = write_text_pdf(tmp_path, "doc.pdf", "text here")

    # 与 API 流程一致：上传时先建 C1(PROCESSING)
    await create_processing_document("DOC101", "doc.pdf", session_factory=db)

    def empty_embed(chunks):
        return []

    result = await process_document(
        "DOC101",
        pdf_path,
        "doc.pdf",
        fake_chunk_pages,
        empty_embed,
        session_factory=db,
    )

    assert result.status == "FAILED"
    assert "缺失向量" in (result.reason or "")

    doc = await get_document("DOC101", session_factory=db)
    assert doc.status == "FAILED"
    assert doc.error_message
    # 向量缺失 → 不写任何 C2 文本块（不能被检索）
    assert await get_document_chunks("DOC101", session_factory=db) == []


@pytest.mark.asyncio
async def test_pipeline_deleted_during_processing_not_resurrected(db, tmp_path) -> None:
    """Bug2 验收：处理中文件被删除 → 后台任务不重建 C1，文件不重新出现、不置 READY。"""
    pdf_path = write_text_pdf(tmp_path, "doc.pdf", "first", "second")

    await create_processing_document("DOC102", "doc.pdf", session_factory=db)
    # 模拟「处理期间被用户删除」：C1 已不存在
    assert await delete_document("DOC102", session_factory=db) is True
    assert await get_document("DOC102", session_factory=db) is None

    result = await process_document(
        "DOC102",
        pdf_path,
        "doc.pdf",
        fake_chunk_pages,
        fake_embed_chunks,
        session_factory=db,
    )

    assert result.status == "FAILED"
    assert "已被删除" in (result.reason or "")
    # 关键：文件没有重新出现
    assert await get_document("DOC102", session_factory=db) is None
    assert await get_document_chunks("DOC102", session_factory=db) == []
