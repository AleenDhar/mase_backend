"""
Anthropic prompt-caching wrapper for ChatAnthropic.

Auto-injects `cache_control: {type: "ephemeral"}` on:
  * the system prompt block (so the system prompt is cached for ~5 min)
  * the LAST tool in the tools array (caches the *entire* tool definitions array
    per Anthropic's caching rules)
  * the LAST message in the conversation history (caches the *growing
    conversation tail* — accumulated user/tool-result messages from previous
    agent steps). Added 2026-05-20 after chat 0c95cef2 sat at 48% cache hit
    on a 75-step turn because accumulated tool results were paid uncached on
    every LLM call. With this third breakpoint, step N+1 reads-cache for
    everything up to step N's last message, lifting hit rate toward 80%+.

Cached input is billed at:
  * cache WRITE: 1.25x the normal input rate (only on the first call that builds
    the cache)
  * cache READ : 0.10x the normal input rate (all subsequent calls within 5 min)

Anthropic allows up to 4 cache_control breakpoints per request; we use 3.

For a 251-tool agent with a long system prompt, this typically cuts per-call
input cost by 70-90% on the second and later steps of a turn.
"""

from __future__ import annotations

import asyncio
import os
import random
import time

from langchain_anthropic import ChatAnthropic

# ---------------------------------------------------------------------------
# Retry-on-529 (overloaded_error)
# ---------------------------------------------------------------------------
# Anthropic returns HTTP 529 with `{"type":"overloaded_error"}` when the model
# fleet is at capacity (typically US-business-hours peaks for the newest
# Sonnet/Opus). The official SDK does NOT include 529 in its default
# retryable-status set (only 408/409/429/500/502/503/504), so a single 529 =
# immediate failure with no second chance — which is exactly what burned chat
# 87b2b881 (Phase 1 died on the first LLM call before any tool ran).
#
# Hooked in here (rather than in pipeline_runner) so it covers EVERY caller
# of CachedChatAnthropic — agent loop, verifier, summariser, reply classifier.
_OVERLOADED_MAX_RETRIES = int(os.getenv("ANTHROPIC_OVERLOADED_MAX_RETRIES", "2"))
_OVERLOADED_BASE_BACKOFF_S = float(os.getenv("ANTHROPIC_OVERLOADED_BASE_BACKOFF_S", "2.0"))


