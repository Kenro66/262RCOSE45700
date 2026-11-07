"""Core-focused audits and stable source lookup; synthetic facts are not policy."""
from copy import deepcopy
import json
from unittest.mock import AsyncMock
import pytest
from pydantic import ValidationError
from app.contracts import SearchHit
from app.qa.agent import AgentError, agent_answer
from app.qa.agent.nodes import age_definition_gap, evidence_is_sufficient, trusted_support
from app.qa.agent.state import Conflict, EvidenceDecision
from app.qa.citation import Evidence


def assessment():
    supported=dict(status="supported",affects_core=True,reason="Source supports core",support=[dict(chunk_id="s1")])
    not_required=dict(status="not_required",affects_core=False,reason="Not material",support=[])
    return dict(verdict="SUFFICIENT",core_claim="测试规则",core_support=[dict(chunk_id="s1")],
                definitions=deepcopy(not_required),exceptions=deepcopy(not_required),applicability=deepcopy(supported),
                complete_answer=deepcopy(supported),missing_evidence=[])


def evidence(text="测试规则原文。"):
    return [Evidence("E1",SearchHit(chunk_id="s1",document_id="d1",page_number=1,text=text,score=.9))]


@pytest.mark.parametrize("aspect",["definitions","exceptions","applicability","complete_answer"])
def test_material_missing_coverage_blocks_sufficient(aspect):
    raw=assessment(); raw[aspect]=dict(status="missing",affects_core=True,reason="Changes core",support=[])
    assert not evidence_is_sufficient(EvidenceDecision(**raw),evidence())


@pytest.mark.parametrize("aspect",["definitions","exceptions"])
def test_noncritical_cross_reference_does_not_block_core_answer(aspect):
    raw=assessment(); raw[aspect]=dict(status="missing",affects_core=False,reason="Separate category",support=[])
    assert evidence_is_sufficient(EvidenceDecision(**raw),evidence())


def test_program_preserves_source_exactly_instead_of_model_transcription():
    text="1. 原文\n含 1.5 升及标点。"
    assert trusted_support(EvidenceDecision(**assessment()),evidence(text))["s1"]==dict(evidence_id="E1",quote=text)


def test_transcribed_quote_is_not_a_valid_support_field():
    raw=assessment(); raw["core_support"]=[dict(chunk_id="s1",quote="模型改写的原文")]
    with pytest.raises(ValidationError): EvidenceDecision(**raw)


def test_unknown_chunk_id_is_rejected():
    raw=assessment(); raw["core_support"]=[dict(chunk_id="invented")]
    with pytest.raises(AgentError,match="unknown stable chunk"):
        evidence_is_sufficient(EvidenceDecision(**raw),evidence())


@pytest.mark.parametrize("verdict",["MISSING_CRITICAL","UNSUPPORTED"])
def test_nonsufficient_verdict_is_not_silently_overridden(verdict):
    raw=assessment(); raw["verdict"]=verdict
    assert not evidence_is_sufficient(EvidenceDecision(**raw),evidence())


@pytest.mark.parametrize("value",["[E1]","E1 original quote","E0","source1"])
def test_conflict_sides_require_bare_valid_evidence_id_format(value):
    with pytest.raises(ValidationError): Conflict(reason="Synthetic conflict",side_a=[value],side_b=["E2"])


@pytest.mark.parametrize("question,restriction",[
    ("만 23세 미만이면 시험 혜택이 적용되나요?","만 23세 미만인 자에게는 시험 혜택이 적용되지 않는다."),
    ("不满17周岁的测试旅客适用优惠吗？","不满17周岁的测试旅客不享受优惠。")])
def test_age_restriction_does_not_define_the_category(question,restriction):
    raw=assessment(); raw["definitions"]=dict(status="supported",affects_core=True,reason="Model overstates definition",support=[dict(chunk_id="s1")])
    decision=EvidenceDecision(**raw)
    assert age_definition_gap(question,decision,evidence(restriction))
    assert not evidence_is_sufficient(decision,evidence(restriction),question)


def test_actual_definition_is_checked_in_trusted_source():
    raw=assessment(); raw["definitions"]=dict(status="supported",affects_core=True,reason="Definition exists",support=[dict(chunk_id="s1")])
    text="만 23세 미만인 사람(별도 정의에 해당하는 사람은 제외한다. 이하 같다)."
    assert evidence_is_sufficient(EvidenceDecision(**raw),evidence(text),"만 23세 미만의 적용 범위는?")


def test_quantity_is_not_mistaken_for_an_age():
    assert not age_definition_gap("12度以上饮料多少升？",EvidenceDecision(**assessment()),evidence())


async def invoke(generate,sources=None):
    retrieve=AsyncMock(side_effect=sources if sources is not None else [[evidence()[0].hit]])
    result=await agent_answer("测试规则如何适用？",retrieve_fn=retrieve,
                              get_documents_fn=AsyncMock(return_value={"d1":dict(filename="test.pdf",status="READY")}),
                              generate_fn=generate)
    return result,retrieve


def generator(*,critical_first=False,unsupported=False,repeat_query=False,side_gap=False):
    def generate(messages):
        data=json.loads(messages[-1]["content"]); task=data["task"]
        if task in ("check_conditions","check_applicability"): value=dict(missing_conditions=[])
        elif task=="check_evidence":
            value=assessment()
            if side_gap: value["exceptions"]=dict(status="missing",affects_core=False,reason="Different category",support=[])
            if unsupported: value["verdict"]="UNSUPPORTED"
            elif critical_first and data["retrieval_count"]==1:
                value["verdict"]="MISSING_CRITICAL"; value["missing_evidence"]=["Missing legal premise"]
        elif task=="rewrite_query":
            assert data["previous_evidence_check"]["accepted_sufficient"] is False
            value=dict(query=data["question"] if repeat_query else "检索必要的申报条件及定义")
        elif task=="check_conflict": value=dict(conflict=None)
        else: value=dict(answer="测试规则原文。[E1]",insufficient_evidence=False)
        return json.dumps(value,ensure_ascii=False)
    return generate


