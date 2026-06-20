"""Independent Quality Inspector (QI) for the Deal Intelligence Engine sweep.

WHY THIS EXISTS
---------------
The January sweep generator self-inspects ("SELF-INSPECT until the standalone
Quality Inspector is wired" in the mase_deal_sweep prompt). Self-inspection has a
blind spot: the same agent that fabricated a name or over-escalated will not
reliably catch its own mistake. The Claire Hudson 24-deal test (2026-06-20)
proved an INDEPENDENT pass catches what self-inspection misses — it flagged
"Matthew Indge" (a person who exists nowhere in Salesforce) and every
escalation-on-a-non-forecasted-deal that the generator had let through.

WHAT IT DOES
------------
Runs AFTER the generator produces a record and BEFORE publish. Two gates:

  1. ESCALATION GATE (deterministic, no LLM, no network).
     A VP / manager / exec getting on a call may be recommended ONLY on a
     forecasted deal (ForecastCategory in {Commit, Best Case, Upside Key Deal}).
     On a non-forecasted deal:
       - any recommended move whose owner is "Executive connect" is a hard
         violation -> the owner is auto-downgraded to "Deal team" (this is the
         convention the clean records already use), and
       - exec/VP/manager-call language in the move's action/trigger text is
         FLAGGED for review (not auto-rewritten — rewriting prose is risky).

  2. FABRICATION GATE.
     - Legacy strip-names (Sarah Chen / Ryan Mitchell) are scrubbed on sight.
     - When the caller supplies `known_good_names` (the opp's real Salesforce
       contact-role names + owner + owner's manager) and `asserted_names` (the
       person-names the record asserts), every asserted name must trace to the
       known-good set or it is flagged as a suspected fabrication.

`inspect()` returns a verdict, the list of violations, and a CLEANED record. The
caller decides whether a flagged record blocks publish or publishes cleaned with
the violations recorded in an audit field. Escalation owner-fixes and strip-name
scrubs are always applied to the cleaned record.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import date, datetime
from typing import Any, Iterable, Optional

FORECASTED = {"commit", "best case", "upside key deal"}

# The structured signal: a move owner of exactly "Executive connect" means the
# deal owner's manager gets on the call. On a non-forecasted deal that is the
# violation, and it is deterministically fixable (downgrade the owner).
_ESC_OWNER = "executive connect"
_DOWNGRADE_OWNER = "Deal team"

# Softer, prose-level escalation signals — FLAGGED for review, never auto-edited.
_ESC_TEXT_PATTERNS = (
    "executive connect",
    "exec-to-exec",
    "executive-to-executive",
    "vp to call",
    "vp to reach",
    "manager to call",
    "manager to reach",
    "owner's manager to",
    "escalate to the deal owner's manager",
    "escalate to the owner's manager",
)

# Legacy fabrications that recur (~2% model leak); always scrubbed.
_STRIP_NAMES = {"Sarah Chen": "the contact", "Ryan Mitchell": "the rep"}


def _coerce_moves(record: dict) -> tuple[Optional[dict], list]:
    """Return (moves_container, items_list). recommended_moves may be a dict
    {"items": [...]}, a bare list, or a JSON string of either."""
    ai = record.get("ai") if isinstance(record, dict) else None
    rm = ai.get("recommended_moves") if isinstance(ai, dict) else None
    if isinstance(rm, str):
        try:
            rm = json.loads(rm)
        except Exception:  # noqa: BLE001
            return None, []
    if isinstance(rm, dict):
        items = rm.get("items")
        return rm, items if isinstance(items, list) else []
    if isinstance(rm, list):
        return None, rm
    return None, []


def _is_forecasted(forecast_category: Optional[str]) -> bool:
    return (forecast_category or "").strip().lower() in FORECASTED


def check_escalation(record: dict, forecast_category: Optional[str]) -> tuple[list, dict]:
    """Deterministic escalation gate. Returns (violations, cleaned_record).
    No-op (no violations) when the deal is forecasted."""
    cleaned = copy.deepcopy(record)
    if _is_forecasted(forecast_category):
        return [], cleaned

    violations: list[dict] = []
    _, items = _coerce_moves(cleaned)
    for it in items:
        if not isinstance(it, dict):
            continue
        rank = it.get("rank")
        owner = str(it.get("owner") or "")
        if owner.strip().lower() == _ESC_OWNER:
            violations.append({
                "type": "escalation_owner",
                "rank": rank,
                "detail": f'move owner "{owner}" on a non-forecasted deal',
                "fixed": True,
            })
            it["owner"] = _DOWNGRADE_OWNER  # deterministic auto-fix
        text = " ".join(str(it.get(k, "")) for k in ("action", "trigger", "expected_effect")).lower()
        hit = next((p for p in _ESC_TEXT_PATTERNS if p in text), None)
        if hit:
            violations.append({
                "type": "escalation_text",
                "rank": rank,
                "detail": f'escalation language in move text: "{hit}"',
                "fixed": False,  # flagged for review, not auto-rewritten
            })
    return violations, cleaned


def _scrub_strip_names(record: dict) -> tuple[list, dict]:
    """Deterministic legacy-fabrication scrub (Sarah Chen / Ryan Mitchell)."""
    blob = json.dumps(record)
    hits = [name for name in _STRIP_NAMES if name in blob]
    if not hits:
        return [], record
    for name, repl in _STRIP_NAMES.items():
        blob = blob.replace(name, repl)
    cleaned = json.loads(blob)
    return [{"type": "fabrication_striplist", "detail": f"scrubbed legacy name(s): {', '.join(hits)}", "fixed": True} for _ in [0]], cleaned


def _norm_name(n: str) -> str:
    return re.sub(r"\s+", " ", (n or "").strip().lower())


def check_fabrication(
    record: dict,
    known_good_names: Optional[Iterable[str]] = None,
    asserted_names: Optional[Iterable[str]] = None,
) -> tuple[list, dict]:
    """Fabrication gate. Always scrubs the legacy strip-list. When the caller
    supplies the opp's real Salesforce name set (`known_good_names`) and the
    names the record asserts (`asserted_names`), every asserted name must trace
    to a known-good name (case/space-insensitive, first-or-last-name match) or it
    is flagged as a suspected fabrication. Returns (violations, cleaned_record)."""
    violations, cleaned = _scrub_strip_names(record)

    if known_good_names is None or asserted_names is None:
        return violations, cleaned

    good_full = {_norm_name(n) for n in known_good_names if n}
    good_tokens = set()
    for n in known_good_names:
        for tok in _norm_name(n).split():
            if len(tok) > 2:
                good_tokens.add(tok)

    for raw in asserted_names:
        name = _norm_name(raw)
        if not name:
            continue
        if name in good_full:
            continue
        # accept if every alphabetic token of the asserted name is a known token
        toks = [t for t in name.split() if len(t) > 2]
        if toks and all(t in good_tokens for t in toks):
            continue
        violations.append({
            "type": "fabrication_name",
            "detail": f'asserted person "{raw}" does not trace to a Salesforce contact / owner on this deal',
            "fixed": False,  # cannot auto-resolve; flag (block on publish)
        })
    return violations, cleaned


def inspect(
    record: dict,
    forecast_category: Optional[str],
    known_good_names: Optional[Iterable[str]] = None,
    asserted_names: Optional[Iterable[str]] = None,
) -> dict:
    """Run both gates. Returns:
       { verdict: 'pass'|'block', violations: [...], cleaned_record: {...},
         summary: {escalation_violations, fabrications, auto_fixed} }
    'block' = at least one violation that could NOT be auto-fixed remains
    (unverified name, or escalation language left in prose). The cleaned_record
    always has owner-downgrades + strip-name scrubs applied."""
    esc_v, rec = check_escalation(record, forecast_category)
    fab_v, rec = check_fabrication(rec, known_good_names, asserted_names)
    violations = esc_v + fab_v
    unresolved = [v for v in violations if not v.get("fixed")]
    return {
        "verdict": "block" if unresolved else "pass",
        "violations": violations,
        "cleaned_record": rec,
        "summary": {
            "escalation_violations": sum(1 for v in violations if v["type"].startswith("escalation")),
            "fabrications": sum(1 for v in violations if v["type"].startswith("fabrication")),
            "auto_fixed": sum(1 for v in violations if v.get("fixed")),
            "unresolved": len(unresolved),
        },
    }


# ---------------------------------------------------------------------------
# January 1.0 — gate EVERYTHING that reaches the UI, not just the sweep record.
# ---------------------------------------------------------------------------

_TODO_DUE_KEYS = ("due_date", "due", "act_by", "date", "backPlannedDue", "dueDate")
_TODO_TEXT_KEYS = ("text", "action", "title", "note", "description", "label")


def _todo_due(t: dict) -> Optional[str]:
    for k in _TODO_DUE_KEYS:
        v = t.get(k)
        if v:
            return str(v)
    return None


def check_todos(
    todos: Iterable[dict],
    forecast_category: Optional[str],
    known_good_names: Optional[Iterable[str]] = None,
    asserted_names: Optional[Iterable[str]] = None,
    today: Optional[date] = None,
    max_days: int = 60,
) -> list:
    """QI gate for the to-do surface (mase_todo_runner output) + any UI-bound
    action list. Same compliance rules as the sweep record, plus the to-do
    horizon: never more than `max_days` out (daily-reviewed; default 60). Returns
    a list of violations (flagged, not auto-fixed — the caller drops/blocks)."""
    forecasted = _is_forecasted(forecast_category)
    today = today or date.today()
    violations: list[dict] = []
    items = list(todos or [])
    for i, t in enumerate(items):
        if not isinstance(t, dict):
            continue
        text = " ".join(str(t.get(k, "")) for k in _TODO_TEXT_KEYS).lower()
        owner = str(t.get("owner") or "").strip().lower()
        if not forecasted:
            hit = next((p for p in _ESC_TEXT_PATTERNS if p in text), None)
            if owner == _ESC_OWNER or hit:
                violations.append({
                    "type": "todo_escalation", "index": i,
                    "detail": f'escalation to-do on a non-forecasted deal: "{hit or owner}"',
                    "fixed": False,
                })
        due = _todo_due(t)
        if due:
            try:
                dd = datetime.fromisoformat(due[:10]).date()
                if (dd - today).days > max_days:
                    violations.append({
                        "type": "todo_due_too_far", "index": i,
                        "detail": f"due {due} is more than {max_days} days out",
                        "fixed": False,
                    })
            except Exception:  # noqa: BLE001
                pass
    blob = json.dumps(items)
    for name in _STRIP_NAMES:
        if name in blob:
            violations.append({"type": "todo_fabrication_striplist",
                               "detail": f'legacy name "{name}" in a to-do', "fixed": False})
    if known_good_names is not None and asserted_names is not None:
        fab, _ = check_fabrication({}, known_good_names, asserted_names)
        for v in fab:
            if v["type"] == "fabrication_name":
                violations.append({**v, "type": "todo_fabrication_name"})
    return violations


def _ceil_div(a: int, b: int) -> int:
    return -(-a // b) if b else 0


def staffing_plan(
    *,
    calls_read: int = 0,
    richness_score: float = 0.0,
    forecasted: bool = False,
    stakeholders: int = 0,
    amount: Optional[float] = None,
    big_amount: float = 250_000.0,
) -> dict:
    """Cost-aware dynamic staffing — scale agents to deal DEPTH, not a flat fan-out.
    A thin deal gets 1 reader and a light review; a deep deal gets the full reader
    pool + the RevOps Head + a QI panel. Reuse the generator's richness signal as
    the depth proxy. Returns the per-deal staffing the Deal Chief should appoint."""
    big = (amount or 0) >= big_amount
    deep = forecasted or big or calls_read > 5 or richness_score >= 0.70 or stakeholders >= 5
    lean = (not forecasted and not big and calls_read <= 1
            and richness_score < 0.35 and stakeholders <= 2)
    tier = "deep" if deep else ("lean" if lean else "standard")
    readers = 1 if (lean or calls_read <= 0) else min(6, max(1, _ceil_div(calls_read, 3)))
    return {
        "tier": tier,
        "readers": readers,                         # Conversation reader pool size
        "meddpicc_analysts": 2 if deep else 1,      # split MEDDPICC only when deep
        # The RevOps Head is the EXPENSIVE editor — gate it tightly to the deals
        # that close the quarter: forecasted (Commit/Best Case/Upside) OR big-$.
        # NOT standard pipeline (base sweep + the cheap deterministic gate already
        # cover most of that value). Narrowed 2026-06-20 after the Claire Hudson
        # 3-deal review showed the marginal lift on pipeline is small vs its cost.
        "revops_head_review": forecasted or big,
        "qi_panel": deep,                            # 3-vote adversarial QI only on deep/forecasted
        "rationale": (f"tier={tier}: forecasted={forecasted}, big_amount={big}, "
                      f"calls={calls_read}, richness={round(richness_score,2)}, "
                      f"stakeholders={stakeholders}"),
    }


if __name__ == "__main__":  # tiny smoke test
    demo = {
        "ai": {"recommended_moves": {"items": [
            {"rank": 1, "owner": "Executive connect", "action": "VP to call the CPO"},
            {"rank": 2, "owner": "Deal team", "action": "send the mutual close plan"},
        ]}},
        "stakeholder_map": "Champion is Matthew Indge (fabricated).",
    }
    out = inspect(demo, "Pipeline",
                  known_good_names=["Stuart Lamont", "Elaine Rymill"],
                  asserted_names=["Matthew Indge", "Stuart Lamont"])
    print(json.dumps(out["summary"], indent=2))
    print("verdict:", out["verdict"])
    print("fixed owner:", out["cleaned_record"]["ai"]["recommended_moves"]["items"][0]["owner"])
