# Requirements: VISTA Blender-to-Unreal Interactive World

Status: Approved for bounded implementation
Updated: 2026-08-11

## Problem

The current VISTA/SimWorld integration can build a deterministic `mmg_040`
office scene and has real UE assets, but it lacks a reproducible procedural
asset-authoring lane, retained visual proof, and a current interactive Unreal
runtime showing a convincing environment. The public BlenderMCP workflow is
useful for interactive refinement but exposes arbitrary Python execution and
cannot be the production source of truth.

## Goals

- Deliver one visibly realistic `mmg_040` office/storage-room vertical slice.
- Generate at least one scene-defining asset through version-controlled Blender
  Python and export it with a reproducibility manifest and preview.
- Import the output into a fresh disposable UE 5.3.2 project without modifying
  the archived/canonical project.
- Expose a loopback Studio/Pixel Streaming session that a Mac can reach through
  SSH and use for camera/navigation input.
- Install and validate a loopback-only Blender MCP lane for bounded interactive
  refinement while keeping headless Blender as the canonical build route.
- Retain append-only evidence and push coherent source changes to GitHub.

## Non-goals

- Public Internet deployment, Coturn, DNS, TLS, or opening control ports.
- Claiming the three-record semantic smoke catalog is a Production asset index.
- Compiling the animation plugin without an exact full UE 5.3.2 build tree.
- Reconstructing every VISTA scene or completing character IK/fall behavior.
- Allowing an LLM-controlled Blender process access to human credentials,
  canonical datasets, production services, or unrestricted host paths.

## Assumptions

- The existing GPU 1 UE 5.3.2/Pixel Streaming path remains usable; GPU 0 and
  `/dev/dri/renderD128` remain an administrator gate.
- `/mnt/NAS2/yhliu` is available for append-only run artifacts; the root volume
  is too full for large generated workspaces.
- The existing `MMG040_Office_CommandletR3` scene and CC0/official box and
  stool assets may be copied into a new disposable project. Its visibly basic
  provisional chair is a surrogate and must not survive the accepted map.
- The user's request on 2026-08-11 explicitly approves this bounded build,
  GPU 1 use, Blender generation, and bounded Claude Code assistance. It does
  not approve public deploy, secret copying, or canonical dataset mutation.

## Requirements

### R1 — Honest preflight and isolation

WHEN implementation begins THEN the system SHALL record the exact Git base,
worktree, GPU/port ownership, tool versions, writable artifact root, and any
administrator-only blockers before starting mutation.

Acceptance notes:
- Work occurs on `codex/vista-blender-world` in its own worktree.
- Existing GPU 1 demo processes and archived runs are not modified or killed.
- Production VISTA port `8000` and canonical VISTA datasets remain untouched.

### R2 — Reproducible Blender asset build

WHEN the asset build runs from a clean Blender startup THEN it SHALL generate
the same named asset hierarchy, metric dimensions, materials, GLB, JSON
`.gltf`/`.bin`, `.blend`, preview PNG, and manifest from a version-controlled
script and fixed seed.

Acceptance notes:
- The bundle includes a detailed tall office storage cabinet, room kit, and
  ergonomic wheeled office chair. The generated chair replaces the visibly
  basic source-scene proxy instead of preserving a fallback.
- The manifest records Blender version, script SHA-256, output SHA-256, bounds,
  mesh/material/triangle counts, units, source/license, and build timestamp.
- No silent basic-cube fallback is accepted.

### R3 — Safe Blender MCP lane

WHEN Blender MCP is enabled THEN it SHALL use the approved pinned third-party
revision, bind only to loopback with bearer authentication, run in a dedicated
workspace without human credential mounts, keep `execute_python` disabled, and
be described as an arbitrary-code surface if that tool is ever explicitly
enabled later.

Acceptance notes:
- MCP is not required to reproduce the final asset.
- The final accepted change is reflected back into the canonical Blender script
  or documented as a deterministic export step.
- The MCP process is not exposed publicly. For the explicitly requested
  wake-up demo it may remain in one named, owned tmux session with no
  auto-restart; its exact stop command and token-file location are documented.

### R4 — UE 5.3.2 import and scene composition

WHEN Blender outputs pass validation THEN the system SHALL import them through
the proven `UnrealEditor-Cmd -run=pythonscript`/Interchange path into a fresh
disposable project and compose a new `MMG040_Office_BlenderR1` map.

Acceptance notes:
- The imported asset has finite non-zero bounds, non-default material slots,
  collision, and correct centimetre-scale placement.
- The scene retains the cardboard box, stool, character, lighting, and spatial
  affordance needed by the VISTA sample, replaces the provisional chair with
  the generated ergonomic chair, and records PlayerStart capsule clearance.
- The source r7/r8 projects and archived Content remain byte-untouched.

### R5 — Interactive Unreal delivery

WHEN the final runtime starts THEN the system SHALL expose only loopback Studio,
MCP, Cirrus and SFU ports and provide a documented SSH tunnel for the user.

Acceptance notes:
- UE and MCP health are connected.
- Pixel Streaming decodes nonblank frames and receives keyboard/mouse input.
- The user can navigate/inspect the scene; at least one prop exposes a visible
  movable or physics-enabled interaction without requiring a new C++ plugin.

### R6 — Visual and deterministic acceptance

WHEN the scene is considered deliverable THEN the system SHALL retain fixed
overview, interaction, first-person, and detail views plus objective scene and
image metrics.

Acceptance notes:
- No missing textures, default checker material, gross floating, penetration,
  black frame, or basic-geometry placeholder in the hero view.
- Evidence includes Git SHA, Blender/script/output hashes, UE map/content paths,
  actor list, camera transforms, screenshot hashes, and health/input receipt.
- A visual critic may recommend repairs but deterministic checks remain
  authoritative for collision, transforms, hashes, and runtime connectivity.

### R7 — Bounded AI iteration

WHEN Claude Code or a visual critic is used THEN the run SHALL have an explicit
call, wall-clock, and cost/turn limit and SHALL stop on repeated no-progress.

Acceptance notes:
- At most two Claude-assisted iterations are allowed for this vertical slice.
- No agent may continue until an undefined "AAA" judgment passes.
- Fable 5/`ultracode` is used only if the installed CLI proves that exact
  model/profile exists; otherwise the run records the available model and does
  not silently mislabel it.

### R8 — Git and handoff

WHEN the slice is complete THEN source/spec/test changes SHALL be split into
coherent commits, validated, pushed to the user fork, and accompanied by a
handoff with the exact startup/tunnel commands and remaining admin gates.

## Edge Cases

- If user-space Blender cannot run, preserve the failed receipt and do not fake
  MCP readiness; use no generated asset until a real GLB/preview exists.
- If UE import fails or hangs, quarantine that disposable project and create a
  fresh one for a single bounded retry.
- If GPU 1 cannot host another runtime safely, preserve the existing demo and
  defer final UE launch rather than terminating an unowned process.
- If Claude authentication/model selection is unavailable, continue with the
  deterministic script pipeline and report that the optional AI lane was not
  exercised.

## Resolved runtime choices

- Claude uses the verified `fable` alias with `--effort max`; `ultracode` is a
  workflow trigger, not a separately selectable CLI model or effort value.
- Dedicated ports are Studio `3022`, SimWorld MCP `55582`, Cirrus `8615/8616`,
  SFU `8919`, and Blender MCP `8400`; all were free at preflight.

## Approval

- Requested by: yhliu
- Approved by: yhliu's 2026-08-11 instruction to create the goal and deliver
  the bounded working world by the next wake-up checkpoint.
- Date: 2026-08-11
