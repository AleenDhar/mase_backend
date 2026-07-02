# KT — Sweep runs, triggers & workers (why they run, how to stop/start)

Everything about the `deal_trigger_runs` you see in the UI: what each `source` means, where it
comes from, why the money burned, and the exact levers + commands to check and control it.

> **Times:** `deal_trigger_runs.created_at` is **UTC**. The UI shows **IST (+5:30)**.
> e.g. 06:05 UTC = 11:35 AM IST.

---

## 0. Current state (what is ON / OFF right now)

> **Updated 2026-07-02** — SFDC re-enabled **with brakes**. See CHANGELOG entries
> "CDC brakes: meaningful-field filter + per-opp sweep cooldown" and "Close the
> `/cron/nightly-sf-pull` side-door".

| Thing | State | Meaning |
|---|---|---|
| CDC trigger (real-time SFDC → sweep) | **ON** — rule `mase-sf-cdc-to-lambda` **ENABLED** | `salesforce_trigger` runs fire again — but only on *meaningful* Opportunity changes (next row). |
| CDC meaningful-field filter (Lambda) | **ON** | `mase-sf-cdc-bridge` triggers only when `StageName/Amount/CloseDate/NextStep` change (env `MEANINGFUL_FIELDS`). Task/Event/EmailMessage activity no longer triggers (env `CDC_TRIGGER_ON_ACTIVITY=false`). |
| Per-opp sweep cooldown (backend) | **ON** — 6h | a Salesforce-triggered re-sweep is skipped if the opp was swept within `DEAL_SWEEP_TRIGGER_COOLDOWN_HOURS` (default 6). Manual + from-scratch bypass it. |
| Worker fleet (`mase-worker`) | **ON** — desiredCount **1** | drains the queue (autoscaler may resize). |
| Nightly `/cron/nightly-sf-pull` | **CLOSED** — gated on `SF_PULL_CRON_ENABLED` (off) | side-door no longer runs `scheduled_discovery`/`scheduled_reconcile`, even though an external cron still calls it. `SF_PULL_CRON_ENABLED=true` re-opens it. |

So right now: **real-time SFDC is back, braked.** The only automated AI-sweep trigger is
Salesforce CDC on meaningful field changes; the nightly path is dead. To hard-stop again, see §4
(disable the rule + worker desiredCount 0).

---

## 1. The run sources (`deal_trigger_runs.source`)

| source | What it is | Origin |
|---|---|---|
| `salesforce_trigger` | A real Salesforce change fired a sweep for one opp | SFDC CDC → EventBridge partner bus → Lambda `mase-sf-cdc-bridge` → `POST /api/deal-engine/sweep/trigger` → enqueued as `sftrig-*` run_id |
| `worker` | A queue row drained that had no special run_id prefix (book / discovery drain) | `mase-worker` draining `sweep_queue`; `worker.py` labels by run_id prefix |
| `scheduled_discovery` | Nightly "find new/changed opps and sweep them" | `discover_and_sweep_new` inside `[NIGHTLY-SF-PULL]` (`server.py`), invoked via `GET /cron/nightly-sf-pull` |
| `scheduled_reconcile` | Nightly book membership reconcile | `reconcile_membership` in the same `[NIGHTLY-SF-PULL]` job |
| `manual` | User clicked a sweep in the UI | `trigger-*` run_id |
| `update_living_memory` | From-scratch rebuild (purge poisoned memory) | `fromscratch-*` run_id |

**Label mapping** (`worker.py`, current main): `fromscratch-` → `update_living_memory`,
`sftrig-` → `salesforce_trigger`, `trigger-` → `manual`, else → `worker`.
> Historical caveat: BEFORE that fix (commit `7aebccd`), the worker stamped **everything**
> `worker`, so old CDC-triggered runs show as `worker` in the history — that's why "worker"
> dominated and the SFDC trigger looked invisible.

---

## 2. Why the money burned (the investigation, 2026-07-02)

- **48h window: `worker` = 829 runs / ~$1,157** — the dominant cost. `scheduled_discovery` 51/$47,
  `salesforce_trigger` 10/$9, `manual` 3/$3.
- **99% of "worker" were actually the CDC trigger** — join `sweep_queue.run_id`: 763 `trigger-` +
  53 `sftrig-` = 816/827 came from the single-opp Salesforce path, relabeled `worker`.
- **The CDC Lambda fires on activity, not just field changes** — every `Task` / `Event` /
  `EmailMessage` on a tracked opp triggers a full paid AI re-sweep (`infra/sf-cdc-bridge/lambda_function.py`).
