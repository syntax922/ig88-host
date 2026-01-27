#!/bin/zsh
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
  echo "run as root: sudo /Users/copilot/ig88-host/scripts/disable-sleep.sh" >&2
  exit 1
fi

pmset -a sleep 0
pmset -a disksleep 0
pmset -a displaysleep 30
pmset -a standby 0
pmset -a autopoweroff 0
