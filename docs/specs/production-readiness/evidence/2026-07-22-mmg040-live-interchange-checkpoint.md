# `mmg_040` live Interchange checkpoint

Date: 2026-07-22

Branch: `codex/semantic-production-adapter`

Pushed commits at this checkpoint:

- `ec5ed8dd` — claim the bounded `mmg_040` live runtime;
- `37283a8c` — inspect pinned `mmg_040` assets in live UE;
- `749a9121` — pin a real CC0 cardboard box;
- `90eeb3f8` — bound legacy UE inspection frames;
- `74524aa4` — add the typed pick-up/IK animation contract;
- `ef4d17d9` — record the live Interchange evidence;
- `c3b0fb27` — use the UE 5.3 overlap-event property API.

## Resource and mutation boundary

The user authorized this bounded live run. It used only a disposable local UE
project and NullRHI commandlets assigned to this work item. It did not bind a
public listener, call a paid model, write Postgres/Qdrant, modify the canonical
archived UE Content, touch VISTA Production port `8000`, or inspect/restart the
separate demo's GPU 1 and ports `3012/55570/8595/8596/8899`.

The first two scene-build commandlets inherited the project-enabled UnrealMCP
plugin and briefly started its default loopback listener on `55559`. No client
used that listener and it stopped with each commandlet. This was outside the
declared port set, so the successful third build explicitly disabled UnrealMCP
and PixelStreaming in its disposable project descriptor. The later rendered
probe enabled UnrealMCP only on the owned `55560` port.

The full successful import wrote only below the local disposable path:

```text
/home/yhliu/SimWorldStudio-live/0.2.0-806e869a/runs/
  ec5ed8dd4beb-mmg040-live-r1/disposable-project-r3/
  gym_citynav/Content/VISTA/External/PolyHaven
```

## Official archive observation

The live AssetRegistry bootstrap at:

```text
/home/yhliu/SimWorldStudio-live/0.2.0-806e869a/releases/
  ec5ed8dd4beb-mmg040-live-r1/evidence/ue-asset-bootstrap/
  official-minimal-26bdd2ca-live-r1
```

observed UE `5.3.2`, 1,350 registry rows and 402 object candidates. The bounded
seven-object inspection is at:

```text
/home/yhliu/SimWorldStudio-live/0.2.0-806e869a/releases/
  ec5ed8dd4beb-mmg040-live-r1/evidence/mmg040-object-inspection/live-r4
```

All seven packages loaded and spawned, and cleanup completed. The chair,
seat-table and two cart objects are real static meshes with material/bounds/
collision observations. The three `BP_Box` variants are collision helpers with
no visible mesh or material, so the overall seven-object receipt correctly did
not pass. Observation SHA-256:
`1d2d7f8084299640080dc9afe0880cc325d45d5077d4a0e64298b1c9039276ba`;
receipt SHA-256:
`ef7646329069cb6c37054d3d9306557ea45351482530634949348bd815e34d7f`.

## Pinned CC0 replacement bundle

The authorized Poly Haven v2 acquisition contains a cardboard box, painted
wooden stool and shelf: three assets, 15 files and 4,648,718 bytes. It is
stored at:

```text
/home/yhliu/.simworld/vendor-assets/vista-mmg-040-polyhaven-v2
```

Pins:

- source-manifest SHA-256:
  `f887de3303fc9ad513fb0c75172e8332d75f8e0dc29e2af9fd542972160ab5f2`;
- normalized tree SHA-256:
  `0d3858dc07a1cf77d9dd592a6eb897865f2fb7c3e4796a4f86d215ac4969ac36`;
- prepared import-job SHA-256:
  `d1e929b7466384b5661e999653889f3fd68fe8d06fc80887204cedcc867999b9`;
- preparation-receipt SHA-256:
  `ecc5815d2f313915fe1bfe2d62b38d697586bb7ab246205c532480db629c9897`.

The prepared job remains an input contract; it is not itself import evidence.

## Transport finding

A single fixed `AssetImportTask` sent through the Studio TCP Python bridge
reached `LogInterchangeEngine: Interchange start importing source` but did not
return and wrote no package within five minutes. The host timed out, did not
retry, terminated only its own stuck UE process, and quarantined that
disposable project. The preserved log is:

```text
/home/yhliu/SimWorldStudio-live/0.2.0-806e869a/runs/
  ec5ed8dd4beb-mmg040-live-r1/nullrhi-writable-r2/
  ue-user/Saved/Logs/gym_citynav.log
```

This establishes `transport_ambiguous` for the editor-main-thread socket
route. It does not establish a failed source asset or failed Interchange
importer.

The same cardboard-box task executed by
`UnrealEditor-Cmd -run=pythonscript` completed in about 0.6 seconds after
Python startup and wrote five packages. A fresh all-three commandlet then
completed with process exit code 0, one synchronous task and one bounded
inventory per asset, and wrote 15 packages. Commandlet script SHA-256:
`8cadcbe950d6c114bf93f4b6931e8f168f7632e94fe8bdeb740c9e2797570c6d`;
UE log SHA-256:
`7f167c9821e2d209a6825622f6d1a05f3e05910320d1d2983fb033248834301c`.

The operator-summarized live receipt is:

```text
/home/yhliu/SimWorldStudio-live/0.2.0-806e869a/releases/
  ec5ed8dd4beb-mmg040-live-r1/evidence/ue-interchange-execution/
  commandlet-v2-r1/observation-receipt.json
```

