"""
SF enrichment for Avoma meeting events.

Given a meeting UUID (from an SNS Notification), this module:
  1. Pulls the meeting's CRM associations from Avoma to extract
     opportunity_id / account_id / contact_ids.
  2. Runs three tiers of SF queries in parallel:
       - deal_health     : Opp + stage history + open tasks + last 5 activities (~5 KB)
       - account_briefing: deal_health + all contacts on account + related opps +
                           opp team + opp contact roles (~30 KB)
       - full_snapshot   : everything above + emails + cases + contracts +
                           campaign touches + chatter + files (~50-100 KB)
  3. Summarises each tier via gpt-4o-mini so the operator can judge usefulness.
  4. Returns a single dict ready to insert into public.avoma_event_reports.

Functional style — no global state, every function takes its inputs explicitly.
"""

import os
import json
import time
import httpx
from typing import Optional, Any
from simple_salesforce import Salesforce
from openai import OpenAI

AVOMA_TOKEN = os.environ.get("AVOMA_API_TOKEN", "") or "ifi116h6e8:2p7r6khoxqojr5638sld"
AVOMA_BASE = "https://api.avoma.com/v1"
# NOTE: hardcoded fallback matches the pattern in avoma_mcp_server.py:12. Rotate
# the token via the AVOMA_API_TOKEN secret and remove the fallback before any
# prod / external-tenant deployment.

_SF: Optional[Salesforce] = None
_OAI: Optional[OpenAI] = None


def _sf() -> Salesforce:
    global _SF
    if _SF is None:
        _SF = Salesforce(
            username=os.environ["SF_USERNAME"],
            password=os.environ["SF_PASSWORD"],
            security_token=os.environ["SF_SECURITY_TOKEN"],
            domain=os.environ.get("SF_DOMAIN", "login"),
        )
    return _SF


def _oai() -> OpenAI:
    global _OAI
    if _OAI is None:
        _OAI = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    return _OAI


def _safe_query(sf: Salesforce, q: str) -> list[dict]:
    """SOQL that never raises — returns [] on error so one bad query
    doesn't sink the whole enrichment."""
    try:
        return sf.query_all(q).get("records", [])
    except Exception as e:
        return [{"_error": str(e)[:300], "_query": q[:200]}]


def _soql_in_list(ids: list[str]) -> str:
    """Quote a list of SF IDs for IN clause."""
    return ",".join(f"'{x}'" for x in ids if x)


# ---------------- meeting -> SF IDs ----------------

def extract_sf_ids_from_meeting(meeting_uuid: str) -> dict:
    """Call Avoma, return {meeting, opportunity_id, account_id, contact_ids[]}.

    Retries on HTTP 429 (Too Many Requests) honouring Retry-After / exponential
    backoff. Avoma rate-limiting here was the root cause of `status=failed`
    reports — the raw call raised immediately on 429 before this guard.
    """
    _max_retries = int(os.environ.get("AVOMA_MAX_429_RETRIES", "5"))
    r = None
    for _attempt in range(_max_retries + 1):
        r = httpx.get(
            f"{AVOMA_BASE}/meetings/{meeting_uuid}/",
            headers={"Authorization": f"Bearer {AVOMA_TOKEN}"},
            params={"include_crm_associations": "true"},
            timeout=20,
        )
        if r.status_code != 429 or _attempt >= _max_retries:
            break
        _ra = r.headers.get("Retry-After")
        try:
            _delay = float(_ra) if _ra else float(min(2 ** _attempt, 30))
        except (TypeError, ValueError):
            _delay = float(min(2 ** _attempt, 30))
        _delay = max(0.5, min(_delay, 30.0))
        print(f"[AVOMA-429] extract_sf_ids meeting={meeting_uuid} attempt "
              f"{_attempt + 1}/{_max_retries} — sleeping {_delay:.1f}s", flush=True)
        time.sleep(_delay)
    r.raise_for_status()
    meeting = r.json()
    assocs = meeting.get("crm_associations") or []
    opp_id, acct_id, contact_ids = None, None, []
    if isinstance(assocs, list):
        for a in assocs:
            if not isinstance(a, dict):
                continue
            t = (a.get("crm_obj_type") or "").lower()
            oid = a.get("crm_obj_id")
            if not oid:
                continue
            if t.startswith("oppo"):
                opp_id = oid
            elif t == "account":
                acct_id = oid
            elif t == "contact":
                contact_ids.append(oid)
    return {
        "meeting": meeting,
        "opportunity_id": opp_id,
        "account_id": acct_id,
        "contact_ids": sorted(set(contact_ids)),
    }


