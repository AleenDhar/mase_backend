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
}
API_ENV = {
    "HOST": "0.0.0.0", "PORT": str(PORT),
    **_DATALAKE_AND_SNS, **_SWEEP_TUNING,
    # worker autoscaler (runs on the api): sizes mase-worker to the queue backlog
    "SWEEP_AUTOSCALE_ENABLED": "true",
    "SWEEP_AUTOSCALE_MAX": "6",
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
