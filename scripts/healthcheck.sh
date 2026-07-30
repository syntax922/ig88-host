#!/bin/zsh
set -euo pipefail

# Checks the live serving chain: Caddy :11434 -> qwen-param-proxy :11435 ->
# oMLX :8000. Runs at the end of apply-system.sh, so a failure here fails the
# apply.
#
# The LM Studio probe (`curl -sf http://127.0.0.1:1234/v1/models`) was removed
# 2026-07-30 along with the daemon. Leaving it would have failed every apply
# under `set -e` once the daemon stopped being installed.

# oMLX — the engine of record.
curl -sf http://127.0.0.1:8000/v1/models >/dev/null

# Param proxy — in-process health, independent of the upstream.
curl -sf http://127.0.0.1:11435/healthz >/dev/null

# Caddy front door, through the full chain.
curl -sf http://127.0.0.1:11434/v1/models >/dev/null
