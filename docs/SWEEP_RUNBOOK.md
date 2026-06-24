# Deal Sweep at Scale — Runbook (9 workers, datalake + G8, "never fail")

> How to run the full deal sweep fast (many workers in parallel) **without** the failure
> modes we actually hit. Pair with [API_INVENTORY.md](API_INVENTORY.md) (QA) and
> [MASE_CONTEXT.md](MASE_CONTEXT.md). All AWS commands: `--region ap-south-1`, cluster
> `mase-cluster`, service `mase-worker`. AWS CLI on a Zscaler laptop needs
> `AWS_CA_BUNDLE` + `PYTHONUTF8=1` set first (see MASE_CONTEXT §3).

## What "9 workers" actually is
One `mase-worker` ECS task runs `DEAL_SWEEP_CONCURRENCY=8` sweeps in parallel. Scaling the
**service to 9 tasks** = up to ~72 concurrent sweeps draining one shared `sweep_queue`
(`claim_one` uses `FOR UPDATE SKIP LOCKED`, so no opp is ever double-processed). Each sweep
is a genuine **3–23 min** deep-agent run (median ~11 min); the speed is the *parallelism*,
not skipped work. `attempt=2/2` in logs = the agent's first JSON failed to parse and it
retried — **normal, not a failure** (it completes on attempt 2).

## Concurrency sizing — how many workers is RIGHT (measured, not guessed)
From 300 real sweeps: each costs ~**32K output tokens over ~12 min** = **~2,800 output
tokens/min per concurrent sweep** (output is the binding constraint; input is ~140K but
mostly the *cached* datalake prefix re-read each turn, which counts lighter). So:

```
safe_concurrency  ≈  0.7 × (Anthropic OTPM_limit ÷ 2,800)
workers           =  safe_concurrency ÷ 8        # DEAL_SWEEP_CONCURRENCY per task
```

**9 workers (72 concurrent) demanded ~200K output tok/min and hit Anthropic `500`s (~8%
fail)** — that was *marginally* over the account ceiling (so OTPM_limit ≈ 200K). **Default
to ~48 concurrent = 6 workers × 8-wide** (~135K OTPM, comfortably under). Get your exact
limit and re-size:

```bash
curl -s -D - -o /dev/null https://api.anthropic.com/v1/messages \
  -H "x-api-key: $ANTHROPIC_API_KEY" -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{"model":"claude-sonnet-4-6","max_tokens":1,"messages":[{"role":"user","content":"hi"}]}' \
  | grep -i ratelimit          # read anthropic-ratelimit-output-tokens-limit
```

Rule of thumb: OTPM 200K → **6 workers**; 400K → ~12 (but test — input/caching may bind
first). Never exceed ~70% of the limit; `ANTHROPIC_MAX_RETRIES=8` absorbs the rest. The
`500 InternalServerError` failures are **Anthropic's** API erroring under our burst, not a
MASE bug — they're fully recoverable by re-running at the right concurrency.

## Pre-flight (this is the "never fail" part — do ALL of it first)
1. **Anthropic credits topped up.** The #1 killer: `BadRequestError 400 "Your credit
   balance is too low"` silently fails every sweep. Check the Anthropic console BEFORE a
   big run — 380 sweeps burns real tokens.
2. **Env verified** — `curl .../api/deal-engine/selfcheck` must return `ok:true`. Confirms
   the durable tuning that prevents the other failures is present:
   - `DEAL_SWEEP_AVOMA_FROM_DATALAKE=true` (datalake source, fast + no 90-day clip)
   - `LLM_REQUEST_TIMEOUT_S=1200` (datalake prompts are big; 600s default → `APITimeoutError`)
   - `ANTHROPIC_MAX_RETRIES=8` + `DEAL_SWEEP_MAX_TRANSIENT_RETRIES=50` (absorb 429 rate-limits)
   - `DEAL_SWEEP_MAX_TOKENS=64000`, `DEAL_SWEEP_AVOMA_DL_TRANSCRIPT_BUDGET=80000`
     (80 KB budget → prompt small enough that one generation finishes < the LLM timeout)
