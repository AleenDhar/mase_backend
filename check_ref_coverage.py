"""LIVE per-engine reference-citation coverage (queries the locked scoring_instructions)."""
import warnings
warnings.filterwarnings("ignore")
import requests, urllib3
from daily_summary.common import load_secret, VERIFY
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
sec = load_secret()
SB = sec["SUPABASE_URL"].rstrip("/")
K = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
H = {"apikey": K, "Authorization": f"Bearer {K}"}
rows = requests.get(f"{SB}/rest/v1/scoring_instructions",
                    params={"locked": "eq.true", "select": "engine,version,content"},
                    headers=H, verify=VERIFY, timeout=60).json()


def vk(v):
    try:
        return tuple(int(x) for x in str(v).split("."))
    except Exception:
        return (-1,)


best = {}
for r in rows:
    if r.get("version") == "draft":
        continue
    e = r["engine"]
    if e not in best or vk(r["version"]) > vk(best[e]["version"]):
        best[e] = r

ORDER = ["extract", "win", "mom", "todo", "sum", "sweep", "vendordict", "playbook"]
print(f"{'engine':11} {'ver':7} {'playbook':9} {'vendordict':11}")
print("-" * 42)
for e in ORDER:
    r = best.get(e)
    if not r:
        continue
    c = r["content"] or ""
    pb = "YES" if "{{ref:deal-playbook}}" in c else "-"
    vd = "YES" if "{{ref:vendor-dictionary}}" in c else "-"
    tag = "  (reference asset)" if e in ("vendordict", "playbook") else ""
    print(f"{e:11} v{r['version']:6} {pb:9} {vd:11}{tag}")
