#!/usr/bin/env python3
"""LM Studio Prompt-Cache Prometheus Exporter

Tails the LM Studio server log and exposes prompt-cache restore statistics
in Prometheus text format on 127.0.0.1:11436. Cluster Prometheus reaches it
through the existing Caddy :11434 listener at /cache-metrics (see Caddyfile).

Log source:
  LM Studio writes rotating logs under
  /Users/copilot/.lmstudio/server-logs/YYYY-MM/YYYY-MM-DD.N.log
  Rotation is by size (~10 MB) and by day: the ACTIVE file changes NAME on
  rollover (a new YYYY-MM-DD.(N+1).log appears), it is not renamed in place.
  We therefore track "newest file by mtime" rather than a fixed path, drain
  the current file fully before switching, and start a fresh switch at
  offset 0 so the lines written since rollover are counted exactly once.

Parsed line (single canonical format, verified against live logs):
  [2026-07-03 14:11:00][DEBUG][coordinator][INFO]: Prompt cache restore: \
      cached_tokens=14336 uncached_tokens=113 lifetime_efficiency=47.94%

Stdlib only — no pip deps (mirrors the param-proxy constraint, but this
process needs no HTTP client so it stays pure stdlib).
"""

import glob
import http.server
import json
import os
import re
import sys
import threading
import time

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Hardcoded, NOT ~ / expanduser: this runs as a launchd system daemon
# (UserName copilot) where HOME is frequently unset, so expanduser("~")
# would resolve to /var/empty and find zero logs. Every path in this repo
# is hardcoded to /Users/copilot/... — match that.
LOG_DIR = "/Users/copilot/.lmstudio/server-logs"
LOG_GLOB = os.path.join(LOG_DIR, "*", "*.log")
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 11436
POLL_INTERVAL = 1.0  # seconds between tailer polls

CACHE_LINE_RE = re.compile(
    r"Prompt cache restore: "
    r"cached_tokens=(\d+) "
    r"uncached_tokens=(\d+) "
    r"lifetime_efficiency=([0-9.]+)%"
)

# ---------------------------------------------------------------------------
# Metrics state (shared between tailer thread and HTTP handler thread)
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_metrics = {
    "requests_cold": 0,        # cached_tokens == 0
    "requests_partial": 0,     # cached_tokens > 0
    "cached_tokens_total": 0,
    "uncached_tokens_total": 0,
    "lifetime_efficiency": None,  # last seen; None until first observation
    "malformed_lines": 0,
    "current_file": "",
}


def record_line(line: str):
    m = CACHE_LINE_RE.search(line)
    if not m:
        if "Prompt cache restore:" in line:
            with _lock:
                _metrics["malformed_lines"] += 1
        return
    cached = int(m.group(1))
    uncached = int(m.group(2))
    efficiency = float(m.group(3))
    with _lock:
        if cached == 0:
            _metrics["requests_cold"] += 1
        else:
            _metrics["requests_partial"] += 1
        _metrics["cached_tokens_total"] += cached
        _metrics["uncached_tokens_total"] += uncached
        _metrics["lifetime_efficiency"] = efficiency


# ---------------------------------------------------------------------------
# Log tailer
# ---------------------------------------------------------------------------
def newest_log():
    """Path of the newest log file by mtime, or None if none exist."""
    paths = glob.glob(LOG_GLOB)
    if not paths:
        return None
    return max(paths, key=lambda p: os.stat(p).st_mtime)


