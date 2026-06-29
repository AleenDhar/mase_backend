"""
Real-time sync of a single Avoma meeting (header + transcript + AI notes) into the
`datalake` Supabase project. Called from the /webhook Notification handler when an
Avoma AINOTE event fires (transcript + notes are ready by then). Best-effort and
self-contained: no-ops unless DATALAKE_URL / DATALAKE_SERVICE_KEY / AVOMA_API_TOKEN
are configured, and never raises into the webhook.

Mirrors scripts/datalake_backfill.py's row shape so backfill + live-sync write
identical rows.
"""
import os
import json
import asyncio

import httpx

DL_URL = os.getenv("DATALAKE_URL", "").rstrip("/")
DL_KEY = os.getenv("DATALAKE_SERVICE_KEY", "")
AVOMA_TOKEN = os.getenv("AVOMA_API_TOKEN", "ifi116h6e8:2p7r6khoxqojr5638sld")
AVOMA_BASE = os.getenv("AVOMA_API_BASE", "https://api.avoma.com/v1").rstrip("/")
# The MASE app DB (deal-intelligence) holds the tracked-opp list (deal_records).
MASE_SB_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
MASE_SB_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SERVICE_KEY", "")
_TIMEOUT = float(os.getenv("DATALAKE_SYNC_TIMEOUT_S", "45"))
_MAX_429 = int(os.getenv("AVOMA_MAX_429_RETRIES", "5"))

ENABLED = bool(DL_URL and DL_KEY and AVOMA_TOKEN)


async def _avoma_get(client: httpx.AsyncClient, path: str, params: dict | None = None):
    """Avoma GET with 429/Retry-After backoff. Returns parsed JSON or {'error': ...}."""
    url = AVOMA_BASE + path
    headers = {"Authorization": f"Bearer {AVOMA_TOKEN}"}
    rate = 0
    while True:
        try:
            r = await client.get(url, headers=headers, params=params or {})
            if r.status_code == 429 and rate < _MAX_429:
                ra = r.headers.get("Retry-After")
                delay = max(0.5, min(float(ra) if ra else min(2 ** rate, 30), 30))
                rate += 1
                await asyncio.sleep(delay)
                continue
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            if code in (401, 403):
                print(f"[DATALAKE-SYNC] AVOMA AUTH FAILED ({code}) — the Avoma token is "
                      "missing/expired; live sync is dead until it is fixed.", flush=True)
            return {"error": f"HTTP {code}"}
        except Exception as e:  # noqa: BLE001
            return {"error": f"{type(e).__name__}: {e}"}


async def _supa_upsert(client: httpx.AsyncClient, table: str, rows: list, on_conflict: str):
    if not rows:
        return
    url = f"{DL_URL}/rest/v1/{table}?on_conflict={on_conflict}"
    headers = {"apikey": DL_KEY, "Authorization": f"Bearer {DL_KEY}",
               "Content-Type": "application/json",
               "Prefer": "resolution=merge-duplicates,return=minimal"}
    try:
        r = await client.post(url, headers=headers, json=rows)
        if r.status_code >= 300:
            print(f"[DATALAKE-SYNC] upsert {table} HTTP {r.status_code}: {r.text[:200]}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[DATALAKE-SYNC] upsert {table} failed: {e}", flush=True)


def _domain_of(email):
    return email.split("@")[-1].lower() if email and "@" in email else None


def _label(v):
    return v.get("label") if isinstance(v, dict) else v


def _flatten_transcript(tr):
    t = tr.get("transcript") if isinstance(tr, dict) else None
    if isinstance(t, list):
        out = []
        for seg in t:
            if isinstance(seg, dict):
                spk = seg.get("speaker") or seg.get("speaker_id") or ""
                txt = seg.get("transcript") or seg.get("text") or ""
                out.append((f"{spk}: " if spk else "") + str(txt))
        return "\n".join(out)
    return json.dumps(t if t is not None else tr)[:300000]


