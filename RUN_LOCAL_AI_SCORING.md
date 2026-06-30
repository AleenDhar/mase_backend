# Running the AI scorer locally with Claude Code ($0 Anthropic API)

This is the **Claude-Code path**: Claude Code itself is the judge, so no
`ANTHROPIC_API_KEY` and no API cost. The deterministic facts and guardrails come
from the repo; the per-score judgment is done by Claude Code reading a facts-only
evidence packet. You can grind the whole book this way over hours/days.

> This re-scores deals **without re-sweeping** them — every other field (Salesforce
> mechanics, MEDDPICC, competition, …) is left exactly as the last sweep produced it.
> Only `ai.deal_scores` is rewritten.

## 1. One-time setup

```bash
git clone <the mase_backend repo>
cd <repo>
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Create a `.env` at the repo root (these secrets are **not** in GitHub — get them from the owner):

```dotenv
SUPABASE_URL=https://<project>.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<service role key>     # read/write the deal book
DATALAKE_URL=https://<datalake project>.supabase.co
DATALAKE_SERVICE_KEY=<datalake service key>      # read Avoma meetings for the packet
# CORP_CA_BUNDLE=C:/path/to/corp-ca.pem          # ONLY behind a TLS-inspecting proxy (Zscaler)
```

You also need **Claude Code** installed and logged in. That's the only "model" —
there is no Anthropic API key in this flow.

## 2. The loop (dump → judge → apply)

**Step 1 — dump the facts.** Pick a scope:

```bash
python tools/score_dump.py --forecast            # the forecast block (Commit / Best Case / Upside)
python tools/score_dump.py --opps 006P...,006P... # specific deals
python tools/score_dump.py --all                 # the whole active book
```

This writes `tools/scores_io/packets.json` — one facts-only evidence packet per
deal (meeting counts/dates, stage, next step, MEDDPICC, competition, …). Deals
already flagged `dead` (Closed Lost / Qualified Out / a loss signal) are marked so
you skip them — they're forced to 0 + dead automatically in step 3.

**Step 2 — judge (this is the Claude Code part).** In Claude Code, open this repo and say:

> Read `tools/scores_io/packets.json`. For every deal that isn't `dead`, score it
> on the five dimensions per the current rubric and write
> `tools/scores_io/scores.json`.

Claude Code reads the packets, reasons over them, and writes `scores.json`. Shape:

```json
{
  "006P...": {
    "scores": {"win_position": 82, "deal_momentum": 78, "customer_commitment": 70,
               "deal_risk": 30, "forecast_confidence": 75},
    "read": "Accelerating",
    "reasons": {
      "win_position":  [{"text": "Vendor Selected; won the eval", "tone": "good"}],
      "deal_momentum": [{"text": "3 buyer meetings in 30d", "tone": "good"}],
      "deal_risk":     [{"text": "Close date slipped twice", "tone": "bad"}]
    }
  }
}
```

**Step 3 — apply.**

```bash
python tools/score_apply.py
```

This runs the production guardrails (`deal_engine_ai_scoring._normalize`), enforces
the dead/loss override (a lost deal reads 0 + dead no matter what the judge said),
builds the CRO panel, and writes `ai.deal_scores` back to Supabase.

For a big book, do it in batches: `score_dump.py --opps <batch>` → judge → `score_apply.py`, repeat.

## Notes
- **No Anthropic API is called anywhere in this path.** The judging is Claude Code; the
  scripts only read/transform/write.
- `tools/scores_io/` holds deal data and is git-ignored — it never gets committed.
- The scoring **rubric/formula** lives in the scorer (`deal_engine_ai_scoring.py` +
  its prompt). When the formula changes, that's the only thing that changes — this
  dump/apply plumbing stays the same.
- On a normal network leave `CORP_CA_BUNDLE` unset. Set it only if `requests`/datalake
  calls fail with `CERTIFICATE_VERIFY_FAILED` behind a corporate proxy.
