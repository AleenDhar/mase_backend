# -*- coding: utf-8 -*-
"""Governed CEO ATTENTION engine — the single source of the SUPPORT/WATCH
determination, driven by the locked Omnivision `ceo` engine (scoring_instructions).

`ceo_intervention_for(opp_id, ai, hard, prior_ai)` returns a ready-to-store
ai.ceo_intervention dict, or None on ineligibility/failure so the caller falls back
to its deterministic path. NEVER raises. Import-safe (requests/json/os only), so it
can be called from inside the sweep on a dedicated bounded pool.

Mirrors ceo_run.py's logic: win>=40 floor, 4 SUPPORT levers (irreplaceable/why_not_vp),
3 WATCH triggers on a 14-day anchor, native scope_shrink, carry-forward within 90 days.
"""
import os, re, json, datetime
import warnings
warnings.filterwarnings("ignore")
import requests
try:
    import urllib3
    urllib3.disable_warnings()
    VERIFY = False
except Exception:  # noqa: BLE001
    VERIFY = True

WIN_BAR = 40.0
LEVERS = {"pricing", "product", "presales_resources", "exec_connect"}
MODEL = os.getenv("CEO_ENGINE_MODEL", "claude-sonnet-5")
_CACHE = {"prompt": None, "loaded": False}


def _creds():
    base = (os.getenv("SUPABASE_URL") or os.getenv("NEXT_PUBLIC_SUPABASE_URL") or "").rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SERVICE_KEY") or ""
    ak = os.getenv("ANTHROPIC_API_KEY") or ""
    if not (base and key and ak):
        try:
            from daily_summary.common import load_secret
            sec = load_secret()
            base = base or (sec.get("SUPABASE_URL") or "").rstrip("/")
            key = key or sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY") or ""
            ak = ak or sec.get("ANTHROPIC_API_KEY") or ""
        except Exception:  # noqa: BLE001
            pass
    return base, key, ak


def _load_prompt():
    if _CACHE["loaded"]:
        return _CACHE["prompt"]
    base, key, _ = _creds()
    try:
        rows = requests.get(f"{base}/rest/v1/scoring_instructions",
                            params={"select": "content", "engine": "eq.ceo", "locked": "is.true",
                                    "order": "created_at.desc", "limit": "1"},
                            headers={"apikey": key, "Authorization": f"Bearer {key}"},
                            verify=VERIFY, timeout=20).json()
        _CACHE["prompt"] = rows[0]["content"] if isinstance(rows, list) and rows else None
    except Exception:  # noqa: BLE001
        _CACHE["prompt"] = None
    _CACHE["loaded"] = True
    return _CACHE["prompt"]


def _num(v):
    try:
        return float(v)
    except Exception:
        return None


def _within(as_of, days, today):
    if not as_of:
        return True
    try:
        d = datetime.date.fromisoformat(str(as_of)[:10])
        return (today - d).days <= days
    except Exception:
        return True


def _extract_json(txt):
    if not txt:
        return {}
    i = txt.find("{")
    if i < 0:
        return {}
    depth = 0; instr = False; esc = False
    for j in range(i, len(txt)):
        c = txt[j]
        if esc:
            esc = False; continue
        if c == "\\":
            esc = True; continue
        if c == '"':
            instr = not instr; continue
        if instr:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(txt[i:j + 1])
                except Exception:
                    return {}
    return {}


