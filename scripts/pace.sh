#!/usr/bin/env bash
#
# hr pace — pool capacity signal for client pacing (stretch interval / skip tick)
#
# Queries the router's /v1/capacity endpoint.
#
# Usage:
#   hr pace            One-liner: advice / skip / multiplier / capacity
#   hr pace --json     Print the raw JSON (for scripts / Hermes cron)
#
# Reads PORT and the proxy API key from .env (or override with env vars).
#
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO" || { echo "cannot cd to repo dir"; exit 1; }
ENV_FILE="${HR_ENV_FILE:-$REPO/.env}"

err() { printf '\033[1;31m[pace]\033[0m %s\n' "$*" >&2; }

from_env() {
  [ -f "$ENV_FILE" ] || return 1
  local line
  line="$(grep -E "^$1=" "$ENV_FILE" 2>/dev/null | head -1)" || return 1
  [ -n "$line" ] || return 1
  printf '%s' "${line#*=}" | sed 's/^[[:space:]"'"'"']*//; s/[[:space:]"'"'"']*$//'
}

PORT="${PORT:-$(from_env PORT || echo 8319)}"
KEY="${PROXY_API_KEYS:-$(from_env PROXY_API_KEYS || echo 'sk-router-1')}"
KEY="${KEY%%,*}"

JSON_ONLY=0
[ "${1:-}" = "--json" ] && JSON_ONLY=1

command -v curl >/dev/null 2>&1 || { err "curl is not installed."; exit 1; }

raw="$(curl -fsS -H "Authorization: Bearer ${KEY}" "http://localhost:${PORT}/v1/capacity" 2>/dev/null)"
if [ -z "$raw" ]; then
  err "couldn't reach the router on http://localhost:${PORT}"
  err "is it running? start it with:  hr start"
  err "if it's on another port, set PORT; key via PROXY_API_KEYS."
  # Fail-open defaults for Hermes cron: slow pace, do not skip.
  if [ "$JSON_ONLY" = "1" ]; then
    printf '%s\n' '{"advice":"slow","interval_multiplier":2.0,"skip":false,"capacity":null,"reasons":["endpoint_unreachable"]}'
  else
    printf 'advice=slow skip=false mult=2.0 capacity=? (unreachable)\n'
  fi
  exit 1
fi

if [ "$JSON_ONLY" = "1" ]; then
  printf '%s\n' "$raw" | python3 -m json.tool 2>/dev/null || printf '%s\n' "$raw"
  exit 0
fi

HR_PACE_JSON="$raw" python3 - <<'PY'
import json, os
d = json.loads(os.environ.get("HR_PACE_JSON", "{}"))
advice = d.get("advice", "?")
skip = d.get("skip", False)
mult = d.get("interval_multiplier", "?")
cap = d.get("capacity", "?")
print(f"advice={advice} skip={str(skip).lower()} mult={mult} capacity={cap}")
PY
