"""deal_engine_validation.py — deterministic, no-LLM anti-fabrication gate for the
Deal Intelligence Engine sweep.

The sweep agent (an LLM) emits one canonical JSON record per opportunity. Left to
its own devices it fabricates: a plausible-but-fake manager name, stakeholders /
champions / requirement authors who exist in no Salesforce Contact Role nor Avoma
call, and template/placeholder leakage ("manager_name", "<opp_id>", "historical
record from prior sweep"). This module makes that structurally impossible at the
single persist chokepoint (analyze_one -> store.upsert_record), with PURE code:

  1. SERVER-OWNED facts — the deal owner's manager is read live from Salesforce
     (Owner.Manager.Name) and OVERRIDES whatever the model emits (`reassert_manager`).
     A fact we hold ground truth for is never taken from the model.
  2. SANITIZE structured people — every named person in a structured field
     (stakeholder name, champion, requirement said_by) must be in the allowlist
     (SF contact roles + task contacts + names already on the prior record) OR the
     item must carry provenance (a non-empty source / quote). A name that is both
     unknown AND unsourced is the fabrication signature and is removed. This NEVER
     strips a legitimate Avoma-discovered buyer (it carries a source) nor durable
     carried-forward memory (that is not in the raw output this function gates).
  3. SCRUB placeholders — high-precision template leakage is blanked.

Everything here is a pure function over plain dicts so it is unit-testable without
an agent, a network, or Salesforce.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Optional

# High-precision template/placeholder leakage. Kept deliberately tight so it never
# touches real deal prose: an angle-bracket token (the field-name placeholders this
# prompt itself uses, e.g. "<opp_id>", "<18-char Id>"), the literal "manager_name"
# token, and the forbidden carried-forward placeholder phrase.
_ANGLE_TOKEN = re.compile(r"<[^<>\n]{1,40}>")
# Square-bracket template slots the model leaks when it fails to resolve a value,
# e.g. "[CFO name]", "[X]", "[European public sector customer]". Kept tight: must
# START WITH A LETTER (so numeric citations like "[1]" are never touched) and must
# NOT be a markdown link "[text](url)" (negative lookahead on "(").
_BRACKET_TOKEN = re.compile(r"\[[A-Za-z][^\[\]\n]{0,39}\](?!\()")
_PLACEHOLDER_SUBSTRINGS = (
    "historical record from prior sweep",
    "manager_name",  # the literal template token, never legitimate prose
)


def _norm_name(s: Any) -> str:
    """Casefolded, whitespace-collapsed name for set membership. '' for non-strings."""
    if not isinstance(s, str):
        return ""
    return re.sub(r"\s+", " ", s).strip().casefold()


def resolve_manager_name(opp: dict) -> Optional[str]:
    """The authoritative deal-owner's-manager name from the live SF snapshot, or
    None when Salesforce has none / was unread. This is the ONLY source of truth
    for hard.manager_name; the model never gets a vote."""
    v = (opp or {}).get("manager_name")
    return v if (v not in (None, "")) else None


def reassert_manager(hard: dict, opp: dict) -> bool:
    """Force hard.manager_name to the authoritative SF value (or None). Returns
    True if it overwrote a different value the model had emitted."""
    if not isinstance(hard, dict):
        return False
    authoritative = resolve_manager_name(opp)
    before = hard.get("manager_name")
    hard["manager_name"] = authoritative
    return _norm_name(before) != _norm_name(authoritative)


def manager_fabricated(hard: dict, opp: dict) -> bool:
    """True iff the MODEL emitted a NON-EMPTY manager_name that contradicts the
    authoritative Salesforce value. Emitting nothing (None/"") is NOT a fabrication
    — the prompt now tells the model to omit manager_name entirely and let the
    server fill it — so only a concrete wrong name counts. This keeps the violation
    counter measuring REAL fabrications, not the normal server fill-in. Call this
    BEFORE reassert_manager (which overwrites the value)."""
    if not isinstance(hard, dict):
        return False
    before = hard.get("manager_name")
    if before in (None, ""):
        return False
    return _norm_name(before) != _norm_name(resolve_manager_name(opp))


def _sourced_names_in_record(rec: Optional[dict]) -> set[str]:
    """Prior-record person names that carried PROVENANCE when they were stored (a
    non-empty source / quote on their item). Only these are grandfathered onto the
    allowlist: a name persisted WITHOUT a source may itself be a pre-gate
    fabrication, so it is NOT trusted on a re-sweep — it must re-earn its place via
    a live SF contact / Avoma attendee / active-user match or a fresh source, or it
    is cleaned out. This is what lets a hardened re-sweep scrub legacy fabrications
    instead of grandfathering them forever, while genuinely-discovered (sourced)
    dormant people still survive."""
    out: set[str] = set()
    ai = (rec or {}).get("ai") or {}
    if isinstance(ai, dict):
        sm = ai.get("stakeholder_map") or {}
        for s in (sm.get("items") or []) if isinstance(sm, dict) else []:
            if isinstance(s, dict) and _has_source(s):
                out.add(_norm_name(s.get("name")))
        cs = ai.get("champion_strength") or {}
        if isinstance(cs, dict) and _has_source(cs):
            out.add(_norm_name(cs.get("champion")))
        er = ai.get("explicit_requirements") or {}
        for r in (er.get("items") or []) if isinstance(er, dict) else []:
            if isinstance(r, dict) and (
                    _has_source(r)
                    or (isinstance(r.get("quote"), str) and r["quote"].strip())):
                out.add(_norm_name(r.get("said_by")))
    for p in (rec or {}).get("packets") or []:
        v = p.get("value") if isinstance(p, dict) else None
        if isinstance(v, dict) and _has_source(v):
            out.add(_norm_name(v.get("name")))
            out.add(_norm_name(v.get("champion")))
    out.discard("")
    return out


def build_people_allowlist(buyer: Optional[dict],
                           existing_record: Optional[dict]) -> set[str]:
    """Names we can vouch for without a per-item source: every Salesforce
    OpportunityContactRole contact, every recent Task contact, and every person on
    the prior canonical record THAT CARRIED A SOURCE when it was stored. A prior
    name with no provenance is deliberately NOT grandfathered, so legacy pre-gate
    fabrications are cleaned on re-sweep rather than surviving forever. Avoma-
    discovered buyers need not be in here (the server can't see Avoma) — that is
    why sanitize_people also accepts any item that carries its own provenance."""
    names: set[str] = set()
    b = buyer or {}
    for c in (b.get("contacts") or []):
        if isinstance(c, dict):
            names.add(_norm_name(c.get("name")))
    for n in (b.get("task_contacts") or []):
        names.add(_norm_name(n))
    names |= _sourced_names_in_record(existing_record)
    names.discard("")
    return names


def _has_source(item: dict) -> bool:
    """True if a structured item carries provenance: a non-empty source / sources
    / quote / trigger. A real Avoma-discovered person always has one (the prompt
    requires it); a fabricated name typically has none."""
    if not isinstance(item, dict):
        return False
    for k in ("source", "sources", "quote", "trigger"):
        v = item.get(k)
        if isinstance(v, str) and v.strip():
            return True
        if isinstance(v, list) and any(isinstance(x, str) and x.strip() for x in v):
            return True
    return False


def sanitize_people(ai: dict, allowlist: set[str]) -> list[str]:
    """Remove fabricated STRUCTURED people from the raw agent `ai` IN PLACE. A name
    is kept iff it is in `allowlist` OR its item carries provenance. Returns a list
    of human-readable violation notes (empty == clean). Operates only on the raw
    sweep output, so durable carried-forward memory is never touched."""
    violations: list[str] = []
    if not isinstance(ai, dict):
        return violations

    sm = ai.get("stakeholder_map")
    if isinstance(sm, dict) and isinstance(sm.get("items"), list):
        kept = []
        for it in sm["items"]:
            if not isinstance(it, dict):
                continue
            nm = _norm_name(it.get("name"))
            if not nm:
                violations.append("removed stakeholder with no name")
                continue
            if nm in allowlist or _has_source(it):
                kept.append(it)
            else:
                violations.append(
                    f"removed unverifiable stakeholder '{it.get('name')}' "
                    "(no Salesforce contact role, task contact, or source)")
        sm["items"] = kept

    cs = ai.get("champion_strength")
    if isinstance(cs, dict):
        nm = _norm_name(cs.get("champion"))
        if nm and not (nm in allowlist or _has_source(cs)):
            violations.append(
                f"cleared unverifiable champion '{cs.get('champion')}' "
                "(no Salesforce contact role, task contact, or source)")
            cs["champion"] = ""
            cs["strength"] = "none"

    er = ai.get("explicit_requirements")
    if isinstance(er, dict) and isinstance(er.get("items"), list):
        for it in er["items"]:
            if not isinstance(it, dict):
                continue
            nm = _norm_name(it.get("said_by"))
            if nm and nm not in allowlist and not _has_source(it):
                violations.append(
                    f"blanked unverifiable requirement author '{it.get('said_by')}'")
                it["said_by"] = ""

    return violations


def _scrub_string(s: str) -> tuple[str, int]:
    """Neutralize template leakage in one string. A string that IS placeholder
    leakage (contains a forbidden substring) is emptied; angle-bracket tokens are
    stripped. Returns (cleaned, scrub_count)."""
    low = s.casefold()
    for sub in _PLACEHOLDER_SUBSTRINGS:
        if sub in low:
            return "", 1
    new = _ANGLE_TOKEN.sub("", s)
    new = _BRACKET_TOKEN.sub("", new)
    if new != s:
        # Collapse the whitespace (and stray space-before-punctuation) left where a
        # token was removed, so "call the [CFO name] before" -> "call the before".
        new = re.sub(r"\s+([,.;:])", r"\1", re.sub(r"\s{2,}", " ", new)).strip()
        return new, 1
    return s, 0


def scrub_placeholders(obj: Any) -> tuple[Any, int]:
    """Recursively scrub template/placeholder leakage from all string VALUES in a
    nested structure (dict keys are left intact). Returns (obj, total_scrubbed)."""
    count = 0
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            nv, c = scrub_placeholders(v)
            obj[k] = nv
            count += c
        return obj, count
    if isinstance(obj, list):
        for i, v in enumerate(obj):
            nv, c = scrub_placeholders(v)
            obj[i] = nv
            count += c
        return obj, count
    if isinstance(obj, str):
        return _scrub_string(obj)
    return obj, count


def scrub_record(parsed: dict) -> int:
    """Scrub placeholder leakage from the agent-authored surfaces (ai + hard) of a
    canonical record IN PLACE. Server-managed packets/deltas are left untouched
    (they were already sanitized when they were minted). Returns the scrub count."""
    total = 0
    if not isinstance(parsed, dict):
        return 0
    for key in ("ai", "hard"):
        sub = parsed.get(key)
        if isinstance(sub, (dict, list)):
            _, c = scrub_placeholders(sub)
            total += c
    return total


# ---------------------------------------------------------------------------
# Part 4 — the MANDATORY deterministic final-record validation gate.
#
# `validate_record` runs AFTER the model returns and BEFORE the record is
# persisted (analyze_one calls it inside a retry loop). It does NOT mutate the
# record; it returns a list of violations. A non-empty list == the record FAILED
# the gate and must be re-run (with the violations fed back) or, once retries are
# exhausted, repaired by `sanitize_failed_record` before a single safe persist.
#
# The gate is deliberately HIGH-PRECISION: it only flags facts the server can
# disprove (a name that contradicts the live Salesforce manager / is in no
# contact role, Avoma attendee list, or active-user roster and carries no source;
# a hard fact that diverges from the authoritative snapshot; a fact value with no
# source; template/placeholder leakage). Its free-text person scan fires ONLY on a
# capitalised name in a person-referencing context (a cue like "escalate to X" or a
# role parenthetical), suppressed by a role/company stopword set — so an ordinary
# capitalised phrase never trips it and the gate cannot gut a legitimate record.
# ---------------------------------------------------------------------------

# The fixed set of structured HARD fact fields the gate governs. The server owns
# these (it reads them live from Salesforce and overrides the model), so each one
# either matches the authoritative snapshot and carries a `<field>_source` naming
# the SF API field, or it is null. A non-null value with an empty source is the
# fabrication signature (Part 4 check 5).
FACT_SOURCE_FIELDS: dict[str, str] = {
    "manager_name": "Owner.Manager.Name",
    "owner_name": "Owner.Name",
    "account_name": "Account.Name",
    "stage": "StageName",
    "amount": "Amount",
    "close_date": "CloseDate",
    "forecast_category": "ForecastCategoryName",
    "competitor": "Competitors__c",
    "products": "Products__c",
    # Deterministic SF free-text fact. Governed + source-stamped so a model-authored
    # next step can't persist unattributed (clean reads already overrode it).
    "next_step": "Next_Step__c",
    "ais_score": "AIS_Score__c",
    "ais_status": "AIS_Status__c",
    "ais_why": "AIS_Why__c",
    # Deterministic SF date facts. Previously the model authored these freely (no
    # SF read), so a hallucinated qualified_date / last_activity_date could skew
    # the Matcha pipeline-health views. They are now server-read and governed.
    "created_date": "CreatedDate",
    "last_modified_date": "LastModifiedDate",
    "last_activity_date": "LastActivityDate",
    "qualified_date": "Qualified_Submission_Date__c",
}

# How a FACT_SOURCE_FIELDS key maps onto the authoritative SF snapshot dict
# (`opp`, the `_map_opps` shape) whose keys differ slightly from the hard block.
_SF_KEY = {
    "manager_name": "manager_name", "owner_name": "owner_name",
    "account_name": "account", "stage": "stage", "amount": "amount",
    "close_date": "close_date", "forecast_category": "forecast_category",
    "competitor": "competitor", "products": "products", "next_step": "next_step",
    "ais_score": "ais_score", "ais_status": "ais_status", "ais_why": "ais_why",
    "created_date": "created_date", "last_modified_date": "last_modified_date",
    "last_activity_date": "last_activity_date", "qualified_date": "qualified_date",
}

# Identity labels are deterministic SF facts that drive team grouping and the book
# UI. Unlike the governed facts they are overridden from the live snapshot ONLY
# WHEN PRESENT and are NEVER blanked — a transient read miss must not erase a
# known owner/account/name. Maps hard-block field -> `_map_opps` snapshot key.
IDENTITY_LABEL_KEYS: dict[str, str] = {
    "owner_name": "owner_name",
    "owner_id": "owner_id",
    "account_name": "account",
    "opp_name": "name",
}

# Every deterministic, Salesforce-sourced hard fact the server owns, EXCLUDING the
# identity labels above and manager_name (which goes through reassert_manager so a
# dormant manager can be carried forward on a degraded read). Maps the hard-block
# field -> the `_map_opps` snapshot key. This is the SINGLE list the AI sweep and
# the AI-free hard refresh both override from, so the two paths cannot drift.
SF_FACT_OPP_KEYS: dict[str, str] = {
    "stage": "stage",
    "forecast_category": "forecast_category",
    "amount": "amount",
    "close_date": "close_date",
    "next_step": "next_step",
    "products": "products",
    "competitor": "competitor",
    "ais_score": "ais_score",
    "ais_status": "ais_status",
    "ais_why": "ais_why",
    "created_date": "created_date",
    "last_modified_date": "last_modified_date",
    "last_activity_date": "last_activity_date",
    "qualified_date": "qualified_date",
}


def _parse_iso_date(v: Any) -> Optional[date]:
    """Parse a 'YYYY-MM-DD' (or longer ISO datetime) string to a date; None on
    anything unparseable so we never fabricate a close date."""
    if isinstance(v, str) and len(v) >= 10:
        try:
            return date.fromisoformat(v[:10])
        except ValueError:
            return None
    return None


def set_days_to_close(hard: dict) -> None:
    """Server-compute `hard.days_to_close` from close_date (never the model's
    value). Cleared to None when close_date is missing/unparseable. Mutates in
    place."""
    if not isinstance(hard, dict):
        return
    d = _parse_iso_date(hard.get("close_date"))
    hard["days_to_close"] = (d - date.today()).days if d is not None else None


def apply_sf_hard_facts(hard: dict, opp: dict, *, authoritative: bool) -> None:
    """Override the server-owned hard facts from the live Salesforce snapshot
    `opp` (the `_map_opps` shape). The ONE place both the AI sweep (analyze_one)
    and the AI-free hard refresh write deterministic SF facts, so the model can
    never author a fact the server holds ground truth for, and the two write
    paths can never drift apart.

    - Identity labels (owner/account/opp name + owner_id): overridden when
      present, NEVER blanked.
    - Governed SF facts (stage, amount, dates, competitor, AIS, next_step, ...):
      when `authoritative` is True (the SF read for THIS opp succeeded),
      Salesforce wins outright — including writing null when SF genuinely has no
      value, which CLEARS a value the model invented for a blank field. When
      False (a degraded/failed read) only non-empty SF values override, so a read
      hiccup never blanks a known fact.

    manager_name is intentionally NOT handled here — callers use reassert_manager
    (which also carries a dormant manager forward on a degraded read). Also
    recomputes `hard.days_to_close` from the (now authoritative) close_date.
    Mutates `hard` in place."""
    if not isinstance(hard, dict) or not isinstance(opp, dict):
        return
    for f, k in IDENTITY_LABEL_KEYS.items():
        v = opp.get(k)
        if v is not None and v != "":
            hard[f] = v
        else:
            hard.setdefault(f, v)
    for f, k in SF_FACT_OPP_KEYS.items():
        v = opp.get(k)
        if v is not None and v != "":
            hard[f] = v
        elif authoritative:
            hard[f] = None
    set_days_to_close(hard)

# A capitalised personal name (2-3 tokens). Deliberately strict so it never fires
# on a sentence-initial capitalised word or a role phrase: "the deal owner's
# manager" has no capitalised name after "manager", so it never matches.
# NOTE: the keyword literals are matched case-insensitively via inline (?i:...)
# groups, but `_PERSON` stays CASE-SENSITIVE — a global re.I would make `[A-Z]`
# match lowercase too and the pattern would fire on role phrases like "the deal
# owner's manager". Keep the name part strictly capitalised.
_PERSON = r"[A-Z][A-Za-z.'\-]+(?:\s+[A-Z][A-Za-z.'\-]+){1,2}"
_MANAGER_SLOT_RES = [
    re.compile(r"(?i:executive\s+connect)\s*(?i:via|through|with|to|by|:)?\s*(" + _PERSON + r")"),
    re.compile(r"(" + _PERSON + r")\s*\(\s*(?i:manager)\s*\)"),
    re.compile(r"(?i:\bmanager)\s*[:\-]\s+(" + _PERSON + r")"),
]
_MANAGER_ROLE = "the deal owner's manager"


def _amount_to_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[^0-9.]", "", str(v))
    try:
        return float(s) if s else None
    except ValueError:
        return None


def _datestr(v: Any) -> Optional[str]:
    return str(v)[:10] if v not in (None, "") else None


def _fact_differs(field: str, hv: Any, sfv: Any) -> bool:
    """True if a hard fact value materially diverges from the authoritative SF
    value (numeric for amount, date-prefix for close_date, normalised text else)."""
    if field == "amount":
        a, b = _amount_to_float(hv), _amount_to_float(sfv)
        return a is not None and b is not None and abs(a - b) > 0.5
    if field == "close_date":
        return _datestr(hv) != _datestr(sfv)
    return _norm_name(hv) != _norm_name(sfv)


def _role_names(contact_roles: Any) -> set[str]:
    out: set[str] = set()
    for c in (contact_roles or []):
        if isinstance(c, dict):
            out.add(_norm_name(c.get("name")))
        else:
            out.add(_norm_name(c))
    out.discard("")
    return out


# --- free-text person detection (Part 4 checks 2/3 across the to-do arrays) ----
# The gate must reject an invented person named ANYWHERE in the actionable text,
# not only in a manager slot. Free-text NER is impossible to do precisely, so the
# scan is deliberately HIGH-PRECISION: a capitalised name is only validated when it
# is introduced by a person-referencing CUE ("escalate to X", "loop in X", "with
# X") or carries a role parenthetical ("X (champion)"). An ordinary capitalised
# phrase that is not a person reference therefore never trips the gate.

# Title-case 2-3 token phrases that look like a personal name but are domain
# role / business phrases (they can legitimately follow a cue, e.g. "align with
# Decision Makers"). Normalised (casefolded). Suppresses false positives.
_NON_PERSON_PHRASES: set[str] = {
    "economic buyer", "economic buyers", "decision maker", "decision makers",
    "decision criteria", "decision process", "paper process", "identify pain",
    "executive sponsor", "executive sponsors", "exec sponsor", "executive connect",
    "executive connection", "deal team", "deal owner", "deal review",
    "account team", "account executive", "procurement lead", "procurement team",
    "finance team", "it team", "security team", "legal team", "buying committee",
    "steering committee", "project team", "evaluation team", "vendor team",
    "leadership team", "senior leadership", "executive leadership", "business unit",
    "subject matter", "power sponsor", "technical buyer", "technical evaluator",
    "user buyer", "product team", "engineering team", "best case", "best practice",
    "best practices", "use case", "use cases", "close plan", "action plan",
    "next step", "next steps", "mutual action", "go live", "proof of concept",
    "statement of work", "master service", "master services", "service agreement",
    "purchase order", "purchase orders", "source to pay", "procure to pay",
    "quarterly business", "business review", "business case", "value case",
    "pain points", "reference call", "reference calls", "customer reference",
    "customer references", "ai hungry", "ai curious", "ai resistant", "shoe fit",
    "info sec", "infosec review", "salesforce opportunity", "salesforce contact",
    "contact role", "contact roles", "north star", "close date",
    "champion strength", "the deal", "our team",
}

# Honorific / title tokens that may prefix a real name ("VP Jane Smith"); stripped
# before allowlist membership so a titled mention of a known contact still matches.
_TITLE_PREFIXES: set[str] = {
    "mr", "mrs", "ms", "miss", "dr", "prof", "sir", "vp", "svp", "evp", "avp",
    "ceo", "cfo", "coo", "cto", "cio", "ciso", "cmo", "cro", "cdo", "cpo",
    "president", "director", "manager", "head", "lead", "chief", "senior",
    "junior", "principal", "global", "regional",
}

# Verbs / prepositions that introduce a PERSON in action prose. A capitalised name
# is only validated when it appears in one of these person-referencing contexts.
_PERSON_CUE = (
    r"(?i:with|to|from|via|by|cc|copy|copying|contact|contacting|email|e-?mail|"
    r"emailing|call|calling|meet|meeting|ask|asking|engage|engaging|involve|"
    r"involving|loop\s+in|loops\s+in|reach\s+out\s+to|reaching\s+out\s+to|"
    r"connect\s+with|connecting\s+with|escalate\s+to|escalating\s+to|introduce|"
    r"introducing|intro\s+to|brief|briefing|update|updating|invite|inviting|"
    r"sync\s+with|align\s+with|aligning\s+with|nudge|chase|offered|owe|told|"
    r"thank|remind|reminding|schedule\s+with|sponsor)"
)
_PERSON_CTX_RE = re.compile(r"\b" + _PERSON_CUE + r"\s+(" + _PERSON + r")")
_ROLE_PAREN = (
    r"(?i:manager|champion|economic\s+buyer|eb|dm|decision\s+maker|sponsor|"
    r"exec(?:utive)?\s+sponsor|coach|influencer|detractor|owner|rep|ae|sdr|"
    r"contact|stakeholder)"
)
_PERSON_PAREN_RE = re.compile(r"(" + _PERSON + r")\s*\(\s*" + _ROLE_PAREN + r"\s*\)")
# Deterministic neutral replacement for an unverifiable free-text person name.
_PERSON_ROLE = "the relevant stakeholder"


def _non_person_entities(sf: Optional[dict]) -> set[str]:
    """Normalised account / competitor / product / owner / manager / category
    strings from the SF snapshot — company & product names a capitalised cue match
    can collide with (e.g. "to Coupa Inc"). Used to suppress those false hits."""
    out: set[str] = set()
    for k in ("account", "competitor", "products", "owner_name", "manager_name",
              "forecast_category", "ais_status"):
        v = (sf or {}).get(k)
        if isinstance(v, str):
            for part in re.split(r"[;,/|]| and ", v):
                n = _norm_name(part)
                if n:
                    out.add(n)
    out.discard("")
    return out


def _person_is_known(cn: str, allow: set[str], entities: set[str]) -> bool:
    """True if a normalised candidate name is vouched for (in the allowlist, a
    known role/business phrase, or a company/product entity). Handles honorific
    prefixes ("vp jane smith" contains the allowlisted "jane smith")."""
    if not cn:
        return True
    if cn in allow or cn in _NON_PERSON_PHRASES or cn in entities:
        return True
    for a in allow:
        if a and a in cn:          # an allowlisted full name under an honorific
            return True
    for e in entities:
        if e and (e in cn or cn in e):   # company / product mention either way
            return True
    toks = cn.split()
    while toks and toks[0] in _TITLE_PREFIXES:
        toks = toks[1:]
    stripped = " ".join(toks)
    if stripped and stripped != cn and (
            stripped in allow or stripped in _NON_PERSON_PHRASES):
        return True
    return False


def _strip_name_punct(name: str) -> str:
    """Trim leading/trailing punctuation/space from a captured name. `_PERSON`
    allows '.' inside (for initials like "J. Smith"), so it over-captures a
    sentence-final period ("Decision Makers."); strip it so the stopword/allowlist
    comparison sees the bare name."""
    return name.strip(" .,;:!?'\"")


def _persons_in_text(s: Any) -> list[str]:
    """Raw person-name strings referenced in one free-text string (cue-introduced
    or role-parenthetical). High precision: never fires on an ordinary capitalised
    phrase that is not in a person-referencing context."""
    if not isinstance(s, str):
        return []
    out: list[str] = []
    for m in _PERSON_CTX_RE.finditer(s):
        out.append(_strip_name_punct(m.group(1)))
    for m in _PERSON_PAREN_RE.finditer(s):
        out.append(_strip_name_punct(m.group(1)))
    return [n for n in out if n]


def _iter_action_text_slots(ai: dict):
    """Yield (container, key, label) for every MODEL-AUTHORED actionable free-text
    slot across ALL to-do arrays (recommended moves, explicit/implicit
    requirements, open deliverables, vulnerabilities, best-practice flags). Verbatim
    Avoma quotes / sources are deliberately EXCLUDED — a name inside a quote is
    evidence, not an assertion. container[key] is both readable and assignable for
    dict items and for the flags list (index), so the same surface drives both the
    validation scan and the last-resort sanitiser."""
    slots: list[tuple[Any, Any, str]] = []
    if not isinstance(ai, dict):
        return slots

    def _add_items(section: str, keys: tuple[str, ...]) -> None:
        sec = ai.get(section)
        for it in (sec.get("items") or []) if isinstance(sec, dict) else []:
            if isinstance(it, dict):
                for k in keys:
                    if isinstance(it.get(k), str) and it[k].strip():
                        slots.append((it, k, section))

    _add_items("recommended_moves", ("action", "trigger", "expected_effect"))
    _add_items("explicit_requirements", ("requirement",))
    _add_items("vulnerabilities", ("detail",))
    # implicit head: the 4-head shape nests two sub-buckets (we_promised /
    # buyer_dependent); the legacy shape is a flat list + a separate open_deliverables.
    _impl = ai.get("implicit_requirements")
    if isinstance(_impl, dict) and ("we_promised" in _impl or "buyer_dependent" in _impl):
        for _side in ("we_promised", "buyer_dependent"):
            _blk = _impl.get(_side)
            for it in (_blk.get("items") or []) if isinstance(_blk, dict) else []:
                if isinstance(it, dict):
                    for k in ("deliverable", "commitment", "inferred_need"):
                        if isinstance(it.get(k), str) and it[k].strip():
                            slots.append((it, k, "implicit_requirements"))
    else:
        _add_items("implicit_requirements", ("inferred_need",))
        _add_items("open_deliverables", ("commitment",))
    bp = ai.get("best_practice_check")
    if isinstance(bp, dict) and isinstance(bp.get("flags"), list):
        for i, f in enumerate(bp["flags"]):
            if isinstance(f, str) and f.strip():
                slots.append((bp["flags"], i, "best_practice_check"))
            elif isinstance(f, dict):
                for k in ("flag", "text", "detail"):
                    if isinstance(f.get(k), str) and f[k].strip():
                        slots.append((f, k, "best_practice_check"))
    return slots


def _move_texts(ai: dict) -> list[str]:
    """Every actionable free-text string across all to-do arrays (the surfaces a
    fabricated name or placeholder can hide in). Thin wrapper over the slot
    iterator so callers that just want the strings stay simple."""
    return [cont[key] for (cont, key, _label) in _iter_action_text_slots(ai)]


def _iter_structured_people(ai: dict) -> list[tuple[str, str, str, bool]]:
    """(location, raw_name, normalised_name, has_source) for every structured
    PERSON slot: stakeholder names, the champion, and explicit-requirement
    authors. raw_name is the display string (for readable feedback); the
    normalised name is used for allowlist membership. has_source is True when the
    item carries provenance (a real Avoma-discovered person always does)."""
    out: list[tuple[str, str, str, bool]] = []
    if not isinstance(ai, dict):
        return out

    def _raw(v: Any) -> str:
        return v.strip() if isinstance(v, str) else ""

    sm = ai.get("stakeholder_map")
    for it in (sm.get("items") or []) if isinstance(sm, dict) else []:
        if isinstance(it, dict):
            out.append(("stakeholder_map", _raw(it.get("name")),
                        _norm_name(it.get("name")), _has_source(it)))
    cs = ai.get("champion_strength")
    if isinstance(cs, dict):
        out.append(("champion_strength", _raw(cs.get("champion")),
                    _norm_name(cs.get("champion")), _has_source(cs)))
    er = ai.get("explicit_requirements")
    for it in (er.get("items") or []) if isinstance(er, dict) else []:
        if isinstance(it, dict):
            sourced = bool(isinstance(it.get("quote"), str) and it["quote"].strip()) or _has_source(it)
            out.append(("explicit_requirements.said_by", _raw(it.get("said_by")),
                        _norm_name(it.get("said_by")), sourced))
    return [(loc, raw, nm, src) for (loc, raw, nm, src) in out if nm]


def _any_placeholder(obj: Any) -> bool:
    """True if any string anywhere in `obj` is template/placeholder leakage
    (a forbidden substring or an angle-bracket token)."""
    if isinstance(obj, str):
        return _scrub_string(obj)[1] > 0
    if isinstance(obj, dict):
        return any(_any_placeholder(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_any_placeholder(v) for v in obj)
    return False


def sanitize_packets(packets: list, allowlist: set[str],
                     sf_facts: Optional[dict] = None) -> tuple[list, int]:
    """Apply the anti-fabrication gate at the PACKET level, BEFORE the durable
    packet store is projected back into ai.*.

    The per-attempt gate (validate_record / sanitize_people / sanitize_failed_record)
    only cleans THIS sweep's raw model output. But living memory MERGES those clean
    candidates with the CARRIED-FORWARD packets read from the store, and a packet
    minted by a PRE-GATE sweep can still hold a fabricated person or a placeholder.
    Without this pass that legacy poison would survive `reconcile`, be reassigned to
    `record["packets"]`, and be re-introduced into ai.* by `project_into_ai` AFTER
    the record already passed validation. This makes the packet store itself
    structurally unable to retain or re-project a fabrication.

    Consistency with the rest of the gate: a person is kept iff its NAME is in
    `allowlist` OR its packet `value` carries provenance (exactly `sanitize_people`).
    A packet whose key-bearing `subject` names an unverifiable person, or that leaks
    a placeholder anywhere, is DROPPED — the subject is the packet's identity key,
    so it cannot be edited in place without corrupting it. An unverifiable
    requirement author (`value.said_by`, which is NOT part of the key) is BLANKED so
    the requirement itself survives. Safe to run on ANY read quality (poison removal
    is never a durable-fact-retention decision). Pure: returns
    (clean_packets, sanitized_count); never raises on shape surprises.
    """
    allow = allowlist or set()
    entities = _non_person_entities(sf_facts)
    kept: list = []
    touched = 0
    for p in packets or []:
        if not isinstance(p, dict):
            kept.append(p)
            continue
        v = p.get("value") if isinstance(p.get("value"), dict) else {}
        subject = p.get("subject") if isinstance(p.get("subject"), str) else ""
        ptype = p.get("type")
        # 1) Placeholder leakage anywhere -> drop (subject is the identity key).
        if _any_placeholder(subject) or _any_placeholder(v):
            touched += 1
            continue
        # 2) Structured-person packets: the name must be vouched for.
        if ptype == "stakeholder":
            if subject and _norm_name(subject) not in allow and not _has_source(v):
                touched += 1
                continue
        if ptype == "champion":
            nm = _norm_name(v.get("name"))
            if nm and nm not in allow and not _has_source(v):
                touched += 1
                continue
        # 3) Requirement author: blank an unverifiable said_by (the requirement
        #    subject is the key, so the ask itself is preserved).
        if ptype == "requirement":
            sb = _norm_name(v.get("said_by"))
            if sb and sb not in allow and not _has_source(v):
                v["said_by"] = ""
                touched += 1
        # 4) A fabricated person named in the key-bearing subject text -> drop.
        if ptype in ("requirement", "risk", "commitment", "hygiene"):
            unknown = [n for n in _persons_in_text(subject)
                       if not _person_is_known(_norm_name(n), allow, entities)]
            if unknown:
                touched += 1
                continue
        kept.append(p)
    return kept, touched


def validate_record(record: dict,
                    sf_facts: Optional[dict] = None,
                    contact_roles: Optional[Any] = None,
                    avoma_attendees: Optional[Any] = None,
                    active_sf_user_names: Optional[set] = None,
                    prior_names: Optional[set] = None) -> list[dict]:
    """The deterministic, no-LLM anti-fabrication gate (Task spec Part 4).

    Runs after the model returns and before persistence. Returns a list of
    violation dicts ({check, field, offending, detail}); an empty list means the
    record PASSES. Never mutates the record; never raises on shape surprises (an
    unexpected shape simply yields no violations for that check).

    Checks:
      1. Manager — any manager-slot name (hard.manager_name or an "Executive
         connect / X (manager)" slot in a move) must equal the live
         Owner.Manager.Name; a placeholder/other name FAILS.
      2/3. People — every structured stakeholder / champion / requirement author
         must be a known Salesforce contact, an echoed Avoma attendee, an active
         SF user, or carry its own inline source; otherwise FAILS.
      4. Hard facts — amount / stage / close_date / competitor / AIS / etc. must
         match the authoritative SF snapshot (when SF has the value).
      5. Source — any governed fact field with a non-null value but an empty
         `<field>_source` FAILS (a value the server cannot attribute to SF).
    """
    violations: list[dict] = []
    if not isinstance(record, dict):
        return violations
    sf = sf_facts or {}
    ai = record.get("ai") if isinstance(record.get("ai"), dict) else {}
    hard = record.get("hard") if isinstance(record.get("hard"), dict) else {}
    sf_manager = _norm_name(sf.get("manager_name"))

    # People we can vouch for, built ONCE and shared by the free-text scan and the
    # structured-people check: SF contact roles + echoed Avoma attendees + active-
    # user roster + the live owner & manager + SOURCED prior-record names. NEVER
    # allowlist the candidate's own names — that would let a fabrication whitelist
    # itself. Company / product strings are kept separately to suppress free-text
    # false positives (a cue match on "to Coupa Inc" is not a person).
    allow: set[str] = set()
    allow |= _role_names(contact_roles)
    allow |= {_norm_name(n) for n in (avoma_attendees or [])}
    allow |= {_norm_name(n) for n in (active_sf_user_names or set())}
    allow |= {_norm_name(n) for n in (prior_names or set())}
    sf_owner = _norm_name(sf.get("owner_name"))
    if sf_manager:
        allow.add(sf_manager)
    if sf_owner:
        allow.add(sf_owner)
    allow.discard("")
    entities = _non_person_entities(sf)

    # ---- check 1: manager slot in hard; placeholder leakage + ANY fabricated
    #      person named across ALL to-do/action arrays (Part 4 checks 2/3) --------
    hm = _norm_name(hard.get("manager_name"))
    if hm and hm != sf_manager:
        violations.append({"check": "manager", "field": "hard.manager_name",
                           "offending": hard.get("manager_name"),
                           "detail": "hard.manager_name does not match the live Owner.Manager.Name"})
    for cont, key, label in _iter_action_text_slots(ai):
        txt = cont[key]
        if not isinstance(txt, str):
            continue
        low = txt.casefold()
        for sub in _PLACEHOLDER_SUBSTRINGS:
            if sub in low:
                violations.append({"check": "placeholder", "field": label,
                                   "offending": sub,
                                   "detail": f"template/placeholder token '{sub}' in a to-do item"})
        m_ang = _ANGLE_TOKEN.search(txt)
        if m_ang:
            violations.append({"check": "placeholder", "field": label,
                               "offending": m_ang.group(0),
                               "detail": "angle-bracket placeholder token in a to-do item"})
        for rx in _MANAGER_SLOT_RES:
            for m in rx.finditer(txt):
                nm = _norm_name(m.group(1))
                if nm and nm != sf_manager:
                    violations.append({"check": "manager", "field": label,
                                       "offending": m.group(1),
                                       "detail": "a manager slot names someone other than the live Owner.Manager.Name"})
        for raw in _persons_in_text(txt):
            cn = _norm_name(raw)
            if cn == sf_manager:
                continue  # the live manager — already governed by the manager rule
            if not _person_is_known(cn, allow, entities):
                violations.append({"check": "person", "field": label, "offending": raw,
                                   "detail": ("a named person in a to-do/action item is in "
                                              "no Salesforce contact role, Avoma attendee "
                                              "list, or active-user roster and is anchored "
                                              "to no source")})

    # ---- checks 2/3: structured people must be verifiable -----------------------
    for loc, raw, nm, sourced in _iter_structured_people(ai):
        if nm not in allow and not sourced:
            violations.append({"check": "person", "field": loc, "offending": raw or nm,
                               "detail": ("named person is in no Salesforce contact role, "
                                          "Avoma attendee list, or active-user roster and "
                                          "carries no source")})

    # ---- check 4: hard fact divergence vs the authoritative SF snapshot ---------
    for f in FACT_SOURCE_FIELDS:
        sfv = sf.get(_SF_KEY[f])
        if sfv in (None, ""):
            continue
        hv = hard.get(f)
        if hv in (None, ""):
            continue
        if _fact_differs(f, hv, sfv):
            violations.append({"check": "hard_fact", "field": f, "offending": hv,
                               "detail": f"hard.{f} {hv!r} diverges from Salesforce {sfv!r}"})

    # ---- check 5: a governed fact value with no source -------------------------
    for f in FACT_SOURCE_FIELDS:
        hv = hard.get(f)
        if hv in (None, "", 0):
            continue
        src = hard.get(f + "_source")
        if not (isinstance(src, str) and src.strip()):
            violations.append({"check": "source", "field": f, "offending": hv,
                               "detail": f"hard.{f} has a value but no {f}_source attributing it to Salesforce"})

    return violations


def stamp_fact_sources(hard: dict, sf_facts: dict) -> None:
    """Server-owned provenance (Part 3): for every governed fact the server can
    attribute to the live SF snapshot, set `<field>_source` to the SF API field
    name; clear it when the value is null or has no SF backing (so check 5 flags
    a model value the server cannot vouch for). Mutates `hard` in place."""
    if not isinstance(hard, dict):
        return
    sf = sf_facts or {}
    for f, api in FACT_SOURCE_FIELDS.items():
        hv = hard.get(f)
        if hv in (None, "", 0):
            hard.pop(f + "_source", None)
            continue
        sfv = sf.get(_SF_KEY[f])
        if sfv not in (None, ""):
            hard[f + "_source"] = api
        else:
            hard.pop(f + "_source", None)


def format_validation_feedback(violations: list[dict]) -> str:
    """Render gate violations as a corrective instruction appended to the agent's
    next attempt (Part 4: re-run with the violations fed back)."""
    if not violations:
        return ""
    lines = []
    for v in violations[:25]:
        lines.append(f"- [{v.get('check')}] {v.get('field')}: {v.get('detail')} "
                     f"(offending: {v.get('offending')!r})")
    return (
        "\n\n--- YOUR PREVIOUS OUTPUT FAILED THE ANTI-FABRICATION GATE ---\n"
        "The following facts could NOT be traced to Salesforce or to a quoted "
        "Avoma span, so they were rejected and NOT saved. Re-emit the FULL "
        "canonical record JSON, and for EACH item below either (a) use the real "
        "Salesforce value, (b) replace the invented name with a ROLE (e.g. 'the "
        "deal owner's manager', 'the economic buyer'), or (c) drop it and record "
        "the gap in evidence_coverage.gaps. Do NOT invent names, amounts, dates, "
        "stages, or competitors.\nViolations:\n" + "\n".join(lines))


def _sanitize_free_text(s: Any, sf_manager_norm: str,
                        allow: Optional[set] = None,
                        entities: Optional[set] = None) -> tuple[Any, int]:
    """Neutralise fabrication in ONE free-text ASSERTION string: strip placeholder
    tokens, replace any manager-slot name that is not the live manager with the
    manager role, and replace any other unverifiable named person (one in no
    allowlist / not a company-or-product entity) with a neutral role. Returns
    (cleaned, fix_count). Callers must NOT pass verbatim evidence quotes — a name in
    a quote is evidence, not an assertion."""
    if not isinstance(s, str):
        return s, 0
    allow = allow or set()
    entities = entities or set()
    fixes = 0
    out = s
    for sub in _PLACEHOLDER_SUBSTRINGS:
        if sub in out.casefold():
            repl = _MANAGER_ROLE if sub == "manager_name" else ""
            out = re.sub(re.escape(sub), repl, out, flags=re.I)
            fixes += 1
    if _ANGLE_TOKEN.search(out):
        out = _ANGLE_TOKEN.sub("", out)
        fixes += 1
    for rx in _MANAGER_SLOT_RES:
        def _rm(m):
            nonlocal fixes
            if _norm_name(m.group(1)) != sf_manager_norm:
                fixes += 1
                return m.group(0).replace(m.group(1), _MANAGER_ROLE)
            return m.group(0)
        out = rx.sub(_rm, out)

    def _rp(m):
        nonlocal fixes
        nm = _strip_name_punct(m.group(1))
        cn = _norm_name(nm)
        if not cn or cn == sf_manager_norm or _person_is_known(cn, allow, entities):
            return m.group(0)
        fixes += 1
        return m.group(0).replace(nm, _PERSON_ROLE)
    out = _PERSON_CTX_RE.sub(_rp, out)
    out = _PERSON_PAREN_RE.sub(_rp, out)
    return re.sub(r"\s{2,}", " ", out).strip(), fixes


def _sanitize_action_texts(ai: dict, sf_manager_norm: str,
                           allow: Optional[set] = None,
                           entities: Optional[set] = None) -> int:
    """Repair free-text to-do strings in place across ALL action arrays: strip
    placeholder tokens, replace any manager-slot name that is not the live manager
    with the manager role, and replace any other unverifiable named person (one in
    no allowlist / not a company-or-product entity) with a neutral role. After this
    the free-text scan in validate_record finds nothing left to reject."""
    allow = allow or set()
    entities = entities or set()
    fixes = 0
    for cont, key, _label in _iter_action_text_slots(ai):
        cont[key], c = _sanitize_free_text(cont[key], sf_manager_norm, allow, entities)
        fixes += c
    return fixes


def sanitize_meddpicc(ai: dict, allowlist: Optional[set] = None,
                      sf_facts: Optional[dict] = None) -> int:
    """Apply the free-text anti-fabrication pass to `ai.meddpicc` narratives.

    MEDDPICC is the one free-text ai surface NOT covered by validate_record /
    sanitize_people / _sanitize_action_texts, AND _normalize_meddpicc carries a
    prior element's narrative forward when this sweep emits an empty one — so a
    fabricated person or placeholder minted by a PRE-GATE sweep could otherwise ride
    a carried-forward element straight into the persisted record untouched. This
    neutralises any manager-slot / unverifiable person in each element narrative to a
    role and strips placeholder leakage, mirroring _sanitize_action_texts, so
    MEDDPICC is structurally as clean as every other surface. The per-element
    `sources` list holds verbatim evidence spans (a name there is evidence, not an
    assertion), so it is only placeholder-scrubbed, never person-neutralised. Call
    AFTER _normalize_meddpicc so it covers BOTH this sweep's output and the carried-
    forward prior. Mutates `ai` in place; returns the fix count."""
    if not isinstance(ai, dict):
        return 0
    md = ai.get("meddpicc")
    if not isinstance(md, dict):
        return 0
    sf = sf_facts or {}
    sf_manager_norm = _norm_name(sf.get("manager_name"))
    entities = _non_person_entities(sf)
    allow = set(allowlist or set())
    if sf_manager_norm:
        allow.add(sf_manager_norm)
    _owner = _norm_name(sf.get("owner_name"))
    if _owner:
        allow.add(_owner)
    allow.discard("")
    fixes = 0
    for _el, elt in list(md.items()):
        if not isinstance(elt, dict):
            continue
        narr = elt.get("narrative")
        if isinstance(narr, str) and narr.strip():
            new_narr, c = _sanitize_free_text(narr, sf_manager_norm, allow, entities)
            if c:
                elt["narrative"] = new_narr
                fixes += c
                if not new_narr:        # a narrative that WAS only leakage -> honest gap
                    elt["status"] = "gap"
        srcs = elt.get("sources")
        if isinstance(srcs, list):
            _, c = scrub_placeholders(srcs)   # in place; names in quotes are evidence
            fixes += c
    return fixes


def sanitize_failed_record(record: dict, violations: list[dict],
                           sf_facts: dict,
                           allowlist: Optional[set] = None) -> int:
    """Last-resort deterministic repair when the agent exhausts its retries
    (Part 4): force every gate-failing fact to the Salesforce value, a role, or
    null; drop unverifiable structured people; strip placeholder leakage; and
    record each violation in evidence_coverage.gaps. After this runs the record
    is SAFE — validate_record returns clean. Returns the number of fixes applied.
    NEVER drops the record: an honest, scrubbed record is always persisted."""
    if not isinstance(record, dict):
        return 0
    sf = sf_facts or {}
    ai = record.get("ai")
    if not isinstance(ai, dict):
        ai = record["ai"] = {}
    hard = record.setdefault("hard", {})
    if not isinstance(hard, dict):
        hard = record["hard"] = {}
    sf_manager_norm = _norm_name(sf.get("manager_name"))
    fixes = 0

    # 1. manager + every governed hard fact -> SF ground truth (or null).
    if reassert_manager(hard, sf):
        fixes += 1
    for f in FACT_SOURCE_FIELDS:
        sfv = sf.get(_SF_KEY[f])
        hv = hard.get(f)
        if sfv not in (None, ""):
            if hv not in (None, "") and _fact_differs(f, hv, sfv):
                hard[f] = sfv
                fixes += 1
        elif hv not in (None, "", 0):
            hard[f] = None
            fixes += 1
    stamp_fact_sources(hard, sf)

    # 2. free-text to-do strings across ALL action arrays: placeholders, wrong
    #    manager slots, and any other unverifiable named person -> a neutral role.
    _entities = _non_person_entities(sf)
    _allow = set(allowlist or set())
    if sf_manager_norm:
        _allow.add(sf_manager_norm)
    _owner = _norm_name(sf.get("owner_name"))
    if _owner:
        _allow.add(_owner)
    _allow.discard("")
    fixes += _sanitize_action_texts(ai, sf_manager_norm, _allow, _entities)

    # 3. structured people: drop anyone not in the allowlist and not sourced.
    fixes += len(sanitize_people(ai, allowlist or set()))

    # 4. belt-and-braces placeholder scrub of the agent-authored surfaces.
    _, c1 = scrub_placeholders(ai)
    _, c2 = scrub_placeholders(hard)
    fixes += c1 + c2

    # 5. honest read: log every violation as a gap.
    ec = record.setdefault("evidence_coverage", {})
    if isinstance(ec, dict):
        g = ec.setdefault("gaps", [])
        if isinstance(g, list):
            for v in (violations or []):
                g.append(f"validation gate: {v.get('detail')} (offending: {v.get('offending')!r})")
    return fixes
