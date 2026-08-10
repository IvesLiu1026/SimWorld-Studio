#!/usr/bin/env python3
"""Start, inspect, or stop the disposable VISTA PIE world through fixed UE code.

This is a bounded acceptance helper, not a general Python execution client.  It
loads the owned runtime state, connects only to its loopback Unreal MCP port,
and sends one of three source-controlled scripts.  The resulting receipt is
written below the append-only runtime workspace without exposing Studio tokens.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from pathlib import Path
from typing import Any, Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from tools.runtime.vista_blender_world.runtime import (  # type: ignore
        LOOPBACK_HOST,
        SCHEMA,
        RuntimeSafetyError,
        atomic_write_json,
        identity_is_live,
        listener_pids_for_port,
        utc_now,
    )
else:
    from .runtime import (
        LOOPBACK_HOST,
        SCHEMA,
        RuntimeSafetyError,
        atomic_write_json,
        identity_is_live,
        listener_pids_for_port,
        utc_now,
    )


RECEIPT_SCHEMA = "vista-blender-world-live-qa/v1"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MCP_TYPE = "execute_python_script"
MARKER_PREFIX = "VISTA_BLENDER_WORLD_QA_V1"

STATE_SCRIPT = """import json
import unreal
level = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
editor = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
if level is None or editor is None:
    raise RuntimeError('required fixed editor subsystem is unavailable')
pie = bool(level.is_in_play_in_editor())
payload = {
    'action': 'state',
    'pie': pie,
    'possessed': False,
    'world': None,
    'pawn_class': None,
    'pawn_location_cm': None,
    'pawn_rotation_deg': None,
    'pawn_velocity_cm_s': None,
    'on_ground': None,
    'engine_time_s': None,
    'physics_prop': None,
    'floor': None,
}
if pie:
    world = editor.get_game_world()
    if world is None:
        raise RuntimeError('PIE game world is unavailable')
    pawn = unreal.GameplayStatics.get_player_pawn(world, 0)
    controller = unreal.GameplayStatics.get_player_controller(world, 0)
    if pawn is None or controller is None:
        raise RuntimeError('PIE player zero is not possessed')
    location = pawn.get_actor_location()
    rotation = pawn.get_actor_rotation()
    velocity = pawn.get_velocity()
    movement = pawn.get_component_by_class(unreal.CharacterMovementComponent)
    if movement is None:
        raise RuntimeError('PIE pawn has no CharacterMovementComponent')
    payload.update({
        'possessed': True,
        'world': str(world.get_name()),
        'pawn_class': str(pawn.get_class().get_path_name()),
        'pawn_location_cm': [float(location.x), float(location.y), float(location.z)],
        'pawn_rotation_deg': [float(rotation.pitch), float(rotation.yaw), float(rotation.roll)],
        'pawn_velocity_cm_s': [float(velocity.x), float(velocity.y), float(velocity.z)],
        'on_ground': bool(movement.is_moving_on_ground()),
        'engine_time_s': float(unreal.GameplayStatics.get_time_seconds(world)),
    })
    boxes = []
    for actor in unreal.GameplayStatics.get_all_actors_of_class(world, unreal.StaticMeshActor):
        try:
            label = str(actor.get_actor_label())
        except Exception:
            label = str(actor.get_name())
        if label != 'VISTA_high_cardboard_box':
            continue
        component = actor.get_component_by_class(unreal.StaticMeshComponent)
        box_location = actor.get_actor_location()
        boxes.append({
            'label': label,
            'location_cm': [float(box_location.x), float(box_location.y), float(box_location.z)],
            'mobility': str(component.get_editor_property('mobility')) if component is not None else None,
            'simulate_physics': bool(component.is_simulating_physics()) if component is not None else False,
        })
    if len(boxes) != 1:
        raise RuntimeError('expected exactly one VISTA physics prop in PIE')
    payload['physics_prop'] = boxes[0]
    floors = []
    for actor in unreal.GameplayStatics.get_all_actors_of_class(world, unreal.StaticMeshActor):
        try:
            label = str(actor.get_actor_label())
        except Exception:
            label = str(actor.get_name())
        if label != 'VISTA_Generated_VISTA_Room_FloorSlab':
            continue
        component = actor.get_component_by_class(unreal.StaticMeshComponent)
        origin, extent = actor.get_actor_bounds(False)
        floors.append({
            'label': label,
            'origin_cm': [float(origin.x), float(origin.y), float(origin.z)],
            'extent_cm': [float(extent.x), float(extent.y), float(extent.z)],
            'collision_enabled': str(component.get_collision_enabled()) if component is not None else None,
        })
    if len(floors) != 1:
        raise RuntimeError('expected exactly one generated floor slab in PIE')
    payload['floor'] = floors[0]
