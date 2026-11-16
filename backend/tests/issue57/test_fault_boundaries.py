"""Boundary cases that would otherwise silently lose state or use invalid sources."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app.chat.config import RagSettings
from app.chat.persistent.cancellation import AgentStopped
from app.chat.persistent.errors import ChatProblem
from app.chat.persistent.managed import execute_managed
from app.chat.persistent.runner import ChatRunner
from app.chat.persistent.schemas import PersistentChatRequest
from app.qa.agent.nodes import AgentError, Dependencies, Nodes
from app.qa.citation import Evidence
from app.qa.orchestrator import answer
from .conftest import hit
from .test_managed_flow import assessment, services as shared_services

pytestmark = pytest.mark.asyncio


@pytest.fixture
def services(monkeypatch):
    return shared_services.__wrapped__(monkeypatch)


async def test_source_outside_frozen_scope_poisoned_execution_is_unrecoverable(execution, services):
    services.hits = [hit(document="outside")]
    await execute_managed(SimpleNamespace(store=execution.store, worker_id="worker"), execution.data, execution.stop)
    view = await execution.store.get_execution(execution.qid)
    assert view["execution_status"] == "FAILED" and not view["retryable"]
    assert "single" not in services.tasks


async def test_bound_runner_rejects_newer_execution_before_dispatch(execution):
    runner = ChatRunner(execution.store)
    runner.worker_id = "worker"
    with pytest.raises(ChatProblem) as error:
        await runner._execute(execution.qid, {"execution_key": "old", "reliable": False}, asyncio.Event())
    assert error.value.code == "STALE_EXECUTION"
    assert (await execution.store.get_execution(execution.qid))["execution_status"] == "RUNNING"


async def test_legacy_unknown_mode_is_not_guessed_after_startup(database):
    database.store.rag_settings = None
    runner = ChatRunner(database.store)
    record, _ = await database.store.reserve(PersistentChatRequest(message="Q"), runner.worker_id)
    database.store.rag_settings = RagSettings("agent", 120)
    await runner._supervise(record["question_id"])
    result = await database.store.get_execution(record["question_id"])
    assert result["execution_status"] == "FAILED" and not result["retryable"]


async def test_generation_cancel_is_not_reclassified_as_shared_service_error(execution, services):
    services.generate_error = AgentStopped("stop")
    await execute_managed(SimpleNamespace(store=execution.store, worker_id="worker"), execution.data, execution.stop)
    result = await execution.store.get_execution(execution.qid)
    assert result["execution_status"] == "FAILED"
    assert not services.queries


@pytest.mark.parametrize("sufficient", [False, True])
async def test_partial_source_invalidation_reaudits_remaining_sources(sufficient):
    evidence = [Evidence("E1", hit()), Evidence("E2", hit("c2", "d2"))]
    tasks = []
    async def documents(ids):
        return {"d1": {"filename": "old.pdf", "status": "PROCESSING"},
                "d2": {"filename": "valid.pdf", "status": "READY"}}
    async def generate(messages):
        payload = json.loads(messages[-1]["content"])
        tasks.append(payload["task"])
        if payload["task"] == "check_evidence":
            value = assessment(("c2",))
            if not sufficient:
                value["verdict"] = "UNSUPPORTED"
        else:
            value = {"answer": "测试规则原文。[E2]", "insufficient_evidence": False}
        return json.dumps(value, ensure_ascii=False)
    async def retrieve(query):
        return []
    nodes = Nodes(Dependencies(retrieve, documents, generate), cached_evidence=list(evidence))
    state = dict(question="测试规则如何适用？", session_id="s", question_id="q", conditions={},
        current_query="Q", seen_queries=["Q"], retrieval_count=1, clarification_count=0,
        evidence=evidence, documents={}, sufficient=True, evidence_check_result={}, missing_conditions=[],
        status=None, answer=None, clarification=None, conflict=None, trace=[])
    result = await nodes.generate_answer(state)
    assert result["status"].value == ("ANSWERED" if sufficient else "INSUFFICIENT_EVIDENCE")
    assert tasks == (["check_evidence", "generate_answer"] if sufficient else ["check_evidence"])
    if sufficient:
        assert [item.evidence_id for item in result["evidence"]] == ["E2"]


async def test_changed_source_with_same_chunk_id_is_rejected_by_node_checkpoint():
    async def unused(*args):
        raise AssertionError("unexpected dependency")
    nodes = Nodes(Dependencies(unused, unused, unused), cached_evidence=[Evidence("E1", hit())])
    # A changed source arrives at a node boundary under an existing stable ID.
    state = {"trace": [], "status": "ANSWERED", "evidence": [Evidence("E1", hit(text="替换原文"))]}
    with pytest.raises(AgentError) as error:
        await nodes.tracked("finalize")(state)
    assert isinstance(error.value.__cause__, ValueError)
    assert nodes.cached_evidence[0].hit.text == "测试规则原文。"


async def test_fallback_completion_error_cannot_swallow_cancellation(execution, monkeypatch):
    from app.chat.persistent.context import SingleExecutionContext
    async def retrieve(query):
        return [hit()]
    async def complete(hits):
        execution.stop.set()
        raise OSError("completion failed after cancellation")
    async def forbidden(*args):
        pytest.fail("cancelled fallback must not lookup or generate")
    monkeypatch.setattr("app.qa.orchestrator.settings", lambda: {"EVIDENCE_COMPLETION_ENABLED": "true"})
    from app.chat.persistent.context import ExecutionStopped
    with pytest.raises(ExecutionStopped):
        await answer("测试规则如何适用？", retrieve_fn=retrieve, get_documents_fn=forbidden,
            generate_fn=forbidden, complete_evidence_fn=complete,
            execution_context=SingleExecutionContext({}, execution.progress, execution.guard))
