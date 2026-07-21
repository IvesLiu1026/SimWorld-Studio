# Public Pixel Streaming / Coturn Runbook

Status: code-complete, deployment-gated. Do not mark public WebRTC ready until
the external forced-relay matrix at the end has a signed receipt.

## 1. Required topology

- `studio.<domain>` resolves to the HTTPS/WSS ingress.
- `turn.<domain>` resolves to the Coturn public address.
- Nginx owns public TCP `80/443` and proxies to Node on loopback `:3002`.
- Coturn owns public UDP/TCP `3478`, TCP/TLS `5349`, and UDP relay
  `49160-49200`.
- Node, Cirrus `HttpPort`, UE `StreamerPort`, SFU, MCP, and UnrealCV all remain
  loopback-only. Public firewall rules must reject `3002`, `85xx`, `89xx`,
  `55xxx`, and `9017+`.
- If TURN-over-TLS must use TCP `443`, provision a separate public IP or load
  balancer. Coturn and Nginx cannot both bind the same IP/port.

## 2. Administrator prerequisites

Install `nginx`, `coturn`, `certbot`, and the Nginx Certbot integration, then
issue certificates for both public DNS names. Create three independent random
secret files; never place their values in Git, command-line arguments, logs, or
shell history.

```bash
sudo install -d -m 0750 -o root -g simworld /etc/simworld/secrets
sudo sh -c 'umask 0077; openssl rand -hex 32 > /etc/simworld/secrets/studio_access_token'
sudo sh -c 'umask 0077; openssl rand -hex 32 > /etc/simworld/secrets/pixel_streaming_hmac'
sudo sh -c 'umask 0077; openssl rand -hex 32 > /etc/simworld/secrets/turn_shared_secret'
sudo chown root:simworld /etc/simworld/secrets/*
sudo chmod 0640 /etc/simworld/secrets/*
```

Set the public origin, TURN DNS/IP, exact Git revision, and secret-file paths in
`/etc/default/simworld`, following `deploy/aws/templates/simworld.env`.

This deployment tooling is Linux-only and requires `O_NOFOLLOW` and
`O_DIRECTORY` semantics. Record the exact numeric `simworld` secret-group GID
as `TURN_SHARED_SECRET_FILE_GID` whenever the TURN secret is `0640`. A
group-readable secret is rejected unless its GID exactly matches that value;
owner-only `0400/0600` secrets omit it. Container deployments must also add
that same host numeric GID through Compose `group_add`. Compose also requires
the exact host `simworld` runtime UID:GID for writable state mounts; image-local
names or coincidental IDs are not accepted as evidence.

## 3. Materialize and validate Coturn

The reviewed template uses Coturn REST auth. The same shared-secret file is read
by the per-slot Cirrus config builder to produce short-lived HMAC credentials.
Both materializers fail closed unless every secret path is absolute and
normalized, has no symlink component, and resolves to a single-link regular
file owned by root or the invoking service user. Secret files may be `0600` or
root/service-group-readable `0640`; group write and every world permission are
rejected. Secret content is length-bounded and restricted to base64/hex-safe
bytes, and is re-checked through the same open file descriptor before use.

Generated Coturn and Cirrus configs contain credentials. They are written with
same-directory `O_EXCL` temporary files, FD-bound `0600` mode (or explicit
numeric-GID-bound `0640` for Coturn), file and directory `fsync`, and atomic
rename. The destination directory must be owned by root or the invoking user
and must not be group/world writable; an existing destination must itself be a
private, single-link regular file with the same approved GID policy. Do not
weaken these checks by copying secrets through `/tmp`, process substitution,
environment values, or command-line arguments.

`build-cirrus-config.js` refuses to create a config unless this invariant holds:

```text
TURN_CREDENTIAL_TTL_SECONDS >=
  (SESSION_HARD_MAX_MS / 1000) + TURN_CREDENTIAL_RECONNECT_GRACE_SECONDS
```

The checked-in service default reserves a 600-second reconnect grace. Set the
hard session maximum and grace explicitly in production rather than reducing
the credential TTL to make an otherwise-invalid deployment start.

