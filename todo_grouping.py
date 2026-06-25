"""Deterministic homogeneous-grouping for the to-do surface (belt-and-suspenders).

The sweep prompt asks the model to collapse near-duplicate `open_deliverables` and
`best_practice_check.flags` into one entry per theme (4-bucket MECE model, 2026-06-23).
But the living-memory carry-forward contract ("never drop a known fact") pulls the
other way, and in practice the model re-lists the same point many times (Publicis:
58 commitments + 137 best-practice flags, almost all duplicates of ~7 themes).

This module is the deterministic safety net: regardless of what the model emits, it
clusters homogeneous items by token-set similarity and keeps ONE merged entry per
theme (carrying all dates + provenance, so nothing is lost). Pure, idempotent, no
external deps — unit-testable in isolation and called once just before persist.
"""
from __future__ import annotations
import re

# Filler/verbs/entities that do NOT identify the *deliverable* — the same item is
# phrased many ways ("send revised OF1", "send new OF1 for FY26"), so we key on the
# NOUN content, not the verb or the date or the party.
_STOP = {
    "the", "a", "an", "to", "of", "for", "and", "or", "by", "on", "in", "with", "is",
    "are", "be", "we", "our", "us", "they", "their", "them", "this", "that", "at", "as",
    "it", "its", "from", "per", "not", "yet", "no", "new", "revised", "first", "second",
    "third", "fourth", "round", "version", "send", "sending", "provide", "schedule",
    "scheduling", "secure", "finalize", "finalise", "ensure", "confirm", "get", "make",
    "push", "start", "begin", "complete", "address", "answer", "come", "back", "return",
    "due", "open", "overdue", "still", "again", "also", "now", "asap", "week", "next",
    "early", "late", "end", "beginning", "month", "june", "july", "before", "after",
}
_MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"


def _sig(text: str) -> set:
    """Normalised token-set signature of an item: lowercase, strip dates / order-form
    version numbers / punctuation / filler, keep the content nouns."""
    t = (text or "").lower()
    t = re.sub(r"\d{4}-\d{2}-\d{2}", " ", t)                       # ISO dates
    t = re.sub(rf"\b\d{{1,2}}\s*(?:{_MONTHS})\w*\b", " ", t)        # "15 jun"
    t = re.sub(rf"\b(?:{_MONTHS})\w*\b", " ", t)                    # bare month
    t = re.sub(r"\bof[12]\b", "orderform", t)                       # OF1/OF2 = order-form family
    t = re.sub(r"[^a-z\s]", " ", t)
    return {w for w in t.split() if len(w) > 2 and w not in _STOP}


def _overlap(a: set, b: set) -> float:
    """Overlap coefficient |A∩B| / min(|A|,|B|). Better than Jaccard for verbose
    items whose shared THEME is a small core inside long, differently-worded
    sentences (e.g. 30 phrasings of 'no economic buyer')."""
    if not a or not b:
        return 0.0
    m = min(len(a), len(b))
    return len(a & b) / m if m else 0.0


def _cluster(texts, threshold: float):
    """Greedy single-link clustering by signature overlap.
    Returns a list of clusters; each is {"sig": set, "idx": [original indices]}.
    Guards against a tiny (1-2 token) signature acting as a promiscuous bridge by
    requiring at least 2 shared content tokens to merge."""
    clusters = []
    for i, txt in enumerate(texts):
        sig = _sig(txt)
        best = None
        best_sim = threshold
        for c in clusters:
            inter = len(sig & c["sig"])
            # require ≥2 shared tokens normally, but allow a short item (≤2 content
            # tokens, e.g. "finalize agreement") to merge on its single strong token.
            if inter < min(2, len(sig), len(c["sig"])):
                continue
            s = _overlap(sig, c["sig"])
            if s >= best_sim:
                best_sim = s
                best = c
        if best is None:
            clusters.append({"sig": set(sig), "idx": [i]})
        else:
            best["sig"] |= sig
            best["idx"].append(i)
    return clusters


def _norm_who(who: str) -> str:
    """Two sides only: 'zycus' (us/seller) vs 'buyer' (the prospect, by any name —
    'Buyer', 'Publicis', an account name, a buyer-side person)."""
    w = (who or "").strip().lower()
    return "zycus" if any(t in w for t in ("zycus", "we ", "us", "seller", "our")) else "buyer"


