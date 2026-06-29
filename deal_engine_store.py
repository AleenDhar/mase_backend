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
import random
import re
import time
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
# User overrides on the AI-derived to-dos (edit/delete), keyed by todo_key so they
# persist across daily re-sweeps; and manually-added completed updates.
T_OVERRIDES = "deal_todo_overrides"
T_MANUAL = "deal_manual_updates"
# Learning Observatory: curated learnings that evolve Deal Sweep over time.
T_LEARNINGS = "sweep_learnings"

_TIMEOUT = 30.0

# Reliability: one shared, connection-pooled HTTP client + bounded jittered retries so
# a transient PostgREST/network blip degrades instead of hard-failing a sweep upsert or
# a dashboard read. httpx.Client is safe across the run_in_executor worker threads.
# Connection errors (request never sent) retry on any verb; read-timeout/5xx/429 retry
# ONLY for idempotent verbs so a retried insert can't double-write. Tune: STORE_HTTP_RETRIES.
_client = httpx.Client(
    timeout=httpx.Timeout(_TIMEOUT),
    limits=httpx.Limits(max_keepalive_connections=20, max_connections=100),
)
_RETRY_ATTEMPTS = max(1, int(os.getenv("STORE_HTTP_RETRIES", "3")))
_CONNECT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
_MAYBE_SENT_ERRORS = (httpx.ReadTimeout, httpx.WriteTimeout, httpx.WriteError, httpx.RemoteProtocolError)


def _request(method: str, url: str, *, idempotent: bool, **kw) -> httpx.Response:
    """Pooled HTTP with bounded jittered retries (see module note)."""
    last_exc = None
    resp = None
    for attempt in range(_RETRY_ATTEMPTS):
        retry = False
        try:
            resp = _client.request(method, url, **kw)
        except _CONNECT_ERRORS as e:
            last_exc, retry = e, True
        except _MAYBE_SENT_ERRORS as e:
            last_exc = e
            if not idempotent:
                raise
            retry = True
        else:
            if idempotent and resp.status_code in (429, 500, 502, 503, 504):
                last_exc, retry = None, True
            else:
                return resp
        if not retry or attempt == _RETRY_ATTEMPTS - 1:
            break
        time.sleep(min(2.0, 0.25 * (2 ** attempt)) + random.uniform(0, 0.25))
    if resp is not None:
        return resp
    raise last_exc


# ---------------------------------------------------------------------------
# Canonical opportunity-id form — the SINGLE source of truth for the whole app.
#
# Salesforce ids come in two forms that refer to the SAME record: 15-char
# (case-sensitive; report exports, Lightning URLs) and 18-char (case-insensitive;
# the API / CDC). We standardise on the 15-CHAR form EVERYWHERE we key or store an
# opportunity (deal_records, sweep_queue, todo pushes), so the two forms can never
# create two rows / two records / two concurrent sweeps for one deal. Avoma requires
# the 15-char id and Salesforce SOQL `Id = '<15-char>'` matches fine, so 15-char is
# safe end-to-end. Every id that ENTERS a storage path must pass through here.
# ---------------------------------------------------------------------------
def canonical_opp_id(opp_id: Optional[str]) -> str:
    """The canonical 15-char Salesforce opportunity id (trimmed). Accepts a 15- or
    18-char id (or None) and always returns the 15-char form; '' for empty input."""
    return (opp_id or "").strip()[:15]


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
    resp = _request("GET", f"{_url(table)}?{'&'.join(params)}", idempotent=True, headers=_headers())
    _raise_for(resp, f"select from {table}")
    return resp.json()


def _upsert(table: str, rows, on_conflict: str, *, returning: bool = True):
    _check()
    prefer = ("return=representation," if returning else "return=minimal,") + "resolution=merge-duplicates"
    # Idempotent (merge-duplicates) -> safe to retry.
    resp = _request(
        "POST", f"{_url(table)}?on_conflict={on_conflict}",
        idempotent=True, headers=_headers(prefer), json=rows,
    )
    _raise_for(resp, f"upsert into {table}")
    return resp.json() if returning else None


def _insert(table: str, rows, *, returning: bool = True):
    _check()
    prefer = "return=representation" if returning else "return=minimal"
    # NOT idempotent -> never retried on a maybe-landed error (would double-insert).
    resp = _request("POST", _url(table), idempotent=False, headers=_headers(prefer), json=rows)
    _raise_for(resp, f"insert into {table}")
    return resp.json() if returning else None


def _delete(table: str, filters: dict) -> None:
    _check()
    params = "&".join(f"{k}=eq.{quote(str(v), safe='')}" for k, v in filters.items())
    resp = _request("DELETE", f"{_url(table)}?{params}", idempotent=True, headers=_headers())
    _raise_for(resp, f"delete from {table}")


