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

# `launchctl bootout` returns before the job is fully torn down, so an
# immediate `bootstrap` can land while the old job still holds the label and
# fail with "Bootstrap failed: 5: Input/output error". Under `set -e` that
# aborts the whole apply — and because this loop is ordered, it aborts with
# the FIRST label booted out but not re-bootstrapped, i.e. one service down.
# That happened on 2026-07-30: the run died on mlx-audio and left it stopped
# while every later label went untouched.
#
# Wait for the label to actually disappear, then retry the bootstrap a few
# times before giving up.
for label in "${LABELS[@]}"; do
  launchctl bootout "system/$label" >/dev/null 2>&1 || true

  # Poll until the label is really gone (max ~10s).
  for _ in $(seq 1 20); do
    launchctl print "system/$label" >/dev/null 2>&1 || break
    sleep 0.5
  done

  bootstrapped=0
  for attempt in 1 2 3 4 5; do
    if launchctl bootstrap system "$LAUNCH_DAEMONS_DIR/$label.plist" 2>/dev/null; then
      bootstrapped=1
      break
    fi
    echo "bootstrap $label failed (attempt $attempt/5), retrying..." >&2
    sleep 2
  done
  if [ "$bootstrapped" -ne 1 ]; then
    echo "FATAL: could not bootstrap $label — it is now DOWN. Investigate with:" >&2
    echo "  sudo launchctl print system/$label" >&2
    exit 1
  fi

  launchctl enable "system/$label"
  launchctl kickstart -k "system/$label"
done

"$REPO_DIR/scripts/healthcheck.sh"
