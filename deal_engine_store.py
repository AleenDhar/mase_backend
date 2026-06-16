"""deal_engine_store.py — data-access + deterministic derivations for the Deal
Intelligence Engine (Deals / Espresso / Matcha / Team).

Functional, dependency-light layer over Supabase's REST API using the
service-role key, mirroring analysis_store.py. The table name is a MODULE
CONSTANT, never taken from the caller. There is no generic "run arbitrary SQL"
path here.

The four tabs are computed DETERMINISTICALLY from the stored evidence-anchored
records (no AI at view time):
  - Deals    -> list_records / get_record (the raw book)
  - Espresso -> derive_todo  (RSD-filterable action list grouped by impact)
  - Matcha   -> derive_matcha (per-RSD coverage vs $4M, byStage, NAA, stalled)
  - Team     -> get_team (RSD hierarchy)

All functions are synchronous (httpx). Async callers wrap them in a thread.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import quote

import httpx

_SUPABASE_URL = (os.environ.get("SUPABASE_URL", "") or "").rstrip("/")
_SERVICE_KEY = (
    os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    or os.environ.get("SUPABASE_SERVICE_KEY", "")
)

# Hard-scoped table names — constants, never supplied by the caller.
T_RECORDS = "deal_records"
T_PUSHES = "deal_todo_pushes"

_TIMEOUT = 30.0

# Pipeline coverage target per RSD (overridable via env).
COVERAGE_TARGET = float(os.environ.get("DEAL_ENGINE_COVERAGE_TARGET", "4000000"))

# Days with no activity that flags a deal as stalled-at-Qualified.
STALLED_DAYS = int(os.environ.get("DEAL_ENGINE_STALLED_DAYS", "30"))

# Sales org hierarchy (single source of truth). Each node is
# {"name", "title", optional "region", optional "note", optional "children"}.
# Overridable via env DEAL_ENGINE_TEAM as JSON holding the same nested shape
# under a top-level "org" key, or the legacy {"vp":..,"rsds":[..]} shape.
_DEFAULT_ORG = {
    "name": "Shekhar Varma",
    "title": "President, Zycus",
    "children": [
        {
            "name": "Anthony Gray", "title": "VP", "region": "EU/UK",
            "children": [
                {"name": "Claire Hudson", "title": "Regional Sales Director, EU Sales"},
                {"name": "Casper Hoeholt", "title": "Regional Sales Director", "region": "Nordics"},
            ],
        },
        {
            "name": "John Woodcock", "title": "VP", "region": "EMEA / Continental",
            "children": [
                {"name": "Caroline Lacocque", "title": "RSD"},
                {"name": "Dirk Fischbach", "title": "RSD"},
                {"name": "Pierre Meraud", "title": "RSD"},
            ],
        },
        {
            "name": "Carl Kimball", "title": "VP", "region": "APAC / MEA",
            "children": [
                {
                    "name": "Mohamad Alhakim", "title": "Regional Vice President", "region": "UAE",
                    "children": [
                        {"name": "Dan Quinn", "title": "RSD"},
                    ],
                },
                {"name": "Adam Hasan", "title": "RSD", "region": "Australia"},
                {"name": "George John", "title": "RSD", "region": "Malaysia"},
                {"name": "Guillaume Pasquet", "title": "RSD"},
                {"name": "Luke Dougherty", "title": "RSD"},
                {"name": "Tanmay Srivastava", "title": "RSD", "region": "Bangalore"},
            ],
        },
        {
            "name": "Alexa Bradley", "title": "VP",
            "children": [
                {"name": "Karson Keogh", "title": "RSD"},
                {"name": "Mario Castro", "title": "Regional Director, Enterprise Sales"},
                {"name": "Rick Taranek", "title": "RSD"},
            ],
        },
        {
            "name": "VP East", "title": "VP", "note": "open position, managed by Alexa Bradley",
            "children": [
                {"name": "Edward Dlugosz", "title": "RSD"},
                {"name": "Marc Quessenberry", "title": "RSD"},
                {"name": "Richard Hunsinger", "title": "RSD"},
                {"name": "Mike Flowers", "title": "RSD"},
            ],
        },
        {
            "name": "Arthur Raguette", "title": "VP, US Strategic Accounts",
            "note": "solo - owns deals himself",
        },
        {
            "name": "Michael McCarthy", "title": "VP, US Mid-Markets",
            "children": [
                {"name": "Bailey Erazo", "title": "Account Executive"},
                {"name": "Grace Kim", "title": "Account Executive"},
                {"name": "Justin Ajmo", "title": "Account Executive"},
                {"name": "Steve Ovadje", "title": "Account Executive"},
            ],
        },
    ],
}


class DealEngineError(Exception):
    """Raised on configuration or REST failures (carries a readable message)."""


# ---------- low-level REST (mirrors analysis_store) ----------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check() -> None:
    if not _SUPABASE_URL or not _SERVICE_KEY:
        raise DealEngineError("Supabase is not configured (SUPABASE_URL / service key missing).")


def _headers(prefer: str = "") -> dict:
    h = {
        "apikey": _SERVICE_KEY,
        "Authorization": f"Bearer {_SERVICE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        h["Prefer"] = prefer
    return h


def _url(table: str) -> str:
    return f"{_SUPABASE_URL}/rest/v1/{table}"


def _raise_for(resp: httpx.Response, what: str):
    if resp.status_code >= 400:
        raise DealEngineError(f"{what}: HTTP {resp.status_code} {resp.text[:400]}")


def _select(table: str, *, select: str = "*", filters: Optional[list[str]] = None,
            order: Optional[str] = None, limit: Optional[int] = None) -> list:
    _check()
    params = [f"select={select}"]
    if filters:
        params.extend(filters)
    if order:
        params.append(f"order={order}")
    if limit:
        params.append(f"limit={int(limit)}")
    resp = httpx.get(f"{_url(table)}?{'&'.join(params)}", headers=_headers(), timeout=_TIMEOUT)
    _raise_for(resp, f"select from {table}")
    return resp.json()


def _upsert(table: str, rows, on_conflict: str, *, returning: bool = True):
    _check()
    prefer = ("return=representation," if returning else "return=minimal,") + "resolution=merge-duplicates"
    resp = httpx.post(
        f"{_url(table)}?on_conflict={on_conflict}",
        headers=_headers(prefer),
        json=rows,
        timeout=_TIMEOUT,
    )
    _raise_for(resp, f"upsert into {table}")
    return resp.json() if returning else None


def _insert(table: str, rows, *, returning: bool = True):
    _check()
    prefer = "return=representation" if returning else "return=minimal"
    resp = httpx.post(
        _url(table),
        headers=_headers(prefer),
        json=rows,
        timeout=_TIMEOUT,
    )
    _raise_for(resp, f"insert into {table}")
    return resp.json() if returning else None


def _delete(table: str, filters: dict) -> None:
    _check()
    params = "&".join(f"{k}=eq.{quote(str(v), safe='')}" for k, v in filters.items())
    resp = httpx.delete(f"{_url(table)}?{params}", headers=_headers(), timeout=_TIMEOUT)
    _raise_for(resp, f"delete from {table}")


def _patch(table: str, patch: dict, *, filters: list[str], returning: bool = False):
    """PATCH rows matching `filters` (raw PostgREST predicates) with `patch`."""
    _check()
    prefer = "return=representation" if returning else "return=minimal"
    qs = "&".join(filters) if filters else ""
    resp = httpx.patch(
        f"{_url(table)}?{qs}" if qs else _url(table),
        headers=_headers(prefer),
        json=patch,
        timeout=_TIMEOUT,
    )
    _raise_for(resp, f"patch {table}")
    return resp.json() if returning else None


# ---------- small helpers ----------

def _g(d: Any, *path, default=None):
    """Safe nested get over dict/lists-of-dicts."""
    cur = d
    for p in path:
        if isinstance(cur, dict):
            cur = cur.get(p)
        else:
            return default
        if cur is None:
            return default
    return cur


def _items(ai: dict, col: str) -> list:
    v = _g(ai, col, "items", default=[])
    return v if isinstance(v, list) else []


def _to_date(v) -> Optional[date]:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v)[:10]).date()
    except (ValueError, TypeError):
        return None


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# ---------- CRUD ----------

def upsert_record(record: dict) -> dict:
    """Insert/replace one canonical deal record. `record` must carry opp_id and
    the hard/ai layers; the flat filter columns are derived from it."""
    opp_id = (record.get("opp_id") or "").strip()
    if not opp_id:
        raise DealEngineError("record.opp_id is required")
    # Canonicalize the key to the 15-char Salesforce ID. The table is PK'd on the
    # 15-char form, but discovery/enrichment/sweep inputs frequently carry the
    # 18-char form; without this, an 18-char upsert would INSERT a duplicate row
    # alongside the canonical 15-char one instead of updating it. Keep the jsonb
    # copy in lockstep so derivations and re-reads stay consistent.
    if len(opp_id) > 15:
        opp_id = opp_id[:15]
    record["opp_id"] = opp_id
    hard = record.get("hard") or {}
    row = {
        "opp_id": opp_id,
        "owner_name": hard.get("owner_name"),
        "account_name": hard.get("account_name"),
        "opp_name": hard.get("opp_name"),
        "stage": hard.get("stage"),
        "forecast_category": hard.get("forecast_category"),
        "amount": hard.get("amount"),
        "close_date": hard.get("close_date") or None,
        "qualified_date": hard.get("qualified_date") or None,
        "last_activity_date": hard.get("last_activity_date") or None,
        "forecast_critical": bool(record.get("forecast_critical")),
        "analysis_confidence": record.get("analysis_confidence"),
        "swept_at": record.get("swept_at") or None,
        "record": record,
        "updated_at": _now(),
    }
    res = _upsert(T_RECORDS, row, on_conflict="opp_id")
    return res[0] if res else row


def list_records(owner: Optional[str] = None,
                 include_inactive: bool = False) -> list[dict]:
    """Return the canonical records, optionally by owner.

    By default returns ONLY active records (in the MASE report). Deals that left
    the report are soft-deactivated (active=false) and hidden from every view;
    their record + history are retained. Pass include_inactive=True for an
    admin/full read."""
    filters = []
    if not include_inactive:
        filters.append("active=is.true")
    if owner:
        # URL-encode the value so names with spaces/specials can't break the
        # PostgREST query composition (e.g. "Claire Hudson").
        filters.append(f"owner_name=eq.{quote(owner, safe='')}")
    rows = _select(T_RECORDS, select="record", filters=filters or None,
                   order="account_name.asc")
    return [r["record"] for r in rows if r.get("record")]


def known_active_map() -> dict[str, bool]:
    """Map of every known deal's 15-char opp id -> its active flag. The diff
    basis for report reconciliation (distinguishes new vs re-entrant)."""
    rows = _select(T_RECORDS, select="opp_id,active")
    out: dict[str, bool] = {}
    for r in rows:
        oid = (r.get("opp_id") or "").strip()[:15]
        if oid:
            out[oid] = bool(r.get("active"))
    return out


def active_opp_ids15() -> set[str]:
    """The current ACTIVE book as a set of 15-char opp ids."""
    return {k for k, v in known_active_map().items() if v}


def is_active_member(opp_id: str) -> bool:
    """True iff this opp has an active record (membership = the MASE report).
    Accepts a 15- or 18-char id."""
    oid = (opp_id or "").strip()[:15]
    if not oid:
        return False
    rows = _select(T_RECORDS, select="opp_id",
                   filters=[f"opp_id=eq.{quote(oid, safe='')}", "active=is.true"],
                   limit=1)
    return bool(rows)


def set_active(opp_ids, active: bool) -> int:
    """Flip the active flag for a batch of opp ids (15-char keyed). Sets
    removed_at when deactivating, clears it when reactivating. Membership is
    changed ONLY here (reconciliation) — never by upsert_record. Returns the
    number of ids submitted (PostgREST in-filter batched)."""
    ids15 = sorted({(o or "").strip()[:15] for o in (opp_ids or []) if (o or "").strip()})
    if not ids15:
        return 0
    patch = {"active": bool(active), "updated_at": _now(),
             "removed_at": (None if active else _now())}
    # Batch the in() filter so a very large membership delta can't blow the URL.
    BATCH = 100
    for i in range(0, len(ids15), BATCH):
        chunk = ids15[i:i + BATCH]
        in_list = ",".join(quote(c, safe="") for c in chunk)
        _patch(T_RECORDS, patch, filters=[f"opp_id=in.({in_list})"])
    return len(ids15)


def get_record(opp_id: str) -> Optional[dict]:
    rows = _select(T_RECORDS, select="record",
                   filters=[f"opp_id=eq.{quote(opp_id, safe='')}"], limit=1)
    if not rows:
        # Salesforce ids come in 15- and 18-char forms (report exports vs API);
        # a back-link may carry the 18-char id while the row is keyed on 15
        # (or vice-versa). Fall back to a shared 15-char-prefix match so a link
        # always resolves to its deal.
        prefix = (opp_id or "").strip()[:15]
        if len(prefix) >= 15:
            rows = _select(T_RECORDS, select="record",
                           filters=[f"opp_id=like.{quote(prefix, safe='')}*"], limit=1)
    return rows[0]["record"] if rows else None


def delete_record(opp_id: str) -> None:
    _delete(T_RECORDS, {"opp_id": opp_id})


# ---------- one-time living-memory baseline (backfill) ----------

def _needs_packet_baseline(rec: dict) -> bool:
    """A record needs a baseline if it predates living memory (no packet store
    yet). Records already at schema_version >= 2 were written by a real sweep and
    carry a genuine change log; we leave them untouched so a re-run never wipes
    their deltas or re-seeds them."""
    if not isinstance(rec, dict):
        return False
    return int(rec.get("schema_version") or 0) < 2


def _needs_pulse_baseline(rec: dict) -> bool:
    """A record needs a pulse baseline if it was swept before the engagement
    pulse existed (no stamped `pulse` with a state). Records that already carry a
    stamped pulse are left untouched — the next real sweep re-stamps them with
    fresh evidence, so re-stamping here would only duplicate that work."""
    if not isinstance(rec, dict):
        return False
    p = rec.get("pulse")
    return not (isinstance(p, dict) and p.get("state"))


def backfill_pulse(*, dry_run: bool = False) -> dict:
    """Token-free, idempotent pass that stamps an engagement pulse onto every
    record that predates the pulse (stored `pulse: null`), so the persisted
    record and the dashboard `pulse_summary` aggregate are consistent immediately
    instead of only after each deal's natural next sweep.

    Behaviour (deliberate, documented):
      * For each record WITHOUT a stamped pulse we derive one with
        deal_engine_pulse.compute_pulse_from_hard from its EXISTING hard.* facts —
        the SAME shape the derived views (Espresso/Matcha) already compute on read
        via _pulse_of — and write it to record["pulse"].
      * Nothing else is touched: no ai re-projection, no SF read, no token spend.

    Idempotent: records already carrying a stamped pulse are skipped, so re-running
    is a no-op. Per-record errors are counted and skipped so one bad record cannot
    abort the whole pass.

    `dry_run=True` computes the same stats but writes nothing.
    """
    records = _select(T_RECORDS, select="record", order="account_name.asc")
    stats = {"total": len(records), "stamped": 0, "skipped": 0,
             "by_state": {"live": 0, "cooling": 0, "dark": 0, "unknown": 0},
             "errors": 0}
    for row in records:
        rec = row.get("record")
        if not isinstance(rec, dict) or not (rec.get("opp_id") or "").strip():
            stats["skipped"] += 1
            continue
        if not _needs_pulse_baseline(rec):
            stats["skipped"] += 1
            continue
        try:
            pulse = _pulse.compute_pulse_from_hard(rec.get("hard") or {})
            rec["pulse"] = pulse
            state = pulse.get("state") or "unknown"
            stats["by_state"][state if state in stats["by_state"] else "unknown"] += 1
            if not dry_run:
                upsert_record(rec)
            stats["stamped"] += 1
        except Exception as e:  # noqa: BLE001 — one bad record must not abort the pass
            stats["errors"] += 1
            print(f"[DEAL-PULSE-BACKFILL] stamp failed opp={rec.get('opp_id')}: "
                  f"{type(e).__name__}: {e}", flush=True)
    return stats


def backfill_packets(*, dry_run: bool = False) -> dict:
    """One-time, idempotent pass that seeds a living-memory packets baseline onto
    every deal record that predates living memory, so the "What changed" feed is
    populated across the whole book immediately instead of only after each deal's
    natural next sweep.

    Behaviour (deliberate, documented):
      * For each record WITHOUT a packet store we derive packets from its EXISTING
        ai.*/hard via deal_engine_packets.seed_packets and attach them with an
        EMPTY delta log. Seeding pre-existing facts is NOT a change, so we emit no
        `added` deltas — the migration is treated as seeding, not history.
      * Prior analysis is preserved exactly: we do NOT re-project ai. The next
        real sweep reconciles fresh evidence against this baseline and projects
        as normal from there on.
      * schema_version is stamped 2 so the record is recognised as living-memory.

    Idempotent: records already at schema_version >= 2 are skipped, so re-running
    is a no-op (never double-seeds, never re-charges). Per-record errors are
    counted and skipped so one bad record cannot abort the whole pass.

    `dry_run=True` computes the same stats but writes nothing.
    """
    records = _select(T_RECORDS, select="record", order="account_name.asc")
    stats = {"total": len(records), "seeded": 0, "skipped": 0,
             "packets_created": 0, "errors": 0}
    for row in records:
        rec = row.get("record")
        if not isinstance(rec, dict) or not (rec.get("opp_id") or "").strip():
            stats["skipped"] += 1
            continue
        if not _needs_packet_baseline(rec):
            stats["skipped"] += 1
            continue
        try:
            as_of = rec.get("swept_at") or date.today().isoformat()
            seeded = _packets.seed_packets(rec.get("ai") or {},
                                           rec.get("hard") or {}, as_of)
            rec["packets"] = seeded
            # Pre-living-memory records normally have no delta log; preserve any
            # that exists. Seeding itself adds none.
            rec.setdefault("deltas", [])
            rec["schema_version"] = 2
            stats["packets_created"] += len(seeded)
            if not dry_run:
                upsert_record(rec)
            stats["seeded"] += 1
        except Exception as e:  # noqa: BLE001 — one bad record must not abort the pass
            stats["errors"] += 1
            print(f"[DEAL-BACKFILL] seed failed opp={rec.get('opp_id')}: "
                  f"{type(e).__name__}: {e}", flush=True)
    return stats


# ---------- living-memory change feed (deltas) ----------

import deal_engine_packets as _packets  # noqa: E402
import deal_engine_pulse as _pulse  # noqa: E402

# The four rep-facing buckets, in a stable display order.
DELTA_GROUPS = ("added", "changed", "resolved", "dormant")


def _deal_ctx(rec: dict) -> dict:
    """Minimal deal context so a feed entry can link back to its deal."""
    hard = rec.get("hard") or {}
    return {
        "opp_id": rec.get("opp_id"),
        "account_name": hard.get("account_name"),
        "opp_name": hard.get("opp_name"),
        "owner_name": hard.get("owner_name"),
        "stage": hard.get("stage"),
    }


def _present(d: dict) -> dict:
    """Enrich one stored delta with a human label + rep-facing group."""
    return _packets.present_delta(d)


def get_deltas(opp_id: str) -> list[dict]:
    """The change log for one deal, newest first, each enriched with a human
    `label` + `group` (added/changed/resolved/dormant)."""
    rec = get_record(opp_id) or {}
    d = rec.get("deltas")
    if not isinstance(d, list):
        return []
    return [_present(x) for x in d if isinstance(x, dict)]


def get_deltas_view(opp_id: str) -> dict:
    """One deal's 'What changed' panel: the deal context (for the header /
    back-link) plus its labelled change log, newest first, and a per-group
    count so the UI can show tab badges."""
    rec = get_record(opp_id) or {}
    deltas = get_deltas(opp_id)
    counts = {g: 0 for g in DELTA_GROUPS}
    for d in deltas:
        g = d.get("group")
        if g in counts:
            counts[g] += 1
    return {
        "opp_id": opp_id,
        "deal": _deal_ctx(rec) if rec else {"opp_id": opp_id},
        "count": len(deltas),
        "countsByGroup": counts,
        "deltas": deltas,
    }


def list_deltas(owner: Optional[str] = None, limit: int = 200,
                group_by: Optional[str] = None) -> dict:
    """Book-wide change feed (newest first), each entry enriched with its deal
    context (for the back-link) and a human `label` + `group`, plus a per-owner
    roll-up count. Mirrors list_records' scope.

    `group_by="owner"` (or "rsd") adds a `groups` array bucketing the (capped)
    feed by deal owner, newest-first within each owner."""
    records = list_records(owner)
    feed: list[dict] = []
    by_owner: dict[str, int] = {}
    for rec in records:
        ctx = _deal_ctx(rec)
        dl = rec.get("deltas")
        if not isinstance(dl, list):
            continue
        own = ctx.get("owner_name") or "Unknown"
        for d in dl:
            if isinstance(d, dict):
                feed.append({**ctx, **_present(d)})
                by_owner[own] = by_owner.get(own, 0) + 1
    feed.sort(key=lambda d: str(d.get("date") or ""), reverse=True)
    capped = feed[: max(0, int(limit))]
    out = {
        "owner": owner or "all",
        "count": len(feed),
        "byOwner": dict(sorted(by_owner.items())),
        "deltas": capped,
    }
    if str(group_by or "").strip().lower() in ("owner", "rsd"):
        grouped: dict[str, list] = {}
        for d in capped:
            grouped.setdefault(d.get("owner_name") or "Unknown", []).append(d)
        out["groups"] = [
            {"owner": own, "count": len(items), "deltas": items}
            for own, items in sorted(grouped.items())
        ]
    return out


def count_records() -> int:
    rows = _select(T_RECORDS, select="opp_id")
    return len(rows)


# ---------- team ----------

def _org_from_legacy(team: dict) -> dict:
    """Adapt the legacy {"vp":.., "rsds":[..]} shape into an org tree."""
    return {
        "name": team["vp"], "title": "VP",
        "children": [{"name": r, "title": "RSD"} for r in team["rsds"]],
    }


def _resolve_org() -> dict:
    """Return the org tree, honouring DEAL_ENGINE_TEAM overrides if valid."""
    raw = os.environ.get("DEAL_ENGINE_TEAM", "")
    if not raw:
        return _DEFAULT_ORG
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return _DEFAULT_ORG
    if isinstance(parsed.get("org"), dict) and parsed["org"].get("name"):
        return parsed["org"]
    if parsed.get("vp") and isinstance(parsed.get("rsds"), list):
        return _org_from_legacy(parsed)
    return _DEFAULT_ORG


def _flatten_org(node: dict, manager: Optional[str], out: list) -> None:
    """Depth-first flatten into member records carrying their direct manager."""
    member = {k: node[k] for k in ("name", "title", "region", "note") if k in node}
    member["reportsTo"] = manager
    out.append(member)
    for child in node.get("children", []) or []:
        _flatten_org(child, node["name"], out)


def get_team() -> dict:
    """Full sales-org hierarchy.

    Returns the nested ``tree`` (single source of truth), a flat ``members``
    list (each with its direct ``reportsTo``), a ``reportsTo`` lookup covering
    everyone, the ``president`` at the top, and the list of ``vps``. Legacy
    ``vp``/``rsds`` keys are kept for backward compatibility (top person and
    their direct reports).
    """
    org = _resolve_org()
    members: list = []
    _flatten_org(org, None, members)
    reports_to = {m["name"]: m["reportsTo"] for m in members}
    vps = [c["name"] for c in org.get("children", []) or []]
    return {
        "president": {"name": org["name"], "title": org.get("title", "")},
        "tree": org,
        "members": members,
        "reportsTo": reports_to,
        "vps": vps,
        # Legacy shape (top person + direct reports).
        "vp": org["name"],
        "rsds": vps,
    }


# ---------- Espresso (to-do) push ledger ----------

# Which display field is the "primary text" of a to-do in each category. This
# (with opp_id + category + primary date) makes the deterministic todo_key.
_TODO_PRIMARY_TEXT = {
    "critical": "action",
    "important": "commitment",
    "explicitRequirements": "requirement",
    "implicit": "inferred_need",
    "bestPractice": "flag",
}


def todo_key(opp_id: Any, category: Any, text: Any, date_str: Any = None) -> str:
    """Deterministic fingerprint for one derived to-do.

    To-dos are derived on the fly and have no stored id, so we hash a stable
    tuple — opp_id (15-char prefix so the 15- vs 18-char Salesforce id forms
    collapse to the same key) + category + primary text + primary date. The same
    logical to-do produces the same key across reloads and sweeps, which is what
    the frontend echoes back and what makes the Salesforce push idempotent."""
    raw = "|".join([
        (str(opp_id or "").strip())[:15],
        (str(category or "").strip()),
        (str(text or "").strip()),
        (str(date_str or "").strip()),
    ])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def get_push(key: str) -> Optional[dict]:
    """Return the push record for one todo_key, or None if not yet pushed."""
    rows = _select(T_PUSHES, select="*",
                   filters=[f"todo_key=eq.{quote(str(key), safe='')}"], limit=1)
    return rows[0] if rows else None


def list_pushes(opp_id: str) -> list[dict]:
    """All push records for one opportunity (15-char-prefix match so either id
    form resolves)."""
    prefix = (str(opp_id or "").strip())[:15]
    if not prefix:
        return []
    return _select(T_PUSHES, select="*",
                   filters=[f"opp_id=like.{quote(prefix, safe='')}*"],
                   order="pushed_at.desc")


def _pushes_index() -> dict[str, str]:
    """Map of todo_key -> sf_task_id across the whole ledger, used to annotate
    the to-do view with pushed state. Degrades to an empty map if the ledger
    table isn't present yet (so /todo keeps working pre-migration)."""
    try:
        rows = _select(T_PUSHES, select="todo_key,sf_task_id")
    except DealEngineError:
        return {}
    return {r["todo_key"]: r.get("sf_task_id") for r in rows if r.get("todo_key")}