def _pack(opp_id, ai, hard, win, mom, today):
    medd = ai.get("meddpicc") if isinstance(ai.get("meddpicc"), dict) else {}
    vuln = ai.get("vulnerabilities")
    p = {
        "opp_id": opp_id, "today": today.isoformat(),
        "account": hard.get("account_name"), "opp_name": hard.get("opp_name"),
        "owner_rsd": hard.get("owner_name"),
        "vp": hard.get("manager_name"),   # escalation target — the CEO asks the VP, not the rep
        "amount": hard.get("amount"),
        "is_large": (_num(hard.get("amount")) or 0) >= 250000,
        "forecast_category": hard.get("forecast_category"), "stage": hard.get("stage"),
        "close_date": hard.get("close_date"), "days_to_close": hard.get("days_to_close"),
        "last_activity_date": hard.get("last_activity_date"),
        "win_position": win, "deal_momentum": mom,
        "economic_buyer": medd.get("economic_buyer") or {}, "champion": medd.get("champion") or {},
        "champion_strength": ai.get("champion_strength"),
        "competitive_position": ai.get("competitive_position"),
        "recent_deal_movement": ai.get("deal_movement"),
        "our_recommended_moves": ai.get("recommended_moves"),
        "explicit_requirements": ai.get("explicit_requirements"),
        "our_open_deliverables": ai.get("implicit_requirements"),
        "scope_change": ai.get("scope_change"), "day_summary": ai.get("day_summary"),
        "vulnerabilities": (vuln.get("items") or [])[:4] if isinstance(vuln, dict) else vuln,
        "prior_ceo_intervention": ai.get("ceo_intervention"),
    }
    return json.dumps(p, default=str)[:24000]


