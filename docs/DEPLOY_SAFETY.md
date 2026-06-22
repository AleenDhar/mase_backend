# MASE backend deploy safety — read before deploying (humans + AI agents)

**Why this exists:** the MASE chat endpoint has gone `404` in production more than
once after a teammate deployed. Every time, the cause was one of the **two failure
modes** below — not a bug in the code on GitHub. This doc is the standing context
for any coding agent or person who deploys `mase_backend`. Follow it exactly.

---

## The single most important fact

> **`deploy.ps1` ships your LOCAL WORKING TREE, not GitHub.**
> It builds an image from whatever files are on your disk right now and tags it
> with your local `HEAD`. "The code on GitHub is fine" is **irrelevant** — GitHub
> is not what gets deployed. Your laptop is.

So if your working tree differs from `origin/main` in any way, you deploy that
difference to production.

---

## Failure mode #1 — deploying a tree that isn't `origin/main`

The chat 404 happened because a deployed image was built from a local tree that
**did not match `origin/main`**: uncommitted edits, local commits never pushed, a
stale/old branch, or a bad merge that dropped the chat router. The deploy "worked"
(it was green) but shipped code missing the `/api/deal-engine/chat/*` routes.

**Rules (non-negotiable):**
1. **Before any deploy**, the tree MUST be clean AND exactly `origin/main`:
   ```sh
   git fetch origin
   git status --porcelain        # must print NOTHING
   git rev-parse HEAD            # must equal:
   git rev-parse origin/main
   ```
   If `HEAD != origin/main` or the tree is dirty → **STOP. Do not deploy.**
   Either push your commits and let them land on `main`, or
   `git reset --hard origin/main`, then deploy.
2. **Always pull the latest `deploy.ps1` first.** It contains a *sync guard*
   (step 0b) that enforces rule 1 automatically. A teammate running an **old copy
   of `deploy.ps1` has no guard** — so an outdated script is itself a risk.
3. **Never pass `-AllowDirty`** unless a human owner has explicitly authorized a
   one-off hotfix and knows the tree is intentionally ahead of `main`.
4. Prefer: **everyone deploys from the same place** (ideally CI from `origin/main`),
   not from individual laptops. Laptop deploys are how drift reaches prod.

---

## Failure mode #2 — the health gate is too shallow to catch a broken chat route

`deploy.ps1` flips ALB traffic to the new color as soon as its target group is
**"healthy"**, and the container health check is **`curl /api/health` only**
(`deploy.ps1` ~line 293). But:

> **`/api/health` returns `200` even when `/api/deal-engine/chat/async` is `404`.**

`/api/health` is a *liveness* probe (process is up, agent initialized). It says
**nothing** about whether the chat routes are registered. A broken-chat image
passes the gate and gets flipped live. **There is currently no functional smoke
test of the chat endpoint before the flip.**

**Rule:** after every deploy (and ideally wired into `deploy.ps1` *before* the
flip), run the smoke test below. **A `404` on a chat route = broken image →
roll back immediately.**

### Endpoint contract (how to read the codes)

| Endpoint | Healthy | Broken |
|---|---|---|
| `GET  /api/health` | `200` | non-200 |
| `GET  /api/deal-engine/opportunities?slim=1` | `401` (route exists, auth-gated) | **`404` = route missing** |
| `POST /api/deal-engine/chat/async` | `401` (route exists, auth-gated) | **`404` = route missing** |
| `GET  /api/deal-engine/chat/prompt` | `401` | **`404`** |
| `POST /api/chat` | `200` | non-200 |

`401` is **GOOD** here — it means the route is registered and just needs auth
(the frontend supplies it). `404` is the failure signal. `502/503` = the color
isn't serving (mid-flip / unhealthy).

### Post-deploy smoke test (run this every time)

