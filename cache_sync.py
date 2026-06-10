"""
cache_sync.py — populates and keeps fresh 3 Supabase cache tables:
    public.opportunity_cache    (1 row per Opportunity)
    public.meeting_cache        (1 row per meeting_uuid)
    public.field_history_cache  (1 row per field change)

Functions are intentionally pure-Python + functional. supabase client is passed
in (no module-level state) so tests/scripts can use a mock.

Data sources:
    1. Bulk import from deal_summaries_bulk_parallel.json (one-time backfill).
    2. update_cache_from_report() — called at the end of enrich_and_summarize
       in server.py /webhook flow. Drives meeting_cache + field_history_cache
       growth + keeps opportunity_cache fresh as Avoma meetings arrive.
    3. sync_sf_changes_to_cache() — optional cron endpoint to poll SF for
       OpportunityFieldHistory rows newer than the most recent cached change
       and refresh affected opportunity_cache rows.

Health score formula (documented in replit.md):
    stage_progress  = Probability * 0.5                      [0..50]
    recency         = 30 / (days_since_last_meeting + 1)     [0..30, capped]
    activity        = min(open_tasks_count, 5) * 4           [0..20]
    momentum_field  = +10 if stage advanced last 30d, -10 if regressed
    total           = clamp(sum, 0, 100)
    momentum bucket = Active >=75 | Moderate 50-74 | Slow 25-49 | Stalled <25
    risks           = JSON array of human-readable risk strings
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

# Stage rank used for advancement detection (higher = later in funnel).
# Zycus pipeline order based on observed values in bulk import.
_STAGE_RANK = {
    "Initial Interest":    1,
    "Qualified":           2,
    "Shortlisted":         3,
    "Formal Evaluation":   4,
    "Vendor Selected":     5,
    "Negotiation":         6,
    "Closed Won":          7,
    "Closed Lost":         0,  # treated as regression
}


# ---------- helpers ----------

def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    """Always returns tz-aware datetime (assumes UTC if no tz info in string)."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None

def _days_since(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0, (datetime.now(timezone.utc) - dt).days)

def _first(lst, default=None):
    if isinstance(lst, list) and lst:
        return lst[0]
    return default


# ---------- health score ----------

def calculate_health_score(
    deal_health: Optional[dict],
    account_briefing: Optional[dict],
    full_snapshot: Optional[dict],
    *,
    last_meeting_at: Optional[datetime] = None,
) -> dict:
    """Returns {score, momentum, days_in_stage, risks}.

    All inputs are tier dicts produced by sf_meeting_enricher. Any may be None
    (no-opp meetings) — function degrades gracefully.
    """
    deal_health = deal_health or {}
    full_snapshot = full_snapshot or {}

    opp = _first(deal_health.get("opportunity") or []) or {}
    probability = opp.get("Probability") or 0
    stage = opp.get("StageName")
    close_date = _parse_dt(opp.get("CloseDate"))

    # stage_progress 0..50
    stage_progress = float(probability) * 0.5

    # recency 0..30
    days_since_meeting = _days_since(last_meeting_at)
    recency = 30.0 / ((days_since_meeting or 0) + 1) if last_meeting_at else 0.0
    recency = min(recency, 30.0)

    # activity 0..20
    open_tasks = deal_health.get("open_tasks") or []
    activity = min(len(open_tasks), 5) * 4.0

    # momentum from stage_history (last 30d)
    momentum_delta = 0.0
    stage_hist = deal_health.get("stage_history") or []
    if stage_hist:
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        recent = [h for h in stage_hist if (_parse_dt(h.get("CreatedDate") or h.get("createdDate")) or cutoff) >= cutoff]
        if len(recent) >= 2:
            ranks = [_STAGE_RANK.get(h.get("StageName"), -1) for h in recent]
            ranks = [r for r in ranks if r >= 0]
            if ranks and ranks[0] > ranks[-1]:
                momentum_delta = +10.0
            elif ranks and ranks[0] < ranks[-1]:
                momentum_delta = -10.0

    score = max(0.0, min(100.0, stage_progress + recency + activity + momentum_delta))

    if score >= 75:
        momentum = "Active"
    elif score >= 50:
        momentum = "Moderate"
    elif score >= 25:
        momentum = "Slow"
    else:
        momentum = "Stalled"

    # days_in_stage — from last stage change
    days_in_stage = None
    if stage_hist:
        last_change = _parse_dt(stage_hist[0].get("CreatedDate") or stage_hist[0].get("createdDate"))
        days_in_stage = _days_since(last_change)

    # risk signals
    risks: list[str] = []
    if close_date and close_date < datetime.now(timezone.utc):
        risks.append(f"close_date_passed:{close_date.date().isoformat()}")
    if days_since_meeting is not None and days_since_meeting > 30:
        risks.append(f"no_meetings_{days_since_meeting}d")
    if momentum_delta < 0:
        risks.append("stage_regressed_last_30d")
    if not open_tasks:
        risks.append("no_open_tasks")
    # amount regression — compare against most recent stage_history Amount
    if len(stage_hist) >= 2:
        try:
            cur = float(opp.get("Amount") or 0)
            prev = float(stage_hist[-1].get("Amount") or 0)
            if prev > 0 and cur < prev * 0.8:
                risks.append(f"amount_dropped:{prev:.0f}->{cur:.0f}")
        except Exception:
            pass

    return {
        "score": round(score, 1),
        "momentum": momentum,
        "days_in_stage": days_in_stage,
        "risks": risks,
    }


