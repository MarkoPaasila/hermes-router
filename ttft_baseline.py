"""Per-(provider, model) TTFT EWMA baselines and first-byte deadlines."""
from __future__ import annotations

import os
import threading
from typing import Any


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def abort_enabled() -> bool:
    return os.environ.get("TTFT_ABORT_ENABLED", "1").strip().lower() not in (
        "0", "", "false", "no", "off",
    )


class TtftDeadlineExceeded(Exception):
    def __init__(self, deadline_s: float, waited_s: float):
        self.deadline_s = float(deadline_s)
        self.waited_s = float(waited_s)
        super().__init__(
            f"TTFT deadline exceeded: waited {self.waited_s:.2f}s > {self.deadline_s:.2f}s"
        )


class TtftBaselineStore:
    def __init__(
        self,
        floor_s: float | None = None,
        mult: float | None = None,
        min_samples: int | None = None,
        cold_deadline_s: float | None = None,
        alpha: float | None = None,
    ):
        self.floor_s = float(floor_s if floor_s is not None else _float_env("TTFT_FLOOR_S", 5.0))
        self.mult = float(mult if mult is not None else _float_env("TTFT_MULT", 3.0))
        self.min_samples = int(
            min_samples if min_samples is not None else _int_env("TTFT_MIN_SAMPLES", 5)
        )
        self.cold_deadline_s = float(
            cold_deadline_s if cold_deadline_s is not None
            else _float_env("TTFT_COLD_DEADLINE_S", 20.0)
        )
        self.alpha = float(
            alpha if alpha is not None else _float_env("TTFT_EWMA_ALPHA", 0.2)
        )
        self._lock = threading.Lock()
        self._data: dict[tuple[str, str], dict[str, Any]] = {}

    def _key(self, provider: str, model: str) -> tuple[str, str]:
        return (provider, model)

    def deadline_s(self, provider: str, model: str) -> float:
        with self._lock:
            e = self._data.get(self._key(provider, model))
            if not e:
                return self.cold_deadline_s
            warm = max(self.floor_s, self.mult * e["ewma_s"])
            if e["sample_count"] < self.min_samples:
                # Abort-inflated EWMA must loosen the deadline immediately; staying
                # pinned at cold until min_samples re-aborts at the same cap forever.
                # Fast early samples still get at least the cold window.
                return max(self.cold_deadline_s, warm)
            return warm

    def record(self, provider: str, model: str, ttft_s: float) -> None:
        ttft_s = float(ttft_s)
        if ttft_s < 0:
            return
        with self._lock:
            k = self._key(provider, model)
            e = self._data.get(k)
            if not e:
                self._data[k] = {
                    "ewma_s": ttft_s,
                    "sample_count": 1,
                    "last_ttft_s": ttft_s,
                }
                return
            a = self.alpha
            e["ewma_s"] = a * ttft_s + (1.0 - a) * e["ewma_s"]
            e["sample_count"] += 1
            e["last_ttft_s"] = ttft_s

    def summary(self, provider: str, model: str) -> dict:
        with self._lock:
            e = self._data.get(self._key(provider, model))
            if not e:
                return {"ewma_s": None, "sample_count": 0, "last_ttft_s": None}
            return {
                "ewma_s": e["ewma_s"],
                "sample_count": e["sample_count"],
                "last_ttft_s": e["last_ttft_s"],
            }
