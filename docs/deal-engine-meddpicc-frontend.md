# Deal Engine — MEDDPICC + Stakeholders frontend contract

This tells the **Vercel frontend** how to render the rich MEDDPICC and stakeholder
data the backend now produces on every sweep. The backend is done; the only change
is on the frontend — point the MEDDPICC panel at the new `ai.meddpicc` field instead
of the old boolean/gap fields.

## Where to implement
- Repo: the **Vercel frontend** (NOT this FastAPI backend repo).
- View: the **deal detail page** — the MEDDPICC section/panel, and the stakeholder
  list/contacts section.
- Nothing else changes: same endpoint, same auth, same page. You are swapping which
  field the panel reads.

## Data source
- `GET /api/deal-engine/opportunities/{opp_id}`
- Header: `Authorization: Bearer <DISPATCH_SECRET>` (same token the rest of the deal
  endpoints already use).
- The MEDDPICC + stakeholder data live at the **top level** of the response under `ai`
  (it is NOT nested under a `record` key):

```jsonc
{
  "opp_id": "006P700000Kpl1O",
  "hard": { /* Salesforce facts */ },
  "ai": {
    "meddpicc": { /* see below */ },
    "stakeholder_map": { "items": [ /* see below, max 7 */ ] },
    /* ...other ai.* fields (gaps, champion_strength, etc.) — legacy, do not use for MEDDPICC */
  },
  "swept_at": "2026-06-10"
}
```

## `ai.meddpicc` shape
Always exactly **8 keys** (stable order to render in):

```
metrics, economic_buyer, decision_criteria, decision_process,
paper_process, identify_pain, champion, competition
```

Each value is:

```ts
type MeddpiccElement = {
  status: "confirmed" | "partial" | "gap";  // drives the badge color
  narrative: string;   // the rich RevOps-level write-up (~800–2000 chars). RENDER THIS.
  sources: string[];   // evidence citations, each a short string. Render as a list/tooltip.
};

type Meddpicc = {
  metrics: MeddpiccElement;
  economic_buyer: MeddpiccElement;
  decision_criteria: MeddpiccElement;
  decision_process: MeddpiccElement;
  paper_process: MeddpiccElement;
  identify_pain: MeddpiccElement;
  champion: MeddpiccElement;
  competition: MeddpiccElement;
};
```

Display labels:

| key | label |
|---|---|
| `metrics` | Metrics |
| `economic_buyer` | Economic Buyer |
| `decision_criteria` | Decision Criteria |
| `decision_process` | Decision Process |
| `paper_process` | Paper Process |
| `identify_pain` | Identify Pain |
| `champion` | Champion |
| `competition` | Competition |

Suggested badge colors: `confirmed` → green, `partial` → amber, `gap` → red.

## `ai.stakeholder_map` shape
Already capped at **≤7** by the backend — render all items, no client-side slicing.

```ts
type Stakeholder = {
  name: string;
  title: string;
  role: string;                 // e.g. "Economic Buyer", "Champion"
  sentiment: string;            // "positive" | "neutral" | "unknown" | ...
  risk: string;                 // short risk note (may be empty)
  last_contact_date: string | null; // ISO date or null
};

type StakeholderMap = { items: Stakeholder[] };
```

## What to STOP rendering for MEDDPICC
The thin one-liners you see today come from legacy/derived fields — do **not** use these
for the MEDDPICC panel anymore:
- `ai.gaps` and `ai.best_practice_check` (e.g. "No quantified value case")
- `hard.eb_identified` / `hard.metrics_identified` / ... booleans (e.g. "No EB in the
  stakeholder map")
- `ai.champion_strength` (e.g. "Carina Lööf — developing")

These remain in the payload for other widgets, but the MEDDPICC section should read
`ai.meddpicc` exclusively.

## Minimal render example (React/TSX)

```tsx
const MEDDPICC_ORDER = [
  ["metrics", "Metrics"],
  ["economic_buyer", "Economic Buyer"],
  ["decision_criteria", "Decision Criteria"],
  ["decision_process", "Decision Process"],
  ["paper_process", "Paper Process"],
  ["identify_pain", "Identify Pain"],
  ["champion", "Champion"],
  ["competition", "Competition"],
] as const;

const BADGE = { confirmed: "green", partial: "amber", gap: "red" } as const;

function MeddpiccPanel({ meddpicc }: { meddpicc: Meddpicc }) {
  return (
    <div className="meddpicc">
      {MEDDPICC_ORDER.map(([key, label]) => {
        const el = meddpicc?.[key];
        if (!el) return null;
        return (
          <section key={key} className="meddpicc-element">
            <header>
              <h4>{label}</h4>
              <span className={`badge badge-${BADGE[el.status]}`}>{el.status}</span>
            </header>
            <p>{el.narrative}</p>
            {el.sources?.length > 0 && (
              <details>
                <summary>Sources ({el.sources.length})</summary>
                <ul>{el.sources.map((s, i) => <li key={i}>{s}</li>)}</ul>
              </details>
            )}
          </section>
        );
      })}
    </div>
  );
}
```

## Quick verification
Before/after wiring, hit the endpoint for any forecast deal and confirm `ai.meddpicc`
has 8 keys, each with a multi-sentence `narrative` and a `sources` array:

```bash
curl -s -H "Authorization: Bearer $DISPATCH_SECRET" \
  "$BACKEND_URL/api/deal-engine/opportunities/006P700000Kpl1O" \
  | jq '.ai.meddpicc | keys, (.metrics.narrative | length)'
```
