"""Append an ANTI-FABRICATION guard to the LIVE mase_deal_sweep prompt (Supabase). Stops the
model asserting a negative meeting fact ('the CPO never showed up', 'left unresolved') from
missing/partial coverage — the Austrian Post 1-Jul onsite failure (Teil 2, where the CPO spoke,
had been dropped to notes-only). Backs up outside the repo; inserts before '## 3. The North Star';
verifies additive; idempotent; reversible. Run with --apply to write."""
import sys, os, datetime, requests, urllib3
from daily_summary.common import load_secret
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ANCHOR = "## 3. The North Star"
MARKER = "ANTI-FABRICATION — never state what you did not read"
BLOCK = (
    "## 2.12 " + MARKER + " (2026-07-08)\n\n"
    "The worst failure is INVENTING what happened in a meeting you did not fully read (it is how "
    "a summary came to say 'the invited CPO never showed up' on an onsite whose second part — where "
    "the CPO actually spoke — had not been read). Hard rules, no exceptions:\n"
    "- NEVER assert a NEGATIVE meeting fact from missing data. Do NOT write that a person 'never "
    "showed up' / 'did not attend', that a topic 'was not discussed', or that an issue was 'left "
    "unresolved' UNLESS you READ the full transcript of that meeting and confirmed it. If a meeting "
    "carries only AI-NOTES (no verbatim transcript) or is a gap / not-recorded touchpoint, you have "
    "NOT fully read it — summarise what the notes state and treat everything else as UNKNOWN, never "
    "as 'did not happen'.\n"
    "- MULTI-PART MEETINGS ARE ONE MEETING. An onsite / workshop logged across several same-day "
    "recordings — 'Teil 1' + 'Teil 2', 'Part 1/2', 'Session 1/2', 'Day 1/2', '(1/2)' — is a SINGLE "
    "meeting. Read and summarise ALL its parts together. Someone absent from Part 1 may join in "
    "Part 2; never conclude anything about attendance or outcome from ONE part. If a part is missing "
    "from your manifest, say the meeting is only partially read — do not infer the missing part.\n"
    "- ABSENCE IS NOT ABSENCE-OF-EVENT. A name, price, decision or attendee not appearing in the "
    "slice you read is 'not seen in the evidence read', NEVER 'did not happen'. Attendee metadata is "
    "often incomplete (people walk into an onsite without being on the invite), so ground attendance "
    "in the transcript; when unsure, attribute by ROLE and flag it unconfirmed — never a false "
    "negative about a named person.\n"
    "- The day_summary and the critical_signals 'last meeting' read MUST describe ONLY what the read "
    "transcript / notes actually contain. If the most recent meeting was not deep-read, summarise "
    "from its notes and say coverage was partial — do not manufacture a narrative to fill the gap.\n\n")


def main():
    dry = "--apply" not in sys.argv
    sec = load_secret()
    base = sec["SUPABASE_URL"].rstrip("/")
    key = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
    h = {"apikey": key, "Authorization": f"Bearer {key}"}
    cur = requests.get(f"{base}/rest/v1/jarvis_settings",
                       params={"id": "eq.mase_deal_sweep", "select": "system_prompt"},
                       headers=h, verify=False, timeout=40).json()[0]["system_prompt"]
    print(f"[read] prompt {len(cur)} chars")
    if MARKER in cur:
        print("!! anti-fabrication block already present — abort (idempotent)."); return
    n = cur.count(ANCHOR)
    if n != 1:
        print(f"!! anchor {ANCHOR!r} appears {n}x — expected 1. ABORT."); return
    new = cur.replace(ANCHOR, BLOCK + ANCHOR, 1)
    assert new.replace(BLOCK, "", 1) == cur, "not purely additive"
    assert new.count(ANCHOR) == 1
    print(f"[verify] additive OK (+{len(BLOCK)} chars)")
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bk = os.path.join(os.path.expanduser("~"), f"mase_deal_sweep_prompt_backup_{stamp}.md")
    open(bk, "w", encoding="utf-8").write(cur)
    print(f"[backup] {bk}")
    if dry:
        print("\n[DRY RUN] re-run with --apply to write."); return
    r = requests.post(f"{base}/rest/v1/jarvis_settings", params={"on_conflict": "id"},
                      headers={**h, "Content-Type": "application/json",
                               "Prefer": "resolution=merge-duplicates,return=minimal"},
                      json={"id": "mase_deal_sweep", "system_prompt": new,
                            "updated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
                      verify=False, timeout=60)
    if r.status_code >= 300:
        print("!! WRITE FAILED", r.status_code, r.text[:300]); return
    back = requests.get(f"{base}/rest/v1/jarvis_settings",
                        params={"id": "eq.mase_deal_sweep", "select": "system_prompt"},
                        headers=h, verify=False, timeout=40).json()[0]["system_prompt"]
    print(f"[write] OK. prompt now {len(back)} chars; block present: {MARKER in back}")


if __name__ == "__main__":
    main()
