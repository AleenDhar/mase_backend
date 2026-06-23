# MASE Backend — API Inventory & Deploy QA

> **Every deploy MUST pass the smoke test, before and after.** This is how we stop a
> build from silently dropping a route (the chat-404 outages) or an env var (datalake /
> SNS / LLM tuning). Read this with [MASE_CONTEXT.md](MASE_CONTEXT.md) + [DEPLOY_SAFETY.md](DEPLOY_SAFETY.md).

---

## The deploy QA loop (do this every time)

```bash
export BASE_URL=http://mase-alb-1262623499.ap-south-1.elb.amazonaws.com
export TOKEN=<DEAL_ENGINE_TOKEN>        # from the MASE frontend .env.local

# 1) PRE-DEPLOY baseline — current prod must be green first
./scripts/smoke_test.sh            #  -> "All critical routes live. Safe."  (exit 0)

# 2) deploy
./deploy.ps1 -Message "..."        # (PowerShell host) — clean tree, synced to origin/main

# 3) POST-DEPLOY verify — same script; everything that passed before must still pass
./scripts/smoke_test.sh            #  -> exit 0 = good; exit 1 = a route 404'd/5xx'd -> ROLL BACK

# 4) ENV verify (proves the deploy didn't drop datalake/SNS/LLM env)
curl -s "$BASE_URL/api/deal-engine/selfcheck" -H "Authorization: Bearer $TOKEN" | jq
#    -> {"ok": true, "missing": [], "checks": {...}}    ok:false => an env vanished, re-add + redeploy
```

