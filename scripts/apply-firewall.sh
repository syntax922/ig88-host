#!/bin/zsh
set -euo pipefail

CONF_SRC="/Users/copilot/ig88-host/firewall/pf-ig88.conf"
CONF_DST="/etc/pf.ig88.conf"

if [ "$EUID" -ne 0 ]; then
  echo "run as root: sudo /Users/copilot/ig88-host/scripts/apply-firewall.sh" >&2
  exit 1
fi

cp "$CONF_SRC" "$CONF_DST"

if ! grep -q "pf.ig88.conf" /etc/pf.conf; then
  echo "add load anchor "ig88" from "/etc/pf.ig88.conf" to /etc/pf.conf" >&2
  exit 1
fi

pfctl -f /etc/pf.conf
pfctl -e
