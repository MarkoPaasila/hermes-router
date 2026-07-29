"""
Adaptive multi-window token bucket rate limiter for hermes-router.
"""
from __future__ import annotations
import os
import time
import threading
import json
import logging
from collections import defaultdict
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

RATE_SHORT_WAIT_MS        = _int_env("RATE_SHORT_WAIT_MS", 500)
RATE_HEADROOM_THRESHOLD   = _float_env("RATE_HEADROOM_THRESHOLD", 0.05)
RATE_LEARN_SUCCESS_STREAK = _int_env("RATE_LEARN_SUCCESS_STREAK", 20)
RATE_LEARN_NUDGE_PCT      = _float_env("RATE_LEARN_NUDGE_PCT", 5.0)
RATE_LEARN_CUT_FACTOR     = _float_env("RATE_LEARN_CUT_FACTOR", 0.8)
RATE_LEARN_MAX_MULTIPLIER = _float_env("RATE_LEARN_MAX_MULTIPLIER", 10.0)
RATE_STATE_FLUSH_INTERVAL = _int_env("RATE_STATE_FLUSH_INTERVAL", 60)

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
        self.tokens                = float(cap if tokens is None else tokens)
        self.last_refill           = last_refill if last_refill is not None else time.time()
        self.active                = True
        self._hit_zero             = False
        self._period_consumed      = 0.0
        self._consecutive_successes = 0

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

    def on_success(self) -> None:
        self._consecutive_successes += 1
        if self._consecutive_successes >= RATE_LEARN_SUCCESS_STREAK:
            ceiling = self._initial_cap * RATE_LEARN_MAX_MULTIPLIER
            if self.cap < ceiling:
                self.cap = min(ceiling, self.cap * (1.0 + RATE_LEARN_NUDGE_PCT / 100.0))
                log.info(f"[rate] nudged cap up to {self.cap:.1f}")
            self._consecutive_successes = 0

    def on_429(self, observed_rate: float) -> None:
        if self._period_consumed >= 3:
            new_cap = max(1.0, observed_rate * RATE_LEARN_CUT_FACTOR)
        else:
            new_cap = max(1.0, self.cap * 0.5)
        log.info(f"[rate] 429 cut cap {self.cap:.1f} → {new_cap:.1f}")
        self.cap = new_cap
        self.tokens = 0.0
        self._consecutive_successes = 0
        self._period_consumed = 0.0

    def set_from_header(self, cap: float, remaining: float) -> None:
        self.cap    = float(cap)
        self.tokens = float(remaining)
        if not self.active:
            self.active = True
            log.info("[rate] bucket re-activated by header")

    def check_inactive(self, requests_this_period: int) -> None:
        threshold = max(10, self.cap * 0.1)
        if not self._hit_zero and requests_this_period < threshold:
            self.active = False
            log.info(f"[rate] bucket marked inactive (requests={requests_this_period}, threshold={threshold:.0f})")

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


