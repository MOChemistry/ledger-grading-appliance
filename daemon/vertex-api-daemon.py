#!/usr/bin/env python3
"""
vertex-api-daemon.py  --  The Ledger's local AI broker  (v2.12, multi-provider
+ direct Anthropic API route + multi-image Silo 2 + Google-optional startup
+ vendor-default sampling + Gemini Developer-API route + formatter endpoints
+ batch-scoring endpoints).

[PATCH:batch] v2.12 (WO-B1-2): batch hardening.
1. /api/batch/keepalive (Anthropic direct route only): renews the prompt
   cache while a batch is pending. The request is built by the SAME
   _anthropic_silo2_body call as live and batched requests, so the cached
   prefix is byte-identical; it carries NO student content (master pages
   and the grading instructions only) and asks for max_tokens=0 -- the
   cheapest request the API allows that reads (and re-times) the cache.
   Usage is returned and logged so the GUI can show it registered as a
   cache READ, not a cache write.
2. /api/batch/cleanup now also DELETES an ended Anthropic batch at the
   provider (messages.batches.delete), removing the stored results instead
   of waiting out Anthropic's 29-day retention. The GUI calls cleanup only
   after every student in the job is finished or deferred.
3. The Vertex bucket check works with OBJECT-level permission only
   (Storage Object Admin granted on the one bucket): a 403 on the bucket
   metadata GET falls back to an object list before concluding anything.
4. /api/batch/results reports how many succeeded requests read the stored
   prompt from cache ("reused"), per job, in the totals log line and the
   response payload.

[PATCH:batch] v2.11 (WO-B1): a self-contained batch-scoring endpoint family
(/api/batch/spool, /submit, /status, /results, /cancel, /cleanup) below the
formatter family. Anthropic and Google sell batched requests at half price;
the GUI's SCORING: BATCH mode submits every chunk through these endpoints
and collects results later. Each batched request is built by the SAME code
that builds the live request (_anthropic_silo2_body /
_google_silo2_request_dict), so the two can never drift apart -- cache
markers, output limit and schema instruction included, and still NO
sampling or thinking parameters. Routes: Anthropic direct key -> Message
Batches API; Google AI Studio key -> file-based Gemini batch (Files API);
Google Vertex -> GCS JSONL batch prediction in the bucket
ledger-batch-<project-id> (created on first use, 2-day delete lifecycle;
a service account without permission gets a clear "needs bucket" error).
Claude THROUGH VERTEX cannot batch (no Message Batches on that route) and
says so on the health endpoint, which now carries "batch_endpoints": true
and a per-route "batch" capability map. The daemon keeps no job state --
the GUI holds the job record; only transient per-submission spool files
live here. Truncated batch results are DISCARDED with the live guard's
exact message, on both providers. Token usage is logged per result and
totalled per job; keys and student content are never logged.

[PATCH:formatter] v2.10 (WO-F1): a new, self-contained endpoint family for the
FORMAT ASSIGNMENT window. It changes nothing about the four evaluation
endpoints above it:
  /api/formatter/start   POST {model_name, system, messages} -> {status, job_id}
                         (returns immediately; the provider call runs in a
                         background thread; ONE formatter job at a time)
  /api/formatter/status  GET ?job_id=... -> {state: running|done|error,
                         text, usage, message}
Routing is by _provider_for(model_name) exactly as the other endpoints.
Anthropic: the system text rides as a cache_control system block and the
call is STREAMED (long outputs exceed the non-streaming SDK window).
Google: the system text rides as system_instruction. The output limit is
LEDGER_FORMATTER_MAX_TOKENS (default 32000), separate from the evaluation
limit; a reply cut off at the limit returns an error, never a truncated
script. Token usage (input / cache write / cache read / output) is logged
per call; keys and assignment content are never logged. The daemon still
sends NO sampling or thinking parameters on this route.
[PATCH:formatter] v2.10 also adds the LEDGER_PORT test seam next to
LEDGER_ENV_PATH: tests run a second copy of this daemon on a spare port as
the logged-in user. Production never sets it; the systemd unit carries no
such variable.

[PATCH:google-devapi] v2.9 (WO-A7): the Google route can be provisioned with a
Google AI Studio API key (LEDGER_GOOGLE_API_KEY, "AIza...") INSTEAD of a GCP
project + service account. The key builds genai.Client(api_key=...) against
the Gemini Developer API; everything downstream (caches.create, inline
fallback, usage logging, silo1/silo2) is client-agnostic. If BOTH the key and
GCP_PROJECT_ID are present the key wins and a WARNING is logged -- one Google
route per box. The key is never written to any log line.

[PATCH:no-sampling-params] WO-A2: the daemon sends NO sampling or thinking
parameters (temperature, top_p, top_k, thinking budgets) to any provider, on
any path -- every model runs at its vendor defaults. A "temperature" field in
an incoming /api/silo2 body (the GUI still sends one) is accepted and ignored.

[PATCH:google-optional] v2.4: the Google/Vertex stack is no longer mandatory.
A district may initialize with (a) Google credentials, (b) an Anthropic API
key, or (c) both. The daemon refuses to start only when NEITHER provider is
configured. When GCP_PROJECT_ID is absent, the Vertex client is never
constructed, and any request routed to a Google model returns a clear
GUI-surfaced error instead of crashing the service.

The grading GUI (ledger-main-minimus.py) never talks to a provider directly. It
POSTs to this loopback service at http://127.0.0.1:5000, which brokers every
call. Routing is by the model id the GUI sends:

  * Google models  (gemini-*, meta/llama-*, qwen-*, any 'publishers/...' id)
        -> the unified google-genai SDK  (unchanged from v1 / Appendix A)
  * Anthropic models (claude-*, 'anthropic/...', 'publishers/anthropic/...')
        -> [PATCH:direct-api] the DIRECT Anthropic API (anthropic.Anthropic)
           when LEDGER_ANTHROPIC_API_KEY is set in /etc/paragon/.env;
           otherwise the anthropic[vertex] SDK (AnthropicVertex), exactly as
           v2.x. Same messages.create call either way; the GUI cannot tell
           the difference.

[PATCH:multi-image] /api/silo2 additionally accepts an OPTIONAL "images_b64"
array (essay / FULL_PAGE mode: one entry per student response page, in page
order). When present it wins; the legacy single "image_b64" behaves exactly
as before, so the boxed-grid pipeline is byte-path identical. On the Google
path each page becomes its own Part (Gemini tiles each at full resolution);
on the Anthropic path each page becomes its own image block (each page keeps
its own resolution budget instead of sharing one across a tall stitched
stack).

The four-endpoint contract the GUI speaks is IDENTICAL to v1 -- no GUI network
code changes. Caching is abstracted per provider behind that contract:
  * Google    : a server-side context cache (client.caches.create), reused by name.
  * Anthropic : there is no server-side named cache; the daemon remembers the
                system prompt + master images locally and re-sends them as
                cache_control:"ephemeral" blocks on every grade (Anthropic prompt
                caching, 5-min lifetime refreshed on each hit).

Endpoints (all POST, JSON in / JSON out):
  /api/silo1        {model_name, prompt, image_b64}                 -> one stateless vision call
  /api/cache/build  {model_name, system_instruction, images_b64[]} -> establish the grading context
  /api/cache/delete (no body)                                       -> purge the active context
  /api/silo2        {prompt, image_b64 | images_b64[], schema}  ("temperature" ignored)
                                                                     -> grade grid/pages against the context

Success envelope : {"status": "success", "text": "<string>"}
Failure          : HTTP 500 + {"status": "error", "message": "<reason>"}

Config is read from /etc/paragon/.env (root-owned, 0640 root:paragon so the
paragon-run daemon can read it):
  GCP_PROJECT_ID="..."                                      (required for the Google route;
                                                             [PATCH:google-optional] may be
                                                             omitted when LEDGER_ANTHROPIC_API_KEY
                                                             is set -- at least ONE of the two
                                                             must be present)
  GCP_LOCATION="global"                                     (optional; default "global")
  GOOGLE_APPLICATION_CREDENTIALS="/etc/paragon/sa.json"     (optional; service-account key)
  LEDGER_DEFAULT_MODEL="gemini-3.5-flash"                   (optional fallback model)
  ANTHROPIC_VERTEX_REGION="global"                          (optional; region for Claude;
                                                             "global", a region like "us-east5",
                                                             or a multi-region "us"/"eu".
                                                             Falls back to GCP_LOCATION.)
  LEDGER_ANTHROPIC_MAX_TOKENS="8192"                        (optional; Claude max output tokens)
  LEDGER_ANTHROPIC_API_KEY="sk-ant-..."                     (optional; when present, Claude
                                                             routes to the DIRECT Anthropic API
                                                             instead of Vertex. Leave unset or
                                                             empty to keep the Vertex route.)
  LEDGER_GOOGLE_API_KEY="AIza..."                           (optional; [PATCH:google-devapi]
                                                             Google AI Studio key -> Gemini
                                                             Developer API. Wins over
                                                             GCP_PROJECT_ID when both are set.)

Auth uses Application Default Credentials -- the SAME service-account JSON serves
both providers. The Anthropic path stays fully DORMANT until (a) the anthropic
SDK is installed in the venv AND (b) a claude-* model id is selected, so a box
with only google-genai installed runs Gemini exactly as before.
"""

import os
import base64
import logging
import threading

from flask import Flask, request, jsonify
from waitress import serve

from google import genai
from google.genai import types
from google.genai.errors import APIError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# [PATCH:google-devapi] LEDGER_ENV_PATH is a test seam (tests/ points it at a
# temp file so the startup env-matrix can run without touching /etc/paragon).
# Production never sets it; the systemd unit carries no such variable.
ENV_PATH = os.environ.get("LEDGER_ENV_PATH") or "/etc/paragon/.env"
HOST = "127.0.0.1"
# [PATCH:formatter] LEDGER_PORT is a test seam like LEDGER_ENV_PATH above:
# tests run a branch copy of this daemon on a spare port as the logged-in
# user. Production never sets it; the systemd unit carries no such variable.
try:
    PORT = int(os.environ.get("LEDGER_PORT") or "5000")
except ValueError:
    PORT = 5000
CACHE_TTL = "43200s"                  # 12 hours, matching the GUI's note (Google path)
FALLBACK_MODEL = "gemini-4-argon"   # current Gemini flagship (GA 2026-10-13)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [ledger-daemon] %(levelname)s: %(message)s",
)
log = logging.getLogger("ledger-daemon")