def insert_push(*, todo_key: str, opp_id: str, category: Optional[str],
                subject: Optional[str], sf_task_id: Optional[str],
                pushed_by: Optional[str] = None,
                payload: Optional[dict] = None) -> dict:
    """Persist a successful push (idempotency ledger). Caller must only insert
    after the Salesforce write succeeds, and only when no push exists yet for
    this todo_key."""
    row = {
        "todo_key": todo_key,
        "opp_id": opp_id,
        "category": category,
        "subject": subject,
        "sf_task_id": sf_task_id,
        "pushed_by": pushed_by,
        "payload": payload or {},
        "pushed_at": _now(),
    }
    res = _insert(T_PUSHES, row)
    return res[0] if res else row


# ---------- Espresso (to-do) derivation ----------

# Daily to-do actionability horizon. The dashboard is rebuilt every day, so a
# to-do is only "act on it now" if it is overdue, undated, or due within this many
# days. Far-future items (a buyer's "issue RFP in Q3") are held back and resurface
# as they come into range. The agent decides separately whether a near-term soft
# nudge is worth recommending (that flows in as a recommended_move).
TODO_HORIZON_DAYS = int(os.environ.get("DEAL_TODO_HORIZON_DAYS", "60"))
# Cap on best-practice flags surfaced per deal so the urgent few aren't buried.
TODO_MAX_BEST_PRACTICE = int(os.environ.get("DEAL_TODO_MAX_BEST_PRACTICE", "5"))
# A dated ask/commitment whose evidence is older than this (and not re-confirmed
# on a recent call/activity) is treated as context/history, not a live to-do, so
# the action surface stays forward-looking instead of resurfacing year-old asks.
# Such items still appear in the deal analysis view; they only drop off Espresso.
TODO_RECENCY_DAYS = int(os.environ.get("DEAL_TODO_RECENCY_DAYS", "90"))


