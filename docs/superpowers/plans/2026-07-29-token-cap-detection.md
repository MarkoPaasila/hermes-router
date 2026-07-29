# Adaptive Per-Model Token Cap Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect per-`(provider, model)` input/output token caps from `/models` metadata and adaptive traffic learning, then auto-skip oversized requests and clamp `max_tokens`.

**Architecture:** New `token_caps.py` module owns `TokenCapTracker`, metadata extraction, and token-limit error classification. The router seeds caps during `/models` discovery, consults `effective_*_cap` for skip/clamp, and feeds classified failures/near-cap successes back into the tracker. Env `{PROVIDER}_SKIP_TOKENS_OVER` / `{PROVIDER}_MAX_OUTPUT_TOKENS` remain provider-wide outer fences. Separate from TBF.

**Tech Stack:** Python 3, Flask router (`router.py`), pytest, JSON state file persistence.

## Global Constraints

- Scope is `(provider, model)` — never per-key.
- Env bounds are outer fences: discovery/learning may only tighten inside them (`0` = no fence).
- Skip the **candidate** when `est_tokens >= effective_input_cap`, not the whole provider.
- `TOKEN_CAPS=0` disables tracker values in `effective_*` and disables learning updates.
- Learning constants (v1 hardcoded): `CUT_FACTOR=0.9`, raise `1.05`, near-cap `0.85`, `MIN_CAP=256`.
- Uncertain 400s must not learn; only 413 or phrase-matched 400s.
- Do not fold into `AdaptiveRateLimiter`.
- Spec: `docs/superpowers/specs/2026-07-29-token-cap-detection-design.md`.

---

## File Structure

| File | Responsibility |
|---|---|
| `token_caps.py` | Tracker, classifier, metadata extraction, persistence |
| `tests/test_token_caps.py` | Unit tests for tracker / classifier / metadata / persist |
| `tests/test_token_caps_router.py` | Router wiring tests (skip, clamp, learn, seed) with mocks |
| `router.py` | Construct tracker, wire skip/clamp/learn/seed/status |
| `.gitignore` | Ignore `token_caps_state.json` |
| `.env.example` | Document `TOKEN_CAPS` / `TOKEN_CAPS_STATE_FILE` |
| `documentation/configuration.md` | Docs for adaptive token caps |
| `website/src/content/docs/configuration.md` | Mirror docs |

---

### Task 1: TokenCapTracker core (effective caps, cut, raise, persist)

**Files:**
- Create: `token_caps.py`
- Create: `tests/test_token_caps.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - Constants: `CUT_FACTOR = 0.9`, `RAISE_FACTOR = 1.05`, `NEAR_CAP_RATIO = 0.85`, `MIN_CAP = 256`
  - `class TokenCapTracker`
  - `TokenCapTracker(state_file: Path, enabled: bool = True)`
  - `effective_input_cap(provider: str, model: str, env_bound: int) -> int | None`
  - `effective_output_cap(provider: str, model: str, env_bound: int) -> int | None`
  - `seed_from_metadata(provider: str, model: str, max_input: int | None = None, max_output: int | None = None) -> None`
  - `on_token_limit_failure(provider: str, model: str, kind: str, observed_tokens: int) -> None` — `kind` is `"input"` or `"output"`
  - `on_success_near_cap(provider: str, model: str, kind: str, used_tokens: int) -> None`
  - `snapshot(provider: str, model: str) -> dict | None` — `{max_input, max_output, source, updated_at}` or `None`
  - `load() -> None`, `flush() -> None`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_token_caps.py`:

