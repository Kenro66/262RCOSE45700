"""通过真实数据库操作验证持久化预算、重放、过期写入和停止优先级。"""
from datetime import datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import select

from app.chat.config import RagSettings
from app.chat.persistent.errors import ChatProblem
from app.chat.persistent.models import ChatQuestion, ChatRetrievalAttempt, ChatSubmission
from app.chat.persistent.schemas import PersistentChatRequest
from app.qa.agent.state import AgentState, CachedEvidence
from .conftest import checkpoint, hit

pytestmark = pytest.mark.asyncio


async def change(execution, question=None, submission=None):
    async with execution.factory() as session, session.begin():
        for model, values in ((ChatQuestion, question), (ChatSubmission, submission)):
            if values:
                row = await session.get(model, execution.qid)
                for key, value in values.items():
                    setattr(row, key, value)


async def test_mode_scope_and_run_token_are_persisted_but_token_is_not_public(execution):
    assert execution.data["mode"] == "agent"
    assert execution.data["document_ids"] == ["d1"]
    assert execution.key.startswith("run_")
    assert "run_id" not in json.loads(json.dumps(execution.row))
    assert await execution.store.check_run(execution.qid, execution.key) is None
    await checkpoint(execution)
    assert await execution.store.begin_fallback(execution.qid, execution.key, execution.state)
    assert not await execution.store.begin_fallback(execution.qid, execution.key, execution.state)


async def test_failed_retrieval_counts_and_fourth_dispatch_is_blocked(execution):
    for index, source in enumerate(("AGENT", "AGENT", "FALLBACK"), 1):
        assert await execution.store.reserve_retrieval(execution.qid, execution.key, f"query{index}", source) == index
        await execution.store.finish_retrieval(execution.qid, execution.key, index, "FAILED")
    with pytest.raises(ChatProblem, match="检索"):
        await execution.store.reserve_retrieval(execution.qid, execution.key, "fourth", "FALLBACK")
    async with execution.factory() as session:
        attempts = list((await session.scalars(select(ChatRetrievalAttempt).order_by(ChatRetrievalAttempt.ordinal))).all())
        assert [(a.ordinal, a.source, a.outcome) for a in attempts] == [(1, "AGENT", "FAILED"), (2, "AGENT", "FAILED"), (3, "FALLBACK", "FAILED")]


@pytest.mark.parametrize("query,source", [("", "AGENT"), (" ", "AGENT"), (None, "AGENT"), ("q", "OTHER")])
async def test_invalid_attempt_is_rejected_without_spending_budget(execution, query, source):
    with pytest.raises(ValueError):
        await execution.store.reserve_retrieval(execution.qid, execution.key, query, source)
    async with execution.factory() as session:
        assert (await session.get(ChatQuestion, execution.qid)).retrieval_used == 0


@pytest.mark.parametrize("field,value", [("cancel_requested", True), ("stop_reason", "TIMED_OUT"), ("deadline_at", None), ("deadline_at", datetime(2000, 1, 1, tzinfo=timezone.utc))])
async def test_stopped_or_unknown_deadline_never_dispatches(execution, field, value):
    await change(execution, submission={field: value})
    with pytest.raises(ChatProblem):
        await execution.store.reserve_retrieval(execution.qid, execution.key, "query", "AGENT")


@pytest.mark.parametrize("changes", [{"mode": "single"}, {"retrieval_used": None}])
async def test_unknown_budget_or_wrong_mode_never_dispatches(execution, changes):
    await change(execution, question=changes)
    with pytest.raises(ChatProblem):
        await execution.store.reserve_retrieval(execution.qid, execution.key, "query", "AGENT")


async def test_old_run_cannot_read_write_or_finish_new_run(execution):
    await checkpoint(execution)
    await execution.store.finish(execution.qid, "worker", "FAILED", execution_key=execution.key)
    execution.store.rag_settings = RagSettings("single", 120)
    record, started = await execution.store.reserve(PersistentChatRequest(message=execution.data["question"], session_id="s1", question_id=execution.qid), "worker")
    assert started and record.run_id != execution.key
    new_data = await execution.store.execution_input(execution.qid, "worker")
    assert new_data["mode"] == "agent"
    for operation in (
        execution.store.get_execution(execution.qid, execution_key=execution.key),
        execution.store.check_run(execution.qid, execution.key),
        checkpoint(execution),
    ):
        with pytest.raises(ChatProblem):
            await operation
    await execution.store.finish(execution.qid, "worker", "FAILED", execution_key=execution.key)
    assert (await execution.store.get_execution(execution.qid))["execution_status"] == "RUNNING"


async def test_missing_run_and_missing_attempt_are_rejected(execution):
    with pytest.raises(ChatProblem) as error:
        await execution.store.check_run("absent", "token")
    assert error.value.status == 404
    with pytest.raises(ChatProblem):
        await execution.store.finish_retrieval(execution.qid, execution.key, 1, "SUCCEEDED")
    await execution.store.reserve_retrieval(execution.qid, execution.key, "query", "AGENT")
    async with execution.factory() as session, session.begin():
        (await session.get(ChatRetrievalAttempt, (execution.qid, 1))).run_id = "old"
    with pytest.raises(ChatProblem):
        await execution.store.finish_retrieval(execution.qid, execution.key, 1, "SUCCEEDED")


