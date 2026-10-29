"""Issue #7 单文件自动化测试。

放进 backend/tests/ 后：python -m pytest tests/test_issue7.py -v
独立文件运行：python test_issue7.py --backend "你的项目/backend"
测试依赖：pytest pytest-asyncio asyncpg，以及项目 requirements.txt。
仅使用 PostgreSQL/pgvector，不读取 DATABASE_URL 作为测试目标。
必须设置 ISSUE7_TEST_DATABASE_URL，数据库名必须以 issue7_test 开头。
数据库测试会清空测试库的 documents、ParsedPage、chunks，不能指向业务库。
切分和向量化使用测试替身，不会下载模型，不代表真实模型验收。
已知异常路径保留普通断言，不以 xfail 隐藏失败。
依据：Issue7 d1adb42 + 团队 main 9bc2332。
"""
from __future__ import annotations

import os
from io import BytesIO
from fastapi import BackgroundTasks, UploadFile
from starlette.datastructures import Headers
import sys
from pathlib import Path

if __name__ == "__main__":
    import argparse
    import pytest
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", help="含 app 目录的 backend 绝对路径")
    options, pytest_args = parser.parse_known_args()
    if options.backend:
        os.environ["ISSUE7_BACKEND_DIR"] = str(Path(options.backend).resolve())
    raise SystemExit(pytest.main([str(Path(__file__).resolve()), "-v",
        "-o", "asyncio_default_fixture_loop_scope=function", *pytest_args]))

_backend_override = os.environ.get("ISSUE7_BACKEND_DIR")
_candidates = [Path(_backend_override)] if _backend_override else [
    Path(__file__).resolve().parent.parent, Path.cwd(), Path.cwd() / "backend"]
BACKEND_DIR = next((p.resolve() for p in _candidates if (p / "app/documents/service.py").is_file()), None)
if BACKEND_DIR is None:
    raise RuntimeError("找不到 Issue7 backend：使用 --backend 或设置 ISSUE7_BACKEND_DIR")
sys.path.insert(0, str(BACKEND_DIR))

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import NullPool

_TEST_URL = os.environ.get("ISSUE7_TEST_DATABASE_URL", "").strip()
if not _TEST_URL:
    raise RuntimeError("请设置 ISSUE7_TEST_DATABASE_URL，指向独立 PostgreSQL/pgvector 测试库")
if _TEST_URL.startswith("postgresql://"):
    _TEST_URL = _TEST_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
if not _TEST_URL.startswith("postgresql+asyncpg://"):
    raise RuntimeError("仅接受 PostgreSQL 测试连接串：postgresql:// 或 postgresql+asyncpg://")
if not (make_url(_TEST_URL).database or "").startswith("issue7_test"):
    raise RuntimeError("拒绝清理非测试数据库：数据库名必须以 issue7_test 开头")
os.environ["DATABASE_URL"] = _TEST_URL
import app.database as _database
# 在业务模块导入前配置独立测试会话，避免默认参数绑定业务数据库。
engine = create_async_engine(_TEST_URL, poolclass=NullPool)
_database.engine = engine
_database.SessionFactory = async_sessionmaker(engine, expire_on_commit=False)

@pytest_asyncio.fixture
async def db():
    """每个用例使用隔离数据；Postgres 仅允许明确指定的测试库。"""
    await _database.init_db()
    async with engine.begin() as conn:
        for name in ("chunks", "ParsedPage", "documents"):
            await conn.execute(text(f'DELETE FROM "{name}"'))
    yield _database.SessionFactory
    async with engine.begin() as conn:
        for name in ("chunks", "ParsedPage", "documents"):
            await conn.execute(text(f'DELETE FROM "{name}"'))
    await engine.dispose()


# ---- test_api_documents.py ----
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

def _make_chunk(document_id: str, index: int=0) -> ChunkData:
    return ChunkData(schema_version='1.0', chunk_id=f'{document_id}_C{index:06d}', document_id=document_id, chunk_index=index, page_index=0, page_number=1, text='chunk text', section_title=None, language='en', token_count=2, char_count=10, content_hash='sha256:test', embedding=[0.1] * 1024)

