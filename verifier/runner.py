"""High-level orchestration. Called fire-and-forget post-run from server.py.

Strictly advisory: every error is logged and swallowed. Verifier failures
must NEVER take down an agent run or block any UI.
"""

from __future__ import annotations

import asyncio
import json
import os
import traceback
from typing import Any

from supabase import Client, create_client

from .checker import (
    Verdict,
    evaluate_flow,
    render_verdict,
    verdict_to_dict,
)
from .flow_detection import detect_flow_for_chat
from .loader import ToolCall, load_chat_meta, load_tool_calls


def _build_supabase_client() -> Client | None:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        return None
    try:
        return create_client(url, key)
    except Exception:
        return None


def run_verifier_for_chat_sync(
    chat_id: str,
    *,
    project_id: str | None = None,
    supabase: Client | None = None,
    extra_account_tokens: list[str] | None = None,
) -> Verdict | None:
    """Synchronous core. Returns None when the chat isn't in scope or on error."""
    sb = supabase or _build_supabase_client()
    if sb is None:
        print("[VERIFIER] Supabase not configured — skipping")
        return None

    pid = project_id
    if pid is None:
        pid = load_chat_meta(chat_id, sb).project_id

    flow = detect_flow_for_chat(pid)
    if flow is None:
        print(f"[VERIFIER] chat={chat_id} project_id={pid} — no flow registered, skipping")
        return None

    calls: list[ToolCall] = load_tool_calls(chat_id, sb)
    if not calls:
        print(f"[VERIFIER] chat={chat_id} — no tool_call rows yet, skipping")
        return None

    context = {"extra_account_tokens": extra_account_tokens or []}
    verdict = evaluate_flow(
        chat_id=chat_id,
        project_id=pid,
        calls=calls,
        spec=flow,
        context=context,
    )
    print(f"[VERIFIER] {verdict.summary_line()}")
    return verdict


def persist_verdict(
    verdict: Verdict,
    *,
    supabase: Client | None = None,
    save_chat_message: bool = True,
    save_report_table: bool = True,
) -> dict:
    """Write verdict to chat_messages and (best-effort) verifier_reports.

    Returns `{chat_messages: bool, verifier_reports: bool}`.
    """
    sb = supabase or _build_supabase_client()
    if sb is None:
        return {"chat_messages": False, "verifier_reports": False}

    out = {"chat_messages": False, "verifier_reports": False}
    payload = verdict_to_dict(verdict)
    summary = verdict.summary_line()

    if save_chat_message:
        try:
            from .loader import next_sequence
            row = {
                "chat_id": verdict.chat_id,
                "role": "assistant",
                "type": "verifier_report",
                "content": summary,
                "sequence": next_sequence(verdict.chat_id, sb),
                "metadata": json.dumps({
                    **payload,
                    "detail": render_verdict(verdict),
                }),
            }
            sb.table("chat_messages").insert(row).execute()
            out["chat_messages"] = True
        except Exception as e:
            print(f"[VERIFIER] chat_messages insert failed (non-fatal): {e}")

    if save_report_table:
        try:
            sb.table("verifier_reports").insert({
                "chat_id": verdict.chat_id,
                "project_id": verdict.project_id,
                "flow": verdict.flow,
                "flow_version": verdict.flow_version,
                "passed": verdict.passed,
                "missed_ids": verdict.missed_ids,
                "total_tool_calls": verdict.total_tool_calls,
                "report": payload,
                "summary": summary,
            }).execute()
            out["verifier_reports"] = True
        except Exception as e:
            # Expected to fail until the migration in `sql/verifier_reports.sql`
            # is applied; falls back silently to chat_messages-only persistence.
            print(f"[VERIFIER] verifier_reports insert skipped (table may not exist): {e}")

    return out


async def run_verifier_for_chat(
    chat_id: str,
    *,
    project_id: str | None = None,
    extra_account_tokens: list[str] | None = None,
) -> Verdict | None:
    """Async wrapper for fire-and-forget use from FastAPI handlers.

    Wraps the sync core in a thread to avoid blocking the event loop on
    Supabase reads/writes. Catches everything — never raises.
    """
    try:
        loop = asyncio.get_event_loop()
        verdict = await loop.run_in_executor(
            None,
            lambda: run_verifier_for_chat_sync(
                chat_id,
                project_id=project_id,
                extra_account_tokens=extra_account_tokens,
            ),
        )
        if verdict is None:
            return None
        await loop.run_in_executor(None, lambda: persist_verdict(verdict))
        return verdict
    except Exception as e:
        print(f"[VERIFIER] run failed (non-fatal): {e}")
        print(traceback.format_exc())
        return None


def latest_report(
    chat_id: str,
    supabase: Client | None = None,
) -> dict | None:
    """Read the most recent verifier_report for `chat_id` from chat_messages.

    Used by the GET endpoint. Falls back to chat_messages so it works even
    when the dedicated `verifier_reports` table isn't installed yet.
    """
    sb = supabase or _build_supabase_client()
    if sb is None:
        return None
    try:
        # Prefer the dedicated table when available
        try:
            res = (
                sb.table("verifier_reports")
                .select("report, summary, passed, created_at")
                .eq("chat_id", chat_id)
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            rows = res.data or []
            if rows:
                return {
                    "chat_id": chat_id,
                    "summary": rows[0].get("summary"),
                    "passed": rows[0].get("passed"),
                    "report": rows[0].get("report"),
                    "created_at": rows[0].get("created_at"),
                    "source": "verifier_reports",
                }
        except Exception:
            pass

        res = (
            sb.table("chat_messages")
            .select("content, metadata, created_at")
            .eq("chat_id", chat_id)
            .eq("type", "verifier_report")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        if not rows:
            return None
        md = rows[0].get("metadata")
        if isinstance(md, str):
            try:
                md = json.loads(md)
            except Exception:
                md = {}
        return {
            "chat_id": chat_id,
            "summary": (md or {}).get("summary") or rows[0].get("content", "").split("\n", 1)[0],
            "passed": (md or {}).get("passed"),
            "report": md,
            "created_at": rows[0].get("created_at"),
            "source": "chat_messages",
        }
    except Exception as e:
        print(f"[VERIFIER] latest_report failed: {e}")
        return None