def _patch(table: str, patch: dict, *, filters: list[str], returning: bool = False):
    """PATCH rows matching `filters` (raw PostgREST predicates) with `patch`."""
    _check()
    prefer = "return=representation" if returning else "return=minimal"
    qs = "&".join(filters) if filters else ""
    # PATCH by filter is idempotent (re-applying the same patch is a no-op) -> safe.
    resp = _request(
        "PATCH", f"{_url(table)}?{qs}" if qs else _url(table),
        idempotent=True, headers=_headers(prefer), json=patch,
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
    # Canonicalize the key to the 15-char Salesforce ID via the single app-wide
    # helper. The table is PK'd on the 15-char form, but discovery/enrichment/sweep
    # inputs frequently carry the 18-char form; without this, an 18-char upsert
    # would INSERT a duplicate row alongside the canonical 15-char one instead of
    # updating it. Keep the jsonb copy in lockstep so derivations/re-reads stay
    # consistent.
    opp_id = canonical_opp_id(record.get("opp_id"))
    if not opp_id:
        raise DealEngineError("record.opp_id is required")
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


def slim_record(rec: dict) -> dict:
    """A lightweight projection of a canonical record for LIST + aggregate views.

    Keeps `hard` (all the deal mechanics the Deals list, Matcha and the filters use)
    plus the only two `ai` summary fields the list/filters read — the verdict (chip)
    and the AI-fit signal (AI-excitement filter) — and `pulse`. It DROPS the heavy ai
    narratives/arrays (meddpicc, competitive_position, recommended_moves, requirements,
    stakeholder_map, gaps, …) and evidence_coverage, which are only needed when a deal
    DRAWER is opened (fetched then via GET /opportunities/{opp_id}). Cuts the list
    payload ~10-25x, so the book loads fast while every deal stays loaded for search."""
    rec = attach_deal_scores(rec)  # compute scores read-time if a sweep dropped them
    rec = attach_verdict_view(rec)  # stage-correct health bucket + risk tag + re-graded label
    ai = rec.get("ai") or {}
    # deal_scores: keep ONLY the headline (5 scores + read) for list chips/sort;
    # the full breakdown + commentary stays in the drawer (full record).
    _ds = ai.get("deal_scores") or {}
    _ds_slim = {"headline": _ds.get("headline")} if _ds.get("headline") else None
    return {
        "opp_id": rec.get("opp_id"),
        "hard": rec.get("hard") or {},
        "ai": {
            "north_star_verdict": ai.get("north_star_verdict"),
            "ai_fit_signal": ai.get("ai_fit_signal"),
            "deal_scores": _ds_slim,
        },
        "pulse": rec.get("pulse"),
        "forecast_critical": rec.get("forecast_critical"),
        "analysis_confidence": rec.get("analysis_confidence"),
        "swept_at": rec.get("swept_at"),
    }


_PAGE_SORT_COLS = {"account_name", "opp_name", "stage", "forecast_category",
                   "amount", "close_date", "owner_name", "swept_at", "analysis_confidence"}


def list_records_page(*, owners: Optional[list[str]] = None, q: str = "",
                      sort: str = "close_date", direction: str = "asc",
                      limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
    """Server-side PAGINATED + searched + sorted slim list for the Deals table.

    Filters/sorts on the flat indexed columns (owner_name, account_name, opp_name,
    stage, forecast_category, amount, close_date, …), reads only one page of rows,
    and returns (slim_records, total_count). The Deals UI hits this so a request
    returns ONE page instead of the whole book, and search/sort run in Postgres.
    Excludes inactive (off-report) deals, same as list_records."""
    _check()
    if sort == "days_to_close":   # computed in the UI; closest flat proxy
        sort = "close_date"
    col = sort if sort in _PAGE_SORT_COLS else "close_date"
    dirn = "desc" if str(direction).lower() in ("desc", "-1", "-") else "asc"
    try:
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
    except Exception:  # noqa: BLE001
        limit, offset = 50, 0

    params = ["select=opp_id,record", "active=is.true"]
    if owners:
        vals = ",".join('"' + str(o).replace('"', "") + '"' for o in owners if str(o).strip())
        if vals:
            enc = quote(vals, safe='",()')
            params.append("owner_name=in.(" + enc + ")")
    qclean = re.sub(r"[,()*]", " ", q or "").strip()
    if qclean:
        pat = "*" + quote(qclean) + "*"   # PostgREST ilike: * is the wildcard
        params.append(
            f"or=(account_name.ilike.{pat},opp_name.ilike.{pat},"
            f"owner_name.ilike.{pat},stage.ilike.{pat})")
    params.extend([f"order={col}.{dirn}", f"limit={limit}", f"offset={offset}"])

    headers = _headers()
    headers["Prefer"] = "count=exact"   # -> Content-Range: 0-49/441
    resp = _request("GET", f"{_url(T_RECORDS)}?{'&'.join(params)}", idempotent=True, headers=headers)
    _raise_for(resp, "paged select")
    rows = resp.json() or []
    total = None
    cr = resp.headers.get("content-range") or resp.headers.get("Content-Range")
    if cr and "/" in cr:
        tail = cr.rsplit("/", 1)[-1]
        if tail.isdigit():
            total = int(tail)
    recs = [slim_record(attach_pulse(r["record"])) for r in rows if r.get("record")]
    return recs, (total if total is not None else len(recs))


def count_active_records() -> int:
    """Cheap total count of ACTIVE (on-report) tracked deals for the Admin panel —
    matches the Deals list total. (Distinct from the existing count_records(), which
    counts every record including off-report ones.)"""
    _check()
    headers = _headers()
    headers["Prefer"] = "count=exact"
    resp = _request("GET", f"{_url(T_RECORDS)}?select=opp_id&active=is.true&limit=1",
                    idempotent=True, headers=headers)
    _raise_for(resp, "count records")
    cr = resp.headers.get("content-range") or resp.headers.get("Content-Range")
    if cr and "/" in cr:
        tail = cr.rsplit("/", 1)[-1]
        if tail.isdigit():
            return int(tail)
    return len(resp.json() or [])


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


# Confirmed economic buyers recorded in the SF MEDDPICC objects but flagged as a
# "gap" by an earlier sweep — written into the stored packet so the cache matches the
# UI (frontend helpers.getEbOverride). 15-char opp_id -> economic buyer. The sweep's
# new MEDDPICC read keeps this fresh going forward; refresh from the SF MEDDPICC scan.
EB_BACKFILL: dict = {
    "006P700000VSLhB": "Gavin Greer",
    "006P700000RFGL6": "Arnd Christochowitz",
    "006P700000J71MD": "Barbara Potisk-Eibensteiner (CFO)",
    "006P700000PlMpu": "Simon Vogelmann",
    "006P700000Xl06R": "Florence Tinsley Roi",
    "006P7000009O2Ri": "CPO + Jacques De Villiers",
    "006P700000KlsBE": "Philippe Pourquéry (Group CFO)",
    "006P7000001j0JR": "Benoit Thibaudon + M. Grootenbeer (CPO)",
    "006P700000PtQGP": "Mazin (Head of Procurement)",
    "006P700000LtIUv": "Bilal; Abraham Mathew",
    "006P700000MB1SN": "Jason Tranter (VP)",
    "006P700000FF6Np": "Ahmed Rafat (CPO)",
    "006P700000Z98IL": "Sameh Bartok (SVP Contracts & Procurement)",
    "006P700000Y1Ont": "Tarek Ibrahim Youssef (Proc Dir)",
    "006P700000T0trq": "Kerelos (Head of Procurement)",
    "006P700000CWvfN": "Dan Lahey (EVP & CFO)",
    "006P7000009T3v1": "George Andrus (ED, Head of Procurement)",
}


def backfill_economic_buyer(mapping: Optional[dict] = None) -> dict:
    """Write a confirmed economic_buyer into ai.meddpicc for each opp in `mapping`
    (15-char opp_id -> buyer name); defaults to EB_BACKFILL. Read -> patch the
    economic_buyer element -> upsert the full record. Idempotent (re-running is a
    no-op). Returns a per-opp summary."""
    mapping = mapping or EB_BACKFILL
    done: list = []
    missing: list = []
    errors: list = []
    for oid, name in mapping.items():
        key = canonical_opp_id(oid)
        if not key:
            errors.append({"opp_id": oid, "error": "bad opp_id"})
            continue
        try:
            rec = get_record(key)
        except Exception as e:  # noqa: BLE001
            errors.append({"opp_id": key, "error": str(e)})
            continue
        if not rec:
            missing.append(key)
            continue
        ai = rec.get("ai")
        ai = ai if isinstance(ai, dict) else {}
        md = ai.get("meddpicc")
        md = md if isinstance(md, dict) else {}
        prev = md.get("economic_buyer") if isinstance(md.get("economic_buyer"), dict) else {}
        md["economic_buyer"] = {
            "status": "confirmed",
            "narrative": (f"Economic buyer recorded in CRM MEDDPICC: {name}. "
                          "Visibility confirmed; engagement to be read from calls."),
            "sources": [{"type": "crm", "ref": "MEDDPICC__c"}],
            "crm_backfill": True,
        }
        ai["meddpicc"] = md
        rec["ai"] = ai
        try:
            upsert_record(rec)
            done.append({"opp_id": key, "economic_buyer": name,
                         "was": (prev or {}).get("status") or "absent"})
        except Exception as e:  # noqa: BLE001
            errors.append({"opp_id": key, "error": str(e)})
    return {"updated": len(done), "missing": missing, "errors": errors, "details": done}


def opp_trends_one(opp_id: str) -> dict:
    """Compute ai.opp_trends for ONE opp from field_history_cache (small per-opp read).
    Used by the sweep so re-sweeps recompute trends (they'd otherwise wipe the backfilled
    field). Never raises — returns {} on any problem."""
    try:
        import deal_engine_trends as trends
        k = (opp_id or "")[:15]
        if not k:
            return {}
        rows = _select("field_history_cache",
                       select="field_name,old_value,new_value,changed_date",
                       filters=[f"opportunity_id=like.{k}*",
                                "field_name=in.(Amount,CloseDate,StageName,ForecastCategoryName,ForecastCategory)"],
                       order="changed_date.desc", limit=200)
        return trends.derive_opp_trends(rows)
    except Exception as e:  # noqa: BLE001
        print(f"[OPP-TRENDS] one failed opp={opp_id}: {e}", flush=True)
        return {}


def backfill_opp_trends() -> dict:
    """Compute ai.opp_trends for every stored record from `field_history_cache` (amount,
    close-date, stage, forecast-category progression/regression) and re-score. Deterministic,
    no LLM. One batched read of the history cache; matches 18-char cache ids to 15-char book
    ids by prefix. Idempotent; safe to re-run (refreshes the trends + scores)."""
    import deal_engine_trends as trends
    import deal_engine_scoring
    from datetime import datetime, timezone, timedelta
    fields = "opportunity_id,field_name,old_value,new_value,changed_date"
    _fld = "field_name=in.(Amount,CloseDate,StageName,ForecastCategoryName,ForecastCategory)"
    # Supabase caps a response at 1000 rows, so cursor-paginate by changed_date within the
    # trend window (only the last ~130 days matter) until the page is short.
    # NOTE: a '+' in a PostgREST filter value is URL-decoded to a space, which corrupts an
    # ISO timestamp ("...+00:00" -> "... 00:00"). Use a 'Z' UTC suffix (no '+') everywhere.
    def _z(ts):
        return str(ts or "").replace("+00:00", "Z")
    cutoff = _z((datetime.now(timezone.utc) - timedelta(days=130)).isoformat())
    rows: list = []
    cursor = None
    for _ in range(50):  # hard stop; 50 * 1000 rows is far more than the window holds
        flt = [_fld, f"changed_date=gte.{cutoff}"]
        if cursor:
            flt.append(f"changed_date=lt.{cursor}")
        page = _select("field_history_cache", select=fields, filters=flt,
                       order="changed_date.desc", limit=1000)
        if not page:
            break
        rows.extend(page)
        if len(page) < 1000:
            break
        cursor = _z(page[-1].get("changed_date"))
    by_opp: dict = {}
    for r in rows:
        k = (r.get("opportunity_id") or "")[:15]
        by_opp.setdefault(k, []).append(r)

    recs = _select(T_RECORDS, select="opp_id,record", limit=100000)
    updated = 0
    with_trends = 0
    errors: list = []
    for row in recs:
        rec = row.get("record")
        if not rec:
            continue
        k = (rec.get("opp_id") or "")[:15]
        try:
            tr = trends.derive_opp_trends(by_opp.get(k, []))
        except Exception as e:  # noqa: BLE001
            errors.append({"opp_id": k, "error": f"trends: {e}"})
            continue
        ai = rec.get("ai")
        ai = ai if isinstance(ai, dict) else {}
        ai["opp_trends"] = tr            # may be {} when the deal has no recent CRM moves
        rec["ai"] = ai
        if any(not str(kk).endswith("_detail") for kk in tr):
            with_trends += 1
        try:
            ds = deal_engine_scoring.compute_deal_scores(rec)
            if ds and isinstance(ds, dict):
                ai["deal_scores"] = ds
            upsert_record(rec)
            updated += 1
        except Exception as e:  # noqa: BLE001
            errors.append({"opp_id": k, "error": str(e)})
    return {"updated": updated, "with_trends": with_trends,
            "history_rows": len(rows), "error_count": len(errors), "errors": errors[:30]}


def backfill_deal_scores(opp_ids: Optional[list] = None) -> dict:
    """Compute ai.deal_scores for stored records using the SAME deterministic model the
    sweep uses (`deal_engine_scoring.compute_deal_scores`), and upsert. This is the
    TEMPORARY push that scores the existing book NOW without waiting for each deal's
    next sweep — and because it's the exact same code path, the numbers are consistent
    with the dynamic per-sweep recompute that tracks stage + opportunity updates going
    forward. Additive (only sets ai.deal_scores). Idempotent. `opp_ids` None = whole book."""
    import deal_engine_scoring
    targets: list = []
    if opp_ids:
        for o in opp_ids:
            k = canonical_opp_id(o)
            if not k:
                continue
            try:
                r = get_record(k)
            except Exception:  # noqa: BLE001
                r = None
            if r:
                targets.append((k, r))
    else:
        rows = _select(T_RECORDS, select="opp_id,record", limit=100000)
        targets = [(row.get("opp_id"), row.get("record")) for row in rows
                   if row.get("record")]
    updated = 0
    skipped = 0
    errors: list = []
    for key, rec in targets:
        try:
            ds = deal_engine_scoring.compute_deal_scores(rec)
        except Exception as e:  # noqa: BLE001
            errors.append({"opp_id": key, "error": f"score: {e}"})
            continue
        if not ds or not isinstance(ds, dict):
            skipped += 1
            continue
        ai = rec.get("ai")
        ai = ai if isinstance(ai, dict) else {}
        ai["deal_scores"] = ds
        rec["ai"] = ai
        try:
            upsert_record(rec)
            updated += 1
        except Exception as e:  # noqa: BLE001
            errors.append({"opp_id": key, "error": str(e)})
    return {"updated": updated, "skipped": skipped,
            "error_count": len(errors), "errors": errors[:50]}


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


def regroup_todos(*, opp_id: Optional[str] = None, dry_run: bool = False,
                  sample: int = 12) -> dict:
    """Token-free pass that RE-PROJECTS the packet-backed `ai.*` lists on records
    ALREADY persisted, migrating them to the current projection shape (the 4-head
    MECE model: implicit_requirements.{we_promised,buyer_dependent}, with the legacy
    open_deliverables folded in and dropped) and tidying near-duplicates — all via
    deal_engine_packets.project_into_ai() over the record's own durable packets. No
    Avoma / Salesforce / LLM, so it migrates the back-catalogue after a
    projection-logic change without a full re-sweep. Records with no packets fall back
    to an in-place todo_grouping.tidy(). Derived/server sections (verdict, MEDDPICC,
    hard.*, pulse, deltas) are preserved.

    Per-record errors are counted and skipped so one bad record cannot abort the pass.
    dry_run computes the same stats + a sample of before/after diffs but writes
    nothing."""
    import todo_grouping
    if opp_id:
        rec = get_record(opp_id)
        rows = [{"record": rec}] if rec else []
    else:
        rows = _select(T_RECORDS, select="record", order="account_name.asc")
    stats = {"total": len(rows), "regrouped": 0, "unchanged": 0, "skipped": 0,
             "errors": 0, "deliverables_removed": 0, "flags_removed": 0}
    samples: list = []

    def _c(ai: dict):
        impl = ai.get("implicit_requirements") or {}
        if isinstance(impl, dict) and ("we_promised" in impl or "buyer_dependent" in impl):
            deliv = len(((impl.get("we_promised") or {}).get("items")) or []) \
                + len(((impl.get("buyer_dependent") or {}).get("items")) or [])
        else:
            deliv = len(((ai.get("open_deliverables") or {}).get("items")) or []) \
                + len((impl.get("items")) or [])
        return (deliv, len(((ai.get("best_practice_check") or {}).get("flags")) or []))

    for row in rows:
        rec = row.get("record")
        if not isinstance(rec, dict) or not (rec.get("opp_id") or "").strip():
            stats["skipped"] += 1
            continue
        ai = rec.get("ai")
        if not isinstance(ai, dict):
            stats["skipped"] += 1
            continue
        try:
            before = _c(ai)
            packets = rec.get("packets")
            if isinstance(packets, list) and packets:
                # Re-project from durable packets: migrates the record to the 4-head
                # shape (implicit_requirements.{we_promised,buyer_dependent}, drops
                # open_deliverables) AND tidies — no LLM. Preserves derived sections.
                rec["ai"] = _packets.project_into_ai(ai, packets, today=rec.get("swept_at"))
                ai = rec["ai"]
            else:
                todo_grouping.tidy({"ai": ai})  # packet-less record: tidy in place
            after = _c(ai)
            if after == before:
                stats["unchanged"] += 1
                continue
            stats["regrouped"] += 1
            stats["deliverables_removed"] += max(0, before[0] - after[0])
            stats["flags_removed"] += max(0, before[1] - after[1])
            if len(samples) < sample:
                samples.append({
                    "opp_id": rec.get("opp_id"),
                    "account": (rec.get("hard") or {}).get("account_name")
                    or rec.get("account_name"),
                    "deliverables": f"{before[0]}->{after[0]}",
                    "best_practice": f"{before[1]}->{after[1]}",
                })
            if not dry_run:
                upsert_record(rec)
        except Exception as e:  # noqa: BLE001 — one bad record must not abort the pass
            stats["errors"] += 1
            print(f"[REGROUP] failed opp={rec.get('opp_id')}: "
                  f"{type(e).__name__}: {e}", flush=True)
    stats["samples"] = samples
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


# ---------- To-do overrides (edit / delete) + manual completed updates ----------

def upsert_override(*, todo_key: str, opp_id: str, action: str,
                    edited_text: Optional[str] = None,
                    edited_due: Optional[str] = None,
                    created_by: Optional[str] = None,
                    category: Optional[str] = None,
                    orig_text: Optional[str] = None,
                    stage: Optional[str] = None) -> dict:
    """Persist a user edit or delete of one AI-derived to-do, keyed by todo_key so
    it survives the daily re-sweep (the regenerated item with the same key picks the
    override back up). action is 'edit' or 'delete'. category/orig_text/stage are
    captured so the Learning Observatory can mine WHAT (and at which stage) people
    delete or rewrite — a delete row alone is otherwise just an opaque hash."""
    if action not in ("edit", "delete"):
        raise DealEngineError("action must be 'edit' or 'delete'")
    row = {
        "todo_key": todo_key,
        "opp_id": (str(opp_id or "").strip())[:15],
        "action": action,
        "edited_text": edited_text,
        "edited_due": edited_due or None,
        "created_by": created_by,
        "category": category,
        "orig_text": orig_text,
        "stage": stage,
        "updated_at": _now(),
    }
    res = _upsert(T_OVERRIDES, row, on_conflict="todo_key")
    return res[0] if res else row


def clear_override(todo_key: str) -> None:
    """Remove an override (undo an edit/delete) for one todo_key."""
    _delete(T_OVERRIDES, {"todo_key": str(todo_key)})


def _overrides_index() -> dict[str, dict]:
    """Map todo_key -> {action, edited_text, edited_due} across all overrides.
    Degrades to {} if the table isn't present yet (so /todo keeps working)."""
    try:
        rows = _select(T_OVERRIDES, select="todo_key,action,edited_text,edited_due")
    except DealEngineError:
        return {}
    return {r["todo_key"]: r for r in rows if r.get("todo_key")}


def insert_manual_update(*, opp_id: str, note: str, done_date: Optional[str] = None,
                         sf_task_id: Optional[str] = None,
                         created_by: Optional[str] = None) -> dict:
    """Persist a manually-added completed update on a deal (surfaces under
    'Recently completed'). done_date is the date it was actually done."""
    row = {
        "opp_id": (str(opp_id or "").strip())[:15],
        "note": note,
        "done_date": done_date or None,
        "sf_task_id": sf_task_id,
        "created_by": created_by,
    }
    res = _insert(T_MANUAL, row)
    return res[0] if res else row


def list_manual_updates(opp_id: Optional[str] = None) -> list[dict]:
    """Manual completed updates, optionally for one opp (15-char-prefix match).
    Degrades to [] if the table isn't present yet."""
    filters = []
    if opp_id:
        prefix = (str(opp_id or "").strip())[:15]
        if prefix:
            filters.append(f"opp_id=like.{quote(prefix, safe='')}*")
    try:
        return _select(T_MANUAL, select="*", filters=filters or None,
                       order="done_date.desc")
    except DealEngineError:
        return []


# ---------- Learning Observatory: store + signal mining ----------

def list_learnings(status: Optional[str] = None) -> list[dict]:
    """Curated learnings, optionally filtered by status. Degrades to [] if absent."""
    filters = [f"status=eq.{quote(status, safe='')}"] if status else None
    try:
        return _select(T_LEARNINGS, select="*", filters=filters, order="updated_at.desc")
    except DealEngineError:
        return []


def insert_learning(*, title: str, body: str, category: str = "general",
                    stage_scope: str = "any", scope: str = "global",
                    scope_selector: Optional[dict] = None, status: str = "candidate",
                    source: str = "manual", evidence: Optional[list] = None,
                    weight: int = 0, created_by: Optional[str] = None) -> dict:
    """Insert one learning. Manual admin entries default status='candidate' (the daily
    miner uses the same path); promotion to 'active' is an explicit switch."""
    row = {
        "title": title, "body": body, "category": category, "stage_scope": stage_scope,
        "scope": scope, "scope_selector": scope_selector or {}, "status": status,
        "source": source, "evidence": evidence or [], "weight": weight,
        "created_by": created_by,
    }
    res = _insert(T_LEARNINGS, row)
    return res[0] if res else row


def update_learning(learning_id: str, patch: dict) -> Optional[dict]:
    """Patch one learning (e.g. flip status candidate->active->paused->retired)."""
    p = {**patch, "updated_at": _now()}
    res = _patch(T_LEARNINGS, p,
                 filters=[f"id=eq.{quote(str(learning_id), safe='')}"], returning=True)
    return res[0] if res else None


def existing_learning_titles() -> set[str]:
    """Lowercased titles already on record — the daily miner dedupes against these so
    it only ever adds genuinely new learnings."""
    try:
        rows = _select(T_LEARNINGS, select="title")
    except DealEngineError:
        return set()
    return {str(r.get("title", "")).strip().lower() for r in rows if r.get("title")}


def _opp_stage_map() -> dict[str, str]:
    """15-char opp id -> current SF stage, from the active book."""
    out: dict[str, str] = {}
    try:
        rows = _select(T_RECORDS, select="record", filters=["active=is.true"])
    except DealEngineError:
        return out
    for r in rows:
        rec = r.get("record") or {}
        oid = (str(rec.get("opp_id") or ""))[:15]
        if oid:
            out[oid] = (rec.get("hard") or {}).get("stage")
    return out


def mine_signals(limit_samples: int = 6) -> dict:
    """Aggregate the raw operator-behaviour signals the Learning Observatory learns
    from — grouped by deal STAGE and to-do CATEGORY:
      - deleted to-dos  (people saw no significance — what to stop generating)
      - edited to-dos   (the wording/shape people actually want)
      - completed to-dos (deal_todo_pushes — what people prioritise finishing)
      - manual updates  (deal_manual_updates — what activity people log themselves)
    This is the evidence the daily miner reads to propose significant, stage-aligned
    learnings. Read-only; no interpretation here (that's the miner's job)."""
    from collections import defaultdict
    stage_by_opp = _opp_stage_map()

    def stage_of(opp):
        return stage_by_opp.get((str(opp or ""))[:15]) or "unknown"

    def fetch(table, **kw):
        try:
            return _select(table, **kw)
        except DealEngineError:
            return []

    deletes = fetch(T_OVERRIDES, select="*", filters=["action=eq.delete"])
    edits = fetch(T_OVERRIDES, select="*", filters=["action=eq.edit"])
    pushes = fetch(T_PUSHES, select="todo_key,opp_id,category,subject,pushed_at")
    manual = fetch(T_MANUAL, select="*")

    def agg(rows, *, cat_key, text_key, stage_key=None):
        groups = defaultdict(lambda: {"count": 0, "samples": []})
        for row in rows:
            # prefer the stage captured on the row; fall back to the deal's current
            # stage so attribution works even for rows logged before capture existed.
            st = (row.get(stage_key) if stage_key else None) or stage_of(row.get("opp_id"))
            cat = row.get(cat_key) or "uncategorized"
            g = groups[(st or "unknown", cat)]
            g["count"] += 1
            t = str(row.get(text_key) or "").strip()
            if t and len(g["samples"]) < limit_samples and t not in g["samples"]:
                g["samples"].append(t[:180])
        return [{"stage": k[0], "category": k[1], "count": v["count"], "samples": v["samples"]}
                for k, v in sorted(groups.items(), key=lambda x: -x[1]["count"])]

    mgroups = defaultdict(lambda: {"count": 0, "samples": []})
    for m in manual:
        st = stage_of(m.get("opp_id"))
        g = mgroups[st]
        g["count"] += 1
        t = str(m.get("note") or "").strip()
        if t and len(g["samples"]) < limit_samples:
            g["samples"].append(t[:180])

    return {
        "deleted": agg(deletes, cat_key="category", text_key="orig_text", stage_key="stage"),
        "edited": agg(edits, cat_key="category", text_key="edited_text", stage_key="stage"),
        "completed": agg(pushes, cat_key="category", text_key="subject"),
        "manual_updates": [{"stage": k, "count": v["count"], "samples": v["samples"]}
                           for k, v in sorted(mgroups.items(), key=lambda x: -x[1]["count"])],
        "totals": {"deleted": len(deletes), "edited": len(edits),
                   "completed": len(pushes), "manual_updates": len(manual)},
    }


# ---------- Espresso (to-do) derivation ----------

# Daily to-do actionability horizon. The dashboard is rebuilt every day, so a
# to-do is only "act on it now" if it is overdue, undated, or due within this many
# days. Far-future items (a buyer's "issue RFP in Q3") are held back and resurface
# as they come into range. The agent decides separately whether a near-term soft
# nudge is worth recommending (that flows in as a recommended_move).
TODO_HORIZON_DAYS = int(os.environ.get("DEAL_TODO_HORIZON_DAYS", "60"))
# A back-planned requirement due date is anchored to the close date and may sit
# further out than the 60-day action horizon, so a real per-deal deadline shows
# (not a flat horizon date) — but still bounded so a 12-month deal doesn't show a
# requirement "due" ~11 months out. ~6 months.
REQUIREMENT_DUE_CAP_DAYS = int(os.environ.get("DEAL_REQUIREMENT_DUE_CAP_DAYS", "180"))
# Cap on best-practice flags surfaced per deal so the urgent few aren't buried.
TODO_MAX_BEST_PRACTICE = int(os.environ.get("DEAL_TODO_MAX_BEST_PRACTICE", "5"))
# Critical surface = the rolling next-moves plan. Emit several ranked moves (not just
# rank-1) so the UI can present next-7 / next-14 / next-30-day horizons; cap to keep
# the surface focused.
TODO_MAX_CRITICAL = int(os.environ.get("DEAL_TODO_MAX_CRITICAL", "6"))
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


def _heavy_requirement(text: Any) -> bool:
    """A heavy buyer deliverable needs more lead time before close (security
    review, RFP/tender response, POC, legal/redline, integration, references)."""
    return bool(re.search(
        r"\b(poc|pilot|proof[- ]of[- ]concept|security review|info[- ]?sec|"
        r"pen[- ]?test|penetration|soc\s?[12]|legal|red[- ]?line|redlin|msa|dpa|\bnda\b|"
        r"rfp|rfi|rfq|tender|workshop|integration|sandbox|data migration|"
        r"reference|business case|sign[- ]?off)\b",
        str(text or ""), re.I))


def _closest_year_date(month: int, day: int, year: Optional[int], today: date):
    """Build a date, inferring an omitted year as the one that puts the date
    CLOSEST to today (so a bare "30 June"/"18 Jul" reads as this year's upcoming
    deadline, not last year — and "30 May" still reads as the recent, overdue
    one). Future-biased counterpart to the pulse parser's past bias."""
    if year is not None:
        if year < 100:
            year += 2000
        try:
            return date(year, month, day)
        except ValueError:
            return None
    best = None
    for y in (today.year - 1, today.year, today.year + 1):
        try:
            d = date(y, month, day)
        except ValueError:
            continue
        if best is None or abs((d - today).days) < abs((best - today).days):
            best = d
    return best


def _stated_due_dates(text: Any, today: date) -> list:
    """Month-name + ISO dates stated in a requirement, with future-aware year
    inference. Numeric M/D forms are deliberately ignored to avoid prose false
    positives ("24/7", "3/4 of")."""
    s = str(text or "")
    out = []
    for m in _pulse._ISO_RE.finditer(s):
        d = _closest_year_date(int(m.group(2)), int(m.group(3)), int(m.group(1)), today)
        if d:
            out.append(d)
    for m in _pulse._DM_RE.finditer(s):
        mon = _pulse._MONTHS.get(m.group(2).lower())
        if mon:
            yr = int(m.group(3)) if m.group(3) else None
            d = _closest_year_date(mon, int(m.group(1)), yr, today)
            if d:
                out.append(d)
    for m in _pulse._MD_RE.finditer(s):
        mon = _pulse._MONTHS.get(m.group(1).lower())
        if mon:
            yr = int(m.group(3)) if m.group(3) else None
            d = _closest_year_date(mon, int(m.group(2)), yr, today)
            if d:
                out.append(d)
    return out


def _requirement_due(text: Any, close_date: Any,
                     today: Optional[date] = None):
    """Derive a trackable due date for an open prospect requirement, so the team
    can track WHEN a deliverable is due and whether it slipped.

    1. A date STATED in the requirement text ("by 18 Jul", "due 30 June") is the
       real deadline -> source "stated" (may be in the past = genuinely overdue).
    2. Otherwise back-plan from the deal close date: the ask must be resolved a
       lead time before close (heavier deliverables need more), clamped to a
       [today+3, today+HORIZON] window so it stays near-term on the
       daily-replanned board -> source "back_planned".
    Returns (iso, source); (None, None) when neither a stated date nor a close
    date is available."""
    today = today or date.today()
    try:
        cands = _stated_due_dates(text, today)
    except Exception:
        cands = []
    if cands:
        # The latest parsed date is the deadline (a "by/ due X"); an earlier date
        # in the same text is usually an origin ("raised 1 Jun, due 18 Jul").
        return max(cands).isoformat(), "stated"
    cd = _pulse._parse_date(close_date)
    if cd is None:
        return None, None
    lead = 30 if _heavy_requirement(text) else 14
    due = cd - timedelta(days=lead)
    floor = today + timedelta(days=3)
    ceil = today + timedelta(days=REQUIREMENT_DUE_CAP_DAYS)
    if due < floor:
        due = floor
    if due > ceil:
        due = ceil
    return due.isoformat(), "back_planned"


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


def attach_deal_scores(rec: dict) -> dict:
    """Return a copy of `rec` with ai.deal_scores GUARANTEED. If a sweep/re-sweep left
    it empty (or never wrote it), compute it READ-TIME from the record's stored signals
    via the same deterministic model the sweep uses — so MOM / CMT / Risk / FC can never
    render blank, regardless of what the sweep did. Mirrors attach_pulse: read-only
    (never persisted over a fresh sweep), never raises."""
    if not isinstance(rec, dict):
        return rec
    import deal_engine_scoring
    ai = rec.get("ai")
    ds = ai.get("deal_scores") if isinstance(ai, dict) else None
    if isinstance(ds, dict) and isinstance(ds.get("headline"), dict):
        h = ds["headline"]
        # Already a terminal (dead) block, or live scores present AND the deal is still
        # live — nothing to do. But a deal that just went DEAD with stale live scores must
        # be recomputed to the terminal block (don't keep showing win 40 / FC 34).
        if h.get("dead"):
            return rec
        if h.get("forecast_confidence") is not None and not deal_engine_scoring.is_dead_deal(rec):
            return rec
    try:
        scores = deal_engine_scoring.compute_deal_scores(rec)
    except Exception as e:  # noqa: BLE001 — a scoring failure must never break a read
        print(f"[DEAL-SCORES] read-time compute failed opp={rec.get('opp_id')}: {e}", flush=True)
        return rec
    if not scores:
        return rec
    out = dict(rec)
    new_ai = dict(out.get("ai") or {})
    new_ai["deal_scores"] = scores
    out["ai"] = new_ai
    return out


def attach_verdict_view(rec: dict) -> dict:
    """Return a copy of `rec` with the deal-drawer verdict view GUARANTEED + stage-correct:
    ai.north_star_verdict gets a 1-3 word `risk_tag`, a 4-bucket `health_bucket`, and its
    `verdict` label re-graded under the current stage-aware rules (see deal_engine_verdict).
    Read-only (never persisted over a sweep), never raises. If a verdict-only LLM recompute
    already owns this deal (verdict_recomputed_at stamped), its label/headline are LEFT
    ALONE — we only ensure risk_tag/health_bucket are present."""
    if not isinstance(rec, dict):
        return rec
    try:
        import deal_engine_verdict as dv
        import deal_engine_scoring
        out = dict(rec)
        ai = dict(out.get("ai") or {})
        nsv = dict(ai.get("north_star_verdict") or {})
        dead = deal_engine_scoring.is_dead_deal(out)
        if dead:
            # A dead deal reads a terminal status, not On Track / Slowing / Off Track.
            nsv["verdict"] = dead              # "Lost" | "Qualified Out" | "Omitted"
            nsv["health_bucket"] = dead
            nsv["risk_tag"] = "None"
            nsv["dead"] = True
            ai["north_star_verdict"] = nsv
            out["ai"] = ai
            return out
        owned = bool(nsv.get("verdict_recomputed_at"))
        if not owned:
            nsv["verdict"] = dv.regrade_label(out)
        if not nsv.get("risk_tag"):
            nsv["risk_tag"] = dv.derive_risk_tag(out)
        nsv["health_bucket"] = nsv.get("verdict")
        ai["north_star_verdict"] = nsv
        out["ai"] = ai
        return out
    except Exception as e:  # noqa: BLE001 — a view failure must never break a read
        print(f"[VERDICT-VIEW] read-time compute failed opp={rec.get('opp_id')}: {e}", flush=True)
        return rec


def stamp_move_overrides(rec: dict, ovr: Optional[dict] = None) -> dict:
    """Stamp every recommended_move with its deterministic todo_key and apply any
    user edit/delete override keyed by that todo_key. Serve-time only (read path),
    no mutation of the stored record. This makes the drawer's next-moves plan honour
    edits/deletes for EVERY deal — including the ones that don't flow through
    derive_todo's forecast-critical gate (whose other to-do buckets already get the
    same overrides applied in derive_todo). Pass a shared `ovr` index when mapping a
    list of records so the override table is read once, not per record."""
    if not isinstance(rec, dict):
        return rec
    ai = rec.get("ai") or {}
    rm = ai.get("recommended_moves") if isinstance(ai, dict) else None
    items = rm.get("items") if isinstance(rm, dict) else None
    if not isinstance(items, list) or not items:
        return rec
    if ovr is None:
        ovr = _overrides_index()
    opp = rec.get("opp_id") or (rec.get("hard") or {}).get("opp_id")
    out_items = []
    for m in items:
        if not isinstance(m, dict):
            out_items.append(m); continue
        key = todo_key(opp, "critical", m.get("action"),
                       m.get("act_by") or m.get("trigger_date"))
        m2 = dict(m); m2["todo_key"] = key
        o = ovr.get(key)
        if o:
            if o.get("action") == "delete":
                continue
            if o.get("action") == "edit":
                m2["edited"] = True
                if o.get("edited_text"):
                    m2["action"] = o["edited_text"]
                if o.get("edited_due"):
                    m2["act_by"] = o["edited_due"]
        out_items.append(m2)
    out = dict(rec); out_ai = dict(ai); out_rm = dict(rm)
    out_rm["items"] = out_items
    out_ai["recommended_moves"] = out_rm
    out["ai"] = out_ai
    return out


def _is_buyer_side(who: Any) -> bool:
    """True when a deliverable is owed by the BUYER (the 3b "waiting on the buyer"
    sub-bucket). Empty/unknown -> our (Zycus) side. Mirrors packets._is_buyer_who and
    the frontend isBuyerSide so the 3a/3b split is identical end-to-end."""
    w = str(who or "").strip().lower()
    if not w:
        return False
    return not any(t in w for t in ("zycus", "seller", "we ", "us ", "our"))


def _we_promised_items(ai: dict) -> list:
    """Implicit head 3a — deliverables WE owe (implicit needs + our commitments).
    New 4-head shape: implicit_requirements.we_promised. Legacy fallback: the flat
    implicit_requirements list PLUS our-side open_deliverables (so un-re-projected
    records still render under the new buckets)."""
    impl = _g(ai, "implicit_requirements", default={})
    if isinstance(impl, dict) and ("we_promised" in impl or "buyer_dependent" in impl):
        v = _g(ai, "implicit_requirements", "we_promised", "items", default=[])
        return v if isinstance(v, list) else []
    out = list(_items(ai, "implicit_requirements"))
    out.extend(d for d in _items(ai, "open_deliverables") if not _is_buyer_side(d.get("who")))
    return out


def _buyer_dependent_items(ai: dict) -> list:
    """Implicit head 3b — what the BUYER owes us, to unblock our delivery. New shape:
    implicit_requirements.buyer_dependent. Legacy fallback: buyer-side open_deliverables."""
    impl = _g(ai, "implicit_requirements", default={})
    if isinstance(impl, dict) and ("we_promised" in impl or "buyer_dependent" in impl):
        v = _g(ai, "implicit_requirements", "buyer_dependent", "items", default=[])
        return v if isinstance(v, list) else []
    return [d for d in _items(ai, "open_deliverables") if _is_buyer_side(d.get("who"))]


def _dead_deal_best_practices(rec: dict, dead_label: str) -> list:
    """For a DEAD deal (lost / qualified out / omitted) the only to-dos that make sense are
    (1) a short RETROSPECTIVE — what we didn't do well — and (2) specific SALESFORCE HYGIENE
    gaps (right stage, logged outcome). No win-back (sponsors are locked 3-5 yrs). All
    derived from the stored record, named to the actual gap."""
    hard = rec.get("hard") or {}
    ai = rec.get("ai") or {}
    stage = str(hard.get("stage") or "")
    stage_l = stage.lower()
    out: list = []

    # --- Salesforce hygiene (named to the specific gap) ---
    closed_stage = (any(m in stage_l for m in ("closed lost", "qualified out", "closed-lost",
                                               "qualified-out")) or stage_l.strip() == "lost")
    if not closed_stage:  # dead via Omitted forecast but the stage still says it's live
        out.append(f"SF hygiene: stage still reads '{stage}' but the deal is {dead_label} — "
                   f"set the stage to Closed Lost / Qualified Out so the record matches reality.")
    if not (hard.get("close_reason") or hard.get("loss_reason") or hard.get("next_step")):
        kind = "omit" if dead_label == "Omitted" else "loss"
        out.append(f"SF hygiene: log the {kind} reason and the winning vendor on the "
                   f"opportunity so win/loss analysis is accurate.")

    # --- Retrospective: what we didn't do well (grounded in the record) ---
    comp = ai.get("competitive_position") or {}
    citems = comp.get("items") if isinstance(comp.get("items"), list) else []
    winner = next((c.get("name") for c in citems
                   if str(c.get("status") or "").lower() in ("preferred", "won", "selected", "incumbent")
                   or str(c.get("threat_level") or "").lower() in ("high", "critical")), None)
    if winner:
        out.append(f"Retrospective: lost to {winner} — review why (entered late, single-threaded, "
                   f"pricing, or product gap) and record the lesson.")
    medd = ai.get("meddpicc") or {}
    eb_status = str((medd.get("economic_buyer") or {}).get("status") or "").lower()
    if eb_status in ("", "unknown", "not identified", "unmapped", "none", "no"):
        out.append("Retrospective: economic buyer was never mapped/engaged — reach power earlier "
                   "and multi-thread on similar accounts.")
    smap = (ai.get("stakeholder_map") or {}).get("items")
    if isinstance(smap, list) and len(smap) <= 1:
        out.append("Retrospective: the deal ran effectively single-threaded — build a wider buying "
                   "coalition next time.")
    if not any(o.startswith("Retrospective") for o in out):
        out.append("Retrospective: capture the loss reason and what we'd do differently for "
                   "win/loss analysis.")
    return out[:4]


def derive_todo(owner: Optional[str] = None) -> dict:
    """RSD-filterable action list grouped by impact, computed from the records.

    Every item across all five categories carries a deterministic `todo_key`
    plus its current `pushed` / `sf_task_id` state (joined from the
    deal_todo_pushes ledger) so the UI can show which to-dos were already pushed
    to Salesforce, surviving reloads."""
    import deal_engine_scoring as _scoring
    records = list_records(owner)
    pmap = _pushes_index()
    critical, important, explicit, implicit, best_practice = [], [], [], [], []

    for rec in records:
        hard = rec.get("hard") or {}
        ai = rec.get("ai") or {}
        pulse = _pulse_of(rec)
        # The deal owner's manager, ONLY when it came from the live Salesforce org
        # chart (Owner.Manager.Name) — never a model guess — so the UI can name them
        # safely. The frontend substitutes it for the generic "the deal owner's
        # manager" phrase (display-only; the todo_key stays keyed on the original text).
        _mgr = hard.get("manager_name") if hard.get("manager_name_source") == "Owner.Manager.Name" else None
        ctx = {
            "opp_id": rec.get("opp_id"),
            "account_name": hard.get("account_name"),
            "opp_name": hard.get("opp_name"),
            "owner_name": hard.get("owner_name"),
            "manager_name": _mgr,
        }

        # DEAD deal (lost / qualified out / omitted): no live action items. Keep ONLY the
        # single top play (retrospective-style) + best practices (retrospective + SF hygiene).
        # Suppress prospect requirements, Zycus commitments, and buyer-owed items entirely.
        # Read-time, so a re-opened deal auto-regains its full plan.
        _dead = _scoring.is_dead_deal(rec)
        if _dead:
            _mv = _items(ai, "recommended_moves")
            if _mv:
                _top = sorted(_mv, key=lambda m: _num(m.get("rank")) if m.get("rank") is not None else 99)[0]
                _ad = _top.get("act_by") or _top.get("trigger_date")
                critical.append(_stamp_todo({**ctx, "action": _top.get("action"),
                                 "intervention_owner": _top.get("owner"), "horizon": _top.get("horizon"),
                                 "trigger": _top.get("trigger"), "trigger_date": _top.get("trigger_date"),
                                 "act_by": _top.get("act_by"), "urgency": _urgency(_ad),
                                 "expected_effect": _top.get("expected_effect")},
                                 "critical", _top.get("action"), _ad, pmap))
            for _bp in _dead_deal_best_practices(rec, _dead):
                best_practice.append(_stamp_todo({**ctx, "flag": _bp}, "bestPractice", _bp, None, pmap))
            continue

        # Moves: EVERY recommended move on EVERY deal. These fold into the
        # "Commitments made by Zycus" bucket in the UI, so the full ranked plan is
        # always visible — no forecast-critical gate, no near-term-horizon filter.
        # (Carried under the `critical` to-do category so existing push/edit state
        # keyed by todo_key survives.)
        moves = _items(ai, "recommended_moves")
        if moves:
            def _act_date(m):
                return m.get("act_by") or m.get("trigger_date")
            ranked = sorted(moves, key=lambda m: _num(m.get("rank")) if m.get("rank") is not None else 99)
            for top in ranked:
                critical.append(_stamp_todo({**ctx,
                                 "action": top.get("action"),
                                 "intervention_owner": top.get("owner"),
                                 "horizon": top.get("horizon"),
                                 "trigger": top.get("trigger"),
                                 "trigger_date": top.get("trigger_date"),
                                 "act_by": top.get("act_by"),
                                 "urgency": _urgency(_act_date(top)),
                                 "expected_effect": top.get("expected_effect")},
                                 "critical", top.get("action"), _act_date(top), pmap))

        # Waiting on the buyer (implicit head 3b): what the BUYER owes us, to unblock
        # our delivery. Kept under the `important` to-do category so existing push /
        # edit state survives. Fed by implicit_requirements.buyer_dependent (legacy:
        # buyer-side open_deliverables).
        for d in _buyer_dependent_items(ai):
            txt = d.get("deliverable") or d.get("commitment")
            if not txt:
                continue
            status = (d.get("status") or "").lower()
            # Defer commitments due more than ~2 months out; they resurface daily.
            if status in ("", "open", "overdue") and _within_todo_horizon(d.get("due")) \
                    and _within_recency(d.get("date")):
                important.append(_stamp_todo({**ctx,
                                  "who": d.get("who") or "Buyer",
                                  "commitment": txt, "deliverable": txt,
                                  "due": d.get("due"),
                                  "urgency": _urgency(d.get("due")),
                                  "status": status},
                                  "important", txt, d.get("due"), pmap))

        # Explicit requirements still open. Each carries a trackable due date so a
        # buyer-owed deliverable can be followed for timeliness: a date stated in the
        # ask wins, else one back-planned from the close date. `due_source` lets the
        # UI distinguish a hard stated deadline from an inferred target.
        for r in _items(ai, "explicit_requirements"):
            if not r.get("addressed") and _within_recency(r.get("date")):
                _due, _due_src = _requirement_due(
                    r.get("requirement"), hard.get("close_date"))
                # A due the sweep itself captured is a hard stated deadline.
                _swept_due = r.get("due") or r.get("due_date")
                if _swept_due:
                    _due, _due_src = _swept_due, "stated"
                explicit.append(_stamp_todo({**ctx,
                                 "requirement": r.get("requirement"),
                                 "said_by": r.get("said_by"),
                                 "date": r.get("date"),
                                 "due": _due, "act_by": _due,
                                 "due_source": _due_src,
                                 "urgency": _urgency(_due)},
                                 "explicitRequirements", r.get("requirement"),
                                 r.get("date"), pmap))

        # Commitments made by Zycus (implicit head 3a "we promised"): implicit needs
        # we owe + our open commitments. Fed by implicit_requirements.we_promised
        # (legacy: flat implicit_requirements + our-side open_deliverables). A dated
        # commitment is horizon-gated; a pure need (no due) is recency-gated only.
        for r in _we_promised_items(ai):
            txt = r.get("deliverable") or r.get("inferred_need") or r.get("commitment")
            if not txt:
                continue
            if r.get("due"):
                status = (r.get("status") or "").lower()
                if status not in ("", "open", "overdue") \
                        or not _within_todo_horizon(r.get("due")) \
                        or not _within_recency(r.get("date")):
                    continue
            elif not _within_recency(r.get("date")):
                continue
            # Commitment-evidence gate (C-level rule): an item is a Zycus COMMITMENT
            # only when Zycus actually committed it on a call / email / channel —
            # proven by a grounding_quote or a named source. With no such evidence
            # it's an inferred "we should…", which is a Best practice, NOT a
            # commitment. Enforced HERE (at the source) so every surface classifies
            # it identically — Espresso (raw categories), Matcha, and the drawer's
            # display buckets.
            _has_commitment_evidence = bool(
                (r.get("grounding_quote") or "").strip()
                or (r.get("source") or "").strip())
            if _has_commitment_evidence:
                implicit.append(_stamp_todo({**ctx,
                                 "inferred_need": txt, "deliverable": txt,
                                 "grounding_quote": r.get("grounding_quote"),
                                 "source": r.get("source"),
                                 "who": r.get("who") or "Zycus",
                                 "due": r.get("due"),
                                 "urgency": _urgency(r.get("due") or r.get("date")),
                                 "status": (r.get("status") or "").lower(),
                                 "date": r.get("date")},
                                 "implicit", txt, r.get("date"), pmap))
            else:
                best_practice.append(_stamp_todo({**ctx, "flag": txt},
                                     "bestPractice", txt, r.get("date"), pmap))

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

    # Apply user overrides (edit/delete), keyed by todo_key so they survive the
    # daily re-sweep: a deleted to-do stays gone; an edited one keeps the user's
    # wording (and due date). The todo_key stays the ORIGINAL, so push state and
    # the override both keep matching the regenerated item.
    ovr = _overrides_index()
    def _apply_overrides(items: list, primary_field: str) -> list:
        if not ovr:
            return items
        out = []
        for it in items:
            o = ovr.get(it.get("todo_key"))
            if not o:
                out.append(it); continue
            if o.get("action") == "delete":
                continue
            if o.get("action") == "edit":
                it = dict(it)
                it["edited"] = True
                if o.get("edited_text"):
                    it[primary_field] = o["edited_text"]
                if o.get("edited_due"):
                    it["due"] = o["edited_due"]; it["act_by"] = o["edited_due"]
            out.append(it)
        return out
    critical = _apply_overrides(critical, "action")
    important = _apply_overrides(important, "commitment")
    explicit = _apply_overrides(explicit, "requirement")
    implicit = _apply_overrides(implicit, "inferred_need")
    best_practice = _apply_overrides(best_practice, "flag")

    # ---- MECE de-duplication (one ask = one row) ----
    # The same ask was landing in multiple buckets: a buyer Requirement re-stated as
    # a Zycus Commitment AND folded into a Move, plus exact dupes within a bucket.
    # Collapse them deterministically (read-time, no re-sweep), scoped PER OPP so two
    # different deals can legitimately share wording.
    def _todo_text(it: dict) -> str:
        for k in ("action", "commitment", "requirement", "inferred_need",
                  "deliverable", "flag", "text"):
            v = it.get(k)
            if v:
                return str(v)
        return ""

    def _norm_todo(t: str) -> str:
        s = "".join(c if c.isalnum() else " " for c in str(t or "").lower())
        return " ".join(s.split())

    def _same_ask(a: str, b: str) -> bool:
        # exact normalised match, or one clearly contained in the other (guarded by
        # length so short generic phrases don't over-collapse).
        if not a or not b:
            return False
        if a == b:
            return True
        return len(min(a, b, key=len)) > 12 and (a in b or b in a)

    def _dedup_within(items: list) -> list:
        kept: list = []
        seen: dict = {}  # opp_id -> [normalised keys kept]
        for it in items:
            k = _norm_todo(_todo_text(it))
            if not k:
                kept.append(it)
                continue
            ks = seen.setdefault(it.get("opp_id"), [])
            if any(_same_ask(k, ek) for ek in ks):
                continue
            kept.append(it)
            ks.append(k)
        return kept

    critical = _dedup_within(critical)
    important = _dedup_within(important)
    explicit = _dedup_within(explicit)
    implicit = _dedup_within(implicit)
    best_practice = _dedup_within(best_practice)

    # Cross-bucket: a Commitment (implicit) that merely restates a Prospect
    # Requirement (explicit) or a buyer-owed item (important) on the SAME deal is a
    # duplicate — the buyer-stated ask owns the row; drop the mirrored commitment.
    _req_keys: dict = {}
    for it in explicit + important:
        k = _norm_todo(_todo_text(it))
        if k:
            _req_keys.setdefault(it.get("opp_id"), []).append(k)
    implicit = [it for it in implicit
                if not any(_same_ask(_norm_todo(_todo_text(it)), rk)
                           for rk in _req_keys.get(it.get("opp_id"), []))]

    # Manually-added completed updates (book-wide; carry opp_id only, so the
    # frontend filters by opp and merges them into 'Recently completed').
    manual = [{
        "id": m.get("id"), "opp_id": m.get("opp_id"),
        "note": m.get("note"), "done_date": m.get("done_date"),
        "sf_task_id": m.get("sf_task_id"), "created_by": m.get("created_by"),
        "created_at": m.get("created_at"),
    } for m in list_manual_updates(None)]

    return {
        "owner": owner or "all",
        "critical": critical,
        "important": important,
        "explicitRequirements": explicit,
        "implicit": implicit,
        "bestPractice": best_practice,
        "manualCompleted": manual,
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
