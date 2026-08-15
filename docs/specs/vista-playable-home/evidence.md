# Evidence: VISTA Playable Home

Status: Integrated game-only preview accepted; residual gates recorded
Updated: 2026-08-15

This ledger points to append-only generated artifacts without committing large
Blender or Unreal outputs to Git. A checked task means the stated acceptance
evidence has been retained; it does not imply that later runtime tasks passed.

## Contracts and Blender forge

- Run root:
  `/mnt/NAS2/yhliu/SimWorldStudio/vista-playable-home/runs/20260815T081201Z`
- Canonical build plan: `contracts/build-plan.json`
- Build-plan file SHA-256:
  `71a0796b3c1c6b9cf977bcc5a86089d94814c4f3f4aa434944b5ca717a515507`
- World content digest:
  `0987e540baa4d399538c49e58ffd9b3446c39e2b7d594bab75e819b184781fe0`
- HouseSpec digest:
  `b9674469c5ee7ca2315be1df7145dbef5527a38ee2a145f9f1e2ad5709b7ecb1`
- Blender: pinned Linux Blender 4.5.8 LTS, seed `20260815`.
- Independent outputs: `blender/build-a` and `blender/build-b`.
- Normalized manifest SHA-256, identical for both builds:
  `c4e1e63b64aa9957eefac64b8020be30bd9ae220a00b82a7dc94ac5b6f996b54`
- Normalized Blender content digest, identical for both builds:
  `333db58984fd22459f031728d8aaf5263805df2642225be5258ae5f9a5297492`
- Retained counts: 6 rooms, 34 semantic entities, 35 one-asset GLBs,
  12 room-bundle nodes, 154 primitives, and 38 materials.
- Both builds exited cleanly and retained an inspectable full-world GLB,
  source `.blend`, overview preview, and interior preview.
- Focused combined validation:
  `49 passed, 23 subtests passed` for contracts, compiler, Blender forge, and
  runtime-profile tests.

The normalized source graph and semantic content are deterministic. Blender's
generated GLB binary chunks and Eevee preview pixels are not claimed to be
bit-for-bit identical: their file sizes and normalized content agree, but
their media SHA-256 values differ between the two executions. Runtime
acceptance therefore uses the normalized manifest plus visual/Unreal probes,
not a false byte-equality claim for renderer output.

## High-detail visual binding

- Active run root:
  `/mnt/NAS2/yhliu/SimWorldStudio/vista-playable-home/runs/20260815T110115Z-navfix`
- Active build-plan SHA-256:
  `37bc046c9d81f8dda6e56681230681306d85ebc05912b42766866174ce62430e`
- Active build-plan content digest:
  `0c51140540ed6cb63dc47abdd8f6776de7b248a01d1f2f3cfff1a8695d198b03`
- Blender manifest SHA-256:
  `c20f701318e4cefdce231315e20848ddc1c7fcc9ea7ce54dde729a83d39d7f10`
- HSSD binding/attribution manifest SHA-256:
  `4d721c4dc8c1b15602987bd7404288d02d247ba83b68ab22c3ed14e4eb199227`
- The binding contains 23 closed-world principal assets with normalized
  geometry, embedded PBR materials/textures and exact upstream hashes. HSSD is
  retained as research/noncommercial demo content under CC BY-NC 4.0.
- T7A remains open: the assets are real and bound, but there is no retained
  fixed Unreal screenshot for every room, and the present visuals are not
  claimed photorealistic.

## Unreal build

- Accepted attempt:
  `ue/attempt-09-hssd-playable-input`
- Accepted pointer: `ue/accepted.json`.
- Build result SHA-256:
  `a4954dd61d8bec682bdd99beaf2c6a226d1e278c3e3f9fad116c64e0032f6569`
- Result content digest:
  `29cb3c4a0c996b7fba0288f113e925d8bc4eb19b56cde967d341c6ce1f5b3044`
- Execution manifest SHA-256:
  `75826d9c550aa24ba8213a27ad2538f323c4521a69c11b89380dd4c27e3e10fd`
- Scene receipt SHA-256:
  `55cbc6be3e8c8f53ee41d004a37a4ba9c858c851bab3a8de0e107c1fc009c6e0`
- Plugin package tree SHA-256:
  `05fef8b15f3accfe41db097fe6988c9b0f55f1db782a1ef7e608fe8056271628`
- Manny character tree SHA-256:
  `424aaf443954ce893d141fba833683be968a2ec0994862c7fc7cbc34c701b609`
- Persisted `DefaultInput.ini` SHA-256:
  `0c7ac04b205137b457a286231588ca984e2f78ca75d7310f4c33260ac357a6d6`
- Scene gates passed: saved, reloaded, semantic tags, PlayerStart, GameMode,
  NavMesh bounds, dynamic lighting, deterministic exposure and reflected input
  mappings. The attempt is not quarantined.
- Attempt 08 is deliberately retained as rejected evidence: import succeeded,
  but the original reflected-key verifier misread the UE key struct and failed
  closed rather than publishing an unverified build.

## Gameplay acceptance

- Accepted runtime attempt:
  `ue/attempt-09-hssd-playable-input/game-runtime/attempt-20260815T133111.731389Z-794633`
- Acceptance receipt SHA-256:
  `99fab796477465e300c678c2eda7b42bba384864b273ed350653697488adb409`
