"""deal_engine_chat_agent.py — the RevOps chat as a tool-using deep agent.

Upgrades /api/deal-engine/chat from a one-shot OpenAI completion into a deep agent
that can:
  - retrieve from the SHARED MASE knowledge base (search_knowledge, routed to the
    isolated MASE namespace — the same store the sweep + todo-runner use), and
  - delegate ONE tactical email-drafting to-do to the Todo Runner (run_todo), which
    runs as a SEPARATE deep agent with its own Supabase prompt + Salesforce/Avoma/
    Showpad/knowledge tools (mirrors deal_engine_sweep's independent-agent pattern).

The chat's system prompt is admin-editable in Supabase (agent_prompt_store ID_CHAT);
server.py appends the book context + a capabilities block describing exactly what the
Todo Runner can and cannot do, so the chat knows when to delegate. Model + helpers are
reused from opportunity_analyzer (OpenAI by default, same as the sweep).
"""
from __future__ import annotations

import asyncio
import os
import uuid

from deepagents import create_deep_agent
from deepagents_patches import disable_write_todos
from langchain_core.tools import tool

import opportunity_analyzer as _oa  # _build_model / _final_text

disable_write_todos()

# Kept in sync with custom_tools.search_knowledge._MASE_KNOWLEDGE_PROJECT_ID,
# deal_engine_sweep.MASE_KNOWLEDGE_PROJECT_ID and the frontend marker.
MASE_KNOWLEDGE_PROJECT_ID = "7e9b2f48-3c1a-4d6e-8b05-9a2c4f1d7e30"

# Servers the Todo Runner sub-agent is allowed to use (it drafts prospect emails:
# Salesforce for real references, Avoma for call context, Showpad for collateral).
_TODO_SERVERS = {"salesforce", "avoma", "showpad"}


def _search_knowledge_tool(agent_manager):
    for ct in (getattr(agent_manager, "_cached_custom_tools", []) or []):
        if getattr(ct, "name", "") == "search_knowledge":
            return ct
    return None


def _middleware():
    mw = []
    if os.getenv("CONTEXT_TRIM_ENABLED", "true").lower() in ("1", "true", "yes"):
        try:
            from agent_checklist.context_trim_middleware import ContextTrimMiddleware
            mw.append(ContextTrimMiddleware(
                threshold_tokens=int(os.getenv("CONTEXT_TRIM_THRESHOLD_TOKENS", "120000")),
                keep_recent_messages=int(os.getenv("CONTEXT_TRIM_KEEP_RECENT_MESSAGES", "14")),
                placeholder_max_chars=int(os.getenv("CONTEXT_TRIM_PLACEHOLDER_MAX_CHARS", "400")),
            ))
        except Exception as _e:  # noqa: BLE001
            print(f"[CHAT-AGENT] context-trim middleware unavailable: {_e}", flush=True)
    return mw


def _load_todo_prompt() -> str:
    """The Todo Runner system prompt — Supabase override first, on-disk seed fallback."""
    try:
        import agent_prompt_store as aps
        p = aps.get_prompt(aps.ID_TODO_RUNNER)
        if (p or "").strip():
            return p
    except Exception:  # noqa: BLE001
        pass
    try:
        from pathlib import Path
        import agent_prompt_store as aps
        seed = (Path(__file__).parent / "prompts" / "todo_runner_system_prompt.md").read_text(encoding="utf-8")
        return aps.strip_leading_banner(seed)
    except Exception:  # noqa: BLE001
        return ("You are MASE's Tactical Fulfillment Agent. Complete ONE tactical, "
                "prospect-facing to-do by DRAFTING a single outbound email. Gate out "
                "anything that needs a human (reply 'NEEDS HUMAN: <who and why>'); never "
                "invent facts; output the email draft only.")


async def _safe_emit(emit, t: str, c: str, meta: dict) -> None:
    """Fire one trace callback, swallowing any error so a bad callback never
    breaks the Todo Runner stream. No-op when emit is None."""
    if emit is None:
        return
    try:
        await emit(t, c, meta)
    except Exception as _e:  # noqa: BLE001
        print(f"[CHAT-AGENT] todo emit failed ({t}): {_e}", flush=True)


