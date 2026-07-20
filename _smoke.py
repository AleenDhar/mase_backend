import warnings, json; warnings.filterwarnings("ignore")
import requests, urllib3; urllib3.disable_warnings()
cfg = {}
for l in open(r"C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local", encoding="utf-8"):
    l = l.strip()
    if l and not l.startswith("#") and "=" in l:
        k, v = l.split("=", 1); cfg[k.strip()] = v.strip().strip('"').strip("'")
API = cfg["DEAL_ENGINE_API_BASE"].rstrip("/")
AH = {"Authorization": f"Bearer {cfg['DEAL_ENGINE_TOKEN']}"}
r = requests.get(f"{API}/api/deal-engine/sweep/active", headers=AH, verify=False, timeout=(10, 40))
print("GET /api/deal-engine/sweep/active ->", r.status_code)
try:
    j = r.json()
    print("keys:", list(j.keys()) if isinstance(j, dict) else type(j))
    print("running:", j.get("running"), "| count:", j.get("count"))
    print("sample:", json.dumps((j.get("active") or [])[:2], indent=2)[:400])
except Exception as e:
    print("body:", r.text[:300], e)