- Status is `accepted`, generation advanced exactly from 0 to 25, and all 32
  typed checks passed in one UE session.
- Proven behavior: living-room door open/inspect/close, NPC queued navigation
  and physical doorway crossing, keys pickup/inspect/place, plus start/status/
  reset for `mmg_001`, `mmg_044` and `mmg_045`.
- Retained input views in that attempt prove nonblack idle, mouse/W movement
  and Space jump. A later run through the installed Sunshine profile isolates
  keyboard translation without mouse rotation.
- T15 remains open because the retained protocol run covers one live door and
  same-room pickup/place only; it does not contain an uncut visual traversal
  through all six rooms, a cross-room carry or a second live door action.
- Known state deviation: `keys.place_tabletop_right` moved the actor to the
  correct right-anchor transform, but the response retained the baseline
  `values.placed_at` string for `tabletop_left`. The transform proof is valid;
  the stale semantic value must be fixed before T15 is accepted.
- Provenance limitation: the acceptance receipt bound the then-current
  `runtime-state.json`, but that state file is intentionally updated by the
  later stop lifecycle. The 32-step transcript remains self-contained and
  immutable; a follow-up harness revision should copy an acceptance-time state
  snapshot instead of binding a mutable lifecycle file.

## Sunshine/Moonlight handoff

- Live runtime attempt:
  `ue/attempt-09-hssd-playable-input/game-runtime/attempt-20260815T134359.458370Z-808721`
- Source commit at launch:
  `57fc8485097cd4514a9f223cfd8fffda3d8c3c87` (clean worktree).
- Launch-plan SHA-256:
  `dddec678f6d8300d75fee478ec4d97609c0e00a634ca8784d0d5c7618bb2e680`
- Sunshine profile SHA-256:
  `3cc9ff47b8e0f082bd4ee951073410d84a7ea0418d9a5fcd3cf2df171be00611`
- Input-proof SHA-256:
  `b6cb8f50572c7158e92b717dc4f7bb92c9a120697ddf6fffcc790efc57d627bd`
- XTest held W for one second in the game-only window. Idle-to-idle SSIM was
  `0.940250`; idle-to-after-W SSIM was `0.350643` at 1280x720, with a visible
  character translation. Screenshot hashes are bound by `input-proof.json`.
- Corrected final preflight SHA-256:
  `cf7e76ba95381ba3d47b4987e4780ad0b7a2e51c4fd16ced6231e57d3ba7c2cf`.
  It reports display `:117`, GPU 0, Tailnet IP `100.114.80.121`, UDP available,
  Sunshine listener reachable, `VISTA World` registered, preview/toolchain
  ready, and `moonlight_control_ready: false`.
- `preflight-final.json` is superseded and must not be used: its caller passed
  the `apps.json` file where a config directory was required. Append-only
  discipline preserved it; `preflight-final-corrected.json` is authoritative.
- The only current remote-control blocker is `moonlight_input_view_only`:
  `/dev/uinput` and `/dev/uhid` exist as root-only 0600 devices, so `yhliu`
  cannot inject a Moonlight keyboard, mouse or gamepad. Sunshine video capture
  is ready; control is not claimed ready.

Mac connection and lifecycle:

1. Join the same Tailnet, open Moonlight, add host `100.114.80.121`, and select
   the single `VISTA World` application. The game surface is the UE window, not
   Studio or Editor chrome.
2. After the administrator grants the two device permissions, controls are
   W/A/S/D, mouse look, Space jump, Left Shift sprint, C crouch, E interact and
   Q drop. Until then the stream is view-only.
3. Disconnecting Moonlight stops viewing; it is not treated as a scenario
   timeout. To explicitly stop only the owned server world, run:

```bash
cd /home/yhliu/SimWorld-Studio-worktrees/vista-playable-home
PYTHONPATH=tools uv run --offline --project tools python \
  tools/runtime/vista_playable_home/stop.py \
  --workspace /mnt/NAS2/yhliu/SimWorldStudio/vista-playable-home/runs/20260815T110115Z-navfix/ue/attempt-09-hssd-playable-input
```

## Remaining production gates

- Run and retain one end-to-end Studio/NLP request through the already
  implemented authenticated HTTP/MCP surface into the live typed Unreal lane.
- Retain an uncut six-room traversal, a second live door interaction and fixed
  review-camera screenshots for every room; include a cross-room carried item
  and correct the stale `placed_at` semantic value.
- Improve lighting, character presentation and room dressing before calling
  the scene photorealistic.
- Cook and smoke-test a Development executable. Toolchain preflight is ready,
  but no package receipt exists, so the current deliverable is correctly
  labeled `UnrealEditor -game` preview rather than packaged or Production.

## Final source validation

- Collaboration branch: `origin/codex/vista-playable-home` in
  `IvesLiu1026/SimWorld-Studio`; generated UE/Blender/runtime artifacts are not
  committed. Pull-request entry:
  `https://github.com/IvesLiu1026/SimWorld-Studio/pull/new/codex/vista-playable-home`.
- Python VISTA Playable Home suite while the demo remained live:
  `125 passed, 44 subtests passed`.
- Focused Node compile/route/service/MCP suite: `28 passed`.
- The Python suite initially exposed two command-plan tests that accidentally
  depended on live port `55620` being unused. They now mock availability only
  for those pure plan tests, while the occupied/reserved-port rejection remains
  explicitly tested.
