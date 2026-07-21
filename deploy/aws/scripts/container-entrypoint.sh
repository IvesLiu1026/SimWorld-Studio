#!/usr/bin/env sh
set -eu

fail() {
    printf '%s\n' "simworld-container-entrypoint: $*" >&2
    exit 78
}

positive_id() {
    name="$1"
    value="${2:-}"
    case "$value" in
        ''|0|*[!0-9]*) fail "$name must be a positive non-root numeric ID" ;;
    esac
    [ "$value" -le 4294967295 ] 2>/dev/null \
        || fail "$name is outside the supported numeric ID range"
}

runtime_uid="${SIMWORLD_RUNTIME_UID:-}"
runtime_gid="${SIMWORLD_RUNTIME_GID:-}"
secret_gid="${SIMWORLD_SECRET_GID:-}"
positive_id SIMWORLD_RUNTIME_UID "$runtime_uid"
positive_id SIMWORLD_RUNTIME_GID "$runtime_gid"
positive_id SIMWORLD_SECRET_GID "$secret_gid"

[ "$(id -u)" = "$runtime_uid" ] \
    || fail "effective UID does not match SIMWORLD_RUNTIME_UID"
[ "$(id -g)" = "$runtime_gid" ] \
    || fail "effective GID does not match SIMWORLD_RUNTIME_GID"
case " $(id -G) " in
    *" $secret_gid "*) ;;
    *) fail "supplementary groups do not contain SIMWORLD_SECRET_GID" ;;
esac

if [ "${1:-}" = "--validate-only" ]; then
    exit 0
fi
[ "$#" -gt 0 ] || fail "no container command was supplied"
exec /usr/bin/tini -- "$@"
