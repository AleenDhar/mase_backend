"""ABM Prospecting Engine v11.1 — expectation spec.

Source of truth: `attached_assets/Pasted--ABM-Prospecting-Engine-System-Instructions-v11-1-...txt`
§0.5 Parallelization Map.

Real production tool names (observed in chat_messages, not the briefing's
ideal names):
    - SF: `get_record` (account fetch), `soql` (queries)
    - Web: `web_search`, `web_search_with_urls`
    - RAG: `search_knowledge`
    - Email validation: `validate_batch`, `validate_email` (zerobounce);
      `clearout_find_email` (clearout)
    - Lemlist: `lemlist_get_team`, `lemlist_get_campaign`,
      `lemlist_get_campaign_sequences`, `lemlist_add_lead_to_campaign`,
      `lemlist_get_lead_in_campaign`

The 7 web_search angle check is plugged in as a custom check (ported from
`audit_spike/c7_waterfall_check.py`) so each angle becomes its own
CheckResult with PASS/MISS status.
"""

from __future__ import annotations

import re
from typing import Iterable

from ..checker import (
    CheckResult,
    ExpectedCall,
    FlowSpec,
    ToolMatcher,
    args_contain,
)
from ..loader import ToolCall


# Project ID → flow mapping. Add more here as flows are formalised.
ABM_PROJECT_IDS: tuple[str, ...] = (
    "13a9b0a0-4f8c-4ab1-bca9-b90af966a722",
)


# ---- Phase 1+2: account discovery + SF batch + RAG preloads --------------

P1_P2 = "P1+P2 (Researcher + Mapper)"

EXPECTED_P1_P2 = (
    ExpectedCall(
        id="P2_SF_GET_ACCOUNT",
        description="Salesforce Account fetched (get_record / Account)",
        phase=P1_P2,
        matchers=(
            ToolMatcher(
                tool_names=("get_record",),
                arg_predicate=lambda a: str(a.get("object_api_name") or "").lower() == "account",
            ),
        ),
        min_count=1,
    ),
    ExpectedCall(
        id="P2_SF_SOQL_QUERIES",
        description="SF SOQL queries (contacts / opps / tasks / fields)",
        phase=P1_P2,
        matchers=(ToolMatcher(tool_names=("soql",)),),
        min_count=3,  # contacts + opps + tasks at minimum per §0.5
    ),
    ExpectedCall(
        id="P1_WEB_SEARCH_FIRED",
        description="At least one web_search fired (gateway check; see angle breakdown below)",
        phase=P1_P2,
        matchers=(
            ToolMatcher(tool_names=("web_search", "web_search_with_urls")),
        ),
        min_count=2,  # §1.2 minimum two distinct angles
    ),
    ExpectedCall(
        id="P1_RAG_TITLE_PRIORITIZATION",
        description="RAG preload: Title_Prioritization (used in Phase 2.4 scoring)",
        phase=P1_P2,
        matchers=(
            ToolMatcher(
                tool_names=("search_knowledge",),
                arg_predicate=args_contain("query", "title", "prioritization", "prioritisation"),
            ),
        ),
        min_count=1,
        severity="advisory",  # weak signal; query phrasing varies
    ),
    ExpectedCall(
        id="P1_RAG_CIM",
        description="RAG preload: CIM v2.4 (used in Phase 3.2 routing)",
        phase=P1_P2,
        matchers=(
            ToolMatcher(
                tool_names=("search_knowledge",),
                arg_predicate=args_contain("query", "cim", "routing", "contact intelligence"),
            ),
        ),
        min_count=1,
        severity="advisory",
    ),
)


# ---- Phase 3: validation + enrichment waterfall --------------------------

P3 = "P3 (Strategist)"

EXPECTED_P3 = (
    ExpectedCall(
        id="P3_EMAIL_VALIDATION",
        description="Email validation fired (zerobounce / clearout) for top contacts",
        phase=P3,
        matchers=(
            ToolMatcher(
                tool_names=(
                    "validate_email",
                    "validate_batch",
                    "clearout_find_email",
                ),
            ),
        ),
        min_count=1,  # batch counts as 1; per-contact also acceptable
    ),
    ExpectedCall(
        id="P3_ENRICHMENT_WATERFALL",
        description="Enrichment waterfall touched (ZI / Lusha / Apollo / Seamless / Wiza)",
        phase=P3,
        matchers=(
            ToolMatcher(
                tool_names=(
                    "zi_enrich_contact",
                    "zi_enrich_company",
                    "zi_search_contacts",
                    "lusha_enrich_person",
                    "apollo_enrich_person",
                    "apollo_search_people",
                    "seamless_search_contacts",
                    "wiza_reveal_contact",
                    "wiza_create_list",
                    "wiza_get_list",
                    "wiza_get_list_contacts",
                    "wiza_create_prospect_list",
                ),
            ),
        ),
        min_count=1,
    ),
)


