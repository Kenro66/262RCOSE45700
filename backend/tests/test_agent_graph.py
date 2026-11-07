"""Deterministic full-graph tests: no models, embeddings or business database."""

import asyncio
import json
import socket
from unittest.mock import AsyncMock

import pytest
import httpx
from pydantic import ValidationError

from app.contracts import ChatResponse, ChatStatus, Chunk, SearchHit
from app.qa.agent import AgentError, AgentResult, AgentState, agent_answer
from app.qa.agent.graph import build_graph
from app.qa.agent.nodes import Dependencies, Nodes
from app.qa.agent.state import CachedEvidence
from app.qa.evidence_completion import SupplementalEvidence


QUESTION = "旅客携带测试物品有什么规定？"
CONDITION = {"field": "destination_country", "question": "准备入境哪个国家？",
             "type": "select", "options": ["CN", "KR"]}


def hit(index=1, text="测试资料：物品需要申报。"):
    return SearchHit(chunk_id=f"chunk_{index}", document_id=f"doc_{index}",
                     page_number=index, text=text, score=0.9)


def document(index=1, country="CN", direction="INBOUND", status="READY"):
    return {"document_id": f"doc_{index}", "filename": f"test_{index}.pdf", "status": status,
            "country": country, "direction": direction, "effective_date": "2025-01-01",
            "applicable_persons": ["GENERAL_TRAVELER"]}


class MockLLM:
    """Uses the same messages -> JSON text signature as the existing generator."""

    def __init__(self, *, sufficient_at=1, missing_pre=False, missing_post=False,
                 conflict=False, answer=None, overrides=None):
        self.sufficient_at = sufficient_at
        self.missing_pre = missing_pre
        self.missing_post = missing_post
        self.conflict = conflict
        self.answer = answer
        self.overrides = overrides or {}
        self.calls = []

    def __call__(self, messages):
        data = json.loads(messages[-1]["content"])
        task = data["task"]
        self.calls.append(data)
        if task in self.overrides:
            return self.overrides[task]
        if task in ("check_conditions", "check_applicability"):
            missing = ((self.missing_pre and task == "check_conditions") or
                       (self.missing_post and task == "check_applicability"))
            result = {"missing_conditions": [CONDITION] if missing and not data["conditions"].get("destination_country") else []}
        elif task == "check_evidence":
            supported = {"status": "supported", "affects_core": True, "reason": "Mock source supports the answer", "support": [
                {"chunk_id": data["evidence"][0]["source"]["chunk_id"]}]}
            not_required = {"status": "not_required", "affects_core": False, "reason": "Synthetic test has no additional definition", "support": []}
            sufficient = data["retrieval_count"] >= self.sufficient_at
            result = {"verdict": "SUFFICIENT" if sufficient else "MISSING_CRITICAL", "core_claim":"Synthetic test rule", "core_support":supported["support"], "definitions": not_required, "exceptions": not_required,
                      "applicability": supported, "complete_answer": supported,
                      "missing_evidence": [] if sufficient else ["More source evidence required"]}
        elif task == "rewrite_query":
            result = {"query": data["question"] + " 改写" + str(data["retrieval_count"]) + json.dumps(data["conditions"])}
        elif task == "check_conflict":
            result = {"conflict": {"reason": "同一适用范围的条款结论相反。", "side_a": ["E1"],
                                    "side_b": ["E2"]} if self.conflict else None}
        elif task == "generate_answer":
            result = {"answer": self.answer or "测试资料规定物品需要申报。[E1]", "insufficient_evidence": False}
        else:
            raise AssertionError(task)
        return json.dumps(result, ensure_ascii=False)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    original_connect = socket.socket.connect
    def connect(sock, address):
        # Windows asyncio creates its internal wakeup socket pair on loopback.
        if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
            return original_connect(sock, address)
        raise AssertionError("Agent Mock tests must not connect to the network")
    def forbidden(*args, **kwargs):
        raise AssertionError("Agent Mock tests must not connect to the network")
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)


def dependencies(llm=None, batches=None, docs=None):
    return dict(retrieve_fn=AsyncMock(side_effect=batches if batches is not None else [[hit()]]),
                get_documents_fn=AsyncMock(return_value=docs if docs is not None else {"doc_1": document()}),
                generate_fn=llm or MockLLM())


async def run(deps, **kwargs):
    return await agent_answer(QUESTION, "session_test", **deps, **kwargs)


NORMAL = ["check_conditions", "retrieve", "check_evidence", "check_applicability",
          "check_conflict", "generate_answer", "finalize"]


