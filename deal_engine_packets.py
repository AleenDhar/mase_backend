"""Per-deal living memory: durable insight packets + a change-log of deltas.

This module is the deterministic, side-effect-free core of the deal sweep's
"living memory". Today a sweep OVERWRITES the deal's analysis, so anything the
new sweep does not re-mention is lost. Here we keep a durable per-deal store:

  * packets  -- the insight store and source of truth. One packet per durable
               topic (a requirement, a stakeholder, a competitor, a risk, ...).
  * deltas   -- a newest-first change log ("what changed since last sweep").

Both live INSIDE the existing record JSON (no DB migration). After each sweep we
reconcile the new evidence into the packets and then regenerate the packet-backed
`ai.*` item lists by PROJECTION, so the existing dashboard contract is unchanged.

Design choice (deliberate, documented): candidate packets are derived
SERVER-SIDE from the agent's already-emitted `ai.*`/`hard` sections rather than
asking the model for a separate `candidate_packets` array. That guarantees we can
never lose a section the agent did emit (the candidates ARE the agent's current
items), while still merging in retained facts from prior sweeps. The prompt only
needs a light "reuse the same wording for recurring topics" nudge so the stable
keys line up across days.

Retention policy (the absence rule):
  * DURABLE facts (requirement, pain, stakeholder, risk, hygiene, champion) are
    RETAINED when a sweep does not re-mention them: they go `dormant`, never
    deleted, and still project into the dashboard.
  * STATE / EVENT facts (product_scope, competitor, commitment) are left active
    on absence (latest-wins via the change rule; commitments are append-only).
Nothing is ever blind-deleted. A fact only becomes `superseded`/`resolved` on an
explicit contradicting/closing signal.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
from typing import Any, Optional

# Facts whose ABSENCE from a sweep means "not re-mentioned", not "gone". They go
# dormant (retained + still projected) rather than disappearing.
DURABLE_TYPES = {"requirement", "pain", "stakeholder", "risk", "hygiene", "champion"}

# All packet types we mint. Non-durable ones (product_scope, competitor,
# commitment) are left active on absence.
KNOWN_TYPES = DURABLE_TYPES | {"product_scope", "competitor", "commitment"}

# Packet types that are NOT rendered into a stakeholder/requirement/etc. list.
# `champion` and `product_scope` are singletons we track purely for the change
# feed + history; the dashboard reads champion strength / scope from elsewhere.
_NON_PROJECTED_LIST_TYPES = {"champion"}

_MAX_SLUG = 60


# ---------------------------------------------------------------------------
# Keys & value comparison
# ---------------------------------------------------------------------------

def _slug(s: Any) -> str:
    txt = re.sub(r"[^a-z0-9]+", "-", str(s or "").strip().lower()).strip("-")
    return txt[:_MAX_SLUG] or "unknown"


def make_key(ptype: str, subject: Any) -> str:
    return f"{ptype}:{_slug(subject)}"


def _key_of(c: dict) -> Optional[str]:
    k = (c.get("key") or "").strip()
    if k:
        return k
    t = (c.get("type") or "").strip()
    subj = c.get("subject")
    if not t or subj in (None, ""):
        return None
    return make_key(t, subj)


def _type_of_key(key: str) -> str:
    return key.split(":", 1)[0] if ":" in key else key


def _canon(v: Any) -> str:
    """Stable string form of a value for equality checks (order-insensitive,
    whitespace-trimmed)."""
    def _clean(x):
        if isinstance(x, str):
            return x.strip()
        if isinstance(x, dict):
            return {k: _clean(val) for k, val in x.items() if val not in (None, "")}
        if isinstance(x, list):
            return [_clean(i) for i in x]
        return x
    try:
        return json.dumps(_clean(v), sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(v)


def _value_equal(a: Any, b: Any) -> bool:
    return _canon(a) == _canon(b)


# Per-type fields that constitute a MEANINGFUL change. Volatile fields (a
# refreshed quote, a re-stated date, last_contact_date) are excluded so a daily
# re-sweep that merely re-quotes the same fact does NOT spam the change feed: we
# still refresh the stored value to the latest, but log a `changed` delta only
# when one of these significant fields actually moves. Subject-derived fields
# (e.g. a requirement's text == its subject) are already pinned by the key, so
# they cannot differ within the same packet. Types absent here fall back to
# whole-value comparison.
_SIGNIFICANT_FIELDS: dict[str, tuple] = {
    "requirement": ("addressed", "kind"),
    "stakeholder": ("role", "title", "sentiment", "risk"),
    "competitor": ("sentiment",),
    "risk": ("status", "category"),
    "commitment": ("status", "due"),
    "champion": ("name", "strength", "at_risk"),
    "product_scope": ("scope",),
    "hygiene": (),
    "pain": (),
}


def _significant(ptype: str, value: Any) -> Any:
    """Project a value down to the fields whose change is meaningful for `ptype`."""
    if not isinstance(value, dict):
        return value
    fields = _SIGNIFICANT_FIELDS.get(ptype)
    if fields is None:
        return value
    return {k: value.get(k) for k in fields}


_BRIEF_KEYS = ("scope", "name", "requirement", "inferred_need", "detail",
               "commitment", "flag", "role", "title", "status", "strength")


def _brief(value: Any) -> str:
    """A short human string for a value, used in the change feed's from/to."""
    if isinstance(value, dict):
        parts = []
        for k in _BRIEF_KEYS:
            if value.get(k):
                parts.append(str(value[k]))
            if len(parts) >= 2:
                break
        if parts:
            return " - ".join(parts)[:160]
        try:
            return json.dumps(value, ensure_ascii=False, default=str)[:160]
        except (TypeError, ValueError):
            return str(value)[:160]
    return str(value)[:160] if value not in (None, "") else ""


