"""Pure, deterministic architectural planning for the three-room r2 slice."""

from __future__ import annotations

import hashlib
import json
import math
import pathlib
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .config import (
    DEFAULT_BASEBOARD_DEPTH_M,
    DEFAULT_BASEBOARD_HEIGHT_M,
    DEFAULT_TRIM_WIDTH_M,
    DEFAULT_WALL_THICKNESS_M,
    FINISHED_ROOM_KINDS,
    FORGE_SCHEMA_VERSION,
    ForgeInputError,
    content_digest,
    profile_value,
    require_mapping,
    validate_source_contracts,
    vector3,
)
from .dressing import DressingPlan, build_dressing_plan
from .external_assets import AcquiredAsset, ExternalAssetSet
from .materials import material_by_id, material_plan_manifest
from .placement import (
    EXTERNAL_FORGE_SCHEMA_VERSION,
    ExternalPlacementPlan,
    ExternalPlacementSpec,
    PlacementManifestDocument,
    build_external_placement_plan,
)


@dataclass(frozen=True)
class RoomSpec:
    room_id: str
    kind: str
    location_m: tuple[float, float, float]
    rotation_deg: tuple[float, float, float]
    scale: tuple[float, float, float]
    bounds_min_m: tuple[float, float, float]
    bounds_max_m: tuple[float, float, float]


@dataclass(frozen=True)
class OpeningSpec:
    opening_id: str
    room_id: str
    wall_side: str
    opening_kind: str
    center_offset_m: float
    width_m: float
    sill_m: float
    height_m: float
    source_id: str


@dataclass(frozen=True)
class ComponentSpec:
    component_id: str
    room_id: str
    room_kind: str
    role: str
    export_role: str
    shape: str
    location_m: tuple[float, float, float]
    dimensions_m: tuple[float, float, float]
    rotation_deg: tuple[float, float, float]
    material_id: str
    collision_policy: str = "presentation_no_collision"
    semantic_policy: str = "presentation_only"
    preview_visible: bool = True
    source_opening_id: str | None = None


@dataclass(frozen=True)
class ForgePlan:
    schema_version: str
    forge_id: str
    house_revision: str
    visual_profile_id: str
    seed: int
    rooms: tuple[RoomSpec, ...]
    openings: tuple[OpeningSpec, ...]
    components: tuple[ComponentSpec, ...]
    dressing: DressingPlan
    material_plan: tuple[dict[str, Any], ...]
    source_house_digest: str
    source_profile_digest: str
    content_digest: str


@dataclass(frozen=True)
class ExternalForgePlan(ForgePlan):
    """Forge v2 plan; v1 remains a distinct byte-stable dataclass."""

    external_placement: ExternalPlacementPlan


FLOOR_MATERIAL_BY_KIND = {
    "entry_hall": "r2.slate_honed",
    "living_room": "r2.oak_natural",
    "kitchen_dining": "r2.terrazzo_warm",
}

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_SHA1_HEX = re.compile(r"^[0-9a-f]{40}$")
_ALLOWED_PBR_SEMANTICS = frozenset(
    {"base_color", "normal", "roughness", "metalness", "opacity", "ao"}
)
_PINNED_EXTERNAL_HERO_CONTRACTS = {
    "home.r1/room.living_room/entity.coffee_table.01": {
        "logical_asset_id": "visual.hero.living_coffee_table",
        "asset_id": "modern_coffee_table_01",
        "provider_files_hash": "31772c0aab6f930a18de82606146c0a97f08b7d0",
        "source_tree_sha256": "cf5fac22ac00b8725f91ad4565ddaa32dc5f10b213a0938a92de9e2432c1ddfe",
        "presentation_role": "hero",
        "slot_id": "table_surface",
        "attribution": "Modern Coffee Table 01 by Poly Haven, provided under CC0 1.0.",
        "modification_notice": (
            "The Poly Haven source is floor-centered, uniformly scaled, and exported as an "
            "identity-root presentation bundle; source geometry and textures are otherwise retained."
        ),
    },
    "home.r1/room.kitchen_dining/entity.stove.01": {
        "logical_asset_id": "visual.hero.kitchen_stove",
        "asset_id": "electric_stove",
        "provider_files_hash": "750ee10bdfe78eb6b0b620ef7b5a898e436fb696",
        "source_tree_sha256": "c55acbd188af4674ce5c1c8605f2447c5fb830a05b1650b0d03296b419b38795",
        "presentation_role": "event_critical",
        "slot_id": "stove_surface",
        "attribution": "Electric Stove by Poly Haven, provided under CC0 1.0.",
        "modification_notice": (
            "The Poly Haven source is floor-centered and exported as an identity-root presentation "
            "bundle. Its receipt-bound opacity texture is preserved; the direct opacity-to-Principled "
            "Alpha link is sanitized in Blender 4.5.8 to a GREATER_THAN 0.5 clip graph so glTF exports "
            "alphaMode MASK (effective alphaCutoff 0.5), and VISTA source/digest/active-semantic/"
            "alpha-policy material extras are added. Geometry and other receipt-bound PBR texture "
            "semantics are otherwise retained."
        ),
    },
}


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _room_specs(house: Mapping[str, Any]) -> tuple[RoomSpec, ...]:
    result: list[RoomSpec] = []
    for room in house["rooms"]:
        if not isinstance(room, Mapping) or room.get("kind") not in FINISHED_ROOM_KINDS:
            continue
        transform = require_mapping(room.get("transform"), field=f"room {room.get('room_id')} transform")
        bounds = require_mapping(room.get("bounds_m"), field=f"room {room.get('room_id')} bounds_m")
        room_spec = RoomSpec(
            room_id=str(room["room_id"]),
            kind=str(room["kind"]),
            location_m=vector3(transform.get("location_m", ()), field="room location_m"),
            rotation_deg=vector3(transform.get("rotation_deg", ()), field="room rotation_deg"),
            scale=vector3(transform.get("scale", ()), field="room scale"),
            bounds_min_m=vector3(bounds.get("min_m", ()), field="room bounds min_m"),
            bounds_max_m=vector3(bounds.get("max_m", ()), field="room bounds max_m"),
        )
        if any(value <= 0 for value in room_spec.scale):
            raise ForgeInputError(f"room {room_spec.room_id} scale must be positive")
        if any(high <= low for low, high in zip(room_spec.bounds_min_m, room_spec.bounds_max_m)):
            raise ForgeInputError(f"room {room_spec.room_id} bounds must have positive volume")
        result.append(room_spec)
    result.sort(key=lambda item: FINISHED_ROOM_KINDS.index(item.kind))
    return tuple(result)


def _world_to_room(room: RoomSpec, point_m: Sequence[float]) -> tuple[float, float, float]:
    point = vector3(point_m, field="world point")
    delta = tuple(point[index] - room.location_m[index] for index in range(3))
    if abs(room.rotation_deg[0]) > 1e-6 or abs(room.rotation_deg[1]) > 1e-6:
        raise ForgeInputError("the r2 architectural forge supports only upright room transforms")
    angle = math.radians(-room.rotation_deg[2])
    rotated = (
        delta[0] * math.cos(angle) - delta[1] * math.sin(angle),
        delta[0] * math.sin(angle) + delta[1] * math.cos(angle),
        delta[2],
    )
    return tuple(rotated[index] / room.scale[index] for index in range(3))  # type: ignore[return-value]


