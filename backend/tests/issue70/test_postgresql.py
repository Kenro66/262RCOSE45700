"""可选真实 PostgreSQL 检查：仅显式提供专用测试库地址时运行，使用独立临时 schema。"""
import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from app.chat.persistent.schemas import PersistentChatRequest
from app.chat.persistent.store import ChatStore
from .conftest import BACKEND, QUESTION

TEST_URL = os.environ.get("ISSUE70_TEST_POSTGRES_URL")
pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(not TEST_URL, reason="未设置专用 ISSUE70_TEST_POSTGRES_URL，真实 PostgreSQL 未验证")]


@pytest_asyncio.fixture
async def postgres_store():
    import asyncpg
    url = TEST_URL.replace("postgresql://", "postgresql+asyncpg://", 1) if TEST_URL.startswith("postgresql://") else TEST_URL
    assert url.startswith("postgresql+asyncpg://"), "仅支持专用 PostgreSQL 测试库"
    schema = "issue70_test_" + uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = None
    try:
        raw = await asyncpg.connect(url.replace("+asyncpg", "", 1), server_settings={"search_path": schema})
        try:
            migration = BACKEND.parent / "storage/migrations/070_chat_persistence.sql"
            await raw.execute(migration.read_text(encoding="utf-8-sig"))
        finally:
            await raw.close()
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
        yield ChatStore(async_sessionmaker(engine, expire_on_commit=False))
    finally:
        if engine is not None:
            await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


async def test_migration_and_real_jsonb_round_trip(postgres_store):
    row, started = await postgres_store.reserve(PersistentChatRequest(message=QUESTION, session_id="pg_session"), "owner")
    assert started
    data = await postgres_store.execution_input(row["question_id"], "owner")
    assert data["state"]["question"] == QUESTION


async def test_two_workers_cannot_claim_same_question_concurrently(postgres_store):
    row, _ = await postgres_store.reserve(PersistentChatRequest(message=QUESTION, session_id="pg_session"), "original")
    qid = row["question_id"]
    await postgres_store.finish(qid, "original", "FAILED")
    request = PersistentChatRequest(message=QUESTION, session_id="pg_session", question_id=qid)
    results = await asyncio.gather(*(postgres_store.reserve(request, owner) for owner in ("worker1", "worker2")))
    assert sum(started for _, started in results) == 1
