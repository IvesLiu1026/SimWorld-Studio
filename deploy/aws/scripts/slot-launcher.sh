#!/usr/bin/env bash
# slot-launcher.sh — Launch one UE instance for a given slot.
#
# Each slot has:
#   - Its own GPU              (--graphicsadapter $GPU)
#   - Its own MCP / Cirrus / UCV ports (derived from slot id)
#   - Its own Saved/Intermediate dirs at /var/lib/simworld/slots/$SLOT
#   - Shared READ-ONLY project + engine + Content (mounted via symlinks)
#
# Designed to be spawned by slot-pool.js (Node child_process); inherits
# CLAUDE_HOME so the UE-side MCP server reuses the shared OAuth.
#
# Usage:
#   slot-launcher.sh --slot 0 [--map /Game/Main.umap]
#   slot-launcher.sh --stop --slot 0      # kill the slot's UE if running

set -euo pipefail

# ── Defaults from environment, with sensible fallbacks ────────────────────────
UE_ENGINE_DIR="${UE_ENGINE_DIR:-/opt/ue-engine}"
UE_PROJECT_DIR_SHARED="${UE_PROJECT_DIR_SHARED:-/opt/simworld-project}"
SLOTS_ROOT="${SLOTS_ROOT:-/var/lib/simworld/slots}"
CIRRUS_JS="${CIRRUS_JS:-$UE_ENGINE_DIR/Engine/Plugins/Media/PixelStreaming/Resources/WebServers/SignallingWebServer/cirrus.js}"

UE_BASE_MCP="${UE_BASE_MCP:-55559}"
UE_BASE_CIRRUS_HTTP="${UE_BASE_CIRRUS_HTTP:-8585}"
UE_BASE_CIRRUS_WS="${UE_BASE_CIRRUS_WS:-8586}"
UE_BASE_CIRRUS_SFU="${UE_BASE_CIRRUS_SFU:-8989}"
UE_BASE_UCV="${UE_BASE_UCV:-9017}"
UE_PORT_STRIDE="${UE_PORT_STRIDE:-2}"

MAP="${MAP:-/Game/Main.umap}"
RES_X="${RES_X:-1280}"
RES_Y="${RES_Y:-720}"
FPS_MAX="${FPS_MAX:-15}"

SLOT=""
ACTION="start"

usage() {
    cat <<EOF
Usage: $0 --slot N [--map PATH] [--stop]

Launches (or stops) one UE instance bound to slot N.

Ports for slot N:
  MCP        = $UE_BASE_MCP + N * $UE_PORT_STRIDE
  Cirrus HTTP= $UE_BASE_CIRRUS_HTTP + N * $UE_PORT_STRIDE
  Cirrus WS  = $UE_BASE_CIRRUS_WS + N * $UE_PORT_STRIDE
  Cirrus SFU = $UE_BASE_CIRRUS_SFU + N * $UE_PORT_STRIDE
  UnrealCV   = $UE_BASE_UCV + N

GPU index = N (one GPU per slot).

Slot state is at \$SLOTS_ROOT/N/.
EOF
}

while [[ $# -gt 0 ]]; do
    case $1 in
        --slot) SLOT="$2"; shift 2 ;;
        --map)  MAP="$2"; shift 2 ;;
        --stop) ACTION="stop"; shift ;;
        --help|-h) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
    esac
done

if ! [[ "$SLOT" =~ ^[0-9]+$ ]]; then
    echo "ERROR: --slot N (integer) is required" >&2
    exit 1
fi

# ── Derived ───────────────────────────────────────────────────────────────────
MCP_PORT=$((UE_BASE_MCP + SLOT * UE_PORT_STRIDE))
CIRRUS_HTTP=$((UE_BASE_CIRRUS_HTTP + SLOT * UE_PORT_STRIDE))
CIRRUS_WS=$((UE_BASE_CIRRUS_WS + SLOT * UE_PORT_STRIDE))
CIRRUS_SFU=$((UE_BASE_CIRRUS_SFU + SLOT * UE_PORT_STRIDE))
UCV_PORT=$((UE_BASE_UCV + SLOT))
GPU=$SLOT

