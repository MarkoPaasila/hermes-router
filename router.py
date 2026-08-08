#!/usr/bin/env python3
"""
hermes-router — Free-tier AI load balancer with automatic key rotation.

A lightweight OpenAI-compatible proxy that:
  - Rotates across multiple API keys per provider automatically
  - Cascades to the next provider when one is exhausted or rate-limited
  - Strips thinking/reasoning fields that break non-Claude providers
  - Handles 413 (payload too large) by cascading instead of crashing
  - Caches identical responses to preserve free-tier quota
  - Routes short requests to low-latency providers first (optional)
  - Tracks per-provider latency and error rates

Supported providers (configure via .env or auth.json):
  Free:  Gemini · OpenRouter · SambaNova · GitHub Models · Cerebras · Groq · Mistral · Cohere · Z.ai · Naga · NVIDIA NIM · Hugging Face
  Paid:  OpenAI · Anthropic
  Subscription (OAuth): Codex (ChatGPT) — via `hr auth import-codex`

Quick start:
  pip install -r requirements.txt
  cp .env.example .env   # add your API keys
  python router.py
"""

import atexit
import json, os, time, threading, logging, hashlib, hmac, re, subprocess, secrets
from pathlib import Path
from collections import deque, OrderedDict, defaultdict
from flask import Flask, request, jsonify, Response, stream_with_context, redirect
import requests
from rate_limiter import AdaptiveRateLimiter, RATE_HEADROOM_THRESHOLD, RATE_EXHAUSTED_WAIT_S, RATE_ADMIT_WAIT_S
from token_caps import (
    TokenCapTracker,
    classify_token_limit_error,
    extract_caps_from_model_item,
)
from session_sticky import SessionStickyStore, resolve_session_id
from cascade_trail import CascadeTrail, http_reason
from ttft_baseline import (
    TtftBaselineStore,
    TtftDeadlineExceeded,
    abort_enabled as ttft_abort_enabled,
)
import gi_ranking

# ── Config ─────────────────────────────────────────────────────────────────────

def _load_env(path: str = ".env"):
    """Load key=value pairs from a .env file into os.environ (no-op if missing)."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

_load_env()

logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("hermes-router")

# Shared HTTP session — reuses TCP/TLS connections to each provider host across
# requests (HTTP keep-alive), so we don't pay a fresh ~100–300ms handshake on
# every call. Thread-safe for sending; pool_maxsize covers our worker threads.
# max_retries=0 because the cascade handles retries, not urllib3.
_HTTP = requests.Session()
_http_adapter = requests.adapters.HTTPAdapter(
    pool_connections=20,
    pool_maxsize=max(32, int(os.environ.get("WORKER_THREADS", 16)) * 2),
    max_retries=0,
)
_HTTP.mount("https://", _http_adapter)
_HTTP.mount("http://", _http_adapter)

PORT              = int(os.environ.get("PORT", 8319))
# Bind address. Default 0.0.0.0 (needed for Docker port mapping). Set HOST=127.0.0.1
# to expose the router to localhost only — recommended on a shared/VPS host where
# you reach it via localhost or an SSH tunnel rather than a public port.
HOST              = os.environ.get("HOST", "0.0.0.0")

# Well-known placeholder values shipped in .env.example / the old hardcoded
# fallback. PROXY_API_KEYS now gates real config-write power (add provider keys,
# mint/revoke access keys, restart) via the dashboard, not just chat — so an
# install left on one of these would share a publicly-documented credential with
# every other install that never edited it. See _ensure_real_proxy_key below.
_KNOWN_DEFAULT_PROXY_KEYS = {"sk-router-1", "sk-my-router-key-1"}


def _ensure_real_proxy_key(env_path: str = ".env") -> list[str]:
    """If no PROXY_API_KEYS is set, or it's still one of the placeholder values
    above, generate a real random key and persist it to .env — so every install
    gets a unique dashboard/API secret on first boot without the operator needing
    to remember to change it. A no-op once a real key is in place."""
    raw = os.environ.get("PROXY_API_KEYS", "").strip()
    current = [k.strip() for k in raw.split(",") if k.strip()]
    if current and not all(k in _KNOWN_DEFAULT_PROXY_KEYS for k in current):
        return current   # already a real, user-set key (or keys) — leave it alone

    new_key = "sk-router-" + secrets.token_urlsafe(24)
    p = Path(env_path)
    lines = p.read_text().splitlines() if p.exists() else []
    found, out = False, []
    for line in lines:
        if line.strip().startswith("PROXY_API_KEYS="):
            out.append(f"PROXY_API_KEYS={new_key}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"PROXY_API_KEYS={new_key}")
    p.write_text("\n".join(out) + "\n")
    os.environ["PROXY_API_KEYS"] = new_key
    log.warning("=" * 72)
    log.warning("No unique proxy API key was configured — generated one and saved")
    log.warning(f"it to {env_path}. Use this to access the dashboard and the API:")
    log.warning(f"    {new_key}")
    log.warning("=" * 72)
    return [new_key]


PROXY_API_KEYS    = _ensure_real_proxy_key()
ROUTER_MODEL      = os.environ.get("ROUTER_MODEL_ID", "hermes-router")
CACHE_TTL         = int(os.environ.get("CACHE_TTL_SECONDS", 300))   # 0 = disabled
CACHE_MAX_SIZE    = int(os.environ.get("CACHE_MAX_SIZE", 100))
FAST_ROUTE_TOKENS = int(os.environ.get("FAST_ROUTE_THRESHOLD", 0))  # 0 = disabled
# Optional startup model discovery. Kept opt-in because some gateways list paid
# models alongside free ones, and some expose very large catalogs.
AUTO_DISCOVER_MODELS = os.environ.get("AUTO_DISCOVER_MODELS", "0").strip().lower() not in ("0", "", "false", "no", "off")
AUTO_DISCOVER_MODEL_LIMIT = max(1, int(os.environ.get("AUTO_DISCOVER_MODEL_LIMIT", "8")))
# When discovery is on, drop purpose-specific models (TTS/STT/image-gen/OCR/video/
# embedding/moderation/rerank) from discovered catalogs. Configured model lists
# are never filtered. Opt-in.
FILTER_SPECIALIZED_MODELS = os.environ.get("FILTER_SPECIALIZED_MODELS", "0").strip().lower() not in ("0", "", "false", "no", "off")
# Semantic cache: serve a cached answer for a *similar* (not just identical) prompt,
# by embedding prompts and comparing cosine similarity. Opt-in (needs an embedding
# provider); falls back to exact match when off or unavailable.
SEMANTIC_CACHE     = os.environ.get("SEMANTIC_CACHE", "0").strip().lower() not in ("0", "", "false", "no", "off")
SEMANTIC_THRESHOLD = float(os.environ.get("SEMANTIC_CACHE_THRESHOLD", "0.95"))
# Keys use key affinity (preferred key when ready, else first ready in deque order).
_raw_rotation = os.environ.get("ROTATION_MODE", "").strip()
if _raw_rotation:
    log.warning(f"ROTATION_MODE={_raw_rotation!r} is ignored; keys use key affinity")
STATE_FILE        = Path(os.environ.get("ROUTER_STATE_FILE", "./router_state.json"))
RATE_STATE_FILE   = Path(os.environ.get("RATE_STATE_FILE", "./rate_limits_state.json"))
TOKEN_CAPS_ENABLED = os.environ.get("TOKEN_CAPS", "1").strip().lower() not in (
    "0", "", "false", "no", "off",
)
TOKEN_CAPS_STATE_FILE = Path(
    os.environ.get("TOKEN_CAPS_STATE_FILE", "./token_caps_state.json")
)
STATE_TTL_HOURS   = int(os.environ.get("ROUTER_STATE_TTL_HOURS", 24))  # 0 = re-probe every start
AUTH_FILE         = Path(os.environ.get("ROUTER_AUTH_FILE", "./auth.json"))  # router's own key store
# In-memory request log: last N requests kept in a ring buffer. Pure RAM, no disk
# writes. Set REQUEST_LOG_SIZE=0 to disable. Exposed via GET /v1/logs.
REQUEST_LOG_SIZE  = max(0, int(os.environ.get("REQUEST_LOG_SIZE", "500")))


def _load_auth_json() -> dict[str, list[str]]:
    """Load provider API keys from auth.json — the router's own credential store,
    managed by `hr auth add`. This makes the router self-contained: keys live with
    the router, independent of any host application.

      Format: {"providers": {"openrouter": ["key1", "key2"], "gemini": ["key"]}}

    Returns {provider_name: [keys]}. A missing or invalid file is non-fatal —
    the router simply falls back to keys from .env (see _keys_for)."""
    if not AUTH_FILE.exists():
        return {}
    try:
        doc = json.loads(AUTH_FILE.read_text())
        out: dict[str, list[str]] = {}
        for name, keys in doc.get("providers", {}).items():
            if isinstance(keys, list):
                out[name] = [str(k).strip() for k in keys if str(k).strip()]
        return out
    except Exception as e:
        log.warning(f"Could not read {AUTH_FILE}: {e}")
        return {}

_AUTH_KEYS = _load_auth_json()


# ── Codex (ChatGPT OAuth) credentials ──────────────────────────────────────────
# Codex authenticates with ChatGPT-subscription OAuth tokens, not static API keys.
# Access tokens are short-lived JWTs; we mint fresh ones from the long-lived refresh
# token. Accounts live in auth.json under "codex_accounts" (written by
# `hr auth import-codex`), separate from the plain-string provider keys so the
# normal credential pool is untouched.
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"


def _jwt_exp(token: str) -> int:
    """Read the `exp` (unix seconds) claim from a JWT without verifying it."""
    try:
        import base64
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)        # pad base64url
        return int(json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0))
    except Exception:
        return 0


def _load_codex_accounts() -> list[dict]:
    """Load Codex accounts from auth.json's "codex_accounts" list."""
    if not AUTH_FILE.exists():
        return []
    try:
        doc = json.loads(AUTH_FILE.read_text())
        accts = doc.get("codex_accounts", [])
        return [a for a in accts if isinstance(a, dict)
                and a.get("refresh_token") and a.get("account_id")]
    except Exception as e:
        log.warning(f"Could not read codex accounts from {AUTH_FILE}: {e}")
        return []


