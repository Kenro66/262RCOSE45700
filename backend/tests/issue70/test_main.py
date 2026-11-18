from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
import app.main as main


@pytest.fixture(autouse=True)
def enable_agent_mode(monkeypatch):
    monkeypatch.setenv("RAG_MODE", "agent")


def test_explicit_agent_application_registers_all_six_chat_interfaces():
    application = main.create_app()
    paths = application.openapi()["paths"]
    expected = {
        "/api/chat": "post",
        "/api/chat/sessions": "get",
        "/api/chat/sessions/{session_id}": "get",
        "/api/chat/sessions/{session_id}/questions/{question_id}/cancel": "post",
        "/api/chat/sessions/{session_id}/questions/{question_id}/execution": "get",
    }
    for path, method in expected.items():
        assert method in paths[path]
    assert "delete" in paths["/api/chat/sessions/{session_id}"]
    assert "requestBody" not in paths["/api/chat/sessions/{session_id}/questions/{question_id}/cancel"]["post"]


def test_explicit_old_answer_function_preserves_original_router():
    application = main.create_app(answer_fn=lambda *args: None)
    assert "/api/chat" in application.openapi()["paths"]
    assert "/api/chat/sessions" not in application.openapi()["paths"]
    assert not hasattr(application.state, "chat_runner")


@pytest.mark.asyncio
async def test_lifespan_stops_runner_before_closing_database(monkeypatch):
    calls = []

    async def init():
        calls.append("init")

    async def close_runner():
        calls.append("runner")

    async def close_database():
        calls.append("database")

    monkeypatch.setattr(main, "init_db", init)
    monkeypatch.setattr(main, "close_db", close_database)
    application = main.create_app()
    application.state.chat_runner = SimpleNamespace(close=close_runner)
    async with main.lifespan(application):
        calls.append("serving")
    assert calls == ["init", "serving", "runner", "database"]


@pytest.mark.asyncio
async def test_old_entrypoint_lifespan_without_runner(monkeypatch):
    initialize, close = AsyncMock(), AsyncMock()
    monkeypatch.setattr(main, "init_db", initialize)
    monkeypatch.setattr(main, "close_db", close)
    async with main.lifespan(main.create_app(answer_fn=lambda *args: None)):
        pass
    initialize.assert_awaited_once()
    close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "connect", "query", "close"])
async def test_health_checks_release_connection_and_handle_errors(monkeypatch, failure):
    connection = SimpleNamespace(fetchrow=AsyncMock(return_value=("16.0", True)), close=AsyncMock())
    connect = AsyncMock(return_value=connection)
    if failure == "connect":
        connect.side_effect = OSError("connection unavailable")
    elif failure == "query":
        connection.fetchrow.side_effect = RuntimeError("query failed")
    elif failure == "close":
        connection.close.side_effect = RuntimeError("close failed")
    monkeypatch.setattr(main.asyncpg, "connect", connect)
    async with AsyncClient(transport=ASGITransport(app=main.create_app()), base_url="http://test") as client:
        result = await client.get("/health")
    assert result.status_code == (503 if failure in {"connect", "query"} else 200)
    if failure != "connect":
        connection.close.assert_awaited_once()
    if failure is None:
        assert result.json()["database"] == {"version": "16.0", "vector_extension": True}


def test_database_url_helpers(monkeypatch):
    assert main._asyncpg_url("postgresql+asyncpg://u:p@host/db") == "postgresql://u:p@host/db"
    monkeypatch.setattr(main, "DATABASE_URL", "postgresql+asyncpg://u:secret@host/db")
    assert main._db_host() == "host/db"


def test_explicit_ingestion_dependencies_are_forwarded(monkeypatch):
    sentinel_chunker, sentinel_embedder, seen = object(), object(), []
    original = main.build_documents_router

    def router(chunker, embedder):
        seen.append((chunker, embedder))
        return original(chunker, embedder)

    monkeypatch.setattr(main, "build_documents_router", router)
    main.create_app(chunk_pages=sentinel_chunker, embed_chunks=sentinel_embedder)
    assert seen == [(sentinel_chunker, sentinel_embedder)]