def _delta(today: str, packet: dict, kind: str, *, frm: str = "", to: str = "",
           reason: Any = None, source: Any = None) -> dict:
    d = {
        "date": today,
        "kind": kind,
        "type": packet.get("type"),
        "subject": packet.get("subject"),
        "key": packet.get("key"),
    }
    if frm:
        d["from"] = frm
    if to:
        d["to"] = to
    if reason:
        d["reason"] = reason
    if source:
        d["source"] = source
    return d


# ---------------------------------------------------------------------------
# Human-readable presentation of a delta (for the "What changed" panel).
#
# Pure, read-time formatting so it also works on deltas written by older
# sweeps (no re-sweep required). Two derived fields:
#   - group: one of added / changed / resolved / dormant (the four buckets a
#            rep cares about; reactivated rolls into "added", superseded into
#            "resolved").
#   - label: a short headline, e.g. "New requirement: SAP integration".
# ---------------------------------------------------------------------------

# Singular, lower-case noun for each packet type.
_TYPE_NOUNS = {
    "requirement": "requirement",
    "stakeholder": "stakeholder",
    "competitor": "competitor",
    "risk": "risk",
    "commitment": "commitment",
    "hygiene": "hygiene flag",
    "champion": "champion",
    "product_scope": "product scope",
}

# Map each raw kind onto one of the four rep-facing buckets.
_KIND_GROUP = {
    "added": "added",
    "reactivated": "added",
    "changed": "changed",
    "resolved": "resolved",
    "superseded": "resolved",
    "retired": "resolved",
    "dormant": "dormant",
}

# Subjects that carry no information on their own (the key suffix is generic),
# so we drop them from the label rather than print "Champion changed: champion".
_GENERIC_SUBJECTS = {"champion", "scope", "product_scope", ""}


def delta_group(kind: Any) -> str:
    """The rep-facing bucket for a delta kind (added/changed/resolved/dormant)."""
    return _KIND_GROUP.get(str(kind or "").strip().lower(), "changed")


def _noun(typ: Any) -> str:
    t = str(typ or "").strip().lower()
    return _TYPE_NOUNS.get(t, t.replace("_", " ") or "item")


def delta_label(delta: dict) -> str:
    """A short human headline for one delta. Never raises; falls back to a
    generic phrasing for unknown kinds/types."""
    if not isinstance(delta, dict):
        return ""
    kind = str(delta.get("kind") or "").strip().lower()
    noun = _noun(delta.get("type"))
    cap = noun[:1].upper() + noun[1:]
    subject = str(delta.get("subject") or "").strip()
    has_subj = subject and subject.lower() not in _GENERIC_SUBJECTS
    tail = f": {subject}" if has_subj else ""

    if kind == "added":
        return f"New {noun}{tail}"
    if kind == "reactivated":
        return f"{cap} back in play{tail}"
    if kind == "changed":
        return f"{cap} updated{tail}"
    if kind == "resolved":
        return f"{cap} resolved{tail}"
    if kind == "superseded":
        return f"{cap} superseded{tail}"
    if kind == "retired":
        return f"{cap} retired (stale){tail}"
    if kind == "dormant":
        return f"{cap} went quiet{tail}"
    # Unknown kind: best-effort.
    return f"{cap} {kind}{tail}".strip() if kind else f"{cap}{tail}"


