"""5.8 / SPEAR navigation training runner (replaces the 5.3 / UnrealCV batch_runner).

UE 5.8 controls agents via **SPEAR** (msgpack-rpc), not UnrealCV.  The correct
topology is a cluster: one **server** computes physics (nullrhi) and one
**render client** renders cameras; Python connects to the *server* SPEAR port for
control and to the *client* SPEAR port for camera pixels (see
SimWorld-SPEAR `utils/cluster_launcher` + `docs/ARCHITECTURE_CLUSTER.md`).

This runner:
  1. boots ONE cluster (server + 1 render client) for the whole run,
  2. for each navigation episode: spawns the BP pedestrian agent, drives it with an
     LLM (GPT-4o vision) reading the agent's camera, and
  3. writes Studio-compatible output via `gym_env.logger.EpisodeLogger`
     (episode.jsonl / llm_raw.jsonl / frames / summary.json) + prints
     ``batch output dir: <dir>`` so `web/server/training.js` can tail it and the
     Agent Training panel's live charts populate unchanged.

Memory-update training: on a collision (agent stuck against an obstacle) the LLM
writes a reusable lesson into a persisted NavMemory store, which is injected into
the policy on later episodes. Training updates the store; evaluation freezes it.

Output is byte-compatible with the panel: training.js reads
``info.{action_name,distance_to_goal_cm,path_length_cm,success,agent_xy,episode_id}``
per episode.jsonl line and ``summary.json.{SR,SPL,steps,path_length_cm,ended_reason}``.

Usage:
    python -m gym_env.spear_nav_runner --episodes-file <taskset.jsonl> \
        --max-steps 40 --model-id gpt-4o-mini --root runs --run-prefix <jobId>
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import logging
import math
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

# --- bridge to the UE 5.8 SimWorld-SPEAR stack (cluster_launcher + spear) ------
_SPEAR_REPO = Path(os.environ.get("SIMWORLD_SPEAR_REPO", "/data/koe/SimWorld_SPEAR_dev"))
_SPEAR_PY = Path(os.environ.get("SIMWORLD_SPEAR_PYTHON", "/data/koe/spear-sim-spear/python"))
_SPEAR_EXT = Path(os.environ.get("SIMWORLD_SPEAR_EXT", "/data/koe/spear-sim-spear/python_ext/python"))
for _p in (str(_SPEAR_REPO / "utils"), str(_SPEAR_PY), str(_SPEAR_EXT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np  # noqa: E402
import spear  # noqa: E402
import spear_ext  # noqa: F401,E402  (registers SPEAR ext types)
from cluster_launcher.cluster_launcher import Cluster, ClusterSpec  # noqa: E402


# --- SPEAR helpers, inlined from simworld.client -----------------------------
# (NOT imported: the Studio's nav_task registers its own `simworld` SDK package,
#  which shadows SimWorld-SPEAR's `simworld` python package — so `from
#  simworld.client import ...` fails inside gym_env. These three are small.)
def _complete(value):
    """Resolve a SPEAR async future (``.get()``); pass dicts through unchanged."""
    get = getattr(value, "get", None)
    if callable(get) and not isinstance(value, dict):
        try:
            return get()
        except TypeError:
            return value
    return value


def _loc_xyz(value):
    if isinstance(value, dict):
        return (float(value.get("X", value.get("x", 0.0))),
                float(value.get("Y", value.get("y", 0.0))),
                float(value.get("Z", value.get("z", 0.0))))
    return (float(getattr(value, "X", getattr(value, "x", 0.0))),
            float(getattr(value, "Y", getattr(value, "y", 0.0))),
            float(getattr(value, "Z", getattr(value, "z", 0.0))))


def _camera_bundle_to_rgb(bundle):
    """SPEAR SceneCapture read_pixels bundle -> contiguous RGB (BGRA->RGB)."""
    if isinstance(bundle, dict) and isinstance(bundle.get("arrays"), dict):
        arr = np.asarray(bundle["arrays"].get("data"), dtype=np.uint8)
        if arr.ndim == 3 and arr.shape[2] >= 3:
            return np.ascontiguousarray(arr[:, :, :3][:, :, ::-1])
        raise RuntimeError(f"unexpected camera data shape: {arr.shape}")
    if isinstance(bundle, dict):
        width = int(bundle.get("Width", bundle.get("width", 0)) or 0)
        height = int(bundle.get("Height", bundle.get("height", 0)) or 0)
        raw = bundle.get("Image", bundle.get("image", b""))
        if width <= 0 or height <= 0:
            raise RuntimeError(f"camera bundle missing dimensions: {list(bundle)}")
        arr = np.frombuffer(raw, dtype=np.uint8) if isinstance(raw, bytes) else np.asarray(raw, dtype=np.uint8)
        ch = arr.size // max(1, width * height)
        if ch < 3:
            raise RuntimeError(f"camera bundle has too few channels: {ch}")
        arr = arr[: width * height * ch].reshape((height, width, ch))
        return np.ascontiguousarray(arr[:, :, :3][:, :, ::-1])
    raise RuntimeError(f"unsupported camera bundle type: {type(bundle).__name__}")

from .logger import EpisodeLogger  # noqa: E402

log = logging.getLogger("spear_nav_runner")

# Movement geometry knobs (demo maps are cm-scale).
# MOVE_FORWARD holds the input until the agent advances _STRIDE_CM, then StopAgent halts it.
# A DISTANCE bound (not a fixed tick count) keeps the stride constant regardless of real-time
# frame duration — under PixelStreaming load a fixed 30 ticks covered ~5 m and overshot.
_STRIDE_CM = 1000.0         # target distance per MOVE_FORWARD (~10 m big stride, user-requested)
_FORWARD_MAX_TICKS = 240    # hard cap so a blocked agent can't loop forever (slow speed needs more
                            # ticks to cover 10 m; the distance bound breaks early in the common case)
_SETTLE_MAX_TICKS = 120     # frames to bring the agent to a full stop before the LLM call
_SETTLE_STILL_CM = 1.5      # per-frame motion below which the agent counts as stopped
_MAX_WALK_SPEED_CMS = 220.0 # slower walk: smooth/visible stride + precise distance bound (less
                            # per-tick overshoot than the fast default glide)
# Async stride pacing. stepping_mode="async" = the engine free-runs at full FPS; begin/end_frame
# are just sync points. So we issue ONE sustained MoveForward, then SLEEP and poll distance every
# _FORWARD_POLL_S — a handful of sync points spread over the stride instead of ~150 back-to-back,
# which keeps PixelStreaming smooth (the back-to-back syncs were what made MOVE_FORWARD stutter).
_FORWARD_POLL_S = 0.15        # poll agent distance every 150 ms during a stride
_FORWARD_MAX_SECONDS = 12.0   # wall-clock cap for one stride (10 m at ~220 cm/s ≈ 4.5 s)
_FORWARD_STALL_CM = 2.0       # moved < this between polls = not progressing
_FORWARD_STALL_POLLS = 4      # consecutive stalled polls → blocked; end the stride early (~0.6 s)
_FORWARD_TICKS = 30       # (legacy; unused now that the stride is distance-bounded)
_ROTATE_DEG = 30.0        # degrees per TURN action
_ACTIONS = ("MOVE_FORWARD", "TURN_LEFT", "TURN_RIGHT", "STOP")
_STUCK_MIN_MOVE_CM = 20.0  # a real forward stride moves ≫ this; less = blocked
_STUCK_LIMIT = 3           # consecutive blocked MOVE_FORWARDs → collision verdict
# Blueprint class of the replicated agent proxy on the render client. SPEAR's
# find_actors_by_class does NOT match BP subclasses through a C++ base, so the
# client proxy must be located by this exact BP class (matches simworld.recording).
PEDESTRIAN_BP = "/Game/Agent/Pedestrian/Core/BP_PedestrianAgentBase.BP_PedestrianAgentBase_C"

_NAV_SYSTEM_PROMPT = (
    "You are an embodied navigation agent in a 3D scene. You see a first-person RGB "
    "image plus the bearing and distance to the goal. The bearing is RELATIVE TO YOUR "
    "FACING: 0 deg = goal straight ahead, positive = goal is to your RIGHT, negative = "
    "to your LEFT. Actions: MOVE_FORWARD (walk ~2 m forward), TURN_LEFT (turn 30 deg "
    "left), TURN_RIGHT (turn 30 deg right), STOP (declare arrival). Decision rule: if "
    "distance < the success radius, STOP. Else if the bearing is within +/-25 deg, "
    "MOVE_FORWARD. Else TURN_RIGHT when bearing is positive, TURN_LEFT when negative, to "
    "face the goal. Do not keep turning past the goal. Reply with EXACTLY ONE action "
    "name on its own line, nothing else."
)


# --------------------------------------------------------------------------- #
# Episodes
# --------------------------------------------------------------------------- #


def _load_episodes(path: str) -> list[dict[str, Any]]:
    """Load nav episodes from the Studio task-set file (episodes.jsonl / taskset.json)."""

    text = Path(path).read_text(encoding="utf-8").strip()
    # Whole-file JSON: wrapper {"episodes": [...]}, a list, or a single object.
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return list(data["episodes"]) if "episodes" in data else [data]
        if isinstance(data, list):
            return list(data)
    except json.JSONDecodeError:
        pass
    # JSONL: one object per line (each an episode, or a wrapper).
    eps: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        if isinstance(obj, dict) and "episodes" in obj:
            eps.extend(obj["episodes"])
        else:
            eps.append(obj)
    return eps


def _ep_field(ep: dict, *names, default=None):
    for n in names:
        if n in ep and ep[n] is not None:
            return ep[n]
    return default


# --------------------------------------------------------------------------- #
# LLM policy (OpenAI vision)
# --------------------------------------------------------------------------- #


class GPTNavPolicy:
    def __init__(self, model_id: str = "gpt-4o-mini"):
        from openai import OpenAI

        self.client = OpenAI()
        self.model_id = model_id

    def act(self, rgb: np.ndarray, distance_cm: float, bearing_deg: float, radius_cm: float,
            memory_prompt: str = ""):
        png = _rgb_to_png_b64(rgb)
        user = (
            f"Goal: distance={distance_cm:.0f} cm, bearing={bearing_deg:+.0f} deg "
            f"(positive = right). Success radius = {radius_cm:.0f} cm. Pick one action."
        )
        t0 = time.time()
        resp = self.client.chat.completions.create(
            model=self.model_id,
            messages=[
                {"role": "system", "content": _NAV_SYSTEM_PROMPT + (memory_prompt or "")},
                {"role": "user", "content": [
                    {"type": "text", "text": user},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{png}"}},
                ]},
            ],
            max_tokens=12,
            temperature=0.0,
        )
        text = (resp.choices[0].message.content or "").strip().upper()
        action = next((a for a in _ACTIONS if a in text), "MOVE_FORWARD")
        usage = {
            "input_tokens": getattr(resp.usage, "prompt_tokens", 0),
            "output_tokens": getattr(resp.usage, "completion_tokens", 0),
        }
        return action, _LLMResp(text=text, reasoning=text, usage=usage), time.time() - t0


class _LLMResp:
    def __init__(self, text, reasoning, usage):
        self.text, self.reasoning, self.usage, self.tool_calls, self.raw = text, reasoning, usage, [], None


class NavMemory:
    """Accumulated navigation lessons the LLM writes after failed (collision) trajectories
    — the unit of memory-update training. Persisted to JSON so lessons carry across runs
    and are injected into the policy prompt on later episodes."""

    def __init__(self, path=None, readonly=False):
        self.path = Path(path) if path else None
        self.readonly = readonly
        self.lessons = []
        if self.path and self.path.exists():
            try:
                self.lessons = list(json.loads(self.path.read_text()).get("lessons", []))
            except Exception:
                self.lessons = []

    def add(self, lesson):
        lesson = (lesson or "").strip()
        if not lesson or self.readonly or lesson in self.lessons:
            return False
        self.lessons.append(lesson)
        if self.path:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps({"lessons": self.lessons}, indent=2))
            except Exception:
                pass
        return True

    def as_prompt(self):
        if not self.lessons:
            return ""
        items = "\n".join(f"- {l}" for l in self.lessons[-12:])
        return "\nLessons from your past attempts (apply them):\n" + items


def reflect_on_failure(policy, rgb, *, x, y, yaw, dist_cm, bearing_deg, reason="collision"):
    """Verifier feedback → the LLM writes a reusable navigation lesson from a FAILED episode.
    Grounded in WHERE it failed (position + heading) so the lesson stays useful even when the
    low-res rear camera doesn't clearly reveal the scene. Handles both a collision (blocked
    ahead) and a timeout (didn't reach the goal in time). This is the 'write to experience' step."""
    if reason == "collision":
        fallback = (f"Near x={x:.0f}, y={y:.0f} the path toward bearing {bearing_deg:+.0f} deg is "
                    f"blocked; turn left or right to find a clear opening instead of pushing forward.")
        situation = (f"A navigation robot got STUCK: it tried to move forward {_STUCK_LIMIT}+ times "
                     f"without advancing, so the path DIRECTLY AHEAD is blocked.")
    else:
        fallback = (f"From near x={x:.0f}, y={y:.0f} the goal at bearing {bearing_deg:+.0f} deg was "
                    f"not reached in time; commit to forward progress when roughly aligned and avoid "
                    f"oscillating turns.")
        situation = ("A navigation robot RAN OUT OF TIME (hit the step limit) without reaching the "
                     "goal — it likely wandered or hesitated instead of committing to the route.")
    try:
        png = _rgb_to_png_b64(rgb if rgb is not None else np.zeros((8, 8, 3), np.uint8))
        user = (
            f"{situation} It was near map position x={x:.0f}, y={y:.0f}, facing yaw={yaw:.0f} deg, "
            f"goal {dist_cm:.0f} cm away at bearing {bearing_deg:+.0f} deg (0=ahead, +=right). If the "
            f"image reveals the scene (wall, building, opening, road), use it. Write ONE short, reusable "
            f"lesson referencing the location and a concrete tactic. One sentence, no apologies."
        )
        resp = policy.client.chat.completions.create(
            model=policy.model_id, max_tokens=80, temperature=0.3,
            messages=[
                {"role": "system", "content": "You review failed robot navigation attempts and write concise, reusable, actionable lessons. Never apologize; always give a usable lesson."},
                {"role": "user", "content": [
                    {"type": "text", "text": user},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{png}"}},
                ]},
            ],
        )
        text = (resp.choices[0].message.content or "").strip()
        low = text.lower()
        # Any hedge → the clean, position-grounded fallback (more useful than a lesson
        # that opens with "I don't know what the obstacle is").
        if not text or any(p in low for p in (
                "can't", "cannot", "sorry", "unable", "i'm not", "don't know", "do not know",
                "not sure", "unclear", "can't tell", "cannot tell", "hard to tell")):
            return fallback
        return text
    except Exception as exc:
        log.warning("collision reflection failed: %s", exc)
        return fallback


# --------------------------------------------------------------------------- #
# SPEAR helpers
# --------------------------------------------------------------------------- #


def _rgb_to_png_b64(rgb: np.ndarray) -> str:
    from PIL import Image

    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _write_live(rgb, path) -> None:
    """Overwrite the run's single 'live' JPEG with the latest agent-camera frame, atomically.
    The web UI polls this at ~5 Hz for a real-time broadcast of the agent's camera."""
    if rgb is None or not path:
        return
    try:
        from PIL import Image
        tmp = f"{path}.tmp"
        Image.fromarray(rgb).save(tmp, format="JPEG", quality=72)
        os.replace(tmp, path)
    except Exception:
        pass


def _unpack_xyz(v) -> tuple[float, float, float]:
    if isinstance(v, dict):
        return float(v.get("X", v.get("x", 0))), float(v.get("Y", v.get("y", 0))), float(v.get("Z", v.get("z", 0)))
    return float(v[0]), float(v[1]), float(v[2])


def _capture(cam_inst, comp, w: int, h: int) -> np.ndarray | None:
    """One RGB frame from the agent's recording camera on the render client.

    ``read_pixels`` runs inside the client's ``end_frame`` (SPEAR receive phase);
    the bundle is an async future, so ``_complete`` it before decoding — exactly
    as ``simworld.recording.engine.RemoteClientCamera.capture`` does.
    """
    if comp is None:
        return None
    try:
        raw: dict[str, Any] = {}
        with cam_inst.begin_frame():
            pass
        with cam_inst.end_frame():
            raw["bundle"] = comp.read_pixels()
        bundle = _complete(raw.get("bundle"))
        if bundle is None:
            return None
        return _camera_bundle_to_rgb(bundle)
    except Exception as exc:
        log.warning("camera capture failed: %s", exc)
        return None


def _connect(port: int, timeout_s: float = 180.0):
    # CONNECT mode: LAUNCH_MODE="none" so spear.Instance attaches to the
    # already-running cluster process instead of launching its own (the launch
    # path hangs headless). Matches cluster_launcher/test_e2e_multiagent.py.
    config = spear.get_config()
    config.defrost()
    config.SPEAR.LAUNCH_MODE = "none"
    config.SPEAR.INSTANCE.EDITOR_LAUNCH_MODE = "none"
    config.SP_SERVICES.RPC_SERVICE.RPC_SERVER_PORT = port
    config.SPEAR.INSTANCE.CLIENT_INTERNAL_TIMEOUT_SECONDS = 180
    config.freeze()
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        try:
            inst = spear.Instance(config=config)
            inst.start_keepalive(interval_seconds=10.0)
            return inst
        except Exception as exc:  # boot/compile can take >2s; retry
            last = exc
            time.sleep(3)
    raise RuntimeError(f"could not connect SPEAR :{port}: {last}")


# --------------------------------------------------------------------------- #
# Episode loop
# --------------------------------------------------------------------------- #


class DataHub:
    """One shared store all runs append to (run -> episode -> step), read by the web UI's
    datahub-polling panel so the browser always shows the latest/running data."""

    def __init__(self, path):
        self.path = Path(path) if path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record):
        if not self.path:
            return
        try:
            with self.path.open("a") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as exc:
            log.warning("datahub append failed: %s", exc)


