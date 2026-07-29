# Rate-Limit TBF Dashboard View — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a browser dashboard **Rate limits** page backed by `GET/POST /v1/rate-limits` that lists TBF groups, drills into per-bucket detail, toggles orphans, and clears learned state.

**Architecture:** Extend `AdaptiveRateLimiter` with `list_groups` / `clear_group`. Router builds the set of configured group ids from `PROVIDERS`, exposes thin authenticated routes, and adds a new inline-dashboard page that polls the dedicated endpoint (leaving `/v1/status` and the Providers headroom bar unchanged).

**Tech Stack:** Python 3.10+, existing Flask routes in `router.py`, inline dashboard HTML/JS in `router.py`, `pytest` in `tests/test_rate_limiter.py` — no new dependencies.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-29-rate-limit-tbf-dashboard-design.md`
- Do not change Providers headroom-bar behaviour or `/v1/status` `rate_limits` snapshot shape.
- Never expose full API keys — only the existing last-8 suffix / `key_hint`.
- Group `id` is the existing `_group_key` string (`provider:…|key:…` / `…|model:…`).
- `clear_group` must flush `RATE_STATE_FILE` immediately on success.
- VS Code extension is out of scope.
- No new pip dependencies.
- Tests live in `tests/test_rate_limiter.py` (and a small pure helper testable without booting the full app when practical); run with `pytest tests/`.
- Keep `router.py` as the integration target — do not extract a separate dashboard package.

## File structure

| File | Responsibility |
|---|---|
| `rate_limiter.py` | `parse_group_key`, `list_groups`, `clear_group` on `AdaptiveRateLimiter` |
| `tests/test_rate_limiter.py` | Unit tests for list/clear/parse/configured filtering |
| `router.py` | `_configured_rate_group_ids()`, `GET /v1/rate-limits`, `POST /v1/rate-limits/clear`, dashboard nav + page + JS |

---

### Task 1: `clear_group` + immediate flush

**Files:**
- Modify: `rate_limiter.py` (`AdaptiveRateLimiter`)
- Test: `tests/test_rate_limiter.py`

**Interfaces:**
- Consumes: existing `_groups`, `flush()`
- Produces:
  - `AdaptiveRateLimiter.clear_group(self, group_id: str) -> bool`
    - If `group_id` in `_groups`: delete it, call `self.flush()`, return `True`
    - Else: return `False` (no flush)

- [ ] **Step 1: Write the failing tests**

```python
def test_clear_group_removes_and_flushes(tmp_path):
    rl = make_limiter(tmp_path)
    rl.check_and_consume("groq", "key-abc12345", "llama", 1.0, 50.0)
    gk = AdaptiveRateLimiter._group_key("groq", "key-abc12345", None)
    assert gk in rl._groups or AdaptiveRateLimiter._group_key("groq", "key-abc12345", "llama") in rl._groups
    # Clear the provider-wide group that check_and_consume created
    pw = AdaptiveRateLimiter._group_key("groq", "key-abc12345", None)
    mg = AdaptiveRateLimiter._group_key("groq", "key-abc12345", "llama")
    assert rl.clear_group(pw) is True
    assert pw not in rl._groups
    # Disk no longer contains pw
    rl.flush()  # ensure file readable; clear_group already flushed
    doc = json.loads((Path(tmp_path) / "rate_limits_state.json").read_text())
    assert pw not in (doc.get("groups") or {})

def test_clear_group_unknown_returns_false(tmp_path):
    rl = make_limiter(tmp_path)
    assert rl.clear_group("provider:nope|key:deadbeef") is False
```

Add `import json` at top of the test file if missing. Import `AdaptiveRateLimiter` is already present.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_rate_limiter.py::test_clear_group_removes_and_flushes tests/test_rate_limiter.py::test_clear_group_unknown_returns_false -v`

Expected: FAIL with `AttributeError: ... clear_group`

- [ ] **Step 3: Implement `clear_group`**

In `rate_limiter.py` on `AdaptiveRateLimiter`:

```python
def clear_group(self, group_id: str) -> bool:
    with self._lock:
        if group_id not in self._groups:
            return False
        del self._groups[group_id]
    self.flush()
    return True
```

Note: release the lock before `flush()` because `flush()` takes `_lock` itself (avoid deadlock).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_rate_limiter.py::test_clear_group_removes_and_flushes tests/test_rate_limiter.py::test_clear_group_unknown_returns_false -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add rate_limiter.py tests/test_rate_limiter.py
git commit -m "feat: add AdaptiveRateLimiter.clear_group with immediate flush"
```

---

### Task 2: `parse_group_key` + `list_groups`

**Files:**
- Modify: `rate_limiter.py`
- Test: `tests/test_rate_limiter.py`

**Interfaces:**
- Consumes: `_groups`, `TokenBucket.headroom` / refill semantics, `_group_key`
- Produces:
  - `AdaptiveRateLimiter.parse_group_key(group_id: str) -> dict | None`
    - Success: `{"provider": str, "key_hint": str, "model": str | None}`
    - Malformed id: `None`
  - `AdaptiveRateLimiter.list_groups(self, include_orphans: bool = False, configured_ids: set[str] | None = None) -> list[dict]`
    - Each dict keys: `id`, `provider`, `key_hint`, `model`, `scope` (`"provider_wide"`|`"model"`), `configured` (bool), `headroom` (`float | None`), `binding` (`str | None`), `buckets` (dict)
    - Bucket entry: `{"cap": float, "used": float, "tokens": float, "headroom": float, "active": bool}` (rounded like `snapshot`: cap/used/tokens to 1 decimal, headroom to 3)
    - `headroom`/`binding`: min across **active** buckets only; if no active buckets → both `None`
    - If `configured_ids` is `None`, treat as empty set (everything orphan unless include_orphans)
    - When `include_orphans` is False, omit groups whose `id` not in `configured_ids`

- [ ] **Step 1: Write the failing tests**

```python
def test_parse_group_key_provider_wide():
    d = AdaptiveRateLimiter.parse_group_key("provider:groq|key:abc12345")
    assert d == {"provider": "groq", "key_hint": "abc12345", "model": None}

def test_parse_group_key_model():
    d = AdaptiveRateLimiter.parse_group_key("provider:groq|key:abc12345|model:llama")
    assert d == {"provider": "groq", "key_hint": "abc12345", "model": "llama"}

def test_parse_group_key_malformed():
    assert AdaptiveRateLimiter.parse_group_key("not-a-key") is None

def test_list_groups_filters_orphans(tmp_path):
    rl = make_limiter(tmp_path)
    rl.check_and_consume("groq", "key-abc12345", "llama", 1.0, 50.0)
    pw = AdaptiveRateLimiter._group_key("groq", "key-abc12345", None)
    mg = AdaptiveRateLimiter._group_key("groq", "key-abc12345", "llama")
    # Only mark provider-wide as configured
    rows = rl.list_groups(include_orphans=False, configured_ids={pw})
    ids = {r["id"] for r in rows}
    assert pw in ids
    assert mg not in ids
    rows_all = rl.list_groups(include_orphans=True, configured_ids={pw})
    ids_all = {r["id"] for r in rows_all}
    assert pw in ids_all and mg in ids_all
    by_id = {r["id"]: r for r in rows_all}
    assert by_id[pw]["configured"] is True
    assert by_id[mg]["configured"] is False
    assert by_id[pw]["scope"] == "provider_wide"
    assert by_id[mg]["scope"] == "model"
    assert by_id[mg]["model"] == "llama"
    assert "RPM" in by_id[pw]["buckets"] or len(by_id[pw]["buckets"]) >= 1
    b = next(iter(by_id[pw]["buckets"].values()))
    assert set(b) >= {"cap", "used", "tokens", "headroom", "active"}

