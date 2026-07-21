# Public WebRTC host delta

Date: 2026-07-21 (Asia/Taipei)

Scope: read-only local-host preflight. No package was installed, no service was
started or stopped, no listener/firewall/DNS/TLS setting was changed, and no
public or provider request was made.

## Observations

The following commands were used from the pinned production worktree:

```bash
command -v turnserver coturn nginx openssl
ss -H -lntup
```

- `turnserver`, `coturn`, and `nginx` were not present on `PATH`.
- `/usr/bin/openssl` was present.
- TCP `80` and `443` were already listening on both IPv4 wildcard and IPv6
  wildcard addresses. The owning process was not visible to this unprivileged
  inspection.
- No listener was reported for TURN `3478` or TURN/TLS `5349`.

This supersedes only the listener observations in
`2026-07-21-host-preflight.md`; it does not change that document's artifact,
UE, database, or remote-host evidence.

## Decision

Public WebRTC remains deployment-gated. An administrator must identify and
approve integration with the current `80/443` ingress owner before installing
or enabling another proxy. The implementation must not bind a competing Nginx
listener. Coturn installation, public/private IP mapping, DNS, TLS, firewall
and relay-range changes, external forced-relay tests, and signed readiness
receipt remain unexecuted.
