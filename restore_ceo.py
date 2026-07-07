"""RESTORE CEO supervision from ceo_attention_export.csv (the prior working state: 38 deals /
44 reasons). My re-sweep recomputed ceo_intervention from scratch and wiped watches the fresh
LLM didn't re-detect (Techtronic's scope-shrink, etc.). This rebuilds ai.ceo_intervention.reasons[]
from the saved export and re-applies it, refreshing win/mom from the CURRENT deterministic scores.
Opp-scoped jsonb_set. Dry-run by default; --apply to write."""
import csv, json, re, sys
from collections import defaultdict
import requests, urllib3
from daily_summary.common import load_secret, VERIFY, id15
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _b(v):
    return str(v or "").strip().lower() in ("true", "1", "yes")


# human reason label (in the export) -> machine type
_TYPE = {"ceo to act": "support", "our-side slip": "our_slip", "large deal slowing": "large_slowdown",
         "competitor ahead": "competitor_edge", "scope shrinking": "scope_shrink",
         "scope shrink": "scope_shrink", "scope reduced": "scope_shrink"}


def _mtype(label):
    s = (label or "").strip().lower()
    return _TYPE.get(s, s.replace(" ", "_").replace("-", "_") or "support")


def main():
    apply = "--apply" in sys.argv
    rows = list(csv.DictReader(open("ceo_attention_export.csv", encoding="utf-8-sig")))

    sec = load_secret(); base = sec["SUPABASE_URL"].rstrip("/")
    key = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
    ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", sec["SUPABASE_URL"]).group(1)
    mgmt = f"https://api.supabase.com/v1/projects/{ref}/database/query"; token = sec["SUPABASE_ACCESS_TOKEN"]
    h = {"apikey": key, "Authorization": f"Bearer {key}"}
    # account_name -> opp_id (the export has account names, not ids)
    allrecs = requests.get(f"{base}/rest/v1/deal_records", params={"select": "opp_id,account_name", "active": "eq.true"},
                           headers=h, verify=VERIFY, timeout=120).json()
    acct2oid = {}
    for r in allrecs:
        if r.get("account_name"):
            acct2oid[r["account_name"].strip().lower()] = id15(r["opp_id"])
    by = defaultdict(list)
    missed = set()
    for r in rows:
        oid = id15(r.get("opp_id") or "") or acct2oid.get((r.get("account") or "").strip().lower())
        if oid:
            by[oid].append(r)
        else:
            missed.add(r.get("account"))
    print(f"export: {len(rows)} reasons | matched {len(by)} deals | unmatched accounts: {sorted(missed)[:6]}")
    # current win/mom per opp (refresh from deterministic scores)
    winmom = {}
    recs = requests.get(f"{base}/rest/v1/deal_records",
                        params={"opp_id": "in.(" + ",".join(by.keys()) + ")", "select": "opp_id,record"},
                        headers=h, verify=VERIFY, timeout=90).json()
    for r in recs:
        hl = ((r.get("record") or {}).get("ai") or {}).get("deal_scores", {}).get("headline", {}) or {}
        winmom[id15(r["opp_id"])] = (hl.get("win_position"), hl.get("deal_momentum"))

    out = {}
    for oid, rs in by.items():
        reasons = []
        for r in rs:
            typ = _mtype(r.get("reason_type"))
            act = _b(r.get("is_action")) or typ == "support"
            reason = {"type": typ, "act": act,
                      "severity": (r.get("reason_priority") or "medium").strip() or "medium",
                      "summary": (r.get("headline") or "").strip(),
                      "detail": (r.get("detail") or "").strip() or None,
                      "metric": (r.get("metric") or "").strip() or None,
                      "owner": (r.get("reason_owner") or "").strip() or None,
                      "as_of": (r.get("as_of") or "").strip() or None}
            cta = (r.get("ceo_action_or_ask") or "").strip()
            if act:
                reason["ceo_action"] = cta or None
                lev = (r.get("ceo_levers") or "").strip()
                if lev:
                    reason["areas"] = [x.strip() for x in re.split(r"[;,/]", lev) if x.strip()]
                bt = (r.get("buyer_target") or "").strip()
                if bt:
                    reason["buyer_target"] = {"name": bt}
            else:
                reason["ceo_ask"] = cta or None
            ev = (r.get("evidence") or "").strip()
            if ev:
                reason["evidence"] = ev
            reasons.append({k: v for k, v in reason.items() if v is not None})
        needed = bool(reasons)
        severity = "high" if any(x.get("severity") == "high" for x in reasons) else "medium"
        needs_action = any(x.get("type") == "support" for x in reasons)
        w, m = winmom.get(oid, (None, None))
        out[oid] = {"needed": needed, "severity": severity if needed else None,
                    "needs_action": needs_action, "reasons": reasons,
                    "win": w, "mom": m, "source": "restored_v1", "generated_at": "2026-07-07"}

    print(f"reconstructed CEO for {len(out)} deals")
    for oid in list(out)[:4]:
        d = out[oid]
        print(f"  {oid} needed={d['needed']} act={d['needs_action']} reasons={[x['type'] for x in d['reasons']]}")
    if not apply:
        print("\n[DRY RUN] pass --apply to write."); return
    blob = json.dumps(out)
    sql = ("update deal_records d set record = jsonb_set(record,'{ai,ceo_intervention}', m.value, true), "
           "updated_at = now() from (select key as opp_id, value from jsonb_each($J$" + blob + "$J$::jsonb)) m "
           "where d.opp_id = m.opp_id returning d.opp_id")
    r = requests.post(mgmt, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                      json={"query": sql}, verify=VERIFY, timeout=120)
    print("APPLIED:", len(r.json()) if r.status_code < 300 else (r.status_code, r.text[:200]))


if __name__ == "__main__":
    main()
