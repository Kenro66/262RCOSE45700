"""异步内部服务；持久化、网络接口接入和回退由后端负责。"""

import asyncio
import json
from typing import cast
from uuid import uuid4

from langgraph.errors import NodeCancelledError

from ...contracts import ChatStatus, Citation, DocumentStatus
from ...adapters.generation import capture_generation_models
from ..citation import Evidence, resolve_citations
from ..orchestrator import Completion, DocumentLookup, Generator, Retrieval
from ..prompts import detect_language
from .graph import build_graph
from .conditions import retrieval_policy_scope
from .nodes import AgentError, AgentErrorType, Dependencies, Nodes, classify_error
from .state import (AgentChatResponse, AgentResult, AgentState, CachedEvidence,
                    GraphState, normalize_agent_state)


def snapshot(state: GraphState, cached_evidence=None) -> AgentState:
    evidence = state["evidence"] if cached_evidence is None else cached_evidence
    return AgentState(
        retrieval_count=state["retrieval_count"], clarification_count=state["clarification_count"],
        evidence_ids=[e.evidence_id for e in evidence],
        evidence=[CachedEvidence(evidence_id=e.evidence_id, hit=e.hit) for e in evidence],
        question=state["question"], session_id=state["session_id"], question_id=state["question_id"],
        conditions=state["conditions"],
        retrieval_queries=state.get("seen_queries",[]),
    )


async def agent_answer(
    question: str, session_id: str | None = None, *, conditions: dict | None = None,
    question_id: str | None = None, agent_state: AgentState | dict | None = None,
    retrieve_fn: Retrieval | None = None, get_documents_fn: DocumentLookup | None = None,
    generate_fn: Generator | None = None, complete_evidence_fn: Completion | None = None,
    on_progress=None, preserve_evidence_ids: bool = False,
) -> AgentResult:
    """使用显式注入的替身或服务执行，不隐式调用外部依赖。

    恢复执行时传入原始问题及相同的会话、问题标识。
    使用完整服务端快照字典或 AgentState 恢复状态。缓存完整来源，
    而非只有证据编号；新请求使用缓存前重新检查文档是否就绪。
    技术故障抛出携带状态和执行轨迹的 AgentError，此处不触发回退。
    """
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must be nonempty")
    for name, value in (("session_id", session_id), ("question_id", question_id)):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"{name} must be nonempty or None")
    if conditions is not None and not isinstance(conditions, dict):
        raise ValueError("conditions must be a dictionary")
    saved = normalize_agent_state(agent_state)
    if saved.question is not None and (saved.question != question or saved.session_id != session_id
                                       or saved.question_id != question_id):
        raise ValueError("Restored state belongs to a different logical question/session")
    if not callable(retrieve_fn):
        raise AgentError("Inject retrieve_fn; no production default is configured")
    if not callable(get_documents_fn):
        raise AgentError("Inject get_documents_fn; no production default is configured")
    if not callable(generate_fn):
        raise AgentError("Inject generate_fn; no production default is configured")
    if complete_evidence_fn is not None and not callable(complete_evidence_fn):
        raise AgentError("complete_evidence_fn must be callable")
    merged = {**saved.conditions, **(conditions or {})}
    # 条件按 JSON 传递，首次检索也必须包含这些条件。
    query = question + ("\nConditions: " + json.dumps(merged, ensure_ascii=False, sort_keys=True) if merged else "")
    initial: GraphState = dict(
        question=question, session_id=session_id, question_id=question_id or f"q_{uuid4().hex}",
        conditions=merged, current_query=query, seen_queries=list(saved.retrieval_queries), retrieval_count=saved.retrieval_count,
        clarification_count=saved.clarification_count,
        evidence=[Evidence(e.evidence_id, e.hit.model_copy(deep=True)) for e in saved.evidence],
        documents={}, sufficient=False, evidence_check_result={}, missing_conditions=[], status=None, answer=None,
        clarification=None, conflict=None, trace=[],
    )
    async def observe(state):
        await on_progress(snapshot(state, nodes.cached_evidence))

    nodes = Nodes(Dependencies(cast(Retrieval, retrieve_fn), cast(DocumentLookup, get_documents_fn),
                              cast(Generator, generate_fn), complete_evidence_fn),
                  # Retain the source registry across scope changes so surviving IDs never shift.
                  cached_evidence=list(initial["evidence"]) if preserve_evidence_ids or (
                      saved.evidence and retrieval_policy_scope(question, merged) is not None) else None,
                  on_progress=observe if on_progress is not None else None)
    try:
        with capture_generation_models() as model_records:
            final = await build_graph(nodes).ainvoke(initial, config={"recursion_limit": 40})
        citations: list[Citation] = []
        if final["status"] == ChatStatus.ANSWERED:
            _, citations = resolve_citations(final["answer"], final["evidence"], final["documents"])
        elif final["status"] == ChatStatus.CONFLICTING_EVIDENCE:
            ids = final["conflict"].side_a + final["conflict"].side_b
            _, citations = resolve_citations("".join(f"[{key}]" for key in ids), final["evidence"], final["documents"])
            missing = set(ids) - {c.evidence_id for c in citations}
            if missing:
                sources = {e.evidence_id: e.hit for e in final["evidence"]}
                for key in missing:
                    if key not in sources:
                        raise AgentError("Conflict references unknown evidence", error_type=AgentErrorType.AGENT_WORKFLOW_ERROR)
                    metadata = final["documents"].get(sources[key].document_id)
                    if metadata is not None and DocumentStatus(metadata.get("status")) == DocumentStatus.READY:
                        raise AgentError("Resolver lost a conflict citation from a READY source",
                                         error_type=AgentErrorType.AGENT_WORKFLOW_ERROR)
                # 文档缺失或尚未就绪表示政策证据不可用，不属于工具故障。
                # 不能返回只有单方证据的冲突或不完整答案。
                final = {**final, "status": ChatStatus.INSUFFICIENT_EVIDENCE,
                         "conflict": None, "answer": None, "clarification": None}
                citations = []
        response = AgentChatResponse(
            status=final["status"], answer=final["answer"], citations=citations,
            message=("현재 증거가 부족하여 관련 규정을 확인할 수 없습니다."
                     if detect_language(question) == "ko" else "现有证据不足，无法确认相关政策。")
                    if final["status"] == ChatStatus.INSUFFICIENT_EVIDENCE else None,
            question_id=final["question_id"], clarification=final["clarification"], conflict=final["conflict"],
        )
        return AgentResult(response=response, state=snapshot(final, nodes.cached_evidence), trace=final["trace"],
                           model_name=model_records[-1] if response.status == ChatStatus.ANSWERED and model_records else None)
    except NodeCancelledError as exc:
        # LangGraph 会包装节点内部依赖抛出的取消异常。
        raise asyncio.CancelledError() from exc
    except Exception as exc:
        error = exc if isinstance(exc, AgentError) else AgentError(
            "Agent dependency or workflow failed", error_type=classify_error(exc))
        error.state = snapshot(nodes.latest or initial, nodes.cached_evidence)
        error.trace = list((nodes.latest or initial)["trace"])
        if error is exc:
            raise
        raise error from exc
