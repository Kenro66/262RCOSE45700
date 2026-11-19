"""独立智能体的只读生产服务绑定，不负责网络接口接线。"""

import json
from collections.abc import Callable
from sqlalchemy.ext.asyncio import AsyncEngine

from ...adapters.config import settings
from ...adapters.generation import generate_text
from ...contracts import SearchHit
from ..evidence_completion import complete_evidence
from ..retriever import retrieve
from .service import agent_answer
from .generation import agent_generator
from .state import AgentState, normalize_agent_state
from .conditions import comparison_policy_countries, retrieval_policy_scope


class RealRAG:
    """单次请求的服务绑定；只释放自建引擎，不影响共享连接池。

    通过异步上下文管理器使用。数据库操作均为只读；
    本适配器不建表、不导入文档，也不保存会话状态。
    """

    def __init__(self, original_question: str, *, conditions: dict | None = None,
                 session_factory=None, embedder: Callable | None = None, top_k: int | None = None,
                 document_ids=None):
        self.original_question = original_question
        self.conditions = conditions or {}
        self.session_factory = session_factory
        self.embedder = embedder
        self.top_k = top_k
        self.document_ids = None if document_ids is None else tuple(document_ids)
        self._scope_loaded = False
        self._effective_document_ids = self.document_ids
        self._comparison_document_ids: dict[str, tuple[str, ...]] = {}
        self.current_query: str | None = None
        self.engine: AsyncEngine | None = None

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
        effective = await self._policy_document_ids()
        if effective == ():
            return []  # Empty scope is no evidence, never an unrestricted search.

        async def search(vector, *, top_k):
            if self._comparison_document_ids and top_k >= 2:
                return await self._comparison_search(vector, top_k)
            if effective is None:
                return await search_similar(vector, top_k=top_k, session_factory=self.session_factory)
            return await search_similar(vector, top_k=top_k, session_factory=self.session_factory, document_ids=effective)

        return await retrieve(query, search_similar=search, top_k=self.top_k, embedder=self.embedder)

    async def _comparison_search(self, vector: list[float], top_k: int) -> list[SearchHit]:
        """One embedding/logical Retrieval, country recalls, bounded diverse merge."""
        from ...storage.repo import search_similar

        common = dict(top_k=top_k, session_factory=self.session_factory)
        candidates = list(await search_similar(vector, document_ids=self._effective_document_ids, **common))
        for ids in self._comparison_document_ids.values():
            if ids:
                candidates.extend(await search_similar(vector, document_ids=ids, **common))
        unique: dict[str, SearchHit] = {}
        for hit in candidates:
            if self._effective_document_ids is not None and hit.document_id not in self._effective_document_ids:
                raise ValueError("Comparison search returned a source outside the trusted scope")
            previous = unique.get(hit.chunk_id)
            if previous is not None and (previous.document_id, previous.page_number, previous.text) != (hit.document_id, hit.page_number, hit.text):
                raise ValueError("Stable chunk ID returned inconsistent comparison sources")
            if previous is None or hit.score > previous.score:
                unique[hit.chunk_id] = hit
        ranked = sorted(unique.values(), key=lambda hit: (-hit.score, hit.chunk_id))
        selected: set[str] = set()
        quota = top_k // len(self._comparison_document_ids)
        for ids in self._comparison_document_ids.values():
            selected.update(hit.chunk_id for hit in [h for h in ranked if h.document_id in ids][:quota])
        for hit in ranked:
            if len(selected) >= top_k:
                break
            selected.add(hit.chunk_id)
        return [hit for hit in ranked if hit.chunk_id in selected]

    async def _policy_document_ids(self):
        """Request-local scope; leave the persisted trusted snapshot untouched."""
        if self._scope_loaded:
            return self._effective_document_ids
        scope = retrieval_policy_scope(self.original_question, self.conditions)
        comparison = comparison_policy_countries(self.original_question)
        if scope is not None or comparison:
            from ...storage.repo import get_documents, list_documents

            if self.document_ids is None:
                documents = await list_documents(session_factory=self.session_factory)
            else:
                documents = list((await get_documents(list(self.document_ids), session_factory=self.session_factory)).values())
            # Unknown metadata is not proof of inapplicability. Keep it for the
            # existing evidence/applicability checks rather than guessing.
            self._effective_document_ids = tuple(
                document.document_id for document in documents
                if document.status == "READY" and (scope is None or document.country in (None, scope["destination_country"]))
                and (self.document_ids is None or document.document_id in self.document_ids)
                and (scope is None or "direction" not in scope or document.direction in (None, "BOTH", scope["direction"]))
            )
            if comparison:
                self._comparison_document_ids = {
                    country: tuple(d.document_id for d in documents
                                   if d.document_id in self._effective_document_ids and d.country == country)
                    for country in comparison
                }
        self._scope_loaded = True
        return self._effective_document_ids

    async def documents(self, ids):
        from ...storage.repo import get_documents

        allowed = ids if self.document_ids is None else [item for item in ids if item in self.document_ids]
        return await get_documents(allowed, session_factory=self.session_factory)

    async def complete(self, hits):
        from ...storage.repo import get_ready_chunk_sources

        if self.current_query is None:
            raise RuntimeError("Completion requires a preceding retrieval in this request")
        effective = await self._policy_document_ids()
        scoped_hits = hits if effective is None else tuple(hit for hit in hits if hit.document_id in effective)
        if effective == ():
            return []

        async def load(ids):
            allowed = ids if effective is None else [item for item in ids if item in effective]
            return await get_ready_chunk_sources(allowed, session_factory=self.session_factory)

        # 即使问题改写遗漏了某个词，也保留原始适用条件。
        context = (self.current_query + "\nOriginal question: " + self.original_question
                   + "\nConditions: " + json.dumps(self.conditions, ensure_ascii=False, sort_keys=True))
        return await complete_evidence(scoped_hits, question=context, original_question=self.original_question,
                                       load_chunks_fn=load)

    def dependencies(self):
        return dict(retrieve_fn=self.retrieve, get_documents_fn=self.documents,
                    generate_fn=agent_generator(settings(), generate_text), complete_evidence_fn=self.complete)


async def agent_answer_real(question: str, session_id: str | None = None, *,
                            conditions: dict | None = None, question_id: str | None = None,
                            agent_state: AgentState | dict | None = None):
    """显式调用真实服务的入口，不自动切换模式或执行回退。"""
    saved = normalize_agent_state(agent_state)
    merged = {**saved.conditions, **(conditions or {})}
    async with RealRAG(question, conditions=merged) as runtime:
        return await agent_answer(question, session_id, conditions=conditions, question_id=question_id,
                                  agent_state=saved if isinstance(agent_state, dict) else agent_state,
                                  **runtime.dependencies())
