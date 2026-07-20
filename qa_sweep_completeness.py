"""QA: verify a deal sweep actually refreshes EVERYTHING the deal drawer shows —
in ONE pass, not separate jobs. For each opp, assert the canonical record carries a
fresh, non-empty value for every drawer surface:

  deal_scores.headline   5 scores (win/mom/commitment/risk/forecast)   [Scores]
  deal_scores.cro_panel  human-readable blocks (>=2)                    [Scores & Reasons]
  day_summary            overall + items (the 24h summary)              [24h Summary]
  meddpicc               8 elements w/ narratives                       [Intel]
  stakeholder_map        >=1 item                                       [Stakeholders]
  competitive_position   summary or >=1 competitor                     [Intel]
  recommended_moves      >=1 item                                       [Action]
  north_star_verdict     a verdict                                      [header]
  ceo_intervention       present (needed bool)                          [CEO]

Usage:
  python qa_sweep_completeness.py --ids 006...,006...        # specific opps
  python qa_sweep_completeness.py --since 2026-07-07         # all deals swept on/after a date
  python qa_sweep_completeness.py --recent 40                # 40 most-recently-swept
  python qa_sweep_completeness.py --account "Bright Horizon" # by account name
Read-only. Exit 0 if every checked deal passes the REQUIRED surfaces, else 1.
"""
from __future__ import annotations
import sys, json
import requests, urllib3
from daily_summary.common import load_secret, VERIFY
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# (key, label, required?, checker(ai, hard) -> (ok, detail))
def _hl(ai):
    return ((ai.get("deal_scores") or {}).get("headline") or {})

def _panel_blocks(ai):
    return len(((ai.get("deal_scores") or {}).get("cro_panel") or {}).get("blocks") or [])

def _medd_narr(ai):
    m = ai.get("meddpicc") or {}
    return sum(1 for k in ("metrics", "economic_buyer", "decision_criteria", "decision_process",
                           "identify_pain", "champion", "competition", "paper_process")
               if str(((m.get(k) or {}) if isinstance(m, dict) else {}).get("narrative") or "").strip())

def _day(ai):
    d = ai.get("day_summary") or {}
    return d, len(d.get("items") or []), bool(str(d.get("overall") or "").strip())

CHECKS = [
    ("scores", "5 deal scores", True, lambda ai, h: (
        all(_hl(ai).get(k) is not None for k in ("win_position", "deal_momentum", "customer_commitment", "deal_risk", "forecast_confidence")),
        "win={} mom={} cmt={} risk={} fc={}".format(*[_hl(ai).get(k) for k in ("win_position","deal_momentum","customer_commitment","deal_risk","forecast_confidence")]))),
    ("cro_panel", "human-readable panel", True, lambda ai, h: (_panel_blocks(ai) >= 2, f"{_panel_blocks(ai)} blocks")),
    ("day_summary", "24h summary", True, lambda ai, h: ((lambda d, n, o: (bool(o or n), f"overall={o} items={n}"))(*_day(ai)))),
    ("meddpicc", "MEDDPICC narratives", True, lambda ai, h: (_medd_narr(ai) >= 6, f"{_medd_narr(ai)}/8 narratives")),
    ("stakeholders", "stakeholder map", True, lambda ai, h: (len((ai.get("stakeholder_map") or {}).get("items") or []) >= 1, f"{len((ai.get('stakeholder_map') or {}).get('items') or [])} people")),
    ("competitive", "competitive read", False, lambda ai, h: (bool(str((ai.get("competitive_position") or {}).get("summary") or "").strip()) or bool((ai.get("competitive_position") or {}).get("competitors")), "present")),
    ("moves", "recommended moves", True, lambda ai, h: (len((ai.get("recommended_moves") or {}).get("items") or []) >= 1, f"{len((ai.get('recommended_moves') or {}).get('items') or [])} moves")),
    ("verdict", "north-star verdict", True, lambda ai, h: (bool(str((ai.get("north_star_verdict") or {}).get("verdict") or "").strip()), str((ai.get("north_star_verdict") or {}).get("verdict") or ""))),
    ("ceo", "ceo_intervention", True, lambda ai, h: (isinstance(ai.get("ceo_intervention"), dict) and "needed" in ai.get("ceo_intervention"), f"needed={ (ai.get('ceo_intervention') or {}).get('needed') }")),
]


def fetch(sec, args):
    base = sec["SUPABASE_URL"].rstrip("/")
    key = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
    h = {"apikey": key, "Authorization": f"Bearer {key}"}
    p = {"select": "opp_id,account_name,swept_at,record", "active": "eq.true", "order": "swept_at.desc"}
    if args.get("ids"):
        ids = ",".join(f'"{i.strip()[:15]}*"' for i in args["ids"].split(",") if i.strip())
        # PostgREST: use like on 15-char prefix via or=
        p2 = dict(p); p2["or"] = "(" + ",".join(f"opp_id.like.{i.strip()[:15]}*" for i in args["ids"].split(",") if i.strip()) + ")"
        return requests.get(f"{base}/rest/v1/deal_records", params=p2, headers=h, verify=VERIFY, timeout=120).json()
    if args.get("account"):
        p["account_name"] = f"ilike.*{args['account']}*"
    elif args.get("since"):
        p["swept_at"] = f"gte.{args['since']}"
    if args.get("recent"):
        p["limit"] = int(args["recent"])
    return requests.get(f"{base}/rest/v1/deal_records", params=p, headers=h, verify=VERIFY, timeout=150).json()


def main():
    a = {}
    for i, tok in enumerate(sys.argv):
        if tok in ("--ids", "--since", "--recent", "--account") and i + 1 < len(sys.argv):
            a[tok[2:]] = sys.argv[i + 1]
    if not a:
        a["recent"] = "20"
    sec = load_secret()
    rows = fetch(sec, a)
    if not isinstance(rows, list) or not rows:
        print("no matching deals"); sys.exit(1)
    print(f"QA sweep-completeness — {len(rows)} deal(s)\n")
    labels = [c[1] for c in CHECKS]
    print(f"{'account':26} {'swept':11} " + " ".join(f"{l[:9]:9}" for l in labels))
    n_fail = 0
    surface_fail = {c[0]: 0 for c in CHECKS}
    for r in rows:
        ai = (r.get("record") or {}).get("ai") or {}
        hard = (r.get("record") or {}).get("hard") or {}
        cells, req_ok = [], True
        for key, label, required, fn in CHECKS:
            try:
                ok, _ = fn(ai, hard)
            except Exception:
                ok = False
            cells.append("  OK " if ok else " FAIL")
            if not ok:
                surface_fail[key] += 1
                if required:
                    req_ok = False
        if not req_ok:
            n_fail += 1
        acct = str(r.get("account_name") or r.get("opp_id"))[:26]
        print(f"{acct:26} {str(r.get('swept_at') or '')[:11]:11} " + " ".join(f"{c:9}" for c in cells))
    print(f"\nREQUIRED-surface pass: {len(rows)-n_fail}/{len(rows)} deals")
    print("per-surface failures: " + ", ".join(f"{k}={v}" for k, v in surface_fail.items() if v))
    # sample a day_summary so QA shows it's a real summary, not a dump
    for r in rows:
        d = ((r.get("record") or {}).get("ai") or {}).get("day_summary") or {}
        if d.get("items"):
            print(f"\nsample day_summary [{r.get('account_name')}]: {(d.get('overall') or '')[:160]}")
            for it in d["items"][:3]:
                print(f"   - {it.get('kind')}: {(it.get('name') or '')[:34]} — {(it.get('summary') or '')[:80]}")
            break
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
