"""deal_engine_sweep.py — the per-opportunity AI sweep that POPULATES the book.

This is the write path behind the Deal Intelligence Engine. For each open
opportunity owned by a member of the configured team it runs a Salesforce + Avoma
deep agent (read-only) that emits ONE evidence-anchored canonical record (per
prompts/deal_engine_sweep_system_prompt.md), then upserts it via
deal_engine_store. The Deals / Espresso / Matcha views derive deterministically
from those records, so once the sweep has run the four tabs fill themselves.

Design (ported from the reference worker/sweep.ts, adapted to this app):
- Discovery: one SOQL query (via the live salesforce MCP `soql` tool) for open
  opps owned by the team (by Owner.Name, matching the env team config). No
  hardcoded opp list; an explicit opp_ids list may be supplied for cheap reruns.
- Per-opp agent: a scoped deep agent (salesforce + avoma MCP tools only),
  OpenAI by default (Anthropic 404s in this env). Reuses the JSON-extraction
  helpers from opportunity_analyzer.
- Bounded parallelism: asyncio.Semaphore(SWEEP_CONCURRENCY).
- Run tracking: process-local _RUN_STATE for the status endpoint. One sweep at a
  time (guarded). Records are upserted as each opp completes, so a restart loses
  only the in-flight opps; a rerun refreshes the book.

Cost note: each opp is a full multi-tool agent run. A full book sweep is heavy
(tokens + minutes); it is gated behind an explicit POST and bounded concurrency.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from deepagents import create_deep_agent
from deepagents_patches import disable_write_todos

import deal_engine_store as store
import opportunity_analyzer as _oa  # reuse _extract_json / _final_text
import deal_trigger_log as _trigger_log
import deal_hard_refresh_log as _hard_refresh_log
import deal_engine_validation as _val
import deal_engine_pulse as _pulse
import sweep_queue as _queue

disable_write_todos()

_ALLOWED_SERVERS = {"salesforce", "avoma"}
_PROMPT_PATH = Path(__file__).parent / "prompts" / "deal_engine_sweep_system_prompt.md"

_agent_lock = asyncio.Lock()
_cached_agent = None
_cached_tool_names: list[str] = []

# ---- run state (process-local; one sweep at a time) ----
_state_lock = asyncio.Lock()
_RUN_STATE: dict[str, Any] = {"status": "idle"}
_run_task: Optional[asyncio.Task] = None


def _today() -> str:
    return date.today().isoformat()


def _now() -> str:
    """UTC ISO timestamp (with time) for live progress tracking."""
    return datetime.now(timezone.utc).isoformat()


def _within_days(date_str: Optional[str], n: int) -> bool:
    """True if `date_str` (a Salesforce date/datetime ISO string) falls within the
    last `n` days (0 <= today - date <= n). Future dates and None/unparseable
    input return False."""
    if not date_str or not isinstance(date_str, str):
        return False
    try:
        d = datetime.fromisoformat(date_str.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            d = date.fromisoformat(date_str[:10])
        except ValueError:
            return False
    delta = (date.today() - d).days
    return 0 <= delta <= n


def _parse_sf_dt(value: Optional[str]) -> Optional[datetime]:
    """Parse a Salesforce datetime/date string to a timezone-aware datetime, or
    None. Used for watermark comparison so we never rely on brittle string
    ordering across offset formats (`+0000` vs `+00:00`, fractional seconds)."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = datetime.fromisoformat(value[:10])
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _load_prompt() -> str:
    if not _PROMPT_PATH.exists():
        raise FileNotFoundError(f"sweep prompt missing: {_PROMPT_PATH}")
    return _PROMPT_PATH.read_text()


def _prompt_fingerprint(text: str) -> str:
    """A short, loggable identity for a prompt: its first line + a content hash, so
    a restart/reset can confirm WHICH prompt version is live (the agent is cached,
    so editing the file alone does not take effect until rebuild)."""
    first = (text.splitlines()[0].strip() if text else "")[:80]
    h = hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:12]
    return f"sha256={h} first_line={first!r}"


_FRONTIER_DEFAULT = "anthropic:claude-sonnet-4-5"
# Substrings that mark a small/cheap model. The sweep is a deep, multi-tool
# reconstruction job: a mini/haiku/flash/nano model produces shallow, low-recall
# records (and OpenAI mini variants also hang when the MCP tool schemas are
# bound — see _build_model). If one is configured we refuse it and fall back to
# the frontier default, so a stray env can never quietly degrade the whole book.
_MINI_MODEL_MARKERS = (
    "mini", "haiku", "nano", "flash-lite", "flash-8b", "gpt-3.5", "instant",
    "small", "lite",
)


def _selected_model_name() -> str:
    """The model string the sweep agent will use (for logging + dispatch).

    Guarded to a frontier model: a configured mini/haiku/etc. is rejected and
    replaced by the frontier default, so the single source of truth used by
    _build_model, run-start logging, and the audit log can never be a small
    model."""
    configured = (
        os.getenv("DEAL_ENGINE_SWEEP_MODEL")
        or os.getenv("OPP_ANALYZER_MODEL")
        or _FRONTIER_DEFAULT
    )
    low = configured.lower()
    if any(m in low for m in _MINI_MODEL_MARKERS):
        print(
            f"[DEAL-SWEEP] configured model {configured!r} is a small/mini model; "
            f"refusing it and using frontier default {_FRONTIER_DEFAULT!r}",
            flush=True,
        )
        return _FRONTIER_DEFAULT
    return configured


def _sum_usage(messages: list) -> dict:
    """Aggregate token usage across an agent run's AI messages.

    Anthropic reports `input_tokens` as NEW uncached input only, with cache
    creation/read split out under `input_token_details`. We keep that split for
    accurate cost, and also surface a grand-total input for display."""
    inp = out = cc = cr = tot = 0
    seen = False
    for m in messages or []:
        u = getattr(m, "usage_metadata", None)
        if not isinstance(u, dict):
            continue
        seen = True
        i = u.get("input_tokens", 0) or 0
        o = u.get("output_tokens", 0) or 0
        det = u.get("input_token_details") or {}
        c_create = (det.get("cache_creation") or 0) if isinstance(det, dict) else 0
        c_read = (det.get("cache_read") or 0) if isinstance(det, dict) else 0
        inp += i
        out += o
        cc += c_create
        cr += c_read
        tot += u.get("total_tokens", 0) or (i + c_create + c_read + o)
    return {"uncached_input": inp, "output": out, "cache_creation": cc,
            "cache_read": cr, "total": tot, "seen": seen}


def _build_model():
    """Model for the tool-using sweep agent.

    Default: reuse opportunity_analyzer's proven model path (Anthropic
    claude-sonnet with prompt caching). That analyzer runs this exact
    salesforce+avoma toolset unattended in the webhook pipeline and works in
    this env. OpenAI gpt-4o, by contrast, HANGS at the first model call when the
    27 MCP tool schemas are bound (it is fine without tools — see the chat
    endpoint), so it is not a safe default for a tool-using agent here.

    Selection order is deliberately tool-safe:
      1. DEAL_ENGINE_SWEEP_MODEL (explicit per-feature override)
      2. OPP_ANALYZER_MODEL (shares the proven analyzer model)
      3. anthropic:claude-sonnet-4-5 (hard pin)
    We intentionally do NOT inherit the generic MODEL env: a deployment that
    pins MODEL to an OpenAI model would otherwise silently re-enter the hang
    path this function exists to avoid."""
    selected = _selected_model_name()
    if selected.startswith("anthropic:"):
        from anthropic_cache import CachedChatAnthropic
        return CachedChatAnthropic(
            model_name=selected.split(":", 1)[1],
            api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
            max_retries=int(os.getenv("ANTHROPIC_MAX_RETRIES", "2")),
            timeout=int(os.getenv("LLM_REQUEST_TIMEOUT_S", "180")),
            max_tokens=int(os.getenv("DEAL_SWEEP_MAX_TOKENS", "32000")),
            stop=None,
        )
    from langchain.chat_models import init_chat_model
    return init_chat_model(selected)


def reset():
    """Drop the cached agent (call after MCP reload)."""
    global _cached_agent, _cached_tool_names
    _cached_agent = None
    _cached_tool_names = []
    try:
        print(f"[DEAL-SWEEP] agent cache reset; on-disk prompt: "
              f"{_prompt_fingerprint(_load_prompt())}", flush=True)
    except Exception as _e:  # noqa: BLE001
        print(f"[DEAL-SWEEP] agent cache reset (prompt read failed: {_e})", flush=True)


async def _get_agent(agent_manager):
    global _cached_agent, _cached_tool_names
    async with _agent_lock:
        if _cached_agent is not None:
            return _cached_agent
        tools = _oa._collect_scoped_tools(agent_manager)
        if not tools:
            raise RuntimeError(
                "deal_engine_sweep: no salesforce/avoma tools loaded yet "
                "(agent_manager._cached_mcp_tools_by_server empty)"
            )
        _cached_tool_names = [t.name for t in tools]
        middleware = []
        if os.getenv("CONTEXT_TRIM_ENABLED", "true").lower() in ("1", "true", "yes"):
            try:
                from agent_checklist.context_trim_middleware import ContextTrimMiddleware
                # The sweep ingests verbatim Salesforce field reads + full Avoma
                # notes/transcripts (both bypass the prose summariser), so it needs
                # a higher trim budget and a larger keep-recent window than the
                # chat agent — otherwise the top matched calls get trimmed to
                # placeholders before synthesis. Use sweep-specific env knobs that
                # fall back to the shared ones, then to raised sweep defaults.
                _trim_threshold = int(
                    os.getenv("DEAL_SWEEP_CONTEXT_TRIM_THRESHOLD_TOKENS")
                    or os.getenv("CONTEXT_TRIM_THRESHOLD_TOKENS", "120000"))
                _trim_keep = int(
                    os.getenv("DEAL_SWEEP_CONTEXT_TRIM_KEEP_RECENT_MESSAGES")
                    or os.getenv("CONTEXT_TRIM_KEEP_RECENT_MESSAGES", "14"))
                middleware.append(
                    ContextTrimMiddleware(
                        threshold_tokens=_trim_threshold,
                        keep_recent_messages=_trim_keep,
                        placeholder_max_chars=int(os.getenv("CONTEXT_TRIM_PLACEHOLDER_MAX_CHARS", "400")),
                    )
                )
                print(f"[DEAL-SWEEP] context-trim threshold={_trim_threshold} "
                      f"keep_recent={_trim_keep}", flush=True)
            except Exception as _e:  # noqa: BLE001
                print(f"[DEAL-SWEEP] context-trim middleware unavailable: {_e}", flush=True)
        print(
            f"[DEAL-SWEEP] building agent with {len(tools)} tools "
            f"(servers: {sorted(_ALLOWED_SERVERS)}, middleware: {len(middleware)})",
            flush=True,
        )
        _prompt_text = _load_prompt()
        print(f"[DEAL-SWEEP] system prompt loaded: "
              f"{_prompt_fingerprint(_prompt_text)}", flush=True)
        _cached_agent = create_deep_agent(
            tools=tools,
            system_prompt=_prompt_text,
            subagents=[],
            model=_build_model(),
            middleware=middleware,
            debug=False,
        )
        return _cached_agent


def _find_tool(agent_manager, server: str, name: str):
    by_server = getattr(agent_manager, "_cached_mcp_tools_by_server", {}) or {}
    for t in by_server.get(server, []) or []:
        if t.name == name or t.name.endswith(f".{name}"):
            return t
    return None


_coerce_debugged = False


def _parse_maybe(s: str):
    s = s.strip()
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        pass
    try:
        import ast
        return ast.literal_eval(s)  # MCP adapters sometimes return a Python repr
    except Exception:
        return None


def _coerce_rows(raw: Any) -> list[dict]:
    """MCP tool output may be a list, a dict, a JSON/Python-repr string, or a
    list of content blocks. Normalise to a list of record dicts; surface query
    errors as an exception."""
    global _coerce_debugged
    if not _coerce_debugged:
        _coerce_debugged = True
        print(f"[DEAL-SWEEP] soql raw type={type(raw).__name__} "
              f"snippet={str(raw)[:160]!r}", flush=True)

    # LangChain tool results can arrive as (content, artifact) tuples.
    if isinstance(raw, tuple) and raw:
        raw = raw[0]
    # Or as a list of content blocks [{type:text,text:...}].
    if isinstance(raw, list) and raw and isinstance(raw[0], dict) and "text" in raw[0] \
            and set(raw[0].keys()) <= {"type", "text", "annotations"}:
        joined = "".join(b.get("text", "") for b in raw if isinstance(b, dict))
        raw = _parse_maybe(joined)

    if isinstance(raw, str):
        raw = _parse_maybe(raw)

    if isinstance(raw, dict):
        if raw.get("error"):
            raise RuntimeError(f"salesforce soql error: {raw['error']}")
        for key in ("records", "result", "data"):
            if isinstance(raw.get(key), list):
                return raw[key]
        return [raw]
    return raw if isinstance(raw, list) else []


def _sf_name(o: dict, *path: str) -> Optional[str]:
    cur: Any = o
    for p in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur if isinstance(cur, (str, int, float)) else None


def _sql_str(v: str) -> str:
    return v.replace("\\", "\\\\").replace("'", r"\'")


async def _soql(agent_manager, query: str) -> list[dict]:
    """Run a SOQL query for discovery.

    Discovery is pure, deterministic SOQL, so we hit Salesforce directly via the
    same cached connection the MCP server uses (salesforce_mcp_server.sf_conn).
    We deliberately bypass the agent's MCP `soql` tool here: that tool is wrapped
    with a tool-output summariser which rewrites large results into prose, making
    them unparseable for record extraction. The per-opp AI analysis still goes
    through the deep agent as designed.
    """
    from salesforce_mcp_server import sf_conn

    def _run() -> list[dict]:
        return sf_conn().query_all(query).get("records", []) or []

    return await asyncio.to_thread(_run)