```python
"""Unit tests for TokenCapTracker (effective caps, learning, persistence)."""
import pytest
from token_caps import TokenCapTracker, MIN_CAP, CUT_FACTOR, RAISE_FACTOR, NEAR_CAP_RATIO


@pytest.fixture
def tracker(tmp_path):
    return TokenCapTracker(state_file=tmp_path / "caps.json", enabled=True)


def test_effective_unset_returns_none(tracker):
    assert tracker.effective_input_cap("groq", "llama", 0) is None
    assert tracker.effective_output_cap("groq", "llama", 0) is None


def test_effective_env_only(tracker):
    assert tracker.effective_input_cap("groq", "llama", 5500) == 5500
    assert tracker.effective_output_cap("cohere", "cmd", 8192) == 8192


def test_effective_tracker_only(tracker):
    tracker.seed_from_metadata("groq", "llama", max_input=8000, max_output=4096)
    assert tracker.effective_input_cap("groq", "llama", 0) == 8000
    assert tracker.effective_output_cap("groq", "llama", 0) == 4096


def test_effective_min_of_env_and_tracker(tracker):
    tracker.seed_from_metadata("groq", "llama", max_input=8000)
    assert tracker.effective_input_cap("groq", "llama", 5500) == 5500
    tracker.seed_from_metadata("groq", "llama", max_input=4000)
    assert tracker.effective_input_cap("groq", "llama", 5500) == 4000


def test_disabled_ignores_tracker_values(tmp_path):
    t = TokenCapTracker(state_file=tmp_path / "caps.json", enabled=False)
    t.seed_from_metadata("groq", "llama", max_input=4000)
    assert t.effective_input_cap("groq", "llama", 5500) == 5500
    assert t.effective_input_cap("groq", "llama", 0) is None


def test_failure_cuts_cap(tracker):
    tracker.seed_from_metadata("groq", "llama", max_input=10000)
    tracker.on_token_limit_failure("groq", "llama", "input", observed_tokens=9000)
    expected = max(MIN_CAP, int(9000 * CUT_FACTOR))
    assert tracker.effective_input_cap("groq", "llama", 0) == expected


def test_failure_never_raises(tracker):
    tracker.seed_from_metadata("groq", "llama", max_input=5000)
    tracker.on_token_limit_failure("groq", "llama", "input", observed_tokens=8000)
    # new_cap = min(prior, max(MIN_CAP, int(observed * CUT_FACTOR)))
    assert tracker.effective_input_cap("groq", "llama", 0) <= 5000


def test_success_near_cap_raises(tracker):
    tracker.seed_from_metadata("groq", "llama", max_input=1000)
    used = int(1000 * NEAR_CAP_RATIO)
    tracker.on_success_near_cap("groq", "llama", "input", used_tokens=used)
    assert tracker.effective_input_cap("groq", "llama", 0) == int(1000 * RAISE_FACTOR)


def test_success_far_below_does_not_raise(tracker):
    tracker.seed_from_metadata("groq", "llama", max_input=1000)
    tracker.on_success_near_cap("groq", "llama", "input", used_tokens=100)
    assert tracker.effective_input_cap("groq", "llama", 0) == 1000


def test_seed_does_not_loosen_tighter_learned(tracker):
    tracker.on_token_limit_failure("groq", "llama", "input", observed_tokens=4000)
    learned = tracker.effective_input_cap("groq", "llama", 0)
    tracker.seed_from_metadata("groq", "llama", max_input=20000)
    assert tracker.effective_input_cap("groq", "llama", 0) == learned


def test_persist_round_trip(tmp_path):
    path = tmp_path / "caps.json"
    t1 = TokenCapTracker(state_file=path, enabled=True)
    t1.seed_from_metadata("cerebras", "llama3.1-8b", max_input=8192, max_output=2048)
    t1.flush()
    t2 = TokenCapTracker(state_file=path, enabled=True)
    t2.load()
    assert t2.effective_input_cap("cerebras", "llama3.1-8b", 0) == 8192
    assert t2.effective_output_cap("cerebras", "llama3.1-8b", 0) == 2048
    snap = t2.snapshot("cerebras", "llama3.1-8b")
    assert snap["source"] == "metadata"
    assert "updated_at" in snap


def test_corrupt_file_fail_soft(tmp_path):
    path = tmp_path / "caps.json"
    path.write_text("{not json")
    t = TokenCapTracker(state_file=path, enabled=True)
    t.load()  # must not raise
    assert t.effective_input_cap("x", "y", 0) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_token_caps.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'token_caps'` (or import errors).

- [ ] **Step 3: Implement `token_caps.py`**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_token_caps.py -v`

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add token_caps.py tests/test_token_caps.py
git commit -m "$(cat <<'EOF'
feat: add TokenCapTracker for per-model input/output ceilings

Persist learned/metadata caps and apply env outer fences in effective_* helpers.
EOF
)"
```