def present_delta(delta: dict) -> dict:
    """Return a copy of `delta` enriched with `label` + `group` for the UI."""
    if not isinstance(delta, dict):
        return {}
    return {**delta, "label": delta_label(delta), "group": delta_group(delta.get("kind"))}


# ---------------------------------------------------------------------------
# Reconcile
# ---------------------------------------------------------------------------

def reconcile(existing_packets: list, candidate_packets: list,
              today: str) -> tuple[list, list]:
    """Merge a sweep's candidate packets into the deal's existing packets.

    Pure function. Returns (packets, deltas_for_this_sweep). Cases handled:
      * insert     -- a key not seen before becomes a new active packet.
      * freshen    -- same key, same value: last_confirmed bumped (dormant ->
                      active reactivation emits a delta).
      * change     -- same key, different value: old value pushed to history, new
                      value stored, `changed` delta with from/to recorded.
      * dormant    -- a DURABLE packet absent from this sweep is retained and
                      marked dormant (one delta). Non-durable absent packets are
                      left active.
      * supersede  -- a candidate may list `supersedes: [keys]` to mark an old
                      packet superseded (linked + reason retained).
      * resolve    -- a candidate may list `resolves: [keys]` (or carry
                      value.status == "resolved") to close a packet.
    Nothing is ever deleted.
    """
    by_key: dict[str, dict] = {}
    for p in existing_packets or []:
        k = p.get("key")
        if k:
            by_key[k] = dict(p)

    deltas: list[dict] = []
    seen: set[str] = set()

    candidates = [c for c in (candidate_packets or []) if isinstance(c, dict)]

    # 1) explicit supersede / resolve signals first.
    for c in candidates:
        new_key = _key_of(c)
        for sk in (c.get("supersedes") or []):
            e = by_key.get(sk)
            if e and e.get("status") != "superseded":
                e["status"] = "superseded"
                if new_key:
                    e["superseded_by"] = new_key
                e["last_updated"] = today
                deltas.append(_delta(today, e, "superseded",
                                     reason=c.get("reason") or c.get("supersede_reason"),
                                     source=c.get("source")))
        for rk in (c.get("resolves") or []):
            e = by_key.get(rk)
            if e and e.get("status") != "resolved":
                e["status"] = "resolved"
                e["last_updated"] = today
                deltas.append(_delta(today, e, "resolved",
                                     reason=c.get("reason"), source=c.get("source")))

    # 2) upsert each candidate.
    for c in candidates:
        key = _key_of(c)
        if not key:
            continue
        seen.add(key)
        ctype = (c.get("type") or _type_of_key(key))
        raw_val = c.get("value")
        cval = raw_val if isinstance(raw_val, dict) else {"value": raw_val}
        explicit_status = (cval.get("status") or "").strip().lower()

        if key not in by_key:
            packet = {
                "key": key,
                "type": ctype,
                "subject": c.get("subject") or key.split(":", 1)[-1],
                "value": cval,
                "status": "active",
                "first_seen": today,
                "last_confirmed": today,
                "last_updated": today,
                "source": c.get("source"),
                "confidence": c.get("confidence"),
                "evidence": c.get("evidence"),
                "history": [],
            }
            by_key[key] = packet
            deltas.append(_delta(today, packet, "added", to=_brief(cval),
                                 source=c.get("source")))
            continue

        e = by_key[key]
        old_val = e.get("value")
        meaningful = not _value_equal(_significant(e.get("type"), old_val),
                                      _significant(ctype, cval))
        if not meaningful:
            # No significant change: refresh to the latest value (keeps dates /
            # quotes current) and re-confirm, but do NOT log a noise delta.
            e["value"] = cval
            e["last_confirmed"] = today
            if c.get("source"):
                e["source"] = c.get("source")
            if e.get("status") == "dormant":
                e["status"] = "active"
                deltas.append(_delta(today, e, "reactivated", source=c.get("source")))
        else:
            e.setdefault("history", []).insert(0, {
                "value": old_val,
                "as_of": e.get("last_updated") or e.get("last_confirmed"),
            })
            e["value"] = cval
            e["last_confirmed"] = today
            e["last_updated"] = today
            e["status"] = "active"
            if c.get("source"):
                e["source"] = c.get("source")
            if c.get("confidence") is not None:
                e["confidence"] = c.get("confidence")
            deltas.append(_delta(today, e, "changed", frm=_brief(old_val),
                                 to=_brief(cval), reason=c.get("reason"),
                                 source=c.get("source")))

        # A candidate can also close itself by carrying value.status == resolved.
        if explicit_status in ("resolved", "closed", "completed") and \
                e.get("status") not in ("resolved",):
            e["status"] = "resolved"
            e["last_updated"] = today
            deltas.append(_delta(today, e, "resolved", source=c.get("source")))

    # 3) absence policy for packets not seen this sweep.
    for key, e in by_key.items():
        if key in seen:
            continue
        if e.get("status") in ("superseded", "resolved", "dormant"):
            continue
        if e.get("type") in DURABLE_TYPES:
            e["status"] = "dormant"
            e["last_updated"] = today
            deltas.append(_delta(today, e, "dormant", source="absence"))
        # non-durable absent: leave active, no delta.

    return list(by_key.values()), deltas


