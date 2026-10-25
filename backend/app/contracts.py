"""接口约定 v1（草案）

对应《MVP技术方案》第 5 节。规则（5.7）：
- 物理页码从 1 开始
- 6 种问答状态名固定，不改
- 证据编号（E1、E2……）只在本轮问答内有效
- 接口只加字段，不改名；改本文件要走 PR，提供方和使用方都要审

字段命名暂按对外接口统一为 filename、page、lang【待确认：王诗琪、孙忠其、王一霖】。
"""

from enum import Enum
from typing import Any, Optional

from pydantic import AwareDatetime, BaseModel, Field


# ---------- 状态 ----------


class DocumentStatus(str, Enum):
    """文件处理状态（《MVP技术方案》3.1）。只会从 PROCESSING 变成 READY 或 FAILED。"""

    PROCESSING = "PROCESSING"
    READY = "READY"
    FAILED = "FAILED"


class ChatStatus(str, Enum):
    """问答状态（《MVP技术方案》5.3）。"""

    ANSWERED = "ANSWERED"  # MVP，孙忠其判断
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"  # MVP，孙忠其判断
    NO_READY_DOCUMENTS = "NO_READY_DOCUMENTS"  # MVP，王诗琪判断
    SYSTEM_ERROR = "SYSTEM_ERROR"  # MVP，王诗琪判断
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"  # 第一批
    CONFLICTING_EVIDENCE = "CONFLICTING_EVIDENCE"  # 第一批


# ---------- C1 文件记录（王诗琪提供，王一霖使用） ----------


class DocumentRecord(BaseModel):
    """C1：POST /api/documents 和 GET /api/documents 返回的文件记录。"""

    document_id: str = Field(examples=["doc_7f3a"])
    filename: str  # 原始文件名
    status: DocumentStatus
    error_message: Optional[str] = None  # FAILED 时写原因
    page_count: Optional[int] = Field(default=None, ge=1)  # READY 后才有
    uploaded_at: AwareDatetime  # ISO 8601，带时区


# ---------- C2 文本块（孙忠其提供，王诗琪存储） ----------


class Chunk(BaseModel):
    """C2：chunk_pages() 的输出，一个文本块不跨页。

    字段以孙忠其的接口文档（ParsedPage、Chunking、Embedding）为准；
    page、lang 两个名字是按对外接口统一后的写法【待确认：孙忠其】。
    """

    # 标识
    schema_version: str
    chunk_id: str
    document_id: str
    chunk_index: int = Field(ge=0)
    # 页码
    page_index: int = Field(ge=0)  # 【待确认：是否从 0 开始】
    page: int = Field(ge=1)  # 物理页码；孙忠其文档原名 page_number
    # 内容
    text: str
    section_title: Optional[str] = None
    lang: str  # 孙忠其文档原名 language
    # 统计
    token_count: int = Field(ge=0)
    char_count: int = Field(ge=0)
    # 校验
    content_hash: str


class SearchHit(BaseModel):
    """search_similar() 的单条结果（王诗琪提供，孙忠其使用）。只查 READY 文件。"""

    chunk_id: str
    document_id: str
    page: int = Field(ge=1)  # 《MVP技术方案》5.4 原写 page_number
    text: str
    score: float


# ---------- C5 问答（王诗琪提供，王一霖、姜超使用） ----------


class ChatRequest(BaseModel):
    """C5 请求：POST /api/chat。"""

    session_id: Optional[str] = None
    message: str = Field(min_length=1)


class Citation(BaseModel):
    """C5 中的一条出处。"""

    evidence_id: str = Field(examples=["E1"])
    filename: str
    page: int = Field(ge=1)  # 物理页码
    quote: str  # 证据原文


class ChatResponse(BaseModel):
    """C5 响应。"""

    status: ChatStatus
    answer: Optional[str] = None
    citations: list[Citation] = Field(default_factory=list)
    message: Optional[str] = None  # 给用户看的提示，例如拒答原因


# ---------- C6 评测题（姜超） ----------


class GoldEvidence(BaseModel):
    filename: str
    page: int = Field(ge=1)
    quote: str


class EvalQuestion(BaseModel):
    """C6：eval/questions.jsonl 的一行。运行结果另记在 results.csv。"""

    qid: str
    qtype: str = Field(examples=["QType-01"])
    lang: str
    question: str
    answerable: bool
    gold_answer: Optional[str] = None  # 【待确认：不可回答题是否为 null】
    conditions: Any = None  # 【待确认：姜超确认类型（列表还是对象）】
    gold_evidence: list[GoldEvidence] = Field(default_factory=list)
