# Deal Engine — engagement pulse (live / cooling / dark) frontend contract

This tells the **Vercel frontend** how to render the per-deal engagement *pulse*
the backend now produces. The backend is done; the only change is on the
frontend — add a pulse badge to each deal row/card (and the deal detail page),
plus a book-level roll-up on the sweep dashboard.

The pulse is the single authoritative, server-computed read of how recently and
meaningfully a deal is being worked, anchored to today and derived from VERIFIED
signals (Salesforce `LastActivityDate`, buyer calls read this sweep, stage/close
proximity, forecast) plus any recent dated rep-initiated outreach parsed from the
Next Step field. Surfacing it gives reps an at-a-glance read of which deals are
actually moving vs going dark, and visibly explains why stale "ghost"/"dark"
flags disappeared.

## Where to implement
- Repo: the **Vercel frontend** (NOT this FastAPI backend repo).
- Views:
  - The **Deals list/grid** — a pulse badge on each deal row/card.
  - The **deal detail page** — the same badge plus the `summary` text and (when
    present) the rep-outreach note.
  - The **sweep dashboard** — a book-level roll-up of live/cooling/dark counts.
- Same endpoints, same auth — you are only reading new fields.

## Auth
- Header: `Authorization: Bearer <DISPATCH_SECRET>` (the same token the rest of
  the deal endpoints already use).

## Per-deal pulse — data source
- List: `GET /api/deal-engine/opportunities?owner=<rsd?>` → `{ count, records: [...] }`
- Detail: `GET /api/deal-engine/opportunities/{opp_id}`

The pulse lives at the **top level** of each record under `pulse` (a sibling of
`hard` / `ai`, NOT nested under a `record` key). It is guaranteed present on
every record returned by these endpoints — for deals swept before the pulse
existed the backend derives an equivalent one read-only from the stored facts, so
the frontend never has to handle a missing `pulse`.

```jsonc
{
  "opp_id": "006P700000Kpl1O",
  "hard": { /* Salesforce facts */ },
  "ai":   { /* analysis columns */ },
  "swept_at": "2026-06-10",
  "pulse": {
    "as_of": "2026-06-16",            // the date the pulse was anchored to
    "state": "live",                   // "live" | "cooling" | "dark"  → drives the badge
    "summary": "Live: last verified Salesforce activity 4 day(s) ago (2026-06-12).",
    "last_activity_date": "2026-06-12", // ISO date or null
    "days_since_activity": 4,           // int or null
    "calls_read": 2,                    // buyer calls read this sweep (int or null)
    "buyer_calls_seen": true,
    "stage": "Validating Benefits",     // string or null
    "days_since_qualified": 38,         // int or null
    "close_date": "2026-07-31",         // ISO date or null
    "days_to_close": 45,                // int or null (negative = past due)
    "forecast_category": "Commit",      // string or null
    "verified_known": true,             // false → no verified signal at all (treat state cautiously)
    "rep_outreach": {
      "detected": false,                // true → rep reached out, awaiting buyer reply
      "date": null,                     // ISO date of the rep touch (when detected)
      "text": null,                     // the Next Step snippet (≤300 chars)
      "note": "rep reached out, awaiting buyer reply"
    }
  }
}
```

### What to render per deal
1. **Badge** keyed on `pulse.state`:
   - `live` → green
   - `cooling` → amber
   - `dark` → red
2. **`pulse.summary`** — a ready-to-show one-liner. Render it as-is (badge
   tooltip, or a line under the badge on the detail page). Do not re-derive it.
3. **Rep-outreach note** — when `pulse.rep_outreach.detected` is true, show the
   `note` ("rep reached out, awaiting buyer reply") with `rep_outreach.date`.
   This is DISTINCT from a two-way buyer touch — label it as a rep-initiated
   outreach, never as buyer engagement. (`pulse.summary` already appends this
   sentence, so if you render the full summary you do not need to render it
   twice.)

State meaning (for copy/tooltips, do not re-implement the thresholds):
- **live** — verified buyer/Salesforce activity recently, or a buyer call was
  read this sweep.
- **cooling** — verified activity has aged out of the live window but is not yet
  dark, OR the deal is otherwise quiet but the rep reached out recently.
- **dark** — no verified buyer engagement in the window and no recent rep
  outreach. The right move is to re-engage.

## Book-level roll-up — data source
- `GET /api/deal-engine/sweep/status`

Two pulse-related fields:

```jsonc
{
  // ...existing sweep status fields...
  "pulse_summary": { "live": 42, "cooling": 31, "dark": 12, "unknown": 3 },
  "opps": [
    { "opp_id": "006...", "account": "Acme", "owner_name": "Claire Hudson",
      "name": "Acme — Renewal", "pulse_state": "live", /* ...other row fields... */ }
  ]
}
```

- `pulse_summary` — counts across the whole book. Render as a roll-up
  (e.g. three counters or a small stacked bar): live / cooling / dark, with
  `unknown` shown only if non-zero. Both `pulse_summary` and `pulse_state` are
  best-effort: if `pulse_summary` is missing/empty (an older backend, or a read
  hiccup) hide the roll-up rather than erroring.
- `opps[].pulse_state` — the per-row state (`"live" | "cooling" | "dark"` or
  `null`) if the sweep table also wants a column.

## Minimal render example (React/TSX)

```tsx
const PULSE_BADGE = {
  live:    { label: "Live",    color: "green" },
  cooling: { label: "Cooling", color: "amber" },
  dark:    { label: "Dark",    color: "red"   },
} as const;

function PulseBadge({ pulse }: { pulse: DealPulse }) {
  const meta = PULSE_BADGE[pulse?.state as keyof typeof PULSE_BADGE];
  if (!meta) return null;
  return (
    <span className={`badge badge-${meta.color}`} title={pulse.summary}>
      {meta.label}
    </span>
  );
}

function PulseRollup({ summary }: { summary?: Record<string, number> }) {
  if (!summary) return null;
  const { live = 0, cooling = 0, dark = 0, unknown = 0 } = summary;
  return (
    <div className="pulse-rollup">
      <span className="badge badge-green">{live} live</span>
      <span className="badge badge-amber">{cooling} cooling</span>
      <span className="badge badge-red">{dark} dark</span>
      {unknown > 0 && <span className="badge">{unknown} unknown</span>}
    </div>
  );
}
```

## Quick verification
```bash
# Per-deal pulse is present and shaped right:
curl -s -H "Authorization: Bearer $DISPATCH_SECRET" \
  "$BACKEND_URL/api/deal-engine/opportunities" \
  | jq '.records[0].pulse | {state, summary, rep_outreach}'

# Book roll-up:
curl -s -H "Authorization: Bearer $DISPATCH_SECRET" \
  "$BACKEND_URL/api/deal-engine/sweep/status" | jq '.pulse_summary'
```
