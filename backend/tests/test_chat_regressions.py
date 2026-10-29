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
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        return await client.post("/api/chat", json=payload or {"message": "政策是什么？"})


def assert_system_error(response):
    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == dict(
        status="SYSTEM_ERROR", answer=None, citations=[], message=SYSTEM_ERROR_MESSAGE
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
async def test_default_app_no_documents_no_embedding_or_generation(db, monkeypatch, caplog):
    import app.adapters.embedding as embedding
    import app.qa.orchestrator as orchestrator

    def forbidden(*args, **kwargs):
        pytest.fail("No READY documents must bypass all model calls")

    monkeypatch.setattr(embedding, "embed_texts", forbidden)
    monkeypatch.setattr(orchestrator, "generate_text", forbidden)
    # No caplog.at_level override: production app config must enable this log.
    response = await post(create_app())
    assert response.status_code == 200
    assert response.json() == dict(
        status="NO_READY_DOCUMENTS", answer=None, citations=[], message="当前没有可用文件"
    )
    assert any("不调用 answer()" in record.message for record in caplog.records)


async def seed_real_wiring(db, monkeypatch):
    import app.adapters.embedding as embedding
    import app.storage.repo as repo

    await create_processing_document("DOC001", "actual-name.pdf", session_factory=db)
    await mark_document_ready("DOC001", session_factory=db)
    observed = []

    def embed(texts):
        observed.append(("embed", texts))
        return [[0.1] * 1024]

    async def search(vector, top_k):
        assert len(vector) == 1024
        assert top_k == 5
        observed.append(("search", top_k))
        return [SearchHit(chunk_id="DOC001_C000000", document_id="DOC001",
                          page_number=2, text="政策允许办理。", score=0.9)]

    monkeypatch.setattr(embedding, "embed_texts", embed)
    monkeypatch.setattr(repo, "search_similar", search)
    monkeypatch.setenv("RETRIEVAL_TOP_K", "5")
    return observed


@pytest.mark.asyncio
async def test_default_wiring_runs_real_retriever_orchestrator_and_db_metadata(db, monkeypatch):
    import app.qa.orchestrator as orchestrator

    observed = await seed_real_wiring(db, monkeypatch)
    monkeypatch.setattr(orchestrator, "generate_text", lambda messages:
                        '{"answer":"政策允许办理。[E1]","insufficient_evidence":false}')
    response = await post(create_app(), {"message": "政策是什么？", "session_id": "s1"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ANSWERED"
    assert body["citations"] == [dict(evidence_id="E1", filename="actual-name.pdf",
                                      page=2, quote="政策允许办理。")]
    assert observed == [("embed", ["政策是什么？"]), ("search", 5)]


@pytest.mark.asyncio
async def test_real_generation_adapter_bad_address_returns_c5_error(db, monkeypatch):
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
        assert_system_error(await post(create_app()))


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
