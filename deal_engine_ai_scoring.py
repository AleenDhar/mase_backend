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


# OMNIVISION GOVERNANCE (2026-07-09): the deal scores are GOVERNED by the locked Scoring
# Version Studio engines — Zycus Win Position + Deal Momentum — exactly as the 24-Hour Summary
# generator is governed by the locked `sum` engine (day_summary_ai). The Studio text LEADS
# (the operating law for win/momentum); the OUTPUT ADAPTER below is appended as the engine
# contract (the strict JSON shape `_normalize` parses + the three non-Studio scores that have
# no engine of their own). Edit + lock a new win/mom version in /omnivision → the scorer adopts
# it on the next sweep, no code deploy. Fail-OPEN to the on-disk seed / `mase_deal_scoring`
# override if the Studio read fails, so a Supabase blip can never leave a deal unscored.
_OUTPUT_ADAPTER = """# OUTPUT ADAPTER (engine contract — follow the GOVERNING win/momentum instructions above for
# HOW to score; this section only fixes the OUTPUT SHAPE and the three scores that have no Studio engine).

## Emit EXACTLY one JSON object, nothing else
```json
{
  "scores": {
    "deal_momentum": 0,          // Deal Momentum engine above — 0-99
    "win_position": 0,           // Zycus Win Position engine above — 0-99
    "customer_commitment": 0,    // 0-100 (see below)
    "deal_risk": 0,              // 0-100, higher = MORE risk (see below)
    "forecast_confidence": 0     // 0-100 (see below)
  },
  "read": "one phrase from the allowed set",
  "reasons": {
    "deal_momentum":      [{"tone":"good|warn","text":"..."}],
    "win_position":       [{"tone":"good|warn","text":"..."}],
    "customer_commitment":[{"tone":"good|warn","text":"..."}],
    "deal_risk":          [{"tone":"good|warn","text":"..."}]
  }
}
```

## The three scores with no Studio engine (judge from the evidence packet)
- customer_commitment (0-100) — the buyer's own investment: action items they own, security/procurement
  review run, exec access granted, references requested, paper process moving.
- deal_risk (0-100, higher = worse) — close date pushed repeatedly, stage inflation, a competitor the
  buyer prefers, budget frozen/unclear, access blocked, buyer passive/dark, no economic buyer.
- forecast_confidence (0-100) — rolls Win + Momentum + commitment up, attenuated by how COMPLETE the
  evidence is (thin packet -> lower confidence even if the point estimate is high).

## read label (must agree with the scores)
One of: Accelerating - Moving - Slowing - Stalled - Close-date risk - Front-runner - Closing - On hold - At risk.

## reasons house style
Plain English a CRO can act on; NO model internals (no "strength +1.00 (weight 20)", "stage-expected 56").
Always cite a REAL source (a call date, a field, a Next-Step note, a dollar move); keyword-only -> soften to
"rep-noted (unverified)". Win, Momentum, the read label and the risk line must tell ONE consistent story.
3-5 bullets per score, most-decisive first. Genuinely thin evidence -> score LOW and say so.
Every count/date you cite MUST come from the packet — never invent a meeting/touch/date."""


def _studio_governing() -> str:
    """The locked Omnivision Win Position + Deal Momentum engine instructions — the GOVERNING
    scoring law. '' if unavailable (caller falls back to the seed / mase_deal_scoring override)."""
    try:
        import scoring_studio as _st
        active = _st.active_locked()
        parts = []
        for eng in ("win", "mom"):
            row = active.get(eng)
            if row and row.get("content"):
                parts.append(f"# GOVERNING ENGINE — {_st.ENGINE_NAMES.get(eng, eng)} · LOCKED v{row['version']} "
                             f"(Omnivision — THIS is the operating law for this score)\n\n{row['content']}")
        return "\n\n".join(parts)
    except Exception:
        return ""


def _prompt() -> str:
    # 1) OMNIVISION GOVERNANCE: locked Studio win+mom engines lead, adapter appended.
    gov = _studio_governing()
    if gov:
        return gov + "\n\n" + _OUTPUT_ADAPTER
    # 2) Fallback — the mase_deal_scoring admin override (Supabase), if set.
    if _aps is not None:
        try:
            ov = _aps.get_prompt(_aps.ID_DEAL_SCORING)
            if ov and ov.strip():
                return _aps.strip_leading_banner(ov) if hasattr(_aps, "strip_leading_banner") else ov
        except Exception:
            pass
    # 3) Fallback — the on-disk seed.
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
    # NO temperature= : claude-sonnet-5 REJECTS it ("temperature is deprecated for this model",
    # HTTP 400) — every AI-scoring call 400'd and silently fell back to deterministic (hybrid).
    # Match the proven sweep model path (deal_engine_sweep._build_model), which passes no temperature.
    # max_tokens 16000 (was 4000): sonnet-5 is a THINKING model — it emits a thinking block
    # BEFORE the text. At 4000 the entire budget was consumed by thinking (stop_reason=
    # max_tokens, ZERO text), so _extract_json found nothing and every deal silently fell
    # back to hybrid. Verified 2026-07-09: 16000 → ['thinking','text'], clean adapter JSON.
    return CachedChatAnthropic(
        model_name=model_id,
        api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        max_tokens=int(os.getenv("DEAL_SCORING_MAX_TOKENS", "16000")),
        timeout=int(os.getenv("LLM_REQUEST_TIMEOUT_S", "300")),
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

    # PURE STUDIO GOVERNANCE (2026-07-09, user-directed): NO deterministic floors/caps on top of
    # the Studio judgment — the locked Omnivision win/mom engines are the single source of truth
    # (their own ceilings/floors/§9 govern). The only clamp is the 0-99 / 0-100 range above. If a
    # floor is wanted (e.g. §9 over-lift), fix it IN the Studio prompt and lock a new version.
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
            print(f"[DEAL-SCORES] ai response unusable (chars={len(text)}) head: {text[:220]!r}",
                  flush=True)
            raise ValueError("ai scoring returned no usable scores")
        out = _normalize(parsed, packet)
        out["evidence_packet"] = packet
        return out
    except Exception as e:
        det = _det.compute_deal_scores(record)
        if isinstance(det, dict):
            det.setdefault("ai_scoring_error", str(e)[:200])
        return det
