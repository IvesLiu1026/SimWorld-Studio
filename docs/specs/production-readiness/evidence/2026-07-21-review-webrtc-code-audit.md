# Review and WebRTC code-only closure evidence

Date: 2026-07-21

Base revision: `82e0b7fced4bd44b5ca3d20cdd30702d4c6bf91f`

Scope: `T1A.10` and the offline portion of `T4.10`/`T4.11`

## Safety boundary

This audit changed and tested repository code only. It did not invoke a real
review provider, connect to UE, acquire or terminate a live Studio lease,
restart a service, open a public listener, install or reconfigure Coturn,
modify DNS/firewall/NAT, or execute a live credential rotation.

## Review closure

- The production coordinator derives its state and cancellation scope from the
  exact server-validated active lease authority: owner, session, slot, lease,
  and MCP port. The public conversation ID is validated and namespaced inside
  that authority; a caller-supplied session ID is not an authorization input.
- Text and Visual handlers receive an internal lease-derived identifier for
  nested builder requests and private intent/run state. Outer SSE metadata
  remains compatible with existing clients.
- Trusted-proxy requests without an active Studio lease fail closed. Loopback
  uses a deterministic server-configured scope so local development keeps its
  previous single-session behavior.
- Exact run cancellation is isolated by lease-derived scope. A second lease
  that knows another run or conversation ID cannot cancel or observe it.
- A real HTTP coordinator harness covers Text and Visual success, independent
  budget accounting, 401/429/500/timeout/malformed-SSE failures, builder
  failure without critic execution, Visual-only capture, exact cancellation,
  and cross-lease isolation.

`T1A.10` is therefore code-complete. `T1A.11` remains intentionally open.

## WebRTC closure

- The browser reduces `RTCPeerConnection.getStats()` to bounded state enums,
  advancing decoded-frame state, data-channel state, and selected-candidate
  type/protocol/TURN transport. It never uploads SDP, candidate addresses,
  ports, TURN URLs, or credentials.
- POST/GET telemetry routes require normal Studio access and an active session.
  Records are keyed to a hash of the server-side owner/session/slot/lease
  identity, expire in memory, reject unknown/sensitive fields and repeated
  sequence numbers, and are deleted when the lease is released.
- Session status omits the candidate fingerprint and raw endpoint material;
  health exposes aggregate counts only. Browser telemetry is explicitly
  untrusted operational evidence and cannot satisfy `/health/ready`.
- Production readiness remains bound to the separately verified external
  receipt that covers certificate/WSS `101`, Cirrus and streamer state, ICE,
  forced UDP/TCP/TLS relay, decoded video, input data channel, reconnect, and
  isolation across two external network classes.
- Cirrus configuration now fails closed unless TURN credential TTL covers the
  hard Studio session maximum plus the configured reconnect grace. Offline
  rotation contracts prove old/new secrets generate distinct credentials and
  that output remains redacted.

`T4.10` is therefore code-complete. Live portions of `T4.11` and all of
`T4.12` remain open.

### Independent secret-path hardening delta

A later independent security review reproduced two destructive deployment
defects before commit: either config CLI could replace its own TURN shared
secret when `--output` named that file, and group-readable `0640/0650` secrets
were accepted without binding the group to an approved numeric GID. It also
found that the AMI recipe made the release tree writable by the service account
even though an administrator later executes its materializer as root.

The corrected contract now rejects path and existing-inode aliases against the
secret and Coturn template, accepts group read only as exact `0040` bound to an
explicit numeric GID, rejects special/execute/world bits and unsafe ancestors,
and performs owner/GID/mode checks through the temporary FD before rename.
Post-commit directory-fsync failure is reported as installed-but-not-confirmed,
not as a clean rollback. The AMI builds under a distinct, private build UID,
seals and validates build outputs before root copies them, and keeps release
code root-owned and service-user read-only. Compose explicitly pins the host
runtime UID:GID and reviewed host secret GID. Focused
tests preserve protected input bytes across both destructive-alias attempts,
cover wrong-GID and `0650`, nested writable ancestors, and fault-injected
post-commit fsync semantics. These remain offline contracts; no live secret was
read or rotated.

## Offline verification

- Review coordinator and focused Review contracts: 33 passing tests.
- Pixel Streaming gateway and telemetry: 15 passing tests.
- Security source parity: 6 passing tests.
- Cirrus config builder: 4 passing tests.
- Full Node `*.test.js` suite: 467 passing tests, 0 failures.
- Legacy server unit runner: 11 passing, 18 live integration/UE tests skipped
  by the explicit `unit` selection.
- AWS deployment script contracts: 21 passing tests after the secret-path
  hardening delta.
- Launcher/security packaging suite: 28 passing tests.
- Production frontend build: passed (1,879 modules transformed).

## Required external and administrator gates

Review provider:

1. Obtain user approval for exactly two bounded, tool-free
   `claude-opus-4-8` calls: one Text and one read-only Visual review.
2. Use a disposable, stable UE scene and shared-broker before/after canonical
   snapshots plus bounded screenshots; do not open an ad-hoc UE socket.
3. Verify current CLI authentication/version and use a managed `0700` evidence
   directory with `0600` receipts.
4. Pin both passing receipts to the exact deployed commit, provider, model,
   CLI, budget, scene digests, and TTL. Any scene diff or identity mismatch
   remains fail-closed.

Public WebRTC:

1. Decide Studio/TURN DNS, existing ingress ownership, TLS termination and
   renewal, TURN public/private IP mapping, and any TURN/TLS `443` strategy.
2. Install and operate Coturn with approved realm, quota, monitoring, secret
   storage, and external/private address configuration.
3. Approve and apply firewall/ACL/NAT rules for HTTPS/WSS, TURN UDP/TCP/TLS,
   and the bounded relay range; prove internal Node/Cirrus/UE ports remain
   loopback-only.
4. From at least two independent external network classes, prove WSS `101`,
   streamer registration, normal ICE, forced UDP/TCP/TLS relay, advancing
   decoded frames, keyboard/mouse data channel, reconnect, and unauthenticated
   and cross-slot denial.
5. Choose maintenance-drain or blue/green credential rotation, execute the
   live drill, and prove rollback. Static-secret hot overlap is not implemented.
6. Produce and sign a deployment manifest, redacted probe bundle, and readiness
   receipt bound to the exact Git/image/config/DNS/certificate revisions.
