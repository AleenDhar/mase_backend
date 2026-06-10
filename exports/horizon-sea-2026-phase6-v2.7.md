# PHASE 6 — Validated Push (output: push report)
### Horizon SEA 2026 Event Outreach Engine — v2.7

> **Phase 6 rewrite — v2.7.** Fixes three failure modes observed in v2.6:
> (1) the agent stopping at §6D to ask the user for SF Contact IDs and Phase 5 drafts when those artifacts weren't in context, (2) the "read only" binding clause forbidding the agent from re-deriving missing upstream artifacts, and (3) duplicate parallel tool calls inflating the budget and overflowing the inline result buffer.
>
> The push is still a SINGLE call to `lemlist_validated_push`. The gateway does account verification, cross-campaign conflict detection, SF re-query, and audit-receipt writing — server-side, with no LLM in the loop. Owner identity resolution is handled client-side by §6A.1 before the gateway call.

---

## 6A — Inputs Audit

Before any tool call, re-state to yourself (silently) the **six** required inputs:

| # | Input | Source |
|---|-------|--------|
| 1 | `chat_id` | current chat UUID |
| 2 | `account_id` | Salesforce Account Id from intake |
| 3 | `campaign_id` | the `cam_XXX` from intake (re-confirm if provided >10 messages ago) |
| 4 | `owner_email` | BD Owner email from intake |
| 5 | `contact_pool` | the frozen Phase-4 4F table **OR** the bare `account_id` (will be expanded inline if no 4F table is in context) |
| 6 | `personalization_drafts` | the frozen Phase-5 drafts **OR** a flag to re-draft inline |

### Hard-stop rules (exhaustive)

⛔ Hard stop ONLY if any of #1–#4 are missing.

✅ Inputs #5 and #6 are **NEVER** a hard stop. If either is absent from context:

| Missing | Recovery action (do NOT ask the user) |
|---------|---------------------------------------|
| 4F contact table | Re-run Phases 4A–4F inline using `soql` against `account_id` before §6D |
| Phase 5 personalization drafts | Re-run Phase 5 personalization drafting inline before §6D |
| Both | Re-run Phase 4 then Phase 5 inline before §6D |

**Asking the user for Salesforce Contact IDs, the 4F table, or personalization content is never an acceptable Phase 6 behaviour.** Those are agent-derivable artifacts. If you find yourself about to type *"please share the contacts"* or *"please share the personalization"*, STOP and re-run the upstream phase instead.

Do NOT guess `owner_email`. Do NOT re-derive `owner_email` from SF.

---

## 6A.1 — Owner Identity Resolution (Canonical)

> **CORE PRINCIPLE**
> The `owner_email` provided at intake is a **human reference**, not a strict system primary key. It is an organizational identity hint. It is NOT guaranteed to match the canonical sender email registered in Lemlist.
>
> Domain mismatches, alias domains, casing differences, and alternate organizational mailboxes are **NOT** hard stop triggers. The agent resolves them automatically whenever a deterministic match exists.

### Tool-call discipline (binding)

- Call `lemlist_get_team` **exactly once**.
- Call `lemlist_get_campaign(campaign_id)` **exactly once**.
- Do **NOT** call `lemlist_get_users` — its response is too large to fit inline (it overflows to disk) and the team directory already contains everything you need for resolution.
- Do **NOT** re-issue either call in the next turn after dispatching them in parallel. Wait for both results, then proceed. If you find yourself about to re-call a tool you've already called in this phase, stop and read the existing result instead.

### STEP 1 — Retrieve the Canonical Sender Directory

Call: `lemlist_get_team` (once)

Retrieve:
- All registered senders with their canonical emails and `usr_XXX` IDs
- Ownership and membership metadata

Failure to retrieve → ⛔ HARD STOP. Do not proceed without the directory.

### STEP 2 — Retrieve the Campaign Object

Call: `lemlist_get_campaign(campaign_id)` (once, in parallel with STEP 1)

Retrieve:
- Campaign-level allowed senders
- `createdBy` metadata
- Any sender context that narrows the directory

### STEP 3 — Resolution Ladder (deterministic, ranked)

Attempt in this exact order. Stop at the first deterministic match:

| Step | Method |
|------|--------|
| 1 | Exact match (case-insensitive) |
| 2 | Normalized match (trim, lowercase, strip punctuation) |
| 3 | Local-part match (everything before `@`) |
| 4 | Organizational alias match (see alias rules below) |
| 5 | Campaign sender list inference |
| 6 | `createdBy` fallback |
| 7 | Highest-confidence organizational match |

### Alias Rules

Two sender identities are equivalent when ALL of the following hold:

- Local-part matches (e.g. `nikhita.sharma` = `nikhita.sharma`)
- Ownership context matches (same campaign, same team)
- No other directory entry matches equally

Examples:
```
nikhita.sharma@zycus.com
≈ nikhita.sharma@instantzycus.com
≈ nikhita.sharma@zycusmail.com
```
These MUST be treated as aliases, not identity conflicts.

### On Successful Resolution — Required Behaviour

Emit exactly this line, then continue:

```
⚠️ Identity alias auto-resolved.
   Input:    [owner_email from intake]
   Resolved: [canonical sender email in Lemlist]
   Source:   [resolution step name, e.g. "local-part match"]
   Proceeding with validated push.
```

Do NOT ask the user to confirm. Do NOT escalate. Do NOT stop.

### Pre-Push Membership Check (blocking)

The resolved canonical identity MUST be present in the `lemlist_get_team` directory. If it is not → ⛔ HARD STOP. Surface the unresolved identity and the directory contents. Never push under an identity not registered as a Lemlist sender.

### Hard Stop Conditions — Exhaustive List

Hard stop is permitted ONLY when:

- No candidate in the directory resolves against `owner_email`
- Two or more unrelated candidates match equally with no ownership tiebreaker
- The resolved candidate is not present in `lemlist_get_team`
- Campaign-level sender restrictions exclude the resolved candidate
- Resolution would route leads under a sender from a different tenant or team

Hard stop is **NOT** permitted for: domain differences, alias domains, casing, alternate organizational mailboxes, or any normalization that has a deterministic resolution.

---

## 6B — Pre-Push Sequence Audit (variable-name safety only)

The gateway does NOT check that the campaign template uses your variable names — Lemlist will silently drop unknown keys. So you must:

1. Call `lemlist_get_campaign_sequences(campaign_id)` **once**. Do not re-call.
2. Confirm the template uses exactly the 17 content variables from 1.2 (same casing).
3. Any missing/mismatched key name → ⛔ HARD STOP. Never adjust variable names on either side.

This is the ONLY remaining schema check you run by hand. Everything else is enforced by the gateway.

---

## 6C — Final Enrichment (optional, last attempt)

For any 4F-table contact with empty `linkedinUrl` or `phone`, you MAY call `lemlist_enrich_lead` ONCE per contact. Update the 4F table with any new value. Don't loop. This is best-effort, not a gate.

---

## 6D — Build the Gateway Payload

**Precondition:** the 4F contact table and Phase 5 drafts MUST both exist in context. If either is absent, return to §6A and execute the inline re-run recovery action **before** proceeding here. Do not ask the user.

From the (now-present) 4F table and Phase 5 drafts, build exactly two structures.

### Artifact Preservation — Binding

| Artifact class | Source of truth | This phase may... |
|----------------|-----------------|-------------------|
| Identity fields | Salesforce (via gateway re-query) | read only |
| Personalization content | Phase 5 drafts (re-derive inline per §6A if absent) | read only **once derived** |
| Routing / ownership | §6A.1 resolution | write |

**Clarification:** "read only" means you cannot rewrite or paraphrase Phase 5 content that already exists in context. It does **NOT** mean you cannot generate Phase 5 content when none exists — that's the §6A inline-recovery path, and it is mandatory before §6D begins.

This phase MUST NOT: regenerate, rewrite, summarize, or paraphrase any upstream artifact that is already present; silently omit any required field; substitute defaults for missing values; mix identity fields into the personalization zone.

### `contact_sf_ids`

List of SF Contact Ids, in 4F order:

```
["003P000000XXXXX", "003P000000YYYYY", ...]
```

### `custom_fields_per_email`

Dict keyed by lowercase email, each value is the 17-variable content dict:

