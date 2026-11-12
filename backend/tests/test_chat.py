"""POST /api/chat（Issue #9 完成条件，SQLite 验证；answer() 用 fake 注入）。

设计要点：与 test_e2e_postgres.py 相同，全程用「单一事件循环」驱动 ASGI
（httpx.ASGITransport，不用 TestClient 的 portal 线程），避免跨 event loop
崩溃。answer() 是孙忠其 Issue #12 的交付物，这里用 fake 验证 #9 的编排逻辑
（READY 前置检查 / SYSTEM_ERROR 兜底 / 结果透传）；真实模型联调（姜超冒烟题）
在 #12 接入后进行（见 README 完成条件对照表）。
"""

from __future__ import annotations

import logging

import httpx
import pytest
from fastapi import FastAPI

from app.chat.service import AnswerResult, create_chat_router
from app.contracts import Citation
from app.storage.repo import (
    create_processing_document,
    mark_document_failed,
    mark_document_ready,
    replace_document_chunks,
)
from app.storage.schemas import ChunkData

LOGGER_NAME = "app.chat.service"


def _make_chunk(document_id: str, index: int = 0) -> ChunkData:
    return ChunkData(
        schema_version="1.0",
        chunk_id=f"{document_id}_C{index:06d}",
        document_id=document_id,
        chunk_index=index,
        page_index=0,
        page_number=1,
        text="chunk text",
        section_title=None,
        language="en",
        token_count=2,
        char_count=10,
        content_hash="sha256:test",
        embedding=[0.1] * 1024,
    )


async def _seed_ready_document(
    db, document_id: str, filename: str = "sample.pdf"
) -> None:
    """建 C1 → 写一个 C2 → 置 READY（不经过上传 API，chat 不碰 uploads/）。"""
    await create_processing_document(document_id, filename, session_factory=db)
    await replace_document_chunks(
        document_id, [_make_chunk(document_id)], session_factory=db
    )
    await mark_document_ready(document_id, session_factory=db)


def _build_app_for(db, answer_fn) -> FastAPI:
    """用测试 db 组装只含 /api/chat 的应用。"""
    app = FastAPI()
    app.include_router(
        create_chat_router(answer_fn, session_factory=db), prefix="/api"
    )
    return app


# ---------------------------------------------------------------------------
# 测试客户端：单一 event loop 驱动 ASGI
# ---------------------------------------------------------------------------


async def _post_chat(app: FastAPI, message: str, session_id: str | None = None, expected_status: int = 200) -> dict:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/chat",
            json={"session_id": session_id, "message": message},
        )
        assert response.status_code == expected_status, response.text
        return response.json()


