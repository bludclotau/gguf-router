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

Service: `systemctl --user status gguf-router` (system unit is installed at `/etc/systemd/system/gguf-router.service`; enable/start of that unit needs a sudo password).
