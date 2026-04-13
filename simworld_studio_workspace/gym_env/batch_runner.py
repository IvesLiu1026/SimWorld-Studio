"""Batch episode runner using ghost-mode agents.

One UE instance, N ghost agents running N tasks concurrently.
LLM calls are either batched (vLLM ``/v1/chat/completions`` with
multiple independent requests) or sequential.

Modes
-----
* **batch** (default): ghost agents, no trajectory saving, RGB on.
  Task generator produces a pool of episodes; agents run them in
  parallel waves.
* **single**: normal (non-ghost) agent, trajectory + frames saved.

Usage::

    # Batch: 6 tasks, 3 concurrent ghost agents per wave
    python -m gym_env.batch_runner --mode batch --n-tasks 6 --wave-size 3 \\
        --model qwen --base-url http://localhost:8000/v1

    # Single with trajectory
    python -m gym_env.batch_runner --mode single --n-tasks 1 --model claude
"""

from __future__ import annotations

import argparse
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from nav_task.episode import NavigationEpisode
from nav_task.task_spec import make_task_prompt

from .action_space import nav_tool_schemas, translate_action, is_stop_action
from .ghost import enable_ghost, disable_ghost, teleport_ghost, GHOST_CHANNEL
from .llm import LLMClient, LLMMessage, LLMResponse, ToolCall, make_llm
from .logger import EpisodeLogger
from .memory import NullMemory
from .observation import ObservationBuilder
from .simworld_nav_env import SimWorldNavEnv
from .ucv_client import UCVClient
from .mcp_client import MCPClient

log = logging.getLogger(__name__)

_HUMANOID_BP = "/Game/TrafficSystem/Pedestrian/Base_User_Agent.Base_User_Agent_C"
_DEFAULT_SPAWN_Z = 110.0

_NAV_SYSTEM_PROMPT = """You are an embodied navigation agent in a 3D city scene.

You receive a first-person RGB image and a goal description.  Choose
ONE navigation action per turn:

  - MOVE_FORWARD : walk forward ~2 seconds (~200-400 cm)
  - TURN_LEFT    : rotate 30 degrees left
  - TURN_RIGHT   : rotate 30 degrees right
  - STOP         : declare you have reached the goal

Each step you receive the bearing to the goal (degrees) and distance.
Discover the bearing sign convention by observing your TURN effects.
STOP when distance < 200 cm.

Think briefly, then call exactly one tool."""


# ---------------------------------------------------------------------------
# Ghost agent slot — holds per-agent state for one concurrent task
# ---------------------------------------------------------------------------

@dataclass
class AgentSlot:
    """Mutable state for one ghost agent running one episode."""
    idx: int
    agent_name: str
    episode: NavigationEpisode
    env: SimWorldNavEnv
    history: List[LLMMessage] = field(default_factory=list)
    step: int = 0
    done: bool = False
    metrics: Dict[str, Any] = field(default_factory=dict)
    task_prompt: str = ""
    _obs: Dict[str, Any] = field(default_factory=dict)
    _info: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_user_text(info: Dict[str, Any], obs: Dict[str, Any]) -> str:
    parts: List[str] = []
    parts.append(f"Task: {info.get('task_prompt', '')}")
    if "pointgoal_with_gps_compass" in obs:
        d, ang = obs["pointgoal_with_gps_compass"].tolist()
        parts.append(f"Goal: distance={d:.0f} cm, bearing={math.degrees(ang):+.0f} deg")
    parts.append(
        f"Position: ({obs['agent_xy'][0]:.0f}, {obs['agent_xy'][1]:.0f})"
        f"  yaw={obs['agent_yaw_deg']:+.0f} deg"
    )
    parts.append(f"Step: {info['step']}")
    return "\n".join(parts)


