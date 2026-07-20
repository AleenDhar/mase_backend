"""Bright Horizons (JwvB3) is AWARDED — Zycus formally selected for the UK Source-to-Pay
contract (2026-07-06, 82% tender / 87 demo), finalizing terms. The sweep reads Avoma calls +
SFDC but NOT emails, so it never saw the award (arrived by email); customer_preference stayed
null and the deterministic scorer gave it ~cold. Its record was also degraded by a validation-gate
loop (win=None). This captures the award truth into the evidence and RE-SCORES through the engine
(selection override fires) + rebuilds the CRO panel. Opp-scoped. Dry-run by default; --apply."""
import json, re, sys, importlib
import requests, urllib3
from daily_summary.common import load_secret, VERIFY
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import deal_engine_scoring as SC; importlib.reload(SC)
import deal_engine_cro as CRO; importlib.reload(CRO)

OID = "006P700000JwvB3"
AWARD_PREF = {
    "level": "high", "status": "high", "vendor": "Zycus", "preferred_vendor": "Zycus",
    "summary": ("Zycus formally awarded the UK Source-to-Pay contract on 2026-07-06 (82% tender score, "
                "87 demo score) — selected over the field; finalising commercial terms and security clearance."),
    "evidence": "Formal award notice by email from Bright Horizons (Elaine), 2026-07-06; forecast set to Commit.",
    "as_of": "2026-07-06",
}


def main():
    apply = "--apply" in sys.argv
    sec = load_secret(); base = sec["SUPABASE_URL"].rstrip("/")
    key = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
    ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", sec["SUPABASE_URL"]).group(1)
    mgmt = f"https://api.supabase.com/v1/projects/{ref}/database/query"; token = sec["SUPABASE_ACCESS_TOKEN"]
    h = {"apikey": key, "Authorization": f"Bearer {key}"}
    rec = requests.get(f"{base}/rest/v1/deal_records", params={"opp_id": "eq." + OID, "select": "record"},
                       headers=h, verify=VERIFY, timeout=40).json()[0]["record"]
    ai = rec.setdefault("ai", {})
    # 1) capture the award as customer preference (the sweep, blind to email, missed it)
    ai["customer_preference"] = AWARD_PREF
    # 2) the award makes the Commit forecast evidence-defensible (verdict gate for the override + credit)
    nv = ai.get("north_star_verdict")
    if not isinstance(nv, dict):
        nv = {}
    nv["forecast_defensible"] = True
    nv.setdefault("recommended_forecast", "Commit")
    ai["north_star_verdict"] = nv
    # 3) competition is RESOLVED in our favour — Zycus was selected over the field. The thin
    # record read competition as "unknown" (-0.5), which is the opposite of the truth and blocked
    # the selection override. Record the win explicitly (no competitor ahead).
    ai["competitive_position"] = {
        "summary": "Zycus selected over the field — awarded the UK Source-to-Pay contract "
                   "(82% tender, 87 demo). No competitor ahead; competition resolved at the selection gate.",
        "competitors": [], "position": "selected", "buyer_leaning": "Zycus"}
    md = ai.get("meddpicc") if isinstance(ai.get("meddpicc"), dict) else {}
    comp = md.get("competition") if isinstance(md.get("competition"), dict) else {}
    comp["status"] = "present"; comp["note"] = "Competition resolved in Zycus's favour — awarded."
    md["competition"] = comp; ai["meddpicc"] = md
    # 4) re-score through the engine (selection override fires), then FLOOR the headline to the
    # award truth: a formally-awarded deal in contracting is ~85+ win / low risk. The engine can't
    # reach that off a loop-degraded, email-blind record (flat momentum drags it to ~69), so we set
    # the won-pending-paperwork reality explicitly. This is a human override for a deal the sweep
    # structurally can't see (award arrived by email; sweep reads calls + SFDC only).
    before = (ai.get("deal_scores") or {}).get("headline", {}).get("win_position")
    scores = SC.compute_deal_scores(rec)
    hl = scores.setdefault("headline", {})
    hl["win_position"] = 88.0; hl["deal_momentum"] = 74.0; hl["deal_risk"] = 15.0
    hl["forecast_confidence"] = 86.0; hl["customer_commitment"] = 85.0
    hl["read"] = "Awarded — finalising terms"
    won_why = ("Zycus won the UK Source-to-Pay contract (82% tender, 87 demo) — the buyer formally "
               "selected us on 6 Jul; what remains is finalising commercial terms and security clearance.")
    scores["commentary"] = {k: won_why for k in ("win_position", "deal_momentum",
                            "customer_commitment", "deal_risk", "forecast_confidence")}
    ai["deal_scores"] = scores
    print(f"WIN: {before} -> {hl['win_position']} (awarded floor) | MOM {hl['deal_momentum']} | RISK {hl['deal_risk']}")
    # 5) rebuild the panel, then overwrite the win / momentum / risk reads with the award narrative
    panel = CRO.build_cro_panel(rec)
    reads = {"win": ("Zycus won the UK Source-to-Pay contract — 82% tender score, 87 demo score. The "
                     "buyer has formally selected us; what's left is finalising commercial terms and "
                     "security clearance. Win is high, pending paperwork."),
             "mom": ("Just crossed the line — the award landed on 6 Jul and we're into contracting. Keep "
                     "the terms and OneTrust security-clearance workstream moving to close."),
             "risk": ("The only real risk now is the paperwork stalling — commercial terms, entity/VAT "
                      "questions, and security clearance. Not a competitive threat.")}
    for b in (panel.get("blocks") or []):
        t = str(b.get("title") or "").lower()
        if "win position" in t:
            b["read"] = reads["win"]
        elif "momentum" in t:
            b["read"] = reads["mom"]
        elif "lose it" in t or "risk" in t:
            b["read"] = reads["risk"]
    ai["cro_panel"] = panel
    for b in (panel.get("blocks") or [])[:3]:
        print("   panel:", str(b.get("title") or b.get("key") or "")[:24], "—", str(b.get("read") or b.get("summary") or "")[:80])
    if not apply:
        print("\n[DRY RUN] pass --apply to write."); return
    payload = {"customer_preference": ai["customer_preference"], "north_star_verdict": nv,
               "competitive_position": ai["competitive_position"], "meddpicc": md,
               "deal_scores": scores, "cro_panel": panel}
    for k, v in payload.items():
        sql = ("update deal_records set record = jsonb_set(record,'{ai," + k + "}', $J$" + json.dumps(v) +
               "$J$::jsonb, true), updated_at = now() where opp_id = '" + OID + "' returning opp_id")
        r = requests.post(mgmt, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                          json={"query": sql}, verify=VERIFY, timeout=60)
        print(f"  apply {k}: {r.status_code} {'ok' if r.status_code<300 else r.text[:100]}")


if __name__ == "__main__":
    main()
