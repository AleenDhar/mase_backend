"""deal_engine_ceo.py — native CEO-intervention finalizer for the sweep.

CEO help (ai.ceo_intervention) is now computed on EVERY sweep instead of a separate
local pass. The split of responsibility keeps it safe:

  ELIGIBILITY (a DETERMINISTIC FLOOR, not the qualifier) — a deal is only ever
  CONSIDERED when its win score clears win_position >= 40. This applies to ALL deals,
  not just forecasted ones (forecast category is NOT gated). Momentum is NOT gated
  either (a winnable-but-stalling deal is exactly when the CEO might be needed). But
  clearing the floor does NOT tag the CEO.

  THE REAL FILTER is an AI ANALYSIS — for each eligible deal the model decides
  whether the CEO (the single most senior Zycus leader) is GENUINELY, SPECIFICALLY
  required, vs. the deal needing NO intervention or only a senior/C-level exec
  (VP / SVP / CRO / CMO) who is NOT the CEO. The DEFAULT is needed=false; the CEO is
  a scarce last-resort lever. This finalizer RESPECTS that decision — it never forces
  needed=true just because the floor passed. So only the few deals where the CEO is
  truly the one required get tagged.

  WHAT (the content, when needed) rides the sweep's EXISTING LLM output — the four
  CEO levers + a CEO-personal action + a Salesforce-grounded buyer_target. No extra
  API call.

  Then this finalizer OVERRIDES needed from the gate, clamps the areas to the four
  CEO levers, stamps the real win/mom + source, and SANITIZES the free text with the
  same title / name guardrails the rest of the record uses — so a wrong exec title
  ("CFO <name>") can never survive here, exactly as it can't in moves / MEDDPICC.

Pure over plain dicts, never raises. Mutates parsed["ai"]["ceo_intervention"].
"""
from __future__ import annotations

from datetime import date
from typing import Any, Optional

import deal_engine_validation as _val

LEVERS = ("pricing", "product", "presales_resources", "exec_connect")
WIN_BAR = 40.0   # eligibility floor — win_position >= 40 (momentum is not gated)


