"""agent_prompt_store.py — persistent admin overrides for MASE agent system
prompts. SUPABASE IS THE RUNTIME SOURCE OF TRUTH for these prompts.

Each agent's prompt is stored as a singleton row in the EXISTING
public.jarvis_settings table, keyed by a distinct `id` (the agent key) — so no new
table / migration is required. Reuses analysis_store's service-role REST helpers
(same pattern as jarvis_store).

Prompt keys (jarvis_settings.id) — one row per agent:
  - ID_TODO_RUNNER ("mase_todo_runner") — the Tactical Fulfillment / "Run with AI"
        drafting agent (the to-do runner that drafts an outbound email for a
        to-do). Override is OPTIONAL: "" => fall back to the version-controlled
        seed shipped on disk at prompts/todo_runner_system_prompt.md.
  - ID_DEAL_SWEEP  ("mase_deal_sweep")  — the Deal Intelligence Engine SWEEP agent
        (deal_engine_sweep.py). Override is OPTIONAL: "" => fall back to the
        version-controlled seed shipped on disk at
        prompts/deal_engine_sweep_system_prompt.md.
  - ID_CHAT        ("mase_chat_agent")  — the generic conversational chat agent
        (the chat page). Override is OPTIONAL: "" => fall back to the per-request
        prompt and then the deep-agent built-in.

These agents are DISTINCT: editing one prompt never touches another.

All functions are synchronous (httpx); async callers wrap them in
asyncio.to_thread (see server.py `_aw`). get_prompt() never raises (returns "" if
the settings table is unavailable) so no agent path is ever blocked by this read —
callers degrade to their own built-in / on-disk default.
"""
from __future__ import annotations

import analysis_store as store

T_SETTINGS = "jarvis_settings"

# Prompt keys (jarvis_settings.id). One row per agent.
ID_CHAT = "mase_chat_agent"
ID_DEAL_SWEEP = "mase_deal_sweep"
ID_TODO_RUNNER = "mase_todo_runner"
# Deal SCORING agent — judges momentum/win/commitment/risk/forecast over the
# deterministic evidence packet (deal_engine_ai_scoring.py). Override OPTIONAL:
# "" => fall back to the seed at prompts/deal_engine_scoring_system_prompt.md.
ID_DEAL_SCORING = "mase_deal_scoring"
# Deal Sweep January 1.0 — the RevOps Head strategic editor: runs LAST (after the
# compliance QI), on standard+deep deals only, over the UI-bound record. Seed on
# disk at prompts/mase_revops_head.md. "" => fall back to that seed.
ID_REVOPS_HEAD = "mase_revops_head"

# Back-compat alias for the original single-prompt (chat) callers.
_ID = ID_CHAT


def get_prompt(agent_id: str = ID_CHAT) -> str:
    """The admin prompt override stored in Supabase for `agent_id` ("" => unset).
    Never raises — a missing table / REST blip degrades to no-override so the
    caller falls back to its built-in / on-disk default."""
    try:
        row = store._first(
            store._select(T_SETTINGS, filters=[f"id=eq.{agent_id}"], limit=1)
        ) or {}
    except Exception:  # noqa: BLE001 — never block an agent path on this read
        return ""
    return (row.get("system_prompt") or "")


def set_prompt(prompt: str, agent_id: str = ID_CHAT) -> str:
    """Persist the override for `agent_id` to Supabase (pass "" to clear it).
    Returns the stored value. Raises on a hard REST failure so the caller
    (endpoint) can surface it."""
    store._upsert(
        T_SETTINGS,
        {"id": agent_id, "system_prompt": str(prompt or ""), "updated_at": store._now()},
        on_conflict="id", returning=False,
    )
    return get_prompt(agent_id)


def strip_leading_banner(text: str) -> str:
    """The on-disk prompt SEED files (prompts/*.md) carry a DEPRECATION banner as a
    single leading HTML comment (Supabase is the source of truth; the files are only
    a cold-start fallback). Strip ONE leading `<!-- ... -->` block so that banner can
    never enter the actual system prompt when a disk seed is used, nor the "default"
    shown in the admin editor. A comment anywhere else in the file is left untouched."""
    t = (text or "").lstrip()
    if t.startswith("<!--"):
        end = t.find("-->")
        if end != -1:
            return t[end + 3:].lstrip()
    return text
