"""Agent tools for reading the Opportunity Observatory dossiers.

Exposes 3 @tool functions to the agent, ALL hard-scoped to the single table
public.opportunity_observatory. This is a thin, read-only wrapper over the
Supabase REST API — the table name is a module constant and is never taken
from the model, so the agent cannot pivot to any other table the way it could
with the generic supabase_query MCP tool.

    - list_opportunity_dossiers   (lightweight list / filter / full-text search)
    - get_opportunity_dossier     (one full dossier by opportunity_id)
    - search_opportunity_dossiers (full-text search across name + account)

Each dossier row has these long-form sections (markdown):
    sf_90day_evidence, avoma_evidence, outbound_campaign_intelligence,
    bundle_a_deal_progress, bundle_b_competition_fit,
    bundle_c_stakeholder_map, bundle_d_vulnerabilities, diagnosis_sheet
plus the SF header fields: name, opportunity_owner, close_date, amount,
stage, account_name.
"""

import json
import os
from typing import Optional

import httpx
from langchain_core.tools import tool

# Hard-coded. The model never supplies the table name — this is the entire
# point of the wrapper: the agent stays locked to this one table.
_TABLE = "opportunity_observatory"

_SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
_SUPABASE_KEY = (
    os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    or os.environ.get("SUPABASE_SERVICE_KEY")
    or ""
)

# Lightweight columns for list views (omit the heavy markdown sections).
_LIGHT_COLS = "opportunity_id,name,opportunity_owner,close_date,amount,stage,account_name,updated_at"
# All the long-form analysis sections.
_SECTION_COLS = (
    "sf_90day_evidence,avoma_evidence,outbound_campaign_intelligence,"
    "bundle_a_deal_progress,bundle_b_competition_fit,bundle_c_stakeholder_map,"
    "bundle_d_vulnerabilities,diagnosis_sheet"
)
_VALID_SECTIONS = set(_SECTION_COLS.split(","))


def _rest(params: dict) -> list:
    """Read-only GET against the single Observatory table. No other table is reachable."""
    if not _SUPABASE_URL or not _SUPABASE_KEY:
        raise RuntimeError("Supabase not configured (SUPABASE_URL / SERVICE_ROLE_KEY)")
    headers = {
        "apikey": _SUPABASE_KEY,
        "Authorization": f"Bearer {_SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    r = httpx.get(
        f"{_SUPABASE_URL}/rest/v1/{_TABLE}",
        headers=headers,
        params=params,
        timeout=30.0,
    )
    r.raise_for_status()
    return r.json()


@tool
def list_opportunity_dossiers(
    limit: int = 50,
    stage: Optional[str] = None,
    account_name_contains: Optional[str] = None,
    name_contains: Optional[str] = None,
) -> str:
    """List Opportunity Observatory dossiers (lightweight — header fields only).

    The Observatory holds one rich, pre-computed dossier per opportunity (SF
    90-day evidence, Avoma evidence, outbound/campaign intelligence, and four
    diagnostic bundles A-D plus a final diagnosis sheet). This tool returns ONLY
    the header fields so you can find the right opportunity before pulling its
    full dossier with get_opportunity_dossier.

    Args:
        limit:                 Max rows (default 50, hard cap 200).
        stage:                 Exact stage filter (e.g. 'Qualified', 'Shortlisted').
        account_name_contains: Case-insensitive substring match on account_name.
        name_contains:         Case-insensitive substring match on opportunity name.

    Returns:
        JSON string: {count, dossiers:[{opportunity_id, name, opportunity_owner,
        close_date, amount, stage, account_name, updated_at}]}.
    """
    try:
        params = {
            "select": _LIGHT_COLS,
            "order": "account_name.asc",
            "limit": str(min(max(int(limit), 1), 200)),
        }
        if stage:
            params["stage"] = f"eq.{stage}"
        if account_name_contains:
            params["account_name"] = f"ilike.*{account_name_contains}*"
        if name_contains:
            params["name"] = f"ilike.*{name_contains}*"
        rows = _rest(params)
        return json.dumps({"count": len(rows), "dossiers": rows}, default=str)
    except Exception as e:
        return json.dumps({"error": f"list_opportunity_dossiers failed: {type(e).__name__}: {e}"})