```bash
cd /opt/simworld-studio
SIMWORLD_SECRET_GID="$(getent group simworld | cut -d: -f3)"
TURN_CONFIG_GID="$(getent group turnserver | cut -d: -f3)"
test -n "$SIMWORLD_SECRET_GID" && test -n "$TURN_CONFIG_GID"
sudo env \
  TURN_PUBLIC_HOST=turn.example.edu \
  TURN_EXTERNAL_IP=203.0.113.20 \
  TURN_PRIVATE_IP=10.0.0.20 \
  TURN_SHARED_SECRET_FILE=/etc/simworld/secrets/turn_shared_secret \
  TURN_SHARED_SECRET_FILE_GID="$SIMWORLD_SECRET_GID" \
  TURN_CONFIG_GID="$TURN_CONFIG_GID" \
  node deploy/aws/scripts/materialize-coturn-config.js \
    --template deploy/aws/templates/coturn.conf \
    --output /etc/turnserver.conf
```

The materializer creates the final `root:turnserver 0640` generation through
the temporary file descriptor before rename. It refuses an output that is the
same path or filesystem object as the TURN secret or template. Never execute
the root materializer from a service-user-writable checkout: the pinned release
tree and deployment templates must be root-owned, non-group/world-writable,
and verified against the approved Git/security-manifest revision first.
An older installation whose checkout was ever recursively owned by `simworld`
must not repair trust by running its in-place `bake-ami.sh` as root. Stage a
fresh root-owned checkout at the approved immutable revision, verify its
security manifest out of band, then replace the release generation through the
administrator-controlled deployment procedure.

Install the systemd override, start Coturn, and inspect `journalctl -u coturn`
for parser/listener errors before opening the public firewall rules.

## 4. Install the same-origin gateway

1. Install `deploy/aws/templates/nginx.conf`, replace the example Studio DNS,
   and run `nginx -t`.
2. Install the systemd unit and `/etc/default/simworld`.
3. Confirm `STUDIO_TRANSPORT_PROFILE=trusted_proxy`, canonical HTTPS
   `STUDIO_PUBLIC_ORIGIN`, explicit numeric-loopback `STUDIO_TRUSTED_PROXY`, and
   both Studio secret-file settings are present.
4. Start Node before Nginx. Trusted-proxy startup intentionally fails if the
   endpoint HMAC key, public origin, proxy address, or build revision is absent.

The browser contract is now:

```text
GET /api/pixel-streaming-url
  -> {schema, path: "/pixel-stream/session/ps1_<opaque>", expiresAt, webRtcFps}
WSS /pixel-stream/session/ps1_<opaque>
  -> Nginx -> Node session/auth binding -> 127.0.0.1:<slot Cirrus HttpPort>
```

No response or browser storage may contain the Studio session bearer, raw slot
number, raw Cirrus port, TURN shared secret, or UE control port. Session state is
an HttpOnly, Secure, SameSite=Strict cookie.

The player reports a bounded operational snapshot through the same-origin
gateway:

```text
POST /api/pixel-streaming-telemetry
GET  /api/pixel-streaming-telemetry
```

Both routes require the active Studio session cookie and bind storage to the
server-side owner/session/slot/lease identity. POST accepts only connection and
ICE enums, data-channel state, bounded decoded-frame counters, an advancing
flag, and a selected-candidate type/protocol/TURN-transport tuple. Candidate
addresses are reduced in the browser and must carry `address_redacted: true`;
SDP, IP addresses, ports, URLs, credentials, unknown fields, and replayed
sequences are rejected. GET omits the candidate fingerprint and all endpoint
material. `/api/health` contains only aggregate counts.

This browser-originated, short-lived telemetry is operational evidence only.
It is not trusted release evidence and never makes `/health/ready` pass. Public
readiness still requires the deployment-pinned external receipt described in
`webrtc-readiness-receipt.md`.

## 5. Firewall and listener audit

From the server, archive `ss -lntup` and the firewall/security-group export.
Expected public listeners are only Nginx `80/443` and Coturn
`3478/5349/49160-49200`; every Node/Cirrus/UE control listener must be on
`127.0.0.1` or `::1`.

From a machine outside the server network, verify:

- HTTPS and WSS use the expected certificate and public origin.
- `3002`, all configured Cirrus ports, MCP, SFU, and UnrealCV are unreachable.
- A copied opaque path without its session cookie returns a generic denial.
- A path issued for session A cannot be used with session B.

## 6. Forced-relay acceptance matrix