def _combine_competitors(picklist, other) -> Optional[str]:
    """Merge the Competitors__c multipicklist (semicolon-delimited) with the
    free-text Others_Competitors_Please_specify__c overflow into one string."""
    parts = []
    if picklist and str(picklist).strip():
        parts.append(str(picklist).strip())
    if other and str(other).strip():
        parts.append(str(other).strip())
    return ";".join(parts) if parts else None


def _map_opps(rows: list[dict]) -> list[dict]:
    out = []
    for o in rows:
        oid = o.get("Id")
        if not oid:
            continue
        out.append({
            "id": oid,
            "name": o.get("Name"),
            "account": _sf_name(o, "Account", "Name"),
            "owner_name": _sf_name(o, "Owner", "Name"),
            "owner_id": o.get("OwnerId"),
            "manager_name": _sf_name(o, "Owner", "Manager", "Name"),
            "manager_id": _sf_name(o, "Owner", "ManagerId"),
            "stage": o.get("StageName"),
            "forecast_category": o.get("ForecastCategoryName"),
            "amount": o.get("Amount"),
            "close_date": o.get("CloseDate"),
            "next_step": o.get("Next_Step__c"),
            "ais_score": o.get("AIS_Score__c"),
            "ais_status": o.get("AIS_Status__c"),
            "ais_why": o.get("AIS_Why__c"),
            "products": o.get("Products__c"),
            "competitor": _combine_competitors(
                o.get("Competitors__c"), o.get("Others_Competitors_Please_specify__c")),
            "last_modified": o.get("LastModifiedDate"),
            "created": o.get("CreatedDate"),
            # Deterministic SF date facts the server owns (date-only for the two
            # datetime fields so they compare cleanly with the model's emitted
            # YYYY-MM-DD). LastActivityDate / Qualified_Submission_Date__c are
            # already date-typed in Salesforce.
            "created_date": ((o.get("CreatedDate") or "")[:10] or None),
            "last_modified_date": ((o.get("LastModifiedDate") or "")[:10] or None),
            "last_activity_date": o.get("LastActivityDate"),
            "qualified_date": o.get("Qualified_Submission_Date__c"),
        })
    return out


# Part 1: the ONE authoritative Salesforce field list every opp-snapshot SOQL
# uses. Keeping the three readers (single-opp hydration, book discovery, and
# id-list enrichment) on a single constant means the server-owned hard.* override
# and the fabrication gate always validate against the exact same ground-truth
# columns. ONLY org-verified fields belong here — an unverified field 400s the
# query and breaks the WHOLE sweep, so do not add a column without confirming it
# exists in the org first (Task spec Part 1 ruling F).
_OPP_SELECT_FIELDS = (
    "Id, Name, Account.Name, Owner.Name, OwnerId, "
    "Owner.ManagerId, Owner.Manager.Name, StageName, ForecastCategoryName, "
    "Amount, CloseDate, "
    "Next_Step__c, AIS_Score__c, AIS_Status__c, AIS_Why__c, Products__c, Competitors__c, "
    "Others_Competitors_Please_specify__c, LastModifiedDate, CreatedDate, "
    "LastActivityDate, Qualified_Submission_Date__c"
)


async def _authoritative_opp(agent_manager, opp_id: str) -> dict:
    """The authoritative per-opp Salesforce snapshot (core mechanics + the deal
    owner's manager) via direct SOQL, mapped to the `_map_opps` shape.

    Every entry path funnels through analyze_one, but several pass only a THIN opp
    dict (the worker queue carries just id/account/owner_name/name). Without this
    hydration the server-owned hard.* override below would be a near no-op on the
    main production path, so the model's stage/amount/manager could survive. We
    fetch the real values here so the override always has ground truth. Best-effort:
    returns {} on any failure and the caller falls back to whatever it was given."""
    if not opp_id:
        return {}
    q = (f"SELECT {_OPP_SELECT_FIELDS} "
         f"FROM Opportunity WHERE Id = '{_sql_str(opp_id)}' LIMIT 1")
    try:
        mapped = _map_opps(await _soql(agent_manager, q))
    except Exception as e:  # noqa: BLE001 — never block the sweep on this read
        print(f"[DEAL-SWEEP] authoritative-opp read failed opp={opp_id}: "
              f"{type(e).__name__}: {e}", flush=True)
        return {}
    return mapped[0] if mapped else {}


async def discover_opps(
    agent_manager,
    owner: Optional[str] = None,
    limit: int = 500,
) -> list[dict]:
    """Open opps to sweep. Returns [{id, name, account, owner_name, owner_id}].

    - owner given: that one rep's open opps (by Owner.Name) — used for reruns.
    - else: the VP's whole team. We resolve the team the robust way the
      reference worker does, from Salesforce (User.Manager.Name = VP), so the
      book matches reality even when the env RSD names are placeholders. If VP
      resolution yields nobody, fall back to the configured RSD names.
    """
    base = f"SELECT {_OPP_SELECT_FIELDS} FROM Opportunity WHERE "
    tail = f" AND IsClosed = false ORDER BY Amount DESC NULLS LAST LIMIT {int(limit)}"

    if owner:
        q = f"{base}Owner.Name = '{_sql_str(owner)}'{tail}"
        return _map_opps(await _soql(agent_manager, q))

    # Team/book path: the MASE report is the single source of truth for
    # membership. Take its ids and enrich them via SOQL (so the hard fields,
    # incl. LastModifiedDate for the watermark, match the rest of the pipeline).
    # The VP/owner SOQL below is kept ONLY as a fallback so the book never
    # empties if the report read fails.
    import deal_engine_report as report
    mem = await asyncio.to_thread(report.fetch_report_membership)
    if mem.get("ok") and mem.get("ids18"):
        return await _enrich_opp_ids(agent_manager, mem["ids18"][:int(limit)])
    print(f"[DEAL-SWEEP] report membership unavailable ({mem.get('error')}); "
          f"falling back to VP/owner SOQL discovery", flush=True)

    team = store.get_team()
    vp = (team.get("vp") or "").strip()
    owner_ids: list[str] = []
    if vp:
        reps = await _soql(
            agent_manager,
            f"SELECT Id, Name FROM User WHERE Manager.Name = '{_sql_str(vp)}' AND IsActive = true",
        )
        owner_ids = [r["Id"] for r in reps if isinstance(r, dict) and r.get("Id")]

    if owner_ids:
        ids = ",".join("'" + i + "'" for i in owner_ids)
        q = f"{base}OwnerId IN ({ids}){tail}"
        return _map_opps(await _soql(agent_manager, q))

    # Fallback: configured RSD names.
    names = [n for n in (team.get("rsds") or []) if n]
    if not names:
        return []
    quoted = ",".join("'" + _sql_str(n) + "'" for n in names)
    q = f"{base}Owner.Name IN ({quoted}){tail}"
    return _map_opps(await _soql(agent_manager, q))


async def _enrich_opp_ids(agent_manager, opp_ids: list[str]) -> list[dict]:
    """Cheap, AI-free label lookup for an explicit opp_id list.

    One chunked SOQL per ~200 ids fetches Id/Name/Account.Name/Owner.Name so the
    dashboard can show account + owner immediately (while queued), instead of bare
    ids. Falls back to bare dicts for any id the lookup can't resolve."""
    found: dict[str, dict] = {}
    CHUNK = 200
    for i in range(0, len(opp_ids), CHUNK):
        chunk = [c for c in opp_ids[i:i + CHUNK] if c]
        if not chunk:
            continue
        ids = ",".join("'" + _sql_str(c) + "'" for c in chunk)
        q = (f"SELECT {_OPP_SELECT_FIELDS} FROM Opportunity "
             f"WHERE Id IN ({ids})")
        try:
            for o in _map_opps(await _soql(agent_manager, q)):
                # Key on the 15-char prefix: Salesforce returns 18-char ids while
                # report exports are often 15-char. The first 15 chars are identical.
                found[(o["id"] or "")[:15]] = o
        except Exception as e:  # noqa: BLE001 — labels are best-effort
            print(f"[DEAL-SWEEP] enrich chunk failed: {type(e).__name__}: {e}", flush=True)
    out: list[dict] = []
    for oid in opp_ids:
        m = found.get((oid or "")[:15])
        if m:
            # Carry every captured SF field; keep the caller's id form (15/18-char).
            o2 = dict(m)
            o2["id"] = oid
            out.append(o2)
        else:
            out.append({"id": oid, "name": None, "account": None,
                        "owner_name": None, "owner_id": None, "stage": None,
                        "amount": None, "close_date": None, "ais_score": None,
                        "ais_status": None, "ais_why": None, "products": None,
                        "competitor": None})
    return out


def _domain_of(value: Optional[str]) -> Optional[str]:
    """Lowercased registrable host from an email or website, or None.

    "jane@acme.co.uk" -> "acme.co.uk"; "https://www.acme.com/x" -> "acme.com".
    Generic mailbox providers are dropped so they never widen attendee matching
    into unrelated calls."""
    if not value or not isinstance(value, str):
        return None
    v = value.strip().lower()
    if "@" in v:
        v = v.rsplit("@", 1)[-1]
    else:
        v = v.split("//", 1)[-1]
        v = v.split("/", 1)[0]
    if v.startswith("www."):
        v = v[4:]
    v = v.split(":", 1)[0].strip().strip(".")
    if not v or "." not in v:
        return None
    _GENERIC = {
        "gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com",
        "live.com", "icloud.com", "aol.com", "protonmail.com", "me.com",
        "zycus.com",  # our own domain is never a buyer-side signal
    }
    return None if v in _GENERIC else v


async def _buyer_identity(agent_manager, opp_id: str) -> dict:
    """Cheap, AI-free buyer-identity prefetch for Avoma attendee matching.

    Resolves, via direct SOQL (bypassing the agent's summarised tool path), the
    Account name + website, every OpportunityContactRole contact (name / title /
    email), the email/website domains, recent Task contact names, and the opp's
    LastActivityDate. Injected into the agent user message so account+attendee
    Avoma discovery is reliable even when the agent's own contact-role read is
    flaky, and reused server-side to decide whether a calls_read==0 record is
    genuinely dark or just a discovery miss worth retrying. Best-effort: any
    failure returns an empty-but-shaped dict so the sweep still runs."""
    out = {"account_name": None,
           "website": None, "domains": [], "contacts": [], "task_contacts": [],
           "roles_count": 0, "last_activity_date": None}
    sid = _sql_str(opp_id)
    try:
        head = await _soql(
            agent_manager,
            f"SELECT Account.Name, Account.Website, LastActivityDate "
            f"FROM Opportunity WHERE Id = '{sid}'")
        if head:
            h = head[0]
            out["account_name"] = _sf_name(h, "Account", "Name") or out["account_name"]
            out["website"] = _sf_name(h, "Account", "Website")
            out["last_activity_date"] = h.get("LastActivityDate")
    except Exception as e:  # noqa: BLE001 — prefetch is best-effort
        print(f"[DEAL-SWEEP] buyer-identity head failed opp={opp_id}: "
              f"{type(e).__name__}: {e}", flush=True)
    try:
        roles = await _soql(
            agent_manager,
            f"SELECT Contact.Name, Contact.Title, Contact.Email, Role, IsPrimary "
            f"FROM OpportunityContactRole WHERE OpportunityId = '{sid}'")
        for r in roles or []:
            nm = _sf_name(r, "Contact", "Name")
            if not nm:
                continue
            out["contacts"].append({
                "name": nm,
                "title": _sf_name(r, "Contact", "Title"),
                "email": _sf_name(r, "Contact", "Email"),
                "role": r.get("Role"),
            })
        out["roles_count"] = len(out["contacts"])
    except Exception as e:  # noqa: BLE001
        print(f"[DEAL-SWEEP] buyer-identity roles failed opp={opp_id}: "
              f"{type(e).__name__}: {e}", flush=True)
    try:
        tasks = await _soql(
            agent_manager,
            f"SELECT Who.Name FROM Task WHERE WhatId = '{sid}' AND WhoId != null "
            f"ORDER BY ActivityDate DESC NULLS LAST LIMIT 25")
        seen = set()
        for t in tasks or []:
            nm = _sf_name(t, "Who", "Name")
            if nm and nm not in seen:
                seen.add(nm)
                out["task_contacts"].append(nm)
    except Exception as e:  # noqa: BLE001
        print(f"[DEAL-SWEEP] buyer-identity tasks failed opp={opp_id}: "
              f"{type(e).__name__}: {e}", flush=True)
    # Derive the attendee-matching domains: contact emails + account website.
    domains: list[str] = []
    for c in out["contacts"]:
        d = _domain_of(c.get("email"))
        if d and d not in domains:
            domains.append(d)
    wd = _domain_of(out["website"])
    if wd and wd not in domains:
        domains.append(wd)
    out["domains"] = domains
    return out


def _buyer_identity_block(bi: dict) -> str:
    """Render the prefetched buyer identity as a compact instruction block the
    agent uses to drive account+attendee Avoma discovery. Empty string when we
    found nothing (so the prompt's own discovery still governs)."""
    if not bi:
        return ""
    lines: list[str] = []
    if bi.get("domains"):
        lines.append("Buyer email/website domains (match Avoma attendees on these): "
                     + ", ".join(bi["domains"]))
    if bi.get("contacts"):
        people = "; ".join(
            f"{c['name']}"
            + (f" ({c['title']})" if c.get("title") else "")
            + (f" <{c['email']}>" if c.get("email") else "")
            + (f" [{c['role']}]" if c.get("role") else "")
            for c in bi["contacts"][:20])
        lines.append(f"Opportunity contact roles ({bi.get('roles_count', 0)}): {people}")
    if bi.get("task_contacts"):
        lines.append("Recent task contacts: " + ", ".join(bi["task_contacts"][:15]))
    if bi.get("last_activity_date"):
        lines.append(f"Salesforce LastActivityDate: {bi['last_activity_date']}")
    if not lines:
        return ""
    return (
        "\n\nBuyer identity (prefetched from Salesforce — use it to discover Avoma "
        "calls by ACCOUNT + ATTENDEES, not opp_id alone; a call with a Zycus rep "
        "plus any attendee on these domains or names is a buyer call for THIS deal):\n- "
        + "\n- ".join(lines)
    )


