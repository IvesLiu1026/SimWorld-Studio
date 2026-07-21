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
| Semantic Postgres/Qdrant asset retrieval | Deployment/audit contracts, sealed pending-job preparation and reviewed fail-closed executor are integrated and verified offline | No Production adapter, DB/Qdrant/model service or live index job was run | Code verified; adapter/deployment/live gated |
| Deterministic UE scene build | Typed BuildPlan, exact asset/material/content receipts and rollback verified offline | Real Blueprint + StaticMesh PBR build not yet run | Deployment gated |
| Claude scene builder | Lease-scoped capability broker; Production model pinned to `claude-opus-4-8` | Real provider/build smoke still requires an approved session | Code verified, smoke gated |
| Text / Visual Review | Durable coordinator/outbox, preflight, tool-free adapter, browser recovery and managed opaque evidence lifecycle are integrated and verified offline | No paid/live provider call or retained provider receipt; Production evidence root/retention/restore not exercised | Code verified; cost/operations/live gated |
| 12-second timeline control | Backend-authoritative PIE, scheduler, Stop/Replay, drift and UI verified offline | Exact UE PIE APIs and live keyframes not yet proven | Deployment gated |
| Character animation / IK / fall | v1.1.0 source contract and 13 derived target paths are integrated and verified offline | Filename-only candidates exist, but no verified profile/digests, UE 5.3.2 build/load or live receipt | Code verified; content/live gated |
| Public Pixel Streaming / Coturn | Same-origin WSS, lease isolation, ICE config and receipt validators verified | DNS/TLS/Coturn/firewall and two-network forced-relay matrix absent | Admin/live gated |
| Artifact revision/retention | Unified import/scene/review/timeline journal is wired and verified in offline code tests | Production journal root, retention cleanup and backup/restore drill were not run | Code verified; operations/live gated |

No current evidence supports calling the whole system Production-ready.

This integration pass did not call a model provider, start Postgres/Qdrant or an
embedding service, mutate or compile/load UE, or open a public listener. The local
integration branch is code evidence only until it is pushed and exercised against
the pinned deployment, content and service revisions.

## What is now real in the repository

### VISTA dataset to deterministic build plan

The importer validates an allowlisted dataset revision, sample ID, attempt,
checksums, duration, dialogue/media joins and timestamps. It normalizes the source
into `vista-simworld-scene/v1`, preserves reconstruction/evaluation privilege
boundaries, exposes preview/commit/status APIs, and creates an idempotent artifact.

For `mmg_040`, the checked-in sanitized fixture preserves the 0, 2, 5 and 9 second
beats through a 12-second timeline. Missing asset matches remain explicit; the
importer never silently substitutes cubes.

The semantic job tooling can seal a reviewed recipe, immutable inputs and exact
asset/content pins into a pending job. The integrated typed executor accepts only
that sealed bundle and independent pins, uses allowlisted resumable phases, bounded
Git/source attestation and fail-closed terminal evidence. Its only registered
adapter is an offline fixture with `production_capable=false`; a Production plan
fails closed without a separately reviewed adapter and exact credential bindings.
Preparation and offline execution do not create an index: no catalog migration,
embedding inference, Postgres write, Qdrant collection build or live audit has been
executed.

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

Text and Visual Review use a tool-free strict verdict provider. A durable coordinator
and outbox bind each paid attempt to the artifact journal; preflight occurs before
builder work, and browser recovery resumes from server-owned state. Internal
HTTP/SSE auth, timeout, abort, non-2xx handling, malformed stream handling,
run/session isolation and aggregate budget enforcement are covered by a real HTTP
fake-provider matrix. Review Off makes no critic/VLM call. The integrated managed
private evidence lifecycle uses opaque handles, bounded/cancellable capture,
fail-closed retain/cleanup and path redaction rather than exposing server paths to a
builder. This is offline code evidence only: a Production journal/evidence root,
service identity, retention/cleanup/restart and backup/restore still require live
operations evidence. Visual capture remains read-only and must preserve canonical
pre/post scene digests.

This is not a live provider result. Production readiness still requires two bounded,
approved calls—one Text and one Visual—whose receipts match the deployed build,
provider, model, usage limits and unchanged Visual scene.

### Timeline and character runtime

The browser is no longer the clock or Play/Stop authority. Fixed backend routes bind
PIE lifecycle to the active lease and exact scene proof. The timeline compiler,
monotonic scheduler, engine-time sampling, bounded queue, event timeout, Stop,
Replay, cleanup, restart quarantine, drift display and evidence UI are implemented.

