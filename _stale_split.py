import json, sys
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass
stale = json.load(open("cc_work/_stale_deals.json", encoding="utf-8"))
DEAD = {"closed lost", "closed won", "qualified out", "no decision", "omitted", "disqualified", "dead", "lost"}
live = [s for s in stale if str(s.get("stage") or "").strip().lower() not in DEAD]
dead = [s for s in stale if str(s.get("stage") or "").strip().lower() in DEAD]
live_na = [s for s in live if "new-activity" in s["reason"]]
print(f"STALE total: {len(stale)}  |  LIVE (actionable): {len(live)}  |  DEAD (just needs mark-dead): {len(dead)}")
print(f"LIVE with NEW ACTIVITY missed: {len(live_na)}")
json.dump([s["opp_id"] for s in live], open("cc_work/_stale_live_opps.json", "w"), indent=2)
print(f"\n=== {len(live)} LIVE stale deals (SFDC newer, need refresh) ===")
for s in live:
    print(f"  {str(s['account'])[:34]:34} [{str(s['stage'])[:16]:16}] +{s['days_newer']:>5}d  {s['reason']}")
print("\nwrote cc_work/_stale_live_opps.json")
