"""Production binding contract tests; no external service required."""

from unittest.mock import AsyncMock, Mock
from types import SimpleNamespace

import pytest

from app.qa.agent import runtime
from app.qa.agent.runtime import RealRAG, agent_answer_real


@pytest.mark.parametrize("error", [OSError("synthetic read failure"), UnicodeError("synthetic encoding failure")])
def test_agent_credential_read_failure_is_typed_and_never_dispatches(monkeypatch, error):
    from app.adapters import config
    from app.adapters.generation import GenerationError
    from app.qa.agent.generation import agent_generator
    reader = Mock(side_effect=error)
    dispatch = Mock(side_effect=AssertionError("must not dispatch"))
    monkeypatch.setattr(config, "dotenv_values", reader)
    raw = {"GENERATION_PROFILE": "furiosa_gemma", "FURIOSA_API_KEY_FILE": "synthetic.env"}
    saved = dict(raw)
    with pytest.raises(GenerationError, match="Cannot read the configured Furiosa credential file") as caught:
        agent_generator(raw, dispatch)
    assert raw == saved and caught.value.__cause__ is None
    reader.assert_called_once()
    dispatch.assert_not_called()
    assert "synthetic read failure" not in str(caught.value)


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
    source = SimpleNamespace(document_id="doc_1")
    monkeypatch.setattr(repo, "list_documents", AsyncMock(return_value=[
        SimpleNamespace(document_id="doc_1", country="CN", direction="BOTH", status="READY")]))
    contexts = []
    async def complete(hits, *, question, original_question, load_chunks_fn):
        contexts.append(question)
        assert original_question == "超过免税限量应走什么通道？"
        assert tuple(hits) == (source,)
        return await load_chunks_fn(["doc_1"])
    monkeypatch.setattr(runtime, "complete_evidence", complete)
    async with RealRAG("超过免税限量应走什么通道？", conditions={"destination_country": "CN"},
                       session_factory=factory) as adapter:
        await adapter.retrieve("第一次查询")
        await adapter.complete([source])
        await adapter.retrieve("红色通道及第七项改写查询")
        assert await adapter.complete([source]) == ["original chunks"]
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


@pytest.mark.parametrize("task", ["check_conditions", "check_applicability", "check_evidence",
                                  "rewrite_query", "check_conflict", "generate_answer"])
def test_agent_native_schema_uses_trusted_identifiers(monkeypatch, task):
    import json
    from app.qa.agent import generation
    capture = Mock(return_value="{}")
    monkeypatch.setattr(generation, "generate_text", capture)
    function = generation.agent_generator({"AGENT_GENERATION_JSON_SCHEMA_ENABLED": "true"}, Mock())
    evidence = [{"evidence_id": "E1", "source": {"chunk_id": "SYN_MAIN"}},
                {"evidence_id": "E2", "source": {"chunk_id": "SYN_NEXT"}}]
    function([{"role": "user", "content": json.dumps({"task": task, "evidence": evidence})}])
    fmt = capture.call_args.kwargs["response_format"]
    assert fmt["json_schema"]["name"] == task and fmt["json_schema"]["strict"] is True
    schema = fmt["json_schema"]["schema"]
    assert schema["additionalProperties"] is False
    if task == "check_evidence":
        assert schema["$defs"]["EvidenceSupport"]["properties"]["chunk_id"]["enum"] == ["SYN_MAIN", "SYN_NEXT"]
    elif task == "check_conflict":
        assert schema["$defs"]["Conflict"]["properties"]["side_a"]["items"]["enum"] == ["E1", "E2"]
    elif task in ("check_conditions", "check_applicability"):
        question = schema["$defs"]["ClarificationQuestion"]
        assert {"field", "question", "type", "options"} == set(question["required"])
        assert "default" not in question["properties"]["options"]
        assert question["properties"]["field"]["pattern"] == r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$"
        assert "enum" not in question["properties"]["field"]  # Dynamic fields remain open.
    elif task == "generate_answer":
        assert set(schema["properties"]) == {"answer", "insufficient_evidence"}


