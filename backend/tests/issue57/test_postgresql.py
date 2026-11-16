"""Real PostgreSQL: migrations, vector scope and concurrent budget reservations.

Set ISSUE57_TEST_DATABASE_URL to a disposable database named issue57_test*.
Each test creates its own random schema and removes only that schema afterward.
"""
import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import NullPool

from app.chat.config import RagSettings
from app.chat.persistent.errors import ChatProblem
from app.chat.persistent.models import ChatRetrievalAttempt
from app.chat.persistent.schemas import PersistentChatRequest
from app.chat.persistent.store import ChatStore
from app.qa.agent.runtime import RealRAG
from app.storage.models import Document, Chunk
from app.storage.repo import search_similar

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def postgres():
    url = os.environ.get("ISSUE57_TEST_DATABASE_URL")
    if not url:
        pytest.skip("requires explicit disposable ISSUE57_TEST_DATABASE_URL")
    parsed = make_url(url)
    if not (parsed.database or "").startswith("issue57_test") or parsed.get_backend_name() != "postgresql":
        raise RuntimeError("Only an explicitly named issue57_test* PostgreSQL database is allowed")
    url = parsed.set(drivername="postgresql+asyncpg")
    schema = "issue57_" + uuid4().hex
    admin = create_async_engine(url, poolclass=NullPool)
    async with admin.begin() as connection:
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(url, poolclass=NullPool,
        connect_args={"server_settings": {"search_path": schema + ",public"}})
    try:
        async with engine.begin() as connection:
            for model in (Document, Chunk):
                await connection.run_sync(model.__table__.create)
        # 070 establishes #68's original schema; 071 must work twice unchanged.
        migrations = Path(__file__).resolve().parents[3] / "storage/migrations"
        async with engine.connect() as connection:
            raw = await connection.get_raw_connection()
            for filename in ("070_chat_persistence.sql", "071_chat_mode_fallback.sql", "071_chat_mode_fallback.sql"):
                await raw.driver_connection.execute((migrations / filename).read_text(encoding="utf-8"))
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


async def test_real_vector_query_filters_scope_before_top_k(postgres):
    unit = [1.0] + [0.0] * 1023
    distant = [0.8, 0.6] + [0.0] * 1022
    async with postgres() as session, session.begin():
        session.add_all([Document(document_id=key, filename=key + ".pdf", status="READY") for key in ("inside", "outside")])
        await session.flush()
        for index, (key, vector) in enumerate((("outside", unit), ("inside", distant))):
            session.add(Chunk(schema_version="1.0", chunk_id=key, document_id=key, chunk_index=index,
                page_index=0, page_number=1, text=key, token_count=1, embedding=vector))
    assert [item.document_id for item in await search_similar(unit, 1, postgres)] == ["outside"]
    assert [item.document_id for item in await search_similar(unit, 1, postgres, document_ids=["inside"])] == ["inside"]
    assert await search_similar(unit, 1, postgres, document_ids=[]) == []
    async with RealRAG("测试", session_factory=postgres, embedder=lambda texts: [unit], top_k=1, document_ids=["inside"]) as runtime:
        hits = await runtime.retrieve("测试")
        assert [item.document_id for item in hits] == ["inside"]
        assert set(await runtime.documents(["inside", "outside"])) == {"inside"}


async def test_two_workers_cannot_both_claim_last_budget_slot(postgres):
    store = ChatStore(postgres, RagSettings("agent", 120))
    row, _ = await store.reserve(PersistentChatRequest(message="Q"), "worker")
    qid, key = row["question_id"], row.run_id
    for query in ("first", "second"):
        ordinal = await store.reserve_retrieval(qid, key, query, "AGENT")
        await store.finish_retrieval(qid, key, ordinal, "FAILED")
    results = await asyncio.gather(
        store.reserve_retrieval(qid, key, "third-a", "FALLBACK"),
        store.reserve_retrieval(qid, key, "third-b", "FALLBACK"), return_exceptions=True)
    assert sum(result == 3 for result in results) == 1
    assert sum(isinstance(result, ChatProblem) for result in results) == 1
    async with postgres() as session:
        assert len(list((await session.scalars(select(ChatRetrievalAttempt))).all())) == 3
