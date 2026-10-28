"""文件上传服务（Issue #7）。

POST   /api/documents          上传 PDF（multipart，字段名 file），立即 201 + C1(PROCESSING)
GET    /api/documents          C1 列表（最新在前）
DELETE /api/documents/{id}     删除 DB 记录（级联 ParsedPage/C2）与 uploads/ 原文件

上传校验（与 Issue #6 失败规则一致）：
- content_type 必须是 application/pdf，否则 400「文件不是 PDF 类型」
- 魔数必须是 %PDF-，否则 400「文件不是 PDF 类型」
- 非空、不超过 25MB

上传成功即返回 status=PROCESSING 的 C1；
后台跑 pipeline（解析→切分→向量化→入库），失败置 FAILED + error_message
并删除 uploads/ 里的原文件。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, Response, UploadFile, status
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.contracts import DocumentRecord, DocumentStatus
from app.database import SessionFactory
from app.documents.pipeline import ProcessingResult
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
ProcessingRecordCreator = Callable[[str, str], Awaitable[Any]]
FailureMarker = Callable[[str, str], Awaitable[None]]
SessionMaker = Callable[[], Any]


async def _save_pdf(upload: UploadFile, destination: Path) -> None:
    """流式写入临时文件，校验通过后才发布为正式文件。"""
    temporary = destination.with_suffix(".uploading")
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

                total_bytes += len(chunk)
                if total_bytes > MAX_PDF_BYTES:
                    raise ValueError(
                        f"PDF exceeds the {MAX_PDF_BYTES // (1024 * 1024)} MB limit"
                    )
                await run_in_threadpool(output.write, chunk)

        if total_bytes == 0:
            raise ValueError("Uploaded PDF is empty")

        await run_in_threadpool(os.replace, temporary, destination)
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


def create_documents_router(
    process_document: DownstreamProcessor,
    generate_document_id: DocumentIdGenerator = next_document_id,
    create_processing_record: ProcessingRecordCreator = create_processing_document,
    mark_failed: FailureMarker = mark_document_failed,
    session_factory: SessionMaker = SessionFactory,
) -> APIRouter:
    """构建 /api/documents 路由，处理实现由 app（pipeline）注入。"""
    router = APIRouter(prefix="/documents", tags=["documents"])

    @router.post(
        "", response_model=DocumentRecord, status_code=status.HTTP_201_CREATED
    )
    async def upload_document(
        background_tasks: BackgroundTasks,
        file: Annotated[UploadFile, File(description="PDF document")],
    ) -> DocumentRecord:
        filename = Path(file.filename or "document.pdf").name

        if file.content_type not in {"application/pdf", "application/x-pdf"}:
            await file.close()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=ERROR_NOT_PDF
            )

        document_id = await generate_document_id()
        pdf_path = UPLOAD_DIR / f"{document_id}.pdf"

        try:
            await _save_pdf(file, pdf_path)
            record = await create_processing_record(document_id, filename)
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
            except Exception as exc:  # pipeline 抛出未預期異常 → 兜底置 FAILED
                await mark_failed(document_id, str(exc) or type(exc).__name__)
                await _delete_if_present(pdf_path)
                return
            if result.status == "FAILED":
                # pipeline 正常返回 FAILED：確保 C1 進入終態（冪等），再刪原檔
                await mark_failed(document_id, result.reason or "處理失敗")
                await _delete_if_present(pdf_path)
            # READY：保持 pipeline 已寫入的 READY 狀態

        background_tasks.add_task(process_in_background)
        return to_document_record(record)

    @router.get("", response_model=list[DocumentRecord])
    async def get_documents_list() -> list[DocumentRecord]:
        return await list_documents(session_factory=session_factory)

    @router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def remove_document(document_id: str) -> Response:
        documents = await get_documents([document_id], session_factory=session_factory)
        if document_id not in documents:
            raise HTTPException(status_code=404, detail="Document not found")

        # 先删原文件：文件被占用或无权限时，保留记录与文本块，供用户重试。
        try:
            await _delete_if_present(UPLOAD_DIR / f"{document_id}.pdf", strict=True)
        except OSError as exc:
            logger.warning("原文件删除失败，保留数据库记录 %s：%s", document_id, exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="原文件暂时无法删除，请稍后重试",
            ) from exc

        # 原文件已不存在；若数据库操作失败，重试时 missing_ok=True 仍可继续。
        # 若另一请求已删除该记录，删除目标也已达成，仍返回 204。
        await delete_document(document_id, session_factory=session_factory)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router
