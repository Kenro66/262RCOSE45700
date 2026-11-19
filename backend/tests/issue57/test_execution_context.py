"""验证真实执行保护逻辑；仅注入外部服务与数据库故障。"""
import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.chat.persistent.context import ExecutionStopped, SingleExecutionContext
from app.chat.persistent.models import ChatRetrievalAttempt, ChatSubmission
from app.qa.evidence_completion import SupplementalEvidence
from .conftest import hit

pytestmark = pytest.mark.asyncio


async def test_http_guard_routes_agent_profile_but_not_single_fallback(execution, monkeypatch):
    from unittest.mock import Mock
    agent = Mock(return_value="agent-result")
    single = Mock(return_value="single-result")
    builder = Mock(return_value=agent)
    monkeypatch.setattr("app.qa.agent.generation.agent_generator", builder)
    monkeypatch.setattr("app.adapters.generation.generate_text", single)
    runtime = SimpleNamespace()
    message = [{"content": "synthetic"}]
    assert await execution.guard.dependencies(runtime)["generate_fn"](message) == "agent-result"
    assert await execution.guard.dependencies(runtime, "FALLBACK")["generate_fn"](message) == "single-result"
    assert builder.call_count == 1
    agent.assert_called_once_with(message)
    single.assert_called_once_with(message)
    assert execution.progress.state.retrieval_count == 0
    execution.stop.set()
    with pytest.raises(ExecutionStopped):
        await execution.guard.dependencies(runtime)["generate_fn"](message)
    assert agent.call_count == 1


async def test_shared_gemma_profile_keeps_http_agent_and_flow_fallback_on_gemma(execution, monkeypatch):
    import json
    import httpx
    from app.adapters import config, generation
    from tests.test_generation import gemma_profile
    calls = []
    client = httpx.Client
    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        assert str(request.url) == config.GEMMA_BASE_URL + "/chat/completions"
        assert body["model"] == config.GEMMA_MODEL and body["max_tokens"] == 2048
        content = '{"missing_conditions":[]}' if "response_format" in body else '{"answer":"测试。[E1]","insufficient_evidence":false}'
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
    monkeypatch.setattr(config, "settings", gemma_profile)
    monkeypatch.setattr(generation, "settings", gemma_profile)
    monkeypatch.setattr(generation.httpx, "Client", lambda **kw: client(transport=httpx.MockTransport(respond), **kw))
    runtime = SimpleNamespace()
    envelope = [{"role": "user", "content": json.dumps({"task": "check_conditions", "evidence": []})}]
    result = await execution.guard.dependencies(runtime)["generate_fn"](envelope)
    assert json.loads(result) == {"missing_conditions": []}
    result = await execution.guard.dependencies(runtime, "FALLBACK")["generate_fn"]([{"role": "user", "content": "synthetic"}])
    assert json.loads(result)["answer"] == "测试。[E1]"
    assert len(calls) == 2 and "response_format" in calls[0] and "response_format" not in calls[1]
    assert execution.progress.state.retrieval_count == 0


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
    # 使用服务实际返回的 SupplementalEvidence 类型，避免使用不完整的替身。
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
