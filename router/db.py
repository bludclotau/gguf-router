"""PostgreSQL state, conversation cache, and cooldown helpers."""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import SimpleConnectionPool

log = logging.getLogger("gguf-router.db")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS conversations (
    id            BIGSERIAL PRIMARY KEY,
    persona       TEXT,
    task          TEXT,
    model         TEXT,
    prompt        TEXT,
    raw_output    TEXT,
    clean_output  TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_conversations_persona_created
    ON conversations (persona, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_conversations_created
    ON conversations (created_at DESC);

CREATE TABLE IF NOT EXISTS cache (
    key           TEXT PRIMARY KEY,
    value         TEXT,
    expires_at    TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_cache_expires_at ON cache (expires_at);

CREATE TABLE IF NOT EXISTS tools (
    id            SERIAL PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    description   TEXT,
    config        JSONB NOT NULL DEFAULT '{}'::jsonb,
    enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


def _load_dotenv() -> None:
    for candidate in (
        Path(__file__).resolve().parent.parent / ".env",
        Path(__file__).resolve().parent / ".env",
    ):
        if not candidate.is_file():
            continue
        for raw_line in candidate.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

DSN = (
    os.environ.get("DATABASE_URL")
    or os.environ.get("POSTGRES_DSN")
    or os.environ.get("POSTGRES_URL")
)

_pool: Optional[SimpleConnectionPool] = None


def _get_pool() -> Optional[SimpleConnectionPool]:
    global _pool
    if _pool is not None:
        return _pool
    if not DSN:
        log.warning("DATABASE_URL is not set; PostgreSQL layer disabled")
        return None
    try:
        _pool = SimpleConnectionPool(1, 8, dsn=DSN, connect_timeout=5)
    except Exception as exc:
        log.warning("PostgreSQL pool init failed: %s", exc)
        return None
    return _pool


@contextmanager
def get_conn() -> Iterator[Any]:
    pool = _get_pool()
    if pool is None:
        yield None
        return
    conn = None
    try:
        conn = pool.getconn()
        yield conn
        conn.commit()
    except Exception:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    finally:
        if conn is not None:
            pool.putconn(conn)


def init_schema() -> bool:
    try:
        with get_conn() as conn:
            if conn is None:
                return False
            with conn.cursor() as cur:
                cur.execute(SCHEMA_SQL)
        log.info("PostgreSQL schema ready")
        return True
    except Exception as exc:
        log.warning("Schema init failed: %s", exc)
        return False


def save_raw_output(
    prompt: str,
    raw_output: str,
    persona: Optional[str] = None,
    task: Optional[str] = None,
    model: Optional[str] = None,
) -> Optional[int]:
    try:
        with get_conn() as conn:
            if conn is None:
                return None
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO conversations (persona, task, model, prompt, raw_output)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (persona, task, model, prompt, raw_output),
                )
                row = cur.fetchone()
                return int(row[0]) if row else None
    except Exception as exc:
        log.warning("save_raw_output failed: %s", exc)
        return None


def save_clean_output(conversation_id: Optional[int], clean_output: str) -> bool:
    if conversation_id is None:
        return False
    try:
        with get_conn() as conn:
            if conn is None:
                return False
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE conversations
                    SET clean_output = %s
                    WHERE id = %s
                    """,
                    (clean_output, conversation_id),
                )
                return cur.rowcount > 0
    except Exception as exc:
        log.warning("save_clean_output failed: %s", exc)
        return False


def get_last_message(
    persona: Optional[str] = None,
    task: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    clauses = []
    params: list[Any] = []
    if persona:
        clauses.append("persona = %s")
        params.append(persona)
    if task:
        clauses.append("task = %s")
        params.append(task)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT id, persona, task, model, prompt, raw_output, clean_output, created_at
        FROM conversations
        {where}
        ORDER BY created_at DESC, id DESC
        LIMIT 1
    """
    try:
        with get_conn() as conn:
            if conn is None:
                return None
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql, params)
                row = cur.fetchone()
                return dict(row) if row else None
    except Exception as exc:
        log.warning("get_last_message failed: %s", exc)
        return None


def set_cooldown(key: str, seconds: float) -> bool:
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=float(seconds))
    try:
        with get_conn() as conn:
            if conn is None:
                return False
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO cache (key, value, expires_at, updated_at)
                    VALUES (%s, %s, %s, NOW())
                    ON CONFLICT (key) DO UPDATE
                    SET value = EXCLUDED.value,
                        expires_at = EXCLUDED.expires_at,
                        updated_at = NOW()
                    """,
                    (f"cooldown:{key}", str(seconds), expires_at),
                )
                return True
    except Exception as exc:
        log.warning("set_cooldown failed: %s", exc)
        return False


def check_cooldown(key: str) -> bool:
    """Return True when the key is still cooling down."""
    try:
        with get_conn() as conn:
            if conn is None:
                return False
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT expires_at FROM cache
                    WHERE key = %s
                    """,
                    (f"cooldown:{key}",),
                )
                row = cur.fetchone()
                if not row or row[0] is None:
                    return False
                expires_at = row[0]
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                return datetime.now(timezone.utc) < expires_at
    except Exception as exc:
        log.warning("check_cooldown failed: %s", exc)
        return False
