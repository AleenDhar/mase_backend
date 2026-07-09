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
import re
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

# MASE knowledge namespace marker — routes search_knowledge to the isolated MASE
# knowledge tables (mase_documents/mase_document_chunks), the SAME store the todo-runner
# uses. Kept in sync with custom_tools.search_knowledge._MASE_KNOWLEDGE_PROJECT_ID and
# the frontend MASE_KNOWLEDGE_PROJECT_ID.
MASE_KNOWLEDGE_PROJECT_ID = "7e9b2f48-3c1a-4d6e-8b05-9a2c4f1d7e30"

# The deal-sweep system prompt is fetched from SUPABASE at runtime — that is the
# source of truth (see agent_prompt_store, key ID_DEAL_SWEEP). The on-disk markdown
# file below is the version-controlled SEED / DEFAULT: it ships in the image, it is
# what the Admin -> Agent Control editor pre-fills, and it is used verbatim
# whenever no Supabase override is set. Admins edit the LIVE prompt from the Admin
# page (which writes the Supabase row); the change is picked up on the next opp
# without a redeploy because _get_agent re-resolves the prompt (TTL-throttled) and
# rebuilds the cached agent whenever its fingerprint changes — in BOTH the API
# process and the separate sweep worker. See _load_prompt() / _get_agent().
_PROMPT_PATH = Path(__file__).parent / "prompts" / "deal_engine_sweep_system_prompt.md"

_agent_lock = asyncio.Lock()
_cached_agent = None
_cached_tool_names: list[str] = []
# Fingerprint of the prompt the cached agent was built with + when we last
# re-resolved it from Supabase, so an admin edit takes effect without a restart.
_cached_prompt_fp: str = ""
_cached_prompt_checked_at: float = 0.0
# Don't hit Supabase for the prompt more than once per this window per process
# (a big concurrent sweep would otherwise re-read the settings row every opp).
_PROMPT_RECHECK_TTL_S = float(os.getenv("DEAL_SWEEP_PROMPT_RECHECK_TTL_S", "15"))

# ---- run state (process-local; one sweep at a time) ----
_state_lock = asyncio.Lock()
_RUN_STATE: dict[str, Any] = {"status": "idle"}
_run_task: Optional[asyncio.Task] = None


def _today() -> str:
    return date.today().isoformat()


def _now() -> str:
    """UTC ISO timestamp (with time) for live progress tracking."""
    return datetime.now(timezone.utc).isoformat()


# Indian Standard Time (UTC+5:30). swept_at carries a full IST timestamp (date AND
# time), not just a date, so freshness checks (next-step / activity / meeting vs the
# sweep) are exact — no same-calendar-day ambiguity.
_IST = timezone(timedelta(hours=5, minutes=30))


def _now_ist() -> str:
    """Full IST ISO timestamp, e.g. 2026-06-28T15:42:10+05:30."""
    return datetime.now(_IST).isoformat()


def _trigger_cooldown_hours() -> float:
    """Hours an opp is exempt from a NEW Salesforce-triggered re-sweep after its
    last COMPLETED sweep. 0 or negative disables the cooldown. Env-tunable via
    DEAL_SWEEP_TRIGGER_COOLDOWN_HOURS (default 6). This is the debounce that stops
    a burst of CDC triggers (a rep editing stage, then amount, then close date, or
    activity churn) from firing many paid sweeps of the same deal — the 76%
    repeat-sweep waste in the 2026-07-02 burn."""
    try:
        return float(os.getenv("DEAL_SWEEP_TRIGGER_COOLDOWN_HOURS", "6"))
    except (TypeError, ValueError):
        return 6.0


def _recent_sweep_age_hours(opp_id: str) -> Optional[float]:
    """Hours since this opp's last completed sweep, read from the stored record's
    `swept_at` IST timestamp. None if never swept / no record / unparseable — the
    caller treats None as "no cooldown" (fail-open, never drop a real trigger on a
    lookup hiccup). Sync (httpx) — call via asyncio.to_thread."""
    try:
        rec = store.get_record(opp_id)
    except Exception:  # noqa: BLE001 — best-effort; never block enqueue on lookup
        return None
    ts = (rec or {}).get("swept_at")
    if not ts or not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_IST)
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0)


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


def _disk_prompt() -> str:
    """The version-controlled cold-start SEED prompt shipped on disk.

    DEPRECATED as the source of truth: Supabase (agent_prompt_store ID_DEAL_SWEEP)
    is authoritative. This file is the fallback used only when Supabase has no row,
    and the value the Admin editor shows as the built-in default. Its leading
    DEPRECATION banner comment is stripped so it never enters the prompt."""
    if not _PROMPT_PATH.exists():
        raise FileNotFoundError(f"sweep prompt seed missing: {_PROMPT_PATH}")
    import agent_prompt_store as _aps
    return _aps.strip_leading_banner(_PROMPT_PATH.read_text(encoding="utf-8"))


def _load_prompt() -> str:
    """Return the EFFECTIVE deal-sweep system prompt.

    SUPABASE IS THE SOURCE OF TRUTH. Precedence (2026-07-09, Omnivision Deal-Sweep asset):
      1. the LOCKED `sweep` engine from the Scoring Version Studio (Omnivision) — the
         Deal Sweep (Deal Drawer) instruction IS the base system prompt when locked;
         edit + lock a new version in /omnivision and the sweep adopts it on the next
         agent (re)build, no code deploy;
      2. else the Supabase agent-control value (agent_prompt_store, key ID_DEAL_SWEEP)
         — the legacy monolithic prompt, still editable in Admin -> Agent Control;
      3. else the on-disk seed (prompts/deal_engine_sweep_system_prompt.md).

    The studio block (the other locked engines + the reference assets) is ALWAYS
    appended on top of whichever base won. Never raises on a Supabase blip — degrades
    down the precedence chain so the sweep is never blocked by a settings read.
    """
    base = ""
    # 1) Omnivision-governed base: the locked Deal Sweep engine (via the studio cache).
    try:
        _blk = _studio_block()   # primes/reuses the cache; also builds the appendix
        _sw = (_studio_cache.get("sweep_base") or "").strip()
        if _sw:
            return _sw + _blk
    except Exception as _e:  # noqa: BLE001
        print(f"[DEAL-SWEEP] studio sweep-base read failed ({_e}); falling back", flush=True)
    # 2) legacy Supabase base prompt
    try:
        import agent_prompt_store as _aps
        base = (_aps.get_prompt(_aps.ID_DEAL_SWEEP) or "").strip()
    except Exception as _e:  # noqa: BLE001 — never block the sweep on the settings read
        print(f"[DEAL-SWEEP] supabase prompt read failed ({_e}); using disk seed", flush=True)
    return (base or _disk_prompt()) + _studio_block()


# --- SCORING VERSION STUDIO (Omnivision) — the LOCKED engine instructions GOVERN the sweep ---
# The five versioned instructions (extract / win / mom / todo / sum) edited + locked in
# /omnivision are appended to the effective sweep prompt as the AUTHORITATIVE final section:
# lock a new version there → the sweep adopts it on the next agent (re)build (TTL / reset),
# no code deploy. Lock-before-run: only LOCKED versions are ever injected (drafts invisible);
# fail-OPEN to the previous cached block (or none) so a Supabase blip can't stall sweeps.
_STUDIO_TTL_S = int(os.getenv("SCORING_STUDIO_TTL_S", "300"))
_studio_cache: dict = {"at": 0.0, "block": "", "versions": {}, "sweep_base": ""}


def studio_versions() -> dict:
    """Asset→version map of the locked instructions the CURRENT prompt carries
    (provenance — stamped on every swept record). Includes the sweep base engine
    and the reference assets when locked."""
    return dict(_studio_cache.get("versions") or {})


def _studio_block() -> str:
    """Build (and cache) the studio appendix + the Omnivision sweep BASE prompt.

    2026-07-09 (Studio v2 — 8 assets): the locked `sweep` engine is the Deal Sweep
    (Deal Drawer) BASE system prompt (consumed by _load_prompt via the cache's
    `sweep_base`); the OTHER five engines are appended as the authoritative studio
    block, with `{{ref:...}}` citations resolved to pointers and each locked
    REFERENCE ASSET (vendor dictionary, deal playbook) appended exactly once."""
    now = time.time()
    if now - _studio_cache["at"] < _STUDIO_TTL_S:
        return _studio_cache["block"]
    try:
        import scoring_studio as _st
        active = _st.active_locked()
        parts, versions = [], {}
        cited_all = set()
        for eng in _st.ENGINES:
            if eng == "sweep":
                continue   # the sweep engine is the BASE prompt, not an appended block
            row = active.get(eng)
            if not row:
                continue
            versions[eng] = row["version"]
            _txt, _cited = _st.resolve_refs(row["content"], active)
            cited_all |= _cited
            parts.append(f"### ENGINE — {_st.ENGINE_NAMES[eng]} · LOCKED v{row['version']}\n\n{_txt}")
        # Reference assets: append every locked reference ONCE (the sweep engine cites
        # them in prose — vendor dictionary §4.3b, playbook §12 — so include them all).
        ref_txt, ref_versions = _st.reference_sections(active)
        versions.update(ref_versions)
        # The Omnivision Deal Sweep base prompt (tokens resolved against the same refs).
        _sw_row = active.get("sweep")
        if _sw_row and (_sw_row.get("content") or "").strip():
            versions["sweep"] = _sw_row["version"]
            _sw_txt, _ = _st.resolve_refs(_sw_row["content"], active)
            _studio_cache["sweep_base"] = _sw_txt
        else:
            _studio_cache["sweep_base"] = ""
        if parts or ref_txt:
            head = ("\n\n# SCORING VERSION STUDIO — LOCKED ENGINE INSTRUCTIONS (AUTHORITATIVE)\n"
                    "The instructions below are the versioned, LOCKED governing instructions "
                    "(edited in Omnivision). They are the CURRENT operating law for signal extraction, "
                    "win-position reading, momentum reading, to-do generation and the 24-hour summary — "
                    "where anything above conflicts with them, THESE WIN. Provenance: "
                    + " · ".join(f"{e} v{v}" for e, v in versions.items()) + "\n\n")
            block = head + "\n\n".join(parts)
            if ref_txt:
                block += ("\n\n# REFERENCE ASSETS (LOCKED — cited by the engines above)\n\n"
                          + ref_txt)
            _studio_cache.update(at=now, block=block, versions=versions)
        else:
            _studio_cache.update(at=now, block="", versions={})
    except Exception as _e:  # noqa: BLE001 — keep the previous block; never stall the sweep
        print(f"[DEAL-SWEEP] studio instructions read failed ({_e}); keeping cached block", flush=True)
        _studio_cache["at"] = now
    return _studio_cache["block"]


def _prompt_fingerprint(text: str) -> str:
    """A short, loggable identity for a prompt: its first line + a content hash, so
    a restart/reset can confirm WHICH prompt version is live (the agent is cached,
    so editing the file alone does not take effect until rebuild)."""
    first = (text.splitlines()[0].strip() if text else "")[:80]
    h = hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:12]
    return f"sha256={h} first_line={first!r}"


_FRONTIER_DEFAULT = "anthropic:claude-sonnet-5"
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
            # 64000 = Claude Sonnet 4.5's max output. Evidence-heavy deals produce a
            # record JSON that overflowed the old 32000 ceiling, truncating mid-object
            # -> json_parse_failed. Doubling the room lets the full record finish.
            max_tokens=int(os.getenv("DEAL_SWEEP_MAX_TOKENS", "64000")),
            stop=None,
        )
    from langchain.chat_models import init_chat_model
    return init_chat_model(selected)


def reset():
    """Drop the cached agent (call after an MCP reload, or after the admin edits
    the deal-sweep prompt in Supabase so the new prompt rebuilds immediately)."""
    global _cached_agent, _cached_tool_names, _cached_prompt_fp, _cached_prompt_checked_at
    _cached_agent = None
    _cached_tool_names = []
    _cached_prompt_fp = ""
    _cached_prompt_checked_at = 0.0
    try:
        # Log the disk seed only (a pure file read) — reset() can run inside the
        # request loop and we don't want a sync Supabase call here. The EFFECTIVE
        # prompt (Supabase override or seed) is resolved on the next _get_agent.
        print(f"[DEAL-SWEEP] agent cache reset; disk seed prompt: "
              f"{_prompt_fingerprint(_disk_prompt())}", flush=True)
    except Exception as _e:  # noqa: BLE001
        print(f"[DEAL-SWEEP] agent cache reset (prompt read failed: {_e})", flush=True)


async def _get_agent(agent_manager):
    global _cached_agent, _cached_tool_names, _cached_prompt_fp, _cached_prompt_checked_at
    async with _agent_lock:
        # The system prompt lives in Supabase and is admin-editable, so re-resolve
        # it (TTL-throttled) and rebuild the cached agent if it changed since we
        # last built. This makes an admin edit take effect on the next opp in BOTH
        # the API and the worker process, with no redeploy. The read is offloaded
        # to a thread (sync httpx) so it never blocks the loop, and _load_prompt
        # degrades to the on-disk seed on any Supabase error.
        now = time.time()
        if _cached_agent is not None and (now - _cached_prompt_checked_at) < _PROMPT_RECHECK_TTL_S:
            return _cached_agent
        _prompt_text = await asyncio.get_running_loop().run_in_executor(None, _load_prompt)
        _cached_prompt_checked_at = now
        _fp = _prompt_fingerprint(_prompt_text)
        if _cached_agent is not None and _fp == _cached_prompt_fp:
            return _cached_agent
        if _cached_agent is not None:
            print(f"[DEAL-SWEEP] prompt changed ({_cached_prompt_fp} -> {_fp}); "
                  f"rebuilding agent", flush=True)
        tools = _oa._collect_scoped_tools(agent_manager)
        if not tools:
            raise RuntimeError(
                "deal_engine_sweep: no salesforce/avoma tools loaded yet "
                "(agent_manager._cached_mcp_tools_by_server empty)"
            )
        # Give the sweep the search_knowledge tool too, so it can fetch MASE knowledge
        # docs (playbooks/guides) while analysing a deal — the SAME isolated MASE store
        # the todo-runner uses. analyze_one sets rag_context to the MASE namespace so
        # search_knowledge routes to the MASE tables.
        for _ct in (getattr(agent_manager, "_cached_custom_tools", []) or []):
            if getattr(_ct, "name", "") == "search_knowledge":
                tools = tools + [_ct]
                break
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
        # _prompt_text + _fp were resolved above (Supabase override else disk seed).
        try:
            _src = "supabase-override" if _fp != _prompt_fingerprint(_disk_prompt()) else "disk-seed"
        except Exception:  # noqa: BLE001 — labelling only; never fail the build on it
            _src = "unknown"
        print(f"[DEAL-SWEEP] system prompt loaded ({_src}): "
              f"{_prompt_fingerprint(_prompt_text)}", flush=True)
        _cached_agent = create_deep_agent(
            tools=tools,
            system_prompt=_prompt_text,
            subagents=[],
            model=_build_model(),
            middleware=middleware,
            debug=False,
        )
        _cached_prompt_fp = _fp
        return _cached_agent


def _load_revops_prompt() -> str:
    """Effective RevOps Head prompt — Supabase (ID_REVOPS_HEAD) else the on-disk
    seed (prompts/mase_revops_head.md, from '## Who you are'). Never raises."""
    try:
        import agent_prompt_store as _aps
        override = (_aps.get_prompt(_aps.ID_REVOPS_HEAD) or "").strip()
        if override:
            return override
        import pathlib
        seed = pathlib.Path(__file__).with_name("prompts") / "mase_revops_head.md"
        txt = _aps.strip_leading_banner(seed.read_text(encoding="utf-8"))
        i = txt.find("## Who you are")
        return txt[i:] if i >= 0 else txt
    except Exception as _e:  # noqa: BLE001 — never block the sweep on this read
        print(f"[REVOPS-HEAD] prompt read failed ({_e})", flush=True)
        return ""


async def _revops_head_review(parsed: dict, opp: dict, opp_id: str) -> dict:
    """RevOps Head strategic review (Deal Sweep January 1.0). Runs LAST, AFTER the
    compliance QI, on FORECASTED deals only (staffing_plan gates it to Commit /
    Best Case / Upside Key Deal), behind REVOPS_HEAD_ENABLED. Works ONLY from the
    gate-clean record (no tools,
    no fetch — it cannot introduce a new name/fact). On ANY error, when disabled,
    or on a lean deal, returns `parsed` UNCHANGED — never blocks persist."""
    if os.getenv("REVOPS_HEAD_ENABLED", "false").lower() not in ("1", "true", "yes"):
        return parsed
    try:
        import deal_engine_qi as _qigate
        fc = opp.get("forecast_category")
        forecasted = (fc or "").strip().lower() in _qigate.FORECASTED
        ev = (parsed.get("evidence_coverage")
              or (parsed.get("ai") or {}).get("evidence_coverage") or {})
        calls = int(ev.get("calls_read") or ev.get("calls_found") or 0)
        try:
            amount = float(opp.get("amount") or 0)
        except Exception:  # noqa: BLE001
            amount = 0.0
        # richness_score scale is not normalised to 0-1, so don't let it drive the
        # tier — gate staffing on the reliable signals (forecast / amount / calls).
        plan = _qigate.staffing_plan(calls_read=calls, richness_score=0.0,
                                     forecasted=forecasted, amount=amount)
        if not plan.get("revops_head_review"):
            return parsed  # lean deal — skip the expensive senior review
        prompt = await asyncio.get_running_loop().run_in_executor(None, _load_revops_prompt)
        if not prompt:
            return parsed
        from langchain_core.messages import SystemMessage, HumanMessage
        user = ("Here is the gate-clean canonical record for this deal. Review it "
                "per your remit and return the FULL record as ONE JSON object with "
                "the `ai` block revised (re-ranked / sharpened moves, tightened "
                "verdict) and `ai.revops_review` added. Change nothing about the "
                "hard facts.\n\n" + json.dumps(parsed, ensure_ascii=False))
        resp = await asyncio.wait_for(
            _build_model().ainvoke(
                [SystemMessage(content=prompt), HumanMessage(content=user)]),
            timeout=float(os.getenv("REVOPS_HEAD_TIMEOUT_S", "150")))
        revised = _oa._extract_json(getattr(resp, "content", "") or "")
        new_ai = revised.get("ai") if isinstance(revised, dict) else None
        if not isinstance(new_ai, dict) or not new_ai:
            return parsed  # malformed — keep the gate-clean original
        # MERGE over the projected ai: the RevOps Head's revised fields (re-ranked
        # moves, tightened verdict) + the new ai.revops_review WIN (latest/greatest
        # first), but any field the editor omitted falls back to the accurate
        # living-memory-projected value — so nothing good/accurate is dropped.
        parsed["ai"] = {**(parsed.get("ai") or {}), **new_ai}
        # Defense-in-depth: re-run the escalation gate — the RevOps Head must never
        # reintroduce a VP/manager escalation on a non-forecasted deal.
        _ev2, parsed = _qigate.check_escalation(parsed, fc)
        print(f"[REVOPS-HEAD] opp={opp_id} reviewed (tier={plan['tier']})", flush=True)
    except Exception as _re:  # noqa: BLE001 — the review must never block persist
        print(f"[REVOPS-HEAD] opp={opp_id} non-fatal: "
              f"{type(_re).__name__}: {_re}", flush=True)
    return parsed


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


# --- IDENTIFIER POLICY: a primary key is NEVER truncated for an external lookup ---
# A Salesforce Id has two forms: 15-char (case-sensitive) and 18-char
# (case-insensitive = the 15-char + a 3-char checksum). Avoma — and other systems —
# file records under the 18-char Id, so a 15-char id matches NOTHING and silently
# returns zero. ALWAYS normalise a Salesforce Id to its canonical 18-char form
# before any external lookup; never slice it. (Meeting / recording ids are UUIDs —
# likewise always passed whole, never sliced.)
_SFID_SUFFIX = "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"


def sf_id_18(oid: str) -> str:
    """Canonical 18-char Salesforce Id. Converts a 15-char Id to 18-char by
    appending the checksum; an already-18-char Id (or anything not exactly 15
    chars) passes through unchanged. NEVER truncates."""
    if not isinstance(oid, str):
        return oid
    oid = oid.strip()
    if len(oid) != 15:
        return oid
    suffix = ""
    for chunk in (oid[0:5], oid[5:10], oid[10:15]):
        v = 0
        for i, ch in enumerate(chunk):
            if "A" <= ch <= "Z":
                v |= 1 << i
        suffix += _SFID_SUFFIX[v]
    return oid + suffix


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


