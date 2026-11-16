"""Isolated fixtures: never read or mutate the deployment database."""
import asyncio
from types import SimpleNamespace

import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.chat.config import RagSettings
from app.chat.persistent.context import ExecutionGuard, RunProgress
from app.chat.persistent.models import ChatQuestion, ChatRetrievalAttempt, ChatSession, ChatSubmission
from app.chat.persistent.schemas import PersistentChatRequest
from app.chat.persistent.store import ChatStore
from app.contracts import SearchHit
from app.qa.agent.state import AgentState
from app.storage.models import Document


def hit(chunk="c1", document="d1", text="测试规则原文。"):
    return SearchHit(chunk_id=chunk, document_id=document, page_number=1, text=text, score=.9)


@pytest_asyncio.fixture
async def database(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "issue57.sqlite").as_posix())

    @event.listens_for(engine.sync_engine, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as connection:
        for model in (Document, ChatSession, ChatQuestion, ChatSubmission, ChatRetrievalAttempt):
            await connection.run_sync(model.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        session.add(Document(document_id="d1", filename="test.pdf", status="READY"))
        session.add(Document(document_id="pending", filename="pending.pdf", status="PROCESSING"))
    store = ChatStore(factory, RagSettings("agent", 120))
    yield SimpleNamespace(factory=factory, store=store)
    await engine.dispose()


@pytest_asyncio.fixture
async def execution(database):
    row, started = await database.store.reserve(PersistentChatRequest(message="测试规则如何适用？", session_id="s1"), "worker")
    assert started
    data = await database.store.execution_input(row["question_id"], "worker")
    state = AgentState.model_validate(data["state"])
    progress = RunProgress(state, document_ids=tuple(data["document_ids"]))
    stop = asyncio.Event()
    return SimpleNamespace(**vars(database), row=row, data=data, state=state, progress=progress,
                           stop=stop, guard=ExecutionGuard(database.store, data, stop, progress),
                           qid=row["question_id"], key=row.run_id, worker="worker")


async def checkpoint(execution, state=None):
    state = state or execution.progress.snapshot(execution.progress.state)
    await execution.store.checkpoint(execution.qid, execution.worker, state, execution_key=execution.key)
    return state
