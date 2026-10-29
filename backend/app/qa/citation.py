"""Request-local evidence IDs and citation resolution, without database access."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from math import isfinite
import re
from typing import Any
import unicodedata

from ..contracts import Citation, DocumentRecord, DocumentStatus, SearchHit


REFERENCE = re.compile(r"\[E\d+\]")


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    hit: SearchHit


def build_evidence(hits: Sequence[SearchHit | dict[str, Any]]) -> list[Evidence]:
    if not isinstance(hits, (list, tuple)):
        raise ValueError("Retrieval must return a list or tuple of SearchHit")
    evidence = []
    for row in hits:
        hit = SearchHit.model_validate(row)
        if not hit.chunk_id.strip() or not hit.document_id.strip() or not hit.text.strip() or not isfinite(hit.score):
            raise ValueError("Retrieval returned invalid evidence")
        evidence.append(Evidence(f"E{len(evidence) + 1}", hit))
    return evidence


def resolve_citations(text: str, evidence: list[Evidence], documents: Mapping) -> tuple[str, list[Citation]]:
    """Resolve trusted document metadata; missing/non-READY records are unusable."""
    if not isinstance(documents, Mapping):
        raise ValueError("get_documents must return a document_id-keyed mapping")
    by_id = {item.evidence_id: item.hit for item in evidence}
    citations: dict[str, Citation] = {}
    for marker in REFERENCE.findall(text):
        evidence_id = marker[1:-1]
        if evidence_id in citations or evidence_id not in by_id:
            continue
        hit = by_id[evidence_id]
        metadata = documents.get(hit.document_id)
        if metadata is None:
            continue
        if isinstance(metadata, DocumentRecord):
            metadata = metadata.model_dump()
        if not isinstance(metadata, Mapping):
            raise ValueError("Document metadata must be a mapping or DocumentRecord")
        filename = metadata.get("filename")
        if not isinstance(filename, str) or not filename.strip():
            raise ValueError("Document metadata requires filename, not file_name")
        if metadata.get("document_id", hit.document_id) != hit.document_id:
            raise ValueError("Document metadata ID mismatch")
        status = DocumentStatus(metadata.get("status"))
        if status != DocumentStatus.READY:
            continue
        citations[evidence_id] = Citation(evidence_id=evidence_id, filename=filename,
                                          page=hit.page_number, quote=hit.text)
    cleaned = REFERENCE.sub(lambda m: m.group() if m.group()[1:-1] in citations else "", text).strip()
    return cleaned, list(citations.values())


def _conclusion_units(text: str) -> list[str]:
    # Normalize only a validation copy; never rewrite the displayed answer or quote.
    text = "\n".join(text.translate(str.maketrans({"｡": "。", "．": ".", "․": ".", "﹒": "."})).splitlines())
    boundary = re.compile(r"(?:[。！？!?]|\.(?!\d)|\n)[ \t]*(?:\[E\d+\][ \t]*)*")
    start = 0
    units = []
    for match in boundary.finditer(text):
        units.append(text[start:match.end()])
        start = match.end()
    units.append(text[start:])
    return units


def has_cited_conclusions(text: str) -> bool:
    """Require a marker on each sentence/line; this is not semantic entailment."""
    for unit in _conclusion_units(text):
        content = REFERENCE.sub("", unit)
        if any(character.isalnum() for character in content) and not REFERENCE.search(unit):
            return False
    return True


def _numbers(text: str) -> set:
    text = unicodedata.normalize("NFKC", REFERENCE.sub("", text))
    values = set()
    for token in re.findall(r"[0-9]+(?:,[0-9]+)*(?:\.[0-9]+)?", text):
        integer = token.split(".", 1)[0]
        if "," in integer and not re.fullmatch(r"[0-9]{1,3}(?:,[0-9]{3})+", integer):
            values.add(token)
        else:
            values.add(Decimal(token.replace(",", "")))
    return values


def has_unsupported_numbers(text: str, citations: Sequence[Citation]) -> bool:
    """Limited numeral check, not verification of subject, units or legal meaning."""
    quoted_numbers = {citation.evidence_id: _numbers(citation.quote) for citation in citations}
    for unit in _conclusion_units(text):
        supported = set()
        for marker in REFERENCE.findall(unit):
            supported.update(quoted_numbers.get(marker[1:-1], set()))
        if not _numbers(unit).issubset(supported):
            return True
    return False
