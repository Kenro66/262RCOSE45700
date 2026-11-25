from collections import OrderedDict
from time import monotonic
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from types import SimpleNamespace
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from app.database import SessionFactory
from app.qa.agent.state import AgentChatResponse, AgentState, Clarification, normalize_agent_state
from app.contracts import ChatStatus
from .errors import ChatProblem
from .models import ChatQuestion, ChatRetrievalAttempt, ChatSession, ChatSubmission

ACTIVE = {"RUNNING", "CANCELLING"}
PERSISTED_FIELDS = {"conditions", "retrieval_count", "clarification_count"}
RESPONSE_TTL_SECONDS = 300
MAX_TRANSIENT_RESPONSES = 256


class ExecutionRecord(dict):
    """公开返回字段映射；执行标识仅供服务端使用，不参与序列化。"""
    def __init__(self, values, run_id):
        super().__init__(values)
        self.run_id = run_id

def now():
    return datetime.now(timezone.utc)

def new_id(prefix):
    return prefix + uuid4().hex

def validate_conditions(prompt, conditions):
    try:
        questions = Clarification.model_validate(prompt).questions
    except ValueError as exc:
        raise ChatProblem("INVALID_SAVED_STATE", "追问状态无法恢复，请重新提问。") from exc
    fields = [item.field for item in questions]
    if len(set(fields)) != len(fields):
        raise ChatProblem("INVALID_SAVED_STATE", "追问字段重复，无法安全恢复。")
    if conditions is None or set(conditions) != set(fields):
        raise ChatProblem("INVALID_CONDITIONS", "请完整填写本轮追问，不要添加其他字段。", 422)
    for item in questions:
        value = conditions[item.field]
        if not isinstance(value, str) or not value.strip():
            raise ChatProblem("INVALID_CONDITIONS", f"请填写：{item.question}", 422)
        if item.type == "select" and value not in item.options:
            raise ChatProblem("INVALID_CONDITIONS", f"请选择有效选项：{item.question}", 422)

def restore_state(raw):
    try:
        return normalize_agent_state(raw)
    except (ValueError, TypeError) as exc:
        raise ChatProblem("INVALID_SAVED_STATE", "问题状态无法安全恢复，请重新提问。") from exc

def persisted_state(state):
    return state.model_dump(mode="json", include=PERSISTED_FIELDS)


def restore_question_state(question):
    raw = question.state
    identity = {"question": question.question, "session_id": question.session_id, "question_id": question.question_id}
    if not isinstance(raw, dict) or not PERSISTED_FIELDS.issubset(raw):
        raise ChatProblem("INVALID_SAVED_STATE", "问题状态无法安全恢复，请重新提问。")
    if any(key in raw and raw[key] != value for key, value in identity.items()):
        raise ChatProblem("INVALID_SAVED_STATE", "问题状态关联异常，请重新提问。")
    try:
        return AgentState.model_validate({**identity, **{key: raw[key] for key in PERSISTED_FIELDS}})
    except ValueError as exc:
        raise ChatProblem("INVALID_SAVED_STATE", "问题状态无法安全恢复，请重新提问。") from exc