# ---------- bulk import from JSON ----------

def bulk_import_opportunities(supabase, json_path: str, *, batch: int = 100) -> dict:
    """One-time backfill from deal_summaries_bulk_parallel.json.

    JSON shape: { opp_id: { opportunity_name, account_name, stage_name, amount,
                            close_date, meetings_count, ... } }
    Sets data_source='bulk_backfill' so webhook updates can be distinguished
    later. Does NOT touch meeting_cache / field_history_cache (those grow
    from webhook events).
    """
    with open(json_path) as f:
        data = json.load(f)

    rows = []
    for opp_id, opp in data.items():
        rows.append({
            "opportunity_id":   opp_id,
            "opportunity_name": opp.get("opportunity_name"),
            "account_name":     opp.get("account_name"),
            "stage_name":       opp.get("stage_name"),
            "amount":           opp.get("amount"),
            "close_date":       opp.get("close_date"),
            "meetings_count":   opp.get("meetings_count") or 0,
            "data_source":      "bulk_backfill",
            "last_synced_at":   _utcnow_iso(),
            "updated_at":       _utcnow_iso(),
        })

    inserted = 0
    failed = 0
    for i in range(0, len(rows), batch):
        chunk = rows[i:i+batch]
        try:
            supabase.table("opportunity_cache").upsert(chunk, on_conflict="opportunity_id").execute()
            inserted += len(chunk)
        except Exception as e:
            failed += len(chunk)
            print(f"[CACHE-BULK] batch {i//batch} failed: {e}", flush=True)
    return {"total": len(rows), "inserted": inserted, "failed": failed}


# ---------- webhook-driven incremental update ----------

def bulk_import_field_history(supabase, json_path: str, *, batch: int = 500) -> dict:
    """One-time backfill from OpportunityFieldHistory JSON dump.

    Shape: list of {opportunity_id, field_name, old_value, new_value,
                    changed_date, changed_by}.
    Deduped via the UNIQUE (opportunity_id, field_name, changed_date,
    old_value, new_value) constraint + ignore_duplicates=True.
    """
    with open(json_path) as f:
        data = json.load(f)

    rows = [{
        "opportunity_id": r.get("opportunity_id"),
        "field_name":     r.get("field_name"),
        "old_value":      None if r.get("old_value") is None else str(r["old_value"]),
        "new_value":      None if r.get("new_value") is None else str(r["new_value"]),
        "changed_date":   r.get("changed_date"),
        "changed_by":     r.get("changed_by"),
        "source":         "opp_field_history_bulk",
    } for r in data if r.get("opportunity_id") and r.get("field_name") and r.get("changed_date")]

    inserted = 0
    failed = 0
    for i in range(0, len(rows), batch):
        chunk = rows[i:i+batch]
        try:
            supabase.table("field_history_cache").upsert(
                chunk,
                on_conflict="opportunity_id,field_name,changed_date,old_value,new_value",
                ignore_duplicates=True,
            ).execute()
            inserted += len(chunk)
        except Exception as e:
            failed += len(chunk)
            print(f"[CACHE-BULK-FH] batch {i//batch} failed: {e}", flush=True)
    return {"total": len(rows), "inserted": inserted, "failed": failed,
            "unique_opps": len({r["opportunity_id"] for r in rows})}


