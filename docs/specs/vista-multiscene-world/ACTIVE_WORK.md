# Active Work: VISTA Multi-Scene Continuous 3D World

- Coordinator: Codex `/root`
- Session: 2026-08-11 multi-scene world expansion
- Worktree: `/home/yhliu/SimWorld-Studio-worktrees/vista-multiscene-world`
- Branch: `codex/vista-multiscene-world`
- Base: `codex/vista-blender-world@1dfff7f67a426e57c5c632b01784d32f471cd92d`
- Status: discovery/specification only; implementation awaits code-scope
  approval.

## Current Ownership

- Coordinator owns only `docs/specs/vista-multiscene-world/**` before approval.
- Three read-only reviewers audited architecture, feasibility, and early user
  value; they changed no files.
- No implementation worker owns Production source paths yet.

## Delivery Order

1. M0: strict contracts and sealed legacy adapter.
2. UE capability spike: two textured bundles, namespace/collision/NavMesh and
   real performance instrumentation.
3. M1a: legacy office core → main corridor/lobby → teaching-kitchen/café,
   one persistent map, one interaction, overlay launch/restart, and an uncut
   out-and-back traversal beyond 12 seconds. This is three named slices, not a
   claim that all subspaces in zones 1-3 are finished.
4. M1: zones 1-5.
5. M2: all eight zones.
6. M3: compiled authoritative WorldSession, overlay/reset/persistence and
   eight-hour soak after the full UE development toolchain is provisioned.

## Apply Gates

Approval of the spec authorizes code/schema/test implementation only. Record a
separate authorization before each:

- external asset download or redistribution;
- every Blender executable or generation invocation, including tiny fixtures;
- Unreal commandlet import/composition or project/map write;
- live GPU/Pixel Streaming launch;
- paid model/API call, upload, deploy, merge, or public exposure.

Dry-run plans, hashes, tests, preflight, and candidate ledgers may be prepared
while those gates remain closed.

## Must Not Touch

- Accepted attempt-11 runtime and its append-only evidence.
- `vista-blender-world-final-r5-20260811`,
  `vista-blender-mcp-final-20260811`, and
  `simworld-nlp-demo-20260721-142800` tmux sessions.
- GPU 1 and ports
  `3012/3022/55570/55582/8595/8596/8615/8616/8899/8919/8400`.
- Production port 8000, canonical VISTA datasets, historical receipts, and the
  old `vista-blender-world` source worktree.
- Live attempt-11 as a build input, NAS cleanup, external upload, or paid model
  use without the applicable gate.

## Pinned Inputs and Environment

- Canonical legacy bundle:
  `/mnt/NAS2/yhliu/SimWorldStudio/vista-blender-world/runs/20260811T-procedural-chair-upgrade-r3/blender`.
- Manifest SHA-256:
  `1c1008ebcd3b9cb6f130a54f65e04de530e81f3a6954c042e43f237fbe35cc55`.
- GLB SHA-256:
  `c3d67a34f0f0bd720133dc8ce08c7bb52b0c3008386aec04957014d76010e759`.
- GPU 0: RTX A6000, approximately 49 GiB and idle at discovery; requires a
  fresh render/device preflight and explicit assignment before use.
- GPU 1: reserved by two live UnrealEditor processes.
- Root filesystem: approximately 96% used, 39 GiB free. New assets/projects go
  to NAS, which had approximately 58 TiB free at discovery.
- Current UE artifact supports UnrealEditor/Cmd, Interchange, Python, and
  Pixel Streaming but lacks RunUAT, UBT/UHT, Engine Source, and cooking tools.

## Review Corrections Incorporated

- Split M1a, M1, and M2 builds/acceptance instead of conflating them.
- Moved the smallest visible three-slice connected world ahead of Studio
  registry work.
- Added a two-bundle textured UE capability spike before bulk authoring.
- Aligned PBR/hero thresholds: M1a 3/6, M1 5/8, M2 8/12.
- Added objective visual, overlay, telemetry, memory, lifecycle, and evidence
  semantics.
- Added explicit implementation ownership for the preview HUD, fixed typed
  interaction, and overlay-at-launch/clean-restart hook.
- Split indoor completion and zones 6/7/8 into independent authoring,
  composition, acceptance, and publication tasks.
- Deferred formal generations/reset/checkpoint to M3 while preserving
  continuous editor-process lifetime in M1a-M2.
- Required a clean candidate commit before each live run and a later
  evidence-pointer commit/push, avoiding a circular self-hash.

## Next Gate

The user approves `requirements.md`, `design.md`, and `tasks.md` for code work.
Then M0 tasks receive non-overlapping worktrees. External PBR acquisition,
Blender/UE apply operations, and GPU runtime remain later explicit gates.