def _ai_text(msg) -> str:
    """Extract plain assistant text from an AIMessage's content — mirrors
    server.py's run_agent_and_save extraction (list-of-blocks or str)."""
    raw = getattr(msg, "content", None)
    if not raw:
        return ""
    if isinstance(raw, list):
        parts = [b.get("text", "") for b in raw
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(parts).strip()
    return str(raw).strip()


async def _run_todo(agent_manager, task: str, *, account: str = "",
                    contact: str = "", opportunity_id: str = "",
                    emit=None) -> str:
    """Run the Todo Runner as a standalone deep agent for ONE to-do and return its
    output (an email draft, or a 'NEEDS HUMAN: ...' line).

    When `emit` is supplied, the Todo Runner is STREAMED (not blocking-invoked) and
    each of its internal steps — assistant thinking, tool calls, tool results — is
    pushed through `emit(type, content, meta)` tagged `{"group": "todo"}` so the
    parent chat can render a nested live sub-trace. The full final draft is still
    accumulated and RETURNED so the parent's run_todo tool_result carries it."""
    by_server = getattr(agent_manager, "_cached_mcp_tools_by_server", {}) or {}
    tools = []
    for server_name, server_tools in by_server.items():
        if server_name in _TODO_SERVERS:
            tools.extend(server_tools)
    sk = _search_knowledge_tool(agent_manager)
    if sk is not None:
        tools.append(sk)
    if not tools:
        return ("NEEDS HUMAN: cannot run the to-do — the Salesforce/Showpad/knowledge "
                "tools are not loaded right now.")

    prompt = await asyncio.get_running_loop().run_in_executor(None, _load_todo_prompt)
    agent = create_deep_agent(
        tools=tools, system_prompt=prompt, subagents=[],
        model=_oa._build_model(), middleware=_middleware(), debug=False)

    # Route this sub-run's search_knowledge to the MASE namespace, with its OWN
    # chat_id so its per-turn retrieval cap/dedupe doesn't collide with the chat's.
    import rag_context as _rag
    tok_p = _rag.current_project_id.set(MASE_KNOWLEDGE_PROJECT_ID)
    tok_c = _rag.current_chat_id.set(f"chat-todo:{uuid.uuid4().hex[:8]}")
    try:
        ctx_lines = []
        if account:
            ctx_lines.append(f"Account: {account}")
        if contact:
            ctx_lines.append(f"Prospect contact: {contact}")
        if opportunity_id:
            ctx_lines.append(f"Opportunity Id: {opportunity_id}")
        user_msg = task if not ctx_lines else task + "\n\nContext:\n" + "\n".join(ctx_lines)

        cfg = {"recursion_limit": int(os.getenv("CHAT_TODO_RECURSION_LIMIT", "60"))}
        # Headroom for a real multi-search Showpad + Salesforce + draft run. The chat
        # endpoint is async (streams to chat_messages), and the Todo Runner streams a
        # sub-row per step, so a longer run keeps the UI's watchdog alive rather than
        # blocking a request. Working DIRECTLY (no sub-agents, per the prompt) keeps
        # most runs well under this.
        timeout_s = int(os.getenv("CHAT_TODO_TIMEOUT_S", "600"))

        # If no callback was passed, keep the cheap blocking path (callers that
        # don't want a live trace, e.g. tests / non-streaming endpoints).
        if emit is None:
            result = await asyncio.wait_for(
                agent.ainvoke(
                    {"messages": [{"role": "user", "content": user_msg}]},
                    config=cfg,
                ),
                timeout=timeout_s,
            )
            return _oa._final_text(result) or "(the Todo Runner returned no output)"

        # Streaming path: mirror server.py _agent_astream_autocontinue's
        # stream_mode="values" + the same per-message extraction/dedupe so each
        # Todo Runner step shows up live in the parent chat's nested sub-trace.
        last_chunk = None
        final_text = ""          # last non-tool-calling assistant text seen
        seen_tool_calls = set()  # name+id, dedupes repeated tool_call rows
        seen_tool_results = set()  # tool_call_id, dedupes repeated tool_result rows
        emitted_thinking = ""    # last thinking emitted, avoids dup consecutive

        async def _run_stream():
            nonlocal last_chunk, final_text, emitted_thinking
            async for chunk in agent.astream(
                {"messages": [{"role": "user", "content": user_msg}]},
                stream_mode="values", config=cfg,
            ):
                last_chunk = chunk
                if not isinstance(chunk, dict) or not chunk.get("messages"):
                    continue
                msg = chunk["messages"][-1]
                mtype = type(msg).__name__

                if mtype == "AIMessage":
                    has_tc = bool(getattr(msg, "tool_calls", None))
                    text = _ai_text(msg)
                    if text:
                        if has_tc:
                            # Reasoning that precedes tool calls -> thinking row.
                            if text != emitted_thinking:
                                emitted_thinking = text
                                await _safe_emit(emit, "thinking", text,
                                                 {"group": "todo"})
                        else:
                            # Plain assistant text: part of the running draft.
                            final_text = text
                    if has_tc:
                        for tc in msg.tool_calls:
                            name = tc.get("name", "unknown")
                            key = f"{name}_{tc.get('id', '')}"
                            if key in seen_tool_calls:
                                continue
                            seen_tool_calls.add(key)
                            await _safe_emit(emit, "tool_call", name, {
                                "group": "todo",
                                "tool": name,
                                "args": tc.get("args", {}),
                                "tool_call_id": tc.get("id"),
                            })
                elif mtype == "ToolMessage":
                    tcid = getattr(msg, "tool_call_id", "unknown")
                    if tcid in seen_tool_results:
                        continue
                    seen_tool_results.add(tcid)
                    name = getattr(msg, "name", "unknown")
                    await _safe_emit(emit, "tool_result", str(msg.content), {
                        "group": "todo",
                        "tool": name,
                    })

        await asyncio.wait_for(_run_stream(), timeout=timeout_s)
        # Prefer the helper's final-text extraction of the last chunk; fall back
        # to the last plain assistant text we accumulated.
        return (_oa._final_text(last_chunk) if last_chunk else "") \
            or final_text or "(the Todo Runner returned no output)"
    except asyncio.TimeoutError:
        return "NEEDS HUMAN: the Todo Runner timed out completing this to-do."
    except Exception as e:  # noqa: BLE001
        return f"NEEDS HUMAN: the Todo Runner failed: {e}"
    finally:
        try:
            _rag.current_project_id.reset(tok_p)
            _rag.current_chat_id.reset(tok_c)
        except Exception:  # noqa: BLE001
            pass


def _make_run_todo_tool(agent_manager, emit=None):
    @tool
    async def run_todo(task: str, account: str = "", contact: str = "",
                       opportunity_id: str = "") -> str:
        """Delegate ONE tactical, prospect-facing to-do to the Todo Runner agent, which
        DRAFTS a single outbound email to complete it. Use this whenever the user asks you
        to draft / write / send / follow up with an email for a specific to-do.

        The Todo Runner CAN: draft one outbound email for a tactical to-do that needs no
        internal collaboration; retrieve real facts from Showpad, Salesforce (real
        closed-won references) and the MASE knowledge base; attach relevant Showpad
        collateral (as shareable links); and it never invents customers, prices, or claims.
        It CANNOT and WILL NOT: send the email (a human reviews and sends); do anything that
        needs a manager/exec, legal, security, the pricing desk, a sales engineer, product,
        or a partner — for those it returns a single line 'NEEDS HUMAN: <who and why>'.

        Pass the to-do in plain words plus any context you know.
        Args:
            task: the to-do to complete, in plain words.
            account: prospect/account name, if known.
            contact: the named prospect contact, if known.
            opportunity_id: Salesforce opportunity id, if known.
        Returns the Todo Runner's output verbatim (an email draft, or a NEEDS HUMAN line)."""
        return await _run_todo(agent_manager, task, account=account,
                               contact=contact, opportunity_id=opportunity_id,
                               emit=emit)
    return run_todo


def build_chat_agent(agent_manager, system_prompt: str, emit=None):
    """Build the RevOps chat deep agent: search_knowledge (shared MASE KB) + run_todo
    (delegate to the Todo Runner). Raises if neither tool is available so the caller can
    fall back to the plain one-shot completion.

    `emit` is an optional async callback `emit(type, content, meta)` created by the
    caller (server.py) and threaded into run_todo so the Todo Runner's internal
    steps stream live into the same chat feed (tagged meta {"group": "todo"}). This
    module must NOT import server.py — the callback is injected from outside."""
    tools = []
    sk = _search_knowledge_tool(agent_manager)
    if sk is not None:
        tools.append(sk)
    tools.append(_make_run_todo_tool(agent_manager, emit=emit))
    return create_deep_agent(
        tools=tools, system_prompt=system_prompt, subagents=[],
        model=_oa._build_model(), middleware=_middleware(), debug=False)
