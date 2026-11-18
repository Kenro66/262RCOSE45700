"""仅供 #61 页面联调：真实 HTTP/Agent/PostgreSQL，只替换生成与嵌入模型。

必须显式提供专用 issue68_test* 数据库；控制端点不属于生产应用。
运行方法见 docs/interfaces/issue68-integration.md。
"""
import asyncio
from contextlib import asynccontextmanager
import json
import os
import threading
from urllib.parse import urlparse

url = os.environ.get("ISSUE68_LIVE_DATABASE_URL", "")
if not urlparse(url).path.removeprefix("/").startswith("issue68_test"):
    raise RuntimeError("联调必须使用专用 issue68_test* PostgreSQL 数据库")
os.environ["DATABASE_URL"] = url
os.environ.setdefault("RAG_MODE", "agent")
os.environ["EVIDENCE_COMPLETION_ENABLED"] = "false"

from fastapi import Request
from sqlalchemy import select, func
import uvicorn

import app.adapters.embedding as embedding
import app.adapters.generation as generation
import app.qa.agent.runtime as runtime
import app.qa.orchestrator as orchestrator
from app.database import SessionFactory
from app.main import create_app
from app.storage.models import Document
from app.storage.repo import create_processing_document, mark_document_ready, replace_document_chunks
from app.storage.schemas import ChunkData
from app.chat.persistent.models import ChatQuestion, ChatSession, ChatSubmission

release = threading.Event()
release.set()
entered = threading.Event()
embedding_calls = 0


def embed(texts):
    global embedding_calls
    embedding_calls += 1
    entered.set()
    if not release.wait(20):
        raise TimeoutError("联调未释放检索屏障")
    return [[0.1] * 1024 for _ in texts]


def generate(messages):
    data = json.loads(messages[-1]["content"])
    task = data["task"]
    if task in {"check_conditions", "check_applicability"}:
        missing = task == "check_conditions" and "直接" not in data["question"] and not data["conditions"]
        result = {"missing_conditions": [{"field": "destination_country", "question": "准备入境哪个国家？", "type": "select", "options": ["CN", "KR"]}] if missing else []}
    elif task == "check_evidence":
        support = [{"chunk_id": data["evidence"][0]["source"]["chunk_id"]}]
        supported = {"status": "supported", "affects_core": True, "reason": "联调原文支持结论", "support": support}
        optional = {"status": "not_required", "affects_core": False, "reason": "无额外定义", "support": []}
        sufficient = "三次检索" not in data["question"] or data["retrieval_count"] >= 3
        result = {"verdict": "SUFFICIENT" if sufficient else "MISSING_CRITICAL", "core_claim": "物品需要申报", "core_support": support, "definitions": optional, "exceptions": optional, "applicability": supported, "complete_answer": supported, "missing_evidence": [] if sufficient else ["联调检索下一条证据"]}
    elif task == "rewrite_query":
        result = {"query": data["question"] + " 补充检索 " + str(data["retrieval_count"])}
    elif task == "check_conflict":
        result = {"conflict": None}
    elif task == "generate_answer":
        result = {"answer": "物品需要申报。[E1]", "insufficient_evidence": False}
    else:
        raise AssertionError(task)
    return json.dumps(result, ensure_ascii=False)


embedding.embed_texts = embed
runtime.generate_text = generate
generation.generate_text = generate
orchestrator.generate_text = lambda _: '{"answer":"物品需要申报。[E1]","insufficient_evidence":false}'
application = create_app()
original_lifespan = application.router.lifespan_context


@asynccontextmanager
async def lifespan(app):
    async with original_lifespan(app):
        async with SessionFactory() as session:
            exists = await session.get(Document, "LIVE001")
        if exists is None:
            await create_processing_document("LIVE001", "live-policy.pdf")
            await replace_document_chunks("LIVE001", [ChunkData(
                schema_version="1.0", document_id="LIVE001", chunk_id="LIVE001_C000000", chunk_index=0,
                page_index=1, page_number=2, text="物品需要申报。", section_title=None, language="zh",
                token_count=8, char_count=8, content_hash="live-fixed", embedding=[0.1] * 1024)])
            await mark_document_ready("LIVE001")
        yield


application.router.lifespan_context = lifespan


@application.post("/__test/control")
async def control(request: Request):
    values = await request.json()
    entered.clear()
    release.clear() if values.get("block") else release.set()
    return {"embedding_calls": embedding_calls}


@application.get("/__test/state")
async def state(question_id: str = "", session_id: str = ""):
    async with SessionFactory() as session:
        question = await session.get(ChatQuestion, question_id) if question_id else None
        chat = await session.get(ChatSession, session_id) if session_id else None
        count = await session.scalar(select(func.count()).select_from(ChatSubmission).where(ChatSubmission.question_id == question_id))
        return {"pid": os.getpid(), "entered": entered.is_set(), "embedding_calls": embedding_calls,
                "state": question.state if question else None, "question_status": question.status if question else None,
                "deleted": chat.deleted if chat else None, "submissions": count}


@application.post("/__test/stop")
async def stop():
    server.should_exit = True
    return {"stopping_pid": os.getpid()}


if __name__ == "__main__":
    server = uvicorn.Server(uvicorn.Config(application, host="127.0.0.1", port=8000, log_level="warning"))
    asyncio.run(server.serve())
