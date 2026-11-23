"""结果可复现的完整工作流测试，不调用模型、嵌入服务或业务数据库。"""

import asyncio
import inspect
import json
import socket
from unittest.mock import AsyncMock

import pytest
import httpx
from pydantic import ValidationError

from app.contracts import ChatResponse, ChatStatus, Chunk, SearchHit
from app.qa.agent import AgentError, AgentResult, AgentState, agent_answer
from app.qa.agent.graph import build_graph
from app.qa.agent.nodes import AgentErrorType, Dependencies, Nodes
from app.adapters.generation import GenerationError
from app.qa.retriever import RetrievalError
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
    """沿用现有生成器接收消息并返回 JSON 文本的接口形式。"""

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
        # Windows 异步事件循环通过本机回环套接字对实现内部唤醒。
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


def condition_question(field, *, options=None):
    return {"field": field, "question": "Synthetic applicability fact?",
            "type": "select" if options is not None else "text", "options": options or []}


@pytest.mark.parametrize("field,options", [
    ("destination_country", ["CN", "KR"]), ("direction", ["INBOUND", "OUTBOUND"]),
    ("item_name", None), ("quantity", None), ("age", None), ("traveler_status", None),
    ("is_professional_crew", ["是", "否"]), ("special_policy_fact", None)])
@pytest.mark.asyncio
async def test_new_core_and_dynamic_conditions_preserve_open_field_contract(field, options):
    question = condition_question(field, options=options)
    llm = MockLLM(overrides={"check_conditions": json.dumps({"missing_conditions": [question]})})
    result = await run(dependencies(llm))
    assert result.response.status == ChatStatus.NEEDS_CLARIFICATION
    assert result.response.clarification.questions[0].model_dump() == question
    assert result.state.clarification_count == 1 and result.state.retrieval_count == 0


@pytest.mark.parametrize("questions", [
    [condition_question("travel_direction", options=["입국", "출국"])],
    [condition_question("jurisdiction")], [condition_question("country")],
    [condition_question("Direction")], [condition_question("item-name")],
    [condition_question(" item_name")], [condition_question("item__name")],
    [condition_question("物品")], [condition_question("item_name"), condition_question("item_name")],
    [condition_question("direction", options=["입국", "출국"])],
    [condition_question("direction", options=["INBOUND", "INBOUND"])],
    [condition_question("destination_country", options=["中国", "韩国"])],
    [{"field": "item_name", "question": "Item?", "type": "text", "options": ["invented"]}],
])
@pytest.mark.asyncio
async def test_invalid_new_conditions_are_workflow_errors_without_rewriting(questions):
    llm = MockLLM(overrides={"check_conditions": json.dumps({"missing_conditions": questions})})
    deps = dependencies(llm)
    with pytest.raises(AgentError) as caught:
        await run(deps)
    assert caught.value.failed_node == "check_conditions"
    assert caught.value.error_type is AgentErrorType.AGENT_WORKFLOW_ERROR
    assert caught.value.state.conditions == {} and caught.value.state.retrieval_count == 0
    assert len(llm.calls) == 1
    deps["retrieve_fn"].assert_not_awaited()


@pytest.mark.parametrize("key,value,expected", [("travel_direction", "입국", "INBOUND"),
    ("travel_direction", "출국", "OUTBOUND"), ("travel_direction", "入境", "INBOUND"),
    ("travel_direction", "出境", "OUTBOUND"), ("direction", "입국", "INBOUND"),
    ("direction", "출국", "OUTBOUND"), ("travel_direction", "inbound", "INBOUND")])
@pytest.mark.asyncio
async def test_historical_direction_is_read_without_mutating_restored_state(key, value, expected):
    from app.qa.agent.conditions import condition_view
    raw = {key: value}
    state = AgentState(conditions=raw)
    serialized = state.model_dump_json()
    question = condition_question("direction", options=["INBOUND", "OUTBOUND"])
    seen = []
    llm = MockLLM(overrides={"check_conditions": json.dumps({"missing_conditions": [question]})})
    def generate(messages):
        data = json.loads(messages[-1]["content"])
        if data["task"] in ("check_conditions", "check_applicability"):
            seen.append(data)
        return llm(messages)
    result = await run(dependencies(generate), agent_state=state)
    assert result.response.status == ChatStatus.ANSWERED and result.trace == NORMAL
    assert condition_view(raw)["direction"] == expected
    assert all(data["conditions"] == raw and data["confirmed_conditions"]["direction"] == expected for data in seen)
    assert result.state.conditions == raw and state.model_dump_json() == serialized
    assert result.state.clarification_count == 0


@pytest.mark.parametrize("raw", [
    {"direction": "INBOUND", "travel_direction": "출국"},
    {"direction": "OUTBOUND", "travel_direction": "입국"}])
@pytest.mark.asyncio
async def test_conflicting_aliases_refuse_without_overwrite_fallback_or_model(raw, caplog):
    deps = dependencies()
    result = await run(deps, agent_state=AgentState(conditions=raw))
    assert result.response.status == ChatStatus.INSUFFICIENT_EVIDENCE
    assert result.trace == ["check_conditions", "finalize"]
    assert result.state.conditions == raw and result.state.retrieval_count == 0
    assert result.state.clarification_count == 0
    assert "Conflicting conditions for direction" in caplog.text
    assert not deps["generate_fn"].calls
    deps["retrieve_fn"].assert_not_awaited()


def test_ambiguous_country_aliases_and_direction_are_not_inferred():
    from app.qa.agent.conditions import condition_view
    raw = {"country": "KR", "jurisdiction": "CN", "travel_direction": "回国"}
    assert condition_view(raw) == raw
    equivalent = {"direction": "INBOUND", "travel_direction": "입국"}
    assert condition_view(equivalent)["direction"] == "INBOUND"
    assert equivalent == {"direction": "INBOUND", "travel_direction": "입국"}


def test_legacy_nonstring_values_survive_the_read_only_view():
    from app.qa.agent.conditions import condition_view
    raw = {"age": 18, "quantity": ["historical value"], "destination_country": "한국"}
    view = condition_view(raw)
    assert view["age"] == 18 and view["quantity"] == raw["quantity"]
    assert view["destination_country"] == "KR" and raw["destination_country"] == "한국"


@pytest.mark.parametrize("country", ["JP", "日本", "일본", "US"])
@pytest.mark.asyncio
async def test_explicit_unsupported_policy_never_forces_cn_kr(country):
    deps = dependencies(MockLLM(missing_pre=True))
    result = await run(deps, conditions={"destination_country": country})
    assert result.response.status == ChatStatus.INSUFFICIENT_EVIDENCE
    assert result.response.clarification is None and result.state.conditions["destination_country"] == country
    assert result.state.clarification_count == result.state.retrieval_count == 0
    assert not deps["generate_fn"].calls


@pytest.mark.asyncio
async def test_legacy_second_turn_restores_original_ids_values_and_budget():
    from app.qa.agent.state import ClarificationQuestion
    # Historical state parsing must still accept the old field/options.
    old_question = ClarificationQuestion.model_validate(condition_question("travel_direction", options=["입국", "출국"]))
    state = AgentState(question=QUESTION, session_id="session_test", question_id="legacy_q",
        clarification_count=1, retrieval_count=2, retrieval_queries=["old1", "old2"])
    q = condition_question("direction", options=["INBOUND", "OUTBOUND"])
    llm = MockLLM(overrides={"check_conditions": json.dumps({"missing_conditions": [q]})})
    deps = dependencies(llm)
    result = await run(deps, agent_state=json.loads(state.model_dump_json()), question_id="legacy_q",
                       conditions={old_question.field: old_question.options[0]})
    assert result.response.status == ChatStatus.ANSWERED
    assert result.state.conditions == {"travel_direction": "입국"}
    assert result.state.question == QUESTION and result.state.question_id == "legacy_q"
    assert result.state.session_id == "session_test"
    assert result.state.retrieval_count == 3 and result.state.clarification_count == 1
    assert deps["retrieve_fn"].await_count == 1


