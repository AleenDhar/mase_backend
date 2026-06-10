---
name: Deal sweep stage authority
description: StageName must be re-synced from live Salesforce on every sweep; the agent-emitted record stage is not trustworthy.
---

# Deal sweep stage authority

The canonical deal record's `hard.stage` (and the derived flat `stage` column /
`/opportunities` output) must come from a **live Salesforce StageName read at
sweep time**, not from the sweep agent's emitted JSON.

**Why:** the agent can emit a stale stage (e.g. "Negotiation/Review",
"Prospecting") while Salesforce has moved on ("Shortlisted", "Vendor Selected").
Users saw stale stages persist on the dashboard.

**How to apply:**
- Every sweep entrypoint funnels through `analyze_one(opp)`. It OVERRIDES (not
  setdefault) `hard["stage"]` from `opp["stage"]` whenever present.
- So `opp["stage"]` must be populated for every path. `discover_opps`/`_map_opps`
  and `_enrich_opp_ids` both select `StageName` in their SOQL and carry it.
- The manual single-opp endpoint (`POST /sweep/{opp_id}`) must enrich via
  `_enrich_opp_ids` first (not trust the request body) so it also gets the live
  stage. If you add a new sweep path, build its `opp` dict the same way or the
  override silently no-ops and the stale agent stage wins.

## swept_at is the same class of bug

`swept_at` is server-owned too. The agent hallucinates future dates (prod had 63
records with swept_at after "today", e.g. a same-day sweep stamped 6 days out).
`analyze_one` must OVERRIDE `parsed["swept_at"] = _today()`, never `setdefault`.

**Rule of thumb:** any record field that means "a live system fact" or "when this
run happened" (stage, swept_at) is owned by the server, not the model. Override it
from the real source at sweep time; never let the agent's JSON win.
