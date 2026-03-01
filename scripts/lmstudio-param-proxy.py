#!/usr/bin/env python3
"""LM Studio Inference Parameter Proxy

Ensures optimal sampling parameters for Qwen3-Next models and provides
automatic retry logic for LM Studio's transient errors during model loading.

Architecture:
  Caddy :11434 → This Proxy :11435 → LM Studio :1234

LM Studio concurrency behaviour (observed empirically):
  - Model already loaded → concurrent requests work fine (serialized internally)
  - During JIT model loading → concurrent requests get HTTP 500 (instant reject)
  - Different-model request while another is active → HTTP 400 "Model unloaded."

Mitigation strategy (retry with backoff, NO serialization):
  When the proxy receives a retryable error (500 during JIT load, 400 model unloaded),
  it retries with exponential backoff. This lets the first request trigger the model
  load and subsequent retries land after the model is ready (~10s typical JIT time).
  No semaphore/serialization is used because LM Studio handles concurrent requests
  fine once the model is loaded, and serialization causes streaming timeouts.

Qwen3 official docs: "DO NOT use greedy decoding —
it can lead to performance degradation and endless repetitions."

Recommended parameters (non-thinking / instruct-only mode):
  temperature: 0.7, top_p: 0.8, top_k: 20, presence_penalty: 1.0
"""

import http.server
import json
import logging
import sys
import time
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

