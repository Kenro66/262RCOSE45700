"""Issue #45：验证上传元数据、空值约定及数据库读取。

运行：python -m pytest backend/tests/test.py -q
使用隔离测试库和模拟处理函数，不访问 AWS 或真实向量模型。
"""

import asyncio
import hashlib
import json

import pytest
from sqlalchemy import select

from app.documents import metadata as policy_metadata
from app.documents.pipeline import ProcessingResult
from app.storage.models import Document
from app.storage.repo import get_documents, mark_document_ready
from test_api_documents import build_client, make_pdf_bytes


@pytest.mark.parametrize("registered", [True, False], ids=["registered", "unregistered"])
def test_upload_metadata_post_get_and_database(db, tmp_path, monkeypatch, registered):
    """匹配文件保存四项条件；未匹配文件在首次响应和查询中明确返回四个空值。"""
    content = make_pdf_bytes("Issue 45 metadata regression test.")
    expected = {
        "country": "KR",
        "direction": "BOTH",
        "effective_date": "2026-04-01",
        "applicable_persons": ["GENERAL_TRAVELER", "CREW", "MINOR"],
    }
    # 为本次生成的测试 PDF 登记哈希，不改动正式五份政策文件的登记表。
    registry = tmp_path / "test_registry.json"
    registry.write_text(json.dumps([{
        "source_id": "TEST_ONLY",
        "sha256": hashlib.sha256(content).hexdigest(),
        "metadata": expected,
    }] if registered else []), encoding="utf-8")
    monkeypatch.setattr(policy_metadata, "_REGISTRY", registry)

    async def processor(document_id, pdf_path, filename):
        # 本测试聚焦 C1 契约；模拟后台完成，不运行解析和向量模型。
        await mark_document_ready(document_id, session_factory=db)
        return ProcessingResult(status="READY")

    client = build_client(db, monkeypatch, tmp_path / "uploads", processor)
    # 请求仅包含 PDF；不由前端提交四项条件，文件名也不参与匹配。
    response = client.post("/api/documents", files={
        "file": ("renamed_policy.pdf", content, "application/pdf"),
    })
    assert response.status_code == 201
    posted = response.json()
    assert posted["status"] == "PROCESSING"
    values = expected if registered else dict.fromkeys(expected)
    for field, value in values.items():
        assert field in posted
        assert posted[field] == value

    # 查询列表时仍返回同一份条件，后台状态更新不能丢失元数据。
    response = client.get("/api/documents")
    assert response.status_code == 200
    records = response.json()
    assert len(records) == 1
    assert records[0]["document_id"] == posted["document_id"]
    assert records[0]["status"] == "READY"
    for field, value in values.items():
        assert field in records[0]
        assert records[0][field] == value

    async def verify_persistence():
        # 用新的数据库会话核对真实存储值，避免只验证接口拼接的响应。
        async with db() as session:
            row = await session.scalar(select(Document).where(
                Document.document_id == posted["document_id"]
            ))
            assert row is not None
            assert row.country == values["country"]
            assert row.direction == values["direction"]
            actual_date = row.effective_date.isoformat() if row.effective_date else None
            assert actual_date == values["effective_date"]
            assert row.applicable_persons == values["applicable_persons"]
        # Agent 使用的批量读取接口也应读到相同四项条件。
        documents = await get_documents([posted["document_id"]], session_factory=db)
        record = documents[posted["document_id"]].model_dump(mode="json")
        for field, value in values.items():
            assert record[field] == value

    asyncio.run(verify_persistence())
