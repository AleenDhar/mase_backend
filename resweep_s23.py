"""S2 + S3 re-sweep runner (2026-07-07, user-directed: "run S2 and S3, don't ask").
Runs AFTER the S1/forecasted pipeline: enqueues S2 (non-forecasted >= Formal Evaluation),
waits, heals scores; then S3 (rest); then restores the intelligent 24h summaries for every
swept deal and prints book QA. Pinned deals keep their scores by design (sweep carry-forward).

The queue is server-side: even if this local monitor dies, the AWS workers finish the sweeps —
heal + day summaries can be re-run idempotently."""
import json, re, subprocess, sys, time
from collections import Counter
import requests, urllib3
import deal_engine_scoring as SC, deal_engine_cro as CRO
from daily_summary.common import load_secret, VERIFY, id15
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = "http://mase-alb-1262623499.ap-south-1.elb.amazonaws.com"
sec = load_secret()
SB = sec["SUPABASE_URL"].rstrip("/")
KEY = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}
TOK = sec.get("DISPATCH_SECRET")
REF = re.search(r"https://([a-z0-9]+)\.supabase\.co", sec["SUPABASE_URL"]).group(1)
MGMT = f"https://api.supabase.com/v1/projects/{REF}/database/query"
MTOK = sec["SUPABASE_ACCESS_TOKEN"]


def drows(params):
    return requests.get(f"{SB}/rest/v1/deal_records", params=params, headers=H, verify=VERIFY, timeout=150).json()


def trigger(ids, chunk=60):
    ok = True
    for i in range(0, len(ids), chunk):
        part = ids[i:i + chunk]
        r = requests.post(f"{BASE}/api/deal-engine/sweep/trigger",
                          headers={"Authorization": f"Bearer {TOK}", "Content-Type": "application/json"},
                          json={"opp_ids": part}, timeout=90)
        print(f"  trigger {i + len(part)}/{len(ids)} -> HTTP {r.status_code}", flush=True)
        ok = ok and r.status_code in (200, 202)
        time.sleep(2)
    return ok


def wait_done(ids, max_min):
    t0 = time.time()
    while time.time() - t0 < max_min * 60:
        try:
            by = Counter()
            for i in range(0, len(ids), 150):
                inl = "in.(" + ",".join(ids[i:i + 150]) + ")"
                q = requests.get(f"{SB}/rest/v1/sweep_queue", params={"opp_id": inl, "select": "status"},
                                 headers=H, verify=VERIFY, timeout=60).json()
                by.update(x.get("status") for x in q)
            rem = by.get("waiting", 0) + by.get("working", 0)
            print(f"  [{int((time.time()-t0)//60):3d}m] {dict(by)} remaining={rem}", flush=True)
            if rem == 0:
                return True
        except Exception as e:
            print("  poll err:", str(e)[:60], flush=True)
        time.sleep(120)
    print(f"  TIMEOUT after {max_min}m — releasing stuck rows and continuing", flush=True)
    for i in range(0, len(ids), 150):
        inl = "in.(" + ",".join(ids[i:i + 150]) + ")"
        try:
            requests.patch(f"{SB}/rest/v1/sweep_queue?status=in.(waiting,working)&opp_id={inl}",
                           headers={**H, "Content-Type": "application/json"}, json={"status": "done"},
                           verify=VERIFY, timeout=60)
        except Exception:
            pass
    return False


def heal_scores(ids):
    out, healed = {}, 0
    for i in range(0, len(ids), 100):
        recs = drows({"opp_id": "in.(" + ",".join(ids[i:i + 100]) + ")", "select": "opp_id,account_name,record"})
        for r in recs:
            oid = id15(r["opp_id"]); rec = r.get("record") or {}
            ds = ((rec.get("ai") or {}).get("deal_scores") or {})
            if (ds.get("headline") or {}).get("win_position") is not None:
                continue
            try:
                sc = SC.compute_deal_scores(rec)
                if not (sc and (sc.get("headline") or {}).get("win_position") is not None):
                    continue
                rec.setdefault("ai", {})["deal_scores"] = sc
                panel = CRO.build_cro_panel(rec)
                if panel:
                    sc["cro_panel"] = panel
                out[oid] = sc; healed += 1
            except Exception as e:
                print(f"  heal FAIL {r.get('account_name')}: {str(e)[:70]}", flush=True)
    if out:
        for i in range(0, len(out), 60):
            part = dict(list(out.items())[i:i + 60])
            blob = json.dumps(part)
            sql = ("update deal_records d set record = jsonb_set(record,'{ai,deal_scores}', m.value, true), "
                   "updated_at = now() from (select key as opp_id, value from jsonb_each($J$" + blob +
                   "$J$::jsonb)) m where d.opp_id = m.opp_id returning d.opp_id")
            requests.post(MGMT, headers={"Authorization": f"Bearer {MTOK}", "Content-Type": "application/json"},
                          json={"query": sql}, verify=VERIFY, timeout=120)
    print(f"  healed {healed} record(s) missing scores", flush=True)


def qa(ids, label):
    scored = ceo = ds_ok = total = 0
    for i in range(0, len(ids), 150):
        recs = drows({"opp_id": "in.(" + ",".join(ids[i:i + 150]) + ")", "select": "record"})
        for r in recs:
            total += 1
            ai = (r.get("record") or {}).get("ai") or {}
            if ((ai.get("deal_scores") or {}).get("headline") or {}).get("win_position") is not None:
                scored += 1
            if (ai.get("ceo_intervention") or {}).get("needed"):
                ceo += 1
            if (ai.get("day_summary") or {}).get("overall"):
                ds_ok += 1
    print(f"{label} QA: {total} deals | scored={scored} | ceo_watch={ceo} | 24h_present={ds_ok}", flush=True)


def main():
    s2 = [id15(x) for x in json.load(open("cc_work/_stage2.json"))]
    s3 = [id15(x) for x in json.load(open("cc_work/_stage3.json"))]
    print(f"S2: {len(s2)} deals | S3: {len(s3)} deals", flush=True)

    for label, ids, mins in (("S2 (non-forecasted >= Formal Evaluation)", s2, 240),
                             ("S3 (rest of book)", s3, 420)):
        print(f"\n>>> {label}: enqueueing {len(ids)}", flush=True)
        if not trigger(ids):
            print("  some triggers failed — continuing with what enqueued", flush=True)
        wait_done(ids, mins)
        heal_scores(ids)
        qa(ids, label)

    everything = s2 + s3
    print("\n>>> restoring intelligent 24h summaries for S2+S3", flush=True)
    for i in range(0, len(everything), 120):
        part = everything[i:i + 120]
        try:
            p = subprocess.run([sys.executable, "day_summary_ai.py", "--ids", ",".join(part), "--apply"],
                               capture_output=True, text=True, timeout=3600)
            tail = [ln for ln in (p.stdout or "").splitlines() if ln.strip()]
            print(f"  batch {i//120+1}: " + (tail[-1] if tail else "?"), flush=True)
        except Exception as e:
            print("  day-summary batch failed:", str(e)[:100], flush=True)

    qa(everything, "\nS2+S3 FINAL")
    print("S2+S3 COMPLETE", flush=True)


if __name__ == "__main__":
    main()
