#!/bin/zsh
set -euo pipefail

CADDY_BIN="/Users/copilot/ig88-host/bin/caddy"
if [ -x "$CADDY_BIN" ]; then
  exit 0
fi

TMP_DIR="$(mktemp -d)"
trap "rm -rf $TMP_DIR" EXIT

curl -fsSL "https://caddyserver.com/api/download?os=darwin&arch=arm64" -o "$TMP_DIR/caddy"
install -m 0755 "$TMP_DIR/caddy" "$CADDY_BIN"
