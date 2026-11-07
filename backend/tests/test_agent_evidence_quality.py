"""Generic coverage gates: no policy question, page or answer is hardcoded."""

from copy import deepcopy
import json
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.contracts import SearchHit
from app.qa.agent import AgentError, agent_answer
from app.qa.agent.nodes import age_definition_gap, evidence_is_sufficient, proof_text
from app.qa.agent.state import Conflict, EvidenceDecision
from app.qa.citation import Evidence


def assessment():
    supported = {"status": "supported", "reason": "Source supports scope", "support": [
        {"evidence_id": "E1", "quote": "测试规则原文。"}]}
    not_required = {"status": "not_required", "reason": "Not material to this synthetic question", "support": []}
    return dict(sufficient=True, definitions=deepcopy(not_required), exceptions=deepcopy(not_required),
                applicability=deepcopy(supported), complete_answer=deepcopy(supported), missing_evidence=[])


def evidence():
    return [Evidence("E1", SearchHit(chunk_id="s1", document_id="d1", page_number=1,
                                    text="测试规则原文。", score=.9))]


@pytest.mark.parametrize("aspect", ["definitions", "exceptions", "applicability", "complete_answer"])
def test_missing_coverage_overrides_confident_sufficient_boolean(aspect):
    raw = assessment()
    raw[aspect] = {"status": "missing", "reason": "Required source is absent", "support": []}
    assert not evidence_is_sufficient(EvidenceDecision(**raw), evidence())


def test_missing_search_gap_overrides_sufficient_boolean():
    raw = assessment()
    raw["missing_evidence"] = ["Definition of the traveler's age category"]
    assert not evidence_is_sufficient(EvidenceDecision(**raw), evidence())


def test_supported_coverage_requires_a_source_excerpt():
    raw = assessment()
    raw["definitions"] = {"status": "supported", "reason": "Unsupported claim", "support": []}
    assert not evidence_is_sufficient(EvidenceDecision(**raw), evidence())


@pytest.mark.parametrize("support", [dict(evidence_id="E999", quote="测试规则原文。"),
                                    dict(evidence_id="E1", quote="原文没有这个例外。")])
def test_fabricated_proof_is_a_technical_validation_failure(support):
    raw = assessment()
    raw["complete_answer"]["support"] = [support]
    with pytest.raises(AgentError, match="nonexistent source or quote"):
        evidence_is_sufficient(EvidenceDecision(**raw), evidence())


def test_complete_grounded_assessment_passes():
    assert evidence_is_sufficient(EvidenceDecision(**assessment()), evidence())


@pytest.mark.parametrize("value", ["[E1]", "E1 original quote", "E0", "source1"])
def test_conflict_sides_require_bare_valid_evidence_id_format(value):
    with pytest.raises(ValidationError):
        Conflict(reason="Synthetic test conflict", side_a=[value], side_b=["E2"])


def test_conflict_side_id_schema_exposes_format_to_the_real_model():
    schema = Conflict.model_json_schema()
    assert schema["properties"]["side_a"]["items"]["pattern"] == r"^E[1-9]\d*$"


@pytest.mark.parametrize("question,restriction", [
    ("만 23세 미만이면 시험 혜택이 적용되나요?", "만 23세 미만인 자에게는 시험 혜택이 적용되지 않는다."),
    ("不满17周岁的测试旅客适用优惠吗？", "不满17周岁的测试旅客不享受优惠。"),
])
def test_age_restriction_cannot_be_relabelled_as_a_complete_definition(question, restriction):
    raw = assessment()
    raw["definitions"] = {"status": "supported", "reason": "Model mistakenly treats restriction as definition",
                          "support": [{"evidence_id": "E1", "quote": restriction}]}
    source = Evidence("E1", SearchHit(chunk_id="test",document_id="d",page_number=1,text=restriction+"测试规则原文。",score=.9))
    decision = EvidenceDecision(**raw)
    assert age_definition_gap(question, decision)
    assert not evidence_is_sufficient(decision, [source], question)


def test_explicit_age_definition_with_exclusion_can_pass_without_fixed_age_or_page():
    raw = assessment()
    quote = "만 23세 미만인 사람(별도 정의에 해당하는 사람은 제외한다. 이하 같다)"
    raw["definitions"] = {"status": "supported", "reason": "Explicit synthetic definition",
                          "support": [{"evidence_id": "E1", "quote": quote}]}
    source = Evidence("E1",SearchHit(chunk_id="test",document_id="d",page_number=7,text=quote+"테스트 규칙.测试规则原文。",score=.9))
    assert evidence_is_sufficient(EvidenceDecision(**raw),[source],"만 23세 미만의 적용 범위는?")


def test_general_quantity_question_is_not_treated_as_an_age_category():
    assert not age_definition_gap("12度以上饮料最多多少升？", EvidenceDecision(**assessment()))


def test_internal_proof_allows_escaped_newlines_and_ordinal_punctuation_only():
    assert proof_text("3. 测试\n条款。")==proof_text(r"3 测试\n条款")
    assert proof_text("1.5升") != proof_text("15升")
    assert proof_text("1,000元") != proof_text("1,00元")


def test_proof_matching_does_not_mutate_original_source_text():
    original=evidence()[0].hit.text
    assert evidence_is_sufficient(EvidenceDecision(**assessment()),evidence())
    assert evidence()[0].hit.text==original


@pytest.mark.asyncio
async def test_missing_definition_drives_actual_graph_rewrite_and_search_gap_context():
    seen = []
    source = evidence()[0].hit
    retrieve = AsyncMock(return_value=[source])
    async def generate(messages):
        data = json.loads(messages[-1]["content"])
        seen.append(data)
        task = data["task"]
        if task in ("check_conditions", "check_applicability"):
            value = {"missing_conditions": []}
        elif task == "check_evidence":
            value = assessment()
            if data["retrieval_count"] == 1:
                value["definitions"] = {"status": "missing", "reason": "Category definition is absent", "support": []}
                value["missing_evidence"] = ["Category definition"]
        elif task == "rewrite_query":
            assert data["previous_evidence_check"]["accepted_sufficient"] is False
            assert data["previous_evidence_check"]["missing_evidence"] == ["Category definition"]
            value = {"query": "查询该类别的定义及排除项"}
        elif task == "check_conflict":
            value = {"conflict": None}
        else:
            value = {"answer": "测试规则原文。[E1]", "insufficient_evidence": False}
        return json.dumps(value, ensure_ascii=False)
    result = await agent_answer("测试规则如何适用？", retrieve_fn=retrieve,
                                get_documents_fn=AsyncMock(return_value={"d1": {"filename": "test.pdf", "status": "READY"}}),
                                generate_fn=generate)
    assert result.response.status.value == "ANSWERED"
    assert result.state.retrieval_count == retrieve.await_count == 2
    assert result.trace[:6] == ["check_conditions", "retrieve", "check_evidence", "rewrite_query", "retrieve", "check_evidence"]
