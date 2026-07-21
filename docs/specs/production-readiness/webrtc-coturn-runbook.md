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

## 3. Materialize and validate Coturn

The reviewed template uses Coturn REST auth. The same shared-secret file is read
by the per-slot Cirrus config builder to produce short-lived HMAC credentials.

```bash
cd /opt/simworld-studio
sudo env \
  TURN_PUBLIC_HOST=turn.example.edu \
  TURN_EXTERNAL_IP=203.0.113.20 \
  TURN_PRIVATE_IP=10.0.0.20 \
  TURN_SHARED_SECRET_FILE=/etc/simworld/secrets/turn_shared_secret \
  node deploy/aws/scripts/materialize-coturn-config.js \
    --template deploy/aws/templates/coturn.conf \
    --output /etc/turnserver.conf
sudo chown root:turnserver /etc/turnserver.conf
sudo chmod 0640 /etc/turnserver.conf
```

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

## 7. Failure drills and rollback

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
