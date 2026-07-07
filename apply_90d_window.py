"""Patch the LIVE mase_deal_sweep prompt (Supabase jarvis_settings id=mase_deal_sweep):
STRICT 90-DAY EVIDENCE WINDOW for everything presented as the deal's CURRENT state.
Root cause: ACEN's drawer narrated Jun-2025 down-select / late-2025 pauses as if current
("looking at data from '25"). Idempotent (marker-guarded); backs up the prior prompt."""
import datetime, os, sys
import requests, urllib3
from daily_summary.common import load_secret, VERIFY
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

MARK = "## EVIDENCE WINDOW (STRICT"
BLOCK = """

## EVIDENCE WINDOW (STRICT — 2026-07-07)
Everything you present as the deal's CURRENT state — What matters, competition, commercials,
last meeting, stakeholder posture, momentum notes, the verdict — must be grounded in the
LAST 90 DAYS of evidence. Anything older is BACKGROUND only: at most ONE clearly-dated line
(e.g. "Background: down-selected to a final two (Jun 2025)"), never told as the live story.
If the last 90 days are thin, SAY the deal has been quiet for N days and what the most recent
concrete touch was — do NOT fill the gap by narrating months-old history as current motion.
Recent movement (stage/forecast upgrades, fresh buyer replies, new meetings) ALWAYS outranks
old history in every summary you write.
"""


def main():
    apply = "--apply" in sys.argv
    sec = load_secret(); base = sec["SUPABASE_URL"].rstrip("/")
    key = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
    h = {"apikey": key, "Authorization": f"Bearer {key}"}
    cur = requests.get(f"{base}/rest/v1/jarvis_settings",
                       params={"id": "eq.mase_deal_sweep", "select": "system_prompt"},
                       headers=h, verify=VERIFY, timeout=40).json()
    if not cur:
        print("prompt row missing"); return
    text = cur[0].get("system_prompt") or ""
    print(f"current prompt: {len(text)} chars | window-marker present: {MARK in text}")
    if MARK in text:
        print("already patched — nothing to do"); return
    new = text.rstrip() + BLOCK
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    bk = os.path.join(os.path.expanduser("~"), f"mase_deal_sweep_prompt_backup_{stamp}.md")
    open(bk, "w", encoding="utf-8").write(text)
    print(f"backup -> {bk}")
    if not apply:
        print("[DRY RUN] pass --apply to write."); return
    r = requests.post(f"{base}/rest/v1/jarvis_settings", params={"on_conflict": "id"},
                      headers={**h, "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates"},
                      json={"id": "mase_deal_sweep", "system_prompt": new,
                            "updated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
                      verify=VERIFY, timeout=40)
    print("APPLIED:", r.status_code)


if __name__ == "__main__":
    main()
