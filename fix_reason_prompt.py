"""Patch the LIVE mase_deal_sweep prompt: reasons must describe the DEAL, never the
scoring inner working / logic (no stage cap, 'anchors at', 'earns roughly N', 'why this
number'). Replaces the two offending sentences my §2.10 block introduced. Backup + verify;
--apply to write."""
import sys, os, datetime, requests, urllib3
from daily_summary.common import load_secret
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

REPLACES = [
    ("- ANSWER 'WHY THIS NUMBER'. State plainly why the score sits where it does relative to its stage cap — the CMO/VP "
     "question. e.g. \"Shortlisted caps confidence at 70; it earns ~77 on fit + champion but we hold at 70 until Vendor "
     "Selected lands\" or \"56 not 70 because no economic buyer is engaged and the field is still narrowing.\"",
     "- DESCRIBE THE DEAL, NEVER THE SCORE MACHINERY. Explain the deal's real position — who is engaged, what is proven, "
     "what is missing or at risk — in plain language, using the deal's own facts. NEVER mention the scoring inner working "
     "or logic: NO stage cap / ceiling, NO \"anchors at\", NO \"earns roughly N\", NO \"why this number\", NO \"holds in the "
     "mid-50s\", NO rubric / weights / momentum-lift / \"Shortlisted caps confidence\". The stage cap still limits the number "
     "internally, but it is NEVER spoken in a reason. Say why the deal stands where it does with deal facts (e.g. \"no economic "
     "buyer is engaged and the field is still narrowing to two\"), not the mechanics of the score."),
    ("win_position MUST lead with a \"why this number\" bullet and MUST include 1-2 `warn` risk bullets.",
     "win_position MUST lead with the deal's overall read as a DEAL-FACT sentence (never the score mechanics) and MUST "
     "include 1-2 `warn` risk bullets."),
]


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
    if "DESCRIBE THE DEAL, NEVER THE SCORE MACHINERY" in cur:
        print("!! already patched — abort (idempotent)."); return
    new = cur
    for old, rep in REPLACES:
        n = new.count(old)
        if n != 1:
            print(f"!! expected 1 occurrence, found {n} for: {old[:60]!r} ABORT."); return
        new = new.replace(old, rep, 1)
    print(f"[verify] both replacements applied. {len(cur)} -> {len(new)} chars")
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
    print(f"[write] OK. patched: {'DESCRIBE THE DEAL, NEVER THE SCORE MACHINERY' in new}")


if __name__ == "__main__":
    main()
