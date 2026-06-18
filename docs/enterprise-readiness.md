# Enterprise Readiness — scaling MASE to ~1000 concurrent users

> Source: multi-agent code audit (8 dimensions, 53 grounded findings), 2026-06-18.
> Status: **NOT ready for 1000 concurrent users yet.** This is the prioritized plan.
> Every item cites real code. Pick items off this list; update it as they land.

## TL;DR — two failure classes trip well below 1000 users

1. **Process-local state on a multi-instance fleet.** Critical invariants (one run per
   chat, `chat_messages.sequence`, run stop/cancel, scheduler singletons, idempotency
   caches) are enforced with in-process Python state that silently breaks the moment two
   ECS tasks (`mase-api-blue/green` behind the ALB) serve the same user — i.e. the normal
   steady state at scale. Result: duplicate runs, double token burn, double side-effecting
   pushes, corrupted realtime transcripts, duplicate nightly cron jobs.
2. **No cluster-wide LLM governor.** Zero rate limiter on the model clients; the only cap
   is per-process (`MAX_CONCURRENT_SESSIONS=50`). The fleet stampedes the Anthropic
   account limit — **OTPM 400k is the true ceiling (~48 heavy turns/min)**, hit well
   before ITPM 2M / RPM 20k — so when it trips, **every** user 429/529s at once and SDK
   retries amplify the herd.

Plus a launch-blocking **security** posture: auth fails **open** when no token is set,
one shared secret with no per-user identity, and multi-tenant Salesforce data
(`deal_records`) is granted `SELECT` to **anon** (the anon key ships in the browser).

---

## P0 — must fix before a 1000-user launch

### P0.1 — Cluster-wide LLM rate/concurrency governor (OTPM-first) · effort L
Zero `rate_limiter` on `CachedChatAnthropic` (server.py:1781) or `ChatOpenAI`
(server.py:8215); `MAX_CONCURRENT_SESSIONS=50` is per-process. **Fix:** shared
token-bucket admission gate (Redis or Postgres advisory lock) keyed on the Anthropic
account, budgeted against **OTPM** (queue near 400k); per-instance budget = account
budget / instance count; add `InMemoryRateLimiter` on the shared clients + an OpenAI-side
limiter; surface a "queued" state instead of hard 429s.

### P0.2 — Stop rebuilding the shared agent singleton per request · effort L
`reinitialize_agent` mutates one global `self.agent` on the hot chat path
(server.py:3000, 3144); under concurrency User A can execute under User B's
system_prompt/model — **cross-tenant prompt/model bleed** (confidentiality + correctness).
**Fix:** build a per-run agent (or cache by `(system_prompt_hash, model, headless)`);
reserve `reinitialize_agent` for admin-gated `/api/config` only.

### P0.3 — Durable, cross-instance run guard + sequence + stop · effort M
`_reserve_run_slot` checks only process-local sets (server.py:2001) → duplicate runs
across tasks. `chat_messages.sequence` from in-process counter (server.py:2462) with **no
`UNIQUE(chat_id,sequence)`** → dropped/mis-ordered realtime rows. `/api/chat/stop` only
cancels on the owning instance. **Fix:** atomic durable run claim (`chat_runs` row
`UNIQUE(chat_id) WHERE active` / advisory lock, released in `finally`); DB-side sequence
(`INSERT RETURNING` / identity) + `UNIQUE(chat_id,sequence)`; a `stop_requested` flag the
owner polls between steps.

### P0.4 — Fail CLOSED on auth + per-user identity + trim public allowlist · effort L
Auth gate is skipped when no token is set (server.py:10243, fail-open); one shared
`DISPATCH_SECRET` with no per-user claim; `/api/chat`, `/api/chat/async`, `/api/config`
are on the public allowlist (server.py:10163) → unauthenticated LLM spend + global prompt
overwrite; `MCP_ALLOW_UNAUTH` exists. **Fix:** refuse to start without a token; have the
Next.js proxy forward the Supabase JWT and verify it at the backend to derive user/tenant
(keep the shared secret only as proxy→backend auth); remove `/api/chat*` + `/api/config`
from the allowlist and admin-gate `/api/config`; strip `MCP_ALLOW_UNAUTH` from prod.

### P0.5 — RLS / tenant scoping on Salesforce-bearing tables · effort L
RLS disabled project-wide; `deal_records` (migrations 0005:53), analysis tables, etc.
grant `SELECT` to **anon**, and the anon key ships in the Next.js bundle → anyone can read
the **entire company pipeline** via the public REST API. No `tenant_id`/`owner` column
exists. **Fix:** add `tenant_id`/`owner` to `deal_records`, `analyses*`,
`chats`/`chat_messages`, `documents*`; `ENABLE ROW LEVEL SECURITY` with owner/tenant
policies; stop blanket anon `SELECT` on SF-data tables; route user reads through the
authenticated backend under a request-scoped JWT.

### P0.6 — Move nightly schedulers to a single owner (the worker) · effort M
`_nightly_sf_pull_scheduler` + `_nightly_hard_refresh_scheduler` start in
`@app.on_event(startup)` (server.py:8545) on **every** API task, de-duped only by a
process-local bool → N-fold midnight Salesforce re-reads / bulk refreshes (risks SF daily
API limits). **Fix:** run schedulers only in the single `mase-worker`, OR gate each run
behind a Postgres `cron_runs` row-claim / advisory lock (the `sweep_queue.claim_one`
pattern already proves this); or external EventBridge → one endpoint.

## P1 — degrades badly / operational pain

