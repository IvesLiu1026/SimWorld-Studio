---
id: demo_recording
name: Demo Video Recording
version: 1.0.0
author: simworld
tags: [demo, video, recording, capture, camera, ffmpeg, results]
dependencies: [python_batching]
description: Record finished SimWorld scenes as orbit or fly-through demo videos and attach them to saved maps in Results.
---

# Demo Video Recording

Use this skill after a scene has been generated or verified. The output is a demo video visible in the Results > Videos tab. If the scene has been saved as a map, the same video is also attached to that map card in Results > Scenes.

## Rules

1. If the map is already saved, use that saved map name in the metadata. If it is not saved, still record the video and use a generated `unsaved_<timestamp>` map name.
2. Record one viewpoint per `execute_python_script` call. Do not combine scene construction and video recording in the same script.
3. This is the only case where moving the viewport camera from Python is allowed. Restore quality settings when finished.
4. The script is long-running. After `execute_python_script` returns, read `log_path` until it contains `[DONE]` or `[ERROR]`.
5. Prefer a 720p preview first. Use 1080p only when the user asks for a final-quality clip.

## Storage Contract

Write each demo under the UE project Saved directory:

```python
from pathlib import Path
import json, time, unreal

MAP_NAME = "MySavedMap"  # or "unsaved_" + time.strftime("%Y%m%d_%H%M%S")
MAP_PATH = "/Game/SavedScenes/MySavedMap"  # or "" for unsaved scenes
VIEW_NAME = "orbit_overview"
VIDEO_ID = MAP_NAME + "_" + VIEW_NAME + "_" + time.strftime("%Y%m%d_%H%M%S")

out_dir = Path(unreal.SystemLibrary.get_project_saved_directory()) / "SimWorldDemos" / MAP_NAME / VIDEO_ID
frames_dir = out_dir / "frames"
frames_dir.mkdir(parents=True, exist_ok=True)
video_file = "demo.mp4"

metadata = {
    "id": VIDEO_ID,
    "mapName": MAP_NAME,
    "mapPath": MAP_PATH,
    "viewName": VIEW_NAME,
    "cameraMode": "orbit",  # orbit | forward | static
    "fileName": video_file,
    "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "durationSec": 6,
    "fps": 30,
    "resolution": [1280, 720],
    "notes": "Recorded after scene verification"
}
```

The Web Results page scans `Saved/SimWorldDemos/**/metadata.json`, streams `fileName`, and always shows the video in Results > Videos. If `mapName` or `mapPath` matches a saved map, it also shows the video on that saved map card.

## Camera Patterns

### Orbit

Use orbit for overview clips. Pick a scene center and a reference camera XY; derive the radius from those points. Each frame sets camera position on the circle and yaws toward the center.

```python
theta = start_angle + 2.0 * math.pi * i / num_frames
x = center_x + radius * math.cos(theta)
y = center_y + radius * math.sin(theta)
yaw = math.degrees(math.atan2(center_y - y, center_x - x))
vp_sub.set_level_viewport_camera_info(unreal.Vector(x, y, camera_z), unreal.Rotator(pitch, yaw, 0.0))
```

### Forward Fly-Through

Use forward for route preview clips. Keep pitch/yaw stable and translate along one axis or along a short path.

```python
t = i / max(num_frames - 1, 1)
x = start_x + travel_distance_cm * t
vp_sub.set_level_viewport_camera_info(unreal.Vector(x, start_y, start_z), unreal.Rotator(pitch, yaw, roll))
```

## Capture Template

Use HighResShot on editor post-tick, then rename screenshots by mtime and encode with ffmpeg.

