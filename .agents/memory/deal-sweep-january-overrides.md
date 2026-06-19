---
name: Deal Sweep — January version (live Supabase overrides)
description: The live mase_deal_sweep prompt carries 6 appended override blocks (the "January" version). Agent prompts live in Supabase, NOT in these files / prompts/*.md. Versions are named by month.
---

# Deal Sweep — versioning + January contents

Agent prompts live in **Supabase `jarvis_settings` (`mase_deal_sweep`)**, NOT in `prompts/*.md` (deprecated seeds). Sam's convention (2026-06-19): each Deal Sweep version is named by **month** (≥12 planned), upgraded over time.

## January — current LIVE
Base prompt + these appended override blocks, in order (all live, ~15s TTL to take effect):
1. **VERDICT RUBRIC OVERRIDE v2** — verdict = ≤90-word momentum pulse, graded on BUYER engagement.
2. **DEALSWEEP v2 OVERRIDE** — anti-fabrication (provenance-or-generic; no invented ERP/competitor/blocker; no substitution), full-identity entity resolution, broadened engagement, Avoma-by-account+domain, I1→I2 living-memory (carry-forward; downgrade only on physical evidence; age@90d), inline self-inspect.
3. **OpportunityContactRole patch** — read OCR + website domain, NOT `Account.Contacts` (the gateway never materialises child-relationship subqueries; query the child object by FK).
4. **AVOMA RETRIEVAL LADDER & COVERAGE** — discover by Account.Id + domain, never Avoma's cross-wired opp/account association; scope-route calls by subject; retrieval fallback transcript→notes(derived)→metadata; capture gaps (bot_denied/timeout/etc.) ≠ silence; coverage tally → analysis_confidence Low (never read a gap as dark).
5. **INTERNAL ORG + NO-FABRICATION + ESCALATION** — the Zycus sales roster is the ONLY valid internal names; every name must trace to SF `Owner.Name`/`Owner.Manager.Name` or a real buyer contact (else use the role); VP/exec-on-call ONLY on forecasted deals (ForecastCategory ∈ Commit/Best Case/Upside Key Deal). Shekhar (President) never in a sales cycle.
6. **STRIP CARRIED FABRICATIONS + HARD ESCALATION** — re-validate every CARRIED-forward name each sweep and strip fabrications even from prior packets/commitments (Sarah Chen / Ryan Mitchell strip-on-sight); no exec/VP-call move on non-forecasted deals, fresh OR carried. (Closed the gap where living memory preserved an old fabrication.)

Deployed CODE this version (`deal_engine_sweep.py` buyer-identity prefetch): partner-vs-buyer (dominant-domain + SI denylist), thin-roles (<3) account-bench fallback, always-on sibling-opp scope, non-person/mailbox filter. See `buyer-identity-account-fallback.md`, `update-branching-three-destinations.md`.

## Bumping a version
Append/replace override blocks on `mase_deal_sweep` (Admin → Agent Control or `POST /api/deal-engine/sweep/prompt`) and/or deploy code; then rename here with the new month + changelog delta. Rollback = strip the block(s) that version added.

**NEXT (designed, not built — February candidates):** an independent **Quality Inspector** lane (re-inspects the whole record incl. carried memory; *continuity ≠ immunity*; strips fabrication/inaccuracy/inadequacy; blocks publish) and the **recency-weighted signal model**.