def _wall_side(room: RoomSpec, local: Sequence[float]) -> tuple[str, float]:
    x, y, _ = local
    candidates = {
        "west": abs(x - room.bounds_min_m[0]),
        "east": abs(x - room.bounds_max_m[0]),
        "south": abs(y - room.bounds_min_m[1]),
        "north": abs(y - room.bounds_max_m[1]),
    }
    side = min(candidates, key=candidates.get)
    if candidates[side] > 0.12:
        raise ForgeInputError(f"opening is not on a room boundary for {room.room_id}: {local}")
    return side, x if side in {"north", "south"} else y


def _portal_openings(house: Mapping[str, Any], rooms: tuple[RoomSpec, ...]) -> list[OpeningSpec]:
    room_by_id = {room.room_id: room for room in rooms}
    result: list[OpeningSpec] = []
    portals = house.get("portals", [])
    if not isinstance(portals, list):
        raise ForgeInputError("HouseSpec portals must be a list")
    for portal in portals:
        if not isinstance(portal, Mapping):
            raise ForgeInputError("HouseSpec portal entries must be objects")
        transform = require_mapping(portal.get("world_transform"), field="portal world_transform")
        clearance = require_mapping(portal.get("clearance"), field="portal clearance")
        for room_field in ("from_room_id", "to_room_id"):
            room_id = portal.get(room_field)
            if room_id not in room_by_id:
                continue
            room = room_by_id[str(room_id)]
            local = _world_to_room(room, transform.get("location_m", ()))
            side, offset = _wall_side(room, local)
            width = float(clearance.get("width_m", 0))
            height = float(clearance.get("height_m", 0))
            if width <= 0 or height <= 0:
                raise ForgeInputError(f"portal {portal.get('portal_id')} has invalid clearance")
            result.append(
                OpeningSpec(
                    opening_id=f"{portal['portal_id']}@{room.room_id}",
                    room_id=room.room_id,
                    wall_side=side,
                    opening_kind="door",
                    center_offset_m=offset,
                    width_m=width,
                    sill_m=room.bounds_min_m[2],
                    height_m=height,
                    source_id=str(portal["portal_id"]),
                )
            )
    return result


def _exit_openings(house: Mapping[str, Any], rooms: tuple[RoomSpec, ...]) -> list[OpeningSpec]:
    room_by_id = {room.room_id: room for room in rooms}
    result: list[OpeningSpec] = []
    for entity in house.get("entities", []):
        if not isinstance(entity, Mapping) or entity.get("category") != "exit_door":
            continue
        room = room_by_id.get(str(entity.get("room_id")))
        if room is None:
            continue
        transform = require_mapping(entity.get("transform"), field="exit door transform")
        local = vector3(transform.get("location_m", ()), field="exit door location_m")
        side, offset = _wall_side(room, local)
        result.append(
            OpeningSpec(
                opening_id=f"{entity['entity_id']}@architecture",
                room_id=room.room_id,
                wall_side=side,
                opening_kind="door",
                center_offset_m=offset,
                width_m=1.1,
                sill_m=room.bounds_min_m[2],
                height_m=2.2,
                source_id=str(entity["entity_id"]),
            )
        )
    return result


def _window_openings(profile: Mapping[str, Any], rooms: tuple[RoomSpec, ...]) -> list[OpeningSpec]:
    room_by_id = {room.room_id: room for room in rooms}
    architecture = require_mapping(profile.get("architecture_profile", {}), field="architecture_profile")
    configured = architecture.get("windows")
    if configured is None:
        kind_to_room = {room.kind: room for room in rooms}
        configured = [
            {
                "window_id": "window.living.west.01",
                "room_id": kind_to_room["living_room"].room_id,
                "wall_side": "west",
                "center_offset_m": -0.35,
                "width_m": 1.75,
                "sill_m": 0.78,
                "height_m": 1.35,
            },
            {
                "window_id": "window.kitchen.east.01",
                "room_id": kind_to_room["kitchen_dining"].room_id,
                "wall_side": "east",
                "center_offset_m": 0.15,
                "width_m": 1.55,
                "sill_m": 0.92,
                "height_m": 1.25,
            },
        ]
    if not isinstance(configured, list):
        raise ForgeInputError("architecture_profile.windows must be a list")
    result: list[OpeningSpec] = []
    for index, item in enumerate(configured):
        if not isinstance(item, Mapping):
            raise ForgeInputError("architecture_profile window entries must be objects")
        room_id = str(item.get("room_id", ""))
        room = room_by_id.get(room_id)
        if room is None:
            raise ForgeInputError(f"window {index} references a non-finished room: {room_id}")
        side = str(item.get("wall_side", ""))
        if side not in {"north", "south", "east", "west"}:
            raise ForgeInputError(f"window {index} has invalid wall_side")
        width = float(item.get("width_m", 0))
        height = float(item.get("height_m", 0))
        sill = float(item.get("sill_m", 0))
        center = float(item.get("center_offset_m", 0))
        if not all(math.isfinite(value) for value in (width, height, sill, center)) or width <= 0 or height <= 0:
            raise ForgeInputError(f"window {index} has invalid dimensions")
        if sill < room.bounds_min_m[2] or sill + height >= room.bounds_max_m[2]:
            raise ForgeInputError(f"window {index} does not fit room height")
        result.append(
            OpeningSpec(
                opening_id=str(item.get("window_id", f"window.{index:02d}")),
                room_id=room_id,
                wall_side=side,
                opening_kind="window",
                center_offset_m=center,
                width_m=width,
                sill_m=sill,
                height_m=height,
                source_id=str(item.get("window_id", f"window.{index:02d}")),
            )
        )
    return result


def _component(
    room: RoomSpec,
    suffix: str,
    role: str,
    export_role: str,
    location: Sequence[float],
    dimensions: Sequence[float],
    material_id: str,
    *,
    rotation_deg: Sequence[float] = (0.0, 0.0, 0.0),
    preview_visible: bool = True,
    source_opening_id: str | None = None,
) -> ComponentSpec:
    location_m = vector3(location, field=f"component {suffix} location")
    dimensions_m = vector3(dimensions, field=f"component {suffix} dimensions")
    if any(value <= 0 for value in dimensions_m):
        raise ForgeInputError(f"component {suffix} dimensions must be positive")
    if material_id not in material_by_id():
        raise ForgeInputError(f"component {suffix} references unknown material {material_id}")
    return ComponentSpec(
        component_id=f"{room.room_id}/visual.r2/{_slug(suffix)}",
        room_id=room.room_id,
        room_kind=room.kind,
        role=role,
        export_role=export_role,
        shape="box",
        location_m=location_m,
        dimensions_m=dimensions_m,
        rotation_deg=vector3(rotation_deg, field=f"component {suffix} rotation"),
        material_id=material_id,
        preview_visible=preview_visible,
        source_opening_id=source_opening_id,
    )


def _wall_axis(room: RoomSpec, side: str) -> tuple[float, float, float]:
    if side in {"north", "south"}:
        return room.bounds_min_m[0], room.bounds_max_m[0], room.bounds_max_m[1] if side == "north" else room.bounds_min_m[1]
    return room.bounds_min_m[1], room.bounds_max_m[1], room.bounds_max_m[0] if side == "east" else room.bounds_min_m[0]


def _wall_box_location(side: str, boundary: float, axis_center: float, z: float) -> tuple[float, float, float]:
    return (axis_center, boundary, z) if side in {"north", "south"} else (boundary, axis_center, z)


def _wall_box_dimensions(side: str, axis_length: float, depth: float, height: float) -> tuple[float, float, float]:
    return (axis_length, depth, height) if side in {"north", "south"} else (depth, axis_length, height)


def _inward_offset(side: str, distance: float) -> tuple[float, float]:
    return {
        "north": (0.0, -distance),
        "south": (0.0, distance),
        "east": (-distance, 0.0),
        "west": (distance, 0.0),
    }[side]


