"""Pool capacity score for client pacing (Hermes cron, etc.).

Pure functions — no Flask / rate_limiter imports. Router gathers candidate
dicts and calls score_pool().
"""
from __future__ import annotations

HEALTH_FACTOR = {0: 1.0, 1: 0.7, 2: 0.3}
TOP_K = 3
USABLE_MIN = 0.05
USABLE_COUNT_FLOOR = 2


def health_factor(health_bucket: int, *, breaker_open: bool = False,
                 blocked: bool = False) -> float:
    if breaker_open or blocked:
        return 0.0
    return HEALTH_FACTOR.get(int(health_bucket), 0.3)


def advice_for(capacity: float) -> tuple[str, float, bool]:
    """Map capacity in [0,1] → (advice, interval_multiplier, skip)."""
    if capacity >= 0.60:
        return "fast", 0.5, False
    if capacity >= 0.35:
        return "normal", 1.0, False
    if capacity >= 0.15:
        return "slow", 2.0, False
    return "skip", 4.0, True


def _effective(c: dict) -> float:
    h = max(0.0, min(1.0, float(c.get("headroom", 1.0))))
    f = health_factor(
        int(c.get("health_bucket", 0) or 0),
        breaker_open=bool(c.get("breaker_open")),
        blocked=bool(c.get("blocked")),
    )
    return h * f


def score_pool(candidates: list[dict], *, top_k: int = TOP_K) -> dict:
    """Score a pool of candidate dicts into the /v1/capacity body (sans generated_at)."""
    if not candidates:
        return {
            "capacity": 0.0,
            "advice": "skip",
            "interval_multiplier": 4.0,
            "skip": True,
            "reasons": ["no_candidates"],
            "components": {
                "headroom": 0.0,
                "health": 0.0,
                "usable_candidates": 0,
                "top_k": top_k,
            },
        }

    scored = []
    for c in candidates:
        h = max(0.0, min(1.0, float(c.get("headroom", 1.0))))
        f = health_factor(
            int(c.get("health_bucket", 0) or 0),
            breaker_open=bool(c.get("breaker_open")),
            blocked=bool(c.get("blocked")),
        )
        scored.append((h * f, h, f))

    usable = sum(1 for e, _, _ in scored if e > USABLE_MIN)
    scored.sort(key=lambda t: t[0], reverse=True)
    k = max(1, min(top_k, len(scored)))
    top = scored[:k]
    capacity = sum(e for e, _, _ in top) / k
    mean_headroom = sum(h for _, h, _ in top) / k
    mean_health = sum(f for _, _, f in top) / k

    reasons = [
        f"top_headroom={mean_headroom:.2f}",
        f"health_drag={max(0.0, mean_headroom - capacity):.2f}",
    ]

    thin = usable < USABLE_COUNT_FLOOR
    if thin:
        advice, mult, skip = "skip", 4.0, True
        reasons.append("thin_usable_set")
    else:
        advice, mult, skip = advice_for(capacity)

    return {
        "capacity": round(capacity, 3),
        "advice": advice,
        "interval_multiplier": mult,
        "skip": skip,
        "reasons": reasons,
        "components": {
            "headroom": round(mean_headroom, 3),
            "health": round(mean_health, 3),
            "usable_candidates": usable,
            "top_k": k,
        },
    }
