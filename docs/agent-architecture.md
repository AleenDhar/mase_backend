This is the critical correction. The doc at line 7 says the SF write is "**direct, server-side, human-initiated**" with a confirm modal (line 20-21). The draft architecture's §3.3/§3.6/P3 has the *agent* perform the SF write ("wire the Completed-SF-Task push... as the one sequential mutation so the agent can actually complete a todo"). The critique flags this as a major: "Agent given a human-gated write → Keep human confirm." The critique is correct and the draft violated the actual product contract. The agent must draft/stage; the human confirms; the server writes.

I now have everything grounded. Writing the final architecture.

# MASE Document-Aware Task-Completion Agent — Final Engineering Architecture & Phased Build Plan

**Author:** Lead Architect
**Date:** 2026-06-18
**Status:** Final design — ready for engineering
**Scope:** A document-aware task-completion agent for Zycus sales/RevOps on MASE's real stack: Python `deepagents`/`create_deep_agent`, Supabase Postgres + pgvector (`match_document_chunks`), OpenAI embeddings, `custom_tools/`, the Showpad MCP, and the Run-with-AI UI.

This revision folds in the adversarial critique. Five changes are load-bearing and were re-verified against the repo before writing:

1. **ACL / RLS (blocker)** — `upload_document(request_body: dict)` at `server.py:4124` has no auth dependency; service-key access bypasses RLS. Fixed in §5 (now P0-gating).
2. **Concurrency (blocker)** — `agent_manager` is a module-level singleton (`server.py:1894`) with a single mutable `self.agent` (`server.py:738`, `:1835`) reinitialized from ~8 call sites. Per-run instances are now the foundational fix (§2.1, P2 → promoted into P0a).
3. **SF write (major)** — the todo-push is "**direct, server-side, human-initiated**" with a confirm modal (`docs/deal-engine-todo-push.md:7,20`). The draft wrongly handed the agent the write. **Reverted: agent stages, human confirms, server writes** (§3.3, §3.6, P3).
4. **Embedding migration (major)** — the 3072-dim change is a cross-team pgvector dimension change; it is now scheduled **strictly after** P0 lands, as its own isolated phase (§1.1, P1b).
5. **Eval (major)** — added an **ACL-leakage eval** (assert no cross-tenant/cross-user chunk ever returns) and removed the self-grading judge (judge corpus is held-out + human-anchored) (§4).

---

## 0. Grounding facts that constrain every decision

Verified against the repos, not assumed:

- **Model & economics (claude-api skill).** Agent runs on **`claude-opus-4-8`** ($5 / $25 per MTok, 1M context, 128K output). Prompt caching is a **strict prefix match** (`tools → system → messages`); any byte change in the prefix invalidates everything after. Cache reads ≈ 0.1×, writes ≈ 1.25×. **Consequence:** per-deal context that varies every turn goes in the **user message**, never the system prefix.
- **No first-party Anthropic embeddings.** Everything is Messages + Files + Batches. Embeddings stay OpenAI. Do not introduce an "Anthropic embedding."
- **Vector store is real but thin.** `search_knowledge` (`custom_tools/search_knowledge.py`), `list_documents`, one write path `POST /api/documents/upload`. `documents`/`document_chunks` carry no `doc_type`/`metadata`. `match_document_chunks` RPC + DDL live in Supabase (Next.js-owned) — **schema changes are cross-team**. `match_threshold=-1.0` (`search_knowledge.py:424,451`) means **no relevance floor today**.
- **Five typed work items** via `deal_engine_store.py derive_todo()`: `critical`/`important`/`explicitRequirements`/`implicit`/`bestPractice`, keyed by `todo_key`. Completion is a **direct, server-side, human-initiated** SF Task write behind a confirm modal (`docs/deal-engine-todo-push.md:7,20`), idempotent on `todo_key` (`:199`).
- **Showpad** is a disjoint, non-ingested, live read-only MCP surface (char-paginated at 20k, no embeddings, no OCR).
- **Hermes offers no RAG** (BM25 over tools, FTS5 over sessions). The vector/document layer is **net-new** for MASE; from Hermes we borrow *mechanisms* (background-review learning, skills+curator, structured compression, anti-fabrication footer, structural continue/stop, one-shot recovery), not a RAG implementation.

---

## 1. Document Retrieval Architecture (the #1 ask)

### 1.1 Vector DB design on the existing Supabase

#### Schema upgrades — highest-leverage change

Today `documents`/`document_chunks` carry only `{id, name, file_path, content, embedding, project_id}` — you can filter only by `project_id`, joined `chat_id`, or `name ILIKE`. You cannot say "case studies for this competitor." Fix it.

**`documents` (add columns):**

