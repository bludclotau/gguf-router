# feature/agent-tools

Ready for review. Do not merge until you're happy with the remaining nits.

## Done

- **GBNF planner**: llama.cpp grammar forces `{"tool", "args"}` or `{"reply"}`. Tool-only grammar until the first action when the user named a URL; reply-only grammar once we have the page text we need. Persona prose is kept out of the planner prompt so it cannot fight the JSON.
- **Multi-step chains**: verified `goto → read` (heading) and `goto → click → read` (Learn more landed on IANA “Example Domains”).
- **Every persona** has its own browser context, session, events, memories.
- **Auto-login**: `credentials` rows (JSON in `encrypted_key`; LAN-only, no extra crypto). `browser_login` fills selectors; secrets never enter the model prompt. Login-wall in the loop tries `browser_login` once. Fixture: `http://127.0.0.1:9000/debug/login` (wendy / snacktime) → `/debug/secret`.
- Blocked pages still return a human `message`; Discord ack-then-edit.
- Unit tests: `python3 router/tests/test_hardening.py`

## Still rough (ok to land later)

- Screenshots.
- Multiple uvicorn workers / multiple Chromium processes.
- Reply text can slightly paraphrase (`waffle-42` vs `waffle-iron-42`) — GBNF reply is a short string, not a quote engine.
- Discord bot patches live in `~/vibe-hub/discord-bots`, not this git repo.
- `encrypted_key` is JSON plaintext unless we add CREDENTIALS_KEY later.
