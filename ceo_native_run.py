"""Run the DEPLOYED native CEO finalizer over all forecasted opps, locally ($0):
feed each record's real scores + the v3 content (as the LLM emit) through
deal_engine_ceo.finalize_ceo_intervention, then upsert ai.ceo_intervention to prod.
Faithful to what the native sweep produces; stamps source="sweep"."""
import json, glob, re, sys, requests, urllib3
from daily_summary.common import load_secret, sf_login, soql
import deal_engine_ceo as CEO
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sec = load_secret()
BASE = sec["SUPABASE_URL"].rstrip("/")
KEY = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
REF = re.search(r"https://([a-z0-9]+)\.supabase\.co", sec["SUPABASE_URL"]).group(1)
MGMT = f"https://api.supabase.com/v1/projects/{REF}/database/query"
TOKEN = sec["SUPABASE_ACCESS_TOKEN"]
SBH = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}
FC = {"commit", "best case", "upside key deal"}


def sfdc_contacts(sid, inst, opp):
    try:
        rows = soql(sid, inst, f"SELECT Contact.Name, Contact.Title, Contact.Email, Role FROM OpportunityContactRole WHERE OpportunityId='{opp}'")
    except Exception:
        return []
    out = []
    for r in rows:
        c = r.get("Contact") or {}
        if c.get("Name"):
            out.append({"name": c.get("Name"), "title": c.get("Title"),
                        "email": c.get("Email"), "role": r.get("Role")})
    return out


def _gen():
    import datetime
    return datetime.date.today().isoformat()


def _apply_chunked(out: dict) -> int:
    items = list(out.items())
    total = 0
    for i in range(0, len(items), 150):
        chunk = dict(items[i:i + 150])
        blob = json.dumps(chunk)
        sql = ("update deal_records d set record = jsonb_set(record,'{ai,ceo_intervention}', m.value, true), "
               "updated_at = now() from (select key as opp_id, value from jsonb_each($J$" + blob + "$J$::jsonb)) m "
               "where d.opp_id = m.opp_id returning d.opp_id")
        resp = requests.post(MGMT, headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
                             json={"query": sql}, verify=False, timeout=120)
        if resp.status_code >= 300:
            print("APPLY FAILED", resp.status_code, resp.text[:300]); break
        total += len(resp.json())
    return total


def main():
    all_opps = "--all" in sys.argv
    rows = requests.get(f"{BASE}/rest/v1/deal_records",
                        params={"select": "opp_id,forecast_category,active,record"},
                        headers=SBH, verify=False, timeout=120).json()
    active = [r for r in rows if r.get("active")]
    fc = [r for r in active if (r.get("forecast_category") or "").strip().lower() in FC]
    scope = active if all_opps else fc
    print(f"scope: {'ALL active opps' if all_opps else 'forecasted only'} = {len(scope)} "
          f"({len(fc)} forecasted, {len(scope)-len(fc)} non-forecasted)")

    verdicts = {}
    for fp in glob.glob("ceo_verdicts/*.json"):
        try:
            v = json.load(open(fp, encoding="utf-8"))
            verdicts[v["opp_id"]] = v
        except Exception:
            pass
    print(f"AI discriminator verdicts (eligible deals): {len(verdicts)}")

    sid, inst = sf_login(sec)
    out = {}
    passers = 0
    gen = _gen()
    for r in scope:
        opp = r["opp_id"]
        fcat = (r.get("forecast_category") or "").strip().lower()
        # FAST PATH: a non-forecasted opp can never clear the floor -> needed:false,
        # no record load / SF call / finalizer needed.
        if fcat not in FC:
            out[opp] = {"needed": False, "win": None, "mom": None,
                        "source": "sweep", "generated_at": gen}
            continue
        rec = r.get("record") or {}
        ai = rec.setdefault("ai", {})
        hard = rec.get("hard") or {}
        if opp in verdicts:
            ai["ceo_intervention"] = {k: v for k, v in verdicts[opp].items()
                                      if k not in ("win", "mom", "source", "generated_at")}
        else:
            ai.pop("ceo_intervention", None)
        opp_snap = {"forecast_category": r.get("forecast_category"),
                    "amount": hard.get("amount"), "owner_name": hard.get("owner_name"),
                    "manager_name": hard.get("manager_name")}
        buyer = {"contacts": sfdc_contacts(sid, inst, opp)} if opp in verdicts else None
        prior = {"ceo_intervention": (rec.get("ai") or {}).get("ceo_intervention")}
        CEO.finalize_ceo_intervention(rec, opp_snap, buyer, prior_ai=prior)
        ci = rec["ai"]["ceo_intervention"]
        out[opp] = ci
        if ci.get("needed"):
            passers += 1

    n = _apply_chunked(out)
    print(f"APPLIED (native finalizer, source=sweep): {n} rows | needed=true {passers} | needed=false {len(out)-passers}")


if __name__ == "__main__":
    main()
