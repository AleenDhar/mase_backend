# The CRO Scoring Doctrine — Win Position & Deal Momentum

> **Whose document this is.** The revenue leader's contract with the two numbers the whole
> team runs on. If a score can't survive this doctrine, the score is wrong — not the deal.
> Sources of authority: the locked Omnivision instructions (`win`, `mom` in
> `scoring_instructions`), the deterministic scorer (`deal_engine_scoring.py`), and
> `docs/deal-scoring-logic.md` (full mechanics). This file is the doctrine; that file is the math.

---

## 1. The contract — what each number answers

- **Win Position (5–100):** *if this deal closes, do WE win it?* Independent of whether it's moving.
- **Deal Momentum (0–100):** *is this deal actively moving RIGHT NOW?* Independent of whether we'd win.
- They share evidence, never each other's conclusions. A stalled deal we'd win ≠ a racing deal we'd lose.
- Every score ships with its top 5–6 reasons — a mix of ✅ working and ⚠️ gaps, every gap with a ► move.
- A reason speaks the deal's facts, never the machinery. No "anchor", no "ceiling" in a rep's face.

## 2. The non-negotiables (the doctrine)

- **Buyer-voiced or it didn't happen.** A rep's "we're in the lead" is worth zero. Preference,
  selection, championing — only the buyer's words and actions count.
- **Qualification before probability.** No high win score without the boxes ticked: access to
  power, competitive visibility, a champion. Optimism doesn't skip the checklist.
- **Direction beats volume.** Ten meetings on a deal being renegotiated DOWN is not momentum —
  it's a busy funeral. Activity is discounted when the deal's substance is declining.
- **History is not a plan.** Past milestones prove nothing about the next 90 days. Plan credit
  is for dated, future, near-term buyer milestones only.
- **Symmetric reading.** If an upgrade adds points, a downgrade must subtract them. Any term
  that can only ever add is an inflation machine.
- **Evidence beats the rollup; the CRM fact out-votes an unbacked inference.** A "confirmed EB"
  with no evidence and `eb_identified=false` in Salesforce is a gap, not a confirmation.
- **Absence is UNOBSERVED, not good news.** Unknown competitors are a ⚠️ blind spot, never an
  "edge". An uninstrumented deal is not "dark" — and not "alive" either.
- **No silent ceiling breach.** Crossing a stage ceiling requires hard physical evidence, a
  written exception statement, and a nudge to fix the system of record. Otherwise the ceiling stands.
- **Terminal is terminal.** A voiced loss or dead stage forces Win 0 / Momentum 0. Activity
  before the verdict stopped mattering when the verdict landed.

## 3. Win Position — how the number is earned

**Shape:** stage anchor → ±30 rubric → momentum coupling → CRM-trend nudge → folds → **capped
by the lowest ceiling that applies**.

- **Stage anchor (the prior):** Initial Interest 8 · Qualified 18 · Formal Eval 35 ·
  Shortlisted 55 · Vendor Selected 72 · Negotiation/Contract 85 · Signed 95 · PO 98.
- **Stage ceilings (structure beats sentiment):** pre-RFP caps at **30** · RFP round (Formal
  Eval / Shortlisted) caps at **70** — *crossing 70 means the buyer picked us* · post-selection may reach 100.
- **Qualification ceilings (the gate that ended the 99s):** economic buyer gap → **52** /
  partial → 74 · competition unmapped → 66 · champion gap → 60. Lowest binds. Post-selection SF
  stages release the gate — the stage itself is the proof.
- **The rubric (±30):** differentiation 20 · preference 20 · champion 15 · exec access 15 ·
  competitive 15 · business case 10 · commercial 5. Missing evidence = mild negative (−0.3):
  *we haven't proven it.*
  - An **at-risk champion** can never score better than partial (0.3), whatever the prose says.
  - A **keyword-only preference** ("in the lead") caps at moderate (0.5) on a declining deal.
  - **Exec access:** full credit = direct EB face time; indirect-but-real involvement
    (CEO reviewed the POC on a mandated project) earns partial, never full.
- **Momentum coupling (asymmetric on purpose):** momentum below the stage's expected level
  chips Win at full rate; momentum above adds at half rate. Heat helps; cold hurts more.
- **CRM-trend nudge (±8):** stage/forecast up, amount growth → up; downgrades and close-date
  pushes → down. Forecast ranks are Omitted < Pipeline < Upside < Best Case < Commit — a
  Best-Case→Upside move is a downgrade and reads as one.
- **Selection override (the only elevator):** fires ONLY with all six — stage ≥ Formal Eval,
  EB confirmed, verdict not slowing, buyer-voiced high preference, a positive competitive edge,
  and a real won/Commit signal. **Best Case is upside, not a selection.**
- **Folds:** honest Commit +7 / Best Case +4 (only when the category is evidence-consistent) ·
  scope shrink −7 · sibling Closed-Won or strong live sibling +10 (capped by own stage).
- **Risk is not double-charged here** — Position is a pure "do we win" read; risk lives in Deal
  Risk and Forecast Confidence.

## 4. Deal Momentum — how the number is earned

**Shape:** base 50 → + engagement (cap ~35) → ± direction → − decline terms → guards.

