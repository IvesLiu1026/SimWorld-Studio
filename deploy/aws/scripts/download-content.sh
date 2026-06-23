#!/usr/bin/env bash
# download-content.sh — Pull SimWorld Internal Content from HuggingFace once.
#
# Run as the simworld user (or root). Idempotent: the .downloaded sentinel
# prevents a second download. Triggered automatically by the
# simworld-content-init.service systemd unit on first boot.

set -euo pipefail

REPO="${SIMWORLD_HF_REPO:-SimWorld-AI/SimWorld-Internal}"
DEST="${SIMWORLD_CONTENT_DIR:-/opt/simworld-content}"
SENTINEL="$DEST/.downloaded"

log() { echo "[download-content] $*"; }

if [[ -f "$SENTINEL" ]]; then
    log "already downloaded at $DEST — skipping (delete $SENTINEL to force)"
    exit 0
fi

mkdir -p "$DEST"

# huggingface-cli is installed by bake-ami.sh
if ! command -v huggingface-cli >/dev/null; then
    log "ERROR: huggingface-cli not found; run bake-ami.sh first" >&2
    exit 1
fi

log "downloading $REPO → $DEST (this may take 10-30 min)"
huggingface-cli download "$REPO" \
    --repo-type dataset \
    --local-dir "$DEST" \
    --local-dir-use-symlinks False

# If the repo lays things out under a Content/ subdirectory, hoist it.
if [[ -d "$DEST/Content" && ! -L "$DEST/Content" ]]; then
    log "found nested Content/ — flattening"
    mv "$DEST/Content"/* "$DEST/" || true
    rmdir "$DEST/Content" 2>/dev/null || true
fi

touch "$SENTINEL"
log "done. Sentinel: $SENTINEL"
log "Symlink the project Content dir to this path:"
log "  ln -s $DEST /opt/simworld-project/Content"