class ChatStore:
    def __init__(self, factory=SessionFactory, rag_settings=None):
        self.factory = factory
        self.rag_settings = rag_settings
        self.responses = OrderedDict()
        self.retrieval_queries = {}

    async def _session(self, db, session_id):  # 先锁聊天，再更新问题，保持固定锁顺序。
        chat = await db.scalar(select(ChatSession).where(ChatSession.session_id == session_id).with_for_update())
        if chat is None or chat.deleted:
            raise ChatProblem("SESSION_NOT_FOUND", "找不到这个聊天。", 404)
        return chat

    def execution_view(self, row, question=None):
        response = None
        if row.status in {"SUCCEEDED", "FAILED"}:
            cached = self.responses.get(row.question_id)
            if cached is not None:
                expires_at, updated_at, body = cached
                if monotonic() >= expires_at:
                    self.responses.pop(row.question_id)
                elif updated_at == row.updated_at.replace(tzinfo=timezone.utc):
                    response = {"model_name": None, **body}
        if response is None and question is not None and question.status == "WAITING":
            response = AgentChatResponse(
                status=ChatStatus.NEEDS_CLARIFICATION,
                question_id=row.question_id,
                clarification=Clarification.model_validate(question.clarification),
            ).model_dump(mode="json")
            response.update(session_id=row.session_id, execution_status=row.status)
        return ExecutionRecord({
            "session_id": row.session_id,
            "question_id": row.question_id,
            "execution_status": row.status,
            "response": response,
            "error_code": row.error_code,
            "retryable": row.status in {"FAILED", "CANCELLED"} and row.error_code != "INVALID_SAVED_STATE",
        }, row.run_id)

    async def reserve(self, request, worker_id):
        session_id = request.session_id or new_id("s_")
        async with self.factory() as db, db.begin():
            if not request.question_id:
                insert = pg_insert if db.bind.dialect.name == "postgresql" else sqlite_insert
                stmt = insert(ChatSession).values(session_id=session_id, title=request.message[:100], deleted=False)
                await db.execute(stmt.on_conflict_do_nothing(index_elements=["session_id"]))
            chat = await self._session(db, session_id)
            current = None
            if request.question_id:
                question = await db.scalar(select(ChatQuestion).where(ChatQuestion.question_id == request.question_id, ChatQuestion.session_id == session_id).with_for_update())
                if question is None:
                    raise ChatProblem("QUESTION_NOT_FOUND", "找不到原问题，请重新提问。", 404)
                if question.question != request.message:
                    raise ChatProblem("QUESTION_MISMATCH", "原问题不匹配，请作为新问题提交。")
                current = await db.get(ChatSubmission, question.question_id)
                if current is None:
                    raise ChatProblem("INVALID_SAVED_STATE", "本题执行状态缺失，请重新提问。")
                if question.status in ACTIVE:
                    if current.conditions == (request.conditions or {}):
                        return self.execution_view(current), False
                    raise ChatProblem("QUESTION_BUSY", "本题正在处理或停止，请稍后再提交。")
                if question.status == "COMPLETED":
                    if current.conditions == (request.conditions or {}):
                        return self.execution_view(current), False
                    raise ChatProblem("QUESTION_FINISHED", "本题已完成，修改条件请另提新问题。")
                if question.status == "WAITING" and request.conditions is None:
                    return self.execution_view(current, question), False
                if question.status == "UNRECOVERABLE":
                    raise ChatProblem("INVALID_SAVED_STATE", "本题状态无法安全恢复，请重新提问。")
                if question.clarification is not None:
                    validate_conditions(question.clarification, request.conditions)
                elif request.conditions:
                    raise ChatProblem("UNEXPECTED_CONDITIONS", "本题尚未询问这些条件。", 422)
                saved = (SimpleNamespace(question=question.question, session_id=session_id,
                                         question_id=question.question_id, conditions=question.state.get("conditions", {}))
                         if question.mode == "single" else restore_question_state(question))
                if (saved.question, saved.session_id, saved.question_id) != (question.question, session_id, question.question_id):
                    raise ChatProblem("INVALID_SAVED_STATE", "问题状态关联异常，请重新提问。")
            else:
                mode = self.rag_settings.mode if self.rag_settings is not None else None
                values = dict(question=request.message, session_id=session_id, question_id=new_id("q_"))
                saved = SimpleNamespace(**values, conditions={}) if mode == "single" else AgentState(**values)
                document_ids = None
                if mode == "agent":
                    from app.storage.models import Document
                    document_ids = list((await db.scalars(select(Document.document_id).where(Document.status == "READY"))).all())
                question = ChatQuestion(question_id=saved.question_id, session_id=session_id, question=request.message,
                    state={"conditions": {}} if mode == "single" else persisted_state(saved), status="NEW",
                    mode=mode, document_ids=document_ids, retrieval_used=0 if mode == "agent" else None)
                db.add(question)
                await db.flush()
            merged = {**saved.conditions, **(request.conditions or {})}
            question.state = (persisted_state(saved.model_copy(update={"conditions": merged}))
                              if question.mode != "single" else {"conditions": merged})
            if current is None:
                current = ChatSubmission(question_id=question.question_id, session_id=session_id, worker_id=worker_id)
                db.add(current)
            current.conditions = request.conditions or {}
            current.status = "RUNNING"
            current.response = None
            current.error_code = None
            current.cancel_requested = False
            current.worker_id = worker_id
            if question.mode is not None:
                current.run_id = new_id("run_")
                current.fallback_started = False
                current.stop_reason = None
                current.inflight_call = False
                current.deadline_at = (now() + timedelta(seconds=self.rag_settings.execution_timeout_seconds)
                                       if question.mode == "agent" and self.rag_settings is not None else None)
            current.updated_at = question.updated_at = chat.updated_at = now()
            question.status = "RUNNING"
            await db.flush()
            return self.execution_view(current), True

    async def get_execution(self, question_id, conditions=None, *, session_id=None, include_worker=False, execution_key=None):
        async with self.factory() as db:
            row = await db.get(ChatSubmission, question_id)
            if row is None:
                raise ChatProblem("QUESTION_NOT_FOUND", "找不到本题执行状态。", 404)
            if session_id is not None and row.session_id != session_id:
                raise ChatProblem("QUESTION_NOT_FOUND", "这个聊天中找不到本题。", 404)
            await self._session(db, row.session_id)
            await db.refresh(row)
            if execution_key is not None and row.run_id != execution_key:
                raise ChatProblem("QUESTION_CHANGED", "本题执行已更新，请读取当前问题状态。")
            if conditions is not None and row.conditions != conditions:
                raise ChatProblem("QUESTION_CHANGED", "本题条件已更新，请读取当前问题状态。")
            question = await db.get(ChatQuestion, question_id) if row.status == "SUCCEEDED" else None
            result = self.execution_view(row, question)
            result["cancel_requested"] = row.cancel_requested
            if include_worker:
                result["_worker_id"] = row.worker_id
            return result

    async def execution_input(self, question_id, worker_id):
        async with self.factory() as db:
            row = await db.get(ChatSubmission, question_id)
            if row is None:
                raise ChatProblem("QUESTION_NOT_FOUND", "本题执行状态已不存在。", 404)
            await self._session(db, row.session_id)
            await db.refresh(row)
            if row.worker_id != worker_id or row.status not in ACTIVE:
                raise ChatProblem("QUESTION_NOT_ACTIVE", "本题已结束或不属于当前执行进程。")
            question = await db.get(ChatQuestion, question_id)
            state = question.state if question.mode == "single" else restore_question_state(question).model_dump(mode="json")
            return {"question": question.question, "session_id": row.session_id, "question_id": question_id, "conditions": row.conditions, "state": state,
                    "mode": question.mode, "run_id": row.run_id, "deadline_at": row.deadline_at, "document_ids": question.document_ids}

    async def checkpoint(self, question_id, worker_id, state, clarification=None, *, execution_key=None):
        state = restore_state(state.model_dump(mode="json"))
        async with self.factory() as db, db.begin():
            row = await db.get(ChatSubmission, question_id)
            if row is None:
                raise ChatProblem("QUESTION_NOT_FOUND", "本题已删除。", 404)
            await self._session(db, row.session_id)
            await db.refresh(row)
            question = await db.get(ChatQuestion, question_id)
            if row.worker_id != worker_id or row.status not in ACTIVE or question.status not in ACTIVE:
                raise ChatProblem("STALE_EXECUTION", "执行状态已变化。")
            if row.run_id is not None and row.run_id != execution_key:
                raise ChatProblem("STALE_EXECUTION", "执行状态已变化。")
            if question.mode == "agent":
                await self._validate_snapshot(db, question, row, state)
            old = restore_question_state(question)
            if state.retrieval_count < old.retrieval_count or state.clarification_count < old.clarification_count:
                raise ChatProblem("INVALID_SAVED_STATE", "执行次数不能回退。")
            if (state.question, state.session_id, state.question_id) != (question.question, row.session_id, question_id):
                raise ChatProblem("INVALID_SAVED_STATE", "状态不属于当前问题。")
            question.state = persisted_state(state)
            question.state_reliable = True
            if clarification is not None:
                question.clarification = clarification.model_dump(mode="json")
            question.updated_at = row.updated_at = now()

    async def finish(self, question_id, worker_id, status, response=None, error_code=None, *, state_reliable=True, execution_key=None):
        async with self.factory() as db, db.begin():
            row = await db.get(ChatSubmission, question_id)
            if row is None:
                return
            try:
                chat = await self._session(db, row.session_id)
            except ChatProblem:
                return
            await db.refresh(row)
            question = await db.get(ChatQuestion, question_id)
            if row.worker_id != worker_id or row.status not in ACTIVE:
                return
            if row.run_id is not None and row.run_id != execution_key:
                return
            if question.mode == "agent" and row.deadline_at is not None and self._utc(row.deadline_at) <= now():
                row.stop_reason = row.stop_reason or "TIMED_OUT"
                status, error_code = "FAILED", "SYSTEM_ERROR"
                response = dict(status="SYSTEM_ERROR", answer=None, citations=[], question_id=question_id,
                                message="系统暂时无法处理请求，请稍后重试。")
            if row.inflight_call:
                raise ChatProblem("STALE_EXECUTION", "当前服务调用尚未结束。")
            if row.cancel_requested:
                status, response, error_code = "CANCELLED", None, "CANCELLED"
            if not state_reliable:
                error_code = "INVALID_SAVED_STATE"
            row.status, row.error_code = status, error_code
            question.state_reliable = state_reliable
            row.response = None
            body = {**response, "session_id": row.session_id, "execution_status": status} if response is not None else None
            if status == "SUCCEEDED":
                question.status = "WAITING" if response["status"] == "NEEDS_CLARIFICATION" else "COMPLETED"
                question.clarification = response.get("clarification") if question.status == "WAITING" else None
            else:
                question.status = status if state_reliable else "UNRECOVERABLE"  # 保留旧快照，但不把旧预算当作最新预算。
            updated_at = now()
            question.updated_at = row.updated_at = chat.updated_at = updated_at
            # Publish before commit so terminal reads on this process can retrieve the body.
            if body is not None:
                self.responses[question_id] = (monotonic() + RESPONSE_TTL_SECONDS, updated_at, body)
                self.responses.move_to_end(question_id)
                if len(self.responses) > MAX_TRANSIENT_RESPONSES:
                    self.responses.popitem(last=False)
        self.retrieval_queries.pop((question_id, execution_key), None)

    async def cancel_question(self, question_id, *, session_id=None):
        async with self.factory() as db, db.begin():
            row = await db.get(ChatSubmission, question_id)
            if row is None:
                raise ChatProblem("QUESTION_NOT_FOUND", "找不到本题。", 404)
            if session_id is not None and row.session_id != session_id:
                raise ChatProblem("QUESTION_NOT_FOUND", "这个聊天中找不到本题。", 404)
            await self._session(db, row.session_id)
            await db.refresh(row)
            if row.status in ACTIVE:
                row.cancel_requested, row.status = True, "CANCELLING"
                question = await db.get(ChatQuestion, question_id)
                question.status = "CANCELLING"
                row.updated_at = question.updated_at = now()
            question = await db.get(ChatQuestion, question_id) if row.status == "SUCCEEDED" else None
            return self.execution_view(row, question)

    async def list_sessions(self):
        async with self.factory() as db:
            rows = (await db.scalars(select(ChatSession).where(ChatSession.deleted.is_(False)).order_by(ChatSession.updated_at.desc(), ChatSession.session_id))).all()
            return [{"session_id": row.session_id, "title": row.title, "created_at": row.created_at, "updated_at": row.updated_at} for row in rows]

    async def session_detail(self, session_id):
        async with self.factory() as db:
            chat = await self._session(db, session_id)
            questions = (await db.scalars(select(ChatQuestion).where(ChatQuestion.session_id == session_id).order_by(ChatQuestion.created_at, ChatQuestion.question_id))).all()
            return {"session_id": session_id, "title": chat.title, "questions": [{"question_id": q.question_id, "message": q.question, "status": q.status, "clarification": q.clarification, "conditions": q.state.get("conditions", {}), "retrieval_count": q.state.get("retrieval_count"), "clarification_count": q.state.get("clarification_count")} for q in questions]}

    async def delete_session(self, session_id):
        async with self.factory() as db, db.begin():
            chat = await self._session(db, session_id)
            chat.deleted, chat.title, chat.updated_at = True, "", now()
            question_ids = (await db.scalars(select(ChatQuestion.question_id).where(ChatQuestion.session_id == session_id))).all()
            await db.execute(delete(ChatSubmission).where(ChatSubmission.session_id == session_id))
            await db.execute(delete(ChatQuestion).where(ChatQuestion.session_id == session_id))
        for question_id in question_ids:
            self.responses.pop(question_id, None)
        deleted_ids = set(question_ids)
        for key in list(self.retrieval_queries):
            if key[0] in deleted_ids:
                self.retrieval_queries.pop(key, None)

    @staticmethod
    def _utc(value):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value

    async def _locked_run(self, db, question_id, run_id):
        row = await db.get(ChatSubmission, question_id)
        if row is None:
            raise ChatProblem("QUESTION_NOT_FOUND", "找不到本题。", 404)
        await self._session(db, row.session_id)
        await db.refresh(row)
        question = await db.get(ChatQuestion, question_id)
        if row.run_id != run_id or row.status not in ACTIVE or question is None or question.status not in ACTIVE:
            raise ChatProblem("STALE_EXECUTION", "执行状态已变化。")
        return question, row

    async def check_run(self, question_id, run_id, *, allow_stopped=False):
        async with self.factory() as db, db.begin():
            _, row = await self._locked_run(db, question_id, run_id)
            reason = row.stop_reason
            if row.cancel_requested:
                reason = "CANCELLED"
            elif row.deadline_at is not None and self._utc(row.deadline_at) <= now():
                reason = reason or "TIMED_OUT"
            if reason:
                row.stop_reason = reason
            return reason

    async def mark_call(self, question_id, run_id, active):
        async with self.factory() as db, db.begin():
            _, row = await self._locked_run(db, question_id, run_id)
            if active and (row.cancel_requested or row.stop_reason or (row.deadline_at is not None and self._utc(row.deadline_at) <= now())):
                raise ChatProblem("STALE_EXECUTION", "本次执行已停止。")
            row.inflight_call = active

    async def reserve_retrieval(self, question_id, run_id, query, source):
        if source not in {"AGENT", "FALLBACK"} or not isinstance(query, str) or not query.strip():
            raise ValueError("Invalid retrieval attempt")
        async with self.factory() as db, db.begin():
            question, row = await self._locked_run(db, question_id, run_id)
            if (question.mode != "agent" or row.cancel_requested or row.stop_reason
                    or row.deadline_at is None or self._utc(row.deadline_at) <= now()
                    or question.retrieval_used is None or question.retrieval_used >= 3):
                raise ChatProblem("INVALID_SAVED_STATE", "无法安全开始新的检索。")
            ordinal = question.retrieval_used + 1
            question.retrieval_used = ordinal
            question.state_reliable = False
            db.add(ChatRetrievalAttempt(question_id=question_id, ordinal=ordinal, run_id=run_id, query="", source=source))
            self.retrieval_queries.setdefault((question_id, run_id), {})[ordinal] = query
        return ordinal

    async def finish_retrieval(self, question_id, run_id, ordinal, outcome):
        async with self.factory() as db, db.begin():
            await self._locked_run(db, question_id, run_id)
            attempt = await db.get(ChatRetrievalAttempt, (question_id, ordinal))
            if attempt is None or attempt.run_id != run_id:
                raise ChatProblem("INVALID_SAVED_STATE", "检索记录无法确认。")
            attempt.outcome = outcome

    async def _validate_snapshot(self, db, question, row, state):
        attempts = list((await db.scalars(select(ChatRetrievalAttempt).where(ChatRetrievalAttempt.question_id == question.question_id).order_by(ChatRetrievalAttempt.ordinal))).all())
        queries = self.retrieval_queries.get((question.question_id, row.run_id), {})
        if (question.retrieval_used is None or state.retrieval_count != question.retrieval_used
                or [item.ordinal for item in attempts] != list(range(1, question.retrieval_used + 1))
                or state.retrieval_queries != [queries.get(item.ordinal) for item in attempts if item.run_id == row.run_id]
                or any(item.outcome in {"RESERVED", "UNKNOWN"} for item in attempts)
                or state.conditions != question.state.get("conditions", {})
                or row.inflight_call):
            raise ChatProblem("INVALID_SAVED_STATE", "状态与已消耗检索预算不一致。")
        allowed = set(question.document_ids or [])
        if any(item.hit.document_id not in allowed for item in state.evidence):
            raise ChatProblem("INVALID_SAVED_STATE", "证据超出原问题范围。")

    async def begin_fallback(self, question_id, run_id, state):
        async with self.factory() as db, db.begin():
            question, row = await self._locked_run(db, question_id, run_id)
            await self._validate_snapshot(db, question, row, state)
            if (not question.state_reliable or row.fallback_started or row.inflight_call
                    or row.cancel_requested or row.stop_reason or question.retrieval_used >= 3
                    or row.deadline_at is None or self._utc(row.deadline_at) <= now()):
                return False
            row.fallback_started = True
            return True
