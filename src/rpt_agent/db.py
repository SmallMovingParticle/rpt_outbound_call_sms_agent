from __future__ import annotations

from contextlib import contextmanager
from functools import cache

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import get_settings


@cache
def get_pool() -> ConnectionPool:
    settings = get_settings()
    url = settings.supabase_db_url
    if not url:
        raise RuntimeError("SUPABASE_DB_URL is required for database operations")
    return ConnectionPool(
        conninfo=url,
        min_size=1,
        max_size=10,
        timeout=settings.db_pool_timeout_seconds,
        kwargs={"row_factory": dict_row, "connect_timeout": int(settings.db_pool_timeout_seconds)},
    )


@contextmanager
def transaction():
    with get_pool().connection() as conn, conn.transaction():
        yield conn
