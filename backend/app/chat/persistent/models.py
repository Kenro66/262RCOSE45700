from datetime import datetime
from sqlalchemy import JSON, Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base

JsonType = JSON().with_variant(JSONB(), "postgresql")

class ChatSession(Base):
    __tablename__ = "chat_sessions"
    session_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    title: Mapped[str] = mapped_column(String(100), default="")
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)  # 删除后保留最小墓碑，防止迟到请求重新建回会话。
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

class ChatQuestion(Base):
    __tablename__ = "chat_questions"
    question_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("chat_sessions.session_id", ondelete="CASCADE"), index=True)
    question: Mapped[str] = mapped_column(Text)
    state: Mapped[dict] = mapped_column(JsonType)
    clarification: Mapped[dict | None] = mapped_column(JsonType, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="NEW")
    mode: Mapped[str | None] = mapped_column(String(8), nullable=True)
    document_ids: Mapped[list | None] = mapped_column(JsonType, nullable=True)
    retrieval_used: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state_reliable: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (CheckConstraint("retrieval_used IS NULL OR retrieval_used BETWEEN 0 AND 3", name="ck_chat_retrieval_used"),)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

class ChatSubmission(Base):  # 每个问题只保留一份当前执行记录，不再给每次提交创建编号。
    __tablename__ = "chat_submissions"
    session_id: Mapped[str] = mapped_column(ForeignKey("chat_sessions.session_id", ondelete="CASCADE"), index=True)
    question_id: Mapped[str] = mapped_column(ForeignKey("chat_questions.question_id", ondelete="CASCADE"), primary_key=True)
    conditions: Mapped[dict] = mapped_column(JsonType, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="RUNNING", index=True)
    response: Mapped[dict | None] = mapped_column(JsonType, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    worker_id: Mapped[str] = mapped_column(String(64))
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fallback_started: Mapped[bool] = mapped_column(Boolean, default=False)
    stop_reason: Mapped[str | None] = mapped_column(String(24), nullable=True)
    inflight_call: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChatRetrievalAttempt(Base):
    __tablename__ = "chat_retrieval_attempts"
    question_id: Mapped[str] = mapped_column(ForeignKey("chat_questions.question_id", ondelete="CASCADE"), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64))
    query: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(8))
    outcome: Mapped[str] = mapped_column(String(24), default="RESERVED")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (CheckConstraint("ordinal BETWEEN 1 AND 3", name="ck_chat_attempt_ordinal"),)
