#!/usr/bin/env bash
# bake-ami.sh — Provision a fresh Ubuntu 22.04 EC2 instance into a SimWorld
# Studio worker. Run as root once; reboot for NVIDIA driver; then ready.
#
# Designed to be run BEFORE Content download (which lives in its own oneshot)
# and BEFORE UE engine + project are placed on disk (those are operator steps).
#
# Idempotent — safe to re-run; each step skips if already done.

set -euo pipefail

if [[ "$EUID" -ne 0 ]]; then
    echo "ERROR: run as root (sudo)" >&2; exit 1
fi

REPO_DIR="${REPO_DIR:-/opt/simworld-studio}"
DEPLOY_DIR="$REPO_DIR/deploy/aws"
LOG="/var/log/simworld-bake.log"

log() { echo "[$(date -Is)] $*" | tee -a "$LOG"; }

log "=== bake-ami.sh start ==="

# ── 1. Base packages ──────────────────────────────────────────────────────────
log "[1/8] apt update + base packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    curl wget git unzip rsync htop tmux jq \
    build-essential python3 python3-pip python3-venv \
    nginx coturn certbot python3-certbot-nginx \
    netcat-openbsd vulkan-tools mesa-vulkan-drivers \
    libsdl2-2.0-0 libsdl2-image-2.0-0 \
    apache2-utils

# ── 2. Node.js 18 ─────────────────────────────────────────────────────────────
if ! command -v node >/dev/null || ! node --version | grep -qE '^v(1[89]|2[0-9])\.'; then
    log "[2/8] installing Node.js 18"
    curl -fsSL https://deb.nodesource.com/setup_18.x | bash -
    apt-get install -y -qq nodejs
else
    log "[2/8] Node.js already present: $(node --version)"
fi

# ── 3. NVIDIA driver (skip if already present) ────────────────────────────────
if nvidia-smi >/dev/null 2>&1; then
    log "[3/8] NVIDIA driver already installed: $(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1)"
else
    log "[3/8] installing NVIDIA driver 535 (REBOOT REQUIRED after this script)"
    apt-get install -y -qq linux-headers-$(uname -r) nvidia-driver-535
fi

# ── 4. HuggingFace CLI for Content download ───────────────────────────────────
if ! command -v huggingface-cli >/dev/null; then
    log "[4/8] installing huggingface_hub CLI"
    pip3 install -q huggingface_hub[cli]
else
    log "[4/8] huggingface-cli already present"
fi

# ── 5. Claude Code CLI ────────────────────────────────────────────────────────
if ! command -v claude >/dev/null; then
    log "[5/8] installing Claude Code"
    npm install -g @anthropic-ai/claude-code
else
    log "[5/8] Claude Code already present: $(claude --version 2>&1 | head -1)"
fi

# ── 6. Create simworld user + directories ────────────────────────────────────
if ! id simworld >/dev/null 2>&1; then
    log "[6/8] creating simworld system user"
    useradd --system --create-home \
            --home-dir /var/lib/simworld/claude-home \
            --shell /bin/bash simworld
else
    log "[6/8] simworld user already exists"
fi

install -d -o simworld -g simworld -m 755 \
    /var/lib/simworld \
    /var/lib/simworld/claude-home \
    /var/lib/simworld/slots \
    /opt/simworld-content

# Each slot dir created lazily by slot-launcher.sh on first start.

# Web server logs dir (must be writable by simworld user)
install -d -o simworld -g simworld -m 755 \
    "$REPO_DIR/simworld_studio_workspace/logs"

# Give simworld user read access to the repo
chown -R simworld:simworld "$REPO_DIR"

# ── 7. Web frontend build (one-shot) ──────────────────────────────────────────
WEB_DIR="$REPO_DIR/simworld_studio_workspace/web"
if [[ -d "$WEB_DIR" && ! -d "$WEB_DIR/dist" ]]; then
    log "[7/8] building web frontend"
    pushd "$WEB_DIR" >/dev/null
    sudo -u simworld npm ci
    sudo -u simworld npm run build
    popd >/dev/null
else
    log "[7/8] frontend dist/ already present or web dir missing — skipping"
fi

# Same for server
SERVER_DIR="$REPO_DIR/simworld_studio_workspace/web/server"
if [[ -d "$SERVER_DIR" && ! -d "$SERVER_DIR/node_modules" ]]; then
    log "[7/8b] installing server node_modules"
    pushd "$SERVER_DIR" >/dev/null
    sudo -u simworld npm ci --omit=dev
    popd >/dev/null
fi

# ── 8. Install systemd units + nginx config ──────────────────────────────────
log "[8/8] installing systemd units"
install -m 644 "$DEPLOY_DIR/systemd/simworld-web.service"          /etc/systemd/system/
install -m 644 "$DEPLOY_DIR/systemd/simworld-content-init.service" /etc/systemd/system/
mkdir -p /etc/systemd/system/coturn.service.d
install -m 644 "$DEPLOY_DIR/systemd/coturn.service.d-override.conf" \
        /etc/systemd/system/coturn.service.d/override.conf

# nginx — install but don't enable until cert is in place
if [[ ! -f /etc/nginx/sites-available/simworld ]]; then
    install -m 644 "$DEPLOY_DIR/templates/nginx.conf" /etc/nginx/sites-available/simworld
    log "    nginx config written to /etc/nginx/sites-available/simworld"
    log "    EDIT server_name + run certbot, then: ln -s ../sites-available/simworld /etc/nginx/sites-enabled/"
fi

if [[ ! -f /etc/turnserver.conf ]]; then
    install -m 640 -o root -g turnserver "$DEPLOY_DIR/templates/coturn.conf" /etc/turnserver.conf 2>/dev/null \
        || install -m 644 "$DEPLOY_DIR/templates/coturn.conf" /etc/turnserver.conf
    log "    coturn config written to /etc/turnserver.conf — EDIT before enabling"
fi

systemctl daemon-reload

# ── Done ──────────────────────────────────────────────────────────────────────
log "=== bake done ==="
cat <<EOF

NEXT STEPS
==========
1. If NVIDIA driver was just installed: sudo reboot
2. Place UE 5.3.2 engine at /opt/ue-engine/
   rsync -a /source/Linux_Unreal_Engine_5.3.2/ /opt/ue-engine/
3. Place the SimWorld project at /opt/simworld-project/ (uproject + Plugins + Config, NOT Content)
   ln -s /opt/simworld-content /opt/simworld-project/Content
4. Download Content from HuggingFace:
   sudo systemctl start simworld-content-init
5. One-time Claude OAuth:
   sudo -u simworld HOME=/var/lib/simworld/claude-home claude
6. Edit /etc/nginx/sites-available/simworld (set server_name) and /etc/turnserver.conf,
   then: sudo certbot --nginx -d <your-domain>
7. Auth file: sudo htpasswd -c /etc/nginx/htpasswd <user>
8. Enable services:
   sudo systemctl enable --now coturn simworld-web nginx
EOF
