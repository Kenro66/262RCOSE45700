from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from app.chat.persistent.errors import ChatProblem
from app.chat.persistent.models import ChatQuestion, ChatSession, ChatSubmission
from app.chat.persistent.schemas import PersistentChatRequest
from app.chat.persistent.store import ChatStore
from app.contracts import SearchHit
from app.qa.agent.state import CachedEvidence
from .conftest import QUESTION, prompt, response, submission

pytestmark = pytest.mark.asyncio


async def follow(db, item, **changes):
    values = submission(item.qid)
    values.update(changes)
    return await db.store.reserve(PersistentChatRequest.model_validate(values), item.worker)


async def test_new_question_creates_three_linked_records(db):
    row, started = await db.store.reserve(PersistentChatRequest(message=QUESTION), "worker")
    assert started and row["session_id"].startswith("s_") and row["question_id"].startswith("q_")
    async with db.factory() as session:
        for model in (ChatSession, ChatQuestion, ChatSubmission):
            assert await session.scalar(select(func.count()).select_from(model)) == 1


async def test_multiple_unanswered_questions_are_independent(db, waiting):
    row, started = await db.store.reserve(PersistentChatRequest(message="另一个问题", session_id=waiting.sid), "workerB")
    assert started and row["question_id"] != waiting.qid
    data = await db.store.session_detail(waiting.sid)
    assert len(data["questions"]) == 2
    assert {q["status"] for q in data["questions"]} == {"WAITING", "RUNNING"}


async def test_identical_active_submission_joins_without_new_row(db, reserved):
    row, started = await db.store.reserve(PersistentChatRequest(message=QUESTION, session_id=reserved.sid, question_id=reserved.qid), "other-worker")
    assert not started and row["execution_status"] == "RUNNING"
    async with db.factory() as session:
        assert await session.scalar(select(func.count()).select_from(ChatSubmission)) == 1


async def test_active_changed_conditions_rejected(db, reserved):
    with pytest.raises(ChatProblem) as error:
        await follow(db, reserved)
    assert error.value.code == "QUESTION_BUSY"


async def test_original_message_cannot_be_rewritten(db, waiting):
    with pytest.raises(ChatProblem) as error:
        await follow(db, waiting, message=QUESTION + " CN")
    assert error.value.code == "QUESTION_MISMATCH"


async def test_other_session_cannot_resume_question(db, waiting):
    await db.store.reserve(PersistentChatRequest(message="其他会话", session_id="sessionB"), "workerB")
    with pytest.raises(ChatProblem) as error:
        await follow(db, waiting, session_id="sessionB")
    assert error.value.code == "QUESTION_NOT_FOUND"


async def test_waiting_replay_returns_original_prompt(db, waiting):
    row, started = await db.store.reserve(PersistentChatRequest(message=QUESTION, session_id=waiting.sid, question_id=waiting.qid), waiting.worker)
    assert not started
    assert row["response"]["clarification"] == prompt().model_dump(mode="json")


async def test_followup_keeps_counts_and_binds_original_question(db, waiting):
    _, started = await follow(db, waiting)
    assert started
    data = await db.store.execution_input(waiting.qid, waiting.worker)
    assert data["question"] == QUESTION
    assert data["state"]["retrieval_count"] == data["state"]["clarification_count"] == 1
    assert data["state"]["conditions"] == {"country": "CN"}


async def test_checkpoint_discards_retrieval_data(db, reserved):
    hit = SearchHit(chunk_id="chunk1", document_id="doc1", page_number=2, text="原始政策证据", score=.9)
    state = reserved.state.model_copy(update={"retrieval_count": 1, "evidence_ids": ["E1"], "evidence": [CachedEvidence(evidence_id="E1", hit=hit)], "retrieval_queries": [QUESTION]})
    await db.store.checkpoint(reserved.qid, reserved.worker, state)
    data = await db.store.execution_input(reserved.qid, reserved.worker)
    assert data["state"]["retrieval_count"] == 1
    assert data["state"]["evidence"] == data["state"]["evidence_ids"] == []
    assert data["state"]["retrieval_queries"] == []
    async with db.factory() as session:
        question = await session.get(ChatQuestion, reserved.qid)
        assert set(question.state) == {"conditions", "retrieval_count", "clarification_count"}


