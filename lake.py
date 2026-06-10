"""lake.py — Opportunity Diagnosis Data Lake

Handles ingestion (write_lake_diagnosis) and context injection
(fetch_prior_diagnoses, extract_account_id_from_messages) for the
lake.opportunity_diagnoses table.

Architecture:
- Path A: regex extraction for all SF-derived fields (free, deterministic)
- Path B: GPT-4o-mini extraction for narrative fields only (momentum_verdict,
          health_rating, top_risks, recommendations)
- All functions are wrapped in try/except — failures log and return gracefully.
- The table is created by the Next.js team; all code handles table-not-found
  silently so agent behaviour is byte-identical when the migration hasn't landed.
"""

import re
import json
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Project constants
# ---------------------------------------------------------------------------

OD_PROJECT_IDS = {
    "87f864e2-50bf-4015-a0f8-4ed7426b2a50",  # Bite Size 2.0
    "22fbcc90-f594-4fd3-978c-26b9efeced11",  # Bite Size v1
}

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Salesforce Account ID — 001-prefix only (rejects Contact 003, Opp 006, User 005)
_SF_ACCOUNT_ID_RE = re.compile(r'\b001[A-Za-z0-9]{12}([A-Za-z0-9]{3})?\b')

_DIAGNOSTIC_TOOL_RE = re.compile(
    r'\b(soql|get_record|get_all_meetings_for_account|'
    r'get_meeting_notes|get_meeting_insights|get_meeting_transcript|'
    r'describe_object)\b',
    re.IGNORECASE,
)


def _is_diagnostic_tool_call(content: str) -> bool:
    """True if a tool_call row's content names a substantive diagnostic tool.

    Used by write_lake_diagnosis to skip lake writes for meta-question chats
    that didn't perform any fresh SF/Avoma data pulls.
    """
    if not content:
        return False
    return bool(_DIAGNOSTIC_TOOL_RE.search(content))


def _relative_time(iso_str: str, now: Optional[datetime] = None) -> str:
    """Render an ISO-8601 timestamp as a human-readable 'N units ago' string.

    Returns an empty string on parse failure (caller should omit the suffix).
    """
    try:
        s = iso_str.replace("Z", "+00:00") if iso_str.endswith("Z") else iso_str
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = now or datetime.now(timezone.utc)
        secs = int((now - dt).total_seconds())
        if secs < 0:
            return "just now"
        if secs < 60:
            return "just now"
        mins = secs // 60
        if mins < 60:
            return f"{mins} minute{'s' if mins != 1 else ''} ago"
        hours = mins // 60
        if hours < 24:
            return f"{hours} hour{'s' if hours != 1 else ''} ago"
        days = hours // 24
        if days < 30:
            return f"{days} day{'s' if days != 1 else ''} ago"
        # Use a 365-day cutoff (not months<12) so the 360-364 day window
        # doesn't fall through to "0 years ago" (architect-flagged edge case).
        if days < 365:
            months = days // 30
            return f"{months} month{'s' if months != 1 else ''} ago"
        years = days // 365
        return f"{years} year{'s' if years != 1 else ''} ago"
    except Exception:
        return ""


def _format_run_header_ts(iso_str: str) -> str:
    """Format an ISO timestamp as 'YYYY-MM-DDTHH:MMZ' (UTC, minute precision).

    Falls back to the raw input on parse failure so headers always render.
    """
    try:
        s = iso_str.replace("Z", "+00:00") if iso_str.endswith("Z") else iso_str
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%MZ")
    except Exception:
        return iso_str

# Account: plain JSON + double-escaped variant
_ACCT_PLAIN_RE = re.compile(
    r'"type"\s*:\s*"Account".{0,200}"Id"\s*:\s*"(001[A-Za-z0-9]{12,15})"[^{}]*"Name"\s*:\s*"([^"]+)"',
    re.DOTALL,
)
_ACCT_ESC_RE = re.compile(
    r'\\"type\\"\s*:\s*\\"Account\\".{0,200}\\"Id\\"\s*:\s*\\"(001[A-Za-z0-9]{12,15})\\"[^{}]*\\"Name\\"\s*:\s*\\"([^"\\]+)\\"',
    re.DOTALL,
)

# AccountId field on other SObject types (Opportunity, Contact, Task, etc.) —
# captures only the ID, no Name (Name only resolves via Account-typed records).
_ACCT_ID_FIELD_PLAIN_RE = re.compile(r'"AccountId"\s*:\s*"(001[A-Za-z0-9]{12,15})"')
_ACCT_ID_FIELD_ESC_RE = re.compile(r'\\"AccountId\\"\s*:\s*\\"(001[A-Za-z0-9]{12,15})\\"')

# Prior-diagnosis row marker — each row produced by fetch_prior_diagnoses begins
# with "### Run N — ".  Anchored to line start (MULTILINE) so incidental "—"
# characters elsewhere in the block (risk lines, narratives) don't get counted.
_PRIOR_ROW_RE = re.compile(r'^### Run \d+ — ', re.MULTILINE)

