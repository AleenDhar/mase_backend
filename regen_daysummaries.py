"""Regenerate the `summary` on existing deal_daily_summaries rows as plain-English PROSE
(new deterministic_summary), from each row's STORED structured data — no SF pull, no LLM.
Skips Claude-authored rows. Opp+date-scoped UPDATE via the Supabase Management API.
Dry-run by default; --apply to write."""
import json, re, sys
import requests, urllib3
from daily_summary.common import load_secret, VERIFY
from daily_summary.extract import deterministic_summary
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def main():
    apply = "--apply" in sys.argv
    sec = load_secret()
    base = sec["SUPABASE_URL"].rstrip("/")
    key = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
    ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", sec["SUPABASE_URL"]).group(1)
    mgmt = f"https://api.supabase.com/v1/projects/{ref}/database/query"
    token = sec["SUPABASE_ACCESS_TOKEN"]
    h = {"apikey": key, "Authorization": f"Bearer {key}"}

    rows, off = [], 0
    while True:
        batch = requests.get(f"{base}/rest/v1/deal_daily_summaries",
                             params={"select": "opp_id,summary_date,owner_name,has_activity,counts,activities,movements,meetings_avoma,next_step_changed_at,summary,summary_source",
                                     "has_activity": "eq.true", "order": "summary_date.desc", "limit": 1000, "offset": off},
                             headers=h, verify=VERIFY, timeout=120).json()
        if not isinstance(batch, list) or not batch:
            break
        rows += batch
        off += len(batch)
        if len(batch) < 1000:
            break

    updates, samples = [], []
    for r in rows:
        if r.get("summary_source") == "claude":
            continue
        rec = {"has_activity": True, "owner_name": r.get("owner_name"),
               "counts": r.get("counts") or {}, "movements": r.get("movements") or [],
               "activities": r.get("activities") or [], "meetings_avoma": r.get("meetings_avoma") or [],
               "next_step_changed_at": r.get("next_step_changed_at")}
        new = deterministic_summary(rec)
        if new and new != (r.get("summary") or ""):
            updates.append({"opp_id": r["opp_id"], "summary_date": str(r["summary_date"]), "summary": new})
            if len(samples) < 6:
                samples.append((r.get("summary") or "", new))

    print(f"has_activity rows scanned: {len(rows)} | prose-rewritten: {len(updates)}")
    for old, new in samples:
        print(f"\n  OLD: {old[:150]}\n  NEW: {new[:170]}")
    if not apply:
        print("\n[DRY RUN] pass --apply to write.")
        return
    total = 0
    for i in range(0, len(updates), 200):
        blob = json.dumps(updates[i:i + 200])
        sql = ("update deal_daily_summaries d set summary = e->>'summary', updated_at = now() "
               "from jsonb_array_elements($J$" + blob + "$J$::jsonb) e "
               "where d.opp_id = e->>'opp_id' and d.summary_date = (e->>'summary_date')::date returning d.opp_id")
        resp = requests.post(mgmt, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                             json={"query": sql}, verify=VERIFY, timeout=120)
        if resp.status_code >= 300:
            print("APPLY FAILED", resp.status_code, resp.text[:300]); break
        total += len(resp.json())
    print(f"\nAPPLIED: {total} daily summaries rewritten as prose")


if __name__ == "__main__":
    main()
