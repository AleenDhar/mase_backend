"""Import FIRST (before dryrun_fleet / anything using daily_summary.common).

Why: daily_summary.common.load_secret() shells out to `aws secretsmanager get-secret-value`
on every fresh process. Through Zscaler that call takes minutes, and it happens at MODULE
IMPORT time in dryrun_fleet.py — so any harness that spawns per-deal subprocesses (six_live
-> qa_live) pays it once per process, serially, inside the poll loop.

load_secret() short-circuits to reading os.environ when SF_USERNAME, SF_PASSWORD and
SUPABASE_URL are all present. So: hydrate os.environ once from a cached copy of the secret.
Child processes inherit it, so the whole tree pays zero AWS calls.

Cache file (.mase_app_env.json) is gitignored; refresh it with:
  aws secretsmanager get-secret-value --secret-id mase/app-env --region ap-south-1 \
      --query SecretString --output text > .mase_app_env.json
"""
import json, os, sys

_REQUIRED = ("SF_USERNAME", "SF_PASSWORD", "SUPABASE_URL")
_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".mase_app_env.json")


def hydrate(verbose: bool = False) -> bool:
    """Load the cached secret into os.environ. Returns True if env is now satisfied."""
    if all(os.environ.get(k) for k in _REQUIRED):
        if verbose:
            print("[boot_env] env already satisfied — no AWS call", file=sys.stderr)
        return True
    if not os.path.exists(_PATH):
        if verbose:
            print(f"[boot_env] no cache at {_PATH} — load_secret() will shell to AWS (slow)",
                  file=sys.stderr)
        return False
    try:
        data = json.load(open(_PATH, encoding="utf-8"))
    except Exception as e:  # corrupt/partial cache — fall back to the AWS path
        if verbose:
            print(f"[boot_env] cache unreadable ({e}) — falling back to AWS", file=sys.stderr)
        return False
    if not isinstance(data, dict):
        return False
    n = 0
    for k, v in data.items():
        if v is not None and not os.environ.get(k):
            os.environ[k] = str(v)
            n += 1
    ok = all(os.environ.get(k) for k in _REQUIRED)
    if verbose:
        print(f"[boot_env] hydrated {n} keys from cache; required-keys-present={ok}",
              file=sys.stderr)
    return ok


hydrate(verbose=os.environ.get("BOOT_ENV_VERBOSE") == "1")