# ---------------------------------------------------------------------------
# Clean-read expiry: retire aged carried-forward facts + obsolete hygiene flags
# ---------------------------------------------------------------------------

def _days_between(a: Any, b: Any) -> Optional[int]:
    """Whole days from date-string `a` to date-string `b` (b - a). None if either
    is missing/unparseable (treated as 'unknown', never expired)."""
    try:
        da = _dt.date.fromisoformat(str(a)[:10])
        db = _dt.date.fromisoformat(str(b)[:10])
    except (TypeError, ValueError):
        return None
    return (db - da).days


# A hygiene flag whose text matches any of these is a pre-v2 artifact: it claims a
# Salesforce field is missing, which v2 forbids emitting as a knowledge gap (the
# fact lives in calls / next-steps / other fields). These are retired on any sweep
# with a working Salesforce read, regardless of age, so they stop re-surfacing.
_OBSOLETE_HYGIENE_SUBSTRINGS = (
    "field does not exist", "field not present", "no such field", "invalid field",
    "competitor__c", "pain_identified__c", "metrics_identified__c",
    "champion_identified__c", "economic_buyer_identified__c",
    "primary_competitor__c",
)


def _is_obsolete_hygiene(packet: dict) -> bool:
    if packet.get("type") != "hygiene":
        return False
    v = packet.get("value") or {}
    flag = v.get("flag") if isinstance(v, dict) else v
    text = str(flag or packet.get("subject") or "").lower()
    return any(s in text for s in _OBSOLETE_HYGIENE_SUBSTRINGS)


def expire_stale(packets: list, today: str, *, retire_aged: bool = True,
                 retire_obsolete: bool = True, age_days: int = 45,
                 requirement_unaddressed_age_days: int = 60) -> tuple[list, list]:
    """Retire carried-forward packets that should no longer project as live items.

    Pure. The caller gates each retirement on the quality of THIS sweep's read,
    because the whole point of living memory is to retain facts a sweep didn't
    re-mention — we must only drop them when we genuinely saw the deal clearly:

      * retire_obsolete (gate: Salesforce read succeeded) — retire pre-v2 hygiene
        flags that assert a field is missing. These are categorically wrong under
        v2, so age is irrelevant.
      * retire_aged (gate: a CLEAN live read — Q1 mechanics returned AND Avoma
        account-attendee discovery ran, not a degraded/suspect-dark read) — retire
        DURABLE packets NOT re-confirmed this run (last_confirmed != today) whose
        last_confirmed is older than the threshold: 60d for an unaddressed explicit
        requirement, 45d otherwise.

    A clean read means the agent addressed the standard sections this run, so a
    durable fact it did not re-surface after that long is stale. Retired packets
    stay in the store + history (kept in deltas) but are excluded from the
    projection by `_live`, so they stop appearing as live requirements/flags.
    Returns (packets, deltas). NEVER call with retire_aged=True on a failed or
    degraded read or a hiccup would silently drop durable facts.
    """
    deltas: list[dict] = []
    for e in packets or []:
        if e.get("status") in ("superseded", "resolved", "retired"):
            continue
        retire = False
        reason: Optional[str] = None
        if retire_obsolete and _is_obsolete_hygiene(e):
            retire, reason = True, "obsolete pre-v2 field flag"
        elif (retire_aged and e.get("type") in DURABLE_TYPES
              and e.get("last_confirmed") != today):
            age = _days_between(e.get("last_confirmed") or e.get("first_seen"), today)
            if age is not None:
                v = e.get("value") or {}
                unaddressed_req = (e.get("type") == "requirement"
                                   and not bool(v.get("addressed")))
                threshold = (requirement_unaddressed_age_days
                             if unaddressed_req else age_days)
                if age > threshold:
                    retire = True
                    reason = f"stale {age}d (>{threshold}d), not re-confirmed on a clean read"
        if retire:
            e["status"] = "retired"
            e["last_updated"] = today
            deltas.append(_delta(today, e, "retired", reason=reason))
    return list(packets or []), deltas


