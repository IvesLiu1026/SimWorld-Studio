#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: $0 /absolute/append-only/run-dir" >&2
  exit 64
}

[[ $# -eq 1 ]] || usage

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd -P)"
ALLOWED_ROOT="${VISTA_BLENDER_ALLOWED_ROOT:-/mnt/NAS2/yhliu/SimWorldStudio/vista-blender-world/runs}"
BLENDER_BIN="${BLENDER_BIN:-/home/yhliu/.local/opt/blender-4.5.8-linux-x64/blender}"
RUN_DIR="$1"

[[ "$RUN_DIR" = /* ]] || {
  echo "Run directory must be absolute: $RUN_DIR" >&2
  exit 65
}
[[ "$ALLOWED_ROOT" = /* ]] || {
  echo "VISTA_BLENDER_ALLOWED_ROOT must be absolute: $ALLOWED_ROOT" >&2
  exit 65
}

mkdir -p -- "$ALLOWED_ROOT"
ALLOWED_ROOT="$(realpath -e -- "$ALLOWED_ROOT")"
RUN_DIR="$(realpath -m -- "$RUN_DIR")"
case "$RUN_DIR/" in
  "$ALLOWED_ROOT"/*/) ;;
  *)
    echo "Run directory must be a child of append-only root $ALLOWED_ROOT: $RUN_DIR" >&2
    exit 65
    ;;
esac
[[ "$RUN_DIR" != "$ALLOWED_ROOT" ]] || {
  echo "Run directory may not equal the append-only root" >&2
  exit 65
}

[[ -x "$BLENDER_BIN" ]] || {
  echo "Pinned Blender executable not found: $BLENDER_BIN" >&2
  exit 69
}
BLENDER_VERSION="$($BLENDER_BIN --version | sed -n '1p')"
case "$BLENDER_VERSION" in
  "Blender 4.5.8"*) ;;
  *)
    echo "Pinned Blender 4.5.8 is required; found: $BLENDER_VERSION" >&2
    exit 69
    ;;
esac

umask 077
mkdir -p -- "$RUN_DIR"
[[ ! -L "$RUN_DIR" ]] || {
  echo "Run directory may not be a symbolic link: $RUN_DIR" >&2
  exit 65
}

BLENDER_OUTPUT="$RUN_DIR/blender"
LOG_DIR="$BLENDER_OUTPUT/logs"
BUILD_LOG="$LOG_DIR/headless-build.log"
VALIDATION_RECEIPT="$BLENDER_OUTPUT/validation.json"
for controlled in \
  "$BLENDER_OUTPUT/source.blend" \
  "$BLENDER_OUTPUT/vista_mmg040_office.glb" \
  "$BLENDER_OUTPUT/vista_mmg040_office.gltf" \
  "$BLENDER_OUTPUT/vista_mmg040_office.bin" \
  "$BLENDER_OUTPUT/preview-overview.png" \
  "$BLENDER_OUTPUT/preview-detail.png" \
  "$BLENDER_OUTPUT/manifest.json" \
  "$BUILD_LOG" \
  "$VALIDATION_RECEIPT"; do
  [[ ! -e "$controlled" && ! -L "$controlled" ]] || {
    echo "Refusing to replace append-only Blender artifact: $controlled" >&2
    exit 73
  }
done
mkdir -p -- "$LOG_DIR"

echo "Building VISTA mmg_040 asset with $BLENDER_VERSION" >&2
echo "Append-only output: $BLENDER_OUTPUT" >&2
PYTHONHASHSEED=0 OMP_NUM_THREADS=1 "$BLENDER_BIN" \
  --background \
  --factory-startup \
  -noaudio \
  --threads 1 \
  --python "$SCRIPT_DIR/build_vista_mmg040_office.py" \
  -- \
  --output-root "$BLENDER_OUTPUT" \
  --seed 4040 \
  2>&1 | tee "$BUILD_LOG"

uv run --project "$REPO_ROOT/tools" \
  python "$SCRIPT_DIR/validate_vista_asset.py" \
  "$BLENDER_OUTPUT/manifest.json" \
  --json | tee "$VALIDATION_RECEIPT"

echo "VISTA Blender bundle passed validation: $BLENDER_OUTPUT/manifest.json" >&2
