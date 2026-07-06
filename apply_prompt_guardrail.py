"""Append the 'Name & title fidelity' guardrail to the LIVE deal-sweep system
prompt (Supabase jarvis_settings id=mase_deal_sweep). Backs up the current prompt
OUTSIDE the repo first; pure APPEND (inserts before '## 2. Tools and the read
plan'); verifies nothing else changed. Reversible."""
import sys, os, datetime, requests, urllib3
from daily_summary.common import load_secret
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ANCHOR = "## 2. Tools and the read plan"
GUARDRAIL = (
    "**Name & title fidelity — Salesforce is the canonical spelling.** Call transcripts "
    "are speech-to-text: attendee and speaker names are frequently mis-transcribed (a "
    "mispronounced or accented name comes through wrong). A transcript is therefore NOT an "
    "authority for how a person's name is spelled or who they are. Before you name any person:\n"
    "1) **Resolve to the Salesforce contact.** Match them to an OpportunityContactRole / Task / "
    "Event contact by name OR by email (email local-parts — first.last@ — are reliable; "
    "transcripts are not). On a match, use the SALESFORCE spelling of the name verbatim and "
    "their Salesforce Contact.Title for the title. Never emit a transcript-only spelling when a "
    "Salesforce contact is the same person.\n"
    "2) **No Salesforce match → do not promote them.** If a name appears only in a transcript and "
    "matches no Salesforce contact or email, treat it as UNVERIFIED: do not present it as a "
    "confirmed named stakeholder, and do NOT attach a job title or authority (CFO / economic buyer "
    "/ decision-maker) to it. Refer to the role instead (\"a finance stakeholder\", \"the budget "
    "owner\") and record the gap in evidence_coverage.gaps.\n"
    "3) **Titles come from Salesforce, not inference.** Only state an executive title or "
    "economic-buyer / decision-maker role when the Salesforce Contact.Title or "
    "OpportunityContactRole.Role supports it. Never infer \"CFO\" because the deal needs one or a "
    "call implied seniority — a wrong title sends the rep to the wrong person. Never emit a role "
    "with no name (\"CFO (—)\"). (The server also enforces this deterministically.)\n\n")


def main():
    dry = "--apply" not in sys.argv
    sec = load_secret()
    base = sec["SUPABASE_URL"].rstrip("/")
    key = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
    h = {"apikey": key, "Authorization": f"Bearer {key}"}

    rows = requests.get(f"{base}/rest/v1/jarvis_settings",
                        params={"id": "eq.mase_deal_sweep", "select": "system_prompt"},
                        headers=h, verify=False, timeout=40).json()
    cur = (rows[0]["system_prompt"] if rows and rows[0].get("system_prompt") else "")
    print(f"[read] live prompt: {len(cur)} chars")

    if GUARDRAIL.split(".")[0] in cur:
        print("!! guardrail already present — aborting (idempotent)."); return
    n = cur.count(ANCHOR)
    if n != 1:
        print(f"!! anchor '{ANCHOR}' appears {n} times — expected exactly 1. ABORT."); return

    new = cur.replace(ANCHOR, GUARDRAIL + ANCHOR, 1)

    # --- verify the edit is a clean, additive insertion -----------------------
    assert len(new) == len(cur) + len(GUARDRAIL), "length delta mismatch"
    assert new.count(ANCHOR) == 1, "anchor count changed"
    assert cur.replace(ANCHOR, GUARDRAIL + ANCHOR, 1) == new
    # everything that was there is still there (removing only the inserted block returns the original)
    assert new.replace(GUARDRAIL, "", 1) == cur, "insertion is not purely additive"
    for keep in ("## 1. Operating rules", "## 3. The North Star", "## 5. Output contract"):
        assert new.count(keep) == cur.count(keep), f"section '{keep}' count changed"
    print(f"[verify] additive OK: +{len(GUARDRAIL)} chars, all sections intact")

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = os.path.join(os.path.expanduser("~"), f"mase_deal_sweep_prompt_backup_{stamp}.md")
    with open(backup, "w", encoding="utf-8") as f:
        f.write(cur)
    print(f"[backup] current prompt saved -> {backup}")

    if dry:
        print("\n[DRY RUN] not written. Re-run with --apply to write to Supabase.")
        print("--- guardrail preview ---")
        print(GUARDRAIL[:400] + "…")
        return

    r = requests.post(f"{base}/rest/v1/jarvis_settings",
                      params={"on_conflict": "id"},
                      headers={**h, "Content-Type": "application/json",
                               "Prefer": "resolution=merge-duplicates,return=minimal"},
                      json={"id": "mase_deal_sweep", "system_prompt": new,
                            "updated_at": datetime.datetime.now(datetime.timezone.utc)
                            .strftime("%Y-%m-%dT%H:%M:%SZ")},
                      verify=False, timeout=60)
    if r.status_code >= 300:
        print("!! WRITE FAILED", r.status_code, r.text[:300]); return

    # read-back verify
    back = requests.get(f"{base}/rest/v1/jarvis_settings",
                        params={"id": "eq.mase_deal_sweep", "select": "system_prompt"},
                        headers=h, verify=False, timeout=40).json()[0]["system_prompt"]
    ok = (len(back) == len(new)) and (GUARDRAIL.split(".")[0] in back)
    print(f"[write] OK. live prompt now {len(back)} chars (was {len(cur)}); guardrail present: {ok}")
    print(f"[revert] to undo: write the backup file back to jarvis_settings id=mase_deal_sweep")


if __name__ == "__main__":
    main()
