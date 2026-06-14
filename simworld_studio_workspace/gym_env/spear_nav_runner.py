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
_FORWARD_TICKS = 6        # server frames per MoveForward step (~one "stride")
_ROTATE_DEG = 30.0        # degrees per TURN action
_ACTIONS = ("MOVE_FORWARD", "TURN_LEFT", "TURN_RIGHT", "STOP")
# Blueprint class of the replicated agent proxy on the render client. SPEAR's
# find_actors_by_class does NOT match BP subclasses through a C++ base, so the
# client proxy must be located by this exact BP class (matches simworld.recording).
PEDESTRIAN_BP = "/Game/Agent/Pedestrian/Core/BP_PedestrianAgentBase.BP_PedestrianAgentBase_C"

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

    # Bind the agent's OWN recording camera on the render client (ConfigureCamera /
    # InitializeCamera / GetRecordingCamera are driven on the replicated proxy).
    cam = _bind_client_camera(cam_inst, (sx, sy), cam_w, cam_h)

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
            x, y, _z = _loc_xyz(_complete(actor.K2_GetActorLocation()))
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

        # Apply the action. ASpPedestrianAgentBase control surface (no Agent_ prefix):
        #   MoveForward(Scale) is AddMovementInput-style — HELD every tick;
        #   Rotate(DeltaYawDegrees) is a one-shot yaw delta (left = negative).
        if action == "MOVE_FORWARD":
            for _ in range(_FORWARD_TICKS):
                with ctrl.begin_frame():
                    actor.MoveForward(Scale=1.0)
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
        log.info("agent recording camera bound (%dx%d)", w, h)
        return comp
    except Exception as exc:
        log.warning("client camera bind aborted (%s); proceeding without RGB", exc)
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
