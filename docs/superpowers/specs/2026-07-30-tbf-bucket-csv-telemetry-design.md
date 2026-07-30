# TBF Bucket CSV Telemetry — Design Spec

**Date:** 2026-07-30  
**Project:** hermes-router  
**Status:** Approved for implementation planning

---

## 1. Goal

Give developers an opt-in, Calc-friendly CSV of adaptive token-bucket filter (TBF)
**cap** and **headroom** at each learning event, so they can see how bucket sizes
evolve and converge and spot tuning opportunities (nudge %, cut factors, priors,
provider-wide multipliers).

This complements the existing **Rate limits** dashboard (current snapshot only).
That page stays as-is; historical charts remain out of scope.

---

## 2. Scope

**In scope**
- Event-driven CSV append when a bucket **cap** actually changes
- Columns covering identity, event/reason, `cap` / `old_cap`, fill (`tokens`,
  `used`), and post-mutation `headroom`
- Env opt-in: enable flag + optional file path
- Append across restarts (operator deletes/rotates the file)
- Soft-fail I/O so routing/learning never breaks on a bad path
- Unit tests + configuration docs for the new env vars

**Out of scope**
- Periodic snapshots or headroom-threshold-only rows
- Dashboard time-series / Prometheus series for these events
- Adaptive **token caps** (413 / context-limit) CSV
- Auto-rotation, compression, or multi-file sharding
- VS Code extension changes

---

## 3. Architecture

Hook a tiny CSV writer into the existing adaptive limiter (`rate_limiter.py`),
not a separate service or dashboard API.

```text
TokenBucket / BucketGroup learn paths
  (nudge, 429 cut, header pin, ensure_fits lift)
        |
        v  if RATE_BUCKET_CSV_ENABLED
  BucketEventCsv.append(row…)
        |
        v
  RATE_BUCKET_CSV  (append-only file)
```

**Why here:** learn mutations already know `old_cap`, new `cap`, and live
`tokens`/`headroom`. Log scraping would miss fields and break easily.

**Behavior when disabled:** no file open, no lock contention beyond a cheap
flag check — zero effect on the hot path beyond that check at learn sites
(learn sites are already infrequent vs admit/consume).

---

## 4. Configuration

| Env var | Default | Meaning |
|---|---|---|
| `RATE_BUCKET_CSV_ENABLED` | off | On when `1` / `true` / `yes` (case-insensitive) |
| `RATE_BUCKET_CSV` | `./rate_bucket_events.csv` | Destination path when enabled |

Document both in the adaptive upstream rate limiter env table in
`documentation/configuration.md` (and the website mirror if that tree is kept
in sync for this section).

---

## 5. CSV schema

**Header (fixed column order):**

```text
datetime,provider,key_hint,model,scope,bucket,event,reason,cap,old_cap,tokens,used,headroom
```

| Column | Meaning |
|---|---|
| `datetime` | ISO-8601 **local** time with offset, e.g. `2026-07-30T13:14:02.123+03:00` (first column for Calc) |
| `provider` | Provider name |
| `key_hint` | Last-8 key suffix (same convention as TBF `_group_key`) |
| `model` | Model id, or empty string for provider-wide scope |
| `scope` | `model` or `provider_wide` |
| `bucket` | Limit name (`RPM`, `TPM`, `RPD`, …) |
| `event` | `nudge` \| `cut` \| `header_pin` \| `lift` |
| `reason` | `success_streak` \| `hard_429` \| `soft_429` \| `header` \| `request_burst` \| `soft_floor` |
| `cap` | Cap after mutation |
| `old_cap` | Cap before mutation |
| `tokens` | Tokens remaining after mutation (after refill semantics already applied by the mutator) |
| `used` | `max(0, cap - tokens)` after mutation |
| `headroom` | `tokens / cap` clamped to `[0, 1]` after mutation; `1.0` if `cap <= 0` |

Use Python’s `csv` module (Excel/Calc-friendly quoting). One data row per
bucket that changed. Numeric fields as plain numbers (no thousands separators).