def _parse_iso_date(s: Any) -> Optional[date]:
    if not s or not isinstance(s, str):
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s.strip())
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _within_todo_horizon(date_str: Any, today: Optional[date] = None) -> bool:
    """A forward-looking to-do is actionable now when it has no parseable date, is
    overdue / due today, or falls within TODO_HORIZON_DAYS. Far-future is deferred."""
    d = _parse_iso_date(date_str)
    if d is None:
        return True
    today = today or date.today()
    return d <= today + timedelta(days=TODO_HORIZON_DAYS)


def _within_recency(date_str: Any, today: Optional[date] = None) -> bool:
    """True when a dated ask/commitment is fresh enough to be a live to-do: it has
    no parseable evidence date (we cannot prove it is stale) or its evidence is
    within TODO_RECENCY_DAYS. Older, unconfirmed items become context, not actions
    (e.g. a 2024 NDA request must not resurface as today's to-do)."""
    d = _parse_iso_date(date_str)
    if d is None:
        return True
    today = today or date.today()
    return d >= today - timedelta(days=TODO_RECENCY_DAYS)


def _urgency(date_str: Any, today: Optional[date] = None) -> str:
    """Urgency tier for a target/act-by/due date, for the UI to flash:
    overdue (past), next_14_days, next_30_days, later, or undated."""
    d = _parse_iso_date(date_str)
    if d is None:
        return "undated"
    today = today or date.today()
    days = (d - today).days
    if days < 0:
        return "overdue"
    if days <= 14:
        return "next_14_days"
    if days <= 30:
        return "next_30_days"
    return "later"


