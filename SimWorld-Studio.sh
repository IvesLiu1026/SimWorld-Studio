#!/bin/bash
# SimWorld Studio - Launch Script
# Starts: Cirrus (Pixel Streaming) + UE Editor + Web UI

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UE_ROOT="${UE_ROOT:-/data/koe/Linux_Unreal_Engine_5.3.2}"
UE_PROJECT_PATH="${UE_PROJECT_PATH:-/data/koe/simworld_studio_projects}"
ENGINE_DIR="$UE_ROOT/Engine"
PROJECT_DIR="$UE_PROJECT_PATH"
PROJECT_FILE="$PROJECT_DIR/SimWorld.uproject"
UE_EDITOR="$ENGINE_DIR/Binaries/Linux/UnrealEditor"
WEB_DIR="$SCRIPT_DIR/simworld_studio_workspace/web/server"
WORKSPACE="$SCRIPT_DIR/simworld_studio_workspace"
CIRRUS_JS="$ENGINE_DIR/Plugins/Media/PixelStreaming/Resources/WebServers/SignallingWebServer/cirrus.js"

# Defaults
WEB_PORT=3005
MCP_PORT=55564
GPU_INDEX=0
CIRRUS_HTTP_PORT=8589
CIRRUS_WS_PORT=8590
CIRRUS_SFU_PORT=8893
MAP="/Game/Main.umap"

usage() {
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  --gpu INDEX               GPU index (default: 0)"
    echo "  --port PORT               Web UI port (default: 3002)"
    echo "  --mcp-port PORT           UE MCP port (default: 55559)"
    echo "  --cirrus-http-port PORT   Cirrus HTTP port (default: 8585)"
    echo "  --cirrus-ws-port PORT     Cirrus WebSocket port (default: 8586)"
    echo "  --cirrus-sfu-port PORT    Cirrus SFU port (default: 8889)"
    echo "  --ucv-port PORT           UnrealCV port (default: 9017; must be free)"
    echo "  --map MAP                 UE map path (default: /Game/Main.umap)"
    echo "  --help                    Show this help"
    echo ""
    echo "Examples:"
    echo "  $0 --gpu 0"
    echo "  $0 --gpu 1 --port 3003 --mcp-port 55560"
}

while [[ $# -gt 0 ]]; do
    case $1 in
        --port)             WEB_PORT="$2";           shift 2 ;;
        --mcp-port)         MCP_PORT="$2";            shift 2 ;;
        --gpu)              GPU_INDEX="$2";           shift 2 ;;
        --cirrus-http-port) CIRRUS_HTTP_PORT="$2";    shift 2 ;;
        --cirrus-ws-port)   CIRRUS_WS_PORT="$2";      shift 2 ;;
        --cirrus-sfu-port)  CIRRUS_SFU_PORT="$2";     shift 2 ;;
        --ucv-port)         UNREALCV_PORT="$2";       shift 2 ;;
        --map)              MAP="$2";                 shift 2 ;;
        --help|-h)          usage; exit 0 ;;
        *) echo "Unknown option: $1 (use --help)"; exit 1 ;;
    esac
done

# ── Preflight checks ─────────────────────────────────────────────────────────
if [ ! -f "$UE_EDITOR" ]; then
    echo "ERROR: UnrealEditor not found at $UE_EDITOR"; exit 1
fi
if [ ! -f "$PROJECT_FILE" ]; then
    echo "ERROR: Project file not found at $PROJECT_FILE"; exit 1
fi

# ── GPU isolation ─────────────────────────────────────────────────────────────
export CUDA_VISIBLE_DEVICES="$GPU_INDEX"
NVIDIA_ICD="/usr/share/vulkan/icd.d/nvidia_icd.json"
[ -f "$NVIDIA_ICD" ] && export VK_ICD_FILENAMES="$NVIDIA_ICD"

# ── SDL: prevent Wayland/X11 message-box crash on headless servers ────────────
export SDL_VIDEODRIVER="${SDL_VIDEODRIVER:-offscreen}"

