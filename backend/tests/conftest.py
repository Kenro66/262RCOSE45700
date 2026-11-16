"""隔离测试默认使用临时 SQLite，即使环境中存在生产数据库地址也不例外。

仅在显式设置 ISSUE9_TEST_DATABASE_URL 和 ISSUE9_ALLOW_DATABASE_RESET=1 时，
使用可丢弃的 PostgreSQL 测试库。测试夹具会创建并清空文档和聊天相关表。
禁止使用共享数据库或生产数据库。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

# 只有显式指定独立测试库并授权重置时，才启用Postgres测试。
# 不能因为设置了生产数据库地址，就让测试自动执行数据清理。
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
    from sqlalchemy.pool import NullPool  # noqa: E402

    import app.database as database  # noqa: E402

    # 轮询与后台执行需要独立事务。单连接池会让读取事务的回滚
    # 撤销写入事务尚未提交的修改，因此使用临时文件和独立连接。
    _SQLITE_DIR = tempfile.TemporaryDirectory(prefix="rag-tests-")
    TEST_ENGINE = create_async_engine(
        "sqlite+aiosqlite:///" + (Path(_SQLITE_DIR.name) / "tests.db").as_posix(),
        poolclass=NullPool,
    )
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

_TABLES = ("chat_retrieval_attempts", "chat_submissions", "chat_questions", "chat_sessions",
           "chunks", "ParsedPage", "documents")


@pytest_asyncio.fixture()
async def db():
    """每个测试前建表并清空，测试结束后清空。

    SQLite 模式使用临时文件；PostgreSQL 模式使用独立的可写测试库。
    本夹具会清空文档和聊天相关表。
    """
    from app.database import SessionFactory, engine, init_db
    from app.chat.persistent import models  # noqa: F401  显式注册聊天表，支持单独运行测试。

    await init_db()
    async with engine.begin() as connection:
        for table in _TABLES:
            await connection.execute(text(f'DELETE FROM "{table}"'))
    yield SessionFactory
    async with engine.begin() as connection:
        for table in _TABLES:
            await connection.execute(text(f'DELETE FROM "{table}"'))