def _sweep_facts_block(opp: dict, buyer: dict) -> str:
    """Authoritative Salesforce mechanics, captured live via direct SOQL at sweep
    time (`_enrich_opp_ids`/`_buyer_identity`), rendered as a GROUND-TRUTH block
    for the agent prompt.

    The agent's own MCP `soql` reads are unreliable in this run (they frequently
    fail -> the agent self-reports "Q1 SOQL failure" and falls back to stale
    living-memory packets, emitting wrong close dates / overdue math). These
    fields, by contrast, are the same authoritative snapshot we OVERRIDE `hard.*`
    with after the run, so handing them to the agent up front keeps the free-text
    verdict/date math consistent with the hard facts instead of hallucinated."""
    def _f(v):
        return v if (v is not None and v != "") else "unknown"
    la = buyer.get("last_activity_date") if isinstance(buyer, dict) else None
    lines = [
        f"- StageName: {_f(opp.get('stage'))}",
        f"- ForecastCategory: {_f(opp.get('forecast_category'))}",
        f"- Amount: {_f(opp.get('amount'))}",
        f"- CloseDate: {_f(opp.get('close_date'))}",
        f"- CreatedDate: {_f(opp.get('created'))}",
        f"- LastModifiedDate: {_f(opp.get('last_modified'))}",
        f"- LastActivityDate: {_f(la)}",
        f"- NextStep: {_f(opp.get('next_step'))}",
        f"- Products: {_f(opp.get('products'))}",
        f"- Competitor(s): {_f(opp.get('competitor'))}",
        f"- Owner: {_f(opp.get('owner_name'))}",
        f"- Owner's manager: {_f(opp.get('manager_name'))}",
        f"- Account: {_f(opp.get('account'))}",
    ]
    return (
        "\n\n=== GROUND TRUTH — authoritative Salesforce fields (live, this sweep) ===\n"
        "These values were read directly from Salesforce at sweep time and are "
        "AUTHORITATIVE. Use these EXACT values for ALL stage, amount, forecast, "
        "close-date, age, days-to-close, overdue, and time-in-stage math in your "
        "verdict and analysis. Do NOT recompute or infer any of these from prior "
        "memory, packets, or your own tool reads; if your own SOQL disagrees with "
        "these, THESE win. A field marked 'unknown' is genuinely unread — say so "
        "rather than inventing a value.\n"
        + "\n".join(lines)
        + f"\nToday's date is {_today()}.\n"
    )


MEDDPICC_ELEMENTS = (
    "metrics", "economic_buyer", "decision_criteria", "decision_process",
    "paper_process", "identify_pain", "champion", "competition",
)


def _normalize_meddpicc(new_ai: dict, existing_ai: Optional[dict]) -> None:
    """Normalise ai.meddpicc to the 8 fixed elements, each {status, narrative,
    sources}. When this sweep produced an empty/missing narrative for an element
    but the prior record had a real one, carry the prior element forward — a thin
    or dark read must never blank a previously detailed element (mirrors the
    champion-backfill philosophy in project_into_ai). Mutates new_ai in place."""
    new_md = new_ai.get("meddpicc")
    new_md = new_md if isinstance(new_md, dict) else {}
    prior_md = (existing_ai or {}).get("meddpicc")
    prior_md = prior_md if isinstance(prior_md, dict) else {}
    out: dict = {}
    for el in MEDDPICC_ELEMENTS:
        cur = new_md.get(el)
        cur = cur if isinstance(cur, dict) else {}
        narrative = str(cur.get("narrative") or "").strip()
        if not narrative:
            prior = prior_md.get(el)
            prior = prior if isinstance(prior, dict) else None
            prior_narr = str((prior or {}).get("narrative") or "").strip()
            if prior_narr:
                psrc = (prior or {}).get("sources")
                out[el] = {
                    "status": (prior or {}).get("status") or "partial",
                    "narrative": prior_narr,
                    "sources": psrc if isinstance(psrc, list) else [],
                    "carried_forward": True,
                }
                continue
        status = str(cur.get("status") or "").strip().lower()
        if status not in ("confirmed", "partial", "gap"):
            status = "partial" if narrative else "gap"
        sources = cur.get("sources")
        out[el] = {
            "status": status,
            "narrative": narrative,
            "sources": sources if isinstance(sources, list) else [],
        }
    new_ai["meddpicc"] = out


_ACTIVE_USERS_CACHE: dict = {"names": set(), "ts": 0.0}
_ACTIVE_USERS_TTL_S = int(os.getenv("DEAL_ACTIVE_USERS_TTL_S", "3600"))


async def _active_user_names(agent_manager) -> set:
    """Normalised names of all ACTIVE Salesforce users, cached per-process with a
    TTL. Feeds the fabrication gate's internal-person check (so an "executive
    connect with <rep>" move is verifiable against the real roster) without a
    per-opp query. Best-effort: on a read failure we keep the last good set
    (possibly empty), so the gate degrades to "no roster" rather than blocking."""
    now = time.time()
    cached = _ACTIVE_USERS_CACHE.get("names") or set()
    if cached and (now - float(_ACTIVE_USERS_CACHE.get("ts") or 0)) < _ACTIVE_USERS_TTL_S:
        return cached
    names: set = set()
    try:
        rows = await _soql(agent_manager, "SELECT Name FROM User WHERE IsActive = true")
        for r in rows or []:
            if isinstance(r, dict):
                nm = _val._norm_name(r.get("Name"))
                if nm:
                    names.add(nm)
    except Exception as e:  # noqa: BLE001 — gate degrades gracefully without it
        print(f"[DEAL-SWEEP] active-user roster read failed: "
              f"{type(e).__name__}: {e}", flush=True)
        return cached
    if names:
        _ACTIVE_USERS_CACHE["names"] = names
        _ACTIVE_USERS_CACHE["ts"] = now
        return names
    return cached


def _attendees_of(rec: dict) -> list:
    """The Avoma attendee names the agent echoed in evidence_coverage (top-level);
    the gate uses them to verify buyer names the record asserts."""
    ec = rec.get("evidence_coverage") if isinstance(rec, dict) else None
    att = ec.get("avoma_attendees") if isinstance(ec, dict) else None
    if not isinstance(att, list):
        return []
    return [a for a in att if isinstance(a, str) and a.strip()]


