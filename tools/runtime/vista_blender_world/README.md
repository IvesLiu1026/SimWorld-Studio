# VISTA Blender World Runtime

This launcher owns one disposable UE project, GPU 1, and only the dedicated
loopback ports `3022/55582/8615/8616/8919`. It refuses project paths outside
the supplied workspace and any path containing canonical, archive, r7, or r8
markers. It never stops or probes process identities by name, so the existing
GPU 1 demo is outside its cleanup scope.

The Cirrus directory must already contain the reviewed
`VISTA_LOOPBACK_PATCH_V1` and its patch receipt. An unpatched Epic Cirrus tree
is rejected because its config alone is not sufficient proof of loopback bind
and player authentication.

For the packaged UE 5.3.2 compatibility UI, patch a fresh disposable legacy
Studio copy before preflight. This accepts only the pinned input bytes and
produces the exact player bytes enforced by the launcher:

```bash
uv run --project tools python tools/runtime/vista_blender_world/patch_legacy_player.py \
  --studio-workspace "$ATTEMPT/studio-workspace" --apply
```

## Preflight (does not start services)

```bash
uv run --project tools python tools/runtime/vista_blender_world/launch.py \
  --workspace /mnt/NAS2/yhliu/SimWorldStudio/vista-blender-world/runs/RUN_ID \
  --project /mnt/NAS2/yhliu/SimWorldStudio/vista-blender-world/runs/RUN_ID/ue/project/gym_citynav.uproject \
  --map /Game/VISTA/Scenes/MMG040_Office_BlenderR1 \
  --ue-editor /ABS/UE/Engine/Binaries/Linux/UnrealEditor \
  --cirrus-dir /ABS/UE/Engine/Plugins/Media/PixelStreaming/Resources/WebServers/SignallingWebServer \
  --studio-workspace /mnt/NAS2/yhliu/SimWorldStudio/vista-blender-world/runs/RUN_ID/studio-workspace \
  --nvidia-icd /ABS/runtime/nvidia-headless-icd.json \
  --nvidia-compat /ABS/runtime/nvidia-compat \
  --preflight-only
```

`studio-workspace` is a disposable copy of `simworld_studio_workspace`, kept
under the run root because the current Studio backend writes its logs,
screenshots, sessions, and MCP config relative to that tree. Pointing the
launcher at the source checkout is intentionally refused.

Remove `--preflight-only` to launch. The safe default is
`STUDIO_MODEL_MODE=off`; an explicitly approved NLP lane additionally uses
`--model-mode live --coding-agents --claude-bin /ABS/claude --claude-model fable`.
The access token is generated under the runtime workspace with mode `0600` and
is never printed or written to receipts.

## Verify and stop

```bash
uv run --project tools python tools/runtime/vista_blender_world/verify.py \
  --workspace /mnt/NAS2/yhliu/SimWorldStudio/vista-blender-world/runs/RUN_ID

uv run --project tools python tools/runtime/vista_blender_world/stop.py \
  --workspace /mnt/NAS2/yhliu/SimWorldStudio/vista-blender-world/runs/RUN_ID
```

The Mac reaches the single UI endpoint through SSH; the Unreal and Cirrus
control ports remain server-loopback only:

```bash
ssh -N -L 3022:127.0.0.1:3022 yhliu@SERVER
```

Open `http://127.0.0.1:3022/?token=...` using the token obtained through the
already-authenticated SSH session. Do not paste the token into chat or logs.

If UE rejects a `PlayerStart`, run the fixed read-only diagnostic. It reports
the pawn capsule and intersecting static-mesh bounds for a bounded candidate
list; it cannot move actors, save maps, or execute arbitrary Python:

```bash
PYTHONPATH=. uv run --project tools python tools/runtime/vista_blender_world/live_qa.py \
  diagnose-start --workspace /mnt/NAS2/yhliu/SimWorldStudio/vista-blender-world/runs/RUN_ID
```

`stage-start-candidate` can then move the one fixed `VISTA_PlayerStart` to the
reviewed clear candidate in editor memory for a bounded trial. It refuses to
run during PIE and deliberately does not save the map; the accepted location
must still be rebuilt by the commandlet for final evidence.

For final keyboard proof, retain a Playwright trace plus grounded `state`
receipts immediately before and after the bounded key press, then correlate
them without granting the browser any UE control token:

```bash
PYTHONPATH=. uv run --project tools python tools/runtime/vista_blender_world/verify_browser_input.py \
  --workspace "$ATTEMPT" --before-receipt "$BEFORE" --after-receipt "$AFTER" \
  --browser-trace "$ATTEMPT/evidence/browser-input-trace.zip"
```
