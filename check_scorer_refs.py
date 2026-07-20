"""Verify the scorer renders reference citations as plain names (no literal token) and still
carries the win qualification-depth floor."""
import os, warnings
warnings.filterwarnings("ignore")
import requests, urllib3
from daily_summary.common import load_secret, VERIFY
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
sec = load_secret()
for k in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SERVICE_KEY"):
    if sec.get(k):
        os.environ[k] = sec[k]
SB = sec["SUPABASE_URL"].rstrip("/")
K = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
H = {"apikey": K, "Authorization": f"Bearer {K}"}
import scoring_studio as st


def active_locked_local():
    rows = requests.get(f"{SB}/rest/v1/scoring_instructions",
                        params={"locked": "eq.true", "select": "engine,version,content"},
                        headers=H, verify=VERIFY, timeout=60).json()

    def vk(v):
        try:
            return tuple(int(x) for x in str(v).split("."))
        except ValueError:
            return (-1,)
    best = {}
    for r in rows:
        if r.get("version") == "draft":
            continue
        e = r["engine"]
        if e not in best or vk(r["version"]) > vk(best[e]["version"]):
            best[e] = r
    return {e: ({"version": best[e]["version"], "content": best[e]["content"]} if e in best else None)
            for e in st.ASSETS}


st.active_locked = active_locked_local
import deal_engine_ai_scoring as A
p = A._prompt()
print("scorer prompt chars :", len(p))
print("literal {{ref: token :", "{{ref:" in p, "(want False)")
print("plain Vendor Dict    :", "MASE Vendor Dictionary" in p, "(want True)")
print("plain Deal Playbook  :", "Zycus Deal Playbook" in p, "(want True)")
print("win 4.4a floor kept  :", "QUALIFICATION-DEPTH FLOOR" in p, "(want True)")
