"""
Adaptive multi-window token bucket rate limiter for hermes-router.
"""
from __future__ import annotations
import csv
import os
import time
import threading
import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default) or default)
    except (TypeError, ValueError):
        return default

def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default) or default)
    except (TypeError, ValueError):
        return default

def _truthy_env(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes")

CSV_COLUMNS = [
    "datetime", "provider", "key_hint", "model", "scope", "bucket",
    "event", "reason", "cap", "old_cap", "tokens", "used", "headroom",
]

@dataclass
class CapChange:
    old_cap: float
    event: str
    reason: str

class BucketEventCsv:
    def __init__(self, enabled: bool, path: Path):
        self.enabled = enabled
        self.path = Path(path)
        self._lock = threading.Lock()
        self._header_ready = False
        self._last_warn_at = 0.0

    @staticmethod
    def from_env() -> "BucketEventCsv":
        return BucketEventCsv(
            enabled=_truthy_env("RATE_BUCKET_CSV_ENABLED"),
            path=Path(os.environ.get("RATE_BUCKET_CSV") or "./rate_bucket_events.csv"),
        )

    def record(self, *, provider: str, key_hint: str, model: str, scope: str,
               bucket: str, event: str, reason: str,
               cap: float, old_cap: float, tokens: float,
               used: float, headroom: float) -> None:
        if not self.enabled:
            return
        row = {
            "datetime": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "provider": provider,
            "key_hint": key_hint,
            "model": model or "",
            "scope": scope,
            "bucket": bucket,
            "event": event,
            "reason": reason,
            "cap": cap,
            "old_cap": old_cap,
            "tokens": tokens,
            "used": used,
            "headroom": headroom,
        }
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                new_file = (not self.path.exists()) or self.path.stat().st_size == 0
                with self.path.open("a", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
                    if new_file or not self._header_ready:
                        if new_file:
                            w.writeheader()
                        self._header_ready = True
                    w.writerow(row)
            except OSError as e:
                now = time.time()
                if now - self._last_warn_at >= 60.0:
                    log.warning(f"[rate] bucket CSV write failed: {e}")
                    self._last_warn_at = now

def _row_metrics(b: "TokenBucket") -> tuple[float, float, float]:
    tokens = float(b.tokens)
    cap = float(b.cap)
    used = max(0.0, cap - tokens)
    headroom = b.headroom()
    return tokens, used, headroom

_bucket_csv = BucketEventCsv.from_env()

RATE_SHORT_WAIT_MS        = _int_env("RATE_SHORT_WAIT_MS", 500)
RATE_HEADROOM_THRESHOLD   = _float_env("RATE_HEADROOM_THRESHOLD", 0.05)
RATE_LEARN_SUCCESS_STREAK = _int_env("RATE_LEARN_SUCCESS_STREAK", 20)
RATE_LEARN_NUDGE_PCT      = _float_env("RATE_LEARN_NUDGE_PCT", 5.0)
RATE_LEARN_CUT_FACTOR     = _float_env("RATE_LEARN_CUT_FACTOR", 0.8)
RATE_LEARN_CUT_FACTOR_PROVIDER      = _float_env("RATE_LEARN_CUT_FACTOR_PROVIDER", 0.95)
RATE_LEARN_SOFT_CUT_FACTOR          = _float_env("RATE_LEARN_SOFT_CUT_FACTOR", 0.9)
RATE_LEARN_SUCCESS_STREAK_PROVIDER  = _int_env("RATE_LEARN_SUCCESS_STREAK_PROVIDER", 10)
RATE_LEARN_NUDGE_PCT_PROVIDER       = _float_env("RATE_LEARN_NUDGE_PCT_PROVIDER", 8.0)
RATE_PROVIDER_CAP_MULTIPLIER      = _float_env("RATE_PROVIDER_CAP_MULTIPLIER", 10.0)
# Soft 429 cuts must not sink a bucket below this fraction of unmultiplied defaults.
RATE_LEARN_SOFT_FLOOR_FRAC        = _float_env("RATE_LEARN_SOFT_FLOOR_FRAC", 0.5)
# When lifting for an oversized single request, size the cap to this multiple of
# the debit so one success does not leave ~0% headroom and soft-lock the next turn.
RATE_REQUEST_BURST_FACTOR         = _float_env("RATE_REQUEST_BURST_FACTOR", 2.0)
# Before returning 503, wait up to this many seconds for the best rate-limited
# candidate to refill (agent turns often arrive before the minute window resets).
RATE_EXHAUSTED_WAIT_S             = _float_env("RATE_EXHAUSTED_WAIT_S", 60.0)
RATE_STATE_FLUSH_INTERVAL = _int_env("RATE_STATE_FLUSH_INTERVAL", 60)
# Fraction of cap for new buckets when tokens is omitted (conservative prior).
RATE_BUCKET_INITIAL_FILL          = _float_env("RATE_BUCKET_INITIAL_FILL", 0.5)

# ── Window definitions ────────────────────────────────────────────────────────

WINDOWS = {
    "M":  60.0,
    "H":  3600.0,
    "D":  86400.0,
    "W":  604800.0,
    "Mo": 2592000.0,
}

# Logical limit names → (dimension, window_key)
# dimension: "R" (requests) or "T" (tokens)
LIMIT_KEYS = {
    "RPM": ("R", "M"),  "RPH": ("R", "H"),  "RPD": ("R", "D"),
    "RPW": ("R", "W"),  "RPMo": ("R", "Mo"),
    "TPM": ("T", "M"),  "TPH": ("T", "H"),  "TPD": ("T", "D"),
    "TPW": ("T", "W"),  "TPMo": ("T", "Mo"),
}

# ── TokenBucket ───────────────────────────────────────────────────────────────

class TokenBucket:
    """One window × one dimension (requests or tokens)."""

    def __init__(self, window_seconds: float, cap: float,
                 tokens: float = None, last_refill: float = None):
        self.window_seconds        = window_seconds
        self.cap                   = float(cap)
        self._initial_cap          = float(cap)
        # Soft-cut / migration floor (unmultiplied defaults for PW groups).
        self._floor_cap            = float(cap)
        fill = max(0.0, min(1.0, RATE_BUCKET_INITIAL_FILL))
        self.tokens                = float(cap * fill if tokens is None else tokens)
        self.last_refill           = last_refill if last_refill is not None else time.time()
        self.active                = True
        self._hit_zero             = False
        self._period_consumed      = 0.0
        self._consecutive_successes = 0
        self._header_pinned        = False
        self._header_obs_at        = 0.0

    def refill(self, now: float) -> None:
        elapsed = max(0.0, now - self.last_refill)
        rate = self.cap / self.window_seconds if self.window_seconds > 0 else 0.0
        self.tokens = min(self.cap, self.tokens + elapsed * rate)
        self.last_refill = now

    def consume(self, amount: float) -> bool:
        self.refill(time.time())
        if self.tokens >= amount:
            self.tokens -= amount
            self._period_consumed += amount
            if self.tokens <= 0:
                self._hit_zero = True
            return True
        return False

    def restore(self, amount: float) -> None:
        self.tokens = min(self.cap, self.tokens + amount)

    def headroom(self) -> float:
        if self.cap <= 0:
            return 1.0
        self.refill(time.time())
        return max(0.0, min(1.0, self.tokens / self.cap))

    def time_to_refill(self, amount: float) -> float:
        self.refill(time.time())
        if self.tokens >= amount:
            return 0.0
        rate = self.cap / self.window_seconds if self.window_seconds > 0 else 0.0
        if rate <= 0:
            return float("inf")
        return (amount - self.tokens) / rate

    def on_success(self, streak: int | None = None, nudge_pct: float | None = None) -> None:
        if self._header_pinned:
            return
        need = RATE_LEARN_SUCCESS_STREAK if streak is None else streak
        pct = RATE_LEARN_NUDGE_PCT if nudge_pct is None else nudge_pct
        self._consecutive_successes += 1
        if self._consecutive_successes >= need:
            self.cap = self.cap * (1.0 + pct / 100.0)
            log.info(f"[rate] nudged cap up to {self.cap:.1f}")
            self._consecutive_successes = 0

    def on_429(self, observed_rate: float, *, soft: bool = False) -> None:
        if self._period_consumed >= 3:
            factor = RATE_LEARN_CUT_FACTOR_PROVIDER if soft else RATE_LEARN_CUT_FACTOR
            new_cap = max(1.0, observed_rate * factor)
        else:
            frac = RATE_LEARN_SOFT_CUT_FACTOR if soft else 0.5
            new_cap = max(1.0, self.cap * frac)
        if soft:
            # Floor against unmultiplied defaults so soft learning cannot death-spiral
            # into caps that permanently refuse normal (or large) requests.
            floor = max(1.0, getattr(self, "_floor_cap", self._initial_cap)
                        * RATE_LEARN_SOFT_FLOOR_FRAC)
            if new_cap < floor:
                log.info(f"[rate] soft cut floored {new_cap:.1f} → {floor:.1f}")
                new_cap = floor
        log.info(f"[rate] 429 {'soft ' if soft else ''}cut cap {self.cap:.1f} → {new_cap:.1f}")
        self.cap = new_cap
        self.tokens = 0.0
        self._consecutive_successes = 0
        self._period_consumed = 0.0
        self._header_pinned = False

    def ensure_fits(self, amount: float) -> None:
        """Raise cap when a single debit exceeds the guessed ceiling.

        TPM/RPM guesses are throughput estimates, not hard per-request limits.
        Refusing forever (need > cap) soft-locks the router into 503s for large
        agent contexts. Lift to amount × RATE_REQUEST_BURST_FACTOR so one
        success leaves headroom for the next agent turn in the same window.
        """
        burst = max(1.0, RATE_REQUEST_BURST_FACTOR)
        target = float(amount) * burst
        if target <= self.cap:
            return
        old = self.cap
        self.cap = target
        self.tokens = max(self.tokens, self.cap)
        self._floor_cap = max(getattr(self, "_floor_cap", old), float(amount))
        log.info(f"[rate] lifted cap {old:.1f} → {self.cap:.1f} "
                 f"(request {amount:.0f} × burst {burst:g})")

    def set_from_header(self, cap: float, remaining: float,
                        observed_at: float | None = None) -> bool:
        """Snap cap/remaining from upstream headers. Returns False if stale."""
        obs = time.time() if observed_at is None else float(observed_at)
        if obs < self._header_obs_at:
            return False
        self.cap    = float(cap)
        self.tokens = float(remaining)
        self._header_pinned = True
        self._header_obs_at = obs
        self._consecutive_successes = 0
        if not self.active:
            self.active = True
            log.info("[rate] bucket re-activated by header")
        return True

    def check_inactive(self, activity: float) -> None:
        """Mark this bucket inactive when it is not a binding constraint.

        Minute-window buckets (RPM/TPM) are never auto-deactivated — they are the
        primary pacing controls and must stay available for headroom + consume.
        Longer windows use *activity* (requests for R-buckets, tokens consumed for
        T-buckets) against max(10, 10% of cap).
        """
        if self.window_seconds <= 60:
            return
        threshold = max(10.0, self.cap * 0.1)
        if not self._hit_zero and activity < threshold:
            self.active = False
            log.info(f"[rate] bucket marked inactive (activity={activity:.1f}, threshold={threshold:.0f})")

    def to_dict(self) -> dict:
        return {"cap": self.cap, "tokens": self.tokens, "last_refill": self.last_refill}

    @classmethod
    def from_dict(cls, d: dict, window_seconds: float, initial_cap: float) -> "TokenBucket":
        b = cls(window_seconds=window_seconds, cap=d["cap"],
                tokens=d.get("tokens"), last_refill=d.get("last_refill"))
        b._initial_cap = initial_cap
        return b


# ── Default caps table ────────────────────────────────────────────────────────
# Only windows with known free-tier defaults are listed. Others start uncapped
# (bucket not created until a header or 429 teaches a real limit).

PROVIDER_RATE_DEFAULTS: dict[str, dict[str, float]] = {
    "groq":         {"RPM": 30,  "TPM": 6_000,  "RPD": 14_400},
    "gemini":       {"RPM": 15,  "TPM": 32_000, "RPD": 1_500},
    "openrouter":   {"RPM": 20,  "TPM": 20_000},
    "mistral":      {"RPM": 5,   "TPM": 16_000},
    "cohere":       {"RPM": 20,  "TPM": 10_000},
    "nvidia":       {"RPM": 40,  "TPM": 40_000},
    "_default":     {"RPM": 10,  "TPM": 10_000},
}


def _load_caps_for(provider_name: str) -> dict[str, float]:
    """Merge built-in defaults < env-var overrides. auth.json overrides are applied
    at BucketGroup construction time by the caller (AdaptiveRateLimiter)."""
    base = dict(PROVIDER_RATE_DEFAULTS.get(provider_name)
                or PROVIDER_RATE_DEFAULTS["_default"])
    # Env overrides: RATE_DEFAULT_GROQ_RPM, RATE_DEFAULT_GROQ_TPM, …
    prefix = f"RATE_DEFAULT_{provider_name.upper()}_"
    for key in list(os.environ):
        if key.startswith(prefix):
            limit_name = key[len(prefix):]  # e.g. "RPM"
            try:
                base[limit_name] = float(os.environ[key])
            except (TypeError, ValueError):
                pass
    return base


# ── BucketGroup ───────────────────────────────────────────────────────────────

# Maps x-ratelimit header suffixes to (dimension, window_key) for bucket lookup.
_HEADER_MAP = {
    "requests":        ("R", "M"),   # x-ratelimit-{limit,remaining}-requests → RPM
    "tokens":          ("T", "M"),   # x-ratelimit-{limit,remaining}-tokens   → TPM
    "requests-day":    ("R", "D"),
    "tokens-day":      ("T", "D"),
    "requests-hour":   ("R", "H"),
    "tokens-hour":     ("T", "H"),
}

def _dim_window_to_limit(dim: str, window: str) -> str:
    prefix = "R" if dim == "R" else "T"
    return f"{prefix}P{window}"   # e.g. "RPM", "TPD"


def _parse_retry_after(value, default: int | None = None) -> int | None:
    """Parse a Retry-After header. RFC 9110 allows delay-seconds or an HTTP date;
    fractional seconds are accepted. Returns default (or None) when unparsable."""
    try:
        return max(1, int(float(value)))
    except (TypeError, ValueError):
        return default


class BucketGroup:
    """All active token buckets for one (provider, key, model?) scope."""

    def __init__(self, provider_name: str, caps: dict[str, float] = None):
        self.provider_name = provider_name
        self.buckets: dict[str, TokenBucket] = {}
        self._requests_this_period = 0
        self.blocked_until = 0.0
        self._last_surprise_cut_at = 0.0
        caps = caps or _load_caps_for(provider_name)
        for limit_name, cap in caps.items():
            if limit_name not in LIMIT_KEYS:
                continue
            _, window_key = LIMIT_KEYS[limit_name]
            self.buckets[limit_name] = TokenBucket(
                window_seconds=WINDOWS[window_key], cap=float(cap))

    def _active(self):
        return [b for b in self.buckets.values() if b.active]

    def consume(self, req_count: float, token_count: float) -> tuple[bool, float]:
        """Check all active buckets. Consume atomically only if all pass."""
        now = time.time()
        if now < self.blocked_until:
            return False, self.blocked_until - now
        max_wait = 0.0
        checks: list[tuple[TokenBucket, float]] = []
        for name, b in self.buckets.items():
            if not b.active:
                continue
            dim, _ = LIMIT_KEYS[name]
            amount = req_count if dim == "R" else token_count
            if amount <= 0:
                continue
            b.refill(now)
            b.ensure_fits(amount)
            if b.tokens < amount:
                max_wait = max(max_wait, b.time_to_refill(amount))
            else:
                checks.append((b, amount))
        if max_wait > 0:
            return False, max_wait
        for b, amount in checks:
            b.tokens -= amount
            b._period_consumed += amount
            if b.tokens <= 0:
                b._hit_zero = True
        self._requests_this_period += int(req_count)
        return True, 0.0

    def restore_tokens(self, token_surplus: float) -> None:
        for name, b in self.buckets.items():
            if b.active and LIMIT_KEYS.get(name, ("?",))[0] == "T":
                b.restore(token_surplus)

    def restore_requests(self, req_count: float) -> None:
        for name, b in self.buckets.items():
            if b.active and LIMIT_KEYS.get(name, ("?",))[0] == "R":
                b.restore(req_count)

    def headroom(self) -> float:
        if time.time() < self.blocked_until:
            return 0.0
        active = self._active()
        if not active:
            return 1.0
        return min(b.headroom() for b in active)

    def on_success(self, token_count: float,
                   streak: int | None = None, nudge_pct: float | None = None) -> None:
        for b in self._active():
            b.on_success(streak=streak, nudge_pct=nudge_pct)

    def on_429(self, headers: dict, apply_retry_after: bool = True, *,
               apply_headers: bool = True, soft: bool = False,
               observed_at: float | None = None) -> None:
        updated = (self._apply_headers(headers, on_429=True, observed_at=observed_at)
                   if apply_headers else set())
        for name, b in self.buckets.items():
            if name in updated:
                continue
            if not b.active:
                b.active = True
                log.info(f"[rate] bucket {name} re-activated by 429")
            b.on_429(observed_rate=b._period_consumed, soft=soft)
        # Retry-After holds are model-scoped (caller passes apply_retry_after=False
        # for the provider-wide group so one model's 429 cannot block siblings).
        if not apply_retry_after:
            return
        retry = None
        if headers:
            # Case-insensitive Retry-After lookup
            for k, v in headers.items():
                if k.lower() == "retry-after":
                    retry = _parse_retry_after(v)
                    break
        if retry:
            until = time.time() + retry
            self.blocked_until = max(self.blocked_until, until)
            log.info(f"[rate] Retry-After hold until {self.blocked_until:.0f} ({retry}s)")

    def update_from_headers(self, headers: dict,
                            observed_at: float | None = None) -> None:
        self._apply_headers(headers, on_429=False, observed_at=observed_at)

    def _apply_headers(self, headers: dict, on_429: bool,
                       observed_at: float | None = None) -> set:
        """Parse x-ratelimit-* headers and update matching buckets. Returns
        set of limit names that were updated from headers."""
        updated = set()
        limits   = {}
        remaining = {}
        for raw_key, val in headers.items():
            k = raw_key.lower()
            if k.startswith("x-ratelimit-limit-"):
                suffix = k[len("x-ratelimit-limit-"):]
                try: limits[suffix] = float(val)
                except (TypeError, ValueError): pass
            elif k.startswith("x-ratelimit-remaining-"):
                suffix = k[len("x-ratelimit-remaining-"):]
                try: remaining[suffix] = float(val)
                except (TypeError, ValueError): pass
        obs = time.time() if observed_at is None else float(observed_at)
        for suffix, cap_val in limits.items():
            rem_val = remaining.get(suffix, cap_val)
            pair = _HEADER_MAP.get(suffix)
            if not pair:
                continue
            dim, window_key = pair
            limit_name = _dim_window_to_limit(dim, window_key)
            if limit_name not in self.buckets:
                _, wk = LIMIT_KEYS[limit_name]
                b = TokenBucket(
                    window_seconds=WINDOWS[wk], cap=cap_val, tokens=rem_val)
                if b.set_from_header(cap_val, rem_val, observed_at=obs):
                    self.buckets[limit_name] = b
                    log.info(f"[rate] created bucket {limit_name} from header cap={cap_val}")
                    updated.add(limit_name)
            else:
                if self.buckets[limit_name].set_from_header(
                        cap_val, rem_val, observed_at=obs):
                    updated.add(limit_name)
        return updated

    def run_inactive_check(self) -> None:
        n = self._requests_this_period
        for name, b in self.buckets.items():
            if not b.active:
                continue
            dim = LIMIT_KEYS.get(name, ("?",))[0]
            # R-buckets: request count this period. T-buckets: tokens consumed
            # (comparing request count to a token cap falsely deactivates TPM/TPD).
            activity = float(n) if dim == "R" else float(b._period_consumed)
            b.check_inactive(activity)
        self._requests_this_period = 0
        for b in self.buckets.values():
            b._period_consumed = 0.0

    def to_dict(self) -> dict:
        out = {name: b.to_dict() for name, b in self.buckets.items() if b.active}
        now = time.time()
        if self.blocked_until > now:
            out["blocked_until"] = self.blocked_until
        return out

    @classmethod
    def from_dict(cls, d: dict, provider_name: str) -> "BucketGroup":
        blocked = d.get("blocked_until")
        bucket_data = {k: v for k, v in d.items()
                       if k != "blocked_until" and isinstance(v, dict)}
        caps = {name: v["cap"] for name, v in bucket_data.items()}
        g = cls(provider_name=provider_name, caps=caps)
        for name, bdict in bucket_data.items():
            if name in g.buckets:
                fill = max(0.0, min(1.0, RATE_BUCKET_INITIAL_FILL))
                g.buckets[name].tokens = min(bdict["cap"] * fill,
                                             bdict.get("tokens", bdict["cap"]))
                g.buckets[name].last_refill = bdict.get("last_refill", time.time())
                g.buckets[name]._initial_cap = bdict["cap"]
        if isinstance(blocked, (int, float)) and blocked > time.time():
            g.blocked_until = float(blocked)
        return g


# ── AdaptiveRateLimiter ───────────────────────────────────────────────────────

class AdaptiveRateLimiter:
    """Top-level manager: one BucketGroup per (provider, key[-8:], model?)."""

    def __init__(self, state_file: Path, auth_file: Path = None):
        self.state_file = state_file
        self._lock   = threading.Lock()
        self._groups: dict[str, BucketGroup] = {}
        self._auth_rate_defaults: dict[str, dict] = {}
        if auth_file and auth_file.exists():
            try:
                doc = json.loads(auth_file.read_text())
                raw = doc.get("rate_defaults") or {}
                self._auth_rate_defaults = {
                    k: dict(v) for k, v in raw.items() if isinstance(v, dict)
                }
            except Exception as e:
                log.warning(f"[rate] could not read auth rate_defaults: {e}")

    # ── Internal helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _group_key(provider_name: str, key: str, model: str | None) -> str:
        key_suffix = (key or "")[-8:] or "unknown"
        if model:
            return f"provider:{provider_name}|key:{key_suffix}|model:{model}"
        return f"provider:{provider_name}|key:{key_suffix}"

    @staticmethod
    def parse_group_key(group_id: str) -> dict | None:
        parts = {}
        for piece in (group_id or "").split("|"):
            if ":" not in piece:
                return None
            k, v = piece.split(":", 1)
            parts[k] = v
        if "provider" not in parts or "key" not in parts:
            return None
        return {
            "provider": parts["provider"],
            "key_hint": parts["key"],
            "model": parts.get("model"),
        }

    def _caps_for(self, provider_name: str, *, provider_wide: bool = False) -> dict[str, float]:
        caps = _load_caps_for(provider_name)
        overrides = self._auth_rate_defaults.get(provider_name, {})
        if overrides:
            caps = {**caps, **overrides}
        if provider_wide:
            mult = RATE_PROVIDER_CAP_MULTIPLIER
            caps = {k: float(v) * mult for k, v in caps.items()}
        return caps

    def _get_group_unlocked(self, provider_name: str, key: str,
                            model: str | None) -> BucketGroup:
        gk = self._group_key(provider_name, key, model)
        if gk not in self._groups:
            self._groups[gk] = BucketGroup(
                provider_name=provider_name,
                caps=self._caps_for(provider_name, provider_wide=(model is None)),
            )
            # Soft-cut floor uses unmultiplied defaults even for ×10 PW groups.
            base = self._caps_for(provider_name, provider_wide=False)
            for name, b in self._groups[gk].buckets.items():
                if name in base:
                    b._floor_cap = float(base[name])
        return self._groups[gk]

    def get_group(self, provider_name: str, key: str, model: str | None) -> BucketGroup:
        with self._lock:
            return self._get_group_unlocked(provider_name, key, model)

    def _both_groups_unlocked(self, provider_name: str, key: str, model: str):
        """Returns (provider_wide_group, model_group). model_group may equal
        provider_wide_group when model is None."""
        return (self._get_group_unlocked(provider_name, key, None),
                self._get_group_unlocked(provider_name, key, model))

    # ── Public API ────────────────────────────────────────────────────────────

    def check_and_consume(self, provider_name: str, key: str, model: str,
                          req_count: float, token_count: float) -> tuple[bool, float]:
        with self._lock:
            pw, mg = self._both_groups_unlocked(provider_name, key, model)
            pw_ok, pw_wait = pw.consume(req_count, token_count)
            if not pw_ok:
                return False, pw_wait
            mg_ok, mg_wait = mg.consume(req_count, token_count)
            if not mg_ok:
                pw.restore_tokens(token_count)
                pw.restore_requests(req_count)
                pw._requests_this_period -= int(req_count)
                return False, mg_wait
            return True, 0.0

    def restore(self, provider_name: str, key: str, model: str,
                token_surplus: float) -> None:
        if token_surplus <= 0:
            return
        with self._lock:
            pw, mg = self._both_groups_unlocked(provider_name, key, model)
            pw.restore_tokens(token_surplus)
            if mg is not pw:
                mg.restore_tokens(token_surplus)

    def release_reservation(self, provider_name: str, key: str, model: str,
                            req_count: float, token_count: float) -> None:
        """Undo a successful check_and_consume after a failed upstream call."""
        with self._lock:
            pw, mg = self._both_groups_unlocked(provider_name, key, model)
            for g in (pw,) if mg is pw else (pw, mg):
                if token_count > 0:
                    g.restore_tokens(token_count)
                if req_count > 0:
                    g.restore_requests(req_count)
                    g._requests_this_period = max(
                        0, g._requests_this_period - int(req_count))

    def reconcile(self, provider_name: str, key: str, model: str,
                  reserved: float, actual: float) -> None:
        """Align T-buckets with measured usage after a request completes.

        Over-reserve → restore surplus (same as restore()). Under-reserve →
        force-debit the shortfall so headroom reflects real spend.
        """
        delta = float(reserved) - float(actual)
        if delta > 0:
            self.restore(provider_name, key, model, delta)
            return
        if delta >= 0:
            return
        extra = -delta
        with self._lock:
            pw, mg = self._both_groups_unlocked(provider_name, key, model)
            now = time.time()
            for g in (pw,) if mg is pw else (pw, mg):
                for bname, b in g.buckets.items():
                    if not b.active or LIMIT_KEYS.get(bname, ("?",))[0] != "T":
                        continue
                    b.refill(now)
                    b.tokens = max(0.0, b.tokens - extra)
                    b._period_consumed += extra

    def on_success(self, provider_name: str, key: str, model: str,
                   token_count: float) -> None:
        with self._lock:
            pw, mg = self._both_groups_unlocked(provider_name, key, model)
            pw.on_success(
                token_count,
                streak=RATE_LEARN_SUCCESS_STREAK_PROVIDER,
                nudge_pct=RATE_LEARN_NUDGE_PCT_PROVIDER,
            )
            if mg is not pw:
                mg.on_success(token_count)  # model defaults

    def on_429(self, provider_name: str, key: str, model: str,
               headers: dict, *, model_headroom_before: float | None = None,
               observed_at: float | None = None) -> None:
        with self._lock:
            pw, mg = self._both_groups_unlocked(provider_name, key, model)
            if mg is pw:
                pw.on_429(headers, apply_retry_after=True, apply_headers=True,
                          soft=False, observed_at=observed_at)
                return
            pw.on_429(headers, apply_retry_after=False, apply_headers=False, soft=True)
            mg.on_429(headers, apply_retry_after=True, apply_headers=True,
                      soft=False, observed_at=observed_at)
            now = time.time()
            if (model_headroom_before is not None
                    and model_headroom_before >= 0.9
                    and (now - pw._last_surprise_cut_at) >= 60.0):
                pw.on_429({}, apply_retry_after=False, apply_headers=False, soft=True)
                pw._last_surprise_cut_at = now

    def update_from_headers(self, provider_name: str, key: str, model: str,
                            headers: dict, *,
                            observed_at: float | None = None) -> None:
        with self._lock:
            _pw, mg = self._both_groups_unlocked(provider_name, key, model)
            mg.update_from_headers(headers, observed_at=observed_at)

    def headroom(self, provider_name: str, key: str, model: str) -> float:
        """Read-only headroom for ranking. Does not create bucket groups."""
        with self._lock:
            pw_key = self._group_key(provider_name, key, None)
            mg_key = self._group_key(provider_name, key, model)
            pw = self._groups.get(pw_key)
            mg = self._groups.get(mg_key)
            if pw is None and mg is None:
                return 1.0
            scores = []
            if pw is not None:
                scores.append(pw.headroom())
            if mg is not None:
                scores.append(mg.headroom())
            return min(scores) if scores else 1.0

    def snapshot(self, provider_name: str, key: str, model: str) -> dict:
        """Read-only view for /v1/status. Does not create bucket groups."""
        with self._lock:
            pw_key = self._group_key(provider_name, key, None)
            mg_key = self._group_key(provider_name, key, model)
            pw = self._groups.get(pw_key)
            mg = self._groups.get(mg_key)

            def _buckets_to_status(g: BucketGroup | None) -> dict:
                if g is None:
                    return {}
                out = {}
                for name, b in g.buckets.items():
                    if not b.active:
                        continue
                    used = b.cap - b.tokens
                    out[name] = {
                        "cap":      round(b.cap, 1),
                        "used":     round(max(0.0, used), 1),
                        "headroom": round(b.headroom(), 3),
                    }
                return out

            blocked_until = 0.0
            if pw:
                blocked_until = max(blocked_until, pw.blocked_until)
            if mg:
                blocked_until = max(blocked_until, mg.blocked_until)
            return {
                "provider_wide": _buckets_to_status(pw),
                "model":         _buckets_to_status(mg),
                "blocked_until": blocked_until if blocked_until > time.time() else None,
            }

    def list_groups(self, include_orphans: bool = False,
                    configured_ids: set[str] | None = None) -> list[dict]:
        configured_ids = configured_ids or set()
        now = time.time()
        out = []
        with self._lock:
            for gk, g in self._groups.items():
                is_cfg = gk in configured_ids
                if not include_orphans and not is_cfg:
                    continue
                parsed = self.parse_group_key(gk)
                if not parsed:
                    continue
                buckets = {}
                for name, b in g.buckets.items():
                    b.refill(now)
                    used = max(0.0, b.cap - b.tokens)
                    buckets[name] = {
                        "cap": round(b.cap, 1),
                        "used": round(used, 1),
                        "tokens": round(b.tokens, 1),
                        "headroom": round(b.headroom(), 3),
                        "active": b.active,
                    }
                active = [(n, d) for n, d in buckets.items() if d["active"]]
                # Default view hides dormant groups (no active buckets); the
                # dashboard "Show dormant / orphan groups" toggle maps to
                # include_orphans=True and shows them with null binding/headroom.
                if not active and not include_orphans:
                    continue
                if active:
                    binding, bd = min(active, key=lambda x: x[1]["headroom"])
                    headroom = bd["headroom"]
                else:
                    binding, headroom = None, None
                out.append({
                    "id": gk,
                    "provider": parsed["provider"],
                    "key_hint": parsed["key_hint"],
                    "model": parsed["model"],
                    "scope": "model" if parsed["model"] else "provider_wide",
                    "role": "authoritative" if parsed["model"] else "estimate",
                    "configured": is_cfg,
                    "headroom": headroom,
                    "binding": binding,
                    "buckets": buckets,
                    "blocked_until": g.blocked_until if g.blocked_until > now else None,
                })
        return out

    def clear_group(self, group_id: str) -> bool:
        with self._lock:
            if group_id not in self._groups:
                return False
            del self._groups[group_id]
        self.flush()
        return True

    # ── Persistence ───────────────────────────────────────────────────────────

    def load(self) -> None:
        if not self.state_file.exists():
            return
        try:
            doc = json.loads(self.state_file.read_text())
            if doc.get("version") != 1:
                log.warning("[rate] state file version mismatch, skipping")
                return
            cleared_pw_holds = 0
            lifted_floors = 0
            with self._lock:
                for gk, bdict in (doc.get("groups") or {}).items():
                    pname = gk.split("|")[0].removeprefix("provider:")
                    g = BucketGroup.from_dict(bdict, provider_name=pname)
                    is_pw = "|model:" not in gk
                    # Migrate: Retry-After holds belong on (key, model) groups only.
                    # Drop leftover provider-wide blocked_until so one model's 429
                    # no longer blocks sibling models after restart.
                    if is_pw and g.blocked_until:
                        g.blocked_until = 0.0
                        cleared_pw_holds += 1
                    # Restore floor from unmultiplied defaults; lift only severe
                    # death-spirals (below soft-floor of the model default).
                    base_caps = self._caps_for(pname, provider_wide=False)
                    full_caps = self._caps_for(pname, provider_wide=is_pw)
                    for name, b in g.buckets.items():
                        floor = base_caps.get(name)
                        if floor is None:
                            continue
                        b._floor_cap = float(floor)
                        min_ok = float(floor) * RATE_LEARN_SOFT_FLOOR_FRAC
                        if b.cap + 1e-9 < min_ok:
                            target = float(full_caps.get(name, floor))
                            log.info(
                                f"[rate] migrate {gk} {name} cap "
                                f"{b.cap:.1f} → {target:.1f} (below floor)")
                            b.cap = target
                            b.tokens = target
                            lifted_floors += 1
                    self._groups[gk] = g
                n = len(self._groups)
            log.info(f"[rate] loaded {n} bucket groups from {self.state_file}")
            if cleared_pw_holds:
                log.info(f"[rate] cleared {cleared_pw_holds} provider-wide Retry-After hold(s)")
            if lifted_floors:
                log.info(f"[rate] lifted {lifted_floors} bucket(s) back to default floor")
        except Exception as e:
            log.warning(f"[rate] could not load state file: {e}")

    def flush(self) -> None:
        try:
            with self._lock:
                groups = {gk: d for gk, g in self._groups.items()
                          if (d := g.to_dict())}
            doc = {"version": 1, "groups": groups}
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(doc, indent=2))
            tmp.replace(self.state_file)
            log.debug(f"[rate] flushed {len(groups)} groups to {self.state_file}")
        except Exception as e:
            log.warning(f"[rate] flush failed: {e}")

    def run_all_inactive_checks(self) -> None:
        """Call run_inactive_check on every group. Called periodically."""
        with self._lock:
            groups = list(self._groups.values())
        for g in groups:
            g.run_inactive_check()
        log.debug(f"[rate] inactive check complete ({len(groups)} groups)")

    def start_flush_thread(self) -> None:
        def _loop():
            sweep_counter = 0
            while True:
                time.sleep(RATE_STATE_FLUSH_INTERVAL)
                self.flush()
                sweep_counter += 1
                if sweep_counter >= 10:
                    try:
                        self.run_all_inactive_checks()
                    except Exception as e:
                        log.warning(f"[rate] inactive check failed: {e}")
                    sweep_counter = 0
        t = threading.Thread(target=_loop, daemon=True, name="rate-flush")
        t.start()
        log.info(f"[rate] flush thread started (interval={RATE_STATE_FLUSH_INTERVAL}s)")