async def analyze_one(
    agent_manager,
    opp: dict,
    *,
    recursion_limit: Optional[int] = None,
    timeout_s: Optional[int] = None,
    source: str = "sweep",
) -> dict:
    """Run the sweep agent for one opp and upsert the resulting canonical record.
    Returns {opp_id, status, duration_ms, error}. Every run (success OR failure)
    is logged to the deal_trigger_runs audit table, tagged with `source`
    (sweep | manual | salesforce_trigger)."""
    if recursion_limit is None:
        recursion_limit = int(os.getenv("DEAL_SWEEP_RECURSION_LIMIT", "80"))
    if timeout_s is None:
        timeout_s = int(os.getenv("DEAL_SWEEP_TIMEOUT_S", "900"))
    opp_id = opp["id"]
    model_name = _selected_model_name()
    usage = {"uncached_input": 0, "output": 0, "cache_creation": 0,
             "cache_read": 0, "total": 0, "seen": False}
    t0 = time.time()
    result = {"opp_id": opp_id, "account": opp.get("account"),
              "owner_name": opp.get("owner_name"), "status": "pending",
              "duration_ms": 0, "error": None, "validation_violations": 0,
              "failed_validation": False,
              "thin": False, "thin_reason": None, "calls_read": None}
    _skip_token = None
    print(f"[DEAL-SWEEP] analyze_one START opp={opp_id}", flush=True)
    try:
        agent = await _get_agent(agent_manager)
        print(f"[DEAL-SWEEP] agent ready, invoking opp={opp_id} "
              f"(tools={len(_cached_tool_names)})", flush=True)
        # Per-deal living memory: load the prior record so we can (a) tell the
        # agent which insight topics are already on record (so it reuses the same
        # wording and our stable keys line up) and (b) merge this sweep into the
        # durable packets afterwards instead of overwriting.
        import deal_engine_packets as packets_mod
        existing_record = {}
        try:
            existing_record = await asyncio.get_running_loop().run_in_executor(
                None, store.get_record, opp_id) or {}
        except Exception as _e:  # noqa: BLE001
            print(f"[DEAL-SWEEP] prior record load failed opp={opp_id}: {_e}", flush=True)
        existing_packets = existing_record.get("packets") or []
        topics_block = packets_mod.known_topics_block(existing_packets)
        # Buyer-identity prefetch (account + contact roles + domains + recent task
        # contacts + LastActivityDate), via direct SOQL. Drives reliable
        # account+attendee Avoma discovery and lets us tell a genuinely-dark deal
        # from a discovery miss (calls_read==0 but stakeholders/recent activity
        # exist -> retry). Best-effort; never blocks the sweep.
        try:
            buyer = await _buyer_identity(agent_manager, opp_id)
        except Exception as _e:  # noqa: BLE001
            print(f"[DEAL-SWEEP] buyer-identity prefetch failed opp={opp_id}: "
                  f"{type(_e).__name__}: {_e}", flush=True)
            buyer = {}
        identity_block = _buyer_identity_block(buyer)
        # Authoritative per-opp Salesforce snapshot (core mechanics + the deal
        # owner's manager). Several entry paths pass only a THIN opp dict (the
        # worker queue carries just id/account/owner_name/name), so without this
        # the server-owned hard.* override below would be a near no-op and the
        # model's stage/amount/manager could survive. Merge the real values over
        # whatever we were handed (skip id so the caller's key is preserved).
        try:
            _auth = await _authoritative_opp(agent_manager, opp_id)
        except Exception as _e:  # noqa: BLE001
            print(f"[DEAL-SWEEP] authoritative hydration failed opp={opp_id}: "
                  f"{type(_e).__name__}: {_e}", flush=True)
            _auth = {}
        for _ak, _av in (_auth or {}).items():
            if _ak != "id" and _av not in (None, ""):
                opp[_ak] = _av
        # Degraded/failed authoritative read (empty _auth from an exception or a
        # not-found opp): do NOT let the server-owned manager override below blank a
        # known manager just because this one read failed (living-memory: a durable
        # fact goes dormant, it is not deleted on a bad read). Carry the last
        # server-derived manager_name forward. We still NEVER trust the model's value
        # here — the prior persisted value was itself set from Salesforce by this gate.
        if not _auth:
            _prior_mgr = ((existing_record.get("hard") or {}).get("manager_name")
                          if isinstance(existing_record, dict) else None)
            if _prior_mgr and not opp.get("manager_name"):
                opp["manager_name"] = _prior_mgr
        # People allowlist for the anti-fabrication gate: names we can vouch for
        # without a per-item source (SF contact roles + recent task contacts +
        # names on the prior record THAT CARRIED A SOURCE). Sourced/Avoma-discovered
        # people are accepted via their own provenance, so they need not be in here;
        # an unsourced prior name is NOT grandfathered (legacy fabrications get
        # cleaned on re-sweep instead of surviving forever).
        _allowlist = _val.build_people_allowlist(buyer, existing_record)
        # Skip the per-tool gpt-4o-mini prose summariser for this run (same path
        # the opportunity_analyzer uses unattended on this exact toolset): the
        # summariser rewrites Salesforce field reads into lossy prose, dropping the
        # verbatim field VALUES the synthesis read depends on. Avoma meeting tools
        # are already exempt (server._AVOMA_NO_TRUNCATE_TOOLS), so this only affects
        # the SF reads; oversized payloads fall back to deterministic truncation,
        # not prose. Set BEFORE the coroutine is created so it propagates into
        # LangGraph's tool tasks; reset in finally.
        try:
            import server as _server
            _skip_token = _server._skip_llm_summarizer.set(True)
        except Exception as _e:  # noqa: BLE001
            print(f"[DEAL-SWEEP] could not set summariser-skip flag: {_e}", flush=True)
        _active_users = await _active_user_names(agent_manager)
        # Pre-run engagement pulse (calls_read not yet known): one authoritative,
        # today-anchored read of how this deal is being worked, derived from the
        # authoritative SF mechanics + Next Step rep-outreach parse. Injected as
        # ground truth so every section the agent emits is consistent with it
        # (no ghost/dark/future-date/wrong-stage worldview on a live deal; the
        # recent rep outreach is surfaced rather than reported as silence).
        _pre_pulse = _pulse.compute_pulse(
            last_activity_date=(buyer.get("last_activity_date")
                                if isinstance(buyer, dict) else None)
            or opp.get("last_activity_date"),
            calls_read=None,
            stage=opp.get("stage"),
            close_date=opp.get("close_date"),
            forecast_category=opp.get("forecast_category"),
            qualified_date=opp.get("qualified_date"),
            next_step=opp.get("next_step"),
        )
        user_msg = (
            f"Sweep Salesforce Opportunity Id `{opp_id}`"
            + (f" (account: {opp.get('account')}, name: {opp.get('name')})" if opp.get("account") else "")
            + ". Follow your system prompt end-to-end and emit the canonical record "
            "JSON. Output JSON only, no preamble."
            + _sweep_facts_block(opp, buyer)
            + "\n\n" + _pulse.render_block(_pre_pulse) + "\n"
            + identity_block
            + topics_block
        )
        _meta = {"agent_sf_blank": False}

        def _finalize(parsed: dict) -> dict:
            """Deterministic FACT preparation that turns ONE raw agent record into
            a gate-ready candidate: server-owned hard.* override, manager
            reassertion, raw-output people sanitisation, placeholder scrub, and
            per-fact source stamping. Does NOT build the living-memory packets —
            that runs once via _apply_living_memory AFTER the gate, so packets are
            always derived from gate-clean ai. Synchronous and safe to re-run on a
            retry. Records honest hygiene notes in evidence_coverage.gaps but does
            NOT set the gate's violation count — that is owned by the gate outcome
            below."""
            # packets / deltas / schema_version are SERVER-OWNED living memory,
            # rebuilt deterministically by _apply_living_memory from the gate-clean
            # ai AFTER validation. Drop any the model emitted so a hallucinated
            # packet/delta blob can never ride the raw output into the persist path
            # (e.g. on the no-candidates / reconcile-exception branches that don't
            # reassign them). The authoritative prior packets live in
            # existing_packets/existing_record, not in this turn's model output.
            for _owned in ("packets", "deltas", "schema_version", "pulse"):
                parsed.pop(_owned, None)
            # Normalise required envelope fields before persisting.
            parsed["opp_id"] = opp_id
            # swept_at is owned by the server, not the model: the agent sometimes
            # emits a future or wrong date. Always stamp the real run date.
            parsed["swept_at"] = _today()
            # Snapshot what the AGENT itself read from Salesforce, BEFORE we
            # override hard.* from the live discovery snapshot below. If the
            # agent's own read produced none of the core mechanics, its SOQL almost
            # certainly failed (we still persist a record using the snapshot —
            # never withhold — but we mark it retryable).
            _agent_hard = parsed.get("hard") or {}
            _agent_sf_blank = not any(
                (_agent_hard.get(k) not in (None, "", 0))
                for k in ("stage", "amount", "close_date", "account_name", "owner_name"))
            _meta["agent_sf_blank"] = _agent_sf_blank
            hard = parsed.setdefault("hard", {})
            hard.setdefault("opp_id", opp_id)
            # Server-owned deterministic Salesforce facts: a SINGLE canonical
            # override path (shared with the AI-free hard refresh) writes the
            # identity labels, every SF-sourced hard fact, and the server-computed
            # days_to_close from the live snapshot, so the model can never author
            # a fact we hold ground truth for. When THIS opp's authoritative read
            # succeeded (_auth non-empty), Salesforce wins outright — including
            # CLEARING a value the model invented for a field SF leaves blank; a
            # degraded read only fills, never blanks (a known fact stays put).
            # manager_name is handled just below via reassert_manager.
            _val.apply_sf_hard_facts(hard, opp, authoritative=bool(_auth))
            # manager_name is SERVER-OWNED: forced to the live Owner.Manager.Name
            # from the authoritative snapshot (or None when SF has none), NEVER the
            # model's value. Count a REAL fabrication (a non-empty model name that
            # contradicts ground truth) BEFORE we overwrite it — omitting it, the
            # new normal, is not a violation.
            _manager_viol = 1 if _val.manager_fabricated(hard, opp) else 0
            _val.reassert_manager(hard, opp)
            # Anti-fabrication people gate on the RAW agent output, BEFORE it
            # becomes packets: drop structured people that are neither a known
            # SF/Avoma contact nor carry a source. Runs here so fabrications never
            # enter durable memory; legit carried-forward (dormant) names are
            # untouched (not in raw output).
            _people_violations: list = []
            try:
                _people_violations = _val.sanitize_people(parsed.get("ai") or {}, _allowlist)
                if _people_violations:
                    print(f"[DEAL-SWEEP] people-gate opp={opp_id} removed "
                          f"{len(_people_violations)} unverifiable person field(s)", flush=True)
            except Exception as _e:  # noqa: BLE001 — gate must never block the sweep
                print(f"[DEAL-SWEEP] people-gate skipped opp={opp_id}: "
                      f"{type(_e).__name__}: {_e}", flush=True)
            # Deterministic hygiene pass: re-assert the server-owned manager
            # (defensive — projection only touches ai.*), scrub template/
            # placeholder leakage, stamp per-fact provenance the server can vouch
            # for, and record honest hygiene notes in evidence_coverage.gaps. The
            # gate's pass/fail (below) — not this pass — owns the violation count.
            try:
                _val.reassert_manager(parsed.setdefault("hard", {}), opp)
                _scrub_n = _val.scrub_record(parsed)
                _val.stamp_fact_sources(parsed.setdefault("hard", {}), opp)
                _viol_notes = list(_people_violations)
                if _manager_viol:
                    _viol_notes.append("overrode a fabricated manager_name with the "
                                       "live Salesforce owner's manager")
                if _scrub_n:
                    _viol_notes.append(f"scrubbed {_scrub_n} placeholder/template string(s)")
                if _viol_notes:
                    _ec_v = parsed.setdefault("evidence_coverage", {})
                    if isinstance(_ec_v, dict):
                        _g = _ec_v.setdefault("gaps", [])
                        if isinstance(_g, list):
                            _g.extend(_viol_notes)
            except Exception as _e:  # noqa: BLE001 — never block persistence
                print(f"[DEAL-SWEEP] finalize hygiene skipped opp={opp_id}: "
                      f"{type(_e).__name__}: {_e}", flush=True)
            return parsed

        def _apply_living_memory(parsed: dict) -> None:
            """Living-memory packet step, run ONCE after the anti-fabrication gate
            has approved (or deterministically sanitised) the facts. Building the
            durable packets HERE — not per attempt inside _finalize — guarantees
            packets are always derived from gate-clean ai.*, so a fabrication the
            gate stripped from ai (a free-text person, a structured person, or a
            placeholder) can never survive in the packet store (the source of
            truth) and re-project on a later sweep. Merges this sweep into the
            durable packets, retires aged/obsolete carried-forward facts on a clean
            read, regenerates the packet-backed ai.* by projection, then applies the
            zero-calls recency guard and the MEDDPICC normalise. Never raises."""
            import copy
            hard = parsed.setdefault("hard", {})
            _agent_sf_blank = bool(_meta.get("agent_sf_blank"))
            # ONE authoritative engagement pulse, now recomputed WITH this sweep's
            # calls_read folded in, from the server-owned hard.* facts (already
            # overridden from the live SF snapshot in _finalize). Stamped onto the
            # canonical record as the single signal every section + derived view
            # reads. _finalize already dropped any model-emitted "pulse".
            _ec_pulse = parsed.get("evidence_coverage")
            _cr_pulse_raw = _ec_pulse.get("calls_read") if isinstance(_ec_pulse, dict) else None
            try:
                _cr_pulse = int(_cr_pulse_raw) if _cr_pulse_raw is not None else None
            except (TypeError, ValueError):
                _cr_pulse = None
            final_pulse = _pulse.compute_pulse_from_hard(hard, calls_read=_cr_pulse)
            parsed["pulse"] = final_pulse
            # Read-quality gates for living-memory expiry. We only retire carried-
            # forward facts when this sweep genuinely saw the deal — otherwise a
            # read hiccup would silently drop durable memory. Two gates:
            #   * _sf_ok      -- the agent's own Salesforce read returned core
            #                    mechanics (used to retire obsolete pre-v2 hygiene
            #                    flags that assert a field is missing).
            #   * _clean_read -- a full clean read: SF mechanics + Avoma account-
            #                    attendee discovery actually ran, and it is NOT a
            #                    suspect-dark read (zero calls while contact roles
            #                    or recent SF activity exist, i.e. discovery likely
            #                    missed the calls). Gates the age-based retirement.
            _sf_ok = not _agent_sf_blank
            _ec_clean = parsed.get("evidence_coverage")
            _cr_raw_clean = _ec_clean.get("calls_read") if isinstance(_ec_clean, dict) else None
            try:
                _cr_clean = int(_cr_raw_clean) if _cr_raw_clean is not None else None
            except (TypeError, ValueError):
                _cr_clean = None
            _roles_clean = int(buyer.get("roles_count") or 0) if isinstance(buyer, dict) else 0
            _recent_clean = (_within_days(buyer.get("last_activity_date"), 45)
                             if isinstance(buyer, dict) else False)
            _suspect_dark = (_cr_clean == 0 and (_roles_clean > 0 or _recent_clean))
            _clean_read = bool(
                _sf_ok and hard.get("stage") and hard.get("close_date")
                and isinstance(_ec_clean, dict) and _ec_clean.get("discovery_method")
                and not _suspect_dark)
            # Reconcile against a DEEPCOPY of the prior packets (defensive — keep
            # existing_packets/existing_record pristine for the post-persist
            # thin-detection that still reads them).
            _prior_packets = copy.deepcopy(existing_packets)
            # People allowlist for BOTH the packet gate and the MEDDPICC gate —
            # built identically to the per-attempt gate's _sanitize_allow so the
            # carried-forward surfaces are held to exactly the same bar.
            _pkt_allow = set(_allowlist)
            _pkt_allow |= {_val._norm_name(n) for n in _attendees_of(parsed)}
            _pkt_allow |= set(_active_users or set())
            _pkt_allow |= {_val._norm_name(n)
                           for n in _val._sourced_names_in_record(existing_record)}
            _pkt_allow.discard("")
            try:
                candidates = packets_mod.extract_candidates(
                    parsed.get("ai") or {}, parsed.get("hard") or {})
                if candidates or _prior_packets:
                    merged_packets, new_deltas = packets_mod.reconcile(
                        _prior_packets, candidates, parsed["swept_at"])
                    # Clean-read expiry: retire aged carried-forward packets the
                    # agent did not re-confirm (only on a clean read) and obsolete
                    # pre-v2 hygiene flags (whenever the SF read worked), so stale
                    # items and wrong-field flags stop projecting. Prepend deltas.
                    if _sf_ok or _clean_read:
                        merged_packets, _exp_deltas = packets_mod.expire_stale(
                            merged_packets, parsed["swept_at"],
                            retire_aged=_clean_read, retire_obsolete=_sf_ok)
                        if _exp_deltas:
                            new_deltas = _exp_deltas + new_deltas
                            print(f"[DEAL-SWEEP] expiry opp={opp_id} retired "
                                  f"{len(_exp_deltas)} stale packet(s) "
                                  f"(clean_read={_clean_read} sf_ok={_sf_ok})", flush=True)
                    # Packet-level anti-fabrication gate: the per-attempt gate only
                    # cleaned THIS sweep's raw output, but reconcile just merged in
                    # the carried-forward packets — a pre-gate sweep may have minted
                    # one holding a fabricated person/placeholder. Sanitise the
                    # MERGED store before projecting so legacy poison can never be
                    # re-introduced into ai.* after validation. Runs on ANY read
                    # quality (poison removal is not a fact-retention decision). The
                    # allowlist (_pkt_allow, built above) mirrors the gate exactly.
                    merged_packets, _pkt_fixes = _val.sanitize_packets(
                        merged_packets, _pkt_allow, opp)
                    if _pkt_fixes:
                        print(f"[DEAL-SWEEP] packet-gate opp={opp_id} sanitized "
                              f"{_pkt_fixes} poisoned carried-forward packet(s)",
                              flush=True)
                    # Pulse reconciliation: when the live pulse shows recent
                    # verified activity, retire stale-worldview best-practice flags
                    # (ghost / dark-for-months / future-date / wrong-stage) — both
                    # this sweep's fresh ones (already merged in as hygiene packets
                    # above) AND carried-forward ones — so they stop projecting as
                    # live to-do flags that contradict a live deal. Gated on the
                    # pulse being live (which itself requires a known recent
                    # LastActivityDate), not on _clean_read, so a live deal whose
                    # calls discovery missed (suspect-dark) still gets cleaned.
                    if _pulse.is_pulse_live(final_pulse):
                        merged_packets, _pulse_deltas = packets_mod.retire_contradicted_hygiene(
                            merged_packets, parsed["swept_at"],
                            lambda t: _pulse.flag_contradicts_live_pulse(t, final_pulse))
                        if _pulse_deltas:
                            new_deltas = _pulse_deltas + new_deltas
                            print(f"[DEAL-SWEEP] pulse-reconcile opp={opp_id} retired "
                                  f"{len(_pulse_deltas)} stale-worldview flag(s) "
                                  f"(state=live)", flush=True)
                    prior_deltas = existing_record.get("deltas") or []
                    delta_cap = int(os.getenv("DEAL_DELTA_CAP", "200"))
                    parsed["packets"] = merged_packets
                    parsed["deltas"] = (new_deltas + prior_deltas)[:delta_cap]
                    parsed["ai"] = packets_mod.project_into_ai(
                        parsed.get("ai") or {}, merged_packets)
                    parsed["schema_version"] = 2
                    print(f"[DEAL-SWEEP] living-memory opp={opp_id} "
                          f"packets={len(merged_packets)} new_deltas={len(new_deltas)}",
                          flush=True)
            except Exception as _e:  # noqa: BLE001
                print(f"[DEAL-SWEEP] reconcile skipped opp={opp_id}: "
                      f"{type(_e).__name__}: {_e}", flush=True)
            # Recency guard: when this sweep read ZERO buyer calls the deal has no
            # fresh engagement evidence, so any "open requirement" is necessarily
            # carried-forward context, not a freshly confirmed ask. We key off
            # calls_read (the same signal the thin-detection below already trusts)
            # rather than each item's own date, because the agent sometimes
            # re-stamps a stale ask (e.g. a 2024 NDA request) with the current year
            # to look recent — the date is the field it fabricates, calls_read is
            # not. We act ONLY on an explicit, valid zero; a missing/malformed
            # count is treated as "unknown" and left untouched so we never clear a
            # warm deal by accident. The durable packets still retain the asks as
            # history and the re-engagement path lives in recommended_moves, so the
            # deal-detail view stops surfacing stale asks as live requirements.
            _ec_guard = parsed.get("evidence_coverage")
            _cr_raw = _ec_guard.get("calls_read") if isinstance(_ec_guard, dict) else None
            try:
                _calls_read_guard = int(_cr_raw) if _cr_raw is not None else None
            except (TypeError, ValueError):
                _calls_read_guard = None
            if _calls_read_guard == 0 and isinstance(parsed.get("ai"), dict):
                _ai_block = parsed["ai"]
                _moved = 0
                for _sec in ("explicit_requirements", "implicit_requirements"):
                    _s = _ai_block.get(_sec)
                    _its = _s.get("items") if isinstance(_s, dict) else None
                    if isinstance(_its, list):
                        _moved += len(_its)
                    _ai_block[_sec] = {"items": []}
                if _moved:
                    print(f"[DEAL-SWEEP] recency-guard opp={opp_id} cleared {_moved} "
                          f"carried-forward requirement(s) (calls_read=0)", flush=True)
            # MEDDPICC per-element block: normalise to the 8 fixed elements and
            # carry a prior detailed element forward when this sweep emitted an
            # empty one, so a thin/dark read never blanks a previously rich block.
            if isinstance(parsed.get("ai"), dict):
                try:
                    _normalize_meddpicc(parsed["ai"], existing_record.get("ai") or {})
                    # MEDDPICC anti-fabrication gate: narratives (incl. a prior
                    # element carried forward above) are free text NOT covered by
                    # validate_record / sanitize_people / the action-text sanitizer,
                    # so a person/placeholder minted by a pre-gate sweep could ride a
                    # carried-forward element into the record. Neutralise it here,
                    # AFTER carry-forward, on ANY read quality.
                    _md_fixes = _val.sanitize_meddpicc(parsed["ai"], _pkt_allow, opp)
                    if _md_fixes:
                        print(f"[DEAL-SWEEP] meddpicc-gate opp={opp_id} neutralised "
                              f"{_md_fixes} fabrication(s) in MEDDPICC narrative",
                              flush=True)
                except Exception as _e:  # noqa: BLE001
                    print(f"[DEAL-SWEEP] meddpicc normalize skipped opp={opp_id}: "
                          f"{type(_e).__name__}: {_e}", flush=True)

        # MANDATORY anti-fabrication gate at the single persist chokepoint (Task
        # spec Part 4): invoke -> _finalize -> validate_record. A clean candidate
        # is persisted; a failing one is RE-RUN with the violations fed back to the
        # model (<=2 retries); if the model still cannot anchor every fact, a
        # deterministic last-resort sanitize forces each offending value to the
        # Salesforce truth / a role / null and we persist that honest record ONCE.
        # The sweep is therefore structurally unable to persist an invented fact.
        _max_attempts = max(1, int(os.getenv("DEAL_SWEEP_GATE_ATTEMPTS", "3")))
        _feedback = ""
        _failed_validation = False
        _final_violations: list = []
        parsed = None
        for _attempt in range(_max_attempts):
            coro = agent.ainvoke(
                {"messages": [{"role": "user", "content": user_msg + _feedback}]},
                config={"recursion_limit": recursion_limit},
            )
            agent_result = await asyncio.wait_for(coro, timeout=timeout_s)
            # Accumulate token usage ACROSS attempts so the audit log charges the
            # full cost of a retried run, not just the last attempt.
            _u = _sum_usage(agent_result.get("messages", [])
                            if isinstance(agent_result, dict) else [])
            for _uk in ("uncached_input", "output", "cache_creation", "cache_read", "total"):
                usage[_uk] = (usage.get(_uk) or 0) + (_u.get(_uk) or 0)
            usage["seen"] = bool(usage.get("seen")) or bool(_u.get("seen"))
            text = _oa._final_text(agent_result)
            print(f"[DEAL-SWEEP] ainvoke returned opp={opp_id} "
                  f"attempt={_attempt + 1}/{_max_attempts} "
                  f"text_chars={len(text or '')}", flush=True)
            _candidate = _oa._extract_json(text)
            if not isinstance(_candidate, dict) or _candidate.get("_error"):
                if _attempt < _max_attempts - 1:
                    _feedback = ("\n\n--- Your previous output was not valid JSON. "
                                 "Re-emit the FULL canonical record as a single JSON "
                                 "object — no preamble, no markdown fences. ---")
                    print(f"[DEAL-SWEEP] parse failed opp={opp_id} "
                          f"attempt={_attempt + 1} -> retry", flush=True)
                    continue
                result["status"] = "parse_error"
                result["error"] = (_candidate or {}).get("_error", "unparseable record")
                return result
            parsed = _finalize(_candidate)
            _violations = _val.validate_record(
                parsed,
                sf_facts=opp,
                contact_roles=(buyer or {}).get("contacts"),
                avoma_attendees=_attendees_of(parsed),
                active_sf_user_names=_active_users,
                prior_names=_val._sourced_names_in_record(existing_record),
            )
            if not _violations:
                if _attempt:
                    print(f"[DEAL-SWEEP] gate PASS opp={opp_id} on retry "
                          f"{_attempt + 1}/{_max_attempts}", flush=True)
                break
            if _attempt < _max_attempts - 1:
                _feedback = _val.format_validation_feedback(_violations)
                print(f"[DEAL-SWEEP] gate FAIL opp={opp_id} "
                      f"attempt={_attempt + 1}/{_max_attempts} "
                      f"violations={len(_violations)} -> retry", flush=True)
                continue
            # Retries exhausted: deterministic last-resort sanitize, then persist
            # ONCE. An honest, scrubbed record is always saved (never withhold).
            # The sanitize allowlist mirrors the gate's people check (contacts +
            # echoed attendees + active users + SOURCED prior names) so legitimate
            # people survive while unverifiable ones are dropped.
            _sanitize_allow = set(_allowlist)
            _sanitize_allow |= {_val._norm_name(n) for n in _attendees_of(parsed)}
            _sanitize_allow |= set(_active_users or set())
            _sanitize_allow |= {_val._norm_name(n)
                                for n in _val._sourced_names_in_record(existing_record)}
            _sanitize_allow.discard("")
            _fixes = _val.sanitize_failed_record(parsed, _violations, opp,
                                                 allowlist=_sanitize_allow)
            _failed_validation = True
            _final_violations = _violations
            print(f"[DEAL-SWEEP] gate EXHAUSTED opp={opp_id} sanitized "
                  f"{len(_violations)} violation(s) with {_fixes} fix(es)", flush=True)
            break
        # The audit count reflects ONLY unresolved violations at exhaustion — it is
        # 0 when the record passed clean or a retry fixed it; it is the count of
        # facts the model never anchored (then deterministically sanitized) when it
        # did not. result["failed_validation"] flags the latter for the dashboard.
        result["validation_violations"] = len(_final_violations) if _failed_validation else 0
        result["failed_validation"] = _failed_validation
        if result["validation_violations"]:
            print(f"[DEAL-SWEEP] validation gate opp={opp_id} persisted with "
                  f"{result['validation_violations']} sanitized fabrication(s)", flush=True)
        _agent_sf_blank = bool(_meta.get("agent_sf_blank"))
        # Build the durable living-memory packets ONCE, AFTER the gate has approved
        # or sanitised the facts — so packets are always derived from gate-clean ai
        # and a stripped fabrication can never survive in the packet store.
        _apply_living_memory(parsed)
        await asyncio.get_running_loop().run_in_executor(None, store.upsert_record, parsed)
        result["status"] = "completed"
        # Surface the stamped engagement state so the dashboard/audit can flag a
        # regression (a live deal read as dark, or vice versa).
        result["pulse_state"] = (parsed.get("pulse") or {}).get("state")
        # Thin-record detection (drives the worker's retry loop). We ALWAYS keep
        # the record we just persisted — "thin" never withholds, it only flags the
        # record as worth one more attempt:
        #   * SF-read failure: the agent's own read returned no core mechanics.
        #   * Dark-but-shouldn't-be: it read zero buyer calls, yet the deal has
        #     contact roles OR Salesforce activity in the last 45 days — i.e. the
        #     calls almost certainly exist and discovery missed them.
        ec = parsed.get("evidence_coverage") or {}
        try:
            calls_read = int(ec.get("calls_read") or 0)
        except (TypeError, ValueError):
            calls_read = 0
        result["calls_read"] = calls_read
        roles_count = int(buyer.get("roles_count") or 0) if isinstance(buyer, dict) else 0
        recent_activity = (
            _within_days(buyer.get("last_activity_date"), 45)
            if isinstance(buyer, dict) else False)
        reasons = []
        if _agent_sf_blank:
            reasons.append("sf_read_blank")
        if calls_read == 0 and (roles_count > 0 or recent_activity):
            reasons.append(
                f"calls_read=0 with roles={roles_count} recent_activity={recent_activity}")
        if reasons:
            result["thin"] = True
            result["thin_reason"] = "; ".join(reasons)
            print(f"[DEAL-SWEEP] thin record opp={opp_id}: {result['thin_reason']}",
                  flush=True)
    except asyncio.TimeoutError:
        result["status"] = "failed"
        result["error"] = f"timeout after {timeout_s}s"
    except Exception as e:  # noqa: BLE001
        result["status"] = "failed"
        result["error"] = f"{type(e).__name__}: {str(e)[:400]}"
    finally:
        if _skip_token is not None:
            try:
                import server as _server
                _server._skip_llm_summarizer.reset(_skip_token)
            except Exception:
                pass
        result["duration_ms"] = int((time.time() - t0) * 1000)
        await asyncio.get_running_loop().run_in_executor(
            None, _persist_run_log, opp, source, result, usage, model_name)
    return result


