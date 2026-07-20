"""ONE clean controlled test: clear stuck queue rows, run a single IN-PROCESS sweep on Bandhan
(POST /sweep/{oid} -> analyze_one direct, no queue/worker), verify deal_scores come back."""
import warnings, time, datetime, threading
warnings.filterwarnings("ignore")
import requests, urllib3
urllib3.disable_warnings()
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

cfg = {}
for _l in open(r"C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local", encoding="utf-8"):
    _l = _l.strip()
    if _l and not _l.startswith("#") and "=" in _l:
        k, v = _l.split("=", 1)
        cfg[k.strip()] = v.strip().strip('"').strip("'")
API = cfg["DEAL_ENGINE_API_BASE"].rstrip("/")
AH = {"Authorization": f"Bearer {cfg['DEAL_ENGINE_TOKEN']}", "Content-Type": "application/json"}
SB = cfg["NEXT_PUBLIC_SUPABASE_URL"].rstrip("/")
K = cfg["SUPABASE_SERVICE_ROLE_KEY"]
SH = {"apikey": K, "Authorization": f"Bearer {K}"}
OID = "006P700000H55TV"


def ts():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")


for o in ("006P700000H55TV", "006P700000QFJwD"):
    requests.patch(SB + "/rest/v1/sweep_queue",
                   params={"opp_id": f"eq.{o}", "status": "in.(waiting,working)"},
                   headers={**SH, "Content-Type": "application/json", "Prefer": "return=minimal"},
                   json={"status": "failed", "error": "operator: clear before in-process test"},
                   verify=False, timeout=60)
print(f"[{ts()}] cleared stuck queue rows", flush=True)


def rec():
    r = requests.get(SB + "/rest/v1/deal_records",
                     params={"select": "updated_at,scores:record->ai->deal_scores,studio:record->ai->scoring_studio",
                             "opp_id": f"eq.{OID}"}, headers=SH, verify=False, timeout=60).json()[0]
    ds = r.get("scores") or {}
    hl = ds.get("headline") or {}
    sv = (r.get("studio") or {}).get("versions") or {}
    return r["updated_at"], bool(ds), hl.get("win_position"), hl.get("deal_momentum"), ds.get("factor_source"), sv.get("win")


bu = rec()[0]
print(f"[{ts()}] firing IN-PROCESS sweep POST /sweep/{OID}", flush=True)
threading.Thread(target=lambda: requests.post(f"{API}/api/deal-engine/sweep/{OID}", headers=AH,
                 json={}, verify=False, timeout=(10, 1500)), daemon=True).start()
t0 = time.time()
while time.time() - t0 < 1500:
    time.sleep(40)
    up, present, win, mom, src, eng = rec()
    if up != bu:
        verdict = "IN-PROCESS WORKS" if (present and win is not None) else "IN-PROCESS ALSO BROKEN - deeper bug"
        print(f"[{ts()}] RESULT scores_present={present} win={win} mom={mom} src={src} v{eng}", flush=True)
        print(f"[{ts()}] {verdict}", flush=True)
        break
    print(f"[{ts()}]  ... sweeping ({int(time.time() - t0) // 60}m)", flush=True)
print("CLEAN-TEST-DONE", flush=True)
