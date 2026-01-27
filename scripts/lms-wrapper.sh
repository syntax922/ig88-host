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

while true; do
  "$LMS_BIN" server start --port 1234 --log-level info >>"$LOG_DIR/lmstudio.log" 2>&1 || true
  sleep 5
  while curl -sf http://127.0.0.1:1234/v1/models >/dev/null 2>&1; do
    sleep 30
  done
  sleep 5
done
