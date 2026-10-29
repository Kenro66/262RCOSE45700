"""Issue #9 route assembly; database and answer dependencies stay injectable."""

from app.chat.service import AnswerFn, create_chat_router
from app.database import SessionFactory


def build_chat_router(answer_fn: AnswerFn):
    return create_chat_router(answer_fn, session_factory=SessionFactory)
