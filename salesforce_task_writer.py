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
