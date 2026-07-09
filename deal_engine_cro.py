"""deal_engine_cro.py — build the CRO-readable "Scores & reasons" panel.

The deal sweep already produces rich, human-written prose on every record
(``competitive_position.summary``, ``vulnerabilities[].detail``,
``champion_strength.summary``, ``recommended_moves[].action`` …) plus the
deterministic ``deal_scores`` block. This module *selects and trims* that
existing prose into a crisp, scannable narrative a CRO/CO can act on — one read
per score, with ✅ / ⚠️ bullets and a plain-English "what could lose it" block.

Nothing here invents claims or does maths on the page: every bullet is grounded
in a field the sweep already wrote. ``build_cro_panel(record)`` returns the
structured ``cro_panel`` dict the frontend renders (or ``None`` when there is
nothing to say yet — a dead/unscored deal).
"""
from __future__ import annotations
import datetime as _dt
import re

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _clean(s):
    """Collapse CRLF / runaway whitespace from the LLM prose into one tidy line."""
    if not s:
        return ""
    return re.sub(r"\s+", " ", str(s).replace("\r", " ").replace("\n", " ")).strip()


def _first_sentence(s, max_len=170):
    """First sentence of a prose blob, hard-capped so a bullet stays scannable."""
    s = _clean(s)
    if not s:
        return ""
    # Split on sentence end followed by a space + capital / digit.
    m = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", s)
    out = m[0] if m else s
    if len(out) > max_len:
        cut = out[:max_len]
        sp = cut.rfind(" ")
        out = (cut[:sp] if sp > 40 else cut).rstrip(",;: ") + "…"
    return out


def _compact_amount(amt):
    try:
        v = float(amt)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    if v >= 1_000_000:
        return f"${v/1_000_000:.1f}".rstrip("0").rstrip(".") + "M"
    if v >= 1_000:
        return f"${round(v/1_000)}K"
    return f"${round(v)}"


def _human_date(iso):
    try:
        d = _dt.date.fromisoformat(str(iso)[:10])
        return f"{d.day} {_MONTHS[d.month - 1]}"
    except Exception:
        return None


def _r0(v):
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


# Win-position factor → human label. The contribution `evidence` carries a
# robotic "strength +1.00 (weight 20) — from <src>: <real text>" prefix; we
# strip the prefix and keep the real text (or fall back to crm_evidence.value).
_WIN_FACTOR = {
    "differentiation": ("Differentiated where it counts", "Losing on capability fit"),
    "preference":      ("Buyer is leaning our way", "Buyer is leaning elsewhere"),
    "champion":        ("Champion is driving it", "No real champion yet"),
    "exec_access":     ("We have executive access", "No executive access yet"),
    "business_case":   ("ROI / business case landed", "No quantified business case"),
    "commercial":      ("Commercial conversation is live", "Commercial talks stalled"),
    "competitive":     ("Edge over the competition", "A competitor is pushing back"),
}

_EVI_PREFIX = re.compile(r"^.*?strength [+\-][0-9.]+ \(weight \d+\)\s*(?:—|--|-)?\s*(?:from [^:]+:\s*)?", re.I)
# robotic tails the scorer appends: "(depth 5.0, 2 in 30d)", " in Next-Step/narrative", " in MEDDPICC".
_EVI_TAIL = re.compile(r"\s*(?:\((?:depth|weight)[^)]*\)|in Next-Step(?:/narrative)?|in MEDDPICC|in narrative)\s*$", re.I)


