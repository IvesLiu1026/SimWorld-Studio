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
die() { echo "ERROR: $*" >&2; exit 1; }

log "=== bake-ami.sh start ==="

# ── 1. Base packages ──────────────────────────────────────────────────────────
log "[1/8] apt update + base packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    curl wget git unzip rsync htop tmux jq procps \
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

if ! id simworld-build >/dev/null 2>&1; then
    log "[6/8b] creating isolated build user"
    useradd --system --create-home \
            --home-dir /var/cache/simworld-build \
            --shell /usr/sbin/nologin simworld-build
fi
[[ "$(id -u simworld-build)" != "$(id -u simworld)" ]] || {
    echo "ERROR: simworld-build must have a distinct UID from simworld" >&2
    exit 1
}
BUILD_PRIMARY_GID="$(id -g simworld-build)"
SIMWORLD_PRIMARY_GID="$(id -g simworld)"
[[ "$(id -u simworld-build)" != "0" \
   && "$BUILD_PRIMARY_GID" != "0" \
   && "$BUILD_PRIMARY_GID" != "$SIMWORLD_PRIMARY_GID" ]] || {
    echo "ERROR: simworld-build must use a dedicated non-root primary group" >&2
    exit 1
}
for build_group in $(id -G simworld-build); do
    [[ "$build_group" == "$BUILD_PRIMARY_GID" ]] || {
        echo "ERROR: simworld-build must not have supplementary groups" >&2
        exit 1
    }
done

install -d -o simworld -g simworld -m 755 \
    /var/lib/simworld \
    /var/lib/simworld/claude-home \
    /var/lib/simworld/slots \
    /opt/simworld-content
install -d -o root -g root -m 711 /var/lib/simworld-build
install -d -o simworld-build -g simworld-build -m 700 /var/cache/simworld-build

# Each slot dir created lazily by slot-launcher.sh on first start.

# Release code and deployment templates are later executed by root during
# config materialization. Keep them root-owned and immutable to the long-lived
# service account; only the explicit runtime log directory is writable.
[[ -d "$REPO_DIR/.git" && ! -L "$REPO_DIR" ]] || {
    echo "ERROR: REPO_DIR must be a non-symlink Git checkout" >&2
    exit 1
}
lock_release_tree() {
    local runtime_log_dir="$REPO_DIR/simworld_studio_workspace/logs"
    chown -hR root:root "$REPO_DIR"
    chmod -R go-w "$REPO_DIR"
    if [[ -L "$runtime_log_dir" ]]; then
        echo "ERROR: runtime log directory must not be a symlink" >&2
        return 1
    fi
    install -d -o simworld -g simworld -m 750 \
        "$runtime_log_dir"
    chown -hR simworld:simworld "$runtime_log_dir"
}
BUILD_UID="$(id -u simworld-build)"
kill_build_processes() {
    if pgrep -u "$BUILD_UID" >/dev/null 2>&1; then
        pkill -TERM -u "$BUILD_UID" >/dev/null 2>&1 || true
        for _ in {1..20}; do
            pgrep -u "$BUILD_UID" >/dev/null 2>&1 || return 0
            sleep 0.1
        done
        pkill -KILL -u "$BUILD_UID" >/dev/null 2>&1 || true
    fi
    ! pgrep -u "$BUILD_UID" >/dev/null 2>&1
}

