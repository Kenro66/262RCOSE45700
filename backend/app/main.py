"""FastAPI 项目骨架（Issue #4）。

目前只提供 GET /health：每次请求都真实连一次数据库探测——
连接成功且 SQL 查询成功 → 200；连接失败，或连接成功但 SQL 查询失败 → 503，
并在日志里打出明确错误（不静默成功、不出现 500）。任何情况下连接都会正确释放。

启动（详见根目录 README）：
    docker compose --env-file backend/.env up -d postgres        # 在 repo 根目录
    cd backend && pip install -r requirements.txt
    uvicorn app.main:app --reload --env-file .env                # 在 backend 目录；
                                                                 # --env-file 从 .env 读 DATABASE_URL
"""

from __future__ import annotations

import logging
import os

import asyncpg
from fastapi import FastAPI
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("app")

# 读 backend/.env 里的 DATABASE_URL；没设时用与 docker-compose.yml 一致的本地默认值
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://rag:rag@127.0.0.1:5432/p4rag")
CONNECT_TIMEOUT = 5  # 秒；数据库没启动时也让 /health 在几秒内给出明确结果


def _db_host() -> str:
    """日志用：去掉连接串里的凭据部分。"""
    return DATABASE_URL.split("@")[-1]


app = FastAPI(title="Document-RAG-Chatbot", version="0.1.0")


@app.get("/health")
async def health():
    """数据库健康检查，只会返回 200 或 503（不会出现 500）：

    - 数据库连接成功且 SQL 查询成功 → 200（返回版本与 vector 扩展状态）
    - 数据库连接失败               → 503（日志记录明确错误，不静默成功）
    - 数据库连接成功但 SQL 查询失败  → 503（查询失败同样视为数据库不可用）
    任何情况下（包括查询失败、关闭失败）连接都会被正确释放。
    """
    conn = None
    try:
        conn = await asyncpg.connect(DATABASE_URL, timeout=CONNECT_TIMEOUT)
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
    logger.debug("health: database OK (%s, vector=%s)", version, has_vector)
    return {"status": "ok", "database": {"version": version, "vector_extension": has_vector}}
