from __future__ import annotations

import logging
import os

import asyncpg
from fastapi import FastAPI
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("app")

# 从进程环境读取连接串；启动时由 uvicorn --env-file .env 加载配置。
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://rag:rag@127.0.0.1:5432/p4rag")
CONNECT_TIMEOUT = 5  # 秒；数据库没启动时也让 /health 在几秒内给出明确结果


def _db_host() -> str:
    """日志用：去掉连接串里的凭据部分。"""
    return DATABASE_URL.split("@")[-1]


app = FastAPI(title="Document-RAG-Chatbot", version="0.1.0")


@app.get("/health")
async def health():
    """数据库健康检查：库在 → 200；库没启动 → 503 + 明确错误（日志同步记录）。"""
    try:
        conn = await asyncpg.connect(DATABASE_URL, timeout=CONNECT_TIMEOUT)
    except Exception as exc:
        logger.error("health: 无法连接数据库 %s — %s", _db_host(), exc)
        return JSONResponse(
            status_code=503,
            content={"status": "error", "database": f"unreachable: {exc}"},
        )
    try:
        version, has_vector = await conn.fetchrow(
            """
            SELECT current_setting('server_version'),
                   EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')
            """
        )
    finally:
        await conn.close()
    logger.debug("health: database OK (%s, vector=%s)", version, has_vector)
    return {"status": "ok", "database": {"version": version, "vector_extension": has_vector}}
