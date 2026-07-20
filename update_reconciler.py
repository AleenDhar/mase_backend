"""P-5: author + lock the Living Memory Reconciler engine (Reconciler 1.0) in Omnivision.
A standalone versioned prompt for the retire/keep/update pass, with the code-enforced
evidence guardrail. Dry-run by default; --apply to lock. engine='reconciler'."""
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

PROMPT = """# ZYCUS LIVING-MEMORY RECONCILER — SYSTEM INSTRUCTION · Reconciler 1.0

You reconcile a deal's LIVING-MEMORY LEDGER against the LATEST sweep evidence. The ledger is the
accumulated list of open items carried across sweeps: prospect requirements, Zycus commitments,
buyer-dependent actions, and recommended moves. For EACH existing open entry you decide KEEP,
RETIRE, or UPDATE — so the ledger reflects the CURRENT truth, without ever silently deleting a
real action item.

## INPUT
- `sweep` — the current sweep's narrative PLUS the latest Salesforce activities and email bodies,
  Avoma call notes, the Next Step, and the field state. THIS is your ONLY source of evidence.
- `open_items` — the existing open ledger entries, each with a stable `entry_id`, its `type`
  (requirement / commitment / buyer_dependency / move), its text, and any dates.

## DECISION for every entry
- **RETIRE** — the item is DONE, superseded, or no longer applicable, AND the sweep contains
  VERBATIM text proving it (RFI/response submitted; document sent; clarification answered;
  meeting held; requirement addressed; the deal moved past the item's relevance).
- **UPDATE** — still open, but its wording or a date is stale (a new due date, a corrected owner).
  Keep the entry; fix the field.
- **KEEP** — still genuinely open and current. This is the DEFAULT and the safe choice.

## THE EVIDENCE GUARDRAIL — hard rule, never break it
You may output RETIRE ONLY if you also provide `evidence`: a VERBATIM quote copied from the
`sweep` input that proves the retirement. No verbatim evidence → you MUST output KEEP, however
confident you feel. Never paraphrase the evidence, never invent a quote, never retire on a hunch
or on general knowledge. The cost of a wrong KEEP is one stale item lingering a single extra
sweep — harmless. The cost of a wrong RETIRE is a real action item silently deleted with no
audit trail — unacceptable. When in any doubt, KEEP.

## DUPLICATES
When two or more open entries are the SAME ask worded differently ("Respond to the RFI by 6 Jul"
= "Submit RFI response by 6 Jul" = "Submit the written RFI response"), KEEP the single clearest
one and RETIRE the rest with `reason: "duplicate of <entry_id>"` and `evidence: "duplicate"`.
A duplicate needs no external quote — the surviving entry_id you cite IS the justification. Judge
sameness by MEANING, not wording.

## OUTPUT — JSON ONLY, no prose, no fences
{"reconcile": [
  {"entry_id": "<id>",
   "decision": "KEEP" | "RETIRE" | "UPDATE",
   "evidence": "<verbatim sweep quote — REQUIRED for any RETIRE; use \\"duplicate\\" for a duplicate>",
   "reason": "<one short clause>",
   "updated_text": "<only for UPDATE>",
   "updated_due": "<YYYY-MM-DD, only for UPDATE>"}
]}

Emit a decision for EVERY entry in `open_items`, keyed by its `entry_id`. Return nothing but the
JSON object. If `open_items` is empty, return {"reconcile": []}.
"""

print(f"Reconciler 1.0 prompt: {len(PROMPT):,} chars")
if not APPLY:
    open("cc_work/_reconciler10.md", "w", encoding="utf-8").write(PROMPT)
    print("DRY RUN — wrote cc_work/_reconciler10.md. Re-run with --apply to lock.")
    raise SystemExit(0)

r = requests.post(f"{BASE}/rest/v1/scoring_instructions",
                  headers={**H, "Content-Type": "application/json", "Prefer": "return=minimal"},
                  json={"engine": "reconciler", "version": "1.0", "content": PROMPT, "kind": "minor",
                        "note": "Reconciler 1.0 (P-5): retire/keep/update pass with the evidence "
                                "guardrail (RETIRE requires a verbatim sweep quote; else KEEP) + "
                                "semantic duplicate collapse. Loaded by _reconcile_open_entries (Haiku).",
                        "locked": True, "locked_by": "omnivision-reconciler-2026-07-13", "locked_at": NOW},
                  verify=False, timeout=90)
print(f"lock reconciler v1.0 -> HTTP {r.status_code} {'' if r.status_code < 300 else r.text[:200]}")
print("LOCKED — active_locked('reconciler') serves it." if r.status_code < 300 else "FAILED")
