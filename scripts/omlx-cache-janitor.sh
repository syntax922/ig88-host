#!/bin/bash
# oMLX paged-SSD-cache janitor. oMLX has no time-based expiry: it stores
# every request's KV blocks (4096-token granularity) plus a GDN recurrent-state
# sidecar, and only LRU-evicts at the cap. Worse, a layout-signature change
# (version upgrade, offload toggle) marks old entries stale in the index but
# never deletes the files, so they keep counting toward the cap. On 2026-10-14
# 133 GB of the 300 GB cache was such dead weight.
#
# Policy: delete blocks and sidecars whose ACCESS time is older than
# RETENTION_DAYS. Cache hits update atime on this APFS volume (verified
# 2026-10-14), so this is LRU-by-use, and the daily-hit shared prefixes
# survive. oMLX tolerates files vanishing: a miss prunes the index entry, and
# a restart rescans the directory. Files written in the last day are never
# touched (in-flight guard). Only the hex block dirs and _gdn_sidecars are
# swept; nothing else under the cache dir is.
#
#   omlx-cache-janitor.sh            # delete
#   omlx-cache-janitor.sh --dry-run  # report only
# Env: OMLX_CACHE_DIR (default ~/.omlx/cache), OMLX_CACHE_RETENTION_DAYS (7).
set -u
C="${OMLX_CACHE_DIR:-$HOME/.omlx/cache}"
DAYS="${OMLX_CACHE_RETENTION_DAYS:-7}"
LOG="$HOME/ig88-host/logs/omlx-cache-janitor.log"
DRY=0; [ "${1:-}" = "--dry-run" ] && DRY=1
[ -d "$C" ] || { echo "$(date -u +%FT%TZ) janitor: no cache dir $C" >>"$LOG"; exit 0; }
sweep() { # $1 = find root(s) pattern, $2 = name filter (or "")
  local list; list=$(mktemp)
  # shellcheck disable=SC2086
  find $1 -type f $2 -atime +"$DAYS" -mtime +1 -print0 2>/dev/null >"$list"
  local n bytes; n=$(tr -cd '\0' <"$list" | wc -c | tr -d ' ')
  bytes=$(xargs -0 stat -f %z <"$list" 2>/dev/null | awk '{s+=$1} END{print s+0}')
  [ "$DRY" = 0 ] && [ "$n" -gt 0 ] && xargs -0 rm -f <"$list"
  rm -f "$list"; echo "$n $bytes"
}
read -r nb bb <<<"$(sweep "$C/[0-9a-f]" "-name *.safetensors")"
read -r ns bs <<<"$(sweep "$C/_gdn_sidecars" "")"
nd=0
if [ "$DRY" = 0 ]; then nd=$(find "$C/_gdn_sidecars" -mindepth 1 -type d -empty -delete -print 2>/dev/null | wc -l | tr -d ' '); fi
printf '%s janitor: retention=%sd dry=%s blocks=%s (%.1f GB) sidecars=%s (%.1f GB) empty_dirs=%s cache_now=%s\n' \
  "$(date -u +%FT%TZ)" "$DAYS" "$DRY" "$nb" "$(echo "$bb/1e9" | bc -l)" "$ns" "$(echo "$bs/1e9" | bc -l)" "$nd" "$(du -sh "$C" 2>/dev/null | cut -f1)" | tee -a "$LOG"
