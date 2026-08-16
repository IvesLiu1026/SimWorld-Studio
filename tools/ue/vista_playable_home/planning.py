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
VISUAL_PROFILE_SCHEMA = "simworld.vista.playable-home-visual-profile/v1"
R2_REVIEW_CAMERA_TAG = "VistaVisualRevision=realistic_interior_r2"
MIN_REVIEW_CLEARANCE_CM = 25.0
MAX_REVIEW_CLEARANCE_CM = 500.0
PRESENTATION_ROOM_KINDS = ("entry_hall", "living_room", "kitchen_dining")
PRESENTATION_ARTIFACT_KIND = "ue_import_bundle"
PRESENTATION_ROOT_TRANSFORM_POLICY = "room_local_geometry_identity_root"
PRESENTATION_SEMANTIC_POLICY = "presentation_only_preserve_r1_authority"
PRESENTATION_COLLISION_POLICY = "presentation_no_collision_use_hidden_r1_proxies"
PRESENTATION_UNREAL_COLLISION_PROFILE = "NoCollision"
PRESENTATION_EXECUTION_BINDING_KEYS = frozenset({
    "artifact_id",
    "artifact_kind",
    "target_asset_id",
    "room_id",
    "room_kind",
    "relative_path",
    "source_file",
    "source_file_sha256",
    "media_type",
    "sha256",
    "size_bytes",
    "mesh_count",
    "material_count",
    "pbr_complete_material_count",
    "texture_count",
    "material_ids",
    "expected_world_transform_cm",
    "bundle_root_transform",
    "root_transform_policy",
    "semantic_policy",
    "collision_policy",
    "unreal_collision_profile",
    "cameras_exported",
    "lights_exported",
    "source_hashes",
})


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


def _finite_number(value: Any, label: str, *, minimum: float | None = None,
                   maximum: float | None = None) -> float:
    _require(isinstance(value, (int, float)) and not isinstance(value, bool) and
             math.isfinite(float(value)), "VISTA_HOME_VISUAL_NUMBER_INVALID",
             f"{label} must be finite")
    result = float(value)
    _require(minimum is None or result >= minimum,
             "VISTA_HOME_VISUAL_NUMBER_INVALID", f"{label} is below its minimum")
    _require(maximum is None or result <= maximum,
             "VISTA_HOME_VISUAL_NUMBER_INVALID", f"{label} is above its maximum")
    return result


def _v3(value: Any, label: str) -> list[float]:
    _require(isinstance(value, list) and len(value) == 3 and
             all(isinstance(number, (int, float)) and not isinstance(number, bool) and
                 math.isfinite(float(number)) for number in value),
             "VISTA_HOME_VISUAL_VECTOR_INVALID", f"{label} must be a finite xyz vector")
    return [float(number) for number in value]


def look_at_rotation_deg(eye_location_cm: Any, look_at_target_cm: Any) -> list[float]:
    """Return HouseSpec XYZ Euler values for an Unreal camera look direction.

    HouseSpec XYZ rotations map to Unreal roll, pitch, yaw.  A camera looks
    along Unreal +X, so yaw is the azimuth in XY and pitch is the elevation.
    Roll is intentionally authored as exactly zero; this is the contract that
    prevents the historic ``[-10, 0, yaw]`` value becoming a ten-degree roll.
    """

    eye = _v3(eye_location_cm, "review shot eye_location_cm")
    target = _v3(look_at_target_cm, "review shot look_at_target_cm")
    direction = [target[index] - eye[index] for index in range(3)]
    horizontal = math.hypot(direction[0], direction[1])
    distance = math.sqrt(sum(component * component for component in direction))
    _require(distance > 1e-4, "VISTA_HOME_REVIEW_LOOK_AT_INVALID",
             "review shot eye and look-at target must differ")
    yaw = math.degrees(math.atan2(direction[1], direction[0]))
    pitch = math.degrees(math.atan2(direction[2], horizontal))
    result = [0.0, pitch, yaw]
    _require(all(math.isfinite(value) for value in result) and result[0] == 0.0,
             "VISTA_HOME_REVIEW_LOOK_AT_INVALID", "derived review rotation is invalid")
    return result


