# -*- coding: utf-8 -*-
"""Build the refreshed Yondr canonical record (subagent output) from the prior
record + fresh 7/9-7/10 evidence, then write cc_work/<oid>.json for postprocess."""
import json, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OID = "006P700000YjU3D"
ctx = json.load(open(f"cc_work/{OID}.ctx.json", encoding="utf-8"))
rec = dict(ctx.get("existing") or {})
ai = dict(rec.get("ai") or {})
rec["ai"] = ai
rec.setdefault("hard", {})

# north_star_verdict: prior Close-Date-Risk resolved (close pulled 1May2027 -> 30Sep2026,
# now aligned with buyer plan); live gate = competitive finalist cut next week + Zip cost gap
ai["north_star_verdict"] = {
    "verdict": "At risk",
    "critical": False,
    "math": ("Created 27 May 2026; Formal Evaluation ~44 days. Buyer plan (call 3 Jun): decision "
             "late Jun/early Jul, contract Jul-Aug, kickoff Sep 2026, go-live Feb 2027. SF close date "
             "now 30 Sep 2026 (pulled in from 1 May 2027) — now CONSISTENT with the buyer plan, so the "
             "prior Close-Date-Risk is largely resolved. The live gate is a competitive finalist cut: on "
             "10 Jul David told Claire Zycus implementation is 50% higher than Zip and Yondr will decide "
             "next week who reaches the final round."),
    "evidence": [
        "David (10 Jul 2026): 'been told our implementation is 50% more than zips.. they will be making a call next week on who is getting to the final round'",
        "Zycus counter (10 Jul 2026): 'we can move on the implementation cost, we can bring our initial quote of GBP140k down to GBP100k, fixed, paid against milestones'",
        "Rep Next Step (early Jul): 'pricing wise we are very similar to ZIP which is good news, its just do they want a payment solution or a procurement'",
        "SF close date pulled in to 30 Sep 2026, now aligned with the buyer's contract Jul-Aug / kickoff Sep plan.",
    ],
    "headline": ("Yondr is an active, healthy evaluation now gated by a competitive finalist cut next week: "
                 "Zycus is at price parity on the platform but was 50% higher on implementation vs Zip, and has "
                 "just countered by cutting implementation GBP140k->GBP100k fixed. The close date has been "
                 "corrected to 30 Sep 2026, resolving the prior CRM/plan mismatch."),
}

# competitive_position: sharpen Zip head-to-head + Zycus counter
cp = dict(ai.get("competitive_position") or {})
cp["summary"] = ("Zip is now the explicit head-to-head procurement rival: on 10 Jul David told Zycus its "
                 "implementation was 50% higher than Zip and that Yondr will pick finalists next week. Zycus "
                 "responded by cutting implementation from GBP140k to GBP100k fixed (paid against milestones) to "
                 "close the gap; platform pricing is already near-parity with Zip. The decision axis is procurement "
                 "(Zycus P2P) vs a narrower payments play. Renpayments (CFO POC) and payment-focused finalists "
                 "(Airwallix, Payhawk) remain in the mix but Zip is the deciding comparison on cost. "
                 + (cp.get("summary") or ""))[:4000]
comps = list(cp.get("competitors") or [])
comps.insert(0, {
    "date": "2026-07-10", "name": "Zip", "threat_level": "high", "status": "preferred",
    "quote": "been told our implementation is 50% more than zips.. they will be making a call next week on who is getting to the final round",
    "how_we_win": ("Match implementation cost (GBP100k fixed counter sent 10 Jul), then win on procurement depth + "
                   "purchasing UX where David already rates Zycus best-of-breed; frame Zip as payments-narrow vs Zycus full P2P."),
})
cp["competitors"] = comps
ai["competitive_position"] = cp

# recommended_moves: finalist-week priorities
ai["recommended_moves"] = {"items": [
    {"rank": 1, "owner": "Claire Hudson", "horizon": "next_7_days", "act_by": "2026-07-15",
     "trigger": "David (10 Jul): implementation 50% more than Zip; finalist call next week", "trigger_date": "2026-07-10",
     "action": "Confirm the GBP100k fixed implementation counter has reached David AND the decision-makers before the finalist cut, and get written acknowledgement it is being scored.",
     "expected_effect": "Neutralises the single reason (implementation cost) Zycus could be cut, before next week's finalist decision."},
    {"rank": 2, "owner": "Deal team", "horizon": "next_7_days", "act_by": "2026-07-15",
     "trigger": "Buyer (29 Jun) asked Zycus to complete the AI Questionnaire", "trigger_date": "2026-06-29",
     "action": "Complete and return the buyer-requested AI Questionnaire.",
     "expected_effect": "Removes an open buyer-owed deliverable that could disqualify Zycus in the review."},
    {"rank": 3, "owner": "Claire Hudson", "horizon": "next_7_days", "act_by": "2026-07-16",
     "trigger": "Decision axis: payment solution vs procurement", "trigger_date": "2026-07-02",
     "action": "Reinforce the P2P/procurement value story vs Zip's narrower scope with David and Robert Greig, tied to the 4-5x legal-entity growth pain.",
     "expected_effect": "Shifts the comparison off raw cost onto procurement depth where Zycus leads."},
    {"rank": 4, "owner": "Anthony Gray", "horizon": "next_14_days", "act_by": "2026-07-22",
     "trigger": "CFO running a parallel Renpayments POC; economic buyer not yet engaged by Zycus", "trigger_date": "2026-07-02",
     "action": "Secure direct CFO/economic-buyer access before the finalist decision locks.",
     "expected_effect": "Protects against the champion recommendation being overridden by the CFO POC."},
]}

