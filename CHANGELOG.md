# CHANGELOG — MASE backend (`mase_backend`)

> **Agents & teammates: read this file after every `git pull`.** It is the running
> log of behaviour-changing decisions and conventions. Newest first. When you make a
> change that affects how the system behaves, where data lives, or how another agent
> should work, **add an entry here** (and, for a durable rule, a note under
> `.agents/memory/` with a line in `.agents/memory/MEMORY.md`).

Conventions for an entry: `## YYYY-MM-DD — <short title>`, then **What / Why /
How to work with it going forward**. Keep it tight; link code paths and docs.

---

## 2026-07-15 — Slim-book cache: kill the ~7.5s dashboard load (stale-while-revalidate)

**What.** `GET /api/deal-engine/opportunities?slim=1` (no owner) — the payload EVERY rep's
dashboard loads on open — is now served from an in-process **stale-while-revalidate** cache of
pre-serialized JSON bytes. Within `_SLIM_TTL` (30s) a request returns the cached bytes instantly
(no DB read, no slim compute, no re-serialize); when stale (but under a 10-min hard cap) it
returns the stale bytes immediately AND fires ONE background rebuild; only a cold request (or
past the hard cap) builds inline, serialized behind an `asyncio.Lock` so a burst can't stampede
the DB. Owner-scoped / search (`q`) / `paged` variants bypass the cache and read live.

**Why.** The slim endpoint pulls every record's full `record` jsonb (~20 MB) then slims it
~10-25×, plus per-record `attach_deal_scores` / `attach_verdict_view` — ~7.5s of server work,
paid on every load. Biggest single source of the "initial load takes forever" sluggishness. The
book only changes on sweeps/refreshes (not per-request), so ≤30s staleness is harmless (the
frontend also re-fetches on tab focus).

**How to work with it.** The build path (`_build_slim_book_body`) reuses the SAME stub-filter +
`slim_record(attach_pulse(r))` as the inline path, so output is byte-identical — just cached.
Staleness is time-based (cross-process safe: the worker writes to Supabase, the web cache expires
on its own); after a write the list catches up within 30s. Bump `_SLIM_TTL` if you need fresher.

## 2026-07-14 — SFDC CDC trigger now does UPDATE-LIVING-MEMORY (from-scratch)

**What.** A live Salesforce CDC trigger (`source="salesforce_trigger"`, set by the worker for a
`sftrig-*` run_id) now runs a **from-scratch** rebuild — the same as the manual "Update Living
Memories" purge — instead of an incremental keep-LM sweep. One-line change to the `_from_scratch`
gate in `analyze_one` (`deal_engine_sweep.py` ~3076): `"salesforce_trigger"`/`"salesforce"` added to
the from-scratch source set.

**Why.** User-directed: when something new happens on Salesforce, the deal should refresh purely on
the latest truth (no carried memory) — 24h summary / to-dos / score all re-derived fresh.

**How to work with it going forward.**
- **Scoped to the SFDC trigger ONLY.** Manual re-runs (`source="manual"`), scheduled/book sweeps
  (`source="worker"`), and the AI-free hard-refresh are UNCHANGED — they keep living memory.
- Prereq (Salesforce-side): the **Event Relay** to AWS EventBridge must be running — it stopped
  delivering on **Jul 6** (AWS side is all ACTIVE: partner source ACTIVE, rule ENABLED, Lambda
  `CDC_TRIGGER_ON_ACTIVITY=true`, `DEAL_SWEEP_MANUAL_ONLY=false`). Resume it in SF Setup → Event
  Relays. Activity (Task/Event) triggering also needs those entities in the SF CDC channel.
- TODO (next): progressive/ordered writes (24h summary → to-dos → score → rest) + navbar
  notification (admin beta: "sweep running… done").

## 2026-07-14 — Hide unpopulated stubs; fill facts on reactivation (the recurring "$0 pipeline" fix)

**What.** Two guards so a deal never appears before its Salesforce hard facts land:
- `GET /api/deal-engine/opportunities` now drops records with an **empty `hard.stage`** (an
  unpopulated stub) before returning; frontend `keepRecord` (`isUnpopulatedStub`) does the same.
  Every real SF opp has a StageName, so empty stage ⇒ facts not yet pulled.