# Opportunity: plain JSON + double-escaped variant
_OPP_PLAIN_RE = re.compile(
    r'"type"\s*:\s*"Opportunity".{0,200}"Id"\s*:\s*"(006[A-Za-z0-9]{12,15})"[^{}]*"Name"\s*:\s*"([^"]+)"',
    re.DOTALL,
)
_OPP_ESC_RE = re.compile(
    r'\\"type\\"\s*:\s*\\"Opportunity\\".{0,200}\\"Id\\"\s*:\s*\\"(006[A-Za-z0-9]{12,15})\\"[^{}]*\\"Name\\"\s*:\s*\\"([^"\\]+)\\"',
    re.DOTALL,
)

# Opportunity scalar fields (plain JSON)
_STAGE_RE = re.compile(r'"StageName"\s*:\s*"([^"]+)"')
_AMOUNT_RE = re.compile(r'"Amount"\s*:\s*([0-9.]+)')
_CLOSE_DATE_RE = re.compile(r'"CloseDate"\s*:\s*"([^"]+)"')
_FORECAST_RE = re.compile(r'"ForecastCategoryName"\s*:\s*"([^"]+)"')

# OwnerId — deterministic SF ID from the Opportunity record (primary extraction target)
_OWNER_ID_RE = re.compile(r'"OwnerId"\s*:\s*"(005[A-Za-z0-9]{12,15})"')

# Owner display name — pulled from the nested Owner relationship object that
# SOQL returns alongside OwnerId, e.g. `"Owner":{"attributes":{...},"Name":"Jane Doe"}`.
# Two patterns to cover plain JSON (tool result payloads) and backslash-escaped
# JSON (when the same payload is embedded inside another JSON string).
_OWNER_NAME_RE = re.compile(
    r'"Owner"\s*:\s*\{.*?"Name"\s*:\s*"([^"]+)"',
    re.DOTALL,
)
_OWNER_NAME_ESC_RE = re.compile(
    r'\\"Owner\\"\s*:\s*\{.*?\\"Name\\"\s*:\s*\\"([^"\\]+)\\"',
    re.DOTALL,
)

# Product names from OpportunityLineItems.records[].Product2.Name (plain + escaped)
_PRODUCT_NAME_RE = re.compile(
    r'"Product2"\s*:\s*\{[^}]*"Name"\s*:\s*"([^"]+)"'
)
_PRODUCT_NAME_ESC_RE = re.compile(
    r'\\"Product2\\"\s*:\s*\{[^}]*\\"Name\\"\s*:\s*\\"([^"\\]+)\\"'
)
# Fallback: ProductCode field on OpportunityLineItem
_PRODUCT_CODE_RE = re.compile(r'"ProductCode"\s*:\s*"([^"]+)"')

# Avoma meeting dates
# Avoma meeting start times — both plain and single-escaped JSON forms.
# Avoma tool results are persisted as Python list-of-dict serialization with
# backslash-escaped quotes (\"start_at\":\"...\"), not pure JSON.  The earlier
# pattern only matched plain quotes AND used the wrong key name (scheduled_at
# instead of start_at), so it matched 0 of the 15+ chats with real Avoma data.
_AVOMA_START_PLAIN_RE = re.compile(r'"start_at"\s*:\s*"([^"]+)"')
_AVOMA_START_ESC_RE = re.compile(r'\\"start_at\\"\s*:\s*\\"([^"\\]+)\\"')

# Avoma meeting UUID — same dual-form treatment.  Used for last_meeting_id.
_AVOMA_UUID_PLAIN_RE = re.compile(r'"meeting_uuid"\s*:\s*"([^"]+)"')
_AVOMA_UUID_ESC_RE = re.compile(r'\\"meeting_uuid\\"\s*:\s*\\"([^"\\]+)\\"')

# ---------------------------------------------------------------------------
# Path A: regex extraction of SF-derived fields
# ---------------------------------------------------------------------------

