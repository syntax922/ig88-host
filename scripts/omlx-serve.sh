#!/bin/bash
# oMLX prod server for ig88 qwen-tier. Managed by launchd (KeepAlive).
# Continuous batching for concurrency (validated 2026-07-17: 2.16x @ N=5 vs LM Studio).
# 2026-08-15: enable the prefix cache — hot (RAM) + paged SSD tiers. Without
# these flags snapshots are captured but never retained (hot cache defaults
# to 0/disabled, no SSD dir configured): 318 stores / 0 restores observed,
# cached_tokens was 0 on every call, long prompts paid full prefill per turn.
# 2026-08-26: oMLX 0.6.3rc3. ANE/GPU hybrid prefill is opt-in PER MODEL
# (qwen35_ane_prefill_enabled in ~/.omlx/model_settings.json; enabled for
# qwen38-27b-4bit + qwen3.5-27b). There is no serve flag for it; the env below
# only persists compiled ANE programs across restarts (Apple AOT cache) so
# model load after a restart skips recompilation.
export OMLX_QWEN35_ANE_COMPILE_CACHE=1
exec /Applications/oMLX.app/Contents/MacOS/omlx-cli serve \
  --host 127.0.0.1 --port 8000 \
  --model-dir /Users/copilot/.omlx/models \
  --memory-guard aggressive \
  --max-concurrent-requests 5 \
  --hot-cache-max-size 8GB \
  --paged-ssd-cache-dir /Users/copilot/.omlx/cache \
  --paged-ssd-cache-max-size 100GB \
  --log-level info
