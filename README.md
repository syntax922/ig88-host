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
| `http://10.20.0.26:11434/proxy-metrics` | `scripts/qwen-param-proxy.py` in-process (127.0.0.1:11435) | Requests + upstream latency, per prompt family |

Both underlying listeners are localhost-only (pf allows only :11434 on the LAN),
reached via dedicated bypass-`handle` routes in the Caddyfile that reuse the
inference IP allowlist. The `/cache-metrics` route strips its prefix (the
exporter serves generic `/metrics`); `/proxy-metrics` is passed through
unstripped and intercepted in-process by the param-proxy, so inference paths are
never touched.

### Metrics

**Cache exporter** — tails the LM Studio server log
(`/Users/copilot/.lmstudio/server-logs/YYYY-MM/YYYY-MM-DD.N.log`) and parses
`Prompt cache restore:` lines. These are `DEBUG`-level lines that only appear
when LM Studio's `verbose: true` logging is on (currently enabled) — if verbose
is turned off, the cache series go silent (the exporter stays healthy and
`malformed_lines` stays flat; you just see no new events). A duplicate stream
also lands in `logs/lmstudio.log` (launchd StandardOut), but the rotated
server-logs files are the cleaner source and are what the exporter tails.
Timestamps in the log are local America/Chicago; the exporter ignores them and
lets Prometheus stamp scrape time.

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
send). Match against the `family` label, then extend the lookup below as callers
are onboarded. Current traffic mix (fill in the hashes once observed):

| `family` | Caller | Path | Approx share |
|---|---|---|---|
| _(hash of DA system prompt)_ | DA playthroughs (operator laptop) | direct from `10.40.0.216` | ~58% |
| _(hash per LiteLLM caller)_ | LiteLLM-routed callers | via LiteLLM inference gateway | ~41% |
| `nosys` | any request with no leading system message | — | remainder |

The hash is content-addressed, so a caller that changes its system prompt gets a
new family — expected, and the signal that a prompt changed. `overflow` appearing
means some caller is emitting non-static system prompts (e.g. an embedded
timestamp or session id); investigate that caller rather than raising the cap.

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
   **not** restart Caddy), publishes both the `/cache-metrics` and
   `/proxy-metrics` routes:
   `/Users/copilot/ig88-host/bin/caddy reload --config /Users/copilot/ig88-host/caddy/Caddyfile --adapter caddyfile`
4. Verify the exporter and cache route now (no restart needed):
   - `curl -s http://127.0.0.1:11436/healthz` → `{"status":"ok",...}`
   - `curl -s http://127.0.0.1:11434/cache-metrics | head` → `lmstudio_cache_*`
5. **Param-proxy `/proxy-metrics` requires restarting param-proxy** to load the
   new code, which drops in-flight completions. Do this in an idle window only:
   `sudo launchctl kickstart -k system/com.syntax922.ig88.param-proxy`
   then verify: `curl -s http://127.0.0.1:11434/proxy-metrics | head` →
   `paramproxy_*`, and `make status` shows the `cache-exporter` label.

> **Daemons live in `/Library/LaunchDaemons/`, not per-user LaunchAgents.**
> The authoritative running set is the system-domain daemons installed by
> `apply-system.sh`. Stale per-user copies may exist under
> `~/Library/LaunchAgents/` — those are **not** the running set; ignore them
> and manage services via `system/<label>` as above.

> **Exporter counters reset on restart.** It attaches at end-of-log on
> startup (no history replay), so a restart zeroes the `_total` counters —
> expected Prometheus counter-reset semantics; `rate()` handles it.

## oMLX 0.7.1.dev1 (2026-10-14)

ig88 runs **oMLX 0.7.1.dev1** (operator-approved upgrade from 0.6.4 on
2026-10-14). The 0.6.4 bundle is kept beside it for rollback. Nothing in
`/Applications/oMLX*.app` is version-controlled.

### Install shape (reusable)

