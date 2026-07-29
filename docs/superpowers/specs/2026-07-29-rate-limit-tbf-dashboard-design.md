# Rate-Limit TBF Dashboard View — Design Spec

**Date:** 2026-07-29  
**Project:** hermes-router  
**Status:** Approved for implementation planning

---

## 1. Goal

Give operators a dedicated browser dashboard page to **monitor and debug** the adaptive
token-bucket filter (TBF): an at-a-glance flat list of rate-limit scopes, with drill-down
into per-bucket detail, plus light actions to clear learned state.

This complements the existing Providers “Rate headroom” bar (min headroom across active
buckets for the current model). That bar stays as the lightweight signal; this page is the
ops-depth view.

---

## 2. Scope

**In scope**
- New `/dashboard` nav page: **Rate limits**
- Dedicated read API listing all TBF groups in a flat table shape
- Overview → row click → bucket detail
- Toggle: show dormant / orphan groups (default off = configured scopes only)
- Light action: clear a group’s learned state (relearn from traffic/headers)
- Limiter helpers to enumerate groups, classify configured vs orphan, and clear by id

**Out of scope**
- VS Code extension dashboard
- Editing caps or activating/deactivating individual buckets from the UI
- Changing Providers page headroom-bar behaviour
- Historical charts / time-series of headroom

---

## 3. Architecture

Three pieces:

1. **`AdaptiveRateLimiter`** — enumerate persisted/in-memory groups; expose list + clear;
   classify each group as configured vs orphan against the live provider pool.
2. **HTTP API** — `GET /v1/rate-limits` and `POST /v1/rate-limits/clear`, authenticated like
   other dashboard `/v1/*` routes (Bearer proxy API key).
3. **Dashboard UI** — new nav page that polls GET on the dashboard’s existing refresh cadence;
   flat table + detail panel + orphan toggle + clear-with-confirm.

```text
Browser /dashboard (Rate limits page)
        |  GET /v1/rate-limits?include_orphans=0|1
        |  POST /v1/rate-limits/clear { id }
        v
   router.py routes
        v
   AdaptiveRateLimiter.list_groups / clear_group
        v
   in-memory _groups (+ flush to RATE_STATE_FILE)
```

Do **not** overload `/v1/status` with the full TBF dump. Status already embeds a narrow
per-provider `rate_limits` snapshot (current key × models) for the Providers headroom bar.
The dedicated endpoint owns the flat ops view (all keys, orphans, inactive bucket detail).

---

## 4. Data model

### 4.1 Group identity

One table row = one limiter group, keyed the same way as persistence today:

- Provider-wide: `provider:{name}|key:{suffix}`
- Model-specific: `provider:{name}|key:{suffix}|model:{model}`

`suffix` is the existing last-8-characters convention from `_group_key` (not the full API
key). The list `id` field is this opaque group key string.

### 4.2 List / detail fields

| Field | Meaning |
|---|---|
| `id` | Opaque group key (for select/clear) |
| `provider` | Provider name |
| `key_hint` | Masked key display (derived from suffix; never full secret) |
| `model` | Model name, or `null` for provider-wide |
| `scope` | `"provider_wide"` \| `"model"` |
| `configured` | `true` if provider + matching live key (by suffix) exist, and for model scopes the model is still in that provider’s model list |
| `headroom` | Min headroom across **active** buckets; if none active, `null` (UI shows “—” and sorts these rows last) |
| `binding` | Name of the active bucket with lowest headroom, or `null` |
| `buckets` | Map of limit name → `{cap, used, tokens, headroom, active}` |

`used` = `max(0, cap - tokens)` after refill, consistent with the existing `snapshot()` shape.
Detail includes inactive buckets; list-level headroom/binding ignore inactive buckets.

### 4.3 Configured vs orphan

- **Configured:** provider still exists; at least one live pool key’s suffix matches the
  group’s key suffix; if `scope == model`, that model is still listed for the provider.
- **Orphan / dormant group:** anything else still present in limiter state (removed keys,
  removed models, stale groups). Shown only when `include_orphans=1`.

Matching by suffix inherits the existing collision risk if two keys share the same last 8
characters; this design does not change that convention.

