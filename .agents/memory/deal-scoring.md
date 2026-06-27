---
name: Deal scoring (ai.deal_scores)
description: Deterministic 5-score model computed inside the sweep (deal_engine_scoring.py); hybrid factors derived from swept signals + optional agent overlay; additive, guarded, behind DEAL_SCORES_ENABLED.
---

# Deal scoring — `ai.deal_scores`

`deal_engine_scoring.py` computes **Win Position / Deal Momentum / Customer Commitment / Deal Risk** (0–100 each) + a **Forecast Confidence** roll-up + a **Read** label (Full/Solid/Partial/Early), each with a 2-sentence commentary. Runs in `analyze_one()` (deal_engine_sweep.py) right after `_revops_head_review`, before `store.upsert_record`, writing `parsed["ai"]["deal_scores"]`. Lives in the existing `deal_records.record` JSONB — **no migration**.

## Design (do not drift)
- **Arithmetic is an exact port of the offline model** (`~/Downloads/scoreModefiles/deal_scoring.py`); reference cases reconcile to the decimal (`tests/test_deal_scoring.py`). Win = baseline 50 + signed factors + saturating lift; Momentum = from flat 50, eases toward 50 on silence; Commitment = floor 8 + saturating; Risk = observed-only saturating; FC = weighted blend × coverage multiplier. **3 evidence states** — positive / negative / UNOBSERVED; absence never penalises a primary score, only lowers the Read + FC.
- **Hybrid factors.** `derive_evidence(record)` maps swept signals → factors: pulse.state, north_star_verdict {verdict, trajectory, forecast_defensible}, MEDDPICC `*.status`, competitive_position items, evidence_coverage, stakeholder_map, durable packets, close-date verdict history. Then `_overlay_agent_factors()` overlays `ai.deal_scores_evidence.factors` if the agent emitted them (agent wins). Factor emission is OPTIONAL — block in `docs/DEAL_SCORES_PROMPT_BLOCK.md`, append to the live `mase_deal_sweep` Supabase prompt via `agent_prompt_store.set_prompt(...)` when ready.
- **Recency-weighted**: dynamic factor strengths discounted by days-since-last-touch (≤21 ×1.0 … >180 ×0.25; structural floored 0.60).

## Safety
No LLM call; additive (only `ai.deal_scores`); behind `DEAL_SCORES_ENABLED` (default on); `compute_deal_scores()` NEVER raises (returns `{}` or `{error}` and the sweep continues). Backend populates; frontend renders separately — backend-first is low-blast-radius. Re-sweep repopulates. Related: [[deal-living-memory]], [[avoma-mcp-contention]].