class CodexCredentials:
    """Holds Codex OAuth accounts and hands out fresh access tokens, refreshing
    via the refresh token when a token is missing or near expiry. Refreshed
    tokens are persisted back to auth.json so they survive restarts."""
    REFRESH_SKEW = 300   # refresh this many seconds before the JWT expires

    def __init__(self, accounts: list[dict]):
        self.lock = threading.Lock()
        self.accounts = {a["account_id"]: dict(a) for a in accounts}

    def account_ids(self) -> list[str]:
        return list(self.accounts.keys())

    def get_access_token(self, account_id: str) -> str | None:
        """Return a valid access token for the account, refreshing if needed."""
        with self.lock:
            acct = self.accounts.get(account_id)
            if not acct:
                return None
            tok = acct.get("access_token", "")
            if tok and _jwt_exp(tok) - self.REFRESH_SKEW > time.time():
                return tok
            return self._refresh(acct)

    def _refresh(self, acct: dict) -> str | None:
        try:
            r = _HTTP.post(CODEX_TOKEN_URL, json={
                "client_id":     CODEX_CLIENT_ID,
                "grant_type":    "refresh_token",
                "refresh_token": acct["refresh_token"],
            }, timeout=30)
        except requests.exceptions.RequestException as e:
            log.error(f"  codex token refresh network error: {e}")
            return None
        if r.status_code != 200:
            log.error(f"  codex token refresh failed: HTTP {r.status_code}")
            return None
        data = r.json()
        acct["access_token"] = data.get("access_token", acct.get("access_token", ""))
        if data.get("refresh_token"):           # refresh tokens can rotate
            acct["refresh_token"] = data["refresh_token"]
        acct["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._persist()
        log.info(f"  codex token refreshed for account ...{acct['account_id'][-6:]}")
        return acct["access_token"]

    def _persist(self):
        """Write current accounts back to auth.json (best-effort, 0600)."""
        try:
            doc = json.loads(AUTH_FILE.read_text()) if AUTH_FILE.exists() else {}
        except Exception:
            doc = {}
        if not isinstance(doc, dict):
            doc = {}
        doc["codex_accounts"] = list(self.accounts.values())
        try:
            AUTH_FILE.write_text(json.dumps(doc, indent=2) + "\n")
            os.chmod(AUTH_FILE, 0o600)
        except Exception as e:
            log.warning(f"  could not persist codex tokens: {e}")


codex_creds = CodexCredentials(_load_codex_accounts())


# Circuit-breaker knobs — a provider that fails health repeatedly is tripped out
# of rotation for a cooldown, then probed again (half-open). Overridable via env.
BREAKER_WINDOW      = int(os.environ.get("BREAKER_WINDOW", 8))          # recent outcomes to weigh
BREAKER_MIN_SAMPLES = int(os.environ.get("BREAKER_MIN_SAMPLES", 4))     # min samples before it can trip
BREAKER_ERROR_RATE  = float(os.environ.get("BREAKER_ERROR_RATE", 0.5))  # trip at >= this health-fail fraction
BREAKER_COOLDOWN    = int(os.environ.get("BREAKER_COOLDOWN", 60))       # seconds the breaker stays open

# Providers known for low-latency inference — promoted for short requests
_FAST_PROVIDERS = {"groq", "cerebras", "sambanova", "mistral"}

# ── Selection: general intelligence ranking (GI, 0–100) ───────────────────────
# Higher = stronger. Complexity maps to a minimum GI threshold; pick cheapest
# eligible. See CONTEXT.md / ADR-0002. Snapshot: gi_rankings.json.
CAPABILITY_SCALE_VERSION = 2  # legacy probe-state migration only (old rating field)
_COMPLEXITY_LABELS = {1: "trivial", 2: "simple", 3: "standard", 4: "complex", 5: "critical"}

# Approximate list prices (USD per 1M tokens) as (input, output), for cost
# ESTIMATION only. Substring match like _rate_model (longest key wins). Anything
# not listed — every free provider, and subscription plans like Codex (ChatGPT)
# and the Kimi coding plan — is treated as $0. Prices drift; treat as estimates
# and override/extend with MODEL_PRICES_FILE (JSON: {"model-substr": [in, out]}).
MODEL_PRICES: dict = {
    "gpt-4o-mini":      (0.15, 0.60),
    "gpt-4o":           (2.50, 10.00),
    "gpt-4.1-mini":     (0.40, 1.60),
    "gpt-4.1":          (2.00, 8.00),
    "o1-mini":          (1.10, 4.40),
    "o1":               (15.00, 60.00),
    "o3-mini":          (1.10, 4.40),
    "claude-opus":      (15.00, 75.00),
    "claude-sonnet":    (3.00, 15.00),
    "claude-3-5-haiku": (0.80, 4.00),
    "claude-haiku":     (0.80, 4.00),
    "mistral-large":    (2.00, 6.00),
    "mistral-medium":   (0.40, 2.00),
    "mistral-small":    (0.10, 0.30),
    "command-a":        (2.50, 10.00),
    "command-r-plus":   (2.50, 10.00),
    "command-r":        (0.15, 0.60),
    "kimi-k2":          (0.60, 2.50),
}

_provider_state: dict = {}   # populated at startup by _initialize_ratings()
# Per-(provider, model) feature probe state (tools/reasoning). Keyed by
# (provider_name, model). GI strength comes from gi_ranking, not this dict.
_model_state: dict = {}


def _keys(env_var: str) -> list[str]:
    """Collect all keys for a provider from three naming conventions (combined + de-duped):
      1. Singular:  MISTRAL_API_KEY=k1
      2. Plural:    MISTRAL_API_KEYS=k1,k2,k3   (comma-separated)
      3. Numbered:  MISTRAL_API_KEY_2=k2, MISTRAL_API_KEY_3=k3, ...
    The plural form is the canonical multi-key env var; singular and numbered are
    convenience aliases that are merged in automatically.
    """
    collected = []
    # singular (drop the trailing S if the caller passed the plural form)
    singular = env_var[:-1] if env_var.endswith("S") else env_var
    if singular != env_var:
        single = os.environ.get(singular, "").strip()
        if single:
            collected.append(single)
    # plural / comma-separated
    for piece in os.environ.get(env_var, "").split(","):
        piece = piece.strip()
        if piece:
            collected.append(piece)
    # numbered suffixes on the singular name (_2, _3, ...)
    i = 2
    while True:
        nv = os.environ.get(f"{singular}_{i}", "").strip()
        if not nv:
            break
        collected.append(nv)
        i += 1
    seen, out = set(), []
    for k in collected:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _keys_for(provider_name: str, env_var: str) -> list[str]:
    """All keys for a provider: auth.json entries first (the primary store that
    `hr auth add` writes to), then any matching .env keys as a fallback. Deduped,
    order preserved. A provider with keys in EITHER source is enabled."""
    merged = list(_AUTH_KEYS.get(provider_name, []))
    merged += _keys(env_var)
    seen, out = set(), []
    for k in merged:
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _int_env(env_var: str, default: int = 0) -> int:
    """Parse an integer env var, falling back to default on missing/invalid."""
    try:
        return int(os.environ.get(env_var, default))
    except (TypeError, ValueError):
        return default


# ── Per-provider exclude list ─────────────────────────────────────────────────


def _excluded_models(provider_name: str) -> set[str]:
    """Case-insensitive exact model IDs listed in {PROVIDER}_EXCLUDE_MODELS.

    Excluded models are stripped from a provider's active roster whether
    they come from config or auto-discovery.
    """
    raw = os.environ.get(f"{provider_name.upper()}_EXCLUDE_MODELS", "")
    return {m.strip().lower() for m in raw.split(",") if m.strip()}


def _filter_excluded(provider_name: str, models: list[str]) -> list[str]:
    """Drop models blocked by {PROVIDER}_EXCLUDE_MODELS (exact, case-insensitive)."""
    excl = _excluded_models(provider_name)
    if not excl:
        return models
    return [m for m in models if m.lower() not in excl]


def _exclude_env_key(provider_name: str) -> str:
    return f"{provider_name.upper()}_EXCLUDE_MODELS"


def _exclude_list_raw(provider_name: str) -> list[str]:
    """Ordered exclude IDs for a provider, preserving casing from os.environ."""
    raw = os.environ.get(_exclude_env_key(provider_name), "")
    return [m.strip() for m in raw.split(",") if m.strip()]


def _persist_exclude_list(provider_name: str, items: list[str]) -> None:
    """Write {PROVIDER}_EXCLUDE_MODELS to .env and os.environ (delete when empty)."""
    key = _exclude_env_key(provider_name)
    if items:
        value = ",".join(items)
        _env_write_line(key, value)
        os.environ[key] = value
    else:
        _env_write_line(key, None)
        os.environ.pop(key, None)


def _exclude_list_add(provider_name: str, model: str) -> list[str]:
    """Append model to the provider exclude list (case-insensitive dedupe)."""
    items = _exclude_list_raw(provider_name)
    lower = model.lower()
    if any(m.lower() == lower for m in items):
        return items
    items = items + [model]
    _persist_exclude_list(provider_name, items)
    return items


def _exclude_list_remove(provider_name: str, model: str) -> list[str]:
    """Remove model from the provider exclude list (case-insensitive)."""
    lower = model.lower()
    items = [m for m in _exclude_list_raw(provider_name) if m.lower() != lower]
    _persist_exclude_list(provider_name, items)
    return items


def _apply_model_block_live(provider_name: str, model: str, blocked: bool) -> None:
    """Mutate the live PROVIDERS roster (and key pool on unblock) immediately."""
    lower = model.lower()
    for p in PROVIDERS:
        if p["name"] != provider_name:
            continue
        models = list(p.get("models") or [])
        if blocked:
            kept = [m for m in models if m.lower() != lower]
            if models and not kept:
                log.warning(f"{provider_name}: all models excluded via "
                            f"{_exclude_env_key(provider_name)} — provider has no usable models")
            p["models"] = kept
            p["model"] = kept[0] if kept else ""
        else:
            if not any(m.lower() == lower for m in models):
                models.append(model)
            p["models"] = models
            if not p.get("model") and models:
                p["model"] = models[0]
            pool.ensure_model(provider_name, model, list(p.get("keys") or []))
        return


def _set_model_excluded(provider_name: str, model: str, blocked: bool) -> list[str]:
    """Persist exclude change to .env first, then apply to the live roster.

    Returns the provider's exclude list after the change. Raises on .env write
    failure without mutating PROVIDERS.
    """
    if blocked:
        items = _exclude_list_add(provider_name, model)
    else:
        items = _exclude_list_remove(provider_name, model)
    _apply_model_block_live(provider_name, model, blocked)
    return items


def _all_excluded_models() -> list[dict]:
    """All (provider, model) pairs currently listed in *_EXCLUDE_MODELS."""
    names = set(PROVIDER_MODEL_ENV.keys()) | {p["name"] for p in PROVIDERS}
    out: list[dict] = []
    for name in sorted(names):
        for mid in _exclude_list_raw(name):
            out.append({"provider": name, "model": mid})
    return out


# Substrings that mark purpose-specific (non-chat) models when catalog metadata
# does not explicitly classify the item. Avoid bare "image" — vision chat models
# use that word too.
_SPECIALIZED_NAME_PATTERNS = (
    "whisper", "tts", "speech", "audio", "imagen", "dall-e", "dalle", "flux",
    "ocr", "embed", "embedding", "moderation", "rerank", "video",
    "deep-research", "robotics", "lyria", "nano-banana", "aqa",
)


def _metadata_specialization(item: dict | None) -> str | None:
    """Return 'specialized', 'chat', or None if metadata is absent/unclear."""
    if not isinstance(item, dict):
        return None
    blobs: list[str] = []
    arch = item.get("architecture")
    if isinstance(arch, dict):
        for key in ("modality", "output_modalities", "input_modalities"):
            val = arch.get(key)
            if isinstance(val, str):
                blobs.append(val.lower())
            elif isinstance(val, list):
                blobs.extend(str(v).lower() for v in val)
    for key in ("type", "object", "task"):
        val = item.get(key)
        if isinstance(val, str):
            blobs.append(val.lower())
    caps = item.get("capabilities")
    if isinstance(caps, str):
        blobs.append(caps.lower())
    elif isinstance(caps, list):
        blobs.extend(str(c).lower() for c in caps)
    elif isinstance(caps, dict):
        blobs.extend(str(k).lower() for k, v in caps.items() if v)

    if not blobs:
        return None
    joined = " ".join(blobs)

    specialized_tokens = (
        "embedding", "embeddings", "tts", "speech", "audio", "asr", "stt",
        "whisper", "moderation", "rerank", "reranking", "ocr", "video",
        "image-generation", "image_generation", "text->image", "text→image",
        "->image", "→image", "->embedding", "→embedding", "->audio", "→audio",
        "deep-research", "robotics", "lyria", "nano-banana", "aqa",
    )
    if any(tok in joined for tok in specialized_tokens):
        # text+image->text is vision chat, not image generation
        if "->image" in joined or "→image" in joined:
            return "specialized"
        if any(tok in joined for tok in (
            "embedding", "embeddings", "tts", "speech", "audio", "asr", "stt",
            "whisper", "moderation", "rerank", "reranking", "ocr", "video",
            "image-generation", "image_generation",
            "deep-research", "robotics", "lyria", "nano-banana", "aqa",
        )):
            return "specialized"

    chat_tokens = (
        "text->text", "text→text", "text+image->text", "text+image→text",
        "chat", "completion", "language",
    )
    if any(tok in joined for tok in chat_tokens) or (
        "text" in joined and "embedding" not in joined and "->image" not in joined
        and "→image" not in joined
    ):
        # output_modalities including text (and not only specialized outputs)
        if "embeddings" in joined or "embedding" in joined:
            return "specialized"
        return "chat"
    return None


def _is_specialized_model(model_id: str, item: dict | None = None) -> bool:
    """True when a catalog model is purpose-specific (not chat completions).

    Detection order: explicit specialized metadata → drop; explicit chat/text
    metadata → keep; otherwise name-pattern denylist.
    """
    kind = _metadata_specialization(item)
    if kind == "specialized":
        return True
    if kind == "chat":
        return False
    mn = (model_id or "").lower()
    return any(p in mn for p in _SPECIALIZED_NAME_PATTERNS)


# ── Unsuitable-model cooldown (in-memory exponential backoff) ─────────────────
# 404 / model-not-found style failures cool a (provider, model) so later requests
# skip it. Payload-shaped 400s do not. 429 stays on the TBF path. Not persisted.

_UNSUITABLE_MODEL_BASE_S = float(os.environ.get("UNSUITABLE_MODEL_BASE_S", "60") or 60)
_UNSUITABLE_MODEL_CAP_S = float(os.environ.get("UNSUITABLE_MODEL_CAP_S", "3600") or 3600)

_UNSUITABLE_400_RE = re.compile(
    r"model\s+not\s+found|"
    r"unknown\s+model|"
    r"model\s+(?:is\s+)?not\s+supported|"
    r"not\s+supported\s+for\s+this\s+(?:endpoint|api)|"
    r"(?:does\s+not\s+exist|not\s+exist).*model|"
    r"model.*(?:does\s+not\s+exist|not\s+exist)|"
    r"\bnot_found\b|"
    r"no\s+such\s+model",
    re.I,
)


def _is_unsuitable_model_error(status_code: int, body: str = "") -> bool:
    """True when the upstream error means this model should cool down.

    404 is always unsuitable. 400 only when the body looks like model-missing /
    wrong-endpoint — not payload/schema/reasoning-replay errors. 429 and others
    are never unsuitable here.
    """
    if status_code == 404:
        return True
    if status_code != 400:
        return False
    return bool(_UNSUITABLE_400_RE.search(body or ""))


class UnsuitableModelCooldown:
    """Per-(provider, model) exponential backoff for unsuitable upstream errors."""

    def __init__(self, base_s: float = _UNSUITABLE_MODEL_BASE_S,
                 cap_s: float = _UNSUITABLE_MODEL_CAP_S):
        self.base_s = float(base_s)
        self.cap_s = float(cap_s)
        self._lock = threading.Lock()
        # (provider, model) -> {"failures": int, "cool_until": float}
        self._state: dict[tuple[str, str], dict] = {}

    def _now(self) -> float:
        return time.time()

    def _delay(self, failures: int) -> float:
        if failures <= 0:
            return 0.0
        return min(self.cap_s, self.base_s * (2 ** (failures - 1)))

    def is_cooling(self, provider: str, model: str) -> bool:
        with self._lock:
            st = self._state.get((provider, model))
            if not st:
                return False
            return self._now() < float(st.get("cool_until") or 0)

    def ready_in(self, provider: str, model: str) -> float:
        with self._lock:
            st = self._state.get((provider, model))
            if not st:
                return 0.0
            return max(0.0, float(st.get("cool_until") or 0) - self._now())

    def failures(self, provider: str, model: str) -> int:
        with self._lock:
            st = self._state.get((provider, model))
            return int(st.get("failures") or 0) if st else 0

    def record(self, provider: str, model: str) -> float:
        """Record an unsuitable failure; return the new cool-down delay in seconds."""
        with self._lock:
            key = (provider, model)
            st = self._state.get(key) or {"failures": 0, "cool_until": 0.0}
            st["failures"] = int(st.get("failures") or 0) + 1
            delay = self._delay(st["failures"])
            st["cool_until"] = self._now() + delay
            self._state[key] = st
            return delay

    def clear(self, provider: str, model: str) -> None:
        with self._lock:
            self._state.pop((provider, model), None)


unsuitable_models = UnsuitableModelCooldown()


# ── Provider definitions ───────────────────────────────────────────────────────

def _build_providers() -> list[dict]:
    providers = []

    gemini_keys = _keys_for("gemini", "GEMINI_API_KEYS")
    if gemini_keys:
        providers.append({
            "name":     "gemini",
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
            "model":    os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite"),
            "keys":     gemini_keys,
        })

    openrouter_keys = _keys_for("openrouter", "OPENROUTER_API_KEYS")
    if openrouter_keys:
        providers.append({
            "name":     "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "model":    os.environ.get("OPENROUTER_MODEL", "nvidia/nemotron-3-super-120b-a12b:free"),
            "keys":     openrouter_keys,
            "headers":  {
                "HTTP-Referer": os.environ.get("OPENROUTER_SITE_URL", "https://github.com/Shaf2665/hermes-router"),
                "X-Title":      os.environ.get("OPENROUTER_APP_NAME", "hermes-router"),
            },
        })

    sambanova_keys = _keys_for("sambanova", "SAMBANOVA_API_KEYS")
    if sambanova_keys:
        providers.append({
            "name":     "sambanova",
            "base_url": "https://api.sambanova.ai/v1",
            "model":    os.environ.get("SAMBANOVA_MODEL", "DeepSeek-V3.2"),
            "keys":     sambanova_keys,
        })

    github_keys = _keys_for("github_models", "GITHUB_MODELS_TOKENS")
    if github_keys:
        providers.append({
            "name":     "github_models",
            "base_url": "https://models.inference.ai.azure.com",
            "model":    os.environ.get("GITHUB_MODELS_MODEL", "gpt-4o"),
            "keys":     github_keys,
        })

    cerebras_keys = _keys_for("cerebras", "CEREBRAS_API_KEYS")
    if cerebras_keys:
        providers.append({
            "name":     "cerebras",
            "base_url": "https://api.cerebras.ai/v1",
            "model":    os.environ.get("CEREBRAS_MODEL", "gpt-oss-120b"),
            "keys":     cerebras_keys,
        })

    groq_keys = _keys_for("groq", "GROQ_API_KEYS")
    if groq_keys:
        providers.append({
            "name":     "groq",
            "base_url": "https://api.groq.com/openai/v1",
            "model":    os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile"),
            "keys":     groq_keys,
        })

    mistral_keys = _keys_for("mistral", "MISTRAL_API_KEYS")
    if mistral_keys:
        providers.append({
            "name":     "mistral",
            "base_url": "https://api.mistral.ai/v1",
            "model":    os.environ.get("MISTRAL_MODEL", "mistral-medium-latest"),
            "keys":     mistral_keys,
        })

    cohere_keys = _keys_for("cohere", "COHERE_API_KEYS")
    if cohere_keys:
        providers.append({
            "name":     "cohere",
            "base_url": "https://api.cohere.ai/compatibility/v1",
            "model":    os.environ.get("COHERE_MODEL", "command-a-03-2025"),
            "keys":     cohere_keys,
        })

    zai_keys = _keys_for("zai", "GLM_API_KEYS")
    if zai_keys:
        providers.append({
            "name":     "zai",
            "base_url": "https://api.z.ai/api/paas/v4",
            "model":    os.environ.get("ZAI_MODEL", "glm-4.5-flash"),
            "keys":     zai_keys,
        })

    naga_keys = _keys_for("naga", "NAGA_API_KEYS")
    if naga_keys:
        providers.append({
            "name":     "naga",
            "base_url": "https://api.naga.ac/v1",
            "model":    os.environ.get("NAGA_MODEL", "nemotron-3-super-120b-a12b:free"),
            "keys":     naga_keys,
        })

    nvidia_keys = _keys_for("nvidia", "NVIDIA_API_KEYS")
    if nvidia_keys:
        providers.append({
            "name":     "nvidia",
            "base_url": "https://integrate.api.nvidia.com/v1",
            "model":    os.environ.get("NVIDIA_MODEL", "deepseek-ai/deepseek-v4-flash"),
            "keys":     nvidia_keys,
        })

    # Hugging Face Inference Providers — one OpenAI-compatible endpoint fronting
    # 45k+ models across many partners. Free accounts get a small monthly credit
    # ($0.10; PRO $2), so it exhausts faster than the request-quota free tiers —
    # the `:cheapest` suffix routes to the cheapest partner to stretch it. Use a
    # token from huggingface.co/settings/tokens (with Inference Providers access).
    huggingface_keys = _keys_for("huggingface", "HUGGINGFACE_API_KEYS")
    if huggingface_keys:
        providers.append({
            "name":     "huggingface",
            "base_url": "https://router.huggingface.co/v1",
            "model":    os.environ.get("HUGGINGFACE_MODEL", "openai/gpt-oss-120b:cheapest"),
            "keys":     huggingface_keys,
        })

    # Kimi (Moonshot) — the "Kimi coding plan" subscription exposes an
    # OpenAI-compatible endpoint and authenticates with a normal API key (sk-...),
    # so it drops in like any other provider. Model id `kimi-for-coding`. Get a key
    # from platform.kimi.ai / platform.moonshot.ai.
    kimi_keys = _keys_for("kimi", "KIMI_API_KEYS")
    if kimi_keys:
        providers.append({
            "name":     "kimi",
            "base_url": os.environ.get("KIMI_BASE_URL", "https://api.kimi.com/coding/v1"),
            "model":    os.environ.get("KIMI_MODEL", "kimi-for-coding"),
            "keys":     kimi_keys,
        })

    # OpenCode Zen — an OpenAI-compatible gateway for coding models with a pool of
    # genuinely FREE models (default below). One API key (`hr auth add opencode`);
    # paid premium models (claude/gpt/gemini/…) are reachable too via OPENCODE_MODEL.
    opencode_keys = _keys_for("opencode", "OPENCODE_API_KEYS")
    if opencode_keys:
        providers.append({
            "name":     "opencode",
            "base_url": os.environ.get("OPENCODE_BASE_URL", "https://opencode.ai/zen/v1"),
            "model":    os.environ.get("OPENCODE_MODEL",
                        "deepseek-v4-flash-free,minimax-m3-free,qwen3.6-plus-free"),
            "keys":     opencode_keys,
        })

    # OpenCode Go — the same OpenCode key + an OpenAI-compatible endpoint, but the
    # low-cost subscription tier ($5 first month, then $10/mo). Enabled only when an
    # `opencode_go` key is configured (signals you've turned on Go billing), so it
    # never adds dead attempts before you subscribe.
    opencode_go_keys = _keys_for("opencode_go", "OPENCODE_GO_API_KEYS")
    if opencode_go_keys:
        providers.append({
            "name":     "opencode_go",
            "base_url": os.environ.get("OPENCODE_GO_BASE_URL", "https://opencode.ai/zen/go/v1"),
            "model":    os.environ.get("OPENCODE_GO_MODEL", "deepseek-v4-flash,minimax-m3"),
            "keys":     opencode_go_keys,
        })

    openai_keys = _keys_for("openai", "OPENAI_API_KEYS")
    if openai_keys:
        providers.append({
            "name":     "openai",
            "base_url": "https://api.openai.com/v1",
            "model":    os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
            "keys":     openai_keys,
        })

    anthropic_keys = _keys_for("anthropic", "ANTHROPIC_API_KEYS")
    if anthropic_keys:
        providers.append({
            "name":     "anthropic",
            "base_url": "https://api.anthropic.com/v1",
            "model":    os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
            "keys":     anthropic_keys,
            "protocol": "anthropic",   # triggers format translation in forward()
        })

    # Codex — ChatGPT-subscription OAuth (not API keys). Accounts come from
    # `hr auth import-codex` and are keyed by account_id; forward() resolves each
    # to a fresh access token. Speaks the Responses API, so it needs translation.
    codex_ids = codex_creds.account_ids()
    if codex_ids:
        providers.append({
            "name":     "codex",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "model":    os.environ.get("CODEX_MODEL", "gpt-5.5"),
            "keys":     codex_ids,     # account_ids, resolved to tokens at send time
            "protocol": "codex",
        })

    # Local model — Ollama / LM Studio / llama.cpp / any OpenAI-compatible server
    # running on your own machine. Free, private, and fast. Local servers are
    # keyless, but the rotation pool needs ≥1 entry, so we use a sentinel key
    # (LOCAL_API_KEY, default "local") that forward() sends as a harmless Bearer
    # header the server ignores. Enabled by setting LOCAL_BASE_URL or LOCAL_MODEL.
    if os.environ.get("LOCAL_BASE_URL") or os.environ.get("LOCAL_MODEL"):
        providers.append({
            "name":     "local",
            "base_url": os.environ.get("LOCAL_BASE_URL", "http://localhost:11434/v1"),
            "model":    os.environ.get("LOCAL_MODEL", "llama3.1"),
            "keys":     [os.environ.get("LOCAL_API_KEY", "local")],
        })

    if not providers:
        log.warning("No providers configured — set GEMINI_API_KEYS, OPENROUTER_API_KEYS, etc. in .env")

    # Multi-model support: a provider's model string may be a comma-separated list
    # (e.g. GEMINI_MODEL=gemini-2.5-flash-lite,gemini-2.5-flash). Free-tier rate
    # limits are per-model, so the router fails over across a provider's models —
    # each its own quota bucket — before cascading to the next provider. The first
    # entry is the "primary" model used for probing, rating, and status display.
    for p in providers:
        models = [m.strip() for m in str(p.get("model", "")).split(",") if m.strip()]
        models = models or [p.get("model", "")]
        filtered = _filter_excluded(p["name"], models)
        if models and not filtered:
            log.warning(f"{p['name']}: all models excluded via "
                        f"{p['name'].upper()}_EXCLUDE_MODELS — provider has no usable models")
        p["models"] = filtered
        p["model"]  = filtered[0] if filtered else ""

    # Per-provider "skip when the request is too big" ceiling. Some free tiers
    # reject large payloads outright, so trying them with a big prompt just wastes
    # a round-trip before cascading. When the estimated request size exceeds a
    # provider's ceiling, that provider is skipped entirely.
    #   Configure via  {PROVIDER}_SKIP_TOKENS_OVER  (0 = never skip).
    # Defaults match each free tier's known limit:
    #   • groq          ~6000 TPM → 413
    #   • sambanova     DeepSeek-V3.2 here caps at 32K context → 400
    #   • github_models gpt-4o free tier ~8K input-token limit → 413
    _skip_defaults = {"groq": 5500, "sambanova": 30000, "github_models": 6000}
    for p in providers:
        env_var = f"{p['name'].upper()}_SKIP_TOKENS_OVER"
        p["skip_if_tokens_over"] = _int_env(env_var, _skip_defaults.get(p["name"], 0))

    # Per-provider output-token ceiling. Some providers 400 the whole request when
    # max_tokens exceeds their output cap, so we clamp it down in forward().
    #   Configure via  {PROVIDER}_MAX_OUTPUT_TOKENS  (0 = no clamp).
    #   • cohere        command-a caps output at 8192
    #   • github_models gpt-4o here rejects very large max_tokens (e.g. 65536)
    _max_out_defaults = {"cohere": 8192, "github_models": 16384}
    for p in providers:
        env_var = f"{p['name'].upper()}_MAX_OUTPUT_TOKENS"
        p["max_output_tokens"] = _int_env(env_var, _max_out_defaults.get(p["name"], 0))

    # Per-provider embedding model. Only providers with a non-empty embed model
    # take part in /v1/embeddings routing (OpenRouter, Groq, etc. are chat-only).
    # Each uses the same base_url with an /embeddings path; the wire format is
    # OpenAI-compatible, so no translation is needed. Configure or enable more
    # via {PROVIDER}_EMBED_MODEL (empty string disables a provider for embeds).
    # NVIDIA is intentionally omitted: its embedding models are "asymmetric" and
    # require an input_type (query/passage) parameter that the OpenAI embeddings
    # format doesn't carry, so they can't be served by clean passthrough. Enable
    # one explicitly with NVIDIA_EMBED_MODEL if you know it accepts OpenAI format.
    _embed_defaults = {
        "gemini":  "gemini-embedding-001",
        "mistral": "mistral-embed",
        "openai":  "text-embedding-3-small",
        "cohere":  "embed-v4.0",
    }
    for p in providers:
        env_var = f"{p['name'].upper()}_EMBED_MODEL"
        p["embed_model"] = os.environ.get(env_var, _embed_defaults.get(p["name"], ""))

    return providers


PROVIDERS = _build_providers()

# Providers whose /models endpoint mixes paid models in with the free ones.
# When auto-discovering a replacement model for these, restrict to :free ids so
# a probe can never silently promote the router onto a paid model.
_FREE_ONLY_DISCOVERY = {"openrouter", "naga", "opencode"}
_MODEL_DISCOVERY_SKIP = {"anthropic", "codex", "local", "huggingface"}


def _is_free_model_id(model: str) -> bool:
    m = (model or "").lower()
    return m.endswith(":free") or m.endswith("-free") or "/free" in m

# ── Config-write support (web dashboard "Add key" / "Set model" / add-on toggles) ──
# Mirrors the canonical provider lists + env-var mappings already used by the `hr`
# CLI scripts (scripts/auth.sh, scripts/model.sh), so the dashboard and CLI agree
# on what's valid. Kept as plain data here (not shelling out to bash) so it works
# identically in Docker, where those scripts aren't necessarily present.

# Providers that take a plain API key (excludes "codex" — OAuth via `hr auth
# import-codex` — and "local", which is keyless).
KEY_SETTABLE_PROVIDERS = [
    "gemini", "openrouter", "sambanova", "github_models", "cerebras", "groq",
    "mistral", "cohere", "zai", "naga", "nvidia", "huggingface", "kimi",
    "opencode", "opencode_go", "openai", "anthropic",
]

# Providers whose model(s) can be overridden — a superset of the above (codex and
# local don't take a key here, but do have a settable model).
PROVIDER_MODEL_ENV = {
    "gemini": "GEMINI_MODEL", "openrouter": "OPENROUTER_MODEL",
    "sambanova": "SAMBANOVA_MODEL", "github_models": "GITHUB_MODELS_MODEL",
    "cerebras": "CEREBRAS_MODEL", "groq": "GROQ_MODEL", "mistral": "MISTRAL_MODEL",
    "cohere": "COHERE_MODEL", "zai": "ZAI_MODEL", "naga": "NAGA_MODEL",
    "nvidia": "NVIDIA_MODEL", "huggingface": "HUGGINGFACE_MODEL", "kimi": "KIMI_MODEL",
    "opencode": "OPENCODE_MODEL", "opencode_go": "OPENCODE_GO_MODEL",
    "openai": "OPENAI_MODEL", "anthropic": "ANTHROPIC_MODEL",
    "codex": "CODEX_MODEL", "local": "LOCAL_MODEL",
}

ENV_FILE_PATH = Path(os.environ.get("HR_ENV_FILE", ".env"))


def _env_read_line(key: str) -> str | None:
    """Current value of KEY in .env (last occurrence wins), or None if unset."""
    if not ENV_FILE_PATH.exists():
        return None
    val = None
    for line in ENV_FILE_PATH.read_text().splitlines():
        if line.strip().startswith(f"{key}="):
            val = line.split("=", 1)[1]
    return val


def _env_write_line(key: str, value: str | None) -> None:
    """Upsert (or, if value is None, delete) a KEY=VALUE line in .env, preserving
    every other line untouched. Mirrors scripts/model.sh's write_env / scripts/
    features.sh's set_env so the CLI and dashboard produce identical files."""
    lines = ENV_FILE_PATH.read_text().splitlines() if ENV_FILE_PATH.exists() else []
    found, out = False, []
    for line in lines:
        if line.strip().startswith(f"{key}="):
            found = True
            if value is not None:
                out.append(f"{key}={value}")
            # value is None → delete this line (skip appending it)
        else:
            out.append(line)
    if not found and value is not None:
        out.append(f"{key}={value}")
    ENV_FILE_PATH.write_text("\n".join(out) + "\n")


def _auth_json_add_key(provider: str, key: str) -> tuple[bool, int]:
    """Append `key` to auth.json's providers[provider] list (creating the file/
    section as needed). Returns (added, total_count) — added=False on duplicate.
    Mirrors scripts/auth.sh's append_key."""
    doc = {}
    if AUTH_FILE.exists():
        try:
            doc = json.loads(AUTH_FILE.read_text())
        except Exception:
            doc = {}
    if not isinstance(doc, dict):
        doc = {}
    providers = doc.setdefault("providers", {})
    keys = providers.setdefault(provider, [])
    if key in keys:
        return False, len(keys)
    keys.append(key)
    AUTH_FILE.write_text(json.dumps(doc, indent=2) + "\n")
    try:
        os.chmod(AUTH_FILE, 0o600)   # keys are secrets — owner read/write only
    except OSError:
        pass
    return True, len(keys)


# ── Proxy (access) key management ───────────────────────────────────────────────
# "Proxy keys" are the credential CALLERS use to authenticate to the router
# itself (PROXY_API_KEYS) — distinct from provider keys above, which the router
# uses to authenticate to upstream providers. Lets the dashboard mint new keys
# for teammates/other apps, with optional per-key budgets, without hand-editing
# .env/auth.json. Same proxy-key auth as every other /v1/config/* endpoint —
# this project has one flat admin tier, not per-key permission levels.

def _generate_proxy_key() -> str:
    """A new, cryptographically random proxy key. Shown once at creation time —
    only its last-6-char tail is ever displayed again, matching every other key
    in this codebase."""
    return "sk-router-" + secrets.token_urlsafe(24)


def _read_proxy_api_keys_live() -> list[str]:
    """Fresh-read PROXY_API_KEYS from .env (not the process's stale in-memory
    PROXY_API_KEYS global), so a just-created/revoked key is reflected in the
    dashboard immediately, before a restart makes it actually active."""
    raw = _env_read_line("PROXY_API_KEYS")
    if raw is None:
        return list(PROXY_API_KEYS)   # .env has no override line — use the running default
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    return keys or list(PROXY_API_KEYS)


def _read_proxy_keys_meta() -> dict:
    """Fresh-read auth.json's proxy_keys metadata (name + limits per key)."""
    if not AUTH_FILE.exists():
        return {}
    try:
        doc = json.loads(AUTH_FILE.read_text())
    except Exception:
        return {}
    pk = doc.get("proxy_keys", {})
    return pk if isinstance(pk, dict) else {}


def _write_proxy_key_meta(key: str, patch: dict) -> None:
    """Merge `patch` into auth.json's proxy_keys[key] (creating it if absent)."""
    doc = {}
    if AUTH_FILE.exists():
        try:
            doc = json.loads(AUTH_FILE.read_text())
        except Exception:
            doc = {}
    if not isinstance(doc, dict):
        doc = {}
    pk = doc.setdefault("proxy_keys", {})
    spec = pk.get(key, {})
    spec.update(patch)
    pk[key] = spec
    AUTH_FILE.write_text(json.dumps(doc, indent=2) + "\n")
    try:
        os.chmod(AUTH_FILE, 0o600)
    except OSError:
        pass


def _delete_proxy_key_meta(key: str) -> None:
    if not AUTH_FILE.exists():
        return
    try:
        doc = json.loads(AUTH_FILE.read_text())
    except Exception:
        return
    if not isinstance(doc, dict):
        return
    pk = doc.get("proxy_keys")
    if isinstance(pk, dict) and pk.pop(key, None) is not None:
        AUTH_FILE.write_text(json.dumps(doc, indent=2) + "\n")
        try:
            os.chmod(AUTH_FILE, 0o600)
        except OSError:
            pass


def _resolve_proxy_key_by_tail(tail: str, keys: list[str]) -> str | None:
    matches = [k for k in keys if k[-6:] == tail]
    return matches[0] if len(matches) == 1 else None


def _trigger_restart(delay_s: float = 1.2) -> None:
    """Restart the router shortly after this call returns, so the HTTP response
    triggering it has time to reach the client first. Delegates to the same,
    already-tested scripts/restart.sh used by `hr restart` (handles both the
    systemd and standalone-process cases) rather than duplicating that logic."""
    script = Path(__file__).resolve().parent / "scripts" / "restart.sh"
    if not script.exists():
        log.warning("restart requested but scripts/restart.sh not found — skipping")
        return

    def _go():
        try:
            subprocess.Popen(["/usr/bin/env", "bash", str(script)],
                              cwd=str(script.parent.parent),
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              start_new_session=True)
        except Exception as e:
            log.error(f"restart trigger failed: {e}")

    threading.Timer(delay_s, _go).start()

# ── Credential pool ────────────────────────────────────────────────────────────

# ── Smart routing helpers ─────────────────────────────────────────────────────

def _rate_model(model_name: str) -> float:
    """Effective GI for a model id (snapshot/default only — no provider override)."""
    return gi_ranking.resolve_gi("", model_name)[0]


def _apply_price_overrides():
    """Merge MODEL_PRICES_FILE (JSON {"model-substr": [in, out]}) over the built-in
    price table, so users can correct/extend prices without editing code."""
    path = os.environ.get("MODEL_PRICES_FILE")
    if not path or not os.path.exists(path):
        return
    try:
        doc = json.loads(Path(path).read_text())
        n = 0
        for k, v in (doc or {}).items():
            if isinstance(v, (list, tuple)) and len(v) == 2:
                MODEL_PRICES[k.lower()] = (float(v[0]), float(v[1])); n += 1
        log.info(f"Pricing: loaded {n} override(s) from {path}")
    except Exception as e:
        log.warning(f"Pricing: could not read MODEL_PRICES_FILE {path}: {e}")


def _price_model(model: str) -> tuple:
    """(input, output) USD per 1M tokens for a model; (0, 0) if unpriced/free.
    Longest-substring match, mirroring GI snapshot matching."""
    mn = (model or "").lower()
    for key in sorted(MODEL_PRICES, key=len, reverse=True):
        if key in mn:
            return MODEL_PRICES[key]
    return (0.0, 0.0)


def _price_rank(model: str) -> float:
    """Single sortable price estimate. Unknown/free/subscription models are 0."""
    pin, pout = _price_model(model)
    return float(pin or 0.0) + float(pout or 0.0)


def _cost(model: str, prompt_toks, completion_toks) -> float:
    """Estimated USD cost of one response from its token usage. Free/unpriced = 0."""
    pin, pout = _price_model(model)
    if not pin and not pout:
        return 0.0
    return (int(prompt_toks or 0) / 1e6) * pin + (int(completion_toks or 0) / 1e6) * pout


def _cost_obj(usd: float) -> dict:
    """Serialize a USD amount for JSON output."""
    return {"usd": round(float(usd or 0), 6)}


_apply_price_overrides()


def _model_env_suffix(model: str) -> str:
    """Sanitize a model id into an env-var fragment: upper-case, non-alnum → '_'.
    e.g. 'gemini-2.5-pro' → 'GEMINI_2_5_PRO' (used for <PROVIDER>_<MODEL>_* overrides)."""
    return re.sub(r"[^A-Za-z0-9]+", "_", model).strip("_").upper()


def _model_caps(name: str, model: str) -> dict:
    """Per-(provider, model) GI + feature probes. GI from gi_ranking; tools default
    capable when unknown; reasoning defaults to off.

    GI is always resolved live (snapshot/overrides) — never from router_state.
    """
    st = _model_state.get((name, model))
    if st:
        out = {k: v for k, v in st.items() if k not in ("gi", "gi_source", "rating")}
        gi, src = gi_ranking.resolve_gi(name, model)
        out["gi"] = gi
        out["gi_source"] = src
        return out
    gi, src = gi_ranking.resolve_gi(name, model)
    return {"gi": gi, "gi_source": src, "supports_tools": True, "reasoning": False}


def _feature_caps_only(entry: dict | None) -> dict:
    """Probe/feature fields for router_state — never persist snapshot GI scores."""
    if not isinstance(entry, dict):
        return {"supports_tools": True, "reasoning": False}
    out = {k: v for k, v in entry.items() if k not in ("gi", "gi_source", "rating")}
    out.setdefault("supports_tools", True)
    out.setdefault("reasoning", False)
    return out


def _model_supports_tools(name: str, model: str) -> bool:
    """Whether this specific (provider, model) handles function calling."""
    return bool(_model_caps(name, model).get("supports_tools", True))


def _promote_tools_support(name: str, model: str) -> None:
    """Mark a model as tool-capable after a live response emitted tool_calls.

    Startup probes can false-negative (small max_tokens / truncated replies);
    correcting the cache here prevents the next tools request from skipping a
    model that just proved it works.
    """
    st = _model_state.get((name, model))
    if st is not None and st.get("supports_tools"):
        return
    if st is None:
        _model_state[(name, model)] = {
            "supports_tools": True, "reasoning": False,
            "supports_tools_source": "promote"}
    else:
        st = _feature_caps_only(st)
        st["supports_tools"] = True
        st["supports_tools_source"] = "promote"
        _model_state[(name, model)] = st
    ps = _provider_state.get(name)
    if isinstance(ps, dict) and ps.get("model") == model:
        ps["supports_tools"] = True
    try:
        if not STATE_FILE.exists():
            return
        doc = json.loads(STATE_FILE.read_text())
        key = f"{name}::{model}"
        entry = _feature_caps_only(
            (doc.get("model_state") or {}).get(key) or _model_state[(name, model)]
        )
        entry["supports_tools"] = True
        entry["supports_tools_source"] = "promote"
        doc.setdefault("model_state", {})[key] = entry
        _model_state[(name, model)] = _feature_caps_only(entry)
        if name in (doc.get("providers") or {}) and doc["providers"][name].get("model") == model:
            doc["providers"][name]["supports_tools"] = True
        doc.setdefault("scale_version", CAPABILITY_SCALE_VERSION)
        STATE_FILE.write_text(json.dumps(doc, indent=2))
    except Exception as e:
        log.debug(f"[ratings] could not persist tools promotion for {name}/{model}: {e}")


def _response_has_tool_calls(data) -> bool:
    """True when an OpenAI-format completion message includes tool_calls."""
    if not isinstance(data, dict):
        return False
    for ch in data.get("choices") or []:
        if not isinstance(ch, dict):
            continue
        msg = ch.get("message") or {}
        if isinstance(msg, dict) and msg.get("tool_calls"):
            return True
    return False


# Known vision-capable model families, matched by substring (mirrors _rate_model's
# approach). Unlike tool support — which most modern chat models handle, so
# _model_supports_tools defaults to True — vision support is the exception rather
# than the rule among free-tier/small text models. A real cascade test showed 5 of
# 6 non-vision candidates (mistral, cerebras, groq, huggingface) fail cleanly on an
# image request before reaching a model that works, wasting real latency. So this
# defaults to False, but — exactly like enforce_tool — the caller only enforces the
# filter when at least one matching candidate exists, so an incomplete pattern list
# can never make routing worse than it is today, only skip predictable failures.
_VISION_MODEL_PATTERNS = (
    "gemini", "gpt-4o", "gpt-4.1", "gpt-4-turbo", "gpt-5", "o1", "o3",
    "claude-3", "claude-opus", "claude-sonnet", "claude-haiku",
    "pixtral", "llava", "-vl", "vl-", "llama-4", "grok", "vision",
)


def _model_supports_vision(provider: dict, model: str) -> bool:
    """Whether this specific (provider, model) can accept image input.
    Anthropic and Codex (GPT-4o/5-family via ChatGPT) are natively multimodal;
    everything else is matched by known vision-capable family name patterns."""
    if provider.get("protocol") in ("anthropic", "codex"):
        return True
    mn = model.lower()
    if "embed" in mn:   # e.g. gemini-embedding-001 — matches "gemini" but isn't a chat model
        return False
    return any(p in mn for p in _VISION_MODEL_PATTERNS)


def _payload_has_image(payload: dict) -> bool:
    """Whether any message in this OpenAI-format payload carries image content."""
    for m in payload.get("messages", []):
        content = m.get("content")
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "image_url":
                    return True
    return False


def _discover_best_model(base_url: str, key: str, extra_headers: dict = None,
                         free_only: bool = False) -> str | None:
    try:
        hdrs = {"Authorization": f"Bearer {key}", **(extra_headers or {})}
        r = _HTTP.get(f"{base_url.rstrip('/')}/models", headers=hdrs, timeout=10)
        if r.status_code != 200:
            return None
        models = []
        for item in r.json().get("data", []):
            if not isinstance(item, dict):
                continue
            mid = item.get("id")
            if not isinstance(mid, str) or not mid.strip():
                continue
            normalized = mid.strip()
            if FILTER_SPECIALIZED_MODELS and _is_specialized_model(normalized, item):
                continue
            models.append(normalized)
        if free_only:
            models = [m for m in models if _is_free_model_id(m)]
        return max(models, key=_rate_model) if models else None
    except Exception:
        return None


def _discover_models(provider: dict, key: str, free_only: bool = False) -> list[str]:
    """Fetch provider models from an OpenAI-compatible /models endpoint.

    Returns a quality-sorted full catalog (caller applies AUTO_DISCOVER_MODEL_LIMIT
    when appending extras). Fail-soft: any provider quirk simply disables discovery
    for that provider on this start.
    """
    filtered, _catalog = _discover_models_with_catalog(provider, key, free_only=free_only)
    return filtered


def _discover_models_with_catalog(provider: dict, key: str, free_only: bool = False) -> tuple[list[str], list[str]]:
    """Fetch models from /models; return (filtered, catalog).

    ``catalog`` includes every normalized ID (membership checks for configured
    models). ``filtered`` excludes specialized models when FILTER_SPECIALIZED_MODELS.
    """
    try:
        hdrs = {"Authorization": f"Bearer {key}", **provider.get("headers", {})}
        r = _HTTP.get(f"{provider['base_url'].rstrip('/')}/models", headers=hdrs, timeout=10)
        if r.status_code != 200:
            return [], []
        catalog = []
        filtered = []
        dropped = []
        for item in r.json().get("data", []):
            if not isinstance(item, dict):
                continue
            mid = item.get("id")
            if not isinstance(mid, str) or not mid.strip():
                continue
            normalized = mid.strip()
            if provider["name"] == "gemini" and normalized.startswith("models/"):
                normalized = normalized[len("models/"):]
            if TOKEN_CAPS_ENABLED:
                max_in, max_out = extract_caps_from_model_item(item)
                if max_in or max_out:
                    token_caps.seed_from_metadata(
                        provider["name"], normalized, max_in, max_out
                    )
            catalog.append(normalized)
            if FILTER_SPECIALIZED_MODELS and _is_specialized_model(normalized, item):
                dropped.append(normalized)
                continue
            filtered.append(normalized)
        if dropped:
            sample = ", ".join(dropped[:5])
            more = f" (+{len(dropped) - 5} more)" if len(dropped) > 5 else ""
            log.debug(f"[ratings]   {provider['name']}: dropped {len(dropped)} specialized "
                      f"model(s): {sample}{more}")
        if free_only:
            catalog = [m for m in catalog if _is_free_model_id(m)]
            filtered = [m for m in filtered if _is_free_model_id(m)]
        catalog = list(dict.fromkeys(catalog))
        filtered = list(dict.fromkeys(filtered))
        sort_key = lambda m: (_price_rank(m), -_rate_model(m), m.lower())
        catalog.sort(key=sort_key)
        filtered.sort(key=sort_key)
        return filtered, catalog
    except Exception as e:
        log.debug(f"[ratings]   {provider['name']}: model discovery skipped: {e}")
        return [], []


def _provider_model_discovery_enabled(provider: dict) -> bool:
    name = provider["name"].upper()
    val = os.environ.get(f"{name}_AUTO_DISCOVER_MODELS")
    if val is None:
        return AUTO_DISCOVER_MODELS
    return val.strip().lower() not in ("0", "", "false", "no", "off")


def _refresh_discovered_models(provider: dict, key: str, pool_ref) -> None:
    """Opt-in model refresh: prune configured models not reported by /models and
    append the best discovered models up to AUTO_DISCOVER_MODEL_LIMIT.

    Configured models that still exist in the API catalog are always kept;
    AUTO_DISCOVER_MODEL_LIMIT only bounds how many extras are appended.

    This is deliberately conservative: unsupported protocols and huge/mixed
    catalogs are skipped unless a per-provider env flag explicitly enables them.
    """
    if not _provider_model_discovery_enabled(provider):
        return
    name = provider["name"]
    if name in _MODEL_DISCOVERY_SKIP and os.environ.get(f"{name.upper()}_AUTO_DISCOVER_MODELS") is None:
        log.info(f"[ratings]   {name}: model discovery skipped by default")
        return
    free_only = name in _FREE_ONLY_DISCOVERY
    discovery = _discover_models_with_catalog(provider, key, free_only=free_only)
    discovered, catalog = discovery
    discovered = _filter_excluded(name, discovered)
    if not discovered:
        return

    configured = list(provider.get("models") or [provider["model"]])
    # Configured models are kept when still in the API catalog, even if the
    # specialized filter removed them from the discovery append list.
    catalog_set = set(_filter_excluded(name, catalog))
    # Prune only when doing so still leaves a configured model; otherwise the
    # existing invalid-model repair path can try to recover a primary model.
    kept = _filter_excluded(name, [m for m in configured if m in catalog_set])
    if not kept:
        kept = discovered[:1]
    # Never drop valid configured models; only bound appended discoveries.
    refreshed = _filter_excluded(name, list(dict.fromkeys(kept + discovered)))
    if len(kept) < AUTO_DISCOVER_MODEL_LIMIT:
        refreshed = refreshed[:AUTO_DISCOVER_MODEL_LIMIT]
    if not refreshed or refreshed == configured:
        return

    provider["models"] = refreshed
    old_primary = provider["model"]
    provider["model"] = refreshed[0]
    for m in refreshed:
        try:
            pool_ref.ensure_model(name, m, provider["keys"])
        except AttributeError:
            pass
    if old_primary != provider["model"]:
        pool_ref.rename_model(name, old_primary, provider["model"])
    log.info(f"[ratings]   {name}: discovered models → {', '.join(refreshed)}")


def _fetch_models_catalog_map(provider: dict, key: str) -> dict[str, dict]:
    """GET /models once; return normalized id → raw catalog item. {} on failure.

    For Gemini, also merges native ``thinking`` flags (OpenAI-compat /models omits them).
    """
    try:
        hdrs = {"Authorization": f"Bearer {key}", **provider.get("headers", {})}
        r = _HTTP.get(f"{provider['base_url'].rstrip('/')}/models", headers=hdrs, timeout=10)
        if r.status_code != 200:
            out: dict[str, dict] = {}
        else:
            out = {}
            for item in r.json().get("data", []):
                if not isinstance(item, dict):
                    continue
                mid = item.get("id")
                if not isinstance(mid, str) or not mid.strip():
                    continue
                normalized = mid.strip()
                if provider["name"] == "gemini" and normalized.startswith("models/"):
                    normalized = normalized[len("models/"):]
                out[normalized] = item
        if provider["name"] == "gemini":
            _enrich_gemini_catalog_thinking(out, key)
        return out
    except Exception as e:
        log.debug(f"[ratings]   {provider['name']}: capability catalog fetch skipped: {e}")
        return {}


def _enrich_gemini_catalog_thinking(out: dict[str, dict], key: str) -> None:
    """Attach native Gemini ``thinking`` onto catalog items (query-key auth only)."""
    try:
        url = "https://generativelanguage.googleapis.com/v1beta/models"
        r = _HTTP.get(url, params={"key": key, "pageSize": 200}, timeout=15)
        if r.status_code != 200:
            return
        for item in r.json().get("models") or []:
            if not isinstance(item, dict) or "thinking" not in item:
                continue
            name = item.get("name") or ""
            if not isinstance(name, str):
                continue
            mid = name[len("models/"):] if name.startswith("models/") else name
            if not mid:
                continue
            base = out.get(mid) or {"id": mid}
            merged = dict(base)
            think = item.get("thinking")
            if think is True or think is False:
                merged["thinking"] = think
                out[mid] = merged
            elif mid not in out:
                out[mid] = merged
    except Exception as e:
        log.debug(f"[ratings]   gemini: native thinking enrich skipped: {e}")


_STICKY_CAP_SOURCES = frozenset({"catalog", "promote"})

# Normalized model id → True when OpenRouter advertises reasoning. Filled once per
# ratings pass so thin-catalog providers (SambaNova/Cerebras/…) can inherit.
_openrouter_reasoning_index: dict[str, bool] | None = None

_REASONING_INDEX_STRIP_SUFFIXES = (
    "-it", "-terminus", "-exp", "-instruct", "-chat",
)


def _openrouter_item_has_reasoning(item: dict) -> bool:
    params = item.get("supported_parameters")
    params_set = {p for p in params if isinstance(p, str)} if isinstance(params, list) else set()
    reasoning_obj = item.get("reasoning")
    return (
        isinstance(reasoning_obj, dict)
        or "reasoning" in params_set
        or "include_reasoning" in params_set
        or "reasoning_effort" in params_set
    )


def _reasoning_index_keys(model_id: str) -> list[str]:
    """Normalized id plus shortened forms (strip -it/-terminus/…)."""
    nid = gi_ranking.normalize_model_id(model_id)
    if not nid:
        return []
    keys = [nid]
    for suf in _REASONING_INDEX_STRIP_SUFFIXES:
        if nid.endswith(suf) and len(nid) > len(suf) + 2:
            keys.append(nid[: -len(suf)])
    return keys


def _load_openrouter_reasoning_index() -> dict[str, bool]:
    """Public OpenRouter /models → {normalized_id: True} for reasoning models."""
    global _openrouter_reasoning_index
    if _openrouter_reasoning_index is not None:
        return _openrouter_reasoning_index
    out: dict[str, bool] = {}
    try:
        r = _HTTP.get("https://openrouter.ai/api/v1/models", timeout=15)
        if r.status_code == 200:
            for item in r.json().get("data") or []:
                if not isinstance(item, dict) or not _openrouter_item_has_reasoning(item):
                    continue
                mid = item.get("id")
                if not isinstance(mid, str):
                    continue
                for k in _reasoning_index_keys(mid):
                    out[k] = True
    except Exception as e:
        log.debug(f"[ratings] openrouter reasoning index skipped: {e}")
    _openrouter_reasoning_index = out
    log.info(f"[ratings] OpenRouter reasoning index: {len(out)} id(s)")
    return out


def _shared_reasoning_hint(model: str) -> bool | None:
    """True when a shared OpenRouter catalog marks this model family as reasoning."""
    idx = _load_openrouter_reasoning_index()
    if not idx:
        return None
    for k in _reasoning_index_keys(model):
        if idx.get(k):
            return True
    # Cerebras-style truncations: gemma-4-31b ↔ gemma-4-31b-it already covered by
    # strip suffixes on the index side; also try query + common suffixes.
    nid = gi_ranking.normalize_model_id(model)
    if not nid:
        return None
    for suf in ("-it", "-terminus", "-instruct"):
        if idx.get(nid + suf):
            return True
    return None


def _merge_capability(prior: dict | None, cap: str, value: bool, source: str) -> tuple[bool, str]:
    """Apply sticky-positive rules for one capability field.

    Sticky sources (catalog/promote) with prior True are never demoted.
    """
    src_key = f"{cap}_source"
    if prior and prior.get(cap) is True and prior.get(src_key) in _STICKY_CAP_SOURCES:
        if value is False:
            return True, prior.get(src_key) or "catalog"
        if value is True:
            return True, source if source in _STICKY_CAP_SOURCES else (prior.get(src_key) or source)
    return value, source


def _catalog_caps_from_item(provider_name: str, item: dict | None) -> dict:
    """Map a /models catalog item to optional capability bools.

    Returns {"supports_tools": True|False|None, "reasoning": True|False|None}.
    None means silent — caller should fall back to behavioral probe.
    """
    if not isinstance(item, dict):
        return {"supports_tools": None, "reasoning": None}

    params = item.get("supported_parameters")
    has_params = isinstance(params, list)
    params_set = {p for p in params if isinstance(p, str)} if has_params else set()
    rich = has_params and len(params_set) > 0

    tools_true = "tools" in params_set or "tool_choice" in params_set
    reasoning_obj = item.get("reasoning")
    reasoning_true = (
        isinstance(reasoning_obj, dict)
        or "reasoning" in params_set
        or "include_reasoning" in params_set
        or "reasoning_effort" in params_set
    )

    if provider_name == "openrouter":
        if not rich and not isinstance(reasoning_obj, dict):
            return {"supports_tools": None, "reasoning": None}
        supports_tools = True if tools_true else (False if rich else None)
        if isinstance(reasoning_obj, dict) or reasoning_true:
            reasoning = True
        elif rich:
            reasoning = False
        else:
            reasoning = None
        return {"supports_tools": supports_tools, "reasoning": reasoning}

    if provider_name == "gemini":
        # Native model metadata exposes thinking; OpenAI-compat /models does not.
        # Only assert when the API sends a real bool — null/missing stays silent.
        if item.get("thinking") is True:
            return {
                "supports_tools": True if tools_true else None,
                "reasoning": True,
            }
        if item.get("thinking") is False:
            return {
                "supports_tools": True if tools_true else None,
                "reasoning": False,
            }
        return {
            "supports_tools": True if tools_true else None,
            "reasoning": None,
        }

    # Default adapter: may assert true on obvious fields; never assert false.
    return {
        "supports_tools": True if tools_true else None,
        "reasoning": True if reasoning_true else None,
    }


def _probe_anthropic(provider: dict, key: str) -> tuple:
    """Probe Anthropic using the Messages API (not OpenAI-format /chat/completions)."""
    url  = "https://api.anthropic.com/v1/messages"
    hdrs = {"x-api-key": key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}
    body = {"model": provider["model"], "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1}
    t0 = time.time()
    try:
        r = _HTTP.post(url, headers=hdrs, json=body, timeout=12)
        latency = (time.time() - t0) * 1000
        if r.status_code == 200:
            return True, latency, provider["model"], "ok"
        return False, latency, provider["model"], ("auth" if r.status_code in (401, 403) else "http")
    except requests.exceptions.ReadTimeout:
        return True, (time.time() - t0) * 1000, provider["model"], "timeout"
    except Exception:
        return False, (time.time() - t0) * 1000, provider["model"], "network"


def _probe_provider(provider: dict, key: str) -> tuple:
    """Returns (success, latency_ms, model_used, status). Auto-discovers alt model on 400/404.

    A read-timeout means the provider accepted the request and is still
    generating — alive but slow. Large MoE models can cold-start for 30–60s,
    past the probe window, so a read-timeout counts as available rather than
    wrongly dropping a working provider to the back of its rating tier. Only a
    connection failure (host unreachable) counts as down."""
    if provider.get("protocol") == "anthropic":
        return _probe_anthropic(provider, key)
    if provider.get("protocol") == "codex":
        # Don't spend ChatGPT quota (or risk ToS) on a startup completion —
        # "available" means we can mint a valid access token for the account.
        t0 = time.time()
        ok = bool(codex_creds.get_access_token(key))
        return ok, (time.time() - t0) * 1000, provider["model"], ("ok" if ok else "auth")

    url  = provider["base_url"].rstrip("/") + "/chat/completions"
    hdrs = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
            **provider.get("headers", {})}
    body = {"model": provider["model"],
            "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1}
    t0 = time.time()
    try:
        r = _HTTP.post(url, headers=hdrs, json=body, timeout=12)
        latency = (time.time() - t0) * 1000
        if r.status_code == 200:
            return True, latency, provider["model"], "ok"
        if r.status_code == 429:
            return False, latency, provider["model"], "rate_limited"
        if r.status_code in (401, 403):
            return False, latency, provider["model"], "auth"
        if r.status_code in (400, 404):
            # Providers that list paid models alongside free ones — never let
            # auto-discovery silently pick something that costs credits.
            alt = _discover_best_model(provider["base_url"], key, provider.get("headers", {}),
                                       free_only=provider["name"] in _FREE_ONLY_DISCOVERY)
            if alt:
                body["model"] = alt
                t0 = time.time()
                r2 = _HTTP.post(url, headers=hdrs, json=body, timeout=12)
                if r2.status_code == 200:
                    return True, (time.time() - t0) * 1000, alt, "ok"
        return False, (time.time() - t0) * 1000, provider["model"], "http"
    except requests.exceptions.ReadTimeout:
        # Connected, still generating — alive, just slow (cold MoE start).
        return True, (time.time() - t0) * 1000, provider["model"], "timeout"
    except Exception:
        return False, (time.time() - t0) * 1000, provider["model"], "network"


_TOOL_PROBE = [{"type": "function", "function": {
    "name": "get_weather", "description": "Get the current weather for a city",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                   "required": ["city"]}}}]


def _probe_tools(provider: dict, key: str, model: str) -> bool | None:
    """Detect whether a provider's model supports function calling. Sends a tiny
    request that forces a tool call (tool_choice=required, falling back to auto
    for providers that reject 'required') and checks whether the model actually
    emits one. Anthropic providers always support tools.

    Returns True/False on a conclusive (HTTP 200) response, or None when neither
    attempt got one — network error, timeout, or a non-200 on both (e.g. the
    provider's free-tier RPM was already spent by earlier probes in this same
    startup pass). None means "couldn't determine", NOT "doesn't support tools":
    caching a transient probe failure as a confident False would silently and
    persistently (for STATE_TTL_HOURS) exclude a capable model from tool-aware
    routing. Callers should treat None as unknown and keep the optimistic default.

    A 200 with no tool_calls is only a confident False when the model returned a
    normal text answer (non-empty content, finish_reason stop/end). Truncated
    (finish_reason=length) or empty replies are treated as inconclusive (None) —
    small max_tokens probes often starve capable models into a false negative.
    """
    if provider.get("protocol") in ("anthropic", "codex"):
        return True   # both support function calling
    url  = provider["base_url"].rstrip("/") + "/chat/completions"
    hdrs = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
            **provider.get("headers", {})}
    base = {"model": model, "max_tokens": 64, "tools": _TOOL_PROBE,
            "messages": [{"role": "user", "content": "What is the weather in Paris? Use the get_weather tool."}]}
    saw_conclusive_no = False
    for choice in ("required", "auto"):
        try:
            r = _HTTP.post(url, headers=hdrs, json={**base, "tool_choice": choice}, timeout=12)
        except Exception:
            continue   # network hiccup on this attempt — still try the other tool_choice
        if r.status_code != 200:
            continue   # provider may reject tool_choice=required → try auto
        try:
            choice_obj = (r.json().get("choices") or [{}])[0] or {}
            msg = choice_obj.get("message") or {}
            if msg.get("tool_calls"):
                return True
            content = (msg.get("content") or "").strip()
            finish = (choice_obj.get("finish_reason") or "").lower()
            # Weak signal → inconclusive (do not cache as tools=no)
            if finish == "length" or not content:
                continue
            if finish in ("", "stop", "end_turn", "completed"):
                saw_conclusive_no = True
        except Exception:
            continue
    if saw_conclusive_no:
        return False
    return None  # no 200, or only weak/inconclusive 200s


def _probe_reasoning(provider: dict, key: str, model: str) -> bool:
    """Detect whether a provider's model is a 'reasoning' model — one that spends
    output tokens on hidden chain-of-thought before answering. These return empty
    content if max_tokens is too small to cover the thinking. We probe with a
    small budget and a trivial prompt: a reasoning model exposes a reasoning field
    or burns the whole budget thinking (empty content, truncated), while a normal
    model just answers. Anthropic's thinking is opt-in, so it's treated as normal."""
    if provider.get("protocol") == "anthropic":
        return False
    if provider.get("protocol") == "codex":
        return True   # Codex (GPT-5) is a reasoning model — reserve output headroom
    url  = provider["base_url"].rstrip("/") + "/chat/completions"
    hdrs = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
            **provider.get("headers", {})}
    body = {"model": model, "max_tokens": 24,
            "messages": [{"role": "user", "content": "Reply with just the word: ready"}]}
    try:
        r = _HTTP.post(url, headers=hdrs, json=body, timeout=12)
        if r.status_code != 200:
            return False
        choice = (r.json().get("choices") or [{}])[0]
        msg     = choice.get("message") or {}
        content = (msg.get("content") or "").strip()
        if msg.get("reasoning_content") or msg.get("reasoning"):
            return True
        return not content and choice.get("finish_reason") == "length"
    except Exception:
        return False


def classify_complexity(messages: list) -> int:
    """Heuristic: 1 (easiest/trivial) → 5 (hardest/critical). No LLM call."""
    content = " ".join(
        m["content"] if isinstance(m.get("content"), str)
        else " ".join(p.get("text", "") for p in m["content"] if isinstance(p, dict))
        for m in messages if m.get("content")
    )
    tokens = len(content) // 4
    cl = content.lower()
    has_code    = "```" in content or any(k in cl for k in ["def ", "function ", "class ", "import "])
    has_complex = any(k in cl for k in ["implement", "design", "architect", "debug", "refactor",
                                         "algorithm", "optimize", "analyze", "build", "develop",
                                         "summarize", "explain how", "compare", "research", "create a plan",
                                         "generate", "convert", "migrate", "write tests", "test cases",
                                         "step by step", "walk me through", "help me understand"])
    has_simple  = any(k in cl for k in ["what is", "who is", "define", "translate", "yes or no",
                                         "how many", "give me a number", "true or false", "in one word",
                                         "spell", "what does", "one sentence", "yes or no answer",
                                         "what year", "what time", "how old"])
    if tokens > 2000 or (has_code and has_complex): return 5
    if tokens > 800  or has_complex:                return 4
    if tokens > 300  or has_code:                   return 3
    if tokens > 100  or (not has_simple):           return 2
    return 1


