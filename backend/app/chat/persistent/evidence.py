"""Canonical source cache and exact evidence-token mappings for managed runs."""

import re

from app.qa.agent.state import AgentState, CachedEvidence
from app.qa.citation import Evidence


def merge_sources(state: AgentState, sources) -> AgentState:
    cache = list(state.evidence)
    by_chunk = {item.hit.chunk_id: item.hit for item in cache}
    for source in sources:
        hit = source.hit if isinstance(source, (CachedEvidence, Evidence)) else source
        previous = by_chunk.get(hit.chunk_id)
        if previous is not None:
            if (previous.document_id, previous.page_number, previous.text) != (hit.document_id, hit.page_number, hit.text):
                raise ValueError("Conflicting original source for stable chunk ID")
            continue
        cache.append(CachedEvidence(evidence_id=f"E{len(cache) + 1}", hit=hit.model_copy(deep=True)))
        by_chunk[hit.chunk_id] = hit
    return AgentState.model_validate({**state.model_dump(), "evidence": cache,
                                     "evidence_ids": [item.evidence_id for item in cache]})


def remap_tokens(text: str, mapping: dict[str, str]) -> str:
    return re.sub(r"\[(E[1-9]\d*)\]", lambda match: "[" + mapping.get(match[1], match[1]) + "]", text)