def _slug(sig: set, n: int = 3) -> str:
    return "_".join(sorted(sig)[:n]) or "item"


_STATUS_RANK = {"overdue": 3, "open": 2, "no due date": 1, "completed": 0}


def _deliv_text(d: dict) -> str:
    """Clustering text for a deliverable item. The 4-head shape uses `deliverable`;
    the legacy open_deliverables used `commitment`; implicit needs use `inferred_need`."""
    return d.get("commitment") or d.get("deliverable") or d.get("inferred_need") or ""


def _group_open_deliverables(block: dict) -> int:
    items = [x for x in (block.get("items") or []) if isinstance(x, dict)]
    if len(items) < 2:
        return 0
    # cluster WITHIN each who-bucket so a Zycus item never merges with a Buyer item
    by_who: dict = {}
    for it in items:
        by_who.setdefault(_norm_who(it.get("who")), []).append(it)
    merged = []
    for _who, group in by_who.items():
        clusters = _cluster([_deliv_text(g) for g in group], 0.5)
        for c in clusters:
            members = [group[i] for i in c["idx"]]
            rep = max(members, key=lambda m: len(_deliv_text(m)))
            statuses = [str(m.get("status") or "").lower() for m in members]
            if statuses and all(s == "completed" for s in statuses):
                status = "completed"
            else:
                status = max((s for s in statuses), key=lambda s: _STATUS_RANK.get(s, 1)) \
                    if statuses else rep.get("status")
            dates = [m.get("date") for m in members if m.get("date")]
            dues = [m.get("due") for m in members if m.get("due")]
            srcs = [m.get("source") for m in members if m.get("source")]
            out = dict(rep)
            out["status"] = status
            out["date"] = min(dates) if dates else rep.get("date")
            out["due"] = max(dues) if dues else rep.get("due")
            out["group_key"] = rep.get("group_key") or _slug(c["sig"])
            out["waiting_on_buyer"] = any(bool(m.get("waiting_on_buyer")) for m in members)
            if srcs:
                out["source"] = max(srcs, key=len)
            merged.append(out)
    block["items"] = merged
    return len(items) - len(merged)


def _flag_text(f):
    if isinstance(f, str):
        return f
    if isinstance(f, dict):
        return f.get("flag") or f.get("text") or f.get("play") or ""
    return ""


def _group_best_practice(block: dict) -> int:
    flags = [f for f in (block.get("flags") or []) if _flag_text(f).strip()]
    if len(flags) < 2:
        return 0
    clusters = _cluster([_flag_text(f) for f in flags], 0.40)
    reps = []
    for c in clusters:
        members = [flags[i] for i in c["idx"]]
        # keep the longest / most specific phrasing as the representative
        reps.append(max(members, key=lambda m: len(_flag_text(m))))
    block["flags"] = reps
    return len(flags) - len(reps)


def group_todo_lists(parsed: dict) -> dict:
    """Collapse homogeneous open_deliverables + best_practice flags in-place.
    Never raises — a failure leaves the lists untouched."""
    try:
        ai = parsed.get("ai") or {}
        # 4-head shape: group each implicit sub-bucket; legacy: open_deliverables.
        impl = ai.get("implicit_requirements")
        if isinstance(impl, dict) and ("we_promised" in impl or "buyer_dependent" in impl):
            for _side in ("we_promised", "buyer_dependent"):
                blk = impl.get(_side)
                if isinstance(blk, dict):
                    n = _group_open_deliverables(blk)
                    if n:
                        print(f"[TODO-GROUP] implicit.{_side} -{n} (grouped homogeneous)", flush=True)
        else:
            od = ai.get("open_deliverables")
            if isinstance(od, dict):
                n = _group_open_deliverables(od)
                if n:
                    print(f"[TODO-GROUP] open_deliverables -{n} (grouped homogeneous)", flush=True)
        bp = ai.get("best_practice_check")
        if isinstance(bp, dict):
            n = _group_best_practice(bp)
            if n:
                print(f"[TODO-GROUP] best_practice flags -{n} (grouped homogeneous)", flush=True)
    except Exception as e:  # noqa: BLE001 — never block persist
        print(f"[TODO-GROUP] skipped: {type(e).__name__}: {e}", flush=True)
    return parsed


