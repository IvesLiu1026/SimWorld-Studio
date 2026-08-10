#!/usr/bin/env bash
set -Eeuo pipefail

readonly EXPECTED_COMMIT="43d60c36aadc892739d42051f64f87fe55a57b48"
readonly EXPECTED_TREE="3657b1223e0d98d3376b2b155fd862a58eadbe42"
readonly EXPECTED_LOCK_SHA256="1f2186650366cb23862c34f918196a2e21e50c01295ec43fc6a3693f2175e651"
readonly EXPECTED_SOURCE_MANIFEST_SHA256="eeaa3a5dcd4d695ca030960f7632b14e481a829af2b1ceaa299d89d3f333935b"
readonly DEFAULT_SOURCE_DIR="/mnt/NAS2/yhliu/SimWorldStudio/vista-blender-world/third_party/blender-mcp-v0.5.1"
readonly DEFAULT_BLENDER_BIN="/home/yhliu/.local/opt/blender-4.5.8-linux-x64/blender"

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
MANIFEST_TOOL="$SCRIPT_DIR/vista_blender_mcp_bootstrap.py"
SOURCE_DIR="$DEFAULT_SOURCE_DIR"
BLENDER_BIN="${BLENDER_BIN:-$DEFAULT_BLENDER_BIN}"
RUN_ROOT=""

usage() {
  printf 'Usage: %s --run-root PATH [--source-dir PATH] [--blender-bin PATH]\n' "$0"
}

