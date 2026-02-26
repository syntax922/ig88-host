#!/bin/zsh
set -euo pipefail

LOG_DIR="/Users/copilot/ig88-host/logs"
mkdir -p "$LOG_DIR"

LMS_APP="/Applications/LM Studio.app/Contents/MacOS/LM Studio"

if [ ! -x "$LMS_APP" ]; then
  echo "LM Studio app not found at $LMS_APP" >&2
  exit 1
fi

# Use the direct binary with --run-as-service instead of the flaky lms CLI
# KeepAlive in the LaunchDaemon plist handles restarts
exec "$LMS_APP" --run-as-service
