"""Cron entrypoint for the DB backup (GitHub Actions). Pulls creds from mase/app-env via the
AWS CLI (works on the Linux runner — no corporate proxy), sets them in the env, then runs the
mirror. Env must be set BEFORE importing db_backup (it reads creds at import time).

Requires these keys present in mase/app-env:
  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY  (main — usually already there)
  SUPABASE_ACCESS_TOKEN                     (Supabase Management token, for schema introspection)
  BACKUP_URL, BACKUP_SERVICE_KEY            (the mase-backup project)

Usage: python run_backup_cron.py [incremental|full]
"""
import os, sys, json, subprocess

raw = subprocess.check_output(
    ["aws", "secretsmanager", "get-secret-value", "--secret-id", "mase/app-env",
     "--region", os.getenv("AWS_REGION", "ap-south-1"), "--query", "SecretString", "--output", "text"],
    text=True)
secret = json.loads(raw)
NEEDED = ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_ACCESS_TOKEN",
          "BACKUP_URL", "BACKUP_SERVICE_KEY", "BACKUP_REF")
missing = []
for k in NEEDED:
    v = secret.get(k)
    if v:
        os.environ[k] = v
    elif k != "BACKUP_REF":
        missing.append(k)
if missing:
    print(f"[db-backup] MISSING keys in mase/app-env: {', '.join(missing)} — add them, then re-run.")
    sys.exit(1)

os.environ.setdefault("DS_TLS_VERIFY", "1")   # Linux runner: verify TLS (no interception)
import db_backup  # noqa: E402 — env must be populated first

mode = sys.argv[1] if len(sys.argv) > 1 else "incremental"
summary = db_backup.run(mode)
if summary and summary.get("errors"):
    print(f"[db-backup] completed with {summary['errors']} table error(s) — see log above.")