def _get_smart_ordered(providers: list, complexity: int, est_tokens: int = 0,
                       prefer_local: bool = False, sticky: dict | None = None) -> list:
    """
    Rank every configured (provider, model) for this complexity: cheapest model
    that clears the min GI threshold first, then too-weak as last resort. Never
    blocks. Returns a flat list of candidate dicts
    {"provider": <provider>, "model": <model str>}.

    Each model in a provider's comma-separated list is its own candidate, scored
    on its OWN GI — so e.g. gemini-2.5-pro can be picked for a hard request while
    gemini-2.5-flash-lite handles easy ones. Within equal price/GI overshoot, a
    provider's models keep their listed order (list_index tie-break).

    When FAST_ROUTE_THRESHOLD is set and the request is shorter than it, low-latency
    providers win ties. With prefer_local (the `:fast` preference), a configured local
    model leads on easy turns (complexity ≤ 3), with cloud as fallback.

    When sticky (session affinity) is provided and its (provider, model) is still in the
    catalog, that candidate is moved to the front after scoring.
    """
    fast_first = FAST_ROUTE_TOKENS > 0 and 0 < est_tokens < FAST_ROUTE_TOKENS
    min_gi = gi_ranking.min_gi_for_complexity(complexity)

    def _key(cand):
        p      = cand["provider"]
        model  = cand["model"]
        name   = p["name"]
        gi     = _model_caps(name, model)["gi"]
        avail  = _provider_state.get(name, {}).get("available", True)
        fast   = 0 if (fast_first and name in _FAST_PROVIDERS) else 1
        # `:fast` preference: a short/casual turn prefers the local model first.
        local_first = 0 if (prefer_local and name == "local" and complexity <= 3) else 1
        # Health-aware terms — tier/sort_within stay FIRST so GI matching
        # is never overridden by health (a healthy weak model must not outrank the
        # correct-GI one). When every candidate is healthy these two terms
        # are constant (0), leaving the existing tie order untouched.
        breaker_open = 1 if stats.breaker_open(name) else 0  # open breakers sink within tier
        health       = stats.health_bucket(name)             # 0 healthy / 1 degraded / 2 bad
        # Rate headroom: 0.0 = full headroom (best sort position), 1.0 = empty (worst)
        _peek_key = pool.peek_key(name, model)
        _rate_score = 0.0
        if _peek_key:
            _rate_score = 1.0 - rate_limiter.headroom(name, _peek_key, model)
        price   = _price_rank(model)
        if gi >= min_gi:
            tier        = 0
            sort_within = gi - min_gi   # 0 = on the bar, larger = overkill
        else:
            tier        = 1
            sort_within = min_gi - gi   # too weak — closest first
        # local_first leads the key so a preferred local model sorts ahead of all
        # others on easy turns; it's a constant 1 otherwise, leaving order unchanged.
        # list_index trails so a provider's listed model order breaks GI ties.
        return (local_first, tier, price, sort_within, breaker_open, health,
                _rate_score, 0 if avail else 1, fast, cand["list_index"])

    candidates = [{"provider": p, "model": m, "list_index": i}
                  for p in providers
                  for i, m in enumerate(p.get("models") or [p["model"]])]
    ordered = sorted(candidates, key=_key)
    if sticky:
        sp, sm = sticky.get("provider"), sticky.get("model")
        if sp and sm:
            for i, c in enumerate(ordered):
                if c["provider"]["name"] == sp and c["model"] == sm:
                    if i > 0:
                        ordered = [ordered[i]] + ordered[:i] + ordered[i + 1:]
                    break
    return ordered


def _env_flag(name: str, suffix: str, model: str):
    """Read a feature-probe override env var, preferring the per-model form
    <PROVIDER>_<MODEL>_<SUFFIX> over the provider-wide <PROVIDER>_<SUFFIX>.
    Returns True/False if set, else None (= not overridden → probe)."""
    val = os.environ.get(f"{name.upper()}_{_model_env_suffix(model)}_{suffix}")
    if val is None:
        val = os.environ.get(f"{name.upper()}_{suffix}")
    if val is None:
        return None
    return val.strip().lower() not in ("0", "false", "no", "")


def _resolve_caps(p: dict, key: str, model: str, ok: bool,
                  catalog_item=None, prior=None) -> dict:
    """Feature probes for one (provider, model). GI is resolved separately via
    gi_ranking.

    Resolve order per capability: env override → catalog → behavioral probe.
    Catalog/promote positives in ``prior`` are never demoted by a later probe
    false. _probe_tools None stays optimistic True.

    When catalog is silent on reasoning and prior was a probe-false, re-probe
    so sticky false-negatives (e.g. OpenCode) can recover.
    """
    name = p["name"]
    cat = _catalog_caps_from_item(name, catalog_item)

    et = _env_flag(name, "SUPPORTS_TOOLS", model)
    if et is not None:
        supports_tools, tools_source = et, "env"
    elif cat["supports_tools"] is not None:
        supports_tools, tools_source = _merge_capability(
            prior, "supports_tools", cat["supports_tools"], "catalog")
    elif not ok:
        supports_tools, tools_source = _merge_capability(
            prior, "supports_tools", False, "probe")
    elif prior is not None and "supports_tools" in prior:
        # Keep cached tools when catalog is silent — avoid mass re-probes.
        supports_tools = bool(prior.get("supports_tools"))
        tools_source = prior.get("supports_tools_source") or "probe"
        if prior.get("supports_tools") is True and prior.get("supports_tools_source") in _STICKY_CAP_SOURCES:
            tools_source = prior.get("supports_tools_source") or "catalog"
    else:
        probed = _probe_tools(p, key, model)
        raw = True if probed is None else probed
        supports_tools, tools_source = _merge_capability(
            prior, "supports_tools", raw, "probe")

    er = _env_flag(name, "REASONING", model)
    if er is not None:
        reasoning, reasoning_source = er, "env"
    elif cat["reasoning"] is not None:
        reasoning, reasoning_source = _merge_capability(
            prior, "reasoning", cat["reasoning"], "catalog")
    else:
        shared = _shared_reasoning_hint(model)
        if shared is True:
            reasoning, reasoning_source = _merge_capability(
                prior, "reasoning", True, "catalog")
        elif not ok:
            reasoning, reasoning_source = _merge_capability(
                prior, "reasoning", False, "probe")
        elif prior is not None and prior.get("reasoning") is True:
            # Keep prior true when catalog silent (skip redundant probe).
            reasoning, reasoning_source = True, prior.get("reasoning_source") or "probe"
        else:
            # No prior, or prior false/unknown — probe (recovers sticky false-negatives).
            raw = _probe_reasoning(p, key, model) if ok else False
            reasoning, reasoning_source = _merge_capability(
                prior, "reasoning", raw, "probe")

    return {
        "supports_tools": supports_tools,
        "reasoning": reasoning,
        "supports_tools_source": tools_source,
        "reasoning_source": reasoning_source,
    }


def _migrate_capability_scale(doc: dict) -> dict:
    """Invert persisted capability scores from scale v1 (1=strongest) to v2 (1=weakest)."""
    if doc.get("scale_version") == CAPABILITY_SCALE_VERSION:
        return doc

    def _flip(entry: dict) -> dict:
        if not isinstance(entry, dict):
            return entry
        out = dict(entry)
        r = out.get("rating")
        if isinstance(r, (int, float)) and 1 <= int(r) <= 5:
            out["rating"] = 6 - int(r)
        return out

    providers = doc.get("providers") or {}
    doc["providers"] = {k: _flip(v) for k, v in providers.items()}
    model_state = doc.get("model_state") or {}
    doc["model_state"] = {k: _flip(v) for k, v in model_state.items()}
    doc["scale_version"] = CAPABILITY_SCALE_VERSION
    log.info("[ratings] Migrated persisted capability scores to scale_version=%s",
             CAPABILITY_SCALE_VERSION)
    return doc


def _initialize_ratings(providers: list, pool_ref):
    """Background: probe all providers, fix bad models, assign ratings, persist state."""
    global _provider_state, _model_state, _openrouter_reasoning_index
    _openrouter_reasoning_index = None  # refresh shared hints each ratings pass
    if STATE_FILE.exists():
        try:
            cached_doc = _migrate_capability_scale(json.loads(STATE_FILE.read_text()))
            _provider_state = cached_doc.get("providers", {})
            # Per-model caps were persisted as "name::model" keys — restore tuples.
            _model_state = {}
            for k, v in (cached_doc.get("model_state") or {}).items():
                n, _, m = k.partition("::")
                if m:
                    _model_state[(n, m)] = _feature_caps_only(v)
            log.info(f"[ratings] Loaded cached state ({len(_provider_state)} providers, "
                     f"{len(_model_state)} models)")
            # Probes cost a real completion per model, so skip them while the state
            # is fresh AND still covers every configured provider and model.
            age = time.time() - cached_doc.get("last_updated_ts", 0)
            models_covered = all((p["name"], m) in _model_state
                                 for p in providers for m in (p.get("models") or [p["model"]]))
            discovery_requested = any(_provider_model_discovery_enabled(p) for p in providers)
            if (not discovery_requested
                    and STATE_TTL_HOURS > 0 and age < STATE_TTL_HOURS * 3600
                    and all(p["name"] in _provider_state for p in providers)
                    and models_covered):
                for p in providers:
                    cached_model = _provider_state[p["name"]].get("model")
                    if cached_model and cached_model != p["model"]:
                        old = p["model"]
                        p["model"] = cached_model
                        if p.get("models"):
                            p["models"][0] = cached_model
                        pool_ref.rename_model(p["name"], old, cached_model)
                log.info(f"[ratings] State is {age/3600:.1f}h old (< {STATE_TTL_HOURS}h TTL) "
                         "— skipping startup probes")
                try:
                    STATE_FILE.write_text(json.dumps({
                        **cached_doc,
                        "last_updated": cached_doc.get("last_updated")
                            or time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "providers": _provider_state,
                        "model_state": {
                            f"{n}::{m}": _feature_caps_only(v)
                            for (n, m), v in _model_state.items()
                        },
                        "scale_version": CAPABILITY_SCALE_VERSION,
                    }, indent=2))
                except Exception:
                    pass
                return
        except Exception:
            pass

    log.info("[ratings] Background provider validation starting…")
    new_state = {}
    new_model_state = {}
    cached_models = dict(_model_state)   # reuse fresh entries; only probe new/expired ones
    for p in providers:
        name  = p["name"]
        key   = pool_ref.first_key(name)
        if not key:
            new_state[name] = {"model": p["model"],
                                "available": False, "latency_ms": 0, "overridden": False}
            for m in (p.get("models") or [p["model"]]):
                new_model_state[(name, m)] = {
                                              "supports_tools": False, "reasoning": False}
            continue
        _refresh_discovered_models(p, key, pool_ref)
        ok, latency, actual, probe_status = _probe_provider(p, key)
        # A primary model can be rate-limited, missing tools, or otherwise rejected
        # while the provider/key is still usable for other configured models.
        # Only auth/network failures confidently make every model unusable.
        caps_probe_ok = ok or probe_status not in ("auth", "network")
        original   = p["model"]
        overridden = actual != original
        if overridden:
            log.info(f"[ratings]   {name}: model fixed {original} → {actual}")
            p["model"] = actual
            if p.get("models"):
                p["models"][0] = actual
                p["models"] = list(dict.fromkeys(p["models"]))
            pool_ref.rename_model(name, original, actual)
        # Per-model feature probes for the whole list (primary = models[0] = actual).
        # Always re-resolve with catalog + prior sticky state so catalog can upgrade
        # probe false-negatives; sticky positives are protected inside _resolve_caps.
        catalog_map = _fetch_models_catalog_map(p, key)
        for m in (p.get("models") or [actual]):
            prior = cached_models.get((name, m))
            caps = _feature_caps_only(
                _resolve_caps(p, key, m, caps_probe_ok,
                              catalog_item=catalog_map.get(m), prior=prior)
            )
            new_model_state[(name, m)] = caps
            gi, gi_src = gi_ranking.resolve_gi(name, m)
            log.info(f"[ratings]   {name}/{m}: gi={gi:.1f} ({gi_src}) "
                     f"tools={'yes' if caps['supports_tools'] else 'no'} "
                     f"reasoning={'yes' if caps['reasoning'] else 'no'}")
        # Provider-level fields mirror the primary model's caps (back-compat).
        prim = new_model_state[(name, actual)]
        available = ok or probe_status in ("rate_limited", "http", "timeout")
        log.info(f"[ratings]   {name}: {'✓' if available else '✗'} model={actual} {latency:.0f}ms "
                 f"status={probe_status}")
        new_state[name] = {"model": actual, "available": available,
                            "latency_ms": round(latency, 1), "overridden": overridden,
                            "original_model": original, "supports_tools": prim["supports_tools"],
                            "reasoning": prim["reasoning"], "probe_status": probe_status}
    _provider_state = new_state
    _model_state = new_model_state
    try:
        STATE_FILE.write_text(json.dumps({"last_updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                           "last_updated_ts": time.time(),
                                           "scale_version": CAPABILITY_SCALE_VERSION,
                                           "providers": new_state,
                                           "model_state": {
                                               f"{n}::{m}": _feature_caps_only(v)
                                               for (n, m), v in new_model_state.items()
                                           }},
                                          indent=2))
        log.info("[ratings] State persisted to disk")
    except Exception as e:
        log.warning(f"[ratings] Could not persist state: {e}")
    try:
        rate_limiter.flush()
    except Exception as e:
        log.warning(f"[rate_limiter] Could not flush state: {e}")
    try:
        token_caps.flush()
    except Exception as e:
        log.warning(f"[token_caps] Could not flush state: {e}")


class CredentialPool:
    """Thread-safe key pool with per-key health cooldown tracking.

    Keys use key affinity: return the preferred key when ready, otherwise
    the first ready key in stable deque order (no rotation).

    Upstream rate limits are handled by AdaptiveRateLimiter, not this pool.
    cool_until here is only for network/5xx health failures via mark_key_down."""

    def __init__(self, providers: list[dict]):
        self.lock  = threading.Lock()
        # provider -> { model -> deque({key, cool_until}) }. Each model gets its own
        # key deque so health cooldowns (mark_key_down) are tracked per (key, model).
        # Upstream rate limits go through AdaptiveRateLimiter, not cool_until.
        self.pools: dict[str, dict[str, deque]] = {}
        # (provider, key) -> total times this key has been handed out, across all of
        # the provider's models. Lets /v1/status show whether load is actually
        # spreading across configured keys, not just that rotation "should" work.
        self.key_requests: dict = defaultdict(int)
        for p in providers:
            models = list(p.get("models") or [p.get("model", "")])
            if p.get("embed_model"):
                models.append(p["embed_model"])   # embeddings get their own bucket
            self.pools[p["name"]] = {
                m: deque({"key": k, "cool_until": 0.0} for k in p["keys"])
                for m in dict.fromkeys(models)     # de-dupe, preserve order
            }
            log.info(f"  {p['name']}: {len(p['keys'])} key(s) × {len(self.pools[p['name']])} model(s) loaded")

    def peek_key(self, provider_name: str, model: str) -> str | None:
        """Return the first ready key without rotating or bumping key_requests."""
        with self.lock:
            pool = self.pools.get(provider_name, {}).get(model, deque())
            now  = time.time()
            for entry in pool:
                if entry["cool_until"] <= now:
                    return entry["key"]
            return None

    def get_key(self, provider_name: str, model: str, preferred: str | None = None) -> str | None:
        """Return a ready key for (provider, model), or None."""
        with self.lock:
            pool = self.pools.get(provider_name, {}).get(model, deque())
            now  = time.time()
            if preferred:
                for entry in pool:
                    if entry["key"] == preferred and entry["cool_until"] <= now:
                        self.key_requests[(provider_name, entry["key"])] += 1
                        return entry["key"]
            for entry in pool:
                if entry["cool_until"] <= now:
                    self.key_requests[(provider_name, entry["key"])] += 1
                    return entry["key"]
            return None

    def key_requests_for(self, provider_name: str, key: str) -> int:
        """Total times this (provider, key) has been handed out via get_key()."""
        return self.key_requests.get((provider_name, key), 0)

    def key_count(self, provider_name: str, model: str) -> int:
        """How many keys exist for (provider, model) — used to bound retry attempts."""
        return len(self.pools.get(provider_name, {}).get(model, ()))

    def first_key(self, provider_name: str) -> str | None:
        """Any key for a provider (from its primary model's deque) — used for probing."""
        for entries in self.pools.get(provider_name, {}).values():
            if entries:
                return entries[0]["key"]
        return None

    def mark_key_down(self, provider_name: str, key: str, retry_after: int = 30):
        """Cool a key across ALL of the provider's models — for network/5xx (key/
        provider-health) failures, which aren't specific to one model."""
        with self.lock:
            now = time.time()
            for entries in self.pools.get(provider_name, {}).values():
                for entry in entries:
                    if entry["key"] == key:
                        entry["cool_until"] = now + retry_after
            log.warning(f"  {provider_name} key ...{key[-6:]} cooling {retry_after}s (all models)")

    def rename_model(self, provider_name: str, old: str, new: str):
        """Re-key a model's deque — used when the startup probe auto-discovers a
        replacement for a deprecated/invalid primary model name, so the pool's
        per-model bucket keeps matching the provider's model list."""
        with self.lock:
            prov = self.pools.get(provider_name)
            if prov and old in prov and old != new:
                prov[new] = prov.pop(old)

    def ensure_model(self, provider_name: str, model: str, keys: list[str]):
        """Ensure the pool has a bucket for a newly discovered model."""
        with self.lock:
            prov = self.pools.setdefault(provider_name, {})
            if model not in prov:
                prov[model] = deque({"key": k, "cool_until": 0.0} for k in keys)


pool = CredentialPool(PROVIDERS)
sticky_store = SessionStickyStore()


def _sticky_for_request(headers, body) -> tuple[str | None, dict | None]:
    sid = resolve_session_id(headers, body)
    if not sid:
        return None, None
    return sid, sticky_store.get(sid)


def _remember_sticky(session_id: str | None, provider: str, model: str, key: str) -> None:
    if session_id:
        sticky_store.set(session_id, provider=provider, model=model, key=key)


def _clear_sticky(session_id: str | None) -> None:
    if session_id:
        sticky_store.clear(session_id)

rate_limiter = AdaptiveRateLimiter(state_file=RATE_STATE_FILE, auth_file=AUTH_FILE)
rate_limiter.load()
rate_limiter.start_flush_thread()

token_caps = TokenCapTracker(
    state_file=TOKEN_CAPS_STATE_FILE, enabled=TOKEN_CAPS_ENABLED
)
token_caps.load()


def _effective_input_cap_for(provider: dict, model: str) -> int | None:
    return token_caps.effective_input_cap(
        provider["name"], model, int(provider.get("skip_if_tokens_over") or 0)
    )


def _hard_input_cap_for(provider: dict, model: str) -> int | None:
    """Env fence and/or high-confidence learned cap — safe to hard-skip on."""
    return token_caps.hard_input_cap(
        provider["name"], model, int(provider.get("skip_if_tokens_over") or 0)
    )


def _effective_output_cap_for(provider: dict, model: str) -> int | None:
    return token_caps.effective_output_cap(
        provider["name"], model, int(provider.get("max_output_tokens") or 0)
    )


def _hard_output_cap_for(provider: dict, model: str) -> int | None:
    return token_caps.hard_output_cap(
        provider["name"], model, int(provider.get("max_output_tokens") or 0)
    )


def _effective_requested_output_for_learning(
    provider: dict, model: str, payload: dict
) -> int:
    req_max = 0
    for field in ("max_tokens", "max_completion_tokens"):
        if isinstance(payload.get(field), int):
            req_max = max(req_max, payload[field])
    out_cap = _effective_output_cap_for(provider, model)
    if out_cap and req_max:
        req_max = min(req_max, out_cap)
    elif out_cap and not req_max:
        req_max = out_cap
    return req_max


def _apply_output_token_cap(body: dict, provider: dict, model: str) -> None:
    # Env fence and high-confidence learned caps only — low-confidence guesses
    # must not clamp (explore-into-limit).
    out_cap = _hard_output_cap_for(provider, model)
    if not out_cap:
        return
    for field in ("max_tokens", "max_completion_tokens"):
        if isinstance(body.get(field), int) and body[field] > out_cap:
            log.info(
                f"  clamping {field} {body[field]}→{out_cap} "
                f"for {provider['name']}/{model}"
            )
            body[field] = out_cap


def _learn_token_cap_from_error(
    *,
    provider_name: str,
    model: str,
    status_code: int,
    body: str,
    est_tokens: int,
    requested_max_tokens: int,
) -> None:
    if not TOKEN_CAPS_ENABLED:
        return
    kind = classify_token_limit_error(
        status_code,
        body,
        est_tokens=est_tokens,
        requested_max_tokens=requested_max_tokens,
    )
    if not kind:
        return
    observed = est_tokens if kind == "input" else requested_max_tokens
    if observed <= 0:
        return
    token_caps.on_token_limit_failure(provider_name, model, kind, observed)


def _learn_token_cap_from_success(
    *,
    provider_name: str,
    model: str,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    provider: dict | None = None,
) -> None:
    if not TOKEN_CAPS_ENABLED:
        return
    input_bound = int(provider.get("skip_if_tokens_over") or 0) if provider else 0
    output_bound = int(provider.get("max_output_tokens") or 0) if provider else 0
    if prompt_tokens:
        token_caps.on_success_near_cap(
            provider_name, model, "input", int(prompt_tokens), input_bound
        )
    if completion_tokens:
        token_caps.on_success_near_cap(
            provider_name, model, "output", int(completion_tokens), output_bound
        )


def _shutdown_flush():
    try:
        rate_limiter.flush()
    except Exception:
        pass
    try:
        token_caps.flush()
    except Exception:
        pass

atexit.register(_shutdown_flush)


def _configured_rate_group_ids() -> set[str]:
    """Group ids that match currently configured provider keys/models."""
    ids: set[str] = set()
    for p in PROVIDERS:
        name = p["name"]
        keys = p.get("keys") or []
        models = list(p.get("models") or ([p.get("model")] if p.get("model") else []))
        if p.get("embed_model"):
            models.append(p["embed_model"])
        models = [m for m in dict.fromkeys(models) if m]
        for key in keys:
            ids.add(AdaptiveRateLimiter._group_key(name, key, None))
            for m in models:
                ids.add(AdaptiveRateLimiter._group_key(name, key, m))
    return ids


# Background: always re-read GI snapshot + manual overrides from disk on process
# start (snapshot is never persisted in router_state — only gi_overrides.json).
gi_ranking.reload_scores()
threading.Thread(target=_initialize_ratings, args=(PROVIDERS, pool), daemon=True).start()

# ── Per-provider stats ─────────────────────────────────────────────────────────

class ProviderStats:
    """Tracks latency and error rates per provider for observability."""

    def __init__(self):
        self.lock   = threading.Lock()
        self._data: dict[str, dict] = {}

    def _ensure(self, name: str):
        if name not in self._data:
            self._data[name] = {"latency_sum": 0.0, "latency_count": 0,
                                "error_count": 0, "request_count": 0,
                                "health": deque(maxlen=BREAKER_WINDOW), "open_until": 0.0}

    def record_success(self, name: str, latency_s: float):
        with self.lock:
            self._ensure(name)
            s = self._data[name]
            s["latency_sum"]   += latency_s
            s["latency_count"] += 1
            s["request_count"] += 1

    def record_error(self, name: str):
        with self.lock:
            self._ensure(name)
            s = self._data[name]
            s["error_count"]   += 1
            s["request_count"] += 1

    # ── Circuit breaker ──────────────────────────────────────────────────────
    def record_health(self, name: str, ok: bool):
        """Record a HEALTH outcome (separate from request stats — breaker only).
        On failure: trip the breaker open once the window has enough samples and
        the health-fail fraction crosses the threshold. On success: half-open
        recovery — close the breaker and wipe the window for a clean slate."""
        with self.lock:
            self._ensure(name)
            s   = self._data[name]
            win = s["health"]
            win.append(ok)
            if ok:
                s["open_until"] = 0.0
                win.clear()
            elif len(win) >= BREAKER_MIN_SAMPLES:
                fails = sum(1 for x in win if not x)
                if fails / len(win) >= BREAKER_ERROR_RATE:
                    s["open_until"] = time.time() + BREAKER_COOLDOWN

    def breaker_open(self, name: str) -> bool:
        with self.lock:
            s = self._data.get(name)
            return bool(s) and time.time() < s.get("open_until", 0.0)

    def breaker_status(self, name: str) -> dict:
        with self.lock:
            s   = self._data.get(name, {})
            now = time.time()
            open_until = s.get("open_until", 0.0)
            win   = s.get("health", ())
            fails = sum(1 for x in win if not x)
            return {"open": now < open_until,
                    "opens_in_s": max(0, round(open_until - now)),
                    "recent_health_fails": fails}

    def health_bucket(self, name: str) -> int:
        """Recent error-rate bucket for routing: 0 healthy / 1 degraded / 2 bad.
        Too few samples → 0 (unknown = healthy; don't penalize new providers)."""
        with self.lock:
            s = self._data.get(name)
            if not s:
                return 0
            win = s.get("health", ())
            if len(win) < BREAKER_MIN_SAMPLES:
                return 0
            err_rate = sum(1 for x in win if not x) / len(win)
            return 0 if err_rate < 0.10 else (1 if err_rate < 0.50 else 2)

    def summary(self, name: str) -> dict:
        with self.lock:
            s  = self._data.get(name, {})
            lc = s.get("latency_count", 0)
            rc = s.get("request_count", 0)
            ec = s.get("error_count", 0)
            return {
                "avg_latency_ms": round(s.get("latency_sum", 0) / lc * 1000) if lc else None,
                "error_rate":     round(ec / rc, 3) if rc else 0.0,
                "total_requests": rc,
                "errors":         ec,
            }

    def all_summaries(self) -> dict:
        with self.lock:
            return {name: self.summary(name) for name in self._data}


stats = ProviderStats()

# ── Request ring buffer ─────────────────────────────────────────────────────────
# Per-thread context written by _route_completion so endpoint handlers can read
# back routing metadata (chosen provider, model, cascade count) after the call
# returns without changing _route_completion's return signature.
_req_ctx = threading.local()


class RequestRingBuffer:
    """Fixed-size in-memory circular log of recent requests.

    Oldest entries are silently dropped when full. Never touches disk.
    Thread-safe: all mutations hold a lock."""

    def __init__(self, maxlen: int = 500):
        self._buf   = deque(maxlen=max(1, maxlen)) if maxlen > 0 else None
        self._lock  = threading.Lock()
        self.maxlen = maxlen

    def append(self, entry: dict) -> dict | None:
        if self._buf is None:
            return None
        with self._lock:
            self._buf.append(entry)
        return entry

    def update_entry(self, entry: dict, **fields) -> None:
        """Patch fields on an existing log entry if it is still in the buffer.
        No-op when the buffer is disabled or the entry has already rotated out."""
        if self._buf is None or not fields:
            return
        with self._lock:
            if any(e is entry for e in self._buf):
                entry.update(fields)

    def snapshot(self, limit: int = 100, provider: str | None = None,
                 status: str | None = None, endpoint: str | None = None) -> list:
        if self._buf is None:
            return []
        with self._lock:
            items = list(self._buf)
        if provider:
            items = [e for e in items if e.get("provider") == provider]
        if status:
            items = [e for e in items if e.get("status") == status]
        if endpoint:
            items = [e for e in items if e.get("endpoint") == endpoint]
        items = list(reversed(items))   # most recent first
        return items[:limit]

    def clear(self) -> None:
        if self._buf is None:
            return
        with self._lock:
            self._buf.clear()

    @property
    def size(self) -> int:
        if self._buf is None:
            return 0
        with self._lock:
            return len(self._buf)

    @property
    def enabled(self) -> bool:
        return self._buf is not None


request_log = RequestRingBuffer(maxlen=REQUEST_LOG_SIZE)

# ── Response cache ─────────────────────────────────────────────────────────────

def _cosine(a: list, b: list) -> float:
    """Cosine similarity of two equal-length vectors. Pure Python (vectors are
    short and the cache is bounded, so this is plenty fast); numpy not required."""
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y; na += x * x; nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / ((na ** 0.5) * (nb ** 0.5))


class ResponseCache:
    """
    In-memory LRU cache for non-streaming responses.
    Identical requests (same model + messages) return a cached copy,
    saving free-tier quota for novel queries.
    Set CACHE_TTL_SECONDS=0 to disable.
    """

    def __init__(self, ttl: int = 300, max_size: int = 100):
        self.ttl      = ttl
        self.max_size = max_size
        self.lock     = threading.Lock()
        self._store: OrderedDict = OrderedDict()  # hash -> (data, ts, ns, embedding|None)
        self.hits          = 0
        self.misses        = 0
        self.semantic_hits = 0

    def _hash(self, payload: dict, ns: str = "") -> str:
        # Hash the entire request (minus "stream", which doesn't change the
        # answer) so requests differing only in temperature, max_tokens,
        # tools, response_format, etc. never collide. `ns` namespaces the entry
        # to the authenticated caller, so two different API keys never share a
        # cached answer for an identical prompt (multi-tenant isolation).
        relevant = {k: v for k, v in payload.items() if k != "stream"}
        content = json.dumps({"ns": ns, "req": relevant}, sort_keys=True, default=str)
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def get(self, payload: dict, ns: str = "") -> dict | None:
        if self.ttl <= 0:
            return None
        key = self._hash(payload, ns)
        with self.lock:
            if key in self._store:
                data, ts, *_ = self._store[key]
                if time.time() - ts < self.ttl:
                    self._store.move_to_end(key)
                    self.hits += 1
                    return data
                del self._store[key]
            self.misses += 1
        return None

    def set(self, payload: dict, data: dict, ns: str = "", embedding: list | None = None):
        if self.ttl <= 0:
            return
        key = self._hash(payload, ns)
        ts  = time.time()
        with self.lock:
            if key not in self._store and len(self._store) >= self.max_size:
                self._store.popitem(last=False)  # evict oldest
            self._store[key] = (data, ts, ns, embedding)
            self._store.move_to_end(key)

    def semantic_lookup(self, query_emb: list, ns: str = "") -> dict | None:
        """Return the cached response whose stored prompt embedding is most similar
        to query_emb (same namespace, same vector dimension), if it clears
        SEMANTIC_THRESHOLD. Bounded linear scan over the LRU (max_size)."""
        if self.ttl <= 0 or not query_emb:
            return None
        now = time.time()
        qlen = len(query_emb)
        best_key, best_data, best_sim = None, None, 0.0
        with self.lock:
            for key, (data, ts, ens, emb) in self._store.items():
                if emb is None or ens != ns or len(emb) != qlen:
                    continue
                if now - ts >= self.ttl:
                    continue
                sim = _cosine(query_emb, emb)
                if sim > best_sim:
                    best_key, best_data, best_sim = key, data, sim
            if best_key is not None and best_sim >= SEMANTIC_THRESHOLD:
                self._store.move_to_end(best_key)
                self.semantic_hits += 1
                log.info(f"  semantic match sim={best_sim:.3f}")
                return best_data
        return None

    @property
    def size(self) -> int:
        with self.lock:
            return len(self._store)

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return round(self.hits / total, 3) if total else 0.0


cache = ResponseCache(ttl=CACHE_TTL, max_size=CACHE_MAX_SIZE)

# ── Per-key budgets & rate limits ("virtual keys" lite) ─────────────────────────
# Each PROXY_API_KEYS entry can carry a requests-per-minute ceiling and per-UTC-day
# request/token budgets, so the router is safe to share with a team. Limits come
# from auth.json under "proxy_keys" ({ "<key>": {"rpm","req_per_day","tokens_per_day"} }),
# with env-var globals (PROXY_LIMIT_RPM / PROXY_LIMIT_REQ_DAY / PROXY_LIMIT_TOKENS_DAY)
# as defaults. 0/absent everywhere = unlimited → identical to the prior behavior.

def _load_key_limits() -> dict:
    g_rpm    = _int_env("PROXY_LIMIT_RPM", 0)
    g_req    = _int_env("PROXY_LIMIT_REQ_DAY", 0)
    g_tokens = _int_env("PROXY_LIMIT_TOKENS_DAY", 0)
    try:    g_cost = float(os.environ.get("PROXY_LIMIT_COST_DAY", 0) or 0)
    except (TypeError, ValueError): g_cost = 0.0
    per_key = {}
    if AUTH_FILE.exists():
        try:
            doc = json.loads(AUTH_FILE.read_text())
            pk = doc.get("proxy_keys", {})
            if isinstance(pk, dict):
                per_key = pk
        except Exception as e:
            log.warning(f"Could not read proxy_keys from {AUTH_FILE}: {e}")
    limits = {}
    for k in PROXY_API_KEYS:
        spec = per_key.get(k) or {}
        limits[k] = {
            "rpm":            int(spec.get("rpm", g_rpm) or 0),
            "req_per_day":    int(spec.get("req_per_day", g_req) or 0),
            "tokens_per_day": int(spec.get("tokens_per_day", g_tokens) or 0),
            "cost_per_day":   float(spec.get("cost_per_day", g_cost) or 0),
        }
    return limits

KEY_LIMITS    = _load_key_limits()
KEY_LIMITS_ON = any(any(v.values()) for v in KEY_LIMITS.values())


def _load_key_provider_scope() -> dict:
    """Per-key provider allow-list (auth.json's proxy_keys[key].allowed_providers),
    set from the dashboard's Access Keys page. None = unrestricted (the default —
    backward compatible with every key that predates this feature); a set means
    that key's requests may only route through those providers."""
    per_key = {}
    if AUTH_FILE.exists():
        try:
            doc = json.loads(AUTH_FILE.read_text())
            pk = doc.get("proxy_keys", {})
            if isinstance(pk, dict):
                per_key = pk
        except Exception:
            pass
    scope = {}
    for k in PROXY_API_KEYS:
        allowed = (per_key.get(k) or {}).get("allowed_providers")
        scope[k] = set(allowed) if isinstance(allowed, list) and allowed else None
    return scope


KEY_PROVIDER_SCOPE = _load_key_provider_scope()


def _utc_day() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())

def _secs_to_utc_midnight() -> int:
    t = time.gmtime()
    return max(1, 86400 - (t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec))


class KeyUsage:
    """Thread-safe per-key counters: a rolling 60s window for RPM, plus per-UTC-day
    request and token tallies. In-memory (resets on restart)."""

    def __init__(self):
        self.lock = threading.Lock()
        self._win  = defaultdict(deque)   # key -> deque[timestamps] within the last 60s
        self._day  = defaultdict(lambda: {"day": "", "req": 0, "tokens": 0, "cost": 0.0})
        self._life = defaultdict(lambda: {"req": 0, "tokens": 0, "cost": 0.0})  # since start

    def _roll(self, key, now):
        w = self._win[key]
        cutoff = now - 60
        while w and w[0] < cutoff:
            w.popleft()

    def _day_bucket(self, key):
        d = self._day[key]
        today = _utc_day()
        if d["day"] != today:
            d.update(day=today, req=0, tokens=0, cost=0.0)
        return d

    def check_and_record(self, key, limits):
        """Atomically enforce this key's limits and, if allowed, count the request
        (RPM window + per-day + lifetime). `limits` may be all-zero, in which case
        nothing is gated and the request is simply recorded — so usage analytics
        work whether or not limits are configured. Returns (ok, retry, reason)."""
        rpm     = limits.get("rpm", 0)
        req_day = limits.get("req_per_day", 0)
        tpd     = limits.get("tokens_per_day", 0)
        cpd     = limits.get("cost_per_day", 0)
        now = time.time()
        with self.lock:
            d = self._day_bucket(key)
            self._roll(key, now)
            if cpd and d["cost"] >= cpd:
                return (False, _secs_to_utc_midnight(), f"${cpd:g} cost/day")
            if tpd and d["tokens"] >= tpd:
                return (False, _secs_to_utc_midnight(), f"{tpd} tokens/day")
            if rpm and len(self._win[key]) >= rpm:
                return (False, max(1, int(60 - (now - self._win[key][0]))), f"{rpm} requests/min")
            if req_day and d["req"] >= req_day:
                return (False, _secs_to_utc_midnight(), f"{req_day} requests/day")
            self._win[key].append(now)
            d["req"] += 1
            self._life[key]["req"] += 1
            return (True, 0, "")

    def add_tokens(self, key, n):
        n = int(n or 0)
        if not n:
            return
        with self.lock:
            self._day_bucket(key)["tokens"] += n
            self._life[key]["tokens"] += n

    def add_cost(self, key, usd):
        usd = float(usd or 0)
        if not usd:
            return
        with self.lock:
            self._day_bucket(key)["cost"] += usd
            self._life[key]["cost"] += usd

    def snapshot(self, key):
        with self.lock:
            d = self._day_bucket(key)
            self._roll(key, time.time())
            l = self._life[key]
            return {"req_today": d["req"], "tokens_today": d["tokens"],
                    "cost_today": round(d["cost"], 6),
                    "rpm_window": len(self._win[key]),
                    "req_total": l["req"], "tokens_total": l["tokens"],
                    "cost_total": round(l["cost"], 6)}


key_usage = KeyUsage()

# Cumulative tokens + estimated cost served per provider (from provider-reported
# usage). Streaming responses that include a usage chunk are counted too; those
# without usage count toward request totals but not tokens/cost.
_provider_tokens = defaultdict(int)
_provider_cost   = defaultdict(float)
_ptok_lock = threading.Lock()

def _add_provider_tokens(name: str, data: dict, model: str | None = None):
    usage = data.get("usage") or {}
    n = usage.get("total_tokens") or 0
    # Cost uses the prompt/completion split (input and output are priced
    # differently). Prefer the model the provider actually reports serving
    # (authoritative for pricing); fall back to the routed model name.
    cost = _cost(data.get("model") or model or "",
                 usage.get("prompt_tokens"), usage.get("completion_tokens"))
    if n or cost:
        with _ptok_lock:
            if n:
                _provider_tokens[name] += n
            if cost:
                _provider_cost[name] += cost

# ── Thinking field stripping ───────────────────────────────────────────────────
# Some providers (e.g. Gemini 2.5) emit reasoning/thinking fields in responses.
# These fields cause 400 errors on other providers (Groq, Cerebras, OpenRouter).
# We strip them from both outgoing requests and incoming responses.

def _strip_message(msg: dict):
    """Remove thinking fields from a message dict in-place."""
    msg.pop("reasoning_content", None)
    msg.pop("reasoning", None)
    msg.pop("think", None)
    if isinstance(msg.get("content"), list):
        msg["content"] = [
            b for b in msg["content"]
            if b.get("type") not in ("thinking", "think")
        ]


def _strip_response(data: dict):
    """Strip thinking fields from a non-streaming response before returning it."""
    for choice in data.get("choices", []):
        if "message" in choice:
            _strip_message(choice["message"])


def _choice_has_output(choice: dict) -> bool:
    """True when a chat-completion choice contains user-visible output or a tool
    call. Empty assistant messages are treated as unusable so failover can try
    another provider instead of caching a blank answer."""
    msg = choice.get("message") or {}
    if msg.get("tool_calls"):
        return True
    content = msg.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            text = part.get("text") or part.get("content")
            if isinstance(text, str) and text.strip():
                return True
    return False


