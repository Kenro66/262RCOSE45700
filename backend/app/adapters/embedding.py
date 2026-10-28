"""BGE-M3 dense CPU embeddings. Remote protocol is not yet specified."""

from functools import lru_cache
from math import isfinite
from typing import TYPE_CHECKING

from .config import required, settings

if TYPE_CHECKING:
    from ..contracts import Chunk, EmbeddingRecord


class EmbeddingError(RuntimeError):
    """Embedding configuration, model loading or inference failed."""


@lru_cache(maxsize=1)
def _model(name: str, cache_dir: str | None, cpu_threads: int):
    try:
        import torch
        from FlagEmbedding import BGEM3FlagModel

        torch.set_num_threads(cpu_threads)
        return BGEM3FlagModel(name, devices="cpu", use_fp16=False,
                              cache_dir=cache_dir, trust_remote_code=False)
    except Exception as exc:
        raise EmbeddingError("CPU embedding model could not load; check EMBEDDING_MODEL, dependencies and download access") from exc


def embed_texts(texts: list[str]) -> list[list[float]]:
    if not isinstance(texts, list) or any(not isinstance(t, str) or not t.strip() for t in texts):
        raise ValueError("texts must be a list of non-empty strings")
    if not texts:
        return []
    config = settings()
    try:
        name = required(config, "EMBEDDING_MODEL")
    except ValueError as exc:
        raise EmbeddingError(str(exc)) from exc
    try:
        dimension = int(config.get("EMBEDDING_DIMENSION", "1024"))
        batch = int(config.get("EMBEDDING_BATCH_SIZE", "1"))
        max_length = int(config.get("EMBEDDING_MAX_LENGTH", "8192"))
        cpu_threads = int(config.get("EMBEDDING_CPU_THREADS", "1"))
        if dimension != 1024 or batch <= 0 or cpu_threads <= 0 or not 1 <= max_length <= 8192:
            raise ValueError
    except ValueError:
        raise EmbeddingError("MVP dimension must be 1024; batch size and CPU threads must be positive; max length must be 1..8192") from None
    if config.get("EMBEDDING_BASE_URL", "").strip():
        raise EmbeddingError("Remote embedding service is not configured: NPU API protocol is pending; leave EMBEDDING_BASE_URL blank for CPU")
    try:
        cache_dir = config.get("EMBEDDING_CACHE_DIR", "").strip() or None
        output = _model(name, cache_dir, cpu_threads).encode(texts, batch_size=batch, max_length=max_length,
                                     return_dense=True, return_sparse=False, return_colbert_vecs=False)
        vectors = output["dense_vecs"].tolist()
    except EmbeddingError:
        raise
    except Exception as exc:
        raise EmbeddingError("CPU embedding inference failed") from exc
    try:
        if len(vectors) != len(texts) or any(len(v) != dimension for v in vectors):
            raise ValueError
        if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not isfinite(x) for v in vectors for x in v):
            raise ValueError
        if any(not any(x != 0 for x in v) for v in vectors):
            raise ValueError
        return [[float(x) for x in vector] for vector in vectors]
    except (TypeError, ValueError):
        raise EmbeddingError(f"Expected {len(texts)} finite nonzero embedding vectors with dimension {dimension}") from None


def embed_chunks(chunks: list["Chunk"]) -> list["EmbeddingRecord"]:
    from ..contracts import EmbeddingRecord

    if not isinstance(chunks, list):
        raise ValueError("chunks must be a list of Chunk objects")
    if any(not c.text.strip() for c in chunks):
        raise ValueError("Chunk text must not be empty or whitespace")
    texts = [f"{c.section_title}\n\n{c.text}" if c.section_title else c.text for c in chunks]
    return [EmbeddingRecord(chunk_id=c.chunk_id, vector=v) for c, v in zip(chunks, embed_texts(texts), strict=True)]