def retire_contradicted_hygiene(packets: list, today: str,
                                predicate) -> tuple[list, list]:
    """Retire LIVE hygiene (best-practice flag) packets whose flag text matches
    `predicate(text) -> bool`. Used to drop stale-worldview flags (ghost /
    dark-for-months / future-date / wrong-stage) that contradict the live
    engagement pulse, so they stop projecting as live to-do flags. Retired
    packets stay in the store + history (kept in deltas); they are merely
    excluded from the projection by `_live`. Pure; the caller gates on the pulse
    being live. Returns (packets, deltas)."""
    deltas: list[dict] = []
    for e in packets or []:
        if e.get("status") in ("superseded", "resolved", "retired"):
            continue
        if e.get("type") != "hygiene":
            continue
        v = e.get("value") or {}
        flag = v.get("flag") if isinstance(v, dict) else v
        text = str(flag or e.get("subject") or "")
        if predicate(text):
            e["status"] = "retired"
            e["last_updated"] = today
            deltas.append(_delta(today, e, "retired",
                                 reason="contradicts live engagement pulse"))
    return list(packets or []), deltas


# ---------------------------------------------------------------------------
# Candidate extraction (server-side, from the agent's emitted ai.* / hard)
# ---------------------------------------------------------------------------

def _items(section: Any) -> list:
    if isinstance(section, dict):
        v = section.get("items")
        return v if isinstance(v, list) else []
    return section if isinstance(section, list) else []


def extract_candidates(ai: dict, hard: Optional[dict] = None) -> list[dict]:
    """Turn the agent's emitted `ai.*` (+ a couple of `hard` facts) into candidate
    packets. Each candidate is {type, subject, value, source}. Keys are derived
    from type+subject by reconcile()."""
    ai = ai or {}
    hard = hard or {}
    out: list[dict] = []

    for r in _items(ai.get("explicit_requirements")):
        subj = r.get("requirement")
        if not subj:
            continue
        out.append({"type": "requirement", "subject": subj,
                    "value": {**r, "kind": "explicit"},
                    "source": r.get("said_by") or "ai:explicit_requirements"})

    for r in _items(ai.get("implicit_requirements")):
        subj = r.get("inferred_need")
        if not subj:
            continue
        out.append({"type": "requirement", "subject": subj,
                    "value": {**r, "kind": "implicit"},
                    "source": "ai:implicit_requirements"})

    for s in _items(ai.get("stakeholder_map")):
        subj = s.get("name")
        if not subj:
            continue
        out.append({"type": "stakeholder", "subject": subj, "value": s,
                    "source": "ai:stakeholder_map"})

    cp = ai.get("competitive_position") or {}
    comps = cp.get("competitors") if isinstance(cp, dict) else None
    for comp in (comps if isinstance(comps, list) else []):
        subj = comp.get("name")
        if not subj:
            continue
        out.append({"type": "competitor", "subject": subj, "value": comp,
                    "source": "ai:competitive_position"})

    for v in _items(ai.get("vulnerabilities")):
        subj = v.get("detail") or v.get("category")
        if not subj:
            continue
        out.append({"type": "risk", "subject": subj, "value": v,
                    "source": "ai:vulnerabilities"})

    for d in _items(ai.get("open_deliverables")):
        subj = d.get("commitment")
        if not subj:
            continue
        who = d.get("who") or ""
        out.append({"type": "commitment", "subject": f"{who}:{subj}" if who else subj,
                    "value": d, "source": "ai:open_deliverables"})

    bp = ai.get("best_practice_check") or {}
    flags = bp.get("flags") if isinstance(bp, dict) else None
    for f in (flags if isinstance(flags, list) else []):
        flag = f if isinstance(f, str) else (f.get("flag") if isinstance(f, dict) else None)
        if not flag:
            continue
        out.append({"type": "hygiene", "subject": flag, "value": {"flag": flag},
                    "source": "ai:best_practice_check"})

    ch = ai.get("champion_strength") or {}
    champ = ch.get("champion") if isinstance(ch, dict) else None
    if champ:
        out.append({"type": "champion", "subject": "champion",
                    "value": {"name": champ, "strength": ch.get("strength"),
                              "at_risk": bool(ch.get("at_risk"))},
                    "source": "ai:champion_strength"})

    prods = hard.get("products")
    if prods:
        out.append({"type": "product_scope", "subject": "scope",
                    "value": {"scope": prods},
                    "source": "sf:OpportunityLineItem"})

    return out


