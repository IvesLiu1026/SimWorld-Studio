"""Pure, deterministic build-plan to Unreal operation compiler.

This module deliberately has no ``unreal`` import.  It is the reviewable and
unit-testable boundary between the closed world compiler contract and the UE
Editor commandlets.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import re
from collections import deque
from collections.abc import Mapping, Sequence
from typing import Any


BUILD_PLAN_SCHEMA = "simworld.vista.playable-home-build-plan/v1"
COMPOSITION_SPEC_SCHEMA = "simworld.vista.playable-home-ue-composition/v1"
CONTENT_ROOT = "/Game/VISTA/PlayableHome/"
TAG_PREFIX = "VistaSemanticId="
PLAYABLE_CAPSULE_HALF_HEIGHT_CM = 96.0
EXPECTED_COMPOSITION_ORDER = [
    "verify_inputs",
    "import_assets",
    "place_rooms",
    "place_entities",
    "configure_gameplay",
    "build_navigation",
    "save_reload_verify",
]
TOP_LEVEL_KEYS = {
    "schema_version",
    "plan_id",
    "house",
    "units",
    "assets",
    "rooms",
    "portals",
    "entities",
    "relations",
    "runtime_profile",
    "event_plans",
    "unreal",
    "provenance",
    "content_digest",
}
COLLISION_SETTINGS = {
    "world_static": {"profile": "BlockAll", "simulate_physics": False, "generate_overlap": False},
    "furniture": {"profile": "BlockAll", "simulate_physics": False, "generate_overlap": False},
    "detail_no_collision": {"profile": "NoCollision", "simulate_physics": False, "generate_overlap": False},
    "pickup_physics": {"profile": "PhysicsActor", "simulate_physics": True, "generate_overlap": True},
    "door_dynamic": {"profile": "BlockAllDynamic", "simulate_physics": False, "generate_overlap": True},
    "pawn": {"profile": "Pawn", "simulate_physics": False, "generate_overlap": True},
    "trigger_only": {"profile": "Trigger", "simulate_physics": False, "generate_overlap": True},
}
ROLE_CLASSES = {
    "static_furniture": "/Script/VistaPlayableHome.VistaSemanticPropActor",
    "decoration": "/Script/VistaPlayableHome.VistaSemanticPropActor",
    "pickup": "/Script/VistaPlayableHome.VistaPickupActor",
    "door": "/Script/VistaPlayableHome.VistaDoorActor",
    "container": "/Script/VistaPlayableHome.VistaContainerActor",
    "appliance": "/Script/VistaPlayableHome.VistaStatefulApplianceActor",
    "npc": "/Script/VistaPlayableHome.VistaHomeNpcCharacter",
    "hazard": "/Script/VistaPlayableHome.VistaSemanticPropActor",
    "anchor": "/Script/Engine.TargetPoint",
}
ROLE_COLLISIONS = {
    "static_furniture": {"world_static", "furniture"},
    "decoration": {"detail_no_collision", "furniture"},
    "pickup": {"pickup_physics"},
    "door": {"door_dynamic"},
    "container": {"furniture", "door_dynamic"},
    "appliance": {"furniture", "door_dynamic", "detail_no_collision"},
    "npc": {"pawn"},
    "hazard": {"detail_no_collision", "trigger_only"},
    "anchor": {"detail_no_collision", "trigger_only"},
}
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@=-]{0,223}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class VistaPlayableHomePlanError(ValueError):
    """Closed-contract validation failure with a stable public code."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclasses.dataclass(frozen=True)
class CompositionSpec:
    value: dict[str, Any]
    raw: bytes
    sha256: str


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def _fail(code: str, detail: str) -> None:
    raise VistaPlayableHomePlanError(code, detail)


def _require(condition: bool, code: str, detail: str) -> None:
    if not condition:
        _fail(code, detail)


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    _require(isinstance(value, Mapping), "VISTA_HOME_PLAN_SHAPE_INVALID", f"{label} must be an object")
    return value


def _array(value: Any, label: str) -> Sequence[Any]:
    _require(isinstance(value, list), "VISTA_HOME_PLAN_SHAPE_INVALID", f"{label} must be an array")
    return value