def _num(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _is_forecasted(forecast_category: Any) -> bool:
    try:
        import deal_engine_qi as _qi
        if hasattr(_qi, "_is_forecasted"):
            return bool(_qi._is_forecasted(forecast_category))
    except Exception:  # noqa: BLE001
        pass
    return (forecast_category or "").strip().lower() in {"commit", "best case", "upside key deal", "upside"}


def _economic_buyer_from_record(ai: dict) -> dict:
    """The economic buyer the sweep already resolved (SFDC-grounded MEDDPICC), as a
    {name,title} — used to fill/repair buyer_target. Never a transcript name."""
    md = ai.get("meddpicc") if isinstance(ai.get("meddpicc"), dict) else {}
    eb = md.get("economic_buyer") if isinstance(md.get("economic_buyer"), dict) else {}
    return {"name": eb.get("name"), "title": eb.get("title") or eb.get("role")}


def _verify_buyer_target(bt: dict, contact_titles: dict, ai: dict) -> dict:
    """buyer_target names WHO the CEO connects to. Keep a name only if Salesforce
    backs it (an OpportunityContactRole contact); otherwise fall back to the record's
    MEDDPICC economic buyer, else null + role. Never asserts an unverifiable name."""
    bt = dict(bt) if isinstance(bt, dict) else {}
    name = (bt.get("name") or "").strip()
    nn = _val._norm_name(name)
    # 1) name that IS a Salesforce contact -> keep, snap title to the SFDC title.
    if nn and _val._sf_title_for(nn, contact_titles):
        bt["title"] = _val._sf_title_for(nn, contact_titles) or bt.get("title")
        return bt
    # 2) else the record's (SFDC-grounded) economic buyer.
    eb = _economic_buyer_from_record(ai)
    ebn = _val._norm_name(eb.get("name"))
    if ebn:
        return {"name": eb["name"], "title": eb.get("title") or bt.get("title"),
                "engaged": bool(bt.get("engaged"))}
    # 3) else no verifiable person -> role only.
    return {"name": None, "title": bt.get("title") or "the economic buyer / budget owner",
            "engaged": False}


def _sanitize_text(s: Any, contact_titles: dict, allow: set) -> Any:
    if not isinstance(s, str) or not s:
        return s
    out, _ = _val._neutralise_title_claims(s, contact_titles, allow)
    return out


def finalize_ceo_intervention(parsed: dict, opp: dict, buyer: Optional[dict],
                              prior_ai: Optional[dict] = None,
                              allowlist: Optional[set] = None) -> None:
    """Compute + sanitize ai.ceo_intervention in place. `opp` is the SF snapshot
    (forecast_category, amount), `buyer` the OpportunityContactRole prefetch."""
    if not isinstance(parsed, dict) or not isinstance(parsed.get("ai"), dict):
        return
    ai = parsed["ai"]
    hl = ((ai.get("deal_scores") or {}).get("headline") or {}) if isinstance(ai.get("deal_scores"), dict) else {}
    win, mom = _num(hl.get("win_position")), _num(hl.get("deal_momentum"))
    gen = date.today().isoformat()

    # --- the DETERMINISTIC FLOOR (win only; ALL deals, momentum not gated) -------
    # This is only ELIGIBILITY — it does NOT tag the CEO. Any deal (not just
    # forecasted) with win_position >= 40 is considered; the AI decides from there.
    eligible = bool(win is not None and win >= WIN_BAR)
    prior = (prior_ai or {}).get("ceo_intervention") if isinstance(prior_ai, dict) else None
    if not eligible:
        # below the win floor -> not eligible for support OR monitor (the attention
        # run only covers win>=40), so emit a clean empty attention object.
        ai["ceo_intervention"] = {"needed": False, "kind": "none",
                                  "support": {"needed": False},
                                  "monitor": {"needed": False, "triggers": []},
                                  "win": win, "mom": mom, "source": "sweep",
                                  "generated_at": gen}
        return

    # ceo_intervention now carries TWO sub-determinations: `support` (CEO must ACT —
    # computed here from the sweep's own discriminator) and `monitor` (CEO should
    # WATCH — a 14-day-surgical analysis owned by the SEPARATE ceo_attention run).
    # The sweep NEVER computes/clobbers monitor; it carries the prior monitor forward
    # so a CDC re-sweep can't wipe the attention run's output.
    prior_ci = prior if isinstance(prior, dict) else {}
    prior_monitor = prior_ci.get("monitor") if isinstance(prior_ci.get("monitor"), dict) else {"needed": False, "triggers": []}

    def _emit(support: dict) -> None:
        needed = bool(support.get("needed") or prior_monitor.get("needed"))
        kind = ("both" if support.get("needed") and prior_monitor.get("needed")
                else "support" if support.get("needed")
                else "monitor" if prior_monitor.get("needed") else "none")
        ai["ceo_intervention"] = {"needed": needed, "kind": kind, "support": support,
                                  "monitor": prior_monitor, "win": win, "mom": mom,
                                  "source": "sweep", "generated_at": gen}

    # --- THE SUPPORT FILTER: does the CEO GENUINELY need to ACT? (default NO) ----
    # Read the model's emitted support (new nested OR legacy flat shape), else a
    # prior support decision on a thin read.
    ci = ai.get("ceo_intervention") if isinstance(ai.get("ceo_intervention"), dict) else {}
    ci_support = ci.get("support") if isinstance(ci.get("support"), dict) else ci  # new nested or legacy flat
    prior_support = prior_ci.get("support") if isinstance(prior_ci.get("support"), dict) else prior_ci
    if "needed" in (ci.get("support") or ci):
        ai_needs_ceo = (ci_support.get("needed") is True)
        src = ci_support
    elif isinstance(prior_support, dict) and prior_support.get("needed") and prior_support.get("ceo_action"):
        src = dict(prior_support); ai_needs_ceo = True
    else:
        src = {}; ai_needs_ceo = False

    if not ai_needs_ceo:
        _emit({"needed": False})
        return

    contact_titles = _val.build_contact_titles(buyer)
    allow = set(allowlist or set())
    for k in ("owner_name", "manager_name"):
        n = _val._norm_name((opp or {}).get(k))
        if n:
            allow.add(n)
    allow.discard("")

    areas = [a for a in (src.get("areas") or []) if a in LEVERS] or ["exec_connect"]
    amount = _num((parsed.get("hard") or {}).get("amount")) or 0.0
    priority = src.get("priority") if src.get("priority") in ("high", "medium") else \
        ("high" if amount > 400000 else "medium")
    buyer_target = _verify_buyer_target(src.get("buyer_target") or {}, contact_titles, ai)
    reason = _sanitize_text(src.get("reason"), contact_titles, allow)
    action = _sanitize_text(src.get("ceo_action"), contact_titles, allow)
    if not action:
        who = buyer_target.get("name") or buyer_target.get("title") or "the economic buyer / budget owner"
        action = (f"The Zycus CEO personally opens a CEO-to-executive relationship with "
                  f"{who} to unblock this deal on: {', '.join(areas)}.")
    lower = [e for e in (src.get("lower_execs_engaged") or []) if isinstance(e, dict) and e.get("name")]

    _emit({"needed": True, "priority": priority, "areas": areas, "reason": reason,
           "ceo_action": action, "buyer_target": buyer_target,
           "why_not_vp": src.get("why_not_vp"),
           "ceo_not_engaged": bool(src.get("ceo_not_engaged", True)),
           "lower_execs_engaged": lower,
           "evidence": src.get("evidence") if isinstance(src.get("evidence"), list) else []})
