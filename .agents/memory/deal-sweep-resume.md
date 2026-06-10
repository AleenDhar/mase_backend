---
name: Deal sweep run-state is in-memory; resume by report-minus-persisted
description: Why restarting the server interrupts a live deal sweep, and how to resume without re-running (re-charging) finished opps.
---

The deal-engine sweep tracks live progress in a process-local in-memory run state
(one run at a time). Completed opps are upserted to Supabase as they finish, but
the run state itself is NOT persisted.

**Consequence:** restarting the server (required to apply ANY code change to the
sweep/server, since there is no auto-reload) interrupts a live sweep and resets
the dashboard to idle. Only opps already saved survive; in-flight opps are lost.

**Why it matters:** each opp is a full multi-tool agent run — minutes + real
token cost. Re-running already-finished opps wastes the user's money and is a
serious trust issue. The user is highly sensitive to this.

**How to resume safely (never re-charge finished work):**
1. Build the done-set from persisted records: `{r.opp_id[:15] for r in store.list_records(None)}`.
2. Recompute remaining = report opp_ids minus the done-set (match on 15-char prefix — report ids are 15-char, API ids 18-char).
3. Relaunch `POST /api/deal-engine/sweep` with `opp_ids = remaining` (not the full set).

**Dashboard history:** `get_status()` merges persisted records in as `completed`
rows (de-duped by 15-char prefix, cached ~15s) so prior-run opps still show as
done after a restart instead of looking lost.

**Operational rule:** avoid restarting while a sweep runs. If a code change forces
a restart, warn first, then resume with the remaining-only scope above.
