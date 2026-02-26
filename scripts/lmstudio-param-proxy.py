#!/usr/bin/env python3
"""LM Studio Inference Parameter Proxy

Ensures optimal sampling parameters for Qwen3-Next models.
VS Code Copilot Chat sends temperature=0.1, top_p=1, which causes
Qwen3-Next models to enter repetition loops (greedy decoding).

Architecture:
  Caddy :11434 → This Proxy :11435 → LM Studio :1234

Qwen3 official docs: "DO NOT use greedy decoding —
it can lead to performance degradation and endless repetitions."

Recommended parameters (non-thinking / instruct-only mode):
  temperature: 0.7, top_p: 0.8, top_k: 20, presence_penalty: 1.0
"""

import http.server
import json
import logging
import sys
import threading
from urllib.parse import urlparse

import httpx

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
UPSTREAM_URL = "http://127.0.0.1:1234"
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 11435
REQUEST_TIMEOUT = 300  # seconds (model loading + generation)

# Models that need parameter clamping (Qwen3-Next architecture, no thinking mode)
QWEN3_MODELS = frozenset({
    "qwen3-coder-next-mlx",
    "qwen3-next-80b-a3b-instruct-mlx",
})

# Qwen3-Next recommended non-thinking mode parameters
QWEN3_OVERRIDES = {
    "min_temperature": 0.6,       # Floor - never go below this
    "default_temperature": 0.7,   # Use when client sends < min
    "max_top_p": 0.8,             # Cap - never go above this
    "default_top_k": 20,          # Add if missing
    "default_presence_penalty": 1.0,  # Add if missing/zero
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [param-proxy] %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("param-proxy")


def clamp_params(data: dict) -> dict:
    """Fix sampling parameters for Qwen3-Next models.

    Only modifies parameters that would cause known issues:
    - temperature too low → repetition loops
    - top_p too high → no nucleus sampling
    - missing presence_penalty → no repetition penalty
    """
    model = data.get("model", "")
    if model not in QWEN3_MODELS:
        return data

    original = {
        "temperature": data.get("temperature"),
        "top_p": data.get("top_p"),
        "top_k": data.get("top_k"),
        "presence_penalty": data.get("presence_penalty"),
    }

    # Temperature: clamp to minimum 0.6 (Qwen3 non-thinking recommended floor)
    temp = data.get("temperature")
    if temp is not None and temp < QWEN3_OVERRIDES["min_temperature"]:
        data["temperature"] = QWEN3_OVERRIDES["default_temperature"]

    # top_p: cap at 0.8 (1.0 = no filtering, bad for Qwen3)
    top_p = data.get("top_p")
    if top_p is not None and top_p > QWEN3_OVERRIDES["max_top_p"]:
        data["top_p"] = QWEN3_OVERRIDES["max_top_p"]

    # top_k: add if missing
    if "top_k" not in data:
        data["top_k"] = QWEN3_OVERRIDES["default_top_k"]

    # presence_penalty: add if missing or zero
    pp = data.get("presence_penalty", 0)
    if pp == 0:
        data["presence_penalty"] = QWEN3_OVERRIDES["default_presence_penalty"]

    modified = {
        "temperature": data.get("temperature"),
        "top_p": data.get("top_p"),
        "top_k": data.get("top_k"),
        "presence_penalty": data.get("presence_penalty"),
    }

    if original != modified:
        log.info(
            "Clamped params for %s: %s → %s",
            model,
            {k: v for k, v in original.items() if v is not None},
            modified,
        )

    return data


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    """Transparent reverse proxy with parameter clamping for chat completions."""

    # Suppress default request logging (we do our own)
    def log_message(self, format, *args):
        pass

    def _forward_headers(self) -> dict:
        headers = {}
        for k, v in self.headers.items():
            if k.lower() not in ("host", "transfer-encoding", "content-length"):
                headers[k] = v
        return headers

    def _proxy_simple(self, method: str, body: bytes = b""):
        """Non-streaming proxy pass-through."""
        try:
            with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
                resp = client.request(
                    method,
                    f"{UPSTREAM_URL}{self.path}",
                    headers=self._forward_headers(),
                    content=body,
                )
                self.send_response(resp.status_code)
                for k, v in resp.headers.multi_items():
                    if k.lower() not in ("transfer-encoding",):
                        self.send_header(k, v)
                self.end_headers()
                self.wfile.write(resp.content)
        except Exception as e:
            log.error("Upstream error: %s", e)
            self.send_error(502, f"Upstream error: {e}")

    def _proxy_stream(self, body: bytes):
        """Streaming proxy for SSE responses (chat completions with stream=true)."""
        try:
            with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
                with client.stream(
                    "POST",
                    f"{UPSTREAM_URL}{self.path}",
                    headers=self._forward_headers(),
                    content=body,
                ) as resp:
                    self.send_response(resp.status_code)
                    for k, v in resp.headers.multi_items():
                        if k.lower() not in ("transfer-encoding",):
                            self.send_header(k, v)
                    self.end_headers()
                    for chunk in resp.iter_bytes():
                        self.wfile.write(chunk)
                        self.wfile.flush()
        except Exception as e:
            log.error("Upstream streaming error: %s", e)
            self.send_error(502, f"Upstream streaming error: {e}")

    def do_GET(self):
        log.debug("GET %s", self.path)
        self._proxy_simple("GET")

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b""

        is_chat = "/chat/completions" in self.path
        is_stream = False

        if is_chat and body:
            try:
                data = json.loads(body)
                is_stream = data.get("stream", False)
                data = clamp_params(data)
                body = json.dumps(data).encode("utf-8")
                log.info(
                    "POST %s model=%s stream=%s temp=%.2f top_p=%.2f pp=%.1f",
                    self.path,
                    data.get("model", "?"),
                    is_stream,
                    data.get("temperature", -1),
                    data.get("top_p", -1),
                    data.get("presence_penalty", 0),
                )
            except (json.JSONDecodeError, KeyError) as e:
                log.warning("Failed to parse request body: %s", e)
        else:
            log.debug("POST %s (passthrough)", self.path)

        if is_stream:
            self._proxy_stream(body)
        else:
            self._proxy_simple("POST", body)

    def do_DELETE(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b""
        self._proxy_simple("DELETE", body)

    def do_PUT(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b""
        self._proxy_simple("PUT", body)

    def do_OPTIONS(self):
        self._proxy_simple("OPTIONS")


def main():
    server = http.server.ThreadingHTTPServer(
        (LISTEN_HOST, LISTEN_PORT), ProxyHandler
    )
    log.info(
        "Parameter proxy listening on %s:%d → %s",
        LISTEN_HOST, LISTEN_PORT, UPSTREAM_URL,
    )
    log.info("Qwen3-Next models: %s", ", ".join(sorted(QWEN3_MODELS)))
    log.info(
        "Clamping: temp≥%.1f, top_p≤%.1f, top_k=%d, presence_penalty=%.1f",
        QWEN3_OVERRIDES["min_temperature"],
        QWEN3_OVERRIDES["max_top_p"],
        QWEN3_OVERRIDES["default_top_k"],
        QWEN3_OVERRIDES["default_presence_penalty"],
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