@pytest.mark.asyncio
async def test_p001_explicit_conditions_preserve_raw_pilot_and_filter_known_direction():
    from pathlib import Path
    pilot = json.loads((Path(__file__).resolve().parents[2] / "eval/pilot43_questions_v1_40.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert pilot["qid"] == "P001" and "입국" not in pilot["question"]
    message = "적용 조건: 한국 입국; 일반 여행자; 별도면세 대상 제외.\n" + pilot["question"]
    direction = condition_question("direction", options=["INBOUND", "OUTBOUND"])
    # Synthetic Korean answer: this tests field flow, not P001 policy accuracy.
    llm = MockLLM(answer="테스트 자료에 따르면 물품은 신고해야 합니다.[E1]",
                  overrides={"check_conditions": json.dumps({"missing_conditions": [direction]})})
    deps = dependencies(llm, docs={"doc_1": document(country="KR")})
    result = await agent_answer(message, "P001-mock", conditions={"destination_country": "KR", "direction": "INBOUND"}, **deps)
    assert result.response.status == ChatStatus.ANSWERED and result.state.clarification_count == 0
    assert result.state.question == message and result.state.retrieval_count == 1


@pytest.mark.parametrize("field,value", [("destination_country", "KR"), ("direction", "INBOUND"),
    ("item_name", "香水"), ("quantity", "100毫升"), ("age", "18"), ("traveler_status", "普通旅客")])
@pytest.mark.asyncio
async def test_all_known_core_fields_are_filtered_without_reasking(field, value):
    options = {"destination_country": ["CN", "KR"], "direction": ["INBOUND", "OUTBOUND"]}.get(field)
    q = condition_question(field, options=options)
    llm = MockLLM(overrides={"check_conditions": json.dumps({"missing_conditions": [q]}),
                             "check_applicability": json.dumps({"missing_conditions": [q]})})
    result = await run(dependencies(llm), conditions={field: value})
    assert result.response.status == ChatStatus.ANSWERED
    assert result.state.clarification_count == 0 and result.state.conditions == {field: value}


@pytest.mark.asyncio
async def test_post_retrieval_duplicate_fields_are_rejected_without_second_model_call():
    q = condition_question("is_professional_crew", options=["是", "否"])
    llm = MockLLM(overrides={"check_applicability": json.dumps({"missing_conditions": [q, q]})})
    with pytest.raises(AgentError) as caught:
        await run(dependencies(llm))
    assert caught.value.failed_node == "check_applicability"
    assert caught.value.error_type is AgentErrorType.AGENT_WORKFLOW_ERROR
    assert caught.value.state.retrieval_count == 1
    assert sum(c["task"] == "check_applicability" for c in llm.calls) == 1


@pytest.mark.asyncio
async def test_historical_direction_is_used_by_existing_conflict_scope_guard():
    deps = dependencies(MockLLM(conflict=True), [[hit(), hit(2)]],
                        {"doc_1": document(direction="OUTBOUND"), "doc_2": document(2, direction="OUTBOUND")})
    raw = {"travel_direction": "입국"}
    result = await run(deps, agent_state=AgentState(conditions=raw))
    assert result.response.status == ChatStatus.ANSWERED and result.response.conflict is None
    assert result.state.conditions == raw


@pytest.mark.asyncio
async def test_incoming_canonical_and_saved_legacy_conflict_is_not_overwritten():
    state = AgentState(conditions={"travel_direction": "출국"})
    result = await run(dependencies(), agent_state=state, conditions={"direction": "INBOUND"})
    assert result.response.status == ChatStatus.INSUFFICIENT_EVIDENCE
    assert result.state.conditions == {"travel_direction": "출국", "direction": "INBOUND"}
    assert state.conditions == {"travel_direction": "출국"}


@pytest.mark.asyncio
async def test_third_country_in_question_keeps_scope_for_normal_unsupported_evidence_result():
    base = MockLLM()
    seen = []
    def generate(messages):
        data = json.loads(messages[-1]["content"])
        seen.append((data, messages[0]["content"]))
        answer = json.loads(base(messages))
        if data["task"] == "check_evidence":
            answer["verdict"] = "UNSUPPORTED"
        return json.dumps(answer)
    question = "日本入境的香水政策是什么？"
    result = await agent_answer(question, "third-country", **dependencies(generate))
    assert result.response.status == ChatStatus.INSUFFICIENT_EVIDENCE
    assert result.state.question == question and result.state.conditions == {}
    assert result.state.retrieval_count == 1 and result.state.clarification_count == 0
    assert "do not reinterpret it as CN/KR" in seen[0][1]
    assert all(data["question"] == question for data, _ in seen)


@pytest.mark.asyncio
async def test_native_schema_profile_runs_existing_graph_without_changing_budgets(monkeypatch):
    from app.qa.agent import generation
    llm = MockLLM(sufficient_at=2)
    requests = []
    def transport(messages, **kwargs):
        requests.append(kwargs["response_format"]["json_schema"])
        return llm(messages)
    monkeypatch.setattr(generation, "generate_text", transport)
    fn = generation.agent_generator({"AGENT_GENERATION_JSON_SCHEMA_ENABLED": "true"}, llm)
    deps = dependencies(fn, [[hit()], [hit(), hit(2)]], {"doc_1": document(), "doc_2": document(2)})
    result = await run(deps)
    assert result.response.status == ChatStatus.ANSWERED
    assert result.state.retrieval_count == 2 and result.state.clarification_count == 0
    assert "rewrite_query" in result.trace
    assert result.response.citations[0].quote == hit().text
    assert {item["name"] for item in requests} == {"check_conditions", "check_evidence",
        "check_applicability", "rewrite_query", "check_conflict", "generate_answer"}
    assert all(item["strict"] for item in requests)


@pytest.mark.asyncio
async def test_native_schema_does_not_bypass_stable_id_guard(monkeypatch):
    from app.qa.agent import generation
    llm = MockLLM()
    def transport(messages, **kwargs):
        result = json.loads(llm(messages))
        if json.loads(messages[-1]["content"])["task"] == "check_evidence":
            result["core_support"] = [{"chunk_id": "E1"}]
        return json.dumps(result)
    monkeypatch.setattr(generation, "generate_text", transport)
    fn = generation.agent_generator({"AGENT_GENERATION_JSON_SCHEMA_ENABLED": "true"}, llm)
    with pytest.raises(AgentError) as caught:
        await run(dependencies(fn))
    assert caught.value.failed_node == "check_evidence"
    assert caught.value.error_type == AgentErrorType.AGENT_WORKFLOW_ERROR


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
    assert set(result.model_dump()) == {"response", "state", "trace", "model_name"}
    assert result.model_name is None  # This injected Fake supplies no provider metadata.
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
    assert deps["get_documents_fn"].await_count == 3
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


def exception_chain(error):
    chain = []
    while error is not None:
        chain.append(error)
        error = error.__cause__
    return chain


def test_agent_error_legacy_constructor_and_derived_read_only_eligibility():
    error = AgentError('legacy message')
    assert str(error) == 'legacy message'
    assert error.failed_node is None and error.error_type is AgentErrorType.UNKNOWN_ERROR
    assert not error.fallback_eligible and error.state is None and error.trace is None
    for kind in AgentErrorType:
        classified = AgentError('message', error_type=kind)
        assert classified.fallback_eligible is (kind is AgentErrorType.AGENT_WORKFLOW_ERROR)
        with pytest.raises(AttributeError):
            classified.fallback_eligible = not classified.fallback_eligible
        with pytest.raises(AttributeError):
            classified.error_type = AgentErrorType.AGENT_WORKFLOW_ERROR
    with pytest.raises(TypeError):
        AgentError('message', fallback_eligible=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('query', ['', '   '])
async def test_blank_rewrite_is_workflow_error_not_shared_generation(query):
    deps = dependencies(MockLLM(sufficient_at=99, overrides={'rewrite_query': json.dumps({'query': query})}))
    with pytest.raises(AgentError) as caught:
        await run(deps)
    error = caught.value
    assert error.failed_node == 'rewrite_query'
    assert error.error_type is AgentErrorType.AGENT_WORKFLOW_ERROR and error.fallback_eligible
    assert error.state.retrieval_count == 1
    assert error.trace[-1] == 'rewrite_query'


@pytest.mark.asyncio
@pytest.mark.parametrize('raw', ['not JSON', '{"query": "ok", "extra": true}', '{"query": 123}'])
async def test_successful_generation_invalid_agent_schema_keeps_original_cause(raw):
    llm = MockLLM(sufficient_at=99, overrides={'rewrite_query': raw})
    with pytest.raises(AgentError) as caught:
        await run(dependencies(llm))
    error = caught.value
    assert error.failed_node == 'rewrite_query'
    assert error.error_type is AgentErrorType.AGENT_WORKFLOW_ERROR and error.fallback_eligible
    assert any(isinstance(e, (json.JSONDecodeError, ValidationError)) for e in exception_chain(error))
    assert error.trace[-1] == 'rewrite_query' and error.state.retrieval_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('failure', ['rate_limit', 'connection', 'timeout', 'adapter_response', 'adapter_json'])
async def test_rewrite_shared_generation_failures_never_eligible(asynchronous, failure):
    request = httpx.Request('POST', 'https://synthetic.invalid/v1/chat/completions')
    errors = {
        'rate_limit': httpx.HTTPStatusError('synthetic', request=request,
                                          response=httpx.Response(429, request=request)),
        'connection': httpx.ConnectError('synthetic', request=request),
        'timeout': httpx.ReadTimeout('synthetic', request=request),
        'adapter_response': GenerationError('synthetic invalid HTTP envelope'),
        'adapter_json': json.JSONDecodeError('synthetic envelope', 'invalid', 0),
    }
    original = errors[failure]
    llm = MockLLM(sufficient_at=99)
    failed_calls = []
    def generate(messages):
        if json.loads(messages[-1]['content'])['task'] == 'rewrite_query':
            failed_calls.append(True)
            raise original
        return llm(messages)
    async def async_generate(messages):
        return generate(messages)
    with pytest.raises(AgentError) as caught:
        await run(dependencies(async_generate if asynchronous else generate))
    error = caught.value
    assert error.failed_node == 'rewrite_query'
    assert error.error_type is AgentErrorType.SHARED_GENERATION_ERROR and not error.fallback_eligible
    assert error.__cause__ is original and failed_calls == [True]
    assert error.state.retrieval_count == 1 and error.trace[-1] == 'rewrite_query'


@pytest.mark.asyncio
async def test_shared_generation_error_at_answer_does_not_use_answer_repair():
    llm = MockLLM()
    failed_calls = []
    def generate(messages):
        if json.loads(messages[-1]['content'])['task'] == 'generate_answer':
            failed_calls.append(True)
            raise GenerationError('shared service unavailable')
        return llm(messages)
    with pytest.raises(AgentError) as caught:
        await run(dependencies(generate))
    assert caught.value.failed_node == 'generate_answer'
    assert caught.value.error_type is AgentErrorType.SHARED_GENERATION_ERROR
    assert not caught.value.fallback_eligible and failed_calls == [True]


@pytest.mark.asyncio
async def test_agent_answer_json_failure_still_uses_exactly_one_repair():
    llm = MockLLM(overrides={'generate_answer': 'not JSON'})
    with pytest.raises(AgentError) as caught:
        await run(dependencies(llm))
    assert caught.value.failed_node == 'generate_answer'
    assert caught.value.error_type is AgentErrorType.AGENT_WORKFLOW_ERROR
    assert caught.value.fallback_eligible
    assert sum(c['task'] == 'generate_answer' for c in llm.calls) == 2
    assert any(isinstance(e, json.JSONDecodeError) for e in exception_chain(caught.value))


@pytest.mark.asyncio
@pytest.mark.parametrize('metadata', [False, True])
async def test_shared_retrieval_and_document_dependency_origin(metadata):
    original = ConnectionError('synthetic database connection')
    deps = dependencies()
    deps['get_documents_fn' if metadata else 'retrieve_fn'] = AsyncMock(side_effect=original)
    with pytest.raises(AgentError) as caught:
        await run(deps)
    error = caught.value
    assert error.failed_node == ('check_evidence' if metadata else 'retrieve')
    assert error.error_type is AgentErrorType.SHARED_RETRIEVAL_ERROR and not error.fallback_eligible
    assert error.state.retrieval_count == 1 and error.trace[-1] == error.failed_node
    assert error.__cause__ is original


@pytest.mark.asyncio
async def test_unknown_node_failure_is_conservative_regardless_of_message(monkeypatch):
    original = RuntimeError('shared generation workflow failed')
    async def broken(self, state):
        raise original
    monkeypatch.setattr(Nodes, 'check_applicability', broken)
    with pytest.raises(AgentError) as caught:
        await run(dependencies())
    error = caught.value
    assert error.error_type is AgentErrorType.UNKNOWN_ERROR and not error.fallback_eligible
    assert error.failed_node == 'check_applicability'
    assert error.__cause__ is original and error.trace[-1] == error.failed_node
    assert error.state.retrieval_count == 1 and error.state.evidence


@pytest.mark.asyncio
async def test_pre_node_graph_failure_has_no_failed_node(monkeypatch):
    import app.qa.agent.service as service
    original = RuntimeError('synthetic graph construction failure')
    def fail_build(nodes):
        raise original
    monkeypatch.setattr(service, 'build_graph', fail_build)
    with pytest.raises(AgentError) as caught:
        await run(dependencies())
    assert caught.value.failed_node is None and caught.value.trace == []
    assert caught.value.state.retrieval_count == 0
    assert caught.value.error_type is AgentErrorType.UNKNOWN_ERROR and not caught.value.fallback_eligible
    assert caught.value.__cause__ is original


@pytest.mark.asyncio
async def test_post_graph_failure_not_attributed_to_last_successful_node(monkeypatch):
    import app.qa.agent.service as service
    original = RuntimeError('synthetic response assembly failure')
    def fail_response(**kwargs):
        raise original
    monkeypatch.setattr(service, 'AgentChatResponse', fail_response)
    with pytest.raises(AgentError) as caught:
        await run(dependencies())
    assert caught.value.failed_node is None and caught.value.trace[-1] == 'finalize'
    assert caught.value.error_type is AgentErrorType.UNKNOWN_ERROR and not caught.value.fallback_eligible
    assert caught.value.__cause__ is original and caught.value.state.retrieval_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('typed', [False, True])
async def test_completion_failure_policy_unchanged_with_conservative_classification(typed):
    original = RetrievalError('synthetic source read failure') if typed else RuntimeError('untyped completion failure')
    completion = AsyncMock(side_effect=original)
    with pytest.raises(AgentError) as caught:
        await run(dependencies(), complete_evidence_fn=completion)
    assert caught.value.failed_node == 'retrieve'
    assert caught.value.error_type is (AgentErrorType.SHARED_RETRIEVAL_ERROR if typed else AgentErrorType.UNKNOWN_ERROR)
    assert not caught.value.fallback_eligible and caught.value.state.retrieval_count == 1
    assert caught.value.__cause__ is original and completion.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('dependency', ['retrieve_fn', 'get_documents_fn', 'generate_fn'])
async def test_dependency_cancellation_not_wrapped(dependency):
    deps = dependencies()
    deps[dependency] = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await run(deps)


@pytest.mark.asyncio
async def test_langgraph_node_cancelled_error_keeps_existing_cancel_semantics():
    from langgraph.errors import NodeCancelledError
    deps = dependencies()
    deps['generate_fn'] = AsyncMock(side_effect=NodeCancelledError('synthetic cancellation'))
    with pytest.raises(asyncio.CancelledError):
        await run(deps)


@pytest.mark.asyncio
async def test_sync_generator_returning_coroutine_is_awaited():
    llm = MockLLM()

    async def generate(messages):
        return llm(messages)

    result = await run(dependencies(lambda messages: generate(messages)))
    assert result.response.status == ChatStatus.ANSWERED
    assert result.trace == NORMAL


@pytest.mark.asyncio
async def test_marked_async_generator_with_nonawaitable_result_is_shared_failure():
    import inspect

    @inspect.markcoroutinefunction
    def generate(messages):
        return "{}"

    with pytest.raises(AgentError) as caught:
        await run(dependencies(generate))
    assert caught.value.error_type == AgentErrorType.SHARED_GENERATION_ERROR
    assert isinstance(caught.value.__cause__, TypeError)
    assert not caught.value.fallback_eligible


@pytest.mark.asyncio
async def test_nontext_decision_is_workflow_failure():
    with pytest.raises(AgentError) as caught:
        await run(dependencies(lambda messages: {"missing_conditions": []}))
    assert caught.value.error_type == AgentErrorType.AGENT_WORKFLOW_ERROR
    assert caught.value.failed_node == "check_conditions"
    assert caught.value.state.retrieval_count == 0


@pytest.mark.asyncio
async def test_typed_generation_error_from_node_keeps_shared_origin(monkeypatch):
    async def fail(self, state):
        raise GenerationError("测试生成服务故障")

    monkeypatch.setattr(Nodes, "check_applicability", fail)
    with pytest.raises(AgentError) as caught:
        await run(dependencies())
    assert caught.value.error_type == AgentErrorType.SHARED_GENERATION_ERROR
    assert not caught.value.fallback_eligible


@pytest.mark.asyncio
async def test_finalize_rejects_missing_business_status():
    deps = dependencies()
    nodes = Nodes(Dependencies(deps["retrieve_fn"], deps["get_documents_fn"], deps["generate_fn"]))
    with pytest.raises(AgentError) as caught:
        await nodes.finalize({"status": None})
    assert caught.value.error_type == AgentErrorType.AGENT_WORKFLOW_ERROR


@pytest.mark.asyncio
async def test_post_graph_unknown_conflict_source_is_workflow_failure(monkeypatch):
    import app.qa.agent.service as service
    from app.qa.agent.state import Conflict

    original = service.build_graph

    def corrupt_graph(nodes):
        graph = original(nodes)

        class CorruptGraph:
            async def ainvoke(self, initial, config):
                final = await graph.ainvoke(initial, config=config)
                return {**final, "status": ChatStatus.CONFLICTING_EVIDENCE,
                        "conflict": Conflict(reason="测试失效来源", side_a=["E1"], side_b=["E99"])}

        return CorruptGraph()

    monkeypatch.setattr(service, "build_graph", corrupt_graph)
    with pytest.raises(AgentError, match="unknown evidence") as caught:
        await run(dependencies())
    assert caught.value.error_type == AgentErrorType.AGENT_WORKFLOW_ERROR
    assert caught.value.failed_node is None


@pytest.mark.asyncio
async def test_typed_generation_failure_in_completion_is_not_classified_by_node_name():
    original = GenerationError('synthetic shared generation failure')
    completion = AsyncMock(side_effect=original)
    deps = dependencies()
    with pytest.raises(AgentError) as caught:
        await run(deps, complete_evidence_fn=completion)
    error = caught.value
    assert error.failed_node == 'retrieve'
    assert error.error_type is AgentErrorType.SHARED_GENERATION_ERROR
    assert not error.fallback_eligible and error.__cause__ is original
    assert error.state.retrieval_count == 1 and error.trace[-1] == 'retrieve'
    deps['retrieve_fn'].assert_awaited_once()
    completion.assert_awaited_once()


@pytest.mark.asyncio
async def test_declared_async_generator_must_actually_return_an_awaitable():
    calls = []

    @inspect.markcoroutinefunction
    def malformed_generator(messages):
        calls.append(json.loads(messages[-1]['content'])['task'])
        return '{"missing_conditions": []}'

    deps = dependencies(malformed_generator)
    with pytest.raises(AgentError) as caught:
        await run(deps)
    error = caught.value
    assert calls == ['check_conditions']
    assert error.failed_node == 'check_conditions'
    assert error.error_type is AgentErrorType.SHARED_GENERATION_ERROR
    assert not error.fallback_eligible and isinstance(error.__cause__, TypeError)
    assert error.state.retrieval_count == 0 and error.trace == ['check_conditions']
    deps['retrieve_fn'].assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_generator_factory_returning_awaitable_executes_the_real_graph():
    llm = MockLLM()
    awaited = []

    async def generate(messages):
        await asyncio.sleep(0)
        awaited.append(json.loads(messages[-1]['content'])['task'])
        return llm(messages)

    def factory(messages):
        return generate(messages)

    deps = dependencies(factory)
    result = await run(deps)
    assert result.trace == NORMAL and result.response.status == ChatStatus.ANSWERED
    assert awaited == ['check_conditions', 'check_evidence', 'check_applicability',
                       'check_conflict', 'generate_answer']
    assert result.state.retrieval_count == 1
    assert result.response.citations[0].quote == hit().text
    deps['retrieve_fn'].assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('raw', [None, {}, b'{}'])
async def test_successful_nontext_answer_is_workflow_failure_without_extra_service_call(raw):
    llm = MockLLM()
    calls = []

    def generate(messages):
        if json.loads(messages[-1]['content'])['task'] == 'generate_answer':
            calls.append(True)
            return raw
        return llm(messages)

    with pytest.raises(AgentError, match='must return JSON text') as caught:
        await run(dependencies(generate))
    error = caught.value
    assert error.failed_node == 'generate_answer'
    assert error.error_type is AgentErrorType.AGENT_WORKFLOW_ERROR and error.fallback_eligible
    assert calls == [True]  # This is not a JSON parsing error or a network retry.
    assert error.state.retrieval_count == 1 and error.state.evidence_ids == ['E1']
    assert error.trace[-1] == 'generate_answer'


@pytest.mark.asyncio
async def test_finalizer_rejects_missing_business_status_as_workflow_error():
    deps = dependencies()
    nodes = Nodes(Dependencies(deps['retrieve_fn'], deps['get_documents_fn'], deps['generate_fn']))
    with pytest.raises(AgentError, match='without a business status') as caught:
        await nodes.tracked('finalize')({'status': None, 'trace': []})
    assert caught.value.failed_node == 'finalize'
    assert caught.value.error_type is AgentErrorType.AGENT_WORKFLOW_ERROR
    assert caught.value.fallback_eligible
    assert nodes.latest['trace'] == ['finalize'] and nodes.latest['status'] is None
    deps['retrieve_fn'].assert_not_awaited()


@pytest.mark.asyncio
async def test_service_rejects_unknown_conflict_id_if_graph_result_contract_is_broken(monkeypatch):
    import app.qa.agent.service as service
    original_build = service.build_graph

    def build_with_corrupt_result(nodes):
        graph = original_build(nodes)

        class CorruptResult:
            async def ainvoke(self, *args, **kwargs):
                final = await graph.ainvoke(*args, **kwargs)
                # Fault injection at the service boundary, after all real graph nodes ran.
                return {**final, 'conflict': final['conflict'].model_copy(update={'side_b': ['E999']})}

        return CorruptResult()

    monkeypatch.setattr(service, 'build_graph', build_with_corrupt_result)
    deps = dependencies(MockLLM(conflict=True), [[hit(), hit(2)]],
                        {'doc_1': document(), 'doc_2': document(2)})
    with pytest.raises(AgentError, match='Conflict references unknown evidence') as caught:
        await run(deps)
    error = caught.value
    assert error.error_type is AgentErrorType.AGENT_WORKFLOW_ERROR and error.fallback_eligible
    assert error.failed_node is None  # The graph completed; this is response assembly.
    assert error.trace == NORMAL[:5] + ['finalize']
    assert error.state.retrieval_count == 1 and error.state.evidence_ids == ['E1', 'E2']
    deps['retrieve_fn'].assert_awaited_once()


@pytest.mark.parametrize("question,country,direction", [
    ("韩国入境香水规定是什么？", "KR", "INBOUND"),
    ("入境韩国时香水有什么规定？", "KR", "INBOUND"),
    ("中国出境通关规定是什么？", "CN", "OUTBOUND"),
    ("出境中国时有什么规定？", "CN", "OUTBOUND"),
    ("从韩国入境中国，有什么规定？", "CN", "INBOUND"),
    ("从 韩国 入境 中国，如何通关？", "CN", "INBOUND"),
    ("从韩国携带香烟入境中国，一般有什么通关规定？", "CN", "INBOUND"),
    ("中国到韩国，咨询中国出境政策。", "CN", "OUTBOUND"),
    ("中国到韩国，咨询韩国入境政策。", "KR", "INBOUND"),
    ("韩国入境，韩国海关怎样规定？", "KR", "INBOUND"),
    ("한국에 입국할 때 향수 규정은 무엇인가요?", "KR", "INBOUND"),
    ("한국으로 입국하는 경우 규정은?", "KR", "INBOUND"),
    ("한국에 입국할 때 한국 세관 규정은?", "KR", "INBOUND"),
    ("중국에서 출국할 때 규정은 무엇인가요?", "CN", "OUTBOUND"),
    ("한국에서 출국하는 규정은?", "KR", "OUTBOUND"),
    ("한국에서 중국에 입국할 때 규정은?", "CN", "INBOUND"),
    ("중국에서 한국으로 가는데 중국 출국 규정은?", "CN", "OUTBOUND"),
    ("중국에서 한국으로 가는데 한국 입국 규정은?", "KR", "INBOUND"),
    ("적용 조건: 한국 입국; 일반 여행자; 별도면세 대상 제외.\n기본면세범위는 얼마인가요?", "KR", "INBOUND"),
])
def test_explicit_bilingual_scope_relations(question, country, direction):
    from app.qa.agent.conditions import explicit_question_scope
    assert explicit_question_scope(question) == {"destination_country": country, "direction": direction}


@pytest.mark.parametrize("question", [
    "韩国旅客有什么免税规定？", "中国居民携带香水有什么规定？", "中国到韩国，有什么规定？",
    "从韩国出发，香烟免税多少？", "从韩国入境，香水免税多少？", "韩国买的香水有什么规定？",
    "韩国入境还是出境的规定？", "韩国或中国入境有什么区别？", "可能入境韩国，规定是什么？",
    "不入境韩国，香水有什么规定？", "不是韩国入境的规定。", "我不准备入境韩国。",
    "韩国入境和中国入境规定有什么区别？", "中国出境和韩国入境规定有什么区别？",
    "从中国出境去日本，日本海关如何规定？", "从中国出境去韩国，韩国海关有什么规定？",
    "去韩国出境有什么规定？", "向中国出境有什么规定？", "入境中国香港有什么规定？",
    "自韩国入境，如何申报？", "经韩国入境，如何申报？",
    "中国出境政策与日本政策。", "韩国入境规定与日本海关相比如何？",
    "中国出境，韩国入境。", "韩国入境，中国入境。",
    "中国出境时，韩国海关有什么规定？", "중국에서 출국할 때 한국 세관 규정은?",
    "한국에서 입국합니다. 규정은?", "한국으로 출국합니다. 규정은?",
    "한국에 입국하지 않습니다.", "한국 입국 아니면 중국 출국 규정은?",
    "한국 출국 예정입니다.", "북한국 입국 규정은?", "중국인 입국 규정은?",
    "중국에서 출국해 일본으로 가는데 일본 세관 규정은?", '문서의 "한국 입국"이라는 표현은?',
    "중국 출국 규정과 일본 세관 정책은?",
])
def test_scope_inference_abstains_on_ambiguity_origin_negation_and_multiple_policies(question):
    from app.qa.agent.conditions import explicit_question_scope
    assert explicit_question_scope(question) == {}


@pytest.mark.parametrize("question", ["韩国入境香水规定是什么？", "중국에서 출국할 때 규정은 무엇인가요?"])
@pytest.mark.asyncio
async def test_question_only_facts_filter_repeated_core_questions_without_state_mutation(question):
    from app.qa.agent.conditions import explicit_question_scope
    known = explicit_question_scope(question)
    repeat = [condition_question("destination_country", options=["CN", "KR"]),
              condition_question("direction", options=["INBOUND", "OUTBOUND"])]
    llm = MockLLM(answer=("테스트 규칙에 따라 신고합니다.[E1]" if "국" in question else "测试资料规定物品需要申报。[E1]"),
                  overrides={"check_conditions": json.dumps({"missing_conditions": repeat}),
                             "check_applicability": json.dumps({"missing_conditions": repeat})})
    seen = []
    def generate(messages):
        data = json.loads(messages[-1]["content"])
        if data["task"] in ("check_conditions", "check_applicability"):
            seen.append(data)
            assert "Empty conditions does NOT mean" in messages[0]["content"]
        return llm(messages)
    deps = dependencies(generate, docs={"doc_1": document(country=known["destination_country"], direction=known["direction"])})
    result = await agent_answer(question, "explicit-facts", **deps)
    assert result.response.status == ChatStatus.ANSWERED and result.trace == NORMAL
    assert result.state.conditions == {} and result.state.clarification_count == 0
    assert result.state.retrieval_count == 1
    assert all(row["conditions"] == {} and row["confirmed_conditions"] == known for row in seen)


@pytest.mark.parametrize("raw", [{"destination_country": "CN"}, {"direction": "OUTBOUND"}, {"travel_direction": "출국"}])
@pytest.mark.asyncio
async def test_question_and_confirmed_history_conflict_refuses_without_overwrite(raw):
    question = "韩国入境香水有什么规定？"
    state = AgentState(conditions=raw)
    deps = dependencies()
    result = await agent_answer(question, "conflicting-facts", agent_state=state, **deps)
    assert result.response.status == ChatStatus.INSUFFICIENT_EVIDENCE
    assert result.state.conditions == state.conditions == raw
    assert result.state.retrieval_count == result.state.clarification_count == 0
    assert not deps["generate_fn"].calls
    deps["retrieve_fn"].assert_not_awaited()


@pytest.mark.asyncio
async def test_ambiguous_question_keeps_existing_clarification_ability():
    llm = MockLLM(missing_pre=True)
    result = await agent_answer("韩国旅客携带香水有什么规定？", "ambiguous-facts", **dependencies(llm))
    assert result.response.status == ChatStatus.NEEDS_CLARIFICATION
    assert result.state.conditions == {} and result.state.retrieval_count == 0
    assert llm.calls[0]["confirmed_conditions"] == {}


@pytest.mark.asyncio
async def test_explicit_question_facts_survive_restore_without_mutating_history_or_resetting_budget():
    question = "韩国入境香水有什么规定？"
    state = AgentState(question=question, session_id="restore-facts", question_id="q_facts",
        conditions={"item_name": "香水"}, clarification_count=1, retrieval_count=2, retrieval_queries=["old1", "old2"])
    llm = MockLLM(overrides={"check_conditions": json.dumps({"missing_conditions": [
        condition_question("direction", options=["INBOUND", "OUTBOUND"])]})})
    result = await agent_answer(question, "restore-facts", question_id="q_facts",
        agent_state=json.loads(state.model_dump_json()), **dependencies(llm, docs={"doc_1": document(country="KR")}))
    assert result.response.status == ChatStatus.ANSWERED
    assert result.state.conditions == state.conditions == {"item_name": "香水"}
    assert result.state.retrieval_count == 3 and result.state.clarification_count == 1
    assert result.state.question_id == "q_facts"


A06_QUESTION = "我是成年普通旅客，携带自用香水入境，香水的单独免税数量上限是多少？"


@pytest.mark.parametrize("question,direction", [
    (A06_QUESTION, "INBOUND"),
    ("携带测试物品入境，应如何申报？", "INBOUND"),
    ("带着自用物品出境，有什么通关规定？", "OUTBOUND"),
    ("出境时携带香水的免税规定是什么？", "OUTBOUND"),
    ("我是韩国人，携带香水入境有什么免税规定？", "INBOUND"),
    ("我是中国居民，携带物品出境应如何申报？", "OUTBOUND"),
    ("향수를 휴대하여 입국할 때 면세 한도는?", "INBOUND"),
    ("향수를 휴대하고 출국할 때 통관 규정은?", "OUTBOUND"),
    ("물품을 가지고 입국할 때 신고 규정은?", "INBOUND"),
    ("물품을 휴대한 채 출국할 때 규정은?", "OUTBOUND"),
])
def test_independent_direction_is_partial_and_never_guesses_country(question, direction):
    from app.qa.agent.conditions import explicit_question_scope, confirmed_conditions
    assert explicit_question_scope(question) == {"direction": direction}
    raw = {"is_professional_crew": "否"}
    assert confirmed_conditions(question, raw) == {**raw, "direction": direction}
    assert raw == {"is_professional_crew": "否"}


@pytest.mark.parametrize("question", [
    "我是韩国人，免税多少？", "中国到韩国，香水限额多少？",
    "我携带香水旅行，免税多少？", "携带香水入境了，天气怎么样？",
    "不要携带测试物品入境，通关规定是什么？", "无需携带物品出境，规则是什么？",
    "未入境，携带物品的规定是什么？", "可能携带香水入境，免税多少？",
    "携带香水入境还是出境，规定是什么？",
    "携带香水入境，韩国入境，中国入境。",  # multiple full pairs must not fall back
    "携带香水出境去韩国，韩国海关规定是什么？",
    "去韩国携带物品出境，免税多少？", "携带香水到日本出境，免税多少？",
    "携带香水入境，中国海关、日本海关有何政策？",
    "携带香水在出境免税店购买物品，免税吗？",
    "향수를 휴대하고 입국하지 않습니다. 면세 규정은?",
    "물품을 휴대하고 입국 안 합니다. 신고 규정은?",
    "물품을 가지고 안 입국합니다. 면세 규정은?",
    "물품을 휴대하고 출국할 생각은 없습니다. 규정은?",
    "한국으로 향수를 휴대하고 출국할 때 규정은?",
    "향수를 휴대하고 출국해 한국으로 가는데 세관 규정은?",
    "향수를 휴대하고 입국장 면세점에 갑니다. 규정은?",
])
def test_independent_direction_abstains_on_negation_routes_and_non_action_terms(question):
    from app.qa.agent.conditions import explicit_question_scope
    assert explicit_question_scope(question) == {}


@pytest.mark.asyncio
async def test_a06_exact_model_repeated_direction_is_filtered_but_country_remains_missing():
    repeat = [condition_question("destination_country", options=["CN", "KR"]),
              condition_question("direction", options=["INBOUND", "OUTBOUND"])]
    llm = MockLLM(overrides={"check_conditions": json.dumps({"missing_conditions": repeat})})
    deps = dependencies(llm)
    result = await agent_answer(A06_QUESTION, "a06-partial", **deps)
    assert result.response.status == ChatStatus.NEEDS_CLARIFICATION
    assert [q.field for q in result.response.clarification.questions] == ["destination_country"]
    assert llm.calls[0]["conditions"] == {} and llm.calls[0]["confirmed_conditions"] == {"direction": "INBOUND"}
    assert result.state.conditions == {} and result.state.clarification_count == 1 and result.state.retrieval_count == 0
    deps["retrieve_fn"].assert_not_awaited()


@pytest.mark.parametrize("raw", [{"direction": "OUTBOUND"}, {"travel_direction": "출국"}])
@pytest.mark.asyncio
async def test_partial_direction_conflicts_refuse_without_overwriting_history(raw):
    state = AgentState(conditions=raw)
    deps = dependencies()
    result = await agent_answer(A06_QUESTION, "partial-conflict", agent_state=state, **deps)
    assert result.response.status == ChatStatus.INSUFFICIENT_EVIDENCE
    assert result.state.conditions == state.conditions == raw
    assert result.state.retrieval_count == result.state.clarification_count == 0 and not deps["generate_fn"].calls


@pytest.mark.asyncio
async def test_a06_a07_partial_direction_serialized_state_restore_preserves_budget_and_raw_values():
    repeat = [condition_question("destination_country", options=["CN", "KR"]),
              condition_question("direction", options=["INBOUND", "OUTBOUND"])]
    llm = MockLLM(overrides={"check_conditions": json.dumps({"missing_conditions": repeat}),
                             "check_applicability": json.dumps({"missing_conditions": repeat})})
    first_deps = dependencies(llm)
    first = await agent_answer(A06_QUESTION, "partial-restore", **first_deps)
    assert [q.field for q in first.response.clarification.questions] == ["destination_country"]
    saved = json.loads(first.state.model_dump_json())
    second_deps = dependencies(llm, docs={"doc_1": document(country="KR")})
    second = await agent_answer(A06_QUESTION, "partial-restore", question_id=first.response.question_id,
        conditions={"destination_country": "KR"}, agent_state=saved, **second_deps)
    assert second.response.status == ChatStatus.ANSWERED
    assert second.state.question_id == first.state.question_id and second.state.question == A06_QUESTION
    assert second.state.conditions == {"destination_country": "KR"} and saved["conditions"] == {}
    assert second.state.clarification_count == 1 and second.state.retrieval_count == 1
    assert all(c["confirmed_conditions"]["direction"] == "INBOUND" for c in llm.calls if c["task"] in ("check_conditions", "check_applicability"))


@pytest.mark.parametrize("used", [2, 3])
@pytest.mark.asyncio
async def test_partial_direction_legacy_restore_respects_remaining_retrieval_budget(used):
    raw = {"destination_country": "KR", "travel_direction": "입국"}
    state = AgentState(question=A06_QUESTION, session_id="remaining-budget", question_id="q_remaining",
        conditions=raw, retrieval_count=used, clarification_count=1,
        retrieval_queries=[f"old{i}" for i in range(used)])
    repeat = [condition_question("destination_country", options=["CN", "KR"]),
              condition_question("direction", options=["INBOUND", "OUTBOUND"])]
    llm = MockLLM(overrides={"check_conditions": json.dumps({"missing_conditions": repeat})})
    deps = dependencies(llm, docs={"doc_1": document(country="KR")})
    result = await agent_answer(A06_QUESTION, "remaining-budget", question_id="q_remaining",
        agent_state=json.loads(state.model_dump_json()), **deps)
    assert result.response.status == (ChatStatus.ANSWERED if used == 2 else ChatStatus.INSUFFICIENT_EVIDENCE)
    assert deps["retrieve_fn"].await_count == (1 if used == 2 else 0)
    assert result.state.retrieval_count == 3 and result.state.clarification_count == 1
    assert result.state.conditions == state.conditions == raw


def synthetic_citation_sources():
    """Synthetic clauses for citation plumbing, not real policy gold."""
    return [hit(i, text) for i, text in enumerate([
        "合成测试：适用于普通旅客。", "合成测试：物品为自用。",
        "合成测试：一般金额额度为800美元。", "合成测试：该物品限量为100毫升。",
        "合成测试：该物品不适用一般金额额度。"], 1)]


@pytest.mark.parametrize("answer", [
    "该物品限量100毫升[E4]。该物品不适用一般金额额度[E5]。",
    "一般金额额度800美元，物品限量100毫升[E3][E4]。",
    "一般金额额度800美元[E3]。物品限量100毫升[E4]。",
])
@pytest.mark.asyncio
async def test_each_numeric_claim_can_use_its_own_trusted_clause_without_repair(answer):
    llm = MockLLM(answer=answer)
    seen_prompt = []
    def generate(messages):
        if json.loads(messages[-1]["content"])["task"] == "generate_answer":
            seen_prompt.append(messages[0]["content"])
        return llm(messages)
    deps = dependencies(generate, [synthetic_citation_sources()], {f"doc_{i}": document(i) for i in range(1, 6)})
    result = await run(deps)
    assert result.response.status == ChatStatus.ANSWERED
    assert len(seen_prompt) == 1
    assert "Every policy claim" in seen_prompt[0] and "not just a number" in seen_prompt[0]
    assert "Do not introduce policy amounts" in seen_prompt[0]
    assert [e.evidence_id for e in result.state.evidence] == [f"E{i}" for i in range(1, 6)]
    assert result.state.retrieval_count == 1


@pytest.mark.parametrize("bad,good,offending,reason", [
    ("物品限量100毫升[E4]。不适用一般800美元额度[E5]。",
     "物品限量100毫升[E4]。不适用一般金额额度[E5]。", "800", "numeric value"),
    ("一般额度800美元，物品限量100毫升[E4]。",
     "一般额度800美元，物品限量100毫升[E3][E4]。", "800", "numeric value"),
    ("物品限量100毫升。该物品不适用一般金额额度[E5]。",
     "物品限量100毫升[E4]。该物品不适用一般金额额度[E5]。", "100", "no valid citation"),
    ("物品限量100毫升[E999]。",
     "物品限量100毫升[E4]。", "100", "no valid citation"),
])
@pytest.mark.asyncio
async def test_answer_repair_identifies_the_failed_sentence_without_auto_adding_citations(bad, good, offending, reason):
    llm = MockLLM()
    attempts = []
    def generate(messages):
        data = json.loads(messages[-1]["content"])
        if data["task"] == "generate_answer":
            attempts.append(data)
            llm.answer = bad if len(attempts) == 1 else good
        return llm(messages)
    deps = dependencies(generate, [synthetic_citation_sources()], {f"doc_{i}": document(i) for i in range(1, 6)})
    result = await run(deps)
    assert result.response.status == ChatStatus.ANSWERED
    assert len(attempts) == 2 and "validation_feedback" not in attempts[0]
    feedback = attempts[1]["validation_feedback"]
    assert feedback["previous_answer"] == bad
    assert any(offending in d["sentence"] and reason in d["reason"] for d in feedback["citation_feedback"])
    assert attempts[0]["evidence"] == attempts[1]["evidence"]
    assert result.response.answer == good  # The fake model, not programmatic replacement, supplied it.
    assert result.state.retrieval_count == 1 and result.state.clarification_count == 0


@pytest.mark.asyncio
async def test_citation_mismatch_after_one_repair_remains_insufficient_not_silently_fixed():
    llm = MockLLM(answer="物品限量100毫升，一般金额额度800美元[E5]。")
    deps = dependencies(llm, [synthetic_citation_sources()], {f"doc_{i}": document(i) for i in range(1, 6)})
    result = await run(deps)
    assert result.response.status == ChatStatus.INSUFFICIENT_EVIDENCE
    assert result.response.answer is None and result.response.citations == []
    assert sum(c["task"] == "generate_answer" for c in llm.calls) == 2
    assert result.state.retrieval_count == 1


COMPARISON_QUESTION = "中国和韩国的普通旅客免税政策有什么区别？"
COMPARISON_GOOD = "中国合成规则的额度为5000元[E1]。韩国合成规则的额度为800美元[E2]。"


@pytest.mark.parametrize("question,expected", [
    ("对比中国的政策和韩国的政策：普通旅客入境时一般免税额度有何不同？", {"direction":"INBOUND"}),
    ("比较中韩政策，出境时通关规定有什么区别？", {"direction":"OUTBOUND"}),
    ("중국과 한국 정책 비교: 입국 시 면세 한도는?", {"direction":"INBOUND"}),
    ("比较中国和韩国政策，入境或出境时免税额度？", {}),
    ("比较中国和韩国政策，不是入境时免税规定。", {}),
    ("比较中国和韩国政策，出境后再入境时免税额度？", {}),
])
def test_comparison_shared_direction_is_independent_and_restore_safe(question, expected):
    from app.qa.agent.conditions import explicit_question_scope, confirmed_conditions, ConditionConflict
    assert explicit_question_scope(question) == expected
    raw = {"destination_country":"CN"}
    assert confirmed_conditions(question, raw) == {**raw, **expected}
    assert raw == {"destination_country":"CN"}
    if expected:
        opposite = "OUTBOUND" if expected["direction"] == "INBOUND" else "INBOUND"
        with pytest.raises(ConditionConflict):
            confirmed_conditions(question, {"direction":opposite})


@pytest.mark.parametrize("preserve", [False, True])
@pytest.mark.asyncio
async def test_restored_scope_excludes_foreign_and_wrong_direction_but_preserves_ids(preserve):
    question = "携带物品通关有什么规定？"
    sources = [hit(1), hit(2), hit(3)]
    saved = AgentState(question=question, session_id="scope", question_id="scope-q", retrieval_count=1,
                       clarification_count=1, evidence_ids=["E1", "E2", "E3"],
                       evidence=[CachedEvidence(evidence_id=f"E{i}", hit=h) for i, h in enumerate(sources, 1)])
    llm = MockLLM(answer="测试资料规定物品需要申报。[E2]")
    deps = dependencies(llm, batches=[], docs={"doc_1": document(1, country="KR"),
        "doc_2": document(2), "doc_3": document(3, direction="OUTBOUND")})
    result = await agent_answer(question, "scope", question_id="scope-q", agent_state=saved,
        conditions={"destination_country": "CN", "direction": "INBOUND"}, preserve_evidence_ids=preserve, **deps)
    audit = next(c for c in llm.calls if c["task"] == "check_evidence")
    assert [(e["evidence_id"], e["source"]["chunk_id"]) for e in audit["evidence"]] == [("E2", "chunk_2")]
    assert result.response.status == ChatStatus.ANSWERED
    assert [c.evidence_id for c in result.response.citations] == ["E2"]
    assert result.state.retrieval_count == 1 and result.state.clarification_count == 1
    deps["retrieve_fn"].assert_not_awaited()


@pytest.mark.parametrize("found", [True, False])
@pytest.mark.asyncio
async def test_missing_comparison_side_rewrites_with_remaining_budget(found):
    cn = hit(1, "合成中国规则：普通旅客额度5000元。")
    kr = hit(2, "合成韩国规则：普通旅客额度800美元。")
    llm = MockLLM(answer=COMPARISON_GOOD)
    calls = []
    def generate(messages):
        data = json.loads(messages[-1]["content"])
        calls.append(data)
        result = json.loads(llm(messages))
        if data["task"] == "check_evidence":
            support = [{"chunk_id": e["source"]["chunk_id"]} for e in data["evidence"]]
            result["complete_answer"]["support"] = support
            result["applicability"]["support"] = support
        return json.dumps(result, ensure_ascii=False)
    deps = dependencies(generate, batches=[[cn], [kr]] if found else [[cn], [cn], [cn]],
        docs={"doc_1": document(1), "doc_2": document(2, country="KR")})
    result = await agent_answer(COMPARISON_QUESTION, "retry-comparison", **deps)
    assert result.state.retrieval_count == (2 if found else 3)
    assert result.trace.count("rewrite_query") == (1 if found else 2)
    rewrite = next(c for c in calls if c["task"] == "rewrite_query")
    assert "KR" in " ".join(rewrite["previous_evidence_check"]["missing_evidence"])
    assert result.response.status == (ChatStatus.ANSWERED if found else ChatStatus.INSUFFICIENT_EVIDENCE)
    if not found:
        assert not any(c["task"] == "generate_answer" for c in calls)


def comparison_test(answers, *, complete_both=True, metadata=None, applicable_both=True):
    """Synthetic audit/support and model candidates; not real policy gold."""
    sources = [hit(1, "合成中国规则：普通旅客额度5000元。"), hit(2, "合成韩国规则：普通旅客额度800美元。")]
    docs = {"doc_1": document(1, direction="BOTH"), "doc_2": document(2, country="KR", direction="BOTH")}
    for key, changes in (metadata or {}).items():
        docs[key].update(changes)
    llm = MockLLM()
    attempts = []
    def generate(messages):
        data = json.loads(messages[-1]["content"])
        if data["task"] == "generate_answer":
            attempts.append(data)
            assert "Explaining only the destination's rule" in messages[0]["content"]
            assert "separate policy sentences" in messages[0]["content"]
            return json.dumps({"answer": answers[min(len(attempts)-1, len(answers)-1)], "insufficient_evidence": False}, ensure_ascii=False)
        result = json.loads(llm(messages))
        if data["task"] in ("check_evidence", "check_applicability"):
            assert data["comparison_requirements"]["countries"] == ["CN", "KR"]
            assert "TWO distinct applicability questions" in messages[0]["content"]
            assert "not only the source governing the actual trip" in messages[0]["content"]
        if data["task"] == "check_evidence":
            assert data["comparison_requirements"]["countries"] == ["CN", "KR"]
            support = [{"chunk_id": e["source"]["chunk_id"]} for e in data["evidence"]]
            result["complete_answer"]["support"] = support if complete_both else support[:1]
            result["applicability"]["support"] = support if applicable_both else support[:1]
        return json.dumps(result, ensure_ascii=False)
    return dependencies(generate, [sources, sources, sources], docs), attempts


@pytest.mark.parametrize("answer", [
    COMPARISON_GOOD,
    "中国额度5000元[E1]；韩国额度800美元[E2]。",
])
@pytest.mark.asyncio
async def test_comparison_both_supported_rules_pass_without_extra_call(answer):
    deps, calls = comparison_test([answer])
    result = await agent_answer(COMPARISON_QUESTION, "comparison-valid", **deps)
    assert result.response.status == ChatStatus.ANSWERED and result.response.answer == answer
    assert len(calls) == 1
    assert calls[0]["comparison_requirements"]["approved_evidence_ids"] == {"CN":["E1"], "KR":["E2"]}
    assert result.state.retrieval_count == 1 and result.state.clarification_count == 0


@pytest.mark.parametrize("bad", [
    "中国合成规则的额度为5000元[E1]。",  # Exactly the omission class from A09.
    "中国合成规则的额度为5000元[E1][E2]。",  # Decorative foreign citation is not a Korean answer.
    "中国额度800美元[E2]；韩国额度5000元[E1]。",  # Values/IDs exist but attached to wrong country.
    "中国：[E1]。韩国：[E2]。",  # Mere headings are not policy claims.
])
@pytest.mark.asyncio
async def test_comparison_missing_or_misplaced_country_claim_uses_only_existing_repair(bad):
    deps, calls = comparison_test([bad, COMPARISON_GOOD])
    result = await agent_answer(COMPARISON_QUESTION, "comparison-repair", **deps)
    assert result.response.status == ChatStatus.ANSWERED and result.response.answer == COMPARISON_GOOD
    assert len(calls) == 2 and calls[0]["evidence"] == calls[1]["evidence"]
    feedback = calls[1]["validation_feedback"]
    assert "incomplete_country_comparison" in feedback["issues"]
    assert "KR" in feedback["comparison_feedback"][0]["countries"]
    assert feedback["previous_answer"] == bad
    assert result.state.evidence_ids == ["E1", "E2"]
    assert result.state.retrieval_count == 1


@pytest.mark.asyncio
async def test_comparison_still_incomplete_after_one_repair_safely_refuses():
    deps, calls = comparison_test(["中国合成规则的额度为5000元[E1]。"])
    result = await agent_answer(COMPARISON_QUESTION, "comparison-refuse", **deps)
    assert result.response.status == ChatStatus.INSUFFICIENT_EVIDENCE
    assert result.response.answer is None and result.response.citations == []
    assert "证据不足" in result.response.message and len(calls) == 2


@pytest.mark.parametrize("kwargs,conditions", [
    ({"complete_both":False}, {}),  # Korean metadata alone is NOT sufficient legal support.
    ({"applicable_both":False}, {}),
    ({"metadata":{"doc_2":{"status":"PROCESSING"}}}, {}),
    ({"metadata":{"doc_1":{"direction":"OUTBOUND"}}}, {"destination_country":"CN", "direction":"INBOUND"}),
    ({"metadata":{"doc_2":{"applicable_persons":["CREW"]}}}, {"traveler_status":"GENERAL_TRAVELER"}),
])
@pytest.mark.asyncio
async def test_comparison_missing_or_inapplicable_support_never_forces_foreign_citation(kwargs, conditions):
    deps, calls = comparison_test([COMPARISON_GOOD], **kwargs)
    result = await agent_answer(COMPARISON_QUESTION, "comparison-insufficient", conditions=conditions, **deps)
    assert result.response.status == ChatStatus.INSUFFICIENT_EVIDENCE
    assert result.response.answer is None and result.response.citations == [] and calls == []
    assert "证据不足" in result.response.message


@pytest.mark.asyncio
async def test_comparison_korean_answer_keeps_same_ids_and_country_pairing():
    deps, calls = comparison_test(["중국 합성 규정은 5000위안 한도입니다[E1]. 한국 합성 규정은 800달러 한도입니다[E2]."])
    result = await agent_answer("중국과 한국 면세 정책의 차이는?", "comparison-ko", **deps)
    assert result.response.status == ChatStatus.ANSWERED and len(calls) == 1


@pytest.mark.asyncio
async def test_comparison_explanatory_korean_inbound_scope_is_not_chinese_actual_trip():
    """Synthetic dual-scope evidence; actual CN entry does not govern KR explanation."""
    deps, calls = comparison_test([COMPARISON_GOOD], metadata={
        "doc_1": {"direction": "INBOUND"}, "doc_2": {"direction": "INBOUND"}})
    result = await agent_answer(COMPARISON_QUESTION, "comparison-scope", conditions={
        "destination_country": "CN", "direction": "INBOUND", "traveler_status": "GENERAL_TRAVELER"}, **deps)
    assert result.response.status == ChatStatus.ANSWERED
    assert {c.evidence_id for c in result.response.citations} == {"E1", "E2"}
    assert len(calls) == 1 and result.state.retrieval_count == 1


@pytest.mark.parametrize("case", ["missing_complete", "conflicting_history", "non_ready"])
def test_comparison_support_rejects_unverified_or_conflicting_context(case):
    from app.qa.agent.nodes import comparison_support
    from app.qa.citation import Evidence
    state = {"question": COMPARISON_QUESTION, "conditions": {},
             "evidence": [Evidence("E1", hit())], "documents": {"doc_1": document()},
             "evidence_check_result": {"accepted_sufficient": True,
                 "complete_answer": {"status": "supported", "support": [{"chunk_id": "chunk_1"}]},
                 "applicability": {"status": "supported", "support": [{"chunk_id": "chunk_1"}]}}}
    if case == "missing_complete":
        state["evidence_check_result"]["complete_answer"]["status"] = "missing"
    elif case == "conflicting_history":
        state["conditions"] = {"direction": "INBOUND", "travel_direction": "OUTBOUND"}
    else:
        state["documents"]["doc_1"]["status"] = "PROCESSING"
    assert comparison_support(state) == {"CN": set(), "KR": set()}


def test_comparison_claim_cannot_use_a_country_outside_approved_scope():
    from app.qa.agent.nodes import missing_comparison_claims
    from app.qa.citation import Evidence, resolve_citations
    evidence = [Evidence("E1", hit())]
    _, citations = resolve_citations("[E1]", evidence, {"doc_1": document()})
    # A Korean-labelled explanation with a Chinese source cannot supply China’s claim.
    assert missing_comparison_claims("韩国规定需要申报[E1]。", citations, {"CN": {"E1"}}) == ["CN"]

@pytest.mark.asyncio
@pytest.mark.parametrize('outcome',['normal','repair','clarify','refuse','failure','cancel'])
async def test_actual_model_tracks_accepted_answer(monkeypatch,outcome):
    from app.adapters import generation
    llm=MockLLM(missing_pre=outcome=='clarify')
    attempts=0
    def provider(messages,tools=None,**kwargs):
        nonlocal attempts
        data=json.loads(messages[-1]['content']);text=llm(messages);model='decision'
        if data['task']=='generate_answer':
            attempts+=1;model=f'answer-{attempts}'
            if outcome=='failure':raise GenerationError('synthetic failure')
            if outcome=='cancel':raise asyncio.CancelledError()
            if outcome=='repair' and attempts==1:text='{"answer":"没有引用","insufficient_evidence":false}'
            if outcome=='refuse':text='{"answer":"证据不足","insufficient_evidence":true}'
        return generation.GenerationResult(text,[],model)
    monkeypatch.setattr(generation,'generate',provider)
    if outcome in ('failure','cancel'):
        with pytest.raises(AgentError if outcome=='failure' else asyncio.CancelledError):
            await run(dependencies(generation.generate_text))
    else:
        result=await run(dependencies(generation.generate_text))
        assert result.model_name==(f'answer-{attempts}' if outcome in ('normal','repair') else None)
        assert attempts==(2 if outcome=='repair' else 0 if outcome=='clarify' else 1)

@pytest.mark.asyncio
async def test_model_capture_concurrent_and_fallback_isolation(monkeypatch):
    from app.adapters import generation
    monkeypatch.setattr(generation,'generate',lambda messages,*a,**k:generation.GenerationResult('text',[],messages))
    async def call(name):
        with generation.capture_generation_models() as records:
            await asyncio.to_thread(generation.generate_text,name)
            with generation.capture_generation_models() as fallback:
                await asyncio.to_thread(generation.generate_text,'single-'+name)
            assert records==[name] and fallback==['single-'+name]
        return records
    assert await asyncio.gather(call('a'),call('b'))==[['a'],['b']]