# ---------------------------------------------------------------------------
# .env loading  (no python-dotenv dependency; tiny hand-rolled parser)
# ---------------------------------------------------------------------------
def load_env(path):
    """Read KEY=VALUE / KEY="VALUE" lines, ignore blanks and '#' comments, and
    push every value into os.environ so the SDKs can pick up
    GOOGLE_APPLICATION_CREDENTIALS automatically."""
    if not os.path.exists(path):
        raise RuntimeError(
            f"{path} not found. Run the provisioning script (or the setup wizard) first."
        )
    values = {}
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            values[key] = val
            os.environ.setdefault(key, val)
    return values


ENV = load_env(ENV_PATH)
PROJECT_ID = ENV.get("GCP_PROJECT_ID")
LOCATION = ENV.get("GCP_LOCATION") or "global"
DEFAULT_MODEL = ENV.get("LEDGER_DEFAULT_MODEL") or FALLBACK_MODEL
ANTHROPIC_REGION = ENV.get("ANTHROPIC_VERTEX_REGION") or LOCATION
try:
    ANTHROPIC_MAX_TOKENS = int(ENV.get("LEDGER_ANTHROPIC_MAX_TOKENS") or "8192")
except ValueError:
    ANTHROPIC_MAX_TOKENS = 8192

# [PATCH:google-optional] Startup rule: at least ONE provider must be
# configured. [PATCH:google-devapi] Google is configured by GCP_PROJECT_ID OR
# LEDGER_GOOGLE_API_KEY; the direct Anthropic route is configured by
# LEDGER_ANTHROPIC_API_KEY. (The Anthropic key is also resolved again below
# for route selection -- this early peek exists only so the refusal-to-start
# check can run before any client construction.)
_GOOGLE_API_KEY = (ENV.get("LEDGER_GOOGLE_API_KEY") or "").strip()
_HAS_GOOGLE = bool(PROJECT_ID) or bool(_GOOGLE_API_KEY)
_HAS_ANTHROPIC_KEY = bool((ENV.get("LEDGER_ANTHROPIC_API_KEY") or "").strip())
if not _HAS_GOOGLE and not _HAS_ANTHROPIC_KEY:
    raise RuntimeError(
        "No AI provider is configured in /etc/paragon/.env. Set "
        "LEDGER_GOOGLE_API_KEY (Google AI Studio key), GCP_PROJECT_ID "
        "(Google Vertex route), and/or LEDGER_ANTHROPIC_API_KEY (direct "
        "Anthropic route), then restart ledger-daemon.service."
    )

# [PATCH:google-optional] On an Anthropic-only box the shipped Gemini fallback
# model would be undispatchable; fall back to a Claude model instead so a GUI
# request that omits model_name still lands on a live route. A box with Google
# configured keeps the exact v2.x fallback behavior.
if not _HAS_GOOGLE and not ENV.get("LEDGER_DEFAULT_MODEL"):
    DEFAULT_MODEL = "claude-sonnet-5-5"

# [PATCH:google-devapi] Which Google route this box is using, surfaced on the
# health endpoint: "developer-api" (AI Studio key) | "vertex" | "absent".
if _HAS_GOOGLE:
    # [PATCH:upstream-timeout] bound every Google call so a wedged client cannot hang forever.
    # Override via LEDGER_GOOGLE_TIMEOUT_MS in /etc/paragon/.env (milliseconds). Default 300000 = 5 min.
    _LEDGER_GOOGLE_TIMEOUT_MS = int(ENV.get("LEDGER_GOOGLE_TIMEOUT_MS") or "300000")
    if _GOOGLE_API_KEY:
        # Same hygiene as the Anthropic key: the key value itself is NEVER
        # logged -- only the fact that key-based auth is in use.
        if PROJECT_ID:
            log.warning("Both LEDGER_GOOGLE_API_KEY and GCP_PROJECT_ID are set -- "
                        "the AI Studio key wins; the Vertex project (%s) is ignored. "
                        "One Google route per box.", PROJECT_ID)
        log.info("Initialising Gemini Developer-API (google-genai) client (key-based auth).")
        client = genai.Client(api_key=_GOOGLE_API_KEY,
                              http_options=types.HttpOptions(timeout=_LEDGER_GOOGLE_TIMEOUT_MS))
        _google_route = "developer-api"
    else:
        log.info("Initialising Vertex (google-genai) client (project=%s, location=%s)",
                 PROJECT_ID, LOCATION)
        client = genai.Client(vertexai=True, project=PROJECT_ID, location=LOCATION,
                              http_options=types.HttpOptions(timeout=_LEDGER_GOOGLE_TIMEOUT_MS))
        _google_route = "vertex"
    # [PATCH:upstream-timeout-end]
else:
    # [PATCH:google-optional] Anthropic-only box: the Google client is never
    # constructed. Every Google-path caller goes through _require_google(),
    # which converts this None into a clear, GUI-surfaced error message.
    client = None
    _google_route = "absent"
    log.info("No Google credentials -- Google route is DORMANT on this box.")


def _require_google():
    """[PATCH:google-optional] Gate for every Google-path call site. Returns the
    live Vertex client, or raises a plain-language error that the standard
    endpoint handlers surface to the GUI as {"status":"error", ...}."""
    if client is None:
        raise RuntimeError(
            "This Ledger is initialized for the Anthropic route only -- no Google "
            "credentials are installed. Select a claude-* model in AI MODEL, or "
            "re-initialize with Google credentials to use Gemini models."
        )
    return client

# The Anthropic client is created lazily on first use (see _anthropic_client),
# so a box without the anthropic SDK installed still serves Gemini normally.
_anthropic_client = None
_anthropic_lock = threading.Lock()
# [PATCH:direct-api] Which Claude route this box is using: "direct-api" when
# LEDGER_ANTHROPIC_API_KEY is present, else "vertex". Resolved once, at client
# construction, and surfaced on the health endpoint for field diagnosis.
# [PATCH:google-devapi] "absent" when neither a key nor a GCP project exists
# (AI-Studio-key-only box): no Claude route can serve, so say so on /.
_ANTHROPIC_API_KEY = (ENV.get("LEDGER_ANTHROPIC_API_KEY") or "").strip()
_anthropic_route = ("direct-api" if _ANTHROPIC_API_KEY
                    else "vertex" if PROJECT_ID else "absent")


def _get_anthropic_client():
    """Construct (once) and return the Claude client for this box's route:
    the DIRECT Anthropic API when LEDGER_ANTHROPIC_API_KEY is set, otherwise
    AnthropicVertex (unchanged v2.x behavior). Both speak the identical
    messages.create contract, so every caller downstream is route-agnostic.
    Raises a clear, GUI-surfaced error if the SDK is not installed."""
    global _anthropic_client
    with _anthropic_lock:
        if _anthropic_client is None:
            # [PATCH:upstream-timeout-anthropic] mirror the bounded timeout on the Claude path.
            # Override via LEDGER_ANTHROPIC_TIMEOUT_S in /etc/paragon/.env (seconds). Default 300.
            _timeout = float(ENV.get("LEDGER_ANTHROPIC_TIMEOUT_S") or "300")
            if _ANTHROPIC_API_KEY:
                try:
                    from anthropic import Anthropic
                except ImportError as exc:
                    raise RuntimeError(
                        "Anthropic models require the 'anthropic' SDK, which is not "
                        "installed in this venv. Install it (pip install anthropic) "
                        "and restart ledger-daemon, or choose a Gemini model."
                    ) from exc
                log.info("Initialising DIRECT Anthropic API client (key-based auth).")
                _anthropic_client = Anthropic(
                    api_key=_ANTHROPIC_API_KEY,
                    timeout=_timeout,
                )
            else:
                # [PATCH:google-devapi] An AI-Studio-key-only box has no GCP
                # project, so AnthropicVertex cannot authenticate. Fail with
                # a GUI-surfaced message instead of a null-project crash.
                if not PROJECT_ID:
                    raise RuntimeError(
                        "Claude models need an Anthropic API key on this box (no "
                        "Google Cloud project is configured, so the Vertex route "
                        "for Claude is unavailable). Add LEDGER_ANTHROPIC_API_KEY "
                        "to /etc/paragon/.env and restart ledger-daemon.service, "
                        "or choose a Gemini model."
                    )
                try:
                    from anthropic import AnthropicVertex
                except ImportError as exc:
                    raise RuntimeError(
                        "Anthropic models require the 'anthropic[vertex]' SDK, which is not "
                        "installed in this venv. Install it (pip install 'anthropic[vertex]') "
                        "and restart ledger-daemon, or choose a Gemini model."
                    ) from exc
                log.info("Initialising AnthropicVertex client (project=%s, region=%s)",
                         PROJECT_ID, ANTHROPIC_REGION)
                _anthropic_client = AnthropicVertex(
                    project_id=PROJECT_ID, region=ANTHROPIC_REGION,
                    timeout=_timeout,
                )
            # [PATCH:upstream-timeout-anthropic-end]
        return _anthropic_client


def _provider_for(model_name):
    """Decide which SDK a model id belongs to. Anthropic Claude ids on Vertex
    look like 'claude-opus-4-8', 'anthropic/claude-...', or
    'publishers/anthropic/models/claude-...'. Everything else (gemini-*,
    meta/llama-*, qwen-*, bare 'publishers/...') goes through google-genai."""
    m = (model_name or "").lower()
    if m.startswith("claude") or "anthropic/" in m or m.startswith("publishers/anthropic"):
        return "anthropic"
    return "google"


# ---------------------------------------------------------------------------
# Shared context state  (one active grading context at a time)
#   Google    : name = the Vertex cache resource name
#   Anthropic : name = the sentinel "anthropic-local"; the actual reusable
#               material is held in _anthropic_ctx and re-sent each grade.
# ---------------------------------------------------------------------------
_cache_lock = threading.Lock()
_active_cache = {"name": None, "model": None}
_anthropic_ctx = {"system": "", "image_bytes": []}   # raw PNG bytes for master pages
_ANTHROPIC_SENTINEL = "anthropic-local"
# Google inline fallback: when a master is below the model's minimum-cache-token
# floor, caches.create is rejected (400 INVALID_ARGUMENT). We then hold the
# master locally and inline it into each grade -- no cache discount, but small
# masters (e.g. short/single-page demos) still grade. Large masters still cache.
_google_ctx = {"system": "", "image_bytes": []}      # raw PNG bytes for master pages
_GOOGLE_INLINE = "google-inline"


