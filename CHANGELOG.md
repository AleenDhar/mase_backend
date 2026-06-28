# CHANGELOG — MASE backend (`mase_backend`)

> **Agents & teammates: read this file after every `git pull`.** It is the running
> log of behaviour-changing decisions and conventions. Newest first. When you make a
> change that affects how the system behaves, where data lives, or how another agent
> should work, **add an entry here** (and, for a durable rule, a note under
> `.agents/memory/` with a line in `.agents/memory/MEMORY.md`).

Conventions for an entry: `## YYYY-MM-DD — <short title>`, then **What / Why /
How to work with it going forward**. Keep it tight; link code paths and docs.

---

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
