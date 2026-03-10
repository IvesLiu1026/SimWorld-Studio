#!/bin/bash
# SimWorld Studio - Minimal Launch Script
# Usage: ./SimWorld-Studio.sh [--port PORT] [--render-offscreen]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE_DIR="$SCRIPT_DIR/Engine"
PROJECT_DIR="$SCRIPT_DIR/gym_citynav"
PROJECT_FILE="$PROJECT_DIR/gym_citynav.uproject"
UE_EDITOR="$ENGINE_DIR/Binaries/Linux/UnrealEditor"

# Default settings
MCP_PORT=55559
GPU_ADAPTER=""
RENDER_OFFSCREEN=""
RESOLUTION="-ResX=1280 -ResY=720"
PIXEL_STREAMING_ARGS=""
FPSMAX="-FPSMAX=15"

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --port)
            MCP_PORT="$2"
            shift 2
            ;;
        --gpu)
            GPU_ADAPTER="-graphicsadapter=$2"
            shift 2
            ;;
        --render-offscreen)
            RENDER_OFFSCREEN="-RenderOffScreen"
            shift
            ;;
        --pixel-streaming)
            PIXEL_STREAMING_ARGS="-PixelStreamingIP=127.0.0.1 -PixelStreamingPort=8586"
            shift
            ;;
        --res)
            RESOLUTION="-ResX=$2 -ResY=$3"
            shift 3
            ;;
        --help)
            echo "SimWorld Studio Launcher"
            echo ""
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --port PORT           MCP TCP port (default: 55559)"
            echo "  --gpu INDEX           GPU adapter index (default: auto)"
            echo "  --render-offscreen    Run without display window (headless)"
            echo "  --pixel-streaming     Enable Pixel Streaming on port 8586"
            echo "  --res WIDTH HEIGHT    Set resolution (default: 1280 720)"
            echo "  --help                Show this help"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

# Check if editor exists
if [ ! -f "$UE_EDITOR" ]; then
    echo "ERROR: UnrealEditor not found at $UE_EDITOR"
    exit 1
fi

# Check if project exists
if [ ! -f "$PROJECT_FILE" ]; then
    echo "ERROR: Project file not found at $PROJECT_FILE"
    exit 1
fi

echo "=== SimWorld Studio ==="
echo "  Engine: $UE_EDITOR"
echo "  Project: $PROJECT_FILE"
echo "  MCP Port: $MCP_PORT"
echo "  Map: /Game/Maps/Empty"
echo ""

# Launch UE Editor
exec "$UE_EDITOR" "$PROJECT_FILE" \
    /Game/Maps/Empty.umap \
    -MCPPort=$MCP_PORT \
    -Unattended \
    -NOSPLASH \
    -NOSOUND \
    -Messaging \
    $RESOLUTION \
    $FPSMAX \
    $GPU_ADAPTER \
    $RENDER_OFFSCREEN \
    $PIXEL_STREAMING_ARGS \
    -log \
    "$@"
