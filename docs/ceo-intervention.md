# CEO Intervention ("CEO help needed") — analysis logic

A per-deal flag, `ai.ceo_intervention`, that surfaces where the **CEO should personally
step into a live deal**. Computed **outside** the sweep and the AI scorer, by a standalone
Claude-Code workflow, then written to the records. This doc is the source of truth for the
rule, the workflow, and how to re-run / extend it.

_First run: 2026-07-02 over all forecasted deals._

---

## 1. The rule (deterministic gate)

> **The CEO should be involved when `win_position > 60` AND `deal_momentum > 60`.**

Both, strictly greater than 60. Read from the stored `record.ai.deal_scores.headline`. A deal
that passes the gate gets `needed: true`; everything else gets `needed: false` (no AI call — just
stamped with its win/mom so the column/filter can still show "evaluated, not needed").

First run: **6 of 122** forecasted deals passed — ARUP, Austrian Post, Robert Bosch, Domino's,
Consumer Cellular, McAfee.

## 2. The 4 CEO levers

The judge picks the 1–3 that would actually move *that* deal (never generically all four):

| area | when it applies |
|---|---|
| `pricing` | approve discount / pricing structure / commercial flexibility — price is a lever or blocker |
| `product` | commit to product feature development / roadmap — a capability gap or roadmap assurance would win it |
| `presales_resources` | allocate pre-sales / solution-engineering / POC / implementation resources — technical bandwidth or a POC is the constraint |
| `exec_connect` | CEO-to-executive relationship connect to reach/align the economic buyer or exec sponsor (valid because a gate-passing deal already has real win possibility) |

## 3. The workflow — `ceo-help-judge` (Claude Code as the judge)

NOT the prod sweep and NOT `deal_engine_ai_scoring`. A standalone multi-agent workflow:

1. **Gate (SQL):** select forecasted deals where `win > 60 AND momentum > 60`.
2. **Fetch context:** for each gate-passer, pull the rich record fields the judgment needs —
   `competitive_position`, `champion_strength`, `vulnerabilities`, `recommended_moves`, `gaps`,
   `business_case`, `product_scope`, `next_step`, stage, amount, close date, win/mom → a JSON file.
3. **Judge (fan-out, 1 agent per deal, parallel):** each subagent is told the gate is already
   passed (`needed = true`), reads ONLY its own deal's JSON, and must:
   - pick the 1–3 **areas** that move *this* deal (grounded in its evidence),
   - write a one-sentence **reason** citing a real deal fact,
   - write the single most valuable concrete **ceo_action**,
   - set **priority** = `high` if amount > $400K or at a decisive gate, else `medium`.
4. **Forced structured output** — validated against the schema below (the model retries on mismatch).

### Output schema (per deal)

```json
{
  "opp_id": "006P...",
  "needed": true,
  "priority": "high | medium",
  "areas": ["pricing" | "product" | "presales_resources" | "exec_connect"],
  "reason": "one plain-English sentence citing a real deal fact",
  "ceo_action": "the single most valuable concrete thing the CEO should do"
}
```

## 4. Apply (data write)

A single opp-scoped `UPDATE … FROM jsonb_each` stamps `ai.ceo_intervention` on every forecasted
row — touching only that one field (never scores/roster):

```json
{
  "needed": true,
  "priority": "high",
  "areas": ["presales_resources", "exec_connect"],
  "reason": "...",
  "ceo_action": "...",
  "win": 62, "mom": 64,
  "source": "workflow_v1",
  "generated_at": "2026-07-02"
}
```

Non-gate deals get `needed: false`, empty `areas`, and a boilerplate reason.
Prod writes go through the dry-run/confirm gate.

## 5. How it reaches the UI

- **`slim_record`** (`deal_engine_store.py`) keeps `ceo_intervention` so the Deals list can
  read + filter it without loading the full record.
- **`analyze_one`** (`deal_engine_sweep.py`) **carries `ceo_intervention` forward** on re-sweep —
  it's written by this separate pass, not computed in the sweep, so a re-sweep must not drop it.
- **Frontend:** the live deal drawer is `components/deals/DealDrawerView.tsx`. The CEO-help card
  renders at the top (between the amount line and the score strip) when `needed`. A `👔 CEO help`
  toggle sits next to Favourites in `ScopeFilterBar.tsx` to filter the list to CEO deals.
  (`DealFold.tsx` is deprecated — do not add deal-view UI there.)

## 6. Re-running / extending

- **Refresh or extend beyond forecasted:** re-run the gate SQL over the wider set, re-run the
  `ceo-help-judge` workflow on the new gate-passers, and re-apply. `needed=false` for the rest.
- **Move into the sweep (future):** the same gate + judge could run inside `analyze_one` so the
  flag is always fresh. Kept separate for now so it stays decoupled from the AI scorer's cost.
- **Change the bar:** the 60/60 gate is the single knob. Adjust it in the gate SQL.