@pytest.mark.parametrize("field", ["retrieval_count", "clarification_count"])
async def test_checkpoint_cannot_rewind_budget(db, waiting, field):
    await follow(db, waiting)
    bad = waiting.state.model_copy(update={field: 0})
    with pytest.raises(ChatProblem) as error:
        await db.store.checkpoint(waiting.qid, waiting.worker, bad)
    assert error.value.code == "INVALID_SAVED_STATE"


async def test_checkpoint_rejects_other_question_binding(db, reserved):
    with pytest.raises(ChatProblem) as error:
        await db.store.checkpoint(reserved.qid, reserved.worker, reserved.state.model_copy(update={"question_id": "wrong"}))
    assert error.value.code == "INVALID_SAVED_STATE"


async def test_other_worker_cannot_read_or_checkpoint_execution(db, reserved):
    with pytest.raises(ChatProblem) as error:
        await db.store.execution_input(reserved.qid, "wrong")
    assert error.value.code == "QUESTION_NOT_ACTIVE"
    with pytest.raises(ChatProblem) as error:
        await db.store.checkpoint(reserved.qid, "wrong", reserved.state)
    assert error.value.code == "STALE_EXECUTION"
    await db.store.finish(reserved.qid, "wrong", "SUCCEEDED", response(reserved.qid))
    assert (await db.store.get_execution(reserved.qid))["execution_status"] == "RUNNING"


async def test_completed_same_conditions_replay_changed_conditions_rejected(db, waiting):
    await follow(db, waiting)
    await db.store.finish(waiting.qid, waiting.worker, "SUCCEEDED", response(waiting.qid))
    _, started = await follow(db, waiting)
    assert not started
    with pytest.raises(ChatProblem) as error:
        await follow(db, waiting, conditions={"country": "KR"})
    assert error.value.code == "QUESTION_FINISHED"


@pytest.mark.parametrize("terminal", ["FAILED", "CANCELLED"])
async def test_reliable_terminal_allows_edited_condition_retry(db, waiting, terminal):
    await follow(db, waiting)
    await db.store.finish(waiting.qid, waiting.worker, terminal, error_code=terminal)
    assert (await db.store.get_execution(waiting.qid))["retryable"]
    _, started = await follow(db, waiting, conditions={"country": "KR"})
    assert started
    data = await db.store.execution_input(waiting.qid, waiting.worker)
    assert data["state"]["retrieval_count"] == 1
    assert data["state"]["conditions"] == {"country": "KR"}


async def test_missing_latest_state_blocks_unsafe_retry(db, waiting):
    await follow(db, waiting)
    await db.store.finish(waiting.qid, waiting.worker, "FAILED", state_reliable=False)
    view = await db.store.get_execution(waiting.qid)
    assert not view["retryable"] and view["error_code"] == "INVALID_SAVED_STATE"
    with pytest.raises(ChatProblem) as error:
        await follow(db, waiting)
    assert error.value.code == "INVALID_SAVED_STATE"


async def test_cancel_registration_does_not_claim_stopped(db, waiting):
    await follow(db, waiting)
    view = await db.store.cancel_question(waiting.qid, session_id=waiting.sid)
    assert view["execution_status"] == "CANCELLING" and not view["retryable"]
    with pytest.raises(ChatProblem, match="正在处理或停止"):
        await follow(db, waiting, conditions={"country": "KR"})


