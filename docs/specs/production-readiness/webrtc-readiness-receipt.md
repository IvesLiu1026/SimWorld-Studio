# Public WebRTC Readiness Receipt

Status: offline verifier implemented; live evidence remains deployment-gated.
This verifier does not open sockets, start Cirrus/Coturn/UE, launch a browser,
or make a network request. It only consumes an operator-produced, redacted JSON
probe file.

## Acceptance contract

The input must conform to
`simworld-webrtc-probe-results/v1`. The verifier fails closed unless all of the
following evidence is present:

- canonical public-DNS HTTPS origin, valid TLS 1.2+ certificate, HTTPS 200,
  same-certificate WSS 101, same-origin opaque signalling path, and no mixed
  content;
- reachable Cirrus player signalling, registered UE streamer, and loopback-only
  Cirrus HTTP/streamer listeners;
- unauthenticated and cross-session denial plus externally unreachable raw
  control ports;
- at least two distinct external network classes; each network must complete
  forced-relay tests through UDP TURN, TCP TURN, and TLS TURN;
- complete ICE gathering with a selected `relay` candidate matching the
  configured TURN endpoint and a redacted, short-lived REST/HMAC credential;
- at least 12 minutes of decoded video, acknowledged data-channel input, and a
  successful reconnect for every network/transport pair; and
- explicit negative leakage assertions for host candidates, private addresses,
  raw ports, credentials, and session tokens.

The probe file must never contain candidate SDP, IP addresses, ports, TURN
usernames/passwords, Studio tokens, cookies, opaque signalling paths, or raw
URLs. Store only booleans, bounded metrics, public origin, hashes, timestamps,
and enums defined by the schema. Compute candidate fingerprints inside the
collector after redaction, then discard raw candidate material.

## Deployment binding

Before testing, create a secret-free deployment manifest that identifies the
reviewed ingress, certificate fingerprint, pinned Node/Nginx/Coturn/Cirrus
artifacts, firewall/ACL revision, and redacted configuration hashes. Hash its
canonical bytes with SHA-256. That independently recorded value is the
`deployment_fingerprint`; it must not be copied from the browser probe when
invoking the verifier.

The verifier separately requires the exact 40-character deployed Git revision,
expected HTTPS origin, and optionally the independently observed certificate
SHA-256 fingerprint. A receipt expires no more than 24 hours after recording.
Changing the deployment fingerprint, build, origin, certificate, or current
time makes verification fail.

## Offline verification

Run this only after administrators have completed DNS, TLS, firewall, Coturn,
and external-browser testing described in `webrtc-coturn-runbook.md`:

```bash
cd simworld_studio_workspace/web/server
node webrtc-readiness-cli.js \
  --input /secure/evidence/webrtc-probes.redacted.json \
  --expect-build-revision "$SIMWORLD_BUILD_REVISION" \
  --expect-deployment-fingerprint "$WEBRTC_DEPLOYMENT_FINGERPRINT" \
  --expect-origin "$STUDIO_PUBLIC_ORIGIN" \
  --expect-certificate-fingerprint "$WEBRTC_CERTIFICATE_SHA256" \
  --output /secure/evidence/webrtc-readiness-receipt.json
```

The output receipt is written atomically with mode `0600`. Standard output is a
small secret-safe summary and SHA-256 digest. Errors contain only a structured
code and field path; raw probe values are never echoed.

The verifier returning `ready: true` validates the supplied evidence contract;
it is not itself a live network probe. Production readiness must continue to
fail until a current receipt is generated from the required external tests.