def _persist_run_log(opp: dict, source: str, result: dict,
                     usage: dict, model_name: str) -> None:
    """Best-effort: write one row to the deal_trigger_runs audit table.

    Runs in a worker thread (httpx is sync) and never raises — a logging failure
    must not affect the analysis result. Cost reuses server._calculate_llm_cost
    (single pricing source of truth); imported lazily to avoid a circular import
    at module load."""
    try:
        cost = 0.0
        if usage.get("seen"):
            try:
                import server  # lazy: server imports this module's siblings
                cost = server._calculate_llm_cost(
                    model_name,
                    usage.get("uncached_input", 0),
                    usage.get("output", 0),
                    usage.get("cache_creation", 0),
                    usage.get("cache_read", 0),
                )
            except Exception:  # noqa: BLE001 — cost is best-effort
                cost = 0.0
        oid = opp.get("id") or ""
        total_input = (usage.get("uncached_input", 0)
                       + usage.get("cache_creation", 0)
                       + usage.get("cache_read", 0))
        row = {
            "opp_id": oid,
            "opp_id_15": oid[:15],
            "opp_name": opp.get("name"),
            "account_name": opp.get("account"),
            "owner_name": opp.get("owner_name"),
            "source": source,
            "status": result.get("status") or "unknown",
            "duration_ms": result.get("duration_ms"),
            "model": model_name,
            "input_tokens": total_input or None,
            "output_tokens": usage.get("output") or None,
            "total_tokens": usage.get("total") or None,
            "cost_usd": round(cost, 6) if cost else None,
            "error": result.get("error"),
            "validation_violations": int(result.get("validation_violations") or 0),
        }
        _trigger_log.log_run(row)
    except Exception as e:  # noqa: BLE001
        print(f"[DEAL-SWEEP] _persist_run_log failed: {type(e).__name__}: {e}", flush=True)


async def _run_sweep(agent_manager, run_id: str, owner: Optional[str],
                     opp_ids: Optional[list[str]], limit: int,
                     concurrency: int, max_retries: int):
    retry_backoff = max(0, int(os.getenv("DEAL_SWEEP_RETRY_BACKOFF_S", "10")))
    print(f"[DEAL-SWEEP] run {run_id} START model={_selected_model_name()} "
          f"owner={owner or 'ALL'} concurrency={concurrency} "
          f"max_retries={max_retries} retry_backoff_s={retry_backoff}", flush=True)
    try:
        if opp_ids:
            # Explicit id list (e.g. a filtered SF report). Dedupe (preserve order)
            # so one opp == one state row, then enrich labels cheaply so the
            # dashboard shows account + owner while opps are still queued.
            opp_ids = list(dict.fromkeys(i for i in opp_ids if i))
            opps = await _enrich_opp_ids(agent_manager, opp_ids)
            for o in opps:
                if owner and not o.get("owner_name"):
                    o["owner_name"] = owner
        else:
            opps = await discover_opps(agent_manager, owner, limit=limit)
        async with _state_lock:
            _RUN_STATE.update({
                "total": len(opps), "done": 0, "failed": 0, "in_progress": 0,
                "concurrency": concurrency, "max_retries": max_retries,
                "opps": [{"opp_id": o["id"], "account": o.get("account"),
                          "owner_name": o.get("owner_name"), "name": o.get("name"),
                          "status": "queued", "error": None, "attempts": 0,
                          "duration_ms": 0, "started_at": None, "finished_at": None}
                         for o in opps],
            })
        if not opps:
            async with _state_lock:
                _RUN_STATE.update({"status": "succeeded", "finished_at": _now(),
                                   "note": "no open opportunities found for scope"})
            return

        sem = asyncio.Semaphore(max(1, concurrency))

        async def _set(i: int, **fields):
            async with _state_lock:
                _RUN_STATE["opps"][i].update(fields)

        async def _worker(i: int, opp: dict):
            async with sem:
                async with _state_lock:
                    _RUN_STATE["in_progress"] = _RUN_STATE.get("in_progress", 0) + 1
                res = {"status": "failed", "error": "not started", "duration_ms": 0}
                try:
                    attempt = 0
                    while True:
                        attempt += 1
                        if attempt == 1:
                            await _set(i, status="running", attempts=attempt,
                                       started_at=_now())
                        else:
                            await _set(i, status="running", attempts=attempt)
                        res = await analyze_one(agent_manager, opp)
                        # A "thin" completed record (SF read blank, or zero buyer
                        # calls on a deal that clearly has them) is worth one more
                        # attempt — the record is ALWAYS persisted regardless, so a
                        # retry only ever upgrades it, never withholds it.
                        ok_done = res["status"] == "completed" and not res.get("thin")
                        if ok_done or attempt > max_retries:
                            break
                        # transient failure OR thin record -> brief backoff, retry
                        _retry_note = res.get("error") or (
                            f"thin: {res.get('thin_reason')}" if res.get("thin") else None)
                        await _set(i, status="retrying", error=_retry_note)
                        if retry_backoff:
                            await asyncio.sleep(retry_backoff)
                finally:
                    async with _state_lock:
                        _RUN_STATE["in_progress"] = max(
                            0, _RUN_STATE.get("in_progress", 0) - 1)
                        _RUN_STATE["opps"][i].update({
                            "status": res["status"], "error": res.get("error"),
                            "duration_ms": res.get("duration_ms", 0),
                            "finished_at": _now(),
                        })
                        if res["status"] == "completed":
                            _RUN_STATE["done"] = _RUN_STATE.get("done", 0) + 1
                        else:
                            _RUN_STATE["failed"] = _RUN_STATE.get("failed", 0) + 1
                return res

        results = await asyncio.gather(*[_worker(n, o) for n, o in enumerate(opps)])
        ok = sum(1 for r in results if r["status"] == "completed")
        async with _state_lock:
            _RUN_STATE.update({
                "status": "succeeded" if ok == len(opps) else ("partial" if ok else "failed"),
                "finished_at": _now(),
            })
    except Exception as e:  # noqa: BLE001
        async with _state_lock:
            _RUN_STATE.update({"status": "failed", "error": f"{type(e).__name__}: {str(e)[:400]}",
                               "finished_at": _now()})


