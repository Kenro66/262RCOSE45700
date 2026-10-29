"""数据表模型：documents 文件记录 / ParsedPage 单页解析 / chunks 文本块。

表名与 storage/schema.sql（Issue #4）保持一致：documents、chunks、ParsedPage。
字段与《MVP技术方案》及 backend/app/contracts.py 保持一致：
- 内部（ParsedPage、chunks）按孙忠其接口文档：page_number、language
- 对外（documents）：filename、status、error_message、page_count、uploaded_at
"""

from __future__ import annotations

import os
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

EMBEDDING_DIMENSION = int(os.getenv("EMBEDDING_DIMENSION", "1024"))


class Document(Base):
    """documents：文件记录（王诗琪提供，王一霖使用）。"""

    __tablename__ = "documents"

    document_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    error_message: Mapped[str | None] = mapped_column(Text)
    page_count: Mapped[int | None] = mapped_column(Integer)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    pages: Mapped[list["ParsedPage"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="ParsedPage.page_index",
    )
    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="Chunk.chunk_index",
    )


class ParsedPage(Base):
    """单页解析结果（parse_pdf 输出，王诗琪存储，孙忠其使用）。"""

    __tablename__ = "ParsedPage"
    __table_args__ = (
        UniqueConstraint("document_id", "page_index", name="uq_document_page_index"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("documents.document_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    page_index: Mapped[int] = mapped_column(Integer, nullable=False)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str | None] = mapped_column(String(16))
    content_hash: Mapped[str | None] = mapped_column(String(71))
    char_count: Mapped[int | None] = mapped_column(Integer)
    schema_version: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="1.0"
    )
    parser_version: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default="unknown"
    )
    blocks: Mapped[list[dict] | None] = mapped_column(
        JSON().with_variant(JSONB, "postgresql")
    )
    document: Mapped["Document"] = relationship(back_populates="pages")


class Chunk(Base):
    """chunks：文本块（chunk_pages 输出，王诗琪存储）。"""

    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint(
            "document_id", "chunk_index", name="uq_chunks_document_chunk_index"
        ),
        CheckConstraint("chunk_index >= 0", name="ck_chunks_chunk_index_nonnegative"),
        CheckConstraint("page_index >= 0", name="ck_chunks_page_index_nonnegative"),
        CheckConstraint("page_number >= 1", name="ck_chunks_page_number_positive"),
        CheckConstraint("token_count >= 0", name="ck_chunks_token_count_nonnegative"),
        CheckConstraint(
            "char_count IS NULL OR char_count >= 0",
            name="ck_chunks_char_count_nonnegative",
        ),
        CheckConstraint(
            "language IS NULL OR language IN ('ko', 'zh', 'en')",
            name="ck_chunks_supported_language",
        ),
        Index("ix_chunks_document_page", "document_id", "page_index"),
    )

    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    chunk_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("documents.document_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    page_index: Mapped[int] = mapped_column(Integer, nullable=False)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    section_title: Mapped[str | None] = mapped_column(String(512))
    language: Mapped[str | None] = mapped_column(String(16))
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    char_count: Mapped[int | None] = mapped_column(Integer)
    content_hash: Mapped[str | None] = mapped_column(String(71))
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(EMBEDDING_DIMENSION), nullable=True
    )

    document: Mapped["Document"] = relationship(back_populates="chunks")
