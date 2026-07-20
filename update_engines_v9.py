"""Omnivision — add a GROUND TRUTH & RECENCY directive to EVERY engine (user directive
2026-07-13). SFDC (next steps, activities, fields) + the Avoma datalake are the ONLY
source of truth and the LATEST state wins; no living-memory carry-forward; done-is-done;
one consistent state across every section; collapse duplicates by meaning. Pairs with the
from-scratch sweep code change (DEAL_SWEEP_KEEP_LIVING_MEMORY).

Dry-run by default; --apply to write + lock. Bumps each engine's minor version.
"""
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
for _l in open(r"C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local", encoding="utf-8"):
    _l = _l.strip()
    if _l and not _l.startswith("#") and "=" in _l:
        k, v = _l.split("=", 1)
        cfg[k.strip()] = v.strip().strip('"').strip("'")
BASE = cfg["NEXT_PUBLIC_SUPABASE_URL"].rstrip("/")
KEY = cfg["SUPABASE_SERVICE_ROLE_KEY"]
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}
NOW = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
BY = "omnivision-ground-truth-2026-07-13"

MARKER = "## GROUND TRUTH & RECENCY"
BLOCK = """## GROUND TRUTH & RECENCY — read this FIRST; it overrides everything below

Salesforce and the Avoma datalake are the ONLY sources of truth for this deal, and the
LATEST state is the truth:
- **Salesforce** = the deal's **Next Step**, its **open and closed Activities** (tasks,
  events, and logged emails — including `[Clari]` / `[Email Sent]` / `[Email Received]`
  threads), and its **fields** (stage, amount, forecast category, close date, owner). The
  rep-maintained **Next Step** and the newest activities describe what is ACTUALLY happening
  right now — treat them as authoritative over any inference.
- **Avoma datalake** = the deal's calls and meeting transcripts.

Non-negotiable rules:
1. **Latest wins — there is NO living memory.** Each sweep rebuilds from the current
   evidence. Never carry forward, re-assert, or trust a prior/remembered value when the
   latest Salesforce/Avoma evidence covers it. If your own read of the latest evidence
   disagrees with anything remembered, the latest evidence wins.
2. **Done is done.** If the latest evidence shows an ask is completed — the RFI/proposal was
   submitted, a document was sent, a clarification was answered, a meeting was held — it is
   CLOSED. NEVER emit it as an open requirement, commitment, buyer-dependency, best-practice,
   move, or to-do, and never flag it unaddressed. Its completion must show EVERYWHERE it
   appears, not just in one place.
3. **One deal, one state.** Every section you produce must describe the SAME current reality.
   No section may contradict another — a score narrative that says "the RFI response landed"
   while a to-do says "submit the RFI", or a move that treats the RFI as pre-release while
   another calls it post-submission, is a HARD FAILURE. Reconcile to the latest truth BEFORE
   you write anything.
4. **No duplicates.** Collapse the same ask stated in different words into ONE item; judge
   sameness by MEANING, not wording ("Respond to the RFI by 6 Jul" = "Submit RFI response by
   6 Jul" = "Submit the written RFI response" → one item, and if the latest evidence shows it
   done, drop it).
5. **Honour the rep's Salesforce Next Step + activities.** They are ground truth for what is
   happening now (e.g. "currently bidding", "met the buyer", "responses submitted"). Do not
   overwrite them with a guess or an older state.

"""


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
    if MARKER in content:
        print(f"  SKIP {eng}: already has the directive")
        continue
    # Insert the block right after the first line (the title).
    nl = content.find("\n")
    if nl == -1:
        new = content + "\n\n" + BLOCK
    else:
        new = content[:nl + 1] + "\n" + BLOCK + content[nl + 1:]
    nv = bump(cur["version"])
    plan.append((eng, cur["version"], nv, new))
    print(f"  {eng:8} v{cur['version']} -> v{nv}  (+{len(new) - len(content):,} chars)")

if not plan:
    print("nothing to do.")
    raise SystemExit(0)

if not APPLY:
    print("\nDRY RUN — re-run with --apply to lock all of the above.")
    raise SystemExit(0)

NOTE = ("Added GROUND TRUTH & RECENCY directive: SFDC (next steps/activities/fields) + Avoma "
        "datalake are the only source of truth, latest wins, no living-memory carry-forward, "
        "done-is-done, one consistent state across all sections, collapse duplicates by "
        "meaning, honour the rep's SF Next Step. Pairs with the from-scratch sweep change.")
for eng, ov, nv, new in plan:
    r = requests.post(f"{BASE}/rest/v1/scoring_instructions",
                      headers={**H, "Content-Type": "application/json", "Prefer": "return=minimal"},
                      json={"engine": eng, "version": nv, "content": new, "kind": "minor",
                            "note": NOTE, "locked": True, "locked_by": BY, "locked_at": NOW},
                      verify=False, timeout=90)
    print(f"lock {eng} v{nv} -> HTTP {r.status_code} {'' if r.status_code < 300 else r.text[:160]}")
print("DONE — active_locked() serves the new versions on the next sweep.")
