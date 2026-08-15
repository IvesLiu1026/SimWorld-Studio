# Evidence: VISTA Playable Home

Status: Sealed Linux Development package accepted; remote video ready and
administrator input blocker recorded
Updated: 2026-08-16

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
- Accepted fixed-camera attempt:
  `ue/attempt-10-placement-cross-room/review-cameras/attempt-07-proof-handshake`
- Review-capture receipt SHA-256:
  `51a77f4973534ce5fe29f75f905d4b7264a6d0479722b65d7cce7371856c2e33`
- Six distinct 1280x720 RGBA PNGs cover entry hall, living room,
  kitchen/dining, bedroom, office and bathroom/laundry. Every worker reloaded
  the exact map, revalidated the complete six-CameraActor semantic-tag set,
  matched its selected transform/FOV, and produced one native HighResShot.
  The host strictly decoded every PNG, proved nonblank pixels, copied with
  `O_EXCL`, re-hashed native/final byte equality and wrote the aggregate
  receipt only after all six passed.
- T7A remains open on presentation quality, not evidence availability. Visual
  inspection shows the fixed cameras are frequently dominated by walls or
  occluding furniture and the lighting/material presentation remains closer
  to a functional blockout than a photorealistic interior. The receipt is a
  truthful all-room technical baseline, not a claim of finished realism.

## Unreal build

- Accepted attempt:
  `ue/attempt-10-placement-cross-room`
- Build result SHA-256:
  `1b4853547bfa6ebd6d62ca2f1243ae2f74acdf67bfdfb11aa969c3013ccf1a2f`
- Result content digest:
  `32f4064dfdf0b1945b23f7630d010ed3e6777d78358351e804d1d311a35e9e37`
- Execution manifest SHA-256:
  `5eb4a0f9410a0f351599c5f8bf6caa8a59f716efdbf19189b909f276273b2208`
- Scene receipt SHA-256:
  `7e7997e8346e4c1c600131c3419600d5d4508039e1b733445e5718315adaea34`
- Materialized map SHA-256:
  `6533ae772d136a93e09adb923a3b7a864219fa51bec48d9d3ab9195f9b89a1f9`
- Plugin package tree SHA-256:
  `05fef8b15f3accfe41db097fe6988c9b0f55f1db782a1ef7e608fe8056271628`
- Manny character tree SHA-256:
  `424aaf443954ce893d141fba833683be968a2ec0994862c7fc7cbc34c701b609`
- Persisted `DefaultInput.ini` SHA-256:
  `b22db9229bbb2c18160197f51400f3bf171951780e58661d8dccf4dc99f405f3`
- Scene gates passed: saved, reloaded, semantic tags, PlayerStart, GameMode,
  NavMesh bounds, dynamic lighting, deterministic exposure and reflected input
  mappings. Attempt 10 also contains the corrected portable placement anchors
  and second-door/cross-room acceptance path. The attempt is not quarantined.
- Attempt 08 is deliberately retained as rejected evidence: import succeeded,
  but the original reflected-key verifier misread the UE key struct and failed
  closed rather than publishing an unverified build.

## Gameplay acceptance

- Accepted runtime attempt:
  `ue/attempt-10-placement-cross-room/game-runtime/attempt-20260815T143001.463678Z-847049`
- Acceptance receipt SHA-256:
  `fa8aaf966026147c2fd44fb50c34c6e160b63a34bfd14fa6114b0a90baa1ddb5`
- Status is `accepted`; all 29 typed checks passed in one UE session with exact
  generation accounting.
- Proven behavior: one closed living-room door opened and inspected; the
  initially open office door was independently inspected and then closed
  after the crossing. Keys were picked up and placed at the correct right-side
  coffee-table anchor. The NPC physically moved from the hall into the office,
  carried the keys across the room boundary and placed them on the office desk.
  `mmg_001`, `mmg_044` and `mmg_045` each passed start/status/reset.
- The placement response now retains the correct
  `values.placed_at=.../anchor.tabletop_right`; the earlier attempt-09 stale
  value is superseded rather than hidden.
- T15 remains open only for an uncut visual player traversal through all six
  rooms. Door, carried-object, moving-NPC, event, state-receipt and local input
  behavior are otherwise retained.

## Sealed Linux Development package

- Accepted package attempt:
  `ue/package-linux-development/attempt-04-no-afs-clean`
- Package receipt SHA-256:
  `c7dcd0bea0c2cb0de8f874857add910acfeca43af4caaf28295210c224734787`
- Archive tree SHA-256:
  `b48743bd949c0256696344d4c1a60b75209d9f2d096781a5832554b7379f6bcb`
- Packaged ELF SHA-256:
  `ce0761a8b702cb5aed6f857490be771ea09fcf7b6f2a5d8dd60f8449171e2837`