```
curl -L -o ~/Downloads/oMLX-<v>-macos26-27.dmg \
  https://github.com/jundot/omlx/releases/download/v<v>/oMLX-<v>-macos26-27.dmg
hdiutil attach -nobrowse -readonly ~/Downloads/oMLX-<v>-macos26-27.dmg
sudo cp -R /Volumes/oMLX/oMLX.app /Applications/oMLX-<v>.app
sudo xattr -dr com.apple.quarantine /Applications/oMLX-<v>.app
/Applications/oMLX-<v>.app/Contents/MacOS/omlx-cli --version   # proves it runs
# at active_requests == 0:
sudo mv /Applications/oMLX.app /Applications/oMLX-<old>.app
sudo mv /Applications/oMLX-<v>.app /Applications/oMLX.app
sudo launchctl kickstart -k system/com.syntax922.ig88.omlx
```

Rollback is the two renames in reverse plus the kickstart. Both pinned
models load in ~30 s. Every SSD prefix-cache entry is invalidated on the
first start of a new version (layer-signature change), so first turns are
cold fleet-wide for a while. The `OMLX_*` env vars exported by
`scripts/omlx-serve.sh` are ignored by 0.7.x (about 150 were removed).

### What changed, measured with the same scripts

| test | 0.6.4 | 0.7.1.dev1 |
|---|---|---|
| greedy 300-token decode | 44 cold / 58 warm tok/s | 61 / 113 tok/s |
| 180k 4-turn via gateway | turns 3-4 rejected; cold 657 s | all admitted; cold 225 s; cached 5-8 s |
| 242k 4-turn direct | cold 1125 s | cold 331 s; cached 7-20 s |
| 2 x 128k concurrent | ~800 s each | ~347 s each |

Production sampling for `flash-next-oq4e-mtp` is the proxy's non-thinking
profile (temp 0.7 / top_p 0.80 / top_k 20 / presence 1.5), not the 1.0/0.95
row in the checkpoint's `generation_config.json` (that is the thinking row).
On that profile decode is ~50 tok/s at 300 tokens with MTP acceptance 55-65 %.

### Memory guard: pinned at 89 GB, and why that is not enough

The `aggressive` tier's ceiling is **dynamic** and twice climbed to 92.3-92.7 GB,
above the 92 GB `iogpu.wired_limit_mb` cap. `--memory-guard-gb 89` (custom
tier) pins it. Even pinned, **a cold prefill of >=~180k tokens dies at the
Metal wall about half the time**, always when it follows another long session:

```
Emergency memory pressure: aborted 1 in-flight request(s) (current=96.6GB, ceiling=92.7GB)
[METAL] Command buffer execution failed: Insufficient Memory (00000008:kIOGPUCommandBufferCallbackErrorOutOfMemory)
Engine loop error: ...
```

The server survives and a retry works. The peak overshoots the ceiling by a
roughly constant ~8 GB (85 GB ceiling -> 93.5 GB peak; 89 -> 96.6), so a
tighter ceiling does not reach the wall: **85 GB was tested and was worse**
(180k failed on every turn; 242k became a clean preflight 400). There is no
max-chunk knob (`prefill_min_chunk_tokens` only). Direct clients get a 500
with the Metal text; through LiteLLM's `/v1/messages` translation the same
failure arrives as an **empty 200 or a hang**. The gateway-side
`claude-local` wrapper therefore defaults to a 128k context.

### MoE expert offload (tested, not enabled)

`moe_expert_offload_enabled: true` with `resident_fraction 0.25` in
`~/.omlx/model_settings.json` streams three quarters of the 68 GB expert
tables from SSD: resident memory 73 GB -> 23 GB, loads in 10 s, Lightning MTP
kept (the MTP head stays resident). With it on, 180k and 242k sessions
completed every turn with no Metal error. Costs on the production profile:
decode 50 -> 15-18 tok/s; cold 180k prefill 1305 s; cold 242k 925 s; cached
turns 6-335 s. Turned back off after the comparison. A middle resident
fraction (0.5) is untested.

### SSD prefix cache raised 300 -> 450 GB (2026-10-14)