async def test_accepted_cancel_discards_late_answer(db, reserved):
    await db.store.cancel_question(reserved.qid)
    await db.store.finish(reserved.qid, reserved.worker, "SUCCEEDED", response(reserved.qid))
    view = await db.store.get_execution(reserved.qid)
    assert view["execution_status"] == "CANCELLED" and view["response"] is None


async def test_cancel_after_completion_does_not_rewrite_result(db, reserved):
    await db.store.finish(reserved.qid, reserved.worker, "SUCCEEDED", response(reserved.qid))
    view = await db.store.cancel_question(reserved.qid)
    assert view["execution_status"] == "SUCCEEDED" and view["response"] is not None


async def test_old_waiter_does_not_receive_new_conditions_result(db, waiting):
    await follow(db, waiting)
    with pytest.raises(ChatProblem) as error:
        await db.store.get_execution(waiting.qid, conditions={"country": "KR"})
    assert error.value.code == "QUESTION_CHANGED"


async def test_saved_waiting_state_restores_after_store_recreation_without_expiry(db, waiting):
    async with db.factory() as session, session.begin():
        question = await session.get(ChatQuestion, waiting.qid)
        question.updated_at = datetime.now(timezone.utc) - timedelta(days=60)
    restored = ChatStore(db.factory)
    detail = await restored.session_detail(waiting.sid)
    assert detail["questions"][0]["clarification"] == prompt().model_dump(mode="json")
    _, started = await restored.reserve(PersistentChatRequest.model_validate(submission(waiting.qid)), "new-worker")
    assert started
    assert (await restored.execution_input(waiting.qid, "new-worker"))["state"]["retrieval_count"] == 1


@pytest.mark.parametrize("corruption", ["missing_field", "wrong_binding", "missing_submission", "null_state"])
async def test_corrupt_database_state_never_restarts_from_zero(db, waiting, corruption):
    async with db.factory() as session, session.begin():
        question = await session.get(ChatQuestion, waiting.qid)
        if corruption == "missing_submission":
            await session.delete(await session.get(ChatSubmission, waiting.qid))
        elif corruption == "null_state":
            question.state = None
        else:
            state = dict(question.state)
            if corruption == "missing_field":
                state.pop("retrieval_count")
            else:
                state["question_id"] = "wrong"
            question.state = state
    with pytest.raises(ChatProblem) as error:
        await follow(db, waiting)
    assert error.value.code == "INVALID_SAVED_STATE"


async def test_delete_keeps_tombstone_removes_children_and_rejects_late_write(db, waiting):
    await db.store.delete_session(waiting.sid)
    await db.store.finish(waiting.qid, waiting.worker, "SUCCEEDED", response(waiting.qid))
    async with db.factory() as session:
        assert (await session.get(ChatSession, waiting.sid)).deleted
        assert await session.get(ChatQuestion, waiting.qid) is None
        assert await session.get(ChatSubmission, waiting.qid) is None
    assert await db.store.list_sessions() == []
    with pytest.raises(ChatProblem) as error:
        await db.store.reserve(PersistentChatRequest(message=QUESTION, session_id=waiting.sid), waiting.worker)
    assert error.value.code == "SESSION_NOT_FOUND"


@pytest.mark.parametrize("action", ["get_execution", "cancel_question", "session_detail", "delete_session", "execution_input"])
async def test_missing_record_returns_explicit_not_found(db, action):
    arguments = ["missing", "worker"] if action == "execution_input" else ["missing"]
    with pytest.raises(ChatProblem) as error:
        await getattr(db.store, action)(*arguments)
    assert error.value.status == 404


async def test_session_list_is_shared_and_detail_is_not_full_message_history(db, waiting):
    listed = await db.store.list_sessions()
    assert listed[0]["session_id"] == waiting.sid
    assert {"created_at", "updated_at", "title"} <= listed[0].keys()
    detail = await db.store.session_detail(waiting.sid)
    assert "messages" not in detail and "response" not in detail["questions"][0]