```sql
ALTER TABLE documents
  ADD COLUMN doc_type      text,        -- 'playbook'|'guide'|'showpad_asset'|'transcript'|'sf_field'|'email_template'|'chat_upload'
  ADD COLUMN source        text,        -- 'showpad'|'manual_upload'|'avoma'|'salesforce'|'playbook_json'
  ADD COLUMN title         text,
  ADD COLUMN content_hash  text,        -- sha256(content) for upsert/dedup
  ADD COLUMN recency_at    timestamptz, -- authoritative recency
  ADD COLUMN tenant_id     text,        -- ACL: hard tenant boundary (see §1.4)
  ADD COLUMN acl_scope     jsonb,       -- {owner_email, visibility, allowed_roles[]}
  ADD COLUMN source_id     text,
  ADD COLUMN metadata      jsonb DEFAULT '{}';
CREATE UNIQUE INDEX ON documents (source, source_id) WHERE source_id IS NOT NULL;
```

**`document_chunks` (denormalize for SQL-side pre-filtering):**

```sql
ALTER TABLE document_chunks
  ADD COLUMN doc_type     text,
  ADD COLUMN section      text,
  ADD COLUMN recency_at   timestamptz,
  ADD COLUMN tenant_id    text,         -- denormalized ACL key (so the RPC filters without a join)
  ADD COLUMN acl_scope    jsonb,        -- denormalized
  ADD COLUMN token_count  int,
  ADD COLUMN metadata     jsonb DEFAULT '{}',
  ADD COLUMN content_fts  tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED;
CREATE INDEX ON document_chunks USING GIN (content_fts);
CREATE INDEX ON document_chunks (tenant_id, doc_type, project_id);  -- ACL + metadata pre-filter
CREATE INDEX ON document_chunks (recency_at);
-- ANN index type: confirm with Supabase team; recommend HNSW over IVFFlat for recall stability.
```

**Why denormalize `doc_type`/`recency_at`/`tenant_id` onto chunks:** the ANN path is a single RPC; pushing filters into SQL (`WHERE tenant_id = $t AND doc_type = ANY($types)`) lets pgvector prune *before* ranking, replacing today's "fetch all chunks, rank in Python" fallback (`search_knowledge.py:294`), which is O(all chunks) over REST.

**Cross-team gate (load-bearing):** DDL + `match_document_chunks` body are Supabase-owned. **P0 cannot ship without a coordinated migration + RPC signature change with the Next.js team.** Treat the new RPC signature as the contract (§1.2).

#### Embedding model — keep ada-002 through P0; migrate in its own isolated phase

**Critique fix (major):** the draft scheduled the 3072-dim cutover alongside the schema change, doubling cross-team risk on the gating phase. Revised:

- **P0 ships the schema + hybrid retrieval on the existing `text-embedding-ada-002` (1536-dim).** No dimension change, no re-embed. This de-risks the gating phase to *additive columns + a new RPC* only.
- **P1b** (separate, after P0 + ingestion are stable) migrates `ada-002 → text-embedding-3-large` (`dimensions` truncation available — store 1024 or 3072 per index-cost call; see Open Decisions). Requires: `embedding_model` column on both tables, a Batches-style off-hours backfill, **dual-read** (query embeds with the model recorded on the target corpus), and a flip only when coverage is 100%. Keep the unit-normalized dot-product == cosine invariant (`search_knowledge.py`) — verify `3-large` normalization before flip.
- Collapse the duplicated model constant (`server.py:4195`, `search_knowledge.py:213`) into **one shared config** now — that drift is a latent bug regardless of the upgrade.

#### Chunking strategy per doc type (token-aware, structure-aware)

Replace the naive fixed 1000-char/200-overlap windower (`server.py:4167`) with a **token-aware, doc-type-dispatched chunker** (same OpenAI tokenizer family as the embedder). Reuse Showpad's extractors (`showpad_mcp_server.py:184-263`) as the binary→text front end.

| doc_type | Chunking | Section metadata |
|---|---|---|
| **playbook** (plays) | One chunk **per play** (atomic ~200-400 tok); never split a play. | `lever`, `stage[]`, `competitor[]`, `vertical[]`, `motion[]` → `metadata` (mirrors `matchPlays()`, `helpers.ts:395`). |
| **guide** (`prompts/*.md`) | Markdown-heading-aware ~512 tok, 64 overlap; never split mid-heading. | heading path → `section`. |
| **showpad_asset** | 512-tok windows, 64 overlap; preserve slide/page boundaries. | slide/page → `section`; tags/type → `metadata`. |
| **transcript** (Avoma) | Speaker-turn-aware; group turns up to ~512 tok; never split a turn. | speaker+timestamp → `section`; meeting date → `recency_at`. |
| **sf_field** | Whole field = one chunk; split long history by entry. | field name → `section`. |

### 1.2 Retrieval strategy — hybrid, ACL-scoped, metadata-filtered, reranked, deduped, cited