print('VISTA_BLENDER_WORLD_QA_V1:' + json.dumps(payload, separators=(',', ':'), allow_nan=False))
"""

STOP_SCRIPT = """import json
import unreal
level = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
if level is None or not hasattr(level, 'editor_request_end_play'):
    raise RuntimeError('fixed end-PIE API is unavailable')
was_playing = bool(level.is_in_play_in_editor())
if was_playing:
    level.editor_request_end_play()
payload = {'action': 'stop', 'requested': True, 'was_playing': was_playing}
print('VISTA_BLENDER_WORLD_QA_V1:' + json.dumps(payload, separators=(',', ':'), allow_nan=False))
"""

START_DIAGNOSTIC_SCRIPT = """import json
import unreal
editor = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
actors = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
if editor is None or actors is None:
    raise RuntimeError('required fixed editor subsystem is unavailable')
world = editor.get_editor_world()
if world is None:
    raise RuntimeError('editor world is unavailable')
pawn_class = unreal.load_class(None, '/Game/Human_Avatar/DefaultCharacter/ThirdPerson/Blueprints/BP_ThirdPersonCharacter.BP_ThirdPersonCharacter_C')
if pawn_class is None:
    raise RuntimeError('fixed VISTA pawn class is unavailable')
pawn_default = unreal.get_default_object(pawn_class)
capsule = pawn_default.get_component_by_class(unreal.CapsuleComponent)
if capsule is None:
    raise RuntimeError('fixed VISTA pawn capsule is unavailable')
radius = float(capsule.get_unscaled_capsule_radius())
half_height = float(capsule.get_unscaled_capsule_half_height())
candidates = [
    [110.0, -180.0, 100.0],
    [150.0, -150.0, 100.0],
    [180.0, -120.0, 100.0],
    [220.0, -100.0, 100.0],
    [260.0, -80.0, 100.0],
    [300.0, -80.0, 100.0],
]
static_actors = []
for actor in actors.get_all_level_actors():
    component = actor.get_component_by_class(unreal.StaticMeshComponent)
    if component is None:
        continue
    origin, extent = actor.get_actor_bounds(False)
    try:
        label = str(actor.get_actor_label())
    except Exception:
        label = str(actor.get_name())
    static_actors.append((label, origin, extent, str(component.get_collision_enabled())))
results = []
for candidate in candidates:
    x, y, z = candidate
    overlaps = []
    for label, origin, extent, collision in static_actors:
        if (
            origin.x + extent.x >= x - radius
            and origin.x - extent.x <= x + radius
            and origin.y + extent.y >= y - radius
            and origin.y - extent.y <= y + radius
            and origin.z + extent.z >= z - half_height
            and origin.z - extent.z <= z + half_height
        ):
            overlaps.append({'label': label, 'collision_enabled': collision})
    results.append({'location_cm': candidate, 'aabb_overlaps': sorted(overlaps, key=lambda item: item['label'])})
payload = {
    'action': 'diagnose-start',
    'pie': False,
    'world': str(world.get_name()),
    'capsule_radius_cm': radius,
    'capsule_half_height_cm': half_height,
    'candidates': results,
}
print('VISTA_BLENDER_WORLD_QA_V1:' + json.dumps(payload, separators=(',', ':'), allow_nan=False))
"""

STAGE_START_CANDIDATE_SCRIPT = """import json
import unreal
level = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
editor = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
actors = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
if level is None or editor is None or actors is None:
    raise RuntimeError('required fixed editor subsystem is unavailable')
if bool(level.is_in_play_in_editor()):
    raise RuntimeError('refusing to move PlayerStart while PIE is active')
world = editor.get_editor_world()
if world is None or str(world.get_name()) != 'MMG040_Office_BlenderR1':
    raise RuntimeError('unexpected editor world for fixed PlayerStart candidate')
starts = [actor for actor in actors.get_all_level_actors() if str(actor.get_actor_label()) == 'VISTA_PlayerStart']
if len(starts) != 1 or not isinstance(starts[0], unreal.PlayerStart):
    raise RuntimeError('expected exactly one fixed VISTA PlayerStart')
