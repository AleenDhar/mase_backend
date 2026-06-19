"""salesforce_task_writer.py — direct, server-side Salesforce Task writer.

This is the ONE explicit, human-initiated Salesforce write path in the app. It
opens its OWN simple-salesforce connection using the same credentials the
Salesforce MCP server uses (SF_USERNAME / SF_PASSWORD / SF_SECURITY_TOKEN /
SF_DOMAIN) and creates a single COMPLETED Task on an Opportunity.

It deliberately does NOT route through the agent tool catalog or the MCP servers,
so the agent's Salesforce write lockdown (MCP_TOOL_DENYLIST) stays fully intact.
The field mapping mirrors the proven `create_task` MCP tool
(salesforce_mcp_server.py): Subject / Status / WhatId / ActivityDate /
Description (+ optional WhoId / OwnerId).

Functional, dependency-light: one cached connection + one create function.
"""
from __future__ import annotations

import functools
import os
from typing import Optional

# Salesforce's Subject field caps at 255 chars.
SUBJECT_MAX = 255


class SalesforceWriteError(Exception):
    """Raised when the Salesforce connection or Task create fails."""


@functools.lru_cache(maxsize=1)
def _sf_conn():
    """Establish and cache a Salesforce connection (same env vars as the MCP
    server). Imported lazily so the module loads even where simple-salesforce or
    the SF credentials are absent (the error only surfaces on an actual push)."""
    try:
        from simple_salesforce import Salesforce
    except Exception as e:  # noqa: BLE001
        raise SalesforceWriteError(f"simple-salesforce unavailable: {e}")
    try:
        return Salesforce(
            username=os.environ["SF_USERNAME"],
            password=os.environ["SF_PASSWORD"],
            security_token=os.environ["SF_SECURITY_TOKEN"],
            domain=os.environ.get("SF_DOMAIN", "login"),
        )
    except KeyError as e:
        raise SalesforceWriteError(f"missing Salesforce credential env var: {e}")
    except Exception as e:  # noqa: BLE001
        raise SalesforceWriteError(f"Salesforce connection failed: {e}")


def get_connection():
    """Public accessor for the shared, cached simple-salesforce connection.

    Lets other server-side modules (e.g. deal_engine_report) reuse the same
    SF_*-credentialed connection instead of opening their own. Raises
    SalesforceWriteError if simple-salesforce or the credentials are missing."""
    return _sf_conn()


def truncate_subject(subject: str) -> str:
    s = (subject or "").strip()
    return s[:SUBJECT_MAX]


def create_completed_task(
    *,
    subject: str,
    what_id: str,
    activity_date: str,
    description: Optional[str] = None,
    who_id: Optional[str] = None,
    owner_id: Optional[str] = None,
) -> dict:
    """Create a COMPLETED Salesforce Task linked to an Opportunity (WhatId).

    Mirrors the create_task MCP tool's field mapping. Subject is required and is
    truncated to Salesforce's 255-char limit. Returns the raw Salesforce create
    result, e.g. {"id": "00T...", "success": True, "errors": []}. Raises
    SalesforceWriteError on connection/create failure (caller does NOT persist a
    push record on failure, so the rep can retry)."""
    subj = truncate_subject(subject)
    if not subj:
        raise SalesforceWriteError("subject is required")
    if not (what_id or "").strip():
        raise SalesforceWriteError("what_id (Opportunity id) is required")
    payload: dict = {
        "Subject": subj,
        "Status": "Completed",
        "Priority": "Normal",
        "WhatId": what_id.strip(),
    }
    if activity_date:
        payload["ActivityDate"] = activity_date
    if description:
        payload["Description"] = description
    if who_id:
        payload["WhoId"] = who_id
    if owner_id:
        payload["OwnerId"] = owner_id
    try:
        return _sf_conn().Task.create(payload)
    except SalesforceWriteError:
        raise
    except Exception as e:  # noqa: BLE001
        raise SalesforceWriteError(f"Task create failed: {e}")


# Salesforce REST API version used for the per-user (OAuth) write path.
SF_API_VERSION = "v60.0"


