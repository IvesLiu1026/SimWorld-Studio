#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

ASSET_DB_DIR="${ASSET_DB_DIR:-/data/siddhant/asset_db_ue58_qwen}"
MANIFEST="${MANIFEST:-/data/siddhant/asset_db/ue58_object_manifest.json}"
RUN_ROOT="${RUN_ROOT:-${ASSET_DB_DIR}/runs}"
RUN_ID="${RUN_ID:-ue58_parallel_qwen36_$(date -u +%Y%m%d_%H%M%S)}"
RUN_DIR="${RUN_DIR:-${RUN_ROOT}/${RUN_ID}}"
POSTGRES_URL="${POSTGRES_URL:-postgresql://USER:PASSWORD@127.0.0.1:55432/asset_db_ue58_qwen}"
QDRANT_URL="${QDRANT_URL:-http://127.0.0.1:6333}"
QDRANT_COLLECTION="${QDRANT_COLLECTION:-assets_ue58_qwen}"
CAPTION_PROVIDER="${CAPTION_PROVIDER:-qwen}"
QWEN_BASE_URL="${QWEN_BASE_URL:-http://137.110.161.132:8005/v1}"
QWEN_MODEL="${QWEN_MODEL:-Qwen3.6-35B-A3B}"
QWEN_ENABLE_THINKING="${QWEN_ENABLE_THINKING:-0}"
QWEN_MAX_TOKENS="${QWEN_MAX_TOKENS:-1200}"
QWEN_TEMPERATURE="${QWEN_TEMPERATURE:-0.0}"
QWEN_TIMEOUT="${QWEN_TIMEOUT:-240}"
UE58_WORKERS="${UE58_WORKERS:-6}"
SIMWORLD_GPU="${SIMWORLD_GPU:-3}"
N_VIEWS="${N_VIEWS:-8}"
MIN_VIEWS="${MIN_VIEWS:-4}"
RES="${RES:-1024}"
ENSURE_POSTGRES_DB="${ENSURE_POSTGRES_DB:-1}"

mkdir -p "$RUN_DIR"
LOG="${RUN_DIR}/runner.log"

if [[ "$ENSURE_POSTGRES_DB" == "1" || "$ENSURE_POSTGRES_DB" == "true" ]]; then
  POSTGRES_URL="$POSTGRES_URL" python3 "${REPO_ROOT}/tools/ensure_postgres_database.py" --postgres-url "$POSTGRES_URL"
fi

CMD=(
  python3 "${REPO_ROOT}/tools/ue58_parallel_asset_index_runner.py" run
  --asset-db-dir "$ASSET_DB_DIR"
  --manifest "$MANIFEST"
  --run-root "$RUN_ROOT"
  --run-dir "$RUN_DIR"
  --postgres-url "$POSTGRES_URL"
  --qdrant-url "$QDRANT_URL"
  --qdrant-collection "$QDRANT_COLLECTION"
  --caption-provider "$CAPTION_PROVIDER"
  --qwen-base-url "$QWEN_BASE_URL"
  --qwen-model "$QWEN_MODEL"
  --qwen-max-tokens "$QWEN_MAX_TOKENS"
  --qwen-temperature "$QWEN_TEMPERATURE"
  --qwen-timeout "$QWEN_TIMEOUT"
  --workers "$UE58_WORKERS"
  --gpu "$SIMWORLD_GPU"
  --n-views "$N_VIEWS"
  --min-views "$MIN_VIEWS"
  --res "$RES"
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
  echo "qwen_base_url=${QWEN_BASE_URL}"
  echo "qwen_model=${QWEN_MODEL}"
  echo "qwen_enable_thinking=${QWEN_ENABLE_THINKING}"
  echo "workers=${UE58_WORKERS}"
  echo "gpu=${SIMWORLD_GPU}"
  echo "n_views=${N_VIEWS}"
  echo "res=${RES}"
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

echo "Launched UE 5.8 parallel asset index run."
echo "  pid: ${PID}"
echo "  run_dir: ${RUN_DIR}"
echo "  log: ${LOG}"
echo "  status: python3 ${REPO_ROOT}/tools/full_asset_index_runner.py status --asset-db-dir ${ASSET_DB_DIR} --run-dir ${RUN_DIR}"
