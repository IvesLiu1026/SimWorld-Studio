"""Co-evolution loop v4: PIE cycle architecture.

Each epoch:
  1. Exit PIE → editor mode
  2. Coding agent designs scene (spawn/destroy via UCV in editor)
  3. Build NavMesh (editor mode)
  4. Start PIE
  5. Run nav agent episodes
  6. Collect results + update memory
"""
from __future__ import annotations

import json
import logging
import math
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .checkpoint import CheckpointManager
from .coding_agent import CodingAgent
from .coding_memory import CodingAgentMemory
from .config import CoEvolveConfig
from .context_manager import CoEvolveContextManager
from .difficulty import compute_coding_reward, measure_blocked_ratio, measure_float_penalty, score_task_difficulty
from .scene_manager import SceneManager, SceneSpec

log = logging.getLogger(__name__)


class CoEvolutionRunner:
    def __init__(self, config: CoEvolveConfig, resume_dir: str = None):
        self.config = config
        self.gen_results: List[Dict[str, Any]] = []
        self._start_epoch = 0
        self._scene_obj_coords: List[tuple] = []

        if resume_dir:
            self.output_dir = Path(resume_dir)
        else:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.output_dir = Path(config.output_dir) / f"coevolve_{ts}"
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.ckpt = CheckpointManager(self.output_dir)
        self.ctx = CoEvolveContextManager()

        if not resume_dir:
            (self.output_dir / "config.json").write_text(
                json.dumps(vars(config), indent=2, default=str), encoding="utf-8"
            )

    def run(self) -> List[Dict[str, Any]]:
        return self._run_live()

    # ------------------------------------------------------------------
    # PIE helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _exit_pie(ucv) -> None:
        """Exit PIE via UCV. Safe to call when already in editor mode."""
        try:
            resp = ucv.send("vexec /action/exit_pie")
            log.info("exit_pie: %s", resp)
        except Exception as exc:
            log.warning("exit_pie failed (may already be in editor): %s", exc)
        time.sleep(3)

    @staticmethod
    def _start_pie(ucv) -> None:
        """Start PIE via UCV. Waits for game world to be ready."""
        resp = ucv.send("vexec /action/start_pie")
        log.info("start_pie: %s", resp)
        # Wait for PIE world to initialize
        for i in range(30):
            time.sleep(2)
            try:
                status = ucv.send("vget /scene/status")
                if '"pie_mode": true' in status or '"pie_mode":true' in status:
                    log.info("PIE ready (attempt %d)", i + 1)
                    return
            except Exception:
                pass
        log.warning("PIE may not be fully ready after 60s")

    @staticmethod
    def _ensure_navmesh_volume(ucv, mcp_port: int) -> None:
        """Spawn NavMeshBoundsVolume in editor mode via MCP (one-time)."""
        from gym_env.mcp_client import MCPClient
        mcp = MCPClient(port=mcp_port, timeout=20)
        volume_script = '''
import unreal
loc = unreal.Vector(0, 0, 0)
rot = unreal.Rotator(0, 0, 0)
# Check if already exists
for actor in unreal.EditorLevelLibrary.get_all_level_actors():
    if isinstance(actor, unreal.NavMeshBoundsVolume):
        print('VOLUME_EXISTS')
        break
else:
    vol = unreal.EditorLevelLibrary.spawn_actor_from_class(
        unreal.NavMeshBoundsVolume, loc, rot)
    if vol:
        vol.set_actor_scale3d(unreal.Vector(100, 100, 10))
        print('VOLUME_OK')
    else:
        print('VOLUME_FAILED')
'''
        try:
            resp = mcp.execute_python(volume_script, timeout=15)
            logs = resp.get("result", {}).get("python_logs", [])
            for l in logs:
                if l.strip():
                    log.info("[NavVolume] %s", l.strip())
        except Exception as exc:
            log.warning("NavMeshBoundsVolume spawn failed: %s", exc)

    @staticmethod
    def _build_navmesh_editor(ucv, mcp_port: int) -> None:
        """Build NavMesh in editor mode via MCP."""
        from gym_env.mcp_client import MCPClient
        mcp = MCPClient(port=mcp_port, timeout=20)
        build_script = '''
import unreal
world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
nav_sys = unreal.NavigationSystemV1.get_navigation_system(world)
if nav_sys:
    nav_sys.build()
    print('NAVMESH_BUILT')
else:
    print('NO_NAV_SYS')
'''
        try:
            resp = mcp.execute_python(build_script, timeout=15)
            logs = resp.get("result", {}).get("python_logs", [])
            for l in logs:
                if l.strip():
                    log.info("[NavMesh] %s", l.strip())
        except Exception as exc:
            log.warning("NavMesh build failed: %s", exc)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _run_live(self) -> List[Dict[str, Any]]:
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

        from gym_env.episode_builder import sample_pointnav_episode_navmesh, sample_pointnav_episode
        from gym_env.llm import make_llm
        from gym_env.logger import EpisodeLogger
        from gym_env.memory import build_memory
        from gym_env.mcp_client import MCPClient
        from gym_env.runner import run_episode
        from gym_env.simworld_nav_env import SimWorldNavEnv
        from gym_env.ucv_client import UCVClient
        from nav_task.navmesh_interface import NavmeshNavigationInterface

        cfg = self.config

        # Build LLM callables
        coding_llm = self._make_llm_call(cfg.coding_model_id, cfg.coding_base_url, cfg.coding_api_key)
        nav_reflect_llm = self._make_llm_call(cfg.nav_model_id, cfg.nav_base_url, cfg.nav_api_key)

        # Coding agent + memory
        coding_mem = CodingAgentMemory(
            path=str(self.output_dir / "coding_memory.json"),
            reflect_every_n=3, llm_call=coding_llm,
        )
        coding_agent = CodingAgent(llm_call=coding_llm, coding_memory=coding_mem)

        # Nav agent LLM + memory
        nav_llm = make_llm(cfg.nav_model, model=cfg.nav_model_id,
                           base_url=cfg.nav_base_url, api_key=cfg.nav_api_key)
        nav_memory = build_memory(cfg.nav_memory, agent_id="coevolve_nav",
                                  config={"path": str(self.output_dir / "strategy_memory.json")})
        if hasattr(nav_memory, '_llm_call') and nav_memory._llm_call is None:
            nav_memory._llm_call = nav_reflect_llm

        # Connect UCV (works in both editor and PIE modes)
        ucv = UCVClient(host=cfg.ucv_host, port=cfg.ucv_port, name="coevolve-ucv")
        for attempt in range(30):
            try:
                ucv.connect()
                log.info("UnrealCV connected (attempt %d)", attempt + 1)
                break
            except Exception:
                time.sleep(3)
        else:
            raise RuntimeError("UnrealCV not available")

        # One-time: ensure NavMeshBoundsVolume exists
        self._exit_pie(ucv)
        self._ensure_navmesh_volume(ucv, cfg.mcp_port)

        scene_mgr = SceneManager(ucv)
        mcp = MCPClient(host=cfg.mcp_host, port=cfg.mcp_port, name="coevolve-mcp")

        # Resume
        ckpt_state = self.ckpt.load()
        if ckpt_state:
            self._start_epoch = ckpt_state["last_epoch"] + 1
            self.gen_results = self.ckpt.load_gen_results()
            for r in self.gen_results:
                self.ctx.add_generation(r)
            log.info("Resuming from epoch %d", self._start_epoch)
            coding_agent._current_scene_id = ckpt_state.get("current_scene_id", "scene_000")
            coding_agent._current_difficulty = ckpt_state.get("difficulty", 0)

        # ── Main loop ──
        for epoch in range(self._start_epoch, cfg.generations):
            log.info("=" * 60)
            log.info("EPOCH %d / %d", epoch, cfg.generations - 1)
            log.info("=" * 60)

            # ── Phase 1: Exit PIE → Editor mode ──
            self._exit_pie(ucv)
            time.sleep(2)

            # ── Phase 2: Coding agent designs scene ──
            nav_ctx = self.ctx.get_nav_context_for_coding_agent(nav_memory)
            spec = coding_agent.design(
                performance_history=nav_ctx["performance_history"],
                strategies=nav_ctx["strategies"],
                failure_patterns=nav_ctx["failure_summary"],
                current_scene_objects=scene_mgr.get_scene_objects(),
                rolling_summary=nav_ctx.get("rolling_summary", "(no data yet)"),
                current_scene_streak=nav_ctx.get("current_scene_streak", 0),
            )
            spec.max_steps = min(spec.max_steps, cfg.max_steps)
            spec.max_path_cm = min(spec.max_path_cm, 5000.0)
            # cfg.episodes_per_gen is a FLOOR — each epoch runs at least this
            # many tasks, so the per-epoch SR average has enough samples to be
            # a stable signal for the coding agent. Fixes the 4-episode
            # quantization noise from coevolve_20260420_055752.
            spec.n_episodes = max(spec.n_episodes, cfg.episodes_per_gen)

            # Hard-clip path length step-downs to prevent difficulty regression.
            if self.gen_results:
                prev = self.gen_results[-1]
                floor_min = max(500.0, prev.get("min_path_cm", 500.0) - 500.0)
                floor_max = max(1000.0, prev.get("max_path_cm", 1000.0) - 500.0)
                if spec.min_path_cm < floor_min:
                    log.info("Clamping min_path_cm %.0f -> %.0f (max -500 step)",
                             spec.min_path_cm, floor_min)
                    spec.min_path_cm = floor_min
                if spec.max_path_cm < floor_max:
                    log.info("Clamping max_path_cm %.0f -> %.0f (max -500 step)",
                             spec.max_path_cm, floor_max)
                    spec.max_path_cm = floor_max
                if spec.min_path_cm >= spec.max_path_cm:
                    spec.max_path_cm = spec.min_path_cm + 500.0

            is_new_scene = getattr(spec, '_is_new_scene', False)
            is_modify = getattr(spec, '_is_modify', False)
            remove_names = getattr(spec, '_remove_names', [])
            action_label = "NEW_SCENE" if is_new_scene else ("MODIFY" if is_modify else "KEEP")
            log.info("Coding agent: %s | path=[%.0f,%.0f]cm task=%s | %s",
                     action_label, spec.min_path_cm, spec.max_path_cm,
                     spec.task_type, spec.reasoning[:60])

            # Apply scene changes (in editor mode via UCV)
            if is_modify:
                if remove_names:
                    n_removed = scene_mgr.remove_objects(remove_names)
                    log.info("Removed %d objects: %s", n_removed, remove_names)
                    # Update coords
                    self._scene_obj_coords = [
                        c for i, c in enumerate(self._scene_obj_coords)
                        # Keep coords not associated with removed objects
                    ]
                    time.sleep(1)
                if spec.objects:
                    scene_mgr.build_scene(spec)
                    for obj in spec.objects:
                        self._scene_obj_coords.append((obj.x, obj.y))
                    time.sleep(2)

            elif is_new_scene and spec.objects:
                scene_mgr.clear_scene()
                log.info("Cleared old scene for new_scene")
                self._scene_obj_coords = []
                time.sleep(2)
                scene_mgr.build_scene(spec)
                self._scene_obj_coords = [(obj.x, obj.y) for obj in spec.objects]
                time.sleep(2)
                self.ckpt.save_scene(
                    spec.scene_id,
                    [{"actor_name": o.actor_name, "asset_key": o.asset_key,
                      "x": o.x, "y": o.y, "z": o.z, "yaw": o.yaw}
                     for o in spec.objects],
                    spec.description,
                )

            # ── Phase 3: Build NavMesh in editor mode ──
            self._build_navmesh_editor(ucv, cfg.mcp_port)
            time.sleep(2)

            # ── Phase 4: Start PIE ──
            self._start_pie(ucv)
            time.sleep(3)

            # Reconnect UCV after PIE start (new game world)
            ucv.disconnect()
            time.sleep(2)
            for attempt in range(20):
                try:
                    ucv.connect()
                    log.info("UCV reconnected after PIE start (attempt %d)", attempt + 1)
                    break
                except Exception:
                    time.sleep(2)
            else:
                log.error("UCV reconnect failed after PIE start")
                continue

            # ── Phase 5: Generate episodes ──
            nav_interface = None
            blocked_ratio = 0.0
            use_navmesh = False

            try:
                nav_interface = NavmeshNavigationInterface(ucv)
                test_pts = nav_interface.get_navigable_positions(count=10)
                if len(set(int(p.x) for p in test_pts)) > 1:
                    use_navmesh = True
                    log.info("NavMesh OK: %d navigable points", len(test_pts))
                    try:
                        blocked_ratio = measure_blocked_ratio(ucv, seed=cfg.seed + epoch)
                    except Exception:
                        blocked_ratio = 0.0
                else:
                    log.warning("NavMesh not working, using legacy episode gen")
            except Exception as exc:
                log.warning("NavMesh init failed: %s", exc)

            # Compute episode bounds from scene objects
            PLAY_HALF = 5000.0
            scene_obj_coords = list(self._scene_obj_coords)
            if scene_obj_coords:
                obj_xs = [c[0] for c in scene_obj_coords]
                obj_ys = [c[1] for c in scene_obj_coords]
                cx = (min(obj_xs) + max(obj_xs)) / 2
                cy = (min(obj_ys) + max(obj_ys)) / 2
                spread = max(max(obj_xs) - min(obj_xs), max(obj_ys) - min(obj_ys), 2000.0)
                half = max(spread * 0.75, 1500.0) + spec.max_path_cm * 0.5
                half = max(half, 3000.0)
                half = min(half, PLAY_HALF)
                ep_bounds = (cx - half, cy - half, cx + half, cy + half)
                log.info("Episode bounds: center=(%.0f,%.0f) half=%.0f", cx, cy, half)
            else:
                ep_bounds = (-PLAY_HALF, -PLAY_HALF, PLAY_HALF, PLAY_HALF)

            episodes = []
            episodes_data = []
            task_difficulties = []
            for i in range(spec.n_episodes):
                ep_seed = cfg.seed + epoch * 100 + i
                try:
                    if use_navmesh:
                        r = sample_pointnav_episode_navmesh(
                            ucv, seed=ep_seed, idx=i,
                            min_geodesic_cm=spec.min_path_cm,
                            max_geodesic_cm=spec.max_path_cm,
                            max_steps=spec.max_steps,
                            build_navmesh=False, nav_interface=nav_interface,
                            bounds=ep_bounds,
                        )
                        ep = r["episode"]
                        geo = r["difficulty"]["distance_m"] * 100
                        eucl = math.sqrt(
                            (ep.start_position.x - ep.goal_position.x)**2 +
                            (ep.start_position.y - ep.goal_position.y)**2
                        )
                        detour = r["difficulty"]["detour_ratio"]
                        heading_off = r["difficulty"].get("heading_offset_deg", 0)
                    else:
                        dist = (spec.min_path_cm + spec.max_path_cm) / 2
                        ep = sample_pointnav_episode(
                            ucv, seed=ep_seed, idx=i,
                            target_distance_cm=dist,
                            distance_jitter_cm=(spec.max_path_cm - spec.min_path_cm) / 2,
                            max_steps=spec.max_steps,
                        )
                        eucl = math.sqrt(
                            (ep.start_position.x - ep.goal_position.x)**2 +
                            (ep.start_position.y - ep.goal_position.y)**2
                        )
                        geo = eucl
                        detour = 1.0
                        heading_off = 0.0

                    episodes.append(ep)
                    task_diff = score_task_difficulty(
                        geodesic_cm=geo, euclidean_cm=eucl,
                        heading_offset_deg=heading_off,
                        task_type=spec.task_type,
                        blocked_ratio=blocked_ratio,
                    )
                    task_difficulties.append(task_diff)
                    episodes_data.append({
                        "episode_id": ep.episode_id,
                        "start": {"x": ep.start_position.x, "y": ep.start_position.y},
                        "goal": {"x": ep.goal_position.x, "y": ep.goal_position.y},
                        "geodesic_cm": geo, "euclidean_cm": eucl,
                        "detour_ratio": detour,
                        "difficulty": task_diff,
                    })
                    log.info("  Task %d: geo=%.0fcm detour=%.2f diff=%.1f/10",
                             i, geo, detour, task_diff["total"])
                except Exception as exc:
                    log.warning("Episode %d gen failed: %s", i, exc)

            avg_task_diff = (sum(d["total"] for d in task_difficulties) / len(task_difficulties)
                            if task_difficulties else 0.0)
            coding_agent._current_difficulty = avg_task_diff

            if not episodes:
                log.error("No episodes for epoch %d", epoch)
                continue

            # ── Phase 6: Run nav agent episodes ──
            epoch_dir = self.output_dir / f"epoch_{epoch:03d}"
            epoch_dir.mkdir(parents=True, exist_ok=True)

            # Create env for this PIE session.
            # spawn_on_reset=False: agent is spawned ONCE below (ghost mode
            # allows teleport between episodes, avoiding 10s respawn overhead
            # per episode).
            env = SimWorldNavEnv(
                ucv_client=ucv, mcp_client=mcp,
                agent_name="CoEvolveAgent_0",
                capture_rgb=cfg.capture_rgb,
                spawn_on_reset=False, ensure_pie=False,
            )
            # Pre-spawn the agent for this epoch (one-time cost, then
            # subsequent resets just teleport).
            try:
                env._spawn_agent()
                log.info("Agent pre-spawned for epoch %d", epoch)
            except Exception as exc:
                log.warning("Agent pre-spawn failed, falling back to per-episode spawn: %s", exc)
                env.spawn_on_reset = True

            wave_results = []
            all_trajectories = []
            for ep_idx, ep in enumerate(episodes):
                ep_logger = EpisodeLogger(
                    run_name=f"epoch{epoch:03d}_ep{ep_idx:02d}",
                    root=str(epoch_dir), save_frames=False,
                    install_log_handler=False, timestamp_dir=False,
                )
                trajectory = []
                try:
                    metrics = run_episode(
                        env, nav_llm, ep, ep_logger,
                        memory=nav_memory,
                        max_steps=spec.max_steps,
                        vision_history_depth=cfg.vision_depth,
                    )
                    sr = float(metrics.get("SR", 0) or 0)
                    spl = float(metrics.get("SPL", 0) or 0)
                    steps = int(env.step_count)

                    if hasattr(nav_memory, 'reflect'):
                        outcome = f"{'SUCCESS' if sr > 0 else 'FAILED'}: {steps} steps"
                        try:
                            nav_memory.reflect(outcome)
                        except Exception:
                            pass

                    result = {
                        "episode_id": ep.episode_id,
                        "SR": sr, "SPL": spl, "steps": steps,
                        "path_length_cm": float(metrics.get("path_length_cm", 0) or 0),
                        "ended_reason": "success" if sr > 0 else "max_steps",
                    }
                    wave_results.append(result)
                    log.info("  ep %d: SR=%d steps=%d %s",
                             ep_idx, sr, steps, result["ended_reason"])
                except Exception as exc:
                    log.error("  ep %d FAILED: %s: %s", ep_idx,
                              type(exc).__name__, exc, exc_info=True)
                    wave_results.append({
                        "episode_id": ep.episode_id,
                        "SR": 0, "SPL": 0, "steps": 0,
                        "ended_reason": "error",
                    })
                all_trajectories.append(trajectory)

            # ── Phase 7: Aggregate + save ──
            n = len(wave_results)
            n_success = sum(1 for r in wave_results if r.get("SR", 0) > 0)
            sr = n_success / n if n else 0
            spl = sum(r.get("SPL", 0) for r in wave_results) / n if n else 0
            avg_steps = sum(r.get("steps", 0) for r in wave_results) / n if n else 0
            # Measure float penalty in PIE — check if objects are grounded.
            float_penalty = 0.0
            spawned = scene_mgr.get_scene_objects()
            if spawned:
                try:
                    float_penalty = measure_float_penalty(ucv, spawned)
                except Exception as fp_exc:
                    log.warning("Float penalty check failed: %s", fp_exc)

            coding_reward = compute_coding_reward(
                sr, difficulty=avg_task_diff,
                best_difficulty=coding_agent._best_difficulty,
                float_penalty=float_penalty,
            )

            coding_mem.record(epoch, spec, sr, avg_task_diff)
            coding_mem.maybe_reflect(epoch)

            nav_strategies = nav_memory.query("", k=10) if hasattr(nav_memory, 'query') else []

            gen_record = {
                "generation": epoch,
                "sr": sr, "spl": spl, "avg_steps": avg_steps,
                "n_episodes": n, "n_success": n_success,
                "difficulty_score": avg_task_diff,
                "task_difficulties": task_difficulties,
                "blocked_ratio": blocked_ratio,
                "coding_reward": coding_reward,
                "scene_id": spec.scene_id,
                "scene_description": spec.description,
                "task_type": spec.task_type,
                "min_path_cm": spec.min_path_cm,
                "max_path_cm": spec.max_path_cm,
                "task_reasoning": spec.reasoning,
                "nav_strategies": list(nav_strategies),
                "coding_principles": list(coding_mem.principles),
                "episode_results": wave_results,
            }
            self.gen_results.append(gen_record)
            self.ctx.add_generation(gen_record)

            self.ckpt.save_epoch_data(epoch, spec.to_dict(), episodes_data,
                                       all_trajectories, gen_record)

            scene_objs = [
                {"actor_name": n, "asset_key": "", "x": 0, "y": 0, "z": 0, "yaw": 0}
                for n in scene_mgr.get_scene_objects()
            ]
            ckpt_dict = vars(cfg).copy()
            ckpt_dict["difficulty"] = avg_task_diff
            self.ckpt.save(epoch, self.gen_results, spec.scene_id, scene_objs, ckpt_dict)

            summary = (
                f"Epoch {epoch}: SR={sr:.0%} SPL={spl:.3f} "
                f"diff={avg_task_diff:.1f}/10 reward={coding_reward:.2f} "
                f"scene={spec.scene_id}({len(scene_mgr.get_scene_objects())}obj) "
                f"| {spec.reasoning[:50]}"
            )
            log.info(summary)
            print(f"\n  >>> {summary}\n")

        # Final
        self._exit_pie(ucv)
        self._save_final()
        scene_mgr.clear_scene()
        ucv.disconnect()
        return self.gen_results

    def _save_final(self):
        path = self.output_dir / "all_results.json"
        path.write_text(json.dumps(self.gen_results, indent=2, default=str), encoding="utf-8")
        log.info("Results saved: %s", path)

    @staticmethod
    def _make_llm_call(model_id, base_url, api_key):
        """Build a per-call LLM closure with fresh OpenAI client each call.

        Fresh client = fresh httpx pool. Reusing a single long-lived client
        across ucv.connect() on Windows reproduced WinError 10061 on every
        subsequent outbound request. Per-call clients work. Connect timeout
        is short so transient SYN rejections fail fast and retry.
        """
        from openai import OpenAI
        import httpx as _httpx
        _timeout = _httpx.Timeout(connect=5.0, read=300.0, write=30.0, pool=5.0)

        def call(prompt: str) -> str:
            client = OpenAI(
                api_key=api_key, base_url=base_url,
                timeout=_timeout, max_retries=6,
            )
            try:
                resp = client.chat.completions.create(
                    model=model_id,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=8192, temperature=0.7,
                )
                return resp.choices[0].message.content or ""
            finally:
                try:
                    client.close()
                except Exception:
                    pass
        return call
