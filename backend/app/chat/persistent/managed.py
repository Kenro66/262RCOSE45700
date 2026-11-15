"""Managed mode routing, progress and guarded fallback behind the existing API."""

import logging

from app.contracts import ChatResponse, ChatStatus
from app.qa.agent.nodes import AgentError
from app.qa.agent.runtime import RealRAG
from app.qa.agent.service import agent_answer
from app.qa.agent.state import AgentChatResponse, AgentResult, normalize_agent_state
from app.storage.repo import get_documents, has_ready_documents

from .cancellation import AgentStopped, call_until_stopped
from .context import ExecutionGuard, RunProgress
from .fallback import maybe_fallback

logger = logging.getLogger(__name__)


async def execute_managed(runner, data, stop_event):
    store = runner.store
    qid, run_id = data["question_id"], data["run_id"]

    async def finish(status, response=None, *, reliable=False):
        payload = response.model_dump(mode="json") if response is not None else None
        if payload is not None:
            payload.setdefault("question_id", qid)
        await store.finish(qid, runner.worker_id, status,
                           payload,
                           "SYSTEM_ERROR" if status == "FAILED" else None,
                           state_reliable=reliable, execution_key=run_id)

    if data["mode"] == "single":
        from app.chat.runtime import answer_question
        try:
            ready = await call_until_stopped(has_ready_documents, stop_event, session_factory=store.factory)
            response = (await call_until_stopped(answer_question, stop_event, data["question"], data["session_id"])
                        if ready else ChatResponse(status=ChatStatus.NO_READY_DOCUMENTS, message="当前没有可用文件"))
            await finish("SUCCEEDED", response, reliable=True)
        except AgentStopped:
            await finish("CANCELLED", reliable=True)
        except Exception:
            await finish("FAILED", ChatResponse(status=ChatStatus.SYSTEM_ERROR, message="系统暂时无法处理请求，请稍后重试。"), reliable=True)
        return

    saved = normalize_agent_state(data["state"])
    progress = RunProgress(saved, document_ids=tuple(data["document_ids"] or []))
    guard = ExecutionGuard(store, data, stop_event, progress)
    reliable = False

    async def checkpoint(state):
        nonlocal reliable
        reliable = False
        state = progress.snapshot(state)
        if progress.poisoned:
            raise ValueError("Unconfirmed execution progress")
        await store.checkpoint(qid, runner.worker_id, state, execution_key=run_id)
        reliable = True
        return state

    try:
        await guard.check()
        if not data["document_ids"]:
            result = AgentResult(response=AgentChatResponse(status=ChatStatus.NO_READY_DOCUMENTS,
                                 question_id=qid, message="当前没有可用文件"), state=saved)
        else:
            try:
                async with RealRAG(data["question"], conditions=saved.conditions,
                                   session_factory=store.factory, document_ids=data["document_ids"]) as runtime:
                    result = await agent_answer(data["question"], data["session_id"],
                        question_id=qid, conditions=data["conditions"], agent_state=saved,
                        on_progress=checkpoint, preserve_evidence_ids=True,
                        **guard.dependencies(runtime))
            except AgentError as error:
                await guard.check(allow_stopped=True)
                if getattr(error, "state", None) is not None:
                    await checkpoint(error.state)
                else:
                    reliable = False
                if guard.reason or not reliable:
                    raise
                result = await maybe_fallback(error, data, store, progress, guard)
                if result is None:
                    raise
        await guard.check()
        if result.response.citations:
            cited = {item.evidence_id for item in result.response.citations}
            ids = list({item.hit.document_id for item in result.state.evidence if item.evidence_id in cited})
            documents = await guard.call(get_documents, ids, session_factory=store.factory)
            if any(item not in documents or documents[item].status != "READY" for item in ids):
                result = AgentResult(response=AgentChatResponse(status=ChatStatus.INSUFFICIENT_EVIDENCE,
                    question_id=qid, message="现有证据不足，无法确认相关政策。"), state=result.state)
        final_state = await checkpoint(result.state)
        await store.checkpoint(qid, runner.worker_id, final_state, result.response.clarification, execution_key=run_id)
        await finish("SUCCEEDED", result.response, reliable=True)
    except Exception as error:
        # Cancellation/timeout outrank error classification, even when a service wrapped it.
        try:
            await guard.check(allow_stopped=True)
            candidate = getattr(error, "state", None) or progress.state
            await checkpoint(candidate)
        except Exception:
            reliable = False
        logger.warning("chat execution failed question=%s mode=agent stopped=%s reliable=%s",
                       qid, guard.reason, reliable)
        if guard.reason == "CANCELLED":
            await finish("CANCELLED", reliable=reliable)
        else:
            response = AgentChatResponse(status=ChatStatus.SYSTEM_ERROR, question_id=qid,
                                         message="系统暂时无法处理请求，请稍后重试。")
            await finish("FAILED", response, reliable=reliable)
