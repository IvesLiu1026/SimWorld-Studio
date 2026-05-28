#!/usr/bin/env bash
# bootstrap.sh — One-command provision for a fresh Ubuntu 22.04 EC2 instance.
#
# Run order (what's git-clonable vs not):
#
#   ┌─────────────────────────────────────────────────────────────────┐
#   │                                              source                │
#   ├──────────────────────────────────────────┬──────────────────────┤
#   │ /opt/simworld-studio        (this repo)  │ git clone            │
#   │ /opt/ue-engine              (58 GB)      │ Epic / S3 / HF       │
#   │ /opt/simworld-project       (Plugins +   │ HF / your dev box    │
#   │                              uproject +  │                      │
#   │                              Config +    │                      │
#   │                              Source)     │                      │
#   │ /opt/simworld-content       (~hundreds   │ HF (auto, this script)│
#   │                              of GB)      │                      │
#   └──────────────────────────────────────────┴──────────────────────┘
#
# Usage (the repo is private, so clone first with a deploy key, then run):
#   sudo git clone -b aws git@github.com:SimWorld-AI/SimWorld-Studio-Internal.git /opt/simworld-studio
#   sudo /opt/simworld-studio/deploy/aws/scripts/bootstrap.sh
#
# Re-run any time — every step is idempotent.

set -euo pipefail

if [[ "$EUID" -ne 0 ]]; then
    echo "ERROR: run as root (sudo)" >&2; exit 1
fi

REPO_URL="${REPO_URL:-git@github.com:SimWorld-AI/SimWorld-Studio-Internal.git}"
REPO_BRANCH="${REPO_BRANCH:-aws}"
REPO_DIR="${REPO_DIR:-/opt/simworld-studio}"

UE_ENGINE_DIR="${UE_ENGINE_DIR:-/opt/ue-engine}"
UE_PROJECT_DIR_SHARED="${UE_PROJECT_DIR_SHARED:-/opt/simworld-project}"
CONTENT_DIR="${CONTENT_DIR:-/opt/simworld-content}"

# Optional: HF repos for engine / project skeleton. Set to skip the manual rsync.
UE_ENGINE_HF_REPO="${UE_ENGINE_HF_REPO:-}"          # e.g. SimWorld-AI/UE-5.3.2-Linux
UE_PROJECT_HF_REPO="${UE_PROJECT_HF_REPO:-}"        # e.g. SimWorld-AI/SimWorld-Project-Skeleton

