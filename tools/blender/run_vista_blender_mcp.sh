#!/usr/bin/env bash
set -Eeuo pipefail

readonly EXPECTED_COMMIT="43d60c36aadc892739d42051f64f87fe55a57b48"
readonly EXPECTED_TREE="3657b1223e0d98d3376b2b155fd862a58eadbe42"
readonly EXPECTED_SOURCE_MANIFEST_SHA256="eeaa3a5dcd4d695ca030960f7632b14e481a829af2b1ceaa299d89d3f333935b"
readonly DEFAULT_SOURCE_DIR="/mnt/NAS2/yhliu/SimWorldStudio/vista-blender-world/third_party/blender-mcp-v0.5.1"
readonly DEFAULT_BLENDER_BIN="/home/yhliu/.local/opt/blender-4.5.8-linux-x64/blender"

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
MANIFEST_TOOL="$SCRIPT_DIR/vista_blender_mcp_bootstrap.py"
SOURCE_DIR="$DEFAULT_SOURCE_DIR"
BLENDER_BIN="${BLENDER_BIN:-$DEFAULT_BLENDER_BIN}"
RUN_ROOT=""
BLEND_FILE=""
PORT=8400
DISPLAY_NUMBER=117
XVFB_PID=""
MCP_PID=""

usage() {
  printf '%s\n' \
    "Usage: $0 --run-root PATH [--blend-file PATH] [--source-dir PATH]" \
    "          [--blender-bin PATH] [--port PORT] [--display NUMBER]"
}

while (($#)); do
  case "$1" in
    --run-root)
      RUN_ROOT=${2:?missing value for --run-root}
      shift 2
      ;;
    --blend-file)
      BLEND_FILE=${2:?missing value for --blend-file}
      shift 2
      ;;
    --source-dir)
      SOURCE_DIR=${2:?missing value for --source-dir}
      shift 2
      ;;
    --blender-bin)
      BLENDER_BIN=${2:?missing value for --blender-bin}
      shift 2
      ;;
    --port)
      PORT=${2:?missing value for --port}
      shift 2
      ;;
    --display)
      DISPLAY_NUMBER=${2:?missing value for --display}
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'Unknown argument: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$RUN_ROOT" ]]; then
  usage >&2
  exit 2
fi
if [[ ! "$PORT" =~ ^[0-9]+$ ]] || ((PORT < 1024 || PORT > 65535)); then
  printf 'Invalid port: %s\n' "$PORT" >&2
  exit 2
fi
if [[ ! "$DISPLAY_NUMBER" =~ ^[0-9]+$ ]]; then
  printf 'Invalid X display number: %s\n' "$DISPLAY_NUMBER" >&2
  exit 2
fi

