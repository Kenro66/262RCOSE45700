"""Issue9 error contracts and real #12 wiring; no real model is claimed here."""
import asyncio
import logging
import subprocess
import sys
import threading
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.chat import service
from app.chat.service import SYSTEM_ERROR_MESSAGE, create_chat_router
from app.contracts import SearchHit
from app.main import create_app
from app.storage.repo import create_processing_document, mark_document_ready


async def post(app, payload=None):
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            return await client.post("/api/chat", json=payload or {"message": "政策是什么？"})
    finally:
        runner = getattr(app.state, "chat_runner", None)
        if runner is not None:
            await runner.close()


def assert_system_error(response):
    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == dict(
        status="SYSTEM_ERROR", answer=None, citations=[], message=SYSTEM_ERROR_MESSAGE, model_name=None
    )


@pytest.mark.asyncio
async def test_database_failure_has_c5_and_does_not_call_model(monkeypatch):
    async def fail(**kwargs):
        raise ConnectionError("database unavailable")

    async def forbidden(*args):
        pytest.fail("answer must not run after a database error")

    monkeypatch.setattr(service, "has_ready_documents", fail)
    app = FastAPI()
    app.include_router(create_chat_router(forbidden), prefix="/api")
    assert_system_error(await post(app))


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [
    None, {}, {"status": "BROKEN"}, {"status": "SYSTEM_ERROR"},
    {"status": "ANSWERED", "answer": "text", "citations": []},
    {"status": "ANSWERED", "answer": " ", "citations": [
        {"evidence_id": "E1", "filename": "a.pdf", "page": 1, "quote": "text"}]},
    {"status": "ANSWERED", "answer": "text", "citations": [
        {"evidence_id": "E1", "filename": " ", "page": 1, "quote": "text"}]},
    {"status": "ANSWERED", "answer": "text", "citations": [
        {"evidence_id": "E1", "filename": "a.pdf", "page": 0, "quote": "text"}]},
    {"status": "ANSWERED", "answer": "text", "citations": [
        {"evidence_id": "E1", "filename": "a.pdf", "page": 1, "quote": " "}]},
    {"status": "INSUFFICIENT_EVIDENCE", "citations": [
        {"evidence_id": "E1", "filename": "a.pdf", "page": 1, "quote": "text"}]},
])
async def test_invalid_dependency_result_is_c5_error(monkeypatch, result):
    async def ready(**kwargs):
        return True

    async def answer(*args):
        return result

    monkeypatch.setattr(service, "has_ready_documents", ready)
    app = FastAPI()
    app.include_router(create_chat_router(answer), prefix="/api")
    assert_system_error(await post(app))


@pytest.mark.asyncio
async def test_cancellation_is_not_converted_to_business_error(monkeypatch):
    async def ready(**kwargs):
        return True

    async def cancelled(*args):
        raise asyncio.CancelledError()

    monkeypatch.setattr(service, "has_ready_documents", ready)
    app = FastAPI()
    app.include_router(create_chat_router(cancelled), prefix="/api")
    with pytest.raises(asyncio.CancelledError):
        await post(app)


@pytest.mark.asyncio
async def test_sync_dependency_does_not_block_the_event_loop():
    released = threading.Event()

    def answer(*args):
        return released.wait(2)

    task = asyncio.create_task(service._invoke(answer, "question", None))
    await asyncio.sleep(0.02)
    released.set()
    assert await task is True


@pytest.mark.asyncio
async def test_agent_app_no_documents_no_embedding_or_generation(db, monkeypatch):
    monkeypatch.setenv("RAG_MODE", "agent")
    import app.adapters.embedding as embedding
    import app.adapters.generation as generation

    def forbidden(*args, **kwargs):
        pytest.fail("No READY documents must bypass all model calls")

    monkeypatch.setattr(embedding, "embed_texts", forbidden)
    monkeypatch.setattr(generation, "generate_text", forbidden)
    response = await post(create_app())
    assert response.status_code == 200
    body = response.json()
    assert {key: body[key] for key in ("status", "answer", "citations", "message")} == dict(
        status="NO_READY_DOCUMENTS", answer=None, citations=[], message="当前没有可用文件"
    )
    assert body["session_id"] and body["question_id"]
    assert body["execution_status"] == "SUCCEEDED"
    assert body["error_code"] is None and not body["retryable"]