def create_completed_task_oauth(
    *,
    access_token: str,
    instance_url: str,
    subject: str,
    what_id: str,
    activity_date: str,
    description: Optional[str] = None,
    who_id: Optional[str] = None,
    owner_id: Optional[str] = None,
) -> dict:
    """Create the COMPLETED Task using a USER's OAuth access token (the rep/VP),
    so CreatedBy AND Owner are the rep — not the shared integration user. Same
    field mapping as create_completed_task, but POSTs to the REST API at the
    user's instance_url with their bearer token. Returns the SF create result
    {"id","success","errors"}. Raises SalesforceWriteError on failure (incl. 401
    so the caller can fall back to the shared connection)."""
    import requests  # lazy: keeps module import light
    subj = truncate_subject(subject)
    if not subj:
        raise SalesforceWriteError("subject is required")
    if not (what_id or "").strip():
        raise SalesforceWriteError("what_id (Opportunity id) is required")
    if not (access_token and instance_url):
        raise SalesforceWriteError("missing access_token/instance_url for per-user push")
    payload: dict = {
        "Subject": subj,
        "Status": "Completed",
        "Priority": "Normal",
        "WhatId": what_id.strip(),
    }
    if activity_date:
        payload["ActivityDate"] = activity_date
    if description:
        payload["Description"] = description
    if who_id:
        payload["WhoId"] = who_id
    if owner_id:
        payload["OwnerId"] = owner_id
    url = f"{instance_url.rstrip('/')}/services/data/{SF_API_VERSION}/sobjects/Task"
    try:
        r = requests.post(
            url, json=payload,
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            timeout=30,
        )
    except Exception as e:  # noqa: BLE001
        raise SalesforceWriteError(f"Task create (oauth) request failed: {e}")
    if r.status_code in (200, 201):
        try:
            j = r.json()
        except Exception:  # noqa: BLE001
            j = {}
        return {"id": j.get("id"), "success": bool(j.get("success", True)), "errors": j.get("errors", [])}
    raise SalesforceWriteError(f"Task create (oauth) failed [{r.status_code}]: {r.text[:300]}")


# ---------------------------------------------------------------------------
# Open-activity Task writers (Status='Planned' — this org's open-task value) and
# the Next Step append writers. Added for the 3-destination update branching
# (Next Step / open To-Do activity / Completed). All stay on this sanctioned,
# human-initiated path so the agent's MCP write lockdown is untouched.
# ---------------------------------------------------------------------------

def create_open_task(
    *,
    subject: str,
    what_id: str,
    activity_date: str,
    description: Optional[str] = None,
    who_id: Optional[str] = None,
    owner_id: Optional[str] = None,
) -> dict:
    """Create an OPEN Salesforce Task (Status='Planned') on an Opportunity, with
    activity_date as the DUE date. 'Planned' is this org's open-task picklist value
    (Planned / In Process / Completed / ...). Same field mapping + shared connection
    as create_completed_task; left open so it surfaces as a pending activity."""
    subj = truncate_subject(subject)
    if not subj:
        raise SalesforceWriteError("subject is required")
    if not (what_id or "").strip():
        raise SalesforceWriteError("what_id (Opportunity id) is required")
    payload: dict = {
        "Subject": subj,
        "Status": "Planned",
        "Priority": "Normal",
        "WhatId": what_id.strip(),
    }
    if activity_date:
        payload["ActivityDate"] = activity_date
    if description:
        payload["Description"] = description
    if who_id:
        payload["WhoId"] = who_id
    if owner_id:
        payload["OwnerId"] = owner_id
    try:
        return _sf_conn().Task.create(payload)
    except SalesforceWriteError:
        raise
    except Exception as e:  # noqa: BLE001
        raise SalesforceWriteError(f"Open Task create failed: {e}")


