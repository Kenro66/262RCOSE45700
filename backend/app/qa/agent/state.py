"""Agent-only models; persistent state is separate from the execution graph."""

from typing import Annotated, Any, Literal, TypedDict
from math import isfinite

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...contracts import ChatResponse, ChatStatus, Chunk, SearchHit
from ..citation import Evidence


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ClarificationQuestion(StrictModel):
    field: str = Field(min_length=1)
    question: str = Field(min_length=1)
    type: Literal["select", "text"] = "text"
    options: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_question(self):
        if not self.field.strip() or not self.question.strip():
            raise ValueError("Clarification field and question must not be blank")
        if self.type == "select" and (not self.options or any(not x.strip() for x in self.options)):
            raise ValueError("Select clarification requires nonempty options")
        return self


class Clarification(StrictModel):
    questions: list[ClarificationQuestion] = Field(min_length=1)


class Conflict(StrictModel):
    reason: str = Field(min_length=1)
    side_a: list[Annotated[str, Field(pattern=r"^E[1-9]\d*$")]] = Field(min_length=1)
    side_b: list[Annotated[str, Field(pattern=r"^E[1-9]\d*$")]] = Field(min_length=1)


class AgentChatResponse(ChatResponse):
    """Additive internal C5 candidate; the existing public model is untouched."""

    question_id: str
    clarification: Clarification | None = None
    conflict: Conflict | None = None


class CachedEvidence(StrictModel):
    evidence_id: str
    hit: SearchHit | Chunk


class AgentState(StrictModel):
    retrieval_count: int = Field(default=0, ge=0, le=3)
    clarification_count: int = Field(default=0, ge=0, le=1)
    evidence_ids: list[str] = Field(default_factory=list)
    # Full original source data survives JSON round trips, including Chunk metadata.
    evidence: list[CachedEvidence] = Field(default_factory=list)
    question: str | None = None
    session_id: str | None = None
    question_id: str | None = None
    conditions: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_cache(self):
        expected = [f"E{i}" for i in range(1, len(self.evidence) + 1)]
        if self.evidence_ids != expected or [e.evidence_id for e in self.evidence] != expected:
            raise ValueError("Evidence IDs must match the full, sequential source cache")
        ids = [e.hit.chunk_id for e in self.evidence]
        if len(ids) != len(set(ids)):
            raise ValueError("Cached chunk IDs must be unique")
        if any(not e.hit.chunk_id.strip() or not e.hit.document_id.strip() or not e.hit.text.strip()
               for e in self.evidence):
            raise ValueError("Cached sources must contain stable IDs and original text")
        if any(isinstance(e.hit, SearchHit) and not isfinite(e.hit.score) for e in self.evidence):
            raise ValueError("Cached search scores must be finite")
        if self.evidence and (not self.question or not self.question_id or self.retrieval_count == 0):
            raise ValueError("Cached sources must be bound to a logical question and consumed retrieval")
        return self


class AgentResult(BaseModel):
    response: AgentChatResponse
    state: AgentState
    # Execution diagnostics belong to the backend result, never the HTTP response.
    trace: list[str] = Field(default_factory=list)


class ConditionsDecision(StrictModel):
    missing_conditions: list[ClarificationQuestion]


class EvidenceSupport(StrictModel):
    evidence_id: str
    quote: str = Field(min_length=1)


class EvidenceCoverage(StrictModel):
    status: Literal["supported", "missing", "not_required"]
    reason: str = Field(min_length=1)
    support: list[EvidenceSupport]


class EvidenceDecision(StrictModel):
    sufficient: bool
    definitions: EvidenceCoverage
    exceptions: EvidenceCoverage
    applicability: EvidenceCoverage
    complete_answer: EvidenceCoverage
    missing_evidence: list[str]


class RewriteDecision(StrictModel):
    query: str = Field(min_length=1)


class ConflictDecision(StrictModel):
    conflict: Conflict | None


class AnswerDecision(StrictModel):
    answer: str = Field(min_length=1)
    insufficient_evidence: bool


class GraphState(TypedDict):
    question: str
    session_id: str | None
    question_id: str
    conditions: dict[str, Any]
    current_query: str
    retrieval_count: int
    clarification_count: int
    evidence: list[Evidence]
    documents: dict
    sufficient: bool
    evidence_check_result: dict
    missing_conditions: list[ClarificationQuestion]
    status: ChatStatus | None
    answer: str | None
    clarification: Clarification | None
    conflict: Conflict | None
    trace: list[str]