# explicit_requirements: add AI Questionnaire (open, buyer-requested)
er = dict(ai.get("explicit_requirements") or {})
items = list(er.get("items") or [])
items.insert(0, {"date": "2026-06-29", "said_by": "Buyer (Yondr)", "addressed": False,
                 "requirement": "Complete the AI Questionnaire sent by the buyer",
                 "quote": "Thanks for the quote and additional responses on Friday, we'll be reviewing these this week... are you able to complete the attached AI Questionnaire"})
er["items"] = items
ai["explicit_requirements"] = er

# deal_movement: add 9-10 Jul
dm = dict(ai.get("deal_movement") or {})
di = list(dm.get("items") or [])
di.append({"date": "2026-07-09", "change": "Claire chasing David for the next-stage update."})
di.append({"date": "2026-07-10", "change": "David: Zycus implementation 50% higher than Zip; Yondr to pick finalists next week. Zycus countered implementation GBP140k->GBP100k fixed."})
dm["items"] = di
dm["summary"] = ("Steady, active evaluation now at the competitive finalist gate: five demo/eval calls May-Jun, "
                 "weekly touches through early Jul, and a 10 Jul cost-gap disclosure (implementation 50% over Zip) "
                 "met immediately by a Zycus GBP100k fixed counter. Close date corrected to 30 Sep 2026. "
                 + (dm.get("summary") or ""))[:1500]
ai["deal_movement"] = dm

# day_summary: 10 Jul briefing
ai["day_summary"] = {
    "as_of": "2026-07-10", "source": "ai",
    "overall": ("The pivotal update: David told Claire that Zycus's implementation quote is running 50% higher than "
                "Zip's, and that Yondr will decide next week which vendors advance to the final round. Claire moved "
                "immediately the same day, emailing David that Zycus can drop the implementation cost from its initial "
                "GBP140k to GBP100k fixed, paid against milestones, after clearing it with SLT. Platform pricing is "
                "already near-parity with Zip, so implementation cost was the one exposed gap and Zycus has now closed "
                "most of it going into the finalist decision. Separately, the buyer (29 Jun) asked Zycus to complete an "
                "AI Questionnaire as part of its review, which is still outstanding."),
    "items": [
        {"kind": "movement", "name": "Finalist cut imminent", "at": "2026-07-10",
         "summary": "David told Zycus Yondr will choose finalists next week; Zycus implementation was 50% higher than Zip, the deciding cost comparison."},
        {"kind": "email", "name": "Zycus implementation counter GBP140k->GBP100k", "at": "2026-07-10",
         "summary": "Claire emailed David that Zycus can move to GBP100k fixed implementation (from GBP140k), paid against milestones, after SLT sign-off — closing the cost gap before the finalist decision."},
    ],
}

# deal_scores_evidence: refresh top-line + cost reason
dse = dict(ai.get("deal_scores_evidence") or {})
dse["summary"] = ("Zycus is at platform-price parity with Zip and holds the strongest purchasing UX of the vendors "
                  "evaluated, but was 50% higher on implementation — the one exposed gap — and Yondr picks finalists "
                  "next week. Zycus has just countered (implementation GBP140k->GBP100k fixed), so the deal turns on "
                  "whether that counter lands with the decision-makers and whether Zycus reaches the CFO before the "
                  "parallel Renpayments POC concludes.")
ar = dict(dse.get("ai_reasons") or {})
ar["deal_risk"] = [{"tone": "crit", "text": "Yondr picks finalists next week and Zycus was 50% higher on implementation than Zip (10 Jul); if the GBP100k fixed counter does not reach the decision-makers in time, Zycus can be cut on cost alone."}] + list((ar.get("deal_risk") or []))[:2]
ar["win_position"] = [{"tone": "good", "text": "Zycus is at price parity on the platform, rated best purchasing UX of five vendors, and responded to the cost gap within the same day with a GBP100k fixed implementation counter — keeping it live for the finalist round."}] + list((ar.get("win_position") or []))[:2]
dse["ai_reasons"] = ar
ai["deal_scores_evidence"] = dse

json.dump(rec, open(f"cc_work/{OID}.json", "w", encoding="utf-8"), default=str)
print("wrote cc_work/%s.json | ai keys=%d | verdict=%s" % (OID, len(ai), ai["north_star_verdict"]["verdict"]))