The cache was full (1,782 entries; ~182 GB KV blocks + 118 GB fp32 GDN
sidecars) before any long-context test: every request stores its blocks at
4096-token granularity plus a recurrent-state sidecar, with no expiry, only
LRU at the cap. A cold long turn adds 11-12 GB. LRU keeps a live session's
blocks (newest) safe, so the raise lowers churn rather than fixing a failure.
Disk had 260 GB free; 450 leaves ~110 GB. `gdn_sidecar_state_dtype` (fp32)
could halve the sidecars but is untested for recall on this hybrid model.

### SSD cache janitor (2026-10-14)

oMLX has **no time-based expiry** for the paged SSD cache: every request's
blocks + GDN sidecar are kept until LRU at the cap, and a layout-signature
change (version upgrade, offload toggle) marks old entries stale in the index
**but never deletes the files**, which keep counting toward the cap. Measured
2026-10-14: 133 GB of the 300 GB cache was dead (620 pre-upgrade blocks,
71 GB; two dead sidecar signature dirs, 62 GB). Deleted by hand once.

`scripts/omlx-cache-janitor.sh` (launchd
`com.syntax922.ig88.omlx-cache-janitor`, daily 04:30, runs as `copilot`)
deletes blocks and sidecars whose **access** time is older than 7 days
(`OMLX_CACHE_RETENTION_DAYS`). Hits update atime on this volume, so it is
LRU-by-use; shared prefixes hit daily survive. Safe because oMLX prunes the
index entry on a miss and rescans at restart. Log:
`logs/omlx-cache-janitor.log`. `--dry-run` reports without deleting.

### Host-state drift to commit

`scripts/omlx-serve.sh` carries two uncommitted edits (the Sep 10 revert of
`prefill_safe_zone_ratio` to 0.80 — the 0.95 change below never worked — and
the `--memory-guard-gb 89` pin). `gitops-sync.sh` pulls `--ff-only`, which
fails silently once upstream touches that file. `~/.omlx/model_settings.json`
is still untracked. New files to commit: `scripts/omlx-cache-janitor.sh`,
`launchd/com.syntax922.ig88.omlx-cache-janitor.plist`, plus the
`--paged-ssd-cache-max-size 450GB` edit.

## oMLX prefill chunk sizing (memory guard)

> **Historical (2026-09-07 -> 09-10).** The 0.95 ratio change below did not
> work: throttles/day were 350 and 820 while it was live vs 180-504 around it,
> and the chunk stayed 1024. The clamp is `_adaptive_chunk_size`'s
> predicted-peak gate, not the soft watermark analysed here. Reverted to 0.80 on
> 2026-09-10 (uncommitted at the time). Kept for the ceiling arithmetic.

Runbook for `scripts/omlx-serve.sh`'s `prefill_safe_zone_ratio` change
(2026-09-07). All sizes below are GiB; oMLX's log prints GiB and labels it
"GB". Read against the oMLX 0.6.4 source shipped in
`/Applications/oMLX.app/Contents/Resources/omlx/` (`scheduler.py`
`_adaptive_chunk_size`, `process_memory_enforcer.py`).

### What was wrong

Every 8-10k-token prompt on `flash-next-oq4e-mtp` logged
`Prefill throttled ... chunk 4096 -> 1024` (70-130x/hour). The output was
1024 for *every* requested size (1606..4096) and every measured `per_token`
(2.6..7.5 MB), which the headroom gate cannot produce — it would vary. The
uniform 1024 is the scheduler's secondary **band clamp**
(`_PREFILL_STEP_TIERS[0]`), which engages whenever
`usage >= (ceiling x soft_threshold) x prefill_safe_zone_ratio`
= (90.00 x 0.925) x 0.80 = **66.6 GiB**. A model whose weights are ~65 GiB
sits at 73.2-76.9 GiB between chunks, so the clamp was on permanently. The
log line's own advice (raise `iogpu.wired_limit_mb`) points at the wrong knob.

### The two ceilings

