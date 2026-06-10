"""jarvis_store.py — global settings for the Jarvis agent.

Jarvis is a single cross-analysis agent. The set of analyses it is allowed to read
("enabled analyses") is a GLOBAL singleton row (id='global') in the
public.jarvis_settings table — toggled from the frontend settings tab.

This module reuses analysis_store's service-role REST helpers; the table name is a
module constant (never supplied by a caller/model), and ids are UUID-validated, so
there is no arbitrary-table or raw-SQL path here.

All functions are synchronous (httpx, via analysis_store). Async callers should
wrap them in asyncio.to_thread.
"""
from __future__ import annotations

import analysis_store as store

T_SETTINGS = "jarvis_settings"
_SINGLETON = "global"


def _clean_ids(ids) -> list[str]:
    """Keep only well-formed UUID strings, de-duplicated, order preserved."""
    out: list[str] = []
    seen: set[str] = set()
    for x in (ids or []):
        s = str(x).strip()
        if store._UUID_RE.match(s) and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _row() -> dict:
    """The raw singleton settings row (or {} if it hasn't been seeded yet)."""
    return store._first(
        store._select(T_SETTINGS, filters=[f"id=eq.{_SINGLETON}"], limit=1)
    ) or {}


def get_settings() -> dict:
    """The full global Jarvis settings: the enabled-analyses list and the editable
    system prompt. system_prompt is "" when unset (the backend then uses its
    default persona)."""
    row = _row()
    return {
        "enabled_analysis_ids": _clean_ids(row.get("enabled_analysis_ids")),
        "system_prompt": (row.get("system_prompt") or ""),
    }


def set_settings(*, enabled_analysis_ids=None, system_prompt=None) -> dict:
    """Partial update of the global settings. Only the fields passed (non-None) are
    written, so the frontend can save the toggles and the prompt independently.
    Pass system_prompt="" to clear it back to the backend default. Returns the
    full settings after the write."""
    patch = {"id": _SINGLETON, "updated_at": store._now()}
    if enabled_analysis_ids is not None:
        patch["enabled_analysis_ids"] = _clean_ids(enabled_analysis_ids)
    if system_prompt is not None:
        patch["system_prompt"] = str(system_prompt)
    store._upsert(T_SETTINGS, patch, on_conflict="id", returning=False)
    return get_settings()


def get_enabled_analysis_ids() -> list[str]:
    """The list of analysis ids Jarvis is currently allowed to read."""
    return _clean_ids(_row().get("enabled_analysis_ids"))


def set_enabled_analysis_ids(ids) -> list[str]:
    """Replace the enabled-analyses list. Returns the cleaned list that was saved."""
    return set_settings(enabled_analysis_ids=ids)["enabled_analysis_ids"]


def get_system_prompt() -> str:
    """The editable Jarvis system prompt ("" => use the backend default)."""
    return _row().get("system_prompt") or ""


def get_enabled_analyses() -> list[dict]:
    """Enabled analyses as lightweight dicts (id, title, status), skipping any id
    whose analysis no longer exists."""
    out: list[dict] = []
    for aid in get_enabled_analysis_ids():
        a = store.get_analysis(aid)
        if a:
            out.append({
                "id": a["id"],
                "title": a.get("title"),
                "status": a.get("status"),
            })
    return out
