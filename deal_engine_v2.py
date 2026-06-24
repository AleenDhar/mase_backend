"""deal_engine_v2 — analyze-first TWO-STAGE sweep (forecasted-first, flag-gated).

Architecture (the "analyze-first" design, validated on McAfee 2026-06-24):

  STAGE 1 — ANALYZER: a deep agent with the Salesforce + Avoma tools TRIANGULATES
  all four sources — the SF opportunity + Next_Step_History__c, every recent Avoma
  call (discovered by account + attendees, not just the opp id), and the completed
  SF Activities/Tasks INCLUDING their Description/email bodies — into one
  evidence-anchored FACT BASE + dated EVENT TIMELINE. Prompt: mase_deal_analyzer.

  STAGE 2 — DERIVER: a model pass turns that fact base into the canonical card
  (verdict, blocker, 4-bucket to-dos, MEDDPICC, competition, champion, AI), deriving
  from COMPREHENSION of the deal's momentum, not field-fill. Prompt: mase_deal_deriver.

Two LOCKED rules carried by the prompts:
  * field-absence is NOT fact-absence (an empty Competitors__c never means "no
    competition"; judge from evidence across all four sources, recency-weighted);
  * plans are not events (a rep-noted plan / "went well" is not a confirmed fact).

SAFETY: this module is OFF unless DEAL_SWEEP_V2_ENABLED is truthy, and it is NOT
wired into the production analyze_one path — callers invoke analyze_one_v2 explicitly
(forecasted deals only). It reuses the existing sweep helpers (_get_agent build
logic, _build_model, _collect_scoped_tools, _extract_json). The deep-agent
invocation mirrors the v1 sweep but MUST be validated on real infra before any
production rollout (build on a branch, dry-run, compare, then enable the flag).
"""
from __future__ import annotations

import json
import os

import deal_engine_sweep as _s          # _build_model, persistence, helpers
import opportunity_analyzer as _oa      # _collect_scoped_tools, _extract_json, _final_text
import deal_engine_qi as _qi            # FORECASTED set

ID_ANALYZER = "mase_deal_analyzer"
ID_DERIVER = "mase_deal_deriver"


def v2_enabled() -> bool:
    return os.getenv("DEAL_SWEEP_V2_ENABLED", "false").lower() in ("1", "true", "yes")


def is_forecasted(opp: dict) -> bool:
    """Gate v2 to forecasted deals (Commit / Best Case / Upside) — where the extra
    two-stage cost is justified. The pipeline tail keeps the v1 single-pass sweep."""
    return (opp.get("forecast_category") or "").strip().lower() in _qi.FORECASTED


def _load_prompt(key: str, disk_name: str) -> str:
    """Supabase override (admin-editable, no redeploy) else the on-disk seed."""
    try:
        import agent_prompt_store as _aps
        override = (_aps.get_prompt(key) or "").strip()
        if override:
            return override
    except Exception:  # noqa: BLE001
        pass
    try:
        import pathlib
        seed = pathlib.Path(__file__).with_name("prompts") / disk_name
        return seed.read_text(encoding="utf-8") if seed.exists() else ""
    except Exception:  # noqa: BLE001
        return ""


async def _build_analyzer_agent(agent_manager):
    """A deep agent with the SF + Avoma tools and the ANALYZER prompt. Mirrors
    deal_engine_sweep._get_agent's tool collection; separate so the analyzer and the
    v1 sweep can coexist."""
    from deepagents import create_deep_agent
    tools = _oa._collect_scoped_tools(agent_manager)
    if not tools:
        raise RuntimeError("deal_engine_v2: no salesforce/avoma tools loaded yet")
    return create_deep_agent(
        tools=tools,
        system_prompt=_load_prompt(ID_ANALYZER, "mase_deal_analyzer.md"),
        subagents=[],
        model=_s._build_model(),
        middleware=[],
        debug=False,
    )


async def _run_analyzer(agent_manager, opp: dict) -> dict:
    agent = await _build_analyzer_agent(agent_manager)
    oid = opp.get("id") or opp.get("opp_id")
    user = (
        f"Analyze opportunity {oid} ({opp.get('name')}, account {opp.get('account')}).\n"
        "Triangulate ALL FOUR sources using your tools, then COMPREHEND the momentum:\n"
        "1) the Salesforce Opportunity fields + Next_Step__c + Next_Step_History__c;\n"
        "2) every recent Avoma call — search by ACCOUNT + attendee names/domains AND the "
        "opp id (a relevant call is often filed under the account or a sibling opp);\n"
        "3) the COMPLETED Salesforce Tasks/Activities INCLUDING each Description / logged-"
        "email body (the richest, most under-read source);\n"
        "4) reconcile them into one dated timeline.\n"
        "Remember: an empty Salesforce field is NOT proof a fact is false; plans are not "
        "events. Produce the fact base + event timeline per your system prompt. Emit JSON only."
    )
    res = await agent.ainvoke({"messages": [{"role": "user", "content": user}]})
    text = _oa._final_text(res) if hasattr(_oa, "_final_text") else str(res)
    return _oa._extract_json(text) or {}


async def _run_deriver(fact_base: dict) -> dict:
    from langchain_core.messages import SystemMessage, HumanMessage
    prompt = _load_prompt(ID_DERIVER, "mase_deal_deriver.md")
    user = "FACT BASE + EVENT TIMELINE (from the Analyzer):\n" + json.dumps(fact_base, ensure_ascii=False)
    resp = await _s._build_model().ainvoke([SystemMessage(content=prompt), HumanMessage(content=user)])
    return _oa._extract_json(getattr(resp, "content", "") or "") or {}


async def analyze_one_v2(agent_manager, opp: dict, source: str = "manual", dry_run: bool = False) -> dict:
    """Two-stage analyze-first sweep for ONE forecasted opp.

    Returns {opp_id, status, record, error}. Persists via deal_engine_store.upsert_record
    unless dry_run. Forecasted-gated by the caller (or here if you prefer). Never raises.
    """
    oid = opp.get("id") or opp.get("opp_id")
    out = {"opp_id": oid, "status": "failed", "record": None, "error": None}
    try:
        fact_base = await _run_analyzer(agent_manager, opp)
        card = await _run_deriver(fact_base)
        record = dict(card or {})
        # Carry the analysis through so the UI gets the timeline + provenance and a
        # later sweep can build on it.
        record.setdefault("fact_base", (fact_base or {}).get("fact_base"))
        record.setdefault("event_timeline", (fact_base or {}).get("event_timeline"))
        record.setdefault("evidence_coverage", (fact_base or {}).get("evidence_coverage"))
        record["opp_id"] = oid
        record["schema_version"] = "v2-analyze-first"
        out["record"] = record
        out["status"] = "completed" if record.get("ai") else "failed"
        if out["status"] == "completed" and not dry_run:
            import deal_engine_store as _store
            import asyncio
            await asyncio.get_running_loop().run_in_executor(None, _store.upsert_record, record)
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
    return out
