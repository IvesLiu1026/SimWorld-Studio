"""5.8 / SPEAR navigation training runner (replaces the 5.3 / UnrealCV batch_runner).

UE 5.8 controls agents via **SPEAR** (msgpack-rpc), not UnrealCV.  The correct
topology is a cluster: one **server** computes physics (nullrhi) and one
**render client** renders cameras; Python connects to the *server* SPEAR port for
control and to the *client* SPEAR port for camera pixels (see
SimWorld-SPEAR `utils/cluster_launcher` + `docs/ARCHITECTURE_CLUSTER.md`).

This runner:
  1. boots ONE cluster (server + 1 render client) for the whole run,
  2. for each navigation episode: spawns a SpHumanoidAgent, drives it with an
     LLM (GPT-4o vision) reading the agent's camera, and
  3. writes Studio-compatible output via `gym_env.logger.EpisodeLogger`
     (episode.jsonl / llm_raw.jsonl / frames / summary.json) + prints
     ``batch output dir: <dir>`` so `web/server/training.js` can tail it and the
     Agent Training panel's live charts populate unchanged.

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
import sys
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

from .logger import EpisodeLogger  # noqa: E402

log = logging.getLogger("spear_nav_runner")

# Movement geometry knobs (demo maps are cm-scale).
_FORWARD_TICKS = 6        # server frames per MoveForward step (~one "stride")
_ROTATE_DEG = 30.0        # degrees per TURN action
_ACTIONS = ("MOVE_FORWARD", "TURN_LEFT", "TURN_RIGHT", "STOP")

_NAV_SYSTEM_PROMPT = (
    "You are an embodied navigation agent in a 3D city scene. You see a "
    "first-person RGB image plus the bearing (degrees) and distance (cm) to the "
    "goal. Choose ONE action: MOVE_FORWARD (walk ~2.5 m), TURN_LEFT (30 deg left), "
    "TURN_RIGHT (30 deg right), STOP (declare arrival). STOP when distance < the "
    "success radius. Reply with EXACTLY ONE action name on its own line, nothing else."
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

    def act(self, rgb: np.ndarray, distance_cm: float, bearing_deg: float, radius_cm: float):
        png = _rgb_to_png_b64(rgb)
        user = (
            f"Goal: distance={distance_cm:.0f} cm, bearing={bearing_deg:+.0f} deg "
            f"(positive = right). Success radius = {radius_cm:.0f} cm. Pick one action."
        )
        t0 = time.time()
        resp = self.client.chat.completions.create(
            model=self.model_id,
            messages=[
                {"role": "system", "content": _NAV_SYSTEM_PROMPT},
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


# --------------------------------------------------------------------------- #
# SPEAR helpers
# --------------------------------------------------------------------------- #


def _rgb_to_png_b64(rgb: np.ndarray) -> str:
    from PIL import Image

    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _unpack_xyz(v) -> tuple[float, float, float]:
    if isinstance(v, dict):
        return float(v.get("X", v.get("x", 0))), float(v.get("Y", v.get("y", 0))), float(v.get("Z", v.get("z", 0)))
    return float(v[0]), float(v[1]), float(v[2])


def _capture(cam_inst, cam, w: int, h: int) -> np.ndarray | None:
    """Read one RGB frame from the render client's bound SceneCapture.

    ``read_pixels`` must run inside the client's ``end_frame`` (SPEAR receive phase).
    """
    if cam is None:
        return None
    try:
        with cam_inst.begin_frame():
            pass
        with cam_inst.end_frame():
            return _read_camera_rgb(cam, w, h)
    except Exception as exc:
        log.warning("camera capture failed: %s", exc)
        return None


def _read_camera_rgb(cam, w: int, h: int) -> np.ndarray | None:
    try:
        bundle = cam.read_pixels()
        arr = np.asarray(bundle["arrays"]["data"], dtype=np.uint8)
        if arr.ndim == 3 and arr.shape[2] >= 3:
            return np.ascontiguousarray(arr[:, :, :3])
        if arr.size >= w * h * 3:
            c = arr.size // (w * h)
            return np.ascontiguousarray(arr[: w * h * c].reshape((h, w, c))[:, :, :3])
    except Exception as exc:
        log.warning("camera read failed: %s", exc)
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


def run_episode(ctrl, cam_inst, ep, *, max_steps, policy, root, run_name, cam_w, cam_h):
    ep_id = str(_ep_field(ep, "episode_id", "id", default=run_name))
    start = _ep_field(ep, "start_position", "start", "spawn", default={"X": 0, "Y": 0, "Z": 100})
    goal = _ep_field(ep, "goal_position", "goal", "target", default={"X": 500, "Y": 0, "Z": 100})
    sx, sy, sz = _unpack_xyz(start)
    gx, gy, _gz = _unpack_xyz(goal)
    radius = float(_ep_field(ep, "success_radius_cm", "radius_cm", default=200.0))

    us = ctrl._unreal_service
    with ctrl.begin_frame():
        us.initialize()
        uclass = us.get_static_class(uclass="ASpHumanoidAgent")
        us.set_world(world=ctrl._get_game_world())
        actor = us.spawn_actor(
            uclass=uclass, location={"X": sx, "Y": sy, "Z": sz},
            rotation={"Pitch": 0.0, "Yaw": 0.0, "Roll": 0.0},
            spawn_parameters={"SpawnCollisionHandlingOverride": "AlwaysSpawn"},
            with_sp_funcs=True)
        if actor is not None:
            actor.ConfigureCamera(InWidth=cam_w, InHeight=cam_h, InFovDegrees=90.0)
            actor.InitializeCamera()
    with ctrl.end_frame():
        pass
    if actor is None:
        log.error("episode %s: spawn failed", ep_id)
        return {"SR": 0.0, "SPL": 0.0, "steps": 0, "ended_reason": "spawn_error", "episode_id": ep_id}

    # Bind the replicated agent's camera on the render client (match by class).
    cam = _bind_client_camera(cam_inst, cam_w, cam_h)

    logger = EpisodeLogger(run_name, root=root, save_frames=True, timestamp_dir=False,
                           meta={"episode_id": ep_id, "backend": "spear_5_8"})
    path_len = 0.0
    prev = (sx, sy)
    ended_reason = "max_steps"
    success = False
    x, y = sx, sy
    t = 0
    for t in range(max_steps):
        # SPEAR frame model: SEND actions in begin_frame, READ state in end_frame.
        # One observe-frame: advance + read the agent pose.
        with ctrl.begin_frame():
            pass
        with ctrl.end_frame():
            x, y, _z = _unpack_xyz(actor.Agent_GetLocation())
        rgb = _capture(cam_inst, cam, cam_w, cam_h)
        path_len += math.hypot(x - prev[0], y - prev[1])
        prev = (x, y)

        dx, dy = gx - x, gy - y
        dist = math.hypot(dx, dy)
        bearing = math.degrees(math.atan2(dy, dx))
        success = dist <= radius

        if success:
            action, llm = "STOP", _LLMResp("STOP", "reached goal", {})
            ended_reason = "success"
        else:
            frame = rgb if rgb is not None else np.zeros((cam_h, cam_w, 3), np.uint8)
            action, llm, _ = policy.act(frame, dist, bearing, radius)

        info = {
            "episode_id": ep_id, "step": t, "action_name": action,
            "distance_to_goal_cm": dist, "path_length_cm": path_len,
            "success": success, "agent_xy": [x, y],
        }
        logger.log_step(t, {"tool": action}, {"rgb": rgb} if rgb is not None else {},
                        reward=-dist / 1000.0, done=success, truncated=False, info=info)
        logger.log_llm(t, policy.model_id, llm)
        if success:
            break
        if action == "STOP":
            ended_reason = "stopped"
            break

        # Apply the chosen action over a few frames (sustained-input semantics).
        n_ticks = _FORWARD_TICKS if action == "MOVE_FORWARD" else 2
        with ctrl.begin_frame():
            if action == "MOVE_FORWARD":
                actor.Agent_MoveForward()  # no-arg: sustained walk (matches e2e)
            elif action == "TURN_LEFT":
                actor.Agent_Rotate(AngleDeg=_ROTATE_DEG, Direction="left")
            elif action == "TURN_RIGHT":
                actor.Agent_Rotate(AngleDeg=_ROTATE_DEG, Direction="right")
        with ctrl.end_frame():
            pass
        for _ in range(n_ticks - 1):
            with ctrl.begin_frame():
                pass
            with ctrl.end_frame():
                pass
        if action == "MOVE_FORWARD":  # halt sustained walk before next observation
            with ctrl.begin_frame():
                actor.Agent_StopAgent()
            with ctrl.end_frame():
                pass

    # Tear down the agent so the next episode starts clean.
    try:
        with ctrl.begin_frame():
            actor.Agent_StopAgent()
            us.destroy_actor(actor=actor)
        with ctrl.end_frame():
            pass
    except Exception:
        pass

    straight = math.hypot(gx - sx, gy - sy)
    spl = (straight / max(path_len, straight)) if success else 0.0
    summary = {"SR": 1.0 if success else 0.0, "SPL": spl, "steps": t + 1,
               "path_length_cm": path_len, "ended_reason": ended_reason, "episode_id": ep_id}
    logger.log_summary(summary)
    print(f"batch output dir: {Path(root).resolve()}", flush=True)
    log.info("episode %s done: success=%s steps=%d dist_final=%.0f", ep_id, success, t + 1, dist)
    return summary


def _advance(inst, n=1):
    for _ in range(n):
        with inst.begin_frame():
            pass
        with inst.end_frame():
            pass


def _bind_client_camera(cam_inst, w, h, *, settle_frames=12, retries=8):
    """Find the replicated SpHumanoidAgent on the render client + bind its SceneCapture.

    The agent is spawned server-side and takes a few client frames to replicate,
    so we let the client settle, then poll find_actors_by_class with retries
    (mirrors cluster_launcher/test_e2e_multiagent.py).
    """

    us = cam_inst._unreal_service
    try:
        with cam_inst.begin_frame():
            us.initialize()
            us.set_world(world=cam_inst._get_game_world())
        with cam_inst.end_frame():
            pass
    except Exception as exc:
        log.warning("client unreal_service init failed: %s", exc)
        return None
    _advance(cam_inst, settle_frames)

    for attempt in range(retries):
        comp = None
        try:
            with cam_inst.begin_frame():
                actors = us.find_actors_by_class(uclass="ASpHumanoidAgent", with_sp_funcs=True) or []
                actor = actors[0] if actors else None
                if actor is not None:
                    comp = us.get_component_by_class(
                        actor=actor, uclass="USpSceneCaptureComponent2D", with_sp_funcs=True)
                    if comp is not None:
                        try:
                            comp.Width, comp.Height = w, h
                        except Exception:
                            pass
                        comp.Initialize()
                        comp.initialize_sp_funcs()
            with cam_inst.end_frame():
                pass
            if comp is not None:
                log.info("client camera bound (attempt %d)", attempt + 1)
                return comp
        except Exception as exc:
            log.warning("camera bind attempt %d: %s", attempt + 1, exc)
        _advance(cam_inst, 4)
    log.warning("client camera bind failed after %d retries; proceeding without RGB", retries)
    return None


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
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    episodes = _load_episodes(args.episodes_file)
    log.info("loaded %d episodes from %s", len(episodes), args.episodes_file)

    spec = ClusterSpec(
        cluster_id="spear-train", map_path=args.map,
        ue_port=args.ue_port, spear_port=args.spear_port,
        n_clients=1, headless_clients=True,
        launch_mode="editor-server", render_offscreen=False,
        log_dir=Path(args.log_dir), beacon_listen_port=args.beacon_port,
        client_spear_port=args.client_spear_port, stepping_mode="async",
    )
    cluster = Cluster(spec)
    log.info("starting 5.8 cluster (server + 1 render client)...")
    cluster.start()
    log.info("cluster up; connecting SPEAR control :%d + camera :%d", args.spear_port, args.client_spear_port)
    try:
        ctrl = _connect(args.spear_port)
        cam_inst = _connect(args.client_spear_port)
        policy = GPTNavPolicy(args.model_id)
        for i, ep in enumerate(episodes):
            run_name = f"{args.run_prefix}_e{i}"
            log.info("=== episode %d/%d (%s) ===", i + 1, len(episodes), run_name)
            try:
                run_episode(ctrl, cam_inst, ep, max_steps=args.max_steps, policy=policy,
                            root=args.root, run_name=run_name, cam_w=args.cam_w, cam_h=args.cam_h)
            except Exception as exc:
                log.exception("episode %s failed: %s", run_name, exc)
    finally:
        try:
            cluster.stop()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
