#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ASSET_DB_DIR="${ASSET_DB_DIR:-/data/siddhant/asset_db}"
MANIFEST="${MANIFEST:-${ASSET_DB_DIR}/manifest_full.json}"
RUN_ROOT="${RUN_ROOT:-${ASSET_DB_DIR}/runs}"
RUN_ID="${RUN_ID:-full_index_gpt55_$(date -u +%Y%m%d_%H%M%S)}"
RUN_DIR="${RUN_DIR:-${RUN_ROOT}/${RUN_ID}}"
POSTGRES_URL="${POSTGRES_URL:-postgresql://USER:PASSWORD@127.0.0.1:55432/asset_db}"
QDRANT_URL="${QDRANT_URL:-http://127.0.0.1:6333}"
QDRANT_COLLECTION="${QDRANT_COLLECTION:-assets}"
CAPTION_PROVIDER="${CAPTION_PROVIDER:-codex}"
CODEX_MODEL="${CODEX_MODEL:-gpt-5.5}"
QWEN_BASE_URL="${QWEN_BASE_URL:-http://137.110.161.132:8005/v1}"
QWEN_MODEL="${QWEN_MODEL:-Qwen3.6-35B-A3B}"
QWEN_ENABLE_THINKING="${QWEN_ENABLE_THINKING:-0}"
QWEN_MAX_TOKENS="${QWEN_MAX_TOKENS:-1200}"
QWEN_TEMPERATURE="${QWEN_TEMPERATURE:-0.0}"
QWEN_TIMEOUT="${QWEN_TIMEOUT:-240}"
UE_PROJECT="${UE_PROJECT:-/data/siddhant/simworld_studio_projects}"
MCP_PORT="${MCP_PORT:-55571}"

mkdir -p "$RUN_DIR"
LOG="${RUN_DIR}/runner.log"

CMD=(
  python3 "${REPO_ROOT}/tools/full_asset_index_runner.py" run
  --asset-db-dir "$ASSET_DB_DIR"
  --manifest "$MANIFEST"
  --run-dir "$RUN_DIR"
  --caption-provider "$CAPTION_PROVIDER"
  --model "$CODEX_MODEL"
  --qwen-base-url "$QWEN_BASE_URL"
  --qwen-model "$QWEN_MODEL"
  --qwen-max-tokens "$QWEN_MAX_TOKENS"
  --qwen-temperature "$QWEN_TEMPERATURE"
  --qwen-timeout "$QWEN_TIMEOUT"
  --postgres-url "$POSTGRES_URL"
  --qdrant-url "$QDRANT_URL"
  --qdrant-collection "$QDRANT_COLLECTION"
  --ue-project "$UE_PROJECT"
  --mcp-port "$MCP_PORT"
)

case "$(printf '%s' "$QWEN_ENABLE_THINKING" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes|on) CMD+=(--qwen-enable-thinking) ;;
esac

{
  echo "repo=${REPO_ROOT}"
  echo "asset_db_dir=${ASSET_DB_DIR}"
  echo "manifest=${MANIFEST}"
  echo "run_dir=${RUN_DIR}"
  echo "postgres_url=${POSTGRES_URL}"
  echo "qdrant_url=${QDRANT_URL}"
  echo "qdrant_collection=${QDRANT_COLLECTION}"
  echo "caption_provider=${CAPTION_PROVIDER}"
  echo "codex_model=${CODEX_MODEL}"
  echo "qwen_base_url=${QWEN_BASE_URL}"
  echo "qwen_model=${QWEN_MODEL}"
  echo "qwen_enable_thinking=${QWEN_ENABLE_THINKING}"
  echo "qwen_max_tokens=${QWEN_MAX_TOKENS}"
  echo "qwen_temperature=${QWEN_TEMPERATURE}"
  echo "qwen_timeout=${QWEN_TIMEOUT}"
  echo "ue_project=${UE_PROJECT}"
  echo "mcp_port=${MCP_PORT}"
  printf "command="
  printf "%q " "${CMD[@]}" "$@"
  echo
} > "${RUN_DIR}/launch.env"

printf "%q " "${CMD[@]}" "$@" > "${RUN_DIR}/command.txt"
echo >> "${RUN_DIR}/command.txt"

printf '{"ts":"%s","event":"launch","run_dir":"%s","log":"%s"}\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$RUN_DIR" "$LOG" >> "${RUN_DIR}/launches.ndjson"

nohup setsid "${CMD[@]}" "$@" >> "$LOG" 2>&1 < /dev/null &
PID=$!
echo "$PID" > "${RUN_DIR}/launcher_pid"
sleep 2
if ! kill -0 "$PID" 2>/dev/null; then
  echo "Launch failed quickly; see ${LOG}" >&2
  tail -40 "$LOG" >&2 || true
  exit 1
fi

echo "Launched full asset index run."
echo "  pid: ${PID}"
echo "  run_dir: ${RUN_DIR}"
echo "  log: ${LOG}"
echo "  status: python3 ${REPO_ROOT}/tools/full_asset_index_runner.py status --run-dir ${RUN_DIR}"
