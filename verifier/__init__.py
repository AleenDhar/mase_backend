"""Verifier — post-run coverage check for expected tool calls.

Triggered post-run by the orchestrator. Reads `chat_messages` from Supabase,
detects which flow a chat belonged to, evaluates the flow's expectations,
and writes a verdict back as `chat_messages.type='verifier_report'` (and
optionally to a dedicated `verifier_reports` table when present).

Behavior:
  - The verifier itself is read-only: it never modifies prior history,
    never blocks the agent loop, and swallows every error.
  - When the verdict is dirty, `verifier.remediation` re-prompts the
    agent ONCE with the list of missed checks (capped at 1 per chat,
    via an atomic `verifier_remediation` row claim — see
    `verifier/remediation.py`). This was added in the Task #24
    follow-up at user request; the original Task #24 was advisory-only.

See `.local/tasks/task-24.md` and `audit_spike/READOUT.md` for design
rationale.
"""

from .runner import run_verifier_for_chat, persist_verdict
from .checker import Verdict, evaluate_flow, ExpectedCall, ToolMatcher
from .flow_detection import detect_flow_for_chat
from .loader import load_tool_calls, ToolCall

__all__ = [
    "run_verifier_for_chat",
    "persist_verdict",
    "Verdict",
    "evaluate_flow",
    "ExpectedCall",
    "ToolMatcher",
    "detect_flow_for_chat",
    "load_tool_calls",
    "ToolCall",
]