| Quantity | Value | Source |
|---|---|---|
| final ceiling (`/health`, enforcer) | 92.00 | min(static 96 - 4 [aggressive], metal cap 92 [sysctl 94208 MiB], dynamic ~89-96) |
| scheduler ceiling (log "ceiling") | 90.00 | 92 - hot-cache reservation (min(2 GB cap, used + 0.5 GB slack)) |
| sizing target (log "sizing target") | 83.25 | 90.00 x 0.925 (`_PREFILL_HEADROOM_SAFETY["aggressive"]`) |
| prefill abort cap | 85.50 | 90.00 x 0.95 (`_PREFILL_ABORT_MARGIN["aggressive"]`) |
| enforcer soft / hard watermark | 85.1 / 87.4 | 92 x 0.925 / 92 x 0.95 (on the unreserved ceiling) |
| Metal wall (panic) | 92.00 | `iogpu.wired_limit_mb` |

The **scheduler ceiling / sizing target** governs chunk sizing. The enforcer's
92 GiB ceiling governs LRU eviction and in-flight aborts. A 4096 chunk runs
only when `usage + per_token x 4096 <= 83.25`; `per_token` is
max(last measured, EWMA, static estimate) x **1.3**
(`_PREFILL_TRANSIENT_SAFETY`; the logged `per_token=` already includes it).
When the dynamic ceiling binds instead (other apps holding RAM) the log says
`dynamic ceiling 89.02GB` and the target drops to 82.34; the same clamp
arithmetic applies to that base.

### Why a full 4096 is not reachable on this box

At the median observed `per_token` (4.9 MB) a 4096 chunk predicts 19.2 GiB
of transient, so usage must be <= 64.0 GiB (65.9 with the hot cache at 0).
Weights alone are ~65 GiB. Every other knob, with numbers:

| Option | Sizing target | 4096 fits? | Notes |
|---|---|---|---|
| current (`aggressive`, hot cache 2 GB, ratio 0.80) | 83.25 | no | clamp -> 1024 always |
| **ratio 0.80 -> 0.95 (this change)** | 83.25 | no | clamp threshold 79.1; chunk = n_fit = **1344..3936**, ~2048 at median |
| + hot cache 2 GB -> 0 | 85.10 | no | +1.85 GiB headroom, ~2496 at median; loses the RAM prefix tier (SSD restore ~2.4 s/2 blocks). Optional second step, not applied |
| `--memory-guard-gb` (custom tier) | 82.80 | no | static reserve 2 GiB but metal cap 92 still binds; headroom safety drops 0.925 -> 0.90. Strictly worse |
| raise `iogpu.wired_limit_mb` | - | no | live 94208 MiB (92 GiB) already exceeds the enforcer's 5% jetsam floor (91.2 GiB); it warns at startup. Repo plist says 93388, live is 94208: drift, see below |
| unpin `qwen3-embedding-0.6b` | 83.25 | no | ~1.2 GiB, +~250 tokens of n_fit; the eviction path reports "no idle model to evict" because both are pinned |
| `--max-concurrent-requests` 5 -> 1 | 83.25 | no | baseline usage is weights; concurrency only matters for the stacked-transient peak (see margins) |
| `scheduler.prefill_priority: speed` | - | no | never shrinks; `_admission_transient_bound` (73.3 + 16.3) vs abort cap 85.5 -> the request is **aborted**, not slowed |
| `--memory-guard off` | - | OOM | observed peak today 85.6 GiB *with* 1024 chunks under three concurrent 10k prompts; unthrottled 4096 -> 73 + 16-19 -> Metal wall |

The alternative that does reach 4096 is a smaller resident model (a ~50 GiB
quant / REAP variant), which is the operator's quality call, not a config
change.

### Margins after the change

Sizing target 83.25 < abort cap 85.5 < enforcer hard 87.4 < Metal wall 92.0.
The predicted-peak gate still holds (usage + 1.3 x measured <= 83.25), so a
granted 2048 chunk is planned 8.75 GiB under the wall. A 2x under-prediction
on a 2048 chunk peaks ~88.6: the enforcer aborts that request at the hard
watermark; Metal does not panic. 0.95 over 0.90 (74.9 GiB, still clamps on
the 76.86 GiB sizing-time reads seen today) and over 0.99 (82.4 GiB, which
deletes the clamp as a backstop).

