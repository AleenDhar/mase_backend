#!/usr/bin/env bash
# =============================================================================
# Update the MCP connector config that every CI build bakes into the image.
#
# The real mcp_config.json is GITIGNORED — it is not in the repo. The single source
# of truth is the `mase/mcp-config` Secrets Manager secret, which the pipeline fetches
# at build time. So changing the config is NOT a code push: edit mcp_config.json, run
# THIS to push it to the secret, then deploy.
#
# This validates the config the SAME way CI does (salesforce + avoma must be enabled),
# so a broken config can never even reach the secret — let alone ship.
#
# Usage:   ./scripts/update_mcp_config.sh [path/to/mcp_config.json]   (default: ./mcp_config.json)
# Needs:   AWS creds with secretsmanager:PutSecretValue on mase/mcp-config.
# Zscaler: if you're behind a TLS-inspecting proxy, export AWS_CA_BUNDLE first.
# =============================================================================
set -euo pipefail

REGION="ap-south-1"
SECRET_ID="mase/mcp-config"
CFG="${1:-mcp_config.json}"

if [[ ! -f "$CFG" ]]; then echo "ERROR: $CFG not found (pass the path as arg 1)"; exit 1; fi

# Guard: identical check to the CI build — refuse to push a config that would
# crash-loop the sweep worker ("MCP tools did not load in time").
python3 - "$CFG" <<'PY'
import json, sys
cfg = sys.argv[1]
try:
    servers = (json.load(open(cfg)).get("mcp_servers") or {})
except Exception as e:
    sys.exit(f"FATAL: {cfg} is not valid JSON: {e}")
bad = [s for s in ("salesforce", "avoma") if not (servers.get(s) or {}).get("enabled")]
if bad:
    sys.exit(f"FATAL: {cfg} is missing/disabled required MCP server(s) {bad} — "
             "refusing to push a config that would break the sweep worker.")
print(f"validated: {sum(1 for s in servers.values() if s.get('enabled'))} enabled servers incl salesforce + avoma")
PY

echo "pushing $CFG -> Secrets Manager ($SECRET_ID) ..."
aws secretsmanager put-secret-value --secret-id "$SECRET_ID" \
  --secret-string "file://$CFG" --region "$REGION" \
  --query "VersionId" --output text >/dev/null
echo "OK - secret updated (new version)."
echo
echo "NEXT: deploy so the new config is baked into the image:"
echo "    git commit --allow-empty -m 'deploy: pick up updated mcp_config' && git push origin HEAD:main"
echo "  or: GitHub -> Actions -> deploy -> Run workflow"
