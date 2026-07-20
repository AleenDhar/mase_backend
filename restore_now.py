"""RESTORE Robert Bosch (+ Etex) by re-sweeping IN-PROCESS on the api tier.

POST /api/deal-engine/sweep/{opp_id} calls sweep.analyze_one(...) directly in the request
handler — it never touches sweep_queue and never reaches the stale mase-worker that wrote
`deal_scores = null`. The api container runs the current image (model=claude-sonnet-5,
DEAL_ENGINE_AI_SCORING=true), so this produces a governed Omnivision score.

The call is synchronous and a sweep takes ~10-16 min, so the ALB's 60s idle timeout will
almost certainly kill our HTTP connection. The server-side coroutine keeps running and still
writes deal_records — so we fire, ignore the read timeout, and POLL the row for the result.

NORTHPORT is deliberately EXCLUDED: its sweep_queue row is still `working` on the stale
worker, and that run would race this one and clobber the result.
"""
import sys, time, threading, warnings, datetime
warnings.filterwarnings("ignore")
import requests, urllib3
urllib3.disable_warnings()
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
ENV = r"C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local"
cfg = {}
for _l in open(ENV, encoding="utf-8"):
    _l = _l.strip()
    if _l and not _l.startswith("#") and "=" in _l:
        k, v = _l.split("=", 1)
        cfg[k.strip()] = v.strip().strip('"').strip("'")
API = cfg["DEAL_ENGINE_API_BASE"].rstrip("/")
AH = {"Authorization": f"Bearer {cfg['DEAL_ENGINE_TOKEN']}", "Content-Type": "application/json"}
SB = cfg["NEXT_PUBLIC_SUPABASE_URL"].rstrip("/")
K = cfg["SUPABASE_SERVICE_ROLE_KEY"]
SH = {"apikey": K, "Authorization": f"Bearer {K}"}

TARGETS = [("Robert Bosch", "006P700000PlMpu"), ("Etex Group", "006P700000UGPE5")]
SEL = ("updated_at,scores:record->ai->deal_scores,studio:record->ai->scoring_studio,"
       "cov:record->evidence_coverage")


def ts():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")


def state(oid):
    r = requests.get(f"{SB}/rest/v1/deal_records", params={"select": SEL, "opp_id": f"eq.{oid}"},
                     headers=SH, verify=False, timeout=(10, 60)).json()
    if not r:
        return None
    r = r[0]
    ds = r.get("scores") or {}
    hl = ds.get("headline") or {}
    sv = (r.get("studio") or {}).get("versions") or {}
    return {"upd": r.get("updated_at"), "win": hl.get("win_position"),
            "mom": hl.get("deal_momentum"), "read": hl.get("read"),
            "src": ds.get("factor_source"), "win_engine": sv.get("win"),
            "degraded": ds.get("scoring_degraded"),
            "calls_read": (r.get("cov") or {}).get("calls_read")}


def fire(lbl, oid):
    """Synchronous sweep. The ALB will time us out; the server keeps working."""
    try:
        r = requests.post(f"{API}/api/deal-engine/sweep/{oid}", headers=AH, json={},
                          verify=False, timeout=(10, 1500))
        print(f"[{ts()}] {lbl}: HTTP {r.status_code} {r.text[:160]}", flush=True)
    except Exception as e:
        print(f"[{ts()}] {lbl}: connection closed ({type(e).__name__}) — "
              f"expected; sweep continues server-side", flush=True)


print(f"[{ts()}] baseline (what the UI shows now):", flush=True)
base = {}
for lbl, oid in TARGETS:
    s = state(oid)
    base[oid] = (s or {}).get("upd")
    print(f"    {lbl:14} win={(s or {}).get('win')} mom={(s or {}).get('mom')} "
          f"src={(s or {}).get('src')}", flush=True)

print(f"\n[{ts()}] firing in-process sweeps (bypasses queue + stale worker)…\n", flush=True)
threads = []
for lbl, oid in TARGETS:
    t = threading.Thread(target=fire, args=(lbl, oid), daemon=True)
    t.start(); threads.append(t)
    time.sleep(2)

done = set()
t0 = time.time()
while len(done) < len(TARGETS) and time.time() - t0 < 2100:
    time.sleep(45)
    for lbl, oid in TARGETS:
        if oid in done:
            continue
        try:
            s = state(oid)
        except Exception:
            continue
        if s and s["upd"] != base[oid] and s["win"] is not None:
            ok = s["src"] == "ai" and not s["degraded"] and str(s["win_engine"]) == "10.7"
            print(f"\n[{ts()}] ### {lbl} RESTORED — win={s['win']} mom={s['mom']} "
                  f"read={s['read']!r} src={s['src']} winEng=v{s['win_engine']} "
                  f"calls_read={s['calls_read']} {'✅ GOVERNED' if ok else '⚠ CHECK'}", flush=True)
            done.add(oid)
        else:
            print(f"[{ts()}]  … {lbl} sweeping ({int(time.time()-t0)//60}m)", flush=True)

for lbl, oid in TARGETS:
    if oid not in done:
        print(f"\n[{ts()}] !!! {lbl} did not restore within 35m — investigate", flush=True)
print(f"\n[{ts()}] RESTORE-DONE", flush=True)
