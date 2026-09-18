# feature/agent-tools

Working branch for the bounded browser-agent slice. Do not land on `main` until the rough items below are acceptable.

## Done

- Stateful Playwright: one Chromium context per persona (`wendy` / `gumbo` / `tabatha`).
- `storageState()` saved to `data/browser/<persona>.json` and `tools.tool_state`.
- Tool actions: `browser_goto`, `browser_read` (clean text), `browser_click`, `browser_type`, `browser_submit`. Same `/tool` payload shape (`tool`, `args`, `user_id`, `bot_name`).
- Blocked pages return `{"status":"blocked","reason":...}` (CAPTCHA, Cloudflare, login wall, timeout) instead of fake success.
- Memory split on `agent_cluster`:
  - `sessions.context` — trimmed scratch (URL, last read, last 6 turns)
  - `events` — append-only audit (`event_type`, `payload`)
  - `memories` — durable extractive notes after a loop / every 8 events
- Bounded loop on `POST /route` with `"agent": true`: 5 tool steps, 45s wall clock; over-budget returns a partial summary.
- URL bootstrap: if the prompt contains `http(s)://`, run goto+read before asking the model (local GGUF models rarely emit tool JSON).
- Discord (in `~/vibe-hub/discord-bots`, not this repo): `@bot !browse <url>`, ack-then-edit replies.
- Verified: Wendy `browser_goto` + `browser_read` on example.com, events + session + memory rows written.

## Still rough

- Dolphin/Qwen almost never emit `{"tool":...}` on their own; the URL bootstrap is a crutch, not a general planner.
- No screenshots.
- `credentials` table is unused — no auto-login.
- Memory consolidation is extractive (`event_type + url + title`), not an LLM summary.
- Browser pool is one process-wide Chromium; not safe with multiple uvicorn workers.
- Selector language is CSS / `text=` only; no accessibility-tree agent.
- Login-wall heuristic can fire on pages the agent is *trying* to log into (type/submit skip the check; goto/read do not).
- Discord bot patches live outside this git repo.
- No automated tests for the loop, blocked detection, or schema migrate.
