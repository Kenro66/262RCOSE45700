import asyncio

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from app.chat.persistent.api import build_persistent_chat_router
from app.chat.persistent.models import ChatQuestion
from app.chat.persistent.runner import ChatRunner
from app.chat.persistent.schemas import PersistentChatRequest
from app.qa.agent.nodes import AgentError
from app.qa.agent.state import AgentChatResponse, AgentResult, AgentState
from .conftest import QUESTION, ask, prompt, question_url, submission

pytestmark = pytest.mark.asyncio


async def test_first_question_and_followup_use_real_agent(web):
    first = await ask(web)
    assert first["status"] == "NEEDS_CLARIFICATION" and first["clarification"]["questions"]
    result = await web.client.post("/api/chat", json=submission(first["question_id"]))
    assert result.status_code == 200 and result.json()["status"] == "INSUFFICIENT_EVIDENCE"
    assert len(web.control.retrievals) == 1
    query = web.control.retrievals[0]
    assert query.startswith(QUESTION) and '"country": "CN"' in query
    async with web.db.factory() as session:
        stored = await session.get(ChatQuestion, first["question_id"])
        assert stored.question == QUESTION
        assert stored.state["clarification_count"] == stored.state["retrieval_count"] == 1


async def test_no_ready_documents_never_calls_agent_services(web):
    web.control.ready = False
    first = await ask(web)
    assert first["status"] == "NO_READY_DOCUMENTS"
    assert web.control.retrievals == web.control.generations == []


async def test_cookie_not_created_and_responses_not_cached(web):
    result = await web.client.get("/api/chat/sessions")
    assert result.status_code == 200
    assert "set-cookie" not in result.headers
    assert result.headers["cache-control"] == "no-store"


async def test_unanswered_questions_can_coexist(web):
    first, second = await ask(web), await ask(web)
    assert first["question_id"] != second["question_id"]
    detail = await web.client.get("/api/chat/sessions/sessionA")
    assert len(detail.json()["questions"]) == 2


@pytest.mark.parametrize("payload", [{"message": " "}, {"message": QUESTION, "request_id": "r"}, {"message": QUESTION, "conditions": {"x": "a"}}])
async def test_invalid_http_request_returns_422(web, payload):
    assert (await web.client.post("/api/chat", json=payload)).status_code == 422


async def test_wrong_condition_rejected_before_model_call(web):
    first = await ask(web)
    result = await web.client.post("/api/chat", json=submission(first["question_id"], country="XX"))
    assert result.status_code == 422 and result.json()["error_code"] == "INVALID_CONDITIONS"
    assert not web.control.retrievals


async def test_pending_and_completed_execution_query(web):
    first = await ask(web)
    base = question_url(first["question_id"])
    complete = await web.client.get(base + "/execution")
    assert complete.status_code == 200 and complete.json()["response"]["status"] == "NEEDS_CLARIFICATION"
    web.control.block_retrieve = True
    await web.client.post("/api/chat?wait=false", json=submission(first["question_id"]))
    await asyncio.wait_for(web.control.entered.wait(), 3)
    pending = await web.client.get(base + "/execution")
    assert pending.status_code == 202 and pending.json()["execution_status"] == "RUNNING"
    assert "cancel_requested" not in pending.json()


async def test_cancel_waits_then_allows_same_question_with_edited_conditions(web):
    first = await ask(web)
    qid, base = first["question_id"], question_url(first["question_id"])
    web.control.block_retrieve = True
    await web.client.post("/api/chat?wait=false", json=submission(qid))
    await asyncio.wait_for(web.control.entered.wait(), 3)
    cancel = await web.client.post(base + "/cancel")
    assert cancel.status_code == 202 and cancel.json()["execution_status"] == "CANCELLING"
    await asyncio.sleep(.25)
    status = await web.client.get(base + "/execution")
    assert status.status_code == 202 and not status.json()["retryable"]
    busy = await web.client.post("/api/chat", json=submission(qid, country="KR"))
    assert busy.status_code == 409 and busy.json()["error_code"] == "QUESTION_BUSY"
    web.control.release.set()
    stopped = await asyncio.wait_for(web.runner.wait(qid), 3)
    assert stopped["execution_status"] == "CANCELLED" and stopped["retryable"]
    again = await web.client.post("/api/chat", json=submission(qid, country="KR"))
    assert again.status_code == 200 and again.json()["question_id"] == qid
    async with web.db.factory() as session:
        state = (await session.get(ChatQuestion, qid)).state
        assert state["retrieval_count"] == 2 and state["clarification_count"] == 1
        assert state["conditions"] == {"country": "KR"}


