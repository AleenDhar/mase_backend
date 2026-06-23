#!/usr/bin/env python3
"""
Parallel, NEWEST-FIRST backfill: Avoma meetings + transcripts + AI notes -> the
`datalake` Supabase project. Reads config from ../.datalake_secrets.env.

Strategy: process ONE DAY at a time, today backwards (so the most recent / most
relevant calls land first), with DL_WORKERS days pulled CONCURRENTLY via a thread
pool. Each day is resumable (tracked in avoma_sync_days) so a kill/restart — or a
move to AWS — only re-does in-flight days. Throttled + 429/timeout-aware per request.

Env:  DL_LOOKBACK_DAYS (730)  DL_WORKERS (6)  DL_THROTTLE_S (0.4)
      DL_MAX_DAYS (0=all, else only the N most-recent days — for smoke tests)
      AVOMA_API_TOKEN (falls back to the known org token)
"""
import json, os, ssl, time, threading, urllib.request, urllib.parse, urllib.error
import concurrent.futures
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
SECRETS = os.path.join(os.path.dirname(HERE), ".datalake_secrets.env")


def load_secrets():
    """Config from ../.datalake_secrets.env on the laptop, OR from env vars on AWS
    (where that file is absent and DATALAKE_URL/DATALAKE_SERVICE_KEY are injected)."""
    cfg = {}
    if os.path.exists(SECRETS):
        with open(SECRETS) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    cfg[k.strip()] = v.strip()
    for k in ("DATALAKE_URL", "DATALAKE_SERVICE_KEY"):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    return cfg


CFG = load_secrets()
DL_URL = CFG["DATALAKE_URL"].rstrip("/")
DL_KEY = CFG["DATALAKE_SERVICE_KEY"]
AVOMA_TOKEN = os.environ.get("AVOMA_API_TOKEN", "ifi116h6e8:2p7r6khoxqojr5638sld")
AVOMA_BASE = "https://api.avoma.com/v1"
LOOKBACK_DAYS = int(os.environ.get("DL_LOOKBACK_DAYS", "730"))
WORKERS = int(os.environ.get("DL_WORKERS", "6"))
THROTTLE = float(os.environ.get("DL_THROTTLE_S", "0.4"))
MAX_DAYS = int(os.environ.get("DL_MAX_DAYS", "0"))
MAX_429, MAX_TIMEOUT = 6, 3

# TLS: trust the corp CA bundle (it contains the Zscaler root) so HTTPS to Supabase
# verifies properly behind the corp proxy. On AWS (no Zscaler) the bundle is absent
# and we use the system defaults. Verification is ALWAYS on.
_CA = os.environ.get("DL_CA_BUNDLE", r"C:\Users\Aleen.Dhar\.aws\corp-ca-bundle.pem")
try:
    SSL_CTX = ssl.create_default_context(cafile=_CA) if os.path.exists(_CA) else ssl.create_default_context()
except Exception:  # noqa: BLE001
    SSL_CTX = ssl.create_default_context()

_lock = threading.Lock()
_tot_m = _tot_t = _days_done = 0


def _req(method, url, headers, body=None, timeout=90):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        return r.status, r.read().decode(), dict(r.headers)


def avoma_get(path, params):
    url = AVOMA_BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    headers = {"Authorization": f"Bearer {AVOMA_TOKEN}"}
    rate = tout = 0
    while True:
        try:
            _, raw, _ = _req("GET", url, headers, timeout=60)
            return json.loads(raw)
        except urllib.error.HTTPError as e:
            if e.code == 429 and rate < MAX_429:
                ra = e.headers.get("Retry-After")
                time.sleep(max(0.5, min(float(ra) if ra else min(2 ** rate, 30), 30)))
                rate += 1
                continue
            return {"error": f"HTTP {e.code}"}
        except Exception as e:  # noqa: BLE001
            if tout < MAX_TIMEOUT:
                time.sleep(min(2 ** tout, 30))
                tout += 1
                continue
            return {"error": str(e)}


def supa_upsert(table, rows, on_conflict):
    if not rows:
        return
    url = f"{DL_URL}/rest/v1/{table}?on_conflict={on_conflict}"
    headers = {"apikey": DL_KEY, "Authorization": f"Bearer {DL_KEY}",
               "Content-Type": "application/json",
               "Prefer": "resolution=merge-duplicates,return=minimal"}
    for attempt in range(4):
        try:
            _req("POST", url, headers, rows, timeout=120)
            return
        except urllib.error.HTTPError as e:
            if attempt == 3:
                try:
                    print(f"[UPSERT {table}] HTTP {e.code}: {e.read().decode()[:300]}", flush=True)
                except Exception:
                    print(f"[UPSERT {table}] HTTP {e.code}", flush=True)
            else:
                time.sleep(2)
        except Exception as e:  # noqa: BLE001
            if attempt == 3:
                print(f"[UPSERT {table}] {e}", flush=True)
            else:
                time.sleep(2)