@pytest.mark.asyncio
async def test_compiles_and_answered_early_stop_with_compatible_citations():
    deps = dependencies()
    compiled = build_graph(Nodes(Dependencies(deps["retrieve_fn"], deps["get_documents_fn"], deps["generate_fn"])))
    assert "retrieve" in compiled.get_graph().nodes
    result = await run(deps)
    assert result.trace == NORMAL
    assert result.response.status == ChatStatus.ANSWERED
    assert isinstance(result.response, ChatResponse)
    assert isinstance(result, AgentResult)
    assert result.state.retrieval_count == deps["retrieve_fn"].await_count == 1
    assert result.response.citations[0].model_dump() == dict(
        evidence_id="E1", filename="test_1.pdf", page=1, quote=hit().text)
    assert set(result.model_dump()) == {"response", "state", "trace"}
    assert "state" not in result.response.model_dump()


@pytest.mark.asyncio
async def test_rewrite_second_retrieval_sufficient_and_deduplicates_sources():
    llm = MockLLM(sufficient_at=2)
    deps = dependencies(llm, [[hit()], [hit(), hit(2)]], {"doc_1": document(), "doc_2": document(2)})
    result = await run(deps, conditions={"destination_country": "CN"})
    assert result.response.status == ChatStatus.ANSWERED
    assert result.trace == NORMAL[:3] + ["rewrite_query", "retrieve", "check_evidence"] + NORMAL[3:]
    assert result.state.retrieval_count == deps["retrieve_fn"].await_count == 2
    assert result.state.evidence_ids == ["E1", "E2"]
    queries = [c.args[0] for c in deps["retrieve_fn"].await_args_list]
    assert queries[0] != queries[1]
    assert all("CN" in q for q in queries)


@pytest.mark.asyncio
async def test_pre_retrieval_clarification_ends_request():
    deps = dependencies(MockLLM(missing_pre=True))
    result = await run(deps)
    assert result.trace == ["check_conditions", "clarify", "finalize"]
    assert result.response.status == ChatStatus.NEEDS_CLARIFICATION
    assert result.response.clarification.questions[0].field == "destination_country"
    assert result.state.clarification_count == 1
    assert result.state.retrieval_count == 0
    deps["retrieve_fn"].assert_not_awaited()


@pytest.mark.asyncio
async def test_three_retrievals_exhausted_is_normal_business_result():
    deps = dependencies(MockLLM(sufficient_at=4), [[hit()], [hit()], [hit()]])
    result = await run(deps)
    assert result.response.status == ChatStatus.INSUFFICIENT_EVIDENCE
    assert result.trace == ["check_conditions", "retrieve", "check_evidence", "rewrite_query",
                            "retrieve", "check_evidence", "rewrite_query", "retrieve",
                            "check_evidence", "insufficient", "finalize"]
    assert result.state.retrieval_count == deps["retrieve_fn"].await_count == 3
    assert result.response.citations == []


@pytest.mark.asyncio
async def test_true_conflict_returns_both_validated_original_sources():
    deps = dependencies(MockLLM(conflict=True), [[hit(), hit(2, "测试资料：物品无需申报。")]],
                        {"doc_1": document(), "doc_2": document(2)})
    result = await run(deps)
    assert result.response.status == ChatStatus.CONFLICTING_EVIDENCE
    assert result.trace == NORMAL[:5] + ["finalize"]
    assert result.response.conflict.side_a == ["E1"]
    assert result.response.conflict.side_b == ["E2"]
    assert [c.evidence_id for c in result.response.citations] == ["E1", "E2"]
    assert result.response.citations[1].quote == "测试资料：物品无需申报。"


@pytest.mark.asyncio
@pytest.mark.parametrize("country,direction", [("KR", "INBOUND"), ("CN", "OUTBOUND")])
async def test_different_country_or_direction_cannot_be_true_conflict(country, direction):
    deps = dependencies(MockLLM(conflict=True), [[hit(), hit(2)]],
                        {"doc_1": document(), "doc_2": document(2, country, direction)})
    result = await run(deps)
    assert result.trace == NORMAL
    assert result.response.status == ChatStatus.ANSWERED
    assert result.response.conflict is None


@pytest.mark.asyncio
async def test_second_clarification_is_not_allowed():
    deps = dependencies(MockLLM(missing_pre=True))
    result = await run(deps, agent_state=AgentState(clarification_count=1))
    assert result.trace == ["check_conditions", "finalize"]
    assert result.response.status == ChatStatus.INSUFFICIENT_EVIDENCE
    assert result.state.clarification_count == 1
    deps["retrieve_fn"].assert_not_awaited()