def _extract_sf_fields(tool_results_text: str, user_message_text: Optional[str] = None) -> dict:
    """Extract SF-derived fields via regex from concatenated tool result content.

    `user_message_text` is the concatenation of user-role messages only; if
    provided it is the ONLY corpus searched for the bare-ID tier-3 fallback.
    This avoids picking up unrelated 001-prefix IDs that may appear elsewhere
    in tool output (e.g., a related-account record). When omitted, the bare-ID
    fallback is skipped entirely — preferable to a wrong answer.
    """
    fields: dict = {}

    # Account — layered extraction (most specific → least specific):
    #   1. Account-typed record (yields both id + name)
    #   2. AccountId field on Opportunity/Contact/Task/etc. (id only)
    #   3. Bare 001-prefix ID — restricted to user_message_text only.
    m = _ACCT_PLAIN_RE.search(tool_results_text) or _ACCT_ESC_RE.search(tool_results_text)
    if m:
        fields["account_id"] = m.group(1)
        fields["account_name"] = m.group(2)
    else:
        m2 = (
            _ACCT_ID_FIELD_PLAIN_RE.search(tool_results_text)
            or _ACCT_ID_FIELD_ESC_RE.search(tool_results_text)
        )
        if m2:
            fields["account_id"] = m2.group(1)
        elif user_message_text:
            m3 = _SF_ACCOUNT_ID_RE.search(user_message_text)
            if m3:
                fields["account_id"] = m3.group(0)

    # Opportunity
    m = _OPP_PLAIN_RE.search(tool_results_text) or _OPP_ESC_RE.search(tool_results_text)
    if m:
        fields["opportunity_id"] = m.group(1)
        fields["opportunity_name"] = m.group(2)

    # Opportunity scalar fields
    m = _STAGE_RE.search(tool_results_text)
    if m:
        fields["stage"] = m.group(1)

    m = _AMOUNT_RE.search(tool_results_text)
    if m:
        try:
            fields["amount"] = float(m.group(1))
        except ValueError:
            pass

    m = _CLOSE_DATE_RE.search(tool_results_text)
    if m:
        fields["close_date"] = m.group(1)  # YYYY-MM-DD string, Supabase accepts this

    m = _FORECAST_RE.search(tool_results_text)
    if m:
        fields["forecast_category"] = m.group(1)

    # OwnerId from the Opportunity record (primary, deterministic)
    m = _OWNER_ID_RE.search(tool_results_text)
    if m:
        fields["owner"] = m.group(1)

    # Owner display name from the nested Owner.Name relationship.
    # Resolved here so downstream reports/UI don't need a secondary SF lookup.
    m = _OWNER_NAME_RE.search(tool_results_text) or _OWNER_NAME_ESC_RE.search(tool_results_text)
    if m:
        fields["owner_name"] = m.group(1)

    # Products: collect unique names from OpportunityLineItems
    product_names = _PRODUCT_NAME_RE.findall(tool_results_text)
    if not product_names:
        product_names = _PRODUCT_NAME_ESC_RE.findall(tool_results_text)
    if not product_names:
        # Fall back to ProductCode values
        product_names = _PRODUCT_CODE_RE.findall(tool_results_text)
    if product_names:
        # Deduplicate while preserving order
        seen: set = set()
        unique: list = []
        for p in product_names:
            if p not in seen:
                seen.add(p)
                unique.append(p)
        fields["products"] = unique

    return fields


def _extract_avoma_fields(tool_results_text: str) -> dict:
    """Extract Avoma meeting data from tool result payloads.

    Handles both plain JSON and single-escaped JSON (the actual storage shape
    in chat_messages.content).  Produces:
      - last_meeting_date: ISO-8601 timestamp of the most recent meeting start
      - meeting_count_30d: count of meetings whose start_at is within the last
        30 days from "now"
      - last_meeting_id: meeting_uuid paired with the latest start_at when
        derivable; otherwise the first meeting_uuid encountered.
    """
    # 1) Collect all start_at strings (plain + escaped, deduped)
    raw_starts = set()
    raw_starts.update(_AVOMA_START_PLAIN_RE.findall(tool_results_text))
    raw_starts.update(_AVOMA_START_ESC_RE.findall(tool_results_text))

    # 2) Parse to timezone-aware datetimes.  Skip unparseable entries silently.
    parsed: list[datetime] = []
    for s in raw_starts:
        try:
            # Handle both "...Z" suffix and explicit "+HH:MM" offset.
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            parsed.append(dt)
        except (ValueError, AttributeError):
            continue

    # 3) Collect meeting UUIDs (plain + escaped, deduped) — for last_meeting_id.
    raw_uuids = set()
    raw_uuids.update(_AVOMA_UUID_PLAIN_RE.findall(tool_results_text))
    raw_uuids.update(_AVOMA_UUID_ESC_RE.findall(tool_results_text))

    # If no recoverable date AND no recoverable uuid, nothing to write.
    if not parsed and not raw_uuids:
        return {}

    result: dict = {}

    if parsed:
        from datetime import timedelta
        latest = max(parsed)
        # Store as ISO-8601 string so Supabase casts to timestamptz cleanly.
        result["last_meeting_date"] = latest.isoformat()
        # 30-day window measured from "now" (UTC).  This makes the count a
        # rolling window rather than the historical total — matches the field
        # name and the spec's intent.
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        result["meeting_count_30d"] = sum(1 for d in parsed if d >= cutoff)

    if raw_uuids:
        # Best-effort: pick any uuid (set is unordered but stable enough).
        # Cannot reliably pair uuid<->start_at without parsing the full JSON,
        # which we deliberately avoid for resilience to malformed payloads.
        result["last_meeting_id"] = next(iter(raw_uuids))

    return result


