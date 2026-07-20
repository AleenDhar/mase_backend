"""Omnivision v11 — reconcile the GROUND TRUTH directive with the KEEP-LIVING-MEMORY
reversal (user directive 2026-07-13/14: "keep the living memory and add all the pointers
in the reconciler doc"). The v9 block was written for the from-scratch experiment and its
rule #1 said "there is NO living memory — each sweep rebuilds from scratch". Living memory
is now KEPT and RECONCILED (the P-6 reconciler retires done/duplicate items with an
evidence guardrail), so rule #1 now CONTRADICTS the code. This script surgically rewrites
ONLY rule #1 in every engine to reconcile-aware language ("latest wins — reconcile the
ledger to it"), leaving rules 2-5 (done-is-done, one-state, no-dupes, honour-next-step)
untouched — they already reinforce the reconciler.

Dry-run by default; --apply to write + lock. Bumps each engine's minor version.
"""
import sys, re, warnings, datetime
warnings.filterwarnings("ignore")
import requests, urllib3
urllib3.disable_warnings()
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

APPLY = "--apply" in sys.argv
cfg = {}
for _l in open(r"C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local", encoding="utf-8"):
    _l = _l.strip()
    if _l and not _l.startswith("#") and "=" in _l:
        k, v = _l.split("=", 1)
        cfg[k.strip()] = v.strip().strip('"').strip("'")
BASE = cfg["NEXT_PUBLIC_SUPABASE_URL"].rstrip("/")
KEY = cfg["SUPABASE_SERVICE_ROLE_KEY"]
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}
NOW = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
BY = "omnivision-keep-lm-reconcile-2026-07-14"

# Match the whole of rule #1 as v9 wrote it (multi-line), regardless of internal wrapping.
OLD_RULE1_RE = re.compile(
    r"1\.\s*\*\*Latest wins\s*[—-]\s*there is NO living memory\.\*\*.*?the latest evidence wins\.",
    re.DOTALL)

NEW_RULE1 = (
    "1. **Latest wins — reconcile living memory to it.** This deal carries a LIVING-MEMORY "
    "ledger of still-open items (requirements, commitments, buyer-dependencies) accumulated "
    "across sweeps. That ledger is a running record, NOT a source of truth: the moment the "
    "latest Salesforce/Avoma evidence covers an item, the latest evidence WINS. Reconcile "
    "every carried item against it — RETIRE what the evidence shows is done, UPDATE what "
    "changed (a new date or owner), and KEEP only what is still genuinely open. Never "
    "re-assert or trust a remembered value that the latest evidence contradicts; when your "
    "own read of the latest evidence disagrees with anything remembered, the latest evidence "
    "wins.")


def latest(engine):
    rows = requests.get(f"{BASE}/rest/v1/scoring_instructions",
                        params={"engine": f"eq.{engine}", "locked": "is.true",
                                "select": "version,content"},
                        headers=H, verify=False, timeout=60).json()
    rows = [r for r in rows if r.get("version") != "draft"]
    rows.sort(key=lambda r: tuple(int(x) for x in r["version"].split(".")), reverse=True)
    return rows[0]


def bump(v):
    a, b = v.split(".")
    return f"{a}.{int(b) + 1}"


ENGINES = ["extract", "win", "mom", "todo", "sum", "sweep"]
plan = []
for eng in ENGINES:
    cur = latest(eng)
    content = cur["content"]
    if "reconcile living memory to it" in content:
        print(f"  SKIP {eng}: already reconcile-aware")
        continue
    if not OLD_RULE1_RE.search(content):
        print(f"  WARN {eng}: old rule #1 not found (v{cur['version']}) — SKIPPING, inspect manually")
        continue
    new = OLD_RULE1_RE.sub(NEW_RULE1, content, count=1)
    nv = bump(cur["version"])
    plan.append((eng, cur["version"], nv, new))
    print(f"  {eng:8} v{cur['version']} -> v{nv}  ({len(new) - len(content):+,} chars)")

if not plan:
    print("nothing to do.")
    raise SystemExit(0)

if not APPLY:
    print("\nDRY RUN — re-run with --apply to lock all of the above.")
    raise SystemExit(0)

NOTE = ("v11: reconcile GROUND TRUTH rule #1 with keep-living-memory reversal — the ledger "
        "is KEPT and reconciled to latest truth (was: 'no living memory, rebuild from "
        "scratch'). Rules 2-5 unchanged. Pairs with the P-6 reconciler + "
        "DEAL_SWEEP_KEEP_LIVING_MEMORY=true default.")
for eng, ov, nv, new in plan:
    r = requests.post(f"{BASE}/rest/v1/scoring_instructions",
                      headers={**H, "Content-Type": "application/json", "Prefer": "return=minimal"},
                      json={"engine": eng, "version": nv, "content": new, "kind": "minor",
                            "note": NOTE, "locked": True, "locked_by": BY, "locked_at": NOW},
                      verify=False, timeout=90)
    print(f"  lock {eng} v{nv} -> HTTP {r.status_code} {'' if r.status_code < 300 else r.text[:160]}")
print("DONE" if all(True for _ in plan) else "")
