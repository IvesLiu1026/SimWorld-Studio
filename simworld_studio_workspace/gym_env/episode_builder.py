"""Builds NavigationEpisode objects from a live UE scene.

Replaces ``nav_task.NavigationTaskGenerator`` for our pipeline.  Where
the original generator samples positions from a SimWorld road graph
(loaded from ``roads.json``), we sample from whatever a coding agent
already spawned in the UE world via ``vget /objects`` + per-object
``vget /object/{name}/location``.

Two task variants are exposed:

  * :func:`sample_pointnav_episode` — choose a random goal coordinate
    a configurable distance from the agent start.
  * :func:`sample_objectnav_episode` — choose a target object actor
    matching a name filter; the agent's job is to walk to it.

Both produce a fully-populated :class:`NavigationEpisode` JSON-ready
for ``env.reset(episode)``.
"""

from __future__ import annotations

import logging
import math
import random
import uuid
from datetime import datetime, timezone
from typing import Callable, List, Optional, Sequence, Tuple

from nav_task.episode import (
    EvaluationMetrics,
    NavigationEpisode,
    ObjectGoal,
    ObjectViewPoint,
    Position,
    ReferencePath,
    RewardConfig,
    SuccessCriteria,
    WorldConfig,
)

from .ucv_client import UCVClient

log = logging.getLogger(__name__)

_DEFAULT_SPAWN_Z = 110.0  # humanoid agent spawn Z (matches agent-registry.json)


# ---------------------------------------------------------------------------
# Scene snapshot helpers
# ---------------------------------------------------------------------------

def snapshot_scene(ucv: UCVClient) -> List[Tuple[str, Tuple[float, float, float]]]:
    """List all UE actors and their locations.

    Uses UnrealCV (PIE-safe) rather than MCP so this works after PIE
    has been entered.
    """
    names = ucv.vget_objects()
    out: List[Tuple[str, Tuple[float, float, float]]] = []
    for name in names:
        try:
            loc = ucv.vget_location(name)
            out.append((name, loc))
        except Exception as exc:
            log.debug("scene snapshot: skipped %s (%s)", name, exc)
    log.info("snapshot_scene: %d actors", len(out))
    return out


def filter_actors(
    actors: Sequence[Tuple[str, Tuple[float, float, float]]],
    name_filter: Optional[Callable[[str], bool]],
) -> List[Tuple[str, Tuple[float, float, float]]]:
    if name_filter is None:
        return list(actors)
    return [a for a in actors if name_filter(a[0])]


# ---------------------------------------------------------------------------
# Episode constructors
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_episode_id(seed: int, idx: int = 0) -> str:
    return f"nav_ep_{seed}_{idx:03d}"


def _make_world(map_label: str = "live_ue_scene") -> WorldConfig:
    return WorldConfig(
        map_file=map_label,
        coordinate_unit="cm",
        x_min=-100_000.0, x_max=100_000.0,
        y_min=-100_000.0, y_max=100_000.0,
    )


def _build_episode(
    *,
    seed: int,
    idx: int,
    start: Position,
    goal: Position,
    success_distance_cm: float,
    max_steps: int,
    max_episode_time_s: float,
    reward_config: RewardConfig,
    object_goal: Optional[ObjectGoal] = None,
    task_type: str = "pointnav",
    object_category: Optional[str] = None,
    map_label: str = "live_ue_scene",
) -> NavigationEpisode:
    straight = math.sqrt((start.x - goal.x) ** 2 + (start.y - goal.y) ** 2)
    ref_path = ReferencePath(
        waypoints=(start, goal),
        shortest_path_length_cm=straight,
    )
    return NavigationEpisode(
        episode_id=_new_episode_id(seed, idx),
        seed=seed,
        world=_make_world(map_label),
        start_position=start,
        goal_position=goal,
        reference_path=ref_path,
        success_criteria=SuccessCriteria(
            success_distance_cm=success_distance_cm,
            max_steps=max_steps,
            max_episode_time_s=max_episode_time_s,
        ),
        evaluation_metrics=EvaluationMetrics(
            success_distance_cm=success_distance_cm,
            shortest_path_length_cm=straight,
        ),
        generated_at=_now_iso(),
        reward_config=reward_config,
        task_type=task_type,
        object_category=object_category,
        object_goal=object_goal,
    )