def _stamp_todo(item: dict, category: str, text: Any, date_str: Any,
                pmap: dict) -> dict:
    """Annotate one derived to-do in place with its deterministic todo_key and
    its current pushed-to-Salesforce state (pushed / sf_task_id)."""
    k = todo_key(item.get("opp_id"), category, text, date_str)
    sf = pmap.get(k)
    item["todo_key"] = k
    item["pushed"] = bool(sf)
    item["sf_task_id"] = sf
    return item


def _pulse_of(rec: dict) -> dict:
    """The single authoritative engagement pulse for a record. Prefer the pulse
    stamped at sweep time; for a record swept before the pulse existed, derive an
    equivalent one from its stored hard.* facts so the derived views always read
    the SAME pulse shape."""
    p = rec.get("pulse")
    if isinstance(p, dict) and p.get("state"):
        return p
    return _pulse.compute_pulse_from_hard(rec.get("hard") or {})


def attach_pulse(rec: dict) -> dict:
    """Return a read-only copy of `rec` with `pulse` guaranteed for the API.

    The frontend contract promises `record["pulse"]` on every opportunities
    response, but records swept before the pulse existed have no stamped pulse.
    This fills it in (deriving from the stored hard.* facts via `_pulse_of`)
    WITHOUT mutating the write path — no upsert caller routes through here, so a
    read-time-derived pulse can never be persisted over a freshly-swept one."""
    if not isinstance(rec, dict):
        return rec
    p = rec.get("pulse")
    if isinstance(p, dict) and p.get("state"):
        return rec
    out = dict(rec)
    out["pulse"] = _pulse_of(rec)
    return out