# ---------------------------------------------------------------------------
# Path B: GPT-4o-mini extraction of narrative fields
# ---------------------------------------------------------------------------
# Architecture note: lake.py uses its own ChatOpenAI instance rather than
# context_manager._get_summarizer() from server.py.  Reason: lake.py is a
# standalone module that must be importable without importing server.py
# (circular import risk) and must work in the backfill script context
# (Task #10) where no ContextWindowManager is available.  The model version
# and temperature are identical to the summarizer; the only difference is
# the prompt and max_tokens cap.  If the SUMMARIZER_MODEL env var changes
# in future, update this function's model string to match.

_NARRATIVE_SYSTEM_PROMPT = """\
You extract structured diagnosis data from an Opportunity Diagnosis markdown report.

Return ONLY a valid JSON object with these exact keys:
{
  "momentum_verdict": <"accelerating" | "stalling" | "drifting" | null>,
  "health_rating": <"high" | "medium" | "low" | null>,
  "top_risks": <array of {title, dynamic, signal} | null>,
  "recommendations": <array of {what, why, who, when} | null>
}

Rules:
- Output null for any field not clearly present in the text. NEVER fabricate.
- momentum_verdict: pick the single closest word from the allowed values.
- health_rating: pick the single closest word from the allowed values.
- top_risks: capture up to 3 risks. Each must have title (string), dynamic (string), signal (string).
- recommendations: capture Tier 2 recommendations only. Each must have what, why, who, when (all strings).
- Do not include any text outside the JSON object.
"""


async def _extract_narrative_fields(final_response: str, openai_api_key: str) -> dict:
    """Call GPT-4o-mini to extract narrative fields from the final markdown."""
    if not openai_api_key or not final_response:
        return {}
    try:
        from langchain_openai import ChatOpenAI
        from langchain_core.messages import HumanMessage, SystemMessage

        llm = ChatOpenAI(
            model="gpt-4o-mini",
            temperature=0,
            max_tokens=2048,
            api_key=openai_api_key,
        )
        # Cap input at 40k chars — narrative fields live in the final response
        capped = final_response[:40000]
        response = await asyncio.wait_for(
            llm.ainvoke([
                SystemMessage(content=_NARRATIVE_SYSTEM_PROMPT),
                HumanMessage(content=f"Extract from this diagnosis report:\n\n{capped}"),
            ]),
            timeout=30,
        )
        raw = response.content.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = re.sub(r'^```[a-z]*\n?', '', raw)
            raw = re.sub(r'\n?```$', '', raw.strip())
        data = json.loads(raw)
        # Filter to only the expected keys and valid values
        result = {}
        mv = data.get("momentum_verdict")
        if mv in ("accelerating", "stalling", "drifting"):
            result["momentum_verdict"] = mv
        hr = data.get("health_rating")
        if hr in ("high", "medium", "low"):
            result["health_rating"] = hr
        if isinstance(data.get("top_risks"), list):
            result["top_risks"] = data["top_risks"][:3]
        if isinstance(data.get("recommendations"), list):
            result["recommendations"] = data["recommendations"]
        return result
    except asyncio.TimeoutError:
        logger.warning("[LAKE] GPT-4o-mini narrative extraction timed out")
        return {}
    except Exception as e:
        logger.warning(f"[LAKE] GPT-4o-mini narrative extraction failed: {e}")
        return {}


# ---------------------------------------------------------------------------
# Main write function
# ---------------------------------------------------------------------------

