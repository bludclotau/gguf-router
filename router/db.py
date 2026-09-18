import os
from datetime import datetime, timedelta
from pathlib import Path

import psycopg2
import psycopg2.extras

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS conversations (
    id SERIAL PRIMARY KEY,
    user_id TEXT,
    bot_name TEXT,
    persona TEXT,
    task TEXT,
    last_message TEXT,
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS cache (
    id SERIAL PRIMARY KEY,
    key TEXT UNIQUE,
    value TEXT,
    expires_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tools (
    id SERIAL PRIMARY KEY,
    bot_name TEXT,
    tool_name TEXT,
    tool_state JSONB,
    updated_at TIMESTAMP DEFAULT NOW()
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


def _connect():
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_DSN")
    if dsn:
        return psycopg2.connect(dsn)
    try:
        return psycopg2.connect(
            dbname="postgres",
            user="postgres",
            host="localhost",
        )
    except psycopg2.OperationalError:
        return psycopg2.connect(
            dbname=os.environ.get("PGDATABASE", "postgres"),
            user=os.environ.get("PGUSER", "postgres"),
            host=os.environ.get("PGHOST", "localhost"),
            password=os.environ.get("PGPASSWORD"),
        )


conn = _connect()


def _cursor(dict_cursor=False):
    global conn
    if conn is None or conn.closed:
        conn = _connect()
    factory = psycopg2.extras.DictCursor if dict_cursor else None
    return conn.cursor(cursor_factory=factory) if factory else conn.cursor()


def init_schema() -> bool:
    cur = _cursor()
    cur.execute(SCHEMA_SQL)
    conn.commit()
    cur.close()
    return True


def save_raw_output(key, value):
    cur = _cursor()
    cur.execute(
        """
        INSERT INTO cache (key, value, expires_at)
        VALUES (%s, %s, %s)
        ON CONFLICT (key)
        DO UPDATE SET value = EXCLUDED.value, expires_at = EXCLUDED.expires_at;
        """,
        (key, value, datetime.now() + timedelta(minutes=10)),
    )
    conn.commit()
    cur.close()


def get_cached(key):
    cur = _cursor(dict_cursor=True)
    cur.execute("SELECT value, expires_at FROM cache WHERE key = %s", (key,))
    row = cur.fetchone()
    cur.close()
    if not row:
        return None
    expires_at = row["expires_at"]
    if expires_at is not None:
        now = datetime.now(expires_at.tzinfo) if getattr(expires_at, "tzinfo", None) else datetime.now()
        if expires_at < now:
            return None
    return row["value"]


def set_cooldown(user_id, seconds):
    cur = _cursor()
    cur.execute(
        """
        INSERT INTO cache (key, value, expires_at)
        VALUES (%s, %s, %s)
        ON CONFLICT (key)
        DO UPDATE SET value = EXCLUDED.value, expires_at = EXCLUDED.expires_at;
        """,
        (f"cooldown:{user_id}", "1", datetime.now() + timedelta(seconds=seconds)),
    )
    conn.commit()
    cur.close()


def check_cooldown(user_id):
    return get_cached(f"cooldown:{user_id}") is not None


def update_conversation(user_id, bot_name, persona, task, last_message):
    cur = _cursor()
    cur.execute(
        """
        INSERT INTO conversations (user_id, bot_name, persona, task, last_message)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (user_id, bot_name, persona, task, last_message),
    )
    conn.commit()
    cur.close()


def get_last_message(user_id=None, bot_name=None):
    clauses = []
    params = []
    if user_id:
        clauses.append("user_id = %s")
        params.append(user_id)
    if bot_name:
        clauses.append("bot_name = %s")
        params.append(bot_name)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    cur = _cursor(dict_cursor=True)
    cur.execute(
        f"""
        SELECT user_id, bot_name, persona, task, last_message, updated_at
        FROM conversations
        {where}
        ORDER BY updated_at DESC, id DESC
        LIMIT 1
        """,
        params,
    )
    row = cur.fetchone()
    cur.close()
    return dict(row) if row else None
