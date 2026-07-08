"""EVAL — sweep the 11-deal eval set under the CURRENTLY LOCKED mom version and
export a CSV once every record carries the expected provenance stamp.

Usage: python eval_run_batch.py <tag> <expected_mom_version> [--no-wait] [--no-csv]
  tag: strict | loose | restore  ->  Desktop\\eval_<tag>.csv

Waits ~6 min after start (worker studio-TTL) unless --no-wait, enqueues all deals,
polls light fields until updated_at > enqueue time AND stamp matches, re-enqueues
(once) any deal that swept under the wrong version, then exports the CSV built from
the swept DB records (scores exactly as production wrote them).
Writes NOTHING to the DB — the only mutation is the sanctioned sweep enqueue."""
import csv, datetime, json, os, sys, time
import requests, urllib3
import deal_engine_cro as CRO
from daily_summary.common import load_secret, VERIFY
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TAG = sys.argv[1]
EXPECT = sys.argv[2]
NO_WAIT = "--no-wait" in sys.argv
NO_CSV = "--no-csv" in sys.argv
DEALS_PATH = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--deals=")), "eval_deals.json")
OUT = os.path.join(os.path.expanduser("~"), "Desktop", f"eval_{TAG}.csv")
DEALS = json.load(open(DEALS_PATH))
IDS = [d["opp_id"] for d in DEALS]
BASELINE = {d["opp_id"]: d for d in DEALS}

