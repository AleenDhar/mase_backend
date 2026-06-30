# CHANGELOG — MASE backend (`mase_backend`)

> **Agents & teammates: read this file after every `git pull`.** It is the running
> log of behaviour-changing decisions and conventions. Newest first. When you make a
> change that affects how the system behaves, where data lives, or how another agent
> should work, **add an entry here** (and, for a durable rule, a note under
> `.agents/memory/` with a line in `.agents/memory/MEMORY.md`).

Conventions for an entry: `## YYYY-MM-DD — <short title>`, then **What / Why /
How to work with it going forward**. Keep it tight; link code paths and docs.

---

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
