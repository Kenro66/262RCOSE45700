"""Validated deployment settings; no implicit RAG mode or execution deadline."""

from dataclasses import dataclass
from typing import Literal, Mapping, cast

from app.adapters.config import settings


@dataclass(frozen=True)
class RagSettings:
    mode: Literal["single", "agent"]
    execution_timeout_seconds: int


def load_rag_settings(values: Mapping[str, str] | None = None) -> RagSettings:
    config = settings() if values is None else values
    mode = config.get("RAG_MODE")
    if mode not in {"single", "agent"}:
        raise ValueError("RAG_MODE must be set to exactly single or agent")
    timeout = config.get("RAG_EXECUTION_TIMEOUT_SECONDS", "")
    if not isinstance(timeout, str) or not timeout.isascii() or not timeout.isdecimal() or int(timeout) <= 0:
        raise ValueError("RAG_EXECUTION_TIMEOUT_SECONDS must be a positive integer")
    return RagSettings(mode=cast(Literal["single", "agent"], mode), execution_timeout_seconds=int(timeout))