async def write_lake_diagnosis(
    chat_id: str,
    project_id: str,
    final_response: str,
    supabase_client,
    openai_api_key: str,
    run_at: Optional[str] = None,
    run_by_user_id: Optional[str] = None,
) -> None:
    """Write a row to lake.opportunity_diagnoses after a diagnosis run completes.

    Must be called as asyncio.create_task() — it is fire-and-forget.
    All exceptions are caught and logged; this function never raises.
    """
    try:
        if not supabase_client:
            logger.warning("[LAKE] No Supabase client, skipping lake write")
            return

        loop = asyncio.get_event_loop()

        # Fetch ALL messages for this chat from Supabase to maximise regex coverage.
        # tool_result rows contain raw SOQL JSON; other rows (thinking, final) may also
        # include SF IDs and product data.  No type filter applied.
        try:
            rows = await loop.run_in_executor(
                None,
                lambda: (
                    supabase_client
                    .table("chat_messages")
                    .select("content,role,type")
                    .eq("chat_id", chat_id)
                    .execute()
                )
            )
            all_rows = rows.data or []

            # Frontend hand-back gate: skip lake write when the chat performed
            # no substantive diagnostic tool calls. Meta-question chats (e.g.
            # "summarise the prior runs") otherwise create phantom rows that
            # masquerade as fresh diagnoses and pollute the timeline.
            diagnostic_tool_calls = sum(
                1 for r in all_rows
                if r.get("type") == "tool_call"
                and _is_diagnostic_tool_call(r.get("content", ""))
            )
            if diagnostic_tool_calls == 0:
                print(
                    f"[LAKE] ⏭️ Skipping lake write for chat={chat_id}: "
                    f"no diagnostic tool calls (meta-question or summary, "
                    f"not a fresh diagnosis)",
                    flush=True,
                )
                return

            tool_results_text = "\n".join(r.get("content", "") for r in all_rows)
            # User-message-only corpus for the bare-ID tier-3 account fallback
            user_message_text = "\n".join(
                r.get("content", "") for r in all_rows if r.get("role") == "user"
            )
        except Exception as e:
            logger.warning(f"[LAKE] Could not fetch chat_messages for {chat_id}: {e}")
            tool_results_text = ""
            user_message_text = ""

        # Path A: regex extraction of SF-derived fields
        sf_fields = _extract_sf_fields(tool_results_text, user_message_text)
        avoma_fields = _extract_avoma_fields(tool_results_text)

        # Path B: GPT-4o-mini extraction of narrative fields
        narrative_fields = await _extract_narrative_fields(final_response, openai_api_key)

        # Build the full row
        now_utc = run_at or datetime.now(timezone.utc).isoformat()
        row = {
            "chat_id": chat_id,
            "project_id": project_id,
            "run_at": now_utc,
            "diagnosis_md": final_response or None,
        }
        if run_by_user_id:
            row["run_by_user_id"] = run_by_user_id

        row.update(sf_fields)
        row.update(avoma_fields)
        row.update(narrative_fields)

        # Convert jsonb fields to JSON strings for Supabase client
        for key in ("top_risks", "recommendations", "products"):
            if key in row and row[key] is not None and not isinstance(row[key], str):
                row[key] = json.dumps(row[key])

        # Upsert (idempotent on chat_id + run_at)
        await loop.run_in_executor(
            None,
            lambda: (
                supabase_client
                .schema("lake")
                .table("opportunity_diagnoses")
                .upsert(row, on_conflict="chat_id,run_at")
                .execute()
            )
        )

        account_label = sf_fields.get("account_name") or sf_fields.get("account_id") or "unknown"
        logger.info(
            f"[LAKE] ✅ Wrote diagnosis row for chat={chat_id} "
            f"account={account_label} verdict={narrative_fields.get('momentum_verdict')}"
        )
        print(
            f"[LAKE] ✅ Wrote diagnosis row for chat={chat_id} "
            f"account={account_label} verdict={narrative_fields.get('momentum_verdict')}",
            flush=True,
        )

    except Exception as e:
        # Any error: log one line, no stack trace leak, never re-raise
        print(f"[LAKE] ⚠️ write_lake_diagnosis failed for chat={chat_id}: {e}", flush=True)


# ---------------------------------------------------------------------------
# Backfill writer — propagates errors, signals existing rows
# ---------------------------------------------------------------------------

async def write_lake_diagnosis_backfill(
    chat_id: str,
    project_id: str,
    final_response: str,
    supabase_client,
    openai_api_key: str,
    run_at: str,
) -> tuple:
    """Backfill-specific writer that reports accurate status rather than swallowing errors.

    Unlike write_lake_diagnosis (which is fire-and-forget and never raises),
    this function lets the caller distinguish three outcomes:

        ("exists",  "")              — row already in lake for this chat_id (upsert no-op)
        ("written", account_label)   — new row written; account_label is name or ID
        raises Exception             — write or extraction failed; caller must catch

    run_at must be a raw Supabase `created_at` string — no parsing applied here.
    """
    if not supabase_client:
        raise RuntimeError("No Supabase client available")

    loop = asyncio.get_event_loop()

    # --- Existence check ---
    # Check by (chat_id, run_at) — the exact upsert conflict target — so that
    # chats with multiple historical final runs (distinct run_at values) each get
    # their own row rather than being skipped after the first one is written.
    exist_resp = await loop.run_in_executor(
        None,
        lambda: (
            supabase_client
            .schema("lake")
            .table("opportunity_diagnoses")
            .select("chat_id")
            .eq("chat_id", chat_id)
            .eq("run_at", run_at)
            .limit(1)
            .execute()
        )
    )
    if exist_resp.data:
        return ("exists", "")

    # --- Fetch all messages for Path A regex ---
    msg_resp = await loop.run_in_executor(
        None,
        lambda: (
            supabase_client
            .table("chat_messages")
            .select("content,role")
            .eq("chat_id", chat_id)
            .execute()
        )
    )
    all_text = "\n".join(r.get("content", "") for r in (msg_resp.data or []))
    user_text = "\n".join(
        r.get("content", "") for r in (msg_resp.data or [])
        if r.get("role") == "user"
    )

    # --- Path A: regex ---
    sf_fields = _extract_sf_fields(all_text, user_text)
    avoma_fields = _extract_avoma_fields(all_text)

    # --- Path B: GPT-4o-mini ---
    narrative_fields = await _extract_narrative_fields(final_response, openai_api_key)

    # --- Build row ---
    row = {
        "chat_id": chat_id,
        "project_id": project_id,
        "run_at": run_at,
        "diagnosis_md": final_response or None,
    }
    row.update(sf_fields)
    row.update(avoma_fields)
    row.update(narrative_fields)

    for key in ("top_risks", "recommendations", "products"):
        if key in row and row[key] is not None and not isinstance(row[key], str):
            row[key] = json.dumps(row[key])

    # --- Upsert — will raise on any DB error (not caught here) ---
    await loop.run_in_executor(
        None,
        lambda: (
            supabase_client
            .schema("lake")
            .table("opportunity_diagnoses")
            .upsert(row, on_conflict="chat_id,run_at")
            .execute()
        )
    )

    account_label = sf_fields.get("account_name") or sf_fields.get("account_id") or "unknown"
    return ("written", account_label)