# ---------------- Tier 1: deal_health (~5 KB) ----------------

def pull_deal_health(opp_id: Optional[str], contact_ids: list[str]) -> dict:
    """Opp + stage history + open tasks + last 5 activities."""
    if not opp_id:
        return {"_skipped": "no opportunity_id"}
    sf = _sf()
    out: dict[str, Any] = {}
    out["opportunity"] = _safe_query(
        sf,
        f"SELECT Id, Name, AccountId, Amount, StageName, CloseDate, Probability, "
        f"ForecastCategory, OwnerId, Owner.Name, CreatedDate, LastModifiedDate, "
        f"LastActivityDate, IsClosed, IsWon, Type, LeadSource "
        f"FROM Opportunity WHERE Id = '{opp_id}'",
    )
    out["stage_history"] = _safe_query(
        sf,
        f"SELECT StageName, Amount, CloseDate, CreatedDate, CreatedBy.Name "
        f"FROM OpportunityHistory WHERE OpportunityId = '{opp_id}' "
        f"ORDER BY CreatedDate DESC LIMIT 20",
    )
    out["open_tasks"] = _safe_query(
        sf,
        f"SELECT Id, Subject, Status, Priority, ActivityDate, OwnerId, Owner.Name, Description "
        f"FROM Task WHERE WhatId = '{opp_id}' AND IsClosed = false "
        f"ORDER BY ActivityDate ASC NULLS LAST LIMIT 25",
    )
    out["recent_activities"] = _safe_query(
        sf,
        f"SELECT Id, Subject, Status, ActivityDate, CompletedDateTime, OwnerId, Owner.Name, Description "
        f"FROM Task WHERE WhatId = '{opp_id}' "
        f"ORDER BY CreatedDate DESC LIMIT 5",
    )
    return out


# ---------------- Tier 2: account_briefing (~30 KB) ----------------

def pull_account_briefing(
    opp_id: Optional[str],
    account_id: Optional[str],
    contact_ids: list[str],
    deal_health: Optional[dict] = None,
) -> dict:
    """account + all contacts + related opps + team + contact roles + (passed-in) deal_health.

    Pass `deal_health` to avoid re-running Tier 1 queries; will compute if None.
    """
    sf = _sf()
    out: dict[str, Any] = {"deal_health": deal_health if deal_health is not None else pull_deal_health(opp_id, contact_ids)}

    if account_id:
        out["account"] = _safe_query(
            sf,
            f"SELECT Id, Name, Industry, NumberOfEmployees, AnnualRevenue, "
            f"BillingCountry, BillingState, BillingCity, Type, Website, "
            f"OwnerId, Owner.Name, ParentId, Parent.Name, CreatedDate "
            f"FROM Account WHERE Id = '{account_id}'",
        )
        out["all_account_contacts"] = _safe_query(
            sf,
            f"SELECT Id, Name, Title, Email, Phone, Department, LeadSource, CreatedDate "
            f"FROM Contact WHERE AccountId = '{account_id}' "
            f"ORDER BY CreatedDate DESC LIMIT 50",
        )
        out["related_opportunities"] = _safe_query(
            sf,
            f"SELECT Id, Name, StageName, Amount, CloseDate, IsClosed, IsWon, OwnerId, Owner.Name "
            f"FROM Opportunity WHERE AccountId = '{account_id}' "
            f"ORDER BY CloseDate DESC LIMIT 25",
        )

    if opp_id:
        out["opportunity_team"] = _safe_query(
            sf,
            f"SELECT Id, UserId, User.Name, TeamMemberRole "
            f"FROM OpportunityTeamMember WHERE OpportunityId = '{opp_id}'",
        )
        out["opportunity_contact_roles"] = _safe_query(
            sf,
            f"SELECT Id, ContactId, Contact.Name, Contact.Title, Role, IsPrimary "
            f"FROM OpportunityContactRole WHERE OpportunityId = '{opp_id}'",
        )

    if contact_ids:
        out["meeting_attendees_full"] = _safe_query(
            sf,
            f"SELECT Id, Name, Title, Email, Phone, Department, AccountId, Account.Name, LeadSource "
            f"FROM Contact WHERE Id IN ({_soql_in_list(contact_ids)})",
        )

    return out


