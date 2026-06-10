"""C7 Waterfall Completeness check — v11.6 audit-layer spike.

Read-only diagnostic against the existing `chat_messages` Supabase log.
Given a chat_id (one Mapper run), determine which of the 7 required
Discovery Waterfall angles fired.

Usage:
    python -m audit_spike.c7_waterfall_check <chat_id> [<chat_id> ...]
    python -m audit_spike.c7_waterfall_check 87e36146-6e15-45f3-b10f-8b7b08cb21f5

Angle matching: each web_search query must contain
    (a) at least one account-context token (proves the query is about THIS run's
        target account, not an unrelated one), AND
    (b) at least one role token from the angle's role-keyword set.

The two LinkedIn site-search angles additionally require the literal
`site:linkedin.com/in` operator.

Account-context tokens are auto-derived per chat from:
    - SF Account record_ids seen in `get_record` calls (object_api_name='Account')
    - Tokens (>=4 chars) that recur across >=2 web_search queries — typically
      the company name the Mapper is investigating.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Iterable

from supabase import create_client


# --- The 7 Discovery Waterfall angles, as codified rules -------------------
#
# Each angle is a set of role keywords. A web_search query satisfies the
# angle if it contains at least one of those role keywords (case-insensitive,
# whole-word). The two LinkedIn angles additionally require the
# `site:linkedin.com/in` operator.
#
# Sources: v11.5 Mapper system instructions (Discovery Waterfall section)
# and the v11.6 briefing § "What the safety net catches — Example 1".

ANGLE_TEMPLATES: dict[str, dict] = {
    "A1_CPO": {
        "role_keywords": [
            "chief procurement officer",
            "cpo",
        ],
        "must_contain": [],
        "description": "CPO query (Bracket A — top of Senior Floor)",
    },
    "A2_Head_of_Procurement": {
        "role_keywords": [
            "head of procurement",
            "vp procurement",
            "vp of procurement",
            "vice president procurement",
            "global procurement",
            "director procurement",
            "director of procurement",
        ],
        "must_contain": [],
        "description": "Head of Procurement query (Bracket A/B)",
    },
    "A3_Head_of_Sourcing": {
        "role_keywords": [
            "head of sourcing",
            "vp sourcing",
            "director sourcing",
            "director of sourcing",
            "global sourcing",
            "strategic sourcing",
            "category management",
        ],
        "must_contain": [],
        "description": "Head of Sourcing / category leadership query",
    },
    "A4_CFO": {
        "role_keywords": [
            "cfo",
            "chief financial officer",
            "finance director",
            "head of finance",
            "vp finance",
        ],
        "must_contain": [],
        "description": "CFO query (Bracket B finance angle)",
    },
    "A5_Procurement_Leadership": {
        "role_keywords": [
            "procurement leader",
            "procurement leadership",
            "procurement transformation",
            "procurement digital",
            "procurement operations",
            "procurement platform",
            "procurement technology",
        ],
        "must_contain": [],
        "description": "Procurement leadership / transformation query",
    },
    "A6_LinkedIn_Procurement": {
        "role_keywords": [
            "procurement",
            "sourcing",
            "category",
            "supply chain",
        ],
        "must_contain": ["site:linkedin.com/in"],
        "description": "LinkedIn site-search — procurement roster",
    },
    "A7_LinkedIn_Finance": {
        "role_keywords": [
            "cfo",
            "finance",
            "controller",
            "treasurer",
        ],
        "must_contain": ["site:linkedin.com/in"],
        "description": "LinkedIn site-search — finance roster",
    },
}

WEB_SEARCH_TOOL_NAMES = {"web_search", "web_search_with_urls"}


# --- Data shapes -----------------------------------------------------------

@dataclass
class WebSearchCall:
    seq: int
    tool: str
    query: str
    raw_args: dict


@dataclass
class C7Verdict:
    chat_id: str
    web_search_calls: list[WebSearchCall]
    account_tokens: list[str] = field(default_factory=list)
    matched: dict[str, list[str]] = field(default_factory=dict)  # angle -> queries
    missed: list[str] = field(default_factory=list)
    unmatched_queries: list[WebSearchCall] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return len(self.missed) == 0

    @property
    def coverage_ratio(self) -> str:
        return f"{len(self.matched)}/{len(ANGLE_TEMPLATES)}"


# --- Pure functions: matching logic ---------------------------------------

def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower().strip())


def _query_has_account_context(query: str, account_tokens: Iterable[str]) -> bool:
    q = _normalize(query)
    for tok in account_tokens:
        t = tok.lower().strip()
        if t and t in q:
            return True
    return False


def _query_matches_angle(query: str, angle: dict, account_tokens: Iterable[str]) -> bool:
    q = _normalize(query)
    for must in angle.get("must_contain", []):
        if must.lower() not in q:
            return False
    if not _query_has_account_context(query, account_tokens):
        return False
    role_keywords = angle.get("role_keywords", [])
    if not role_keywords:
        return False
    for kw in role_keywords:
        # whole-word-ish match (allow surrounding punctuation/quotes)
        if re.search(rf"(?<![a-z]){re.escape(kw.lower())}(?![a-z])", q):
            return True
    return False


# Stop-words excluded when auto-deriving account tokens from recurring query terms.
_TOKEN_STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "that", "this", "into", "site", "linkedin",
    "company", "procurement", "sourcing", "category", "supply", "chain", "finance",
    "controller", "treasurer", "leader", "leadership", "transformation", "digital",
    "operations", "platform", "technology", "officer", "head", "director", "global",
    "vice", "president", "strategic", "management", "chief", "financial", "renewal",
    "intake", "merlin", "news", "information", "about", "their", "they", "have",
    "https", "http", "www",
})


def derive_account_tokens(
    web_search_calls: list[WebSearchCall],
    sf_account_ids: list[str],
    extra: list[str] | None = None,
) -> list[str]:
    """Pure: derive likely account-identifier tokens for this chat.

    - SF Account record_ids (full + 15-char prefix)
    - Tokens (alphanumeric, >=4 chars, not stop-words) that appear in >=2
      distinct web_search queries
    - Any caller-supplied `extra` tokens (CLI override)
    """
    tokens: set[str] = set()
    for aid in sf_account_ids:
        if aid:
            tokens.add(aid)
            tokens.add(aid[:15])
    counts: dict[str, int] = {}
    for call in web_search_calls:
        seen: set[str] = set()
        for raw in re.findall(r"[a-zA-Z0-9&]+", call.query):
            tok = raw.lower()
            if len(tok) < 4 or tok in _TOKEN_STOPWORDS:
                continue
            seen.add(tok)
        for tok in seen:
            counts[tok] = counts.get(tok, 0) + 1
    for tok, c in counts.items():
        if c >= 2:
            tokens.add(tok)
    for tok in extra or []:
        if tok:
            tokens.add(tok.lower())
    return sorted(tokens)


def evaluate_c7(
    calls: list[WebSearchCall],
    account_tokens: list[str],
) -> tuple[dict[str, list[str]], list[WebSearchCall]]:
    """Pure: match each web_search call against angle templates.

    Returns (matched_by_angle, unmatched_calls).
    """
    matched: dict[str, list[str]] = {}
    unmatched: list[WebSearchCall] = []
    for call in calls:
        any_match = False
        for angle_id, angle in ANGLE_TEMPLATES.items():
            if _query_matches_angle(call.query, angle, account_tokens):
                matched.setdefault(angle_id, []).append(call.query)
                any_match = True
        if not any_match and call.query.strip():
            unmatched.append(call)
    return matched, unmatched


# --- Supabase loader -------------------------------------------------------

def _fetch_tool_calls(chat_id: str) -> list[dict]:
    """Page through chat_messages tool_call rows for a chat (Supabase row cap is 1000)."""
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    page = 1000
    out: list[dict] = []
    offset = 0
    while True:
        res = (
            sb.table("chat_messages")
            .select("sequence, metadata, created_at")
            .eq("chat_id", chat_id)
            .eq("type", "tool_call")
            .order("sequence")
            .order("created_at")
            .range(offset, offset + page - 1)
            .execute()
        )
        rows = res.data or []
        out.extend(rows)
        if len(rows) < page:
            break
        offset += page
    return out


def load_chat_artifacts(chat_id: str) -> tuple[list[WebSearchCall], list[str]]:
    """Returns (deduped web_search calls, SF Account record_ids seen in this chat)."""
    rows = _fetch_tool_calls(chat_id)
    seen: set[tuple[str, str]] = set()
    web_calls: list[WebSearchCall] = []
    account_ids: list[str] = []
    for row in rows:
        md = row.get("metadata")
        if isinstance(md, str):
            try:
                md = json.loads(md)
            except json.JSONDecodeError:
                md = {}
        md = md or {}
        tool = md.get("tool", "")
        args = md.get("args") or {}
        if not isinstance(args, dict):
            args = {}

        # Capture SF Account record_ids for account-context derivation
        if tool == "get_record" and args.get("object_api_name") == "Account":
            rid = str(args.get("record_id", "")).strip()
            if rid and rid not in account_ids:
                account_ids.append(rid)

        if tool not in WEB_SEARCH_TOOL_NAMES:
            continue
        query = str(args.get("query", "")).strip()
        # Dedupe: server.py writes tool_call rows from THREE paths
        # (1115 tool_wrapper, 1647 sync handler, 1991 stream handler).
        # All three log the same call; collapse by (tool, query).
        key = (tool, query)
        if key in seen:
            continue
        seen.add(key)
        web_calls.append(WebSearchCall(
            seq=row.get("sequence") or 0,
            tool=tool,
            query=query,
            raw_args=args,
        ))
    return web_calls, account_ids


# --- Verdict orchestration -------------------------------------------------

def run_c7(chat_id: str, extra_account_tokens: list[str] | None = None) -> C7Verdict:
    calls, account_ids = load_chat_artifacts(chat_id)
    account_tokens = derive_account_tokens(calls, account_ids, extra_account_tokens)
    matched, unmatched = evaluate_c7(calls, account_tokens)
    missed = [a for a in ANGLE_TEMPLATES if a not in matched]
    return C7Verdict(
        chat_id=chat_id,
        web_search_calls=calls,
        account_tokens=account_tokens,
        matched=matched,
        missed=missed,
        unmatched_queries=unmatched,
    )


# --- Pretty-printer --------------------------------------------------------

def render(verdict: C7Verdict) -> str:
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append(f"C7 — Waterfall Completeness Check")
    lines.append(f"chat_id: {verdict.chat_id}")
    lines.append("=" * 72)

    lines.append(f"\nAccount-context tokens (auto-derived): {verdict.account_tokens}")
    lines.append(f"\nweb_search calls captured: {len(verdict.web_search_calls)}")
    for c in verdict.web_search_calls:
        lines.append(f"  seq={c.seq:3d}  [{c.tool}]  {c.query!r}")

    lines.append(f"\nAngle coverage: {verdict.coverage_ratio}")
    for angle_id, angle in ANGLE_TEMPLATES.items():
        if angle_id in verdict.matched:
            queries = verdict.matched[angle_id]
            lines.append(f"  [PASS] {angle_id:30s}  {angle['description']}")
            for q in queries:
                lines.append(f"           matched by: {q!r}")
        else:
            lines.append(f"  [MISS] {angle_id:30s}  {angle['description']}")

    if verdict.unmatched_queries:
        lines.append(f"\nQueries that fired but matched no angle: {len(verdict.unmatched_queries)}")
        for c in verdict.unmatched_queries:
            lines.append(f"  seq={c.seq:3d}  {c.query!r}")

    lines.append("\n" + "-" * 72)
    if verdict.passed:
        lines.append("VERDICT: CLEAN — all 7 waterfall angles fired.")
    else:
        lines.append(f"VERDICT: DIRTY — {len(verdict.missed)}/{len(ANGLE_TEMPLATES)} angles missed.")
        lines.append(f"Missing angles: {', '.join(verdict.missed)}")
        lines.append("Remediation (production v11.6): call web_search with each missed-angle template,")
        lines.append("merge results into universe, re-rank, re-audit.")
    lines.append("-" * 72)
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(
            "usage: python -m audit_spike.c7_waterfall_check <chat_id> [<chat_id> ...] "
            "[--account-token TOKEN ...]",
            file=sys.stderr,
        )
        return 2
    chat_ids: list[str] = []
    extra_tokens: list[str] = []
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--account-token" and i + 1 < len(argv):
            extra_tokens.append(argv[i + 1])
            i += 2
        else:
            chat_ids.append(a)
            i += 1
    exit_code = 0
    for chat_id in chat_ids:
        verdict = run_c7(chat_id, extra_tokens)
        print(render(verdict))
        print()
        if not verdict.passed:
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