---

## 5. API

### 5.1 `GET /v1/rate-limits`

Query:

| Param | Default | Meaning |
|---|---|---|
| `include_orphans` | `0` | When `1`/`true`, include `configured: false` groups |

Response:

```json
{
  "generated_at": 1730000000.0,
  "groups": [ /* fields from §4.2 */ ]
}
```

Default response omits orphans. Groups may be returned unsorted; the UI sorts
attention-first (lowest headroom first; no-active-buckets last).

Auth failure → same 401 behaviour as `/v1/status`.

### 5.2 `POST /v1/rate-limits/clear`

Body:

```json
{ "id": "provider:groq|key:abcd1234|model:llama-3.1-70b" }
```

Effect:

1. Remove that group from in-memory `_groups` if present (`clear_group` → `True`).
2. Flush state to `RATE_STATE_FILE` immediately so disk drops it (do not wait only for the
   periodic flush loop).
3. Next traffic that needs that scope recreates defaults and relearns from headers/429s.

Errors:

- Missing/invalid body → 400
- Unknown `id` → 404
- Unauthorized → 401

No other mutations (no cap edits, no per-bucket activate/deactivate).

---

## 6. Dashboard UI

### 6.1 Navigation

Add **Rate limits** alongside Overview, Providers, Provider Keys, etc.

### 6.2 Flat table (overview)

Columns: Provider | Key | Model / scope | Binding bucket | Headroom (bar + %) | Bucket count.

- Sort: lowest headroom first; rows with `headroom == null` (no active buckets) last.
- Toolbar: checkbox **Show dormant / orphan groups** (drives `include_orphans`).
- Refresh: same poll/manual refresh path as the rest of the dashboard (separate fetch to
  `/v1/rate-limits`, not only `/v1/status`).
- Row click selects one group and opens detail.

### 6.3 Detail panel

Shown for the selected row (below or beside the table; one selection at a time):

- Header: provider · key hint · model/scope · configured badge
- Bucket table: name | active | cap | used | tokens left | headroom %
- Inactive buckets listed muted
- **Clear learned state** → confirm dialog → POST clear → clear selection / refresh list

### 6.4 Empty states

- No groups at all: “No rate data yet”
- Orphan toggle on but only empty orphan set while configured empty: still the no-data message
  as appropriate; if configured empty and orphans exist, prompt to enable the toggle only when
  default view is empty and orphans are available (optional UX nicety; not required for v1)

### 6.5 Existing Providers UI

Leave the Advanced Provider Details **Rate headroom** column unchanged.

---

## 7. Error handling & edge cases

- Clear of unknown id → UI shows a short error message and refreshes the list.
- Clear while traffic recreates the group → acceptable race; refresh shows a fresh default group.
- Provider-wide and model groups are separate flat rows (not nested).
- Orphan toggle filters **groups** (`configured`), not inactive **buckets** inside a live group.
- Groups with only inactive buckets: list shows headroom “—” and binding “—” (API `headroom`
  and `binding` are `null`); detail still shows those buckets until cleared.
- Never expose full API keys in API responses or HTML.

---

## 8. Testing

**Unit (`tests/test_rate_limiter.py` or adjacent):**

- `list_groups(include_orphans=False/True)` filtering
- `configured` classification against a fake/live key suffix set
- `clear_group(id)` returns `True` if removed, `False` if unknown; route maps `False` → 404
- Bucket field shape: `cap`, `used`, `tokens`, `headroom`, `active`

**API (thin route tests if the project pattern supports Flask test client):**

- GET default excludes orphans; `include_orphans=1` includes them
- POST clear success / 404 / 401

**UI:** no HTML/JS browser tests required in CI; follow existing inline dashboard JS patterns.

---

## 9. Success criteria

- Operator can open **Rate limits**, see which configured scopes are near empty, and identify
  the binding bucket without reading logs.
- Clicking a row reveals every bucket (active and inactive) with cap/used/headroom.
- Orphan toggle reveals stale groups after key/model removal.
- Clear removes learned state for one group and the UI reflects it after refresh.
- Providers headroom bar and `/v1/status` rate_limits snapshot behaviour remain intact.
`)