"""Single retrieval RAG with injected backend dependencies and validated citations."""

import inspect
import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from ..adapters.generation import generate_text
from ..contracts import ChatResponse, ChatStatus, DocumentRecord, SearchHit
from .citation import build_evidence, has_cited_conclusions, has_unsupported_numbers, resolve_citations
from .prompts import build_messages, detect_language


class AnswerError(RuntimeError):
    """Missing integration dependencies or malformed model output."""


Retrieval = Callable[[str], Awaitable[Sequence[SearchHit | dict[str, Any]]]]
DocumentLookup = Callable[[list[str]], Awaitable[Mapping[str, DocumentRecord | Mapping[str, Any]]]]
Generator = Callable[[list[dict[str, str]]], str | Awaitable[str]]


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
                 get_documents_fn: DocumentLookup | None = None, generate_fn: Generator | None = None) -> ChatResponse:
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
    evidence = build_evidence(await retrieve_fn(question))
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