# ---------------------------------------------------------------------------
# 完成条件 1：删掉所有文件后提问 → NO_READY_DOCUMENTS，且没有调用模型（日志可见）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_no_ready_documents_no_model_call(db, caplog) -> None:
    """库里没有文件（全部删除后）：NO_READY_DOCUMENTS，answer() 未被调用，日志可见。"""
    calls: list[str] = []

    async def fake_answer(message: str, session_id=None) -> AnswerResult:
        calls.append(message)
        return AnswerResult(status="ANSWERED", answer="不应该到这里")

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        body = await _post_chat(_build_app_for(db, fake_answer), "文档里写了什么？")

    assert body["status"] == "NO_READY_DOCUMENTS"
    assert body["answer"] is None
    assert body["citations"] == []
    assert body["message"]
    assert calls == []  # answer() 没有被调用（无模型调用）
    assert any("不调用 answer()" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_chat_no_ready_when_only_processing_or_failed(db) -> None:
    """只有 PROCESSING / FAILED 文件（没有 READY）：同样 NO_READY_DOCUMENTS。"""
    called = {"n": 0}

    async def fake_answer(message: str, session_id=None) -> AnswerResult:
        called["n"] += 1
        return AnswerResult(status="ANSWERED", answer="x")

    await create_processing_document("DOC001", "processing.pdf", session_factory=db)
    await create_processing_document("DOC002", "failed.pdf", session_factory=db)
    await mark_document_failed("DOC002", "解析失败", session_factory=db)

    body = await _post_chat(_build_app_for(db, fake_answer), "文档里写了什么？")

    assert body["status"] == "NO_READY_DOCUMENTS"
    assert called["n"] == 0


# ---------------------------------------------------------------------------
# 完成条件 2：模型不可用（answer() 抛异常）→ SYSTEM_ERROR，不是 INSUFFICIENT_EVIDENCE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_system_error_when_answer_raises(db) -> None:
    """有 READY 文件但 answer() 抛异常（模拟 .env 模型地址配错）：SYSTEM_ERROR。"""

    async def broken_answer(message: str, session_id=None) -> AnswerResult:
        raise RuntimeError("Connection refused: 模型服务不可达（模拟错误地址）")

    await _seed_ready_document(db, "DOC001", "sample.pdf")
    body = await _post_chat(_build_app_for(db, broken_answer), "文档里写了什么？", expected_status=500)

    assert body["status"] == "SYSTEM_ERROR"
    assert body["status"] != "INSUFFICIENT_EVIDENCE"
    assert body["answer"] is None
    assert body["citations"] == []
    assert body["message"]


# ---------------------------------------------------------------------------
# 完成条件 3（编排层）：3 份 READY 文件 + 可回答题 → ANSWERED，citations 非空
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_answered_with_citations(db) -> None:
    """3 份 READY 文件：answer() 正常返回 → ANSWERED，citations 每条有 filename/page/quote。"""

    async def fake_answer(message: str, session_id=None) -> AnswerResult:
        return AnswerResult(
            status="ANSWERED",
            answer="示例文档规定处理时限为 30 天。",
            citations=[
                Citation(evidence_id="E1", filename="alpha.pdf", page=2, quote="处理时限为 30 天"),
                Citation(evidence_id="E2", filename="gamma.pdf", page=1, quote="详见第一章"),
            ],
        )

    for doc_id, name in (
        ("DOC001", "alpha.pdf"),
        ("DOC002", "beta.pdf"),
        ("DOC003", "gamma.pdf"),
    ):
        await _seed_ready_document(db, doc_id, name)

    body = await _post_chat(_build_app_for(db, fake_answer), "处理时限是多少天？")

    assert body["status"] == "ANSWERED"
    assert body["answer"]
    assert len(body["citations"]) == 2
    for citation in body["citations"]:
        assert citation["filename"]  # 文件名
        assert isinstance(citation["page"], int) and citation["page"] >= 1  # 物理页码
        assert citation["quote"]  # 证据原文


# ---------------------------------------------------------------------------
# 状态归属：INSUFFICIENT_EVIDENCE 由 answer() 判断，#9 原样透传
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_insufficient_evidence_passthrough(db) -> None:
    """answer() 判定证据不足：原样透传 INSUFFICIENT_EVIDENCE（不是 SYSTEM_ERROR）。"""

    async def fake_answer(message: str, session_id=None) -> AnswerResult:
        return AnswerResult(
            status="INSUFFICIENT_EVIDENCE",
            message="现有文件中没有足够的证据回答该问题。",
        )

    await _seed_ready_document(db, "DOC001", "sample.pdf")
    body = await _post_chat(_build_app_for(db, fake_answer), "文档里有没有提到预算？")

    assert body["status"] == "INSUFFICIENT_EVIDENCE"
    assert body["citations"] == []
    assert body["message"]


# ---------------------------------------------------------------------------
# 注入兼容性：同步实现的 answer() 也能用（与 pipeline 的 _maybe_await 同理）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_sync_answer_fn_also_works(db) -> None:
    def sync_answer(message: str, session_id=None) -> AnswerResult:
        return AnswerResult(status="ANSWERED", answer="同步实现也 OK", citations=[Citation(evidence_id="E1", filename="sample.pdf", page=1, quote="chunk text")])

    await _seed_ready_document(db, "DOC001", "sample.pdf")
    body = await _post_chat(_build_app_for(db, sync_answer), "你好")

    assert body["status"] == "ANSWERED"
    assert body["answer"] == "同步实现也 OK"


# ---------------------------------------------------------------------------
# session_id 透传（#12 PR #30 确认 answer() 带 session_id 参数；MVP 不启用历史，
# 但要求非空字符串或 None——空串由本服务归一化为 None）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_session_id_passthrough_and_empty_normalized(db) -> None:
    """session_id 原样透传给 answer()；空串归一化为 None。"""
    calls: list[tuple[str, str | None]] = []

    async def fake_answer(message: str, session_id=None) -> AnswerResult:
        calls.append((message, session_id))
        return AnswerResult(status="ANSWERED", answer="ok", citations=[Citation(evidence_id="E1", filename="sample.pdf", page=1, quote="chunk text")])

    await _seed_ready_document(db, "DOC001", "sample.pdf")

    body = await _post_chat(_build_app_for(db, fake_answer), "你好", session_id="sess-001")
    assert body["status"] == "ANSWERED"
    assert calls == [("你好", "sess-001")]

    await _post_chat(_build_app_for(db, fake_answer), "你好", session_id="")
    assert calls[-1] == ("你好", None)

    await _post_chat(_build_app_for(db, fake_answer), "你好", session_id=None)
    assert calls[-1] == ("你好", None)
