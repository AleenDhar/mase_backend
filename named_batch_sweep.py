"""FULL re-sweep of a NAMED list (2026-07-07 user-directed) — every drawer surface refreshed:
scores, reasons, 24h, MEDDPICC, stakeholders, competitive, verdict, footprints, CEO.
Waits for the latest build (all of today's fixes: AI-fit tier, never-clobber persist, gate
first-pass, roster, QA) to be live before triggering, so no run lands on stale code. After the
sweeps: qa_self_heal --apply repairs any component gaps, then the full matrix prints.

Pinned deals (Austrian Post, Bright Horizons) keep their pinned SCORES (sweep carry-forward)
while every other surface refreshes — exactly 'everything on the deal drawer'.
"""
import json, re, subprocess, sys, time
from collections import Counter
import requests, urllib3
from daily_summary.common import load_secret, VERIFY, id15
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = "http://mase-alb-1262623499.ap-south-1.elb.amazonaws.com"
sec = load_secret(); SB = sec["SUPABASE_URL"].rstrip("/")
KEY = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}
TOK = sec.get("DISPATCH_SECRET")

NAMES = ["Publicis Groupe", "Mair Group", "Robert Bosch GmbH", "SAMI", "Global Switch",
         "Consumer Cellular", "ASYAD", "Mizuho Bank", "Austrian Post",
         "Sabic Innovative Plastics", "Bright Horizons Family", "United Overseas Bank",
         "McAfee", "Roivant Sciences", "STANDARD CHARTERED BANK", "S&C Electric", "Alpla",
         "GLOBE TELECOM", "SOUTH AFRICAN REVENUE", "GAMUDA BERHAD", "Perdue Farms", "Sitecore",
         "Amy's Kitchen", "ARUP Laboratories"]


def resolve():
    seq = []
    seen = set()
    for pat in NAMES:
        rows = requests.get(f"{SB}/rest/v1/deal_records",
                            params={"account_name": f"ilike.*{pat.replace(' ', '*')}*", "active": "eq.true",
                                    "select": "opp_id,account_name,stage,amount,record"},
                            headers=H, verify=VERIFY, timeout=40).json()
        if not rows:
            print(f"  ?? NOT FOUND: {pat}", flush=True); continue
        # prefer a record that already has a scored headline; else highest amount
        rows.sort(key=lambda r: (
            0 if (((r.get("record") or {}).get("ai") or {}).get("deal_scores") or {}).get("headline", {}).get("win_position") is not None else 1,
            -float(r.get("amount") or 0)))
        r = rows[0]; oid = id15(r["opp_id"])
        if oid in seen:
            continue
        seen.add(oid); seq.append({"oid": oid, "name": r["account_name"], "stage": r.get("stage")})
    return seq


def wait_deploy(min_rev):
    import subprocess as sp
    aws = r"C:\Program Files\Amazon\AWSCLIV2\aws.exe"
    env = {"PYTHONUTF8": "1", "AWS_CA_BUNDLE": r"C:\Users\Aleen.Dhar\.aws\corp-ca-bundle.pem"}
    import os
    e = {**os.environ, **env}
    stable = 0
    for i in range(40):
        try:
            out = sp.run([aws, "ecs", "describe-services", "--cluster", "mase-cluster", "--services",
                          "mase-worker", "--region", "ap-south-1", "--query",
                          "services[0].{td:taskDefinition,dc:length(deployments),rc:runningCount,desired:desiredCount}",
                          "--output", "json"], capture_output=True, text=True, env=e, timeout=60)
            svc = json.loads(out.stdout or "{}")
            rev = int(str(svc.get("td", "")).split(":")[-1] or 0)
            print(f"  [deploy {i:02d}] rev={rev} dc={svc.get('dc')} run={svc.get('rc')}/{svc.get('desired')} stable={stable}", flush=True)
            if rev >= min_rev and svc.get("dc") == 1 and (svc.get("rc") or 0) >= (svc.get("desired") or 1):
                stable += 1
            else:
                stable = 0
            if stable >= 2:
                print(f"  DEPLOY LIVE rev={rev}", flush=True); return True
        except Exception as ex:
            print("  deploy poll err:", str(ex)[:60], flush=True)
        time.sleep(60)
    return False


