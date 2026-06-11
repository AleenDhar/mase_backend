"""Read-only natural-language Q&A over the pre-computed cache + report tables.

This module powers the "Ask anything" dummy UI (GET /ask, POST /api/ask).

Design / safety:
- A small LLM (gpt-4o-mini by default) answers free-form questions using ONLY
  the read-only tools defined here.
- Every tool is hard-scoped to an allowlist of tables — the model never supplies
  a table name, so it cannot pivot to live Salesforce/Avoma or any other table:
      opportunity_cache, meeting_cache, field_history_cache,
      avoma_event_reports (AI analysis), opportunity_observatory (dossiers)
- No write path exists anywhere in this module.
- If the tools return nothing, the model is instructed to say the answer is not
  in the cached data rather than invent it.

The Supabase client (sync) is passed in and every call runs in a thread executor.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Optional

_QA_MODEL = os.getenv("CACHE_QA_MODEL", "gpt-4o-mini")
_MAX_ITERS = int(os.getenv("CACHE_QA_MAX_ITERS", "6") or "6")

_SYSTEM_PROMPT = """You are a read-only sales-intelligence assistant for a CEO.

You answer questions using ONLY the tools provided, which read from a set of
pre-computed cache and report tables (Salesforce opportunities, meetings, field
history, per-meeting AI analysis, and long-form deal dossiers).

Hard rules:
- NEVER invent numbers, names, stages, dates, or facts. Every concrete claim must
  come from a tool result.
- If the tools return nothing relevant, say plainly that the information is not in
  the cached data — do not guess.
- You cannot access live Salesforce or Avoma; only the cached/reported data.
- Prefer calling a tool over answering from memory. Use multiple tools if helpful.
- Be concise and direct. Use short paragraphs or bullets. Include the concrete
  figures you found (amount, stage, health score, momentum, days in stage, dates).
