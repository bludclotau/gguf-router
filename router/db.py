import json
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path

import psycopg2
import psycopg2.extras

_write_lock = threading.Lock()


def async_write(func, *args):
    threading.Thread(target=func, args=args, daemon=True).start()


def log_tool(user_id, tool_name, output):
    async_write(save_raw_output, f"tool:{user_id}:{tool_name}", str(output))

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

CREATE UNIQUE INDEX IF NOT EXISTS agents_name_key ON agents (name);
CREATE UNIQUE INDEX IF NOT EXISTS sessions_agent_id_key ON sessions (agent_id);
CREATE UNIQUE INDEX IF NOT EXISTS tools_bot_tool_key ON tools (bot_name, tool_name);

CREATE TABLE IF NOT EXISTS memories (
    id SERIAL PRIMARY KEY,
    agent_id INTEGER REFERENCES agents(id) ON DELETE CASCADE,
    user_id TEXT,
    note TEXT NOT NULL,
    source TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_memories_agent_created
    ON memories (agent_id, created_at DESC);
"""

SEED_AGENTS = ("wendy", "gumbo", "tabatha")
SESSION_TURN_CAP = 6
SESSION_READ_CAP = 1500
CONSOLIDATE_EVERY = 8


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


def _run_write(sql, params):
    with _write_lock:
        cur = _cursor()
        cur.execute(sql, params)
        conn.commit()
        cur.close()


def init_schema() -> bool:
    statements = [s.strip() for s in SCHEMA_SQL.split(";") if s.strip()]
    with _write_lock:
        cur = _cursor()
        for stmt in statements:
            try:
                cur.execute(stmt)
                conn.commit()
            except Exception:
                conn.rollback()
        cur.close()
    for name in SEED_AGENTS:
        try:
            ensure_agent(name)
        except Exception:
            pass
    return True


def save_raw_output(key, value):
    _run_write(
        """
        INSERT INTO cache (key, value, expires_at)
        VALUES (%s, %s, %s)
        ON CONFLICT (key)
        DO UPDATE SET value = EXCLUDED.value, expires_at = EXCLUDED.expires_at;
        """,
        (key, value, datetime.now() + timedelta(minutes=10)),
    )


def get_cached(key):
    with _write_lock:
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
    _run_write(
        """
        INSERT INTO cache (key, value, expires_at)
        VALUES (%s, %s, %s)
        ON CONFLICT (key)
        DO UPDATE SET value = EXCLUDED.value, expires_at = EXCLUDED.expires_at;
        """,
        (f"cooldown:{user_id}", "1", datetime.now() + timedelta(seconds=seconds)),
    )


def check_cooldown(user_id):
    return get_cached(f"cooldown:{user_id}") is not None


def update_conversation(user_id, bot_name, persona, task, last_message):
    _run_write(
        """
        INSERT INTO conversations (user_id, bot_name, persona, task, last_message)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (user_id, bot_name, persona, task, last_message),
    )


def get_persona_memory(user_id, persona):
    with _write_lock:
        cur = _cursor(dict_cursor=True)
        cur.execute(
            """
            SELECT last_message FROM conversations
            WHERE user_id = %s AND persona = %s
            ORDER BY updated_at DESC LIMIT 1
            """,
            (user_id, persona),
        )
        row = cur.fetchone()
        cur.close()
    return row["last_message"] if row else None


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
    with _write_lock:
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


def ensure_agent(name):
    """Return agents.id for a Discord persona, creating the row if needed."""
    if not name:
        name = "wendy"
    name = str(name).lower()
    persona = json.dumps({"label": name})
    caps = json.dumps(["browser", "web_fetch"])
    with _write_lock:
        cur = _cursor()
        cur.execute(
            """
            INSERT INTO agents (name, persona, capabilities)
            VALUES (%s, %s::jsonb, %s::jsonb)
            ON CONFLICT (name) DO UPDATE
            SET capabilities = EXCLUDED.capabilities
            RETURNING id
            """,
            (name, persona, caps),
        )
        row = cur.fetchone()
        conn.commit()
        cur.close()
    return int(row[0]) if row else None