def build_client(db, monkeypatch, upload_dir: Path, processor):
    """按注入的 processor 组装 TestClient（id 生成/记录创建都走 db）。"""
    counter = {'n': 0}

    async def gen_id() -> str:
        counter['n'] += 1
        return f"DOC{counter['n']:03d}"
    from app.storage.repo import create_processing_document

    async def create_record(document_id: str, filename: str):
        return await create_processing_document(document_id, filename, session_factory=db)
    app = FastAPI()
    app.include_router(create_documents_router(processor, generate_document_id=gen_id, create_processing_record=create_record, session_factory=db), prefix='/api')
    monkeypatch.setattr(service, 'UPLOAD_DIR', upload_dir)
    return TestClient(app)

def test_upload_pdf_201_processing_then_ready(db, tmp_path, monkeypatch) -> None:
    """上传示例 PDF：立即 201 + C1(PROCESSING)；后台完成后 GET 变 READY。"""

    async def processor(document_id: str, pdf_path: Path, filename: str):
        await mark_document_ready(document_id, session_factory=db)
        return ProcessingResult(status='READY')
    client = build_client(db, monkeypatch, tmp_path, processor)
    response = client.post('/api/documents', files={'file': ('sample.pdf', make_pdf_bytes('hello'), 'application/pdf')})
    body = response.json()
    assert response.status_code == 201
    assert body['status'] == 'PROCESSING'
    assert body['error_message'] is None
    assert body['filename'] == 'sample.pdf'
    assert body['page_count'] is None
    assert body['document_id'] == 'DOC001'
    assert 'uploaded_at' in body
    assert (tmp_path / 'DOC001.pdf').exists()
    listing = client.get('/api/documents').json()
    assert [record['document_id'] for record in listing] == ['DOC001']
    assert listing[0]['status'] == 'READY'

def test_upload_txt_rejected_400_no_record(db, tmp_path, monkeypatch) -> None:
    """失败情况：上传 .docx/.txt 返回 400，列表里不出现这条记录。"""

    async def processor(document_id, pdf_path, filename):
        raise AssertionError('processor must not run for non-PDF')
    client = build_client(db, monkeypatch, tmp_path, processor)
    response = client.post('/api/documents', files={'file': ('sample.txt', b'not a pdf', 'text/plain')})
    assert response.status_code == 400
    assert response.json()['detail'] == '文件不是 PDF 类型'
    assert client.get('/api/documents').json() == []
    assert list(tmp_path.glob('*')) == []

def test_upload_fake_pdf_magic_rejected_400(db, tmp_path, monkeypatch) -> None:
    """content_type 伪装成 PDF 但魔数不对：400，不落文件不留记录。"""

    async def processor(document_id, pdf_path, filename):
        raise AssertionError('processor must not run for fake PDF')
    client = build_client(db, monkeypatch, tmp_path, processor)
    response = client.post('/api/documents', files={'file': ('fake.pdf', b'not a pdf at all', 'application/pdf')})
    assert response.status_code == 400
    assert response.json()['detail'] == '文件不是 PDF 类型'
    assert client.get('/api/documents').json() == []
    assert list(tmp_path.glob('*')) == []

def test_downstream_failure_marks_failed_and_deletes_file(db, tmp_path, monkeypatch) -> None:
    """后台处理失败：GET 列表变 FAILED + error_message，uploads/ 文件被删。"""

    async def processor(document_id, pdf_path, filename):
        return ProcessingResult(status='FAILED', reason='parser error')
    client = build_client(db, monkeypatch, tmp_path, processor)
    response = client.post('/api/documents', files={'file': ('sample.pdf', make_pdf_bytes('hello'), 'application/pdf')})
    assert response.status_code == 201
    assert response.json()['status'] == 'PROCESSING'
    listing = client.get('/api/documents').json()
    assert listing[0]['status'] == 'FAILED'
    assert listing[0]['error_message'] == 'parser error'
    assert list(tmp_path.glob('DOC*.pdf')) == []

def test_same_pdf_twice_two_records(db, tmp_path, monkeypatch) -> None:
    """同一份 PDF 上传两次：列表里有两条不同 document_id 的记录。"""

    async def processor(document_id, pdf_path, filename):
        await mark_document_ready(document_id, session_factory=db)
        return ProcessingResult(status='READY')
    client = build_client(db, monkeypatch, tmp_path, processor)
    payload = ('sample.pdf', make_pdf_bytes('same content'), 'application/pdf')
    first = client.post('/api/documents', files={'file': payload})
    second = client.post('/api/documents', files={'file': payload})
    assert first.status_code == 201
    assert second.status_code == 201
    ids = [record['document_id'] for record in client.get('/api/documents').json()]
    assert len(ids) == 2
    assert len(set(ids)) == 2

