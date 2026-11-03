"""Single retrieval RAG with injected backend dependencies and validated citations."""

import inspect
import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from ..adapters.generation import generate_text
from ..adapters.config import settings
from ..contracts import ChatResponse, ChatStatus, DocumentRecord, SearchHit
from .citation import assemble_evidence, validate_hits, has_cited_conclusions, has_unsupported_numbers, resolve_citations
from .evidence_completion import SupplementalEvidence
from .prompts import build_messages, detect_language


class AnswerError(RuntimeError):
    """Missing integration dependencies or malformed model output."""


Retrieval = Callable[[str], Awaitable[Sequence[SearchHit | dict[str, Any]]]]
DocumentLookup = Callable[[list[str]], Awaitable[Mapping[str, DocumentRecord | Mapping[str, Any]]]]
Generator = Callable[[list[dict[str, str]]], str | Awaitable[str]]
Completion = Callable[[Sequence[SearchHit]], Awaitable[Sequence[SupplementalEvidence]]]
logger = logging.getLogger(__name__)


async def _generate(function: Generator, messages: list[dict[str, str]]) -> str:
    if inspect.iscoroutinefunction(function) or inspect.iscoroutinefunction(getattr(function, "__call__", None)):
        return await function(messages)
    result = await asyncio.to_thread(function, messages)
    if inspect.isawaitable(result):
        return await result
    return result


def _unique_json_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate generation JSON fields")
        result[key] = value
    return result


def _insufficient(language: str) -> ChatResponse:
    message = ("现有证据不足，无法确认相关政策，请提供对应政策资料。" if language == "zh" else
               "현재 증거가 부족하여 관련 규정을 확인할 수 없습니다. 해당 정책 자료를 제공해 주세요.")
    return ChatResponse(status=ChatStatus.INSUFFICIENT_EVIDENCE, answer=message, citations=[])


async def answer(question: str, session_id: str | None = None, *, retrieve_fn: Retrieval | None = None,
                 get_documents_fn: DocumentLookup | None = None, generate_fn: Generator | None = None,
                 complete_evidence_fn: Completion | None = None) -> ChatResponse:
    """Await backend retrieval/metadata; offload the existing synchronous generator."""
    language = detect_language(question)
    if session_id is not None and (not isinstance(session_id, str) or not session_id.strip()):
        raise ValueError("session_id must be a nonempty string or None")
    for name, function in (("retrieve_fn", retrieve_fn), ("get_documents_fn", get_documents_fn)):
        if not callable(function):
            raise AnswerError(f"Inject an asynchronous {name}; no backend default is configured")
    generator = generate_text if generate_fn is None else generate_fn
    if not callable(generator):
        raise AnswerError("generate_fn must be callable")
    initial_hits = validate_hits(await retrieve_fn(question))
    supplemental: Sequence[SupplementalEvidence] = ()
    if (initial_hits and settings().get("EVIDENCE_COMPLETION_ENABLED", "false").strip().lower() == "true"
            and complete_evidence_fn is not None):
        try:
            supplemental = await complete_evidence_fn(tuple(hit.model_copy(deep=True) for hit in initial_hits))
            if not isinstance(supplemental, (list, tuple)) or len(supplemental) > 1:
                raise ValueError("Completion must return at most one supplemental source")
            if any(not isinstance(item, SupplementalEvidence) for item in supplemental):
                raise ValueError("Invalid supplemental source")
        except Exception:
            logger.warning("Evidence completion failed; retaining Initial retrieval", exc_info=True)
            supplemental = ()
    evidence = assemble_evidence(initial_hits, supplemental)
    if not evidence:
        return _insufficient(language)
    document_ids = list(dict.fromkeys(item.hit.document_id for item in evidence))
    documents = await get_documents_fn(document_ids)
    # Validate source metadata before sending any evidence to the model.
    markers = "".join(f"[{item.evidence_id}]" for item in evidence)
    _, sources = resolve_citations(markers, evidence, documents)
    usable = {source.evidence_id for source in sources}
    evidence = [item for item in evidence if item.evidence_id in usable]
    if not evidence:
        return _insufficient(language)
    raw = await _generate(generator, build_messages(question, language, evidence))
    if not isinstance(raw, str) or not raw.strip():
        raise AnswerError("Generation returned no text")
    try:
        payload = json.loads(raw, object_pairs_hook=_unique_json_fields)
    except (TypeError, ValueError) as exc:
        raise AnswerError("Generation must return the specified JSON object") from exc
    if (not isinstance(payload, dict) or set(payload) != {"answer", "insufficient_evidence"}
            or not isinstance(payload["answer"], str)
            or not payload["answer"].strip() or type(payload["insufficient_evidence"]) is not bool):
        raise AnswerError("Generation returned invalid answer fields")
    if payload["insufficient_evidence"]:
        return _insufficient(language)
    cleaned, citations = resolve_citations(payload["answer"], evidence, documents)
    if not citations:
        return _insufficient(language)
    if not has_cited_conclusions(cleaned):
        raise AnswerError("Every generated sentence or line must have a valid evidence reference")
    if detect_language(cleaned) != language:
        raise AnswerError("Generated answer language does not match the question")
    if has_unsupported_numbers(cleaned, citations):
        return _insufficient(language)
    return ChatResponse(status=ChatStatus.ANSWERED, answer=cleaned, citations=citations)
