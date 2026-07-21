# SimWorld Studio × VISTA — Completion Status

Last updated: 2026-07-21
Integration branch: `codex/vista-production-completion`

This document separates three states that older project notes mixed together:

- **Code verified**: deterministic contracts and offline tests pass in this checkout.
- **Deployment gated**: implementation exists, but an administrator must provide data,
  secrets, services, content, or public network configuration.
- **Live verified**: the exact deployed revision has produced retained runtime evidence.

Only the third state is sufficient for a Production-ready claim.

## Executive status

| Capability | Code | Deployment/live evidence | Honest status |
| --- | --- | --- | --- |
| `mmg_040` VISTA import and 12-second SceneSpec | Verified with sanitized golden data | Authoritative raw bundle staging still required | Code verified |
| Semantic Postgres/Qdrant asset retrieval | Snapshot, audit, fallback and fail-closed code verified | Full catalog, model artifacts, DB/index and matching UE Content absent locally | Deployment gated |
| Deterministic UE scene build | Typed BuildPlan, exact asset/material/content receipts and rollback verified offline | Real Blueprint + StaticMesh PBR build not yet run | Deployment gated |
| Claude scene builder | Lease-scoped capability broker; Production model pinned to `claude-opus-4-8` | Real provider/build smoke still requires an approved session | Code verified, smoke gated |
| Text / Visual Review | Tool-free strict provider adapter and fake HTTP matrix verified | One real Text and one read-only Visual receipt not yet captured | Cost/live gated |
| 12-second timeline control | Backend-authoritative PIE, scheduler, Stop/Replay, drift and UI verified offline | Exact UE PIE APIs and live keyframes not yet proven | Deployment gated |
| Character animation / IK / fall | Dedicated fixed protocol and packaged UE 5.7.3 plugin verified | Project content driver, skeleton, AnimBP/Control Rig, montages and notifies missing | Content gated |
| Public Pixel Streaming / Coturn | Same-origin WSS, lease isolation, ICE config and receipt validators verified | DNS/TLS/Coturn/firewall and two-network forced-relay matrix absent | Admin/live gated |
| Artifact revision/retention | Existing stores are owner-bound and mostly atomic | One unified revision journal, retention and restore drill are not yet release-proven | Partial |

No current evidence supports calling the whole system Production-ready.

## What is now real in the repository

### VISTA dataset to deterministic build plan

The importer validates an allowlisted dataset revision, sample ID, attempt,
checksums, duration, dialogue/media joins and timestamps. It normalizes the source
into `vista-simworld-scene/v1`, preserves reconstruction/evaluation privilege
boundaries, exposes preview/commit/status APIs, and creates an idempotent artifact.

For `mmg_040`, the checked-in sanitized fixture preserves the 0, 2, 5 and 9 second
beats through a 12-second timeline. Missing asset matches remain explicit; the
importer never silently substitutes cubes.

The scene builder compiles only a committed artifact into a server-pinned BuildPlan.
Production preflight requires exact `/Game` Blueprint or StaticMesh paths, every
MaterialInterface slot, the matching asset snapshot/content revisions, and a
checksum-pinned `Content/VISTA/Metadata/content-revision.json`. It rejects
`/Engine/BasicShapes/*`, fixture revisions, floating/colliding output, stale plans,
and receipt drift before or during execution. Failed execution rolls back only the
actors it created and restores PlayerStart state.

### Runtime authority and model isolation

Studio sessions own an exact physical slot, lease, MCP/UCV ports and process start
token. Port allocation is registered before spawn, heartbeated and released on
shutdown. Queued and retried operations revalidate the exact lease.

Builder subprocesses receive an opaque run capability instead of the root Studio
token or direct UE ports. Capabilities are bound to owner/session/slot/lease/run,
expire, and are revoked on process exit or lease loss. Production free-form UE
mutation is deliberately unavailable; the supported path is typed VISTA
SceneSpec → BuildPlan → fixed runtime operations.

Production Claude building and skill selection ignore stale browser model choices
and use the deployment pin `claude-opus-4-8`. Review has an independent pinned
provider/model/budget policy.