@pytest.mark.parametrize("task", ["check_evidence", "check_conflict"])
def test_empty_evidence_preserves_no_support_path(monkeypatch, task):
    import json
    from app.qa.agent import generation
    capture = Mock(return_value="{}")
    monkeypatch.setattr(generation, "generate_text", capture)
    fn = generation.agent_generator({"AGENT_GENERATION_JSON_SCHEMA_ENABLED": "true"}, Mock())
    fn([{"content": json.dumps({"task": task, "evidence": []})}])
    schema = capture.call_args.kwargs["response_format"]["json_schema"]["schema"]
    if task == "check_evidence":
        assert schema["properties"]["core_support"]["maxItems"] == 0
        assert schema["$defs"]["EvidenceCoverage"]["properties"]["support"]["maxItems"] == 0
    else:
        assert schema["properties"]["conflict"] == {"type": "null"}


@pytest.mark.parametrize("config", [{"AGENT_GENERATION_JSON_SCHEMA_ENABLED": "yes"},
    {"AGENT_GENERATION_MAX_TOKENS": "0"}, {"AGENT_GENERATION_TEMPERATURE": "nan"},
    {"AGENT_GENERATION_MODEL": "gemma"}, {"AGENT_GENERATION_API_KEY": "synthetic"}])
def test_invalid_agent_configuration_fails_closed(config):
    from app.qa.agent.generation import agent_generator
    from app.adapters.generation import GenerationError
    with pytest.raises(GenerationError):
        agent_generator(config, Mock())


def test_default_and_request_local_agent_profiles(monkeypatch):
    from app.qa.agent import generation
    default = Mock()
    assert generation.agent_generator({}, default) is default
    capture = Mock(return_value="ok")
    monkeypatch.setattr(generation, "generate_text", capture)
    config = {"AGENT_GENERATION_MODEL": "gemma", "AGENT_GENERATION_BASE_URL": "https://gemma.invalid/v1",
              "AGENT_GENERATION_MAX_TOKENS": "2048", "AGENT_GENERATION_TEMPERATURE": "0.6",
              "GENERATION_API_KEY": "school-key-must-not-leak"}
    fn = generation.agent_generator(config, default)
    config["AGENT_GENERATION_MODEL"] = "changed"
    assert fn([{"content": "question"}]) == "ok"
    kwargs = capture.call_args.kwargs
    assert kwargs["provider_config"]["GENERATION_MODEL"] == "gemma"
    assert kwargs["provider_config"]["GENERATION_API_KEY"] == ""
    assert kwargs["max_tokens"] == 2048 and kwargs["temperature"] == 0.6


def test_native_schema_rejects_untrusted_envelope(monkeypatch):
    from app.qa.agent import generation
    from app.adapters.generation import GenerationError
    capture = Mock()
    monkeypatch.setattr(generation, "generate_text", capture)
    fn = generation.agent_generator({"AGENT_GENERATION_JSON_SCHEMA_ENABLED": "true"}, Mock())
    for messages in ([], [{"content": "not json"}], [{"content": '{"task":"unknown"}'}]):
        with pytest.raises(GenerationError, match="task envelope"):
            fn(messages)
    capture.assert_not_called()


@pytest.mark.parametrize("question,conditions,expected", [
    ("韩国入境香水规定？", {}, {"destination_country": "KR", "direction": "INBOUND"}),
    ("香水入境有什么规定？", {"destination_country": "KR", "travel_direction": "입국"},
     {"destination_country": "KR", "direction": "INBOUND"}),
    ("从韩国携带香烟入境中国，一般有什么通关规定？", {},
     {"destination_country": "CN", "direction": "INBOUND"}),
    ("从韩国入境中国，中国海关如何规定？", {},
     {"destination_country": "CN", "direction": "INBOUND"}),
    ("한국에 입국할 때 향수 규정은?", {}, {"destination_country": "KR", "direction": "INBOUND"}),
    ("韩国旅客的免税规定？", {}, None),
    ("从韩国出发有什么规定？", {}, None),
    ("中国到韩国旅行有什么规定？", {}, None),
    ("携带香水入境，规定是什么？", {}, None),
    ("中国和韩国政策有什么区别？", {"destination_country": "KR"}, None),
    ("中国及韩国的免税规定？", {"destination_country": "KR"}, None),
    ("韩国资料和中国资料说法不一样，这次回中国按哪一套规定？", {"destination_country": "CN"}, None),
    ("중국과 한국의 세관 규정은?", {"destination_country": "KR"}, None),
    ("CN / KR customs rules?", {"destination_country": "KR"}, None),
    ("한국 입국 안 합니다. 세관 규정은?", {"destination_country": "KR"}, None),
    ("中国入境、韩国入境规定？", {"destination_country": "KR"}, None),
    ("중국과 한국 세관 정책 비교", {"destination_country": "KR"}, None),
    ("中国出境与韩国入境规定？", {"destination_country": "KR"}, None),
    ("可能韩国入境，规定是什么？", {"destination_country": "KR"}, None),
    ("不入境韩国，规定是什么？", {"destination_country": "KR"}, None),
    ("日本海关政策？", {"destination_country": "KR"}, None),
    ("韩国入境规定？", {"destination_country": "CN"}, None),
    ("规定？", {"destination_country": "KR", "direction": "INBOUND", "travel_direction": "출국"}, None),
])
def test_retrieval_scope_uses_confirmed_facts_without_mutating_history(question, conditions, expected):
    from app.qa.agent.conditions import retrieval_policy_scope
    before = dict(conditions)
    assert retrieval_policy_scope(question, conditions) == expected
    assert conditions == before


