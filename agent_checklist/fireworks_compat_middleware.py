"""Fireworks (OpenAI-compatible) message-compatibility middleware.

WHY THIS EXISTS.  Fireworks' /chat/completions validates the request body
strictly and REJECTS LangChain's content-block `id` field, e.g.::

    400 invalid_request_error: Extra inputs are not permitted,
        field: messages[3].content.list[ChatMessageContent][0].id, value: "lc_04c0..."

A trivial single-turn call has no such history (smoke tests pass), but a real
tool-using agent run accumulates AIMessage / ToolMessage objects whose
`.content` is a *list of content blocks*, and LangChain stamps each block with
an auto-generated `id` ("lc_..."). On every model step those messages are
replayed, so every multi-step Fireworks run 400s. Anthropic/OpenAI tolerate the
extra field; Fireworks does not.

THE FIX.  Right before each model call, normalise message content for the
OpenAI-compatible path:

  * a list of TEXT blocks -> a single plain string (drops the ids entirely —
    the common case for text agents like ABM);
  * a mixed list (e.g. vision) -> keep the list but strip the keys Fireworks
    rejects from each block.

Attach ONLY on the fireworks branch (server.py). Anthropic is untouched, so its
prompt-cache `cache_control` blocks keep working.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

# Keys LangChain attaches to content blocks that OpenAI-compatible endpoints
# (Fireworks) reject with "Extra inputs are not permitted". type/text/image_url
# are the only keys the OpenAI content schema accepts.
_DROP_BLOCK_KEYS = {"id", "index", "cache_control", "annotations", "metadata"}


def _normalise_content(content: list) -> tuple[Any, bool]:
    """Return (new_content, changed) for one message's list content."""
    # Common case: every block is text -> collapse to a single string, which
    # drops the `id` (and every other block key) cleanly.
    if all(isinstance(b, dict) and b.get("type") in ("text", None) for b in content):
        text = "".join(
            (b.get("text", "") if isinstance(b, dict) else str(b)) for b in content
        )
        return text, True

    # Mixed content (e.g. text + image for a vision model): preserve the block
    # structure but strip the keys Fireworks rejects.
    cleaned = []
    changed = False
    for b in content:
        if isinstance(b, dict) and (_DROP_BLOCK_KEYS & b.keys()):
            cleaned.append({k: v for k, v in b.items() if k not in _DROP_BLOCK_KEYS})
            changed = True
        else:
            cleaned.append(b)
    return cleaned, changed


def _sanitize(messages: list[Any]) -> int:
    """Normalise message content in place. Handles both LangChain message
    objects (``.content``) and plain dicts (``{"content": ...}``). Returns how
    many messages changed."""
    fixed = 0
    for m in messages:
        is_dict = isinstance(m, dict)
        content = m.get("content") if is_dict else getattr(m, "content", None)
        if not isinstance(content, list):
            continue  # already a plain string / None — nothing to do

        new_content, changed = _normalise_content(content)
        if not changed:
            continue
        try:
            if is_dict:
                m["content"] = new_content
            else:
                m.content = new_content
            fixed += 1
        except Exception:
            pass
    return fixed


class FireworksCompatMiddleware(AgentMiddleware):
    """Flattens LangChain content blocks (dropping the `id` field Fireworks
    rejects) before every model call. Attach ONLY on the fireworks branch."""

    def _fix(self, request: ModelRequest) -> None:
        n = _sanitize(request.messages)
        if n:
            print(f"[FIREWORKS-COMPAT] normalised {n} message(s) for OpenAI-compat", flush=True)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: "Callable[[ModelRequest], Any]",
    ) -> Any:
        self._fix(request)
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: "Callable[[ModelRequest], Awaitable[Any]]",
    ) -> Any:
        self._fix(request)
        return await handler(request)