def _strip_images(messages: List[LLMMessage], keep_last_k: int) -> None:
    if keep_last_k is None:
        return
    user_turns = [
        i for i, m in enumerate(messages)
        if m.role == "user" and any(b["type"] == "image" for b in m.content)
    ]
    if len(user_turns) <= keep_last_k:
        return
    for i in user_turns[:-keep_last_k]:
        messages[i].content = [
            b if b["type"] == "text" else {"type": "text", "text": "[image omitted]"}
            for b in messages[i].content
        ]


def _spawn_ghost_agent(ucv: UCVClient, name: str, x: float, y: float, z: float) -> None:
    """Spawn a humanoid, enable ghost mode, teleport to position."""
    ucv.spawn_bp_asset(_HUMANOID_BP, name)
    enable_ghost(ucv, name)
    teleport_ghost(ucv, name, x, y, z)
    try:
        ucv.send(f"vset /object/{name}/scale 1 1 1")
        ucv.send(f"vset /object/{name}/object_mobility true")
        ucv.vbp(name, "SetMaxSpeed 200")
        ucv.vbp(name, "EnableController true")
    except Exception as exc:
        log.warning("ghost agent %s post-spawn config: %s", name, exc)


# ---------------------------------------------------------------------------
# Core: run one wave of ghost agents
# ---------------------------------------------------------------------------

def run_wave(
    ucv: UCVClient,
    mcp: Optional[MCPClient],
    llm: LLMClient,
    episodes: List[NavigationEpisode],
    *,
    wave_offset: int = 0,
    max_steps: int = 40,
    vision_depth: int = 3,
    spawn_z: float = _DEFAULT_SPAWN_Z,
) -> List[Dict[str, Any]]:
    """Run a wave of ghost agents concurrently in one UE instance.

    Each agent gets its own ``SimWorldNavEnv`` with a unique name and
    camera ID.  LLM requests are issued sequentially (one per active
    agent per step).  For vLLM batch support, override with a batched
    LLM client.

    Returns a list of final metrics dicts, one per episode.
    """
    n = len(episodes)
    log.info("wave: %d ghost agents, max_steps=%d", n, max_steps)

    # --- Phase 1: spawn all ghost agents ---
    slots: List[AgentSlot] = []
    for i, ep in enumerate(episodes):
        agent_name = f"GhostAgent_{wave_offset + i}"
        start = ep.start_position

        log.info("wave: spawning %s for episode %s", agent_name, ep.episode_id)
        _spawn_ghost_agent(ucv, agent_name, start.x, start.y, spawn_z)

        # Each ghost agent gets its own env (shares UCVClient but has
        # its own agent_name, camera, reward state).
        # camera_id follows spawn order: first humanoid → 0, second → 1, etc.
        # This matches the FusionCamSensor registration order in UE.
        env = SimWorldNavEnv(
            ucv_client=ucv,
            mcp_client=mcp,
            agent_name=agent_name,
            camera_id=i,            # spawn-order camera index
            capture_rgb=True,
            spawn_on_reset=False,   # already spawned as ghost
            ensure_pie=False,       # PIE already running
            spawn_z=spawn_z,
        )
        # Mark as already spawned so reset() doesn't re-spawn
        env._spawned = True

        slot = AgentSlot(
            idx=i,
            agent_name=agent_name,
            episode=ep,
            env=env,
        )
        slot.history = [LLMMessage.text("system", _NAV_SYSTEM_PROMPT)]
        slots.append(slot)

    # --- Phase 2: reset all envs ---
    for slot in slots:
        obs, info = slot.env.reset(slot.episode)
        slot.task_prompt = info.get("task_prompt", "")
        # Store initial obs/info for first LLM call
        slot._obs = obs
        slot._info = info

    time.sleep(2)  # let cameras initialize

    # --- Phase 3: step loop ---
    for t in range(1, max_steps + 1):
        active = [s for s in slots if not s.done]
        if not active:
            break

        for slot in active:
            slot.step = t
            obs = slot._obs
            info = slot._info
            info["task_prompt"] = slot.task_prompt

            user_text = _build_user_text(info, obs)
            rgb = obs.get("rgb")
            if rgb is not None and getattr(llm, "name", "") != "claude-sdk":
                slot.history.append(LLMMessage.user_with_image(user_text, rgb))
            else:
                slot.history.append(LLMMessage.text("user", user_text))
            _strip_images(slot.history, vision_depth)

            # LLM call (sequential per agent — batch via vLLM is TODO)
            try:
                resp = llm.chat(slot.history, nav_tool_schemas(), max_tokens=1024)
            except Exception as exc:
                log.error("LLM error for %s: %s", slot.agent_name, exc)
                slot.done = True
                continue

            if not resp.tool_calls:
                log.info("%s: LLM returned no tool calls at t=%d", slot.agent_name, t)
                slot.done = True
                continue

            slot.history.append(LLMMessage(
                role="assistant",
                content=[{"type": "text", "text": resp.text or ""}],
                tool_calls=resp.tool_calls,
            ))

            for tc in resp.tool_calls:
                obs, reward, done, truncated, info = slot.env.step(tc.to_action_dict())
                slot.history.append(LLMMessage(
                    role="tool",
                    tool_call_id=tc.id,
                    content=[{"type": "text", "text": (
                        f"reward={reward:+.3f} "
                        f"d_goal={info['distance_to_goal_cm']:.0f}cm"
                    )}],
                ))
                if done or truncated:
                    slot.metrics = info.get("metrics", {}) or {}
                    slot.done = True
                    break

            slot._obs = obs
            slot._info = info

        n_done = sum(1 for s in slots if s.done)
        log.info("wave t=%d: %d/%d done", t, n_done, n)

    # --- Phase 4: collect results & cleanup ---
    results = []
    for slot in slots:
        sr = slot.metrics.get("SR", 0)
        log.info(
            "%s episode=%s SR=%.0f steps=%d",
            slot.agent_name, slot.episode.episode_id, sr, slot.step,
        )
        results.append({
            "episode_id": slot.episode.episode_id,
            "agent_name": slot.agent_name,
            "SR": sr,
            "SPL": slot.metrics.get("SPL", 0),
            "SoftSPL": slot.metrics.get("SoftSPL", 0),
            "steps": slot.step,
            "metrics": slot.metrics,
        })
        # Destroy ghost agent
        try:
            ucv.send(f"vset /object/{slot.agent_name}/destroy")
        except Exception:
            pass

    return results


