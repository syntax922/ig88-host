#!/bin/zsh
set -euo pipefail

LOG_DIR="/Users/copilot/ig88-host/logs"
mkdir -p "$LOG_DIR"

LMS_BIN=""
if command -v lms >/dev/null 2>&1; then
  LMS_BIN="$(command -v lms)"
elif [ -x "/Applications/LM Studio.app/Contents/Resources/app/.webpack/lms" ]; then
  LMS_BIN="/Applications/LM Studio.app/Contents/Resources/app/.webpack/lms"
else
  echo "lms not found; install LM Studio and ensure lms is available" >&2
  exit 1
fi

exec "$LMS_BIN" server start --port 1234 >>"$LOG_DIR/lmstudio.log" 2>&1