def _call_llm(blob, ak):
    body = {"model": MODEL, "max_tokens": 4000, "system": _CACHE["prompt"],
            "messages": [{"role": "user", "content": "Decide CEO ATTENTION for this deal per your system instruction. Deal pack (JSON):\n\n" + blob}]}
    r = requests.post("https://api.anthropic.com/v1/messages",
                      headers={"x-api-key": ak, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                      json=body, verify=VERIFY, timeout=(10, 150))
    if r.status_code >= 300:
        return {}
    txt = ""
    for blk in r.json().get("content", []):
        if blk.get("type") == "text":
            txt += blk.get("text", "")
    return _extract_json(txt)


def _to_vp(ask, rep, vp):
    """GUARANTEE the ceo_ask names the actual VP: (1) leave if the VP is already named
    up front; (2) substitute a generic 'the deal owner's VP/manager' with the real name;
    (3) swap a leading 'Ask <rep>' for the VP's name."""
    if not ask or not vp:
        return ask
    vpf = vp.split()[0] if vp.split() else vp
    if vpf.lower() in ask[:55].lower():
        return ask
    generic = re.compile(r"the deal owner'?s (?:VP|manager)", re.I)
    if generic.search(ask):
        return generic.sub(vp, ask, count=1)
    if rep:
        for nm in ([rep, rep.split()[0]] if rep.split() else [rep]):
            if not nm:
                continue
            m = re.match(r"^(\s*ask\s+)" + re.escape(nm) + r"\b", ask, re.I)
            if m:
                return ask[:m.end(1)] + vp + ask[m.end():]
    return ask


def _build(llm_out, ai, hard, win, mom, prior_ai, today):
    prior = (prior_ai or {}).get("ceo_intervention") if isinstance(prior_ai, dict) else None
    prior = prior if isinstance(prior, dict) else {}
    eligible = win is not None and win >= WIN_BAR
    tdy = today.isoformat()
    reasons = []
    sup = (llm_out or {}).get("support") or {}
    if eligible and sup.get("needed") is True:
        areas = [a for a in (sup.get("areas") or []) if a in LEVERS] or ["exec_connect"]
        pr = sup.get("priority") if sup.get("priority") in ("high", "medium") else \
            ("high" if (_num(hard.get("amount")) or 0) > 400000 else "medium")
        ev = sup.get("evidence")
        reasons.append({"type": "support", "act": True, "severity": pr, "areas": areas,
                        "summary": sup.get("summary") or sup.get("detail"), "detail": sup.get("detail"),
                        "metric": sup.get("metric"), "owner": sup.get("owner"),
                        "vp": sup.get("vp") or hard.get("manager_name"),
                        "ceo_action": sup.get("ceo_action"),
                        "ceo_ask": _to_vp(sup.get("ceo_ask"), hard.get("owner_name"), hard.get("manager_name")),
                        "buyer_target": sup.get("buyer_target") or {}, "why_not_vp": sup.get("why_not_vp"),
                        "evidence": ev if isinstance(ev, list) else ([ev] if ev else []), "as_of": tdy})
    mon = (llm_out or {}).get("monitor") or {}
    if eligible:
        for t in (mon.get("triggers") or []):
            if isinstance(t, dict) and t.get("type") in ("our_slip", "large_slowdown", "competitor_edge") and _within(t.get("as_of"), 14, today):
                reasons.append({"type": t["type"], "act": False,
                                "severity": t.get("severity") if t.get("severity") in ("high", "medium") else "medium",
                                "summary": t.get("summary"), "detail": t.get("detail"), "metric": t.get("metric"),
                                "owner": t.get("owner"), "vp": t.get("vp") or hard.get("manager_name"),
                                "ceo_ask": _to_vp(t.get("ceo_ask"), hard.get("owner_name"), hard.get("manager_name")),
                                "evidence": t.get("evidence"), "as_of": t.get("as_of")})
        sc = ai.get("scope_change") if isinstance(ai.get("scope_change"), dict) else {}
        if str(sc.get("direction") or "").strip().lower() in ("reduced", "reduced_scope", "shrunk", "shrinking", "narrowed", "narrowing", "down"):
            amt = _num(hard.get("amount")) or 0.0
            reasons.append({"type": "scope_shrink", "act": False, "severity": "high" if amt >= 250000 else "medium",
                            "summary": "Scope shrinking vs prior — " + str(sc.get("detail") or sc.get("to") or "narrower scope than before")[:160],
                            "detail": sc.get("detail"), "as_of": tdy})
        have = set(r["type"] for r in reasons)
        for r in (prior.get("reasons") or []):
            if isinstance(r, dict) and r.get("type") != "support" and r.get("type") not in have and _within(r.get("as_of"), 90, today):
                reasons.append(r); have.add(r.get("type"))
    seen, dedup = set(), []
    for r in reasons:
        t = r.get("type")
        if (t == "support" or t not in seen) and _within(r.get("as_of"), 90, today):
            dedup.append(r); seen.add(t)
    needed = bool(dedup)
    sev = "high" if any(r.get("severity") == "high" for r in dedup) else "medium"
    summ = ""
    if dedup:
        top = sorted(dedup, key=lambda r: (r.get("type") == "support", r.get("severity") == "high"), reverse=True)[0]
        summ = (top.get("summary") or top.get("detail") or "")[:220]
    return {"needed": needed, "severity": sev if needed else None,
            "needs_action": any(r.get("type") == "support" for r in dedup),
            "summary": summ, "reasons": dedup, "win": win, "mom": mom,
            "source": "ceo_v1", "generated_at": tdy}


def ceo_intervention_for(opp_id, ai, hard, prior_ai=None):
    """Governed CEO determination for one deal. Returns ai.ceo_intervention dict, or
    None on any failure (caller falls back to its deterministic path). NEVER raises."""
    try:
        ai = ai if isinstance(ai, dict) else {}
        hard = hard if isinstance(hard, dict) else {}
        hl = (ai.get("deal_scores") or {}).get("headline") or {}
        win, mom = _num(hl.get("win_position")), _num(hl.get("deal_momentum"))
        today = datetime.date.today()
        if _load_prompt() is None:
            return None
        _, _, ak = _creds()
        if not ak:
            return None
        llm = _call_llm(_pack(opp_id, ai, hard, win, mom, today), ak) if (win is not None and win >= WIN_BAR) else None
        return _build(llm, ai, hard, win, mom, prior_ai, today)
    except Exception:  # noqa: BLE001 — never block the sweep
        return None