---

### Task 2: Error classifier and `/models` metadata extraction

**Files:**
- Modify: `token_caps.py`
- Modify: `tests/test_token_caps.py`

**Interfaces:**
- Consumes: Task 1 tracker
- Produces:
  - `extract_caps_from_model_item(item: dict) -> tuple[int | None, int | None]` — `(max_input, max_output)`
  - `classify_token_limit_error(status_code: int, body: str, *, est_tokens: int = 0, requested_max_tokens: int = 0) -> str | None` — returns `"input"`, `"output"`, or `None`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_token_caps.py`:

```python
from token_caps import extract_caps_from_model_item, classify_token_limit_error


def test_extract_context_length():
    assert extract_caps_from_model_item({"id": "m", "context_length": 8192}) == (8192, None)


def test_extract_max_model_len_and_output():
    item = {"id": "m", "max_model_len": 32768, "max_completion_tokens": 4096}
    assert extract_caps_from_model_item(item) == (32768, 4096)


def test_extract_nested_top_provider():
    item = {
        "id": "m",
        "top_provider": {"context_length": 128000, "max_completion_tokens": 16384},
    }
    assert extract_caps_from_model_item(item) == (128000, 16384)


def test_extract_missing_returns_nones():
    assert extract_caps_from_model_item({"id": "m"}) == (None, None)


def test_classify_413_as_input():
    assert classify_token_limit_error(413, "Payload Too Large") == "input"


def test_classify_400_context_length():
    body = "This model's maximum context length is 8192 tokens"
    assert classify_token_limit_error(400, body, est_tokens=9000) == "input"


def test_classify_400_max_tokens_output():
    body = "max_tokens is too large: 65536"
    assert classify_token_limit_error(400, body, requested_max_tokens=65536) == "output"


def test_classify_unrelated_400_returns_none():
    assert classify_token_limit_error(400, "invalid tool schema") is None


def test_classify_ambiguous_uses_heuristics():
    body = "too many tokens"
    assert classify_token_limit_error(400, body, est_tokens=12000, requested_max_tokens=256) == "input"
    assert classify_token_limit_error(400, body, est_tokens=100, requested_max_tokens=100000) == "output"
```

- [ ] **Step 2: Run new tests to verify they fail**

Run: `pytest tests/test_token_caps.py -k "extract or classify" -v`

Expected: FAIL with import / not-defined errors.

- [ ] **Step 3: Implement classifier and extractor**

Add to `token_caps.py`:

```python
_INPUT_FIELDS = (
    "context_length", "max_model_len", "max_input_tokens", "max_position_embeddings",
)
_OUTPUT_FIELDS = ("max_completion_tokens", "max_output_tokens", "max_tokens")

_TOKEN_LIMIT_PHRASES = (
    "context length",
    "maximum context",
    "too many tokens",
    "token limit",
    "prompt is too long",
    "prompt too long",
    "max_tokens",
    "max_completion_tokens",
    "maximum number of tokens",
    "context_length_exceeded",
    "payload too large",
    "request too large",
)


def _first_positive_int(obj: dict, fields: tuple[str, ...]) -> int | None:
    for f in fields:
        v = obj.get(f)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)) and v > 0:
            return int(v)
        if isinstance(v, str) and v.strip().isdigit():
            n = int(v.strip())
            if n > 0:
                return n
    return None


def extract_caps_from_model_item(item: dict) -> tuple[int | None, int | None]:
    if not isinstance(item, dict):
        return None, None
    buckets = [item]
    for nest in ("top_provider", "architecture", "meta", "limits"):
        nested = item.get(nest)
        if isinstance(nested, dict):
            buckets.append(nested)
    max_in = max_out = None
    for b in buckets:
        if max_in is None:
            max_in = _first_positive_int(b, _INPUT_FIELDS)
        if max_out is None:
            max_out = _first_positive_int(b, _OUTPUT_FIELDS)
        if max_in is not None and max_out is not None:
            break
    return max_in, max_out