def run_episode(ctrl, cam_inst, ep, *, max_steps, policy, root, run_name, cam_w, cam_h,
                memory=None, hub=None, run_id=None, model_id=None, idx=0, epoch=0, live_path=None):
    ep_id = str(_ep_field(ep, "episode_id", "id", default=run_name))
    start = _ep_field(ep, "start_position", "start", "spawn", default={"X": 0, "Y": 0, "Z": 100})
    goal = _ep_field(ep, "goal_position", "goal", "target", default={"X": 500, "Y": 0, "Z": 100})
    sx, sy, sz = _unpack_xyz(start)
    gx, gy, _gz = _unpack_xyz(goal)
    radius = float(_ep_field(ep, "success_radius_cm", "radius_cm", default=200.0))

    us = ctrl._unreal_service
    with ctrl.begin_frame():
        us.initialize()
        us.set_world(world=ctrl._get_game_world())
        # Spawn the BP agent (not the C++ class) so the client proxy replicates as
        # BP_PedestrianAgentBase_C — matchable by the camera proxy search and
        # carrying the authored SpringArm + recording camera (matches simworld.recording).
        uclass = us.load_class(uclass="AActor", name=PEDESTRIAN_BP)
        actor = us.spawn_actor(
            uclass=uclass, location={"X": sx, "Y": sy, "Z": sz},
            rotation={"Pitch": 0.0, "Yaw": 0.0, "Roll": 0.0},
            spawn_parameters={"SpawnCollisionHandlingOverride": "AlwaysSpawn"},
            with_sp_funcs=True)
        if actor is not None:
            # BP-class actors expose the C++ Agent_* UFunctions only after their
            # sp-func wrappers are (re)initialized (same as the camera component).
            if getattr(actor, "_initialized_sp_funcs", False):
                actor._initialized_sp_funcs = False
            actor.initialize_sp_funcs()
    with ctrl.end_frame():
        pass
    # Let the freshly-spawned actor BeginPlay so its UFunctions are live.
    _advance(ctrl, 10)
    if actor is None:
        log.error("episode %s: spawn failed", ep_id)
        return {"SR": 0.0, "SPL": 0.0, "steps": 0, "ended_reason": "spawn_error", "episode_id": ep_id}

    # Slow, smooth walk: lower MaxWalkSpeed so each MOVE_FORWARD stride reads as a visible walk in
    # the viewport (not a fast glide) and the distance-bounded stride below lands precisely on the
    # ~10 m target (less per-tick overshoot).
    try:
        with ctrl.begin_frame():
            actor.SetMaxSpeed(MaxWalkSpeed=_MAX_WALK_SPEED_CMS)
        with ctrl.end_frame():
            pass
    except Exception as exc:
        log.warning("SetMaxSpeed failed (using default speed): %s", exc)

    logger = EpisodeLogger(run_name, root=root, save_frames=True, timestamp_dir=False,
                           meta={"episode_id": ep_id, "backend": "spear_5_8"})

    # Spawn-fall guard: if the start point isn't on the walkable surface the agent drops
    # into the void; KillZ then destroys it and the very next actor RPC HARD-CRASHES the
    # SPEAR server (Index>=0 assert on the stale handle). Detect the free-fall in the first
    # frames and abort this episode cleanly instead of taking the whole cluster down.
    with ctrl.begin_frame():
        pass
    with ctrl.end_frame():
        _, _, z0 = _loc_xyz(_complete(actor.K2_GetActorLocation()))
    _advance(ctrl, 25)
    with ctrl.begin_frame():
        pass
    with ctrl.end_frame():
        _, _, z1 = _loc_xyz(_complete(actor.K2_GetActorLocation()))
    fell = (math.isfinite(z0) and math.isfinite(z1)
            and (z0 - z1) > 300.0 and (sz - z1) > 300.0)
    log.warning("episode %s spawn-Z check: spawn_z=%.0f z0=%.0f z1=%.0f fell=%s", ep_id, sz, z0, z1, fell)
    if fell:
        log.error("episode %s: agent free-falling after spawn (z %.0f -> %.0f); skipping bad spawn",
                  ep_id, z0, z1)
        try:
            with ctrl.begin_frame():
                us.destroy_actor(actor=actor)
            with ctrl.end_frame():
                pass
        except Exception:
            pass
        summary = {"SR": 0.0, "SPL": 0.0, "steps": 0, "path_length_cm": 0.0,
                   "ended_reason": "spawn_fell", "episode_id": ep_id,
                   "spawn_z": round(sz, 1), "z0": round(z0, 1), "z1": round(z1, 1)}
        logger.log_summary(summary)
        print(f"batch output dir: {Path(root).resolve()}", flush=True)
        return summary

    # Route the render client's net view to the agent. The client possesses no
    # pawn, so without this its relevancy stays at spawn and the agent stops
    # replicating (and the agent-bound camera goes stale) once it walks past
    # NetCullDistance. Matches simworld.recording.
    try:
        with ctrl.begin_frame():
            pc_cls = us.get_static_class(uclass="APlayerController")
            pcs = us.find_actors_by_class(uclass=pc_cls, with_sp_funcs=True) or []
            for pc in pcs:
                pc.SetViewTargetWithBlend(NewViewTarget=actor, BlendTime=0.0)
        with ctrl.end_frame():
            pass
    except Exception as exc:
        log.warning("view-target routing failed: %s", exc)

    # Bind the agent's OWN recording camera on the render client (ConfigureCamera /
    # InitializeCamera / GetRecordingCamera are driven on the replicated proxy).
    cam = _bind_client_camera(cam_inst, (sx, sy), cam_w, cam_h)

    path_len = 0.0
    prev = (sx, sy)
    ended_reason = "max_steps"
    success = False
    x, y = sx, sy
    t = 0
    last_action = None     # for the stuck/collision verifier
    stuck = 0
    collision_frame = None
    last_frame = None
    bearing = 0.0
    yaw = 0.0
    for t in range(max_steps):
        # SPEAR frame model: SEND actions in begin_frame, READ state in end_frame.
        # One observe-frame: advance + read the agent pose.
        with ctrl.begin_frame():
            pass
        with ctrl.end_frame():
            nx, ny, _z = _loc_xyz(_complete(actor.K2_GetActorLocation()))
            try:
                rot = _complete(actor.K2_GetActorRotation())
                yaw = float(rot.get("Yaw", rot.get("yaw", 0.0))) if isinstance(rot, dict) else 0.0
            except Exception:
                yaw = 0.0
        # Guard against NaN/inf poses (agent fell out of the world or an RPC glitch):
        # keep the last good position rather than poisoning path length / distance.
        if math.isfinite(nx) and math.isfinite(ny):
            x, y = nx, ny
        rgb = _capture(cam_inst, cam, cam_w, cam_h)
        if rgb is not None:
            last_frame = rgb
            _write_live(rgb, live_path)
        moved = math.hypot(x - prev[0], y - prev[1])
        path_len += moved
        prev = (x, y)
        # Stuck/collision verifier: a MOVE_FORWARD that produced ~no movement means the
        # agent walked into something. Count consecutive blocked forwards.
        if last_action == "MOVE_FORWARD":
            stuck = stuck + 1 if moved < _STUCK_MIN_MOVE_CM else 0
        elif moved >= _STUCK_MIN_MOVE_CM:
            stuck = 0

        dx, dy = gx - x, gy - y
        dist = math.hypot(dx, dy)
        # Bearing RELATIVE to the agent's facing (0 = goal straight ahead, + = right).
        # World-frame bearing alone is useless to the policy — it has no idea which way
        # it faces — so it just spins. Subtract yaw and wrap to [-180, 180].
        world_bearing = math.degrees(math.atan2(dy, dx))
        bearing = (world_bearing - yaw + 180.0) % 360.0 - 180.0
        success = dist <= radius

        collided = stuck >= _STUCK_LIMIT
        if success:
            action, llm = "STOP", _LLMResp("STOP", "reached goal", {})
            ended_reason = "success"
        elif collided:
            # Verifier verdict: blocked for too long → fail the trajectory here. The frame
            # is kept so the LLM can look at what blocked it and write a lesson.
            action, llm = "STOP", _LLMResp("STOP", f"collision: blocked {stuck} steps", {})
            ended_reason = "collision"
            collision_frame = rgb
        else:
            frame = rgb if rgb is not None else np.zeros((cam_h, cam_w, 3), np.uint8)
            action, llm, _ = policy.act(frame, dist, bearing, radius,
                                        memory.as_prompt() if memory is not None else "")

        info = {
            "episode_id": ep_id, "step": t, "action_name": action,
            "distance_to_goal_cm": dist, "path_length_cm": path_len,
            "success": success, "agent_xy": [x, y],
            "bearing_deg": round(bearing, 1), "yaw_deg": round(yaw, 1),
            "stuck": stuck, "collided": collided,
        }
        logger.log_step(t, {"tool": action}, {"rgb": rgb} if rgb is not None else {},
                        reward=-dist / 1000.0, done=success, truncated=False, info=info)
        logger.log_llm(t, policy.model_id, llm)
        if hub is not None:
            hub.append({
                "kind": "step", "runId": run_id, "model": model_id, "episodeId": ep_id,
                "episodeIdx": idx, "epoch": epoch, "step": t, "action": action,
                "input": {"prompt": f"goal {dist:.0f}cm, bearing {bearing:+.0f}deg (real SPEAR camera image)",
                          "distance_cm": round(dist, 1), "bearing_deg": round(bearing, 1), "radius_cm": radius},
                "output": {"text": getattr(llm, "text", action), "usage": getattr(llm, "usage", {})},
                "agentXy": [round(x, 1), round(y, 1)], "goalXy": [round(gx, 1), round(gy, 1)],
                "distanceCm": round(dist, 1), "bearingDeg": round(bearing, 1),
                "yawDeg": round(yaw, 1), "stuck": stuck, "frame": f"{run_name}/{t}",
            })
        if success or collided:
            break
        if action == "STOP":
            ended_reason = "stopped"
            break
        last_action = action

        # Apply the action. ASpPedestrianAgentBase control surface (no Agent_ prefix):
        #   MoveForward(Scale) is AddMovementInput-style — HELD every tick;
        #   Rotate(DeltaYawDegrees) is a one-shot yaw delta (left = negative).
        if action == "MOVE_FORWARD":
            # Distance-bounded ~10 m stride, paced for SMOOTH PixelStreaming. Because async stepping
            # lets the engine free-run, we send ONE sustained MoveForward (the agent's Tick re-applies
            # it every engine frame) and then SLEEP, polling the cheap K2_GetActorLocation only every
            # _FORWARD_POLL_S. That's a few sync points over the stride instead of ~150 back-to-back
            # begin/end frames — the latter contended with the render/encode and made MOVE_FORWARD
            # stutter. (Capturing the camera in this loop was a separate, worse stall — never do it.)
            with ctrl.begin_frame():
                _start_future = actor.K2_GetActorLocation()
            with ctrl.end_frame():
                _sxf, _syf, _szf = _loc_xyz(_complete(_start_future))
            with ctrl.begin_frame():
                actor.MoveForward(Scale=1.0)
            with ctrl.end_frame():
                pass
            _lxf, _lyf = _sxf, _syf
            _stall = 0
            _deadline = time.time() + _FORWARD_MAX_SECONDS
            while time.time() < _deadline:
                time.sleep(_FORWARD_POLL_S)
                with ctrl.begin_frame():
                    _loc_future = actor.K2_GetActorLocation()
                with ctrl.end_frame():
                    _cxf, _cyf, _czf = _loc_xyz(_complete(_loc_future))
                if math.hypot(_cxf - _sxf, _cyf - _syf) >= _STRIDE_CM:
                    break
                # Blocked-early-out: if the agent stops advancing it has hit something — end the
                # stride in ~0.6 s instead of waiting out the full wall-clock cap.
                if math.hypot(_cxf - _lxf, _cyf - _lyf) < _FORWARD_STALL_CM:
                    _stall += 1
                    if _stall >= _FORWARD_STALL_POLLS:
                        break
                else:
                    _stall = 0
                _lxf, _lyf = _cxf, _cyf
            with ctrl.begin_frame():
                actor.StopAgent()
            with ctrl.end_frame():
                pass
        else:
            delta = -_ROTATE_DEG if action == "TURN_LEFT" else _ROTATE_DEG
            with ctrl.begin_frame():
                actor.Rotate(DeltaYawDegrees=float(delta))
            with ctrl.end_frame():
                pass
            with ctrl.begin_frame():
                pass
            with ctrl.end_frame():
                pass

    # Tear down the agent so the next episode starts clean.
    try:
        with ctrl.begin_frame():
            actor.StopAgent()
            us.destroy_actor(actor=actor)
        with ctrl.end_frame():
            pass
    except Exception:
        pass

    # Memory-update step: on ANY failure (collision or timeout) the LLM looks at the last
    # frame, diagnoses what went wrong, and writes a reusable lesson into memory so the agent
    # navigates this region better on the next episodes.
    lesson = None
    if (not success) and ended_reason in ("collision", "max_steps"):
        frame = collision_frame if ended_reason == "collision" else last_frame
        lesson = reflect_on_failure(policy, frame, x=x, y=y, yaw=yaw,
                                    dist_cm=dist, bearing_deg=bearing, reason=ended_reason)
        if memory is not None and lesson:
            saved = memory.add(lesson)
            log.info("episode %s %s lesson%s: %s", ep_id, ended_reason, " (saved)" if saved else "", lesson)

    straight = math.hypot(gx - sx, gy - sy)
    spl = (straight / max(path_len, straight)) if success else 0.0
    summary = {"SR": 1.0 if success else 0.0, "SPL": spl, "steps": t + 1,
               "path_length_cm": path_len, "ended_reason": ended_reason, "episode_id": ep_id}
    if lesson:
        summary["lesson"] = lesson
    logger.log_summary(summary)
    if hub is not None:
        hub.append({"kind": "episode", "runId": run_id, "model": model_id, "episodeId": ep_id,
                    "episodeIdx": idx, "epoch": epoch, "SR": summary["SR"], "SPL": round(spl, 3),
                    "steps": t + 1, "endedReason": ended_reason,
                    "startXy": [round(sx, 1), round(sy, 1)], "goalXy": [round(gx, 1), round(gy, 1)],
                    "lesson": lesson})
    print(f"batch output dir: {Path(root).resolve()}", flush=True)
    log.info("episode %s done: reason=%s steps=%d dist_final=%.0f", ep_id, ended_reason, t + 1, dist)
    return summary