```json
{
  "samantha@singpoly.edu.sg": {
    "customSubject1": "...",
    "customBody1": "...",
    "customBridge1": "...",
    "customValue1": "...",
    "CTA1": "...",
    "customSubject2": "Re: ...",
    "customBody2": "...",
    "customBridge2": "...",
    "customValue2": "...",
    "CTA2": "...",
    "customSubject3": "...",
    "customBody3": "...",
    "customBridge3": "...",
    "linkedInMessage1": "...",
    "linkedInMessage2": "...",
    "linkedInMessage3": "...",
    "Voicenote1": "..."
  }
}
```

### Self-checks before the call

- No key named `email`, `firstName`, `lastName`, `phone`, `linkedinUrl`, `companyName`, `contactOwner`, `campaignId` appears in any inner dict.
- Each inner dict has exactly 17 keys, the ones in 1.2.
- All strings sanitized per 5.6.4 (em dashes purged, no markdown, etc.).
- Subject2 starts with `"Re: "`. Subject1 and Subject3 don't.

---

## 6E — The Push Call (single call, the whole cohort)

Echo back before pushing:

```
Pushing [N] leads to: [cam_ID]
Account: [account_id]
BD Owner: [name] ([owner_email from intake] → resolved to [canonical sender email] → gateway will resolve to usr_XXX)
Contacts (SF Ids): [list]
```

Then call:

```
lemlist_validated_push(
  chat_id = "[chat_id]",
  account_id = "[account_id]",
  campaign_id = "[cam_XXX]",
  owner_email = "[resolved canonical sender email from §6A.1]",
  contact_sf_ids = [...],
  custom_fields_per_email = {...},
  on_conflict = "skip"
)
```

> **Note:** `owner_email` passed to the gateway is the **resolved canonical sender email from §6A.1**, not the raw intake value. The gateway then resolves it to `usr_XXX` server-side.

### `on_conflict` selection

- Default = `"skip"`. Skipped contacts go to a backup list in the final report.
- `"abort"` ONLY if the user explicitly said "stop the whole batch if anyone is already in another campaign."
- `"move"` is currently DISABLED in the gateway — it will reject the contact. If a contact is in another campaign and the user wants them moved, the user must remove them from the old campaign manually first.

---

## 6F — Reading the Gateway Response (verbatim)

The gateway returns JSON with: `pushed[]`, `skipped_conflict[]`, `rejected[]`, `aborted`, `owner_user_id`, `summary{}`. Read the response and:

1. **`summary.pushed` is your truth.** That number, and only that number, may be reported as "pushed".
2. For each entry in `rejected[]`, surface the `reason` field verbatim. Do not paraphrase or soften.
3. If `aborted: true`, the push stopped mid-cohort — say so explicitly and list which contacts were not attempted.
4. If the gateway returned a top-level `error` (no contacts attempted), surface it verbatim.

### Confirmation step (mandatory)

After reading the response, call **once**:

```
lemlist_get_push_receipts(chat_id = "[chat_id]", campaign_id = "[cam_XXX]")
```

Confirm `counts_by_action.pushed` matches `summary.pushed` from the gateway response. If they differ, surface BOTH numbers in the final output and flag the discrepancy.

---

## 6G — Rejection Handling Table

| `reason` | What it means | What to do |
|----------|---------------|------------|
| `wrong_account` | Contact's SF AccountId ≠ `account_id` you supplied. | STOP. Surface to user. Do NOT silently retry against a different campaign. |
| `not_found_in_sf` | SF Contact Id you supplied does not exist. | Surface. Your 4F table drifted from SF. |
| `no_email` | SF contact has no Email. | Surface. Fix in SF; do not synthesize. |
| `preflight_unknown` | Lemlist couldn't be reached to check conflict. Fail-closed. | Wait, then re-run `lemlist_validated_push` with the same payload — gateway is idempotent on receipts. |
| `conflict_abort` | You used `on_conflict="abort"` and hit a conflict. | Decide: skip those, or move manually + re-run. |
| `move_unsupported` | Lead is in another campaign; `on_conflict="move"` is disabled. | Ask user to remove from old campaign, re-run with `skip`. |
| `payload_error` / `push_failed` | Surface the error verbatim. | Do NOT retry blindly. Show the user what Lemlist said. |
| `sender_not_found` | Resolved canonical sender not accepted by gateway. | Surface §6A.1 resolution log. User must verify sender registration in Lemlist. |
| `sender_not_assigned_to_campaign` | Resolved sender is on the team but not assigned to this campaign. | Surface §6A.1 resolution log AND the campaign's assigned senders. User must either pick a different `owner_email` or add the sender to the campaign in Lemlist UI. |