SLOT_DIR="$SLOTS_ROOT/$SLOT"
PROJECT_FILE_LINK="$SLOT_DIR/SimWorld.uproject"
LOG_DIR="$SLOT_DIR/logs"
PID_FILE="$SLOT_DIR/run.pid"
CIRRUS_PID_FILE="$SLOT_DIR/cirrus.pid"
CIRRUS_CONFIG="$SLOT_DIR/cirrus-config.json"

mkdir -p "$LOG_DIR"

# ── Stop ──────────────────────────────────────────────────────────────────────
if [[ "$ACTION" == "stop" ]]; then
    for pf in "$PID_FILE" "$CIRRUS_PID_FILE"; do
        if [[ -f "$pf" ]]; then
            pid=$(cat "$pf" 2>/dev/null || true)
            if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
                echo "[slot $SLOT] SIGTERM pid $pid ($(basename "$pf"))"
                kill "$pid" 2>/dev/null || true
                # Grace period, then SIGKILL
                for _ in 1 2 3 4 5 6 7 8 9 10; do
                    sleep 1
                    kill -0 "$pid" 2>/dev/null || break
                done
                kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
            fi
            rm -f "$pf"
        fi
    done
    echo "[slot $SLOT] stopped"
    exit 0
fi

# ── Preflight ─────────────────────────────────────────────────────────────────
UE_EDITOR="$UE_ENGINE_DIR/Engine/Binaries/Linux/UnrealEditor"
if [[ ! -x "$UE_EDITOR" ]]; then
    echo "ERROR: UnrealEditor not found at $UE_EDITOR" >&2
    exit 1
fi
if [[ ! -d "$UE_PROJECT_DIR_SHARED" ]]; then
    echo "ERROR: shared project dir missing at $UE_PROJECT_DIR_SHARED" >&2
    exit 1
fi

# ── Slot dir init (symlinked layout) ──────────────────────────────────────────
# We can't run UE directly against /opt/simworld-project because UE writes to
# Saved/ and Intermediate/. Instead each slot has its own dir with symlinks to
# the read-only shared resources, plus its own writable Saved/Intermediate.
init_slot_dir() {
    mkdir -p "$SLOT_DIR/Saved" "$SLOT_DIR/Intermediate"
    # uproject + Plugins + Config + Content → symlink to shared
    for entry in SimWorld.uproject Plugins Config Content Source; do
        src="$UE_PROJECT_DIR_SHARED/$entry"
        dst="$SLOT_DIR/$entry"
        if [[ -e "$src" && ! -e "$dst" ]]; then
            ln -s "$src" "$dst"
        fi
    done
}
init_slot_dir

# ── Write UnrealCV port to per-slot ini (cmdline flag is broken upstream) ─────
UCV_INI="$SLOT_DIR/Saved/unrealcv.ini"
printf '[UnrealCV.Core]\nPort=%s\nWidth=640\nHeight=480\nFOV=90\nEnableInput=True\nEnableRightEye=False\n' \
    "$UCV_PORT" > "$UCV_INI"

# ── GPU / Vulkan / headless env ───────────────────────────────────────────────
export CUDA_VISIBLE_DEVICES="$GPU"
NVIDIA_ICD="/usr/share/vulkan/icd.d/nvidia_icd.json"
[[ -f "$NVIDIA_ICD" ]] && export VK_ICD_FILENAMES="$NVIDIA_ICD"
export SDL_VIDEODRIVER="${SDL_VIDEODRIVER:-offscreen}"

# ── 1. Cirrus signalling server ───────────────────────────────────────────────
cat > "$CIRRUS_CONFIG" <<EOF
{
  "UseFrontend": true,
  "UseMatchmaker": false,
  "HttpPort": $CIRRUS_HTTP,
  "StreamerPort": $CIRRUS_WS,
  "SFUPort": $CIRRUS_SFU
}
EOF

