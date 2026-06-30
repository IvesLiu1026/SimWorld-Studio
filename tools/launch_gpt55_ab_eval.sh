#!/usr/bin/env bash
set -euo pipefail

RUN_ROOT="${1:?run dir required}"
SERVER_PORT="${2:-3141}"
UE_PORT="${3:-55570}"

mkdir -p "$RUN_ROOT"
echo "$RUN_ROOT" > /data/siddhant/asset_db_ue58_qwen/ab_eval/latest_gpt55_40way_run_dir.txt

SERVER_LOG="$RUN_ROOT/server_${SERVER_PORT}.log"
BATCH_LOG="$RUN_ROOT/batch.log"
LAUNCH_LOG="$RUN_ROOT/launcher.log"
: > "$SERVER_LOG"
: > "$BATCH_LOG"
: > "$LAUNCH_LOG"

exec > >(tee -a "$LAUNCH_LOG") 2>&1

echo "started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "run_root=$RUN_ROOT"
echo "server_port=$SERVER_PORT"
echo "ue_port=$UE_PORT"
echo "provider=codex model=gpt-5.5"

export HOME=/data/siddhant/codex_runtime_home
export CODEX_HOME=/data/siddhant/codex_runtime_home/.codex
export PORT="$SERVER_PORT"
export UNREAL_HOST=127.0.0.1
export UNREAL_PORT="$UE_PORT"
export LLM_PROVIDER=codex
export LLM_ONESHOT_PROVIDER=codex
export CODEX_MODEL=gpt-5.5
export SUMMARIZER_MODEL=gpt-5.5
export CRITIC_MODEL=gpt-5.5
export LLM_ONESHOT_TIMEOUT_MS=300000
export CODEX_TIMEOUT_MS=1800000
export CODEX_IDLE_TIMEOUT_MS=1800000
export SCENE_LOOP_MAX_ROUNDS=2
export CRITIC_TIMEOUT_MS=300000
export SUMMARIZER_TIMEOUT_MS=180000
export ASSET_DB_DIR=/data/siddhant/asset_db_ue58_qwen
export POSTGRES_URL=postgresql://simworld:simworld@127.0.0.1:55432/asset_db_ue58_qwen
export QDRANT_URL=http://127.0.0.1:6333
export QDRANT_COLLECTION=assets_ue58_qwen
export EMBED_SERVICE_URL=http://127.0.0.1:7777
export ASSET_RETRIEVAL_MODE=db
export ASSET_PREFILTER=true
export PREFILTER_TOP_K=150
export SIMWORLD_UE_SCREENSHOT_DIR=/tmp/simworld_screens

cd /data/siddhant/SimWorld-Studio/simworld_studio_workspace/web/server
./start.sh > "$SERVER_LOG" 2>&1 &
SERVER_PID=$!
echo "$SERVER_PID" > "$RUN_ROOT/server_${SERVER_PORT}.pid"

cleanup() {
  kill "$SERVER_PID" 2>/dev/null || true
}
trap cleanup EXIT

python3 - <<PY
import json, time, urllib.request
url = "http://127.0.0.1:${SERVER_PORT}/api/health"
for _ in range(120):
    try:
        data = json.loads(urllib.request.urlopen(url, timeout=3).read().decode())
        print(data, flush=True)
        if data.get("ueConnected"):
            break
    except Exception as e:
        print("health wait", e, flush=True)
    time.sleep(2)
else:
    raise SystemExit("server/UE health did not become ready")
PY

cd /data/siddhant/SimWorld-Studio
node tools/run_asset_retrieval_ab_eval.js \
  --server-url "http://127.0.0.1:${SERVER_PORT}" \
  --out-dir "$RUN_ROOT" \
  --all-prompts \
  --modes db,off \
  --loop-modes vanilla,visual_loop \
  --runner codex \
  --model gpt-5.5 \
  --skip-preview \
  --screenshot-angles 4 \
  --comparison-views 0 \
  --ue-host 127.0.0.1 \
  --ue-port "$UE_PORT" \
  --timeout-ms 3600000 2>&1 | tee -a "$BATCH_LOG"

echo "finished_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