@pytest.mark.asyncio
async def test_restored_budget_allows_only_one_more_retrieval():
    deps = dependencies(MockLLM(sufficient_at=4), [[hit()]])
    result = await run(deps, agent_state=AgentState(retrieval_count=2))
    assert result.state.retrieval_count == 3
    assert deps["retrieve_fn"].await_count == 1
    assert result.trace == ["check_conditions", "retrieve", "check_evidence", "insufficient", "finalize"]


@pytest.mark.asyncio
async def test_exhausted_budget_never_calls_retrieval():
    deps = dependencies()
    result = await run(deps, agent_state=AgentState(retrieval_count=3))
    assert result.trace == ["check_conditions", "insufficient", "finalize"]
    assert result.state.retrieval_count == 3
    deps["retrieve_fn"].assert_not_awaited()


@pytest.mark.asyncio
async def test_post_retrieval_clarification_json_roundtrip_reuses_evidence():
    deps = dependencies(MockLLM(missing_post=True))
    first = await run(deps)
    assert first.trace == NORMAL[:4] + ["clarify", "finalize"]
    assert first.response.status == ChatStatus.NEEDS_CLARIFICATION
    assert first.state.retrieval_count == first.state.clarification_count == 1
    saved = AgentState.model_validate_json(first.state.model_dump_json())
    assert saved.evidence[0].hit.chunk_id == "chunk_1"
    assert saved.evidence[0].hit.text == hit().text
    second = await run(deps, agent_state=saved, question_id=first.response.question_id,
                       conditions={"destination_country": "CN"})
    assert second.trace == ["check_conditions", "check_evidence"] + NORMAL[3:]
    assert second.response.status == ChatStatus.ANSWERED
    assert second.state.retrieval_count == deps["retrieve_fn"].await_count == 1
    assert second.state.clarification_count == 1
    assert second.response.question_id == first.response.question_id
    assert second.state.conditions == {"destination_country": "CN"}
    assert deps["get_documents_fn"].await_count == 2
    assert AgentResult.model_validate_json(second.model_dump_json()).state == second.state


@pytest.mark.asyncio
async def test_restored_evidence_can_answer_even_when_budget_exhausted():
    deps = dependencies()
    first = await run(deps)
    saved = first.state.model_copy(update={"retrieval_count": 3})
    result = await run(deps, agent_state=saved, question_id=first.response.question_id)
    assert result.response.status == ChatStatus.ANSWERED
    assert result.state.retrieval_count == 3
    assert deps["retrieve_fn"].await_count == 1


@pytest.mark.asyncio
async def test_no_second_post_retrieval_clarification():
    deps = dependencies(MockLLM(missing_post=True))
    result = await run(deps, agent_state=AgentState(clarification_count=1))
    assert result.trace == NORMAL[:4] + ["finalize"]
    assert result.response.status == ChatStatus.INSUFFICIENT_EVIDENCE
    assert result.state.clarification_count == 1


@pytest.mark.asyncio
async def test_empty_hits_are_insufficient_not_a_technical_error():
    deps = dependencies(batches=[[], [], []])
    result = await run(deps)
    assert result.response.status == ChatStatus.INSUFFICIENT_EVIDENCE
    assert result.trace.count("retrieve") == 3
    assert not any(c["task"] == "generate_answer" for c in deps["generate_fn"].calls)


@pytest.mark.asyncio
async def test_stale_non_ready_cached_sources_are_not_reused():
    deps = dependencies()
    first = await run(deps)
    stale_deps = dependencies(batches=[[], []], docs={"doc_1": document(status="FAILED")})
    result = await run(stale_deps, agent_state=first.state, question_id=first.response.question_id)
    assert result.state.retrieval_count == 3
    assert result.state.evidence == []
    assert result.response.status == ChatStatus.INSUFFICIENT_EVIDENCE


@pytest.mark.asyncio
async def test_completion_sources_survive_serialization_as_chunks():
    chunk = Chunk(schema_version="1.0", chunk_id="supp_1", document_id="doc_1", chunk_index=2,
                  page_index=1, page_number=2, text="测试补充原文。", section_title=None,
                  language="zh", token_count=6, char_count=7, content_hash=None)
    completion = AsyncMock(return_value=[SupplementalEvidence(chunk, "chunk_1", ("continuation",))])
    deps = dependencies()
    result = await run(deps, complete_evidence_fn=completion)
    completion.assert_awaited_once()
    assert result.state.evidence_ids == ["E1", "E2"]
    restored = AgentState.model_validate_json(result.state.model_dump_json())
    assert isinstance(restored.evidence[1].hit, Chunk)
    assert restored.evidence[1].hit == chunk


