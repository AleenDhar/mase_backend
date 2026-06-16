"""deal_quality_inspector.py — post-sweep quality inspector + exploratory recovery.

After deal_engine_sweep.analyze_one produces a gate-clean record, the inspector
judges whether that record is actually decision-grade. When it is thin/empty — no
Avoma calls read, empty MEDDPICC / competition / moves, or a blank Salesforce read
— BUT the deal clearly carries recoverable signal (contact roles, recent activity,
a populated Next Step log/history, or tasks), the inspector EXHAUSTS the remaining
sources before letting the thin record stand. Flow:

    sweep agent  ->  quality inspector  ->  good? keep it
                                        ->  not good + recoverable? recover, then
                                            re-synthesize and keep the richer record

What the inspector recovers (all read via direct SOQL — the reliable path that
bypasses the agent's flaky summarised tool reads):

  1. Avoma — a calls_read==0 is most often an MCP endpoint hiccup, not a genuinely
     callless deal. The recovery directive tells the agent to RE-RUN the 3-path
     discovery (opp 15-char + account 18-char + attendee email) before accepting 0.
  2. SFDC tasks — every task's Subject/Description/ActivityDate/Type, with the
     golden nuggets (deal-update intel) segregated from the noise (auto-logged
     email opens/clicks, system rows).
  3. SFDC Next Step + Next Step History — the richest source on partner-led /
     Geography__c = APAC deals. Classic example: Reserve Bank of Australia S2P
     (006P700000Paf9aIAB) — partner ATOS runs every call, so there are NO Avoma
     calls and NO useful tasks, yet the ENTIRE deal (competition, stakeholders,
     timeline, cooling confidence) is logged in Next_Step__c. Next_Step_History__c
     timestamps order the entries so the LATEST state wins.

The inspector is deterministic about gathering the evidence and quality-scoring;
the re-synthesis itself reuses the same sweep agent + the same anti-fabrication
gate, so a recovered record is held to exactly the same no-fabrication bar.

Dependency-light. The SOQL reads reuse deal_engine_sweep._soql (imported lazily to
avoid a circular import at module load).
"""
from __future__ import annotations

import re
from typing import Any, Optional

# Substrings that mark an auto-logged / system task with no human deal intel.
# These are NOISE — they never carry a golden nugget on their own.
_TASK_NOISE_SUBJECT = (
    "email opened", "opened email", "email clicked", "link clicked", "email bounced",
    "email sent", "auto-logged", "list email", "marketing email", "email:", "out of office",
    "unsubscribe", "email delivered", "view email", "webinar registration",
)
_TASK_NOISE_SUBTYPE = {"email", "listemail", "cadence", "automatedemail"}

# Keywords that flag a partner / system-integrator-led motion when they appear in
# the Next Step log, history, or a contact-role title. Deliberately broad: a
# partner-led deal is the canonical no-Avoma/no-task-but-rich-next-step case.
_PARTNER_KEYWORDS = (
    "partner", "atos", "led by", "reseller", "system integrator", " si ", "si-led",
    "channel", "deloitte", "accenture", "kpmg", "pwc", "ey ", "infosys", "tcs",
    "capgemini", "wipro", "cognizant", "via ", "through ",
)


def _s(v: Any) -> str:
    return v if isinstance(v, str) else ("" if v is None else str(v))


def _nonempty_narrative(d: Any, min_len: int = 40) -> bool:
    """A MEDDPICC element counts as 'populated' only when its narrative is a real
    sentence, not a bare gap label ('No EB identified', 'gap', '')."""
    if not isinstance(d, dict):
        return False
    n = _s(d.get("narrative")).strip()
    if len(n) < min_len:
        return False
    low = n.lower()
    bare = ("no eb", "not identified", "not documented", "unclear", "unknown",
            "no champion", "no quantified", "no competitor", "competition unknown")
    # A short narrative that is only a bare-negative label does not count.
    return not (len(n) < 80 and any(b in low for b in bare))


