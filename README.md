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
- `logs/param-proxy.log`
- `logs/cache-exporter.log`

## Cache & per-caller telemetry

Two additive Prometheus surfaces, both reached through the existing Caddy
`:11434` listener (same IP allowlist as inference), so cluster Prometheus
scrapes them with no new network path:

| Scrape URL | Source | What it measures |
|---|---|---|
| `http://10.20.0.26:11434/cache-metrics` | `scripts/lmstudio-cache-exporter.py` (127.0.0.1:11436) | LM Studio prompt-cache hit rate |
| `http://10.20.0.26:11434/metrics` | `scripts/lmstudio-param-proxy.py` in-process | Requests + upstream latency, per prompt family |

### Metrics

**Cache exporter** — tails the LM Studio server log
(`/Users/copilot/.lmstudio/server-logs/YYYY-MM/YYYY-MM-DD.N.log`) and parses
`Prompt cache restore:` lines:

- `lmstudio_cache_requests_total{hit="cold|partial"}` — cache restore events.
  `cold` = `cached_tokens==0` (nothing reused); `partial` = some prefix reused.
- `lmstudio_cache_cached_tokens_total` / `lmstudio_cache_uncached_tokens_total`
  — cumulative tokens served from cache vs. recomputed. Cache efficiency over a
  window ≈ `rate(cached) / (rate(cached) + rate(uncached))`.
- `lmstudio_cache_lifetime_efficiency` — LM Studio's own lifetime figure
  (last seen). Not emitted until the first cache event after (re)start.
- `lmstudio_cache_exporter_malformed_lines_total` — parser health.

**Param-proxy** — `paramproxy_requests_total{family=...}` and the summary
`paramproxy_upstream_seconds_sum|count{family=...}`. `family` is the first 12
hex of `sha256(system_message_content)`, or `nosys` when the first message
isn't a system role. Each caller ships a distinct static system prompt, so a
family hashes ~1:1 to a caller (cardinality ~20; families beyond 64 bucket into
`family="overflow"`). Metrics collection is fail-open — a fault in it never
alters proxied bytes.

**Identifying a caller from a family hash:** hash that caller's known system
prompt the same way, e.g.
`python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.read().encode()).hexdigest()[:12])' < prompt.txt`
(pass the exact system-message string, no trailing newline the caller doesn't
send). Match against the `family` label. Keep a small hash→caller lookup as
callers are onboarded.

### Deploy (operator)

`make apply-system` reconciles everything, but it `kickstart -k`s **every**
label — including `lmstudio` and `param-proxy` — so it restarts LM Studio (model
reload, ~tens of seconds) and drops in-flight completions. Prefer the targeted
path below, which brings up the exporter and the Caddy route with zero inference
disruption, and defers the one unavoidable restart (param-proxy) to a quiet
window. Run on ig88 as `copilot`.

1. Pull the merged branch:
   `cd /Users/copilot/ig88-host && git pull`
2. Install + start **only** the exporter daemon (LaunchDaemons needs root):
   ```
   sudo cp launchd/com.syntax922.ig88.cache-exporter.plist /Library/LaunchDaemons/
   sudo chown root:wheel /Library/LaunchDaemons/com.syntax922.ig88.cache-exporter.plist
   sudo chmod 644 /Library/LaunchDaemons/com.syntax922.ig88.cache-exporter.plist
   sudo launchctl bootstrap system /Library/LaunchDaemons/com.syntax922.ig88.cache-exporter.plist
   sudo launchctl enable system/com.syntax922.ig88.cache-exporter
   sudo launchctl kickstart -k system/com.syntax922.ig88.cache-exporter
   ```
3. Graceful Caddy reload — zero-downtime, in-flight requests preserved (does
   **not** restart Caddy), publishes the `/cache-metrics` route:
   `/Users/copilot/ig88-host/bin/caddy reload --config /Users/copilot/ig88-host/caddy/Caddyfile --adapter caddyfile`
4. Verify the exporter and cache route now (no restart needed):
   - `curl -s http://127.0.0.1:11436/healthz` → `{"status":"ok",...}`
   - `curl -s http://127.0.0.1:11434/cache-metrics | head` → `lmstudio_cache_*`
5. **Param-proxy `/metrics` requires restarting param-proxy** to load the new
   code, which drops in-flight completions. Do this in an idle window only:
   `sudo launchctl kickstart -k system/com.syntax922.ig88.param-proxy`
   then verify: `curl -s http://127.0.0.1:11434/metrics | head` → `paramproxy_*`
   and `make status` shows the `cache-exporter` label.

> **Exporter counters reset on restart.** It attaches at end-of-log on
> startup (no history replay), so a restart zeroes the `_total` counters —
> expected Prometheus counter-reset semantics; `rate()` handles it.