def _strip_evi(evidence, crm_val=None):
    """Turn a contribution's robotic evidence string into grounded plain text, or
    "" when there's no real evidence behind it (so the caller can drop the bullet
    rather than make an ungrounded claim)."""
    raw = _clean(evidence)
    body = _EVI_PREFIX.sub("", raw).strip()
    # Quote-only signals ("'preference for zycus' in Next-Step/narrative") read
    # better as the underlying quote.
    qm = re.match(r"^'([^']+)'\s+in\s+", body)
    if qm:
        body = qm.group(1)
    body = _EVI_TAIL.sub("", body).strip().strip("'\"")
    if (not body or len(body) < 10) and crm_val:
        body = _EVI_TAIL.sub("", _clean(crm_val)).strip().strip("'\"")
    body = _first_sentence(body, 150)
    # Reject non-grounded residue: too short, a lone keyword-match fragment
    # ("inefficien"), or a bare "in <source>" tail.
    if len(body) < 10 or re.match(r"^in\s", body, re.I) or (" " not in body and len(body) < 16):
        return ""
    return body


# Reasons must describe the DEAL, never the scoring machinery. The sweep sometimes leaks
# score-logic language ("Shortlisted caps confidence near 70", "earns roughly 66", "why this
# number", "holds in the mid-50s", "stage ceiling"). We strip those CLAUSES at render time so
# the panel only ever speaks about the deal — durable even if a future sweep leaks again.
_SCORE_LOGIC = re.compile(
    r"(shortlisted caps|formal evaluation caps|vendor[- ]selected caps|stage (cap|ceiling)|"
    r"below (vendor selected|the stage|stage)|caps? (confidence|win|it|the (win|score|read))|"
    r"cap it below|(confidence|score|win read|read) (near|at|around|to|below|of) \d|"
    r"anchors? (at|near|around)|anchored (at|near)|"
    r"earns? (it\b|roughly|about \d|around \d|more\b|a (strong|solid|good) (position|read|score))|"
    r"scores? (mid-?band|around \d|about \d|in the (mid|low|high)|it \d|near \d)|"
    r"sits? (around \d|at \d|mid-?band|in the mid)|(mid|low|high)-?\d0s|mid-?band|"
    r"hold(s|ing)? it (below|down|near|at|in the)|we hold it|holds? (it |the )?(in the (mid|low)|below|near \d)|"
    r"not higher because|why this number|how it adds up|underlying read|earned but not|"
    r"not yet banked|stage anchor|win (read|confidence) (near|of|holds)|ceiling)", re.I)
_WHY_PREFIX = re.compile(r"^\s*(why this number|how it adds up|the read)\s*[:\-—–]\s*", re.I)
# CRO-unfriendly lead-ins that talk ABOUT the score/number instead of the DEAL. Strip the
# lead-in and keep the deal content ("Score is high because selection is confirmed" ->
# "Selection is confirmed"; "Risk to the number; the EB never engaged" -> "The EB never
# engaged"). A CRO wants the deal read, not a sentence about the score.
_CRO_PREFIX = re.compile(
    r"^\s*(score is (?:high|low|moderate|strong|weak|elevated|capped|held|solid|mid[- ]?band)\b[^,;:]*?\bbecause\s+"
    r"|risk to the (?:number|score|forecast)\b[\s;:,.\-—–]*"
    r"|the (?:score|number|read) (?:is|sits|reflects|reads|holds|stays)\b[^,;:]*?[;:,.\-—–]\s*"
    r"|this (?:scores?|reads?|number)\b[^,;:]*?[;:,.\-—–]\s*)", re.I)


def _scrub_score_logic(text):
    """Strip clauses about the score's inner working; return deal-only text, or '' when the
    whole thing was score-logic (the caller then falls back to the deal narratives)."""
    t = _clean(text)
    if not t:
        return ""
    t = _WHY_PREFIX.sub("", t)
    t = _CRO_PREFIX.sub("", t)     # drop "Score is high because…" / "Risk to the number;…" lead-ins
    parts = re.split(r"\s*(?:;|:|—|–|(?<=[.!?])\s+(?=[A-Z0-9]))\s*|\s+-\s+", t)
    kept = [p.strip() for p in parts if p and p.strip() and not _SCORE_LOGIC.search(p)]
    out = "; ".join(kept)
    out = re.sub(r"^(but|and|however|though|although|yet|so|because|which)\b[ ,]*", "", out, flags=re.I).strip(" ;,.")
    if len(out) < 25:
        return ""
    return out[0].upper() + out[1:]


