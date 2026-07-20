"""Sweep Bosch + the 14 screenshot deals under the newly locked win/mom v10.8.

Route: POST /api/deal-engine/sweep/{opp_id} -> sweep.analyze_one(...) IN-PROCESS on the api.
Never touches sweep_queue, so it cannot reach the stale mase-worker that wrote null scores.

Concurrency 5, NOT 8. Three concurrent sweeps exhausted a 2 GB api this morning (~0.5-0.7 GB
each). The api is 4 GB. Eight would need ~5.4 GB and would OOM — and because the durable-queue
routing is rolled back, an OOM silently loses every in-flight sweep with no run-log row. 5 is
the largest number that fits with headroom. Raising API_MEMORY to 8192 is a deploy, and a deploy
restarts the api and kills exactly the sweeps we're running. So: 5 now, memory bump afterwards.

The synchronous call is killed by the ALB's 60s idle timeout; the sweep continues server-side.
We ignore the read timeout and poll deal_records for the result. Proven on the Bosch/Etex restore.
"""
import csv, sys, time, threading, warnings, datetime
from concurrent.futures import ThreadPoolExecutor
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

DEALS = [
    ("Robert Bosch",       "006P700000PlMpu"),
    ("Temasek",            "006P700000BV2eA"),
    ("Techtronic",         "006P700000GWfrf"),
    ("SAMI",               "006P700000RD9Ir"),
    ("Wheelson/Mumtalakat", "006P700000VlPdp"),
    ("Mair Group",         "006P700000PtQGP"),
    ("MTR Corporation",    "006P700000KTTO5"),
    ("Khansaheb",          "006P700000LtIUv"),
    ("HAECO",              "006P700000NwbBd"),
    ("Globe Telecom",      "006P7000008hZHF"),
    ("Gamuda",             "006P700000Q15OU"),
    ("Cebu Pacific Air",   "0066700000wdNe1"),
    ("Bandhan Bank",       "006P700000H55TV"),
    ("Arabian Industries", "006P700000QvP7Z"),
    ("ACEN",               "006P700000DkWgX"),   # REGRESSION GUARD: must stay ~20 (4.4b gate)
]
CONC = 5
DEADLINE_S = 2400
SEL = ("account_name,stage,updated_at,scores:record->ai->deal_scores,"
       "studio:record->ai->scoring_studio,cov:record->evidence_coverage")
_lock = threading.Lock()


def ts():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")


def say(msg):
    with _lock:
        print(f"[{ts()}] {msg}", flush=True)


def state(oid):
    r = requests.get(f"{SB}/rest/v1/deal_records", params={"select": SEL, "opp_id": f"eq.{oid}"},
                     headers=SH, verify=False, timeout=(10, 60)).json()
    if not r:
        return None
    r = r[0]
    ds = r.get("scores") or {}
    hl = ds.get("headline") or {}
    sv = (r.get("studio") or {}).get("versions") or {}
    return {"upd": r.get("updated_at"), "stage": r.get("stage"),
            "win": hl.get("win_position"), "mom": hl.get("deal_momentum"),
            "commit": hl.get("customer_commitment"), "risk": hl.get("deal_risk"),
            "read": hl.get("read"), "src": ds.get("factor_source"),
            "win_engine": sv.get("win"), "mom_engine": sv.get("mom"),
            "degraded": ds.get("scoring_degraded"),
            "calls_read": (r.get("cov") or {}).get("calls_read"),
            "reasons": ds.get("ai_reasons") or {}}


def mcp_ready():
    try:
        h = requests.get(f"{API}/api/health", verify=False, timeout=(8, 25)).json()
        s = h.get("mcp_servers") or {}
        return (h.get("mcp_tools_loaded") is True
                and str(s.get("salesforce", "")).startswith("ready")
                and str(s.get("avoma", "")).startswith("ready"))
    except Exception:
        return False


RESULTS = {}


