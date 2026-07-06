"""Make every analyzed deal HUMAN-READABLE in the Scores & Reasons panel.

The drawer renders the raw math breakdown (non-human-readable) when a deal has AI
analysis but is missing deal_scores and/or the cro_panel. This backfill, for each
active deal that HAS analysis (meddpicc/competitive/champion) but is missing
deal_scores.headline or cro_panel.blocks, computes them DETERMINISTICALLY (no LLM):
  compute_deal_scores(record)  -> headline + per-factor contributions
  build_cro_panel(record)      -> the plain-English ✅/⚠️ bullets + "how it adds up"
and stores ai.deal_scores (incl. cro_panel). Pinned/dead deals are left alone.
Opp-scoped jsonb_set via the Supabase Management API. Local, $0.
"""
from __future__ import annotations
import os, re, json, sys
import requests, urllib3
from daily_summary.common import load_secret, VERIFY, id15
import deal_engine_scoring as SC
import deal_engine_cro as CRO

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _has_analysis(ai):
    return bool(ai.get("meddpicc") or ai.get("competitive_position") or ai.get("champion_strength")
               or ai.get("recommended_moves"))


def _panel_blocks(ds):
    return len((ds.get("cro_panel") or {}).get("blocks") or [])


def main():
    apply = "--apply" in sys.argv
    sec = load_secret()
    base = sec["SUPABASE_URL"].rstrip("/")
    key = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
    ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", sec["SUPABASE_URL"]).group(1)
    mgmt = f"https://api.supabase.com/v1/projects/{ref}/database/query"
    token = sec["SUPABASE_ACCESS_TOKEN"]

    rows = requests.get(f"{base}/rest/v1/deal_records",
                        params={"select": "opp_id,account_name,record", "active": "eq.true"},
                        headers={"apikey": key, "Authorization": f"Bearer {key}"},
                        verify=VERIFY, timeout=150).json()

    out = {}
    fixed_scores = fixed_panel = skip_pinned = skip_noanalysis = skip_ok = 0
    for r in rows:
        rec = r.get("record") or {}
        ai = rec.get("ai") or {}
        ds = ai.get("deal_scores") or {}
        has_head = (ds.get("headline") or {}).get("win_position") is not None
        has_panel = _panel_blocks(ds) > 0
        if has_head and has_panel:
            skip_ok += 1; continue
        if ai.get("pinned"):
            skip_pinned += 1; continue
        if not _has_analysis(ai):
            skip_noanalysis += 1; continue
        try:
            if not has_head:
                sc = SC.compute_deal_scores(rec)
                if not sc or (sc.get("headline") or {}).get("win_position") is None:
                    skip_noanalysis += 1; continue
                rec.setdefault("ai", {})["deal_scores"] = sc
                ds = sc; fixed_scores += 1
            panel = CRO.build_cro_panel(rec)
            if panel:
                ds["cro_panel"] = panel
                fixed_panel += 1
            out[id15(r["opp_id"])] = ds
        except Exception as e:
            print("  failed", r["opp_id"], type(e).__name__, e)

    print(f"active: {len(rows)} | already-OK: {skip_ok} | computed scores: {fixed_scores} | "
          f"added panel: {fixed_panel} | to-write: {len(out)} | skip pinned: {skip_pinned} | skip no-analysis: {skip_noanalysis}")
    if not apply:
        print("[DRY RUN] pass --apply to write. sample deals:")
        for opp in list(out)[:6]:
            hl = (out[opp].get("headline") or {})
            print(f"   {opp} win={hl.get('win_position')} mom={hl.get('deal_momentum')} panel_blocks={_panel_blocks(out[opp])}")
        return

    items = list(out.items())
    total = 0
    for i in range(0, len(items), 40):
        blob = json.dumps(dict(items[i:i + 40]))
        sql = ("update deal_records d set record = jsonb_set(record,'{ai,deal_scores}', m.value, true), "
               "updated_at = now() from (select key as opp_id, value from jsonb_each($J$" + blob + "$J$::jsonb)) m "
               "where d.opp_id = m.opp_id returning d.opp_id")
        resp = requests.post(mgmt, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                             json={"query": sql}, verify=VERIFY, timeout=120)
        if resp.status_code >= 300:
            print("APPLY FAILED", resp.status_code, resp.text[:300]); break
        total += len(resp.json())
    print(f"APPLIED: {total} deal_scores written (scores+panel now human-readable)")


if __name__ == "__main__":
    main()
