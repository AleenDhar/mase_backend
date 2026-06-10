# C7 Spike Readout — v11.6 Audit Layer

**Date:** May 12, 2026
**Spike:** Task #17 — prove C7 (Waterfall Completeness) against a real Mapper run
**Status:** ✅ Complete. Strong **GO** recommendation for the full v11.6 build.

---

## TL;DR

The Supabase `chat_messages` log already contains everything the v11.6 audit layer needs to deterministically verify Phase 2 tool execution. The C7 check is ~330 lines of pure Python, runs read-only against existing data, and **caught the exact failure mode the briefing predicted on the first real run**.

Run against the actual Macquarie Mapper run (chat `87e36146-6e15-45f3-b10f-8b7b08cb21f5`, SF Account `0010O00002Lp2rN`):

```
Account-context tokens (auto-derived): ['0010O00002Lp2rN', 'group', 'macquarie']
web_search calls captured: 2
  seq=15  [web_search] 'Macquarie Group Limited procurement Coupa renewal'
  seq=16  [web_search] 'Macquarie Group procurement transformation GenAI Merlin Intake'
Angle coverage: 1/7
VERDICT: DIRTY — 6/7 angles missed.
Missing: A1_CPO, A2_Head_of_Procurement, A3_Head_of_Sourcing,
         A4_CFO, A6_LinkedIn_Procurement, A7_LinkedIn_Finance
```

The Mapper fired only 2 web searches; both were about Macquarie's procurement *strategy*, neither was a contact-discovery angle. Six of seven required waterfall angles never executed. Silent in production today, deterministically caught by ~330 lines of code.

Second run against AG&P (chat `f9beb15a`) showed the same pattern: 3 web searches, 1 angle covered. The one `site:linkedin.com/in` query was for a single named individual ("Mylen Tria"), not a roster-discovery query — it correctly does **not** count as A6 coverage. Pattern is **not** an isolated one-off.

---

## Does the log have what we need?

**Yes.** Concretely:

| Need | Where it lives | Confirmed |
|---|---|---|
| Tool name | `chat_messages.metadata->>tool` | ✓ |
| Search query string | `chat_messages.metadata->args->query` | ✓ |
| Other args (max_results, filters) | `chat_messages.metadata->args->*` | ✓ |
| Ordering (which call came first) | `sequence` + `created_at` | ✓ (sequence dedupes via tuple) |
| Linkage to a specific run | `chat_id` | ✓ |
| Distinguishing tool_call vs result | `type` column | ✓ |

`metadata` is JSONB. `args` is the literal LangChain `tool_call.get('args', {})` payload — full fidelity, not truncated.

## Gaps found (recommendations, not blockers)

1. **Duplicate rows from THREE write paths.** `server.py` writes tool_call rows from:
   - `1115` — generic tool wrapper (logs every tool call with `source: tool_wrapper`)
   - `1647` — sync handler
   - `1991` — stream handler

   Each call therefore appears 2–3 times. The spike dedupes by `(tool, query)` tuple, which works for diagnostics but collapses legitimate retries and can mask phase boundaries. **Recommendation:** add a `tool_call_id` column populated from LangChain's `tool_call['id']` so audit dedupe is based on a stable primary key.

2. **`sequence` numbers collide.** All three paths increment independent counters, so the same `seq` value can appear 3+ times. Sorting by `(sequence, created_at)` works but is brittle. **Recommendation:** single source-of-truth sequence per chat, or rely on `created_at` (microsecond resolution).

3. **Two distinct web-search tools in production:** `web_search` (38 calls observed) and `web_search_with_urls` (6 calls). Audit handles both. **Recommendation:** in v11.6, lock the Mapper to one. Pick `web_search_with_urls` since it returns URLs needed downstream.

4. **No phase-tagging.** Log is a flat stream — no marker for "Phase 2 starts/ends." For chats running all 5 phases, the audit needs a heuristic to scope which tool calls belong to Phase 2. **Recommendation:** v11.6's per-phase API call architecture makes this a non-issue, but design it in explicitly (e.g., add `phase_tag` column).

5. **Account-context derivation is heuristic.** The matcher requires queries to mention an account token (auto-derived from SF Account record_ids in `get_record` calls + recurring 4+ char tokens across web_search queries). For the two test runs the derivation produced the right tokens (`macquarie`/`group` and `ag&p`), but a Mapper run that fires zero web searches would yield no recurring tokens. The CLI `--account-token TOKEN` flag is the override hatch. **Recommendation in v11.6:** make account_id/account_name an explicit input to the audit layer rather than something it has to infer.

6. **Tool-call args sometimes typed as JSON string instead of dict.** Not observed in this dataset, but the LangChain payload type isn't strongly enforced. **Recommendation:** Pydantic schema at the audit boundary (already in v11.6 plan as Component A).

None block the v11.6 build. All are 1-line schema additions or planning notes.

## Go / No-Go recommendation: **GO**

The single riskiest assumption — "deterministic Python audit can verify execution from the existing log" — is **proven**. The remaining v11.6 components (Pydantic schemas, remediation orchestrator, separate Auditor LLM call, gate logic) are independent engineering work with no comparable architectural risk.

### Pre-work to add before kickoff (1–2 hrs total)

Before Gurv starts the full 4-week build:

1. Apply a small migration adding `tool_call_id` and `phase_tag` columns to `chat_messages` (or a separate `tool_calls` table per the briefing § Component F).
2. Pick one of `web_search` / `web_search_with_urls` and remove the other from the Mapper toolset.
3. Lock the 7 angle templates in `audit_spike/c7_waterfall_check.py:ANGLE_TEMPLATES` against the v11.5 Mapper system prompt — confirm with Sam that the role-keyword sets match the spec.

### What the spike did NOT touch (for awareness)

- Phase 4 anti-pattern checks (W1–W14) — independent, similar architecture, low risk.
- Remediation orchestrator — depends on Anthropic wrapper (D6, Day 1 afternoon).
- Auditor LLM call — separate problem, no dependency on this spike.
- Pydantic schemas — boilerplate, well-understood.
- Schema changes to `chat_messages` — explicitly out of spike scope.

### Cost of the spike

~2 hours of work. Read-only against production data. Zero changes to `server.py` or any agent runtime. Zero new dependencies.

---

## How to reproduce

```bash
python3 -m audit_spike.c7_waterfall_check 87e36146-6e15-45f3-b10f-8b7b08cb21f5
python3 -m audit_spike.c7_waterfall_check f9beb15a-ec7a-49fa-ae3e-266ec8f64db5
```

Exit code: 0 if CLEAN, 1 if any angle missed. Suitable for direct use as a pre-commit-style gate in v11.6.

CLI override (when no recurring tokens are auto-derived):

```bash
python3 -m audit_spike.c7_waterfall_check <chat_id> --account-token Macquarie
```

## Files

- `audit_spike/c7_waterfall_check.py` — the check (~330 LOC, stdlib + supabase only)
- `audit_spike/__init__.py` — package marker
- `audit_spike/READOUT.md` — this file
