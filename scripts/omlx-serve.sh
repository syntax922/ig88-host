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
# 2026-08-30: hot tier 8GB -> 2GB, SSD tier 100GB -> 300GB. Flash-Next books
# ~70GB; with the 8GB hot cache full, usage hit ~78GB against a ~79-81GB sizing
# target and oMLX throttled every prefill chunk 4096 -> 512..1024 (400-790 tok/s
# cold, vs ~3.2k solo in Kluster #2088). Residency is carried by the paged SSD
# cache (2-block restore ~2.4s from SSD); the internal SSD is fast with ~430GiB
# free. Kluster #2092.
# 2026-09-07: prefill chunk sizing — lift the band clamp, not the ceiling.
# Every 8-10k-token prompt logs "Prefill throttled ... chunk 4096 -> 1024"
# (70-130x/hour). Read against oMLX 0.6.4 scheduler._adaptive_chunk_size the
# 1024 is NOT the headroom gate, it is the secondary "band clamp":
#   scheduler ceiling  = min(static 96-4, metal 92, dynamic) - hot-cache
#                        reservation 2         = 90.00 GiB (log: "ceiling")
#   sizing target      = 90.00 x 0.925 (aggressive headroom safety)
#                                              = 83.25 GiB (log: "sizing target")
#   headroom at 73.3 GiB usage                 =  9.94 GiB
#   n_fit = 9.94 GiB / per_token (2.6-7.5 MB, x1.3 safety already applied)
#                                              = 1360 .. 3945 tokens  <- varying
#   observed output                            = 1024 always          <- clamp
# The clamp fires when usage >= soft_base x prefill_safe_zone_ratio
#   = (90 x 0.925) x 0.80 = 66.6 GiB, and a 65 GiB-weights model sits at
#   73.2-76.9 GiB between chunks, so it fires on every chunk regardless of
#   headroom. Raising the ratio to 0.95 moves the clamp to 79.1 GiB — above
#   every sizing-time usage read today (max 76.86) — so n = n_fit and the
#   predicted-peak gate (current + 1.3 x measured <= 83.25) does the sizing.
# Expected: 4096 -> ~2048 at the median per_token (4.9 MB), 1344..3936 across
#   the observed range. NOT a full 4096: that needs 19.2 GiB of headroom, i.e.
#   usage <= 64.0 GiB, and the weights alone are ~65 GiB. No knob on a 96 GiB
#   box reaches it (custom tier lowers safety to 0.90 and metal 92 still binds;
#   wired limit 92 GiB is already over the enforcer's 91.2 GiB jetsam floor;
#   guard off / prefill_priority=speed run into the abort cap or the Metal wall).
# Margins kept: sizing target 83.25 < abort cap 85.5 < enforcer hard 87.4 <
#   Metal wall 92.0. A 2x misprediction on a 2048 chunk peaks ~88.6: request
#   aborts, Metal does not panic. Why 0.95 and not 0.90 (74.9 GiB: still clamps
#   on the 76.86 reads) or 0.99 (82.4 GiB: clamp effectively deleted).
# The key has no serve flag and no admin setter, so it is converged in
#   ~/.omlx/settings.json here, before exec. Rollback = set 0.80 + kickstart
#   (reverting this file alone leaves 0.95 persisted). Runbook: README.md
#   "oMLX prefill chunk sizing".
OMLX_PREFILL_SAFE_ZONE_RATIO="${OMLX_PREFILL_SAFE_ZONE_RATIO:-0.95}"
/usr/bin/python3 /Users/copilot/ig88-host/scripts/omlx-settings-patch.py \
  /Users/copilot/.omlx/settings.json memory.prefill_safe_zone_ratio \
  "$OMLX_PREFILL_SAFE_ZONE_RATIO" \
  || echo "omlx-serve: settings patch failed (non-fatal), serving with existing settings.json" >&2
exec /Applications/oMLX.app/Contents/MacOS/omlx-cli serve \
  --host 127.0.0.1 --port 8000 \
  --model-dir /Users/copilot/.omlx/models \
  --memory-guard aggressive \
  --max-concurrent-requests 5 \
  --hot-cache-max-size 2GB \
  --paged-ssd-cache-dir /Users/copilot/.omlx/cache \
  --paged-ssd-cache-max-size 300GB \
  --log-level info
