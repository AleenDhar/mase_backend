"""1) Activate every real deal (active=true, skip TEST_ rows).
   2) Hard-refresh the whole book from Salesforce so ALL deals get their details.
   3) Verify + report."""
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


def counts():
    def n(params):
        r = requests.get(f"{SB}/rest/v1/deal_records", params=params,
                         headers={**SH, "Prefer": "count=exact", "Range": "0-0"},
                         verify=False, timeout=60)
        cr = r.headers.get("Content-Range", "*/0")
        return int(cr.split("/")[-1]) if "/" in cr else 0
    total = n({"select": "opp_id"})
    active = n({"select": "opp_id", "active": "is.true"})
    inactive = n({"select": "opp_id", "active": "is.false"})
    return total, active, inactive


def amt_health():
    r = requests.get(f"{SB}/rest/v1/deal_records",
                     params={"select": "amt:record->hard->amount"},
                     headers=SH, verify=False, timeout=90).json()
    def num(v):
        try:
            return float(v)
        except Exception:
            return None
    nz = sum(1 for x in r if num(x.get("amt")) not in (None, 0.0))
    return len(r), nz


tot, act, inact = counts()
print(f"[{ts()}] BEFORE: total={tot} active={act} inactive={inact}", flush=True)

# ---- STEP 1: activate every inactive REAL deal (skip TEST_ rows) ----
inact_rows = requests.get(f"{SB}/rest/v1/deal_records",
                          params={"select": "opp_id", "active": "is.false"},
                          headers=SH, verify=False, timeout=90).json()
real_ids = [x["opp_id"] for x in inact_rows
            if x.get("opp_id") and not str(x["opp_id"]).upper().startswith("TEST")]
print(f"[{ts()}] activating {len(real_ids)} inactive real deals "
      f"(skipping {len(inact_rows) - len(real_ids)} TEST rows)…", flush=True)
patched = 0
for i in range(0, len(real_ids), 80):
    chunk = real_ids[i:i + 80]
    lst = ",".join('"' + c + '"' for c in chunk)
    r = requests.patch(f"{SB}/rest/v1/deal_records",
                       params={"opp_id": f"in.({lst})"},
                       headers={**SH, "Content-Type": "application/json", "Prefer": "return=minimal"},
                       json={"active": True}, verify=False, timeout=90)
    if r.status_code < 300:
        patched += len(chunk)
    else:
        print(f"[{ts()}] PATCH chunk HTTP {r.status_code}: {r.text[:200]}", flush=True)
print(f"[{ts()}] activated {patched} deals", flush=True)

tot, act, inact = counts()
print(f"[{ts()}] AFTER ACTIVATE: total={tot} active={act} inactive={inact}", flush=True)

# ---- STEP 2: hard-refresh the whole (now-active) book from Salesforce ----
result = {"summary": None, "err": None}


def fire():
    try:
        rr = requests.post(f"{API}/api/deal-engine/hard-refresh", headers=AH,
                           json={"delete_initial_interest": False}, verify=False,
                           timeout=(10, 1800))
        result["summary"] = rr.json() if rr.status_code < 300 else f"HTTP {rr.status_code}: {rr.text[:300]}"
    except Exception as e:  # noqa: BLE001
        result["err"] = f"{type(e).__name__}: {e}"


print(f"[{ts()}] firing whole-book hard-refresh…", flush=True)
th = threading.Thread(target=fire, daemon=True)
th.start()
t0 = time.time()
while time.time() - t0 < 1900:
    time.sleep(20)
    try:
        st = requests.get(f"{API}/api/deal-engine/hard-refresh/status", headers=AH,
                          verify=False, timeout=(10, 40)).json()
    except Exception as e:  # noqa: BLE001
        print(f"[{ts()}] status poll err {e}", flush=True)
        continue
    last = st.get("last") or {}
    print(f"[{ts()}] running={st.get('running')} matched={last.get('matched')} "
          f"updated={last.get('updated')} unmatched={last.get('unmatched')} "
          f"removed={last.get('removed')} failed={last.get('failed')}", flush=True)
    if result["summary"] is not None or result["err"] is not None:
        break
    if st.get("running") is False and last.get("finished_at"):
        time.sleep(3)
        break

print(f"\n[{ts()}] hard-refresh summary: {result['summary']}", flush=True)
if result["err"]:
    print(f"[{ts()}] POST note (server may still have finished): {result['err']}", flush=True)

nrec, nz = amt_health()
tot, act, inact = counts()
print(f"[{ts()}] FINAL: total={tot} active={act} inactive={inact} | records_with_amount={nz}/{nrec}",
      flush=True)
print("ACTIVATE-REFRESH-DONE")
