# feature/agent-tools

Working branch for the bounded browser-agent slice. Ready for review; do not merge until you're happy with the remaining nits.

## Done

- **Every persona** in `personas.json` is an `agents` row (wendy, gumbo, tabatha, tech, creative, analysis), each with its own Playwright context, `sessions.context`, `events`, and `memories`.
- Stateful Playwright: one Chromium context per persona. `storageState()` → `data/browser/<persona>.json` + `tools.tool_state`. Closed/crashed pages are recreated. Cookies are not saved on a blocked challenge page.
- Tools: `browser_goto`, `browser_read`, `browser_click`, `browser_type`, `browser_submit`, `web_fetch`. Same `/tool` shape.
- Blocked pages return `status=blocked` plus a human `message` (CAPTCHA, Cloudflare wait, login wall, timeout). Discord edits the ack instead of going silent.
- Login-wall heuristic no longer fires on content pages that merely have a header "Log in" link — needs a visible password field *and* a login URL/title.
- Memory split: `sessions.context` (trimmed scratch, also updated from `/tool`), `events` (audit), `memories` (extractive notes after a loop / every 8 events).
- Bounded loop: 6 steps, 60s wall clock, always leaves time for one model call. URL bootstrap goto+read; if that hits a wall the user gets the blocked message immediately.
- Discord: `@bot !browse` / `!web` ack-then-edit; catch path edits the placeholder so it never stays "On it…".
- Unit tests: `python3 router/tests/test_hardening.py`
- Verified live: all six personas goto+read example.{com,net,org}; gumbo agent loop wrote session + memory.

## Still rough (acceptable for review)

- Local GGUF models still rarely emit `{"tool":...}`; URL bootstrap remains the planner for `!browse`.
- No screenshots, no auto-login from `credentials`.
- One Chromium process — don't run multiple uvicorn workers.
- Discord bot patches live in `~/vibe-hub/discord-bots`, not this git repo.
