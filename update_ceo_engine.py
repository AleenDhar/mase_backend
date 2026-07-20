# -*- coding: utf-8 -*-
"""CEO engine v1.0 -> v1.1 (user-directed 2026-07-14): the CEO ESCALATES to the VP
(the deal owner's manager), NEVER the sales rep. Every ceo_ask is addressed to the VP,
who then works the rep. Adds a `vp` field. Dry-run by default; --apply to lock."""
import sys, warnings, datetime
warnings.filterwarnings("ignore")
import requests, urllib3
urllib3.disable_warnings()
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

APPLY = "--apply" in sys.argv
cfg = {}
for l in open(r"C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local", encoding="utf-8"):
    l = l.strip()
    if l and "=" in l and not l.startswith("#"):
        k, v = l.split("=", 1); cfg[k.strip()] = v.strip().strip('"').strip("'")
BASE = cfg["NEXT_PUBLIC_SUPABASE_URL"].rstrip("/"); KEY = cfg["SUPABASE_SERVICE_ROLE_KEY"]
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}
NOW = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
BY = "omnivision-ceo-vp-escalation-2026-07-14"

rows = requests.get(f"{BASE}/rest/v1/scoring_instructions",
                    params={"engine": "eq.ceo", "locked": "is.true", "select": "version,content"},
                    headers=H, verify=False, timeout=60).json()
row = sorted(rows, key=lambda r: tuple(int(x) for x in str(r["version"]).split(".")), reverse=True)[0]
t = row["content"]
print(f"base: ceo v{row['version']} ({len(t):,} chars)\n")


def sub(txt, old, new, label):
    if txt.count(old) != 1:
        raise SystemExit(f"ANCHOR [{label}] appears {txt.count(old)}x — aborting.")
    print(f"   ok {label}")
    return txt.replace(old, new)


# 1) §7 header — add the escalation-chain rule
t = sub(t,
        "## 7. Depth — each reason RICH and self-contained\nEvery reason must let a CEO grasp it in 10 seconds WITHOUT opening the deal. For EVERY\ntrigger and for SUPPORT, provide:",
        "## 7. Depth — each reason RICH and self-contained\nESCALATION CHAIN: the CEO manages DOWN through his VPs. Every `ceo_ask` is put to the\nVP — the deal owner's MANAGER (`vp` / manager_name from Salesforce) — NEVER the sales\nrep directly. The VP then works the rep. Name and address the VP in the ask.\nEvery reason must let a CEO grasp it in 10 seconds WITHOUT opening the deal. For EVERY\ntrigger and for SUPPORT, provide:",
        "§7 escalation-chain rule")

# 2) §7 ceo_ask — direct to the VP, with the user's worked example
t = sub(t,
        "  - `ceo_ask`  — the concrete thing the CEO should DO or ASK. For a WATCH reason this is\n    a pointed question to the rep/RSD (\"Ask Karson why the Ariba beta result is 25 days\n    overdue and whether Gaurav has gone cold\"); for a SUPPORT reason it is the CEO's own\n    action.",
        "  - `ceo_ask`  — the concrete thing the CEO should DO or ASK. For a WATCH reason this is\n    a pointed question the CEO puts to the VP (the deal owner's manager, `vp`), NEVER the\n    sales rep (\"Ask Pierre whether the 16 Jul redline return and the Merlin for iContract\n    core/optional call will genuinely land this week, or whether Florence's 22 Jul vacation\n    is about to force a third slip\"); for a SUPPORT reason it is the CEO's own action.\n  - `vp`       — the VP the CEO escalates to: the deal owner's MANAGER (manager_name from\n    Salesforce). The `ceo_ask` is addressed to THIS person, never the rep. If Salesforce\n    names no manager, `vp: null` and address the ask to \"the deal owner's VP\".",
        "§7 ceo_ask -> VP + vp field")

# 3) owner field description (no longer 'who the CEO would ask')
t = sub(t,
        "  - `owner`    — the Zycus deal owner / RSD accountable (who the CEO would ask).",
        "  - `owner`    — the Zycus deal owner / RSD accountable for the deal (the VP's report).",
        "§7 owner desc")

# 4) output contract — support block: add vp
t = sub(t,
        '    "owner": "RSD name",\n    "ceo_action": "the CEO\'s personal action",',
        '    "owner": "RSD name",\n    "vp": "VP name (deal owner\'s manager) — the CEO escalates here, not the rep",\n    "ceo_action": "the CEO\'s personal action",',
        "output support.vp")

# 5) output contract — trigger block: add vp + retarget ceo_ask
t = sub(t,
        '        "owner": "RSD name",\n        "ceo_ask": "pointed question the CEO asks the rep/RSD",',
        '        "owner": "RSD name",\n        "vp": "VP name (deal owner\'s manager) — the CEO addresses the ask here",\n        "ceo_ask": "pointed question the CEO puts to the VP (never the sales rep)",',
        "output trigger.vp")

# bump the title version
t = t.replace("# ZYCUS CEO ATTENTION — SYSTEM INSTRUCTION · v1.0",
              "# ZYCUS CEO ATTENTION — SYSTEM INSTRUCTION · v1.1", 1)

print(f"\nresult: ceo v1.1 = {len(t):,} chars (+{len(t) - len(row['content']):,})")
if not APPLY:
    open("cc_work/_ceo_v11.md", "w", encoding="utf-8").write(t)
    print("\nDRY RUN — wrote cc_work/_ceo_v11.md. Re-run with --apply to lock.")
    raise SystemExit(0)

NOTE = ("v1.1 (user-directed): the CEO ESCALATES to the VP (deal owner's manager), NEVER the "
        "sales rep — every ceo_ask is addressed to the VP, who then works the rep. Added a `vp` "
        "field (manager_name from Salesforce) on every support + watch reason.")
r = requests.post(f"{BASE}/rest/v1/scoring_instructions",
                  headers={**H, "Content-Type": "application/json", "Prefer": "return=minimal"},
                  json={"engine": "ceo", "version": "1.1", "content": t, "kind": "minor",
                        "note": NOTE, "locked": True, "locked_by": BY, "locked_at": NOW},
                  verify=False, timeout=90)
print(f"lock ceo v1.1 -> HTTP {r.status_code} {'' if r.status_code < 300 else r.text[:300]}")
print("LOCKED — active_locked('ceo') now serves v1.1." if r.status_code < 300 else "FAILED")