# ---------------------------------------------------------------------------
# Context injection helpers
# ---------------------------------------------------------------------------

async def fetch_prior_diagnoses(
    account_id: str,
    supabase_client,
    limit: int = 3,
) -> str:
    """Query the lake for prior diagnosis runs on this account.

    Returns a compact markdown block, or empty string on no data / any error.
    """
    if not supabase_client or not account_id:
        return ""
    try:
        loop = asyncio.get_event_loop()
        rows = await loop.run_in_executor(
            None,
            lambda: (
                supabase_client
                .schema("lake")
                .table("opportunity_diagnoses")
                .select(
                    "run_at,account_name,opportunity_name,stage,amount,"
                    "momentum_verdict,health_rating,top_risks"
                )
                .eq("account_id", account_id)
                .order("run_at", desc=True)
                .limit(limit)
                .execute()
            )
        )
        data = rows.data or []
        if not data:
            return ""

        # New per-row format (frontend hand-back: explicit per-run date headers
        # + relative-time hints prevent the LLM collapsing dates into one).
        now = datetime.now(timezone.utc)
        lines = [
            f"The following diagnoses have been run on this account previously, "
            f"ordered most recent first ({len(data)} found):",
            "",
        ]
        for i, row in enumerate(data, start=1):
            run_iso = row.get("run_at") or ""
            ts_header = _format_run_header_ts(run_iso)
            rel = _relative_time(run_iso, now=now)
            rel_suffix = f" ({rel})" if rel else ""
            opp = row.get("opportunity_name") or "—"
            stage = row.get("stage") or "—"
            amount = row.get("amount")
            amount_str = f"${amount:,.0f}" if amount else "—"
            verdict = row.get("momentum_verdict") or "—"
            health = row.get("health_rating") or "—"

            lines.append(f"### Run {i} — {ts_header}{rel_suffix}")
            lines.append(f"- Opportunity: {opp} | Stage: {stage} | Amount: {amount_str}")
            lines.append(f"- Momentum: **{verdict}**")
            lines.append(f"- Health: **{health}**")

            risks_raw = row.get("top_risks")
            if risks_raw:
                try:
                    risks = json.loads(risks_raw) if isinstance(risks_raw, str) else risks_raw
                    risk_lines = []
                    for r in (risks or [])[:3]:
                        if isinstance(r, dict):
                            title = r.get("title", "")
                            signal = r.get("signal", "")
                            risk_lines.append(
                                f"  - {title}: {signal}" if signal else f"  - {title}"
                            )
                    if risk_lines:
                        lines.append("- Top risks:")
                        lines.extend(risk_lines)
                except Exception:
                    pass
            lines.append("")

        # Usage directive (frontend round-3 hand-back): without this footer the
        # LLM treats the injected block as silent background and produces fresh
        # diagnoses as if no priors existed.  Footer placed last so it's the
        # most-recent thing in the model's working memory before it generates,
        # and separated by a horizontal rule so the model reads it as
        # authoritative meta-instruction rather than data belonging to a run.
        lines.append("---")
        lines.append("")
        lines.append(
            "**How to use this context:** When producing your diagnosis below, "
            "briefly reference at least one prior run by date or relative time "
            "(e.g. \"previously assessed 11 days ago as drifting/low\"), and "
            "state whether the current momentum represents continuation, "
            "deterioration, or improvement vs the prior trajectory. If the "
            "user is just asking a question rather than requesting a fresh "
            "diagnosis, summarise from this context without re-running tools."
        )

        return "\n".join(lines).strip()

    except Exception as e:
        # Table may not exist yet — log one line, return empty (no-op injection)
        print(f"[LAKE] fetch_prior_diagnoses silently skipped: {e}", flush=True)
        return ""


