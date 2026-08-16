"""Pure external-furniture placement planning for the realism forge v2."""

from __future__ import annotations

import hashlib
import math
import pathlib
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .config import ForgeInputError, canonical_json_bytes, content_digest, normalized, vector3
from .external_assets import (
    AUTHORED_RECIPE_MATERIAL_IDS,
    AcquiredAsset,
    ExternalAssetSet,
    asset_digest_record,
    external_source_selected_dimensions_m,
)


PLACEMENT_SCHEMA_VERSION = "simworld.vista.playable-home-external-placement/v1"
EXTERNAL_FORGE_SCHEMA_VERSION = "simworld.vista.playable-home-realism-forge/v2"
NORMALIZATION_POLICY = "measured_combined_bounds_floor_center_uniform_scale_v1"
_ID = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_FIXED_TARGET_CATEGORIES = {
    "shoe_bench": "entry_hall",
    "sofa": "living_room",
    "coffee_table": "living_room",
    "stove": "kitchen_dining",
    "dining_table": "kitchen_dining",
}
_FORBIDDEN_TARGET_CATEGORIES = {
    "keys", "phone", "coffee_cup", "exit_door", "interior_door",
    "resident", "npc", "pot", "slipper", "spill_marker", "fire_marker",
}
_AUTHORED_RECIPES = frozenset(AUTHORED_RECIPE_MATERIAL_IDS)
_EXTERNAL_ANCHOR_CATEGORIES = {
    "reading_corner": frozenset({"armchair", "side_table", "decorative_object"}),
    "media_console": frozenset({"media_cabinet"}),
    "dining_center": frozenset({"dining_chair", "fruit"}),
}


@dataclass(frozen=True)
class PlacementManifestDocument:
    payload: Mapping[str, Any]
    file_sha256: str


@dataclass(frozen=True)
class PlacementAabb:
    min_m: tuple[float, float, float]
    max_m: tuple[float, float, float]


@dataclass(frozen=True)
class ExternalPlacementSpec:
    placement_id: str
    placement_kind: str
    room_id: str
    room_kind: str
    category: str
    realization_mode: str
    semantic_target_id: str | None
    anchor_id: str | None
    support_placement_id: str | None
    source_logical_asset_id: str | None
    geometry_recipe: str | None
    material_logical_asset_ids: tuple[str, ...]
    location_m: tuple[float, float, float]
    rotation_deg: tuple[float, float, float]
    uniform_scale: float
    source_dimensions_m: tuple[float, float, float]
    room_local_aabb: PlacementAabb
    source_tree_sha256: str | None


@dataclass(frozen=True)
class ExternalPlacementPlan:
    schema_version: str
    placement_id: str
    normalization_policy: str
    acquisition_receipt: Mapping[str, Any]
    placement_manifest_sha256: str
    semantic_target_ids: tuple[str, ...]
    dressing_ids: tuple[str, ...]
    asset_sources: tuple[Mapping[str, Any], ...]
    placements: tuple[ExternalPlacementSpec, ...]
    content_digest: str


def _closed(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise ForgeInputError(f"{label} fields differ from the closed placement contract")
    return dict(value)


def load_placement_manifest(path: pathlib.Path) -> PlacementManifestDocument:
    path = pathlib.Path(path)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ForgeInputError("external placement manifest must be an absolute non-symlink file")
    resolved = path.resolve(strict=True)
    if pathlib.Path(path.absolute()) != resolved:
        raise ForgeInputError("external placement manifest may not traverse symbolic links")
    raw = path.read_bytes()
    import json

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ForgeInputError(f"external placement manifest contains duplicate key {key!r}")
            result[key] = value
        return result

    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, OSError) as error:
        raise ForgeInputError(f"cannot read external placement manifest: {error}") from error
    if type(payload) is not dict:
        raise ForgeInputError("external placement manifest must contain one JSON object")
    return PlacementManifestDocument(payload, hashlib.sha256(raw).hexdigest())


def placement_manifest_document(payload: Mapping[str, Any]) -> PlacementManifestDocument:
    """Construct an in-memory document for deterministic pure tests."""

    return PlacementManifestDocument(dict(payload), hashlib.sha256(canonical_json_bytes(payload)).hexdigest())