def _completion_has_output(data: dict) -> bool:
    choices = data.get("choices")
    return isinstance(choices, list) and any(_choice_has_output(c) for c in choices)


def _streaming_generator(resp: requests.Response):
    """
    Yield SSE chunks with thinking fields stripped from delta objects.
    Buffers by newline to handle chunks that split across SSE boundaries.
    """
    buf = b""
    for raw_chunk in resp.iter_content(chunk_size=None):
        buf += raw_chunk
        while b"\n" in buf:
            line_bytes, buf = buf.split(b"\n", 1)
            line = line_bytes.decode("utf-8", errors="replace")
            if line.startswith("data: ") and line != "data: [DONE]":
                try:
                    event = json.loads(line[6:])
                    for choice in event.get("choices", []):
                        delta = choice.get("delta", {})
                        delta.pop("reasoning_content", None)
                        delta.pop("reasoning", None)
                        delta.pop("think", None)
                    yield ("data: " + json.dumps(event) + "\n").encode("utf-8")
                    continue
                except (json.JSONDecodeError, Exception):
                    pass
            yield (line + "\n").encode("utf-8")
    if buf:
        yield buf


def _with_cleanup(resp: requests.Response, gen):
    """Drive a streaming generator and always release the upstream connection
    when done — including when the client disconnects mid-stream (GeneratorExit).
    Without this, an aborted stream could keep an upstream socket checked out of
    the connection pool until garbage collection."""
    try:
        yield from gen
    finally:
        resp.close()


class _StreamWithUsage:
    """Iterable SSE wrapper that exposes captured usage for request-log patching."""
    __slots__ = ("_it", "_hermes_stream_usage")

    def __init__(self, it, captured: dict):
        self._it = it
        self._hermes_stream_usage = captured

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._it)

    def close(self):
        close = getattr(self._it, "close", None)
        if close is not None:
            close()


def _streaming_with_usage(gen, name: str, model: str | None = None, key: str | None = None,
                          resp_headers: dict = None, provider: dict | None = None,
                          est_tokens: float = 0.0, observed_at: float | None = None):
    """Wrap a streaming generator to capture the usage block from the final SSE
    chunk (present when stream_options.include_usage=true is sent upstream) and
    record tokens + cost in _provider_tokens/_provider_cost. Yields every chunk
    unchanged. Captured usage is exposed on the returned iterable as
    `_hermes_stream_usage` so handlers can patch the request log after the stream ends.

    When `key` and `est_tokens` are set, reconciles the rate-limiter reservation
    against measured usage (restore surplus / debit shortfall), matching the
    non-streaming path.
    """
    captured: dict = {}

    def _inner():
        usage: dict = {}
        for chunk in gen:
            text = chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else chunk
            for line in text.split("\n"):
                if line.startswith("data: ") and line != "data: [DONE]":
                    try:
                        event = json.loads(line[6:])
                        u = event.get("usage") or {}
                        if not u:
                            continue
                        # Some translators omit total_tokens; derive it from the split.
                        if not u.get("total_tokens"):
                            pt = u.get("prompt_tokens")
                            ct = u.get("completion_tokens")
                            if pt is not None or ct is not None:
                                u = {**u, "total_tokens": int(pt or 0) + int(ct or 0)}
                        if u.get("total_tokens"):
                            usage = u
                    except Exception:
                        pass
            yield chunk
        if usage:
            captured.clear()
            captured.update(usage)
            _add_provider_tokens(name, {"usage": usage}, model)
            _pt = usage.get("prompt_tokens")
            _ct = usage.get("completion_tokens")
            if _pt is not None or _ct is not None:
                _learn_token_cap_from_success(
                    provider_name=name,
                    model=model or "",
                    prompt_tokens=_pt,
                    completion_tokens=_ct,
                    provider=provider,
                )
            if key:
                _actual = float(usage.get("total_tokens") or 0)
                rate_limiter.reconcile(name, key, model, float(est_tokens or 0), _actual)
                rate_limiter.update_from_headers(
                    name, key, model, resp_headers or {}, observed_at=observed_at)
                rate_limiter.on_success(name, key, model, _actual)

    return _StreamWithUsage(_inner(), captured)


def _patch_stream_log_tokens(entry: dict | None, gen) -> None:
    """After a stream finishes, copy captured usage onto the request-log entry."""
    if entry is None:
        return
    usage = getattr(gen, "_hermes_stream_usage", None) or {}
    if not usage:
        return
    request_log.update_entry(
        entry,
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
    )

# ── Anthropic format translation ──────────────────────────────────────────────
# Anthropic's Messages API uses a different format from OpenAI. These helpers
# translate transparently so the caller never has to know which provider they hit.

def _openai_content_to_anthropic(content) -> list | str:
    """Convert an OpenAI content value (string or list) to Anthropic format.

    OpenAI image_url blocks become Anthropic image blocks (base64 or url source).
    Text blocks are preserved. Thinking/reasoning blocks are dropped (already
    stripped by _strip_message, but safe to double-check here).
    Returns a list when images are present, plain string otherwise.
    """
    if not isinstance(content, list):
        return content or ""
    converted = []
    for block in content:
        if not isinstance(block, dict):
            continue
        t = block.get("type")
        if t in ("thinking", "think"):
            continue
        if t == "text":
            converted.append({"type": "text", "text": block.get("text", "")})
        elif t == "image_url":
            url = (block.get("image_url") or {}).get("url", "")
            if url.startswith("data:"):
                # data:image/jpeg;base64,<data>
                try:
                    header, data = url.split(",", 1)
                    media_type = header.split(";")[0][5:]  # strip "data:"
                    converted.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": media_type, "data": data},
                    })
                except Exception:
                    pass  # malformed data URL — skip
            elif url.startswith(("http://", "https://")):
                converted.append({
                    "type": "image",
                    "source": {"type": "url", "url": url},
                })
        # unknown types are silently dropped
    if not converted:
        return ""
    # If it's purely text with no images, return a plain string for cleanliness
    if all(b.get("type") == "text" for b in converted):
        return " ".join(b.get("text", "") for b in converted)
    return converted


def _merge_anthropic_content(existing, incoming) -> list | str:
    """Merge two content values when combining consecutive same-role messages.
    Produces a list when either side contains images, plain string otherwise."""
    def _to_list(c) -> list:
        if isinstance(c, list):
            return c
        return [{"type": "text", "text": c}] if c else []

    if isinstance(existing, list) or isinstance(incoming, list):
        merged = _to_list(existing) + _to_list(incoming)
        # Collapse back to string if no images remain
        if all(b.get("type") == "text" for b in merged):
            return " ".join(b.get("text", "") for b in merged)
        return merged
    # Both plain strings
    return (existing + "\n" + incoming) if existing else incoming


def _to_anthropic_body(payload: dict, model: str) -> dict:
    """Convert an OpenAI chat-completions request body to Anthropic Messages format.
    Image content (image_url blocks) is translated to Anthropic image blocks so
    vision requests work correctly when routed to Claude.
    """
    system_parts = []
    messages = []
    for msg in payload.get("messages", []):
        role = msg.get("role", "")
        content = _openai_content_to_anthropic(msg.get("content", ""))
        if role == "system":
            # System content is always plain text in Anthropic's API
            text = " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text") if isinstance(content, list) else content
            system_parts.append(text)
        else:
            # Merge consecutive same-role messages (Anthropic requires alternating roles)
            if messages and messages[-1]["role"] == role:
                messages[-1]["content"] = _merge_anthropic_content(
                    messages[-1]["content"], content
                )
            else:
                messages.append({"role": role, "content": content})

    body: dict = {
        "model":      model,
        "messages":   messages,
        "max_tokens": payload.get("max_tokens") or 1024,
    }
    if system_parts:
        system_text = "\n".join(system_parts)
        # Anthropic prompt caching: mark system prompt for caching when it's long
        # enough to qualify (≥ 1024 tokens; estimated as ≥ 4096 chars). Cached
        # tokens are billed at 10% on subsequent requests — transparent to the caller.
        if len(system_text) >= 4096:
            body["system"] = [{"type": "text", "text": system_text,
                                "cache_control": {"type": "ephemeral"}}]
        else:
            body["system"] = system_text
    if payload.get("stream"):
        body["stream"] = True
    if payload.get("temperature") is not None:
        body["temperature"] = payload["temperature"]
    stop = payload.get("stop")
    if stop:
        body["stop_sequences"] = stop if isinstance(stop, list) else [stop]
    return body


def _from_anthropic_response(data: dict) -> dict:
    """Convert an Anthropic Messages response to OpenAI chat-completion format."""
    content = "".join(
        b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
    )
    stop_reason = data.get("stop_reason", "end_turn")
    finish_reason = "stop" if stop_reason in ("end_turn", "stop_sequence") else "length"
    usage = data.get("usage", {})
    prompt_tokens = usage.get("input_tokens", 0)
    completion_tokens = usage.get("output_tokens", 0)
    out: dict = {
        "id":      data.get("id", "msg_unknown"),
        "object":  "chat.completion",
        "created": int(time.time()),
        "model":   data.get("model", ""),
        "choices": [{
            "index":         0,
            "message":       {"role": "assistant", "content": content},
            "finish_reason": finish_reason,
        }],
        "usage": {
            "prompt_tokens":     prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens":      prompt_tokens + completion_tokens,
        },
    }
    # Pass through Anthropic cache token counts when present so callers can
    # observe cache savings without breaking OpenAI-compatible clients.
    if usage.get("cache_read_input_tokens"):
        out["usage"]["cache_read_input_tokens"] = usage["cache_read_input_tokens"]
    if usage.get("cache_creation_input_tokens"):
        out["usage"]["cache_creation_input_tokens"] = usage["cache_creation_input_tokens"]
    return out


def _anthropic_streaming_generator(resp: requests.Response):
    """Translate Anthropic SSE stream to OpenAI SSE format token-by-token."""
    msg_id       = f"chatcmpl-{int(time.time())}"
    model        = ""
    created      = int(time.time())
    finish_reason = "stop"
    first_chunk  = True
    prompt_tokens = None
    completion_tokens = None

    buf = b""
    for raw_chunk in resp.iter_content(chunk_size=None):
        buf += raw_chunk
        while b"\n" in buf:
            line_bytes, buf = buf.split(b"\n", 1)
            line = line_bytes.decode("utf-8", errors="replace").strip()
            if not line.startswith("data: "):
                continue
            data_str = line[6:]
            try:
                event = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            etype = event.get("type")

            if etype == "message_start":
                msg    = event.get("message", {})
                msg_id = msg.get("id", msg_id)
                model  = msg.get("model", "")
                usage  = msg.get("usage") or {}
                if "input_tokens" in usage:
                    prompt_tokens = usage.get("input_tokens", 0)
                # Emit role chunk
                chunk = {"id": msg_id, "object": "chat.completion.chunk", "created": created,
                         "model": model,
                         "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""},
                                      "finish_reason": None}]}
                yield ("data: " + json.dumps(chunk) + "\n\n").encode()
                first_chunk = False

            elif etype == "content_block_delta":
                delta = event.get("delta", {})
                if delta.get("type") == "text_delta":
                    text  = delta.get("text", "")
                    chunk = {"id": msg_id, "object": "chat.completion.chunk", "created": created,
                             "model": model,
                             "choices": [{"index": 0, "delta": {"content": text},
                                          "finish_reason": None}]}
                    yield ("data: " + json.dumps(chunk) + "\n\n").encode()

            elif etype == "message_delta":
                sr = event.get("delta", {}).get("stop_reason", "end_turn")
                finish_reason = "stop" if sr in ("end_turn", "stop_sequence") else "length"
                usage = event.get("usage") or {}
                if "output_tokens" in usage:
                    completion_tokens = usage.get("output_tokens", 0)

            elif etype == "message_stop":
                chunk = {"id": msg_id, "object": "chat.completion.chunk", "created": created,
                         "model": model,
                         "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]}
                yield ("data: " + json.dumps(chunk) + "\n\n").encode()
                if prompt_tokens is not None or completion_tokens is not None:
                    pt = int(prompt_tokens or 0)
                    ct = int(completion_tokens or 0)
                    usage_chunk = {
                        "id": msg_id, "object": "chat.completion.chunk", "created": created,
                        "model": model, "choices": [],
                        "usage": {
                            "prompt_tokens":     pt,
                            "completion_tokens": ct,
                            "total_tokens":      pt + ct,
                        },
                    }
                    yield ("data: " + json.dumps(usage_chunk) + "\n\n").encode()
                yield b"data: [DONE]\n\n"


# ── Complexity-aware provider ordering ────────────────────────────────────────

# Accurate token counting via tiktoken when available. The encoder is loaded
# lazily on first use (not at import) so startup never blocks on tiktoken's
# one-time vocab download, and any failure (no tiktoken, offline, etc.) falls
# back to the character heuristic — the router always works regardless.
_ENCODER = "uninitialized"  # sentinel; resolves to an encoder or None on first use


def _get_encoder():
    global _ENCODER
    if _ENCODER == "uninitialized":
        try:
            import tiktoken
            _ENCODER = tiktoken.get_encoding("o200k_base")
        except Exception as e:
            log.warning(f"tiktoken unavailable ({e}); using char/4 token estimate")
            _ENCODER = None
    return _ENCODER


def _message_text(m: dict) -> str:
    """Extract plain text from a message whose content is either a string or a
    list of multimodal parts (only text parts contribute to the token count)."""
    content = m.get("content", "")
    if isinstance(content, list):
        return " ".join(
            p.get("text", "") for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return str(content)


def _estimated_tokens(messages: list) -> int:
    """Token count for a message list. Uses tiktoken for an accurate count when
    available, otherwise a characters/4 heuristic. Adds a small per-message
    framing overhead (~4 tokens) plus 3 priming tokens, matching how chat
    models actually bill structured messages."""
    enc = _get_encoder()
    if enc is not None:
        total = 3
        for m in messages:
            total += 4 + len(enc.encode(_message_text(m)))
        return total
    return sum(len(_message_text(m)) for m in messages) // 4


def _ordered_providers(payload: dict, prefer_local: bool = False,
                       sticky: dict | None = None) -> list[dict]:
    """
    Complexity-aware catalog selection: use cheapest capable model for simple
    tasks, best model for complex ones. With FAST_ROUTE_THRESHOLD set,
    short requests break ties in favour of low-latency providers. With
    prefer_local (the `:fast` preference), a local model leads on easy turns.
    """
    messages   = payload.get("messages", [])
    complexity = classify_complexity(messages)
    ordered    = _get_smart_ordered(PROVIDERS, complexity, _estimated_tokens(messages),
                                    prefer_local, sticky=sticky)
    log.info(f"→ complexity={complexity} ({_COMPLEXITY_LABELS[complexity]}) "
             f"order={[c['provider']['name'] + '/' + c['model'] for c in ordered]}")
    return ordered

# ── Codex (Responses API) format translation ──────────────────────────────────
# Codex speaks OpenAI's Responses API (not Chat Completions). These helpers
# translate transparently, like the Anthropic ones above.

def _to_codex_body(payload: dict, model: str) -> dict:
    """Convert an OpenAI chat-completions body to a Codex Responses-API request."""
    instructions = []
    input_items = []
    for msg in payload.get("messages", []):
        role = msg.get("role", "")
        content = msg.get("content", "")
        if isinstance(content, list):       # flatten structured content to text
            content = "".join(p.get("text", "") for p in content
                              if isinstance(p, dict) and p.get("type") in ("text", "input_text", "output_text"))
        content = content or ""
        if role == "system":
            instructions.append(content)
            continue
        # assistant turns use output_text; user/tool use input_text
        ctype = "output_text" if role == "assistant" else "input_text"
        input_items.append({"type": "message", "role": role,
                             "content": [{"type": ctype, "text": content}]})

    body: dict = {
        "model":        model,
        "input":        input_items,
        "store":        False,
        "stream":       True,        # Codex backend always streams (SSE)
        # Codex requires a non-empty `instructions`; use the client's system
        # message(s) or a minimal default so requests without one still work.
        "instructions": "\n".join(instructions) if instructions
                        else "You are a helpful assistant.",
    }

    # tools: OpenAI nests under {"function": {...}}; Responses wants them flat.
    tools = []
    for t in payload.get("tools", []) or []:
        if t.get("type") == "function" and isinstance(t.get("function"), dict):
            fn = t["function"]
            tools.append({"type": "function", "name": fn.get("name", ""),
                          "description": fn.get("description", ""),
                          "strict": False, "parameters": fn.get("parameters", {})})
    if tools:
        body["tools"] = tools
        tc = payload.get("tool_choice")
        body["tool_choice"] = tc if isinstance(tc, str) else "auto"
        body["parallel_tool_calls"] = bool(payload.get("parallel_tool_calls", True))

    # reasoning effort (OpenAI clients pass reasoning_effort; default medium)
    effort = payload.get("reasoning_effort") or "medium"
    body["reasoning"] = {"effort": effort}
    body["include"] = ["reasoning.encrypted_content"]
    return body


def _codex_text_and_tools(data: dict):
    """Pull assistant text and any tool calls out of a Responses `output` array."""
    text_parts, tool_calls = [], []
    for item in data.get("output", []) or []:
        itype = item.get("type")
        if itype == "message":
            for c in item.get("content", []) or []:
                if c.get("type") in ("output_text", "text"):
                    text_parts.append(c.get("text", ""))
        elif itype in ("function_call", "tool_call"):
            tool_calls.append({
                "id":   item.get("call_id") or item.get("id") or f"call_{len(tool_calls)}",
                "type": "function",
                "function": {"name": item.get("name", ""),
                             "arguments": item.get("arguments", "") or "{}"},
            })
    return "".join(text_parts), tool_calls


def _from_codex_response(events: list) -> dict:
    """Aggregate a list of Responses SSE event objects into one OpenAI
    chat-completion JSON (used for non-streaming clients)."""
    final = {}
    text_acc = []
    for ev in events:
        t = ev.get("type", "")
        if t == "response.completed" and isinstance(ev.get("response"), dict):
            final = ev["response"]
        elif t == "response.output_text.delta":
            text_acc.append(ev.get("delta", ""))
    text, tool_calls = _codex_text_and_tools(final) if final else ("", [])
    if not text and text_acc:
        text = "".join(text_acc)
    message = {"role": "assistant", "content": text or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    finish = "tool_calls" if tool_calls else "stop"
    usage = (final.get("usage") or {}) if final else {}
    return {
        "id":      final.get("id", "chatcmpl-codex"),
        "object":  "chat.completion",
        "created": int(time.time()),
        "model":   final.get("model", "codex"),
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {
            "prompt_tokens":     usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens":      usage.get("total_tokens", 0),
        },
    }


def _codex_streaming_generator(resp: requests.Response):
    """Translate a Codex Responses SSE stream into OpenAI chat.completion.chunk
    SSE on the fly."""
    cid = "chatcmpl-codex"
    created = int(time.time())
    model = "codex"

    def chunk(delta: dict, finish=None):
        return "data: " + json.dumps({
            "id": cid, "object": "chat.completion.chunk", "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }) + "\n\n"

    yield chunk({"role": "assistant"})
    event_type = None
    finish = "stop"
    usage_out = None
    for raw in resp.iter_lines():
        if not raw:
            continue
        raw = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        if raw.startswith("event:"):
            event_type = raw[6:].strip()
            continue
        if not raw.startswith("data:"):
            continue
        data_str = raw[5:].strip()
        if data_str == "[DONE]":
            break
        try:
            ev = json.loads(data_str)
        except Exception:
            continue
        etype = ev.get("type") or event_type
        if etype == "response.output_text.delta":
            d = ev.get("delta", "")
            if d:
                yield chunk({"content": d})
        elif etype == "response.completed" and isinstance(ev.get("response"), dict):
            resp_obj = ev["response"]
            cid = resp_obj.get("id", cid) or cid
            if resp_obj.get("model"):
                model = resp_obj["model"]
            _, tcs = _codex_text_and_tools(resp_obj)
            if tcs:
                finish = "tool_calls"
                for i, tc in enumerate(tcs):
                    yield chunk({"tool_calls": [{"index": i, **tc}]})
            u = resp_obj.get("usage") or {}
            if u:
                pt = int(u.get("input_tokens") or 0)
                ct = int(u.get("output_tokens") or 0)
                usage_out = {
                    "prompt_tokens":     pt,
                    "completion_tokens": ct,
                    "total_tokens":      int(u.get("total_tokens") or (pt + ct)),
                }
    yield chunk({}, finish=finish)
    if usage_out:
        yield "data: " + json.dumps({
            "id": cid, "object": "chat.completion.chunk", "created": created,
            "model": model, "choices": [], "usage": usage_out,
        }) + "\n\n"
    yield "data: [DONE]\n\n"


# ── Request forwarding ─────────────────────────────────────────────────────────

def _is_auto_model_id(model: str | None) -> bool:
    """True when the client asked for proxy auto-selection (not a pinned model)."""
    m = str(model or "").strip()
    if not m or m == ROUTER_MODEL or m == "auto":
        return True
    return m.endswith(":fast")


def _models_match_normalized(a: str, b: str) -> bool:
    na = gi_ranking.normalize_model_id(a)
    nb = gi_ranking.normalize_model_id(b)
    return bool(na) and na == nb


def _filter_candidates_by_pin(ordered: list, pin_model: str) -> list:
    return [c for c in ordered if _models_match_normalized(c.get("model") or "", pin_model)]


def _chat_catalog_model_ids(providers: list | None = None) -> list[str]:
    """Distinct live chat catalog model strings (config/discovery lists), first-seen order."""
    out: list[str] = []
    seen: set[str] = set()
    for p in (providers if providers is not None else PROVIDERS):
        models = p.get("models") or ([p["model"]] if p.get("model") else [])
        for m in models:
            s = str(m or "").strip()
            if not s or s in seen:
                continue
            seen.add(s)
            out.append(s)
    return out


def _resolve_model(provider: dict, payload: dict, model: str | None) -> str:
    """Which model to actually send: the explicit one the failover loop chose,
    else the client's (if it named a real model), else the provider's primary."""
    if model:
        return model
    m = payload.get("model", "")
    if not m or m in ("", ROUTER_MODEL, "auto"):
        return provider["model"]
    return m


ttft_baselines = TtftBaselineStore()


def _extend_response_read_timeout(resp, seconds: float) -> None:
    """Best-effort: after TTFT, allow long body/stream reads."""
    try:
        sock = getattr(getattr(getattr(resp, "raw", None), "_fp", None), "fp", None)
        raw = getattr(sock, "raw", None) if sock is not None else None
        s = getattr(raw, "_sock", None) if raw is not None else None
        if s is not None:
            s.settimeout(seconds)
            return
    except Exception:
        pass
    try:
        conn = getattr(resp, "connection", None)
        s = getattr(conn, "sock", None) if conn is not None else None
        if s is not None:
            s.settimeout(seconds)
    except Exception:
        pass


def _http_post_ttft(
    *,
    provider_name: str,
    model_id: str,
    url: str,
    headers: dict,
    json_body: dict,
    stream: bool,
    first_byte_deadline_s: float | None,
    default_read_s: float,
    extend_s: float = 180.0,
) -> requests.Response | None:
    """POST with optional TTFT read deadline; record successful header TTFT."""
    t0 = time.time()
    read_s = first_byte_deadline_s if first_byte_deadline_s is not None else default_read_s
    try:
        resp = _HTTP.post(
            url, headers=headers, json=json_body, stream=stream, timeout=(10, read_s),
        )
    except requests.exceptions.ReadTimeout as e:
        if first_byte_deadline_s is not None:
            raise TtftDeadlineExceeded(first_byte_deadline_s, time.time() - t0) from e
        log.error(f"  Network error → {provider_name}: {e}")
        return None
    except requests.exceptions.RequestException as e:
        log.error(f"  Network error → {provider_name}: {e}")
        return None
    ttft_baselines.record(provider_name, model_id, time.time() - t0)
    _extend_response_read_timeout(resp, extend_s)
    return resp


def forward(provider: dict, key: str, payload: dict, streaming: bool,
            model: str | None = None,
            first_byte_deadline_s: float | None = None) -> requests.Response | None:
    # Codex (ChatGPT OAuth) speaks the Responses API — translate and send directly.
    if provider.get("protocol") == "codex":
        token = codex_creds.get_access_token(key)   # key is the account_id
        if not token:
            log.error(f"  codex: no valid token for account ...{key[-6:]}")
            return None
        model = _resolve_model(provider, payload, model)
        cleaned = []
        for msg in payload.get("messages", []):
            m = dict(msg); _strip_message(m); cleaned.append(m)
        body = _to_codex_body({**payload, "messages": cleaned}, model)
        hdrs = {
            "Authorization":      f"Bearer {token}",
            "chatgpt-account-id": key,
            "Content-Type":       "application/json",
            "Accept":             "text/event-stream",
            "originator":         "codex_cli_rs",
            "OpenAI-Beta":        "responses=experimental",
        }
        return _http_post_ttft(
            provider_name=provider["name"],
            model_id=model,
            url=provider["base_url"].rstrip("/") + "/responses",
            headers=hdrs,
            json_body=body,
            stream=True,
            first_byte_deadline_s=first_byte_deadline_s,
            default_read_s=180.0,
            extend_s=180.0,
        )

    # Anthropic uses a different wire format — translate and send directly.
    if provider.get("protocol") == "anthropic":
        model = _resolve_model(provider, payload, model)
        cleaned = []
        for msg in payload.get("messages", []):
            m = dict(msg)
            _strip_message(m)
            cleaned.append(m)
        body = _to_anthropic_body({**payload, "messages": cleaned}, model)
        hdrs = {"x-api-key": key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}
        return _http_post_ttft(
            provider_name=provider["name"],
            model_id=model,
            url="https://api.anthropic.com/v1/messages",
            headers=hdrs,
            json_body=body,
            stream=streaming,
            first_byte_deadline_s=first_byte_deadline_s,
            default_read_s=120.0,
            extend_s=120.0,
        )

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type":  "application/json",
        **provider.get("headers", {}),
    }

    body = dict(payload)

    # Use the model the failover loop chose (else placeholder → provider's primary)
    body["model"] = _resolve_model(provider, payload, model)

    # Strip thinking fields from conversation history before forwarding
    if "messages" in body:
        cleaned = []
        for msg in body["messages"]:
            m = dict(msg)
            _strip_message(m)
            cleaned.append(m)
        body["messages"] = cleaned

    # Strip top-level thinking fields (Gemini sometimes adds these)
    body.pop("think", None)
    body.pop("thinking", None)

    # Reasoning models spend output tokens on hidden chain-of-thought, so a small
    # client max_tokens can be entirely consumed by thinking — leaving empty
    # content. Give reasoning models extra headroom on top of what the client
    # asked for, so the actual answer still fits. (The model stops when done, so
    # short answers stay short.) Tune/disable with REASONING_TOKEN_RESERVE.
    # Per-model: only the actual model being sent gets the reserve.
    if _model_caps(provider["name"], body["model"]).get("reasoning"):
        reserve = _int_env("REASONING_TOKEN_RESERVE", 4096)
        if reserve > 0:
            for field in ("max_tokens", "max_completion_tokens"):
                if isinstance(body.get(field), int):
                    body[field] += reserve

    # Clamp the requested output length to this provider's hard ceiling. Some
    # providers (e.g. Cohere caps output at 8192) reject the ENTIRE request with
    # a 400 when max_tokens exceeds their limit — so a client default like
    # max_tokens=65536 would fail every call. Capping it lets the request through;
    # the model still produces up to its real maximum.
    _apply_output_token_cap(body, provider, body["model"])

    # Ask the provider to include usage in the final SSE chunk so _streaming_with_usage
    # can record actual tokens. Non-destructive: merges with any stream_options the
    # client already sent. Most OpenAI-compatible providers support this.
    if streaming:
        body.setdefault("stream_options", {})
        body["stream_options"]["include_usage"] = True

    url = provider["base_url"].rstrip("/") + "/chat/completions"
    return _http_post_ttft(
        provider_name=provider["name"],
        model_id=body["model"],
        url=url,
        headers=headers,
        json_body=body,
        stream=streaming,
        first_byte_deadline_s=first_byte_deadline_s,
        default_read_s=120.0,
        extend_s=180.0,
    )


def _embed_ordered() -> list[dict]:
    """Embedding-capable providers in a STABLE priority order — deliberately NOT
    round-robined like chat. Different providers return different vector
    dimensions (e.g. gemini 3072, cohere 1536, mistral 1024), and vectors of
    different dimensions can't be compared in one store. So we keep hitting the
    same provider and only fail over (accepting a dimension change) when it's
    actually down. Open breakers and unhealthy providers sink to the back; the
    sort is stable, so healthy providers keep their config order as the priority.

    For STRICT single-dimension guarantees, disable the others' embed models
    (e.g. MISTRAL_EMBED_MODEL= and COHERE_EMBED_MODEL= empty in .env)."""
    embed_providers = [p for p in PROVIDERS if p.get("embed_model")]
    return sorted(embed_providers, key=lambda p: (1 if stats.breaker_open(p["name"]) else 0,
                                                  stats.health_bucket(p["name"])))


def _prompt_text(messages: list) -> str:
    """Flatten a chat request's message text for semantic-cache embedding."""
    return " ".join(_message_text(m) for m in messages if m.get("content")).strip()[:8000]


def _embed_text(text: str) -> list | None:
    """Embed text via the internal embeddings pipeline (used by the semantic cache).
    Returns a vector, or None if no embed provider is available / all are cooling."""
    if not text:
        return None
    body = {"input": text}
    for provider in _embed_ordered():
        em  = provider["embed_model"]
        key = pool.get_key(provider["name"], em)
        if not key:
            continue
        try:
            resp = forward_embeddings(provider, key, body)
        except Exception:
            resp = None
        if resp is not None and 200 <= resp.status_code < 300:
            try:
                return resp.json()["data"][0]["embedding"]
            except Exception:
                pass
    return None


def forward_embeddings(provider: dict, key: str, payload: dict) -> requests.Response | None:
    """POST an OpenAI-format embeddings request to a provider, substituting the
    provider's configured embed model. No streaming, no format translation."""
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type":  "application/json",
        **provider.get("headers", {}),
    }
    body = dict(payload)
    body["model"] = provider["embed_model"]   # always the provider's real embed model
    url = provider["base_url"].rstrip("/") + "/embeddings"
    try:
        return _HTTP.post(url, headers=headers, json=body, timeout=(10, 120))
    except requests.exceptions.RequestException as e:
        log.error(f"  Network error → {provider['name']} embeddings: {e}")
        return None

# ── Flask app ──────────────────────────────────────────────────────────────────

app = Flask(__name__)
# Cap request bodies so a buggy client can't exhaust memory (Flask returns 413)
app.config["MAX_CONTENT_LENGTH"] = _int_env("MAX_REQUEST_BYTES", 10 * 1024 * 1024)
START_TIME = time.time()   # for uptime in /v1/status


def _caller_token() -> str:
    """The API key the caller presented (Bearer, or x-api-key)."""
    header = request.headers.get("Authorization", "").strip()
    token  = header[7:].strip() if header[:7].lower() == "bearer " else header
    if not token:
        token = request.headers.get("x-api-key", "").strip()
    return token


def _auth_check():
    token = _caller_token()
    # compare_digest keeps the comparison constant-time per key
    if not any(hmac.compare_digest(token, k) for k in PROXY_API_KEYS):
        return jsonify({"error": "unauthorized"}), 401


def _cache_ns() -> str:
    """Cache namespace = the authenticated caller, so different API keys never
    share a cached response for an identical request."""
    return _caller_token()


def _admit_request(token: str):
    """Enforce this caller's rate/budget limits AND record the request for usage
    analytics (recording happens whether or not limits are set). Returns a Flask
    (response, 429) tuple to short-circuit when over limit, or None to proceed."""
    limits = KEY_LIMITS.get(token) or {}
    ok, retry, reason = key_usage.check_and_record(token, limits)
    if ok:
        return None
    resp = jsonify({"error": {"message": f"quota exceeded ({reason})",
                              "type": "rate_limit_error"}})
    resp.headers["Retry-After"] = str(retry)
    return resp, 429


def _record_request_tokens(token: str, payload: dict, result):
    """Post-flight: add this request's tokens (and estimated cost) to the caller's
    tally (daily + lifetime). Uses provider-reported usage when present, else an
    estimate (e.g. streaming). Cost uses the response model's prompt/completion
    split when available; $0 for free/unpriced models."""
    n = 0
    if result and result[0] == "json":
        data  = result[1]
        usage = data.get("usage") or {}
        n = usage.get("total_tokens") or 0
        cost = _cost(data.get("model") or payload.get("model") or "",
                     usage.get("prompt_tokens"), usage.get("completion_tokens"))
        if cost:
            key_usage.add_cost(token, cost)
    if not n:
        n = _estimated_tokens(payload.get("messages", []))
    key_usage.add_tokens(token, n)


_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hermes Router — Dashboard</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0f1117;--surface:#181c27;--surface2:#1e2333;--border:#2a3050;
  --text:#e2e8f0;--muted:#8892a4;--accent:#6c8cff;--green:#4ade80;
  --yellow:#facc15;--red:#f87171;--orange:#fb923c;--purple:#c084fc;
  --font:'Inter',system-ui,sans-serif;
}
body{background:var(--bg);color:var(--text);font-family:var(--font);font-size:13px;min-height:100vh}

/* ── layout ── */
header{display:flex;align-items:center;justify-content:space-between;padding:14px 20px;
  background:var(--surface);border-bottom:1px solid var(--border);position:sticky;top:0;z-index:10}
header h1{font-size:15px;font-weight:600;letter-spacing:.3px;color:var(--text)}
header h1 span{color:var(--accent)}
.badge{display:inline-flex;align-items:center;gap:5px;font-size:11px;padding:3px 8px;
  border-radius:99px;background:var(--surface2);border:1px solid var(--border);color:var(--muted)}
.badge .dot{width:7px;height:7px;border-radius:50%;background:var(--green);box-shadow:0 0 5px var(--green)}
.badge .dot.err{background:var(--red);box-shadow:0 0 5px var(--red)}
.header-right{display:flex;align-items:center;gap:10px}
#last-update{font-size:11px;color:var(--muted)}
.btn{cursor:pointer;font-size:11px;padding:4px 10px;border-radius:6px;border:1px solid var(--border);
  background:var(--surface2);color:var(--text);transition:.15s}
.btn:hover{border-color:var(--accent);color:var(--accent)}

/* ── app shell / sidebar ── */
.app-shell{display:flex;align-items:stretch;min-height:100vh}
.sidebar{width:200px;flex:0 0 auto;background:var(--surface);border-right:1px solid var(--border);
  display:flex;flex-direction:column;padding:16px 0;position:sticky;top:0;align-self:flex-start;
  height:100vh}
.sidebar-brand{padding:0 16px 14px;font-size:15px;font-weight:600;letter-spacing:.3px;
  border-bottom:1px solid var(--border);margin-bottom:12px}
.sidebar-brand span{color:var(--accent)}
.sidebar-nav{display:flex;flex-direction:column;gap:2px;padding:0 8px}
.nav-item{display:flex;align-items:center;justify-content:space-between;gap:8px;width:100%;
  text-align:left;padding:9px 10px;border-radius:7px;border:none;background:transparent;
  color:var(--muted);font-size:12.5px;font-family:inherit;cursor:pointer;transition:.15s}
.nav-item:hover{background:var(--surface2);color:var(--text)}
.nav-item.active{background:var(--surface2);color:var(--accent);font-weight:600}
.nav-item .nav-dot{width:6px;height:6px;border-radius:50%;background:var(--border);flex:0 0 auto}
.nav-item .nav-dot.warn{background:var(--yellow);box-shadow:0 0 4px var(--yellow)}
.nav-item .nav-dot.bad{background:var(--red);box-shadow:0 0 4px var(--red)}
.app-main{flex:1;min-width:0;display:flex;flex-direction:column}
@media(max-width:760px){
  .app-shell{flex-direction:column}
  .sidebar{width:100%;height:auto;position:static;flex-direction:row;overflow-x:auto;padding:10px 8px}
  .sidebar-brand{display:none}
  .sidebar-nav{flex-direction:row}
  .nav-item{white-space:nowrap}
}

main{padding:18px 20px;display:grid;gap:16px;max-width:1180px;margin:0 auto;width:100%}
.page{display:none}
.page.active{display:grid;gap:16px}
.page-intro{color:var(--muted);line-height:1.45}

/* ── key input overlay ── */
#key-gate{position:fixed;inset:0;background:rgba(0,0,0,.7);display:flex;
  align-items:center;justify-content:center;z-index:100}
#key-gate.hidden{display:none}
.hidden{display:none !important}
.gate-box{background:var(--surface);border:1px solid var(--border);border-radius:12px;
  padding:28px 32px;min-width:320px;text-align:center}
.gate-box h2{margin-bottom:6px;font-size:15px}
.gate-box p{color:var(--muted);font-size:12px;margin-bottom:18px}
.gate-box input{width:100%;padding:8px 12px;border-radius:7px;border:1px solid var(--border);
  background:var(--bg);color:var(--text);font-size:13px;outline:none;margin-bottom:10px}
.gate-box input:focus{border-color:var(--accent)}
.gate-box .btn{width:100%;padding:7px}

/* ── rate-limit detail modal ── */
#rl-detail-modal{position:fixed;inset:0;background:rgba(0,0,0,.7);display:flex;
  align-items:center;justify-content:center;z-index:90;padding:16px}
#rl-detail-modal.hidden{display:none}
#cascade-detail-modal{position:fixed;inset:0;background:rgba(0,0,0,.7);display:flex;
  align-items:center;justify-content:center;z-index:90;padding:16px}
#cascade-detail-modal.hidden{display:none}
.rl-detail-box{background:var(--surface);border:1px solid var(--border);border-radius:12px;
  width:min(720px,92vw);max-height:min(80vh,640px);display:flex;flex-direction:column;overflow:hidden}
.rl-detail-box .panel-header{flex:0 0 auto}
.rl-detail-actions{display:flex;gap:8px;align-items:center}
.rl-detail-box .panel-body{overflow:auto;flex:1;min-height:0;padding:0}
.rl-key-section{padding:12px 14px;border-bottom:1px solid var(--border)}
.rl-key-section:last-child{border-bottom:none}
.rl-key-section-hdr{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:10px}
.rl-key-section-label{display:flex;align-items:center;gap:6px;font-size:12px}
.rl-dim-sep{height:1px;background:var(--border);margin:10px 0 8px;opacity:.85}
.rl-bar-row{display:flex;align-items:center;gap:8px;margin:5px 0;font-size:11px}
.rl-bar-row.muted{opacity:.45}
.rl-bar-name{flex:0 0 42px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted)}
.rl-bar-track{flex:1;min-width:0;height:16px;background:var(--bg);border:1px solid var(--border);
  border-radius:4px;position:relative;overflow:hidden}
