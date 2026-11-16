"""只有明确允许回退的智能体错误才能进入原子回退检查。"""

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
    # 此处不捕获异常或重试；单次问答再次失败即结束本轮执行。
    return await execute_single_fallback(data, store, progress, guard)