# ---------------------------------------------------------------------------
# Single mode (normal agent, with trajectory)
# ---------------------------------------------------------------------------

def run_single(
    ucv: UCVClient,
    mcp: Optional[MCPClient],
    llm: LLMClient,
    episode: NavigationEpisode,
    *,
    max_steps: int = 40,
    vision_depth: int = 3,
    run_name: Optional[str] = None,
    spawn_z: float = _DEFAULT_SPAWN_Z,
) -> Dict[str, Any]:
    """Run a single episode with normal (non-ghost) agent + trajectory saving."""
    from .runner import run_episode
    from .memory import NullMemory

    env = SimWorldNavEnv(
        ucv_client=ucv,
        mcp_client=mcp,
        agent_name="GymNavAgent_0",
        capture_rgb=True,
        spawn_z=spawn_z,
        ensure_pie=True,
    )
    logger = EpisodeLogger(
        run_id=run_name or f"single_{episode.episode_id}",
        save_frames=True,
        annotate_frames=True,
    )
    metrics = run_episode(
        env, llm, episode, logger,
        memory=NullMemory(),
        max_steps=max_steps,
        vision_history_depth=vision_depth,
    )
    return metrics


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m gym_env.batch_runner",
        description="Batch or single episode runner with ghost-mode support.",
    )
    p.add_argument("--mode", choices=["batch", "single"], default="batch",
                   help="batch = ghost agents, no traj; single = normal agent, save traj")
    p.add_argument("--n-tasks", type=int, default=4,
                   help="Total number of tasks to generate and run")
    p.add_argument("--wave-size", type=int, default=3,
                   help="Concurrent ghost agents per wave (batch mode)")
    p.add_argument("--model", default="qwen")
    p.add_argument("--model-id", default=None)
    p.add_argument("--base-url", default=None)
    p.add_argument("--api-key", default=None)
    p.add_argument("--ucv-host", default="127.0.0.1")
    p.add_argument("--ucv-port", type=int, default=9000)
    p.add_argument("--mcp-host", default="127.0.0.1")
    p.add_argument("--mcp-port", type=int, default=55557)
    p.add_argument("--task", choices=["pointnav", "objectnav"], default="pointnav")
    p.add_argument("--scene-graph", default=None)
    p.add_argument("--nav-min-cm", type=float, default=1000.0)
    p.add_argument("--nav-max-cm", type=float, default=4000.0)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--max-steps", type=int, default=40)
    p.add_argument("--vision-depth", type=int, default=3)
    p.add_argument("--run-name", default=None)
    p.add_argument("--log-level", default="INFO")
    return p


