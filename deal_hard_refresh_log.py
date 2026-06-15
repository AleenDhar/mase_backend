"""deal_hard_refresh_log.py — append-only history of token-free hard-fact runs.

One row per hard_refresh_all() execution (nightly scheduler or manual REST call).
The hard refresh otherwise persists only the MOST RECENT summary (in-memory +
.deal_engine_hard_refresh_last.json); this log keeps a durable trail so the
nightly schedule is auditable and an anomalous run (unusually high/low updated /
removed counts) can be spotted over time.

Functional, dependency-light httpx layer over Supabase REST using the
service-role key. The table name is a MODULE CONSTANT, never taken from the
caller. There is no generic "run arbitrary SQL" path here.

Writes are BEST-EFFORT: logging a run must never break the refresh itself, so
log_run swallows its own errors. Reads raise so the API surfaces real failures.

Mirrors deal_trigger_log.py.
"""
from __future__ import annotations

import os

import httpx

_SUPABASE_URL = (os.environ.get("SUPABASE_URL", "") or "").rstrip("/")
_SERVICE_KEY = (
    os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    or os.environ.get("SUPABASE_SERVICE_KEY", "")
)

# Hard-scoped name — a constant, never supplied by the caller.
T_RUNS = "deal_hard_refresh_runs"

# Columns the table accepts; anything else in the summary dict is dropped so a
# stray key (e.g. removed_opps) never breaks the insert.
_COLS = {
    "source", "status", "records", "matched", "updated", "removed",
    "unmatched", "failed", "skipped", "error", "finished_at",
}

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


def log_run(summary: dict) -> None:
    """Append one hard-refresh run row. Best-effort — never raises into the caller.

    Accepts the summary dict hard_refresh_all() returns (records / matched /
    updated / removed / unmatched / failed / source / finished_at) or a skip
    summary ({"skipped": "..."}). Unknown keys are dropped."""
    if not _ready():
        return
    try:
        row = {k: v for k, v in (summary or {}).items() if k in _COLS}
        r = httpx.post(_rest(T_RUNS), headers=_headers(write=True),
                       json=row, timeout=_TIMEOUT)
        if r.status_code >= 400:
            print(f"[DEAL-HARD-REFRESH-LOG] log_run HTTP {r.status_code}: "
                  f"{r.text[:300]}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[DEAL-HARD-REFRESH-LOG] log_run failed: {type(e).__name__}: {e}",
              flush=True)


def list_runs(limit: int = 200) -> list[dict]:
    """Recent hard-refresh runs, newest first — the audit history."""
    if not _ready():
        return []
    r = httpx.get(_rest(T_RUNS), headers=_headers(),
                  params={"select": "*", "order": "created_at.desc",
                          "limit": str(max(1, limit))},
                  timeout=_TIMEOUT)
    r.raise_for_status()
    return r.json()
