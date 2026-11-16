"""Exercise real guards; only external service and database faults are injected."""
import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.chat.persistent.context import ExecutionStopped, SingleExecutionContext
from app.chat.persistent.models import ChatRetrievalAttempt, ChatSubmission
from app.qa.evidence_completion import SupplementalEvidence
from .conftest import hit

pytestmark = pytest.mark.asyncio


async def test_guard_accepts_sync_async_and_sync_returning_awaitable(execution):
    async def async_value(value):
        return value + 1

    class AsyncCallable:
        async def __call__(self, value):
            return value + 2

    assert await execution.guard.call(lambda value: value * 2, 4) == 8
    assert await execution.guard.call(async_value, 4) == 5
    assert await execution.guard.call(AsyncCallable(), 4) == 6
    assert await execution.guard.call(lambda: async_value(4)) == 5
    async with execution.factory() as session:
        assert not (await session.get(ChatSubmission, execution.qid)).inflight_call


async def test_stop_event_and_poison_prevent_dispatch(execution):
    execution.stop.set()
    with pytest.raises(ExecutionStopped):
        await execution.guard.call(lambda: pytest.fail("must not dispatch"))
    await execution.guard.check(allow_stopped=True)
    assert execution.guard.reason == "CANCELLED"
    execution.stop.clear()
    execution.progress.poisoned = True
    with pytest.raises(ExecutionStopped, match="confirmed"):
        await execution.guard.check()


@pytest.mark.parametrize("fail_active", [True, False])
async def test_uncertain_database_write_poison_progress(execution, monkeypatch, fail_active):
    original = execution.store.mark_call

    async def faulty_mark(qid, key, active):
        if active == fail_active:
            raise OSError("database unavailable")
        return await original(qid, key, active)

    monkeypatch.setattr(execution.store, "mark_call", faulty_mark)
    with pytest.raises(OSError):
        await execution.guard.call(lambda: "value")
    assert execution.progress.poisoned
    with pytest.raises(ExecutionStopped):
        await execution.guard.check()


async def test_task_cancellation_waits_for_inflight_service(execution):
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def service():
        entered.set()
        await release.wait()
        finished.set()
        return "late"

    task = asyncio.create_task(execution.guard.call(service))
    await asyncio.wait_for(entered.wait(), 3)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and not finished.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 3)
    assert finished.is_set() and execution.progress.poisoned
    async with execution.factory() as session:
        assert not (await session.get(ChatSubmission, execution.qid)).inflight_call


async def test_retrieval_failure_spends_budget_and_preserves_failure_ledger(execution):
    async def broken(query):
        raise RuntimeError("retrieval unavailable")

    runtime = SimpleNamespace(retrieve=broken)
    with pytest.raises(RuntimeError):
        await execution.guard.dependencies(runtime)["retrieve_fn"]("original")
    assert execution.progress.state.retrieval_count == 1
    assert execution.progress.state.retrieval_queries == ["original"]
    async with execution.factory() as session:
        attempt = await session.get(ChatRetrievalAttempt, (execution.qid, 1))
        assert attempt.outcome == "FAILED"


async def test_dependency_results_preserve_raw_sources_and_conditions(execution, monkeypatch):
    async def retrieve(query):
        return [hit()]

    async def documents(ids):
        return {key: {"status": "READY"} for key in ids}

    async def complete(hits):
        return []

    async def generate(messages):
        return messages[-1]["content"]

    monkeypatch.setattr("app.adapters.generation.generate_text", generate)
    dependencies = execution.guard.dependencies(SimpleNamespace(retrieve=retrieve, documents=documents, complete=complete))
    assert (await dependencies["retrieve_fn"]("Q"))[0].text == "测试规则原文。"
    assert await dependencies["get_documents_fn"](["d1"]) == {"d1": {"status": "READY"}}
    assert await dependencies["complete_evidence_fn"]([hit()]) == []
    assert await dependencies["generate_fn"]([{"role": "user", "content": "hello"}]) == "hello"
    context = SingleExecutionContext({"country": "KR"}, execution.progress, execution.guard)
    evidence = await context.evidence([hit("c2")])
    assert [item.evidence_id for item in evidence] == ["E1", "E2"]
    await context.before_generation()
    assert '"country": "KR"' in context.messages([])[0]["content"]
    async with execution.factory() as session:
        assert (await session.scalar(select(ChatRetrievalAttempt))).outcome == "SUCCEEDED"


@pytest.mark.parametrize("result", [None, [object()], [object(), object()]])
async def test_malformed_completion_cannot_become_trusted_evidence(execution, result):
    async def complete(hits):
        return result
    with pytest.raises(ValueError, match="completion"):
        await execution.guard.dependencies(SimpleNamespace(complete=complete))["complete_evidence_fn"]([hit()])


async def test_valid_completion_is_saved_before_later_stop(execution):
    # SupplementalEvidence is the actual service boundary type, not a partial mock.
    from app.contracts import Chunk
    supplemental = SupplementalEvidence(chunk=Chunk(chunk_id="supplement", document_id="d1", page_number=1,
        text="补充原文", language="zh", chunk_index=1, page_index=0, section_title=None,
        token_count=4, char_count=4, content_hash=None), related_chunk_id="c1", signals=("definition",))
    async def complete(hits):
        execution.stop.set()
        return [supplemental]
    with pytest.raises(ExecutionStopped):
        await execution.guard.dependencies(SimpleNamespace(complete=complete))["complete_evidence_fn"]([hit()])
    assert execution.progress.sources[0].chunk_id == "supplement"
