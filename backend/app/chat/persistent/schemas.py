from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from app.contracts import ChatRequest
from app.qa.agent.state import AgentChatResponse

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,128}$")]
ConditionValue = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=500)]

class PersistentChatRequest(ChatRequest):
    model_config = ConfigDict(extra="forbid")
    session_id: Identifier | None = None
    question_id: Identifier | None = None
    message: str = Field(min_length=1, max_length=10000)
    conditions: dict[str, ConditionValue] | None = None

    @model_validator(mode="after")
    def check_submission(self):
        if not self.message.strip():
            raise ValueError("问题不能为空白")
        if self.conditions is not None and not self.question_id:
            raise ValueError("提交补充条件时必须提供 question_id")
        if self.question_id and not self.session_id:
            raise ValueError("恢复问题时必须提供 session_id")
        return self

class PersistentChatResponse(AgentChatResponse):
    session_id: str
    execution_status: str

class ExecutionView(BaseModel):
    session_id: str
    question_id: str
    execution_status: str
    response: PersistentChatResponse | None = None
    error_code: str | None = None
    retryable: bool = False
