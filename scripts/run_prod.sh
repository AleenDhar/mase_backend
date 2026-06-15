#!/usr/bin/env bash
# Production entrypoint (vm deployment). Runs TWO processes in one container:
#
#   1. worker.py  — drains the durable sweep_queue (crash-safe deal-engine sweep)
#                   in a respawn loop, so if it ever exits it comes right back.
#   2. server.py  — the FastAPI web app (foreground via exec, so it owns the
#                   container's signals and the platform's health checks see it).
#
# The worker runs as a background child; the web server is the foreground process.
# If the web server dies the container restarts (vm), relaunching both. The queue
# is the source of truth, so any opp the worker was mid-flight on is simply
# reclaimed and retried after a restart — nothing is lost or double-charged.
set -uo pipefail

echo "[run_prod] starting sweep worker (respawn loop) + web server"

(
  while true; do
    python3 worker.py
    code=$?
    echo "[run_prod] worker exited (code ${code}); restarting in 5s" >&2
    sleep 5
  done
) &

exec python3 server.py