step() { printf '\n\033[1;36m==>\033[0m \033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

# ── 1. Clone (or update) the repo ─────────────────────────────────────────────
step "1/6  Repo at $REPO_DIR"
if [[ -d "$REPO_DIR/.git" ]]; then
    echo "    already cloned — fetching"
    git -C "$REPO_DIR" fetch --quiet
    git -C "$REPO_DIR" checkout --quiet "$REPO_BRANCH"
    git -C "$REPO_DIR" pull --quiet --ff-only
else
    apt-get update -qq && apt-get install -y -qq git
    git clone --quiet --branch "$REPO_BRANCH" "$REPO_URL" "$REPO_DIR"
fi

DEPLOY_DIR="$REPO_DIR/deploy/aws"
[[ -d "$DEPLOY_DIR" ]] || die "$DEPLOY_DIR not found — wrong branch?"

# ── 2. System packages + drivers via bake-ami ────────────────────────────────
step "2/6  Install OS deps + NVIDIA driver (bake-ami.sh)"
"$DEPLOY_DIR/scripts/bake-ami.sh"

NEED_REBOOT=0
if ! nvidia-smi >/dev/null 2>&1; then
    NEED_REBOOT=1
    warn "NVIDIA driver was just installed. You MUST reboot before continuing."
    warn "After reboot, re-run this bootstrap to resume."
fi

# ── 3. UE Engine ─────────────────────────────────────────────────────────────
step "3/6  UE 5.3.2 engine at $UE_ENGINE_DIR"
if [[ -x "$UE_ENGINE_DIR/Engine/Binaries/Linux/UnrealEditor" ]]; then
    echo "    already present"
elif [[ -n "$UE_ENGINE_HF_REPO" ]]; then
    echo "    downloading from HuggingFace: $UE_ENGINE_HF_REPO"
    install -d -o simworld -g simworld "$UE_ENGINE_DIR"
    sudo -u simworld huggingface-cli download "$UE_ENGINE_HF_REPO" \
        --repo-type dataset --local-dir "$UE_ENGINE_DIR" \
        --local-dir-use-symlinks False
else
    warn "UE engine missing and UE_ENGINE_HF_REPO not set."
    warn "Stage it manually then re-run:"
    warn "  rsync -av <src>/Linux_Unreal_Engine_5.3.2/ $UE_ENGINE_DIR/"
    warn "  chown -R simworld:simworld $UE_ENGINE_DIR"
    NEED_INTERVENTION=1
fi

# ── 4. UE Project skeleton (uproject + Plugins + Config + Source, NO Content) ─
step "4/6  Project skeleton at $UE_PROJECT_DIR_SHARED"
if [[ -f "$UE_PROJECT_DIR_SHARED/SimWorld.uproject" ]]; then
    echo "    already present"
elif [[ -n "$UE_PROJECT_HF_REPO" ]]; then
    echo "    downloading from HuggingFace: $UE_PROJECT_HF_REPO"
    install -d -o simworld -g simworld "$UE_PROJECT_DIR_SHARED"
    sudo -u simworld huggingface-cli download "$UE_PROJECT_HF_REPO" \
        --repo-type dataset --local-dir "$UE_PROJECT_DIR_SHARED" \
        --local-dir-use-symlinks False
else
    warn "Project skeleton missing and UE_PROJECT_HF_REPO not set."
    warn "Stage it manually then re-run:"
    warn "  rsync -av --exclude Content <src>/SimWorld/ $UE_PROJECT_DIR_SHARED/"
    warn "  chown -R simworld:simworld $UE_PROJECT_DIR_SHARED"
    NEED_INTERVENTION=1
fi

# Symlink Content from the shared HF download
if [[ -d "$UE_PROJECT_DIR_SHARED" && ! -e "$UE_PROJECT_DIR_SHARED/Content" ]]; then
    ln -s "$CONTENT_DIR" "$UE_PROJECT_DIR_SHARED/Content"
    echo "    symlinked Content → $CONTENT_DIR"
fi

# ── 5. Trigger Content download ──────────────────────────────────────────────
step "5/6  Content download (one-shot)"
if [[ -f "$CONTENT_DIR/.downloaded" ]]; then
    echo "    already downloaded"
else
    systemctl start simworld-content-init.service || true
    echo "    started simworld-content-init.service in background"
    echo "    watch with: journalctl -u simworld-content-init -f"
fi

# ── 6. Summary + next steps ──────────────────────────────────────────────────
step "6/6  Summary"
echo
if [[ "${NEED_REBOOT:-0}" == "1" ]]; then
    cat <<EOF
\033[1;33m⚠  REBOOT REQUIRED\033[0m
   NVIDIA driver was just installed. Run:
       sudo reboot
   Then re-run this bootstrap to resume.
EOF
    exit 0
fi

if [[ "${NEED_INTERVENTION:-0}" == "1" ]]; then
    cat <<EOF
\033[1;33m⚠  Manual staging needed\033[0m
   The UE engine or project skeleton couldn't be downloaded automatically.
   Stage them per the [warn] messages above, then re-run this script.
EOF
    exit 0
fi

cat <<EOF
\033[1;32m✓ Provisioning complete.\033[0m

Next:
  1. One-time Claude OAuth (interactive — needs a browser):
       sudo -u simworld HOME=/var/lib/simworld/claude-home claude

  2. Set up auth:
       sudo htpasswd -c /etc/nginx/htpasswd alice

  3. TLS for your domain:
       sudo certbot --nginx -d simworld.your-lab.edu

  4. Enable + start services:
       sudo systemctl enable --now coturn simworld-web nginx

  5. Verify slots come up:
       curl -u alice:pass http://localhost/api/session/status | jq

  6. (optional) Smoke-test the pool without real UE:
       node $REPO_DIR/deploy/aws/scripts/test-slot-pool.js
EOF
