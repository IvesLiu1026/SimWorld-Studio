# VISTA Playable Home Runtime

This package owns a game-only Unreal preview and a deterministic Sunshine app
entry. It does not modify the existing Blender-world or NLP-demo runtimes.

The preview command deliberately uses `-game` and a visible X11 display; it
does not use `-RenderOffScreen`. GPU 1 and the existing VISTA ports are refused.

Start with the read-only host report:

```bash
uv run --project tools python \
  tools/runtime/vista_playable_home/preflight.py \
  --ue-editor /absolute/Engine/Binaries/Linux/UnrealEditor
```

Before running a real map, inspect the fixed command:

```bash
uv run --project tools python \
  tools/runtime/vista_playable_home/launch.py \
  --workspace /absolute/new-run/ue/attempt-01 \
  --project /absolute/new-run/ue/attempt-01/project/Home.uproject \
  --ue-editor /absolute/Engine/Binaries/Linux/UnrealEditor \
  --map /Game/VISTA/PlayableHome/r1/Maps/VistaPlayableHome \
  --display :117 --gpu 0 --vista-world-port 55620 --preflight-only
```

The game process binds only its fixed typed `vista_world_action` listener on
the selected loopback port. The launcher refuses ports owned by existing VISTA
runtimes and never exposes a Python, console, or caller-selected command lane.

`sunshine_app.py` prints a plan by default. `--apply` creates a timestamped
backup before replacing `apps.json`; Sunshine must then be restarted by its
runtime owner. Do not apply the entry until the referenced profile and map
exist.
