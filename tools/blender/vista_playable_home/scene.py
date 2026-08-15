"""Pure-Python geometry primitives and material palette.

This module intentionally contains no room topology or semantic inventory.
The approved HouseSpec is the only authority for those values; geometry here
is local to an asset and can be tested without importing :mod:`bpy`.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Sequence


HOUSE_ID = "home.r1"
REVISION = "vista_playable_home_r1"
GRID_M = 0.1


def _v3(values: Sequence[float]) -> tuple[float, float, float]:
    if len(values) != 3:
        raise ValueError("expected a three-element vector")
    result = tuple(float(value) for value in values)
    if not all(math.isfinite(value) for value in result):
        raise ValueError("vectors must be finite")
    return result  # type: ignore[return-value]


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _normalize(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if isinstance(value, float):
        rounded = round(value, 6)
        if rounded == 0.0:
            return 0
        return int(rounded) if rounded.is_integer() else rounded
    return value


def canonical_json_bytes(value: Any) -> bytes:
    return (json.dumps(_normalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


@dataclass(frozen=True)
class MaterialPlan:
    material_id: str
    base_color: tuple[float, float, float, float]
    roughness: float
    metallic: float = 0.0


@dataclass(frozen=True)
class PrimitivePlan:
    primitive_id: str
    kind: str
    material_id: str
    location_m: tuple[float, float, float]
    dimensions_m: tuple[float, float, float]
    rotation_deg: tuple[float, float, float] = (0.0, 0.0, 0.0)
    radius_m: float | None = None
    major_radius_m: float | None = None
    minor_radius_m: float | None = None
    grid_bounds_m: tuple[tuple[float, float, float], tuple[float, float, float]] | None = None


@dataclass(frozen=True)
class RoomPlan:
    room_id: str
    kind: str
    label: str
    transform_location_m: tuple[float, float, float]
    bounds_min_m: tuple[float, float, float]
    bounds_max_m: tuple[float, float, float]
    anchor_m: tuple[float, float, float]
    review_cameras: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class PreviewView:
    view_id: str
    location_m: tuple[float, float, float]
    target_m: tuple[float, float, float]
    lens_mm: float
    width: int = 960
    height: int = 720


def box(
    primitive_id: str,
    dimensions_m: Sequence[float],
    location_m: Sequence[float],
    material_id: str,
    *,
    rotation_deg: Sequence[float] = (0.0, 0.0, 0.0),
    grid_bounds_m: tuple[Sequence[float], Sequence[float]] | None = None,
) -> PrimitivePlan:
    bounds = None if grid_bounds_m is None else (_v3(grid_bounds_m[0]), _v3(grid_bounds_m[1]))
    return PrimitivePlan(primitive_id, "box", material_id, _v3(location_m), _v3(dimensions_m), _v3(rotation_deg), grid_bounds_m=bounds)


def cylinder(
    primitive_id: str,
    radius_m: float,
    depth_m: float,
    location_m: Sequence[float],
    material_id: str,
    *,
    rotation_deg: Sequence[float] = (0.0, 0.0, 0.0),
) -> PrimitivePlan:
    return PrimitivePlan(
        primitive_id,
        "cylinder",
        material_id,
        _v3(location_m),
        (radius_m * 2, radius_m * 2, float(depth_m)),
        _v3(rotation_deg),
        radius_m=float(radius_m),
    )


def sphere(primitive_id: str, radius_m: float, location_m: Sequence[float], material_id: str) -> PrimitivePlan:
    return PrimitivePlan(
        primitive_id,
        "sphere",
        material_id,
        _v3(location_m),
        (radius_m * 2,) * 3,
        radius_m=float(radius_m),
    )


def torus(
    primitive_id: str,
    major_radius_m: float,
    minor_radius_m: float,
    location_m: Sequence[float],
    material_id: str,
    *,
    rotation_deg: Sequence[float] = (0.0, 0.0, 0.0),
) -> PrimitivePlan:
    diameter = 2 * (major_radius_m + minor_radius_m)
    return PrimitivePlan(
        primitive_id,
        "torus",
        material_id,
        _v3(location_m),
        (diameter, diameter, minor_radius_m * 2),
        _v3(rotation_deg),
        major_radius_m=float(major_radius_m),
        minor_radius_m=float(minor_radius_m),
    )


def _materials() -> tuple[MaterialPlan, ...]:
    values = {
        "wall_warm_white": ((0.72, 0.69, 0.62, 1.0), 0.82, 0.0),
        "ceiling_white": ((0.80, 0.82, 0.80, 1.0), 0.90, 0.0),
        "floor_hall_slate": ((0.13, 0.17, 0.19, 1.0), 0.48, 0.12),
        "floor_living_oak": ((0.34, 0.19, 0.09, 1.0), 0.62, 0.0),
        "floor_kitchen_terrazzo": ((0.48, 0.50, 0.46, 1.0), 0.38, 0.05),
        "floor_bedroom_carpet": ((0.34, 0.20, 0.17, 1.0), 0.96, 0.0),
        "floor_office_cork": ((0.31, 0.25, 0.16, 1.0), 0.82, 0.0),
        "floor_bathroom_tile": ((0.13, 0.31, 0.35, 1.0), 0.32, 0.04),
        "wood_walnut": ((0.19, 0.075, 0.035, 1.0), 0.50, 0.0),
        "wood_oak": ((0.52, 0.29, 0.11, 1.0), 0.58, 0.0),
        "fabric_navy": ((0.025, 0.09, 0.15, 1.0), 0.94, 0.0),
        "fabric_blue": ((0.08, 0.25, 0.34, 1.0), 0.92, 0.0),
        "fabric_ochre": ((0.72, 0.34, 0.07, 1.0), 0.90, 0.0),
        "fabric_cream": ((0.80, 0.72, 0.59, 1.0), 0.92, 0.0),
        "fabric_terracotta": ((0.56, 0.16, 0.08, 1.0), 0.90, 0.0),
        "fabric_terracotta_dark": ((0.35, 0.08, 0.04, 1.0), 0.94, 0.0),
        "paint_sage": ((0.23, 0.39, 0.28, 1.0), 0.67, 0.0),
        "paint_sage_light": ((0.48, 0.60, 0.48, 1.0), 0.72, 0.0),
        "paint_blue_gray": ((0.13, 0.23, 0.30, 1.0), 0.57, 0.08),
        "paint_blue_gray_light": ((0.24, 0.36, 0.43, 1.0), 0.62, 0.05),
        "door_blue_gray": ((0.11, 0.24, 0.32, 1.0), 0.52, 0.06),
        "metal_dark": ((0.025, 0.035, 0.04, 1.0), 0.31, 0.82),
        "metal_brushed": ((0.47, 0.50, 0.50, 1.0), 0.24, 0.90),
        "metal_brass": ((0.59, 0.36, 0.08, 1.0), 0.25, 0.82),
        "rubber_dark": ((0.012, 0.016, 0.017, 1.0), 0.86, 0.0),
        "stone_light": ((0.58, 0.58, 0.53, 1.0), 0.35, 0.02),
        "appliance_white": ((0.62, 0.65, 0.63, 1.0), 0.30, 0.42),
        "appliance_dark": ((0.055, 0.07, 0.075, 1.0), 0.30, 0.54),
        "stove_black": ((0.009, 0.013, 0.016, 1.0), 0.18, 0.45),
        "screen_black": ((0.006, 0.01, 0.015, 1.0), 0.20, 0.28),
        "screen_glass": ((0.025, 0.07, 0.10, 1.0), 0.08, 0.18),
        "ceramic_white": ((0.82, 0.83, 0.78, 1.0), 0.20, 0.0),
        "ceramic_cream": ((0.74, 0.66, 0.50, 1.0), 0.31, 0.0),
        "water_blue": ((0.04, 0.28, 0.36, 1.0), 0.12, 0.0),
        "cardboard": ((0.50, 0.27, 0.08, 1.0), 0.90, 0.0),
        "cardboard_light": ((0.66, 0.40, 0.14, 1.0), 0.87, 0.0),
        "paper_label": ((0.85, 0.82, 0.70, 1.0), 0.90, 0.0),
        "coffee_dark": ((0.06, 0.018, 0.006, 1.0), 0.25, 0.0),
    }
    return tuple(MaterialPlan(name, color, roughness, metallic) for name, (color, roughness, metallic) in sorted(values.items()))