def _purge_active_cache():
    """Drop the active grading context. For Google, delete the server-side cache.
    For Anthropic, just clear the locally-held material."""
    with _cache_lock:
        name = _active_cache["name"]
        model = _active_cache["model"]
        _active_cache["name"] = None
        _active_cache["model"] = None
        _anthropic_ctx["system"] = ""
        _anthropic_ctx["image_bytes"] = []
        _google_ctx["system"] = ""
        _google_ctx["image_bytes"] = []
    if name and name not in (_ANTHROPIC_SENTINEL, _GOOGLE_INLINE):
        # [PATCH:google-optional] a named Google cache cannot exist on a box
        # with no Vertex client, but guard anyway rather than crash a purge.
        if client is None:
            log.warning("Skipping delete of cache %s: no Google client on this box.", name)
            return
        try:
            client.caches.delete(name=name)
            log.info("Deleted Google context cache: %s", name)
        except APIError as exc:
            log.warning("Could not delete cache %s: %s", name, exc)
    elif name == _ANTHROPIC_SENTINEL:
        log.info("Cleared Anthropic local grading context (model=%s).", model)
    elif name == _GOOGLE_INLINE:
        log.info("Cleared Google inline grading context (model=%s).", model)


def _is_cache_too_small(exc):
    """True only when a caches.create failure is the minimum-token-floor
    rejection -- the one case we handle by falling back to an inline context.
    Any other error is a real failure and must keep propagating."""
    msg = str(getattr(exc, "message", "") or exc).lower()
    return "minimum token count" in msg or "to start caching" in msg


def _is_explicit_cache_unsupported(exc):
    """[PATCH:implicit-cache-only] True when caches.create is rejected because
    the model only supports IMPLICIT caching (Gemini 4 Argon and onward).
    Both wordings seen in the field: Vertex says 'Model `x` does not support
    explicit context caching.'; AI Studio says 'Explicit caching is not
    supported for models/x'. Match narrowly -- any other error must propagate."""
    msg = str(getattr(exc, "message", "") or exc).lower()
    return ("does not support explicit context caching" in msg
            or "explicit caching is not supported" in msg)


def _log_google_usage(resp, cache_name, per_call_ctx):
    """[PATCH:google-usage-log] The only way to SEE whether Vertex caching
    (explicit or implicit) is working. cached > 0 on every call after the
    first per distinct prefix means the discount is landing. Never raises."""
    try:
        u = getattr(resp, "usage_metadata", None)
        if per_call_ctx:
            path = "per-chunk"
        elif cache_name == _GOOGLE_INLINE:
            path = "inline"
        else:
            path = "cached"
        finish = None
        cands = getattr(resp, "candidates", None) or []
        if cands:
            finish = getattr(cands[0], "finish_reason", None)
        log.info("Google usage: path=%s prompt=%s cached=%s thoughts=%s output=%s finish=%s",
                 path,
                 getattr(u, "prompt_token_count", "?"),
                 getattr(u, "cached_content_token_count", None),
                 getattr(u, "thoughts_token_count", None),
                 getattr(u, "candidates_token_count", "?"),
                 finish)
    except Exception:  # noqa: BLE001 - logging must never kill a grade
        pass


def _decode_png(image_b64):
    """base64 PNG string (with or without a data: URI prefix) -> raw bytes."""
    if not image_b64:
        raise ValueError("image_b64 is empty")
    if image_b64.strip().startswith("data:") and "," in image_b64:
        image_b64 = image_b64.split(",", 1)[1]
    return base64.b64decode(image_b64)


def _anthropic_image_block(png_bytes, cache=False):
    """Build an Anthropic image content block from raw PNG bytes."""
    block = {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": _anthropic_media_type(png_bytes),
            "data": base64.b64encode(png_bytes).decode("ascii"),
        },
    }
    if cache:
        block["cache_control"] = _anthropic_cache_control()
    return block



# [PATCH:payload-governor] Anthropic-route image normalization.
# The Anthropic Messages API caps a request at ~32 MB and server-side
# downscales any image over ~1568 px on the long edge. The master pages
# arrive from the GUI at Matrix(4.16) (~2550x3300 PNG) and are RE-SENT on
# every grade on this route, so a full-booklet master plus a 17-page essay
# packet blew straight past the cap and Anthropic rejected the request
# (HTTP 413) before the model ever saw it. Normalizing here -- and only
# here -- keeps the Google/Gemini path byte-identical while making the
# Anthropic payload ~4-6x smaller with zero fidelity the model could have
# used anyway. Requires Pillow in the daemon venv; falls back to
# passthrough (old behavior) with a loud warning if it is missing.
try:
    _ANTHROPIC_MAX_EDGE = int(ENV.get("LEDGER_ANTHROPIC_MAX_EDGE") or "1568")
except ValueError:
    _ANTHROPIC_MAX_EDGE = 1568
try:
    _ANTHROPIC_JPEG_QUALITY = int(ENV.get("LEDGER_ANTHROPIC_JPEG_QUALITY") or "85")
except ValueError:
    _ANTHROPIC_JPEG_QUALITY = 85
_ANTHROPIC_PAYLOAD_CEILING = 30_000_000  # bytes of base64 image data; API cap is ~32 MB total

# [PATCH:cache-ttl] Prompt-cache lifetime for every cache_control breakpoint
# on the Anthropic route. "1h" (default) or "5m". 1h costs 2x on the write
# but survives the multi-minute gaps between students; 5m costs 1.25x and
# expired between calls in real batches, paying the write premium with no
# reads. Set LEDGER_ANTHROPIC_CACHE_TTL=5m in /etc/paragon/.env to revert.
_ANTHROPIC_CACHE_TTL = (ENV.get("LEDGER_ANTHROPIC_CACHE_TTL") or "1h").strip().lower()
if _ANTHROPIC_CACHE_TTL not in ("1h", "5m"):
    log.warning("LEDGER_ANTHROPIC_CACHE_TTL=%r is not 1h or 5m -- using 1h.", _ANTHROPIC_CACHE_TTL)
    _ANTHROPIC_CACHE_TTL = "1h"


def _anthropic_cache_control():
    """One cache_control dict, honoring the configured TTL."""
    cc = {"type": "ephemeral"}
    if _ANTHROPIC_CACHE_TTL == "1h":
        cc["ttl"] = "1h"
    return cc


def _anthropic_media_type(image_bytes):
    """Sniff JPEG vs PNG from magic bytes so normalized (JPEG) and
    passthrough (PNG) images both carry an honest media_type."""
    if image_bytes[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    return "image/png"


def _anthropic_normalize_image(image_bytes):
    """Downscale to <=_ANTHROPIC_MAX_EDGE on the long edge and re-encode as
    JPEG q=_ANTHROPIC_JPEG_QUALITY. Anthropic route ONLY."""
    try:
        import io as _io
        from PIL import Image as _PILImage
    except ImportError:
        log.warning("Pillow is not installed in the daemon venv -- Anthropic "
                    "images sent at ORIGINAL size. Large essay packets may "
                    "exceed the 32 MB request cap. Fix: pip install pillow "
                    "in the daemon venv, then restart ledger-daemon.")
        return image_bytes
    try:
        img = _PILImage.open(_io.BytesIO(image_bytes))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        long_edge = max(img.size)
        if _ANTHROPIC_MAX_EDGE > 0 and long_edge > _ANTHROPIC_MAX_EDGE:
            scale = _ANTHROPIC_MAX_EDGE / float(long_edge)
            img = img.resize((max(1, int(img.width * scale)),
                              max(1, int(img.height * scale))),
                             _PILImage.LANCZOS)
        out = _io.BytesIO()
        img.save(out, format="JPEG", quality=_ANTHROPIC_JPEG_QUALITY)
        return out.getvalue()
    except Exception as exc:  # noqa: BLE001 - never let normalization kill a grade
        log.warning("Anthropic image normalization failed (%s); sending "
                    "original bytes.", exc)
        return image_bytes


def _anthropic_text(message):
    """Concatenate the text blocks of an Anthropic Messages response."""
    parts = []
    for block in getattr(message, "content", None) or []:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts)


# [PATCH:batch] v2.11 (WO-B1): the ONE builder for a Silo 2 Anthropic request
# body. The live /api/silo2 path and the batch submit path both call it, so a
# batched request is byte-identical to the live request by construction (and
# tests/test_batch_identical.py proves it by hashing both).
def _anthropic_silo2_body(system_text, master_bytes_list, live_bytes_list,
                          prompt, schema):
    """Returns (system_blocks, content) exactly as the live path sends them:
    cache_control'd system block; master images first with the LAST one cache
    marked; live images; then the per-call prompt + schema instruction."""
    system_blocks = [{
        "type": "text",
        "text": system_text or "",
        "cache_control": _anthropic_cache_control(),
    }]
    content = []
    last = len(master_bytes_list) - 1
    for i, b in enumerate(master_bytes_list):
        # [PATCH:cache-per-call-masters] cache the last master block in
        # BOTH modes. Per-call (S2CTX) masters differ chunk-to-chunk but
        # are identical for the same chunk across every student, so each
        # distinct master set is written once and read on every later
        # student. The old rule left the bulk of the payload uncached.
        content.append(_anthropic_image_block(b, cache=(i == last)))
    # [PATCH:multi-image] one block per live page, in page order
    for b in live_bytes_list:
        content.append(_anthropic_image_block(b))
    content.append({
        "type": "text",
        "text": f"{prompt}\n\n{_schema_instructions(schema)}",
    })
    return system_blocks, content


# [PATCH:batch] The Google inline-request twin pair. Both derive from the
# same sequence so the SDK (live) and REST-dict (batch JSONL) forms carry the
# same content in the same order: masters, live pages, prompt; plus the same
# system instruction and response schema.
def _google_silo2_parts_and_config(system_text, master_bytes_list,
                                   live_bytes_list, prompt, schema):
    """SDK form for the live inline/per-chunk paths."""
    contents = [types.Part.from_bytes(data=b, mime_type="image/png")
                for b in master_bytes_list]
    contents += [types.Part.from_bytes(data=b, mime_type="image/png")
                 for b in live_bytes_list]
    contents.append(prompt)
    cfg_kwargs = {}
    if system_text:
        cfg_kwargs["system_instruction"] = system_text
    if schema:
        cfg_kwargs["response_mime_type"] = "application/json"
        cfg_kwargs["response_schema"] = schema
    return contents, cfg_kwargs


def _google_silo2_request_dict(system_text, master_bytes_list,
                               live_bytes_list, prompt, schema):
    """REST form for a batch JSONL line: same content as the SDK form."""
    parts = [{"inlineData": {"mimeType": "image/png",
                             "data": base64.b64encode(b).decode("ascii")}}
             for b in master_bytes_list]
    parts += [{"inlineData": {"mimeType": "image/png",
                              "data": base64.b64encode(b).decode("ascii")}}
              for b in live_bytes_list]
    parts.append({"text": prompt})
    req = {"contents": [{"role": "user", "parts": parts}]}
    if system_text:
        req["systemInstruction"] = {"parts": [{"text": system_text}]}
    if schema:
        req["generationConfig"] = {"responseMimeType": "application/json",
                                   "responseSchema": schema}
    return req


