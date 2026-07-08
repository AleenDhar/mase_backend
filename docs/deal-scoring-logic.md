# MASE Deal Scoring — complete logic reference

> **What this is.** The full, current logic of the deterministic scorer (`deal_engine_scoring.py`,
> with `deal_engine_trends.py` for CRM-move signals and `deal_engine_footprints.py` for engagement).
> It documents every one of the five scores, every factor, every cap and gate — and, because the
> recurring concern is *"the scorer goes easy on deals and inflates them,"* it calls out **where each
> score can inflate** and the guards that catch it. No LLM computes these numbers; the model only
> supplies evidence — the arithmetic here is deterministic and auditable.
>
> Anchor: `compute_deal_scores(record)` in `deal_engine_scoring.py`. Runs inside the sweep after the
> analysis, writes `ai.deal_scores`. Never raises (returns `{}` / `{error}` on failure).

---

## 0. The five scores (and how they relate)

| Score | Question | Range | Built from |
|---|---|---|---|
| **Win Position** | *If it closes, do we win?* | 5–100 | stage anchor + rubric + momentum coupling + trend, under a stage/qualification ceiling |
| **Deal Momentum** | *Is it moving?* | 0–100 | base 50 + engagement − decline (stall, downgrade, scope cut) |
| **Customer Commitment** | *How much has the buyer invested?* | 8–100 | saturating sum of buyer-action signals |
| **Deal Risk** | *What could kill it?* | 0–100 | saturating sum of observed risk signals |
| **Forecast Confidence** | *Will it close in the forecast window?* | 0–99 | Win, nudged by commitment/momentum, dragged by risk |

**Terminal states** short-circuit everything: an explicit in-call **loss** (or a dead/lost/omitted
stage) forces Win 0 / Momentum 0 with a terminal label — activity before the decision no longer
counts (`is_dead_deal`, `decision_outcome.status=="lost"`).

**Three evidence states** everywhere: positive / negative / **UNOBSERVED**. Absence of evidence
never crashes a primary score — it lowers the *Read* (coverage) and Forecast Confidence, and (for
Win rubric factors) counts as a **mild negative** (`WIN_MISSING = −0.30`, "we haven't proven it").

---

## 1. Win Position — `score_win_position`

**Formula (assembled, then clamped):**

```
raw = anchor_eff + rubric_lift + momentum_adj + scope_adj + forecast_credit
      + trend_nudge + relationship_pts − risk_pen
Win = clamp( raw , floor 5 , min(stage_ceiling, qualification_ceiling) )
```

### 1a. Stage anchor (the prior) — `WIN_STAGE_ANCHOR`

How far through buying = how much is left. Most-specific-first substring match.

| Stage | Anchor | Stage | Anchor |
|---|---|---|---|
| Initial Interest | 8 | Vendor Selected | 72 |
| Qualified | 18 | Negotiation / Contract In Progress | 85 |
| Formal Evaluation | 35 | Contract Signed | 95 |
| Shortlisted | 55 | PO Received | 98 |

*(default 35 for an unrecognized stage.)*

### 1b. Stage ceilings (the hard cap) — `WIN_STAGE_CEILING`

You cannot be highly confident of winning until the buyer is structurally committed.

- **Pre-RFP** (Initial Interest, Qualified) → **max 30**
- **RFP round** (Formal Evaluation, Shortlisted) → **max 70** — *crossing 70 means you've been selected*
- **Post-shortlist** (Vendor Selected → PO) → up to **100**

### 1c. Rubric lift (±30 band) — `RUBRIC_WIN_WEIGHTS`, `_rubric_win_strengths`

A signed adjustment of up to **±30** driven by seven factors, each a signed strength in [−1, +1]:

| Factor | Weight | Read from |
|---|---|---|
| differentiation | 20 | AI-fit tier → else pain |
| preference | 20 | `customer_preference` → else positioning prose → else keyword overlay |
| champion | 15 | `champion_strength.strength` → else MEDDPICC champion |
| exec_access | 15 | MEDDPICC `economic_buyer` |
| competitive | 15 | competitive posture (see 1d) |
| business_case | 10 | `business_case` → else MEDDPICC metrics |
| commercial | 5 | MEDDPICC paper_process |

`lift = 30 × net`, where `net = clamp(Σ weight×strength / 100, −1, +1)`, blended with the CRM
opp-trend (1e). **Missing evidence = −0.30** (mild negative). Status vocabulary → strength:
confirmed/strong/high → **+1.0**; partial/developing/moderate → **+0.3**; gap/none/weak/at-risk →
**−0.5**; unknown → −0.30.

