---
name: deal-reason-quality
description: "How deal-score REASONS are made specific/risk-inclusive/aligned, and the sweep signals the server reads (deal_scores_evidence / scope_change / expansion_context)."
---

# Deal-score reason quality + the sweep→server signal contract

The five deal scores are deterministic (`deal_engine_scoring.compute_deal_scores`). The READABLE
reasons are assembled by `deal_engine_cro.build_cro_panel`. Both the $0 Claude-Code sweep
(`cc_sweep.py`) and prod (`deal_engine_sweep.py`) call these two pure functions; the LLM AI-scorer
(`deal_engine_ai_scoring.py`, writes `ds.ai_reasons`) is **off by default and costs API money**, so
in practice it never runs — do NOT "fix" robotic reasons by turning it on.

**Where reason text comes from (in priority order):**
1. `ai.deal_scores_evidence.ai_reasons` (sweep-authored, verbatim) — OR legacy `ds.ai_reasons`.
2. Per-factor SECTION NARRATIVE via `_factor_narrative()` — champion_strength.summary,
   meddpicc.{economic_buyer,competition,paper_process,metrics,identify_pain}.narrative,
   competitive_position.summary, customer_preference.evidence, ai_fit_signal.summary. Every win
   bullet uses these (not just champion) → deal-specific, sourced, with a `full` expander.
3. Robotic `_WIN_FACTOR` label fallback (last resort).

**Rules baked in (2026-07):**
- Win block **folds in the top risks** (⚠️ bullets) — risks live in the reason, not only a separate
  block. Win math is labelled **"Why this number"** (`how_label`). `intro` prefers
  `ai.deal_scores_evidence.summary` (a deal-specific lead line).
- **Score must match the reasons.** `_crm_evidence_overlay` will NOT lift preference /
  differentiation / champion / commercial past an explicit weak/negative read (`_EXPLICIT_NEGATIVE
  = -0.4`). exec_access / business_case still lift from real structured fields. So to move a score,
  set the SOURCE fields (champion_strength.strength, customer_preference.level, competitor status).

**Sweep signals the server now reads (emit these from the prompt; they're guarded no-ops if absent):**
- `ai.scope_change = {direction:"reduced|expanded|stable", from, to, detail}` — "reduced" drags Win
  ~7 pts (`_SCOPE_SHRINK_PTS`, contribution `scope_reduced`) and raises a native CEO-monitor watch
  (`deal_engine_ceo` `_native_watch`, `type:"scope_shrink"`, ≥$250K = high).
- `ai.expansion_context = {prior_closed_won:true, ...}` — floors exec_access (second-panel into a
  won account already has access; don't score "no exec access").
- `ai.deal_scores_evidence = {summary, ai_reasons{win_position[],deal_momentum[],…}, factors{}}` —
  narrative lead + verbatim bullets + signed factor overrides (`_overlay_agent_factors`, momentum/
  commitment/risk/the 4 baseline win keys only — NOT the 7 rubric win factors).

**Prompt:** §2.10 "Deal-quality tweak pass" in the `mase_deal_sweep` Supabase prompt (apply via
`apply_reason_quality_tweaks.py --apply`, idempotent). Also encodes the email-trail parsing pipeline,
economic-buyer inference, "last conversation includes email", and plain-English "do nothing".

**24h summary:** `deal_daily_summaries` persists one row per (opp_id, summary_date). The frontend
`DealDaySummary.tsx` shows the latest row, and when today's is empty falls back to the most recent
`has_activity=true` day (with its date). Guard tests: `tests/test_deal_reasons.py`.
