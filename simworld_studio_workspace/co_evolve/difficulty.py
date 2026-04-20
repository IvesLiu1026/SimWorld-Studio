"""Task difficulty scoring: 0-10 per task, rubric-based.

Each dimension is scored independently, total = sum of all dimensions.
Scores come from the task's GT trajectory (from NavMesh episode generation).

NavMesh is REQUIRED. If unavailable, the epoch must be skipped and
the coding agent must be told to fix the scene.

Dimensions:
  1. Path length (0-2.5): geodesic distance of GT path
  2. Detour ratio (0-2.5): geodesic / euclidean — how winding the GT path is
  3. Scene blocked ratio (0-2.5): fraction of area blocked by objects (NavMesh)
  4. Heading offset (0-1.0): how far agent starts from facing goal
  5. Task type (0-1.5): objectnav harder than pointnav

Max total: 2.5 + 2.5 + 2.5 + 1.0 + 1.5 = 10.0
"""
from __future__ import annotations

import logging
import math
import random
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


class NavMeshRequired(Exception):
    """Raised when NavMesh is not available but required."""
    pass


def score_task_difficulty(
    geodesic_cm: float,
    euclidean_cm: float,
    heading_offset_deg: float = 0.0,
    task_type: str = "pointnav",
    blocked_ratio: float = 0.0,
) -> Dict[str, Any]:
    """Score a single task's difficulty 0-10.

    Args:
        geodesic_cm: GT path length from NavMesh
        euclidean_cm: straight-line start-to-goal distance
        heading_offset_deg: angle between start heading and goal direction (0-180)
        task_type: "pointnav" or "objectnav"
        blocked_ratio: fraction of scene area blocked (from NavMesh measurement)
    """
    # 1. Path length (0-2.5): 500cm=0.5, 1000=1.0, 2500=2.5
    path_score = min(2.5, geodesic_cm / 1000.0)

    # 2. Detour ratio (0-2.5): 1.0=straight=0, 1.5=moderate=1.25, 2.0+=max
    detour = geodesic_cm / euclidean_cm if euclidean_cm > 0 else 1.0
    detour_score = min(2.5, (detour - 1.0) * 2.5)
    detour_score = max(0.0, detour_score)

    # 3. Scene blocked ratio (0-2.5): 0=empty=0, 0.3=moderate=1.5, 0.5+=max
    scene_score = min(2.5, blocked_ratio * 5.0)

    # 4. Heading offset (0-1.0): 0°=facing goal=0, 90°=0.5, 180°=1.0
    heading_score = min(1.0, heading_offset_deg / 180.0)

    # 5. Task type (0-1.5)
    task_score = 0.0 if task_type == "pointnav" else 1.5

    total = path_score + detour_score + scene_score + heading_score + task_score
    total = round(min(10.0, total), 1)

    return {
        "total": total,
        "path_length": round(path_score, 2),
        "detour_ratio": round(detour_score, 2),
        "detour_raw": round(detour, 3),
        "scene_blocked": round(scene_score, 2),
        "heading_offset": round(heading_score, 2),
        "task_type_score": round(task_score, 2),
        "geodesic_cm": round(geodesic_cm, 0),
        "euclidean_cm": round(euclidean_cm, 0),
    }


def measure_blocked_ratio(
    ucv,
    bounds_half: float = 5000.0,
    n_samples: int = 100,
    seed: int = 42,
) -> float:
    """Measure fraction of scene area blocked by objects.

    Samples uniform grid points and uses NavMesh projection to check
    if each point is navigable. Returns blocked_ratio = 1 - navigable_ratio.

    Raises NavMeshRequired if NavMesh is not functional.
    """
    rng = random.Random(seed)
    n_navigable = 0
    n_total = 0

    for _ in range(n_samples):
        x = rng.uniform(-bounds_half, bounds_half)
        y = rng.uniform(-bounds_half, bounds_half)
        n_total += 1
        try:
            resp = ucv.send(f"vget /nav/project {x} {y} 0")
            resp = resp.strip()
            if resp and not resp.startswith("error") and resp != "-1":
                parts = resp.split(",")
                if len(parts) >= 2:
                    px, py = float(parts[0]), float(parts[1])
                    # Valid if projected point is not degenerate (all zeros)
                    if abs(px) > 1 or abs(py) > 1:
                        n_navigable += 1
                    elif abs(x) < 100 and abs(y) < 100:
                        # Near origin is valid
                        n_navigable += 1
        except Exception:
            pass

    if n_total == 0:
        raise NavMeshRequired("No samples taken")

    navigable_ratio = n_navigable / n_total
    if navigable_ratio < 0.01:
        raise NavMeshRequired(
            f"NavMesh returned 0 navigable points out of {n_total} samples. "
            "NavMesh may not be built or scene has no walkable surface."
        )

    blocked = 1.0 - navigable_ratio
    log.info("Blocked ratio: %.2f (%d/%d navigable)", blocked, n_navigable, n_total)
    return blocked


def compute_coding_reward(nav_sr: float) -> float:
    """Adversarial reward: 1 - SR, but 0 if SR < 0.1 (too hard)."""
    if nav_sr < 0.1:
        return 0.0
    return 1.0 - nav_sr