- **No cooldown/debounce anywhere** — `sweep_queue` upsert only blocks a *concurrent* sweep;
  the next activity re-arms it. Result: **829 sweeps over 198 distinct opps, 76% repeat sweeps =
  ~$894 (77% of spend)**. One deal swept 17× in 48h. Curve peaks at business hours (reps editing).
- **`SF_PULL_CRON_ENABLED=false` has a side-door** — the same nightly job is exposed at
  `GET /cron/nightly-sf-pull` (`server.py`), which **ignores the flag**; an external cron still
  calls it → the `scheduled_discovery` / `scheduled_reconcile` rows.
- **`DEAL_HARD_REFRESH_CRON_ENABLED=false` backfired** — that was the *cheap, no-AI* fact refresh;
  turning it off means stage/amount/dates only refresh via expensive AI sweeps.

---

## 3. Run the analysis yourself — copy-paste

**Supabase (prod) query recipe** — Management API, token in `.supabase_secrets.env`
(`SUPABASE_ACCESS_TOKEN`), prod ref `wfwgatyfzqzrcauatufb`. Python snippet uses a browser
User-Agent (Cloudflare blocks urllib) + the corp CA bundle. SQL to run:

```sql
-- Cost + count by source, last 48h
select source, count(*) n, sum(cost_usd::numeric) cost, min(created_at) first, max(created_at) last
from deal_trigger_runs where created_at > now() - interval '48 hours'
group by source order by n desc;

-- Hourly shape (nightly burst vs all-day storm)
select source, date_trunc('hour', created_at) hr, count(*)
from deal_trigger_runs where created_at > now() - interval '48 hours'
group by 1,2 order by 2;

-- Repeat-sweep waste: runs vs distinct opps
select count(*) runs, count(distinct opp_id) opps
from deal_trigger_runs where created_at > now() - interval '48 hours';

-- Worst repeat offenders
select opp_name, count(*) n from deal_trigger_runs
where created_at > now() - interval '48 hours'
group by opp_name having count(*) > 3 order by n desc;

-- Trace the TRUE enqueuer behind "worker" (join the queue)
select left(run_id,9) prefix, count(*) from sweep_queue group by 1 order by 2 desc;
```

**AWS (region `ap-south-1`, cluster `mase-cluster`, acct 022187637784)** — set
`AWS_CA_BUNDLE` to the corp bundle if TLS fails:

```bash
BUS='aws.partner/salesforce.com/00D2000000016T9EAI/0YLP7000000arPBOAY'
# CDC rule state
aws events describe-rule --name mase-sf-cdc-to-lambda --event-bus-name "$BUS" --query State
# worker fleet
aws ecs describe-services --cluster mase-cluster --services mase-worker \
  --query "services[0].{desired:desiredCount,running:runningCount}"
```

---

## 4. The levers (stop / start)

**Hard stop (what's applied now):**
```bash
# 1. kill the trigger
aws events disable-rule --name mase-sf-cdc-to-lambda --event-bus-name "$BUS"
# 2. kill all processing
aws ecs update-service --cluster mase-cluster --service mase-worker --desired-count 0
```

**Bring it back (normal operation):**
```bash
aws ecs update-service --cluster mase-cluster --service mase-worker --desired-count 1
aws events enable-rule  --name mase-sf-cdc-to-lambda --event-bus-name "$BUS"
```

**Env levers (task-def / `render_taskdef.py` → redeploy):**
`SWEEP_AUTOSCALE_MAX` (6 → 1–2) · `DEAL_SWEEP_CONCURRENCY` (8 → 2) · `SWEEP_AUTOSCALE_ENABLED` ·
`SF_PULL_CRON_LIMIT` (500 → 50) · `SF_PULL_CRON_DISCOVERY_ENABLED` / `_DELTA_ENABLED` /
`DEAL_ENGINE_DISCOVERY_ENABLED` (all default TRUE) · `DEAL_ENGINE_AI_SCORING`.

**Close the nightly side-door:** stop the external cron hitting `GET /cron/nightly-sf-pull`, or
gate that endpoint behind `SF_PULL_CRON_ENABLED`.

---

## 5. The real fix (keep the SFDC trigger, kill the waste)

The trigger and the burn are the same thing with no brakes. To have real-time SFDC updates
**without** the storm (code changes → deploy):
1. **Per-opp cooldown/debounce** in `enqueue_trigger` (`deal_engine_sweep.py`): skip re-enqueue if
   the opp was swept within N hours. Fixes the 76% repeat waste. (Note: user chose **no cooldown**
   at last decision — revisit.)
2. **CDC only on meaningful field changes** (stage/amount/close/next-step), not every Task/Event/
   EmailMessage (`lambda_function.py`).
3. Already shipped: **pin guard** (a corrected deal survives re-sweeps) + **true source label**.

See also: `.agents/memory/` and the auto-memory `sweep-worker-flood.md`.
