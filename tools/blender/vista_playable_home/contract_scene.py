"""HouseSpec-driven render plan for the VISTA Playable Home.

HouseSpec is the sole authority for semantic IDs, transforms, metadata and
topology.  This module only attaches deterministic geometry recipes and
non-semantic room bundle meshes to that contract.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import pathlib
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Sequence

from .scene import (
    GRID_M,
    HOUSE_ID,
    REVISION,
    MaterialPlan,
    PreviewView,
    PrimitivePlan,
    RoomPlan,
    _materials,
    box,
    canonical_json_bytes,
    cylinder,
    sphere,
    torus,
)


HOUSE_SCHEMA = "simworld.vista.playable-house/v1"
MANIFEST_SCHEMA = "simworld.vista.playable-home-blender-manifest/v1"
SUPPORTED_ROOMS = {
    "entry_hall",
    "living_room",
    "kitchen_dining",
    "bedroom",
    "office",
    "bathroom_laundry",
}
OUTPUT_FILENAMES = {
    "blend": "source.blend",
    "glb": "vista_playable_home_r1.glb",
    "preview_overview": "preview-overview.png",
    "preview_interior": "preview-interior.png",
    "normalized_manifest": "normalized-manifest.json",
    "manifest": "manifest.json",
}


class HousePlanError(ValueError):
    """A fail-closed source-contract or geometry-planning error."""


@dataclass(frozen=True)
class RenderNode:
    node_id: str
    room_id: str
    category: str
    label: str
    asset_ref: str
    transform_location_m: tuple[float, float, float]
    transform_rotation_deg: tuple[float, float, float]
    transform_scale: tuple[float, float, float]
    component_role: str
    mobility: str
    collision_policy: str
    nav_obstacle: bool
    affordances: tuple[str, ...]
    initial_state: dict[str, Any]
    tags: tuple[str, ...]
    primitives: tuple[PrimitivePlan, ...]
    semantic_entity_id: str | None = None
    instance_group: str | None = None

    @property
    def preview_visible(self) -> bool:
        return "preview_hide" not in self.tags and self.initial_state.get("visible", True) is not False


@dataclass(frozen=True)
class ContractScenePlan:
    house: dict[str, Any]
    rooms: tuple[RoomPlan, ...]
    nodes: tuple[RenderNode, ...]
    materials: tuple[MaterialPlan, ...]
    preview_views: tuple[PreviewView, ...]


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HousePlanError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _digest_body(house: dict[str, Any]) -> str:
    body = {key: value for key, value in house.items() if key != "content_digest"}
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def load_house(path: pathlib.Path) -> dict[str, Any]:
    """Read a pinned HouseSpec without accepting links, traversal or drift."""

    if not path.is_absolute():
        raise HousePlanError("--house must be an absolute path")
    if path.is_symlink() or not path.is_file():
        raise HousePlanError("--house must be a regular non-symlink file")
    try:
        house = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_pairs)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HousePlanError(f"invalid HouseSpec JSON: {error}") from error
    if not isinstance(house, dict):
        raise HousePlanError("HouseSpec root must be an object")
    validate_house(house)
    return house


def _v3(value: Any, pointer: str) -> tuple[float, float, float]:
    if not isinstance(value, list) or len(value) != 3:
        raise HousePlanError(f"{pointer} must be a three-element vector")
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(float(item)) for item in value):
        raise HousePlanError(f"{pointer} must contain finite numbers")
    return tuple(float(item) for item in value)  # type: ignore[return-value]


def validate_house(house: dict[str, Any]) -> None:
    if house.get("schema_version") != HOUSE_SCHEMA or house.get("house_id") != HOUSE_ID or house.get("revision") != REVISION:
        raise HousePlanError("unsupported HouseSpec identity")
    if house.get("units") != "meters" or not isinstance(house.get("seed"), int):
        raise HousePlanError("HouseSpec must use metres and an integer seed")
    digest = house.get("content_digest")
    if not isinstance(digest, str) or len(digest) != 64 or digest != _digest_body(house):
        raise HousePlanError("HouseSpec content_digest mismatch")
    rooms = house.get("rooms")
    portals = house.get("portals")
    entities = house.get("entities")
    if not isinstance(rooms, list) or not isinstance(portals, list) or not isinstance(entities, list):
        raise HousePlanError("HouseSpec rooms, portals and entities must be arrays")
    room_ids: set[str] = set()
    inventory_ids: set[str] = set()
    for index, room in enumerate(rooms):
        if not isinstance(room, dict) or room.get("kind") not in SUPPORTED_ROOMS:
            raise HousePlanError(f"unsupported room at rooms[{index}]")
        room_id = room.get("room_id")
        if not isinstance(room_id, str) or room_id in room_ids:
            raise HousePlanError("room ids must be unique strings")
        room_ids.add(room_id)
        _v3(room.get("transform", {}).get("location_m"), f"rooms[{index}].transform.location_m")
        minimum = _v3(room.get("bounds_m", {}).get("min_m"), f"rooms[{index}].bounds_m.min_m")
        maximum = _v3(room.get("bounds_m", {}).get("max_m"), f"rooms[{index}].bounds_m.max_m")
        if any(maximum[axis] <= minimum[axis] for axis in range(3)):
            raise HousePlanError("room bounds must have positive extent")
        inventory = room.get("semantic_inventory")
        if not isinstance(inventory, list) or any(not isinstance(item, str) for item in inventory):
            raise HousePlanError("room semantic_inventory must be a string array")
        inventory_ids.update(inventory)
    if {room.get("kind") for room in rooms} != SUPPORTED_ROOMS:
        raise HousePlanError("the r1 build requires exactly the six supported zones")
    entity_ids: set[str] = set()
    for index, entity in enumerate(entities):
        if not isinstance(entity, dict):
            raise HousePlanError(f"entities[{index}] must be an object")
        entity_id = entity.get("entity_id")
        if not isinstance(entity_id, str) or entity_id in entity_ids:
            raise HousePlanError("entity ids must be unique strings")
        entity_ids.add(entity_id)
        if entity.get("room_id") not in room_ids:
            raise HousePlanError(f"entity references unknown room: {entity_id}")
        _v3(entity.get("transform", {}).get("location_m"), f"entities[{index}].transform.location_m")
    if entity_ids != inventory_ids:
        raise HousePlanError("room semantic inventories must equal the exact entity set")
    portal_ids: set[str] = set()
    adjacency = {room_id: set() for room_id in room_ids}
    for index, portal in enumerate(portals):
        if not isinstance(portal, dict):
            raise HousePlanError(f"portals[{index}] must be an object")
        portal_id = portal.get("portal_id")
        if not isinstance(portal_id, str) or portal_id in portal_ids:
            raise HousePlanError("portal ids must be unique strings")
        portal_ids.add(portal_id)
        source = portal.get("from_room_id")
        target = portal.get("to_room_id")
        if source not in room_ids or target not in room_ids or source == target:
            raise HousePlanError(f"invalid portal endpoints: {portal_id}")
        adjacency[source].add(target)
        adjacency[target].add(source)
        _v3(portal.get("world_transform", {}).get("location_m"), f"portals[{index}].world_transform.location_m")
        door_id = portal.get("door_entity_id")
        if door_id is not None and door_id not in entity_ids:
            raise HousePlanError(f"portal references unknown door: {portal_id}")
    visited: set[str] = set()
    pending = [next(iter(room_ids))]
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        pending.extend(adjacency[current] - visited)
    if visited != room_ids:
        raise HousePlanError("HouseSpec room graph is disconnected")


def _room_plans(house: dict[str, Any]) -> tuple[RoomPlan, ...]:
    result: list[RoomPlan] = []
    for room in house["rooms"]:
        result.append(
            RoomPlan(
                room_id=room["room_id"],
                kind=room["kind"],
                label=room["label"],
                transform_location_m=_v3(room["transform"]["location_m"], "room.transform.location_m"),
                bounds_min_m=_v3(room["bounds_m"]["min_m"], "room.bounds_m.min_m"),
                bounds_max_m=_v3(room["bounds_m"]["max_m"], "room.bounds_m.max_m"),
                anchor_m=_v3(room["anchor_m"], "room.anchor_m"),
                review_cameras=tuple(copy.deepcopy(room["review_cameras"])),
            )
        )
    return tuple(sorted(result, key=lambda item: item.room_id))


def _local(room: RoomPlan, world: Sequence[float]) -> tuple[float, float, float]:
    return tuple(float(world[index]) - room.transform_location_m[index] for index in range(3))  # type: ignore[return-value]


def _wall_box(
    primitive_id: str,
    room: RoomPlan,
    *,
    axis: str,
    coordinate: float,
    start: float,
    end: float,
    z0: float,
    z1: float,
) -> PrimitivePlan:
    thickness = 0.2
    if axis == "x":
        world_min, world_max = (coordinate - thickness / 2, start, z0), (coordinate + thickness / 2, end, z1)
    else:
        world_min, world_max = (start, coordinate - thickness / 2, z0), (end, coordinate + thickness / 2, z1)
    local_min, local_max = _local(room, world_min), _local(room, world_max)
    dimensions = tuple(local_max[index] - local_min[index] for index in range(3))
    location = tuple((local_min[index] + local_max[index]) / 2 for index in range(3))
    return box(primitive_id, dimensions, location, "wall_warm_white", grid_bounds_m=(world_min, world_max))


def _wall(
    room: RoomPlan,
    name: str,
    *,
    axis: str,
    coordinate: float,
    start: float,
    end: float,
    height: float,
    openings: Iterable[tuple[float, float, float]] = (),
) -> list[PrimitivePlan]:
    result: list[PrimitivePlan] = []
    cursor = start
    for index, (centre, width, clear_height) in enumerate(sorted(openings), start=1):
        opening_min, opening_max = centre - width / 2, centre + width / 2
        if opening_min < cursor or opening_max > end:
            raise HousePlanError(f"opening outside wall: {name}")
        if opening_min > cursor:
            result.append(_wall_box(f"{name}.segment.{index:02d}", room, axis=axis, coordinate=coordinate, start=cursor, end=opening_min, z0=0, z1=height))
        result.append(_wall_box(f"{name}.lintel.{index:02d}", room, axis=axis, coordinate=coordinate, start=opening_min, end=opening_max, z0=clear_height, z1=height))
        cursor = opening_max
    if cursor < end:
        result.append(_wall_box(f"{name}.segment.end", room, axis=axis, coordinate=coordinate, start=cursor, end=end, z0=0, z1=height))
    return result


def _bundle_nodes(rooms: tuple[RoomPlan, ...], house: dict[str, Any]) -> list[RenderNode]:
    by_kind = {room.kind: room for room in rooms}
    bundle_refs = {room["room_id"]: room["bundle_asset_ref"] for room in house["rooms"]}
    wall_parts: dict[str, list[PrimitivePlan]] = {kind: [] for kind in SUPPORTED_ROOMS}
    add = lambda kind, parts: wall_parts[kind].extend(parts)
    hall = by_kind["entry_hall"]
    add("entry_hall", _wall(hall, "hall.west", axis="x", coordinate=-1.5, start=-4, end=4, height=3, openings=((-2, 1, 2.1), (2, 1, 2.1))))
    add("entry_hall", _wall(hall, "hall.east", axis="x", coordinate=1.5, start=-4, end=4, height=3, openings=((-2, 1, 2.1), (2, 1, 2.1))))
    add("entry_hall", _wall(hall, "hall.south", axis="y", coordinate=-4, start=-1.5, end=1.5, height=3, openings=((0, 1, 2.1),)))
    add("entry_hall", _wall(hall, "hall.north", axis="y", coordinate=4, start=-1.5, end=1.5, height=3, openings=((0, 1, 2.1),)))

    peripheral = {
        "living_room": (("x", -6.5, -4, 0), ("y", -4, -6.5, -1.5), ("y", 0, -6.5, -1.5)),
        "kitchen_dining": (("x", 6.5, -4, 0), ("y", -4, 1.5, 6.5), ("y", 0, 1.5, 6.5)),
        "bedroom": (("x", -6.5, 0, 4), ("y", 0, -6.5, -1.5), ("y", 4, -6.5, -1.5)),
        "office": (("x", 6.5, 0, 4), ("y", 0, 1.5, 6.5), ("y", 4, 1.5, 6.5)),
        "bathroom_laundry": (("x", -1.5, 4, 8), ("x", 1.5, 4, 8), ("y", 8, -1.5, 1.5)),
    }
    for kind, specs in peripheral.items():
        room = by_kind[kind]
        for index, (axis, coordinate, start, end) in enumerate(specs, start=1):
            add(kind, _wall(room, f"{kind}.outer.{index:02d}", axis=axis, coordinate=coordinate, start=start, end=end, height=3))

    floor_material = {
        "entry_hall": "floor_hall_slate",
        "living_room": "floor_living_oak",
        "kitchen_dining": "floor_kitchen_terrazzo",
        "bedroom": "floor_bedroom_carpet",
        "office": "floor_office_cork",
        "bathroom_laundry": "floor_bathroom_tile",
    }
    nodes: list[RenderNode] = []
    for room in rooms:
        width = room.bounds_max_m[0] - room.bounds_min_m[0]
        depth = room.bounds_max_m[1] - room.bounds_min_m[1]
        floor = box(
            f"{room.kind}.floor",
            (width, depth, 0.1),
            ((room.bounds_min_m[0] + room.bounds_max_m[0]) / 2, (room.bounds_min_m[1] + room.bounds_max_m[1]) / 2, -0.05),
            floor_material[room.kind],
        )
        nodes.append(
            RenderNode(
                node_id=f"{room.room_id}/bundle.room_shell",
                room_id=room.room_id,
                category="room_shell",
                label=f"{room.label} room bundle",
                asset_ref=bundle_refs[room.room_id],
                transform_location_m=(0, 0, 0),
                transform_rotation_deg=(0, 0, 0),
                transform_scale=(1, 1, 1),
                component_role="anchor",
                mobility="static",
                collision_policy="world_static",
                nav_obstacle=True,
                affordances=(),
                initial_state={"visible": True},
                tags=("nonsemantic_bundle", "grid_10cm"),
                primitives=tuple([floor, *wall_parts[room.kind]]),
            )
        )
        nodes.append(
            RenderNode(
                node_id=f"{room.room_id}/bundle.ceiling",
                room_id=room.room_id,
                category="ceiling",
                label=f"{room.label} ceiling bundle",
                asset_ref=bundle_refs[room.room_id],
                transform_location_m=(0, 0, 0),
                transform_rotation_deg=(0, 0, 0),
                transform_scale=(1, 1, 1),
                component_role="anchor",
                mobility="static",
                collision_policy="world_static",
                nav_obstacle=False,
                affordances=(),
                initial_state={"visible": True},
                tags=("nonsemantic_bundle", "preview_hide"),
                primitives=(box(f"{room.kind}.ceiling", (width, depth, 0.1), (0, 0, 3.05), "ceiling_white"),),
            )
        )
    return nodes


def _geometry_recipes() -> dict[str, tuple[PrimitivePlan, ...]]:
    # Each hero asset is a recognizable multi-part local assembly.  Small
    # decorative parts are joined into that one semantic mesh at build time.
    recipes: dict[str, tuple[PrimitivePlan, ...]] = {
        "shoe_bench": (
            box("bench.seat", (1.25, 0.48, 0.14), (0, 0, 0.5), "fabric_ochre"),
            box("bench.frame", (1.1, 0.38, 0.34), (0, 0, 0.25), "wood_oak"),
            box("bench.shelf", (1.05, 0.36, 0.05), (0, 0, 0.14), "wood_walnut"),
        ),
        "sofa": (
            box("sofa.base", (0.75, 2.25, 0.38), (0, 0, 0.32), "fabric_navy"),
            box("sofa.back", (0.28, 2.25, 0.82), (-0.28, 0, 0.78), "fabric_navy"),
            box("sofa.arm.a", (0.7, 0.18, 0.62), (0.02, -1.03, 0.55), "fabric_navy"),
            box("sofa.arm.b", (0.7, 0.18, 0.62), (0.02, 1.03, 0.55), "fabric_navy"),
            box("sofa.cushion.a", (0.68, 0.62, 0.16), (0.08, -0.68, 0.57), "fabric_blue"),
            box("sofa.cushion.b", (0.68, 0.62, 0.16), (0.08, 0, 0.57), "fabric_blue"),
            box("sofa.cushion.c", (0.68, 0.62, 0.16), (0.08, 0.68, 0.57), "fabric_blue"),
        ),
        "coffee_table": (
            box("coffee.top", (1.2, 0.7, 0.08), (0, 0, 0.48), "wood_oak"),
            box("coffee.base", (0.62, 0.38, 0.42), (0, 0, 0.23), "wood_walnut"),
        ),
        "keys": (
            torus("keys.ring", 0.055, 0.009, (0, 0, 0), "metal_brass"),
            box("keys.stem.a", (0.11, 0.025, 0.012), (0.09, 0.025, 0), "metal_brass", rotation_deg=(0, 0, 18)),
            box("keys.stem.b", (0.1, 0.025, 0.012), (0.08, -0.045, 0), "metal_brushed", rotation_deg=(0, 0, -24)),
        ),
        "stove": (
            box("stove.body", (0.7, 0.62, 0.9), (0, 0, 0.45), "appliance_dark"),
            box("stove.top", (0.7, 0.62, 0.06), (0, 0, 0.93), "stove_black"),
            cylinder("stove.burner.a", 0.11, 0.025, (-0.18, -0.16, 0.97), "metal_dark"),
            cylinder("stove.burner.b", 0.11, 0.025, (0.18, -0.16, 0.97), "metal_dark"),
            cylinder("stove.burner.c", 0.11, 0.025, (-0.18, 0.16, 0.97), "metal_dark"),
            cylinder("stove.burner.d", 0.11, 0.025, (0.18, 0.16, 0.97), "metal_dark"),
        ),
        "dining_table": (
            box("table.top", (1.65, 0.95, 0.08), (0, 0, 0.78), "wood_oak"),
            box("table.leg.a", (0.09, 0.09, 0.74), (-0.68, -0.33, 0.37), "metal_dark"),
            box("table.leg.b", (0.09, 0.09, 0.74), (0.68, -0.33, 0.37), "metal_dark"),
            box("table.leg.c", (0.09, 0.09, 0.74), (-0.68, 0.33, 0.37), "metal_dark"),
            box("table.leg.d", (0.09, 0.09, 0.74), (0.68, 0.33, 0.37), "metal_dark"),
        ),
        "coffee_cup": (
            cylinder("cup.body", 0.055, 0.12, (0, 0, 0), "ceramic_cream"),
            torus("cup.handle", 0.038, 0.009, (0.065, 0, 0), "ceramic_cream", rotation_deg=(90, 0, 0)),
            cylinder("cup.coffee", 0.047, 0.005, (0, 0, 0.061), "coffee_dark"),
        ),
        "fridge": (
            box("fridge.body", (0.82, 0.72, 2.05), (0, 0, 1.025), "appliance_white"),
            box("fridge.divider", (0.68, 0.02, 0.025), (0, -0.37, 0.68), "metal_dark"),
            box("fridge.handle", (0.035, 0.04, 0.75), (0.28, -0.39, 1.35), "metal_brushed"),
        ),
        "bed": (
            box("bed.frame", (1.75, 2.1, 0.32), (0, 0, 0.3), "wood_walnut"),
            box("bed.mattress", (1.65, 2, 0.28), (0, 0, 0.57), "fabric_cream"),
            box("bed.duvet", (1.55, 1.45, 0.16), (0, -0.18, 0.77), "fabric_terracotta"),
            box("bed.headboard", (1.8, 0.16, 1.05), (0, 0.98, 0.73), "fabric_terracotta_dark"),
            box("bed.pillow.a", (0.65, 0.42, 0.15), (-0.4, 0.65, 0.82), "fabric_cream"),
            box("bed.pillow.b", (0.65, 0.42, 0.15), (0.4, 0.65, 0.82), "fabric_cream"),
        ),
        "nightstand": (
            box("nightstand.body", (0.62, 0.5, 0.58), (0, 0, 0.29), "wood_oak"),
            box("nightstand.drawer", (0.5, 0.02, 0.2), (0, -0.26, 0.4), "wood_walnut"),
        ),
        "phone": (
            box("phone.body", (0.075, 0.16, 0.012), (0, 0, 0), "metal_dark"),
            box("phone.screen", (0.064, 0.14, 0.004), (0, 0, 0.008), "screen_glass"),
            cylinder("phone.camera", 0.008, 0.004, (-0.022, 0.052, -0.008), "screen_black"),
        ),
        "desk": (
            box("desk.top", (1.8, 0.72, 0.08), (0, 0, 0.78), "wood_walnut"),
            box("desk.pedestal", (0.42, 0.62, 0.7), (-0.62, 0, 0.36), "paint_blue_gray"),
            box("desk.leg", (0.08, 0.58, 0.72), (0.72, 0, 0.36), "metal_dark"),
            box("monitor.screen", (0.92, 0.07, 0.53), (0.18, -0.03, 1.2), "screen_black"),
            cylinder("monitor.stand", 0.035, 0.28, (0.18, 0, 0.91), "metal_dark"),
            box("monitor.foot", (0.34, 0.22, 0.035), (0.18, 0, 0.82), "metal_dark"),
        ),
        "rolling_chair": (
            cylinder("chair.base", 0.34, 0.08, (0, 0, 0.12), "metal_dark"),
            cylinder("chair.column", 0.055, 0.42, (0, 0, 0.34), "metal_brushed"),
            box("chair.seat", (0.58, 0.58, 0.16), (0, 0, 0.58), "fabric_blue"),
            box("chair.back", (0.58, 0.16, 0.76), (0, 0.23, 0.95), "fabric_navy"),
            torus("chair.wheel_ring", 0.28, 0.025, (0, 0, 0.07), "rubber_dark"),
        ),
        "cabinet": (
            box("cabinet.body", (0.95, 0.52, 2.2), (0, 0, 1.1), "paint_blue_gray"),
            box("cabinet.door", (0.82, 0.04, 2.02), (0, -0.28, 1.1), "paint_blue_gray_light"),
            cylinder("cabinet.handle", 0.018, 0.42, (0.28, -0.32, 1.25), "metal_brushed"),
        ),
        "cardboard_box": (
            box("box.body", (0.48, 0.36, 0.32), (0, 0, 0), "cardboard"),
            box("box.lid", (0.5, 0.38, 0.045), (0, 0, 0.18), "cardboard_light"),
            box("box.label", (0.22, 0.01, 0.1), (0, -0.19, 0), "paper_label"),
        ),
        "bathtub": (
            box("tub.outer", (1.55, 0.78, 0.58), (0, 0, 0.29), "ceramic_white"),
            box("tub.basin", (1.25, 0.52, 0.48), (0, 0, 0.4), "water_blue"),
            cylinder("tub.faucet", 0.035, 0.34, (0.62, 0, 0.78), "metal_brushed"),
        ),
        "washer": (
            box("washer.body", (0.72, 0.7, 0.92), (0, 0, 0.46), "appliance_white"),
            cylinder("washer.door", 0.24, 0.06, (0, -0.37, 0.48), "screen_glass", rotation_deg=(90, 0, 0)),
            cylinder("washer.rim", 0.29, 0.035, (0, -0.4, 0.48), "metal_brushed", rotation_deg=(90, 0, 0)),
            box("washer.panel", (0.58, 0.04, 0.13), (0, -0.38, 0.8), "appliance_dark"),
        ),
    }
    door = (
        box("door.slab", (1.0, 0.06, 2.05), (0.0, 0.0, 1.025), "door_blue_gray"),
        cylinder("door.handle.front", 0.035, 0.12, (0.28, -0.09, 1.0), "metal_brass", rotation_deg=(90, 0, 0)),
        cylinder("door.handle.back", 0.035, 0.12, (0.28, 0.09, 1.0), "metal_brass", rotation_deg=(90, 0, 0)),
    )
    recipes["interior_door"] = door
    recipes["exit_door"] = (
        box("exit.slab", (1.0, 0.08, 2.1), (0, 0, 1.05), "wood_walnut"),
        box("exit.inset", (0.72, 0.025, 1.7), (0, -0.055, 1.05), "door_blue_gray"),
        cylinder("exit.handle", 0.04, 0.14, (0.3, -0.11, 1.0), "metal_brass", rotation_deg=(90, 0, 0)),
    )
    recipes["slipper"] = (
        box("slipper.sole", (0.13, 0.3, 0.035), (0, 0, 0.025), "fabric_terracotta"),
        torus("slipper.strap", 0.07, 0.018, (0, -0.02, 0.07), "fabric_terracotta_dark", rotation_deg=(70, 0, 0)),
    )
    recipes["spill_marker"] = (cylinder("spill.disc", 0.5, 0.012, (0, 0, 0), "coffee_dark"),)
    recipes["fire_marker"] = (
        sphere("fire.core", 0.16, (0, 0, 0.16), "fabric_ochre"),
        sphere("fire.tip", 0.1, (0, 0, 0.34), "fabric_terracotta"),
    )
    recipes["pot"] = (
        cylinder("pot.body", 0.22, 0.28, (0, 0, 0.14), "metal_brushed"),
        cylinder("pot.lid", 0.23, 0.025, (0, 0, 0.3), "metal_dark"),
        torus("pot.handle", 0.09, 0.018, (0.27, 0, 0.2), "metal_dark", rotation_deg=(90, 0, 0)),
    )
    recipes["backpack"] = (
        box("backpack.body", (0.38, 0.2, 0.5), (0, 0, 0.27), "fabric_navy"),
        torus("backpack.strap.a", 0.13, 0.025, (-0.11, 0.11, 0.28), "fabric_blue", rotation_deg=(90, 0, 0)),
        torus("backpack.strap.b", 0.13, 0.025, (0.11, 0.11, 0.28), "fabric_blue", rotation_deg=(90, 0, 0)),
    )
    ladder_parts: list[PrimitivePlan] = [
        box("ladder.rail.a", (0.07, 0.07, 1.9), (-0.28, 0, 0.95), "metal_brushed", rotation_deg=(0, -8, 0)),
        box("ladder.rail.b", (0.07, 0.07, 1.9), (0.28, 0, 0.95), "metal_brushed", rotation_deg=(0, 8, 0)),
    ]
    ladder_parts.extend(box(f"ladder.step.{index:02d}", (0.52, 0.14, 0.045), (0, 0, 0.25 + index * 0.28), "metal_dark") for index in range(6))
    recipes["ladder"] = tuple(ladder_parts)
    recipes["faucet"] = (
        cylinder("faucet.base", 0.05, 0.18, (0, 0, 0.09), "metal_brushed"),
        torus("faucet.spout", 0.12, 0.025, (0, 0, 0.23), "metal_brushed", rotation_deg=(90, 0, 0)),
    )
    recipes["laundry_basket"] = (
        box("basket.body", (0.5, 0.42, 0.6), (0, 0, 0.3), "fabric_cream"),
        torus("basket.rim", 0.23, 0.025, (0, 0, 0.61), "wood_oak"),
    )
    recipes["clothes"] = (
        sphere("clothes.bundle.a", 0.18, (-0.08, 0, 0), "fabric_blue"),
        sphere("clothes.bundle.b", 0.16, (0.1, 0.03, 0.03), "fabric_terracotta"),
    )
    recipes["overflow_marker"] = (cylinder("overflow.disc", 0.72, 0.012, (0, 0, 0), "water_blue"),)
    recipes["resident"] = ()  # Runtime pawn marker; Unreal supplies the humanoid.
    return recipes


def build_contract_plan(house: dict[str, Any]) -> ContractScenePlan:
    validate_house(house)
    rooms = _room_plans(house)
    recipes = _geometry_recipes()
    nodes = _bundle_nodes(rooms, house)
    for entity in house["entities"]:
        category = entity["category"]
        if category not in recipes:
            raise HousePlanError(f"no deterministic geometry recipe for category: {category}")
        transform = entity["transform"]
        instance_group = "interior_door_r1" if category == "interior_door" else None
        nodes.append(
            RenderNode(
                node_id=entity["entity_id"],
                semantic_entity_id=entity["entity_id"],
                room_id=entity["room_id"],
                category=category,
                label=category.replace("_", " ").title(),
                asset_ref=entity["asset_ref"],
                transform_location_m=_v3(transform["location_m"], f"{entity['entity_id']}.transform.location_m"),
                transform_rotation_deg=_v3(transform["rotation_deg"], f"{entity['entity_id']}.transform.rotation_deg"),
                transform_scale=_v3(transform["scale"], f"{entity['entity_id']}.transform.scale"),
                component_role=entity["component_role"],
                mobility=entity["mobility"],
                collision_policy=entity["collision_policy"],
                nav_obstacle=bool(entity["nav_obstacle"]),
                affordances=tuple(entity["affordances"]),
                initial_state=copy.deepcopy(entity["initial_state"]),
                tags=tuple(entity["tags"]),
                primitives=recipes[category],
                instance_group=instance_group,
            )
        )
    plan = ContractScenePlan(
        house=copy.deepcopy(house),
        rooms=rooms,
        nodes=tuple(sorted(nodes, key=lambda item: item.node_id)),
        materials=_materials(),
        preview_views=(
            PreviewView("overview", (15.0, -16.0, 17.0), (0.0, 2.0, 0.7), 48.0),
            PreviewView("interior", (0.0, -11.0, 7.2), (0.0, 1.5, 1.0), 45.0),
        ),
    )
    validate_contract_plan(plan)
    return plan


def compose_world_transform(house: dict[str, Any], entity: dict[str, Any]) -> dict[str, list[float]]:
    """Compose one room-local HouseSpec transform into world metres.

    Blender performs the same composition through parented room roots.  This
    pure helper makes that contract testable without importing ``bpy``.
    """

    room = next((item for item in house["rooms"] if item["room_id"] == entity["room_id"]), None)
    if room is None:
        raise HousePlanError(f"entity references unknown room: {entity.get('entity_id')}")
    parent, local = room["transform"], entity["transform"]
    parent_location = _v3(parent["location_m"], "room.transform.location_m")
    parent_rotation = _v3(parent["rotation_deg"], "room.transform.rotation_deg")
    parent_scale = _v3(parent["scale"], "room.transform.scale")
    local_location = _v3(local["location_m"], "entity.transform.location_m")
    local_rotation = _v3(local["rotation_deg"], "entity.transform.rotation_deg")
    local_scale = _v3(local["scale"], "entity.transform.scale")
    x, y, z = (local_location[index] * parent_scale[index] for index in range(3))
    rx, ry, rz = (math.radians(value) for value in parent_rotation)
    # Blender XYZ Euler order: rotate local vector around X, then Y, then Z.
    y, z = y * math.cos(rx) - z * math.sin(rx), y * math.sin(rx) + z * math.cos(rx)
    x, z = x * math.cos(ry) + z * math.sin(ry), -x * math.sin(ry) + z * math.cos(ry)
    x, y = x * math.cos(rz) - y * math.sin(rz), x * math.sin(rz) + y * math.cos(rz)
    clean = lambda value: 0.0 if math.isclose(value, 0.0, abs_tol=1e-10) else round(value, 6)
    return {
        "location_m": [clean(parent_location[0] + x), clean(parent_location[1] + y), clean(parent_location[2] + z)],
        "rotation_deg": [clean(parent_rotation[index] + local_rotation[index]) for index in range(3)],
        "scale": [clean(parent_scale[index] * local_scale[index]) for index in range(3)],
    }


def asset_binding_plan(plan: ContractScenePlan) -> dict[str, tuple[str, ...]]:
    """Map every non-builtin asset to the local mesh nodes exported for it."""

    source_kind = {item["asset_id"]: item["source_kind"] for item in plan.house["asset_catalog"]}
    nodes_by_asset: dict[str, list[RenderNode]] = {}
    for node in plan.nodes:
        nodes_by_asset.setdefault(node.asset_ref, []).append(node)
    unknown = set(nodes_by_asset) - set(source_kind)
    if unknown:
        raise HousePlanError(f"render nodes reference unknown assets: {sorted(unknown)}")
    result: dict[str, tuple[str, ...]] = {}
    for asset_id, kind in sorted(source_kind.items()):
        if kind == "builtin":
            continue
        candidates = sorted(nodes_by_asset.get(asset_id, []), key=lambda item: item.node_id)
        if not candidates:
            raise HousePlanError(f"non-builtin asset has no render node: {asset_id}")
        if any("nonsemantic_bundle" in node.tags for node in candidates):
            representatives = candidates
        else:
            signatures = {canonical_json_bytes([asdict(primitive) for primitive in node.primitives]) for node in candidates}
            if len(signatures) != 1:
                raise HousePlanError(f"shared asset_ref has divergent geometry: {asset_id}")
            representatives = candidates[:1]
        if not any(node.primitives for node in representatives):
            raise HousePlanError(f"non-builtin asset has no mesh recipe: {asset_id}")
        result[asset_id] = tuple(node.node_id for node in representatives)
    return result


def validate_contract_plan(plan: ContractScenePlan) -> None:
    validate_house(plan.house)
    semantic_nodes = {node.semantic_entity_id for node in plan.nodes if node.semantic_entity_id is not None}
    contract_entities = {entity["entity_id"] for entity in plan.house["entities"]}
    if semantic_nodes != contract_entities:
        raise HousePlanError("render plan semantic nodes must exactly match HouseSpec entities")
    if len([node for node in plan.nodes if "nonsemantic_bundle" in node.tags and node.category == "room_shell"]) != 6:
        raise HousePlanError("render plan requires one nonsemantic shell bundle per room")
    material_ids = {material.material_id for material in plan.materials}
    for node in plan.nodes:
        if any(primitive.material_id not in material_ids for primitive in node.primitives):
            raise HousePlanError(f"node references an unknown material: {node.node_id}")
        if node.component_role == "decoration" and (node.collision_policy != "detail_no_collision" or node.nav_obstacle):
            raise HousePlanError(f"decorative detail may not block: {node.node_id}")
    room_by_id = {room.room_id: room for room in plan.rooms}
    for node in plan.nodes:
        if node.room_id not in room_by_id:
            raise HousePlanError(f"render node references unknown room: {node.node_id}")
    asset_binding_plan(plan)


def normalized_manifest(plan: ContractScenePlan) -> dict[str, Any]:
    """Return a deterministic manifest with exact HouseSpec semantics."""

    validate_contract_plan(plan)
    node_by_semantic = {node.semantic_entity_id: node for node in plan.nodes if node.semantic_entity_id is not None}
    bundle_nodes = [node for node in plan.nodes if node.semantic_entity_id is None]
    entities: list[dict[str, Any]] = []
    for source in plan.house["entities"]:
        node = node_by_semantic[source["entity_id"]]
        entry = copy.deepcopy(source)
        entry["blender_node_id"] = node.node_id
        entry["world_transform"] = compose_world_transform(plan.house, source)
        entry["geometry"] = {
            "assembly_policy": "runtime_actor" if not node.primitives else "single_semantic_mesh",
            "primitive_count": len(node.primitives),
            "primitives": [asdict(primitive) for primitive in node.primitives],
            "instance_group": node.instance_group,
        }
        entities.append(entry)
    body: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA,
        "house_id": plan.house["house_id"],
        "revision": plan.house["revision"],
        "seed": plan.house["seed"],
        "units": "meters",
        "coordinate_system": copy.deepcopy(plan.house["coordinate_system"]),
        "grid_m": GRID_M,
        "source_house": {"schema_version": plan.house["schema_version"], "content_digest": plan.house["content_digest"]},
        "rooms": copy.deepcopy(plan.house["rooms"]),
        "portals": copy.deepcopy(plan.house["portals"]),
        "entities": entities,
        "room_bundles": [
            {
                "node_id": node.node_id,
                "room_id": node.room_id,
                "category": node.category,
                "asset_ref": node.asset_ref,
                "component_role": node.component_role,
                "collision_policy": node.collision_policy,
                "nav_obstacle": node.nav_obstacle,
                "geometry": {"assembly_policy": "single_room_mesh", "primitive_count": len(node.primitives), "primitives": [asdict(primitive) for primitive in node.primitives]},
            }
            for node in sorted(bundle_nodes, key=lambda item: item.node_id)
        ],
        "materials": [asdict(material) for material in plan.materials],
        "preview_views": [asdict(view) for view in plan.preview_views],
        "geometry_summary": {
            "semantic_node_count": len(entities),
            "room_bundle_node_count": len(bundle_nodes),
            "mesh_node_count": sum(bool(node.primitives) for node in plan.nodes),
            "primitive_count": sum(len(node.primitives) for node in plan.nodes),
            "door_count": sum(node.component_role == "door" for node in plan.nodes),
            "portable_count": sum(node.component_role == "pickup" for node in plan.nodes),
        },
        "generator": {"path": "tools/blender/vista_playable_home", "version": "1"},
    }
    body["content_digest"] = hashlib.sha256(canonical_json_bytes(body)).hexdigest()
    return body


def write_normalized_manifest(plan: ContractScenePlan, path: pathlib.Path) -> dict[str, Any]:
    manifest = normalized_manifest(plan)
    path.write_bytes(canonical_json_bytes(manifest))
    path.chmod(0o600)
    return manifest
