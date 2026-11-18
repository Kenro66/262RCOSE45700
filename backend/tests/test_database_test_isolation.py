"""前台轮询与后台写入必须使用彼此独立的事务。"""
import pytest
from sqlalchemy import select

from app.storage.models import Document
from app.storage.repo import create_processing_document


@pytest.mark.asyncio
async def test_reader_rollback_does_not_undo_background_write(db):
    await create_processing_document("isolation", "isolation.pdf", session_factory=db)
    async with db() as writer, writer.begin():
        row = await writer.get(Document, "isolation")
        row.status = "READY"
        await writer.flush()
        async with db() as reader:
            assert await reader.scalar(select(Document.status)) == "PROCESSING"
    async with db() as reader:
        assert await reader.scalar(select(Document.status)) == "READY"