def get_done_days():
    url = f"{DL_URL}/rest/v1/avoma_sync_days?status=eq.done&select=day"
    headers = {"apikey": DL_KEY, "Authorization": f"Bearer {DL_KEY}"}
    try:
        _, raw, _ = _req("GET", url, headers, timeout=30)
        return {r["day"] for r in json.loads(raw)}
    except Exception:
        return set()


def mark_day(day, status, m, t):
    supa_upsert("avoma_sync_days", [{"day": day, "status": status, "meetings": m,
                "transcripts": t, "updated_at": datetime.now(timezone.utc).isoformat()}], "day")


def domain_of(email):
    return email.split("@")[-1].lower() if email and "@" in email else None


def flatten_transcript(tr):
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


def _label(v):
    return v.get("label") if isinstance(v, dict) else v


def meeting_row(m):
    assoc = m.get("crm_associations") or []
    pick = lambda kind: next((a.get("crm_obj_id") for a in assoc
                              if isinstance(a, dict) and a.get("crm_obj_type") == kind), None)
    emails = []
    for a in (m.get("attendees") or []):
        e = a.get("email") if isinstance(a, dict) else None
        if e and "@" in e:
            emails.append(e.strip().lower())
    domains = sorted({d for d in (domain_of(e) for e in emails) if d})
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


def process_day(day):
    """Pull every meeting (+ transcript/insights) for one calendar day (UTC)."""
    ds = day + "T00:00:00Z"
    de = (datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
          + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")
    mark_day(day, "running", 0, 0)
    page, m, t, page_err = 1, 0, 0, 0
    while True:
        d = avoma_get("/meetings/", {"from_date": ds, "to_date": de, "o": "-start_at",
                                     "page": page, "page_size": 100,
                                     "include_crm_associations": "true"})
        time.sleep(THROTTLE)
        if not isinstance(d, dict) or d.get("error"):
            page_err += 1
            if page_err > 3:
                mark_day(day, "error", m, t)
                return (day, m, t, "error")
            time.sleep(8)
            continue
        results = d.get("results") or []
        if not results:
            break
        supa_upsert("avoma_meetings", [meeting_row(x) for x in results if x.get("uuid")], "uuid")
        m += sum(1 for x in results if x.get("uuid"))
        for x in results:
            uuid, tu = x.get("uuid"), x.get("transcription_uuid")
            if not uuid or not (x.get("transcript_ready") or tu or x.get("notes_ready")):
                continue
            if tu:
                tr = avoma_get(f"/transcriptions/{tu}/", {})
                time.sleep(THROTTLE)
                if isinstance(tr, dict) and not tr.get("error"):
                    supa_upsert("avoma_transcripts", [{
                        "meeting_uuid": uuid, "transcription_uuid": tu,
                        "transcript": tr.get("transcript"),
                        "transcript_text": flatten_transcript(tr),
                        "speakers": tr.get("speakers"),
                        "vtt_url": tr.get("transcription_vtt_url")}], "meeting_uuid")
                    t += 1
            ins = avoma_get(f"/meetings/{uuid}/insights/", {})
            time.sleep(THROTTLE)
            if isinstance(ins, dict) and not ins.get("error"):
                notes = ins.get("ai_notes")
                supa_upsert("avoma_insights", [{
                    "meeting_uuid": uuid, "ai_notes": notes,
                    "ai_notes_text": (json.dumps(notes)[:300000] if notes else None),
                    "keywords": ins.get("keywords")}], "meeting_uuid")
        page += 1
    mark_day(day, "done", m, t)
    return (day, m, t, "done")


def main():
    today = datetime.now(timezone.utc).date()
    days = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(LOOKBACK_DAYS)]
    done = get_done_days()
    todo = [d for d in days if d not in done]          # already newest-first
    if MAX_DAYS:
        todo = todo[:MAX_DAYS]
    print(f"[BACKFILL] newest-first | {len(todo)} days to do ({len(done)} done) "
          f"| workers={WORKERS} throttle={THROTTLE}s", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(process_day, d): d for d in todo}
        for fut in concurrent.futures.as_completed(futs):
            try:
                day, m, t, status = fut.result()
            except Exception as e:  # noqa: BLE001
                print(f"[BACKFILL] day {futs[fut]} crashed: {e}", flush=True)
                continue
            global _tot_m, _tot_t, _days_done
            with _lock:
                _tot_m += m
                _tot_t += t
                _days_done += 1
                dd = _days_done
            print(f"[BACKFILL] {day} {status} m={m} t={t} | "
                  f"days {dd}/{len(todo)} totals m={_tot_m} t={_tot_t}", flush=True)
    print(f"[BACKFILL] COMPLETE. days={_days_done} meetings={_tot_m} transcripts={_tot_t}", flush=True)


if __name__ == "__main__":
    main()