@pytest.mark.asyncio
async def test_retrieval_technical_failure_raises_with_consumed_budget():
    deps = dependencies()
    deps["retrieve_fn"] = AsyncMock(side_effect=RuntimeError("mock retrieval failure"))
    with pytest.raises(AgentError) as caught:
        await run(deps)
    assert caught.value.state.retrieval_count == 1
    assert caught.value.trace == ["check_conditions", "retrieve"]
    assert isinstance(caught.value.__cause__, RuntimeError)


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ['not JSON', '{"sufficient":"yes"}',
                                  '{"sufficient":true,"sufficient":false}',
                                  '{"sufficient":true,"extra":1}'])
async def test_invalid_structured_llm_output_is_technical_failure(raw):
    deps = dependencies(MockLLM(overrides={"check_evidence": raw}))
    with pytest.raises(AgentError) as caught:
        await run(deps)
    assert caught.value.state.retrieval_count == 1


@pytest.mark.asyncio
async def test_unknown_conflict_evidence_is_rejected():
    llm = MockLLM(overrides={"check_conflict": json.dumps({"conflict": {
        "reason": "测试", "side_a": ["E1"], "side_b": ["E999"]}})})
    with pytest.raises(AgentError):
        await run(dependencies(llm))


@pytest.mark.asyncio
async def test_unknown_scope_does_not_establish_conflict():
    deps = dependencies(MockLLM(conflict=True), [[hit(), hit(2)]],
                        {"doc_1": document(country=None), "doc_2": document(2)})
    result = await run(deps)
    assert result.response.status == ChatStatus.INSUFFICIENT_EVIDENCE
    assert result.response.conflict is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["question", "session", "question_id"])
async def test_state_cannot_be_associated_with_another_question(change):
    deps = dependencies()
    first = await run(deps)
    with pytest.raises(ValueError, match="different logical"):
        await agent_answer("另一个问题？" if change == "question" else QUESTION,
                           "other_session" if change == "session" else "session_test",
                           agent_state=first.state,
                           question_id="other_question" if change == "question_id" else first.response.question_id,
                           **deps)
    assert deps["retrieve_fn"].await_count == 1


@pytest.mark.asyncio
async def test_cancellation_propagates_without_fallback():
    deps = dependencies()
    deps["retrieve_fn"] = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await run(deps)


@pytest.mark.asyncio
async def test_async_generator_is_awaited_and_concurrent_states_are_independent():
    llm = MockLLM()
    async def generate(messages):
        await asyncio.sleep(0)
        return llm(messages)
    a, b = dependencies(generate), dependencies(generate)
    ra, rb = await asyncio.gather(run(a), run(b))
    assert ra.state.retrieval_count == rb.state.retrieval_count == 1
    assert ra.response.question_id != rb.response.question_id
    assert ra.trace == rb.trace == NORMAL


@pytest.mark.asyncio
async def test_missing_dependencies_never_enable_a_real_default():
    with pytest.raises(AgentError, match="Inject retrieve_fn"):
        await agent_answer(QUESTION)


@pytest.mark.asyncio
async def test_restored_insufficient_evidence_uses_only_remaining_budget():
    deps = dependencies(MockLLM(missing_post=True))
    first = await run(deps)
    retry = dependencies(MockLLM(sufficient_at=4), [[hit()], [hit()]])
    result = await run(retry, agent_state=AgentState.model_validate_json(first.state.model_dump_json()),
                       question_id=first.response.question_id, conditions={"destination_country": "CN"})
    assert result.state.retrieval_count == 3
    assert deps["retrieve_fn"].await_count + retry["retrieve_fn"].await_count == 3
    assert result.trace.count("rewrite_query") == result.trace.count("retrieve") == 2
    assert result.state.clarification_count == 1
    assert result.response.status == ChatStatus.INSUFFICIENT_EVIDENCE


@pytest.mark.asyncio
async def test_generation_insufficiency_does_not_raise_or_call_retrieval_again():
    llm = MockLLM(overrides={"generate_answer": json.dumps({"answer": "证据不足。", "insufficient_evidence": True})})
    deps = dependencies(llm)
    result = await run(deps)
    assert result.response.status == ChatStatus.INSUFFICIENT_EVIDENCE
    assert result.trace == NORMAL
    assert deps["retrieve_fn"].await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["测试资料规定需要申报。[E999]", "示例限量为999件。[E1]"])