Its SHA-256 is
`ec473e1be15613bf857702b829c39edad63b3292c5b69db6699a3959d51bf0d3`.
It records three static meshes, three non-default material slots, nine texture
assets, finite nonzero bounds, one convex simple-collision element per mesh,
sRGB base color, non-sRGB packed maps and normal maps, `TC_NORMALMAP`, and
OpenGL normal green-channel flipping. The 15-package aggregate digest is
`2a8b9d3f6dd341c7570f31fb1222cbe62ee6ed4067fea50303bd34fc2b0f9ff1`.

## Gates that remain open

Immediate NullRHI `MaterialEditingLibrary.get_used_textures()` returned an
empty list even though each saved material package contains the three exact
texture object paths and Interchange glTF BaseColor/MetallicRoughness/Normal
material-function inputs. A later read-only dependency commandlet encountered
NAS `rpc_wait` during UE startup and its 300-second watchdog terminated it
before Python ran. It made no mutation and was not retried.

Therefore this checkpoint does **not** claim any of the following:

- a sealed Interchange pipeline/config/plugin fingerprint;
- independently validated material-to-texture dependency edges;
- rendered scale, floor/contact, collision or PBR appearance;
- Production-ready imported content;
- semantic-index eligibility or a Postgres/Qdrant write.

The rejected loopback executor prototype was not committed. Independent review
found that it lacked durable write-ahead intent, authenticated UE identity,
partial/timeout quarantine and full dependency-at-use verification. A correct
commandlet executor must close those defects before this one-off observation
becomes a repeatable execution contract.

## Deterministic scene-build observation

Three non-reused disposable projects preserve the UE 5.3 compatibility path:

- r5 failed before save because `StaticMeshComponent` has no
  `set_generate_overlap_events` method;
- r6 passed that point but failed before save because
  `SkyLightComponent.intensity_scale` is not exposed through the UE 5.3
  Python reflection surface;
- r7 used `set_editor_property("generate_overlap_events", ...)`,
  `SkyLightComponent.set_intensity()`, and disabled runtime networking
  plugins. It completed with process exit 0 and UE reported zero errors and
  zero warnings.

r7 saved a real 23,886-byte map at
`/Game/VISTA/Scenes/MMG040_Office_CommandletR3`. Its SHA-256 is
`afa9ecddf4133a443080827922686b44b4f61bd28418d087da378d429d7bfd14`.
The marker contains 14 actors: ground and two walls, the official chair, the
three imported Poly Haven meshes, the pinned third-person character,
PlayerStart, camera, three lights and sky atmosphere. Binary strings in the
saved map independently retain all five non-basic asset references.

The append-only scene-build receipt is:

```text
/home/yhliu/SimWorldStudio-live/0.2.0-806e869a/releases/
  ec5ed8dd4beb-mmg040-live-r1/evidence/ue-scene-build/
  commandlet-r3-r1/observation-receipt.json
```

Its SHA-256 is
`1689f72e1f88205edb17d8056ddec7cb27d135f62542619a232435cf2f90025b`.
This proves a saved machine scene, not rendered correctness or Production
readiness.

## GPU 0 render preflight

A fresh r8 copy retained the exact map digest, enabled UnrealMCP only for the
owned `55560` port, and attempted an offscreen 1280×720 Vulkan editor render
on GPU 0. UE failed before MCP startup with
`vpCreateInstance ... VK_ERROR_INCOMPATIBLE_DRIVER` and exit code 139, so no
screenshot was created and r8 is quarantined.

The host simultaneously showed NVIDIA driver `590.48.01` and an idle RTX
A6000, but the current user cannot read or write either
`/dev/dri/renderD128` or `renderD129` and is not in the `render` group.
That permission gap is the first host issue to fix; it is not proof that no
additional UE 5.3/Vulkan compatibility issue exists. The append-only failed
render receipt is:

```text
/home/yhliu/SimWorldStudio-live/0.2.0-806e869a/releases/
  ec5ed8dd4beb-mmg040-live-r1/evidence/ue-scene-render/
  editor-r1/observation-receipt.json
```

Its SHA-256 is
`170d6f82acf9a7839c162dd369825d3624b74539562970c9420587484a831c2f`.

## Animation checkpoint

Commit `74524aa4` adds fixed `pick_up` semantics:
`vista_pick_up_ik_v1`, upper-body IK, object attachment, a required
hand-contact target and the exact `vista_pick_up_attached` completion signal.
Its counterfactual contract slice runs look-at at 0 seconds, pick-up at 2,
pause at 5, fall at 9 and a terminal checkpoint at 12; focused tests pass
27/27. No live character was mutated. The exact pawn class and C++/Blueprint
adapter implementation, IK contact evidence, fall montage and rendered
12-second run remain open.

## Next authoritative actions

1. Have an administrator add `yhliu` to `render` (or install an equivalent
   persistent udev ACL), start a new login session, and re-run Vulkan preflight.
2. Use a fresh disposable project—not r8—to render the saved map on GPU 0 and
   capture scale/contact/collision/PBR screenshots.
3. Land the commandlet-only executor only after independent review confirms
   full execution-influence sealing, mandatory independent pins, bounded
   process-group cleanup, durable project quarantine and terminal-receipt
   finalization.
4. Re-run the three-asset job once in another fresh disposable project through
   that audited executor; do not reuse or promote the probe projects.
5. Only a visually accepted object manifest may enter a generation-isolated
   semantic snapshot build; Postgres/Qdrant stay unchanged until then.
