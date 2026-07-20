"""Recompute deal_scores + cro_panel for ONLY the deals freshly swept this session
(those with a cc_work/<oid>.final.json = fresh footprints, so a recompute is SAFE — no
stale-footprint divergence). Applies the recent scoring fixes (displaced-incumbent
competitive, scope-shrink) + the CRO panel (CRO-friendly, no Commitment). Dry-run by
default; --apply to write."""
import json, os, re, sys
import requests, urllib3
from daily_summary.common import load_secret, VERIFY, id15
import deal_engine_scoring as SC
import deal_engine_cro as CRO
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def main():
    apply = "--apply" in sys.argv
    only = next((a.split("=", 1)[1].lower() for a in sys.argv if a.startswith("--account=")), None)
    swept = {f[:-11] for f in os.listdir("cc_work") if f.endswith(".final.json")}
    print(f"freshly-swept opps (fresh footprints): {len(swept)}")
    sec = load_secret()
    base = sec["SUPABASE_URL"].rstrip("/")
    key = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
    ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", sec["SUPABASE_URL"]).group(1)
    mgmt = f"https://api.supabase.com/v1/projects/{ref}/database/query"
    token = sec["SUPABASE_ACCESS_TOKEN"]
    rows = requests.get(f"{base}/rest/v1/deal_records",
                        params={"select": "opp_id,account_name,record", "active": "eq.true"},
                        headers={"apikey": key, "Authorization": f"Bearer {key}"}, verify=VERIFY, timeout=180).json()
    out, deltas = {}, []
    for r in rows:
        oid = id15(r["opp_id"])
        if oid not in swept:
            continue
        if only and only not in str(r.get("account_name") or "").lower():
            continue
        rec = r.get("record") or {}
        ai = rec.get("ai") or {}
        if ai.get("pinned"):
            continue
        old = ((ai.get("deal_scores") or {}).get("headline") or {}).get("win_position")
        try:
            sc = SC.compute_deal_scores(rec)
            if not sc or (sc.get("headline") or {}).get("win_position") is None:
                continue
            rec.setdefault("ai", {})["deal_scores"] = sc
            panel = CRO.build_cro_panel(rec)
            if panel:
                sc["cro_panel"] = panel
            out[oid] = sc
            new = sc["headline"]["win_position"]
            if old is not None and abs(float(new) - float(old)) >= 0.5:
                deltas.append((r.get("account_name"), old, new))
        except Exception as e:
            print("  failed", oid, type(e).__name__, str(e)[:100])

    print(f"to write: {len(out)} | win changed: {len(deltas)}")
    for acct, o, n in sorted(deltas, key=lambda x: abs(float(x[2]) - float(x[1])), reverse=True)[:14]:
        print(f"   {str(acct)[:32]:32} {o} -> {n}")
    if not apply:
        print("\n[DRY RUN] pass --apply to write.")
        return
    items = list(out.items())
    total = 0
    for i in range(0, len(items), 40):
        blob = json.dumps(dict(items[i:i + 40]))
        sql = ("update deal_records d set record = jsonb_set(record,'{ai,deal_scores}', m.value, true), "
               "updated_at = now() from (select key as opp_id, value from jsonb_each($J$" + blob + "$J$::jsonb)) m "
               "where d.opp_id = m.opp_id returning d.opp_id")
        resp = requests.post(mgmt, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                             json={"query": sql}, verify=VERIFY, timeout=150)
        if resp.status_code >= 300:
            print("APPLY FAILED", resp.status_code, resp.text[:300]); break
        total += len(resp.json())
    print(f"\nAPPLIED: {total} freshly-swept deals recomputed")


if __name__ == "__main__":
    main()