# ---------------------------------------------------------------------------
# 1. Quality assessment — is the persisted record actually decision-grade?
# ---------------------------------------------------------------------------
def richness_score(parsed: dict) -> int:
    """A coarse 0..N richness score: how much substantive, synthesized content the
    record actually carries. Used to (a) judge thin vs good and (b) decide whether a
    recovery re-synthesis produced a genuinely richer record worth keeping."""
    if not isinstance(parsed, dict):
        return 0
    ai = parsed.get("ai") if isinstance(parsed.get("ai"), dict) else {}
    score = 0
    md = ai.get("meddpicc") if isinstance(ai.get("meddpicc"), dict) else {}
    score += sum(1 for el in md.values() if _nonempty_narrative(el))
    comp = ai.get("competitive_position") if isinstance(ai.get("competitive_position"), dict) else {}
    comps = comp.get("competitors") if isinstance(comp.get("competitors"), list) else []
    score += min(4, sum(1 for c in comps if isinstance(c, dict) and _s(c.get("name")).strip()))
    for key in ("explicit_requirements", "implicit_requirements", "vulnerabilities",
                "open_deliverables", "stakeholder_map"):
        block = ai.get(key) if isinstance(ai.get(key), dict) else {}
        items = block.get("items") if isinstance(block.get("items"), list) else []
        score += min(2, len([i for i in items if isinstance(i, dict)]))
    moves = ai.get("recommended_moves") if isinstance(ai.get("recommended_moves"), dict) else {}
    mitems = moves.get("items") if isinstance(moves.get("items"), list) else []
    score += min(3, len([m for m in mitems if isinstance(m, dict) and len(_s(m.get("action")).strip()) > 25]))
    return score


# Below this richness score the record is "thin" — too little substance to drive a
# confident RevOps decision. Tunable via the caller.
THIN_RICHNESS_THRESHOLD = 4


def assess(parsed: dict, *, agent_sf_blank: bool = False) -> dict:
    """Judge a finalized record. Returns
        {"good": bool, "score": int, "deficits": [str, ...]}.
    'good' means decision-grade; otherwise the deficits name what is missing so the
    recovery directive can target them."""
    score = richness_score(parsed)
    ai = parsed.get("ai") if isinstance(parsed.get("ai"), dict) else {}
    ec = parsed.get("evidence_coverage") if isinstance(parsed.get("evidence_coverage"), dict) else {}
    try:
        calls_read = int(ec.get("calls_read") or 0)
    except (TypeError, ValueError):
        calls_read = 0
    deficits: list[str] = []
    if agent_sf_blank:
        deficits.append("sf_read_blank")
    if calls_read == 0:
        deficits.append("no_avoma_calls")
    md = ai.get("meddpicc") if isinstance(ai.get("meddpicc"), dict) else {}
    if sum(1 for el in md.values() if _nonempty_narrative(el)) < 3:
        deficits.append("thin_meddpicc")
    comp = ai.get("competitive_position") if isinstance(ai.get("competitive_position"), dict) else {}
    if not [c for c in (comp.get("competitors") or []) if isinstance(c, dict) and _s(c.get("name")).strip()]:
        deficits.append("no_competition")
    moves = ai.get("recommended_moves") if isinstance(ai.get("recommended_moves"), dict) else {}
    if len([m for m in (moves.get("items") or []) if isinstance(m, dict) and len(_s(m.get("action")).strip()) > 25]) < 2:
        deficits.append("thin_moves")
    good = score >= THIN_RICHNESS_THRESHOLD and "sf_read_blank" not in deficits
    return {"good": good, "score": score, "deficits": deficits}


# ---------------------------------------------------------------------------
# 2. Recoverable-signal detection + deterministic recovery-context gathering
# ---------------------------------------------------------------------------
def _classify_tasks(rows: list[dict]) -> dict:
    """Split tasks into golden nuggets (carry human deal intel) vs noise
    (auto-logged email/system rows). A task is golden when it has a real
    Description OR a substantive non-noise Subject."""
    golden, noise = [], []
    for t in rows or []:
        if not isinstance(t, dict):
            continue
        subj = _s(t.get("Subject")).strip()
        desc = _s(t.get("Description")).strip()
        subtype = _s(t.get("TaskSubtype")).strip().lower()
        low = subj.lower()
        is_noise = (
            subtype in _TASK_NOISE_SUBTYPE
            or any(k in low for k in _TASK_NOISE_SUBJECT)
        ) and len(desc) < 40
        entry = {
            "subject": subj, "description": desc[:1200],
            "date": _s(t.get("ActivityDate")), "type": _s(t.get("Type")),
            "status": _s(t.get("Status")),
        }
        (noise if is_noise else golden).append(entry)
    return {"golden": golden, "noise_count": len(noise), "golden_count": len(golden)}