async def test_cancelled_original_post_is_control_response_without_c5_status(web):
    first = await ask(web)
    qid = first["question_id"]
    web.control.block_retrieve = True
    original = asyncio.create_task(web.client.post("/api/chat", json=submission(qid)))
    try:
        await asyncio.wait_for(web.control.entered.wait(), 3)
        await web.client.post(question_url(qid) + "/cancel")
        web.control.release.set()
        result = await asyncio.wait_for(original, 3)
        assert result.status_code == 200 and result.json()["execution_status"] == "CANCELLED"
        assert "status" not in result.json()
    finally:
        web.control.release.set()
        if not original.done():
            original.cancel()
            await asyncio.gather(original, return_exceptions=True)


async def test_cancel_after_final_result_keeps_success(web):
    first = await ask(web)
    qid = first["question_id"]
    await web.client.post("/api/chat", json=submission(qid))
    result = await web.client.post(question_url(qid) + "/cancel")
    assert result.status_code == 200 and result.json()["execution_status"] == "SUCCEEDED"


@pytest.mark.parametrize("method,suffix", [("post", "/cancel"), ("get", "/execution")])
async def test_wrong_session_cannot_cancel_or_read(web, method, suffix):
    first = await ask(web)
    result = await getattr(web.client, method)(question_url(first["question_id"], "wrong") + suffix)
    assert result.status_code == 404
    assert result.json()["error_code"] == "QUESTION_NOT_FOUND"


async def test_browser_request_cancellation_does_not_cancel_background(web):
    first = await ask(web)
    qid = first["question_id"]
    web.control.block_retrieve = True
    http_task = asyncio.create_task(web.client.post("/api/chat", json=submission(qid)))
    await asyncio.wait_for(web.control.entered.wait(), 3)
    http_task.cancel()
    await asyncio.gather(http_task, return_exceptions=True)
    assert (await web.store.get_execution(qid))["execution_status"] == "RUNNING"
    web.control.release.set()
    assert (await asyncio.wait_for(web.runner.wait(qid), 3))["execution_status"] == "SUCCEEDED"


async def test_duplicate_active_http_request_does_not_retrieve_twice(web):
    first = await ask(web)
    qid = first["question_id"]
    web.control.block_retrieve = True
    first_submit = await web.client.post("/api/chat?wait=false", json=submission(qid))
    await asyncio.wait_for(web.control.entered.wait(), 3)
    duplicate = await web.client.post("/api/chat?wait=false", json=submission(qid))
    assert first_submit.status_code == duplicate.status_code == 202
    web.control.release.set()
    await asyncio.wait_for(web.runner.wait(qid), 3)
    replay = await web.client.post("/api/chat", json=submission(qid))
    assert replay.status_code == 200 and len(web.control.retrievals) == 1


async def test_technical_failure_saves_agent_budget_and_returns_http_500(web):
    first = await ask(web)
    qid = first["question_id"]
    web.control.retrieve_error = RuntimeError("mock dependency failure")
    result = await web.client.post("/api/chat", json=submission(qid))
    assert result.status_code == 500 and result.json()["status"] == "SYSTEM_ERROR"
    assert result.json()["retryable"]
    async with web.db.factory() as session:
        assert (await session.get(ChatQuestion, qid)).state["retrieval_count"] == 1
    status = await web.client.get(question_url(qid) + "/execution")
    assert status.status_code == 200 and status.json()["execution_status"] == "FAILED"