def _opaque_spans(start: float, end: float, openings: Sequence[OpeningSpec]) -> list[tuple[float, float]]:
    intervals = sorted((item.center_offset_m - item.width_m / 2, item.center_offset_m + item.width_m / 2) for item in openings)
    cursor = start
    spans: list[tuple[float, float]] = []
    for low, high in intervals:
        low = max(start, low)
        high = min(end, high)
        if high <= low:
            continue
        if low < cursor - 1e-6:
            raise ForgeInputError("architectural openings overlap")
        if low > cursor + 1e-6:
            spans.append((cursor, low))
        cursor = max(cursor, high)
    if cursor < end - 1e-6:
        spans.append((cursor, end))
    return spans


def _wall_components(
    room: RoomSpec,
    side: str,
    openings: Sequence[OpeningSpec],
    *,
    wall_thickness: float,
    trim_width: float,
    baseboard_height: float,
    baseboard_depth: float,
) -> list[ComponentSpec]:
    start, end, boundary = _wall_axis(room, side)
    z_min, z_max = room.bounds_min_m[2], room.bounds_max_m[2]
    height = z_max - z_min
    components: list[ComponentSpec] = []
    for index, (low, high) in enumerate(_opaque_spans(start, end, openings)):
        components.append(
            _component(
                room,
                f"wall.{side}.opaque.{index:02d}",
                "wall_opaque",
                "architecture_shell",
                _wall_box_location(side, boundary, (low + high) / 2, z_min + height / 2),
                _wall_box_dimensions(side, high - low, wall_thickness, height),
                "r2.plaster_warm",
                preview_visible=side != "south",
            )
        )
    for opening in openings:
        opening_bottom = opening.sill_m
        opening_top = opening.sill_m + opening.height_m
        if opening_bottom > z_min + 1e-6:
            components.append(
                _component(
                    room,
                    f"wall.{side}.{_slug(opening.opening_id)}.below",
                    "wall_opaque",
                    "architecture_shell",
                    _wall_box_location(side, boundary, opening.center_offset_m, (z_min + opening_bottom) / 2),
                    _wall_box_dimensions(side, opening.width_m, wall_thickness, opening_bottom - z_min),
                    "r2.plaster_warm",
                    preview_visible=side != "south",
                    source_opening_id=opening.opening_id,
                )
            )
        if opening_top < z_max - 1e-6:
            components.append(
                _component(
                    room,
                    f"wall.{side}.{_slug(opening.opening_id)}.above",
                    "wall_opaque",
                    "architecture_shell",
                    _wall_box_location(side, boundary, opening.center_offset_m, (opening_top + z_max) / 2),
                    _wall_box_dimensions(side, opening.width_m, wall_thickness, z_max - opening_top),
                    "r2.plaster_warm",
                    preview_visible=side != "south",
                    source_opening_id=opening.opening_id,
                )
            )
        reveal_thickness = 0.028
        for label, offset in (("jamb_l", -opening.width_m / 2), ("jamb_r", opening.width_m / 2)):
            components.append(
                _component(
                    room,
                    f"reveal.{_slug(opening.opening_id)}.{label}",
                    "opening_reveal",
                    "architectural_detail",
                    _wall_box_location(side, boundary, opening.center_offset_m + offset, opening_bottom + opening.height_m / 2),
                    _wall_box_dimensions(side, reveal_thickness, wall_thickness + 0.01, opening.height_m),
                    "r2.trim_satin",
                    preview_visible=side != "south",
                    source_opening_id=opening.opening_id,
                )
            )
        components.append(
            _component(
                room,
                f"reveal.{_slug(opening.opening_id)}.header",
                "opening_reveal",
                "architectural_detail",
                _wall_box_location(side, boundary, opening.center_offset_m, opening_top),
                _wall_box_dimensions(side, opening.width_m, wall_thickness + 0.01, reveal_thickness),
                "r2.trim_satin",
                preview_visible=side != "south",
                source_opening_id=opening.opening_id,
            )
        )
        inward_x, inward_y = _inward_offset(side, wall_thickness / 2 + baseboard_depth / 2)
        for label, offset in (("left", -opening.width_m / 2 - trim_width / 2), ("right", opening.width_m / 2 + trim_width / 2)):
            location = _wall_box_location(side, boundary, opening.center_offset_m + offset, opening_bottom + opening.height_m / 2)
            location = (location[0] + inward_x, location[1] + inward_y, location[2])
            components.append(
                _component(
                    room,
                    f"trim.{_slug(opening.opening_id)}.{label}",
                    "window_trim" if opening.opening_kind == "window" else "door_trim",
                    "architectural_detail",
                    location,
                    _wall_box_dimensions(side, trim_width, baseboard_depth, opening.height_m + trim_width),
                    "r2.trim_satin",
                    preview_visible=side != "south",
                    source_opening_id=opening.opening_id,
                )
            )
        location = _wall_box_location(side, boundary, opening.center_offset_m, opening_top + trim_width / 2)
        location = (location[0] + inward_x, location[1] + inward_y, location[2])
        components.append(
            _component(
                room,
                f"trim.{_slug(opening.opening_id)}.header",
                "window_trim" if opening.opening_kind == "window" else "door_trim",
                "architectural_detail",
                location,
                _wall_box_dimensions(side, opening.width_m + trim_width * 2, baseboard_depth, trim_width),
                "r2.trim_satin",
                preview_visible=side != "south",
                source_opening_id=opening.opening_id,
            )
        )
        if opening.opening_kind == "window":
            frame_depth = wall_thickness + 0.035
            frame_width = 0.045
            for label, offset in (("left", -opening.width_m / 2), ("right", opening.width_m / 2)):
                components.append(
                    _component(
                        room,
                        f"window.{_slug(opening.opening_id)}.frame.{label}",
                        "window_frame",
                        "architectural_detail",
                        _wall_box_location(side, boundary, opening.center_offset_m + offset, opening_bottom + opening.height_m / 2),
                        _wall_box_dimensions(side, frame_width, frame_depth, opening.height_m),
                        "r2.window_frame",
                        source_opening_id=opening.opening_id,
                    )
                )
            for label, z in (("sill", opening_bottom), ("head", opening_top), ("mullion", opening_bottom + opening.height_m * 0.55)):
                components.append(
                    _component(
                        room,
                        f"window.{_slug(opening.opening_id)}.frame.{label}",
                        "window_frame",
                        "architectural_detail",
                        _wall_box_location(side, boundary, opening.center_offset_m, z),
                        _wall_box_dimensions(side, opening.width_m, frame_depth, frame_width),
                        "r2.window_frame",
                        source_opening_id=opening.opening_id,
                    )
                )
            glass_depth = 0.012
            components.append(
                _component(
                    room,
                    f"window.{_slug(opening.opening_id)}.glass",
                    "window_glass",
                    "architectural_detail",
                    _wall_box_location(side, boundary, opening.center_offset_m, opening_bottom + opening.height_m / 2),
                    _wall_box_dimensions(side, opening.width_m - 0.06, glass_depth, opening.height_m - 0.06),
                    "r2.window_glass",
                    source_opening_id=opening.opening_id,
                )
            )
            outward_x, outward_y = _inward_offset(side, -(wall_thickness / 2 + 0.06))
            scrim = _wall_box_location(side, boundary, opening.center_offset_m, opening_bottom + opening.height_m / 2)
            components.append(
                _component(
                    room,
                    f"window.{_slug(opening.opening_id)}.exterior_scrim",
                    "exterior_treatment",
                    "architectural_detail",
                    (scrim[0] + outward_x, scrim[1] + outward_y, scrim[2]),
                    _wall_box_dimensions(side, opening.width_m + 0.24, 0.018, opening.height_m + 0.24),
                    "r2.exterior_scrim",
                    source_opening_id=opening.opening_id,
                )
            )

    door_openings = [item for item in openings if item.opening_kind == "door" and item.sill_m <= z_min + 1e-6]
    inward_x, inward_y = _inward_offset(side, wall_thickness / 2 + baseboard_depth / 2)
    for index, (low, high) in enumerate(_opaque_spans(start, end, door_openings)):
        location = _wall_box_location(side, boundary, (low + high) / 2, z_min + baseboard_height / 2)
        components.append(
            _component(
                room,
                f"baseboard.{side}.{index:02d}",
                "baseboard",
                "architectural_detail",
                (location[0] + inward_x, location[1] + inward_y, location[2]),
                _wall_box_dimensions(side, high - low, baseboard_depth, baseboard_height),
                "r2.trim_satin",
                preview_visible=side != "south",
            )
        )
    return components