def test_delete_ready_document_cleans_db_chunks_file(db, tmp_path, monkeypatch) -> None:
    """删除 READY 文件：列表里没有它，chunks 表没有它的文本块，uploads/ 没有文件。"""

    async def processor(document_id, pdf_path, filename):
        await replace_document_chunks(document_id, [_make_chunk(document_id)], session_factory=db)
        await mark_document_ready(document_id, session_factory=db)
        return ProcessingResult(status='READY')
    client = build_client(db, monkeypatch, tmp_path, processor)
    response = client.post('/api/documents', files={'file': ('sample.pdf', make_pdf_bytes('hello'), 'application/pdf')})
    document_id = response.json()['document_id']
    assert client.get('/api/documents').json()[0]['status'] == 'READY'
    delete_response = client.delete(f'/api/documents/{document_id}')
    assert delete_response.status_code == 204
    assert client.get('/api/documents').json() == []
    assert list(tmp_path.glob('*.pdf')) == []

def test_delete_missing_document_404(db, tmp_path, monkeypatch) -> None:

    async def processor(document_id, pdf_path, filename):
        return ProcessingResult(status='READY')
    client = build_client(db, monkeypatch, tmp_path, processor)
    response = client.delete('/api/documents/DOC999')
    assert response.status_code == 404

def test_windows_filename_is_normalized(db, tmp_path, monkeypatch):

    async def processor(*args):
        return ProcessingResult(status='READY')
    client = build_client(db, monkeypatch, tmp_path, processor)
    response = client.post('/api/documents', files={'file': ('C:\\folder\\policy.pdf', make_pdf_bytes('text'), 'application/pdf')})
    assert response.status_code == 201
    assert response.json()['filename'] == 'policy.pdf'

def test_long_filename_rejected_before_saving(db, tmp_path, monkeypatch):

    async def processor(*args):
        raise AssertionError('must not process rejected upload')
    client = build_client(db, monkeypatch, tmp_path, processor)
    response = client.post('/api/documents', files={'file': ('a' * 256 + '.pdf', make_pdf_bytes('text'), 'application/pdf')})
    assert response.status_code == 400
    assert client.get('/api/documents').json() == []
    assert not list(tmp_path.glob('*.pdf'))

def test_id_allocation_failure_returns_503_and_closes_upload():
    import asyncio
    from io import BytesIO
    from fastapi import BackgroundTasks, HTTPException, UploadFile
    from starlette.datastructures import Headers

    async def broken_id():
        raise RuntimeError('database offline')

    async def processor(*args):
        raise AssertionError('must not process rejected upload')
    router = create_documents_router(processor, generate_document_id=broken_id)
    endpoint = next((route.endpoint for route in router.routes if 'POST' in route.methods))
    upload = UploadFile(BytesIO(b'%PDF-test'), filename='test.pdf', headers=Headers({'content-type': 'application/pdf'}))
    with pytest.raises(HTTPException) as caught:
        asyncio.run(endpoint(BackgroundTasks(), upload))
    assert caught.value.status_code == 503
    assert upload.file.closed

