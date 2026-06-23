# MASE — Full Operating Context (read before you touch prod)

> **Audience:** any human or AI coding agent working on this repo. This is the single
> "what will bite you" dump. If you are about to deploy, change env, or touch the deal
> sweep / Avoma path, **read the relevant section first.** Pair this with
> [DEPLOY_SAFETY.md](DEPLOY_SAFETY.md) (chat-404 runbook) and the repo's `AGENTS.md`,
> `CHANGELOG.md`, `replit.md`, and `.agents/memory/`.

---

## 0. Golden rules (the do-nots)

1. **Never deploy un-pushed / divergent code.** `deploy.ps1` ships your **WORKING TREE**,
   not GitHub. If your tree isn't clean and synced to `origin/main`, you will ship the
   wrong thing. **This is the #1 way prod gets broken here.** (See §2.)
2. **Never write to Salesforce.** The agent is read-only via `MCP_TOOL_DENYLIST`. Do not
   remove SF write tools from the denylist.
3. **Never deploy without explicit human approval**, each time.
4. **Plain task-def env vanishes on the next `deploy.ps1`.** If env must survive deploys,
   it goes in the `deploy.ps1` task-def template or a Secrets Manager secret (see §4).
5. **Agent prompts live in Supabase**, not `prompts/*.md` (those are deprecated seeds).
   Edit them in **Admin → Agent Control**. See `.agents/memory/prompts-source-of-truth.md`.
6. **Secrets stay out of git.** `.datalake_secrets.env` is gitignored + dockerignored.
   Never paste a key/token into a committed file, a task-def plain env, or a log line.

---

## 1. Repos & runtimes

| Thing | Where | Runs on |
|-------|-------|---------|
| **Backend / deal engine** (this repo) | GitHub `AleenDhar/mase_backend` | AWS ECS Fargate (FastAPI, `server.py`) + a sweep **worker** (`worker.py`) |
| **Frontend** | GitHub `AleenDhar/MASE` (Next.js) | Vercel (prod) + local `:3001` |
| **Datalake** (Avoma transcripts) | Supabase project `datalake` | Supabase (Postgres + PostgREST) |

Backend entrypoint is `python server.py` (uvicorn in-process). The deep agent + MCP
connectors load at boot (`/api/health` shows readiness).

---

## 2. `deploy.ps1` — how a deploy actually works (and how it bites)

