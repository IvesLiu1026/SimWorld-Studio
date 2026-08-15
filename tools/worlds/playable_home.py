"""Fail-closed contracts and deterministic compiler for VISTA Playable Home.

This module deliberately performs no network, Blender, Unreal, dataset, or
runtime work.  It validates public source fixtures, resolves room-local metres
to world centimetres, and emits an immutable build plan for downstream tools.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import itertools
import json
import math
import os
import pathlib
import sys
import tempfile
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

import jsonschema


HOUSE_SCHEMA_VERSION = "simworld.vista.playable-house/v1"
EVENT_SCHEMA_VERSION = "simworld.vista.playable-event/v1"
BUILD_PLAN_SCHEMA_VERSION = "simworld.vista.playable-home-build-plan/v1"
COMPILER_VERSION = "vista-playable-home-compiler/1"

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[2]
SCHEMA_ROOT = REPOSITORY_ROOT / "world_packs" / "schemas"
SCHEMA_PATHS = {
    HOUSE_SCHEMA_VERSION: SCHEMA_ROOT / "vista-playable-house-v1.schema.json",
    EVENT_SCHEMA_VERSION: SCHEMA_ROOT / "vista-playable-event-v1.schema.json",
    BUILD_PLAN_SCHEMA_VERSION: SCHEMA_ROOT / "vista-playable-home-build-plan-v1.schema.json",
}

MAX_DOCUMENT_BYTES = 32 * 1024 * 1024
MAX_JSON_DEPTH = 96
PRIVATE_OR_EXECUTABLE_KEYS = frozenset(
    {
        "oracle_assistance_required",
        "oracle_label",
        "review_notes",
        "private_evidence",
        "private_evidence_atoms",
        "evaluation_target",
        "private_evaluation_reference",
        "render_script",
        "execute_python_script",
        "python_code",
        "shell_command",
        "blueprint_graph",
        "filesystem_write",
        "auth_token",
        "access_token",
    }
)


@dataclass(frozen=True)
class PlayableHomeContractError(Exception):
    """A stable, value-sanitized contract failure."""

    code: str
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.code} at {self.path}: {self.message}"


def _fail(code: str, path: str, message: str) -> None:
    raise PlayableHomeContractError(code=code, path=path, message=message)


def canonical_json_bytes(value: Any) -> bytes:
    """Return the single digest representation used by all three contracts."""

    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise PlayableHomeContractError(
            "VISTA_HOME_CANONICAL_JSON_INVALID", "$", "Document is not finite canonical JSON"
        ) from exc
    return text.encode("utf-8", "strict")


def content_digest(value: Mapping[str, Any]) -> str:
    body = copy.deepcopy(dict(value))
    body.pop("content_digest", None)
    return hashlib.sha256(canonical_json_bytes(body)).hexdigest()


def seal_document(value: Mapping[str, Any]) -> dict[str, Any]:
    sealed = copy.deepcopy(dict(value))
    sealed["content_digest"] = content_digest(sealed)
    return sealed


def _reject_json_constant(value: str) -> None:
    _fail("VISTA_HOME_JSON_NON_FINITE", "$", f"JSON constant {value!r} is not permitted")


def load_json(path: os.PathLike[str] | str) -> dict[str, Any]:
    source = pathlib.Path(path)
    try:
        size = source.stat().st_size
    except OSError as exc:
        raise PlayableHomeContractError(
            "VISTA_HOME_INPUT_UNREADABLE", "$", "Input document cannot be read"
        ) from exc
    if size > MAX_DOCUMENT_BYTES:
        _fail("VISTA_HOME_INPUT_TOO_LARGE", "$", "Input document exceeds the byte limit")
    try:
        parsed = json.loads(
            source.read_text(encoding="utf-8"),
            parse_constant=_reject_json_constant,
        )
    except PlayableHomeContractError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PlayableHomeContractError(
            "VISTA_HOME_JSON_INVALID", "$", "Input is not strict UTF-8 JSON"
        ) from exc
    if type(parsed) is not dict:
        _fail("VISTA_HOME_JSON_INVALID", "$", "Top-level JSON value must be an object")
    _assert_finite_and_bounded(parsed)
    return parsed


def _assert_finite_and_bounded(value: Any, path: str = "$", depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        _fail("VISTA_HOME_JSON_TOO_DEEP", path, "JSON nesting exceeds the limit")
    if type(value) is float and not math.isfinite(value):
        _fail("VISTA_HOME_JSON_NON_FINITE", path, "Non-finite numbers are not permitted")
    if type(value) is dict:
        for key, child in value.items():
            if type(key) is not str:
                _fail("VISTA_HOME_JSON_INVALID", path, "Object keys must be strings")
            _assert_finite_and_bounded(child, f"{path}.{key}", depth + 1)
    elif type(value) is list:
        for index, child in enumerate(value):
            _assert_finite_and_bounded(child, f"{path}[{index}]", depth + 1)


def _schema(schema_version: str) -> dict[str, Any]:
    try:
        schema_path = SCHEMA_PATHS[schema_version]
    except KeyError:
        _fail("VISTA_HOME_SCHEMA_VERSION_UNSUPPORTED", "$.schema_version", "Unsupported schema version")
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
    except (OSError, json.JSONDecodeError, jsonschema.SchemaError) as exc:
        raise PlayableHomeContractError(
            "VISTA_HOME_SCHEMA_UNAVAILABLE", "$", "Pinned schema is unavailable or invalid"
        ) from exc
    return schema


def _json_path(error: jsonschema.ValidationError) -> str:
    path = "$"
    for part in error.absolute_path:
        path += f"[{part}]" if type(part) is int else f".{part}"
    return path


def _validate_schema(document: Mapping[str, Any], schema_version: str) -> None:
    _assert_finite_and_bounded(document)
    validator = jsonschema.Draft202012Validator(_schema(schema_version))
    errors = sorted(
        validator.iter_errors(document),
        key=lambda error: (list(error.absolute_path), error.validator or "", error.message),
    )
    if errors:
        error = errors[0]
        _fail("VISTA_HOME_SCHEMA_INVALID", _json_path(error), f"Schema constraint {error.validator!r} failed")


def _verify_digest(document: Mapping[str, Any], path: str = "$.content_digest") -> None:
    if document.get("content_digest") != content_digest(document):
        _fail("VISTA_HOME_DIGEST_MISMATCH", path, "Content digest does not match canonical document")


def _duplicates(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return sorted(duplicates)


def _require_unique(values: Iterable[str], path: str, kind: str) -> None:
    if _duplicates(values):
        _fail("VISTA_HOME_DUPLICATE_ID", path, f"Duplicate {kind} identifier")


def _scan_prohibited_keys(value: Any, path: str = "$") -> None:
    if type(value) is dict:
        for key, child in value.items():
            normalized = key.strip().lower().replace("-", "_")
            if normalized in PRIVATE_OR_EXECUTABLE_KEYS:
                _fail("VISTA_HOME_PROHIBITED_FIELD", f"{path}.{key}", "Private or executable field is prohibited")
            _scan_prohibited_keys(child, f"{path}.{key}")
    elif type(value) is list:
        for index, child in enumerate(value):
            _scan_prohibited_keys(child, f"{path}[{index}]")
    elif type(value) is str and value.strip().lower().startswith(
        ("/home/", "/root/", "/mnt/", "/nas/", "file://")
    ):
        _fail("VISTA_HOME_PRIVATE_PATH_PROHIBITED", path, "Absolute private paths are prohibited")


def _check_safe_uri(uri: str, path: str) -> None:
    if "\\" in uri or "%" in uri:
        _fail("VISTA_HOME_ASSET_URI_UNSAFE", path, "Encoded or backslash path syntax is prohibited")
    parsed = urlsplit(uri)
    if parsed.scheme not in {"builtin", "procedural", "bundle"} or not parsed.netloc:
        _fail("VISTA_HOME_ASSET_URI_UNSAFE", path, "Asset URI scheme or authority is invalid")
    if parsed.query or parsed.fragment:
        _fail("VISTA_HOME_ASSET_URI_UNSAFE", path, "Asset URI query and fragment are prohibited")
    segments = [parsed.netloc, *parsed.path.split("/")]
    if any(segment in {".", ".."} for segment in segments):
        _fail("VISTA_HOME_ASSET_URI_UNSAFE", path, "Asset URI traversal is prohibited")


def _index(items: Sequence[Mapping[str, Any]], key: str) -> dict[str, Mapping[str, Any]]:
    return {str(item[key]): item for item in items}


def _validate_bounds(bounds: Mapping[str, Sequence[float]], path: str) -> None:
    if any(low >= high for low, high in zip(bounds["min_m"], bounds["max_m"])):
        _fail("VISTA_HOME_BOUNDS_INVALID", path, "Every bounds minimum must be below its maximum")


def _reachable_rooms(house: Mapping[str, Any]) -> set[str]:
    graph: dict[str, set[str]] = defaultdict(set)
    for room in house["rooms"]:
        graph[room["room_id"]]
    for portal in house["portals"]:
        if portal["nav_policy"] != "blocked":
            graph[portal["from_room_id"]].add(portal["to_room_id"])
            graph[portal["to_room_id"]].add(portal["from_room_id"])
    start = house["runtime_profile"]["player_start"]["room_id"]
    reached = {start}
    queue = deque([start])
    while queue:
        room_id = queue.popleft()
        for neighbor in sorted(graph[room_id]):
            if neighbor not in reached:
                reached.add(neighbor)
                queue.append(neighbor)
    return reached


def validate_house(house: Mapping[str, Any]) -> None:
    """Validate schema, stable identity, topology, affordances and provenance."""

    _validate_schema(house, HOUSE_SCHEMA_VERSION)
    _scan_prohibited_keys(house)
    _verify_digest(house)

    assets = _index(house["asset_catalog"], "asset_id")
    rooms = _index(house["rooms"], "room_id")
    portals = _index(house["portals"], "portal_id")
    entities = _index(house["entities"], "entity_id")
    relations = _index(house["relations"], "relation_id")
    _require_unique((item["asset_id"] for item in house["asset_catalog"]), "$.asset_catalog", "asset")
    _require_unique((item["room_id"] for item in house["rooms"]), "$.rooms", "room")
    _require_unique((item["portal_id"] for item in house["portals"]), "$.portals", "portal")
    _require_unique((item["entity_id"] for item in house["entities"]), "$.entities", "entity")
    _require_unique((item["relation_id"] for item in house["relations"]), "$.relations", "relation")

    house_prefix = f"{house['house_id']}/room."
    for index, asset in enumerate(house["asset_catalog"]):
        _check_safe_uri(asset["uri"], f"$.asset_catalog[{index}].uri")

    entities_by_room: dict[str, set[str]] = defaultdict(set)
    for index, room in enumerate(house["rooms"]):
        room_id = room["room_id"]
        if not room_id.startswith(house_prefix):
            _fail("VISTA_HOME_ID_PREFIX_INVALID", f"$.rooms[{index}].room_id", "Room ID is outside house namespace")
        if room["bundle_asset_ref"] not in assets:
            _fail("VISTA_HOME_ASSET_UNKNOWN", f"$.rooms[{index}].bundle_asset_ref", "Room bundle asset is unknown")
        _validate_bounds(room["bounds_m"], f"$.rooms[{index}].bounds_m")
        _require_unique((camera["camera_id"] for camera in room["review_cameras"]), f"$.rooms[{index}].review_cameras", "camera")

    for index, entity in enumerate(house["entities"]):
        entity_id = entity["entity_id"]
        room_id = entity["room_id"]
        if room_id not in rooms:
            _fail("VISTA_HOME_ROOM_UNKNOWN", f"$.entities[{index}].room_id", "Entity room is unknown")
        if not entity_id.startswith(f"{room_id}/entity."):
            _fail("VISTA_HOME_ID_PREFIX_INVALID", f"$.entities[{index}].entity_id", "Entity ID is outside room namespace")
        if entity["asset_ref"] not in assets:
            _fail("VISTA_HOME_ASSET_UNKNOWN", f"$.entities[{index}].asset_ref", "Entity asset is unknown")
        entities_by_room[room_id].add(entity_id)
        _require_unique((anchor["anchor_id"] for anchor in entity["placement_anchors"]), f"$.entities[{index}].placement_anchors", "anchor")

        role = entity["component_role"]
        affordances = set(entity["affordances"])
        state = entity["initial_state"]
        if affordances & {"open", "close"} and role not in {"door", "container"}:
            _fail("VISTA_HOME_AFFORDANCE_INVALID", f"$.entities[{index}].affordances", "Open/close requires door or container role")
        if "toggle" in affordances and role != "appliance":
            _fail("VISTA_HOME_AFFORDANCE_INVALID", f"$.entities[{index}].affordances", "Toggle requires appliance role")
        if "sit" in affordances and role != "static_furniture":
            _fail("VISTA_HOME_AFFORDANCE_INVALID", f"$.entities[{index}].affordances", "Sit requires static furniture role")
        if affordances & {"pick_up", "drop", "place"} and role != "pickup":
            _fail("VISTA_HOME_AFFORDANCE_INVALID", f"$.entities[{index}].affordances", "Portable affordances require pickup role")
        if role == "pickup":
            if entity["collision_policy"] != "pickup_physics" or not state.get("portable", False):
                _fail("VISTA_HOME_PICKUP_INVALID", f"$.entities[{index}]", "Pickup must use pickup physics and portable baseline")
        if role == "door":
            if entity["collision_policy"] != "door_dynamic" or entity["mobility"] != "movable" or "open" not in state:
                _fail("VISTA_HOME_DOOR_INVALID", f"$.entities[{index}]", "Door must be movable, dynamic and declare open state")
        if role == "npc" and entity["collision_policy"] != "pawn":
            _fail("VISTA_HOME_NPC_INVALID", f"$.entities[{index}]", "NPC must use pawn collision")

    for index, room in enumerate(house["rooms"]):
        expected = entities_by_room[room["room_id"]]
        actual = set(room["semantic_inventory"])
        if actual != expected:
            _fail("VISTA_HOME_INVENTORY_MISMATCH", f"$.rooms[{index}].semantic_inventory", "Room inventory must exactly cover its entities")

    nav_agent = house["runtime_profile"]["navigation_agent"]
    for index, portal in enumerate(house["portals"]):
        from_id = portal["from_room_id"]
        to_id = portal["to_room_id"]
        if from_id not in rooms or to_id not in rooms or from_id == to_id:
            _fail("VISTA_HOME_PORTAL_ROOM_INVALID", f"$.portals[{index}]", "Portal endpoints must be distinct known rooms")
        from_slug = from_id.split("/room.", 1)[1]
        to_slug = to_id.split("/room.", 1)[1]
        expected_prefix = f"{house['house_id']}/portal.{from_slug}-{to_slug}."
        if not portal["portal_id"].startswith(expected_prefix):
            _fail("VISTA_HOME_ID_PREFIX_INVALID", f"$.portals[{index}].portal_id", "Portal ID does not match ordered endpoints")
        door_id = portal["door_entity_id"]
        if portal["nav_policy"] == "dynamic_door":
            if door_id not in entities:
                _fail("VISTA_HOME_PORTAL_DOOR_INVALID", f"$.portals[{index}].door_entity_id", "Dynamic portal requires a known door")
            door = entities[door_id]
            if door["component_role"] != "door" or door["room_id"] not in {from_id, to_id}:
                _fail("VISTA_HOME_PORTAL_DOOR_INVALID", f"$.portals[{index}].door_entity_id", "Portal door role or room is invalid")
            if not {"open", "close"}.issubset(door["affordances"]):
                _fail("VISTA_HOME_PORTAL_DOOR_INVALID", f"$.portals[{index}].door_entity_id", "Portal door lacks open/close affordances")
            if door["initial_state"]["open"] != (portal["initial_state"] == "open"):
                _fail("VISTA_HOME_PORTAL_STATE_MISMATCH", f"$.portals[{index}].initial_state", "Portal and door baseline states disagree")
        elif door_id is not None:
            _fail("VISTA_HOME_PORTAL_DOOR_INVALID", f"$.portals[{index}].door_entity_id", "Only dynamic portals may bind a door")
        if portal["nav_policy"] != "blocked":
            if portal["clearance"]["width_m"] < 2 * nav_agent["radius_m"]:
                _fail("VISTA_HOME_PORTAL_CLEARANCE_INVALID", f"$.portals[{index}].clearance.width_m", "Portal is narrower than the navigation agent")
            if portal["clearance"]["height_m"] < nav_agent["height_m"]:
                _fail("VISTA_HOME_PORTAL_CLEARANCE_INVALID", f"$.portals[{index}].clearance.height_m", "Portal is shorter than the navigation agent")

    runtime = house["runtime_profile"]
    if runtime["player_start"]["room_id"] not in rooms:
        _fail("VISTA_HOME_PLAYER_START_INVALID", "$.runtime_profile.player_start.room_id", "PlayerStart room is unknown")
    for field in ("pawn_asset_ref", "game_mode_asset_ref"):
        if runtime[field] not in assets:
            _fail("VISTA_HOME_ASSET_UNKNOWN", f"$.runtime_profile.{field}", "Runtime asset is unknown")
    _require_unique((profile["npc_id"] for profile in runtime["npc_profiles"]), "$.runtime_profile.npc_profiles", "NPC profile")
    _require_unique((profile["entity_id"] for profile in runtime["npc_profiles"]), "$.runtime_profile.npc_profiles", "NPC entity")
    for index, profile in enumerate(runtime["npc_profiles"]):
        if profile["entity_id"] not in entities or entities[profile["entity_id"]]["component_role"] != "npc":
            _fail("VISTA_HOME_NPC_INVALID", f"$.runtime_profile.npc_profiles[{index}].entity_id", "NPC profile entity is unknown or not an NPC")
        for room_id in [profile["home_room_id"], *profile["patrol_room_ids"]]:
            if room_id not in rooms:
                _fail("VISTA_HOME_ROOM_UNKNOWN", f"$.runtime_profile.npc_profiles[{index}]", "NPC profile references an unknown room")

    known_relation_nodes = set(rooms) | set(entities)
    for index, relation in enumerate(house["relations"]):
        subject = relation["subject_id"]
        obj = relation["object_id"]
        if subject not in known_relation_nodes or obj not in known_relation_nodes:
            _fail("VISTA_HOME_RELATION_TARGET_INVALID", f"$.relations[{index}]", "Relation target is unknown")
        if relation["predicate"] == "inside" and not (subject in entities and obj in rooms):
            _fail("VISTA_HOME_RELATION_INVALID", f"$.relations[{index}]", "Inside relation must map entity to room")
        if relation["predicate"] == "adjacent_to" and not (subject in rooms and obj in rooms):
            _fail("VISTA_HOME_RELATION_INVALID", f"$.relations[{index}]", "Adjacency relation must map room to room")

    if _reachable_rooms(house) != set(rooms):
        _fail("VISTA_HOME_GRAPH_DISCONNECTED", "$.portals", "PlayerStart cannot reach every room anchor")

    budgets = house["budgets"]
    if len(rooms) > budgets["max_rooms"] or len(portals) > budgets["max_portals"] or len(entities) > budgets["max_entities"]:
        _fail("VISTA_HOME_BUDGET_EXCEEDED", "$.budgets", "Declared object counts exceed a budget")


def _event_condition_lists(event: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [*event["triggers"], *event["success_conditions"], *event["failure_conditions"]]


def _validate_action(action: Mapping[str, Any], rooms: Mapping[str, Any], entities: Mapping[str, Any], path: str) -> None:
    action_name = action["action"]
    if action_name == "navigate_to":
        if action["room_id"] not in rooms:
            _fail("VISTA_HOME_EVENT_TARGET_UNKNOWN", f"{path}.room_id", "NPC action room is unknown")
        return
    if action_name in {"wait", "speak"}:
        return
    target_id = action["target_id"]
    if target_id not in entities:
        _fail("VISTA_HOME_EVENT_TARGET_UNKNOWN", f"{path}.target_id", "NPC action entity is unknown")
    required = {
        "look_at": None,
        "pick_up": "pick_up",
        "place": None,
        "open_door": "open",
        "close_door": "close",
        "sit": "sit",
    }[action_name]
    if required and required not in entities[target_id]["affordances"]:
        _fail("VISTA_HOME_EVENT_AFFORDANCE_INVALID", f"{path}.target_id", "NPC action targets an unsupported affordance")


def validate_event(event: Mapping[str, Any], house: Mapping[str, Any]) -> None:
    """Validate one public EventSpec against the exact immutable house."""

    validate_house(house)
    _validate_schema(event, EVENT_SCHEMA_VERSION)
    _scan_prohibited_keys(event)
    _verify_digest(event)

    compatibility = event["compatible_house"]
    expected = (house["house_id"], house["revision"], house["content_digest"])
    actual = (compatibility["house_id"], compatibility["revision"], compatibility["content_digest"])
    if actual != expected:
        _fail("VISTA_HOME_EVENT_STALE_REVISION", "$.compatible_house", "Event does not bind the exact house revision and digest")
    if event["source"]["sample_id"] != event["event_id"]:
        _fail("VISTA_HOME_EVENT_SOURCE_MISMATCH", "$.source.sample_id", "Public source sample does not match event ID")

    rooms = _index(house["rooms"], "room_id")
    base_entities = _index(house["entities"], "entity_id")
    assets = _index(house["asset_catalog"], "asset_id")
    npcs = {profile["npc_id"]: profile for profile in house["runtime_profile"]["npc_profiles"]}
    spawn_entities: dict[str, Mapping[str, Any]] = {}
    _require_unique((operation["op_id"] for operation in event["initial_operations"]), "$.initial_operations", "operation")
    for index, operation in enumerate(event["initial_operations"]):
        if operation["op"] == "spawn_fixture":
            entity_id = operation["entity_id"]
            if entity_id in base_entities or entity_id in spawn_entities:
                _fail("VISTA_HOME_EVENT_SPAWN_CONFLICT", f"$.initial_operations[{index}].entity_id", "Spawned fixture ID already exists")
            if operation["room_id"] not in rooms or not entity_id.startswith(f"{operation['room_id']}/entity."):
                _fail("VISTA_HOME_EVENT_TARGET_UNKNOWN", f"$.initial_operations[{index}]", "Spawned fixture room or namespace is invalid")
            if operation["asset_ref"] not in assets:
                _fail("VISTA_HOME_ASSET_UNKNOWN", f"$.initial_operations[{index}].asset_ref", "Spawned fixture asset is unknown")
            spawn_entities[entity_id] = operation

    entities: dict[str, Mapping[str, Any]] = {**base_entities, **spawn_entities}
    participant_rooms = set(event["participating_room_ids"])
    participant_entities = set(event["participating_entity_ids"])
    if not participant_rooms.issubset(rooms):
        _fail("VISTA_HOME_EVENT_TARGET_UNKNOWN", "$.participating_room_ids", "Participating room is unknown")
    if not participant_entities.issubset(entities):
        _fail("VISTA_HOME_EVENT_TARGET_UNKNOWN", "$.participating_entity_ids", "Participating entity is unknown")

    goals = _index(event["public_goals"], "goal_id")
    _require_unique((goal["goal_id"] for goal in event["public_goals"]), "$.public_goals", "goal")
    for index, operation in enumerate(event["initial_operations"]):
        path = f"$.initial_operations[{index}]"
        op = operation["op"]
        if op == "spawn_fixture":
            target_id = operation["entity_id"]
            room_id = operation["room_id"]
        elif op in {"set_transform", "set_state", "set_visibility", "set_portable"}:
            target_id = operation["target_id"]
            if target_id not in entities:
                _fail("VISTA_HOME_EVENT_TARGET_UNKNOWN", f"{path}.target_id", "Operation target is unknown")
            room_id = operation.get("room_id", entities[target_id]["room_id"])
            if room_id not in rooms:
                _fail("VISTA_HOME_EVENT_TARGET_UNKNOWN", f"{path}.room_id", "Operation room is unknown")
            if op == "set_portable" and operation["portable"]:
                if entities[target_id]["component_role"] != "pickup" or "pick_up" not in entities[target_id]["affordances"]:
                    _fail("VISTA_HOME_EVENT_AFFORDANCE_INVALID", f"{path}.target_id", "Portable operation target is not a pickup")
        elif op == "set_npc_queue":
            if operation["npc_id"] not in npcs:
                _fail("VISTA_HOME_EVENT_TARGET_UNKNOWN", f"{path}.npc_id", "NPC queue target is unknown")
            for action_index, action in enumerate(operation["actions"]):
                _validate_action(action, rooms, entities, f"{path}.actions[{action_index}]")
            continue
        elif op == "set_goal":
            if operation["goal_id"] not in goals:
                _fail("VISTA_HOME_EVENT_GOAL_UNKNOWN", f"{path}.goal_id", "Operation goal is unknown")
            continue
        else:  # protected by schema; retained as a fail-closed guard
            _fail("VISTA_HOME_EVENT_OPERATION_UNSUPPORTED", f"{path}.op", "Unsupported operation")
        if target_id not in participant_entities or room_id not in participant_rooms:
            _fail("VISTA_HOME_EVENT_PARTICIPANT_MISSING", path, "Operation target and room must be declared participants")

    conditions = _event_condition_lists(event)
    _require_unique((condition["condition_id"] for condition in conditions), "$.*_conditions", "condition")
    success_ids = {condition["condition_id"] for condition in event["success_conditions"]}
    for index, goal in enumerate(event["public_goals"]):
        if not set(goal["success_condition_ids"]).issubset(success_ids):
            _fail("VISTA_HOME_EVENT_GOAL_CONDITION_UNKNOWN", f"$.public_goals[{index}].success_condition_ids", "Goal references a non-success condition")
    for index, condition in enumerate(conditions):
        path = f"$.conditions[{index}]"
        if "room_id" in condition and condition["room_id"] not in rooms:
            _fail("VISTA_HOME_EVENT_TARGET_UNKNOWN", f"{path}.room_id", "Condition room is unknown")
        if "target_id" in condition:
            target_id = condition["target_id"]
            if target_id not in entities:
                _fail("VISTA_HOME_EVENT_TARGET_UNKNOWN", f"{path}.target_id", "Condition entity is unknown")
            if target_id not in participant_entities:
                _fail("VISTA_HOME_EVENT_PARTICIPANT_MISSING", f"{path}.target_id", "Condition entity must be a participant")
            if condition["type"] == "interaction" and condition["affordance"] not in entities[target_id]["affordances"]:
                _fail("VISTA_HOME_EVENT_AFFORDANCE_INVALID", f"{path}.affordance", "Condition targets an unsupported affordance")


def public_event_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    """Return a deep, allowlisted public payload; no caller keys are forwarded."""

    allowed = (
        "schema_version", "event_id", "title", "compatible_house",
        "participating_room_ids", "participating_entity_ids", "initial_operations",
        "public_goals", "triggers", "success_conditions", "failure_conditions",
        "timeout_s", "reset_policy", "source", "content_digest",
    )
    payload = {key: copy.deepcopy(event[key]) for key in allowed}
    _scan_prohibited_keys(payload)
    return payload


def baseline_runtime_state(house: Mapping[str, Any]) -> dict[str, Any]:
    validate_house(house)
    return {
        "house_id": house["house_id"],
        "revision": house["revision"],
        "house_digest": house["content_digest"],
        "active_event_id": None,
        "entities": {
            entity["entity_id"]: {
                "room_id": entity["room_id"],
                "transform": copy.deepcopy(entity["transform"]),
                "state": copy.deepcopy(entity["initial_state"]),
            }
            for entity in house["entities"]
        },
        "npc_queues": {profile["npc_id"]: [] for profile in house["runtime_profile"]["npc_profiles"]},
        "active_goals": [],
        "spawned_entity_ids": [],
    }


def apply_event_overlay(house: Mapping[str, Any], event: Mapping[str, Any]) -> dict[str, Any]:
    """Atomically resolve an EventSpec into a fresh baseline runtime state."""

    validate_event(event, house)  # validates every target before any mutation
    state = baseline_runtime_state(house)
    state["active_event_id"] = event["event_id"]
    for operation in event["initial_operations"]:
        op = operation["op"]
        if op == "spawn_fixture":
            entity_id = operation["entity_id"]
            state["entities"][entity_id] = {
                "room_id": operation["room_id"],
                "transform": copy.deepcopy(operation["transform"]),
                "state": copy.deepcopy(operation["initial_state"]),
            }
            state["spawned_entity_ids"].append(entity_id)
        elif op == "set_transform":
            target = state["entities"][operation["target_id"]]
            target["room_id"] = operation["room_id"]
            target["transform"] = copy.deepcopy(operation["transform"])
        elif op == "set_state":
            state["entities"][operation["target_id"]]["state"].update(copy.deepcopy(operation["state_patch"]))
        elif op == "set_visibility":
            state["entities"][operation["target_id"]]["state"]["visible"] = operation["visible"]
        elif op == "set_portable":
            state["entities"][operation["target_id"]]["state"]["portable"] = operation["portable"]
        elif op == "set_npc_queue":
            state["npc_queues"][operation["npc_id"]] = copy.deepcopy(operation["actions"])
        elif op == "set_goal":
            if operation["enabled"] and operation["goal_id"] not in state["active_goals"]:
                state["active_goals"].append(operation["goal_id"])
            elif not operation["enabled"] and operation["goal_id"] in state["active_goals"]:
                state["active_goals"].remove(operation["goal_id"])
    state["spawned_entity_ids"].sort()
    state["active_goals"].sort()
    return state


def _clean_number(value: float) -> float | int:
    rounded = round(float(value), 6)
    if abs(rounded) < 0.0000005:
        rounded = 0.0
    integer = round(rounded)
    if abs(rounded - integer) < 0.0000005:
        return int(integer)
    return rounded


def _quaternion(rotation_deg: Sequence[float]) -> tuple[float, float, float, float]:
    roll, pitch, yaw = (math.radians(float(value)) for value in rotation_deg)
    cr, sr = math.cos(roll / 2), math.sin(roll / 2)
    cp, sp = math.cos(pitch / 2), math.sin(pitch / 2)
    cy, sy = math.cos(yaw / 2), math.sin(yaw / 2)
    return (
        cr * cp * cy + sr * sp * sy,
        sr * cp * cy - cr * sp * sy,
        cr * sp * cy + sr * cp * sy,
        cr * cp * sy - sr * sp * cy,
    )


def _qmul(left: Sequence[float], right: Sequence[float]) -> tuple[float, float, float, float]:
    lw, lx, ly, lz = left
    rw, rx, ry, rz = right
    return (
        lw * rw - lx * rx - ly * ry - lz * rz,
        lw * rx + lx * rw + ly * rz - lz * ry,
        lw * ry - lx * rz + ly * rw + lz * rx,
        lw * rz + lx * ry - ly * rx + lz * rw,
    )


def _qrotate(quaternion: Sequence[float], vector: Sequence[float]) -> tuple[float, float, float]:
    vector_q = (0.0, float(vector[0]), float(vector[1]), float(vector[2]))
    conjugate = (quaternion[0], -quaternion[1], -quaternion[2], -quaternion[3])
    result = _qmul(_qmul(quaternion, vector_q), conjugate)
    return (result[1], result[2], result[3])


def _euler(quaternion: Sequence[float]) -> list[float | int]:
    w, x, y, z = quaternion
    roll = math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y))
    sin_pitch = 2 * (w * y - z * x)
    pitch = math.copysign(math.pi / 2, sin_pitch) if abs(sin_pitch) >= 1 else math.asin(sin_pitch)
    yaw = math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))
    return [_clean_number(math.degrees(value)) for value in (roll, pitch, yaw)]


def _compose_transform(parent: Mapping[str, Sequence[float]], local: Mapping[str, Sequence[float]]) -> dict[str, list[float | int]]:
    parent_q = _quaternion(parent["rotation_deg"])
    local_q = _quaternion(local["rotation_deg"])
    scaled_location = [float(local["location_m"][i]) * float(parent["scale"][i]) for i in range(3)]
    rotated = _qrotate(parent_q, scaled_location)
    location = [float(parent["location_m"][i]) + rotated[i] for i in range(3)]
    return {
        "location_m": [_clean_number(value) for value in location],
        "rotation_deg": _euler(_qmul(parent_q, local_q)),
        "scale": [_clean_number(float(parent["scale"][i]) * float(local["scale"][i])) for i in range(3)],
    }


def _to_cm(transform_m: Mapping[str, Sequence[float]]) -> dict[str, Any]:
    return {
        "location_cm": [_clean_number(float(value) * 100) for value in transform_m["location_m"]],
        "rotation_deg": [_clean_number(value) for value in transform_m["rotation_deg"]],
        "scale": [_clean_number(value) for value in transform_m["scale"]],
    }


def _point_world(parent: Mapping[str, Sequence[float]], point_m: Sequence[float]) -> list[float | int]:
    local = {"location_m": list(point_m), "rotation_deg": [0, 0, 0], "scale": [1, 1, 1]}
    return _to_cm(_compose_transform(parent, local))["location_cm"]


def _bounds_world_cm(room: Mapping[str, Any]) -> dict[str, list[float | int]]:
    bounds = room["bounds_m"]
    corners = itertools.product(*zip(bounds["min_m"], bounds["max_m"]))
    world = [_point_world(room["transform"], corner) for corner in corners]
    return {
        "min_cm": [_clean_number(min(point[axis] for point in world)) for axis in range(3)],
        "max_cm": [_clean_number(max(point[axis] for point in world)) for axis in range(3)],
    }


def _resolve_operation(operation: Mapping[str, Any], rooms: Mapping[str, Any], assets: Mapping[str, Any]) -> dict[str, Any]:
    op = operation["op"]
    if op == "set_transform":
        return {
            "op_id": operation["op_id"], "op": op, "target_id": operation["target_id"],
            "room_id": operation["room_id"],
            "world_transform_cm": _to_cm(_compose_transform(rooms[operation["room_id"]]["transform"], operation["transform"])),
        }
    if op == "spawn_fixture":
        return {
            "op_id": operation["op_id"], "op": op, "entity_id": operation["entity_id"],
            "room_id": operation["room_id"], "category": operation["category"],
            "asset": copy.deepcopy(assets[operation["asset_ref"]]),
            "world_transform_cm": _to_cm(_compose_transform(rooms[operation["room_id"]]["transform"], operation["transform"])),
            "component_role": operation["component_role"], "mobility": operation["mobility"],
            "collision_policy": operation["collision_policy"], "affordances": copy.deepcopy(operation["affordances"]),
            "initial_state": copy.deepcopy(operation["initial_state"]),
        }
    return copy.deepcopy(dict(operation))


def compile_build_plan(house: Mapping[str, Any], events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Compile exact source fixtures into one deterministic Unreal-facing plan."""

    validate_house(house)
    _require_unique((event["event_id"] for event in events), "$.events", "event")
    for event in events:
        validate_event(event, house)

    assets = _index(house["asset_catalog"], "asset_id")
    rooms = _index(house["rooms"], "room_id")
    resolved_rooms: list[dict[str, Any]] = []
    for room in sorted(house["rooms"], key=lambda item: item["room_id"]):
        room_transform = room["transform"]
        cameras = []
        for camera in room["review_cameras"]:
            local = {"location_m": camera["location_m"], "rotation_deg": camera["rotation_deg"], "scale": [1, 1, 1]}
            cameras.append({
                "camera_id": camera["camera_id"],
                "world_transform_cm": _to_cm(_compose_transform(room_transform, local)),
                "fov_deg": camera["fov_deg"],
            })
        resolved_rooms.append({
            "room_id": room["room_id"], "kind": room["kind"], "label": room["label"],
            "bundle": copy.deepcopy(assets[room["bundle_asset_ref"]]),
            "world_transform_cm": _to_cm(room_transform), "world_bounds_cm": _bounds_world_cm(room),
            "anchor_world_cm": _point_world(room_transform, room["anchor_m"]),
            "review_cameras": cameras, "semantic_inventory": sorted(room["semantic_inventory"]),
        })

    resolved_entities: list[dict[str, Any]] = []
    for entity in sorted(house["entities"], key=lambda item: item["entity_id"]):
        world_m = _compose_transform(rooms[entity["room_id"]]["transform"], entity["transform"])
        anchors = []
        for anchor in entity["placement_anchors"]:
            anchors.append({
                "anchor_id": anchor["anchor_id"],
                "world_transform_cm": _to_cm(_compose_transform(world_m, anchor["transform"])),
            })
        resolved_entities.append({
            "entity_id": entity["entity_id"], "room_id": entity["room_id"], "category": entity["category"],
            "asset": copy.deepcopy(assets[entity["asset_ref"]]), "world_transform_cm": _to_cm(world_m),
            "tags": sorted(entity["tags"]), "component_role": entity["component_role"],
            "mobility": entity["mobility"], "collision_policy": entity["collision_policy"],
            "nav_obstacle": entity["nav_obstacle"], "affordances": copy.deepcopy(entity["affordances"]),
            "baseline_state": copy.deepcopy(entity["initial_state"]), "placement_anchors": anchors,
        })

    resolved_portals = []
    for portal in sorted(house["portals"], key=lambda item: item["portal_id"]):
        resolved_portals.append({
            "portal_id": portal["portal_id"], "from_room_id": portal["from_room_id"], "to_room_id": portal["to_room_id"],
            "world_transform_cm": _to_cm(portal["world_transform"]),
            "clearance_cm": {key.replace("_m", "_cm"): _clean_number(value * 100) for key, value in portal["clearance"].items()},
            "door_entity_id": portal["door_entity_id"], "initial_state": portal["initial_state"], "nav_policy": portal["nav_policy"],
        })

    runtime = house["runtime_profile"]
    player_room = rooms[runtime["player_start"]["room_id"]]
    resolved_runtime = {
        "player_start": {
            "room_id": runtime["player_start"]["room_id"],
            "world_transform_cm": _to_cm(_compose_transform(player_room["transform"], runtime["player_start"]["transform"])),
        },
        "pawn": copy.deepcopy(assets[runtime["pawn_asset_ref"]]),
        "game_mode": copy.deepcopy(assets[runtime["game_mode_asset_ref"]]),
        "controls": copy.deepcopy(runtime["controls"]),
        "interaction_distance_cm": _clean_number(runtime["interaction_distance_m"] * 100),
        "navigation_agent": {
            "radius_cm": _clean_number(runtime["navigation_agent"]["radius_m"] * 100),
            "height_cm": _clean_number(runtime["navigation_agent"]["height_m"] * 100),
            "max_step_height_cm": _clean_number(runtime["navigation_agent"]["max_step_height_m"] * 100),
            "max_slope_deg": runtime["navigation_agent"]["max_slope_deg"],
        },
        "npc_profiles": sorted(copy.deepcopy(runtime["npc_profiles"]), key=lambda item: item["npc_id"]),
        "remote_surface": runtime["remote_surface"],
    }

    event_plans = []
    for event in sorted(events, key=lambda item: item["event_id"]):
        public = public_event_payload(event)
        event_plans.append({
            "event_id": event["event_id"], "title": event["title"], "event_digest": event["content_digest"],
            "public_payload_digest": hashlib.sha256(canonical_json_bytes(public)).hexdigest(),
            "participating_room_ids": sorted(event["participating_room_ids"]),
            "participating_entity_ids": sorted(event["participating_entity_ids"]),
            "operations": [_resolve_operation(operation, rooms, assets) for operation in event["initial_operations"]],
            "public_goals": copy.deepcopy(event["public_goals"]),
            "triggers": copy.deepcopy(event["triggers"]),
            "success_conditions": copy.deepcopy(event["success_conditions"]),
            "failure_conditions": copy.deepcopy(event["failure_conditions"]),
            "timeout_s": event["timeout_s"],
            "reset_policy": copy.deepcopy(event["reset_policy"]),
            "source": copy.deepcopy(event["source"]),
        })

    room_bounds = [room["world_bounds_cm"] for room in resolved_rooms]
    nav_bounds = {
        "min_cm": [_clean_number(min(bounds["min_cm"][axis] for bounds in room_bounds)) for axis in range(3)],
        "max_cm": [_clean_number(max(bounds["max_cm"][axis] for bounds in room_bounds)) for axis in range(3)],
    }
    revision = house["revision"]
    plan = {
        "schema_version": BUILD_PLAN_SCHEMA_VERSION,
        "plan_id": f"{house['house_id']}@{revision}",
        "house": {"house_id": house["house_id"], "revision": revision, "content_digest": house["content_digest"]},
        "units": "centimeters",
        "assets": sorted(copy.deepcopy(house["asset_catalog"]), key=lambda item: item["asset_id"]),
        "rooms": resolved_rooms, "portals": resolved_portals, "entities": resolved_entities,
        "relations": sorted(copy.deepcopy(house["relations"]), key=lambda item: item["relation_id"]),
        "runtime_profile": resolved_runtime, "event_plans": event_plans,
        "unreal": {
            "content_namespace": f"/Game/VISTA/PlayableHome/{revision}",
            "map_path": f"/Game/VISTA/PlayableHome/{revision}/Maps/VistaPlayableHome",
            "composition_order": [
                "verify_inputs", "import_assets", "place_rooms", "place_entities",
                "configure_gameplay", "build_navigation", "save_reload_verify",
            ],
            "navigation_bounds_cm": nav_bounds,
            "room_graph_portal_ids": [portal["portal_id"] for portal in resolved_portals if portal["nav_policy"] != "blocked"],
            "stable_tag_prefix": "VistaSemanticId=",
        },
        "provenance": {
            "compiler_version": COMPILER_VERSION, "source_commit": house["provenance"]["source_commit"],
            "house_digest": house["content_digest"],
            "event_digests": [{"event_id": event["event_id"], "content_digest": event["content_digest"]} for event in sorted(events, key=lambda item: item["event_id"])],
        },
    }
    sealed = seal_document(plan)
    validate_build_plan(sealed)
    return sealed