def _btext(raw, cap, tone=None):
    """Bullet body with NO dead ellipses: `text` is the readable clip, and when the clip
    lost content the complete prose rides along as `full` (the UI's 'more' expander).
    2026-07-07: truncation without a path to the whole text is a UI defect."""
    fulltxt = _clean(raw)
    short = _first_sentence(raw, cap)
    b = {"text": short}
    if tone is not None:
        b["tone"] = tone
    if fulltxt and short and fulltxt.strip() != short.strip() and len(fulltxt) > len(short) + 8:
        b["full"] = fulltxt[:1200]
    return b


def _band_read(score, bands):
    """bands = list of (min_inclusive, text); first match wins (descending)."""
    s = score if score is not None else -1
    for lo, txt in bands:
        if s >= lo:
            return txt
    return bands[-1][1]


# Phrases that mean WE'RE winning the eval → never frame as a competitor beating us.
_WIN_WORDS = ("best platform", "selected", "preferred vendor", "in the lead", "chosen vendor",
              "came out as the best", "won the", "front-runner", "front runner", "advanced based")
# "Competitors" that are really stalling/incumbent risk, not an active rival out-selling us.
_NON_RIVAL = ("do nothing", "do-nothing", "inertia", "renewal", "incumbent", "status quo",
              "unknown", "landscape", "none", "n/a")


def _competitor_threat(ai):
    """True only when a *named rival* is actively out-selling us. Honours the
    competitive guard: a buyer preference for us, a won eval, or a 'do-nothing /
    incumbent inertia' threat is NOT a competitor beating us."""
    cp = ai.get("competitive_position") or {}
    summ = _clean(cp.get("summary")).lower()
    pref = (ai.get("crm_evidence") or {}).get("preference") or {}
    if pref.get("present") and any(w in _clean(pref.get("value")).lower() for w in ("zycus", "in the lead", "prefer")):
        return False
    if any(w in summ for w in _WIN_WORDS):
        return False
    for c in cp.get("competitors") or []:
        nm = str(c.get("name", "")).lower()
        if any(s in nm for s in _NON_RIVAL):
            continue  # stalling / incumbent / unknown — not a rival out-selling us
        if str(c.get("sentiment", "")).lower() in ("negative", "threat", "strong"):
            return True
    return False


# Win-factor → the rich, deal-specific NARRATIVE the sweep already wrote for it. The sweep
# authors a 2-4 sentence `narrative` on every MEDDPICC element plus section summaries
# (competitive_position, champion_strength, customer_preference, ai_fit_signal …). Weaving
# these in makes every bullet SAY WHY — grounded in this deal (names/dates/quotes) — instead
# of a generic label ("Buyer is leaning our way"). Previously ONLY champion did this.
def _factor_narrative(f, ai):
    ai = ai or {}

    def d(x):
        return x if isinstance(x, dict) else {}

    medd = d(ai.get("meddpicc"))

    def mn(k):
        return _clean(d(medd.get(k)).get("narrative"))

    if f == "champion":
        return _clean(d(ai.get("champion_strength")).get("summary")) or mn("champion")
    if f == "exec_access":
        return mn("economic_buyer")
    if f == "business_case":
        return mn("metrics") or _clean(d(ai.get("business_case")).get("evidence"))
    if f == "commercial":
        return mn("paper_process")
    if f == "competitive":
        return _clean(d(ai.get("competitive_position")).get("summary")) or mn("competition")
    if f == "differentiation":
        return _clean(d(ai.get("ai_fit_signal")).get("summary")) or mn("identify_pain")
    if f == "preference":
        cp = d(ai.get("customer_preference"))
        return (_clean(cp.get("evidence")) or _clean(d(ai.get("ai_positioning_strength")).get("summary"))
                or mn("decision_criteria"))
    return ""