class Tailer:
    """Follows the newest LM Studio log file across size/day/month rotation.

    Buffers partial lines: a poll-read routinely lands mid-line, so we retain
    the trailing fragment and only parse on complete newlines.
    """

    def __init__(self):
        self._fh = None
        self._path = None
        self._buf = b""

    def _open(self, path, seek_end):
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
        self._fh = open(path, "rb")
        if seek_end:
            self._fh.seek(0, os.SEEK_END)
        self._path = path
        self._buf = b""
        with _lock:
            _metrics["current_file"] = path

    def _drain(self):
        """Read and parse all complete lines currently available."""
        data = self._fh.read()
        if not data:
            return
        self._buf += data
        while True:
            nl = self._buf.find(b"\n")
            if nl == -1:
                break
            line = self._buf[:nl]
            self._buf = self._buf[nl + 1:]
            try:
                record_line(line.decode("utf-8", "replace"))
            except Exception:
                # Never let a single malformed line kill the tailer.
                with _lock:
                    _metrics["malformed_lines"] += 1

    def run(self):
        # Startup: attach to newest file at EOF so we don't replay history
        # (and don't double-count on restart — counters correctly reset).
        while self._fh is None:
            path = newest_log()
            if path:
                self._open(path, seek_end=True)
            else:
                time.sleep(POLL_INTERVAL)

        while True:
            time.sleep(POLL_INTERVAL)
            try:
                # Truncation: file shorter than our position -> reopen at 0.
                st = os.stat(self._path)
                if st.st_size < self._fh.tell():
                    self._open(self._path, seek_end=False)

                self._drain()

                # Rotation: a newer file exists. Drain the current file once
                # more to catch stragglers written before rollover, THEN
                # switch to the new file from offset 0.
                nf = newest_log()
                if nf and nf != self._path:
                    self._drain()
                    self._open(nf, seek_end=False)
            except FileNotFoundError:
                # Active file vanished (e.g. month dir rolled) — re-acquire.
                nf = newest_log()
                if nf:
                    self._open(nf, seek_end=False)
            except Exception as e:
                print("[cache-exporter] tailer error: %s" % e, file=sys.stderr)
                time.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Prometheus rendering
# ---------------------------------------------------------------------------
def render_metrics() -> bytes:
    with _lock:
        m = dict(_metrics)
    lines = [
        "# HELP lmstudio_cache_requests_total Prompt-cache restore events by hit class.",
        "# TYPE lmstudio_cache_requests_total counter",
        'lmstudio_cache_requests_total{hit="cold"} %d' % m["requests_cold"],
        'lmstudio_cache_requests_total{hit="partial"} %d' % m["requests_partial"],
        "# HELP lmstudio_cache_cached_tokens_total Tokens served from prompt cache.",
        "# TYPE lmstudio_cache_cached_tokens_total counter",
        "lmstudio_cache_cached_tokens_total %d" % m["cached_tokens_total"],
        "# HELP lmstudio_cache_uncached_tokens_total Tokens recomputed (cache miss).",
        "# TYPE lmstudio_cache_uncached_tokens_total counter",
        "lmstudio_cache_uncached_tokens_total %d" % m["uncached_tokens_total"],
        "# HELP lmstudio_cache_exporter_malformed_lines_total Cache lines that failed to parse.",
        "# TYPE lmstudio_cache_exporter_malformed_lines_total counter",
        "lmstudio_cache_exporter_malformed_lines_total %d" % m["malformed_lines"],
    ]
    # Suppress the efficiency gauge until we have a real observation — a
    # freshly restarted exporter (seek-to-EOF) parses nothing during an idle
    # window, and emitting 0.0 there reads as a real "0% efficiency".
    if m["lifetime_efficiency"] is not None:
        lines += [
            "# HELP lmstudio_cache_lifetime_efficiency Last reported lifetime cache efficiency (percent).",
            "# TYPE lmstudio_cache_lifetime_efficiency gauge",
            "lmstudio_cache_lifetime_efficiency %g" % m["lifetime_efficiency"],
        ]
    return ("\n".join(lines) + "\n").encode("utf-8")


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path in ("/healthz", "/health"):
            with _lock:
                body = json.dumps({
                    "status": "ok",
                    "current_file": _metrics["current_file"],
                }).encode("utf-8")
            self._respond(200, "application/json", body)
            return
        if self.path.rstrip("/") in ("", "/metrics"):
            self._respond(200, "text/plain; version=0.0.4", render_metrics())
            return
        self._respond(404, "text/plain", b"not found\n")

    def _respond(self, status, ctype, body):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    Tailer_thread = threading.Thread(target=Tailer().run, daemon=True)
    Tailer_thread.start()
    server = http.server.ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    print("[cache-exporter] listening on %s:%d, tailing %s"
          % (LISTEN_HOST, LISTEN_PORT, LOG_GLOB), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