def derive_todo(owner: Optional[str] = None) -> dict:
    """RSD-filterable action list grouped by impact, computed from the records.

    Every item across all five categories carries a deterministic `todo_key`
    plus its current `pushed` / `sf_task_id` state (joined from the
    deal_todo_pushes ledger) so the UI can show which to-dos were already pushed
    to Salesforce, surviving reloads."""
    records = list_records(owner)
    pmap = _pushes_index()
    critical, important, explicit, implicit, best_practice = [], [], [], [], []

    for rec in records:
        hard = rec.get("hard") or {}
        ai = rec.get("ai") or {}
        pulse = _pulse_of(rec)
        ctx = {
            "opp_id": rec.get("opp_id"),
            "account_name": hard.get("account_name"),
            "opp_name": hard.get("opp_name"),
            "owner_name": hard.get("owner_name"),
        }

        # Critical: the single rank-1 recommended move on each key deal
        # (forecast-critical or north-star critical flag).
        is_key = bool(rec.get("forecast_critical")) or bool(_g(ai, "north_star_verdict", "critical"))
        moves = _items(ai, "recommended_moves")
        if is_key and moves:
            # Prefer the highest-ranked move that is actionable now. A key deal whose
            # only moves are far-future legitimately has no near-term critical action,
            # so it drops off today's list and resurfaces as a move comes into range.
            # act_by is the near-term date by which to act (set by the sweep agent).
            # trigger_date is the evidence date (often past), so fall back to it only
            # when a record predates act_by. Horizon + urgency key off act_by first.
            def _act_date(m):
                return m.get("act_by") or m.get("trigger_date")
            actionable = [m for m in moves if _within_todo_horizon(_act_date(m))]
            if actionable:
                top = min(actionable, key=lambda m: _num(m.get("rank")) if m.get("rank") is not None else 1)
                critical.append(_stamp_todo({**ctx,
                                 "action": top.get("action"),
                                 "intervention_owner": top.get("owner"),
                                 "trigger": top.get("trigger"),
                                 "trigger_date": top.get("trigger_date"),
                                 "act_by": top.get("act_by"),
                                 "urgency": _urgency(_act_date(top)),
                                 "expected_effect": top.get("expected_effect")},
                                 "critical", top.get("action"), _act_date(top), pmap))

        # Important: our open/overdue commitments (promised, not delivered).
        for d in _items(ai, "open_deliverables"):
            status = (d.get("status") or "").lower()
            # Defer commitments due more than ~2 months out; they resurface daily.
            if status in ("open", "overdue") and _within_todo_horizon(d.get("due")) \
                    and _within_recency(d.get("date")):
                important.append(_stamp_todo({**ctx,
                                  "who": d.get("who"),
                                  "commitment": d.get("commitment"),
                                  "due": d.get("due"),
                                  "urgency": _urgency(d.get("due")),
                                  "status": status},
                                  "important", d.get("commitment"), d.get("due"), pmap))

        # Explicit requirements still open.
        for r in _items(ai, "explicit_requirements"):
            if not r.get("addressed") and _within_recency(r.get("date")):
                explicit.append(_stamp_todo({**ctx,
                                 "requirement": r.get("requirement"),
                                 "said_by": r.get("said_by"),
                                 "date": r.get("date")},
                                 "explicitRequirements", r.get("requirement"),
                                 r.get("date"), pmap))

        # Implicit needs (inferred from call language).
        for r in _items(ai, "implicit_requirements"):
            if not _within_recency(r.get("date")):
                continue
            implicit.append(_stamp_todo({**ctx,
                             "inferred_need": r.get("inferred_need"),
                             "grounding_quote": r.get("grounding_quote"),
                             "date": r.get("date")},
                             "implicit", r.get("inferred_need"), r.get("date"), pmap))

        # Best-practice prompts (single-thread, MEDDPICC gaps, ghost risk). The agent
        # emits these ordered most-important-first; cap per deal so the urgent few
        # aren't buried under an exhaustive audit.
        flags = _g(ai, "best_practice_check", "flags", default=[])
        flist = flags if isinstance(flags, list) else []
        for f in flist[:TODO_MAX_BEST_PRACTICE]:
            flag_text = f if isinstance(f, str) else f.get("flag") or f
            # Read-time safety net (covers records swept before pulse-reconcile, or
            # an exception path that skipped it): drop stale-worldview flags
            # (ghost / dark-for-months / future-date / wrong-stage) that contradict
            # a live pulse, so the Espresso surface never nags a live deal.
            if _pulse.flag_contradicts_live_pulse(flag_text, pulse):
                continue
            best_practice.append(_stamp_todo({**ctx, "flag": flag_text},
                                 "bestPractice", flag_text, None, pmap))

    return {
        "owner": owner or "all",
        "critical": critical,
        "important": important,
        "explicitRequirements": explicit,
        "implicit": implicit,
        "bestPractice": best_practice,
    }


