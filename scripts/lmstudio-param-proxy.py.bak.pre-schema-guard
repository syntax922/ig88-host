#!/usr/bin/env python3
"""LM Studio Inference Parameter Proxy

Ensures sane sampling parameters for Qwen3-Next models, implements a
thinking on/off switch the MLX runtime doesn't honor natively, and provides
automatic retry logic for LM Studio's transient errors during model loading.

Architecture:
  Caddy :11434 → This Proxy :11435 → LM Studio :1234

Two Qwen3 quirks are normalized here (the single layer that owns them):
  1. Temperature is RESPECTED, not overridden — an explicit value passes through
     to the model; only a sub-floor value is raised to the Qwen3 minimum (0.6,
     anti-greedy), and a missing value gets a 0.7 default. (Was: silently forced
     to 1.0, which ignored the caller.)
  2. Thinking on/off via `chat_template_kwargs.enable_thinking`. LM Studio/MLX
     ignores that field, so this proxy implements it: `enable_thinking: false`
     prefills a closed `<think></think>` assistant turn so the model skips its
     reasoning preamble (the only reliable no-think lever on this stack). Absent
     or `true` => native thinking (no behavior change for existing callers).

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

import hashlib
import http.server
import json
import logging
import sys
import time
import threading
from collections import defaultdict
from urllib.parse import urlparse

import httpx

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
UPSTREAM_URL = "http://127.0.0.1:1234"
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 11435
REQUEST_TIMEOUT = 300  # seconds (model loading + generation)

# ---------------------------------------------------------------------------
# Shared HTTP client (connection pooling)
# ---------------------------------------------------------------------------
# CRITICAL: Do NOT create a new httpx.Client per request — that opens a new
# TCP socket every time, and closed sockets linger in TIME_WAIT for 2×MSL
# (30 s on macOS).  At sustained load the ephemeral port range (16 k ports)
# is exhausted within hours, making ALL localhost connections fail.
#
# A single long-lived client reuses connections via HTTP keep-alive, so only
# a handful of sockets are ever open to upstream.
#
# Stale-keepalive mitigation (2026-05-12): LMStudio appears to close idle
# keepalive sockets without sending FIN; the next reuse hits a half-open
# connection and httpx raises an exception whose `str(e)` is empty. The
# proxy retried with backoff, but every retry hit the same stale pool,
# producing an outer "Exception (attempt 1/6): — retrying" loop that
# matched the gateway-side 600s timeout. Two changes:
#   1. retries=2 on the transport so httpx itself re-establishes the
#      connection on the half-open detection instead of bubbling up.
#   2. keepalive_expiry=10 (was 30) so we proactively close idle sockets
#      well before LMStudio's apparent idle-close threshold.
_upstream_client = httpx.Client(
    base_url=UPSTREAM_URL,
    timeout=REQUEST_TIMEOUT,
    limits=httpx.Limits(
        max_connections=20,
        max_keepalive_connections=5,
        keepalive_expiry=10,          # was 30 — see stale-keepalive note above
    ),
    transport=httpx.HTTPTransport(retries=2),
)

# Models that need parameter clamping (Qwen3-Next & Qwen3.5 architecture, no thinking mode)
QWEN3_MODELS = frozenset({
    "qwen3-coder-next-mlx",
    "qwen3-next-80b-a3b-instruct-mlx",
    "qwen3.5-35b-a3b",
    "qwen3.5-122b-a10b",
})

# Qwen3.5 recommended sampling parameters — MODE-DEPENDENT, straight from the
# Qwen3.5-35B-A3B model card "Best Practices" (general-task profiles):
#   non-thinking (instruct): temp 0.7, top_p 0.80, top_k 20, presence_penalty 1.5
#   thinking:                temp 1.0, top_p 0.95, top_k 20, presence_penalty 1.5
# (precise-coding thinking is temp 0.6 — callers wanting that send it explicitly.)
#
# TRANSPARENCY (2026-06-09): these are DEFAULTS applied only when the caller
# omits a value. An explicit value is RESPECTED (a caller asking for temp 0.7
# gets 0.7) — we no longer silently force temperature to 1.0. The one guard is
# the Qwen3 "DO NOT use greedy decoding" floor: a sub-floor temperature (incl. 0)
# is raised to MIN_TEMPERATURE (0.6, the lowest Qwen-recommended value), logged.
MIN_TEMPERATURE = 0.6  # anti-greedy floor for explicit sub-floor temps

QWEN3_DEFAULTS = {
    # enable_thinking=false (no-think / instruct, general tasks)
    False: {"temperature": 0.7, "top_p": 0.80, "top_k": 20, "presence_penalty": 1.5},
    # thinking on (absent switch or true; general tasks) — matches prior live behavior
    True: {"temperature": 1.0, "top_p": 0.95, "top_k": 20, "presence_penalty": 1.5},
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

# ---------------------------------------------------------------------------
# Health Check (bypass LM Studio event loop)
# ---------------------------------------------------------------------------
# LM Studio blocks ALL HTTP requests during inference (single-threaded event
# loop).  When it is processing a 10-40s chat completion, /v1/models probes
# timeout.  We cache the last successful /v1/models response and serve it
# from cache at /healthz so health checks never block on inference.

_health_cache = {
    "models_json": b'{"status":"ok","cached":true}',
    "last_success": 0.0,
    "ttl": 300,  # cache for 5 minutes
}
_health_lock = threading.Lock()


def update_health_cache(body: bytes):
    with _health_lock:
        _health_cache["models_json"] = body
        _health_cache["last_success"] = time.time()


def get_health_response() -> bytes:
    with _health_lock:
        age = time.time() - _health_cache["last_success"]
        if age < _health_cache["ttl"]:
            return json.dumps({
                "status": "ok",
                "upstream": "cached",
                "cache_age_s": round(age, 1),
            }).encode("utf-8")
        else:
            return json.dumps({
                "status": "degraded",
                "upstream": "stale",
                "cache_age_s": round(age, 1),
            }).encode("utf-8")


# ---------------------------------------------------------------------------
# Per-prompt-family metrics (ADDITIVE, fail-open)
# ---------------------------------------------------------------------------
# A "prompt family" is the first 12 hex of sha256 over the system-message
# content. Each caller ships a distinct static system prompt, so families map
# ~1:1 to callers (cardinality naturally ~20). All of this is best-effort
# telemetry — every path is wrapped so a fault here can never alter proxying.
# A hard cap buckets any overflow (e.g. a caller with a dynamic system prompt)
# into family="overflow" so the metrics dict / Prometheus can't blow up.
MAX_FAMILIES = 64

_metrics_lock = threading.Lock()
_family_requests = defaultdict(int)
_family_latency_sum = defaultdict(float)
_family_latency_count = defaultdict(int)


def prompt_family(data: dict) -> str:
    """First 12 hex of sha256 of the system message content, or 'nosys'."""
    msgs = data.get("messages") or []
    if not msgs or msgs[0].get("role") != "system":
        return "nosys"
    content = msgs[0].get("content", "")
    if not isinstance(content, str):
        content = json.dumps(content, sort_keys=True)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]


def record_family(family: str, elapsed: float):
    with _metrics_lock:
        if family not in _family_requests and len(_family_requests) >= MAX_FAMILIES:
            family = "overflow"
        _family_requests[family] += 1
        _family_latency_sum[family] += elapsed
        _family_latency_count[family] += 1


def render_family_metrics() -> bytes:
    with _metrics_lock:
        requests = dict(_family_requests)
        lat_sum = dict(_family_latency_sum)
        lat_count = dict(_family_latency_count)
    lines = [
        "# HELP paramproxy_requests_total Chat-completion requests by prompt family.",
        "# TYPE paramproxy_requests_total counter",
    ]
    for fam, n in sorted(requests.items()):
        lines.append('paramproxy_requests_total{family="%s"} %d' % (fam, n))
    lines += [
        "# HELP paramproxy_upstream_seconds Upstream chat-completion latency by prompt family.",
        "# TYPE paramproxy_upstream_seconds summary",
    ]
    for fam in sorted(lat_count):
        lines.append('paramproxy_upstream_seconds_sum{family="%s"} %g' % (fam, lat_sum[fam]))
        lines.append('paramproxy_upstream_seconds_count{family="%s"} %d' % (fam, lat_count[fam]))
    return ("\n".join(lines) + "\n").encode("utf-8")


# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [param-proxy] %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("param-proxy")


# Closed-thinking prefill. Appended as the final assistant turn so the model
# treats its <think> block as already complete and emits the answer directly.
THINK_PREFILL_CONTENT = "<think>\n\n</think>\n\n"

# Structured-output grammar-skip mitigation (2026-07-06): LM Studio
# intermittently fails to APPLY a json_schema/json_object response_format —
# observed during JIT-load races and under concurrent requests. The response
# then contains unconstrained text (reasoning preamble, ```json fences,
# double emission) instead of schema-valid JSON. When the grammar IS applied
# it constrains from token 0 (thinking is impossible), so non-JSON content on
# a structured request is a reliable skip signature. We validate and retry.
GRAMMAR_SKIP_MAX_RETRIES = 2


def _json_grammar_skipped(raw: bytes) -> bool:
    """True when a structured-output completion came back with non-JSON content.

    Conservative: only flags a standard, non-tool-call completion envelope
    whose string content fails to parse as JSON. Anything unusual (tool
    calls, empty content, unexpected shape) is forwarded untouched.
    """
    try:
        msg = json.loads(raw)["choices"][0]["message"]
    except Exception:
        return False
    if msg.get("tool_calls"):
        return False
    content = msg.get("content")
    if not isinstance(content, str) or not content.strip():
        return False
    try:
        json.loads(content)
        return False
    except ValueError:
        return True


def normalize_qwen_request(data: dict) -> dict:
    """Normalize a Qwen3 chat request: mode-aware sampling defaults + thinking switch.

    Single layer that owns the Qwen3 quirks (the gateway/callers stay clean):

    THINKING SWITCH — `chat_template_kwargs.enable_thinking`. LM Studio's MLX
    runtime IGNORES that field (verified 2026-06-09; `/no_think` soft-switches are
    also unsupported on Qwen3.5), so we IMPLEMENT it: `enable_thinking: false`
    prefills a closed `<think></think>` final assistant turn — the only reliable
    no-think lever on this stack (the model continues past the closed block and
    skips its reasoning preamble → clean structured output). Contract, no silent
    default-flip: ABSENT or `true` => native thinking (unchanged for existing
    callers); `false` => no-think. The kwarg is consumed + stripped before forward.

    SAMPLING — mode-aware Qwen3.5 model-card defaults, applied ONLY to omitted
    params (explicit caller values are RESPECTED, not overridden):
      no-think (instruct, general): temp 0.7, top_p 0.80, top_k 20, pp 1.5
      thinking      (general):      temp 1.0, top_p 0.95, top_k 20, pp 1.5
    An explicit sub-floor temperature is raised to MIN_TEMPERATURE (0.6, Qwen3
    "no greedy decoding") — never silently forced to 1.0.
    """
    model = data.get("model", "")
    if model not in QWEN3_MODELS:
        return data

    # --- thinking mode detection (consume the kwarg + client-dialect aliases) ---
    # Accepted spellings, all consumed + stripped before forwarding (LM Studio
    # ignores every one of them natively); first explicit boolean wins:
    #   chat_template_kwargs.enable_thinking   (HF / vLLM chat-template dialect)
    #   enable_thinking                        (top-level vLLM dialect)
    #   think                                  (Ollama 0.7+ dialect)
    ctk = data.get("chat_template_kwargs")
    _candidates = (
        ctk.get("enable_thinking") if isinstance(ctk, dict) else None,
        data.get("enable_thinking"),
        data.get("think"),
    )
    if "chat_template_kwargs" in data:
        del data["chat_template_kwargs"]
    for _alias in ("enable_thinking", "think"):
        data.pop(_alias, None)
    enable_thinking = next((c for c in _candidates if isinstance(c, bool)), None)
    no_think = enable_thinking is False  # absent/true => thinking on
    defaults = QWEN3_DEFAULTS[not no_think]

    before = {k: data.get(k) for k in ("temperature", "top_p", "top_k", "presence_penalty")}

    # temperature: respect explicit; default when missing; floor sub-0.6 (anti-greedy)
    temp = data.get("temperature")
    if temp is None:
        data["temperature"] = defaults["temperature"]
    elif temp < MIN_TEMPERATURE:
        data["temperature"] = MIN_TEMPERATURE
    # top_p / top_k: respect explicit; default when missing
    if data.get("top_p") is None:
        data["top_p"] = defaults["top_p"]
    if "top_k" not in data:
        data["top_k"] = defaults["top_k"]
    # presence_penalty: fill when missing or zero (zero == no repetition penalty)
    if data.get("presence_penalty", 0) == 0:
        data["presence_penalty"] = defaults["presence_penalty"]

    after = {k: data.get(k) for k in ("temperature", "top_p", "top_k", "presence_penalty")}
    if before != after:
        log.info(
            "qwen %s [%s]: %s -> %s",
            model,
            "no-think" if no_think else "thinking",
            {k: v for k, v in before.items() if v is not None},
            after,
        )

    # --- no-think prefill ---
    if no_think:
        msgs = data.get("messages")
        if isinstance(msgs, list) and msgs and msgs[-1].get("role") != "assistant":
            msgs.append({"role": "assistant", "content": THINK_PREFILL_CONTENT})
            log.info("no-think: prefilled closed <think> block for %s", model)

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
            resp = _upstream_client.request(
                method,
                self.path,
                headers=self._forward_headers(),
                content=body,
            )
            self.send_response(resp.status_code)
            for k, v in resp.headers.multi_items():
                if k.lower() not in ("transfer-encoding",):
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(resp.content)
            # Cache successful /v1/models responses for health endpoint
            if resp.status_code == 200 and "/models" in self.path:
                update_health_cache(resp.content)
        except Exception as e:
            log.error("Upstream error: %s", e)
            self.send_error(502, f"Upstream error: {e}")

    def _proxy_stream(self, body: bytes):
        """Streaming proxy for SSE responses (chat completions with stream=true)."""
        try:
            with _upstream_client.stream(
                "POST",
                self.path,
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

    def _proxy_chat_with_retry(
        self, body: bytes, is_stream: bool, wants_json: bool = False
    ):
        """Proxy a chat completion with automatic retry on transient errors.

        When LM Studio returns a transient error (500 during JIT loading,
        400 model unloaded), waits with backoff and retries.
        """
        grammar_state = {"retries": 0}
        for attempt in range(MAX_RETRIES + 1):
            try:
                if is_stream:
                    result = self._try_stream_with_retry(body, attempt)
                else:
                    result = self._try_request_with_retry(
                        body, attempt, wants_json, grammar_state
                    )

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

    def _try_request_with_retry(
        self,
        body: bytes,
        attempt: int,
        wants_json: bool = False,
        grammar_state=None,  # dict[str,int] | None (py3.9: no PEP 604)
    ) -> str:
        """Non-streaming request with retry check. Returns 'success', 'retry', or 'forwarded'."""
        resp = _upstream_client.request(
            "POST",
            self.path,
            headers=self._forward_headers(),
            content=body,
        )

        if resp.status_code == 200:
            if (
                wants_json
                and grammar_state is not None
                and grammar_state["retries"] < GRAMMAR_SKIP_MAX_RETRIES
                and _json_grammar_skipped(resp.content)
            ):
                grammar_state["retries"] += 1
                log.warning(
                    "structured-output grammar skipped by LM Studio "
                    "(grammar retry %d/%d, attempt %d) -- non-JSON content "
                    "on a json_schema/json_object request; retrying",
                    grammar_state["retries"], GRAMMAR_SKIP_MAX_RETRIES,
                    attempt + 1,
                )
                time.sleep(1.0)
                return "retry"
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
        with _upstream_client.stream(
            "POST",
            self.path,
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
        # Health check endpoint -- responds instantly from cache,
        # never blocks on LM Studio inference.
        if self.path in ("/healthz", "/health"):
            body = get_health_response()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Health-Source", "param-proxy-cache")
            self.end_headers()
            self.wfile.write(body)
            return

        # Prometheus scrape endpoint — additive, served locally. Reachable
        # via Caddy at http://10.20.0.26:11434/proxy-metrics. Best-effort:
        # any fault falls through to the normal upstream proxy path.
        if self.path.split("?", 1)[0] in ("/proxy-metrics", "/metrics"):
            try:
                body = render_family_metrics()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            except Exception as e:
                log.error("metrics render failed: %s", e)

        log.debug("GET %s", self.path)
        self._proxy_simple("GET")

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b""

        is_chat = "/chat/completions" in self.path
        is_stream = False
        family = "nosys"
        wants_json = False

        if is_chat and body:
            try:
                data = json.loads(body)
                is_stream = data.get("stream", False)
                try:
                    family = prompt_family(data)
                except Exception:
                    family = "nosys"
                _rf = data.get("response_format")
                wants_json = isinstance(_rf, dict) and _rf.get("type") in (
                    "json_schema",
                    "json_object",
                )
                data = normalize_qwen_request(data)
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
            _t0 = time.monotonic()
            try:
                self._proxy_chat_with_retry(
                    body, is_stream, wants_json and not is_stream
                )
            finally:
                try:
                    record_family(family, time.monotonic() - _t0)
                except Exception:
                    pass
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
        "Sampling (defaults applied only when omitted; explicit values respected; floor>=%.1f): "
        "no-think=%s  thinking=%s",
        MIN_TEMPERATURE, QWEN3_DEFAULTS[False], QWEN3_DEFAULTS[True],
    )
    log.info(
        "Thinking: enable_thinking=false via chat_template_kwargs / top-level / "
        "Ollama 'think' => <think> prefill (no-think); absent/true => native"
    )
    log.info(
        "Structured-output guard: non-JSON content on a json_schema/json_object "
        "request retries up to %d times (LM Studio grammar-skip mitigation)",
        GRAMMAR_SKIP_MAX_RETRIES,
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
    finally:
        _upstream_client.close()
        log.info("Upstream connection pool closed")


if __name__ == "__main__":
    main()
