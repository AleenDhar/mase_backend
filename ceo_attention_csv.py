"""Export the CEO-attention watchlist to CSV — every deal that needs the CEO, with
exactly the fields shown on the drawer card. ONE ROW PER REASON (a deal with 3
reasons = 3 rows, grouped by opp). Reads from prod deal_records (source of truth
after apply). Output: ceo_attention_export.csv"""
from __future__ import annotations
import csv, sys, requests, urllib3
from daily_summary.common import load_secret, VERIFY

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
TYPE_LABEL = {"support": "CEO to act", "our_slip": "Our-side slip",
              "large_slowdown": "Large deal slowing", "competitor_edge": "Competitor ahead"}
OUT = "ceo_attention_export.csv"


def main():
    sec = load_secret()
    base = sec["SUPABASE_URL"].rstrip("/")
    key = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
    rows = requests.get(f"{base}/rest/v1/deal_records",
                        params={"select": "account_name,owner_name,amount,forecast_category,stage,close_date,record",
                                "active": "eq.true"},
                        headers={"apikey": key, "Authorization": f"Bearer {key}"},
                        verify=VERIFY, timeout=120).json()

    deals = []
    for r in rows:
        ci = ((r.get("record") or {}).get("ai") or {}).get("ceo_intervention") or {}
        if ci.get("source") != "attention_v1" or not ci.get("needed"):
            continue
        deals.append((r, ci))
    # rank: needs-action first, then high severity, then amount desc
    deals.sort(key=lambda x: (0 if x[1].get("needs_action") else 1,
                              0 if x[1].get("severity") == "high" else 1,
                              -(x[0].get("amount") or 0)))

    cols = ["account", "owner_rsd", "amount_usd", "forecast_category", "stage", "close_date",
            "win", "momentum", "deal_priority", "needs_ceo_action",
            "reason_type", "is_action", "reason_priority", "headline", "detail",
            "metric", "reason_owner", "ceo_action_or_ask", "evidence", "as_of",
            "ceo_levers", "buyer_target", "opp_id"]
    n_deals = n_rows = 0
    with open(OUT, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r, ci in deals:
            n_deals += 1
            reasons = ci.get("reasons") or []
            for rr in reasons:
                bt = rr.get("buyer_target") or {}
                bt_s = " / ".join([x for x in (bt.get("name"), bt.get("title")) if x]) if isinstance(bt, dict) else ""
                w.writerow([
                    r.get("account_name"), r.get("owner_name"),
                    int(r.get("amount") or 0), r.get("forecast_category"), r.get("stage"),
                    r.get("close_date"), ci.get("win"), ci.get("mom"),
                    ci.get("severity"), "yes" if ci.get("needs_action") else "no",
                    TYPE_LABEL.get(rr.get("type"), rr.get("type")),
                    "yes" if rr.get("act") else "no", rr.get("severity"),
                    rr.get("summary"), rr.get("detail"), rr.get("metric"), rr.get("owner"),
                    rr.get("ceo_action") or rr.get("ceo_ask"), rr.get("evidence"), rr.get("as_of"),
                    " · ".join(rr.get("areas") or []) if rr.get("type") == "support" else "",
                    bt_s, r.get("opp_id"),
                ])
                n_rows += 1
    print(f"wrote {OUT}: {n_deals} deals, {n_rows} reason-rows")


if __name__ == "__main__":
    main()
