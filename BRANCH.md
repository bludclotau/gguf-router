# feature/agent-tools

Ready for review. Do not merge until you're happy with the remaining nits.

## Done

- **GBNF planner** and multi-step browse (goto/read/click) as before.
- **Credential encryption at rest**: Fernet (`enc:v1:…`) via `CREDENTIALS_KEY` or `data/credentials.key` (gitignored, 0600). Legacy plaintext JSON rows are migrated on startup. Decrypt only inside `get_credential` → `browser_login`. Events/sessions/memories never store usernames or passwords. `list_credential_sites` returns hostnames only.
- **Discord client in this repo**: `clients/discord/dolphin.js` is the router contract (ack-then-edit, blocked replies, `agent: true`). Live bots still run from **`bludclotau/llm-multibot-cluster`** (`~/vibe-hub/discord-bots`, branch `dev`) — keep `shared/dolphin.js` there in sync with this copy.
- Durable memories are injected into the planner prompt. Recall questions use reply-only grammar so the model answers from `memories` instead of browsing.

## Still later

- Screenshots
- Multi-worker Chromium
- Reply-string paraphrasing under GBNF
- Optional `CREDENTIALS_KEY` rotation / pgcrypto