def classify_token_limit_error(
    status_code: int,
    body: str,
    *,
    est_tokens: int = 0,
    requested_max_tokens: int = 0,
) -> str | None:
    text = (body or "").lower()
    if status_code == 413:
        return "input"
    if status_code != 400:
        return None
    if not any(p in text for p in _TOKEN_LIMIT_PHRASES):
        return None
    if "max_tokens" in text or "max_completion_tokens" in text or "completion" in text:
        if "context" not in text and "prompt" not in text:
            return "output"
    if "context" in text or "prompt" in text:
        return "input"
    if est_tokens >= requested_max_tokens and est_tokens >= 1024:
        return "input"
    if requested_max_tokens > est_tokens and requested_max_tokens >= 4096:
        return "output"
    return "input"
```

Tune until the tests above pass; do not weaken `test_classify_unrelated_400_returns_none`.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_token_caps.py -v`

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add token_caps.py tests/test_token_caps.py
git commit -m "$(cat <<'EOF'
feat: classify token-limit errors and extract /models cap fields

Seed helpers for metadata-first caps and safe learning triggers.
EOF
)"
```

---

### Task 3: Wire tracker into router (init, skip, clamp, status, gitignore)

**Files:**
- Modify: `router.py` (imports, globals near `rate_limiter`, skip logic ~5218–5225, clamp in `forward()` ~3316–3326, `/v1/status` ~6138–6141, shutdown flush near rate flush)
- Modify: `.gitignore`
- Create: `tests/test_token_caps_router.py`

**Interfaces:**
- Consumes: `TokenCapTracker`, `effective_*_cap`
- Produces:
  - Global `token_caps: TokenCapTracker`
  - Env: `TOKEN_CAPS` (default on), `TOKEN_CAPS_STATE_FILE` (default `./token_caps_state.json`)
  - Helpers: `_effective_input_cap_for`, `_effective_output_cap_for`, `_apply_output_token_cap`
  - Per-candidate skip; `forward()` clamp; status `token_caps`

- [ ] **Step 1: Write the failing router wiring tests**

Create `tests/test_token_caps_router.py`:

```python
"""Router wiring tests for adaptive token caps."""
import router
from token_caps import TokenCapTracker


def test_skip_uses_effective_input_cap_per_candidate(monkeypatch, tmp_path):
    monkeypatch.setattr(router, "TOKEN_CAPS_ENABLED", True)
    caps = TokenCapTracker(state_file=tmp_path / "c.json", enabled=True)
    caps.seed_from_metadata("groq", "small", max_input=1000)
    monkeypatch.setattr(router, "token_caps", caps)

    provider = {"name": "groq", "skip_if_tokens_over": 5500}
    assert router._effective_input_cap_for(provider, "small") == 1000
    assert router._effective_input_cap_for(provider, "large") == 5500


def test_forward_clamp_uses_effective_output_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(router, "TOKEN_CAPS_ENABLED", True)
    caps = TokenCapTracker(state_file=tmp_path / "c.json", enabled=True)
    caps.seed_from_metadata("cohere", "command-a", max_output=2048)
    monkeypatch.setattr(router, "token_caps", caps)

    provider = {
        "name": "cohere",
        "model": "command-a",
        "max_output_tokens": 8192,
    }
    body = {"model": "command-a", "max_tokens": 65536, "messages": []}
    router._apply_output_token_cap(body, provider, "command-a")
    assert body["max_tokens"] == 2048


def test_token_caps_disabled_uses_env_only(monkeypatch, tmp_path):
    caps = TokenCapTracker(state_file=tmp_path / "c.json", enabled=False)
    caps._caps[("groq", "llama")] = {
        "max_input": 100, "max_output": 50, "source": "learned", "updated_at": 0,
    }
    monkeypatch.setattr(router, "token_caps", caps)
    monkeypatch.setattr(router, "TOKEN_CAPS_ENABLED", False)
    provider = {"name": "groq", "skip_if_tokens_over": 5500, "max_output_tokens": 8192}
    assert router._effective_input_cap_for(provider, "llama") == 5500
    body = {"max_tokens": 65536}
    router._apply_output_token_cap(body, {**provider, "model": "llama"}, "llama")
    assert body["max_tokens"] == 8192
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_token_caps_router.py -v`

Expected: FAIL (helpers / globals missing).

- [ ] **Step 3: Wire into `router.py`**

Near rate-limiter globals (~148 / ~1806):

```python
from token_caps import TokenCapTracker

