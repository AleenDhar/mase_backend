"""Append the CEO-help emit section to the LIVE deal-sweep prompt (Supabase
jarvis_settings id=mase_deal_sweep). Backs up outside the repo; pure APPEND before
'## 5. Output contract'; verifies additive. Reversible. Run with --apply to write."""
import sys, os, datetime, requests, urllib3
from daily_summary.common import load_secret
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ANCHOR = "## 5. Output contract"
SECTION = (
    "**CEO help (ai.ceo_intervention) — CEO-ONLY, the four levers.** When this is a strong "
    "FORECASTED deal (Commit / Best Case / Upside) where your win and momentum reads are BOTH "
    "high AND the deal genuinely needs the CEO — the single most senior Zycus leader — to step "
    "in PERSONALLY, emit ai.ceo_intervention. This is CEO help, NOT general executive help: the "
    "action is what the CEO PERSONALLY does — NEVER \"send a VP / SVP / delivery leader / account "
    "team\". The CEO's four levers (pick the 1-3 that actually move THIS deal):\n"
    "- pricing — the CEO approves a discount / pricing structure / commercial flexibility that is otherwise blocked.\n"
    "- product — the CEO commits to feature development or roadmap.\n"
    "- presales_resources — the CEO allocates or personally guarantees pre-sales / SE / POC / implementation resources.\n"
    "- exec_connect — the CEO opens a CEO-to-executive relationship (Zycus CEO -> the buyer's CEO / CFO / CPO / CIO) to reach or align the economic buyer.\n"
    "Shape: { \"needed\": true, \"priority\": \"high\"|\"medium\", \"areas\": [levers], \"reason\": "
    "\"one sentence citing a real fact\", \"ceo_action\": \"what the CEO personally does\", "
    "\"buyer_target\": {\"name\",\"title\",\"engaged\"}, \"ceo_not_engaged\": true, "
    "\"lower_execs_engaged\": [{\"name\",\"title\"}] }. buyer_target is the economic buyer / budget "
    "owner the CEO connects to — take the NAME + TITLE from Salesforce (OpportunityContactRole / "
    "your MEDDPICC economic buyer), NEVER from a transcript; if Salesforce names no such person, "
    "set name=null and give the role. lower_execs_engaged = any Zycus execs already on calls (a "
    "CMO/VP here means the CEO has NOT gone in and cannot commit resources or make a CEO-peer "
    "connection). If the deal does NOT qualify, emit ai.ceo_intervention: { \"needed\": false }. "
    "The server re-checks the gate against the computed scores and corrects any unbacked name/"
    "title — so ground buyer_target and every title in Salesforce.\n\n")


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
    if "CEO help (ai.ceo_intervention)" in cur:
        print("!! CEO section already present — aborting (idempotent)."); return
    n = cur.count(ANCHOR)
    if n != 1:
        print(f"!! anchor '{ANCHOR}' appears {n}x — expected 1. ABORT."); return
    new = cur.replace(ANCHOR, SECTION + ANCHOR, 1)
    assert new.replace(SECTION, "", 1) == cur, "not purely additive"
    assert new.count(ANCHOR) == 1
    print(f"[verify] additive OK: +{len(SECTION)} chars")
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = os.path.join(os.path.expanduser("~"), f"mase_deal_sweep_prompt_backup_{stamp}.md")
    open(backup, "w", encoding="utf-8").write(cur)
    print(f"[backup] {backup}")
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
    print(f"[write] OK. live prompt now {len(back)} chars; CEO section present: {'CEO help (ai.ceo_intervention)' in back}")


if __name__ == "__main__":
    main()
