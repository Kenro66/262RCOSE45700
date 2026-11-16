"""文档异常一致性：以真实文件和隔离数据库验证清理、恢复与重试。"""

import asyncio
from io import BytesIO
from pathlib import Path
import os
import tempfile
import threading

from fastapi import BackgroundTasks, FastAPI, UploadFile
import httpx
import pytest
from starlette.datastructures import Headers

from app.documents import service
from app.documents.pipeline import ProcessingResult
from app.storage.repo import create_processing_document, get_documents

DOCUMENT_ID = "DOC_CONSISTENCY"
PDF_BYTES = b"%PDF-1.7\noriginal-policy-content\n%%EOF"


async def prepare_delete(db, tmp_path, monkeypatch, *, with_file=True):
    monkeypatch.setattr(service, "UPLOAD_DIR", tmp_path)
    await create_processing_document(DOCUMENT_ID, "policy.pdf", session_factory=db)
    path = tmp_path / f"{DOCUMENT_ID}.pdf"
    if with_file:
        path.write_bytes(PDF_BYTES)
    app = FastAPI()
    app.include_router(service.create_documents_router(None, session_factory=db), prefix="/api")
    return app, path


def client_for(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("processor_raises", [False, True])
async def test_failed_status_write_always_cleans_original(tmp_path, monkeypatch, processor_raises):
    monkeypatch.setattr(service, "UPLOAD_DIR", tmp_path)

    async def generate_id():
        return DOCUMENT_ID

    async def create_record(*args, **kwargs):
        return object()

    async def processor(*args):
        if processor_raises:
            raise RuntimeError("处理器异常")
        return ProcessingResult(status="FAILED", reason="解析失败")

    async def fail_to_mark(*args):
        raise RuntimeError("状态写入失败")

    monkeypatch.setattr(service, "to_document_record", lambda record: record)
    router = service.create_documents_router(
        processor, generate_document_id=generate_id,
        create_processing_record=create_record, mark_failed=fail_to_mark,
    )
    endpoint = next(route.endpoint for route in router.routes if "POST" in route.methods)
    tasks = BackgroundTasks()
    await endpoint(tasks, UploadFile(
        BytesIO(PDF_BYTES), filename="policy.pdf",
        headers=Headers({"content-type": "application/pdf"}),
    ))
    with pytest.raises(RuntimeError, match="状态写入失败"):
        await tasks()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_database_delete_failure_restores_exact_pdf_and_allows_retry(db, tmp_path, monkeypatch):
    app, path = await prepare_delete(db, tmp_path, monkeypatch)
    original_delete = service.delete_document

    async def database_unavailable(*args, **kwargs):
        raise RuntimeError("数据库提交失败")

    monkeypatch.setattr(service, "delete_document", database_unavailable)
    async with client_for(app) as client:
        response = await client.delete(f"/api/documents/{DOCUMENT_ID}")
        assert response.status_code == 500
        assert path.read_bytes() == PDF_BYTES
        assert DOCUMENT_ID in await get_documents([DOCUMENT_ID], session_factory=db)
        assert list(tmp_path.iterdir()) == [path]

        monkeypatch.setattr(service, "delete_document", original_delete)
        response = await client.delete(f"/api/documents/{DOCUMENT_ID}")
    assert response.status_code == 204
    assert await get_documents([DOCUMENT_ID], session_factory=db) == {}
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_locked_pdf_keeps_database_and_original(db, tmp_path, monkeypatch):
    app, path = await prepare_delete(db, tmp_path, monkeypatch)
    original_unlink = Path.unlink

    def locked_unlink(candidate, *args, **kwargs):
        if candidate == path:
            raise PermissionError("原文件被占用")
        return original_unlink(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", locked_unlink)
    async with client_for(app) as client:
        response = await client.delete(f"/api/documents/{DOCUMENT_ID}")
    assert response.status_code == 503
    assert path.read_bytes() == PDF_BYTES
    assert DOCUMENT_ID in await get_documents([DOCUMENT_ID], session_factory=db)
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.asyncio
async def test_missing_pdf_still_allows_database_deletion(db, tmp_path, monkeypatch):
    app, path = await prepare_delete(db, tmp_path, monkeypatch, with_file=False)
    async with client_for(app) as client:
        response = await client.delete(f"/api/documents/{DOCUMENT_ID}")
    assert response.status_code == 204
    assert not path.exists()
    assert await get_documents([DOCUMENT_ID], session_factory=db) == {}
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_backup_failure_does_not_delete_file_or_record(db, tmp_path, monkeypatch):
    app, path = await prepare_delete(db, tmp_path, monkeypatch)

    def backup_unavailable(*args, **kwargs):
        raise OSError("没有空间创建删除备份")

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", backup_unavailable)
    async with client_for(app) as client:
        response = await client.delete(f"/api/documents/{DOCUMENT_ID}")
    assert response.status_code == 503
    assert path.read_bytes() == PDF_BYTES
    assert DOCUMENT_ID in await get_documents([DOCUMENT_ID], session_factory=db)
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.asyncio
async def test_failed_restore_keeps_recovery_copy_and_original_database_error(db, tmp_path, monkeypatch, caplog):
    app, path = await prepare_delete(db, tmp_path, monkeypatch)

    async def database_unavailable(*args, **kwargs):
        raise RuntimeError("数据库提交失败")

    def restore_unavailable(*args, **kwargs):
        raise PermissionError("恢复路径不可写")

    monkeypatch.setattr(service, "delete_document", database_unavailable)
    monkeypatch.setattr(os, "replace", restore_unavailable)
    async with client_for(app) as client:
        response = await client.delete(f"/api/documents/{DOCUMENT_ID}")
    assert response.status_code == 500
    assert DOCUMENT_ID in await get_documents([DOCUMENT_ID], session_factory=db)
    backups = list(tmp_path.iterdir())
    assert len(backups) == 1
    assert backups[0] != path
    assert backups[0].read_bytes() == PDF_BYTES
    assert "恢复" in caplog.text and str(backups[0]) in caplog.text


@pytest.mark.asyncio
async def test_concurrent_retry_waits_for_failed_delete_to_restore_pdf(db, tmp_path, monkeypatch):
    app, path = await prepare_delete(db, tmp_path, monkeypatch)
    original_delete = service.delete_document
    original_lookup = service.get_documents
    entered = asyncio.Event()
    release = asyncio.Event()
    deletes = 0
    lookups = 0

    async def lookup(*args, **kwargs):
        nonlocal lookups
        lookups += 1
        if lookups == 2:
            # 第二个请求必须在第一个请求恢复文件之后，才读取文档并开始删除。
            assert path.read_bytes() == PDF_BYTES
        return await original_lookup(*args, **kwargs)

    async def delete_once_then_retry(*args, **kwargs):
        nonlocal deletes
        deletes += 1
        if deletes == 1:
            entered.set()
            await release.wait()
            raise RuntimeError("第一次提交失败")
        return await original_delete(*args, **kwargs)

    monkeypatch.setattr(service, "get_documents", lookup)
    monkeypatch.setattr(service, "delete_document", delete_once_then_retry)
    async with client_for(app) as client:
        first = asyncio.create_task(client.delete(f"/api/documents/{DOCUMENT_ID}"))
        await asyncio.wait_for(entered.wait(), timeout=5)
        second = asyncio.create_task(client.delete(f"/api/documents/{DOCUMENT_ID}"))
        await asyncio.sleep(0)
        release.set()
        responses = await asyncio.wait_for(asyncio.gather(first, second), timeout=10)
    assert [response.status_code for response in responses] == [500, 204]
    assert await get_documents([DOCUMENT_ID], session_factory=db) == {}
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_count", [1, 2])
async def test_cancelled_delete_waits_for_unlink_before_restoring_pdf(
    db, tmp_path, monkeypatch, cancel_count
):
    app, path = await prepare_delete(db, tmp_path, monkeypatch)
    original_unlink = Path.unlink
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def delayed_unlink(candidate, *args, **kwargs):
        if candidate != path:
            return original_unlink(candidate, *args, **kwargs)
        started.set()
        if not release.wait(timeout=10):
            raise TimeoutError("测试未释放文件删除线程")
        try:
            return original_unlink(candidate, *args, **kwargs)
        finally:
            finished.set()

    async def database_unavailable(*args, **kwargs):
        raise RuntimeError("数据库提交失败")

    monkeypatch.setattr(Path, "unlink", delayed_unlink)
    monkeypatch.setattr(service, "delete_document", database_unavailable)
    async with client_for(app) as client:
        request = asyncio.create_task(client.delete(f"/api/documents/{DOCUMENT_ID}"))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            request.cancel()
            await asyncio.sleep(0)
            if cancel_count == 2:
                request.cancel()
            completed, _ = await asyncio.wait({request}, timeout=1)
            completed_before_unlink = bool(completed)
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await request
            assert await asyncio.to_thread(finished.wait, 5)
    # 请求取消不能让恢复提前于旧线程的 unlink，否则恢复出来的文件会再次被删除。
    assert not completed_before_unlink
    assert path.read_bytes() == PDF_BYTES
    assert DOCUMENT_ID in await get_documents([DOCUMENT_ID], session_factory=db)
    assert list(tmp_path.iterdir()) == [path]
