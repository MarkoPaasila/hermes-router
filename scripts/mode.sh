#!/usr/bin/env bash
#
# hr mode — (deprecated) key rotation mode was removed
#
# Usage:
#   hr mode          Explain current key-selection behavior
#   hr mode help     Show this help
#
# Key selection within each provider is now sticky-until-fail: the router keeps
# using the same key for a (provider, model) until that key errors or is
# rate-limited, then tries the next ready key in stable deque order. There is
# no round-robin or sequential rotation mode.
#
# The legacy ROTATION_MODE env var is ignored by the router (startup warning only).
# Remove any ROTATION_MODE= line from .env manually if present.
#
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO" || { echo "cannot cd to repo dir"; exit 1; }
ENV_FILE="$REPO/.env"

log()  { printf '\033[1;36m[mode]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[mode]\033[0m %s\n' "$*"; }

read_env_rotation() {
  [ -f "$ENV_FILE" ] || return
  grep "^ROTATION_MODE=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d'=' -f2-
}

cmd_show() {
  echo ""
  log "Key selection: sticky-until-fail (same key per provider/model until it fails)"
  log "Round-robin and sequential rotation modes were removed."
  if [ -n "$(read_env_rotation)" ]; then
    warn "ROTATION_MODE is set in $ENV_FILE but ignored — you can delete that line."
  fi
  echo ""
}

cmd_deprecated_set() {
  warn "Rotation mode '$1' is no longer supported (sticky-until-fail is always used)."
  warn "No change was written to .env."
  exit 1
}

subcmd="${1:-show}"

case "$subcmd" in
  ""|show|list|status)  cmd_show ;;
  help|-h|--help)       awk 'NR>1 && /^#/ {sub(/^#[[:space:]]?/,""); print; next} NR>1 {exit}' "$0" ;;
  round-robin|roundrobin|round_robin|rr|sequential|seq|drain)
                        cmd_deprecated_set "$subcmd" ;;
  *)                    cmd_deprecated_set "$subcmd" ;;
esac