**⚠️ Where this inflates (and the guards):**
- **Keyword over-read.** `_crm_evidence_overlay` takes the *best* evidence across the LLM read and a
  Next-Step/narrative keyword scan — so a bare keyword ("in the lead", "preferred vendor") could push
  a factor to **+1.0** even with no structured signal. **Guard (2026-07-08):** preference is capped
  at **moderate 0.5** when it was maxed from a keyword, there's **no structured `customer_preference`**,
  AND the deal is **declining** (forecast/amount down).
- **Ignoring negative flags.** A "strong" champion prose label scored **+1.0** even when
  `champion_strength.at_risk == true`. **Guard (2026-07-08):** an at-risk champion caps at **partial
  0.3**.
- **Tier vocabulary gaps** (historical): an unmapped "AI Hungry" fell to the −0.4 default (read a
  racing buyer as "losing"). Fixed — the AI-appetite ladder (hungry/curious/cool) is mapped.

### 1d. Competitive strength — `_competitive_strength`

`+` only when we're the sole real option (do-nothing rival) or sole-sourced; **strong −** only when
the **buyer is leaning toward a named rival** (a preference/down-select signal, not mere presence);
roughly even (+0.2) when credible rivals are present but not preferred; mild-negative when unknown.
A **displaced incumbent** (we're replacing them) is a win signal, never a buyer-lean.

**⚠️ Inflation caught:** an unknown/even field (+0.2) is NOT "an edge" — the CRO panel renders it as
*"competitive field still unmapped"* (a ⚠️), not *"✅ edge over the competition"*.

### 1e. Opportunity-trend nudge — `_trend_nudge` (±8) + rubric blend

Recent CRM moves are buying/loss signals: stage/forecast **up** and amount **growth** nudge Win up;
a **close-date push** (the slip lives here, in Position) and downgrades nudge it down. Capped ±8,
plus a blend into the rubric net. Sourced from `deal_engine_trends.py`.

**⚠️ Inflation caught (2026-07-08):** the forecast-category rank had **Upside Key Deal above Best
Case** — so a *downgrade* (Best Case→Upside) nudged Win **up**. Fixed: `_FC_RANK` = Omitted < Pipeline
< Upside < Best Case < Commit.

### 1f. Qualification ceiling — `_qualification_ceiling` (the dominant gate)

A high Win must be **earned** by ticking qualification boxes. Win is capped at the **minimum** of:

| Gate (MEDDPICC) | confirmed | partial | gap / missing |
|---|---|---|---|
| **economic_buyer** (Access to Power) | 100 | 74 | **52** / 50 |
| competition (visibility) | 100 | 90 | 66 |
| champion | 100 | 86 | 60 / 58 |

**Post-selection stages** (Vendor Selected → PO) lift the cap to 100 — the hard SF stage proves
access. An **EB hard-flag floor** (`_eb_status_floored`): if SF `eb_identified == False` and the EB
evidence is <10 chars, a "partial/confirmed" EB is floored to **gap** (the CRM fact out-votes an
unbacked inference).

### 1g. Selection override — `_selection_override` (only raises)

A **confirmed selection whose CRM stage lags** is anchored to the Vendor-Selected floor (72) with the
100 ceiling unlocked. **Six gates, all required:** stage at/after Formal Eval; MEDDPICC
`economic_buyer == confirmed`; verdict not Slowing/at-risk; high preference; a *positive* competitive
edge (not "unknown rivals"); and a real won/Commit signal (Best Case is upside, not a selection).

**⚠️ Inflation caught:** this was the "Qualified deal reads 99" bug — the override fired on inferred
preference with no stage floor and no EB requirement. Now stage-gated + EB-gated.

### 1h. Other Win folds

- **Forecast-conviction credit** (`_forecast_conviction_credit`): a Commit +7 / Best Case +4, only
  when the call is evidence-consistent (verdict defensible or trends positive). Sandbagged/inflated
  categories get **nothing**.
- **Scope-shrink −7** (`_scope_shrink`): a deal narrowing vs prior scope (S2P→S2C, modules dropped)
  is the buyer getting defensive.
- **Relationship leverage +10** (`_relationship_context`): a sibling Closed-Won or a strong live
  sibling on the same account (advanced stage / Commit / Best Case). Capped by the deal's own stage.
- **Risk is NOT charged in Position** (2026-07-07): Position is a pure win-likelihood read; risk
  lives in Deal Risk / Forecast Confidence (charging it here double-counted).

### 1i. Momentum → Win coupling (bidirectional) — `WIN_MOMENTUM_*`

`momentum_adj = (momentum − stage_expected_momentum) × rate`, where **rate = 1.0 below** expected
(decline chips Win off fast, no floor) and **0.5 above** (motion adds muscle). The ceiling still caps
the top. Stage-expected momentum: II 48 · Q 50 · FE 52 · SL 56 · VS 60 · Neg/Contract 62 · Signed/PO 55.

**⚠️ Inflation history:** the up-rate was uncapped except by the ceiling, so a "hot" momentum could
add ~+23 and peg Win at its cap. This is why momentum quality (§2) matters to Win, and why the
momentum inflation fixes below also correct Win.

---

## 2. Deal Momentum — `score_momentum_v2` (model `engagement_v5`)

Centered on **base 50**. Momentum = engagement/activity **minus** decline. Primary terms:

### 2a. Engagement points (+, cap ≈ 35)

From `deal_engine_footprints.py`: `Σ(type_weight × who × recency_decay)` over a process-aware window
(`points_90d_process`) with a fresh-touch floor. **Engagement-depth ladder** (what each event is worth):

| Depth | Event | Depth | Event |
|---|---|---|---|
| **10** | Proof of Concept | 6 | on-site / F2F / RFP / RFI |
| 9 | Pilot | 5 | deep-dive / technical / integration demo |
| 8 | ROI / procurement workshop | 3 | standard demo / presentation |
| 7.5 | reference call | 2 | kickoff |
| 7 | InfoSec / security / legal review / redline | 1.5 | discovery / intro |

`who`: buyer/two-way = 1.0; a low-tier rep-sent email = 0.4. Two decay clocks — a fast 14/30/60-day
clock and a stretched 30/60/90-day **process clock** for RFP/tender deals.

**⚠️ Where this inflates:** engagement measures *activity volume/depth*, blind to whether the meeting
was productive or a downward renegotiation. A busy-but-declining deal earns near-max engagement — so
the **decline terms below must bite** to keep it honest. (This was the Austrian Post "90 while
everything's down" problem.)

### 2b. Direction — stage / forecast (SYMMETRIC, 2026-07-08)

- Recent **up-move** (stage advance or forecast upgrade): **+6**.
- Recent **downgrade / stage regression**: **−10** (dominates a stale up-move).

**⚠️ Inflation caught:** previously this term was **one-sided** — it only ever *added* +6, never
subtracted. A forecast cut, a stage slip = zero drag. Now a downgrade is penalized (and the
`_FC_RANK` fix means a real downgrade is *seen* as one).

### 2c. Scope / amount cut (−6, 2026-07-08)

`amount_trend < −0.2` (a deal renegotiated smaller) drags **−6** — contracting, not advancing.
(Previously momentum ignored amount entirely.)

### 2d. Stage-cadence stalling (0 … −25) — `MOM_STALL_CAP 28`

Quiet vs the stage's expected buyer cadence (II/Q 30d · FE 21d · SL 18d · VS 14d): `−min(25,
(quiet_days/cadence − 1)×12)`. An **uninstrumented** deal (no engagement data) takes only a mild
scaled drag (4/8/12), not the full −25 — *data absence ≠ buyer dark*. **Suspended in process-mode.**

### 2e. Close-date direction with tolerance — `MOM_CLOSE_WEIGHT 18`

A push **≤60 days costs nothing** (dates slip); beyond 60d drags up to −10; a pull-in earns up to +5.
The slip's *win* impact lives in Position's trend-nudge; momentum only reacts beyond tolerance.

### 2f. Next-step plan (+8 / +11) — near-term FUTURE only (2026-07-08)

`+8` for ≥1 upcoming milestone, `+3` more for ≥3 — counting only milestones **in the future AND within
a 90-day horizon** (`_plan_ms`). Halved if the buyer is quiet >30d outside a process (theatre guard).

**⚠️ Inflation caught:** previously this counted **all parsed dates** (a Next-Step *history journal*
of mostly past entries) and accepted any future date, even one after the close. Austrian Post's 44
parsed dates (42 past, 2 post-close) scored the full +11 with zero real near-term plan → pegged
momentum to 99.

### 2g. Process-mode (§8.5) — `_process_mode`

During a structured RFP/tender (stage in Formal Eval/Shortlist/Vendor Selected + a live **future**
milestone + RFP keywords + no pause), stalling drag is suspended and momentum is **floored at 50** —
quiet between deliverables is process cadence, not stalling. **Anti-zombie guard:** if the deadline
**passed in silence**, process-mode does NOT apply.

### 2h. False-velocity cap (25) + relationship wrap (+12)

- **False velocity:** ≥3 dated lines on a **slipping** deal (close pushed / confidence <40 / buyer
  quiet), AND not progressing AND not engaged AND not in a process → momentum capped at **25** (a busy
  Next-Step log on a dying deal is not momentum).
- **Relationship wrap:** 0.35 of the gap to a strong sibling's momentum, cap +12 (only raises).

---

## 3. Customer Commitment — `score_commitment`

`score = 8 (floor) + saturate(Σ strength×weight, span 92, scale 26)`. Signals (`COMMITMENT`):
customer_action_items 10 · internal_process_shared 10 · exec_access_granted 9 ·
security_or_procurement_review 9 · deep_eval_or_reference_request 8 · customer_next_meeting_request 7.
Floor 8 = "still live". Observed-only; absence doesn't penalize.

---

## 4. Deal Risk — `score_risk`

`score = saturate(Σ strength×weight, span 100, scale 30)` — **observed-only** (a risk you can't see
isn't charged). Signals (`RISK`): close_date_pushed_repeatedly 14 · stage_inflation 14 ·
competitor_preferred 13 · access_blocked 12 · customer_passivity 11 · low_buyer_intent 11 ·
next_meeting_declined 10 · open_competitive_rfp 9 · budget_frozen_or_unclear 9.

**Stage-bound (2026-07-07):** at a LATE stage (contract executing) only close-date/budget factors
count — early/mid risks are stripped so a contracting deal isn't inflated. Exception: a live
multi-vendor fight still counts.

---

## 5. Forecast Confidence — `score_forecast_confidence`

**Anchored on Win** (the stage-anchored close probability), then adjusted by execution:

```
FC = Win + 0.20×(Commitment − 50) + 0.12×(Momentum − 50) − 0.50×Risk     (clamped 0–99)
```

Coverage is a **confidence flag, not a multiplier** — a thin read means we *know* less, not that the
deal is less likely to close. Because FC anchors on Win, **every Win inflation flows into FC** — the
Win guards above are what keep FC honest.

---

## 6. Evidence Coverage / Read — `score_coverage`

`coverage = 100 × breadth × recency`, where breadth = fraction of read-dimensions with any signal and
recency decays once the last touch is past the expected cadence. Bands → label (Full / Solid /
Partial / Early). Drives the "Read" label and the FC coverage flag; never a primary-score multiplier.

---

## 7. The systematic inflation pattern (why "it goes easy") + the guards

Every inflation found this session shares one root: **the scorer read the most generous available
signal and ignored the deal's own negative flags.** Documented so it can be audited going forward:

| Pattern | Example | Guard / fix |
|---|---|---|
| Max strength off a **keyword** | preference +1.0 from "in the lead" with no structured pref | cap keyword preference at 0.5 on a declining deal (§1c) |
| Ignoring a **negative flag** | champion +1.0 while `at_risk=true` | at-risk champion caps at partial (§1c) |
| **One-sided** term (only adds) | momentum +6 for up-moves, no penalty for downgrades | symmetric direction −10 (§2b) |
| **History counted as plan** | 44 Next-Step dates (42 past) = max plan bonus | near-term-future-only (§2f) |
| **Sign / ordering bug** | Upside ranked above Best Case → downgrade read as upgrade | `_FC_RANK` fixed (§1e) |
| **Inference beats the CRM** | selection override on a Qualified deal → 99 | stage + EB gated (§1g) |
| **Absence read as edge/dark** | "unknown competitors" → "edge"; no footprints → "cold" | render as ⚠️; datalake footprints |

**Still on the watch-list (not yet changed):** the engagement cap (§2a) rewards raw meeting volume —
a deal can bank ~35 engagement points on activity alone before the decline terms net it down. If a
deal that is declining on every axis should read *lower* than "busy," the lever is to discount
engagement itself when the trend is negative (a proposed, not-yet-shipped change).

---

## 8. How to audit one deal's score

1. Pull `record.ai.deal_scores.<score>.contributions` — every factor with its points + evidence.
2. For **Win**: check `anchor`, `lift`, `momentum_adj`, `trend_nudge`, `ceiling`, `selection_override`,
   and `_qualification_ceiling(record)`. Raw = anchor+lift+momentum_adj+trend+folds; the number is
   `min(raw, ceiling)`.
3. For **Momentum**: base 50 + the listed contributions. Watch `engagement` vs the decline terms
   (regression, scope_cut, cadence).
4. Cross-check the **narrative vs the number**: if the reasons describe a losing/declining deal but
   the score is high, a source field (preference/champion/competitor status) is over-read — that's the
   inflation pattern in §7.
5. The reasons must **never** speak the machinery (no "ceiling", "anchor", "momentum lift") — those
   are internal; a reason states the deal's facts.

---

*Maintained as MASE domain knowledge. Constants live in `deal_engine_scoring.py` (win/momentum/
commitment/risk/FC), `deal_engine_trends.py` (`_FC_RANK`, `_STAGE_RANK`, opp-trends), and
`deal_engine_footprints.py` (engagement ladder, cadence). When a constant or guard changes, update
this file and `CHANGELOG.md`.*