def _truncation_message():
    """[PATCH:batch] The truncation-guard wording, shared by the live silo2
    path and the batch results path so a truncated batched result is
    discarded with the identical, GUI-classified message."""
    return (
        "invalid_request: model output was TRUNCATED at the "
        f"{ANTHROPIC_MAX_TOKENS}-token output ceiling before the "
        "evaluation JSON was complete. The partial evaluation was "
        "DISCARDED -- a truncated evaluation must never be "
        "committed. Raise LEDGER_ANTHROPIC_MAX_TOKENS in "
        "/etc/paragon/.env and restart ledger-daemon.service.")


def _schema_instructions(schema):
    """Turn the GUI's Vertex-style schema into a plain-language JSON instruction
    for Claude (which doesn't take a Gemini response_schema). The GUI already
    strips ```json fences and json_repair-parses, so a firm instruction suffices."""
    base = (
        "Respond with ONLY a JSON array and nothing else -- no prose, no "
        "explanation, no markdown code fences. "
    )
    try:
        items = (schema or {}).get("items", {})
        props = items.get("properties", {})
        if props:
            keys = ", ".join(props.keys())
            base += f"Each array element must be a JSON object with exactly these keys: {keys}."
    except Exception:  # noqa: BLE001 - schema hinting is best-effort
        pass
    return base


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)


@app.get("/")
def health():
    with _cache_lock:
        cache_active = _active_cache["name"] is not None
        cache_model = _active_cache["model"]
    anthropic_available = False
    try:
        import anthropic  # noqa: F401
        anthropic_available = True
    except ImportError:
        anthropic_available = False
    return jsonify({
        "status": "ok",
        "project": PROJECT_ID,
        "location": LOCATION,
        "default_model": DEFAULT_MODEL,
        "anthropic_region": ANTHROPIC_REGION,
        "anthropic_route": _anthropic_route,   # [PATCH:direct-api] "direct-api" | "vertex"
        "google_route": _google_route,   # [PATCH:google-devapi] "developer-api" | "vertex" | "absent"
        "anthropic_sdk_installed": anthropic_available,
        "cache_active": cache_active,
        "cache_model": cache_model,
        "per_chunk_context": True,   # [PATCH:silo2-per-chunk-context] GUI capability gate
        "batch_endpoints": True,     # [PATCH:batch] GUI capability gate (WO-B1)
        "batch_keepalive": True,     # [PATCH:batch] WO-B1-2: cache keep-alive
        "batch": _batch_capabilities(),  # [PATCH:batch] per-route support + reason
    })


# ---------------------------------------------------------------------------
# /api/silo1  -- one stateless vision call (title / student-ID OCR)
# ---------------------------------------------------------------------------
@app.post("/api/silo1")
def silo1():
    try:
        body = request.get_json(force=True) or {}
        model_name = body.get("model_name") or DEFAULT_MODEL
        prompt = body.get("prompt", "")
        png = _decode_png(body.get("image_b64"))

        if _provider_for(model_name) == "anthropic":
            ac = _get_anthropic_client()
            msg = ac.messages.create(
                model=model_name,
                max_tokens=ANTHROPIC_MAX_TOKENS,
                messages=[{
                    "role": "user",
                    "content": [
                        _anthropic_image_block(png),
                        {"type": "text", "text": prompt},
                    ],
                }],
            )
            return jsonify({"status": "success", "text": _anthropic_text(msg)})

        # Google path (unchanged from v1)
        image = types.Part.from_bytes(data=png, mime_type="image/png")
        resp = _require_google().models.generate_content(
            model=model_name,
            contents=[image, prompt],
        )
        return jsonify({"status": "success", "text": resp.text or ""})
    except Exception as exc:  # noqa: BLE001 - surface every failure to the GUI
        log.exception("silo1 failed")
        return jsonify({"status": "error", "message": str(exc)}), 500


# ---------------------------------------------------------------------------
# /api/cache/build  -- establish the grading context (system prompt + masters)
# ---------------------------------------------------------------------------
@app.post("/api/cache/build")
def cache_build():
    try:
        body = request.get_json(force=True) or {}
        model_name = body.get("model_name") or DEFAULT_MODEL
        system_instruction = body.get("system_instruction", "")
        images_b64 = body.get("images_b64") or []

        # Never stack contexts: drop any prior one first.
        _purge_active_cache()

        if _provider_for(model_name) == "anthropic":
            # No server-side named cache for Anthropic: hold the material locally
            # and re-send it (cache_control'd) on every grade.
            _get_anthropic_client()  # validate SDK presence now, fail early if missing
            # [PATCH:payload-governor] masters normalized ONCE here; every
            # grade re-sends them, so this is where the savings multiply.
            img_bytes = [_anthropic_normalize_image(_decode_png(b)) for b in images_b64]
            with _cache_lock:
                _anthropic_ctx["system"] = system_instruction
                _anthropic_ctx["image_bytes"] = img_bytes
                _active_cache["name"] = _ANTHROPIC_SENTINEL
                _active_cache["model"] = model_name
            log.info("Anthropic grading context staged (model=%s, %d master image(s)).",
                     model_name, len(img_bytes))
            return jsonify({"status": "success", "text": _ANTHROPIC_SENTINEL})

        # Google path. Try a server-side context cache; if the master is below
        # the model's minimum-cache-token floor, fall back to an inline context
        # rather than failing the whole batch. Real multi-page masters still cache.
        img_bytes = [_decode_png(b) for b in images_b64]
        parts = [types.Part.from_bytes(data=b, mime_type="image/png") for b in img_bytes]
        contents = [types.Content(role="user", parts=parts)] if parts else None
        try:
            cache = _require_google().caches.create(
                model=model_name,
                config=types.CreateCachedContentConfig(
                    system_instruction=system_instruction,
                    contents=contents,
                    ttl=CACHE_TTL,
                ),
            )
        except APIError as exc:
            too_small = _is_cache_too_small(exc)
            implicit_only = _is_explicit_cache_unsupported(exc)
            if not (too_small or implicit_only):
                raise
            with _cache_lock:
                _google_ctx["system"] = system_instruction
                _google_ctx["image_bytes"] = img_bytes
                _active_cache["name"] = _GOOGLE_INLINE
                _active_cache["model"] = model_name
            if implicit_only:
                # [PATCH:implicit-cache-only] Argon-class model: no explicit
                # cache exists. Masters ride inline at the FRONT of every
                # call, where Vertex's automatic implicit cache picks them
                # up (>= 4,096-token prefix). Watch 'Google usage: cached='.
                log.info(
                    "Model is implicit-cache-only -- using inline context "
                    "(model=%s, %d master image(s)). Vertex said: %s",
                    model_name, len(parts), exc,
                )
            else:
                log.warning(
                    "Master below cache-token floor -- using inline context "
                    "(model=%s, %d master image(s)). Vertex said: %s",
                    model_name, len(parts), exc,
                )
            return jsonify({"status": "success", "text": _GOOGLE_INLINE})
        with _cache_lock:
            _active_cache["name"] = cache.name
            _active_cache["model"] = model_name
            # [PATCH:silo2-per-chunk-context] keep the system prompt locally too:
            # per-call master-context grades bypass the server cache and need it
            # (until now _google_ctx was only populated on the inline fallback).
            _google_ctx["system"] = system_instruction
            # [PATCH:batch] keep the master bytes too: batched Google requests
            # always carry the masters INLINE (batch cannot read a server
            # cache, and Google's batch and cache discounts do not combine),
            # so the full-master fallback needs them even on the cached path.
            _google_ctx["image_bytes"] = img_bytes
        log.info("Google context cache built: %s (model=%s, %d master image(s))",
                 cache.name, model_name, len(parts))
        return jsonify({"status": "success", "text": cache.name})
    except Exception as exc:  # noqa: BLE001
        log.exception("cache/build failed")
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.post("/api/cache/delete")
def cache_delete():
    """Purge the active grading context. (The GUI ignores the response body.)"""
    try:
        _purge_active_cache()
        return jsonify({"status": "success", "text": "context purged"})
    except Exception as exc:  # noqa: BLE001
        log.exception("cache/delete failed")
        return jsonify({"status": "error", "message": str(exc)}), 500