`deploy.ps1` does, in order:
1. **Commits your working tree** and **pushes to `origin/main`** (`AleenDhar/mase_backend`).
2. **Sync guard:** asserts the tree is clean and `HEAD == origin/main`. (So it ships
   exactly what's on `main` after the push.)
3. Builds the image in **CodeBuild** (`mase-build`), pushes to **ECR** `mase-service`.
4. **Registers a new task-def revision** for `mase-api`, enumerating secret keys from
   Secrets Manager **`mase/app-env`**, plus the hardcoded env template in the script
   (HOST/PORT + sweep tuning + **the durable datalake/SNS/LLM env, see §4**) and the
   **`mase/datalake`** secret.
5. **Blue-green deploy:** rolls the image onto the **idle** colour
   (`mase-api-blue` / `mase-api-green`), waits for healthy targets, then **flips the ALB
   listener** to it and drains the old colour.
6. Re-registers + updates the **`mase-worker`** service with the same image.

### Why teammates break prod here
- `deploy.ps1` ships the **working tree**, *not* GitHub. If a teammate deploys with
  local un-pushed changes (or an old/diverged branch), **prod ≠ GitHub `main`**, and the
  next person who deploys from clean `main` "reverts" their change — or ships missing
  routes. The **chat-404 incident** was exactly this: a teammate's un-pushed "outlook"
  image lacked the async chat routes. Fix was redeploying `origin/main`. See
  [DEPLOY_SAFETY.md](DEPLOY_SAFETY.md).
- **Rule:** before deploying, `git fetch && git status` → clean tree, on `main`, even
  with `origin/main`. If you didn't write the diff that's in your tree, **stop**.

### Blue-green truth (don't get confused)
- Two services, `mase-api-blue` and `mase-api-green`. **Exactly one is live** (ALB
  listener weight 100); the other is idle (weight 0) and is the **instant rollback**.
- To see live colour: read the listener default-action target-group weights.
- A deploy targets the **idle** colour, then flips. If you see the "latest" task-def on
  the *idle* colour, the flip didn't stick — check the listener.

---

## 3. AWS infrastructure (account `022187637784`, region `ap-south-1`)

| Resource | Identifier |
|----------|-----------|
| ECS cluster | `mase-cluster` |
| API services (blue-green) | `mase-api-blue`, `mase-api-green` |
| Worker service | `mase-worker` |
| Task-def families | `mase-api`, `mase-worker` |
| ALB | `mase-alb-1262623499.ap-south-1.elb.amazonaws.com` |
| ALB listener | `.../listener/app/mase-alb/176c820e3f56b935/c6710f58972ca338` |
| Target groups | `mase-blue/71c71534374ec831`, `mase-green/c8b1ab1c4dff2dbf` |
| ALB idle timeout | **4000 s** (long sync requests OK at the ALB; see §6 caveat) |
| CodeBuild | `mase-build` |
| ECR repo | `mase-service` |
| Log group | `/ecs/mase-service` |
| Exec role | `mase-ecs-task-execution-role` (inline policy `datalake-secret-read` grants read on `mase/datalake`) |
| API task size | **1 vCPU / 2 GB** (small — heavy concurrent sweeps will strain it; see §6) |

### AWS CLI from a corp (Zscaler) laptop
The AWS CLI is at `C:\Program Files\Amazon\AWSCLIV2\aws.exe`. Set **both** before any call:
```powershell
$env:AWS_CA_BUNDLE = "C:\Users\<you>\.aws\corp-ca-bundle.pem"  # Zscaler MITM cert
$env:PYTHONUTF8    = "1"                                        # emoji/encoding in logs
```
**Python `urllib`/OpenSSL rejects the Zscaler cert** ("Basic Constraints… not marked
critical"). So **datalake/Avoma HTTP from a laptop must go through PowerShell**
(`Invoke-RestMethod` uses the Windows cert store) or the AWS CLI (uses `AWS_CA_BUNDLE`),
**not** Python. The container has no such problem.

---

## 4. Env & secrets — what survives a deploy, what doesn't

There are **two** secrets:
- **`mase/app-env`** — the big bundle (~52 keys). `deploy.ps1` **enumerates every key**
  and injects them as task-def `secrets`. **Do not casually rewrite this secret.**
- **`mase/datalake`** — holds **`DATALAKE_SERVICE_KEY`** only. Referenced explicitly by
  `deploy.ps1` (it is *not* in `mase/app-env`). The exec role was granted read via the
  inline policy `datalake-secret-read`.

### The durable env baked into `deploy.ps1`'s task-def template
Because `deploy.ps1` rebuilds the task def from scratch every time, **any env added
manually to a task-def revision is LOST on the next deploy.** To avoid that "corrective
revision" dance, the following are now in the `deploy.ps1` template and survive deploys:
- `DATALAKE_URL`, `SNS_ALLOWED_REGIONS`, `SNS_ALLOWED_TOPIC_ARNS`, `SNS_ALLOWED_ACCOUNT_IDS`
  (plain env) + `DATALAKE_SERVICE_KEY` (from `mase/datalake`).
- API sweep robustness: `LLM_REQUEST_TIMEOUT_S=1200`, `ANTHROPIC_MAX_RETRIES=8`,
  `DEAL_SWEEP_MAX_TRANSIENT_RETRIES=50`, `DEAL_SWEEP_MAX_TOKENS=64000`,
  `MCP_TOOL_TIMEOUT_S=600`.

**If you add new long-lived env, add it to the `deploy.ps1` template (or `mase/app-env`),
never as a one-off task-def revision** — it will silently disappear.

### `.datalake_secrets.env` (repo root, gitignored + dockerignored)
Local-only convenience file holding `SUPABASE_ACCESS_TOKEN`, `DATALAKE_REF`,
`DATALAKE_URL`, `DATALAKE_SERVICE_KEY`, `DATALAKE_DB_PASS`, `AVOMA_BRIDGE_URL`,
`AVOMA_BRIDGE_TOKEN`. Used by the backfill + ops scripts. **Never commit it.**

---

## 5. The datalake (Avoma transcript store)

**Why:** the live deal sweep was missing calls (see §6). The datalake is a searchable,
2-year store of Avoma transcripts for **tracked opps only**, so the sweep can read a
deal's **whole** call history in one fast SQL read instead of paging live Avoma.

| | |
|--|--|
| Supabase project | name `datalake`, ref `upxxvoyngfiblaypluyc`, region ap-south-1 |
| URL | `https://upxxvoyngfiblaypluyc.supabase.co` |
| Service key | in `mase/datalake` secret + `.datalake_secrets.env` (writes; bypasses RLS) |
| Publishable/anon key | frontend admin card only — `NEXT_PUBLIC_DATALAKE_URL` / `NEXT_PUBLIC_DATALAKE_KEY` |

**Tables** (`scripts/datalake_schema.sql`): `avoma_meetings` (uuid pk, subject, start_at,
duration *numeric*, state, attendees, `crm_opportunity_id`, raw), `avoma_transcripts`
(`meeting_uuid` pk, transcript_text, ts), `avoma_insights` (`meeting_uuid` pk,
ai_notes_text), `avoma_sync_days` / `avoma_sync_state` (checkpoints), `ab_test_results`
(A/B test verdicts, §6), view `avoma_call_search`. **PostgREST FK count quirk:** count
with the table's real PK column (`avoma_transcripts?select=meeting_uuid`, not `uuid`).

**Backfill** (`scripts/datalake_backfill_tracked.ps1`, PowerShell so Zscaler TLS works):
reads tracked opps from `/api/deal-engine/opportunities?slim=1`, queries Avoma by 18-char
opp id over 730 d, upserts to the datalake. Resumable via a local checkpoint file.
State as of last run: **444/444 tracked opps processed**, 353 have calls, **1,354
meetings / 884 transcripts / 883 notes**. (Schema applied via the Supabase **Management
API** `POST /v1/projects/{ref}/database/query` — run statements individually; multi-
statement batches 400.)

### Real-time sync (the webhook) — keeps it current
```
Avoma (AINOTE webhook, HMAC-signed)
  -> API Gateway  https://c8uqifdnib.execute-api.ap-south-1.amazonaws.com
  -> Lambda  avoma-sns-bridge  (token-gated; org blocks public Lambda URLs, hence API GW)
  -> SNS topic  arn:aws:sns:ap-south-1:022187637784:avoma-meeting-events
  -> backend  POST /webhook  (SNS-hardened receiver; SNS_ALLOWED_* gate)
  -> datalake_sync.sync_meeting(uuid)
```
`datalake_sync` fetches meeting + transcript + insights from Avoma and upserts to the
datalake — **but only for tracked opps** (`_is_tracked_opp()` checks deal_records in the
MASE Supabase, **fail-closed**). A non-tracked meeting logs
`[DATALAKE-SYNC] … not tracked — skip`. The Avoma webhook is API-managed
(`/v1/webhooks/`, `event_type=AINOTE`, `target_type=2`). Don't register guessed event
types — verify against dev.avoma.com docs + the live API.

---

## 6. The Avoma retrieval fix (the whole point) + the A/B test

### The bug that started it ("Avoma is terrible")
The live prefetch `_avoma_prefetch` widens recency windows `_AVOMA_WINDOWS=[90,270,540]`
and **breaks at the first window with any hits** — a **90-day recency clip**. Deals whose
recent activity is < 90 days old **never look further back** and silently lose older
calls. Proven from prod logs: **Mair Group saw 7 of 14 calls.** It also caps deep-reads at
`_AVOMA_MAX_READS=12`.

### The datalake path: `_avoma_prefetch_from_datalake(opp)`
One SQL read of the deal's **entire** history (`crm_opportunity_id=like.{opp15}*`,
ordered) → builds the **same manifest shape** as the live path, so
`_avoma_prefetch_block()` renders it to the agent **unchanged**. Selected by the sweep
when `avoma_from_datalake=True`.

**Complete-units rule (critical — do not regress):** a transcript is inlined **WHOLE or
not at all — never sliced mid-call.** A mid-cut transcript makes the agent reason over
half a conversation and conclude wrongly. So:
- Every call carries its **complete Avoma AI-notes** (a faithful *whole-call* summary).
- Verbatim **full** transcripts are allocated to the **most-recent** calls until a char
  **budget** is spent (`DEAL_SWEEP_AVOMA_DL_TRANSCRIPT_BUDGET`, default **80000**);
  per-call guard `…_MAXCHARS=48000`.
- Calls beyond the budget keep their complete notes; every call (incl. gaps) is **listed
  as a touchpoint**, so the agent can never falsely say "gone dark."
- The manifest block tells the agent this is its **authoritative, complete** Avoma
  coverage and to **synthesise from it** (don't re-run discovery).

**Why the budget is moderate, not "all transcripts":** inlining every call's full
transcript (15+) pushed a single LLM generation **past 600 s → `APITimeoutError`** (the
Anthropic client timeout). 80 KB ≈ ~8 full transcripts + complete notes for the rest =
small enough to finish, still far more complete than the 90-day-clipped live path. The
API task also now carries the worker's `LLM_REQUEST_TIMEOUT_S` / `ANTHROPIC_MAX_RETRIES`
tuning (§4) so transient timeouts are absorbed.

### The A/B test endpoint (no persist)
`POST /api/deal-engine/sweep/{opp_id}/datalake-test` — **async/detached**: spawns a
datalake-sourced sweep (`dry_run=True`, never writes deal_records / run log) and returns
`{"status":"started"}` **instantly**; the verdict is written to datalake
`ab_test_results` (poll by `opp_id`). **It is async on purpose:** a ~9-min *synchronous*
request is **severed by the corporate (Zscaler) proxy at ~544 s**, which cancels the
server coroutine (`CancelledError`) — so we never hold the connection. (The ALB itself
allows 4000 s; the proxy is the limit.) Use this to compare datalake-sourced verdicts
against the existing live-Avoma record **without touching production data.**

### Repointing the PRODUCTION sweep
The prod sweep (worker) still uses live Avoma. Once the A/B test proves datalake quality,
repoint it by passing `avoma_from_datalake=True` on the worker's `analyze_one` path — a
deliberate, separate change. Don't flip it silently.

---

## 7. Runbooks

**Chat 404 in MASE** → almost always a teammate shipped an image missing the async chat
routes (§2). Redeploy `origin/main`; verify `/api/deal-engine/chat/async` returns 400/401
(not 404). Full steps in [DEPLOY_SAFETY.md](DEPLOY_SAFETY.md).

**Env vanished after a deploy** (datalake sync no-ops, sweep mistuned) → the env was a
one-off task-def revision, not in the `deploy.ps1` template / a secret (§4). Add it to
the template and redeploy; don't hand-patch a revision.

**Sweep fails with `APITimeoutError`** → prompt too big or LLM tuning missing on that
service. Lower `DEAL_SWEEP_AVOMA_DL_TRANSCRIPT_BUDGET`, confirm `LLM_REQUEST_TIMEOUT_S` /
`ANTHROPIC_MAX_RETRIES` are set, and **don't run many heavy sweeps concurrently on the
1-vCPU/2-GB API** (2–3 max).

**Verify the webhook end-to-end** → POST a test event through the bridge URL (token
required) → expect 200 → SNS → `/webhook` → a `[DATALAKE-SYNC] synced meeting …` log for a
tracked opp (or `… not tracked — skip` for others).

**A/B test a deal** → `POST …/datalake-test` for each opp, then poll
`datalake.ab_test_results?opp_id=eq.<id>` for `status=completed` and read `result`.

---

## 8. Security posture (non-negotiable)

- **Read-only Salesforce** (`MCP_TOOL_DENYLIST`) — never enable SF writes.
- **No deploy without explicit approval**, every time.
- Secrets (`sbp_`, `sb_secret_`, service-role JWTs, Avoma token, bridge token) are
  **secrets** — never commit, echo, or put in plain task-def env. `.datalake_secrets.env`
  stays gitignored + dockerignored.
- Don't dump or rewrite the `mase/app-env` production secret.
- The Avoma `/webhook` receiver is SNS-hardened (`SNS_ALLOWED_*`); keep the allowlist tight.
