#!/bin/zsh
set -euo pipefail

REPO_DIR="/Users/copilot/ig88-host"
LAUNCH_DAEMONS_DIR="/Library/LaunchDaemons"

if [ "$EUID" -ne 0 ]; then
  echo "run as root: sudo /Users/copilot/ig88-host/scripts/apply-system.sh" >&2
  exit 1
fi

"$REPO_DIR/scripts/install-caddy.sh"

mkdir -p "$LAUNCH_DAEMONS_DIR"
cp "$REPO_DIR/launchd/com.syntax922.ig88.lmstudio.plist" "$LAUNCH_DAEMONS_DIR/"
cp "$REPO_DIR/launchd/com.syntax922.ig88.caddy.plist" "$LAUNCH_DAEMONS_DIR/"
cp "$REPO_DIR/launchd/com.syntax922.ig88.gitops.plist" "$LAUNCH_DAEMONS_DIR/"

chown root:wheel "$LAUNCH_DAEMONS_DIR/com.syntax922.ig88.lmstudio.plist" \
  "$LAUNCH_DAEMONS_DIR/com.syntax922.ig88.caddy.plist" \
  "$LAUNCH_DAEMONS_DIR/com.syntax922.ig88.gitops.plist"
chmod 644 "$LAUNCH_DAEMONS_DIR/com.syntax922.ig88.lmstudio.plist" \
  "$LAUNCH_DAEMONS_DIR/com.syntax922.ig88.caddy.plist" \
  "$LAUNCH_DAEMONS_DIR/com.syntax922.ig88.gitops.plist"

for label in com.syntax922.ig88.lmstudio com.syntax922.ig88.caddy com.syntax922.ig88.gitops; do
  launchctl bootout "system/$label" >/dev/null 2>&1 || true
  launchctl bootstrap system "$LAUNCH_DAEMONS_DIR/$label.plist"
  launchctl enable "system/$label"
  launchctl kickstart -k "system/$label"
done

"$REPO_DIR/scripts/healthcheck.sh"
