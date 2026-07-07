"""Ensure every deal whose scope has REDUCED carries a scope_shrink CEO watch (merged into
ai.ceo_intervention.reasons[]) — the native watch the re-sweep dropped. Also re-asserts
Techtronic's scope_change (a known S2P->S2C fact the fresh sweep lost). Merges, never wipes.
Dry-run by default; --apply to write."""
import json, re, sys
import requests, urllib3
from daily_summary.common import load_secret, VERIFY, id15
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_RED = ("reduced", "reduced_scope", "shrunk", "shrinking", "narrowed", "narrowing", "down")
TECHTRONIC_SCOPE = {"direction": "reduced", "from": "Full Source-to-Pay (S2P)",
                    "to": "Source-to-Contract (S2C) only, P2P deferred",
                    "detail": "Buyer moved from full S2P to S2C-only with P2P pushed to the new year — a "
                              "cost-defensive, phased-implementation pull."}


def _scope_reason(sc, amount):
    detail = str(sc.get("detail") or sc.get("to") or "narrower scope than before")
    return {"type": "scope_shrink", "act": False,
            "severity": "high" if (amount or 0) >= 250000 else "medium",
            "summary": "Scope shrinking vs prior — " + detail[:200] + ". Buyer likely getting defensive on "
                       "cost or implementation (phased over big-bang) — watch.",
            "detail": sc.get("detail"), "from": sc.get("from"), "to": sc.get("to"), "as_of": "2026-07-07"}


def main():
    apply = "--apply" in sys.argv
    sec = load_secret(); base = sec["SUPABASE_URL"].rstrip("/")
    key = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
    ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", sec["SUPABASE_URL"]).group(1)
    mgmt = f"https://api.supabase.com/v1/projects/{ref}/database/query"; token = sec["SUPABASE_ACCESS_TOKEN"]
    h = {"apikey": key, "Authorization": f"Bearer {key}"}
    rows = requests.get(f"{base}/rest/v1/deal_records", params={"select": "opp_id,account_name,record", "active": "eq.true"},
                        headers=h, verify=VERIFY, timeout=150).json()
    ci_out, sc_out = {}, {}
    for r in rows:
        oid = id15(r["opp_id"]); rec = r.get("record") or {}; ai = rec.get("ai") or {}
        hard = rec.get("hard") or {}; amount = hard.get("amount")
        sc = ai.get("scope_change") if isinstance(ai.get("scope_change"), dict) else {}
        # Techtronic: re-assert the known scope_change fact the fresh sweep lost
        if "techtronic" in str(r.get("account_name") or "").lower() and str(sc.get("direction") or "").lower() not in _RED:
            sc = TECHTRONIC_SCOPE; sc_out[oid] = sc
        if str(sc.get("direction") or "").strip().lower() not in _RED:
            continue
        ci = ai.get("ceo_intervention") if isinstance(ai.get("ceo_intervention"), dict) else {}
        reasons = [x for x in (ci.get("reasons") or []) if isinstance(x, dict)]
        if any(x.get("type") == "scope_shrink" for x in reasons):
            continue   # already has it
        reasons = [_scope_reason(sc, amount)] + reasons
        ci_out[oid] = {"needed": True,
                       "severity": "high" if any(x.get("severity") == "high" for x in reasons) else "medium",
                       "needs_action": any(x.get("type") == "support" for x in reasons),
                       "reasons": reasons,
                       "win": (ai.get("deal_scores") or {}).get("headline", {}).get("win_position"),
                       "mom": (ai.get("deal_scores") or {}).get("headline", {}).get("deal_momentum"),
                       "source": ci.get("source") or "restored_v1", "generated_at": "2026-07-07"}
    print(f"scope_shrink watches to add/ensure: {len(ci_out)} | scope_change re-asserted: {len(sc_out)}")
    if not apply:
        print("[DRY RUN] pass --apply to write."); return
    for path, data in (("{ai,scope_change}", sc_out), ("{ai,ceo_intervention}", ci_out)):
        if not data:
            continue
        blob = json.dumps(data)
        sql = ("update deal_records d set record = jsonb_set(record,'" + path + "', m.value, true), updated_at = now() "
               "from (select key as opp_id, value from jsonb_each($J$" + blob + "$J$::jsonb)) m where d.opp_id = m.opp_id returning d.opp_id")
        resp = requests.post(mgmt, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                             json={"query": sql}, verify=VERIFY, timeout=120)
        print(f"  {path}: {len(resp.json()) if resp.status_code<300 else resp.text[:120]}")


if __name__ == "__main__":
    main()