### Apply (operator, on ig88 as `copilot`)

The key has no `omlx-cli serve` flag and no admin-API setter, so
`omlx-serve.sh` converges `~/.omlx/settings.json` (`memory.prefill_safe_zone_ratio`)
via `scripts/omlx-settings-patch.py` before `exec`. The patch is idempotent,
atomic, fail-open, backs the file up to `settings.json.bak-<UTC>`, and never
prints the file (it holds `auth.secret_key`).

1. `cd /Users/copilot/ig88-host && git pull --ff-only`
2. Idle window only (restart drops in-flight completions, model reload ~1-2 min):
   `sudo launchctl kickstart -k system/com.syntax922.ig88.omlx`
   (`make apply-system` does **not** manage the omlx label; do not use it here.)
3. Confirm the patch ran: `grep omlx-settings-patch logs/omlx.err.log | tail -2`
   -> `memory.prefill_safe_zone_ratio 0.8 -> 0.95` on the first restart,
   `already 0.95; no write` on later ones.

**Merge is armed:** the plist execs `omlx-serve.sh` from the live checkout and
`gitops-sync` ff-pulls `main` nightly at 03:30, so after merge any KeepAlive
restart applies this unattended.

### Watch for 10 minutes after (`logs/omlx.err.log`)

- `grep "Prefill throttled" logs/omlx.err.log | tail` — the line still
  appears (a 4096 request still shrinks), but the output must now **vary**
  with `per_token`: `chunk 4096 -> 2048`, `-> 3232`, `-> 1344`... Any
  `-> 1024` on a `usage` read below 79 GiB means the clamp is still on
  (patch did not apply; check step 3).
- `grep "Memory pressure level" logs/omlx.err.log | tail` — `ok -> soft`
  blips during multi-request bursts are the pre-change baseline (one today at
  85.6 GiB); **any `-> hard`** or any `abort` / `Metal` / `out of memory`
  line is the stop signal -> roll back.
- `curl -s localhost:8000/health` — `loaded_count` 2, `final_ceiling`
  98784247808 (92 GiB) unchanged.
- The `[throttle:*] shrink` lines are debug-level and will not appear at
  `--log-level info`; do not wait for them.

### Rollback (one line)

The serve script converges `settings.json` on every start, so the rollback is
"change the number, restart" — the same path puts 0.80 back into the file.
Do not `git revert` the whole commit first: that removes the patch step and
leaves 0.95 persisted in `settings.json`. launchd does not pass shell
environment through, so exporting `OMLX_PREFILL_SAFE_ZONE_RATIO` in a
terminal does not reach the daemon.

```
cd /Users/copilot/ig88-host && sed -i '' 's/SAFE_ZONE_RATIO:-0.95/SAFE_ZONE_RATIO:-0.80/' scripts/omlx-serve.sh && sudo launchctl kickstart -k system/com.syntax922.ig88.omlx
```

Verify with `grep omlx-settings-patch logs/omlx.err.log | tail -1` ->
`0.95 -> 0.8`. Then commit that one-line change (or land the revert upstream
and `git checkout -- scripts/omlx-serve.sh`) so the nightly `gitops-sync`
ff-pull is not blocked by a dirty tree.

### Known drift (not changed here)

`launchd/com.syntax922.ig88.iogpu-wired-limit.plist` sets 93388 MiB
(91.2 GiB, the enforcer's recommended maximum); the live sysctl is 94208 MiB
(92 GiB). On reboot the plist wins and the metal cap drops to 91.2 -> scheduler
ceiling 89.2 -> sizing target 82.5 (about -0.75 GiB of headroom, ~-160 tokens
of n_fit at the median). Whether to commit 94208 or accept 93388 is an operator
call; the enforcer warns that 94208 is above its jetsam floor.