@tool
def get_opportunity_dossier(
    opportunity_id: str,
    sections: Optional[str] = None,
) -> str:
    """Fetch one full Opportunity Observatory dossier by opportunity_id.

    Returns the header fields plus the long-form analysis sections. Each section
    is multi-paragraph markdown, so pull only the sections you need for large
    dossiers.

    Available sections:
        sf_90day_evidence              SF 90-day evidence pull (snapshot, history, stakeholders)
        avoma_evidence                 Avoma call evidence pull
        outbound_campaign_intelligence Outbound & campaign intelligence
        bundle_a_deal_progress         Bundle A: deal progress & execution
        bundle_b_competition_fit       Bundle B: competition & product-fit
        bundle_c_stakeholder_map       Bundle C: stakeholder & confidence map
        bundle_d_vulnerabilities       Bundle D: vulnerabilities & open risks
        diagnosis_sheet                Final opportunity diagnosis sheet

    Args:
        opportunity_id: Salesforce 15-char Opportunity Id (from list/search).
        sections:       Optional comma-separated subset of section names above.
                        Omit to return ALL sections (can be large).

    Returns:
        JSON string with the header fields + requested section(s), or an error.
    """
    try:
        if sections:
            requested = [s.strip() for s in sections.split(",") if s.strip()]
            bad = [s for s in requested if s not in _VALID_SECTIONS]
            if bad:
                return json.dumps({
                    "error": f"unknown section(s): {bad}. valid: {sorted(_VALID_SECTIONS)}"
                })
            cols = _LIGHT_COLS + "," + ",".join(requested)
        else:
            cols = _LIGHT_COLS + "," + _SECTION_COLS
        rows = _rest({
            "select": cols,
            "opportunity_id": f"eq.{opportunity_id}",
            "limit": "1",
        })
        if not rows:
            return json.dumps({"error": f"no dossier found for opportunity_id={opportunity_id}"})
        return json.dumps(rows[0], default=str)
    except Exception as e:
        return json.dumps({"error": f"get_opportunity_dossier failed: {type(e).__name__}: {e}"})


@tool
def search_opportunity_dossiers(query: str, limit: int = 20) -> str:
    """Substring search the Observatory across opportunity name + account name.

    Case-insensitive ILIKE match on either `name` or `account_name`. Use this when
    you have a fuzzy company or deal name. Returns lightweight header rows; follow
    up with get_opportunity_dossier for the full content.

    Args:
        query: Search text (e.g. 'Bright Horizons', 'Anora'). Matched as a
               substring against both name and account_name.
        limit: Max rows (default 20, hard cap 200).

    Returns:
        JSON string: {count, dossiers:[...header fields...]}.
    """
    try:
        q = (query or "").strip()
        if not q:
            return json.dumps({"error": "empty query"})
        # Strip PostgREST filter metacharacters so the substring stays a value,
        # not an operator/wildcard injection into the or= expression.
        safe = q.replace("*", "").replace(",", " ").replace("(", " ").replace(")", " ").strip()
        if not safe:
            return json.dumps({"error": "empty query after sanitisation"})
        rows = _rest({
            "select": _LIGHT_COLS,
            "or": f"(name.ilike.*{safe}*,account_name.ilike.*{safe}*)",
            "limit": str(min(max(int(limit), 1), 200)),
        })
        return json.dumps({"count": len(rows), "dossiers": rows}, default=str)
    except Exception as e:
        return json.dumps({"error": f"search_opportunity_dossiers failed: {type(e).__name__}: {e}"})