# ── UnrealCV port (write to Saved ini — command-line arg is broken upstream) ──
UNREALCV_PORT="${UNREALCV_PORT:-${UCV_PORT:-9017}}"
SAVED_INI="$PROJECT_DIR/Saved/unrealcv.ini"
mkdir -p "$(dirname "$SAVED_INI")"
if [ -f "$SAVED_INI" ]; then
    sed -i "s/^Port=.*/Port=$UNREALCV_PORT/" "$SAVED_INI"
else
    printf '[UnrealCV.Core]\nPort=%s\nWidth=640\nHeight=480\nFOV=90\nEnableInput=True\nEnableRightEye=False\n' "$UNREALCV_PORT" > "$SAVED_INI"
fi

mkdir -p "$WORKSPACE/logs"

# ── Rotate logs (keep last run clean) ────────────────────────────────────────
for f in cirrus ue web; do
    > "$WORKSPACE/logs/$f.log"
done

# ── Write mcp.json for coding agent (keep in sync with MCP_PORT) ─────────────
MCP_SERVER_JS="$WEB_DIR/mcp-server.js"
cat > "$WORKSPACE/web/mcp.json" <<EOF
{"mcpServers":{"simworld":{"command":"node","args":["$MCP_SERVER_JS"],"env":{"UNREAL_HOST":"127.0.0.1","UNREAL_PORT":"$MCP_PORT"}}}}
EOF
echo "[mcp.json] Written → UNREAL_PORT=$MCP_PORT"

echo ""
echo "======================================================="
echo "  SimWorld Studio"
echo "======================================================="
echo "  Web UI:   http://localhost:$WEB_PORT"
echo "  MCP:      $MCP_PORT  |  GPU: $GPU_INDEX"
echo "  Cirrus:   HTTP:$CIRRUS_HTTP_PORT  WS:$CIRRUS_WS_PORT  SFU:$CIRRUS_SFU_PORT"
echo "  Map:      $MAP"
echo "======================================================="
echo ""

PIDS=()

cleanup() {
    echo ""
    echo "[studio] Shutting down..."
    for pid in "${PIDS[@]}"; do
        kill "$pid" 2>/dev/null
    done
    wait 2>/dev/null
    echo "[studio] Done."
    exit 0
}
trap cleanup SIGINT SIGTERM

# ── 1. Cirrus (Pixel Streaming signaling server) ──────────────────────────────
CIRRUS_CONFIG="$WORKSPACE/cirrus-config.json"
cat > "$CIRRUS_CONFIG" <<EOF
{
  "UseFrontend": true,
  "UseMatchmaker": false,
  "HttpPort": $CIRRUS_HTTP_PORT,
  "StreamerPort": $CIRRUS_WS_PORT,
  "SFUPort": $CIRRUS_SFU_PORT
}
EOF

if [ -f "$CIRRUS_JS" ]; then
    echo "[cirrus] Starting on HTTP:$CIRRUS_HTTP_PORT WS:$CIRRUS_WS_PORT..."
    node "$CIRRUS_JS" --configFile="$CIRRUS_CONFIG" \
        >> "$WORKSPACE/logs/cirrus.log" 2>&1 &
    CIRRUS_PID=$!
    PIDS+=($CIRRUS_PID)
    # Verify cirrus actually bound the HTTP port (up to 8s)
    CIRRUS_OK=0
    for i in 1 2 3 4; do
        sleep 2
        if nc -z 127.0.0.1 $CIRRUS_HTTP_PORT 2>/dev/null; then
            CIRRUS_OK=1; break
        fi
        if ! kill -0 $CIRRUS_PID 2>/dev/null; then
            echo "[cirrus] ERROR: cirrus exited. Last log:"
            tail -20 "$WORKSPACE/logs/cirrus.log"
            cleanup
        fi
    done
    if [ $CIRRUS_OK -eq 1 ]; then
        echo "[cirrus] PID $CIRRUS_PID — port $CIRRUS_HTTP_PORT OK"
    else
        echo "[cirrus] ERROR: port $CIRRUS_HTTP_PORT not listening after 8s. Last log:"
        tail -20 "$WORKSPACE/logs/cirrus.log"
        cleanup
    fi
else
    echo "[cirrus] WARNING: cirrus.js not found — Pixel Streaming viewport unavailable"
    CIRRUS_PID=""