ENV = r"C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local"
cfg = {}
for line in open(ENV, encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.strip()
API = cfg["DEAL_ENGINE_API_BASE"].rstrip("/")
AH = {"Authorization": f"Bearer {cfg['DEAL_ENGINE_TOKEN']}", "Content-Type": "application/json"}
sec = load_secret(); SB = sec["SUPABASE_URL"].rstrip("/")
key = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
SH = {"apikey": key, "Authorization": f"Bearer {key}"}


def now():
    return datetime.datetime.now(datetime.timezone.utc)


def enqueue(oid):
    r = requests.post(f"{API}/api/deal-engine/sweep/rerun", headers=AH, verify=False,
                      json={"opp_id": oid}, timeout=60)
    return r.status_code


def poll_light():
    r = requests.get(f"{SB}/rest/v1/deal_records",
                     params={"select": "opp_id,account_name,updated_at,"
                                       "record->ai->scoring_studio->versions->>mom",
                             "opp_id": f"in.({','.join(IDS)})"},
                     headers=SH, verify=VERIFY, timeout=90)
    return {x["opp_id"]: x for x in r.json()}


if not NO_WAIT:
    print(f"[{now():%H:%M:%S}] waiting 360s for the worker's studio-TTL to adopt v{EXPECT} ...", flush=True)
    time.sleep(360)

t0 = now()
print(f"[{t0:%H:%M:%S}] enqueueing {len(IDS)} sweeps (expect mom v{EXPECT})", flush=True)
for oid in IDS:
    print(f"  enqueue {oid} -> {enqueue(oid)}", flush=True)

enq_at = {oid: t0 for oid in IDS}
requeued = {}          # oid -> retry count (rogue Replit consumer may steal claims)
MAX_REQUEUE = 3
done = {}
DEADLINE = time.time() + 100 * 60
while time.time() < DEADLINE and len(done) < len(IDS):
    time.sleep(75)
    try:
        st = poll_light()
    except Exception as e:  # noqa: BLE001
        print(f"[poll error: {e}]", flush=True)
        continue
    lines = []
    for oid in IDS:
        if oid in done:
            continue
        x = st.get(oid)
        if not x:
            continue
        upd = datetime.datetime.fromisoformat(x["updated_at"].replace("Z", "+00:00"))
        momv = x.get("mom")
        if upd > enq_at[oid]:
            if momv == EXPECT:
                done[oid] = x["updated_at"]
                lines.append(f"DONE  {x['account_name'][:30]:30s} mom v{momv} at {x['updated_at'][:19]}")
            elif requeued.get(oid, 0) < MAX_REQUEUE:
                requeued[oid] = requeued.get(oid, 0) + 1
                enq_at[oid] = now()
                enqueue(oid)
                lines.append(f"RETRY {x['account_name'][:30]:30s} swept v{momv} != v{EXPECT} "
                             f"(stolen/stale, attempt {requeued[oid]}/{MAX_REQUEUE}) — re-enqueued")
            else:
                done[oid] = x["updated_at"]  # accept; flagged in CSV via mom_version_stamp
                lines.append(f"WARN  {x['account_name'][:30]:30s} still v{momv} after "
                             f"{MAX_REQUEUE} retries — accepting (flagged)")
    print(f"[{now():%H:%M:%S}] {len(done)}/{len(IDS)} done"
          + ("\n  " + "\n  ".join(lines) if lines else ""), flush=True)

print(f"[{now():%H:%M:%S}] sweep phase over: {len(done)}/{len(IDS)} completed", flush=True)
if NO_CSV:
    print("(--no-csv: restore sweep only, skipping export)")
    sys.exit(0)


def bullets(panel, key_):
    for b in (panel or {}).get("blocks") or []:
        if b.get("key") == key_:
            out = []
            for bl in b.get("bullets") or []:
                tone = "OK" if bl.get("tone") == "good" else "WARN"
                out.append(f"[{tone}] {bl.get('text') or ''}")
            return " || ".join(out), (b.get("summary") or b.get("read") or "")
    return "", ""


rows = requests.get(f"{SB}/rest/v1/deal_records",
                    params={"select": "opp_id,account_name,opp_name,stage,forecast_category,"
                                      "amount,close_date,updated_at,record",
                            "opp_id": f"in.({','.join(IDS)})"},
                    headers=SH, verify=VERIFY, timeout=180).json()
recs = []
for r in rows:
    rec = r.get("record") or {}
    ai = rec.get("ai") or {}
    hl = (ai.get("deal_scores") or {}).get("headline") or {}
    stamp = ((ai.get("scoring_studio") or {}).get("versions") or {})
    try:
        panel = CRO.build_cro_panel(rec) or {}
    except Exception as e:  # noqa: BLE001
        panel = {}
        print(f"panel error {r['opp_id']}: {e}")
    win_r, win_s = bullets(panel, "win_position")
    mom_r, mom_s = bullets(panel, "deal_momentum")
    b = BASELINE.get(r["opp_id"], {})
    recs.append({
        "account": r["account_name"], "opportunity": r["opp_name"], "opp_id": r["opp_id"],
        "stage": r["stage"], "forecast_category": r["forecast_category"],
        "amount": r["amount"], "close_date": r["close_date"],
        "win": hl.get("win_position"), "momentum": hl.get("deal_momentum"),
        "baseline_win_v2": b.get("baseline_win"), "baseline_mom_v2": b.get("baseline_mom"),
        "mom_version_stamp": stamp.get("mom"), "all_version_stamp": json.dumps(stamp),
        "swept_at": r["updated_at"], "completed_in_eval": r["opp_id"] in done,
        "win_read": win_s, "win_reasons": win_r,
        "momentum_read": mom_s, "momentum_reasons": mom_r,
    })
recs.sort(key=lambda x: -(x.get("baseline_mom_v2") or 0))
cols = list(recs[0].keys())
with open(OUT, "w", newline="", encoding="utf-8-sig") as f:
    w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
    w.writeheader(); w.writerows(recs)
print(f"CSV: {OUT} ({len(recs)} deals)")
for x in recs:
    print(f"  {x['account'][:30]:30s} win {x.get('baseline_win_v2')}->{x.get('win')}  "
          f"mom {x.get('baseline_mom_v2')}->{x.get('momentum')}  stamp v{x.get('mom_version_stamp')}")
