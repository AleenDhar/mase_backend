"""Recompute new-logic deal_scores + human-readable cro_panel for the 3 named deals and
store them (opp-scoped jsonb_set). Deterministic; guarantees the human-readable panel."""
import json, re, sys
import requests, urllib3
from daily_summary.common import load_secret, VERIFY, id15
import deal_engine_scoring as SC
import deal_engine_cro as CRO
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TARGETS = [("006P700000JwvB3", "Bright Horizons"), ("006P700000J71MD", "Austrian Post"),
           ("006P700000PlMpu", "Bosch")]

sec = load_secret()
base = sec["SUPABASE_URL"].rstrip("/")
key = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", sec["SUPABASE_URL"]).group(1)
mgmt = f"https://api.supabase.com/v1/projects/{ref}/database/query"
token = sec["SUPABASE_ACCESS_TOKEN"]
h = {"apikey": key, "Authorization": f"Bearer {key}"}

out = {}
for oid, nm in TARGETS:
    r = requests.get(base + "/rest/v1/deal_records",
                     params={"opp_id": "like." + oid + "*", "select": "opp_id,record"},
                     headers=h, verify=VERIFY, timeout=60).json()
    rec = r[0]["record"]; oid15 = id15(r[0]["opp_id"])
    sc = SC.compute_deal_scores(rec)
    if not sc or (sc.get("headline") or {}).get("win_position") is None:
        print(nm, "-> no win, skip"); continue
    rec.setdefault("ai", {})["deal_scores"] = sc
    panel = CRO.build_cro_panel(rec)
    if panel:
        sc["cro_panel"] = panel
    out[oid15] = sc
    print(f"{nm:16} win={sc['headline']['win_position']} panel_blocks={len((panel or {}).get('blocks') or [])}")

blob = json.dumps(out)
sql = ("update deal_records d set record = jsonb_set(record,'{ai,deal_scores}', m.value, true), "
       "updated_at = now() from (select key as opp_id, value from jsonb_each($J$" + blob + "$J$::jsonb)) m "
       "where d.opp_id = m.opp_id returning d.opp_id")
resp = requests.post(mgmt, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                     json={"query": sql}, verify=VERIFY, timeout=90)
print("STORED:", len(resp.json()) if resp.status_code < 300 else (resp.status_code, resp.text[:200]))