fi

# ── 2. UE Editor ──────────────────────────────────────────────────────────────
echo "[ue] Launching UnrealEditor (headless, GPU $GPU_INDEX)..."
"$UE_EDITOR" "$PROJECT_FILE" \
    "$MAP" \
    -MCPPort=$MCP_PORT \
    -Unattended -NOSPLASH -NOSOUND -Messaging \
    -ResX=1280 -ResY=720 -FPSMAX=15 \
    -graphicsadapter=$GPU_INDEX \
    -RenderOffScreen \
    -EditorPixelStreamingRes=1280x720 \
    -EditorPixelStreamingStartOnLaunch=true \
    -EditorPixelStreamingUseRemoteSignallingServer=true \
    -PixelStreamingURL=ws://127.0.0.1:$CIRRUS_WS_PORT \
    -log \
    >> "$WORKSPACE/logs/ue.log" 2>&1 &
UE_PID=$!
PIDS+=($UE_PID)
echo "[ue] PID $UE_PID, log: $WORKSPACE/logs/ue.log"

# ── 3. Wait for MCP port ──────────────────────────────────────────────────────
echo "[ue] Waiting for MCP port $MCP_PORT..."
WAIT=0
until nc -z 127.0.0.1 $MCP_PORT 2>/dev/null; do
    if ! kill -0 $UE_PID 2>/dev/null; then
        echo "[ue] ERROR: UE exited early. Check $WORKSPACE/logs/ue.log"
        cleanup
    fi
    sleep 3
    WAIT=$((WAIT+3))
    if [ $WAIT -ge 120 ]; then
        echo "[ue] ERROR: MCP port $MCP_PORT not ready after 120s"
        cleanup
    fi
done
echo "[ue] MCP ready!"
# Immersive mode (F11) is triggered by the web app on first stream-connect via
# POST /api/immersive (the viewport is active then, which is more reliable than toggling
# at launch before any browser attaches).

# ── 4. Web UI server ──────────────────────────────────────────────────────────
if [ -f "$WEB_DIR/index.js" ]; then
    echo "[web] Starting on port $WEB_PORT..."
    cd "$WEB_DIR"
    PORT=$WEB_PORT \
    UNREAL_HOST=127.0.0.1 \
    UNREAL_PORT=$MCP_PORT \
    UCV_PORT=$UNREALCV_PORT \
    UE_PROJECT_PATH=$PROJECT_DIR \
    PIXEL_STREAMING_URL=http://127.0.0.1:$CIRRUS_HTTP_PORT \
    CIRRUS_HTTP_PORT=$CIRRUS_HTTP_PORT \
    CIRRUS_WS_PORT=$CIRRUS_WS_PORT \
    SESSION_TTL_MS=3600000 \
    SESSION_HARD_MAX_MS=14400000 \
    node index.js >> "$WORKSPACE/logs/web.log" 2>&1 &
    WEB_PID=$!
    PIDS+=($WEB_PID)
    echo "[web] PID $WEB_PID, log: $WORKSPACE/logs/web.log"
    cd "$SCRIPT_DIR"
else
    echo "[web] WARNING: $WEB_DIR/index.js not found"
fi

echo ""
echo "  Open: http://localhost:$WEB_PORT"
echo "  Press Ctrl+C to stop all services."
echo ""

# ── Monitor: watch all three processes ───────────────────────────────────────
while true; do
    sleep 5
    if ! kill -0 $UE_PID 2>/dev/null; then
        echo "[studio] UE exited unexpectedly. Check $WORKSPACE/logs/ue.log"
        cleanup
    fi
    if [ -n "$CIRRUS_PID" ] && ! kill -0 $CIRRUS_PID 2>/dev/null; then
        echo "[studio] Cirrus exited unexpectedly. Check $WORKSPACE/logs/cirrus.log"
        cleanup
    fi
    if [ -n "$WEB_PID" ] && ! kill -0 $WEB_PID 2>/dev/null; then
        echo "[studio] Web server exited unexpectedly. Check $WORKSPACE/logs/web.log"
        cleanup
    fi
done