.rl-bar-fill{height:100%;background:var(--accent);border-radius:3px;display:flex;align-items:center;
  justify-content:flex-end;padding:0 4px;box-sizing:border-box;min-width:0;max-width:100%;
  color:#fff;font-size:10px;font-weight:600;white-space:nowrap}
.rl-bar-used-out{flex:0 0 auto;font-size:10px;color:var(--muted);min-width:2.5em}
.rl-bar-cap{flex:0 0 auto;font-size:10px;color:var(--muted);font-variant-numeric:tabular-nums;
  min-width:3.5em;text-align:right}

/* ── stat cards ── */
.stat-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.stat-card{background:var(--surface);border:1px solid var(--border);border-radius:10px;
  padding:14px 16px}
.stat-card .label{font-size:11px;color:var(--muted);margin-bottom:5px;text-transform:uppercase;
  letter-spacing:.5px}
.stat-card .value{font-size:22px;font-weight:700;color:var(--text)}
.stat-card .sub{font-size:11px;color:var(--muted);margin-top:3px}

/* ── simple overview ── */
.overview-grid{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(280px,.75fr);gap:14px}
@media(max-width:900px){.overview-grid{grid-template-columns:1fr}}
.hero{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:18px}
.hero-top{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}
.hero h2{font-size:20px;line-height:1.2;margin-bottom:6px}
.hero-copy{color:var(--muted);line-height:1.45;max-width:760px}
.hero-state{font-size:12px;padding:5px 10px;border-radius:999px;white-space:nowrap}
.hero-state.good{background:rgba(74,222,128,.12);color:var(--green)}
.hero-state.warn{background:rgba(250,204,21,.12);color:var(--yellow)}
.hero-state.bad{background:rgba(248,113,113,.12);color:var(--red)}
.quick-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin-top:16px}
.quick-card{background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:12px}
.quick-label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.4px;margin-bottom:5px}
.quick-value{font-size:15px;font-weight:700}
.quick-sub{font-size:11px;color:var(--muted);margin-top:3px;line-height:1.4}
.setup-card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px}
.setup-card h3{font-size:14px;margin-bottom:10px}
.setup-list{display:grid;gap:8px}
.setup-step{display:flex;align-items:center;gap:8px;color:var(--muted)}
.setup-step strong{color:var(--text);font-weight:600}
.step-dot{width:9px;height:9px;border-radius:50%;background:var(--border);flex:0 0 auto}
.setup-step.done .step-dot{background:var(--green);box-shadow:0 0 6px var(--green)}
.setup-step.warn .step-dot{background:var(--yellow);box-shadow:0 0 6px var(--yellow)}
.setup-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}
.provider-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:10px;padding:12px}
.provider-card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:12px}
.provider-card.bad{border-color:rgba(248,113,113,.45)}
.provider-card.warn{border-color:rgba(250,204,21,.45)}
.provider-head{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:5px}
.provider-name{font-weight:700}
.provider-model{font-size:11px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.provider-meta{display:flex;justify-content:space-between;gap:8px;margin-top:10px;font-size:11px;color:var(--muted)}
.advanced-panel summary{cursor:pointer;list-style:none}
.advanced-panel summary::-webkit-details-marker{display:none}
.advanced-panel .panel-header:after{content:"show";font-size:11px;color:var(--muted)}
.advanced-panel[open] .panel-header:after{content:"hide"}
.advanced-panel:not([open]) .panel-body{display:none}

/* ── panels ── */
.panel{background:var(--surface);border:1px solid var(--border);border-radius:10px;overflow:hidden}
.panel-header{display:flex;align-items:center;justify-content:space-between;
  padding:10px 14px;border-bottom:1px solid var(--border);background:var(--surface2)}
.panel-title{font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.6px;
  color:var(--muted)}
.panel-body{overflow-x:auto}
.panel-body.pad{padding:12px}

/* ── tables ── */
table{width:100%;border-collapse:collapse;font-size:12px}
th{padding:7px 12px;text-align:left;color:var(--muted);font-weight:500;
  font-size:11px;border-bottom:1px solid var(--border);white-space:nowrap}
td{padding:7px 12px;border-bottom:1px solid var(--border);vertical-align:middle;white-space:nowrap}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(108,140,255,.04)}
tr.rl-selected td{background:rgba(108,140,255,.10)}
tr.rl-row{cursor:pointer}
th.sortable{cursor:pointer;user-select:none}
th.sortable:hover{color:var(--text)}
th.sortable.sorted-asc::after,th.sortable.sorted-desc::after{
  content:'';margin-left:4px;font-size:10px;opacity:.85}
th.sortable.sorted-asc::after{content:'↑'}
th.sortable.sorted-desc::after{content:'↓'}

/* ── status dots ── */
.dot-ok{display:inline-block;width:8px;height:8px;border-radius:50%;
  background:var(--green);box-shadow:0 0 5px var(--green);margin-right:5px}
.dot-warn{display:inline-block;width:8px;height:8px;border-radius:50%;
  background:var(--yellow);box-shadow:0 0 5px var(--yellow);margin-right:5px}
.dot-err{display:inline-block;width:8px;height:8px;border-radius:50%;
  background:var(--red);box-shadow:0 0 5px var(--red);margin-right:5px}

/* ── pill badges ── */
.pill{display:inline-block;padding:2px 7px;border-radius:99px;font-size:10px;font-weight:600}
.pill-ok{background:rgba(74,222,128,.12);color:var(--green)}
.pill-err{background:rgba(248,113,113,.12);color:var(--red)}
.pill-cache{background:rgba(192,132,252,.12);color:var(--purple)}
.pill-warn{background:rgba(250,204,21,.12);color:var(--yellow)}
.pill-grey{background:rgba(136,146,164,.12);color:var(--muted)}

/* ── GI bar (0–100; higher = stronger) ── */
.gi-bar{display:inline-flex;align-items:center;gap:6px}
.gi-track{display:inline-block;width:48px;height:6px;border-radius:99px;background:var(--border);overflow:hidden;vertical-align:middle}
.gi-fill{display:block;height:100%;border-radius:99px;background:var(--accent)}
.gi-num{font-size:11px;font-variant-numeric:tabular-nums}
.gi-src{font-size:10px;color:var(--muted)}

/* ── progress bar ── */
.prog-track{background:var(--surface2);border-radius:99px;height:5px;min-width:80px;overflow:hidden}
.prog-fill{height:100%;border-radius:99px;background:var(--accent);transition:width .4s}
.prog-fill.green{background:var(--green)}
.prog-fill.red{background:var(--red)}
.prog-fill.yellow{background:var(--yellow)}

/* ── add-on toggles ── */
.addon-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:10px;padding:12px}
.addon-card{background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:12px}
.addon-card.flag{cursor:pointer;transition:border-color .15s}
.addon-card.flag:hover{border-color:var(--accent)}
.addon-card.busy{opacity:.5;pointer-events:none}
.addon-top{display:flex;align-items:center;justify-content:space-between;margin-bottom:4px}
.addon-name{font-size:12px;font-weight:600}
.addon-desc{font-size:11px;color:var(--muted);line-height:1.4}

/* ── provider scope picker ── */
.scope-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:6px;
  margin:6px 0}
.scope-item{display:flex;align-items:center;gap:6px;font-size:12px;padding:5px 8px;
  border:1px solid var(--border);border-radius:6px;background:var(--surface2);cursor:pointer}
.scope-item input{margin:0}
.scope-item .cnt{color:var(--muted);font-size:10px;margin-left:auto}

/* ── restart banner ── */
#restart-banner{display:none;align-items:center;justify-content:space-between;gap:12px;
  padding:10px 20px;background:rgba(250,204,21,.1);border-bottom:1px solid var(--yellow)}
#restart-banner.show{display:flex}
#restart-banner span{font-size:12px;color:var(--yellow)}
#restart-banner .actions{display:flex;gap:8px}

/* ── config forms ── */
.config-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;padding:14px}
@media(max-width:820px){.config-grid{grid-template-columns:1fr}}
.config-grid.narrow{grid-template-columns:1fr;max-width:440px}
.config-intro{padding:12px 14px 0;color:var(--muted);line-height:1.45}
.config-form{display:flex;flex-direction:column;gap:8px}
.config-form label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.4px}
.config-form select, .config-form input{
  background:var(--surface2);border:1px solid var(--border);border-radius:6px;
  color:var(--text);padding:7px 10px;font-size:12px;font-family:inherit;outline:none}
.config-form select:focus, .config-form input:focus{border-color:var(--accent)}
.config-form .row{display:flex;gap:8px}
.config-form .row > *{flex:1}
.config-msg{font-size:11px;min-height:16px}
.config-msg.ok{color:var(--green)}
.config-msg.err{color:var(--red)}
.default-hint{font-size:10px;color:var(--muted)}

/* ── log table ── */
#log-wrap{max-height:340px;overflow-y:auto}
.log-row-success td:first-child{border-left:2px solid var(--green)}
.log-row-error td:first-child{border-left:2px solid var(--red)}
.log-row-cache_hit td:first-child{border-left:2px solid var(--purple)}

/* ── two-col layout for lower panels ── */
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:820px){.two-col{grid-template-columns:1fr}}

/* ── misc ── */
.mono{font-family:monospace;font-size:11px}
.right{text-align:right}
.muted{color:var(--muted)}
</style>
</head>
<body>

<!-- API key gate -->
<div id="key-gate">
  <div class="gate-box">
    <h2>Hermes Router</h2>
    <p>Enter your proxy API key to view the dashboard.</p>
    <input id="key-input" type="password" placeholder="sk-router-..." autocomplete="off">
    <p id="gate-error" style="color:var(--red);font-size:12px;min-height:16px;margin:8px 0 0"></p>
    <button class="btn" onclick="submitKey()">Open Dashboard</button>
  </div>
</div>

<!-- Rate-limit group detail modal -->
<div id="rl-detail-modal" class="hidden" onclick="if(event.target===this)closeRateDetail()">
  <div class="rl-detail-box panel" role="dialog" aria-modal="true" aria-labelledby="rl-detail-title">
    <div class="panel-header">
      <span class="panel-title" id="rl-detail-title">Detail</span>
      <div class="rl-detail-actions">
        <button class="btn hidden" id="rl-detail-block-top" type="button">Block model</button>
        <button class="btn hidden" id="rl-detail-clear-top" type="button">Clear learned state</button>
        <button class="btn" onclick="closeRateDetail()">Close</button>
      </div>
    </div>
    <div class="panel-body">
      <div id="rl-detail-meta" class="muted" style="padding:8px 12px;font-size:12px"></div>
      <div id="rl-detail-gi" class="hidden" style="padding:8px 12px;border-top:1px solid var(--border);font-size:12px"></div>
      <div id="rl-detail-body"></div>
    </div>
  </div>
</div>

<!-- Cascade fail/skip detail modal -->
<div id="cascade-detail-modal" class="hidden" onclick="if(event.target===this)closeCascadeDetail()">
  <div class="rl-detail-box panel" role="dialog" aria-modal="true" aria-labelledby="cascade-detail-title">
    <div class="panel-header">
      <span class="panel-title" id="cascade-detail-title">Cascade</span>
      <button class="btn" onclick="closeCascadeDetail()">Close</button>
    </div>
    <div class="panel-body">
      <div id="cascade-detail-meta" class="muted" style="padding:8px 12px;font-size:12px"></div>
      <table>
        <thead><tr>
          <th>#</th><th>Provider</th><th>Model</th><th>Outcome</th><th>Reason</th>
        </tr></thead>
        <tbody id="cascade-detail-tbody"></tbody>
      </table>
    </div>
  </div>
</div>

<div class="app-shell">
  <aside class="sidebar">
    <div class="sidebar-brand"><span>Hermes</span> Router</div>
    <nav class="sidebar-nav" id="sidebar-nav">
      <button class="nav-item active" data-page="overview" onclick="showPage('overview')">Overview</button>
      <button class="nav-item" data-page="providers" onclick="showPage('providers')"><span>Providers</span><span class="nav-dot" id="nav-dot-providers"></span></button>
      <button class="nav-item" data-page="keys" onclick="showPage('keys')">Provider Keys</button>
      <button class="nav-item" data-page="access" onclick="showPage('access')">Access Keys</button>
      <button class="nav-item" data-page="models" onclick="showPage('models')">Models</button>
      <button class="nav-item" data-page="addons" onclick="showPage('addons')">Add-ons</button>
      <button class="nav-item" data-page="logs" onclick="showPage('logs')">Request Log</button>
    </nav>
  </aside>

  <div class="app-main">
    <header>
      <h1><span>Hermes</span> Router &mdash; Dashboard</h1>
      <div class="header-right">
        <div class="badge"><div class="dot" id="hdr-dot"></div><span id="hdr-status">connecting</span></div>
        <span id="last-update"></span>
        <button class="btn" onclick="refresh()">↺ Refresh</button>
      </div>
    </header>

    <div id="restart-banner">
      <span>⚠ Config changed — restart the router to apply it.</span>
      <div class="actions">
        <button class="btn" onclick="dismissBanner()">Later</button>
        <button class="btn" onclick="doRestart()" style="border-color:var(--yellow);color:var(--yellow)">↻ Restart Now</button>
      </div>
    </div>

    <main>

      <!-- ── Overview ─────────────────────────────────────────────────────── -->
      <section class="page active" id="page-overview">
        <section class="overview-grid">
          <div class="hero">
            <div class="hero-top">
              <div>
                <h2 id="plain-title">Checking router...</h2>
                <div class="hero-copy" id="plain-message">Loading status from Hermes Router.</div>
              </div>
              <div class="hero-state warn" id="plain-state">checking</div>
            </div>
            <div class="quick-grid">
              <div class="quick-card">
                <div class="quick-label">API endpoint</div>
                <div class="quick-value mono" id="quick-endpoint">/v1</div>
                <div class="quick-sub">Use this as the OpenAI base URL.</div>
              </div>
              <div class="quick-card">
                <div class="quick-label">Model name</div>
                <div class="quick-value mono" id="quick-model">hermes-router</div>
                <div class="quick-sub">Send this model from your app.</div>
              </div>
              <div class="quick-card">
                <div class="quick-label">Spend</div>
                <div class="quick-value" id="quick-spend">-</div>
                <div class="quick-sub">Estimated since last restart.</div>
              </div>
            </div>
          </div>
          <div class="setup-card">
            <h3>Setup checklist</h3>
            <div class="setup-list">
              <div class="setup-step" id="step-key"><span class="step-dot"></span><strong>Provider key</strong><span id="step-key-text">checking</span></div>
              <div class="setup-step" id="step-health"><span class="step-dot"></span><strong>Provider health</strong><span id="step-health-text">checking</span></div>
              <div class="setup-step" id="step-restart"><span class="step-dot"></span><strong>Restart</strong><span id="step-restart-text">not needed</span></div>
            </div>
            <div class="setup-actions">
              <button class="btn" onclick="showPage('keys')">Add key</button>
              <button class="btn" onclick="doRestart()">Restart</button>
              <button class="btn" onclick="refresh()">Refresh</button>
            </div>
          </div>
        </section>

        <div class="panel">
          <div class="panel-header"><span class="panel-title">Usage Summary</span></div>
          <div class="panel-body pad">
            <div class="stat-row" id="stat-row">
              <div class="stat-card"><div class="label">Providers</div><div class="value" id="s-providers">—</div><div class="sub" id="s-providers-sub"></div></div>
              <div class="stat-card"><div class="label">Uptime</div><div class="value" id="s-uptime">—</div><div class="sub">since last restart</div></div>
              <div class="stat-card"><div class="label">Total Requests</div><div class="value" id="s-requests">—</div><div class="sub" id="s-requests-sub"></div></div>
              <div class="stat-card"><div class="label">Total Tokens</div><div class="value" id="s-tokens">—</div><div class="sub" id="s-cost"></div></div>
              <div class="stat-card"><div class="label">Cache Hit Rate</div><div class="value" id="s-hitrate">—</div><div class="sub" id="s-cache-sub"></div></div>
              <div class="stat-card"><div class="label">Error Rate</div><div class="value" id="s-errrate">—</div><div class="sub" id="s-errrate-sub"></div></div>
            </div>
          </div>
        </div>
      </section>

      <!-- ── Providers ────────────────────────────────────────────────────── -->
      <section class="page" id="page-providers">
        <div class="panel">
          <div class="panel-header"><span class="panel-title">Provider Health</span><span class="muted">attention first</span></div>
          <div class="provider-grid" id="provider-card-grid"></div>
        </div>

        <details class="panel advanced-panel">
          <summary class="panel-header"><span class="panel-title">Advanced Provider Details</span></summary>
          <div class="panel-body">
            <table>
              <thead><tr>
                <th>Provider</th><th>Model</th><th>GI</th>
                <th class="right">Requests</th><th class="right">Errors</th>
                <th class="right">Err %</th><th class="right">Avg Latency</th>
                <th class="right">Tokens</th><th class="right">Cost (USD)</th>
                <th>Keys</th><th>Breaker</th><th>Status</th><th>Rate headroom</th>
              </tr></thead>
              <tbody id="provider-tbody"></tbody>
            </table>
          </div>
        </details>

        <div class="panel" id="rl-panel-pw">
          <div class="panel-header">
            <span class="panel-title">Provider-wide rate headroom</span>
            <label class="muted" style="font-size:12px;display:flex;align-items:center;gap:6px;font-weight:500;text-transform:none;letter-spacing:0">
              <input type="checkbox" id="rl-orphans-pw" onchange="refreshRateLimits()">
              Show dormant / orphan groups
            </label>
          </div>
          <div class="page-intro" style="padding:12px 14px 0">
            Shared-ceiling estimates (no header sync; softer cuts; faster recovery).
            Click a row for per-bucket detail. Clear drops learned caps for that scope.
          </div>
          <div class="panel-body">
            <table>
              <thead><tr>
                <th class="sortable" data-sort="provider" onclick="sortRateLimits('provider_wide','provider')">Provider</th>
                <th class="sortable" data-sort="key_hint" onclick="sortRateLimits('provider_wide','key_hint')">Key</th>
                <th class="sortable" data-sort="binding" onclick="sortRateLimits('provider_wide','binding')">Binding</th>
                <th class="sortable sorted-asc" data-sort="headroom" onclick="sortRateLimits('provider_wide','headroom')">Headroom</th>
                <th class="sortable right" data-sort="buckets" onclick="sortRateLimits('provider_wide','buckets')">Buckets</th>
              </tr></thead>
              <tbody id="rl-tbody-pw"></tbody>
            </table>
          </div>
        </div>
      </section>

      <!-- ── Provider Keys ────────────────────────────────────────────────── -->
      <section class="page" id="page-keys">
        <div class="panel">
          <div class="panel-header"><span class="panel-title">Provider Key Setup</span></div>
          <div class="page-intro" style="padding:12px 14px 0">Keys the router uses to call upstream providers (Gemini, OpenAI, …) — not the keys your own apps use to call the router (see Access Keys for that).</div>
          <div class="config-grid">
            <div class="config-form">
              <label>Add API key</label>
              <div class="row">
                <select id="cfg-key-provider"></select>
              </div>
              <input id="cfg-key-value" type="password" placeholder="paste provider API key" autocomplete="off">
              <div class="row">
                <button class="btn" onclick="addKey()">Add key</button>
              </div>
              <div class="config-msg" id="cfg-key-msg"></div>
            </div>

          </div>
        </div>

        <div class="panel">
          <div class="panel-header"><span class="panel-title">Key & Budget Usage</span></div>
          <div class="panel-body">
            <table>
              <thead><tr>
                <th>Key</th><th class="right">Requests</th>
                <th class="right">Tokens (day)</th><th class="right">Cost (day)</th>
                <th>RPM used</th>
              </tr></thead>
              <tbody id="keys-tbody"></tbody>
            </table>
          </div>
        </div>
      </section>

      <!-- ── Access Keys ──────────────────────────────────────────────────── -->
      <section class="page" id="page-access">
        <div class="panel">
          <div class="panel-header"><span class="panel-title">Create Access Key</span></div>
          <div class="page-intro" style="padding:12px 14px 0">Generate a key for a teammate or another app to call this router with — separate from your own key, with its own optional usage caps.</div>
          <div class="config-grid narrow">
            <div class="config-form">
              <label>Name (optional)</label>
              <input id="ak-name" type="text" placeholder="e.g. Bob, CI pipeline">
              <label style="margin-top:4px">Limits (optional — blank means unlimited)</label>
              <div class="row">
                <input id="ak-rpm" type="number" min="0" placeholder="requests / min">
                <input id="ak-reqday" type="number" min="0" placeholder="requests / day">
              </div>
              <div class="row">
                <input id="ak-tokday" type="number" min="0" placeholder="tokens / day">
                <input id="ak-costday" type="number" min="0" step="0.01" placeholder="cost / day ($)">
              </div>
              <label style="margin-top:4px">Providers this key may use (none checked = all)</label>
              <div class="scope-grid" id="ak-provider-scope"></div>
              <div class="row">
                <button class="btn" onclick="createAccessKey()">Create key</button>
              </div>
              <div class="config-msg" id="ak-create-msg"></div>
            </div>
          </div>
        </div>

        <div class="panel" id="new-key-panel" style="display:none">
          <div class="panel-header"><span class="panel-title">New Key — copy it now</span></div>
          <div class="panel-body pad">
            <p class="muted" style="margin-bottom:8px">This is the only time the full key is shown. It needs a router restart before it can be used.</p>
            <div class="config-form">
              <div class="row">
                <input id="new-key-value" type="text" readonly class="mono" style="flex:1">
                <button class="btn" onclick="copyNewKey()" style="flex:0 0 auto">Copy</button>
                <button class="btn" onclick="dismissNewKey()" style="flex:0 0 auto">Done</button>
              </div>
            </div>
          </div>
        </div>

        <div class="panel">
          <div class="panel-header"><span class="panel-title">Access Keys</span></div>
          <div class="panel-body">
            <table>
              <thead><tr>
                <th>Name</th><th>Key</th><th class="right">RPM</th><th class="right">Req/day</th>
                <th class="right">Tokens/day</th><th class="right">Cost/day</th>
                <th class="right">Used today</th><th>Providers</th><th>Status</th><th></th>
              </tr></thead>
              <tbody id="access-keys-tbody"></tbody>
            </table>
          </div>
        </div>
      </section>

      <!-- ── Models ───────────────────────────────────────────────────────── -->
      <section class="page" id="page-models">
        <div class="panel" id="rl-panel-model">
          <div class="panel-header">
            <span class="panel-title">Models</span>
            <label class="muted" style="font-size:12px;display:flex;align-items:center;gap:6px;font-weight:500;text-transform:none;letter-spacing:0">
              <input type="checkbox" id="rl-orphans-model" onchange="refreshRateLimits()">
              Show dormant / orphan groups
            </label>
          </div>
          <div class="page-intro" style="padding:12px 14px 0">
            Per-model general intelligence (GI) and authoritative rate headroom. Click a row for details.
          </div>
          <div class="panel-body">
            <table>
              <thead><tr>
                <th class="sortable" data-sort="model" onclick="sortRateLimits('model','model')">Model</th>
                <th class="sortable" data-sort="provider" onclick="sortRateLimits('model','provider')">Provider</th>
                <th>Key</th>
                <th class="sortable" data-sort="gi" onclick="sortRateLimits('model','gi')">GI</th>
                <th>Tools</th><th>Reasoning</th>
                <th class="sortable" data-sort="binding" onclick="sortRateLimits('model','binding')">Limiting factor</th>
                <th class="sortable sorted-asc" data-sort="headroom" onclick="sortRateLimits('model','headroom')">Headroom</th>
              </tr></thead>
              <tbody id="rl-tbody-model"></tbody>
            </table>
          </div>
        </div>

        <div class="panel" id="rl-panel-blocked">
          <div class="panel-header">
            <span class="panel-title">Blocked models</span>
          </div>
          <div class="page-intro" style="padding:12px 14px 0">
            Excluded via <span class="mono">{PROVIDER}_EXCLUDE_MODELS</span>. Unblock restores them to routing without a restart.
          </div>
          <div class="panel-body">
            <table>
              <thead><tr>
                <th>Model</th>
                <th>Provider</th>
                <th></th>
              </tr></thead>
              <tbody id="rl-tbody-blocked"></tbody>
            </table>
          </div>
        </div>
      </section>

      <!-- ── Add-ons ──────────────────────────────────────────────────────── -->
      <section class="page" id="page-addons">
        <div class="panel">
          <div class="panel-header"><span class="panel-title">Feature Add-ons</span></div>
          <div class="addon-grid" id="addon-grid"></div>
        </div>

        <div class="panel">
          <div class="panel-header"><span class="panel-title">Cache</span></div>
          <div class="panel-body">
            <table>
              <tbody id="cache-tbody"></tbody>
            </table>
          </div>
        </div>
      </section>

      <!-- ── Request Log ──────────────────────────────────────────────────── -->
      <section class="page" id="page-logs">
        <div class="panel">
          <div class="panel-header">
            <span class="panel-title">Live Request Log</span>
            <div style="display:flex;gap:8px;align-items:center">
              <select id="log-filter-status" style="background:var(--surface);color:var(--muted);border:1px solid var(--border);border-radius:5px;padding:2px 6px;font-size:11px">
                <option value="">All statuses</option>
                <option value="success">success</option>
                <option value="error">error</option>
                <option value="cache_hit">cache_hit</option>
              </select>
              <select id="log-filter-endpoint" style="background:var(--surface);color:var(--muted);border:1px solid var(--border);border-radius:5px;padding:2px 6px;font-size:11px">
                <option value="">All endpoints</option>
                <option value="chat">chat</option>
                <option value="messages">messages</option>
                <option value="embeddings">embeddings</option>
              </select>
            </div>
          </div>
          <div class="panel-body" id="log-wrap">
            <table>
              <thead><tr>
                <th>Time</th><th>Endpoint</th><th>Provider</th><th>Model</th>
                <th class="right">Latency</th><th class="right">Complexity</th>
                <th class="right">Fail / Skip</th><th class="right">Prompt tok</th>
                <th class="right">Compl tok</th><th>Status</th>
              </tr></thead>
              <tbody id="log-tbody"></tbody>
            </table>
          </div>
        </div>
      </section>

    </main>
  </div>
</div>

<script>
// ── state ──────────────────────────────────────────────────────────────────────
let apiKey = localStorage.getItem('hermes_dash_key') || '';
let statusData = null, usageData = null, logsData = [], accessKeysData = [];
let rateLimitsData = [];
let excludedModelsData = [];
let selectedRateGroupId = null;   // provider-wide detail
let selectedModelRow = null;      // {provider, model} for combined Models modal
let rlSortPw = {key: 'headroom', dir: 1};
let rlSortModel = {key: 'headroom', dir: 1};
let editingKeyTail = null;
let INTERVAL = 5000;
let timer = null;

// ── sidebar navigation ───────────────────────────────────────────────────────
const PAGES = ['overview', 'providers', 'keys', 'access', 'models', 'addons', 'logs'];

function showPage(name) {
  if (!PAGES.includes(name)) name = 'overview';
  document.querySelectorAll('.page').forEach(el => el.classList.toggle('active', el.id === 'page-' + name));
  document.querySelectorAll('.nav-item').forEach(el => el.classList.toggle('active', el.dataset.page === name));
  location.hash = name;
  window.scrollTo({top: 0});
}

// ── key gate ──────────────────────────────────────────────────────────────────
(function init() {
  const initial = (location.hash || '').replace('#', '');
  if (PAGES.includes(initial)) showPage(initial);
  window.addEventListener('hashchange', () => {
    const h = (location.hash || '').replace('#', '');
    if (PAGES.includes(h)) showPage(h);
  });
  if (apiKey) { document.getElementById('key-gate').classList.add('hidden'); start(); }
  document.getElementById('key-input').addEventListener('keydown', e => { if (e.key==='Enter') submitKey(); });
  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    const cascadeModal = document.getElementById('cascade-detail-modal');
    if (cascadeModal && !cascadeModal.classList.contains('hidden')) {
      closeCascadeDetail();
      return;
    }
    const modal = document.getElementById('rl-detail-modal');
    if (modal && !modal.classList.contains('hidden')) closeRateDetail();
  });
})();

function submitKey() {
  const v = document.getElementById('key-input').value.trim();
  if (!v) return;
  apiKey = v;
  localStorage.setItem('hermes_dash_key', v);
  const errEl = document.getElementById('gate-error');
  if (errEl) errEl.textContent = '';
  document.getElementById('key-gate').classList.add('hidden');
  start();
}

// ── polling ───────────────────────────────────────────────────────────────────
function start() { stop(); refresh(); loadConfigProviders(); timer = setInterval(refresh, INTERVAL); }

async function refresh() {
  try {
    const h = { 'Authorization': 'Bearer ' + apiKey };
    const logStatus  = document.getElementById('log-filter-status').value;
    const logEp      = document.getElementById('log-filter-endpoint').value;
    let logUrl = '/v1/logs?limit=100';
    if (logStatus) logUrl += '&status=' + logStatus;
    if (logEp)     logUrl += '&endpoint=' + logEp;

    const resps = await Promise.all([
      fetch('/v1/status', {headers:h}),
      fetch('/v1/usage',  {headers:h}),
      fetch(logUrl,       {headers:h}),
      fetch('/v1/config/proxy-keys', {headers:h}),
    ]);
    // fetch() only rejects on network errors, not on HTTP 4xx/5xx — so a bad key
    // (401) would otherwise parse to an error body and render as all-zeros. Detect
    // it explicitly and send the user back to the key gate instead of faking data.
    if (resps.some(r => r.status === 401)) {
      stop();
      apiKey = '';
      localStorage.removeItem('hermes_dash_key');
      showGate('That key was rejected (401). It must match one of PROXY_API_KEYS.');
      return;
    }
    if (resps.some(r => !r.ok)) { setHeader(false, 'HTTP ' + (resps.find(r=>!r.ok)||{}).status); return; }

    const [s, u, l, ak] = await Promise.all(resps.map(r => r.json()));
    statusData = s; usageData = u; logsData = l.entries || []; accessKeysData = ak.keys || [];
    renderAll();
    setHeader(true);
    await refreshRateLimits();
    await refreshExcludedModels();
  } catch(e) {
    setHeader(false, 'unreachable');
  }
  document.getElementById('log-filter-status').onchange  = refresh;
  document.getElementById('log-filter-endpoint').onchange = refresh;
}

function stop() { if (timer) { clearInterval(timer); timer = null; } }

function showGate(errMsg) {
  stop();
  const gate = document.getElementById('key-gate');
  gate.classList.remove('hidden');
  const errEl = document.getElementById('gate-error');
  if (errEl) errEl.textContent = errMsg || '';
  const input = document.getElementById('key-input');
  input.value = '';
  input.focus();
}

function setHeader(ok, detail) {
  const dot = document.getElementById('hdr-dot');
  const lbl = document.getElementById('hdr-status');
  dot.className = ok ? 'dot' : 'dot err';
  lbl.textContent = ok ? 'live' : ('error' + (detail ? ' · ' + detail : ''));
  document.getElementById('last-update').textContent =
    'Updated ' + new Date().toLocaleTimeString();
}

