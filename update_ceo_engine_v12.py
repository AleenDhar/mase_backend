# -*- coding: utf-8 -*-
"""CEO engine v1.1 -> v1.2 (fix 2026-07-14): v1.1 STILL addressed the rep ('Ask Pierre')
because the worked example named the rep. The example IS the strongest signal to the LLM.
v1.2: OPEN every ceo_ask by addressing the VP by name; reference the rep only in the third
person. Corrected the example accordingly. Dry-run by default; --apply to lock."""
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
BY = "omnivision-ceo-vp-fix-2026-07-14"

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


# 1) harden the escalation rule — OPEN with the VP, rep in third person only
t = sub(t,
        "ESCALATION CHAIN: the CEO manages DOWN through his VPs. Every `ceo_ask` is put to the\nVP — the deal owner's MANAGER (`vp` / manager_name from Salesforce) — NEVER the sales\nrep directly. The VP then works the rep. Name and address the VP in the ask.",
        "ESCALATION CHAIN: the CEO manages DOWN through his VPs and NEVER contacts a sales rep\ndirectly. OPEN every `ceo_ask` by addressing the VP BY NAME (the `vp` / manager_name from\nSalesforce). Reference the sales rep ONLY in the third person, as the VP's report (\"...whether\nhis rep X will...\", \"...why hasn't X sent...\"). Do NOT open the ask with the rep's name and\ndo NOT address the rep — the CEO asks the VP, and the VP works the rep.",
        "§7 escalation rule (open-with-VP)")

# 2) fix the worked example — address the VP (John Woodcock), rep (Pierre) in third person
t = sub(t,
        "  - `ceo_ask`  — the concrete thing the CEO should DO or ASK. For a WATCH reason this is\n    a pointed question the CEO puts to the VP (the deal owner's manager, `vp`), NEVER the\n    sales rep (\"Ask Pierre whether the 16 Jul redline return and the Merlin for iContract\n    core/optional call will genuinely land this week, or whether Florence's 22 Jul vacation\n    is about to force a third slip\"); for a SUPPORT reason it is the CEO's own action.",
        "  - `ceo_ask`  — the concrete thing the CEO should DO or ASK. For a WATCH reason it is a\n    pointed question ADDRESSED TO THE VP (open with the VP's name from `vp`), with the sales\n    rep named only in the third person (\"Ask John Woodcock whether his rep Pierre will\n    genuinely land the 16 Jul redline return and the Merlin for iContract core/optional call\n    this week, or whether Florence's 22 Jul vacation is about to force a third slip\"); for a\n    SUPPORT reason it is the CEO's own action.",
        "§7 ceo_ask example (open-with-VP)")

t = t.replace("# ZYCUS CEO ATTENTION — SYSTEM INSTRUCTION · v1.1",
              "# ZYCUS CEO ATTENTION — SYSTEM INSTRUCTION · v1.2", 1)

print(f"\nresult: ceo v1.2 = {len(t):,} chars")
if not APPLY:
    open("cc_work/_ceo_v12.md", "w", encoding="utf-8").write(t)
    print("\nDRY RUN — wrote cc_work/_ceo_v12.md. Re-run with --apply to lock.")
    raise SystemExit(0)

NOTE = ("v1.2 (fix): v1.1 still addressed the rep because the worked example named the rep. "
        "OPEN every ceo_ask by addressing the VP by name; rep referenced only in the third "
        "person. Corrected the example (Ask John Woodcock whether his rep Pierre ...).")
r = requests.post(f"{BASE}/rest/v1/scoring_instructions",
                  headers={**H, "Content-Type": "application/json", "Prefer": "return=minimal"},
                  json={"engine": "ceo", "version": "1.2", "content": t, "kind": "minor",
                        "note": NOTE, "locked": True, "locked_by": BY, "locked_at": NOW},
                  verify=False, timeout=90)
print(f"lock ceo v1.2 -> HTTP {r.status_code} {'' if r.status_code < 300 else r.text[:300]}")
print("LOCKED — active_locked('ceo') now serves v1.2." if r.status_code < 300 else "FAILED")
