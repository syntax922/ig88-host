#!/bin/zsh
set -euo pipefail

REPO_DIR="/Users/copilot/ig88-host"
LAUNCH_DAEMONS_DIR="/Library/LaunchDaemons"
# RETIRED 2026-07-30: com.syntax922.ig88.lmstudio and .cache-exporter.
# LM Studio serves nothing since oMLX took the LLMs (2026-07-17) and the
# embeddings (c6f6ba74); the cache-exporter only tails LM Studio's server log.
# Their plists stay in launchd/ so rollback is re-adding them here.
LABELS=(com.syntax922.ig88.mlx-audio com.syntax922.ig88.caddy com.syntax922.ig88.gitops com.syntax922.ig88.param-proxy com.syntax922.ig88.iogpu-wired-limit)

if [ "$EUID" -ne 0 ]; then
  echo "run as root: sudo /Users/copilot/ig88-host/scripts/apply-system.sh" >&2
  exit 1
fi

"$REPO_DIR/scripts/install-caddy.sh"

mkdir -p "$LAUNCH_DAEMONS_DIR"
for label in "${LABELS[@]}"; do
  cp "$REPO_DIR/launchd/$label.plist" "$LAUNCH_DAEMONS_DIR/"
  chown root:wheel "$LAUNCH_DAEMONS_DIR/$label.plist"
  chmod 644 "$LAUNCH_DAEMONS_DIR/$label.plist"
done

for label in "${LABELS[@]}"; do
  launchctl bootout "system/$label" >/dev/null 2>&1 || true
  launchctl bootstrap system "$LAUNCH_DAEMONS_DIR/$label.plist"
  launchctl enable "system/$label"
  launchctl kickstart -k "system/$label"
done

"$REPO_DIR/scripts/healthcheck.sh"
