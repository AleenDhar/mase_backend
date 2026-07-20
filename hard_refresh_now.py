"""Restore the blanked SF hard facts (amount/forecast/stage/close_date) across the
whole book by firing the AI-free hard-refresh, then poll status + verify amounts."""
import sys, time, warnings, datetime, threading
warnings.filterwarnings("ignore")
import requests, urllib3
urllib3.disable_warnings()
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


def ts():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")


def amount_health():
    r = requests.get(f"{SB}/rest/v1/deal_records",
                     params={"select": "amt:record->hard->amount"},
                     headers=SH, verify=False, timeout=60).json()
    def num(v):
        try:
            return float(v)
        except Exception:
            return None
    nz = sum(1 for x in r if num(x.get("amt")) not in (None, 0.0))
    nul = sum(1 for x in r if x.get("amt") in (None, ""))
    return len(r), nz, nul


tot, nz, nul = amount_health()
print(f"[{ts()}] BEFORE: {tot} records | non-zero amount={nz} | null amount={nul}", flush=True)

result = {"summary": None, "err": None}


def fire():
    try:
        rr = requests.post(f"{API}/api/deal-engine/hard-refresh", headers=AH,
                           json={"delete_initial_interest": False}, verify=False,
                           timeout=(10, 1800))
        result["summary"] = rr.json() if rr.status_code < 300 else f"HTTP {rr.status_code}: {rr.text[:300]}"
    except Exception as e:  # noqa: BLE001
        result["err"] = f"{type(e).__name__}: {e}"


print(f"[{ts()}] firing whole-book hard-refresh (restore only, no deletes)…", flush=True)
th = threading.Thread(target=fire, daemon=True)
th.start()

# Poll status until it finishes (or the POST returns).
t0 = time.time()
while time.time() - t0 < 1900:
    time.sleep(20)
    try:
        st = requests.get(f"{API}/api/deal-engine/hard-refresh/status", headers=AH,
                          verify=False, timeout=(10, 40)).json()
    except Exception as e:  # noqa: BLE001
        print(f"[{ts()}] status poll err {e}", flush=True)
        continue
    running = st.get("running")
    last = st.get("last") or {}
    print(f"[{ts()}] running={running} last: matched={last.get('matched')} "
          f"updated={last.get('updated')} failed={last.get('failed')} "
          f"unmatched={last.get('unmatched')} finished_at={str(last.get('finished_at'))[:19]}",
          flush=True)
    if result["summary"] is not None or result["err"] is not None:
        break
    if running is False and last.get("finished_at"):
        # give the POST thread a moment to also land
        time.sleep(3)
        break

print(f"\n[{ts()}] POST summary: {result['summary']}", flush=True)
if result["err"]:
    print(f"[{ts()}] POST error (server may still have finished): {result['err']}", flush=True)

tot, nz, nul = amount_health()
print(f"[{ts()}] AFTER: {tot} records | non-zero amount={nz} | null amount={nul}", flush=True)
print("HARD-REFRESH-DONE")
