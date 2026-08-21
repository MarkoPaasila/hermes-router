#!/usr/bin/env bash
#
# hr features — see and toggle hermes-router's optional add-ons
#
# hermes-router separates CORE features (always on — auth, failover, smart
# routing, the circuit breaker, …) from ADD-ONS you can turn on/off. This command
# is a friendly front-end over the env-var settings: it reads the live state from
# the running router (/v1/status) and, for simple flag add-ons, writes the backing
# variable into .env for you. Run `hr restart` after a change to apply it.
#
# Usage:
#   hr features list                 Show core features + add-ons (on/off)
#   hr features enable  <name>       Turn an add-on on  (writes .env)
#   hr features disable <name>       Turn an add-on off (writes .env)
#
# Add-ons backed by richer config (key_budgets, local_model) are managed by their
# own command (hr limit / hr model) — `hr features` shows their status and points
# you there.
#
# Reads PORT and the proxy API key from .env (or override with env vars).
#
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO" || { echo "cannot cd to repo dir"; exit 1; }
ENV_FILE="${HR_ENV_FILE:-$REPO/.env}"
PYTHON="${PYTHON:-python3}"

log()  { printf '\033[1;36m[features]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[features]\033[0m %s\n' "$*" >&2; }
ok()   { printf '\033[1;32m[features]\033[0m %s\n' "$*"; }

usage() { sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'; }

# Pull a value from .env (first match), stripping quotes/whitespace.
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

# Fetch the live features snapshot from the running router.
# Writes JSON to a temp file — /v1/status can exceed Linux MAX_ARG_STRLEN (~128KiB),
# so we must not pass it via an environment variable into python.
fetch_features_file() {
  command -v curl >/dev/null 2>&1 || { err "curl is not installed."; return 1; }
  local out
  out="$(mktemp)" || return 1
  if ! curl -fsS -H "Authorization: Bearer ${KEY}" \
      "http://localhost:${PORT}/v1/status" >"$out" 2>/dev/null; then
    rm -f "$out"
    return 1
  fi
  printf '%s' "$out"
}

# Upsert KEY=VALUE in .env (replace an existing line or append).
set_env() {
  local k="$1" v="$2"
  touch "$ENV_FILE"
  if grep -qE "^${k}=" "$ENV_FILE"; then
    K="$k" V="$v" "$PYTHON" - "$ENV_FILE" <<'PY'
import os, re, sys
f = sys.argv[1]; k = os.environ["K"]; v = os.environ["V"]
lines = open(f).read().splitlines()
out = [(f"{k}={v}" if re.match(rf"^{re.escape(k)}=", ln) else ln) for ln in lines]
open(f, "w").write("\n".join(out) + "\n")
PY
  else
    printf '%s=%s\n' "$k" "$v" >> "$ENV_FILE"
  fi
}

cmd="${1:-list}"
shift 2>/dev/null || true

case "$cmd" in
  list)
    status_file="$(fetch_features_file)" || { err "couldn't reach the router on http://localhost:${PORT}"; \
      err "start it with:  hr start   (or set PORT / PROXY_API_KEYS)"; exit 1; }
    "$PYTHON" - "$status_file" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    d = json.load(f)
f = d.get("features", {})
RST="\033[0m"; BOLD="\033[1m"; GRN="\033[1;32m"; DIM="\033[2m"
core = f.get("core", [])
print()
print(f"  {BOLD}Core features{RST} {DIM}(always on){RST}")
print("  " + ", ".join(core))
print()
print(f"  {BOLD}Add-ons{RST}")
for a in f.get("addons", []):
    dot = f"{GRN}●{RST}" if a.get("enabled") else f"{DIM}○{RST}"
    state = f"{GRN}on{RST}" if a.get("enabled") else f"{DIM}off{RST}"
    print(f"  {dot} {a['name']:<17} {state}")
    print(f"    {DIM}{a.get('desc','')}{RST}")
    if a.get("kind") == "config":
        print(f"    {DIM}manage: {a.get('manage','')}{RST}")
print()
print(f"  {DIM}Toggle a flag add-on:  hr features enable|disable <name>   then  hr restart{RST}")
print()
PY
    rm -f "$status_file"
    ;;

  enable|disable)
    name="${1:-}"
    [ -n "$name" ] || { err "usage: hr features ${cmd} <name>"; exit 1; }
    status_file="$(fetch_features_file)" || { err "couldn't reach the router on http://localhost:${PORT} — start it first."; exit 1; }
    # Resolve the add-on from the live registry; emit a shell snippet to act on.
    # Status JSON is read from a file (argv path only) — never via env (MAX_ARG_STRLEN).
    action="$("$PYTHON" - "$status_file" "$name" "$cmd" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    d = json.load(f)
name, want = sys.argv[2], sys.argv[3]
addons = {a["name"]: a for a in d.get("features", {}).get("addons", [])}
a = addons.get(name)
if not a:
    print("ERR unknown add-on: " + name)
    print("NAMES " + " ".join(addons))
elif a.get("kind") == "config":
    print("CONFIG " + (a.get("manage") or ""))
else:
    print(f"SET {a['env']} {a['on'] if want=='enable' else a['off']}")
PY
)"
    rm -f "$status_file"
    case "$action" in
      "SET "*)
        set -- $action; set_env "$2" "$3"
        ok "${cmd}d '${name}' → set ${2}=${3} in .env"
        log "run 'hr restart' to apply."
        ;;
      "CONFIG "*)
        err "'${name}' isn't a simple on/off flag — manage it with:"
        printf '    %s\n' "${action#CONFIG }" >&2
        exit 1
        ;;
      "ERR "*)
        err "$(printf '%s' "$action" | sed -n '1p' | sed 's/^ERR //')"
        names="$(printf '%s\n' "$action" | sed -n '2p')"; names="${names#NAMES }"
        err "valid add-ons: ${names}"
        exit 1
        ;;
      *) err "unexpected error resolving '${name}'"; exit 1 ;;
    esac
    ;;

  help|--help|-h) usage ;;
  *) err "unknown subcommand: ${cmd}"; usage >&2; exit 1 ;;
esac
