# gguf-router

HTTP model router for llama.cpp nodes. Discord bots on snerloc POST to `/route` with `prompt` plus `persona` or `task`; the router forwards to Qwen (`10.1.1.122:8081`) or Dolphin (`10.1.1.122:8082`).

`persona_engine.apply()` picks the model from `personas.json` / `tasks.json`, prepends `persona_prompts.yaml`, and injects per-user persona memory from PostgreSQL. After each completion the router caches raw llama.cpp output, strips role tags / grounding / XML / think blocks, logs the cleaned line to `conversations`, and enforces a 2s per-user cooldown. Clients should send `prompt`, `persona`, `task`, `user_id`, and `bot_name`, and read `response["clean"]`.

```bash
curl -X POST http://localhost:9000/route \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"test","persona":"wendy","task":"general","user_id":"123","bot_name":"wendy"}'
```

LAN: `http://snerloc:9000/route` (this host is `10.1.1.106`).

Copy `.env.example` to `.env` and set `DATABASE_URL`. Schema lives in `sql/schema.sql` (`conversations`, `cache`, `tools`) and is applied on startup.

Bot endpoint: `http://localhost:9000/route`.

Tool harness: `POST /tool` with `{"tool":"web_fetch","args":{"url":"https://example.com"},"user_id":"...","bot_name":"..."}`. Discord: `@bot !web <url>`.

### Bounded agents (browser + memory)

This is a chat-scoped agent, not an open-ended crawler: **6 tool steps** and **60s wall clock** per `/route` with `"agent": true`. If the budget runs out the router returns a partial "here's what I found so far". If the prompt contains an `http(s)` URL, the loop **bootstraps** `browser_goto` + `browser_read` before asking the model — local GGUF models rarely emit valid tool JSON on their own. A CAPTCHA / login wall returns a clear blocked message (and Discord edits the ack) instead of going silent.

Stateful Playwright keeps **one Chromium context per persona**. Cookies go to `data/browser/<persona>.json` and `tools.tool_state`. CAPTCHA / Cloudflare / login walls return `{"status":"blocked","reason":...}` instead of pretending the page loaded.

Memory split (all in `agent_cluster`):

- `sessions.context` — short-term scratch (current URL, last read, last 6 turns). Trimmed on write.
- `events` — append-only audit (`event_type`, `payload`). Live columns, not the older `type`/`data` names.
- `memories` — durable notes. After a loop (or every 8 events) recent events are folded into an extractive note. No second LLM pass; the local model is already the bottleneck.

Discord: `@bot !browse <url> [instruction]` runs the agent loop. Replies ack immediately (`On it…`) then edit when the loop finishes.

```bash
curl -X POST http://localhost:9000/tool \
  -H 'Content-Type: application/json' \
  -d '{"tool":"browser_goto","args":{"url":"https://example.com"},"bot_name":"wendy"}'

curl -X POST http://localhost:9000/route \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"open https://example.com and tell me the heading","persona":"wendy","bot_name":"wendy","user_id":"1","agent":true}'
```

Service: `systemctl --user status gguf-router` (system unit is installed at `/etc/systemd/system/gguf-router.service`; enable/start of that unit needs a sudo password).