validate_build_tree() {
    local tree="$1"
    local link_policy="$2"
    [[ -d "$tree" && ! -L "$tree" ]] || die "build output must be a real directory: $tree"
    [[ "$(find -P "$tree" -printf '.' | wc -c)" -le 500000 ]] \
        || die "build output exceeds the file-count bound: $tree"
    [[ "$(du -s --apparent-size --block-size=1 "$tree" | cut -f1)" -le 4294967296 ]] \
        || die "build output exceeds the byte bound: $tree"
    [[ -z "$(find -P "$tree" \( ! -type d -a ! -type f -a ! -type l \) -print -quit)" ]] \
        || die "build output contains a special file: $tree"
    [[ -z "$(find -P "$tree" -type f -links +1 -print -quit)" ]] \
        || die "build output contains a hard-linked file: $tree"
    [[ -z "$(find -P "$tree" ! -uid 0 -print -quit)" ]] \
        || die "sealed build output is not root-owned: $tree"
    if [[ "$link_policy" == "none" ]]; then
        [[ -z "$(find -P "$tree" -type l -print -quit)" ]] \
            || die "frontend build output must not contain symlinks"
    else
        while IFS= read -r -d '' link; do
            local target resolved
            target="$(readlink -- "$link")"
            [[ -n "$target" && "$target" != /* ]] \
                || die "node_modules link must be non-empty and relative: $link"
            resolved="$(realpath -e -- "$link")" \
                || die "node_modules link must resolve: $link"
            case "$resolved" in
                "$tree"/*) ;;
                *) die "node_modules link escapes its sealed root: $link" ;;
            esac
        done < <(find -P "$tree" -type l -print0)
    fi
}

BUILD_ROOT=""
DIST_PUBLISH_TEMP=""
DIST_NEW_FINAL=""
MODULES_PUBLISH_TEMP=""
MODULES_NEW_FINAL=""
finish_release_tree() {
    local cleanup_status=0
    kill_build_processes || cleanup_status=1
    if [[ -n "$BUILD_ROOT" && "$BUILD_ROOT" == /var/lib/simworld-build/release.* ]]; then
        chown -hR root:root "$BUILD_ROOT" >/dev/null 2>&1 || cleanup_status=1
        rm -rf -- "$BUILD_ROOT" || cleanup_status=1
    fi
    if [[ -n "$DIST_PUBLISH_TEMP" && "$DIST_PUBLISH_TEMP" == "$REPO_DIR"/simworld_studio_workspace/web/.dist.publish.* ]]; then
        rm -rf -- "$DIST_PUBLISH_TEMP" || cleanup_status=1
    fi
    if [[ -n "$DIST_NEW_FINAL" && "$DIST_NEW_FINAL" == "$REPO_DIR"/simworld_studio_workspace/web/dist ]]; then
        rm -rf -- "$DIST_NEW_FINAL" || cleanup_status=1
    fi
    if [[ -n "$MODULES_PUBLISH_TEMP" && "$MODULES_PUBLISH_TEMP" == "$REPO_DIR"/simworld_studio_workspace/web/server/.node_modules.publish.* ]]; then
        rm -rf -- "$MODULES_PUBLISH_TEMP" || cleanup_status=1
    fi
    if [[ -n "$MODULES_NEW_FINAL" && "$MODULES_NEW_FINAL" == "$REPO_DIR"/simworld_studio_workspace/web/server/node_modules ]]; then
        rm -rf -- "$MODULES_NEW_FINAL" || cleanup_status=1
    fi
    lock_release_tree || cleanup_status=1
    return "$cleanup_status"
}
lock_release_tree
trap finish_release_tree EXIT

# ── 7. Web frontend build (one-shot) ──────────────────────────────────────────
WEB_DIR="$REPO_DIR/simworld_studio_workspace/web"
SERVER_DIR="$WEB_DIR/server"
NEED_WEB_DIST=0
NEED_SERVER_MODULES=0
if [[ -e "$WEB_DIR/dist" || -L "$WEB_DIR/dist" ]]; then
    validate_build_tree "$WEB_DIR/dist" none
elif [[ -d "$WEB_DIR" ]]; then
    NEED_WEB_DIST=1
fi
if [[ -e "$SERVER_DIR/node_modules" || -L "$SERVER_DIR/node_modules" ]]; then
    validate_build_tree "$SERVER_DIR/node_modules" relative-in-root
elif [[ -d "$SERVER_DIR" ]]; then
    NEED_SERVER_MODULES=1
fi
if [[ "$NEED_WEB_DIST" == "1" || "$NEED_SERVER_MODULES" == "1" ]]; then
    BUILD_ROOT="$(mktemp -d /var/lib/simworld-build/release.XXXXXXXX)"
    chown simworld-build:simworld-build "$BUILD_ROOT"
    rsync -a \
        --exclude dist \
        --exclude node_modules \
        --exclude server/node_modules \
        "$WEB_DIR/" "$BUILD_ROOT/"
    chown -R simworld-build:simworld-build "$BUILD_ROOT"
fi
if [[ "$NEED_WEB_DIST" == "1" ]]; then
    log "[7/8] building web frontend"
    pushd "$BUILD_ROOT" >/dev/null
    sudo -u simworld-build env HOME=/var/cache/simworld-build npm ci
    sudo -u simworld-build env HOME=/var/cache/simworld-build npm run build
    popd >/dev/null
else
    log "[7/8] frontend dist/ already present or web dir missing — skipping"
fi

# Same for server
if [[ "$NEED_SERVER_MODULES" == "1" ]]; then
    log "[7/8b] installing server node_modules"
    pushd "$BUILD_ROOT/server" >/dev/null
    sudo -u simworld-build env HOME=/var/cache/simworld-build npm ci --omit=dev
    popd >/dev/null
fi

if [[ "$NEED_WEB_DIST" == "1" || "$NEED_SERVER_MODULES" == "1" ]]; then
    kill_build_processes || die "could not quiesce the isolated build UID"
    chown -hR root:root "$BUILD_ROOT"
    chmod -R u-s,g-s,go-w "$BUILD_ROOT"
fi
if [[ "$NEED_WEB_DIST" == "1" ]]; then
    validate_build_tree "$BUILD_ROOT/dist" none
    [[ ! -e "$WEB_DIR/dist" && ! -L "$WEB_DIR/dist" ]] \
        || die "frontend destination appeared during build"
    DIST_PUBLISH_TEMP="$(mktemp -d "$WEB_DIR/.dist.publish.XXXXXXXX")"
    rsync -a --safe-links --delete "$BUILD_ROOT/dist/" "$DIST_PUBLISH_TEMP/"
    validate_build_tree "$DIST_PUBLISH_TEMP" none
    DIST_NEW_FINAL="$WEB_DIR/dist"
    mv -- "$DIST_PUBLISH_TEMP" "$DIST_NEW_FINAL"
    DIST_PUBLISH_TEMP=""
    validate_build_tree "$WEB_DIR/dist" none
    DIST_NEW_FINAL=""
fi
if [[ "$NEED_SERVER_MODULES" == "1" ]]; then
    validate_build_tree "$BUILD_ROOT/server/node_modules" relative-in-root
    [[ ! -e "$SERVER_DIR/node_modules" && ! -L "$SERVER_DIR/node_modules" ]] \
        || die "server node_modules destination appeared during build"
    MODULES_PUBLISH_TEMP="$(mktemp -d "$SERVER_DIR/.node_modules.publish.XXXXXXXX")"
    rsync -a --safe-links --delete \
        "$BUILD_ROOT/server/node_modules/" "$MODULES_PUBLISH_TEMP/"
    validate_build_tree "$MODULES_PUBLISH_TEMP" relative-in-root
    MODULES_NEW_FINAL="$SERVER_DIR/node_modules"
    mv -- "$MODULES_PUBLISH_TEMP" "$MODULES_NEW_FINAL"
    MODULES_PUBLISH_TEMP=""
    validate_build_tree "$SERVER_DIR/node_modules" relative-in-root
    MODULES_NEW_FINAL=""
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

# The EXIT trap also covers failed npm/install paths. Repeat explicitly on the
# success path before releasing the trap so the service never starts from a
# service-user-writable release tree.
finish_release_tree
BUILD_ROOT=""
trap - EXIT

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