TOKEN_CAPS_ENABLED = os.environ.get("TOKEN_CAPS", "1").strip().lower() not in (
    "0", "", "false", "no", "off",
)
TOKEN_CAPS_STATE_FILE = Path(
    os.environ.get("TOKEN_CAPS_STATE_FILE", "./token_caps_state.json")
)

token_caps = TokenCapTracker(
    state_file=TOKEN_CAPS_STATE_FILE, enabled=TOKEN_CAPS_ENABLED
)
token_caps.load()
```

Add helpers:

```python
def _effective_input_cap_for(provider: dict, model: str) -> int | None:
    return token_caps.effective_input_cap(
        provider["name"], model, int(provider.get("skip_if_tokens_over") or 0)
    )


def _effective_output_cap_for(provider: dict, model: str) -> int | None:
    return token_caps.effective_output_cap(
        provider["name"], model, int(provider.get("max_output_tokens") or 0)
    )


def _apply_output_token_cap(body: dict, provider: dict, model: str) -> None:
    out_cap = _effective_output_cap_for(provider, model)
    if not out_cap:
        return
    for field in ("max_tokens", "max_completion_tokens"):
        if isinstance(body.get(field), int) and body[field] > out_cap:
            log.info(
                f"  clamping {field} {body[field]}→{out_cap} "
                f"for {provider['name']}/{model}"
            )
            body[field] = out_cap
```

Replace provider-wide skip block (~5218–5225) with per-candidate skip:

```python
        cap = _effective_input_cap_for(provider, model)
        if cap and est_tokens >= cap:
            log.info(f"⤳ skipping {name}/{model} (~{est_tokens} tok >= {cap} cap)")
            continue  # next candidate — do NOT add to skip_providers
```

In `forward()`, replace the `out_cap = provider.get("max_output_tokens"...)` block with `_apply_output_token_cap(body, provider, body["model"])`.

In `/v1/status`, after `model_caps`, add:

```python
            entry["token_caps"] = {
                m: (snap := token_caps.snapshot(p["name"], m))
                for m in p["models"]
                if (snap := token_caps.snapshot(p["name"], m))
            }
```

(Use a plain loop if walrus in dict-comp is awkward.)

On ratings persist / shutdown next to `rate_limiter.flush()`, also `token_caps.flush()`.

Add to `.gitignore`:

```
token_caps_state.json
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_token_caps.py tests/test_token_caps_router.py -v`

Expected: all PASS. Also: `pytest tests/ -q` — no regressions.

- [ ] **Step 5: Commit**

```bash
git add router.py tests/test_token_caps_router.py .gitignore
git commit -m "$(cat <<'EOF'
feat: wire TokenCapTracker into skip, clamp, and status

Per-candidate input caps replace provider-wide skip when learned values exist.
EOF
)"
```

---

### Task 4: Learn from traffic (failures + near-cap successes)

**Files:**
- Modify: `router.py` (try-loop around 413 / 400 / success paths ~5307–5420)
- Modify: `tests/test_token_caps_router.py`

**Interfaces:**
- Consumes: `classify_token_limit_error`, `token_caps.on_token_limit_failure`, `on_success_near_cap`
- Produces: `_learn_token_cap_from_error`, `_learn_token_cap_from_success`; live learning at call sites

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_token_caps_router.py`:

```python
def test_learn_from_classified_413(monkeypatch, tmp_path):
    caps = TokenCapTracker(state_file=tmp_path / "c.json", enabled=True)
    monkeypatch.setattr(router, "token_caps", caps)
    monkeypatch.setattr(router, "TOKEN_CAPS_ENABLED", True)
    router._learn_token_cap_from_error(
        provider_name="groq",
        model="llama",
        status_code=413,
        body="Payload Too Large",
        est_tokens=6000,
        requested_max_tokens=1024,
    )
    assert caps.effective_input_cap("groq", "llama", 0) is not None
    assert caps.effective_input_cap("groq", "llama", 0) < 6000


def test_unrelated_400_does_not_learn(monkeypatch, tmp_path):
    caps = TokenCapTracker(state_file=tmp_path / "c.json", enabled=True)
    monkeypatch.setattr(router, "token_caps", caps)
    router._learn_token_cap_from_error(
        provider_name="groq",
        model="llama",
        status_code=400,
        body="invalid tool schema",
        est_tokens=6000,
        requested_max_tokens=1024,
    )
    assert caps.snapshot("groq", "llama") is None


def test_success_near_cap_nudge_helper(monkeypatch, tmp_path):
    caps = TokenCapTracker(state_file=tmp_path / "c.json", enabled=True)
    caps.seed_from_metadata("groq", "llama", max_input=1000)
    monkeypatch.setattr(router, "token_caps", caps)
    router._learn_token_cap_from_success(
        provider_name="groq",
        model="llama",
        prompt_tokens=900,
        completion_tokens=10,
    )
    assert caps.effective_input_cap("groq", "llama", 0) > 1000
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_token_caps_router.py -k learn -v`

Expected: FAIL (helpers missing).

- [ ] **Step 3: Implement learning helpers and call sites**

```python
from token_caps import classify_token_limit_error


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
) -> None:
    if not TOKEN_CAPS_ENABLED:
        return
    if prompt_tokens:
        token_caps.on_success_near_cap(
            provider_name, model, "input", int(prompt_tokens)
        )
    if completion_tokens:
        token_caps.on_success_near_cap(
            provider_name, model, "output", int(completion_tokens)
        )
```

In the try-loop, before cascading on 413 / 400:

```python
            if resp.status_code in (400, 413):
                _body_txt = ""
                try:
                    _body_txt = resp.text[:500]
                except Exception:
                    pass
                _req_max = 0
                for _f in ("max_tokens", "max_completion_tokens"):
                    if isinstance(payload.get(_f), int):
                        _req_max = max(_req_max, payload[_f])
                _learn_token_cap_from_error(
                    provider_name=name,
                    model=model,
                    status_code=resp.status_code,
                    body=_body_txt,
                    est_tokens=est_tokens,
                    requested_max_tokens=_req_max,
                )
```

Keep existing cascade behavior after learning (413 → `skip_providers`; 400 → break model).

On success paths where usage is known, call `_learn_token_cap_from_success` with `prompt_tokens` / `completion_tokens`. If only `total_tokens` is known, skip near-cap learning.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_token_caps.py tests/test_token_caps_router.py -v`

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add router.py tests/test_token_caps_router.py
git commit -m "$(cat <<'EOF'
feat: learn token caps from 413/token-400 failures and near-cap successes

Cuts on classified limit errors; gentle raises when usage sits near the ceiling.
EOF
)"
```

---

### Task 5: Seed from `/models`, docs, and feature flag surfacing

**Files:**
- Modify: `router.py` (`_discover_models_with_catalog` ~1224–1268)
- Modify: `tests/test_token_caps_router.py`
- Modify: `.env.example`
- Modify: `documentation/configuration.md`
- Modify: `website/src/content/docs/configuration.md`
- Optionally: `_features_snapshot()` add-on `token_caps`

**Interfaces:**
- Consumes: `extract_caps_from_model_item`, `token_caps.seed_from_metadata`
- Produces: caps seeded from `/models` fields; documented env knobs

- [ ] **Step 1: Write the failing seed test**

