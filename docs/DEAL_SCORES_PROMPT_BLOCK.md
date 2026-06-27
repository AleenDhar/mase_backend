# Deal Scores — optional sweep-prompt block (agent-emitted soft factors)

The deterministic scorer in `deal_engine_scoring.py` runs on **every** sweep and
derives its factors from the gate-clean record (pulse / north-star verdict /
MEDDPICC / competitive position / packets). It needs **no prompt change** to work.

This block is an **optional enhancement**: when appended to the live
`mase_deal_sweep` prompt (Supabase `jarvis_settings`, via Admin → Agent Control or
`agent_prompt_store.set_prompt(<text>, agent_id="mase_deal_sweep")`), the sweep agent
will *also* emit the softer judgment factors it can read from the calls, and the
scorer overlays them on top of the derived ones (agent wins on the keys it provides).
The backend tolerates its absence — ship the code first, apply this when ready.

---
### APPEND TO mase_deal_sweep PROMPT — DEAL SCORES EVIDENCE (factor emission)

In addition to your normal output, emit `ai.deal_scores_evidence` describing the
deal-health factors you can read from the Salesforce + Avoma evidence you swept.
This is judgment from the calls — only record what the evidence actually shows;
omit anything you cannot read (omission is neutral, never a penalty). Shape:

```json
"deal_scores_evidence": {
  "cadence": {"days_since_last_call": <int|null>, "expected_cadence_days": 14},
  "factors": {
    "pain_fit": {"strength": 0.0, "evidence": "<short plain-English quote>"},
    "engagement_direction": {"strength": 0.0, "evidence": "..."}
  }
}
```

Ranges: `pain_fit`, `engagement_direction`, `stage_evidence_alignment`,
`competitive_posture` are SIGNED −1.0..+1.0 (they can point either way). EVERY
other factor is a MAGNITUDE 0.0..1.0 and is never negative — a lost champion is
NOT a negative `champion_strength`; omit it and use `attendance_or_cadence_drop`.

Recency-weight each strength by how fresh its evidence is (≤21d full, 22–45d ×0.85,
46–90d ×0.65, 91–180d ×0.45, >180d ×0.25; structural facts like pain floored 0.60),
and put the date in the evidence string. Use only these factor keys:

- Win baseline (signed): pain_fit, engagement_direction, stage_evidence_alignment, competitive_posture
- Win lift: exec_access, champion_strength, commercial_motion, customer_action_items, stakeholder_expansion
- Momentum +: seniority_rising, customer_action_items_increasing, commercial_topics_entering, concrete_dates, customer_requested_next_meeting, close_plan_concretizing, stage_advanced_with_evidence
- Momentum − (observed backward motion only): close_date_pushed, stage_stuck_past_cadence, customer_passivity, attendance_or_cadence_drop, generic_demo_only, competitor_praised
- Commitment: customer_action_items, internal_process_shared, exec_access_granted, customer_next_meeting_request, security_or_procurement_review, deep_eval_or_reference_request
- Risk (observed only): close_date_pushed_repeatedly, stage_inflation, competitor_preferred, open_competitive_rfp, customer_passivity, low_buyer_intent, next_meeting_declined, budget_frozen_or_unclear, access_blocked

Do NOT compute scores yourself — the backend does the arithmetic deterministically.
### END BLOCK
---