start = starts[0]
start.set_actor_location(unreal.Vector(x=150.0, y=-150.0, z=100.0), False, False)
start.set_actor_rotation(unreal.Rotator(pitch=0.0, yaw=-75.0, roll=0.0), False)
location = start.get_actor_location()
rotation = start.get_actor_rotation()
payload = {
    'action': 'stage-start-candidate',
    'pie': False,
    'world': str(world.get_name()),
    'saved': False,
    'location_cm': [float(location.x), float(location.y), float(location.z)],
    'rotation_deg': [float(rotation.pitch), float(rotation.yaw), float(rotation.roll)],
}
print('VISTA_BLENDER_WORLD_QA_V1:' + json.dumps(payload, separators=(',', ':'), allow_nan=False))
"""

FIXED_SCRIPTS = {
    "state": STATE_SCRIPT,
    "verify": STATE_SCRIPT,
    "stop": STOP_SCRIPT,
    "diagnose-start": START_DIAGNOSTIC_SCRIPT,
    "stage-start-candidate": STAGE_START_CANDIDATE_SCRIPT,
}
EXPECTED_WORLD = "MMG040_Office_BlenderR1"
EXPECTED_PLAYER_START_XY_CM = (150.0, -150.0)
PLAYER_START_XY_TOLERANCE_CM = 1.0
EXPECTED_PAWN_CLASS = (
    "/Game/Human_Avatar/DefaultCharacter/ThirdPerson/Blueprints/"
    "BP_ThirdPersonCharacter.BP_ThirdPersonCharacter_C"
)


def _plain_object(value: Any) -> bool:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def parse_marker(reply: Mapping[str, Any]) -> dict[str, Any]:
    result = reply.get("result")
    logs = result.get("python_logs") if _plain_object(result) else None
    if not isinstance(logs, list) or not all(isinstance(line, str) for line in logs):
        raise RuntimeSafetyError("UE response does not contain fixed Python logs")
    prefix = f"{MARKER_PREFIX}:"
    matches = [line.split(prefix, 1)[1] for line in logs if prefix in line]
    if len(matches) != 1:
        raise RuntimeSafetyError("UE response must contain exactly one fixed QA marker")
    try:
        payload = json.loads(matches[0])
    except json.JSONDecodeError as exc:
        raise RuntimeSafetyError("UE fixed QA marker is malformed") from exc
    if not _plain_object(payload):
        raise RuntimeSafetyError("UE fixed QA marker must contain an object")
    return payload


def send_fixed_script(port: int, script: str, timeout_seconds: float = 20.0) -> dict[str, Any]:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise RuntimeSafetyError("owned UE MCP port is invalid")
    if script not in FIXED_SCRIPTS.values():
        raise RuntimeSafetyError("refusing non-fixed Unreal Python source")
    request = json.dumps(
        {"type": MCP_TYPE, "params": {"script": script}},
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    chunks: list[bytes] = []
    total = 0
    try:
        with socket.create_connection((LOOPBACK_HOST, port), timeout=timeout_seconds) as connection:
            connection.settimeout(timeout_seconds)
            connection.sendall(request)
            while True:
                block = connection.recv(65536)
                if not block:
                    break
                total += len(block)
                if total > MAX_RESPONSE_BYTES:
                    raise RuntimeSafetyError("UE response exceeded the fixed QA byte limit")
                chunks.append(block)
                try:
                    reply = json.loads(b"".join(chunks))
                except json.JSONDecodeError:
                    continue
                if not _plain_object(reply):
                    raise RuntimeSafetyError("UE response must contain one JSON object")
                return parse_marker(reply)
    except (OSError, TimeoutError) as exc:
        raise RuntimeSafetyError("owned loopback UE MCP request failed") from exc
    raise RuntimeSafetyError("owned loopback UE MCP closed without a complete response")


def load_owned_runtime(workspace: Path) -> tuple[Path, dict[str, Any], int]:
    try:
        root = workspace.expanduser().resolve(strict=True)
        state_path = (root / "runtime-state.json").resolve(strict=True)
        state_path.relative_to(root)
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeSafetyError("runtime workspace/state is unavailable") from exc
    if not _plain_object(state) or state.get("schema") != SCHEMA or state.get("status") != "ready":
        raise RuntimeSafetyError("runtime state is not a ready owned VISTA Blender world")
    launcher = state.get("launcher")
    if not _plain_object(launcher) or not identity_is_live(launcher):
        raise RuntimeSafetyError("recorded runtime launcher identity is no longer live")
    ports = state.get("ports")
    port = ports.get("ue_mcp") if _plain_object(ports) else None
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise RuntimeSafetyError("runtime state has no valid owned UE MCP port")
    processes = state.get("processes")
    if not isinstance(processes, list):
        raise RuntimeSafetyError("runtime state has no owned child process records")
    ue_identities = [
        item for item in processes if _plain_object(item) and item.get("role") == "ue"
    ]
    if len(ue_identities) != 1 or not identity_is_live(ue_identities[0]):
        raise RuntimeSafetyError("recorded owned UE process identity is not live")
    owner_pids = listener_pids_for_port(port)
    if owner_pids != {int(ue_identities[0]["pid"])}:
        raise RuntimeSafetyError("owned UE MCP port listener identity does not match runtime state")
    return root, state, port


def wait_for_pie_state(port: int, expected: bool, deadline_seconds: float) -> dict[str, Any]:
    deadline = time.monotonic() + deadline_seconds
    last_error: RuntimeSafetyError | None = None
    while time.monotonic() <= deadline:
        try:
            payload = send_fixed_script(port, STATE_SCRIPT)
            if payload.get("action") == "state" and payload.get("pie") is expected:
                if not expected or payload.get("possessed") is True:
                    return payload
        except RuntimeSafetyError as exc:
            last_error = exc
        time.sleep(0.5)
    if last_error:
        raise RuntimeSafetyError("timed out confirming PIE transition") from last_error
    raise RuntimeSafetyError("timed out confirming PIE transition")


def _finite_triple(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 3
        and all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in value)
    )


def live_acceptance_passed(payload: Mapping[str, Any]) -> bool:
    pawn_location = payload.get("pawn_location_cm")
    physics = payload.get("physics_prop")
    floor = payload.get("floor")
    if not (
        payload.get("action") == "state"
        and payload.get("pie") is True
        and payload.get("possessed") is True
        and payload.get("world") == EXPECTED_WORLD
        and payload.get("pawn_class") == EXPECTED_PAWN_CLASS
        and payload.get("on_ground") is True
        and _finite_triple(pawn_location)
        and _plain_object(physics)
        and _finite_triple(physics.get("location_cm"))
        and physics.get("simulate_physics") is True
        and _plain_object(floor)
        and _finite_triple(floor.get("origin_cm"))
        and _finite_triple(floor.get("extent_cm"))
    ):
        return False
    pawn_x = float(pawn_location[0])
    pawn_y = float(pawn_location[1])
    pawn_z = float(pawn_location[2])
    prop_z = float(physics["location_cm"][2])
    floor_extent = floor["extent_cm"]
    collision = str(floor.get("collision_enabled") or "").casefold()
    return (
        abs(pawn_x - EXPECTED_PLAYER_START_XY_CM[0]) <= PLAYER_START_XY_TOLERANCE_CM
        and abs(pawn_y - EXPECTED_PLAYER_START_XY_CM[1]) <= PLAYER_START_XY_TOLERANCE_CM
        and -10.0 <= pawn_z <= 500.0
        and -10.0 <= prop_z <= 500.0
        and float(floor_extent[0]) >= 250.0
        and float(floor_extent[1]) >= 240.0
        and "nocollision" not in collision.replace("_", "")
    )


def wait_for_live_acceptance(port: int, deadline_seconds: float) -> dict[str, Any]:
    deadline = time.monotonic() + deadline_seconds
    latest: dict[str, Any] | None = None
    while time.monotonic() <= deadline:
        latest = send_fixed_script(port, STATE_SCRIPT)
        if live_acceptance_passed(latest):
            return latest
        time.sleep(0.5)
    detail = "no state" if latest is None else json.dumps(latest, sort_keys=True)[:512]
    raise RuntimeSafetyError(f"live grounding/physics acceptance failed: {detail}")


def run_action(workspace: Path, action: str, deadline_seconds: float) -> dict[str, Any]:
    root, state, port = load_owned_runtime(workspace)
    command: dict[str, Any] | None = None
    observed: dict[str, Any] | None = None
    failure: RuntimeSafetyError | None = None
    try:
        command = send_fixed_script(port, FIXED_SCRIPTS[action])
        observed = command
        if action == "verify":
            observed = wait_for_live_acceptance(port, deadline_seconds)
        elif action == "stop":
            observed = wait_for_pie_state(port, False, deadline_seconds)
    except RuntimeSafetyError as error:
        failure = error
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "created_at": utc_now(),
        "action": action,
        "runtime_created_at": state.get("created_at"),
        "runtime_ready_at": state.get("ready_at"),
        "map": state.get("map"),
        "port_role": "ue_mcp",
        "command": command,
        "observed": observed,
        "acceptance": (
            "failed" if failure is not None else "passed" if action == "verify" else "observed"
        ),
        "error": str(failure)[:512] if failure is not None else None,
    }
    timestamp = receipt["created_at"].replace(":", "").replace("+", "_")
    receipt_path = root / "receipts" / f"live-qa-{action}-{timestamp}.json"
    atomic_write_json(receipt_path, receipt)
    if failure is not None:
        raise RuntimeSafetyError(
            f"{failure}; failure receipt: {receipt_path.name}"
        ) from failure
    return {**receipt, "receipt_path": str(receipt_path)}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Control only the owned disposable VISTA PIE world")
    result.add_argument("action", choices=tuple(FIXED_SCRIPTS))
    result.add_argument("--workspace", required=True, type=Path)
    result.add_argument("--deadline-seconds", type=float, default=30.0)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not 1 <= args.deadline_seconds <= 120:
        raise RuntimeSafetyError("deadline must be from 1 through 120 seconds")
    result = run_action(args.workspace, args.action, args.deadline_seconds)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeSafetyError as error:
        print(f"live QA refused: {error}", file=sys.stderr)
        raise SystemExit(2)
