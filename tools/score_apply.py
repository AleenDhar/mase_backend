"""STEP 3 of the Claude-Code scoring path: APPLY the judged scores.

Reads tools/scores_io/scores.json (written by Claude Code after reading packets.json),
runs the SAME guardrails the production scorer uses (deal_engine_ai_scoring._normalize),
enforces the dead/loss override (a lost deal reads 0 + dead, never an AI score), builds
the CRO "Scores & reasons" panel, and writes deal_scores back to Supabase. NO LLM /
Anthropic API is called here — the judgment already happened in Claude Code.

scores.json shape — one entry per opp id:
  {
    "006P...": {
      "scores": {"win_position": 82, "deal_momentum": 78, "customer_commitment": 70,
                 "deal_risk": 30, "forecast_confidence": 75},
      "read": "Accelerating",
      "reasons": {
        "win_position":  [{"text": "Vendor Selected; won the eval", "tone": "good"}],
        "deal_momentum": [{"text": "3 buyer meetings in 30d", "tone": "good"}],
        "deal_risk":     [{"text": "Close date slipped twice", "tone": "bad"}]
      }
    }
  }

  python tools/score_apply.py            # applies tools/scores_io/scores.json
  python tools/score_apply.py my.json    # applies a specific file
"""
from __future__ import annotations
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, ".env"))
except Exception:
    pass

import score_lib as SB
import deal_engine_evidence as EV
import deal_engine_ai_scoring as AI
import deal_engine_scoring as DS

IO_DIR = os.path.join(HERE, "scores_io")


def _band(score, kind):
    if kind == "win":
        return "We're ahead." if score >= 70 else "In the mix." if score >= 45 else "We're behind."
    if kind == "mom":
        return "Accelerating." if score >= 75 else "Moving." if score >= 55 else "Slowing." if score >= 35 else "Stalled."
    if kind == "com":
        return "Real skin in the game." if score >= 65 else "Some investment." if score >= 45 else "Light so far."
    return ""


def build_ai_cro(sc, packet):
    """CRO 'Scores & reasons' panel from the AI reasons — same shape the frontend renders."""
    h = sc["headline"]
    deal = packet.get("deal") or {}
    r = sc.get("ai_reasons") or {}
    amt = deal.get("amount")
    header = (f"{deal.get('account')} — {deal.get('stage')}"
              + (f" · ${round(amt / 1000)}K" if isinstance(amt, (int, float)) and amt else "")
              + (f" · closes {deal.get('close_date')}" if deal.get("close_date") else ""))
    blocks = [
        {"kind": "score", "key": "win_position", "score": h["win_position"], "title": "Zycus win position",
         "sub": "can we win it?", "read": _band(h["win_position"], "win"), "bullets": r.get("win_position") or []},
        {"kind": "score", "key": "deal_momentum", "score": h["deal_momentum"], "title": "Deal momentum",
         "sub": "is it moving?", "read": _band(h["deal_momentum"], "mom"), "bullets": r.get("deal_momentum") or []},
        {"kind": "risk", "score": h["deal_risk"], "title": "What could lose it", "bullets": r.get("deal_risk") or []},
        {"kind": "score", "key": "customer_commitment", "score": h["customer_commitment"], "title": "Customer commitment",
         "sub": "how invested are they?", "read": _band(h["customer_commitment"], "com"), "bullets": r.get("customer_commitment") or []},
    ]
    return {"generated": True, "schema": 1, "model": "ai_v1", "header": header,
            "intro": "AI read per score — grounded in the latest meetings + Salesforce evidence.",
            "blocks": blocks}


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(IO_DIR, "scores.json")
    SB.require_env()
    with open(path, encoding="utf-8") as f:
        scores = json.load(f)

    applied = 0
    for oid, entry in scores.items():
        rows = SB.sb_get(f"deal_records?opp_id=eq.{oid}&select=record")
        if not rows:
            print(f"  {oid}: NO RECORD"); continue
        rec = rows[0]["record"]
        ai = rec.setdefault("ai", {})
        old = (ai.get("deal_scores") or {}).get("headline") or {}

        # DEAD/LOSS OVERRIDE: a Closed-Lost / Lost / Qualified-Out deal (by stage OR forecast)
        # or a decision_outcome="lost" signal must read 0 + dead, never an AI score.
        det = DS.compute_deal_scores(rec)
        if isinstance(det, dict) and (det.get("headline") or {}).get("dead"):
            sc = det
        else:
            pk = EV.build_evidence_packet(rec)
            sc = AI._normalize(entry, pk)           # guardrails + deal_scores shape
            sc["cro_panel"] = build_ai_cro(sc, pk)

        ai["deal_scores"] = sc
        st = SB.sb_patch(f"deal_records?opp_id=eq.{oid}", {"record": rec})
        nh = sc["headline"]
        nm = (rec.get("hard") or {}).get("account_name")
        print(f"  [{st}] {str(nm)[:24]:24} Win {old.get('win_position')}->{nh['win_position']}  "
              f"Mom {old.get('deal_momentum')}->{nh['deal_momentum']}  read={nh['read']}")
        applied += 1
    print(f"DONE — applied {applied} deals (judged by Claude Code, $0 Anthropic API)")


if __name__ == "__main__":
    main()
