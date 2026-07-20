"""Read-only: dump every LOCKED Omnivision engine (sweep/win/mom/extract/todo/sum/...) to
local files under engines_dump/ so we can review + edit the system prompts."""
import os, warnings
warnings.filterwarnings("ignore")
import requests, urllib3
from daily_summary.common import load_secret, VERIFY
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

sec = load_secret()
SB = sec["SUPABASE_URL"].rstrip("/")
K = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
H = {"apikey": K, "Authorization": f"Bearer {K}"}
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "engines_dump")
os.makedirs(OUT, exist_ok=True)

rows = requests.get(f"{SB}/rest/v1/scoring_instructions",
                    params={"locked": "eq.true", "select": "engine,version,content"},
                    headers=H, verify=VERIFY, timeout=60).json()
if not isinstance(rows, list):
    print("unexpected response:", rows)
    raise SystemExit(1)


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

print(f"locked engines: {len(best)}")
for e, r in sorted(best.items()):
    c = r["content"] or ""
    path = os.path.join(OUT, f"{e}_v{r['version']}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(c)
    # quick scan: does this engine mention SFDC next-steps / activity-history evidence?
    lc = c.lower()
    hits = [k for k in ("next step", "next_step", "activity", "activities", "task", "email",
                        "logged call", "sfdc", "salesforce", "no transcript", "not recorded",
                        "avoma was not", "history") if k in lc]
    print(f"  {e:11} v{r['version']:6} {len(c):6}c  -> mentions: {', '.join(hits) if hits else '(none)'}")
print(f"\nwrote to {OUT}/")