def queue_enabled() -> bool:
    """Crash-safe queue mode (default ON). When true, a sweep ENQUEUES book opps
    as durable `waiting` rows and the separate worker.py drains them, so the web
    process never runs the batch itself. Flip DEAL_SWEEP_USE_QUEUE=false to fall
    back to the legacy in-process batch (kept for emergencies)."""
    return os.getenv("DEAL_SWEEP_USE_QUEUE", "true").lower() in ("1", "true", "yes")


async def enqueue_book_run(agent_manager, *, owner: Optional[str] = None,
                           opp_ids: Optional[list[str]] = None,
                           limit: int = 500) -> dict:
    """Queue-mode sweep start. Resolve the book (the SAME report-as-book
    membership that is the single source of truth) and enqueue one `waiting` row
    per opp under a fresh run_id, then return immediately — the worker drains the
    queue. One book sweep at a time: refuses while rows are still waiting/working
    so a second click can't double-enqueue the book.
    """
    snap = await asyncio.to_thread(_queue.status)
    if (snap.get("waiting", 0) + snap.get("working", 0)) > 0:
        raise RuntimeError("a sweep is already in progress (queue not drained)")
    if _discovery_running:
        raise RuntimeError("a discovery sweep is in progress; try again shortly")
    if _hard_refresh_running:
        raise RuntimeError("a hard refresh is in progress; try again shortly")

    if opp_ids:
        opp_ids = list(dict.fromkeys(i for i in opp_ids if i))
        # Gate an explicit subset on report-as-book membership (the single source
        # of truth). A manual/triggered subset must NEVER enqueue a non-member —
        # new members are added solely by report reconciliation. active_opp_ids15
        # raises (not returns empty) on a degraded read, so this can't silently
        # drop the whole list.
        active = await asyncio.to_thread(store.active_opp_ids15)
        dropped = [i for i in opp_ids if (i or "")[:15] not in active]
        if dropped:
            print(f"[DEAL-SWEEP] queue enqueue dropped {len(dropped)} non-book "
                  f"opp(s) (not in MASE report): {dropped[:10]}", flush=True)
        opp_ids = [i for i in opp_ids if (i or "")[:15] in active]
        if not opp_ids:
            return {"run_id": None, "status": "queued", "mode": "queue",
                    "owner": owner or "all-team", "total": 0,
                    "note": "no in-book opportunities to enqueue (all ids were "
                            "outside the MASE report)."}
        opps = await _enrich_opp_ids(agent_manager, opp_ids)
        if owner:
            for o in opps:
                o.setdefault("owner_name", owner)
    else:
        opps = await discover_opps(agent_manager, owner, limit=limit)

    run_id = uuid.uuid4().hex[:12]
    enqueued = await asyncio.to_thread(_queue.enqueue_book, run_id, opps)
    print(f"[DEAL-SWEEP] queue enqueue run={run_id} owner={owner or 'ALL'} "
          f"opps={enqueued}", flush=True)
    return {
        "run_id": run_id, "status": "queued", "mode": "queue",
        "owner": owner or "all-team", "total": enqueued,
        "note": ("enqueued; the sweep worker drains the queue. Poll "
                 "/api/deal-engine/sweep/status for progress."),
    }


async def enqueue_trigger(agent_manager, opp_id: str) -> str:
    """Queue-mode single-opp trigger (the Salesforce-update webhook). Enrich the
    opp's display labels cheaply (one SOQL, no agent run) so the dashboard row is
    populated, then enqueue exactly one `waiting` row. Idempotent: an opp already
    waiting/working is left as-is ("already_queued")."""
    opp_id = (opp_id or "").strip()
    if not opp_id:
        return "error"
    # Membership comes ONLY from the MASE report (single source of truth). A
    # trigger is a faster RE-sweep of a deal already in the book — it must never
    # ADD a non-member (e.g. a Salesforce-update webhook firing on an opp outside
    # the report). New members are added solely by report reconciliation.
    if not await asyncio.to_thread(store.is_active_member, opp_id):
        print(f"[DEAL-SWEEP] trigger opp={opp_id} -> not_in_book (skipped)",
              flush=True)
        return "not_in_book"
    # Mutual exclusion with the AI-free hard refresh: once it has set its guard no
    # new queue work may be enqueued, or the worker could claim the row and write a
    # full record over the freshly-corrected SF facts. The webhook is fire-and-
    # forget and the deal is re-swept next cycle, so skipping loses nothing durable.
    if _hard_refresh_running:
        print(f"[DEAL-SWEEP] trigger opp={opp_id} -> skipped "
              "(hard_refresh_in_progress)", flush=True)
        return "skipped_hard_refresh"
    try:
        enriched = await _enrich_opp_ids(agent_manager, [opp_id])
        opp = enriched[0] if enriched else {"id": opp_id}
    except Exception as e:  # noqa: BLE001 — labels are best-effort; never block enqueue
        print(f"[DEAL-SWEEP] trigger enrich failed opp={opp_id}: "
              f"{type(e).__name__}: {e}", flush=True)
        opp = {"id": opp_id}
    opp.setdefault("id", opp_id)
    return await asyncio.to_thread(
        _queue.enqueue_one, f"trigger-{opp_id[:15]}", opp)


async def start_sweep(agent_manager, *, owner: Optional[str] = None,
                      opp_ids: Optional[list[str]] = None, limit: int = 500,
                      concurrency: Optional[int] = None,
                      max_retries: Optional[int] = None) -> dict:
    """Kick off a sweep. One at a time. Returns the run header.

    In queue mode (default) this just enqueues the book and returns; the separate
    worker.py process does the work. With DEAL_SWEEP_USE_QUEUE=false it runs the
    legacy in-process batch.

    concurrency: opps processed in parallel (legacy path only; default
        DEAL_SWEEP_CONCURRENCY, 10). The worker owns concurrency in queue mode.
    max_retries: extra attempts per opp on failure (default DEAL_SWEEP_MAX_RETRIES, 2).
    """
    if queue_enabled():
        return await enqueue_book_run(
            agent_manager, owner=owner, opp_ids=opp_ids, limit=limit)
    global _run_task
    if concurrency is None:
        concurrency = int(os.getenv("DEAL_SWEEP_CONCURRENCY", "10"))
    concurrency = max(1, concurrency)
    if max_retries is None:
        max_retries = int(os.getenv("DEAL_SWEEP_MAX_RETRIES", "2"))
    max_retries = max(0, max_retries)
    async with _state_lock:
        if _RUN_STATE.get("status") == "running":
            raise RuntimeError("a sweep is already running")
        if _discovery_running:
            raise RuntimeError(
                "a discovery sweep is in progress; try again shortly")
        if _hard_refresh_running:
            raise RuntimeError(
                "a hard refresh is in progress; try again shortly")
        run_id = uuid.uuid4().hex[:12]
        _RUN_STATE.clear()
        _RUN_STATE.update({
            "run_id": run_id, "status": "running", "owner": owner or "all-team",
            "started_at": _now(), "finished_at": None,
            "total": None, "done": 0, "failed": 0, "in_progress": 0,
            "concurrency": concurrency, "max_retries": max_retries,
            "opps": [], "error": None,
        })
    _run_task = asyncio.create_task(
        _run_sweep(agent_manager, run_id, owner, opp_ids, limit, concurrency, max_retries))
    return {"run_id": run_id, "status": "running", "owner": owner or "all-team",
            "concurrency": concurrency, "max_retries": max_retries}


# ---- persisted-history merge (so the dashboard shows opps swept in prior runs
# as "completed", not just the current run's opps) ----
_HISTORY_TTL_S = 15
_history_cache: dict[str, Any] = {"ts": 0.0, "rows": []}


def _persisted_completed_rows() -> list[dict]:
    """Build 'completed' dashboard rows from every persisted deal_record.

    Sync (httpx). Cached for _HISTORY_TTL_S so frequent dashboard polls don't
    hammer Supabase. Returns one row per stored opp in the dashboard opp shape.
    """
    now = time.time()
    if now - _history_cache["ts"] < _HISTORY_TTL_S and _history_cache["rows"]:
        return _history_cache["rows"]
    # Deduplicate by 15-char opp-id prefix (an opp may have been persisted under
    # both its 15-char report id and 18-char API id); keep the latest swept_at so
    # done/total reflect UNIQUE opportunities, not raw row counts.
    by_key: dict[str, dict] = {}
    try:
        for rec in store.list_records(None):
            hard = rec.get("hard") or {}
            oid = rec.get("opp_id") or hard.get("opp_id")
            if not oid:
                continue
            key = oid[:15]
            row = {
                "opp_id": oid,
                "account": hard.get("account_name"),
                "owner_name": hard.get("owner_name"),
                "name": hard.get("opp_name"),
                "status": "completed", "error": None, "attempts": 1,
                "duration_ms": 0, "started_at": None,
                "finished_at": rec.get("swept_at"),
                # Prefer the pulse stamped at sweep time; for a record swept
                # before the pulse existed, derive the SAME state from its stored
                # hard.* facts so the book roll-up matches the per-deal badge and
                # is not swamped by "unknown".
                "pulse_state": ((rec.get("pulse") or {}).get("state")
                                or _pulse.compute_pulse_from_hard(hard).get("state")),
            }
            prev = by_key.get(key)
            if prev is None or (row["finished_at"] or "") >= (prev["finished_at"] or ""):
                by_key[key] = row
    except Exception:  # noqa: BLE001 — history is best-effort decoration
        return _history_cache["rows"]
    rows = list(by_key.values())
    _history_cache.update({"ts": now, "rows": rows})
    return rows


def _pulse_summary() -> dict:
    """Aggregate the stamped engagement state across all persisted records so the
    dashboard can flag regressions (a wave of dark deals, or a live deal read as
    dark). Best-effort and cached via _persisted_completed_rows."""
    counts = {"live": 0, "cooling": 0, "dark": 0, "unknown": 0}
    try:
        for r in _persisted_completed_rows():
            s = r.get("pulse_state") or "unknown"
            counts[s if s in counts else "unknown"] += 1
    except Exception:  # noqa: BLE001 — never 500 the dashboard
        pass
    return counts


async def get_status() -> dict:
    if queue_enabled():
        try:
            st = await asyncio.to_thread(_queue.status)
            # Surface the anti-fabrication counter (how many fabrications the gate
            # caught + neutralized in the last 24h). Best-effort: never let the
            # audit read 500 the dashboard.
            try:
                _vc = await asyncio.to_thread(
                    _trigger_log.count_validation_violations, 24)
                st["validation"] = _vc
                # Top-level failure counter the frontend "Sync Quality" panel
                # reads directly (Task spec Part 6): how many records FAILED the
                # gate (needed last-resort sanitize) in the last 24h.
                st["records_failed_validation"] = int(_vc.get("runs_with_violations") or 0)
            except Exception as e:  # noqa: BLE001
                print(f"[DEAL-SWEEP] validation-counter read failed: "
                      f"{type(e).__name__}: {e}", flush=True)
                st.setdefault("records_failed_validation", 0)
            try:
                st["pulse_summary"] = await asyncio.to_thread(_pulse_summary)
            except Exception:  # noqa: BLE001
                st.setdefault("pulse_summary", {})
            return st
        except Exception as e:  # noqa: BLE001 — never 500 the dashboard; fall back
            print(f"[DEAL-SWEEP] queue status read failed, falling back to "
                  f"in-memory: {type(e).__name__}: {e}", flush=True)
    async with _state_lock:
        state = json.loads(json.dumps(_RUN_STATE, default=str))

    live_opps = state.get("opps") or []
    live_ids15 = {(o.get("opp_id") or "")[:15] for o in live_opps}
    history = await asyncio.to_thread(_persisted_completed_rows)
    # Only fold in opps NOT already represented in the current run, so the live
    # run's own completions are never double-counted.
    extra = [r for r in history if (r.get("opp_id") or "")[:15] not in live_ids15]
    if extra:
        state["opps"] = extra + live_opps
        state["total"] = (state.get("total") or len(live_opps)) + len(extra)
        state["done"] = (state.get("done") or 0) + len(extra)
    # Same anti-fabrication counters as the queue path, so /sweep/status always
    # carries the top-level gate counter regardless of which branch served it.
    try:
        _vc = await asyncio.to_thread(_trigger_log.count_validation_violations, 24)
        state["validation"] = _vc
        state["records_failed_validation"] = int(_vc.get("runs_with_violations") or 0)
    except Exception as e:  # noqa: BLE001 — never 500 the dashboard
        print(f"[DEAL-SWEEP] validation-counter read failed (fallback): "
              f"{type(e).__name__}: {e}", flush=True)
        state.setdefault("records_failed_validation", 0)
    return state


