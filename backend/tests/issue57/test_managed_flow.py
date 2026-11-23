"""使用结果可控的外部服务，验证真实智能体、单次问答、存储和网络接口流程。"""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.chat.config import RagSettings
from app.chat.persistent.api import build_persistent_chat_router
from app.chat.persistent.cancellation import AgentStopped
from app.chat.persistent.context import ExecutionStopped
from app.chat.persistent.fallback import maybe_fallback
from app.chat.persistent.managed import execute_managed
from app.chat.persistent.models import ChatQuestion, ChatRetrievalAttempt, ChatSubmission
from app.chat.persistent.runner import ChatRunner
from app.chat.persistent.schemas import PersistentChatRequest, PersistentChatResponse
from app.contracts import ChatResponse, ChatStatus
from app.adapters.generation import generate_text as provider_generate_text
from app.qa.agent.nodes import AgentError, AgentErrorType
from app.storage.models import Document
from .conftest import hit

pytestmark = pytest.mark.asyncio


def assessment(ids=("c1",)):
    support = [{"chunk_id": item} for item in ids]
    supported = dict(status="supported", affects_core=True, reason="原文支持", support=support)
    optional = dict(status="not_required", affects_core=False, reason="不影响结论", support=[])
    return dict(verdict="SUFFICIENT", core_claim="测试规则", core_support=support,
                definitions=deepcopy(optional), exceptions=deepcopy(optional),
                applicability=deepcopy(supported), complete_answer=deepcopy(supported), missing_evidence=[])


@pytest.fixture
def services(monkeypatch):
    control = SimpleNamespace(tasks=[], queries=[], fallback_messages=[], hits=[hit()],
        invalid_task=None, retrieve_error=None, generate_error=None, completion_error=None,
        final_insufficient=False, ready=True, stop=None, poisoned=False, block=False,
        entered=asyncio.Event(), release=asyncio.Event(), documents_calls=0, invalidate_at=None)

    class ExternalRAG:
        def __init__(self, question, *, conditions, session_factory, document_ids):
            self.document_ids = document_ids
            self.conditions = conditions
            self.factory = session_factory

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def retrieve(self, query):
            control.queries.append(query)
            if control.block:
                control.entered.set()
                await control.release.wait()
            if control.retrieve_error:
                raise control.retrieve_error
            if control.stop:
                control.stop.set()
            return control.hits

        async def documents(self, ids):
            from app.storage.repo import get_documents
            control.documents_calls += 1
            if control.invalidate_at == control.documents_calls:
                async with self.factory() as session, session.begin():
                    (await session.get(Document, "d1")).status = "PROCESSING"
            return await get_documents(ids, session_factory=self.factory)

        async def complete(self, hits):
            if control.completion_error:
                raise control.completion_error
            return []

    async def generate(messages):
        if control.generate_error:
            raise control.generate_error
        try:
            data = json.loads(messages[-1]["content"])
            task = data["task"]
        except (ValueError, KeyError):
            task, data = "single", {}
            control.fallback_messages.append(messages)
        control.tasks.append(task)
        if task == control.invalid_task:
            return "not json"
        if task in {"check_conditions", "check_applicability"}:
            payload = {"missing_conditions": []}
        elif task == "check_evidence":
            payload = assessment(tuple(item.chunk_id for item in control.hits))
        elif task == "check_conflict":
            payload = {"conflict": None}
        elif task == "rewrite_query":
            payload = {"query": "改写查询"}
        else:
            payload = {"answer": "测试规则原文。[E1]", "insufficient_evidence": control.final_insufficient}
        return json.dumps(payload, ensure_ascii=False)

    monkeypatch.setattr("app.chat.persistent.managed.RealRAG", ExternalRAG)
    monkeypatch.setattr("app.chat.persistent.single_adapter.RealRAG", ExternalRAG)
    monkeypatch.setattr("app.adapters.generation.generate_text", generate)
    monkeypatch.setattr("app.qa.orchestrator.settings", lambda: {"EVIDENCE_COMPLETION_ENABLED": "true"})
    monkeypatch.setattr("app.qa.agent.nodes.settings", lambda: {"EVIDENCE_COMPLETION_ENABLED": "true"}, raising=False)
    return control


async def run(execution):
    await execute_managed(SimpleNamespace(store=execution.store, worker_id="worker"), execution.data, execution.stop)
    return await execution.store.get_execution(execution.qid)


