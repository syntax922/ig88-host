#!/bin/bash
# oMLX prod server for ig88 qwen-tier. Managed by launchd (KeepAlive).
# Continuous batching for concurrency (validated 2026-07-17: 2.16x @ N=5 vs LM Studio).
# 2026-08-15: enable the prefix cache — hot (RAM) + paged SSD tiers. Without
# these flags snapshots are captured but never retained (hot cache defaults
# to 0/disabled, no SSD dir configured): 318 stores / 0 restores observed,
# cached_tokens was 0 on every call, long prompts paid full prefill per turn.
# 2026-08-26: oMLX 0.6.3rc3. ANE/GPU hybrid prefill is opt-in PER MODEL
# (qwen35_ane_prefill_enabled in ~/.omlx/model_settings.json), not a serve
# flag. Measured on M3 Ultra: +19-22% prefill (332->395 tok/s qwen38-27b-4bit,
# 341->413 qwen3.5-27b at ~7k tokens) BUT the engine pool records the ANE
# load-time peak (38.25GB for a 19GB model; real settled footprint ~22GB) as
# the model's size, so the qwen3.5-27b + qwen38-27b-4bit + qwen38-27b-4bit-mtp
# working set no longer fits the 81-83GB admission target and every 7k
# prefill evicts a neighbour (20-30s reload). Left DISABLED until the ledger
# is fixed upstream. The env below only persists compiled ANE programs
# (Apple AOT cache); harmless while ANE is off.
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
