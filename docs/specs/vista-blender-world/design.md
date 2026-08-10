# Design: VISTA Blender-to-Unreal Interactive World

Status: Approved for bounded implementation
Updated: 2026-08-11
Depends on: requirements.md

## Summary

Build a script-first Blender asset forge around the existing `mmg_040` vertical
slice. A pinned portable Blender runs a deterministic Python script to create a
detailed office cabinet, ergonomic wheeled chair, and presentation-quality room
details. Both a canonical
GLB and importer-compatible JSON `.gltf`/`.bin` pair are validated, then imported
into a fresh copy of the previously proven UE 5.3.2
disposable project, composed into a new map, and served through a separate
loopback Pixel Streaming stack on GPU 1. Blender MCP is installed as a bounded
interactive refinement lane, not as the production build system.

## Architecture and Flow

```text
VISTA mmg_040 SceneSpec + existing real assets
  -> asset-gap contract (TallOfficeCabinet + ErgonomicOfficeChair)
  -> tools/blender/build_vista_mmg040_office.py
  -> .blend + GLB + .gltf/.bin + preview PNG + manifest
  -> structural/visual validation
  -> fresh UE 5.3.2 disposable project
  -> Interchange commandlet import
  -> MMG040_Office_BlenderR1 composition
  -> GPU 1 Unreal Editor + MCP + Pixel Streaming
  -> fixed screenshots, actor/collision/health/input receipts
  -> optional bounded critic/repair (maximum two iterations)
```

### Runtime isolation

- Source worktree: `/home/yhliu/SimWorld-Studio-worktrees/vista-blender-world`.
- Artifact root: `/mnt/NAS2/yhliu/SimWorldStudio/vista-blender-world/runs/<run-id>`.
- UE base: byte-preserved `disposable-project-r7`; each attempt is copied to a
  new append-only run directory before import or map mutation.
- Tentative loopback port family: Studio `3022`, MCP `55582`, Cirrus HTTP/WS
  `8615/8616`, SFU `8919`; final preflight may move the whole family.
- Runtime GPU: GPU 1 only. GPU 0 remains blocked by render-node permission.

### Blender authoring

The canonical script creates predictable named collections:

- `VISTA_RoomShell`: wall panels, acoustic ceiling, skirting, floor trim.
- `VISTA_TallOfficeCabinet`: carcass, two doors, recessed panels, hinges,
  handles, adjustable shelves, feet, labels and subtle wear details.
- `VISTA_ErgonomicOfficeChair`: five-star caster base, gas lift, mechanism,
  contoured seat, mesh/upholstered back and adjustable armrests.
- `VISTA_Presentation`: non-exported camera/light rig for preview images.

Geometry uses real metric dimensions, applied transforms, bevel/weighted-normal
detail, UVs where needed, and Principled BSDF materials. Production exports
exclude the presentation cameras/lights. A JSON manifest is generated from the
actual Blender scene immediately before export and binds both export forms.

### Blender MCP

The formerly popular `ahujasid/blender-mcp` source is currently unavailable and
its surviving PyPI artifact is not a complete, source-traceable add-on, so it is
explicitly prohibited. Use the externally installed GPL-3.0
`zorak1103/blender-mcp` tag `v0.5.1`, commit
`43d60c36aadc892739d42051f64f87fe55a57b48`, after its locked unit suite passes.
Keep it outside this repository. The add-on binds `127.0.0.1`, requires its
0600 bearer token, and keeps `execute_python` disabled. Blender runs with a
disposable HOME/profile and locked dependencies under the append-only run root.
Claude receives no Studio root token, database secret, SSH agent, canonical
VISTA data, or human config directory. The MCP lane may inspect/render/refine;
accepted changes must return to the canonical headless script.

### Unreal integration

Reuse the already proven UE 5.3 Interchange commandlet approach rather than the
ambiguous live Studio socket import. The import script creates `/Game/VISTA/
External/Procedural/MMG040OfficeR1`, validates object paths and bounds, adds
simple collision where required, and saves all packages. A second commandlet
loads `MMG040_Office_CommandletR3`, duplicates/composes a new map, replaces the
shelf surrogate with the generated cabinet, improves lighting/material balance,
removes the visible provisional chair, verifies the fixed PlayerStart capsule
against blocking actor bounds before save, and configures one movable/physics
prop using standard UE components.