def get_session_context(agent_name):
    agent_id = ensure_agent(agent_name)
    with _write_lock:
        cur = _cursor(dict_cursor=True)
        cur.execute(
            """
            SELECT context FROM sessions
            WHERE agent_id = %s
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (agent_id,),
        )
        row = cur.fetchone()
        cur.close()
    if not row:
        return {}
    ctx = row["context"]
    return dict(ctx) if isinstance(ctx, dict) else {}


def set_session_context(agent_name, context):
    agent_id = ensure_agent(agent_name)
    context = trim_session_context(context if isinstance(context, dict) else {})
    payload = json.dumps(context)
    with _write_lock:
        cur = _cursor()
        cur.execute("SELECT id FROM sessions WHERE agent_id = %s LIMIT 1", (agent_id,))
        existing = cur.fetchone()
        if existing:
            cur.execute(
                """
                UPDATE sessions
                SET context = %s::jsonb, updated_at = NOW()
                WHERE agent_id = %s
                """,
                (payload, agent_id),
            )
        else:
            cur.execute(
                """
                INSERT INTO sessions (agent_id, context, updated_at)
                VALUES (%s, %s::jsonb, NOW())
                """,
                (agent_id, payload),
            )
        conn.commit()
        cur.close()


def trim_session_context(context):
    """Keep sessions.context as short-term scratch, not an archive."""
    ctx = dict(context or {})
    turns = ctx.get("recent_turns") or []
    if isinstance(turns, list) and len(turns) > SESSION_TURN_CAP:
        ctx["recent_turns"] = turns[-SESSION_TURN_CAP:]
    last_read = ctx.get("last_read")
    if isinstance(last_read, str) and len(last_read) > SESSION_READ_CAP:
        ctx["last_read"] = last_read[:SESSION_READ_CAP]
    return ctx


def log_agent_event(agent_name, event_type, payload=None):
    """Append-only audit trail. Live table columns: event_type, payload."""
    agent_id = ensure_agent(agent_name)
    blob = json.dumps(payload or {})
    _run_write(
        """
        INSERT INTO events (agent_id, event_type, payload)
        VALUES (%s, %s, %s::jsonb)
        """,
        (agent_id, event_type, blob),
    )


def recent_events(agent_name, limit=12):
    agent_id = ensure_agent(agent_name)
    with _write_lock:
        cur = _cursor(dict_cursor=True)
        cur.execute(
            """
            SELECT event_type, payload, created_at
            FROM events
            WHERE agent_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (agent_id, limit),
        )
        rows = cur.fetchall()
        cur.close()
    return [dict(r) for r in rows]


def add_memory(agent_name, note, user_id=None, source=None):
    if not note:
        return
    agent_id = ensure_agent(agent_name)
    _run_write(
        """
        INSERT INTO memories (agent_id, user_id, note, source)
        VALUES (%s, %s, %s, %s)
        """,
        (agent_id, user_id, note[:1200], source),
    )


def get_memories(agent_name, limit=5):
    agent_id = ensure_agent(agent_name)
    with _write_lock:
        cur = _cursor(dict_cursor=True)
        cur.execute(
            """
            SELECT note FROM memories
            WHERE agent_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (agent_id, limit),
        )
        rows = cur.fetchall()
        cur.close()
    return [r["note"] for r in rows if r.get("note")]


def _extractive_event_summary(rows):
    bits = []
    for row in reversed(rows):
        payload = row.get("payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}
        et = row.get("event_type") or "event"
        url = ""
        title = ""
        if isinstance(payload, dict):
            url = payload.get("url") or ""
            title = payload.get("title") or payload.get("status") or ""
        bits.append(f"{et} {title} {url}".strip())
    return " | ".join(bits)[:800]


def maybe_consolidate(agent_name, user_id=None, force=False):
    """
    Fold recent events into a durable memory note.
    Judgment call: extractive summary, not a second LLM pass — these
    replies already sit on a slow local model and a 45s wall clock.
    """
    rows = recent_events(agent_name, limit=CONSOLIDATE_EVERY)
    if not rows:
        return
    if not force and len(rows) < CONSOLIDATE_EVERY:
        return
    note = _extractive_event_summary(rows)
    if note:
        add_memory(agent_name, note, user_id=user_id, source="event_consolidation")


def save_browser_state(persona, state):
    payload = json.dumps(state or {})
    _run_write(
        """
        INSERT INTO tools (bot_name, tool_name, tool_state, updated_at)
        VALUES (%s, 'browser', %s::jsonb, NOW())
        ON CONFLICT (bot_name, tool_name) DO UPDATE
        SET tool_state = EXCLUDED.tool_state, updated_at = NOW()
        """,
        (persona, payload),
    )


def load_browser_state(persona):
    with _write_lock:
        cur = _cursor(dict_cursor=True)
        cur.execute(
            """
            SELECT tool_state FROM tools
            WHERE bot_name = %s AND tool_name = 'browser'
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (persona,),
        )
        row = cur.fetchone()
        cur.close()
    if not row:
        return None
    state = row["tool_state"]
    return dict(state) if isinstance(state, dict) else None
