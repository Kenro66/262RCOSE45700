import asyncio
import threading

import pytest
from app.chat.persistent.cancellation import AgentStopped, call_until_stopped, stoppable_dependencies

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("synchronous", [False, True])
async def test_normal_dependency_result(synchronous):
    def sync_add(value):
        return value + 1

    async def async_add(value):
        return value + 1

    assert await call_until_stopped(sync_add if synchronous else async_add, asyncio.Event(), 2) == 3


async def test_stop_before_call_never_invokes_service():
    event = asyncio.Event()
    event.set()

    def forbidden():
        raise AssertionError("停止后不应启动服务")

    with pytest.raises(AgentStopped):
        await call_until_stopped(forbidden, event)


async def test_async_call_drains_before_reporting_stop():
    event, entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def service():
        entered.set()
        await release.wait()
        return "late result"

    task = asyncio.create_task(call_until_stopped(service, event))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        event.set()
        await asyncio.sleep(.02)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(AgentStopped):
        await asyncio.wait_for(task, 2)


@pytest.mark.parametrize("nested", [False, True])
async def test_sync_and_nested_thread_must_finish_before_stop(nested):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    event = asyncio.Event()

    def blocking():
        entered.set()
        release.wait(3)
        finished.set()
        return "result"

    async def nested_thread():
        return await asyncio.to_thread(blocking)

    task = asyncio.create_task(call_until_stopped(nested_thread if nested else blocking, event))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        event.set()
        await asyncio.sleep(.02)
        assert not task.done() and not finished.is_set()
    finally:
        release.set()
    with pytest.raises(AgentStopped):
        await asyncio.wait_for(task, 2)
    assert finished.is_set()


async def test_dependency_error_propagates_without_cancel():
    async def broken():
        raise ValueError("dependency failed")

    with pytest.raises(ValueError, match="dependency failed"):
        await call_until_stopped(broken, asyncio.Event())


async def test_stop_has_priority_over_late_dependency_error():
    event = asyncio.Event()

    async def broken():
        event.set()
        raise ValueError("late failure")

    with pytest.raises(AgentStopped):
        await call_until_stopped(broken, event)


async def test_sync_function_returning_coroutine_is_awaited():
    async def value():
        return 7

    def service():
        return value()

    assert await call_until_stopped(service, asyncio.Event()) == 7


async def test_dependency_wrappers_bind_each_original_and_keep_none():
    async def first():
        return "first"

    async def second():
        return "second"

    wrapped = stoppable_dependencies({"a": first, "b": second, "c": None}, asyncio.Event())
    assert await wrapped["a"]() == "first"
    assert await wrapped["b"]() == "second"
    assert wrapped["c"] is None


async def test_external_cancel_still_waits_for_inflight_cleanup():
    entered, release = asyncio.Event(), asyncio.Event()

    async def service():
        entered.set()
        await release.wait()

    task = asyncio.create_task(call_until_stopped(service, asyncio.Event()))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        await asyncio.sleep(.02)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)


async def test_success_does_not_leave_stop_waiter_running():
    before = set(asyncio.all_tasks())

    async def service():
        return "ok"

    await call_until_stopped(service, asyncio.Event())
    await asyncio.sleep(0)
    assert set(asyncio.all_tasks()) <= before
