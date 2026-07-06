"""CEO-help v2 APPLY: stamp ai.ceo_intervention on ALL forecasted deals — the 12
gate-passers get their judge verdict, the rest get needed:false. Single opp-scoped
jsonb_set via the Supabase Management API (only touches that field). Read verdicts
from ceo_verdicts/*.json."""
import json, glob, re, requests, urllib3, datetime, os
from daily_summary.common import load_secret
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

sec = load_secret()
REF = re.search(r"https://([a-z0-9]+)\.supabase\.co", sec["SUPABASE_URL"]).group(1)
MGMT = f"https://api.supabase.com/v1/projects/{REF}/database/query"
TOKEN = sec["SUPABASE_ACCESS_TOKEN"]
BASE = sec["SUPABASE_URL"].rstrip("/")
KEY = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
SBH = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}
GEN = datetime.date.today().isoformat()
FC = {"commit", "best case", "upside key deal"}


def forecasted_scores():
    rows = requests.get(f"{BASE}/rest/v1/deal_records",
                        params={"select": "opp_id,account_name,forecast_category,active,record"},
                        headers=SBH, verify=False, timeout=90).json()
    out = {}
    for r in rows:
        if not (r.get("active") and (r.get("forecast_category") or "").strip().lower() in FC):
            continue
        hl = ((r.get("record") or {}).get("ai") or {}).get("deal_scores", {}).get("headline", {}) or {}
        out[r["opp_id"]] = {"account": r["account_name"], "win": hl.get("win_position"), "mom": hl.get("deal_momentum")}
    return out


def main():
    fc = forecasted_scores()
    verdicts = {}
    for fp in glob.glob("ceo_verdicts/*.json"):
        try:
            v = json.load(open(fp, encoding="utf-8"))
            if v.get("opp_id"):
                verdicts[v["opp_id"]] = v
        except Exception as e:
            print("  bad verdict", fp, e)
    print(f"forecasted={len(fc)}  verdicts loaded={len(verdicts)}")

    ceo = {}
    for opp, meta in fc.items():
        if opp in verdicts:
            v = dict(verdicts[opp])
            v.update({"needed": True, "win": meta["win"], "mom": meta["mom"],
                      "source": "workflow_v3", "generated_at": GEN})
            ceo[opp] = v
        else:
            ceo[opp] = {"needed": False, "win": meta["win"], "mom": meta["mom"],
                        "source": "workflow_v3", "generated_at": GEN}

    missing = [o for o in verdicts if o not in fc]
    if missing:
        print("  NOTE verdicts not in forecasted set (skipped):", missing)

    blob = json.dumps(ceo)
    sql = ("update deal_records d "
           "set record = jsonb_set(record, '{ai,ceo_intervention}', m.value, true), updated_at = now() "
           "from (select key as opp_id, value from jsonb_each($CEOJSON$" + blob + "$CEOJSON$::jsonb)) m "
           "where d.opp_id = m.opp_id returning d.opp_id")
    r = requests.post(MGMT, headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
                      json={"query": sql}, verify=False, timeout=120)
    if r.status_code >= 300:
        print("APPLY FAILED", r.status_code, r.text[:400]); return
    updated = r.json()
    n_true = sum(1 for v in ceo.values() if v["needed"])
    print(f"APPLIED: {len(updated)} rows updated | needed=true {n_true} | needed=false {len(ceo)-n_true} | source=workflow_v3 {GEN}")


if __name__ == "__main__":
    main()
