"""Production binding contract tests; no external service required."""

from unittest.mock import AsyncMock, Mock

import pytest

from app.qa.agent import runtime
from app.qa.agent.runtime import RealRAG, agent_answer_real


@pytest.mark.asyncio
async def test_existing_retriever_and_repository_signatures_are_preserved(monkeypatch):
    import app.storage.repo as repo

    factory = object()
    embedder = Mock()
    search = AsyncMock(return_value=[])
    monkeypatch.setattr(repo, "search_similar", search)
    async def retrieve(query, *, search_similar, top_k, embedder):
        assert query == "改写后的问题"
        assert top_k == 7
        assert embedder is not None
        return await search_similar([0.1] * 1024, top_k=top_k)
    patched = AsyncMock(side_effect=retrieve)
    monkeypatch.setattr(runtime, "retrieve", patched)
    async with RealRAG("原问题", session_factory=factory, embedder=embedder, top_k=7) as adapter:
        assert await adapter.retrieve("改写后的问题") == []
    search.assert_awaited_once_with([0.1] * 1024, top_k=7, session_factory=factory)
    assert patched.await_args.kwargs["embedder"] is embedder


@pytest.mark.asyncio
async def test_completion_uses_latest_rewrite_and_keeps_original_conditions(monkeypatch):
    import app.storage.repo as repo

    factory = object()
    load = AsyncMock(return_value=["original chunks"])
    monkeypatch.setattr(repo, "get_ready_chunk_sources", load)
    monkeypatch.setattr(runtime, "retrieve", AsyncMock(return_value=[]))
    contexts = []
    async def complete(hits, *, question, load_chunks_fn):
        contexts.append(question)
        assert hits == ["hits"]
        return await load_chunks_fn(["doc_1"])
    monkeypatch.setattr(runtime, "complete_evidence", complete)
    async with RealRAG("超过免税限量应走什么通道？", conditions={"destination_country": "CN"},
                       session_factory=factory) as adapter:
        await adapter.retrieve("第一次查询")
        await adapter.complete(["hits"])
        await adapter.retrieve("红色通道及第七项改写查询")
        assert await adapter.complete(["hits"]) == ["original chunks"]
    assert contexts[0].startswith("第一次查询")
    assert contexts[1].startswith("红色通道及第七项改写查询")
    assert all("超过免税限量" in context and "CN" in context for context in contexts)
    assert load.await_count == 2
    load.assert_awaited_with(["doc_1"], session_factory=factory)


@pytest.mark.asyncio
async def test_document_lookup_is_read_only_and_generation_reuses_existing_adapter(monkeypatch):
    import app.storage.repo as repo

    factory = object()
    documents = AsyncMock(return_value={"doc_1": {"status": "READY"}})
    monkeypatch.setattr(repo, "get_documents", documents)
    async with RealRAG("问题", session_factory=factory) as adapter:
        assert (await adapter.documents(["doc_1"]))["doc_1"]["status"] == "READY"
        assert adapter.dependencies()["generate_fn"] is runtime.generate_text
        assert adapter.engine is None
    documents.assert_awaited_once_with(["doc_1"], session_factory=factory)


@pytest.mark.asyncio
async def test_request_instances_do_not_share_completion_context(monkeypatch):
    monkeypatch.setattr(runtime, "retrieve", AsyncMock(return_value=[]))
    a, b = RealRAG("a", session_factory=object()), RealRAG("b", session_factory=object())
    await a.retrieve("query a")
    assert a.current_query == "query a"
    assert b.current_query is None
    with pytest.raises(RuntimeError, match="preceding retrieval"):
        await b.complete([])


@pytest.mark.asyncio
async def test_wrapper_passes_service_state_and_closes_owned_engine(monkeypatch):
    from app.qa.agent import AgentState

    state = AgentState(conditions={"destination_country": "CN"})
    answer = AsyncMock(return_value="result")
    monkeypatch.setattr(runtime, "agent_answer", answer)
    engine = Mock(dispose=AsyncMock())
    import sqlalchemy.ext.asyncio as sa
    monkeypatch.setattr(sa, "create_async_engine", Mock(return_value=engine))
    monkeypatch.setattr(sa, "async_sessionmaker", Mock(return_value=object()))
    result = await agent_answer_real("问题", "s", conditions={"direction": "INBOUND"},
                                     question_id="q", agent_state=state)
    assert result == "result"
    assert answer.await_args.kwargs["agent_state"] is state
    assert answer.await_args.kwargs["complete_evidence_fn"].__self__.conditions == {
        "destination_country": "CN", "direction": "INBOUND"}
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_wrapper_disposes_engine_on_agent_failure(monkeypatch):
    monkeypatch.setattr(runtime, "agent_answer", AsyncMock(side_effect=RuntimeError("failed")))
    engine = Mock(dispose=AsyncMock())
    import sqlalchemy.ext.asyncio as sa
    monkeypatch.setattr(sa, "create_async_engine", Mock(return_value=engine))
    monkeypatch.setattr(sa, "async_sessionmaker", Mock(return_value=object()))
    with pytest.raises(RuntimeError, match="failed"):
        await agent_answer_real("问题")
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_owned_postgres_session_enforces_read_only_transactions(monkeypatch):
    import sqlalchemy.ext.asyncio as sa

    engine = Mock(dispose=AsyncMock())
    create = Mock(return_value=engine)
    monkeypatch.setattr(runtime, "settings", lambda: {"DATABASE_URL": "postgresql://localhost/test"})
    monkeypatch.setattr(sa, "create_async_engine", create)
    monkeypatch.setattr(sa, "async_sessionmaker", Mock(return_value=object()))
    async with RealRAG("问题"):
        pass
    options = create.call_args.kwargs
    assert options["connect_args"]["server_settings"]["default_transaction_read_only"] == "on"
    assert options["connect_args"]["timeout"] == 8
    engine.dispose.assert_awaited_once()
