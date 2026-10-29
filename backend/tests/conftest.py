"""Isolated tests default to SQLite even when a production DATABASE_URL exists.

Opt into a disposable PostgreSQL test DB with ISSUE9_TEST_DATABASE_URL and
ISSUE9_ALLOW_DATABASE_RESET=1. The fixture creates and clears all three tables.
Never use a shared or production database.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

# 只有显式指定独立测试库并授权重置时，才启用Postgres测试。
# Production DATABASE_URL must never silently opt the test suite into deleting data.
_TEST_URL = os.environ.get("ISSUE9_TEST_DATABASE_URL", "")
_USE_POSTGRES = _TEST_URL.startswith("postgresql")
if _TEST_URL and not _USE_POSTGRES:
    raise RuntimeError("ISSUE9_TEST_DATABASE_URL must be a PostgreSQL test URL")
if _USE_POSTGRES:
    if os.environ.get("ISSUE9_ALLOW_DATABASE_RESET") != "1":
        raise RuntimeError("Dedicated test DB reset requires ISSUE9_ALLOW_DATABASE_RESET=1")
    os.environ["DATABASE_URL"] = _TEST_URL

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

if not _USE_POSTGRES:
    os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"

    from sqlalchemy import text  # noqa: E402
    from sqlalchemy.ext.asyncio import (  # noqa: E402
        async_sessionmaker,
        create_async_engine,
    )
    from sqlalchemy.pool import StaticPool  # noqa: E402

    import app.database as database  # noqa: E402

    TEST_ENGINE = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    TEST_SESSION_FACTORY = async_sessionmaker(TEST_ENGINE, expire_on_commit=False)

    database.engine = TEST_ENGINE
    database.SessionFactory = TEST_SESSION_FACTORY

else:
    from sqlalchemy import text  # noqa: E402
    from sqlalchemy.ext.asyncio import (  # noqa: E402
        async_sessionmaker,
        create_async_engine,
    )
    from sqlalchemy.pool import NullPool  # noqa: E402

    import app.database as database  # noqa: E402

    # Postgres 模式用 NullPool：asyncpg 连接绑定创建它的事件循环，连接池会让
    # 连接在多个 loop 间被复用（fixture loop / TestClient portal loop /
    # 各测试 loop）→ "attached to a different loop"。NullPool 每次 checkout
    # 新建、checkin 即关，不做跨 loop 复用。
    # URL 用 app.database.DATABASE_URL（已过 _normalize_database_url）——与 app
    # 同一来源，#4 模板的 postgresql:// 写法在此同样可用，不会驱动加载报错。
    PG_ENGINE = create_async_engine(
        database.DATABASE_URL, poolclass=NullPool
    )
    database.engine = PG_ENGINE
    database.SessionFactory = async_sessionmaker(PG_ENGINE, expire_on_commit=False)

_TABLES = ("chunks", "ParsedPage", "documents")


@pytest_asyncio.fixture()
async def db():
    """每个测试前建表并清空，测试结束后清空。

    SQLite 模式用内存 TEST_ENGINE；Postgres 模式用真实 engine
    （DATABASE_URL 须指向可写测试库——本 fixture 会 DELETE 三张表）。
    """
    from app.database import SessionFactory, engine, init_db

    await init_db()
    async with engine.begin() as connection:
        for table in _TABLES:
            await connection.execute(text(f'DELETE FROM "{table}"'))
    yield SessionFactory
    async with engine.begin() as connection:
        for table in _TABLES:
            await connection.execute(text(f'DELETE FROM "{table}"'))