def seed_packets(ai: dict, hard: Optional[dict], as_of: str) -> list[dict]:
    """Build a baseline packet store from a record's EXISTING ai.*/hard WITHOUT
    emitting any change deltas.

    This is the one-time migration path for deals swept BEFORE living memory
    existed: their facts already live in `ai.*`, so seeding them is NOT a change.
    We must not flood the "what changed" feed with `added` deltas for facts a rep
    has known about for weeks. We therefore reconcile the derived candidates
    against an EMPTY prior store (every fact becomes an active packet with
    first_seen == as_of) and then DISCARD the resulting `added` deltas.

    Pure / side-effect-free. Returns just the packet list; the caller attaches it
    to the record with an empty delta log. The next REAL sweep reconciles fresh
    evidence against this baseline and logs genuine changes from there on.
    """
    candidates = extract_candidates(ai or {}, hard or {})
    packets, _seed_deltas = reconcile([], candidates, as_of)
    return packets


# ---------------------------------------------------------------------------
# Projection back into ai.*
# ---------------------------------------------------------------------------

def _live(packets: list) -> list:
    return [p for p in (packets or [])
            if p.get("status") in ("active", "dormant")]


# Stakeholders to surface on the dashboard. The packets keep the FULL durable
# roster, but the projected stakeholder_map is capped to the few that matter so
# the UI does not render a long, noisy list. Ranked by role importance, then by
# most-recent contact. Cap is env-tunable (DEAL_STAKEHOLDER_CAP, default 7).
_ROLE_PRIORITY = {
    "economic buyer": 0,
    "decision maker": 1,
    "champion": 2,
    "coach": 3,
    "influencer": 4,
    "detractor": 5,
    "unknown": 6,
}


def _stakeholder_cap() -> int:
    try:
        return max(1, int(os.getenv("DEAL_STAKEHOLDER_CAP", "7")))
    except (TypeError, ValueError):
        return 7


def _rank_stakeholders(items: list) -> list:
    """Most important stakeholders first: role priority (EB > DM > Champion >
    Coach > Influencer > Detractor > Unknown), then most-recent contact. Stable
    two-pass sort: recency desc, then role priority asc."""
    def _role_rank(s: dict) -> int:
        role = str((s or {}).get("role") or "unknown").strip().lower()
        return _ROLE_PRIORITY.get(role, 6)
    by_recency = sorted(
        items, key=lambda s: str((s or {}).get("last_contact_date") or ""), reverse=True)
    return sorted(by_recency, key=_role_rank)