def test_list_groups_headroom_null_when_no_active(tmp_path):
    rl = make_limiter(tmp_path)
    g = rl.get_group("groq", "key-abc12345", None)
    for b in g.buckets.values():
        b.active = False
    pw = AdaptiveRateLimiter._group_key("groq", "key-abc12345", None)
    rows = rl.list_groups(include_orphans=True, configured_ids={pw})
    row = next(r for r in rows if r["id"] == pw)
    assert row["headroom"] is None
    assert row["binding"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_rate_limiter.py::test_parse_group_key_provider_wide tests/test_rate_limiter.py::test_parse_group_key_model tests/test_rate_limiter.py::test_parse_group_key_malformed tests/test_rate_limiter.py::test_list_groups_filters_orphans tests/test_rate_limiter.py::test_list_groups_headroom_null_when_no_active -v`

Expected: FAIL (`parse_group_key` / `list_groups` missing)

- [ ] **Step 3: Implement `parse_group_key` and `list_groups`**

```python
@staticmethod
def parse_group_key(group_id: str) -> dict | None:
    # Expect: provider:{name}|key:{hint}[|model:{model}]
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

def list_groups(self, include_orphans: bool = False,
                configured_ids: set[str] | None = None) -> list[dict]:
    configured_ids = configured_ids or set()
    now = time.time()
    with self._lock:
        items = list(self._groups.items())
    out = []
    for gk, g in items:
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
```

Locking note: refill/headroom mutate bucket state. Prefer holding `_lock` for the whole serialization, or copy under lock then refill outside — match existing `snapshot()` (holds lock for the whole call). Prefer holding `_lock` for the entire `list_groups` body for consistency with `snapshot()`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_rate_limiter.py::test_parse_group_key_provider_wide tests/test_rate_limiter.py::test_parse_group_key_model tests/test_rate_limiter.py::test_parse_group_key_malformed tests/test_rate_limiter.py::test_list_groups_filters_orphans tests/test_rate_limiter.py::test_list_groups_headroom_null_when_no_active -v`

Expected: PASS

Also run: `pytest tests/test_rate_limiter.py -v` — all existing tests still PASS.

- [ ] **Step 5: Commit**

```bash
git add rate_limiter.py tests/test_rate_limiter.py
git commit -m "feat: add list_groups and parse_group_key for TBF dashboard"
```

---

### Task 3: Configured-id helper + HTTP routes

**Files:**
- Modify: `router.py` (near other `/v1/` status/config routes; after `_auth_check` users)
- No new test file required for Flask (repo has no Flask test client suite). Cover helper logic with a tiny pure unit if extracted; otherwise verify with manual curl in Step 4.

**Interfaces:**
- Consumes: `rate_limiter.list_groups`, `rate_limiter.clear_group`, `AdaptiveRateLimiter._group_key`, `PROVIDERS`, `_auth_check`
- Produces:
  - `_configured_rate_group_ids() -> set[str]`
  - `GET /v1/rate-limits?include_orphans=0|1` → JSON `{generated_at, groups}`
  - `POST /v1/rate-limits/clear` body `{id}` → `{ok: true}` or 400/404/401

- [ ] **Step 1: Add `_configured_rate_group_ids`**

Place near the rate_limiter globals / status helpers in `router.py`:

```python
def _configured_rate_group_ids() -> set[str]:
    """Group ids that match currently configured provider keys/models."""
    ids: set[str] = set()
    for p in PROVIDERS:
        name = p["name"]
        keys = p.get("keys") or []
        models = list(p.get("models") or ([p.get("model")] if p.get("model") else []))
        if p.get("embed_model"):
            models.append(p["embed_model"])
        models = [m for m in dict.fromkeys(models) if m]
        for key in keys:
            ids.add(AdaptiveRateLimiter._group_key(name, key, None))
            for m in models:
                ids.add(AdaptiveRateLimiter._group_key(name, key, m))
    return ids
```

Import `AdaptiveRateLimiter` is already present via `from rate_limiter import AdaptiveRateLimiter, ...`.

- [ ] **Step 2: Add GET and POST routes**

```python
@app.route("/v1/rate-limits")
def rate_limits_list():
    err = _auth_check()
    if err:
        return err
    raw = (request.args.get("include_orphans") or "0").strip().lower()
    include = raw in ("1", "true", "yes")
    groups = rate_limiter.list_groups(
        include_orphans=include,
        configured_ids=_configured_rate_group_ids(),
    )
    return jsonify({"generated_at": time.time(), "groups": groups})


@app.route("/v1/rate-limits/clear", methods=["POST"])
def rate_limits_clear():
    err = _auth_check()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    gid = body.get("id")
    if not gid or not isinstance(gid, str):
        return jsonify({"error": "id required"}), 400
    if not rate_limiter.clear_group(gid):
        return jsonify({"error": "unknown group"}), 404
    return jsonify({"ok": True})
```

Place these near `/v1/status` (same auth pattern as that route).

- [ ] **Step 3: Sanity-check with a running router (manual)**

With the router running and a valid `PROXY_API_KEYS` value:

```bash
curl -s -H "Authorization: Bearer $PROXY_API_KEY" \
  "http://127.0.0.1:PORT/v1/rate-limits" | python -m json.tool | head

curl -s -H "Authorization: Bearer $PROXY_API_KEY" \
  "http://127.0.0.1:PORT/v1/rate-limits?include_orphans=1" | python -m json.tool | head

curl -s -o /dev/null -w "%{http_code}\n" \
  -H "Authorization: Bearer $PROXY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"id":"provider:nope|key:deadbeef"}' \
  http://127.0.0.1:PORT/v1/rate-limits/clear
# Expected: 404
```

If no live server is available in the agent environment, skip live curl and rely on code review + unit tests from Tasks 1–2; note that in the commit message body is unnecessary — just proceed.

- [ ] **Step 4: Commit**

```bash
git add router.py
git commit -m "feat: expose GET/POST /v1/rate-limits for TBF dashboard"
```

---

### Task 4: Dashboard page — nav, table, detail, clear

**Files:**
- Modify: `router.py` (dashboard HTML/CSS/JS embedded in `dashboard()` / `DASHBOARD_HTML`)

**Interfaces:**
- Consumes: `GET /v1/rate-limits`, `POST /v1/rate-limits/clear`
- Produces: nav item `rate-limits`, page `#page-rate-limits`, render + clear UX

- [ ] **Step 1: Add nav button and page shell**

In the sidebar nav (after Providers is fine):

```html
<button class="nav-item" data-page="rate-limits" onclick="showPage('rate-limits')">Rate limits</button>
```

Add page section:

```html
<section class="page" id="page-rate-limits">
  <div class="panel">
    <div class="panel-header">
      <span class="panel-title">Token bucket filters</span>
      <label class="muted" style="font-size:12px;display:flex;align-items:center;gap:6px;font-weight:500;text-transform:none;letter-spacing:0">
        <input type="checkbox" id="rl-orphans" onchange="refreshRateLimits()">
        Show dormant / orphan groups
      </label>
    </div>
    <div class="page-intro" style="padding:12px 14px 0">
      Live adaptive rate-limit buckets. Click a row for per-bucket detail. Clear drops learned caps for that scope.
    </div>
    <div class="panel-body">
      <table>
        <thead><tr>
          <th>Provider</th><th>Key</th><th>Scope</th>
          <th>Binding</th><th>Headroom</th><th class="right">Buckets</th>
        </tr></thead>
        <tbody id="rl-tbody"></tbody>
      </table>
    </div>
  </div>
  <div class="panel" id="rl-detail-panel" style="display:none;margin-top:12px">
    <div class="panel-header">
      <span class="panel-title" id="rl-detail-title">Detail</span>
      <button class="btn" onclick="clearRateGroup()">Clear learned state</button>
    </div>
    <div class="panel-body">
      <div id="rl-detail-meta" class="muted" style="padding:8px 12px;font-size:12px"></div>
      <table>
        <thead><tr>
          <th>Bucket</th><th>Active</th>
          <th class="right">Cap</th><th class="right">Used</th>
          <th class="right">Left</th><th class="right">Headroom</th>
        </tr></thead>
        <tbody id="rl-detail-tbody"></tbody>
      </table>
    </div>
  </div>
</section>
```

Add minimal CSS for selected row (near existing table styles):

```css
tr.rl-selected td{background:rgba(108,140,255,.10)}
tr.rl-row{cursor:pointer}
```

- [ ] **Step 2: Wire JS state, fetch, render, clear**

Update `PAGES`:

```javascript
const PAGES = ['overview', 'providers', 'rate-limits', 'keys', 'access', 'models', 'addons', 'logs'];
```

Add globals near other data vars:

```javascript
let rateLimitsData = [];
let selectedRateGroupId = null;
```

Extend `refresh()` to also fetch rate limits (do not block the whole dashboard if this fails):

Inside `refresh()`, after status/usage/logs succeed (or in parallel in the same `Promise.all` — prefer parallel):

```javascript
// Add to the Promise.all array:
fetch('/v1/rate-limits?include_orphans=' + (document.getElementById('rl-orphans')?.checked ? '1' : '0'), {headers:h}),
```

Adjust destructuring accordingly and set `rateLimitsData = rl.groups || []`, then call `renderRateLimits()` from `renderAll()`.

If adding a fifth parallel fetch is awkward, call `refreshRateLimits()` at the end of a successful `refresh()` instead:

```javascript
async function refreshRateLimits() {
  if (!apiKey) return;
  try {
    const orphans = document.getElementById('rl-orphans')?.checked ? '1' : '0';
    const r = await fetch('/v1/rate-limits?include_orphans=' + orphans, {
      headers: {'Authorization': 'Bearer ' + apiKey},
    });
    if (r.status === 401) return;
    if (!r.ok) return;
    const data = await r.json();
    rateLimitsData = data.groups || [];
    renderRateLimits();
  } catch (e) { /* page still usable */ }
}
```

Call `refreshRateLimits()` from `renderAll()` or from the end of `refresh()`. Prefer end of `refresh()` so orphan checkbox changes via `onchange="refreshRateLimits()"` work without re-fetching everything.

Implement:

```javascript
function renderRateLimits() {
  const tbody = document.getElementById('rl-tbody');
  if (!tbody) return;
  const rows = (rateLimitsData || []).slice().sort((a, b) => {
    const ha = a.headroom, hb = b.headroom;
    if (ha == null && hb == null) return 0;
    if (ha == null) return 1;
    if (hb == null) return -1;
    return ha - hb;
  });
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--muted);padding:24px">No rate data yet</td></tr>';
    document.getElementById('rl-detail-panel').style.display = 'none';
    return;
  }
  tbody.innerHTML = '';
  rows.forEach(g => {
    const tr = document.createElement('tr');
    tr.className = 'rl-row' + (g.id === selectedRateGroupId ? ' rl-selected' : '');
    tr.onclick = () => selectRateGroup(g.id);
    const scope = g.scope === 'model' ? (g.model || '—') : 'provider-wide';
    const hPct = g.headroom == null ? null : Math.round(g.headroom * 100);
    const hColor = hPct == null ? '' : hPct >= 50 ? 'green' : hPct >= 20 ? 'yellow' : 'red';
    const bar = hPct == null
      ? '<span class="muted">—</span>'
      : `<div style="display:inline-block;vertical-align:middle">
           <div class="prog-track" style="width:64px;display:inline-block">
             <div class="prog-fill ${hColor}" style="width:${hPct}%"></div>
           </div>
           <span class="muted" style="font-size:10px;margin-left:4px">${hPct}%</span>
         </div>`;
    const nBuckets = Object.keys(g.buckets || {}).length;
    tr.innerHTML = `
      <td><strong>${g.provider}</strong></td>
      <td class="mono muted">…${g.key_hint || ''}</td>
      <td class="mono muted">${scope}</td>
      <td>${g.binding || '<span class="muted">—</span>'}</td>
      <td>${bar}</td>
      <td class="right muted">${nBuckets}</td>`;
    tbody.appendChild(tr);
  });
  if (selectedRateGroupId) {
    const still = rows.find(r => r.id === selectedRateGroupId);
    if (still) renderRateDetail(still);
    else {
      selectedRateGroupId = null;
      document.getElementById('rl-detail-panel').style.display = 'none';
    }
  }
}

function selectRateGroup(id) {
  selectedRateGroupId = id;
  renderRateLimits();
}

function renderRateDetail(g) {
  const panel = document.getElementById('rl-detail-panel');
  panel.style.display = '';
  document.getElementById('rl-detail-title').textContent =
    g.provider + ' · …' + (g.key_hint || '');
  const cfg = g.configured
    ? '<span class="pill pill-ok">configured</span>'
    : '<span class="pill pill-warn">orphan</span>';
  document.getElementById('rl-detail-meta').innerHTML =
    `${cfg} · ${g.scope === 'model' ? 'model ' + (g.model || '') : 'provider-wide'} · <span class="mono">${g.id}</span>`;
  const tb = document.getElementById('rl-detail-tbody');
  const entries = Object.entries(g.buckets || {}).sort((a, b) => a[0].localeCompare(b[0]));
  if (!entries.length) {
    tb.innerHTML = '<tr><td colspan="6" class="muted">No buckets</td></tr>';
    return;
  }
  tb.innerHTML = entries.map(([name, b]) => {
    const muted = b.active ? '' : 'muted';
    const hPct = Math.round((b.headroom || 0) * 100);
    return `<tr class="${muted}">
      <td class="mono">${name}</td>
      <td>${b.active ? '<span class="pill pill-ok">yes</span>' : '<span class="pill pill-grey">no</span>'}</td>
      <td class="right">${fmt.num(b.cap)}</td>
      <td class="right">${fmt.num(b.used)}</td>
      <td class="right">${fmt.num(b.tokens)}</td>
      <td class="right">${hPct}%</td>
    </tr>`;
  }).join('');
}

async function clearRateGroup() {
  if (!selectedRateGroupId) return;
  if (!confirm('Clear learned rate-limit state for this group? Caps will relearn from traffic.')) return;
  try {
    const r = await fetch('/v1/rate-limits/clear', {
      method: 'POST',
      headers: {'Authorization': 'Bearer ' + apiKey, 'Content-Type': 'application/json'},
      body: JSON.stringify({id: selectedRateGroupId}),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      alert(err.error || ('Clear failed: HTTP ' + r.status));
    }
    selectedRateGroupId = null;
    await refreshRateLimits();
  } catch (e) {
    alert('Clear failed: ' + e);
  }
}
```

Ensure `fmt.num` already exists (it does in the dashboard). Soften `Object.keys` usage if needed for older browsers — the dashboard already uses modern JS.

- [ ] **Step 3: Manual UI check**

1. Open `/dashboard`, unlock with proxy key.
2. Open **Rate limits** — empty state or rows after traffic.
3. Toggle orphans; click a row; confirm detail buckets.
4. Clear a group; confirm it disappears / resets after refresh.
5. Confirm Providers advanced headroom column still works.

- [ ] **Step 4: Run unit tests once more**

Run: `pytest tests/test_rate_limiter.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add router.py
git commit -m "feat: add Rate limits TBF page to dashboard"
```

---

## Spec coverage checklist

| Spec requirement | Task |
|---|---|
| `clear_group` → bool + immediate flush | Task 1 |
| `list_groups` / parse / configured vs orphan / bucket fields / null headroom | Task 2 |
| `GET /v1/rate-limits`, `POST /v1/rate-limits/clear`, auth, 400/404 | Task 3 |
| `_configured_rate_group_ids` from live PROVIDERS | Task 3 |
| Nav page, flat table, sort, orphan toggle, detail, clear confirm | Task 4 |
| Providers headroom / `/v1/status` unchanged | Tasks 3–4 (explicit non-goals) |
| No full API keys in responses | Tasks 2–4 (`key_hint` only) |
| VS Code out of scope | — |

## Self-review notes

- No TBD/placeholder steps.
- Method names aligned across tasks: `clear_group`, `list_groups`, `parse_group_key`, `_configured_rate_group_ids`.
- `list_groups(..., configured_ids=)` matches route wiring in Task 3.
`)