The integrated `VistaAnimationContentApi` v1.1.0 source defines a concrete, pinned
project content-driver contract and 13 derived
`/Game/VISTA/MMG040/...` target paths, with deterministic source-audit and offline
tests. This is source evidence only: those target packages have not been derived,
hashed or proven in the target project, and v1.1.0 has not been compiled or loaded
with UE 5.3.2.

A read-only filename audit found 2,937 `.uasset`/`.umap` package files and names
consistent with mannequin/skeletal/AnimBP/Control Rig/IK/lifting/fall/chair/box/table
candidates. This is not an Asset Registry, catalog/index row count, or proof of
asset class, object path, loadability or spawnability. The current inspection
profile remains `verification_status=candidate_unverified`,
`source_lineage_status=candidate_unverified`, `runtime_ready=false` and
`start_allowed=false`; it is not a verified `mmg_040` content profile or live
receipt. The older UE 5.7.3 BuildPlugin result is
historical v1.0.0 evidence only and cannot validate v1.1.0 or UE 5.3.2. Therefore
`drag`, `brace`, `lift_foot`, `fall` and `recover` remain not-ready until the derived
assets, notifies, contact/IK behavior and exact live process are proven.

### Artifact lifecycle

The integration branch wires a unified append-only artifact journal through VISTA
import, scene build, Review and timeline terminal transitions. Offline tests cover
owner binding, recovery and fail-closed transitions. Production still lacks a
provisioned live journal root, executed retention cleanup and a backup/restore drill;
code verification is not operational durability evidence.

### Public WebRTC transport

The public design keeps Node, Cirrus, Streamer, SFU, MCP and UnrealCV on loopback.
An authenticated opaque same-origin WSS path binds viewport and input to the active
session/slot. Coturn REST/HMAC credentials are short-lived and secret-backed;
browser ICE telemetry is bounded and redacted; deployment readiness depends on an
external signed receipt rather than browser claims.

No public listener was opened by the code work. DNS, certificates, Coturn service,
NAT/firewall rules, credential rotation and forced UDP/TCP/TLS relay tests remain
administrator-owned work.

## Latest local verification checkpoint

The 2026-07-21 integration checkpoint passed server 698, frontend 23, tools 208,
semantic executor 53, animation contract 22, candidate-profile 10, packaging
security 35, staging 6, Review Chromium 4 and VISTA Import Chromium 1 tests. Vite
built 1,879 modules and 25 schemas passed meta-schema validation. This is local
offline/mock evidence only; it did not contact the target, a model provider, UE,
Postgres, Qdrant or an embedding service.

## Fastest path to a visible real VISTA 3D scene

The shortest critical path is not more UI work. It is one controlled deployment on
the 5090 host:

1. Restore SSH/routing to `140.113.215.82` and check out the integration branch.
2. Stage one authoritative `mmg_040` import bundle and verify its exact media/source
   checksums without exposing oracle/review-only data to model input.
3. Refresh the read-only `gym_citynav` inventory on the target and create an
   immutable UE 5.3.2 Content revision; do not treat candidate names as verified pins.
4. Provision and review the Production semantic adapter, pinned Postgres, Qdrant and
   embedding artifacts; seal the exact Production plan, then build/audit one matching
   semantic snapshot in an approved execution window.
5. Derive and hash a verified layout/animation profile whose Blueprint,
   StaticMesh, skeletal, PBR and action paths exist in that exact Content revision.
6. Run a disposable scene preflight, then one confirmed BuildPlan execution and
   retain screenshots plus actor/material/content receipts.
7. Rebuild/load the animation plugin for UE 5.3.2, implement the project-specific
   `IVistaMmg040ProjectBackend`, and derive the verified content profile before
   enabling the 12-second action timeline.

Steps 2–6 produce the first defensible, textured VISTA-world scene. Character IK,
live Review and public WebRTC can then be proven independently without blocking the
initial scene image.

## Remaining code, operator-approved live, and external gates

- Authoritative complete asset catalog and matching UE Content revision.
- Pinned dense/sparse model artifacts and administrator-managed secret files.
- Reviewed Production semantic adapter and an operator-approved sealed execution plan.
- Live Postgres/Qdrant/embedding provisioning, index build and backup/restore drill.
- Real UE Blueprint/StaticMesh PBR spawn and immutable evidence capture.
- UE 5.3.2 plugin rebuild/load plus a project-specific backend and verified character content.
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
