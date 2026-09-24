# Discord client (router contract)

The live Discord bots run from **`llm-multibot-cluster`**
(`~/vibe-hub/discord-bots`, GitHub `bludclotau/llm-multibot-cluster`, branch `dev`).
They are a separate repo because they hold many bots, systemd units, and
non-router features.

This directory is the **router-facing contract** that those bots must keep
in sync with `gguf-router`:

| File | Role |
|---|---|
| `dolphin.js` | `callRouter` / `callTool`, retries, `agent: true`, `formatAgentReply` (blocked messages), `editWhenDone` (ack-then-edit so Discord never stays on “On it…”) |

Copy into `shared/dolphin.js` of llm-multibot-cluster. `shared/bot.js` and
`shared/bot-runtime.js` in that repo call `editWhenDone` for `!browse` / `!web`
and for normal replies.

If you change `/route` or `/tool` response shape, update **both** this copy
and llm-multibot-cluster in the same change set.
