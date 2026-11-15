"""依据 README 上传规则和 C1/C5 契约补充边界测试，不调用真实模型。"""

import asyncio
import threading
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.contracts import DocumentRecord
from app.documents.metadata import PolicyMetadata
from app.documents.pipeline import ProcessingResult
from app.qa.orchestrator import _generate
from app.storage.repo import mark_document_ready
from test_api_documents import build_client


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["sync", "async", "callable", "returns_awaitable"])
async def test_req_c5_generator_accepts_supported_callable_shapes(kind):
    """C5：同步、异步和可调用对象返回同一文本，同步计算不能阻塞事件循环。"""
    loop_thread = threading.get_ident()
    calls = []

    def sync(messages):
        calls.append((messages, threading.get_ident()))
        return "按原文办理。[E1]"

    async def asynchronous(messages):
        calls.append((messages, threading.get_ident()))
        return "按原文办理。[E1]"

    class Callable:
        async def __call__(self, messages):
            return await asynchronous(messages)

    def returns_awaitable(messages):
        return asynchronous(messages)

    function = {"sync": sync, "async": asynchronous, "callable": Callable(),
                "returns_awaitable": returns_awaitable}[kind]
    messages = [{"role": "user", "content": "如何办理？"}]
    assert await _generate(function, messages) == "按原文办理。[E1]"
    assert calls[0][0] == messages
    assert (calls[0][1] != loop_thread) == (kind == "sync")


@pytest.mark.parametrize("failure", ["empty", "oversize", "long_name"])
def test_req_upload_invalid_input_leaves_no_file_record_or_processing(db, tmp_path, monkeypatch, failure):
    """README：空文件、超过 25 MiB、超过 255 字符文件名必须拒绝，且无残留。"""
    processor = AsyncMock(return_value=ProcessingResult(status="READY"))
    client = build_client(db, monkeypatch, tmp_path, processor)
    name, data = "policy.pdf", b"%PDF-1.7\ncontent"
    if failure == "empty":
        data = b""
    elif failure == "oversize":
        data = b"%PDF-" + b"x" * (25 * 1024 * 1024 - 4)
    else:
        name = "a" * 252 + ".pdf"
    response = client.post("/api/documents", files={"file": (name, data, "application/pdf")})
    assert response.status_code == 400
    assert client.get("/api/documents").json() == []
    assert list(tmp_path.iterdir()) == []
    processor.assert_not_awaited()


def test_req_upload_exact_size_limit_and_legacy_pdf_mime_are_accepted(db, tmp_path, monkeypatch):
    """README：不超过 25 MiB；文件服务同时接受 application/x-pdf。"""
    async def processor(document_id, pdf_path, filename):
        await mark_document_ready(document_id, session_factory=db)
        return ProcessingResult(status="READY")

    client = build_client(db, monkeypatch, tmp_path, processor)
    data = b"%PDF-" + b"x" * (25 * 1024 * 1024 - 5)
    response = client.post("/api/documents", files={"file": ("a" * 251 + ".pdf", data, "application/x-pdf")})
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "PROCESSING"
    assert len(body["filename"]) == 255
    assert client.get("/api/documents").json()[0]["status"] == "READY"
    assert (tmp_path / (body["document_id"] + ".pdf")).read_bytes() == data


@pytest.mark.parametrize("field,value", [
    ("country", "US"), ("direction", "UNKNOWN"), ("applicable_persons", ["UNKNOWN"]),
    ("effective_date", "not-a-date"),
])
def test_req_metadata_rejects_values_outside_public_contract(field, value):
    """C1/Issue45：数据库与对外协议的适用条件不能接受未知枚举或无效日期。"""
    with pytest.raises(ValidationError):
        PolicyMetadata.model_validate({field: value})


@pytest.mark.parametrize("person", ["GENERAL_TRAVELER", "RESIDENT_TRAVELER", "NON_RESIDENT_TRAVELER",
                                   "FREQUENT_TRAVELER", "CREW", "MINOR"])
def test_req_metadata_each_supported_person_survives_storage_and_public_contract(db, person):
    """C1/Issue45：六种适用对象及日期经真实数据库读写后保持不变。"""
    from app.storage.repo import create_processing_document, get_documents

    async def exercise():
        metadata = PolicyMetadata.model_validate({"country": "KR", "direction": "BOTH",
                     "effective_date": "2026-01-01", "applicable_persons": [person]})
        await create_processing_document("DOC_REQ", "policy.pdf", session_factory=db, metadata=metadata)
        record = (await get_documents(["DOC_REQ"], session_factory=db))["DOC_REQ"]
        assert isinstance(record, DocumentRecord)
        assert record.country == "KR" and record.direction == "BOTH"
        assert record.effective_date.isoformat() == "2026-01-01"
        assert record.applicable_persons == [person]
    asyncio.run(exercise())