# ---------------- Tier 3: full_snapshot (~50-100 KB) ----------------

def pull_full_snapshot(
    opp_id: Optional[str],
    account_id: Optional[str],
    contact_ids: list[str],
    account_briefing: Optional[dict] = None,
) -> dict:
    """emails + cases + contracts + campaigns + chatter + files + (passed-in) account_briefing.

    Pass `account_briefing` to avoid re-running Tier 1+2 queries; will compute if None.
    """
    sf = _sf()
    out: dict[str, Any] = {"account_briefing": account_briefing if account_briefing is not None else pull_account_briefing(opp_id, account_id, contact_ids)}

    if opp_id:
        out["opp_line_items"] = _safe_query(
            sf,
            f"SELECT Id, Product2.Name, Quantity, UnitPrice, TotalPrice, ServiceDate "
            f"FROM OpportunityLineItem WHERE OpportunityId = '{opp_id}'",
        )
        out["opp_field_history"] = _safe_query(
            sf,
            f"SELECT Field, OldValue, NewValue, CreatedDate, CreatedBy.Name "
            f"FROM OpportunityFieldHistory WHERE OpportunityId = '{opp_id}' "
            f"ORDER BY CreatedDate DESC LIMIT 25",
        )
        out["opp_chatter"] = _safe_query(
            sf,
            f"SELECT Id, Type, Body, CreatedDate, CreatedBy.Name "
            f"FROM FeedItem WHERE ParentId = '{opp_id}' "
            f"ORDER BY CreatedDate DESC LIMIT 10",
        )
        out["opp_files"] = _safe_query(
            sf,
            f"SELECT ContentDocumentId, ContentDocument.Title, ContentDocument.FileType, "
            f"ContentDocument.ContentSize, ContentDocument.CreatedDate "
            f"FROM ContentDocumentLink WHERE LinkedEntityId = '{opp_id}' LIMIT 20",
        )
        out["opp_emails"] = _safe_query(
            sf,
            f"SELECT Id, Subject, FromAddress, ToAddress, MessageDate, Status, HasAttachment "
            f"FROM EmailMessage WHERE RelatedToId = '{opp_id}' "
            f"ORDER BY MessageDate DESC LIMIT 15",
        )

    if account_id:
        out["open_cases"] = _safe_query(
            sf,
            f"SELECT Id, CaseNumber, Subject, Status, Priority, CreatedDate, OwnerId, Owner.Name "
            f"FROM Case WHERE AccountId = '{account_id}' AND IsClosed = false "
            f"ORDER BY CreatedDate DESC LIMIT 15",
        )
        out["contracts"] = _safe_query(
            sf,
            f"SELECT Id, ContractNumber, Status, StartDate, EndDate, ContractTerm, "
            f"OwnerId, Owner.Name "
            f"FROM Contract WHERE AccountId = '{account_id}' "
            f"ORDER BY EndDate DESC NULLS LAST LIMIT 10",
        )

    if contact_ids:
        out["campaign_touches"] = _safe_query(
            sf,
            f"SELECT Id, ContactId, Contact.Name, CampaignId, Campaign.Name, "
            f"Campaign.Type, Campaign.Status, Status, FirstRespondedDate, CreatedDate "
            f"FROM CampaignMember WHERE ContactId IN ({_soql_in_list(contact_ids)}) "
            f"ORDER BY CreatedDate DESC LIMIT 50",
        )

    return out


