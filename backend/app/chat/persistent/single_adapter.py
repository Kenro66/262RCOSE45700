"""绑定只检索一次的回退流程，保持普通单次问答的调用逻辑不变。"""

from app.contracts import ChatStatus
from app.qa.agent.runtime import RealRAG
from app.qa.agent.state import AgentChatResponse, AgentResult
from app.qa.orchestrator import answer

from .context import SingleExecutionContext


async def execute_single_fallback(data, store, progress, guard):
    conditions = progress.state.conditions
    async with RealRAG(data["question"], conditions=conditions, session_factory=store.factory,
                       document_ids=data["document_ids"]) as runtime:
        dependencies = guard.dependencies(runtime, "FALLBACK")
        retrieve = dependencies["retrieve_fn"]

        async def retrieve_with_conditions(question):
            import json
            query = question + ("\nConditions: " + json.dumps(conditions, ensure_ascii=False, sort_keys=True) if conditions else "")
            return await retrieve(query)

        dependencies["retrieve_fn"] = retrieve_with_conditions
        response = await answer(data["question"], data["session_id"], **dependencies,
                                execution_context=SingleExecutionContext(conditions, progress, guard))
        await guard.check()
        state = progress.snapshot(progress.state)
        payload = response.model_dump()
        if response.status == ChatStatus.INSUFFICIENT_EVIDENCE:
            payload["message"] = response.message or response.answer
            payload["answer"] = None
        return AgentResult(response=AgentChatResponse(**payload, question_id=data["question_id"]), state=state)
