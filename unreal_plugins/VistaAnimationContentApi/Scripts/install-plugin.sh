#!/bin/sh
set -eu

usage() {
  echo "usage: $0 --project-root ABSOLUTE_UE_PROJECT_ROOT [--apply]" >&2
  exit 2
}

project_root=
apply=false
while [ "$#" -gt 0 ]; do
  case "$1" in
    --project-root)
      [ "$#" -ge 2 ] || usage
      project_root=$2
      shift 2
      ;;
    --apply)
      apply=true
      shift
      ;;
    *) usage ;;
  esac
done

[ -n "$project_root" ] || usage
case "$project_root" in /*) ;; *) echo "project root must be absolute" >&2; exit 2 ;; esac
[ -d "$project_root" ] || { echo "project root does not exist" >&2; exit 1; }
[ ! -L "$project_root" ] || { echo "project root must not be a symlink" >&2; exit 1; }

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
plugin_root=$(CDPATH= cd -- "$script_dir/.." && pwd -P)
if find "$plugin_root" -type l -print -quit | grep -q .; then
  echo "plugin source must not contain symlinks" >&2
  exit 1
fi

project_count=$(find "$project_root" -maxdepth 1 -type f -name '*.uproject' | wc -l | tr -d ' ')
[ "$project_count" = 1 ] || { echo "project root must contain exactly one regular .uproject file" >&2; exit 1; }

destination="$project_root/Plugins/VistaAnimationContentApi"
[ ! -e "$destination" ] && [ ! -L "$destination" ] || {
  echo "destination already exists; move it aside explicitly before installing" >&2
  exit 1
}

echo "source: $plugin_root"
echo "destination: $destination"
if [ "$apply" != true ]; then
  echo "dry run only; add --apply to install"
  exit 0
fi

mkdir -p "$project_root/Plugins"
staging=$(mktemp -d "$project_root/Plugins/.VistaAnimationContentApi.install.XXXXXX")
cleanup() { rm -rf -- "$staging"; }
trap cleanup EXIT HUP INT TERM
cp -R "$plugin_root/." "$staging/"
mv -- "$staging" "$destination"
trap - EXIT HUP INT TERM
echo "installed source plugin; no compile or live verification was performed"