async def test_agent_success_preserves_evidence_and_never_fallback(execution, services):
    view = await run(execution)
    assert view["response"]["status"] == "ANSWERED"
    assert view["response"]["citations"][0]["quote"] == "测试规则原文。"
    assert "single" not in services.tasks
    async with execution.factory() as session:
        question = await session.get(ChatQuestion, execution.qid)
        assert question.retrieval_used == 1 and question.state_reliable
        assert not (await session.get(ChatSubmission, execution.qid)).fallback_started


@pytest.mark.parametrize("failure", [None, "workflow", "shared"])
async def test_managed_native_agent_preserves_controlled_single_routing(execution, services, monkeypatch, failure):
    from app.adapters.generation import GenerationError, generate_text
    requests = []
    config = {"AGENT_GENERATION_MODEL": "gemma", "AGENT_GENERATION_BASE_URL": "https://gemma.invalid/v1",
              "AGENT_GENERATION_JSON_SCHEMA_ENABLED": "true"}
    monkeypatch.setattr("app.adapters.config.settings", lambda: config)
    def native(messages, **kwargs):
        requests.append(kwargs)
        if failure == "shared":
            raise GenerationError("Synthetic service HTTP 503")
        return generate_text(messages)
    monkeypatch.setattr("app.qa.agent.generation.generate_text", native)
    if failure == "workflow":
        services.invalid_task = "check_evidence"
    view = await run(execution)
    assert requests and all(item["provider_config"]["GENERATION_MODEL"] == "gemma" for item in requests)
    assert all(item["response_format"]["json_schema"]["strict"] is True for item in requests)
    if failure == "shared":
        assert view["execution_status"] == "FAILED"
        assert "single" not in services.tasks
    else:
        assert view["response"]["status"] == "ANSWERED"
        assert ("single" in services.tasks) is (failure == "workflow")
    async with execution.factory() as session:
        submission = await session.get(ChatSubmission, execution.qid)
        assert submission.fallback_started is (failure == "workflow")


@pytest.mark.parametrize("profile", ["furiosa_gemma", "legacy"])
@pytest.mark.parametrize("workflow_failure", [False, True])
async def test_http_native_profile_fallback_uses_single_messages_and_same_provider(
        database, services, monkeypatch, profile, workflow_failure):
    """Real HTTP routing/graph/store/adapter; only external IO is Fake/Mock."""
    import httpx
    from app.adapters import config, generation
    raw = {"GENERATION_PROFILE": profile, "FURIOSA_API_KEY": "synthetic-furiosa",
           "GENERATION_MODEL": "synthetic-legacy", "GENERATION_BASE_URL": "https://legacy.invalid/v1",
           "GENERATION_API_KEY": "synthetic-legacy-key", "GENERATION_TIMEOUT_SECONDS": "3",
           "AGENT_GENERATION_JSON_SCHEMA_ENABLED": "true"}
    calls = []
    client_type = httpx.Client
    expected_model = config.GEMMA_MODEL if profile == "furiosa_gemma" else raw["GENERATION_MODEL"]
    expected_base = config.GEMMA_BASE_URL if profile == "furiosa_gemma" else raw["GENERATION_BASE_URL"]

    def respond(request):
        payload = json.loads(request.content)
        assert str(request.url) == expected_base + "/chat/completions"
        assert payload["model"] == expected_model
        try:
            data = json.loads(payload["messages"][-1]["content"])
            task = data["task"]
        except (ValueError, KeyError):
            task = "single"
        calls.append((task, payload))
        if task == "single":
            assert "response_format" not in payload
            assert "Confirmed applicability conditions" in payload["messages"][-1]["content"]
            content = json.dumps({"answer": "测试规则原文。[E1]", "insufficient_evidence": False})
        else:
            assert payload["response_format"]["json_schema"]["strict"] is True
            if task == "check_evidence":
                assert payload["response_format"]["json_schema"]["schema"]["$defs"]["EvidenceSupport"]["properties"]["chunk_id"]["enum"] == ["c1"]
                content = "not json" if workflow_failure else json.dumps(assessment())
            elif task in ("check_conditions", "check_applicability"):
                content = '{"missing_conditions":[]}'
            elif task == "check_conflict":
                content = '{"conflict":null}'
            else:
                assert task == "generate_answer"
                content = json.dumps({"answer": "测试规则原文。[E1]", "insufficient_evidence": False})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    monkeypatch.setattr(config, "settings", lambda: raw)
    monkeypatch.setattr(generation, "settings", lambda: raw)
    monkeypatch.setattr(generation, "generate_text", provider_generate_text)
    monkeypatch.setattr("app.qa.agent.generation.generate_text", provider_generate_text)
    monkeypatch.setattr(generation.httpx, "Client", lambda **kw: client_type(transport=httpx.MockTransport(respond), **kw))
    runner = ChatRunner(database.store)
    app = FastAPI()
    app.include_router(build_persistent_chat_router(database.store, runner), prefix="/api")
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/chat", json={"message": "测试规则如何适用？"})
    finally:
        await runner.close()
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["execution_status"] == "SUCCEEDED" and body["status"] == "ANSWERED"
    assert body["citations"][0]["evidence_id"] == "E1"
    assert body["citations"][0]["quote"] == "测试规则原文。"
    assert sum(task == "single" for task, _ in calls) == int(workflow_failure)
    async with database.factory() as session:
        submission = await session.scalar(select(ChatSubmission).where(ChatSubmission.question_id == body["question_id"]))
        question = await session.get(ChatQuestion, body["question_id"])
        assert submission.fallback_started is workflow_failure
        assert question.retrieval_used == 1 + int(workflow_failure) <= 3
        assert question.state["clarification_count"] == 0


