#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

: "${ASSET_DB_DIR:?Set ASSET_DB_DIR to the approved writable asset database root}"
: "${MANIFEST:?Set MANIFEST to the approved immutable UE object manifest}"
: "${UE58_EDITOR:?Set UE58_EDITOR to the approved absolute SimWorldEditor path}"
: "${UE58_SOURCE_PROJECT:?Set UE58_SOURCE_PROJECT to the approved absolute UE project root}"
: "${UE58_CONTENT_ROOT:?Set UE58_CONTENT_ROOT to the approved immutable Content root}"
: "${UE58_WORKER_PROJECT_ROOT:?Set UE58_WORKER_PROJECT_ROOT to the approved absolute worker output root}"
: "${UE58_BRIDGE_SCRIPT:?Set UE58_BRIDGE_SCRIPT to the approved absolute MCP bridge script}"
RUN_ROOT="${RUN_ROOT:-${ASSET_DB_DIR}/runs}"
RUN_ID="${RUN_ID:-ue58_parallel_qwen36_$(date -u +%Y%m%d_%H%M%S)}"
RUN_DIR="${RUN_DIR:-${RUN_ROOT}/${RUN_ID}}"
# The DSN is environment-only so it cannot leak through argv or run metadata.
POSTGRES_URL="${POSTGRES_URL:-}"
export POSTGRES_URL
POSTGRES_URL_FILE="${POSTGRES_URL_FILE:-}"
export POSTGRES_URL_FILE
if [[ -n "$POSTGRES_URL" && -n "$POSTGRES_URL_FILE" ]]; then
  echo "Set exactly one of POSTGRES_URL or POSTGRES_URL_FILE." >&2
  exit 2
fi
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
UE58_DDC_MODE="${UE58_DDC_MODE:-default}"
UE58_DDC_ROOT="${UE58_DDC_ROOT:-}"
if [[ "$UE58_DDC_MODE" == "local" && -z "$UE58_DDC_ROOT" ]]; then
  echo "UE58_DDC_ROOT is required when UE58_DDC_MODE=local." >&2
  exit 2
fi
SIMWORLD_GPU="${SIMWORLD_GPU:-3}"
N_VIEWS="${N_VIEWS:-8}"
MIN_VIEWS="${MIN_VIEWS:-4}"
RES="${RES:-1024}"
ENSURE_POSTGRES_DB="${ENSURE_POSTGRES_DB:-1}"

for arg in "$@"; do
  case "$arg" in
    --postgres-url|--postgres-url=*|*postgresql://*|*postgres://*)
      echo "Refusing a Postgres DSN on the command line; set POSTGRES_URL in the service environment instead." >&2
      exit 2
      ;;
  esac
done

mkdir -p "$RUN_DIR"
LOG="${RUN_DIR}/runner.log"

if [[ "$ENSURE_POSTGRES_DB" == "1" || "$ENSURE_POSTGRES_DB" == "true" ]]; then
  if [[ -z "${POSTGRES_URL}" && -z "${POSTGRES_URL_FILE}" ]]; then
    echo "POSTGRES_URL_FILE or POSTGRES_URL is required when ENSURE_POSTGRES_DB is enabled." >&2
    exit 2
  fi
  uv run --project "${REPO_ROOT}/tools" --frozen python "${REPO_ROOT}/tools/ensure_postgres_database.py"
fi

CMD=(
  uv run --project "${REPO_ROOT}/tools" --frozen python "${REPO_ROOT}/tools/ue58_parallel_asset_index_runner.py" run
  --asset-db-dir "$ASSET_DB_DIR"
  --manifest "$MANIFEST"
  --run-root "$RUN_ROOT"
  --run-dir "$RUN_DIR"
  --qdrant-url "$QDRANT_URL"
  --qdrant-collection "$QDRANT_COLLECTION"
  --caption-provider "$CAPTION_PROVIDER"
  --qwen-base-url "$QWEN_BASE_URL"
  --qwen-model "$QWEN_MODEL"
  --qwen-max-tokens "$QWEN_MAX_TOKENS"
  --qwen-temperature "$QWEN_TEMPERATURE"
  --qwen-timeout "$QWEN_TIMEOUT"
  --workers "$UE58_WORKERS"
  --ue-editor "$UE58_EDITOR"
  --source-project "$UE58_SOURCE_PROJECT"
  --content-root "$UE58_CONTENT_ROOT"
  --worker-project-root "$UE58_WORKER_PROJECT_ROOT"
  --bridge-script "$UE58_BRIDGE_SCRIPT"
  --ddc-mode "$UE58_DDC_MODE"
  --gpu "$SIMWORLD_GPU"
  --n-views "$N_VIEWS"
  --min-views "$MIN_VIEWS"
  --res "$RES"
)

if [[ -n "$UE58_DDC_ROOT" ]]; then
  CMD+=(--ddc-root "$UE58_DDC_ROOT")
fi

case "$(printf '%s' "$QWEN_ENABLE_THINKING" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes|on) CMD+=(--qwen-enable-thinking) ;;
esac

{
  echo "repo=${REPO_ROOT}"
  echo "asset_db_dir=${ASSET_DB_DIR}"
  echo "manifest=${MANIFEST}"
  echo "run_dir=${RUN_DIR}"
  if [[ -n "${POSTGRES_URL}" || -n "${POSTGRES_URL_FILE}" ]]; then
    echo "postgres_url_configured=true"
  else
    echo "postgres_url_configured=false"
  fi
  if [[ -n "${POSTGRES_URL_FILE}" ]]; then
    echo "postgres_url_source=POSTGRES_URL_FILE"
  else
    echo "postgres_url_source=POSTGRES_URL_environment"
  fi
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
echo "  status: uv run --project ${REPO_ROOT}/tools --frozen python ${REPO_ROOT}/tools/full_asset_index_runner.py status --asset-db-dir ${ASSET_DB_DIR} --run-dir ${RUN_DIR}"
