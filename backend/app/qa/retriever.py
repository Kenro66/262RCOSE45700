"""One asynchronous vector search; database implementation is injected by backend."""

import asyncio
from math import isfinite
from typing import Callable, Protocol, Sequence

from ..adapters.config import settings
from ..contracts import SearchHit


class SearchFunction(Protocol):
    async def __call__(self, query_vector: list[float], *, top_k: int) -> Sequence[SearchHit]: ...


class RetrievalError(RuntimeError):
    """Embedding, backend search, or returned evidence was invalid."""


async def retrieve(question: str, *, search_similar: SearchFunction, top_k: int | None = None,
                   embedder: Callable | None = None) -> list[SearchHit]:
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must be a nonempty string")
    if top_k is None:
        try:
            top_k = int(settings().get("RETRIEVAL_TOP_K", "5"))
        except ValueError as exc:
            raise ValueError("RETRIEVAL_TOP_K must be an integer from 1 to 100") from exc
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 100:
        raise ValueError("top_k must be an integer from 1 to 100")
    if not callable(search_similar):
        raise ValueError("Inject an asynchronous backend search callable")
    if embedder is None:
        from ..adapters.embedding import embed_texts
        embedder = embed_texts
    try:
        vectors = await asyncio.to_thread(embedder, [question])
        if len(vectors) != 1 or len(vectors[0]) != 1024:
            raise ValueError("Expected one 1024-dimensional query vector")
        if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not isfinite(x) for x in vectors[0]) or not any(vectors[0]):
            raise ValueError("Query vector must be finite and nonzero")
        rows = await search_similar(vectors[0], top_k=top_k)
        if not isinstance(rows, (list, tuple)):
            raise ValueError("Backend search must return a list or tuple of SearchHit")
        hits = [SearchHit.model_validate(row) for row in rows]
        if any(not hit.chunk_id.strip() or not hit.document_id.strip() or not hit.text.strip() or not isfinite(hit.score) for hit in hits):
            raise ValueError("Backend search returned invalid evidence")
        return hits[:top_k]
    except Exception as exc:
        raise RetrievalError("Question embedding or backend search failed; no results fabricated") from exc
