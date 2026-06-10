---
name: Watchdog vs LLM self-recovery window
description: Why the stream watchdog must outlast the LLM SDK's own timeout×retries, not equal it.
---

# Watchdog timeout chain (server.py)

The agent streams with `stream_mode="values"`, which emits NO chunk *during* a
single LLM/tool superstep — only after a node finishes. So the gap the watchdog
measures = one whole heavy LLM call (large context, big output) PLUS the SDK's
internal retry budget.

**Rule:** `WATCHDOG_STALL_SECONDS` must be strictly greater than the LLM SDK's
self-recovery window = `ANTHROPIC_TIMEOUT_SECONDS × (ANTHROPIC_MAX_RETRIES + 1)`,
plus slack. Default now derives from that (`×(retries+1)+120` ⇒ 660s with stock
180s/2-retry settings) and stays under the pipeline `PHASE_TIMEOUT_SECONDS`
(1800s) so the phase cap remains the outer guard.

**Why:** When watchdog == per-call timeout (180/180), the watchdog killed runs
the moment a single slow call (or its first retry) went quiet — pre-empting the
SDK's own retry/recovery. Surfaced as `WATCHDOG: no agent chunk for 180s — run
stalled` on legitimate heavy runs that make many external API calls.

**How to apply:** If you change `LLM_REQUEST_TIMEOUT_S`/`ANTHROPIC_TIMEOUT_SECONDS`
or `ANTHROPIC_MAX_RETRIES`, the watchdog default re-derives automatically. There
are TWO watchdog sites (async auto-continue stream + websocket handler) — both
read the same `config.WATCHDOG_STALL_SECONDS`. The watchdog is a backstop for
true deadlocks / dropped streams (the SDK's own timeout handles network hangs);
it should not be sized to police normal-but-slow calls.