def run_one(lbl, oid):
    base = state(oid)
    b_upd = (base or {}).get("upd")
    say(f"→ {lbl:20} start (was win={(base or {}).get('win')} mom={(base or {}).get('mom')})")
    try:
        requests.post(f"{API}/api/deal-engine/sweep/{oid}", headers=AH, json={},
                      verify=False, timeout=(10, 1500))
    except Exception:
        pass                       # ALB idle timeout — sweep continues server-side
    t0 = time.time()
    while time.time() - t0 < DEADLINE_S:
        time.sleep(30)
        try:
            s = state(oid)
        except Exception:
            continue
        if s and s["upd"] != b_upd and s["win"] is not None:
            ok = s["src"] == "ai" and not s["degraded"] and str(s["win_engine"]) == "10.8"
            say(f"✅ {lbl:20} win={s['win']} mom={s['mom']} read={s['read']!r} "
                f"eng=v{s['win_engine']} calls={s['calls_read']} "
                f"{'GOVERNED' if ok else '⚠ CHECK'}   [was {(base or {}).get('win')}/{(base or {}).get('mom')}]")
            RESULTS[lbl] = {"oid": oid, "before": base, "after": s, "mins": int(time.time() - t0) // 60}
            return
    say(f"❌ {lbl:20} TIMEOUT after {DEADLINE_S // 60}m — no record write")
    RESULTS[lbl] = {"oid": oid, "before": base, "after": None, "mins": DEADLINE_S // 60}


if not mcp_ready():
    say("MCP not ready on the api — refusing to fire (a trigger on a cold task dies in 1ms)")
    raise SystemExit(1)
say(f"MCP ready. Sweeping {len(DEALS)} deals under win/mom v10.8, {CONC} concurrent.\n")

with ThreadPoolExecutor(max_workers=CONC) as ex:
    list(ex.map(lambda d: run_one(*d), DEALS))

print("\n" + "=" * 104)
print(f"{'deal':21}{'stage':22}{'WIN':>10}{'MOM':>10}  {'read':<18}{'eng':>6}{'calls':>6}")
print("=" * 104)
for lbl, oid in DEALS:
    r = RESULTS.get(lbl)
    if not r or not r["after"]:
        print(f"{lbl:21}{'—':22}{'FAILED':>10}"); continue
    a, b = r["after"], r["before"] or {}
    dw = f"{a['win']}" + (f" ({b.get('win')})" if b.get("win") not in (None, a["win"]) else "")
    dm = f"{a['mom']}" + (f" ({b.get('mom')})" if b.get("mom") not in (None, a["mom"]) else "")
    print(f"{lbl:21}{str(a['stage'])[:21]:22}{dw:>10}{dm:>10}  {str(a['read'])[:17]:<18}"
          f"{'v' + str(a['win_engine']):>6}{str(a['calls_read']):>6}")
print("\n(parenthesis = previous value under v10.7)")

with open("fleet_v108_results.csv", "w", newline="", encoding="utf-8-sig") as fh:
    w = csv.writer(fh)
    w.writerow(["deal", "opp_id", "stage", "win", "momentum", "commitment", "risk", "read",
                "win_engine", "factor_source", "calls_read", "prev_win", "prev_mom", "minutes",
                "win_reasons", "momentum_reasons"])
    for lbl, oid in DEALS:
        r = RESULTS.get(lbl)
        if not r or not r["after"]:
            w.writerow([lbl, oid, "", "FAILED"] + [""] * 12); continue
        a, b = r["after"], r["before"] or {}

        def j(k):
            return " || ".join(f"[{x.get('tone')}] {x.get('text')}" for x in (a["reasons"].get(k) or []))
        w.writerow([lbl, oid, a["stage"], a["win"], a["mom"], a["commit"], a["risk"], a["read"],
                    a["win_engine"], a["src"], a["calls_read"], b.get("win"), b.get("mom"),
                    r["mins"], j("win_position"), j("deal_momentum")])
print("wrote fleet_v108_results.csv")
print("FLEET-DONE")
