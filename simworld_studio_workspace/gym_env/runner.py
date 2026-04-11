"""Episode loop: feed observations to the LLM, send chosen actions to env.

Vision history truncation
-------------------------
Per-step RGB images blow up token budgets fast.  By default we keep
only the **last K** observation images in the chat history (K=3); older
turns retain only their text summary.  Disable by passing
``vision_history_depth=None`` to keep all frames.

Loop control
------------
The loop terminates when the env reports ``done`` / ``truncated``, OR
when the LLM returns a turn with no tool calls (the model gives up /
emits a final text answer).
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
from typing import Any, Dict, List, Optional

from nav_task.episode import NavigationEpisode
from nav_task.task_spec import make_task_prompt

from .action_space import nav_tool_schemas
from .llm import LLMClient, LLMMessage, ToolCall, make_llm
from .logger import EpisodeLogger
from .memory import AgentMemory, NullMemory, build_memory
from .simworld_nav_env import SimWorldNavEnv

log = logging.getLogger(__name__)


_NAV_SYSTEM_PROMPT = """You are an embodied navigation agent in a 3D city scene.

You receive a first-person RGB image and a goal description.  Choose
ONE navigation action per turn from this set:

  - MOVE_FORWARD : walk forward roughly 2 seconds (~200-400 cm)
  - TURN_LEFT    : rotate 30 degrees left
  - TURN_RIGHT   : rotate 30 degrees right
  - STOP         : declare you have reached the goal

