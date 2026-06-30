#!/usr/bin/env bash
set -euo pipefail

ORIGINAL_HOME="${SIMWORLD_CLAUDE_ORIGINAL_HOME:-${HOME:-/home/siddhant}}"
RUNTIME_HOME="${SIMWORLD_CLAUDE_RUNTIME_HOME:-/data/siddhant/asset_db_ue58_qwen/runtime/claude_home}"
REAL_CLAUDE_BIN="${SIMWORLD_REAL_CLAUDE_BIN:-/home/siddhant/.local/bin/claude}"

mkdir -p "$RUNTIME_HOME"

if [[ ! -e "${RUNTIME_HOME}/.claude" && -d "${ORIGINAL_HOME}/.claude" ]]; then
  ln -s "${ORIGINAL_HOME}/.claude" "${RUNTIME_HOME}/.claude"
fi

if [[ ! -s "${RUNTIME_HOME}/.claude.json" ]]; then
  printf '{}\n' > "${RUNTIME_HOME}/.claude.json"
fi

export HOME="$RUNTIME_HOME"
exec "$REAL_CLAUDE_BIN" "$@"
