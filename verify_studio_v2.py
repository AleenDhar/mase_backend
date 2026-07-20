"""LOCAL verify of the Studio-v2 effective sweep prompt composition (read-only)."""
import sys, os, re, warnings
warnings.filterwarnings("ignore")
import requests, urllib3
from daily_summary.common import load_secret, VERIFY
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sec = load_secret(); SB = sec["SUPABASE_URL"].rstrip("/")
KEY = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}
for k in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SERVICE_KEY"):
    if sec.get(k):
        os.environ[k] = sec[k]

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
        if r["version"] == "draft":
            continue
        e = r["engine"]
        if e not in best or vk(r["version"]) > vk(best[e]["version"]):
            best[e] = r
    return {e: ({"version": best[e]["version"], "content": best[e]["content"]} if e in best else None)
            for e in st.ASSETS}


st.active_locked = active_locked_local

import deal_engine_sweep as S
S._studio_cache.update(at=0.0)   # bust TTL
eff = S._load_prompt()
print("=== EFFECTIVE SWEEP PROMPT (Studio v2) ===")
print("total chars:", len(eff))
print("first line:", eff.splitlines()[0][:90])
print("versions (provenance):", S.studio_versions())
checks = [
    ("Deal Drawer) · v3" in eff.splitlines()[0], "base = Deal Sweep v3"),
    ("### ENGINE — Signal Extraction / Deal-Reading · LOCKED v10.4" in eff, "extract v10.4 block"),
    ("### ENGINE — Zycus Win Position" in eff, "win block"),
    ("### ENGINE — Deal Momentum" in eff, "mom block"),
    ("### ENGINE — To-Do Generation" in eff, "todo block"),
    ("### ENGINE — 24-Hour Summary" in eff, "sum block"),
    ("# REFERENCE ASSETS" in eff, "reference appendix"),
    ("### REFERENCE — Vendor Dictionary · LOCKED v1.0" in eff, "vendordict section"),
    ("### REFERENCE — Deal Playbook · LOCKED v1.0" in eff, "playbook section"),
    ("## A5b. Vendor / competitor entity resolution" in eff, "A5b in extract"),
    (len(re.findall(r"\{\{\s*ref:", eff)) == 0, "no unresolved {{ref:}} tokens"),
    (eff.count("### REFERENCE — Vendor Dictionary") == 1, "vendordict appears once"),
    (eff.count("### REFERENCE — Deal Playbook") == 1, "playbook appears once"),
    ("### ENGINE — Deal Sweep" not in eff, "sweep engine not duplicated in appendix"),
]
fails = 0
for ok, label in checks:
    print(("OK   " if ok else "FAIL ") + label)
    fails += (not ok)
print(f"\n{len(checks)-fails}/{len(checks)} checks pass")
sys.exit(1 if fails else 0)