def _is_overloaded(exc: BaseException) -> bool:
    """True iff `exc` is an Anthropic 529 overloaded_error."""
    sc = getattr(exc, "status_code", None)
    if sc == 529:
        return True
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error") or {}
        if isinstance(err, dict) and err.get("type") == "overloaded_error":
            return True
    msg = str(exc)
    return "Error code: 529" in msg or "overloaded_error" in msg


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter: 2s, 4s, 8s, ... ± up to 1s."""
    return _OVERLOADED_BASE_BACKOFF_S * (2 ** attempt) + random.uniform(0, 1)


class CachedChatAnthropic(ChatAnthropic):
    """ChatAnthropic with automatic prompt caching on system + tools,
    PLUS retry-on-529 (overloaded_error) since the SDK omits 529 from its
    default retryable set."""

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        _apply_cache_control(payload)
        return payload

    # --- sync paths ---
    def _generate(self, *args, **kwargs):
        for attempt in range(_OVERLOADED_MAX_RETRIES + 1):
            try:
                return super()._generate(*args, **kwargs)
            except Exception as exc:
                if attempt >= _OVERLOADED_MAX_RETRIES or not _is_overloaded(exc):
                    raise
                delay = _backoff_delay(attempt)
                print(f"[anthropic-retry] 529 overloaded (generate), "
                      f"retry {attempt + 1}/{_OVERLOADED_MAX_RETRIES} "
                      f"in {delay:.1f}s")
                time.sleep(delay)

    def _stream(self, *args, **kwargs):
        # Only safe to retry if NO chunks have been yielded — once the caller
        # has consumed partial output, replaying would duplicate it.
        for attempt in range(_OVERLOADED_MAX_RETRIES + 1):
            try:
                yielded_any = False
                for chunk in super()._stream(*args, **kwargs):
                    yielded_any = True
                    yield chunk
                return
            except Exception as exc:
                if (yielded_any or attempt >= _OVERLOADED_MAX_RETRIES
                        or not _is_overloaded(exc)):
                    raise
                delay = _backoff_delay(attempt)
                print(f"[anthropic-retry] 529 overloaded (stream), "
                      f"retry {attempt + 1}/{_OVERLOADED_MAX_RETRIES} "
                      f"in {delay:.1f}s")
                time.sleep(delay)

    # --- async paths (this is what the agent loop actually hits) ---
    async def _agenerate(self, *args, **kwargs):
        for attempt in range(_OVERLOADED_MAX_RETRIES + 1):
            try:
                return await super()._agenerate(*args, **kwargs)
            except Exception as exc:
                if attempt >= _OVERLOADED_MAX_RETRIES or not _is_overloaded(exc):
                    raise
                delay = _backoff_delay(attempt)
                print(f"[anthropic-retry] 529 overloaded (agenerate), "
                      f"retry {attempt + 1}/{_OVERLOADED_MAX_RETRIES} "
                      f"in {delay:.1f}s")
                await asyncio.sleep(delay)

    async def _astream(self, *args, **kwargs):
        for attempt in range(_OVERLOADED_MAX_RETRIES + 1):
            yielded_any = False
            try:
                async for chunk in super()._astream(*args, **kwargs):
                    yielded_any = True
                    yield chunk
                return
            except Exception as exc:
                if (yielded_any or attempt >= _OVERLOADED_MAX_RETRIES
                        or not _is_overloaded(exc)):
                    raise
                delay = _backoff_delay(attempt)
                print(f"[anthropic-retry] 529 overloaded (astream), "
                      f"retry {attempt + 1}/{_OVERLOADED_MAX_RETRIES} "
                      f"in {delay:.1f}s")
                await asyncio.sleep(delay)


def _apply_cache_control(payload: dict) -> None:
    cc = {"type": "ephemeral"}

    # 1. Cache the system prompt.
    system = payload.get("system")
    if isinstance(system, str) and system:
        payload["system"] = [
            {"type": "text", "text": system, "cache_control": cc}
        ]
    elif isinstance(system, list) and system:
        for block in reversed(system):
            if isinstance(block, dict) and block.get("type") == "text":
                block.setdefault("cache_control", cc)
                break

    # 2. Cache the tool definitions (mark on the last tool -> caches the array).
    tools = payload.get("tools")
    if isinstance(tools, list) and tools:
        last = tools[-1]
        if isinstance(last, dict):
            last["cache_control"] = cc

    # 3. Cache the growing conversation tail by marking the last message.
    # Anthropic caches the prefix up to and including the breakpoint, so on
    # step N+1 the entire history up to step N's marker is read from cache
    # instead of re-billed as uncached input. The marker moves forward each
    # step (new cache write of the delta, cache read of everything before).
    msgs = payload.get("messages")
    if isinstance(msgs, list) and msgs:
        last_msg = msgs[-1]
        if isinstance(last_msg, dict):
            content = last_msg.get("content")
            if isinstance(content, str) and content:
                last_msg["content"] = [
                    {"type": "text", "text": content, "cache_control": cc}
                ]
            elif isinstance(content, list) and content:
                # Attach cache_control to the last cacheable block. Anthropic
                # accepts cache_control on text / tool_use / tool_result /
                # image blocks; skip anything else.
                _CACHEABLE = {"text", "tool_use", "tool_result", "image", "document"}
                for block in reversed(content):
                    if isinstance(block, dict) and block.get("type") in _CACHEABLE:
                        block.setdefault("cache_control", cc)
                        break
