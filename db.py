"""
db.py — asyncpg connection pool and SQL helper layer.

Usage:
    from db import fetch_one, fetch_all, execute, fetch_val, executemany, transaction, init_pool, close_pool
"""

import os
import asyncpg
from typing import Optional, Any

_pool: Optional[asyncpg.Pool] = None


async def init_pool():
    global _pool
    _pool = await asyncpg.create_pool(
        dsn=os.environ["DATABASE_URL"],
        min_size=2,
        max_size=10,
        command_timeout=60,
    )


async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


def _row_to_dict(record) -> Optional[dict]:
    if record is None:
        return None
    return dict(record)


async def fetch_one(sql: str, *args) -> Optional[dict]:
    """Return first matching row as dict, or None."""
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(sql, *args)
        return _row_to_dict(row)


async def fetch_all(sql: str, *args) -> list[dict]:
    """Return all matching rows as list of dicts."""
    async with _pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
        return [dict(r) for r in rows]


async def fetch_val(sql: str, *args) -> Any:
    """Return a single scalar value (COUNT, SUM, etc.)."""
    async with _pool.acquire() as conn:
        return await conn.fetchval(sql, *args)


async def execute(sql: str, *args) -> str:
    """Execute INSERT/UPDATE/DELETE. Returns status string."""
    async with _pool.acquire() as conn:
        return await conn.execute(sql, *args)


async def executemany(sql: str, args_list: list) -> None:
    """Batch execute (e.g. bulk insert)."""
    async with _pool.acquire() as conn:
        await conn.executemany(sql, args_list)


class _Transaction:
    """Async context manager for atomic transaction blocks."""

    def __init__(self):
        self._conn = None
        self._tx = None

    async def __aenter__(self):
        self._conn = await _pool.acquire()
        self._tx = self._conn.transaction()
        await self._tx.start()
        return _TransactionConn(self._conn)

    async def __aexit__(self, exc_type, exc, tb):
        try:
            if exc_type:
                await self._tx.rollback()
            else:
                await self._tx.commit()
        finally:
            await _pool.release(self._conn)


class _TransactionConn:
    """Connection wrapper used inside a transaction block."""

    def __init__(self, conn):
        self._conn = conn

    async def fetch_one(self, sql: str, *args) -> Optional[dict]:
        row = await self._conn.fetchrow(sql, *args)
        return _row_to_dict(row)

    async def fetch_all(self, sql: str, *args) -> list[dict]:
        rows = await self._conn.fetch(sql, *args)
        return [dict(r) for r in rows]

    async def fetch_val(self, sql: str, *args) -> Any:
        return await self._conn.fetchval(sql, *args)

    async def execute(self, sql: str, *args) -> str:
        return await self._conn.execute(sql, *args)

    async def executemany(self, sql: str, args_list: list) -> None:
        await self._conn.executemany(sql, args_list)


def transaction() -> _Transaction:
    """Return an async context manager that wraps statements in a DB transaction."""
    return _Transaction()