# ---------- Matcha (pipeline health) derivation ----------

def derive_matcha(owner: Optional[str] = None) -> dict:
    """Per-RSD coverage vs target, byStage, NAA by month, stalled-at-Qualified."""
    records = list_records(owner)
    today = date.today()

    # Coverage vs target, grouped by owner (so the "all" view shows each RSD).
    coverage_by_owner: dict[str, float] = {}
    by_stage: dict[str, dict] = {}
    naa_by_month: dict[str, int] = {}
    stalled: list[dict] = []

    for rec in records:
        hard = rec.get("hard") or {}
        own = hard.get("owner_name") or "Unknown"
        amount = _num(hard.get("amount"))
        stage = hard.get("stage") or "Unknown"

        # Coverage measures OPEN pipeline vs target, so closed deals (won/lost)
        # never inflate it. byStage/NAA below still report every stage.
        if not (stage or "").strip().lower().startswith("closed"):
            coverage_by_owner[own] = coverage_by_owner.get(own, 0.0) + amount

        st = by_stage.setdefault(stage, {"count": 0, "amount": 0.0})
        st["count"] += 1
        st["amount"] += amount

        qd = _to_date(hard.get("qualified_date"))
        if qd:
            key = f"{qd.year:04d}-{qd.month:02d}"
            naa_by_month[key] = naa_by_month.get(key, 0) + 1

        if (stage or "").strip().lower() == "qualified":
            # Read idle-days from the one authoritative pulse so the stalled rollup
            # cannot disagree with the verdict's engagement read. days_since_activity
            # is the same verified-activity recency the pulse anchors on.
            pulse = _pulse_of(rec)
            days_idle = pulse.get("days_since_activity")
            if days_idle is None or days_idle >= STALLED_DAYS:
                stalled.append({
                    "opp_id": rec.get("opp_id"),
                    "account_name": hard.get("account_name"),
                    "opp_name": hard.get("opp_name"),
                    "owner_name": own,
                    "amount": amount,
                    "last_activity_date": hard.get("last_activity_date"),
                    "days_since_activity": days_idle,
                })

    coverage = [
        {"owner": own, "open_amount": round(total, 2), "target": COVERAGE_TARGET,
         "status": "adequate" if total >= COVERAGE_TARGET else "inadequate"}
        for own, total in sorted(coverage_by_owner.items())
    ]

    return {
        "owner": owner or "all",
        "target": COVERAGE_TARGET,
        "coverage": coverage,
        "byStage": {k: {"count": v["count"], "amount": round(v["amount"], 2)}
                    for k, v in sorted(by_stage.items())},
        "naaByMonth": dict(sorted(naa_by_month.items())),
        "stalledAtQualified": stalled,
    }