# ---- scheduled new-opportunity discovery + sweep ----
# The "book" (discover_opps) is resolved live from Salesforce on every run, so a
# brand-new open opportunity is already IN scope — it simply has no canonical
# deal_record until something sweeps it. This closes that gap: find open team
# opps with no persisted record yet and sweep ONLY those, so new deals
# auto-appear in the Deal Engine within one scheduled cycle. Bounded + capped so
# a first run (or a bad/empty SOQL) can never trigger a runaway book-wide sweep.
_discovery_tasks: set = set()
# Only one discovery run at a time (set+checked atomically on the event loop, no
# await between check and set) so two overlapping discoveries can't both snapshot
# the same "new" opps and double-charge them.
_discovery_running: bool = False


def _persisted_opp_ids15() -> set[str]:
    """The 15-char ids of every opportunity that already has a canonical record.
    Sync (httpx); call via a worker thread from async code."""
    ids: set[str] = set()
    try:
        for rec in store.list_records(None):
            oid = rec.get("opp_id") or (rec.get("hard") or {}).get("opp_id")
            if oid:
                ids.add(oid[:15])
    except Exception as e:  # noqa: BLE001 — caller decides how to treat empty
        print(f"[DEAL-DISCOVERY] persisted-id load failed: "
              f"{type(e).__name__}: {e}", flush=True)
    return ids


_WATERMARK_PATH = Path(__file__).parent / ".deal_engine_discovery_watermark.json"


def _load_watermark() -> Optional[str]:
    """The last-seen Salesforce LastModifiedDate watermark (ISO string), or None
    on first run / unreadable file. File-based: durable across restarts within a
    deployment; a redeploy that resets the FS just re-bootstraps the watermark to
    'now' (records remain the durable new-opp signal, so nothing is missed)."""
    try:
        if _WATERMARK_PATH.exists():
            data = json.loads(_WATERMARK_PATH.read_text())
            wm = data.get("watermark")
            return wm if isinstance(wm, str) and wm else None
    except Exception as e:  # noqa: BLE001
        print(f"[DEAL-DISCOVERY] watermark load failed: {type(e).__name__}: {e}",
              flush=True)
    return None


def _save_watermark(value: str) -> None:
    """Persist the discovery watermark. Best-effort; never raises."""
    try:
        _WATERMARK_PATH.write_text(json.dumps({"watermark": value}))
    except Exception as e:  # noqa: BLE001
        print(f"[DEAL-DISCOVERY] watermark save failed: {type(e).__name__}: {e}",
              flush=True)


async def discover_and_sweep_new(
    agent_manager,
    *,
    limit: int = 500,
    concurrency: Optional[int] = None,
    max_new: Optional[int] = None,
    source: str = "scheduled_discovery",
) -> dict:
    """Sweep open team opportunities that have NO canonical record yet.

    Returns {discovered, already_known, new, capped, swept, completed, failed,
    opp_ids} (or {skipped: ...}). Safe on a schedule: skips entirely while a full
    sweep is in progress (so the same opp is never double-charged), caps the
    number of new opps swept per run (DEAL_DISCOVERY_MAX_NEW), and runs at low
    concurrency (DEAL_DISCOVERY_CONCURRENCY) to stay gentle on the shared Avoma
    subprocess. Each new opp is enriched with live Salesforce labels by
    discover_opps, so owner attribution is preserved."""
    global _discovery_running
    # Clamp ALL inputs (callers/endpoints can pass junk): limit>=1, concurrency>=1,
    # max_new>=0. A negative max_new with new_opps[:max_new] would otherwise sweep
    # almost the whole book, defeating the cap.
    limit = max(1, int(limit))
    if concurrency is None:
        concurrency = int(os.getenv("DEAL_DISCOVERY_CONCURRENCY", "2"))
    concurrency = max(1, int(concurrency))
    if max_new is None:
        max_new = int(os.getenv("DEAL_DISCOVERY_MAX_NEW", "25"))
    max_new = max(0, int(max_new))

    # Serialize discovery (atomic: no await between the check and the set).
    if _discovery_running:
        return {"skipped": "discovery_in_progress", "discovered": 0, "new": 0,
                "swept": 0, "completed": 0, "failed": 0, "opp_ids": []}
    _discovery_running = True
    try:
        async with _state_lock:
            running = _RUN_STATE.get("status") == "running"
        if running:
            return {"skipped": "sweep_in_progress", "discovered": 0, "new": 0,
                    "swept": 0, "completed": 0, "failed": 0, "opp_ids": []}
        # Mutual exclusion with the AI-free hard refresh (same reasoning as the
        # other enqueue paths): once the hard refresh has set its guard, discovery
        # must NOT analyze/upsert any opp, or analyze_one would write a full record
        # over the freshly-corrected SF facts. Discovery is schedule-driven and
        # re-runs next cycle, so skipping loses nothing durable.
        if _hard_refresh_running:
            return {"skipped": "hard_refresh_in_progress", "discovered": 0,
                    "new": 0, "swept": 0, "completed": 0, "failed": 0,
                    "opp_ids": []}

        opps = await discover_opps(agent_manager, None, limit=limit)
        # Skip deals sitting at "Initial Interest": these are too early-stage for
        # the engine, and re-pulling them would undo a hard-refresh that removed
        # them. Filter them out before the new-vs-known comparison.
        opps = [o for o in opps
                if (o.get("stage") or "").strip().lower() != "initial interest"]
        known = await asyncio.to_thread(_persisted_opp_ids15)
        # Membership is the MASE report ONLY. Gate brand-new additions on the
        # report so that if discover_opps had to fall back to VP/owner SOQL
        # (report read failed), we NEVER add a non-report opp to the book. If the
        # report is unavailable, add nothing new this run; the change re-sweep of
        # already-known opps below is membership-neutral and still runs.
        import deal_engine_report as report
        _mem = await asyncio.to_thread(report.fetch_report_membership)
        report15 = set(_mem["ids15"]) if _mem.get("ok") else None
        if report15 is None:
            new_opps = []
        else:
            new_opps = [o for o in opps
                        if (o.get("id") or "")[:15] not in known
                        and (o.get("id") or "")[:15] in report15]
        # Watermark self-refresh: besides brand-new opps, also re-sweep
        # already-known opps that Salesforce has MODIFIED since our last run
        # (e.g. a stage move or amount change), so the record never goes stale.
        # First run (no watermark) only bootstraps the watermark and sweeps new
        # opps — it never treats the whole book as "changed". The watermark is a
        # FIFO cursor: changed opps are processed OLDEST-first, and the watermark
        # only advances past the changed opps we actually swept, so any capped-out
        # backlog stays eligible next run (never silently skipped).
        wm_dt = _parse_sf_dt(_load_watermark())
        max_changed = max(0, int(os.getenv("DEAL_DISCOVERY_MAX_CHANGED", "25")))
        changed_pairs: list[tuple] = []  # (last_modified_dt, opp)
        if wm_dt and max_changed:
            for o in opps:
                if (o.get("id") or "")[:15] not in known:
                    continue
                lm = _parse_sf_dt(o.get("last_modified"))
                if lm and lm > wm_dt:
                    changed_pairs.append((lm, o))
            changed_pairs.sort(key=lambda t: t[0])  # oldest first (FIFO)
        changed_list = [o for _lm, o in changed_pairs]
        capped_new = new_opps[:max_new] if max_new else []
        capped_changed = changed_list[:max_changed] if max_changed else []
        leftover_changed = changed_list[len(capped_changed):]
        # Combine, de-duped by 15-char id (a brand-new opp can't also be changed,
        # but guard anyway so one opp is never swept twice in a single run).
        _seen15: set[str] = set()
        capped: list[dict] = []
        for o in capped_new + capped_changed:
            k = (o.get("id") or "")[:15]
            if k and k not in _seen15:
                _seen15.add(k)
                capped.append(o)
        # Advance the watermark to the newest LastModifiedDate among the opps we
        # actually swept, but never past the oldest changed opp we had to DEFER —
        # so deferred (capped-out) changes are re-picked next run. Bootstrap to now
        # on an empty book.
        swept_dts = [d for d in (_parse_sf_dt(o.get("last_modified")) for o in capped) if d]
        candidate = max(swept_dts) if swept_dts else wm_dt
        leftover_dts = [d for d in
                        (_parse_sf_dt(o.get("last_modified")) for o in leftover_changed) if d]
        if leftover_dts:
            boundary = min(leftover_dts) - timedelta(milliseconds=1)
            candidate = boundary if candidate is None else min(candidate, boundary)
        if candidate is None:
            candidate = datetime.now(timezone.utc)
        new_watermark = candidate.isoformat()
        _save_watermark(new_watermark)
        out = {
            "discovered": len(opps),
            "already_known": len(opps) - len(new_opps),
            "new": len(new_opps),
            "changed": len(changed_list),
            "deferred_changed": len(leftover_changed),
            "capped": len(capped),
            "watermark": new_watermark,
            "swept": 0, "completed": 0, "failed": 0, "skipped_inflight": 0,
            "opp_ids": [o.get("id") for o in capped],
        }
        if not capped:
            return out

        sem = asyncio.Semaphore(concurrency)

        async def _one(o: dict) -> dict:
            # Share the trigger in-flight registry so the same opp is never swept
            # by discovery AND a manual /sweep/trigger at the same time. Claim is
            # atomic (no await between the check and the add), released in finally.
            key = (o.get("id") or "")[:15]
            async with sem:
                if not key or key in _trigger_inflight:
                    return {"opp_id": o.get("id"), "status": "already_running"}
                _trigger_inflight.add(key)
                try:
                    return await analyze_one(agent_manager, o, source=source)
                finally:
                    _trigger_inflight.discard(key)

        results = await asyncio.gather(*[_one(o) for o in capped],
                                       return_exceptions=True)
        for r in results:
            if isinstance(r, dict) and r.get("status") == "already_running":
                out["skipped_inflight"] += 1
                continue
            out["swept"] += 1
            if isinstance(r, dict) and r.get("status") == "completed":
                out["completed"] += 1
            else:
                out["failed"] += 1
        # New records exist now — bust the dashboard/book history cache so they show.
        _history_cache["ts"] = 0.0
        print(f"[DEAL-DISCOVERY] discovered={out['discovered']} new={out['new']} "
              f"changed={out['changed']} deferred_changed={out['deferred_changed']} "
              f"capped={out['capped']} completed={out['completed']} "
              f"failed={out['failed']} skipped_inflight={out['skipped_inflight']} "
              f"watermark={out['watermark']}",
              flush=True)
        return out
    finally:
        _discovery_running = False


# ---- report-driven membership reconciliation (single source of truth) ----
# The MASE report decides who is in the book. Each cycle we deactivate opps that
# left and reactivate re-entrants. The sanity ratio guards against a bad report
# read silently gutting the book.
_RECONCILE_MIN_RATIO = float(os.getenv("DEAL_RECONCILE_MIN_RATIO", "0.60"))
_reconcile_running = False


async def reconcile_membership(
    agent_manager,
    *,
    sweep_new: bool = True,
    concurrency: Optional[int] = None,
    max_new: Optional[int] = None,
    source: str = "report_reconcile",
) -> dict:
    """Make the MASE report the single source of truth for book membership.

    Each run: read the report fresh; deactivate opps that LEFT the report (soft —
    record + history kept); reactivate re-entrants; then sweep brand-new +
    re-entered opps (capped) so they get a canonical record.

    SAFETY: if the report read fails / is empty (ok=False), abort entirely and
    never touch the book. If the report set is implausibly smaller than the
    current active book (< DEAL_RECONCILE_MIN_RATIO, default 60%), abort the
    REMOVAL only (still allow adds/reactivations) and flag it loudly. Returns a
    structured summary."""
    global _reconcile_running
    import deal_engine_report as report
    if concurrency is None:
        concurrency = int(os.getenv("DEAL_DISCOVERY_CONCURRENCY", "2"))
    concurrency = max(1, int(concurrency))
    if max_new is None:
        max_new = int(os.getenv("DEAL_DISCOVERY_MAX_NEW", "25"))
    max_new = max(0, int(max_new))

    if _reconcile_running:
        return {"ok": False, "skipped": "reconcile_in_progress"}
    _reconcile_running = True
    try:
        # Fresh, uncached read — this is the safety-critical membership decision.
        mem = await asyncio.to_thread(report.fetch_report_membership, True)
        if not mem.get("ok"):
            print(f"[DEAL-RECONCILE] aborted: {mem.get('error')}", flush=True)
            return {"ok": False, "aborted": True, "reason": mem.get("error"),
                    "report_count": 0, "removal_ran": False,
                    "added": [], "removed": [], "reactivated": [], "unchanged": 0,
                    "swept": 0, "completed": 0, "failed": 0}

        report15 = set(mem["ids15"])
        id18_by15 = {(i or "")[:15]: i for i in mem["ids18"]}

        known = await asyncio.to_thread(store.known_active_map)  # id15 -> active
        active15 = {k for k, v in known.items() if v}
        inactive15 = {k for k, v in known.items() if not v}

        to_remove = sorted(active15 - report15)
        reenter = sorted(report15 & inactive15)
        new15 = sorted(report15 - set(known.keys()))
        unchanged = len(active15 & report15)

        # Sanity bound: a healthy report should not shrink the active book by
        # >40% in one cycle. If it does, the read is likely a filter/permission
        # blip — keep the book, still allow adds/reactivations.
        removal_ran = True
        sanity_alert = None
        if active15 and len(report15) < _RECONCILE_MIN_RATIO * len(active15):
            removal_ran = False
            sanity_alert = (
                f"report set ({len(report15)}) < "
                f"{int(_RECONCILE_MIN_RATIO * 100)}% of active book "
                f"({len(active15)}); skipping removals this cycle")
            print(f"[DEAL-RECONCILE] \u26a0 {sanity_alert}", flush=True)

        removed_done: list[str] = []
        if removal_ran and to_remove:
            await asyncio.to_thread(store.set_active, to_remove, False)
            removed_done = to_remove
        if reenter:
            await asyncio.to_thread(store.set_active, reenter, True)

        out = {
            "ok": True, "aborted": False, "report_count": len(report15),
            "active_before": len(active15), "removal_ran": removal_ran,
            "sanity_alert": sanity_alert,
            "added": new15, "removed": removed_done, "reactivated": reenter,
            "unchanged": unchanged, "truncated": bool(mem.get("truncated")),
            "swept": 0, "completed": 0, "failed": 0, "skipped_inflight": 0,
        }

        # Sweep brand-new + re-entered members so they get a fresh record.
        # Cap across the combined set (new first); max_new==0 disables sweeping.
        if sweep_new and max_new:
            combined = new15 + [r for r in reenter if r not in set(new15)]
            combined = combined[:max_new]
            sweep_ids18 = [id18_by15.get(k, k) for k in combined]
            if sweep_ids18:
                opps = await _enrich_opp_ids(agent_manager, sweep_ids18)
                sem = asyncio.Semaphore(concurrency)

                async def _one(o: dict) -> dict:
                    key = (o.get("id") or "")[:15]
                    async with sem:
                        if not key or key in _trigger_inflight:
                            return {"opp_id": o.get("id"), "status": "already_running"}
                        _trigger_inflight.add(key)
                        try:
                            return await analyze_one(agent_manager, o, source=source)
                        finally:
                            _trigger_inflight.discard(key)

                results = await asyncio.gather(*[_one(o) for o in opps],
                                               return_exceptions=True)
                for r in results:
                    if isinstance(r, dict) and r.get("status") == "already_running":
                        out["skipped_inflight"] += 1
                        continue
                    out["swept"] += 1
                    if isinstance(r, dict) and r.get("status") == "completed":
                        out["completed"] += 1
                    else:
                        out["failed"] += 1

        # Membership changed — bust the dashboard/book history cache.
        _history_cache["ts"] = 0.0
        print(f"[DEAL-RECONCILE] report={out['report_count']} "
              f"added={len(out['added'])} removed={len(out['removed'])} "
              f"reactivated={len(out['reactivated'])} unchanged={out['unchanged']} "
              f"removal_ran={out['removal_ran']} swept={out['swept']} "
              f"completed={out['completed']} failed={out['failed']}", flush=True)
        return out
    finally:
        _reconcile_running = False


