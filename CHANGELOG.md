# CHANGELOG — MASE backend (`mase_backend`)

> **Agents & teammates: read this file after every `git pull`.** It is the running
> log of behaviour-changing decisions and conventions. Newest first. When you make a
> change that affects how the system behaves, where data lives, or how another agent
> should work, **add an entry here** (and, for a durable rule, a note under
> `.agents/memory/` with a line in `.agents/memory/MEMORY.md`).

Conventions for an entry: `## YYYY-MM-DD — <short title>`, then **What / Why /
How to work with it going forward**. Keep it tight; link code paths and docs.

---

## 2026-06-19 — Chat agent: tool-using (shared knowledge base + Todo Runner delegation)

**What.** `/api/deal-engine/chat` (the RevOps chat over the book) was a tool-less one-shot
OpenAI completion. It is now a **tool-using deep agent** (`deal_engine_chat_agent.py`,
`build_chat_agent`) that:
- **shares the MASE knowledge base** — it has `search_knowledge` routed to the isolated MASE
  namespace (`MASE_KNOWLEDGE_PROJECT_ID`), the same store the sweep + todo-runner use, and
- **can delegate to the Todo Runner** — a `run_todo(task, account?, contact?, opportunity_id?)`
  tool runs the Todo Runner as a SEPARATE deep agent (its own Supabase prompt
  `mase_todo_runner` + Salesforce/Avoma/Showpad/knowledge tools, MASE rag namespace, own
  chat_id) and returns the draft (or a `NEEDS HUMAN:` line). Mirrors the sweep's
  independent-agent pattern (`create_deep_agent` + `_oa._build_model` + `_oa._final_text`).