# Models that need parameter clamping (Qwen3-Next & Qwen3.5 architecture, no thinking mode)
QWEN3_MODELS = frozenset({
    "qwen3-coder-next-mlx",
    "qwen3-next-80b-a3b-instruct-mlx",
    "qwen3.5-35b-a3b",
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
# Retry Configuration
# ---------------------------------------------------------------------------
# Retry settings for transient LM Studio errors:
#   - HTTP 500 during JIT model loading (instant <10ms rejection)
#   - HTTP 400 with "Model unloaded." during model swapping
#
# Backoff schedule: 2s, 4s, 6s, 8s, 10s (cumulative worst-case ~30s)
# This covers a typical JIT model load time of ~10-15s.
MAX_RETRIES = 5
RETRY_BACKOFF_BASE = 2.0       # seconds — actual delay = base * (attempt + 1)
RETRYABLE_STATUS_CODES = {500, 400}

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
    - temperature too low -> repetition loops
    - top_p too high -> no nucleus sampling
    - missing presence_penalty -> no repetition penalty
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

    # Temperature: set default if missing, clamp to min 0.6 if too low
    temp = data.get("temperature")
    if temp is None or temp < QWEN3_OVERRIDES["min_temperature"]:
        data["temperature"] = QWEN3_OVERRIDES["default_temperature"]

    # top_p: set to 0.8 if missing, cap at 0.8 if too high
    top_p = data.get("top_p")
    if top_p is None or top_p > QWEN3_OVERRIDES["max_top_p"]:
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
            "Clamped params for %s: %s -> %s",
            model,
            {k: v for k, v in original.items() if v is not None},
            modified,
        )

    return data


def _is_retryable_error(status_code: int, body: bytes = b"") -> bool:
    """Check if an LM Studio error response is a transient/retryable error.

    Known retryable patterns:
    - HTTP 500 during JIT model loading (instant <10ms rejection)
    - HTTP 400 with {"error": "Model unloaded."} during model swapping
    - HTTP 400 with "not exist" during model state transition
    """
    if status_code == 500:
        return True
    if status_code == 400:
        try:
            data = json.loads(body)
            error_msg = str(data.get("error", ""))
            return "unloaded" in error_msg.lower() or "not exist" in error_msg.lower()
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass
    return False


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    """Transparent reverse proxy with parameter clamping and retry logic."""

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
                    # Signal end-of-response via connection close (we strip
                    # Transfer-Encoding so the client has no other way to
                    # detect the stream is complete).
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.close_connection = True
                    for chunk in resp.iter_bytes():
                        self.wfile.write(chunk)
                        self.wfile.flush()
        except Exception as e:
            log.error("Upstream streaming error: %s", e)
            self.send_error(502, f"Upstream streaming error: {e}")

    def _proxy_chat_with_retry(self, body: bytes, is_stream: bool):
        """Proxy a chat completion with automatic retry on transient errors.

        When LM Studio returns a transient error (500 during JIT loading,
        400 model unloaded), waits with backoff and retries.
        """
        for attempt in range(MAX_RETRIES + 1):
            try:
                if is_stream:
                    result = self._try_stream_with_retry(body, attempt)
                else:
                    result = self._try_request_with_retry(body, attempt)

                if result == "success" or result == "forwarded":
                    return
                # result == "retry" -> continue loop

            except Exception as e:
                if attempt < MAX_RETRIES:
                    delay = RETRY_BACKOFF_BASE * (attempt + 1)
                    log.error(
                        "Exception (attempt %d/%d): %s — retrying in %.1fs",
                        attempt + 1, MAX_RETRIES + 1, e, delay,
                    )
                    time.sleep(delay)
                else:
                    log.error("Exception after all retries: %s", e)

        # All retries exhausted — send structured error
        log.error("All %d attempts exhausted for chat completion", MAX_RETRIES + 1)
        self._send_json_error(
            503,
            "LM Studio model loading timeout. All retries exhausted. "
            "The model may still be loading. Please try again shortly.",
            "model_loading_timeout",
        )

    def _try_request_with_retry(self, body: bytes, attempt: int) -> str:
        """Non-streaming request with retry check. Returns 'success', 'retry', or 'forwarded'."""
        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            resp = client.request(
                "POST",
                f"{UPSTREAM_URL}{self.path}",
                headers=self._forward_headers(),
                content=body,
            )

            if resp.status_code == 200:
                if attempt > 0:
                    log.info("Succeeded on retry %d/%d", attempt + 1, MAX_RETRIES + 1)
                self._send_upstream_response(resp)
                return "success"

            if _is_retryable_error(resp.status_code, resp.content) and attempt < MAX_RETRIES:
                delay = RETRY_BACKOFF_BASE * (attempt + 1)
                log.warning(
                    "Retryable error (attempt %d/%d): HTTP %d — retrying in %.1fs",
                    attempt + 1, MAX_RETRIES + 1, resp.status_code, delay,
                )
                time.sleep(delay)
                return "retry"

            # Non-retryable or last retry — forward as-is
            self._send_upstream_response(resp)
            return "forwarded"

    def _try_stream_with_retry(self, body: bytes, attempt: int) -> str:
        """Streaming request with retry check. Returns 'success', 'retry', or 'forwarded'.

        Key insight: httpx stream gives us the status code before we read the body.
        If the status is retryable, we consume the error body and return 'retry'
        without having sent anything to the client.
        """
        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            with client.stream(
                "POST",
                f"{UPSTREAM_URL}{self.path}",
                headers=self._forward_headers(),
                content=body,
            ) as resp:
                # Check status BEFORE streaming to the client
                if _is_retryable_error(resp.status_code, b"") and attempt < MAX_RETRIES:
                    # Consume error body so connection is cleanly closed
                    try:
                        resp.read()
                    except Exception:
                        pass
                    delay = RETRY_BACKOFF_BASE * (attempt + 1)
                    log.warning(
                        "Retryable stream error (attempt %d/%d): HTTP %d — retrying in %.1fs",
                        attempt + 1, MAX_RETRIES + 1, resp.status_code, delay,
                    )
                    time.sleep(delay)
                    return "retry"

                # Stream the response to the client
                if attempt > 0 and resp.status_code == 200:
                    log.info("Stream succeeded on retry %d/%d", attempt + 1, MAX_RETRIES + 1)
                self.send_response(resp.status_code)
                for k, v in resp.headers.multi_items():
                    if k.lower() not in ("transfer-encoding",):
                        self.send_header(k, v)
                # Signal end-of-response via connection close
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                for chunk in resp.iter_bytes():
                    self.wfile.write(chunk)
                    self.wfile.flush()
                return "success"

    def _send_upstream_response(self, resp):
        """Forward an httpx response to the client."""
        self.send_response(resp.status_code)
        for k, v in resp.headers.multi_items():
            if k.lower() not in ("transfer-encoding",):
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(resp.content)

    def _send_json_error(self, status: int, message: str, code: str):
        """Send a structured JSON error response (OpenAI-compatible format)."""
        error_body = json.dumps({
            "error": {
                "message": message,
                "type": "server_error",
                "code": code,
            }
        }).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(error_body)))
        self.end_headers()
        self.wfile.write(error_body)

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

        if is_chat:
            self._proxy_chat_with_retry(body, is_stream)
        elif is_stream:
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
        "Parameter proxy listening on %s:%d -> %s",
        LISTEN_HOST, LISTEN_PORT, UPSTREAM_URL,
    )
    log.info("Qwen3-Next models: %s", ", ".join(sorted(QWEN3_MODELS)))
    log.info(
        "Clamping: temp>=%.1f, top_p<=%.1f, top_k=%d, presence_penalty=%.1f",
        QWEN3_OVERRIDES["min_temperature"],
        QWEN3_OVERRIDES["max_top_p"],
        QWEN3_OVERRIDES["default_top_k"],
        QWEN3_OVERRIDES["default_presence_penalty"],
    )
    log.info(
        "Retry policy: max_retries=%d, backoff_base=%.1fs (no serialization)",
        MAX_RETRIES, RETRY_BACKOFF_BASE,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
