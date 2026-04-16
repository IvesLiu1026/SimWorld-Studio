"""Batch episode runner using ghost-mode agents.

One UE instance, N ghost agents running N tasks concurrently.
LLM calls are either batched (vLLM ``/v1/chat/completions`` with
multiple independent requests) or sequential.

Modes
-----
* **batch** (default): ghost agents, RGB on, optional memory + WandB.
  Task generator produces a pool of episodes; agents run them in
  parallel waves.
* **single**: normal (non-ghost) agent, trajectory + frames saved.

Usage::

    # Batch: 6 tasks, 3 concurrent ghost agents per wave
    python -m gym_env.batch_runner --mode batch --n-tasks 6 --wave-size 3 \\
        --model qwen --base-url http://localhost:8000/v1

    # With memory enabled
    python -m gym_env.batch_runner --mode batch --n-tasks 6 --wave-size 3 \\
        --model qwen --memory strategy

    # Single with trajectory
    python -m gym_env.batch_runner --mode single --n-tasks 1 --model claude
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from nav_task.episode import NavigationEpisode
from tqdm.auto import tqdm

from .action_space import nav_tool_schemas
from .llm import LLMClient, LLMMessage, make_llm
from .logger import EpisodeLogger
from .memory import AgentMemory, NullMemory, ReadOnlyMemory, build_memory
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
    logger: Optional[EpisodeLogger] = None
    history: List[LLMMessage] = field(default_factory=list)
    step: int = 0
    done: bool = False
    ended_reason: str = "max_steps"  # "success" | "truncated" | "max_steps" | "llm_error" | "no_tool_call"
    cumulative_reward: float = 0.0
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


def _configure_ghost_ucv(ucv: UCVClient, name: str, x: float, y: float, z: float) -> None:
    """Configure ghost mode via UnrealCV: hide, collision channels, teleport.

    Collision is left **disabled** — caller must call
    :func:`_finalize_ghost_agents` after all agents are configured.
    """
    ghost_cmds = [
        (f"vset /object/{name}/collision false", "disable collision"),
        (f"vset /object/{name}/hide", "hide"),
        (f"vset /object/{name}/collision_channel 8", "set channel 8"),
        (f"vset /object/{name}/collision_response 8 ignore", "ignore ghosts"),
        (f"vset /object/{name}/collision_response 2 ignore", "ignore pawns"),
    ]
    for cmd, desc in ghost_cmds:
        try:
            resp = ucv.send(cmd)
            log.info("ghost %s %s: %r", name, desc, resp)
        except Exception as exc:
            log.error("ghost %s %s FAILED: %s", name, desc, exc)

    # Teleport to target position (collision is off, no depenetration)
    try:
        ucv.send(f"vset /object/{name}/location {x} {y} {z}")
        actual_loc = ucv.send(f"vget /object/{name}/location")
        log.info("ghost %s teleported to (%.0f,%.0f,%.0f), actual: %s",
                 name, x, y, z, actual_loc.strip())
    except Exception as exc:
        log.error("ghost %s teleport FAILED: %s", name, exc)

    try:
        ucv.vbp(name, "SetMaxSpeed 200")
        ucv.vbp(name, "EnableController true")
    except Exception as exc:
        log.warning("ghost %s post-spawn config: %s", name, exc)


def _finalize_ghost_agents(ucv: UCVClient, names: List[str]) -> None:
    """Enable collision on all ghost agents in one batch.

    Called after all agents are spawned so UE only rebuilds navmesh once.
    """
    log.info("finalizing %d ghost agents — enabling collision", len(names))
    for name in names:
        try:
            resp = ucv.send(f"vset /object/{name}/collision true")
            log.info("ghost %s collision on: %r", name, resp)
        except Exception as exc:
            log.error("ghost %s collision on FAILED: %s", name, exc)


# ---------------------------------------------------------------------------
# Episode loading
# ---------------------------------------------------------------------------

def _load_episodes_file(path: str) -> List[NavigationEpisode]:
    """Load episodes from a pre-generated JSON file.

    Accepts three shapes produced by ``python -m nav_task``:
      * split file: ``{"episodes": [...], ...}`` (from ``--split``)
      * raw list: ``[{...}, {...}]`` (from ``--output`` with n > 1)
      * single episode: ``{...}`` (from ``--output`` with n == 1)
    """
    text = Path(path).read_text()
    data = json.loads(text)
    if isinstance(data, dict) and "episodes" in data:
        raw_list = data["episodes"]
    elif isinstance(data, list):
        raw_list = data
    elif isinstance(data, dict) and "episode_id" in data:
        raw_list = [data]
    else:
        raise ValueError(
            f"{path}: unrecognised shape (expected split dict, list, or "
            "single-episode dict)"
        )
    return [NavigationEpisode.from_dict(d) for d in raw_list]


# ---------------------------------------------------------------------------
# WandB helpers
# ---------------------------------------------------------------------------

def _init_wandb(args, episodes):
    """Initialize WandB run for batch experiment."""
    import wandb
    config = {
        "mode": args.mode,
        "model": args.model,
        "model_id": args.model_id,
        "memory": args.memory,
        "n_tasks": args.n_tasks,
        "wave_size": args.wave_size,
        "max_steps": args.max_steps,
        "seed": args.seed,
        "ucv_port": args.ucv_port,
        "mcp_port": args.mcp_port,
        "vision_depth": args.vision_depth,
        "nav_min_cm": args.nav_min_cm,
        "nav_max_cm": args.nav_max_cm,
    }
    run_name = args.run_name or f"batch_{args.model}_{args.memory}_n{args.n_tasks}"
    run = wandb.init(
        project=args.wandb_project,
        name=run_name,
        config=config,
        tags=[args.model, args.memory, f"n{args.n_tasks}", f"wave{args.wave_size}"],
    )
    return run


# ---------------------------------------------------------------------------
# Core: run one wave of ghost agents
# ---------------------------------------------------------------------------

def run_wave(
    ucv: UCVClient,
    mcp: Optional[MCPClient],
    llm: LLMClient,
    episodes: List[NavigationEpisode],
    *,
    max_steps: int = 40,
    vision_depth: int = 3,
    spawn_z: float = _DEFAULT_SPAWN_Z,
    memory: Optional[AgentMemory] = None,
    wandb_run=None,
    global_step: int = 0,
    batch_dir: Optional[Path] = None,
    save_frames: bool = False,
    capture_rgb: bool = True,
    reuse_agents: bool = False,
    skip_destroy: bool = False,
) -> Tuple[List[Dict[str, Any]], int]:
    """Run one batch of ghost agents concurrently in one UE instance.

    **One UE instance runs exactly one wave.**  Each agent gets its
    own ``SimWorldNavEnv`` with a unique name and camera ID, plus its
    own :class:`EpisodeLogger` writing per-step JSONL + frames +
    summary under ``batch_dir/ep_XXX_<name>/``.  LLM requests are
    issued sequentially (one per active agent per step).

    If *reuse_agents* is True, ghost agents are assumed to already
    exist in the world — only teleport to new start positions.
    If *skip_destroy* is True, agents are NOT destroyed at the end
    (caller plans to reuse them).

    Returns (list of metrics dicts, updated global_step).
    """
    n = len(episodes)
    log.info("wave: %d ghost agents, max_steps=%d, reuse=%s", n, max_steps, reuse_agents)
    mem = memory or NullMemory()

    # --- Phase 1: spawn (or reuse) ghost agents ---
    agent_names = [f"GhostAgent_{i}" for i in range(n)]
    if not reuse_agents:
        for i, ep in enumerate(episodes):
            name = agent_names[i]
            x, y, z = ep.start_position.x, ep.start_position.y, spawn_z
            log.info("wave: spawning %s at (%.0f,%.0f,%.0f) for %s",
                     name, x, y, z, ep.episode_id)
            ucv.spawn_bp_asset(_HUMANOID_BP, name, location=(x, y, z),
                               auto_repair_collision=False)
            _configure_ghost_ucv(ucv, name, x, y, z)

        # Enable collision on all ghosts in one batch (single navmesh rebuild)
        _finalize_ghost_agents(ucv, agent_names)
    else:
        # Just teleport existing agents to new start positions
        for i, ep in enumerate(episodes):
            name = agent_names[i]
            x, y, z = ep.start_position.x, ep.start_position.y, spawn_z
            try:
                ucv.send(f"vset /object/{name}/location {x} {y} {z}")
                ucv.send(f"vset /object/{name}/rotation 0 0 0")
                log.info("wave: teleported %s to (%.0f,%.0f,%.0f) for %s",
                         name, x, y, z, ep.episode_id)
            except Exception as exc:
                log.error("wave: teleport %s failed: %s", name, exc)

    slots: List[AgentSlot] = []
    for i, ep in enumerate(episodes):
        agent_name = agent_names[i]

        env = SimWorldNavEnv(
            ucv_client=ucv,
            mcp_client=mcp,
            agent_name=agent_name,
            camera_id=i,            # spawn-order camera index (fresh UE → starts at 0)
            capture_rgb=capture_rgb,
            spawn_on_reset=False,   # already spawned as ghost
            ensure_pie=False,       # PIE already running
            spawn_z=spawn_z,
        )
        # Mark as already spawned so reset() doesn't re-spawn
        env._spawned = True

        ep_logger: Optional[EpisodeLogger] = None
        if batch_dir is not None:
            ep_logger = EpisodeLogger(
                run_name=f"ep_{i:03d}_{agent_name}",
                root=str(batch_dir),
                save_frames=save_frames,
                annotate_frames=False,
                timestamp_dir=False,
                install_log_handler=False,  # batch-level handler already installed
                meta={
                    "episode_id": ep.episode_id,
                    "episode_idx": i,
                    "agent_name": agent_name,
                    "camera_id": i,
                    "task_type": getattr(ep, "task_type", "pointnav"),
                },
            )

        slot = AgentSlot(
            idx=i,
            agent_name=agent_name,
            episode=ep,
            env=env,
            logger=ep_logger,
        )
        slot.history = [LLMMessage.text("system", _NAV_SYSTEM_PROMPT)]
        slots.append(slot)

    # --- Reset all envs ---
    failed_slots = []
    for slot in slots:
        try:
            obs, info = slot.env.reset(slot.episode)
            slot.task_prompt = info.get("task_prompt", "")
            slot._obs = obs
            slot._info = info
            if slot.logger is not None:
                slot.logger.log_step(0, None, obs, 0.0, False, False, info)
        except Exception as exc:
            log.error("env.reset failed for %s (%s): %s",
                      slot.agent_name, slot.episode.episode_id, exc)
            slot.done = True
            slot.ended_reason = "reset_error"
            failed_slots.append(slot.idx)
    if failed_slots:
        log.warning("wave: %d agents failed to reset: %s", len(failed_slots), failed_slots)

    time.sleep(2)  # let cameras initialize

    # --- Step loop ---
    step_bar = tqdm(
        total=max_steps,
        desc=f"batch ({n} ghosts)",
        unit="step",
        leave=True,
    )
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

            # Memory: prepend recalled experience
            recalled = mem.query(user_text, k=5)
            if recalled:
                recalled_text = "\n".join(f"- {m}" for m in recalled)
                user_text = f"Relevant past experience:\n{recalled_text}\n\n{user_text}"

            rgb = obs.get("rgb")
            if rgb is not None and getattr(llm, "name", "") != "claude-sdk":
                slot.history.append(LLMMessage.user_with_image(user_text, rgb))
            else:
                slot.history.append(LLMMessage.text("user", user_text))
            _strip_images(slot.history, vision_depth)

            # LLM call (sequential per agent)
            try:
                resp = llm.chat(slot.history, nav_tool_schemas(), max_tokens=1024)
            except Exception as exc:
                log.error("LLM error for %s: %s", slot.agent_name, exc)
                slot.ended_reason = "llm_error"
                slot.done = True
                continue

            if slot.logger is not None:
                slot.logger.log_llm(t, llm.name, resp)

            if not resp.tool_calls:
                log.info("%s: LLM returned no tool calls at t=%d",
                         slot.agent_name, t)
                slot.ended_reason = "no_tool_call"
                slot.done = True
                continue

            slot.history.append(LLMMessage(
                role="assistant",
                content=[{"type": "text", "text": resp.text or ""}],
                tool_calls=resp.tool_calls,
            ))

            for tc in resp.tool_calls:
                prev_d = info.get("distance_to_goal_cm")
                try:
                    obs, reward, done, truncated, info = slot.env.step(tc.to_action_dict())
                except Exception as exc:
                    log.error("%s step %d action %s failed: %s",
                              slot.agent_name, t, tc.name, exc)
                    slot.done = True
                    slot.ended_reason = "step_error"
                    break
                global_step += 1
                slot.cumulative_reward += float(reward)

                if slot.logger is not None:
                    slot.logger.log_step(
                        t, tc.to_action_dict(), obs, reward, done, truncated, info,
                    )

                slot.history.append(LLMMessage(
                    role="tool",
                    tool_call_id=tc.id,
                    content=[{"type": "text", "text": (
                        f"reward={reward:+.3f} "
                        f"d_goal={info['distance_to_goal_cm']:.0f}cm"
                    )}],
                ))

                # Memory: record step
                new_d = info.get("distance_to_goal_cm")
                delta = (prev_d - new_d) if (prev_d and new_d) else 0.0
                step_record = (
                    f"t={t} {tc.name} d_goal:{prev_d:.0f}->{new_d:.0f}cm "
                    f"(delta={delta:+.0f}) reward={reward:+.3f}"
                )
                mem.insert(step_record)

                # WandB per-step logging
                if wandb_run:
                    import wandb
                    wandb.log({
                        "batch/step_reward": float(reward),
                        "batch/cumulative_reward": float(slot.cumulative_reward),
                        "batch/distance_to_goal": float(info["distance_to_goal_cm"]),
                        "batch/action": tc.name,
                        "batch/agent": slot.agent_name,
                        "batch/episode_idx": slot.idx,
                        "batch/step": t,
                    }, step=global_step)

                if done or truncated:
                    slot.metrics = info.get("metrics", {}) or {}
                    slot.ended_reason = "success" if done else "truncated"
                    slot.done = True
                    break

            slot._obs = obs
            slot._info = info

        n_done = sum(1 for s in slots if s.done)
        step_bar.set_postfix(done=f"{n_done}/{n}")
        step_bar.update(1)
        log.info("batch t=%d: %d/%d done", t, n_done, n)
    step_bar.close()

    # --- Finalize timed-out slots: compute metrics from env state ---
    for slot in slots:
        if slot.metrics:
            continue
        try:
            final_metrics = slot.env._final_metrics(slot.env._last_xy)
        except Exception as exc:
            log.warning("%s: _final_metrics failed (%s)", slot.agent_name, exc)
            final_metrics = {}
        final_metrics.setdefault("cumulative_reward", slot.cumulative_reward)
        slot.metrics = final_metrics

    # --- Collect results, write per-episode summary, cleanup ---
    results = []
    for slot in slots:
        sr = float(slot.metrics.get("SR", 0) or 0)
        spl = float(slot.metrics.get("SPL", 0) or 0)
        softspl = float(slot.metrics.get("SoftSPL", 0) or 0)
        path_cm = float(slot.metrics.get("path_length_cm", 0) or 0)
        cum_r = float(slot.metrics.get("cumulative_reward", slot.cumulative_reward) or 0)

        log.info(
            "%s episode=%s SR=%.0f SPL=%.3f steps=%d reason=%s",
            slot.agent_name, slot.episode.episode_id, sr, spl,
            slot.step, slot.ended_reason,
        )

        summary = {
            "episode_id": slot.episode.episode_id,
            "episode_idx": slot.idx,
            "agent_name": slot.agent_name,
            "SR": sr,
            "SPL": spl,
            "SoftSPL": softspl,
            "steps": slot.step,
            "path_length_cm": path_cm,
            "cumulative_reward": cum_r,
            "ended_reason": slot.ended_reason,
            "metrics": slot.metrics,
        }

        if slot.logger is not None:
            try:
                slot.logger.log_summary(summary)
            except Exception as exc:
                log.warning("log_summary failed for %s: %s",
                            slot.agent_name, exc)
            slot.logger.close()

        # WandB per-episode rollup (one point per episode, keyed to final step)
        if wandb_run:
            import wandb
            wandb.log({
                "episode/SR": sr,
                "episode/SPL": spl,
                "episode/SoftSPL": softspl,
                "episode/steps": slot.step,
                "episode/path_length_cm": path_cm,
                "episode/cumulative_reward": cum_r,
                "episode/idx": slot.idx,
                "episode/ended_reason_code": {
                    "success": 0, "truncated": 1, "max_steps": 2,
                    "llm_error": 3, "no_tool_call": 4,
                }.get(slot.ended_reason, -1),
            }, step=global_step)

        results.append(summary)
        # Destroy ghost agent (unless caller wants to reuse)
        if not skip_destroy:
            try:
                ucv.send(f"vset /object/{slot.agent_name}/destroy")
            except Exception:
                pass

    return results, global_step


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
    memory: Optional[AgentMemory] = None,
) -> Dict[str, Any]:
    """Run a single episode with normal (non-ghost) agent + trajectory saving."""
    from .runner import run_episode

    env = SimWorldNavEnv(
        ucv_client=ucv,
        mcp_client=mcp,
        agent_name="GymNavAgent_0",
        capture_rgb=True,
        spawn_z=spawn_z,
        ensure_pie=True,
    )
    logger = EpisodeLogger(
        run_name=run_name or f"single_{episode.episode_id}",
        save_frames=True,
        annotate_frames=True,
    )
    metrics = run_episode(
        env, llm, episode, logger,
        memory=memory or NullMemory(),
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
                   help="batch = ghost agents; single = normal agent, save traj")
    p.add_argument("--n-tasks", type=int, default=4,
                   help=(
                       "Number of concurrent ghost agents (and episodes) "
                       "to run in this UE instance.  One UE instance runs "
                       "exactly one wave — spawn more UE instances for "
                       "more concurrency."
                   ))
    # Kept for back-compat; if supplied, must equal --n-tasks.
    p.add_argument("--wave-size", type=int, default=None,
                   help="Deprecated alias for --n-tasks (must equal --n-tasks if set).")
    # LLM
    p.add_argument("--model", default="claude")
    p.add_argument("--model-id", default=None)
    p.add_argument("--base-url", default=None)
    p.add_argument("--api-key", default=None)
    # UE connection — defaults honour env vars so one `export` can
    # flip an entire shell session to a different UE instance.
    import os as _os
    p.add_argument("--ucv-host", default=_os.environ.get("UNREALCV_HOST", "127.0.0.1"))
    p.add_argument("--ucv-port", type=int,
                   default=int(_os.environ.get("UNREALCV_PORT", "9001")))
    p.add_argument("--mcp-host", default=_os.environ.get("UNREAL_MCP_HOST", "127.0.0.1"))
    p.add_argument("--mcp-port", type=int,
                   default=int(_os.environ.get("UNREAL_MCP_PORT", "55557")))
    # Episode generation
    p.add_argument("--scene-graph", default=None)
    p.add_argument("--episodes-file", default=None,
                   help=(
                       "Load pre-generated episodes from a JSON file instead "
                       "of sampling them at runtime. Accepts either a split "
                       "file (dict with 'episodes' list, as produced by "
                       "`python -m nav_task --split ...`) or a raw list / "
                       "single-episode dict from `--output`. When set, the "
                       "runner skips navmesh build and episode sampling — "
                       "all path/geodesic info is read from the file. "
                       "--n-tasks selects how many episodes from the file "
                       "to run (must be <= file size)."
                   ))
    p.add_argument("--nav-min-cm", type=float, default=1000.0)
    p.add_argument("--nav-max-cm", type=float, default=4000.0)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--max-steps", type=int, default=40)
    p.add_argument("--vision-depth", type=int, default=3)
    # Trajectory recording — batch defaults to no frames (fast eval),
    # single-mode always keeps frames on.
    p.add_argument("--save-frames", action="store_true", default=False,
                   help="Save RGB frames per step (default off in batch mode; "
                        "single mode always saves).")
    p.add_argument("--no-rgb", action="store_true", default=False,
                   help="Disable RGB capture entirely (faster, smaller logs).")
    # Memory
    p.add_argument("--memory", default="none",
                   choices=["none", "text", "mem0", "strategy", "hierarchical"],
                   help="Memory backend: none, text, mem0, strategy, or hierarchical")
    p.add_argument("--eval-mode", default="train",
                   choices=["train", "test"],
                   dest="eval_mode",
                   help=(
                       "train: memory is read-write (insert + query). "
                       "test: memory is read-only (query only, no insert). "
                       "Use 'test' for deterministic evaluation with frozen "
                       "memories from a prior training run."
                   ))
    # WandB
    p.add_argument("--wandb-project", default="simworld-nav")
    p.add_argument("--wandb-key", default=None,
                   help="WandB API key (or set WANDB_API_KEY env var)")
    p.add_argument("--no-wandb", action="store_true",
                   help="Disable WandB logging")
    # Misc
    p.add_argument("--run-name", default=None)
    p.add_argument("--log-level", default="INFO")
    return p


def main(argv: Optional[List[str]] = None) -> None:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
    )

    # --wave-size is a deprecated alias for --n-tasks.  One UE instance
    # runs exactly one wave; to run more ghost agents you launch another
    # UE instance.
    if args.wave_size is not None and args.wave_size != args.n_tasks:
        log.warning(
            "--wave-size=%d ignored — one UE instance runs one wave. "
            "Setting --n-tasks=%d is what controls concurrency.",
            args.wave_size, args.n_tasks,
        )

    from .episode_builder import (
        sample_pointnav_episode,
        sample_pointnav_episode_navmesh,
    )

    mcp = MCPClient(host=args.mcp_host, port=args.mcp_port, name="batch-mcp")

    # Start PIE.  Need a longer wait when --scene-graph is set because
    # the navmesh plugin's game-world binding takes ~10s after PIE
    # transition to be queryable; 5s is enough for spawn_bp_asset but
    # not for `vset /nav/build`.
    pie_wait = 12.0 if args.scene_graph else 5.0
    try:
        mcp.start_pie(wait_seconds=pie_wait)
    except Exception as exc:
        log.warning("PIE auto-start failed (%s); proceeding anyway", exc)

    ucv = UCVClient(host=args.ucv_host, port=args.ucv_port, name="batch-0")
    for attempt in range(15):
        try:
            ucv.connect()
            break
        except Exception:
            log.info("waiting for UnrealCV... (attempt %d)", attempt + 1)
            time.sleep(2)
    else:
        raise RuntimeError("UnrealCV not available after PIE start")

    llm = make_llm(
        args.model,
        model=args.model_id,
        base_url=args.base_url,
        api_key=args.api_key,
    )

    # Build memory
    memory = build_memory(
        args.memory,
        agent_id="batch_agent",
        llm_model=args.model_id,
        llm_base_url=args.base_url,
        llm_api_key=args.api_key,
    )
    if args.eval_mode == "test":
        memory = ReadOnlyMemory(memory)
    log.info("memory backend: %s (eval_mode=%s)", getattr(memory, 'name', type(memory).__name__), args.eval_mode)

    # WandB
    import os
    wandb_run = None
    if not args.no_wandb:
        if args.wandb_key:
            os.environ["WANDB_API_KEY"] = args.wandb_key
        if os.environ.get("WANDB_API_KEY"):
            try:
                wandb_run = _init_wandb(args, [])
                log.info("WandB run initialized: %s", wandb_run.name)
            except Exception as exc:
                log.warning("WandB init failed (%s); continuing without", exc)
        else:
            log.info("WandB disabled (no API key)")

    # Episode source: either load from a pre-generated file (deterministic,
    # skips live navmesh build) or sample at runtime via UE navmesh.
    episodes: List[NavigationEpisode] = []
    if args.episodes_file:
        log.info("Loading episodes from %s (skipping navmesh build + sampling)",
                 args.episodes_file)
        episodes = _load_episodes_file(args.episodes_file)
        if args.n_tasks > len(episodes):
            raise RuntimeError(
                f"--n-tasks={args.n_tasks} but {args.episodes_file} only "
                f"contains {len(episodes)} episode(s)"
            )
        episodes = episodes[:args.n_tasks]
        for i, ep in enumerate(episodes):
            log.info("  episode %d: %s", i, ep.episode_id)
    else:
        # Build navmesh once, then generate all episodes.  The plugin's
        # game-world binding is racy right after PIE starts — retry on
        # "No game world available" or any error containing PIE-start hints.
        nav_interface = None
        if args.scene_graph:
            from nav_task.navmesh_interface import NavmeshNavigationInterface
            nav_interface = NavmeshNavigationInterface(ucv)
            for attempt in range(6):
                resp = nav_interface.build_navmesh()
                log.info("navmesh build attempt %d: %s", attempt + 1, resp)
                if "error" not in resp.lower():
                    break
                log.warning("navmesh build returned error; sleeping 3s and retrying")
                time.sleep(3.0)
            else:
                raise RuntimeError(
                    f"navmesh build failed after 6 attempts; last response: {resp}"
                )

        log.info("Generating %d pointnav episodes (seed=%d)", args.n_tasks, args.seed)
        for i in range(args.n_tasks):
            seed = args.seed + i
            if args.scene_graph:
                result = sample_pointnav_episode_navmesh(
                    ucv,
                    seed=seed, idx=i,
                    min_geodesic_cm=args.nav_min_cm,
                    max_geodesic_cm=args.nav_max_cm,
                    build_navmesh=False,
                    nav_interface=nav_interface,
                )
                episodes.append(result["episode"])
                log.info("  episode %d: %s", i, result["episode"].episode_id)
            else:
                ep = sample_pointnav_episode(ucv, seed=seed, idx=i)
                episodes.append(ep)
                log.info("  episode %d: %s", i, ep.episode_id)

    # --- Run ---
    if args.mode == "single":
        log.info("=== SINGLE MODE: 1 episode, normal agent, trajectory saved ===")
        metrics = run_single(
            ucv, mcp, llm, episodes[0],
            max_steps=args.max_steps,
            vision_depth=args.vision_depth,
            run_name=args.run_name,
            memory=memory,
        )
        print(f"\n[SINGLE] {metrics}")
    else:
        # Create a single batch-level directory housing one subdir per
        # episode.  Install ONE FileHandler here so the whole batch run
        # is captured in ``batch_run.log`` — per-episode EpisodeLoggers
        # are created with ``install_log_handler=False`` to avoid
        # duplicating every log record N times.
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        batch_run_id = f"batch_{ts}_{args.run_name or 'results'}"
        batch_dir = Path("runs") / batch_run_id
        batch_dir.mkdir(parents=True, exist_ok=True)

        batch_log_path = batch_dir / "batch_run.log"
        batch_fh = logging.FileHandler(batch_log_path, encoding="utf-8")
        batch_fh.setLevel(logging.DEBUG)
        batch_fh.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-5s %(name)s | %(message)s"
        ))
        logging.getLogger().addHandler(batch_fh)

        def _git_sha() -> Optional[str]:
            import subprocess
            try:
                return subprocess.check_output(
                    ["git", "rev-parse", "HEAD"],
                    stderr=subprocess.DEVNULL, text=True,
                ).strip()
            except Exception:
                return None

        batch_meta = {
            "batch_run_id": batch_run_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "git_sha": _git_sha(),
            "mode": args.mode,
            "model": args.model,
            "model_id": args.model_id,
            "memory": args.memory,
            "eval_mode": args.eval_mode,
            "n_tasks": args.n_tasks,
            "max_steps": args.max_steps,
            "vision_depth": args.vision_depth,
            "seed": args.seed,
            "ucv_port": args.ucv_port,
            "mcp_port": args.mcp_port,
            "nav_min_cm": args.nav_min_cm,
            "nav_max_cm": args.nav_max_cm,
            "scene_graph": args.scene_graph,
            "episodes_file": args.episodes_file,
            "save_frames": args.save_frames,
            "capture_rgb": not args.no_rgb,
            "run_name": args.run_name,
            "wandb_project": args.wandb_project if not args.no_wandb else None,
            "episode_ids": [ep.episode_id for ep in episodes],
        }
        (batch_dir / "batch_meta.json").write_text(
            json.dumps(batch_meta, indent=2), encoding="utf-8"
        )
        log.info("=== BATCH MODE: %d concurrent ghost agents in one UE instance ===",
                 args.n_tasks)
        log.info("batch output dir: %s", batch_dir)

        # One UE instance runs exactly one wave.  No loop — if you want
        # more concurrency, spawn more UE instances (see run_batch.sh).
        global_step = 0
        try:
            all_results, global_step = run_wave(
                ucv, mcp, llm, episodes,
                max_steps=args.max_steps,
                vision_depth=args.vision_depth,
                memory=memory,
                wandb_run=wandb_run,
                global_step=global_step,
                batch_dir=batch_dir,
                save_frames=args.save_frames,
                capture_rgb=not args.no_rgb,
            )
        except Exception:
            log.exception("batch crashed")
            raise

        # Batch summary
        n = len(all_results)
        n_success = sum(1 for r in all_results if r.get("SR", 0) > 0)
        avg_sr = n_success / n if n else 0.0
        avg_spl = sum(r.get("SPL", 0) for r in all_results) / n if n else 0.0
        avg_softspl = sum(r.get("SoftSPL", 0) for r in all_results) / n if n else 0.0
        avg_cum_r = sum(r.get("cumulative_reward", 0) for r in all_results) / n if n else 0.0
        avg_path = sum(r.get("path_length_cm", 0) for r in all_results) / n if n else 0.0

        batch_summary = {
            "batch_run_id": batch_run_id,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "n_episodes": n,
            "n_success": n_success,
            "SR": avg_sr,
            "SPL": avg_spl,
            "SoftSPL": avg_softspl,
            "cumulative_reward_mean": avg_cum_r,
            "path_length_cm_mean": avg_path,
            "episodes": all_results,
        }
        (batch_dir / "batch_summary.json").write_text(
            json.dumps(batch_summary, indent=2), encoding="utf-8"
        )

        # Keep the legacy flat JSON for back-compat with analyze_runs.
        legacy_path = Path("runs") / f"batch_{args.run_name or 'results'}.json"
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        with open(legacy_path, "w") as f:
            json.dump(all_results, f, indent=2)

        print(f"\n{'='*50}")
        print(f"BATCH RESULTS: {n} episodes")
        print(f"  SR:      {n_success}/{n} ({100*avg_sr:.0f}%)")
        print(f"  SPL:     {avg_spl:.3f}")
        print(f"  SoftSPL: {avg_softspl:.3f}")
        print(f"  cum_r:   {avg_cum_r:+.1f} avg")
        print(f"{'='*50}")
        for r in all_results:
            print(
                f"  {r['episode_id']}: SR={r['SR']:.0f} "
                f"SPL={r['SPL']:.3f} steps={r['steps']} "
                f"cum_r={r['cumulative_reward']:+.1f} "
                f"end={r['ended_reason']}"
            )
        log.info("batch summary written to %s", batch_dir / "batch_summary.json")

        # WandB final summary
        if wandb_run:
            import wandb
            wandb.log({
                "batch/SR": avg_sr,
                "batch/SPL": avg_spl,
                "batch/SoftSPL": avg_softspl,
                "batch/cumulative_reward_mean": avg_cum_r,
                "batch/path_length_cm_mean": avg_path,
                "batch/n_episodes": n,
                "batch/n_success": n_success,
            })

        # Remove the batch-level file handler before we exit.
        try:
            logging.getLogger().removeHandler(batch_fh)
            batch_fh.close()
        except Exception:
            pass

    ucv.disconnect()
    if wandb_run:
        import wandb
        wandb.finish()


if __name__ == "__main__":
    main()
