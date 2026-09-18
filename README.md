# gguf-router

HTTP model router for llama.cpp nodes. Discord bots on snerloc POST to `/route` with `prompt` plus `persona` or `task`; the router forwards to Qwen (`10.1.1.122:8081`) or Dolphin (`10.1.1.122:8082`).

```bash
curl -X POST http://localhost:9000/route \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"test","persona":"wendy"}'
```

LAN: `http://snerloc:9000/route` (this host is `10.1.1.106`).

Service: `systemctl --user status gguf-router` (system unit is installed at `/etc/systemd/system/gguf-router.service`; enable/start of that unit needs a sudo password).