def _slab_components(room: RoomSpec) -> list[ComponentSpec]:
    x0, y0, z0 = room.bounds_min_m
    x1, y1, z1 = room.bounds_max_m
    center = ((x0 + x1) / 2, (y0 + y1) / 2)
    return [
        _component(
            room,
            "floor.finish",
            "floor_finish",
            "architecture_shell",
            (center[0], center[1], z0 - 0.025),
            (x1 - x0, y1 - y0, 0.05),
            FLOOR_MATERIAL_BY_KIND[room.kind],
        ),
        _component(
            room,
            "ceiling.finish",
            "ceiling_finish",
            "architecture_shell",
            (center[0], center[1], z1 + 0.035),
            (x1 - x0, y1 - y0, 0.07),
            "r2.ceiling_matte",
            preview_visible=False,
        ),
    ]


def _threshold_components(rooms: tuple[RoomSpec, ...], openings: Sequence[OpeningSpec]) -> list[ComponentSpec]:
    room_by_id = {room.room_id: room for room in rooms}
    seen_sources: set[str] = set()
    result: list[ComponentSpec] = []
    for opening in sorted(openings, key=lambda item: (item.source_id, item.room_id)):
        if opening.opening_kind != "door" or opening.source_id in seen_sources:
            continue
        linked = [item for item in openings if item.source_id == opening.source_id]
        if len(linked) != 2:
            continue
        seen_sources.add(opening.source_id)
        owner = min(linked, key=lambda item: item.room_id)
        room = room_by_id[owner.room_id]
        _, _, boundary = _wall_axis(room, owner.wall_side)
        location = _wall_box_location(owner.wall_side, boundary, owner.center_offset_m, room.bounds_min_m[2] + 0.012)
        result.append(
            _component(
                room,
                f"threshold.{_slug(owner.source_id)}",
                "floor_transition",
                "architectural_detail",
                location,
                _wall_box_dimensions(owner.wall_side, owner.width_m + 0.06, 0.20, 0.024),
                "r2.threshold_brass",
                source_opening_id=owner.opening_id,
            )
        )
    return result


def _kitchen_cabinetry(room: RoomSpec) -> list[ComponentSpec]:
    result: list[ComponentSpec] = []
    centers = (-1.92, -1.32, -0.72, -0.12, 0.48, 1.08)
    for index, x in enumerate(centers):
        result.extend(
            [
                _component(room, f"cabinet.lower.{index:02d}.carcass", "cabinet_carcass", "cabinetry", (x, 1.70, 0.46), (0.57, 0.56, 0.82), "r2.cabinet_walnut"),
                _component(room, f"cabinet.lower.{index:02d}.front", "cabinet_front", "cabinetry", (x, 1.405, 0.49), (0.525, 0.025, 0.68), "r2.cabinet_sage"),
                _component(room, f"cabinet.lower.{index:02d}.toe", "cabinet_toe_kick", "cabinetry", (x, 1.49, 0.07), (0.53, 0.08, 0.11), "r2.cabinet_sage"),
                _component(room, f"cabinet.lower.{index:02d}.handle", "cabinet_hardware", "cabinetry", (x, 1.383, 0.72), (0.22, 0.018, 0.018), "r2.hardware_brass"),
                _component(room, f"cabinet.upper.{index:02d}.carcass", "cabinet_carcass", "cabinetry", (x, 1.79, 2.12), (0.57, 0.36, 0.70), "r2.cabinet_walnut"),
                _component(room, f"cabinet.upper.{index:02d}.front", "cabinet_front", "cabinetry", (x, 1.598, 2.12), (0.525, 0.025, 0.64), "r2.cabinet_sage"),
                _component(room, f"cabinet.upper.{index:02d}.handle", "cabinet_hardware", "cabinetry", (x, 1.577, 1.91), (0.20, 0.018, 0.018), "r2.hardware_brass"),
            ]
        )
    run_center = (centers[0] + centers[-1]) / 2
    run_length = centers[-1] - centers[0] + 0.60
    result.extend(
        [
            _component(room, "cabinet.countertop", "countertop", "cabinetry", (run_center, 1.68, 0.905), (run_length + 0.04, 0.62, 0.06), "r2.counter_quartz"),
            _component(room, "cabinet.backsplash", "backsplash", "cabinetry", (run_center, 1.895, 1.30), (run_length, 0.025, 0.68), "r2.backsplash_tile"),
            _component(room, "cabinet.end_panel.west", "cabinet_end_panel", "cabinetry", (centers[0] - 0.305, 1.70, 0.46), (0.025, 0.58, 0.86), "r2.cabinet_sage"),
            _component(room, "cabinet.end_panel.east", "cabinet_end_panel", "cabinetry", (centers[-1] + 0.305, 1.70, 0.46), (0.025, 0.58, 0.86), "r2.cabinet_sage"),
        ]
    )
    return result