def build_cro_panel(record, pinned_override=None):
    """Return the cro_panel dict, or None when there's nothing to render."""
    if pinned_override:
        p = dict(pinned_override)
        p["pinned"] = True
        return p

    record = record or {}
    hard = record.get("hard") or {}
    pulse = record.get("pulse") or {}
    ai = record.get("ai") or {}
    ds = ai.get("deal_scores") or {}
    hl = ds.get("headline") or {}
    if not hl or hl.get("dead"):
        return None
    win, mom = hl.get("win_position"), hl.get("deal_momentum")
    cmt, risk = hl.get("customer_commitment"), hl.get("deal_risk")
    if win is None and mom is None:
        return None

    # AI-scored deals carry CRO-ready reason bullets (deal_scores.ai_reasons), one
    # full sentence per point, already written for a CRO/CO. Use them VERBATIM — never
    # re-derive from contributions or re-trim with _first_sentence (that chopped them
    # mid-word, e.g. "…advocate for Zycus as the…"). Deterministic deals have no
    # ai_reasons and fall through to the trimmed prose path below unchanged.
    # ONE scorer (2026-07-07): the AI scorer is OFF — scores are deterministic. So the panel's
    # per-score bullets are derived DETERMINISTICALLY below (grounded in the same factors that
    # produced the number), NEVER from LLM-authored ai_reasons — else the reasons could claim a
    # read that contradicts the deterministic number (the "correct number, wrong reasons"
    # confusion). ai_reasons is force-empty so the deterministic narrative path always wins.
    ai_reasons = {}

    def _ai_bullets(key):
        """AI-authored bullets for one score, verbatim. [] when none stored."""
        out = []
        for b in (ai_reasons.get(key) or []):
            t = _scrub_score_logic(b.get("text"))   # deal-only; drop a fully score-logic bullet
            if not t:
                continue
            out.append({"tone": "good" if b.get("tone") == "good" else "warn", "text": t})
        return out

    crm = ai.get("crm_evidence") or {}
    fp = ai.get("footprints") or {}
    trends = ai.get("opp_trends") or {}
    comp_threat = _competitor_threat(ai)
    # EB-unmapped guard: if the analysis flags the economic buyer as unmapped/unengaged,
    # don't claim "executive access" / "EB access recorded" off a bare keyword match
    # (e.g. the words "economic buyer" appearing in a Next-Step note — Allstate).
    _vuln_txt = " ".join(_clean(v.get("detail")) for v in
                         ((ai.get("vulnerabilities") or {}).get("items") or [])).lower()
    eb_unmapped = (("economic buyer" in _vuln_txt or "exec" in _vuln_txt) and
                   any(w in _vuln_txt for w in ("unmapped", "unengaged", "never", "not confirmed",
                                                "not identified", "not activated", "not been activated",
                                                "no cfo", "has not appeared", "no economic buyer")))

    # ---- header ----
    acct = hard.get("account_name") or hard.get("opp_name") or "This deal"
    stage = hard.get("stage") or pulse.get("stage") or ""
    amt = _compact_amount(hard.get("amount"))
    close = _human_date(hard.get("close_date"))
    hbits = [stage] + ([amt] if amt else []) + ([f"closes {close}"] if close else [])
    header = f"{acct} — " + " · ".join([b for b in hbits if b])

    # ---- intro frame ----
    risk_hi = (risk or 0) >= 30
    mom_lo = (mom or 0) < 50
    if (win or 0) >= 60 and not comp_threat and not risk_hi and not mom_lo:
        frame = "We're well-positioned — the real risk is timing and sign-off, not a competitor."
    elif (win or 0) >= 60 and (risk_hi or mom_lo):
        frame = "Strong on paper for the stage, but it's stalling — the 'what could lose it' block is the real story."
    elif (win or 0) >= 45:
        frame = "It's genuinely competitive and could go either way; the reads below say where it stands."
    else:
        frame = "We're behind on this one — the reads below say why and what would change it."
    # Lead line: prefer a sweep-authored, deal-specific narrative headline (one crisp
    # sentence naming the champion, the forcing date and the ONE thing between us and the
    # win) when the sweep supplies it; else the generic stage-framed sentence.
    _lead = _scrub_score_logic((ai.get("deal_scores_evidence") or {}).get("summary"))
    intro = _lead or ("One read per score, in language a CRO/CO can act on. " + frame)

    blocks = []

    # ---- WIN block ----
    win_read = _band_read(win, [
        (70, "We're ahead."), (55, "We're in it, with a slight edge."),
        (45, "Too close to call."), (30, "We're behind."), (-1, "We're well behind — this one is cold."),
    ])
    wc = (ds.get("win_position") or {}).get("contributions") or []
    win_bullets = []
    TREND = {"forecast_category_trend": "forecast_category_trend_detail",
             "amount_trend": "amount_trend_detail", "close_date_trend": "close_date_trend_detail"}
    for c in sorted(wc, key=lambda x: -abs(float(x.get("points") or 0))):
        f = c.get("factor"); pts = float(c.get("points") or 0)
        if f == "momentum_adj" or abs(pts) < 0.5:
            continue
        if f in _WIN_FACTOR:
            # Don't assert exec access / preference off a keyword when the EB is unmapped.
            if f == "exec_access" and pts >= 0 and eb_unmapped:
                continue
            crm_e = crm.get(f) or {}
            src = (crm_e.get("src") or "").lower()
            pos, neg = _WIN_FACTOR[f]
            label = pos if pts >= 0 else neg
            # COMPETITIVE (2026-07-07): a weak-positive competitive strength means 'credible rivals
            # present but the field is UNKNOWN / even' — that is NOT an edge. Only a real edge
            # (sole-source / buyer-preferred, pts >= ~2.0) keeps "Edge over the competition"; an
            # unknown or merely-even field reads as the open question it is (Barnes & Noble:
            # "unknown competitors" was wrongly rendered "✅ Edge over the competition").
            force_warn = False
            if f == "competitive" and 0 <= pts < 2.0:
                _cst = str(((ai.get("meddpicc") or {}).get("competition") or {}).get("status") or "").lower()
                _unknown = _cst in ("gap", "unknown", "missing", "")
                label = "Competitive field still unmapped" if _unknown else "No decisive edge over the field yet"
                force_warn = True
            # Prefer the rich, deal-specific SECTION NARRATIVE the sweep already wrote for
            # THIS factor (economic_buyer / competition / paper_process narratives,
            # competitive & champion summaries, customer_preference …) over the robotic
            # contribution string or a bare keyword. This is what makes every bullet SAY WHY
            # ("Buyer is leaning our way — Nishan confirmed we're 1st on the product
            # assessment; only blocker is FSI-India experience, per the 24 Jun call") instead
            # of a generic label. The untrimmed narrative rides `full` for the "more" expander.
            text = ""
            full_txt = None
            narr_full = _factor_narrative(f, ai)
            if narr_full:
                narr = _first_sentence(narr_full, 160)
                if narr and narr.lower() != f.replace("_", " "):
                    text = f"{label} — {narr}"
                    if len(narr_full) > len(narr):
                        full_txt = f"{label} — {narr_full}"
            if not text:
                body = _strip_evi(c.get("evidence"), crm_e.get("value"))
                # A Next-Step keyword match ("'pain point' in Next-Step/narrative") strips to a
                # cryptic fragment — the LABEL alone is the real, readable reason; drop the
                # fragment. Keep the body only when it's grounded MEDDPICC-style prose.
                keyword_only = ("next-step" in src or "narrative" in src or "next step" in src)
                if not body or keyword_only or body.lower() == f.replace("_", " ") or len(body) < 14:
                    text = label
                else:
                    text = f"{label} — {body}"
            bl = {"tone": "warn" if (force_warn or pts < 0) else "good", "text": text}
            if full_txt and full_txt != text:
                bl["full"] = full_txt
            win_bullets.append(bl)
        elif f in TREND:
            detail = _clean(trends.get(TREND[f]))
            if detail:
                win_bullets.append({"tone": "good" if pts >= 0 else "warn", "text": detail})
    # de-dup + cap
    seen, wb = set(), []
    for b in win_bullets:
        k = b["text"][:40]
        if k in seen:
            continue
        seen.add(k); wb.append(b)
    win_bullets = wb[:7]
    # Fold the TOP RISKS into the win block itself — the deal-score reason must carry the
    # downside INLINE (user: "win position should include the risks", "you don't have to make
    # a different column for it"). Up to two live vulnerabilities become ⚠️ bullets here, in
    # addition to the standalone "What could lose it" block. Skipped when the sweep supplies
    # its own win_position ai_reasons (those already fold risk in).
    _wvulns = [v for v in ((ai.get("vulnerabilities") or {}).get("items") or [])
               if str(v.get("status", "")).lower() not in ("resolved", "completed", "closed")]
    _radded = 0
    for v in _wvulns:
        d = _first_sentence(v.get("detail"), 160)
        k = (d or "")[:40]
        if d and k not in seen:
            win_bullets.append({"tone": "warn", "text": d})
            seen.add(k); _radded += 1
        if _radded >= 2:
            break
    win_bullets = win_bullets[:8]
    _ai_win = _ai_bullets("win_position")
    if _ai_win:
        win_bullets = _ai_win
    # Nothing grounded surfaced — fall back to the champion / competitive narrative
    # so the block still says *something* real rather than sitting empty.
    if not win_bullets:
        for src, tone in ((ai.get("champion_strength") or {}).get("summary"), "good"), \
                         ((ai.get("competitive_position") or {}).get("summary"), "warn"):
            full = _clean(src)
            t = _first_sentence(full, 170)
            if t:
                bl = {"tone": tone, "text": t}
                if len(full) > len(t):
                    bl["full"] = full
                win_bullets.append(bl)
                break
    # Honest read: a high stage-anchored score with no grounded positive signal
    # underneath isn't really an "edge" — say so rather than imply one.
    if (win or 0) >= 55 and not any(x["tone"] == "good" for x in win_bullets):
        win_read = "Late-stage by the book, but the signals underneath are thin."

    # The stage anchor + ceiling still drive the SCORE (deal_engine_scoring), but we do NOT
    # surface the "a deal at 'Shortlisted' anchors near X … caps confidence at 70" mechanic
    # in the panel — it read as textbook boilerplate. The deal-specific "why this number"
    # lives in the intro lead + the win bullets (user-directed 2026-07-06: keep the stage-cap
    # logic strictly internal to scoring, never spoken in the reasons).
    blocks.append({"kind": "score", "key": "win_position", "score": _r0(win),
                   "title": "Zycus win position", "sub": "can we win it?",
                   "read": win_read, "bullets": win_bullets})

    # ---- MOMENTUM block ----
    mom_read = _band_read(mom, [
        (75, "Accelerating — one of the hotter deals in the book."),
        (60, "Moving — steady forward motion."),
        (50, "Lukewarm — some motion, but not strong."),
        (40, "Flat — little is happening."),
        (-1, "Going quiet — engagement has dropped off."),
    ])
    # Reasons come from the DETERMINISTIC momentum contributions (v3: close-date direction,
    # genuine buyer-touch recency, confidence-% trajectory, real-session engagement,
    # false-velocity), so the bullets ALWAYS match the score — never the stale footprints
    # (buyer_touches_30d etc.) that contradicted a slipping score with "buyer touched 3×".
    mb = []
    for c in (ds.get("deal_momentum") or {}).get("contributions") or []:
        evi = _clean(c.get("evidence"))
        if not evi:
            continue
        pts = float(c.get("points") or 0)
        # +points = forward (good), −points = slipping (warn); 0-point factors are commentary
        # (false_velocity / engagement_ignored / one_sided) — always a caution.
        tone = "good" if pts > 0.1 else "warn"
        mb.append(_btext(evi, 150, tone=tone))
    # de-dup
    _seenm, _mb = set(), []
    for b in mb:
        k = b["text"][:40]
        if k in _seenm:
            continue
        _seenm.add(k); _mb.append(b)
    mb = _mb
    if not mb:  # contributions absent — fall back to pulse
        cr = pulse.get("calls_read")
        if cr:
            mb.append({"tone": "good", "text": f"{cr} call{'s' if cr != 1 else ''} read in the window"})
        la = pulse.get("last_activity_date")
        if la:
            mb.append({"tone": "warn", "text": f"Last recorded activity {la}"})
        # 2026-07-09 (Publicis): `last_activity_date` is already guarded to never be a
        # future date (deal_engine_pulse splits a future SF LastActivityDate out as
        # next_scheduled_date). Surface that honestly — as an upcoming touch, never as
        # something already "recorded" — instead of silently dropping the fact.
        nsd = pulse.get("next_scheduled_date")
        if nsd and not la:
            mb.append({"tone": "good", "text": f"Next meeting scheduled {nsd}"})
    _ai_mom = _ai_bullets("deal_momentum")
    if _ai_mom:
        mb = _ai_mom
    blocks.append({"kind": "score", "key": "deal_momentum", "score": _r0(mom),
                   "title": "Deal momentum", "sub": "is it moving?",
                   "read": mom_read, "bullets": mb[:6], "how": None})

    # ---- RISK block (risk guard: show even when deal_risk == 0 if real threats exist) ----
    vulns = [v for v in ((ai.get("vulnerabilities") or {}).get("items") or []) if str(v.get("status", "")).lower() != "resolved"]
    odl = (ai.get("open_deliverables") or {}).get("items") or []
    overdue = [d for d in odl if str(d.get("status", "")).lower() == "overdue"]
    risk_bullets = []
    for v in vulns[:4]:
        risk_bullets.append(_btext(v.get("detail"), 170, tone="warn"))
    for d in overdue[:2]:
        due = _human_date(d.get("due"))
        risk_bullets.append({"tone": "warn",
                             "text": _first_sentence(d.get("commitment"), 120) + (f" — overdue (was due {due})" if due else " — overdue")})
    cdt = _clean(trends.get("close_date_trend_detail"))
    if cdt and "push" in cdt.lower():
        risk_bullets.append({"tone": "warn", "text": cdt})
    _ai_risk = _ai_bullets("deal_risk")
    if _ai_risk:
        risk_bullets = _ai_risk
    if risk_bullets:
        risk_read = ("The threat is the deal stalling into 'do nothing', not a competitor beating us."
                     if not comp_threat else "There's a live competitive threat plus execution risk — both below.")
        footer = None
        cp = ai.get("competitive_position") or {}
        if cp.get("summary"):
            footer = "Competitive picture: " + _first_sentence(cp.get("summary"), 240)
        elif not comp_threat:
            footer = ("No competitor is out-selling us — the real threat is 'do nothing': the buyer keeps "
                      "solving this with their current/manual process and never signs. That's a no-decision "
                      "risk (timing, internal priority, sign-off), not a loss to a rival.")
        # de-dup risk bullets
        seen, rb = set(), []
        for b in risk_bullets:
            k = b["text"][:45]
            if k in seen or not b["text"]:
                continue
            seen.add(k); rb.append(b)
        blocks.append({"kind": "risk", "score": _r0(risk),
                       "title": "What could lose it", "sub": "the honest downside",
                       "read": risk_read, "bullets": rb[:5], "footer": footer})

    # ---- COMMITMENT block: intentionally NOT rendered (user-directed 2026-07-06). The
    # "Customer commitment" score + reasons are suppressed from the CRO panel; win position,
    # momentum and "what could lose it" carry the story a CRO acts on.

    # ---- MOVES block ----
    moves = []
    for m in sorted((ai.get("recommended_moves") or {}).get("items") or [], key=lambda x: x.get("rank", 99))[:3]:
        act = _first_sentence(m.get("action"), 320)
        by = _human_date(m.get("act_by"))
        if act:
            moves.append(act + (f" (by {by})" if by else ""))
    if not moves:
        for g in ((ai.get("gaps") or {}).get("items") or [])[:2]:
            t = _first_sentence(g.get("area") or g.get("quote"), 120)
            if t:
                moves.append("Close the gap: " + t)
    if moves:
        blocks.append({"kind": "moves", "title": "What moves it forward", "items": moves})

    _polish_panel(blocks)

    return {
        "pinned": False,
        "generated": True,
        "schema": 1,
        "header": header,
        "intro": intro,
        "blocks": blocks,
    }


