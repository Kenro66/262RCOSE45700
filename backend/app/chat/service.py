"""Issue #9: READY guard and C5 responses; HTTP errors follow Issue #8."""
from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal, Self

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field, model_validator
from starlette.concurrency import run_in_threadpool

from app.contracts import ChatRequest, ChatResponse, ChatStatus, Citation
from app.database import SessionFactory
from app.storage.repo import has_ready_documents

logger = logging.getLogger(__name__)
SessionMaker = Callable[[], Any]
NO_READY_MESSAGE = "当前没有可用文件"
SYSTEM_ERROR_MESSAGE = "系统暂时无法处理请求，请稍后重试。"


class AnswerResult(BaseModel):
    """Validate the two states owned by #12 without changing public contracts."""
    status: Literal["ANSWERED", "INSUFFICIENT_EVIDENCE"]
    answer: str | None = None
    citations: list[Citation] = Field(default_factory=list)
    message: str | None = None

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.status == "ANSWERED":
            if not self.answer or not self.answer.strip() or not self.citations:
                raise ValueError("ANSWERED requires text and citations")
            for citation in self.citations:
                if not all(value.strip() for value in (
                    citation.filename, citation.quote, citation.evidence_id
                )):
                    raise ValueError("Citation fields must not be blank")
        elif self.citations:
            raise ValueError("INSUFFICIENT_EVIDENCE must not include citations")
        return self


AnswerFn = Callable[[str, str | None], Any | Awaitable[Any]]


async def _invoke(function: AnswerFn, message: str, session_id: str | None) -> Any:
    if inspect.iscoroutinefunction(function) or inspect.iscoroutinefunction(
        getattr(function, "__call__", None)
    ):
        value = function(message, session_id)
    else:
        value = await run_in_threadpool(function, message, session_id)
    return await value if inspect.isawaitable(value) else value


def create_chat_router(
    answer_fn: AnswerFn,
    session_factory: SessionMaker = SessionFactory,
) -> APIRouter:
    router = APIRouter(prefix="/chat", tags=["chat"])

    @router.post("", response_model=ChatResponse, responses={500: {"model": ChatResponse}})
    async def chat(request: ChatRequest, response: Response) -> ChatResponse:
        try:
            if not await has_ready_documents(session_factory=session_factory):
                logger.info("没有 READY 文件，不调用 answer()（无模型调用）")
                return ChatResponse(
                    status=ChatStatus.NO_READY_DOCUMENTS, message=NO_READY_MESSAGE
                )
            session_id = request.session_id
            if session_id is not None and not session_id.strip():
                session_id = None
            raw = await _invoke(answer_fn, request.message, session_id)
            result = AnswerResult.model_validate(
                raw.model_dump() if isinstance(raw, BaseModel) else raw
            )
            return ChatResponse(**result.model_dump())
        except Exception:
            # CancelledError deliberately propagates (it derives from BaseException).
            logger.exception("问答系统调用失败，返回 SYSTEM_ERROR")
            response.status_code = 500
            return ChatResponse(
                status=ChatStatus.SYSTEM_ERROR, message=SYSTEM_ERROR_MESSAGE
            )

    return router
