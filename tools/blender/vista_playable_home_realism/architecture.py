"""Pure, deterministic architectural planning for the three-room r2 slice."""

from __future__ import annotations

import math
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
from .external_assets import ExternalAssetSet
from .materials import material_by_id, material_plan_manifest
from .placement import (
    EXTERNAL_FORGE_SCHEMA_VERSION,
    ExternalPlacementPlan,
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