def _meeting_row(m: dict) -> dict:
    assoc = m.get("crm_associations") or []
    def pick(kind):
        return next((a.get("crm_obj_id") for a in assoc
                     if isinstance(a, dict) and a.get("crm_obj_type") == kind), None)
    emails = []
    for a in (m.get("attendees") or []):
        e = a.get("email") if isinstance(a, dict) else None
        if e and "@" in e:
            emails.append(e.strip().lower())
    domains = sorted({d for d in (_domain_of(e) for e in emails) if d})
    return {
        "uuid": m.get("uuid"), "subject": m.get("subject"),
        "start_at": m.get("start_at"), "end_at": m.get("end_at"),
        "duration": m.get("duration"), "state": m.get("state"),
        "recording_state": m.get("recording_state"),
        "transcript_ready": m.get("transcript_ready"), "notes_ready": m.get("notes_ready"),
        "is_call": m.get("is_call"), "is_internal": m.get("is_internal"),
        "organizer_email": m.get("organizer_email"), "attendees": m.get("attendees"),
        "attendee_emails": emails, "attendee_domains": domains,
        "crm_opportunity_id": pick("oppo"), "crm_account_id": pick("account"),
        "crm_contact_ids": [a.get("crm_obj_id") for a in assoc
                            if isinstance(a, dict) and a.get("crm_obj_type") == "contact"],
        "purpose": _label(m.get("purpose")), "outcome": _label(m.get("outcome")),
        "url": m.get("url"), "created": m.get("created"), "modified": m.get("modified"),
        "raw": m,
    }


async def _is_tracked_opp(client: httpx.AsyncClient, opp_id) -> bool:
    """True iff this opp (normalised to 15-char) is in the deal-engine tracked list
    (deal_records in the MASE app DB). Fail-CLOSED: returns False on a missing opp,
    missing MASE-DB config, or any error — so the webhook never stores a call for an
    untracked opp."""
    if not opp_id or not MASE_SB_URL or not MASE_SB_KEY:
        return False
    opp15 = str(opp_id)[:15]
    url = f"{MASE_SB_URL}/rest/v1/deal_records?opp_id=eq.{opp15}&select=opp_id&limit=1"
    try:
        r = await client.get(url, headers={"apikey": MASE_SB_KEY,
                                           "Authorization": f"Bearer {MASE_SB_KEY}"})
        return r.status_code == 200 and bool(r.json())
    except Exception:  # noqa: BLE001 — fail closed
        return False


async def sync_meeting(meeting_uuid: str) -> None:
    """Fetch one meeting + its transcript + AI notes from Avoma and upsert into the
    datalake. Best-effort; never raises."""
    if not ENABLED or not meeting_uuid:
        return
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            m = await _avoma_get(client, f"/meetings/{meeting_uuid}/",
                                 {"include_crm_associations": "true"})
            if not isinstance(m, dict) or m.get("error") or not m.get("uuid"):
                print(f"[DATALAKE-SYNC] meeting {meeting_uuid} fetch failed: "
                      f"{m.get('error') if isinstance(m, dict) else m}", flush=True)
                return
            # TRACKED-OPP GATE — only store calls whose opp is in the tracked list.
            opp_id = next((a.get("crm_obj_id") for a in (m.get("crm_associations") or [])
                           if isinstance(a, dict) and a.get("crm_obj_type") == "oppo"), None)
            if not await _is_tracked_opp(client, opp_id):
                print(f"[DATALAKE-SYNC] meeting {meeting_uuid} opp={opp_id} not tracked — skip", flush=True)
                return
            await _supa_upsert(client, "avoma_meetings", [_meeting_row(m)], "uuid")
            tu = m.get("transcription_uuid")
            if tu:
                tr = await _avoma_get(client, f"/transcriptions/{tu}/")
                if isinstance(tr, dict) and not tr.get("error"):
                    await _supa_upsert(client, "avoma_transcripts", [{
                        "meeting_uuid": meeting_uuid, "transcription_uuid": tu,
                        "transcript": tr.get("transcript"),
                        "transcript_text": _flatten_transcript(tr),
                        "speakers": tr.get("speakers"),
                        "vtt_url": tr.get("transcription_vtt_url")}], "meeting_uuid")
            ins = await _avoma_get(client, f"/meetings/{meeting_uuid}/insights/")
            if isinstance(ins, dict) and not ins.get("error"):
                notes = ins.get("ai_notes")
                await _supa_upsert(client, "avoma_insights", [{
                    "meeting_uuid": meeting_uuid, "ai_notes": notes,
                    "ai_notes_text": (json.dumps(notes)[:300000] if notes else None),
                    "keywords": ins.get("keywords")}], "meeting_uuid")
            print(f"[DATALAKE-SYNC] synced meeting {meeting_uuid} "
                  f"(transcript={'y' if tu else 'n'})", flush=True)
    except Exception as e:  # noqa: BLE001 — never raise into the webhook
        print(f"[DATALAKE-SYNC] sync_meeting {meeting_uuid} crashed: {e}", flush=True)
