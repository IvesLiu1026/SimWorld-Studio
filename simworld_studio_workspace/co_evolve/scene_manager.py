"""Scene manager: build/clear UE scenes via UnrealCV.

Handles spawning and destroying objects to create training environments.
The coding agent outputs a SceneSpec, and this module materializes it in UE.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Asset catalog — auto-generated from CityDatabase/blueprints
# ---------------------------------------------------------------------------

def _build_asset_catalog():
    """Build full asset catalog from CityDatabase.

    Buildings use numbered format: building_01 .. building_127.
    Other assets use descriptive names.
    """
    catalog = {}

    # Buildings: 01-48, 100-127 (125 total, skipping gaps)
    _building_nums = list(range(1, 49)) + list(range(100, 128))
    for n in _building_nums:
        key = f"building_{n:02d}" if n < 100 else f"building_{n}"
        bp = f"BP_Building_{n:02d}" if n < 100 else f"BP_Building_{n}"
        catalog[key] = f"/Game/CityDatabase/blueprints/{bp}.{bp}_C"

    # Trees (6)
    for i in range(1, 7):
        catalog[f"tree_{i}"] = f"/Game/CityDatabase/blueprints/BP_Tree{i}.BP_Tree{i}_C"

    # Street furniture & obstacles
    _furniture = {
        "table": "BP_Table", "table_2": "BP_Table2", "table_3": "BP_Table3",
        "hydrant": "BP_Hydrant",
        "trash_bin": "BP_Trash_bin_a", "trash_can": "BP_Trash_can",
        "road_blocker": "BP_RoadBlocker", "road_cone": "BP_RoadCone",
        "couch": "BP_Couch",
        "box": "BP_Box", "box_2": "BP_Box2", "box_3": "BP_Box3",
        "can": "BP_Can", "can_2": "BP_Can2",
        "soda_1": "BP_Soda1", "soda_2": "BP_Soda2",
        "soda_3": "BP_Soda3", "soda_4": "BP_Soda4",
        "rabbish": "BP_Rabbish",
    }
    for key, bp in _furniture.items():
        catalog[key] = f"/Game/CityDatabase/blueprints/{bp}.{bp}_C"

    # Vehicles
    catalog["scooter"] = "/Game/CityDatabase/blueprints/BP_Scooter_01.BP_Scooter_01_C"
    catalog["cart"] = "/Game/CityDatabase/blueprints/BP_Cart.BP_Cart_C"
    catalog["cart_2"] = "/Game/CityDatabase/blueprints/BP_Cart2.BP_Cart2_C"

    return catalog


ASSET_CATALOG = _build_asset_catalog()

# Semantic groupings for the coding agent prompt
_building_keys = [k for k in ASSET_CATALOG if k.startswith("building_")]
_tree_keys = [k for k in ASSET_CATALOG if k.startswith("tree_")]
_obstacle_keys = [k for k in ASSET_CATALOG
                  if not k.startswith("building_") and not k.startswith("tree_")]

ASSET_CATEGORIES = {
    "buildings": _building_keys,
    "trees": _tree_keys,
    "obstacles": _obstacle_keys,
}


@dataclass
class SpawnedObject:
    """One object in the scene."""
    actor_name: str
    asset_key: str          # key in ASSET_CATALOG
    x: float
    y: float
    z: float = 0.0
    yaw: float = 0.0


@dataclass
class SceneSpec:
    """Complete scene specification output by the coding agent."""
    scene_id: str
    description: str                            # verbal description of the scene
    objects: List[SpawnedObject] = field(default_factory=list)
    task_type: str = "pointnav"                 # "pointnav" or "objectnav"
    min_path_cm: float = 500.0
    max_path_cm: float = 2000.0
    max_steps: int = 30
    n_episodes: int = 4
    reasoning: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scene_id": self.scene_id,
            "description": self.description,
            "objects": [
                {"name": o.actor_name, "asset": o.asset_key,
                 "x": o.x, "y": o.y, "z": o.z, "yaw": o.yaw}
                for o in self.objects
            ],
            "task_type": self.task_type,
            "min_path_cm": self.min_path_cm,
            "max_path_cm": self.max_path_cm,
            "max_steps": self.max_steps,
            "n_episodes": self.n_episodes,
            "reasoning": self.reasoning,
        }


class SceneManager:
    """Build and clear scenes in UE via UnrealCV."""

    # Ground plane asset — standard UE Cube flattened.
    # Required for NavMesh to work on maps without walkable geometry.
    GROUND_MESH = "/Engine/BasicShapes/Cube.Cube"
    GROUND_NAME = "CoEvolve_Ground"
    GROUND_SCALE = (200, 200, 1)  # 20000x20000x100 cm

    def __init__(self, ucv):
        self.ucv = ucv
        self._spawned_names: List[str] = []
        self._ground_placed = False

    def build_scene(self, spec: SceneSpec) -> bool:
        """Spawn all objects in a SceneSpec. Returns True if successful.

        Objects are spawned incrementally — we do NOT clear previous objects
        first. The coding agent should manage the scene by adding new objects.
        Use clear_scene() explicitly only when needed.
        """
        log.info("Building scene '%s': adding %d objects...",
                 spec.scene_id, len(spec.objects))

        success_count = 0
        for obj in spec.objects:
            # Skip if already spawned
            if obj.actor_name in self._spawned_names:
                log.info("  %s already exists, skipping", obj.actor_name)
                continue

            bp_path = ASSET_CATALOG.get(obj.asset_key)
            if bp_path is None:
                log.warning("Unknown asset key '%s', skipping", obj.asset_key)
                continue

            try:
                self.ucv.spawn_bp_asset(
                    bp_path, obj.actor_name,
                    location=(obj.x, obj.y, obj.z),
                    rotation=(0.0, obj.yaw, 0.0),
                    collision_mode=2,  # XY-only: separate objects, no ground trace
                )
                self._spawned_names.append(obj.actor_name)
                success_count += 1
                log.info("  Spawned %s (%s) at (%.0f, %.0f)",
                         obj.actor_name, obj.asset_key, obj.x, obj.y)
            except Exception as exc:
                log.warning("  Failed to spawn %s: %s", obj.actor_name, exc)

        log.info("Scene: %d new + %d existing = %d total objects",
                 success_count, len(self._spawned_names) - success_count,
                 len(self._spawned_names))
        return True

    def clear_scene(self):
        """Destroy all objects we spawned (keep the agent and engine objects)."""
        if not self._spawned_names:
            return
        log.info("Clearing %d spawned objects...", len(self._spawned_names))
        for name in self._spawned_names:
            try:
                self.ucv.send(f"vset /object/{name}/destroy")
            except Exception:
                pass
        self._spawned_names.clear()

    def remove_objects(self, names: List[str]) -> int:
        """Destroy specific objects by name. Returns count of removed objects."""
        removed = 0
        for name in names:
            if name in self._spawned_names:
                try:
                    self.ucv.send(f"vset /object/{name}/destroy")
                    self._spawned_names.remove(name)
                    removed += 1
                    log.info("  Removed %s", name)
                except Exception as exc:
                    log.warning("  Failed to remove %s: %s", name, exc)
            else:
                log.info("  %s not found in spawned objects, skipping", name)
        return removed

    def ensure_ground(self):
        """Spawn a walkable ground plane if not already present.
        Required for NavMesh to work on maps without built-in walkable geometry."""
        if self._ground_placed:
            return
        existing = self.ucv.vget_objects()
        if self.GROUND_NAME in existing:
            self._ground_placed = True
            return
        log.info("Spawning ground plane for NavMesh...")
        try:
            self.ucv.spawn_bp_asset(
                self.GROUND_MESH, self.GROUND_NAME,
                location=(0, 0, -50),  # slightly below origin
            )
            sx, sy, sz = self.GROUND_SCALE
            self.ucv.send(f"vset /object/{self.GROUND_NAME}/scale {sx} {sy} {sz}")
            self._ground_placed = True
            log.info("Ground plane placed: %s scale=(%d,%d,%d)", self.GROUND_NAME, sx, sy, sz)
        except Exception as exc:
            log.warning("Ground plane spawn failed: %s", exc)

    def get_scene_objects(self) -> List[str]:
        """Return names of currently spawned scene objects (excluding ground)."""
        return [n for n in self._spawned_names if n != self.GROUND_NAME]

    @staticmethod
    def get_asset_catalog_prompt() -> str:
        """Format asset catalog for injection into coding agent prompt."""
        lines = ["Available assets for scene building:"]
        # Show buildings as a range to avoid bloating the prompt
        n_buildings = len(ASSET_CATEGORIES["buildings"])
        lines.append(f"  buildings ({n_buildings} types): building_01, building_02, ... building_48, "
                      "building_100 ... building_127. Each has a unique shape and size.")
        # Trees
        tree_items = ", ".join(ASSET_CATEGORIES["trees"])
        lines.append(f"  trees: {tree_items}")
        # Obstacles
        obstacle_items = ", ".join(ASSET_CATEGORIES["obstacles"])
        lines.append(f"  obstacles/furniture: {obstacle_items}")
        lines.append("")
        lines.append("Coordinates: UE units (1m = 100 units). "
                      "Buildings are large obstacles (fill ~500-2000 units of ground). "
                      "Keep objects spaced at least 500 units apart. "
                      "ALL objects MUST be placed at z=0 (ground level).")
        return "\n".join(lines)
