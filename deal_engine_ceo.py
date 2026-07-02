"""deal_engine_ceo.py — native CEO-intervention finalizer for the sweep.

CEO help (ai.ceo_intervention) is now computed on EVERY sweep instead of a separate
local pass. The split of responsibility keeps it safe:

  WHEN (the gate) is DETERMINISTIC — a deal qualifies only when it is FORECASTED
  (Commit / Best Case / Upside) AND its server-computed scores clear the bar
  (win_position > 60 AND deal_momentum > 60). The model never decides the gate.

  WHAT (the content) rides the sweep's EXISTING LLM output — the model emits its
  best CEO read (the four CEO levers + a CEO-personal action + a Salesforce-grounded
  buyer_target). No extra API call.

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
WIN_BAR = 60.0
MOM_BAR = 60.0


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
    forecasted = _is_forecasted((opp or {}).get("forecast_category"))
    gen = date.today().isoformat()

    # --- the DETERMINISTIC gate -------------------------------------------------
    gate = bool(forecasted and win is not None and mom is not None
                and win > WIN_BAR and mom > MOM_BAR)
    if not gate:
        ai["ceo_intervention"] = {"needed": False, "win": win, "mom": mom,
                                  "source": "sweep", "generated_at": gen}
        return

    # --- passed: take the model's content, else carry a prior, else minimal -----
    ci = ai.get("ceo_intervention") if isinstance(ai.get("ceo_intervention"), dict) else {}
    if not ci.get("ceo_action"):
        prior = (prior_ai or {}).get("ceo_intervention") if isinstance(prior_ai, dict) else None
        if isinstance(prior, dict) and prior.get("needed") and prior.get("ceo_action"):
            ci = dict(prior)

    contact_titles = _val.build_contact_titles(buyer)
    allow = set(allowlist or set())
    for k in ("owner_name", "manager_name"):
        n = _val._norm_name((opp or {}).get(k))
        if n:
            allow.add(n)
    allow.discard("")

    areas = [a for a in (ci.get("areas") or []) if a in LEVERS]
    if not areas:
        areas = ["exec_connect"]
    amount = _num((parsed.get("hard") or {}).get("amount")) or 0.0
    priority = ci.get("priority") if ci.get("priority") in ("high", "medium") else \
        ("high" if amount > 400000 else "medium")

    buyer_target = _verify_buyer_target(ci.get("buyer_target") or {}, contact_titles, ai)
    reason = _sanitize_text(ci.get("reason"), contact_titles, allow)
    action = _sanitize_text(ci.get("ceo_action"), contact_titles, allow)
    if not action:
        who = buyer_target.get("name") or buyer_target.get("title") or "the economic buyer / budget owner"
        action = (f"The Zycus CEO personally opens a CEO-to-executive relationship with "
                  f"{who} to unblock this deal on: {', '.join(areas)}.")

    lower = [e for e in (ci.get("lower_execs_engaged") or []) if isinstance(e, dict) and e.get("name")]

    ai["ceo_intervention"] = {
        "needed": True, "priority": priority, "areas": areas,
        "reason": reason, "ceo_action": action,
        "buyer_target": buyer_target,
        "ceo_not_engaged": bool(ci.get("ceo_not_engaged", True)),
        "lower_execs_engaged": lower,
        "evidence": ci.get("evidence") if isinstance(ci.get("evidence"), list) else [],
        "win": win, "mom": mom, "source": "sweep", "generated_at": gen,
    }
