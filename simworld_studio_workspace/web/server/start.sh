#!/usr/bin/env bash
# SimWorld Studio - Linux/macOS startup script
# Usage: ./start.sh
#   or with overrides: PORT=3002 UNREAL_PORT=55558 ./start.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

export PORT="${PORT:-3002}"
export UCV_PORT="${UCV_PORT:-9001}"
export UNREAL_PORT="${UNREAL_PORT:-55557}"
export UNREAL_HOST="${UNREAL_HOST:-127.0.0.1}"
export CIRRUS_HTTP_PORT="${CIRRUS_HTTP_PORT:-8685}"
export CIRRUS_WS_PORT="${CIRRUS_WS_PORT:-8686}"
DEFAULT_DATA_ROOT="${XDG_DATA_HOME:-${HOME:+${HOME}/.local/share}}"
DEFAULT_DATA_ROOT="${DEFAULT_DATA_ROOT:-${REPO_ROOT}/.runtime}"
DEFAULT_STATE_ROOT="${XDG_STATE_HOME:-${HOME:+${HOME}/.local/state}}"
DEFAULT_STATE_ROOT="${DEFAULT_STATE_ROOT:-${REPO_ROOT}/.runtime}"
export ASSET_DB_DIR="${ASSET_DB_DIR:-${DEFAULT_DATA_ROOT}/simworld-studio/asset-db}"
export VISTA_IMPORT_ARTIFACT_ROOT="${VISTA_IMPORT_ARTIFACT_ROOT:-${DEFAULT_STATE_ROOT}/simworld-studio/vista-imports}"
# Database credentials must be supplied by the service environment/secret store.
# Keep the variable exported for the Node process, but never provide or print a
# credential-bearing fallback from this script.
export POSTGRES_URL="${POSTGRES_URL:-}"
export QDRANT_URL="${QDRANT_URL:-http://127.0.0.1:6333}"
export QDRANT_COLLECTION="${QDRANT_COLLECTION:-assets_ue58_qwen}"
export EMBED_SERVICE_URL="${EMBED_SERVICE_URL:-http://127.0.0.1:7777}"
export ASSET_RETRIEVAL_MODE="${ASSET_RETRIEVAL_MODE:-hybrid}"
export CLAUDE_MODEL="${CLAUDE_MODEL:-claude-opus-4-8}"
export CRITIC_PROVIDER="${CRITIC_PROVIDER:-claude}"
export CRITIC_MODEL="${CRITIC_MODEL:-claude-opus-4-8}"
if [[ -z "${ASSET_PREFILTER+x}" ]]; then
  case "${ASSET_RETRIEVAL_MODE,,}" in
    db|qdrant|prefilter|vector|hybrid) export ASSET_PREFILTER="true" ;;
    *) export ASSET_PREFILTER="false" ;;
  esac
else
  export ASSET_PREFILTER
fi
export PREFILTER_TOP_K="${PREFILTER_TOP_K:-150}"
export EMBED_VERSION="${EMBED_VERSION:-bge-large-en-v1.5-bm25-v1}"

export SIMWORLD_MCP_CONFIG="${SIMWORLD_MCP_CONFIG:-${SCRIPT_DIR}/../.runtime/mcp-${PORT}.json}"
mkdir -p "$(dirname "${SIMWORLD_MCP_CONFIG}")"
# index.js atomically creates this 0600 config from the current checkout.  It
# intentionally contains no DB DSN, access token, or receipt secret; MCP
# subprocesses inherit those values from the already-sandboxed builder process.

ORIGINAL_HOME="${HOME:-}"
REAL_CLAUDE_BIN="${CLAUDE_BIN:-$(command -v claude || true)}"
if [[ -n "$ORIGINAL_HOME" && -n "$REAL_CLAUDE_BIN" && -f "${ORIGINAL_HOME}/.claude.json" ]]; then
  if ! python3 -m json.tool "${ORIGINAL_HOME}/.claude.json" >/dev/null 2>&1; then
    export SIMWORLD_CLAUDE_ORIGINAL_HOME="${SIMWORLD_CLAUDE_ORIGINAL_HOME:-$ORIGINAL_HOME}"
    export SIMWORLD_REAL_CLAUDE_BIN="${SIMWORLD_REAL_CLAUDE_BIN:-$REAL_CLAUDE_BIN}"
    export SIMWORLD_CLAUDE_RUNTIME_HOME="${SIMWORLD_CLAUDE_RUNTIME_HOME:-${DEFAULT_STATE_ROOT}/simworld-studio/claude-home}"
    export CLAUDE_BIN="${CLAUDE_BIN:-${REPO_ROOT}/tools/claude_with_runtime_home.sh}"
    echo "  Claude     : using runtime HOME wrapper because ${ORIGINAL_HOME}/.claude.json is invalid"
  fi
fi

echo "Starting SimWorld Studio server..."
echo "  Studio UI  : http://localhost:${PORT}"
echo "  UE TCP     : ${UNREAL_HOST}:${UNREAL_PORT}"
echo "  UCV broker : ${UNREAL_HOST}:${UCV_PORT}"
echo "  Cirrus HTTP: ${CIRRUS_HTTP_PORT}   WS: ${CIRRUS_WS_PORT}"
echo "  Asset DB   : ${ASSET_DB_DIR}"
echo "  VISTA store: ${VISTA_IMPORT_ARTIFACT_ROOT}"
if [[ -n "${POSTGRES_URL}" ]]; then
  echo "  Postgres   : configured via POSTGRES_URL (value hidden)"
else
  echo "  Postgres   : not configured (set POSTGRES_URL via service secret)"
fi
echo "  Qdrant     : configured via QDRANT_URL (value hidden) collection=${QDRANT_COLLECTION}"
echo "  Embeddings : configured via EMBED_SERVICE_URL (value hidden) version=${EMBED_VERSION}"
echo "  Asset mode : ${ASSET_RETRIEVAL_MODE}   topK=${PREFILTER_TOP_K}"
echo "  Builder     : claude model=${CLAUDE_MODEL}"
echo "  Review      : provider=${CRITIC_PROVIDER} model=${CRITIC_MODEL}"
echo "  Prefilter  : ${ASSET_PREFILTER} (legacy assetMode=retrieval alias)"
echo "  MCP config : ${SIMWORLD_MCP_CONFIG}"
echo ""

exec node "${SCRIPT_DIR}/index.js"