# ---------------------------------------------------------------------------
# /api/silo2  -- grade the stitched grid against the active context
#   Note: the silo2 payload carries NO model_name, so we reuse the model the
#   context was built with -- a Google cache can only be consumed by its own
#   model, and the Anthropic material was staged for one model too.
# ---------------------------------------------------------------------------
@app.post("/api/silo2")
def silo2():
    try:
        body = request.get_json(force=True) or {}
        # [PATCH:no-sampling-params] body["temperature"] is deliberately unread.
        prompt = body.get("prompt", "")
        schema = body.get("schema")
        # [PATCH:multi-image] Essay / FULL_PAGE mode sends "images_b64" (one
        # entry per student response page, in page order). When present it
        # wins; otherwise the legacy single "image_b64" is decoded exactly as
        # before, so the boxed-grid pipeline is untouched.
        imgs_b64 = body.get("images_b64")
        if imgs_b64:
            live_pngs = [_decode_png(b) for b in imgs_b64]
            if not live_pngs:
                raise ValueError("images_b64 is present but empty")
        else:
            live_pngs = [_decode_png(body.get("image_b64"))]
        # [PATCH:silo2-per-chunk-context] OPTIONAL per-call master context. When
        # "master_images_b64" is present, THIS call is graded against exactly
        # those master page(s) instead of the full staged/cached master; when
        # absent, behavior is byte-path identical to before (the fallback).
        master_b64 = body.get("master_images_b64")
        master_pngs = [_decode_png(b) for b in master_b64] if master_b64 else None

        with _cache_lock:
            cache_name = _active_cache["name"]
            cache_model = _active_cache["model"]
            anthropic_system = _anthropic_ctx["system"]
            anthropic_imgs = list(_anthropic_ctx["image_bytes"])
            google_system = _google_ctx["system"]
            google_imgs = list(_google_ctx["image_bytes"])
        if not cache_name:
            raise RuntimeError("No active evaluation context. Call /api/cache/build first.")

        if _provider_for(cache_model) == "anthropic":
            ac = _get_anthropic_client()
            # [PATCH:payload-governor] live pages get the same normalization
            # the masters got at cache/build.
            live_pngs = [_anthropic_normalize_image(b) for b in live_pngs]
            # [PATCH:silo2-per-chunk-context] per-call masters REPLACE the staged
            # full-master set for THIS call only, normalized like everything else.
            per_call_ctx = master_pngs is not None
            if per_call_ctx:
                anthropic_imgs = [_anthropic_normalize_image(b) for b in master_pngs]
            # [PATCH:batch] System block, master images (last one cache
            # marked), live images and the prompt line are built by the ONE
            # shared builder, so live and batched requests cannot drift apart.
            system_blocks, content = _anthropic_silo2_body(
                anthropic_system, anthropic_imgs, live_pngs, prompt, schema)
            # [PATCH:payload-governor] measure what we are about to send and
            # refuse anything the API would 413 anyway -- with a message that
            # names the fix instead of masquerading as a network blip.
            _approx = sum(len(blk["source"]["data"])
                          for blk in content if blk.get("type") == "image")
            log.info("Anthropic silo2 payload: %d image block(s) "
                     "(%d master + %d live), ~%.1f MB base64.",
                     len(anthropic_imgs) + len(live_pngs),
                     len(anthropic_imgs), len(live_pngs), _approx / 1e6)
            if _approx > _ANTHROPIC_PAYLOAD_CEILING:
                raise RuntimeError(
                    f"request_too_large: this evaluation would carry ~{_approx / 1e6:.0f} MB "
                    f"of image data, over the Anthropic 32 MB request cap (HTTP 413). "
                    f"Retrying cannot succeed. Reduce LEDGER_ESSAY_MAX_PAGES_PER_CALL "
                    f"(GUI env) or LEDGER_ANTHROPIC_MAX_EDGE (/etc/paragon/.env).")
            msg = ac.messages.create(
                model=cache_model,
                max_tokens=ANTHROPIC_MAX_TOKENS,
                system=system_blocks,
                messages=[{"role": "user", "content": content}],
            )
            # [PATCH:cache-usage-log] the only way to SEE whether the prefix
            # cache is working. cache_read should be large on every call after
            # the first per distinct master set; cache_write large only then.
            _u = getattr(msg, "usage", None)
            log.info("Anthropic usage: input=%s cache_write=%s cache_read=%s output=%s ttl=%s per_call_ctx=%s",
                     getattr(_u, "input_tokens", "?"),
                     getattr(_u, "cache_creation_input_tokens", "?"),
                     getattr(_u, "cache_read_input_tokens", "?"),
                     getattr(_u, "output_tokens", "?"),
                     _ANTHROPIC_CACHE_TTL, per_call_ctx)
            # [PATCH:truncation-guard] A response that hit the max_tokens
            # ceiling is INCOMPLETE JSON. Returned as-is, json_repair closes
            # the dangling brackets and the GUI commits an amputated grade
            # (missing essays, empty feedback, phantom zeros) with no visible
            # error. Fail LOUDLY instead; the "invalid_request" prefix makes
            # the GUI defer this student immediately with the real reason.
            if getattr(msg, "stop_reason", None) == "max_tokens":
                # [PATCH:batch] message text extracted to _truncation_message()
                # so the batch result path discards truncations with the SAME
                # wording; the behavior here is unchanged.
                raise RuntimeError(_truncation_message())
            return jsonify({"status": "success", "text": _anthropic_text(msg)})

        # Google path.
        # [PATCH:multi-image] one Part per live page/grid, in page order --
        # Gemini tiles each Part natively at its own full resolution.
        live_parts = [
            types.Part.from_bytes(data=b, mime_type="image/png")
            for b in live_pngs
        ]

        # [PATCH:silo2-per-chunk-context] per-call master context: bypass the
        # server cache for THIS call and inline the chunk's own master page(s).
        if master_pngs is not None:
            # [PATCH:batch] built by the shared builder (identical content).
            contents, cfg_kwargs = _google_silo2_parts_and_config(
                google_system, master_pngs, live_pngs, prompt, schema)
            log.info("Google silo2 per-chunk context: %d master + %d live image(s).",
                     len(master_pngs), len(live_parts))
            resp = _require_google().models.generate_content(
                model=cache_model or DEFAULT_MODEL,
                contents=contents,
                config=types.GenerateContentConfig(**cfg_kwargs),
            )
            _log_google_usage(resp, cache_name, master_pngs is not None)
            return jsonify({"status": "success", "text": resp.text or ""})

        if cache_name == _GOOGLE_INLINE:
            # No server cache (master was sub-floor): re-send the system prompt +
            # master pages inline alongside the live grid on every grade.
            # [PATCH:batch] built by the shared builder (identical content).
            contents, cfg_kwargs = _google_silo2_parts_and_config(
                google_system, google_imgs, live_pngs, prompt, schema)
            resp = _require_google().models.generate_content(
                model=cache_model or DEFAULT_MODEL,
                contents=contents,
                config=types.GenerateContentConfig(**cfg_kwargs),
            )
            _log_google_usage(resp, cache_name, master_pngs is not None)
            return jsonify({"status": "success", "text": resp.text or ""})

        # Cached path (unchanged from v1 apart from the live-parts splat)
        cfg_kwargs = {"cached_content": cache_name}
        if schema:
            cfg_kwargs["response_mime_type"] = "application/json"
            cfg_kwargs["response_schema"] = schema
        resp = _require_google().models.generate_content(
            model=cache_model or DEFAULT_MODEL,
            contents=[*live_parts, prompt],
            config=types.GenerateContentConfig(**cfg_kwargs),
        )
        _log_google_usage(resp, cache_name, master_pngs is not None)
        return jsonify({"status": "success", "text": resp.text or ""})
    except Exception as exc:  # noqa: BLE001
        log.exception("silo2 failed")
        return jsonify({"status": "error", "message": str(exc)}), 500


# ---------------------------------------------------------------------------
# [PATCH:formatter] /api/formatter/*  --  WO-F1 assignment-formatter endpoints
# Self-contained: nothing above this block changed behavior. One job at a
# time; the provider call runs in a background thread so /start returns at
# once and the GUI polls /status. No sampling or thinking parameters are
# sent, matching the daemon-wide rule.
# ---------------------------------------------------------------------------
import time  # [PATCH:formatter] job ids

try:
    FORMATTER_MAX_TOKENS = int(ENV.get("LEDGER_FORMATTER_MAX_TOKENS") or "32000")
except ValueError:
    FORMATTER_MAX_TOKENS = 32000

_formatter_lock = threading.Lock()
_formatter_job = {"id": None, "state": None, "text": None, "usage": None,
                  "message": None}


def _formatter_anthropic_content(content):
    """Convert the GUI's neutral content list into Anthropic blocks.
    Entries: {type: text|image|document, ...}. Data arrives base64-encoded."""
    blocks = []
    for item in content:
        t = item.get("type")
        if t == "text":
            blocks.append({"type": "text", "text": item.get("text", "")})
        elif t == "image":
            blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": item.get("media_type", "image/png"),
                    "data": item.get("data", ""),
                },
            })
        elif t == "document":
            blocks.append({
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": item.get("media_type", "application/pdf"),
                    "data": item.get("data", ""),
                },
            })
    return blocks


def _formatter_google_parts(content):
    """Convert the GUI's neutral content list into google-genai Parts.
    PDF document blocks are SKIPPED on this route (the page images and
    extracted text already carry the source); that is logged once."""
    parts = []
    skipped_docs = 0
    for item in content:
        t = item.get("type")
        if t == "text":
            parts.append(types.Part.from_text(text=item.get("text", "")))
        elif t == "image":
            parts.append(types.Part.from_bytes(
                data=base64.b64decode(item.get("data", "")),
                mime_type=item.get("media_type", "image/png")))
        elif t == "document":
            skipped_docs += 1
    if skipped_docs:
        log.info("Formatter (Google route): %d PDF document block(s) skipped "
                 "(page images + extracted text carry the source).", skipped_docs)
    return parts


def _run_formatter_job(job_id, model_name, system_text, messages):
    """Background worker: one provider call, result parked in _formatter_job."""
    try:
        if _provider_for(model_name) == "anthropic":
            ac = _get_anthropic_client()
            system_blocks = [{
                "type": "text",
                "text": system_text or "",
                "cache_control": _anthropic_cache_control(),
            }]
            conv = [{"role": m.get("role", "user"),
                     "content": _formatter_anthropic_content(m.get("content") or [])}
                    for m in messages]
            # Long outputs: the SDK requires streaming past the non-streaming
            # window; stream and collect the final message.
            with ac.messages.stream(
                model=model_name,
                max_tokens=FORMATTER_MAX_TOKENS,
                system=system_blocks,
                messages=conv,
            ) as stream:
                msg = stream.get_final_message()
            u = getattr(msg, "usage", None)
            usage = {
                "input": getattr(u, "input_tokens", None),
                "cache_write": getattr(u, "cache_creation_input_tokens", None),
                "cache_read": getattr(u, "cache_read_input_tokens", None),
                "output": getattr(u, "output_tokens", None),
            }
            log.info("Formatter usage (model=%s): input=%s cache_write=%s "
                     "cache_read=%s output=%s", model_name, usage["input"],
                     usage["cache_write"], usage["cache_read"], usage["output"])
            if getattr(msg, "stop_reason", None) == "max_tokens":
                raise RuntimeError(
                    "The reply was cut off at the "
                    f"{FORMATTER_MAX_TOKENS}-token formatter output limit and "
                    "was discarded. Raise LEDGER_FORMATTER_MAX_TOKENS in "
                    "/etc/paragon/.env and restart ledger-daemon.service.")
            text = _anthropic_text(msg)
        else:
            gc = _require_google()
            contents = []
            for m in messages:
                role = "model" if m.get("role") == "assistant" else "user"
                parts = _formatter_google_parts(m.get("content") or [])
                if parts:
                    contents.append(types.Content(role=role, parts=parts))
            cfg = types.GenerateContentConfig(
                system_instruction=system_text or None,
                max_output_tokens=FORMATTER_MAX_TOKENS,
            )
            resp = gc.models.generate_content(
                model=model_name, contents=contents, config=cfg)
            u = getattr(resp, "usage_metadata", None)
            usage = {
                "input": getattr(u, "prompt_token_count", None),
                "cache_write": None,
                "cache_read": getattr(u, "cached_content_token_count", None),
                "output": getattr(u, "candidates_token_count", None),
            }
            log.info("Formatter usage (model=%s): input=%s cache_write=%s "
                     "cache_read=%s output=%s", model_name, usage["input"],
                     usage["cache_write"], usage["cache_read"], usage["output"])
            finish = None
            cands = getattr(resp, "candidates", None) or []
            if cands:
                finish = str(getattr(cands[0], "finish_reason", "") or "")
            if finish and "MAX_TOKENS" in finish.upper():
                raise RuntimeError(
                    "The reply was cut off at the "
                    f"{FORMATTER_MAX_TOKENS}-token formatter output limit and "
                    "was discarded. Raise LEDGER_FORMATTER_MAX_TOKENS in "
                    "/etc/paragon/.env and restart ledger-daemon.service.")
            text = resp.text or ""
        with _formatter_lock:
            if _formatter_job["id"] == job_id:
                _formatter_job["state"] = "done"
                _formatter_job["text"] = text
                _formatter_job["usage"] = usage
    except Exception as exc:  # noqa: BLE001 - every failure surfaces to the GUI
        log.exception("formatter job failed")
        with _formatter_lock:
            if _formatter_job["id"] == job_id:
                _formatter_job["state"] = "error"
                _formatter_job["message"] = str(exc)


