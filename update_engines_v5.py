"""Omnivision Studio — WIN 10.6 -> 10.7: add the MOMENTUM GATE. On a late-stage deal
(Vendor Selected+), Deal Momentum < 30 HALVES the Win Position, overriding the 4.4a
qualification-depth floor (historical depth protects a deal only while it's still alive).
Surgical insert after 4.4a + a section-8 acceptance test. Dry-run default; --apply to lock.
"""
import sys, warnings
warnings.filterwarnings("ignore")
import requests, urllib3
from daily_summary.common import load_secret, VERIFY
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

APPLY = "--apply" in sys.argv
sec = load_secret()
BASE = sec["SUPABASE_URL"].rstrip("/")
KEY = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}

GATE = (
    "\n\n4.4b MOMENTUM GATE on late-stage deals (SUPERSEDES 4.4a when momentum is dead). "
    "Historical qualification and a high recorded stage PROTECT a deal only while it is still "
    "alive in the market. A late-stage deal that has gone momentum-dead is a stalled deal wearing "
    "a stage badge, not a strong win. THEREFORE: when the deal is LATE-STAGE (recorded stage "
    "Vendor Selected or later) AND Deal Momentum < 30 (the same 0–100 Momentum score produced for "
    "this deal on this evidence), HALVE the Win Position — take it to roughly 50% of what the stage "
    "anchor + fundamentals + the 4.4a floor would otherwise give (a Vendor Selected deal the "
    "fundamentals put at ~40 drops to ~20). This OVERRIDES the 4.4a qualification-depth floor: deep "
    "history no longer holds the floor once the deal is momentum-dead. State it in the rationale, "
    "e.g. 'Momentum gate: Vendor Selected but momentum 8 (<30) after 330 days dark — win halved from "
    "~40 to ~20.' Momentum ≥ 30 → gate OFF, score normally. (added v10.7)")

ACCEPT = ("\n\n## 8c. Momentum-gate acceptance\nVendor Selected, deep qualification (EB engaged + "
          "MEDDPICC complete), BUT Deal Momentum 8 (<30) after 330 days dark → Win HALVED to ~18–22 "
          "(the 4.4a ~40 floor is overridden by 4.4b), NOT held at 40. Same deal with Momentum 35 "
          "(≥30) → gate off, 4.4a floor applies (~40).")

ANCHOR = "and a genuinely cold or shallow dark deal still scores low."

row = requests.get(f"{BASE}/rest/v1/scoring_instructions",
                   params={"engine": "eq.win", "locked": "eq.true", "select": "id,version,content"},
                   headers=H, verify=VERIFY, timeout=30).json()
row = [r for r in row if r.get("version") != "draft"]
row.sort(key=lambda r: tuple(int(x) for x in str(r["version"]).split(".")), reverse=True)
row = row[0]
assert row["version"] == "10.6", f"expected locked win v10.6, got {row['version']}"
c = row["content"]
assert c.count(ANCHOR) == 1, "4.4a anchor not unique/found"
assert "MOMENTUM GATE on late-stage deals" not in c, "gate already present"
i = c.find(ANCHOR) + len(ANCHOR)
new_c = c[:i] + GATE + c[i:]
new_c = new_c.rstrip() + ACCEPT
assert "4.4b MOMENTUM GATE" in new_c and "## 8c." in new_c
print(f"win v10.6 -> v10.7: {len(c)} -> {len(new_c)} chars")

if not APPLY:
    print("\n[DRY-RUN] --apply to lock.\n--- gate ---\n" + GATE.strip())
    raise SystemExit(0)

r = requests.post(f"{BASE}/rest/v1/scoring_instructions",
                  headers={**H, "Content-Type": "application/json", "Prefer": "return=minimal"},
                  json=[{"engine": "win", "version": "10.7", "kind": "minor",
                         "note": "Momentum gate (4.4b): late-stage (Vendor Selected+) deal with Deal "
                                 "Momentum < 30 has its Win Position HALVED, overriding the 4.4a "
                                 "qualification-depth floor — historical depth protects a deal only "
                                 "while it is still alive.",
                         "content": new_c, "locked": True, "locked_by": "omnivision-momgate-2026-07-09"}],
                  verify=VERIFY, timeout=60)
if r.status_code >= 300:
    raise SystemExit(f"insert win v10.7 FAILED {r.status_code}: {r.text[:300]}")
u = requests.patch(f"{BASE}/rest/v1/scoring_instructions", params={"id": f"eq.{row['id']}"},
                   headers={**H, "Content-Type": "application/json"}, json={"locked": False},
                   verify=VERIFY, timeout=30)
requests.patch(f"{BASE}/rest/v1/scoring_instructions", params={"locked": "eq.true", "locked_at": "is.null"},
               headers={**H, "Content-Type": "application/json"}, json={"locked_at": "now()"}, verify=VERIFY, timeout=30)
print(f"win v10.7 locked; prior v10.6 unlock={u.status_code}")
# write the repo sync copy
import os
os.makedirs("prompts/studio_seeds", exist_ok=True)
with open("prompts/studio_seeds/win-position.md", "w", encoding="utf-8") as f:
    f.write(new_c)
print("synced -> prompts/studio_seeds/win-position.md")
