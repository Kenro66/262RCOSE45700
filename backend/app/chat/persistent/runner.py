import asyncio
import logging
from contextlib import suppress
from uuid import uuid4
from typing import cast
from app.contracts import ChatStatus
from app.qa.agent.runtime import RealRAG
from app.qa.agent.nodes import AgentError
from app.qa.agent.service import agent_answer
from app.qa.agent.state import AgentChatResponse, AgentResult, AgentState, normalize_agent_state
from app.storage.repo import has_ready_documents
from .cancellation import AgentStopped, call_until_stopped, stoppable_dependencies
from .errors import ChatProblem

logger = logging.getLogger(__name__)
POLL_SECONDS = 0.2

class ChatRunner:
    def __init__(self, store, executor=None):
        self.store = store
        self.executor = executor
        self.worker_id = uuid4().hex
        self.tasks = {}

    def start(self, question_id):
        previous = self.tasks.get(question_id)
        async def after_previous():
            if previous is not None:
                try:
                    await previous
                except asyncio.CancelledError:
                    if cast(asyncio.Task, asyncio.current_task()).cancelling():
                        raise
                except Exception:
                    pass
            await self._supervise(question_id)
        task = asyncio.create_task(after_previous(), name="chat-" + question_id)
        self.tasks[question_id] = task
        task.add_done_callback(lambda done: self._forget(question_id, done))

    def _forget(self, question_id, task):  # 旧任务收尾不能移除同一问题的新任务。
        if self.tasks.get(question_id) is task:
            self.tasks.pop(question_id, None)
        if not task.cancelled() and task.exception() is not None:
            logger.error("聊天后台任务异常结束：%s", question_id, exc_info=task.exception())

    async def _agent(self, data, checkpoint):
        saved = normalize_agent_state(data["state"])
        stop_event = data["stop_event"]
        try:
            ready = await call_until_stopped(has_ready_documents, stop_event, session_factory=self.store.factory)
            if not ready:
                response = AgentChatResponse(status=ChatStatus.NO_READY_DOCUMENTS, question_id=data["question_id"], message="当前没有可用文件")
                return AgentResult(response=response, state=saved)
            async with RealRAG(data["question"], conditions=saved.conditions, session_factory=self.store.factory) as runtime:
                return await agent_answer(
                    question=data["question"],
                    session_id=data["session_id"],
                    question_id=data["question_id"],
                    conditions=data["conditions"],
                    agent_state=saved,
                    **stoppable_dependencies(runtime.dependencies(), stop_event),
                )
        except AgentStopped as exc:
            if exc.state is None:
                setattr(exc, "state", saved)
            raise

    async def _execute(self, question_id, progress, stop_event):
        data = await self.store.execution_input(question_id, self.worker_id)
        data["stop_event"] = stop_event
        async def checkpoint(state, clarification=None):
            await self.store.checkpoint(question_id, self.worker_id, state, clarification)
        execute = self.executor or self._agent
        try:
            result = await execute(data, checkpoint)
        except AgentError as exc:
            if isinstance(getattr(exc, "state", None), AgentState):
                await checkpoint(exc.state)
                progress["reliable"] = True
            if isinstance(exc, AgentStopped):
                await self.store.finish(question_id, self.worker_id, "CANCELLED", error_code="CANCELLED", state_reliable=progress["reliable"])
                return
            raise
        if not isinstance(result, AgentResult):
            raise TypeError("执行器必须返回 AgentResult")
        await checkpoint(result.state, result.response.clarification)  # 先保存最终状态，再确认执行完成。
        progress["reliable"] = True
        await self.store.finish(question_id, self.worker_id, "SUCCEEDED", result.response.model_dump(mode="json"))

    async def _watch_cancel(self, question_id):
        while True:
            record = await self.store.get_execution(question_id)
            if record["cancel_requested"]:
                return
            await asyncio.sleep(POLL_SECONDS)

    async def _supervise(self, question_id):
        progress = {"reliable": False}  # 默认不能假定运行中的最新预算已经返回。
        stop_event = asyncio.Event()
        operation = asyncio.create_task(self._execute(question_id, progress, stop_event))
        cancellation = asyncio.create_task(self._watch_cancel(question_id))
        try:
            done, _ = await asyncio.wait({operation, cancellation}, return_when=asyncio.FIRST_COMPLETED)
            if operation in done:
                await operation
            else:
                await cancellation
                stop_event.set()  # 让依赖包装器抛出可携带快照的异常，不直接取消 Agent。
                await operation
                # _execute 已经保存终态，不能再次 finish，否则可能覆盖用户刚认领的重提。
        except asyncio.CancelledError:
            operation.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await operation
            if cast(asyncio.Task, asyncio.current_task()).cancelling():
                raise
            await self.store.finish(question_id, self.worker_id, "CANCELLED", error_code="CANCELLED", state_reliable=progress["reliable"])
        except Exception as exc:
            operation.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await operation
            if isinstance(exc, ChatProblem) and exc.status == 404:
                return
            logger.exception("聊天执行失败：%s", question_id)
            response = AgentChatResponse(status=ChatStatus.SYSTEM_ERROR, question_id=(await self.store.get_execution(question_id))["question_id"], message="系统暂时无法处理请求，请稍后重试。" if progress["reliable"] else "本题执行状态无法安全恢复，请重新提问。")
            await self.store.finish(question_id, self.worker_id, "FAILED", response.model_dump(mode="json"), "SYSTEM_ERROR", state_reliable=progress["reliable"])  # 状态不可信时禁止清零重跑。
        finally:
            cancellation.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await cancellation

    async def wait(self, question_id, conditions=None):
        while True:
            record = await self.store.get_execution(question_id, conditions=conditions)
            if record["execution_status"] not in {"RUNNING", "CANCELLING"}:
                return record
            await asyncio.sleep(POLL_SECONDS)

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
