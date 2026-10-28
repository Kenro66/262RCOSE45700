"""数据库连接与会话工厂（Issue #7 数据层基础设施）。

DATABASE_URL 未设置时，默认连本机 docker compose 里的 Postgres + pgvector
（rag:rag@127.0.0.1:5432/p4rag，与 docker-compose.yml 及 Issue #4 骨架一致）；
正常启动前请先创建 backend/.env 并以 uvicorn --env-file .env 启动。
连接串兼容 Issue #4 模板：postgresql:// 与 postgresql+asyncpg:// 两种写法都可用
（见 _normalize_database_url）。
本项目只支持 Postgres（含 pgvector）——不提供 SQLite 回退：
测试如需 SQLite，由 tests/conftest.py 显式设置 DATABASE_URL，与本默认值无关。
"""

from __future__ import annotations

import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncAttrs, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


def _normalize_database_url(url: str) -> str:
    """兼容 Issue #4 的配置格式：连接串写成 postgresql://（未带驱动后缀）时
    自动补为 postgresql+asyncpg://；已是 postgresql+asyncpg:// 的原样使用。
    （SQLAlchemy 异步引擎必须带驱动，Issue #4 的 .env 模板直接可用。）
    """
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


DATABASE_URL = _normalize_database_url(
    os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://rag:rag@127.0.0.1:5432/p4rag",
    )
)
engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
SessionFactory = async_sessionmaker(engine, expire_on_commit=False)


class Base(AsyncAttrs, DeclarativeBase):
    pass


async def init_db() -> None:
    """建表（含 pgvector 扩展与 document 编号序列）。"""
    from app.storage import models  # noqa: F401  注册 ORM 模型

    async with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))

        await connection.run_sync(Base.metadata.create_all)

        if connection.dialect.name == "postgresql":
            await connection.execute(
                text("CREATE SEQUENCE IF NOT EXISTS document_number_seq START WITH 1")
            )


async def close_db() -> None:
    await engine.dispose()