def _detect_partner_led(geography: str, next_step: str, history: str,
                        contacts: list) -> Optional[str]:
    """Heuristic partner-led detection. Returns a short label of the partner signal
    (e.g. 'partner mentioned in Next Step (\"ATOS\")') or None. A partner-led /
    APAC deal is the canonical no-Avoma case where the Next Step log holds
    everything."""
    blob = " ".join([_s(next_step), _s(history)]).lower()
    for kw in _PARTNER_KEYWORDS:
        if kw in blob:
            # Surface a little context around the keyword for the directive.
            idx = blob.find(kw)
            snippet = blob[max(0, idx - 20): idx + 40].strip()
            return f'partner/SI signal in Next Step ("{snippet}")'
    for c in contacts or []:
        title = _s((c or {}).get("title")).lower() if isinstance(c, dict) else ""
        role = _s((c or {}).get("role")).lower() if isinstance(c, dict) else ""
        if any(k in title or k in role for k in ("partner", "integrator", "reseller")):
            return f'partner/SI contact role ({title or role})'
    return None


async def gather_recovery_context(agent_manager, opp_id: str, opp: dict,
                                  buyer: dict) -> dict:
    """Read — via direct SOQL, the reliable path — the sources a thin sweep most
    often missed: Geography__c, the full Next_Step__c + Next_Step_History__c, and
    every Task (Subject/Description/date), then classify tasks and detect a
    partner-led motion. Best-effort: any failure returns an empty-but-shaped dict so
    recovery still runs on whatever was found."""
    import deal_engine_sweep as _sweep  # lazy: avoid circular import at load
    sid = _sweep._sql_str(opp_id)
    ctx: dict = {
        "geography": None, "next_step": None, "next_step_history": None,
        "tasks": {"golden": [], "golden_count": 0, "noise_count": 0},
        "partner_signal": None, "is_apac": False,
    }
    # Opportunity recovery fields. Geography__c is org-verified (confirmed on the
    # RBA deal); Next_Step__c / Next_Step_History__c carry the partner-led log.
    try:
        rows = await _sweep._soql(
            agent_manager,
            f"SELECT Geography__c, Next_Step__c, Next_Step_History__c "
            f"FROM Opportunity WHERE Id = '{sid}'")
        if rows:
            r = rows[0]
            ctx["geography"] = r.get("Geography__c")
            ctx["next_step"] = r.get("Next_Step__c")
            ctx["next_step_history"] = r.get("Next_Step_History__c")
            ctx["is_apac"] = _s(r.get("Geography__c")).strip().upper() == "APAC"
    except Exception as e:  # noqa: BLE001 — recovery is best-effort
        print(f"[QUALITY-INSPECTOR] opp-field read failed opp={opp_id}: "
              f"{type(e).__name__}: {e}", flush=True)
    # Tasks — every row, classify golden vs noise.
    try:
        trows = await _sweep._soql(
            agent_manager,
            f"SELECT Id, Subject, Description, ActivityDate, Status, Type, TaskSubtype "
            f"FROM Task WHERE WhatId = '{sid}' "
            f"ORDER BY ActivityDate DESC NULLS LAST LIMIT 100")
        ctx["tasks"] = _classify_tasks(trows)
    except Exception as e:  # noqa: BLE001
        print(f"[QUALITY-INSPECTOR] task read failed opp={opp_id}: "
              f"{type(e).__name__}: {e}", flush=True)
    ctx["partner_signal"] = _detect_partner_led(
        ctx.get("geography") or "", ctx.get("next_step") or "",
        ctx.get("next_step_history") or "",
        (buyer or {}).get("contacts") if isinstance(buyer, dict) else [])
    return ctx


