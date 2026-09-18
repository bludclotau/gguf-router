# gguf-router

HTTP model router for llama.cpp nodes. Discord bots on snerloc POST to `/route` with `prompt` plus `persona` or `task`; the router forwards to Qwen (`10.1.1.122:8081`) or Dolphin (`10.1.1.122:8082`).

After each completion the router stores the raw llama.cpp output in PostgreSQL, strips leaked role tags / XML / JSON metadata, trims trailing fragments at the last sentence, stores the cleaned text, and returns JSON with `content` and `reply`.

```bash
curl -X POST http://localhost:9000/route \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"test","persona":"wendy","task":"general"}'
```

LAN: `http://snerloc:9000/route` (this host is `10.1.1.106`).

Copy `.env.example` to `.env` and set `DATABASE_URL`. Schema lives in `sql/schema.sql` (`conversations`, `cache`, `tools`) and is applied on startup.

Bot endpoint: `http://localhost:9000/route`. Bots should send both `persona` and `task`.

Service: `systemctl --user status gguf-router` (system unit is installed at `/etc/systemd/system/gguf-router.service`; enable/start of that unit needs a sudo password).
