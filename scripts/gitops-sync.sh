#!/bin/bash
set -euo pipefail

REPO_DIR="/Users/copilot/ig88-host"
LOG_DIR="$REPO_DIR/logs"
mkdir -p "$LOG_DIR"

if ! command -v git >/dev/null 2>&1; then
  echo "git not installed; install Xcode Command Line Tools" >&2
  exit 1
fi

cd "$REPO_DIR"

PRE_SHA="$(git rev-parse HEAD)"
git pull --ff-only
POST_SHA="$(git rev-parse HEAD)"

TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
if [ "$PRE_SHA" != "$POST_SHA" ]; then
  echo "$TS gitops-sync: pulled $PRE_SHA -> $POST_SHA"
  echo "  changes need manual apply: run sudo $REPO_DIR/scripts/apply-system.sh"
  git log --oneline "$PRE_SHA..$POST_SHA"
else
  echo "$TS gitops-sync: no changes ($POST_SHA)"
fi
