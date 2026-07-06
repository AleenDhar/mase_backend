"""Backfill ai.deal_scores.cro_panel across all active deals by re-running the
(pure, deterministic, no-LLM) build_cro_panel so bullets gain the new `full` field
that powers the 'more' expander. Skips pinned panels. Opp-scoped jsonb_set via the
Supabase Management API. Local, $0."""
from __future__ import annotations
import os, re, json, sys, datetime as dt
import requests, urllib3
from daily_summary.common import load_secret, VERIFY, id15
import deal_engine_cro as cro

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _has_full(panel):
    for bl in (panel or {}).get("blocks") or []:
        for b in (bl.get("bullets") or []):
            if isinstance(b, dict) and b.get("full"):
                return True
    return False


def main():
    apply = "--apply" in sys.argv
    sec = load_secret()
    base = sec["SUPABASE_URL"].rstrip("/")
    key = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
    ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", sec["SUPABASE_URL"]).group(1)
    mgmt = f"https://api.supabase.com/v1/projects/{ref}/database/query"
    token = sec["SUPABASE_ACCESS_TOKEN"]

    rows = requests.get(f"{base}/rest/v1/deal_records",
                        params={"select": "opp_id,record", "active": "eq.true"},
                        headers={"apikey": key, "Authorization": f"Bearer {key}"},
                        verify=VERIFY, timeout=120).json()
    out = {}
    gained = pinned = skipped = 0
    for r in rows:
        rec = r.get("record") or {}
        ds = (rec.get("ai") or {}).get("deal_scores") or {}
        old = ds.get("cro_panel") or {}
        if not old:
            skipped += 1; continue
        if isinstance(old, dict) and old.get("pinned"):
            pinned += 1; continue
        try:
            panel = cro.build_cro_panel(rec)
        except Exception as e:
            print("  build failed", r["opp_id"], e); skipped += 1; continue
        if not panel:
            skipped += 1; continue
        out[id15(r["opp_id"])] = panel
        if _has_full(panel) and not _has_full(old):
            gained += 1
    print(f"active with cro_panel: {len(out)} | newly-gain 'full' bullets: {gained} | pinned(skip): {pinned} | no-panel(skip): {skipped}")
    if not apply:
        print("[DRY RUN] pass --apply to write."); return

    items = list(out.items())
    total = 0
    for i in range(0, len(items), 60):
        blob = json.dumps(dict(items[i:i + 60]))
        sql = ("update deal_records d set record = jsonb_set(record,'{ai,deal_scores,cro_panel}', m.value, true), "
               "updated_at = now() from (select key as opp_id, value from jsonb_each($J$" + blob + "$J$::jsonb)) m "
               "where d.opp_id = m.opp_id returning d.opp_id")
        resp = requests.post(mgmt, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                             json={"query": sql}, verify=VERIFY, timeout=120)
        if resp.status_code >= 300:
            print("APPLY FAILED", resp.status_code, resp.text[:300]); break
        total += len(resp.json())
    print(f"APPLIED: {total} cro_panels refreshed")


if __name__ == "__main__":
    main()
