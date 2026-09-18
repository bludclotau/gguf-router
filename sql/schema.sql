-- gguf-router state/cache schema.

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