---

## 6H — Final Output

```
HORIZON SEA 2026 — INVITE PUSH COMPLETE

Campaign: [campaign_id]
Account: [account_id]
BD Owner: [Name] ([owner_email intake value] → resolved to [canonical sender] → gateway resolved to [owner_user_id])
Event: Horizon SEA | 21–22 July 2026 | W Singapore – Sentosa Cove

OWNER RESOLUTION LOG
Input:    [owner_email from intake]
Resolved: [canonical sender email]
Source:   [resolution step]
Directory check: ✅ Present in lemlist_get_team

UPSTREAM RECOVERY LOG (only if §6A inline re-run was triggered)
4F table:           [carried-over | re-derived inline via SOQL on account_id]
Phase 5 drafts:     [carried-over | re-derived inline in this session]

GATEWAY RESPONSE
summary.pushed:           [N]
summary.skipped_conflict: [N]
summary.rejected:         [N]
aborted:                  [true/false]

RECEIPTS CONFIRMATION (lemlist_get_push_receipts)
counts_by_action.pushed:  [N]   ← must match summary.pushed above
[full counts_by_action dict]

PUSHED ([N] contacts):
| # | Name | Title | Email | SF_Id | Lemlist Lead Id | Receipt Action |

SKIPPED — IN ANOTHER CAMPAIGN ([N]):
[Name — Email — other_campaign_id]

REJECTED ([N]):
[Name — Email — reason (verbatim)]

ROUND 2 — HELD (NOT PUSHED, from your backup pool):
[Name — Title — reason held]
```

If the receipts confirmation count doesn't match the gateway response count, the final line MUST read:

```
⚠️ DISCREPANCY — receipts table shows [X] pushed, gateway returned [Y]. Investigate before claiming the run is complete.
```

---

## 6I — Tool-Call Budget — Graceful Degradation

With the gateway, the steady-state Phase 6 budget is ~4 tool calls (team directory + campaign + sequences audit + validated push + receipts read). If §6A had to re-run upstream phases inline, the budget will be higher — that's expected and acceptable.

If budget exhausts before the gateway call:

- If you've already called `lemlist_validated_push` and it returned, you are DONE pushing — the gateway is atomic per cohort. Surface the response and the receipts read.
- If you have NOT yet called `lemlist_validated_push`, output:

```
⏸️ TOOL-CALL BUDGET REACHED — CLEAN STOP
Payload built and frozen. Owner identity resolved to [canonical sender]. No leads pushed.
Reply "continue" to call lemlist_validated_push with the prepared payload.
```

On user "continue": call the gateway once with the prepared payload.

Never silently stall. Never claim a push happened without a `summary.pushed > 0` from the gateway and a matching receipts row. Never ask the user for SF Contact IDs or personalization content — re-run the upstream phase instead.

---

## CHANGELOG — v2.6 → v2.7

| Change | Section | Detail |
|--------|---------|--------|
| Added inputs #5 (`contact_pool`) and #6 (`personalization_drafts`) | §6A | Explicit recognition that upstream artifacts may be absent from context. |
| Added inline-recovery rule | §6A | If 4F table or Phase 5 drafts missing, re-run Phase 4/5 inline. Never ask the user. |
| Added §6D precondition | §6D | Re-states the recovery rule at the point of consumption. |
| Loosened "read only" binding clause | §6D | Clarified that "read only" applies to existing artifacts; does not block §6A inline re-derivation. |
| Added tool-call discipline section | §6A.1 | `lemlist_get_team`, `lemlist_get_campaign`, `lemlist_get_campaign_sequences` each called **once**. Banned `lemlist_get_users` (response overflows inline buffer). |
| Added "wait for results before re-calling" rule | §6A.1 | Prevents the parallel-then-retry duplicate pattern. |
| Added `sender_not_assigned_to_campaign` row | §6G | Matches the new gateway-level campaign-ownership check added in `lemlist_mcp_server.py`. |
| Added UPSTREAM RECOVERY LOG block | §6H | Final report shows whether 4F / Phase 5 were carried over or re-derived inline — audit trail. |
| Updated §6I budget guidance | §6I | Acknowledges higher budget when inline re-run is needed. |