def project_into_ai(agent_ai: dict, packets: list) -> dict:
    """Regenerate the packet-backed `ai.*` item lists from active + dormant
    packets, preserving the agent's derived sections and summaries. The result is
    shape-compatible with what derive_todo / derive_matcha / the frontend read."""
    ai = dict(agent_ai or {})
    expl, impl, stake, comps, vulns, deliv, flags = [], [], [], [], [], [], []
    scope = None
    champ = None

    for p in _live(packets):
        t = p.get("type")
        v = p.get("value") or {}
        subj = p.get("subject")
        lc = p.get("last_confirmed")
        if t == "requirement":
            if (v.get("kind") or "explicit") == "implicit":
                impl.append({"inferred_need": v.get("inferred_need") or subj,
                             "grounding_quote": v.get("grounding_quote") or v.get("quote"),
                             "date": v.get("date") or lc})
            else:
                expl.append({"requirement": v.get("requirement") or subj,
                             "said_by": v.get("said_by"),
                             "date": v.get("date") or lc,
                             "addressed": bool(v.get("addressed")),
                             "quote": v.get("quote")})
        elif t == "pain":
            impl.append({"inferred_need": v.get("inferred_need") or v.get("detail") or subj,
                         "grounding_quote": v.get("grounding_quote") or v.get("quote"),
                         "date": v.get("date") or lc})
        elif t == "stakeholder":
            stake.append({"name": v.get("name") or subj, "title": v.get("title"),
                          "role": v.get("role") or "Unknown",
                          "last_contact_date": v.get("last_contact_date"),
                          "sentiment": v.get("sentiment"), "risk": v.get("risk")})
        elif t == "competitor":
            comps.append({"name": v.get("name") or subj, "sentiment": v.get("sentiment"),
                          "quote": v.get("quote"), "date": v.get("date") or lc})
        elif t == "risk":
            vulns.append({"category": v.get("category") or "other",
                          "detail": v.get("detail") or subj,
                          "first_raised": v.get("first_raised") or p.get("first_seen"),
                          "date": v.get("date") or lc,
                          "status": v.get("status") or p.get("status")})
        elif t == "commitment":
            deliv.append({"who": v.get("who"), "commitment": v.get("commitment") or subj,
                          "date": v.get("date"), "due": v.get("due"),
                          "status": v.get("status") or "open"})
        elif t == "hygiene":
            flags.append(v.get("flag") or subj)
        elif t == "product_scope":
            scope = v.get("scope") if isinstance(v, dict) else v
        elif t == "champion":
            champ = v

    ai["explicit_requirements"] = {"items": expl}
    ai["implicit_requirements"] = {"items": impl}
    ai["stakeholder_map"] = {"items": _rank_stakeholders(stake)[:_stakeholder_cap()]}

    cp = dict(ai.get("competitive_position") or {})
    cp["competitors"] = comps
    ai["competitive_position"] = cp

    ai["vulnerabilities"] = {"items": vulns}
    ai["open_deliverables"] = {"items": deliv}

    bp = dict(ai.get("best_practice_check") or {})
    bp["flags"] = flags
    ai["best_practice_check"] = bp

    if scope is not None:
        ai["product_scope"] = {"scope": scope}

    # Champion is a derived section the agent normally emits each sweep. But the
    # champion identity is durable in our packets, so if a later sweep omits it we
    # backfill from the retained packet rather than letting the dashboard's
    # champion field vanish. We never override a champion the agent DID emit.
    if champ:
        cs = dict(ai.get("champion_strength") or {})
        if not cs.get("champion"):
            cs["champion"] = champ.get("name")
            if cs.get("strength") in (None, ""):
                cs["strength"] = champ.get("strength")
            if "at_risk" not in cs:
                cs["at_risk"] = bool(champ.get("at_risk"))
            ai["champion_strength"] = cs

    return ai


# ---------------------------------------------------------------------------
# Prompt helper: a compact list of known topics for entity resolution.
# ---------------------------------------------------------------------------

_TOPIC_LABELS = [
    ("requirement", "requirements"),
    ("stakeholder", "stakeholders"),
    ("competitor", "competitors"),
    ("risk", "risks"),
    ("commitment", "commitments"),
]


def known_topics_block(packets: list, per_type: int = 12) -> str:
    """A compact, human-readable listing of the deal's known packet subjects so
    the sweep agent reuses the same wording for recurring topics (keeping keys
    stable across daily sweeps). Returns "" when there is nothing to show."""
    live = [p for p in _live(packets)]
    if not live:
        return ""
    lines = []
    for ptype, label in _TOPIC_LABELS:
        subs = []
        for p in live:
            if p.get("type") == ptype and p.get("subject"):
                s = str(p["subject"]).strip()
                if s and s not in subs:
                    subs.append(s)
            if len(subs) >= per_type:
                break
        if subs:
            lines.append(f"- {label}: " + "; ".join(subs))
    if not lines:
        return ""
    return (
        "\n\nKnown insight topics already on record for this deal. When new "
        "evidence concerns one of these, describe it with the SAME wording so it "
        "is recognised as the same item; only use new wording for a genuinely new "
        "topic:\n" + "\n".join(lines)
    )
