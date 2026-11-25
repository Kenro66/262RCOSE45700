"""Bind the existing RAG APIs to the current asynchronous backend repository."""
from __future__ import annotations

from app.adapters.generation import capture_generation_models
from app.contracts import ChatResponse, ChatStatus


async def answer_question(question: str, session_id: str | None = None) -> ChatResponse:
    from app.qa.orchestrator import answer
    from app.qa.evidence_completion import complete_evidence
    from app.qa.retriever import retrieve
    from app.storage.repo import get_documents, get_ready_chunk_sources, search_similar

    async def retrieve_question(message: str):
        return await retrieve(message, search_similar=search_similar)

    async def complete_question(hits):
        return await complete_evidence(hits, question=question, load_chunks_fn=get_ready_chunk_sources)

    with capture_generation_models() as models:
        response = await answer(
            question, session_id,
            retrieve_fn=retrieve_question, get_documents_fn=get_documents,
            complete_evidence_fn=complete_question,
        )
    response.model_name = models[-1] if response.status == ChatStatus.ANSWERED and models else None
    return response
