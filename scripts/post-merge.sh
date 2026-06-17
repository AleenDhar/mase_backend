#!/bin/bash
set -e

echo "[post-merge] Installing Python dependencies..."
pip install -r requirements.txt --quiet

# Surface behaviour changes pulled in, so humans AND coding agents immediately notice
# what changed. Prints the CHANGELOG.md lines added by this pull. (Git sets ORIG_HEAD
# to the pre-merge commit.) Install this hook with: cp scripts/post-merge.sh
# .git/hooks/post-merge && chmod +x .git/hooks/post-merge
if git rev-parse --verify -q ORIG_HEAD >/dev/null; then
  CL_DIFF=$(git diff --unified=0 ORIG_HEAD HEAD -- CHANGELOG.md 2>/dev/null \
    | grep -E '^\+' | grep -vE '^\+\+\+' | sed 's/^+//')
  if [ -n "$CL_DIFF" ]; then
    echo ""
    echo "📋 [post-merge] CHANGELOG.md updated in this pull — read before you work:"
    echo "------------------------------------------------------------"
    echo "$CL_DIFF"
    echo "------------------------------------------------------------"
    echo "Agents: read AGENTS.md + these CHANGELOG entries before changing anything."
    echo ""
  fi
fi

echo "[post-merge] Done."
