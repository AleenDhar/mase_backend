"""FAN-OUT the remaining 5 forecasted deals (NORTHPORT handled by the canary) — fired all at
once into the durable sweep_queue (9b3e003), drained in parallel by the autoscaling worker
fleet (MAX=8 x concurrency 8). Only run AFTER canary.py PASSES.

Per deal: poll deal_records until updated_at advances AND deal_scores is non-null (or timeout),
capture the deal_trigger_runs provenance (src/model/violations), then run qa_live.py. Prints a
scorecard: live win/mom/commit/risk vs the local CSV, factor_source, engine version, QA accuracy.

No AWS; creds from frontend/.env.local.
  python fanout.py            # fire all 5
  python fanout.py watch      # read-only: poll+QA whatever is already running (no new triggers)
"""
import csv, subprocess, sys, time, re, warnings
warnings.filterwarnings("ignore")
import requests, urllib3
urllib3.disable_warnings()
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ENV = r"C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local"
cfg = {}
for line in open(ENV, encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.strip()
SB = cfg["NEXT_PUBLIC_SUPABASE_URL"].rstrip("/")
KEY = cfg["SUPABASE_SERVICE_ROLE_KEY"]
SH = {"apikey": KEY, "Authorization": f"Bearer {KEY}", "Accept": "application/json"}
API = cfg["DEAL_ENGINE_API_BASE"].rstrip("/")
AH = {"Authorization": f"Bearer {cfg['DEAL_ENGINE_TOKEN']}", "Content-Type": "application/json"}

FIVE = [("SAMI", "006P700000RD9Ir"), ("Allstate", "006P7000006uKrq"),
        ("Robert Bosch", "006P700000PlMpu"), ("Domino's Pizza", "006P700000X6hvK"),
        ("Greencore", "006P700000WeRX8")]
TIMEOUT_S = 1800
POLL_S = 30
WATCH_ONLY = len(sys.argv) > 1 and sys.argv[1] == "watch"

LOCAL = {}
try:
    for r in csv.DictReader(open("cc_fleet_results.csv", encoding="utf-8-sig")):
        LOCAL[r["opp_id"]] = (r["win"], r["momentum"], r["commitment"], r["risk"])
except Exception:
    pass


def dr(oid):
    r = requests.get(f"{SB}/rest/v1/deal_records",
                     params={"select": "updated_at,scores:record->ai->deal_scores,"
                                        "studio:record->ai->scoring_studio", "opp_id": f"eq.{oid}"},
                     headers=SH, verify=False, timeout=(10, 45))
    j = r.json()
    return j[0] if isinstance(j, list) and j else None


def run_of(oid):
    r = requests.get(f"{SB}/rest/v1/deal_trigger_runs",
                     params={"select": "status,source,model,duration_ms,validation_violations,created_at",
                             "opp_id": f"eq.{oid}", "order": "created_at.desc", "limit": "1"},
                     headers=SH, verify=False, timeout=(10, 45))
    j = r.json()
    return j[0] if isinstance(j, list) and j else None


def trigger(oid):
    r = requests.post(f"{API}/api/deal-engine/sweep/trigger", headers=AH,
                      json={"opp_id": oid, "source": "manual"}, verify=False, timeout=60)
    return r.status_code, r.text[:120]


inflight = {}
for label, oid in FIVE:
    base = dr(oid)
    if WATCH_ONLY:
        code = "watch"
    else:
        code, body = trigger(oid)
        print(f"[trigger] {label:14} {oid} -> HTTP {code} {body if isinstance(code,int) and code>=300 else ''}",
              flush=True)
    inflight[oid] = {"label": label, "base_upd": (base or {}).get("updated_at"), "t0": time.time()}
    if not WATCH_ONLY:
        time.sleep(2)

print(f"\n[watch] {len(inflight)} deal(s), polling every {POLL_S}s (timeout {TIMEOUT_S//60}m)\n", flush=True)
results = []
while inflight:
    time.sleep(POLL_S)
    for oid in list(inflight):
        st = inflight[oid]
        rec = dr(oid)
        age = int(time.time() - st["t0"])
        adv = rec and rec.get("updated_at") != st["base_upd"]
        has = bool(rec and rec.get("scores"))
        if adv and has:
            ds = rec["scores"]; hl = ds.get("headline") or {}
            sv = (rec.get("studio") or {}).get("versions") or {}
            run = run_of(oid)
            print(f"\n[done {age//60}m{age%60:02d}s] {st['label']} — win={hl.get('win_position')} "
                  f"mom={hl.get('deal_momentum')} commit={hl.get('customer_commitment')} "
                  f"risk={hl.get('deal_risk')} src={ds.get('factor_source')} winEng=v{sv.get('win')} "
                  f"| run src={run.get('source') if run else '?'} model={run.get('model') if run else '?'} "
                  f"viol={run.get('validation_violations') if run else '?'}", flush=True)
            p = subprocess.run([sys.executable, "qa_live.py", oid, st["label"]],
                               capture_output=True, text=True, timeout=240)
            out = (p.stdout or "") + (p.stderr or "")
            m = re.search(r"PASS (\d+) / FAIL (\d+) / WARN (\d+)\s+->\s+accuracy (\d+)%", out)
            print(out, flush=True)
            results.append({"label": st["label"], "oid": oid, "sec": age,
                            "win": hl.get("win_position"), "mom": hl.get("deal_momentum"),
                            "commit": hl.get("customer_commitment"), "risk": hl.get("deal_risk"),
                            "src": ds.get("factor_source"), "winEng": sv.get("win"),
                            "acc": (m.group(4)+"%") if m else "?",
                            "pfw": f"{m.group(1)}/{m.group(2)}/{m.group(3)}" if m else "?"})
            del inflight[oid]
        elif age > TIMEOUT_S:
            print(f"[TIMEOUT] {st['label']} after {age//60}m (adv={adv} scores={has})", flush=True)
            results.append({"label": st["label"], "oid": oid, "sec": age, "acc": "TIMEOUT", "pfw": "-"})
            del inflight[oid]
        else:
            print(f"  … {st['label']:14} {age//60}m{age%60:02d}s (adv={adv} scores={has})", flush=True)

print("\n" + "=" * 104, flush=True)
print("FAN-OUT SCORECARD (live cloud v10.8 vs local CSV v10.7)", flush=True)
print("=" * 104, flush=True)
print(f"{'deal':15}{'live w/m/c/r':>20}{'local w/m/c/r':>20}{'src':>6}{'eng':>6}{'min':>5}{'QA':>7}  P/F/W", flush=True)
for r in sorted(results, key=lambda x: x["label"]):
    lw = LOCAL.get(r["oid"], ("-", "-", "-", "-"))
    live = f"{r.get('win')}/{r.get('mom')}/{r.get('commit')}/{r.get('risk')}"
    loc = f"{lw[0]}/{lw[1]}/{lw[2]}/{lw[3]}"
    print(f"{r['label']:15}{live:>20}{loc:>20}{str(r.get('src')):>6}{'v'+str(r.get('winEng')):>6}"
          f"{r['sec']//60:>5}{r.get('acc',''):>7}  {r.get('pfw','')}", flush=True)
