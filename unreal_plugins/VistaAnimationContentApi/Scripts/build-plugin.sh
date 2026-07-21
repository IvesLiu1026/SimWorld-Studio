#!/bin/sh
set -eu

usage() {
  echo "usage: $0 --engine-root ABSOLUTE_UE_ROOT --output ABSOLUTE_EMPTY_DIR --platform Linux [--apply]" >&2
  exit 2
}

engine_root=
output=
platform=
apply=false
while [ "$#" -gt 0 ]; do
  case "$1" in
    --engine-root) [ "$#" -ge 2 ] || usage; engine_root=$2; shift 2 ;;
    --output) [ "$#" -ge 2 ] || usage; output=$2; shift 2 ;;
    --platform) [ "$#" -ge 2 ] || usage; platform=$2; shift 2 ;;
    --apply) apply=true; shift ;;
    *) usage ;;
  esac
done

[ -n "$engine_root" ] && [ -n "$output" ] && [ -n "$platform" ] || usage
case "$engine_root:$output" in /*:/*) ;; *) echo "engine root and output must be absolute" >&2; exit 2 ;; esac
case "$platform" in Linux|Win64|Mac) ;; *) echo "platform must be Linux, Win64, or Mac" >&2; exit 2 ;; esac
: "${VISTA_ANIMATION_PLUGIN_BUILD_ID:?set VISTA_ANIMATION_PLUGIN_BUILD_ID to a reviewed opaque build id}"
case "$VISTA_ANIMATION_PLUGIN_BUILD_ID" in *[!A-Za-z0-9._:@-]*|'') echo "invalid build id" >&2; exit 2 ;; esac
case "$VISTA_ANIMATION_PLUGIN_BUILD_ID" in [A-Za-z0-9]*) ;; *) echo "build id must start with an ASCII letter or digit" >&2; exit 2 ;; esac
[ "${#VISTA_ANIMATION_PLUGIN_BUILD_ID}" -le 160 ] || { echo "build id is too long" >&2; exit 2; }

run_uat="$engine_root/Engine/Build/BatchFiles/RunUAT.sh"
[ -x "$run_uat" ] || { echo "RunUAT.sh is missing or not executable" >&2; exit 1; }
[ ! -e "$output" ] || { [ -d "$output" ] && [ -z "$(find "$output" -mindepth 1 -maxdepth 1 -print -quit)" ]; } || {
  echo "output must not exist or must be empty" >&2
  exit 1
}

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
plugin="$script_dir/../VistaAnimationContentApi.uplugin"
echo "$run_uat BuildPlugin -Plugin=$plugin -Package=$output -TargetPlatforms=$platform -Rocket"
if [ "$apply" != true ]; then
  echo "dry run only; add --apply to build"
  exit 0
fi

mkdir -p "$output"
exec "$run_uat" BuildPlugin \
  "-Plugin=$plugin" \
  "-Package=$output" \
  "-TargetPlatforms=$platform" \
  -Rocket
