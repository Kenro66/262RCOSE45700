from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from app.database import SessionFactory
from app.qa.agent.state import AgentState, Clarification, normalize_agent_state
from .errors import ChatProblem
from .models import ChatQuestion, ChatSession, ChatSubmission  # 每个问题只保留一份当前执行记录。

ACTIVE = {"RUNNING", "CANCELLING"}

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

class ChatStore:
    def __init__(self, factory=SessionFactory):
        self.factory = factory

    async def _session(self, db, session_id):  # 先锁聊天，再更新问题，保持固定锁顺序。
        chat = await db.scalar(select(ChatSession).where(ChatSession.session_id == session_id).with_for_update())
        if chat is None or chat.deleted:
            raise ChatProblem("SESSION_NOT_FOUND", "找不到这个聊天。", 404)
        return chat

    @staticmethod
    def execution_view(row):
        return {
            "session_id": row.session_id,
            "question_id": row.question_id,
            "execution_status": row.status,
            "response": row.response,
            "error_code": row.error_code,
            "retryable": row.status in {"FAILED", "CANCELLED"} and row.error_code != "INVALID_SAVED_STATE",
        }

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
                    return self.execution_view(current), False
                if question.status == "UNRECOVERABLE":
                    raise ChatProblem("INVALID_SAVED_STATE", "本题状态无法安全恢复，请重新提问。")
                if question.clarification is not None:
                    validate_conditions(question.clarification, request.conditions)
                elif request.conditions:
                    raise ChatProblem("UNEXPECTED_CONDITIONS", "本题尚未询问这些条件。", 422)
                saved = restore_state(question.state)
                if (saved.question, saved.session_id, saved.question_id) != (question.question, session_id, question.question_id):
                    raise ChatProblem("INVALID_SAVED_STATE", "问题状态关联异常，请重新提问。")
            else:
                saved = AgentState(question=request.message, session_id=session_id, question_id=new_id("q_"))
                question = ChatQuestion(question_id=saved.question_id, session_id=session_id, question=request.message, state=saved.model_dump(mode="json"), status="NEW")
                db.add(question)
                await db.flush()
            merged = {**saved.conditions, **(request.conditions or {})}
            question.state = saved.model_copy(update={"conditions": merged}).model_dump(mode="json")  # 计数和证据不清零。
            if current is None:
                current = ChatSubmission(question_id=question.question_id, session_id=session_id, worker_id=worker_id)
                db.add(current)
            current.conditions = request.conditions or {}
            current.status = "RUNNING"
            current.response = None
            current.error_code = None
            current.cancel_requested = False
            current.worker_id = worker_id
            current.updated_at = question.updated_at = chat.updated_at = now()
            question.status = "RUNNING"
            await db.flush()
            return self.execution_view(current), True

    async def get_execution(self, question_id, conditions=None, *, session_id=None, include_worker=False):
        async with self.factory() as db:
            row = await db.get(ChatSubmission, question_id)
            if row is None:
                raise ChatProblem("QUESTION_NOT_FOUND", "找不到本题执行状态。", 404)
            if session_id is not None and row.session_id != session_id:
                raise ChatProblem("QUESTION_NOT_FOUND", "这个聊天中找不到本题。", 404)
            await self._session(db, row.session_id)
            await db.refresh(row)
            if conditions is not None and row.conditions != conditions:
                raise ChatProblem("QUESTION_CHANGED", "本题条件已更新，请读取当前问题状态。")
            result = self.execution_view(row)
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
            return {"question": question.question, "session_id": row.session_id, "question_id": question_id, "conditions": row.conditions, "state": question.state}

    async def checkpoint(self, question_id, worker_id, state, clarification=None):
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
            old = restore_state(question.state)
            if state.retrieval_count < old.retrieval_count or state.clarification_count < old.clarification_count:
                raise ChatProblem("INVALID_SAVED_STATE", "执行次数不能回退。")
            if (state.question, state.session_id, state.question_id) != (question.question, row.session_id, question_id):
                raise ChatProblem("INVALID_SAVED_STATE", "状态不属于当前问题。")
            question.state = state.model_dump(mode="json")
            if clarification is not None:
                question.clarification = clarification.model_dump(mode="json")
            question.updated_at = row.updated_at = now()

    async def finish(self, question_id, worker_id, status, response=None, error_code=None, *, state_reliable=True):
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
            if row.cancel_requested:
                status, response, error_code = "CANCELLED", None, "CANCELLED"
            if not state_reliable:
                error_code = "INVALID_SAVED_STATE"
            row.status, row.error_code = status, error_code
            row.response = {**response, "session_id": row.session_id, "execution_status": status} if response is not None else None
            if status == "SUCCEEDED":
                question.status = "WAITING" if response["status"] == "NEEDS_CLARIFICATION" else "COMPLETED"
                if response.get("clarification"):
                    question.clarification = response["clarification"]
            else:
                question.status = status if state_reliable else "UNRECOVERABLE"  # 保留旧快照，但不把旧预算当作最新预算。
            question.updated_at = row.updated_at = chat.updated_at = now()

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
            return self.execution_view(row)

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
            await db.execute(delete(ChatSubmission).where(ChatSubmission.session_id == session_id))
            await db.execute(delete(ChatQuestion).where(ChatQuestion.session_id == session_id))