def update_cache_from_report(supabase, report: dict, *, report_id: Optional[str] = None) -> dict:
    """Called after enrich_and_summarize writes a row to avoma_event_reports.

    Updates:
      - opportunity_cache (upsert opp + bump meetings_count, set health)
      - meeting_cache     (insert if new meeting_uuid)
      - field_history_cache (insert any new OpportunityFieldHistory + stage_history rows)

    Never raises — logs and returns counts so caller stays alive.
    """
    out = {"opp_upserts": 0, "meeting_inserts": 0, "field_history_inserts": 0, "errors": []}
    try:
        deal_health      = report.get("deal_health_data") or {}
        account_briefing = report.get("account_briefing_data") or {}
        full_snapshot    = report.get("full_snapshot_data") or {}

        opp_row = _first(deal_health.get("opportunity") or [])
        acct_row = _first((account_briefing.get("account") or [])) if account_briefing else None
        meeting_start = _parse_dt(report.get("meeting_start_at"))

        # --- 1. opportunity_cache ---
        if opp_row and opp_row.get("Id"):
            health = calculate_health_score(deal_health, account_briefing, full_snapshot, last_meeting_at=meeting_start)
            owner_name = (opp_row.get("Owner") or {}).get("Name") if isinstance(opp_row.get("Owner"), dict) else None
            account_name = ((opp_row.get("Account") or {}).get("Name")
                            if isinstance(opp_row.get("Account"), dict)
                            else (acct_row or {}).get("Name"))
            account_id = ((opp_row.get("Account") or {}).get("Id")
                          if isinstance(opp_row.get("Account"), dict)
                          else (acct_row or {}).get("Id"))

            # meetings_count + last_meeting_date: fetch current row to increment
            cur = supabase.table("opportunity_cache").select("meetings_count,last_meeting_date").eq("opportunity_id", opp_row["Id"]).limit(1).execute()
            prev_count = (cur.data[0]["meetings_count"] if cur.data else 0) or 0
            prev_last = _parse_dt(cur.data[0]["last_meeting_date"]) if cur.data else None
            new_last = meeting_start if (meeting_start and (prev_last is None or meeting_start > prev_last)) else prev_last
            new_last_iso = new_last.isoformat() if new_last else None

            upsert_payload = {
                "opportunity_id":          opp_row["Id"],
                "opportunity_name":        opp_row.get("Name"),
                "account_id":              account_id,
                "account_name":            account_name,
                "amount":                  opp_row.get("Amount"),
                "stage_name":              opp_row.get("StageName"),
                "close_date":              opp_row.get("CloseDate"),
                "probability":             opp_row.get("Probability"),
                "owner_name":              owner_name,
                "is_closed":               opp_row.get("IsClosed"),
                "meetings_count":          prev_count + 1,
                "last_meeting_date":       new_last_iso,
                "days_since_last_meeting": _days_since(new_last),
                "health_score":            health["score"],
                "momentum":                health["momentum"],
                "days_in_stage":           health["days_in_stage"],
                "risk_signals":            health["risks"],
                "latest_report_id":        report_id,
                "latest_report_date":      _utcnow_iso(),
                "data_source":             "webhook_update",
                "last_synced_at":          _utcnow_iso(),
                "updated_at":              _utcnow_iso(),
            }
            supabase.table("opportunity_cache").upsert(upsert_payload, on_conflict="opportunity_id").execute()
            out["opp_upserts"] = 1

        # --- 2. meeting_cache ---
        muid = report.get("meeting_uuid")
        if muid:
            # Prefer the Id from the SF pull's opp row, but fall back to the
            # opportunity_id resolved from Avoma (report["sf_opportunity_id"]).
            # Without the fallback, a meeting whose opp was resolved but whose SF
            # pull returned no row (deleted opp / pull error) lands with an EMPTY
            # opportunity_ids, so list_opportunity_meetings can never find it.
            fallback_opp_id = report.get("sf_opportunity_id")
            if opp_row and opp_row.get("Id"):
                opp_ids = [opp_row["Id"]]
            elif fallback_opp_id:
                opp_ids = [fallback_opp_id]
            else:
                opp_ids = []
            account_id = (
                (opp_row.get("Account") or {}).get("Id")
                if isinstance(opp_row, dict) and isinstance(opp_row.get("Account"), dict)
                else (acct_row or {}).get("Id")
            ) or report.get("sf_account_id")
            meeting_payload = {
                "meeting_uuid":    muid,
                "meeting_title":   report.get("meeting_subject"),
                "meeting_date":    report.get("meeting_start_at"),
                "opportunity_ids": opp_ids,
                "account_id":      account_id,
                "account_name":    (opp_row.get("Account") or {}).get("Name") if isinstance(opp_row, dict) and isinstance(opp_row.get("Account"), dict) else (acct_row or {}).get("Name"),
                "transcript_summary": report.get("full_snapshot_summary") or report.get("deal_health_summary"),
                "report_id":       report_id,
            }
            try:
                supabase.table("meeting_cache").upsert(meeting_payload, on_conflict="meeting_uuid").execute()
                out["meeting_inserts"] = 1
            except Exception as e:
                out["errors"].append(f"meeting_cache: {e}")

        # --- 3. field_history_cache ---
        # Combine stage_history (Tier 1) + opp_field_history (Tier 3).
        fh_rows = []
        for h in (deal_health.get("stage_history") or []):
            fh_rows.append({
                "opportunity_id": opp_row["Id"] if opp_row else None,
                "field_name":     "StageName",
                "old_value":      str(h.get("StageName")) if h.get("StageName") is not None else None,
                "new_value":      str(h.get("StageName")) if h.get("StageName") is not None else None,
                "changed_date":   h.get("CreatedDate") or h.get("createdDate"),
                "changed_by":     (h.get("CreatedBy") or {}).get("Name") if isinstance(h.get("CreatedBy"), dict) else None,
                "source":         "stage_history",
            })
        for h in (full_snapshot.get("opp_field_history") or []):
            fh_rows.append({
                "opportunity_id": opp_row["Id"] if opp_row else None,
                "field_name":     h.get("Field"),
                "old_value":      str(h.get("OldValue")) if h.get("OldValue") is not None else None,
                "new_value":      str(h.get("NewValue")) if h.get("NewValue") is not None else None,
                "changed_date":   h.get("CreatedDate") or h.get("createdDate"),
                "changed_by":     (h.get("CreatedBy") or {}).get("Name") if isinstance(h.get("CreatedBy"), dict) else None,
                "source":         "opp_field_history",
            })
        # filter rows missing required keys
        fh_rows = [r for r in fh_rows if r["opportunity_id"] and r["field_name"] and r["changed_date"]]
        if fh_rows:
            try:
                # ON CONFLICT (opportunity_id, field_name, changed_date, old_value, new_value) DO NOTHING semantics:
                # supabase-py upsert with ignore_duplicates=True
                supabase.table("field_history_cache").upsert(
                    fh_rows,
                    on_conflict="opportunity_id,field_name,changed_date,old_value,new_value",
                    ignore_duplicates=True,
                ).execute()
                out["field_history_inserts"] = len(fh_rows)
            except Exception as e:
                out["errors"].append(f"field_history_cache: {e}")
    except Exception as e:
        out["errors"].append(f"top-level: {e}")
    return out


