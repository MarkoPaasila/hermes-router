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
