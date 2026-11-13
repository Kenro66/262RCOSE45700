
"""
文件上传服务（Issue #7）。

POST   /api/documents          上传 PDF（multipart，字段名 file），立即 201 + C1(PROCESSING)
GET    /api/documents          C1 列表（最新在前）
DELETE /api/documents/{id}     删除 DB 记录（级联 ParsedPage/C2）与 uploads/ 原文件

上传校验（与 Issue #6 失败规则一致）：
- content_type 接受 application/pdf 或 application/x-pdf，否则 400「文件不是 PDF 类型」
- 魔数必须是 %PDF-，否则 400「文件不是 PDF 类型」
- 非空、不超过 25MB

上传成功即返回 status=PROCESSING 的 C1；

Issue #45：手动上传流程不变；按完整 PDF 原始字节的 SHA-256 匹配登记信息，
在创建 PROCESSING 记录时写入四项适用条件，并随首次 POST 响应返回。
未匹配时四项均为 null，表示未登记或未确认，不表示不适用。

"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any
from weakref import WeakValueDictionary

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, Response, UploadFile, status
from starlette.concurrency import run_in_threadpool

from app.contracts import DocumentRecord
from app.database import SessionFactory
from app.documents.pipeline import ProcessingResult
from app.documents.metadata import lookup_metadata
from app.storage.repo import (
    create_processing_document,
    delete_document,
    get_documents,
    list_documents,
    mark_document_failed,
    next_document_id,
    to_document_record,
)

UPLOAD_DIR = Path(__file__).resolve().parents[2] / "uploads"
MAX_PDF_BYTES = 25 * 1024 * 1024
CHUNK_SIZE = 1024 * 1024
ERROR_NOT_PDF = "文件不是 PDF 类型"

logger = logging.getLogger(__name__)

DownstreamProcessor = Callable[[str, Path, str], Awaitable[ProcessingResult]]
DocumentIdGenerator = Callable[[], Awaitable[str]]
ProcessingRecordCreator = Callable[..., Awaitable[Any]]
FailureMarker = Callable[[str, str], Awaitable[None]]
SessionMaker = Callable[[], Any]


async def _save_pdf(upload: UploadFile, destination: Path) -> str:
    """流式保存并校验 PDF，返回完整文件原始字节的 SHA-256（#45）。

    校验通过后才发布正式文件；哈希不依赖文件名或解析后的文本。
    """
    temporary = destination.with_suffix(".uploading")
    digest = hashlib.sha256()
    total_bytes = 0
    first_chunk = True

    await run_in_threadpool(destination.parent.mkdir, parents=True, exist_ok=True)

    try:
        with temporary.open("xb") as output:
            while chunk := await upload.read(CHUNK_SIZE):
                if first_chunk:
                    first_chunk = False
                    if not chunk.startswith(b"%PDF-"):
                        raise ValueError(ERROR_NOT_PDF)

                # #45：每个原始字节块参与摘要，不额外读取整份 PDF 到内存。
                digest.update(chunk)
                total_bytes += len(chunk)
                if total_bytes > MAX_PDF_BYTES:
                    raise ValueError(
                        f"PDF exceeds the {MAX_PDF_BYTES // (1024 * 1024)} MB limit"
                    )
                await run_in_threadpool(output.write, chunk)

        if total_bytes == 0:
            raise ValueError("Uploaded PDF is empty")

        await run_in_threadpool(os.replace, temporary, destination)
        return digest.hexdigest()
    except BaseException:
        await _delete_if_present(temporary)
        raise
    finally:
        await upload.close()


async def _delete_if_present(path: Path, *, strict: bool = False) -> None:
    """删除原文件；主动删除必须成功，失败流程的清理不掩盖原始错误。"""
    try:
        await run_in_threadpool(path.unlink, missing_ok=True)
    except OSError as exc:
        if strict:
            raise
        logger.warning("删除文件失败 %s（DB 为准，请人工清理孤儿文件）：%s", path, exc)


def _create_delete_backup(path: Path) -> Path | None:
    """删除前在同目录保留可恢复副本；原文件不存在时无需备份。"""
    try:
        source = path.open("rb")
    except FileNotFoundError:
        return None

    backup = None
    try:
        with source, tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.stem}.", suffix=".delete-backup", delete=False
        ) as output:
            backup = Path(output.name)
            shutil.copyfileobj(source, output, length=CHUNK_SIZE)
    except BaseException:
        if backup is not None:
            try:
                backup.unlink(missing_ok=True)
            except OSError:
                logger.exception("清理不完整的删除备份失败：%s", backup)
        raise
    return backup


async def _delete_record_and_pdf(
    document_id: str, pdf_path: Path, session_factory: SessionMaker
) -> None:
    """文件删除失败保留记录；数据库删除失败则恢复原始 PDF。"""
    try:
        backup = await run_in_threadpool(_create_delete_backup, pdf_path)
    except OSError as exc:
        logger.warning("无法备份原文件，保留文件与数据库记录 %s：%s", document_id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="原文件暂时无法删除，请稍后重试",
        ) from exc

    cleanup_backup = True
    restore_needed = True
    try:
        try:
            await _delete_if_present(pdf_path, strict=True)
        except OSError as exc:
            restore_needed = False
            logger.warning("原文件删除失败，保留数据库记录 %s：%s", document_id, exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="原文件暂时无法删除，请稍后重试",
            ) from exc
        await delete_document(document_id, session_factory=session_factory)
    except BaseException:
        if backup is not None and restore_needed:
            # 先保留备份；恢复再次失败或被取消时，不清理唯一剩余的原文副本。
            cleanup_backup = False
            try:
                await run_in_threadpool(os.replace, backup, pdf_path)
            except OSError:
                logger.exception("恢复原始 PDF 失败 %s；恢复副本保留在 %s", document_id, backup)
            else:
                cleanup_backup = True
        raise
    finally:
        if backup is not None and cleanup_backup:
            await _delete_if_present(backup)


async def _finish_delete_before_cancellation(
    document_id: str, pdf_path: Path, session_factory: SessionMaker
) -> None:
    """收到取消也等待删除与恢复结束，避免旧线程删除刚恢复的文件。"""
    deletion = asyncio.create_task(
        _delete_record_and_pdf(document_id, pdf_path, session_factory)
    )
    cancellation = None
    while not deletion.done():
        try:
            await asyncio.shield(deletion)
        except asyncio.CancelledError as exc:
            if deletion.cancelled():
                raise
            cancellation = exc
        except Exception:
            break
    try:
        deletion.result()
    except Exception:
        if cancellation is None:
            raise
        logger.exception("请求取消期间删除失败，已完成恢复或保留恢复副本：%s", document_id)
    if cancellation is not None:
        raise cancellation


def create_documents_router(
    process_document: DownstreamProcessor,
    generate_document_id: DocumentIdGenerator = next_document_id,
    create_processing_record: ProcessingRecordCreator = create_processing_document,
    mark_failed: FailureMarker = mark_document_failed,
    session_factory: SessionMaker = SessionFactory,
) -> APIRouter:
    """构建 /api/documents 路由，处理实现由 app（pipeline）注入。"""
    router = APIRouter(prefix="/documents", tags=["documents"])
    # 当前后端使用单进程；同一文档的并发删除必须等待前一请求完成恢复。
    delete_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    @router.post(
        "", response_model=DocumentRecord, status_code=status.HTTP_201_CREATED
    )
    async def upload_document(
        background_tasks: BackgroundTasks,
        file: Annotated[UploadFile, File(description="PDF document")],
    ) -> DocumentRecord:
        filename = (file.filename or "document.pdf").replace("\\", "/").rsplit("/", 1)[-1] or "document.pdf"
        if len(filename) > 255:
            await file.close()
            raise HTTPException(status_code=400, detail="文件名不能超过 255 个字符")

        if file.content_type not in {"application/pdf", "application/x-pdf"}:
            await file.close()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=ERROR_NOT_PDF
            )

        try:
            document_id = await generate_document_id()
        except Exception as exc:
            await file.close()
            logger.exception("分配文档编号失败")
            raise HTTPException(status_code=503, detail="数据库暂时不可用，请稍后重试") from exc
        pdf_path = UPLOAD_DIR / f"{document_id}.pdf"

        try:
            digest = await _save_pdf(file, pdf_path)
            # #45：先匹配登记，再随文档记录一起写入，确保首次响应已有四项条件。
            # 请求仍然只提交 file；未匹配时 lookup_metadata 返回四项空值。
            record = await create_processing_record(
                document_id, filename, metadata=lookup_metadata(digest)
            )
        except ValueError as exc:
            await _delete_if_present(pdf_path)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc
        except Exception as exc:
            await _delete_if_present(pdf_path)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=str(exc) or type(exc).__name__,
            ) from exc

        async def process_in_background() -> None:
            try:
                result = await process_document(document_id, pdf_path, filename)
            except Exception as exc:  # pipeline 抛出未预期异常 → 兜底置 FAILED
                try:
                    await mark_failed(document_id, str(exc) or type(exc).__name__)
                finally:
                    await _delete_if_present(pdf_path)
                return
            if result.status == "FAILED":
                # pipeline 正常返回 FAILED：确保 C1 进入终态（幂等），再删原文件
                try:
                    await mark_failed(document_id, result.reason or "處理失敗")
                finally:
                    await _delete_if_present(pdf_path)
            # READY：保持 pipeline 已写入的 READY 状态

        background_tasks.add_task(process_in_background)
        return to_document_record(record)

    @router.get("", response_model=list[DocumentRecord])
    async def get_documents_list() -> list[DocumentRecord]:
        return await list_documents(session_factory=session_factory)

    @router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def remove_document(document_id: str) -> Response:
        lock = delete_locks.setdefault(document_id, asyncio.Lock())
        async with lock:
            documents = await get_documents([document_id], session_factory=session_factory)
            if document_id not in documents:
                raise HTTPException(status_code=404, detail="Document not found")
            await _finish_delete_before_cancellation(
                document_id, UPLOAD_DIR / f"{document_id}.pdf", session_factory
            )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router
