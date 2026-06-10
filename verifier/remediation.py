"""Remediation: when the verifier finds gaps, re-prompt the agent.

Per the user's choice (Task #24 follow-up):
  - Re-prompt the agent (do NOT silently self-heal).
  - All tool categories are allowed; the agent decides what to fire.
  - Account context comes from the chat itself; the verifier does not
    fetch missing context from Salesforce on its own.

Loop safety: at most ONE remediation per chat. We persist a
`type='verifier_remediation'` row in `chat_messages` immediately before
the follow-up agent run; subsequent verifier passes see that row and
short-circuit.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from supabase import Client

from .checker import Verdict
from .loader import _coerce_metadata, next_sequence


REMEDIATION_TYPE = "verifier_remediation"
MAX_REMEDIATION_ATTEMPTS = 1


def build_remediation_prompt(verdict: Verdict) -> str:
    """Plain-language follow-up message listing the missed checks.

    Grouped by phase so the agent can slot the calls back into the right
    place in its workflow. Pure function — no I/O.
    """
    by_phase: dict[str, list] = {}
    for r in verdict.results:
        if r.status == "pass" or r.severity != "expected":
            continue
        by_phase.setdefault(r.phase or "Unphased", []).append(r)

    if not by_phase:
        return ""

    lines = [
        "🔍 **Coverage check — verifier follow-up**",
        "",
        (
            "Your previous turn finished, but the system prompt for this flow "
            f"({verdict.flow} v{verdict.flow_version}) requires some tool "
            "calls that did not fire. Please run the missing calls below "
            "now, then continue from where you left off. **Do not re-run "
            "calls that already succeeded** — only fill the gaps."
        ),
        "",
    ]
    for phase in sorted(by_phase.keys()):
        lines.append(f"**{phase}**")
        for r in by_phase[phase]:
            lines.append(f"- `{r.id}` — {r.description}")
        lines.append("")
    lines.append(
        "When you have addressed every missing call (or have a concrete "
        "reason it does not apply), produce the final answer. This is "
        "your one remediation turn — the verifier will not prompt again."
    )
    return "\n".join(lines)


def count_prior_remediations(chat_id: str, sb: Client) -> int:
    """Count `verifier_remediation` rows already written for this chat."""
    try:
        res = (
            sb.table("chat_messages")
            .select("sequence", count="exact")
            .eq("chat_id", chat_id)
            .eq("type", REMEDIATION_TYPE)
            .execute()
        )
        return int(res.count or 0)
    except Exception:
        return 0


def should_remediate(verdict: Verdict, sb: Client) -> bool:
    """Decide whether to fire a remediation turn for this verdict."""
    if verdict.passed:
        return False
    if not verdict.missed_ids:
        return False
    return count_prior_remediations(verdict.chat_id, sb) < MAX_REMEDIATION_ATTEMPTS


def mark_remediation(verdict: Verdict, prompt: str, sb: Client) -> bool:
    """Persist a `verifier_remediation` row and *atomically claim* the
    one-shot remediation slot for this chat.

    Race-safety: two verifier tasks can both observe `count_prior == 0`
    and both insert. To enforce strict cap-at-one we tag our row with a
    fresh `claim_id`, then read back the OLDEST `verifier_remediation`
    row for this chat. We only return True if our claim_id won that
    race; the loser returns False and the caller skips the re-run.

    Returns True on success+win, False on error or lost race.
    """
    claim_id = uuid.uuid4().hex
    try:
        sb.table("chat_messages").insert({
            "chat_id": verdict.chat_id,
            "role": "user",
            "type": REMEDIATION_TYPE,
            "content": prompt,
            "sequence": next_sequence(verdict.chat_id, sb),
            "metadata": json.dumps({
                "flow": verdict.flow,
                "flow_version": verdict.flow_version,
                "missed_ids": verdict.missed_ids,
                "claim_id": claim_id,
            }),
        }).execute()
    except Exception as e:
        print(f"[VERIFIER] mark_remediation insert failed (skip rerun): {e}")
        return False

    # Read back the oldest claim — only the winner re-prompts.
    try:
        res = (
            sb.table("chat_messages")
            .select("metadata,created_at")
            .eq("chat_id", verdict.chat_id)
            .eq("type", REMEDIATION_TYPE)
            .order("created_at")
            .limit(1)
            .execute()
        )
        rows = res.data or []
        if not rows:
            return False
        meta = _coerce_metadata(rows[0].get("metadata"))
        winner = meta.get("claim_id") if isinstance(meta, dict) else None
        if winner != claim_id:
            print(
                f"[VERIFIER] lost remediation race for chat={verdict.chat_id} "
                f"(winner={winner}); skipping rerun"
            )
            return False
        return True
    except Exception as e:
        print(f"[VERIFIER] mark_remediation claim-check failed (skip rerun): {e}")
        return False


def load_messages_for_rerun(
    chat_id: str,
    sb: Client,
    *,
    max_history: int = 20,
) -> list[dict[str, Any]]:
    """Rebuild the LangChain-shaped message list for the follow-up run.

    Pulls user `message` rows and assistant `final` rows (the actual
    conversation turns), in sequence order, capped at `max_history`.
    Returns a list of `{"role": "user"|"assistant", "content": str}` dicts
    that `_build_message_content` in server.py can normalise.
    """
    try:
        # Fetch the LATEST `max_history * 2` rows (descending) so long
        # chats keep recent context, then reverse for chronological order.
        res = (
            sb.table("chat_messages")
            .select("sequence,role,type,content,created_at")
            .eq("chat_id", chat_id)
            .in_("type", ["message", "final"])
            .order("sequence", desc=True)
            .order("created_at", desc=True)
            .limit(max_history * 2)
            .execute()
        )
        rows = list(reversed(res.data or []))
    except Exception as e:
        print(f"[VERIFIER] load_messages_for_rerun failed: {e}")
        return []

    out: list[dict[str, Any]] = []
    for row in rows:
        content = (row.get("content") or "").strip()
        if not content:
            continue
        role = row.get("role")
        if row.get("type") == "message" and role == "user":
            out.append({"role": "user", "content": content})
        elif row.get("type") == "final" and role == "assistant":
            out.append({"role": "assistant", "content": content})
    if len(out) > max_history:
        out = out[-max_history:]
    return out