async def test_same_query_after_failure_uses_original_agent_deduplication(web):
    first = await ask(web)
    qid = first["question_id"]
    web.control.retrieve_error = RuntimeError("temporary")
    await web.client.post("/api/chat", json=submission(qid))
    web.control.retrieve_error = None
    result = await web.client.post("/api/chat", json=submission(qid))
    assert result.status_code == 200 and result.json()["status"] == "INSUFFICIENT_EVIDENCE"
    assert len(web.control.retrievals) == 1


async def test_delete_running_question_never_revives_data(web):
    first = await ask(web)
    qid = first["question_id"]
    web.control.block_retrieve = True
    await web.client.post("/api/chat?wait=false", json=submission(qid))
    await asyncio.wait_for(web.control.entered.wait(), 3)
    assert (await web.client.delete("/api/chat/sessions/sessionA")).status_code == 200
    web.control.release.set()
    await asyncio.wait_for(asyncio.gather(*list(web.runner.tasks.values())), 3)
    assert (await web.client.get(question_url(qid) + "/execution")).status_code == 404
    assert (await web.client.get("/api/chat/sessions")).json() == {"sessions": []}


async def test_cancel_retry_before_old_supervisor_cleanup_is_not_overwritten(web, monkeypatch):
    import app.chat.persistent.runner as runner_module
    first = await ask(web)
    qid = first["question_id"]
    committed, cleanup = asyncio.Event(), asyncio.Event()
    original_finish, writes = web.store.finish, []
    stop_signals = []
    original_dependencies = runner_module.stoppable_dependencies

    def observe_dependencies(dependencies, event):
        stop_signals.append(event)
        return original_dependencies(dependencies, event)

    async def finish(*args, **kwargs):
        await original_finish(*args, **kwargs)
        writes.append(args[2])
        if args[2] == "CANCELLED" and not committed.is_set():
            committed.set()
            await cleanup.wait()

    monkeypatch.setattr(web.store, "finish", finish)
    monkeypatch.setattr(runner_module, "stoppable_dependencies", observe_dependencies)
    web.control.block_retrieve = True
    await web.client.post("/api/chat?wait=false", json=submission(qid))
    await asyncio.wait_for(web.control.entered.wait(), 3)
    await web.client.post(question_url(qid) + "/cancel")
    await asyncio.wait_for(stop_signals[0].wait(), 3)
    web.control.release.set()
    try:
        await asyncio.wait_for(committed.wait(), 3)
        retry = await web.client.post("/api/chat?wait=false", json=submission(qid, country="KR"))
        assert retry.status_code == 202
    finally:
        cleanup.set()
    result = await asyncio.wait_for(web.runner.wait(qid), 3)
    assert result["execution_status"] == "SUCCEEDED" and writes.count("CANCELLED") == 1


async def test_runner_invalid_executor_result_fails_without_resetting_state(db):
    async def invalid(data, checkpoint):
        return "wrong contract"

    runner = ChatRunner(db.store, invalid)
    row, _ = await db.store.reserve(PersistentChatRequest(message=QUESTION), runner.worker_id)
    await runner._supervise(row["question_id"])
    result = await db.store.get_execution(row["question_id"])
    assert result["execution_status"] == "FAILED" and not result["retryable"]


async def test_runner_closes_and_forgets_background_tasks(db):
    entered = asyncio.Event()

    async def hanging(data, checkpoint):
        entered.set()
        await asyncio.Event().wait()

    runner = ChatRunner(db.store, hanging)
    row, _ = await db.store.reserve(PersistentChatRequest(message=QUESTION), runner.worker_id)
    runner.start(row["question_id"])
    await asyncio.wait_for(entered.wait(), 3)
    await asyncio.wait_for(runner.close(), 3)
    await asyncio.sleep(0)
    assert not runner.tasks


async def test_forget_old_task_cannot_remove_new_task(db):
    runner = ChatRunner(db.store)
    old, new = asyncio.create_task(asyncio.sleep(0)), asyncio.create_task(asyncio.sleep(0))
    await asyncio.gather(old, new)
    runner.tasks["q"] = new
    runner._forget("q", old)
    assert runner.tasks["q"] is new
    runner._forget("q", new)
    assert "q" not in runner.tasks