async def seed_real_wiring(db, monkeypatch, *, empty_hits=False):
    import app.adapters.embedding as embedding
    import app.storage.repo as repo

    await create_processing_document("DOC001", "actual-name.pdf", session_factory=db)
    await mark_document_ready("DOC001", session_factory=db)
    observed = []

    def embed(texts):
        observed.append(("embed", texts))
        return [[0.1] * 1024]

    async def search(vector, top_k, *, session_factory=None, document_ids=None):
        assert len(vector) == 1024
        assert top_k == 5
        observed.append(("search", top_k))
        if empty_hits:
            assert document_ids == ('DOC001',)
            assert session_factory is db
            return []
        return [SearchHit(chunk_id="DOC001_C000000", document_id="DOC001",
                          page_number=2, text="政策允许办理。", score=0.9)]

    monkeypatch.setattr(embedding, "embed_texts", embed)
    monkeypatch.setattr(repo, "search_similar", search)
    monkeypatch.setenv("RETRIEVAL_TOP_K", "5")
    return observed


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [None, "single"])
async def test_configured_single_wiring_runs_real_retriever_orchestrator_and_db_metadata(db, monkeypatch, mode):
    import app.qa.orchestrator as orchestrator

    if mode is None:
        monkeypatch.delenv("RAG_MODE", raising=False)
    else:
        monkeypatch.setenv("RAG_MODE", mode)

    observed = await seed_real_wiring(db, monkeypatch)
    from app.adapters import generation
    monkeypatch.setattr(generation, "generate", lambda *args, **kwargs: generation.GenerationResult(
        model_name="single-provider-model", tool_calls=[], text='{"answer":"政策允许办理。[E1]","insufficient_evidence":false}'))
    response = await post(create_app(), {"message": "政策是什么？", "session_id": "s1"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ANSWERED"
    assert body["model_name"] == "single-provider-model"
    assert body["citations"] == [dict(evidence_id="E1", filename="actual-name.pdf",
                                      page=2, quote="政策允许办理。")]
    assert observed == [("embed", ["政策是什么？"]), ("search", 5)]


@pytest.mark.asyncio
async def test_configured_agent_wiring_runs_real_retriever_and_saves_budget(db, monkeypatch):
    monkeypatch.setenv("RAG_MODE", "agent")
    import json
    import app.adapters.generation as generation
    from app.chat.persistent.models import ChatQuestion

    observed = await seed_real_wiring(db, monkeypatch, empty_hits=True)

    def generate(messages):
        data = json.loads(messages[-1]["content"])
        if data["task"] == "check_conditions":
            return '{"missing_conditions":[]}'
        assert data["task"] == "rewrite_query"
        return json.dumps({"query": data["current_query"]})

    monkeypatch.setattr(generation, "generate_text", generate)
    result = await post(create_app(), {"message": "政策是什么？", "session_id": "s1"})
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["status"] == "INSUFFICIENT_EVIDENCE" and body["session_id"] == "s1"
    assert body["execution_status"] == "SUCCEEDED"
    async with db() as session:
        row = await session.get(ChatQuestion, body["question_id"])
        assert row.question == "政策是什么？"
        assert row.state["retrieval_count"] == 1 and row.state["clarification_count"] == 0
    assert observed == [("embed", ["政策是什么？"]), ("search", 5)]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["single", "agent"])
async def test_real_generation_adapter_bad_address_returns_c5_error(db, monkeypatch, mode):
    monkeypatch.setenv("RAG_MODE", mode)
    import socket
    import app.adapters.generation as generation

    await seed_real_wiring(db, monkeypatch)
    # Reserve a port without listening: a real HTTP connection must be refused.
    with socket.socket() as closed_port:
        closed_port.bind(("127.0.0.1", 0))
        port = closed_port.getsockname()[1]
        monkeypatch.setattr(generation, "settings", lambda: {
            "GENERATION_MODEL": "test", "GENERATION_BASE_URL": f"http://127.0.0.1:{port}/v1",
            "GENERATION_TIMEOUT_SECONDS": "1", "GENERATION_API_KEY": "",
        })
        app = create_app()
        result = await post(app)
        if mode == "single":
            assert_system_error(result)
        else:
            assert result.status_code == 500
            body = result.json()
            assert body["status"] == "SYSTEM_ERROR" and body["answer"] is None
            assert body["citations"] == [] and body["message"]
            assert body["execution_status"] == "FAILED" and body["question_id"]
            assert body["error_code"] == "SYSTEM_ERROR" and body["retryable"]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{}, {"message": ""}, {"message": None}])
async def test_invalid_request_keeps_public_fastapi_validation(payload):
    response = await post(create_app(), payload if payload else {"session_id": "s1"})
    assert response.status_code == 422


def test_logging_enabled_in_fresh_process():
    backend = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, "-c",
        "import logging; import app.main; "
        "assert logging.getLogger('app.chat.service').isEnabledFor(logging.INFO)"],
        cwd=backend, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_fixture_does_not_reset_application_database():
    import os
    backend = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["DATABASE_URL"] = "postgresql://never-connect:unused@127.0.0.1:1/application"
    env.pop("ISSUE9_TEST_DATABASE_URL", None)
    env.pop("ISSUE9_ALLOW_DATABASE_RESET", None)
    result = subprocess.run([sys.executable, "-c",
        "import sys; sys.path.insert(0, 'tests'); import conftest; "
        "import app.database; assert app.database.DATABASE_URL.startswith('sqlite')"],
        cwd=backend, env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_latest_lifespan_closes_database_even_if_initialization_fails(monkeypatch):
    from unittest.mock import AsyncMock
    import app.main as main

    initialize = AsyncMock(side_effect=RuntimeError("test initialization failure"))
    close = AsyncMock()
    monkeypatch.setattr(main, "init_db", initialize)
    monkeypatch.setattr(main, "close_db", close)
    with pytest.raises(RuntimeError, match="initialization failure"):
        async with main.lifespan(main.app):
            pytest.fail("An application with failed initialization must not serve")
    close.assert_awaited_once()