3. **Smoke test green** — `./scripts/smoke_test.sh` exit 0.
4. **Do NOT deploy during the run.** `deploy.ps1` restarts the worker → kills in-flight
   sweeps (they reclaim to `waiting` and re-run, wasting work). Deploy before or after.

## Run
```bash
export AWS=...aws; R="--region ap-south-1"; C="--cluster mase-cluster"
BASE=http://mase-alb-1262623499.ap-south-1.elb.amazonaws.com
TOKEN=<DEAL_ENGINE_TOKEN>

# 1) scale OUT to 6 workers (~48 concurrent — the measured safe default; see "Concurrency sizing")
$AWS ecs update-service $C --service mase-worker --desired-count 6 $R
#    wait until runningCount=6:
$AWS ecs describe-services $C --services mase-worker $R --query "services[0].{r:runningCount,d:desiredCount}"

# 2) enqueue the opps (trigger). Body: {"opp_ids":[...]}.
#    GOTCHA: a big list (e.g. 380 ids) makes the POST run long; the CLIENT times out
#    (~90s) BUT the backend keeps enqueuing server-side. So either:
#      a) use a long client timeout (curl --max-time 600), OR
#      b) batch in chunks of ~50, OR
#      c) fire it and just verify via /sweep/status that waiting climbed.
curl -s --max-time 600 -X POST "$BASE/api/deal-engine/sweep/trigger" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"opp_ids\":[ ...18-or-15-char ids... ]}"

# 3) MONITOR (poll every ~60-90s)
curl -s "$BASE/api/deal-engine/sweep/status" -H "Authorization: Bearer $TOKEN" \
  | jq '{done,failed,working,waiting}'
#    watch `failed`: if it climbs with "credit balance" -> STOP, top up Anthropic, resume.
#    `working` ~70-77 with 9 tasks is healthy. waiting drains over ~1-1.5 hr for ~380 deals.

# 4) scale BACK to 1 when waiting+working = 0 (don't leave 9 idle = cost)
$AWS ecs update-service $C --service mase-worker --desired-count 1 $R
```

## Stop / pause / cancel
- **Pause (keep the queue):** `--desired-count 0`. In-flight sweeps die and reclaim to
  `waiting`; nothing new runs. Scale back up to resume.
- **Cancel (drop the queue):** delete `waiting` rows from the `sweep_queue` table
  (Supabase, service role) — destructive, do only on purpose.

## The failure modes we hit, and the fix (so you don't repeat them)
| Failure | Symptom | Fix |
|---------|---------|-----|
| Anthropic credit | 46 deals `400 "credit balance too low"` | top up credits before the run |
| `APITimeoutError` | sweeps die at ~600s | 80 KB transcript budget + `LLM_REQUEST_TIMEOUT_S=1200` (already env-set) |
| Rate limit (429) | transient failures under high concurrency | `ANTHROPIC_MAX_RETRIES=8` absorbs them (already env-set) |
| Big-trigger POST "times out" | client error, but it actually worked | backend enqueues server-side; verify `/sweep/status`, or batch/`--max-time 600` |
| Deploy mid-run | sweeps restart/duplicate | never deploy during a run |
| Idle cost | 9 workers left running | scale back to 1 when drained |

## Verify the sweeps are REAL (not no-ops)
- `/sweep/status` per-opp `duration_ms` should be **180s–1400s** (median ~650s). Anything
  `<90s` across the board = fast no-ops → investigate.
- Logs (`/ecs/mase-service`): `analyze_one START` → `avoma-engine window=alld read=N`
  (datalake) → `ainvoke returned … text_chars=30000–90000` (real agent output) → done.
- A swept record has a fresh `swept_at = today` + a rich `north_star_verdict` (headline,
  10+ evidence items, recommended moves). Old `swept_at` = not re-run yet.
