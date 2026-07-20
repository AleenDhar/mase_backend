"""Read-only: every deal_trigger_runs row since the trigger, for the 5 outstanding deals."""
import sys, warnings, datetime
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
SB = cfg["NEXT_PUBLIC_SUPABASE_URL"].rstrip("/")
SH = {"apikey": cfg["SUPABASE_SERVICE_ROLE_KEY"],
      "Authorization": f"Bearer {cfg['SUPABASE_SERVICE_ROLE_KEY']}"}
API = cfg["DEAL_ENGINE_API_BASE"].rstrip("/")
AH = {"Authorization": f"Bearer {cfg['DEAL_ENGINE_TOKEN']}"}

T0 = "2026-07-09T14:00:45"
PEND = {"006P700000PlMpu": "Robert Bosch", "006P700000QFJwD": "NORTHPORT",
        "006P700000X6hvK": "Domino's", "006P700000WeRX8": "Greencore",
        "006P700000UZv8c": "SARS"}
DONE = {"006P700000RD9Ir": "SAMI", "006P7000006uKrq": "Allstate"}

print("ALL deal_trigger_runs rows since trigger (T0=%s):" % T0)
rows = requests.get(f"{SB}/rest/v1/deal_trigger_runs",
                    params={"select": "opp_id,account_name,status,error,duration_ms,created_at,source",
                            "created_at": f"gte.{T0}", "order": "created_at.asc", "limit": "60"},
                    headers=SH, verify=False, timeout=(10, 60)).json()
if not rows:
    print("  (none)")
for r in rows:
    lbl = PEND.get(r["opp_id"]) or DONE.get(r["opp_id"]) or (r.get("account_name") or "")[:20]
    d = r.get("duration_ms")
    print(f"  {str(r['created_at'])[11:19]}  {lbl:16} {r['status']:>10} "
          f"{(str(round(d/60000,1))+'m') if d else '-':>7} src={r.get('source')} "
          f"{('ERR ' + str(r.get('error'))[:90]) if r.get('error') else ''}")

print("\nAny run row at all in the last 40 min (whole book — is the tier alive?):")
since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=40)).isoformat()
rows2 = requests.get(f"{SB}/rest/v1/deal_trigger_runs",
                     params={"select": "account_name,status,duration_ms,created_at",
                             "created_at": f"gte.{since}", "order": "created_at.desc", "limit": "12"},
                     headers=SH, verify=False, timeout=(10, 60)).json()
for r in rows2:
    d = r.get("duration_ms")
    print(f"  {str(r['created_at'])[11:19]}  {str(r.get('account_name'))[:24]:26} {r['status']:>10} "
          f"{(str(round(d/60000,1))+'m') if d else '-':>7}")
if not rows2:
    print("  (nothing finished in 40m)")

print("\nAPI health right now:")
try:
    h = requests.get(f"{API}/api/health", verify=False, timeout=(10, 25))
    print(" ", h.status_code, h.text[:150])
except Exception as e:
    print("  UNREACHABLE", type(e).__name__, e)