**Rules**
- **Pre AND post.** Pre proves the baseline was healthy (so a post-deploy failure is *yours*); post proves you didn't break it.
- **`smoke_test.sh` exit 1 → roll back** (flip the ALB listener to the previous colour — see DEPLOY_SAFETY.md). Do not leave a build live with a 404/5xx route.
- **`selfcheck.ok must be true.** If `missing[]` is non-empty, the deploy dropped a durable env — it belongs in the `deploy.ps1` task-def template or a Secrets Manager secret, never a one-off task-def revision (see §"Env durability").
- Probes are **safe**: GETs, and POSTs with `{}` that the handler rejects `400/422`. They prove a route exists **without** running a sweep, a chat, or any write. PASS = not `404`, not `5xx`.

---

## What `/api/deal-engine/selfcheck` guards (the env-damage fix)

It returns booleans (never secret values). `ok:false` if any **required** key is missing:

| check | env it verifies | why it matters |
|-------|-----------------|----------------|
| `anthropic_api_key` | `ANTHROPIC_API_KEY` | no key → every sweep/chat 400s ("credit/billing") |
| `supabase` | `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` | deal records / queue / prompts |
| `avoma_api_token` | `AVOMA_API_TOKEN` | live-Avoma fallback + webhook sync |
| `datalake_url` / `datalake_service_key` | `DATALAKE_URL`, `DATALAKE_SERVICE_KEY` | the repointed Avoma source |
| `avoma_from_datalake` | `DEAL_SWEEP_AVOMA_FROM_DATALAKE=true` | the prod repoint flag |
| `sns_allowlist` | `SNS_ALLOWED_TOPIC_ARNS` | Avoma→SNS→datalake webhook |
| `llm_request_timeout_s`, `anthropic_max_retries` | sweep robustness tuning | shown so a wrong value is visible |

**Env durability (prevention, not just detection):** all of the above are baked into the
`deploy.ps1` task-def template + the `mase/app-env` / `mase/datalake` secrets, so a normal
`deploy.ps1` **keeps them automatically**. `selfcheck` is the seatbelt that catches it if
someone edits the template or a secret and drops one.

---

## Full endpoint inventory

✅ = covered by `smoke_test.sh`.  ⚠️ = destructive/expensive/parametrised — NOT auto-probed (inventory only).

### Core / health
- ✅ `GET /api/health` — process + agent + MCP readiness
- ✅ `GET /api/deal-engine` / `GET /api/deal-engine/health` — descriptor + health
- ✅ `GET /api/deal-engine/selfcheck` — env self-check (this doc)
- ✅ `GET /api/tools` · `GET /api/mcp/servers` · `GET /api/mcp/status`
- `GET /api/metrics` · `GET /api/config` · `POST /api/config`

### RevOps chat (the recurring 404 — always smoke-tested)
- ✅ `POST /api/deal-engine/chat` · ✅ `POST /api/deal-engine/chat/async` · `POST /api/deal-engine/chat/stop`
- ✅ `GET /api/deal-engine/chat/prompt` · `POST /api/deal-engine/chat/prompt` (admin)
- (generic agent chat: `POST /api/chat`, `/api/chat/async`, `/api/chat/stop`, `/api/chat/structured[/async]`, `GET /api/chat/active`, `/api/chat/{id}/verifier_report`, `/api/chat/result/{id}`)

### Deal book / opportunities
- ✅ `GET /api/deal-engine/opportunities[?owner=&slim=1]` · `GET /api/deal-engine/opportunities/{opp_id}`
- ✅ `GET /api/deal-engine/deals-count` · ✅ `GET /api/deal-engine/team`
- ✅ `GET /api/deal-engine/matcha` · ✅ `GET /api/deal-engine/deltas` · `GET /api/deal-engine/deltas/{opp_id}`
- ⚠️ `POST /api/deal-engine/records` · `POST /api/deal-engine/backfill-packets`

### Sweep / engine
- ✅ `GET /api/deal-engine/sweep/status` · ✅ `GET /api/deal-engine/sweep/prompt` · `POST /api/deal-engine/sweep/prompt` (admin)
- ✅ `POST /api/deal-engine/sweep/trigger` (enqueue) · ⚠️ `POST /api/deal-engine/sweep` (book sweep)
- ⚠️ `POST /api/deal-engine/sweep/{opp_id}` (re-run one) · ⚠️ `POST /api/deal-engine/sweep/{opp_id}/update-living-memory`
- `POST /api/deal-engine/sweep/{opp_id}/datalake-test` (A/B, no-persist) · `GET /api/deal-engine/sweep/discover` · `POST /api/deal-engine/sweep/discover-new` · `POST /api/deal-engine/sweep/reconcile`
- ✅ `GET /api/deal-engine/hard-refresh/status` · `POST /api/deal-engine/hard-refresh` · `GET .../history`
- ✅ `GET /api/deal-engine/trigger-logs` · `GET /api/deal-engine/trigger-logs/{opp_id}`
- `GET /api/deal-engine/sweep/dashboard` · `GET /api/deal-engine/todo/dashboard` (HTML)

### To-dos / updates (the "Add update" path — Marc's flow)
- ✅ `GET /api/deal-engine/todo`
- ✅ `POST /api/deal-engine/todo/update` (next_step / todo / completed — writes SF as the rep)
- ✅ `POST /api/deal-engine/todo/push` · `POST /api/deal-engine/todo/override[/clear]`
- `GET/POST /api/deal-engine/todo-runner/prompt` (admin) · `GET /api/deal-engine/todo-runner/runs` (admin)

### Learnings / knowledge (admin)
- ✅ `GET /api/deal-engine/learnings` · `GET .../learnings/signals` · `POST .../learnings` · `POST .../learnings/{id}`
- ✅ `GET /api/deal-engine/knowledge` · `POST .../knowledge[/presign]` · `GET/DELETE .../knowledge/{id}`

### Avoma + webhook
- ✅ `POST /webhook` — SNS/Avoma receiver → `datalake_sync` (tracked-opp gated)
- `GET /api/avoma/reports[/{id}]` · `POST /api/avoma/reports/{id}/reanalyze` · `POST .../refresh-sf`

### Analysis workspace (Jarvis) — `/api/analysis/*`
- `GET/POST /api/analysis`, `/{id}` (CRUD), `/{id}/columns`, `/{id}/rows`, `/{id}/run|stop|resume|query`, `/{id}/dashboards`, cells … (full CRUD set; parametrised — inventory only)
- `GET /api/jarvis/settings` · `PUT /api/jarvis/settings` · `POST /api/jarvis/chat/async`

### Documents / cache / cron / misc
- `POST /api/documents/upload` · `GET /api/documents`
- `POST /api/cache/bulk_import[_field_history]` · `GET /api/cache/tenant/{opp_id}`
- ⚠️ `GET /cron/sync-sf-to-cache` · `/cron/sf-pull-refresh` · `/cron/nightly-sf-pull` (run jobs — never auto-probe)
- `POST /api/ceo-query` · `POST /api/ask` · `GET /ask` · LinkedIn/Sheets OAuth callbacks
- `POST /api/admin/backfill-lake` · `GET /api/lake/diagnoses/{account_id}` · `GET /api/usage/{chat_id}`

---

## If the smoke test fails

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `chat/async -> 404` | shipped an image missing the chat routes (un-pushed/divergent tree) | redeploy `origin/main`; never deploy a dirty/divergent tree |
| `selfcheck.ok=false`, datalake missing | env dropped (one-off task-def revision, or template/secret edited) | re-add to `deploy.ps1` template / secret, redeploy |
| many routes `5xx` | app crashed at boot (bad import/env) | check `/ecs/mase-service` logs; roll back the ALB colour |
| `todo/update -> 5xx` for one rep | rep's Salesforce token stale (writes as the rep) | rep reconnects Salesforce |
