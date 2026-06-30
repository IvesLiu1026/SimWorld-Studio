#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${PORT:-7777}"
HOST="${HOST:-127.0.0.1}"
LOG_DIR="${LOG_DIR:-/data/siddhant/asset_db_ue58_qwen/runtime}"
LOG="${LOG:-${LOG_DIR}/embed_service_${PORT}.log}"
PID_FILE="${PID_FILE:-${LOG_DIR}/embed_service_${PORT}.pid}"

mkdir -p "$LOG_DIR"

if curl -fsS "http://${HOST}:${PORT}/health" >/dev/null 2>&1; then
  echo "Embedding service already healthy at http://${HOST}:${PORT}"
  exit 0
fi

if [[ -f "$PID_FILE" ]]; then
  old_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
    echo "PID file exists and process is alive, but health check failed: ${PID_FILE} pid=${old_pid}" >&2
    exit 1
  fi
fi

echo "Starting embedding service on ${HOST}:${PORT}"
echo "  log: ${LOG}"
echo "  pid: ${PID_FILE}"

nohup setsid env PORT="$PORT" \
  EMBED_DENSE_MODEL="${EMBED_DENSE_MODEL:-BAAI/bge-large-en-v1.5}" \
  EMBED_SPARSE_MODEL="${EMBED_SPARSE_MODEL:-Qdrant/bm25}" \
  python3 "${REPO_ROOT}/tools/embed_service.py" >> "$LOG" 2>&1 < /dev/null &

pid=$!
echo "$pid" > "$PID_FILE"

for _ in $(seq 1 30); do
  if curl -fsS "http://${HOST}:${PORT}/health" >/dev/null 2>&1; then
    echo "Embedding service healthy at http://${HOST}:${PORT}"
    exit 0
  fi
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "Embedding service exited early; tailing log:" >&2
    tail -80 "$LOG" >&2 || true
    exit 1
  fi
  sleep 1
done

echo "Embedding service did not become healthy within 30s; tailing log:" >&2
tail -80 "$LOG" >&2 || true
exit 1