def create_open_task_oauth(
    *,
    access_token: str,
    instance_url: str,
    subject: str,
    what_id: str,
    activity_date: str,
    description: Optional[str] = None,
    who_id: Optional[str] = None,
    owner_id: Optional[str] = None,
) -> dict:
    """create_open_task via the rep's OAuth token (CreatedBy + Owner = the rep)."""
    import requests  # lazy
    subj = truncate_subject(subject)
    if not subj:
        raise SalesforceWriteError("subject is required")
    if not (what_id or "").strip():
        raise SalesforceWriteError("what_id (Opportunity id) is required")
    if not (access_token and instance_url):
        raise SalesforceWriteError("missing access_token/instance_url for per-user push")
    payload: dict = {
        "Subject": subj,
        "Status": "Planned",
        "Priority": "Normal",
        "WhatId": what_id.strip(),
    }
    if activity_date:
        payload["ActivityDate"] = activity_date
    if description:
        payload["Description"] = description
    if who_id:
        payload["WhoId"] = who_id
    if owner_id:
        payload["OwnerId"] = owner_id
    url = f"{instance_url.rstrip('/')}/services/data/{SF_API_VERSION}/sobjects/Task"
    try:
        r = requests.post(
            url, json=payload,
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            timeout=30,
        )
    except Exception as e:  # noqa: BLE001
        raise SalesforceWriteError(f"Open Task create (oauth) request failed: {e}")
    if r.status_code in (200, 201):
        try:
            j = r.json()
        except Exception:  # noqa: BLE001
            j = {}
        return {"id": j.get("id"), "success": bool(j.get("success", True)), "errors": j.get("errors", [])}
    raise SalesforceWriteError(f"Open Task create (oauth) failed [{r.status_code}]: {r.text[:300]}")


def append_next_step(*, opp_id: str, entry: str, existing: Optional[str] = None) -> dict:
    """Prepend `entry` to Opportunity.Next_Step__c, NEWEST ON TOP, preserving the
    FULL prior trail (read-modify-write). Next_Step__c is an HTML rich-text long-
    text-area, so `entry` must already be an HTML fragment (e.g. '<p>...</p>').
    Reads the current value (unless `existing` is supplied) via the shared
    connection, prepends, and updates. Returns {id, success}."""
    oid = (opp_id or "").strip()
    if not oid:
        raise SalesforceWriteError("opp_id is required")
    if not (entry or "").strip():
        raise SalesforceWriteError("entry is required")
    try:
        conn = _sf_conn()
        if existing is None:
            recs = conn.query(
                f"SELECT Next_Step__c FROM Opportunity WHERE Id = '{oid}'"
            ).get("records", [])
            existing = (recs[0].get("Next_Step__c") if recs else "") or ""
        new_val = entry + (existing or "")
        conn.Opportunity.update(oid, {"Next_Step__c": new_val})
        return {"id": oid, "success": True}
    except SalesforceWriteError:
        raise
    except Exception as e:  # noqa: BLE001
        raise SalesforceWriteError(f"Next Step append failed: {e}")


def append_next_step_oauth(
    *, access_token: str, instance_url: str, opp_id: str, entry: str,
) -> dict:
    """append_next_step via the rep's OAuth token (LastModifiedBy = the rep). GETs
    the current Next_Step__c, prepends `entry`, PATCHes the field."""
    import requests  # lazy
    oid = (opp_id or "").strip()
    if not oid:
        raise SalesforceWriteError("opp_id is required")
    if not (entry or "").strip():
        raise SalesforceWriteError("entry is required")
    if not (access_token and instance_url):
        raise SalesforceWriteError("missing access_token/instance_url for per-user push")
    base = f"{instance_url.rstrip('/')}/services/data/{SF_API_VERSION}/sobjects/Opportunity/{oid}"
    hdr = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    try:
        g = requests.get(base + "?fields=Next_Step__c", headers=hdr, timeout=30)
    except Exception as e:  # noqa: BLE001
        raise SalesforceWriteError(f"Next Step read (oauth) failed: {e}")
    if g.status_code != 200:
        raise SalesforceWriteError(f"Next Step read (oauth) failed [{g.status_code}]: {g.text[:200]}")
    try:
        existing = (g.json().get("Next_Step__c") or "")
    except Exception:  # noqa: BLE001
        existing = ""
    new_val = entry + existing
    try:
        p = requests.patch(base, json={"Next_Step__c": new_val}, headers=hdr, timeout=30)
    except Exception as e:  # noqa: BLE001
        raise SalesforceWriteError(f"Next Step write (oauth) failed: {e}")
    if p.status_code in (200, 204):
        return {"id": oid, "success": True}
    raise SalesforceWriteError(f"Next Step write (oauth) failed [{p.status_code}]: {p.text[:300]}")
