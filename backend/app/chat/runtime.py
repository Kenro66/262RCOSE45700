"""Bind the existing RAG APIs to the current asynchronous backend repository."""
from __future__ import annotations


async def answer_question(question: str, session_id: str | None = None):
    from app.qa.orchestrator import answer
    from app.qa.evidence_completion import complete_evidence
    from app.qa.retriever import retrieve
    from app.storage.repo import get_documents, get_ready_chunk_sources, search_similar

    async def retrieve_question(message: str):
        return await retrieve(message, search_similar=search_similar)

    async def complete_question(hits):
        return await complete_evidence(hits, question=question, load_chunks_fn=get_ready_chunk_sources)

    return await answer(
        question, session_id,
        retrieve_fn=retrieve_question, get_documents_fn=get_documents,
        complete_evidence_fn=complete_question,
    )
