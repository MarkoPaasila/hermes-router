"""Structured cascade trail for request-log observability."""
from __future__ import annotations

REASON_LABELS = {
    "rate_headroom": "Rate headroom exhausted",
    "rate_hold": "Rate limit hold (Retry-After)",
    "token_cap": "Input over token cap",
    "no_tools": "No tool support",
    "no_vision": "No vision support",
    "circuit_open": "Circuit breaker open",
    "access_scope": "Outside access-key provider scope",
    "keys_cooling": "All keys cooling",
    "unsuitable_cooling": "Unsuitable model cooldown",
    "network": "Network / timeout",
    "ttft_deadline": "TTFT deadline exceeded",
    "http_429": "HTTP 429",
    "http_401": "HTTP 401",
    "http_403": "HTTP 403",
    "http_400": "HTTP 400",
    "http_404": "HTTP 404",
    "http_413": "HTTP 413",
    "http_5xx": "HTTP 5xx",
}

# Higher wins when coalescing multiple key attempts on one model.
_REASON_PRIORITY = {
    "network": 100,
    "ttft_deadline": 95,
    "http_5xx": 90,
    "http_429": 80,
    "http_401": 70,
    "http_403": 70,
    "http_413": 65,
    "http_400": 60,
    "http_404": 60,
    "rate_headroom": 40,
    "rate_hold": 45,
    "keys_cooling": 20,
    "token_cap": 50,
    "no_tools": 50,
    "no_vision": 50,
    "circuit_open": 50,
    "access_scope": 50,
}


def reason_label(code: str | None) -> str:
    if code is None:
        return ""
    return REASON_LABELS.get(code, code)


def http_reason(status_code: int) -> str:
    known = {429, 401, 403, 400, 404, 413}
    if status_code in known:
        return f"http_{status_code}"
    if status_code >= 500:
        return "http_5xx"
    return f"http_{status_code}"


def _prio(reason: str) -> int:
    return _REASON_PRIORITY.get(reason, 10)


class CascadeTrail:
    def __init__(self) -> None:
        self.steps: list[dict] = []
        self._open: dict | None = None

    def skip(self, provider: str, model: str, reason: str) -> None:
        self.flush()
        self.steps.append({
            "provider": provider, "model": model,
            "outcome": "skipped", "reason": reason,
        })

    def note(self, provider: str, model: str, outcome: str, reason: str) -> None:
        if self._open and (self._open["provider"] != provider or self._open["model"] != model):
            self.flush()
        if self._open is None:
            self._open = {
                "provider": provider, "model": model,
                "outcome": outcome, "reason": reason,
            }
            return
        if outcome == "failed" and self._open["outcome"] != "failed":
            self._open["outcome"] = "failed"
        if _prio(reason) >= _prio(self._open["reason"]):
            self._open["reason"] = reason

    def flush(self) -> None:
        if self._open is not None:
            self.steps.append(self._open)
            self._open = None

    def success(self, provider: str, model: str) -> None:
        self.flush()
        self.steps.append({
            "provider": provider, "model": model,
            "outcome": "success", "reason": None,
        })

    def as_log_fields(self) -> dict:
        self.flush()
        failed = sum(1 for s in self.steps if s["outcome"] == "failed")
        skipped = sum(1 for s in self.steps if s["outcome"] == "skipped")
        return {
            "cascade": list(self.steps),
            "failed": failed,
            "skipped": skipped,
            "cascades": failed + skipped,
        }