// ── helpers ───────────────────────────────────────────────────────────────────
function esc(s){ return String(s==null?'':s).replace(/[&<>]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
function attr(s){ return String(s==null?'':s).replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
const fmt = {
  num:  n => n == null ? '—' : Number(n).toLocaleString(),
  tok:  n => { if (n==null||n===0) return '0'; if (n>=1e9) return (n/1e9).toFixed(1)+'B'; if (n>=1e6) return (n/1e6).toFixed(1)+'M'; if (n>=1e3) return (n/1e3).toFixed(1)+'K'; return String(n); },
  pct:  n => n == null ? '—' : n.toFixed(1) + '%',
  ms:   n => n == null ? '—' : (n >= 1000 ? (n/1000).toFixed(1)+'s' : Math.round(n)+'ms'),
  usd:  n => n == null ? '—' : (n < 0.0001 ? '<$0.0001' : '$' + n.toFixed(4)),
  uptime: s => { if (!s) return '—'; const h=Math.floor(s/3600),m=Math.floor((s%3600)/60); return (h?h+'h ':'') + m+'m'; },
  time: ts => { if (!ts) return '—'; try { return new Date(ts).toLocaleTimeString(); } catch { return ts; } },
};

function el(tag, cls, html) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html != null) e.innerHTML = html;
  return e;
}

function ratingPips(r) {
  return giBadge(r, null);
}

function giBadge(gi, src) {
  if (gi == null || gi === '' || !isFinite(Number(gi))) return '<span class="muted">—</span>';
  const pct = Math.max(0, Math.min(100, Number(gi)));
  const srcLabel = src ? ` <span class="gi-src">(${esc(src)})</span>` : '';
  return `<span class="gi-bar" title="General intelligence ${pct.toFixed(1)}/100${src ? ' · ' + src : ''}">
    <span class="gi-track"><span class="gi-fill" style="width:${pct}%"></span></span>
    <span class="gi-num">${pct.toFixed(0)}</span>${srcLabel}
  </span>`;
}

function statusPill(s, breaker) {
  if (breaker) return '<span class="pill pill-err">⨂ tripped</span>';
  if (s && s.total_requests === 0) return '<span class="pill pill-grey">idle</span>';
  const erp = s ? (s.errors / (s.total_requests||1) * 100) : 0;
  if (erp > 20) return '<span class="pill pill-err">degraded</span>';
  if (erp > 5)  return '<span class="pill pill-warn">unstable</span>';
  return '<span class="pill pill-ok">healthy</span>';
}

function keyDots(keys) {
  if (!keys || !keys.length) return '<span class="muted">—</span>';
  return keys.map(k => {
    const cls = k.status === 'cooling' ? 'dot-warn' : 'dot-ok';
    const req = k.requests != null ? `${k.requests} req` : '';
    const title = (k.status === 'cooling' ? `cooling (${k.ready_in}s)` : 'ready') + (req ? ` · ${req}` : '');
    return `<span class="${cls}" title="${title}" style="display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:2px"></span>`;
  }).join('');
}

// ── render all ────────────────────────────────────────────────────────────────
function renderAll() {
  renderPlainOverview();
  renderNavHealth();
  renderStats();
  renderProviderCards();
  renderProviders();
  renderLogs();
  renderCache();
  renderAddons();
  renderKeys();
  renderAccessKeys();
  renderCombinedModels();
  renderBlockedModels();
}

function renderNavHealth() {
  const dot = document.getElementById('nav-dot-providers');
  if (!dot || !statusData) return;
  const vals = Object.values(statusData.providers || {});
  const openBreakers = vals.filter(p => p.breaker?.open).length;
  const totalReq = vals.reduce((a,p) => a + (p.stats?.total_requests || 0), 0);
  const totalErr = vals.reduce((a,p) => a + (p.stats?.errors || 0), 0);
  const errRate = totalReq ? totalErr / totalReq * 100 : 0;
  dot.className = 'nav-dot' + (openBreakers ? ' bad' : errRate > 5 ? ' warn' : '');
}

function renderPlainOverview() {
  if (!statusData || !usageData) return;
  const prov = statusData.providers || {};
  const vals = Object.values(prov);
  const keyCount = vals.reduce((a,p) => a + ((p.keys || []).length), 0);
  const readyKeys = vals.reduce((a,p) => a + ((p.keys || []).filter(k => k.status === 'ready').length), 0);
  const openBreakers = vals.filter(p => p.breaker?.open).length;
  const active = vals.filter(p => (p.stats?.total_requests || 0) > 0).length;
  const totalReq = vals.reduce((a,p) => a + (p.stats?.total_requests || 0), 0);
  const totalErr = vals.reduce((a,p) => a + (p.stats?.errors || 0), 0);
  const errRate = totalReq ? totalErr / totalReq * 100 : 0;

  const state = document.getElementById('plain-state');
  const title = document.getElementById('plain-title');
  const msg = document.getElementById('plain-message');
  state.className = 'hero-state';
  if (!keyCount) {
    state.classList.add('bad');
    state.textContent = 'needs a key';
    title.textContent = 'Add one provider key to start';
    msg.textContent = 'Choose a provider below, paste its API key, then restart hermes-router.';
  } else if (openBreakers || errRate > 25) {
    state.classList.add('warn');
    state.textContent = 'needs attention';
    title.textContent = 'hermes-router is running, but some providers need attention';
    msg.textContent = 'Requests can still fall back to healthy providers. Add more keys or check providers with high errors.';
  } else {
    state.classList.add('good');
    state.textContent = 'ready';
    title.textContent = 'hermes-router is ready';
    msg.textContent = active ? 'Traffic is flowing through your providers.' : 'No requests yet. Point your client at the endpoint below.';
  }

  document.getElementById('quick-endpoint').textContent = location.origin + '/v1';
  document.getElementById('quick-model').textContent = 'hermes-router';
  document.getElementById('quick-spend').textContent = fmt.usd(usageData.totals?.cost?.usd);
  setStep('step-key', keyCount > 0, `${readyKeys}/${keyCount} keys ready`);
  setStep('step-health', !openBreakers && errRate <= 25, openBreakers ? `${openBreakers} breaker open` : (errRate ? `${fmt.pct(errRate)} errors` : 'ok'));
}

function setStep(id, done, text) {
  const el = document.getElementById(id);
  el.className = 'setup-step ' + (done ? 'done' : 'warn');
  const label = document.getElementById(id + '-text');
  if (label) label.textContent = text;
}

function renderProviderCards() {
  if (!statusData) return;
  const prov = statusData.providers || {};
  const grid = document.getElementById('provider-card-grid');
  const entries = Object.entries(prov).sort(([an,a],[bn,b]) => {
    const badA = (a.breaker?.open ? 2 : 0) + ((a.stats?.errors || 0) > 0 ? 1 : 0);
    const badB = (b.breaker?.open ? 2 : 0) + ((b.stats?.errors || 0) > 0 ? 1 : 0);
    return badB - badA || an.localeCompare(bn);
  }).slice(0, 8);
  if (!entries.length) {
    grid.innerHTML = '<div class="provider-card bad"><div class="provider-name">No providers configured</div><div class="provider-model">Add an API key below.</div></div>';
    return;
  }
  grid.innerHTML = entries.map(([name,p]) => {
    const req = p.stats?.total_requests || 0;
    const err = p.stats?.errors || 0;
    const erp = req ? err / req * 100 : 0;
    const ready = (p.keys || []).filter(k => k.status === 'ready').length;
    const brk = p.breaker?.open;
    const cls = brk || erp > 25 ? 'bad' : erp > 5 ? 'warn' : '';
    const pill = brk ? '<span class="pill pill-err">paused</span>' :
      erp > 25 ? '<span class="pill pill-err">check</span>' :
      erp > 5 ? '<span class="pill pill-warn">watch</span>' :
      '<span class="pill pill-ok">ready</span>';
    return `<div class="provider-card ${cls}">
      <div class="provider-head"><span class="provider-name">${name}</span>${pill}</div>
      <div class="provider-model" title="${p.model || ''}">${p.model || 'no model'}</div>
      <div class="provider-meta"><span>${ready} ready key${ready===1?'':'s'}</span><span>${fmt.ms(p.stats?.avg_latency_ms)}</span></div>
    </div>`;
  }).join('');
}

// ── stat cards ────────────────────────────────────────────────────────────────
function renderStats() {
  if (!statusData || !usageData) return;
  const prov   = statusData.providers || {};
  const nProv  = Object.keys(prov).length;
  const broken = Object.values(prov).filter(p => p.breaker && p.breaker.open).length;
  document.getElementById('s-providers').textContent = nProv;
  document.getElementById('s-providers-sub').textContent =
    broken ? `${broken} breaker(s) tripped` : 'all healthy';

  document.getElementById('s-uptime').textContent = fmt.uptime(usageData.uptime_s);

  const totReq = Object.values(prov).reduce((a,p) => a + (p.stats?.total_requests||0), 0);
  const totErr = Object.values(prov).reduce((a,p) => a + (p.stats?.errors||0), 0);
  document.getElementById('s-requests').textContent = fmt.num(totReq);
  document.getElementById('s-requests-sub').textContent =
    totReq ? fmt.pct(totErr/totReq*100) + ' error rate' : '';

  const tot = usageData.totals || {};
  document.getElementById('s-tokens').textContent = fmt.tok(tot.tokens);
  document.getElementById('s-cost').textContent   = 'est. ' + fmt.usd(tot.cost?.usd);

  const cache = statusData.cache || {};
  document.getElementById('s-hitrate').textContent = fmt.pct((cache.hit_rate||0)*100);
  document.getElementById('s-cache-sub').textContent =
    `${fmt.num(cache.hits)} hits / ${fmt.num(cache.misses)} misses`;

  const errRate = totReq ? (totErr / totReq * 100) : 0;
  const errEl = document.getElementById('s-errrate');
  errEl.textContent = fmt.pct(errRate);
  errEl.style.color = errRate > 10 ? 'var(--red)' : errRate > 3 ? 'var(--yellow)' : 'var(--green)';
  document.getElementById('s-errrate-sub').textContent = fmt.num(totErr) + ' total errors';
}

// ── provider table ────────────────────────────────────────────────────────────
function renderProviders() {
  if (!statusData) return;
  const prov = statusData.providers || {};
  const tbody = document.getElementById('provider-tbody');
  tbody.innerHTML = '';
  Object.entries(prov).forEach(([name, p]) => {
    const s   = p.stats || {};
    const req = s.total_requests || 0;
    const err = s.errors || 0;
    const erp = req ? err / req * 100 : 0;
    const lat = s.avg_latency_ms;
    const brk = p.breaker?.open || false;
    const model = p.model || '';
    // Rate headroom bar
    const rlData = (p.rate_limits || {})[model] || {};
    const allBuckets = {...(rlData.provider_wide || {}), ...(rlData.model || {})};
    const bucketValues = Object.values(allBuckets);
    const headroom = bucketValues.length
        ? Math.min(...bucketValues.map(b => b.headroom))
        : 1.0;
    const hPct = Math.round(headroom * 100);
    const hColor = hPct >= 50 ? 'green' : hPct >= 20 ? 'yellow' : 'red';
    const hTitle = Object.entries(allBuckets)
        .map(([k, v]) => `${k}: ${v.used}/${v.cap}`)
        .join(' | ') || 'no rate data';
    const headroomBar = `<div title="${hTitle}" style="display:inline-block;vertical-align:middle;margin-left:4px">
  <div class="prog-track" style="width:48px;display:inline-block">
    <div class="prog-fill ${hColor}" style="width:${hPct}%"></div>
  </div>
  <span class="muted" style="font-size:10px">${hPct}%</span>
</div>`;
    const tr  = document.createElement('tr');
    tr.innerHTML = `
      <td><strong>${name}</strong></td>
      <td class="muted mono" style="max-width:160px;overflow:hidden;text-overflow:ellipsis" title="${p.model||''}">${p.model||'—'}</td>
      <td>${giBadge(p.gi, p.gi_source)}</td>
      <td class="right">${fmt.num(req)}</td>
      <td class="right ${err>0?'':'muted'}">${fmt.num(err)}</td>
      <td class="right" style="color:${erp>10?'var(--red)':erp>3?'var(--yellow)':'var(--muted)'}">${req?fmt.pct(erp):'—'}</td>
      <td class="right">${fmt.ms(lat)}</td>
      <td class="right muted">${fmt.tok(p.tokens)}</td>
      <td class="right muted">${fmt.usd(p.cost_usd)}</td>
      <td>${keyDots(p.keys)}</td>
      <td>${brk?'<span class="pill pill-err">open</span>':'<span class="pill pill-ok">closed</span>'}</td>
      <td>${statusPill(s, brk)}</td>
      <td>${headroomBar}</td>
    `;
    tbody.appendChild(tr);
  });
}

// ── live log ──────────────────────────────────────────────────────────────────
const CASCADE_REASON_LABELS = {
  rate_headroom: 'Rate headroom exhausted',
  rate_hold: 'Rate limit hold (Retry-After)',
  token_cap: 'Input over token cap',
  no_tools: 'No tool support',
  no_vision: 'No vision support',
  circuit_open: 'Circuit breaker open',
  access_scope: 'Outside access-key provider scope',
  keys_cooling: 'All keys cooling',
  unsuitable_cooling: 'Unsuitable model cooldown',
  network: 'Network / timeout',
  ttft_deadline: 'TTFT deadline exceeded',
  empty_response: 'Empty / unusable response',
  http_429: 'HTTP 429',
  http_401: 'HTTP 401',
  http_403: 'HTTP 403',
  http_400: 'HTTP 400',
  http_404: 'HTTP 404',
  http_413: 'HTTP 413',
  http_5xx: 'HTTP 5xx',
};
function cascadeReasonLabel(code) {
  if (code == null || code === '') return '—';
  return CASCADE_REASON_LABELS[code] || code;
}

function openCascadeDetail(idx) {
  const e = logsData[idx];
  if (!e || !Array.isArray(e.cascade) || !e.cascade.length) return;
  document.getElementById('cascade-detail-title').textContent =
    `Cascade · ${e.endpoint || '—'} · ${e.status || '—'}`;
  document.getElementById('cascade-detail-meta').textContent =
    `${fmt.time(e.ts)} · fail ${e.failed||0} / skip ${e.skipped||0}`;
  const outcomePill = (o) => {
    const cls = o === 'success' ? 'pill-ok' : o === 'failed' ? 'pill-err' : 'pill-warn';
    return `<span class="pill ${cls}">${o}</span>`;
  };
  document.getElementById('cascade-detail-tbody').innerHTML = e.cascade.map((s, i) => `
    <tr>
      <td class="muted">${i+1}</td>
      <td><strong>${s.provider||'—'}</strong></td>
      <td class="mono muted">${s.model||'—'}</td>
      <td>${outcomePill(s.outcome)}</td>
      <td class="muted">${s.outcome==='success'?'—':cascadeReasonLabel(s.reason)}</td>
    </tr>`).join('');
  document.getElementById('cascade-detail-modal').classList.remove('hidden');
}
function closeCascadeDetail() {
  document.getElementById('cascade-detail-modal').classList.add('hidden');
}

function renderLogs() {
  const tbody = document.getElementById('log-tbody');
  if (!logsData.length) {
    tbody.innerHTML = '<tr><td colspan="10" style="text-align:center;color:var(--muted);padding:24px">No requests logged yet</td></tr>';
    return;
  }
  tbody.innerHTML = '';
  logsData.forEach((e, idx) => {
    const tr = document.createElement('tr');
    tr.className = 'log-row-' + (e.status||'');
    const sp = e.status === 'success' ? 'pill-ok' : e.status === 'error' ? 'pill-err' : 'pill-cache';
    const hasTrail = Array.isArray(e.cascade) && e.cascade.length > 0;
    const failed = e.failed != null ? e.failed : null;
    const skipped = e.skipped != null ? e.skipped : null;
    let cascCell;
    if (failed != null && skipped != null) {
      const nums = `<span class="${(failed+skipped)>0?'':'muted'}">${failed} / ${skipped}</span>`;
      cascCell = hasTrail
        ? `<span style="cursor:pointer;text-decoration:underline dotted" title="Show cascade path">${nums}</span>`
        : nums;
    } else {
      cascCell = e.cascades > 0
        ? `<span class="pill pill-warn">${e.cascades}</span>`
        : '<span class="muted">0</span>';
    }
    const cmpx = e.complexity;
    const cmpxColor = !cmpx ? 'var(--muted)' : cmpx>=4?'var(--red)':cmpx<=2?'var(--green)':'var(--yellow)';
    const cmpxTitle = cmpx ? `Complexity ${cmpx}/5 (${({1:'trivial',2:'simple',3:'standard',4:'complex',5:'critical'})[cmpx]||''})` : '';
    tr.innerHTML = `
      <td class="mono muted">${fmt.time(e.ts)}</td>
      <td>${e.endpoint||'—'}</td>
      <td><strong>${e.provider||'—'}</strong></td>
      <td class="muted mono" style="max-width:140px;overflow:hidden;text-overflow:ellipsis" title="${e.model||''}">${e.model||'—'}</td>
      <td class="right">${fmt.ms(e.latency_ms)}</td>
      <td class="right" style="color:${cmpxColor}" title="${cmpxTitle}">${cmpx||'—'}</td>
      <td class="right" ${hasTrail ? `style="cursor:pointer" data-cascade-idx="${idx}"` : ''}>${cascCell}</td>
      <td class="right muted">${e.prompt_tokens!=null?fmt.num(e.prompt_tokens):'—'}</td>
      <td class="right muted">${e.completion_tokens!=null?fmt.num(e.completion_tokens):'—'}</td>
      <td><span class="pill ${sp}">${e.status||'—'}</span></td>
    `;
    if (hasTrail) {
      tr.querySelector('[data-cascade-idx]').addEventListener('click', () => openCascadeDetail(idx));
    }
    tbody.appendChild(tr);
  });
}

// ── cache panel ───────────────────────────────────────────────────────────────
function renderCache() {
  if (!statusData) return;
  const c = statusData.cache || {};
  const sem = c.semantic || {};
  const rows = [
    ['Enabled',       c.enabled ? '<span class="pill pill-ok">yes</span>' : '<span class="pill pill-grey">no</span>'],
    ['TTL',           c.ttl_s != null ? c.ttl_s + 's' : '—'],
    ['Size',          `${fmt.num(c.size)} / ${fmt.num(c.max_size)}`],
    ['Hits',          fmt.num(c.hits)],
    ['Misses',        fmt.num(c.misses)],
    ['Hit rate',      fmt.pct((c.hit_rate||0)*100)],
    ['Semantic cache',sem.enabled ? '<span class="pill pill-ok">yes</span>' : '<span class="pill pill-grey">no</span>'],
    ['Semantic hits', fmt.num(sem.hits)],
    ['Sem. threshold',sem.threshold != null ? sem.threshold : '—'],
  ];
  const tbody = document.getElementById('cache-tbody');
  tbody.innerHTML = rows.map(([k,v]) =>
    `<tr><td class="muted" style="width:50%">${k}</td><td>${v}</td></tr>`
  ).join('');
}

// ── add-ons panel ─────────────────────────────────────────────────────────────
function renderAddons() {
  if (!statusData?.features) return;
  const addons = statusData.features.addons || [];
  const grid = document.getElementById('addon-grid');
  grid.innerHTML = addons.map(a => {
    const clickable = a.kind === 'flag';
    const attrs = clickable ? `onclick="toggleAddon('${a.name}', ${!a.enabled})" title="Click to ${a.enabled?'disable':'enable'}"` : '';
    return `
    <div class="addon-card ${clickable?'flag':''}" id="addon-${a.name}" ${attrs}>
      <div class="addon-top">
        <span class="addon-name">${a.title || a.name}</span>
        <span class="pill ${a.enabled ? 'pill-ok' : 'pill-grey'}">${a.enabled ? 'on' : 'off'}</span>
      </div>
      <div class="addon-desc">${a.desc || ''}</div>
      ${a.env ? `<div class="mono muted" style="margin-top:5px;font-size:10px">${a.env}</div>` : ''}
    </div>
  `;
  }).join('');
}

// ── config: add-ons toggle ───────────────────────────────────────────────────
async function toggleAddon(name, enable) {
  const card = document.getElementById('addon-' + name);
  if (card) card.classList.add('busy');
  try {
    const r = await fetch('/v1/config/features/' + name, {
      method: 'POST',
      headers: {'Authorization':'Bearer '+apiKey, 'Content-Type':'application/json'},
      body: JSON.stringify({enabled: enable}),
    });
    const d = await r.json();
    if (!r.ok) { alert(d.error?.message || 'Failed to toggle ' + name); return; }
    showRestartBanner();
    await refresh();
  } catch(e) {
    alert('Network error: ' + e.message);
  } finally {
    if (card) card.classList.remove('busy');
  }
}

// ── config: providers / add key / model ──────────────────────────────────────
let configProviders = null;

async function loadConfigProviders() {
  try {
    const r = await fetch('/v1/config/providers', {headers:{'Authorization':'Bearer '+apiKey}});
    if (!r.ok) return;
    configProviders = await r.json();
    const keySel = document.getElementById('cfg-key-provider');
    keySel.innerHTML = configProviders.key_settable.map(p => `<option value="${p}">${p}</option>`).join('');
    renderProviderScopePicker();
    renderAccessKeys();   // re-render now that provider names/counts are known
  } catch(e) { /* dashboard still usable without this */ }
}

// Providers a caller might sensibly scope an access key to — anything with a
// live key count, or already model-settable (covers keyless "local"). Sorted
// with the most-provisioned providers first, since those are the likely picks.
function scopeableProviders() {
  if (!configProviders) return [];
  const counts = configProviders.key_counts || {};
  const names = new Set([...Object.keys(counts), ...configProviders.model_settable]);
  return [...names].sort((a,b) => (counts[b]||0) - (counts[a]||0) || a.localeCompare(b));
}

function renderProviderScopePicker() {
  const grid = document.getElementById('ak-provider-scope');
  if (!grid) return;
  grid.innerHTML = renderScopeCheckboxes([]);
}

function setMsg(id, text, ok) {
  const el = document.getElementById(id);
  el.textContent = text;
  el.className = 'config-msg ' + (ok ? 'ok' : 'err');
}

async function addKey() {
  const provider = document.getElementById('cfg-key-provider').value;
  const key = document.getElementById('cfg-key-value').value.trim();
  if (!key) { setMsg('cfg-key-msg', 'Enter a key first.', false); return; }
  try {
    const r = await fetch('/v1/config/keys/' + provider, {
      method: 'POST',
      headers: {'Authorization':'Bearer '+apiKey, 'Content-Type':'application/json'},
      body: JSON.stringify({key}),
    });
    const d = await r.json();
    if (!r.ok) { setMsg('cfg-key-msg', d.error?.message || 'Failed.', false); return; }
    if (d.duplicate) { setMsg('cfg-key-msg', 'Already stored — no change.', false); return; }
    document.getElementById('cfg-key-value').value = '';
    setMsg('cfg-key-msg', `Saved — ${provider} now has ${d.total_keys} key(s).`, true);
    showRestartBanner();
  } catch(e) { setMsg('cfg-key-msg', 'Network error: ' + e.message, false); }
}

// ── restart ───────────────────────────────────────────────────────────────────
function showRestartBanner() {
  document.getElementById('restart-banner').classList.add('show');
  setStep('step-restart', false, 'needed');
}
function dismissBanner() { document.getElementById('restart-banner').classList.remove('show'); }

async function doRestart() {
  if (!confirm('Restart the router now? It will be unreachable for a few seconds.')) return;
  try {
    await fetch('/v1/config/restart', {method:'POST', headers:{'Authorization':'Bearer '+apiKey}});
  } catch(e) { /* the process may already be going down mid-response — expected */ }
  dismissBanner();
  setStep('step-restart', true, 'restarting');
  setHeader(false, 'restarting…');
  stop();
  // Poll /health until it responds again, then resume normal operation.
  const waitForRestart = setInterval(async () => {
    try {
      const r = await fetch('/health');
      if (r.ok) { clearInterval(waitForRestart); start(); }
    } catch(e) { /* still down — keep polling */ }
  }, 1500);
}

// ── key usage ─────────────────────────────────────────────────────────────────
function renderKeys() {
  if (!usageData) return;
  const keys = usageData.keys || [];
  const limData = statusData?.limits || {};
  const tbody = document.getElementById('keys-tbody');
  if (!keys.length) {
    tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--muted);padding:18px">No key data</td></tr>';
    return;
  }
  tbody.innerHTML = keys.map(k => {
    const limEntry = (limData.keys || []).find(l => l.key_tail === k.key_tail);
    const lim = limEntry?.limits || {};
    const rpmUsed = k.rpm_current || 0;
    const rpmMax  = lim.rpm || 0;
    const rpmPct  = rpmMax ? Math.min(rpmUsed / rpmMax * 100, 100) : 0;
    const rpmColor = rpmPct > 80 ? 'red' : rpmPct > 50 ? 'yellow' : 'green';
    return `<tr>
      <td class="mono">...${k.key_tail}</td>
      <td class="right">${fmt.num(k.req_total)}</td>
      <td class="right">
        ${fmt.tok(k.tokens_today)}
        ${lim.tokens_day ? `<br><span class="muted" style="font-size:10px">/ ${fmt.tok(lim.tokens_day)}</span>` : ''}
      </td>
      <td class="right">
        ${fmt.usd(k.cost_today)}
        ${lim.cost_day ? `<br><span class="muted" style="font-size:10px">/ ${fmt.usd(lim.cost_day)}</span>` : ''}
      </td>
      <td style="min-width:100px">
        ${rpmMax
          ? `<div class="prog-track"><div class="prog-fill ${rpmColor}" style="width:${rpmPct}%"></div></div>
             <span class="muted" style="font-size:10px">${rpmUsed}/${rpmMax} rpm</span>`
          : '<span class="muted">unlimited</span>'}
      </td>
    </tr>`;
  }).join('');
}

// ── access keys (clients use these to call the proxy) ────────────────────────
function renderAccessKeys() {
  const tbody = document.getElementById('access-keys-tbody');
  if (!tbody) return;
  if (!accessKeysData.length) {
    tbody.innerHTML = '<tr><td colspan="10" style="text-align:center;color:var(--muted);padding:18px">No access keys yet</td></tr>';
    return;
  }
  tbody.innerHTML = accessKeysData.map(k => {
    const tail = k.key_tail;
    const lim = k.limits || {};
    const used = k.usage || {};
    const allowed = k.allowed_providers || [];
    const statusPill = k.pending_restart
      ? '<span class="pill pill-warn">pending restart</span>'
      : '<span class="pill pill-ok">active</span>';

    if (editingKeyTail === tail) {
      return `<tr>
        <td><input id="edit-name-${tail}" type="text" value="${attr(k.name||'')}" style="width:110px;background:var(--surface2);border:1px solid var(--border);border-radius:4px;color:var(--text);padding:4px 6px;font-size:11px"></td>
        <td class="mono muted">...${esc(tail)}</td>
        <td class="right"><input id="edit-rpm-${tail}" type="number" min="0" value="${lim.rpm||''}" placeholder="∞" style="width:55px;text-align:right;background:var(--surface2);border:1px solid var(--border);border-radius:4px;color:var(--text);padding:4px 6px;font-size:11px"></td>
        <td class="right"><input id="edit-reqday-${tail}" type="number" min="0" value="${lim.req_per_day||''}" placeholder="∞" style="width:65px;text-align:right;background:var(--surface2);border:1px solid var(--border);border-radius:4px;color:var(--text);padding:4px 6px;font-size:11px"></td>
        <td class="right"><input id="edit-tokday-${tail}" type="number" min="0" value="${lim.tokens_per_day||''}" placeholder="∞" style="width:75px;text-align:right;background:var(--surface2);border:1px solid var(--border);border-radius:4px;color:var(--text);padding:4px 6px;font-size:11px"></td>
        <td class="right"><input id="edit-costday-${tail}" type="number" min="0" step="0.01" value="${lim.cost_per_day||''}" placeholder="∞" style="width:65px;text-align:right;background:var(--surface2);border:1px solid var(--border);border-radius:4px;color:var(--text);padding:4px 6px;font-size:11px"></td>
        <td class="right muted">${fmt.num(used.req_today)}</td>
        <td><div class="scope-grid" id="edit-scope-${tail}" style="min-width:180px">${renderScopeCheckboxes(allowed)}</div></td>
        <td>${statusPill}</td>
        <td style="white-space:nowrap"><button class="btn" onclick="saveEditAccessKey('${tail}')">Save</button> <button class="btn" onclick="cancelEditAccessKey()">Cancel</button></td>
      </tr>`;
    }
    return `<tr>
      <td>${esc(k.name || '—')}</td>
      <td class="mono muted">...${esc(tail)}</td>
      <td class="right">${lim.rpm || '∞'}</td>
      <td class="right">${lim.req_per_day || '∞'}</td>
      <td class="right">${lim.tokens_per_day ? fmt.tok(lim.tokens_per_day) : '∞'}</td>
      <td class="right">${lim.cost_per_day ? fmt.usd(lim.cost_per_day) : '∞'}</td>
      <td class="right muted">${fmt.num(used.req_today)}</td>
      <td class="muted">${allowed.length ? esc(allowed.join(', ')) : 'all'}</td>
      <td>${statusPill}</td>
      <td style="white-space:nowrap"><button class="btn" onclick="startEditAccessKey('${tail}')">Edit</button> <button class="btn" onclick="revokeAccessKey('${tail}')">Revoke</button></td>
    </tr>`;
  }).join('');
}

function renderScopeCheckboxes(selected) {
  const sel = new Set(selected || []);
  return scopeableProviders().map(name => {
    const n = (configProviders && configProviders.key_counts || {})[name];
    const cnt = n != null ? `<span class="cnt">${n} key${n===1?'':'s'}</span>` : '';
    const checked = sel.has(name) ? ' checked' : '';
    return `<label class="scope-item"><input type="checkbox" value="${attr(name)}"${checked}> ${esc(name)}${cnt}</label>`;
  }).join('');
}

function startEditAccessKey(tail) { editingKeyTail = tail; renderAccessKeys(); }
function cancelEditAccessKey() { editingKeyTail = null; renderAccessKeys(); }

async function saveEditAccessKey(tail) {
  const scopeEl = document.getElementById(`edit-scope-${tail}`);
  const allowedProviders = scopeEl ? [...scopeEl.querySelectorAll('input:checked')].map(el => el.value) : [];
  const body = {
    name: document.getElementById(`edit-name-${tail}`).value,
    rpm: document.getElementById(`edit-rpm-${tail}`).value,
    req_per_day: document.getElementById(`edit-reqday-${tail}`).value,
    tokens_per_day: document.getElementById(`edit-tokday-${tail}`).value,
    cost_per_day: document.getElementById(`edit-costday-${tail}`).value,
    allowed_providers: allowedProviders,
  };
  try {
    const r = await fetch('/v1/config/proxy-keys/' + tail, {
      method: 'POST',
      headers: {'Authorization':'Bearer '+apiKey, 'Content-Type':'application/json'},
      body: JSON.stringify(body),
    });
    const d = await r.json();
    if (!r.ok) { alert(d.error?.message || 'Failed to save.'); return; }
    editingKeyTail = null;
    showRestartBanner();
    await refresh();
  } catch(e) { alert('Network error: ' + e.message); }
}

async function revokeAccessKey(tail) {
  if (!confirm('Revoke access key ...' + tail + '? Anyone using it will lose access after the next restart.')) return;
  try {
    const r = await fetch('/v1/config/proxy-keys/' + tail, {
      method: 'DELETE',
      headers: {'Authorization':'Bearer '+apiKey},
    });
    const d = await r.json();
    if (!r.ok) { alert(d.error?.message || 'Failed to revoke.'); return; }
    showRestartBanner();
    await refresh();
  } catch(e) { alert('Network error: ' + e.message); }
}

async function createAccessKey() {
  const checked = [...document.querySelectorAll('#ak-provider-scope input:checked')].map(el => el.value);
  const body = {
    name: document.getElementById('ak-name').value,
    rpm: document.getElementById('ak-rpm').value,
    req_per_day: document.getElementById('ak-reqday').value,
    tokens_per_day: document.getElementById('ak-tokday').value,
    cost_per_day: document.getElementById('ak-costday').value,
    allowed_providers: checked,
  };
  try {
    const r = await fetch('/v1/config/proxy-keys', {
      method: 'POST',
      headers: {'Authorization':'Bearer '+apiKey, 'Content-Type':'application/json'},
      body: JSON.stringify(body),
    });
    const d = await r.json();
    if (!r.ok) { setMsg('ak-create-msg', d.error?.message || 'Failed to create key.', false); return; }
    ['ak-name','ak-rpm','ak-reqday','ak-tokday','ak-costday'].forEach(id => document.getElementById(id).value = '');
    document.querySelectorAll('#ak-provider-scope input:checked').forEach(el => el.checked = false);
    setMsg('ak-create-msg', 'Key created.', true);
    document.getElementById('new-key-value').value = d.key;
    document.getElementById('new-key-panel').style.display = 'block';
    document.getElementById('new-key-panel').scrollIntoView({behavior:'smooth', block:'nearest'});
    showRestartBanner();
    await refresh();
  } catch(e) { setMsg('ak-create-msg', 'Network error: ' + e.message, false); }
}

function copyNewKey() {
  const el = document.getElementById('new-key-value');
  el.select();
  navigator.clipboard?.writeText(el.value).catch(() => document.execCommand('copy'));
}

function dismissNewKey() {
  document.getElementById('new-key-panel').style.display = 'none';
  document.getElementById('new-key-value').value = '';
}

// ── combined Models table (capabilities + rate headroom) ─────────────────────
const RL_BUCKET_ORDER_R = ['RPM','RPH','RPD','RPW','RPMo'];
const RL_BUCKET_ORDER_T = ['TPM','TPH','TPD','TPW','TPMo'];

function _modelRowKey(provider, model) {
  return (provider || '') + '\0' + (model || '');
}

function _statusKeysForProvider(provider) {
  return (statusData?.providers?.[provider]?.keys) || [];
}

function _matchStatusKey(keys, hint) {
  if (!hint) return null;
  return keys.find(k => {
    const tail = k.key_tail || '';
    return hint === tail || hint.endsWith(tail) || (tail && hint.includes(tail));
  }) || null;
}

function _modelKeyDots(provider, groups) {
  const keys = _statusKeysForProvider(provider);
  const hints = [...new Set((groups || []).map(g => g.key_hint).filter(Boolean))];
  if (!hints.length) return '<span class="muted">—</span>';
  return hints.map(hint => {
    const k = _matchStatusKey(keys, hint);
    if (k) {
      const cls = k.status === 'cooling' ? 'dot-warn' : 'dot-ok';
      const req = k.requests != null ? `${k.requests} req` : '';
      const title = (k.status === 'cooling' ? `cooling (${k.ready_in}s)` : 'ready')
        + (req ? ` · ${req}` : '') + ` · …${hint}`;
      return `<span class="${cls}" title="${esc(title)}" style="display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:2px"></span>`;
    }
    return `<span class="dot-grey" title="…${esc(hint)}" style="display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:2px;background:var(--muted);opacity:.5"></span>`;
  }).join('');
}

function _aggregateModelGroups(groups) {
  let headroom = null, binding = null;
  for (const g of groups || []) {
    if (g.headroom == null) continue;
    if (headroom == null || g.headroom < headroom) {
      headroom = g.headroom;
      binding = g.binding || null;
    }
  }
  return {headroom, binding};
}

function _collectCombinedModelRows(showOrphans) {
  const byKey = new Map();
  const prov = statusData?.providers || {};

  Object.entries(prov).forEach(([name, p]) => {
    const caps = p.model_caps;
    const entries = (caps && caps.length)
      ? caps.map(mc => ({model: mc.model, gi: mc.gi, gi_source: mc.gi_source, tools: mc.supports_tools, reasoning: mc.reasoning}))
      : (p.model ? [{model: p.model, gi: p.gi, gi_source: p.gi_source, tools: p.supports_tools, reasoning: p.reasoning}] : []);
    entries.forEach(e => {
      if (!e.model) return;
      const k = _modelRowKey(name, e.model);
      byKey.set(k, {
        provider: name, model: e.model, gi: e.gi, gi_source: e.gi_source,
        tools: e.tools, reasoning: e.reasoning, orphan: false, groups: [],
      });
    });
  });

  const modelGroups = (rateLimitsData || []).filter(g => g.scope === 'model');
  for (const g of modelGroups) {
    if (!showOrphans && g.configured === false) continue;
    const k = _modelRowKey(g.provider, g.model);
    let row = byKey.get(k);
    if (!row) {
      if (!showOrphans) continue;
      row = {
        provider: g.provider, model: g.model || '', gi: null, gi_source: null,
        tools: null, reasoning: null, orphan: true, groups: [],
      };
      byKey.set(k, row);
    }
    row.groups.push(g);
  }

  return [...byKey.values()].map(row => {
    const agg = _aggregateModelGroups(row.groups);
    return {...row, headroom: agg.headroom, binding: agg.binding};
  });
}

function _modelSortValue(row, key) {
  if (key === 'model') return row.model || '';
  if (key === 'provider') return row.provider || '';
  if (key === 'gi' || key === 'rating') return row.gi == null ? null : row.gi;
  if (key === 'binding') return row.binding || null;
  if (key === 'headroom') return row.headroom == null ? null : row.headroom;
  return null;
}

function renderCombinedModels() {
  const tbody = document.getElementById('rl-tbody-model');
  if (!tbody) return;
  _rlSyncSortHeaders('rl-panel-model', rlSortModel.key, rlSortModel.dir);
  const showOrphans = document.getElementById('rl-orphans-model')?.checked;
  const rows = _collectCombinedModelRows(showOrphans).slice().sort((a, b) => {
    const va = _modelSortValue(a, rlSortModel.key);
    const vb = _modelSortValue(b, rlSortModel.key);
    if (va == null && vb == null) return 0;
    if (va == null) return 1;
    if (vb == null) return -1;
    let cmp;
    if (typeof va === 'number' && typeof vb === 'number') cmp = va - vb;
    else cmp = String(va).localeCompare(String(vb));
    return cmp * rlSortModel.dir;
  });
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--muted);padding:24px">No models configured</td></tr>';
    return;
  }
  tbody.innerHTML = '';
  rows.forEach(row => {
    const selected = selectedModelRow
      && selectedModelRow.provider === row.provider
      && selectedModelRow.model === row.model;
    const tr = document.createElement('tr');
    tr.className = 'rl-row' + (selected ? ' rl-selected' : '');
    tr.onclick = () => selectModelRow(row.provider, row.model);
    const hPct = row.headroom == null ? null : Math.round(row.headroom * 100);
    const hColor = hPct == null ? '' : hPct >= 50 ? 'green' : hPct >= 20 ? 'yellow' : 'red';
    const bar = hPct == null
      ? '<span class="muted">—</span>'
      : `<div style="display:inline-block;vertical-align:middle">
           <div class="prog-track" style="width:64px;display:inline-block">
             <div class="prog-fill ${hColor}" style="width:${hPct}%"></div>
           </div>
           <span class="muted" style="font-size:10px;margin-left:4px">${hPct}%</span>
         </div>`;
    const tools = row.tools == null ? '<span class="muted">—</span>'
      : (row.tools ? '<span class="pill pill-ok">yes</span>' : '<span class="pill pill-grey">no</span>');
    const reasoning = row.reasoning == null ? '<span class="muted">—</span>'
      : (row.reasoning ? '<span class="pill pill-ok">yes</span>' : '<span class="pill pill-grey">no</span>');
    tr.innerHTML = `
      <td><strong class="mono">${esc(row.model || '—')}</strong></td>
      <td class="muted">${esc(row.provider)}</td>
      <td>${_modelKeyDots(row.provider, row.groups)}</td>
      <td>${row.gi != null ? giBadge(row.gi, row.gi_source) : '<span class="muted">—</span>'}</td>
      <td>${tools}</td>
      <td>${reasoning}</td>
      <td>${row.binding || '<span class="muted">—</span>'}</td>
      <td>${bar}</td>`;
    tbody.appendChild(tr);
  });
}

function selectModelRow(provider, model) {
  selectedRateGroupId = null;
  selectedModelRow = {provider, model};
  renderRateLimitsTables();
}

// ── rate limits (token bucket filters) ───────────────────────────────────────
async function refreshRateLimits() {
  if (!apiKey) return;
  try {
    const orphansPw = document.getElementById('rl-orphans-pw')?.checked;
    const orphansModel = document.getElementById('rl-orphans-model')?.checked;
    const includeOrphans = (orphansPw || orphansModel) ? '1' : '0';
    const r = await fetch('/v1/rate-limits?include_orphans=' + includeOrphans, {
      headers: {'Authorization': 'Bearer ' + apiKey},
    });
    if (r.status === 401) return;
    if (!r.ok) return;
    const data = await r.json();
    rateLimitsData = data.groups || [];
    renderRateLimitsTables();
  } catch (e) { /* page still usable */ }
}

function renderRateLimitsTables() {
  renderProviderWideRateLimits();
  renderCombinedModels();
  if (selectedModelRow) {
    const groups = (rateLimitsData || []).filter(g =>
      g.scope === 'model'
      && g.provider === selectedModelRow.provider
      && g.model === selectedModelRow.model);
    if (groups.length) renderRateDetail(groups);
    else if (selectedModelRow) {
      // Still open title for a model with no rate groups yet
      renderRateDetail([]);
    }
  } else if (selectedRateGroupId) {
    const g = rateLimitsData.find(r => r.id === selectedRateGroupId);
    if (g) renderRateDetail([g]);
    else closeRateDetail();
  }
}

function sortRateLimits(scope, key) {
  const st = scope === 'provider_wide' ? rlSortPw : rlSortModel;
  if (st.key === key) st.dir = -st.dir;
  else { st.key = key; st.dir = 1; }
  if (scope === 'provider_wide') renderProviderWideRateLimits();
  else renderCombinedModels();
  if (selectedModelRow || selectedRateGroupId) {
    // re-apply open modal after sort re-render
    if (selectedModelRow) {
      const groups = (rateLimitsData || []).filter(g =>
        g.scope === 'model'
        && g.provider === selectedModelRow.provider
        && g.model === selectedModelRow.model);
      if (groups.length) renderRateDetail(groups);
    } else if (selectedRateGroupId) {
      const g = rateLimitsData.find(r => r.id === selectedRateGroupId);
      if (g) renderRateDetail([g]);
    }
  }
}

function _rlSortValue(g, key) {
  if (key === 'provider') return g.provider || '';
  if (key === 'key_hint') return g.key_hint || '';
  if (key === 'scope') return g.scope === 'model' ? (g.model || '') : 'provider-wide';
  if (key === 'binding') return g.binding || null;
  if (key === 'headroom') return g.headroom == null ? null : g.headroom;
  if (key === 'buckets') return Object.keys(g.buckets || {}).length;
  return null;
}

function _rlCompare(a, b, sortKey, sortDir) {
  const va = _rlSortValue(a, sortKey);
  const vb = _rlSortValue(b, sortKey);
  if (va == null && vb == null) return 0;
  if (va == null) return 1;
  if (vb == null) return -1;
  let cmp;
  if (typeof va === 'number' && typeof vb === 'number') cmp = va - vb;
  else cmp = String(va).localeCompare(String(vb));
  return cmp * sortDir;
}

function _rlSyncSortHeaders(panelId, sortKey, sortDir) {
  document.querySelectorAll('#' + panelId + ' thead th.sortable').forEach(th => {
    th.classList.remove('sorted-asc', 'sorted-desc');
    if (th.dataset.sort === sortKey)
      th.classList.add(sortDir === 1 ? 'sorted-asc' : 'sorted-desc');
  });
}

function _rlFilterGroups(scope, showOrphans) {
  return (rateLimitsData || []).filter(g => {
    if (g.scope !== scope) return false;
    if (!showOrphans && g.configured === false) return false;
    return true;
  });
}

function _rlRenderRow(g) {
  const tr = document.createElement('tr');
  tr.className = 'rl-row' + (g.id === selectedRateGroupId ? ' rl-selected' : '');
  tr.onclick = () => selectRateGroup(g.id);
  const hPct = g.headroom == null ? null : Math.round(g.headroom * 100);
  const hColor = hPct == null ? '' : hPct >= 50 ? 'green' : hPct >= 20 ? 'yellow' : 'red';
  const bar = hPct == null
    ? '<span class="muted">—</span>'
    : `<div style="display:inline-block;vertical-align:middle">
         <div class="prog-track" style="width:64px;display:inline-block">
           <div class="prog-fill ${hColor}" style="width:${hPct}%"></div>
         </div>
         <span class="muted" style="font-size:10px;margin-left:4px">${hPct}%</span>
       </div>`;
  const nBuckets = Object.keys(g.buckets || {}).length;
  tr.innerHTML = `
    <td><strong>${g.provider}</strong></td>
    <td class="mono muted">…${g.key_hint || ''}</td>
    <td>${g.binding || '<span class="muted">—</span>'}</td>
    <td>${bar}</td>
    <td class="right muted">${nBuckets}</td>`;
  return tr;
}

