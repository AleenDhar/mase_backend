"""Opportunity Analysis Agent.

A dedicated DeepAgent loop scoped to Salesforce + Avoma tools that produces
the OpportunityAnalysisRecord JSON for a single Opportunity. Triggered from
the Avoma webhook pipeline (server.py:_enrich_bg) after the 3-tier enrichment
completes; result is persisted to public.avoma_event_reports.opportunity_analysis_data.

Design:
- Lazy singleton: built on first call. Reuses the already-loaded MCP tools
  from the running agent_manager (`_cached_mcp_tools_by_server`) to avoid
  spinning up a second MCP client.
- Tool scope: only servers in _ALLOWED_SERVERS (salesforce + avoma) plus the
  always-allowed custom `get_current_time`. No write tools.
- Model: same MODEL env as the main chat agent (Claude Sonnet by default).
- Output: parsed JSON dict. Falls back to {"_error": ..., "_raw": ...} on
  parse failure so the row is still informative.
"""

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

from deepagents import create_deep_agent
from deepagents_patches import disable_write_todos

# Strip the built-in deepagents `write_todos` tool from this agent too.
disable_write_todos()

_ALLOWED_SERVERS = {"salesforce", "avoma"}
_PROMPT_PATH = Path(__file__).parent / "prompts" / "opportunity_analysis_system_prompt.md"

_agent_lock = asyncio.Lock()
_cached_agent = None
_cached_tool_names: list[str] = []


def _load_system_prompt() -> str:
    if not _PROMPT_PATH.exists():
        raise FileNotFoundError(f"system prompt missing: {_PROMPT_PATH}")
    return _PROMPT_PATH.read_text()


def _build_model():
    """Mirror server.py's model construction. Anthropic-only path is enough
    here; if MODEL is non-anthropic we fall through to init_chat_model.

    Override priority:
      1. OPP_ANALYZER_MODEL env (e.g. "anthropic:claude-sonnet-4-5")
      2. MODEL env (the main agent's model)
      3. Hard fallback "anthropic:claude-sonnet-4-5"
    Reason for override: the main MODEL env may pin a future-dated model
    id that 404s today. The analyzer runs unattended in the webhook
    pipeline, so it needs a model that's actually live right now.
    """
    selected = (
        os.getenv("OPP_ANALYZER_MODEL")
        or os.getenv("MODEL")
        or "anthropic:claude-sonnet-4-5"
    )
    if selected.startswith("anthropic:"):
        from anthropic_cache import CachedChatAnthropic
        return CachedChatAnthropic(
            model_name=selected.split(":", 1)[1],
            api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
            max_retries=int(os.getenv("ANTHROPIC_MAX_RETRIES", "2")),
            timeout=int(os.getenv("LLM_REQUEST_TIMEOUT_S", "180")),
            max_tokens=int(os.getenv("OPP_ANALYZER_MAX_TOKENS", "16000")),
            stop=None,
        )
    from langchain.chat_models import init_chat_model
    return init_chat_model(selected)


def _collect_scoped_tools(agent_manager) -> list:
    """Pull SF + Avoma tools from the live agent_manager cache."""
    by_server = getattr(agent_manager, "_cached_mcp_tools_by_server", {}) or {}
    tools = []
    for server_name, server_tools in by_server.items():
        if server_name in _ALLOWED_SERVERS:
            tools.extend(server_tools)
    return tools


async def _get_agent(agent_manager):
    global _cached_agent, _cached_tool_names
    async with _agent_lock:
        if _cached_agent is not None:
            return _cached_agent
        tools = _collect_scoped_tools(agent_manager)
        if not tools:
            raise RuntimeError(
                "opportunity_analyzer: no salesforce/avoma tools loaded yet "
                "(agent_manager._cached_mcp_tools_by_server empty)"
            )
        _cached_tool_names = [t.name for t in tools]
        # Bound intra-run context growth. Without this the analyzer replays every
        # accumulated tool_result on every LLM step; on large opps (many meetings
        # × transcript/notes/insights) the request balloons until a single
        # Anthropic call exceeds LLM_REQUEST_TIMEOUT_S and the whole run dies with
        # APITimeoutError. Same middleware the main chat agent uses (server.py).
        middleware = []
        if os.getenv("CONTEXT_TRIM_ENABLED", "true").lower() in ("1", "true", "yes"):
            try:
                from agent_checklist.context_trim_middleware import ContextTrimMiddleware
                middleware.append(
                    ContextTrimMiddleware(
                        threshold_tokens=int(os.getenv("CONTEXT_TRIM_THRESHOLD_TOKENS", "60000")),
                        keep_recent_messages=int(os.getenv("CONTEXT_TRIM_KEEP_RECENT_MESSAGES", "10")),
                        placeholder_max_chars=int(os.getenv("CONTEXT_TRIM_PLACEHOLDER_MAX_CHARS", "400")),
                    )
                )
            except Exception as _e:
                print(f"[OPP-ANALYZER] context-trim middleware unavailable: {_e}", flush=True)
        print(
            f"[OPP-ANALYZER] building agent with {len(tools)} tools "
            f"(servers: {sorted(_ALLOWED_SERVERS)}, middleware: {len(middleware)})",
            flush=True,
        )
        _cached_agent = create_deep_agent(
            tools=tools,
            system_prompt=_load_system_prompt(),
            subagents=[],
            model=_build_model(),
            middleware=middleware,
            debug=False,
        )
        return _cached_agent


def reset():
    """Drop the cached agent so the next call rebuilds with fresh tools.
    Call after MCP reload."""
    global _cached_agent, _cached_tool_names
    _cached_agent = None
    _cached_tool_names = []