### Visual acceptance

Capture at least four views at a fixed resolution:

1. first-person scene overview;
2. high-box/chair/cabinet affordance view;
3. cabinet material/detail close-up;
4. interaction/physics state after user input.

Record black/clipping percentages, image dimensions and SHA-256. UE actor and
asset inventories prove that the screenshots match the imported map revision.

## Interfaces and Contracts

- `tools/blender/build_vista_mmg040_office.py --output-root <dir> --seed 4040`
- `tools/blender/validate_vista_asset.py <manifest-or-glb>`
- `tools/blender/run_vista_blender_build.sh <run-dir>`
- UE import/composition scripts accept only absolute run paths beneath the
  selected append-only root and refuse archived/canonical destinations.
- The remote handoff exposes a single local Studio URL through SSH; internal UE
  ports are not published.

## Data Model and Migration

No canonical dataset or Production database migration occurs. Generated data is
append-only evidence. Each run has:

```text
run.json
blender/{source.blend, export.glb, preview-*.png, manifest.json, logs/}
ue/{project/, import-receipt.json, scene-receipt.json, screenshots/, logs/}
review/{metrics.json, critic.json, repair-ledger.json}
```

## File Plan

- `docs/specs/vista-blender-world/**`: requirements, design, tasks, handoff.
- `tools/blender/**`: script-first asset generator, validator, wrappers, tests.
- `tools/ue/**` or an existing compatible tools namespace: bounded UE import,
  composition and capture scripts.
- Runtime artifacts remain outside Git; only small representative previews and
  deterministic manifests may be committed after review.

## Failure Handling

- Every attempt writes to a new run/attempt directory.
- A failed Blender export, UE import, or rendered launch is never reused as a
  successful input.
- Import has one fresh-project retry; model/critic has at most two iterations.
- Existing GPU 1 demo is preserved; final runtime chooses non-conflicting ports.
- Rollback is stopping only this task's processes and deleting nothing; evidence
  and source projects remain intact.

## Testing Strategy

- Static Python syntax plus helper unit tests outside Blender.
- Headless clean-start Blender build and second reproducibility build.
- GLB and JSON glTF inspection for meshes/materials/bounds/extensions and
  nonblank PNG checks.
- UE commandlet import/load/collision/asset inventory in a disposable project.
- Live health, Pixel Streaming frame decode, input/data-channel and screenshot
  evidence from the exact launched revision.
- Existing focused server/tool tests and Vite build for any touched web code.

## Rollout and Observability

This is a loopback development vertical slice, not Production deploy. Process
PIDs, ports, GPU, Git SHA, content/map hashes and log paths are written to the
run receipt. The Mac uses an SSH tunnel. Public WebRTC remains a separately
approved administrator phase.

## Tradeoffs

- Headless Blender is chosen over MCP-first generation for reproducibility and
  security; MCP remains available for the requested interactive workflow.
- A real cabinet plus existing CC0/official props is chosen over regenerating
  the whole room, reducing time and retaining semantic fidelity.
- Standard UE physics/navigation is chosen over compiling the unfinished
  animation plugin because the exact UE 5.3.2 build tree is unavailable.

## Traceability

- R1 -> isolated worktree, preflight and run receipt.
- R2 -> Blender scripts, manifest and reproducibility checks.
- R3 -> pinned loopback MCP setup and security wrapper.
- R4 -> Interchange import and scene-composition commandlets.
- R5 -> dedicated GPU 1 loopback runtime and SSH handoff.
- R6 -> fixed views, deterministic metrics and inventories.
- R7 -> bounded Claude/critic ledger and stop conditions.
- R8 -> tests, atomic commits, push and handoff.

## Resolved runtime choices

- Claude Code `2.1.215` exposes the `fable` model alias. `ultracode` is an
  interactive workflow trigger, not a CLI model or effort value; bounded jobs
  use `--model fable --effort max` and retain hard iteration/time limits.
- Blender `4.5.8 LTS` is already installed at the verified user-space path.
- The MCP lane uses the pinned authenticated implementation above; it is not
  allowed to become the canonical asset build path.

## Approval

- Requested by: yhliu
- Approved by: yhliu, 2026-08-11
- Date: 2026-08-11
