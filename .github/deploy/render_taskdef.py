#!/usr/bin/env python3
"""Render the mase-api + mase-worker ECS task definitions for the GitHub Actions
deploy. THIS FILE IS THE SINGLE SOURCE OF TRUTH for the durable task-def env — it
replaces the env blocks that used to live (and drift) in deploy.ps1 / deploy.mac.ps1.
Because it's in git and rendered in CI from origin/main, the env can never be
silently dropped by a stale laptop script again.

Run in CI:  python .github/deploy/render_taskdef.py <IMAGE_URI> <out_api.json> <out_worker.json>

The app-env SECRET KEYS are enumerated live from Secrets Manager (so a new key added
to mase/app-env is picked up automatically) — the runner's IAM user needs
secretsmanager:GetSecretValue on mase/app-env. Secret VALUES never touch CI; only the
key names are read, to build `secrets[].valueFrom` ARN references.
"""
import json
import os
import subprocess
import sys

ACCOUNT = "022187637784"
REGION = "ap-south-1"
APP_ENV_SECRET = "mase/app-env"
APP_ENV_ARN = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:mase/app-env-Adtn25"
DATALAKE_SECRET_ARN = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:mase/datalake-kcMH0p"
EXEC_ROLE = f"arn:aws:iam::{ACCOUNT}:role/mase-ecs-task-execution-role"
TASK_ROLE = f"arn:aws:iam::{ACCOUNT}:role/mase-ecs-task-role"
LOG_GROUP = "/ecs/mase-service"
CPU = "1024"
MEMORY = "2048"
PORT = 5000

# ---- DURABLE ENV (the single source of truth) -------------------------------
# Shared by api + worker; values are non-secret config. Secrets come from the
# secrets[] block below (app-env enumeration + the datalake key).
_DATALAKE_AND_SNS = {
    "DATALAKE_URL": "https://upxxvoyngfiblaypluyc.supabase.co",
    "SNS_ALLOWED_REGIONS": REGION,
    "SNS_ALLOWED_TOPIC_ARNS": f"arn:aws:sns:{REGION}:{ACCOUNT}:avoma-meeting-events",
    "SNS_ALLOWED_ACCOUNT_IDS": ACCOUNT,
    "DEAL_SWEEP_AVOMA_FROM_DATALAKE": "true",
}
_SWEEP_TUNING = {
    "DEAL_SWEEP_PARALLEL_READERS": "true",
    "LLM_REQUEST_TIMEOUT_S": "1200",
    "ANTHROPIC_MAX_RETRIES": "8",
    "DEAL_SWEEP_MAX_TRANSIENT_RETRIES": "50",
    "DEAL_SWEEP_MAX_TOKENS": "64000",
    "MCP_TOOL_TIMEOUT_S": "600",
    # Sweep/analysis model — Sonnet 4.5 (reverted from Opus 4.8: Opus ran ~5x the
    # cost, ~$4.83/sweep, and exhausted the Anthropic credit balance). Frontier-guarded
    # in deal_engine_sweep (mini/haiku refused); Anthropic-only (OpenAI hangs on the
    # MCP tool schemas here).
    "DEAL_ENGINE_SWEEP_MODEL": "anthropic:claude-sonnet-5",
    # AI deal-scorer (deal_engine_ai_scoring): RE-ENABLED 2026-07-09 (user-directed) under
    # OMNIVISION GOVERNANCE. The two headline scores (Win Position + Deal Momentum) are now
    # produced by the LLM applying the LOCKED Scoring Version Studio engines (win + mom),
    # exactly as the 24-Hour Summary is governed by the locked `sum` engine — the Studio is
    # the single source of truth. deal_engine_ai_scoring._prompt() reads the locked win/mom
    # instructions; edit + lock a new version in /omnivision → the scorer adopts it on the
    # next sweep, no code deploy. PURE STUDIO, NO deterministic floors on top (user-directed).
    # The deterministic engine (deal_engine_scoring.py) remains the FALLBACK only — it scores
    # a deal if the AI call fails or a loss is a hard fact, so a deal is never left unscored.
    "DEAL_ENGINE_AI_SCORING": "true",
    "DEAL_ENGINE_SCORING_MODEL": "anthropic:claude-sonnet-5",
    # MANUAL-ONLY TEST PAUSE (2026-07-09, user-directed): ALL automated sweeping is OFF —
    # Salesforce-CDC triggers are dropped at enqueue, whole-book/scheduled runs are refused,
    # and the mase-worker fleet IDLES (never drains the queue). Only an explicit per-deal
    # MANUAL trigger runs, and it runs SYNCHRONOUSLY on the api (no worker). Shared by api +
    # worker via _SWEEP_TUNING. Set "false" (or delete this line) + re-enable the autoscaler
    # to resume automated sweeping.
    "DEAL_SWEEP_MANUAL_ONLY": "true",
}
API_ENV = {
    "HOST": "0.0.0.0", "PORT": str(PORT),
    **_DATALAKE_AND_SNS, **_SWEEP_TUNING,
    # worker autoscaler (runs on the api): sizes mase-worker to the queue backlog.
    # DISABLED 2026-07-09 alongside DEAL_SWEEP_MANUAL_ONLY — with automated sweeping paused
    # the worker must stay at 0 and NOT be auto-scaled back up. Re-enable ("true") when
    # resuming automated sweeping.
    "SWEEP_AUTOSCALE_ENABLED": "false",
    "SWEEP_AUTOSCALE_MAX": "6",
    # KILL the nightly scheduled discovery + reconcile AI sweeps — the
    # `scheduled_discovery` / `scheduled_reconcile` burn. This gates sub-job D of
    # `_run_nightly_sf_pull` (server.py:6099), the ONLY code that produces those two
    # run sources, so it stops them no matter how the nightly is invoked (its in-process
    # scheduler is default-off and the /cron/nightly-sf-pull endpoint is gated, yet the
    # job still fired ~00:00 UTC on 2026-07-04 and -05, ~50 paid sweeps/night). Manual
    # discovery via POST /api/deal-engine/discover-new is UNAFFECTED (it doesn't read
    # this flag). Remove this line to re-enable nightly deal-engine discovery.
    "DEAL_ENGINE_DISCOVERY_ENABLED": "false",
}
WORKER_ENV = {
    **_DATALAKE_AND_SNS, **_SWEEP_TUNING,
    "DEAL_SWEEP_CONCURRENCY": "8",
    "MCP_SERVER_ALLOWLIST": "salesforce,avoma",
    "DEAL_SWEEP_TIMEOUT_S": "2400",
}


