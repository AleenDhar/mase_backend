"""
Recurring datalake reconciliation — the anti-rot safety net behind the real-time
webhook (datalake_sync.sync_meeting) and the one-shot backfill.

WHY THIS EXISTS: the lake was only ever filled by (1) a manual day-by-day backfill
that marks whole days "done" and never revisits, and (2) the per-event AINOTE
webhook. So any meeting whose transcript/AI-notes became ready AFTER its day was
processed — a header synced before the call, a late transcript, or a missed/failed
webhook event — was left as a content-less header FOREVER (the HAVI 2026-06-23 case).
This module is the recurring pass that heals those automatically.

Two bounded passes, both reusing datalake_sync's helpers/config so the row shapes
are byte-identical to the webhook + backfill:

  1. incremental_window() — re-pull the last RECONCILE_LOOKBACK_DAYS of Avoma
     meetings (newest-first), upsert each header, and fill transcript + insights
     via the DETAIL endpoints for any that are ready. Catches late transcripts and
     missed webhook events within the recent window.

  2. reconcile_null_content() — re-fetch older datalake rows that have a meeting
     header but NO transcript yet (the late-transcript class), prioritising tracked
     opps, so they fill in once Avoma has them.

A run records its outcome in avoma_sync_state (id='reconcile') for observability.
No-op unless the datalake + Avoma are configured; never raises.
"""
from __future__ import annotations

import os
import json
import asyncio
from datetime import datetime, timezone, timedelta

import httpx

import datalake_sync as ds  # reuse identical config + row shaping + Avoma/Supabase IO

AVOMA_BASE = ds.AVOMA_BASE
ENABLED = ds.ENABLED  # requires DATALAKE_URL / DATALAKE_SERVICE_KEY / AVOMA_API_TOKEN

LOOKBACK_DAYS = int(os.getenv("DATALAKE_RECONCILE_LOOKBACK_DAYS", "7"))
NULL_FILL_LIMIT = int(os.getenv("DATALAKE_RECONCILE_NULL_LIMIT", "300"))
PAGE_SIZE = 100
MAX_WINDOW_PAGES = int(os.getenv("DATALAKE_RECONCILE_MAX_PAGES", "40"))


async def _avoma_get_url(client: httpx.AsyncClient, url: str):
    """GET a FULL Avoma url (used for cursor pagination via the `next` link)."""
    rate = 0
    headers = {"Authorization": f"Bearer {ds.AVOMA_TOKEN}"}
    while True:
        try:
            r = await client.get(url, headers=headers)
            if r.status_code == 429 and rate < ds._MAX_429:
                ra = r.headers.get("Retry-After")
                await asyncio.sleep(max(0.5, min(float(ra) if ra else min(2 ** rate, 30), 30)))
                rate += 1
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            return {"error": f"{type(e).__name__}: {e}"}


async def _store_meeting_full(client: httpx.AsyncClient, m: dict) -> bool:
    """Upsert one meeting header + (if available) its transcript + insights, fetched
    from the DETAIL endpoints. Returns True iff a transcript landed. Never raises."""
    uuid = m.get("uuid")
    if not uuid:
        return False
    await ds._supa_upsert(client, "avoma_meetings", [ds._meeting_row(m)], "uuid")
    got = False
    tu = m.get("transcription_uuid")
    if tu:
        tr = await ds._avoma_get(client, f"/transcriptions/{tu}/")
        if isinstance(tr, dict) and not tr.get("error") and tr.get("transcript"):
            await ds._supa_upsert(client, "avoma_transcripts", [{
                "meeting_uuid": uuid, "transcription_uuid": tu,
                "transcript": tr.get("transcript"),
                "transcript_text": ds._flatten_transcript(tr),
                "speakers": tr.get("speakers"),
                "vtt_url": tr.get("transcription_vtt_url")}], "meeting_uuid")
            got = True
    ins = await ds._avoma_get(client, f"/meetings/{uuid}/insights/")
    if isinstance(ins, dict) and not ins.get("error"):
        notes = ins.get("ai_notes")
        await ds._supa_upsert(client, "avoma_insights", [{
            "meeting_uuid": uuid, "ai_notes": notes,
            "ai_notes_text": (json.dumps(notes)[:300000] if notes else None),
            "keywords": ins.get("keywords")}], "meeting_uuid")
    return got


