"""Surgical verdict / health / risk recompute over STORED records — no re-sweep.

The deal drawer shows three things for a deal:
  * Verdict — the prose pulse sentence (ai.north_star_verdict.headline), <=40 words.
  * Health  — one of FOUR buckets: On Track / Close Date Risk / Slowing / Off Track
              (ai.north_star_verdict.verdict, mapped to a bucket by the frontend).
  * Risk    — a 1-3 word tag for the dominant open risk (ai.north_star_verdict.risk_tag).

This module recomputes those WITHOUT re-running a sweep (no Avoma / SF fetch, no
MEDDPICC re-analysis) — it works purely off what's already in the stored record and
applies the current stage-aware definitions (see deal_engine_scoring._stage_tier /
_late_keep_risk and the STAGE-AWARE VERDICT & RISK block in the sweep prompt).

Two layers:
  1. DETERMINISTIC (free, read-time, all deals) — `derive_risk_tag` + `regrade_label`
     are pure functions over stored signals; surfaced read-time via
     deal_engine_store.attach_verdict_view, so every deal gets a stage-correct health
     bucket + risk tag the moment this deploys. No LLM, no persist.
  2. PROSE (LLM, opt-in, the ~62 forecasted) — `recompute_prose` runs a verdict-only
     model call over each stored record (bounded concurrency) to rewrite the <=40 word
     headline + label + risk tag, then PERSISTS it (stamps verdict_recomputed_at so the
     read-time layer defers to the richer LLM output).
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Optional

import deal_engine_scoring as _sc

# --- 1-3 word risk tags, keyed by the dominant deterministic risk factor ----------
_RISK_TAG = {
    "competitor_preferred": "Competitive threat",
    "open_competitive_rfp": "Open RFP",
    "close_date_pushed_repeatedly": "Close-date slip",
    "budget_frozen_or_unclear": "Budget unclear",
    "customer_passivity": "Buyer gone quiet",
    "low_buyer_intent": "Low buyer intent",
    "next_meeting_declined": "No next step",
    "access_blocked": "Access blocked",
    "stage_inflation": "Stage inflated",
}
_RISK_TAG_MINSCORE = 18.0          # below this deal_risk, no material risk -> "None"
_VALID_LABELS = ("On Track", "Close Date Risk", "Slowing", "Off Track")
FORECASTED_FC = {"Commit", "Best Case", "Upside Key Deal"}


def _evidence(record: dict):
    try:
        ev, _ = _sc.derive_evidence(record)
        return ev or {}
    except Exception:  # noqa: BLE001
        return {}


def _sig_strength(ev: dict, key: str) -> float:
    s = ev.get(key)
    return float(s.strength or 0.0) if isinstance(s, _sc.Signal) else 0.0


def _has_live_fight(record: dict, ev: Optional[dict] = None) -> bool:
    """A strong, fresh competitive signal — the LATE-stage 'still a dogfight' case."""
    ev = ev if ev is not None else _evidence(record)
    return any(_sig_strength(ev, k) >= _sc._LATE_COMPETE_MIN for k in _sc._LATE_COMPETE)


def derive_risk_tag(record: dict) -> str:
    """1-3 word tag for the dominant OPEN risk, stage-aware (uses the same gated risk
    the scorer uses, so LATE deals only surface late-relevant risks)."""
    try:
        ds = _sc.compute_deal_scores(record) or {}
        score = float((ds.get("headline") or {}).get("deal_risk") or 0.0)
        contribs = (ds.get("deal_risk") or {}).get("contributions") or []
        if score < _RISK_TAG_MINSCORE or not contribs:
            return "None"
        top = max(contribs, key=lambda c: c.get("points", 0) or 0)
        return _RISK_TAG.get(top.get("factor"), "Execution risk")
    except Exception:  # noqa: BLE001
        return "None"


def regrade_label(record: dict) -> str:
    """Re-grade the stored verdict label under the stage-aware rules. Returns one of
    _VALID_LABELS. The big correction is at LATE (contract executing): it can never be
    Off Track, and champion/EB/pain gaps are not risks — only close-date, paperwork or a
    LIVE multi-vendor fight count. EARLY/MID preserve the stored label (normalised)."""
    ai = record.get("ai") if isinstance(record.get("ai"), dict) else {}
    nsv = ai.get("north_star_verdict") if isinstance(ai.get("north_star_verdict"), dict) else {}
    stored = str(nsv.get("verdict") or "").strip().lower()
    tier = _sc._stage_tier(record)
    ev = _evidence(record)
    close_risk = _sig_strength(ev, "close_date_pushed_repeatedly") >= 0.5

    if tier == "late":
        if _has_live_fight(record, ev):
            return "Slowing"                      # live dogfight at contracting = amber
        if close_risk:
            return "Close Date Risk"
        # strip stale negativity (no champion / EB / pain are NOT risks at LATE)
        if "close" in stored:
            return "Close Date Risk"
        return "On Track"

    # EARLY / MID — keep the stored read, just normalise wording to a valid bucket.
    if "off" in stored:
        return "Off Track"
    if "close" in stored:
        return "Close Date Risk"
    if "slow" in stored or "risk" in stored:     # legacy "At Risk" -> Slowing (amber)
        return "Slowing"
    if "on" in stored and "track" in stored:
        return "On Track"
    # unknown / blank wording -> derive from the risk score
    try:
        rs = float((_sc.compute_deal_scores(record) or {}).get("headline", {}).get("deal_risk") or 0.0)
    except Exception:  # noqa: BLE001
        rs = 0.0
    return "Slowing" if rs >= 45 else "On Track"


# ----------------------------------------------------------------------------------
# PROSE layer — verdict-only LLM rewrite over the stored record (no re-sweep).
# ----------------------------------------------------------------------------------
_MODEL = os.getenv("MASE_VERDICT_MODEL", "claude-sonnet-4-5")
_STAGE_RULES = (
    "STAGE-AWARE RULES — read the verdict RELATIVE TO STAGE.\n"
    "Tiers: EARLY=Initial Interest/Qualified/Formal Evaluation; MID=Shortlisted/Vendor Selected; "
    "LATE=Contract In Progress or Negotiation/Contract Signed/PO Received.\n"
    "Risks that count: EARLY=weak champion, EB unmapped, pain/metrics unclear, competitor preferred, "
    "single-thread, stalled. MID=EB not engaged, competitor preferred/active bake-off, pricing/ROI gap, "
    "no mutual close plan/slipping timeline, InfoSec/legal/refs not cleared (early-funnel gaps drop to minor). "
    "LATE=ONLY close-date slippage, legal/redline/MSA paperwork, procurement/signature/PO, budget pulled, "
    "PLUS a LIVE multi-vendor fight (parallel redlines / comparing finalists / a competitor still actively "
    "preferred with FRESH evidence). At LATE a missing champion/EB/pain is NOT a risk; do not raise it.\n"
    "Label = exactly one of: On Track / Close Date Risk / Slowing / Off Track.\n"
    "EARLY/MID may use any label. LATE may ONLY be On Track or Close Date Risk — UNLESS there is a live "
    "multi-vendor fight, in which case Slowing is allowed; a contract-executing deal is NEVER Off Track. "
    "A deal merely stalled for months = Slowing, not Off Track (Off Track is hard-kill: we lost / "
    "disqualified / cancelled, EARLY/MID only). Forecast category does NOT set the verdict. On a LATE deal a "
    "quiet legal period is NORMAL, not slipping."
)
_PROSE_INSTR = (
    "You are re-grading ONE deal's verdict from the facts already gathered (no new research). "
    "Apply the stage-aware rules. Return STRICT JSON only, no prose around it:\n"
    '{"label": "<one of: On Track|Close Date Risk|Slowing|Off Track>", '
    '"headline": "<<=40 words, a crisp momentum read of where the deal stands and why, '
    'judged on BUYER engagement; finish the thought, no trailing ellipsis>", '
    '"risk_tag": "<1-3 words naming the single biggest open risk, or \'None\'>"}'
)


def _facts_block(record: dict) -> str:
    hard = record.get("hard") or {}
    ai = record.get("ai") or {}
    nsv = ai.get("north_star_verdict") or {}
    pulse = record.get("pulse") or {}
    ds = (ai.get("deal_scores") or {}).get("headline") or {}
    comp = ai.get("competitive_position") or ai.get("competition") or {}
    parts = [
        f"Account/Opp: {hard.get('account_name')} — {hard.get('opp_name')}",
        f"Stage: {hard.get('stage')} | Forecast category: {hard.get('forecast_category')} | Amount: {hard.get('amount')}",
        f"Close date: {hard.get('close_date')} | Days to close: {pulse.get('days_to_close')} | "
        f"Days since activity: {pulse.get('days_since_activity')}",
        f"Scores: win={ds.get('win_position')} momentum={ds.get('deal_momentum')} "
        f"commitment={ds.get('customer_commitment')} risk={ds.get('deal_risk')} fc={ds.get('forecast_confidence')}",
        f"Prior verdict: {nsv.get('verdict')} | prior headline: {nsv.get('headline')}",
        f"Reasoning on file (math): {nsv.get('math')}",
        f"Evidence on file: {json.dumps(nsv.get('evidence'))[:1200]}",
        f"Competitive position: {json.dumps(comp)[:800]}" if comp else "Competitive position: (none recorded)",
    ]
    return "\n".join(p for p in parts if p)


def _parse_json(text: str) -> Optional[dict]:
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = t[t.find("{"):] if "{" in t else t
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j < 0:
        return None
    try:
        return json.loads(t[i:j + 1])
    except Exception:  # noqa: BLE001
        return None


def _normalise_label(lbl: str, record: dict) -> str:
    k = str(lbl or "").strip().lower()
    if "off" in k:
        cand = "Off Track"
    elif "close" in k:
        cand = "Close Date Risk"
    elif "slow" in k or "risk" in k:
        cand = "Slowing"
    elif "on" in k and "track" in k:
        cand = "On Track"
    else:
        cand = None
    # Hard stage clamp regardless of what the model said.
    if _sc._stage_tier(record) == "late":
        if cand == "Off Track":
            cand = "Slowing" if _has_live_fight(record) else "Close Date Risk"
        if cand == "Slowing" and not _has_live_fight(record):
            cand = "Close Date Risk"
    return cand or regrade_label(record)


def _clip_words(s: str, n: int = 40) -> str:
    w = str(s or "").split()
    if len(w) <= n:
        return str(s or "").strip()
    return " ".join(w[:n]).rstrip(",;:—–- ") + "."


async def _one_prose(client, sem, record: dict) -> dict:
    opp = record.get("opp_id")
    prompt = f"{_STAGE_RULES}\n\n{_facts_block(record)}\n\n{_PROSE_INSTR}"
    async with sem:
        try:
            msg = await client.messages.create(
                model=_MODEL,
                max_tokens=400,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(getattr(b, "text", "") for b in msg.content)
        except Exception as e:  # noqa: BLE001
            return {"opp_id": opp, "ok": False, "error": str(e)[:200]}
    data = _parse_json(text)
    if not data:
        return {"opp_id": opp, "ok": False, "error": "unparseable"}
    return {
        "opp_id": opp,
        "ok": True,
        "label": _normalise_label(data.get("label"), record),
        "headline": _clip_words(data.get("headline"), 40),
        "risk_tag": str(data.get("risk_tag") or "None").strip() or "None",
    }


async def _run_prose(records: list, concurrency: int = 6) -> list:
    try:
        from anthropic import AsyncAnthropic
    except Exception as e:  # noqa: BLE001
        return [{"ok": False, "error": f"anthropic SDK unavailable: {e}"}]
    client = AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY") or None)
    sem = asyncio.Semaphore(max(1, int(concurrency)))
    return await asyncio.gather(*[_one_prose(client, sem, r) for r in records])


def recompute_prose(opp_ids: Optional[list] = None, *, concurrency: int = 6,
                    forecasted_only: bool = True) -> dict:
    """LLM verdict-only rewrite over STORED records (no re-sweep). Persists headline
    (<=40w) + label + risk_tag + verdict_recomputed_at. Default scope = forecasted book."""
    import deal_engine_store as store

    targets: list = []
    if opp_ids:
        for o in opp_ids:
            r = None
            try:
                r = store.get_record(store.canonical_opp_id(o))
            except Exception:  # noqa: BLE001
                r = None
            if r:
                targets.append(r)
    else:
        rows = store._select(store.T_RECORDS, select="opp_id,record", limit=100000)
        for row in rows:
            r = row.get("record")
            if not r:
                continue
            if forecasted_only and (r.get("hard") or {}).get("forecast_category") not in FORECASTED_FC:
                continue
            targets.append(r)

    if not targets:
        return {"scope": 0, "updated": 0, "failed": 0, "errors": []}

    results = asyncio.run(_run_prose(targets, concurrency=concurrency))
    by_id = {r.get("opp_id"): r for r in results if isinstance(r, dict)}
    updated, failed, errors = 0, 0, []
    for rec in targets:
        res = by_id.get(rec.get("opp_id"))
        if not res or not res.get("ok"):
            failed += 1
            if res and res.get("error"):
                errors.append({"opp_id": rec.get("opp_id"), "error": res["error"]})
            continue
        ai = rec.get("ai") if isinstance(rec.get("ai"), dict) else {}
        nsv = ai.get("north_star_verdict") if isinstance(ai.get("north_star_verdict"), dict) else {}
        nsv["verdict"] = res["label"]
        nsv["headline"] = res["headline"]
        nsv["risk_tag"] = res["risk_tag"]
        nsv["verdict_recomputed_at"] = _sc_now()
        ai["north_star_verdict"] = nsv
        rec["ai"] = ai
        try:
            store.upsert_record(rec)
            updated += 1
        except Exception as e:  # noqa: BLE001
            failed += 1
            errors.append({"opp_id": rec.get("opp_id"), "error": f"persist: {e}"})
    return {"scope": len(targets), "updated": updated, "failed": failed,
            "model": _MODEL, "concurrency": concurrency, "errors": errors[:50]}


def _sc_now() -> str:
    from datetime import datetime, timezone, timedelta
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).strftime("%Y-%m-%d %H:%M IST")