def _entry_millwork(room: RoomSpec) -> list[ComponentSpec]:
    """Author a dense, presentation-only arrival sequence along the hall walls.

    The r1 entry is a three-metre-wide circulation spine with paired doorways at
    y=-2 and y=2.  This millwork therefore stays inside the central, door-free
    wall spans and outside the protected x=+/-0.62 m pawn/NPC corridor.  It is
    deliberately expressed as deterministic box components so the same source
    plan can be rebuilt headlessly and inspected before Blender is available.
    """

    result = [
        # West feature wall: dark walnut field, oak battens, floating console,
        # and a framed dark focal panel at standing eye height.
        _component(
            room,
            "entry.west.feature_backer",
            "entry_feature_panel",
            "architectural_detail",
            (-1.385, 0.0, 1.36),
            (0.04, 2.68, 2.44),
            "r2.cabinet_walnut",
        ),
        _component(
            room,
            "entry.west.console.carcass",
            "entry_console_carcass",
            "cabinetry",
            (-1.19, 0.0, 0.67),
            (0.38, 1.30, 0.28),
            "r2.cabinet_walnut",
        ),
        _component(
            room,
            "entry.west.console.front.lower",
            "entry_console_front",
            "cabinetry",
            (-0.987, 0.0, 0.605),
            (0.026, 1.22, 0.105),
            "r2.cabinet_sage",
        ),
        _component(
            room,
            "entry.west.console.front.upper",
            "entry_console_front",
            "cabinetry",
            (-0.987, 0.0, 0.735),
            (0.026, 1.22, 0.105),
            "r2.cabinet_sage",
        ),
        _component(
            room,
            "entry.west.console.top",
            "entry_console_top",
            "cabinetry",
            (-1.185, 0.0, 0.835),
            (0.43, 1.36, 0.045),
            "r2.counter_quartz",
        ),
        _component(
            room,
            "entry.west.console.handle.lower",
            "entry_console_hardware",
            "cabinetry",
            (-0.980, 0.0, 0.605),
            (0.018, 0.28, 0.024),
            "r2.hardware_brass",
        ),
        _component(
            room,
            "entry.west.console.handle.upper",
            "entry_console_hardware",
            "cabinetry",
            (-0.980, 0.0, 0.735),
            (0.018, 0.28, 0.024),
            "r2.hardware_brass",
        ),
        _component(
            room,
            "entry.west.console.shadow_line",
            "entry_console_hardware",
            "cabinetry",
            (-0.985, 0.0, 0.515),
            (0.022, 1.16, 0.022),
            "r2.hardware_brass",
        ),
        _component(
            room,
            "entry.west.focal_panel",
            "entry_focal_panel",
            "architectural_detail",
            (-1.320, 0.0, 1.72),
            (0.025, 0.82, 1.04),
            "r2.window_frame",
        ),
        # East utility wall: a continuous walnut datum gives the long corridor
        # visual weight while the shelf, rail and hooks communicate daily use.
        _component(
            room,
            "entry.east.coat_backer",
            "entry_coat_panel",
            "architectural_detail",
            (1.385, 0.0, 1.37),
            (0.04, 2.48, 2.42),
            "r2.cabinet_walnut",
        ),
        _component(
            room,
            "entry.east.coat_shelf",
            "entry_coat_shelf",
            "cabinetry",
            (1.22, 0.0, 2.18),
            (0.36, 1.88, 0.065),
            "r2.oak_natural",
        ),
        _component(
            room,
            "entry.east.coat_rail",
            "entry_coat_rail",
            "architectural_detail",
            (1.29, 0.0, 1.78),
            (0.075, 1.66, 0.065),
            "r2.hardware_brass",
        ),
        _component(
            room,
            "entry.east.boot_ledge",
            "entry_boot_ledge",
            "cabinetry",
            (1.25, 0.0, 0.44),
            (0.31, 1.72, 0.065),
            "r2.oak_natural",
        ),
    ]

    for index, y in enumerate((-1.20, -0.80, -0.40, 0.0, 0.40, 0.80, 1.20)):
        result.append(
            _component(
                room,
                f"entry.west.feature_batten.{index:02d}",
                "entry_feature_batten",
                "architectural_detail",
                (-1.350, y, 1.36),
                (0.025, 0.035, 2.36),
                "r2.oak_natural",
            )
        )

    for label, location, dimensions in (
        ("left", (-1.297, -0.455, 1.72), (0.018, 0.028, 1.13)),
        ("right", (-1.297, 0.455, 1.72), (0.018, 0.028, 1.13)),
        ("bottom", (-1.297, 0.0, 1.155), (0.018, 0.94, 0.028)),
        ("top", (-1.297, 0.0, 2.285), (0.018, 0.94, 0.028)),
    ):
        result.append(
            _component(
                room,
                f"entry.west.focal_frame.{label}",
                "entry_focal_frame",
                "architectural_detail",
                location,
                dimensions,
                "r2.hardware_brass",
            )
        )

    for index, y in enumerate((-0.66, -0.33, 0.0, 0.33, 0.66)):
        result.append(
            _component(
                room,
                f"entry.east.coat_hook.{index:02d}",
                "entry_coat_hook",
                "architectural_detail",
                (1.205, y, 1.55),
                (0.17, 0.035, 0.22),
                "r2.hardware_brass",
            )
        )

    return result


def _plan_payload(
    house: Mapping[str, Any],
    profile: Mapping[str, Any],
    rooms: tuple[RoomSpec, ...],
    openings: tuple[OpeningSpec, ...],
    components: tuple[ComponentSpec, ...],
    dressing: DressingPlan,
) -> dict[str, Any]:
    return {
        "schema_version": FORGE_SCHEMA_VERSION,
        "forge_id": "vista_playable_home.realistic_interior_r2",
        "house_revision": house["revision"],
        "visual_profile_id": profile.get("visual_profile_id"),
        "seed": profile["seed"],
        "rooms": rooms,
        "openings": openings,
        "components": components,
        "dressing": dressing,
        "material_plan": material_plan_manifest(),
        "source_house_digest": house.get("content_digest") or content_digest(house),
        "source_profile_digest": profile.get("content_digest") or content_digest(profile),
    }


def build_forge_plan(house: Mapping[str, Any], profile: Mapping[str, Any]) -> ForgePlan:
    """Compile HouseSpec + VisualProfile-shaped input into one immutable plan."""

    validate_source_contracts(house, profile)
    rooms = _room_specs(house)
    openings = tuple(
        sorted(
            _portal_openings(house, rooms) + _exit_openings(house, rooms) + _window_openings(profile, rooms),
            key=lambda item: (item.room_id, item.wall_side, item.center_offset_m, item.opening_id),
        )
    )
    wall_thickness = profile_value(profile, "wall_thickness_m", DEFAULT_WALL_THICKNESS_M, minimum=0.10, maximum=0.35)
    trim_width = profile_value(profile, "trim_width_m", DEFAULT_TRIM_WIDTH_M, minimum=0.04, maximum=0.16)
    baseboard_height = profile_value(profile, "baseboard_height_m", DEFAULT_BASEBOARD_HEIGHT_M, minimum=0.06, maximum=0.25)
    baseboard_depth = profile_value(profile, "baseboard_depth_m", DEFAULT_BASEBOARD_DEPTH_M, minimum=0.012, maximum=0.06)
    components: list[ComponentSpec] = []
    for room in rooms:
        components.extend(_slab_components(room))
        for side in ("south", "west", "north", "east"):
            side_openings = [item for item in openings if item.room_id == room.room_id and item.wall_side == side]
            components.extend(
                _wall_components(
                    room,
                    side,
                    side_openings,
                    wall_thickness=wall_thickness,
                    trim_width=trim_width,
                    baseboard_height=baseboard_height,
                    baseboard_depth=baseboard_depth,
                )
            )
        if room.kind == "kitchen_dining":
            components.extend(_kitchen_cabinetry(room))
        elif room.kind == "entry_hall":
            components.extend(_entry_millwork(room))
    components.extend(_threshold_components(rooms, openings))
    components.sort(key=lambda item: item.component_id)
    if len({item.component_id for item in components}) != len(components):
        raise ForgeInputError("architectural component IDs are not unique")
    dressing = build_dressing_plan(house, profile, rooms, openings)
    payload = _plan_payload(house, profile, rooms, openings, tuple(components), dressing)
    digest = content_digest(payload)
    return ForgePlan(
        schema_version=FORGE_SCHEMA_VERSION,
        forge_id="vista_playable_home.realistic_interior_r2",
        house_revision=str(house["revision"]),
        visual_profile_id=str(profile.get("visual_profile_id")),
        seed=int(profile["seed"]),
        rooms=rooms,
        openings=openings,
        components=tuple(components),
        dressing=dressing,
        material_plan=tuple(material_plan_manifest()),
        source_house_digest=str(house.get("content_digest") or content_digest(house)),
        source_profile_digest=str(profile.get("content_digest") or content_digest(profile)),
        content_digest=digest,
    )


def _profile_rows(
    profile: Mapping[str, Any], field: str
) -> list[dict[str, Any]]:
    rows = profile.get(field)
    if type(rows) is not list or any(type(row) is not dict for row in rows):
        raise ForgeInputError(f"VisualProfile {field} must be a list of objects")
    return rows


