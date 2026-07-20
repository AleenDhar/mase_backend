"""Print the locked mom v10.7 engine in full (and win §4.4b/4.5) so the gap is exact."""
import sys, warnings
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
K = cfg["SUPABASE_SERVICE_ROLE_KEY"]
SH = {"apikey": K, "Authorization": f"Bearer {K}"}
eng = sys.argv[1] if len(sys.argv) > 1 else "mom"
ver = sys.argv[2] if len(sys.argv) > 2 else "10.7"
r = requests.get(f"{SB}/rest/v1/scoring_instructions",
                 params={"select": "content", "engine": f"eq.{eng}", "version": f"eq.{ver}"},
                 headers=SH, verify=False, timeout=(10, 60)).json()
print(r[0]["content"])