def _point_in_bounds(point: Sequence[float], bounds: Mapping[str, Any]) -> bool:
    value = _bounds(bounds, "review eye bounds")
    return all(low <= coordinate <= high for coordinate, low, high in
               zip(point, value["min_cm"], value["max_cm"], strict=True))


def _point_aabb_clearance_cm(point: Sequence[float], bounds: Mapping[str, Any]) -> float:
    value = _bounds(bounds, "review blocking bounds")
    squared = 0.0
    for coordinate, low, high in zip(point, value["min_cm"], value["max_cm"], strict=True):
        delta = low - coordinate if coordinate < low else coordinate - high if coordinate > high else 0.0
        squared += delta * delta
    return math.sqrt(squared)


def compile_look_at_review_shot(
    shot: Mapping[str, Any],
    *,
    room_bounds: Mapping[str, Any] | None = None,
    approved_doorway_bounds: Sequence[Mapping[str, Any]] = (),
    blocking_bounds: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Validate and compile one r2 ReviewShot into a CameraActor operation.

    Bounds passed by callers are deterministic preflight hooks.  Their absence
    is recorded as requiring Unreal runtime observation; it is never reported
    as clearance proof.
    """

    value = dict(_mapping(shot, "review shot"))
    _require(not {"rotation_deg", "world_transform_cm", "transform"} & set(value),
             "VISTA_HOME_REVIEW_CALLER_EULER_REFUSED",
             "r2 review shots cannot carry caller-authored Euler transforms")
    shot_id = _safe_id(value.get("shot_id"), "review shot shot_id")
    room_id = _safe_id(value.get("room_id"), "review shot room_id")
    purpose = value.get("purpose")
    _require(purpose in {"overview", "hero"},
             "VISTA_HOME_REVIEW_SHOT_INVALID", f"review shot {shot_id} purpose is invalid")
    eye = _v3(value.get("eye_location_cm"), f"review shot {shot_id}.eye_location_cm")
    target = _v3(value.get("look_at_target_cm"),
                 f"review shot {shot_id}.look_at_target_cm")
    fov = _finite_number(value.get("horizontal_fov_deg"),
                         f"review shot {shot_id}.horizontal_fov_deg",
                         minimum=5.0, maximum=170.0)
    clearance = _finite_number(value.get("near_field_clearance_cm"),
                               f"review shot {shot_id}.near_field_clearance_cm",
                               minimum=MIN_REVIEW_CLEARANCE_CM,
                               maximum=MAX_REVIEW_CLEARANCE_CM)
    rotation_deg = look_at_rotation_deg(eye, target)

    containment_status = "runtime_observation_required"
    if room_bounds is not None or approved_doorway_bounds:
        contained = room_bounds is not None and _point_in_bounds(eye, room_bounds)
        contained = contained or any(_point_in_bounds(eye, bounds)
                                     for bounds in approved_doorway_bounds)
        _require(contained, "VISTA_HOME_REVIEW_EYE_OUTSIDE_ALLOWED_BOUNDS",
                 f"review shot {shot_id} eye is outside its room and approved doorways")
        containment_status = "preflight_passed"

    nearest_clearance: float | None = None
    nearest_blocker: str | None = None
    for index, raw_blocker in enumerate(blocking_bounds):
        blocker = _mapping(raw_blocker, f"review blocking bounds[{index}]")
        if blocker.get("translucent") is True:
            continue
        blocker_id = _safe_id(blocker.get("semantic_id"),
                              f"review blocking bounds[{index}].semantic_id")
        measured = _point_aabb_clearance_cm(eye, blocker.get("bounds"))
        if nearest_clearance is None or measured < nearest_clearance:
            nearest_clearance = measured
            nearest_blocker = blocker_id
    if nearest_clearance is not None:
        _require(nearest_clearance >= clearance,
                 "VISTA_HOME_REVIEW_NEAR_FIELD_BLOCKED",
                 f"review shot {shot_id} is {nearest_clearance:.3f} cm from {nearest_blocker}, "
                 f"below the {clearance:.3f} cm clearance")
        clearance_status = "preflight_passed"
    else:
        clearance_status = "runtime_observation_required"

    exposure = dict(_mapping(value.get("exposure"), f"review shot {shot_id}.exposure"))
    _require(exposure.get("mode") == "pinned_physical_camera",
             "VISTA_HOME_REVIEW_EXPOSURE_INVALID",
             f"review shot {shot_id} exposure must be pinned physical camera")
    normalized_exposure = {
        "mode": "pinned_physical_camera",
        "iso": _finite_number(exposure.get("iso"), "review exposure iso", minimum=1.0, maximum=102400.0),
        "shutter_speed_s": _finite_number(exposure.get("shutter_speed_s"),
                                          "review exposure shutter_speed_s",
                                          minimum=1.0 / 32000.0, maximum=60.0),
        "aperture_fstop": _finite_number(exposure.get("aperture_fstop"),
                                         "review exposure aperture_fstop",
                                         minimum=0.5, maximum=64.0),
        "exposure_compensation_ev": _finite_number(
            exposure.get("exposure_compensation_ev", 0.0),
            "review exposure compensation", minimum=-16.0, maximum=16.0),
    }
    expected_hero_ids = list(value.get("expected_hero_ids", []))
    forbidden_foreground_ids = list(value.get("forbidden_foreground_ids", []))
    for label, identifiers in (("expected_hero_ids", expected_hero_ids),
                               ("forbidden_foreground_ids", forbidden_foreground_ids)):
        _require(all(isinstance(item, str) and SAFE_ID.fullmatch(item) is not None
                     for item in identifiers) and len(identifiers) == len(set(identifiers)),
                 "VISTA_HOME_REVIEW_SHOT_INVALID", f"review shot {shot_id} {label} is invalid")
    _require(purpose != "overview" or len(expected_hero_ids) >= 3,
             "VISTA_HOME_REVIEW_SHOT_INVALID",
             f"overview shot {shot_id} must declare at least three room-defining heroes")
    layers = list(value.get("allowed_visibility_layers", []))
    _require(layers and all(isinstance(item, str) and SAFE_ID.fullmatch(item) is not None
                            for item in layers) and len(layers) == len(set(layers)),
             "VISTA_HOME_REVIEW_SHOT_INVALID",
             f"review shot {shot_id} allowed visibility layers are invalid")

    semantic_id = f"{room_id}/camera.{shot_id}"
    return _operation("place_rooms", "place_review_camera", {
        "semantic_id": semantic_id,
        "room_id": room_id,
        "review_shot_id": shot_id,
        "purpose": purpose,
        "eye_location_cm": eye,
        "look_at_target_cm": target,
        "transform": {
            "location_cm": eye,
            "rotation_deg": rotation_deg,
            "scale": [1.0, 1.0, 1.0],
        },
        "fov_deg": fov,
        "near_field_clearance_cm": clearance,
        "exposure": normalized_exposure,
        "allowed_visibility_layers": sorted(layers),
        "expected_hero_ids": sorted(expected_hero_ids),
        "forbidden_foreground_ids": sorted(forbidden_foreground_ids),
        "preflight": {
            "eye_containment": containment_status,
            "near_field_clearance": clearance_status,
            "nearest_blocker_id": nearest_blocker,
            "nearest_blocker_clearance_cm": nearest_clearance,
            "runtime_observation_required": (
                containment_status != "preflight_passed" or
                clearance_status != "preflight_passed"
            ),
        },
        "tags": [TAG_PREFIX + semantic_id, "VistaRoom=" + room_id,
                 R2_REVIEW_CAMERA_TAG, "VistaReviewShot=" + shot_id],
    })


def compile_realistic_review_operations(
    visual_profile: Mapping[str, Any],
    *,
    room_bounds_by_id: Mapping[str, Mapping[str, Any]] | None = None,
    approved_doorway_bounds_by_room: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    blocking_bounds: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Compile a deterministic, unique r2 look-at review-shot set."""

    profile = _mapping(visual_profile, "visual profile")
    _require(profile.get("schema_version") == VISUAL_PROFILE_SCHEMA,
             "VISTA_HOME_VISUAL_PROFILE_INVALID", "visual profile schema differs")
    shots = _array(profile.get("review_shots"), "visual profile review_shots")
    operations: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw_shot in enumerate(shots):
        shot = _mapping(raw_shot, f"review_shots[{index}]")
        shot_id = shot.get("shot_id")
        _require(isinstance(shot_id, str) and shot_id not in seen,
                 "VISTA_HOME_REVIEW_SHOT_INVALID", "review shot IDs must be unique")
        seen.add(shot_id)
        room_id = shot.get("room_id")
        operations.append(compile_look_at_review_shot(
            shot,
            room_bounds=(room_bounds_by_id or {}).get(room_id),
            approved_doorway_bounds=(approved_doorway_bounds_by_room or {}).get(room_id, ()),
            blocking_bounds=blocking_bounds,
        ))
    _require(operations, "VISTA_HOME_REVIEW_SHOT_INVALID", "r2 needs at least one review shot")
    return operations


def _unit_direction(value: Any, label: str) -> list[float]:
    direction = _v3(value, label)
    length = math.sqrt(sum(component * component for component in direction))
    _require(length > 1e-6, "VISTA_HOME_LIGHT_DIRECTION_INVALID",
             f"{label} cannot be zero")
    return [component / length for component in direction]


def compile_realistic_lighting_operation(
    lighting_rig: Mapping[str, Any],
    *,
    room_ids: set[str],
) -> dict[str, Any]:
    """Compile the additive physical-lighting request for the r2 composer."""

    value = _mapping(lighting_rig, "lighting rig")
    rig_id = _safe_id(value.get("rig_id"), "lighting rig rig_id")
    _require(value.get("profile") == "neutral_day",
             "VISTA_HOME_LIGHTING_RIG_INVALID", "lighting profile must be neutral_day")
    sun = _mapping(value.get("sun"), "lighting rig sun")
    sun_direction = _unit_direction(sun.get("direction"), "lighting rig sun.direction")
    normalized_sun = {
        "direction": sun_direction,
        "rotation_deg": look_at_rotation_deg([0.0, 0.0, 0.0], sun_direction),
        "illuminance_lux": _finite_number(sun.get("illuminance_lux"),
                                          "lighting rig sun.illuminance_lux",
                                          minimum=0.01, maximum=200000.0),
        "temperature_k": _finite_number(sun.get("temperature_k"),
                                         "lighting rig sun.temperature_k",
                                         minimum=1000.0, maximum=20000.0),
    }
    sky = _mapping(value.get("sky"), "lighting rig sky")
    source = _safe_id(sky.get("source"), "lighting rig sky.source")
    _require(source == "real_time_capture", "VISTA_HOME_LIGHTING_RIG_INVALID",
             "lighting sky source must be real_time_capture")
    normalized_sky = {
        "source": source,
        "sky_intensity": _finite_number(sky.get("sky_intensity"),
                                        "lighting rig sky.sky_intensity",
                                        minimum=0.0, maximum=100.0),
    }

    apertures: list[dict[str, Any]] = []
    seen_apertures: set[str] = set()
    for index, raw in enumerate(_array(value.get("apertures"), "lighting rig apertures")):
        aperture = _mapping(raw, f"lighting rig apertures[{index}]")
        aperture_id = _safe_id(aperture.get("aperture_id"), "lighting aperture ID")
        room_id = _safe_id(aperture.get("room_id"), "lighting aperture room ID")
        _require(aperture_id not in seen_apertures and room_id in room_ids and
                 aperture.get("visible_geometry_required") is True,
                 "VISTA_HOME_LIGHTING_RIG_INVALID", "lighting aperture is invalid")
        seen_apertures.add(aperture_id)
        apertures.append({
            "aperture_id": aperture_id,
            "room_id": room_id,
            "visible_geometry_required": True,
        })

    practicals: list[dict[str, Any]] = []
    seen_lights: set[str] = set()
    for index, raw in enumerate(_array(value.get("practical_lights"),
                                       "lighting rig practical_lights")):
        light = _mapping(raw, f"lighting rig practical_lights[{index}]")
        light_id = _safe_id(light.get("light_id"), "practical light ID")
        room_id = _safe_id(light.get("room_id"), "practical light room ID")
        light_type = light.get("type")
        unit = light.get("unit")
        _require(light_id not in seen_lights and room_id in room_ids and
                 light_type in {"rect", "spot"} and unit in {"lumens", "candelas"},
                 "VISTA_HOME_LIGHTING_RIG_INVALID",
                 "practical light must be a unique room-bound rect/spot light")
        seen_lights.add(light_id)
        direction = _unit_direction(light.get("direction"),
                                    f"practical light {light_id}.direction")
        fixture_id = _safe_id(light.get("visible_fixture_id"),
                              f"practical light {light_id}.visible_fixture_id")
        practicals.append({
            "light_id": light_id,
            "room_id": room_id,
            "type": light_type,
            "location_cm": _v3(light.get("location_cm"),
                               f"practical light {light_id}.location_cm"),
            "direction": direction,
            "rotation_deg": look_at_rotation_deg([0.0, 0.0, 0.0], direction),
            "intensity": _finite_number(light.get("intensity"),
                                        f"practical light {light_id}.intensity",
                                        minimum=0.01, maximum=1e7),
            "unit": unit,
            "temperature_k": _finite_number(light.get("temperature_k"),
                                             f"practical light {light_id}.temperature_k",
                                             minimum=1000.0, maximum=20000.0),
            "visible_fixture_id": fixture_id,
            "tags": [TAG_PREFIX + light_id, "VistaRoom=" + room_id,
                     "VistaRole=lighting", R2_REVIEW_CAMERA_TAG],
        })
    _require(practicals, "VISTA_HOME_LIGHTING_RIG_INVALID",
             "r2 lighting needs at least one visible-fixture practical light")

    exposure = _mapping(value.get("gameplay_exposure"), "lighting rig gameplay_exposure")
    _require(exposure.get("metering_mode") == "histogram",
             "VISTA_HOME_LIGHTING_RIG_INVALID", "gameplay exposure must use histogram metering")
    min_ev100 = _finite_number(exposure.get("min_ev100"), "gameplay exposure min_ev100",
                               minimum=-16.0, maximum=32.0)
    max_ev100 = _finite_number(exposure.get("max_ev100"), "gameplay exposure max_ev100",
                               minimum=-16.0, maximum=32.0)
    _require(min_ev100 < max_ev100, "VISTA_HOME_LIGHTING_RIG_INVALID",
             "gameplay exposure EV100 range is empty")
    normalized_exposure = {
        "metering_mode": "histogram",
        "min_ev100": min_ev100,
        "max_ev100": max_ev100,
        "speed_up": _finite_number(exposure.get("speed_up"), "gameplay exposure speed_up",
                                   minimum=0.01, maximum=20.0),
        "speed_down": _finite_number(exposure.get("speed_down"), "gameplay exposure speed_down",
                                     minimum=0.01, maximum=20.0),
    }
    return _operation("configure_gameplay", "place_realistic_lighting", {
        "rig_id": rig_id,
        "profile": "neutral_day",
        "light_mobility": "movable",
        "sun": normalized_sun,
        "sky": normalized_sky,
        "apertures": sorted(apertures, key=lambda item: item["aperture_id"]),
        "practical_lights": sorted(practicals, key=lambda item: item["light_id"]),
        "gameplay_exposure": normalized_exposure,
        "runtime_observation_required": True,
    })


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


def compile_presentation_bundle_operations(
    visual_profile: Mapping[str, Any],
    *,
    rooms_by_id: Mapping[str, Mapping[str, Any]],
    presentation_bindings: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Compile the three validated room-local GLBs into presentation actors.

    Host-private source paths stay in the execution binding.  The composition
    operation carries only the pinned artifact identity, source digest,
    presentation policy, and world transform needed by the fixed commandlet.
    It intentionally has no ``semantic_id``: r1 room actors remain the sole
    semantic and collision authorities.
    """

    profile = _mapping(visual_profile, "visual profile")
    finished_room_ids = list(_array(
        profile.get("finished_room_ids"), "visual profile finished_room_ids"
    ))
    _require(
        len(finished_room_ids) == len(PRESENTATION_ROOM_KINDS)
        and len(finished_room_ids) == len(set(finished_room_ids)),
        "VISTA_HOME_PRESENTATION_BINDING_INVALID",
        "presentation bundles require the exact three-room finished slice",
    )
    _require(
        isinstance(presentation_bindings, Sequence)
        and not isinstance(presentation_bindings, (str, bytes))
        and len(presentation_bindings) == len(PRESENTATION_ROOM_KINDS),
        "VISTA_HOME_PRESENTATION_BINDING_INVALID",
        "presentation binding inventory must contain exactly three bundles",
    )
    room_kind_by_id = {
        room_id: room.get("kind")
        for room_id, room in rooms_by_id.items()
    }
    expected_room_ids = {
        room_id
        for room_id, kind in room_kind_by_id.items()
        if kind in set(PRESENTATION_ROOM_KINDS)
    }
    _require(
        set(finished_room_ids) == expected_room_ids,
        "VISTA_HOME_PRESENTATION_BINDING_INVALID",
        "visual profile finished rooms differ from the presentation slice",
    )

    operations: list[dict[str, Any]] = []
    seen_rooms: set[str] = set()
    seen_artifacts: set[str] = set()
    for index, raw_binding in enumerate(presentation_bindings):
        binding = dict(_mapping(raw_binding, f"presentation bindings[{index}]"))
        _require(
            set(binding) == PRESENTATION_EXECUTION_BINDING_KEYS,
            "VISTA_HOME_PRESENTATION_BINDING_INVALID",
            f"presentation binding {index} fields differ",
        )
        room_id = _safe_id(binding.get("room_id"), f"presentation binding {index}.room_id")
        room_kind = binding.get("room_kind")
        artifact_id = _safe_id(
            binding.get("artifact_id"), f"presentation binding {index}.artifact_id"
        )
        target_asset_id = _safe_id(
            binding.get("target_asset_id"),
            f"presentation binding {index}.target_asset_id",
        )
        _require(
            room_id in expected_room_ids
            and room_id not in seen_rooms
            and room_kind == room_kind_by_id[room_id]
            and room_kind in PRESENTATION_ROOM_KINDS,
            "VISTA_HOME_PRESENTATION_BINDING_INVALID",
            f"presentation binding {index} room identity differs",
        )
        room = rooms_by_id[room_id]
        expected_asset_id = _mapping(room.get("bundle"), f"room {room_id}.bundle").get("asset_id")
        _require(
            artifact_id == f"ue_bundle.room.{room_kind}"
            and artifact_id not in seen_artifacts
            and binding.get("artifact_kind") == PRESENTATION_ARTIFACT_KIND
            and target_asset_id == expected_asset_id == f"asset.bundle.{room_kind}",
            "VISTA_HOME_PRESENTATION_BINDING_INVALID",
            f"presentation binding {index} artifact identity differs",
        )
        expected_path = f"ue_import_bundles/{room_kind}_presentation_bundle.glb"
        _require(
            binding.get("relative_path") == expected_path
            and isinstance(binding.get("source_file"), str)
            and binding["source_file"].startswith("/")
            and binding.get("media_type") == "model/gltf-binary",
            "VISTA_HOME_PRESENTATION_BINDING_INVALID",
            f"presentation binding {index} source identity differs",
        )
        source_sha = _sha(
            binding.get("source_file_sha256"),
            f"presentation binding {index}.source_file_sha256",
        )
        _require(
            source_sha == _sha(binding.get("sha256"), f"presentation binding {index}.sha256"),
            "VISTA_HOME_PRESENTATION_BINDING_INVALID",
            f"presentation binding {index} source digests differ",
        )
        integers = {
            "size_bytes": 1,
            "mesh_count": 1,
            "material_count": 2,
            "pbr_complete_material_count": 2,
            "texture_count": 6,
        }
        for key, minimum in integers.items():
            number = binding.get(key)
            _require(
                isinstance(number, int) and not isinstance(number, bool) and number >= minimum,
                "VISTA_HOME_PRESENTATION_BINDING_INVALID",
                f"presentation binding {index}.{key} is invalid",
            )
        material_ids = binding.get("material_ids")
        _require(
            isinstance(material_ids, list)
            and len(material_ids) >= 2
            and material_ids == sorted(set(material_ids))
            and all(isinstance(item, str) and SAFE_ID.fullmatch(item) is not None
                    for item in material_ids)
            and binding["mesh_count"] == 1
            and binding["material_count"] == len(material_ids)
            and binding["pbr_complete_material_count"] == binding["material_count"]
            and binding["texture_count"] >= binding["material_count"] * 3,
            "VISTA_HOME_PRESENTATION_BINDING_INVALID",
            f"presentation binding {index} material inventory differs",
        )
        expected_transform = _transform(
            binding.get("expected_world_transform_cm"),
            f"presentation binding {index}.expected_world_transform_cm",
        )
        room_transform = _transform(
            room.get("world_transform_cm"), f"room {room_id}.world_transform_cm"
        )
        _require(
            expected_transform == room_transform
            and binding.get("bundle_root_transform") == {
                "location_m": [0, 0, 0],
                "rotation_deg": [0, 0, 0],
                "scale": [1, 1, 1],
            }
            and binding.get("root_transform_policy") == PRESENTATION_ROOT_TRANSFORM_POLICY
            and binding.get("semantic_policy") == PRESENTATION_SEMANTIC_POLICY
            and binding.get("collision_policy") == PRESENTATION_COLLISION_POLICY
            and binding.get("unreal_collision_profile") == PRESENTATION_UNREAL_COLLISION_PROFILE
            and binding.get("cameras_exported") is False
            and binding.get("lights_exported") is False,
            "VISTA_HOME_PRESENTATION_BINDING_INVALID",
            f"presentation binding {index} transform or policy differs",
        )
        source_hashes = _mapping(
            binding.get("source_hashes"), f"presentation binding {index}.source_hashes"
        )
        _require(
            set(source_hashes) == {
                "house_sha256", "visual_profile_sha256", "forge_plan_sha256"
            },
            "VISTA_HOME_PRESENTATION_BINDING_INVALID",
            f"presentation binding {index} source hash fields differ",
        )
        for key, value in source_hashes.items():
            _sha(value, f"presentation binding {index}.source_hashes.{key}")

        presentation_id = f"{room_id}/presentation.{profile['visual_profile_id']}"
        operations.append(_operation("place_rooms", "place_room_presentation_bundle", {
            "presentation_id": presentation_id,
            "room_id": room_id,
            "room_kind": room_kind,
            "artifact_id": artifact_id,
            "target_asset_id": target_asset_id,
            "source_file_sha256": source_sha,
            "transform": expected_transform,
            "material_count": binding["material_count"],
            "material_ids": list(material_ids),
            "texture_count": binding["texture_count"],
            "root_transform_policy": PRESENTATION_ROOT_TRANSFORM_POLICY,
            "semantic_policy": PRESENTATION_SEMANTIC_POLICY,
            "collision_policy": PRESENTATION_COLLISION_POLICY,
            "unreal_collision_profile": PRESENTATION_UNREAL_COLLISION_PROFILE,
            "source_hashes": dict(source_hashes),
            "tags": [
                "VistaPresentationId=" + presentation_id,
                "VistaPresentationFor=" + room_id,
                "VistaRole=room_presentation",
                "VistaVisualRevision=" + str(profile["visual_profile_id"]),
                "VistaCollisionPolicy=" + PRESENTATION_COLLISION_POLICY,
            ],
        }))
        seen_rooms.add(room_id)
        seen_artifacts.add(artifact_id)

    _require(
        seen_rooms == expected_room_ids and len(seen_artifacts) == len(PRESENTATION_ROOM_KINDS),
        "VISTA_HOME_PRESENTATION_BINDING_INVALID",
        "presentation bindings do not cover the exact finished room set",
    )
    return sorted(operations, key=lambda item: item["room_id"])


def build_composition_spec(
    plan: Mapping[str, Any],
    visual_profile: Mapping[str, Any] | None = None,
    presentation_bindings: Sequence[Mapping[str, Any]] | None = None,
) -> CompositionSpec:
    """Validate critical invariants and compile stable Editor operations.

    ``visual_profile=None`` is the accepted r1 compatibility path.  The r2
    path is additive and replaces only materialized review cameras here; it
    does not mutate the HouseSpec semantic, collision, or gameplay records.
    """

    value = dict(_mapping(plan, "build plan"))
    _require(set(value) == TOP_LEVEL_KEYS, "VISTA_HOME_PLAN_SHAPE_INVALID", "top-level fields differ")
    _require(value["schema_version"] == BUILD_PLAN_SCHEMA, "VISTA_HOME_PLAN_SCHEMA_MISMATCH", "schema mismatch")
    _require(value["units"] == "centimeters", "VISTA_HOME_PLAN_UNITS_INVALID", "units must be centimeters")
    plan_digest = _sha(value["content_digest"], "content_digest")
    house = _mapping(value["house"], "house")
    _sha(house.get("content_digest"), "house.content_digest")
    visual: Mapping[str, Any] | None = None
    if visual_profile is not None:
        visual = _mapping(visual_profile, "visual profile")
        _require(visual.get("schema_version") == VISUAL_PROFILE_SCHEMA,
                 "VISTA_HOME_VISUAL_PROFILE_INVALID", "visual profile schema differs")
        _require(visual.get("house_revision") == house.get("revision"),
                 "VISTA_HOME_VISUAL_PROFILE_INVALID",
                 "visual profile house revision differs")
        _safe_id(visual.get("visual_profile_id"), "visual profile visual_profile_id")
        _sha(visual.get("content_digest"), "visual profile content_digest")
    _require(
        not presentation_bindings or visual is not None,
        "VISTA_HOME_PRESENTATION_BINDING_INVALID",
        "presentation bundles require a selected visual profile",
    )

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
        if visual is None:
            for camera in sorted(_array(room.get("review_cameras"), f"room {room_id}.review_cameras"),
                                 key=lambda item: item["camera_id"]):
                operations.append(_operation("place_rooms", "place_review_camera", {
                    "semantic_id": room_id + "/camera." + _safe_id(camera.get("camera_id"), "camera_id"),
                    "transform": _transform(camera.get("world_transform_cm"), "camera transform"),
                    "fov_deg": float(camera.get("fov_deg")),
                    "tags": [TAG_PREFIX + room_id + "/camera." + camera["camera_id"], "VistaRoom=" + room_id],
                }))

    if visual is not None:
        finished_rooms = list(visual.get("finished_room_ids", []))
        compatibility_rooms = list(visual.get("compatibility_room_ids", []))
        _require(finished_rooms and all(room_id in rooms_by_id for room_id in finished_rooms) and
                 len(finished_rooms) == len(set(finished_rooms)),
                 "VISTA_HOME_VISUAL_PROFILE_INVALID", "finished room IDs are invalid")
        _require(all(room_id in rooms_by_id for room_id in compatibility_rooms) and
                 len(compatibility_rooms) == len(set(compatibility_rooms)) and
                 not set(finished_rooms) & set(compatibility_rooms),
                 "VISTA_HOME_VISUAL_PROFILE_INVALID", "compatibility room IDs are invalid")
        if presentation_bindings:
            operations.extend(compile_presentation_bundle_operations(
                visual,
                rooms_by_id=rooms_by_id,
                presentation_bindings=presentation_bindings,
            ))
        shot_operations = compile_realistic_review_operations(
            visual,
            room_bounds_by_id={
                room_id: _bounds(room["world_bounds_cm"], f"room {room_id}.bounds")
                for room_id, room in rooms_by_id.items()
            },
        )
        shots_per_finished_room = {
            room_id: sum(operation["room_id"] == room_id for operation in shot_operations)
            for room_id in finished_rooms
        }
        _require(all(count >= 2 for count in shots_per_finished_room.values()) and
                 all(operation["room_id"] in set(finished_rooms) for operation in shot_operations),
                 "VISTA_HOME_REVIEW_SHOT_INVALID",
                 "r2 requires at least two review shots for every finished room and no others")
        operations.extend(shot_operations)

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
    if visual is None:
        lighting_operation = _operation("configure_gameplay", "place_lighting", {
            "profile": "vista_playable_home_neutral_day_v2",
            "light_mobility": "movable",
            "exposure": {
                "method": "manual",
                "bias": -6.0,
                "apply_physical_camera_exposure": False,
            },
            "indoor_lights": indoor_lights,
        })
    else:
        lighting_operation = compile_realistic_lighting_operation(
            _mapping(visual.get("lighting_rig"), "visual profile lighting_rig"),
            room_ids=room_ids,
        )
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
        lighting_operation,
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
    if visual is not None:
        compiled["visual_profile_id"] = visual["visual_profile_id"]
        compiled["visual_profile_content_digest"] = visual["content_digest"]
    raw = canonical_json(compiled)
    return CompositionSpec(compiled, raw, hashlib.sha256(raw).hexdigest())
