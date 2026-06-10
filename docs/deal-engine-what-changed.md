# "What changed" panel — frontend integration guide

How to build the **What changed** surface for the Deal Intelligence Engine: a
per-deal change panel plus a book-level change feed driven by the sweep's living
memory (deltas). The backend is already built — this is the contract the frontend
codes against. For the wider Deal Engine surface (Deals / Espresso / Matcha /
Team / Chat) see `replit.md`.

## Mental model

- Every time the sweep re-runs a deal it reconciles the new evidence against the
  deal's stored living memory and records a newest-first **change log** of
  **deltas** (`deal_records.record.deltas`). A delta is one thing that moved:
  a new requirement, a champion change, a resolved risk, a fact that went quiet.
- The panel turns that log into something a rep actually reads: a short headline
  per change, bucketed into four rep-facing **groups**, newest first.
- It is strictly read-only. Nothing here writes back.

## Endpoints

Both are Bearer-gated (env `API_AUTH_TOKEN`, fallback `DISPATCH_SECRET`); send
`Authorization: Bearer <token>`.

### Per-deal panel — `GET /api/deal-engine/deltas/{opp_id}`

Returns one deal's panel. `opp_id` may be the 15- **or** 18-char Salesforce id —
the lookup matches on the shared 15-char prefix, so a back-link from the feed
always resolves.

```json
{
  "opp_id": "006P700000HBXgRIAX",
  "deal": {
    "opp_id": "006P700000HBXgRIAX",
    "account_name": "Nordea",
    "opp_name": "Nordea_S2C_2025",
    "owner_name": "Casper Hoeholt",
    "stage": "Contract In Progress"
  },
  "count": 36,
  "countsByGroup": { "added": 36, "changed": 0, "resolved": 0, "dormant": 0 },
  "deltas": [ /* newest-first, see "Delta shape" */ ]
}
```

- Use `deal` for the panel header / back-link target.
- Use `countsByGroup` for tab badges (the four buckets, always present, in the
  order `added`, `changed`, `resolved`, `dormant`).
- An unknown / un-swept `opp_id` returns `count: 0`, empty `deltas`, and a `deal`
  with just the `opp_id`.

### Book-level feed — `GET /api/deal-engine/deltas?owner=&limit=&group_by=owner`

Returns the whole pipeline's changes, newest first, each entry tagged with its
deal context for the back-link.

- `owner=` (optional) scopes to one RSD (the deal's `owner_name`).
- `limit=` (default 200) caps the feed length.
- `group_by=owner` (alias `rsd`) adds a `groups` array bucketing the capped feed
  by deal owner (newest-first within each owner). Omit it for a flat feed.

```json
{
  "owner": "all",
  "count": 87,
  "byOwner": { "Casper Hoeholt": 36, "Claire Hudson": 12, "...": 0 },
  "deltas": [ /* flat, newest-first */ ],
  "groups": [
    { "owner": "Casper Hoeholt", "count": 36, "deltas": [ /* ... */ ] }
  ]
}
```

- `count` is the full (uncapped) feed size; `deltas` is capped to `limit`.
- `byOwner` is a roll-up count per owner (handy for a sidebar).
- `groups` only appears when `group_by` is set.

## Delta shape

Each delta in either endpoint is the stored delta enriched with two
**frontend-ready** fields, `label` and `group`:

```json
{
  "opp_id": "006P700000HBXgRIAX",      // feed entries only: deal context
  "account_name": "Nordea",            // feed entries only
  "opp_name": "Nordea_S2C_2025",       // feed entries only
  "owner_name": "Casper Hoeholt",      // feed entries only
  "stage": "Contract In Progress",     // feed entries only
  "date": "2026-06-06",
  "kind": "added",
  "type": "requirement",
  "subject": "Penetration test fields …",
  "key": "requirement:penetration-test-fields…",
  "from": "…",                          // present on `changed`
  "to": "…",                            // present on `added` / `changed`
  "reason": "…",                        // optional
  "source": "Mateusz Tylkowski",        // optional (who/where it came from)
  "label": "New requirement: Penetration test fields …",
  "group": "added"
}
```

- **`group`** is the bucket to render against: one of `added`, `changed`,
  `resolved`, `dormant`. (Raw `kind` `reactivated` rolls into `added`,
  `superseded` into `resolved`.) Always render against `group`, not `kind`.
- **`label`** is the short human headline — use it directly as the list-row
  title. Phrasing by group:
  - `added` → "New {type}: {subject}" (or "{Type} back in play: {subject}")
  - `changed` → "{Type} updated: {subject}"
  - `resolved` → "{Type} resolved: {subject}" (or "… superseded …")
  - `dormant` → "{Type} went quiet: {subject}"
- **`type`** is the kind of fact: `requirement`, `stakeholder`, `competitor`,
  `risk`, `commitment`, `hygiene`, `champion`, `product_scope` — use it for an
  icon / colour.
- `from` / `to` (on `changed`) hold the old/new value briefs — show them as a
  secondary "X → Y" detail line under the label if you want more than the
  headline. `subject` for `champion` / `product_scope` is generic and is omitted
  from the label on purpose.

## UI requirements

- **Per-deal panel.** A "What changed" tab/section on a deal that lists its
  deltas newest-first. Show four group filters/tabs with the `countsByGroup`
  badges. Each row: a `type` icon, the `label`, and the `date`. Empty state when
  `count` is 0 ("No tracked changes yet").
- **Book feed.** A pipeline-wide feed of recent changes, newest-first. Offer an
  owner/RSD filter (`?owner=`) or a grouped view (`?group_by=owner`, render the
  `groups` array as sections). Each row links back to its deal using the entry's
  `opp_id` (→ the per-deal panel / deal page).
- Both surfaces are read-only.
