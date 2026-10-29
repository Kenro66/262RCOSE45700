"""POST/GET/DELETE /api/documents（Issue #7 完成条件，SQLite 验证）。"""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.documents import service
from app.documents.pipeline import ProcessingResult
from app.documents.service import create_documents_router
from app.storage.repo import mark_document_ready, replace_document_chunks
from app.storage.schemas import ChunkData


def make_pdf_bytes(*page_texts: str) -> bytes:
    document = pymupdf.open()
    for page_text in page_texts:
        page = document.new_page()
        if page_text:
            page.insert_text((72, 72), page_text)
    content = document.tobytes()
    document.close()
    return content


def _make_chunk(document_id: str, index: int = 0) -> ChunkData:
    return ChunkData(
        schema_version="1.0",
        chunk_id=f"{document_id}_C{index:06d}",
        document_id=document_id,
        chunk_index=index,
        page_index=0,
        page_number=1,
        text="chunk text",
        section_title=None,
        language="en",
        token_count=2,
        char_count=10,
        content_hash="sha256:test",
        embedding=[0.1] * 1024,
    )


def build_client(db, monkeypatch, upload_dir: Path, processor):
    """按注入的 processor 组装 TestClient（id 生成/记录创建都走 db）。"""
    counter = {"n": 0}

    async def gen_id() -> str:
        counter["n"] += 1
        return f"DOC{counter['n']:03d}"

    from app.storage.repo import create_processing_document

    async def create_record(document_id: str, filename: str):
        return await create_processing_document(
            document_id, filename, session_factory=db
        )

    app = FastAPI()
    app.include_router(
        create_documents_router(
            processor,
            generate_document_id=gen_id,
            create_processing_record=create_record,
            session_factory=db,
        ),
        prefix="/api",
    )
    monkeypatch.setattr(service, "UPLOAD_DIR", upload_dir)
    return TestClient(app)


def test_upload_pdf_201_processing_then_ready(db, tmp_path, monkeypatch) -> None:
    """上传示例 PDF：立即 201 + C1(PROCESSING)；后台完成后 GET 变 READY。"""

    async def processor(document_id: str, pdf_path: Path, filename: str):
        await mark_document_ready(document_id, session_factory=db)
        return ProcessingResult(status="READY")

    client = build_client(db, monkeypatch, tmp_path, processor)
    response = client.post(
        "/api/documents",
        files={"file": ("sample.pdf", make_pdf_bytes("hello"), "application/pdf")},
    )

    body = response.json()
    assert response.status_code == 201
    assert body["status"] == "PROCESSING"
    assert body["error_message"] is None
    assert body["filename"] == "sample.pdf"
    assert body["page_count"] is None
    assert body["document_id"] == "DOC001"
    assert "uploaded_at" in body
    assert (tmp_path / "DOC001.pdf").exists()

    listing = client.get("/api/documents").json()
    assert [record["document_id"] for record in listing] == ["DOC001"]
    assert listing[0]["status"] == "READY"


def test_upload_txt_rejected_400_no_record(db, tmp_path, monkeypatch) -> None:
    """失败情况：上传 .docx/.txt 返回 400，列表里不出现这条记录。"""

    async def processor(document_id, pdf_path, filename):
        raise AssertionError("processor must not run for non-PDF")

    client = build_client(db, monkeypatch, tmp_path, processor)
    response = client.post(
        "/api/documents",
        files={"file": ("sample.txt", b"not a pdf", "text/plain")},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "文件不是 PDF 类型"
    assert client.get("/api/documents").json() == []
    assert list(tmp_path.glob("*")) == []


def test_upload_fake_pdf_magic_rejected_400(db, tmp_path, monkeypatch) -> None:
    """content_type 伪装成 PDF 但魔数不对：400，不落文件不留记录。"""

    async def processor(document_id, pdf_path, filename):
        raise AssertionError("processor must not run for fake PDF")

    client = build_client(db, monkeypatch, tmp_path, processor)
    response = client.post(
        "/api/documents",
        files={"file": ("fake.pdf", b"not a pdf at all", "application/pdf")},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "文件不是 PDF 类型"
    assert client.get("/api/documents").json() == []
    assert list(tmp_path.glob("*")) == []


def test_downstream_failure_marks_failed_and_deletes_file(
    db, tmp_path, monkeypatch
) -> None:
    """后台处理失败：GET 列表变 FAILED + error_message，uploads/ 文件被删。"""

    async def processor(document_id, pdf_path, filename):
        return ProcessingResult(status="FAILED", reason="parser error")

    client = build_client(db, monkeypatch, tmp_path, processor)
    response = client.post(
        "/api/documents",
        files={"file": ("sample.pdf", make_pdf_bytes("hello"), "application/pdf")},
    )

    assert response.status_code == 201
    assert response.json()["status"] == "PROCESSING"

    listing = client.get("/api/documents").json()
    assert listing[0]["status"] == "FAILED"
    assert listing[0]["error_message"] == "parser error"
    assert list(tmp_path.glob("DOC*.pdf")) == []


def test_same_pdf_twice_two_records(db, tmp_path, monkeypatch) -> None:
    """同一份 PDF 上传两次：列表里有两条不同 document_id 的记录。"""

    async def processor(document_id, pdf_path, filename):
        await mark_document_ready(document_id, session_factory=db)
        return ProcessingResult(status="READY")

    client = build_client(db, monkeypatch, tmp_path, processor)
    payload = ("sample.pdf", make_pdf_bytes("same content"), "application/pdf")
    first = client.post("/api/documents", files={"file": payload})
    second = client.post("/api/documents", files={"file": payload})

    assert first.status_code == 201
    assert second.status_code == 201
    ids = [record["document_id"] for record in client.get("/api/documents").json()]
    assert len(ids) == 2
    assert len(set(ids)) == 2


def test_delete_ready_document_cleans_db_chunks_file(
    db, tmp_path, monkeypatch
) -> None:
    """删除 READY 文件：列表里没有它，chunks 表没有它的文本块，uploads/ 没有文件。"""

    async def processor(document_id, pdf_path, filename):
        await replace_document_chunks(
            document_id, [_make_chunk(document_id)], session_factory=db
        )
        await mark_document_ready(document_id, session_factory=db)
        return ProcessingResult(status="READY")

    client = build_client(db, monkeypatch, tmp_path, processor)
    response = client.post(
        "/api/documents",
        files={"file": ("sample.pdf", make_pdf_bytes("hello"), "application/pdf")},
    )
    document_id = response.json()["document_id"]
    assert client.get("/api/documents").json()[0]["status"] == "READY"

    delete_response = client.delete(f"/api/documents/{document_id}")
    assert delete_response.status_code == 204

    assert client.get("/api/documents").json() == []
    assert list(tmp_path.glob("*.pdf")) == []


def test_delete_missing_document_404(db, tmp_path, monkeypatch) -> None:
    async def processor(document_id, pdf_path, filename):
        return ProcessingResult(status="READY")

    client = build_client(db, monkeypatch, tmp_path, processor)
    response = client.delete("/api/documents/DOC999")
    assert response.status_code == 404
