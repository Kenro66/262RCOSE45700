"""Only explicit Agent eligibility can enter the atomic fallback gate."""

from app.qa.agent.nodes import AgentError

from .context import ExecutionStopped
from .single_adapter import execute_single_fallback


async def maybe_fallback(error, data, store, progress, guard):
    if (not isinstance(error, AgentError) or isinstance(error, ExecutionStopped)
            or getattr(error, "fallback_eligible", None) is not True or progress.poisoned):
        return None
    await guard.check()
    if not await store.begin_fallback(data["question_id"], data["run_id"], progress.state):
        return None
    # Deliberately no catch/retry here: another Single failure ends this execution.
    return await execute_single_fallback(data, store, progress, guard)