def wait_done(ids, max_min):
    inl = "in.(" + ",".join(ids) + ")"
    t0 = time.time()
    while time.time() - t0 < max_min * 60:
        try:
            q = requests.get(f"{SB}/rest/v1/sweep_queue", params={"opp_id": inl, "select": "status"},
                             headers=H, verify=VERIFY, timeout=40).json()
            by = Counter(x.get("status") for x in q); rem = by.get("waiting", 0) + by.get("working", 0)
            print(f"  [{int((time.time()-t0)//60):3d}m] {dict(by)} remaining={rem}", flush=True)
            if rem == 0:
                return True
        except Exception as e:
            print("  poll err:", str(e)[:50], flush=True)
        time.sleep(90)
    print("  TIMEOUT — releasing stuck rows", flush=True)
    requests.patch(f"{SB}/rest/v1/sweep_queue?status=in.(waiting,working)&opp_id={inl}",
                   headers={**H, "Content-Type": "application/json"}, json={"status": "done"}, verify=VERIFY, timeout=40)
    return False


def matrix(seq):
    ids = [s["oid"] for s in seq]
    recs = requests.get(f"{SB}/rest/v1/deal_records", params={"opp_id": "in.(" + ",".join(ids) + ")",
                        "select": "opp_id,account_name,record"}, headers=H, verify=VERIFY, timeout=90).json()
    by = {id15(r["opp_id"]): r for r in recs}
    print("\n=== FULL DRAWER MATRIX ===", flush=True)
    for s in seq:
        r = by.get(s["oid"]); ai = ((r or {}).get("record") or {}).get("ai") or {}
        ds = ai.get("deal_scores") or {}; hl = ds.get("headline") or {}
        pnl = len((ds.get("cro_panel") or {}).get("blocks") or [])
        md = ai.get("meddpicc") or {}; d24 = ai.get("day_summary") or {}
        stk = len(ai.get("stakeholder_map") or ai.get("stakeholders") or [])
        ceo = "Y" if (ai.get("ceo_intervention") or {}).get("needed") else "-"
        print(f"  {s['name'][:26]:26} win={hl.get('win_position')} mom={hl.get('deal_momentum')} "
              f"panel={pnl} medd={len(md) if isinstance(md,dict) else 0} stk={stk} "
              f"24h={'Y' if d24.get('overall') else '-'}({d24.get('source')}) ceo={ceo}", flush=True)


def main():
    seq = resolve()
    ids = [s["oid"] for s in seq]
    print(f"resolved {len(seq)}/{len(NAMES)} named deals", flush=True)
    print(">>> waiting for latest build to settle (all today's fixes)…", flush=True)
    if not wait_deploy(200):
        print("DEPLOY NOT SETTLED — aborting (no sweep started).", flush=True); return
    # clear any stragglers, fresh trigger the whole batch
    requests.patch(f"{SB}/rest/v1/sweep_queue?status=in.(waiting,working)&opp_id=in.(" + ",".join(ids) + ")",
                   headers={**H, "Content-Type": "application/json"}, json={"status": "done"}, verify=VERIFY, timeout=40)
    t = requests.post(f"{BASE}/api/deal-engine/sweep/trigger",
                      headers={"Authorization": f"Bearer {TOK}", "Content-Type": "application/json"},
                      json={"opp_ids": ids}, timeout=60)
    print(f">>> FULL SWEEP triggered for {len(ids)} deals -> HTTP {t.status_code}", flush=True)
    wait_done(ids, 150)
    # component QA self-heal (repairs any missing surface independently)
    print(">>> QA self-heal", flush=True)
    try:
        p = subprocess.run([sys.executable, "qa_self_heal.py", "--apply"], capture_output=True, text=True, timeout=3600)
        for ln in [l for l in (p.stdout or "").splitlines() if l.strip()][-6:]:
            print("  " + ln, flush=True)
    except Exception as e:
        print("  QA failed:", str(e)[:80], flush=True)
    matrix(seq)
    print("NAMED BATCH COMPLETE", flush=True)


if __name__ == "__main__":
    main()
