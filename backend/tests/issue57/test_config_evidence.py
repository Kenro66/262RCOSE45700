"""检测隐式默认值、来源篡改、标识混用和响应契约回归。"""
import pytest
from pydantic import ValidationError

from app.chat.config import RagSettings, load_rag_settings
from app.chat.persistent.context import RunProgress
from app.chat.persistent.evidence import merge_sources, remap_tokens
from app.chat.persistent.schemas import PersistentChatResponse
from app.qa.agent.state import AgentState, CachedEvidence
from app.qa.citation import Evidence
from .conftest import hit


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["get_documents_fn", "generate_fn"])
async def test_single_rejects_invalid_dependencies_before_retrieval(invalid):
    from unittest.mock import AsyncMock, Mock
    from app.qa.orchestrator import AnswerError, answer

    retrieve = AsyncMock(return_value=[])
    dependencies = dict(retrieve_fn=retrieve, get_documents_fn=AsyncMock(), generate_fn=Mock())
    dependencies[invalid] = object()
    with pytest.raises(AnswerError, match=invalid):
        await answer("测试问题", **dependencies)
    retrieve.assert_not_awaited()


@pytest.mark.parametrize("mode", ["single", "agent"])
def test_explicit_mode_and_positive_timeout(mode, monkeypatch):
    config = {"RAG_MODE": mode, "RAG_EXECUTION_TIMEOUT_SECONDS": "120"}
    monkeypatch.setattr("app.chat.config.settings", lambda: config)
    assert load_rag_settings() == RagSettings(mode, 120)
    assert load_rag_settings(config).mode == mode


@pytest.mark.parametrize("mode", [None, "", "AGENT", " single", "rag"])
def test_missing_or_invalid_mode_never_defaults(mode):
    with pytest.raises(ValueError, match="RAG_MODE"):
        load_rag_settings({"RAG_MODE": mode, "RAG_EXECUTION_TIMEOUT_SECONDS": "120"})


@pytest.mark.parametrize("timeout", [None, "", "0", "-1", "1.5", " 3", "１２", "two", 12])
def test_timeout_must_be_explicit_positive_ascii_integer(timeout):
    with pytest.raises(ValueError, match="TIMEOUT"):
        load_rag_settings({"RAG_MODE": "agent", "RAG_EXECUTION_TIMEOUT_SECONDS": timeout})


def test_sources_keep_stable_ids_and_full_original_text():
    state = AgentState(question="Q", session_id="s", question_id="q", retrieval_count=1, retrieval_queries=["Q"])
    first = hit(text="原文\n第二行")
    merged = merge_sources(state, [first, Evidence("E99", first), CachedEvidence(evidence_id="E8", hit=hit("c2"))])
    assert merged.evidence_ids == ["E1", "E2"]
    assert merged.evidence[0].hit.text == "原文\n第二行"
    assert state.evidence == []
    assert remap_tokens("[E1] [E10] [E2] E1", {"E1": "E3"}) == "[E3] [E10] [E2] E1"
    with pytest.raises(ValueError, match="Conflicting"):
        merge_sources(merged, [hit(text="changed")])


def test_progress_rejects_wrong_scope_and_identity():
    state = AgentState(question="Q", session_id="s", question_id="q", retrieval_count=1, retrieval_queries=["Q"])
    progress = RunProgress(state, document_ids=("d1",))
    progress.accept_sources([hit()])
    assert progress.snapshot(state).evidence_ids == ["E1"]
    with pytest.raises(ValueError, match="belong"):
        progress.snapshot(state.model_copy(update={"question_id": "other"}))
    with pytest.raises(ValueError, match="scope"):
        progress.accept_sources([hit(document="other")])
    assert progress.poisoned


def test_single_refusal_preserved_and_agent_response_still_validated():
    base = dict(question_id="q", session_id="s", execution_status="SUCCEEDED", status="INSUFFICIENT_EVIDENCE")
    assert PersistentChatResponse(**base, answer="原 Single 拒答").answer == "原 Single 拒答"
    assert PersistentChatResponse(**base, message="Agent 拒答").answer is None
    for changes in ({"answer": " "}, {"question_id": " ", "answer": "拒答"}):
        with pytest.raises(ValidationError):
            PersistentChatResponse(**(base | changes))