# ---- test_pipeline.py ----
import asyncio
import hashlib
import random
from pathlib import Path
import pymupdf
import pytest
from sqlalchemy import select
from app.contracts import Chunk, EmbeddingRecord
from app.documents.pipeline import process_document
from app.storage.models import ParsedPage
from app.storage.repo import create_processing_document, delete_document, get_document, get_document_chunks
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
        chunks.append(Chunk(schema_version='1.0', chunk_id=f'{page.document_id}_C{len(chunks):06d}', document_id=page.document_id, chunk_index=len(chunks), page_index=page.page_index, page_number=page.page_number, text=page.text.strip(), section_title=None, language=page.language, token_count=max(1, len(page.text) // 2), char_count=len(page.text), content_hash='sha256:test-chunk'))
    return chunks

def fake_embed_chunks(chunks) -> list[EmbeddingRecord]:
    """确定性伪向量：同一 text 永远得到同一向量（检索测试可复现）。"""
    records = []
    for chunk in chunks:
        seed = int.from_bytes(hashlib.sha256(chunk.text.encode('utf-8')).digest()[:8], 'big')
        rng = random.Random(seed)
        records.append(EmbeddingRecord(chunk_id=chunk.chunk_id, vector=[round(rng.random(), 6) for _ in range(EMBEDDING_DIMENSION)]))
    return records

@pytest.mark.asyncio
async def test_pipeline_ready_on_text_pdf(db, tmp_path) -> None:
    """3 页文本 PDF：READY、page_count=3、ParsedPage/C2 各 3 条且带向量。"""
    pdf_path = write_text_pdf(tmp_path, 'doc.pdf', 'first page', 'second', 'third')
    await create_processing_document('DOC001', 'doc.pdf', session_factory=db)
    result = await process_document('DOC001', pdf_path, 'doc.pdf', fake_chunk_pages, fake_embed_chunks, session_factory=db)
    assert result.status == 'READY'
    assert result.reason is None
    document = await get_document('DOC001', session_factory=db)
    assert document is not None
    assert document.status == 'READY'
    assert document.page_count == 3
    async with db() as session:
        pages = list((await session.scalars(select(ParsedPage).where(ParsedPage.document_id == 'DOC001').order_by(ParsedPage.page_index))).all())
    assert [page.page_number for page in pages] == [1, 2, 3]
    chunks = await get_document_chunks('DOC001', session_factory=db)
    assert [chunk.page_number for chunk in chunks] == [1, 2, 3]
    assert all((chunk.embedding is not None and len(chunk.embedding) == 1024 for chunk in chunks))

@pytest.mark.asyncio
async def test_pipeline_empty_chunks_failed_no_c2(db, tmp_path) -> None:
    """chunk_pages 返回空列表：整份 FAILED「未提取到文字」，C2 没有该文件的文本块。"""
    pdf_path = write_text_pdf(tmp_path, 'doc.pdf', 'some text')

    def empty_chunk_pages(pages):
        return []
    await create_processing_document('DOC002', 'doc.pdf', session_factory=db)
    result = await process_document('DOC002', pdf_path, 'doc.pdf', empty_chunk_pages, fake_embed_chunks, session_factory=db)
    assert result.status == 'FAILED'
    assert result.reason == '未提取到文字'
    document = await get_document('DOC002', session_factory=db)
    assert document.status == 'FAILED'
    assert document.error_message == '未提取到文字'
    assert document.page_count is None
    assert await get_document_chunks('DOC002', session_factory=db) == []

@pytest.mark.asyncio
async def test_pipeline_parse_failed_blank_pdf(db, tmp_path) -> None:
    """空白扫描件：Issue #6 的解析失败规则 → 整份 FAILED（分类提示），不写 ParsedPage/C2。"""
    document = pymupdf.open()
    for _ in range(3):
        document.new_page()
    pdf_path = tmp_path / 'blank.pdf'
    document.save(str(pdf_path))
    document.close()
    await create_processing_document('DOC003', 'blank.pdf', session_factory=db)
    result = await process_document('DOC003', pdf_path, 'blank.pdf', fake_chunk_pages, fake_embed_chunks, session_factory=db)
    assert result.status == 'FAILED'
    assert result.reason == '未检测到可提取文字，请上传包含文字的PDF文件。'
    doc = await get_document('DOC003', session_factory=db)
    assert doc.status == 'FAILED'
    assert doc.error_message
    async with db() as session:
        page_rows = await session.scalars(select(ParsedPage).where(ParsedPage.document_id == 'DOC003'))
        assert list(page_rows.all()) == []
    assert await get_document_chunks('DOC003', session_factory=db) == []

@pytest.mark.asyncio
async def test_pipeline_embed_raises_failed(db, tmp_path) -> None:
    """向量化异常：整份 FAILED，error_message 写异常信息。"""
    pdf_path = write_text_pdf(tmp_path, 'doc.pdf', 'text here')

    def broken_embed(chunks):
        raise RuntimeError('embedding service down')
    await create_processing_document('DOC004', 'doc.pdf', session_factory=db)
    result = await process_document('DOC004', pdf_path, 'doc.pdf', fake_chunk_pages, broken_embed, session_factory=db)
    assert result.status == 'FAILED'
    assert result.reason == 'embedding service down'
    doc = await get_document('DOC004', session_factory=db)
    assert doc.status == 'FAILED'
    assert doc.error_message == 'embedding service down'
    assert await get_document_chunks('DOC004', session_factory=db) == []

@pytest.mark.asyncio
async def test_pipeline_accepts_async_chunk_and_embed(db, tmp_path) -> None:
    """孙忠其的模块可能是 async 实现：流水线两种签名都兼容。"""
    pdf_path = write_text_pdf(tmp_path, 'doc.pdf', 'async page')

    async def async_chunk_pages(pages):
        return fake_chunk_pages(pages)

    async def async_embed(chunks):
        return fake_embed_chunks(chunks)
    await create_processing_document('DOC005', 'doc.pdf', session_factory=db)
    result = await process_document('DOC005', pdf_path, 'doc.pdf', async_chunk_pages, async_embed, session_factory=db)
    assert result.status == 'READY'
    chunks = await get_document_chunks('DOC005', session_factory=db)
    assert len(chunks) == 1

@pytest.mark.asyncio
async def test_pipeline_embed_returns_empty_failed(db, tmp_path) -> None:
    """Bug1 验收：embed_chunks 返回空列表 → 整份 FAILED（缺失向量），不置 READY、无法检索。"""
    pdf_path = write_text_pdf(tmp_path, 'doc.pdf', 'text here')
    await create_processing_document('DOC101', 'doc.pdf', session_factory=db)

    def empty_embed(chunks):
        return []
    result = await process_document('DOC101', pdf_path, 'doc.pdf', fake_chunk_pages, empty_embed, session_factory=db)
    assert result.status == 'FAILED'
    assert '缺失向量' in (result.reason or '')
    doc = await get_document('DOC101', session_factory=db)
    assert doc.status == 'FAILED'
    assert doc.error_message
    assert await get_document_chunks('DOC101', session_factory=db) == []

@pytest.mark.asyncio
async def test_pipeline_deleted_during_processing_not_resurrected(db, tmp_path) -> None:
    """Bug2 验收：处理中文件被删除 → 后台任务不重建 C1，文件不重新出现、不置 READY。"""
    pdf_path = write_text_pdf(tmp_path, 'doc.pdf', 'first', 'second')
    await create_processing_document('DOC102', 'doc.pdf', session_factory=db)
    assert await delete_document('DOC102', session_factory=db) is True
    assert await get_document('DOC102', session_factory=db) is None
    result = await process_document('DOC102', pdf_path, 'doc.pdf', fake_chunk_pages, fake_embed_chunks, session_factory=db)
    assert result.status == 'FAILED'
    assert '已被删除' in (result.reason or '')
    assert await get_document('DOC102', session_factory=db) is None
    assert await get_document_chunks('DOC102', session_factory=db) == []

@pytest.mark.asyncio
async def test_sync_impl_runs_in_worker_thread_not_event_loop(db, tmp_path) -> None:
    """复审点2：同步实现（CPU 密集，如真实 BGE-M3）必须在工作线程执行，不阻塞事件循环。

    断言方法：函数执行时探测当前线程的 running event loop——
    若在事件循环上直接执行，get_running_loop() 会返回主循环；
    若已放到工作线程（run_in_threadpool），工作线程没有事件循环 → RuntimeError。
    """
    pdf_path = write_text_pdf(tmp_path, 'doc.pdf', 'thread check')
    loop_state = {}

    def sync_chunk_pages(pages):
        try:
            asyncio.get_running_loop()
            loop_state['chunk'] = 'event_loop'
        except RuntimeError:
            loop_state['chunk'] = 'worker_thread'
        return fake_chunk_pages(pages)

    def sync_embed(chunks):
        try:
            asyncio.get_running_loop()
            loop_state['embed'] = 'event_loop'
        except RuntimeError:
            loop_state['embed'] = 'worker_thread'
        return fake_embed_chunks(chunks)
    await create_processing_document('DOC103', 'doc.pdf', session_factory=db)
    result = await process_document('DOC103', pdf_path, 'doc.pdf', sync_chunk_pages, sync_embed, session_factory=db)
    assert result.status == 'READY'
    assert loop_state.get('chunk') == 'worker_thread'
    assert loop_state.get('embed') == 'worker_thread'

# ---- test_storage_repo.py ----
from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from app.storage.models import Chunk as StorageChunk, Document, ParsedPage as StorageParsedPage
from app.storage.repo import build_search_similar_stmt, create_processing_document, delete_document, get_documents, list_documents, mark_document_failed, mark_document_ready, replace_document_chunks
from app.storage.schemas import ChunkData

async def _storage_make_chunk(document_id: str, index: int=0) -> ChunkData:
    return ChunkData(schema_version='1.0', chunk_id=f'{document_id}_C{index:06d}', document_id=document_id, chunk_index=index, page_index=0, page_number=1, text=f'chunk {index}', section_title=None, language='en', token_count=1, char_count=7, content_hash='sha256:test', embedding=[0.2] * 1024)

@pytest.mark.asyncio
async def test_get_documents_skips_missing_ids(db) -> None:
    """get_documents：存在的 id 有结果；不存在的 id 不报错，只是不在结果里。"""
    await create_processing_document('DOC001', 'a.pdf', session_factory=db)
    await create_processing_document('DOC002', 'b.pdf', session_factory=db)
    result = await get_documents(['DOC001', 'DOC002', 'NOPE'], session_factory=db)
    assert set(result.keys()) == {'DOC001', 'DOC002'}
    assert result['DOC001'].filename == 'a.pdf'
    assert result['DOC002'].status.value == 'PROCESSING'
    assert await get_documents([], session_factory=db) == {}

@pytest.mark.asyncio
async def test_list_documents_newest_first(db) -> None:
    """list_documents：C1 列表，最新在前。"""
    await create_processing_document('DOC001', 'a.pdf', session_factory=db)
    await create_processing_document('DOC002', 'b.pdf', session_factory=db)
    now = datetime.now(timezone.utc)
    async with db() as session:
        old = await session.get(Document, 'DOC001')
        old.uploaded_at = now - timedelta(hours=1)
        await session.commit()
    listing = await list_documents(session_factory=db)
    assert [record.document_id for record in listing] == ['DOC002', 'DOC001']

@pytest.mark.asyncio
async def test_delete_document_cascades_pages_and_chunks(db) -> None:
    """delete_document：C1、ParsedPage、C2 一起删掉。"""
    await create_processing_document('DOC001', 'a.pdf', session_factory=db)
    async with db() as session:
        session.add(StorageParsedPage(document_id='DOC001', page_index=0, page_number=1, text='page one'))
        await session.commit()
    await replace_document_chunks('DOC001', [await _storage_make_chunk('DOC001', 0), await _storage_make_chunk('DOC001', 1)], session_factory=db)
    assert await delete_document('DOC001', session_factory=db) is True
    assert await delete_document('DOC001', session_factory=db) is False
    async with db() as session:
        assert await session.get(Document, 'DOC001') is None
        pages = await session.scalars(select(StorageParsedPage))
        chunks = await session.scalars(select(StorageChunk))
        assert list(pages.all()) == []
        assert list(chunks.all()) == []

@pytest.mark.asyncio
async def test_mark_ready_and_failed(db) -> None:
    await create_processing_document('DOC001', 'a.pdf', session_factory=db)
    await mark_document_failed('DOC001', 'boom', session_factory=db)
    async with db() as session:
        document = await session.get(Document, 'DOC001')
    assert document.status == 'FAILED'
    assert document.error_message == 'boom'
    await mark_document_ready('DOC001', session_factory=db)
    async with db() as session:
        document = await session.get(Document, 'DOC001')
    assert document.status == 'READY'
    assert document.error_message is None

@pytest.mark.asyncio
async def test_replace_document_chunks_replaces_all(db) -> None:
    await create_processing_document('DOC001', 'a.pdf', session_factory=db)
    await replace_document_chunks('DOC001', [await _storage_make_chunk('DOC001', 0), await _storage_make_chunk('DOC001', 1)], session_factory=db)
    await replace_document_chunks('DOC001', [await _storage_make_chunk('DOC001', 0)], session_factory=db)
    async with db() as session:
        rows = await session.scalars(select(StorageChunk))
    assert [row.chunk_id for row in rows.all()] == ['DOC001_C000000']
    with pytest.raises(ValueError):
        await replace_document_chunks('DOC001', [await _storage_make_chunk('DOC002')], session_factory=db)
    with pytest.raises(ValueError):
        await replace_document_chunks('NOPE', [], session_factory=db)

@pytest.mark.asyncio
async def test_search_stmt_compiles_for_postgres(db) -> None:
    """search_similar 的语句按 Postgres/pgvector 编译：cosine 距离、只查 READY、limit。"""
    stmt = build_search_similar_stmt([0.0] * 8, top_k=3)
    sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={'literal_binds': True}))
    assert '<=>' in sql
    assert "'READY'" in sql
    assert 'LIMIT 3' in sql

# ---- test_health.py ----
import asyncpg
import httpx
import pytest
from fastapi import FastAPI
from app.main import app

class _FakeConn:

    def __init__(self, fetchrow_result=None, fetchrow_exc=None, close_exc=None):
        self._result = fetchrow_result
        self._exc = fetchrow_exc
        self._close_exc = close_exc
        self.closed = False

    async def fetchrow(self, *args, **kwargs):
        if self._exc is not None:
            raise self._exc
        return self._result

    async def close(self):
        if self._close_exc is not None:
            raise self._close_exc
        self.closed = True

async def _get_health(app: FastAPI) -> tuple[int, dict]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url='http://testserver') as client:
        response = await client.get('/health')
        return (response.status_code, response.json())

