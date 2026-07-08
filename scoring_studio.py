"""Scoring Version Studio — control-plane API logic (Supabase `scoring_instructions`).

Five engines: extract · win · mom · todo · sum. Contract (per the staging handoff):
- Independent semver per engine (text '10.3'; sorted numerically).
- ONE draft max per engine: a row with version='draft', locked=false. Editing updates it.
- LOCK requires a changelog note + kind (minor|major); computes the next version from the
  latest locked, stamps locked_by/locked_at. Runtime must only ever execute LOCKED versions
  (`active_locked()` is the resolver); an unlocked draft is invisible to the runtime.
- Super-admin enforcement lives in the FRONTEND PROXY (same convention as the other
  Admin/Agent-Control endpoints); the backend trusts the service bearer.
"""
from __future__ import annotations
import datetime
import os
from typing import Optional

import httpx

ENGINES = ("extract", "win", "mom", "todo", "sum")
ENGINE_NAMES = {
    "extract": "Signal Extraction / Deal-Reading",
    "win": "Zycus Win Position",
    "mom": "Deal Momentum",
    "todo": "To-Do Generation",
    "sum": "24-Hour Summary",
}

_SB = (os.environ.get("SUPABASE_URL", "") or "").rstrip("/")
_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_SERVICE_KEY", "")
_T = "scoring_instructions"


class StudioError(Exception):
    pass


def _h(prefer: str = "") -> dict:
    h = {"apikey": _KEY, "Authorization": f"Bearer {_KEY}", "Content-Type": "application/json"}
    if prefer:
        h["Prefer"] = prefer
    return h


def _req(method: str, path: str, **kw) -> httpx.Response:
    if not _SB or not _KEY:
        raise StudioError("Supabase is not configured")
    r = httpx.request(method, f"{_SB}/rest/v1/{path}", timeout=30, **kw)
    if r.status_code >= 400:
        raise StudioError(f"{method} {path}: HTTP {r.status_code} {r.text[:300]}")
    return r


def _vkey(v: str):
    try:
        return tuple(int(x) for x in str(v).split("."))
    except ValueError:
        return (-1,)  # 'draft' and malformed sort below any real version


def _rows(engine: str, select: str) -> list[dict]:
    if engine not in ENGINES:
        raise StudioError(f"unknown engine {engine!r}")
    return _req("GET", f"{_T}?engine=eq.{engine}&select={select}").json()


def _latest_locked(engine: str, with_content: bool = False) -> Optional[dict]:
    sel = "engine,version,kind,note,locked,locked_by,locked_at,created_at" + (",content" if with_content else "")
    rows = [r for r in _rows(engine, sel) if r.get("locked") and r.get("version") != "draft"]
    rows.sort(key=lambda r: _vkey(r["version"]), reverse=True)
    return rows[0] if rows else None


def list_engines() -> list[dict]:
    """One card per engine: active locked version + whether a draft exists."""
    out = []
    for e in ENGINES:
        rows = _rows(e, "version,kind,note,locked,locked_by,locked_at,created_at")
        locked = sorted((r for r in rows if r["locked"]), key=lambda r: _vkey(r["version"]), reverse=True)
        draft = next((r for r in rows if r["version"] == "draft" and not r["locked"]), None)
        out.append({
            "engine": e, "name": ENGINE_NAMES[e],
            "active": ({k: locked[0].get(k) for k in ("version", "kind", "note", "locked_by", "locked_at")}
                       if locked else None),
            "has_draft": bool(draft),
            "draft_saved_at": (draft or {}).get("created_at"),
            "versions": len([r for r in rows if r["version"] != "draft"]),
        })
    return out


def trail(engine: str) -> dict:
    """Full version history (no content — fetch a version for its text)."""
    rows = _rows(engine, "version,kind,note,locked,locked_by,locked_at,created_at")
    real = [r for r in rows if r["version"] != "draft"]
    real.sort(key=lambda r: _vkey(r["version"]), reverse=True)
    draft = next((r for r in rows if r["version"] == "draft" and not r["locked"]), None)
    return {"engine": engine, "name": ENGINE_NAMES.get(engine), "trail": real, "draft": draft}


def get_version(engine: str, version: str) -> dict:
    rows = _req("GET", f"{_T}?engine=eq.{engine}&version=eq.{version}"
                       "&select=engine,version,kind,note,locked,locked_by,locked_at,created_at,content").json()
    if not rows:
        raise StudioError(f"{engine} v{version} not found")
    return rows[0]


def save_draft(engine: str, content: str, author: str = "") -> dict:
    """Create/replace the single draft row for an engine. While it exists unlocked,
    the engine is BLOCKED from adopting any new instruction (lock-before-run)."""
    if engine not in ENGINES:
        raise StudioError(f"unknown engine {engine!r}")
    if not (content or "").strip():
        raise StudioError("draft content is empty")
    existing = _req("GET", f"{_T}?engine=eq.{engine}&version=eq.draft&select=id,locked").json()
    if existing and existing[0].get("locked"):
        raise StudioError("draft row is unexpectedly locked — refusing to overwrite")
    note = f"(unlocked draft{' by ' + author if author else ''})"
    if existing:
        _req("PATCH", f"{_T}?engine=eq.{engine}&version=eq.draft",
             headers=_h("return=minimal"),
             json={"content": content, "note": note, "created_at": _now()})
    else:
        _req("POST", _T, headers=_h("return=minimal"),
             json={"engine": engine, "version": "draft", "kind": "minor",
                   "note": note, "content": content, "locked": False})
    return {"ok": True, "engine": engine, "draft": True}


def discard_draft(engine: str) -> dict:
    _req("DELETE", f"{_T}?engine=eq.{engine}&version=eq.draft&locked=eq.false", headers=_h())
    return {"ok": True, "engine": engine, "draft": False}


def lock(engine: str, kind: str, note: str, locked_by: str) -> dict:
    """Promote the draft to the next version and LOCK it. Requires a changelog note."""
    kind = (kind or "").strip().lower()
    if kind not in ("minor", "major"):
        raise StudioError("kind must be 'minor' or 'major'")
    if not (note or "").strip():
        raise StudioError("a changelog note is required to lock")
    draft = _req("GET", f"{_T}?engine=eq.{engine}&version=eq.draft&locked=eq.false&select=id,content").json()
    if not draft:
        raise StudioError("no unlocked draft to lock — save a draft first")
    cur = _latest_locked(engine)
    if cur:
        major, minor = _vkey(cur["version"])[0], _vkey(cur["version"])[1] if len(_vkey(cur["version"])) > 1 else 0
        new_v = f"{major}.{minor + 1}" if kind == "minor" else f"{major + 1}.0"
    else:
        new_v = "10.0"
    _req("PATCH", f"{_T}?id=eq.{draft[0]['id']}", headers=_h("return=minimal"),
         json={"version": new_v, "kind": kind, "note": note.strip(), "locked": True,
               "locked_by": (locked_by or "").strip() or "unknown", "locked_at": _now()})
    return {"ok": True, "engine": engine, "version": new_v, "locked": True}


def active_locked() -> dict:
    """RUNTIME RESOLVER (the lock-before-run gate's read side): the latest LOCKED
    instruction per engine, with content. Drafts are invisible here by design."""
    out = {}
    for e in ENGINES:
        row = _latest_locked(e, with_content=True)
        out[e] = ({"version": row["version"], "content": row["content"]} if row else None)
    return out


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
