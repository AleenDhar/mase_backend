# sf-report-watch

Scheduled poller that turns **new rows on a Salesforce report into VIBE project
runs**. Watches the object behind the report **APAC GTM MQL Global_V1**
(`00OP7000005v4TsMAI` — a *Contacts with MQL History* report filtered to
`MQL_History__c.MQL__c = true AND Contact.Account.Geography__c = 'APAC'`) and, for
each new MQL, POSTs to VIBE `/api/workflows/dispatch-abm` to kick a run under the
contact's owning BDR.

Same shape as `../sf-cdc-bridge`: a single zero-dependency `lambda_function.py`
on the stock `python3.12` runtime, driven by an **EventBridge schedule (rate: 5
minutes)** instead of a CDC event bus.

```
EventBridge (rate 5 min) → Lambda → Salesforce (SOQL: new MQL_History__c since watermark)
                                   → VIBE /api/workflows/dispatch-abm  (Bearer DISPATCH_SECRET)
                                   → Supabase state (cursor + dedup ledger)
```

## How "new" is detected

- **High-water mark** on Salesforce `CreatedDate` (second precision — the
  `MQL_Date_Time__c` field is rounded to 5-minute buckets so it would drop ties).
- **Dedup ledger** keyed on the `MQL_History__c` record id → every row dispatched
  **exactly once**, even across overlapping windows.
- **First run seeds the watermark to "now"** → an initial deploy does **not**
  fire the ~73 existing APAC MQLs. To backfill from a point in time, set
  `SEED_WATERMARK_ISO` before the first run.
- `MAX_DISPATCH_PER_RUN` caps dispatches per invocation; a backlog drains over
  subsequent 5-minute runs.

## Prerequisites

1. Apply the state schema (one time): `migrations/0013_sf_report_watch.sql`
   (via the Supabase Management API / `execute_sql`, like the other migrations).
2. A VIBE **example project** exists; put its UUID in `MQL_ABM_PROJECT_ID`.
3. `DISPATCH_SECRET` matches the value VIBE checks in `dispatch-abm/route.ts`.

## Environment variables

| Var | Required | Default | Notes |
|-----|----------|---------|-------|
| `SF_USERNAME` / `SF_PASSWORD` / `SF_SECURITY_TOKEN` | ✅ | — | Same creds the backend's simple_salesforce uses |
| `SF_DOMAIN` | | `login` | `login`, `test`, or a my-domain host |
| `SF_API_VERSION` | | `59.0` | |
| `SUPABASE_URL` / `SUPABASE_KEY` | ✅ | — | Service-role key; holds cursor + ledger |
| `VIBE_DISPATCH_URL` | | `https://zycus-deal.vercel.app/api/workflows/dispatch-abm` | |
| `DISPATCH_SECRET` | ✅ | — | Bearer token VIBE authenticates |
| `MQL_ABM_PROJECT_ID` | ✅ | — | Target VIBE project UUID |
| `MODEL` | | `anthropic:claude-sonnet-4-20250514` | Passed to dispatch |
| `FALLBACK_BDR_EMAIL` | | — | Used when a contact's owner isn't a VIBE user (else that row is `skipped_no_bdr`) |
| `REPORT_ID` | | `00OP7000005v4TsMAI` | State key / label |
| `GEOGRAPHY` | | `APAC` | The report's geography filter |
| `MAX_DISPATCH_PER_RUN` | | `25` | Per-invocation dispatch cap |
| `SEED_WATERMARK_ISO` | | — | First-run backfill start, e.g. `2026-07-09T00:00:00Z` |
| `DRY_RUN` | | — | `true` = log candidates, do **not** dispatch |

## Deploy (AWS CLI)

```bash
cd infra/sf-report-watch
zip function.zip lambda_function.py

# 1) Create the function (reuse the CDC bridge's execution role or any role with
#    basic Lambda logging perms — no extra AWS service perms are needed).
aws lambda create-function \
  --function-name mase-sf-report-watch \
  --runtime python3.12 --handler lambda_function.handler \
  --timeout 120 --memory-size 256 \
  --role arn:aws:iam::022187637784:role/<lambda-basic-exec-role> \
  --zip-file fileb://function.zip --region ap-south-1

# 2) Set env (use a file:// JSON — never inline JSON from PowerShell; see AGENTS.md)
aws lambda update-function-configuration \
  --function-name mase-sf-report-watch \
  --environment file://env.json --region ap-south-1

# 3) Schedule it every 5 minutes and let EventBridge invoke it
aws events put-rule --name mase-sf-report-watch-5min \
  --schedule-expression "rate(5 minutes)" --region ap-south-1
aws lambda add-permission --function-name mase-sf-report-watch \
  --statement-id evb-5min --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn arn:aws:events:ap-south-1:022187637784:rule/mase-sf-report-watch-5min \
  --region ap-south-1
aws events put-targets --rule mase-sf-report-watch-5min \
  --targets "Id"="1","Arn"="arn:aws:lambda:ap-south-1:022187637784:function:mase-sf-report-watch" \
  --region ap-south-1
```

`env.json` is `{ "Variables": { "SF_USERNAME": "...", ... } }`. Store real secrets
in Secrets Manager and reference them, matching the rest of the stack.

## Recommended rollout

1. Apply `0013_sf_report_watch.sql`.
2. Deploy with **`DRY_RUN=true`**. Let one or two 5-min ticks run; check
   CloudWatch logs for `[poll] … candidates=N` and `[dry_run] would dispatch …`
   and confirm the ledger cursor seeded to "now".
3. Manually create one real MQL_History__c (or wait for a live one), confirm it's
   picked up as a candidate.
4. Flip `DRY_RUN` off. Verify a VIBE chat is created under the example project and
   a `sf_report_watch_log` row lands with `status=dispatched` + a `chat_id`.

## Local test

```bash
python -m py_compile lambda_function.py         # syntax check
DRY_RUN=true SF_USERNAME=... SF_PASSWORD=... SF_SECURITY_TOKEN=... \
SUPABASE_URL=... SUPABASE_KEY=... DISPATCH_SECRET=... MQL_ABM_PROJECT_ID=... \
python -c "import lambda_function as l; print(l.handler({}, None))"
```
