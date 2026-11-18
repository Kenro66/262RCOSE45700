from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.adapters.embedding import embed_chunks as default_embed_chunks
from app.adapters.config import settings
from app.api.documents import build_documents_router
from app.api.chat import build_chat_router
from app.chat.config import load_rag_settings
from app.chat.persistent.api import build_persistent_chat_router
from app.chat.persistent.store import ChatStore
from app.chat.persistent.runner import ChatRunner
from app.chat.runtime import answer_question
from app.database import DATABASE_URL, close_db, init_db
from app.ingestion.chunker import chunk_pages as default_chunk_pages


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

logging.getLogger("app").setLevel(logging.INFO)
logger = logging.getLogger("app.main")

CONNECT_TIMEOUT = 5


def _asyncpg_url(url: str) -> str:
    """SQLAlchemy 风格连接串 → asyncpg 可直接用的原始连接串
    （asyncpg.connect 只认 postgresql://，不认 postgresql+asyncpg://）。"""
    return url.replace("+asyncpg", "", 1)


ASYNC_PG_URL = _asyncpg_url(DATABASE_URL)


def _db_host() -> str:
    """日志用：去掉连接串里的凭据部分。"""
    return DATABASE_URL.split("@")[-1]


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await init_db()
        yield
    finally:
        runner = getattr(app.state, "chat_runner", None)
        if runner is not None:
            await runner.close()
        await close_db()


def create_app(chunk_pages=None, embed_chunks=None, answer_fn=None, chat_executor=None) -> FastAPI:
    """构建应用；默认连接真实切分、嵌入、检索和问答。

    标题/版本沿用 Issue #4 骨架（Document-RAG-Chatbot 0.1.0）。"""
    rag_settings = load_rag_settings(settings())
    if chunk_pages is None:
        chunk_pages = default_chunk_pages
    if embed_chunks is None:
        embed_chunks = default_embed_chunks
    app = FastAPI(title="Document-RAG-Chatbot", version="0.1.0", lifespan=lifespan)
    app.include_router(build_documents_router(chunk_pages, embed_chunks), prefix="/api")
    if answer_fn is not None or rag_settings.mode == "single":
        app.include_router(build_chat_router(answer_fn if answer_fn is not None else answer_question), prefix="/api")
    else:
        store = ChatStore(rag_settings=rag_settings if chat_executor is None else None)
        runner = ChatRunner(store, executor=chat_executor)
        app.state.chat_runner = runner
        app.include_router(build_persistent_chat_router(store, runner), prefix="/api")

    @app.get("/health")
    async def health():
        """数据库健康检查（保留 Issue #4 行为，每次请求真实连库探测）：

        - 数据库连接成功且 SQL 查询成功 → 200（返回版本与 vector 扩展状态）
        - 数据库连接失败               → 503（日志记录明确错误，不静默成功）
        - 数据库连接成功但 SQL 查询失败  → 503（查询失败同样视为数据库不可用）
        任何情况下（包括查询失败、关闭失败）连接都会被正确释放，不会出现 500。
        """
        conn = None
        try:
            conn = await asyncpg.connect(ASYNC_PG_URL, timeout=CONNECT_TIMEOUT)
            version, has_vector = await conn.fetchrow(
                """
                SELECT current_setting('server_version'),
                       EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')
                """
            )
        except Exception as exc:
            stage = "查询" if conn is not None else "连接"
            logger.error("health: 数据库%s失败 %s — %s", stage, _db_host(), exc)
            return JSONResponse(
                status_code=503,
                content={"status": "error", "database": f"{stage}失败: {exc}"},
            )
        finally:
            if conn is not None:
                try:
                    await conn.close()
                except Exception as close_exc:
                    logger.warning("health: 关闭数据库连接失败 — %s", close_exc)

        logger.debug("health: database OK (%s, vector=%s)", version, has_vector)
        return {"status": "ok", "database": {"version": version, "vector_extension": has_vector}}

    return app


# 首次运行需下载 BAAI/bge-m3（tokenizer + 模型），离线部署请预置 EMBEDDING_CACHE_DIR。
app = create_app()