def _unique_profile_index(
    rows: Sequence[Mapping[str, Any]], field: str, label: str
) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        identity = row.get(field)
        if type(identity) is not str or not identity:
            raise ForgeInputError(f"{label} has an invalid {field}")
        if identity in result:
            raise ForgeInputError(f"{label} {field} identities are duplicated: {identity}")
        result[identity] = row
    return result


def _exact_metric_vector(value: Any, *, field: str) -> tuple[float, float, float]:
    if (
        type(value) is not list
        or len(value) != 3
        or any(type(component) not in {int, float} or isinstance(component, bool) for component in value)
    ):
        raise ForgeInputError(f"{field} must contain exactly three JSON numbers")
    result = tuple(float(component) for component in value)
    if not all(math.isfinite(component) for component in result):
        raise ForgeInputError(f"{field} must be finite")
    return result  # type: ignore[return-value]


def _canonical_metadata_digest(value: Any, *, label: str) -> str:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8", "strict")
    except (TypeError, ValueError, OverflowError) as error:
        raise ForgeInputError(f"{label} is not canonical JSON") from error
    return hashlib.sha256(payload).hexdigest()


def _safe_asset_relative_path(value: Any, *, field: str) -> str:
    if type(value) is not str or not value or "\\" in value or "\x00" in value:
        raise ForgeInputError(f"{field} is not a safe relative path")
    path = pathlib.PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ForgeInputError(f"{field} is not a safe relative path")
    return path.as_posix()


def _validate_acquired_model_metadata(asset: AcquiredAsset) -> None:
    """Revalidate loader-representable metadata, not Blender runtime graphs."""

    logical_id = asset.logical_asset_id
    if (
        asset.asset_type != "model"
        or asset.file_variant != "blend"
        or asset.resolution != "4k"
        or type(asset.provider_files_hash) is not str
        or not _SHA1_HEX.fullmatch(asset.provider_files_hash)
    ):
        raise ForgeInputError(
            f"external semantic acquisition model identity is invalid: {logical_id}"
        )
    expected_root = f"assets/{asset.asset_id}"
    if _safe_asset_relative_path(
        asset.source_relative_root, field=f"{logical_id} source_relative_root"
    ) != expected_root:
        raise ForgeInputError(
            f"external semantic acquisition source root differs from asset identity: {logical_id}"
        )
    if type(asset.files) is not tuple or not asset.files:
        raise ForgeInputError(f"external semantic acquisition has no ordered files: {logical_id}")

    first_relative_path = _safe_asset_relative_path(
        asset.files[0].relative_path, field=f"{logical_id} primary acquired file"
    )
    expected_primary = f"{expected_root}/{first_relative_path}"
    if (
        _safe_asset_relative_path(
            asset.primary_relative_path, field=f"{logical_id} primary_relative_path"
        )
        != expected_primary
        or not first_relative_path.lower().endswith(".blend")
        or asset.files[0].semantic
        or asset.files[0].dimensions_px is not None
    ):
        raise ForgeInputError(
            f"external semantic acquisition primary Blender file is absent or not first: {logical_id}"
        )

    tree_rows: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    seen_semantics: set[str] = set()
    minimum_texture_size = 4096 if asset.resolution == "4k" else 2048
    for file_index, file in enumerate(asset.files):
        relative_path = _safe_asset_relative_path(
            file.relative_path, field=f"{logical_id} acquired file"
        )
        if relative_path in seen_paths:
            raise ForgeInputError(
                f"external semantic acquisition repeats a file row: {logical_id}"
            )
        seen_paths.add(relative_path)
        if (
            type(file.size_bytes) is not int
            or isinstance(file.size_bytes, bool)
            or file.size_bytes <= 0
            or type(file.sha256) is not str
            or not _SHA256_HEX.fullmatch(file.sha256)
            or type(file.semantic) is not tuple
            or any(type(semantic) is not str for semantic in file.semantic)
            or len(set(file.semantic)) != len(file.semantic)
            or not set(file.semantic).issubset(_ALLOWED_PBR_SEMANTICS)
        ):
            raise ForgeInputError(
                f"external semantic acquisition file metadata is invalid: {logical_id}"
            )
        duplicate_semantics = seen_semantics.intersection(file.semantic)
        if duplicate_semantics:
            raise ForgeInputError(
                f"external semantic acquisition repeats a texture semantic: {logical_id}"
            )
        seen_semantics.update(file.semantic)
        if file.semantic:
            if (
                pathlib.PurePosixPath(relative_path).suffix.lower()
                not in {".jpg", ".jpeg", ".png", ".exr"}
                or type(file.dimensions_px) is not tuple
                or len(file.dimensions_px) != 2
                or any(
                    type(component) is not int
                    or isinstance(component, bool)
                    or component < minimum_texture_size
                    for component in file.dimensions_px
                )
            ):
                raise ForgeInputError(
                    f"external semantic acquisition texture is below requested 4K or invalid: {logical_id}"
                )
        elif file.dimensions_px is not None or file_index != 0:
            raise ForgeInputError(
                f"external semantic acquisition contains an unmapped non-primary file: {logical_id}"
            )
        tree_rows.append(
            {
                "relative_path": relative_path,
                "size_bytes": file.size_bytes,
                "sha256": file.sha256,
            }
        )

    actual_tree_digest = _canonical_metadata_digest(
        tree_rows, label=f"{logical_id} ordered source tree"
    )
    if asset.source_tree_sha256 != actual_tree_digest:
        raise ForgeInputError(
            f"external semantic acquisition source tree digest mismatch: {logical_id}"
        )


