"""Intra-run context-trim middleware.

Cuts cost on long agent runs by shrinking old `ToolMessage` content before
every LLM call, *without* dropping or reordering messages and *without*
touching the most recent K messages. Tool_call_id pairing is preserved by
construction (we only edit `.content` on `ToolMessage` objects in place).

Why this exists.  The existing `ContextWindowManager.summarize_conversation_history`
in `server.py` runs ONCE before `agent.astream()` is invoked on the user-message
history loaded from Supabase. It does not reach into the agent's internal state
during the run. Inside a single astream call, the agent can fire 30+ tool calls;
every tool_result accumulates in `state["messages"]` and is re-sent on every
subsequent LLM step. By turn N of a heavy run, each LLM call is replaying
hundreds of KB of historical tool output (mostly cache_read, but still billed).

This middleware addresses that intra-run growth directly. It is safe to combine
with the existing pre-astream summarizer.

Behaviour.

  * Hook: `awrap_model_call` (and the sync `wrap_model_call` fallback).
  * Trigger: estimated message tokens > `threshold_tokens` (default 60_000).
  * Action: walk messages OLDEST → NEWEST, skip the last `keep_recent_messages`,
    and for every `ToolMessage` whose content exceeds `placeholder_max_chars`,
    replace `.content` with a short placeholder of the form
    `[Earlier <tool_name> result summarised: <orig_chars> chars; first 200: ...]`
  * `HumanMessage`, `AIMessage`, `SystemMessage`, `ToolMessage` smaller than
    `placeholder_max_chars`, and the last K messages are untouched.
  * The full tool output remains on disk (the `_wrap_mcp_tool` wrapper already
    persists raw payloads to `mcp_output/` and `chat_messages`).

Why ToolMessage content only.  Anthropic's API requires every `tool_use` block
in an assistant message to be paired with a matching `tool_result` (matched by
`tool_call_id`) in the immediately following user-role message. If we dropped
or reordered messages, those pairs would break and the API would reject the
request. Editing the `.content` of a `ToolMessage` in place leaves the
`tool_call_id` and message order intact, so the pairing always holds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelRequest,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain_core.messages import AnyMessage


_DEFAULT_THRESHOLD_TOKENS = 60_000
_DEFAULT_KEEP_RECENT = 10
_DEFAULT_PLACEHOLDER_CAP = 400


def _estimate_tokens(messages: list[Any]) -> int:
    total = 0
    for m in messages:
        content = getattr(m, "content", None)
        if content is None and isinstance(m, dict):
            content = m.get("content", "")
        total += len(str(content or "")) // 4
    return total


def _shrink_old_tool_messages(
    messages: list[Any],
    keep_recent: int,
    placeholder_cap: int,
) -> tuple[list[Any], int, int]:
    """Return (new_messages, rewritten_count, bytes_saved).

    Walks messages oldest → newest, skipping the last `keep_recent`. For every
    `ToolMessage` older than that whose stringified content is longer than
    `placeholder_cap`, replace `.content` with a short placeholder. Other
    message types and recent messages are untouched.
    """
    from langchain_core.messages import ToolMessage

    if len(messages) <= keep_recent:
        return messages, 0, 0

    head_end = len(messages) - keep_recent
    rewritten = 0
    bytes_saved = 0

    for i in range(head_end):
        m = messages[i]
        if not isinstance(m, ToolMessage):
            continue
        content_str = str(getattr(m, "content", "") or "")
        if len(content_str) <= placeholder_cap:
            continue
        tool_name = getattr(m, "name", None) or "tool"
        snippet = content_str[:200].replace("\n", " ").replace("\r", " ")
        placeholder = (
            f"[Earlier {tool_name} result summarised: "
            f"{len(content_str)} chars omitted; first 200: {snippet}...]"
        )[:placeholder_cap]
        bytes_saved += len(content_str) - len(placeholder)
        try:
            m.content = placeholder
            rewritten += 1
        except Exception:
            try:
                messages[i] = ToolMessage(
                    content=placeholder,
                    tool_call_id=getattr(m, "tool_call_id", ""),
                    name=getattr(m, "name", None),
                )
                rewritten += 1
            except Exception:
                continue

    return messages, rewritten, bytes_saved


class ContextTrimMiddleware(AgentMiddleware):
    """Shrinks old `ToolMessage` content before every LLM call when the
    estimated message-token total exceeds `threshold_tokens`. Never drops or
    reorders messages; never touches the last `keep_recent_messages`.

    Args:
        threshold_tokens: estimated token budget that triggers a trim.
            Defaults to 60,000 (about 240 KB of message content).
        keep_recent_messages: how many tail messages are always untouched.
            Defaults to 10. Must be enough that the active tool_call /
            tool_result pair for the current LLM step is never rewritten.
        placeholder_max_chars: cap on the placeholder string substituted for
            each large tool result. Defaults to 400.
    """

    def __init__(
        self,
        threshold_tokens: int = _DEFAULT_THRESHOLD_TOKENS,
        keep_recent_messages: int = _DEFAULT_KEEP_RECENT,
        placeholder_max_chars: int = _DEFAULT_PLACEHOLDER_CAP,
    ) -> None:
        super().__init__()
        self.threshold_tokens = max(1_000, int(threshold_tokens))
        self.keep_recent_messages = max(4, int(keep_recent_messages))
        self.placeholder_max_chars = max(80, int(placeholder_max_chars))

    def _maybe_trim(self, request: ModelRequest) -> None:
        before = _estimate_tokens(request.messages)
        if before <= self.threshold_tokens:
            return
        _, rewritten, bytes_saved = _shrink_old_tool_messages(
            request.messages,
            self.keep_recent_messages,
            self.placeholder_max_chars,
        )
        if rewritten == 0:
            return
        after = _estimate_tokens(request.messages)
        print(
            f"[CONTEXT-TRIM] {before:,} → {after:,} tokens "
            f"(rewrote {rewritten} ToolMessage{'s' if rewritten != 1 else ''}, "
            f"saved {bytes_saved:,} chars; kept last {self.keep_recent_messages})",
            flush=True,
        )

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: "Callable[[ModelRequest], Any]",
    ) -> Any:
        self._maybe_trim(request)
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: "Callable[[ModelRequest], Awaitable[Any]]",
    ) -> Any:
        self._maybe_trim(request)
        return await handler(request)
