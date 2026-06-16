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
export ASSET_DB_DIR="${ASSET_DB_DIR:-/data/siddhant/asset_db_ue58_qwen}"
export POSTGRES_URL="${POSTGRES_URL:-postgresql://USER:PASSWORD@127.0.0.1:55432/asset_db_ue58_qwen}"
export QDRANT_URL="${QDRANT_URL:-http://127.0.0.1:6333}"
export QDRANT_COLLECTION="${QDRANT_COLLECTION:-assets_ue58_qwen}"
export EMBED_SERVICE_URL="${EMBED_SERVICE_URL:-http://127.0.0.1:7777}"
export ASSET_RETRIEVAL_MODE="${ASSET_RETRIEVAL_MODE:-db}"
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
SIMWORLD_WEB_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
SIMWORLD_WEB_DIR="${SIMWORLD_WEB_DIR}" node <<'NODE'
const fs = require("fs");
const path = require("path");

const src = path.join(process.env.SIMWORLD_WEB_DIR, "mcp.json");
const dst = process.env.SIMWORLD_MCP_CONFIG;
const cfg = JSON.parse(fs.readFileSync(src, "utf8"));

cfg.mcpServers = cfg.mcpServers || {};
cfg.mcpServers.simworld = cfg.mcpServers.simworld || {};
cfg.mcpServers.simworld.env = cfg.mcpServers.simworld.env || {};
cfg.mcpServers.simworld.env.UNREAL_HOST = process.env.UNREAL_HOST || "127.0.0.1";
cfg.mcpServers.simworld.env.UNREAL_PORT = String(process.env.UNREAL_PORT || "55559");

fs.writeFileSync(dst, `${JSON.stringify(cfg, null, 2)}\n`, "utf8");
NODE

ORIGINAL_HOME="${HOME:-/home/siddhant}"
REAL_CLAUDE_BIN="${CLAUDE_BIN:-$(command -v claude || true)}"
if [[ -n "$REAL_CLAUDE_BIN" && -f "${ORIGINAL_HOME}/.claude.json" ]]; then
  if ! python3 -m json.tool "${ORIGINAL_HOME}/.claude.json" >/dev/null 2>&1; then
    export SIMWORLD_CLAUDE_ORIGINAL_HOME="${SIMWORLD_CLAUDE_ORIGINAL_HOME:-$ORIGINAL_HOME}"
    export SIMWORLD_REAL_CLAUDE_BIN="${SIMWORLD_REAL_CLAUDE_BIN:-$REAL_CLAUDE_BIN}"
    export SIMWORLD_CLAUDE_RUNTIME_HOME="${SIMWORLD_CLAUDE_RUNTIME_HOME:-/data/siddhant/asset_db_ue58_qwen/runtime/claude_home}"
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
echo "  Postgres   : ${POSTGRES_URL}"
echo "  Qdrant     : ${QDRANT_URL} collection=${QDRANT_COLLECTION}"
echo "  Embeddings : ${EMBED_SERVICE_URL} version=${EMBED_VERSION}"
echo "  Asset mode : ${ASSET_RETRIEVAL_MODE}   topK=${PREFILTER_TOP_K}"
echo "  Prefilter  : ${ASSET_PREFILTER} (legacy assetMode=retrieval alias)"
echo "  MCP config : ${SIMWORLD_MCP_CONFIG}"
echo ""

exec node "${SCRIPT_DIR}/index.js"
