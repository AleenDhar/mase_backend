"""sweep_queue.py — durable queue access for the crash-safe deal-engine sweep.

The sweep used to run as an asyncio batch inside the web process with progress in
an in-memory dict (lost on restart, and it starved web requests). This module is
the data-access layer for the `sweep_queue` table that replaces that: the web
process ENQUEUES book opps as `waiting` rows, a separate worker.py process drains
them (`waiting -> working -> done|failed`), and a restart resumes from the table.

Functional, dependency-light layer over Supabase's REST API using the
service-role key, mirroring deal_engine_store.py. The table name is a MODULE
CONSTANT, never taken from the caller. There is no generic "run arbitrary SQL"
path here. All functions are synchronous (httpx); async callers wrap them in a
thread.

Atomicity: the claim is the one place a naive select-then-update would race two
workers onto the same opp, so it goes through the `claim_one_sweep()` Postgres
function (FOR UPDATE SKIP LOCKED) exposed at POST /rpc/claim_one_sweep. The
single-opp trigger enqueue uses `enqueue_one_sweep()` (insert / re-arm only a
finished row) for the same reason.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import quote

import httpx

# Single app-wide rule for the canonical (15-char) opportunity id. Importing it
# (rather than re-implementing `[:15]`) keeps ONE definition so the queue can never
# drift from deal_records and reintroduce 15- vs 18-char duplicate rows.
from deal_engine_store import canonical_opp_id

_SUPABASE_URL = (os.environ.get("SUPABASE_URL", "") or "").rstrip("/")
_SERVICE_KEY = (
    os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    or os.environ.get("SUPABASE_SERVICE_KEY", "")
)
# 2026-07-09: an abandoned Replit deployment of this same codebase kept polling
# THIS SAME shared sweep_queue with its old (pre-Omnivision, pre-datalake) code —
# it has no way to know this secret, so claim_one() below silently stops handing
# it any work once the old claim_one_sweep() is neutered (see migration note in
# CHANGELOG.md 2026-07-09). Missing here is a loud, one-time log, not a crash —
# the worker should idle honestly, not pretend nothing's wrong.
_SWEEP_QUEUE_SECRET = os.environ.get("SWEEP_QUEUE_SECRET", "")
if not _SWEEP_QUEUE_SECRET:
    print("[sweep_queue] WARNING: SWEEP_QUEUE_SECRET is not set — claim_one() will "
          "never claim a row once the legacy claim_one_sweep() RPC is retired.",
          flush=True)

T_QUEUE = "sweep_queue"

_TIMEOUT = 30.0

# Worst-case time a single analyze_one can legitimately take (the worker reads
# the SAME env var for its per-opp timeout, so the two stay tied together).
_ANALYZE_TIMEOUT_S = int(os.environ.get("DEAL_SWEEP_TIMEOUT_S", "900"))

# A claimed row whose worker died mid-run is reclaimed after this many seconds
# (status flipped working -> waiting so another worker picks it up). It MUST sit
# safely ABOVE the worst-case analyze_one time, or a legitimately long-running
# analysis would be force-reclaimed mid-flight and processed twice. So the
# default is the analysis timeout + a 50% buffer (min 5 min). Tie-to-timeout
# (rather than a bare constant) prevents config drift if DEAL_SWEEP_TIMEOUT_S is
# raised; override explicitly with DEAL_SWEEP_STALE_CLAIM_S only if you must.
STALE_CLAIM_S = int(os.environ.get(
    "DEAL_SWEEP_STALE_CLAIM_S",
    # First-pass analyze_one AND the quality inspector's recovery re-synthesis can
    # each run up to the per-opp timeout, so a single LEGIT run can take ~2x the
    # timeout. Stale-reclaim must sit safely above that, or a long-but-healthy run
    # gets force-reclaimed mid-flight and processed twice (a duplicate sweep).
    str(_ANALYZE_TIMEOUT_S * 2 + max(600, _ANALYZE_TIMEOUT_S // 2)),
))


class SweepQueueError(Exception):
    """Raised on configuration or REST failures (carries a readable message)."""


# ---------- low-level REST (mirrors deal_engine_store) ----------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check() -> None:
    if not _SUPABASE_URL or not _SERVICE_KEY:
        raise SweepQueueError(
            "Supabase is not configured (SUPABASE_URL / service key missing).")


def _headers(prefer: str = "") -> dict:
    h = {
        "apikey": _SERVICE_KEY,
        "Authorization": f"Bearer {_SERVICE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        h["Prefer"] = prefer
    return h


def _url(path: str) -> str:
    return f"{_SUPABASE_URL}/rest/v1/{path}"


def _raise_for(resp: httpx.Response, what: str):
    if resp.status_code >= 400:
        raise SweepQueueError(f"{what}: HTTP {resp.status_code} {resp.text[:400]}")


def _select(*, select: str = "*", filters: Optional[list[str]] = None,
            order: Optional[str] = None, limit: Optional[int] = None) -> list:
    _check()
    params = [f"select={select}"]
    if filters:
        params.extend(filters)
    if order:
        params.append(f"order={order}")
    if limit:
        params.append(f"limit={int(limit)}")
    resp = httpx.get(f"{_url(T_QUEUE)}?{'&'.join(params)}",
                     headers=_headers(), timeout=_TIMEOUT)
    _raise_for(resp, f"select from {T_QUEUE}")
    return resp.json()


def _upsert(rows, *, returning: bool = False):
    _check()
    prefer = ("return=representation," if returning else "return=minimal,") + \
        "resolution=merge-duplicates"
    resp = httpx.post(
        f"{_url(T_QUEUE)}?on_conflict=opp_id",
        headers=_headers(prefer),
        json=rows,
        timeout=_TIMEOUT,
    )
    _raise_for(resp, f"upsert into {T_QUEUE}")
    return resp.json() if returning else None


def _patch(patch: dict, *, filters: list[str], returning: bool = False):
    _check()
    prefer = "return=representation" if returning else "return=minimal"
    qs = "&".join(filters)
    resp = httpx.patch(
        f"{_url(T_QUEUE)}?{qs}",
        headers=_headers(prefer),
        json=patch,
        timeout=_TIMEOUT,
    )
    _raise_for(resp, f"patch {T_QUEUE}")
    return resp.json() if returning else None


def _rpc(fn: str, args: dict) -> list:
    _check()
    resp = httpx.post(_url(f"rpc/{fn}"), headers=_headers(), json=args,
                      timeout=_TIMEOUT)
    _raise_for(resp, f"rpc {fn}")
    body = resp.json()
    if body is None:
        return []
    return body if isinstance(body, list) else [body]


# ---------- enqueue ----------

def enqueue_book(run_id: str, opps: list[dict]) -> int:
    """Upsert one `waiting` row per book opp under a fresh run_id, RESETTING any
    existing row (a deliberate full sweep re-runs the whole book). `opps` are the
    discovery dicts ({id, account, owner_name, name}). Returns rows enqueued."""
    if not opps:
        return 0
    now = _now()
    rows = []
    seen = set()
    for o in opps:
        # Canonicalize to the 15-char Salesforce id (the single app-wide rule) so a
        # 15-char (book/report) and an 18-char (trigger/CDC) enqueue of the SAME opp
        # collapse onto ONE queue row. Without this they keyed as two rows and two
        # workers swept the same deal at once (double cost + API load).
        oid = canonical_opp_id(o.get("id"))
        if not oid or oid in seen:
            continue
        seen.add(oid)
        rows.append({
            "opp_id": oid,
            "opp_id_15": oid,
            "run_id": run_id,
            "status": "waiting",
            "attempts": 0,
            "account_name": o.get("account"),
            "owner_name": o.get("owner_name"),
            "opp_name": o.get("name"),
            "duration_ms": None,
            "error": None,
            "claimed_at": None,
            "created_at": now,
            "updated_at": now,
        })
    if rows:
        _upsert(rows, returning=False)
    return len(rows)


def enqueue_one(run_id: str, opp: dict) -> str:
    """Idempotent single-opp enqueue (the Salesforce-update trigger path) via the
    enqueue_one_sweep() RPC. Returns "accepted" (newly queued / re-armed a
    finished row) or "already_queued" (a waiting/working row was left untouched)."""
    # Canonicalize to the 15-char Salesforce id (the single app-wide rule) so 15-
    # and 18-char enqueues of the same opp map to ONE row and the deal can never be
    # swept twice concurrently.
    oid = canonical_opp_id(opp.get("id"))
    if not oid:
        return "error"
    out = _rpc("enqueue_one_sweep", {
        "p_opp_id": oid,
        "p_run_id": run_id,
        "p_account": opp.get("account"),
        "p_owner": opp.get("owner_name"),
        "p_name": opp.get("name"),
    })
    return "accepted" if out else "already_queued"


# ---------- claim + complete (worker side) ----------

def claim_one() -> Optional[dict]:
    """Atomically claim the next `waiting` row (FOR UPDATE SKIP LOCKED via RPC).
    Returns the claimed row (now `working`, attempts bumped) or None when drained.

    2026-07-09: calls the SECRET-GATED `claim_one_sweep_v2(p_secret)` (not the
    original zero-arg `claim_one_sweep()`) — a caller without SWEEP_QUEUE_SECRET
    (the abandoned Replit deployment; any future stray copy of this code) gets an
    empty result forever, identical to "queue drained", never an error. Only this
    ECS worker (env-injected via Secrets Manager, see .github/deploy/render_taskdef.py)
    knows the secret. The original function is left in place, neutered separately
    (see CHANGELOG.md 2026-07-09) — this call never touches it."""
    out = _rpc("claim_one_sweep_v2", {"p_secret": _SWEEP_QUEUE_SECRET})
    return out[0] if out else None


def mark_done(opp_id: str, *, duration_ms: Optional[int] = None) -> None:
    _patch({"status": "done", "error": None, "duration_ms": duration_ms,
            "claimed_at": None, "updated_at": _now()},
           filters=[f"opp_id=eq.{opp_id}"])


def mark_failed(opp_id: str, *, error: Optional[str] = None,
                duration_ms: Optional[int] = None) -> None:
    _patch({"status": "failed", "error": (error or "")[:500],
            "duration_ms": duration_ms, "claimed_at": None, "updated_at": _now()},
           filters=[f"opp_id=eq.{opp_id}"])


def retry(opp_id: str, *, error: Optional[str] = None) -> None:
    """Put a row back to `waiting` for another attempt. attempts is NOT reset (the
    next claim bumps it again), so reclaim/retry loops are naturally bounded."""
    _patch({"status": "waiting", "error": (error or "")[:500],
            "claimed_at": None, "updated_at": _now()},
           filters=[f"opp_id=eq.{opp_id}"])


def get_run_id(opp_id: str) -> Optional[str]:
    """Authoritative run_id for an opp's current queue row.

    The worker derives the run SOURCE label (salesforce_trigger / manual /
    update_living_memory / worker) from the run_id PREFIX. A claimed row can reach
    the worker without its run_id populated, which made a Salesforce/manual trigger
    get logged under the generic "worker" source (2026-07-05: 100% of recent
    "worker" runs were actually sftrig- rows). Re-reading the run_id straight from
    the row by opp_id removes that ambiguity. Returns None if no row / no run_id.
    """
    oid = (opp_id or "").strip()
    if not oid:
        return None
    rows = _select(select="run_id",
                   filters=[f"opp_id=eq.{quote(oid, safe='')}"], limit=1)
    return (rows[0].get("run_id") if rows else None)


# ---------- recovery ----------

def reclaim_stragglers() -> int:
    """Worker-startup recovery: flip any `working` row back to `waiting` (its
    worker died mid-run). Returns the count reclaimed."""
    rows = _patch({"status": "waiting", "claimed_at": None, "updated_at": _now()},
                  filters=["status=eq.working"], returning=True)
    return len(rows or [])


def reclaim_stale(max_age_s: int = STALE_CLAIM_S) -> int:
    """Periodic recovery: reclaim `working` rows whose claim is older than
    max_age_s (a worker hung or vanished without the startup reclaim). Returns
    the count reclaimed."""
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max_age_s)).isoformat()
    # PostgREST decodes a literal '+' in a query value as a space, which corrupts
    # the ISO timestamp's '+00:00' offset into ' 00:00' (HTTP 400). Percent-encode
    # the value so the offset survives the round-trip.
    cutoff_q = quote(cutoff, safe="")
    rows = _patch({"status": "waiting", "claimed_at": None, "updated_at": _now()},
                  filters=["status=eq.working", f"claimed_at=lt.{cutoff_q}"],
                  returning=True)
    return len(rows or [])


# ---------- status read (web side) ----------

def status() -> dict:
    """Queue snapshot for GET /api/deal-engine/sweep/status. Reads every row (the
    book is ~hundreds of rows, tiny) and aggregates in Python.

    Returns a payload compatible with the existing dashboard JS: top-level
    status/run_id/total/done/failed/in_progress(+waiting/working) plus an `opps`
    array (queue statuses mapped to the dashboard's vocabulary) and up to 25
    recent_failed rows.
    """
    rows = _select(
        select=("opp_id,opp_id_15,run_id,status,attempts,account_name,"
                "owner_name,opp_name,duration_ms,error,claimed_at,"
                "created_at,updated_at"),
        order="created_at.asc",
    )
    counts = {"waiting": 0, "working": 0, "done": 0, "failed": 0}
    for r in rows:
        st = r.get("status") or "waiting"
        if st in counts:
            counts[st] += 1
    total = len(rows)
    waiting, working = counts["waiting"], counts["working"]
    done, failed = counts["done"], counts["failed"]

    if total == 0:
        run_status = "idle"
    elif (waiting + working) > 0:
        run_status = "running"
    elif failed == 0:
        run_status = "succeeded"
    elif done == 0:
        run_status = "failed"
    else:
        run_status = "partial"

    # Dominant run_id = the batch with the most rows (the last full enqueue).
    run_tally: dict[str, int] = {}
    for r in rows:
        rid = r.get("run_id")
        if rid:
            run_tally[rid] = run_tally.get(rid, 0) + 1
    run_id = max(run_tally, key=run_tally.get) if run_tally else None

    created = [r.get("created_at") for r in rows if r.get("created_at")]
    updated = [r.get("updated_at") for r in rows if r.get("updated_at")]
    started_at = min(created) if created else None
    finished_at = (max(updated) if updated else None) if run_status not in (
        "running", "idle") else None

    _MAP = {"waiting": "queued", "working": "running",
            "done": "completed", "failed": "failed"}
    opps = [{
        "opp_id": r.get("opp_id"),
        "account": r.get("account_name"),
        "owner_name": r.get("owner_name"),
        "name": r.get("opp_name"),
        "status": _MAP.get(r.get("status") or "waiting", "queued"),
        "attempts": r.get("attempts") or 0,
        "duration_ms": r.get("duration_ms") or 0,
        "error": r.get("error"),
    } for r in rows]

    recent_failed = [{
        "opp_id": r.get("opp_id"), "account": r.get("account_name"),
        "owner_name": r.get("owner_name"), "error": r.get("error"),
        "attempts": r.get("attempts") or 0,
    } for r in sorted(
        [r for r in rows if r.get("status") == "failed"],
        key=lambda r: r.get("updated_at") or "", reverse=True)][:25]

    return {
        "mode": "queue",
        "status": run_status,
        "run_id": run_id,
        "total": total,
        "done": done,
        "failed": failed,
        "in_progress": working,
        "waiting": waiting,
        "working": working,
        "started_at": started_at,
        "finished_at": finished_at,
        "concurrency": int(os.getenv("DEAL_SWEEP_CONCURRENCY", "2")),
        "opps": opps,
        "recent_failed": recent_failed,
    }


def active_depth() -> int:
    """Lightweight count of rows still to process (waiting + working). Used by the
    worker autoscaler to size the fleet — reads only opp_id, not the whole row."""
    try:
        rows = _select(select="opp_id", filters=["status=in.(waiting,working)"])
        return len(rows)
    except Exception:  # noqa: BLE001 — autoscaler must never crash on a read blip
        return 0


def failed_opp_ids(limit: int = 5000) -> list[str]:
    """opp_ids of rows currently in `failed` — for a 'rerun all failed' action."""
    rows = _select(select="opp_id", filters=["status=eq.failed"], limit=limit)
    return [r.get("opp_id") for r in rows if r.get("opp_id")]
