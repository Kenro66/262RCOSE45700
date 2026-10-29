"""存储层数据载体（Issue #7）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator


class ChunkData(BaseModel):
    """C2 文本块的入库载体（字段与 app.contracts.Chunk 一致 + embedding）。"""

    model_config = ConfigDict(from_attributes=True)

    schema_version: str
    chunk_id: str
    document_id: str
    chunk_index: int
    page_index: int
    page_number: int
    text: str
    section_title: str | None
    language: Literal["ko", "zh", "en"] | None
    token_count: int
    char_count: int | None
    content_hash: str | None
    embedding: list[float] | None = None

    @model_validator(mode="after")
    def validate_chunk_id(self) -> "ChunkData":
        expected = f"{self.document_id}_C{self.chunk_index:06d}"
        if self.chunk_id != expected:
            raise ValueError(f"chunk_id must be {expected}")
        return self
