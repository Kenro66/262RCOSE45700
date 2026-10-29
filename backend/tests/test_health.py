"""GET /health（Issue #4 行为在 #7 main.py 的整合回归测试）。

每次请求真实连库探测（#4 契约）：
- 连接成功且 SQL 查询成功 → 200（返回版本与 vector 扩展状态）
- 连接失败 → 503（日志有明确错误，不静默成功，不出 500）
- 连接成功但查询失败 → 503
- 关闭连接失败不掩盖原始结果
- 任何情况下连接都会被释放

全部用 monkeypatch asyncpg.connect 断言（无需真实数据库，Postgres 模式下
本套件同样确定性运行）。
"""

from __future__ import annotations

import asyncpg
import httpx
import pytest
from fastapi import FastAPI

from app.main import app


class _FakeConn:
    def __init__(self, fetchrow_result=None, fetchrow_exc=None, close_exc=None):
        self._result = fetchrow_result
        self._exc = fetchrow_exc
        self._close_exc = close_exc
        self.closed = False

    async def fetchrow(self, *args, **kwargs):
        if self._exc is not None:
            raise self._exc
        return self._result

    async def close(self):
        if self._close_exc is not None:
            raise self._close_exc
        self.closed = True


async def _get_health(app: FastAPI) -> tuple[int, dict]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/health")
        return response.status_code, response.json()


@pytest.mark.asyncio
async def test_health_200_when_database_ok(monkeypatch) -> None:
    """连接＋查询都成功 → 200，返回版本与 vector 扩展状态，且连接被释放。"""
    created = {}

    async def fake_connect(url, timeout=None):
        conn = _FakeConn(fetchrow_result=("16.4", True))
        created["conn"] = conn
        return conn

    monkeypatch.setattr(asyncpg, "connect", fake_connect)
    status, body = await _get_health(app)

    assert status == 200
    assert body["status"] == "ok"
    assert body["database"] == {"version": "16.4", "vector_extension": True}
    assert created["conn"].closed is True  # 连接任何情况都释放


@pytest.mark.asyncio
async def test_health_503_when_connect_fails(monkeypatch) -> None:
    """数据库没启动（连接失败）→ 503 带明确错误，不静默 200、不出 500。"""

    async def fake_connect(url, timeout=None):
        raise OSError("connection refused（模拟数据库未启动）")

    monkeypatch.setattr(asyncpg, "connect", fake_connect)
    status, body = await _get_health(app)

    assert status == 503
    assert body["status"] == "error"
    assert "连接失败" in body["database"]


@pytest.mark.asyncio
async def test_health_503_when_query_fails(monkeypatch) -> None:
    """连接成功但 SQL 查询失败 → 同样 503（查询失败视为数据库不可用）。"""

    async def fake_connect(url, timeout=None):
        return _FakeConn(fetchrow_exc=RuntimeError("模拟查询失败"))

    monkeypatch.setattr(asyncpg, "connect", fake_connect)
    status, body = await _get_health(app)

    assert status == 503
    assert body["status"] == "error"
    assert "查询失败" in body["database"]


@pytest.mark.asyncio
async def test_health_close_failure_does_not_mask_success(monkeypatch) -> None:
    """关闭失败不掩盖成功结果：仍 200。"""

    async def fake_connect(url, timeout=None):
        return _FakeConn(
            fetchrow_result=("16.4", False), close_exc=RuntimeError("模拟关闭失败")
        )

    monkeypatch.setattr(asyncpg, "connect", fake_connect)
    status, body = await _get_health(app)

    assert status == 200
    assert body["database"]["vector_extension"] is False


@pytest.mark.asyncio
async def test_health_close_failure_does_not_mask_503(monkeypatch) -> None:
    """双重失败（查询失败＋关闭失败）：仍回原始查询失败 503，不出 500。"""

    async def fake_connect(url, timeout=None):
        return _FakeConn(
            fetchrow_exc=RuntimeError("模拟查询失败"),
            close_exc=RuntimeError("模拟关闭失败"),
        )

    monkeypatch.setattr(asyncpg, "connect", fake_connect)
    status, body = await _get_health(app)

    assert status == 503
    assert "查询失败" in body["database"]
