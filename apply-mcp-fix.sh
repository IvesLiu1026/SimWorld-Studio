#!/bin/bash
# Apply the MCPServerRunnable TCP accumulation fix to a SimWorld UE project.
#
# Usage:
#   ./apply-mcp-fix.sh                          # auto-detect project under /data/$USER
#   ./apply-mcp-fix.sh /path/to/MyProject       # explicit project root
#
# What it does:
#   1. Copies the patched MCPServerRunnable.cpp into the project's plugin source.
#   2. Copies the pre-built libUnrealEditor-UnrealMCP.so so UE picks up the fix
#      immediately — no rebuild required.
#
# The bug fixed:
#   UE's MCPServerRunnable read TCP data in 8192-byte chunks and tried to parse
#   each chunk as a complete JSON message. Large execute_python_script payloads
#   (> 8KB) were silently dropped because they arrived in multiple TCP packets.
#   The fix accumulates data across reads until a complete JSON object is received.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIXED_CPP="$SCRIPT_DIR/patches/MCPServerRunnable_fixed.cpp"
FIXED_SO="$SCRIPT_DIR/patches/libUnrealEditor-UnrealMCP.so"

if [ ! -f "$FIXED_CPP" ] || [ ! -f "$FIXED_SO" ]; then
    echo "ERROR: patch files not found under $SCRIPT_DIR/patches/"
    echo "Make sure you ran: git pull"
    exit 1
fi

# ── Find project root ──────────────────────────────────────────────────────────
if [ -n "$1" ]; then
    PROJECT_ROOT="$1"
else
    # Try common locations for current user
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
    echo "ERROR: Could not find a SimWorld UE project for user '$USER'."
    echo "Pass the project root explicitly: $0 /path/to/project"
    exit 1
fi

CPP_TARGET="$PROJECT_ROOT/Plugins/UnrealMCP/Source/UnrealMCP/Private/MCPServerRunnable.cpp"
SO_TARGET="$PROJECT_ROOT/Plugins/UnrealMCP/Binaries/Linux/libUnrealEditor-UnrealMCP.so"

if [ ! -f "$CPP_TARGET" ]; then
    echo "ERROR: Plugin source not found at $CPP_TARGET"
    exit 1
fi
if [ ! -f "$SO_TARGET" ]; then
    echo "ERROR: Plugin binary not found at $SO_TARGET"
    exit 1
fi

echo "Applying MCPServerRunnable fix to: $PROJECT_ROOT"

# Backup originals
cp "$CPP_TARGET" "${CPP_TARGET}.bak" && echo "  Backed up .cpp"
cp "$SO_TARGET"  "${SO_TARGET}.bak"  && echo "  Backed up .so"

# Apply patch
cp "$FIXED_CPP" "$CPP_TARGET" && echo "  Patched MCPServerRunnable.cpp"
cp "$FIXED_SO"  "$SO_TARGET"  && echo "  Replaced libUnrealEditor-UnrealMCP.so"

echo ""
echo "Done. Restart UE (or ./SimWorld-Studio.sh) to pick up the fix."