### Review correctness

Text and Visual Review use a tool-free strict verdict provider. Internal HTTP/SSE
auth, timeout, abort, non-2xx handling, malformed stream handling, run/session
isolation and aggregate budget enforcement are covered by a real HTTP fake-provider
matrix. Review Off makes no critic/VLM call. Visual evidence capture is read-only
and must preserve canonical pre/post scene digests.

This is not a live provider result. Production readiness still requires two bounded,
approved calls—one Text and one Visual—whose receipts match the deployed build,
provider, model, usage limits and unchanged Visual scene.

### Timeline and character runtime

The browser is no longer the clock or Play/Stop authority. Fixed backend routes bind
PIE lifecycle to the active lease and exact scene proof. The timeline compiler,
monotonic scheduler, engine-time sampling, bounded queue, event timeout, Stop,
Replay, cleanup, restart quarantine, drift display and evidence UI are implemented.

The repository also contains a dedicated four-command animation transport and a
portable `VistaAnimationContentApi` plugin. A reproducible UE 5.7.3 BuildPlugin
package exists. The plugin intentionally exposes an abstract content-driver
boundary; it does not contain a project-specific human skeleton, IK rig or montage
library. Therefore `drag`, `brace`, `lift_foot`, `fall` and `recover` must remain
not-ready until the target UE project supplies and proves those assets and signals.

### Public WebRTC transport

The public design keeps Node, Cirrus, Streamer, SFU, MCP and UnrealCV on loopback.
An authenticated opaque same-origin WSS path binds viewport and input to the active
session/slot. Coturn REST/HMAC credentials are short-lived and secret-backed;
browser ICE telemetry is bounded and redacted; deployment readiness depends on an
external signed receipt rather than browser claims.

No public listener was opened by the code work. DNS, certificates, Coturn service,
NAT/firewall rules, credential rotation and forced UDP/TCP/TLS relay tests remain
administrator-owned work.

## Fastest path to a visible real VISTA 3D scene

The shortest critical path is not more UI work. It is one controlled deployment on
the 5090 host:

1. Restore SSH/routing to `140.113.215.82` and check out the integration branch.
2. Stage one authoritative `mmg_040` import bundle and verify its exact media/source
   checksums without exposing oracle/review-only data to model input.
3. Inventory the UE 5.3.2 project Content and create an immutable content revision.
4. Provision pinned Postgres, Qdrant and embedding artifacts; build and audit one
   matching semantic snapshot.
5. Register a verified layout profile whose Blueprint/StaticMesh/PBR paths exist in
   that exact Content revision.
6. Run a disposable scene preflight, then one confirmed BuildPlan execution and
   retain screenshots plus actor/material/content receipts.
7. Rebuild/load the animation plugin for UE 5.3.2 and implement the project content
   driver before enabling the 12-second action timeline.

Steps 2–6 produce the first defensible, textured VISTA-world scene. Character IK,
live Review and public WebRTC can then be proven independently without blocking the
initial scene image.

## Remaining external gates

- Authoritative complete asset catalog and matching UE Content revision.
- Pinned dense/sparse model artifacts and administrator-managed secret files.
- Live Postgres/Qdrant/embedding provisioning, index build and backup/restore drill.
- Real UE Blueprint/StaticMesh PBR spawn and immutable evidence capture.
- UE 5.3.2 plugin rebuild/load plus a project-specific character content driver.
- Disposable-scene Text/Visual provider calls with approved cost limits.
- Public DNS, TLS, Coturn, firewall/NAT and two independent external test networks.
- Writable Production save/restore, retention cleanup and rollback drill.
- Release/admin sign-off on the exact Git, service, content and evidence revisions.

## Other product modes

Task Generation, Agent Training and Co-evolution have accumulated UI and backend
code since the old May status document. Their old “complete”/“missing” table is no
longer reliable. They are outside the current VISTA-world production slice and must
receive a separate evidence-based audit before any demo-ready or Production-ready
claim.

The authoritative implementation checklist is
`docs/specs/production-readiness/tasks.md`; operational gates and exact commands are
in the runbooks beside it.
