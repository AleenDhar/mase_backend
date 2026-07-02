"""Supabase-backed store for the MASE Teams bot control room.

Three concerns: the allowlist (who may use the bot), the activity log, and settings
flags (enforce_allowlist, history_enabled). Reuses the httpx-REST helpers in
analysis_store.py (service-role key, same posture as the other MASE stores).

All functions are best-effort at the call site: the bot wraps allowlist checks to
fail OPEN (allow) if the store is unreachable, so a DB blip never locks users out.
"""

from typing import Optional

from analysis_store import _select, _insert, _patch, _delete, _upsert, _first, _now

ALLOWLIST = "teams_bot_allowlist"
ACTIVITY = "teams_bot_activity"
SETTINGS = "teams_bot_settings"


# ───────────────────────── settings ─────────────────────────
def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    r = _first(_select(SETTINGS, filters=[f"key=eq.{key}"], limit=1))
    return r["value"] if r else default


def set_setting(key: str, value) -> None:
    _upsert(SETTINGS, {"key": key, "value": str(value), "updated_at": _now()},
            on_conflict="key", returning=False)


def _flag(key: str, default: bool = False) -> bool:
    v = get_setting(key, None)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def enforce_allowlist() -> bool:
    return _flag("enforce_allowlist", False)


def history_enabled() -> bool:
    return _flag("history_enabled", False)


def all_settings() -> dict:
    return {"enforce_allowlist": enforce_allowlist(), "history_enabled": history_enabled()}


# ───────────────────────── allowlist ─────────────────────────
def list_allowlist() -> list:
    return _select(ALLOWLIST, order="added_at.desc")


def add_allowlist(*, email: Optional[str] = None, display_name: Optional[str] = None,
                  aad_object_id: Optional[str] = None, added_by: Optional[str] = None) -> dict:
    email = (email or "").strip().lower() or None
    if email:
        existing = _first(_select(ALLOWLIST, filters=[f"email=eq.{email}"], limit=1))
        if existing:  # re-add = re-enable + refresh name
            _patch(ALLOWLIST, {"id": existing["id"]},
                   {"enabled": True, "display_name": display_name or existing.get("display_name"),
                    "updated_at": _now()}, returning=False)
            return {"id": existing["id"], "reactivated": True}
    row = {"email": email, "display_name": display_name, "aad_object_id": aad_object_id,
           "enabled": True, "added_by": added_by, "added_at": _now(), "updated_at": _now()}
    res = _insert(ALLOWLIST, row)
    return _first(res) or {}


def remove_allowlist(row_id: str) -> None:
    _delete(ALLOWLIST, {"id": row_id})


def set_allowlist_enabled(row_id: str, enabled: bool) -> None:
    _patch(ALLOWLIST, {"id": row_id}, {"enabled": bool(enabled), "updated_at": _now()},
           returning=False)


def is_allowed(email: Optional[str] = None, aad_object_id: Optional[str] = None) -> bool:
    """True if the user may use the bot. Enforcement OFF => everyone allowed."""
    if not enforce_allowlist():
        return True
    email = (email or "").strip().lower()
    for r in _select(ALLOWLIST, filters=["enabled=eq.true"]):
        if email and (r.get("email") or "").strip().lower() == email:
            return True
        if aad_object_id and r.get("aad_object_id") == aad_object_id:
            return True
    return False


# ───────────────────────── activity log ─────────────────────────
def log_activity(*, conversation_id=None, conversation_type=None, user_name=None,
                 user_email=None, direction=None, status=None, text=None, detail=None) -> None:
    try:
        _insert(ACTIVITY, {
            "ts": _now(), "conversation_id": conversation_id,
            "conversation_type": conversation_type, "user_name": user_name,
            "user_email": user_email, "direction": direction, "status": status,
            "text": (text or "")[:2000], "detail": (detail or "")[:1000],
        }, returning=False)
    except Exception as e:  # noqa: BLE001 — logging must never break the bot
        print(f"[TEAMS BOT] activity log failed: {e}")


def recent_activity(limit: int = 50) -> list:
    return _select(ACTIVITY, order="ts.desc", limit=limit)