# ---------------- Summarization ----------------

_SUMMARY_PROMPTS = {
    "deal_health": (
        "You are a sales-ops analyst. Given this Salesforce data pulled right after "
        "a customer meeting, write a 3-5 bullet 'deal health' update: current stage, "
        "days in stage, momentum signals, and any open task that looks blocked or stale. "
        "Be concrete, cite numbers, no fluff."
    ),
    "account_briefing": (
        "You are an account executive. Given this Salesforce account context (the "
        "meeting attendees, all contacts on the account, related opportunities, opp "
        "team, contact roles), write a 5-7 bullet briefing: who else at this account "
        "should we be talking to, what other deals are open, what's the org map, what's "
        "the next move."
    ),
    "full_snapshot": (
        "You are a deal-review analyst. Given this full Salesforce snapshot (opp, "
        "account, line items, field history, emails, cases, contracts, campaign "
        "touches, chatter, files), produce a comprehensive 8-12 bullet review: "
        "deal health, account context, recent engagement signals, risk factors "
        "(cases, churn signals, stalled fields), comms history, and 2-3 concrete "
        "recommended next steps."
    ),
}


def summarize(tier: str, data: dict) -> str:
    """Run gpt-4o-mini over a tier's data. Returns text or '[error: ...]'.

    Short-circuits without calling the LLM when the tier was skipped or has no
    real SF rows — otherwise the model hallucinates plausible-sounding deal
    history out of an empty payload.
    """
    prompt = _SUMMARY_PROMPTS.get(tier)
    if not prompt:
        return f"[no prompt for tier {tier}]"
    if not isinstance(data, dict) or data.get("_skipped"):
        return f"[skipped: {(data or {}).get('_skipped', 'no data')}]"
    # Empty-of-real-rows check: every value is [] or a dict containing _skipped.
    def _empty(v):
        if isinstance(v, list):
            return len(v) == 0
        if isinstance(v, dict):
            return v.get("_skipped") is not None or all(_empty(x) for x in v.values())
        return False
    if all(_empty(v) for v in data.values()):
        return "[skipped: no SF rows returned for this tier]"
    try:
        payload = json.dumps(data, default=str)[:60_000]  # cap at ~60k chars
        resp = _oai().chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"SF data:\n{payload}"},
            ],
            max_tokens=600,
            temperature=0.2,
        )
        return resp.choices[0].message.content or ""
    except Exception as e:
        return f"[summarize error: {e}]"


# ---------------- Deterministic pull (no summaries) ----------------

def _tier_has_errors(d: Any) -> bool:
    """True if any _safe_query sentinel error row is present anywhere in d."""
    if isinstance(d, list):
        return any(isinstance(x, dict) and "_error" in x for x in d)
    if isinstance(d, dict):
        return any(_tier_has_errors(v) for v in d.values())
    return False


