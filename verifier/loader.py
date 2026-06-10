"""Pure-ish data access layer for the verifier.

`chat_messages` is the system of record: every tool call the agent fires is
already persisted there as `type='tool_call'` with `metadata.tool` and
`metadata.args` (full LangChain payload — see `audit_spike/READOUT.md`).

`server.py` writes `tool_call` rows from THREE paths (tool_wrapper, sync
handler, stream handler), so the same call can appear 2-3 times. We dedupe
on the LangChain `tool_call.id` when present, and fall back to a
`(tool, sorted(args))` tuple otherwise. This mirrors the spike's behaviour
but pins to a stable id when available.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from supabase import Client, create_client


SUPABASE_PAGE_SIZE = 1000


@dataclass(frozen=True)
class ToolCall:
    """One deduped tool call as observed in the chat log."""

    sequence: int
    created_at: str | None
    tool: str
    args: dict
    tool_call_id: str | None
    source: str | None  # which write path logged it (best-effort)


def _coerce_metadata(md: Any) -> dict:
    if isinstance(md, str):
        try:
            return json.loads(md)
        except json.JSONDecodeError:
            return {}
    return md or {}


def _stable_args_key(args: dict) -> str:
    """Order-independent JSON key for arg-based dedupe."""
    try:
        return json.dumps(args, sort_keys=True, default=str)
    except Exception:
        return str(args)


def next_sequence(chat_id: str, sb: Client) -> int:
    """Return the next safe `chat_messages.sequence` value for `chat_id`.

    The frontend orders the live chat feed by `sequence` and uses
    `sequence > current_max` to detect new messages over its realtime
    subscription. Verifier writes that omit `sequence` default to 0,
    which the UI treats as "older than everything" and silently drops
    from the live view (only reload re-fetches them via created_at
    tiebreaker).

    We query MAX(sequence) for the chat and add 1. Safe because the
    verifier always runs AFTER the agent's turn has finished — there
    is no concurrent writer at this point.
    """
    try:
        res = (
            sb.table("chat_messages")
            .select("sequence")
            .eq("chat_id", chat_id)
            .order("sequence", desc=True)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        current_max = int(rows[0]["sequence"]) if rows and rows[0].get("sequence") is not None else 0
        return current_max + 1
    except Exception as e:
        print(f"[VERIFIER] next_sequence query failed (defaulting to 1): {e}")
        return 1


def _build_supabase_client() -> Client | None:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        return None
    try:
        return create_client(url, key)
    except Exception:
        return None


def _fetch_rows(
    supabase: Client,
    chat_id: str,
    type_filter: str,
) -> list[dict]:
    """Page through chat_messages rows of a given type for a chat."""
    out: list[dict] = []
    offset = 0
    while True:
        res = (
            supabase.table("chat_messages")
            .select("sequence, metadata, created_at, content")
            .eq("chat_id", chat_id)
            .eq("type", type_filter)
            .order("sequence")
            .order("created_at")
            .range(offset, offset + SUPABASE_PAGE_SIZE - 1)
            .execute()
        )
        rows = res.data or []
        out.extend(rows)
        if len(rows) < SUPABASE_PAGE_SIZE:
            break
        offset += SUPABASE_PAGE_SIZE
    return out


def load_tool_calls(
    chat_id: str,
    supabase: Client | None = None,
) -> list[ToolCall]:
    """Return deduped `ToolCall`s for a chat, ordered by (sequence, created_at).

    Pure aside from the Supabase read. Safe to call with no client — returns
    `[]` when Supabase isn't configured (matches `lake.py` graceful-degrade).
    """
    sb = supabase or _build_supabase_client()
    if sb is None:
        return []

    rows = _fetch_rows(sb, chat_id, "tool_call")
    rows.sort(
        key=lambda r: (r.get("sequence") or 0, r.get("created_at") or "")
    )

    # Pass 1: collect every (tool, args)-key that appears with a tool_call_id.
    # In production server.py logs each call from up to three paths: the
    # tool wrapper (no id available there) plus the sync/stream handlers
    # (id present). If any id-bearing row exists for a (tool, args), we
    # treat that as the canonical row and drop the wrapper duplicate.
    keys_with_id: set[tuple[str, str]] = set()
    for row in rows:
        md = _coerce_metadata(row.get("metadata"))
        tcid = md.get("tool_call_id") or md.get("id")
        if not tcid:
            continue
        tool = str(md.get("tool") or "").strip()
        if not tool:
            continue
        args = md.get("args") if isinstance(md.get("args"), dict) else {}
        keys_with_id.add((tool, _stable_args_key(args)))

    seen_ids: set[str] = set()
    seen_tuples: set[tuple[str, str]] = set()
    out: list[ToolCall] = []
    for row in rows:
        md = _coerce_metadata(row.get("metadata"))
        tool = str(md.get("tool") or "").strip()
        if not tool:
            continue
        args = md.get("args")
        if not isinstance(args, dict):
            args = {}
        tcid = md.get("tool_call_id") or md.get("id")
        key = (tool, _stable_args_key(args))
        if tcid:
            if tcid in seen_ids:
                continue
            seen_ids.add(tcid)
            seen_tuples.add(key)
        else:
            # If the same (tool, args) was already logged with a real
            # call id, skip this no-id duplicate (almost always the
            # tool_wrapper write).
            if key in keys_with_id:
                continue
            if key in seen_tuples:
                continue
            seen_tuples.add(key)
        out.append(
            ToolCall(
                sequence=int(row.get("sequence") or 0),
                created_at=row.get("created_at"),
                tool=tool,
                args=args,
                tool_call_id=tcid,
                source=md.get("source"),
            )
        )
    return out


@dataclass(frozen=True)
class ChatMeta:
    chat_id: str
    project_id: str | None


def load_chat_meta(
    chat_id: str,
    supabase: Client | None = None,
) -> ChatMeta:
    """Return `(chat_id, project_id)` for flow detection."""
    sb = supabase or _build_supabase_client()
    if sb is None:
        return ChatMeta(chat_id=chat_id, project_id=None)
    try:
        res = (
            sb.table("chats")
            .select("project_id")
            .eq("id", chat_id)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        if rows:
            return ChatMeta(
                chat_id=chat_id,
                project_id=rows[0].get("project_id"),
            )
    except Exception:
        pass
    return ChatMeta(chat_id=chat_id, project_id=None)
