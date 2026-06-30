"""Shared helpers for the Claude-Code AI-scoring path (NO Anthropic API).

Supabase REST over `requests` (not the app's httpx client) so it works behind a
TLS-inspecting corporate proxy: set CORP_CA_BUNDLE to the proxy root CA and every
call verifies against it; on a normal network leave it unset (system CAs are used).

Reads SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_SERVICE_KEY) from the
environment / a .env at the repo root.
"""
from __future__ import annotations
import os
import requests

SB_URL = (os.getenv("SUPABASE_URL") or "").rstrip("/")
SB_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SERVICE_KEY") or ""

# The forecast block = these three forecast categories (mirrors deal_engine_verdict).
FORECASTED_FC = {"commit", "best case", "upside key deal"}


def _verify():
    """CA bundle for requests: CORP_CA_BUNDLE if it exists, else default system CAs."""
    b = os.getenv("CORP_CA_BUNDLE")
    return b if (b and os.path.exists(b)) else True


def _headers(extra=None):
    h = {"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY,
         "Content-Type": "application/json"}
    if extra:
        h.update(extra)
    return h


def require_env():
    if not SB_URL or not SB_KEY:
        raise SystemExit("Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (e.g. in .env).")


def sb_get(path: str):
    r = requests.get(f"{SB_URL}/rest/v1/{path}", headers=_headers(),
                     verify=_verify(), timeout=60)
    r.raise_for_status()
    return r.json()


def sb_patch(path: str, body: dict) -> int:
    r = requests.patch(f"{SB_URL}/rest/v1/{path}", headers=_headers({"Prefer": "return=minimal"}),
                       json=body, verify=_verify(), timeout=60)
    r.raise_for_status()
    return r.status_code


def fetch_active_records() -> list[dict]:
    """Every ACTIVE tracked deal: [{opp_id, record}, ...] (paginated)."""
    out, off = [], 0
    while True:
        pg = sb_get(f"deal_records?select=opp_id,record&active=is.true&limit=1000&offset={off}")
        out += pg
        if len(pg) < 1000:
            return out
        off += 1000
