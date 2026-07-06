"""Reframe CEO watch 'ceo_ask' so the CEO asks the VP (the owner's manager), not the
BDR/rep. e.g. "Ask Grace why IDB's close date slipped..." -> "Ask Michael McCarthy (VP
over Grace Kim) why IDB's close date slipped...". Keeps the rep named so downstream
pronouns ('...how she is forcing...') still resolve. Deterministic, no LLM. Opp-scoped
jsonb_set on ai.ceo_intervention. Dry-run by default; --apply to write."""
import re, json, sys
import requests, urllib3
from daily_summary.common import load_secret, VERIFY, id15
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _reframe(ask, owner, vp):
    """If `ask` opens by addressing the owner ('Ask <owner-first> ...'), redirect it to
    the VP while keeping the owner named. Returns (new_ask, changed?)."""
    if not ask or not vp or not owner:
        return ask, False
    of = owner.split()[0]
    m = re.match(r"^\s*(ask|have|get|tell|check with)\s+" + re.escape(of) + r"\b", ask, re.I)
    if not m:
        return ask, False
    verb = m.group(1)
    lead = "Ask" if verb.lower() == "ask" else verb[0].upper() + verb[1:].lower()
    rest = ask[m.end():]
    return f"{lead} {vp} (VP over {owner}){rest}", True


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
                        headers={"apikey": key, "Authorization": f"Bearer {key}"}, verify=VERIFY, timeout=150).json()

    out, samples = {}, []
    for r in rows:
        rec = r.get("record") or {}
        ai = rec.get("ai") or {}
        hard = rec.get("hard") or {}
        ci = ai.get("ceo_intervention") or {}
        if not ci.get("needed"):
            continue
        owner, vp = hard.get("owner_name"), hard.get("manager_name")
        changed = False
        for reason in (ci.get("reasons") or []):
            new_ask, ch = _reframe(reason.get("ceo_ask"), owner, vp)
            if ch:
                if len(samples) < 10:
                    samples.append((r.get("account_name"), reason.get("ceo_ask"), new_ask))
                reason["ceo_ask"] = new_ask
                changed = True
        if changed:
            out[id15(r["opp_id"])] = ci

    print(f"active CEO-flagged deals: {sum(1 for r in rows if ((r.get('record') or {}).get('ai') or {}).get('ceo_intervention',{}).get('needed'))} | "
          f"ceo_ask reframed to VP on {len(out)} deals")
    for acct, old, new in samples[:6]:
        print(f"\n  {acct}\n    OLD: {old[:150]}\n    NEW: {new[:170]}")
    if not apply:
        print("\n[DRY RUN] pass --apply to write.")
        return
    items = list(out.items())
    total = 0
    for i in range(0, len(items), 60):
        blob = json.dumps(dict(items[i:i + 60]))
        sql = ("update deal_records d set record = jsonb_set(record,'{ai,ceo_intervention}', m.value, true), "
               "updated_at = now() from (select key as opp_id, value from jsonb_each($J$" + blob + "$J$::jsonb)) m "
               "where d.opp_id = m.opp_id returning d.opp_id")
        resp = requests.post(mgmt, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                             json={"query": sql}, verify=VERIFY, timeout=120)
        if resp.status_code >= 300:
            print("APPLY FAILED", resp.status_code, resp.text[:300]); break
        total += len(resp.json())
    print(f"\nAPPLIED: {total} deals' ceo_ask reframed to the VP")


if __name__ == "__main__":
    main()