- `reconcile_membership()` runs a **targeted `hard_refresh_all(opp_ids=…)`** on just-reactivated
  members right after `set_active(reenter, True)` (before the AI enqueue, so the sweep-queue guard
  doesn't skip it). `hard_refresh_all` gained an `opp_ids` subset param.

**Why.** Report reconciliation reactivates a returning member (active=true) with its OLD record —
often a stub with null stage/amount from before it left the book — and only AI-sweeps up to
`DEAL_DISCOVERY_MAX_NEW` (25)/cycle. The rest sat visible as $0/blank rows until the *nightly*
hard-refresh. When a whole rep's book was freshly (re)tracked (Karson Keogh: 38 re-entrants) their
entire MASE looked empty ("Salesforce not connecting"). The hard-refresh itself never nulls (audit
`deal_hard_refresh_runs` clean for weeks) — this was a membership-before-facts ordering gap.

**How to work with it.** Stubs self-heal — once stage lands they reappear automatically. The
reconcile fill is best-effort (a hiccup can't fail reconcile) and logs a `source="reconcile-fill"`
row in `deal_hard_refresh_runs`. A deal you want visible must carry a stage.

## 2026-07-14 — Living-Memory Reconciler: keep the ledger, reconcile it to the latest truth

**What.** Reversed the short-lived from-scratch experiment. Living memory is **kept by default
again** (`DEAL_SWEEP_KEEP_LIVING_MEMORY` now defaults **true**; a from-scratch rebuild is now an
*explicit* escape hatch only — the "Update Living Memories" purge button, `source=
"update_living_memory"`, still forces a no-carry rebuild to clear poisoned memory). On top of the
kept ledger we added a **Reconciler** (Sam Thomas brief):
- **P-4** — every packet carries a stable `entry_id` (survives text re-wordings) + `generated_at_stage`.
- **P-5** — a new locked Omnivision engine `reconciler` (Reconciler 1.0) makes a **RETIRE / KEEP /
  UPDATE** decision per open entry, with a hard **evidence guardrail** (RETIRE requires a *verbatim*
  quote from the sweep evidence; duplicates retire with `evidence:"duplicate"`).
- **P-6** — `_reconcile_open_entries` (Haiku, via the daysum pool) runs each sweep AFTER the packet
  merge/projection over the open `requirement`/`commitment` packets, judged against the latest SFDC
  activities + Avoma notes. A **code-level** guardrail re-checks the model: a RETIRE with empty
  evidence is downgraded to KEEP, so a real action item can never be silently deleted. Retired items
  become `status="resolved"` (+ `retire_evidence`/`retire_reason`/`retire_sweep_date` audit trail),
  which `_live()` drops from the re-projection → they vanish from `ai.*` and `derive_todo` without a
  hard delete. Fixes the Birmingham "submit RFI: done in the score narrative, still open in the
  to-dos" class of contradiction.
- **P-2/P-3** (hardening, same push) — `_soql` now drops an invalid SELECT column and retries instead
  of blanking the book on a schema change; `_recent_activities_deep` filters OOO/auto-reply/
  undeliverable/calendar-status noise from the deep read.
- **Prompts v11** — GROUND TRUTH rule #1 rewritten from "there is NO living memory / rebuild from
  scratch" to "**latest wins — reconcile the ledger to it**" across all 6 engines (rules 2–5 unchanged).

**Why.** From-scratch fixed the contradictions but threw away genuinely useful accumulated context
every sweep. The reconciler keeps the context AND kills the staleness: latest evidence still wins,
but via *retirement-with-audit*, not by nuking the ledger. P-7/P-8/P-9 from the brief were found
already covered (stakeholder_map is already anchored to real `OpportunityContactRole`; MEDDPICC EB
already prefers `MEDDPICC__c`; dedup handled by `todo_grouping.tidy` + the reconciler's DUPLICATES rule).

**How to work with it going forward.**
- Flags: `DEAL_SWEEP_KEEP_LIVING_MEMORY` (default true), `DEAL_SWEEP_RECONCILER_ENABLED` (default
  true), `DEAL_SWEEP_RECONCILER_MODEL` (default `claude-haiku-4-5`). The reconciler is non-fatal and
  skipped under an explicit from-scratch purge (no prior ledger to reconcile).
- A wrongly-KEPT item lingers one extra sweep (harmless); a wrongly-RETIRED item is an audited
  `status="resolved"` packet you can inspect — never a silent delete.
- Edit the reconciler prompt in Admin → Agent Control (engine `reconciler`), NOT in code.

## 2026-07-09 — Parallel fleet: manual triggers → worker queue, autoscaler on (re-applied)

**What.** Manual sweep triggers ENQUEUE again (durable `sweep_queue`, drained by the mase-worker
fleet) and `SWEEP_AUTOSCALE_ENABLED=true` with `SWEEP_AUTOSCALE_MAX=8` → up to 8 workers ×
`DEAL_SWEEP_CONCURRENCY=8` = **64 parallel slots**. `DEAL_SWEEP_MANUAL_ONLY` stays `true`
(automated CDC/scheduled sweeping remains OFF — only explicit manual triggers fill the queue).

**Why.** The fleet was running ~5-wide in-process on the api because the earlier durable-queue
routing was rolled back after the mase-worker wrote null `deal_scores`. Root cause was a
half-rolled deploy leaving the worker on an OLD image (model=claude-sonnet-4-5), NOT the routing.
`deploy.yml` rolls mase-worker to the current image on every deploy, so a clean deploy cures it.
User wants the fleet run in parallel (≥20 at once); the worker fleet is the right engine for that.

**How to work with it going forward.**
- After THIS deploy, before pointing the fleet at the queue, run a 1-deal CANARY: enqueue one opp,
  let a worker claim it, confirm the run logs `model=claude-sonnet-5` and writes a NON-NULL
  `ai.deal_scores`. Only then fan out the full fleet. (This is the check whose absence nulled
  Bosch/NORTHPORT earlier today.)
- Parallelism now comes from workers, so the api no longer runs sweeps — its 4 GB is for UI +
  enqueue only; the 16 GB worker runs the 8 concurrent sweeps.
- To pause everything: `SWEEP_AUTOSCALE_ENABLED=false` (fleet drains then scales to 0).

---

## 2026-07-09 — ROLLBACK: manual triggers back in-process (stale worker wiped scores)

**What.** Reverted the same-day change that routed manual sweep triggers through the durable
`sweep_queue`. Manual triggers run in-process again (`trigger_opp_async`), now at
`DEAL_TRIGGER_CONCURRENCY=8` on a 4 GB api task. `SWEEP_AUTOSCALE_ENABLED` back to `false`.
Kept from the original fix: per-role sizing (api `1024/4096`, worker `4096/16384`) and the
8-wide trigger concurrency. Also added the missing `claude-sonnet-5` key to `_LLM_PRICING`.

**Why.** The running `mase-worker` task was on an OLDER task-definition revision — its runs
logged `model=claude-sonnet-4-5` while the api logged `claude-sonnet-5`, and it wrote
`deal_records` rows with `ai.deal_scores = null`. Routing manual triggers to it CLOBBERED good
governed scores: NORTHPORT (27/8 -> null) and Robert Bosch (54/49 -> null). The api tier runs
the current image and scores correctly, so correctness beats durability here. Separately,
`_calculate_llm_cost` had no key matching `claude-sonnet-5` (`"claude-sonnet-4" in
"claude-sonnet-5"` is False), so it returned 0.0 — every Omnivision sweep logged `cost_usd=NULL`
and the cost dashboard reported them as free.

**How to work with it going forward.**
- Do NOT re-enable the queue path or the autoscaler until a `mase-worker` run is verified to log
  `model=claude-sonnet-5` AND write a non-null `ai.deal_scores`. The worker service's task
  definition must be confirmed current — the autoscaler only changes `desiredCount`; it launches
  whatever revision the service points at.
- A no-data sweep (`calls_read=0`) can still overwrite a good record with null scores — the
  `_no_data` carry-forward guard requires calls_read=0 AND no footprints AND no CRM evidence, so
  `roles>0` defeats it. Separate bug, still open.
- Sonnet 5 carries an introductory $2/$10 per MTok through 2026-08-31; the table encodes the
  $3/$15 list price, so logged cost is an upper bound until then.

---

## 2026-07-09 — Manual sweeps are durable + 8-wide (OOM incident fix)

**What.** A manual `POST /api/deal-engine/sweep/trigger` no longer runs fire-and-forget on
the web tier. Under `DEAL_SWEEP_MANUAL_ONLY=true` it is now **enqueued** as a durable
`waiting` row and drained by the `mase-worker` fleet (`DEAL_SWEEP_CONCURRENCY=8` per worker,
autoscaled to `SWEEP_AUTOSCALE_MAX=6`). Automated sweeping (Salesforce CDC, scheduled) stays
**paused** — the route still drops non-manual sources, and `enqueue_trigger` re-checks
`manual_only()` independently. Task sizing is now per-role: api `1024/4096`, worker
`4096/16384` (was a shared `1024/2048`). `DEAL_TRIGGER_CONCURRENCY=8` set for the
in-process emergency fallback (`DEAL_SWEEP_USE_QUEUE=false`), which defaulted to 3.

**Why.** 2026-07-09 ~14:13 UTC: seven manual triggers were fired for the forecasted-deal
set. Two completed (SAMI 10.4m, Allstate 12.8m); the other five died in the same instant.
The api container (1 vCPU / 2 GB) was running three concurrent sweeps — each holding the
locked win+mom+sweep engines plus the full vendordict/playbook reference bodies (~58K) and
a deal's Avoma transcripts — and was OOM-killed. Because those sweeps were
`asyncio.create_task` coroutines on the web process, they died with the event loop:
`analyze_one`'s `finally` never ran, so **no `deal_trigger_runs` row, no error, no retry**,
and the `_trigger_inflight` claim vanished with the process. The deals silently never
updated, and nothing in the system could tell they had failed. `worker.py` already solves
this — it reclaims claimed-but-unfinished rows back to `waiting` on restart — the trigger
route just never used it.

**How to work with it going forward.**
- A manual trigger returns `accepted` / `already_queued`; watch `deal_sweep_queue`, not an
  in-memory claim. A run that finishes (success OR failure) always writes `deal_trigger_runs`.
- Silence is no longer possible: a killed worker leaves its row reclaimable.
- `DEAL_SWEEP_TIMEOUT_S=2400` (40m) is deliberate — it converts a wedged sweep into a
  recorded `failed` row instead of a slot held forever. Do not remove it.
- Do NOT set `DEAL_SWEEP_MANUAL_ONLY=false` to get parallelism; that also resumes automated
  CDC sweeping. Parallelism now comes from the worker fleet under manual-only.

---

## 2026-07-09 — sf-report-watch: Salesforce report → VIBE project dispatcher (new infra)

**What.** New scheduled Lambda `infra/sf-report-watch/lambda_function.py` (+ state
schema `migrations/0013_sf_report_watch.sql`). On an EventBridge 5-min schedule it
polls the object behind the Salesforce report **APAC GTM MQL Global_V1**
(`00OP7000005v4TsMAI`, a *Contacts with MQL History* report = `MQL_History__c.MQL__c
= true AND Contact.Account.Geography__c = 'APAC'`), and for each NEW `MQL_History__c`
row POSTs to VIBE `/api/workflows/dispatch-abm` (Bearer `DISPATCH_SECRET`) to kick a
run under the contact's owning BDR in the target project (`MQL_ABM_PROJECT_ID`).
Zero-dep stdlib, mirrors `sf-cdc-bridge`; SF auth is the SOAP username-password login
(same `SF_*` creds as `salesforce_mcp_server.py`), session id reused as REST Bearer.

**Why.** Reports can't be subscribed to (CDC watches objects, not reports) and this
one is Matrix format, so a scheduled SOQL poller with a high-water mark is the robust
"new entry → project run" pipe. First deliverable of the MQL→ABM automation.

**How to work with it going forward.** "New" = high-water mark on SF `CreatedDate`
(second precision; `MQL_Date_Time__c` is 5-min-bucketed so it would drop ties) PLUS a
dedup ledger on the `MQL_History__c` id in `sf_report_watch_log` → exactly-once.
`sf_report_watch_cursor` holds the watermark, **seeded to "now" on first run** so a
deploy does NOT fire the existing backlog (`SEED_WATERMARK_ISO` to backfill;
`MAX_DISPATCH_PER_RUN` caps per-run; `DRY_RUN=true` to observe without dispatching).
Owners not present as VIBE users fall back to `FALLBACK_BDR_EMAIL` (else logged
`skipped_no_bdr`). The report's exact filter/columns were read via the Analytics
`ReportManager.describeReport` metadata; the poller SOQL is pinned to that definition.
Deploy + env table in `infra/sf-report-watch/README.md`. Salesforce stays read-only
(no `MCP_TOOL_DENYLIST` interaction — this queries via REST, writes nothing to SF).

## 2026-07-09 — Win Position momentum gate (win engine v10.7)

**What.** On a LATE-STAGE deal (recorded stage Vendor Selected or later) whose Deal Momentum
< 30, the Zycus Win Position is now HALVED — the §4.4a qualification-depth floor (which credits
historical EB/MEDDPICC depth) is overridden by the new **§4.4b momentum gate**. Locked as **win
engine v10.7** in Omnivision (Scoring Version Studio). Verified: ACEN (Vendor Selected, momentum
8, 330 days dark) drops from win **40 → 20**.

**Why.** Historical qualification and a high stage protect a deal only while it is still alive in
the market; a late-stage deal that has gone momentum-dead is a stalled deal wearing a stage badge,
not a strong win. (Reviewer-directed.)

**How to work with it.** The rule lives ONLY in the locked win engine (Supabase / Omnivision) —
edit + lock a new win version to change it, no code deploy. A repo copy of the current win-engine
prompt is synced to `prompts/studio_seeds/win-position.md` on each lock. Momentum ≥ 30 → gate off,
the §4.4a floor applies normally.

## 2026-07-09 — Future-dated SF activity no longer read as "happened" + queue-auth gate

**What.** (1) A Salesforce Task/Event/`LastActivityDate` dated in the FUTURE (a merely
scheduled, not-yet-held meeting) is no longer treated as verified past engagement.
`deal_engine_footprints._is_future` excludes it from `last_meeting` / `last_buyer_touch` /
`general_last_activity` / engagement points; `deal_engine_pulse.compute_pulse` splits it out
as `next_scheduled_date` instead of folding it into `last_activity_date` (which drove the false
`state=live` + the "Last recorded activity 2026-07-13" bullet on Publicis — a call calendared
4 days out read as having already happened). `deal_engine_cro` surfaces the honest fact
("Next meeting scheduled …") instead of dropping it. (2) **Queue-auth gate**: a still-running,
abandoned Replit deployment of this codebase (frozen pre-Omnivision/pre-datalake) has been
polling the SAME production `sweep_queue` via the shared service-role key and silently
writing old-schema, unscored records over live deals (Publicis, 15+ others). Added
`public.claim_one_sweep_v2(p_secret)` (Supabase migration, `_sweep_auth` singleton table,
service-role-only) — `sweep_queue.claim_one()` now calls it with `SWEEP_QUEUE_SECRET`
(new key in the `mase/app-env` Secrets Manager secret, auto-picked up by
`render_taskdef.py`'s live key enumeration). The original zero-arg `claim_one_sweep()` is
**left untouched for now** — neutering it (so Replit's calls go permanently empty-handed) is
a deliberate follow-up step, done only after confirming the ECS worker is healthy on v2.

**Why.** The future-date bug: SF's `LastActivityDate` rollup includes the next/most-recent
Event even before it's held, and `_age_days`/`_days_since` both clamped a negative (future) age
to 0 — "happened today" — instead of treating it as no verified signal. The queue-auth gap:
`claim_one_sweep()` takes no arguments and is callable by anyone holding the service-role key,
so two independent deployments have been racing to claim the same rows with no way to tell them
apart.

**How to work with it going forward.** A future SF date is now ALWAYS excluded from "what
already happened" signals project-wide (footprints + pulse); use `next_scheduled_date` /
`pulse.get("next_scheduled_date")` if you need to surface an upcoming touch honestly. For the
queue: any NEW claimer must call `claim_one_sweep_v2` with `SWEEP_QUEUE_SECRET` — the old
zero-arg RPC is legacy-only and will start returning nothing once neutered (watch for that
follow-up entry before assuming Replit is fully cut off).

## 2026-07-09 — Studio v2: Deal Sweep engine + reference assets (vendor dictionary, playbook) wired end-to-end

**What.** Adopted the governance prototype Sam landed in the MASE repo (`scoring-studio/index.html`,
commit 4dda444) into the REAL Omnivision + runtime. The Studio now governs EIGHT versioned assets:
the five engines + **`sweep`** (Deal Sweep / Deal Drawer v10.0 — the rebuilt canonical-record
instruction from `refs/deal-sweep-v3.md`) + two REFERENCE assets — **`vendordict`** (Vendor
Dictionary v1.0, canonical vendor entity-resolution glossary) and **`playbook`** (Deal-Progression
Playbook v1.0) — cited via stable `{{ref:vendor-dictionary}}` / `{{ref:deal-playbook}}` tokens.
Also locked **extract v10.4** (= v10.3 + §A5b vendor/competitor entity resolution, composed exactly
per the prototype). Changes: `scoring_studio.py` (ASSETS model, `resolve_refs`, `reference_sections`,
ref-aware list/active/lock), `deal_engine_sweep.py` (**the locked `sweep` engine now REPLACES the
monolithic `mase_deal_sweep` base prompt** when present — precedence: locked sweep → agent-control
override → disk seed; the studio block appends the other five engines with tokens resolved + ONE
locked copy of each reference; provenance stamps include sweep + refs), a **verdict compatibility
adapter** (Deal Sweep v3 drops `north_star_verdict` for `ai.forecast_read` — the server now carries
the prior verdict forward, else synthesizes On Track/At Risk from forecast_read, so the UI badge /
pulse reconcile / fallback-scorer caps keep working), and `scripts/scoring_studio_v2_seed.py`
(CHECK-constraint extension + idempotent seed; APPLIED 2026-07-09). Seeds archived under
`prompts/studio_seeds/` (cold-start copies; Supabase stays the source of truth).

**Why.** "Connect the new Studio assets to the sweep and Omnivision so every sweep actually runs on
them" (user-directed). The Omnivision UI is API-driven, so the three new asset cards appear
automatically; editing + locking any of the eight in /omnivision now changes the next sweep with no
code deploy.

**How to work with it going forward.** The sweep's base prompt is the LOCKED `sweep` asset — edit it
in /omnivision (Deal Sweep card), never in Agent Control (that override is now the fallback only).
New rival names/ASR mishearings go into the Vendor Dictionary asset; domain knowledge goes into the
Playbook. Effective prompt ≈147k chars (was ~130k). Watch-outs: v3 emits `ai.forecast_read` (new
field, persisted as-is) and no verdict (adapter handles it); the day-summary generator still runs on
the locked `sum` engine unchanged.

## 2026-07-09 — Sweep hardening: CEO-finalize NameError + scoreless-persist clobber (John Deere/Publicis)

**What.** Three fixes in `deal_engine_sweep.analyze_one`: (1) **`_pkt_allow` NameError** — the
native CEO-intervention finalize referenced `_pkt_allow`, a LOCAL of the nested
`_apply_living_memory()`, so it raised NameError on EVERY sweep and silently fell back to
carrying the prior CEO value; the allowlist is now built at the call site (`_ceo_allow`, same
recipe). (2) **Exception-path score carry-forward** — if the whole scoring block raised, the
record persisted with NO `ai.deal_scores`, blanking a good stored score; the except path now
carries the prior scores forward (`stale_read` marked). (3) **Never-clobber guard hardened** —
its persist-time re-read now falls back to the sweep-start snapshot when the re-read fails or
returns unscored, so the guard can't silently no-op mid-race.

**Why.** During the 2026-07-09 test sweeps, duplicate concurrent runs of the same opp (operator
re-queues + deploy reclaims + Avoma 429 slowdowns) raced, and a scoreless persist LANDED ON TOP
of scored records — John Deere (52/88 → None) and Publicis (99/99 → None). The CEO NameError was
found in the same logs, firing on every deal.

**How to work with it going forward.** A sweep can no longer persist a scoreless record over a
scored one on ANY path. Known residual (deliberately not fixed here): duplicate sweeps of one opp
can still run concurrently (no distributed per-opp lease); the guard removes the damage. Avoid
resetting `working` queue rows while a sweep might still be in flight.

## 2026-07-09 — Deterministic scorer (now the fallback) — dormant floor + verdict/close/differentiator fixes

**What.** Shipped the local edits to `deal_engine_scoring.py`: (1) **dormant/on-hold floor** — a
buyer-parked, inert deal (next step says on-hold/suspended/frozen/relaunch-in-future-year AND ~0
engagement) caps Deal Momentum at 8, so a merger-frozen deal can't read "Steady" off a status
email confirming the freeze; (2) **verdict-reconcile** — a Slowing verdict caps momentum at 60,
Off Track at 35 (one story per deal); (3) **close-push ramp** — a beyond-60d close slip charges
the whole push (−5..−12), not a from-zero ramp; (4) **blocked-differentiator guard** — a "ZERO AI"
buyer ban caps AI differentiation. These are the anti-inflation floors validated on the Galp /
Austrian Post / John Deere calibration set.

**Why.** `deal_engine_scoring.py` is now the FALLBACK scorer (the primary path is the Omnivision
Studio-governed AI scorer — see the entry below). These fixes keep the fallback honest for the
cases the AI call can't cover (LLM failure / hard loss). NOTE: under "pure Studio, no floors" the
Studio path itself carries no deterministic floors — these live only in the fallback.

**How to work with it going forward.** Primary scoring = the Studio win/mom engines (edit in
`/omnivision`). This deterministic engine only runs when the AI scorer fails; tune it in code.

## 2026-07-09 — Deal scores now GOVERNED by the Omnivision Scoring Version Studio (win/mom)

**What.** Re-enabled the AI deal-scorer (`DEAL_ENGINE_AI_SCORING=true`, both api + worker) and
rewired it to be GOVERNED by the locked Scoring Version Studio engines. The two headline scores —
**Zycus Win Position** and **Deal Momentum** — are now produced by the LLM applying the LOCKED
`win` + `mom` instructions from `/omnivision`, exactly as the 24-Hour Summary is governed by the
locked `sum` engine (`day_summary_ai`). `deal_engine_ai_scoring._prompt()` reads the locked engines
via `scoring_studio.active_locked()` and appends only a thin OUTPUT ADAPTER (the JSON shape + the
three scores with no Studio engine: commitment/risk/forecast). **Pure Studio — the deterministic
floors are NOT applied on top** (the `_normalize` late-stage/zero-engagement clamps were removed,
user-directed). Deterministic `deal_engine_scoring.py` stays as the FALLBACK only (LLM failure or a
hard loss), so a deal is never left unscored.

**Why.** The Studio is meant to be the control plane for scoring, but until now editing win/mom in
Omnivision did NOT change the stored numbers — those came from the Python scorer. So the Studio and
the DB disagreed (e.g. John Deere read 88 from the code vs 31/45 from the prompt). Governance closes
that gap: lock a new win/mom version → it governs the next sweep, no code deploy.

**How to work with it going forward.** Tune scoring by editing + locking `win`/`mom` in `/omnivision`
(NOT `deal_engine_scoring.py`, which is now only the fallback). The current locked `mom` (v10.4/10.5)
carries a §9 "generous-reading" floor that lifts some dark/declining deals to Steady 45 (FGV 136d
dark → 45, John Deere paused → 45, Austrian Post −31% amount → 73) — fix that IN the Studio prompt.
Cost: +1 LLM call per deal per sweep. Reverses the 2026-07-07 "one scorer / deterministic-only"
decision, this time with the Studio as the single source of truth.

## 2026-07-09 — Live sweep prompt was reading its own deprecation banner (fixed)

**What.** The `mase_deal_sweep` Supabase row had been seeded WITH the disk file's leading
`<!-- DEPRECATED -->` banner, so every sweep's system prompt literally opened with "do not edit
this file / this is deprecated". The row is cleaned (banner removed via POST /sweep/prompt;
backup kept locally), and `agent_prompt_store` now strips ONE leading HTML comment at BOTH load
(`get_prompt`) and save (`set_prompt`) so a future paste-with-banner is harmless for every
prompt key. Frontend editors gained the same guard + a live-provenance badge (see MASE repo
CHANGELOG 2026-07-09).

## 2026-07-09 — Omnivision E2E proven + strict/loose scoring evals + ROGUE SWEEPER found

**What.** (1) `docs/cro-scoring-doctrine.md` — the CRO-voice doctrine for Win Position + Deal
Momentum. (2) E2E injection PROVEN: the worker's effective-prompt sha256 matches base-prompt +
locked studio block exactly, hot-swaps within seconds of a lock (`prompt changed …; rebuilding
agent`), and every swept record stamps `ai.scoring_studio.versions`. (3) Calibration evals: mom
v10.3 (STRICT) and v10.4 (LOOSE) locked, 11 forecasted deals swept under each →
`Desktop\eval_strict.csv` / `eval_loose.csv` / `eval_comparison.csv` / `eval_cro_analysis.md`;
v10.5 (content-identical to production doctrine) re-locked after. Result: momentum numbers are
deterministic (identical under both variants, by design — the instruction steers the AI reading/
narrative); WIN moves with the instruction on evidence-thin deals (±9-10) and holds on
evidence-backed ones.

**⚠️ Why it matters / the find.** The ABANDONED REPLIT DEPLOYMENT (`agent-salesforce-link.replit.app`)
still runs a queue worker on OLD code against the production `sweep_queue`. Every claim it wins
(most of the 07-08 afternoon: Teads, Discovery, Lincoln, PSEG, Changi, HAECO, Bandhan, Cebu…)
writes an old-schema record with NO `deal_scores`, NO datalake footprints and NO Omnivision
governance — 15 live records were left with blank score panels; all re-swept in a repair pass.
**Until its sweep worker is stopped (user action, Replit console — the app itself must stay up
for the MCP endpoint), any Omnivision lock only governs the sweeps the ECS worker wins.**

**How to work with it going forward.** Evals against live locks: use the per-record version stamp
as ground truth and re-enqueue on mismatch (see `eval_run_batch.py`). Momentum *number*
calibration from the locked text = Phase 3 (not built, deliberate). Helper scripts kept in repo
root (`eval_*.py`); eval CSVs on the Desktop, never in git.

## 2026-07-08 — Omnivision Phase 2: locked instructions GOVERN the live sweep

**What.** The five LOCKED Studio instructions are appended to the effective sweep prompt as the
authoritative final section (`deal_engine_sweep._studio_block`, 5-min TTL, fail-open) — so editing
+ locking in /omnivision changes the actual deal sweep on its next run, no deploy. Every swept
record stamps `ai.scoring_studio.versions` (provenance). `day_summary_ai.py` is governed by the
locked 24h-Summary text (its JSON contract kept as an output adapter). Locking busts the cache +
resets the cached sweep agent.

**How to work with it.** Win/Momentum ARITHMETIC stays deterministic code (hybrid model): the
locked win/mom texts govern the LLM's reading/evidence/rationale, not the Python constants —
changing anchors/ceilings still means a code change (or Phase 3: constants parsed from the locked
text). Drafts never run; no locked version → no injection.

## 2026-07-08 — Scoring Version Studio (Omnivision) — control plane shipped

**What.** Phase 1 of the versioned, lock-before-run engine-instruction system from the
MASE_Scoring_Studio staging handoff. (1) Supabase **`scoring_instructions`** (versioned texts,
required changelog note, locked/locked_by/locked_at) + **`deal_outputs`** (provenance), seeded with
the five engines' full trails — latest locked: extract 10.3, win 10.3, mom 10.1, todo 10.1,
sum 10.1 (`scripts/scoring_studio_schema.py`, idempotent). (2) **`scoring_studio.py`** + endpoints
`/api/deal-engine/scoring-studio/*`: engines · trail · version content · single-unlocked-draft
save/discard · lock (kind minor/major + required note → next semver) · `active` (runtime resolver —
latest LOCKED per engine; drafts invisible). (3) Frontend (MASE repo, staging): **/omnivision**
route, SUPER-ADMIN only (aleen.dhar + sam.thomas via `SUPER_ADMIN_EMAILS`, enforced in the
deal-engine proxy on every method).

**Why.** User-directed: one place where the system prompts for the different engines live, editable
with version control, an edit-blocks-run lock gate, and provenance — restricted to the two platform
owners. Full mapping of the handoff's five engines vs today's implementation (win/momentum are
Python, extraction/todo/24h live inside the monolithic sweep prompt) + the calibration deltas:
**`docs/scoring-studio-gap-analysis.md`**.

**How to work with it.** Phase 2 (NOT built): wire the sweep to `active_locked()` (lock-before-run),
split the monolithic prompt into per-engine locked instructions, stamp instruction/extraction
versions on every output. Adopting the handoff's more conservative calibration (RFP ceiling 60,
momentum start 35, …) is a per-parameter decision = a locked version bump + book rescore.

## 2026-07-08 — Momentum: engagement is DISCOUNTED when the deal is declining

**What.** In `score_momentum_v2`, the engagement-points term is now multiplied by a decline factor
when the deal is trending down in SUBSTANCE: **×0.82** with a forecast downgrade OR a scope/amount
cut, **×0.66** with both. A close-date slip is deliberately NOT counted (timing, already tolerated),
so the discount fires only on substance-declining deals, never on a healthy deal that rescheduled.

**Why.** Engagement rewarded raw meeting VOLUME regardless of direction, so a busy deal being
renegotiated DOWNWARD banked near-max points (~35) that the flat decline penalties (−10 regression,
−6 scope cut) couldn't fully offset. Austrian Post: onsite + pricing rounds → engagement 34 while
amount −31% and forecast cut → momentum still 68. Now the busywork itself is discounted →
**Austrian Post 68 → 57**. Surgical: **27 deals** move (avg −2.2); routine close-slip deals
(SAMI, S&C, McAfee) are untouched.

**How to work with it.** Factors tunable in the engagement block. This was the last item on the
scoring inflation watch-list (`docs/deal-scoring-logic.md` §7). Ships with the next worker deploy;
book rescored.

## 2026-07-08 — Win rubric: honest-examination guards (at-risk champion, keyword-only preference)

**What.** Two guards at the end of `deal_engine_scoring._rubric_win_strengths` stop the win rubric
reading MAX strength off prose while the deal's own flags say otherwise. (1) An **at-risk champion**
(`ai.champion_strength.at_risk == true`) is capped at partial (0.3) — it can no longer score
full-strength off a "strong" prose label. (2) **Preference** is capped at moderate (0.5) when it was
maxed from a NARRATIVE KEYWORD with **no structured `customer_preference`** AND the deal is visibly
**declining** (forecast cut / amount cut).

**Why.** Austrian Post read **win 70** but the raw was propped by preference **+1.0** (from a
keyword; `customer_preference` was empty; the buyer is renegotiating DOWN) and champion **+1.0**
(despite `at_risk=true` — the champion himself said they were being "strung along" — and MEDDPICC
champion = *partial*). Honest win **70 → 64**. Surgical: **22 deals** move (avg −4); most at-risk
champions were already scored low so are untouched.

**How to work with it.** Caps are conservative and tunable in the guard block. A genuine structured
high preference, or a non-declining deal, is untouched. Ships with the next worker deploy; book
rescored.

## 2026-07-08 — Forecast-category order FIXED + momentum reacts to DECLINE (not just volume)

**What.** (1) `deal_engine_trends._FC_RANK` corrected: "Upside Key Deal" ranked ABOVE "Best Case"
(3 vs 2) — backwards. Real order is Omitted < Pipeline < **Upside < Best Case** < Commit. A
`Best Case → Upside` move is a DOWNGRADE; the old map scored it as an UPGRADE. (2)
`score_momentum_v2` stage/forecast term is now **symmetric**: a recent up-move still +6, but a
**downgrade / stage regression now −10** (was: no penalty at all — momentum only ever *added*). (3)
New **scope-cut drag −6** when `amount_trend < −0.2` (a deal renegotiated smaller is contracting).

**Why.** A deal with every substantive signal DOWN — amount cut 31%, forecast cut Best Case→Upside,
close slipped 23d — still read **momentum 90** because momentum rewarded raw meeting VOLUME and was
blind to decline, AND the forecast cut was mis-scored as a +6 upgrade. Austrian Post: **90→68**
("actively engaged but terms declining"). The forecast-order bug flipped the sign of every
Upside↔Best Case move book-wide (inflating momentum + win-nudge on downgraded deals). 40 deals
corrected (avg −8.6); the −22 swings had both a downgrade and a scope cut.

**How to work with it.** Ships with the next worker deploy. Offline rescore re-derives the stored
`opp_trends.forecast_category_trend` sign from its detail using the corrected rank, then recomputes.
Future sweeps recompute trends fresh. Momentum now = engagement/activity MINUS decline (downgrade,
scope cut, stall) — a busy-but-shrinking deal no longer reads hot.

## 2026-07-08 — Momentum: "advancing plan" credit counts only NEAR-TERM FUTURE milestones

**What.** `deal_engine_scoring.score_momentum_v2` next-step-plan term no longer credits a deal for
its Next-Step *history*. It previously gated the +8/+11 "live advancing plan" on `dated` = the count
of ALL parsed milestone dates (mostly PAST log entries) and accepted ANY single future date — even
one after the close. Now the credit counts only milestones that are in the FUTURE **and inside a
90-day planning horizon** (`_plan_ms`); the total `dated` count is retained ONLY for the
false-velocity signal. +8 for ≥1 near-term future milestone, +3 more for ≥3.

**Why.** Austrian Post read momentum **99** (the max). Its Next-Step is a 10,006-char running
journal; the parser found **44 dates — 42 already past, and the only 2 future ones in December,
after a 23-Jul close**. It scored the full +11 "advancing plan" with ZERO real near-term milestone,
pegging momentum to 99. Systemic: **113 of 409 live deals** were inflated this way — including
stalled deals the noise was masking (Louis Dreyfus momentum 55→19, last touch 99d ago, close
slipped 195d; Hager 73→37, 147d quiet). This is the "everything scores too high" pattern.

**How to work with it.** Austrian Post honest momentum **99→90** (still high — genuinely active
onsite + pricing cadence earns the engagement points; the fake plan credit is gone). Book rescored.
Horizon tunable in code. A milestone beyond 90 days (or after the close) is context, not current
momentum.

## 2026-07-08 — Avoma: multi-part meetings read WHOLE + anti-fabrication guard

**What.** Two fixes so the sweep never invents a meeting it only half-read. (1) **Multi-part
grouping** in the datalake transcript budget (`deal_engine_sweep._avoma_prefetch_from_datalake`):
same-day recordings of ONE meeting — `Teil 1`/`Teil 2`, `Part 1/2`, `Session/Day/(1/2)` — are now
grouped (`_meeting_group_key` + `_MEETING_PART_MARKER`) and inlined as a UNIT (whole meeting or
notes-only, never one part while dropping the other); the single newest meeting is guaranteed
inlined. Budget raised 80k→110k and per-call cap 48k→56k so a grouped onsite fits whole alongside
the newest call. (2) **Anti-fabrication guard** appended to the live `mase_deal_sweep` Supabase
prompt (§2.12, via `apply_antifabrication_guard.py`): never assert a negative meeting fact ('CPO
never showed up', 'left unresolved') from missing/partial coverage; multi-part meetings are one
meeting; absence in the read slice ≠ the event didn't happen.

**Why.** Austrian Post's "Last meeting" read said *"the invited CPO never showed up… pricing gap
left unresolved"* about the 1-Jul onsite. Proven from the datalake: the onsite is **two**
recordings — `Zycus (Onsite) Teil 1` + `Teil 2` — and **Teil 2 (46,082 chars), where the CPO
actually spoke, was dropped to notes-only** because Teil 2 alone ate the 80k budget and Teil 1
fell out. The model summarised half the meeting and invented the rest. Teil 2's transcript opens
with ~600 chars of small-talk (a sightseeing/conference chat), so even the old REST `[:6000]`
path would have captured pleasantries and missed the substance.

**How to work with it.** The datalake path is already the default (`DEAL_SWEEP_AVOMA_FROM_DATALAKE`);
this makes its coverage whole-meeting-safe. Verified on Austrian Post: both onsite parts now inline
as one 71,231-char unit. Tunable via `DEAL_SWEEP_AVOMA_DL_TRANSCRIPT_BUDGET` / `_MAXCHARS`. The
prompt guard is additive + backed up outside the repo + reversible. **Code change needs a worker
deploy to take effect; the prompt guard is already live.** The Austrian Post record's stale
"Last meeting" text was separately corrected in-place (pinned deal, not re-swept).

## 2026-07-07 — Momentum v4, durable pins/CEO/24h ownership, one score source, panel polish

**What.** (1) Momentum rebuilt (direction_v4): engagement volume is primary (ALL real sessions
+ INBOUND buyer replies count — footprints.last_buyer_touch/buyer_touches_30d; a narrative
'top event' can no longer zero real meetings); a close-date push only drags when the buyer is
ALSO quiet (engaged deals: timing, already priced into Win); stage/forecast up-moves +8;
false-velocity cannot fire on a progressed/engaged deal (Bosch). (2) Ownership rules the
sweep must honor: ai.pinned re-persisted every sweep (pins are durable until unpinned);
source=='ai' day_summary carried verbatim (the intelligent 24h can never be replaced by the
template dump); CEO watches carry forward (verified live: Cornell swept, watches survived).
(3) ONE source of truth for scores: ai.deal_scores.headline drives list + drawer cards +
modal (frontend 267aa05); panels are rebuilt FROM the final headline everywhere. (4) Panel
polish (_polish_panel): no engine internals, no raw __c field names, one fact once, pretty
dates. (5) 90-day evidence window appended to the live sweep prompt (Supabase): current-state
narratives may not retell old history as live motion.

**Why.** The day's user-reported regressions: Alghanim 'dark 74d' while the buyer replied 3x
a week ago; Bosch 33 win from a false-velocity misfire; three different scores on three UI
surfaces; sweeps stomping intelligent 24h summaries and pins; ACEN narrating Jun-2025 as
current.

**How to work with it going forward.** Pins: set ai.pinned + deal_scores.pinned; sweeps carry
them verbatim; unpin to hand back to the engine. 24h: day_summary_ai.py owns ai.day_summary
(source 'ai'); the sweep's deterministic build is a backstop only. Momentum/scoring edits go
in deal_engine_scoring.py + tests/test_deal_reasons.py. Score display: headline only — never
render panel-embedded numbers. Hand-fix scripts: boost_three.py (pins), restore_ceo.py +
ensure_scope_shrink_ceo.py (CEO), recompute_broken.py / qa_self_heal.py (component heals).

## 2026-07-07 — hard-flag floor on economic_buyer (the examination can't out-claim Salesforce)

**What.** `deal_engine_scoring._eb_status_floored(record)`: if Salesforce's own `hard.eb_identified`
flag is **False** AND the sweep recorded **no evidence** for its inferred `economic_buyer` status,
that `partial/confirmed` is floored to **gap**. Wired into both the qualification ceiling
(`_qualification_ceiling`) and the `exec_access` rubric factor. This closes the hole the
qualification-gate left open: a no-evidence "partial" EB dodged the Access-to-Power cap.

**Why.** Avaya Corp read **Win 70** at Formal Evaluation — but the raw win was only ~44; momentum
coupling (+25) carried it, and the 52 cap that should have caught it was dodged because the sweep
marked `economic_buyer=partial` with `(none)` evidence, contradicting SF's `eb_identified=False`
(and `dm_identified=False`). Book-wide this mismatch hit **28 deals — all 28 with empty EB evidence**
(pure inference), 11 inflated above 52. Same principle as the stage-authority playbook: the engine
must never be more bullish than the hard CRM facts. The scoring gate is only as honest as the
examination feeding it — this makes a hard SF fact win over an unbacked LLM inference.

**How to work with it.** Avaya / Vodafone Idea / Gamuda / National Holding / Etex / Hager … → ≤52.
Safe by construction: only fires when `eb_identified=False` **and** EB evidence is empty — a
genuinely-evidenced EB, or `eb_identified=True` (104 deals), is untouched; expansion-into-won-account
and post-selection stages keep their lift. The durable fix for a wrongly-floored deal is the sweep
recording real EB evidence (examination quality), not re-opening the floor. Momentum→Win over-credit
(the other half of Avaya's 70) is still open — tracked separately.

---

**What.** Win Position is now ceilinged by qualification (`deal_engine_scoring._qualification_ceiling`),
applied after the raw compute so momentum / stated preference can't lift a deal past what its
qualification boxes support. **Access to Power (MEDDPICC `economic_buyer`) is the dominant gate:**
`gap` → win capped **52**, `partial` → **74**, `confirmed` → no cap. Competitive visibility and
champion depth also gate the top (`competition`/`champion` gap → 66/60). Once the **hard SF stage is
Vendor Selected+**, the stage itself proves selection → no cap (Publicis / Swift / Mair untouched).
Two supporting fixes: (1) the selection override now **requires a confirmed economic buyer** (+ a real
Commit/won signal, verdict not Slowing, a *positive* competitive edge — not "unknown rivals"); (2) the
CRO panel no longer renders a weak/unknown competitive field as "✅ Edge over the competition" — it
reads "⚠️ Competitive field still unmapped".

**Why.** The engine was bullish on inferred soft signals and blind to obvious gaps. Barnes & Noble —
Formal Evaluation, SF forecast **Pipeline**, verdict **Slowing**, `economic_buyer=gap`, single-threaded
to a Manager, unknown competitors — read **Win 99** off the override + a stale April "checks the boxes"
quote. That violates the stage-authority rule (*the engine must never be more bullish than the hard CRM
facts*). User's 7-point-drill logic: *momentum first, then win; and tick the qualification boxes before
a higher probability can be established.* Book-wide, 25 of 60 high-win deals had `economic_buyer=gap`.

**How to work with it.** B&N 99→52; ~41 no-EB Formal-Eval/Shortlisted deals corrected to ≤52; momentum
is UNTOUCHED (the gate caps win regardless of momentum). Legit late-stage deals untouched. Cap values
live in `QUAL_EB_CEILING` / `QUAL_COMP_CEILING` / `QUAL_CHAMP_CEILING`. Diagnostics:
`diag_qual_gate.py`, `diag_scoring_integrity.py`, `diag_momentum_inflation.py` (all read-only). Rescore
the book with `refresh_scores_panels_all.py --rescore --apply` (skips pins on `ai.pinned` OR
`deal_scores.pinned`). If a deal is wrongly capped because its EB is engaged but MEDDPICC under-read it
as `gap`, the fix is the sweep's MEDDPICC accuracy (examination), not re-opening the gate.

---

## 2026-07-07 — selection override is stage-gated (a selection can't precede an evaluation)

**What.** `deal_engine_scoring._selection_override` now returns `False` for any pre-RFP /
unknown stage (Initial Interest, Qualified, prospect, discovery, lead, `1.*`, `2.*`). The
override unlocks the 100 win-ceiling for a **confirmed selection whose CRM stage lags**; it had
no stage floor, so early-stage deals that merely *looked* strong (positive preference + no named
competitor + a defensible-ish verdict) tripped all three gates and blew past the pre-RFP ceiling.
PremiStar — a 7-day Qualified deal with one discovery call — read **win 99**. Book-wide, 4
Qualified deals were affected (PremiStar, Parsons International, Northeastern University,
International SOS), all now correctly capped at **30**.

**Why.** A selection cannot happen before an evaluation exists. Crossing a stage ceiling must
require the deal to actually be in (or past) the RFP round. Pre-RFP hard cap stays 30 (spec §7).

**How to work with it.** Legit Shortlisted / Formal-Eval selections still cross (Global Switch
holds at its Shortlisted ceiling of 70 because its fresh sweep set `forecast_defensible=False` —
that's the gate doing its job, not the stage block). If a genuinely-selected deal is stuck below
its stage in CRM, fix the stage or pin it — don't reopen the pre-RFP gate. Helper:
`rescore_prerfp_overcap.py --apply` re-caps any pre-RFP deal found above 30.

## 2026-07-07 — `calls_read` is now deterministic; sweep model → `claude-sonnet-5`

**What.** (1) In `deal_engine_sweep.analyze_one`, `result["calls_read"]` (and the persisted
`evidence_coverage.calls_read`) is now the **max of the LLM self-report and the engine's actual
datalake/Avoma read count** (`_avoma_pf.coverage.read`) — a deterministic floor, not a number the
model writes. (2) Bumped the deal-sweep pipeline from `claude-sonnet-4-5` → `claude-sonnet-5`:
`DEAL_ENGINE_SWEEP_MODEL` + `DEAL_ENGINE_SCORING_MODEL` (render_taskdef), `_FRONTIER_DEFAULT`
(deal_engine_sweep), `MASE_VERDICT_MODEL` default (deal_engine_verdict), `DAY_SUMMARY_MODEL`
(day_summary_ai).

**Why.** The model routinely under-reported `evidence_coverage.calls_read` (wrote 0 even when handed
11 transcripts — Bright Horizons: `DL-DIAG … read=11` but the LLM emitted `calls_read=0`). That
mislabelled a fully-read deal as "dark" and fed spurious retry / thin / self-heal logic — the root of
the 9 forecasted deals that never completed a fresh sweep. A read count is a **fact**; it must come
from the engine, not the model. Sonnet 5 is the latest Sonnet (verified 200 on the prod key).

**How to work with it going forward.** `calls_read` can be trusted downstream now. If a deal still
reads dark, check the `[DL-DIAG] … read=N` log line (the engine truth), not the model's self-report.
Model is reversible via the `DEAL_ENGINE_SWEEP_MODEL` env.

---

## 2026-07-06 — CEO-ask names the VP; 24h summary is prose not a metadata dump

**What.** Three feedback fixes. (1) **CEO watch `ceo_ask` now addresses the VP**, not the BDR — the
CEO talks to his VP (the owner's manager), who owns the rep's book. Reframed existing records
(`patch_ceo_ask_vp.py`, 28 deals: "Ask Grace…" → "Ask Michael McCarthy (VP over Grace Kim)…") and
made it durable (`ceo_attention_fetch.py` adds `vp` to the pack; `gen_attention_wf.py` judge rule
directs the ask at the VP). (2) **24h summary reads as a real summary**: removed the raw
`Next_Step__c` wall (a months-long log, not a 24h item) from `DealDaySummary.tsx`, stripped
`[Clari - Email Sent]` metadata prefixes from activity subjects, and rewrote `deterministic_summary`
into plain-English prose ("Tanmay sent an email — Re: … (Jul 04)"); regenerated 74 stored rows from
their structured data (`regen_daysummaries.py`, $0, no SF pull). (3) **Diagnosed** the "todo push
didn't re-sweep": by design — the CDC bridge cost-guard (2026-07-02) filters Task/activity events
(`CDC_TRIGGER_ON_ACTIVITY` off); only Opportunity Stage/Amount/CloseDate/Next_Step__c changes trigger
a (paid-API) re-sweep.

**How to work with it.** CEO-ask + prose-summary fixes are already applied to prod data. Frontend
(`DealDaySummary.tsx`) needs a deploy to show the Next-Step-removal + subject-cleaning. To re-enable
todo-push re-sweeps, either set `CDC_TRIGGER_ON_ACTIVITY=true` (blunt, re-adds the paid-API burn) or
trigger a targeted re-analysis from the todo-push path — decide deliberately (it hits the paid API,
not $0 Claude Code).

## 2026-07-06 — Deal-quality tweak pass: specific + risk-inclusive score reasons, alignment, scope-shrink, second-panel, 24h last-active-day

**What.** A set of surgical tweaks ON TOP of the working sweep (the ~80% stays untouched), from a
deal-quality review across Publicis / Bandhan / Consumer Cellular / Fortive / SARS / Sabic / Omnia /
Techtronic / Austrian Post / Bosch:
- **Reasons are no longer robotic.** `build_cro_panel` now weaves the sweep's rich per-factor
  NARRATIVE into EVERY win bullet (economic_buyer / competition / paper_process narratives,
  competitive & champion summaries, customer_preference) — not just champion — so a bullet reads
  "Buyer leaning our way — Nishan said we're 1st on product assessment; only blocker is FSI-India
  experience (24 Jun)" instead of a bare label. The top **risks are folded INTO the win block**
  (user: "win position should include the risks, don't make a different column"), the win math is
  labelled **"Why this number"**, and the panel `intro` + bullets now also accept a sweep-authored
  `ai.deal_scores_evidence.{summary,ai_reasons}` verbatim.
- **Score ↔ reasons alignment (SARS).** `_crm_evidence_overlay` no longer maxes preference /
  differentiation / champion / commercial back up on a keyword hit when the sweep scored them
  weak/negative — killing "We're ahead (70)" sitting next to reasons that describe losing.
- **Scope-shrink (Techtronic).** `ai.scope_change.direction=="reduced"` drags Win a fixed ~7 pts
  (`scope_reduced` contribution) and raises a native **CEO-monitor watch** (`type:"scope_shrink"`,
  large ≥ $250K = high) — framed as buyer-defensive (cost / phased implementation).
- **Second-panel (Fortive).** `ai.expansion_context.prior_closed_won` floors exec_access so an
  expansion into an already-won account is never scored "no executive access".
- **24h summary (Publicis).** `DealDaySummary.tsx` falls back to the most recent `has_activity=true`
  day (with its date + a banner) when today's window is empty — instead of a bare "nothing happened".
  Summaries were already persisted per-day + searchable by date (`deal_daily_summaries`); this just
  surfaces the last active day.
- **Sweep prompt (§2.10, +6.2K chars, dry-run in `apply_reason_quality_tweaks.py`).** Mandates
  deal-specific, risk-inclusive, "why-this-number" reasons; an **email-trail parsing pipeline**
  (segment on headers → four-field → strip noise → two-bucket latest-development + unresolved-open →
  owner/next-action) that also fixes the **Omnia SOW** contradiction; **economic-buyer inference**
  from conversation (Austrian Post); **"last conversation" includes email** (Bosch); emission of the
  `scope_change` / `expansion_context` / `deal_scores_evidence` signals the server now reads; and a
  plain-English **"do nothing"** explanation (Publicis).

- **Reasons never speak the scoring machinery (user-directed).** The deal reason describes the DEAL
  only — who's engaged, what's proven, what's at risk. The stage cap still limits the *number*
  internally but is never spoken: removed the "how it adds up / Shortlisted caps confidence near 70 /
  anchors near X" line, added a render-time `_scrub_score_logic` in `build_cro_panel` that strips any
  score-logic clause from the sweep's `deal_scores_evidence` reasons (43/59 fresh reasons had leaked
  it), and patched §2.10 (`fix_reason_prompt.py`) so future sweeps don't write it. All 449 panels
  re-rendered clean.

**Why.** CMO/VPs kept asking "why is this score high/low?" and having to open Ask-AI. The reasons
were textbook labels with no deal-specific detail and hid the risks; some scores contradicted their
own reasons; scope cuts and second-panel access were mis-read. These close that gap without
disturbing the deterministic 5-score model.

**How to work with it going forward.** Backend (`deal_engine_cro.py`, `deal_engine_scoring.py`,
`deal_engine_ceo.py`) + tests (`tests/test_deal_reasons.py`, all pass; the 6 pre-existing
`test_deal_scoring.py` failures are stale old-flat-50-model tests, unaffected). New scoring/CEO paths
are **guarded no-ops until the sweep emits the new signals** — safe before deploy. To activate: apply
the prompt (`python apply_reason_quality_tweaks.py --apply`, idempotent, backup saved) → deploy
backend + frontend → re-run the $0 CC sweep (or a `cro_panel` backfill) so panels regenerate. The
narrative-per-factor bullets already work off the EXISTING meddpicc narratives (no new sweep needed
for that part — just a panel rebuild).

## 2026-07-06 — Zycus contracting knowledge fed into the sweep prompt (terms + how we operate)

**What.** Added Zycus's new-business contracting paper-trail + terminology to the analysis so
contracting-stage deals are read correctly. Full reference: `docs/zycus-contracting-reference.md`;
durable note: `.agents/memory/zycus-contracting-glossary.md`; distilled block appended to the LIVE
`mase_deal_sweep` Supabase prompt (§2.9, +2.9K chars, backup saved outside the repo). Key rules the
sweep now applies: (1) **Contract In Progress is NOT one gate** — four independent tracks (legal /
infosec+compliance / supplier-onboarding / signature) resolve separately, so name WHICH gate a
stall is on; (2) **the SOW is the choke point + signature predictor** — buyers agree MSA+Order Form
but won't sign until the SOW closes (signed separately by AVP Delivery), so "won't sign until SOW"
is NORMAL; (3) **PO is region-conditional** — no PO in much of Europe/US is normal, don't flag it;
(4) glossary — BAFO/LOI/MSA/OF1-2/SOW/DPA/GDPR+TOM/SOC 1-2/T4C/framework+call-off/AIGC/Zycus SO Form;
new-business only (single-module Certinal-only = Order Form + SOW, no MSA).

**Why.** The system needs Zycus's language + operating model so it stops mis-reading normal
contracting signals (a Europe "no PO" or a "won't sign until SOW") as red flags, and can tell
"in contracting" apart from "blocked on one specific gate."

**How to work with it going forward.** Prompt lives in Supabase (source of truth); re-apply via
`apply_contracting_knowledge.py --apply` (idempotent). Takes effect on the next sweep — no deploy.
To give the deal-chat / CEO-attention agents the same knowledge, append the block to
`mase_chat_agent` / the judge prompt. Code-level stage-modelling (sub-stage Contract-In-Progress,
SOW-status close predictor, PO-optional gate) is a larger follow-up, not done yet.

## 2026-07-05 — Kill the nightly `scheduled_discovery` / `scheduled_reconcile` burn

**What.** Set `DEAL_ENGINE_DISCOVERY_ENABLED=false` in the durable API env
(`.github/deploy/render_taskdef.py`). This gates **sub-job D of `_run_nightly_sf_pull`**
(server.py:6099) — the ONLY code that produces the `scheduled_discovery` (discover + sweep new
opps) and `scheduled_reconcile` (book membership reconcile) run sources.

**Why.** The nightly kept firing ~00:00 UTC and running ~50 paid AI sweeps/night
(`scheduled_discovery` 00:09–02:28 UTC on 2026-07-05, and again 2026-07-04) even though its
in-process scheduler is default-off (`SF_PULL_CRON_ENABLED` unset) AND the `/cron/nightly-sf-pull`
endpoint is gated — i.e. it's being invoked by a path we haven't pinned. Rather than chase the
invoker, gate the innermost block that emits those two sources, so it stops regardless of how the
nightly is reached. `scheduled_*` come from exactly one place (server.py:6116/6106), both inside
this flag's `if`.

**How to work with it going forward.** Manual discovery via `POST /api/deal-engine/discover-new`
is UNAFFECTED (it doesn't read this flag; it uses `source="manual_discovery"`). Sub-jobs A/B/C of
the nightly (deterministic SF/cache refresh, no AI sweeps) are unchanged. To re-enable nightly
deal-engine discovery, remove the `DEAL_ENGINE_DISCOVERY_ENABLED` line from `render_taskdef.py`.
Takes effect on the next CI deploy (env is baked into the api task-def).

## 2026-07-05 — Reliable sweep source label (Salesforce triggers no longer mislabeled `worker`)

**What.** The sweep worker now derives a run's `source` from the **authoritative** queue
`run_id`: if the claimed row reaches `worker.py._process` without a `run_id` (or with one
lacking a known origin prefix), it re-reads the run_id from the row via new
`sweep_queue.get_run_id(opp_id)` before mapping the prefix → source. It also logs the
decision: `[SWEEP-WORKER] source label opp=… run_id=… -> …`.

**Why.** Investigation (2026-07-05, RHB_S2P_2026 traced through the CDC Lambda + ECS logs):
**100% of recent `worker`-labeled runs were actually Salesforce triggers** — their queue rows
were `sftrig-…` (e.g. RHB fired on a real `Next_Step__c` change → CDC → HTTP 202). The label
is derived from the run_id prefix at drain time, and a claimed row occasionally arrived without
its run_id, so the derivation fell through to the generic `worker`. This made the dashboard
under-report `salesforce_trigger` and over-report `worker`. The retry/reclaim path was ruled
out (it preserves run_id); genuine book sweeps (bare-uuid run_id, correctly `worker`) stopped
2026-07-01. Only the labeling was wrong — no extra sweeps were run by this bug.

**How to work with it going forward.** Correctly-prefixed rows are unchanged (no extra DB read).
Genuine full-book sweeps still read `worker` (bare-uuid run_id, no prefix — expected). Watch the
new `source label …` worker log after deploy: the next Salesforce-triggered sweep should print
`-> salesforce_trigger`. NOTE: the **3× thin-retry** cost (one SF change can bill up to 3 sweeps)
and the still-firing nightly `scheduled_discovery` are SEPARATE issues, not addressed here.

## 2026-07-03 — CRO panel win bullets carry `full` text → "more" expander

**What.** `deal_engine_cro.build_cro_panel` now attaches an optional `full` field to a
win bullet whose `text` was clipped (the champion narrative + the champion/competitive
fallback, clipped by `_first_sentence(...,150/170)`). The frontend `CroBullets` renders a
"more"/"less" toggle when `bullet.full` differs from `bullet.text`, expanding the full
narrative in place (which often names the competitor / the multi-thread targets — e.g.
Allstate's champion bullet expands to reveal the "SAP Ariba beta" risk + Mohit Bothra /
Casey McDowell). Backfilled `ai.deal_scores.cro_panel` across all active deals
(`cro_panel_backfill.py`, pure/no-LLM, 402 panels; 52 gained a `full` bullet).

**Why.** The win-position "Scores & Reasons" bullets clipped a long champion narrative to a
cryptic "…and…" with no way to read the rest.

**How to work with it going forward.** `full` is only set when the source was actually
clipped. Extendable to other blocks (risk/momentum) by carrying `full` at their
`_first_sentence` sites the same way. Frontend change is in the MASE frontend repo.

## 2026-07-03 — CEO attention UNIFIED into one watchlist (support is a reason type)

**What.** Collapsed the earlier Support/Monitor split into ONE determination. `ai.ceo_intervention`
is now `{needed, severity, needs_action, reasons[], win, mom, source, generated_at}` — a single CEO
watchlist. `support` (the CEO must ACT — pricing/product/presales_resources/exec_connect) is just one
reason `type` inside `reasons[]`, auto-included alongside the WATCH reasons (`our_slip`,
`large_slowdown`, `competitor_edge`). Each reason: `{type, act, severity, summary, evidence, as_of,
+ceo_action/areas/buyer_target for support}`. `needed = len(reasons)>0`; `needs_action = any support
reason`. No more separate support/monitor objects, no "both". `deal_engine_ceo.finalize_ceo_intervention`
computes the support reason and CARRIES THE WATCH REASONS FORWARD from the prior record (owned by the
14-day `ceo_attention` run); frontend renders ONE "🔎 CEO monitor" banner listing all reasons with
act-items highlighted.

**Why.** Two parallel lists (CEO support + CEO monitor) were redundant — a CEO wants ONE list of deals
needing his attention, where each entry says whether he must act or just watch. In practice every
support deal was also on the monitor list, so the split added confusion, not signal.

**How to work with it going forward.** `ceo_attention_apply.build_attention` merges verdicts into the
unified shape; re-runnable local/$0 over win>=40. Old `.support`/`.monitor` records are read via a
fallback but overwritten on the next run/sweep. (Superseded the same-day Support+Monitor entry below.)

## 2026-07-03 — CEO attention = Support + Monitor (ai.ceo_intervention restructured, SUPERSEDED)

**What.** `ai.ceo_intervention` now carries TWO sub-determinations instead of one flat
"intervention": `support` (the CEO must ACT — the existing 4-lever discriminator:
pricing/product/presales_resources/exec_connect) and `monitor` (the CEO should WATCH).
New shape: `{needed, kind: support|monitor|both|none, support{…}, monitor{needed, reason,
triggers[]}, win, mom, source, generated_at}`. `needed` (top-level) is `support.needed OR
monitor.needed` so the existing column keeps working. `deal_engine_ceo.finalize_ceo_intervention`
computes `support` (from the sweep's own output; reads both the new nested and legacy flat
model emit) and **carries `monitor` forward from the prior record** — the sweep NEVER
computes or clobbers monitor. Monitor is owned by a SEPARATE, 14-day-surgical
`ceo_attention` run (`ceo_attention_fetch.py` → judge workflow → `ceo_attention_apply.py`):
three triggers — **our_slip** (a deliverable the prospect expected from us is still
outstanding and it's NOT buyer-dependent — buyer waits are softened), **large_slowdown**
(amount ≥ $250K or forecasted, and slowing / disengaging), **competitor_edge** (a competitor
out-delivering us, surfaced from our interactions). Eligibility floor = `win_position >= 40`.
Every monitor trigger MUST be anchored to a signal dated within the **last 14 days**
(hard deterministic gate in apply drops any trigger whose `as_of` is stale — "don't fetch
historical SF and let it explode").

**Why.** CEO involvement split into two needs: *intervention* (act/veto — existing) vs
*oversight* (watch — new: gauge that WE are slipping on a deliverable, a big deal is going
quiet, or a competitor is out-executing). The oversight signal must be surgical and recent
(≤14 days) or it misleads.

**How to work with it going forward.** The `ceo_attention` run is standalone/local ($0),
re-runnable over all win≥40 opps; it writes `source:"attention_v1"`. A CDC sweep refreshes
`support` and preserves `monitor` (carry-forward). Frontend reads `ceo_intervention.support.*`
and `ceo_intervention.monitor.*`. Requires a backend deploy for the sweep carry-forward.

## 2026-07-03 — Win score: competitor drag only on a BUYER-LEANING signal (not mere presence)

**What.** `deal_engine_scoring._competitive_strength` and the `_signals` competitive-posture
block no longer apply the competitive WIN penalty just because a credible competitor is
present / high `threat_level`. The full drag (rubric strength −1.0, i.e. the ~−30 band hit,
plus the `competitor_preferred` risk signal and the −0.4 posture) now fires ONLY when the
BUYER is leaning toward a competitor — new `_buyer_leans_competitor()` gates on a
preference / down-select signal in `status` (preferred / ahead / incumbent / winning /
leading / selected / frontrunner / favored / chosen / recommended / down-selected), an
explicit `preferred:true` / `buyer_leaning:true` flag, or a leaning phrase in `sentiment` /
`buyer_preference`. A credible rival merely present in an active eval now scores +0.2
("roughly even") and is surfaced as the neutral `open_competitive_rfp` signal, not a loss.
`threat_level` alone NEVER triggers the drag (it measures how dangerous a rival could be,
not whether the buyer prefers them).

**Why.** Reps flagged that Win dropped ~30 points whenever a competitor was merely
*mentioned* — Coupa/SAP present in a normal competitive RFP tanked winnable deals even
with zero signal the buyer favored them. Win should reflect "can we win it," and a named
competitor in an eval is expected, not a losing signal; only a buyer leaning toward /
about to buy a competitor should lower Win.

**How to work with it going forward.** Shared constant `_COMPETITOR_LEANING` + helper
`_buyer_leans_competitor(c)` in `deal_engine_scoring.py`; used by both the win rubric
(`_competitive_strength`) and the signal scan (`_signals`). Momentum/risk unaffected except
that `competitor_preferred` now only fires on a genuine leaning. Takes effect on the next
sweep (deterministic scorer). If the AI deal-scorer is enabled it judges Win from evidence
directly and is unaffected.

## 2026-07-03 — CEO help computed NATIVELY in the sweep (win>60 FLOOR + AI discriminator, 4 levers, sanitized)

**What.** `ai.ceo_intervention` is now produced on EVERY sweep instead of a separate
local pass (new `deal_engine_ceo.finalize_ceo_intervention`, wired into
`deal_engine_sweep.analyze_one` at the persist chokepoint, replacing the old
carry-forward-only block). Two-part decision: (1) a deterministic **eligibility FLOOR**
— `win_position >= 40` for **ANY deal** (forecast category NOT gated; momentum NOT gated —
a winnable-but-stalling deal is when the CEO may be needed). Clearing the floor does NOT
tag the CEO. (Floor lowered 60 → 40 on 2026-07-03, user-directed.) (2) The **real filter is an AI analysis** — for each eligible deal the
model decides whether the CEO is GENUINELY, SPECIFICALLY required vs. no intervention
or only a senior/C-level exec (VP/SVP/CRO/CMO); DEFAULT is needed=false. The finalizer
RESPECTS that decision (never forces needed=true on a floor-pass) — so only the few
deals where the CEO is irreplaceable get tagged. The **content rides the sweep's LLM
output** (a new
prompt section has the model emit its CEO read — no extra API call). The finalizer
overrides `needed` from the gate, clamps `areas` to the four CEO levers (pricing /
product / presales_resources / exec_connect), stamps real win/mom + `source:"sweep"`,
and **sanitizes** `ceo_action`/`reason` with the title/name guardrails + verifies
`buyer_target` against Salesforce (repairs an unbacked name to the MEDDPICC economic
buyer, else null+role). On any failure it falls back to carrying the prior value
forward, so a re-sweep never drops a good read.

**Why.** CEO help was (1) a separate manual workflow, not native, and (2) conflated
"executive help" with "CEO help" — it drifted into "send a VP/SVP/delivery leader."
Now it is CEO-ONLY (the action is what the CEO personally does) and computed each run.
Apollo/ZoomInfo are NOT used — buyer authority comes from Salesforce contact roles.

**How to work with it going forward.** Levers + gate live in `deal_engine_ceo.py`
(`LEVERS`, `WIN_BAR`/`MOM_BAR`); prompt emit-rules in the Supabase `mase_deal_sweep`
prompt (CEO help section). `source:"sweep"` marks a natively-computed record; older
`workflow_v2`/`v3` records are overwritten on their next sweep. Requires a backend
deploy + the prompt section. Unit-tested (`test_ceo_native.py`).

## 2026-07-02 — Add `Next_Step__c` to CDC meaningful fields (custom next-step field, from live data)

**What.** The CDC Lambda's default `MEANINGFUL_FIELDS` is now
`{StageName, Amount, CloseDate, Next_Step__c, NextStep}` — added the org's **custom**
next-step field `Next_Step__c` (kept standard `NextStep` for safety).

**Why.** Verified against live data: this org stores the deal's next step in the custom
field `Next_Step__c`, not standard `NextStep` (confirmed rep-edited on opp
`006P700000S00xaIAB` — FCC / Farm Credit Canada, editor Bailey Erazo — and present in
~35% of CDC events). The prior default targeted only `NextStep`, so real next-step edits
were filtered as noise. A rep advancing the next step is a genuine deal signal and should
re-sweep; the 6h per-opp cooldown collapses edit-bursts (the field's history is
polling-duplicated at the same timestamp) so this does not reintroduce the burn.

**Field taxonomy (from 62 live CDC events + one opp's full history).** Meaningful =
StageName / Amount / CloseDate (standard, carry real old→new values) + Next_Step__c.
Deliberately EXCLUDED as noise: `Revenue__c`, `VIBE_Influenced__c`, `QIT_Count__c` /
`Last_QIT_Name__c`, `Discount__c`, `Division__c` (territory reassignment), `Buyer_Journey__c`,
`Shortlist_to_Won_Score__c`, `BD_Manager__c` / `Manager_BD__c`, `Event_Source__c` /
`Opportunity_Source__c`, and the next-step METADATA companions `Next_Step_History__c` /
`Next_Step_Updated_By__c` / `Next_Step_Updated_Date_Time__c` (automation churn; exact-match
so they never collide with `Next_Step__c`). A deal CLOSING is already caught by `StageName`.

**How to work with it.** Still env-tunable (`MEANINGFUL_FIELDS` on the Lambda). Lambda deploys
SEPARATELY: `aws lambda update-function-code --function-name mase-sf-cdc-bridge` (done +
verified live 2026-07-02: `Next_Step__c` edit passes the gate; metadata-only churn → filtered).

## 2026-07-02 — Competitor gate: unanchored active rivals are rejected (kills inferred-GEP)

**What.** New deterministic anti-fabrication check `Part 6` in `deal_engine_validation.py`,
wired into the persist chokepoint. An entry in `ai.competitive_position.competitors` marked
`status: active` must have its NAME traceable to evidence the server holds — the combined
Salesforce competition text (`Competitors__c` + `Others_Competitors_Please_specify__c`,
plus `Next_Step__c`) OR the entry's OWN verbatim buyer `quote` (which the sweep prompt rule
d.1 already requires to be the buyer talking about that competitor). A name found in NEITHER
is unanchored and is rejected. Three surfaces enforce it: `validate_record` (new `competitor`
violation → retry with feedback), `sanitize_failed_record` (`sanitize_competitors` drops the
entry on retry-exhaustion so the record persists clean), and `sanitize_packets` (drops a
carried-forward/this-sweep `competitor` packet so it can never re-project into `ai`).

**Why.** The gate verified person NAMES but never competitor names, so the model could invent
a rival to "fill a shortlist" and it sailed through — the Austrian Post case: GEP was named in
NO call and NO field, inferred from a "top 4 suppliers" line in `Next_Step__c`, then rated an
active **medium** threat. A fabricated competitor is not cosmetic: a high/preferred rival costs
−1.0 on the win score (`deal_engine_scoring.py`) and misleads the rep about live competition.

**How to work with it going forward.** Deliberately high-precision so it never guts a real
read: only `status: active` entries are policed; historical statuses (incumbent / declined /
faded / do_nothing) are EXEMPT (the prompt keeps priced-out/displaced rivals as durable
history, and incumbents often live only in server-invisible fields). A competitor named only
on an Avoma call survives via its verbatim quote. Matching is generous (full-name or ≥3-char
token, either direction) so "GEP SMART" matches a corpus "GEP"; a too-generous match only
KEEPS a competitor (the safe direction). Pure/no-network; unit-tested in
`test_sweep_validation_gate.py` (7 new cases). NOTE: the sibling fix — stripping real vendor
names (GEP/Coupa/Proactis) from the **sweep prompt's own examples** in Supabase, which is what
primes the model to invent GEP in the first place — is NOT in this change and still stands.

---

## 2026-07-02 — Title gate: stakeholder titles are server-owned (kills "CFO Flandorfer")

**What.** New deterministic anti-fabrication pass `sanitize_title_claims` in
`deal_engine_validation.py` (Part 5), wired into `deal_engine_sweep.analyze_one` at the
persist chokepoint (right after `sanitize_meddpicc`). It neutralises any executive /
economic-authority title the model pins on a name that Salesforce cannot back: for a
C-suite claim ("CFO/CEO/CIO/CPO/… <Name>", "<Name> (CFO)") the named person must be an
`OpportunityContactRole` contact whose `Contact.Title` carries compatible evidence
(claim `cfo` ⇒ Title contains "finance"/"cfo"/…); for a role assignment ("economic
buyer/decision maker <Name>") the name must at least be a known contact/attendee.
Otherwise the unbacked **title is dropped and the real name is kept** ("the
economic-buyer CFO Flandorfer" → "Flandorfer"). Covers moves, requirements, MEDDPICC
narratives, `competitive_position.summary`, and `north_star_verdict`. Verbatim evidence
(sources/quotes) is never touched. `build_contact_titles(buyer)` builds the authoritative
`{name: Contact.Title}` map and also accepts enrichment-verified titles (Apollo/ZoomInfo)
so a future layer can *correct* a title instead of dropping it.

**Why.** The gate verified person NAMES (allowlist) but never their TITLE, so the model
could attach a fabricated exec title to a real name and it sailed through — e.g. Austrian
Post's "the economic-buyer **CFO Flandorfer** has never engaged" when Salesforce/Apollo
show Mathias Flandorfer is the **Deputy CPO** and the CFO is Barbara Potisk-Eibensteiner.
A wrong exec title is uniquely harmful: it sends a rep escalating to the wrong person, and
the CEO-help pass inherited it verbatim. Titles are now server-owned like `manager_name`.

**How to work with it going forward.** Pure/no-network; unit-tested in `test_title_claims.py`
(7 cases). Runs on every sweep — logs `[DEAL-SWEEP] title-gate opp=… neutralised N …`.
Existing records keep their old titles until re-swept (or run a one-off backfill applying
`sanitize_title_claims` over stored `record.ai`). Requires a backend deploy to take effect.
To *correct* (not just drop) titles, union Apollo/ZoomInfo `{name:title}` into
`build_contact_titles`. The `sanitize_failed_record` last-resort path does not call it yet
(main path covers every successful sweep).

## 2026-07-02 — CDC brakes: meaningful-field filter (Lambda) + per-opp sweep cooldown (backend)

**What.** Two changes so the Salesforce CDC trigger can deliver real-time sweeps WITHOUT the
re-sweep storm:
1. **CDC Lambda** (`infra/sf-cdc-bridge/lambda_function.py`) fires a paid sweep only on a
   *meaningful* Opportunity change. Task/Event/EmailMessage **activity no longer triggers** (set
   env `CDC_TRIGGER_ON_ACTIVITY=true` to restore). An Opportunity UPDATE triggers only when a field
   in `MEANINGFUL_FIELDS` changed (default `StageName,Amount,CloseDate,NextStep`; env-tunable, and
   checked against `changedFields` + `nulledFields` + present payload values). CREATE/UNDELETE and
   untracked-but-Qualified+ adoption still trigger, so genuinely new deals still appear.
2. **Backend** (`enqueue_trigger`, `deal_engine_sweep.py`) adds a per-opp cooldown: a
   Salesforce-triggered re-sweep returns `skipped_cooldown` if the opp was swept within
   `DEAL_SWEEP_TRIGGER_COOLDOWN_HOURS` (default 6), read from the record's `swept_at`. **Manual
   clicks and from-scratch rebuilds bypass the cooldown** — explicit human intent always runs now.

**Why.** The 2026-07-02 burn (~$1,157/48h) was the CDC path with no brakes: the Lambda fired on
every activity (not just field changes) and nothing debounced repeat triggers → 829 sweeps over
198 opps, 76% repeat (~$894), one deal 17×. The filter removes the activity volume; the cooldown
collapses meaningful-change bursts. Together they make re-enabling the CDC rule safe.

**How to work with it going forward.** All three knobs are env-tunable without code:
`MEANINGFUL_FIELDS`, `CDC_TRIGGER_ON_ACTIVITY`, `DEAL_SWEEP_TRIGGER_COOLDOWN_HOURS`. The Lambda
deploys SEPARATELY from ECS — it is **not** in the GitHub Actions pipeline; ship it with
`aws lambda update-function-code --function-name mase-sf-cdc-bridge`. Only after BOTH are live,
re-enable real-time: worker `--desired-count 1` + `aws events enable-rule --name
mase-sf-cdc-to-lambda`. Verified: 10-case Lambda filter behavioral test + IST `swept_at` parsing;
both files `py_compile`. See `docs/sweep-runs-kt.md`.

## 2026-07-02 — Close the `/cron/nightly-sf-pull` side-door (SFDC CDC is the only automated sweep trigger)

**What.** `GET /cron/nightly-sf-pull` now returns `{"status":"disabled"}` (HTTP 200, no work)
unless `SF_PULL_CRON_ENABLED` is true — the same flag the in-process `_nightly_sf_pull_scheduler`
already respects (default off). Previously the endpoint ran the full combined nightly pull
unconditionally, ignoring the flag.

**Why.** The endpoint was a side-door: an external cron kept invoking it even though the nightly
scheduler is disabled by default, so sub-job (D) fired **paid AI sweeps** — `reconcile_membership`
(`source=scheduled_reconcile`) + `discover_and_sweep_new` (`source=scheduled_discovery`). Intent:
the **only** automated trigger that spends on AI sweeps is the Salesforce CDC path
(`salesforce_trigger` → `/api/deal-engine/sweep/trigger`). The two cheap, no-AI cache endpoints
(`/cron/sync-sf-to-cache`, `/cron/sf-pull-refresh`) are intentionally left open — killing them
would just push hard-fact refresh back onto expensive AI sweeps.

**How to work with it going forward.** Real-time SFDC sweeps still flow via CDC and manual clicks
still work — both unchanged. To deliberately re-run the nightly combined pull, set
`SF_PULL_CRON_ENABLED=true` (re-arms both the endpoint and the scheduler). **NOTE:** this does NOT
address the dominant burn (CDC fires on every Task/Event/EmailMessage with no per-opp cooldown);
that remains a separate fix (per-opp cooldown in `enqueue_trigger` + meaningful-field CDC filter
in `lambda_function.py`). The CDC EventBridge rule `mase-sf-cdc-to-lambda` and the worker fleet
are controlled at the AWS level, not by this code. See `docs/sweep-runs-kt.md`.

## 2026-07-02 — CEO-intervention flag ("CEO help needed") per deal

**What.** A new per-deal field `ai.ceo_intervention` = `{needed, priority, areas[], reason,
ceo_action, win, mom, source, generated_at}`. `areas` ⊆ {pricing, product, presales_resources,
exec_connect}. Backend: `slim_record` now keeps `ceo_intervention` so the Deals LIST/column can
read + filter it without loading the full record; `analyze_one` carries it forward on re-sweep
(it is written by a separate pass, not computed in the sweep yet).

**Why.** The CEO can move deals via pricing approval, product/roadmap commitment, pre-sales/
resource allocation, and exec-to-exec connects. Rule: **CEO should be involved when
win_position > 60 AND deal_momentum > 60.** The UI needs a filterable "CEO help" column.

**How it's computed (for now).** NOT in the sweep and NOT via the AI scorer. A separate
Claude-Code workflow (`ceo-help-judge`) gates on win>60 & mom>60, then judges the areas +
reason + concrete `ceo_action` per gate-passing deal, and a scoped `jsonb_set` stamps
`ai.ceo_intervention` on the deal_records rows. First run (2026-07-02): all 122 forecasted
deals — 6 gate-passers get needed=true (ARUP, Austrian Post, Robert Bosch, Domino's, Consumer
Cellular, McAfee), 116 needed=false. Later this can move into the sweep.

**How to work with it going forward.** Re-run the `ceo-help-judge` workflow + apply pass to
refresh (or extend beyond forecasted). Frontend renders a "CEO help" column + filter mirroring
the AI-excitement (`ai_fit_signal`) pattern. Requires the backend deploy for the list payload
to include the field.

## 2026-07-02 — Pin guard, verbatim AI reasons, never-blank stakeholder role

**What.** Three changes so automated sweeps stop clobbering corrected deals and the
Scores/Stakeholders UI reads right:
1. **Pin guard** (`deal_engine_sweep.py` `analyze_one`): if the stored record has
   `ai.pinned == true`, the sweep carries the prior `deal_scores` (headline + cro_panel)
   and `stakeholder_map` forward **verbatim** — a human correction is frozen against
   re-sweeps; hard facts (stage/amount/dates) still refresh. The pin is re-stamped so it
   survives the upsert (which replaces `record`).
2. **CRO panel uses AI reasons verbatim** (`deal_engine_cro.py` `build_cro_panel`): when
   `deal_scores.ai_reasons` exists, each score block uses those bullets as-is instead of
   re-deriving from `contributions` and hard-trimming through `_first_sentence(…,150)`
   (which chopped them mid-word: "…advocate for Zycus as the…"). New `_ai_bullets(key)`
   helper. Deterministic deals (no `ai_reasons`) are unchanged.
3. **Never-blank stakeholder role + email/phone** (`_roster_from_sfdc._sfdc_item`): SFDC
   contacts carry `email`/`phone`; when neither the AI nor SFDC `OpportunityContactRole.Role`
   gives a role (senior-by-title backfill contacts had none), a conservative title-based
   `_role_from_title` fills it (flagged `_role_inferred`). AI/SFDC role always wins.

**Why.** Austrian Post kept reverting: the CDC trigger fires on every activity and each
re-sweep recomputed + overwrote the hand-applied score + curated roster (the sweep is
stateless/destructive with no "corrected" marker). AI reason bullets rendered mid-word
truncated. Backfilled stakeholders showed a blank Role column. (The `worker`→real-source
audit label was already fixed on main in `7aebccd`.)

**How to work with it going forward.** Set `ai.pinned = true` on a record after a manual
correction to freeze it; clear it to let sweeps own the deal again. Non-pinned deals behave
exactly as before. Requires a backend deploy. The CDC EventBridge rule
`mase-sf-cdc-to-lambda` is currently paused (no cooldown chosen); re-enable when ready.

## 2026-06-30 — Sweep model → Opus 4.8 + cleaner win reasons

**What.** (1) `DEAL_ENGINE_SWEEP_MODEL=anthropic:claude-opus-4-8` set in `render_taskdef.py`
(the durable task-def env source of truth) — the deal sweep/analysis now runs on Claude Opus
4.8 instead of the hard-pinned Sonnet 4.5 default, for deeper, more accurate reads. (2)
`deal_engine_cro` win bullets no longer surface cryptic Next-Step keyword fragments
("Differentiated where it counts — pain point"); a keyword-only hit now shows the clean label
alone, and the champion bullet pulls the rich `champion_strength.summary` narrative.

**Why.** Reps flagged the win reasons as "full of mistakes" — the fragments ("— preferred
partner", "— economic buyer") read as filler. And Sonnet 4.5 was leaving qualitative analysis
(summaries, competitive read) weaker than Opus can do.

**How to work with it going forward.** Reversible: drop the `DEAL_ENGINE_SWEEP_MODEL` line (or
set another `anthropic:` model) to change the sweep model; per-deal cost rises ~3-5× on Opus.
`DEAL_SWEEP_MAX_TOKENS` stays 64K (Opus 4.8 caps at 128K, so it's in range).

## 2026-06-30 — Meeting count now sourced from the DATALAKE (real Avoma meetings)

**What.** `_footprints_for` now feeds the deal's **datalake Avoma meetings** (the
`_avoma_pf` manifest — already matched by opp/account/buyer-domain) into `derive_footprints`
as `meeting_dates`, so `meetings_60d` is the count of REAL meetings (deduped by day), not a
guess from SF subject keywords. Verified against the datalake: Allstate 4→**0**, SABIC 55→**5-6**,
Metallus 10→**2**.

**Why.** The count was 100% deterministic SF-keyword matching and ignored the datalake entirely
— so emails with "POC" in the subject and 3×-logged sessions inflated it. The datalake holds the
authoritative meetings (dates, opp/account/domain links, internal/external flags).

**How to work with it going forward.** `deal_engine_sweep._footprints_for(... avoma_meeting_dates=)`
fed from `_avoma_pf["manifest"]`; combined with the email-exclusion + same-day dedupe in
`deal_engine_footprints`. Takes effect on the next sweep.

## 2026-06-30 — Footprints: emails no longer counted as meetings

**What.** `_meeting_task` matched loose keywords ("poc"/"demo"/"call with") in ANY subject — so
emails about a meeting counted as meetings. Allstate read "4 meetings in 60 days" that were all
4 *emails* with "POC" in the subject (real meeting count: 0). Now an email (Clari/Outreach sync,
`[Clari - Email …]`, `Email Sent/Received`, `[in]`/`[out]`, lemlist) never counts as a meeting,
on Tasks or Events; a meeting needs an explicit Meeting/Clari-Meeting/Avoma marker or an
unambiguous in-person session. Pairs with the same-day dedupe to make `meetings_60d` real.

**Why.** Inflated, misleading "meetings" on the UI; flagged on Allstate/SABIC/Metallus.

**How to work with it going forward.** `deal_engine_footprints._is_email` + `_meeting_task` in
`deal_engine_footprints.py`. Takes effect on the next sweep (footprints are re-derived from SF).

## 2026-06-30 — Footprints: dedupe meetings by day (fixes ~9× meeting over-count)

**What.** `derive_footprints` was appending EVERY Event + meeting-Task + Avoma date to the
meeting list, so the same session logged across Avoma + Clari + a Task counted 3×+ —
`meetings_60d` was ~9× inflated (Sabic: 55 meetings for 6 real). Meetings now dedupe by
calendar date, and engagement frequency (`events_30d`) counts distinct days, not raw rows.

**Why.** The inflated counts misrepresented engagement on the UI ("55 meetings in 60 days")
and fed the momentum frequency bump.

**How to work with it going forward.** `deal_engine_footprints.derive_footprints` — `meet_dts`
deduped by `.date()`; `n30` = distinct engagement-days. One meeting-day = one meeting (a minor
under-count if two genuinely distinct meetings fall on one day, vs the old 9× over-count).

## 2026-06-30 — Deal Momentum is now BI-DIRECTIONAL (one-sided outreach no longer inflates it)

**What.** `score_momentum_v2` (`deal_engine_scoring.py`) now gates its activity pillars
(engagement / next-step / milestone) by how much the BUYER is participating — buyer-side
touches in 30d, or a genuinely recent meeting (meetings are two-way). A high-depth event the
REP drove (a sent email, an old demo, an "unmet commitment") that the buyer hasn't engaged
with is scaled down (×0.2–0.6), a buyer who's gone dark (no buyer touch in 60d) takes a real
stall, and a close date that keeps sliding right (`opp_trends.close_date_trend`) now drags
momentum ("pushed out is a bad signal"). A `one_sided` contribution surfaces the discount so
the reasons say why.

**Why.** Momentum was scoring DEPTH regardless of direction: Allstate read 82 ("accelerating")
off a single rep-sent POC email with the buyer silent 22d; Cornell read 84 off an "RFI-not-sent"
unmet-commitment with 0 buyer touches in 60d and the close slid 126d. One-sided pitching isn't
momentum — bi-directional response is. Validated book-wide: the buyer-silent anomalies drop
(Cornell 84→44, Allstate 82→46, Amplifon 77→49, Swift 73→42) while genuinely two-way deals are
untouched (Publicis 91→88, Mair 88→88, McAfee 85→80 — only nudged by real date slips).

**How to work with it going forward.** Tunables are the `bidir` tiers + `dark_stall` (12) + the
close-date `push` cap (10) in `score_momentum_v2`. Reads `footprints.buyer_touches_30d/60d`,
`last_meeting`, and `opp_trends.close_date_trend`. Applies to the engagement_v2 model only
(footprints present); the signal-based fallback `score_momentum` is unchanged.

---

## 2026-06-30 — CRO-readable "Scores & reasons" panel (`deal_engine_cro`)

**What.** New `deal_engine_cro.build_cro_panel(record)` assembles a plain-English brief — one read
per score, ✅/⚠️ bullets, an honest "what could lose it" block (replaces the misleading "Risk 0"),
and the moves — and the sweep attaches it at `ai.deal_scores.cro_panel`. The frontend
(`components/deals/DealScores.tsx` → `DealReasonsPanel`) renders this narrative INSTEAD of the
maths breakdown when present, falling back to the old additive view otherwise. No LLM call: it
*selects and trims existing prose* the sweep already wrote (`competitive_position.summary`,
`vulnerabilities[].detail`, `champion_strength.summary`, `recommended_moves[].action`) plus
footprints/crm_evidence/trends. Two guards: the competitive guard never frames a do-nothing /
incumbent-inertia threat as "a competitor beating us" when we're preferred or won the eval; the
risk guard still surfaces the threat block even when `deal_risk == 0`.

**Why.** Reps won't do maths. The old panel showed numeric contribution breakdowns ("differentiation
strength +1.00 (weight 20)"); reps need the human reason ("buyer prefers Coupa — cheaper for the
same thing"). The prose already existed on the record; this just puts it on the UI.

**How to work with it going forward.** A hand-authored panel pinned with `cro_panel.pinned: true`
(Bright Horizons) is preserved verbatim across sweeps — the generator never overwrites a pinned
panel. To re-pin/edit, set the `cro_panel` object on the record and `pinned: true`. Cosmetic +
best-effort: a build failure logs `[CRO-PANEL]` and never blocks a sweep.

## 2026-06-30 — Datalake is now the DEFAULT Avoma source (activates the domain match + loss detector)

**What.** Flipped `DEAL_SWEEP_AVOMA_FROM_DATALAKE` default `false`→`true`. The domain-match +
loss-detector shipped yesterday live in the *datalake* prefetch, but prod still ran the *live*
Avoma path (flag was unset/false), so the fixes were DORMANT: HAVI re-swept with `calls_read=0`
(live association broken), came back thin, no scores, no loss detection. The datalake path holds
the whole call history AND matches opp_id OR account_id OR buyer attendee-DOMAIN, so it catches
the loss call (`crm_opportunity_id=null`). Verified the exact prod httpx query returns 9 calls
incl. the Jun-29 loss. Per-deal LIVE fallback remains when the datalake has nothing for an opp.

**Why.** Without this, the Avoma never-miss + decision-detector do nothing in prod. With it, HAVI
gets all 9 calls → footprints + scores compute → the loss detector fires → Win/Mom 0.

**How to work with it going forward.** Reversible instantly: set `DEAL_SWEEP_AVOMA_FROM_DATALAKE=false`.
Blast radius is the whole book (every sweep now reads Avoma from the datalake first) — watch for
deals where the datalake is unbackfilled (they fall back to live, same as before).

## 2026-06-29 — Never miss the latest call + LOSS detector hard-overrides to 0

**What.** Two trust-critical fixes after HAVI scored Win 70 / Mom 76 while it had **already
been lost to Coupa** on that day's "RFP decision announcement" call:
- **Avoma ingestion now matches opp_id OR account_id OR buyer attendee-DOMAIN.**
  `_avoma_prefetch_from_datalake` previously queried `crm_opportunity_id` only. The HAVI loss
  call had `crm_opportunity_id=null` AND a `crm_account_id` that didn't match the opp's account,
  so the single most decisive call was INVISIBLE. It now also matches the buyer email domain
  (e.g. `havi.com`) via `attendee_domains.cs.{}`. Verified against the live datalake: old match
  8 calls (missed the loss), new match 9 calls (loss call included). The caller passes `buyer`.
- **Decision-outcome detector → instant 0.** `_detect_decision_outcome` scans the 3 most recent
  call notes/transcripts + Next-Step for high-precision win/loss phrases (`runner-up`,
  `selected the competing vendor`, `lost to coupa`, …) and stamps `ai.decision_outcome`.
  `compute_deal_scores` HARD-OVERRIDES a detected loss to **Win 0 / Mom 0 / Cmt 0 / Risk 100 /
  read "Lost"** with a sourced reason — regardless of CRM stage or prior activity. (Loss only;
  a `won` flag is stored but does not force the score.)

**Why.** The data existed (transcript in the datalake 1h45m before the sweep) but the agent
never ingested it because the SF↔Avoma link was broken, then scored a dead deal as healthy.
A lost deal must read 0 the moment a call says so — not ride stale engagement.

**How to work with it going forward.** Loss phrases live in `_LOSS_PHRASES` (high-precision by
design — a false loss is costly; generic "other vendor" alone never triggers). Validated on the
real HAVI loss call: detector → lost; score → 0/0, reason cites the Jun-29 call. The domain
match also fixes the general "agent doesn't see the newest Avoma call" class of staleness.

## 2026-06-29 — Win rubric: deterministic Next-Step/narrative scan + PREFERENCE (playbook "next step")

**What.** Implements the playbook's stated next step: the deterministic Win-rubric overlay now
also keyword-scans the **Next-Step log + opp narrative** (Next_Step__c, Next_Step_History__c,
Description, Customer_Business_Problem__c, Compelling_Event__c), not just MEDDPICC 2.0. New
`_rubric_crm_scan` + `_rubric_text_scan` + `_merge_crm_evidence` (deal_engine_sweep.py) MAX-merge
the hits into `ai.crm_evidence`; one extra SOQL, fully try/except-wrapped (gated
`DEAL_SWEEP_RUBRIC_SCAN`, default on). Crucially this adds **`preference`** to the overlay
(`_CRM_FACTOR_KEYS` in deal_engine_scoring.py) — the weight-20 factor that has **no MEDDPICC
field**, so before this it could only ever read "missing" (−0.30) and silently capped Win. Each
lifted Win factor's contribution now **cites its source** ("preference — from Next-Step: 'positive
feedback'"), which powers the per-score "why is it scored this" reason.

**Why.** Hot deals scored low on Win because the LLM under-read the rubric while the real evidence
(preference, champion, EB, pain, ROI, pricing) sat in the Next-Step log. The overlay rescues it
deterministically (MAX, never hides). Validated on **real HAVI** Next-Step data: keyword scan hits
preference/exec_access/business_case/differentiation/commercial → **Win 65 → 70** (its honest
Shortlisted ceiling), FC 46 → 51; Deal Momentum already 83 via footprints. (Win 80+ still requires
Vendor-Selected+ per the unchanged stage ceilings — RFP rounds cap at 70 by design.)

**How to work with it going forward.** Takes effect on the next sweep after deploy (re-sweep to
refresh). Keyword lists live in `_RUBRIC_KEYWORDS`; presence-based, so tune phrases there. The
overlay only ever LIFTS a factor — MEDDPICC stays authoritative for the factors it covers.

## 2026-06-29 — Win momentum-drag: the stage anchor falls if its expected motion isn't happening

**What.** Win is now dragged DOWN when Deal Momentum is below the stage's EXPECTED momentum
(`WIN_EXPECTED_MOMENTUM`: Qualified 50 / Formal 52 / Shortlisted 56 / Vendor Selected 60 /
Contracting 62 / Signed-PO 55). `drag = max(0, expected − momentum) * WIN_MOMENTUM_DRAG_RATE`
(=1.0, "drastic"), subtracted before the stage ceiling, NO floor. Momentum is now computed
BEFORE Win in `compute_deal_scores` and passed in (`score_win_position(..., momentum=)`).

**Why.** A high-stage deal shouldn't coast on its anchor if it isn't behaving like its stage
demands. ACEN (Vendor Selected, base 80): momentum 60 -> 80 (on track); 50 -> 70; 40 -> 60;
30 -> 50; 20 -> 40. A quiet Vendor-Selected deal collapses instead of reading near-won.

**Note.** Bites fully once engagement-based Momentum v2 is live per deal (re-sweep populates
ai.footprints); until then a deal's signal-momentum (~50) yields a modest blanket drag at the
higher stages.

## 2026-06-29 — Deal Momentum v2: pure engagement + next-steps + milestones

**What.** New momentum model (`score_momentum_v2`) reads PURELY three pillars, centered on 50:
1. **Engagement depth** (dominant) — each meeting/task classified by an engagement TIER
   (`deal_engine_footprints.classify_engagement`): POC 10 · workshop/ROI/procurement-workshop 8 ·
   themed diligence calls (InfoSec/reference/legal-redline) 7 · F2F/RFP-submitted 6 · deep-dive
   demo 5 · standard demo 3 · discovery 1.5. Highest recent tier sets the floor (×2.6), plus a
   frequency bump. Buyer-attended = full weight; rep-only email of a type ≈ 40%; recency-weighted.
2. **Next-step freshness** — recently updated + dated milestones logged.
3. **New milestones** — a recent stage advance + a real high-tier session having happened.
   minus an **asymmetric stall** drag (quiet beyond stage cadence sinks it).
The friction that used to drag momentum (pricing, demo failures) is GONE from momentum — it
lives in Win/Risk now (user-directed). `compute_deal_scores` uses v2 when `ai.footprints` exist.

**Data path.** The sweep computes `ai.footprints` deterministically from SF Tasks + Events +
opp summary fields (`_footprints_for`): buyer-vs-rep direction (Clari `[Email Received]`/`[In]`
= buyer; `Sent`/`[Out]`/lemlist = rep), engagement tiers, last-buyer-touch, alive-vs-stage.
Populates on (re-)sweep; until then a deal keeps the signal-based momentum.

**Validated.** HAVI (recent workshop + F2F + integration + frequent buyer-attended sessions) ->
momentum 85. Standard Chartered's "72d silent" already fixed via the inbound-email pulse work.

## 2026-06-29 — Engagement reads inbound email; momentum credits forward close-date + next-step

**What.** Two scoring-accuracy fixes from rep feedback (HAVI, Standard Chartered):
- **Inbound buyer email is now an engagement signal.** The pulse was blind to incoming
  email — it only saw Avoma calls + `LastActivityDate` — so a buyer reply that lands as a
  Clari `[Clari - Email Received]` Task (or `EmailMessage Incoming=true`) was invisible and
  the deal read falsely cold (Standard Chartered: "72 days silent" while Stuart had replied
  + nominated Horizon attendees). `_buyer_identity` (deal_engine_sweep.py) now reads the
  latest inbound-email date; it's threaded into `compute_pulse`/`compute_pulse_from_hard`
  (deal_engine_pulse.py) as a real two-way touch (treated like a buyer call), stamped onto
  `hard.last_inbound_email_date`, and surfaced in the pulse summary + ground-truth
  `render_block`. Also hardened `flag_contradicts_live_pulse` (added "internal activity",
  "not buyer engagement", "buyer silence") so the agent can't re-narrate a live pulse cold.
- **Momentum credits a forward-pulled close date + active Next Step.** Added `+close_date_pulled_forward`
  (reads the already-stored `ai.opp_trends.close_date_trend>0`) and `+next_step_active`
  (counts dated milestones in the Next_Step log) to `MOMENTUM` (deal_engine_scoring.py), and
  **guarded the negative** so a date pulled EARLIER can no longer fire `close_date_pushed`
  (the HAVI bug: accelerating the date lost momentum).

**Why.** The engine under-read genuine engagement (inbound email) and under-credited genuine
forward motion (date pulled in, milestones logged) — producing false-cold verdicts and
suppressed momentum on healthy deals.

**How to work with it going forward.** Takes effect on the next sweep (re-sweep affected
deals to refresh). Limitations: true Next-Step *update cadence* needs SF history-tracking on
`Next_Step__c` (off today) — we proxy via dated milestones in the current Next_Step text. The
inbound source is the Clari Task subject `[Clari - Email Received]` (primary) + `EmailMessage
Incoming=true` (fallback); `LastActivityDate` is NOT reliable for inbound.
## 2026-06-29 — Win Position stage ceilings + opp-trend signals

**Win ceilings** (`WIN_STAGE_CEILING`, `_win_ceiling`): you can't be highly confident of
winning before the buyer is structurally committed. Caps applied AFTER anchor+rubric+trend:
BEFORE RFP (Initial Interest / Qualified) -> max 30; DURING RFP (Formal Evaluation /
Shortlisted) -> max 70; post-shortlist (Vendor Selected / Contract / PO) -> up to 100.
So a strongest-possible Qualified deal caps at 30, Shortlisted at 70.

**Opp-trend signals** (`deal_engine_trends`, `opp_trends_one`, backfill_opp_trends): Win now
blends a modest (±influence 0.4) signed nudge from field-history CRM moves — amount up/down,
close date pulled-in/pushed-out, stage advance/regress, forecast-category up/down — all
recency-weighted within the trend window. Durable (sweep recomputes per-opp so re-sweeps keep
them). Backfill: POST /api/deal-engine/backfill/opp-trends.

## 2026-06-29 — Datalake self-healing reconciliation + sync hardening

**What.** The Avoma→datalake lake was only filled by (1) a manual day-by-day backfill
that marks days `done` and never revisits, and (2) the per-event AINOTE webhook
(`datalake_sync.sync_meeting`). Any meeting whose transcript became ready AFTER its day
was processed — a header synced before the call, a late transcript, or a missed/failed
webhook — was left a **content-less header forever**. Because the sweep reads calls from
the datalake (`DEAL_SWEEP_AVOMA_FROM_DATALAKE=true`), such deals read `calls_read=0`
→ `buyer_calls_seen=false` → suppressed scores / `Off Track` (the HAVI 2026-06-23 case).
Three fixes:
- **`datalake_reconcile.py` (new) + `_datalake_reconcile_scheduler` in server.py** — a
  recurring pass: re-pulls the last `DATALAKE_RECONCILE_LOOKBACK_DAYS` (7) of meetings
  each `DATALAKE_RECONCILE_INTERVAL_MIN` (60), filling transcripts/insights via the
  Avoma DETAIL endpoints; plus a once-daily null-content backfill for tracked-opp rows
  missing a transcript. Idempotent upserts; no-op unless datalake+Avoma configured.
- **Backfill hardening** (`scripts/datalake_backfill.py`) — a day is marked `done` ONLY
  when every expected transcript landed (else `partial`, re-runnable); fetches the
  transcription_uuid from the meeting DETAIL endpoint when the LIST payload omits it;
  fails loud (SystemExit) on Avoma 401/403.
- **Fail-loud on Avoma auth** — `datalake_sync.py` + `scripts/datalake_backfill.py` now
  surface 401/403 (live path logs LOUD; backfill `SystemExit`s) instead of silently
  emitting empty rows. Added the missing `avoma_sync_days` table to
  `scripts/datalake_schema.sql`. Bumped the code-default `DEAL_SWEEP_MAX_TOKENS`
  32000→64000 to match the prod task-def (helps non-task-def runs like the backfill).
- **NOT done this round: the committed hardcoded Avoma token removal.** It can't ship
  yet — committing the token is forbidden, and removing it would disable the live
  webhook sync + this reconciler because `AVOMA_API_TOKEN` is not yet a `mase/app-env`
  secret. Deferred (see below); the token line is left exactly as it already was.

**Why.** The lake silently rotted between backfills; deals with real, recent calls read
as dark. The webhook alone can't guarantee delivery/completeness.

**How to work with it going forward.** This deploy is **non-breaking** (the existing
token keeps sync alive; the reconciler just adds a healing pass). ⚠️ **Follow-up to
finish the security fix:** add `AVOMA_API_TOKEN` to the `mase/app-env` Secrets Manager
secret (`render_taskdef.py` auto-enumerates app-env keys, so it injects on the next
deploy), THEN delete the hardcoded token from `datalake_sync.py` + `datalake_backfill.py`.
Toggle the new reconcile loop with `DATALAKE_RECONCILE_ENABLED=false`.

## 2026-06-29 — Win opportunity-trend signals (deterministic, from field history)

**What.** Win now reflects the deal's CRM MOMENTUM, deterministically (no LLM). New
`deal_engine_trends.derive_opp_trends` reads `field_history_cache` (Amount, CloseDate,
StageName, ForecastCategory) and emits signed, recency-weighted trends in [-1,1]:
- amount up = +, down = -; close date pulled IN = +, pushed OUT = -;
- stage advanced = +, regressed / went dead (No Decision / Qualified Out / Closed Lost /
  Omitted) = -; forecast category upgraded = +, downgraded = -.
`score_win_position` blends these (`WIN_TREND_WEIGHTS`, `WIN_TREND_INFLUENCE=0.40`) into the
Win net — so progression lifts Win and regression chips it off, still inside the +/-30 band
(stage/forecast weigh a bit more than amount/close; a strong trend set shifts Win up to ~12).
Populated for the book by `deal_engine_store.backfill_opp_trends` (one batched cache read,
matches 18-char cache ids to 15-char book ids), endpoint
`POST /api/deal-engine/backfill/opp-trends`. Validated on real history (e.g. stage
Formal->Qualified = -0.72; close pushed 286d = -0.95; amount 0->49k = +0.96).

**Why.** User: "if amount increased / close date pulled earlier / forecast category upgraded
that's a buying signal; stage/forecast/close regression is a loss signal." These CRM moves
are deterministic and were not feeding Win.

**Phase 1 of 3** (momentum buyer-vs-rep + failed-sweep resilience to follow). Risk/CMT/FC
unchanged. Refresh trends by re-running the backfill (or wire into the field-history webhook).

## 2026-06-29 — Rubric Win + 30-60d Momentum (user rubric, phase 1: scoring)

**Win** (`score_win_position`): keeps the STAGE ANCHOR as the base, then applies a SIGNED
adjustment of up to +/-30 (`WIN_RUBRIC_BAND`) driven by the FULL rubric factor table —
differentiation 20 / customer preference 20 / champion 15 / exec access 15 / competitive 15 /
business case 10 / commercial 5 (`RUBRIC_WIN_WEIGHTS`). Strong evidence ADDS, weak/negative
CHIPS OFF, and MISSING evidence is a MILD NEGATIVE (`WIN_MISSING=-0.30`) — "not proven yet".
Factors map from real structured fields today (`meddpicc.*` status, `champion_strength`,
`ai_fit_signal`, `competitive_position`); two factors (customer_preference, business_case)
read sweep fields if present, else proxy. `_rubric_win_strengths` / `_status_strength` /
`_competitive_strength`. Validated: stage still leads (Omnia 85->95, MAIR ~86), weak early
deals chip down (ABM ~2).

**Momentum** (`score_momentum`): assessed over a BROADER 30-60 DAY window — only quiet beyond
~30d (`MOMENTUM_WINDOW`) counts as stalling, scaling across the next 30 (`MOMENTUM_STALL_TAU`).
The 7 granular rubric signals (seniority rising, commercial topics entering, concrete dates,
customer asked next meeting, close plan concretizing, generic demo only, competitor praised)
now fire from `ai.momentum_signals.<key>` — they were dead factors; they stay dormant until
the sweep extracts them (phase 2), because they're call-level/time-sensitive and can't be
faked from static MEDDPICC without inflating stalled deals.

**Risk / Commitment / FC unchanged** (per instruction). FC mechanically reflects the new win
(it is win-anchored) — formula untouched.

**Phase 2 (separate):** extend the sweep prompt to emit `customer_preference`, `business_case`,
and `momentum_signals` so the rubric runs on real call evidence, not proxies.

## 2026-06-29 — Dead-deal handling (lost / qualified out / omitted)

**What.** A dead deal is no longer treated as a live opportunity anywhere.
- **Detection** `deal_engine_scoring.is_dead_deal(record)` -> 'Lost' | 'Qualified Out' | 'Omitted'.
  EITHER stage (Closed Lost / Qualified Out) OR forecast category (Omitted) triggers it. Closed
  WON is NOT included. Read-time, so re-opening a deal auto-revives it.
- **Scores** (`compute_deal_scores`): dead -> terminal block `{dead:true, dead_label, read:<label>,
  win/mom/cmt/risk/fc = null}` — no misleading numbers. `attach_deal_scores` recomputes a now-dead
  deal so stale live scores can't linger.
- **Verdict/Health** (`attach_verdict_view`): dead -> verdict + health_bucket = the label (Lost/
  Omitted), risk_tag None, `dead:true` — not On Track / Slowing / Off Track.
- **To-dos** (`derive_todo`): dead -> ONLY the single top play + best practices; prospect
  requirements, Zycus commitments, and buyer-owed items are suppressed. Best practices come from
  `_dead_deal_best_practices` = retrospective ("what we didn't do well", grounded in the record:
  who we lost to, EB never mapped, single-thread) + SPECIFIC SF hygiene (wrong stage, unlogged
  outcome). No win-back (sponsors locked 3-5 yrs).

**Why.** On a closed-lost deal (e.g. Restaurant Brands) the prospect's open requirements, our
pending commitments, and buyer-owed items are all irrelevant — they already went with another
vendor. Generating action items on dead deals is noise.

**Frontend companion** (MASE): Lost/Omitted health tone, scores show the terminal label not
numbers, and dead deals are EXCLUDED from the weighted-forecast / weighted-pipeline roll-ups.

## 2026-06-29 — Enterprise-sales recalibration of momentum / risk / FC

Companion to the stage-anchored win change. All three other scores were flat or mis-shaped
for an enterprise book.

**Momentum** (`score_momentum` + derive_evidence): was 50 +/- sparse signals with a SYMMETRIC
decay that pulled stalled deals back UP toward 50 — so the whole book sat ~48 (uninformative).
Now: silence DRAGS momentum down (asymmetric `MOMENTUM_STALL_MAX=25` stall, grows with overdue
days), and a live deal with buyer calls this sweep gets a lift (new `buyer_engaged_this_sweep`
factor). Stalled deals sink below 50, active deals rise — real spread.

**Risk** (derive_evidence close-date block): close-date risk previously fired ONLY from verdict
wording (`cdr_now`/`cdr_count`), so a deal whose close DATE had simply passed read 0 risk (e.g.
Mair, a month overdue, showed 0 at LATE). Now an overdue / imminent (<=14d at advanced stage)
close date fires `close_date_pushed_repeatedly` directly from `pulse.days_to_close`. No baseline
floor added (user: a genuinely clean deal may read 0).

**FC** (`score_forecast_confidence`): was a weighted avg of win/mom/com/(100-risk) TIMES a
coverage multiplier (0.5-1.0) — the multiplier crushed "Partial Read" deals (a Commit in
contracting read ~62). Now FC ANCHORS ON WIN (the stage close-probability) and adjusts:
`fc = win + 0.20*(com-50) + 0.12*(mom-50) - 0.50*risk`, clamped 0-99. Coverage is a reported
FLAG (`coverage_flag`), not a haircut. Result (validated): Qualified ~12-17, Shortlisted ~40-60,
Vendor Selected ~62-85, clean Commit-in-contracting 90+ (Omnia 90.7), overdue contracting
dampened (Mair 76 — high win, but slipping close window). User-approved 90+ ceiling.

**Rollout.** Read-time recompute automatic; stored refreshed via backfill/deal-scores.

## 2026-06-29 — Stage-anchored win probability

**What.** `score_win_position` no longer starts from a flat 50 baseline. It now starts from a
STAGE PRIOR (how far through buying = how much is left to close) and lets within-stage signals
move it by at most +/-15 (`WIN_BAND`). Anchors (user-approved "standard enterprise"):
Initial Interest 8 · Qualified 18 · Formal Eval 35 · Shortlisted 55 · Vendor Selected 72 ·
Contract In Progress/Negotiation 85 · Contract Signed 95 · PO 98 (`WIN_STAGE_ANCHOR`).
Within-stage adjustment = net of POSITIVE drivers (`WIN_POS`: product fit, buyer momentum/
engagement, champion + EB access = "we're leading", commercial/pricing motion, milestone
evidence, multi-threading) minus LOSS risk (`WIN_NEG`: competitor preferred, open competitive
RFP, no-decision drift, stage inflation), normalised to +/-15.

**Why.** Win was nearly flat across the funnel (avg Qualified 62 -> Contract-In-Progress 72,
~10 pts total) because stage was worth only +/-8. Late deals were under-scored (MAIR, a Commit /
Contract-In-Progress deal, read 64) and early deals over-scored (Qualified ~62 ≈ coin-flip).
Now MAIR -> 88.9 and Qualified deals land ~20. Stage drives win; signals refine.

**Key nuance.** Close-date / budget / paperwork are TIMING risks — they do NOT drag *win* (you
still win, just later); they remain in `deal_risk` / momentum. Only loss-risk drags win.

**Caveat.** "Pricing comfort" and "verbal confirmation we're leading" are approximated today
(commercial_motion; champion+EB minus competitor). They sharpen once the sweep captures them as
explicit signals.

**Rollout.** Read-time recompute is automatic (attach_deal_scores); stored deal_scores refreshed
via POST /api/deal-engine/backfill/deal-scores. Flows into forecast_confidence (win weight 0.30).

## 2026-06-29 — Surgical verdict/health/risk recompute (no re-sweep) + dogfight-gate fix

**What.** A way to redo Verdict / Health / Risk across the book from STORED data, applying
the current stage-aware definitions, WITHOUT a re-sweep (no Avoma/SF fetch).
- **`deal_engine_verdict.py`** (new):
  - `derive_risk_tag` — a 1-3 word tag for the dominant OPEN risk (stage-aware; uses the
    same gated risk the scorer uses).
  - `regrade_label` — re-grades the stored verdict label under the stage rules (the big
    correction is LATE: never Off Track; champion/EB/pain gaps are not risks; only close-
    date / paperwork / budget / a LIVE multi-vendor fight count).
  - `recompute_prose` — optional verdict-only LLM pass over each stored record (bounded
    concurrency, default 6) that rewrites the **<=40 word** headline + label + risk tag and
    PERSISTS it (stamps `verdict_recomputed_at`). Default scope = the ~62 forecasted deals.
- **`deal_engine_store.attach_verdict_view`** — read-time net (mirrors attach_deal_scores):
  guarantees `north_star_verdict.health_bucket` + `risk_tag` and a stage-corrected `verdict`
  on every read; defers to a persisted LLM recompute when `verdict_recomputed_at` is set.
  Wired into `slim_record` (list) and the `/opportunities/{id}` drawer. So the deterministic
  layer (health bucket + risk tag + label re-grade) is live for ALL deals the moment this
  deploys — no batch needed.
- **Endpoint** `POST /api/deal-engine/recompute/verdict` `{scope:"forecasted"|"all"|[ids],
  concurrency:6}` — runs the LLM prose pass + persists.

**Dogfight-gate fix.** `derive_evidence` emits competition at a FIXED strength 0.5 (no
recency decay yet) and distinguishes `competitor_preferred` (a rival ahead/incumbent/high-
threat = a real fight) from `open_competitive_rfp` (merely named rivals). The 2026-06-29
"live-dogfight exception" used a `>=0.6` strength gate that could never be met → it was dead
on real data. Fixed: `_LATE_COMPETE = {"competitor_preferred"}`, `_LATE_COMPETE_MIN = 0.5`
— so a real ongoing fight at contracting now correctly counts; plain named-rivals stays
suppressed. NOTE: true "stale vs fresh" competition can't be told apart deterministically
until the recency-weighted signal model lands; the LLM prose pass judges freshness for the
forecasted deals.

**Rollout.** Deterministic layer = live on deploy (read-time, free, all 440). Prose pass =
on-demand via the endpoint (forecasted 62, ~1-2 min at concurrency 6, modest cost).

## 2026-06-29 — LATE-stage live-dogfight exception

**What.** The stage-aware risk rule no longer blanket-suppresses competition at LATE.
- **Scoring** (`_late_keep_risk`): at LATE, `competitor_preferred` / `open_competitive_rfp` are
  re-admitted when the signal is strong/fresh (`strength >= _LATE_COMPETE_MIN = 0.6`) — a live
  multi-vendor fight at contracting. Weak/stale competition stays stripped (otherwise still only
  close-date/budget). Verified: LATE + strength 0.3 → risk 0; LATE + 0.6/0.9 → full risk (== mid).
- **Sweep prompt** (live override): LATE risk rule + verdict labels updated. A LATE deal may now
  read **At Risk** *only* on a live multi-vendor fight (parallel redlines / comparing final proposals /
  competitor actively preferred with fresh evidence); absent that, worst case stays Close-date risk
  and it can never be Off Track. Stale/settled competition must not be re-raised.

**Why.** A contracting-stage deal can still be a genuine 2–3 vendor dogfight (parallel redlines,
competitor kept warm as leverage). The original Myer fix over-corrected by hiding ALL late
competition; this restores the real-fight signal while keeping stale-competition noise suppressed.

**How to work with it.** Sweep + scoring only — applies as deals are next swept.

## 2026-06-28 — Stage-aware verdict & risk (Myer fix)

**What.** Verdict and risk are now interpreted relative to the deal's STAGE.
- **Sweep prompt** (live Supabase override): new "STAGE-AWARE VERDICT & RISK" block. Tiers
  EARLY (Qualified/Formal Eval) / MID (Shortlisted/Vendor Selected) / LATE (Contract*/PO).
  Risks that count per tier (LATE = only close-date / legal / procurement / budget; champion/EB/
  pain are NOT risks at LATE and the champion/EB SPOF is suppressed). Verdict labels stage-scaled:
  LATE can only read On Track or **Close-date risk** (never At Risk/Off Track). Default On Track
  when no stage-relevant risk; Off Track reserved for hard-kill (lost/disqualified/cancelled) at
  EARLY/MID — a long stall = At Risk. Forecast category never sets the verdict. Silence in legal ≠
  slipping. EB: unmapped (early) → not-engaged (mid) → ignore (late). Verdict and risk kept aligned.
- **Scoring** (`deal_engine_scoring.compute_deal_scores`): at LATE, the deal-risk score is computed
  from ONLY close-date/budget risk factors (`_LATE_RISK_OK`); competitor/passivity/access/stage-
  inflation etc. are stripped so a contract-executing deal can't show inflated risk.

**Why.** Myer (contracting executed) was reading "At Risk — biggest risk: no champion", which is
nonsensical once the contract is signed. Risk must match where the deal actually is.

**Rollout.** Sweep + scoring only (per decision) — existing records update as they're next swept;
no mass re-sweep. (The read-time score net still guarantees no blank scores meanwhile.)

## 2026-06-28 — deal_scores can never render blank (read-time safety net)

**What.** New `attach_deal_scores(rec)` (`deal_engine_store.py`) guarantees `ai.deal_scores`
on every read: if a sweep/re-sweep left it empty, it computes the scores read-time from the
record's stored signals via the same `deal_engine_scoring.compute_deal_scores` model (mirrors
`attach_pulse`). Wired into `slim_record` (the deals list) and the `/opportunities/{id}` drawer
endpoint (`stamp_move_overrides(attach_deal_scores(attach_pulse(rec)))`). Read-only, never
persisted over a fresh sweep, never raises.

**Why.** 31 deals showed blank MOM/CMT/Risk/FC — all freshly re-swept; the (likely stale-worker)
sweep path had dropped `ai.deal_scores`. Backfilling after every sweep is a treadmill; this net
makes the scores impossible to show blank regardless of what the sweep does. Cheap — only the
few deals missing scores recompute (pure arithmetic, no LLM, no I/O). Deploying also refreshes
the worker image, which should restore persistence at source (the score step already runs there).

## 2026-06-28 — MECE de-duplication of to-dos (one ask = one row)

**What.** `derive_todo` now de-dupes action items, PER OPP, read-time: (1) within each
category it collapses exact-normalised and contained near-duplicates (e.g. "book the demo"
listed 3×); (2) across categories it drops a Commitment (`implicit`) that merely restates a
Prospect Requirement (`explicit`) or a buyer-owed item (`important`) on the same deal — the
buyer-stated ask owns the row. Matching is normalise-to-alnum + exact-or-contained (containment
guarded by length>12 so short generic phrases don't over-collapse).

**Why.** QI review found 85% of sampled deals had duplicated/overlapping items — the same ask
appearing as a Requirement, a Commitment, AND a Move. This enforces the long-standing MECE rule
("no repetition in the to-dos") deterministically, no re-sweep, on every surface that reads the
to-do book. Still TODO (prompt-side, separate): de-essay long Moves/Best-practice text and stop
Moves restating Requirements.

## 2026-06-28 — "Commitments made by Zycus" requires evidence (else Best practices)

**What.** In `derive_todo`'s we_promised loop, an item is emitted under the `implicit`
("Commitments made by Zycus") category ONLY if it carries evidence of an actual commitment —
a `grounding_quote` or a named `source`. Without that, it's reclassified to `bestPractice`
(an inferred "we should…" is a best practice, not a commitment). `source` is now carried onto
the implicit item too.

**Why.** C-level rule: don't claim Zycus committed something unless we actually said so on a
call / email / channel. Enforcing it at the source (not just the drawer's display gate) means
EVERY surface agrees — Espresso (which renders the raw categories), Matcha, and the drawer.
Read-time (no re-sweep); the sweep prompt already demands grounding_quote+source on we_promised,
so well-swept records are unaffected — this only catches ungrounded inferences.

## 2026-06-28 — swept_at carries a full IST timestamp (date + time)

**What.** `parsed["swept_at"]` is now `_now_ist()` (Asia/Kolkata, UTC+5:30, full ISO with time
— e.g. `2026-06-28T15:53:13+05:30`) instead of `_today()` (date-only). `_today()` is unchanged
and still used for the agent prompt's "Today's date" line.

**Why.** Freshness audits (Next Step / SF activity / Avoma meeting vs the sweep) were ambiguous
on same-calendar-day changes because swept_at had no time. A real timestamp makes "did X happen
after we swept?" exact. Stored in the JSONB record (the audit/API read it from there); the
`swept_at` table column truncates to date harmlessly. Additive; only affects deals swept from now
on (old records stay date-only until re-swept).

## 2026-06-27 — Deal-scores backfill endpoint (push scores to the existing book)

**What.** `deal_engine_store.backfill_deal_scores(opp_ids=None)` + POST
`/api/deal-engine/backfill/deal-scores` compute `ai.deal_scores` for stored records via
the SAME model the sweep uses (`deal_engine_scoring.compute_deal_scores`) and upsert. Body
optional `{"opp_ids": [...]}`; omitted = whole book. The 440 deals predate the sweep-side
scorer (`6070328`) so none carry scores yet; this pushes them now without re-sweeping each.
Idempotent, additive (only sets `ai.deal_scores`).

**Why.** Light up the frontend Deal Scores UI on the existing book immediately. Because it's
the identical code path, backfilled scores match the dynamic per-sweep recompute — so when a
deal is next swept (tracking stage + opp updates) the number stays consistent.

## 2026-06-27 — Deterministic deal scoring inside the sweep (`ai.deal_scores`)

**What.** New module `deal_engine_scoring.py` computes five scores per opportunity —
**Win Position / Deal Momentum / Customer Commitment / Deal Risk** (each 0–100) plus a
**Forecast Confidence** roll-up and an evidence-coverage **Read** label (Full/Solid/Partial/
Early) — each with a 2-sentence plain-English commentary. It runs as a step inside
`analyze_one()` in `deal_engine_sweep.py` (right after `_revops_head_review`, before persist)
and writes `parsed["ai"]["deal_scores"]`. Stored in the existing `deal_records.record` JSONB
(no migration). `GET /api/deal-engine/opportunities/{opp_id}` returns it under `ai.deal_scores`.

**Why.** Give VPs a defensible, evidence-anchored read of every deal — winnability vs timing-
risk separated, absence treated as low *confidence* not low *score*, recency-weighted — and a
single forecast-confidence number to rank the book. Mirrors the offline model in
`~/Downloads/scoreModefiles` (arithmetic is an exact port; reference cases reconcile to the
decimal — see `tests/test_deal_scoring.py`).

**How to work with it.**
- **Hybrid factor source.** Factors are DERIVED deterministically from the gate-clean swept
  signals (pulse state, north-star verdict + trajectory, MEDDPICC statuses, competitive_position,
  evidence_coverage, stakeholder_map, durable packets, close-date verdict history). If the sweep
  agent additionally emits `ai.deal_scores_evidence.factors`, those soft judgment factors are
  overlaid (agent wins on the keys it provides). The agent emission is OPTIONAL — see
  `docs/DEAL_SCORES_PROMPT_BLOCK.md` for the block to append to the live `mase_deal_sweep`
  Supabase prompt when ready; the code works without it.
- **Safety.** No LLM call, additive (touches only `ai.deal_scores`), behind env flag
  `DEAL_SCORES_ENABLED` (default on), and `compute_deal_scores()` NEVER raises — a scoring
  failure logs and the sweep continues. Backend populates the field; the frontend renders it
  separately (score chips + commentary drawer) — so shipping the backend first is low-blast-radius.
- **Re-score the book:** any re-sweep repopulates `ai.deal_scores`. Tune derivation in
  `derive_evidence()`; the arithmetic/weights are locked to match the offline model.

## 2026-06-27 — Sweep reads MEDDPICC custom objects + economic-buyer cache backfill

**What.** Two changes so the economic buyer (and the rest of MEDDPICC) is sourced from
the CRM and reflected in the cache, not just the UI:
1. **New sweep evidence source** (`deal_engine_sweep.py`): `_meddpicc_crm()` pulls the
   `MEDDPICC__c` (preferred) and `MEDDPICC_2_0__c` custom objects by `Opportunity_Name__c`,
   merges them (full MEDDPICC: economic buyer, budget, decision criteria/process, pain,
   champion, competition, blockers, products), and `_meddpicc_crm_block()` injects them into
   the agent user message as a **CRM hint to corroborate** — with an explicit instruction to
   drop dated/contradicted items (the block carries the record's last-updated date). Where a
   named economic buyer is present and uncontradicted, the agent confirms it (no gap). Gated
   by `DEAL_SWEEP_MEDDPICC_FETCH` (default on); best-effort, never blocks the sweep.
2. **Economic-buyer backfill** (`deal_engine_store.py` `backfill_economic_buyer` + `EB_BACKFILL`,
   POST `/api/deal-engine/backfill/economic-buyer`): one-time/idempotent write of the 17
   confirmed EBs into `ai.meddpicc.economic_buyer` (status=confirmed, source=CRM) on the stored
   packets, so the cache matches the UI without a re-sweep.

**Why.** The EB was recorded in the SF MEDDPICC objects but the sweep marked it a gap (it never
read those objects). The UI override (frontend `getEbOverride`) fixed the *display*; this makes
the *data* right at source (future sweeps) and in the *cache* now (backfill).

**How to work with it.** Future sweeps auto-confirm the EB from the CRM. To refresh the backfill
list, re-run the SF MEDDPICC scan and update `EB_BACKFILL`. The engagement verdict is unchanged —
MEDDPICC only fixes visibility; momentum still comes from call evidence.

## 2026-06-26 — Prospect requirements are date-tracked (no re-sweep)

**What.** `derive_todo` now stamps a trackable due date on every open
`explicit_requirements` item (`due`/`act_by` + `due_source` + `urgency`). The date
is, in order: a deadline STATED in the ask text ("by 18 Jul", "due 30 June" →
`due_source="stated"`), else one BACK-PLANNED from the deal close date (heavier
deliverables get more lead time; clamped to `[today+3, today+REQUIREMENT_DUE_CAP_DAYS]` — a
~6-month / 180-day cap decoupled from the 60-day action horizon, so each deal's requirement
date reflects its own close instead of a flat shared horizon date →
`due_source="back_planned"`). New helpers: `_requirement_due`, `_stated_due_dates`,
`_closest_year_date`, `_heavy_requirement`.

**Why.** RevOps needs to track WHEN a buyer-owed deliverable is due and whether it
slipped — requirements previously carried no date. Close date is the north-star
anchor. Date parsing is FUTURE-aware (closest-year inference) — unlike the pulse
parser's past bias, which was turning "30 June" into last year. Numeric M/D forms
are ignored to avoid prose false-positives ("24/7").

**How to work with it.** Read-time only: `/todo` recomputes from stored packets, so
this took effect on deploy with **no re-sweep**. The frontend renders overdue/on-time
from `due_source` + `act_by`. If a future sweep captures a real `due`/`due_date` on a
requirement, that wins and is marked `stated`.

## 2026-06-25 — Pulse accuracy + heavy-deal sweep reliability

**What.** (1) Engagement pulse (`deal_engine_pulse.py`): `_days_since` clamps a future
LastActivityDate to 0 (kills the negative "−25 days" display); and a buyer call read this
sweep only counts toward "live" when SF activity is NOT 90+ days old — so a months-silent deal
with old calls no longer reads "live" (the "118 days yet live" bug). (2) Sweep
(`deal_engine_sweep.py`): a hard cap `DEAL_SWEEP_AVOMA_READER_CAP` (default 3) on concurrent
Avoma transcript reads per deal — `staffing_plan` scaled the reader pool to 6 on deep deals,
which throttled the DeepAgent/Avoma gateway (≈1MB transcripts) and made heavy deals miss
discovery and fail.

**Why.** The pulse showed impossible day-counts and false-"live" on stale deals; the heavy
forecasted deals (the very ones we most need re-swept) kept failing under 5–6-wide Avoma load.

**How to work with it.** Pulse is read-time → applies to every deal immediately. The reader cap
is env-tunable (`DEAL_SWEEP_AVOMA_READER_CAP`): raise it if sweeps are too slow, lower if Avoma
still throttles. Validated pulse on 5 cases (future→0/live; 118d+old-calls→dark; recent-lag-
with-call→live preserved).

## 2026-06-25 — Restore interactive MCQ (mase-choice) cards in the deal chat

**What.** The deal-AI chat again emits hidden `<!--mase-choice {...}-->` markers that the
frontend (`DealAgentPanel`) renders as clickable choice cards. The instruction is now baked into
the code-appended `_CHAT_CAPABILITIES` block (server.py), always present on the `/api/deal-engine/
chat/async` deal-chat path.

**Why.** The behaviour lived ONLY in the Supabase `mase_chat_agent` prompt, which was cleared to
empty on 2026-06-24, so the agent silently stopped emitting markers (the renderer was untouched).
Moving it into code makes it wipe-proof — an emptied admin prompt can no longer kill the feature.

**How.** Marker schema: `{"question": "...", "options": ["...","..."], "multi": false, "title"?: "..."}`,
one per question. Ships with the deploy. No Supabase change required.

## 2026-06-25 — we_promised must be an evidence-backed commitment (not inferred)

**What.** Sweep prompt (§ FOUR HEADS): an `implicit_requirements.we_promised` deliverable is emitted
ONLY when Zycus actually committed to it on a call / in writing — verbatim quote in `grounding_quote`
plus a named `source`. Inferred "what we should do next" is a `recommended_move`, not a commitment; an
empty `we_promised` is correct when we made no commitments.

**Why.** Keeps "Commitments made by Zycus" short and trustworthy. Pairs with the frontend filter that
drops grounding-less implicit items from that bucket. Applies on re-sweep.

## 2026-06-25 — Deal health: FOUR-tier verdict (split At Risk → Close Date Risk + Slowing)

**What.** `north_star_verdict.verdict` now emits one of FOUR exact strings — `On Track`,
`Close Date Risk`, `Slowing`, `Off Track` — replacing the three (`At Risk` removed, split in two):
- **Close Date Risk** — a fundamentally healthy, engaged deal whose ONLY problem is an optimistic
  close date that will slip (a POSITIVE/light read; frontend colours it light green).
- **Slowing** — losing momentum: a key action stalled (withheld approval / missing info) or buyer
  engagement thinning, but not yet cold.
Precedence: **Off Track (cold) > Slowing (stalled/thinning) > Close Date Risk (healthy but late) >
On Track.** An indefensible forecast on an otherwise-healthy deal maps to Close Date Risk, not lower.

**Why.** One "At Risk" bucket lumped healthy-but-late deals (a live POC days from a placeholder close)
with genuinely stalling deals, making the forecast book read alarmingly red. Splitting lets McAfee read
light-green ("good deal, date slips") instead of amber alarm — fixing the perceived over-stringency
WITHOUT masking real risk.

**How.** §3 rubric rewritten + §5 schema enum updated in the seed (live prompt — Supabase override is
empty so the seed ships with deploy); `_RANK` (verdict trajectory) extended to four tiers with legacy
`At Risk` == `Slowing`. Existing records keep `At Risk` until re-swept; the frontend maps legacy
`At Risk` → Slowing (amber). Apply to the book via a re-sweep.

## 2026-06-25 — Verdict definitions locked to three statuses (On Track / At Risk / Off Track)

**What.** Rewrote the `north_star_verdict` guide rails (§3 of the sweep prompt) to three explicit,
canonical definitions:
- **On Track** — significant recent movement consistent with the stage + close date, AND the buyer is
  engaged/responsive on the planned next step. A few missed/delayed deliverables are tolerated while the
  deal is, on balance, progressing toward close.
- **At Risk** — still progressing, but an important action is stalled: blocked on a buyer approval to
  advance, OR missing information we need to execute, OR engagement gone thin/silent (not yet cold). One
  stalled action is enough.
- **Off Track** — gone cold: no buyer-facing deliverable executed in the last 60 days AND no buyer
  engagement.

**Why.** The three bands are unchanged (enum stays `On Track|At Risk|Off Track`); the *criteria* are now the
canonical product definition, applied consistently, replacing the prior contributor/forcing-condition rubric.
`forecast_defensible` now flags the NUMBER only — it no longer drags the verdict band unless the deal is also
stalled/cold.

**How to work with it.** Edit is in the on-disk seed (`prompts/deal_engine_sweep_system_prompt.md`) — the live
`mase_deal_sweep` Supabase override is EMPTY, so prod runs the seed and this ships with the deploy. Takes effect
per deal on its **next sweep**; existing records keep their last-computed band until re-swept. The frontend
(MASE) now displays the band identically on every surface via a single `healthLabel()`.

## 2026-06-25 — 4-head MECE to-do model (consolidate `open_deliverables` into `implicit_requirements`)

**What.** The sweep output's to-do blocks are reduced to **four MECE heads**, so one live
thread appears in exactly one place:
1. **Moves** = `recommended_moves` (unchanged; a prioritization layer, rendered as "The Play").
2. **Deliverables / Prospect requirements** = `explicit_requirements` (unchanged).
3. **Implicit requirements** = now a single head with **two sub-buckets**:
   `implicit_requirements.we_promised` (Zycus owes — head 3a) and
   `implicit_requirements.buyer_dependent` (the buyer owes us — head 3b).
4. **Best practices** = `best_practice_check.flags` (unchanged).

The old flat `implicit_requirements` AND the separate **`open_deliverables`** block both fold
into head 3 (`who` is the only 3a/3b divider). `open_deliverables` is **removed** from the
output. Precedence: **explicit beats implicit** (a buyer-demanded item stays in
`explicit_requirements`, never `we_promised`).

**How it's built.** The output is projected from the durable packets, so the real edit is in
**`deal_engine_packets.project_into_ai`** (emits the new nested `implicit_requirements`, drops
`open_deliverables`) + **`extract_candidates`** (reads the new shape too; new-shape items become
who-tagged `commitment` packets). Packet TYPES are unchanged. Readers updated:
`deal_engine_store.derive_todo` (new legacy-tolerant readers `_we_promised_items` /
`_buyer_dependent_items`; **to-do `category` strings kept stable** — `implicit` = head 3a,
`important` = head 3b — so the Salesforce push/edit/delete ledger keyed by `todo_key` survives),
`todo_grouping` (group + de-collide on the new shape), `deal_engine_validation`,
`deal_quality_inspector`, `server.py` (SF-push labels + admin viewer). The prompt seed
(`prompts/deal_engine_sweep_system_prompt.md`) documents the new contract + the MECE precedence.

**Migration (no re-sweep).** `regroup_todos()` now **re-projects from packets** (AI-free) so the
back-catalogue migrates to the new shape. Until re-projected, `derive_todo`'s legacy fallback reads
`open_deliverables` + flat `implicit_requirements` so existing records still render under the four
new buckets. **The live Supabase `mase_deal_sweep` prompt row is empty → prod runs the on-disk
seed**, so the prompt change ships with a deploy (or push the seed text into the Supabase override).

**Frontend.** `DealTodoBuckets` now renders **Moves / Prospect requirements / Commitments made by
Zycus / Waiting on the buyer / Best practices**; `hideMoves` hides the Moves head where "The Play"
already shows it (the drawer). The deployed inline-bucket drawer (read raw `open_deliverables`) is
**replaced** by the `DealTodoBuckets`-based `DealDrawerView` (reads `/todo`, has the per-row SF push).

## 2026-06-25 — To-do hygiene moved INTO the projection + cross-bucket de-collision

**What.** Two changes to how the four to-do buckets (Prospect requirements / Next phase /
Waiting on the buyer / Best practices) are built:
1. **Dedup now runs inside `project_into_ai`** (`deal_engine_packets.py`), the single
   projection chokepoint, instead of only as a post-sweep step in `analyze_one`. Every
   projection of the packet store now calls `todo_grouping.tidy()` at the end, so the
   display lists are clean **by construction** on every sweep.
2. **New cross-bucket de-collision** (`todo_grouping.decollide_buckets` / `tidy`): a
   `best_practice_check` flag that merely restates a `recommended_move` or `open_deliverable`
   is dropped (it already lives in "Next phase"/"Waiting on the buyer"). Best-practice now
   only carries genuine action-less gaps. Conservative: requires ≥2 shared content tokens and
   ≥0.55 overlap of the action's signature.

**Why.** Served records still showed heavy duplication (Publicis: 12 best-practice flags = 4
themes, each also a move + a deliverable; ALTRAD: 57 flags). Root causes: (a) the deterministic
grouper's output never reached the persisted record — `group_key` was `None` on every served
item, i.e. it wasn't taking effect on the live path; the packets are the source of truth and the
lists are *projected* from them, so dedup belongs in the projection, not bolted on after. (b)
The grouper only deduped *within* a block; the duplication users see is *across* the three
blocks that feed the four UI buckets, and is *semantic* (long, differently-worded restatements
that lexical token-overlap can't merge within a block but de-collision catches across blocks).

**How to work with it going forward.** Packets are never mutated — full living-memory history is
preserved; only the projected display lists are tidied, so it's safe and re-runnable. Validated
before/after on 4 deals: Publicis bp 12→1, Allstate 3→1, ALTRAD 57→14, Ancestry (dark) 17→14
(de-collision kept all genuine gaps — it does NOT empty buckets). Idempotent. Existing records
clean up on their next sweep, or via a token-free re-projection pass (load record →
`project_into_ai(rec['ai'], rec['packets'])` → re-store; no Avoma/SF). Tune knobs:
`todo_grouping._DECOLLIDE_THRESHOLD` (0.55) and the per-block grouping thresholds.

## 2026-06-24 — Coverage counts: engine truth overwrites the model's self-report (calls_read fix)

**What.** `evidence_coverage.calls_read` / `calls_discovered` were taken from the AI agent's
JSON self-report, which is unreliable — the agent routinely under-reports the calls it was
handed (DuBois: engine read 7 transcripts, log `avoma-engine read=7`, but the persisted
record said `calls_read=0`; Publicis: `discovered=4` yet `calls_read=0`). The existing
"never-miss floor" only corrected `calls_discovered` and was gated on the model UNDER-reporting
discovered (`_reported < _eng_calls`), so a correct discovered + wrong `read=0` slipped through.
Now the engine's ground-truth coverage (`_avoma_pf.coverage`) **overwrites both counts whenever
they disagree** with the model — up or down (`deal_engine_sweep.py` ~2645-2679).

**Why.** A wrong `calls_read=0` poisons the engagement pulse, RevOps staffing (`calls_read==0`
→ "lean" deal → skips senior review), thin-detection, and the UI — and was the root of the
false "thin → retry 3× → failed" churn on deals that actually had calls. The datalake HAS the
calls and the engine reads them; only the stored count was wrong (a model reporting bug, not a
data/retrieval bug).

**How to work with it going forward.** `evidence_coverage.calls_read/calls_discovered` are now
ENGINE facts, not model output. The `calls_read=0` thin guard stays intact and finally sees the
true count. Model-owned only when the engine didn't run (parallel-readers off / empty prefetch).
Follow-up (separate, planned): stamp `stakeholder_map[].title` from Salesforce `Contact.Title`
to kill fabricated titles (audit 2026-06-24).

## 2026-06-24 — Worker from-scratch PURGE mode (bulk living-memory rebuild on the fleet)

**What.** The from-scratch rebuild (drop ALL carry-forward, rebuild a deal record purely
from current Avoma+SF evidence) used to exist ONLY on the synchronous
`POST /sweep/{opp_id}/update-living-memory` endpoint — fine for one deal, useless for
bulk (60+ deals × ~17 min each would hammer the API web tier). Now the **worker** can do
it too:
- `POST /api/deal-engine/sweep` accepts **`from_scratch: true`** (with `opp_ids: [...]`).
- `start_sweep` / `enqueue_book_run` mint the queue rows under a **`fromscratch-*` run_id`**.
- `worker.py::_process` sees that prefix and calls `analyze_one(source="update_living_memory")`
  instead of `source="worker"` — i.e. NO carry-forward, record rebuilt from scratch, on the
  autoscaled fleet (6 workers × 8 = up to 48 concurrent).
The report-as-book membership gate and the one-sweep-at-a-time guard are UNCHANGED — a
from-scratch run still can't enqueue a non-member and still refuses while the queue is busy.

**Why.** Living memory accumulated fabrications (McAfee: 349 packets incl. invented "Ariba
18% gap"/"data residency redlines"). From-scratch purges them (McAfee → 35 packets,
calls_read 5, fabrications gone). Needed to roll that purge across the whole book without
melting the API.

**How to work with it going forward.** To purge + rebuild a set of deals clean:
`POST /api/deal-engine/sweep {"opp_ids": [...], "from_scratch": true}` then poll
`/sweep/status`. Re-run is safe (idempotent, replaces the record). NOTE: from-scratch
purges *accumulated* poison but the engine can still over-reach per run (the content-blind
validation gate) — the durable anti-re-poison fixes (claim-content gate, packet cap,
calls_read floor) are still pending.

## 2026-06-23 — Deploy QA layer: smoke test + /selfcheck endpoint + API inventory

**What.** A pre/post-deploy QA gate so a build can't silently drop a route (the chat-404
outages) or an env var (datalake/SNS/LLM tuning):
- **`scripts/smoke_test.sh`** — probes every critical route with SAFE probes (GETs, and
  POSTs with `{}` that validate-reject 400/422 so no sweep/chat/write is triggered).
  PASS = not 404, not 5xx. Exit 1 if any critical route is dead → roll back. Run it
  **before AND after** every deploy.
- **`GET /api/deal-engine/selfcheck`** (`server.py`) — returns BOOLEANS (never secret
  values) for the durable env (Anthropic key, Supabase, Avoma token, `DATALAKE_URL`,
  `DATALAKE_SERVICE_KEY`, `DEAL_SWEEP_AVOMA_FROM_DATALAKE`, `SNS_ALLOWED_TOPIC_ARNS`,
  LLM tuning) + `agent_initialized`. `ok:false` + `missing[]` = a deploy dropped env.
- **`docs/API_INVENTORY.md`** — full endpoint inventory + the deploy QA loop + a
  failure-triage table.

**Why / how to work with it.** The env is already durable in the `deploy.ps1` template +
secrets (prevention); the smoke test + `selfcheck` are the seatbelt that *detects* a
regression immediately instead of a rep finding it in prod. **Every deploy must pass the
smoke test (pre + post) and `selfcheck.ok` must be true.** See `docs/API_INVENTORY.md`.

## 2026-06-23 — G8 temporal anchoring: sweep re-anchors all relative time to today

**What.** Added a hard **TEMPORAL ANCHORING** rule to the ground-truth block injected
into every sweep (`_sweep_facts_block` in `deal_engine_sweep.py`, right after
`Today's date is …`). The agent must re-anchor EVERY time reference to today: convert a
relative phrase copied from a note / living memory (`next week`, `this Thursday`,
`recently`) to its ABSOLUTE date, state whether it is now PAST or upcoming vs today with
approx elapsed/remaining time, never echo a bare `next week` (a `next week` from an old
note is usually now in the past), and compute all `X days ago` / overdue / days-to-close
math from absolute dates vs today (not carried-forward relative numbers). Living memory
must store facts with their ABSOLUTE date (YYYY-MM-DD).

**Why / how to work with it.** Sweeps were echoing stale relative time —
"demo 15 May, Horizon next week" read as future on 23 Jun when the "next week" came from
a 16 Jun note and is now past. This is in the **ground-truth block** (always injected),
so it holds regardless of the Supabase system prompt. The matching UI fix (compute
"X days ago"/overdue labels from the absolute date + today, not echo the agent's number)
is tracked in the frontend spec (`MASE_Deal_Card_Section_Definitions.md` G8).

## 2026-06-23 — PRODUCTION sweep repointed to the datalake (env-flagged, live-Avoma fallback)

**What.** The production sweep now reads Avoma from the **datalake** (whole call history,
no 90-day clip) instead of live Avoma. Mechanism: `analyze_one()` resolves
`avoma_from_datalake` from env `DEAL_SWEEP_AVOMA_FROM_DATALAKE` (now `=true` in both the
api + worker task-def templates in `deploy.ps1`) when a caller doesn't force it. Per-deal
**fallback**: if the datalake has no calls for an opp (not yet backfilled / webhook
missed it), the prefetch falls back to LIVE Avoma so a deal is never falsely read as dark
(`[DEAL-SWEEP] datalake empty opp=… -> live Avoma fallback`). Worker `LLM_REQUEST_TIMEOUT_S`
raised 600→1200 for the larger datalake prompts.

**Why / how to work with it.** The A/B test proved it: on 5 already-swept deals the
datalake materially improved 3 verdicts (two escalated to **critical** — a hidden
Economic-Buyer gap and an IT-freeze blocker the 90-day clip hid) and rescued 2 deals that
live Avoma read with **zero** calls. **Roll back** by setting
`DEAL_SWEEP_AVOMA_FROM_DATALAKE=false` (edit the `deploy.ps1` templates + redeploy) — no
code change needed. Tell datalake vs live in logs by the avoma-engine line:
`window=alld` = datalake, `window=90/270/540` = live. The datalake stays current via the
Avoma AINOTE webhook (tracked-opp gated); see `docs/MASE_CONTEXT.md`.

## 2026-06-23 — Datalake-sourced Avoma sweep (complete-units, no sliced transcripts) + async A/B endpoint + durable env

**What.**
- New Avoma source for the sweep: `_avoma_prefetch_from_datalake()` in `deal_engine_sweep.py`
  reads a deal's **entire** Avoma history from the `datalake` Supabase project in one SQL
  read (no 90-day clip, no 12-read cap) and builds the **same manifest** the live path
  produces, so `_avoma_prefetch_block()` renders it to the agent unchanged. Selected when
  `analyze_one(..., avoma_from_datalake=True)`.
- **Complete-units rule:** transcripts are inlined **whole or not at all — never sliced
  mid-call.** Every call carries its **complete Avoma AI-notes** (whole-call summary);
  verbatim full transcripts go to the most-recent calls within a char budget
  (`DEAL_SWEEP_AVOMA_DL_TRANSCRIPT_BUDGET`, default 80000). Every call is listed as a
  touchpoint, so the agent can never falsely report "gone dark."
- **Async A/B endpoint** `POST /api/deal-engine/sweep/{opp_id}/datalake-test` — spawns a
  detached `dry_run` datalake-sourced sweep (no persist) and writes the verdict to
  datalake `ab_test_results`; returns `started` instantly. Async because a 9-min sync
  request is killed by the corporate proxy mid-run.
- **`deploy.ps1` durable env:** the datalake/SNS env + `mase/datalake` secret, and the
  API sweep tuning (`LLM_REQUEST_TIMEOUT_S=1200`, `ANTHROPIC_MAX_RETRIES=8`,
  `DEAL_SWEEP_MAX_TRANSIENT_RETRIES=50`, `DEAL_SWEEP_MAX_TOKENS=64000`,
  `MCP_TOOL_TIMEOUT_S=600`) are now in the task-def template, so they survive every deploy.

**Why / how to work with it.** Live Avoma's 90-day recency clip silently dropped older
calls (Mair Group: 7 of 14). The datalake gives the agent the **whole, complete** call
history without fragments. Budget is moderate (not "all transcripts") because inlining
15+ full transcripts pushed one LLM generation past 600 s → `APITimeoutError`; 80 KB ≈ ~8
full transcripts + complete notes for the rest. Full operating context (datalake,
webhook, AWS, deploy hazards) is in **`docs/MASE_CONTEXT.md`** — read it before touching
prod. Production sweep still uses live Avoma; repoint it to the datalake deliberately
once the A/B comparison proves quality.

## 2026-06-22 — Fireworks AI models (super-admin sandbox) routed through the agent backend

**What.** Added a `fireworks:` provider branch in `server.py` (`initialize_agent`, alongside
anthropic/google/grok): any model id prefixed `fireworks:` (e.g.
`fireworks:accounts/fireworks/models/gpt-oss-120b`) is built as a `ChatOpenAI` against the
Fireworks OpenAI-compatible endpoint (`https://api.fireworks.ai/inference/v1`), keyed by
`Config.FIREWORKS_API_KEY` (env `FIREWORKS_API_KEY`, injected from Secrets Manager `mase/app-env`).
A dedicated `FIREWORKS_MAX_TOKENS` (default 32000) keeps the 8192 Anthropic-sized
`MAX_OUTPUT_TOKENS` from truncating gpt-oss reasoning turns. Surfaced from VIBE as a
super-admin-only model-picker option (normal **and** project chats).

**Why / how to work with it.** Lets us A/B Fireworks-hosted open models (gpt-oss-120b/20b active;
kimi/deepseek/qwen3 seeded inactive pending account access) without a separate calling path — same
agent loop, tools, streaming. Keys are **env-only**: the backend never reads the request `api_keys`,
so the VIBE admin `fireworks_api_key` Supabase row is a record/rotation surface only — to change the
operative key, update the AWS secret `mase/app-env`. Super-admin gating is enforced **VIBE-side**
(`/api/chat` provider gate + picker filter); the backend trusts it (no role check). Only `/api/chat`
(create_deep_agent) understands `fireworks:` — the deal sweep / analyzer / AI-columns resolvers do
NOT, so never set a fireworks id as their model.

---

## 2026-06-19 — RevOps chat goes streaming/realtime (VIBE pattern); fixes the proxy timeout

**What.** The tool-using RevOps chat can run for tens of seconds to minutes (search_knowledge +
the run_todo sub-agent), which blew past the Vercel proxy's function timeout when run behind the
blocking `/api/deal-engine/chat` — the UI saw it as "the chat is failing." Fixed by moving the
chat onto the **streaming/realtime path** that VIBE uses:
- **New `POST /api/deal-engine/chat/async`** (`server.py`): builds the SAME book + editable prompt
  (`_CHAT_CAPABILITIES`) as the sync endpoint, builds the tool-using agent
  (`deal_engine_chat_agent.build_chat_agent`), then spawns `run_agent_and_save(chat_id, conv,
  agent, model, MASE_KNOWLEDGE_PROJECT_ID)` as a **tracked background task** (`_running_tasks`,
  slot reservation + cleanup callback — mirrors `/api/chat/async`) and returns **fast JSON
  `{chat_id}`**. The agent's thinking / tool_call / tool_result / final stream into the shared
  `chat_messages` table; the browser subscribes over Supabase realtime. Nothing blocks the proxy →
  no timeout. On agent-build failure it writes an `error` row and still returns `{chat_id}`.
- The blocking sync `/api/deal-engine/chat` (fast one-shot) stays as a fallback/compat endpoint.

**Why / how to work with it.** This is the correct home for the KB + run_todo delegation — long
runs stream instead of timing out, and the live thinking/tool trace powers the chat UI's
"Agent working…" accordion. Note `run_agent_and_save` also fires the verifier hook (advisory,
background) keyed off project_id; the MASE marker isn't in the lake-diagnosis set so that's
skipped. Frontend rewired to realtime in the same date's MASE-frontend changelog.

**Nested Todo-Runner trace (follow-up, same day).** `deal_engine_chat_agent.build_chat_agent`
now takes an optional async `emit(type, content, meta)`; `_run_todo` STREAMS the Todo Runner
(`agent.astream(stream_mode="values")`, mirroring `_agent_astream_autocontinue`'s
extraction/dedupe) and emits its own thinking/tool_call/tool_result tagged `{"group":"todo"}`.
`/api/deal-engine/chat/async` passes `emit = save_to_supabase(chat_id, …, {"group":"todo"})`,
so the Todo Runner's internal steps stream into the SAME `chat_id` (sequenced between the
parent's run_todo tool_call/tool_result) and the UI renders them as a nested sub-accordion. With
no `emit` the cheap blocking `ainvoke` path is kept (tests / non-streaming callers).

## 2026-06-19 — Chat agent: tool-using (shared knowledge base + Todo Runner delegation)

**What.** `/api/deal-engine/chat` (the RevOps chat over the book) was a tool-less one-shot
OpenAI completion. It is now a **tool-using deep agent** (`deal_engine_chat_agent.py`,
`build_chat_agent`) that:
- **shares the MASE knowledge base** — it has `search_knowledge` routed to the isolated MASE
  namespace (`MASE_KNOWLEDGE_PROJECT_ID`), the same store the sweep + todo-runner use, and
- **can delegate to the Todo Runner** — a `run_todo(task, account?, contact?, opportunity_id?)`
  tool runs the Todo Runner as a SEPARATE deep agent (its own Supabase prompt
  `mase_todo_runner` + Salesforce/Avoma/Showpad/knowledge tools, MASE rag namespace, own
  chat_id) and returns the draft (or a `NEEDS HUMAN:` line). Mirrors the sweep's
  independent-agent pattern (`create_deep_agent` + `_oa._build_model` + `_oa._final_text`).
- **uses the admin-editable prompt** — the base prompt now comes from Supabase `ID_CHAT`
  (fallback `_DEAL_ENGINE_CHAT_SYSTEM`); the book + a fixed `_CHAT_CAPABILITIES` block
  (describing exactly what the Todo Runner can/can't do) are appended by code. Previously the
  `/chat/prompt` editor wrote a key the chat ignored — now it actually drives the chat.

**Why / how to work with it.** Fulfils "chat shares the KB + can call the Todo Runner + its
prompt is editable in Admin." The agent path is wrapped in try/except and **falls back to the
original one-shot completion** if the agent stack/tools aren't available, so the chat can't
hard-break. Tunables: `DEAL_CHAT_RECURSION_LIMIT` (40), `DEAL_CHAT_TIMEOUT_S` (300),
`CHAT_TODO_RECURSION_LIMIT` (60), `CHAT_TODO_TIMEOUT_S` (300). Edit the chat prompt at Admin →
Agent Control → **Chat Agent** (`/api/deal-engine/chat/prompt`, key `mase_chat_agent`).

## 2026-06-19 — Knowledge uploads: large files via S3 (no size limit)

**What.** Knowledge-base file uploads no longer go through the Vercel proxy as a
base64 JSON body (capped at ~4.5 MB on Vercel serverless). The browser now uploads
the raw file **directly to S3** via a presigned PUT, then registers it with the
backend, which pulls the object from S3 and extracts the text. Effectively no
file-size limit (S3 single-PUT supports up to 5 GB).
- **New endpoint** `POST /api/deal-engine/knowledge/presign` (`server.py`
  `mase_knowledge_presign`): returns `{url, key}` — a presigned PUT to bucket
  `mase-knowledge-uploads-022187637784` under `uploads/<uuid>/<safe-name>`. Admin-gated
  at the proxy (path starts with `knowledge`).
- **`POST /api/deal-engine/knowledge`** now also accepts `s3_key` (+ `filename`):
  downloads the object (`_s3_download`), extracts via the new `_extract_text_from_bytes`
  (refactored out of `_extract_text_from_file` so the inline-base64 and S3 paths share
  it), then **deletes the temp object** (`_s3_delete`). The old `file_b64` inline path
  still works for small/legacy callers.
- **Extraction caps raised + env-configurable** (`server.py`): `MASE_MAX_UPLOAD_BYTES`
  (default **200 MB**), `MASE_MAX_EXTRACT_CHARS` (4 M), `MASE_MAX_PDF_PAGES` (5000),
  `MASE_MAX_FILE_B64` (~210 MB). Bucket via `MASE_KNOWLEDGE_S3_BUCKET`, region via
  `AWS_REGION`/`AWS_DEFAULT_REGION` (fallback `ap-south-1`), presign TTL
  `MASE_PRESIGN_EXPIRY_S` (900s).
- **Dependency:** added `boto3` to `requirements.txt`.

**Infra (prod, ap-south-1, acct 022187637784).** New private bucket
`mase-knowledge-uploads-022187637784` with CORS (PUT/GET, any origin — the presigned
URL is the gate) and a 1-day lifecycle expiry on `uploads/`. New inline policy
`mase-knowledge-s3` on `mase-ecs-task-role` granting `s3:PutObject/GetObject/DeleteObject`
on that bucket only (additive — does not touch existing SQS/secrets perms).

**Why / how to work with it.** The Vercel proxy body cap made multi-MB sales decks
impossible to upload; routing the bytes around the proxy (browser → S3 → backend) was
the only way to truly remove the limit (Supabase Storage has its own limits, and the
ALB is HTTP-only so the HTTPS frontend can't post to it directly — mixed content).
Frontend: `app/(dashboard)/admin/page.tsx` `DocumentsSection` now PUTs the raw `File`
to S3 (no client-side base64 read) and removed the 15 MB cap.

## 2026-06-18 — Reliability batch: MCP tool timeout, pooled store HTTP + retries, graceful drain

**What.** Three reliability hardening changes (from the enterprise-readiness audit;
adversarially reviewed before ship):
- **MCP per-tool timeout** (`server.py` `_wrap_mcp_tool`): every async MCP tool call is
  bounded by `asyncio.wait_for` (`MCP_TOOL_TIMEOUT_S`, default **300s** for API; the
  worker sets **600s** in `deploy.ps1`). A hung subprocess returns `{status:failed}` so
  the agent recovers instead of pinning a run to the ~660s watchdog. The default sits
  above a worst-case legit Avoma call (~180s) so it won't cut valid reads.
- **Pooled store HTTP + idempotency-safe retries** (`analysis_store.py`,
  `deal_engine_store.py`): one shared `httpx.Client` (keep-alive) + `_request()` with
  bounded jittered retries. Connection errors retry on any verb; read/write-timeout /
  5xx / 429 retry **only** for idempotent verbs (`select`/`upsert`/`patch`/`delete`);
  `insert` never retries on a maybe-landed error → **no double-writes**. Tune with
  `STORE_HTTP_RETRIES`.
- **Graceful shutdown drain** (`server.py` `shutdown_event` + `deploy.ps1`
  `stopTimeout:120`): on SIGTERM, give in-flight runs a grace window
  (`SHUTDOWN_DRAIN_GRACE_S=15`) then **cancel** stragglers so each run's OWN finally /
  cancel handler writes its single terminal row — chats no longer hang on "Thinking…"
  after a deploy. We do NOT inject a terminal row (that would double-write / violate the
  one-terminal-row contract).

**Why / how to work with it.** Targets reliability ("all systems working"), not scaling
or security. No behaviour change intended. The drain depends on graceful SIGTERM +
`stopTimeout`; a hard SIGKILL (OOM) still needs the cross-instance run reconciler (P1.1
follow-up in `docs/enterprise-readiness.md`). Adversarial review caught the original drain
design double-writing terminal rows — fixed to cancel-based.

## 2026-06-18 — Enterprise-readiness audit + roadmap (docs/enterprise-readiness.md)

**What.** Added `docs/enterprise-readiness.md`: a prioritized P0/P1/P2 roadmap (from a
multi-agent code audit, 53 grounded findings) for scaling to ~1000 concurrent users.

**Why / how to work with it.** MASE is NOT yet ready for 1000 concurrent users. Two
failure classes dominate: (1) process-local state breaks across multiple ECS tasks
(duplicate runs, sequence collisions, duplicate crons), and (2) no cluster-wide LLM
governor → the fleet stampedes Anthropic OTPM 400k. Plus fail-open auth + anon SELECT on
`deal_records`. **Before adding features at scale, work the P0 list.** Keep the doc updated
as items land.

## 2026-06-18 — Agent onboarding: AGENTS.md + CLAUDE.md + auto-surfaced changelog on pull

**What.** Added `AGENTS.md` (the operating guide coding agents auto-load) and a short
`CLAUDE.md` pointer at the repo root, with copy-paste prompts (session catch-up,
post-pull "what changed", pre-commit wrap-up). Enhanced `scripts/post-merge.sh` to print
the CHANGELOG.md lines added by a `git pull`.

**Why / how to work with it.** So every agent (and teammate) understands the changes that
come with each push/commit. **Start every session by reading `AGENTS.md` then
`CHANGELOG.md`.** Install the hook once: `cp scripts/post-merge.sh .git/hooks/post-merge
&& chmod +x .git/hooks/post-merge` — then each pull prints what changed. When you make a
behaviour change, append a CHANGELOG entry (the wrap-up prompt in AGENTS.md reminds you).

## 2026-06-18 — System prompts now live in Supabase (Supabase is the SOURCE OF TRUTH)

**What.** The two MASE agent system prompts are now stored in, and served from,
Supabase — not the local `prompts/*.md` files:

| Agent | Supabase row (`public.jarvis_settings.id`) | Edit it from |
| --- | --- | --- |
| Deal Intelligence Engine **sweep** | `mase_deal_sweep` | Admin → Agent Control → **Deal Sweep** (or `POST /api/deal-engine/sweep/prompt`) |
| **Todo Runner** ("Run with AI" Tactical Fulfillment) | `mase_todo_runner` | Admin → Agent Control → **Todo Runner** (or `POST /api/deal-engine/todo-runner/prompt`) |

Both rows are seeded with the current prompt text and read at runtime via
`agent_prompt_store.get_prompt(<id>)`. The chat agent key `mase_chat_agent` already
worked this way.

**Why.** So the prompts can be edited live by admins without a code change/redeploy,
and so there is ONE authoritative copy. The deal-sweep agent re-resolves the prompt
on a 15s TTL and rebuilds when its fingerprint changes (`deal_engine_sweep._get_agent`);
the todo-runner fetches it per run from the frontend (`components/agent/AgentRun.tsx`).

**How to work with it going forward.**
- ✅ To change an agent's behaviour, **edit the Supabase prompt** (via the Admin UI or
  the endpoint above). Supabase ALWAYS wins.
- ⚠️ Do **NOT** edit `prompts/deal_engine_sweep_system_prompt.md` or
  `prompts/todo_runner_system_prompt.md` to change live behaviour. They are now only
  the **cold-start SEED / fallback** (used only if the Supabase row is missing) and
  carry a `⚠️ DEPRECATED` banner at the top. That banner is a leading HTML comment
  stripped at load (`agent_prompt_store.strip_leading_banner`) so it never enters the
  prompt. If you intentionally improve the seed, mirror the change into Supabase too.
- The Admin editor's **"Reset to default"** clears the Supabase override and falls
  back to the seed — that's the only path back to the on-disk version.
- See `.agents/memory/prompts-source-of-truth.md`.

## 2026-06-18 — Admin → Execution shows two separate run feeds

**What.** The Admin → Execution tab now lists **Deal Sweep runs** (worker status +
`/api/deal-engine/trigger-logs`) and **Todo Runner runs** separately. The latter is a
new endpoint `GET /api/deal-engine/todo-runner/runs` that identifies "Run with AI"
runs by their seed user-message in the shared `chats`/`chat_messages` tables (no
schema change) and derives each run's status (draft_ready / needs_human / error /
running). Admin-gated at the Next.js proxy.

## 2026-06-18 — Agent doc upload hardened

**What.** `POST /api/documents/upload` (Admin → Knowledge) accepts PDF/DOCX (`file_b64`
+ `filename`) and `doc_type`; extraction runs off the event loop with a 120s timeout
and is bounded (size/pages/chars). Endpoint is no longer in the public allowlist.
