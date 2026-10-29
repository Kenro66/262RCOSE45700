"""完整 e2e：Postgres + pgvector（只在有 Docker 的機器上跑）。

用法（先创建独立issue9_test数据库并启用pgvector，在backend目录）：
    $env:ISSUE9_TEST_DATABASE_URL = "postgresql+asyncpg://rag:TEST_PASSWORD@127.0.0.1:5432/issue9_test"
    $env:ISSUE9_ALLOW_DATABASE_RESET = "1"
    python -m pytest tests/test_e2e_postgres.py -v

TEST_PASSWORD仅为命令示例；不向共享库或业务库执行本测试。

設計要點：全程用「單一事件迴圈」驅動 ASGI（httpx.ASGITransport，不用 TestClient 的
portal 執行緒），並把建表/清空放在測試裡，避免 Windows 上 asyncpg 跨 event loop
重用連線的崩潰（'_proactor.send' / 'attached to a different loop'）。

覆蓋 Issue #7 的 search_similar / get_documents 完成條件：
- 上傳 3 份示例 PDF 後，用 1024 維向量調用 search_similar：返回不超過 top_k 條，
  按 score 從高到低排列
- 刪除一份文件後，其文本塊不再出現在結果裡
- get_documents 只回傳存在的文件
"""

from __future__ import annotations

import asyncio
import os
import random
import sys

import httpx
import pytest
from sqlalchemy import text

# asyncpg 在 Windows 預設 ProactorEventLoop 下、跨執行緒 event loop 會崩，改用
# SelectorEventLoop 更穩（Linux 不受影響）。
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.contracts import Chunk, EmbeddingRecord  # noqa: E402
from app.database import engine, init_db  # noqa: E402
from app.main import create_app  # noqa: E402
from app.storage.repo import (  # noqa: E402
    get_documents,
    next_document_id,
    search_similar,
)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
pytestmark = [
    pytest.mark.skipif(
        not DATABASE_URL.startswith("postgresql"),
        reason="需要独立Postgres测试库：设置ISSUE9_TEST_DATABASE_URL和ISSUE9_ALLOW_DATABASE_RESET=1",
    ),
]


def _fake_chunk_pages(pages) -> list[Chunk]:
    chunks = []
    for page in pages:
        if not page.text.strip():
            continue
        chunks.append(
            Chunk(
                schema_version="1.0",
                chunk_id=f"{page.document_id}_C{len(chunks):06d}",
                document_id=page.document_id,
                chunk_index=len(chunks),
                page_index=page.page_index,
                page_number=page.page_number,
                text=f"chunk of {page.document_id} page {page.page_number}: {page.text.strip()[:80]}",
                section_title=None,
                language=page.language,
                token_count=8,
                char_count=len(page.text),
                content_hash="sha256:e2e",
            )
        )
    return chunks


def _fake_embed(chunks) -> list[EmbeddingRecord]:
    records = []
    for chunk in chunks:
        rng = random.Random(abs(hash(chunk.text)) % (2**32))
        records.append(
            EmbeddingRecord(
                chunk_id=chunk.chunk_id,
                vector=[round(rng.random(), 6) for _ in range(1024)],
            )
        )
    return records


def _make_pdf(name: str, *lines: str) -> bytes:
    import pymupdf

    doc = pymupdf.open()
    for line in lines:
        page = doc.new_page()
        page.insert_text((72, 72), line)
    data = doc.tobytes()
    doc.close()
    return data


async def _reset_db() -> None:
    """建表（冪等）+ 清空，讓 e2e 從乾淨狀態跑。"""
    await init_db()
    async with engine.begin() as conn:
        await conn.execute(text('TRUNCATE "documents", "chunks", "ParsedPage" RESTART IDENTITY CASCADE'))


@pytest.mark.asyncio
async def test_postgres_e2e() -> None:
    """id 序列 + 上傳 → READY → search_similar top_k 有序 → 刪除後 chunk 消失 → get_documents。"""
    await _reset_db()

    # 1) document id 由 Postgres 序列分配：DOCxxx，遞增
    first = await next_document_id()
    second = await next_document_id()
    assert first.startswith("DOC") and second.startswith("DOC")
    assert int(first[3:]) + 1 == int(second[3:])

    # 2) 用單一 event loop 驅動 ASGI，上傳 3 份 PDF
    app = create_app(_fake_chunk_pages, _fake_embed)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # /health（#4 契约）：真实连库成功 → 200 + 版本与 vector 扩展状态
        health = (await client.get("/health")).json()
        assert health["status"] == "ok"
        assert health["database"]["version"]
        assert health["database"]["vector_extension"] is True

        for name, lines in (
            ("alpha.pdf", ("alpha paragraph one", "alpha paragraph two")),
            ("beta.pdf", ("beta paragraph one",)),
            ("gamma.pdf", ("gamma paragraph one", "gamma paragraph two")),
        ):
            response = await client.post(
                "/api/documents",
                files={"file": (name, _make_pdf(name, *lines), "application/pdf")},
            )
            assert response.status_code == 201, response.text

        listing = (await client.get("/api/documents")).json()
        assert {record["status"] for record in listing} == {"READY"}
        assert all(record["page_count"] for record in listing)

        alpha = next(r for r in listing if r["filename"] == "alpha.pdf")
        assert alpha["page_count"] == 2

        # 3) 用一個 1024 維（非零）向量調用 search_similar
        query = [0.25 if i % 3 == 0 else 0.05 for i in range(1024)]
        hits = await search_similar(query, top_k=10)
        assert 0 < len(hits) <= 10
        scores = [hit.score for hit in hits]
        assert scores == sorted(scores, reverse=True)

        # 4) 刪掉 alpha 後，它的 chunk 不該再出現
        delete_response = await client.delete(f"/api/documents/{alpha['document_id']}")
        assert delete_response.status_code == 204
        hits_after = await search_similar(query, top_k=10)
        assert all(hit.document_id != alpha["document_id"] for hit in hits_after)

        # 5) get_documents：只回傳存在的文件
        remaining = (await client.get("/api/documents")).json()
        fetched = await get_documents([r["document_id"] for r in remaining] + ["NOPE"])
        assert set(fetched.keys()) == {r["document_id"] for r in remaining}