@app.post("/api/formatter/start")
def formatter_start():
    try:
        body = request.get_json(force=True) or {}
        model_name = body.get("model_name") or DEFAULT_MODEL
        system_text = body.get("system", "")
        messages = body.get("messages") or []
        with _formatter_lock:
            if _formatter_job["state"] == "running":
                return jsonify({"status": "error",
                                "message": "A formatting job is already "
                                           "running. Wait for it to finish."}), 409
            job_id = f"fmt-{int(time.time() * 1000)}"
            _formatter_job.update(id=job_id, state="running", text=None,
                                  usage=None, message=None)
        n_img = sum(1 for m in messages for c in (m.get("content") or [])
                    if c.get("type") == "image")
        log.info("Formatter job %s started (model=%s, %d message(s), "
                 "%d image(s)).", job_id, model_name, len(messages), n_img)
        threading.Thread(target=_run_formatter_job,
                         args=(job_id, model_name, system_text, messages),
                         daemon=True).start()
        return jsonify({"status": "success", "job_id": job_id})
    except Exception as exc:  # noqa: BLE001
        log.exception("formatter/start failed")
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.get("/api/formatter/status")
def formatter_status():
    job_id = request.args.get("job_id", "")
    with _formatter_lock:
        if _formatter_job["id"] != job_id:
            return jsonify({"state": "error", "text": None, "usage": None,
                            "message": "Unknown job id."}), 404
        return jsonify({"state": _formatter_job["state"],
                        "text": _formatter_job["text"],
                        "usage": _formatter_job["usage"],
                        "message": _formatter_job["message"]})


# ---------------------------------------------------------------------------
# [PATCH:batch] /api/batch/*  --  WO-B1 batch-scoring endpoints (v2.11)
# Self-contained, like the formatter family: nothing above changed behavior.
# The daemon stays (nearly) stateless: the GUI keeps the job record and the
# daemon needs only provider batch ids to report status and fetch results.
# The one transient piece of daemon state is a per-submission SPOOL file on
# disk: the GUI sends requests in small groups (bounded memory on a small
# PC), the daemon builds each provider request with the SAME code as the
# live path and appends it to the spool, and /api/batch/submit turns one
# spool into one provider batch. A spool that never gets submitted is only
# a temp file; it holds no provider state.
#   POST /api/batch/spool   {submission_id, requests:[{custom_id, prompt,
#                            schema, image_b64|images_b64[],
#                            master_images_b64[]?}]}
#                           -> {status, submission_id, count, bytes}
#   POST /api/batch/submit  {submission_id} -> {status, batch_id, count}
#   GET  /api/batch/status  ?ids=a,b,c -> {status, batches:[{id, done,
#                            state, counts}]}
#   POST /api/batch/results {id} -> {status, job_state, results:[{custom_id,
#                            state, text, message, usage}]} ; a job whose
#                            stored output no longer exists returns
#                            {"status":"error","reason":"results_gone"}.
#   POST /api/batch/cancel  {id} -> {status}
#   POST /api/batch/cleanup {id} -> {status}  (delete provider-side files
#                            for a COLLECTED job: GCS objects / Files API
#                            entries; call only after results are saved)
# Routing: msgbatch_* -> Anthropic Message Batches; batches/* -> Gemini
# Developer API (file-based); *batchPredictionJobs* -> Vertex (GCS JSONL).
# Vertex needs the Cloud Storage bucket ledger-batch-<project-id>; it is
# created on first use with a 2-day delete lifecycle when the service
# account is permitted, and a clear "needs bucket" error is returned when
# it is not. No sampling or thinking parameters ride on any batched
# request, and a result that stopped at the output limit is DISCARDED with
# the same message as the live truncation guard, on both providers.
# ---------------------------------------------------------------------------
import json as _json
import shutil as _shutil
import tempfile as _tempfile

_batch_lock = threading.Lock()
_batch_spools = {}
_batch_spool_dir = None
_gcs_session_cache = {"sess": None}


def _batch_spool_path(sid):
    global _batch_spool_dir
    if _batch_spool_dir is None:
        _batch_spool_dir = _tempfile.mkdtemp(prefix="ledger-batch-spool-")
        os.chmod(_batch_spool_dir, 0o700)
    safe = "".join(c for c in sid if c.isalnum() or c in "-_")[:80]
    return os.path.join(_batch_spool_dir, f"{safe}.jsonl")


def _batch_route_for_id(batch_id):
    b = batch_id or ""
    if b.startswith("msgbatch"):
        return "anthropic"
    if "batchPredictionJobs" in b:
        return "google-vertex"
    if b.startswith("batches/"):
        return "google-devapi"
    raise ValueError(f"Unrecognized batch id shape: {b[:60]!r}")


def _batch_capabilities():
    """Per-route batch capability, with a plain reason when unsupported."""
    if _anthropic_route == "direct-api":
        anth = {"supported": True, "reason": None}
    elif _anthropic_route == "vertex":
        anth = {"supported": False,
                "reason": "Claude on this box routes through Vertex; the SDK "
                          "cannot submit Message Batches on that route."}
    else:
        anth = {"supported": False,
                "reason": "No Anthropic route is configured on this box."}
    if _google_route == "developer-api":
        goog = {"supported": True, "reason": None}
    elif _google_route == "vertex":
        goog = {"supported": True,
                "reason": f"Vertex batch stores requests in the Cloud Storage "
                          f"bucket ledger-batch-{PROJECT_ID} (created on first "
                          f"use when the service account is permitted)."}
    else:
        goog = {"supported": False,
                "reason": "No Google route is configured on this box."}
    return {"anthropic": anth, "google": goog}


def _gcs_session():
    if _gcs_session_cache["sess"] is None:
        import google.auth
        from google.auth.transport.requests import AuthorizedSession
        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"])
        _gcs_session_cache["sess"] = AuthorizedSession(creds)
    return _gcs_session_cache["sess"]


def _vertex_bucket_name():
    return f"ledger-batch-{PROJECT_ID}"


def _vertex_ensure_bucket(sess):
    """Create ledger-batch-<project> with a 2-day delete lifecycle if absent.
    A permission failure raises a plain 'needs bucket' message -- never
    worked around."""
    bucket = _vertex_bucket_name()
    r = sess.get(f"https://storage.googleapis.com/storage/v1/b/{bucket}")
    if r.status_code == 200:
        return bucket
    if r.status_code == 403:
        # [PATCH:batch] v2.12 (WO-B1-2): a service account with Storage
        # Object Admin on ONLY this bucket (the recommended
        # least-privilege setup) cannot read bucket METADATA (storage.buckets.get)
        # but CAN list objects. A 403 here therefore does not mean the
        # bucket is unusable -- ask for an object list before concluding.
        r2 = sess.get(f"https://storage.googleapis.com/storage/v1/b/{bucket}"
                      f"/o?maxResults=1")
        if r2.status_code == 200:
            return bucket
        raise RuntimeError(
            "needs bucket: the service account can reach neither the Cloud "
            f"Storage bucket {bucket} nor its objects (HTTP {r.status_code}/"
            f"{r2.status_code}). Grant it Storage Object Admin on that "
            "bucket (or create the bucket if it does not exist).")
    if r.status_code != 404:
        raise RuntimeError(f"Cloud Storage bucket check failed "
                           f"(HTTP {r.status_code}): {r.text[:200]}")
    body = {
        "name": bucket,
        "lifecycle": {"rule": [{"action": {"type": "Delete"},
                                "condition": {"age": 2}}]},
        "iamConfiguration": {"uniformBucketLevelAccess": {"enabled": True}},
    }
    r = sess.post(f"https://storage.googleapis.com/storage/v1/b"
                  f"?project={PROJECT_ID}", json=body)
    if r.status_code != 200:
        raise RuntimeError(
            "needs bucket: the service account cannot create the Cloud "
            f"Storage bucket {bucket} (HTTP {r.status_code}). Vertex batch "
            "is unavailable until it exists. Grant the service account "
            "Storage Admin on the project, or create the bucket (2-day "
            "object lifecycle) and grant Storage Object Admin on it.")
    log.info("Created Cloud Storage bucket %s (2-day delete lifecycle).", bucket)
    return bucket


def _gcs_upload_file(sess, bucket, object_name, path):
    import urllib.parse
    with open(path, "rb") as fh:
        r = sess.post(
            "https://storage.googleapis.com/upload/storage/v1/b/"
            f"{bucket}/o?uploadType=media&name="
            f"{urllib.parse.quote(object_name, safe='')}",
            data=fh, headers={"Content-Type": "application/jsonl"})
    r.raise_for_status()
    return f"gs://{bucket}/{object_name}"


def _gcs_list(sess, bucket, prefix):
    import urllib.parse
    items = []
    page = None
    while True:
        url = (f"https://storage.googleapis.com/storage/v1/b/{bucket}/o"
               f"?prefix={urllib.parse.quote(prefix, safe='')}")
        if page:
            url += f"&pageToken={page}"
        r = sess.get(url)
        r.raise_for_status()
        d = r.json()
        items.extend(d.get("items", []))
        page = d.get("nextPageToken")
        if not page:
            return items


def _gcs_download(sess, bucket, object_name):
    import urllib.parse
    r = sess.get(f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/"
                 f"{urllib.parse.quote(object_name, safe='')}?alt=media")
    r.raise_for_status()
    return r.text


def _gcs_delete_prefix(sess, bucket, prefix):
    import urllib.parse
    n = 0
    for item in _gcs_list(sess, bucket, prefix):
        r = sess.delete(
            f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/"
            f"{urllib.parse.quote(item['name'], safe='')}")
        if r.status_code in (200, 204):
            n += 1
    return n