# --- MEDDPICC custom-object pull (CRM-entered) -----------------------------
# Reps fill MEDDPICC into two SF custom objects: MEDDPICC__c (human-entered,
# authoritative — clean named economic buyer) and MEDDPICC_2_0__c (auto-synced,
# sometimes an org-chart in the EB field). We pull both, PREFER __c, and feed the
# agent a CRM HINT it must corroborate against calls + recent activity, dropping
# anything dated/contradicted. Flag-gated (on by default). Best-effort; the queries
# are wrapped so a bad field name / SF blip degrades to "no MEDDPICC" and never
# blocks the sweep.
_MEDDPICC_FETCH = os.getenv(
    "DEAL_SWEEP_MEDDPICC_FETCH", "true").lower() in ("1", "true", "yes", "on")
# logical label -> (MEDDPICC__c field, MEDDPICC_2_0__c field); None = not on that object.
_MEDDPICC_FIELDS: list = [
    ("Economic buyer", "Who_is_the_economic_buyer__c", "Who_is_the_economic_buyer__c"),
    ("Budget", "What_is_the_budget__c", None),
    ("Budget owner", "Who_Own_s_the_budget__c", "Who_owns_the_budget__c"),
    ("Metrics", "Metrics_Important_to_Buyer__c", None),
    ("Decision criteria", "Decision_Criteria__c", "Decision_criteria__c"),
    ("Decision process", "Purchase_Process__c", "Purchase_process__c"),
    ("Identify pain", "What_problem_is_Zycus_solving__c", "What_problem_is_Zycus_solving__c"),
    ("Champion", "Champion_for_Zycus__c", "Champion_for_Zycus__c"),
    ("Competition", "Competition_and_our_differentiator__c", "Competition_and_our_differentiator__c"),
    ("Blockers", "Any_blockers__c", "Any_blockers__c"),
    ("Products considered", "Products_being_considered__c", "Products_being_considered__c"),
]
_MEDDPICC_NULLISH = {"", "n.a.", "na", "n/a", "none", "no", "-", "unknown", "tbd"}


# MEDDPICC label -> rubric factor (presence = good). Competition excluded (a named rival
# isn't necessarily favourable); preference has no direct CRM field.
_CRM_LABEL_TO_FACTOR = {
    "economic buyer": "exec_access",
    "champion": "champion",
    "metrics": "business_case",
    "identify pain": "differentiation",
    "decision process": "commercial",
}