async def _record_state(client: httpx.AsyncClient, **fields):
    row = {"id": "reconcile", "updated_at": datetime.now(timezone.utc).isoformat(), **fields}
    await ds._supa_upsert(client, "avoma_sync_state", [row], "id")


async def incremental_window(client: httpx.AsyncClient) -> dict:
    """Re-pull the last LOOKBACK_DAYS of Avoma meetings (newest-first) and refill
    any that have a transcript/notes ready. Bounded by MAX_WINDOW_PAGES."""
    now = datetime.now(timezone.utc)
    frm = (now - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%dT00:00:00Z")
    to = (now + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")
    d = await ds._avoma_get(client, "/meetings/", {
        "from_date": frm, "to_date": to, "o": "-start_at",
        "page_size": PAGE_SIZE, "include_crm_associations": "true"})
    meetings = transcripts = pages = 0
    while isinstance(d, dict) and not d.get("error"):
        results = d.get("results") or []
        for m in results:
            if not m.get("uuid"):
                continue
            # Only spend the detail calls when content is (or should be) available.
            if m.get("transcript_ready") or m.get("notes_ready") or m.get("transcription_uuid"):
                if await _store_meeting_full(client, m):
                    transcripts += 1
            else:
                await ds._supa_upsert(client, "avoma_meetings", [ds._meeting_row(m)], "uuid")
            meetings += 1
        pages += 1
        nxt = d.get("next")
        if not nxt or pages >= MAX_WINDOW_PAGES:
            break
        d = await _avoma_get_url(client, nxt)
    return {"window_meetings": meetings, "window_transcripts": transcripts, "window_pages": pages}


async def reconcile_null_content(client: httpx.AsyncClient) -> dict:
    """Re-fetch datalake rows that have a header but no transcript yet, prioritising
    tracked opps (crm_opportunity_id present), newest-first. Heals the late-transcript
    class the webhook missed."""
    sel = "uuid,start_at,avoma_transcripts(meeting_uuid)"
    flt = ("avoma_transcripts=is.null&crm_opportunity_id=not.is.null"
           f"&order=start_at.desc&limit={NULL_FILL_LIMIT}")
    url = f"{ds.DL_URL}/rest/v1/avoma_meetings?select={sel}&{flt}"
    try:
        r = await client.get(url, headers={"apikey": ds.DL_KEY,
                                           "Authorization": f"Bearer {ds.DL_KEY}"})
        rows = r.json() if r.status_code < 300 else []
    except Exception as e:  # noqa: BLE001
        print(f"[DATALAKE-RECONCILE] null-content query failed: {e}", flush=True)
        return {"null_scanned": 0, "null_filled": 0}
    if not isinstance(rows, list):
        return {"null_scanned": 0, "null_filled": 0}
    filled = 0
    for row in rows:
        m = await ds._avoma_get(client, f"/meetings/{row['uuid']}/",
                                {"include_crm_associations": "true"})
        if isinstance(m, dict) and not m.get("error") and m.get("uuid"):
            if await _store_meeting_full(client, m):
                filled += 1
    return {"null_scanned": len(rows), "null_filled": filled}


async def run_once(do_null_fill: bool = True) -> dict:
    """One reconciliation cycle. Safe to call on a schedule. Never raises."""
    if not ENABLED:
        print("[DATALAKE-RECONCILE] disabled (DATALAKE_URL/KEY/AVOMA_API_TOKEN not set)", flush=True)
        return {"skipped": True}
    out: dict = {}
    try:
        async with httpx.AsyncClient(timeout=ds._TIMEOUT) as client:
            out.update(await incremental_window(client))
            if do_null_fill:
                out.update(await reconcile_null_content(client))
            await _record_state(client, status="ok", **out)
        print(f"[DATALAKE-RECONCILE] cycle done: {out}", flush=True)
    except Exception as e:  # noqa: BLE001 — a scheduler must survive any single run
        print(f"[DATALAKE-RECONCILE] cycle crashed: {type(e).__name__}: {e}", flush=True)
        out["error"] = f"{type(e).__name__}: {e}"
    return out
