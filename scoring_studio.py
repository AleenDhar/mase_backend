"""Scoring Version Studio — control-plane API logic (Supabase `scoring_instructions`).

EIGHT versioned assets (2026-07-09, per the governance prototype landed in the MASE repo,
scoring-studio/index.html):
- SIX ENGINES: extract · win · mom · todo · sum · **sweep** (Deal Sweep / Deal Drawer — the
  canonical-record layer; its locked content REPLACES the base sweep system prompt).
- TWO REFERENCE ASSETS: **vendordict** (Vendor Dictionary — canonical vendor entity-resolution
  glossary) · **playbook** (Zycus Deal-Progression Playbook). Engines cite them with stable
  `{{ref:vendor-dictionary}}` / `{{ref:deal-playbook}}` tokens; the runtime resolves the token
  to a pointer and appends ONE locked copy of each cited reference (see resolve_refs /
  reference_sections).

Contract (per the staging handoff):
- Independent semver per asset (text '10.3'; sorted numerically).
- ONE draft max per asset: a row with version='draft', locked=false. Editing updates it.
- LOCK requires a changelog note + kind (minor|major); computes the next version from the
  latest locked, stamps locked_by/locked_at. Runtime must only ever execute LOCKED versions
  (`active_locked()` is the resolver); an unlocked draft is invisible to the runtime.
- Super-admin enforcement lives in the FRONTEND PROXY (same convention as the other
  Admin/Agent-Control endpoints); the backend trusts the service bearer.
"""
from __future__ import annotations
import datetime
import os
import re
from typing import Optional

import httpx

ENGINES = ("extract", "win", "mom", "todo", "sum", "sweep")
REFERENCES = ("vendordict", "playbook")
ASSETS = ENGINES + REFERENCES
ENGINE_NAMES = {
    "extract": "Signal Extraction / Deal-Reading",
    "win": "Zycus Win Position",
    "mom": "Deal Momentum",
    "todo": "To-Do Generation",
    "sum": "24-Hour Summary",
    "sweep": "Deal Sweep (Deal Drawer)",
    "vendordict": "Vendor Dictionary",
    "playbook": "Deal Playbook",
}
# {{ref:<token>}} → asset key (tokens are the stable citation ids used inside engine texts)
REF_TOKENS = {"vendor-dictionary": "vendordict", "deal-playbook": "playbook"}
_REF_TOKEN_RE = re.compile(r"\{\{\s*ref:([a-z0-9_\-]+)\s*\}\}", re.I)

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
    kw.setdefault("headers", _h())   # GETs must carry the apikey too (401 without it)
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
    if engine not in ASSETS:
        raise StudioError(f"unknown engine {engine!r}")
    return _req("GET", f"{_T}?engine=eq.{engine}&select={select}").json()


def _latest_locked(engine: str, with_content: bool = False) -> Optional[dict]:
    sel = "engine,version,kind,note,locked,locked_by,locked_at,created_at" + (",content" if with_content else "")
    rows = [r for r in _rows(engine, sel) if r.get("locked") and r.get("version") != "draft"]
    rows.sort(key=lambda r: _vkey(r["version"]), reverse=True)
    return rows[0] if rows else None


def list_engines() -> list[dict]:
    """One card per asset (engines + reference assets): active locked version + draft state."""
    out = []
    for e in ASSETS:
        rows = _rows(e, "version,kind,note,locked,locked_by,locked_at,created_at")
        locked = sorted((r for r in rows if r["locked"]), key=lambda r: _vkey(r["version"]), reverse=True)
        draft = next((r for r in rows if r["version"] == "draft" and not r["locked"]), None)
        _kind = "reference" if e in REFERENCES else "engine"
        _tok = next((t for t, k in REF_TOKENS.items() if k == e), None)
        out.append({
            "engine": e, "name": ENGINE_NAMES[e],
            "kind": _kind,
            "ref_token": (f"{{{{ref:{_tok}}}}}" if _tok else None),
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
    if engine not in ASSETS:
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
        new_v = "1.0" if engine in REFERENCES else "10.0"
    _req("PATCH", f"{_T}?id=eq.{draft[0]['id']}", headers=_h("return=minimal"),
         json={"version": new_v, "kind": kind, "note": note.strip(), "locked": True,
               "locked_by": (locked_by or "").strip() or "unknown", "locked_at": _now()})
    return {"ok": True, "engine": engine, "version": new_v, "locked": True}


def active_locked() -> dict:
    """RUNTIME RESOLVER (the lock-before-run gate's read side): the latest LOCKED
    instruction per asset (engines AND reference assets), with content. Drafts are
    invisible here by design."""
    out = {}
    for e in ASSETS:
        row = _latest_locked(e, with_content=True)
        out[e] = ({"version": row["version"], "content": row["content"]} if row else None)
    return out


# ---------------------------------------------------------------------------
# Reference-asset resolution ({{ref:<token>}} citations)
# ---------------------------------------------------------------------------
def resolve_refs(text: str, active: dict) -> tuple[str, set]:
    """Replace every {{ref:<token>}} citation in `text` with a short pointer to the
    reference section (the full locked content is appended ONCE via
    reference_sections, never inlined N times). Returns (resolved_text, cited_keys).
    Unknown tokens are left verbatim (never guessed)."""
    cited: set = set()

    def _sub(m):
        tok = m.group(1).strip().lower()
        key = REF_TOKENS.get(tok)
        row = active.get(key) if key else None
        if not row:
            return m.group(0)          # unknown or unlocked → leave the token as-is
        cited.add(key)
        return (f"(see REFERENCE — {ENGINE_NAMES[key]} · LOCKED v{row['version']}, "
                f"appended below)")

    return _REF_TOKEN_RE.sub(_sub, text or ""), cited


def reference_sections(active: dict, keys=None) -> tuple[str, dict]:
    """The locked reference assets rendered as appended sections (ONE copy each).
    keys=None → all locked references. Returns (sections_text, versions{key: version})."""
    parts, versions = [], {}
    for key in (keys if keys is not None else REFERENCES):
        row = active.get(key)
        if not row or not (row.get("content") or "").strip():
            continue
        versions[key] = row["version"]
        parts.append(f"### REFERENCE — {ENGINE_NAMES[key]} · LOCKED v{row['version']}\n"
                     f"(cited by the engines above; the single source of truth for this asset)\n\n"
                     f"{row['content']}")
    return ("\n\n".join(parts), versions)


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