def sample_pointnav_episode(
    ucv: UCVClient,
    *,
    seed: int = 42,
    idx: int = 0,
    target_distance_cm: float = 2000.0,
    distance_jitter_cm: float = 500.0,
    success_distance_cm: float = 200.0,
    max_steps: int = 60,
    max_episode_time_s: float = 300.0,
    start_xy: Optional[Tuple[float, float]] = None,
    reward_config: Optional[RewardConfig] = None,
) -> NavigationEpisode:
    """Sample a PointNav episode in the current UE scene.

    The agent start defaults to ``(0, 0)``; the goal is placed at a
    random heading at roughly ``target_distance_cm`` cm away.  We do
    NOT verify navigability — without a navmesh query, no offline
    check is meaningful.
    """
    rng = random.Random(seed + idx)
    sx, sy = start_xy if start_xy is not None else (0.0, 0.0)
    distance = target_distance_cm + rng.uniform(-distance_jitter_cm, distance_jitter_cm)
    angle = rng.uniform(0, 2 * math.pi)
    gx = sx + distance * math.cos(angle)
    gy = sy + distance * math.sin(angle)

    start = Position(x=sx, y=sy, node_type="intersection")
    goal = Position(x=gx, y=gy, node_type="intersection")
    rc = reward_config or RewardConfig()

    ep = _build_episode(
        seed=seed, idx=idx, start=start, goal=goal,
        success_distance_cm=success_distance_cm,
        max_steps=max_steps, max_episode_time_s=max_episode_time_s,
        reward_config=rc,
        task_type="pointnav",
    )
    log.info(
        "pointnav episode %s: start=(%.0f,%.0f) goal=(%.0f,%.0f) d=%.0fcm",
        ep.episode_id, sx, sy, gx, gy, distance,
    )
    return ep


def sample_objectnav_episode(
    ucv: UCVClient,
    *,
    seed: int = 42,
    idx: int = 0,
    target_filter: Callable[[str], bool],
    object_category: str,
    success_distance_cm: float = 300.0,
    max_steps: int = 60,
    max_episode_time_s: float = 300.0,
    start_xy: Optional[Tuple[float, float]] = None,
    min_separation_cm: float = 800.0,
    reward_config: Optional[RewardConfig] = None,
) -> NavigationEpisode:
    """Sample an ObjectNav episode by picking a target actor in the scene.

    Parameters
    ----------
    target_filter : callable
        Returns True for actor names that count as valid targets, e.g.
        ``lambda name: name.startswith("BP_Trash_can")``.
    object_category : str
        Semantic label included in the agent prompt
        (``"Find and navigate to a {category}"``).
    """
    rng = random.Random(seed + idx)
    sx, sy = start_xy if start_xy is not None else (0.0, 0.0)

    actors = snapshot_scene(ucv)
    candidates = filter_actors(actors, target_filter)
    if not candidates:
        raise RuntimeError(
            f"sample_objectnav_episode: no actors matched filter; "
            f"scene has {len(actors)} actors"
        )

    far_enough = [
        c for c in candidates
        if math.sqrt((c[1][0] - sx) ** 2 + (c[1][1] - sy) ** 2) >= min_separation_cm
    ]
    pool = far_enough or candidates
    target_name, target_loc = rng.choice(pool)
    gx, gy, _gz = target_loc

    start = Position(x=sx, y=sy, node_type="intersection")
    goal = Position(x=gx, y=gy, node_type="intersection")
    rc = reward_config or RewardConfig()

    obj_goal = ObjectGoal(
        object_id=target_name,
        object_type=target_name,  # we don't have the BP class string here
        object_category=object_category,
        position=goal,
        view_points=(ObjectViewPoint(position=goal, iou=None),),
    )
    ep = _build_episode(
        seed=seed, idx=idx, start=start, goal=goal,
        success_distance_cm=success_distance_cm,
        max_steps=max_steps, max_episode_time_s=max_episode_time_s,
        reward_config=rc,
        object_goal=obj_goal,
        task_type="objectnav",
        object_category=object_category,
    )
    log.info(
        "objectnav episode %s: target=%s @ (%.0f,%.0f) start=(%.0f,%.0f)",
        ep.episode_id, target_name, gx, gy, sx, sy,
    )
    return ep
