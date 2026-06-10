---
name: Chat/pipeline terminal-row contract
description: How chat turns and pipeline runs must persist their turn-ending row, and which error rows are NOT terminal.
---

# Terminal-row contract (chat_messages)

The chat UI renders assistant output only from Supabase `chat_messages` rows via
Realtime; for phased projects the Vercel `/api/chat` closes its SSE stream
immediately, so Realtime is the only source of truth during a turn.

## Rule
Every chat turn AND every pipeline run must persist EXACTLY ONE terminal row:
`type='final'` on success, `type='error'` on failure/cancel. The UI treats BOTH
`final` and `error` as the turn-ending marker; a `status` row does NOT stop the
spinner.

**Why:** writing both an `error` and a `final` (the old "belt-and-braces"
pattern) produces a duplicate terminal / stub row that can mask real content.
Reliability must come from the insert retry layer + a finally safety net, not
from writing a second terminal row.

**How to apply:**
- Funnel terminals through a single guarded writer (`_emit_terminal` +
  `terminal_written` flag) and a `finally` safety net.
- The pipeline safety net is gated on `req.task_id`: non-task runs have a
  concurrent live agent that owns the terminal, so writing one there creates a
  duplicate stub. Automation runs (task_id set) own their terminal.
- Inserts: await + bounded retry (~3x backoff) + response-shape check
  (`data is None`) + loud DROP log + bump `chats.updated_at`.

## Rows that are NOT terminal (do not retype to error)
- Per-phase `_run_phase` diagnostics use `kind='phase_error'` written as
  `type='error'` but are mid-run diagnostics, intentionally left as-is. The
  automations table surface polls `automation_tasks.status`
  (complete/failed/stopped), NOT chat terminal rows — see
  `docs/frontend_phase_outputs_contract.md` ("the error is in the chat stream +
  automation_tasks.status flips to failed").
- The disabled-phase advisory (`kind='pipeline_phases_skipped'`) is a
  driver-level warning, written as `type='status'` (NOT error) so it doesn't
  double as a terminal before the success `final`.