def _entity_rows(house: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    rows = house.get("entities", [])
    if not isinstance(rows, list):
        raise ForgeInputError("HouseSpec entities must be a list")
    result = {
        str(item.get("entity_id")): item
        for item in rows
        if isinstance(item, Mapping) and isinstance(item.get("entity_id"), str)
    }
    if len(result) != len([item for item in rows if isinstance(item, Mapping)]):
        raise ForgeInputError("HouseSpec entity identities are invalid or duplicated")
    return result


def _aabb(
    location: Sequence[float], dimensions: Sequence[float], rotation_deg: Sequence[float]
) -> PlacementAabb:
    if abs(float(rotation_deg[0])) > 1e-6 or abs(float(rotation_deg[1])) > 1e-6:
        raise ForgeInputError("external placements must remain upright")
    angle = math.radians(float(rotation_deg[2]))
    x = abs(math.cos(angle)) * float(dimensions[0]) + abs(math.sin(angle)) * float(dimensions[1])
    y = abs(math.sin(angle)) * float(dimensions[0]) + abs(math.cos(angle)) * float(dimensions[1])
    return PlacementAabb(
        min_m=(float(location[0]) - x / 2, float(location[1]) - y / 2, float(location[2])),
        max_m=(
            float(location[0]) + x / 2,
            float(location[1]) + y / 2,
            float(location[2]) + float(dimensions[2]),
        ),
    )


def aabbs_overlap(left: PlacementAabb, right: PlacementAabb, *, tolerance: float = 0.005) -> bool:
    return all(
        left.min_m[index] < right.max_m[index] - tolerance
        and left.max_m[index] > right.min_m[index] + tolerance
        for index in range(3)
    )


def _inside_room(aabb: PlacementAabb, room: Any, tolerance: float = 0.005) -> bool:
    return all(
        aabb.min_m[index] >= room.bounds_min_m[index] - tolerance
        and aabb.max_m[index] <= room.bounds_max_m[index] + tolerance
        for index in range(3)
    )


def _intersects_exclusion(aabb: PlacementAabb, volume: Any) -> bool:
    return aabbs_overlap(aabb, PlacementAabb(tuple(volume.min_m), tuple(volume.max_m)))


def _source_dimensions(asset: AcquiredAsset, uniform_scale: float) -> tuple[float, float, float]:
    try:
        # Synthetic planning fixtures and future acquisition candidates may
        # not yet have a retained Blender selection policy.  Staticization is
        # the strict policy gate; planning uses the catalog envelope unless
        # the exact retained source has a selected-object override.
        dimensions = external_source_selected_dimensions_m(
            asset,
            require_exact_policy=False,
        )
    except RuntimeError as error:
        raise ForgeInputError(str(error)) from error
    return tuple(round(value * uniform_scale, 6) for value in dimensions)  # type: ignore[return-value]


def _manifest_digest_valid(payload: Mapping[str, Any]) -> bool:
    declared = payload.get("content_digest")
    body = {key: payload[key] for key in payload if key != "content_digest"}
    return isinstance(declared, str) and declared == content_digest(body)


def build_external_placement_plan(
    house: Mapping[str, Any],
    rooms: Sequence[Any],
    dressing_plan: Any,
    asset_set: ExternalAssetSet,
    document: PlacementManifestDocument,
) -> ExternalPlacementPlan:
    root = _closed(
        document.payload,
        {"schema_version", "placement_id", "acquisition", "placements", "content_digest"},
        "external placement manifest",
    )
    if root["schema_version"] != PLACEMENT_SCHEMA_VERSION or not _manifest_digest_valid(root):
        raise ForgeInputError("external placement schema or content_digest is invalid")
    if type(root["placement_id"]) is not str or not _ID.fullmatch(root["placement_id"]):
        raise ForgeInputError("external placement_id is invalid")
    acquisition = _closed(
        root["acquisition"],
        {
            "provider", "receipt_filename", "receipt_schema_version",
            "receipt_digest", "receipt_file_sha256", "acquisition_manifest_sha256",
        },
        "external placement acquisition",
    )
    expected_acquisition = {
        **asset_set.receipt_reference(),
        "receipt_filename": "acquisition-receipt.json",
    }
    if acquisition != expected_acquisition:
        raise ForgeInputError("external placement does not bind the exact acquisition receipt")
    rows = root["placements"]
    if type(rows) is not list or not rows:
        raise ForgeInputError("external placement manifest has no placements")
    room_by_kind = {room.kind: room for room in rooms}
    entity_by_id = _entity_rows(house)
    anchor_by_id = {anchor.anchor_id: anchor for anchor in dressing_plan.anchors}
    placements: list[ExternalPlacementSpec] = []
    used_sources: set[str] = set()
    for index, raw in enumerate(rows):
        row = _closed(
            raw,
            {
                "placement_id", "placement_kind", "room_kind", "category",
                "realization_mode", "semantic_target_id", "anchor_id",
                "support_placement_id", "source_logical_asset_id", "geometry_recipe",
                "material_logical_asset_ids", "location_offset_m", "rotation_offset_deg",
                "uniform_scale", "authored_dimensions_m",
            },
            f"external placements[{index}]",
        )
        placement_id = row["placement_id"]
        if type(placement_id) is not str or not _ID.fullmatch(placement_id):
            raise ForgeInputError(f"external placement ID is invalid at index {index}")
        room = room_by_kind.get(row["room_kind"])
        if room is None:
            raise ForgeInputError(f"external placement references an unfinished room: {placement_id}")
        kind = row["placement_kind"]
        mode = row["realization_mode"]
        if kind not in {"semantic_fixed", "dressing"} or mode not in {"project_authored", "external_blend"}:
            raise ForgeInputError(f"external placement kind/mode is invalid: {placement_id}")
        offset = vector3(row["location_offset_m"], field=f"{placement_id} location_offset_m")
        rotation_offset = vector3(row["rotation_offset_deg"], field=f"{placement_id} rotation_offset_deg")
        scale = row["uniform_scale"]
        if type(scale) not in {int, float} or isinstance(scale, bool) or not math.isfinite(float(scale)) or not 0.1 <= float(scale) <= 4.0:
            raise ForgeInputError(f"external placement scale is invalid: {placement_id}")
        semantic_target_id: str | None = None
        anchor_id: str | None = None
        if kind == "semantic_fixed":
            if row["anchor_id"] is not None or type(row["semantic_target_id"]) is not str:
                raise ForgeInputError(f"semantic placement base is invalid: {placement_id}")
            semantic_target_id = row["semantic_target_id"]
            entity = entity_by_id.get(semantic_target_id)
            if entity is None or entity.get("category") in _FORBIDDEN_TARGET_CATEGORIES:
                raise ForgeInputError(f"semantic placement targets a missing/movable/forbidden entity: {placement_id}")
            category = str(entity.get("category"))
            if category not in _FIXED_TARGET_CATEGORIES or _FIXED_TARGET_CATEGORIES[category] != room.kind:
                raise ForgeInputError(f"semantic placement is not one of the fixed hero targets: {placement_id}")
            if entity.get("room_id") != room.room_id or row["category"] != category:
                raise ForgeInputError(f"semantic placement room/category differs from HouseSpec: {placement_id}")
            transform = entity.get("transform")
            if not isinstance(transform, Mapping):
                raise ForgeInputError(f"semantic target lacks a transform: {placement_id}")
            base_location = vector3(transform.get("location_m", ()), field="semantic location_m")
            base_rotation = vector3(transform.get("rotation_deg", ()), field="semantic rotation_deg")
        else:
            if row["semantic_target_id"] is not None or type(row["anchor_id"]) is not str:
                raise ForgeInputError(f"dressing placement base is invalid: {placement_id}")
            anchor_id = row["anchor_id"]
            anchor = anchor_by_id.get(anchor_id)
            if anchor is None or anchor.room_id != room.room_id:
                raise ForgeInputError(f"dressing placement references an invalid room anchor: {placement_id}")
            short_anchor = anchor.anchor_id.rsplit(".", 1)[-1]
            allowed_categories = set(anchor.allowed_categories) | set(
                _EXTERNAL_ANCHOR_CATEGORIES.get(short_anchor, ())
            )
            if row["category"] not in allowed_categories:
                raise ForgeInputError(f"dressing category is not allowed at its anchor: {placement_id}")
            base_location = anchor.location_m
            base_rotation = (0.0, 0.0, anchor.deterministic_yaw_deg)
        location = tuple(base_location[i] + offset[i] for i in range(3))
        rotation = tuple(base_rotation[i] + rotation_offset[i] for i in range(3))
        materials = row["material_logical_asset_ids"]
        if type(materials) is not list or any(type(item) is not str for item in materials):
            raise ForgeInputError(f"external placement material list is invalid: {placement_id}")
        source_id = row["source_logical_asset_id"]
        recipe = row["geometry_recipe"]
        source_tree: str | None
        if mode == "project_authored":
            expected_materials = (
                AUTHORED_RECIPE_MATERIAL_IDS.get(recipe) if type(recipe) is str else None
            )
            if (
                source_id is not None
                or type(recipe) is not str
                or recipe not in _AUTHORED_RECIPES
                or expected_materials is None
                or tuple(materials) != expected_materials
            ):
                raise ForgeInputError(f"project-authored placement source is invalid: {placement_id}")
            dimensions = vector3(row["authored_dimensions_m"], field=f"{placement_id} authored_dimensions_m")
            if any(value <= 0 for value in dimensions) or abs(float(scale) - 1.0) > 1e-6:
                raise ForgeInputError(f"project-authored dimensions/scale are invalid: {placement_id}")
            for logical_id in materials:
                material_asset = asset_set.asset(logical_id)
                if material_asset.asset_type != "texture" or material_asset.resolution != "4k":
                    raise ForgeInputError(f"project-authored hero material is not acquired 4K PBR: {logical_id}")
                used_sources.add(logical_id)
            source_tree = None
        else:
            if type(source_id) is not str or recipe is not None or materials or row["authored_dimensions_m"] is not None:
                raise ForgeInputError(f"external Blender placement source is invalid: {placement_id}")
            asset = asset_set.asset(source_id)
            if asset.asset_type != "model" or (kind == "semantic_fixed" and asset.resolution != "4k") or (kind == "dressing" and asset.resolution != "2k"):
                raise ForgeInputError(f"external model resolution/role is invalid: {placement_id}")
            dimensions = _source_dimensions(asset, float(scale))
            used_sources.add(source_id)
            source_tree = asset.source_tree_sha256
        aabb = _aabb(location, dimensions, rotation)
        if not _inside_room(aabb, room):
            raise ForgeInputError(f"external placement leaves room-local bounds: {placement_id}")
        support = row["support_placement_id"]
        if support is not None and (type(support) is not str or not _ID.fullmatch(support)):
            raise ForgeInputError(f"external support placement ID is invalid: {placement_id}")
        placements.append(
            ExternalPlacementSpec(
                placement_id=placement_id,
                placement_kind=kind,
                room_id=room.room_id,
                room_kind=room.kind,
                category=str(row["category"]),
                realization_mode=mode,
                semantic_target_id=semantic_target_id,
                anchor_id=anchor_id,
                support_placement_id=support,
                source_logical_asset_id=source_id,
                geometry_recipe=recipe,
                material_logical_asset_ids=tuple(materials),
                location_m=location,  # type: ignore[arg-type]
                rotation_deg=rotation,  # type: ignore[arg-type]
                uniform_scale=float(scale),
                source_dimensions_m=dimensions,
                room_local_aabb=aabb,
                source_tree_sha256=source_tree,
            )
        )
    placements.sort(key=lambda item: item.placement_id)
    if len({item.placement_id for item in placements}) != len(placements):
        raise ForgeInputError("external placement IDs are duplicated")
    by_id = {item.placement_id: item for item in placements}
    for item in placements:
        if item.support_placement_id is not None:
            support = by_id.get(item.support_placement_id)
            if support is None or support.room_id != item.room_id or support.placement_id == item.placement_id:
                raise ForgeInputError(f"external placement has an invalid support: {item.placement_id}")
            if abs(item.room_local_aabb.min_m[2] - support.room_local_aabb.max_m[2]) > 0.10:
                raise ForgeInputError(f"external placement is not on its declared support: {item.placement_id}")
            center_xy = tuple(
                (item.room_local_aabb.min_m[index] + item.room_local_aabb.max_m[index]) / 2
                for index in (0, 1)
            )
            if any(
                center_xy[index] < support.room_local_aabb.min_m[index] - 0.05
                or center_xy[index] > support.room_local_aabb.max_m[index] + 0.05
                for index in (0, 1)
            ):
                raise ForgeInputError(f"external placement is outside its declared support: {item.placement_id}")
        if item.placement_kind == "dressing":
            blockers = [
                volume.exclusion_id
                for volume in dressing_plan.exclusions
                if volume.room_id == item.room_id and _intersects_exclusion(item.room_local_aabb, volume)
            ]
            if blockers:
                raise ForgeInputError(f"external dressing intersects protected exclusions: {item.placement_id}: {blockers}")
    for item in placements:
        visited = {item.placement_id}
        current = item
        while current.support_placement_id is not None:
            if current.support_placement_id in visited:
                raise ForgeInputError(f"external placement support cycle exists: {item.placement_id}")
            visited.add(current.support_placement_id)
            current = by_id[current.support_placement_id]
    for index, left in enumerate(placements):
        for right in placements[index + 1 :]:
            if left.room_id != right.room_id or not aabbs_overlap(left.room_local_aabb, right.room_local_aabb):
                continue
            if left.support_placement_id == right.placement_id or right.support_placement_id == left.placement_id:
                continue
            raise ForgeInputError(f"external placement AABBs overlap: {left.placement_id}, {right.placement_id}")
    semantic_ids = tuple(sorted(item.semantic_target_id for item in placements if item.semantic_target_id))
    expected_targets = tuple(
        sorted(
            entity_id
            for entity_id, entity in entity_by_id.items()
            if entity.get("category") in _FIXED_TARGET_CATEGORIES
            and entity.get("room_id") in {room.room_id for room in rooms}
        )
    )
    if semantic_ids != expected_targets:
        raise ForgeInputError("external mode must realize exactly the five fixed semantic hero targets")
    dressing_ids = tuple(sorted(item.placement_id for item in placements if item.placement_kind == "dressing"))
    if not dressing_ids:
        raise ForgeInputError("external mode must include non-interactive acquired dressing")
    sources = tuple(asset_digest_record(asset_set.asset(logical_id)) for logical_id in sorted(used_sources))
    payload = {
        "schema_version": PLACEMENT_SCHEMA_VERSION,
        "placement_id": root["placement_id"],
        "normalization_policy": NORMALIZATION_POLICY,
        "acquisition_receipt": asset_set.receipt_reference(),
        "placement_manifest_sha256": document.file_sha256,
        "semantic_target_ids": semantic_ids,
        "dressing_ids": dressing_ids,
        "asset_sources": sources,
        "placements": placements,
    }
    return ExternalPlacementPlan(
        **payload,
        content_digest=content_digest(payload),
    )


def bundle_external_content(plan: ExternalPlacementPlan, room_id: str) -> dict[str, Any]:
    room_placements = [item for item in plan.placements if item.room_id == room_id]
    source_ids = {
        logical_id
        for item in room_placements
        for logical_id in (
            ((item.source_logical_asset_id,) if item.source_logical_asset_id else ())
            + item.material_logical_asset_ids
        )
    }
    sources = [item for item in plan.asset_sources if item["logical_asset_id"] in source_ids]
    return normalized(
        {
            "schema_version": PLACEMENT_SCHEMA_VERSION,
            "normalization_policy": plan.normalization_policy,
            "acquisition_receipt": plan.acquisition_receipt,
            "placement_manifest_sha256": plan.placement_manifest_sha256,
            "placement_plan_sha256": plan.content_digest,
            "semantic_target_ids": sorted(
                item.semantic_target_id for item in room_placements if item.semantic_target_id
            ),
            "dressing_ids": sorted(
                item.placement_id for item in room_placements if item.placement_kind == "dressing"
            ),
            "asset_sources": sources,
        }
    )
