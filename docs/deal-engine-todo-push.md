# Deal Engine — Push a ticked to-do to Salesforce

Frontend integration guide for closing the loop between an Espresso (Deal
Engine) to-do and Salesforce. When a rep ticks off a to-do and confirms, the
backend logs a **Completed Salesforce Task** on the related Opportunity.

The Salesforce write is a **direct, server-side, human-initiated** write. It does
NOT go through the agent / MCP tool catalog, so the agent's Salesforce write
lockdown is untouched. This is the only path in the app that writes to
Salesforce.

---

## 1. The modal flow

```
rep ticks a to-do
      │
      ▼
confirm modal  ──cancel──▶  (no-op, leave unticked)
      │ confirm
      ▼
POST /api/deal-engine/todo/push   (with the to-do's fields)
      │
      ├─ 200 ok, already_pushed:false  ▶ show "Pushed to Salesforce" + sf_task_id
      ├─ 200 ok, already_pushed:true   ▶ same (it was already pushed)
      ├─ 404 / 400                      ▶ show a validation error, stay unpushed
      └─ 502                            ▶ Salesforce write failed, allow retry
```

A to-do that is already pushed (see `pushed` on the `/todo` response) should
render in its pushed state directly and not re-open the modal.

---

## 2. New fields on `GET /api/deal-engine/todo`

Every item in all five categories (`critical`, `important`,
`explicitRequirements`, `implicit`, `bestPractice`) now carries three extra
fields. Everything else about the response is unchanged.

| field        | type            | meaning                                                        |
|--------------|-----------------|----------------------------------------------------------------|
| `todo_key`   | string          | Deterministic fingerprint of this to-do. **Echo it back** on push. Stable across reloads and sweeps. |
| `pushed`     | boolean         | `true` if this to-do has already been pushed to Salesforce.    |
| `sf_task_id` | string \| null  | The Salesforce Task Id, when `pushed` is `true`.               |

Example item (a `critical` move):

```json
{
  "opp_id": "0065g00000ABCDEisAAA",
  "account_name": "Globex",
  "opp_name": "Globex — Source-to-Pay 2026",
  "owner_name": "Claire Hudson",
  "action": "Get the CFO into the value-engineering session before the RFP closes",
  "intervention_owner": "Claire Hudson",
  "trigger": "CFO went quiet after the pricing call",
  "trigger_date": "2026-05-20",
  "act_by": "2026-06-15",
  "urgency": "next_14_days",
  "expected_effect": "Re-anchors the deal on value before procurement compresses price",
  "todo_key": "a6c0d50af50d22b169e164d2",
  "pushed": false,
  "sf_task_id": null
}
```

The `todo_key` is the join key. When a to-do is pushed, the next `/todo` fetch
returns it with `pushed:true` and the `sf_task_id` filled in — that is how the
pushed state survives a page reload.

---

## 3. `POST /api/deal-engine/todo/push`

Bearer-gated like the rest of `/api/deal-engine` (send
`Authorization: Bearer <token>`, or `?key=<token>`).

### Request body

Send the ticked to-do back. Required: `todo_key`, `opp_id`, `category`. Then
include the display fields you already have for that item — the backend builds
the Task **Subject** (the primary action text) and **Description** (supporting
context) from them.

| field        | required | notes                                                              |
|--------------|----------|--------------------------------------------------------------------|
| `todo_key`   | yes      | The `todo_key` from `/todo`. Drives idempotency.                   |
| `opp_id`     | yes      | The to-do's `opp_id` (15- or 18-char Salesforce id — both work).   |
| `category`   | yes      | One of `critical` / `important` / `explicitRequirements` / `implicit` / `bestPractice`. Picks the primary text field for the Subject. |
| `pushed_by`  | no       | Who confirmed the push (rep name / email). Stored for audit.       |
| display fields | no     | The same fields shown in the UI: `action`, `commitment`, `requirement`, `inferred_need`, `flag`, plus `account_name`, `opp_name`, `owner_name`, `trigger`, `trigger_date`, `act_by`, `due`, `date`, `said_by`, `status`, `expected_effect`, `grounding_quote`, `intervention_owner`. Send whatever the item has. |
| `who_id`     | no       | Optional Salesforce Contact/Lead Id for the Task `WhoId`. Usually omitted. |

The simplest correct call is: send the **entire to-do item object** from `/todo`
back as the body, adding `pushed_by` if you have it. Extra fields are ignored.

#### Subject mapping by category

| category               | Subject is built from |
|------------------------|-----------------------|
| `critical`             | `action`              |
| `important`            | `commitment`          |
| `explicitRequirements` | `requirement`         |
| `implicit`             | `inferred_need`       |
| `bestPractice`         | `flag`                |