def scope_documents():
    return {key: SimpleNamespace(document_id=key, country=country, direction=direction, status=status)
            for key, country, direction, status in [
                ("cn", "CN", "INBOUND", "READY"), ("kr", "KR", "INBOUND", "READY"),
                ("both", "KR", "BOTH", "READY"), ("out", "KR", "OUTBOUND", "READY"),
                ("outside_snapshot", "KR", "INBOUND", "READY"),
                ("unknown", None, None, "READY"), ("pending", "KR", "INBOUND", "PROCESSING")]}


@pytest.mark.asyncio
async def test_country_scope_intersects_snapshot_and_is_shared_with_completion(monkeypatch):
    import app.storage.repo as repo
    docs = scope_documents()
    lookup = AsyncMock(return_value=docs)
    monkeypatch.setattr(repo, "get_documents", lookup)
    search = AsyncMock(return_value=[])
    load = AsyncMock(return_value=[])
    monkeypatch.setattr(repo, "search_similar", search)
    monkeypatch.setattr(repo, "get_ready_chunk_sources", load)
    async def retrieve(query, *, search_similar, **kwargs):
        return await search_similar([0.1] * 1024, top_k=5)
    monkeypatch.setattr(runtime, "retrieve", retrieve)
    async def complete(hits, *, load_chunks_fn, **kwargs):
        assert [h.document_id for h in hits] == ["kr"]
        return await load_chunks_fn(list(docs))
    monkeypatch.setattr(runtime, "complete_evidence", complete)
    snapshot = ["cn", "kr", "both", "out", "unknown", "pending"]
    adapter = RealRAG("香水入境规定？", conditions={"destination_country": "KR", "travel_direction": "입국"},
                      session_factory=object(), document_ids=snapshot)
    await adapter.retrieve("查询")
    await adapter.retrieve("改写遗漏国家")
    await adapter.complete([SimpleNamespace(document_id="cn"), SimpleNamespace(document_id="kr")])
    assert search.await_args.kwargs["document_ids"] == ("kr", "both", "unknown")
    assert load.await_args.args[0] == ["kr", "both", "unknown"]
    assert adapter.document_ids == tuple(snapshot) and snapshot == ["cn", "kr", "both", "out", "unknown", "pending"]
    assert adapter.conditions == {"destination_country": "KR", "travel_direction": "입국"}
    lookup.assert_awaited_once()  # No repeated metadata read on rewrite.


@pytest.mark.asyncio
async def test_empty_policy_intersection_never_searches_or_embeds(monkeypatch):
    import app.storage.repo as repo
    monkeypatch.setattr(repo, "get_documents", AsyncMock(return_value={"cn": scope_documents()["cn"]}))
    search = AsyncMock()
    embed = Mock()
    monkeypatch.setattr(repo, "search_similar", search)
    complete = AsyncMock()
    monkeypatch.setattr(runtime, "complete_evidence", complete)
    adapter = RealRAG("韩国入境政策？", document_ids=["cn"], session_factory=object(), embedder=embed)
    assert await adapter.retrieve("查询") == []
    assert await adapter.complete([]) == []
    search.assert_not_awaited()
    complete.assert_not_awaited()
    embed.assert_not_called()
    assert adapter.document_ids == ("cn",)