# ---- Phase 4: RAG load × 4 ----------------------------------------------

P4 = "P4 (Writer)"

EXPECTED_P4 = (
    ExpectedCall(
        id="P4_RAG_EMAIL_FRAMEWORK",
        description="RAG: EMAIL_FRAMEWORK loaded",
        phase=P4,
        matchers=(
            ToolMatcher(
                tool_names=("search_knowledge",),
                arg_predicate=args_contain("query", "email_framework", "email framework"),
            ),
        ),
        min_count=1,
    ),
    ExpectedCall(
        id="P4_RAG_EMAIL_INTEL",
        description="RAG: EMAIL_INTEL loaded",
        phase=P4,
        matchers=(
            ToolMatcher(
                tool_names=("search_knowledge",),
                arg_predicate=args_contain("query", "email_intel", "email intel", "intelligence"),
            ),
        ),
        min_count=1,
    ),
    ExpectedCall(
        id="P4_RAG_VALUE_PROPS",
        description="RAG: VALUE_PROPOSITIONS loaded",
        phase=P4,
        matchers=(
            ToolMatcher(
                tool_names=("search_knowledge",),
                arg_predicate=args_contain("query", "value_proposition", "value prop"),
            ),
        ),
        min_count=1,
    ),
    ExpectedCall(
        id="P4_RAG_CASE_STUDIES",
        description="RAG: CASE_STUDIES loaded (proof traceability gate)",
        phase=P4,
        matchers=(
            ToolMatcher(
                tool_names=("search_knowledge",),
                arg_predicate=args_contain("query", "case_studies", "case study", "case studies"),
            ),
        ),
        min_count=1,
    ),
)


# ---- Phase 5: Lemlist push sequence -------------------------------------

P5 = "P5 (Operator)"

EXPECTED_P5 = (
    ExpectedCall(
        id="P5_LEMLIST_GET_TEAM",
        description="lemlist_get_team (pre-push parallel batch)",
        phase=P5,
        matchers=(ToolMatcher(tool_names=("lemlist_get_team",)),),
        min_count=1,
        severity="advisory",  # only required if push happened
    ),
    ExpectedCall(
        id="P5_LEMLIST_GET_CAMPAIGN",
        description="lemlist_get_campaign (pre-push)",
        phase=P5,
        matchers=(ToolMatcher(tool_names=("lemlist_get_campaign",)),),
        min_count=1,
        severity="advisory",
    ),
    ExpectedCall(
        id="P5_LEMLIST_GET_SEQUENCES",
        description="lemlist_get_campaign_sequences (template diff input)",
        phase=P5,
        matchers=(ToolMatcher(tool_names=("lemlist_get_campaign_sequences",)),),
        min_count=1,
        severity="advisory",
    ),
    ExpectedCall(
        id="P5_LEMLIST_ADD_LEAD",
        description="lemlist_add_lead_to_campaign (push sequence)",
        phase=P5,
        matchers=(
            ToolMatcher(tool_names=(
                "lemlist_add_lead_to_campaign",
                "lemlist_add_leads_batch",
            )),
        ),
        min_count=1,
        severity="advisory",
    ),
    ExpectedCall(
        id="P5_LEMLIST_VERIFY_LEAD",
        description="lemlist_get_lead_in_campaign (post-push verification)",
        phase=P5,
        matchers=(ToolMatcher(tool_names=("lemlist_get_lead_in_campaign",)),),
        min_count=1,
        severity="advisory",
    ),
)


# ---- Custom check: 7 web_search angles (ported from C7 spike) ------------

ANGLE_TEMPLATES: dict[str, dict] = {
    "P1_ANGLE_A1_CPO": {
        "role_keywords": ("chief procurement officer", "cpo"),
        "must_contain": (),
        "description": "CPO discovery query",
    },
    "P1_ANGLE_A2_HEAD_OF_PROCUREMENT": {
        "role_keywords": (
            "head of procurement", "vp procurement", "vp of procurement",
            "vice president procurement", "global procurement",
            "director procurement", "director of procurement",
        ),
        "must_contain": (),
        "description": "Head of Procurement query",
    },
    "P1_ANGLE_A3_HEAD_OF_SOURCING": {
        "role_keywords": (
            "head of sourcing", "vp sourcing", "director sourcing",
            "director of sourcing", "global sourcing", "strategic sourcing",
            "category management",
        ),
        "must_contain": (),
        "description": "Head of Sourcing / category leadership query",
    },
    "P1_ANGLE_A4_CFO": {
        "role_keywords": (
            "cfo", "chief financial officer", "finance director",
            "head of finance", "vp finance",
        ),
        "must_contain": (),
        "description": "CFO query",
    },
    "P1_ANGLE_A5_PROCUREMENT_LEADERSHIP": {
        "role_keywords": (
            "procurement leader", "procurement leadership",
            "procurement transformation", "procurement digital",
            "procurement operations", "procurement platform",
            "procurement technology",
        ),
        "must_contain": (),
        "description": "Procurement leadership / transformation query",
    },
    "P1_ANGLE_A6_LINKEDIN_PROCUREMENT": {
        "role_keywords": ("procurement", "sourcing", "category", "supply chain"),
        "must_contain": ("site:linkedin.com/in",),
        "description": "LinkedIn site-search — procurement roster",
    },
    "P1_ANGLE_A7_LINKEDIN_FINANCE": {
        "role_keywords": ("cfo", "finance", "controller", "treasurer"),
        "must_contain": ("site:linkedin.com/in",),
        "description": "LinkedIn site-search — finance roster",
    },
}