# ---------- SF polling (optional cron) ----------

def sync_sf_changes_to_cache(supabase, sf, *, lookback_minutes: int = 20) -> dict:
    """Poll OpportunityFieldHistory for recent changes; refresh affected
    opportunity_cache rows + insert field history.

    Returns {changes_seen, opps_refreshed, field_history_inserts}.
    """
    # SOQL has no LAST_N_MINUTES date literal, so use an explicit datetime
    # threshold (UTC, second precision) for a minute-granular lookback window.
    since = (datetime.now(timezone.utc)
             - timedelta(minutes=max(lookback_minutes, 1))).strftime("%Y-%m-%dT%H:%M:%SZ")
    q = (
        "SELECT OpportunityId, Field, OldValue, NewValue, CreatedDate, "
        "CreatedBy.Name FROM OpportunityFieldHistory "
        f"WHERE CreatedDate >= {since}"
    )
    res = sf.query_all(q).get("records") or []
    out = {"changes_seen": len(res), "opps_refreshed": 0, "field_history_inserts": 0}
    if not res:
        return out

    affected_opps: set[str] = set()
    fh_rows = []
    for r in res:
        opp_id = r.get("OpportunityId")
        affected_opps.add(opp_id)
        fh_rows.append({
            "opportunity_id": opp_id,
            "field_name":     r.get("Field"),
            "old_value":      str(r.get("OldValue")) if r.get("OldValue") is not None else None,
            "new_value":      str(r.get("NewValue")) if r.get("NewValue") is not None else None,
            "changed_date":   r.get("CreatedDate"),
            "changed_by":     (r.get("CreatedBy") or {}).get("Name") if isinstance(r.get("CreatedBy"), dict) else None,
            "source":         "sf_poll",
        })
    if fh_rows:
        try:
            supabase.table("field_history_cache").upsert(
                fh_rows,
                on_conflict="opportunity_id,field_name,changed_date,old_value,new_value",
                ignore_duplicates=True,
            ).execute()
            out["field_history_inserts"] = len(fh_rows)
        except Exception as e:
            print(f"[CACHE-CRON] field_history insert failed: {e}", flush=True)

    # refresh each affected opp's current state
    for opp_id in affected_opps:
        try:
            opp = sf.query(
                "SELECT Id, Name, Amount, StageName, CloseDate, Probability, "
                "Owner.Name, IsClosed, Account.Id, Account.Name "
                f"FROM Opportunity WHERE Id = '{opp_id}'"
            ).get("records") or []
            if not opp:
                continue
            o = opp[0]
            supabase.table("opportunity_cache").upsert({
                "opportunity_id":   opp_id,
                "opportunity_name": o.get("Name"),
                "amount":           o.get("Amount"),
                "stage_name":       o.get("StageName"),
                "close_date":       o.get("CloseDate"),
                "probability":      o.get("Probability"),
                "owner_name":       (o.get("Owner") or {}).get("Name") if isinstance(o.get("Owner"), dict) else None,
                "is_closed":        o.get("IsClosed"),
                "account_id":       (o.get("Account") or {}).get("Id") if isinstance(o.get("Account"), dict) else None,
                "account_name":     (o.get("Account") or {}).get("Name") if isinstance(o.get("Account"), dict) else None,
                "data_source":      "sf_poll",
                "last_synced_at":   _utcnow_iso(),
                "updated_at":       _utcnow_iso(),
            }, on_conflict="opportunity_id").execute()
            out["opps_refreshed"] += 1
        except Exception as e:
            print(f"[CACHE-CRON] opp {opp_id} refresh failed: {e}", flush=True)
    return out


