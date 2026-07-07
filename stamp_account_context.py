"""Stamp ai.account_context onto every active deal that has SIBLING opportunities on the same
account (2026-07-07 user spec — expansion/phase-2 leverage):
  {sibling_closed_won, best_sibling_win, best_sibling_mom, sibling_name, stamped}
Sources: active same-account deals (their stored headlines) + recent sibling Closed-Wons
(active=false, stage Closed Won). The scorer turns this into +10 Win relationship points and
a partial momentum wrap. Idempotent; only writes deals whose context changed. --apply writes."""
import sys, re, json
from collections import defaultdict
import requests, urllib3
from daily_summary.common import load_secret, VERIFY, id15
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def main():
    apply = "--apply" in sys.argv
    sec = load_secret(); base = sec["SUPABASE_URL"].rstrip("/")
    key = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
    h = {"apikey": key, "Authorization": f"Bearer {key}"}
    ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", sec["SUPABASE_URL"]).group(1)
    mgmt = f"https://api.supabase.com/v1/projects/{ref}/database/query"; tok = sec["SUPABASE_ACCESS_TOKEN"]

    act = requests.get(f"{base}/rest/v1/deal_records",
                       params={"select": "opp_id,account_name,opp_name,record", "active": "eq.true"},
                       headers=h, verify=VERIFY, timeout=150).json()
    won = requests.get(f"{base}/rest/v1/deal_records",
                       params={"select": "opp_id,account_name,opp_name,stage", "active": "eq.false",
                               "stage": "ilike.*closed won*"},
                       headers=h, verify=VERIFY, timeout=90).json()
    # Normalize account names so legal-entity variants group as ONE account (Civeo: "Civeo
    # Corporation" vs "CIVEO PTY LTD" are the same customer). Strip common suffixes + noise words.
    def norm(s):
        s = str(s or "").lower()
        s = re.sub(r"\b(inc|incorporated|ltd|limited|llc|llp|corp|corporation|co|company|gmbh|ag|plc|pty|pte|the|of|and)\b", " ", s)
        return re.sub(r"\W+", "", s)
    by_acct = defaultdict(list)
    for r in act:
        by_acct[norm(r.get("account_name"))].append(r)
    won_accts = defaultdict(list)
    for r in won:
        won_accts[norm(r.get("account_name"))].append(r.get("opp_name") or "closed-won deal")

    out, n = {}, 0
    for acct, rows_ in by_acct.items():
        has_won = bool(won_accts.get(acct))
        if len(rows_) < 2 and not has_won:
            continue
        for r in rows_:
            oid = id15(r["opp_id"])
            best_w = best_m = 0.0; best_nm = None; sib_strong = False
            for s in rows_:
                if s is r:
                    continue
                srec = s.get("record") or {}
                hl = ((srec.get("ai") or {}).get("deal_scores") or {}).get("headline") or {}
                w = hl.get("win_position"); m = hl.get("deal_momentum")
                shard = srec.get("hard") or {}
                sstg = str(shard.get("stage") or "").lower(); sfc = str(shard.get("forecast_category") or "").lower()
                # A sibling is a real FOOTHOLD if it's advanced-stage / forecasted / strongly scored —
                # its own win may be CAPPED by the Access-to-Power gate (Cadence CLM: Shortlisted /
                # Best Case reads 52) yet it's a live engaged deal we can lever off.
                # Foothold = we're genuinely ADVANCED / FORECASTED on the sibling, not merely active.
                # Advanced stage or a Commit/Best-Case forecast is a real foothold even when the
                # sibling's own win is capped by the Access-to-Power gate; a high win (>=60) or a very
                # hot clock (mom >=70) also counts. Plain activity (mom 60 on an early deal) does not.
                if (any(t in sstg for t in ("shortlist", "vendor select", "selected", "negotiat", "contract", "won", "po "))
                        or sfc in ("commit", "best case")
                        or (isinstance(w, (int, float)) and w >= 60) or (isinstance(m, (int, float)) and m >= 70)):
                    sib_strong = True
                if isinstance(w, (int, float)) and w > best_w:
                    best_w, best_nm = float(w), (s.get("opp_name") or s.get("account_name"))
                if isinstance(m, (int, float)) and m > best_m:
                    best_m = float(m)
            ctx = {"sibling_closed_won": has_won,
                   "best_sibling_win": round(best_w, 1), "best_sibling_mom": round(best_m, 1),
                   "sibling_strong": bool(sib_strong or has_won),
                   "sibling_name": (won_accts[acct][0] if has_won and not best_nm else best_nm),
                   "stamped": "2026-07-07"}
            prev = ((r.get("record") or {}).get("ai") or {}).get("account_context") or {}
            if {k: prev.get(k) for k in ("sibling_closed_won", "best_sibling_win", "best_sibling_mom", "sibling_strong")} != \
               {k: ctx[k] for k in ("sibling_closed_won", "best_sibling_win", "best_sibling_mom", "sibling_strong")}:
                out[oid] = ctx
            n += 1
    print(f"accounts with siblings/closed-wons: deals in scope={n} | to stamp (changed)={len(out)}")
    for oid, c in list(out.items())[:8]:
        print(f"  {oid} won={c['sibling_closed_won']} best_sib_win={c['best_sibling_win']} mom={c['best_sibling_mom']} ({str(c['sibling_name'])[:30]})")
    if not apply:
        print("[DRY RUN] --apply to write."); return
    items = list(out.items())
    total = 0
    for i in range(0, len(items), 80):
        blob = json.dumps(dict(items[i:i + 80]))
        sql = ("update deal_records d set record = jsonb_set(record,'{ai,account_context}', m.value, true), "
               "updated_at = now() from (select key as opp_id, value from jsonb_each($J$" + blob +
               "$J$::jsonb)) m where d.opp_id = m.opp_id returning d.opp_id")
        resp = requests.post(mgmt, headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                             json={"query": sql}, verify=VERIFY, timeout=120)
        total += len(resp.json()) if resp.status_code < 300 else 0
    print("STAMPED:", total)


if __name__ == "__main__":
    main()