@pytest.mark.asyncio
async def test_cross_country_comparison_keeps_both_snapshot_sides(monkeypatch):
    import app.storage.repo as repo
    lookup = AsyncMock(return_value={key: value for key, value in scope_documents().items() if key in ("cn", "kr")})
    monkeypatch.setattr(repo, "get_documents", lookup)
    search = AsyncMock(return_value=[])
    monkeypatch.setattr(repo, "search_similar", search)
    async def retrieve(query, *, search_similar, **kwargs):
        return await search_similar([0.1] * 1024, top_k=5)
    monkeypatch.setattr(runtime, "retrieve", retrieve)
    adapter = RealRAG("中国和韩国政策有什么区别？", conditions={"destination_country": "CN"},
                      document_ids=["cn", "kr"], session_factory=object())
    await adapter.retrieve("CN query does not override the original comparison")
    assert search.await_args_list[0].kwargs["document_ids"] == ("cn", "kr")
    lookup.assert_awaited_once()
    assert search.await_count == 3  # Global recall plus the two country partitions.


@pytest.mark.asyncio
async def test_direct_runtime_without_snapshot_freezes_ready_scope(monkeypatch):
    import app.storage.repo as repo
    docs = list(scope_documents().values())
    listing = AsyncMock(return_value=docs)
    monkeypatch.setattr(repo, "list_documents", listing)
    monkeypatch.setattr(runtime, "retrieve", AsyncMock(return_value=[]))
    adapter = RealRAG("中国入境规定？", session_factory=object())
    await adapter.retrieve("查询")
    assert adapter._effective_document_ids == ("cn", "unknown")
    listing.assert_awaited_once()
    assert adapter.document_ids is None


@pytest.mark.asyncio
async def test_country_without_direction_does_not_exclude_outbound_rules(monkeypatch):
    import app.storage.repo as repo
    monkeypatch.setattr(repo, "get_documents", AsyncMock(return_value=scope_documents()))
    monkeypatch.setattr(runtime, "retrieve", AsyncMock(return_value=[]))
    adapter = RealRAG("香水规定？", conditions={"destination_country": "KR"},
                      document_ids=["kr", "both", "out"], session_factory=object())
    await adapter.retrieve("查询")
    assert adapter._effective_document_ids == ("kr", "both", "out")


@pytest.mark.asyncio
async def test_new_request_after_restore_recomputes_scope_without_changing_snapshot(monkeypatch):
    import app.storage.repo as repo
    from app.qa.agent import AgentState
    snapshot = ["cn", "kr", "both"]
    docs = scope_documents()
    monkeypatch.setattr(repo, "get_documents", AsyncMock(return_value=docs))
    monkeypatch.setattr(runtime, "retrieve", AsyncMock(return_value=[]))
    saved = AgentState(conditions={"destination_country": "CN"})
    first = RealRAG("携带物品入境的规定？", conditions=saved.conditions,
                    document_ids=snapshot, session_factory=object())
    await first.retrieve("查询")
    restored = AgentState.model_validate_json(saved.model_dump_json())
    second = RealRAG("携带物品入境的规定？", conditions=restored.conditions,
                     document_ids=snapshot, session_factory=object())
    await second.retrieve("恢复查询")
    assert first._effective_document_ids == second._effective_document_ids == ("cn",)
    assert first.document_ids == second.document_ids == tuple(snapshot)
    assert saved.conditions == restored.conditions == {"destination_country": "CN"}


@pytest.mark.asyncio
async def test_scope_metadata_failure_remains_shared_retrieval_error(monkeypatch):
    import app.storage.repo as repo
    from app.qa.agent import AgentError, agent_answer
    from app.qa.agent.nodes import AgentErrorType
    from tests.test_agent_graph import MockLLM
    monkeypatch.setattr(repo, "get_documents", AsyncMock(side_effect=ConnectionError("synthetic metadata outage")))
    adapter = RealRAG("韩国入境规定？", document_ids=["kr"], session_factory=object())
    deps = adapter.dependencies()
    deps["generate_fn"] = MockLLM()
    with pytest.raises(AgentError) as caught:
        await agent_answer(adapter.original_question, "scope-error", **deps)
    assert caught.value.failed_node == "retrieve"
    assert caught.value.error_type is AgentErrorType.SHARED_RETRIEVAL_ERROR
    assert caught.value.fallback_eligible is False


