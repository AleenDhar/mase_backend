---
name: Deal-engine to-do actionability horizon
description: The daily Espresso to-do defers far-future items; the "act now vs in control" judgment lives in the sweep agent, not the deterministic view.
---

# Deal-engine to-do actionability horizon

The Espresso to-do (`derive_todo`) is rebuilt every day, so it must only show items
the team can act on now. Forward-looking buckets are gated by `_within_todo_horizon`
(`TODO_HORIZON_DAYS`, default 60, env `DEAL_TODO_HORIZON_DAYS`):

- **critical** = top-ranked `recommended_moves` move, gated/tiered by `act_by` (the
  near-term date to act, set by the agent), falling back to `trigger_date` only for old
  records lacking `act_by`. Pick the highest-ranked move that is *in horizon* (not
  blindly rank-1), so a key deal whose only moves are far-future drops off until one
  comes into range.
- **important** = open/overdue `open_deliverables`, gated by `due`.
- Gate semantics: show if undated, overdue/past, or within horizon; defer far-future.

**`act_by` vs `trigger_date` (don't conflate — this bit me once):** `trigger_date` is
the date of the justifying *evidence/signal* and is usually in the PAST; `act_by` is the
future near-term date by which to act. An earlier attempt to make `trigger_date` mean
"act soon" made 295/302 critical moves show as "overdue" (evidence dates are old). Fix
was a dedicated `act_by` field — the view keys urgency/horizon off it. Populating
`act_by` on existing records requires a re-sweep; until then critical falls back to the
past `trigger_date` and reads as overdue.

**Urgency tiers** (`_urgency`, on critical + important): overdue / next_14_days /
next_30_days / later / undated — for the UI to flash. **Volume cap:** `best_practice`
flags are capped per deal at `TODO_MAX_BEST_PRACTICE` (default 5, env
`DEAL_TODO_MAX_BEST_PRACTICE`); the agent emits them most-important-first, so first-N is
the safety net, prompt ordering is the real trim.

**Why:** users saw December due dates in June. The far-future items were almost all
`open_deliverable.due` (a buyer's real "issue RFP in Q3"), NOT move trigger dates, so
re-sweeping does NOT fix them — the date is a fact and re-emits. The deterministic
filter is the real fix and needs no re-sweep.

**How to apply / division of labour:**
- The deterministic view only hides far-future raw items.
- The *intelligence* about whether to act early on a far-off milestone lives in the
  sweep agent prompt (`prompts/deal_engine_sweep_system_prompt.md`, section 4): if
  Zycus is in control (strong multi-threaded champion, recent engagement, momentum),
  emit no near-term move; only if control is shaky emit a dated near-term "soft nudge"
  move. That nudge then flows into the critical bucket naturally.
- `_parse_iso_date` is fail-open (unparsable date -> shown). Schema mandates
  YYYY-MM-DD, so this is fine; if the model starts emitting prose dates, harden here.