while (($#)); do
  case "$1" in
    --run-root)
      RUN_ROOT=${2:?missing value for --run-root}
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

RUN_ROOT=$(realpath -m "$RUN_ROOT")
case "$RUN_ROOT" in
  /mnt/NAS2/yhliu/SimWorldStudio/vista-blender-world/runs/*) ;;
  *)
    printf 'Refusing non-append-only run root: %s\n' "$RUN_ROOT" >&2
    exit 2
    ;;
esac

for command_name in git tar uv bwrap sha256sum; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    printf '%s is required\n' "$command_name" >&2
    exit 1
  fi
done
if [[ ! -x "$BLENDER_BIN" ]]; then
  printf 'Blender is not executable: %s\n' "$BLENDER_BIN" >&2
  exit 1
fi
if [[ ! -f "$MANIFEST_TOOL" ]]; then
  printf 'Manifest tool is missing: %s\n' "$MANIFEST_TOOL" >&2
  exit 1
fi

umask 077
mkdir -p "$RUN_ROOT/mcp"

if [[ ! -d "$SOURCE_DIR/.git" ]]; then
  if [[ -e "$SOURCE_DIR" ]]; then
    printf 'Refusing to replace non-git source path: %s\n' "$SOURCE_DIR" >&2
    exit 1
  fi
  mkdir -p "$(dirname "$SOURCE_DIR")"
  git clone --branch v0.5.1 --depth 1 \
    https://github.com/zorak1103/blender-mcp.git "$SOURCE_DIR"
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

manifest_tool() {
  env -i \
    HOME="$RUN_ROOT/mcp/manifest-home" \
    XDG_CACHE_HOME="$RUN_ROOT/mcp/uv-cache" \
    PATH=/usr/bin:/bin:"$(dirname -- "$(command -v uv)")" \
    LC_ALL=C.UTF-8 \
    uv run --no-project python "$MANIFEST_TOOL" "$@"
}

readonly SOURCE_SNAPSHOT="$RUN_ROOT/mcp/vendor/blender-mcp-$EXPECTED_COMMIT"
readonly SOURCE_MANIFEST="$RUN_ROOT/mcp/vendor/blender-mcp-$EXPECTED_COMMIT.manifest.json"

prepare_source_snapshot() {
  local actual_manifest_hash staging staging_manifest
  mkdir -p "$RUN_ROOT/mcp/vendor"
  if [[ -e "$SOURCE_SNAPSHOT" || -e "$SOURCE_MANIFEST" ]]; then
    if [[ ! -d "$SOURCE_SNAPSHOT" || ! -f "$SOURCE_MANIFEST" ]]; then
      printf 'Incomplete locked source snapshot; use a fresh run root\n' >&2
      return 1
    fi
  else
    staging=$(mktemp -d "$RUN_ROOT/mcp/vendor/.source-stage.XXXXXX")
    staging_manifest="$staging.manifest.json"
    git -C "$SOURCE_DIR" archive --format=tar "$EXPECTED_COMMIT" | \
      tar -xf - -C "$staging"
    find "$staging" -type f -exec chmod 0444 {} +
    manifest_tool write-manifest "$staging" "$staging_manifest"
    actual_manifest_hash=$(sha256sum -- "$staging_manifest")
    actual_manifest_hash=${actual_manifest_hash%% *}
    if [[ "$actual_manifest_hash" != "$EXPECTED_SOURCE_MANIFEST_SHA256" ]]; then
      printf 'Archived source manifest mismatch: %s (expected %s)\n' \
        "$actual_manifest_hash" "$EXPECTED_SOURCE_MANIFEST_SHA256" >&2
      return 1
    fi
    find "$staging" -type d -exec chmod 0555 {} +
    chmod 0444 "$staging_manifest"
    mv -- "$staging" "$SOURCE_SNAPSHOT"
    mv -- "$staging_manifest" "$SOURCE_MANIFEST"
  fi

  actual_manifest_hash=$(sha256sum -- "$SOURCE_MANIFEST")
  actual_manifest_hash=${actual_manifest_hash%% *}
  if [[ "$actual_manifest_hash" != "$EXPECTED_SOURCE_MANIFEST_SHA256" ]]; then
    printf 'Locked source manifest has changed: %s\n' "$SOURCE_MANIFEST" >&2
    return 1
  fi
  manifest_tool verify-manifest "$SOURCE_SNAPSHOT" "$SOURCE_MANIFEST"
  if [[ "$(sha256sum -- "$SOURCE_SNAPSHOT/uv.lock")" != \
        "$EXPECTED_LOCK_SHA256  $SOURCE_SNAPSHOT/uv.lock" ]]; then
    printf 'Locked source uv.lock has changed\n' >&2
    return 1
  fi
}

prepare_source_snapshot

BLENDER_BIN=$(realpath "$BLENDER_BIN")
BLENDER_ROOT=$(dirname -- "$BLENDER_BIN")
case "$BLENDER_ROOT" in
  /|/home|/home/yhliu|/mnt|/mnt/NAS2)
    printf 'Refusing overly broad Blender root mount: %s\n' "$BLENDER_ROOT" >&2
    exit 1
    ;;
esac
blender_python="$BLENDER_ROOT/4.5/python/bin/python3.11"
if [[ ! -x "$blender_python" ]]; then
  printf 'Blender Python is not executable: %s\n' "$blender_python" >&2
  exit 1
fi
readonly UV_BIN=$(realpath "$(command -v uv)")

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

readonly TEST_WORK="$RUN_ROOT/mcp/setup-test"
mkdir -p "$TEST_WORK/home" "$TEST_WORK/cache" "$TEST_WORK/venv"
printf 'Running locked upstream unit suite in the credential-isolated sandbox\n'
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
  --dir /opt \
  --ro-bind "$UV_BIN" /opt/uv \
  --ro-bind "$SOURCE_SNAPSHOT" /source \
  --bind "$TEST_WORK" /work \
  --setenv HOME /work/home \
  --setenv XDG_CACHE_HOME /work/cache \
  --setenv UV_CACHE_DIR /work/cache/uv \
  --setenv UV_PROJECT_ENVIRONMENT /work/venv \
  --setenv PYTHONDONTWRITEBYTECODE 1 \
  --setenv PYTEST_DISABLE_PLUGIN_AUTOLOAD 1 \
  --setenv PATH /usr/bin:/bin:/opt \
  --setenv LC_ALL C.UTF-8 \
  --chdir /work \
  /opt/uv run --project /source --frozen --extra test \
    python -m pytest -q \
      -p no:cacheprovider \
      -p pytest_asyncio.plugin \
      /source/tests/unit

readonly SITE_DIR="$RUN_ROOT/mcp/site-packages-locked"
readonly SITE_MANIFEST="$RUN_ROOT/mcp/site-packages-locked.manifest.json"
readonly SITE_MANIFEST_HASH="$RUN_ROOT/mcp/site-packages-locked.manifest.sha256"
readonly INSTALL_WORK="$RUN_ROOT/mcp/setup-install"
mkdir -p "$INSTALL_WORK/home" "$INSTALL_WORK/cache"

verify_site_packages() {
  local actual_manifest_hash expected_manifest_hash
  if [[ ! -d "$SITE_DIR" || ! -f "$SITE_MANIFEST" || ! -f "$SITE_MANIFEST_HASH" ]]; then
    printf 'Incomplete locked site-packages; use a fresh run root\n' >&2
    return 1
  fi
  read -r expected_manifest_hash < "$SITE_MANIFEST_HASH"
  if [[ ! "$expected_manifest_hash" =~ ^[0-9a-f]{64}$ ]]; then
    printf 'Invalid site-packages manifest attestation\n' >&2
    return 1
  fi
  actual_manifest_hash=$(sha256sum -- "$SITE_MANIFEST")
  actual_manifest_hash=${actual_manifest_hash%% *}
  if [[ "$actual_manifest_hash" != "$expected_manifest_hash" ]]; then
    printf 'Site-packages manifest has changed\n' >&2
    return 1
  fi
  manifest_tool verify-manifest "$SITE_DIR" "$SITE_MANIFEST"
}

if [[ -e "$SITE_DIR" || -e "$SITE_MANIFEST" || -e "$SITE_MANIFEST_HASH" ]]; then
  verify_site_packages
  printf 'Verified existing locked dependency target: %s\n' "$SITE_DIR"
else
  requirements_file="$INSTALL_WORK/requirements.locked.txt"
  env -i \
    HOME="$INSTALL_WORK/home" \
    XDG_CACHE_HOME="$INSTALL_WORK/cache" \
    PATH=/usr/bin:/bin:"$(dirname -- "$UV_BIN")" \
    LC_ALL=C.UTF-8 \
    uv export \
      --project "$SOURCE_SNAPSHOT" \
      --frozen \
      --no-dev \
      --no-emit-project \
      --format requirements-txt \
      --output-file "$requirements_file" \
      >/dev/null

  site_staging=$(mktemp -d "$RUN_ROOT/mcp/.site-packages-stage.XXXXXX")
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
    --dir /opt \
    --ro-bind "$UV_BIN" /opt/uv \
    --ro-bind "$BLENDER_ROOT" /opt/blender \
    --ro-bind "$INSTALL_WORK" /install \
    --bind "$site_staging" /target \
    --setenv HOME /tmp/home \
    --setenv XDG_CACHE_HOME /tmp/cache \
    --setenv UV_CACHE_DIR /tmp/cache/uv \
    --setenv PATH /usr/bin:/bin:/opt \
    --setenv LC_ALL C.UTF-8 \
    --chdir /tmp \
    /opt/uv pip install \
      --python /opt/blender/4.5/python/bin/python3.11 \
      --target /target \
      --require-hashes \
      -r /install/requirements.locked.txt

  find "$site_staging" -type d -exec chmod 0555 {} +
  find "$site_staging" -type f -exec chmod a-w {} +
  site_manifest_staging="$site_staging.manifest.json"
  manifest_tool write-manifest "$site_staging" "$site_manifest_staging"
  site_manifest_sha=$(sha256sum -- "$site_manifest_staging")
  site_manifest_sha=${site_manifest_sha%% *}
  printf '%s\n' "$site_manifest_sha" > "$site_staging.manifest.sha256"
  chmod 0444 "$site_manifest_staging" "$site_staging.manifest.sha256"
  mv -- "$site_staging" "$SITE_DIR"
  mv -- "$site_manifest_staging" "$SITE_MANIFEST"
  mv -- "$site_staging.manifest.sha256" "$SITE_MANIFEST_HASH"
  verify_site_packages
fi

site_manifest_sha=$(sha256sum -- "$SITE_MANIFEST")
site_manifest_sha=${site_manifest_sha%% *}
readonly VERIFY_HOME="$RUN_ROOT/mcp/setup-import-home"
mkdir -p "$VERIFY_HOME"

printf 'Verifying Blender imports inside the credential-isolated sandbox\n'
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
  --dir /opt \
  --dir /home \
  --ro-bind "$BLENDER_ROOT" /opt/blender \
  --ro-bind "$SOURCE_SNAPSHOT" /opt/blender-mcp \
  --ro-bind "$SOURCE_MANIFEST" /opt/blender-mcp.manifest.json \
  --ro-bind "$SITE_DIR" /opt/vista-site \
  --ro-bind "$SITE_MANIFEST" /opt/vista-site.manifest.json \
  --ro-bind "$MANIFEST_TOOL" /opt/vista-bootstrap.py \
  --bind "$VERIFY_HOME" /home/vista \
  --setenv HOME /home/vista \
  --setenv PATH /usr/bin:/bin \
  --setenv LC_ALL C.UTF-8 \
  --setenv PYTHONDONTWRITEBYTECODE 1 \
  --setenv VISTA_BLENDER_MCP_BOOTSTRAP 1 \
  --setenv VISTA_BLENDER_MCP_VERIFY_ONLY 1 \
  --setenv VISTA_BLENDER_MCP_SOURCE /opt/blender-mcp \
  --setenv VISTA_BLENDER_MCP_SOURCE_MANIFEST /opt/blender-mcp.manifest.json \
  --setenv VISTA_BLENDER_MCP_SITE /opt/vista-site \
  --setenv VISTA_BLENDER_MCP_SITE_MANIFEST /opt/vista-site.manifest.json \
  --setenv VISTA_BLENDER_MCP_SITE_MANIFEST_SHA256 "$site_manifest_sha" \
  /opt/blender/blender \
    --background \
    --factory-startup \
    --disable-autoexec \
    -noaudio \
    --python-exit-code 1 \
    --python /opt/vista-bootstrap.py

printf 'MCP setup ready: source=%s site=%s\n' "$SOURCE_SNAPSHOT" "$SITE_DIR"