def validate_build_plan(plan: Mapping[str, Any]) -> None:
    _validate_schema(plan, BUILD_PLAN_SCHEMA_VERSION)
    _scan_prohibited_keys(plan)
    _verify_digest(plan)
    _require_unique((asset["asset_id"] for asset in plan["assets"]), "$.assets", "asset")
    _require_unique((room["room_id"] for room in plan["rooms"]), "$.rooms", "room")
    _require_unique((portal["portal_id"] for portal in plan["portals"]), "$.portals", "portal")
    _require_unique((entity["entity_id"] for entity in plan["entities"]), "$.entities", "entity")
    _require_unique((event["event_id"] for event in plan["event_plans"]), "$.event_plans", "event")
    if plan["provenance"]["house_digest"] != plan["house"]["content_digest"]:
        _fail("VISTA_HOME_BUILD_PLAN_PROVENANCE_INVALID", "$.provenance.house_digest", "House digest binding disagrees")

    assets = _index(plan["assets"], "asset_id")
    rooms = _index(plan["rooms"], "room_id")
    entities = _index(plan["entities"], "entity_id")
    npcs = {profile["npc_id"]: profile for profile in plan["runtime_profile"]["npc_profiles"]}
    for index, asset in enumerate(plan["assets"]):
        _check_safe_uri(asset["uri"], f"$.assets[{index}].uri")
    for index, room in enumerate(plan["rooms"]):
        asset_id = room["bundle"]["asset_id"]
        if asset_id not in assets or room["bundle"] != assets[asset_id]:
            _fail("VISTA_HOME_BUILD_PLAN_ASSET_INVALID", f"$.rooms[{index}].bundle", "Room asset binding is absent or disagrees")
    for index, entity in enumerate(plan["entities"]):
        if entity["room_id"] not in rooms:
            _fail("VISTA_HOME_BUILD_PLAN_TARGET_UNKNOWN", f"$.entities[{index}].room_id", "Entity room is unknown")
        asset_id = entity["asset"]["asset_id"]
        if asset_id not in assets or entity["asset"] != assets[asset_id]:
            _fail("VISTA_HOME_BUILD_PLAN_ASSET_INVALID", f"$.entities[{index}].asset", "Entity asset binding is absent or disagrees")
    for index, portal in enumerate(plan["portals"]):
        if portal["from_room_id"] not in rooms or portal["to_room_id"] not in rooms:
            _fail("VISTA_HOME_BUILD_PLAN_TARGET_UNKNOWN", f"$.portals[{index}]", "Portal room is unknown")
        if portal["door_entity_id"] is not None and portal["door_entity_id"] not in entities:
            _fail("VISTA_HOME_BUILD_PLAN_TARGET_UNKNOWN", f"$.portals[{index}].door_entity_id", "Portal door is unknown")
    if plan["runtime_profile"]["player_start"]["room_id"] not in rooms:
        _fail("VISTA_HOME_BUILD_PLAN_TARGET_UNKNOWN", "$.runtime_profile.player_start.room_id", "PlayerStart room is unknown")
    for field in ("pawn", "game_mode"):
        binding = plan["runtime_profile"][field]
        if binding["asset_id"] not in assets or binding != assets[binding["asset_id"]]:
            _fail("VISTA_HOME_BUILD_PLAN_ASSET_INVALID", f"$.runtime_profile.{field}", "Runtime asset binding is absent or disagrees")

    event_digest_map = {entry["event_id"]: entry["content_digest"] for entry in plan["provenance"]["event_digests"]}
    _require_unique((entry["event_id"] for entry in plan["provenance"]["event_digests"]), "$.provenance.event_digests", "event digest")
    if set(event_digest_map) != {event["event_id"] for event in plan["event_plans"]}:
        _fail("VISTA_HOME_BUILD_PLAN_PROVENANCE_INVALID", "$.provenance.event_digests", "Event digest inventory disagrees")
    for index, event in enumerate(plan["event_plans"]):
        path = f"$.event_plans[{index}]"
        if event_digest_map[event["event_id"]] != event["event_digest"]:
            _fail("VISTA_HOME_BUILD_PLAN_PROVENANCE_INVALID", f"{path}.event_digest", "Event digest binding disagrees")
        if event["source"]["sample_id"] != event["event_id"]:
            _fail("VISTA_HOME_BUILD_PLAN_PROVENANCE_INVALID", f"{path}.source.sample_id", "Event source binding disagrees")
        participant_rooms = set(event["participating_room_ids"])
        participant_entities = set(event["participating_entity_ids"])
        if not participant_rooms.issubset(rooms):
            _fail("VISTA_HOME_BUILD_PLAN_TARGET_UNKNOWN", f"{path}.participating_room_ids", "Event room is unknown")
        spawned = {operation["entity_id"] for operation in event["operations"] if operation["op"] == "spawn_fixture"}
        known_entities = set(entities) | spawned
        if not participant_entities.issubset(known_entities):
            _fail("VISTA_HOME_BUILD_PLAN_TARGET_UNKNOWN", f"{path}.participating_entity_ids", "Event entity is unknown")
        _require_unique((operation["op_id"] for operation in event["operations"]), f"{path}.operations", "operation")
        goal_ids = {goal["goal_id"] for goal in event["public_goals"]}
        for operation_index, operation in enumerate(event["operations"]):
            operation_path = f"{path}.operations[{operation_index}]"
            if operation["op"] == "spawn_fixture":
                if operation["entity_id"] not in participant_entities or operation["room_id"] not in participant_rooms:
                    _fail("VISTA_HOME_BUILD_PLAN_TARGET_UNKNOWN", operation_path, "Spawn target is outside event participants")
                binding = operation["asset"]
                if binding["asset_id"] not in assets or binding != assets[binding["asset_id"]]:
                    _fail("VISTA_HOME_BUILD_PLAN_ASSET_INVALID", f"{operation_path}.asset", "Spawn asset binding disagrees")
            elif "target_id" in operation and operation["target_id"] not in participant_entities:
                _fail("VISTA_HOME_BUILD_PLAN_TARGET_UNKNOWN", f"{operation_path}.target_id", "Operation target is outside event participants")
            elif operation["op"] == "set_npc_queue" and operation["npc_id"] not in npcs:
                _fail("VISTA_HOME_BUILD_PLAN_TARGET_UNKNOWN", f"{operation_path}.npc_id", "NPC queue target is unknown")
            elif operation["op"] == "set_goal" and operation["goal_id"] not in goal_ids:
                _fail("VISTA_HOME_BUILD_PLAN_TARGET_UNKNOWN", f"{operation_path}.goal_id", "Event goal is unknown")
        for condition_index, condition in enumerate([
            *event["triggers"], *event["success_conditions"], *event["failure_conditions"]
        ]):
            condition_path = f"{path}.conditions[{condition_index}]"
            if "room_id" in condition and condition["room_id"] not in rooms:
                _fail("VISTA_HOME_BUILD_PLAN_TARGET_UNKNOWN", f"{condition_path}.room_id", "Condition room is unknown")
            if "target_id" in condition and condition["target_id"] not in participant_entities:
                _fail("VISTA_HOME_BUILD_PLAN_TARGET_UNKNOWN", f"{condition_path}.target_id", "Condition target is outside event participants")

    revision = plan["house"]["revision"]
    expected_namespace = f"/Game/VISTA/PlayableHome/{revision}"
    if plan["unreal"]["content_namespace"] != expected_namespace or not plan["unreal"]["map_path"].startswith(f"{expected_namespace}/"):
        _fail("VISTA_HOME_BUILD_PLAN_NAMESPACE_INVALID", "$.unreal", "Unreal namespace is not bound to the house revision")