**File lifecycle**
- Always **append** across process restarts.
- Write the header row only when the file does not exist or has size `0`.
- Operator deletes or moves the file to reset history.

---

## 6. Events (when to emit)

Emit **only** when the bucket’s `cap` (or an equivalent applied ceiling) actually
changes. Skip no-ops.

| Source | `event` | `reason` | Notes |
|---|---|---|---|
| `TokenBucket.on_success` after streak nudge raises `cap` | `nudge` | `success_streak` | Not on intermediate streak increments |
| `TokenBucket.on_429` hard cut | `cut` | `hard_429` | |
| `TokenBucket.on_429` soft cut | `cut` | `soft_429` | |
| Soft cut where the applied cap is the soft floor | `cut` | `soft_floor` | Prefer over `soft_429` when flooring determined the final cap |
| `TokenBucket.set_from_header` returns True | `header_pin` | `header` | Includes newly created header buckets; skip stale rejects |
| `TokenBucket.ensure_fits` raises cap | `lift` | `request_burst` | Skip when amount already fits |

**Identity fields** (`provider`, `key_hint`, `model`, `scope`) must be available
at the call site. Prefer recording from `AdaptiveRateLimiter` / `BucketGroup`
wrappers that already know the group key, rather than from bare `TokenBucket`
methods that lack scope context — pass a small context object or record after
the bucket mutates inside the group/limiter method.

**Headroom:** sample **after** the mutation on that same bucket (not group min
headroom). Group-min headroom can be derived later in Calc if needed by joining
rows; v1 does not emit extra aggregate rows.

---

## 7. Implementation sketch

1. **`BucketEventCsv` helper** (same module or a tiny adjacent helper used only
   by `rate_limiter.py`):
   - Reads enable flag + path once (or on first use).
   - Holds a `threading.Lock` for append.
   - `record(...)` builds one row and writes it.
2. **Wire-up** at the learn call sites listed in §6, after a successful cap change.
3. **Errors:** on write/`mkdir` failure, `log.warning` and return; never raise into
   the request/learn path. Optionally suppress repeat warnings with a simple
   “last warn at” throttle so a full disk does not spam logs every 429.
4. **Parent dirs:** `Path.parent.mkdir(parents=True, exist_ok=True)` once before
   first successful open; if mkdir fails, soft-fail as above.

No changes to `/v1/rate-limits`, dashboard HTML, or Prometheus metrics.

---

## 8. Error handling & edge cases

- Disabled → no file creation.
- Unwritable path / permission error → warn, continue routing.
- Concurrent learns → lock serializes CSV lines (no torn rows).
- Empty `model` for provider-wide; never write full API keys.
- Header pin: emit `header_pin` only when `cap` changes or the bucket is newly
  created. Remaining-only refreshes (same cap) must not write a row.
- Process crash mid-write: acceptable; Calc may see a truncated last line —
  no fsync requirement for v1.

---

## 9. Testing

**Unit tests** (`tests/test_rate_limiter.py` or adjacent):

- Enabled + temp path: nudge / hard 429 / soft 429 / header create / lift each
  append expected columns; header written once.
- Second event appends without duplicating the header.
- Disabled: no file created.
- No-op `ensure_fits` / failed streak / stale header: no row.
- Unwritable path (e.g. file mocked to raise): learn method still completes;
  no exception propagates.

**Docs:** env vars listed; one sentence that the CSV is append-only event
telemetry for local analysis in Calc/Excel.

---

## 10. Success criteria

- With `RATE_BUCKET_CSV_ENABLED=1`, driving traffic that learns limits produces a
  CSV openable in LibreOffice Calc whose first column is datetime and whose
  `cap` / `old_cap` / `headroom` columns show learning steps.
- With the flag off, behavior and I/O match today’s limiter.
- A full-disk or bad path cannot turn learns into request failures.

---

## 11. Relationship to existing work

Builds on the adaptive TBF (`rate_limiter.py`) and the Rate limits dashboard
design (2026-07-29), which explicitly deferred historical charts. This spec
fills that gap with the simplest ops-friendly artifact (CSV), not charts.
`)
