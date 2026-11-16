"""Offline Stage 6B service boundary regressions (no HTTP or real services)."""

import json
from unittest.mock import AsyncMock

import pytest

from app.contracts import ChatStatus, Chunk
from app.qa.agent import AgentError, AgentState
from app.qa.agent.state import AgentChatResponse
from app.qa.agent import service
from app.qa.evidence_completion import SupplementalEvidence
from test_agent_graph import (
    CONDITION, QUESTION, MockLLM, dependencies, document, hit, no_network, run,
)


@pytest.mark.asyncio
async def test_model_dict_and_json_snapshots_restore_equivalently():
    first = await run(dependencies(MockLLM(missing_post=True)))
    forms = [first.state, first.state.model_dump(), json.loads(first.state.model_dump_json())]
    results = []
    for state in forms:
        deps = dependencies()
        result = await run(deps, agent_state=state, question_id=first.response.question_id,
                           conditions={"destination_country": "CN"})
        deps["retrieve_fn"].assert_not_awaited()
        assert result.state.retrieval_count == result.state.clarification_count == 1
        assert result.state.evidence == first.state.evidence
        assert result.state.retrieval_queries == first.state.retrieval_queries
        results.append(result.model_dump())
    assert results[0] == results[1] == results[2]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", list(AgentState.model_fields))
async def test_incomplete_dictionary_snapshot_is_not_reset(field):
    state = AgentState(retrieval_count=3, clarification_count=1).model_dump()
    del state[field]
    deps = dependencies()
    with pytest.raises(ValueError, match="Incomplete AgentState"):
        await run(deps, agent_state=state)
    deps["retrieve_fn"].assert_not_awaited()
    assert not deps["generate_fn"].calls


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"retrieval_count": -1}, {"retrieval_count": 4}, {"retrieval_count": "0"},
    {"clarification_count": 2}, {"evidence_ids": ["E1"]}, {"evidence": [{}]},
    {"retrieval_queries": [QUESTION]},
    {"unexpected": True},
])
async def test_damaged_dictionary_snapshot_is_rejected(change):
    state = {**AgentState().model_dump(), **change}
    deps = dependencies()
    with pytest.raises(ValueError):
        await run(deps, agent_state=state)
    deps["retrieve_fn"].assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("question_id", "another"), ("session_id", "another"), ("question", "another"),
])
async def test_dictionary_association_mismatch_is_rejected(field, value):
    first = await run(dependencies())
    state = first.state.model_dump()
    state[field] = value
    with pytest.raises(ValueError, match="different logical question/session"):
        await run(dependencies(), agent_state=state, question_id=first.response.question_id)


@pytest.mark.asyncio
async def test_dictionary_preserves_exhausted_budgets_and_no_second_clarification():
    state = AgentState(retrieval_count=3, clarification_count=1).model_dump()
    for llm in (MockLLM(), MockLLM(missing_pre=True)):
        deps = dependencies(llm)
        result = await run(deps, agent_state=state)
        assert result.response.status == ChatStatus.INSUFFICIENT_EVIDENCE
        assert result.state.retrieval_count == 3
        assert result.state.clarification_count == 1
        assert "clarify" not in result.trace
        deps["retrieve_fn"].assert_not_awaited()


@pytest.mark.asyncio
async def test_dictionary_resume_only_uses_remaining_retrieval():
    deps = dependencies(MockLLM(sufficient_at=4))
    result = await run(deps, agent_state=AgentState(retrieval_count=2).model_dump())
    assert result.state.retrieval_count == 3
    assert deps["retrieve_fn"].await_count == 1


@pytest.mark.asyncio
async def test_dictionary_restored_evidence_is_rechecked_for_readiness():
    first = await run(dependencies())
    state = first.state.model_dump()
    state["retrieval_count"] = 3
    deps = dependencies(docs={"doc_1": document(status="FAILED")})
    result = await run(deps, agent_state=state, question_id=first.response.question_id)
    assert result.response.status == ChatStatus.INSUFFICIENT_EVIDENCE
    assert not result.state.evidence
    deps["retrieve_fn"].assert_not_awaited()