function _rlRenderTable(scope, tbodyId, panelId, sortState, showOrphans, emptyColspan) {
  const tbody = document.getElementById(tbodyId);
  if (!tbody) return;
  _rlSyncSortHeaders(panelId, sortState.key, sortState.dir);
  const rows = _rlFilterGroups(scope, showOrphans).slice().sort((a, b) =>
    _rlCompare(a, b, sortState.key, sortState.dir));
  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="${emptyColspan}" style="text-align:center;color:var(--muted);padding:24px">No rate data yet</td></tr>`;
    return;
  }
  tbody.innerHTML = '';
  rows.forEach(g => tbody.appendChild(_rlRenderRow(g)));
}

function renderProviderWideRateLimits() {
  const showOrphans = document.getElementById('rl-orphans-pw')?.checked;
  _rlRenderTable('provider_wide', 'rl-tbody-pw', 'rl-panel-pw', rlSortPw, showOrphans, 5);
}

function selectRateGroup(id) {
  selectedModelRow = null;
  selectedRateGroupId = id;
  renderRateLimitsTables();
}

function closeRateDetail() {
  selectedRateGroupId = null;
  selectedModelRow = null;
  const modal = document.getElementById('rl-detail-modal');
  if (modal) modal.classList.add('hidden');
  document.querySelectorAll('#rl-tbody-pw tr.rl-selected, #rl-tbody-model tr.rl-selected').forEach(tr => tr.classList.remove('rl-selected'));
}

function _rlFmtAmt(n) {
  if (n == null || !isFinite(n)) return '—';
  if (Math.abs(n) >= 1000) return fmt.num(Math.round(n));
  if (Math.abs(n) >= 10) return String(Math.round(n * 10) / 10);
  return String(Math.round(n * 100) / 100);
}

function _rlBucketBarRow(name, b) {
  const cap = Number(b.cap) || 0;
  const used = Number(b.used) || 0;
  const pct = cap > 0 ? Math.max(0, Math.min(100, (used / cap) * 100)) : 0;
  const usedInside = pct >= 15;
  const muted = b.active ? '' : ' muted';
  const usedLabel = _rlFmtAmt(used);
  const capLabel = _rlFmtAmt(cap);
  const fillInner = usedInside ? esc(usedLabel) : '';
  const usedOut = usedInside
    ? ''
    : `<span class="rl-bar-used-out">${esc(usedLabel)}</span>`;
  return `<div class="rl-bar-row${muted}">
    <span class="rl-bar-name">${esc(name)}</span>
    <div class="rl-bar-track">
      <div class="rl-bar-fill" style="width:${pct}%">${fillInner}</div>
    </div>
    ${usedOut}
    <span class="rl-bar-cap">${esc(capLabel)}</span>
  </div>`;
}

function _rlRenderBucketBars(buckets) {
  const bmap = buckets || {};
  const rRows = RL_BUCKET_ORDER_R.filter(n => n in bmap).map(n => _rlBucketBarRow(n, bmap[n])).join('');
  const tRows = RL_BUCKET_ORDER_T.filter(n => n in bmap).map(n => _rlBucketBarRow(n, bmap[n])).join('');
  // Also include any unexpected bucket names after known order
  const known = new Set([...RL_BUCKET_ORDER_R, ...RL_BUCKET_ORDER_T]);
  const extra = Object.keys(bmap).filter(n => !known.has(n)).sort()
    .map(n => _rlBucketBarRow(n, bmap[n])).join('');
  if (!rRows && !tRows && !extra)
    return '<div class="muted" style="padding:4px 0">No buckets</div>';
  let html = '';
  if (rRows) html += rRows;
  if (rRows && tRows) html += '<div class="rl-dim-sep" role="separator"></div>';
  if (tRows) html += tRows;
  if (extra) {
    if (html) html += '<div class="rl-dim-sep" role="separator"></div>';
    html += extra;
  }
  return html;
}

function _rlKeyDotForGroup(g) {
  const keys = _statusKeysForProvider(g.provider);
  const k = _matchStatusKey(keys, g.key_hint);
  if (k) {
    const cls = k.status === 'cooling' ? 'dot-warn' : 'dot-ok';
    return `<span class="${cls}" style="display:inline-block;width:8px;height:8px;border-radius:50%"></span>`;
  }
  return `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--muted);opacity:.5"></span>`;
}

function renderRateDetail(groups) {
  const list = Array.isArray(groups) ? groups.slice() : (groups ? [groups] : []);
  const modal = document.getElementById('rl-detail-modal');
  if (modal) modal.classList.remove('hidden');

  const blockTop = document.getElementById('rl-detail-block-top');
  if (blockTop) {
    if (selectedModelRow) {
      blockTop.classList.remove('hidden');
      blockTop.onclick = () => blockSelectedModel();
    } else {
      blockTop.classList.add('hidden');
      blockTop.onclick = null;
    }
  }

  const clearTop = document.getElementById('rl-detail-clear-top');
  if (clearTop) {
    if (list.length === 1) {
      clearTop.classList.remove('hidden');
      clearTop.onclick = () => clearRateGroup(list[0].id);
    } else {
      clearTop.classList.add('hidden');
      clearTop.onclick = null;
    }
  }

  const giBox = document.getElementById('rl-detail-gi');
  if (giBox) {
    if (selectedModelRow) {
      giBox.classList.remove('hidden');
      const row = _collectCombinedModelRows(true).find(r =>
        r.provider === selectedModelRow.provider && r.model === selectedModelRow.model);
      const gi = row && row.gi != null ? row.gi : '';
      const src = row && row.gi_source ? row.gi_source : '';
      const clearBtn = src === 'override'
        ? `<button class="btn" type="button" onclick="clearGiOverride()">Clear override</button>`
        : '';
      giBox.innerHTML = `
        <div style="display:flex;flex-wrap:wrap;align-items:center;gap:8px">
          <strong>GI</strong> ${giBadge(gi === '' ? null : gi, src || null)}
          <label class="muted">Set <input id="rl-gi-input" type="number" min="0" max="100" step="0.1"
            value="${gi === '' ? '' : Number(gi).toFixed(1)}"
            style="width:72px;margin-left:4px;background:var(--surface2);border:1px solid var(--border);color:var(--text);border-radius:4px;padding:2px 6px"></label>
          <button class="btn" type="button" onclick="saveGiOverride()">Save</button>
          ${clearBtn}
        </div>`;
    } else {
      giBox.classList.add('hidden');
      giBox.innerHTML = '';
    }
  }

  if (selectedModelRow) {
    document.getElementById('rl-detail-title').textContent =
      (selectedModelRow.model || '—') + ' · ' + selectedModelRow.provider;
    document.getElementById('rl-detail-meta').innerHTML =
      list.length
        ? `${list.length} key group${list.length === 1 ? '' : 's'} · authoritative model buckets`
        : '<span class="muted">No rate-limit groups yet for this model</span>';
  } else if (list.length) {
    const g = list[0];
    document.getElementById('rl-detail-title').textContent =
      g.provider + ' · …' + (g.key_hint || '');
    const cfg = g.configured
      ? '<span class="pill pill-ok">configured</span>'
      : '<span class="pill pill-warn">orphan</span>';
    const roleNote = g.role === 'estimate'
      ? ' · shared-ceiling estimate — no header sync'
      : (g.role === 'authoritative' ? ' · authoritative' : '');
    document.getElementById('rl-detail-meta').innerHTML =
      `${cfg} · ${g.scope === 'model' ? 'model ' + (g.model || '') : 'provider-wide'}${roleNote} · <span class="mono">${esc(g.id)}</span>`;
  } else {
    document.getElementById('rl-detail-title').textContent = 'Detail';
    document.getElementById('rl-detail-meta').textContent = '';
  }

  const body = document.getElementById('rl-detail-body');
  if (!list.length) {
    body.innerHTML = '<div class="muted" style="padding:16px">No bucket data</div>';
    return;
  }
  const sorted = list.slice().sort((a, b) => String(a.key_hint || '').localeCompare(String(b.key_hint || '')));
  body.innerHTML = sorted.map(g => {
    const clearBtn = list.length > 1
      ? `<button class="btn" type="button" data-rl-clear="${attr(g.id)}">Clear learned state</button>`
      : '';
    return `<div class="rl-key-section">
      <div class="rl-key-section-hdr">
        <div class="rl-key-section-label">${_rlKeyDotForGroup(g)}<span class="mono muted">…${esc(g.key_hint || '')}</span></div>
        ${clearBtn}
      </div>
      ${_rlRenderBucketBars(g.buckets)}
    </div>`;
  }).join('');
  body.querySelectorAll('[data-rl-clear]').forEach(btn => {
    btn.addEventListener('click', () => clearRateGroup(btn.getAttribute('data-rl-clear')));
  });
}