- **uses the admin-editable prompt** — the base prompt now comes from Supabase `ID_CHAT`
  (fallback `_DEAL_ENGINE_CHAT_SYSTEM`); the book + a fixed `_CHAT_CAPABILITIES` block
  (describing exactly what the Todo Runner can/can't do) are appended by code. Previously the
  `/chat/prompt` editor wrote a key the chat ignored — now it actually drives the chat.

**Why / how to work with it.** Fulfils "chat shares the KB + can call the Todo Runner + its
prompt is editable in Admin." The agent path is wrapped in try/except and **falls back to the
original one-shot completion** if the agent stack/tools aren't available, so the chat can't
hard-break. Tunables: `DEAL_CHAT_RECURSION_LIMIT` (40), `DEAL_CHAT_TIMEOUT_S` (300),
`CHAT_TODO_RECURSION_LIMIT` (60), `CHAT_TODO_TIMEOUT_S` (300). Edit the chat prompt at Admin →
Agent Control → **Chat Agent** (`/api/deal-engine/chat/prompt`, key `mase_chat_agent`).

## 2026-06-19 — Knowledge uploads: large files via S3 (no size limit)

**What.** Knowledge-base file uploads no longer go through the Vercel proxy as a
base64 JSON body (capped at ~4.5 MB on Vercel serverless). The browser now uploads
the raw file **directly to S3** via a presigned PUT, then registers it with the
backend, which pulls the object from S3 and extracts the text. Effectively no
file-size limit (S3 single-PUT supports up to 5 GB).
- **New endpoint** `POST /api/deal-engine/knowledge/presign` (`server.py`
  `mase_knowledge_presign`): returns `{url, key}` — a presigned PUT to bucket
  `mase-knowledge-uploads-022187637784` under `uploads/<uuid>/<safe-name>`. Admin-gated
  at the proxy (path starts with `knowledge`).
- **`POST /api/deal-engine/knowledge`** now also accepts `s3_key` (+ `filename`):
  downloads the object (`_s3_download`), extracts via the new `_extract_text_from_bytes`
  (refactored out of `_extract_text_from_file` so the inline-base64 and S3 paths share
  it), then **deletes the temp object** (`_s3_delete`). The old `file_b64` inline path
  still works for small/legacy callers.
- **Extraction caps raised + env-configurable** (`server.py`): `MASE_MAX_UPLOAD_BYTES`
  (default **200 MB**), `MASE_MAX_EXTRACT_CHARS` (4 M), `MASE_MAX_PDF_PAGES` (5000),
  `MASE_MAX_FILE_B64` (~210 MB). Bucket via `MASE_KNOWLEDGE_S3_BUCKET`, region via
  `AWS_REGION`/`AWS_DEFAULT_REGION` (fallback `ap-south-1`), presign TTL
  `MASE_PRESIGN_EXPIRY_S` (900s).
- **Dependency:** added `boto3` to `requirements.txt`.

**Infra (prod, ap-south-1, acct 022187637784).** New private bucket
`mase-knowledge-uploads-022187637784` with CORS (PUT/GET, any origin — the presigned
URL is the gate) and a 1-day lifecycle expiry on `uploads/`. New inline policy
`mase-knowledge-s3` on `mase-ecs-task-role` granting `s3:PutObject/GetObject/DeleteObject`
on that bucket only (additive — does not touch existing SQS/secrets perms).

**Why / how to work with it.** The Vercel proxy body cap made multi-MB sales decks
impossible to upload; routing the bytes around the proxy (browser → S3 → backend) was
the only way to truly remove the limit (Supabase Storage has its own limits, and the
ALB is HTTP-only so the HTTPS frontend can't post to it directly — mixed content).
Frontend: `app/(dashboard)/admin/page.tsx` `DocumentsSection` now PUTs the raw `File`
to S3 (no client-side base64 read) and removed the 15 MB cap.

## 2026-06-18 — Reliability batch: MCP tool timeout, pooled store HTTP + retries, graceful drain

**What.** Three reliability hardening changes (from the enterprise-readiness audit;
adversarially reviewed before ship):
- **MCP per-tool timeout** (`server.py` `_wrap_mcp_tool`): every async MCP tool call is
  bounded by `asyncio.wait_for` (`MCP_TOOL_TIMEOUT_S`, default **300s** for API; the
  worker sets **600s** in `deploy.ps1`). A hung subprocess returns `{status:failed}` so
  the agent recovers instead of pinning a run to the ~660s watchdog. The default sits
  above a worst-case legit Avoma call (~180s) so it won't cut valid reads.
- **Pooled store HTTP + idempotency-safe retries** (`analysis_store.py`,
  `deal_engine_store.py`): one shared `httpx.Client` (keep-alive) + `_request()` with
  bounded jittered retries. Connection errors retry on any verb; read/write-timeout /
  5xx / 429 retry **only** for idempotent verbs (`select`/`upsert`/`patch`/`delete`);
  `insert` never retries on a maybe-landed error → **no double-writes**. Tune with
  `STORE_HTTP_RETRIES`.
- **Graceful shutdown drain** (`server.py` `shutdown_event` + `deploy.ps1`
  `stopTimeout:120`): on SIGTERM, give in-flight runs a grace window
  (`SHUTDOWN_DRAIN_GRACE_S=15`) then **cancel** stragglers so each run's OWN finally /
  cancel handler writes its single terminal row — chats no longer hang on "Thinking…"
  after a deploy. We do NOT inject a terminal row (that would double-write / violate the
  one-terminal-row contract).

**Why / how to work with it.** Targets reliability ("all systems working"), not scaling
or security. No behaviour change intended. The drain depends on graceful SIGTERM +
`stopTimeout`; a hard SIGKILL (OOM) still needs the cross-instance run reconciler (P1.1
follow-up in `docs/enterprise-readiness.md`). Adversarial review caught the original drain
design double-writing terminal rows — fixed to cancel-based.

## 2026-06-18 — Enterprise-readiness audit + roadmap (docs/enterprise-readiness.md)

**What.** Added `docs/enterprise-readiness.md`: a prioritized P0/P1/P2 roadmap (from a
multi-agent code audit, 53 grounded findings) for scaling to ~1000 concurrent users.

**Why / how to work with it.** MASE is NOT yet ready for 1000 concurrent users. Two
failure classes dominate: (1) process-local state breaks across multiple ECS tasks
(duplicate runs, sequence collisions, duplicate crons), and (2) no cluster-wide LLM
governor → the fleet stampedes Anthropic OTPM 400k. Plus fail-open auth + anon SELECT on
`deal_records`. **Before adding features at scale, work the P0 list.** Keep the doc updated
as items land.

## 2026-06-18 — Agent onboarding: AGENTS.md + CLAUDE.md + auto-surfaced changelog on pull

**What.** Added `AGENTS.md` (the operating guide coding agents auto-load) and a short
`CLAUDE.md` pointer at the repo root, with copy-paste prompts (session catch-up,
post-pull "what changed", pre-commit wrap-up). Enhanced `scripts/post-merge.sh` to print
the CHANGELOG.md lines added by a `git pull`.

**Why / how to work with it.** So every agent (and teammate) understands the changes that
come with each push/commit. **Start every session by reading `AGENTS.md` then
`CHANGELOG.md`.** Install the hook once: `cp scripts/post-merge.sh .git/hooks/post-merge
&& chmod +x .git/hooks/post-merge` — then each pull prints what changed. When you make a
behaviour change, append a CHANGELOG entry (the wrap-up prompt in AGENTS.md reminds you).

## 2026-06-18 — System prompts now live in Supabase (Supabase is the SOURCE OF TRUTH)

**What.** The two MASE agent system prompts are now stored in, and served from,
Supabase — not the local `prompts/*.md` files:

| Agent | Supabase row (`public.jarvis_settings.id`) | Edit it from |
| --- | --- | --- |
| Deal Intelligence Engine **sweep** | `mase_deal_sweep` | Admin → Agent Control → **Deal Sweep** (or `POST /api/deal-engine/sweep/prompt`) |
| **Todo Runner** ("Run with AI" Tactical Fulfillment) | `mase_todo_runner` | Admin → Agent Control → **Todo Runner** (or `POST /api/deal-engine/todo-runner/prompt`) |

Both rows are seeded with the current prompt text and read at runtime via
`agent_prompt_store.get_prompt(<id>)`. The chat agent key `mase_chat_agent` already
worked this way.

**Why.** So the prompts can be edited live by admins without a code change/redeploy,
and so there is ONE authoritative copy. The deal-sweep agent re-resolves the prompt
on a 15s TTL and rebuilds when its fingerprint changes (`deal_engine_sweep._get_agent`);
the todo-runner fetches it per run from the frontend (`components/agent/AgentRun.tsx`).

**How to work with it going forward.**
- ✅ To change an agent's behaviour, **edit the Supabase prompt** (via the Admin UI or
  the endpoint above). Supabase ALWAYS wins.
- ⚠️ Do **NOT** edit `prompts/deal_engine_sweep_system_prompt.md` or
  `prompts/todo_runner_system_prompt.md` to change live behaviour. They are now only
  the **cold-start SEED / fallback** (used only if the Supabase row is missing) and
  carry a `⚠️ DEPRECATED` banner at the top. That banner is a leading HTML comment
  stripped at load (`agent_prompt_store.strip_leading_banner`) so it never enters the
  prompt. If you intentionally improve the seed, mirror the change into Supabase too.
- The Admin editor's **"Reset to default"** clears the Supabase override and falls
  back to the seed — that's the only path back to the on-disk version.
- See `.agents/memory/prompts-source-of-truth.md`.

## 2026-06-18 — Admin → Execution shows two separate run feeds

**What.** The Admin → Execution tab now lists **Deal Sweep runs** (worker status +
`/api/deal-engine/trigger-logs`) and **Todo Runner runs** separately. The latter is a
new endpoint `GET /api/deal-engine/todo-runner/runs` that identifies "Run with AI"
runs by their seed user-message in the shared `chats`/`chat_messages` tables (no
schema change) and derives each run's status (draft_ready / needs_human / error /
running). Admin-gated at the Next.js proxy.

## 2026-06-18 — Agent doc upload hardened

**What.** `POST /api/documents/upload` (Admin → Knowledge) accepts PDF/DOCX (`file_b64`
+ `filename`) and `doc_type`; extraction runs off the event loop with a 120s timeout
and is bounded (size/pages/chars). Endpoint is no longer in the public allowlist.
