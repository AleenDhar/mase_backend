---
name: Final chat row is the clean answer only
description: The terminal `final` chat_messages row must contain just the agent's answer, never the thinking/tool-call trace.
---

# Final row = clean answer only

The terminal `final` row written for a chat turn (in `run_agent_and_save`, both
the non-stream and stream paths) must be the agent's answer text **alone**.

**Why:** the run loop already persists every intermediate step as its OWN
`chat_messages` row — `thinking`, `tool_call`, `tool_result` (and `status`). The
frontend renders those as a collapsible activity/steps view. A previous version
also concatenated all of that into the `final` row as
`### Thinking Process … Calling **tool** … Result from **tool** … ### Answer …`,
which made the chat answer look like a wall of gibberish and duplicated the steps
that were already shown as activity. Users explicitly do not want the trace inside
the answer bubble.

**How to apply:** when building the `final` content, set it to `final_response`
only (plus the budget-breaker `⚠️` note when a circuit-breaker fired). Do NOT
inline `thinking_logs`. Keep emitting the per-step `thinking`/`tool_call`/
`tool_result` rows so the activity view still works. `thinking_logs` may still be
accumulated for other purposes but must not be folded into the answer.