def discover_new_opportunities(
    supabase,
    sf,
    *,
    lookback_hours: int = 26,
    include_modified: bool = False,
) -> dict:
    """Discover Salesforce opportunities created (and optionally modified) within
    the lookback window and upsert them into opportunity_cache.

    Unlike update_cache_from_report / the meeting-linked pull, this has NO
    dependency on an Avoma meeting having occurred — it scans Salesforce
    directly so brand-new opportunities land in the cache automatically.

    Idempotent: upserts on opportunity_id, supplying only the columns we know
    from the SOQL row (meetings_count / health fields on any pre-existing row
    are left untouched). Never raises — logs and returns counts so the caller
    stays alive.

    Returns {window_opps_found, new_opps_found, opps_upserted}.
    """
    out = {"window_opps_found": 0, "new_opps_found": 0, "opps_upserted": 0}
    since_dt = datetime.now(timezone.utc) - timedelta(hours=max(lookback_hours, 1))
    since = since_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    where = f"CreatedDate >= {since}"
    if include_modified:
        where = f"(CreatedDate >= {since} OR SystemModstamp >= {since})"
    q = (
        "SELECT Id, Name, Amount, StageName, CloseDate, Probability, "
        "Owner.Name, IsClosed, Account.Id, Account.Name, CreatedDate "
        f"FROM Opportunity WHERE {where} ORDER BY CreatedDate DESC"
    )
    try:
        res = sf.query_all(q).get("records") or []
    except Exception as e:
        print(f"[CACHE-DISCOVERY] SOQL query failed: {e}", flush=True)
        return out

    out["window_opps_found"] = len(res)
    if not res:
        return out

    found_ids = [r.get("Id") for r in res if r.get("Id")]

    # Figure out which are genuinely new (not already cached) for reporting only.
    existing: set[str] = set()
    try:
        for i in range(0, len(found_ids), 100):
            chunk = found_ids[i:i + 100]
            ex = (supabase.table("opportunity_cache")
                  .select("opportunity_id").in_("opportunity_id", chunk).execute())
            existing.update(row["opportunity_id"] for row in (ex.data or []))
    except Exception as e:
        print(f"[CACHE-DISCOVERY] existing-id lookup failed: {e}", flush=True)
    out["new_opps_found"] = len([i for i in found_ids if i not in existing])

    rows = []
    for o in res:
        opp_id = o.get("Id")
        if not opp_id:
            continue
        rows.append({
            "opportunity_id":   opp_id,
            "opportunity_name": o.get("Name"),
            "amount":           o.get("Amount"),
            "stage_name":       o.get("StageName"),
            "close_date":       o.get("CloseDate"),
            "probability":      o.get("Probability"),
            "owner_name":       (o.get("Owner") or {}).get("Name") if isinstance(o.get("Owner"), dict) else None,
            "is_closed":        o.get("IsClosed"),
            "account_id":       (o.get("Account") or {}).get("Id") if isinstance(o.get("Account"), dict) else None,
            "account_name":     (o.get("Account") or {}).get("Name") if isinstance(o.get("Account"), dict) else None,
            "data_source":      "sf_discovery",
            "last_synced_at":   _utcnow_iso(),
            "updated_at":       _utcnow_iso(),
        })

    for i in range(0, len(rows), 100):
        chunk = rows[i:i + 100]
        try:
            supabase.table("opportunity_cache").upsert(chunk, on_conflict="opportunity_id").execute()
            out["opps_upserted"] += len(chunk)
        except Exception as e:
            print(f"[CACHE-DISCOVERY] upsert batch {i // 100} failed: {e}", flush=True)
    return out


