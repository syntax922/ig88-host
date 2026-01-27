#!/bin/zsh
set -euo pipefail

REPO_DIR="/Users/copilot/ig88-host"
LOG_DIR="$REPO_DIR/logs"
mkdir -p "$LOG_DIR"

if ! command -v git >/dev/null 2>&1; then
  echo "git not installed; install Xcode Command Line Tools" >&2
  exit 1
fi

cd "$REPO_DIR"
git pull --ff-only
make apply