Think briefly, then call exactly one tool.  Stop calling tools when
you believe you are within 2 meters of the goal."""


def _build_user_text(info: Dict[str, Any], obs: Dict[str, Any]) -> str:
    parts: List[str] = []
    parts.append(f"Task: {info.get('task_prompt', '')}")
    if "pointgoal_with_gps_compass" in obs:
        d, ang = obs["pointgoal_with_gps_compass"].tolist()
        parts.append(
            f"Goal: distance={d:.0f} cm, bearing={math.degrees(ang):+.0f} deg"
        )
    elif "objectgoal" in obs:
        cat = info.get("task_prompt", "")
        parts.append(f"Goal: find a {cat}")
    parts.append(
        f"Position: ({obs['agent_xy'][0]:.0f}, {obs['agent_xy'][1]:.0f})"
        f"  yaw={obs['agent_yaw_deg']:+.0f} deg"
    )
    parts.append(f"Step: {info['step']}")
    return "\n".join(parts)


def _strip_images(messages: List[LLMMessage], keep_last_k: int) -> None:
    """Mutate ``messages`` in-place: keep images only on the last K user turns."""
    if keep_last_k is None:
        return
    user_turns = [
        i for i, m in enumerate(messages)
        if m.role == "user" and any(b["type"] == "image" for b in m.content)
    ]
    if len(user_turns) <= keep_last_k:
        return
    drop = user_turns[:-keep_last_k]
    for i in drop:
        messages[i].content = [
            b if b["type"] == "text" else {"type": "text", "text": "[image omitted]"}
            for b in messages[i].content
        ]


def run_episode(
    env: SimWorldNavEnv,
    llm: LLMClient,
    episode: NavigationEpisode,
    logger: EpisodeLogger,
    *,
    memory: Optional[AgentMemory] = None,
    max_steps: Optional[int] = None,
    vision_history_depth: Optional[int] = 3,
    max_tokens: int = 1024,
) -> Dict[str, Any]:
    """Run one episode end-to-end.  Returns the final metrics dict."""
    obs, info = env.reset(episode)
    logger.log_step(0, None, obs, 0.0, False, False, info)

    if max_steps is None:
        max_steps = episode.success_criteria.max_steps

    memory = memory or NullMemory()
    memory.reset()

    history: List[LLMMessage] = [
        LLMMessage.text("system", _NAV_SYSTEM_PROMPT),
    ]

    final_metrics: Dict[str, Any] = {}
    for t in range(1, max_steps + 1):
        user_text = _build_user_text(info, obs)

        # Memory recall: ask the backend for relevant past items and
        # prepend them to the user turn as plain text.  NullMemory
        # returns []; cost is zero when disabled.
        recalled = memory.query(user_text, k=5)
        if recalled:
            memo_block = "Relevant past experience:\n" + "\n".join(
                f"- {m}" for m in recalled
            )
            user_text = memo_block + "\n\n" + user_text

        # Attach image only if RGB capture is on AND the LLM actually
        # consumes images.  ClaudeAgentSDKClient is text-only.
        rgb = obs.get("rgb")
        if rgb is not None and getattr(llm, "name", "") != "claude-sdk":
            history.append(LLMMessage.user_with_image(user_text, rgb))
        else:
            history.append(LLMMessage.text("user", user_text))
        _strip_images(history, vision_history_depth)

        log.info("[runner t=%d] querying %s", t, llm.name)
        resp = llm.chat(history, nav_tool_schemas(), max_tokens=max_tokens)
        logger.log_llm(t, llm.name, resp)

        # Echo to stdout for live debugging
        thought = (resp.text or "").strip().splitlines()
        if thought:
            print(f"[t={t}] {llm.name} thought: {thought[0][:120]}")
        for tc in resp.tool_calls:
            print(f"[t={t}] action -> {tc.name}")

        if not resp.tool_calls:
            log.info("LLM returned no tool calls; ending episode")
            break

        history.append(LLMMessage(
            role="assistant",
            content=[{"type": "text", "text": resp.text or ""}],
            tool_calls=resp.tool_calls,
        ))

        done_now = False
        for tc in resp.tool_calls:
            prev_d = info.get("distance_to_goal_cm")
            obs, reward, done, truncated, info = env.step(tc.to_action_dict())
            logger.log_step(t, tc.to_action_dict(), obs, reward, done, truncated, info)
            history.append(LLMMessage(
                role="tool",
                tool_call_id=tc.id,
                content=[{
                    "type": "text",
                    "text": (
                        f"reward={reward:+.3f} "
                        f"d_goal={info['distance_to_goal_cm']:.0f}cm "
                        f"pos=({info['agent_xy'][0]:.0f},{info['agent_xy'][1]:.0f})"
                    ),
                }],
            ))

            # Memory insert: record what we tried and what happened.
            # Kept as a short natural-language blurb so mem0's extractor
            # has something to chew on.
            new_d = info.get("distance_to_goal_cm")
            delta = (prev_d - new_d) if (prev_d is not None and new_d is not None) else 0.0
            memory.insert(
                (
                    f"step={t} action={tc.name} reward={reward:+.3f} "
                    f"d_goal {prev_d:.0f}->{new_d:.0f}cm (delta={delta:+.0f}) "
                    f"yaw={obs['agent_yaw_deg']:+.0f}"
                ),
                metadata={
                    "step": t,
                    "action": tc.name,
                    "reward": float(reward),
                    "d_goal_cm": float(new_d) if new_d is not None else None,
                    "delta_cm": float(delta),
                    "done": bool(done),
                },
            )

            if done or truncated:
                final_metrics = info.get("metrics", {}) or {}
                done_now = True
                break
        if done_now:
            break

    # End-of-episode memory: insert a concise lesson learned.
    sr = final_metrics.get("SR", 0)
    path_cm = final_metrics.get("path_length_cm", 0)
    cum_r = final_metrics.get("cumulative_reward", 0)
    if sr > 0:
        memory.insert(
            f"EPISODE SUCCESS: reached goal in {env.step_count} steps, "
            f"path={path_cm:.0f}cm, reward={cum_r:+.0f}. "
            f"Strategy: align bearing to ~0 then MOVE_FORWARD repeatedly."
        )
    else:
        # Describe what went wrong
        if env.step_count >= (max_steps or 999):
            memory.insert(
                f"EPISODE FAIL: ran out of steps ({env.step_count}). "
                f"d_goal={info.get('distance_to_goal_cm', '?')}cm still far. "
                f"Lesson: don't turn more than 2-3 times in a row; "
                f"switch to MOVE_FORWARD once bearing is within ±45°. "
                f"Use STOP when distance < 200cm."
            )
        else:
            memory.insert(
                f"EPISODE FAIL: d_goal={info.get('distance_to_goal_cm', '?')}cm. "
                f"reward={cum_r:+.0f}."
            )

    logger.log_summary(final_metrics or {"note": "loop exited without done"})
    return final_metrics


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m gym_env.runner",
        description="Run a single nav episode with a chosen LLM.",
    )
    p.add_argument("--model", default="claude",
                   help="LLM short name: claude / claude-sdk / gpt / gemini / qwen")
    p.add_argument("--model-id", default=None,
                   help="Override the model id (e.g. 'Qwen/Qwen3-VL-30B-A3B-Instruct')")
    p.add_argument("--base-url", default=None,
                   help="Override the LLM base URL (e.g. 'http://host:8000/v1')")
    p.add_argument("--api-key", default=None,
                   help="Override the API key for the LLM endpoint")
    p.add_argument("--n-episodes", type=int, default=1,
                   help="Number of episodes to run back-to-back (multi-episode experiment)")
    p.add_argument("--ucv-host", default="127.0.0.1")
    p.add_argument("--ucv-port", type=int, default=9000)
    p.add_argument("--mcp-host", default="127.0.0.1")
    p.add_argument("--mcp-port", type=int, default=55557,
                   help="UE editor MCP TCP port (used to start PIE)")
    p.add_argument("--no-start-pie", action="store_true",
                   help="Skip the auto PIE-start on first reset (assume PIE is already running)")
    p.add_argument("--agent-name", default="GymNavAgent_0")
    p.add_argument("--task", choices=["pointnav", "objectnav"], default="pointnav")
    p.add_argument("--target-distance", type=float, default=2000.0,
                   help="Target distance in cm (PointNav)")
    p.add_argument("--target-filter", default=None,
                   help="Substring an actor name must contain (ObjectNav)")
    p.add_argument("--object-category", default="OBJECT")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--max-steps", type=int, default=40)
    p.add_argument("--vision-depth", type=int, default=3,
                   help="How many recent frames to keep in LLM history")
    p.add_argument("--run-name", default=None)
    p.add_argument("--no-rgb", action="store_true",
                   help="Disable RGB capture (text-only ablation). "
                        "Ignored when --record-trajectory is set.")
    p.add_argument("--record-trajectory", action="store_true",
                   help="Save annotated PNG frames of every step under "
                        "runs/<id>/frames/. Forces RGB capture on, even "
                        "for text-only LLMs (claude-sdk).")
    p.add_argument("--memory", default="none",
                   choices=["none", "text", "mem0"],
                   help="Agent memory backend. 'none' disables memory. "
                        "'text' uses a simple JSON file (no extra deps). "
                        "'mem0' uses mem0ai (pip install mem0ai).")
    p.add_argument("--log-level", default="INFO")
    return p


def main(argv: Optional[List[str]] = None) -> None:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
    )

    from .ucv_client import UCVClient
    from .mcp_client import MCPClient
    from .episode_builder import sample_pointnav_episode, sample_objectnav_episode

    ucv = UCVClient(host=args.ucv_host, port=args.ucv_port, name="env-0")
    mcp = MCPClient(host=args.mcp_host, port=args.mcp_port, name="env-0-mcp")

    # If we're going to auto-start PIE, do it BEFORE the first UCV
    # connect — UnrealCV inside PIE only comes up once the game world
    # exists, and a connect attempted against the editor world will
    # succeed against a different (cached) socket and then fail when
    # PIE swaps it underneath us.
    if not args.no_start_pie:
        try:
            mcp.start_pie(wait_seconds=5.0)
        except Exception as exc:
            print(f"WARN: PIE auto-start failed ({exc}); proceeding anyway",
                  file=sys.stderr)
    ucv.connect()

    # --record-trajectory wins over --no-rgb.
    capture_rgb = args.record_trajectory or not args.no_rgb

    env = SimWorldNavEnv(
        ucv_client=ucv,
        mcp_client=mcp,
        agent_name=args.agent_name,
        capture_rgb=capture_rgb,
        ensure_pie=not args.no_start_pie,
    )

    # ── Single source of truth: model config flows to both agent + mem0.
    llm = make_llm(
        args.model,
        model=args.model_id,
        base_url=args.base_url,
        api_key=args.api_key,
    )
    memory = build_memory(
        args.memory,
        agent_id=args.agent_name,
        llm_model=args.model_id,
        llm_base_url=args.base_url,
        llm_api_key=args.api_key,
    )

    # ── Multi-episode loop ───────────────────────────────────────────
    all_metrics: List[Dict[str, Any]] = []

    try:
        for ep_idx in range(args.n_episodes):
            seed = args.seed + ep_idx
            if args.task == "pointnav":
                episode = sample_pointnav_episode(
                    ucv, seed=seed,
                    target_distance_cm=args.target_distance,
                    max_steps=args.max_steps,
                )
            else:
                if not args.target_filter:
                    print("ERROR: --target-filter required for objectnav",
                          file=sys.stderr)
                    sys.exit(2)
                substr = args.target_filter
                episode = sample_objectnav_episode(
                    ucv, seed=seed,
                    target_filter=lambda name, s=substr: s in name,
                    object_category=args.object_category,
                    max_steps=args.max_steps,
                )

            run_tag = (
                args.run_name
                or f"{llm.name}_{episode.episode_id}"
            )
            logger = EpisodeLogger(
                run_name=run_tag,
                save_frames=capture_rgb,
                annotate_frames=args.record_trajectory,
                meta={
                    "model": llm.name,
                    "model_id": llm.model,
                    "task": args.task,
                    "episode_id": episode.episode_id,
                    "episode_index": ep_idx,
                    "seed": seed,
                    "n_episodes": args.n_episodes,
                    "memory": args.memory,
                    "args": vars(args),
                },
            )

            try:
                metrics = run_episode(
                    env, llm, episode, logger,
                    memory=memory,
                    max_steps=args.max_steps,
                    vision_history_depth=args.vision_depth,
                )
            finally:
                logger.close()

            all_metrics.append(metrics)
            sr = metrics.get("SR", 0)
            spl = metrics.get("SPL", 0)
            cum_sr = sum(m.get("SR", 0) for m in all_metrics) / len(all_metrics)
            print(
                f"\n== episode {ep_idx+1}/{args.n_episodes}  seed={seed} ==\n"
                f"  SR={sr:.1f}  SPL={spl:.2f}  cumulative_SR={cum_sr:.2f}"
            )

        # ── Experiment summary ───────────────────────────────────────
        n = len(all_metrics)
        avg_sr  = sum(m.get("SR", 0)  for m in all_metrics) / max(n, 1)
        avg_spl = sum(m.get("SPL", 0) for m in all_metrics) / max(n, 1)
        print(
            f"\n{'='*50}\n"
            f"EXPERIMENT DONE: {n} episodes\n"
            f"  avg SR  = {avg_sr:.2f}\n"
            f"  avg SPL = {avg_spl:.2f}\n"
            f"  per-episode SR: {[m.get('SR',0) for m in all_metrics]}\n"
            f"{'='*50}"
        )
    finally:
        env.close()
        ucv.disconnect()


if __name__ == "__main__":
    main()