@pytest.mark.asyncio
async def test_health_200_when_database_ok(monkeypatch) -> None:
    """连接＋查询都成功 → 200，返回版本与 vector 扩展状态，且连接被释放。"""
    created = {}

    async def fake_connect(url, timeout=None):
        conn = _FakeConn(fetchrow_result=('16.4', True))
        created['conn'] = conn
        return conn
    monkeypatch.setattr(asyncpg, 'connect', fake_connect)
    status, body = await _get_health(app)
    assert status == 200
    assert body['status'] == 'ok'
    assert body['database'] == {'version': '16.4', 'vector_extension': True}
    assert created['conn'].closed is True

@pytest.mark.asyncio
async def test_health_503_when_connect_fails(monkeypatch) -> None:
    """数据库没启动（连接失败）→ 503 带明确错误，不静默 200、不出 500。"""

    async def fake_connect(url, timeout=None):
        raise OSError('connection refused（模拟数据库未启动）')
    monkeypatch.setattr(asyncpg, 'connect', fake_connect)
    status, body = await _get_health(app)
    assert status == 503
    assert body['status'] == 'error'
    assert '连接失败' in body['database']

@pytest.mark.asyncio
async def test_health_503_when_query_fails(monkeypatch) -> None:
    """连接成功但 SQL 查询失败 → 同样 503（查询失败视为数据库不可用）。"""

    async def fake_connect(url, timeout=None):
        return _FakeConn(fetchrow_exc=RuntimeError('模拟查询失败'))
    monkeypatch.setattr(asyncpg, 'connect', fake_connect)
    status, body = await _get_health(app)
    assert status == 503
    assert body['status'] == 'error'
    assert '查询失败' in body['database']