async def test_database_exception_returns_safe_http_error(web, monkeypatch):
    async def fail():
        raise RuntimeError("secret-password-not-for-client")

    monkeypatch.setattr(web.store, "list_sessions", fail)
    result = await web.client.get("/api/chat/sessions")
    assert result.status_code == 500 and result.json()["status"] == "SYSTEM_ERROR"
    assert "secret-password" not in result.text


@pytest.mark.parametrize("status", ["ANSWERED", "INSUFFICIENT_EVIDENCE", "NO_READY_DOCUMENTS", "SYSTEM_ERROR", "NEEDS_CLARIFICATION", "CONFLICTING_EVIDENCE"])
async def test_all_six_business_statuses_keep_extended_response_fields(db, status):
    """检查 HTTP 序列化，不测试模型是否正确判断政策。"""
    async def executor(data, checkpoint):
        values = {"question_id": data["question_id"], "status": status}
        if status == "NEEDS_CLARIFICATION":
            values["clarification"] = prompt()
        if status in {"ANSWERED", "CONFLICTING_EVIDENCE"}:
            values["citations"] = [{"evidence_id": "E1", "filename": "a.pdf", "page": 1, "quote": "政策 A"}]
        if status == "ANSWERED":
            values["answer"] = "结论 [E1]"
        if status == "CONFLICTING_EVIDENCE":
            values["citations"].append({"evidence_id": "E2", "filename": "b.pdf", "page": 2, "quote": "政策 B"})
            values["conflict"] = {"reason": "来源结论不一致", "side_a": ["E1"], "side_b": ["E2"]}
        return AgentResult(response=AgentChatResponse(**values), state=AgentState.model_validate(data["state"]))

    runner = ChatRunner(db.store, executor)
    application = FastAPI()
    application.include_router(build_persistent_chat_router(db.store, runner), prefix="/api")
    try:
        async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as client:
            result = await client.post("/api/chat", json={"message": QUESTION})
            assert result.status_code == 200
            body = result.json()
            assert body["status"] == status and body["question_id"] and body["session_id"]
            if status == "ANSWERED":
                assert body["answer"] == "结论 [E1]"
                assert body["citations"] == [{"evidence_id": "E1", "filename": "a.pdf", "page": 1, "quote": "政策 A"}]
            if status == "NEEDS_CLARIFICATION":
                assert body["clarification"]["questions"][0]["field"] == "country"
            if status == "CONFLICTING_EVIDENCE":
                assert body["conflict"] == {"reason": "来源结论不一致", "side_a": ["E1"], "side_b": ["E2"]}
                assert body["answer"] is None
                assert body["citations"] == [
                    {"evidence_id": "E1", "filename": "a.pdf", "page": 1, "quote": "政策 A"},
                    {"evidence_id": "E2", "filename": "b.pdf", "page": 2, "quote": "政策 B"},
                ]
    finally:
        await runner.close()


async def test_exhausted_retrieval_budget_does_not_start_another_retrieval(web):
    first = await ask(web)
    qid = first["question_id"]
    async with web.db.factory() as session, session.begin():
        row = await session.get(ChatQuestion, qid)
        state = dict(row.state)
        state["retrieval_count"] = 3
        row.state = state
    result = await web.client.post("/api/chat", json=submission(qid))
    assert result.status_code == 200 and result.json()["status"] == "INSUFFICIENT_EVIDENCE"
    assert not web.control.retrievals


async def test_agent_cannot_issue_second_clarification_round(web):
    first = await ask(web)
    qid = first["question_id"]
    web.control.extra_questions = [{"field": "age", "question": "年龄？", "type": "text", "options": []}]
    result = await web.client.post("/api/chat", json=submission(qid))
    assert result.status_code == 200 and result.json()["status"] == "INSUFFICIENT_EVIDENCE"
    assert result.json()["clarification"] is None and not web.control.retrievals
    async with web.db.factory() as session:
        assert (await session.get(ChatQuestion, qid)).state["clarification_count"] == 1


