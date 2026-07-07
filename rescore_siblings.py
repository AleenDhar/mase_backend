"""Recompute deal_scores + CRO panel for EVERY deal on a multi-deal account (so the relationship
leverage / foothold credit lands), regardless of forecast_critical — these specific deals are the
ones the user is asking to fix. Pinned deals are skipped (carry-forward protects them). Run AFTER
stamp_account_context.py --apply so ai.account_context is fresh. --apply writes; else dry-run."""
import sys, re, json
from collections import defaultdict
import requests, urllib3
import deal_engine_scoring as SC, deal_engine_cro as CRO
from daily_summary.common import load_secret, VERIFY, id15
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

APPLY = "--apply" in sys.argv
sec = load_secret(); SB = sec["SUPABASE_URL"].rstrip("/")
KEY = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}
REF = re.search(r"https://([a-z0-9]+)\.supabase\.co", sec["SUPABASE_URL"]).group(1)
MGMT = f"https://api.supabase.com/v1/projects/{REF}/database/query"; MTOK = sec["SUPABASE_ACCESS_TOKEN"]


def norm(s):
    s = str(s or "").lower()
    s = re.sub(r"\b(inc|incorporated|ltd|limited|llc|llp|corp|corporation|co|company|gmbh|ag|plc|pty|pte|the|of|and)\b", " ", s)
    return re.sub(r"\W+", "", s)


rows = requests.get(f"{SB}/rest/v1/deal_records", params={"select": "opp_id,account_name,opp_name,record", "active": "eq.true", "limit": "900"},
                    headers=H, verify=VERIFY, timeout=180).json()
by = defaultdict(list)
for r in rows:
    by[norm(r.get("account_name"))].append(r)
sib = [r for v in by.values() if len(v) >= 2 for r in v]
print(f"multi-deal-account deals: {len(sib)} (across {sum(1 for v in by.values() if len(v)>=2)} accounts)")

out, rep = {}, []
for r in sib:
    rec = r.get("record") or {}; ai = rec.get("ai") or {}
    if (ai.get("deal_scores") or {}).get("pinned") or ai.get("pinned"):
        rep.append((r.get("opp_name"), "PINNED-skip", "", "")); continue
    old = (ai.get("deal_scores") or {}).get("headline", {}).get("win_position")
    sc = SC.compute_deal_scores(rec)
    nw = (sc.get("headline") or {}).get("win_position")
    if nw is None:
        continue
    rec.setdefault("ai", {})["deal_scores"] = sc
    p = CRO.build_cro_panel(rec)
    if p:
        sc["cro_panel"] = p
    out[r["opp_id"]] = sc
    rel = any(c.get("factor") == "relationship_leverage" for c in (sc.get("win_position") or {}).get("contributions") or [])
    if old != nw or rel:
        rep.append((r.get("opp_name"), old, nw, "REL+10" if rel else ""))

for nm, o, n, tag in sorted(rep, key=lambda x: str(x[0])):
    print(f"  {str(nm)[:32]:32} {str(o):>6} -> {str(n):<6} {tag}")
if not APPLY:
    print(f"\n[DRY RUN] {len(out)} deals would be written. --apply to write."); sys.exit()

items = list(out.items()); n = 0
for i in range(0, len(items), 50):
    blob = json.dumps(dict(items[i:i + 50]))
    sql = ("update deal_records d set record = jsonb_set(record,'{ai,deal_scores}', m.value, true), updated_at=now() "
           "from (select key as opp_id, value from jsonb_each($J$" + blob + "$J$::jsonb)) m where d.opp_id=m.opp_id returning d.opp_id")
    resp = requests.post(MGMT, headers={"Authorization": f"Bearer {MTOK}", "Content-Type": "application/json"},
                         json={"query": sql}, verify=VERIFY, timeout=120)
    n += len(resp.json()) if resp.status_code < 300 else 0
print(f"\nAPPLIED: {n} sibling deals rescored")