async def test_unasked_conditions_rejected_on_failed_original_question(db, reserved):
    await db.store.finish(reserved.qid, reserved.worker, "FAILED")
    with pytest.raises(ChatProblem) as error:
        await follow(db, reserved)
    assert error.value.code == "UNEXPECTED_CONDITIONS"


async def test_late_finish_ignores_session_tombstone_even_if_child_still_exists(db, reserved):
    async with db.factory() as session, session.begin():
        chat = await session.get(ChatSession, reserved.sid)
        chat.deleted = True
    await db.store.finish(reserved.qid, reserved.worker, "SUCCEEDED", response(reserved.qid))
    async with db.factory() as session:
        row = await session.get(ChatSubmission, reserved.qid)
        assert row.status == "RUNNING" and row.response is None
        assert (await session.get(ChatSession, reserved.sid)).deleted


@pytest.mark.parametrize("action", ["get_execution", "cancel_question"])
async def test_existing_question_rejects_wrong_session_in_store(db, reserved, action):
    with pytest.raises(ChatProblem) as error:
        await getattr(db.store, action)(reserved.qid, session_id="wrong")
    assert error.value.code == "QUESTION_NOT_FOUND" and error.value.status == 404


async def test_failed_original_question_can_retry_without_unasked_conditions(db, reserved):
    state = reserved.state.model_copy(update={"retrieval_count": 2, "retrieval_queries": ["a", "b"]})
    await db.store.checkpoint(reserved.qid, reserved.worker, state)
    await db.store.finish(reserved.qid, reserved.worker, "FAILED")
    row, started = await db.store.reserve(PersistentChatRequest(message=QUESTION, session_id=reserved.sid, question_id=reserved.qid), reserved.worker)
    assert started and row["execution_status"] == "RUNNING"
    assert (await db.store.execution_input(reserved.qid, reserved.worker))["state"]["retrieval_count"] == 2


async def test_answer_visible_to_polling_without_database_persistence(db, reserved):
    answer = response(
        reserved.qid, "ANSWERED", answer="结论 [E1]",
        citations=[{"evidence_id": "E1", "filename": "a.pdf", "page": 1, "quote": "政策 A"}],
    )
    await db.store.finish(reserved.qid, reserved.worker, "SUCCEEDED", answer)
    assert (await db.store.get_execution(reserved.qid))["response"]["answer"] == answer["answer"]
    async with db.factory() as session:
        submission_row = await session.get(ChatSubmission, reserved.qid)
        assert submission_row.response is None
    restored = ChatStore(db.factory)
    record = await restored.get_execution(reserved.qid)
    assert record["execution_status"] == "SUCCEEDED"
    assert record["response"] is None


async def test_legacy_evidence_is_discarded_on_followup_after_restart(db, waiting):
    hit = SearchHit(chunk_id="old", document_id="doc1", page_number=1, text="旧条件证据", score=.9)
    legacy = waiting.state.model_copy(update={
        "conditions": {"country": "CN"},
        "evidence_ids": ["E1"],
        "evidence": [CachedEvidence(evidence_id="E1", hit=hit)],
        "retrieval_queries": [QUESTION],
    })
    async with db.factory() as session, session.begin():
        question = await session.get(ChatQuestion, waiting.qid)
        question.state = legacy.model_dump(mode="json")
    restored = ChatStore(db.factory)
    pending = await restored.get_execution(waiting.qid)
    assert pending["response"]["clarification"] == prompt().model_dump(mode="json")
    request = PersistentChatRequest.model_validate(submission(waiting.qid, country="KR"))
    _, started = await restored.reserve(request, waiting.worker)
    assert started
    data = await restored.execution_input(waiting.qid, waiting.worker)
    assert data["state"]["conditions"] == {"country": "KR"}
    assert data["state"]["retrieval_count"] == 1
    assert data["state"]["evidence"] == data["state"]["retrieval_queries"] == []