async def test_cancel_before_agent_starts_keeps_zero_budget_and_can_resume(web, monkeypatch):
    import app.chat.persistent.runner as runner_module
    entered, release = asyncio.Event(), asyncio.Event()
    signals = []
    original_call = runner_module.call_until_stopped

    async def observe_call(function, event, *args, **kwargs):
        signals.append(event)
        return await original_call(function, event, *args, **kwargs)

    async def ready(**_):
        entered.set()
        await release.wait()
        return True

    monkeypatch.setattr(runner_module, "has_ready_documents", ready)
    monkeypatch.setattr(runner_module, "call_until_stopped", observe_call)
    pending = await web.client.post("/api/chat?wait=false", json={"message": QUESTION, "session_id": "sessionA"})
    qid = pending.json()["question_id"]
    await asyncio.wait_for(entered.wait(), 3)
    await web.client.post(question_url(qid) + "/cancel")
    await asyncio.wait_for(signals[0].wait(), 3)
    release.set()
    stopped = await asyncio.wait_for(web.runner.wait(qid), 3)
    assert stopped["execution_status"] == "CANCELLED" and stopped["retryable"]
    async with web.db.factory() as session:
        state = (await session.get(ChatQuestion, qid)).state
        assert state["retrieval_count"] == state["clarification_count"] == 0
    result = await web.client.post("/api/chat", json={"message": QUESTION, "session_id": "sessionA", "question_id": qid})
    assert result.status_code == 200 and result.json()["status"] == "NEEDS_CLARIFICATION"


@pytest.mark.parametrize("kind", ["agent_error_without_snapshot", "dependency_cancelled"])
async def test_untrusted_executor_exit_cannot_enable_unsafe_retry(db, kind):
    async def executor(data, checkpoint):
        if kind == "dependency_cancelled":
            raise asyncio.CancelledError()
        raise AgentError("no snapshot")

    runner = ChatRunner(db.store, executor)
    record, _ = await db.store.reserve(PersistentChatRequest(message=QUESTION), runner.worker_id)
    await asyncio.wait_for(runner._supervise(record["question_id"]), 3)
    record = await db.store.get_execution(record["question_id"])
    assert not record["retryable"] and record["error_code"] == "INVALID_SAVED_STATE"


@pytest.mark.parametrize("outcome", ["cancelled", "exception"])
async def test_new_attempt_can_start_after_previous_task_exits_abnormally(db, outcome):
    entered, release, executed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def previous():
        entered.set()
        await release.wait()
        if outcome == "cancelled":
            raise asyncio.CancelledError()
        raise RuntimeError("old attempt cleanup failed")

    runner = ChatRunner(db.store)
    old = asyncio.create_task(previous())
    await entered.wait()
    runner.tasks["q"] = old

    async def supervise(_):
        executed.set()

    runner._supervise = supervise
    runner.start("q")
    replacement = runner.tasks["q"]
    await asyncio.sleep(0)
    assert not executed.is_set()
    release.set()
    await asyncio.wait_for(replacement, 3)
    assert executed.is_set()


async def test_closing_new_attempt_while_waiting_old_task_does_not_restart(db):
    entered, exited = asyncio.Event(), asyncio.Event()

    async def previous():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            exited.set()

    runner = ChatRunner(db.store)
    old = asyncio.create_task(previous())
    await entered.wait()
    runner.tasks["q"] = old
    runner.start("q")
    replacement = runner.tasks["q"]
    await asyncio.sleep(0)
    await asyncio.wait_for(runner.close(), 3)
    assert exited.is_set() and old.cancelled() and replacement.cancelled()


async def test_unhandled_task_error_is_logged_and_forgotten(db, caplog):
    async def broken():
        raise RuntimeError("unexpected task failure")

    task = asyncio.create_task(broken())
    await asyncio.gather(task, return_exceptions=True)
    runner = ChatRunner(db.store)
    runner.tasks["q"] = task
    runner._forget("q", task)
    assert "q" not in runner.tasks
    assert "聊天后台任务异常结束" in caplog.text