@pytest.mark.asyncio
async def test_empty_scope_uses_existing_insufficient_path_with_normal_budget(monkeypatch):
    import app.storage.repo as repo
    from app.qa.agent import agent_answer
    from tests.test_agent_graph import MockLLM
    monkeypatch.setattr(repo, "get_documents", AsyncMock(return_value={"cn": scope_documents()["cn"]}))
    embed = Mock()
    search = AsyncMock()
    monkeypatch.setattr(repo, "search_similar", search)
    adapter = RealRAG("韩国入境规定？", document_ids=["cn"], session_factory=object(), embedder=embed)
    deps = adapter.dependencies()
    deps["generate_fn"] = MockLLM()
    result = await agent_answer(adapter.original_question, "empty-scope", **deps)
    assert result.response.status == "INSUFFICIENT_EVIDENCE"
    assert 1 <= result.state.retrieval_count <= 3 and result.state.clarification_count == 0
    assert result.response.answer is None and result.response.citations == []
    embed.assert_not_called()
    search.assert_not_awaited()


@pytest.mark.asyncio
async def test_unified_gemma_agent_nodes_and_one_answer_repair_use_same_provider(monkeypatch):
    import json
    import httpx
    from app.adapters import config, generation
    from app.qa.agent.generation import agent_generator
    from app.qa.agent import agent_answer
    from tests.test_generation import gemma_profile
    from tests.test_agent_graph import MockLLM, dependencies
    llm = MockLLM()
    calls = []
    client = httpx.Client
    def respond(request):
        body = json.loads(request.content)
        task = json.loads(body["messages"][-1]["content"])["task"]
        calls.append(task)
        assert str(request.url) == config.GEMMA_BASE_URL + "/chat/completions"
        assert body["model"] == config.GEMMA_MODEL and body["max_tokens"] == 2048
        assert body["response_format"]["json_schema"]["name"] == task
        assert body["response_format"]["json_schema"]["strict"] is True
        if task == "generate_answer":
            llm.answer = "物品限额800美元。[E1]" if calls.count(task) == 1 else "测试资料规定物品需要申报。[E1]"
        return httpx.Response(200, json={"choices": [{"message": {"content": llm(body["messages"])}}]})
    monkeypatch.setattr(generation.httpx, "Client", lambda **kw: client(transport=httpx.MockTransport(respond), **kw))
    deps = dependencies()
    deps["generate_fn"] = agent_generator(gemma_profile(), generation.generate_text)
    result = await agent_answer("物品需要申报吗？", "unified", **deps)
    assert result.response.status == "ANSWERED"
    assert calls == ["check_conditions", "check_evidence", "check_applicability", "check_conflict",
                     "generate_answer", "generate_answer"]
    assert result.state.retrieval_count == 1 and result.state.clarification_count == 0


A09_COMPARISON = "我在韩国买了一些准备带回中国自用的普通物品。韩国资料和中国资料对旅客免税额度的说法不一样，这次回中国该按哪一套规定办理？"


@pytest.mark.parametrize("question,expected", [
    (A09_COMPARISON, ("CN", "KR")),
    ("中国和韩国政策有什么区别？", ("CN", "KR")),
    ("比较中国与韩国的免税规定。", ("CN", "KR")),
    ("中韩通关政策对比", ("CN", "KR")),
    ("중국과 한국 세관 규정을 비교해 주세요.", ("CN", "KR")),
    ("중국 정책과 한국 정책의 차이는?", ("CN", "KR")),
    ("从中国到韩国，入境韩国的海关规定是什么？", ()),
    ("中国人和韩国人的免税政策有什么区别？", ()),
    ("한국 입국 시 동반가족은 대표자가 신고하나요?", ()),
    ("不比较中国政策，只问韩国政策有什么不同？", ()),
])
def test_only_explicit_bilateral_policy_comparison_gets_country_coverage(question, expected):
    from app.qa.agent.conditions import comparison_policy_countries
    assert comparison_policy_countries(question) == expected