RUN_ROOT=$(realpath -m "$RUN_ROOT")
case "$RUN_ROOT" in
  /mnt/NAS2/yhliu/SimWorldStudio/vista-blender-world/runs/*) ;;
  *)
    printf 'Refusing non-append-only run root: %s\n' "$RUN_ROOT" >&2
    exit 2
    ;;
esac

internal_blend_file=""
if [[ -n "$BLEND_FILE" ]]; then
  BLEND_FILE=$(realpath "$BLEND_FILE")
  case "$BLEND_FILE" in
    "$RUN_ROOT"/*)
      internal_blend_file="/work/${BLEND_FILE#"$RUN_ROOT"/}"
      ;;
    *)
      printf 'Blend file must be inside the current run root: %s\n' "$BLEND_FILE" >&2
      exit 2
      ;;
  esac
fi

for command_name in git uv bwrap Xvfb ss sha256sum; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    printf '%s is required\n' "$command_name" >&2
    exit 1
  fi
done
if [[ ! -x "$BLENDER_BIN" ]]; then
  printf 'Blender is not executable: %s\n' "$BLENDER_BIN" >&2
  exit 1
fi

verify_checkout() {
  local actual_commit actual_tree untracked
  actual_commit=$(git -C "$SOURCE_DIR" rev-parse --verify HEAD 2>/dev/null) || {
    printf 'Pinned blender-mcp checkout is unreadable: %s\n' "$SOURCE_DIR" >&2
    return 1
  }
  actual_tree=$(git -C "$SOURCE_DIR" rev-parse --verify 'HEAD^{tree}')
  if [[ "$actual_commit" != "$EXPECTED_COMMIT" || "$actual_tree" != "$EXPECTED_TREE" ]]; then
    printf 'Unexpected blender-mcp identity: commit=%s tree=%s\n' \
      "$actual_commit" "$actual_tree" >&2
    return 1
  fi
  if ! git -C "$SOURCE_DIR" diff --quiet "$EXPECTED_COMMIT" -- || \
     ! git -C "$SOURCE_DIR" diff --cached --quiet "$EXPECTED_COMMIT" --; then
    printf 'Refusing dirty tracked blender-mcp checkout: %s\n' "$SOURCE_DIR" >&2
    return 1
  fi
  untracked=$(git -C "$SOURCE_DIR" ls-files --others --exclude-standard)
  if [[ -n "$untracked" ]]; then
    printf 'Refusing blender-mcp checkout with untracked source files:\n%s\n' \
      "$untracked" >&2
    return 1
  fi
}

verify_checkout

readonly SOURCE_SNAPSHOT="$RUN_ROOT/mcp/vendor/blender-mcp-$EXPECTED_COMMIT"
readonly SOURCE_MANIFEST="$RUN_ROOT/mcp/vendor/blender-mcp-$EXPECTED_COMMIT.manifest.json"
readonly SITE_DIR="$RUN_ROOT/mcp/site-packages-locked"
readonly SITE_MANIFEST="$RUN_ROOT/mcp/site-packages-locked.manifest.json"
readonly SITE_MANIFEST_HASH="$RUN_ROOT/mcp/site-packages-locked.manifest.sha256"

for required_path in \
  "$SOURCE_SNAPSHOT" \
  "$SOURCE_MANIFEST" \
  "$SITE_DIR" \
  "$SITE_MANIFEST" \
  "$SITE_MANIFEST_HASH"; do
  if [[ ! -e "$required_path" ]]; then
    printf 'Locked MCP input is missing; run setup_vista_blender_mcp.sh first: %s\n' \
      "$required_path" >&2
    exit 1
  fi
done

manifest_tool() {
  env -i \
    HOME="$RUN_ROOT/mcp/manifest-home" \
    XDG_CACHE_HOME="$RUN_ROOT/mcp/uv-cache" \
    PATH=/usr/bin:/bin:"$(dirname -- "$(command -v uv)")" \
    LC_ALL=C.UTF-8 \
    uv run --no-project python "$MANIFEST_TOOL" "$@"
}

source_manifest_sha=$(sha256sum -- "$SOURCE_MANIFEST")
source_manifest_sha=${source_manifest_sha%% *}
if [[ "$source_manifest_sha" != "$EXPECTED_SOURCE_MANIFEST_SHA256" ]]; then
  printf 'Locked source manifest has changed\n' >&2
  exit 1
fi
manifest_tool verify-manifest "$SOURCE_SNAPSHOT" "$SOURCE_MANIFEST"

read -r expected_site_manifest_sha < "$SITE_MANIFEST_HASH"
if [[ ! "$expected_site_manifest_sha" =~ ^[0-9a-f]{64}$ ]]; then
  printf 'Invalid site-packages manifest attestation\n' >&2
  exit 1
fi
site_manifest_sha=$(sha256sum -- "$SITE_MANIFEST")
site_manifest_sha=${site_manifest_sha%% *}
if [[ "$site_manifest_sha" != "$expected_site_manifest_sha" ]]; then
  printf 'Site-packages manifest has changed\n' >&2
  exit 1
fi
manifest_tool verify-manifest "$SITE_DIR" "$SITE_MANIFEST"

if ss -ltnH "sport = :$PORT" | grep -q .; then
  printf 'Refusing occupied MCP port: %s\n' "$PORT" >&2
  exit 1
fi

BLENDER_BIN=$(realpath "$BLENDER_BIN")
BLENDER_ROOT=$(dirname -- "$BLENDER_BIN")
case "$BLENDER_ROOT" in
  /|/home|/home/yhliu|/mnt|/mnt/NAS2)
    printf 'Refusing overly broad Blender root mount: %s\n' "$BLENDER_ROOT" >&2
    exit 1
    ;;
esac

home_dir="$RUN_ROOT/mcp-home"
log_dir="$RUN_ROOT/mcp/logs"
mkdir -p "$home_dir" "$log_dir"
chmod 700 "$home_dir" "$log_dir"

cleanup() {
  local exit_code=$?
  trap - EXIT INT TERM HUP
  if [[ -n "$MCP_PID" ]]; then
    kill -TERM "$MCP_PID" 2>/dev/null || true
    for _ in $(seq 1 30); do
      kill -0 "$MCP_PID" 2>/dev/null || break
      sleep 0.1
    done
    if kill -0 "$MCP_PID" 2>/dev/null; then
      kill -KILL "$MCP_PID" 2>/dev/null || true
    fi
    wait "$MCP_PID" 2>/dev/null || true
  fi
  if [[ -n "$XVFB_PID" ]]; then
    kill -TERM "$XVFB_PID" 2>/dev/null || true
  fi
  exit "$exit_code"
}
trap cleanup EXIT INT TERM HUP

display_socket="/tmp/.X11-unix/X$DISPLAY_NUMBER"
if [[ -e "$display_socket" ]]; then
  printf 'Refusing occupied X display: :%s\n' "$DISPLAY_NUMBER" >&2
  exit 1
fi

env -i \
  HOME="$home_dir" \
  PATH=/usr/bin:/bin \
  LC_ALL=C.UTF-8 \
  Xvfb ":$DISPLAY_NUMBER" -screen 0 1280x720x24 -nolisten tcp -noreset \
  >>"$log_dir/xvfb.log" 2>&1 &
XVFB_PID=$!
for _ in $(seq 1 50); do
  [[ -S "$display_socket" ]] && break
  kill -0 "$XVFB_PID" 2>/dev/null || {
    printf 'Xvfb exited before creating %s\n' "$display_socket" >&2
    exit 1
  }
  sleep 0.1
done
if [[ ! -S "$display_socket" ]]; then
  printf 'Xvfb did not create %s\n' "$display_socket" >&2
  exit 1
fi

blender_args=(--factory-startup --disable-autoexec -noaudio)
if [[ -n "$internal_blend_file" ]]; then
  blender_args+=("$internal_blend_file")
fi
blender_args+=(--python /opt/vista/vista_blender_mcp_bootstrap.py)

safe_system_mounts=(
  --ro-bind /usr /usr
  --ro-bind /bin /bin
  --ro-bind /lib /lib
)
if [[ -e /lib64 ]]; then
  safe_system_mounts+=(--ro-bind /lib64 /lib64)
fi
safe_system_mounts+=(--dir /etc)
for safe_path in \
  /etc/fonts \
  /etc/ssl/certs \
  /etc/ssl/openssl.cnf \
  /etc/resolv.conf \
  /etc/hosts \
  /etc/nsswitch.conf \
  /etc/gai.conf \
  /etc/ld.so.cache \
  /etc/machine-id; do
  if [[ -e "$safe_path" ]]; then
    safe_system_mounts+=(--ro-bind "$safe_path" "$safe_path")
  fi
done

printf 'Starting authenticated Blender MCP on 127.0.0.1:%s\n' "$PORT"
bwrap \
  --die-with-parent \
  --new-session \
  --unshare-user-try \
  --unshare-pid \
  --unshare-ipc \
  --unshare-uts \
  --unshare-cgroup-try \
  --clearenv \
  "${safe_system_mounts[@]}" \
  --dev /dev \
  --proc /proc \
  --tmpfs /tmp \
  --dir /tmp/.X11-unix \
  --dir /tmp/vista-runtime \
  --ro-bind "$display_socket" "/tmp/.X11-unix/X$DISPLAY_NUMBER" \
  --dir /opt \
  --dir /opt/vista \
  --dir /home \
  --ro-bind "$BLENDER_ROOT" /opt/blender \
  --ro-bind "$SOURCE_SNAPSHOT" /opt/blender-mcp \
  --ro-bind "$SOURCE_MANIFEST" /opt/blender-mcp.manifest.json \
  --ro-bind "$SITE_DIR" /opt/vista-site \
  --ro-bind "$SITE_MANIFEST" /opt/vista-site.manifest.json \
  --ro-bind "$MANIFEST_TOOL" /opt/vista/vista_blender_mcp_bootstrap.py \
  --bind "$RUN_ROOT" /work \
  --ro-bind "$SOURCE_SNAPSHOT" "/work/mcp/vendor/blender-mcp-$EXPECTED_COMMIT" \
  --ro-bind "$SOURCE_MANIFEST" "/work/mcp/vendor/blender-mcp-$EXPECTED_COMMIT.manifest.json" \
  --ro-bind "$SITE_DIR" /work/mcp/site-packages-locked \
  --ro-bind "$SITE_MANIFEST" /work/mcp/site-packages-locked.manifest.json \
  --ro-bind "$SITE_MANIFEST_HASH" /work/mcp/site-packages-locked.manifest.sha256 \
  --bind "$home_dir" /home/vista \
  --setenv HOME /home/vista \
  --setenv DISPLAY ":$DISPLAY_NUMBER" \
  --setenv XDG_RUNTIME_DIR /tmp/vista-runtime \
  --setenv PATH /usr/bin:/bin \
  --setenv LC_ALL C.UTF-8 \
  --setenv PYTHONDONTWRITEBYTECODE 1 \
  --setenv VISTA_BLENDER_MCP_BOOTSTRAP 1 \
  --setenv VISTA_BLENDER_MCP_SOURCE /opt/blender-mcp \
  --setenv VISTA_BLENDER_MCP_SOURCE_MANIFEST /opt/blender-mcp.manifest.json \
  --setenv VISTA_BLENDER_MCP_SITE /opt/vista-site \
  --setenv VISTA_BLENDER_MCP_SITE_MANIFEST /opt/vista-site.manifest.json \
  --setenv VISTA_BLENDER_MCP_SITE_MANIFEST_SHA256 "$site_manifest_sha" \
  --setenv VISTA_BLENDER_MCP_PORT "$PORT" \
  --chdir /work \
  /opt/blender/blender "${blender_args[@]}" \
  > >(tee -a "$log_dir/blender-mcp.log") 2>&1 &
MCP_PID=$!
wait "$MCP_PID"
