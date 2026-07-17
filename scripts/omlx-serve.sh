#!/bin/bash
# oMLX prod server for ig88 qwen-tier. Managed by launchd (KeepAlive).
# Continuous batching for concurrency (validated 2026-07-17: 2.16x @ N=5 vs LM Studio).
exec /Applications/oMLX.app/Contents/MacOS/omlx-cli serve \
  --host 127.0.0.1 --port 8000 \
  --model-dir /Users/copilot/.omlx/models \
  --memory-guard aggressive \
  --max-concurrent-requests 5 \
  --log-level info
