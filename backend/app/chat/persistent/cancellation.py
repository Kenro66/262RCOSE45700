import asyncio
import inspect
from contextlib import suppress
from functools import wraps
from app.qa.agent.nodes import AgentError


class AgentStopped(AgentError):
    pass


async def call_until_stopped(function, stop_event, *args, **kwargs):
    if stop_event.is_set():
        raise AgentStopped("用户已申请停止执行")
    asynchronous = inspect.iscoroutinefunction(function) or inspect.iscoroutinefunction(getattr(function, "__call__", None))

    async def invoke():
        result = await function(*args, **kwargs) if asynchronous else await asyncio.to_thread(function, *args, **kwargs)
        return await result if inspect.isawaitable(result) else result

    operation = asyncio.create_task(invoke())
    stopping = asyncio.create_task(stop_event.wait())
    try:
        await asyncio.wait({operation, stopping}, return_when=asyncio.FIRST_COMPLETED)
        if stop_event.is_set():
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.shield(operation)  # 异步检索内部也可能含线程，统一等待当前调用结束，避免提前确认停止。
            raise AgentStopped("用户已申请停止执行")
        return await operation
    finally:
        stopping.cancel()
        with suppress(asyncio.CancelledError):
            await stopping
        if not operation.done():
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.shield(operation)


def stoppable_dependencies(dependencies, stop_event):  # 包装现有注入入口，不新增 Agent 函数参数。
    def decorate(function):
        @wraps(function)
        async def wrapped(*args, **kwargs):
            return await call_until_stopped(function, stop_event, *args, **kwargs)
        return wrapped
    return {name: decorate(function) if function is not None else None for name, function in dependencies.items()}