async function saveGiOverride() {
  if (!selectedModelRow || !apiKey) return;
  const input = document.getElementById('rl-gi-input');
  const gi = Number(input && input.value);
  if (!isFinite(gi) || gi < 0 || gi > 100) {
    alert('GI must be a number between 0 and 100');
    return;
  }
  try {
    const r = await fetch('/v1/config/gi-override', {
      method: 'PUT',
      headers: {'Authorization': 'Bearer ' + apiKey, 'Content-Type': 'application/json'},
      body: JSON.stringify({
        provider: selectedModelRow.provider,
        model: selectedModelRow.model,
        gi,
      }),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      alert((data.error && data.error.message) || ('Save failed: HTTP ' + r.status));
      return;
    }
    await refresh();
    await refreshRateLimits();
  } catch (e) {
    alert('Save failed: ' + e);
  }
}

async function clearGiOverride() {
  if (!selectedModelRow || !apiKey) return;
  try {
    const r = await fetch('/v1/config/gi-override', {
      method: 'DELETE',
      headers: {'Authorization': 'Bearer ' + apiKey, 'Content-Type': 'application/json'},
      body: JSON.stringify({
        provider: selectedModelRow.provider,
        model: selectedModelRow.model,
      }),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      alert((data.error && data.error.message) || ('Clear failed: HTTP ' + r.status));
      return;
    }
    await refresh();
    await refreshRateLimits();
  } catch (e) {
    alert('Clear failed: ' + e);
  }
}

async function clearRateGroup(id) {
  const gid = id || selectedRateGroupId;
  if (!gid) return;
  if (!confirm('Clear learned rate-limit state for this group? Caps will relearn from traffic.')) return;
  try {
    const r = await fetch('/v1/rate-limits/clear', {
      method: 'POST',
      headers: {'Authorization': 'Bearer ' + apiKey, 'Content-Type': 'application/json'},
      body: JSON.stringify({id: gid}),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      alert(err.error || ('Clear failed: HTTP ' + r.status));
    }
    // Keep model row selection so modal refreshes for remaining keys
    if (!selectedModelRow) closeRateDetail();
    await refreshRateLimits();
  } catch (e) {
    alert('Clear failed: ' + e);
  }
}

async function refreshExcludedModels() {
  if (!apiKey) return;
  try {
    const r = await fetch('/v1/config/excluded-models', {
      headers: {'Authorization': 'Bearer ' + apiKey},
    });
    if (r.status === 401 || !r.ok) return;
    const data = await r.json();
    excludedModelsData = data.excluded || [];
    renderBlockedModels();
  } catch (e) { /* page still usable */ }
}

function renderBlockedModels() {
  const tbody = document.getElementById('rl-tbody-blocked');
  if (!tbody) return;
  const rows = (excludedModelsData || []).slice().sort((a, b) => {
    const c = String(a.provider || '').localeCompare(String(b.provider || ''));
    if (c) return c;
    return String(a.model || '').localeCompare(String(b.model || ''));
  });
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="3" class="muted">No blocked models</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map(row => `<tr>
    <td class="mono">${esc(row.model || '')}</td>
    <td>${esc(row.provider || '')}</td>
    <td style="text-align:right">
      <button class="btn" type="button"
        data-unblock-provider="${attr(row.provider || '')}"
        data-unblock-model="${attr(row.model || '')}">Unblock</button>
    </td>
  </tr>`).join('');
  tbody.querySelectorAll('[data-unblock-provider]').forEach(btn => {
    btn.addEventListener('click', () => unblockModel(
      btn.getAttribute('data-unblock-provider'),
      btn.getAttribute('data-unblock-model')));
  });
}

async function blockSelectedModel() {
  if (!selectedModelRow) return;
  const provider = selectedModelRow.provider;
  const model = selectedModelRow.model;
  if (!provider || !model) return;
  if (!confirm('Block ' + provider + '/' + model + '? It will be added to '
    + provider.toUpperCase() + '_EXCLUDE_MODELS and will not be routed.')) return;
  try {
    const r = await fetch('/v1/config/exclude-model', {
      method: 'POST',
      headers: {'Authorization': 'Bearer ' + apiKey, 'Content-Type': 'application/json'},
      body: JSON.stringify({provider, model, blocked: true}),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      alert((err.error && err.error.message) || err.error || ('Block failed: HTTP ' + r.status));
      return;
    }
    closeRateDetail();
    await refresh();
  } catch (e) {
    alert('Block failed: ' + e);
  }
}

async function unblockModel(provider, model) {
  if (!provider || !model) return;
  if (!confirm('Unblock ' + provider + '/' + model + '? It will be removed from '
    + provider.toUpperCase() + '_EXCLUDE_MODELS and restored to routing.')) return;
  try {
    const r = await fetch('/v1/config/exclude-model', {
      method: 'POST',
      headers: {'Authorization': 'Bearer ' + apiKey, 'Content-Type': 'application/json'},
      body: JSON.stringify({provider, model, blocked: false}),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      alert((err.error && err.error.message) || err.error || ('Unblock failed: HTTP ' + r.status));
      return;
    }
    await refresh();
  } catch (e) {
    alert('Unblock failed: ' + e);
  }
}
</script>
</body>
</html>
"""


@app.route("/")
def root():
    """Land bare-host visitors on the dashboard so `http://<host>:<port>` just works
    in a browser — no need to know the /dashboard path. API clients use /v1/* and
    never hit this."""
    return redirect("/dashboard", code=302)


@app.route("/dashboard")
def dashboard():
    """Self-contained monitoring dashboard. Opens in any browser.
    Polls /v1/status, /v1/usage, and /v1/logs every 5 seconds.
    Prompts for the proxy API key on first load (stored in localStorage)."""
    return Response(_DASHBOARD_HTML, content_type="text/html; charset=utf-8")


@app.route("/favicon.ico")
def favicon():
    return Response(status=204)


@app.route("/health")
def health():
    """Unauthenticated health check for uptime monitoring."""
    return jsonify({"status": "ok", "providers": [p["name"] for p in PROVIDERS]})


@app.route("/v1/models")
def models():
    err = _auth_check()
    if err:
        return err
    data = [{"id": ROUTER_MODEL, "object": "model", "owned_by": "hermes-router"}]
    # Advertise the fast/conversation profile only when a local model is configured,
    # since that's what it routes short turns to.
    if any(p["name"] == "local" for p in PROVIDERS):
        data.append({"id": f"{ROUTER_MODEL}:fast", "object": "model", "owned_by": "hermes-router"})
    for mid in _chat_catalog_model_ids():
        data.append({"id": mid, "object": "model", "owned_by": "hermes-router"})
    return jsonify({"object": "list", "data": data})


def _route_completion(payload: dict, streaming: bool, ns: str = "",
                      *, _rate_retry: bool = False,
                      _session_id: str | None = None,
                      _sticky: dict | None = None):
    """Core selection + fallback cascade for /v1/chat/completions.
    Takes an OpenAI-format payload and returns one of:
        ("json",   data_dict)            non-streaming success (OpenAI format)
        ("stream", generator, provider)  streaming success; generator yields
                                         OpenAI-format SSE regardless of upstream
        ("error",  error_dict, status)   every provider exhausted
    """
    # Seed per-thread routing context so endpoint handlers can read it back
    # after this call returns (provider chosen, cascade count, cache-hit flag).
    _req_ctx.provider  = None
    _req_ctx.model     = None
    _req_ctx.cache_hit = False
    _req_ctx.attempts  = 0   # total forward() calls made
    _req_ctx.last_tried_provider = None
    _req_ctx.last_tried_model = None
    if not _rate_retry:
        _req_ctx.cascade = CascadeTrail()
    trail = getattr(_req_ctx, "cascade", None) or CascadeTrail()
    _req_ctx.cascade = trail

    def _crec(method, *a, **k):
        try:
            getattr(trail, method)(*a, **k)
        except Exception as e:
            log.debug(f"cascade trail record failed: {e}")

    # Routing profile: `hermes-router:fast` (or header X-Hermes-Profile: fast)
    # prefers a local model for short/casual turns, with cloud as fallback. We
    # normalize the model back to the router id so the cache and upstream model
    # selection behave exactly like a default request.
    original_model = str(payload.get("model") or "")
    pinned = not _is_auto_model_id(original_model)

    prefer_local = False
    if original_model.endswith(":fast"):
        prefer_local = True
        payload = {**payload, "model": ROUTER_MODEL}
    else:
        try:
            if request.headers.get("X-Hermes-Profile", "").strip().lower() == "fast":
                prefer_local = True
        except RuntimeError:
            pass  # called outside a request context (e.g. tests)

    messages = payload.get("messages", [])
    ordered = None
    if pinned:
        ordered = _filter_candidates_by_pin(
            _ordered_providers(payload, prefer_local, sticky=_sticky),
            original_model,
        )
        caller_scope = None
        try:
            caller_scope = KEY_PROVIDER_SCOPE.get(_caller_token())
        except RuntimeError:
            pass
        if caller_scope is not None:
            allowed = set(caller_scope)
            ordered = [
                c for c in ordered if c["provider"]["name"] in allowed]
        if not ordered:
            return ("error", {
                "error": {
                    "message": (
                        f"Model '{original_model}' is not in the proxy catalog "
                        f"(or not allowed for this access key). "
                        f"See GET /v1/models."
                    ),
                    "type": "invalid_request_error",
                }
            }, 400)

    # Cache check (non-streaming only): exact match first (cheap), then optional
    # semantic match. query_emb is reused to store the response so future similar
    # prompts can match it.
    query_emb = None
    if not streaming:
        cached = cache.get(payload, ns)
        if cached is not None:
            log.info("↩ cache hit")
            _req_ctx.cache_hit = True
            return ("json", cached)
        if SEMANTIC_CACHE and not pinned and _embed_ordered():
            query_emb = _embed_text(_prompt_text(messages))
            if query_emb:
                hit = cache.semantic_lookup(query_emb, ns)
                if hit is not None:
                    log.info("↩ semantic cache hit")
                    _req_ctx.cache_hit = True
                    return ("json", hit)

    est_tokens = _estimated_tokens(messages)
    if ordered is None:
        ordered = _ordered_providers(payload, prefer_local, sticky=_sticky)

    def _leave_sticky_model(name: str, model: str) -> None:
        nonlocal _sticky
        if (_session_id and _sticky
                and _sticky.get("provider") == name
                and _sticky.get("model") == model):
            _clear_sticky(_session_id)
            _sticky = None

    def _remember_success(name: str, model: str, key: str) -> None:
        _remember_sticky(_session_id, name, model, key)

    # Tool-aware routing: when the request carries tools, prefer (provider, model)
    # candidates whose MODEL actually supports function calling — otherwise a model
    # that silently ignores tools would return plain text instead of the tool call.
    # SAFETY — only enforce this when at least one tool-capable candidate exists;
    # if none do, fall through to all of them rather than hard-fail.
    needs_tools  = bool(payload.get("tools"))
    enforce_tool = needs_tools and any(
        _model_supports_tools(c["provider"]["name"], c["model"]) for c in ordered)

    # Vision-aware routing: when the request carries an image, prefer candidates
    # whose MODEL is known to accept image input — otherwise the request cascades
    # through every text-only model's clean 400/403 rejection first, wasting real
    # latency before reaching one that actually works. SAFETY — same fallback as
    # tools: only enforce when at least one vision-capable candidate exists.
    needs_vision  = _payload_has_image(payload)
    enforce_vision = needs_vision and any(
        _model_supports_vision(c["provider"], c["model"]) for c in ordered)

    # Provider scoping: an access key can be restricted to specific providers
    # from the dashboard's Access Keys page. Unlike tool/vision detection above,
    # this is an explicit admin restriction, not a heuristic — so there is
    # deliberately NO safety-net fallback. If none of the caller's allowed
    # providers are viable right now, the request should fail rather than
    # silently route through a provider it was scoped away from.
    caller_providers = None
    try:
        caller_providers = KEY_PROVIDER_SCOPE.get(_caller_token())
    except RuntimeError:
        pass  # called outside a request context (e.g. tests)

    # Circuit breaker: skip providers whose breaker is open. SAFETY — if EVERY
    # candidate is open, treat them all as half-open probes (skip none) so we
    # always make forward progress instead of hard-failing while options remain.
    any_closed = any(not stats.breaker_open(c["provider"]["name"]) for c in ordered)

    # Per-(provider, model) failover: walk the ranked candidate list, rotating keys
    # within each candidate. A whole provider is taken out of the running for this
    # request (skip_providers) on auth / payload / unexpected errors — those won't
    # be fixed by another of its models.
    # Tool-deferred: models cached as tools=no are skipped on the first pass, then
    # retried once as a last resort if every tool-capable candidate was exhausted
    # (false-negative probes must not 503 when a capable model remains).
    skip_providers: set = set()
    tool_deferred: list = []
    work: list = [("main", c) for c in ordered]
    wi = 0
    appended_last_resort = False
    _best_rl_wait: float | None = None

    def _queue_tool_last_resort() -> None:
        nonlocal appended_last_resort, work
        if wi >= len(work) and not appended_last_resort and tool_deferred:
            log.info(f"⚒ last-resort: trying {len(tool_deferred)} candidate(s) "
                     "skipped for no tool support")
            appended_last_resort = True
            work.extend(("last_resort", c) for c in tool_deferred)

    while wi < len(work):
        phase, cand = work[wi]
        wi += 1
        provider = cand["provider"]
        name     = provider["name"]
        model    = cand["model"]

        if name in skip_providers:
            _queue_tool_last_resort()
            continue

        # Caller's access key is scoped to specific providers — skip anything else.
        if caller_providers is not None and name not in caller_providers:
            _crec("skip", name, model, "access_scope")
            skip_providers.add(name)
            _leave_sticky_model(name, model)
            _queue_tool_last_resort()
            continue

        # Breaker open → skip the whole provider (unless all are open, then probe).
        if any_closed and stats.breaker_open(name):
            log.info(f"⨂ skipping {name} (circuit open)")
            _crec("skip", name, model, "circuit_open")
            skip_providers.add(name)
            _leave_sticky_model(name, model)
            _queue_tool_last_resort()
            continue

        # Skip only on env fences and high-confidence learned caps. Low-confidence
        # guesses are explorable (bump → learn → raise confidence).
        cap = _hard_input_cap_for(provider, model)
        if cap and est_tokens >= cap:
            log.info(f"⤳ skipping {name}/{model} (~{est_tokens} tok >= {cap} cap)")
            _crec("skip", name, model, "token_cap")
            _leave_sticky_model(name, model)
            _queue_tool_last_resort()
            continue

        # Tool request → skip candidates whose MODEL can't do function calling
        # (per-model; another model on the same provider may still qualify).
        # First pass only — deferred list is retried after tool-capable path exhausts.
        if (enforce_tool and phase == "main"
                and not _model_supports_tools(name, model)):
            log.info(f"⚒ skipping {name}/{model} (no tool support)")
            _crec("skip", name, model, "no_tools")
            tool_deferred.append(cand)
            _leave_sticky_model(name, model)
            _queue_tool_last_resort()
            continue

        # Vision request → skip candidates whose MODEL isn't known to accept images.
        if enforce_vision and not _model_supports_vision(provider, model):
            log.info(f"🖼 skipping {name}/{model} (no vision support)")
            _crec("skip", name, model, "no_vision")
            _leave_sticky_model(name, model)
            _queue_tool_last_resort()
            continue

        # Unsuitable-model cooldown: skip models that recently returned 404 /
        # model-not-found (exponential backoff). Payload-shaped 400s do not cool.
        if unsuitable_models.is_cooling(name, model):
            _ready = unsuitable_models.ready_in(name, model)
            _fails = unsuitable_models.failures(name, model)
            log.info(f"⏭ skipping {name}/{model} (unsuitable, "
                     f"failures={_fails}, ready in {_ready:.0f}s)")
            _crec("skip", name, model, "unsuitable_cooling")
            _leave_sticky_model(name, model)
            _queue_tool_last_resort()
            continue

        attempts = pool.key_count(name, model) or 1
        for _ in range(attempts):
            preferred = (_sticky["key"] if (_sticky and _sticky.get("provider") == name
                                            and _sticky.get("model") == model)
                         else None)
            key = pool.get_key(name, model, preferred=preferred)
            if not key:
                _crec("note", name, model, "skipped", "keys_cooling")
                break   # all keys for this (provider, model) are cooling → next candidate

            log.info(f"→ Trying {name}/{model} ...{key[-6:]}")
            _req_ctx.last_tried_provider = name
            _req_ctx.last_tried_model = model
            # Reserve against TBF using the request token estimate — never the
            # skip_if_tokens_over fence (that is a routing ceiling, not usage).
            _est_tokens = float(est_tokens) if est_tokens else max(
                1.0, sum(len(str(m.get("content", ""))) for m in
                         payload.get("messages", [])) / 4)
            # Thin headroom is a signal, not a hard skip: after burst-sized caps a
            # prior large debit can sit at ~0–5% while the next debit still fits,
            # and when it does not we want the wait tracked for exhausted-retry.
            _current_headroom = rate_limiter.headroom(name, key, model)
            if _current_headroom < RATE_HEADROOM_THRESHOLD:
                log.debug(f"  {name}/{model} thin headroom ({_current_headroom:.1%}) — attempting")
            _rl_ok, _rl_wait = rate_limiter.check_and_consume(
                name, key, model, req_count=1.0, token_count=_est_tokens)
            if not _rl_ok:
                if 0 < _rl_wait < RATE_ADMIT_WAIT_S:
                    log.debug(f"  {name}/{model} thin bucket — waiting {_rl_wait*1000:.0f}ms")
                    time.sleep(_rl_wait)
                    _rl_ok, _rl_wait = rate_limiter.check_and_consume(
                        name, key, model, req_count=1.0, token_count=_est_tokens)
                if not _rl_ok:
                    # Explore-into-limit: force-admit unless Retry-After still holds.
                    _rl_ok, _rl_wait = rate_limiter.check_and_consume(
                        name, key, model, req_count=1.0, token_count=_est_tokens,
                        force=True)
                if not _rl_ok:
                    if _rl_wait > 0 and (_best_rl_wait is None or _rl_wait < _best_rl_wait):
                        _best_rl_wait = float(_rl_wait)
                    log.info(f"  {name}/{model} rate hold ({_rl_wait:.1f}s) — trying next")
                    _crec("note", name, model, "skipped", "rate_hold")
                    continue
            _rl_t0 = time.time()
            _req_ctx.attempts += 1
            t0 = _rl_t0

            def _rl_release():
                rate_limiter.release_reservation(
                    name, key, model, 1.0, _est_tokens)

            _deadline = (
                ttft_baselines.deadline_s(name, model) if ttft_abort_enabled() else None
            )
            try:
                resp = forward(
                    provider, key, payload, streaming, model,
                    first_byte_deadline_s=_deadline,
                )
            except TtftDeadlineExceeded as _ttft_ex:
                _rl_release()
                _sum = ttft_baselines.summary(name, model)
                log.warning(
                    f"  {name}/{model} TTFT abort after {_ttft_ex.waited_s:.1f}s "
                    f"(deadline {_ttft_ex.deadline_s:.1f}s, ewma {_sum.get('ewma_s')}, "
                    f"n={_sum.get('sample_count', 0)})"
                )
                _crec("note", name, model, "failed", "ttft_deadline")
                _leave_sticky_model(name, model)
                continue
            elapsed = time.time() - t0

            if resp is None:
                _rl_release()
                stats.record_error(name)
                stats.record_health(name, False)   # network/timeout = provider health failure
                pool.mark_key_down(name, key, retry_after=30)
                _crec("note", name, model, "failed", "network")
                continue

            if resp.status_code == 429:
                _rl_release()
                stats.record_error(name)
                # 429 is NOT a health failure — rate limiter learns + Retry-After hold.
                rate_limiter.on_429(
                    name, key, model, dict(resp.headers),
                    model_headroom_before=_current_headroom,
                    observed_at=_rl_t0,
                )
                log.warning(f"  {name}/{model} 429 — rate-limit hold, trying next")
                _crec("note", name, model, "failed", "http_429")
                continue

            if resp.status_code in (401, 403):
                _rl_release()
                stats.record_error(name)
                btxt = (resp.text or "")[:300]
                _crec("note", name, model, "failed", http_reason(resp.status_code))
                # Some gateways (e.g. OpenCode) return a MODEL-level rejection as a
                # 401 — an ended free promo, an unsupported/paywalled model. That's
                # not a credential problem, so skip just this model and try the
                # provider's next one instead of disabling the whole provider.
                if re.search(r"modelerror|not supported|promotion has ended|subscrib|no payment|credits", btxt, re.I):
                    log.warning(f"  {name}/{model} {resp.status_code} model-level — skipping this model: {btxt[:160]}")
                    break
                # Genuine auth/permission failure — won't work for any model here.
                # Also count it against the circuit breaker: record_error() alone only
                # feeds /v1/usage stats, not the breaker (that's record_health-only). A
                # provider with a permanently bad/unsubscribed key would otherwise be
                # retried and rejected on every single future request forever, instead
                # of tripping the breaker and cooling down like any other unhealthy
                # provider (e.g. a key configured for a paid tier the account never
                # actually enabled, like OpenCode Go without Go billing turned on).
                log.error(f"  {name} {resp.status_code} — auth, skipping provider: {btxt[:200]}")
                stats.record_health(name, False)
                skip_providers.add(name)
                break

            if resp.status_code in (400, 413):
                _body_txt = ""
                try:
                    _body_txt = resp.text[:500]
                except Exception:
                    pass
                _req_max = _effective_requested_output_for_learning(provider, model, payload)
                _learn_token_cap_from_error(
                    provider_name=name,
                    model=model,
                    status_code=resp.status_code,
                    body=_body_txt,
                    est_tokens=int(_est_tokens),
                    requested_max_tokens=_req_max,
                )

            if resp.status_code in (400, 404):
                _rl_release()
                stats.record_error(name)
                try:
                    _err_body = (resp.text or "")[:500]
                except Exception:
                    _err_body = ""
                # model-specific (e.g. bad model name) — just skip this candidate.
                # Unsuitable (404 / model-not-found 400) cools with exponential
                # backoff so later requests skip it; payload-shaped 400s do not.
                if _is_unsuitable_model_error(resp.status_code, _err_body):
                    _delay = unsuitable_models.record(name, model)
                    log.warning(
                        f"  {name}/{model} {resp.status_code} unsuitable — "
                        f"cooling {_delay:.0f}s "
                        f"(failures={unsuitable_models.failures(name, model)}): "
                        f"{_err_body[:150]}")
                else:
                    log.warning(
                        f"  {name}/{model} {resp.status_code} — skipping this model: "
                        f"{_err_body[:150]}")
                _crec("note", name, model, "failed", http_reason(resp.status_code))
                break

            if resp.status_code == 413:
                _rl_release()
                stats.record_error(name)
                # payload-specific — bigger model won't help; cascade providers.
                log.warning(f"  {name} 413 — payload too large, cascading")
                _crec("note", name, model, "failed", "http_413")
                skip_providers.add(name)
                break

            if resp.status_code >= 500:
                _rl_release()
                stats.record_error(name)
                stats.record_health(name, False)   # 5xx = provider health failure
                pool.mark_key_down(name, key, retry_after=15)
                _crec("note", name, model, "failed", "http_5xx")
                continue

            if not (200 <= resp.status_code < 300):
                _rl_release()
                stats.record_error(name)
                stats.record_health(name, False)   # unexpected non-2xx = health failure
                log.warning(f"  {name} unexpected {resp.status_code} — skipping provider")
                _crec("note", name, model, "failed", http_reason(resp.status_code))
                skip_providers.add(name)
                break

            # Success
            stats.record_success(name, elapsed)
            stats.record_health(name, True)        # 2xx = healthy (half-open recovery)
            unsuitable_models.clear(name, model)
            log.info(f"  ✓ {name}/{model} {resp.status_code} ({elapsed*1000:.0f}ms)")
            _req_ctx.provider = name
            _req_ctx.model    = model
            is_anthropic = provider.get("protocol") == "anthropic"
            is_codex     = provider.get("protocol") == "codex"
            if is_codex:
                # Codex backend always streams SSE. Stream it through, or
                # aggregate it into one response for non-streaming clients.
                if streaming:
                    gen = _with_cleanup(resp, _codex_streaming_generator(resp))
                    wrapped = _streaming_with_usage(
                        gen, name, model, key=key, resp_headers=dict(resp.headers),
                        provider=provider, est_tokens=_est_tokens,
                    )
                    _remember_success(name, model, key)
                    _crec("success", name, model)
                    return ("stream", wrapped, name)
                events = []
                for raw in resp.iter_lines():
                    if not raw:
                        continue
                    raw = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
                    if raw.startswith("data:"):
                        ds = raw[5:].strip()
                        if ds and ds != "[DONE]":
                            try: events.append(json.loads(ds))
                            except Exception: pass
                data = _from_codex_response(events)
                if not _completion_has_output(data):
                    stats.record_error(name)
                    stats.record_health(name, False)
                    log.warning(f"  {name}/{model} empty completion — cascading")
                    _crec("note", name, model, "failed", "empty_response")
                    break
                _add_provider_tokens(name, data, model)
                _usage = data.get("usage") or {}
                _pt = _usage.get("prompt_tokens")
                _ct = _usage.get("completion_tokens")
                if _pt is not None or _ct is not None:
                    _learn_token_cap_from_success(
                        provider_name=name,
                        model=model,
                        prompt_tokens=_pt,
                        completion_tokens=_ct,
                        provider=provider,
                    )
                _actual_tokens = float(_usage.get("total_tokens") or _est_tokens)
                rate_limiter.reconcile(name, key, model, _est_tokens, _actual_tokens)
                rate_limiter.update_from_headers(
                    name, key, model, dict(resp.headers), observed_at=_rl_t0)
                rate_limiter.on_success(name, key, model, _actual_tokens)
                cache.set(payload, data, ns, query_emb)
                if _response_has_tool_calls(data):
                    _promote_tools_support(name, model)
                _remember_success(name, model, key)
                _crec("success", name, model)
                return ("json", data)
            if streaming:
                gen = (_anthropic_streaming_generator(resp) if is_anthropic
                       else _streaming_generator(resp))
                wrapped = _streaming_with_usage(
                    _with_cleanup(resp, gen), name, model, key=key,
                    resp_headers=dict(resp.headers), provider=provider,
                    est_tokens=_est_tokens, observed_at=_rl_t0,
                )
                _remember_success(name, model, key)
                _crec("success", name, model)
                return ("stream", wrapped, name)
            else:
                try:
                    raw = resp.json()
                except Exception:
                    raw = None
                data = (_from_anthropic_response(raw) if (is_anthropic and isinstance(raw, dict))
                        else raw)
                # Guard: a 2xx that carries no usable completion (no `choices`) — e.g.
                # a gateway that wraps an error in an HTTP-200 body (NVIDIA NIM's gRPC
                # "ResourceExhausted: Worker local total request limit reached"), or a
                # non-JSON body. Don't surface that to the caller as the answer —
                # treat it as a provider failure and cascade.
                if not isinstance(data, dict) or not data.get("choices"):
                    _rl_release()
                    stats.record_error(name)
                    stats.record_health(name, False)
                    err = data.get("error") if isinstance(data, dict) else None
                    emsg = (err.get("message", "") if isinstance(err, dict)
                            else err if isinstance(err, str)
                            else (data.get("message", "") if isinstance(data, dict) else ""))
                    # NVIDIA NIM (and similar gateways) wrap a transient rate-limit /
                    # resource-exhaustion error in an HTTP-200 body. Feed true RL
                    # signals into TBF; other empty 2xx bodies just cascade.
                    _emsg_l = str(emsg).lower()
                    _transient = any(s in _emsg_l for s in (
                        "resourceexhausted", "resource exhausted", "request limit reached",
                        "rate limit", "too many requests", "quota", "overloaded"))
                    (log.debug if _transient else log.warning)(
                        f"  {name}/{model} 2xx without choices — cascading: {str(emsg)[:140]}")
                    if _transient:
                        rate_limiter.on_429(
                            name, key, model, dict(resp.headers),
                            model_headroom_before=_current_headroom,
                            observed_at=_rl_t0,
                        )
                    _crec("note", name, model, "failed",
                          "http_429" if _transient else "empty_response")
                    break
                if not _completion_has_output(data):
                    _rl_release()
                    stats.record_error(name)
                    stats.record_health(name, False)
                    log.warning(f"  {name}/{model} empty completion — cascading")
                    _crec("note", name, model, "failed", "empty_response")
                    break
                if not is_anthropic:
                    _strip_response(data)
                _add_provider_tokens(name, data, model)
                _usage = data.get("usage") or {}
                _pt = _usage.get("prompt_tokens")
                _ct = _usage.get("completion_tokens")
                if _pt is not None or _ct is not None:
                    _learn_token_cap_from_success(
                        provider_name=name,
                        model=model,
                        prompt_tokens=_pt,
                        completion_tokens=_ct,
                        provider=provider,
                    )
                _actual_tokens = float(_usage.get("total_tokens") or _est_tokens)
                rate_limiter.reconcile(name, key, model, _est_tokens, _actual_tokens)
                rate_limiter.update_from_headers(
                    name, key, model, dict(resp.headers), observed_at=_rl_t0)
                rate_limiter.on_success(name, key, model, _actual_tokens)
                cache.set(payload, data, ns, query_emb)
                if _response_has_tool_calls(data):
                    _promote_tools_support(name, model)
                _remember_success(name, model, key)
                _crec("success", name, model)
                return ("json", data)

        _crec("flush")
        _leave_sticky_model(name, model)
        _queue_tool_last_resort()

    if (not _rate_retry
            and _best_rl_wait is not None
            and 0 < _best_rl_wait <= RATE_EXHAUSTED_WAIT_S):
        log.info(f"⏳ all candidates rate-limited — waiting {_best_rl_wait:.1f}s "
                 f"then retrying once (cap {RATE_EXHAUSTED_WAIT_S:g}s)")
        time.sleep(_best_rl_wait)
        return _route_completion(payload, streaming, ns, _rate_retry=True,
                                 _session_id=_session_id, _sticky=_sticky)

    if pinned:
        msg = f"Pinned model '{original_model}' could not be served"
    else:
        msg = "All providers exhausted"
    return ("error", {"error": {"message": msg, "type": "router_error"}}, 503)
def _log_completion(token: str, endpoint: str, payload: dict, result: tuple, elapsed: float) -> dict | None:
    """Append one entry to the request ring buffer. Returns the entry (for later
    stream-token patching) or None if logging is disabled / failed. Never raises."""
    try:
        messages = payload.get("messages", [])
        is_cache = getattr(_req_ctx, "cache_hit", False)
        attempts = getattr(_req_ctx, "attempts", 0)

        if result[0] == "json":
            status = "cache_hit" if is_cache else "success"
            usage  = result[1].get("usage", {}) if isinstance(result[1], dict) else {}
            ptok   = usage.get("prompt_tokens")
            ctok   = usage.get("completion_tokens")
        elif result[0] == "stream":
            status = "success"
            ptok   = ctok = None
        else:
            status = "error"
            ptok   = ctok = None

        _ctx_model = getattr(_req_ctx, "model", None)
        _ctx_provider = getattr(_req_ctx, "provider", None)
        _last_model = getattr(_req_ctx, "last_tried_model", None)
        _last_provider = getattr(_req_ctx, "last_tried_provider", None)
        _payload_model = payload.get("model")
        # Prefer the upstream model that succeeded, else the last candidate we
        # tried. Never surface the client placeholder ROUTER_MODEL as if it were
        # an upstream model (exhausted requests used to show hermes-router→hermes-router).
        if is_cache:
            _logged_provider = "cache"
            _logged_model = _ctx_model or _last_model
        else:
            _logged_provider = _ctx_provider or _last_provider
            _logged_model = _ctx_model or _last_model
        if not _logged_model and _payload_model:
            _pm = str(_payload_model)
            if _pm not in ("", ROUTER_MODEL, "auto") and not _pm.endswith(":fast"):
                _logged_model = _payload_model

        _cascade = getattr(_req_ctx, "cascade", None)
        if isinstance(_cascade, CascadeTrail):
            _fields = _cascade.as_log_fields()
        else:
            _failed = max(0, attempts - 1)
            _fields = {"cascade": [], "failed": _failed, "skipped": 0, "cascades": _failed}

        entry = {
            "ts":               time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "endpoint":         endpoint,
            "caller":           token[-6:] if token else "anon",
            "streaming":        bool(payload.get("stream", False)),
            "complexity":       classify_complexity(messages),
            "est_tokens":       _estimated_tokens(messages),
            "provider":         _logged_provider,
            "model":            _logged_model,
            "latency_ms":       round(elapsed * 1000),
            "cascades":         _fields["cascades"],
            "failed":           _fields["failed"],
            "skipped":          _fields["skipped"],
            "cascade":          _fields["cascade"],
            "status":           status,
            "prompt_tokens":    ptok,
            "completion_tokens": ctok,
        }
        return request_log.append(entry)
    except Exception:
        return None   # logging must never break the response path


@app.route("/v1/chat/completions", methods=["POST"])
def chat():
    err = _auth_check()
    if err:
        return err

    payload = request.get_json(force=True, silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": {"message": "request body must be a JSON object",
                                  "type": "invalid_request_error"}}), 400

    token  = _caller_token()
    gate   = _admit_request(token)
    if gate:
        return gate

    t_start = time.time()
    _session_id, _sticky = _sticky_for_request(request.headers, payload)
    result  = _route_completion(payload, payload.get("stream", False), _cache_ns(),
                                _session_id=_session_id, _sticky=_sticky)
    _record_request_tokens(token, payload, result)

    entry = _log_completion(token, "chat", payload, result, time.time() - t_start)

    if result[0] == "json":
        return jsonify(result[1]), 200
    if result[0] == "stream":
        _, gen, name = result

        def _finalize():
            try:
                yield from gen
            finally:
                _patch_stream_log_tokens(entry, gen)

        return Response(stream_with_context(_finalize()), content_type="text/event-stream",
                        headers={"X-Provider": name})
    return jsonify(result[1]), result[2]


@app.route("/v1/embeddings", methods=["POST"])
def embeddings():
    err = _auth_check()
    if err:
        return err

    payload = request.get_json(force=True, silent=True)
    if not isinstance(payload, dict) or "input" not in payload:
        return jsonify({"error": {"message": "request body must be a JSON object with an 'input' field",
                                  "type": "invalid_request_error"}}), 400

    token  = _caller_token()
    gate   = _admit_request(token)
    if gate:
        return gate

    ordered = _embed_ordered()
    if not ordered:
        return jsonify({"error": {"message": "no embedding-capable providers configured "
                                             "(set e.g. GEMINI_API_KEYS or MISTRAL_API_KEYS)",
                                  "type": "router_error"}}), 503

    # Embeddings are deterministic — identical input is a perfect cache hit.
    ns      = _cache_ns()
    t_start = time.time()
    trail   = CascadeTrail()
    cached  = cache.get(payload, ns)
    if cached is not None:
        log.info("↩ cache hit (embeddings)")
        request_log.append({
            "ts":         time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "endpoint":   "embeddings",
            "caller":     token[-6:] if token else "anon",
            "streaming":  False,
            "complexity": None,
            "est_tokens": 0,
            "provider":   "cache",
            "model":      payload.get("model"),
            "latency_ms": round((time.time() - t_start) * 1000),
            "status":     "cache_hit",
            "prompt_tokens": None,
            "completion_tokens": None,
            **trail.as_log_fields(),
        })
        return jsonify(cached)

    any_closed = any(not stats.breaker_open(p["name"]) for p in ordered)

    for provider in ordered:
        name = provider["name"]
        if any_closed and stats.breaker_open(name):
            log.info(f"⨂ skipping {name} embeddings (circuit open)")
            trail.skip(name, provider.get("embed_model") or "", "circuit_open")
            continue

        em = provider["embed_model"]
        attempts = pool.key_count(name, em) or 1
        for _ in range(attempts):
            key = pool.get_key(name, em)
            if not key:
                log.warning(f"All {name} keys cooling — skipping provider")
                trail.note(name, em, "skipped", "keys_cooling")
                break

            log.info(f"→ Trying {name} embeddings ({em}) ...{key[-6:]}")
            _inp = payload.get("input", "")
            _est_tokens = max(1.0, (len(_inp) if isinstance(_inp, str)
                                    else sum(len(str(x)) for x in _inp)) / 4)
            _current_headroom = rate_limiter.headroom(name, key, em)
            if _current_headroom < RATE_HEADROOM_THRESHOLD:
                log.debug(f"  {name}/{em} thin headroom ({_current_headroom:.1%}) — attempting")
            _rl_ok, _rl_wait = rate_limiter.check_and_consume(
                name, key, em, req_count=1.0, token_count=_est_tokens)
            if not _rl_ok:
                if 0 < _rl_wait < RATE_ADMIT_WAIT_S:
                    log.debug(f"  {name}/{em} thin bucket — waiting {_rl_wait*1000:.0f}ms")
                    time.sleep(_rl_wait)
                    _rl_ok, _rl_wait = rate_limiter.check_and_consume(
                        name, key, em, req_count=1.0, token_count=_est_tokens)
                if not _rl_ok:
                    _rl_ok, _rl_wait = rate_limiter.check_and_consume(
                        name, key, em, req_count=1.0, token_count=_est_tokens,
                        force=True)
                if not _rl_ok:
                    log.info(f"  {name}/{em} rate hold ({_rl_wait:.1f}s) — trying next")
                    trail.note(name, em, "skipped", "rate_hold")
                    continue
            _rl_t0 = time.time()
            t0   = _rl_t0
            resp = forward_embeddings(provider, key, payload)
            elapsed = time.time() - t0

            def _rl_release():
                rate_limiter.release_reservation(name, key, em, 1.0, _est_tokens)

            if resp is None:
                _rl_release()
                stats.record_error(name); stats.record_health(name, False)
                pool.mark_key_down(name, key, retry_after=30)
                trail.note(name, em, "failed", "network")
                continue
            if resp.status_code == 429:
                _rl_release()
                stats.record_error(name)
                rate_limiter.on_429(
                    name, key, em, dict(resp.headers),
                    model_headroom_before=_current_headroom,
                    observed_at=_rl_t0,
                )
                log.warning(f"  {name} embeddings 429 — rate-limit hold, trying next key")
                trail.note(name, em, "failed", "http_429")
                continue
            if resp.status_code in (400, 401, 403, 404):
                _rl_release()
                stats.record_error(name)   # request/auth/model-specific, not a health failure
                log.error(f"  {name} embeddings {resp.status_code} — skipping provider: {resp.text[:200]}")
                trail.note(name, em, "failed", http_reason(resp.status_code))
                break
            if resp.status_code >= 500:
                _rl_release()
                stats.record_error(name); stats.record_health(name, False)
                pool.mark_key_down(name, key, retry_after=15)
                trail.note(name, em, "failed", "http_5xx")
                continue
            if not (200 <= resp.status_code < 300):
                _rl_release()
                stats.record_error(name); stats.record_health(name, False)
                log.warning(f"  {name} embeddings unexpected {resp.status_code} — skipping provider")
                trail.note(name, em, "failed", http_reason(resp.status_code))
                break

            stats.record_success(name, elapsed); stats.record_health(name, True)
            log.info(f"  ✓ {name} embeddings ({elapsed*1000:.0f}ms)")
            data = resp.json()
            _actual = float((data.get("usage") or {}).get("total_tokens") or _est_tokens)
            rate_limiter.reconcile(name, key, em, _est_tokens, _actual)
            rate_limiter.update_from_headers(
                name, key, em, dict(resp.headers), observed_at=_rl_t0)
            rate_limiter.on_success(name, key, em, _actual)
            key_usage.add_tokens(token, (data.get("usage") or {}).get("total_tokens") or 0)
            _add_provider_tokens(name, data)
            cache.set(payload, data, ns)
            trail.success(name, em)
            request_log.append({
                "ts":         time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "endpoint":   "embeddings",
                "caller":     token[-6:] if token else "anon",
                "streaming":  False,
                "complexity": None,
                "est_tokens": 0,
                "provider":   name,
                "model":      em,
                "latency_ms": round((time.time() - t_start) * 1000),
                "status":     "success",
                "prompt_tokens": (data.get("usage") or {}).get("total_tokens"),
                "completion_tokens": None,
                **trail.as_log_fields(),
            })
            return jsonify(data), 200

        trail.flush()
        log.warning(f"✗ {name} embeddings exhausted — cascading")

    request_log.append({
        "ts":         time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "endpoint":   "embeddings",
        "caller":     token[-6:] if token else "anon",
        "streaming":  False,
        "complexity": None,
        "est_tokens": 0,
        "provider":   None,
        "model":      None,
        "latency_ms": round((time.time() - t_start) * 1000),
        "status":     "error",
        "prompt_tokens": None,
        "completion_tokens": None,
        **trail.as_log_fields(),
    })
    return jsonify({"error": {"message": "All embedding providers exhausted", "type": "router_error"}}), 503


# ── Feature add-ons ─────────────────────────────────────────────────────────────
# hermes-router separates CORE features (always on — the router's identity) from
# ADD-ONS (opt-in behaviors, each backed by an env var or some config). The
# registry below is the single source of truth: it powers the `features` block in
# /v1/status, the `hr features` CLI (which reads it and toggles the `env` flag in
# .env), and the dashboard. Env vars remain authoritative — this is just a unified
# view + friendly toggle, so behavior is unchanged whether or not you use it.
CORE_FEATURES = [
    "auth", "credential_pool", "key_rotation", "failover", "circuit_breaker",
    "smart_routing", "protocol_translation", "feature_probing", "token_counting",
    "request_guardrails", "usage_cost_tracking",
]

def _features_snapshot() -> dict:
    """Live core/add-on categorization for /v1/status and `hr features`.
    `enabled` is computed from the already-parsed config (env vars stay the source
    of truth). Flag add-ons carry env/on/off so the CLI can toggle them; config
    add-ons carry a `manage` command instead."""
    has_local = any(p["name"] == "local" for p in PROVIDERS)
    addons = [
        {"name": "response_cache", "title": "Response cache", "kind": "flag",
         "enabled": CACHE_TTL > 0, "env": "CACHE_TTL_SECONDS", "on": "300", "off": "0",
         "desc": "Serve identical requests from an in-memory TTL+LRU cache."},
        {"name": "semantic_cache", "title": "Semantic cache", "kind": "flag",
         "enabled": SEMANTIC_CACHE, "env": "SEMANTIC_CACHE", "on": "1", "off": "0",
         "desc": "Also serve cached answers for similar (not just identical) prompts."},
        {"name": "fast_routing", "title": "Fast selection", "kind": "flag",
         "enabled": FAST_ROUTE_TOKENS > 0, "env": "FAST_ROUTE_THRESHOLD", "on": "200", "off": "0",
         "desc": "Short requests prefer low-latency providers on ties."},
        {"name": "model_discovery", "title": "Model discovery", "kind": "flag",
         "enabled": AUTO_DISCOVER_MODELS, "env": "AUTO_DISCOVER_MODELS", "on": "1", "off": "0",
         "desc": "Refresh configured provider model lists from /models at startup, bounded by AUTO_DISCOVER_MODEL_LIMIT."},
        {"name": "filter_specialized_models", "title": "Filter specialized models", "kind": "flag",
         "enabled": FILTER_SPECIALIZED_MODELS, "env": "FILTER_SPECIALIZED_MODELS", "on": "1", "off": "0",
         "desc": "When model discovery is on, drop TTS / STT / image-gen / OCR / video / embedding / moderation / rerank IDs from discovered catalogs so they never enter the chat catalog."},
        {"name": "token_caps", "title": "Adaptive token caps", "kind": "flag",
         "enabled": TOKEN_CAPS_ENABLED, "env": "TOKEN_CAPS", "on": "1", "off": "0",
         "desc": "Track per-model input/output ceilings from /models metadata and classified 413/token-limit 400s."},
        {"name": "key_budgets", "title": "Per-access-key budgets", "kind": "config",
         "enabled": KEY_LIMITS_ON, "manage": "hr limit set <key> --rpm/--req-day/--tokens-day/--cost-day",
         "desc": "Per-access-key RPM / daily request / token / cost ceilings (operator budgets, not upstream rate limits)."},
        {"name": "local_model", "title": "Local model provider", "kind": "config",
         "enabled": has_local, "manage": "hr model set local <model>",
         "desc": "Route to a model on your own machine (Ollama / LM Studio / llama.cpp)."},
        {"name": "request_log", "title": "Request log", "kind": "flag",
         "enabled": request_log.enabled, "env": "REQUEST_LOG_SIZE", "on": "500", "off": "0",
         "desc": f"In-memory ring buffer of the last {REQUEST_LOG_SIZE} requests. No disk writes. Query via GET /v1/logs."},
        {"name": "dashboard", "title": "Monitoring dashboard", "kind": "builtin",
         "enabled": True,
         "desc": "Browser-based live dashboard at /dashboard — provider health, request log, cache stats, key usage."},
    ]
    return {"core": CORE_FEATURES, "addons": addons}


# ── Config-write endpoints (web dashboard) ──────────────────────────────────────
# These back the dashboard's "Add key" / "Model" / add-on toggle forms. Same
# proxy-key auth as every other endpoint — whoever can view /v1/status can also
# change config, matching the existing CLI's trust model (one operator key).
# Every write is a plain, auditable file edit (.env or auth.json), identical to
# what `hr auth add` / `hr model set` / `hr features enable` already produce —
# nothing here is a new mechanism, just an HTTP front-end for the same files.
# Changes take effect after a restart; the dashboard prompts for one via
# POST /v1/config/restart.

@app.route("/v1/config/providers")
def config_providers():
    """List of providers the dashboard can build add-key / set-model forms for,
    plus which ones accept a plain key vs. model-only (codex/local)."""
    err = _auth_check()
    if err:
        return err
    return jsonify({
        "key_settable": KEY_SETTABLE_PROVIDERS,
        "model_settable": list(PROVIDER_MODEL_ENV.keys()),
        # Live key count per currently-configured provider, e.g. {"gemini": 6} —
        # informational context for the Access Keys page's provider picker, not
        # an enforced quota split (see monitoring docs on provider scoping).
        "key_counts": {p["name"]: len(p.get("keys", [])) for p in PROVIDERS},
    })


@app.route("/v1/config/keys/<provider>", methods=["POST"])
def config_add_key(provider):
    """Add one API key for a provider to auth.json. Body: {"key": "..."}"""
    err = _auth_check()
    if err:
        return err
    if provider not in KEY_SETTABLE_PROVIDERS:
        return jsonify({"error": {"message": f"unknown or non-key provider: {provider}",
                                  "type": "invalid_request_error"}}), 400
    body = request.get_json(force=True, silent=True) or {}
    key = (body.get("key") or "").strip()
    if not key:
        return jsonify({"error": {"message": "missing 'key'", "type": "invalid_request_error"}}), 400
    if "\n" in key or "\r" in key:
        return jsonify({"error": {"message": "key must not contain newlines", "type": "invalid_request_error"}}), 400
    added, total = _auth_json_add_key(provider, key)
    return jsonify({"provider": provider, "added": added, "total_keys": total,
                    "duplicate": not added, "restart_required": added})


@app.route("/v1/config/model/<provider>", methods=["POST"])
def config_model(provider):
    """POST {"model": "m1,m2"} to override a provider's model(s)."""
    err = _auth_check()
    if err:
        return err
    env_var = PROVIDER_MODEL_ENV.get(provider)
    if not env_var:
        return jsonify({"error": {"message": f"unknown provider: {provider}",
                                  "type": "invalid_request_error"}}), 400

    body = request.get_json(force=True, silent=True) or {}
    model = (body.get("model") or "").strip()
    if not model:
        return jsonify({"error": {"message": "missing 'model'", "type": "invalid_request_error"}}), 400
    if any(c in model for c in "\n\r"):
        return jsonify({"error": {"message": "model must not contain newlines", "type": "invalid_request_error"}}), 400
    if not re.fullmatch(r"[A-Za-z0-9._\-:/, ]+", model):
        return jsonify({"error": {"message": "model contains unsupported characters",
                                  "type": "invalid_request_error"}}), 400
    _env_write_line(env_var, model)
    return jsonify({"provider": provider, "model": model, "restart_required": True})


@app.route("/v1/config/features/<name>", methods=["POST"])
def config_feature(name):
    """Toggle a flag-kind add-on on/off. Body: {"enabled": true|false}. Config-kind
    add-ons (per-key budgets, local model) aren't simple flag writes — use their
    own command (`hr limit`, `hr model set local ...`), matching `hr features`."""
    err = _auth_check()
    if err:
        return err
    addon = next((a for a in _features_snapshot()["addons"] if a["name"] == name), None)
    if not addon:
        return jsonify({"error": {"message": f"unknown add-on: {name}", "type": "invalid_request_error"}}), 404
    if addon.get("kind") != "flag":
        return jsonify({"error": {"message": f"'{name}' isn't a simple toggle — manage it with: {addon.get('manage', '(see docs)')}",
                                  "type": "invalid_request_error"}}), 400

    body = request.get_json(force=True, silent=True) or {}
    enabled = bool(body.get("enabled"))
    _env_write_line(addon["env"], addon["on"] if enabled else addon["off"])
    return jsonify({"name": name, "enabled": enabled, "restart_required": True})


@app.route("/v1/config/excluded-models")
def config_excluded_models():
    """List models blocked via {PROVIDER}_EXCLUDE_MODELS (env + dashboard)."""
    err = _auth_check()
    if err:
        return err
    return jsonify({"excluded": _all_excluded_models()})


@app.route("/v1/config/exclude-model", methods=["POST"])
def config_exclude_model():
    """Block or unblock a provider model. Body: {provider, model, blocked}.

    Writes {PROVIDER}_EXCLUDE_MODELS and updates the live roster immediately
    (no restart required).
    """
    err = _auth_check()
    if err:
        return err
    body = request.get_json(force=True, silent=True) or {}
    provider = (body.get("provider") or "").strip()
    model = (body.get("model") or "").strip()
    if "blocked" not in body:
        return jsonify({"error": {"message": "missing 'blocked'",
                                  "type": "invalid_request_error"}}), 400
    blocked = bool(body.get("blocked"))
    if not provider:
        return jsonify({"error": {"message": "missing 'provider'",
                                  "type": "invalid_request_error"}}), 400
    if provider not in PROVIDER_MODEL_ENV:
        return jsonify({"error": {"message": f"unknown provider: {provider}",
                                  "type": "invalid_request_error"}}), 400
    if not model:
        return jsonify({"error": {"message": "missing 'model'",
                                  "type": "invalid_request_error"}}), 400
    if any(c in model for c in "\n\r"):
        return jsonify({"error": {"message": "model must not contain newlines",
                                  "type": "invalid_request_error"}}), 400
    if not re.fullmatch(r"[A-Za-z0-9._\-:/]+", model):
        return jsonify({"error": {"message": "model contains unsupported characters",
                                  "type": "invalid_request_error"}}), 400
    try:
        excluded = _set_model_excluded(provider, model, blocked)
    except OSError as e:
        log.error(f"exclude-model write failed: {e}")
        return jsonify({"error": {"message": f"failed to write .env: {e}",
                                  "type": "server_error"}}), 500
    return jsonify({
        "provider": provider,
        "model": model,
        "blocked": blocked,
        "excluded": excluded,
    })


@app.route("/v1/config/gi-override", methods=["PUT"])
def config_gi_override_put():
    """Set a manual GI override. Body: {provider, model, gi} with gi in 0–100."""
    err = _auth_check()
    if err:
        return err
    body = request.get_json(force=True, silent=True) or {}
    provider = (body.get("provider") or "").strip()
    model = (body.get("model") or "").strip()
    if not provider:
        return jsonify({"error": {"message": "missing 'provider'",
                                  "type": "invalid_request_error"}}), 400
    if not model:
        return jsonify({"error": {"message": "missing 'model'",
                                  "type": "invalid_request_error"}}), 400
    if "gi" not in body:
        return jsonify({"error": {"message": "missing 'gi'",
                                  "type": "invalid_request_error"}}), 400
    try:
        score = gi_ranking.set_override(provider, model, body["gi"])
    except (TypeError, ValueError) as e:
        return jsonify({"error": {"message": str(e),
                                  "type": "invalid_request_error"}}), 400
    except OSError as e:
        return jsonify({"error": {"message": f"failed to persist override: {e}",
                                  "type": "server_error"}}), 500
    return jsonify({
        "provider": provider, "model": model, "gi": score, "gi_source": "override",
    })


@app.route("/v1/config/gi-override", methods=["DELETE"])
def config_gi_override_delete():
    """Clear a manual GI override. Body: {provider, model}."""
    err = _auth_check()
    if err:
        return err
    body = request.get_json(force=True, silent=True) or {}
    provider = (body.get("provider") or "").strip()
    model = (body.get("model") or "").strip()
    if not provider or not model:
        return jsonify({"error": {"message": "provider and model are required",
                                  "type": "invalid_request_error"}}), 400
    try:
        cleared = gi_ranking.clear_override(provider, model)
    except OSError as e:
        return jsonify({"error": {"message": f"failed to persist override: {e}",
                                  "type": "server_error"}}), 500
    gi, src = gi_ranking.resolve_gi(provider, model)
    return jsonify({
        "provider": provider, "model": model, "cleared": cleared,
        "gi": gi, "gi_source": src,
    })


@app.route("/v1/config/restart", methods=["POST"])
def config_restart():
    """Restart the router so config changes take effect. Responds immediately;
    the actual restart happens ~1s later so this response reaches the client."""
    err = _auth_check()
    if err:
        return err
    _trigger_restart()
    return jsonify({"status": "restarting", "message": "Router restarting — this page will reconnect shortly."})


def _parse_limit_fields(body: dict) -> tuple[dict, str | None]:
    """Validate rpm/req_per_day/tokens_per_day/cost_per_day from a request body.
    Returns (limits, error) — only includes fields the caller actually sent, so a
    partial update doesn't zero out fields left unset (0 itself is a valid,
    meaningful 'unlimited' value and is kept distinct from 'not provided')."""
    out: dict = {}
    for f in ("rpm", "req_per_day", "tokens_per_day"):
        if f in body and body[f] not in (None, ""):
            try:
                v = int(body[f])
            except (TypeError, ValueError):
                return {}, f"'{f}' must be a whole number"
            if v < 0:
                return {}, f"'{f}' must not be negative"
            out[f] = v
    if "cost_per_day" in body and body["cost_per_day"] not in (None, ""):
        try:
            v = float(body["cost_per_day"])
        except (TypeError, ValueError):
            return {}, "'cost_per_day' must be a number"
        if v < 0:
            return {}, "'cost_per_day' must not be negative"
        out["cost_per_day"] = v
    return out, None


def _parse_allowed_providers(body: dict) -> tuple[dict, str | None]:
    """Validate 'allowed_providers' from a request body. Returns a patch dict
    (empty if the field wasn't sent at all — leaves any existing value untouched
    on an update) and an error message or None. An explicit empty list / null
    means 'unrestricted', matching KEY_PROVIDER_SCOPE's loader semantics."""
    if "allowed_providers" not in body:
        return {}, None
    val = body["allowed_providers"] or []
    if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
        return {}, "'allowed_providers' must be a list of provider names"
    known = set(PROVIDER_MODEL_ENV.keys())
    unknown = [x for x in val if x not in known]
    if unknown:
        return {}, f"unknown provider(s): {', '.join(unknown)}"
    return {"allowed_providers": val}, None


@app.route("/v1/config/proxy-keys")
def config_list_proxy_keys():
    """List every proxy (access) key — the credential CALLERS use to authenticate
    to this router, distinct from the provider keys under /v1/config/keys. Shows
    tail, optional name, limits, and live usage. Reads fresh from .env/auth.json
    (not the process's own stale PROXY_API_KEYS) so a just-created or just-revoked
    key shows immediately, flagged pending until a restart actually applies it."""
    err = _auth_check()
    if err:
        return err
    live_keys = _read_proxy_api_keys_live()
    meta = _read_proxy_keys_meta()
    active_now = set(PROXY_API_KEYS)
    out = []
    for k in live_keys:
        spec = meta.get(k, {})
        out.append({
            "key_tail": k[-6:],
            "name": spec.get("name", ""),
            "limits": {
                "rpm":            spec.get("rpm", 0) or 0,
                "req_per_day":    spec.get("req_per_day", 0) or 0,
                "tokens_per_day": spec.get("tokens_per_day", 0) or 0,
                "cost_per_day":   spec.get("cost_per_day", 0) or 0,
            },
            "allowed_providers": spec.get("allowed_providers") or [],
            "usage": key_usage.snapshot(k),
            "pending_restart": k not in active_now,
        })
    return jsonify({"keys": out})


@app.route("/v1/config/proxy-keys", methods=["POST"])
def config_create_proxy_key():
    """Mint a new proxy key for a teammate/other app to call the router with.
    Body: {"name": "...", "rpm": N, "req_per_day": N, "tokens_per_day": N,
    "cost_per_day": N, "allowed_providers": ["gemini",...]} — all optional;
    omitted limits fall back to the PROXY_LIMIT_* env defaults (0/unset =
    unlimited); an empty/omitted allowed_providers means unrestricted (can use
    any configured provider). Returns the plaintext key ONCE — like every other
    key in this codebase, only its tail is shown again."""
    err = _auth_check()
    if err:
        return err
    body = request.get_json(force=True, silent=True) or {}
    name = str(body.get("name") or "").strip()[:80]
    if "\n" in name or "\r" in name:
        return jsonify({"error": {"message": "name must not contain newlines", "type": "invalid_request_error"}}), 400
    limits, verr = _parse_limit_fields(body)
    if verr:
        return jsonify({"error": {"message": verr, "type": "invalid_request_error"}}), 400
    scope, serr = _parse_allowed_providers(body)
    if serr:
        return jsonify({"error": {"message": serr, "type": "invalid_request_error"}}), 400

    live_keys = _read_proxy_api_keys_live()
    new_key = _generate_proxy_key()
    while new_key in live_keys:   # astronomically unlikely; stay correct anyway
        new_key = _generate_proxy_key()
    live_keys.append(new_key)
    _env_write_line("PROXY_API_KEYS", ",".join(live_keys))

    patch = {**limits, **scope}
    if name:
        patch["name"] = name
    if patch:
        _write_proxy_key_meta(new_key, patch)

    return jsonify({"key": new_key, "key_tail": new_key[-6:], "name": name,
                    "limits": limits, "allowed_providers": scope.get("allowed_providers", []),
                    "restart_required": True})


@app.route("/v1/config/proxy-keys/<tail>", methods=["POST"])
def config_update_proxy_key(tail):
    """Update the name/limits of an existing proxy key, found by its last-6-char
    tail. Body: same shape as create. Only fields present in the body change —
    others keep their current value."""
    err = _auth_check()
    if err:
        return err
    live_keys = _read_proxy_api_keys_live()
    key = _resolve_proxy_key_by_tail(tail, live_keys)
    if not key:
        return jsonify({"error": {"message": f"no access key ending in '{tail}'",
                                  "type": "invalid_request_error"}}), 404

    body = request.get_json(force=True, silent=True) or {}
    limits, verr = _parse_limit_fields(body)
    if verr:
        return jsonify({"error": {"message": verr, "type": "invalid_request_error"}}), 400
    scope, serr = _parse_allowed_providers(body)
    if serr:
        return jsonify({"error": {"message": serr, "type": "invalid_request_error"}}), 400

    patch = {**limits, **scope}
    if "name" in body:
        name = str(body.get("name") or "").strip()[:80]
        if "\n" in name or "\r" in name:
            return jsonify({"error": {"message": "name must not contain newlines",
                                      "type": "invalid_request_error"}}), 400
        patch["name"] = name
    if patch:
        _write_proxy_key_meta(key, patch)
    return jsonify({"key_tail": tail, "restart_required": True})


@app.route("/v1/config/proxy-keys/<tail>", methods=["DELETE"])
def config_delete_proxy_key(tail):
    """Revoke a proxy key so it can no longer authenticate to the router.
    Refuses to remove the last remaining key — that would lock everyone out,
    including whoever is using the dashboard right now."""
    err = _auth_check()
    if err:
        return err
    live_keys = _read_proxy_api_keys_live()
    key = _resolve_proxy_key_by_tail(tail, live_keys)
    if not key:
        return jsonify({"error": {"message": f"no access key ending in '{tail}'",
                                  "type": "invalid_request_error"}}), 404
    if len(live_keys) <= 1:
        return jsonify({"error": {"message": "can't delete the last access key — you'd lock yourself out",
                                  "type": "invalid_request_error"}}), 400
    live_keys.remove(key)
    _env_write_line("PROXY_API_KEYS", ",".join(live_keys))
    _delete_proxy_key_meta(key)
    return jsonify({"key_tail": tail, "revoked": True, "restart_required": True})


@app.route("/v1/status")
def status():
    """Show key cooldown state, latency/error stats, and cache metrics."""
    err = _auth_check()
    if err:
        return err

    now  = time.time()
    keys = {}
    with pool.lock:
        for name, model_pools in pool.pools.items():
            # Representative key status from the provider's primary model bucket
            # (insertion order → models[0]); per-model buckets share the same keys.
            primary = next(iter(model_pools), None)
            entries = model_pools.get(primary, []) if primary else []
            keys[name] = [
                {
                    "key_tail": e["key"][-6:],
                    "status":   "cooling" if e["cool_until"] > now else "ready",
                    "ready_in": max(0, round(e["cool_until"] - now)),
                    "requests": pool.key_requests_for(name, e["key"]),
                }
                for e in entries
            ]

    provider_stats = {}
    for p in PROVIDERS:
        entry = {
            "keys":  keys.get(p["name"], []),
            "stats": stats.summary(p["name"]),
            "breaker": stats.breaker_status(p["name"]),
            "tokens": _provider_tokens.get(p["name"], 0),
            "cost_usd": round(_provider_cost.get(p["name"], 0.0), 6),
        }
        # Surface routing signals (GI + probe latency + model)
        # so dashboards can show them. Added only when known, so un-probed
        # providers still fall back to the dashboard's "?"/"—" placeholders.
        st = _provider_state.get(p["name"], {})
        _models = p.get("models") or [p.get("model", "")]
        if _models and _models[0]:
            _gi, _gi_src = gi_ranking.resolve_gi(p["name"], _models[0])
            entry["gi"] = _gi
            entry["gi_source"] = _gi_src
        if st.get("latency_ms"):
            entry["latency_ms"] = st["latency_ms"]
        if st.get("model"):
            entry["model"] = st["model"]
        if p.get("models"):
            entry["models"] = p["models"]
            # Per-model GI + tool/reasoning support, so dashboards can show why
            # a non-primary model gets picked for hard turns.
            entry["model_caps"] = [
                {"model": m, **_model_caps(p["name"], m)} for m in p["models"]]
        if "available" in st:
            entry["available"] = st["available"]
        if "supports_tools" in st:
            entry["supports_tools"] = st["supports_tools"]
        if "reasoning" in st:
            entry["reasoning"] = st["reasoning"]
        if p.get("skip_if_tokens_over"):
            entry["skip_if_tokens_over"] = p["skip_if_tokens_over"]
        if p.get("max_output_tokens"):
            entry["max_output_tokens"] = p["max_output_tokens"]
        _tc = {}
        for _m in _models:
            _snap = token_caps.snapshot(p["name"], _m)
            if _snap:
                _tc[_m] = _snap
        if _tc:
            entry["token_caps"] = _tc
        _rl = {}
        for _m in _models:
            _k = pool.peek_key(p["name"], _m)
            if _k:
                _rl[_m] = rate_limiter.snapshot(p["name"], _k, _m)
        entry["rate_limits"] = _rl
        provider_stats[p["name"]] = entry

    return jsonify({
        "providers": provider_stats,
        "cache": {
            "enabled":    CACHE_TTL > 0,
            "ttl_s":      CACHE_TTL,
            "size":       cache.size,
            "max_size":   CACHE_MAX_SIZE,
            "hits":       cache.hits,
            "misses":     cache.misses,
            "hit_rate":   cache.hit_rate,
            "semantic": {
                "enabled":   SEMANTIC_CACHE,
                "threshold": SEMANTIC_THRESHOLD,
                "hits":      cache.semantic_hits,
            },
        },
        "fast_routing": {
            "enabled":         FAST_ROUTE_TOKENS > 0,
            "threshold_tokens": FAST_ROUTE_TOKENS,
            "fast_providers":  sorted(_FAST_PROVIDERS),
        },
        "rotation": {
            "mode": "sticky-key",
        },
        "limits": {
            "enabled": KEY_LIMITS_ON,
            "keys": ([
                {"key_tail": k[-6:], "limits": KEY_LIMITS[k], "usage": key_usage.snapshot(k)}
                for k in PROXY_API_KEYS
            ] if KEY_LIMITS_ON else []),
        },
        "circuit_breaker": {
            "window":      BREAKER_WINDOW,
            "min_samples": BREAKER_MIN_SAMPLES,
            "error_rate":  BREAKER_ERROR_RATE,
            "cooldown_s":  BREAKER_COOLDOWN,
        },
        "features": _features_snapshot(),
    })


@app.route("/v1/rate-limits")
def rate_limits_list():
    err = _auth_check()
    if err:
        return err
    raw = (request.args.get("include_orphans") or "0").strip().lower()
    include = raw in ("1", "true", "yes")
    groups = rate_limiter.list_groups(
        include_orphans=include,
        configured_ids=_configured_rate_group_ids(),
    )
    return jsonify({"generated_at": time.time(), "groups": groups})


@app.route("/v1/rate-limits/clear", methods=["POST"])
def rate_limits_clear():
    err = _auth_check()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    gid = body.get("id")
    if not gid or not isinstance(gid, str):
        return jsonify({"error": "id required"}), 400
    if not rate_limiter.clear_group(gid):
        return jsonify({"error": "unknown group"}), 404
    return jsonify({"ok": True})


@app.route("/v1/usage")
def usage():
    """Usage analytics: per-provider request/error/token counts, per-key request
    and token totals (key tails only — never full keys), and cache stats."""
    err = _auth_check()
    if err:
        return err

    providers = {}
    for p in PROVIDERS:
        s = stats.summary(p["name"])
        providers[p["name"]] = {
            "requests": s["total_requests"],
            "errors":   s["errors"],
            "tokens":   _provider_tokens.get(p["name"], 0),
            "cost":     _cost_obj(_provider_cost.get(p["name"], 0.0)),
        }
    keys = [{"key_tail": k[-6:], **key_usage.snapshot(k)} for k in PROXY_API_KEYS]

    return jsonify({
        "uptime_s":  round(time.time() - START_TIME),
        "totals":    {"tokens": sum(_provider_tokens.values()),
                      "cost":   _cost_obj(sum(_provider_cost.values()))},
        "providers": providers,
        "keys":      keys,
        "cache": {
            "hits":          cache.hits,
            "misses":        cache.misses,
            "hit_rate":      cache.hit_rate,
            "semantic_hits": cache.semantic_hits,
        },
    })


@app.route("/v1/logs")
def logs():
    """In-memory request log — last REQUEST_LOG_SIZE entries, most recent first.

    Never writes to disk. Returns an empty list when REQUEST_LOG_SIZE=0.

    Query params (all optional):
      limit=N          Max entries to return (default 100, capped at REQUEST_LOG_SIZE)
      provider=name    Filter by provider name (e.g. "gemini", "anthropic", "cache")
      status=s         Filter by status: success | error | cache_hit
      endpoint=e       Filter by endpoint: chat | messages | embeddings
    """
    err = _auth_check()
    if err:
        return err

    try:
        limit = min(int(request.args.get("limit", 100)), max(1, REQUEST_LOG_SIZE))
    except (TypeError, ValueError):
        limit = 100
    provider = request.args.get("provider") or None
    status   = request.args.get("status")   or None
    endpoint = request.args.get("endpoint") or None

    valid_statuses  = {"success", "error", "cache_hit"}
    valid_endpoints = {"chat", "messages", "embeddings"}
    if status and status not in valid_statuses:
        return jsonify({"error": {"message": f"status must be one of {sorted(valid_statuses)}",
                                  "type": "invalid_request_error"}}), 400
    if endpoint and endpoint not in valid_endpoints:
        return jsonify({"error": {"message": f"endpoint must be one of {sorted(valid_endpoints)}",
                                  "type": "invalid_request_error"}}), 400

    entries = request_log.snapshot(limit=limit, provider=provider,
                                   status=status, endpoint=endpoint)
    return jsonify({
        "buffer_size": REQUEST_LOG_SIZE,
        "stored":      request_log.size,
        "returned":    len(entries),
        "entries":     entries,
    })


if __name__ == "__main__":
    log.info(f"hermes-router starting on {HOST}:{PORT}")
    log.info(f"Providers: {[p['name'] for p in PROVIDERS]}")
    _embed = {p["name"]: p["embed_model"] for p in PROVIDERS if p.get("embed_model")}
    log.info(f"Embeddings (/v1/embeddings): {_embed if _embed else 'no embed-capable providers'}")
    log.info(f"Cache: {'enabled' if CACHE_TTL > 0 else 'disabled'} (TTL={CACHE_TTL}s, max={CACHE_MAX_SIZE})")
    log.info(f"Fast routing: {'enabled' if FAST_ROUTE_TOKENS > 0 else 'disabled'} (threshold={FAST_ROUTE_TOKENS} tokens)")
    log.info("Key selection: key affinity")
    log.info(f"Dashboard: http://{'localhost' if HOST in ('0.0.0.0','') else HOST}:{PORT}/dashboard")
    _skips = {p["name"]: p["skip_if_tokens_over"] for p in PROVIDERS if p.get("skip_if_tokens_over")}
    if _skips:
        log.info(f"Large-payload skip ceilings: {_skips}")
    try:
        from waitress import serve
        log.info("Serving with waitress (production WSGI)")
        serve(app, host=HOST, port=PORT, threads=int(os.environ.get("WORKER_THREADS", 16)))
    except ImportError:
        log.warning("waitress not installed — falling back to Flask dev server")
        app.run(host=HOST, port=PORT, threaded=True)