def load_events(directory: os.PathLike[str] | str) -> list[dict[str, Any]]:
    root = pathlib.Path(directory)
    if not root.is_dir():
        _fail("VISTA_HOME_EVENT_DIRECTORY_INVALID", "$", "Event directory is missing")
    paths = sorted(path for path in root.iterdir() if path.is_file() and path.suffix == ".json")
    return [load_json(path) for path in paths]


def _write_json_atomic(path: pathlib.Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = canonical_json_bytes(value) + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate and compile VISTA Playable Home contracts")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate", help="validate house and every event")
    validate.add_argument("--house", required=True, type=pathlib.Path)
    validate.add_argument("--events-dir", required=True, type=pathlib.Path)
    compile_parser = subparsers.add_parser("compile", help="emit deterministic build plan")
    compile_parser.add_argument("--house", required=True, type=pathlib.Path)
    compile_parser.add_argument("--events-dir", required=True, type=pathlib.Path)
    compile_parser.add_argument("--output", type=pathlib.Path)
    public = subparsers.add_parser("public-event", help="print a sanitized public EventSpec")
    public.add_argument("--house", required=True, type=pathlib.Path)
    public.add_argument("--event", required=True, type=pathlib.Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        house = load_json(args.house)
        if args.command == "validate":
            events = load_events(args.events_dir)
            validate_house(house)
            for event in events:
                validate_event(event, house)
            result: Mapping[str, Any] = {
                "ok": True, "house_id": house["house_id"], "revision": house["revision"],
                "house_digest": house["content_digest"], "event_count": len(events),
            }
        elif args.command == "compile":
            result = compile_build_plan(house, load_events(args.events_dir))
            if args.output:
                _write_json_atomic(args.output, result)
                result = {"ok": True, "output": str(args.output), "content_digest": result["content_digest"]}
        else:
            event = load_json(args.event)
            validate_event(event, house)
            result = public_event_payload(event)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True))
        return 0
    except PlayableHomeContractError as exc:
        print(json.dumps({"ok": False, "error": {"code": exc.code, "path": exc.path, "message": exc.message}}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
