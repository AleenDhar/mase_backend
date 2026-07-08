# Scoring Version Studio ("Omnivision") — gap analysis & integration plan

> **What this is.** The reconciliation between the **MASE Scoring Studio staging handoff**
> (five versioned, lock-before-run engine instructions at v10.x) and **how MASE actually
> works today**, plus the integration plan. Phase 1 (the control plane) is BUILT — this doc
> records the mapping and what remains.

## 1. The five engines — handoff vs current MASE

| Handoff engine (latest locked) | Where the equivalent lives in MASE today | Form |
|---|---|---|
| **Signal Extraction / Deal-Reading v10.3** | Inside the one big `mase_deal_sweep` Supabase prompt (§1 operating rules, §2 read plan, §2.10B reading rules, §4.5 living memory) + deterministic prefetch code (`_avoma_prefetch_from_datalake`, footprints, roster injection) | prompt + code, not separated |
| **Win Position v10.3** | `deal_engine_scoring.score_win_position` — anchors/ceilings/rubric/gates as **Python constants** | **hardcoded code** |
| **Deal Momentum v10.1** | `deal_engine_scoring.score_momentum_v2` (engagement_v5) | **hardcoded code** |
| **To-Do Generation v10.1** | `mase_deal_sweep` prompt §4/§4.6 (recommendation engine, horizons, MECE clubbing) + `mase_revops_head` | prompt (bundled) |
| **24-Hour Summary v10.1** | `mase_deal_sweep` prompt §2.11 + standalone `day_summary_ai.py` (Sonnet) | prompt (bundled) + script |

**Core structural gap:** the handoff treats Win/Momentum *logic* as editable, versioned
instructions; MASE computes them in deterministic Python (changed via code + deploy). The
handoff's own hybrid model resolves this: *LLM extracts typed signals → deterministic code
computes the number → LLM narrates* — i.e. the instructions govern the LLM stages, the
arithmetic stays code.

## 2. Calibration deltas (handoff v10.x is MORE conservative than current code)

| Parameter | Handoff | Current code |
|---|---|---|
| Win ceilings | pre-RFP ≤35 · RFP ≤**60** · VS+ ≤**85** (cross 85 only on Commit) | 30 · **70** · **100** |
| Shortlisted anchor | 50 | 55 |
| Momentum start | **35** | **50** (base) |
| Momentum engagement cap | +50 | ~+35 |
| Forecast-conviction ceiling | not BC/Commit → ≤80 | Commit +7 / BC +4 credit, no ≤80 cap |
| Selection override | buyer-voiced ONLY, **never breaches a ceiling** (except the documented §5.5 stage-reality override with exception statement + seller nudge) | stage+EB-gated (2026-07-08), unlocks ceiling when it fires |

Direction agrees with this session's anti-inflation fixes (the handoff's 24h-Summary v10.1
even cites the Austrian Post miss; its §5.3 matches the shipped buyer-voiced-selection rule).
**Adopting the handoff numbers is a rescore-the-book decision — do it via a locked version
bump, not silently.**

## 3. What was BUILT (Phase 1 — the control plane, 2026-07-08)

- **Store:** Supabase `scoring_instructions` (engine, version, kind, required note, content,
  locked/locked_by/locked_at; unique engine+version) + `deal_outputs` (provenance stamps).
  Seeded with all five engines' full trails; latest locked: extract 10.3, win 10.3, mom 10.1,
  todo 10.1, sum 10.1. Script: `scripts/scoring_studio_schema.py` (idempotent).
- **Backend API** (`scoring_studio.py` + `server.py` endpoints under
  `/api/deal-engine/scoring-studio/*`): engines list · trail · version content · save/discard
  the **single unlocked draft** · **lock** (requires kind + changelog note; computes next
  semver; stamps locked_by/at) · `active` (runtime resolver: latest locked per engine — drafts
  invisible).
- **Frontend** (MASE repo, `staging` branch): route **`/omnivision`** — engine cards with
  active-version + draft badges, version trail with changelog, read-only version viewer,
  draft editor, lock modal (minor/major + required note), draft-blocks-run banner.
- **Access:** **SUPER-ADMIN only** — `SUPER_ADMIN_EMAILS` = { aleen.dhar@zycus.com,
  sam.thomas@zycus.com } (strict subset of `ADMIN_EMAILS`); `isSuperAdminView` in
  DashboardContext; sidebar `superOnly`; and the REAL gate in the deal-engine proxy
  (`callerIsSuperAdmin()` on GET/POST/DELETE of `scoring-studio/*`).

## 4. What REMAINS (Phase 2 — runtime adoption; NOT built)

1. **Lock-before-run gate in the sweep/worker:** before each engine stage, resolve
   `scoring_studio.active_locked()`; refuse to run an engine with no locked version. (Today
   the sweep still reads the monolithic `mase_deal_sweep` prompt via `agent_prompt_store`.)
2. **Consume the instructions:** split the sweep so the extraction / to-do / 24h stages are
   prompted by their locked instruction; Win/Momentum stay deterministic but their
   signal-extraction + rationale narration adopt the locked win/mom texts (hybrid model).
   Decide per-parameter whether to adopt the handoff calibration (§2) — each adoption = a
   locked version bump + book rescore.
3. **Provenance stamps:** write `instruction_version` + `extraction_version` on every
   score/to-do/summary output (`deal_outputs` and/or `ai.deal_scores.provenance`).
4. **Part A/B split enforcement** for extraction (Part B connector contract read-only in the
   editor), acceptance tests as regression fixtures, and Studio audit-log of who locked what.

## 5. Operating rules (already true in Phase 1)

- Editing NEVER touches the live engine: a save creates the engine's single **unlocked
  draft**; the runtime resolver ignores drafts by design.
- **Locking requires a changelog note** — the "why did this number move" audit trail.
- Supabase remains the single source of truth (same doctrine as `jarvis_settings` prompts);
  the on-disk handoff text is a cold-start seed only.
