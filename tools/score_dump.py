"""STEP 1 of the Claude-Code scoring path: dump deterministic EVIDENCE PACKETS.

Reads the stored deal records, builds one facts-only evidence packet per deal
(via deal_engine_evidence — counts/dates stay deterministic), and writes them to
tools/scores_io/packets.json. Claude Code then reads that file, judges each deal,
and writes tools/scores_io/scores.json (see RUN_LOCAL_AI_SCORING.md). NO LLM /
Anthropic API is called here.

  python tools/score_dump.py --forecast            # the forecast block (Commit/Best Case/Upside)
  python tools/score_dump.py --opps 006...,006...  # specific opp ids
  python tools/score_dump.py --all                 # the whole active book
"""
from __future__ import annotations
import argparse
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
import deal_engine_scoring as DS

IO_DIR = os.path.join(HERE, "scores_io")


def _dead_label(rec: dict):
    """The dead/lost label if the deal is terminal, else None — a lost deal is never
    AI-scored (it must read 0 + dead). Mirrors the apply-step override."""
    ai = rec.get("ai") or {}
    if (ai.get("decision_outcome") or {}).get("status") == "lost":
        return "Lost"
    return DS.is_dead_deal(rec)


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--forecast", action="store_true", help="forecast block only")
    g.add_argument("--all", action="store_true", help="the whole active book")
    g.add_argument("--opps", help="comma-separated opp ids")
    args = ap.parse_args()
    SB.require_env()

    if args.opps:
        ids = [s.strip() for s in args.opps.split(",") if s.strip()]
        rows = []
        for oid in ids:
            r = SB.sb_get(f"deal_records?opp_id=eq.{oid}&select=opp_id,record")
            if r:
                rows.append(r[0])
    else:
        rows = SB.fetch_active_records()
        if args.forecast:
            rows = [r for r in rows
                    if str(((r.get("record") or {}).get("hard") or {}).get("forecast_category") or "")
                    .strip().lower() in SB.FORECASTED_FC]

    deals = []
    for r in rows:
        rec = r.get("record") or {}
        oid = r.get("opp_id")
        hard = rec.get("hard") or {}
        dead = _dead_label(rec)
        deals.append({
            "opp_id": oid,
            "account": hard.get("account_name"),
            "dead": bool(dead),
            "dead_label": dead or None,
            "packet": EV.build_evidence_packet(rec),
        })

    os.makedirs(IO_DIR, exist_ok=True)
    out = os.path.join(IO_DIR, "packets.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"count": len(deals), "deals": deals}, f, ensure_ascii=False, indent=2, default=str)
    live = sum(1 for d in deals if not d["dead"])
    print(f"wrote {out}: {len(deals)} deals ({live} to score, {len(deals) - live} already dead/lost)")


if __name__ == "__main__":
    main()
