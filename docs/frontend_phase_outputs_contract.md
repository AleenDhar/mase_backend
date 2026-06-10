# Phase Outputs — Frontend Parsing Contract

Use this as the prompt / spec for the frontend agent that renders the
automations table (the one in the screenshot with one column per phase).

---

## Data source

Read `automation_tasks` from Supabase. Each row represents one pipeline run
and has these relevant fields:

| Column | Type | What it is |
|---|---|---|
| `id` | uuid | task id |
| `chat_id` | uuid | linked chat |
| `status` | text | `pending` \| `running` \| `complete` \| `failed` \| `stopped` |
| `last_phase_index` | int | 1-based index of the most recently started/completed phase |
| `last_phase_total` | int | total number of phases in this run |
| `last_phase_name` | text | display name of that phase |
| `phase_outputs` | jsonb | array of per-phase result objects (see below) |
| `error_message` | text \| null | populated on `failed` |

Poll the row every 2 s while `status === 'running'`. Stop polling once
status is terminal (`complete` / `failed` / `stopped`).

---

## `phase_outputs` shape

`phase_outputs` is a JSON array, **sorted by `phase_position` ascending** by
the backend. Each element is:

```ts
type PhaseOutput = {
  phase_index: number;        // 1-based ordinal in the enabled phase list
  phase_position: number;     // stable position from the pipeline spec (use this as the dedupe key)
  phase_name: string;         // human-readable phase name, e.g. "Account-Identity Lock"
  phase_model_id: string;     // raw model id, e.g. "claude-sonnet-4-6-20260901"
  content: string;            // the agent's final text output for this phase (markdown)
  completed_at: string;       // ISO-8601 UTC timestamp
  input_tokens: number;       // tokens consumed by THIS phase only
  output_tokens: number;      // tokens produced by THIS phase only
  total_tokens: number;       // input + output
  cost_usd: number;           // USD cost for THIS phase only, 6-decimal float
};
```

### Important semantics

- **Per-phase, not cumulative.** `cost_usd` and `*_tokens` are computed as
  deltas of the chat's cumulative `chat_usage` row, so each phase's number
  is **only that phase's contribution** — do not sum them yourself unless
  you want a manual total.
- **Idempotent re-runs.** When a phase re-runs, its entry replaces the
  previous one at the same `phase_position`. Use `phase_position` as the
  React key, not `phase_index`.
- **Empty / missing.** A phase that hasn't started yet is absent from the
  array. A phase that errored is also absent (the error is in the chat
  stream + `automation_tasks.status` flips to `failed`). Always handle
  the missing case.
- **Possible jsonb-as-string.** Supabase sometimes returns jsonb as a
  parsed object, sometimes as a JSON string. After fetch, run:
  ```ts
  const phases = typeof row.phase_outputs === 'string'
    ? JSON.parse(row.phase_outputs || '[]')
    : (row.phase_outputs ?? []);
  ```

---

## Rendering each phase cell

For each phase cell in the table, render:

1. **Title bar** — `Phase {phase_position}` + optional `phase_name`.
2. **Model badge** — small pill showing the friendly model name (see mapping
   below). Tooltip = raw `phase_model_id`.
3. **Cost badge** — `$ {cost_usd.toFixed(4)}`. Tooltip = `{total_tokens.toLocaleString()} tokens (in: {input_tokens}, out: {output_tokens})`.
4. **Content preview** — first N lines of `content` (markdown rendered).
   Expand-on-click for full content.

### Model id → friendly name mapping

The backend sends the raw model id. Map for display:

```ts
function friendlyModelName(modelId: string): string {
  if (!modelId) return 'unknown';
  const id = modelId.toLowerCase();
  if (id.startsWith('claude-sonnet-4-6')) return 'Claude Sonnet 4.6';
  if (id.startsWith('claude-sonnet-4-5')) return 'Claude Sonnet 4.5';
  if (id.startsWith('claude-sonnet-4'))   return 'Claude Sonnet 4';
  if (id.startsWith('claude-opus-4'))     return 'Claude Opus 4';
  if (id.startsWith('claude-haiku-4'))    return 'Claude Haiku 4';
  if (id.startsWith('claude-3-5-sonnet')) return 'Claude 3.5 Sonnet';
  if (id.startsWith('gpt-5'))             return 'GPT-5';
  if (id.startsWith('gpt-4o'))            return 'GPT-4o';
  if (id.startsWith('gpt-4'))             return 'GPT-4';
  if (id.startsWith('gemini-2'))          return 'Gemini 2';
  if (id.startsWith('gemini-1'))          return 'Gemini 1.5';
  return modelId;
}
```

Unknown ids fall back to the raw id — that's intentional so new models
show up even before the mapping is updated.

### Cost formatting

```ts
function formatCost(usd: number): string {
  if (usd === 0) return '$0.00';
  if (usd < 0.01) return `$${usd.toFixed(4)}`;   // $0.0034
  return `$${usd.toFixed(2)}`;                   // $1.23
}
```

### Token formatting

```ts
function formatTokens(n: number): string {
  if (n < 1_000) return `${n}`;
  if (n < 1_000_000) return `${(n / 1_000).toFixed(1)}k`;
  return `${(n / 1_000_000).toFixed(2)}M`;
}
```

---

## Worked example

A row mid-run might look like:

```json
{
  "id": "11111111-1111-1111-1111-111111111111",
  "chat_id": "cb138862-c3d2-4d4c-91ea-94e1815661d4",
  "status": "running",
  "last_phase_index": 3,
  "last_phase_total": 6,
  "last_phase_name": "Sequence Drafting",
  "phase_outputs": [
    {
      "phase_index": 1,
      "phase_position": 1,
      "phase_name": "Account-Identity Lock",
      "phase_model_id": "claude-sonnet-4-6-20260901",
      "content": "**Phase 1 complete.** ...",
      "completed_at": "2026-05-18T15:17:30.444Z",
      "input_tokens": 12450,
      "output_tokens": 3120,
      "total_tokens": 15570,
      "cost_usd": 0.5941
    },
    {
      "phase_index": 2,
      "phase_position": 2,
      "phase_name": "Contact S-State Classification",
      "phase_model_id": "gpt-5-4",
      "content": "**ABM Campaign Run — ...**",
      "completed_at": "2026-05-18T15:21:44.997Z",
      "input_tokens": 8210,
      "output_tokens": 2100,
      "total_tokens": 10310,
      "cost_usd": 0.0141
    }
  ]
}
```

Renders as: Phase 1 card (Claude Sonnet 4.6, $0.5941) → Phase 2 card
(GPT-5, $0.0141) → Phase 3 cell showing a spinner with
`last_phase_name = "Sequence Drafting"` → Phase 4–6 cells greyed out
("pending").

---

## What NOT to do

- Don't compute cost client-side from tokens — the backend already did it
  using the correct per-model price table.
- Don't sort or dedupe by `phase_index`; it can collide on re-runs. Use
  `phase_position`.
- Don't render `content` as plain text — it's markdown and frequently
  contains tables.
- Don't infer phase progress from `phase_outputs.length` alone — a phase
  can be in-flight without an entry yet. Use `last_phase_index` /
  `last_phase_total` for the progress indicator.
