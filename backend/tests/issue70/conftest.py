"""只在临时 SQLite 中测试。覆盖包运行时需显式设置 ISSUE70_REFERENCE_BACKEND。"""
import asyncio
import importlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
import pytest_asyncio


BACKEND = Path(os.environ["ISSUE70_BACKEND"]) if os.environ.get("ISSUE70_BACKEND") else next(parent for parent in Path(__file__).resolve().parents if (parent / "app/main.py").is_file())
REFERENCE = os.environ.get("ISSUE70_REFERENCE_BACKEND")
if not (BACKEND / "app/contracts.py").is_file() and not REFERENCE:
    raise RuntimeError("覆盖包需要原项目：请设置 ISSUE70_REFERENCE_BACKEND 为原项目 backend 的绝对路径")
if REFERENCE:
    sys.path.insert(0, REFERENCE)
sys.path.insert(0, str(BACKEND))
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
try:
    app_package = importlib.import_module("app")
except ModuleNotFoundError as exc:
    raise RuntimeError("覆盖包需要原项目：请设置 ISSUE70_REFERENCE_BACKEND 为原项目 backend 的绝对路径") from exc
app_package.__path__ = [str(BACKEND / "app"), *list(app_package.__path__)]
chat_package = importlib.import_module("app.chat")
chat_package.__path__ = [str(BACKEND / "app/chat"), *list(chat_package.__path__)]

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from app.chat.persistent.api import build_persistent_chat_router
from app.chat.persistent.models import ChatQuestion, ChatSession, ChatSubmission
from app.chat.persistent.runner import ChatRunner
from app.chat.persistent.schemas import PersistentChatRequest
from app.chat.persistent.store import ChatStore
from app.qa.agent.state import AgentChatResponse, AgentState, Clarification


QUESTION = "免税额度是多少？"


def prompt():
    return Clarification.model_validate({"questions": [
        {"field": "country", "question": "目的地？", "type": "select", "options": ["CN", "KR"]}
    ]})


def response(question_id, status="INSUFFICIENT_EVIDENCE", **values):
    return AgentChatResponse(status=status, question_id=question_id, **values).model_dump(mode="json")


@pytest_asyncio.fixture
async def db(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "chat.sqlite").as_posix())

    @event.listens_for(engine.sync_engine, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as connection:
        for model in (ChatSession, ChatQuestion, ChatSubmission):
            await connection.run_sync(model.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield SimpleNamespace(engine=engine, factory=factory, store=ChatStore(factory))
    await engine.dispose()


@pytest_asyncio.fixture
async def reserved(db):
    row, started = await db.store.reserve(PersistentChatRequest(message=QUESTION, session_id="sessionA"), "workerA")
    assert started
    state = AgentState.model_validate((await db.store.execution_input(row["question_id"], "workerA"))["state"])
    return SimpleNamespace(row=row, qid=row["question_id"], sid="sessionA", worker="workerA", state=state)


@pytest_asyncio.fixture
async def waiting(db, reserved):
    state = reserved.state.model_copy(update={"retrieval_count": 1, "clarification_count": 1})
    await db.store.checkpoint(reserved.qid, reserved.worker, state, prompt())
    await db.store.finish(reserved.qid, reserved.worker, "SUCCEEDED", response(
        reserved.qid, "NEEDS_CLARIFICATION", clarification=prompt()))
    reserved.state = state
    return reserved


@pytest_asyncio.fixture
async def web(db, monkeypatch):
    """运行真正的 Agent 编排，只替换文件检查和外部服务依赖。"""
    import app.chat.persistent.runner as runner_module
    control = SimpleNamespace(
        ready=True, retrievals=[], generations=[], block_retrieve=False,
        block_generate=False, retrieve_error=None, entered=asyncio.Event(),
        release=asyncio.Event(), closed=0, extra_questions=None,
    )

    async def has_ready(**_):
        return control.ready

    class FakeRAG:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            control.closed += 1

        async def retrieve(self, query):
            control.retrievals.append(query)
            if control.block_retrieve:
                control.entered.set()
                await control.release.wait()
            if control.retrieve_error:
                raise control.retrieve_error
            return []

        async def documents(self, _):
            return {}

        async def generate(self, messages):
            data = json.loads(messages[-1]["content"])
            control.generations.append(data)
            if control.block_generate:
                control.entered.set()
                await control.release.wait()
            if data["task"] == "rewrite_query":
                return json.dumps({"query": data["current_query"]})
            questions = control.extra_questions
            if questions is None:
                questions = [] if data["conditions"] else prompt().model_dump()["questions"]
            return json.dumps({"missing_conditions": questions})

        def dependencies(self):
            return dict(retrieve_fn=self.retrieve, get_documents_fn=self.documents, generate_fn=self.generate)

    monkeypatch.setattr(runner_module, "has_ready_documents", has_ready)
    monkeypatch.setattr(runner_module, "RealRAG", FakeRAG)
    runner = ChatRunner(db.store)
    application = FastAPI()
    application.include_router(build_persistent_chat_router(db.store, runner), prefix="/api")
    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as client:
        yield SimpleNamespace(client=client, app=application, runner=runner, control=control, store=db.store, db=db)
        control.release.set()
        if runner.tasks:
            await asyncio.wait_for(asyncio.gather(*list(runner.tasks.values()), return_exceptions=True), 5)
        await runner.close()


async def ask(web, session="sessionA"):
    result = await web.client.post("/api/chat", json={"message": QUESTION, "session_id": session})
    assert result.status_code == 200, result.text
    return result.json()


def submission(qid, country="CN", session="sessionA"):
    return {"session_id": session, "question_id": qid, "message": QUESTION, "conditions": {"country": country}}


def question_url(qid, session="sessionA"):
    return f"/api/chat/sessions/{session}/questions/{qid}"