def _extract_json(text: str) -> dict:
    """Parse the agent's final message into a dict.

    Strategy in order:
      1. Strict json.loads on the whole text.
      2. Strip leading/trailing ``` (with optional `json` tag) and try again
         — handles fenced blocks even if the closing fence is missing.
      3. Last-resort: largest balanced { ... } block.
    On total failure return a stub with the raw text (capped at 30k chars
    so big-but-malformed payloads are still inspectable).
    """
    text = (text or "").strip()
    if not text:
        return {"_error": "empty_response"}
    # 1. strict
    try:
        return json.loads(text)
    except Exception:
        pass
    # 2. fence-strip — robust to missing closing fence
    stripped = text
    m = re.match(r"^```(?:json)?\s*\n?", stripped)
    if m:
        stripped = stripped[m.end():]
    stripped = re.sub(r"\n?```\s*$", "", stripped).strip()
    if stripped and stripped != text:
        try:
            return json.loads(stripped)
        except Exception:
            pass
    # 3. largest balanced { ... } window via stack scan (respects strings + escapes)
    candidates: list[str] = []
    stack: list[int] = []
    in_str = False
    escape = False
    for i, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_str:
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            stack.append(i)
        elif ch == "}" and stack:
            start = stack.pop()
            if not stack:  # top-level balanced block
                candidates.append(text[start:i + 1])
    # Try longest balanced blocks first.
    for blob in sorted(candidates, key=len, reverse=True):
        try:
            return json.loads(blob)
        except Exception:
            continue
    return {"_error": "json_parse_failed", "_raw": text[:30000], "_raw_len": len(text)}


def _final_text(result: Any) -> str:
    """Pull the last AI message text out of the agent result."""
    msgs = result.get("messages") if isinstance(result, dict) else None
    if not msgs:
        return ""
    last = msgs[-1]
    content = getattr(last, "content", None) or (
        last.get("content") if isinstance(last, dict) else ""
    )
    if isinstance(content, list):
        # Anthropic content blocks: take all text parts.
        parts = []
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "text":
                parts.append(blk.get("text", ""))
            elif isinstance(blk, str):
                parts.append(blk)
        return "\n".join(parts)
    return str(content or "")


async def analyze_opportunity(
    agent_manager,
    opp_id: str,
    *,
    recursion_limit: Optional[int] = None,
    timeout_s: Optional[int] = None,
) -> dict:
    """Run the opportunity-analysis agent for a single Opp.

    Returns a dict with keys:
      data: parsed OpportunityAnalysisRecord (or {"_error": ...} stub)
      status: "completed" | "failed" | "parse_error"
      duration_ms: int
      tool_names: list[str] (scoped toolset, debug aid)
      error: str | None

    `recursion_limit` / `timeout_s` fall back to env (OPP_ANALYZER_RECURSION_LIMIT
    / OPP_ANALYZER_TIMEOUT_S) so the documented overrides actually take effect.
    Default timeout raised 600→900s: large opps (many meetings × transcript/
    notes/insights reads) legitimately need more than 600s to walk their full
    recursion budget; runs are fire-and-forget background tasks, so a longer
    outer bound is safe and is still hard-capped by recursion_limit.
    """
    if recursion_limit is None:
        recursion_limit = int(os.getenv("OPP_ANALYZER_RECURSION_LIMIT", "80"))
    if timeout_s is None:
        timeout_s = int(os.getenv("OPP_ANALYZER_TIMEOUT_S", "900"))
    t0 = time.time()
    out = {
        "data": None,
        "status": "pending",
        "duration_ms": 0,
        "tool_names": [],
        "error": None,
    }
    _skip_token = None
    try:
        agent = await _get_agent(agent_manager)
        out["tool_names"] = list(_cached_tool_names)
        # Skip the per-tool gpt-4o-mini summariser for this run. On large
        # Salesforce records it times out at 45s each and grinds the analyzer
        # into its outer timeout; deterministic truncation gives the same data
        # far faster. Scoped to this coroutine via ContextVar (set BEFORE the
        # coroutine is created so it propagates into LangGraph's tool tasks).
        try:
            import server
            _skip_token = server._skip_llm_summarizer.set(True)
        except Exception as _e:
            print(f"[OPP-ANALYZER] could not set summariser-skip flag: {_e}", flush=True)
        user_msg = (
            f"Analyze Salesforce Opportunity Id `{opp_id}` end-to-end per your "
            f"system prompt and emit the OpportunityAnalysisRecord JSON. "
            f"Output JSON only, no preamble."
        )
        coro = agent.ainvoke(
            {"messages": [{"role": "user", "content": user_msg}]},
            config={"recursion_limit": recursion_limit},
        )
        result = await asyncio.wait_for(coro, timeout=timeout_s)
        text = _final_text(result)
        parsed = _extract_json(text)
        out["data"] = parsed
        if isinstance(parsed, dict) and parsed.get("_error"):
            out["status"] = "parse_error"
            out["error"] = parsed.get("_error")
        else:
            out["status"] = "completed"
    except asyncio.TimeoutError:
        out["status"] = "failed"
        out["error"] = f"timeout after {timeout_s}s"
    except Exception as e:
        out["status"] = "failed"
        out["error"] = f"{type(e).__name__}: {str(e)[:500]}"
    finally:
        if _skip_token is not None:
            try:
                import server
                server._skip_llm_summarizer.reset(_skip_token)
            except Exception:
                pass
        out["duration_ms"] = int((time.time() - t0) * 1000)
    return out
