# 2026-07-21 `mmg_040` animation content-driver code audit

## Scope

Offline/source-only implementation and validation of the project-owned `mmg_040` animation profile.
No Unreal process was started, no project/content file was written, no asset was downloaded, and no
provider, database or public network endpoint was called.

## Repository findings

- `packaging/simworld_arena` pins a UE 5.3 Third Person demo Blueprint and two dependency-tree digests,
  but it contains no `.uproject`, plugin binary, or `mmg_040` animation content.
- The verified official minimal archive has plausible retarget sources (Manny, LiftSet, fall loop, IK /
  Control Rig filenames), but no live AssetRegistry/load/skeleton-compatibility receipt. They remain
  source candidates, not executable profile bindings.
- Existing `VistaAnimationContentApi` `1.0.0` provided the four-command protocol and abstract driver.
  Its historical UE 5.7.3 binary predates this change and cannot attest the new source.

## Implemented boundary

- Plugin source version advanced to `1.1.0`.
- Added `FVistaMmg040ContentDriver`, a concrete `IVistaAnimationContentDriver` policy implementation.
- Added a narrow `IVistaMmg040ProjectBackend` with seven distinct typed start methods. No method accepts
  a content path, function/class name, script, console command, Python or generic action string.
- Subsystem configuration now requires `ValidateTrustedProfile`; a driver receipt mismatch prevents the
  Content API from entering configured state.
- Added a byte-pinned source contract for 13 project-owned assets and seven fixed actions under
  `/Game/VISTA/MMG040/`.
- Pinned the exact production parameter variant for each action to the existing server defaults. The
  first revision permits only forward fall/recover; other directions require a new profile revision
  and variant-specific evidence.
- Added strict source/inspection schemas and a non-mutating profile helper. It rejects duplicate JSON
  keys, path aliases, symlinks, hard links, weakly protected files, arbitrary `/Game` substitutions,
  skeleton/class/package digest mismatch, notify mismatch, root-motion mismatch, behavior mismatch and
  noncanonical content digest.
- Each behavior receipt now carries the exact observed live-check set and verified parameter object;
  both enter the canonical content digest. Runtime completion requires the backend-observed signal plus
  an immutable completion evidence ID/SHA contained in the evidence list; no timer/config synthesis is
  accepted.
- Mutation handles are allocated before Start from a random driver-instance GUID namespace plus a
  monotonic, deterministic-budget sequence. Backend false-return is explicitly atomic/no-side-effect;
  invalid successful output is action-typed rolled back, while rollback uncertainty quarantines the
  process/slot for trusted reconciliation and restart without exposing an unusable handle to the wire
  caller.
- Required-target evidence capture is bound to the exact action-target capability/anchor pair accepted
  by preflight, so a target verified only for gaze cannot be reused as drag interaction evidence.
- Without a live receipt, helper preflight exits `3` and reports `ready=false`,
  `start_allowed=false` with explicit reason codes.

Known cross-owner gap: the current server-side `inspectVistaAnimationUePluginSource` inventory still
checks only the descriptor, Build.cs and two module entry files. Its `source_tree_complete=true` does
not prove that the subsystem, strict JSON parser, concrete driver, source profile or receipt schemas
are present. The plugin-focused offline test checks those additional files directly, but the server
readiness owner must expand the production inventory before treating that flag as deployment evidence.

Pinned source-contract SHA-256:

```text
1b0aa6e48d251cb8dbeac4f34528ca8fa6084fb330fc2d150ef341f630528b1c
```

## Validation performed

```text
node --test offline-contract.test.mjs mmg040-content-profile.test.mjs
22 passed, 0 failed

node --test vista-animation-ue-readiness.test.js vista-animation-ue-adapter.test.js vista-animation-runtime.test.js
55 passed, 0 failed

node --check Scripts/prepare-content-profile.mjs
passed

sh -n Scripts/install-plugin.sh
passed

sh -n Scripts/build-plugin.sh
passed

JSON Schema Draft 2020-12 meta-validation
2 schemas valid; source contract and representative receipt instances valid

clang-format --dry-run --Werror (new concrete driver source/header)
passed

git diff --check
passed
```

These checks are offline contract evidence only. The C++ revision was not compiled against UE in this
audit.

## Remaining live gates

1. Author/import all 13 pinned `/Game/VISTA/MMG040/...` assets.
2. Implement the project backend against real bindings, montages, Control/IK Rig, physics and evidence
   capture without reflection/generic execution.
3. Rebuild/package plugin `1.1.0` with exact UE 5.3.2 and pin the new loaded binary SHA/build ID.
4. Exact-dispatch the four reserved commands from the private listener and pass live nonce/capability
   challenge.
5. Expand the server-owned plugin source inventory beyond its current four-file minimum and bind the
   complete inventory/package digest to the build receipt.
6. Produce a protected live inspection receipt and derived server content profile.
7. Run disposable normal/timeout/Stop/disconnect/restart tests and `mmg_040` 0/2/5/9/12-second pose,
   contact, screenshot and scene-validation evidence.

Until those gates pass, external status remains `not_ready`; this audit does not claim that character
animation, IK, fall/recover or the 12-second timeline executed in UE.
