-- gguf-router state/cache schema. Idempotent.

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