def _advance(inst, n=1):
    for _ in range(n):
        with inst.begin_frame():
            pass
        with inst.end_frame():
            pass


def _bind_client_camera(cam_inst, spawn_xy, w, h, *, retries=30):
    """Bind the agent's OWN recording camera on the render client.

    The agent (SpHumanoidAgent) authors its own SpringArm + SpSceneCaptureComponent2D
    in C++; we drive it through the documented UFUNCTIONs (ConfigureCamera /
    InitializeCamera / GetRecordingCamera) on the *client* replicated proxy — no
    manual component enumeration. Mirrors simworld.recording.session exactly:
      1. locate the replicated proxy by its exact BP class, nearest to spawn,
      2. ConfigureCamera + InitializeCamera on the proxy,
      3. GetRecordingCamera -> re-wrap with_sp_funcs=True to expose read_pixels.
    Best-effort: any failure returns None and the episode runs on blank frames.
    """
    try:
        cli = cam_inst._unreal_service
        with cam_inst.begin_frame():
            cli.initialize()
            cli.set_world(world=cam_inst._get_game_world())
            proxy_cls = cli.load_class(uclass="AActor", name=PEDESTRIAN_BP)
        with cam_inst.end_frame():
            pass

        proxy = None
        for attempt in range(retries):
            candidates, locs = [], []
            with cam_inst.begin_frame():
                candidates = cli.find_actors_by_class(uclass=proxy_cls, with_sp_funcs=True) or []
            with cam_inst.end_frame():
                for cand in candidates:
                    locs.append(cand.K2_GetActorLocation())
            best = None
            for cand, raw in zip(candidates, locs):
                x, y, _z = _loc_xyz(_complete(raw))
                d2 = (x - spawn_xy[0]) ** 2 + (y - spawn_xy[1]) ** 2
                if best is None or d2 < best[0]:
                    best = (d2, cand)
            if best is not None and best[0] < 500.0 ** 2:
                proxy = best[1]
                log.info("client proxy matched %.0fcm from spawn (%d candidates)",
                         best[0] ** 0.5, len(candidates))
                break
            _advance(cam_inst, 5)
            time.sleep(0.5)
        if proxy is None:
            log.warning("replicated proxy not found on client; proceeding without RGB")
            return None

        comp = None
        with cam_inst.begin_frame():
            proxy.ConfigureCamera(InWidth=w, InHeight=h, InFovDegrees=90.0)
            # The LLM OBSERVATION is first-person: swing the RecordingCamera's
            # boom to eye level. The live PixelStreaming viewport stays
            # third-person via the agent's separate ViewCamera (set as the
            # PlayerController view target below) — input=FPV, broadcast=3rd.
            try:
                proxy.SetCameraView(View="first_person", DistanceCm=0.0,
                                    PitchDegrees=0.0, HeightCm=70.0)
            except Exception as exc:
                log.warning("SetCameraView(first_person) failed (obs stays 3rd-person): %s", exc)
            proxy.InitializeCamera()
            raw = proxy.GetRecordingCamera()
            handle = spear.to_handle(obj=raw)
            comp = cli.to_handle_or_unreal_object(obj=handle, as_unreal_object=True, with_sp_funcs=True)
            if getattr(comp, "_initialized_sp_funcs", False):
                comp._initialized_sp_funcs = False
            comp.initialize_sp_funcs()
        with cam_inst.end_frame():
            pass
        if not getattr(comp, "_sp_func_names", None):
            log.warning("RecordingCamera has no read_pixels (build older than agent camera?)")
            return None
        # Make the render client's MAIN viewport (what PixelStreaming streams) FOLLOW THE AGENT
        # by setting the client's LOCAL PlayerController view target to the agent proxy. Without
        # this the streamed video is a stationary spectator camera and the agent is off-screen;
        # with it the live video tracks the agent, matching the recording camera in the monitor.
        try:
            with cam_inst.begin_frame():
                pc_cls = cli.get_static_class(uclass="APlayerController")
                pcs = cli.find_actors_by_class(uclass=pc_cls, with_sp_funcs=True) or []
                for pc in pcs:
                    pc.SetViewTargetWithBlend(NewViewTarget=proxy, BlendTime=0.0)
            with cam_inst.end_frame():
                pass
            log.info("client viewport view-target set to agent (%d PCs) — PixelStream follows agent", len(pcs))
        except Exception as exc:
            log.warning("client view-target set failed (stream may not follow agent): %s", exc)
        log.info("agent recording camera bound (%dx%d)", w, h)
        return comp
    except Exception as exc:
        log.warning("client camera bind aborted (%s); proceeding without RGB", exc)
        return None


