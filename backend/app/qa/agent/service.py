"""Async internal service. Backend owns persistence, HTTP integration and fallback."""

import asyncio
import json
from uuid import uuid4

from langgraph.errors import NodeCancelledError

from ...contracts import ChatStatus
from ..citation import Evidence, resolve_citations
from ..orchestrator import Completion, DocumentLookup, Generator, Retrieval
from ..prompts import detect_language
from .graph import build_graph
from .nodes import AgentError, Dependencies, Nodes
from .state import AgentChatResponse, AgentResult, AgentState, CachedEvidence, GraphState


def snapshot(state: GraphState) -> AgentState:
    return AgentState(
        retrieval_count=state["retrieval_count"], clarification_count=state["clarification_count"],
        evidence_ids=[e.evidence_id for e in state["evidence"]],
        evidence=[CachedEvidence(evidence_id=e.evidence_id, hit=e.hit) for e in state["evidence"]],
        question=state["question"], session_id=state["session_id"], question_id=state["question_id"],
        conditions=state["conditions"],
        retrieval_queries=state.get("seen_queries",[]),
    )


async def agent_answer(
    question: str, session_id: str | None = None, *, conditions: dict | None = None,
    question_id: str | None = None, agent_state: AgentState | None = None,
    retrieve_fn: Retrieval | None = None, get_documents_fn: DocumentLookup | None = None,
    generate_fn: Generator | None = None, complete_evidence_fn: Completion | None = None,
) -> AgentResult:
    """Execute with injected mocks/services; no implicit external dependencies.

    Supply the original question and the same session/question IDs when resuming.
    Restore JSON via AgentState.model_validate_json(). Full sources, not E IDs,
    are cached; document readiness is rechecked before using them on a new request.
    Technical failures raise AgentError with state/trace, never trigger fallback here.
    """
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must be nonempty")
    for name, value in (("session_id", session_id), ("question_id", question_id)):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"{name} must be nonempty or None")
    if conditions is not None and not isinstance(conditions, dict):
        raise ValueError("conditions must be a dictionary")
    saved = AgentState() if agent_state is None else AgentState.model_validate(agent_state.model_dump())
    if saved.question is not None and (saved.question != question or saved.session_id != session_id
                                       or saved.question_id != question_id):
        raise ValueError("Restored state belongs to a different logical question/session")
    for name, fn in (("retrieve_fn", retrieve_fn), ("get_documents_fn", get_documents_fn), ("generate_fn", generate_fn)):
        if not callable(fn):
            raise AgentError(f"Inject {name}; no production default is configured")
    if complete_evidence_fn is not None and not callable(complete_evidence_fn):
        raise AgentError("complete_evidence_fn must be callable")
    merged = {**saved.conditions, **(conditions or {})}
    # Conditions are transported as JSON and included in the first retrieval, too.
    query = question + ("\nConditions: " + json.dumps(merged, ensure_ascii=False, sort_keys=True) if merged else "")
    initial: GraphState = dict(
        question=question, session_id=session_id, question_id=question_id or f"q_{uuid4().hex}",
        conditions=merged, current_query=query, seen_queries=list(saved.retrieval_queries), retrieval_count=saved.retrieval_count,
        clarification_count=saved.clarification_count,
        evidence=[Evidence(e.evidence_id, e.hit.model_copy(deep=True)) for e in saved.evidence],
        documents={}, sufficient=False, evidence_check_result={}, missing_conditions=[], status=None, answer=None,
        clarification=None, conflict=None, trace=[],
    )
    nodes = Nodes(Dependencies(retrieve_fn, get_documents_fn, generate_fn, complete_evidence_fn))
    try:
        final = await build_graph(nodes).ainvoke(initial, config={"recursion_limit": 40})
        citations = []
        if final["status"] == ChatStatus.ANSWERED:
            _, citations = resolve_citations(final["answer"], final["evidence"], final["documents"])
        elif final["status"] == ChatStatus.CONFLICTING_EVIDENCE:
            ids = final["conflict"].side_a + final["conflict"].side_b
            _, citations = resolve_citations("".join(f"[{key}]" for key in ids), final["evidence"], final["documents"])
        response = AgentChatResponse(
            status=final["status"], answer=final["answer"], citations=citations,
            message=("현재 증거가 부족하여 관련 규정을 확인할 수 없습니다."
                     if detect_language(question) == "ko" else "现有证据不足，无法确认相关政策。")
                    if final["status"] == ChatStatus.INSUFFICIENT_EVIDENCE else None,
            question_id=final["question_id"], clarification=final["clarification"], conflict=final["conflict"],
        )
        return AgentResult(response=response, state=snapshot(final), trace=final["trace"])
    except NodeCancelledError as exc:
        # LangGraph wraps cancellation raised by a dependency inside a node.
        raise asyncio.CancelledError() from exc
    except Exception as exc:
        error = exc if isinstance(exc, AgentError) else AgentError("Agent dependency or workflow failed")
        error.state = snapshot(nodes.latest or initial)
        error.trace = list((nodes.latest or initial)["trace"])
        if error is exc:
            raise
        raise error from exc
