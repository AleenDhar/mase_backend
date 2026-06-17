---
name: Prompts source of truth (Supabase)
description: Agent system prompts live in Supabase, NOT the local prompts/*.md files; edit Supabase to change behaviour.
---

# Prompts source of truth (Supabase)

MASE agent **system prompts are stored in and served from Supabase** — the
`public.jarvis_settings` table, one row per agent keyed by `id`. **Supabase is the
SOURCE OF TRUTH and always wins.**

| Agent | `jarvis_settings.id` | `agent_prompt_store` key | Edit from |
| --- | --- | --- | --- |
| Deal sweep (`deal_engine_sweep.py`) | `mase_deal_sweep` | `ID_DEAL_SWEEP` | Admin → Agent Control → Deal Sweep · `POST /api/deal-engine/sweep/prompt` |
| Todo Runner ("Run with AI", `components/agent/AgentRun.tsx`) | `mase_todo_runner` | `ID_TODO_RUNNER` | Admin → Agent Control → Todo Runner · `POST /api/deal-engine/todo-runner/prompt` |
| Chat agent | `mase_chat_agent` | `ID_CHAT` | chat page Admin prompt panel · `POST /api/deal-engine/chat/prompt` |

**Why:** admins edit prompts live without a redeploy, and there is ONE authoritative
copy. The deal-sweep agent re-resolves its prompt on a 15s TTL and rebuilds the cached
agent on a fingerprint change (`deal_engine_sweep._get_agent`); the todo-runner fetches
its prompt per run; the chat path reads it per message.

**How to apply:**
- To change behaviour, **edit the Supabase prompt** (Admin UI or the endpoint). NEVER
  edit `prompts/deal_engine_sweep_system_prompt.md` or `prompts/todo_runner_system_prompt.md`
  to change live behaviour — they are now only the cold-start SEED/fallback (used only
  if the Supabase row is missing) and carry a `⚠️ DEPRECATED` banner.
- The banner is a leading HTML comment stripped at load by
  `agent_prompt_store.strip_leading_banner`, so it never pollutes the prompt. If you
  add/keep a banner, keep it as a single leading `<!-- ... -->` block.
- `_load_prompt` / `_disk_prompt` (deal sweep) and `_todo_runner_seed_prompt` (server)
  implement Supabase-first, seed-fallback. The Admin editor's "Reset to default" is the
  only path back to the on-disk seed.
- Related: [[salesforce-write-lockdown]] (another standing "edit here, not there" rule).