@pytest.mark.asyncio
async def test_dictionary_roundtrip_keeps_supplemental_chunk_and_query_history():
    chunk = Chunk(chunk_id="supplement", document_id="doc_1", chunk_index=1,
                  page_index=1, page_number=2, text="补充测试原文。", section_title=None,
                  language="zh", token_count=5, char_count=7, content_hash=None)
    deps = dependencies(MockLLM(missing_post=True))
    deps["complete_evidence_fn"] = AsyncMock(return_value=[
        SupplementalEvidence(chunk, "chunk_1", ("continuation",))])
    first = await run(deps)
    state = json.loads(first.state.model_dump_json())
    second_deps = dependencies()
    second = await run(second_deps, agent_state=state, question_id=first.response.question_id,
                       conditions={"destination_country": "CN"})
    assert second.state.evidence == first.state.evidence
    assert isinstance(second.state.evidence[1].hit, Chunk)
    second_deps["retrieve_fn"].assert_not_awaited()


def conflict_deps(side_a=None, side_b=None, reason="测试条款相互冲突。", count=2):
    payload = {"conflict": {"reason": reason, "side_a": ["E1"] if side_a is None else side_a,
                            "side_b": ["E2"] if side_b is None else side_b}}
    llm = MockLLM(overrides={"check_conflict": json.dumps(payload)})
    return dependencies(llm, batches=[[hit(i) for i in range(1, count + 1)]],
                        docs={f"doc_{i}": document(i) for i in range(1, count + 1)})


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"side_a": ["E1", "E1"]}, {"side_b": ["E2", "E2"]},
    {"side_b": ["E1"]}, {"side_b": ["E9"]}, {"side_b": ["[E2]"]},
    {"side_a": []}, {"side_b": []},
    {"reason": ""}, {"reason": " \t\n"},
])
async def test_invalid_conflict_fails_through_service_with_state(changes):
    with pytest.raises(AgentError) as caught:
        await run(conflict_deps(**changes))
    assert caught.value.state.retrieval_count == 1
    assert "check_conflict" in caught.value.trace


@pytest.mark.asyncio
@pytest.mark.parametrize("side_a,side_b,count", [
    (["E1"], ["E2"], 2), (["E1", "E2"], ["E3", "E4"], 4),
])
async def test_valid_conflict_final_response_has_all_original_citations(side_a, side_b, count):
    result = await run(conflict_deps(side_a, side_b, count=count))
    response = result.response
    assert response.status == ChatStatus.CONFLICTING_EVIDENCE
    assert response.answer is response.clarification is None
    assert response.conflict.side_a == side_a
    assert response.conflict.side_b == side_b
    assert [c.evidence_id for c in response.citations] == side_a + side_b
    assert all(c.quote == hit().text and c.filename == f"test_{c.page}.pdf"
               for c in response.citations)
    assert "state" not in response.model_dump() and "trace" not in response.model_dump()


@pytest.mark.asyncio
@pytest.mark.parametrize("unavailable", [None, "FAILED"])
async def test_resolver_filters_unavailable_side_to_business_insufficiency(monkeypatch, unavailable):
    original = service.resolve_citations
    def resolve(text, evidence, documents):
        if unavailable is None:
            documents.pop("doc_2")
        else:
            documents["doc_2"]["status"] = unavailable
        return original(text, evidence, documents)
    monkeypatch.setattr(service, "resolve_citations", resolve)
    result = await run(conflict_deps())
    assert result.response.status == ChatStatus.INSUFFICIENT_EVIDENCE
    assert result.response.conflict is result.response.answer is None
    assert result.response.citations == []
    assert result.state.retrieval_count == 1