def has_recoverable_signal(buyer: dict, ctx: dict) -> bool:
    """True when the deal plainly has signal a thin record failed to use — so a
    recovery pass is worth the extra agent run. We DON'T recover a genuinely empty
    deal (no roles, no activity, no next step, no tasks): that one is honestly dark."""
    roles = int((buyer or {}).get("roles_count") or 0) if isinstance(buyer, dict) else 0
    next_step = _s(ctx.get("next_step")).strip()
    history = _s(ctx.get("next_step_history")).strip()
    golden = int((ctx.get("tasks") or {}).get("golden_count") or 0)
    return bool(
        roles > 0 or len(next_step) > 40 or len(history) > 40
        or golden > 0 or ctx.get("partner_signal") or ctx.get("is_apac"))


# ---------------------------------------------------------------------------
# 3. The recovery directive appended to the agent's user message on re-synthesis
# ---------------------------------------------------------------------------
def build_recovery_directive(deficits: list, ctx: dict) -> str:
    """Compose the QUALITY INSPECTOR instruction block. It hands the agent the
    deterministically-read Next Step log + golden-nugget tasks and tells it to
    exhaust Avoma and reconstruct fully from the Next Step log on a partner-led /
    APAC deal — never to conclude 'no data'."""
    lines: list[str] = []
    lines.append(
        "=== QUALITY INSPECTOR — RECOVERY PASS (your first record was thin) ===")
    lines.append(
        "Your previous record was thin/empty (deficits: "
        + ", ".join(deficits) + "). This deal DOES carry recoverable signal. Do NOT "
        "conclude 'no data' or return a thin record again. Exhaust every source below "
        "before concluding, and emit the FULL canonical record.")
    if "no_avoma_calls" in deficits:
        lines.append(
            "AVOMA: you read 0 calls. A 0 result here is almost always an MCP endpoint "
            "hiccup, NOT a callless deal. RE-RUN the 3-path discovery now (opp 15-char "
            "id + 18-char account id + attendee-email) and union the results before "
            "accepting zero. Only after all three genuinely return nothing may you treat "
            "Avoma as empty.")
    partner = ctx.get("partner_signal")
    if partner or ctx.get("is_apac"):
        lines.append(
            "PARTNER-LED / APAC DEAL: "
            + (f"{partner}. " if partner else "")
            + ("Geography is APAC. " if ctx.get("is_apac") else "")
            + "On partner/SI-led deals the buyer calls run through the PARTNER and are "
            "NOT recorded in Avoma against this opp, and tasks are sparse — so an empty "
            "Avoma/task read is EXPECTED and is NOT a dark deal. The full deal "
            "intelligence (competition, stakeholders, timeline, status, confidence "
            "trend) lives in the Next Step log below. Reconstruct MEDDPICC, "
            "competitive_position, deal_movement, stakeholder_map, and recommended_moves "
            "ENTIRELY from the Next Step log + golden-nugget tasks. Treat the partner as "
            "the channel, and name the real buyer-side people/competitors the log "
            "mentions, each sourced to its dated Next Step entry.")
    ns = _s(ctx.get("next_step")).strip()
    if ns:
        lines.append("\n--- Next_Step__c (verbatim; each dated entry is an evidence "
                     "anchor — cite it as 'Next Step <date>') ---\n" + ns[:6000])
    hist = _s(ctx.get("next_step_history")).strip()
    if hist:
        lines.append(
            "\n--- Next_Step_History__c (rep + confidence over time; the LATEST "
            "timestamp is current state — order importance by recency, and read the "
            "confidence trend as the engagement pulse) ---\n" + hist[:3000])
    golden = (ctx.get("tasks") or {}).get("golden") or []
    if golden:
        lines.append("\n--- SFDC tasks: golden nuggets (noise filtered out) ---")
        for g in golden[:25]:
            d = g.get("date") or "n/a"
            subj = g.get("subject") or ""
            desc = g.get("description") or ""
            lines.append(f"- [{d}] {subj}" + (f" — {desc}" if desc else ""))
    lines.append(
        "\nEmit the full canonical record JSON now (same contract as your system "
        "prompt). Populate every MEDDPICC element narrative, the competitor list, and "
        "≥1 dated move per 7/14/30-day horizon, anchored to the sources above. JSON only.")
    return "\n".join(lines)
