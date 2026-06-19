---
name: Update branching — three destinations
description: A manual deal update can branch to Next Step (HTML newest-on-top append), an open To-Do activity, or a Completed task — all via the sanctioned human-initiated SF write path.
---

# Update branching — three destinations

`POST /api/deal-engine/todo/update` (server.py) takes a manual update and branches
on a `destination` field; `salesforce_task_writer.py` holds the writers. All writes
are DIRECT simple-salesforce / per-user-OAuth calls (NOT the agent tool catalog),
so the agent's MCP write lockdown (`MCP_TOOL_DENYLIST`) stays fully intact.

**Destinations** (body: `{opp_id, note, destination, due_date, by?, sf_access_token?, sf_instance_url?}`):
- `completed` (DEFAULT — unchanged) → `create_completed_task` (Status='Completed').
- `todo` → MASE to-do (the in-app `insert_manual_update` row) **+** `create_open_task`
  (Status=**'Planned'** — this org's open-task picklist value; ActivityDate = due_date).
- `next_step` → `append_next_step`: **prepend** to `Opportunity.Next_Step__c`, newest
  on top, FULL prior trail preserved.

**Why these specifics (verified against live SF, 2026-06-19):**
- Open-task status is **`Planned`** (Task.Status picklist: Planned / In Process /
  Completed / Tentative / Deferred / Incomplete / Skipped / RESOLVED — no literal "Open").
- The Next Step field is **`Next_Step__c`** (the standard `NextStep` does NOT exist
  in this org) and it is an **HTML rich-text long-text-area** — it "can not be filtered"
  in SOQL, and real values are `<p>…</p>` / `<ul>` blocks, newest-on-top, e.g. Austrian
  Post. So the append writes an HTML `<p>{date} (due {date}): {escaped note}</p>` block
  on top; a plain-text prepend would render broken.

**Invariants:** every branch carries a due date (completed defaults to today). The
in-app row is ALWAYS saved (even if the SF write fails → `sf_error` returned) so an
update is never lost. Per-user OAuth (sf_access_token/sf_instance_url) makes the rep
the author; otherwise the shared integration user. Auto-generated to-dos (to-do
runner) still stay MASE-only — this branching is for USER-created updates.

**Not in scope here:** the frontend chooser (destination radio + due-date picker)
lives in the MASE Next.js app (separate repo), not this backend.