def _safe_id(value: Any, label: str) -> str:
    _require(isinstance(value, str) and SAFE_ID.fullmatch(value) is not None,
             "VISTA_HOME_PLAN_ID_INVALID", f"{label} is invalid")
    return value


def _sha(value: Any, label: str) -> str:
    _require(isinstance(value, str) and SHA256.fullmatch(value) is not None,
             "VISTA_HOME_PLAN_DIGEST_INVALID", f"{label} is invalid")
    return value


def _transform(value: Any, label: str) -> dict[str, list[float]]:
    item = _mapping(value, label)
    _require(set(item) == {"location_cm", "rotation_deg", "scale"},
             "VISTA_HOME_PLAN_TRANSFORM_INVALID", f"{label} fields differ")
    result: dict[str, list[float]] = {}
    for key in ("location_cm", "rotation_deg", "scale"):
        vector = item[key]
        _require(isinstance(vector, list) and len(vector) == 3 and
                 all(isinstance(number, (int, float)) and not isinstance(number, bool) and
                     math.isfinite(number) for number in vector),
                 "VISTA_HOME_PLAN_TRANSFORM_INVALID", f"{label}.{key} invalid")
        result[key] = [float(number) for number in vector]
    _require(all(number > 0 for number in result["scale"]),
             "VISTA_HOME_PLAN_TRANSFORM_INVALID", f"{label}.scale must be positive")
    return result


def _bounds(value: Any, label: str) -> dict[str, list[float]]:
    item = _mapping(value, label)
    _require(set(item) == {"min_cm", "max_cm"}, "VISTA_HOME_PLAN_BOUNDS_INVALID", f"{label} fields differ")
    output: dict[str, list[float]] = {}
    for key in ("min_cm", "max_cm"):
        vector = item[key]
        _require(isinstance(vector, list) and len(vector) == 3 and
                 all(isinstance(number, (int, float)) and not isinstance(number, bool) and
                     math.isfinite(number) for number in vector),
                 "VISTA_HOME_PLAN_BOUNDS_INVALID", f"{label}.{key} invalid")
        output[key] = [float(number) for number in vector]
    _require(all(low < high for low, high in zip(output["min_cm"], output["max_cm"], strict=True)),
             "VISTA_HOME_PLAN_BOUNDS_INVALID", f"{label} has non-positive extent")
    return output