def _split_gs_uri(uri):
    rest = (uri or "")[5:]  # strip gs://
    bucket, _, prefix = rest.partition("/")
    return bucket, prefix


def _google_job_done_state(state_str):
    return any(k in state_str for k in ("SUCCEEDED", "FAILED", "CANCELLED",
                                        "EXPIRED", "PAUSED"))


def _google_batch_text_and_usage(response_dict):
    """(text, usage, truncated) from one batch-result response dict."""
    cands = (response_dict or {}).get("candidates") or []
    finish = str(cands[0].get("finishReason", "")) if cands else ""
    text = ""
    if cands:
        for p in (cands[0].get("content") or {}).get("parts", []) or []:
            text += p.get("text", "") or ""
    um = (response_dict or {}).get("usageMetadata") or {}
    usage = {"input": um.get("promptTokenCount"),
             "cache_read": um.get("cachedContentTokenCount"),
             "output": um.get("candidatesTokenCount"),
             "thoughts": um.get("thoughtsTokenCount")}
    return text, usage, "MAX_TOKENS" in finish.upper()


@app.post("/api/batch/spool")
def batch_spool():
    """Build provider-ready request lines from GUI silo2-shaped requests and
    append them to this submission's spool. Uses the ACTIVE grading context
    (call /api/cache/build first), exactly like /api/silo2."""
    try:
        body = request.get_json(force=True) or {}
        sid = (body.get("submission_id") or "").strip()
        reqs = body.get("requests") or []
        if not sid:
            raise ValueError("submission_id is required")
        with _cache_lock:
            cache_name = _active_cache["name"]
            cache_model = _active_cache["model"]
            anthropic_system = _anthropic_ctx["system"]
            anthropic_imgs = list(_anthropic_ctx["image_bytes"])
            google_system = _google_ctx["system"]
            google_imgs = list(_google_ctx["image_bytes"])
        if not cache_name:
            raise RuntimeError("No active evaluation context. Call /api/cache/build first.")
        route = _provider_for(cache_model)
        if route == "anthropic" and _anthropic_route != "direct-api":
            raise RuntimeError(_batch_capabilities()["anthropic"]["reason"]
                               or "Anthropic batch unavailable.")
        path = _batch_spool_path(sid)
        with _batch_lock:
            entry = _batch_spools.setdefault(
                sid, {"path": path, "count": 0, "route": route,
                      "model": cache_model})
            if entry["route"] != route or entry["model"] != cache_model:
                raise RuntimeError("Spool context changed mid-submission.")
        n_new = 0
        with open(path, "a", encoding="utf-8") as spool:
            for rq in reqs:
                cid = (rq.get("custom_id") or "").strip()
                if not cid:
                    raise ValueError("every request needs a custom_id")
                prompt = rq.get("prompt", "")
                schema = rq.get("schema")
                imgs_b64 = rq.get("images_b64") or ([rq["image_b64"]]
                                                    if rq.get("image_b64") else [])
                if not imgs_b64:
                    raise ValueError(f"request {cid}: no image payload")
                master_b64 = rq.get("master_images_b64")
                if route == "anthropic":
                    lives = [_anthropic_normalize_image(_decode_png(b))
                             for b in imgs_b64]
                    masters = ([_anthropic_normalize_image(_decode_png(b))
                                for b in master_b64]
                               if master_b64 else anthropic_imgs)
                    system_blocks, content = _anthropic_silo2_body(
                        anthropic_system, masters, lives, prompt, schema)
                    line = {"custom_id": cid,
                            "params": {"model": cache_model,
                                       "max_tokens": ANTHROPIC_MAX_TOKENS,
                                       "system": system_blocks,
                                       "messages": [{"role": "user",
                                                     "content": content}]}}
                else:
                    lives = [_decode_png(b) for b in imgs_b64]
                    masters = ([_decode_png(b) for b in master_b64]
                               if master_b64 else google_imgs)
                    rd = _google_silo2_request_dict(
                        google_system, masters, lives, prompt, schema)
                    if _google_route == "vertex":
                        # Mapping key echoed back in the Vertex output line.
                        rd["labels"] = {"ledger_custom_id": cid}
                        line = {"request": rd}
                    else:
                        line = {"key": cid, "request": rd}
                spool.write(_json.dumps(line) + "\n")
                n_new += 1
        with _batch_lock:
            _batch_spools[sid]["count"] += n_new
            total = _batch_spools[sid]["count"]
        size = os.path.getsize(path)
        log.info("Batch spool %s: +%d request(s) (total %d, %.1f MB).",
                 sid, n_new, total, size / 1e6)
        return jsonify({"status": "success", "submission_id": sid,
                        "count": total, "bytes": size})
    except Exception as exc:  # noqa: BLE001
        log.exception("batch/spool failed")
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.post("/api/batch/submit")
def batch_submit():
    """Turn one spool into ONE provider batch. The spool is deleted only on
    success; a failed submit leaves it intact so the GUI can decide."""
    try:
        body = request.get_json(force=True) or {}
        sid = (body.get("submission_id") or "").strip()
        with _batch_lock:
            entry = _batch_spools.get(sid)
        if not entry or not os.path.exists(entry["path"]):
            raise RuntimeError(f"Unknown or empty submission: {sid}")
        route, model, path = entry["route"], entry["model"], entry["path"]
        if route == "anthropic":
            ac = _get_anthropic_client()
            with open(path, encoding="utf-8") as fh:
                reqs = [_json.loads(l) for l in fh if l.strip()]
            batch = ac.messages.batches.create(requests=reqs)
            batch_id = batch.id
        else:
            gc = _require_google()
            if _google_route == "developer-api":
                uploaded = gc.files.upload(
                    file=path,
                    config=types.UploadFileConfig(
                        display_name=f"ledger-batch-{sid}",
                        mime_type="jsonl"))
                job = gc.batches.create(
                    model=model, src=uploaded.name,
                    config=types.CreateBatchJobConfig(
                        display_name=f"ledger-{sid}"))
                batch_id = job.name
            else:
                sess = _gcs_session()
                bucket = _vertex_ensure_bucket(sess)
                gs_in = _gcs_upload_file(
                    sess, bucket, f"jobs/{sid}/input.jsonl", path)
                job = gc.batches.create(
                    model=model, src=gs_in,
                    config=types.CreateBatchJobConfig(
                        display_name=f"ledger-{sid}",
                        dest=f"gs://{bucket}/jobs/{sid}/output"))
                batch_id = job.name
        with _batch_lock:
            _batch_spools.pop(sid, None)
        try:
            os.remove(path)
        except OSError:
            pass
        log.info("Batch submitted: %s -> %s (%d request(s), route %s).",
                 sid, batch_id, entry["count"], route)
        return jsonify({"status": "success", "batch_id": batch_id,
                        "count": entry["count"]})
    except Exception as exc:  # noqa: BLE001
        log.exception("batch/submit failed")
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.get("/api/batch/status")
def batch_status():
    try:
        ids = [i for i in (request.args.get("ids") or "").split(",") if i]
        out = []
        for bid in ids:
            try:
                route = _batch_route_for_id(bid)
                if route == "anthropic":
                    b = _get_anthropic_client().messages.batches.retrieve(bid)
                    c = b.request_counts
                    out.append({"id": bid,
                                "done": b.processing_status == "ended",
                                "state": b.processing_status,
                                "counts": {"processing": c.processing,
                                           "succeeded": c.succeeded,
                                           "errored": c.errored,
                                           "canceled": c.canceled,
                                           "expired": c.expired}})
                else:
                    j = _require_google().batches.get(name=bid)
                    state = str(j.state)
                    counts = None
                    cs = getattr(j, "completion_stats", None)
                    if cs is not None:
                        done_n = int(getattr(cs, "successful_count", 0) or 0)
                        fail_n = int(getattr(cs, "failed_count", 0) or 0)
                        counts = {"succeeded": done_n, "errored": fail_n}
                    out.append({"id": bid,
                                "done": _google_job_done_state(state),
                                "state": state, "counts": counts})
            except Exception as exc:  # noqa: BLE001 - per-id errors ride along
                out.append({"id": bid, "error": str(exc)})
        return jsonify({"status": "success", "batches": out})
    except Exception as exc:  # noqa: BLE001
        log.exception("batch/status failed")
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.post("/api/batch/results")
def batch_results():
    """Fetch and normalize one provider batch's results. Token usage is
    logged per result and totalled per job, in the existing usage-line
    style; keys and student content are never logged."""
    try:
        body = request.get_json(force=True) or {}
        bid = (body.get("id") or "").strip()
        route = _batch_route_for_id(bid)
        results = []
        totals = {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0}
        job_state = None
        # [PATCH:batch] v2.12 (WO-B1-2 Task 4): how many succeeded requests
        # read the stored prompt from cache (Anthropic explicit cache, or
        # Google's implicit cachedContentTokenCount).
        reuse = {"reused": 0, "succeeded": 0}

        def _tally(u):
            for k in totals:
                v = u.get(k)
                if isinstance(v, (int, float)):
                    totals[k] += v
            reuse["succeeded"] += 1
            cr = u.get("cache_read")
            if isinstance(cr, (int, float)) and cr > 0:
                reuse["reused"] += 1

        if route == "anthropic":
            ac = _get_anthropic_client()
            try:
                stream = ac.messages.batches.results(bid)
                for r in stream:
                    cid = r.custom_id
                    typ = r.result.type
                    if typ == "succeeded":
                        msg = r.result.message
                        if getattr(msg, "stop_reason", None) == "max_tokens":
                            log.warning("Batch result %s TRUNCATED at the "
                                        "output ceiling -- discarded.", cid)
                            results.append({"custom_id": cid, "state": "errored",
                                            "message": _truncation_message()})
                            continue
                        u = getattr(msg, "usage", None)
                        usage = {"input": getattr(u, "input_tokens", None),
                                 "cache_write": getattr(u, "cache_creation_input_tokens", None),
                                 "cache_read": getattr(u, "cache_read_input_tokens", None),
                                 "output": getattr(u, "output_tokens", None)}
                        log.info("Anthropic batch usage: id=%s input=%s "
                                 "cache_write=%s cache_read=%s output=%s",
                                 cid, usage["input"], usage["cache_write"],
                                 usage["cache_read"], usage["output"])
                        _tally(usage)
                        results.append({"custom_id": cid, "state": "succeeded",
                                        "text": _anthropic_text(msg),
                                        "usage": usage})
                    elif typ == "errored":
                        results.append({"custom_id": cid, "state": "errored",
                                        "message": str(getattr(r.result, "error", ""))[:500]})
                    else:  # canceled | expired
                        results.append({"custom_id": cid, "state": typ})
                job_state = "ended"
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                if "404" in msg or "not_found" in msg.lower():
                    return jsonify({"status": "error", "reason": "results_gone",
                                    "message": "The provider no longer holds "
                                               "this batch's results."}), 500
                raise
        else:
            gc = _require_google()
            j = gc.batches.get(name=bid)
            job_state = str(j.state)
            if "SUCCEEDED" not in job_state and "FAILED" not in job_state:
                return jsonify({"status": "success", "job_state": job_state,
                                "results": []})
            if route == "google-devapi":
                fname = getattr(j.dest, "file_name", None) if j.dest else None
                if not fname:
                    return jsonify({"status": "error", "reason": "results_gone",
                                    "message": "The batch job holds no result "
                                               "file any more."}), 500
                try:
                    raw = gc.files.download(file=fname)
                except Exception as exc:  # noqa: BLE001
                    msg = str(exc)
                    if "404" in msg or "403" in msg or "PERMISSION" in msg.upper():
                        return jsonify({"status": "error",
                                        "reason": "results_gone",
                                        "message": "Google has already deleted "
                                                   "this batch's result file "
                                                   "(files live ~48 hours)."}), 500
                    raise
                text_blob = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
                lines = text_blob.splitlines()
            else:
                sess = _gcs_session()
                out_uri = getattr(j.dest, "gcs_uri", None) if j.dest else None
                if not out_uri:
                    return jsonify({"status": "error", "reason": "results_gone",
                                    "message": "The batch job holds no output "
                                               "location any more."}), 500
                bucket, prefix = _split_gs_uri(out_uri)
                objs = [o for o in _gcs_list(sess, bucket, prefix)
                        if o["name"].endswith(".jsonl")]
                if not objs:
                    return jsonify({"status": "error", "reason": "results_gone",
                                    "message": "The storage bucket no longer "
                                               "holds this job's results (its "
                                               "objects auto-delete after 2 "
                                               "days)."}), 500
                lines = []
                for o in objs:
                    lines.extend(_gcs_download(sess, bucket, o["name"]).splitlines())
            for ln in lines:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    d = _json.loads(ln)
                except ValueError:
                    continue
                cid = d.get("key")
                if cid is None:
                    cid = ((d.get("request") or {}).get("labels") or {}).get(
                        "ledger_custom_id")
                resp = d.get("response")
                err = d.get("error") or d.get("status")
                if resp:
                    text, usage, truncated = _google_batch_text_and_usage(resp)
                    if truncated:
                        log.warning("Batch result %s TRUNCATED at the output "
                                    "limit -- discarded.", cid)
                        results.append({"custom_id": cid, "state": "errored",
                                        "message": _truncation_message()})
                        continue
                    log.info("Google batch usage: id=%s prompt=%s cached=%s "
                             "thoughts=%s output=%s", cid, usage["input"],
                             usage["cache_read"], usage["thoughts"],
                             usage["output"])
                    _tally(usage)
                    results.append({"custom_id": cid, "state": "succeeded",
                                    "text": text, "usage": usage})
                else:
                    results.append({"custom_id": cid, "state": "errored",
                                    "message": _json.dumps(err)[:500] if err
                                    else "no response in result line"})
        log.info("Batch %s totals: results=%d reused_prompt=%d/%d input=%s "
                 "cache_write=%s cache_read=%s output=%s", bid, len(results),
                 reuse["reused"], reuse["succeeded"],
                 totals["input"], totals["cache_write"], totals["cache_read"],
                 totals["output"])
        return jsonify({"status": "success", "job_state": job_state,
                        "results": results, "totals": totals,
                        "reused_prompt": reuse["reused"],
                        "succeeded": reuse["succeeded"]})
    except Exception as exc:  # noqa: BLE001
        log.exception("batch/results failed")
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.post("/api/batch/cancel")
def batch_cancel():
    try:
        body = request.get_json(force=True) or {}
        bid = (body.get("id") or "").strip()
        route = _batch_route_for_id(bid)
        if route == "anthropic":
            _get_anthropic_client().messages.batches.cancel(bid)
        else:
            _require_google().batches.cancel(name=bid)
        log.info("Batch cancelled at provider: %s", bid)
        return jsonify({"status": "success"})
    except Exception as exc:  # noqa: BLE001
        log.exception("batch/cancel failed")
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.post("/api/batch/keepalive")
def batch_keepalive():
    """[PATCH:batch] v2.12 (WO-B1-2 Task 3): renew the Anthropic prompt cache
    while a batch is pending. Builds the request with the SAME
    _anthropic_silo2_body call as live and batched requests (byte-identical
    cached prefix: same system block, same cache-marked master images), with
    NO student content after it, and asks for max_tokens=0 -- the cheapest
    request the API allows that reads (and re-times) the cache. Direct
    Anthropic route only. The caller treats any failure as log-and-continue."""
    try:
        body = request.get_json(force=True) or {}
        with _cache_lock:
            cache_name = _active_cache["name"]
            cache_model = _active_cache["model"]
            anthropic_system = _anthropic_ctx["system"]
            anthropic_imgs = list(_anthropic_ctx["image_bytes"])
        if not cache_name:
            raise RuntimeError("No active evaluation context. "
                               "Call /api/cache/build first.")
        if _provider_for(cache_model) != "anthropic":
            raise RuntimeError("Keep-alive applies to the Anthropic route only.")
        if _anthropic_route != "direct-api":
            raise RuntimeError(_batch_capabilities()["anthropic"]["reason"]
                               or "Anthropic batch unavailable.")
        master_b64 = body.get("master_images_b64")
        masters = ([_anthropic_normalize_image(_decode_png(b))
                    for b in master_b64]
                   if master_b64 else anthropic_imgs)
        # Everything PAST the cache-marked master block is not part of the
        # cached prefix, so the trailing text may be a plain placeholder.
        system_blocks, content = _anthropic_silo2_body(
            anthropic_system, masters, [], "warmup", None)
        ac = _get_anthropic_client()
        kwargs = dict(model=cache_model, system=system_blocks,
                      messages=[{"role": "user", "content": content}])
        try:
            msg = ac.messages.create(max_tokens=0, **kwargs)
        except Exception as exc:  # noqa: BLE001
            # If this API/SDK pairing rejects max_tokens=0, one output token
            # is the next-cheapest request that still reads the cache.
            if "max_tokens" not in str(exc):
                raise
            msg = ac.messages.create(max_tokens=1, **kwargs)
        u = getattr(msg, "usage", None)
        usage = {"input": getattr(u, "input_tokens", None),
                 "cache_write": getattr(u, "cache_creation_input_tokens", None),
                 "cache_read": getattr(u, "cache_read_input_tokens", None),
                 "output": getattr(u, "output_tokens", None)}
        log.info("Batch keep-alive: cache_read=%s cache_write=%s input=%s",
                 usage["cache_read"], usage["cache_write"], usage["input"])
        return jsonify({"status": "success", "usage": usage})
    except Exception as exc:  # noqa: BLE001
        log.exception("batch/keepalive failed")
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.post("/api/batch/cleanup")
def batch_cleanup():
    """Delete provider-side files for a COLLECTED job (results already saved
    by the GUI): the Vertex job's GCS objects, or the Developer-API job's
    uploaded files. Best-effort; never fails the caller hard."""
    try:
        body = request.get_json(force=True) or {}
        bid = (body.get("id") or "").strip()
        route = _batch_route_for_id(bid)
        removed = 0
        if route == "anthropic":
            # [PATCH:batch] v2.12 (WO-B1-2 Task 5): delete the ended batch at
            # Anthropic, which removes its stored results instead of waiting
            # out the 29-day retention. Only ended batches can be deleted;
            # the GUI calls cleanup only after every student in the job is
            # finished or deferred, so nothing uncollected is ever removed.
            ac = _get_anthropic_client()
            ac.messages.batches.delete(bid)
            removed += 1
            log.info("Anthropic batch deleted at the provider (stored "
                     "results removed): %s", bid)
        elif route == "google-vertex":
            gc = _require_google()
            j = gc.batches.get(name=bid)
            sess = _gcs_session()
            for uri in (getattr(j.src, "gcs_uri", None) if j.src else None,
                        getattr(j.dest, "gcs_uri", None) if j.dest else None):
                if uri:
                    uris = uri if isinstance(uri, list) else [uri]
                    for u in uris:
                        b, p = _split_gs_uri(u)
                        removed += _gcs_delete_prefix(sess, b, p)
        elif route == "google-devapi":
            gc = _require_google()
            j = gc.batches.get(name=bid)
            fnames = []
            for obj in (j.src, j.dest):
                fn = getattr(obj, "file_name", None) if obj else None
                if fn:
                    fnames.append(fn)
            # [PATCH:batch] v2.12 (WO-B1-2 Task 1): the job object does NOT
            # echo the uploaded input file (src comes back None), so find it
            # by the display name the submit path gave it and delete it
            # instead of waiting out the ~48-hour Files API retention.
            disp = getattr(j, "display_name", "") or ""
            if disp.startswith("ledger-"):
                want = "ledger-batch-" + disp[len("ledger-"):]
                try:
                    for f in gc.files.list():
                        if getattr(f, "display_name", None) == want:
                            fnames.append(f.name)
                except Exception:  # noqa: BLE001 - listing is best-effort
                    pass
            for fname in dict.fromkeys(fnames):
                try:
                    gc.files.delete(name=fname)
                    removed += 1
                    log.info("Deleted provider file %s.", fname)
                except Exception as exc:  # noqa: BLE001
                    # The RESULT file's id exceeds the API's 40-character
                    # delete limit, so Google refuses to delete it; it
                    # expires with the ~48-hour Files API retention.
                    log.info("Provider file %s not deletable (%s); it "
                             "expires with the ~48 h Files API retention.",
                             fname, str(exc)[:120])
        log.info("Batch cleanup %s: %d object(s)/file(s) removed.", bid, removed)
        return jsonify({"status": "success", "removed": removed})
    except Exception as exc:  # noqa: BLE001
        log.exception("batch/cleanup failed")
        return jsonify({"status": "error", "message": str(exc)}), 500


if __name__ == "__main__":
    log.info("Ledger AI daemon (v2.12, multi-provider + direct Anthropic API route + multi-image Silo 2 + Google-optional + vendor-default sampling + per-chunk master context + Gemini Developer-API route + formatter endpoints + batch endpoints) listening on http://%s:%d", HOST, PORT)
    log.info("Google route for this box: %s", _google_route)
    log.info("Claude route for this box: %s", _anthropic_route)
    serve(app, host=HOST, port=PORT, threads=4)