class BucketGroup:
    """All active token buckets for one (provider, key, model?) scope."""

    def __init__(self, provider_name: str, caps: dict[str, float] = None):
        self.provider_name = provider_name
        self.buckets: dict[str, TokenBucket] = {}
        self._requests_this_period = 0
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
        max_wait = 0.0
        checks: list[tuple[TokenBucket, float]] = []
        for name, b in self.buckets.items():
            if not b.active:
                continue
            dim, _ = LIMIT_KEYS[name]
            amount = req_count if dim == "R" else token_count
            if amount <= 0:
                continue
            b.refill(time.time())
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
        active = self._active()
        if not active:
            return 1.0
        return min(b.headroom() for b in active)

    def on_success(self, token_count: float) -> None:
        for b in self._active():
            b.on_success()

    def on_429(self, headers: dict) -> None:
        # If headers contain hard data, use set_from_header for those buckets.
        updated = self._apply_headers(headers, on_429=True)
        for name, b in self.buckets.items():
            if name in updated:
                continue
            if not b.active:
                b.active = True
                log.info(f"[rate] bucket {name} re-activated by 429")
            b.on_429(observed_rate=b._period_consumed)

    def update_from_headers(self, headers: dict) -> None:
        self._apply_headers(headers, on_429=False)

    def _apply_headers(self, headers: dict, on_429: bool) -> set:
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
        for suffix, cap_val in limits.items():
            rem_val = remaining.get(suffix, cap_val)
            pair = _HEADER_MAP.get(suffix)
            if not pair:
                continue
            dim, window_key = pair
            limit_name = _dim_window_to_limit(dim, window_key)
            if limit_name not in self.buckets:
                _, wk = LIMIT_KEYS[limit_name]
                self.buckets[limit_name] = TokenBucket(
                    window_seconds=WINDOWS[wk], cap=cap_val, tokens=rem_val)
                log.info(f"[rate] created bucket {limit_name} from header cap={cap_val}")
            else:
                self.buckets[limit_name].set_from_header(cap_val, rem_val)
            updated.add(limit_name)
        return updated

    def run_inactive_check(self) -> None:
        n = self._requests_this_period
        for b in self.buckets.values():
            if b.active:
                b.check_inactive(n)
        self._requests_this_period = 0

    def to_dict(self) -> dict:
        return {name: b.to_dict() for name, b in self.buckets.items() if b.active}

    @classmethod
    def from_dict(cls, d: dict, provider_name: str) -> "BucketGroup":
        caps = {name: v["cap"] for name, v in d.items()}
        g = cls(provider_name=provider_name, caps=caps)
        for name, bdict in d.items():
            if name in g.buckets:
                g.buckets[name].tokens = min(bdict["cap"] * 0.5,
                                             bdict.get("tokens", bdict["cap"]))
                g.buckets[name].last_refill = bdict.get("last_refill", time.time())
                g.buckets[name]._initial_cap = bdict["cap"]
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

    def _caps_for(self, provider_name: str) -> dict[str, float]:
        caps = _load_caps_for(provider_name)
        overrides = self._auth_rate_defaults.get(provider_name, {})
        if overrides:
            caps = {**caps, **overrides}
        return caps

    def _get_group_unlocked(self, provider_name: str, key: str,
                            model: str | None) -> BucketGroup:
        gk = self._group_key(provider_name, key, model)
        if gk not in self._groups:
            self._groups[gk] = BucketGroup(
                provider_name=provider_name, caps=self._caps_for(provider_name))
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

    def on_success(self, provider_name: str, key: str, model: str,
                   token_count: float) -> None:
        with self._lock:
            pw, mg = self._both_groups_unlocked(provider_name, key, model)
            pw.on_success(token_count)
            if mg is not pw:
                mg.on_success(token_count)

    def on_429(self, provider_name: str, key: str, model: str,
               headers: dict) -> None:
        with self._lock:
            pw, mg = self._both_groups_unlocked(provider_name, key, model)
            pw.on_429(headers)
            if mg is not pw:
                mg.on_429(headers)

    def update_from_headers(self, provider_name: str, key: str, model: str,
                            headers: dict) -> None:
        with self._lock:
            pw, mg = self._both_groups_unlocked(provider_name, key, model)
            pw.update_from_headers(headers)
            if mg is not pw:
                mg.update_from_headers(headers)

    def headroom(self, provider_name: str, key: str, model: str) -> float:
        with self._lock:
            pw, mg = self._both_groups_unlocked(provider_name, key, model)
            return min(pw.headroom(), mg.headroom())

    def snapshot(self, provider_name: str, key: str, model: str) -> dict:
        """Returns a dict suitable for inclusion in /v1/status."""
        with self._lock:
            pw = self._get_group_unlocked(provider_name, key, None)
            mg = self._get_group_unlocked(provider_name, key, model)

            def _buckets_to_status(g: BucketGroup) -> dict:
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

            return {
                "provider_wide": _buckets_to_status(pw),
                "model":         _buckets_to_status(mg),
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
                    "configured": is_cfg,
                    "headroom": headroom,
                    "binding": binding,
                    "buckets": buckets,
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
            with self._lock:
                for gk, bdict in (doc.get("groups") or {}).items():
                    pname = gk.split("|")[0].removeprefix("provider:")
                    self._groups[gk] = BucketGroup.from_dict(bdict, provider_name=pname)
                n = len(self._groups)
            log.info(f"[rate] loaded {n} bucket groups from {self.state_file}")
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