# Structured columns returned by read_diagnoses (machine-readable, no markdown
# reformatting). diagnosis_md is the full free-text diagnosis the run produced.
_DIAG_COLS = (
    "run_at,account_id,account_name,opportunity_id,opportunity_name,stage,amount,"
    "momentum_verdict,health_rating,top_risks,recommendations,diagnosis_md"
)


def _coerce_json_list(value):
    """top_risks / recommendations may be stored as a JSON string or a list."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def read_diagnoses(supabase_client, *, account_id: Optional[str] = None,
                   opportunity_id: Optional[str] = None,
                   limit: int = 20, offset: int = 0) -> dict:
    """Read-only structured diagnosis history from lake.opportunity_diagnoses.

    Single source of truth for both the /api/lake/diagnoses endpoint and the
    first-class MCP wrapper. Sync (caller wraps in run_in_executor). Filter by
    account_id and/or opportunity_id; newest first; limit+1 fetched so the
    caller gets a reliable has_more / next_offset.

    Returns {count, diagnoses:[...], has_more, next_offset}.
    """
    lim = max(1, min(int(limit or 20), 50))
    off = max(0, int(offset or 0))
    qy = (supabase_client
          .schema("lake")
          .table("opportunity_diagnoses")
          .select(_DIAG_COLS))
    if account_id:
        qy = qy.eq("account_id", account_id)
    if opportunity_id:
        qy = qy.eq("opportunity_id", opportunity_id)
    res = qy.order("run_at", desc=True).range(off, off + lim).execute()
    rows = res.data or []
    has_more = len(rows) > lim
    rows = rows[:lim]
    diagnoses = [{
        "run_at":           row.get("run_at"),
        "account_id":       row.get("account_id"),
        "account_name":     row.get("account_name"),
        "opportunity_id":   row.get("opportunity_id"),
        "opportunity_name": row.get("opportunity_name"),
        "stage":            row.get("stage"),
        "amount":           row.get("amount"),
        "momentum_verdict": row.get("momentum_verdict"),
        "health_rating":    row.get("health_rating"),
        "top_risks":        _coerce_json_list(row.get("top_risks")),
        "recommendations":  _coerce_json_list(row.get("recommendations")),
        "diagnosis_md":     row.get("diagnosis_md"),
    } for row in rows]
    return {"count": len(diagnoses), "diagnoses": diagnoses,
            "has_more": has_more, "next_offset": (off + lim) if has_more else None}


def extract_account_id_from_messages(messages: list) -> Optional[str]:
    """Scan the last user message for a Salesforce Account ID (001-prefix).

    Returns the matched ID or None.
    Logs a clear message when no ID is found so the gap is visible.
    """
    if not messages:
        return None

    # Scan last user message first, then walk backwards
    for msg in reversed(messages):
        role = ""
        content = ""
        if hasattr(msg, "role"):
            role = msg.role
            content = str(msg.content or "")
        elif isinstance(msg, dict):
            role = msg.get("role", "")
            raw = msg.get("content", "")
            content = raw if isinstance(raw, str) else json.dumps(raw)
        else:
            continue

        if role != "user":
            continue

        m = _SF_ACCOUNT_ID_RE.search(content)
        if m:
            return m.group(0)

    print("[LAKE] no account ID (001…) found in user messages; will try name fallback", flush=True)
    return None


# Stopwords used to filter candidate account-name phrases in the fallback
_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "of", "in", "on", "at", "to", "for",
    "is", "are", "was", "be", "been", "being", "do", "does", "did",
    "have", "has", "had", "will", "would", "could", "should", "may",
    "can", "with", "from", "by", "as", "that", "this", "these", "those",
    "it", "its", "we", "our", "you", "your", "they", "their", "i", "my",
    "please", "run", "start", "begin", "execute", "perform", "analyze",
    "analysis", "opportunity", "diagnosis", "account", "salesforce",
    "using", "using", "show", "give", "get", "tell", "find", "look",
    "help", "me", "us", "report", "all", "any", "some", "about", "into",
})


def _best_candidate_phrase(text: str) -> Optional[str]:
    """Extract the most token-rich non-stopword phrase from user message text.

    Strategy: collect all capitalised multi-word runs (e.g. 'Acme Corp Labs'),
    score by token count, return the longest one that isn't all-stopword.
    """
    # Match runs of 1–5 capitalised or mixed-case tokens
    candidates = re.findall(r'\b[A-Z][A-Za-z0-9&\'-]*(?:\s+[A-Z][A-Za-z0-9&\'-]*){0,4}\b', text)
    best: Optional[str] = None
    best_score = 0
    for c in candidates:
        words = c.split()
        non_stop = [w for w in words if w.lower() not in _STOPWORDS]
        score = len(non_stop)
        if score > best_score and len(c) > 3:
            best_score = score
            best = c
    return best


async def _sf_lookup_account_id(phrase: str) -> Optional[str]:
    """Query Salesforce for Account WHERE Name LIKE '%phrase%'.

    Returns the single matching Account ID, or None on 0 / >1 matches or any error.
    """
    try:
        import os as _os
        sf_user = _os.environ.get("SF_USERNAME", "")
        sf_pass = _os.environ.get("SF_PASSWORD", "")
        sf_token = _os.environ.get("SF_SECURITY_TOKEN", "")
        sf_domain = _os.environ.get("SF_DOMAIN", "login")
        if not sf_user or not sf_pass:
            return None

        from simple_salesforce import Salesforce as _SF
        loop = asyncio.get_event_loop()
        sf = await loop.run_in_executor(
            None,
            lambda: _SF(username=sf_user, password=sf_pass,
                        security_token=sf_token, domain=sf_domain)
        )
        escaped = phrase.replace("'", "\\'")
        query = f"SELECT Id, Name FROM Account WHERE Name LIKE '%{escaped}%' LIMIT 5"
        result = await loop.run_in_executor(None, lambda: sf.query(query))
        records = result.get("records", [])
        if len(records) == 1:
            account_id = records[0]["Id"]
            account_name = records[0]["Name"]
            print(
                f"[LAKE] SF name fallback matched 1 account: '{account_name}' → {account_id}",
                flush=True,
            )
            return account_id
        print(
            f"[LAKE] SF name fallback for '{phrase}' returned {len(records)} matches"
            f" — ambiguous, skipping prior-diagnosis context",
            flush=True,
        )
        return None
    except Exception as e:
        print(f"[LAKE] SF name fallback error: {e}", flush=True)
        return None


async def inject_lake_context(
    request_messages: list,
    project_id: Optional[str],
    system_prompt: Optional[str],
    supabase_client,
) -> str:
    """Prepend prior diagnosis context to system_prompt for OD projects.

    Returns the (possibly augmented) system_prompt string.
    Always safe to call — returns system_prompt unchanged on any error.

    Account ID resolution:
      1. Primary: regex scan of last user message for 001-prefix SF Account ID.
      2. Fallback: extract candidate account name phrase → SOQL LIKE query →
         use only when exactly one result (ambiguous = skip + log).
    """
    # Instrumentation contract (frontend team requirement): on EVERY return path,
    # emit all four lake-inject lines so log slices always tell a complete story.
    od_match = bool(project_id) and project_id in OD_PROJECT_IDS
    account_id: Optional[str] = None
    prior_runs_fetched = 0
    context_injected_len = 0

    def _emit(reason: str = "") -> None:
        suffix = f" ({reason})" if reason else ""
        print(f"[lake-inject] project_id={project_id} matched={od_match}", flush=True)
        print(f"[lake-inject] account_id_extracted={account_id}", flush=True)
        print(f"[lake-inject] prior_runs_fetched={prior_runs_fetched}", flush=True)
        print(f"[lake-inject] context_injected_len={context_injected_len}{suffix}", flush=True)

    if not od_match:
        _emit("not an OD project")
        return system_prompt or ""

    try:
        # Collect the last user message content for both primary and fallback
        last_user_content = ""
        for msg in reversed(request_messages):
            role = ""
            content = ""
            if hasattr(msg, "role"):
                role = msg.role
                content = str(msg.content or "")
            elif isinstance(msg, dict):
                role = msg.get("role", "")
                raw = msg.get("content", "")
                content = raw if isinstance(raw, str) else json.dumps(raw)
            if role == "user":
                last_user_content = content
                break

        account_id = extract_account_id_from_messages(request_messages)

        if not account_id and last_user_content:
            phrase = _best_candidate_phrase(last_user_content)
            if phrase:
                print(f"[LAKE] Trying SF name fallback with phrase: '{phrase}'", flush=True)
                account_id = await _sf_lookup_account_id(phrase)
            else:
                print("[LAKE] no account ID (001…) found and no candidate phrase extracted, skipping prior-diagnosis context", flush=True)

        if not account_id:
            _emit("no account_id")
            return system_prompt or ""

        prior = await fetch_prior_diagnoses(account_id, supabase_client)
        # fetch_prior_diagnoses returns a formatted block; each row begins with
        # "**YYYY-MM-DD** — ".  Anchored multiline regex resists incidental
        # "** — " substrings appearing elsewhere in the block (e.g., risk lines).
        prior_runs_fetched = len(_PRIOR_ROW_RE.findall(prior)) if prior else 0

        if not prior:
            _emit("no prior runs")
            return system_prompt or ""

        injected = (system_prompt or "") + "\n\n## PRIOR DIAGNOSIS CONTEXT\n\n" + prior
        context_injected_len = len(injected) - len(system_prompt or "")
        _emit()
        print(
            f"[LAKE] Injected prior diagnosis context for account={account_id} "
            f"({len(prior)} chars)",
            flush=True,
        )
        return injected

    except Exception as e:
        print(f"[LAKE] inject_lake_context failed silently: {e}", flush=True)
        _emit(f"exception: {type(e).__name__}")
        return system_prompt or ""