@pytest.mark.asyncio
async def test_health_close_failure_does_not_mask_success(monkeypatch) -> None:
    """关闭失败不掩盖成功结果：仍 200。"""

    async def fake_connect(url, timeout=None):
        return _FakeConn(fetchrow_result=('16.4', False), close_exc=RuntimeError('模拟关闭失败'))
    monkeypatch.setattr(asyncpg, 'connect', fake_connect)
    status, body = await _get_health(app)
    assert status == 200
    assert body['database']['vector_extension'] is False

@pytest.mark.asyncio
async def test_health_close_failure_does_not_mask_503(monkeypatch) -> None:
    """双重失败（查询失败＋关闭失败）：仍回原始查询失败 503，不出 500。"""

    async def fake_connect(url, timeout=None):
        return _FakeConn(fetchrow_exc=RuntimeError('模拟查询失败'), close_exc=RuntimeError('模拟关闭失败'))
    monkeypatch.setattr(asyncpg, 'connect', fake_connect)
    status, body = await _get_health(app)
    assert status == 503
    assert '查询失败' in body['database']

# ---- test_e2e_postgres.py ----
import asyncio
import os
import random
import sys
import httpx
import pytest
from sqlalchemy import text
from app.contracts import Chunk, EmbeddingRecord
from app.database import engine, init_db
from app.main import create_app
from app.storage.repo import get_documents, next_document_id, search_similar

