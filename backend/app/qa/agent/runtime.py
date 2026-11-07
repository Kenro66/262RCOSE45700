"""Read-only production bindings for the independent Agent (no HTTP wiring)."""

import json
from collections.abc import Callable

from ...adapters.config import settings
from ...adapters.generation import generate_text
from ..evidence_completion import complete_evidence
from ..retriever import retrieve
from .service import agent_answer
from .state import AgentState


class RealRAG:
    """One request's bindings; own engine is disposed without touching shared pools.

    Use as an async context manager. All repository calls are reads; this adapter
    never creates tables, imports documents or saves conversation state.
    """

    def __init__(self, original_question: str, *, conditions: dict | None = None,
                 session_factory=None, embedder: Callable | None = None, top_k: int | None = None):
        self.original_question = original_question
        self.conditions = conditions or {}
        self.session_factory = session_factory
        self.embedder = embedder
        self.top_k = top_k
        self.current_query: str | None = None
        self.engine = None

    async def __aenter__(self):
        if self.session_factory is None:
            from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
            from ...database import DATABASE_URL, _normalize_database_url

            url = _normalize_database_url(settings().get("DATABASE_URL", DATABASE_URL))
            options = {}
            if url.startswith("postgresql+asyncpg://"):
                options["connect_args"] = {
                    "timeout": 8, "command_timeout": 30,
                    "server_settings": {"default_transaction_read_only": "on"},
                }
            self.engine = create_async_engine(url, pool_pre_ping=True, **options)
            self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        return self

    async def __aexit__(self, *exc):
        if self.engine is not None:
            await self.engine.dispose()

    async def retrieve(self, query):
        from ...storage.repo import search_similar

        self.current_query = query

        async def search(vector, *, top_k):
            return await search_similar(vector, top_k=top_k, session_factory=self.session_factory)

        return await retrieve(query, search_similar=search, top_k=self.top_k, embedder=self.embedder)

    async def documents(self, ids):
        from ...storage.repo import get_documents

        return await get_documents(ids, session_factory=self.session_factory)

    async def complete(self, hits):
        from ...storage.repo import get_ready_chunk_sources

        if self.current_query is None:
            raise RuntimeError("Completion requires a preceding retrieval in this request")

        async def load(ids):
            return await get_ready_chunk_sources(ids, session_factory=self.session_factory)

        # Retain original applicability constraints even if a rewrite omits a term.
        context = (self.current_query + "\nOriginal question: " + self.original_question
                   + "\nConditions: " + json.dumps(self.conditions, ensure_ascii=False, sort_keys=True))
        return await complete_evidence(hits, question=context, load_chunks_fn=load)

    def dependencies(self):
        return dict(retrieve_fn=self.retrieve, get_documents_fn=self.documents,
                    generate_fn=generate_text, complete_evidence_fn=self.complete)


async def agent_answer_real(question: str, session_id: str | None = None, *,
                            conditions: dict | None = None, question_id: str | None = None,
                            agent_state: AgentState | None = None):
    """Explicit real-service entry point. No automatic mode switch or fallback."""
    merged = {**(agent_state.conditions if agent_state else {}), **(conditions or {})}
    async with RealRAG(question, conditions=merged) as runtime:
        return await agent_answer(question, session_id, conditions=conditions, question_id=question_id,
                                  agent_state=agent_state, **runtime.dependencies())
