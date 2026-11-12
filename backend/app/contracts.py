"""接口约定 v1（草案）

对应《MVP技术方案》第 5 节。规则（5.7）：
- 物理页码从 1 开始
- 6 种问答状态名固定，不改
- 证据编号（E1、E2……）只在本轮问答内有效
- 接口只加字段，不改名；改本文件要走 PR，提供方和使用方都要审

字段命名分内外两层（10/25 PM 定，待王诗琪、孙忠其、王一霖确认）：
- 内部（ParsedPage、C2、search_similar 的结果）按孙忠其的接口文档：page_number、language
- 对外（C1、C5、C6）：filename、page、lang
- 只在引用校验（qa/citation.py）里把 page_number 转成 page；文件名只有 filename 一种写法
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


# ---------- ParsedPage 单页解析结果（王诗琪提供，孙忠其使用） ----------


class ParsedPage(BaseModel):
    """parse_pdf() 的输出，一页一个。见孙忠其《ParsedPage 接口文档》v1.0。"""

    schema_version: str = "1.0"
    document_id: str
    page_index: int = Field(ge=0)  # 程序内部用，从 0 开始
    page_number: int = Field(ge=1)  # 物理页码，从 1 开始，用于引用
    text: str  # 尽量保留原换行、段落和条款结构；没有文字时为 ""
    language: Optional[str]  # ko / zh / en；无法判断时为 null
    content_hash: Optional[str] = None  # "sha256:..."，整页 text 的哈希
    char_count: Optional[int] = Field(default=None, ge=0)
    parser_version: Optional[str] = None
    blocks: Optional[list[dict[str, Any]]] = None  # MVP 为 null


# ---------- C2 文本块（孙忠其提供，王诗琪存储） ----------


class Chunk(BaseModel):
    """C2：chunk_pages() 的输出，一个文本块不跨页。见孙忠其《Chunking 接口文档》v1.0。"""

    # 标识
    schema_version: str = "1.0"
    chunk_id: str = Field(examples=["DOC001_C000003"])
    document_id: str
    chunk_index: int = Field(ge=0)
    # 页码
    page_index: int = Field(ge=0)  # 从 0 开始
    page_number: int = Field(ge=1)  # 物理页码，从 1 开始
    # 内容
    text: str = Field(min_length=1)  # 空页不生成 chunk
    section_title: Optional[str]  # 无法识别时为 null
    language: Optional[str]  # 无法判断时为 null
    # 统计
    token_count: int = Field(ge=0)
    char_count: Optional[int] = Field(ge=0)
    # 校验
    content_hash: Optional[str]  # 当前 chunk text 的哈希，不是整页的


class EmbeddingRecord(BaseModel):
    """embed_chunks() 的单条输出。见孙忠其《Embedding 接口文档》v1.0。

    模型名、维度等全局配置不在每条记录里重复保存；MVP 为 bge-m3、1024 维【待确认：孙忠其】。
    """

    chunk_id: str  # 与输入的 C2 完全一致
    vector: list[float] = Field(min_length=1)


class SearchHit(BaseModel):
    """search_similar() 的单条结果（王诗琪提供，孙忠其使用）。只查 READY 文件。"""

    chunk_id: str
    document_id: str
    page_number: int = Field(ge=1)
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