def bilateral_hits(country, count=5, score=0.9):
    from app.contracts import SearchHit
    return [SearchHit(chunk_id=f"{country}_{i}", document_id=country, page_number=i+1,
                      text=f"合成测试{country}条款：需要申报。", score=score-i/100) for i in range(count)]


@pytest.mark.parametrize("top_k", [2, 3, 4, 5, 8])
@pytest.mark.asyncio
async def test_a09_balanced_recall_keeps_limit_scores_and_one_embedding(monkeypatch, top_k):
    import app.storage.repo as repo
    cn, kr = bilateral_hits("cn", 8, 0.99), bilateral_hits("kr", 8, 0.6)
    monkeypatch.setattr(repo, "get_documents", AsyncMock(return_value={k:scope_documents()[k] for k in ("cn", "kr")}))
    async def search(vector, *, top_k, session_factory, document_ids):
        return kr[:top_k] if document_ids == ("kr",) else cn[:top_k]
    calls = AsyncMock(side_effect=search)
    monkeypatch.setattr(repo, "search_similar", calls)
    embed = Mock(return_value=[[0.1]*1024])
    adapter = RealRAG(A09_COMPARISON, conditions={"destination_country":"CN"}, document_ids=["cn", "kr"],
                      top_k=top_k, embedder=embed, session_factory=object())
    result = await adapter.retrieve(A09_COMPARISON)
    assert len(result) == len({h.chunk_id for h in result}) == top_k
    assert sum(h.document_id == "cn" for h in result) >= top_k//2
    assert sum(h.document_id == "kr" for h in result) >= top_k//2
    assert [h.score for h in result] == sorted([h.score for h in result], reverse=True)
    assert all(h in cn+kr for h in result)
    embed.assert_called_once_with([A09_COMPARISON])
    assert calls.await_count == 3 and len({id(c.args[0]) for c in calls.await_args_list}) == 1
    assert adapter.document_ids == ("cn", "kr") and adapter.conditions == {"destination_country":"CN"}


@pytest.mark.asyncio
async def test_p004_single_country_keeps_one_query_and_original_order(monkeypatch):
    import app.storage.repo as repo
    monkeypatch.setattr(repo, "get_documents", AsyncMock(return_value=scope_documents()))
    original = bilateral_hits("kr")
    search = AsyncMock(return_value=original)
    monkeypatch.setattr(repo, "search_similar", search)
    embed = Mock(return_value=[[0.1]*1024])
    question = "동반가족이 함께 한국에 입국하는 경우, 세관신고를 대표자 한 명이 일괄해서 할 수 있나요?"
    adapter = RealRAG(question, document_ids=["cn", "kr"], embedder=embed, session_factory=object())
    assert await adapter.retrieve(question) == original
    search.assert_awaited_once()
    assert search.await_args.kwargs["document_ids"] == ("kr",)
    embed.assert_called_once()


@pytest.mark.asyncio
async def test_comparison_missing_country_and_top_one_do_not_fake_coverage(monkeypatch):
    import app.storage.repo as repo
    monkeypatch.setattr(repo, "get_documents", AsyncMock(return_value=scope_documents()))
    cn = bilateral_hits("cn")
    search = AsyncMock(return_value=cn)
    monkeypatch.setattr(repo, "search_similar", search)
    adapter = RealRAG(A09_COMPARISON, document_ids=["cn"], embedder=Mock(return_value=[[0.1]*1024]), session_factory=object())
    assert await adapter.retrieve(A09_COMPARISON) == cn
    assert search.await_count == 2 and all(c.kwargs["document_ids"] == ("cn",) for c in search.await_args_list)
    search.reset_mock()
    adapter = RealRAG(A09_COMPARISON, document_ids=["cn", "kr"], top_k=1,
                      embedder=Mock(return_value=[[0.1]*1024]), session_factory=object())
    assert await adapter.retrieve(A09_COMPARISON) == cn[:1]
    search.assert_awaited_once()


