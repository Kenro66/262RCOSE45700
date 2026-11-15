import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable
from app.chat.persistent.errors import ChatProblem
from app.chat.persistent.models import ChatQuestion, ChatSession, ChatSubmission
from app.chat.persistent.schemas import PersistentChatRequest
from app.chat.persistent.store import restore_state, validate_conditions
from app.qa.agent.state import AgentState
from .conftest import prompt


@pytest.mark.parametrize("payload", [
    {"message": ""}, {"message": "  "}, {"message": "a" * 10001},
    {"message": "问", "session_id": "bad id"}, {"message": "问", "session_id": "a" * 129},
    {"message": "问", "session_id": ""}, {"message": "问", "question_id": "q"},
    {"message": "问", "conditions": {}}, {"message": "问", "request_id": "r"},
    {"message": "问", "state": {}}, {"message": "问", "cookie_id": "x"},
    {"message": "问", "session_id": "s", "question_id": "q", "conditions": {"x": 1}},
    {"message": "问", "session_id": "s", "question_id": "q", "conditions": {"x": ""}},
    {"message": "问", "session_id": "s", "question_id": "q", "conditions": {"x": "x" * 501}},
])
def test_invalid_request(payload):
    with pytest.raises(ValidationError):
        PersistentChatRequest.model_validate(payload)


@pytest.mark.parametrize("message", ["问", "a" * 10000])
def test_valid_request_boundaries(message):
    assert PersistentChatRequest(message=message).message == message


@pytest.mark.parametrize("conditions", [None, {}, {"other": "CN"}, {"country": "XX"}, {"country": " "}, {"country": "CN", "extra": "x"}])
def test_reject_invalid_conditions(conditions):
    with pytest.raises(ChatProblem) as error:
        validate_conditions(prompt().model_dump(), conditions)
    assert error.value.code == "INVALID_CONDITIONS"
    assert error.value.status == 422


@pytest.mark.parametrize("kind,options,value", [("select", ["CN"], "CN"), ("text", [], "自由填写")])
def test_accept_valid_conditions(kind, options, value):
    validate_conditions({"questions": [{"field": "x", "question": "?", "type": kind, "options": options}]}, {"x": value})


@pytest.mark.parametrize("saved", [{}, {"questions": []}, {"questions": [{"field": "x"}]}])
def test_invalid_saved_clarification(saved):
    with pytest.raises(ChatProblem, match="追问状态"):
        validate_conditions(saved, {"x": "value"})


def test_duplicate_prompt_field_rejected():
    saved = prompt().model_dump()
    saved["questions"] *= 2
    with pytest.raises(ChatProblem) as error:
        validate_conditions(saved, {"country": "CN"})
    assert error.value.code == "INVALID_SAVED_STATE"


@pytest.mark.parametrize("raw", [{}, [], 12, {**AgentState().model_dump(), "retrieval_count": 4}, {**AgentState().model_dump(), "clarification_count": 2}])
def test_corrupt_saved_state_not_reset(raw):
    with pytest.raises(ChatProblem) as error:
        restore_state(raw)
    assert error.value.code == "INVALID_SAVED_STATE"


def test_round_trip_complete_state():
    state = AgentState(question="问", session_id="s", question_id="q", retrieval_count=2, clarification_count=1, conditions={"country": "CN"}, retrieval_queries=["a", "b"])
    assert restore_state(state.model_dump(mode="json")) == state


def test_database_models_compile_and_use_existing_question_id():
    for model in (ChatSession, ChatQuestion, ChatSubmission):
        sql = str(CreateTable(model.__table__).compile(dialect=postgresql.dialect()))
        assert "CREATE TABLE" in sql
        assert "request_id" not in sql and "cookie_id" not in sql
    assert list(ChatSubmission.__table__.primary_key.columns.keys()) == ["question_id"]
    assert "retryable" not in ChatSubmission.__table__.columns
    assert "JSONB" in str(CreateTable(ChatQuestion.__table__).compile(dialect=postgresql.dialect()))


def test_chat_problem_keeps_code_message_and_http_status():
    error = ChatProblem("QUESTION_NOT_FOUND", "找不到", 404)
    assert (error.code, str(error), error.status) == ("QUESTION_NOT_FOUND", "找不到", 404)
