"""AI deal scoring — judges the five scores over the deterministic evidence packet.

The judgment is AI; the FACTS are deterministic. One LLM call reasons over
`deal_engine_evidence.build_evidence_packet()` and returns scores + grounded reasons.
Guardrails clamp the output and apply sanity floors; ANY failure falls back to the
deterministic `deal_engine_scoring.compute_deal_scores()` so a deal is never left unscored.

Gated by `DEAL_ENGINE_AI_SCORING` (default off). Model: `DEAL_ENGINE_SCORING_MODEL`
(default anthropic:claude-sonnet-4-5). Prompt: Supabase override (id mase_deal_scoring)
→ on-disk seed.
"""
from __future__ import annotations
import os
import json
import datetime
from typing import Optional

import deal_engine_scoring as _det
import deal_engine_evidence as _ev

try:
    import agent_prompt_store as _aps
except Exception:  # pragma: no cover
    _aps = None

_SEED_PATH = os.path.join(os.path.dirname(__file__), "prompts", "deal_engine_scoring_system_prompt.md")
_READ_LABELS = ("Accelerating", "Moving", "Slowing", "Stalled", "Close-date risk",
                "Front-runner", "Closing", "On hold", "At risk")


def ai_scoring_enabled() -> bool:
    return os.getenv("DEAL_ENGINE_AI_SCORING", "").strip().lower() in ("1", "true", "yes", "on")


def _prompt() -> str:
    if _aps is not None:
        try:
            ov = _aps.get_prompt(_aps.ID_DEAL_SCORING)
            if ov and ov.strip():
                return _aps.strip_leading_banner(ov) if hasattr(_aps, "strip_leading_banner") else ov
        except Exception:
            pass
    try:
        with open(_SEED_PATH, encoding="utf-8") as f:
            txt = f.read()
        # drop the leading HTML-comment deprecation banner
        if txt.lstrip().startswith("<!--"):
            txt = txt.split("-->", 1)[-1]
        return txt.strip()
    except Exception:
        return "Score the deal 0-100 on win, momentum, commitment, risk, forecast from the evidence. Emit JSON."


def _model():
    name = (os.getenv("DEAL_ENGINE_SCORING_MODEL") or "anthropic:claude-sonnet-4-5").strip()
    model_id = name.split(":", 1)[1] if ":" in name else name
    from anthropic_cache import CachedChatAnthropic
    return CachedChatAnthropic(
        model_name=model_id,
        temperature=0,
        max_tokens=int(os.getenv("DEAL_SCORING_MAX_TOKENS", "4000")),
        timeout=int(os.getenv("LLM_REQUEST_TIMEOUT_S", "180")),
        max_retries=int(os.getenv("ANTHROPIC_MAX_RETRIES", "4")),
    )


def _clamp(x, lo=0.0, hi=100.0) -> float:
    try:
        return round(max(lo, min(hi, float(x))), 1)
    except Exception:
        return 0.0


def _contribs(reasons: list) -> list:
    """Map AI reason bullets -> the contribution shape the CRO panel/UI expects."""
    out = []
    for r in (reasons or []):
        if isinstance(r, dict):
            tone = r.get("tone") or "good"
            text = str(r.get("text") or "").strip()
        else:
            tone, text = "good", str(r).strip()
        if text:
            out.append({"key": "ai", "points": (1.0 if tone == "good" else -1.0),
                        "detail": text, "tone": tone})
    return out


def _normalize(parsed: dict, packet: dict) -> dict:
    """Turn the model's JSON into the deal_scores shape + apply guardrails."""
    sc = parsed.get("scores") or {}
    # Win & Momentum clamp at 0–99 (per the formula); the other three at 0–100.
    win = _clamp(sc.get("win_position"), hi=99.0)
    mom = _clamp(sc.get("deal_momentum"), hi=99.0)
    com = _clamp(sc.get("customer_commitment"))
    rsk = _clamp(sc.get("deal_risk"))
    fc = _clamp(sc.get("forecast_confidence"))
    reasons = parsed.get("reasons") or {}

    # --- Guardrails (deterministic sanity around the AI judgment) ---
    deal = packet.get("deal") or {}
    mt = packet.get("meetings") or {}
    tier = deal.get("stage_tier")
    dsl = mt.get("days_since_last")
    # A late-stage deal with a meeting in the last ~21 days cannot read "slowing"/low momentum.
    if tier == "late" and isinstance(dsl, (int, float)) and dsl <= 21:
        mom = max(mom, 55.0)
        win = max(win, 60.0)
    # A deal with zero real engagement cannot read "hot".
    if (mt.get("count_60d") or 0) == 0 and not (deal.get("next_step")):
        mom = min(mom, 45.0)

    read = str(parsed.get("read") or "").strip()[:40] or _det_read(mom)
    headline = {"win_position": win, "deal_momentum": mom, "customer_commitment": com,
                "deal_risk": rsk, "forecast_confidence": fc, "read": read}
    return {
        "schema_version": getattr(_det, "SCHEMA_VERSION", 1),
        "model": "ai_v1",
        "headline": headline,
        "win_position": {"score": win, "contributions": _contribs(reasons.get("win_position"))},
        "deal_momentum": {"score": mom, "contributions": _contribs(reasons.get("deal_momentum"))},
        "customer_commitment": {"score": com, "contributions": _contribs(reasons.get("customer_commitment"))},
        "deal_risk": {"score": rsk, "contributions": _contribs(reasons.get("deal_risk"))},
        "forecast_confidence": {"score": fc, "contributions": []},
        "ai_reasons": reasons,
        "factor_source": "ai",
    }


def _det_read(mom: float) -> str:
    return "Accelerating" if mom >= 75 else "Moving" if mom >= 55 else "Slowing" if mom >= 35 else "Stalled"


def score_deal_ai(record: dict, *, meetings: Optional[list] = None,
                  sf_activities: Optional[dict] = None) -> dict:
    """AI-judge the deal_scores for one record. Falls back to deterministic scoring on
    any failure (missing key, LLM error, bad JSON). Never raises."""
    # A loss is a FACT, not a judgment: never AI-score a dead/lost deal. Defer to the
    # deterministic dead/loss override (Win/Mom 0, deal_risk 100, dead=True) — this fires
    # on a Closed-Lost forecast/stage OR a decision_outcome=="lost" signal in the latest
    # call/Next-Step even while Salesforce still shows it as live.
    det = _det.compute_deal_scores(record)
    if isinstance(det, dict) and (det.get("headline") or {}).get("dead"):
        return det
    try:
        packet = _ev.build_evidence_packet(record, meetings=meetings, sf_activities=sf_activities)
    except Exception:
        return det
    try:
        from langchain_core.messages import SystemMessage, HumanMessage
        from opportunity_analyzer import _extract_json
        user = ("Score this opportunity. Evidence packet (facts only):\n\n"
                + json.dumps(packet, default=str, ensure_ascii=False))
        resp = _model().invoke([SystemMessage(content=_prompt()), HumanMessage(content=user)])
        text = resp.content if isinstance(resp.content, str) else str(resp.content)
        parsed = _extract_json(text)
        if not isinstance(parsed, dict) or parsed.get("_error") or not parsed.get("scores"):
            raise ValueError("ai scoring returned no usable scores")
        out = _normalize(parsed, packet)
        out["evidence_packet"] = packet
        return out
    except Exception as e:
        det = _det.compute_deal_scores(record)
        if isinstance(det, dict):
            det.setdefault("ai_scoring_error", str(e)[:200])
        return det