The Subject is truncated to Salesforce's 255-char limit. `ActivityDate` is set to
today; `Status` is always `Completed`; the Task is linked to the Opportunity via
`WhatId`.

### Example request

```json
{
  "todo_key": "a6c0d50af50d22b169e164d2",
  "opp_id": "0065g00000ABCDEisAAA",
  "category": "critical",
  "action": "Get the CFO into the value-engineering session before the RFP closes",
  "account_name": "Globex",
  "opp_name": "Globex — Source-to-Pay 2026",
  "owner_name": "Claire Hudson",
  "trigger": "CFO went quiet after the pricing call",
  "trigger_date": "2026-05-20",
  "act_by": "2026-06-15",
  "expected_effect": "Re-anchors the deal on value before procurement compresses price",
  "intervention_owner": "Claire Hudson",
  "pushed_by": "claire.hudson@zycus.com"
}
```

### Responses

**Fresh push (201-style success, HTTP 200):**

```json
{
  "ok": true,
  "already_pushed": false,
  "sf_task_id": "00T5g00000XyZ12EAA",
  "todo_key": "a6c0d50af50d22b169e164d2",
  "pushed_at": "2026-06-08T14:22:51.123456+00:00",
  "subject": "Get the CFO into the value-engineering session before the RFP closes"
}
```

**Already pushed (HTTP 200) — pushing the same to-do again is safe and does NOT
create a second Salesforce Task:**

```json
{
  "ok": true,
  "already_pushed": true,
  "sf_task_id": "00T5g00000XyZ12EAA",
  "todo_key": "a6c0d50af50d22b169e164d2",
  "pushed_at": "2026-06-08T14:22:51.123456+00:00",
  "subject": "Get the CFO into the value-engineering session before the RFP closes"
}
```

**Validation error (HTTP 400):**

```json
{ "error": "todo_key required" }
```

**Opportunity not found (HTTP 404):**

```json
{ "error": "opportunity not found: 006000000000000" }
```

**Salesforce write failed (HTTP 502)** — no push record is stored, so the rep can
retry:

```json
{
  "error": "Salesforce did not confirm Task creation",
  "salesforce": { "success": false, "errors": ["INSUFFICIENT_ACCESS"] }
}
```

---

## 4. Rendering the pushed state

- On a successful push (`ok:true`), mark the to-do as pushed and show the
  `sf_task_id` (optionally as a deep link to the Salesforce Task).
- Treat `already_pushed:true` exactly like a fresh success — the to-do is pushed.
- On the next `/todo` fetch, the item comes back with `pushed:true` and its
  `sf_task_id`, so the pushed state is reload-safe without any client-side
  persistence.
- On `502`, keep the to-do pushable and surface a retry affordance — the backend
  did not record a push, so retrying is safe.

## 5. Idempotency contract

Idempotency is keyed on `todo_key`. The same logical to-do always produces the
same `todo_key` (it is a hash of `opp_id` + `category` + the primary text +
the primary date), so:

- Double-clicks, retries, and re-ticks after reload all converge on one Task.
- The first successful push writes the Salesforce Task and records it; every
  later push for that `todo_key` returns the recorded Task with
  `already_pushed:true`.

## 6. Reference dashboard UI

A self-contained reference UI ships with the backend at
**`GET /api/deal-engine/todo/dashboard`** (a single inline HTML page, no build
step). It demonstrates the full flow end-to-end and can be used as-is or as a
template for the real frontend.

- **Auth:** open it once with `?key=<API_AUTH_TOKEN>`. The auth gate sets an
  HttpOnly cookie and 302-redirects to the clean URL; after that the page's own
  `fetch` calls authenticate via the cookie (the secret never lingers in the URL).
- **What it does:** an RSD filter (from `GET /api/deal-engine/team`), the five
  to-do categories from `GET /api/deal-engine/todo`, a confirm modal, and the
  `POST /api/deal-engine/todo/push` call. Already-pushed items render in their
  pushed state (✓ + `sf_task_id`) and are not re-pushable, surviving reloads via
  the `pushed` / `sf_task_id` fields.
- It sends the **whole to-do item** plus `category` as the push payload, which is
  the simplest correct way to satisfy the field contract in §3.

## Out of scope (today)

- Re-opening / un-pushing a to-do, or editing the Salesforce Task after creation.
- Any Salesforce write other than the completed Task (no Opportunity field
  updates, no Contact/Lead creation).
- Syncing Salesforce completion state back into the to-do derivation.