# ---- token-free hard-field refresh across the whole book ----
# Pulls ONLY the hard Salesforce fields (stage / amount / products / close_date /
# owner / next_step) straight from SOQL — no LLM, no agent, ~zero token cost — and
# merges them onto every persisted record, preserving the AI analysis + history.
_hard_refresh_running: bool = False
_hard_refresh_last: dict = {}
_HARD_REFRESH_LAST_PATH = Path(__file__).parent / ".deal_engine_hard_refresh_last.json"


def _save_hard_refresh_last(summary: dict) -> None:
    """Persist the most recent hard-refresh summary so it survives a restart and
    can be checked later. Best-effort; never raises."""
    try:
        _HARD_REFRESH_LAST_PATH.write_text(json.dumps(summary, default=str))
    except Exception as e:  # noqa: BLE001
        print(f"[DEAL-HARD-REFRESH] summary save failed: {type(e).__name__}: {e}",
              flush=True)


def get_hard_refresh_last() -> dict:
    """The summary of the most recent hard refresh (records / matched / updated /
    removed / unmatched / failed / finished_at / source). Returns the in-memory
    copy if present, else the persisted file, else {} when none has run yet."""
    if _hard_refresh_last:
        return _hard_refresh_last
    try:
        if _HARD_REFRESH_LAST_PATH.exists():
            return json.loads(_HARD_REFRESH_LAST_PATH.read_text())
    except Exception as e:  # noqa: BLE001
        print(f"[DEAL-HARD-REFRESH] summary load failed: {type(e).__name__}: {e}",
              flush=True)
    return {}


async def hard_refresh_all(
    agent_manager,
    *,
    delete_initial_interest: bool = True,
    concurrency: Optional[int] = None,
    source: str = "manual",
) -> dict:
    """Refresh the hard Salesforce fields on every canonical deal record, with no
    AI cost.

    For each persisted record we read the live Salesforce values in bulk (one
    chunked SOQL per ~200 ids) and MERGE stage / amount / products / close_date /
    owner / next_step onto the existing record — the AI analysis (`ai`), packets
    and deltas are left untouched. Any deal that has slipped back to the "Initial
    Interest" stage is DELETED (when delete_initial_interest is set). Returns a
    summary dict. Skips while a full sweep is running so we never clobber a
    just-completed AI sweep."""
    global _hard_refresh_running, _hard_refresh_last
    if concurrency is None:
        concurrency = int(os.getenv("DEAL_HARD_REFRESH_CONCURRENCY", "8"))
    concurrency = max(1, int(concurrency))

    # Serialize hard refreshes (atomic: no await between check and set).
    if _hard_refresh_running:
        out = {"skipped": "hard_refresh_in_progress", "status": "skipped",
               "source": source, "finished_at": _now()}
        _hard_refresh_log.log_run(out)  # log EVERY invocation, skips included
        return out
    _hard_refresh_running = True
    # `out` is the audit row; the finally block logs it for ALL exit paths
    # (completed run, no-op skip, or fatal failure) so the nightly cadence and
    # any anomalous run are always recorded.
    out: dict = {"status": "completed", "source": source}
    try:
        async with _state_lock:
            running = _RUN_STATE.get("status") == "running"
        if running:
            out = {"skipped": "sweep_in_progress", "status": "skipped",
                   "source": source}
            return out
        # Queue mode: the batch sweep runs in the SEPARATE worker.py process and is
        # tracked in sweep_queue, NOT this process's _RUN_STATE. Our guard is now
        # set, so no NEW work can be enqueued (every enqueue_* path checks
        # _hard_refresh_running); refuse only if work is ALREADY waiting/working,
        # else the worker could write a record between our re-read and upsert and we
        # would clobber it with a stale full-record blob.
        if queue_enabled():
            snap = await asyncio.to_thread(_queue.status)
            waiting, working = snap.get("waiting", 0), snap.get("working", 0)
            if (waiting + working) > 0:
                out = {"skipped": "sweep_queue_active", "status": "skipped",
                       "source": source, "waiting": waiting, "working": working}
                return out

        records = await asyncio.to_thread(store.list_records, None)
        ids: list[str] = []
        for rec in records:
            oid = rec.get("opp_id") or (rec.get("hard") or {}).get("opp_id")
            if oid:
                ids.append(oid)
        ids = list(dict.fromkeys(ids))
        enriched = await _enrich_opp_ids(agent_manager, ids)
        by15 = {(o.get("id") or "")[:15]: o for o in enriched}

        out = {
            "records": len(records), "matched": 0, "updated": 0,
            "removed": 0, "unmatched": 0, "failed": 0,
            "removed_opps": [], "source": source, "status": "completed",
        }
        sem = asyncio.Semaphore(concurrency)

        async def _one(rec: dict) -> None:
            oid = (rec.get("opp_id") or (rec.get("hard") or {}).get("opp_id") or "")
            key = oid[:15]
            live = by15.get(key)
            if not live:
                out["unmatched"] += 1
                return
            out["matched"] += 1
            stage = (live.get("stage") or "").strip()
            async with sem:
                try:
                    if delete_initial_interest and stage.lower() == "initial interest":
                        # Delete by the exact stored key form (records are keyed on
                        # the 15-char id by upsert_record; fall back to the prefix).
                        await asyncio.to_thread(
                            store.delete_record, rec.get("opp_id") or key)
                        out["removed"] += 1
                        out["removed_opps"].append(key)
                        return
                    # Re-read the latest record right before writing so a concurrent
                    # single-opp trigger that refreshed the AI analysis in the gap
                    # since the initial snapshot is NOT clobbered by our stale copy.
                    latest = await asyncio.to_thread(store.get_record, key)
                    rec = latest or rec
                    hard = rec.setdefault("hard", {})
                    # The opp matched the bulk SOQL, so this is a CONFIRMED-clean
                    # Salesforce read: apply the SAME canonical override the AI
                    # sweep uses, authoritative=True so Salesforce wins outright
                    # (a field SF leaves blank CLEARS any model-authored value).
                    # manager via reassert_manager (server-owned), then stamp
                    # provenance so the corrected facts carry a <field>_source
                    # exactly like a fresh sweep would.
                    _val.apply_sf_hard_facts(hard, live, authoritative=True)
                    _val.reassert_manager(hard, live)
                    _val.stamp_fact_sources(hard, live)
                    await asyncio.to_thread(store.upsert_record, rec)
                    out["updated"] += 1
                except Exception as e:  # noqa: BLE001
                    out["failed"] += 1
                    print(f"[DEAL-HARD-REFRESH] opp={key} failed: "
                          f"{type(e).__name__}: {e}", flush=True)

        await asyncio.gather(*[_one(r) for r in records])
        # Records changed — bust the dashboard/book history cache so they show.
        _history_cache["ts"] = 0.0
        out["finished_at"] = _now()
        _hard_refresh_last = out
        _save_hard_refresh_last(out)
        print(f"[DEAL-HARD-REFRESH] source={out['source']} records={out['records']} "
              f"matched={out['matched']} updated={out['updated']} "
              f"removed={out['removed']} unmatched={out['unmatched']} "
              f"failed={out['failed']}", flush=True)
        return out
    except Exception as e:  # noqa: BLE001 — record the fatal failure, then re-raise
        out = {"status": "failed", "source": source,
               "error": f"{type(e).__name__}: {e}"}
        raise
    finally:
        _hard_refresh_running = False
        # Append-only audit trail for EVERY non-early-return invocation
        # (completed, skipped, or failed). Best-effort — never masks the result
        # or the re-raised exception.
        out.setdefault("finished_at", _now())
        _hard_refresh_log.log_run(out)


# ---- single-opp trigger (e.g. a Salesforce update webhook) ----
# Independent of the main one-at-a-time sweep guard, so an opp can be refreshed
# while a full sweep is running. Deduped per opp (15-char key) so rapid repeat
# updates from Salesforce don't stack duplicate analyses, and bounded by a small
# semaphore so a burst of updates doesn't overwhelm the shared Avoma subprocess.
_trigger_inflight: set[str] = set()
_trigger_sem: Optional[asyncio.Semaphore] = None
_trigger_tasks: set = set()


async def _run_trigger(agent_manager, opp_id: str, key: str) -> dict:
    """Worker: enrich + analyze one opp, bounded by the trigger semaphore.
    Always releases the in-flight claim. Returns the analyze_one result."""
    global _trigger_sem
    try:
        # Membership comes ONLY from the MASE report. A trigger is a faster
        # RE-sweep of a deal already in the book — it must never ADD a non-member
        # (e.g. a Salesforce-update webhook firing on an opp outside the report).
        # New members are added solely by report reconciliation.
        if not await asyncio.to_thread(store.is_active_member, opp_id):
            print(f"[DEAL-SWEEP] trigger opp={opp_id} -> not_in_book (skipped)",
                  flush=True)
            return {"opp_id": opp_id, "status": "not_in_book"}
        # Mutual exclusion with the AI-free hard refresh (symmetric with the full
        # sweep, which start_sweep/enqueue already gate on). A trigger runs a slow
        # AI analysis off an existing-record base; if it landed mid hard-refresh
        # its full-record write could clobber the freshly-corrected SF facts. The
        # webhook is fire-and-forget and the deal is re-swept on the next cycle,
        # so skipping here is safe and loses nothing durable.
        if _hard_refresh_running:
            print(f"[DEAL-SWEEP] trigger opp={opp_id} -> skipped "
                  "(hard_refresh_in_progress)", flush=True)
            return {"opp_id": opp_id, "status": "skipped",
                    "reason": "hard_refresh_in_progress"}
        if _trigger_sem is None:
            _trigger_sem = asyncio.Semaphore(
                max(1, int(os.getenv("DEAL_TRIGGER_CONCURRENCY", "3"))))
        async with _trigger_sem:
            opps = await _enrich_opp_ids(agent_manager, [opp_id])
            res = await analyze_one(agent_manager, opps[0], source="salesforce_trigger")
        # Refresh the dashboard/book history so the updated record shows promptly.
        _history_cache["ts"] = 0.0
        print(f"[DEAL-SWEEP] trigger opp={opp_id} -> {res.get('status')}", flush=True)
        return res
    finally:
        _trigger_inflight.discard(key)


def trigger_opp_async(agent_manager, opp_id: str) -> str:
    """Fire-and-forget a single-opp re-analysis as a tracked background task.

    The in-flight claim is made SYNCHRONOUSLY here (atomic on the single event
    loop — no await before the set is mutated), so two near-simultaneous calls
    for the same opp can't both start. Returns "accepted", "already_running", or
    "error"."""
    opp_id = (opp_id or "").strip()
    if not opp_id:
        return "error"
    key = opp_id[:15]
    if key in _trigger_inflight:
        return "already_running"
    _trigger_inflight.add(key)
    try:
        t = asyncio.create_task(_run_trigger(agent_manager, opp_id, key))
    except Exception:  # noqa: BLE001 — never leak a claim if scheduling fails
        _trigger_inflight.discard(key)
        raise
    _trigger_tasks.add(t)
    t.add_done_callback(_trigger_tasks.discard)
    return "accepted"


async def analyze_opp_now(agent_manager, opp_id: str) -> dict:
    """Re-run the sweep for ONE opp synchronously and return the result.
    Deduped against in-flight triggers. status "already_running" if busy."""
    opp_id = (opp_id or "").strip()
    if not opp_id:
        return {"opp_id": opp_id, "status": "error", "error": "missing opp_id"}
    key = opp_id[:15]
    if key in _trigger_inflight:
        return {"opp_id": opp_id, "status": "already_running"}
    _trigger_inflight.add(key)
    return await _run_trigger(agent_manager, opp_id, key)
