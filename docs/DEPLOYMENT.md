# Deployment — read this before you deploy anything

**TL;DR: you do not "run a deploy." You push to `main`. GitHub Actions does the rest.**

```bash
git push origin main
```

That's it. The pipeline builds your commit, blue-green deploys it to AWS, runs a
full endpoint QA gate, and **auto-rolls-back** if anything is wrong. There is
nothing to run on your laptop.

---

## ⛔ Do NOT run `deploy.ps1` or `deploy.mac.ps1`

Those scripts are **deprecated and now refuse to run** (they exit with a message
pointing here). They are dangerous because they:

1. **Ship your local working tree**, not `origin/main`. Whatever uncommitted junk
   is on your disk goes straight to production — no review, no git history.
2. **Bypass the env source-of-truth.** They each carried their own copy of the
   task-definition env. One of them went stale and **dropped the datalake + SNS
   env on deploy**, which took the Avoma engine and `/selfcheck` down in prod.
3. **Bypass the QA gate.** No route check, no rollback. If your build 404s the
   chat endpoint, it just… ships.

If CI is genuinely down and you must break-glass deploy by hand, you can override:

```powershell
$env:ALLOW_MANUAL_DEPLOY=1 ; .\deploy.ps1      # Windows
$env:ALLOW_MANUAL_DEPLOY=1 ; ./deploy.mac.ps1  # macOS
```

…but that should be a last resort, and you should re-run the real pipeline (push
an empty commit or use the "Run workflow" button) as soon as CI is back.

---

## How the pipeline works (`.github/workflows/deploy.yml`)

Triggered on **push to `main`** (docs-only changes are ignored) or manually via the
Actions tab → **deploy** → **Run workflow**.

1. **Auth** — assumes an AWS role via GitHub **OIDC** (`mase-github-deploy-role`).
   There are **no AWS keys stored in GitHub**. Nothing to rotate, nothing to leak.
2. **Build & push** — `docker build` from the checked-out commit → ECR, tag
   `<sha7>-<timestamp>`.
3. **Render task definitions** — `.github/deploy/render_taskdef.py` is the **single
   source of truth** for the task-def env. It enumerates the `mase/app-env` secret
   keys live and bakes in the durable config (datalake URL, SNS allow-list, sweep
   tuning, the worker autoscaler). **The env can no longer be silently dropped** —
   it lives in git and is rendered in CI from `main`.
4. **Blue-green** — deploys the new image to the **idle** colour, waits for it to be
   healthy + in service, then **flips the ALB listener** to it. The old colour keeps
   serving until the flip, then drains to zero. Zero-downtime.
5. **Roll the worker** — `mase-worker` rolls to the new image. You do **not** set its
   count; the **autoscaler** (running on the API) sizes it to the queue backlog.
6. **QA gate** — `scripts/qa_endpoints.sh` runs against the freshly-flipped build
   (see below). If it fails, the step fails and the **ALB rolls back** to the old
   colour automatically.

You can watch it live in the repo's **Actions** tab.

---

## The QA gate — why a teammate can't silently break an endpoint

`scripts/qa_endpoints.sh` runs after every deploy and is what stops "I shipped a
feature and broke someone else's endpoint." It does four things:

1. **Route-set diff.** It reads the **live `/openapi.json`** (every route the app
   actually registered) and diffs it against the committed baseline
   `scripts/expected_routes.txt`. **If any route in the baseline is gone**
   (deleted, renamed, or it failed to register because the module crashed on
   import) → **FAIL → rollback**. This auto-covers all ~129 routes and never goes
   stale, because it's generated from the app itself.
2. **Crash probe.** Every non-parameterised route is hit with a safe request
   (a `GET`, or a `POST {}` the handler validate-rejects). A `5xx` → **FAIL**.
3. **Chat-404 guard.** Explicitly asserts the deal-engine chat endpoint is not 404
   (a recurring outage).
4. **Env self-check.** `GET /api/deal-engine/selfcheck` must report `ok:true`
   (datalake / SNS / LLM env all present).

### If you intentionally add or remove a route

The baseline is a **superset gate**: adding routes is always fine. If you
**intentionally remove or rename** a route, regenerate the baseline and commit it:

```bash
export BASE_URL=http://mase-alb-1262623499.ap-south-1.elb.amazonaws.com
export TOKEN=<DEAL_ENGINE_TOKEN>          # = API_AUTH_TOKEN
./scripts/qa_endpoints.sh --write-baseline
git add scripts/expected_routes.txt && git commit -m "qa: update route baseline"
```

You can also run the QA by hand against prod anytime:

```bash
BASE_URL=http://mase-alb-1262623499.ap-south-1.elb.amazonaws.com \
TOKEN=<token> ./scripts/qa_endpoints.sh
```

---

## Changing environment variables

**Never** hand-edit a task definition in the AWS console, and **never** put env in
the laptop scripts again.

- **Non-secret config** (URLs, flags, tuning) → edit the dicts in
  `.github/deploy/render_taskdef.py` (`_DATALAKE_AND_SNS`, `_SWEEP_TUNING`,
  `API_ENV`, `WORKER_ENV`) and push. It's reviewed, in git, and applied on deploy.
- **Secrets** (tokens, keys) → add the key to the `mase/app-env` Secrets Manager
  secret. `render_taskdef.py` enumerates the secret keys automatically, so a new
  key is picked up on the next deploy with no code change. Secret **values** never
  touch git or CI logs — only key names are read to build `valueFrom` references.

---

## Changing the MCP connector config (`mcp_config.json`)

The real `mcp_config.json` is **gitignored** — it is **not** in the repo. The single
source of truth is the **`mase/mcp-config` Secrets Manager secret**, which the pipeline
fetches and bakes into every image at build time (with a guard that fails the build
unless `salesforce` + `avoma` are enabled). So **a code push alone does NOT change the
MCP config** — you must update the secret, then deploy.

To add a connector, rotate a token, enable/disable a server, etc.:

1. Edit your local `mcp_config.json`.
2. Push it to the secret (this validates salesforce+avoma first, so a broken config
   can't reach the secret):
   ```bash
   ./scripts/update_mcp_config.sh            # default ./mcp_config.json, or pass a path
   ```
3. Deploy so the new config is baked in — push any commit to `main`, or
   **Actions → deploy → Run workflow**.

Needs AWS creds with `secretsmanager:PutSecretValue` on `mase/mcp-config`. Behind a
TLS-inspecting proxy (Zscaler), `export AWS_CA_BUNDLE=<corp-ca.pem>` first.

---

## Copy-paste this to your Claude (teammate onboarding)

> This repo (`mase_backend`) deploys **only** through GitHub Actions. To deploy, I
> commit and `git push origin main` — the workflow `.github/workflows/deploy.yml`
> builds the commit, blue-green deploys to AWS ECS, runs `scripts/qa_endpoints.sh`
> as a gate, and auto-rolls-back on failure. **Do NOT run `deploy.ps1` or
> `deploy.mac.ps1`** — they are deprecated, refuse to run, ship the local working
> tree instead of git, and have caused prod env-drop outages. To change task-def
> env, edit `.github/deploy/render_taskdef.py` (non-secret) or add a key to the
> `mase/app-env` secret (secret values) — never hand-edit task defs and never
> reintroduce env into the deploy scripts. If I add/remove an API route, I
> regenerate `scripts/expected_routes.txt` with
> `./scripts/qa_endpoints.sh --write-baseline` and commit it. Never write to
> Salesforce (`MCP_TOOL_DENYLIST`). Never deploy without explicit approval.
