"""deal_trigger_log.py — audit log for Deal Engine analysis runs.

One row per analyze_one() execution (bulk sweep / manual re-run / Salesforce
trigger). Functional, dependency-light httpx layer over Supabase REST using the
service-role key. The table/view names are MODULE CONSTANTS, never taken from the
caller. There is no generic "run arbitrary SQL" path here.

Writes are BEST-EFFORT: logging a run must never break the analysis itself, so
log_run swallows its own errors. Reads raise so the API surfaces real failures.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

_SUPABASE_URL = (os.environ.get("SUPABASE_URL", "") or "").rstrip("/")
_SERVICE_KEY = (
    os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    or os.environ.get("SUPABASE_SERVICE_KEY", "")
)

# Hard-scoped names — constants, never supplied by the caller.
T_RUNS = "deal_trigger_runs"
V_LATEST = "deal_trigger_latest"

_TIMEOUT = 30.0


def _ready() -> bool:
    return bool(_SUPABASE_URL and _SERVICE_KEY)


def _headers(write: bool = False) -> dict:
    h = {
        "apikey": _SERVICE_KEY,
        "Authorization": f"Bearer {_SERVICE_KEY}",
        "Content-Type": "application/json",
    }
    if write:
        h["Prefer"] = "return=minimal"
    return h


def _rest(path: str) -> str:
    return f"{_SUPABASE_URL}/rest/v1/{path}"


def log_run(row: dict) -> None:
    """Insert one run row. Best-effort — never raises into the caller."""
    if not _ready():
        return
    try:
        r = httpx.post(_rest(T_RUNS), headers=_headers(write=True),
                       json=row, timeout=_TIMEOUT)
        if r.status_code >= 400:
            print(f"[DEAL-TRIGGER-LOG] log_run HTTP {r.status_code}: {r.text[:300]}",
                  flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[DEAL-TRIGGER-LOG] log_run failed: {type(e).__name__}: {e}", flush=True)


def list_latest(limit: int = 500) -> list[dict]:
    """Latest run per opp, newest first — the dashboard list."""
    if not _ready():
        return []
    r = httpx.get(_rest(V_LATEST), headers=_headers(),
                  params={"select": "*", "order": "last_run_at.desc",
                          "limit": str(max(1, limit))},
                  timeout=_TIMEOUT)
    r.raise_for_status()
    return r.json()


def list_runs_for_opp(opp_id: str, limit: int = 200) -> list[dict]:
    """Full run history for one opp (matched on the 15-char SF key), newest first."""
    if not _ready():
        return []
    key = (opp_id or "")[:15]
    r = httpx.get(_rest(T_RUNS), headers=_headers(),
                  params={"select": "*", "opp_id_15": f"eq.{key}",
                          "order": "created_at.desc", "limit": str(max(1, limit))},
                  timeout=_TIMEOUT)
    r.raise_for_status()
    return r.json()


def count_validation_violations(hours: int = 24) -> dict:
    """How many anti-fabrication violations the gate caught in the last `hours`.

    Returns {"runs_with_violations": int, "total_violations": int, "window_hours": int}.
    A "violation" is a fabricated/placeholder fact the deterministic gate stripped
    before persistence; this counter proves the gate is actually firing in prod.
    Best-effort: returns zeros if the column/table is missing or anything fails, so
    the dashboard never breaks on a not-yet-migrated DB."""
    out = {"runs_with_violations": 0, "total_violations": 0, "window_hours": int(hours)}
    if not _ready():
        return out
    try:
        since = (datetime.now(timezone.utc) - timedelta(hours=max(1, int(hours)))) \
            .isoformat()
        r = httpx.get(_rest(T_RUNS), headers=_headers(),
                      params={"select": "validation_violations",
                              "created_at": f"gte.{since}",
                              "validation_violations": "gt.0"},
                      timeout=_TIMEOUT)
        if r.status_code >= 400:
            print(f"[DEAL-TRIGGER-LOG] count_validation_violations HTTP "
                  f"{r.status_code}: {r.text[:200]}", flush=True)
            return out
        rows = r.json() or []
        out["runs_with_violations"] = len(rows)
        out["total_violations"] = sum(
            int(x.get("validation_violations") or 0) for x in rows)
    except Exception as e:  # noqa: BLE001
        print(f"[DEAL-TRIGGER-LOG] count_validation_violations failed: "
              f"{type(e).__name__}: {e}", flush=True)
    return out