Run one 12-minute interactive session for each row with browser ICE policy
forced to `relay`; capture `chrome://webrtc-internals` (or Firefox equivalent),
Coturn allocation logs, Node readiness output, and a screen recording.

| Client network | UDP TURN | TCP TURN | TLS TURN | Input/video target |
|---|---:|---:|---:|---|
| External residential NAT | required | required | required | 12 min, no unrecovered disconnect |
| Mobile hotspot | required | required | required | keyboard/mouse + stable video |
| Restricted campus/corporate | attempt | required | required | reconnect after network change |

Acceptance requires a relay candidate, the configured TURN hostname, no host or
server-reflexive fallback in forced-relay mode, working keyboard/mouse input,
and successful reconnect without exposing a raw Cirrus URL. Record browser,
Coturn, Cirrus, UE, and Git revisions in
`runs/production-readiness/webrtc/<timestamp>/receipt.json`.

## 7. TURN credential rotation

The current deployment has one active Coturn REST shared secret and each
running Cirrus slot has a generated short-lived credential. It does not support
zero-downtime hot overlap of old and new Coturn secrets. The administrator must
choose one reviewed operating mode before a live rotation:

1. **Maintenance drain:** block new Studio lease acquisition, wait for every
   active lease to end (or obtain explicit approval to terminate it), rotate
   the secret file atomically, rematerialize and validate Coturn config, then
   restart Coturn and Cirrus in a coordinated maintenance window.
2. **Blue/green Coturn:** provision a separately addressed, fully tested TURN
   service and move newly generated Cirrus configs to it. Retire the old TURN
   service only after its maximum credential TTL plus reconnect grace has
   elapsed and no old allocation remains.

For a maintenance rotation, save the pinned Git/image/config fingerprints and
the old secret file as a root-readable rollback generation; never print either
secret. Generate the replacement into a `0600` temporary file on the same
filesystem, atomically rename it over the configured secret path, rerun
`materialize-coturn-config.js` and `build-cirrus-config.js` validation, and
verify listeners remain loopback/public exactly as designed. After the
coordinated restart, run the full external UDP/TCP/TLS forced-relay matrix,
issue a new deployment fingerprint and readiness receipt, then remove the old
generation under the secret-retention policy.

If validation or any external row fails, restore the prior pinned config and
secret generation, restart the same coordinated service set, and repeat the
listener and forced-relay probes. An offline test proving that old and new
secrets generate distinct redacted Cirrus credentials is only a contract test;
it is not evidence that a live rotation occurred.

## 8. Failure drills and rollback

- Expire a TURN credential and verify only new allocations fail; no secret is
  printed.
- Stop Coturn and verify Studio reports degraded/not-ready instead of silently
  claiming public WebRTC readiness.
- Restart Cirrus and verify the same browser session can request/reload a valid
  opaque endpoint.
- Reuse another session's path/cookie independently and verify both attempts
  fail closed.
- Roll back by restoring the prior pinned image digests and Git revision,
  restarting Node/Cirrus/Nginx/Coturn, and repeating the listener audit. Never
  re-enable the legacy `/cirrus/<slot>` or `/ws/cirrus/<slot>` routes.

## 9. External and administrator gates

The code-only gate is complete, but all of the following remain mandatory
before declaring public WebRTC production-ready:

- an administrator decision for Studio/TURN DNS, the existing `80/443` ingress
  owner, TLS termination, certificate renewal, and (if needed) TURN/TLS `443`;
- Coturn installation, service ownership, realm, public/private IP mapping,
  quota, monitoring, and persistent secret placement;
- approved firewall, ACL, security-group, and NAT rules for HTTPS/WSS, TURN
  UDP/TCP/TLS, and the bounded relay range, plus an archived listener audit;
- a live Cirrus/UE run proving WSS `101`, streamer registration, advancing
  decoded video, keyboard/mouse data channel, disconnect/reconnect, and
  unauthenticated/cross-slot denial;
- normal ICE and forced UDP/TCP/TLS relay tests from at least two independent
  external network classes, including one restricted network where possible;
- an administrator-approved maintenance-drain or blue/green rotation followed
  by an actually executed live credential-rotation drill; and
- a signed deployment manifest, external probe bundle, and readiness receipt
  bound to the exact Git revision, image/config fingerprints, DNS, certificate,
  and evidence TTL.

No DNS, firewall, listener, Coturn, provider, UE, or live service state is
changed by the offline implementation and tests documented here.