New RPC contract (Supabase-side):

```
match_document_chunks_v2(
  query_embedding vector,
  match_count     int,
  match_project_id uuid,
  acl_tenant_id    text,                       -- REQUIRED, non-null; hard ACL boundary (see §1.4)
  acl_user_email   text,                       -- for visibility/owner checks
  filter_doc_types text[]    DEFAULT NULL,
  filter_metadata  jsonb     DEFAULT '{}',
  min_similarity   float     DEFAULT 0.30,     -- REAL floor (replaces -1.0)
  recency_after    timestamptz DEFAULT NULL,
  filter_document_ids uuid[] DEFAULT NULL
)
```

**Pipeline (new `custom_tools/retrieve_documents.py`, wrapping `search_knowledge`):**

1. **Route → corpus + strategy** (§1.3) → `filter_doc_types`, `filter_metadata`, `min_similarity`.
2. **Resolve ACL** from the run identity (§1.4) → `acl_tenant_id`, `acl_user_email` (never client-supplied).
3. **Embed query** (same model as target corpus).
4. **Hybrid retrieve:** vector (`match_document_chunks_v2` with the real floor + ACL + metadata) **‖** keyword/FTS (`content_fts @@ websearch_to_tsquery($q)`, same ACL filter) — FTS catches SKUs, product/competitor names, exact phrases dense retrieval under-recalls.
5. **Fuse** with Reciprocal Rank Fusion (RRF, `k≈60`) — replaces the two divergent ranking paths that can disagree today.
6. **Rerank** fused top-N (40→8). **P0 ships RRF only** (no reranker — keeps the gating phase lean). Reranker added in P2: start with a **`claude-haiku-4-5` subagent rerank** (no new vendor; isolated context so it doesn't pollute the main cache), benchmark vs Cohere/Voyage.
7. **Dedupe** by chunk `id` and by `content_hash`.
8. **Cite:** return `{content, document_name, doc_type, source, section, similarity, document_id, source_id}`. `source_id` deep-links a Showpad asset / SF record. Every used fact carries a citation, enforced by the anti-fabrication gate (§3.5).

**Keep MASE's cost governance, made doc-type-aware** (the cap/dedupe were born from real cost-burn incidents — `search_knowledge.py:23-202`):
- Cap **per doc_type per turn** (e.g. ≤8 transcript, ≤5 playbook) so one corpus can't starve the budget.
- Dedupe on `(normalized_query, doc_type, metadata_filter)`, not just the query string.
- Keep `list_documents` preflight, extended to report `doc_type` counts so the agent confirms a corpus is populated before searching.

### 1.3 Doc-type ROUTING — deterministic, keyed on the five task categories

Routing is **deterministic** (mirrors `derive_todo`), not an LLM guess. Encode as `TASK_RETRIEVAL_ROUTING`:

| Task category | Primary corpus | `metadata` filter | Notes |
|---|---|---|---|
| `critical` | `playbook` + `showpad_asset` | competitor+vertical+stage+motion | Right play + right asset. High `min_similarity`. |
| `important` | `transcript` + `sf_field` | account | Verify what we promised; ground in buyer's words. |
| `explicitRequirements` | `transcript` (Avoma-first) + `sf_field` | account | Ask phrasing is buyer-voice → transcript FTS high-signal. |
| `implicit` | `transcript` + `showpad_asset` | account+topic | Concern + collateral that addresses it. |
| `bestPractice` | `playbook` + `guide` | lever | Pattern intelligence, not a deliverable. |

**Showpad reconciliation (disjoint surface):** two-phase — (a) the `showpad_asset` corpus is **embedded metadata + extracted summaries** ingested in P1 (semantic discovery), and (b) when the agent picks an asset it fetches full live content via the Showpad MCP `get_asset_content` (`showpad_mcp_server.py:266`) for freshness. Semantic discovery without losing the live source of truth.

### 1.4 ACL / multi-tenant isolation (BLOCKER fix — new section)

The critique's top blocker: RLS disabled, service-key bypass, no-auth upload. Verified — `upload_document(request_body: dict)` (`server.py:4124`) takes a raw dict with no auth dependency. Fix, end to end:

- **Authenticate every write/read path.** Add an auth dependency to `POST /api/documents/upload` and every retrieval entry point; resolve identity (`userEmail`/tenant) **server-side from the verified session**, never from the request body.
- **Re-enable Supabase RLS** on `documents`/`document_chunks` and **stop using the service-role key for user-scoped reads.** User-scoped queries run under a request-scoped token so RLS is the backstop even if app-layer filtering has a bug.
- **Defense in depth:** RLS (DB) + mandatory non-null `acl_tenant_id` in `match_document_chunks_v2` (app contract) + the leakage eval (§4). A retrieval call without a resolved tenant must **fail closed**, not fall back to project-wide.
- **Ingestion stamps ACL:** every chunk inherits `tenant_id`/`acl_scope` from its document at write time (denormalized, §1.1). Showpad/Avoma/SF ingests map source ownership → `acl_scope`.
- **Per-user vs per-tenant visibility** is an Open Decision (§ end) — the schema supports both (`tenant_id` hard boundary + `acl_scope.visibility`); the policy is a human call.

---

## 2. Context Awareness — per (task_type, deal, user)

Borrowing Hermes's `ContextEngine`/`ContextCompressor` ideas, adapted to deepagents + the prompt-cache invariant.

### 2.1 Concurrency-safe agent instances (BLOCKER fix) + the context seam

**Critique blocker, verified:** `agent_manager` is a module-level singleton (`server.py:1894`) holding one mutable `self.agent` (`server.py:738`, set at `:1835`), reinitialized in place by `reinitialize_agent` from ~8 call sites (`server.py:2989, 3121, 3792, 3930, 4406, 4545, 4635, 4652`). Under concurrent runs, run B's `reinitialize_agent` swaps the prompt/tools out from under run A → cross-run prompt bleed and cache shatter.

**Fix (foundational, promoted to P0a):** **per-run agent instances.** Build the agent for a run from an immutable `(system_prefix, toolset)` spec and hold it on the run's own context, not on the shared singleton. Concretely:
- Replace in-place `reinitialize_agent` mutation with a `build_agent_for_run(spec) -> agent` factory; each request/run gets its own instance (or a keyed cache by spec hash so identical specs share a warm instance without mutation).
- The shared `agent_manager` keeps only *immutable* shared resources (tool registry, MCP clients), never per-run prompt state.
- This simultaneously fixes the cache anti-pattern: the per-run system prefix is **frozen for the session** and replayed byte-stable.

**Context seam (Hermes prologue→loop→epilogue):** one place each to (a) hydrate deal/account/user context, (b) run act-observe, (c) verify "did the task actually complete?" + audit. Implement `build_task_context()`:

- **Stable cached system prefix (built ONCE per session, byte-stable):** task-completion operating brief, five-category semantics, read-only + anti-fabrication discipline, DEAL PULSE one-story rule, tool catalog. Replayed byte-stable to keep the cache warm. This is the fix for the old `reinitialize_agent`-per-run rebuild.
- **Volatile per-turn block (in the user message, never the system prefix):** this opp's live fields, the rep's message, the specific todo (category + `todo_key` + primary text/date), retrieved snippets, deal/user memory cards. On Opus 4.8, operator-authority context (mode toggles, budget countdowns) may use a mid-conversation `{"role":"system"}` message (beta `mid-conversation-system-2026-04-07`) without touching the cached prefix.

### 2.2 Per-(task_type, deal, user) assembly

```
build_task_context(todo, deal_record, user) -> TaskContext:
  acl          = resolve_acl(user)               # tenant_id + email, server-side (§1.4)
  pulse        = _pulse_of(deal_record)          # live|cooling|dark (deal_engine_store.py:1051)
  routing      = TASK_RETRIEVAL_ROUTING[todo.category]
  retrieved    = retrieve_documents(... routing, acl ...)   # §1.2, ACL-scoped, capped/deduped
  deal_memory  = load_deal_memory(deal_record.opp_id)
  user_memory  = load_user_memory(user.email)
  return TaskContext(stable_brief=CACHED, volatile_block={
     outstanding_ask, deal_snapshot(pulse, stage, amount, competitor),
     todo, retrieved_citations, deal_memory_card, user_card })
```

**Compression** uses the Hermes two-stage trigger (preflight estimate + post-tool real `prompt_tokens`) with **Opus 4.8 server-side compaction** (beta `compact-2026-01-12`) as primary. Upgrade the existing `ContextTrimMiddleware` (`server.py:118-136`) from 400-char placeholder truncation to the structured-summary template below.

### 2.3 User/deal memory model

Two durable stores, both **ACL-scoped** and following Hermes's frozen-snapshot-at-start + immediate durable writes discipline:

- **Deal memory** (`deal_memory`, keyed `opp_id`): accreting card — competitors, blockers, EB/DM map, last-sent artifacts. Follows the **living-memory carry-forward rule** (`deal_engine_sweep_system_prompt.md:241`): never drop a known competitor on silence; retire only on explicit signal. Combined with temporal anchoring, this stops re-drafting an already-sent email.
- **User memory** (`user_card`, keyed `email`): rep preferences (currency display, momentum-sorted lists, voice/signature). Accrues via explicit corrections. Per-user scoped.

**Compression summary template** (temporal-anchored so a resumed/compacted session never re-issues done work):

```
## Outstanding Ask        (rep's last unfulfilled request, verbatim)
## Active Deal            (opp id, account, stage, pulse, amount, competitor)
## Completed Actions      (dated past-tense: "Drafted security-review email 2026-06-16; staged SF Task push for todo_key …")
## Pending Asks
## Blocked                (with exact API errors)
## Key Decisions
[REDACTED] all credentials/PII in prompt AND output
```

Wrap every summary in Hermes's **SUMMARY_PREFIX reference-only contract**: treat as background; respond only to the latest message; reverse signals (stop/never mind) end in-flight work. For a side-effecting agent this prevents "wrapping up" a stale task after the rep moved on.

---

## 3. The Agent Itself

Built on `create_deep_agent`, per-run system-prompt template (MASE's "specialization = prompt, not backend"), prompt **frozen per session** for cache-safety, **instance per run** for concurrency-safety (§2.1).

### 3.1 Task decomposition / planning

Adopt Hermes's **structural continue-vs-stop**: act while emitting tool calls, stop on final answer — no separate planner second-guessing completion. Keep MASE's `_agent_astream_autocontinue` loop (`server.py:2568`). Re-enable a **scoped `write_todos`** (globally disabled today, `deepagents_patches.py:25`) **only** for multi-step `critical`/`implicit` items (draft→ground→verify); single-step items skip it.

### 3.2 SKILLS layer — playbooks/guides → agent skills (self-improving)

Hermes skills system applied to sales-ops:
- **Class-level skills:** "draft a security-review email", "prep a discovery call from Avoma+SF", "build a Coupa-displacement narrative" — mapping to the five categories × playbook `lever` taxonomy.
- **Seed from existing assets:** `playbook.json` plays + `prompts/*.md` guides (ingested P1).
- **Self-improving via background-review fork** (`background_review.py:428`): after a response ships, a forked deep-agent (memory+skills tools only, inheriting the parent's cached prefix byte-for-byte for cache reuse) replays the transcript and patches/creates skills. **Provenance gate** (`skill_provenance.py:75`): only background-created skills get `created_by:"agent"` and are curation-eligible; rep-directed saves are user-owned forever.
- **Anti-self-sabotage denylist:** never persist "tool X is broken", environment-dependent or transient MCP failures — given MASE's flaky MCP surface, these would harden into self-cited refusals.
- **Curator** (`curator.py:276`): inactivity-triggered deterministic prune (active→stale 30d→archived 90d, never deletes, pinned exempt, tar.gz backup). Expensive LLM umbrella-consolidation **off by default** behind a long interval.
- **Skills inject as user/tool messages, not the system prefix** — cache-safe.

### 3.3 Tool design — one registry, one dispatch, arg-coercion; staged (not executed) writes

- **Single self-registering registry + single dispatch** (Hermes `registry.py`/`handle_function_call`). MASE already routes through `_wrap_mcp_tool` + `make_error_safe` (`server.py:1421-1476`) — formalize so guardrails/audit/coercion fire once everywhere.
- **Arg-coercion against JSON schema** (Hermes `model_tools.py:619`): string→int/bool, JSON-string→array; **refuse truncated tool args** (non-`}`-terminated) so a half-written payload never executes.
- **The SF completion is NOT an agent tool (critique major fix).** Verified contract: the Completed-SF-Task write is **direct, server-side, human-initiated** behind a confirm modal (`docs/deal-engine-todo-push.md:7,20`). The draft wrongly gave the agent this write. **Final:** the agent produces a **staged push proposal** (`{todo_key, opp_id, category, draft_artifact}`); it surfaces to the rep; the rep confirms in the existing modal; the **server** performs the idempotent-on-`todo_key` write (`:199`). The agent never mutates Salesforce. The ~100 read-only SF/MCP lookups remain agent tools.
- Read-only batches may run concurrently (Hermes `tool_executor.py:243`); there is now **no agent-side mutation to serialize** (the one write moved server-side behind human confirm).

### 3.4 Subagents for multi-step tasks

deepagents subagents for: (a) the **Haiku reranker** (§1.2, P2) — isolated context, returns only the ranked list; (b) **parallel multi-corpus retrieval** for a `critical` move (playbook+Showpad+transcript fan-out). Children get fresh context, toolset = parent ∩ (minus a hard blocklist: no recursive delegation, no staged-push emission, no memory-write), and return **only a summary**. **Spawn a Haiku subagent rather than switching the main loop's model** — model switch mid-session invalidates the cache.

### 3.5 Grounding / anti-fabrication

MASE's gate + Hermes's footer, combined:
- **Reuse MASE's deterministic fabrication gate** (`deal_engine_sweep_system_prompt.md:11-21`): never invent people/competitors/quotes/prices; cite every claim; server owns `manager_name`/`hard.*`.
- **Hermes completion-verifier footer** (`turn_finalizer.py:204`): the epilogue appends a *verified* side-effect summary from **real tool results**, not model prose — e.g. "Drafted email (NOT sent)", "Staged SF Task push for confirmation (NOT written)". Over-claiming becomes structurally impossible; and the footer must reflect that the SF write is *staged, pending human confirm* — never "I updated Salesforce."
- **Citation enforcement:** every factual claim in a draft maps to a retrieved chunk's `document_id`/`source_id`; uncited claims are stripped or trigger `NEEDS HUMAN` (generalizes MASE's existing sentinel).
- **Wrap untrusted tool output** (Avoma transcripts, emails, Showpad text, web) in `<untrusted_tool_result> data-not-instructions` delimiters (Hermes `tool_dispatch_helpers.py:336`) — prompt-injection vector, especially as self-authored skills can re-enter context.

### 3.6 Per-task-type behavior

| Category | Owner / framing | Done = | Retrieval |
|---|---|---|---|
| `critical` | buyer-owned, future-dated (rank-1 ≤14d), net-new | buyer-facing next-step artifact drafted + asset attached | playbook+showpad |
| `important` | ours | promised artifact drafted, grounded in the commitment | transcript+sf_field |
| `explicitRequirements` | prospect ask | `addressed=true` artifact | transcript-first |
| `implicit` | we volunteered | names exact artifact + recipient | transcript+showpad |
| `bestPractice` | strategy lever | a recommendation, not a deliverable | playbook+guide |

"Done" means **the artifact is drafted and the staged push is ready for human confirm** — not that the SF Task exists. **Never propose CRM-hygiene as a task** (banned moves, `deal_engine_sweep_system_prompt.md:213`). **Suppress anything contradicting a live DEAL PULSE** (`_pulse.flag_contradicts_live_pulse`).

### 3.7 Closed learning loop

Persist user **overrides keyed by `todo_key`** (`deal_engine_store.py:769`) and mine *what/where reps delete or rewrite* drafts (existing `sweep_learnings` Observatory). Feed into (a) background-review skill updates and (b) the eval set (§4). One-directional today; this closes it.

### 3.8 Runaway safety + recovery

Keep MASE's breakers (`MAX_AUTO_CONTINUATIONS=25`, `MAX_RUN_COST_USD=20`, `MAX_RUN_SECONDS=1800`, watchdog, `finally` terminal-row net, `server.py:218`). Layer Hermes's **classify→one-shot flag-guarded recovery** (`turn_retry_state.py`): auth-expired → refresh per-user OAuth + retry once; SF/Apollo rate-limited → backoff/rotate; `REQUIRED_FIELD_MISSING` → surface to model ≤N times; then stop. **Never loop on a deterministic error.** On budget exhaustion, one toolless "summarize what you did and what remains" call. Log `_turn_exit_reason` every turn (WARNING when stopped with a pending tool result) — answers "why did it stop mid-task?"

### 3.9 Transport — keep Supabase-as-stream

Persist every step as ordered `chat_messages` rows (monotonic sequence) + Supabase realtime; HTTP stream is keepalives only (`server.py:3037`). Survives disconnects, free audit log, per-tool side-effect record. Add the verified footer (§3.5) as a distinct terminal row.

---

## 4. Evaluation

Hermes's **behavior-contracts-not-snapshots** philosophy — no tests that break on routine catalog/pricing/playbook updates.

**Retrieval eval (offline, fast, no LLM):**
- Golden set of (query, task_category, expected_chunk_ids) across all five categories + all doc_types, from real reps' deals.
- Metrics: Recall@8, MRR, citation-precision. CI gate: P0 hybrid (RRF) must beat the ada-002 single-vector baseline on Recall@8 by a defined margin; P2 rerank must lift MRR.
- **Relevance-floor check:** assert empty result when nothing clears `min_similarity` (the `-1.0` defect must not regress).

**ACL-leakage eval (critique major fix — new, mandatory):**
- Seed two synthetic tenants/users with disjoint corpora. For every query from tenant A under user A's identity, **assert zero chunks owned by tenant B (or by a non-visible user) ever appear** — across vector path, FTS path, and the Python fallback.
- **Fail-closed test:** a retrieval call with no resolved tenant returns empty, never project-wide.
- Run against both the app-layer filter and (with the service key removed) RLS, so each layer is independently proven.

**Task-quality eval (LLM-judge, leakage-controlled — self-grading removed):**
- **`claude-opus-4-8` judge on a held-out set**, scoring drafts per category: grounded (every claim cited), correct owner/framing, future-dated, net-new (not a re-issue), respects pulse, no fabricated people/prices. **No self-grading:** the judge never scores outputs from its own run; rubric thresholds are **calibrated against human labels** (the rep override/delete signal, §3.7) before being trusted as a gate, and a human-label sample is re-checked each release.
- **Anti-fabrication regression suite:** adversarial deals (missing fields, named-but-undossiered competitor, stale asks) — assert `NEEDS HUMAN`, not invention.
- **Side-effect truth:** assert the verified footer matches actual tool results, and that the footer says **staged/NOT-written** for the SF push (never "updated Salesforce").
- **Human signal = north star:** track "accepted as-is" vs "rewritten" vs "deleted" per category over time.

**Regression prevention:** every background-created skill is prompt-injection-scanned before re-entering context; every RPC change re-runs the golden + leakage sets; cap/dedupe behavior has a contract test.

---

## 5. Phased Build Plan

Each phase independently mergeable, feature-flagged off by default with graceful degradation, tracing reuse-vs-build.

### P0a — Safety foundations (BLOCKERS — must land first, mostly in-repo)

**Build/change:**
- **Per-run agent instances:** replace the singleton `self.agent` mutation (`server.py:738,1835`) + in-place `reinitialize_agent` (8 call sites) with a `build_agent_for_run(spec)` factory; shared `agent_manager` holds only immutable resources. Fixes cross-run prompt bleed *and* the cache anti-pattern.
- **Auth + RLS:** add an auth dependency to `POST /api/documents/upload` (`server.py:4124`) and all retrieval entry points; resolve identity server-side; re-enable Supabase RLS; stop service-key use on user-scoped reads; retrieval **fails closed** without a resolved tenant.

**Acceptance:** concurrent runs never share/swap prompt state (concurrency test); upload + retrieval reject unauthenticated calls; RLS blocks cross-tenant reads with the service key removed.

### P0b — Retrieval upgrade (the #1 ask — cross-team, on ada-002)

**Build/change:**
- Supabase migration: add `doc_type/source/title/content_hash/recency_at/tenant_id/acl_scope/metadata` to `documents`; denormalize `doc_type/section/recency_at/tenant_id/acl_scope/metadata/content_fts` to `document_chunks`; GIN on `content_fts`; confirm/upgrade ANN to HNSW. **(Cross-team — Next.js/Supabase owns DDL + RPC.)**
- `match_document_chunks_v2`: `min_similarity` floor + **required `acl_tenant_id`** + metadata pre-filter + `filter_document_ids`. **Stays on ada-002 (1536-dim) — no dimension change.**
- `custom_tools/retrieve_documents.py`: hybrid (vector+FTS) + RRF + doc-type-aware cap/dedupe; ACL passed from the run identity. Wrap (don't delete) `search_knowledge.py`.
- Collapse the duplicated embedding-model constant into one shared config.

**Reuse:** ContextVar scoping (`rag_context.py`), cap+dedupe governance (`search_knowledge.py:23-202`), `list_documents` preflight, name-prefilter intent (now in SQL).

**Acceptance:** Recall@8 + citation-precision beat the ada-002 baseline; empty-on-no-match works; **leakage eval passes**; cap/dedupe contract tests pass; existing `search_knowledge` callers unaffected behind the flag.

### P1a — Ingestion + doc-type corpus (still ada-002)

**Build/change:**
- Upgrade `POST /api/documents/upload` to accept binary files via reused Showpad extractors (`showpad_mcp_server.py:184-263`); token-aware, doc-type-dispatched chunker (§1.1); `content_hash` upsert (kills duplicate-chunk-on-reupload); stamp `tenant_id`/`acl_scope` per chunk.
- Ingest the corpus: `playbook.json` plays (one chunk each, metadata-tagged), `prompts/*.md` guides, Avoma transcripts, Showpad asset metadata+summaries (federated-discovery half of §1.3).

**Reuse:** Showpad MCP for live full-asset fetch; Avoma cache tools; embed-on-both-sides discipline.

**Acceptance:** all five `doc_type`s queryable/filterable; re-upload supersedes (no dup chunks); Showpad semantic-discovery → live-fetch round-trips; every chunk carries an ACL scope.

### P1b — Embedding upgrade (isolated, after P0/P1a are stable — critique fix)

**Build/change:**
- `embedding_model` column on both tables; `ada-002 → text-embedding-3-large` (dimensions per Open Decision); Batches-style off-hours backfill; dual-read (query embeds with the model recorded on the target corpus); flip index only at 100% coverage; verify normalization invariant before flip.

**Acceptance:** backfill complete before flip; query/corpus model match enforced; retrieval metrics ≥ ada-002 baseline post-flip; zero downtime during dual-read.

### P2 — Context + memory + cache-safety + rerank

**Build/change:**
- `build_task_context()` prologue (§2.1): frozen cached system prefix + volatile user-message block (the per-run factory from P0a makes this concurrency-safe).
- `deal_memory` + `user_card` tables (ACL-scoped); living-memory carry-forward; structured compaction template + SUMMARY_PREFIX contract (upgrade `ContextTrimMiddleware`).
- Reranker: Haiku-subagent rerank; benchmark vs Cohere.

**Reuse:** Opus 4.8 server-side compaction (beta `compact-2026-01-12`); deepagents subagents; `_pulse_of`.

**Acceptance:** `cache_read_input_tokens` > 0 across a session's turns; no cross-run prompt bleed under concurrency (re-assert P0a); rerank lifts MRR.

### P3 — The agent: skills + grounding + per-task behavior + staged write-back

**Build/change:**
- Per-task-type behavior table (§3.6); deterministic `TASK_RETRIEVAL_ROUTING`.
- Anti-fabrication: citation enforcement + verified side-effect footer (`turn_finalizer.py` analog) as a terminal `chat_messages` row; untrusted-output delimiting.
- **Staged write-back (corrected):** the agent emits a `{todo_key, opp_id, category, draft_artifact}` **proposal**; the rep confirms in the existing modal; the **server** does the idempotent-on-`todo_key` write (`docs/deal-engine-todo-push.md`). Agent never writes to SF.
- Single registry + dispatch + arg-coercion; one-shot recovery branches.

**Reuse:** `derive_todo` semantics, `todo_key` identity, the existing human-confirm modal + idempotent server push, `NEEDS HUMAN` sentinel, layered breakers.

**Acceptance:** LLM-judge rubric pass-rate per category clears threshold; anti-fabrication adversarial suite emits `NEEDS HUMAN` (zero invented people/prices); footer matches real tool results and reflects *staged, not written*; server push idempotent under retry; agent has no SF-write capability (negative test).

### P4 — Learning loop (closed)

**Build/change:**
- Background-review fork (`background_review.py` analog): nudge cadence, provenance gate, anti-self-sabotage denylist, prompt-injection scan before skill re-entry.
- Curator: deterministic prune (never delete, tar.gz backup, pinned exempt); LLM consolidation off by default.
- Mine the `todo_key` override/delete signal (§3.7) into skill updates + the eval set; use it to calibrate the judge rubric (§4).

**Reuse:** existing override store + `sweep_learnings`; Supabase audit trail as replay source.

**Acceptance:** background fork inherits the cached prefix byte-for-byte (verify `cache_read` on the fork); only `created_by:"agent"` skills are curated; user-saved skills never auto-archived; draft-accept-rate trends up over a measurement window.

---

### Reuse-vs-build summary

| Reuse as-is | Adapt | Build new |
|---|---|---|
| cap/dedupe governance, ContextVar scoping, `list_documents`, Supabase-as-stream, layered breakers, `derive_todo`/`todo_key`, the human-confirm modal + idempotent server push, `NEEDS HUMAN`, `_pulse_of`, Showpad extractors & MCP | `search_knowledge`→hybrid `retrieve_documents`, `ContextTrimMiddleware`→structured compaction, upload endpoint→binary+typed chunking+ACL, singleton-`reinitialize_agent`→per-run factory, per-run prompt→frozen cached prefix | ACL/RLS + `tenant_id` schema, `doc_type` schema + `match_document_chunks_v2`, FTS+RRF (+rerank P2), `build_task_context`, deal/user memory, skills+background-review+curator, verified side-effect footer, **staged** SF write-back proposal, eval harness incl. leakage eval |

**The two gates that block everything else:** (1) P0a safety (per-run instances + auth/RLS) — entirely in-repo, do first; (2) the cross-team Supabase DDL + `match_document_chunks_v2` RPC (P0b) — coordinate immediately or nothing downstream ships.

---

## Open decisions for the user

1. **Per-user vs per-tenant ACL policy.** The schema supports both (`tenant_id` hard boundary + `acl_scope.visibility/owner_email/allowed_roles`). Decide the actual visibility model: is a Showpad asset / Avoma transcript / uploaded doc visible org-wide, team-scoped, or owner-only? This sets the RLS policy and the leakage-eval fixtures.

2. **Embedding model + dimensionality (P1b).** Confirm `text-embedding-3-large` and pick the stored dimension: 3072 (max recall, larger index/cost) vs 1024-truncated (cheaper index, slight recall trade). Drives pgvector column size and the backfill cost. (Anthropic has no embeddings endpoint — staying OpenAI is fixed.)

3. **Reranker: build vs buy.** Haiku-subagent rerank (no new vendor, in-house, ~$1/$5) vs a hosted cross-encoder (Cohere/Voyage — better latency/quality, new vendor + data-egress review). P2 benchmarks both; the production default needs a human call.

4. **HNSW vs IVFFlat ANN index.** Recommended HNSW for recall stability, but it costs more memory/build time on the shared Supabase instance — needs Supabase-team sign-off given it's their managed resource.

5. **Background-review autonomy scope (P4).** How much should the agent self-edit its skills? Options: suggest-only (human approves every skill change) vs auto-apply background-created skills with the provenance gate + injection scan as the only guardrails. Trades velocity of self-improvement against governance on a side-effecting agent.