@pytest.mark.asyncio
async def test_sufficient_core_stops_despite_unrelated_exception_gap():
    result,retrieve=await invoke(generator(side_gap=True))
    assert result.response.status.value=="ANSWERED"
    assert retrieve.await_count==1 and "rewrite_query" not in result.trace


@pytest.mark.asyncio
async def test_critical_premise_drives_actual_graph_retrieval_loop():
    result,retrieve=await invoke(generator(critical_first=True),[[evidence()[0].hit],[evidence()[0].hit]])
    assert result.response.status.value=="ANSWERED" and retrieve.await_count==2
    assert result.trace[:6]==["check_conditions","retrieve","check_evidence","rewrite_query","retrieve","check_evidence"]


@pytest.mark.asyncio
async def test_unsupported_jurisdiction_stops_without_pointless_rewrite():
    result,retrieve=await invoke(generator(unsupported=True))
    assert result.response.status.value=="INSUFFICIENT_EVIDENCE" and retrieve.await_count==1
    assert "rewrite_query" not in result.trace


@pytest.mark.asyncio
async def test_repeated_query_is_not_executed_twice():
    result,retrieve=await invoke(generator(critical_first=True,repeat_query=True))
    assert result.response.status.value=="INSUFFICIENT_EVIDENCE"
    assert result.state.retrieval_count==retrieve.await_count==1


@pytest.mark.asyncio
async def test_query_history_survives_state_serialization():
    from app.qa.agent import AgentState
    result,_=await invoke(generator())
    restored=AgentState.model_validate_json(result.state.model_dump_json())
    assert restored.retrieval_queries==["测试规则如何适用？"]


@pytest.mark.asyncio
async def test_age_definition_guard_blocks_then_allows_answer_after_definition_retrieval():
    question="만 23세 미만인 사람에게 시험 규칙이 적용되나요?"
    rule=SearchHit(chunk_id="s1",document_id="d1",page_number=1,text="만 23세 미만인 사람에게 시험 혜택은 적용되지 않는다.",score=.9)
    definition=SearchHit(chunk_id="definition",document_id="d1",page_number=2,
                         text="만 23세 미만인 사람(별도 정의에 해당하는 사람은 제외한다. 이하 같다).",score=.8)
    retrieve=AsyncMock(side_effect=[[rule],[definition]])
    def generate(messages):
        data=json.loads(messages[-1]["content"]); task=data["task"]
        if task in ("check_conditions","check_applicability"): value=dict(missing_conditions=[])
        elif task=="check_evidence":
            value=assessment()
            cid="definition" if data["retrieval_count"]>1 else "s1"
            value["definitions"]=dict(status="supported",affects_core=True,reason="Check actual definition",support=[dict(chunk_id=cid)])
        elif task=="rewrite_query":
            assert data["previous_evidence_check"]["verdict"]=="MISSING_CRITICAL"
            value=dict(query="시험 규칙 연령 범주 정의 및 제외")
        elif task=="check_conflict": value=dict(conflict=None)
        else: value=dict(answer="해당 범주에는 시험 혜택이 적용되지 않으며 정의의 제외 사항을 함께 적용합니다.[E1][E2]",insufficient_evidence=False)
        return json.dumps(value,ensure_ascii=False)
    result=await agent_answer(question,retrieve_fn=retrieve,
                              get_documents_fn=AsyncMock(return_value={"d1":dict(filename="test.pdf",status="READY")}),generate_fn=generate)
    assert result.response.status.value=="ANSWERED"
    assert retrieve.await_count==2 and result.trace.count("rewrite_query")==1
    assert [c.page for c in result.response.citations]==[1,2]


@pytest.mark.asyncio
async def test_channel_choice_waits_for_missing_declaration_premise():
    first=SearchHit(chunk_id="s1",document_id="d1",page_number=1,text="须书面申报的入境旅客应走红色通道。",score=.9)
    second=SearchHit(chunk_id="premise",document_id="d1",page_number=2,text="携带超过适用免税额度物品入境应向海关书面申报。",score=.8)
    retrieve=AsyncMock(side_effect=[[first],[second]])
    def generate(messages):
        data=json.loads(messages[-1]["content"]); task=data["task"]
        if task in ("check_conditions","check_applicability"): value=dict(missing_conditions=[])
        elif task=="check_evidence":
            value=assessment()
            if data["retrieval_count"]==1:
                value.update(verdict="MISSING_CRITICAL",missing_evidence=["Whether excess goods require declaration"])
                value["complete_answer"]=dict(status="missing",affects_core=True,reason="Declaration premise missing",support=[])
            else: value["core_support"]=[dict(chunk_id="s1"),dict(chunk_id="premise")]
        elif task=="rewrite_query": value=dict(query="超过免税额度入境物品书面申报前提")
        elif task=="check_conflict": value=dict(conflict=None)
        else: value=dict(answer="超过适用免税额度的物品应书面申报，并选择红色通道。[E1][E2]",insufficient_evidence=False)
        return json.dumps(value,ensure_ascii=False)
    result=await agent_answer("超过免税额度物品入境走什么通道？",retrieve_fn=retrieve,
                              get_documents_fn=AsyncMock(return_value={"d1":dict(filename="test.pdf",status="READY")}),generate_fn=generate)
    assert result.response.status.value=="ANSWERED"
    assert result.state.retrieval_count==2
    assert len(result.response.citations)==2
