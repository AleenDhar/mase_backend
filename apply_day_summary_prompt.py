"""Add ai.day_summary to the LIVE mase_deal_sweep prompt so the AWS/Sonnet sweep emits a
REAL 'what happened' summary (analysis, not a Salesforce dump): the most recent activity day,
each meeting/call/email NAMED + ONE line of what was discussed, plus an OVERALL day summary.
No raw email/subject/transcript dumps; NO 'what to do next' (that lives in the to-dos).

Two additive edits, both verified unique at runtime: (a) a §2.11 spec section before
'## 3. The North Star'; (b) the day_summary field in the output-contract JSON, after the
deal_movement line. Backup + dry-run; --apply to write. Idempotent."""
import sys, os, datetime, requests, urllib3
from daily_summary.common import load_secret
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SECTION_ANCHOR = "## 3. The North Star"
SCHEMA_ANCHOR = ('    "deal_movement": {"summary": "reads the pulse from Next_Step_History__c + field '
                 'history + last real buyer touch", "items": [{"change": "", "date": "YYYY-MM-DD"}]},')
MARKER = "## 2.11 Latest-day summary"

SECTION = (
    "## 2.11 Latest-day summary (ai.day_summary) — a REAL summary of what happened, not a data dump\n\n"
    "MASE is the ANALYSIS LAYER on top of Salesforce, not a second copy of it. Salesforce already holds "
    "the raw emails, call transcripts and activity rows; your job is to SUMMARISE what happened, in plain "
    "English a CRO reads in five seconds. Emit `ai.day_summary` covering the MOST RECENT day that had real "
    "deal activity (a buyer meeting/call, a substantive email either direction, or a real deal movement — "
    "NOT CRM housekeeping like an owner/probability edit). If several days are quiet, summarise the latest "
    "day that actually had something.\n\n"
    "Shape:\n"
    "`\"day_summary\": { \"as_of\": \"YYYY-MM-DD\", \"overall\": \"\", \"items\": [ {\"kind\": "
    "\"meeting|call|email|movement\", \"name\": \"\", \"summary\": \"\", \"at\": \"YYYY-MM-DD\"} ] }`\n\n"
    "Rules — this is the user's explicit ask, follow it exactly:\n"
    "- `overall`: 2-4 sentences telling the STORY of that day across everything that happened (who engaged, "
    "on what, where it moved) — a narrative, never a list, never a count-only line like 'Activity: 2 emails'.\n"
    "- `items`: ONE entry per real activity that day (cap ~6, most significant first). For each: `name` is a "
    "short human label ('Pricing review with Abe', not '[Clari - Email Sent] RE: ...'); `summary` is ONE line "
    "of WHAT WAS ACTUALLY DISCUSSED / decided / asked, drawn from the Avoma call notes or the email substance. "
    "If four meetings happened, name each and summarise each — do not collapse to '4 meetings'.\n"
    "- NEVER paste raw content: no `[Clari - Email Sent]`/`[Outreach]` prefixes, no verbatim email bodies, no "
    "transcript excerpts, no exact SFDC subject dumps. Summarise; Salesforce keeps the raw.\n"
    "- NEVER include recommendations or 'what to do next' / next steps / action items here — that lives ONLY "
    "in recommended_moves / the to-dos. day_summary is purely WHAT HAPPENED, past tense.\n"
    "- Keep it tight: no giant text blocks nobody reads. If a day genuinely had no real activity, set "
    "`items: []` and let `overall` say the deal was quiet (name the last real touch + when).\n\n")

SCHEMA_ADD = ('\n    "day_summary": {"as_of": "YYYY-MM-DD", "overall": "2-4 sentence narrative of the most '
              'recent active day — analysis, not a list", "items": [{"kind": "meeting|call|email|movement", '
              '"name": "short human label", "summary": "one line: what was discussed/decided/asked", '
              '"at": "YYYY-MM-DD"}]},')


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
    if MARKER in cur or "day_summary" in cur:
        print("!! day_summary already present — abort (idempotent)."); return
    for label, anc in (("section", SECTION_ANCHOR), ("schema", SCHEMA_ANCHOR)):
        n = cur.count(anc)
        if n != 1:
            print(f"!! {label} anchor appears {n}x (need 1). ABORT.\n    anchor: {anc[:80]!r}"); return
    new = cur.replace(SECTION_ANCHOR, SECTION + SECTION_ANCHOR, 1)
    new = new.replace(SCHEMA_ANCHOR, SCHEMA_ANCHOR + SCHEMA_ADD, 1)
    assert new.count("day_summary") >= 2, "expected both insertions"
    print(f"[verify] both edits applied. {len(cur)} -> {len(new)} chars (+{len(new)-len(cur)})")
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
    print(f"[write] OK. day_summary present: {'day_summary' in new}")


if __name__ == "__main__":
    main()
