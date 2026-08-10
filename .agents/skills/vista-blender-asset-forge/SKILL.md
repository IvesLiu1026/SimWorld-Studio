---
name: vista-blender-asset-forge
description: Build, validate, and hand off reproducible Blender assets for VISTA or SimWorld Unreal scenes. Use when a SceneSpec has an unresolved 3D asset gap, when generating .blend or GLB deliverables, when preparing procedural room or prop kits for Unreal Engine, or when an optional Blender MCP session must be operated safely.
---

# VISTA Blender Asset Forge

Create detailed assets as deterministic Blender source code, then export and validate them before Unreal import. Treat interactive MCP edits as exploratory: reproduce every accepted edit in the canonical script.

## 1. Preflight the workspace

- Read the repository agent rules and active-work registry before editing.
- Claim only new asset scripts, tests, and append-only run directories.
- Locate Blender explicitly; prefer `BLENDER_BIN`, then the repository default `/home/yhliu/.local/opt/blender-4.5.8-linux-x64/blender`.
- Verify Blender with `"$BLENDER_BIN" --background --factory-startup --version`.
- Keep source and tests in git. Put `.blend`, GLB, renders, and receipts in an append-only run directory outside git.
- Record Blender version, script commit/hash, seed, dimensions, units, output hashes, and license/provenance in the manifest.

## 2. Choose retrieval or generation

Search the semantic asset index first when a suitable licensed asset exists. Procedurally generate only the unresolved gap or when repeatable geometry is a requirement. Never silently replace a requested real asset with an untextured Cube.

For VISTA SceneSpecs:

1. Resolve the scene dimensions and semantic role of each asset.
2. Reuse verified, licensed assets for people and commonplace props when available.
3. Generate architectural shells, fitted furniture, occluders, and task-specific props whose exact dimensions matter.
4. Preserve provenance per asset; do not label generated assets as Poly Haven or another external source.

## 3. Build canonically in headless Blender

Use the repository wrapper:

```bash
tools/blender/run_vista_blender_build.sh "$RUN_ROOT"
```

The positional argument is the new append-only run directory; the wrapper writes the bundle beneath `$RUN_ROOT/blender` and uses the canonical seed `4040`. The build must run from `--factory-startup` without a display. Keep scene creation in `tools/blender/build_vista_mmg040_office.py`; do not make the `.blend` file the only editable source.

Model with these conventions:

- Blender units are meters; Unreal units are centimeters. Record conversion explicitly and test a known dimension after import.
- Use Z-up and apply transforms before export. Avoid negative scale and unapplied object scale.
- Give every object and material a stable semantic name. Group exportable parts into named collections.
- Use an intentional three-level detail hierarchy: primary silhouette, construction details, and small wear/edge details.
- Add bevels and weighted normals where they materially improve highlights; avoid dense geometry that is invisible at the target distance.
- Use physically plausible roughness, metallic values, and scale. Prefer procedural or repository-owned textures; record any external texture license and checksum.
- Separate movable/physics props from static room geometry.

Export GLB with materials, normals, tangents, and stable object names. Save the canonical `.blend` and render at least two fixed-camera previews with a neutral color-management setup.

## 4. Validate before Unreal

Run both structural and visual checks:

```bash
uv run --project tools python tools/blender/validate_vista_asset.py \
  "$RUN_ROOT/blender/manifest.json" --json
uv run --project tools --with pytest pytest -q \
  tools/tests/test_vista_blender_asset.py
```

Require all of the following:

- `.blend`, GLB, previews, and manifest exist and have nonzero size.
- The manifest hashes match the files on disk.
- GLB geometry and materials are present, stable names are retained, and its bounding box matches the expected metric dimensions.
- No placeholder objects, missing materials, external absolute paths, or unexplained generated dependencies remain.
- Fixed-camera previews show a coherent silhouette, contact with the floor, readable materials, and no clipping or black render.

Inspect previews at full resolution. If a result fails, change the canonical script and rebuild into a new append-only run; never overwrite evidence from a prior run.

## 5. Hand off to Unreal

- Import into a fresh disposable project, never a canonical seed or archived run.
- Use `/Game/VISTA/External/Procedural/<AssetVersion>` as the content namespace.
- Verify the import receipt against the GLB and manifest hashes before composing the map.
- Confirm scale, axes, material slots, collision, lightmap behavior, and movable/static classification.
- Save a new map name. Keep the source seed map immutable.
- Produce an import receipt, map-build receipt, fixed screenshots, and a smoke-test result for navigation plus one interaction.

## 6. Use Blender MCP only as a quarantined refinement lane

Third-party Blender MCP servers and add-ons can execute arbitrary Python in Blender. Do not run an unpinned package or install from a moving branch.

- Pin and record the exact source commit and inspect the server and add-on before execution.
- Bind only to loopback and use an isolated Claude MCP config with `--strict-mcp-config`.
- Run without repository, SSH, cloud, database, or model-provider secrets in the process environment.
- Disable telemetry and remote asset-provider integrations unless explicitly authorized.
- Prefer a filesystem sandbox and a disposable Blender profile; grant write access only to the current run directory.
- Do not expose the MCP socket publicly or accept arbitrary remote clients.
- Limit AI refinement to a declared number of iterations. Capture prompts and outcomes without secrets.
- Reimplement every accepted MCP edit in the headless build script, rebuild, and validate from scratch.

If the MCP package cannot be traced to a trustworthy pinned source, report it as a security gate and continue with the headless workflow.

## 7. Completion contract

Finish only when another agent can reproduce the asset from a clean checkout using the documented Blender binary, script, seed, and command; independently verify hashes and dimensions; import it into a disposable UE project; and inspect both build and interaction evidence. Commit only source, tests, manifests or small receipts intended for git, and the handoff documentation. Do not commit generated binaries, credentials, caches, or mutable runtime logs.