_TOKEN_STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "that", "this", "into", "site", "linkedin",
    "company", "procurement", "sourcing", "category", "supply", "chain", "finance",
    "controller", "treasurer", "leader", "leadership", "transformation", "digital",
    "operations", "platform", "technology", "officer", "head", "director", "global",
    "vice", "president", "strategic", "management", "chief", "financial", "renewal",
    "intake", "merlin", "news", "information", "about", "their", "they", "have",
    "https", "http", "www",
})


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower().strip())


def _derive_account_tokens(calls: list[ToolCall], extra: Iterable[str] = ()) -> list[str]:
    tokens: set[str] = set()
    # SF account ids from get_record
    for c in calls:
        if c.tool == "get_record" and str(c.args.get("object_api_name") or "").lower() == "account":
            rid = str(c.args.get("record_id") or "").strip()
            if rid:
                tokens.add(rid.lower())
                tokens.add(rid[:15].lower())
    # Recurring tokens across web_search queries (>=2 occurrences)
    counts: dict[str, int] = {}
    for c in calls:
        if c.tool not in ("web_search", "web_search_with_urls"):
            continue
        q = str(c.args.get("query") or "")
        seen: set[str] = set()
        for raw in re.findall(r"[a-zA-Z0-9&]+", q):
            tok = raw.lower()
            if len(tok) < 4 or tok in _TOKEN_STOPWORDS:
                continue
            seen.add(tok)
        for tok in seen:
            counts[tok] = counts.get(tok, 0) + 1
    for tok, c in counts.items():
        if c >= 2:
            tokens.add(tok)
    for tok in extra:
        if tok:
            tokens.add(tok.lower())
    return sorted(tokens)


def _query_matches_angle(query: str, angle: dict, account_tokens: list[str]) -> bool:
    q = _normalize(query)
    for must in angle.get("must_contain", ()):
        if must.lower() not in q:
            return False
    if not any(t in q for t in account_tokens if t):
        return False
    for kw in angle.get("role_keywords", ()):
        if re.search(rf"(?<![a-z]){re.escape(kw.lower())}(?![a-z])", q):
            return True
    return False


def check_seven_angles(calls: list[ToolCall], context: dict) -> list[CheckResult]:
    """Custom check producing one CheckResult per Discovery Waterfall angle."""
    web_calls = [
        c for c in calls
        if c.tool in ("web_search", "web_search_with_urls")
    ]
    extra = context.get("extra_account_tokens") or []
    tokens = _derive_account_tokens(calls, extra)

    results: list[CheckResult] = []
    matched_any: dict[str, list[ToolCall]] = {}
    for c in web_calls:
        q = str(c.args.get("query") or "")
        for angle_id, angle in ANGLE_TEMPLATES.items():
            if _query_matches_angle(q, angle, tokens):
                matched_any.setdefault(angle_id, []).append(c)

    for angle_id, angle in ANGLE_TEMPLATES.items():
        hits = matched_any.get(angle_id, [])
        results.append(
            CheckResult(
                id=angle_id,
                description=angle["description"],
                phase=P1_P2,
                severity="expected",
                status="pass" if hits else "miss",
                expected_min=1,
                observed=len(hits),
                sample_calls=[
                    {
                        "seq": h.sequence,
                        "tool": h.tool,
                        "tool_call_id": h.tool_call_id,
                        "args_preview": {"query": str(h.args.get("query"))[:240]},
                    }
                    for h in hits[:3]
                ],
            )
        )

    # Diagnostic note about derivation when no tokens (rare but would silently
    # fail every angle otherwise).
    if not tokens and web_calls:
        results.append(
            CheckResult(
                id="P1_ACCOUNT_TOKEN_DERIVATION",
                description="No account tokens derived — every angle will MISS. "
                            "Rerun with explicit --account-token override.",
                phase=P1_P2,
                severity="advisory",
                status="miss",
                expected_min=1,
                observed=0,
            )
        )
    return results


# ---- The flow spec --------------------------------------------------------

ABM_V11_FLOW = FlowSpec(
    name="abm_v11",
    version="11.1",
    project_ids=ABM_PROJECT_IDS,
    expected=(*EXPECTED_P1_P2, *EXPECTED_P3, *EXPECTED_P4, *EXPECTED_P5),
    custom_checks=(check_seven_angles,),
)