def _app_env_secret_keys() -> list:
    """Enumerate the KEY NAMES in mase/app-env (values never leave CI)."""
    raw = subprocess.check_output([
        "aws", "secretsmanager", "get-secret-value", "--secret-id", APP_ENV_SECRET,
        "--region", REGION, "--query", "SecretString", "--output", "text"])
    keys = list(json.loads(raw).keys())
    # DEAL_SWEEP_PARALLEL_READERS is set as plain env above; ECS forbids the same
    # key in both environment[] and secrets[].
    return [k for k in keys if k != "DEAL_SWEEP_PARALLEL_READERS"]


def _secrets_block() -> list:
    out = [{"name": k, "valueFrom": f"{APP_ENV_ARN}:{k}::"} for k in _app_env_secret_keys()]
    out.append({"name": "DATALAKE_SERVICE_KEY", "valueFrom": DATALAKE_SECRET_ARN})
    return out


def _td(family, name, image, env, command=None, with_health=True):
    container = {
        "name": name, "image": image, "essential": True, "stopTimeout": 120,
        "environment": [{"name": k, "value": v} for k, v in env.items()],
        "secrets": _secrets_block(),
        "logConfiguration": {"logDriver": "awslogs", "options": {
            "awslogs-group": LOG_GROUP, "awslogs-region": REGION,
            "awslogs-stream-prefix": name.split("-")[-1]}},
    }
    if command:
        container["command"] = command
    if with_health:
        container["portMappings"] = [{"containerPort": PORT, "protocol": "tcp"}]
        container["healthCheck"] = {
            "command": ["CMD-SHELL", f"curl -fsS http://127.0.0.1:{PORT}/api/health || exit 1"],
            "interval": 30, "timeout": 5, "retries": 3, "startPeriod": 60}
    return {
        "family": family, "networkMode": "awsvpc", "requiresCompatibilities": ["FARGATE"],
        "cpu": CPU, "memory": MEMORY, "executionRoleArn": EXEC_ROLE, "taskRoleArn": TASK_ROLE,
        "containerDefinitions": [container],
    }


def main():
    image, out_api, out_worker = sys.argv[1], sys.argv[2], sys.argv[3]
    api = _td("mase-api", "mase-api", image, API_ENV)
    worker = _td("mase-worker", "mase-worker", image, WORKER_ENV,
                 command=["python", "worker.py"], with_health=False)
    with open(out_api, "w") as f:
        json.dump(api, f, indent=2)
    with open(out_worker, "w") as f:
        json.dump(worker, f, indent=2)
    print(f"rendered api ({len(api['containerDefinitions'][0]['environment'])} env, "
          f"{len(api['containerDefinitions'][0]['secrets'])} secrets) + worker, image={image}")


if __name__ == "__main__":
    main()