```python
import json, math, shutil, subprocess, time
from pathlib import Path
import unreal

try:
    script_start_time = time.time()
    fps = 30
    duration_sec = 6
    num_frames = int(round(duration_sec * fps))
    res_x, res_y = 1280, 720
    ticks_per_frame = 30
    editor_hz = 60

    # Create out_dir, frames_dir, metadata as shown in Storage Contract.

    vp_sub = unreal.UnrealEditorSubsystem()
    world = unreal.EditorLevelLibrary.get_editor_world()
    setup_cmds = [
        "DisableAllScreenMessages",
        "ShowFlag.OnScreenDebug 0",
        "ShowFlag.Selection 0",
        "ShowFlag.Grid 0",
        "ShowFlag.BillboardSprites 0",
        "ShowFlag.WidgetComponents 0",
        "ShowFlag.Fps 0",
        "ShowFlag.StatUnit 0",
        "stat none",
        "r.RayTracing.Shadows 0",
        "r.Shadow.DistanceScale 8",
        "r.Shadow.RadiusThreshold 0",
        "r.Shadow.Virtual.Cache 1",
        "r.Streaming.FullyLoadUsedTextures 1",
        "r.ScreenPercentage 125",
        "t.MaxFPS 0",
    ]
    for cmd in setup_cmds:
        unreal.SystemLibrary.execute_console_command(world, cmd)

    try:
        unreal.EditorLevelLibrary.editor_set_game_view(True)
    except Exception:
        pass

    saved_dir = Path(unreal.SystemLibrary.get_project_saved_directory())
    shot_dirs = [saved_dir / "Screenshots" / "WindowsEditor", saved_dir / "Screenshots" / "Windows"]
    state = {"frame": 0, "tick": 0, "handle": None}

    def set_camera(i):
        # Implement orbit or forward camera here.
        pass

    def restore_cvars():
        for cmd in ["r.RayTracing.Shadows 1", "r.ScreenPercentage 100"]:
            try:
                unreal.SystemLibrary.execute_console_command(world, cmd)
            except Exception:
                pass

    def finish():
        if state["handle"] is not None:
            unreal.unregister_slate_post_tick_callback(state["handle"])
            state["handle"] = None
        restore_cvars()

        candidates = []
        for d in shot_dirs:
            if not d.exists():
                continue
            for p in d.glob("HighresScreenshot*.png"):
                if p.stat().st_mtime >= script_start_time - 1.0:
                    candidates.append((p.stat().st_mtime, p))
        candidates.sort(key=lambda x: x[0])
        shot_paths = [p for _, p in candidates]

        moved = 0
        for idx in range(min(num_frames, len(shot_paths))):
            shutil.move(str(shot_paths[idx]), str(frames_dir / f"frame_{idx:04d}.png"))
            moved += 1
        if moved < num_frames and moved > 0:
            last = frames_dir / f"frame_{moved-1:04d}.png"
            for idx in range(moved, num_frames):
                shutil.copy2(str(last), str(frames_dir / f"frame_{idx:04d}.png"))

        video_path = out_dir / video_file
        cmd = [
            "ffmpeg", "-y", "-framerate", str(fps),
            "-i", str(frames_dir / "frame_%04d.png"),
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:v", "libx264", "-preset", "slow", "-crf", "18",
            "-pix_fmt", "yuv420p", str(video_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print("[ERROR] demo_recording: ffmpeg failed " + result.stderr[-1000:])
            return

        metadata["frameCount"] = num_frames
        (out_dir / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        print("[DONE] demo_recording id=" + metadata["id"] + " file=" + str(video_path))

    def on_tick(delta_time):
        state["tick"] += 1
        if state["tick"] < ticks_per_frame:
            return
        state["tick"] = 0
        i = state["frame"]
        if i >= num_frames:
            finish()
            return
        set_camera(i)
        unreal.SystemLibrary.execute_console_command(world, f"HighResShot {res_x}x{res_y}")
        state["frame"] = i + 1

    state["handle"] = unreal.register_slate_post_tick_callback(on_tick)
    print("Started demo recording; estimated real capture seconds=" + str(num_frames * ticks_per_frame / editor_hz))
except Exception as exc:
    print("[ERROR] demo_recording: " + str(exc))
```