@pytest.mark.parametrize("insufficient", [False, True])
async def test_workflow_error_fallback_shares_budget_conditions_and_sources(execution, services, insufficient):
    services.invalid_task = "check_evidence"
    services.final_insufficient = insufficient
    # 模拟先前已确认、并由 #68 保存的条件。
    async with execution.factory() as session, session.begin():
        question = await session.get(ChatQuestion, execution.qid)
        question.state = {**question.state, "conditions": {"country": "KR"}}
    execution.data = await execution.store.execution_input(execution.qid, "worker")
    view = await run(execution)
    assert view["execution_status"] == "SUCCEEDED"
    assert view["response"]["status"] == ("INSUFFICIENT_EVIDENCE" if insufficient else "ANSWERED")
    assert len(services.queries) == 2 and '"country": "KR"' in services.queries[-1]
    assert '"country": "KR"' in services.fallback_messages[0][-1]["content"]
    async with execution.factory() as session:
        question = await session.get(ChatQuestion, execution.qid)
        assert question.retrieval_used == 2 and question.state["evidence_ids"] == ["E1"]
        attempts = list((await session.scalars(select(ChatRetrievalAttempt).order_by(ChatRetrievalAttempt.ordinal))).all())
        assert [a.source for a in attempts] == ["AGENT", "FALLBACK"]
        assert (await session.get(ChatSubmission, execution.qid)).fallback_started


@pytest.mark.parametrize("failure", ["retrieve", "generate", "unknown", "stopped"])
async def test_shared_and_control_errors_never_fallback(execution, services, failure, monkeypatch):
    if failure == "retrieve":
        services.retrieve_error = OSError("service offline")
    elif failure == "generate":
        services.generate_error = OSError("service offline")
    elif failure == "stopped":
        services.retrieve_error = AgentStopped("cancelled")
    else:
        async def unknown(*args, **kwargs):
            raise RuntimeError("unknown")
        monkeypatch.setattr("app.chat.persistent.managed.agent_answer", unknown)
    view = await run(execution)
    assert view["execution_status"] == "FAILED"
    assert view["response"]["status"] == "SYSTEM_ERROR"
    assert "single" not in services.tasks


async def test_no_documents_returns_existing_business_response(execution, services):
    execution.data["document_ids"] = []
    view = await run(execution)
    assert view["response"]["status"] == "NO_READY_DOCUMENTS" and not services.queries


async def test_cancel_after_retrieval_keeps_source_without_fallback(execution, services):
    services.stop = execution.stop
    view = await run(execution)
    assert view["execution_status"] == "CANCELLED" and "single" not in services.tasks
    async with execution.factory() as session:
        state = (await session.get(ChatQuestion, execution.qid)).state
        assert state["retrieval_count"] == 1 and state["evidence"][0]["hit"]["text"] == "测试规则原文。"


async def test_expired_execution_never_calls_service(execution, services):
    async with execution.factory() as session, session.begin():
        (await session.get(ChatSubmission, execution.qid)).deadline_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
    view = await run(execution)
    assert view["response"]["status"] == "SYSTEM_ERROR" and not services.tasks and not services.queries


async def test_no_trusted_error_snapshot_never_fallback(execution, services, monkeypatch):
    async def fail(*args, **kwargs):
        raise AgentError("no snapshot", error_type=AgentErrorType.AGENT_WORKFLOW_ERROR)
    monkeypatch.setattr("app.chat.persistent.managed.agent_answer", fail)
    view = await run(execution)
    assert view["execution_status"] == "FAILED" and not services.queries


