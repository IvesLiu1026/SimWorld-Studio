#!/bin/bash
# Apply the MCPServerRunnable TCP accumulation fix and rebuild the plugin.
#
# Usage:
#   ./apply-mcp-fix.sh                          # auto-detect project under /data/$USER
#   ./apply-mcp-fix.sh /path/to/MyProject       # explicit project root
#
# The bug fixed:
#   UE's MCPServerRunnable read TCP in 8192-byte chunks and immediately tried to
#   parse each chunk as JSON. Scripts > 8KB were silently dropped because TCP
#   split them across multiple reads. Fix: accumulate chunks until a complete
#   JSON object arrives.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIXED_CPP="$SCRIPT_DIR/patches/MCPServerRunnable_fixed.cpp"

if [ ! -f "$FIXED_CPP" ]; then
    echo "ERROR: patches/MCPServerRunnable_fixed.cpp not found. Run: git pull"
    exit 1
fi

# ── Find project root ──────────────────────────────────────────────────────────
if [ -n "$1" ]; then
    PROJECT_ROOT="$1"
else
    for candidate in \
        "/data/$USER/simworld_studio_projects" \
        "/data/$USER/simworld_projects" \
        "/data/$USER/SimWorld-Studio-Minimal/gym_citynav"
    do
        if [ -f "$candidate/Plugins/UnrealMCP/Source/UnrealMCP/Private/MCPServerRunnable.cpp" ]; then
            PROJECT_ROOT="$candidate"
            break
        fi
    done
fi

if [ -z "$PROJECT_ROOT" ]; then
    echo "ERROR: Could not find a SimWorld UE project for '$USER'."
    echo "Pass the project root explicitly: $0 /path/to/project"
    exit 1
fi

CPP_TARGET="$PROJECT_ROOT/Plugins/UnrealMCP/Source/UnrealMCP/Private/MCPServerRunnable.cpp"
UPROJECT=$(ls "$PROJECT_ROOT"/*.uproject 2>/dev/null | head -1)

if [ ! -f "$CPP_TARGET" ]; then
    echo "ERROR: Plugin source not found at $CPP_TARGET"
    exit 1
fi
if [ -z "$UPROJECT" ]; then
    echo "ERROR: No .uproject found in $PROJECT_ROOT"
    exit 1
fi

echo "Project : $PROJECT_ROOT"
echo "uproject: $UPROJECT"
echo ""

# ── Find UE root ───────────────────────────────────────────────────────────────
UE_ROOT="${UE_ROOT:-}"
if [ -z "$UE_ROOT" ]; then
    for candidate in \
        "/data/$USER/ue/UE_5.3.2" \
        "/data/$USER/Linux_Unreal_Engine_5.3.2" \
        "/data/murray/ue/UE_5.3.2" \
        "/data/siddhant/ue/UE_5.3.2"
    do
        if [ -f "$candidate/Engine/Build/BatchFiles/Linux/Build.sh" ]; then
            UE_ROOT="$candidate"
            break
        fi
    done
fi

if [ -z "$UE_ROOT" ]; then
    echo "ERROR: UE root not found. Set UE_ROOT=/path/to/UE and re-run."
    exit 1
fi

echo "UE root : $UE_ROOT"
echo ""

# ── Patch source ───────────────────────────────────────────────────────────────
cp "$CPP_TARGET" "${CPP_TARGET}.bak"
cp "$FIXED_CPP"  "$CPP_TARGET"
echo "[1/2] Patched MCPServerRunnable.cpp"

# ── Rebuild ────────────────────────────────────────────────────────────────────
echo "[2/2] Rebuilding (incremental)..."
"$UE_ROOT/Engine/Build/BatchFiles/Linux/Build.sh" \
    UnrealEditor Linux Development \
    -Project="$UPROJECT" \
    2>&1 | grep -E "error:|Compiling|Linking|MCPServer|succeeded|failed|ERROR" || true

if [ ${PIPESTATUS[0]} -ne 0 ]; then
    echo ""
    echo "ERROR: Build failed. Restoring original .cpp..."
    cp "${CPP_TARGET}.bak" "$CPP_TARGET"
    exit 1
fi

echo ""
echo "Done. Restart UE to pick up the fix."