@pytest.mark.asyncio
async def test_missing_final_citation_from_ready_source_is_technical(monkeypatch):
    original = service.resolve_citations
    def resolve(*args):
        text, citations = original(*args)
        return text, citations[:1]
    monkeypatch.setattr(service, "resolve_citations", resolve)
    with pytest.raises(AgentError, match="READY source") as caught:
        await run(conflict_deps())
    assert caught.value.state.retrieval_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["filename", "quote", "page", "duplicate"])
async def test_final_citation_contract_is_checked_in_service(monkeypatch, damage):
    original = service.resolve_citations
    def resolve(*args):
        text, citations = original(*args)
        if damage == "duplicate":
            citations.append(citations[0])
        else:
            citations[0] = citations[0].model_copy(update={damage: 0 if damage == "page" else "  "})
        return text, citations
    monkeypatch.setattr(service, "resolve_citations", resolve)
    with pytest.raises(AgentError) as caught:
        await run(conflict_deps())
    assert caught.value.state.retrieval_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status,updates", [
    ("CONFLICTING_EVIDENCE", {"answer": "不得裁决冲突"}),
    ("CONFLICTING_EVIDENCE", {"clarification": {"questions": [CONDITION]}}),
    ("CONFLICTING_EVIDENCE", {"conflict": None}),
    ("CONFLICTING_EVIDENCE", {"citations": []}),
    ("ANSWERED", {}), ("INSUFFICIENT_EVIDENCE", {}), ("NEEDS_CLARIFICATION", {}),
])
async def test_response_rejects_invalid_status_field_combinations(status, updates):
    result = await run(conflict_deps())
    data = {**result.response.model_dump(), "status": status, **updates}
    with pytest.raises(ValueError):
        AgentChatResponse.model_validate(data)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["state", "trace"])
async def test_response_does_not_accept_internal_fields(field):
    result = await run(conflict_deps())
    with pytest.raises(ValueError):
        AgentChatResponse.model_validate({**result.response.model_dump(), field: {}})


@pytest.mark.asyncio
async def test_dictionary_query_history_prevents_repeating_retrieval():
    first = await run(dependencies())
    state = first.state.model_dump()
    state.update(evidence=[], evidence_ids=[])
    deps = dependencies()
    result = await run(deps, agent_state=state, question_id=first.response.question_id)
    assert result.response.status == ChatStatus.INSUFFICIENT_EVIDENCE
    assert result.state.retrieval_count == 1
    assert result.state.retrieval_queries == [QUESTION]
    deps["retrieve_fn"].assert_not_awaited()


@pytest.mark.asyncio
async def test_real_wrapper_normalizes_dictionary_before_dependency_setup(monkeypatch):
    from app.qa.agent import runtime

    state = AgentState(conditions={"destination_country": "CN"}).model_dump()
    entered = []
    class FakeRuntime:
        def __init__(self, question, *, conditions):
            assert conditions == {"destination_country": "CN", "direction": "INBOUND"}
        async def __aenter__(self):
            entered.append(True)
            return self
        async def __aexit__(self, *args):
            pass
        def dependencies(self):
            return {}
    answer = AsyncMock(return_value="result")
    monkeypatch.setattr(runtime, "RealRAG", FakeRuntime)
    monkeypatch.setattr(runtime, "agent_answer", answer)
    assert await runtime.agent_answer_real(QUESTION, agent_state=state,
                                          conditions={"direction": "INBOUND"}) == "result"
    assert answer.await_args.kwargs["agent_state"] == AgentState.model_validate(state)
    assert entered == [True]
    with pytest.raises(ValueError, match="Incomplete AgentState"):
        await runtime.agent_answer_real(QUESTION, agent_state={})
    assert entered == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("updates", [
    {"answer": "不能替用户裁决冲突"},
    {"clarification": {"questions": [CONDITION]}},
])
async def test_final_service_assembly_rejects_conflict_status_mismatch(monkeypatch, updates):
    from app.qa.agent.nodes import Nodes

    async def finalize(self, state):
        return updates
    monkeypatch.setattr(Nodes, "finalize", finalize)
    with pytest.raises(AgentError) as caught:
        await run(conflict_deps())
    assert caught.value.state.retrieval_count == 1