async def test_unusable_citations_or_unsupported_numbers_return_insufficient(answer):
    result = await run(dependencies(MockLLM(answer=answer)))
    assert result.response.status == ChatStatus.INSUFFICIENT_EVIDENCE
    assert result.response.citations == []


@pytest.mark.asyncio
async def test_partly_uncited_answer_is_a_technical_validation_failure():
    with pytest.raises(AgentError, match="citation or language"):
        await run(dependencies(MockLLM(answer="测试资料规定需要申报。[E1]还需要额外手续。")))


@pytest.mark.asyncio
async def test_completion_error_is_technical_and_preserves_retrieval_budget():
    completion = AsyncMock(side_effect=RuntimeError("mock completion unavailable"))
    with pytest.raises(AgentError) as caught:
        await run(dependencies(), complete_evidence_fn=completion)
    assert caught.value.state.retrieval_count == 1


@pytest.mark.asyncio
async def test_inconsistent_stable_chunk_id_is_technical_error():
    deps = dependencies(MockLLM(sufficient_at=2), [[hit()], [hit(text="不同的原文。")]])
    with pytest.raises(AgentError, match="inconsistent source") as caught:
        await run(deps)
    assert caught.value.state.retrieval_count == 2


@pytest.mark.asyncio
async def test_known_conditions_are_not_requested_again_even_if_model_asks():
    llm = MockLLM(overrides={"check_conditions": json.dumps({"missing_conditions": [CONDITION]})})
    result = await run(dependencies(llm), conditions={"destination_country": "CN"})
    assert result.trace == NORMAL
    assert result.state.clarification_count == 0


@pytest.mark.asyncio
async def test_both_direction_overlaps_inbound_for_conflict():
    deps = dependencies(MockLLM(conflict=True), [[hit(), hit(2)]],
                        {"doc_1": document(direction="BOTH"), "doc_2": document(2)})
    result = await run(deps, conditions={"destination_country": "CN", "direction": "INBOUND"})
    assert result.response.status == ChatStatus.CONFLICTING_EVIDENCE


@pytest.mark.asyncio
async def test_foreign_policy_pair_does_not_conflict_for_user_destination():
    deps = dependencies(MockLLM(conflict=True), [[hit(), hit(2)]],
                        {"doc_1": document(country="KR"), "doc_2": document(2, country="KR")})
    result = await run(deps, conditions={"destination_country": "CN"})
    assert result.response.conflict is None


@pytest.mark.asyncio
async def test_korean_generation_keeps_existing_language_and_citation_checks():
    llm = MockLLM(answer="테스트 자료에 따르면 신고가 필요합니다.[E1]")
    result = await agent_answer("여행자 휴대품 규정은 무엇인가요?", **dependencies(llm))
    assert result.response.status == ChatStatus.ANSWERED
    assert result.response.citations[0].quote == hit().text


@pytest.mark.asyncio
@pytest.mark.parametrize("question", [QUESTION, "여행자 휴대품 규정은 무엇인가요?"])
async def test_generation_reuses_single_rag_definition_and_opening_citation_rules(question):
    from app.qa.prompts import build_messages
    from app.qa.citation import Evidence
    seen = []
    llm = MockLLM(answer="테스트 자료에 따르면 신고가 필요합니다.[E1]" if "여행자" in question else None)
    def generate(messages):
        if json.loads(messages[-1]["content"])["task"] == "generate_answer":
            seen.append(messages[0]["content"])
        return llm(messages)
    await agent_answer(question, **dependencies(generate))
    language = "ko" if "여행자" in question else "zh"
    expected = build_messages(question, language, [Evidence("E1", hit())])[0]["content"]
    assert expected in seen[0]


@pytest.mark.asyncio
async def test_condition_prompts_distinguish_general_rules_from_personal_eligibility():
    seen = []
    llm = MockLLM()
    def generate(messages):
        task = json.loads(messages[-1]["content"])["task"]
        if task in ("check_conditions", "check_applicability"):
            seen.append(messages[0]["content"])
        return llm(messages)
    await run(dependencies(generate))
    assert len(seen) == 2
    assert all("general policy questions" in s and "exceptions conditionally" in s for s in seen)


@pytest.mark.parametrize("state", [dict(retrieval_count=4), dict(clarification_count=2),
                                   dict(retrieval_count=True), dict(evidence_ids=["E1"]),
                                   dict(evidence=[CachedEvidence(evidence_id="E1", hit=hit())])])
def test_invalid_state_and_id_only_cache_are_rejected(state):
    with pytest.raises(ValidationError):
        AgentState(**state)
