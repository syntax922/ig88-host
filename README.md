# ig88-host

Host-level GitOps for IG88 (LM Studio + Caddy proxy + launchd).

## Contract
See `contract/ig88.yaml` for the stable interface consumed by Kluster.

## Quickstart (local)
1) Install LM Studio and ensure `lms` is available:
   - `/Applications/LM Studio.app/Contents/MacOS/lms --help`
2) Fill placeholders:
   - `contract/ig88.yaml`
   - `caddy/Caddyfile` (allowlist IPs)
   - `firewall/pf-ig88.conf` (allowlist IPs)
3) Apply services:
   - `make apply-system`

## System settings
- Disable sleep (requires sudo):
  - `sudo /Users/copilot/ig88-host/scripts/disable-sleep.sh`
- Apply firewall rules (requires sudo):
  - Add `load anchor "ig88" from "/etc/pf.ig88.conf"` to `/etc/pf.conf`.
  - `sudo /Users/copilot/ig88-host/scripts/apply-firewall.sh`

## GitOps loop
`launchd/com.syntax922.ig88.gitops.plist` runs `scripts/gitops-sync.sh` at boot
and nightly (03:30). It requires git + repo remote.

## Logs
- `logs/lmstudio.log`
- `logs/caddy.log`
- `logs/gitops.log`
- `logs/caddy-access.log`