@pytest.mark.asyncio
async def test_comparison_query_outage_does_not_return_silent_partial_success(monkeypatch):
    import app.storage.repo as repo
    from app.qa.retriever import RetrievalError
    cn = bilateral_hits("cn")
    monkeypatch.setattr(repo, "get_documents", AsyncMock(return_value={k:scope_documents()[k] for k in ("cn", "kr")}))
    search = AsyncMock(side_effect=[cn, cn, ConnectionError("synthetic KR query outage")])
    monkeypatch.setattr(repo, "search_similar", search)
    adapter = RealRAG(A09_COMPARISON, document_ids=["cn", "kr"],
                      embedder=Mock(return_value=[[0.1]*1024]), session_factory=object())
    with pytest.raises(RetrievalError):
        await adapter.retrieve(A09_COMPARISON)


@pytest.mark.asyncio
async def test_bilateral_rewrite_completion_keep_ids_and_logical_budget(monkeypatch):
    import app.storage.repo as repo
    from app.qa.agent import agent_answer
    from tests.test_agent_graph import MockLLM, document
    cn, kr = bilateral_hits("cn"), bilateral_hits("kr", score=0.6)
    monkeypatch.setattr(repo, "get_documents", AsyncMock(return_value={k:scope_documents()[k] for k in ("cn", "kr")}))
    async def search(vector, *, top_k, session_factory, document_ids):
        return kr if document_ids == ("kr",) else cn
    calls = AsyncMock(side_effect=search)
    monkeypatch.setattr(repo, "search_similar", calls)
    load = AsyncMock(return_value=[])
    monkeypatch.setattr(repo, "get_ready_chunk_sources", load)
    async def complete(hits, *, load_chunks_fn, **kwargs):
        assert {h.document_id for h in hits} == {"cn", "kr"}
        return await load_chunks_fn(["cn", "kr", "not-in-snapshot"])
    monkeypatch.setattr(runtime, "complete_evidence", complete)
    embed = Mock(return_value=[[0.1]*1024])
    adapter = RealRAG(A09_COMPARISON, document_ids=["cn", "kr"], embedder=embed, session_factory=object())
    llm = MockLLM(sufficient_at=2, answer="中国合成规定要求申报。[E1]韩国合成规定要求申报。[E4]")
    def generate(messages):
        import json
        data = json.loads(messages[-1]["content"])
        result = json.loads(llm(messages))
        if data["task"] == "check_evidence":
            support = [{"chunk_id": data["evidence"][i]["source"]["chunk_id"]} for i in (0, 3)]
            result["complete_answer"]["support"] = result["applicability"]["support"] = support
        return json.dumps(result, ensure_ascii=False)
    deps = adapter.dependencies();deps["generate_fn"] = generate
    deps["get_documents_fn"] = AsyncMock(return_value={
        "cn": {**document(), "document_id":"cn"},
        "kr": {**document(2, country="KR"), "document_id":"kr"}})
    result = await agent_answer(A09_COMPARISON, "bilateral", **deps)
    assert result.response.status == "ANSWERED"
    assert result.state.retrieval_count == 2 and result.state.clarification_count == 0
    assert result.state.evidence_ids == ["E1", "E2", "E3", "E4", "E5"]
    assert [e.hit.chunk_id for e in result.state.evidence] == ["cn_0", "cn_1", "cn_2", "kr_0", "kr_1"]
    assert calls.await_count == 6 and embed.call_count == 2
    assert all(c.args[0] == ["cn", "kr"] for c in load.await_args_list)


@pytest.mark.parametrize("fault", ["outside_snapshot", "inconsistent_chunk"])
@pytest.mark.asyncio
async def test_comparison_rejects_untrusted_or_inconsistent_candidate(monkeypatch, fault):
    import app.storage.repo as repo
    from app.qa.retriever import RetrievalError
    cn, kr = bilateral_hits("cn"), bilateral_hits("kr", score=0.6)
    broken = (cn[0].model_copy(update={"document_id":"outside"}) if fault == "outside_snapshot"
              else cn[0].model_copy(update={"text":"changed source under same chunk ID"}))
    monkeypatch.setattr(repo, "get_documents", AsyncMock(return_value={k:scope_documents()[k] for k in ("cn", "kr")}))
    monkeypatch.setattr(repo, "search_similar", AsyncMock(side_effect=[cn, [broken], kr]))
    adapter = RealRAG(A09_COMPARISON, document_ids=["cn", "kr"],
                      embedder=Mock(return_value=[[0.1]*1024]), session_factory=object())
    with pytest.raises(RetrievalError):
        await adapter.retrieve(A09_COMPARISON)
