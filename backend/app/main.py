"""应用入口（Issue #7）。

启动（先创建 backend/.env，见根目录 README）：
    docker compose --env-file backend/.env up -d postgres   # 在 repo 根目录
    cd backend && uvicorn app.main:app --env-file .env      # 从 .env 读 DATABASE_URL

DATABASE_URL 未设置时默认连本机 compose Postgres（rag/p4rag）；
本项目只支持 Postgres，无 SQLite 回退——数据库没启动时启动会直接报错。
连接串兼容 Issue #4 模板的 postgresql:// 写法（见 app.database._normalize_database_url）。

GET /health（保留 Issue #4 行为）：每次请求真实连库探测——成功 200，
连接/查询失败 503（日志有明确错误；连接任何情况都会释放，不会出现 500）。

chunk_pages 为随包附带的 Issue #11 集成依赖（尚未合入基准 main）；
embed_chunks 为已合并 Issue #10 的真实实现：
  - chunk_pages：app/ingestion/chunker.py —— policy-structure-first 切分
    （BGE-M3 fast tokenizer，≤500 tokens/chunk、75 token 重叠、不跨页；
    依赖 transformers 包与 BAAI/bge-m3 tokenizer，首次运行自动下载，
    离线部署请预置于 EMBEDDING_CACHE_DIR）
  - embed_chunks：app/adapters/embedding.py —— BGE-M3 CPU 同步实现

两者均需在 backend/.env 设置 EMBEDDING_MODEL（BAAI/bge-m3）与
EMBEDDING_DIMENSION=1024 等参数（见 .env.example 模型段）。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.adapters.embedding import embed_chunks  # Issue #10：BGE-M3 CPU（已合入 main）
from app.api.documents import build_documents_router
from app.database import DATABASE_URL, close_db, init_db
from app.ingestion.chunker import chunk_pages  # 附带的 Issue #11 集成依赖，尚未合入本次基准 main

# Issue #4 骨架行为保留：日誌初始化（与已合并 Issue #4 的 main.py 一致）
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

logger = logging.getLogger("app.main")

CONNECT_TIMEOUT = 5  # 秒；数据库没启动时也让 /health 在几秒内给出明确结果


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
        await close_db()


def create_app(chunk_pages, embed_chunks) -> FastAPI:
    """构建应用；注入切分/向量化实现（Issue #10 / #11）。

    标题/版本沿用 Issue #4 骨架（Document-RAG-Chatbot 0.1.0）。"""
    app = FastAPI(title="Document-RAG-Chatbot", version="0.1.0", lifespan=lifespan)
    app.include_router(build_documents_router(chunk_pages, embed_chunks), prefix="/api")

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
                except Exception as close_exc:  # 关闭失败不掩盖原始错误
                    logger.warning("health: 关闭数据库连接失败 — %s", close_exc)
        # Issue #4 骨架行为保留
        logger.debug("health: database OK (%s, vector=%s)", version, has_vector)
        return {"status": "ok", "database": {"version": version, "vector_extension": has_vector}}

    return app


# 切分（Issue #11，app/ingestion/chunker.py）与向量化（Issue #10，app/adapters/embedding.py）
# 均为真实实现；首次运行需下载 BAAI/bge-m3（tokenizer + 模型），离线部署请预置 EMBEDDING_CACHE_DIR。
app = create_app(chunk_pages, embed_chunks)