def main(argv: Optional[List[str]] = None) -> None:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
    )

    from .episode_builder import (
        sample_pointnav_episode,
        sample_pointnav_episode_navmesh,
    )

    ucv = UCVClient(host=args.ucv_host, port=args.ucv_port, name="batch-0")
    ucv.connect()
    mcp = MCPClient(host=args.mcp_host, port=args.mcp_port, name="batch-mcp")

    llm = make_llm(
        args.model,
        model=args.model_id,
        base_url=args.base_url,
        api_key=args.api_key,
    )

    # --- Generate episodes ---
    log.info("Generating %d %s episodes (seed=%d)", args.n_tasks, args.task, args.seed)
    episodes: List[NavigationEpisode] = []
    for i in range(args.n_tasks):
        seed = args.seed + i
        if args.scene_graph:
            result = sample_pointnav_episode_navmesh(
                ucv, args.scene_graph,
                seed=seed, idx=i,
                min_geodesic_cm=args.nav_min_cm,
                max_geodesic_cm=args.nav_max_cm,
            )
        else:
            result = sample_pointnav_episode(ucv, seed=seed, idx=i)
        episodes.append(result["episode"])
        log.info("  episode %d: %s", i, result["episode"].episode_id)

    # --- Run ---
    if args.mode == "single":
        log.info("=== SINGLE MODE: 1 episode, normal agent, trajectory saved ===")
        metrics = run_single(
            ucv, mcp, llm, episodes[0],
            max_steps=args.max_steps,
            vision_depth=args.vision_depth,
            run_name=args.run_name,
        )
        print(f"\n[SINGLE] {metrics}")
    else:
        log.info("=== BATCH MODE: %d tasks, wave_size=%d, ghost agents ===",
                 args.n_tasks, args.wave_size)
        all_results = []
        for wave_start in range(0, len(episodes), args.wave_size):
            wave_eps = episodes[wave_start:wave_start + args.wave_size]
            log.info("--- Wave %d-%d ---", wave_start, wave_start + len(wave_eps) - 1)
            results = run_wave(
                ucv, mcp, llm, wave_eps,
                wave_offset=wave_start,
                max_steps=args.max_steps,
                vision_depth=args.vision_depth,
            )
            all_results.extend(results)

        # Summary
        n_success = sum(1 for r in all_results if r.get("SR", 0) > 0)
        avg_spl = sum(r.get("SPL", 0) for r in all_results) / len(all_results)
        print(f"\n{'='*50}")
        print(f"BATCH RESULTS: {len(all_results)} episodes")
        print(f"  SR:  {n_success}/{len(all_results)} ({100*n_success/len(all_results):.0f}%)")
        print(f"  SPL: {avg_spl:.3f}")
        print(f"{'='*50}")
        for r in all_results:
            print(f"  {r['episode_id']}: SR={r['SR']:.0f} SPL={r['SPL']:.3f} steps={r['steps']}")

    ucv.disconnect()


if __name__ == "__main__":
    main()