def _house_entity_index(house: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    rows = house.get("entities")
    if type(rows) is not list or any(type(row) is not dict for row in rows):
        raise ForgeInputError("HouseSpec entities must be a list of objects")
    return _unique_profile_index(rows, "entity_id", "HouseSpec entities")


def _validate_external_hero_transform(
    *,
    binding: Mapping[str, Any],
    placement: ExternalPlacementSpec,
    entity: Mapping[str, Any],
    asset: AcquiredAsset,
) -> Mapping[str, str]:
    target_id = placement.semantic_target_id
    if type(target_id) is not str:
        raise ForgeInputError("external semantic placement target identity is incomplete")
    pinned = _PINNED_EXTERNAL_HERO_CONTRACTS.get(target_id)
    if (
        pinned is None
        or placement.source_logical_asset_id != pinned["logical_asset_id"]
        or asset.logical_asset_id != pinned["logical_asset_id"]
        or asset.asset_id != pinned["asset_id"]
        or asset.provider_files_hash != pinned["provider_files_hash"]
        or asset.source_tree_sha256 != pinned["source_tree_sha256"]
    ):
        raise ForgeInputError(
            f"external semantic placement is not a pinned hero identity: {target_id}"
        )
    if binding.get("presentation_role") != pinned["presentation_role"]:
        raise ForgeInputError(
            f"external semantic binding presentation role drifted: {target_id}"
        )

    offset = binding.get("transform_offset")
    if type(offset) is not dict or set(offset) != {"location_cm", "rotation_deg", "scale"}:
        raise ForgeInputError(
            f"external semantic binding transform offset is invalid: {target_id}"
        )
    offset_location = _exact_metric_vector(
        offset["location_cm"], field=f"{target_id} transform_offset.location_cm"
    )
    offset_rotation = _exact_metric_vector(
        offset["rotation_deg"], field=f"{target_id} transform_offset.rotation_deg"
    )
    offset_scale = _exact_metric_vector(
        offset["scale"], field=f"{target_id} transform_offset.scale"
    )
    if (
        offset_location != (0.0, 0.0, 0.0)
        or offset_rotation != (0.0, 0.0, 0.0)
        or offset_scale != (1.0, 1.0, 1.0)
    ):
        raise ForgeInputError(
            f"external semantic binding must retain the pinned identity transform: {target_id}"
        )

    transform = entity.get("transform")
    if type(transform) is not dict:
        raise ForgeInputError(f"external semantic HouseSpec target lacks a transform: {target_id}")
    base_location = _exact_metric_vector(
        transform.get("location_m"), field=f"{target_id} HouseSpec location_m"
    )
    base_rotation = _exact_metric_vector(
        transform.get("rotation_deg"), field=f"{target_id} HouseSpec rotation_deg"
    )
    base_scale = _exact_metric_vector(
        transform.get("scale"), field=f"{target_id} HouseSpec scale"
    )
    if (
        tuple(placement.location_m) != base_location
        or tuple(placement.rotation_deg) != base_rotation
        or base_scale != (1.0, 1.0, 1.0)
        or placement.uniform_scale != 1.0
    ):
        raise ForgeInputError(
            f"external semantic placement transform differs from its pinned HouseSpec target: {target_id}"
        )
    return pinned


def _validate_external_source_receipt(
    *,
    binding: Mapping[str, Any],
    receipt: Mapping[str, Any],
    placement: ExternalPlacementSpec,
    asset: AcquiredAsset,
    pinned: Mapping[str, str],
) -> None:
    logical_id = asset.logical_asset_id
    declared_receipt_digest = receipt.get("receipt_digest")
    receipt_body = {key: receipt[key] for key in receipt if key != "receipt_digest"}
    actual_receipt_digest = _canonical_metadata_digest(
        receipt_body, label=f"{logical_id} source receipt"
    )
    if declared_receipt_digest != actual_receipt_digest:
        raise ForgeInputError(
            f"external semantic source receipt digest is stale: {logical_id}"
        )
    expected_identity = {
        "source_kind": "existing_local",
        "source_uri": f"polyhaven://models/{asset.asset_id}",
        "source_digest": asset.source_tree_sha256,
        "source_version": f"files-{asset.provider_files_hash}",
        "logical_asset_id": logical_id,
    }
    for field, expected in expected_identity.items():
        if receipt.get(field) != expected:
            raise ForgeInputError(
                f"external semantic source receipt {field} differs from acquisition: {logical_id}"
            )

    dimensions = asset.catalog_dimensions_m
    if (
        type(dimensions) is not tuple
        or len(dimensions) != 3
        or any(
            type(component) not in {int, float} or isinstance(component, bool)
            for component in dimensions
        )
    ):
        raise ForgeInputError(f"external semantic source lacks provider dimensions: {logical_id}")
    scaled = tuple(float(component) * placement.uniform_scale for component in dimensions)
    if not all(math.isfinite(component) and component > 0 for component in scaled):
        raise ForgeInputError(f"external semantic source dimensions are invalid: {logical_id}")
    # The profile stores raw catalog-derived bounds at the pinned placement
    # scale. Placement output separately stores six-decimal export dimensions.
    expected_min = (-scaled[0] / 2.0, -scaled[1] / 2.0, 0.0)
    expected_max = (scaled[0] / 2.0, scaled[1] / 2.0, scaled[2])
    expected_placement_dimensions = tuple(round(component, 6) for component in scaled)
    if tuple(placement.source_dimensions_m) != expected_placement_dimensions:
        raise ForgeInputError(
            f"external semantic placement dimensions differ from rounded catalog-derived dimensions: {logical_id}"
        )
    bounds = receipt.get("metric_bounds_m")
    if type(bounds) is not dict or set(bounds) != {"min_m", "max_m"}:
        raise ForgeInputError(
            f"external semantic source receipt metric bounds are invalid: {logical_id}"
        )
    actual_min = _exact_metric_vector(
        bounds["min_m"], field=f"{logical_id} metric_bounds_m.min_m"
    )
    actual_max = _exact_metric_vector(
        bounds["max_m"], field=f"{logical_id} metric_bounds_m.max_m"
    )
    if actual_min != expected_min or actual_max != expected_max:
        raise ForgeInputError(
            f"external semantic source receipt bounds differ from raw catalog-derived scaled bounds: {logical_id}"
        )

    license_record = receipt.get("license")
    required_license = {
        "license_id": "CC0-1.0",
        "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
        "entitlement_status": "verified",
        "entitlement_record": f"local-audit://poly-haven-cc0-20260816/{asset.asset_id}",
        "attribution": pinned["attribution"],
        "modification_notice": pinned["modification_notice"],
        "commercial_use": "allowed",
        "redistribution_restriction": "project_policy",
    }
    if type(license_record) is not dict or any(
        license_record.get(field) != expected for field, expected in required_license.items()
    ):
        raise ForgeInputError(
            f"external semantic source receipt lacks verified CC0 provenance: {logical_id}"
        )

    material_inventory = receipt.get("material_inventory")
    slots = (
        material_inventory.get("slots")
        if type(material_inventory) is dict
        else None
    )
    if type(slots) is not list or not slots or any(type(slot) is not dict for slot in slots):
        raise ForgeInputError(
            f"external semantic source receipt material inventory is invalid: {logical_id}"
        )
    expected_semantics = asset.pbr_semantics
    actual_semantics: set[str] = set()
    slot_ids: set[str] = set()
    expected_minimum_size = 4096 if asset.resolution == "4k" else 2048
    for slot in slots:
        slot_id = slot.get("slot_id")
        semantics = slot.get("texture_semantics")
        semantics_valid = type(semantics) is list and all(
            type(semantic) is str for semantic in semantics
        )
        semantic_set = set(semantics) if semantics_valid else set()
        expected_blend_mode = "masked" if "opacity" in semantic_set else "opaque"
        if (
            type(slot_id) is not str
            or not slot_id
            or slot_id in slot_ids
            or not semantics_valid
            or not semantics
            or len(set(semantics)) != len(semantics)
            or actual_semantics.intersection(semantic_set)
            or slot.get("shader_class") != "pbr_metallic_roughness"
            or type(slot.get("minimum_texture_size_px")) is not int
            or slot.get("minimum_texture_size_px") != expected_minimum_size
            or slot.get("blend_mode") != expected_blend_mode
        ):
            raise ForgeInputError(
                f"external semantic source receipt material slot differs from acquisition: {logical_id}"
            )
        slot_ids.add(slot_id)
        actual_semantics.update(semantics)
    semantic_texture_count = sum(1 for file in asset.files if file.semantic)
    if (
        slot_ids != {pinned["slot_id"]}
        or actual_semantics != expected_semantics
        or type(material_inventory.get("texture_count")) is not int
        or material_inventory.get("texture_count") != semantic_texture_count
        or material_inventory.get("all_primitives_material_bound") is not True
    ):
        raise ForgeInputError(
            f"external semantic source receipt material inventory differs from acquisition: {logical_id}"
        )

    import_policy = receipt.get("import_policy")
    required_import_policy = {
        "nanite": "disabled_ineligible",
        "mobility": "static",
        "lod_policy": "single_mesh_measured",
        "collision_policy": "hidden_r1_proxy",
    }
    if type(import_policy) is not dict or any(
        import_policy.get(field) != expected
        for field, expected in required_import_policy.items()
    ):
        raise ForgeInputError(
            f"external semantic source receipt has a non-conservative import policy: {logical_id}"
        )
    if (
        binding.get("collision_policy") != "disabled_use_r1_proxy"
        or binding.get("semantic_authority") != "preserve_parent"
    ):
        raise ForgeInputError(
            f"external semantic binding must preserve r1 semantics and collision: {logical_id}"
        )


def _validate_external_semantic_profile_bindings(
    house: Mapping[str, Any],
    profile: Mapping[str, Any],
    asset_set: ExternalAssetSet,
    external: ExternalPlacementPlan,
) -> None:
    """Bind acquired semantic models to one exact VisualProfile source chain."""

    bindings = _profile_rows(profile, "semantic_visual_bindings")
    receipts = _profile_rows(profile, "asset_source_receipts")
    entities_by_id = _house_entity_index(house)
    _unique_profile_index(bindings, "binding_id", "VisualProfile semantic bindings")
    bindings_by_target = _unique_profile_index(
        bindings, "target_entity_id", "VisualProfile semantic bindings"
    )
    receipts_by_id = _unique_profile_index(
        receipts, "receipt_id", "VisualProfile source receipts"
    )
    _unique_profile_index(
        receipts, "logical_asset_id", "VisualProfile source receipts"
    )

    assets_by_logical: dict[str, AcquiredAsset] = {}
    asset_ids: set[str] = set()
    for asset in asset_set.assets:
        if asset.logical_asset_id in assets_by_logical or asset.asset_id in asset_ids:
            raise ForgeInputError("external acquisition asset identities are duplicated")
        assets_by_logical[asset.logical_asset_id] = asset
        asset_ids.add(asset.asset_id)

    heroes = [
        item
        for item in external.placements
        if item.placement_kind == "semantic_fixed"
        and item.realization_mode == "external_blend"
    ]
    hero_targets: set[str] = set()
    hero_logical_ids: set[str] = set()
    hero_pairs: set[tuple[str, str]] = set()
    used_receipt_ids: set[str] = set()
    for placement in heroes:
        target_id = placement.semantic_target_id
        logical_id = placement.source_logical_asset_id
        if type(target_id) is not str or type(logical_id) is not str:
            raise ForgeInputError("external semantic placement identity is incomplete")
        if target_id in hero_targets or logical_id in hero_logical_ids:
            raise ForgeInputError(
                "external semantic placements must have unique target and source identities"
            )
        hero_targets.add(target_id)
        hero_logical_ids.add(logical_id)
        hero_pairs.add((target_id, logical_id))
    if hero_targets != set(_PINNED_EXTERNAL_HERO_CONTRACTS):
        raise ForgeInputError("external semantic placements differ from the pinned hero set")

    for placement in heroes:
        target_id = placement.semantic_target_id
        logical_id = placement.source_logical_asset_id
        if type(target_id) is not str or type(logical_id) is not str:
            raise ForgeInputError("external semantic placement identity is incomplete")
        binding = bindings_by_target.get(target_id)
        if binding is None:
            raise ForgeInputError(
                f"external semantic placement has no VisualProfile binding: {target_id}"
            )
        if binding.get("logical_asset_id") != logical_id:
            raise ForgeInputError(
                f"external semantic binding logical asset differs from placement: {target_id}"
            )
        asset = assets_by_logical.get(logical_id)
        if asset is None or asset.asset_type != "model":
            raise ForgeInputError(
                f"external semantic binding has no unique acquired model: {logical_id}"
            )
        _validate_acquired_model_metadata(asset)
        entity = entities_by_id.get(target_id)
        if entity is None:
            raise ForgeInputError(
                f"external semantic placement has no HouseSpec target: {target_id}"
            )
        pinned = _validate_external_hero_transform(
            binding=binding,
            placement=placement,
            entity=entity,
            asset=asset,
        )
        receipt_id = binding.get("source_receipt_id")
        if type(receipt_id) is not str or receipt_id in used_receipt_ids:
            raise ForgeInputError(
                f"external semantic binding source receipt is absent or reused: {target_id}"
            )
        used_receipt_ids.add(receipt_id)
        receipt = receipts_by_id.get(receipt_id)
        if receipt is None:
            raise ForgeInputError(
                f"external semantic binding source receipt is missing: {target_id}"
            )
        _validate_external_source_receipt(
            binding=binding,
            receipt=receipt,
            placement=placement,
            asset=asset,
            pinned=pinned,
        )

    acquired_model_ids = {
        logical_id
        for logical_id, asset in assets_by_logical.items()
        if asset.asset_type == "model"
    }
    profile_acquired_pairs: set[tuple[str, str]] = set()
    for binding in bindings:
        logical_id = binding.get("logical_asset_id")
        if logical_id not in acquired_model_ids:
            continue
        target_id = binding.get("target_entity_id")
        if type(target_id) is not str or type(logical_id) is not str:
            raise ForgeInputError("acquired semantic binding identity is incomplete")
        profile_acquired_pairs.add((target_id, logical_id))
    if profile_acquired_pairs != hero_pairs:
        raise ForgeInputError(
            "external semantic profile bindings and realized acquired heroes differ"
        )

    architecture_profile = profile.get("architecture_profile")
    if type(architecture_profile) is not dict:
        raise ForgeInputError("VisualProfile architecture_profile must be an object")
    architecture_receipt_id = architecture_profile.get("source_receipt_id")
    if type(architecture_receipt_id) is not str or not architecture_receipt_id:
        raise ForgeInputError("VisualProfile architecture source receipt identity is invalid")
    referenced_receipt_ids = {architecture_receipt_id}
    for label, rows in (
        ("semantic binding", bindings),
        ("dressing instance", _profile_rows(profile, "dressing_instances")),
    ):
        for row in rows:
            receipt_id = row.get("source_receipt_id")
            if type(receipt_id) is not str or not receipt_id:
                raise ForgeInputError(f"VisualProfile {label} source receipt identity is invalid")
            referenced_receipt_ids.add(receipt_id)
    if set(receipts_by_id) != referenced_receipt_ids:
        raise ForgeInputError(
            "VisualProfile source receipts and closed-world references differ"
        )


def build_external_forge_plan(
    house: Mapping[str, Any],
    profile: Mapping[str, Any],
    asset_set: ExternalAssetSet,
    placement_manifest: PlacementManifestDocument,
) -> ExternalForgePlan:
    """Layer verified external presentation on the unchanged v1 architecture."""

    base = build_forge_plan(house, profile)
    external = build_external_placement_plan(
        house,
        base.rooms,
        base.dressing,
        asset_set,
        placement_manifest,
    )
    _validate_external_semantic_profile_bindings(house, profile, asset_set, external)
    payload = {
        "schema_version": EXTERNAL_FORGE_SCHEMA_VERSION,
        "forge_id": base.forge_id,
        "house_revision": base.house_revision,
        "visual_profile_id": base.visual_profile_id,
        "seed": base.seed,
        "rooms": base.rooms,
        "openings": base.openings,
        "components": base.components,
        "dressing": base.dressing,
        "material_plan": base.material_plan,
        "source_house_digest": base.source_house_digest,
        "source_profile_digest": base.source_profile_digest,
        "external_placement": external,
    }
    return ExternalForgePlan(
        schema_version=EXTERNAL_FORGE_SCHEMA_VERSION,
        forge_id=base.forge_id,
        house_revision=base.house_revision,
        visual_profile_id=base.visual_profile_id,
        seed=base.seed,
        rooms=base.rooms,
        openings=base.openings,
        components=base.components,
        dressing=base.dressing,
        material_plan=base.material_plan,
        source_house_digest=base.source_house_digest,
        source_profile_digest=base.source_profile_digest,
        content_digest=content_digest(payload),
        external_placement=external,
    )
