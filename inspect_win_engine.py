"""Read-only: dump the reason/output-format guidance from the LOCKED win/mom Studio engines."""
import warnings; warnings.filterwarnings("ignore")
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

KW = ("reason", "bullet", "evidence", "cite", "why", "output", "format", "detail", "contribut")
for e in ("win", "mom"):
    row = best.get(e)
    if not row:
        print(f"--- {e}: NONE ---")
        continue
    c = row["content"]
    print(f"\n======== {e} v{row['version']} ({len(c)} chars) ========")
    for ln in c.splitlines():
        if any(k in ln.lower() for k in KW):
            print("  |", ln.strip()[:170])