def _fake_chunk_pages(pages) -> list[Chunk]:
    chunks = []
    for page in pages:
        if not page.text.strip():
            continue
        chunks.append(Chunk(schema_version='1.0', chunk_id=f'{page.document_id}_C{len(chunks):06d}', document_id=page.document_id, chunk_index=len(chunks), page_index=page.page_index, page_number=page.page_number, text=f'chunk of {page.document_id} page {page.page_number}: {page.text.strip()[:80]}', section_title=None, language=page.language, token_count=8, char_count=len(page.text), content_hash='sha256:e2e'))
    return chunks

def _fake_embed(chunks) -> list[EmbeddingRecord]:
    records = []
    for chunk in chunks:
        rng = random.Random(abs(hash(chunk.text)) % 2 ** 32)
        records.append(EmbeddingRecord(chunk_id=chunk.chunk_id, vector=[round(rng.random(), 6) for _ in range(1024)]))
    return records

def _make_pdf(name: str, *lines: str) -> bytes:
    import pymupdf
    doc = pymupdf.open()
    for line in lines:
        page = doc.new_page()
        page.insert_text((72, 72), line)
    data = doc.tobytes()
    doc.close()
    return data

async def _reset_db() -> None:
    """建表（冪等）+ 清空，讓 e2e 從乾淨狀態跑。"""
    await init_db()
    async with engine.begin() as conn:
        await conn.execute(text('TRUNCATE "documents", "chunks", "ParsedPage" RESTART IDENTITY CASCADE'))