```python
def test_discover_seeds_context_length(monkeypatch, tmp_path):
    caps = TokenCapTracker(state_file=tmp_path / "c.json", enabled=True)
    monkeypatch.setattr(router, "token_caps", caps)
    monkeypatch.setattr(router, "TOKEN_CAPS_ENABLED", True)
    monkeypatch.setattr(router, "FILTER_SPECIALIZED_MODELS", False)

    catalog = [
        {
            "id": "llama-3.3-70b-versatile",
            "context_length": 8192,
            "max_completion_tokens": 4096,
        },
        {"id": "whisper-1"},
    ]

    class _Resp:
        status_code = 200

        def json(self):
            return {"data": catalog}

    monkeypatch.setattr(router._HTTP, "get", lambda *a, **k: _Resp())
    provider = {
        "name": "groq",
        "base_url": "https://api.groq.com/openai/v1",
        "headers": {},
    }
    found = router._discover_models(provider, key="sk-test")
    assert "llama-3.3-70b-versatile" in found
    assert caps.effective_input_cap("groq", "llama-3.3-70b-versatile", 0) == 8192
    assert caps.effective_output_cap("groq", "llama-3.3-70b-versatile", 0) == 4096
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_token_caps_router.py::test_discover_seeds_context_length -v`

Expected: FAIL (caps still unset).

- [ ] **Step 3: Seed inside discovery + docs**

In `_discover_models_with_catalog`, after normalizing each item id, when `TOKEN_CAPS_ENABLED`:

```python
            from token_caps import extract_caps_from_model_item  # or top-level import
            max_in, max_out = extract_caps_from_model_item(item)
            if max_in or max_out:
                token_caps.seed_from_metadata(
                    provider["name"], normalized, max_in, max_out
                )
```

Update `.env.example` near SKIP_TOKENS / RATE_STATE:

```bash
# Adaptive per-model token caps (metadata + learn from 413/token-limit 400s)
# TOKEN_CAPS=1
# TOKEN_CAPS_STATE_FILE=./token_caps_state.json
# {PROVIDER}_SKIP_TOKENS_OVER / {PROVIDER}_MAX_OUTPUT_TOKENS remain outer fences.
```

Docs subsection (both `documentation/configuration.md` and website mirror), near rate-limiter or capability overrides:

```markdown
### Adaptive per-model token caps

hermes-router tracks effective input/output token ceilings per `(provider, model)`.
It seeds from `/models` metadata when available and tightens from classified
413 / token-limit 400 responses (gentle raises on near-cap successes).

| Var | Default | Notes |
|---|---|---|
| `TOKEN_CAPS` | `1` | `0` disables adaptive caps (static env/defaults only) |
| `TOKEN_CAPS_STATE_FILE` | `./token_caps_state.json` | Persisted learned/metadata caps |

`{PROVIDER}_SKIP_TOKENS_OVER` and `{PROVIDER}_MAX_OUTPUT_TOKENS` remain provider-wide
outer fences — learned values may only tighten further inside them. Caps appear under
each provider in `/v1/status` as `token_caps`.
```

Also update the capability-overrides table rows for SKIP/MAX_OUTPUT to note they are outer fences when token caps are enabled.

Optional: in `_features_snapshot()` addons, add flag add-on `token_caps` mirroring `TOKEN_CAPS`.

- [ ] **Step 4: Run full verification**

Run:

```bash
pytest tests/test_token_caps.py tests/test_token_caps_router.py \
  tests/test_specialized_models.py tests/test_rate_limiter.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add router.py tests/test_token_caps_router.py .env.example \
  documentation/configuration.md website/src/content/docs/configuration.md
git commit -m "$(cat <<'EOF'
feat: seed token caps from /models metadata and document TOKEN_CAPS

Wire discovery seeding and operator-facing configuration for adaptive ceilings.
EOF
)"
```

---

## Self-Review (plan vs spec)

| Spec requirement | Task |
|---|---|
| `TokenCapTracker` sibling of TBF | Task 1 |
| Per-`(provider, model)` state + persist | Task 1 |
| Effective formula + env fence | Task 1 + 3 |
| Metadata extraction from `/models` | Task 2 + 5 |
| Classifier; uncertain 400s skip learning | Task 2 + 4 |
| Skip oversized + clamp max_tokens | Task 3 |
| Cut on failure / raise near-cap success | Task 1 + 4 |
| `TOKEN_CAPS=0` escape hatch | Task 1 + 3 |
| `/v1/status` exposure | Task 3 |
| Docs / `.env.example` / gitignore | Task 3 + 5 |
| Unit + mocked integration tests | Tasks 1–5 |
| No active probing / no TBF fold-in | Honored (out of scope) |

No TBD placeholders. Constant and helper names are consistent across tasks.