async def test_untrusted_snapshot_is_not_saved_as_retryable(execution, services, monkeypatch):
    original = execution.store.checkpoint
    async def fail(*args, **kwargs):
        raise OSError("checkpoint unavailable")
    monkeypatch.setattr(execution.store, "checkpoint", fail)
    view = await run(execution)
    assert view["execution_status"] == "FAILED" and not view["retryable"]
    monkeypatch.setattr(execution.store, "checkpoint", original)


@pytest.mark.parametrize("stage", [1, 2])
async def test_invalidated_document_cannot_be_returned_as_answer(execution, services, stage):
    services.invalidate_at = stage
    view = await run(execution)
    assert view["response"]["status"] == "INSUFFICIENT_EVIDENCE"
    assert "generate_answer" not in services.tasks


async def test_document_invalidated_during_generation_blocks_saved_answer(execution, services, monkeypatch):
    from app.adapters.generation import generate_text
    async def invalidate(messages):
        value = await generate_text(messages)
        if services.tasks[-1] == "generate_answer":
            async with execution.factory() as session, session.begin():
                (await session.get(Document, "d1")).status = "PROCESSING"
        return value
    monkeypatch.setattr("app.adapters.generation.generate_text", invalidate)
    view = await run(execution)
    assert view["response"]["status"] == "INSUFFICIENT_EVIDENCE"


@pytest.mark.parametrize("kind", ["exception", "unknown", "stopped", "poisoned", "gate"])
async def test_fallback_gate_never_dispatches_on_disallowed_input(execution, kind):
    error = AgentError("workflow", error_type=AgentErrorType.AGENT_WORKFLOW_ERROR)
    if kind == "exception":
        error = RuntimeError("arbitrary")
    elif kind == "unknown":
        error = AgentError("unknown")
    elif kind == "stopped":
        error = ExecutionStopped("stop")
    elif kind == "poisoned":
        execution.progress.poisoned = True
    # 回退检查：刚占用执行权的状态尚未被确认为可信。
    assert await maybe_fallback(error, execution.data, execution.store, execution.progress, execution.guard) is None


@pytest.mark.parametrize("outcome", ["answer", "empty", "error", "cancel"])
async def test_normal_single_preserves_original_behavior(database, monkeypatch, outcome):
    database.store.rag_settings = RagSettings("single", 120)
    row, _ = await database.store.reserve(PersistentChatRequest(message="Q"), "worker")
    data = await database.store.execution_input(row["question_id"], "worker")
    async def ready(**kwargs):
        return outcome != "empty"
    async def answer(question, session):
        if outcome == "error":
            raise RuntimeError("service failed")
        if outcome == "cancel":
            raise AgentStopped("stop")
        return ChatResponse(status=ChatStatus.INSUFFICIENT_EVIDENCE, answer="原 Single 拒答")
    monkeypatch.setattr("app.chat.persistent.managed.has_ready_documents", ready)
    monkeypatch.setattr("app.chat.runtime.answer_question", answer)
    await execute_managed(SimpleNamespace(store=database.store, worker_id="worker"), data, asyncio.Event())
    view = await database.store.get_execution(row["question_id"])
    if outcome == "answer":
        parsed = PersistentChatResponse.model_validate(view["response"])
        assert parsed.answer == "原 Single 拒答" and parsed.question_id == row["question_id"]
    elif outcome == "empty":
        assert view["response"]["status"] == "NO_READY_DOCUMENTS"
    else:
        assert view["execution_status"] == ("FAILED" if outcome == "error" else "CANCELLED")
        assert view["retryable"]


async def test_http_runs_managed_mode_without_exposing_execution_token(database, services):
    runner = ChatRunner(database.store)
    app = FastAPI()
    app.include_router(build_persistent_chat_router(database.store, runner), prefix="/api")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/chat", json={"message": "测试规则如何适用？"})
    await runner.close()
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ANSWERED" and "run_id" not in body and "execution_key" not in body


async def test_initial_database_read_failure_does_not_leave_running(database, services, monkeypatch):
    runner = ChatRunner(database.store)
    row, _ = await database.store.reserve(PersistentChatRequest(message="Q"), runner.worker_id)
    async def fail(*args):
        raise OSError("database read failed")
    monkeypatch.setattr(database.store, "execution_input", fail)
    runner.start(row["question_id"], execution_key=row.run_id)
    result = await asyncio.wait_for(runner.wait(row["question_id"], execution_key=row.run_id), 3)
    assert result["execution_status"] == "FAILED" and result["response"]["status"] == "SYSTEM_ERROR"
    await runner.close()