@pytest.mark.asyncio
async def test_postgres_e2e(tmp_path, monkeypatch) -> None:
    """id 序列 + 上傳 → READY → search_similar top_k 有序 → 刪除後 chunk 消失 → get_documents。"""
    from app.documents import service
    monkeypatch.setattr(service, 'UPLOAD_DIR', tmp_path)
    await _reset_db()
    first = await next_document_id()
    second = await next_document_id()
    assert first.startswith('DOC') and second.startswith('DOC')
    assert int(first[3:]) + 1 == int(second[3:])
    app = create_app(_fake_chunk_pages, _fake_embed)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url='http://testserver') as client:
        health = (await client.get('/health')).json()
        assert health['status'] == 'ok'
        assert health['database']['version']
        assert health['database']['vector_extension'] is True
        for name, lines in (('alpha.pdf', ('alpha paragraph one', 'alpha paragraph two')), ('beta.pdf', ('beta paragraph one',)), ('gamma.pdf', ('gamma paragraph one', 'gamma paragraph two'))):
            response = await client.post('/api/documents', files={'file': (name, _make_pdf(name, *lines), 'application/pdf')})
            assert response.status_code == 201, response.text
        listing = (await client.get('/api/documents')).json()
        assert {record['status'] for record in listing} == {'READY'}
        assert all((record['page_count'] for record in listing))
        alpha = next((r for r in listing if r['filename'] == 'alpha.pdf'))
        assert alpha['page_count'] == 2
        query = [0.25 if i % 3 == 0 else 0.05 for i in range(1024)]
        hits = await search_similar(query, top_k=10)
        assert 0 < len(hits) <= 10
        scores = [hit.score for hit in hits]
        assert scores == sorted(scores, reverse=True)
        delete_response = await client.delete(f"/api/documents/{alpha['document_id']}")
        assert delete_response.status_code == 204
        hits_after = await search_similar(query, top_k=10)
        assert all((hit.document_id != alpha['document_id'] for hit in hits_after))
        remaining = (await client.get('/api/documents')).json()
        fetched = await get_documents([r['document_id'] for r in remaining] + ['NOPE'])
        assert set(fetched.keys()) == {r['document_id'] for r in remaining}

@pytest.mark.asyncio
async def test_failure_marker_error_still_cleans_original(tmp_path, monkeypatch):
    monkeypatch.setattr(service, 'UPLOAD_DIR', tmp_path)

    async def gen():
        return 'DOC900'

    async def create(*args):
        return object()

    async def processor(*args):
        return ProcessingResult(status='FAILED', reason='processing failed')

    async def fail(*args):
        raise RuntimeError('database unavailable')
    monkeypatch.setattr(service, 'to_document_record', lambda record: record)
    router = service.create_documents_router(processor, generate_document_id=gen, create_processing_record=create, mark_failed=fail)
    endpoint = next((r.endpoint for r in router.routes if 'POST' in r.methods))
    tasks = BackgroundTasks()
    await endpoint(tasks, UploadFile(BytesIO(b'%PDF-test'), filename='x.pdf', headers=Headers({'content-type': 'application/pdf'})))
    with pytest.raises(RuntimeError, match='database unavailable'):
        await tasks()
    assert not (tmp_path / 'DOC900.pdf').exists(), 'FAILED status write error skips original file cleanup'

@pytest.mark.asyncio
async def test_database_delete_failure_preserves_original(tmp_path, monkeypatch):
    monkeypatch.setattr(service, 'UPLOAD_DIR', tmp_path)
    path = tmp_path / 'DOC901.pdf'
    path.write_bytes(b'%PDF-test')

    async def get(*args, **kwargs):
        return {'DOC901': object()}

    async def delete(*args, **kwargs):
        raise RuntimeError('database commit failed')
    monkeypatch.setattr(service, 'get_documents', get)
    monkeypatch.setattr(service, 'delete_document', delete)
    router = service.create_documents_router(None)
    endpoint = next((r.endpoint for r in router.routes if 'DELETE' in r.methods))
    with pytest.raises(RuntimeError, match='database commit failed'):
        await endpoint('DOC901')
    assert path.exists(), 'DB still has document but original PDF was already removed'