# ---- PANEL POLISH (2026-07-07, "fix the UI") -------------------------------------------
# The rendered panel must read like an analyst wrote it: no engine internals ("top 'event'
# is a narrative/analysis note — ignored"), no raw Salesforce API names (AIS_Score__c), no
# ASCII arrows, and never the SAME bullet repeated across blocks (the close-date push was
# showing three times: win, momentum and risk).
_ENGINE_SPEAK = re.compile(
    r"narrative/analysis note|not a real session|\bignored\b|footprints?\.|jsonb|"
    r"carried[- ]forward packet|sanitiz|calls_read|\bev(ents)?_30d\b|"
    # data-plumbing / SOQL read errors must never surface as a deal reason (a failed field
    # read is not evidence of losing — Global Switch 'AIS_* INVALID_FIELD' leaking as
    # 'losing on capability fit'):
    r"invalid_field|\bsoql\b|not returned by|field(?:s)? (?:not|were not) (?:returned|found)|"
    r"no ais field|read (?:error|artifact)|mcp (?:error|timeout)", re.I)
_SF_API_NAME = re.compile(r"\b\w+__c\b")
_ISO_DATE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
_MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _pretty_dates(t):
    def _one(m):
        y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
        return f"{int(d)} {_MONTHS[mo]} {y}" if 1 <= mo <= 12 else m.group(0)
    return _ISO_DATE.sub(_one, t)


