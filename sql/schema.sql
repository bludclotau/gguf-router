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

-- Live agent_cluster already has agents/sessions/events/tasks (from agent-router).
-- These statements are additive and safe on an empty or populated DB.

CREATE UNIQUE INDEX IF NOT EXISTS agents_name_key ON agents (name);
CREATE UNIQUE INDEX IF NOT EXISTS sessions_agent_id_key ON sessions (agent_id);
CREATE UNIQUE INDEX IF NOT EXISTS tools_bot_tool_key ON tools (bot_name, tool_name);

-- Durable notes, separate from sessions.context (which is trimmed scratch).
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