# --------------------------------------------------------------------------- #
# Cluster teardown
# --------------------------------------------------------------------------- #


def _force_kill_cluster(cluster, log_dir: str | None = None) -> None:
    """Best-effort SIGKILL of the cluster's UE child processes. Used when the normal
    stop() path hangs or on SIGTERM, so we never orphan UE servers/clients."""
    procs = []
    sp = getattr(cluster, "server_proc", None)
    if sp is not None:
        procs.append(sp)
    procs.extend(getattr(cluster, "client_procs", None) or [])
    for p in procs:
        try:
            if p is not None and p.poll() is None:
                p.kill()
        except Exception:
            pass
    # The Popen refs above can be wrapper scripts; the real UE children then survive and
    # orphan (starving the GPU + leaving stale PixelStreaming streamers). Robustly SIGKILL
    # any SimWorldEditor process whose command line references THIS run's unique log dir.
    if log_dir:
        try:
            import subprocess
            subprocess.run(["pkill", "-9", "-f", str(log_dir)], timeout=10)
        except Exception:
            pass


def _teardown(cluster, log_dir: str | None = None) -> None:
    """Stop the cluster with a watchdog. cluster.stop() is known to hang headless
    (a non-daemon SPEAR keepalive thread can block it); if it doesn't return in
    time we force-kill the UE children so we don't leave a lingering cluster."""
    done = threading.Event()

    def _stop():
        try:
            cluster.stop()
        except Exception:
            pass
        finally:
            done.set()

    threading.Thread(target=_stop, daemon=True).start()
    if not done.wait(timeout=25):
        log.warning("cluster.stop() timed out after 25s; force-killing UE children")
        _force_kill_cluster(cluster, log_dir)
    else:
        # Even on a "clean" stop, UE children sometimes survive — sweep by log dir.
        _force_kill_cluster(cluster, log_dir)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--episodes-file", required=True)
    ap.add_argument("--max-steps", type=int, default=40)
    ap.add_argument("--model-id", default="gpt-4o-mini")
    ap.add_argument("--root", default="runs")
    ap.add_argument("--run-prefix", default="spear")
    ap.add_argument("--map", default="/Game/Maps/demo_2?game=/Script/Engine.GameMode")
    ap.add_argument("--ue-port", type=int, default=7802)
    ap.add_argument("--spear-port", type=int, default=30030)
    ap.add_argument("--client-spear-port", type=int, default=31030)
    ap.add_argument("--beacon-port", type=int, default=17950)
    ap.add_argument("--cam-w", type=int, default=256)
    ap.add_argument("--cam-h", type=int, default=192)
    ap.add_argument("--log-dir", default="/tmp/spear_cluster_logs")
    ap.add_argument("--memory-file", default=None,
                    help="JSON file of accumulated nav lessons; injected into the policy and "
                         "(unless --memory-readonly) updated on collisions.")
    ap.add_argument("--memory-readonly", action="store_true",
                    help="Use memory but do not write new lessons (evaluation).")
    ap.add_argument("--epochs", type=int, default=1,
                    help="Run the whole task set this many times (epochs). Memory accumulates "
                         "across epochs, so success rate per epoch forms the learning curve.")
    ap.add_argument("--client-gpu", type=int, default=None,
                    help="Pin the render client to this Vulkan GPU (-graphicsadapter). On a "
                         "shared box, point it at an idle GPU so the client isn't starved and "
                         "its net handshake to the server completes in time.")
    ap.add_argument("--datahub", default=None,
                    help="Shared datahub jsonl the web UI polls (run->episode->step).")
    ap.add_argument("--pixelstream-ws-port", type=int, default=None,
                    help="If set, enable PixelStreaming on the render client and point it at this "
                         "cirrus streamer WS port, so the agent's camera is a real WebRTC video stream.")
    ap.add_argument("--pixelstream-streamer-id", default="AgentTrain",
                    help="StreamerId the render client registers under (the web player selects it).")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    episodes = _load_episodes(args.episodes_file)
    log.info("loaded %d episodes from %s", len(episodes), args.episodes_file)

    # Pin the render client to a specific GPU so it isn't starved on a busy box (a
    # GPU-throttled client can't tick its net driver fast enough to finish the join
    # handshake → ConnectionTimeout). -graphicsadapter selects the Vulkan device.
    extra_client_args = []
    if args.client_gpu is not None:
        extra_client_args.append(f"-graphicsadapter={args.client_gpu}")
    # On a loaded box the client boots slowly (cold shader compile / GPU contention) and
    # can blow past UE's default 60s net-connect timeout before it finishes joining the
    # server. Raise InitialConnectTimeout/ConnectionTimeout so a slow-but-progressing boot
    # still completes the handshake.
    extra_client_args.append(
        "-ini:Engine:[/Script/OnlineSubsystemUtils.IpNetDriver]:InitialConnectTimeout=600")
    extra_client_args.append(
        "-ini:Engine:[/Script/OnlineSubsystemUtils.IpNetDriver]:ConnectionTimeout=600")
    # Real-time agent-camera video: enable PixelStreaming on the render client (it's disabled
    # by default only because clients normally don't need it; SimWorldRuntime references the
    # plugin header-only so enabling it is shader-safe). The client streams its main viewport —
    # which run_episode points at the agent via SetViewTargetWithBlend — to a cirrus streamer.
    disable_plugins = "UnrealCV,EnhancedInput,PixelStreaming"
    pixelstream = args.pixelstream_ws_port is not None
    if pixelstream:
        disable_plugins = "UnrealCV,EnhancedInput"
        # Keep the client headless/offscreen (no display on this box) — PixelStreaming streams
        # the offscreen-rendered viewport, exactly like the bound editor's -RenderOffScreen +
        # EditorPixelStreaming. Only add the streamer URL + codec; the launcher handles the rest.
        extra_client_args += [
            f"-PixelStreamingURL=ws://127.0.0.1:{args.pixelstream_ws_port}",
            f"-PixelStreamingStreamerId={args.pixelstream_streamer_id}",
            "-PixelStreamingEncoderCodec=H264",
        ]
    spec = ClusterSpec(
        cluster_id="spear-train", map_path=args.map,
        ue_port=args.ue_port, spear_port=args.spear_port,
        n_clients=1, headless_clients=True,
        launch_mode="editor-server", render_offscreen=False,
        client_disable_plugins=disable_plugins,
        log_dir=Path(args.log_dir), beacon_listen_port=args.beacon_port,
        client_spear_port=args.client_spear_port, stepping_mode="async",
        extra_client_args=extra_client_args,
        client_launch_retries=2,                 # transient GPU-starvation → retry the client
        client_travel_ready_timeout_sec=300.0,   # allow a slow client to finish booting
        server_spear_ready_timeout_sec=300.0,    # server boot is slow on a loaded box
        net_driver_ready_timeout_sec=300.0,      # ...and so is its GameNetDriver bind
    )
    cluster = Cluster(spec)

    # On SIGTERM/SIGINT (e.g. the Studio backend cancelling a run) force-kill the UE
    # children and exit hard — otherwise the cluster's children would be orphaned.
    def _on_signal(signum, _frame):
        log.warning("signal %d received; tearing down cluster", signum)
        _force_kill_cluster(cluster, args.log_dir)
        os._exit(143)
    for _sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(_sig, _on_signal)
        except Exception:
            pass

    # Announce the run to the datahub BEFORE the (~3 min) cluster boot, so the web UI shows
    # it immediately as "booting" instead of a blank panel until the first episode.
    run_id = args.run_prefix
    n_epochs = max(1, args.epochs)
    hub = DataHub(args.datahub or (Path(args.root) / "datahub.jsonl"))
    hub.append({"kind": "run_start", "runId": run_id, "model": args.model_id,
                "tasks": len(episodes), "epochs": n_epochs, "maxSteps": args.max_steps,
                "memory": "text" if (args.memory_file and not args.memory_readonly) else
                          ("frozen" if args.memory_file else "none"), "backend": "spear_cluster"})
    n_success = 0

    log.info("starting 5.8 cluster (server + 1 render client)...")
    cluster.start()
    log.info("cluster up; connecting SPEAR control :%d + camera :%d", args.spear_port, args.client_spear_port)
    memory = NavMemory(args.memory_file, readonly=args.memory_readonly) if args.memory_file else None
    if memory is not None:
        log.info("memory: %d lesson(s) loaded from %s (%s)", len(memory.lessons),
                 args.memory_file, "frozen" if memory.readonly else "updating")
    try:
        ctrl = _connect(args.spear_port)
        cam_inst = _connect(args.client_spear_port)
        policy = GPTNavPolicy(args.model_id)
        live_path = str(Path(args.root) / f"{run_id}_live.jpg")  # real-time agent-camera broadcast
        # One RUN = N EPOCHS; each epoch runs every TASK once. Memory carries across epochs, so
        # success rate per epoch is the learning curve.
        for epoch in range(n_epochs):
            ep_success = 0
            for i, ep in enumerate(episodes):
                run_name = f"{args.run_prefix}_ep{epoch}_e{i}"
                log.info("=== epoch %d/%d · task %d/%d (%s) ===", epoch + 1, n_epochs, i + 1, len(episodes), run_name)
                try:
                    s = run_episode(ctrl, cam_inst, ep, max_steps=args.max_steps, policy=policy,
                                    root=args.root, run_name=run_name, cam_w=args.cam_w, cam_h=args.cam_h,
                                    memory=memory, hub=hub, run_id=run_id, model_id=args.model_id,
                                    idx=i, epoch=epoch, live_path=live_path)
                    ok = int((s or {}).get("SR", 0) >= 1.0)
                    ep_success += ok
                    n_success += ok
                except Exception as exc:
                    log.exception("task %s failed: %s", run_name, exc)
            hub.append({"kind": "epoch_end", "runId": run_id, "epoch": epoch,
                        "tasks": len(episodes), "success": ep_success,
                        "SR": round(ep_success / max(1, len(episodes)), 3),
                        "lessons": len(memory.lessons) if memory is not None else 0})
            log.info("=== epoch %d done: %d/%d (SR %.0f%%) ===", epoch, ep_success, len(episodes),
                     100 * ep_success / max(1, len(episodes)))
    finally:
        hub.append({"kind": "run_end", "runId": run_id, "model": args.model_id,
                    "epochs": n_epochs, "tasks": len(episodes), "success": n_success,
                    "SR": round(n_success / max(1, n_epochs * len(episodes)), 3)})
        # HARD backstop: force-kill UE children + os._exit after 45s no matter what, so a hung
        # cluster.stop()/PixelStreaming shutdown can never leave the runner alive (which would
        # block the next run via the single-cluster guard).
        def _hard_exit():
            time.sleep(45)
            log.warning("teardown exceeded 45s — force-killing cluster + hard exit")
            _force_kill_cluster(cluster, args.log_dir)
            os._exit(0)
        threading.Thread(target=_hard_exit, daemon=True).start()
        _teardown(cluster, args.log_dir)
    return 0


if __name__ == "__main__":
    rc = main()
    # Hard-exit: a lingering non-daemon SPEAR keepalive thread can otherwise keep the
    # process alive after main() returns, leaving the Studio job stuck in "running".
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(rc)