# ---------- batched read for "current state of one tenant" ----------

async def get_tenant_state(supabase, opp_id: str, *, meetings_limit: int = 10) -> dict:
    """Returns full current state for one opportunity in ONE round-trip-equivalent
    by firing 3 cache reads CONCURRENTLY via asyncio.gather + thread executor.

    Sync supabase-py client doesn't expose an async API and PostgREST has no
    cross-table embed for non-FK joins, so we parallelise instead of batching.
    Latency ≈ max(opp, meetings, history) rather than sum.
    """
    import asyncio as _aio
    loop = _aio.get_running_loop()

    def _opp():
        return supabase.table("opportunity_cache").select("*").eq("opportunity_id", opp_id).execute().data
    def _meets():
        return (supabase.table("meeting_cache").select("*")
                .contains("opportunity_ids", [opp_id])
                .order("meeting_date", desc=True).limit(meetings_limit).execute().data)
    def _hist():
        return (supabase.table("field_history_cache").select("*")
                .eq("opportunity_id", opp_id)
                .order("changed_date", desc=True).execute().data)

    opp, meetings, history = await _aio.gather(
        loop.run_in_executor(None, _opp),
        loop.run_in_executor(None, _meets),
        loop.run_in_executor(None, _hist),
    )

    # Quick history rollup so callers don't re-scan client-side.
    from collections import Counter
    field_counts = Counter(h.get("field_name") for h in (history or []))

    return {
        "opportunity_id": opp_id,
        "opportunity":    (opp or [None])[0],
        "meetings":       meetings or [],
        "meetings_count": len(meetings or []),
        "history":        history or [],
        "history_count":  len(history or []),
        "history_summary": {
            "by_field":     dict(field_counts.most_common()),
            "last_change":  (history or [{}])[0].get("changed_date") if history else None,
            "last_field":   (history or [{}])[0].get("field_name")  if history else None,
        },
    }