- Archive launcher SHA-256:
  `659f82ef64e36e052f6011b542b1d8e9b3ff05b900c44585cbfad76aa7809ef2`
- PAK SHA-256:
  `419c823aab85dcc10ce60e7fc948af0bdb5690184b73f648cf603c89a8cec43e`
- Pinned UnrealPak SHA-256:
  `70f02f3ed3d3ac45b740830b16c7af96c4bcb0e8302795031c76f95d36caff84`
- NullRHI smoke receipt SHA-256:
  `f2fa62f30c9192bd4e0359ce04f89446ae18f3e4468b448913ecb79bd5775386`
- RunUAT completed Build, Cook, Stage, Package and Archive for Linux
  Development. The archive contains 34 files and 1,080,625,414 bytes. Smoke
  reached typed `READY` on isolated loopback port `55621`, proved listener
  ownership and retained identical pre/post archive hashes.

## Sunshine/Moonlight handoff

- Package-bound profile SHA-256:
  `a3ced97d68c2701baf009ddbe97680bbb38c7a86a141342006a8d12f85d6f43b`
- Installed Sunshine `apps.json` SHA-256:
  `96de56d61ead83ee0181df56fe56e6c2219c1ddbb40d805c38b2a53e6296fdfe`
- Pre-install backup SHA-256:
  `372809fd03427587ccdeb0248d27bbe72b5ce8c9adb8b32961978f05926e342c`
- Enabled `vista-sunshine.service` unit SHA-256:
  `08c7367802e8f916f3e992ad50fb4144c96ad8ed523ea49c50ae129cf8228b54`
- Accepted packaged runtime attempt:
  `ue/package-linux-development/attempt-04-no-afs-clean/game-runtime/attempt-20260815T153119.999197Z-955364`
- Immutable typed-READY snapshot SHA-256:
  `159b8ab515e3bd9998055fa8c7ef4da3ddcea0eba9c2e91f4d459b87aa1ed5ae`
- Launch-plan SHA-256:
  `c4303a3d1badc8985e4cb1bc9d7c39a8727034f073e01a728e91e0676ba80cec`
- Package-live preflight SHA-256:
  `fde07ef16a55baa975afcb7b75b27af0da5c60adc7a80580d36a7f776f6b23ca`
- The supervisor re-hashed the full archive before spawn and after typed
  `READY`, launched the packaged ELF directly on GPU 0/display `:117`, and
  proved ownership of loopback port `55620`. It did not invoke UnrealEditor,
  a `.uproject`, the archive shell launcher or `-game`.
- Retained local XTest evidence is a render/input smoke, not a Moonlight input
  claim. At 1280x720, idle-before to idle-baseline SSIM was `0.939224` and
  idle-baseline to after-W SSIM was `0.506581`; the player visibly moved and
  an NPC was visible. The immutable images are bound inside the packaged
  runtime attempt.
- Sunshine is enabled and active as a user service, listens only on Tailnet IP
  `100.114.80.121`, and initialized NVENC H.264/HEVC capture. The packaged live
  preflight reports every package/display/network/service gate ready and the
  single blocker below.
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
  --workspace /mnt/NAS2/yhliu/SimWorldStudio/vista-playable-home/runs/20260815T110115Z-navfix/ue/package-linux-development/attempt-04-no-afs-clean
```

The earlier `UnrealEditor -game` attempt and its profile remain retained only
as rollback evidence. The current `VISTA World` entry is package-bound.

## Remaining production gates

- Run and retain one end-to-end Studio/NLP request through the already
  implemented authenticated HTTP/MCP surface into the live typed Unreal lane.
- Retain an uncut visual player traversal through all six rooms. The second
  door, cross-room carried item and corrected `placed_at` value are already
  accepted in attempt 10.
- Reframe the six review cameras and improve lighting, character presentation
  and room dressing before calling the scene photorealistic.
- Obtain persistent user access to `/dev/uinput` and `/dev/uhid`, then restart
  the Sunshine user service and retain a real Moonlight input proof.

## Final source validation

- Collaboration branch: `origin/codex/vista-playable-home` in
  `IvesLiu1026/SimWorld-Studio`; generated UE/Blender/runtime artifacts are not
  committed. Pull-request entry:
  `https://github.com/IvesLiu1026/SimWorld-Studio/pull/new/codex/vista-playable-home`.
- Source commits use explicit staging; the final push is verified by matching
  local `HEAD` to the remote branch rather than assuming a successful upload.
- Python VISTA Playable Home suite with the final package and sequential
  capture code: `171 passed, 46 subtests passed`.
- Focused Node compile/route/service/MCP suite: `28 passed`.
- The Python suite initially exposed two command-plan tests that accidentally
  depended on live port `55620` being unused. They now mock availability only
  for those pure plan tests, while the occupied/reserved-port rejection remains
  explicitly tested.
