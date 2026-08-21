# Open dashboard without access key

**Date:** 2026-08-21  
**Status:** approved

## Problem

The web dashboard at `/dashboard` ships a client-side key gate and sends `PROXY_API_KEYS` as Bearer on every `/v1/*` call. Operators who want a local control panel without pasting a key cannot use the UI without that credential, even though the HTML page itself is already public.

## Goals

- Opt-in mode where the full dashboard (read and write) works with no access key.
- Chat and related API surfaces stay key-protected.
- Default install behavior unchanged (keyed).
- Keep the existing “cannot revoke the last access key” guard.

## Non-goals

- Changing default bind address (`HOST`).
- Splitting admin vs chat key tiers.
- Opening `/v1/chat/completions`, `/v1/embeddings`, or `/v1/models` without a key.
- Relaxing last-key revoke.

## Behavior

| Mode | Dashboard UI | Allowlisted monitoring/config APIs | Chat / embeddings / models |
|------|--------------|------------------------------------|----------------------------|
| Default (`DASHBOARD_OPEN` off) | Key gate; Bearer required | Key required | Key required |
| Open (`DASHBOARD_OPEN=1`) | No key gate | No key required | Key required |

**Enable:** `DASHBOARD_OPEN=1` in `.env`, or `hr features enable dashboard_open` / Add-ons toggle (restart required like other flag add-ons). First enable from the UI still needs a key (or a manual env edit) while closed.

**Disable:** toggle off or set env to `0`, then restart. Key gate returns.

**Security:** open mode exposes admin config (mint/revoke keys, add provider keys, restart) to anyone who can reach the port. Prefer `HOST=127.0.0.1` or an SSH tunnel on shared hosts.

## Architecture

Auth bypass inside `_auth_check()` when `DASHBOARD_OPEN` is on and the request path is allowlisted. No separate `/dashboard/api/*` surface.

**Allowlist:** `/v1/status`, `/v1/usage`, `/v1/capacity`, `/v1/rate-limits`, `/v1/rate-limits/clear`, `/v1/logs`, and all `/v1/config/*`.

**Not allowlisted:** `/v1/chat/completions`, `/v1/embeddings`, `/v1/models`.

**Feature registry:** flag add-on `dashboard_open` → env `DASHBOARD_OPEN` (`on=1` / `off=0`).

**UI:** on load with empty stored key, probe `GET /v1/status` without Authorization; on 200 hide gate and poll; on 401 keep gate. Omit `Authorization` when `apiKey` is empty. On later 401, show gate again.

## Testing

- Closed: allowlisted routes without key → 401; with key → success.
- Open: allowlisted without key → success; chat/models without key → 401.
- Features snapshot lists `dashboard_open` with correct env/enabled.