- **P1.1 Crash-safe runs + graceful drain (M):** 🟡 PARTIAL (2026-06-18) — graceful
  drain on SIGTERM + `stopTimeout:120` shipped (cancel stragglers → run writes its own
  terminal row → UI unblocks on deploy). Still TODO: a startup reconciler for hard-SIGKILL
  (OOM) orphans, since SIGKILL skips the drain. Original:
  runs live only in `_running_tasks`;
  terminal-row write is in a `finally` a SIGKILL skips; ECS has no `stopTimeout`
  (deploy.ps1) so blue/green flips kill in-flight runs → chats stuck on "Thinking…".
  Add `stopTimeout ~120s` + `minimumHealthyPercent`, a SIGTERM drain that writes an
  "interrupted" terminal row, and a startup reconciler for orphaned runs.
- **P1.2 ECS autoscaling (M):** `DesiredCount=2`, no autoscaling → 2×50 = 100 concurrent
  max → hard 503s. Add target-tracking autoscaling (CPU + active-sessions); load-test the
  real per-instance ceiling and set `MAX_CONCURRENT_SESSIONS` to match, coordinated with
  P0.1.
- **P1.3 AgentRun 2.5s full-table backfill (S):** `AgentRun.tsx:121` polls
  `SELECT(*)` every 2.5s on top of realtime → ~400 SELECT/s at 1000 panels. One-shot on
  mount + `clearInterval` on done; rely on the realtime channel (incremental cursor if a
  safety net is kept).
- **P1.4 Scope the opportunity-book fetch server-side (M):** `DashboardContext.tsx:110`
  fetches the **entire** book with no `?owner=` and filters in-browser (also duplicated in
  /runs + /sync-quality). Pass owner scope to the backend; short-TTL server cache; source
  /runs + /sync-quality from `DashboardContext.records`.
- **P1.5 Pooled httpx clients + bounded retries + sized thread pool (M):** 🟡 PARTIAL
  (2026-06-18) — shared pooled `httpx.Client` + idempotency-safe bounded retries shipped
  in `analysis_store.py` + `deal_engine_store.py`. Still TODO: an explicitly sized
  `ThreadPoolExecutor` (the run_in_executor default pool is still small). Original: store modules
  do per-call `httpx` (fresh TLS each hop) on the default thread pool (~6-8 threads) →
  pool exhaustion under load. One module-level pooled client per store module; jittered
  retries on idempotent verbs; explicit sized `ThreadPoolExecutor` or move to `AsyncClient`.
- **P1.6 Per-tool-call MCP timeout (S):** ✅ DONE (2026-06-18) — `_wrap_mcp_tool` wraps
  the async call in `asyncio.wait_for` (`MCP_TOOL_TIMEOUT_S` 300s API / 600s worker),
  returning `{status:failed}` on timeout. Original: `_wrap_mcp_tool` has no `asyncio.wait_for`
  (server.py:908) → one hung subprocess pins a session slot for the ~660s watchdog window.
  Wrap in `wait_for(~90s)` returning the existing `{error,status:failed}` shape.
- **P1.7 Durable idempotency for side-effecting tools + atomic SF push (M):** dedupe
  caches are per-process (server.py:632) → double lemlist/email pushes across instances;
  `deal_engine_todo_push` is check-then-write → retry double-creates the SF Task. Persist a
  per-`(chat_id,tool,args-hash)` idempotency key; insert the ledger row pending FIRST, then
  do the SF write, then finalize.
- **P1.8 Cross-process Avoma limiter + trim per-instance MCP fleets (L):**
  `avoma_mcp_server` `_api_lock` is per-process → effective Avoma concurrency × instance
  count; API tasks run all 17 MCP servers (worker narrows via allowlist). Shared Avoma
  token bucket; trim each API task's `MCP_SERVER_ALLOWLIST`; jitter the health-check.

## P2 — hardening

- **P2.1 Observability (L):** 286 `print()`s, no structured logging, no request/run
  correlation IDs, no Sentry/metrics/alarms (`opentelemetry`/`prometheus_client` are in
  requirements but unused). Add JSON logging + `X-Request-ID` contextvar + `/metrics` or
  Sentry + a readiness probe (pings Supabase + LLM) + CloudWatch alarms (ALB 5xx,
  `mase-worker` task count < 1, error rate).
- **P2.2 pgvector RAG + payload caps (M):** `match_threshold=-1.0` (no floor), fetches
  full 1536-dim embeddings + ranks cosine in Python (search_knowledge.py:288); uploads +
  `ChatRequest` have no size caps. Real threshold (~0.7), `ORDER BY embedding <=> q LIMIT
  k` in the RPC, never SELECT the embedding column; Pydantic size limits + ASGI body cap.
- **P2.3 Cache derived aggregations + immutable per-run cost rows (M):**
  `derive_todo`/`derive_matcha`/`list_deltas` re-pull the whole book JSONB per request
  (deal_engine_store.py:316); chat cost is a cumulative upsert per chat (lost on instance
  hop). TTL/invalidate-on-sweep cache or SQL view; append immutable per-run usage rows like
  `deal_trigger_runs`; add a `claude-opus-4-8` pricing key.

## Quick wins (high impact, low effort — do first)

1. **P1.3** — AgentRun backfill: one-shot on mount + `clearInterval` (cuts ~400 SELECT/s). (S)
2. **P1.4 (partial)** — source /runs + /sync-quality from `DashboardContext.records` (removes 2 full-book downloads/admin session). (S)
3. **P1.6** — wrap the MCP tool call in `asyncio.wait_for(~90s)`. (S)
4. **P0.4 (partial)** — remove `/api/chat*` + `/api/config` from the public allowlist and fail auth CLOSED when no token. (S)
5. **P0.3 (interim)** — add `UNIQUE(chat_id, sequence)` on `chat_messages` so collisions fail loudly. (S)
6. **P1.5 (partial)** — bounded jittered retry on the store helpers' idempotent verbs. (S)
