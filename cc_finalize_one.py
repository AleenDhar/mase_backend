"""Per-deal finalize steps for the LOCAL Claude-Code fleet run ($0 — no Anthropic API).

  python cc_finalize_one.py prep  <opp15>   # postprocess synthesis JSON with REAL production
                                            # modules (guardrails/roster/CEO/CRO) -> writes
                                            # cc_work/<opp>.prepped.json + cc_work/<opp>.packet.json
  python cc_finalize_one.py merge <opp15>   # inject the agent-authored Studio scores
                                            # (cc_work/<opp>.scores.json) via the REAL
                                            # deal_engine_ai_scoring._normalize, re-run CEO with
                                            # the governed win, rebuild the CRO panel -> writes
                                            # cc_work/<opp>.final.json + cc_work/<opp>.row.json
NOTHING is written to deal_records — local files only (user reviews CSV first).
"""
import json, os, sys, datetime as dt
import warnings
warnings.filterwarnings("ignore")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import cc_sweep
from daily_summary.common import id15

sec = cc_sweep.load_env()
try:
    from daily_summary.common import load_datalake
    _dl = load_datalake()
    os.environ.setdefault("DATALAKE_URL", _dl["DATALAKE_URL"])
    os.environ.setdefault("DATALAKE_SERVICE_KEY", _dl["DATALAKE_SERVICE_KEY"])
except Exception as e:  # noqa: BLE001
    print("datalake env unavailable:", e)

MODE = sys.argv[1]
OID = id15(sys.argv[2])
W = "cc_work"


def path(suffix):
    return os.path.join(W, f"{OID}.{suffix}")


if MODE == "prep":
    from opportunity_analyzer import _extract_json
    import deal_engine_evidence as EV
    raw = open(path("json"), encoding="utf-8").read()
    parsed = _extract_json(raw) if not raw.strip().startswith("{") else json.loads(raw)
    assert isinstance(parsed, dict) and parsed.get("ai"), "synthesis JSON unusable"
    ctx = json.load(open(path("ctx.json"), encoding="utf-8"))
    rec, viol = cc_sweep.postprocess(parsed, ctx["opp"], ctx["buyer"], ctx["existing"])
    rec["account_name"] = ctx["opp"].get("account")
    rec["opp_name"] = ctx["opp"].get("name")
    json.dump(rec, open(path("prepped.json"), "w", encoding="utf-8"), indent=1, default=str)
    packet = EV.build_evidence_packet(rec)
    json.dump(packet, open(path("packet.json"), "w", encoding="utf-8"), indent=1, default=str)
    print(f"PREP OK {OID} violations={len(viol)} packet_chars={len(json.dumps(packet, default=str))}")

elif MODE == "merge":
    from opportunity_analyzer import _extract_json
    import deal_engine_ai_scoring as A
    import deal_engine_cro as CRO
    import deal_engine_ceo as CEO
    rec = json.load(open(path("prepped.json"), encoding="utf-8"))
    packet = json.load(open(path("packet.json"), encoding="utf-8"))
    ctx = json.load(open(path("ctx.json"), encoding="utf-8"))
    pinned = bool(((rec.get("ai") or {}).get("pinned")))
    if not pinned:
        raw = open(path("scores.json"), encoding="utf-8").read()
        parsed = _extract_json(raw) if not raw.strip().startswith("{") else json.loads(raw)
        assert isinstance(parsed, dict) and parsed.get("scores"), "scores JSON unusable"
        ds = A._normalize(parsed, packet)
        ds["evidence_packet"] = packet
        rec["ai"]["deal_scores"] = ds
        # CEO re-finalize with the GOVERNED win (eligibility floor reads the headline)
        prior_ai = (ctx.get("existing") or {}).get("ai") if isinstance(ctx.get("existing"), dict) else None
        try:
            import deal_engine_validation as V
            allow = V.build_people_allowlist(ctx["buyer"], ctx.get("existing") or {})
            CEO.finalize_ceo_intervention(rec, ctx["opp"], ctx["buyer"], prior_ai=prior_ai, allowlist=allow)
        except Exception as e:  # noqa: BLE001
            print("   [ceo] skipped:", e)
        try:
            panel = CRO.build_cro_panel(rec)
            if panel:
                rec["ai"]["deal_scores"]["cro_panel"] = panel
        except Exception as e:  # noqa: BLE001
            print("   [cro] skipped:", e)
    rec["swept_at"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    json.dump(rec, open(path("final.json"), "w", encoding="utf-8"), indent=1, default=str)

    ai = rec.get("ai") or {}
    ds = ai.get("deal_scores") or {}
    hl = ds.get("headline") or {}
    rz = ds.get("ai_reasons") or {}
    ceo_i = ai.get("ceo_intervention") or {}
    ev = rec.get("evidence_coverage") or {}
    d24 = ai.get("day_summary") or {}
    fr = ai.get("forecast_read") or {}

    def _reasons(key):
        return " || ".join(f"[{b.get('tone')}] {b.get('text')}" for b in (rz.get(key) or []))
    row = {
        "opp_id": OID, "account": rec.get("account_name"), "opp_name": rec.get("opp_name"),
        "stage": (rec.get("hard") or {}).get("stage"),
        "forecast_category": (rec.get("hard") or {}).get("forecast_category"),
        "amount": (rec.get("hard") or {}).get("amount"),
        "close_date": (rec.get("hard") or {}).get("close_date"),
        "pinned": pinned,
        "win": hl.get("win_position"), "momentum": hl.get("deal_momentum"),
        "commitment": hl.get("customer_commitment"), "risk": hl.get("deal_risk"),
        "forecast_confidence": hl.get("forecast_confidence"), "read": hl.get("read"),
        "factor_source": ds.get("factor_source"),
        "win_reasons": _reasons("win_position"), "momentum_reasons": _reasons("deal_momentum"),
        "commitment_reasons": _reasons("customer_commitment"), "risk_reasons": _reasons("deal_risk"),
        "forecast_defensible": fr.get("defensible"), "forecast_recommended": fr.get("recommended_forecast"),
        "day_summary": (d24.get("overall") or "")[:500],
        "ceo_needed": ceo_i.get("needed"), "ceo_severity": ceo_i.get("severity"),
        "ceo_summary": (ceo_i.get("summary") or "")[:400],
        "ceo_reason_types": ",".join(str(r.get("type")) for r in (ceo_i.get("reasons") or [])),
        "stakeholders_n": len(((ai.get("stakeholder_map") or {}).get("items")) or []),
        "moves_n": len(((ai.get("recommended_moves") or {}).get("items")) or []),
        "competitors": "; ".join(str(c.get("name")) for c in
                                 ((ai.get("competitive_position") or {}).get("competitors") or [])),
        "calls_discovered": ev.get("calls_discovered"), "calls_read": ev.get("calls_read"),
        "confidence": ev.get("confidence") or ai.get("analysis_confidence"),
    }
    json.dump(row, open(path("row.json"), "w", encoding="utf-8"), indent=1, default=str)
    print(f"MERGE OK {OID} win={row['win']} mom={row['momentum']} read={row['read']} "
          f"ceo={row['ceo_needed']} pinned={pinned}")
else:
    raise SystemExit(f"unknown mode {MODE}")
