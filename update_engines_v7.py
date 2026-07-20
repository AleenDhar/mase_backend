"""Omnivision Studio — TODO 10.2 -> 10.3 (user-approved 2026-07-10).

Three additions to fix the to-do quality the user flagged on Cebu:
  A. RECONCILE-FIRST — collapse to-dos whose event/window passed (ProcureCon), CLOSE ones the
     buyer completed (registered for Horizon), a STALENESS CAP that retires/re-dates 90+ day-old
     items, and a hard 60-day future cap. Never carry a to-do forward just because it was open.
  B. STATE-CONSISTENCY — every to-do must be achievable & consistent with the deal's real state
     (no "submit the RFP" when the RFP hasn't been received).
  C. ANTI-PREEMPTION — explicit/known/immediate next steps only; at most ONE critical look-ahead
     (no "CFO budget approval" on a pre-RFP deal).

Dry-run by default; pass --apply to write + lock.
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
ENV = r"C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local"
cfg = {}
for _l in open(ENV, encoding="utf-8"):
    _l = _l.strip()
    if _l and not _l.startswith("#") and "=" in _l:
        k, v = _l.split("=", 1)
        cfg[k.strip()] = v.strip().strip('"').strip("'")
BASE = cfg["NEXT_PUBLIC_SUPABASE_URL"].rstrip("/")
KEY = cfg["SUPABASE_SERVICE_ROLE_KEY"]
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}
NOW = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
BY = "omnivision-todo-reconcile-2026-07-10"

# -------- A. RECONCILE-FIRST (insert after the gold-mine block, before §3) ----------
A_ANCHOR = ("Missing any ONE of these three drops concrete information that defines the direction "
            "of the deal. This is MANDATORY, not best-effort.")
A_NEW = A_ANCHOR + (
    "\n\n## 2b. RECONCILE FIRST — before generating anything (added v10.3)\n"
    "A to-do is a LIVING item, re-judged against the LATEST evidence on every run — not a list "
    "that only grows. Before clubbing/ranking, reconcile every EXISTING/prior to-do AND every "
    "candidate:\n"
    "- COLLAPSE (retire) any to-do whose triggering EVENT or WINDOW has passed, or whose purpose "
    "is no longer achievable: a dated event now in the past (a conference that already happened), "
    "a milestone already reached, a requirement already fulfilled. e.g. 'invite X to ProcureCon' "
    "after ProcureCon (8-9 Jul) is dead — drop it. It carries NO meaning now.\n"
    "- CLOSE any to-do the buyer or we have since COMPLETED. Read registrations, sign-ups, "
    "attendance, sent docs, and completed steps as CLOSING signals — not just as asks. e.g. "
    "'registered for Horizon' CLOSES the Horizon-invite to-do and reads as progress, not an open "
    "action.\n"
    "- STALENESS CAP: a to-do anchored to an event/ask more than ~90 days old with no movement is "
    "presumed STALE — retire it, or if it is genuinely still live, RE-DATE it to a real near-term "
    "action. NEVER leave a 90+ day-old item sitting as if fresh. And NOTHING surfaces dated more "
    "than 60 days into the future (hard cap; reinforces §7).\n"
    "- KEEP only what is still open AND still relevant. ADD new items only from explicit new "
    "evidence. Never carry a to-do forward merely because it was open last run.")

# -------- B. STATE-CONSISTENCY (insert after §4 rule 5) ----------
B_ANCHOR = ("5. EMPTY section renders as a header with a positive / \"nothing pending\" state, so "
            "the rep knows it was checked, not missed.")
B_NEW = B_ANCHOR + (
    "\n6. STATE-CONSISTENCY (added v10.3). Every to-do must be ACHIEVABLE and CONSISTENT with the "
    "deal's ACTUAL current state. Never surface an action that contradicts where the deal is: if "
    "the RFP has NOT been received, the item is 'obtain/await the RFP', NOT 'submit the RFP'. "
    "Reconcile the action VERB against the real stage/state before surfacing — a to-do the deal "
    "cannot yet act on is wrong, not aspirational. Two items that contradict each other (we need "
    "to GET the RFP vs SUBMIT the RFP) can never both surface.")

# -------- C. ANTI-PREEMPTION (insert before §9 Suppression) ----------
C_ANCHOR = "## 9. Suppression"
C_NEW = (
    "## 8b. Anti-preemption — explicit & immediate only (added v10.3)\n"
    "Stick to the EXPLICIT, KNOWN, IMMEDIATE next steps grounded in real evidence. Do NOT "
    "chain-preempt multiple stages ahead: a pre-RFP deal does NOT get a 'talk to the CFO for "
    "budget approval' to-do. AT MOST ONE forward-looking (preemptive) item is allowed, and ONLY "
    "when it is a genuinely critical heavy-lead step that must start early to land by close (kick "
    "off InfoSec / security review, start a POC). Everything else must be the actual next action "
    "the deal is READY for now. Preempting a single critical step is useful; preempting many is "
    "noise that buries the real next move.\n\n"
    "## 8c. Acceptance (added v10.3)\n"
    "ProcureCon (8-9 Jul) to-do after 9 Jul → collapsed. 'Registered for Horizon' present → the "
    "Horizon-invite to-do is CLOSED and shown as progress. RFP not yet received → 'await/obtain "
    "the RFP', never 'submit the RFP'. Pre-RFP deal → NO 'CFO budget approval' item. A to-do "
    "anchored 90+ days ago with no movement → retired or re-dated, never left as-is.\n\n"
    "## 9. Suppression")


def latest(engine):
    rows = requests.get(f"{BASE}/rest/v1/scoring_instructions",
                        params={"engine": f"eq.{engine}", "locked": "is.true",
                                "select": "id,version,content"},
                        headers=H, verify=False, timeout=60).json()
    rows = [r for r in rows if r.get("version") != "draft"]
    rows.sort(key=lambda r: tuple(int(x) for x in r["version"].split(".")), reverse=True)
    return rows[0]


def sub(txt, anchor, new, label):
    if txt.count(anchor) != 1:
        raise SystemExit(f"ANCHOR [{label}] appears {txt.count(anchor)}x — aborting, no partial edits.")
    print(f"   ok {label}")
    return txt.replace(anchor, new)


todo = latest("todo")
print(f"base: todo v{todo['version']} ({len(todo['content']):,} chars)\n")
t = todo["content"]
t = sub(t, A_ANCHOR, A_NEW, "A. reconcile-first + staleness cap")
t = sub(t, B_ANCHOR, B_NEW, "B. state-consistency")
t = sub(t, C_ANCHOR, C_NEW, "C. anti-preemption + acceptance")
t = t.replace("ZYCUS TO-DO GENERATION — SYSTEM INSTRUCTION · v10.0",
              "ZYCUS TO-DO GENERATION — SYSTEM INSTRUCTION · v10.3", 1)
print(f"\nresult: todo v10.3 = {len(t):,} chars (+{len(t) - len(todo['content']):,})")

if not APPLY:
    open("cc_work/_todo103.md", "w", encoding="utf-8").write(t)
    print("\nDRY RUN — wrote cc_work/_todo103.md. Re-run with --apply to lock.")
    raise SystemExit(0)

NOTE = ("v10.3 (user-approved): RECONCILE-FIRST (collapse expired to-dos, close buyer-completed "
        "ones, 90-day staleness cap, 60-day future cap); STATE-CONSISTENCY (no contradictory "
        "verbs — await vs submit RFP); ANTI-PREEMPTION (explicit/immediate only, at most one "
        "critical look-ahead).")
r = requests.post(f"{BASE}/rest/v1/scoring_instructions",
                  headers={**H, "Content-Type": "application/json", "Prefer": "return=minimal"},
                  json={"engine": "todo", "version": "10.3", "content": t, "kind": "minor",
                        "note": NOTE, "locked": True, "locked_by": BY, "locked_at": NOW},
                  verify=False, timeout=90)
print(f"lock todo v10.3 -> HTTP {r.status_code} {'' if r.status_code < 300 else r.text[:200]}")
print("LOCKED — active_locked() serves todo v10.3 on the next sweep." if r.status_code < 300 else "FAILED")