def _polish_text(t):
    """Human-polish one bullet; return '' to drop it entirely. SURGICAL: when only PART of a
    bullet is plumbing (a SOQL/API-name sentence bolted onto real deal evidence — Bosch:
    'AIS_* not returned in this sweep. Call evidence shows Bosch is genuinely curious about
    agentic AI…'), drop only the offending SENTENCE and keep the deal content — don't blank
    the whole bullet or replace it with a false 'not recorded'."""
    t = _clean(t)
    if not t:
        return ""
    if _ENGINE_SPEAK.search(t) or _SF_API_NAME.search(t):
        sents = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", t)
        kept = [s for s in sents if s.strip() and not _ENGINE_SPEAK.search(s) and not _SF_API_NAME.search(s)]
        t = " ".join(kept).strip()
        if not t or len(t) < 15:
            return ""                   # nothing but plumbing was here
    t = t.replace(" -> ", " → ").replace("->", "→")
    t = _pretty_dates(t)
    return t


def _polish_panel(blocks):
    seen = set()
    for bl in blocks:
        items = bl.get("bullets")
        if not isinstance(items, list):
            continue
        out = []
        for b in items:
            is_dict = isinstance(b, dict)
            txt = _polish_text(b.get("text") if is_dict else b)
            if not txt:
                continue
            key = re.sub(r"\W+", "", txt.lower())[:60]
            if key in seen:
                continue                 # one fact appears ONCE across the whole panel
            seen.add(key)
            out.append({**b, "text": txt} if is_dict else txt)
        bl["bullets"] = out
        if bl.get("footer"):
            bl["footer"] = _polish_text(bl["footer"]) or None
