"""Adaptive per-model input/output token cap tracker for hermes-router."""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

CUT_FACTOR = 0.9
RAISE_FACTOR = 1.05
NEAR_CAP_RATIO = 0.85
MIN_CAP = 256


def _min_cap(env_bound: int, tracker_val: int | None) -> int | None:
    candidates = []
    if env_bound and env_bound > 0:
        candidates.append(env_bound)
    if tracker_val is not None and tracker_val > 0:
        candidates.append(tracker_val)
    if not candidates:
        return None
    return min(candidates)


class TokenCapTracker:
    def __init__(self, state_file: Path, enabled: bool = True):
        self.state_file = Path(state_file)
        self.enabled = enabled
        self._lock = threading.Lock()
        # (provider, model) -> {max_input, max_output, source, updated_at}
        self._caps: dict[tuple[str, str], dict] = {}

    def _entry(self, provider: str, model: str) -> dict:
        key = (provider, model)
        if key not in self._caps:
            self._caps[key] = {
                "max_input": None,
                "max_output": None,
                "source": "metadata",
                "updated_at": time.time(),
            }
        return self._caps[key]

    def effective_input_cap(self, provider: str, model: str, env_bound: int) -> int | None:
        with self._lock:
            raw = None
            if self.enabled:
                e = self._caps.get((provider, model))
                if e:
                    raw = e.get("max_input")
            return _min_cap(env_bound, raw)

    def effective_output_cap(self, provider: str, model: str, env_bound: int) -> int | None:
        with self._lock:
            raw = None
            if self.enabled:
                e = self._caps.get((provider, model))
                if e:
                    raw = e.get("max_output")
            return _min_cap(env_bound, raw)

    def seed_from_metadata(
        self,
        provider: str,
        model: str,
        max_input: int | None = None,
        max_output: int | None = None,
    ) -> None:
        if not self.enabled:
            return
        changed = False
        with self._lock:
            e = self._entry(provider, model)
            for field, val in (("max_input", max_input), ("max_output", max_output)):
                if val is None or val <= 0:
                    continue
                cur = e.get(field)
                # Do not loosen a tighter learned value.
                if cur is not None and e.get("source") in ("learned", "mixed") and val > cur:
                    continue
                if cur != val:
                    e[field] = int(val)
                    changed = True
            if changed:
                if e.get("source") == "learned":
                    e["source"] = "mixed"
                elif e.get("source") not in ("learned", "mixed"):
                    e["source"] = "metadata"
                e["updated_at"] = time.time()
                log.info(
                    f"[token-cap] seed {provider}/{model} "
                    f"in={e.get('max_input')} out={e.get('max_output')}"
                )
        if changed:
            self.flush()

    def on_token_limit_failure(
        self, provider: str, model: str, kind: str, observed_tokens: int
    ) -> None:
        if not self.enabled or observed_tokens <= 0:
            return
        field = "max_input" if kind == "input" else "max_output"
        cut = max(MIN_CAP, int(observed_tokens * CUT_FACTOR))
        with self._lock:
            e = self._entry(provider, model)
            prior = e.get(field)
            new_cap = cut if prior is None else min(prior, cut)
            e[field] = new_cap
            src = e.get("source")
            e["source"] = "mixed" if src == "metadata" else "learned"
            e["updated_at"] = time.time()
            log.info(
                f"[token-cap] cut {provider}/{model} {field} "
                f"{prior} → {new_cap} (observed={observed_tokens})"
            )
        self.flush()

    def on_success_near_cap(
        self, provider: str, model: str, kind: str, used_tokens: int
    ) -> None:
        if not self.enabled or used_tokens <= 0:
            return
        field = "max_input" if kind == "input" else "max_output"
        changed = False
        with self._lock:
            e = self._caps.get((provider, model))
            if not e:
                return
            cur = e.get(field)
            if cur is None or cur <= 0:
                return
            if used_tokens < int(cur * NEAR_CAP_RATIO):
                return
            new_cap = int(cur * RAISE_FACTOR)
            if new_cap == cur:
                new_cap = cur + 1
            e[field] = new_cap
            if e.get("source") == "metadata":
                e["source"] = "mixed"
            e["updated_at"] = time.time()
            changed = True
            log.info(
                f"[token-cap] raise {provider}/{model} {field} {cur} → {new_cap}"
            )
        if changed:
            self.flush()

    def snapshot(self, provider: str, model: str) -> dict | None:
        with self._lock:
            e = self._caps.get((provider, model))
            if not e:
                return None
            return {
                "max_input": e.get("max_input"),
                "max_output": e.get("max_output"),
                "source": e.get("source"),
                "updated_at": e.get("updated_at"),
            }

    def load(self) -> None:
        if not self.state_file.exists():
            return
        try:
            doc = json.loads(self.state_file.read_text())
        except Exception as exc:
            log.warning(f"[token-cap] could not load {self.state_file}: {exc}")
            return
        models = doc.get("models") or {}
        with self._lock:
            self._caps.clear()
            for key, val in models.items():
                if not isinstance(val, dict) or "::" not in key:
                    continue
                provider, model = key.split("::", 1)
                self._caps[(provider, model)] = {
                    "max_input": val.get("max_input"),
                    "max_output": val.get("max_output"),
                    "source": val.get("source") or "learned",
                    "updated_at": val.get("updated_at") or time.time(),
                }

    def flush(self) -> None:
        with self._lock:
            models = {
                f"{p}::{m}": {
                    "max_input": e.get("max_input"),
                    "max_output": e.get("max_output"),
                    "source": e.get("source"),
                    "updated_at": e.get("updated_at"),
                }
                for (p, m), e in self._caps.items()
            }
        try:
            self.state_file.write_text(json.dumps({"models": models}, indent=2))
        except Exception as exc:
            log.warning(f"[token-cap] could not flush {self.state_file}: {exc}")