def _crm_evidence_from(meddpicc_data: dict) -> dict:
    """Parse _meddpicc_crm() output into {factor: {present, value, src, age_days}} for the
    deterministic Win-rubric overlay. Recency from the record's last_modified."""
    if not isinstance(meddpicc_data, dict):
        return {}
    fields = meddpicc_data.get("fields") or []
    age = None
    lm = meddpicc_data.get("last_modified")
    if lm:
        try:
            from datetime import datetime, timezone
            d = datetime.fromisoformat(str(lm).replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            age = max(0, int((datetime.now(timezone.utc) - d).total_seconds() // 86400))
        except Exception:  # noqa: BLE001
            age = None
    out = {}
    for label, value, src in fields:
        fac = _CRM_LABEL_TO_FACTOR.get(str(label or "").strip().lower())
        if fac and str(value or "").strip():
            out[fac] = {"present": True, "value": str(value)[:120], "src": src, "age_days": age}
    return out


# --- Deterministic Win-rubric keyword overlay (playbook "next step") ----------------
# The MEDDPICC overlay (_crm_evidence_from) only covers factors that have a MEDDPICC custom
# field filled. The RICHEST rubric evidence — especially customer PREFERENCE (weight 20, no
# MEDDPICC field) — lives as free text in the Next-Step log + opp narrative. Scan it
# deterministically (NO LLM) so a champion / EB / preference that appears ONLY there still
# lifts Win. Presence-based; MAX-merged into crm_evidence so it can never HIDE a factor.
_RUBRIC_SCAN = os.getenv("DEAL_SWEEP_RUBRIC_SCAN", "true").lower() in ("1", "true", "yes", "on")
_RUBRIC_KEYWORDS = {
    "preference": (
        "favored vendor", "favoured vendor", "preferred vendor", "preferred partner",
        "front runner", "front-runner", "frontrunner", "leading vendor", "in the lead",
        "zycus is ahead", "ahead of competition", "ahead of the competition", "selected zycus",
        "shortlisted zycus", "go with zycus", "going with zycus", "recommend zycus",
        "chose zycus", "chosen zycus", "prefer zycus", "preference for zycus", "positive feedback",
        "very impressed", "favourable", "favorable", "leaning toward zycus", "leaning towards zycus",
        "zycus is the front", "zycus in the lead", "winning the deal",
    ),
    "champion": ("champion", "internal sponsor", "advocate for us", "our advocate",
                 "is sponsoring", "strong supporter", "backing zycus"),
    "exec_access": (
        "economic buyer", " cpo", "cpo ", " cfo", "cfo ", " cio", "cio ", " cto", "cto ",
        "chief procurement", "chief financial", "chief information", "vp procurement",
        "vp of procurement", "svp", "c-level", "c level", "executive sponsor",
        "decision maker", "final decision",
    ),
    "business_case": (
        "business case", " roi", "roi ", "return on investment", "cost saving", "cost reduction",
        "savings", " tco", "total cost of ownership", "payback", "value case", "quantified value",
    ),
    "differentiation": (
        "pain point", "key challenge", "manual process", "inefficien", "consolidat",
        "ai capabilit", "automation", "differentiat", "only vendor", "stands out",
        "compelling event", "burning platform",
    ),
    "commercial": (
        "pricing", "commercials", "proposal sent", "sent the proposal", "quote", "contract draft",
        "procurement process", "paper process", " msa", "redline", "legal review",
        "purchase order", " sow", "negotiation",
    ),
}


def _rubric_text_scan(text: str) -> dict:
    """{factor: matched_phrase} for any rubric keyword found (case-insensitive). Presence-based."""
    s = " " + str(text or "").lower() + " "
    hits = {}
    if len(s) < 5:
        return hits
    for fac, phrases in _RUBRIC_KEYWORDS.items():
        for p in phrases:
            if p in s:
                hits[fac] = p.strip()
                break
    return hits


def _merge_crm_evidence(base: dict, extra: dict) -> dict:
    """MAX-merge two crm_evidence dicts: a factor is present if EITHER source has it; keep the
    FRESHER age. Never downgrades a factor already present (MEDDPICC stays authoritative)."""
    out = dict(base or {})
    for fac, info in (extra or {}).items():
        if not (isinstance(info, dict) and info.get("present")):
            continue
        cur = out.get(fac)
        if not (isinstance(cur, dict) and cur.get("present")):
            out[fac] = info
        else:
            a, b = cur.get("age_days"), info.get("age_days")
            if a is None or (b is not None and b < a):
                out[fac] = info
    return out


async def _rubric_crm_scan(agent_manager, opp_id: str) -> dict:
    """Deterministic Win-rubric overlay from the Next-Step log + opp narrative free text
    (playbook 'next step'). Returns {factor: {present, value, src, age_days}}, MAX-merged into
    crm_evidence by the caller. Best-effort: one SOQL, fully wrapped, never blocks the sweep."""
    if not (_RUBRIC_SCAN and opp_id):
        return {}
    sid = _sql_str(str(opp_id))
    try:
        rows = await _soql(agent_manager,
            "SELECT Next_Step__c, Next_Step_History__c, Description, "
            "Customer_Business_Problem__c, Compelling_Event__c, Next_Step_Updated_Date_Time__c "
            f"FROM Opportunity WHERE Id = '{sid}' LIMIT 1")
    except Exception as _e:  # noqa: BLE001
        print(f"[DEAL-SWEEP] rubric-scan read failed opp={opp_id}: {_e}", flush=True)
        return {}
    o = (rows[0] if rows else {}) or {}
    age = None
    nsu = o.get("Next_Step_Updated_Date_Time__c")
    if nsu:
        try:
            from datetime import datetime, timezone
            d = datetime.fromisoformat(str(nsu).replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            age = max(0, int((datetime.now(timezone.utc) - d).total_seconds() // 86400))
        except Exception:  # noqa: BLE001
            age = None
    blob = " || ".join(str(o.get(f) or "") for f in
                       ("Next_Step__c", "Next_Step_History__c", "Description",
                        "Customer_Business_Problem__c", "Compelling_Event__c"))
    out = {}
    for fac, phrase in _rubric_text_scan(blob).items():
        out[fac] = {"present": True, "value": f"'{phrase}' in Next-Step/narrative",
                    "src": "Next-Step/narrative", "age_days": age}
    return out


# --- Decision-outcome detector: an explicit WIN/LOSS in the latest call/notes/Next-Step ---
# A deal can be lost (or won) on a call DAYS before Salesforce is updated. When the most
# recent transcript/AI-notes say we lost, the deal is over no matter how much activity
# preceded it — scoring must hard-override to 0, not read "healthy". HIGH-PRECISION phrases
# only (a false loss is costly): generic words like "other vendor" alone do NOT trigger.
_LOSS_PHRASES = (
    "finished as the runner-up", "finished as runner-up", "finished second",
    "zycus finished as", "second winner", "the second winner",
    "selected the competing vendor", "selected the other vendor", "selected another vendor",
    "chose the other vendor", "chose the competitor", "went with the competitor",
    "went with another vendor", "awarded to the other", "we lost the deal",
    "lost the deal to", "lost to coupa", "lost to the competitor", "deal is lost",
    "have not been selected", "were not selected", "not been selected as",
    "decided against zycus", "decided not to proceed with zycus", "deprioritized zycus",
    "selected coupa", "chosen coupa", "awarded to coupa", "going with coupa",
    "selected the incumbent", "zycus came second", "runner-up",
)
_WON_PHRASES = (
    "selected zycus", "awarded to zycus", "chosen zycus", "zycus is the winner",
    "zycus has been selected", "awarded the deal to zycus", "decided to go with zycus",
    "we won the deal", "selected us as the vendor", "zycus as the preferred vendor and will",
    "verbal commitment to zycus", "signed with zycus",
)


def _detect_decision_outcome(prefetch: dict, next_step_text: str = "") -> dict:
    """Scan the MOST RECENT call notes/transcript (+ Next-Step) for an explicit win/loss.
    A detected LOSS hard-overrides the score to 0 downstream, so this is high-precision.
    Returns {status: 'lost'|'won'|'none', confidence, matched, evidence, source}."""
    out = {"status": "none"}
    calls = (prefetch or {}).get("calls") or (prefetch or {}).get("manifest") or []
    recent = sorted([c for c in calls if c.get("date")],
                    key=lambda c: str(c.get("date") or ""), reverse=True)[:3]
    blobs = [(c, " ".join(str(c.get(k) or "") for k in
                          ("notes", "transcript_excerpt", "subject")).lower()) for c in recent]
    blobs.append(({"subject": "Next Step", "date": None}, str(next_step_text or "").lower()))
    for c, t in blobs:
        if not t:
            continue
        for p in _LOSS_PHRASES:
            if p in t:
                i = t.find(p)
                return {"status": "lost", "confidence": "high", "matched": p,
                        "evidence": t[max(0, i - 90):i + 110].strip(),
                        "source": f"{c.get('subject') or 'call'} ({str(c.get('date') or '')[:10]})"}
        for p in _WON_PHRASES:
            if p in t:
                i = t.find(p)
                return {"status": "won", "confidence": "high", "matched": p,
                        "evidence": t[max(0, i - 90):i + 110].strip(),
                        "source": f"{c.get('subject') or 'call'} ({str(c.get('date') or '')[:10]})"}
    return out


async def _footprints_for(agent_manager, opp_id: str, stage: str, avoma_meeting_dates=None) -> dict:
    """Deterministic engagement/liveness footprints from SF Tasks + Events + opp summary
    fields, PLUS the deal's real Avoma meetings from the datalake (authoritative meeting
    dates — opp/account/domain matched, so the meeting COUNT is real, not guessed from SF
    subject keywords). Classifies each by buyer-vs-rep direction and engagement DEPTH
    (POC/workshop/F2F/demo...). Feeds Deal Momentum v2. Returns {} on failure (best-effort)."""
    if not opp_id:
        return {}
    import deal_engine_footprints as _fp
    sid = _sql_str(str(opp_id))
    tasks, events, opp = [], [], {}
    try:
        rows = await _soql(agent_manager,
            f"SELECT Subject, ActivityDate, Type FROM Task WHERE WhatId = '{sid}' "
            f"AND ActivityDate >= LAST_N_DAYS:120 ORDER BY ActivityDate DESC LIMIT 60")
        tasks = [{"subject": r.get("Subject"), "date": r.get("ActivityDate"),
                  "type": r.get("Type")} for r in (rows or [])]
    except Exception as _e:  # noqa: BLE001
        print(f"[FOOTPRINTS] task read failed opp={opp_id}: {_e}", flush=True)
    try:
        rows = await _soql(agent_manager,
            f"SELECT Subject, ActivityDateTime FROM Event WHERE WhatId = '{sid}' "
            f"AND ActivityDateTime >= LAST_N_DAYS:120 ORDER BY ActivityDateTime DESC LIMIT 40")
        events = [{"subject": r.get("Subject"), "date": r.get("ActivityDateTime")}
                  for r in (rows or [])]
    except Exception as _e:  # noqa: BLE001
        print(f"[FOOTPRINTS] event read failed opp={opp_id}: {_e}", flush=True)
    try:
        rows = await _soql(agent_manager,
            "SELECT Last_Email_Received_Date__c, Last_Meeting_Date__c, "
            "Next_Step_Updated_Date_Time__c, No_activity_in_last_20_30_Days__c, "
            f"LastActivityDate FROM Opportunity WHERE Id = '{sid}' LIMIT 1")
        opp = (rows[0] if rows else {}) or {}
    except Exception as _e:  # noqa: BLE001
        print(f"[FOOTPRINTS] opp-fields read failed opp={opp_id}: {_e}", flush=True)
    # DIRECT-SOQL FALLBACK (2026-07-07): the agent/MCP SOQL above is flaky — when it returns
    # empty, footprints silently don't get built and Momentum/Win crater to "cold" on genuinely
    # engaged deals (Nidec: 12 buyer touches/30d read win 5, mom 25). If the MCP path found NO
    # tasks AND NO events, re-read them via the reliable server-side Salesforce session before
    # giving up — the data is there, only the MCP hiccupped.
    if not tasks and not events:
        try:
            import asyncio as _asyncio
            from daily_summary import common as _C

            def _direct():
                s, inst = _footprints_sf()
                if not s:
                    return [], [], {}
                tk = _C.soql(s, inst, f"SELECT Subject,Type,CreatedDate,ActivityDate FROM Task WHERE WhatId='{sid}' AND CreatedDate>=LAST_N_DAYS:180 LIMIT 400")
                ev = _C.soql(s, inst, f"SELECT Subject,Type,ActivityDateTime,CreatedDate FROM Event WHERE WhatId='{sid}' AND (ActivityDateTime>=LAST_N_DAYS:180 OR CreatedDate>=LAST_N_DAYS:180) LIMIT 200")
                op = _C.soql(s, inst, f"SELECT Last_Email_Received_Date__c,Last_Meeting_Date__c,Next_Step_Updated_Date_Time__c,No_activity_in_last_20_30_Days__c,LastActivityDate FROM Opportunity WHERE Id='{sid}' LIMIT 1")
                return ([{"subject": r.get("Subject"), "date": r.get("CreatedDate") or r.get("ActivityDate"), "type": r.get("Type")} for r in (tk or [])],
                        [{"subject": r.get("Subject"), "date": r.get("ActivityDateTime") or r.get("CreatedDate")} for r in (ev or [])],
                        (op[0] if op else {}) or {})
            _dt, _de, _do = await _asyncio.get_running_loop().run_in_executor(None, _direct)
            if _dt or _de:
                tasks, events, opp = _dt, _de, (_do or opp)
                print(f"[FOOTPRINTS] direct-SOQL fallback recovered opp={opp_id} "
                      f"(tasks={len(tasks)} events={len(events)}) — MCP read had returned empty", flush=True)
        except Exception as _fbe:  # noqa: BLE001
            print(f"[FOOTPRINTS] direct fallback failed opp={opp_id}: {_fbe}", flush=True)
    try:
        return _fp.derive_footprints(tasks=tasks, opp=opp, events=events, stage=stage,
                                     meeting_dates=avoma_meeting_dates)
    except Exception as _e:  # noqa: BLE001
        print(f"[FOOTPRINTS] derive failed opp={opp_id}: {_e}", flush=True)
        return {}


# Cached server-side Salesforce session for the footprints direct-SOQL fallback (logging in
# per deal is expensive). Lazy, best-effort — returns (None, None) if unavailable.
_FP_SF = {"sid": None, "inst": None}


def _footprints_sf():
    if _FP_SF["sid"]:
        return _FP_SF["sid"], _FP_SF["inst"]
    try:
        from daily_summary import common as _C
        s, inst = _C.sf_login(_C.load_secret())
        _FP_SF["sid"], _FP_SF["inst"] = s, inst
        return s, inst
    except Exception as _e:  # noqa: BLE001
        print(f"[FOOTPRINTS] direct SF login failed: {_e}", flush=True)
        return None, None


async def _meddpicc_crm(agent_manager, opp_id: str) -> dict:
    """Pull CRM-entered MEDDPICC for the opp (MEDDPICC__c preferred, then 2.0).
    Returns {"fields": [(label, value, src)], "last_modified": str} or {}."""
    if not (_MEDDPICC_FETCH and opp_id):
        return {}
    sid, sid15 = _sql_str(str(opp_id)), _sql_str(str(opp_id)[:15])
    where = f"(Opportunity_Name__c = '{sid}' OR Opportunity_Name__c = '{sid15}')"
    primary: dict = {}
    secondary: dict = {}
    try:
        cols = ", ".join(sorted({f for _, f, _ in _MEDDPICC_FIELDS if f}))
        rows = await _soql(agent_manager,
            f"SELECT Id, LastModifiedDate, {cols} FROM MEDDPICC__c "
            f"WHERE {where} ORDER BY LastModifiedDate DESC LIMIT 1")
        primary = (rows[0] if rows else {}) or {}
    except Exception as _e:  # noqa: BLE001
        print(f"[DEAL-SWEEP] MEDDPICC__c read failed opp={opp_id}: {_e}", flush=True)
    try:
        cols = ", ".join(sorted({f for _, _, f in _MEDDPICC_FIELDS if f}))
        rows = await _soql(agent_manager,
            f"SELECT Id, LastModifiedDate, {cols} FROM MEDDPICC_2_0__c "
            f"WHERE {where} ORDER BY LastModifiedDate DESC LIMIT 1")
        secondary = (rows[0] if rows else {}) or {}
    except Exception as _e:  # noqa: BLE001
        print(f"[DEAL-SWEEP] MEDDPICC_2_0__c read failed opp={opp_id}: {_e}", flush=True)
    fields: list = []
    for label, f1, f2 in _MEDDPICC_FIELDS:
        v1 = str(primary.get(f1) or "").strip() if f1 else ""
        v2 = str(secondary.get(f2) or "").strip() if f2 else ""
        if v1 and v1.lower() not in _MEDDPICC_NULLISH:
            fields.append((label, v1, "MEDDPICC"))
        elif v2 and v2.lower() not in _MEDDPICC_NULLISH:
            fields.append((label, v2, "MEDDPICC 2.0"))
    if not fields:
        return {}
    return {"fields": fields,
            "last_modified": primary.get("LastModifiedDate") or secondary.get("LastModifiedDate")}


def _meddpicc_crm_block(data: dict) -> str:
    """Render CRM-entered MEDDPICC as an agent evidence block — a hint to corroborate,
    with explicit instruction to drop dated/contradicted items."""
    if not data or not data.get("fields"):
        return ""
    lm = str(data.get("last_modified") or "unknown")[:10]
    out = [
        f"\n\n=== MEDDPICC (CRM-entered · last updated {lm}) ===",
        "Treat as a STARTING HINT from the CRM, not ground truth. Corroborate each item "
        "against the calls and recent activity above; if a field is contradicted by newer "
        "evidence or clearly dated/superseded, DOWN-WEIGHT or DROP it. Where a named "
        "economic buyer is given here and not contradicted, economic_buyer is CONFIRMED "
        "(not a gap) — cite this CRM source in its narrative.",
    ]
    for label, val, src in data["fields"]:
        v = " ".join(str(val).split())
        out.append(f"- {label} [{src}]: {v[:600] + ('…' if len(v) > 600 else '')}")
    return "\n".join(out)


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
            "geography": o.get("Geography__c"),
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
    "Amount, CloseDate, Geography__c, "
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
                # Key on the canonical 18-char Id (never truncate a primary key):
                # SOQL returns 18-char, report exports are often 15-char; sf_id_18
                # normalises both to the same 18-char key so they reconcile.
                found[sf_id_18(o["id"] or "")] = o
        except Exception as e:  # noqa: BLE001 — labels are best-effort
            print(f"[DEAL-SWEEP] enrich chunk failed: {type(e).__name__}: {e}", flush=True)
    out: list[dict] = []
    for oid in opp_ids:
        m = found.get(sf_id_18(oid or ""))
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


# Known SI / consultancy / channel-partner domains. When several reps from one of
# these sit on an account, the dominant-domain cluster MUST NOT absorb their domain
# as a "buyer alias" — that mis-read RBA as well-threaded when its only contacts
# were Atos. Backstop alongside the dominant-domain test.
_SI_DOMAINS = {
    "atos.com", "atos.net", "accenture.com", "deloitte.com", "pwc.com",
    "ey.com", "kpmg.com", "wipro.com", "tcs.com", "infosys.com",
    "cognizant.com", "capgemini.com", "ibm.com", "dxc.com", "hcltech.com",
    "techmahindra.com", "ltimindtree.com", "wheelsontech.com",
}

# Name tokens that mark a shared mailbox / meeting room / distribution list posing
# as a contact. These are NOT people and must not inflate the buyer-side count.
# Conservative (name-pattern only) to avoid dropping a real contact.
_NONPERSON_TOKENS = (
    "meeting room", "conference room", "boardroom", "salle de", "salle ",
    "mailbox", "distribution list", "shared mailbox", "no-reply", "noreply",
    "do not reply", " dl ",
)


def _is_nonperson(name: Optional[str], email: Optional[str]) -> bool:
    """True for room/mailbox/DL 'contacts' that inflate buyer_roles_count with
    non-people (e.g. 'Victoria Hong Kong Meeting Room', 'Salle De Conference')."""
    n = (name or "").lower()
    return any(tok in n for tok in _NONPERSON_TOKENS)


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
    out = {"account_name": None, "account_id": None, "self_name": None,
           "website": None, "domains": [], "contacts": [], "account_contacts": [],
           "task_contacts": [], "sibling_opps": [], "contact_roles_thin": False,
           "roles_count": 0, "buyer_roles_count": 0, "partner_count": 0,
           "nonperson_count": 0, "last_activity_date": None}
    sid = _sql_str(opp_id)
    try:
        head = await _soql(
            agent_manager,
            f"SELECT AccountId, Account.Name, Account.Website, Name, LastActivityDate "
            f"FROM Opportunity WHERE Id = '{sid}'")
        if head:
            h = head[0]
            out["account_id"] = h.get("AccountId") or _sf_name(h, "Account", "Id")
            out["account_name"] = _sf_name(h, "Account", "Name") or out["account_name"]
            out["website"] = _sf_name(h, "Account", "Website")
            out["self_name"] = h.get("Name")
            out["last_activity_date"] = h.get("LastActivityDate")
    except Exception as e:  # noqa: BLE001 — prefetch is best-effort
        print(f"[DEAL-SWEEP] buyer-identity head failed opp={opp_id}: "
              f"{type(e).__name__}: {e}", flush=True)
    try:
        roles = await _soql(
            agent_manager,
            f"SELECT Contact.Name, Contact.Title, Contact.Email, Contact.Account.Name, "
            f"Role, IsPrimary "
            f"FROM OpportunityContactRole WHERE OpportunityId = '{sid}'")
        for r in roles or []:
            nm = _sf_name(r, "Contact", "Name")
            if not nm:
                continue
            out["contacts"].append({
                "name": nm,
                "title": _sf_name(r, "Contact", "Title"),
                "email": _sf_name(r, "Contact", "Email"),
                "company": _sf_name(r, "Contact", "Account", "Name"),
                "domain": _domain_of(_sf_name(r, "Contact", "Email")),
                "role": r.get("Role"),
            })
        out["roles_count"] = len(out["contacts"])
    except Exception as e:  # noqa: BLE001
        print(f"[DEAL-SWEEP] buyer-identity roles failed opp={opp_id}: "
              f"{type(e).__name__}: {e}", flush=True)
    # PARTNER EXCEPTION — a contact role can be a partner / SI / reseller (an SI
    # like ROJO, a channel partner), not a buyer employee. NEVER drop them; they
    # are real stakeholders (often the channel the deal runs through). We tag each
    # role buyer-side vs partner, and base the "thin / single-threaded" judgement
    # on the BUYER-side count so partners can't mask buyer single-threading.
    #
    # We do NOT classify on the website domain alone: a buyer employee can sit on a
    # corporate ALIAS / subsidiary domain that differs from the website (e.g.
    # Fortive's website is fortive.com but employees use ftvbsllc.com). So we build
    # a buyer-domain SET = website domain + every domain that CLUSTERS across the
    # account's own contacts (>=2), and only a domain OUTSIDE that set is a partner.
    # The extra account-domain query is paid ONLY when a role is off the website
    # domain (the common all-on-website case stays free). If we end up with no
    # buyer-domain set at all, we cannot classify -> treat as buyer (never silently
    # de-weight a real contact).
    _web = _domain_of(out.get("website"))
    buyer_domains = {_web} if _web else set()
    off_domain = any(
        c.get("domain") and c["domain"] not in buyer_domains
        for c in out["contacts"])
    if off_domain and out.get("account_id"):
        try:
            rows = await _soql(
                agent_manager,
                f"SELECT Email FROM Contact "
                f"WHERE AccountId = '{_sql_str(out['account_id'])}' "
                f"AND Email != null LIMIT 200")
            counts: dict = {}
            for r in rows or []:
                d = _domain_of(r.get("Email"))
                if d:
                    counts[d] = counts.get(d, 0) + 1
            # Fold in only the DOMINANT contact domain(s) — the account's real
            # workforce domain — NOT every domain on >=2 contacts. A flat >=2 is
            # exploitable: an SI that seats 2+ reps on the account (Atos on RBA)
            # would be absorbed as a "buyer alias" and mask that the account has
            # zero employee contacts. Add a domain only if it is within 50% of the
            # top domain's count, and never if it is a known SI/partner domain.
            if counts:
                _top = max(counts.values())
                _thresh = max(2, _top * 0.5)
                buyer_domains |= {
                    d for d, n in counts.items()
                    if n >= _thresh and d not in _SI_DOMAINS}
        except Exception as e:  # noqa: BLE001
            print(f"[DEAL-SWEEP] buyer-identity account-domain cluster failed "
                  f"opp={opp_id}: {type(e).__name__}: {e}", flush=True)
    buyer_roles = 0
    for c in out["contacts"]:
        cd = c.get("domain")
        c["is_partner"] = bool(cd and buyer_domains and cd not in buyer_domains)
        c["is_nonperson"] = _is_nonperson(c.get("name"), c.get("email"))
        if not c["is_partner"] and not c["is_nonperson"]:
            buyer_roles += 1
    out["buyer_roles_count"] = buyer_roles
    out["partner_count"] = sum(1 for c in out["contacts"] if c.get("is_partner"))
    out["nonperson_count"] = sum(
        1 for c in out["contacts"] if c.get("is_nonperson"))
    # FALLBACK — when the opp is THIN on contact roles (< 3, the multi-thread bar):
    # recover the account's own contacts directly via Contact WHERE AccountId, and
    # the sibling open opps on the account. We MUST query the child object by FK;
    # the gateway never materialises the Account.Contacts child subquery (always
    # [0 records] even when contacts exist), which is what made multi-threaded
    # accounts read as single-threaded/dark. account_contacts are ACCOUNT-level
    # (not opp stakeholders) — used to recover the mailbox-domain set for Avoma
    # attendee matching, surface multi-thread candidates, and flag that the account
    # is not genuinely empty. The thin flag drives a downstream "add contact roles"
    # to-do nudge.
    out["contact_roles_thin"] = out["buyer_roles_count"] < 3
    # Sibling OPEN opps — fetched ALWAYS, NOT gated on thin. Scope ambiguity exists
    # whenever an account runs >1 open deal, regardless of how well-threaded THIS opp
    # is: a healthy deal (e.g. Austrian Post, 21 roles) still shares its account with
    # a Certinal opp whose calls must not be mis-attributed here. The agent uses these
    # to SCOPE-route shared-account calls/stakeholders by call SUBJECT — never by
    # Avoma's opp association (it dumps everything onto one opp / the wrong account).
    # Cheap; usually returns 0.
    if out.get("account_id"):
        try:
            sibs = await _soql(
                agent_manager,
                f"SELECT Id, Name, StageName FROM Opportunity "
                f"WHERE AccountId = '{_sql_str(out['account_id'])}' AND Id != '{sid}' "
                f"AND IsClosed = false ORDER BY CloseDate ASC NULLS LAST LIMIT 15")
            for s in sibs or []:
                nm = _sf_name(s, "Name")
                if not nm:
                    continue
                out["sibling_opps"].append({
                    "id": s.get("Id"),
                    "name": nm,
                    "stage": s.get("StageName"),
                })
        except Exception as e:  # noqa: BLE001
            print(f"[DEAL-SWEEP] buyer-identity sibling-opps failed "
                  f"opp={opp_id}: {type(e).__name__}: {e}", flush=True)
    # Account-contacts fallback — only when THIN: recover the account's own contacts
    # directly via Contact WHERE AccountId (the gateway never materialises the
    # Account.Contacts child subquery). Recovers the bench + mailbox-domain set for
    # Avoma attendee matching; drives the "add contact roles" to-do nudge.
    if out["contact_roles_thin"] and out.get("account_id"):
        acct = _sql_str(out["account_id"])
        try:
            acct_contacts = await _soql(
                agent_manager,
                f"SELECT Name, Title, Email FROM Contact "
                f"WHERE AccountId = '{acct}' "
                f"AND Email != null ORDER BY LastModifiedDate DESC LIMIT 50")
            for c in acct_contacts or []:
                nm = _sf_name(c, "Name")
                if not nm:
                    continue
                out["account_contacts"].append({
                    "name": nm,
                    "title": _sf_name(c, "Title"),
                    "email": _sf_name(c, "Email"),
                })
        except Exception as e:  # noqa: BLE001
            print(f"[DEAL-SWEEP] buyer-identity account-contacts fallback failed "
                  f"opp={opp_id}: {type(e).__name__}: {e}", flush=True)
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
    # Latest INBOUND buyer email — the two-way engagement signal the pulse was blind to.
    # Clari logs incoming emails as Tasks subject "[Clari - Email Received]"; fall back to
    # EmailMessage(Incoming=true). Salesforce LastActivityDate is unreliable for inbound
    # (Clari/EAC-captured replies often don't bump it), so this is the load-bearing source
    # for "the buyer actually replied" — a real two-way touch, not rep outreach.
    try:
        rx = await _soql(
            agent_manager,
            f"SELECT ActivityDate, Subject FROM Task WHERE WhatId = '{sid}' "
            f"AND Subject LIKE '%Email Received%' "
            f"ORDER BY ActivityDate DESC NULLS LAST LIMIT 20")
        dates = [t.get("ActivityDate") for t in (rx or []) if t.get("ActivityDate")]
        if dates:
            out["last_inbound_email_date"] = str(dates[0])[:10]
            out["inbound_email_count"] = len(dates)
    except Exception as e:  # noqa: BLE001
        print(f"[DEAL-SWEEP] buyer-identity inbound-task query failed opp={opp_id}: "
              f"{type(e).__name__}: {e}", flush=True)
    if not out.get("last_inbound_email_date"):
        try:
            em = await _soql(
                agent_manager,
                f"SELECT MessageDate FROM EmailMessage WHERE RelatedToId = '{sid}' "
                f"AND Incoming = true ORDER BY MessageDate DESC LIMIT 1")
            em = em or []
            if em and em[0].get("MessageDate"):
                out["last_inbound_email_date"] = str(em[0]["MessageDate"])[:10]
                out["inbound_email_count"] = out.get("inbound_email_count") or 1
        except Exception as e:  # noqa: BLE001
            print(f"[DEAL-SWEEP] buyer-identity inbound-email fallback failed opp={opp_id}: "
                  f"{type(e).__name__}: {e}", flush=True)
    # Derive the attendee-matching domains: contact emails (opp roles, then the
    # account-contacts fallback) + account website.
    domains: list[str] = []
    for c in out["contacts"] + out["account_contacts"]:
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
    if bi.get("self_name"):
        lines.append(
            f"THIS opportunity (scope anchor — attribute calls/stakeholders to it "
            f"ONLY when the call subject/scope matches): {bi['self_name']}")
    if bi.get("domains"):
        lines.append("Buyer email/website domains (match Avoma attendees on these): "
                     + ", ".join(bi["domains"]))
    contacts = bi.get("contacts") or []
    buyers = [c for c in contacts
              if not c.get("is_partner") and not c.get("is_nonperson")]
    partners = [c for c in contacts if c.get("is_partner")]
    if buyers:
        people = "; ".join(
            f"{c['name']}"
            + (f" ({c['title']})" if c.get("title") else "")
            + (f" <{c['email']}>" if c.get("email") else "")
            + (f" [{c['role']}]" if c.get("role") else "")
            for c in buyers[:20])
        lines.append(
            f"Buyer-side contact roles ({bi.get('buyer_roles_count', len(buyers))}): "
            f"{people}")
    if partners:
        ppl = "; ".join(
            f"{c['name']}"
            + (f" ({c['title']})" if c.get("title") else "")
            + (f" @{c['company']}" if c.get("company") else "")
            + (f" <{c['email']}>" if c.get("email") else "")
            + (f" [{c['role']}]" if c.get("role") else "")
            for c in partners[:20])
        lines.append(
            f"PARTNER / third-party contact roles ({len(partners)}) — already named on "
            "this opp; RETAIN them in full as real stakeholders (an SI/reseller is often "
            "the channel the deal runs through, and partner-led calls run through them). "
            f"Do NOT count them toward buyer multi-threading: {ppl}")
    if bi.get("account_contacts"):
        acct_people = "; ".join(
            f"{c['name']}"
            + (f" ({c['title']})" if c.get("title") else "")
            + (f" <{c['email']}>" if c.get("email") else "")
            for c in bi["account_contacts"][:20])
        lines.append(
            f"Opp is thin on contact roles ({bi.get('roles_count', 0)}); "
            "account-level contacts pulled directly (for domain/mailbox identification "
            "and multi-thread candidates, NOT confirmed opp stakeholders unless a call "
            f"or email proves involvement in THIS opp's scope): {acct_people}")
    if bi.get("sibling_opps"):
        sibs = "; ".join(
            f"{s['name']} [{s.get('stage') or '?'}]"
            for s in bi["sibling_opps"][:15])
        lines.append(
            "OTHER OPEN OPPS ON THIS ACCOUNT (distinct scopes): " + sibs + ". "
            "A shared-account call/stakeholder belongs to THIS opp ONLY if its "
            "subject/scope matches this opp — route by call SUBJECT, NOT by shared "
            "domain and NOT by Avoma's opp association (it mis-attributes across "
            "opps and even across accounts). Calls clearly scoped to a sibling opp "
            "are NOT evidence for this deal.")
    if bi.get("contact_roles_thin"):
        lines.append(
            f"DATA-HYGIENE GAP: only {bi.get('buyer_roles_count', 0)} BUYER-side "
            f"contact role(s) on this opp"
            + (f" (plus {bi.get('partner_count', 0)} partner role(s))"
               if bi.get("partner_count") else "")
            + ", below the multi-thread bar. Emit a to-do to add the missing "
            "buyer-side contact roles to the opportunity (and multi-thread beyond a "
            "single contact). Treat this as an action item, not housekeeping.")
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


# ---------------------------------------------------------------------------
# Parallel Avoma reader prefetch (flag-gated; OFF by default).
#
# The sweep currently runs as ONE deep agent that discovers + reads each Avoma
# call sequentially inside its agent loop — the slow part. When
# DEAL_SWEEP_PARALLEL_READERS is on, we instead discover the deal's calls THREE
# ways (opp / account / attendee email), union+dedupe, match to the buyer, and
# read notes (+transcript) for the matched calls CONCURRENTLY, then hand the
# agent pre-read, speaker-attributed call notes so it SYNTHESISES instead of
# fetching one-by-one. This mirrors _buyer_identity: best-effort, shaped-empty
# on any failure, and it NEVER raises into the sweep. With the flag off none of
# this runs and the agent message is byte-for-byte unchanged.
#
# Avoma tool contract (verified against avoma_mcp_server.py 2026-06-20):
#   - get_all_meetings_for_opportunity(crm_opportunity_id, from_date, to_date)
#   - get_all_meetings_for_account(crm_account_id, from_date, to_date)
#   - get_all_meetings_for_attendee(email, from_date, to_date)   [per-email]
#     (list_meetings(attendee_emails="a,b", from_date, to_date) is the multi-email
#      equivalent; we use the per-attendee tool so each email auto-paginates.)
#   - get_meeting_notes(uuid) / get_meeting_transcript(uuid)
#   Discovery tools return {"meetings": [...]} where each meeting carries
#   uuid, title, start_at, attendees:[{email,name,...}], state, transcript_ready.
# ---------------------------------------------------------------------------

# How far back to discover Avoma calls. Generous floor — a Zycus deal can run
# 12–15 months, and the cross-wired opp association means we lean on the wide
# account/attendee pulls. Env-tunable.
_AVOMA_LOOKBACK_DAYS = int(os.getenv("DEAL_SWEEP_AVOMA_LOOKBACK_DAYS", "540"))
# RECENCY-FIRST discovery (January 2.0 "never-miss" engine): try the most recent
# window first and widen ONLY if it comes back empty. An actively-moving deal
# resolves on the small window (fast); a quiet deal keeps widening (covered); we
# NEVER stop at an absolute zero while older calls still exist. We stop widening
# the instant a window yields >=1 matched call. The last entry is the floor.
_AVOMA_WINDOWS = [int(x) for x in (
    os.getenv("DEAL_SWEEP_AVOMA_WINDOWS", "90,270,540").split(",")) if x.strip()] \
    or [_AVOMA_LOOKBACK_DAYS]
# Hard cap on how many matched calls we DEEP-READ (most-recent first), so a noisy
# account can't blow up token cost / latency. Every matched call still appears in
# the manifest as a dated touchpoint — the cap bounds depth, never coverage.
_AVOMA_MAX_READS = int(os.getenv("DEAL_SWEEP_AVOMA_MAX_READS", "12"))
# Hard cap on concurrent Avoma transcript reads per deal. staffing_plan scales the
# reader pool up to 6 on deep/forecasted deals, but 5-6 wide × ~1MB transcripts
# throttles the DeepAgent/Avoma gateway -> discovery misses -> heavy deals fail.
# Cap it lower so heavy deals complete reliably (slower but they finish). Env-tunable.
_AVOMA_READER_CAP = int(os.getenv("DEAL_SWEEP_AVOMA_READER_CAP", "2"))
# Cap transcript excerpt length per call (chars) so the injected block stays sane.
_AVOMA_TRANSCRIPT_CHARS = int(os.getenv("DEAL_SWEEP_AVOMA_TRANSCRIPT_CHARS", "6000"))
# Cap for the notes/summary fallback excerpt — used when a transcript is absent,
# too long to page through, or errors. (The AI notes ARE the meeting summary.)
_AVOMA_NOTES_CHARS = int(os.getenv("DEAL_SWEEP_AVOMA_NOTES_CHARS", "4000"))
# --- Datalake A/B path budgets (COMPLETE UNITS ONLY — never a sliced transcript) ---
# A transcript is inlined WHOLE or not at all. Verbatim transcripts are allocated to
# the most-recent calls until this total char budget is spent; every other call still
# carries its COMPLETE Avoma AI-notes (a faithful whole-call summary). So a call is
# always represented completely (full transcript OR full summary) or listed as a bare
# touchpoint — but NEVER cut in half.
# Budget kept moderate (~10 full transcripts) so the prompt stays small enough that the
# agent's LLM call finishes inside the Anthropic client timeout. Inlining EVERY call's
# full transcript (e.g. 15+) pushed a single generation past 600s -> APITimeoutError.
# Older calls beyond the budget keep their COMPLETE notes summary (whole-call, faithful),
# so coverage/quality is preserved while latency stays bounded. Tune via the env var.
# Raised 80k->110k (2026-07-08) so a MULTI-PART meeting (e.g. an onsite logged as
# "Teil 1" + "Teil 2") fits WHOLE alongside the newest call — a split onsite is exactly
# how the model came to invent "the CPO never showed up" (it read one part, missed the other).
_AVOMA_DL_TRANSCRIPT_BUDGET = int(os.getenv("DEAL_SWEEP_AVOMA_DL_TRANSCRIPT_BUDGET", "110000"))
# Per-call guard: a single transcript larger than this is NOT inlined verbatim (it
# would crowd out every other call); that call falls back to its complete AI-notes.
# 56k covers the common 46-55k enterprise call; the 100k+ workshops still fall to notes.
_AVOMA_DL_TRANSCRIPT_MAXCHARS = int(os.getenv("DEAL_SWEEP_AVOMA_DL_TRANSCRIPT_MAXCHARS", "56000"))
# Generous guard on the complete-summary notes (these are whole-call summaries, short).
_AVOMA_DL_NOTES_MAXCHARS = int(os.getenv("DEAL_SWEEP_AVOMA_DL_NOTES_MAXCHARS", "12000"))

# Multi-part meeting detector: a single onsite/workshop is frequently logged as several
# same-day Avoma recordings — "Teil 1"/"Teil 2" (German), "Part 1", "Session 2", "(1/2)",
# "Day 1", "Pt 2". These are ONE logical meeting; the transcript budget must inline them
# together (whole meeting or none), never one part while dropping the other — a half-read
# meeting is exactly what let the model invent the missing half ("the CPO never showed up"
# on the 1-Jul onsite, whose Teil 2 — where the CPO spoke — had been dropped to notes-only).
_MEETING_PART_MARKER = re.compile(
    r"\b(?:teil|part|session|sitzung|tag|day|pt|episode|ep)\s*\.?\s*\d+\b"
    r"|\(\s*\d+\s*(?:of|/|von)\s*\d+\s*\)"
    r"|\b\d+\s*/\s*\d+\b", re.I)


def _meeting_group_key(subject: str, date: str) -> tuple:
    """Group same-day recordings of ONE meeting. Strip part markers from the subject and
    key on (day, normalised stem) so 'Zycus (Onsite) Teil 1' and '... Teil 2' on the same
    day collapse to one group. A blank/degenerate stem falls back to the date+subject so
    unrelated same-day calls are never wrongly merged."""
    day = str(date or "")[:10]
    stem = _MEETING_PART_MARKER.sub(" ", str(subject or "")).lower()
    stem = re.sub(r"[\s\-–—:|.,]+", " ", stem).strip()
    if len(stem) < 4:                       # nothing distinctive left — don't over-merge
        return (day, "\x00" + str(subject or "").lower())
    return (day, stem)


def _avoma_window(days: int) -> tuple[str, str]:
    """(from_date, to_date) ISO-Z strings for a `days`-day recency lookback."""
    now = datetime.now(timezone.utc)
    frm = now - timedelta(days=max(1, int(days)))
    # +1 day on the upper bound so calls earlier today are never excluded.
    to = now + timedelta(days=1)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return frm.strftime(fmt), to.strftime(fmt)


def _coerce_obj(raw: Any) -> Any:
    """Normalise an MCP tool result to a Python object (dict/list/str).

    Avoma tool output arrives like the SOQL output _coerce_rows handles: a
    (content, artifact) tuple, a list of {type,text} content blocks, a JSON /
    Python-repr string, or already a dict/list. Returns the parsed object, or the
    raw value if it could not be parsed. Never raises."""
    try:
        if isinstance(raw, tuple) and raw:
            raw = raw[0]
        if isinstance(raw, list) and raw and isinstance(raw[0], dict) and "text" in raw[0] \
                and set(raw[0].keys()) <= {"type", "text", "annotations"}:
            joined = "".join(b.get("text", "") for b in raw if isinstance(b, dict))
            parsed = _parse_maybe(joined)
            if parsed is not None:
                raw = parsed
        if isinstance(raw, str):
            parsed = _parse_maybe(raw)
            if parsed is not None:
                raw = parsed
    except Exception:  # noqa: BLE001 — coercion is best-effort
        return raw
    return raw


def _avoma_meetings_from(raw: Any) -> list[dict]:
    """Pull the meetings list out of a discovery-tool result. The tools return
    {"meetings": [...]} (also tolerate {"results": [...]} / a bare list)."""
    obj = _coerce_obj(raw)
    if isinstance(obj, dict):
        if obj.get("error"):
            return []
        for key in ("meetings", "results", "records", "data"):
            v = obj.get(key)
            if isinstance(v, list):
                return [m for m in v if isinstance(m, dict)]
        return []
    if isinstance(obj, list):
        return [m for m in obj if isinstance(m, dict)]
    return []


def _avoma_attendee_emails(meeting: dict) -> list[str]:
    """Lower-cased attendee emails on a meeting record (attendees:[{email,...}])."""
    out: list[str] = []
    for a in meeting.get("attendees") or []:
        if isinstance(a, dict):
            em = a.get("email")
        elif isinstance(a, str):
            em = a
        else:
            em = None
        if em and isinstance(em, str) and "@" in em:
            out.append(em.strip().lower())
    return out


def _avoma_meeting_matches_buyer(meeting: dict, buyer_domains: set, buyer_emails: set) -> bool:
    """A meeting belongs to THIS deal if any attendee is on a buyer domain or is a
    known buyer contact email — NOT by Avoma's opp association (cross-wired in this
    org). Matches the prompt's "keep meetings whose attendees match" rule."""
    for em in _avoma_attendee_emails(meeting):
        if buyer_emails and em in buyer_emails:
            return True
        dom = _domain_of(em)
        if dom and dom in buyer_domains:
            return True
    return False


async def _avoma_call_tool(agent_manager, name: str, args: dict) -> Any:
    """Invoke an Avoma MCP tool by name, or return None if it is not wired in.
    Never raises (the caller treats a failure as 'this discovery leg found nothing')."""
    tool = _find_tool(agent_manager, "avoma", name)
    if tool is None:
        return None
    return await tool.ainvoke(args)


# Meeting states that are real touchpoints but carry NO readable content. They
# count as engagement (the buyer met / a session was scheduled) and must NEVER be
# dropped from the manifest — only flagged as a gap so coverage stays honest.
_AVOMA_GAP_STATES = {
    "not_recorded", "bot_denied_entry", "cancelled", "canceled", "no_show",
    "declined", "abandoned", "failed",
}


def _empty_prefetch(err: int = 0) -> dict:
    return {
        "calls": [], "manifest": [], "calls_found": 0, "window_days": None,
        "discovery_method": "opp+account+attendee",
        "match_basis": "none",
        "coverage": {"discovered": 0, "matched": 0, "read": 0, "readers": 0,
                     "transcripts": 0, "notes": 0, "gaps": 0,
                     "mismatch": 0, "errors": err},
    }


# --- Direct Avoma HTTP (self-contained) -------------------------------------
# The engine talks to the Avoma REST API DIRECTLY (httpx), bypassing the MCP /
# LangChain adapter whose response truncation silently drops large meeting-list
# payloads — the root of the long-standing calls_found=0 (proven: the raw API
# returns 16 BH meetings; the adapter path returns 0). Self-contained on purpose:
# no dependency on importing the MCP server module (FastMCP), so it can never
# regress to the broken path and is testable offline. Token mirrors the avoma MCP
# server's resolution (env first, same fallback). TODO(secrets): the fallback
# token is a flagged rotation item — move AVOMA_API_TOKEN into Secrets Manager.
_AVOMA_API_BASE = os.getenv("AVOMA_API_BASE", "https://api.avoma.com/v1").rstrip("/")
_AVOMA_API_TOKEN = os.getenv("AVOMA_API_TOKEN", "ifi116h6e8:2p7r6khoxqojr5638sld")
_AVOMA_HTTP_TIMEOUT = float(os.getenv("DEAL_SWEEP_AVOMA_HTTP_TIMEOUT_S", "30"))
# Resilience for the direct-REST path. Bypassing the MCP adapter (which mangles
# large payloads -> calls_found=0) also dropped avoma_mcp_server._send_with_retry,
# so concurrent sweep reads that hit Avoma's 429 / a transient timeout silently
# returned read=0. Mirror that retry posture here (NOT the global _api_lock —
# concurrency is already bounded by the reader semaphore). Shared env names so
# ops tunes both the MCP and REST paths with the same knobs.
_AVOMA_MAX_429_RETRIES = int(os.getenv("AVOMA_MAX_429_RETRIES", "5"))
_AVOMA_MAX_TIMEOUT_RETRIES = int(os.getenv("AVOMA_MAX_TIMEOUT_RETRIES", "2"))


def _avoma_http() -> bool:
    """Whether direct Avoma HTTP is usable. True whenever a token is configured —
    self-contained, so this is the primary path; the MCP-tool fallback only runs
    if this is somehow disabled."""
    return bool(_AVOMA_API_TOKEN)


def _avoma_http_get(endpoint: str, params: Optional[dict] = None) -> Any:
    """One Avoma GET (sync; called via asyncio.to_thread). Returns parsed JSON, or
    {'error': ...} on failure — never raises.

    Retries HTTP 429 (honouring Retry-After, else capped exponential backoff:
    1,2,4,8...s capped at 30, floor 0.5) and transient timeout/transport errors
    (separate budget), mirroring avoma_mcp_server._send_with_retry. No global lock
    — the caller's reader semaphore already bounds concurrency, and the backoff
    sleeps run in the asyncio.to_thread worker so they don't block the event loop."""
    import time
    import httpx
    url = _AVOMA_API_BASE + endpoint
    headers = {"Authorization": f"Bearer {_AVOMA_API_TOKEN}"}
    rate_retries = 0
    timeout_retries = 0
    max_attempts = _AVOMA_MAX_429_RETRIES + _AVOMA_MAX_TIMEOUT_RETRIES + 1
    for _ in range(max_attempts):
        try:
            r = httpx.get(url, headers=headers, params=params or {},
                          timeout=_AVOMA_HTTP_TIMEOUT)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            # Read/connect timeout or transient transport error: back off and retry
            # up to the timeout budget, then surface the failure.
            if timeout_retries >= _AVOMA_MAX_TIMEOUT_RETRIES:
                return {"error": f"{type(exc).__name__}: {exc}"}
            time.sleep(float(min(2 ** timeout_retries, 30)))
            timeout_retries += 1
            continue
        except Exception as e:  # noqa: BLE001 — any other client error: don't retry
            return {"error": f"{type(e).__name__}: {e}"}
        if r.status_code == 429 and rate_retries < _AVOMA_MAX_429_RETRIES:
            ra = r.headers.get("Retry-After")
            try:
                delay = float(ra) if ra else float(min(2 ** rate_retries, 30))
            except (TypeError, ValueError):
                delay = float(min(2 ** rate_retries, 30))
            delay = max(0.5, min(delay, 30.0))
            rate_retries += 1
            print(f"[DEAL-SWEEP] avoma-429 {endpoint} attempt "
                  f"{rate_retries}/{_AVOMA_MAX_429_RETRIES} — sleeping {delay:.1f}s",
                  flush=True)
            time.sleep(delay)
            continue
        try:
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            return {"error": f"{type(e).__name__}: HTTP {r.status_code}"}
    return {"error": f"429: exhausted {_AVOMA_MAX_429_RETRIES} retries"}


def _avoma_http_pages(params: dict, max_pages: int = 50) -> list:
    """Paginate /meetings/ to completion (mirrors the server's _get_all_pages), so
    no calls are missed on a busy account."""
    out: list = []
    page = 1
    while page <= max_pages:
        d = _avoma_http_get("/meetings/", {**params, "page": page, "page_size": 100})
        if not isinstance(d, dict) or d.get("error"):
            break
        out.extend(d.get("results", []) or [])
        if len(out) >= (d.get("count", 0) or 0) or not d.get("next"):
            break
        page += 1
    return out


async def _avoma_pages(params: dict) -> list:
    """Paginated /meetings/ list, off the event loop."""
    return await asyncio.to_thread(_avoma_http_pages, params)


async def _avoma_get(endpoint: str):
    """Single GET, off the event loop."""
    return await asyncio.to_thread(_avoma_http_get, endpoint)


async def _avoma_discover(agent_manager, opp18: str, account18: str,
                          buyer_emails: set, frm: str, to: str) -> tuple[dict, int]:
    """Discover meetings THREE ways for one window; return (by_uuid, errors).
    Each meeting is tagged `_src` (opp|account|attendee) with opp>account>attendee
    precedence so the matcher can trust the opp-direct leg appropriately."""
    legs: list = []
    tags: list = []
    av = _avoma_http()

    def _dp(extra: dict) -> dict:
        p = {"from_date": frm, "to_date": to, "o": "-start_at",
             "include_crm_associations": True}
        p.update(extra)
        return p

    if opp18:
        legs.append(_avoma_pages(_dp({"crm_opportunity_ids": opp18})) if av
                    else _avoma_call_tool(agent_manager, "get_all_meetings_for_opportunity",
                         {"crm_opportunity_id": opp18, "from_date": frm, "to_date": to}))
        tags.append("opp")
    if account18:
        legs.append(_avoma_pages(_dp({"crm_account_ids": account18})) if av
                    else _avoma_call_tool(agent_manager, "get_all_meetings_for_account",
                         {"crm_account_id": account18, "from_date": frm, "to_date": to}))
        tags.append("account")
    for em in list(buyer_emails)[:12]:
        legs.append(_avoma_pages(_dp({"attendee_emails": em})) if av
                    else _avoma_call_tool(agent_manager, "get_all_meetings_for_attendee",
                         {"email": em, "from_date": frm, "to_date": to}))
        tags.append("attendee")
    if not legs:
        return {}, 0
    raw = await asyncio.gather(*legs, return_exceptions=True)
    _rank = {"opp": 3, "account": 2, "attendee": 1}
    by_uuid: dict = {}
    errors = 0
    for tag, r in zip(tags, raw):
        if isinstance(r, Exception) or r is None:
            errors += 1 if isinstance(r, Exception) else 0
            continue
        for m in _avoma_meetings_from(r):
            mid = m.get("uuid") or m.get("id") or m.get("meeting_id")
            if not mid:
                continue
            mid = str(mid)
            if mid in by_uuid:
                if _rank.get(tag, 0) > _rank.get(by_uuid[mid].get("_src", ""), 0):
                    by_uuid[mid]["_src"] = tag
            else:
                m = dict(m)
                m["_src"] = tag
                by_uuid[mid] = m
    return by_uuid, errors


async def _avoma_prefetch(agent_manager, opp: dict, buyer: dict) -> dict:
    """January 2.0 "never-miss" Avoma retrieval engine (PRIMARY discovery path).

    DISCOVERY = the safety net. Discovers the deal's calls THREE ways — opp + account
    (both FULL 18-char ids, never truncated) and attendee email — recency-first:
    the most-recent window first, widening only if empty, so active deals resolve
    fast and quiet deals are still covered (never an absolute zero while calls
    exist). MATCHING uses the buyer domain/email as the arbiter (this filters the
    org's known cross-wired opp association); only when no domain is known at all do
    we trust the opp/account CRM association directly (flagged low-confidence).
    Every matched meeting becomes a MANIFEST touchpoint — including no-content gaps
    (not_recorded / bot_denied / cancelled) — and is NEVER dropped. We then DEEP-READ
    the most-recent N (cap) concurrently down a ladder: transcript -> notes/summary
    -> metadata-only. Shaped-empty on hard failure (the engine never raises)."""
    try:
        opp_id = opp.get("id") if isinstance(opp, dict) else None
        buyer = buyer if isinstance(buyer, dict) else {}
        account_id = buyer.get("account_id")
        buyer_domains = {d.lower() for d in (buyer.get("domains") or []) if isinstance(d, str)}
        buyer_emails: set = set()
        for c in (buyer.get("contacts") or []) + (buyer.get("account_contacts") or []):
            if isinstance(c, dict) and c.get("email"):
                buyer_emails.add(str(c["email"]).strip().lower())
        # PRIMARY KEYS — never truncate. Avoma files meetings under the FULL 18-char
        # Salesforce Id; a 15-char id matches NOTHING and silently returns zero.
        opp18 = sf_id_18(opp_id) or ""
        account18 = sf_id_18(account_id) or ""
        if not opp18 and not account18 and not buyer_emails:
            return _empty_prefetch()

        shaped = _empty_prefetch()
        have_domain = bool(buyer_domains or buyer_emails)
        shaped["match_basis"] = "buyer-domain" if have_domain else "crm-association"

        # --- Recency-first discovery: widen only until we have a matched call. ---
        by_uuid: dict = {}
        matched: list = []
        chosen_window = _AVOMA_WINDOWS[-1]
        for w in _AVOMA_WINDOWS:
            frm, to = _avoma_window(w)
            by_uuid, errs = await _avoma_discover(
                agent_manager, opp18, account18, buyer_emails, frm, to)
            shaped["coverage"]["errors"] += errs
            chosen_window = w
            if have_domain:
                matched = [(mid, m) for mid, m in by_uuid.items()
                           if m.get("_src") == "opp"
                           or _avoma_meeting_matches_buyer(m, buyer_domains, buyer_emails)]
                # opp-direct calls that fail the domain check = likely mis-association;
                # count them (honest "found but unverified") without surfacing them.
                if buyer_domains:
                    matched = [(mid, m) for mid, m in matched
                               if m.get("_src") != "opp"
                               or _avoma_meeting_matches_buyer(m, buyer_domains, buyer_emails)
                               or not _avoma_attendee_emails(m)]
            else:
                matched = [(mid, m) for mid, m in by_uuid.items()
                           if m.get("_src") in ("opp", "account")]
            if matched:
                break
        shaped["window_days"] = chosen_window
        shaped["coverage"]["discovered"] = len(by_uuid)
        shaped["coverage"]["matched"] = len(matched)
        if buyer_domains:
            shaped["coverage"]["mismatch"] = sum(
                1 for _mid, m in by_uuid.items()
                if m.get("_src") in ("opp", "account")
                and _avoma_attendee_emails(m)
                and not _avoma_meeting_matches_buyer(m, buyer_domains, buyer_emails))
        if not matched:
            return shaped

        # Manifest = ALL matched touchpoints, chronological (oldest first).
        matched.sort(key=lambda mm: (mm[1].get("start_at") or ""))
        manifest: list[dict] = []
        for mid, m in matched:
            state = (m.get("state") or "").lower()
            is_gap = (state in _AVOMA_GAP_STATES) or (
                not m.get("transcript_ready") and not m.get("transcription_uuid")
                and not m.get("notes_ready"))
            manifest.append({
                "meeting_id": mid,
                "date": m.get("start_at"),
                "subject": m.get("subject") or m.get("title"),
                "attendees": _avoma_attendee_emails(m),
                "state": state or "unknown",
                "is_gap": bool(is_gap),
                "has_content": False,
            })
        shaped["manifest"] = manifest
        shaped["calls_found"] = len(manifest)
        shaped["coverage"]["gaps"] = sum(1 for x in manifest if x["is_gap"])

        # DEEP-READ the most-recent N readable calls (cap bounds depth, not coverage).
        readable = [(mid, m) for mid, m in matched
                    if (m.get("state") or "").lower() not in _AVOMA_GAP_STATES]
        to_read = readable[-_AVOMA_MAX_READS:] if _AVOMA_MAX_READS else readable
        to_read = list(reversed(to_read))  # newest first
        try:
            import deal_engine_qi as _qi
            try:
                forecasted = _qi._is_forecasted(opp.get("forecast_category")) \
                    if hasattr(_qi, "_is_forecasted") else False
            except Exception:  # noqa: BLE001
                forecasted = False
            readers = int(_qi.staffing_plan(
                calls_read=len(to_read), forecasted=forecasted).get("readers") or 1)
        except Exception:  # noqa: BLE001 — never block the prefetch on sizing
            readers = 1
        readers = max(1, min(readers, max(1, len(to_read)), _AVOMA_READER_CAP))
        shaped["coverage"]["readers"] = readers
        sem = asyncio.Semaphore(readers)
        by_mid = {x["meeting_id"]: x for x in manifest}
        av = _avoma_http()

        async def _read_one(mid: str, m: dict) -> None:
            """Ladder: transcript -> notes/summary -> metadata. Always attaches what
            it gets to the manifest entry; the entry survives even if all reads fail.
            Reads go DIRECT to the Avoma HTTP layer (proven), with the MCP-tool path
            as fallback. The transcription_uuid is already in the discovered meeting,
            so the transcript is one GET — no extra meeting fetch."""
            async with sem:
                entry = by_mid.get(mid) or {}
                transcript_excerpt = ""
                notes_txt = ""
                tu = m.get("transcription_uuid")
                if av:
                    if tu:
                        try:
                            tr_obj = await _avoma_get(f"/transcriptions/{tu}/")
                            if isinstance(tr_obj, dict) and not tr_obj.get("error"):
                                transcript_excerpt = json.dumps(
                                    tr_obj.get("transcript", tr_obj),
                                    ensure_ascii=False)[:_AVOMA_TRANSCRIPT_CHARS]
                        except Exception:  # noqa: BLE001
                            shaped["coverage"]["errors"] += 1
                    try:
                        nt_obj = await _avoma_get(f"/meetings/{mid}/insights/")
                        if isinstance(nt_obj, dict) and not nt_obj.get("error"):
                            notes_txt = json.dumps(
                                nt_obj.get("ai_notes", nt_obj.get("notes", nt_obj)),
                                ensure_ascii=False)[:_AVOMA_NOTES_CHARS]
                    except Exception:  # noqa: BLE001
                        shaped["coverage"]["errors"] += 1
                else:
                    if m.get("transcript_ready") or tu:
                        try:
                            tr_obj = _coerce_obj(await _avoma_call_tool(
                                agent_manager, "get_meeting_transcript", {"uuid": mid}))
                            if isinstance(tr_obj, dict) and not tr_obj.get("error"):
                                transcript_excerpt = json.dumps(
                                    tr_obj.get("transcript", tr_obj),
                                    ensure_ascii=False)[:_AVOMA_TRANSCRIPT_CHARS]
                        except Exception:  # noqa: BLE001
                            shaped["coverage"]["errors"] += 1
                    try:
                        notes_obj = _coerce_obj(await _avoma_call_tool(
                            agent_manager, "get_meeting_notes", {"uuid": mid}))
                        if isinstance(notes_obj, dict) and not notes_obj.get("error"):
                            notes_txt = json.dumps(
                                notes_obj.get("notes", notes_obj),
                                ensure_ascii=False)[:_AVOMA_NOTES_CHARS]
                    except Exception:  # noqa: BLE001
                        shaped["coverage"]["errors"] += 1
                if transcript_excerpt or notes_txt:
                    entry["has_content"] = True
                    entry["notes"] = notes_txt
                    entry["transcript_excerpt"] = transcript_excerpt
                    if transcript_excerpt:
                        shaped["coverage"]["transcripts"] += 1
                    elif notes_txt:
                        shaped["coverage"]["notes"] += 1

        await asyncio.gather(*[_read_one(mid, m) for mid, m in to_read],
                             return_exceptions=True)
        shaped["calls"] = [x for x in manifest if x.get("has_content")]
        shaped["coverage"]["read"] = len(shaped["calls"])
        return shaped
    except Exception as e:  # noqa: BLE001 — engine must NEVER raise into the sweep
        print(f"[DEAL-SWEEP] avoma-prefetch failed opp="
              f"{opp.get('id') if isinstance(opp, dict) else '?'}: "
              f"{type(e).__name__}: {e}", flush=True)
        return _empty_prefetch(err=1)


_AVOMA_MATCH_EXCLUDE_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com",
    "live.com", "icloud.com", "aol.com", "protonmail.com", "me.com", "zycus.com",
} | _SI_DOMAINS


async def _avoma_prefetch_from_datalake(opp: dict, buyer: dict = None) -> dict:
    """A/B path: build the Avoma manifest from the `datalake` (the deal's WHOLE call
    history — no 90-day clip, no 12-read cap, no rate-limit) in ONE SQL read, instead
    of live Avoma. Same shape as _avoma_prefetch so _avoma_prefetch_block renders it
    unchanged. Shaped-empty on any failure; never raises.

    Matches THREE ways — opp_id OR account_id OR buyer attendee-DOMAIN — because Avoma's
    SF association is frequently null/cross-wired: the HAVI loss-announcement call had
    crm_opportunity_id=null AND a crm_account_id that didn't match the opp's account, so
    the old opp-id-only match made the single most decisive call INVISIBLE. The attendee
    domain (e.g. havi.com) is the reliable link. Newest calls are always included."""
    shaped = _empty_prefetch()
    shaped["discovery_method"] = "datalake"
    shaped["match_basis"] = "datalake opp+account+domain"
    dl_url = os.getenv("DATALAKE_URL", "").rstrip("/")
    dl_key = os.getenv("DATALAKE_SERVICE_KEY", "")
    oid = opp.get("id") if isinstance(opp, dict) else None
    opp18 = sf_id_18(oid or "") or ""
    opp15 = (opp18 or (oid or ""))[:15]
    buyer = buyer or {}
    acc_id = buyer.get("account_id") or (opp.get("account_id") if isinstance(opp, dict) else None)
    acc15 = (sf_id_18(acc_id or "") or (acc_id or ""))[:15]
    doms = [str(d).lower().strip() for d in (buyer.get("domains") or []) if d]
    doms = [d for d in doms if d and "." in d and d not in _AVOMA_MATCH_EXCLUDE_DOMAINS]
    if not (dl_url and dl_key and (opp15 or acc15 or doms)):
        return shaped
    import httpx
    ors = []
    if opp15:
        ors.append(f"crm_opportunity_id.like.{opp15}*")
    if acc15:
        ors.append(f"crm_account_id.like.{acc15}*")
    for d in doms[:6]:
        ors.append(f"attendee_domains.cs.{{{d}}}")   # array-contains the buyer domain
    params = {
        "select": "uuid,subject,start_at,state,attendee_emails,attendee_domains,"
                  "crm_opportunity_id,transcript_ready,notes_ready,"
                  "avoma_transcripts(transcript_text),avoma_insights(ai_notes_text)",
        "or": "(" + ",".join(ors) + ")",
        "order": "start_at",
    }
    shaped["match_basis"] = f"datalake opp+account+domain ({len(doms)} dom)"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"{dl_url}/rest/v1/avoma_meetings", params=params,
                                 headers={"apikey": dl_key, "Authorization": f"Bearer {dl_key}"})
            rows = r.json() if r.status_code < 300 else []
    except Exception as e:  # noqa: BLE001
        print(f"[DEAL-SWEEP] datalake prefetch failed opp={oid}: {type(e).__name__}: {e}", flush=True)
        return shaped
    if not isinstance(rows, list) or not rows:
        return shaped

    def _one(v):
        return (v[0] if v else {}) if isinstance(v, list) else (v if isinstance(v, dict) else {})

    # COMPLETE UNITS ONLY. First build every touchpoint with its COMPLETE Avoma
    # AI-notes (a faithful whole-call summary); then allocate VERBATIM transcripts to
    # the most-recent calls within a total budget — a transcript is inlined WHOLE or
    # not at all, NEVER sliced mid-call. A call that misses the verbatim budget still
    # carries its complete notes; one with neither is listed as a bare touchpoint.
    prepared = []  # [(entry, full_transcript_text_or_None)] in chronological order
    for m in rows:
        tr_text = _one(m.get("avoma_transcripts")).get("transcript_text")
        notes = _one(m.get("avoma_insights")).get("ai_notes_text")
        state = (m.get("state") or "").lower()
        is_gap = state in _AVOMA_GAP_STATES or (not tr_text and not notes)
        entry = {"meeting_id": m.get("uuid"), "date": m.get("start_at"),
                 "subject": m.get("subject"), "attendees": m.get("attendee_emails") or [],
                 "state": state or "unknown", "is_gap": bool(is_gap),
                 "has_content": bool(tr_text or notes)}
        if notes:
            entry["notes"] = str(notes)[:_AVOMA_DL_NOTES_MAXCHARS]  # complete summary
        prepared.append((entry, str(tr_text) if tr_text else None))
    # Verbatim transcripts: whole MEETINGS newest-first, NEVER splitting a multi-part
    # meeting. Group same-day parts (Teil 1/Teil 2, Part 1/2, Day 1/2) into ONE unit and
    # inline all its parts together or none — so the model never sees half a meeting and
    # invents the missing half. The single NEWEST meeting is guaranteed inlined (it is the
    # "last meeting" that drives the day-summary / critical signals); older meetings then
    # fill the remaining budget, whole-unit-or-notes.
    groups: dict = {}
    for entry, tr_text in prepared:                      # prepared is oldest -> newest
        groups.setdefault(_meeting_group_key(entry.get("subject"), entry.get("date")),
                          []).append((entry, tr_text))
    ordered = sorted(groups.values(),
                     key=lambda mem: max((e.get("date") or "") for e, _ in mem),
                     reverse=True)                        # newest meeting first
    budget = _AVOMA_DL_TRANSCRIPT_BUDGET
    full = 0
    for gi, members in enumerate(ordered):
        # Parts individually small enough to inline; an over-long part stays notes-only
        # but must NOT block its siblings.
        inlinable = [(e, t) for (e, t) in members
                     if t and len(t) <= _AVOMA_DL_TRANSCRIPT_MAXCHARS]
        if not inlinable:
            continue
        need = sum(len(t) for _e, t in inlinable)
        # Guarantee the NEWEST meeting (gi == 0) whole even if it slightly exceeds budget;
        # every other meeting must fit the remaining budget as a WHOLE unit (never split).
        if gi == 0 or need <= budget:
            for e, t in inlinable:
                e["transcript_excerpt"] = t              # WHOLE transcript — never truncated
                full += 1
            budget -= need
    manifest = [e for (e, _t) in prepared]
    shaped["manifest"] = manifest
    shaped["calls"] = [x for x in manifest if x.get("has_content")]
    shaped["calls_found"] = len(manifest)
    shaped["window_days"] = "all"
    cov = shaped["coverage"]
    cov["discovered"] = len(manifest)
    cov["matched"] = len(manifest)
    cov["read"] = len(shaped["calls"])
    cov["transcripts"] = full
    cov["notes"] = sum(1 for x in manifest if x.get("notes"))
    cov["gaps"] = sum(1 for x in manifest if x.get("is_gap"))
    print(f"[DL-DIAG] opp={oid} opp15={opp15!r} acc15={acc15!r} doms={doms[:3]} "
          f"rows={len(rows)} manifest={len(manifest)} content_read={cov['read']} "
          f"transcripts={cov['transcripts']} notes={cov['notes']} gaps={cov['gaps']}", flush=True)
    return shaped


def _avoma_prefetch_block(pf: dict) -> str:
    """Render the never-miss engine's output as the AUTHORITATIVE Avoma manifest for
    the agent: every matched touchpoint (chronological), deep content where read,
    and gaps flagged. Empty string ONLY when the engine matched nothing (then the
    agent's own discovery still governs). When present, this block is the agent's
    complete Avoma coverage — it must NOT run its own discovery (that is the slow,
    churn-inducing path this engine replaces)."""
    if not pf or not isinstance(pf, dict):
        return ""
    manifest = pf.get("manifest") or []
    if not manifest:
        return ""
    cov = pf.get("coverage") or {}
    read = cov.get("read", 0)
    gaps = cov.get("gaps", 0)
    basis = pf.get("match_basis", "buyer-domain")
    parts: list[str] = [
        "\n\n=== AVOMA MANIFEST (authoritative — discovered + read FOR YOU by the "
        "never-miss engine) ===",
        f"This is your COMPLETE Avoma coverage for THIS deal: {len(manifest)} buyer "
        f"touchpoint(s) discovered three ways (opp + account + attendee, "
        f"window={pf.get('window_days')}d, match basis={basis}) and {read} deep-read "
        "concurrently before this run. Do NOT run your own Avoma discovery or re-read "
        "these one-by-one — SYNTHESISE from what is below.",
        "RULES: (1) Every line below is a REAL buyer touchpoint — you MUST reflect this "
        "engagement; with a non-empty manifest you may NEVER write 'no conversation' / "
        "'no calls' / 'gone dark'. (2) A line marked [GAP] is a real meeting with no "
        "recording (not_recorded / bot-denied / cancelled) — count it as a touchpoint, "
        "do not invent its content. (3) Quote buyer speakers by name with the call date. "
        "(4) Report evidence_coverage with calls_discovered=" + str(len(manifest)) +
        ", calls_read=" + str(read) + ", discovery_method='never-miss engine "
        "(opp+account+attendee)'. If the deep content is thinner than the manifest, set "
        "confidence to reflect partial coverage — do NOT shrink the touchpoint count.",
        f"Coverage: discovered={cov.get('discovered', 0)}, matched={len(manifest)}, "
        f"deep-read={read} (transcripts={cov.get('transcripts', 0)}, "
        f"notes={cov.get('notes', 0)}), gaps={gaps}, "
        f"unverified-association={cov.get('mismatch', 0)}.",
    ]
    for i, c in enumerate(manifest, 1):
        hdr = (f"\n--- Touchpoint {i}: {c.get('subject') or 'untitled'} "
               f"({c.get('date') or 'date unknown'})")
        if c.get("is_gap"):
            hdr += f" [GAP: {c.get('state') or 'no recording'} — touchpoint, no content]"
        att = c.get("attendees") or []
        if att:
            hdr += " — attendees: " + ", ".join(att[:12])
        parts.append(hdr)
        if c.get("notes"):
            parts.append("Notes / summary (speaker-attributed): " + c["notes"])
        if c.get("transcript_excerpt"):
            parts.append("Transcript excerpt (verbatim): " + c["transcript_excerpt"])
    return "\n".join(parts)


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
        # PRIMARY KEYS for Avoma — use VERBATIM, never truncate (Avoma files
        # meetings under the FULL 18-char Salesforce Id; a 15-char id matches nothing).
        f"- Opportunity Id (18-char — pass VERBATIM to get_all_meetings_for_opportunity): {sf_id_18(opp.get('id') or '')}",
        f"- Account Id (18-char — pass VERBATIM to get_all_meetings_for_account): {sf_id_18((buyer or {}).get('account_id') or '')}",
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
        f"- Geography: {_f(opp.get('geography'))}",
    ]
    # Partner-led / APAC routing hint: on these deals the buyer calls run through
    # the partner and are NOT in Avoma against this opp, and tasks are sparse — the
    # deal intelligence lives in the Next Step log. Tell the agent up front so it
    # mines Next_Step__c / Next_Step_History__c hard instead of reporting a dark deal.
    _geo = (opp.get("geography") or "")
    _ns_blob = (opp.get("next_step") or "")
    if str(_geo).strip().upper() == "APAC" or any(
            k in _ns_blob.lower() for k in ("partner", "atos", "reseller",
                                            "system integrator", "led by")):
        lines.append(
            "- NOTE: this looks PARTNER-LED and/or APAC. Expect few/no Avoma calls and "
            "sparse tasks (the partner runs the calls). Do NOT call this dark: "
            "reconstruct the deal from Next_Step__c + Next_Step_History__c + golden "
            "tasks, name the real buyer-side people/competitors the log mentions, and "
            "treat the partner as the channel.")
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
        + (
            "\n=== TEMPORAL ANCHORING (G8 — hard rule, read before writing ANY prose) ===\n"
            "Re-anchor EVERY time reference in your output to today's date above. A "
            "relative phrase copied from a Salesforce note/task or from living memory "
            "('next week', 'this Thursday', 'recently', 'last week', 'tomorrow', "
            "'soon') was written on an EARLIER date and is almost always STALE as of "
            "today — you MUST recompute it. For every event you mention:\n"
            "  1. State its ABSOLUTE date (e.g. '15 May 2026'), never a bare relative "
            "phrase.\n"
            "  2. Say whether it is now PAST or UPCOMING vs today, with approximate "
            "elapsed/remaining time — e.g. 'on-site demo on 15 May (~6 weeks ago)', "
            "'Horizon event this week (~23 Jun)', 'kickoff 7 Jul (in ~2 weeks)'.\n"
            "  3. A 'next week' / 'this Thursday' from an older note is very likely in "
            "the PAST now — convert it; do NOT imply it is still upcoming. If you "
            "cannot resolve an event to an absolute date, write 'date unclear'.\n"
            "Compute ALL 'X days ago', overdue, days-to-close and time-in-stage math "
            "from the ABSOLUTE dates vs today's date — never carry forward a "
            "previously-computed relative number (that is how '3 days ago' becomes "
            "wrong on a later sweep). In living memory, store every fact with its "
            "ABSOLUTE date (YYYY-MM-DD), never a relative phrase, so the next sweep can "
            "re-anchor it correctly.\n"
        )
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


# --- SFDC-anchored stakeholder roster ---------------------------------------
# The AI reads call transcripts and can INVENT stakeholder names that aren't real
# Salesforce contacts (e.g. "Herr Flandorfer") or drop real ones. This rebuilds the
# roster from the authoritative OpportunityContactRole list (buyer["contacts"]): keep
# the SFDC contacts the AI referenced (fuzzy match) + a few senior-by-title ones,
# DROP any AI name with no SFDC match. Capped, never bloats, never invents.
import re as _rex
import unicodedata as _udx

_SENIOR_TITLE_TOKENS = (
    "chief", "cfo", "cio", "ceo", "cto", "coo", "cpo", "cmo", "cdo", "cro",
    "svp", "evp", "avp", "vp", "vice president", "president",
    "head", "director", "manager", "lead", "owner",
    "procurement", "finance", "controller", "treasur",
    "information technology", "digital",
)
_SENIOR_TITLE_RE = _rex.compile(
    "|".join(_rex.escape(t) for t in _SENIOR_TITLE_TOKENS), _rex.IGNORECASE)
_ROSTER_CAP = 8


def _fold_name_key(name):
    """Accent-folded, honorific-stripped, token-SORTED identity key for a personal
    name — tolerant of casing/accents/order so 'Pölki'≈'Polki' and
    'KOLLER Melissa'≈'Melissa Koller'. Returns '' for pure role placeholders
    ('The CFO') so they never false-match a real contact."""
    try:
        from deal_engine_store import _STK_HONORIFICS, _STK_ROLE_WORDS
    except Exception:  # noqa: BLE001
        _STK_HONORIFICS = {"herr", "frau", "herrn", "mr", "mrs", "ms", "dr",
                           "prof", "mag", "ing", "dipl", "sir", "mme", "m"}
        _STK_ROLE_WORDS = set()
    s = name or ""
    s = _rex.sub(r"\(.*?\)", " ", s)
    s = "".join(c for c in _udx.normalize("NFKD", s) if not _udx.combining(c))
    s = s.lower()
    s = _rex.sub(r"[^0-9a-z\- ]", " ", s)
    toks = [t for t in s.split() if t and t not in _STK_HONORIFICS]
    real = [t for t in toks if len(t) >= 3 and t not in _STK_ROLE_WORDS]
    return " ".join(sorted(set(real))) if real else ""


def _is_senior_title(title):
    return bool(title) and bool(_SENIOR_TITLE_RE.search(str(title)))


def _role_from_title(title):
    """Deterministic, conservative role from a job title so the Stakeholders 'Role'
    column is never blank for a real SFDC contact. Purely title-based — no AI, no
    invention. Returns None when the title gives no clear signal (leave it blank)."""
    t = (title or "").lower()
    if not t:
        return None
    if any(w in t for w in ("chief", "cfo", "cio", "ceo", "coo", "cpo", "vp ", "vice president",
                            "president", "managing director", "head of")):
        return "Decision Maker"
    if any(w in t for w in ("director", "manager", "lead", "leiter", "head ")):
        return "Influencer"
    if any(w in t for w in ("buyer", "purchasing", "procurement", "einkauf", "sourcing",
                            "category", "specialist", "analyst", "processes")):
        return "Evaluator"
    return None


def _roster_from_sfdc(new_ai, buyer, existing_record):
    """Rebuild ai.stakeholder_map.items anchored to SFDC OpportunityContactRole.

    Roster = (A) SFDC contacts the AI referenced (fuzzy match, carrying the AI's
    role/risk/sentiment onto the SFDC canonical name+title) + (B) a few senior-by-
    title SFDC contacts. AI-invented names with no SFDC match are DROPPED. Capped
    at ~8. Never raises — a failure returns new_ai untouched."""
    if not isinstance(new_ai, dict):
        return new_ai
    try:
        import deal_engine_store as _ds
        sm = new_ai.get("stakeholder_map")
        sm = sm if isinstance(sm, dict) else {}
        ai_items = sm.get("items")
        ai_items = ai_items if isinstance(ai_items, list) else []
        contacts = buyer.get("contacts") if isinstance(buyer, dict) else []
        contacts = contacts if isinstance(contacts, list) else []

        pool, index = [], {}
        for c in contacts:
            if not isinstance(c, dict):
                continue
            nm = (c.get("name") or "").strip()
            if not nm or c.get("is_nonperson"):
                continue
            key = _fold_name_key(nm)
            if not key:
                continue
            pool.append((key, c))
            index.setdefault(key, c)
        if not pool:
            return new_ai  # nothing authoritative to anchor to -> leave AI roster

        def _sfdc_item(c, ai_src=None):
            it = {"name": (c.get("name") or "").strip(), "_source": "sfdc_contact_role"}
            if c.get("title"):
                it["title"] = c["title"]
            # email / phone from the SFDC Contact — carried for enrichment (harmless if
            # the drawer doesn't render them yet).
            for _cf in ("email", "phone"):
                if c.get(_cf):
                    it[_cf] = c[_cf]
            if isinstance(ai_src, dict):
                for f in ("role", "risk", "sentiment", "relationship", "last_contact_date"):
                    v = ai_src.get(f)
                    if v not in (None, "", []):
                        it[f] = v
                it["_matched_ai"] = True
            # Role fallback so the Stakeholders 'Role' column is never blank: prefer the
            # authoritative SFDC OpportunityContactRole.Role, else a conservative title-based
            # inference. An AI-annotated role (set above) always wins over both.
            if not it.get("role"):
                if c.get("role"):
                    it["role"] = c["role"]
                else:
                    _inf = _role_from_title(c.get("title"))
                    if _inf:
                        it["role"] = _inf
                        it["_role_inferred"] = True
            if c.get("is_partner"):
                it["_partner"] = True
            return it

        roster, used = [], set()
        # (A) AI-referenced SFDC contacts
        for it in ai_items:
            if not isinstance(it, dict):
                continue
            k = _fold_name_key(it.get("name"))
            if not k or k in used:
                continue
            c = index.get(k)
            if c is None:
                continue  # AI-invented name -> DROP
            used.add(k)
            roster.append(_sfdc_item(c, ai_src=it))
        # (B) senior-by-title backfill
        for k, c in pool:
            if len(roster) >= _ROSTER_CAP:
                break
            if k in used or c.get("is_partner"):
                continue
            if _is_senior_title(c.get("title")):
                used.add(k)
                roster.append(_sfdc_item(c))
        # (C) last resort: seed at least one contact so the drawer isn't empty
        if not roster:
            for k, c in pool:
                if not c.get("is_partner"):
                    roster.append(_sfdc_item(c))
                    break
        if not roster:
            return new_ai

        merged = _ds.dedupe_stakeholder_items(roster)
        merged = merged if isinstance(merged, list) else roster
        merged = merged[:_ROSTER_CAP]
        out = dict(new_ai)
        out_sm = dict(sm)
        out_sm["items"] = merged
        for ck in ("count", "total", "mapped", "num", "n"):
            if isinstance(out_sm.get(ck), int):
                out_sm[ck] = len(merged)
        out["stakeholder_map"] = out_sm
        return out
    except Exception as _e:  # noqa: BLE001 — must never break a sweep
        print(f"[DEAL-SWEEP] SFDC roster build skipped: {type(_e).__name__}: {_e}", flush=True)
        return new_ai


async def analyze_one(
    agent_manager,
    opp: dict,
    *,
    recursion_limit: Optional[int] = None,
    timeout_s: Optional[int] = None,
    source: str = "sweep",
    avoma_from_datalake: bool = False,
    dry_run: bool = False,
) -> dict:
    """Run the sweep agent for one opp and upsert the resulting canonical record.
    Returns {opp_id, status, duration_ms, error}. Every run (success OR failure)
    is logged to the deal_trigger_runs audit table, tagged with `source`
    (sweep | manual | salesforce_trigger)."""
    if recursion_limit is None:
        recursion_limit = int(os.getenv("DEAL_SWEEP_RECURSION_LIMIT", "80"))
    if timeout_s is None:
        timeout_s = int(os.getenv("DEAL_SWEEP_TIMEOUT_S", "900"))
    # PRODUCTION Avoma source: the datalake is now the DEFAULT (was opt-in). It holds the
    # deal's WHOLE call history AND matches by opp_id OR account_id OR buyer attendee-DOMAIN,
    # so it catches calls whose Salesforce association is null/cross-wired — the HAVI loss
    # call (crm_opportunity_id=null) was INVISIBLE to the live-Avoma path and the deal scored
    # as healthy. The prefetch falls back to LIVE Avoma per-deal when the datalake has nothing
    # for an opp, so a not-yet-synced deal never goes dark. Override with the env var = false.
    if not avoma_from_datalake:
        avoma_from_datalake = os.getenv(
            "DEAL_SWEEP_AVOMA_FROM_DATALAKE", "true").strip().lower() in ("1", "true", "yes", "on")
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
        # Scope this run's search_knowledge to the MASE knowledge namespace so the sweep
        # can fetch uploaded docs (playbooks / competitive intel) from the isolated MASE
        # store. project_id routes search_knowledge to the MASE tables; a per-opp chat_id
        # gives the per-turn cap/dedupe its own bucket.
        try:
            import rag_context as _rag
            _rag.current_project_id.set(MASE_KNOWLEDGE_PROJECT_ID)
            _rag.current_chat_id.set(f"sweep:{opp_id}")
        except Exception:  # noqa: BLE001 — never block the sweep on this
            pass
        print(f"[DEAL-SWEEP] agent ready, invoking opp={opp_id} "
              f"(tools={len(_cached_tool_names)})", flush=True)
        # Per-deal living memory: load the prior record so we can (a) tell the
        # agent which insight topics are already on record (so it reuses the same
        # wording and our stable keys line up) and (b) merge this sweep into the
        # durable packets afterwards instead of overwriting.
        import deal_engine_packets as packets_mod
        # "Update Living Memories" (source="update_living_memory"): a deliberate
        # FROM-SCRATCH rebuild that REPLACES the stored record. We skip loading the
        # prior record, so EVERY carry-forward (packets reconcile, requirement /
        # element carry, suspect-dark guard, _apply_living_memory) becomes a no-op
        # for lack of prior state — the sweep result stands on its own. Every other
        # source keeps living memory (the incremental merge used by SF triggers /
        # automated refreshes), so steady-state behaviour is unchanged.
        _from_scratch = (source == "update_living_memory")
        existing_record = {}
        if _from_scratch:
            print(f"[DEAL-SWEEP] update-living-memory opp={opp_id} — FROM SCRATCH "
                  f"(no carry-forward; replaces the prior record)", flush=True)
        else:
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
        # MEDDPICC (CRM-entered) prefetch — fed to the agent as a hint to corroborate.
        # Confirms the economic buyer (and the rest of MEDDPICC) from the SF custom
        # objects, so a deal where the buyer is logged in the CRM stops reading as an
        # "economic buyer gap". Best-effort: never blocks the sweep.
        try:
            _meddpicc_crm_data = await _meddpicc_crm(agent_manager, opp_id)
        except Exception as _e:  # noqa: BLE001
            print(f"[DEAL-SWEEP] MEDDPICC prefetch failed opp={opp_id}: {_e}", flush=True)
            _meddpicc_crm_data = {}
        meddpicc_crm_block = _meddpicc_crm_block(_meddpicc_crm_data)
        # Parallel Avoma reader prefetch (flag-gated; OFF by default). When on, we
        # discover + read the deal's Avoma calls CONCURRENTLY here and hand the agent
        # pre-read, speaker-attributed notes so it synthesises instead of fetching
        # one-by-one. With the flag off this block does not run and the agent message
        # below is byte-for-byte unchanged. Best-effort: never raises into the sweep.
        # January 2.0 never-miss Avoma engine — the PRIMARY discovery path (on by
        # default; set DEAL_SWEEP_PARALLEL_READERS=false to fall back to the agent's
        # own slow in-loop discovery). It discovers + reads the deal's calls here and
        # hands the agent an AUTHORITATIVE manifest so it synthesises (fast) instead
        # of fetching one-by-one (slow -> lease churn). Best-effort: never raises.
        avoma_prefetch_block = ""
        _avoma_pf: dict = {}
        if os.getenv("DEAL_SWEEP_PARALLEL_READERS", "true").lower() in (
                "1", "true", "yes"):
            try:
                if avoma_from_datalake:
                    _avoma_pf = await _avoma_prefetch_from_datalake(opp, buyer)
                    # Per-deal fallback: fall back to LIVE Avoma when the datalake has NO
                    # READABLE CONTENT for this opp — either no meetings at all (not yet
                    # backfilled / webhook missed it) OR meetings whose transcripts haven't
                    # synced yet (manifest present but every call is a content-less gap, so
                    # calls_read=0). Without this a deal with un-synced transcripts reads dark
                    # even though live Avoma has the content. (If live ALSO has nothing — e.g.
                    # the meetings were never recorded — calls_read stays 0, which is correct.)
                    _dl_read = (_avoma_pf.get("coverage") or {}).get("read") or 0
                    if not (_avoma_pf.get("manifest")) or _dl_read == 0:
                        print(f"[DEAL-SWEEP] datalake no readable content opp={opp_id} "
                              f"(manifest={len(_avoma_pf.get('manifest') or [])}, read={_dl_read}) "
                              f"-> live Avoma fallback", flush=True)
                        _avoma_pf = await _avoma_prefetch(agent_manager, opp, buyer)
                else:
                    _avoma_pf = await _avoma_prefetch(agent_manager, opp, buyer)
                avoma_prefetch_block = _avoma_prefetch_block(_avoma_pf)
                _cov = _avoma_pf.get("coverage") or {}
                print(f"[DEAL-SWEEP] avoma-engine opp={opp_id} "
                      f"window={_avoma_pf.get('window_days')}d "
                      f"discovered={_cov.get('discovered')} matched={_avoma_pf.get('calls_found')} "
                      f"read={_cov.get('read')} gaps={_cov.get('gaps')} "
                      f"mismatch={_cov.get('mismatch')}", flush=True)
            except Exception as _e:  # noqa: BLE001 — degrade to no-prefetch
                print(f"[DEAL-SWEEP] avoma-engine wrapper failed opp={opp_id}: "
                      f"{type(_e).__name__}: {_e}", flush=True)
                avoma_prefetch_block = ""
                _avoma_pf = {}
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
            last_inbound_email_date=(buyer.get("last_inbound_email_date")
                                     if isinstance(buyer, dict) else None),
        )
        # FIRST-PASS QUALITY (2026-07-07, user-directed: retries are an ALERT, not normal —
        # 62% of runs were failing the validation gate on attempt 1 because the model was
        # graded against a people-allowlist it never saw). Show the roster UP FRONT so the
        # first attempt can comply; the gate stays as the backstop, not the workflow.
        _roster_block = ""
        try:
            _roster_names = sorted({str(n).strip() for n in (_allowlist or set())
                                    if n and len(str(n).strip()) > 2})[:80]
            if _roster_names:
                _roster_block = (
                    "\n\n## PEOPLE YOU MAY NAME (the ONLY permitted names — exact spellings)\n"
                    + ", ".join(_roster_names)
                    + "\nAnyone else you encounter (in transcripts, emails, notes): refer to them "
                      "by ROLE only ('the IT director', 'their procurement lead') — NEVER write a "
                      "personal name that is not in this list, never guess or normalise spellings, "
                      "and never invent a manager name. Records naming unlisted people are REJECTED "
                      "and force a full re-run."
                )
        except Exception:
            pass
        user_msg = (
            f"Sweep Salesforce Opportunity Id `{opp_id}`"
            + (f" (account: {opp.get('account')}, name: {opp.get('name')})" if opp.get("account") else "")
            + ". Follow your system prompt end-to-end and emit the canonical record "
            "JSON. Output JSON only, no preamble."
            + _sweep_facts_block(opp, buyer)
            + "\n\n" + _pulse.render_block(_pre_pulse) + "\n"
            + identity_block
            # Pre-read Avoma calls (empty string unless DEAL_SWEEP_PARALLEL_READERS
            # is on AND the prefetch found+read matched calls — off-path is unchanged).
            + avoma_prefetch_block
            + meddpicc_crm_block
            + topics_block
            + _roster_block
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
            parsed["swept_at"] = _now_ist()
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
            # Persist the buyer's latest INBOUND email onto hard so the from-hard pulse
            # here (and every derived view) sees the reply — not just LastActivityDate,
            # which is unreliable for Clari/EAC-captured incoming email.
            if isinstance(buyer, dict) and buyer.get("last_inbound_email_date"):
                hard["last_inbound_email_date"] = buyer["last_inbound_email_date"]
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
                # No-evidence guard: on a sweep that read ZERO buyer calls, the agent
                # has no real basis to RE-RANK or re-word an existing competitor (the
                # competitive read lives in the calls). Drop competitor candidates that
                # match an existing packet — carry those forward UNTOUCHED, preserving
                # the evidence-based threat ranking — while still allowing genuinely NEW
                # competitors (new key) and explicit retirements (resolves carry no
                # competitor key, so they pass through). This stops the threat order
                # from drifting on a thin run.
                if _cr_clean == 0 and _prior_packets:
                    _prior_keys = {p.get("key") for p in _prior_packets}
                    _kept = [c for c in candidates
                             if not (c.get("type") == "competitor"
                                     and packets_mod.make_key("competitor", c.get("subject")) in _prior_keys)]
                    if len(_kept) != len(candidates):
                        print(f"[DEAL-SWEEP] no-call guard opp={opp_id}: carried "
                              f"{len(candidates) - len(_kept)} existing competitor(s) "
                              f"forward untouched (calls_read=0)", flush=True)
                    candidates = _kept
                if candidates or _prior_packets:
                    merged_packets, new_deltas = packets_mod.reconcile(
                        _prior_packets, candidates, parsed["swept_at"])
                    # Living-memory rule: NEVER age-retire carried-forward facts.
                    # Absence is "not re-mentioned", never "gone" — a fact is retired
                    # ONLY on an explicit resolve/supersede signal (in reconcile) or an
                    # explicit human retirement. We still clean obsolete pre-v2 hygiene
                    # flags (field-missing artifacts, not real insights) when the SF
                    # read worked. Prepend deltas.
                    if _sf_ok:
                        merged_packets, _exp_deltas = packets_mod.expire_stale(
                            merged_packets, parsed["swept_at"],
                            retire_aged=False, retire_obsolete=_sf_ok)
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
                        parsed.get("ai") or {}, merged_packets,
                        today=parsed.get("swept_at"))
                    parsed["schema_version"] = 2
                    _prior_ai = (existing_record or {}).get("ai") or {}
                    # A sweep that surfaced NO competitor change must not rewrite the
                    # competitive_position SUMMARY prose either — carry it forward so a
                    # thin/0-call run can't replace a good ranked read with a stale one.
                    if not any(d.get("type") == "competitor" for d in (new_deltas or [])):
                        _prior_cp = _prior_ai.get("competitive_position") or {}
                        if _prior_cp.get("summary"):
                            _cp_now = parsed["ai"].setdefault("competitive_position", {})
                            _cp_now["summary"] = _prior_cp["summary"]
                    # Verdict trajectory (living memory): stronger / steady / weaker vs
                    # the prior sweep, plus a dated verdict_history series. Pulse-tied
                    # via swept_at so the trajectory stays consistent with engagement.
                    _RANK = {"On Track": 4, "Close Date Risk": 3, "Slowing": 2,
                             "At Risk": 2, "Off Track": 1}  # At Risk = legacy (== Slowing)
                    _nv = parsed["ai"].get("north_star_verdict") or {}
                    _cur = str(_nv.get("verdict") or "")
                    _prior_nv = _prior_ai.get("north_star_verdict") or {}
                    _prior_v = str(_prior_nv.get("verdict") or "")
                    if _cur:
                        if not _prior_v:
                            _traj = "new"
                        elif _RANK.get(_cur, 0) > _RANK.get(_prior_v, 0):
                            _traj = "stronger"
                        elif _RANK.get(_cur, 0) < _RANK.get(_prior_v, 0):
                            _traj = "weaker"
                        else:
                            _cd, _pd = bool(_nv.get("forecast_defensible")), bool(_prior_nv.get("forecast_defensible"))
                            _traj = "stronger" if (_cd and not _pd) else ("weaker" if (_pd and not _cd) else "steady")
                        _nv["trajectory"] = _traj
                        _nv["prior_verdict"] = _prior_v or None
                        _hist = list(_prior_ai.get("verdict_history")
                                     or (existing_record or {}).get("verdict_history") or [])
                        _hist.append({"date": parsed.get("swept_at"), "verdict": _cur,
                                      "forecast_defensible": bool(_nv.get("forecast_defensible")),
                                      "trajectory": _traj})
                        parsed["verdict_history"] = _hist[-20:]
                        parsed["ai"]["north_star_verdict"] = _nv
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
                    # TITLE gate: neutralise any executive / economic-buyer title the
                    # model pinned on a name Salesforce cannot back (e.g. "CFO <name>"
                    # for someone SF shows as a Deputy CPO). Stakeholder titles are
                    # server-owned from OpportunityContactRole Contact.Title, exactly
                    # like manager_name — an unbacked title is dropped, the real name
                    # kept. Covers moves, requirements, MEDDPICC, competitive read.
                    _title_fixes = _val.sanitize_title_claims(
                        parsed["ai"], _val.build_contact_titles(buyer), _pkt_allow, opp)
                    if _title_fixes:
                        print(f"[DEAL-SWEEP] title-gate opp={opp_id} neutralised "
                              f"{_title_fixes} unverified stakeholder title claim(s)",
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
        _max_attempts = max(1, int(os.getenv("DEAL_SWEEP_GATE_ATTEMPTS", "2")))
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
                # ALERTING (2026-07-07): a gate FAIL is an exception, not a workflow — log
                # WHAT failed (category + first offender) so systemic causes are visible.
                _cats = sorted({str(v.get("check")) for v in _violations if isinstance(v, dict)})
                _v0 = next((v for v in _violations if isinstance(v, dict)), {})
                print(f"[DEAL-SWEEP] gate FAIL opp={opp_id} "
                      f"attempt={_attempt + 1}/{_max_attempts} "
                      f"violations={len(_violations)} cats={_cats} "
                      f"first={_v0.get('field')}::{str(_v0.get('offending'))[:60]!r} -> retry", flush=True)
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
        # ---- Quality inspector + exploratory recovery -----------------------
        # The first record is gate-clean but may be THIN: 0 Avoma calls + empty
        # MEDDPICC / competition / moves. Historically the worker then re-ran the
        # SAME agent (which changed nothing) and the deal landed in `failed`.
        # Instead, when the record is thin BUT the deal carries recoverable signal
        # (contact roles, recent activity, a populated Next Step log/history,
        # golden-nugget tasks, or a partner-led/APAC motion), exhaust those sources
        # and re-synthesize ONCE with them injected. Classic case: a partner-led
        # APAC deal (e.g. Reserve Bank of Australia S2P) where the partner runs every
        # call, so Avoma + tasks are empty but the whole deal lives in Next_Step__c.
        # The recovered record goes through the SAME _finalize + anti-fabrication
        # gate, so it is held to the same no-fabrication bar. Never raises — a
        # recovery failure leaves the original gate-clean record intact.
        if os.getenv("DEAL_SWEEP_QUALITY_INSPECTOR", "true").lower() in ("1", "true", "yes"):
            try:
                import deal_quality_inspector as _qi
                _verdict = _qi.assess(parsed, agent_sf_blank=_agent_sf_blank)
                if not _verdict["good"]:
                    _rctx = await _qi.gather_recovery_context(
                        agent_manager, opp_id, opp, buyer)
                    if _qi.has_recoverable_signal(buyer, _rctx):
                        print(f"[QUALITY-INSPECTOR] opp={opp_id} thin "
                              f"(score={_verdict['score']} deficits={_verdict['deficits']}) "
                              f"-> recovering (apac={_rctx.get('is_apac')} "
                              f"partner={bool(_rctx.get('partner_signal'))} "
                              f"golden_tasks={(_rctx.get('tasks') or {}).get('golden_count')})",
                              flush=True)
                        _directive = _qi.build_recovery_directive(
                            _verdict["deficits"], _rctx)
                        _rec_attempts = max(1, int(
                            os.getenv("DEAL_SWEEP_RECOVERY_ATTEMPTS", "2")))
                        _rec_feedback = ""
                        _best, _best_score = parsed, _verdict["score"]
                        for _rattempt in range(_rec_attempts):
                            _rcoro = agent.ainvoke(
                                {"messages": [{"role": "user",
                                  "content": user_msg + "\n\n" + _directive + _rec_feedback}]},
                                config={"recursion_limit": recursion_limit},
                            )
                            _rres = await asyncio.wait_for(_rcoro, timeout=timeout_s)
                            _ru = _sum_usage(_rres.get("messages", [])
                                             if isinstance(_rres, dict) else [])
                            for _uk in ("uncached_input", "output", "cache_creation",
                                        "cache_read", "total"):
                                usage[_uk] = (usage.get(_uk) or 0) + (_ru.get(_uk) or 0)
                            usage["seen"] = bool(usage.get("seen")) or bool(_ru.get("seen"))
                            _rcand = _oa._extract_json(_oa._final_text(_rres))
                            if not isinstance(_rcand, dict) or _rcand.get("_error"):
                                _rec_feedback = ("\n\n--- That was not valid JSON. Re-emit "
                                    "the FULL canonical record as one JSON object. ---")
                                continue
                            _rparsed = _finalize(_rcand)
                            _rviol = _val.validate_record(
                                _rparsed, sf_facts=opp,
                                contact_roles=(buyer or {}).get("contacts"),
                                avoma_attendees=_attendees_of(_rparsed),
                                active_sf_user_names=_active_users,
                                prior_names=_val._sourced_names_in_record(existing_record),
                            )
                            if _rviol:
                                _rallow = set(_allowlist)
                                _rallow |= {_val._norm_name(n) for n in _attendees_of(_rparsed)}
                                _rallow |= set(_active_users or set())
                                _rallow |= {_val._norm_name(n)
                                            for n in _val._sourced_names_in_record(existing_record)}
                                _rallow.discard("")
                                _val.sanitize_failed_record(_rparsed, _rviol, opp,
                                                            allowlist=_rallow)
                            _rscore = _qi.richness_score(_rparsed)
                            if _rscore > _best_score:
                                _best, _best_score = _rparsed, _rscore
                            if _qi.assess(_rparsed, agent_sf_blank=_agent_sf_blank)["good"]:
                                break
                            _rec_feedback = ""
                        if _best_score > _verdict["score"]:
                            print(f"[QUALITY-INSPECTOR] opp={opp_id} recovered score "
                                  f"{_verdict['score']} -> {_best_score}", flush=True)
                            parsed = _best
                            result["recovered"] = True
                            result["recovery_score"] = _best_score
                        else:
                            print(f"[QUALITY-INSPECTOR] opp={opp_id} recovery did not "
                                  f"improve (stayed {_verdict['score']}); keeping original",
                                  flush=True)
                    else:
                        print(f"[QUALITY-INSPECTOR] opp={opp_id} thin but no recoverable "
                              f"signal — honestly dark, keeping as-is", flush=True)
            except Exception as _qe:  # noqa: BLE001 — recovery must never block persist
                print(f"[QUALITY-INSPECTOR] opp={opp_id} recovery error (non-fatal): "
                      f"{type(_qe).__name__}: {_qe}", flush=True)
        # ---- January 2.0 never-miss floor: the engine's manifest is the truth -----
        # The deterministic Avoma engine already discovered every buyer touchpoint for
        # this deal. If the model under-reported coverage, force evidence_coverage up
        # to the engine's count so the output can NEVER read "no conversation" while
        # real touchpoints exist. We touch ONLY the counts / discovery_method (engine
        # facts) — never the narrative — so this can never fabricate content. Runs
        # BEFORE the pulse is recomputed in _apply_living_memory, so the corrected
        # calls_read drives the engagement pulse too. Never blocks the persist.
        try:
            _engcov = (_avoma_pf or {}).get("coverage") or {}
            _eng_calls = int((_avoma_pf or {}).get("calls_found")
                             or _engcov.get("discovered") or 0)
            _eng_read = int(_engcov.get("read") or 0)
            if _eng_calls > 0 and isinstance(parsed, dict):
                _ec = parsed.get("evidence_coverage")
                if not isinstance(_ec, dict):
                    _ec = {}
                _model_disc = int(_ec.get("calls_discovered") or 0)
                _model_read = int(_ec.get("calls_read") or 0)
                # The ENGINE is the SOURCE OF TRUTH for coverage counts — it fetched the
                # transcripts from the datalake; the model only echoes them and routinely
                # MIS-reports (DuBois: engine read=7 but model wrote read=0; Publicis:
                # discovered=4 yet read=0). The old logic only FLOORED `calls_discovered`
                # (gated on `reported < engine`), so a correct discovered + wrong read=0
                # slipped through and poisoned the pulse, staffing, and the UI. Now we
                # OVERWRITE both counts with the engine's facts whenever they disagree.
                # Counts / discovery_method only — never the narrative — so this can never
                # fabricate content. Runs BEFORE the pulse is recomputed in
                # _apply_living_memory, so the corrected calls_read drives the pulse too.
                if _model_disc != _eng_calls or _model_read != _eng_read:
                    _ec["calls_discovered"] = _eng_calls
                    _ec["calls_read"] = _eng_read
                    _ec.setdefault("discovery_method",
                                   "never-miss engine (opp+account+attendee)")
                    _gp = _ec.setdefault("gaps", [])
                    if isinstance(_gp, list) and int(_engcov.get("gaps") or 0):
                        _gp.append(f"{_engcov.get('gaps')} touchpoint(s) had no "
                                   "recording (counted as engagement, no content)")
                    _ec["engine_floor_applied"] = True
                    parsed["evidence_coverage"] = _ec
                    print(f"[DEAL-SWEEP] coverage stamped opp={opp_id} "
                          f"model(disc={_model_disc},read={_model_read}) -> "
                          f"engine(disc={_eng_calls},read={_eng_read})", flush=True)
        except Exception as _fe:  # noqa: BLE001 — floor must never break the persist
            print(f"[DEAL-SWEEP] never-miss floor skipped opp={opp_id}: "
                  f"{type(_fe).__name__}: {_fe}", flush=True)
        # Escalation gate (deal_engine_qi) — INDEPENDENT of the name-fabrication
        # sanitiser (_val) above. A VP / manager / exec getting on a call may be
        # recommended ONLY on a forecasted deal (ForecastCategory in Commit /
        # Best Case / Upside Key Deal). On a non-forecasted deal this downgrades
        # any "Executive connect" move owner to "Deal team" (the convention the
        # clean records already use) and records the audit. Closes the
        # escalation-on-non-forecasted residual (~14% book-wide as of 2026-06-20).
        # Must run BEFORE the living-memory packets so a downgrade can never be
        # re-projected from a packet. Never blocks persist.
        try:
            import deal_engine_qi as _qigate
            _esc_v, parsed = _qigate.check_escalation(
                parsed, opp.get("forecast_category"))
            if _esc_v:
                result["qi_escalation"] = _esc_v
                print(f"[QI-ESCALATION] opp={opp_id} {len(_esc_v)} escalation "
                      f"violation(s) on a non-forecasted deal "
                      f"(owner-downgrades applied)", flush=True)
        except Exception as _ee:  # noqa: BLE001 — the gate must never block persist
            print(f"[QI-ESCALATION] opp={opp_id} non-fatal: "
                  f"{type(_ee).__name__}: {_ee}", flush=True)
        # Build the durable living-memory packets FIRST, AFTER the gate has approved
        # or sanitised the facts — so packets are always derived from gate-clean ai
        # and a stripped fabrication can never survive in the packet store. This also
        # recomputes the engagement pulse and projects the packet-backed ai.* (moves,
        # verdict, meddpicc) — the good, accurate, deal-progression facts.
        _apply_living_memory(parsed)
        # Belt-and-suspenders dedup: deterministically collapse homogeneous
        # open_deliverables + best_practice flags so carried-forward living memory
        # can never re-bloat the to-do surface even if the model re-lists near-
        # duplicates (Publicis: 58->11 commitments, 137->12 best-practice themes).
        # Pure, idempotent, only ever REDUCES; never raises. Runs before RevOps so
        # the editor re-ranks the already-deduped lists.
        import todo_grouping
        todo_grouping.tidy(parsed)  # within-block grouping + cross-bucket de-collision
        # RevOps Head — strategic editor-in-chief (Deal Sweep January 1.0). Runs
        # ABSOLUTELY LAST, AFTER living-memory, so its review (re-ranked moves +
        # ai.revops_review) is the FINAL write before persist and actually reaches the
        # UI — instead of being clobbered by the packet projection (the prior ordering
        # bug that left revops_review on 0/444 deals). It MERGES over the projected ai
        # (latest/greatest wins; any field it omits keeps the accurate projected value),
        # and pulse/hard are top-level so they are never touched. Forecasted-only,
        # behind REVOPS_HEAD_ENABLED; never blocks persist.
        parsed = await _revops_head_review(parsed, opp, opp_id)
        # Deterministic deal scoring (Deal Sweep January 1.1) — Win Position /
        # Deal Momentum / Customer Commitment / Deal Risk + Forecast Confidence +
        # a read label, each with a 2-sentence commentary. Computed AFTER living
        # memory + RevOps review on the gate-clean record, so it reads accurate,
        # non-fabricated signals (pulse / north-star verdict / MEDDPICC / competitive
        # position / packets). Hybrid: factors are derived from the swept record,
        # with any agent-emitted ai.deal_scores_evidence overlaid. No LLM call,
        # additive (writes ai.deal_scores), behind DEAL_SCORES_ENABLED, and NEVER
        # raises — a scoring failure must not fail a sweep.
        try:
            # Opp-trend signals (amount/close/stage/forecast progression-regression) from
            # field history — recomputed every sweep so a re-sweep keeps them (else the
            # backfilled ai.opp_trends would be wiped). Feeds Win. Best-effort.
            _tr = store.opp_trends_one(opp_id)
            if _tr is not None:
                parsed.setdefault("ai", {})["opp_trends"] = _tr
        except Exception as _te:  # noqa: BLE001
            print(f"[OPP-TRENDS] sweep compute failed for {opp_id}: {_te}", flush=True)
        try:
            # Footprints: deterministic 'is the deal alive + how deep is the engagement' from
            # SF Tasks + Events (buyer-received vs rep-sent, engagement-depth tiers). Feeds the
            # engagement-based Deal Momentum v2. Best-effort; never blocks the sweep.
            # Real meetings come from the DATALAKE (Avoma) manifest already matched for
            # this deal (opp/account/domain) — not from guessing at SF subject keywords.
            _av_dates = [e.get("date") for e in ((_avoma_pf or {}).get("manifest") or [])
                         if e.get("date")]
            _fp = await _footprints_for(agent_manager, opp_id,
                                        (parsed.get("hard") or {}).get("stage") or "",
                                        avoma_meeting_dates=_av_dates)
            if _fp:
                parsed.setdefault("ai", {})["footprints"] = _fp
        except Exception as _fe:  # noqa: BLE001
            print(f"[FOOTPRINTS] sweep compute failed for {opp_id}: {_fe}", flush=True)
        try:
            # CRM evidence: deterministic factor presence from MEDDPICC 2.0 (already fetched).
            # Stored so the Win rubric can broaden its source — a named EB/champion/metrics in
            # MEDDPICC 2.0 lifts the factor even if the LLM under-read it (the HAVI EB case).
            _ce = _crm_evidence_from(_meddpicc_crm_data)
            # Playbook "next step": ALSO scan the Next-Step log + opp narrative free text for the
            # rubric factors (esp. PREFERENCE, which has no MEDDPICC field) and MAX-merge — so a
            # "Zycus is the favoured vendor" noted only in Next-Step lifts Win deterministically.
            _ce = _merge_crm_evidence(_ce, await _rubric_crm_scan(agent_manager, opp_id))
            if _ce:
                parsed.setdefault("ai", {})["crm_evidence"] = _ce
        except Exception as _ce_e:  # noqa: BLE001
            print(f"[CRM-EVIDENCE] sweep compute failed for {opp_id}: {_ce_e}", flush=True)
        try:
            # Decision-outcome detector: an explicit WIN/LOSS in the latest call/notes/Next-Step.
            # MUST run before scoring — a detected loss hard-overrides Win/Momentum to 0 even
            # while Salesforce still shows the deal open (the HAVI-lost-to-Coupa case).
            _dec = _detect_decision_outcome(_avoma_pf, (parsed.get("hard") or {}).get("next_step"))
            if _dec.get("status") in ("lost", "won"):
                parsed.setdefault("ai", {})["decision_outcome"] = _dec
                print(f"[DECISION] opp={opp_id} status={_dec['status']} "
                      f"src={_dec.get('source')} :: matched '{_dec.get('matched')}'", flush=True)
        except Exception as _de:  # noqa: BLE001
            print(f"[DECISION] detect failed for {opp_id}: {_de}", flush=True)
        # PIN GUARD — a hand-corrected deal (existing ai.pinned == true) must survive
        # re-sweeps: carry its scores/panel + roster forward verbatim so an automated
        # sweep never clobbers a human correction (the Austrian Post revert). Hard facts
        # (stage/amount/dates) still refresh; only the AI judgment is frozen.
        _prior_ai_full = existing_record.get("ai") if isinstance(existing_record, dict) else None
        _pinned = bool(isinstance(_prior_ai_full, dict)
                       and (_prior_ai_full.get("pinned")
                            or ((_prior_ai_full.get("deal_scores") or {}).get("pinned")
                                if isinstance(_prior_ai_full.get("deal_scores"), dict) else False)))
        # DURABILITY (2026-07-07): the sweep REPLACES the whole ai object, which used to drop
        # the pin flag itself — so a pin only survived ONE sweep, then the next sweep clobbered
        # the human correction (BH 88 -> 70, Alghanim 35 -> 5). A pin is durable: re-persist the
        # flag onto every re-swept record until a human explicitly unpins.
        if _pinned:
            parsed.setdefault("ai", {})["pinned"] = True
        # RELATIONSHIP CONTEXT carry (2026-07-07): the same-account sibling index
        # (ai.account_context — expansion/phase-2 leverage) is stamped by the rescore pass;
        # a sweep must not drop it (refreshed on the next stamp run).
        if isinstance(_prior_ai_full, dict) and isinstance(_prior_ai_full.get("account_context"), dict) \
                and not isinstance((parsed.get("ai") or {}).get("account_context"), dict):
            parsed.setdefault("ai", {})["account_context"] = _prior_ai_full["account_context"]
        try:
            import deal_engine_scoring
            # VERDICT COMPATIBILITY ADAPTER (2026-07-09, Deal Sweep v3): the Omnivision
            # Deal-Sweep engine v10.0 DROPS the standalone north_star_verdict and emits
            # ai.forecast_read instead (§8/v3.1). Downstream consumers (UI verdict badge,
            # pulse flag reconcile, the fallback scorer's verdict caps, todo derivation)
            # still read north_star_verdict — so when a sweep emits none: carry the PRIOR
            # verdict forward (living memory: absence = not re-mentioned), else synthesize
            # a coarse one from forecast_read (defensible -> On Track, else At Risk).
            try:
                _ai_v = parsed.get("ai") or {}
                _nv = _ai_v.get("north_star_verdict")
                if not (isinstance(_nv, dict) and str(_nv.get("verdict") or "").strip()):
                    _prior_nv = ((existing_record.get("ai") or {}).get("north_star_verdict")
                                 if isinstance(existing_record, dict) else None)
                    if isinstance(_prior_nv, dict) and str(_prior_nv.get("verdict") or "").strip():
                        parsed.setdefault("ai", {})["north_star_verdict"] = {
                            **_prior_nv, "carried_forward": True}
                    else:
                        _fr = _ai_v.get("forecast_read")
                        if isinstance(_fr, dict) and ("defensible" in _fr):
                            parsed.setdefault("ai", {})["north_star_verdict"] = {
                                "verdict": ("On Track" if _fr.get("defensible") else "At Risk"),
                                "summary": str(_fr.get("reason") or "")[:300],
                                "source": "forecast_read_adapter"}
            except Exception:  # noqa: BLE001 — the adapter must never block the sweep
                pass
            # AI DEAL-SCORER (flag-gated by DEAL_ENGINE_AI_SCORING). Judges the five scores
            # over a deterministic evidence packet. score_deal_ai builds the packet itself
            # (datalake meetings) and already falls back to compute_deal_scores on any internal
            # failure, but we STILL wrap defensively so an import/scorer problem can never break
            # the sweep. Emits the SAME _scores shape (headline.win_position/... + per-score
            # contributions) the carry-forward and build_cro_panel below consume.
            _scores = None
            _fallback_reason = None
            try:
                import deal_engine_ai_scoring
                if deal_engine_ai_scoring.ai_scoring_enabled():
                    _scores = deal_engine_ai_scoring.score_deal_ai(parsed)
                else:
                    _fallback_reason = "ai scoring disabled (DEAL_ENGINE_AI_SCORING off)"
            except Exception as _aie:  # noqa: BLE001 — AI scoring is best-effort
                print(f"[DEAL-SCORES] AI scorer failed opp={opp_id}, using deterministic: {_aie}", flush=True)
                _scores = None
                _fallback_reason = f"ai scorer raised: {type(_aie).__name__}: {str(_aie)[:160]}"
            # Flag OFF, or a degenerate AI return -> deterministic compute — but NEVER silently
            # (2026-07-09, Alghanim): the fallback previously wore the analyst's badge with no
            # stamp (factor_source=hybrid, ai_scoring_error=None) — a wrong keyword score shipped
            # looking normal. Every fallback now stamps scoring_degraded + fallback_reason and
            # logs LOUDLY, so a degraded score is visible at a glance and greppable in CloudWatch.
            if not (isinstance(_scores, dict)
                    and (_scores.get("headline") or {}).get("win_position") is not None):
                if _fallback_reason is None:
                    _fallback_reason = ((_scores or {}).get("ai_scoring_error")
                                        if isinstance(_scores, dict) else None) \
                        or "ai scorer returned no usable headline"
                _scores = deal_engine_scoring.compute_deal_scores(parsed)
                if isinstance(_scores, dict):
                    _scores["scoring_degraded"] = True
                    _scores["fallback_reason"] = str(_fallback_reason)[:220]
                print(f"[DEAL-SCORES] DEGRADED opp={opp_id} — deterministic fallback scored this "
                      f"deal ({_fallback_reason})", flush=True)
            elif isinstance(_scores, dict) and _scores.get("ai_scoring_error"):
                # score_deal_ai's INTERNAL fallback (it returns det scores + the error string):
                # normalize to the same loud shape.
                _scores["scoring_degraded"] = True
                _scores["fallback_reason"] = str(_scores.get("ai_scoring_error"))[:220]
                print(f"[DEAL-SCORES] DEGRADED opp={opp_id} — internal fallback "
                      f"({_scores.get('ai_scoring_error')})", flush=True)
            # SAFETY NET — a sweep that read NOTHING (zero Avoma calls AND no engagement
            # footprints AND no CRM evidence) cannot produce a trustworthy score; left alone
            # it writes a confident-but-wrong LOW score over a good one — the "a strong deal
            # suddenly reads Slowing 45/47" bug that destroys trust. When the read came back
            # empty, carry the PRIOR good scores forward instead of clobbering them. A genuinely
            # dark deal still has footprints (old dates) — all-three-empty means the READ failed,
            # not that the deal died. A detected LOSS always stands (never carried over).
            _ai_now = parsed.get("ai") or {}
            _ec_s = parsed.get("evidence_coverage") or {}
            try:
                _cr_s = int(_ec_s.get("calls_read") or 0)
            except (TypeError, ValueError):
                _cr_s = 0
            try:   # deterministic floor: the engine's ACTUAL read, NOT the LLM's self-report.
                _cr_s = max(_cr_s, int((_avoma_pf.get("coverage") or {}).get("read") or 0) if isinstance(_avoma_pf, dict) else 0)
            except (TypeError, ValueError):
                pass
            _no_data = (_cr_s == 0
                        and not ((_ai_now.get("footprints") or {}).get("engagement"))
                        and not (_ai_now.get("crm_evidence")))
            _prior_ds = (existing_record.get("ai") or {}).get("deal_scores") if isinstance(existing_record, dict) else None
            _prior_hl = (_prior_ds or {}).get("headline") or {}
            _prior_ok = bool(_prior_ds) and not _prior_hl.get("dead") and _prior_hl.get("win_position") is not None
            _is_loss = (_ai_now.get("decision_outcome") or {}).get("status") == "lost"
            if _no_data and _prior_ok and not _is_loss:
                _scores = dict(_prior_ds)
                _scores["stale_read"] = True
                _scores["headline"] = {**_prior_hl, "stale_read": True}
                print(f"[DEAL-SCORES] no-data sweep opp={opp_id} (calls_read=0, no footprints, "
                      f"no crm_evidence) — carried prior scores forward instead of overwriting", flush=True)
            # CARRY-FORWARD (broader): a fresh compute that came back EMPTY or missing its
            # headline (scoring disabled, partial record, internal guard returning {}) must
            # NOT blank a good prior score. Keep the prior scores unless the deal is a real
            # terminal loss THIS sweep (a genuine dead/loss result always stands).
            _fresh_hl = (_scores or {}).get("headline") if isinstance(_scores, dict) else None
            _fresh_ok = bool(_scores) and isinstance(_fresh_hl, dict) and _fresh_hl.get("win_position") is not None
            _fresh_dead = isinstance(_fresh_hl, dict) and _fresh_hl.get("dead") is True
            if (not _fresh_ok) and (not _fresh_dead) and (not _is_loss) and _prior_ok:
                _scores = dict(_prior_ds)
                _scores["stale_read"] = True
                _scores["headline"] = {**_prior_hl, "stale_read": True}
                print(f"[DEAL-SCORES] degraded/empty compute opp={opp_id} — carried prior "
                      f"scores forward (fresh headline missing win_position)", flush=True)
            if _pinned and isinstance(_prior_ds, dict):
                # Frozen by a human correction — keep the pinned scores + panel verbatim.
                _scores = dict(_prior_ds)
                print(f"[PIN] opp={opp_id} pinned — carried prior scores/panel forward "
                      f"(sweep did not overwrite)", flush=True)
            # NEVER PERSIST A SCORELESS HEADLINE (2026-07-09, Publicis vibe Run Now): the
            # carry-forwards above ALL key on `_prior_ok` (prior has a win_position). When a
            # record is already a HUSK (prior scores null, e.g. from an earlier race) AND this
            # sweep's compute came back empty/errored, every guard no-ops and the deal PERSISTS
            # with headline=null forever — the UI then shows its client-side 94/62 filler + no
            # reasons. HARD FLOOR: if we still have no usable headline, force one final
            # deterministic compute on THIS sweep's record (it always returns a headline, even
            # for a thin/Salesforce-only deal), so a deal can never get stuck scoreless.
            _fin_hl = (_scores or {}).get("headline") if isinstance(_scores, dict) else None
            if not (isinstance(_fin_hl, dict) and _fin_hl.get("win_position") is not None) and not _pinned:
                try:
                    _forced = deal_engine_scoring.compute_deal_scores(parsed)
                    if isinstance(_forced, dict) and (_forced.get("headline") or {}).get("win_position") is not None:
                        _forced["scoring_degraded"] = True
                        _forced["fallback_reason"] = "husk-floor: no usable score from any path; forced deterministic"
                        _scores = _forced
                        print(f"[DEAL-SCORES] DEGRADED husk-floor opp={opp_id} — record had no usable score "
                              f"(and prior was scoreless); forced a fresh deterministic headline", flush=True)
                except Exception as _hfe:  # noqa: BLE001 — the floor must never block persist
                    print(f"[DEAL-SCORES] husk-floor failed opp={opp_id}: {_hfe}", flush=True)
            if _scores:
                parsed.setdefault("ai", {})["deal_scores"] = _scores
        except Exception as _se:  # noqa: BLE001 — scoring is best-effort, never blocks persist
            print(f"[DEAL-SCORES] compute failed for {opp_id}: {_se}", flush=True)
            # EXCEPTION-PATH CARRY-FORWARD (2026-07-09): if the WHOLE scoring block raised,
            # parsed has NO deal_scores at all — persisting that blanks a good stored score
            # (the John Deere/Publicis clobber). Carry the prior scores forward, exactly like
            # the in-band safety nets above.
            try:
                _pds = (existing_record.get("ai") or {}).get("deal_scores") if isinstance(existing_record, dict) else None
                _phl = (_pds or {}).get("headline") or {}
                if not ((parsed.get("ai") or {}).get("deal_scores")) and _pds and _phl.get("win_position") is not None:
                    _cf = dict(_pds)
                    _cf["stale_read"] = True
                    _cf["headline"] = {**_phl, "stale_read": True}
                    parsed.setdefault("ai", {})["deal_scores"] = _cf
                    print(f"[DEAL-SCORES] carried prior scores forward opp={opp_id} "
                          f"(scoring block raised; never persist scoreless over scored)", flush=True)
            except Exception:  # noqa: BLE001 — the carry-forward itself must never block persist
                pass
        try:
            # CRO-readable "Scores & reasons" panel — assembles the existing human-written
            # prose (competitive_position / vulnerabilities / champion_strength /
            # recommended_moves) + footprints + the deterministic scores into a plain-English
            # narrative the frontend renders INSTEAD of the maths breakdown (one read per
            # score, ✅/⚠️ bullets, an honest "what could lose it" block, the moves). No LLM
            # call. A hand-pinned panel (cro_panel.pinned, e.g. Bright Horizons) is preserved.
            import deal_engine_cro
            _ds_now = (parsed.get("ai") or {}).get("deal_scores")
            if isinstance(_ds_now, dict) and not _pinned:
                _prior_panel = (((existing_record.get("ai") or {}).get("deal_scores") or {}).get("cro_panel")
                                if isinstance(existing_record, dict) else None)
                _pin = _prior_panel if (isinstance(_prior_panel, dict) and _prior_panel.get("pinned")) else None
                _panel = deal_engine_cro.build_cro_panel(parsed, pinned_override=_pin)
                if _panel:
                    _ds_now["cro_panel"] = _panel
                elif isinstance(_prior_panel, dict) and _prior_panel and not _ds_now.get("cro_panel"):
                    # Build produced no panel this sweep — keep the prior CRO panel rather
                    # than render a blank/robotic breakdown over a good one.
                    _ds_now["cro_panel"] = _prior_panel
        except Exception as _cpe:  # noqa: BLE001 — panel is cosmetic, never blocks persist
            print(f"[CRO-PANEL] build failed for {opp_id}: {_cpe}", flush=True)
        # SFDC-anchored roster: rebuild ai.stakeholder_map from real OpportunityContactRole
        # contacts (AI annotates, never invents) at the single persist chokepoint. Defensive.
        try:
            if _pinned and isinstance(_prior_ai_full, dict) and isinstance(_prior_ai_full.get("stakeholder_map"), dict):
                # Pinned deal — keep the human-corrected roster verbatim (don't re-anchor,
                # which would re-admit/rescore names the correction curated).
                parsed.setdefault("ai", {})["stakeholder_map"] = _prior_ai_full["stakeholder_map"]
            elif isinstance(parsed, dict) and isinstance(parsed.get("ai"), dict):
                parsed["ai"] = _roster_from_sfdc(parsed["ai"], buyer, existing_record)
        except Exception as _rre:  # noqa: BLE001
            print(f"[DEAL-SWEEP] roster build skipped opp={opp_id}: {type(_rre).__name__}: {_rre}", flush=True)
        # Carry the pin forward so the freeze survives this write (upsert replaces `record`).
        if _pinned and isinstance(parsed.get("ai"), dict):
            parsed["ai"]["pinned"] = True
            _p_at = _prior_ai_full.get("pinned_at") if isinstance(_prior_ai_full, dict) else None
            if _p_at:
                parsed["ai"]["pinned_at"] = _p_at
        # CEO-intervention — computed NATIVELY each sweep (was a separate pass). The
        # WHEN is a deterministic gate on the just-computed scores (forecasted AND
        # win>60 AND mom>60); the WHAT rides the model's own emitted ceo_intervention
        # (no extra call) and is sanitized with the same title/name guardrails as the
        # rest of the record. On any failure we fall back to carrying the prior value
        # forward, so a re-sweep never drops a good CEO read.
        if isinstance(parsed.get("ai"), dict):
            try:
                import deal_engine_ceo as _ceo
                # People allowlist built HERE, in analyze_one scope (2026-07-09 fix): the old
                # code referenced `_pkt_allow`, which is a LOCAL of the nested
                # _apply_living_memory() — so this call raised NameError on EVERY sweep and
                # the native CEO finalize silently never ran (always fell back to carrying
                # the prior value). Same recipe as the packet gate's allowlist.
                _ceo_allow = set(_allowlist)
                _ceo_allow |= {_val._norm_name(n) for n in _attendees_of(parsed)}
                _ceo_allow |= set(_active_users or set())
                _ceo_allow |= {_val._norm_name(n)
                               for n in _val._sourced_names_in_record(existing_record)}
                _ceo_allow.discard("")
                _ceo.finalize_ceo_intervention(
                    parsed, opp, buyer,
                    prior_ai=_prior_ai_full if isinstance(_prior_ai_full, dict) else None,
                    allowlist=_ceo_allow)
            except Exception as _cie:  # noqa: BLE001 — never block persist
                print(f"[DEAL-SWEEP] ceo-intervention finalize skipped opp={opp_id}: "
                      f"{type(_cie).__name__}: {_cie}", flush=True)
                if not parsed["ai"].get("ceo_intervention"):
                    _prior_ceo = _prior_ai_full.get("ceo_intervention") if isinstance(_prior_ai_full, dict) else None
                    if _prior_ceo:
                        parsed["ai"]["ceo_intervention"] = _prior_ceo
        # 24h / last-active-day summary — built DETERMINISTICALLY from Salesforce activity so it
        # refreshes WITH the rest of the record on every sweep (the drawer's 24h tab reads
        # ai.day_summary). This is the reliable backbone: it captures the same Avoma notes +
        # emails + field-moves regardless of whether the LLM read the calls, so a stuck/thin
        # sweep never leaves the 24h summary blank. Runs in an executor (non-blocking) and is
        # wrapped so a summary hiccup can NEVER fail a sweep. Only overwrites when it finds
        # activity; otherwise any LLM-emitted day_summary is left intact.
        if isinstance(parsed.get("ai"), dict):
            # OWNERSHIP (2026-07-07): the INTELLIGENT day summary (day_summary_ai — Sonnet-written
            # business intelligence: who did what and why, what's pending) OWNS ai.day_summary.
            # A sweep must NEVER replace it with the deterministic template dump ("1 email —
            # subject") — that regression shipped twice. Carry the intelligent one forward; the
            # post-sweep restore refreshes it with the newest activity. The deterministic build
            # remains only as a BACKSTOP for records that have no summary at all.
            _prior_dsy = _prior_ai_full.get("day_summary") if isinstance(_prior_ai_full, dict) else None
            if isinstance(_prior_dsy, dict) and _prior_dsy.get("source") == "ai":
                parsed["ai"]["day_summary"] = _prior_dsy
            else:
                try:
                    import build_day_summaries as _bds
                    _dsy = await asyncio.get_running_loop().run_in_executor(
                        None, _bds.day_summary_for_opp, opp_id)
                    if _dsy:
                        parsed["ai"]["day_summary"] = _dsy
                except Exception as _dse:  # noqa: BLE001 — never block persist
                    print(f"[DAY-SUMMARY] build skipped opp={opp_id}: {type(_dse).__name__}: {_dse}", flush=True)
        # PROVENANCE (Omnivision) — stamped on EVERY run (2026-07-09: moved ABOVE the
        # dry_run split; dry-run/A-B records previously returned WITHOUT the stamp, so
        # QA couldn't verify which locked Studio versions governed the run).
        _sv_all = studio_versions()
        if _sv_all:
            parsed.setdefault("ai", {})["scoring_studio"] = {"versions": _sv_all, "stamped_at": _today()}
        if dry_run:
            # A/B test mode: return the verdict for comparison, do NOT persist.
            result["record"] = parsed
        else:
            # NEVER-CLOBBER GUARD (2026-07-07): a run that ends WITHOUT a scored record must
            # never overwrite one that HAS scores. Concurrent/duplicate runs (overlapping
            # triggers, reclaim races) had a malformed early-exit persist LAND ON TOP of a
            # good fresh record (Techtronic: run B's win=None clobbered run A's 54/61 four
            # minutes later). Re-read the CURRENT record at persist time; if ours is
            # score-less and the stored one is scored+fresher, adopt its judgment surfaces.
            try:
                _my_hl = (((parsed.get("ai") or {}).get("deal_scores") or {}).get("headline") or {})
                if _my_hl.get("win_position") is None:
                    # Re-read the CURRENT stored record; if that read itself fails or comes
                    # back empty (Supabase blip mid-race — exactly when this guard matters
                    # most), fall back to the sweep-start snapshot (existing_record) so the
                    # guard NEVER silently no-ops. (2026-07-09: John Deere/Publicis were
                    # blanked by a scoreless persist that slipped through here.)
                    _cur = None
                    try:
                        _cur = await asyncio.get_running_loop().run_in_executor(
                            None, store.get_record, opp_id)
                    except Exception as _gre:  # noqa: BLE001
                        print(f"[DEAL-SWEEP] never-clobber re-read failed opp={opp_id} "
                              f"({_gre}) — falling back to the sweep-start snapshot", flush=True)
                    _cur_ai = (_cur or {}).get("ai") if isinstance(_cur, dict) else None
                    _cur_hl = (((_cur_ai or {}).get("deal_scores") or {}).get("headline") or {})
                    if _cur_hl.get("win_position") is None and isinstance(existing_record, dict):
                        _snap_ai = existing_record.get("ai")
                        _snap_hl = (((_snap_ai or {}).get("deal_scores") or {}).get("headline") or {})
                        if _snap_hl.get("win_position") is not None:
                            _cur_ai, _cur_hl = _snap_ai, _snap_hl
                    if _cur_hl.get("win_position") is not None:
                        for _k in ("deal_scores", "footprints", "day_summary", "account_context"):
                            if _cur_ai.get(_k) is not None and (parsed.get("ai") or {}).get(_k) is None:
                                parsed.setdefault("ai", {})[_k] = _cur_ai[_k]
                        if (((parsed.get("ai") or {}).get("deal_scores") or {}).get("headline") or {}).get("win_position") is None:
                            parsed["ai"]["deal_scores"] = _cur_ai["deal_scores"]
                        print(f"[DEAL-SWEEP] never-clobber opp={opp_id}: this run produced no scores — "
                              f"kept the stored scored surfaces instead of blanking them", flush=True)
            except Exception as _nce:  # noqa: BLE001 — guard must never block persist
                print(f"[DEAL-SWEEP] never-clobber check skipped opp={opp_id}: {_nce}", flush=True)
            # (provenance stamped above, before the dry_run split)
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
        # calls_read is a FACT, not the model's self-report. Floor it by what the avoma engine
        # ACTUALLY read from the datalake / live Avoma (_avoma_pf coverage). The LLM routinely
        # under-reports evidence_coverage.calls_read — writing 0 even when handed N transcripts —
        # which mislabels a fully-read deal as "dark" and drives spurious retries / self-heals.
        ec = parsed.get("evidence_coverage")
        if not isinstance(ec, dict):
            ec = {}
            parsed["evidence_coverage"] = ec   # ensure the record always carries a coverage block
        try:
            _llm_calls_read = int(ec.get("calls_read") or 0)
        except (TypeError, ValueError):
            _llm_calls_read = 0
        try:
            _engine_calls_read = int((_avoma_pf.get("coverage") or {}).get("read") or 0) if isinstance(_avoma_pf, dict) else 0
        except (TypeError, ValueError):
            _engine_calls_read = 0
        calls_read = max(_llm_calls_read, _engine_calls_read)
        result["calls_read"] = calls_read
        ec["calls_read"] = calls_read   # persist the deterministic count so nothing downstream sees a fabricated 0
        # A thin record (worth the worker re-running analyze_one) is now ONLY one
        # whose Salesforce read genuinely failed (no core mechanics) — a transient
        # MCP/SOQL hiccup a fresh attempt can fix. We DELIBERATELY no longer mark
        # `calls_read==0 with roles>0` as thin: that was the partner-led/APAC failure
        # mode — a deal with no Avoma calls (the partner runs them) but a rich
        # Next_Step__c log was flagged thin, re-run unchanged 3x, and dumped into
        # `failed`. The quality inspector above now OWNS the calls_read==0 case
        # (Avoma re-discovery + Next Step + tasks recovery), so a 0-Avoma record is a
        # COMPLETE record, not a retry candidate.
        if _agent_sf_blank:
            result["thin"] = True
            result["thin_reason"] = "sf_read_blank"
            print(f"[DEAL-SWEEP] thin record opp={opp_id}: sf_read_blank", flush=True)
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
        if not dry_run:
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


def manual_only() -> bool:
    """TEST PAUSE (2026-07-09, user-directed): when DEAL_SWEEP_MANUAL_ONLY is set, ALL
    automated sweeping is OFF — Salesforce-CDC triggers, scheduled/book runs, and the
    mase-worker fleet do NOTHING. Only an explicit per-deal MANUAL trigger runs, and it
    runs SYNCHRONOUSLY on the web process (never the worker). Flip the env back to false
    (or unset it) to resume automated sweeping."""
    return os.getenv("DEAL_SWEEP_MANUAL_ONLY", "false").strip().lower() in ("1", "true", "yes", "on")


async def enqueue_book_run(agent_manager, *, owner: Optional[str] = None,
                           opp_ids: Optional[list[str]] = None,
                           limit: int = 500, from_scratch: bool = False) -> dict:
    """Queue-mode sweep start. Resolve the book (the SAME report-as-book
    membership that is the single source of truth) and enqueue one `waiting` row
    per opp under a fresh run_id, then return immediately — the worker drains the
    queue. One book sweep at a time: refuses while rows are still waiting/working
    so a second click can't double-enqueue the book.
    """
    if manual_only():
        raise RuntimeError("manual-only mode is ON (DEAL_SWEEP_MANUAL_ONLY) — automated and "
                           "whole-book sweeps are disabled. Trigger a single deal manually to test.")
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

    # from_scratch=True => "fromscratch-*" run_id; the worker sees the prefix and runs
    # analyze_one(source="update_living_memory") so the record is rebuilt with NO
    # carry-forward (purges poisoned living memory). Normal runs keep a plain run_id.
    run_id = ("fromscratch-" if from_scratch else "") + uuid.uuid4().hex[:12]
    enqueued = await asyncio.to_thread(_queue.enqueue_book, run_id, opps)
    print(f"[DEAL-SWEEP] queue enqueue run={run_id} owner={owner or 'ALL'} "
          f"opps={enqueued}", flush=True)
    return {
        "run_id": run_id, "status": "queued", "mode": "queue",
        "owner": owner or "all-team", "total": enqueued,
        "note": ("enqueued; the sweep worker drains the queue. Poll "
                 "/api/deal-engine/sweep/status for progress."),
    }


async def enqueue_trigger(agent_manager, opp_id: str, *, source: str = "manual") -> str:
    """Queue-mode single-opp trigger (the Salesforce-update webhook). Enrich the
    opp's display labels cheaply (one SOQL, no agent run) so the dashboard row is
    populated, then enqueue exactly one `waiting` row. Idempotent: an opp already
    waiting/working is left as-is ("already_queued")."""
    opp_id = (opp_id or "").strip()
    if not opp_id:
        return "error"
    # MANUAL-ONLY TEST PAUSE: drop any AUTOMATED enqueue (Salesforce CDC sends
    # source="salesforce_trigger"; scheduled sub-jobs send "scheduled_*"). Only an
    # explicit source="manual" is honoured (and even that runs synchronously via the
    # endpoint, not this queue path). Never fills the queue while automation is paused.
    if manual_only() and source != "manual":
        print(f"[DEAL-SWEEP] manual-only mode: BLOCKED automated trigger opp={opp_id} "
              f"source={source!r} (automated sweeping is paused)", flush=True)
        return "blocked_manual_only"
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
    # Per-opp cooldown (debounce) — Salesforce/CDC path ONLY. A human clicking sweep
    # ("manual" / "trigger-") or a from-scratch rebuild always runs now and bypasses
    # this. For the automated path, collapse repeat triggers of the same opp inside
    # the cooldown window into a single sweep (see _trigger_cooldown_hours).
    if source in ("salesforce_trigger", "salesforce"):
        _cooldown_h = _trigger_cooldown_hours()
        if _cooldown_h > 0:
            _age = await asyncio.to_thread(_recent_sweep_age_hours, opp_id)
            if _age is not None and _age < _cooldown_h:
                print(f"[DEAL-SWEEP] trigger opp={opp_id} -> skipped_cooldown "
                      f"(swept {_age:.1f}h ago < {_cooldown_h:g}h window)", flush=True)
                return "skipped_cooldown"
    try:
        enriched = await _enrich_opp_ids(agent_manager, [opp_id])
        opp = enriched[0] if enriched else {"id": opp_id}
    except Exception as e:  # noqa: BLE001 — labels are best-effort; never block enqueue
        print(f"[DEAL-SWEEP] trigger enrich failed opp={opp_id}: "
              f"{type(e).__name__}: {e}", flush=True)
        opp = {"id": opp_id}
    opp.setdefault("id", opp_id)
    # Encode the ORIGIN in the run_id prefix so the worker can stamp the real source
    # on the run log (the dashboard shows "salesforce" for a CDC trigger, not "worker").
    _prefix = "sftrig" if source in ("salesforce_trigger", "salesforce") else "trigger"
    return await asyncio.to_thread(
        _queue.enqueue_one, f"{_prefix}-{opp_id[:15]}", opp)


async def start_sweep(agent_manager, *, owner: Optional[str] = None,
                      opp_ids: Optional[list[str]] = None, limit: int = 500,
                      concurrency: Optional[int] = None,
                      max_retries: Optional[int] = None,
                      from_scratch: bool = False) -> dict:
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
            agent_manager, owner=owner, opp_ids=opp_ids, limit=limit,
            from_scratch=from_scratch)
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
                    # Route through the SAME durable sweep queue as the manual
                    # sweep so scheduled discovery gets the resilient WORKER flow
                    # (patient rate-limit retries, backoff, quality inspector)
                    # instead of an in-process analyze_one on the web process's
                    # default config. The web only enqueues; the worker sweeps.
                    await asyncio.to_thread(_queue.enqueue_one, f"{source}-{key}", o)
                    return {"opp_id": o.get("id"), "status": "enqueued"}
                finally:
                    _trigger_inflight.discard(key)

        results = await asyncio.gather(*[_one(o) for o in capped],
                                       return_exceptions=True)
        for r in results:
            if isinstance(r, dict) and r.get("status") == "already_running":
                out["skipped_inflight"] += 1
                continue
            out["swept"] += 1
            # "enqueued" = handed to the durable worker queue (the resilient
            # path); count as success here, the worker reports real completion.
            if isinstance(r, dict) and r.get("status") in ("completed", "enqueued"):
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
                            # Same as discovery: enqueue to the durable queue so the
                            # resilient worker sweeps it (patient retries + inspector),
                            # rather than an in-process analyze_one on the web's config.
                            await asyncio.to_thread(
                                _queue.enqueue_one, f"{source}-{key}", o)
                            return {"opp_id": o.get("id"), "status": "enqueued"}
                        finally:
                            _trigger_inflight.discard(key)

                results = await asyncio.gather(*[_one(o) for o in opps],
                                               return_exceptions=True)
                for r in results:
                    if isinstance(r, dict) and r.get("status") == "already_running":
                        out["skipped_inflight"] += 1
                        continue
                    out["swept"] += 1
                    if isinstance(r, dict) and r.get("status") in ("completed", "enqueued"):
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
