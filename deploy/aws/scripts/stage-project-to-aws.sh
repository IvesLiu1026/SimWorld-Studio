#!/usr/bin/env bash
# stage-project-to-aws.sh — Ship the UE project skeleton from your local dev
# box (the SimWorld-Studio-Internal dev machine) up to the AWS instance.
#
# Run this LOCALLY, not on the AWS box.
#
# Ships ~302 MB:
#   SimWorld.uproject + gym_citynav.uproject (uproject files)
#   Plugins/   (302 MB — UnrealCV + project plugins)
#   Source/    (100 KB)
#   Config/    (44 KB)
#   Scripts/   (24 KB)
#
# Excludes:
#   Content/                 → comes from HuggingFace (download-content.sh)
#   Binaries/ Intermediate/  → rebuilt on first UE launch
#   DerivedDataCache/        → regenerates at runtime
#   Saved/                   → per-slot runtime state, NEVER share
#   *.sln, *.code-workspace  → Windows-only
#   transfer_*.sh            → internal dev-box transfer scripts
#
# Usage:
#   stage-project-to-aws.sh ubuntu@<EC2_IP>
#   stage-project-to-aws.sh -i ~/.ssh/my-key.pem ubuntu@<EC2_IP>
#   SRC=/data/koe/simworld_studio_projects stage-project-to-aws.sh ubuntu@<EC2_IP>

set -euo pipefail

SRC="${SRC:-/data/koe/simworld_studio_projects}"
DEST_PATH="${DEST_PATH:-/opt/simworld-project}"
SSH_OPTS=()

# Allow `-i keyfile` and other ssh flags before the host
while [[ $# -gt 1 && "$1" == -* ]]; do
    SSH_OPTS+=("$1" "$2"); shift 2
done

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 [-i keyfile] user@host"
    echo "       SRC=/path/to/project $0 user@host"
    exit 1
fi

HOST="$1"

if [[ ! -d "$SRC" ]]; then
    echo "ERROR: source not found: $SRC" >&2
    echo "Set SRC=... env var to your UE project root." >&2
    exit 1
fi
if [[ ! -f "$SRC/SimWorld.uproject" ]]; then
    echo "ERROR: $SRC/SimWorld.uproject not found — wrong SRC?" >&2
    exit 1
fi

echo "Source : $SRC"
echo "Target : $HOST:$DEST_PATH"
echo "Includes: uproject + Plugins + Source + Config + Scripts"
echo "Excludes: Content + Binaries + Intermediate + DerivedDataCache + Saved"
echo

# Ensure the target dir exists and is writable by the simworld user.
ssh "${SSH_OPTS[@]}" "$HOST" "sudo install -d -o simworld -g simworld $DEST_PATH"

# Use rsync with --rsync-path=sudo so files land owned by simworld:simworld
rsync -avh --progress \
    --rsync-path="sudo rsync" \
    --delete \
    --exclude='Content/' \
    --exclude='Binaries/' \
    --exclude='Intermediate/' \
    --exclude='DerivedDataCache/' \
    --exclude='Saved/' \
    --exclude='*.sln' \
    --exclude='*.code-workspace' \
    --exclude='transfer_*.sh' \
    --exclude='.git/' \
    -e "ssh ${SSH_OPTS[*]}" \
    "$SRC/" "$HOST:$DEST_PATH/"

# Symlink Content → /opt/simworld-content (the HF-downloaded path)
ssh "${SSH_OPTS[@]}" "$HOST" "sudo -u simworld bash -c '
    cd $DEST_PATH
    if [ ! -e Content ]; then
        ln -s /opt/simworld-content Content
        echo \"[remote] symlinked $DEST_PATH/Content → /opt/simworld-content\"
    fi
'"

echo
echo "Done. On the AWS box you can verify with:"
echo "  ssh $HOST 'ls -la $DEST_PATH/'"
echo
echo "Next: trigger Content download if not already done:"
echo "  ssh $HOST 'sudo systemctl start simworld-content-init'"