def _operation(phase: str, kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    base = {"phase": phase, "kind": kind, **payload}
    operation_id = "ueop-" + hashlib.sha256(canonical_json(base)).hexdigest()[:24]
    return {"operation_id": operation_id, **base}


def _binding(binding: Any, assets: Mapping[str, Mapping[str, Any]], label: str) -> dict[str, Any]:
    value = dict(_mapping(binding, label))
    required = {"asset_id", "source_kind", "uri", "source_digest", "license"}
    _require(set(value) == required, "VISTA_HOME_PLAN_ASSET_INVALID", f"{label} fields differ")
    asset_id = _safe_id(value["asset_id"], f"{label}.asset_id")
    _sha(value["source_digest"], f"{label}.source_digest")
    _require(asset_id in assets and dict(assets[asset_id]) == value,
             "VISTA_HOME_PLAN_ASSET_INVALID", f"{label} is not the declared binding")
    return value


def _validate_graph(room_ids: set[str], portals: Sequence[Mapping[str, Any]]) -> None:
    adjacency = {room_id: set() for room_id in room_ids}
    for portal in portals:
        first = portal.get("from_room_id")
        second = portal.get("to_room_id")
        _require(first in room_ids and second in room_ids and first != second,
                 "VISTA_HOME_PLAN_PORTAL_INVALID", "portal endpoints invalid")
        if portal.get("nav_policy") != "blocked":
            adjacency[first].add(second)
            adjacency[second].add(first)
    visited: set[str] = set()
    queue = deque([next(iter(room_ids))])
    while queue:
        current = queue.popleft()
        if current in visited:
            continue
        visited.add(current)
        queue.extend(sorted(adjacency[current] - visited))
    _require(visited == room_ids, "VISTA_HOME_PLAN_GRAPH_DISCONNECTED", "navigable room graph is disconnected")


def build_composition_spec(plan: Mapping[str, Any]) -> CompositionSpec:
    """Validate critical invariants and compile stable Editor operations."""

    value = dict(_mapping(plan, "build plan"))
    _require(set(value) == TOP_LEVEL_KEYS, "VISTA_HOME_PLAN_SHAPE_INVALID", "top-level fields differ")
    _require(value["schema_version"] == BUILD_PLAN_SCHEMA, "VISTA_HOME_PLAN_SCHEMA_MISMATCH", "schema mismatch")
    _require(value["units"] == "centimeters", "VISTA_HOME_PLAN_UNITS_INVALID", "units must be centimeters")
    plan_digest = _sha(value["content_digest"], "content_digest")
    house = _mapping(value["house"], "house")
    _sha(house.get("content_digest"), "house.content_digest")

    declared_assets: dict[str, Mapping[str, Any]] = {}
    for index, raw_asset in enumerate(_array(value["assets"], "assets")):
        asset = dict(_mapping(raw_asset, f"assets[{index}]"))
        asset_id = _safe_id(asset.get("asset_id"), f"assets[{index}].asset_id")
        _require(asset_id not in declared_assets, "VISTA_HOME_PLAN_DUPLICATE_ID", f"duplicate asset {asset_id}")
        declared_assets[asset_id] = asset
    for asset_id, asset in declared_assets.items():
        _binding(asset, declared_assets, f"asset {asset_id}")

    unreal_plan = _mapping(value["unreal"], "unreal")
    namespace = unreal_plan.get("content_namespace")
    map_path = unreal_plan.get("map_path")
    _require(isinstance(namespace, str) and namespace.startswith(CONTENT_ROOT) and
             namespace.count("/") == 4 and ".." not in namespace,
             "VISTA_HOME_PLAN_NAMESPACE_INVALID", "content namespace is not a fresh revision root")
    _require(map_path == namespace + "/Maps/VistaPlayableHome",
             "VISTA_HOME_PLAN_NAMESPACE_INVALID", "map escaped revision namespace")
    _require(unreal_plan.get("stable_tag_prefix") == TAG_PREFIX,
             "VISTA_HOME_PLAN_TAG_POLICY_INVALID", "stable tag prefix mismatch")
    _require(unreal_plan.get("composition_order") == EXPECTED_COMPOSITION_ORDER,
             "VISTA_HOME_PLAN_ORDER_INVALID", "composition order mismatch")
    nav_bounds = _bounds(unreal_plan.get("navigation_bounds_cm"), "unreal.navigation_bounds_cm")

    operations: list[dict[str, Any]] = []
    for asset_id in sorted(declared_assets):
        operations.append(_operation("import_assets", "bind_asset", {"asset": dict(declared_assets[asset_id])}))

    rooms_by_id: dict[str, Mapping[str, Any]] = {}
    room_values = _array(value["rooms"], "rooms")
    for index, raw_room in enumerate(room_values):
        room = _mapping(raw_room, f"rooms[{index}]")
        room_id = _safe_id(room.get("room_id"), f"rooms[{index}].room_id")
        _require(room_id not in rooms_by_id, "VISTA_HOME_PLAN_DUPLICATE_ID", f"duplicate room {room_id}")
        rooms_by_id[room_id] = room
    for room_id in sorted(rooms_by_id):
        room = rooms_by_id[room_id]
        operations.append(_operation("place_rooms", "place_room_bundle", {
            "semantic_id": room_id,
            "asset": _binding(room.get("bundle"), declared_assets, f"room {room_id}.bundle"),
            "transform": _transform(room.get("world_transform_cm"), f"room {room_id}.transform"),
            "bounds": _bounds(room.get("world_bounds_cm"), f"room {room_id}.bounds"),
            "tags": [TAG_PREFIX + room_id, "VistaRole=room"],
        }))
        anchor = room.get("anchor_world_cm")
        _require(isinstance(anchor, list) and len(anchor) == 3 and all(math.isfinite(v) for v in anchor),
                 "VISTA_HOME_PLAN_TRANSFORM_INVALID", f"room {room_id} anchor invalid")
        operations.append(_operation("place_rooms", "place_room_anchor", {
            "semantic_id": room_id + "/anchor.room_center",
            "location_cm": [float(v) for v in anchor],
            "tags": [TAG_PREFIX + room_id + "/anchor.room_center", "VistaRoom=" + room_id],
        }))
        for camera in sorted(_array(room.get("review_cameras"), f"room {room_id}.review_cameras"),
                             key=lambda item: item["camera_id"]):
            operations.append(_operation("place_rooms", "place_review_camera", {
                "semantic_id": room_id + "/camera." + _safe_id(camera.get("camera_id"), "camera_id"),
                "transform": _transform(camera.get("world_transform_cm"), "camera transform"),
                "fov_deg": float(camera.get("fov_deg")),
                "tags": [TAG_PREFIX + room_id + "/camera." + camera["camera_id"], "VistaRoom=" + room_id],
            }))

    room_ids = set(rooms_by_id)
    portals: list[Mapping[str, Any]] = []
    portal_ids: set[str] = set()
    for index, raw_portal in enumerate(_array(value["portals"], "portals")):
        portal = _mapping(raw_portal, f"portals[{index}]")
        portal_id = _safe_id(portal.get("portal_id"), f"portals[{index}].portal_id")
        _require(portal_id not in portal_ids, "VISTA_HOME_PLAN_DUPLICATE_ID", f"duplicate portal {portal_id}")
        portal_ids.add(portal_id)
        portals.append(portal)
    _validate_graph(room_ids, portals)
    _require(set(unreal_plan.get("room_graph_portal_ids", [])) == portal_ids,
             "VISTA_HOME_PLAN_PORTAL_INVALID", "room graph portal IDs differ")
    for portal in sorted(portals, key=lambda item: item["portal_id"]):
        operations.append(_operation("place_rooms", "place_portal_anchor", {
            "semantic_id": portal["portal_id"],
            "from_room_id": portal["from_room_id"],
            "to_room_id": portal["to_room_id"],
            "door_entity_id": portal["door_entity_id"],
            "initial_state": portal["initial_state"],
            "nav_policy": portal["nav_policy"],
            "transform": _transform(portal["world_transform_cm"], f"portal {portal['portal_id']}.transform"),
            "clearance_cm": dict(portal["clearance_cm"]),
            "tags": [TAG_PREFIX + portal["portal_id"], "VistaRole=portal"],
        }))

    entities_by_id: dict[str, Mapping[str, Any]] = {}
    for index, raw_entity in enumerate(_array(value["entities"], "entities")):
        entity = _mapping(raw_entity, f"entities[{index}]")
        entity_id = _safe_id(entity.get("entity_id"), f"entities[{index}].entity_id")
        _require(entity_id not in entities_by_id, "VISTA_HOME_PLAN_DUPLICATE_ID", f"duplicate entity {entity_id}")
        _require(entity.get("room_id") in room_ids, "VISTA_HOME_PLAN_ENTITY_INVALID", f"entity {entity_id} room absent")
        entities_by_id[entity_id] = entity

    runtime = _mapping(value["runtime_profile"], "runtime_profile")
    navigation_agent = _mapping(runtime.get("navigation_agent"),
                                "runtime_profile.navigation_agent")
    capsule_height_cm = float(navigation_agent.get("height_cm"))
    _require(math.isfinite(capsule_height_cm) and 40.0 <= capsule_height_cm <= 300.0,
             "VISTA_HOME_PLAN_RUNTIME_INVALID", "navigation capsule height is invalid")
    # Both native playable-home character classes use a 96 cm capsule half
    # height.  Preserve the plan's floor-contact Z and lift by the larger of
    # its declared nav agent or the concrete runtime capsule.
    capsule_half_height_cm = max(capsule_height_cm / 2.0,
                                 PLAYABLE_CAPSULE_HALF_HEIGHT_CM)
    npc_profiles_by_entity: dict[str, dict[str, Any]] = {}
    for raw_profile in _array(runtime.get("npc_profiles"), "runtime_profile.npc_profiles"):
        profile = _mapping(raw_profile, "npc profile")
        entity_id = _safe_id(profile.get("entity_id"), "npc profile entity_id")
        entity = entities_by_id.get(entity_id)
        _require(entity is not None and entity.get("component_role") == "npc" and
                 entity_id not in npc_profiles_by_entity,
                 "VISTA_HOME_PLAN_RUNTIME_INVALID", "NPC profile entity invalid")
        patrol_rooms = list(profile.get("patrol_room_ids", []))
        _require(patrol_rooms and all(room_id in room_ids for room_id in patrol_rooms),
                 "VISTA_HOME_PLAN_RUNTIME_INVALID", "NPC patrol room invalid")
        timeout = float(profile.get("action_timeout_s"))
        _require(math.isfinite(timeout) and 0.0 <= timeout <= 300.0,
                 "VISTA_HOME_PLAN_RUNTIME_INVALID", "NPC action timeout invalid")
        npc_profiles_by_entity[entity_id] = {
            "npc_id": _safe_id(profile.get("npc_id"), "npc profile npc_id"),
            "home_room_id": profile.get("home_room_id"),
            "patrol_target_semantic_ids": [
                room_id + "/anchor.room_center" for room_id in patrol_rooms
            ],
            "action_timeout_s": timeout,
        }
    for entity_id in sorted(entities_by_id):
        entity = entities_by_id[entity_id]
        role = entity.get("component_role")
        collision = entity.get("collision_policy")
        _require(role in ROLE_CLASSES and collision in COLLISION_SETTINGS,
                 "VISTA_HOME_PLAN_ROLE_INVALID", f"entity {entity_id} role/collision unknown")
        _require(collision in ROLE_COLLISIONS[role], "VISTA_HOME_PLAN_ROLE_INVALID",
                 f"entity {entity_id} role/collision incompatible")
        affordances = list(entity.get("affordances", []))
        generic_affordances = ({"inspect", "sit"} if role == "static_furniture"
                               else {"inspect"})
        _require(role not in {"static_furniture", "decoration", "hazard", "anchor"} or
                 set(affordances).issubset(generic_affordances),
                 "VISTA_HOME_PLAN_AFFORDANCE_INVALID",
                 f"entity {entity_id} needs a typed gameplay actor")
        tags = [TAG_PREFIX + entity_id, "VistaRoom=" + entity["room_id"], "VistaRole=" + role]
        tags.extend("VistaTag=" + tag for tag in sorted(entity.get("tags", [])))
        entity_transform = _transform(entity.get("world_transform_cm"),
                                      f"entity {entity_id}.transform")
        if role == "npc":
            entity_transform["location_cm"][2] += capsule_half_height_cm
            _require(entity_id in npc_profiles_by_entity,
                     "VISTA_HOME_PLAN_RUNTIME_INVALID", "NPC is missing its patrol profile")
        entity_operation = {
            "semantic_id": entity_id,
            "room_id": entity["room_id"],
            "category": entity.get("category"),
            "actor_class": ROLE_CLASSES[role],
            "asset": _binding(entity.get("asset"), declared_assets, f"entity {entity_id}.asset"),
            "transform": entity_transform,
            "component_role": role,
            "mobility": entity.get("mobility"),
            "collision_policy": collision,
            "collision": COLLISION_SETTINGS[collision],
            "nav_obstacle": entity.get("nav_obstacle"),
            "affordances": affordances,
            "baseline_state": dict(entity.get("baseline_state", {})),
            "tags": tags,
        }
        if role == "npc":
            entity_operation["floor_contact_offset_cm"] = capsule_half_height_cm
            entity_operation["npc_profile"] = npc_profiles_by_entity[entity_id]
        operations.append(_operation("place_entities", "place_entity", entity_operation))
        for anchor in sorted(entity.get("placement_anchors", []), key=lambda item: item["anchor_id"]):
            anchor_id = _safe_id(anchor.get("anchor_id"), "placement anchor")
            operations.append(_operation("place_entities", "place_placement_anchor", {
                "semantic_id": entity_id + "/anchor." + anchor_id,
                "owner_entity_id": entity_id,
                "transform": _transform(anchor.get("world_transform_cm"), "placement anchor transform"),
                "tags": [TAG_PREFIX + entity_id + "/anchor." + anchor_id, "VistaOwner=" + entity_id],
            }))

    player_start = _mapping(runtime.get("player_start"), "runtime_profile.player_start")
    _require(player_start.get("room_id") in room_ids, "VISTA_HOME_PLAN_RUNTIME_INVALID", "PlayerStart room absent")
    pawn_binding = _binding(runtime.get("pawn"), declared_assets, "runtime pawn")
    game_mode_binding = _binding(runtime.get("game_mode"), declared_assets, "runtime game mode")
    player_transform = _transform(player_start.get("world_transform_cm"), "player start transform")
    player_transform["location_cm"][2] += capsule_half_height_cm
    indoor_lights = []
    for room_id in sorted(rooms_by_id):
        room = rooms_by_id[room_id]
        bounds = _bounds(room.get("world_bounds_cm"), f"room {room_id}.bounds")
        anchor = room["anchor_world_cm"]
        xy_span = max(bounds["max_cm"][0] - bounds["min_cm"][0],
                      bounds["max_cm"][1] - bounds["min_cm"][1])
        indoor_lights.append({
            "semantic_id": room_id + "/light.ceiling",
            "room_id": room_id,
            "location_cm": [float(anchor[0]), float(anchor[1]),
                            float(bounds["max_cm"][2]) - 45.0],
            "attenuation_radius_cm": max(350.0, float(xy_span) * 0.8),
            "tags": [TAG_PREFIX + room_id + "/light.ceiling", "VistaRoom=" + room_id],
        })
    operations.extend([
        _operation("configure_gameplay", "place_player_start", {
            "semantic_id": "home.r1/player_start.01",
            "room_id": player_start["room_id"],
            "transform": player_transform,
            "floor_contact_offset_cm": capsule_half_height_cm,
            "tags": [TAG_PREFIX + "home.r1/player_start.01", "VistaRoom=" + player_start["room_id"]],
        }),
        _operation("configure_gameplay", "configure_game_mode", {
            "world_revision": house.get("revision"),
            "pawn": pawn_binding,
            "game_mode": game_mode_binding,
            "interaction_distance_cm": float(runtime.get("interaction_distance_cm")),
            "event_plans": list(value["event_plans"]),
        }),
        _operation("configure_gameplay", "place_lighting", {
            "profile": "vista_playable_home_neutral_day_v2",
            "light_mobility": "movable",
            "exposure": {
                "method": "manual",
                "bias": -6.0,
                "apply_physical_camera_exposure": False,
            },
            "indoor_lights": indoor_lights,
        }),
        _operation("build_navigation", "place_navmesh_bounds", {
            "bounds": nav_bounds,
            "agent": dict(runtime.get("navigation_agent", {})),
        }),
        _operation("save_reload_verify", "save_reload_verify", {
            "map_path": map_path,
            "expected_semantic_ids": sorted(room_ids | portal_ids | set(entities_by_id)),
            "expected_npc_entity_ids": sorted(profile["entity_id"] for profile in runtime.get("npc_profiles", [])),
        }),
    ])

    operation_ids = [operation["operation_id"] for operation in operations]
    _require(len(operation_ids) == len(set(operation_ids)),
             "VISTA_HOME_PLAN_OPERATION_COLLISION", "operation IDs collided")
    compiled = {
        "schema_version": COMPOSITION_SPEC_SCHEMA,
        "plan_id": value["plan_id"],
        "plan_content_digest": plan_digest,
        "house_revision": house.get("revision"),
        "content_namespace": namespace,
        "map_path": map_path,
        "stable_tag_prefix": TAG_PREFIX,
        "operations": operations,
    }
    raw = canonical_json(compiled)
    return CompositionSpec(compiled, raw, hashlib.sha256(raw).hexdigest())
