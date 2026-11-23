"""后台异常退出必须结束等待，同时保护仍在执行的任务和原有预算。"""
import asyncio

import pytest

from app.chat.persistent.models import ChatQuestion
from app.chat.persistent.runner import ChatRunner
from app.chat.persistent.schemas import PersistentChatRequest
from app.qa.agent.state import AgentChatResponse, AgentResult, AgentState
from app.qa.agent.nodes import AgentError, AgentErrorType
from app.chat.persistent.cancellation import AgentStopped
from .conftest import QUESTION, question_url, response

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("exit_kind", ["missing", "failed", "cancelled"])
async def test_local_orphan_finishes_instead_of_waiting_forever(db, exit_kind):
    runner = ChatRunner(db.store)
    record, _ = await db.store.reserve(PersistentChatRequest(message=QUESTION), runner.worker_id)
    if exit_kind != "missing":
        async def broken(_):
            if exit_kind == "cancelled":
                raise asyncio.CancelledError()
            raise ConnectionError("cannot save final state")
        runner._supervise = broken
        runner.start(record["question_id"])
        await asyncio.gather(runner.tasks[record["question_id"]], return_exceptions=True)
        await asyncio.sleep(0)
    result = await asyncio.wait_for(runner.wait(record["question_id"]), 1)
    assert result["execution_status"] == "FAILED"
    assert result["error_code"] == "INVALID_SAVED_STATE" and not result["retryable"]
    assert result["response"]["question_id"] == record["question_id"]
    assert "_worker_id" not in result
    async with db.factory() as session:
        assert (await session.get(ChatQuestion, record["question_id"])).status == "UNRECOVERABLE"


async def test_failure_handler_does_not_read_database_again_for_known_id(db, monkeypatch):
    runner = ChatRunner(db.store)
    record, _ = await db.store.reserve(PersistentChatRequest(message=QUESTION), runner.worker_id)
    original_get = db.store.get_execution

    async def failed_execute(*_):
        raise ConnectionError("read failed")

    async def monitor(*_):
        await asyncio.Event().wait()

    async def failed_read(*args, **kwargs):
        pytest.fail("Error handling must use the question_id it already has")

    monkeypatch.setattr(runner, "_execute", failed_execute)
    monkeypatch.setattr(runner, "_watch_cancel", monitor)
    monkeypatch.setattr(db.store, "get_execution", failed_read)
    await runner._supervise(record["question_id"])
    result = await original_get(record["question_id"])
    assert result["execution_status"] == "FAILED" and not result["retryable"]


async def test_other_worker_active_request_is_not_abandoned(db):
    runner = ChatRunner(db.store)
    record, _ = await db.store.reserve(PersistentChatRequest(message=QUESTION), "other-worker")
    waiter = asyncio.create_task(runner.wait(record["question_id"]))
    try:
        await asyncio.sleep(.3)
        assert not waiter.done()
        assert (await db.store.get_execution(record["question_id"]))["execution_status"] == "RUNNING"
        await db.store.finish(record["question_id"], "other-worker", "SUCCEEDED", response(record["question_id"]))
        assert (await asyncio.wait_for(waiter, 1))["execution_status"] == "SUCCEEDED"
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


async def test_final_state_is_reread_before_repairing_stale_snapshot(db, monkeypatch):
    runner = ChatRunner(db.store)
    record, _ = await db.store.reserve(PersistentChatRequest(message=QUESTION), runner.worker_id)
    original_get = db.store.get_execution
    first = True

    async def finish_after_read(*args, **kwargs):
        nonlocal first
        snapshot = await original_get(*args, **kwargs)
        if first:
            first = False
            await db.store.finish(record["question_id"], runner.worker_id, "SUCCEEDED", response(record["question_id"]))
        return snapshot

    monkeypatch.setattr(db.store, "get_execution", finish_after_read)
    result = await runner.wait(record["question_id"])
    assert result["execution_status"] == "SUCCEEDED" and result["error_code"] is None


async def test_claim_and_start_are_atomic_for_waiters(db):
    async def execute(data, checkpoint):
        return AgentResult(state=AgentState.model_validate(data["state"]), response=AgentChatResponse(
            status="INSUFFICIENT_EVIDENCE", question_id=data["question_id"]))

    runner = ChatRunner(db.store, execute)
    waiter = None
    try:
        async with runner.claim_lock:
            record, _ = await db.store.reserve(PersistentChatRequest(message=QUESTION), runner.worker_id)
            waiter = asyncio.create_task(runner.wait(record["question_id"]))
            await asyncio.sleep(.05)
            assert not waiter.done()
            runner.start(record["question_id"])
        result = await asyncio.wait_for(waiter, 2)
        assert result["execution_status"] == "SUCCEEDED"
    finally:
        if waiter is not None:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        await runner.close()


async def test_status_endpoint_recovers_dead_task_without_leaking_worker(web):
    record, _ = await web.store.reserve(PersistentChatRequest(message=QUESTION, session_id="sessionA"), web.runner.worker_id)
    result = await asyncio.wait_for(web.client.get(question_url(record["question_id"]) + "/execution"), 2)
    assert result.status_code == 200
    body = result.json()
    assert body["execution_status"] == "FAILED" and not body["retryable"]
    assert body["response"]["status"] == "SYSTEM_ERROR"
    assert "_worker_id" not in body and "cancel_requested" not in body


async def test_storage_still_unavailable_returns_error_instead_of_hanging(web, monkeypatch):
    record, _ = await web.store.reserve(PersistentChatRequest(message=QUESTION, session_id="sessionA"), web.runner.worker_id)

    async def unavailable(*args, **kwargs):
        raise ConnectionError("database unavailable")

    monkeypatch.setattr(web.store, "finish", unavailable)
    result = await asyncio.wait_for(web.client.get(question_url(record["question_id"]) + "/execution"), 2)
    assert result.status_code == 500 and result.json()["error_code"] == "SYSTEM_ERROR"


@pytest.mark.parametrize("error_type", [AgentErrorType.SHARED_RETRIEVAL_ERROR, AgentErrorType.SHARED_GENERATION_ERROR])
async def test_explicit_stop_survives_agent_dependency_error_wrapping(db, error_type):
    async def execute(data, checkpoint):
        data["stop_event"].set()
        error = AgentError("依赖边界重新包装取消异常", error_type=error_type)
        error.state = AgentState.model_validate(data["state"])
        raise error from AgentStopped("主动取消")

    runner = ChatRunner(db.store, execute)
    record, _ = await db.store.reserve(PersistentChatRequest(message=QUESTION), runner.worker_id)
    progress = {"reliable": False}
    await runner._execute(record["question_id"], progress, asyncio.Event())
    saved = await db.store.get_execution(record["question_id"])
    assert saved["execution_status"] == "CANCELLED"
    assert saved["error_code"] == "CANCELLED" and saved["retryable"]
    assert saved["response"] is None and progress["reliable"]


async def test_supervisor_does_not_interrupt_inflight_cancel_read(db, monkeypatch):
    runner = ChatRunner(db.store)
    reading = asyncio.Event()
    allow_read_to_finish = asyncio.Event()

    async def read_execution(_question_id):
        reading.set()
        await allow_read_to_finish.wait()
        return {"cancel_requested": False}

    async def complete_execution(*_args):
        await reading.wait()

    monkeypatch.setattr(db.store, "get_execution", read_execution)
    monkeypatch.setattr(runner, "_execute", complete_execution)
    task = asyncio.create_task(runner._supervise("question"))
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), 0.05)
    finally:
        allow_read_to_finish.set()
        await asyncio.wait_for(task, 1)