# ---------- chat context ----------

def _records_for_scope(
    owner: Optional[str] = None,
    owners: Optional[list[str]] = None,
    opp_ids: Optional[list[str]] = None,
) -> list[dict]:
    """Resolve the chat scope with precedence: opp_ids > owners > owner > book.

    - opp_ids: exact opportunities. Salesforce ids are 15- OR 18-char; we match
      on the shared 15-char prefix so either form works. Unknown ids are ignored.
    - owners: any deal owned by one of these reps (exact owner_name match).
    - owner: the legacy single-owner filter (PostgREST-side).
    - none of the above: the whole book.
    The returned set is itself the entire model dataset for the chat (we never
    append the full book on top of a scoped selection)."""
    if opp_ids:
        wanted = {str(i).strip()[:15] for i in opp_ids if str(i).strip()}
        if wanted:
            return [r for r in list_records()
                    if (r.get("opp_id") or "")[:15] in wanted]
    if owners:
        names = {o.strip() for o in owners if isinstance(o, str) and o.strip()}
        if names:
            return [r for r in list_records()
                    if (r.get("hard") or {}).get("owner_name") in names]
    return list_records(owner)


def chat_book_context(
    owner: Optional[str] = None,
    owners: Optional[list[str]] = None,
    opp_ids: Optional[list[str]] = None,
    max_records: int = 60,
) -> list[dict]:
    """Compact projection of the book for the strategist chat (keeps tokens sane:
    hard facts + AI headlines/verdicts, not the full evidence arrays). Scope is
    resolved by _records_for_scope (opp_ids > owners > owner > whole book)."""
    out = []
    for rec in _records_for_scope(owner, owners, opp_ids)[:max_records]:
        hard = rec.get("hard") or {}
        ai = rec.get("ai") or {}
        out.append({
            "opp_id": rec.get("opp_id"),
            "account_name": hard.get("account_name"),
            "opp_name": hard.get("opp_name"),
            "owner_name": hard.get("owner_name"),
            "stage": hard.get("stage"),
            "forecast_category": hard.get("forecast_category"),
            "amount": hard.get("amount"),
            "close_date": hard.get("close_date"),
            "ais_status": hard.get("ais_status"),
            "ais_score": hard.get("ais_score"),
            "verdict": _g(ai, "north_star_verdict", "verdict"),
            "verdict_headline": _g(ai, "north_star_verdict", "headline"),
            "top_move": (_items(ai, "recommended_moves")[:1] or [{}])[0].get("action"),
            "vulnerabilities": [v.get("category") for v in _items(ai, "vulnerabilities")][:5],
        })
    return out