# ---------------------------------------------------------------------------
# Cross-bucket de-collision.
#
# Within-block grouping (above) collapses *same-block* near-duplicates. But the
# UI's four buckets draw from THREE different ai.* blocks (recommended_moves +
# open_deliverables -> "Next phase"/"Waiting on buyer"; best_practice_check ->
# "Best practices"), and the model legitimately restates the SAME live thread in
# all three (Publicis: "unstick the technical workshop" appears as a move, a
# deliverable AND a best-practice flag). No amount of within-block grouping fixes
# that — it is a *cross*-block collision.
#
# MECE rule (the 4-bucket model): best_practice_check is for genuine gaps that
# carry NO owned action. A flag that merely restates a recommended_move or an
# open_deliverable already lives in "Next phase"/"Waiting on buyer", so it is
# dropped here. Flags that have no matching action (a true single-thread /
# no-EB / no-ROI gap with nothing being done about it) are KEPT.
# ---------------------------------------------------------------------------

# An action's content core is short (a move/commitment phrase), so a long flag
# that contains most of that core is the same theme. Require >=2 shared content
# tokens AND high overlap of the *action's* (smaller) signature.
_DECOLLIDE_THRESHOLD = 0.55


def _move_text(m) -> str:
    if isinstance(m, str):
        return m
    if isinstance(m, dict):
        return m.get("action") or m.get("move") or m.get("text") or m.get("play") or ""
    return ""


def _action_signatures(ai: dict) -> list:
    """Token-set signatures of every item that represents an OWNED next action —
    recommended_moves + open_deliverables. A best-practice flag whose theme
    matches one of these is a cross-bucket duplicate, not a standalone gap."""
    sigs = []
    moves = ai.get("recommended_moves")
    if isinstance(moves, dict):
        for m in (moves.get("items") or []):
            s = _sig(_move_text(m))
            if s:
                sigs.append(s)

    def _add_block(block):
        if not isinstance(block, dict):
            return
        for d in (block.get("items") or []):
            txt = _deliv_text(d) if isinstance(d, dict) else (d if isinstance(d, str) else "")
            s = _sig(txt or "")
            if s:
                sigs.append(s)

    impl = ai.get("implicit_requirements")
    if isinstance(impl, dict) and ("we_promised" in impl or "buyer_dependent" in impl):
        _add_block(impl.get("we_promised"))
        _add_block(impl.get("buyer_dependent"))
    else:
        _add_block(ai.get("open_deliverables"))
    return sigs


def _restates_action(flag_sig: set, action_sigs: list) -> bool:
    if not flag_sig:
        return False
    for a in action_sigs:
        if len(flag_sig & a) >= 2 and _overlap(flag_sig, a) >= _DECOLLIDE_THRESHOLD:
            return True
    return False


def decollide_buckets(parsed: dict) -> dict:
    """Drop best_practice flags that restate an owned action (move/deliverable).
    Keeps true action-less gaps. In-place, idempotent, never raises."""
    try:
        ai = parsed.get("ai") or {}
        bp = ai.get("best_practice_check")
        if not isinstance(bp, dict):
            return parsed
        flags = [f for f in (bp.get("flags") or []) if _flag_text(f).strip()]
        if not flags:
            return parsed
        action_sigs = _action_signatures(ai)
        if not action_sigs:
            return parsed
        kept = [f for f in flags if not _restates_action(_sig(_flag_text(f)), action_sigs)]
        dropped = len(flags) - len(kept)
        if dropped:
            bp["flags"] = kept
            print(f"[TODO-DECOLLIDE] best_practice -{dropped} (restate an owned action; "
                  f"kept {len(kept)} true gaps)", flush=True)
    except Exception as e:  # noqa: BLE001 — never block persist
        print(f"[TODO-DECOLLIDE] skipped: {type(e).__name__}: {e}", flush=True)
    return parsed


def tidy(parsed: dict) -> dict:
    """Full to-do hygiene in one call: within-block homogeneous grouping THEN
    cross-bucket de-collision. Idempotent, only ever reduces, never raises.
    Call this at the projection chokepoint so every sweep yields clean buckets
    by construction (not as a skippable post-step)."""
    group_todo_lists(parsed)
    decollide_buckets(parsed)
    return parsed
