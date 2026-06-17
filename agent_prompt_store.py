"""agent_prompt_store.py — persistent admin override for the chat/completion
agent's system prompt (the "instructions" set from Admin -> Agent Control).

Persisted as a singleton row in the EXISTING public.jarvis_settings table under a
distinct id, so no new table / migration is required. Reuses analysis_store's
service-role REST helpers (same pattern as jarvis_store). All functions are
synchronous (httpx); async callers wrap them in asyncio.to_thread.

When the override is unset ("") the agent falls back to the per-request system
prompt and then the built-in default — i.e. behaviour is byte-identical to today
until an admin saves an override. get_prompt() never raises (returns "" if the
settings table is unavailable) so the chat path is never blocked by this.
"""
from __future__ import annotations

import analysis_store as store

T_SETTINGS = "jarvis_settings"
_ID = "mase_chat_agent"


def get_prompt() -> str:
    """The admin instruction override for the chat/completion agent ("" => unset).
    Never raises — a missing table / REST blip degrades to no-override."""
    try:
        row = store._first(
            store._select(T_SETTINGS, filters=[f"id=eq.{_ID}"], limit=1)
        ) or {}
    except Exception:  # noqa: BLE001 — never block the chat path on this read
        return ""
    return (row.get("system_prompt") or "")


def set_prompt(prompt: str) -> str:
    """Persist the override (pass "" to clear it). Returns the stored value.
    Raises on a hard REST failure so the caller (endpoint) can surface it."""
    store._upsert(
        T_SETTINGS,
        {"id": _ID, "system_prompt": str(prompt or ""), "updated_at": store._now()},
        on_conflict="id", returning=False,
    )
    return get_prompt()
