#!/bin/bash
# oMLX prod server for ig88 qwen-tier. Managed by launchd (KeepAlive).
# Continuous batching for concurrency (validated 2026-07-17: 2.16x @ N=5 vs LM Studio).
# 2026-08-15: enable the prefix cache — hot (RAM) + paged SSD tiers. Without
# these flags snapshots are captured but never retained (hot cache defaults
# to 0/disabled, no SSD dir configured): 318 stores / 0 restores observed,
# cached_tokens was 0 on every call, long prompts paid full prefill per turn.
exec /Applications/oMLX.app/Contents/MacOS/omlx-cli serve \
  --host 127.0.0.1 --port 8000 \
  --model-dir /Users/copilot/.omlx/models \
  --memory-guard aggressive \
  --max-concurrent-requests 5 \
  --hot-cache-max-size 8GB \
  --paged-ssd-cache-dir /Users/copilot/.omlx/cache \
  --paged-ssd-cache-max-size 100GB \
  --log-level info
