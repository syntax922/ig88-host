#!/bin/zsh
set -euo pipefail

REPO_DIR="/Users/copilot/ig88-host"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
UID_NUM="$(id -u)"

"$REPO_DIR/scripts/install-caddy.sh"

mkdir -p "$LAUNCH_AGENTS_DIR"
cp "$REPO_DIR/launchd/com.syntax922.ig88.lmstudio.plist" "$LAUNCH_AGENTS_DIR/"
cp "$REPO_DIR/launchd/com.syntax922.ig88.caddy.plist" "$LAUNCH_AGENTS_DIR/"
cp "$REPO_DIR/launchd/com.syntax922.ig88.gitops.plist" "$LAUNCH_AGENTS_DIR/"

for label in com.syntax922.ig88.lmstudio com.syntax922.ig88.caddy com.syntax922.ig88.gitops; do
  launchctl bootout "gui/$UID_NUM/$label" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$UID_NUM" "$LAUNCH_AGENTS_DIR/$label.plist"
  launchctl enable "gui/$UID_NUM/$label"
  launchctl kickstart -k "gui/$UID_NUM/$label"
done

"$REPO_DIR/scripts/healthcheck.sh"