async def test_inflight_call_blocks_finish_and_fallback(execution):
    await checkpoint(execution)
    await execution.store.mark_call(execution.qid, execution.key, True)
    with pytest.raises(ChatProblem):
        await execution.store.finish(execution.qid, "worker", "FAILED", execution_key=execution.key)
    with pytest.raises(ChatProblem):
        await execution.store.begin_fallback(execution.qid, execution.key, execution.state)
    await execution.store.mark_call(execution.qid, execution.key, False)
    await checkpoint(execution)
    assert await execution.store.begin_fallback(execution.qid, execution.key, execution.state)


@pytest.mark.parametrize("condition", ["cancel", "timeout", "stopped"])
async def test_stop_prevents_new_external_calls(execution, condition):
    if condition == "cancel":
        await execution.store.cancel_question(execution.qid)
    elif condition == "timeout":
        await change(execution, submission={"deadline_at": datetime.now(timezone.utc) - timedelta(seconds=1)})
    else:
        await change(execution, submission={"stop_reason": "TIMED_OUT"})
    expected = "CANCELLED" if condition == "cancel" else "TIMED_OUT"
    assert await execution.store.check_run(execution.qid, execution.key) == expected
    with pytest.raises(ChatProblem):
        await execution.store.mark_call(execution.qid, execution.key, True)
    await execution.store.mark_call(execution.qid, execution.key, False)
    await execution.store.finish(execution.qid, "worker", "SUCCEEDED", {"status": "ANSWERED", "answer": "late"}, execution_key=execution.key)
    result = await execution.store.get_execution(execution.qid)
    if condition == "cancel":
        assert result["execution_status"] == "CANCELLED" and result["response"] is None
    elif condition == "timeout":
        assert result["execution_status"] == "FAILED" and result["response"]["status"] == "SYSTEM_ERROR"


@pytest.mark.parametrize("corruption", ["count", "query", "pending", "conditions", "scope", "unknown_budget"])
async def test_inconsistent_snapshot_never_enables_fallback(execution, corruption):
    state = execution.state
    if corruption in {"count", "query", "pending"}:
        await execution.store.reserve_retrieval(execution.qid, execution.key, "query", "AGENT")
        if corruption != "pending":
            await execution.store.finish_retrieval(execution.qid, execution.key, 1, "FAILED")
        state = state.model_copy(update={"retrieval_count": 0 if corruption == "count" else 1,
                                       "retrieval_queries": ["wrong" if corruption == "query" else "query"]})
    elif corruption == "conditions":
        state = state.model_copy(update={"conditions": {"country": "KR"}})
    elif corruption == "scope":
        state = state.model_copy(update={"evidence": [CachedEvidence(evidence_id="E1", hit=hit(document="outside"))], "evidence_ids": ["E1"]})
    else:
        await change(execution, question={"retrieval_used": None})
    with pytest.raises(ChatProblem):
        await checkpoint(execution, state)
    with pytest.raises(ChatProblem):
        await execution.store.begin_fallback(execution.qid, execution.key, state)


@pytest.mark.parametrize("changes", [{"cancel_requested": True}, {"stop_reason": "TIMED_OUT"}, {"deadline_at": None}, {"deadline_at": datetime(2000, 1, 1, tzinfo=timezone.utc)}])
async def test_fallback_gate_refuses_stop_or_missing_time(execution, changes):
    await checkpoint(execution)
    await change(execution, submission=changes)
    assert not await execution.store.begin_fallback(execution.qid, execution.key, execution.state)


async def test_unconfirmed_snapshot_cannot_fallback(execution):
    assert not await execution.store.begin_fallback(execution.qid, execution.key, execution.state)


async def test_single_failure_can_use_existing_resubmit_without_agent_state(database):
    database.store.rag_settings = RagSettings("single", 120)
    request = PersistentChatRequest(message="Q", session_id="s1")
    record, _ = await database.store.reserve(request, "worker")
    await database.store.finish(record["question_id"], "worker", "FAILED", execution_key=record.run_id)
    replay, started = await database.store.reserve(request.model_copy(update={"question_id": record["question_id"]}), "worker")
    assert started and replay.run_id != record.run_id
    data = await database.store.execution_input(record["question_id"], "worker")
    assert data["state"] == {"conditions": {}} and data["deadline_at"] is None


async def test_delete_session_discards_only_its_inflight_queries(execution):
    store = execution.store
    other, started = await store.reserve(
        PersistentChatRequest(message="另一个会话的问题", session_id="s2"), execution.worker,
    )
    assert started
    await store.reserve_retrieval(execution.qid, execution.key, "deleted query", "AGENT")
    await store.reserve_retrieval(other["question_id"], other.run_id, "other query", "AGENT")

    await store.delete_session(execution.data["session_id"])

    assert (execution.qid, execution.key) not in store.retrieval_queries
    await store.finish_retrieval(other["question_id"], other.run_id, 1, "SUCCEEDED")
    data = await store.execution_input(other["question_id"], execution.worker)
    state = AgentState.model_validate({
        **data["state"], "retrieval_count": 1, "retrieval_queries": ["other query"],
    })
    await store.checkpoint(other["question_id"], execution.worker, state, execution_key=other.run_id)
