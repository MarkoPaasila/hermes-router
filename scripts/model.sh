#!/usr/bin/env bash
#
# hr model — manage per-provider model overrides
#
# Usage:
#   hr model list                    Show all providers and their active model(s)
#   hr model set <provider> <model>  Override a provider's model(s)
#   hr model help                    Show this help
#
# A provider can use MULTIPLE models — pass a comma-separated list. Rate limits
# are per-model, so the router fails over across them (each its own quota) before
# moving to the next provider. The first model is the primary (used for routing).
#
# Examples:
#   hr model set anthropic claude-sonnet-4-6
#   hr model set openai gpt-4o
#   hr model set gemini gemini-2.5-flash-lite,gemini-2.5-flash,gemini-2.0-flash
#
# To clear an override, remove the corresponding <PROVIDER>_MODEL line from .env
# (or set it empty). There is no built-in default table in the CLI — catalog
# defaults come from the router when no override is set.
#
# Overrides are written to .env and take effect after: hr restart
#
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO" || { echo "cannot cd to repo dir"; exit 1; }
ENV_FILE="$REPO/.env"
PYTHON="${REPO}/venv/bin/python"
[ -f "$PYTHON" ] || PYTHON=python3

log()  { printf '\033[1;36m[model]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[model]\033[0m %s\n' "$*" >&2; }
ok()   { printf '\033[1;32m[model]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[model]\033[0m %s\n' "$*"; }

PROVIDERS_LIST="gemini openrouter sambanova github_models cerebras groq mistral cohere zai naga nvidia huggingface kimi opencode opencode_go openai anthropic codex local"

canonical_provider() {
  case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
    gemini|google)         echo "gemini" ;;
    openrouter|or)         echo "openrouter" ;;
    sambanova|samba)       echo "sambanova" ;;
    github_models|github)  echo "github_models" ;;
    cerebras)              echo "cerebras" ;;
    groq)                  echo "groq" ;;
    mistral)               echo "mistral" ;;
    cohere)                echo "cohere" ;;
    zai|glm|z.ai)          echo "zai" ;;
    naga)                  echo "naga" ;;
    nvidia|nim)            echo "nvidia" ;;
    kimi|moonshot)         echo "kimi" ;;
    opencode|opencode_zen|zen) echo "opencode" ;;
    opencode_go|opencodego|opencode-go|go) echo "opencode_go" ;;
    openai|gpt)            echo "openai" ;;
    anthropic|claude)      echo "anthropic" ;;
    codex|chatgpt)         echo "codex" ;;
    local|ollama)          echo "local" ;;
    *)                     echo "" ;;
  esac
}

# Returns the .env key name for a provider's model override.
env_var_for() {
  case "$1" in
    gemini)        echo "GEMINI_MODEL" ;;
    openrouter)    echo "OPENROUTER_MODEL" ;;
    sambanova)     echo "SAMBANOVA_MODEL" ;;
    github_models) echo "GITHUB_MODELS_MODEL" ;;
    cerebras)      echo "CEREBRAS_MODEL" ;;
    groq)          echo "GROQ_MODEL" ;;
    mistral)       echo "MISTRAL_MODEL" ;;
    cohere)        echo "COHERE_MODEL" ;;
    zai)           echo "ZAI_MODEL" ;;
    naga)          echo "NAGA_MODEL" ;;
    nvidia)        echo "NVIDIA_MODEL" ;;
    huggingface)   echo "HUGGINGFACE_MODEL" ;;
    kimi)          echo "KIMI_MODEL" ;;
    opencode)      echo "OPENCODE_MODEL" ;;
    opencode_go)   echo "OPENCODE_GO_MODEL" ;;
    openai)        echo "OPENAI_MODEL" ;;
    anthropic)     echo "ANTHROPIC_MODEL" ;;
    codex)         echo "CODEX_MODEL" ;;
    local)         echo "LOCAL_MODEL" ;;
  esac
}

# Read a single key from .env (last occurrence wins).
read_env() {
  local key="$1"
  [ -f "$ENV_FILE" ] || return
  grep "^${key}=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d'=' -f2-
}

# Write or delete a key in .env. Pass empty value to delete.
write_env() {
  local key="$1"
  local val="$2"
  "$PYTHON" - "$ENV_FILE" "$key" "$val" <<'PY'
import os, sys
path, key, val = sys.argv[1], sys.argv[2], sys.argv[3]
lines = open(path).readlines() if os.path.exists(path) else []
found, out = False, []
for line in lines:
    if line.strip().startswith(f"{key}="):
        if val:                          # set: replace line
            out.append(f"{key}={val}\n")
        found = True                     # reset: skip line (delete)
    else:
        out.append(line)
if not found and val:                    # new key
    out.append(f"{key}={val}\n")
with open(path, "w") as f:
    f.writelines(out)
PY
}

# ── hr model list ─────────────────────────────────────────────────────────────

cmd_list() {
  echo ""
  printf '  %-16s  %-42s  %s\n' "Provider" "Model" "Source"
  printf '  %-16s  %-42s  %s\n' "────────────────" "──────────────────────────────────────────" "────────"

  for provider in $PROVIDERS_LIST; do
    env_var=$(env_var_for "$provider")
    override=$(read_env "$env_var")

    if [ -n "$override" ]; then
      printf '  %-16s  \033[1;33m%-42s\033[0m  \033[1;33moverride\033[0m\n' "$provider" "$override"
    else
      printf '  %-16s  %-42s  catalog default\n' "$provider" "(see router / dashboard)"
    fi
  done

  echo ""
  log "Overrides are stored in: $ENV_FILE"
  log "Clear an override by removing its <PROVIDER>_MODEL line from .env"
  log "Run 'hr restart' to apply any changes."
}

# ── hr model set <provider> <model> ──────────────────────────────────────────

cmd_set() {
  local raw="${1:-}"
  local model="${2:-}"

  if [ -z "$raw" ] || [ -z "$model" ]; then
    err "Usage: hr model set <provider> <model>"
    err "Example: hr model set anthropic claude-sonnet-4-6"
    exit 1
  fi

  local provider
  provider=$(canonical_provider "$raw")
  if [ -z "$provider" ]; then
    err "Unknown provider: '$raw'"
    err "Supported: $PROVIDERS_LIST"
    exit 1
  fi

  local env_var current
  env_var=$(env_var_for "$provider")
  current=$(read_env "$env_var")

  if [ "$current" = "$model" ]; then
    warn "$provider is already set to: $model"
    exit 0
  fi

  write_env "$env_var" "$model"
  if [ -n "$current" ]; then
    ok "$provider: $current  →  $model"
  else
    ok "$provider: (catalog default)  →  $model"
  fi
  log "Run 'hr restart' to apply."
}

# ── Dispatch ─────────────────────────────────────────────────────────────────

subcmd="${1:-help}"
shift 2>/dev/null || true

case "$subcmd" in
  list)           cmd_list ;;
  set)            cmd_set "$@" ;;
  reset)
    err "'hr model reset' was removed — no built-in default table in the CLI."
    err "To clear an override, remove the <PROVIDER>_MODEL line from $ENV_FILE"
    exit 1
    ;;
  help|-h|--help) awk 'NR>1 && /^#/ {sub(/^#[[:space:]]?/,""); print; next} NR>1 {exit}' "$0" ;;
  *)
    err "unknown subcommand: '$subcmd'"
    err "Usage: hr model list | set <provider> <model>"
    exit 1
    ;;
esac
