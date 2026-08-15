# VISTA Playable Home Runtime

This package owns both the historical game-only Unreal preview and the sealed
Linux Development package used by the current Sunshine application. It does
not modify the existing Blender-world or NLP-demo runtimes.

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
  --map /Game/VISTA/PlayableHome/vista_playable_home_r1/Maps/VistaPlayableHome \
  --display :117 --gpu 0 --vista-world-port 55620 --preflight-only
```

The game process binds only its fixed typed `vista_world_action` listener on
the selected loopback port. The launcher refuses ports owned by existing VISTA
runtimes and never exposes a Python, console, or caller-selected command lane.
It remains in `starting` until a non-mutating typed status handshake proves the
exact world revision and initial generation; a merely-live process or occupied
port is never reported as ready.

Each launch is retained under `game-runtime/attempt-<UTC>-<pid>`. A private
`game-runtime/current.json` pointer lets the stop command target only the
current process identity, so a stopped Sunshine application can be launched
again without deleting or overwriting earlier evidence.

`sunshine_app.py` prints a plan by default. `--apply` creates a timestamped
backup before replacing `apps.json`; Sunshine must then be restarted by its
runtime owner. Do not apply the entry until the referenced profile and map
exist.

## Sealed Linux Development package

`packaged_profile.py` accepts only an `accepted` package receipt and re-hashes
the full archive, executable, PAK, pinned UnrealPak and NVIDIA ICD before it
writes a mode-0600 profile. `packaged_entrypoint.py` then launches the packaged
ELF directly. It never invokes UnrealEditor, a `.uproject`, the archive shell
launcher, or `-game`.

```bash
ATT=/absolute/package-linux-development/attempt-01
PROFILE="$ATT/sunshine-profile-packaged-accepted.json"

uv run --offline --project tools python \
  tools/runtime/vista_playable_home/packaged_profile.py \
  --package-attempt "$ATT" \
  --package-receipt-sha256 <receipt-sha256> \
  --nvidia-icd /usr/share/vulkan/icd.d/nvidia_icd.json \
  --output "$PROFILE"

PROFILE_SHA=$(sha256sum "$PROFILE" | awk '{print $1}')
/usr/bin/python3 \
  tools/runtime/vista_playable_home/packaged_entrypoint.py \
  --profile "$PROFILE" --profile-sha256 "$PROFILE_SHA"
```

The packaged supervisor fixes GPU 0, display `:117`, loopback port `55620`,
the r1 map and 1280x720 at 60 FPS. It re-hashes the archive immediately before
spawn and again after typed `READY`, proves that the listener belongs to its
owned process group, and records an immutable launch attempt beneath the
package's `game-runtime/` directory.

Install the package-bound Sunshine entry with a dry run first, followed by the
same command plus `--apply`:

```bash
uv run --offline --project tools python \
  tools/runtime/vista_playable_home/sunshine_app.py \
  --apps "$HOME/.config/sunshine/apps.json" \
  --python /usr/bin/python3 \
  --launcher "$PWD/tools/runtime/vista_playable_home/packaged_entrypoint.py" \
  --profile "$PROFILE" --profile-sha256 "$PROFILE_SHA" \
  --exit-timeout 90 --working-dir "$PWD"
```

The host's Sunshine process should be a user service with `DISPLAY=:117` and
linger enabled. Moonlight video and NVENC can be ready while control remains
blocked: keyboard/mouse require writable `/dev/uinput`, and gamepads also
require writable `/dev/uhid`. Treat root-only devices as view-only, never as a
successful remote-control setup.

On the accepted host the service name is `vista-sunshine.service`. Stop only
the currently owned packaged world without stopping Sunshine or touching other
UE sessions:

```bash
uv run --offline --project tools python \
  tools/runtime/vista_playable_home/stop.py \
  --workspace "$ATT"
```
