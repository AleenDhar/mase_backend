"""Recompute deterministic deal_scores + cro_panel for records the re-sweep left malformed
(raw LLM sections but no deal_scores/panel — the gate-exhausted persist skipped post-processing).
Targeted, opp-scoped. Dry-run prints the win breakdown; --apply writes."""
import sys, re, json
import requests, urllib3
import deal_engine_scoring as SC, deal_engine_cro as CRO
from daily_summary.common import load_secret, VERIFY, id15
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

IDS = [x for x in (sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else
                   "006P700000JwvB3,006P700000CWvfN").split(",") if x]


def main():
    apply = "--apply" in sys.argv
    sec = load_secret(); base = sec["SUPABASE_URL"].rstrip("/")
    key = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
    ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", sec["SUPABASE_URL"]).group(1)
    mgmt = f"https://api.supabase.com/v1/projects/{ref}/database/query"; token = sec["SUPABASE_ACCESS_TOKEN"]
    h = {"apikey": key, "Authorization": f"Bearer {key}"}
    out = {}
    for oid in IDS:
        r = requests.get(f"{base}/rest/v1/deal_records", params={"opp_id": "eq." + oid, "select": "account_name,stage,forecast_category,record"},
                         headers=h, verify=VERIFY, timeout=40).json()
        if not r:
            print(oid, "(no record)"); continue
        rec = r[0]["record"]
        sc = SC.compute_deal_scores(rec)
        if not (sc and (sc.get("headline") or {}).get("win_position") is not None):
            print(oid, "scoring produced no headline — skip"); continue
        rec.setdefault("ai", {})["deal_scores"] = sc
        panel = CRO.build_cro_panel(rec)
        if panel:
            sc["cro_panel"] = panel
        out[oid] = sc
        hl = sc.get("headline", {})
        win = (sc.get("win_position") or {})
        print(f"\n{r[0]['account_name']} | stage={r[0]['stage']} forecast={r[0]['forecast_category']}")
        print(f"   headline: win={hl.get('win_position')} mom={hl.get('deal_momentum')} risk={hl.get('deal_risk')}")
        # win breakdown (contributions if present)
        contribs = win.get("contributions") or win.get("breakdown") or win.get("factors")
        if contribs:
            print("   win breakdown:", json.dumps(contribs)[:400])
        for k in ("anchor", "ceiling", "adj", "momentum_adj", "scope_pts", "forecast_credit", "risk_penalty", "selection_override"):
            if k in win:
                print(f"     {k} = {win.get(k)}")
    if not apply:
        print("\n[DRY RUN] pass --apply to write."); return
    blob = json.dumps(out)
    sql = ("update deal_records d set record = jsonb_set(record,'{ai,deal_scores}', m.value, true), updated_at = now() "
           "from (select key as opp_id, value from jsonb_each($J$" + blob + "$J$::jsonb)) m where d.opp_id = m.opp_id returning d.opp_id")
    resp = requests.post(mgmt, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                         json={"query": sql}, verify=VERIFY, timeout=120)
    print("\nAPPLIED:", len(resp.json()) if resp.status_code < 300 else (resp.status_code, resp.text[:200]))


if __name__ == "__main__":
    main()
