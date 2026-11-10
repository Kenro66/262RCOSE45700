"""Agent-only models; persistent state is separate from the execution graph."""

from typing import Annotated, Any, Literal, TypedDict
from math import isfinite

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...contracts import ChatResponse, ChatStatus, Chunk, SearchHit, Citation
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

    @model_validator(mode="after")
    def validate_sides(self):
        if not self.reason.strip():
            raise ValueError("Conflict reason must not be blank")
        if len(set(self.side_a)) != len(self.side_a) or len(set(self.side_b)) != len(self.side_b):
            raise ValueError("Conflict sides must not contain duplicate evidence IDs")
        if set(self.side_a) & set(self.side_b):
            raise ValueError("Conflict sides must be disjoint")
        return self


class AgentChatResponse(ChatResponse):
    """Additive internal C5 candidate; the existing public model is untouched."""

    question_id: str
    clarification: Clarification | None = None
    conflict: Conflict | None = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_response(self):
        if not self.question_id.strip():
            raise ValueError("Agent response requires a question ID")
        ids = [c.evidence_id for c in self.citations]
        if len(set(ids)) != len(ids):
            raise ValueError("Citation evidence IDs must be unique")
        for citation in self.citations:
            # Revalidate the original public Citation contract, including page >= 1.
            Citation.model_validate(citation.model_dump())
            if not all(x.strip() for x in (citation.evidence_id, citation.filename, citation.quote)):
                raise ValueError("Citation fields must not be blank")
        if self.status == ChatStatus.NEEDS_CLARIFICATION:
            if self.clarification is None or self.answer is not None or self.citations:
                raise ValueError("Clarification requires questions, no answer and no citations")
        elif self.clarification is not None:
            raise ValueError("Clarification only belongs to NEEDS_CLARIFICATION")
        if self.status == ChatStatus.CONFLICTING_EVIDENCE:
            if self.conflict is None or self.answer is not None:
                raise ValueError("Conflict requires both sides and no answer")
            Conflict.model_validate(self.conflict.model_dump())
            if not set(self.conflict.side_a + self.conflict.side_b) <= set(ids):
                raise ValueError("Conflict references must all resolve to final citations")
        elif self.conflict is not None:
            raise ValueError("Conflict only belongs to CONFLICTING_EVIDENCE")
        if self.status == ChatStatus.ANSWERED:
            if not self.answer or not self.answer.strip() or not self.citations:
                raise ValueError("ANSWERED requires an answer and citations")
        elif self.status == ChatStatus.INSUFFICIENT_EVIDENCE:
            if self.answer is not None or self.citations:
                raise ValueError("INSUFFICIENT_EVIDENCE requires no answer or citations")
        return self


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
    retrieval_queries: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_cache(self):
        if len(self.retrieval_queries) > self.retrieval_count:
            raise ValueError("Query history cannot exceed consumed retrieval budget")
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


def normalize_agent_state(value: AgentState | dict | None) -> AgentState:
    """Restore a complete server snapshot; never default missing persisted fields.

    This validates shape, not authenticity. The backend must supply trusted Store
    data and must not replace a missing/expired snapshot with None.
    """
    if value is None:
        return AgentState()
    if isinstance(value, AgentState):
        value = value.model_dump()
    elif not isinstance(value, dict):
        raise ValueError("agent_state must be AgentState, a snapshot dictionary, or None")
    missing = AgentState.model_fields.keys() - value.keys()
    if missing:
        raise ValueError(f"Incomplete AgentState snapshot: {', '.join(sorted(missing))}")
    return AgentState.model_validate(value)


class AgentResult(BaseModel):
    response: AgentChatResponse
    state: AgentState
    # Execution diagnostics belong to the backend result, never the HTTP response.
    trace: list[str] = Field(default_factory=list)


class ConditionsDecision(StrictModel):
    missing_conditions: list[ClarificationQuestion]


class EvidenceSupport(StrictModel):
    chunk_id: str = Field(min_length=1)


class EvidenceCoverage(StrictModel):
    status: Literal["supported", "missing", "not_required"]
    affects_core: bool
    reason: str = Field(min_length=1)
    support: list[EvidenceSupport]


class EvidenceDecision(StrictModel):
    verdict: Literal["SUFFICIENT", "MISSING_CRITICAL", "UNSUPPORTED"]
    core_claim: str = Field(min_length=1)
    core_support: list[EvidenceSupport]
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
    seen_queries: list[str]
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