if [[ -f "$CIRRUS_JS" ]]; then
    echo "[slot $SLOT][cirrus] starting HTTP:$CIRRUS_HTTP WS:$CIRRUS_WS"
    node "$CIRRUS_JS" --configFile="$CIRRUS_CONFIG" \
        >> "$LOG_DIR/cirrus.log" 2>&1 &
    CIRRUS_PID=$!
    echo "$CIRRUS_PID" > "$CIRRUS_PID_FILE"

    # Verify Cirrus bound the port (up to 8 s)
    for _ in 1 2 3 4; do
        sleep 2
        if ss -ltn "sport = :$CIRRUS_HTTP" 2>/dev/null | grep -q LISTEN; then
            echo "[slot $SLOT][cirrus] up pid=$CIRRUS_PID"
            break
        fi
        if ! kill -0 "$CIRRUS_PID" 2>/dev/null; then
            echo "[slot $SLOT][cirrus] ERROR exited"; tail -20 "$LOG_DIR/cirrus.log"
            exit 1
        fi
    done
else
    echo "[slot $SLOT][cirrus] WARNING: cirrus.js missing at $CIRRUS_JS — pixel streaming disabled"
fi

# ── 2. UE Editor ──────────────────────────────────────────────────────────────
echo "[slot $SLOT][ue] launching headless on GPU $GPU, MCP=$MCP_PORT UCV=$UCV_PORT"
"$UE_EDITOR" "$PROJECT_FILE_LINK" "$MAP" \
    -MCPPort=$MCP_PORT \
    -Unattended -NOSPLASH -NOSOUND -Messaging \
    -ResX=$RES_X -ResY=$RES_Y -FPSMAX=$FPS_MAX \
    -graphicsadapter=$GPU \
    -RenderOffScreen \
    -EditorPixelStreamingRes=${RES_X}x${RES_Y} \
    -EditorPixelStreamingStartOnLaunch=true \
    -EditorPixelStreamingUseRemoteSignallingServer=true \
    -PixelStreamingURL=ws://127.0.0.1:$CIRRUS_WS \
    -log \
    >> "$LOG_DIR/ue.log" 2>&1 &
UE_PID=$!
echo "$UE_PID" > "$PID_FILE"
echo "[slot $SLOT][ue] pid=$UE_PID"

# ── 3. Wait for MCP port ──────────────────────────────────────────────────────
echo "[slot $SLOT][ue] waiting for MCP $MCP_PORT..."
WAIT=0
until nc -z 127.0.0.1 "$MCP_PORT" 2>/dev/null; do
    if ! kill -0 "$UE_PID" 2>/dev/null; then
        echo "[slot $SLOT][ue] ERROR: exited early — see $LOG_DIR/ue.log" >&2
        exit 1
    fi
    sleep 3
    WAIT=$((WAIT + 3))
    if [[ $WAIT -ge 180 ]]; then
        echo "[slot $SLOT][ue] ERROR: MCP $MCP_PORT not ready after 180s" >&2
        kill "$UE_PID" 2>/dev/null || true
        exit 1
    fi
done
echo "[slot $SLOT][ue] MCP ready on $MCP_PORT"

# ── 4. Block in foreground so the supervising slot-pool.js sees us live ───────
# When slot-pool sends SIGTERM, propagate to children.
trap 'echo "[slot $SLOT] SIGTERM — shutting down"; \
      kill ${CIRRUS_PID:-0} $UE_PID 2>/dev/null || true; \
      wait 2>/dev/null; \
      rm -f "$PID_FILE" "$CIRRUS_PID_FILE"; \
      exit 0' SIGTERM SIGINT

# Health-watch the UE process and exit if it dies (lets slot-pool restart us).
while kill -0 "$UE_PID" 2>/dev/null; do
    sleep 5
done
echo "[slot $SLOT][ue] process exited unexpectedly" >&2
kill ${CIRRUS_PID:-0} 2>/dev/null || true
rm -f "$PID_FILE" "$CIRRUS_PID_FILE"
exit 1