- When a deal is involved, mention the opportunity name and account.
"""


def _short(obj, max_chars: int = 6000) -> str:
    """Serialise a tool result to a compact JSON string, truncated for the LLM."""
    try:
        s = json.dumps(obj, default=str, ensure_ascii=False)
    except Exception:
        s = str(obj)
    if len(s) > max_chars:
        s = s[:max_chars] + f"... [truncated {len(s) - max_chars} chars]"
    return s


# ---------------------------------------------------------------------------
# Reusable read paths (single source of truth).
#
# These pure, sync functions are the ONE place the opportunity-cache /
# meeting-cache / field-history-cache read SQL lives. Both the cache_qa LLM
# tools (below) and the first-class MCP wrappers in server.py delegate to them,
# so there is no duplicated query logic. Each is hard-scoped to a single table
# (the table name is never taken from the caller) and is strictly read-only.
#
# Pagination: callers pass limit + offset; we fetch limit+1 rows via .range()
# and return has_more / next_offset so an external client can page reliably.
# ---------------------------------------------------------------------------

_OPP_COLS = (
    "opportunity_id,opportunity_name,account_name,amount,stage_name,close_date,"
    "probability,owner_name,is_closed,meetings_count,last_meeting_date,"
    "days_since_last_meeting,health_score,momentum,days_in_stage,risk_signals"
)

_OPP_ORDER_FIELDS = {
    "amount", "health_score", "days_in_stage", "probability",
    "days_since_last_meeting",
}


def _paged(rows, lim: int, off: int, key: str) -> dict:
    """Shape a limit+1 fetch into a paginated read result."""
    rows = rows or []
    has_more = len(rows) > lim
    rows = rows[:lim]
    return {"count": len(rows), key: rows, "has_more": has_more,
            "next_offset": (off + lim) if has_more else None}


def read_filter_opportunities(supabase, *, momentum: Optional[str] = None,
                              stage: Optional[str] = None,
                              min_amount: Optional[float] = None,
                              max_amount: Optional[float] = None,
                              max_meetings: Optional[int] = None,
                              is_closed: Optional[bool] = None,
                              order_by: str = "amount", descending: bool = True,
                              limit: int = 25, offset: int = 0) -> dict:
    """Filter opportunity_cache by structured criteria (read-only)."""
    lim = max(1, min(int(limit or 25), 100))
    off = max(0, int(offset or 0))
    ob = order_by if order_by in _OPP_ORDER_FIELDS else "amount"
    qy = supabase.table("opportunity_cache").select(_OPP_COLS)
    if momentum:
        qy = qy.eq("momentum", momentum)
    if stage:
        qy = qy.ilike("stage_name", f"%{stage}%")
    if min_amount is not None:
        qy = qy.gte("amount", min_amount)
    if max_amount is not None:
        qy = qy.lte("amount", max_amount)
    if max_meetings is not None:
        qy = qy.lte("meetings_count", max_meetings)
    if is_closed is not None:
        qy = qy.eq("is_closed", is_closed)
    res = qy.order(ob, desc=bool(descending)).range(off, off + lim).execute()
    return _paged(res.data, lim, off, "opportunities")


def read_search_opportunities(supabase, query: str, *, limit: int = 10,
                              offset: int = 0) -> dict:
    """Substring search opportunity_cache by name or account (read-only)."""
    q = (query or "").strip().strip("?.,!")
    lim = max(1, min(int(limit or 10), 50))
    off = max(0, int(offset or 0))
    res = (supabase.table("opportunity_cache").select(_OPP_COLS)
           .or_(f"opportunity_name.ilike.%{q}%,account_name.ilike.%{q}%")
           .order("amount", desc=True).range(off, off + lim).execute())
    return _paged(res.data, lim, off, "opportunities")


def read_opportunity_meetings(supabase, opportunity_id: str, *,
                              limit: int = 20, offset: int = 0) -> dict:
    """Meetings linked to an opportunity, newest first (read-only)."""
    oid = (opportunity_id or "").strip()
    lim = max(1, min(int(limit or 20), 100))
    off = max(0, int(offset or 0))
    res = (supabase.table("meeting_cache")
           .select("meeting_uuid,meeting_title,meeting_date,transcript_summary")
           .contains("opportunity_ids", [oid])
           .order("meeting_date", desc=True).range(off, off + lim).execute())
    return _paged(res.data, lim, off, "meetings")


def read_field_history(supabase, opportunity_id: str, *,
                       field_name: Optional[str] = None,
                       limit: int = 30, offset: int = 0) -> dict:
    """Field-change history for an opportunity, newest first (read-only)."""
    oid = (opportunity_id or "").strip()
    lim = max(1, min(int(limit or 30), 100))
    off = max(0, int(offset or 0))
    qy = supabase.table("field_history_cache").select("*").eq("opportunity_id", oid)
    if field_name:
        qy = qy.eq("field_name", field_name)
    res = qy.order("changed_date", desc=True).range(off, off + lim).execute()
    return _paged(res.data, lim, off, "history")


def read_meeting_analysis(supabase, opportunity_id: str, *,
                          limit: int = 5, offset: int = 0) -> dict:
    """Per-meeting AI analysis reports for an opportunity, newest first
    (read-only), from avoma_event_reports."""
    oid = (opportunity_id or "").strip()
    lim = max(1, min(int(limit or 5), 50))
    off = max(0, int(offset or 0))
    res = (supabase.table("avoma_event_reports")
           .select("message_id,meeting_uuid,sf_opportunity_id,"
                   "opportunity_analysis_data,opportunity_analysis_status,"
                   "status,created_at")
           .eq("sf_opportunity_id", oid)
           .order("created_at", desc=True).range(off, off + lim).execute())
    return _paged(res.data, lim, off, "reports")


def read_meetings_by_name(supabase, query: str, *,
                          include_analysis: bool = False,
                          opp_limit: int = 5, meeting_limit: int = 20,
                          analysis_limit: int = 5) -> dict:
    """Resolve a company/deal name to its opportunities and return each one's
    linked meetings (and optionally per-meeting AI analysis), read-only.

    Substring match on opportunity_name OR account_name (same matching as
    read_search_opportunities); for every matched opportunity we attach its
    meetings from meeting_cache and, when include_analysis is set, its
    avoma_event_reports rows. This saves a caller from the
    search -> opportunity_id -> meetings hop when they only know a name.
    """
    q = (query or "").strip().strip("?.,!")
    if not q:
        return {"error": "query is required"}
    o_lim = max(1, min(int(opp_limit or 5), 25))
    found = read_search_opportunities(supabase, q, limit=o_lim)
    opps = found.get("opportunities") or []
    results = []
    for opp in opps:
        oid = (opp.get("opportunity_id") or "").strip()
        if not oid:
            continue
        meetings = read_opportunity_meetings(supabase, oid, limit=meeting_limit)
        entry = {
            "opportunity_id": oid,
            "opportunity_name": opp.get("opportunity_name"),
            "account_name": opp.get("account_name"),
            "meetings": meetings["meetings"],
            "meetings_has_more": meetings["has_more"],
            "meetings_next_offset": meetings["next_offset"],
        }
        if include_analysis:
            analysis = read_meeting_analysis(supabase, oid, limit=analysis_limit)
            entry["analysis"] = analysis["reports"]
            entry["analysis_has_more"] = analysis["has_more"]
            entry["analysis_next_offset"] = analysis["next_offset"]
        results.append(entry)
    return {"query": q, "matched_opportunities": len(results),
            "opportunities": results,
            "opportunities_has_more": found.get("has_more"),
            "opportunities_next_offset": found.get("next_offset")}


def read_opportunity_detail(supabase, opportunity_id: str) -> dict:
    """Full cached state for one opportunity + its meetings + recent field
    history (read-only). Returns {error:...} when the id is unknown."""
    oid = (opportunity_id or "").strip()
    opp = (supabase.table("opportunity_cache").select("*")
           .eq("opportunity_id", oid).limit(1).execute())
    if not (opp.data or []):
        return {"error": f"no cached opportunity with id '{oid}'"}
    meetings = read_opportunity_meetings(supabase, oid, limit=20)
    hist = read_field_history(supabase, oid, limit=20)
    return {"opportunity": opp.data[0],
            "meetings": meetings["meetings"],
            "field_history": hist["history"]}


def _build_tools(supabase):
    """Return a list of read-only StructuredTools closed over the supabase client."""
    from langchain_core.tools import StructuredTool

    loop_run = asyncio.get_event_loop

    async def _exec(fn):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, fn)

    async def search_opportunities(query: str, limit: int = 10) -> str:
        """Search opportunities by name or account (case-insensitive substring).
        Returns key fields (amount, stage, health score, momentum, etc.)."""
        return _short(await _exec(
            lambda: read_search_opportunities(supabase, query, limit=limit)))

    async def filter_opportunities(momentum: Optional[str] = None,
                                   stage: Optional[str] = None,
                                   min_amount: Optional[float] = None,
                                   max_amount: Optional[float] = None,
                                   max_meetings: Optional[int] = None,
                                   is_closed: Optional[bool] = None,
                                   order_by: str = "amount",
                                   descending: bool = True,
                                   limit: int = 25) -> str:
        """Filter opportunities by structured criteria. momentum is one of
        Active/Moderate/Slow/Stalled. order_by can be amount, health_score, or
        days_in_stage. Use max_meetings=0 to find deals with no meetings."""
        return _short(await _exec(lambda: read_filter_opportunities(
            supabase, momentum=momentum, stage=stage, min_amount=min_amount,
            max_amount=max_amount, max_meetings=max_meetings, is_closed=is_closed,
            order_by=order_by, descending=descending, limit=limit)))

    async def get_opportunity_detail(opportunity_id: str) -> str:
        """Full cached state for one opportunity: all fields + its meetings +
        the last 20 field-history changes (amount/stage/etc.)."""
        return _short(await _exec(
            lambda: read_opportunity_detail(supabase, opportunity_id)))

    async def get_field_history(opportunity_id: str, field_name: Optional[str] = None,
                                limit: int = 30) -> str:
        """History of field changes for an opportunity (e.g. how Amount or
        StageName changed over time). Optionally filter to one field_name."""
        return _short(await _exec(lambda: read_field_history(
            supabase, opportunity_id, field_name=field_name, limit=limit)))

    async def get_meeting_analysis(opportunity_id: str, limit: int = 5) -> str:
        """Per-meeting AI analysis reports (conflicts, win likelihood, evidence)
        for an opportunity, from avoma_event_reports."""
        return _short(await _exec(lambda: read_meeting_analysis(
            supabase, opportunity_id, limit=limit)))

    async def find_meetings_by_name(query: str, include_analysis: bool = False,
                                    opp_limit: int = 5, meeting_limit: int = 20) -> str:
        """Find a deal's meetings directly from a company OR deal name (no
        opportunity_id needed). Substring-matches name/account, then returns each
        matched opportunity with its linked meetings (and, when include_analysis
        is true, its per-meeting AI analysis)."""
        return _short(await _exec(lambda: read_meetings_by_name(
            supabase, query, include_analysis=include_analysis,
            opp_limit=opp_limit, meeting_limit=meeting_limit)))

    specs = [
        (search_opportunities, "search_opportunities"),
        (filter_opportunities, "filter_opportunities"),
        (get_opportunity_detail, "get_opportunity_detail"),
        (get_field_history, "get_field_history"),
        (get_meeting_analysis, "get_meeting_analysis"),
        (find_meetings_by_name, "find_meetings_by_name"),
    ]
    tools = []
    for fn, name in specs:
        tools.append(StructuredTool.from_function(
            coroutine=fn, name=name, description=(fn.__doc__ or name).strip()))
    return tools


async def answer_question(supabase, question: str, *, model_name: Optional[str] = None) -> dict:
    """Answer a natural-language question using only the cache/report tools.

    Returns {"answer": str, "steps": [{tool, args}], "model": str}.
    """
    if supabase is None:
        return {"answer": "Supabase is not configured, so the cached data is unavailable.",
                "steps": [], "model": None}

    q = (question or "").strip()
    if not q:
        return {"answer": "Please enter a question.", "steps": [], "model": None}

    from langchain_openai import ChatOpenAI
    from langchain_core.messages import (SystemMessage, HumanMessage, ToolMessage)

    tools = _build_tools(supabase)
    tool_map = {t.name: t for t in tools}
    mdl = model_name or _QA_MODEL
    llm = ChatOpenAI(model=mdl, temperature=0).bind_tools(tools)

    messages = [SystemMessage(content=_SYSTEM_PROMPT), HumanMessage(content=q)]
    steps = []

    for _ in range(_MAX_ITERS):
        ai = await llm.ainvoke(messages)
        messages.append(ai)
        tool_calls = getattr(ai, "tool_calls", None) or []
        if not tool_calls:
            return {"answer": ai.content or "(no answer)", "steps": steps, "model": mdl}
        for tc in tool_calls:
            name = tc.get("name")
            args = tc.get("args") or {}
            steps.append({"tool": name, "args": args})
            tool = tool_map.get(name)
            if tool is None:
                result = _short({"error": f"unknown tool '{name}'"})
            else:
                try:
                    result = await tool.ainvoke(args)
                except Exception as e:  # noqa: BLE001
                    result = _short({"error": str(e)})
            messages.append(ToolMessage(content=str(result), tool_call_id=tc.get("id", "")))

    # Ran out of iterations — force a final answer from what we have, with the
    # tools unbound so the model must respond in prose rather than call again.
    plain_llm = ChatOpenAI(model=mdl, temperature=0)
    messages.append(HumanMessage(
        content="Answer now using only what the tools returned above. "
                "If it is insufficient, say the data is not in the cached records."))
    final = await plain_llm.ainvoke(messages)
    answer = (final.content or "").strip() or (
        "I couldn't find enough in the cached records to answer that confidently. "
        "Try naming a specific opportunity or account.")
    return {"answer": answer, "steps": steps, "model": mdl}