def pull_structured_sf_data(
    opp_id: Optional[str],
    account_id: Optional[str],
    contact_ids: list[str],
) -> dict:
    """Deterministic SOQL pull of the 3 structured tiers WITHOUT the gpt-4o-mini
    summaries.

    Used by the Avoma webhook (and the manual refresh-sf endpoint) to refresh
    the cache tables + report row with fresh Salesforce data, since CDC is not
    enabled for Opportunity. Chains the tiers so each reuses the prior tier's
    data instead of re-querying SF (Tier 1 once, not 3x).

    Returns {deal_health_data, account_briefing_data, full_snapshot_data,
    status, error, pull_duration_ms}. Never raises.
    """
    t0 = time.time()
    out: dict[str, Any] = {
        "deal_health_data": None,
        "account_briefing_data": None,
        "full_snapshot_data": None,
        "status": "pending",
        "error": None,
        "pull_duration_ms": 0,
    }
    try:
        deal_health = pull_deal_health(opp_id, contact_ids)
        account_briefing = pull_account_briefing(
            opp_id, account_id, contact_ids, deal_health=deal_health
        )
        full_snapshot = pull_full_snapshot(
            opp_id, account_id, contact_ids, account_briefing=account_briefing
        )
        out["deal_health_data"] = deal_health
        out["account_briefing_data"] = account_briefing
        out["full_snapshot_data"] = full_snapshot
        if any(
            _tier_has_errors(out[f"{t}_data"])
            for t in ("deal_health", "account_briefing", "full_snapshot")
        ):
            out["status"] = "completed_with_errors"
        else:
            out["status"] = "completed"
    except Exception as e:
        out["status"] = "failed"
        out["error"] = str(e)[:1000]
    finally:
        out["pull_duration_ms"] = int((time.time() - t0) * 1000)
    return out


# ---------------- Orchestrator ----------------

def enrich_and_summarize(meeting_uuid: str) -> dict:
    """
    End-to-end: meeting_uuid -> {ids, 3 tiers of data, 3 summaries, duration_ms}.
    Never raises; errors are captured in returned dict under 'error' or per-tier '_error'.
    """
    t0 = time.time()
    result: dict[str, Any] = {
        "meeting_uuid": meeting_uuid,
        "meeting_subject": None,
        "meeting_start_at": None,
        "sf_opportunity_id": None,
        "sf_account_id": None,
        "sf_contact_ids": [],
        "deal_health_data": None,
        "account_briefing_data": None,
        "full_snapshot_data": None,
        "deal_health_summary": None,
        "account_briefing_summary": None,
        "full_snapshot_summary": None,
        "pull_duration_ms": 0,
        "status": "pending",
        "error": None,
    }
    try:
        ids = extract_sf_ids_from_meeting(meeting_uuid)
        meeting = ids.get("meeting") or {}
        result["meeting_subject"] = meeting.get("subject")
        result["meeting_start_at"] = meeting.get("start_at")
        opp = ids.get("opportunity_id")
        acct = ids.get("account_id")
        contacts = ids.get("contact_ids") or []
        result["sf_opportunity_id"] = opp
        result["sf_account_id"] = acct
        result["sf_contact_ids"] = contacts

        if not (opp or acct or contacts):
            result["status"] = "no_sf_links"
            result["pull_duration_ms"] = int((time.time() - t0) * 1000)
            return result

        # Chain: each tier reuses the prior tier's data instead of re-querying
        # SF. Without this, Tier 1 runs 3x and Tier 2 runs 2x per event.
        result["deal_health_data"] = pull_deal_health(opp, contacts)
        result["account_briefing_data"] = pull_account_briefing(
            opp, acct, contacts, deal_health=result["deal_health_data"]
        )
        result["full_snapshot_data"] = pull_full_snapshot(
            opp, acct, contacts, account_briefing=result["account_briefing_data"]
        )

        result["deal_health_summary"] = summarize("deal_health", result["deal_health_data"])
        result["account_briefing_summary"] = summarize("account_briefing", result["account_briefing_data"])
        result["full_snapshot_summary"] = summarize("full_snapshot", result["full_snapshot_data"])

        # Detect _safe_query sentinel rows; downgrade status if any tier had errors.
        def _has_errors(d):
            if isinstance(d, list):
                return any(isinstance(x, dict) and "_error" in x for x in d)
            if isinstance(d, dict):
                return any(_has_errors(v) for v in d.values())
            return False
        if any(_has_errors(result.get(f"{t}_data")) for t in ("deal_health", "account_briefing", "full_snapshot")):
            result["status"] = "completed_with_errors"
        else:
            result["status"] = "completed"
    except Exception as e:
        result["status"] = "failed"
        result["error"] = str(e)[:1000]
    finally:
        result["pull_duration_ms"] = int((time.time() - t0) * 1000)
    return result
