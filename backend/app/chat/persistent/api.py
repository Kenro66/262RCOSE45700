import logging
from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from .errors import ChatProblem
from .schemas import ExecutionView, PersistentChatRequest, PersistentChatResponse

logger = logging.getLogger(__name__)

def reply(request, content, status=200):
    result = JSONResponse(jsonable_encoder(content), status_code=status)
    result.headers["Cache-Control"] = "no-store"
    return result

def execution_response(request, record):
    record = {key: value for key, value in record.items() if key != "cancel_requested"}
    if record["response"] is not None:
        status = 500 if record["execution_status"] == "FAILED" else 200
        body = {**record["response"], "error_code": record["error_code"], "retryable": record["retryable"]}
        return reply(request, body, status)
    status = 202 if record["execution_status"] in {"RUNNING", "CANCELLING"} else 200
    return reply(request, record, status)

def build_persistent_chat_router(store, runner):
    router = APIRouter(prefix="/chat", tags=["persistent-chat"])

    async def guarded(request, action):
        try:
            return await action()
        except ChatProblem as exc:
            return reply(request, {"error_code": exc.code, "message": exc.message, "retryable": False}, exc.status)
        except Exception:
            logger.exception("持久化聊天接口失败")
            return reply(request, {"status": "SYSTEM_ERROR", "answer": None, "citations": [], "message": "系统暂时无法处理请求，请稍后重试。", "error_code": "SYSTEM_ERROR", "retryable": True}, 500)

    @router.post("", response_model=PersistentChatResponse, responses={202: {"model": ExecutionView}})
    async def chat(payload: PersistentChatRequest, request: Request, wait: bool = True):
        async def action():
            record, should_start = await store.reserve(payload, runner.worker_id)
            if should_start:
                runner.start(record["question_id"])
            if wait and record["execution_status"] in {"RUNNING", "CANCELLING"}:
                record = await runner.wait(record["question_id"], conditions=payload.conditions or {})
            return execution_response(request, record)
        return await guarded(request, action)

    @router.get("/sessions")
    async def sessions(request: Request):
        async def action():
            return reply(request, {"sessions": await store.list_sessions()})
        return await guarded(request, action)

    @router.get("/sessions/{session_id}")
    async def session_detail(session_id: str, request: Request):
        async def action():
            return reply(request, await store.session_detail(session_id))
        return await guarded(request, action)

    @router.delete("/sessions/{session_id}")
    async def delete_session(session_id: str, request: Request):
        async def action():
            await store.delete_session(session_id)
            return reply(request, {"deleted": True, "session_id": session_id})
        return await guarded(request, action)

    def execution_status_reply(request, record):  # 取消和状态查询始终返回同一种嵌套结构。
        body = {key: value for key, value in record.items() if key != "cancel_requested"}
        status = 202 if record["execution_status"] in {"RUNNING", "CANCELLING"} else 200
        return reply(request, body, status)

    @router.post("/sessions/{session_id}/questions/{question_id}/cancel", response_model=ExecutionView, responses={202: {"model": ExecutionView}})
    async def cancel(session_id: str, question_id: str, request: Request):
        async def action():
            record = await store.cancel_question(question_id, session_id=session_id)
            return execution_status_reply(request, record)
        return await guarded(request, action)

    @router.get("/sessions/{session_id}/questions/{question_id}/execution", response_model=ExecutionView, responses={202: {"model": ExecutionView}})
    async def execution(session_id: str, question_id: str, request: Request):
        async def action():
            record = await store.get_execution(question_id, session_id=session_id)
            return execution_status_reply(request, record)
        return await guarded(request, action)

    return router