- **Engagement is graded by depth, not count:** POC 10 · pilot 9 · ROI/procurement workshop 8 ·
  reference call 7.5 · InfoSec/legal 7 · on-site/RFP 6 · deep-dive 5 · standard demo 3 ·
  kickoff 2 · discovery 1.5. Buyer/two-way full weight; a rep email into silence 0.4. Recency
  decays it; RFP deals get a stretched process clock.
- **Declining-deal discount:** the same meetings are worth **×0.82** when the deal is declining
  on one axis (forecast downgraded OR amount cut >20%), **×0.66** on both. A close slip alone is
  timing, not decline. *Busy ≠ healthy.*
- **Direction is symmetric:** recent stage/forecast up-move **+6** · downgrade or stage
  regression **−10** (and it dominates a stale up-move).
- **Scope cut:** amount renegotiated down >20% → **−6**. A deal getting smaller is not advancing.
- **Stalling drag (0 to −25):** quiet measured against the stage's expected buyer cadence
  (30d early · 21d Formal Eval · 18d Shortlisted · 14d Vendor Selected). Uninstrumented ≠ dark —
  mild drag only. Suspended inside a live RFP process.
- **Close-date tolerance:** a push ≤60 days is free (dates slip); beyond that, up to −10;
  a pull-in earns up to +5.
- **Plan credit (+8/+11) is for the FUTURE:** only dated milestones ahead of today and within
  90 days count. Halved when the buyer's been quiet >30 days outside a process. **Substance
  gate:** near-dead engagement + rep confidence <35% → typed-in dates are theatre, credit zero.
- **Process-mode (RFP/tender):** deliverables ARE engagement; quiet between deliverables is
  cadence, not stalling; floored at 50 while genuinely on-track. **Anti-zombie:** a deadline
  that passed in silence ends the exemption immediately.
- **False-velocity cap:** a busy Next-Step journal on a slipping, unengaged, process-less deal
  caps at **25**. Writing in the CRM is not momentum.
- **Sibling wrap:** a strong live sibling on the account can lift (+12 max), never carry, the number.

## 5. The hard caps — quick reference

| Situation | Max the score can read |
|---|---|
| Pre-RFP stage (any signals) | Win ≤ 30 |
| Formal Eval / Shortlisted, no selection | Win ≤ 70 |
| Economic buyer not established | Win ≤ 52 (partial: 74) |
| Competitive field unmapped | Win ≤ 66 |
| No champion | Win ≤ 60 |
| Busy CRM journal on a slipping deal | Momentum ≤ 25 |
| Deal declining on forecast + amount | engagement worth 2/3 |
| Terminal loss / dead stage | Win 0 · Momentum 0 |

## 6. What a HIGH score must have behind it

- **Win ≥ 70:** buyer-voiced selection or post-selection stage + confirmed EB + mapped
  competitive field + a live champion. All of it. One missing → the gate holds it under.
- **Momentum ≥ 80:** recent (≤30d) deep, two-way buyer engagement + no downgrade/scope-cut on
  the books + a dated near-term buyer milestone. High-volume shallow activity won't get there.
- If the panel's own bullets describe decline while the number reads hot — the number is wrong;
  audit it (§8).

## 7. Governance — where this doctrine physically lives

- **Omnivision (`/omnivision`) is the control plane.** Five locked, versioned instructions
  (extract / win / mom / todo / sum) govern how the sweep reads deals. Locked versions only —
  drafts are invisible to the runtime. Locking requires a changelog note. Super-admin only.
- **Adoption is automatic:** a lock reaches the live sweep in ≤ ~5 minutes (no deploy). Every
  swept record is stamped `ai.scoring_studio.versions` — every number traces to the exact
  instruction versions that produced its reading.
- **The arithmetic stays deterministic.** The LLM reads evidence under the locked instructions;
  `deal_engine_scoring.py` computes the number. Nobody — human or model — hand-waves a score.
- Current locked set (2026-07-08): extract 10.3 · win 10.3 · mom 10.2 · todo 10.1 · sum 10.1.
- The handoff's stricter calibration (RFP ceiling 60, momentum base 35, VS+ ≤85) is catalogued
  in `docs/scoring-studio-gap-analysis.md` §2 — adopting any of it is a locked version bump +
  a book rescore, never a silent tweak.

## 8. The 60-second audit (how I check any number)

1. Open the record's `ai.deal_scores.<score>.contributions` — every point with its evidence.
2. Win: anchor + lift + momentum_adj + trend + folds, then `min(raw, lowest ceiling)`. Which
   ceiling bound it? Was the qualification gate active?
3. Momentum: base 50 + each term. Compare engagement points against the decline terms.
4. **Cross-check narrative vs number.** Reasons describe decline + score reads hot = an
   over-read source field. That's the inflation pattern; find the generous signal.
5. Check the stamp: `ai.scoring_studio.versions` — which locked instructions governed the read.

---

*Companions: `docs/deal-scoring-logic.md` (complete mechanics + inflation-pattern audit table) ·
`docs/scoring-studio-gap-analysis.md` (Omnivision integration + calibration deltas) ·
`docs/zycus-deal-progression-playbook.md` (what good looks like at each stage).*
