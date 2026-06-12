#!/usr/bin/env bash
# SimWorld Studio - Linux/macOS startup script
# Usage: ./start.sh
#   or with overrides: PORT=3002 UNREAL_PORT=55558 ./start.sh

set -e

export PORT="${PORT:-3002}"
export UCV_PORT="${UCV_PORT:-9001}"
export UNREAL_PORT="${UNREAL_PORT:-55557}"
export UNREAL_HOST="${UNREAL_HOST:-127.0.0.1}"
export CIRRUS_HTTP_PORT="${CIRRUS_HTTP_PORT:-8685}"
export CIRRUS_WS_PORT="${CIRRUS_WS_PORT:-8686}"
export ASSET_DB_DIR="${ASSET_DB_DIR:-/data/siddhant/asset_db}"
export POSTGRES_URL="${POSTGRES_URL:-postgresql://simworld:simworld@127.0.0.1:55432/asset_db}"
export QDRANT_URL="${QDRANT_URL:-http://127.0.0.1:6333}"
export QDRANT_COLLECTION="${QDRANT_COLLECTION:-assets}"
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

echo "Starting SimWorld Studio server..."
echo "  Studio UI  : http://localhost:${PORT}"
echo "  UE TCP     : ${UNREAL_HOST}:${UNREAL_PORT}"
echo "  UCV broker : ${UNREAL_HOST}:${UCV_PORT}"
echo "  Cirrus HTTP: ${CIRRUS_HTTP_PORT}   WS: ${CIRRUS_WS_PORT}"
echo "  Asset DB   : ${ASSET_DB_DIR}"
echo "  Asset mode : ${ASSET_RETRIEVAL_MODE}   topK=${PREFILTER_TOP_K}"
echo "  Prefilter  : ${ASSET_PREFILTER} (legacy assetMode=retrieval alias)"
echo ""

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec node "${SCRIPT_DIR}/index.js"