```powershell
$base = "http://mase-alb-1262623499.ap-south-1.elb.amazonaws.com"
function Probe($m,$p,$b){ try {
  $a=@{Uri="$base$p";Method=$m;TimeoutSec=25;UseBasicParsing=$true}
  if($b){$a.Body=$b;$a.ContentType="application/json"}
  "{0,-5}{1,-42}-> {2}" -f $m,$p,[int](Invoke-WebRequest @a).StatusCode
} catch { $s=$null; if($_.Exception.Response){$s=[int]$_.Exception.Response.StatusCode}
  "{0,-5}{1,-42}-> {2}" -f $m,$p,($(if($s){$s}else{"ERR"})) } }
Probe "GET"  "/api/health" $null                              # expect 200
Probe "GET"  "/api/deal-engine/opportunities?slim=1" $null    # expect 401 (NOT 404)
Probe "POST" "/api/deal-engine/chat/async" '{"message":"ping"}' # expect 401 (NOT 404)
Probe "GET"  "/api/deal-engine/chat/prompt" $null             # expect 401 (NOT 404)
```
If any chat row shows `404` → the live image is broken. Roll back (below), then
fix the tree (failure mode #1) and redeploy.

---

## What's actually live + rollback

```powershell
$AWS="C:\Program Files\Amazon\AWSCLIV2\aws.exe"; $env:AWS_CA_BUNDLE="C:\Users\<you>\.aws\corp-ca-bundle.pem"
# which color serves traffic (weight 100 = live):
& $AWS elbv2 describe-listeners --listener-arn <listener-arn> --region ap-south-1 `
  --query "Listeners[0].DefaultActions[0].ForwardConfig.TargetGroups" --output json
# task def + image tag per color (image tag = git HEAD + build time):
& $AWS ecs describe-services --cluster mase-cluster --services mase-api-blue mase-api-green `
  --region ap-south-1 --query "services[].{name:serviceName,td:taskDefinition,running:runningCount}" --output json
& $AWS ecs describe-task-definition --task-definition mase-api:<N> --region ap-south-1 `
  --query "taskDefinition.containerDefinitions[0].image" --output text
```

**Rollback = flip the listener weights back to the last-good color** (both colors
keep running after a deploy, so the previous image is still warm). Set the good
color's target group weight to 100 and the bad one to 0 via
`aws elbv2 modify-listener --default-actions ...` (same mechanism `deploy.ps1`
uses to flip). This is instant and needs no rebuild.

Resource IDs (region `ap-south-1`, account `022187637784`):
- Listener: `.../listener/app/mase-alb/176c820e3f56b935/c6710f58972ca338`
- TG blue: `.../targetgroup/mase-blue/71c71534374ec831`
- TG green: `.../targetgroup/mase-green/c8b1ab1c4dff2dbf`
- Cluster `mase-cluster`; services `mase-api-blue` / `mase-api-green`; log group `/ecs/mase-service`.

---

## RUNBOOK: "the chat in MASE is 404" (or "Couldn't load the book / Unexpected token '<'")

This is the most common incident. **Do NOT reflexively redeploy the backend — most
of the time the backend is fine and a redeploy makes it worse.** Diagnose first.

### How the chat actually works
- Browser → MASE frontend (Vercel) same-origin route `POST /api/deal-engine/chat/async`
  → Next proxy `app/api/deal-engine/[[...path]]/route.ts` → backend ALB
  `POST /api/deal-engine/chat/async`.
- The proxy attaches the shared Bearer token and **gates chat as admin-only**
  (`callerIsAdmin`, against `ADMIN_EMAILS` in `lib/engine/helpers`). So:
  - A **non-admin** hitting chat gets **`403 {"error":"Admin only."}`** — that is
    **expected, not a bug.** If a user "can't use chat," first check they're an admin.
- `"Unexpected token '<', "<html>..." is not valid JSON"` ≠ a missing route. It means
  a fetch got an **HTML error page** (an ALB `503`/`502` or a `404` HTML) and the
  client tried to `JSON.parse` it. It almost always means **the backend was briefly
  down / mid-deploy**, not that the chat code is gone.

### Step 1 — probe the BACKEND directly (decides everything)
```powershell
$base="http://mase-alb-1262623499.ap-south-1.elb.amazonaws.com"
# (see the smoke-test helper above) — check these two:
#   GET  /api/health
#   POST /api/deal-engine/chat/async   body {"message":"ping"}
```
Read the chat status code and act:

| Backend says | Meaning | Fix |
|---|---|---|
| **`401`** | Route **exists**, just needs auth (the proxy adds it). **Backend is HEALTHY.** | The 404 is **frontend or a transient flip window**. Do **NOT** redeploy backend. Go to Step 2. |
| **`404`** | The live image is **missing the chat routes** — someone deployed a broken/divergent local tree. | **Roll back the listener** to the last-good color (see "rollback" above). Then get the correct code onto `origin/main` and redeploy. |
| **`503` / `502` (HTML)** | ALB has **no healthy targets** — mid-deploy churn or a crashed image. | Wait ~30–60s and re-probe; tasks may be cycling. If it persists, a deploy is stuck/crashed → check target health and **scale up / flip to the last-good color**. |

### Step 2 — if the backend is `401` (healthy), it's the FRONTEND or transient
1. **Hard-refresh** MASE and retry — if a blue-green flip just happened, the 404/`<html>`
   error was a few-second window and is already gone.
2. Confirm the user is an **admin** (non-admins get `403` on chat by design).
3. Check the **Vercel** deploy: did a frontend push break the proxy route
   `app/api/deal-engine/[[...path]]/route.ts`, or is the Vercel env
   **`DEAL_ENGINE_API_BASE`** (must point at the AWS ALB, not the old Replit URL) or
   **`DEAL_ENGINE_TOKEN`** missing? A missing env makes the proxy return `500`, a stale
   `DEAL_ENGINE_API_BASE` makes it `404`/error.
4. Grab the failing request from the browser **Network tab** (exact URL + status +
   response body) — that distinguishes a Vercel `404` (route/page) from an upstream
   status the proxy passed through.

**Rule of thumb:** backend chat = `401` → the problem is NOT the backend; redeploying
it wastes time and risks a fresh outage.

---

## TL;DR checklist for the deploying agent

- [ ] `git fetch origin` → tree clean AND `HEAD == origin/main`. Else STOP.
- [ ] Using the **latest** `deploy.ps1` (has the sync guard). No `-AllowDirty`.
- [ ] Deploy. Wait for the idle color to go healthy + the listener flip.
- [ ] **Run the smoke test.** Chat routes must be `401`, never `404`.
- [ ] If any chat route is `404` → roll back the listener immediately, fix the
      tree, redeploy. Do **not** leave it live because `/api/health` is green.
