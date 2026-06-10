---
name: Avoma MCP timeouts under concurrent load
description: Why Avoma tool calls time out during sweeps/webhooks and how the timeout+retry posture protects them
---

Avoma MCP tool calls (e.g. get_all_meetings_for_opportunity, list_meetings,
get_meetings_summary_for_opportunity) intermittently fail with "read operation
timed out" — but ONLY under concurrent load, not because Avoma is slow.

**Evidence:** In isolation, the same opp's meeting fetch returns in ~4s (12
meetings). During a deal-engine sweep that overlapped the always-on Avoma SNS
webhook pipeline (each OPP-ANALYZER run ~3.5min hammering the same single Avoma
stdio subprocess), those calls exceeded the old 30s per-request httpx cap and
failed. The sweep agent then retried/ground until the 900s sweep timeout and
wrote NO record (the timeout path does not upsert) → opp shows as "not found".

**Why one opp "works" and another doesn't:** an opp with 0 Avoma meetings never
touches Avoma, so it sweeps instantly regardless of load. An opp with real
meetings makes many Avoma calls and is exposed to the contention.

**Operational knobs (in avoma_mcp_server.py `_send_with_retry`):**
`AVOMA_HTTP_TIMEOUT` (per-request httpx timeout) and `AVOMA_MAX_TIMEOUT_RETRIES`
(backoff retries on timeout/transport errors, distinct from the 429 retry
budget). Backoff sleeps happen outside `_api_lock`.

**Why:** the live webhook pipeline and any sweep share ONE Avoma subprocess
serialized by `_api_lock`; too short a per-request cap turns transient
under-load slowness into hard failures and silent missing records. Tradeoff:
raising the cap means a single slow call holds `_api_lock` longer, adding queue
latency for other Avoma callers — reliability over throughput.

**How to apply:** if Avoma-backed runs (sweeps, analyzers) fail with "read
operation timed out", suspect concurrency contention first (check for
overlapping OPP-ANALYZER/webhook runs) before assuming Avoma is down; tune the
two env knobs rather than lowering the per-request timeout.
