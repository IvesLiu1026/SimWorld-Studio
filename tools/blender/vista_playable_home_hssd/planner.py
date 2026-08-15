"""Pure-Python HSSD catalog selection and closed binding contracts.

No Blender APIs are imported here.  Selection is driven by the approved
normalized Blender manifest and HSSD's own semantic/dimension metadata.  The
same input bytes therefore produce the same source-object choices regardless
of filesystem enumeration order.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import pathlib
import re
import struct
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping, Sequence


BINDING_PLAN_SCHEMA = "simworld.vista.playable-home-hssd-binding-plan/v1"
BUILT_MANIFEST_SCHEMA = "simworld.vista.playable-home-hssd-attribution/v1"
NORMALIZED_MANIFEST_SCHEMA = "simworld.vista.playable-home-blender-manifest/v1"
HSSD_LICENSE_SPDX = "CC-BY-NC-4.0"
HSSD_LICENSE_URL = "https://creativecommons.org/licenses/by-nc/4.0/"
HSSD_PROJECT_URL = "https://3dlg-hcvc.github.io/hssd/"
HSSD_DATASET_NAME = "Habitat Synthetic Scenes Dataset (HSSD)"
PINNED_HSSD_README_SHA256 = "4509914d584031173390bf5f41722ec25e19de3f1e0ea54a423eadf63073d49c"
SELECTION_POLICY_VERSION = "hssd-pbr-dimension-fit-v1"

_OBJECT_ID_RE = re.compile(r"[0-9a-f]{40}")
_ASSET_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_GIT_SHA_RE = re.compile(r"[0-9a-f]{40}")
_GLB_JSON_CHUNK = 0x4E4F534A
_MAX_GLB_JSON_BYTES = 32 * 1024 * 1024
_MAX_INSPECTED_PER_TARGET = 256
_MIN_TRIANGLES = 50
_MAX_AXIS_SCALE_ANISOTROPY = 2.75
_MIN_UNIFORM_SCALE = 0.10
_MAX_UNIFORM_SCALE = 10.0


class HssdBindingError(ValueError):
    """A source, licensing, containment, or binding contract failed closed."""


@dataclass(frozen=True)
class CategoryRule:
    aliases: tuple[str, ...]


# Exact aliases from metadata/hssd_obj_semantics_condensed.csv.  Generic
# classes (table/seat) are disambiguated by target dimensions, not filenames.
CATEGORY_RULES: dict[str, CategoryRule] = {
    "backpack": CategoryRule(("bag",)),
    "bathtub": CategoryRule(("bathtub",)),
    "bed": CategoryRule(("bed", "bed_small")),
    "cabinet": CategoryRule(("file_cabinet", "storage_cabinet", "cabinet")),
    "cardboard_box": CategoryRule(("box", "storage_box")),
    "chair": CategoryRule(("seat", "semi_chair")),
    "clothes": CategoryRule(("clothes", "cloth", "hanging_clothes")),
    "coffee_cup": CategoryRule(("cup",)),
    "coffee_table": CategoryRule(("table",)),
    "desk": CategoryRule(("desk", "computer_desk", "drawer_desk", "wall_desk")),
    "dining_table": CategoryRule(("dinner_table", "table")),
    "faucet": CategoryRule(("tap", "shower_tap")),
    "fridge": CategoryRule(("fridge",)),
    "lamp": CategoryRule(("lamp",)),
    "ladder": CategoryRule(("ladder",)),
    "laundry_basket": CategoryRule(("laundry_basket", "basket")),
    "nightstand": CategoryRule(("nightstand", "bed_table")),
    "phone": CategoryRule(("phone",)),
    "plant": CategoryRule(("plant",)),
    "pot": CategoryRule(("pot",)),
    "rolling_chair": CategoryRule(("seat", "semi_chair")),
    "shelf": CategoryRule(("shelf", "kitchen_shelf", "bathroom_shelf")),
    "shoe_bench": CategoryRule(("bench",)),
    "sink": CategoryRule(("sink", "bath_sink", "sink/basin")),
    "slipper": CategoryRule(("shoe", "shoes")),
    "sofa": CategoryRule(("couch", "sofa_set")),
    "stove": CategoryRule(("stove", "stovetop")),
    "table": CategoryRule(("table",)),
    "toilet": CategoryRule(("toilet",)),
    "tv": CategoryRule(("tv", "led_tv")),
    "washer": CategoryRule(("washer/dryer", "washing_machine_and_dryer")),
}

# These assets stay bound to the existing deterministic forge.  The reasons
# are part of the closed manifest so preservation cannot become a silent
# fallback.  In particular, HSSD has no verified loose-key semantic class.
PRESERVE_CATEGORY_REASONS: dict[str, str] = {
    "exit_door": "gameplay_door_uses_procedural_collision_and_hinge_contract",
    "fire_marker": "event_overlay_marker_is_not_a_photoreal_prop",
    "interior_door": "gameplay_door_uses_procedural_collision_and_hinge_contract",
    "keys": "hssd_has_no_verified_loose_key_semantic_category",
    "overflow_marker": "event_overlay_marker_is_not_a_photoreal_prop",
    "resident": "runtime_character_is_supplied_by_unreal",
    "spill_marker": "event_overlay_marker_is_not_a_photoreal_prop",
}


@dataclass(frozen=True)
class TargetAsset:
    asset_id: str
    category: str
    component_role: str
    target_dimensions_m: tuple[float, float, float]
    target_bounds_m: tuple[tuple[float, float, float], tuple[float, float, float]]
    source_entity_ids: tuple[str, ...]


@dataclass(frozen=True)
class Candidate:
    object_id: str
    semantic_category: str
    alias_priority: int
    name: str
    source_relpath: str
    config_relpath: str
    source_dimensions_habitat_m: tuple[float, float, float]
    source_dimensions_blender_m: tuple[float, float, float]
    up: tuple[float, float, float]
    front: tuple[float, float, float]
    planned_rotate_z_deg: int
    planned_scale_xyz: tuple[float, float, float]
    scale_anisotropy: float
    uniform_scale: float
    inspection: dict[str, int]


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _normalize(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise HssdBindingError("non-finite number is prohibited")
        rounded = round(value, 6)
        if rounded == 0.0:
            return 0
        return int(rounded) if rounded.is_integer() else rounded
    return value


def canonical_json_bytes(value: Any) -> bytes:
    return (json.dumps(_normalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def content_digest(value: Mapping[str, Any]) -> str:
    body = {key: value[key] for key in value if key != "content_digest"}
    return hashlib.sha256(canonical_json_bytes(body)).hexdigest()


def seal_document(value: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(value))
    result.pop("content_digest", None)
    result["content_digest"] = content_digest(result)
    return result


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HssdBindingError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(path: pathlib.Path) -> dict[str, Any]:
    try:
        result = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(HssdBindingError(f"non-finite JSON constant: {value}")),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HssdBindingError(f"invalid JSON at {path.name}: {error}") from error
    if not isinstance(result, dict):
        raise HssdBindingError(f"JSON root must be an object: {path.name}")
    return result


def _regular_absolute_file(path: pathlib.Path, label: str) -> pathlib.Path:
    if not path.is_absolute():
        raise HssdBindingError(f"{label} must be absolute")
    if path.is_symlink() or not path.is_file():
        raise HssdBindingError(f"{label} must be a regular non-symlink file")
    return path.resolve(strict=True)


def _dataset_root(path: pathlib.Path) -> pathlib.Path:
    if not path.is_absolute():
        raise HssdBindingError("HSSD root must be absolute")
    if path.is_symlink() or not path.is_dir():
        raise HssdBindingError("HSSD root must be a regular non-symlink directory")
    return path.resolve(strict=True)


def _contained_file(root: pathlib.Path, relative: str, label: str) -> pathlib.Path:
    candidate = root / relative
    if candidate.is_symlink() or not candidate.is_file():
        raise HssdBindingError(f"{label} must be a regular non-symlink file: {relative}")
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise HssdBindingError(f"{label} escapes HSSD root: {relative}") from error
    return resolved


def load_normalized_manifest(path: pathlib.Path) -> dict[str, Any]:
    path = _regular_absolute_file(path, "normalized manifest")
    manifest = _load_json(path)
    validate_normalized_manifest(manifest)
    return manifest


def validate_normalized_manifest(manifest: Mapping[str, Any]) -> None:
    if manifest.get("schema_version") != NORMALIZED_MANIFEST_SCHEMA:
        raise HssdBindingError("unsupported normalized manifest schema")
    digest = manifest.get("content_digest")
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest) or digest != content_digest(manifest):
        raise HssdBindingError("normalized manifest content_digest mismatch")
    if manifest.get("units") != "meters" or not isinstance(manifest.get("entities"), list):
        raise HssdBindingError("normalized manifest must contain metre-based entities")
    if not isinstance(manifest.get("room_bundles"), list):
        raise HssdBindingError("normalized manifest must contain room_bundles")


def _v3(value: Any, label: str) -> tuple[float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise HssdBindingError(f"{label} must be a three-element vector")
    result: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(float(item)):
            raise HssdBindingError(f"{label} must contain finite numbers")
        result.append(float(item))
    return tuple(result)  # type: ignore[return-value]


def _rotation_matrix_xyz(rotation_deg: Sequence[float]) -> tuple[tuple[float, float, float], ...]:
    rx, ry, rz = (math.radians(float(value)) for value in rotation_deg)
    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)
    # Rz @ Ry @ Rx, matching Blender XYZ Euler application order.
    return (
        (cz * cy, cz * sy * sx - sz * cx, cz * sy * cx + sz * sx),
        (sz * cy, sz * sy * sx + cz * cx, sz * sy * cx - cz * sx),
        (-sy, cy * sx, cy * cx),
    )


def _primitive_bounds(primitive: Mapping[str, Any]) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    dimensions = _v3(primitive.get("dimensions_m"), "primitive.dimensions_m")
    location = _v3(primitive.get("location_m"), "primitive.location_m")
    rotation = _v3(primitive.get("rotation_deg", [0, 0, 0]), "primitive.rotation_deg")
    if any(value <= 0 for value in dimensions):
        raise HssdBindingError("primitive dimensions must be positive")
    half = tuple(value / 2 for value in dimensions)
    matrix = _rotation_matrix_xyz(rotation)
    extent = tuple(sum(abs(matrix[row][column]) * half[column] for column in range(3)) for row in range(3))
    minimum = tuple(location[axis] - extent[axis] for axis in range(3))
    maximum = tuple(location[axis] + extent[axis] for axis in range(3))
    return minimum, maximum


def _target_from_entities(asset_id: str, entities: Sequence[Mapping[str, Any]]) -> TargetAsset:
    categories = {item.get("category") for item in entities}
    roles = {item.get("component_role") for item in entities}
    if len(categories) != 1 or len(roles) != 1:
        raise HssdBindingError(f"shared asset has divergent semantics: {asset_id}")
    category = next(iter(categories))
    role = next(iter(roles))
    if not isinstance(category, str) or not isinstance(role, str):
        raise HssdBindingError(f"asset semantics must be strings: {asset_id}")
    signatures: set[bytes] = set()
    bounds: tuple[tuple[float, float, float], tuple[float, float, float]] | None = None
    for entity in entities:
        geometry = entity.get("geometry")
        if not isinstance(geometry, dict) or geometry.get("assembly_policy") != "single_semantic_mesh":
            raise HssdBindingError(f"HSSD target must be a semantic mesh: {asset_id}")
        primitives = geometry.get("primitives")
        if not isinstance(primitives, list) or not primitives:
            raise HssdBindingError(f"HSSD target has no target geometry: {asset_id}")
        signature = canonical_json_bytes(primitives)
        signatures.add(signature)
        current_min = [math.inf, math.inf, math.inf]
        current_max = [-math.inf, -math.inf, -math.inf]
        for primitive in primitives:
            if not isinstance(primitive, dict):
                raise HssdBindingError(f"invalid primitive for {asset_id}")
            minimum, maximum = _primitive_bounds(primitive)
            for axis in range(3):
                current_min[axis] = min(current_min[axis], minimum[axis])
                current_max[axis] = max(current_max[axis], maximum[axis])
        scale = _v3(entity.get("transform", {}).get("scale", [1, 1, 1]), "entity.transform.scale")
        if any(value <= 0 for value in scale):
            raise HssdBindingError(f"entity scale must be positive: {asset_id}")
        dimensions = tuple((current_max[axis] - current_min[axis]) * scale[axis] for axis in range(3))
        validate_target_dimensions(dimensions, asset_id)
        canonical_bounds = (
            (-dimensions[0] / 2, -dimensions[1] / 2, 0.0),
            (dimensions[0] / 2, dimensions[1] / 2, dimensions[2]),
        )
        if bounds is None:
            bounds = canonical_bounds
        elif any(abs(bounds[1][axis] - canonical_bounds[1][axis]) > 1e-6 for axis in range(3)):
            raise HssdBindingError(f"shared asset has divergent target bounds: {asset_id}")
    if len(signatures) != 1 or bounds is None:
        raise HssdBindingError(f"shared asset has divergent geometry: {asset_id}")
    dimensions = (bounds[1][0] - bounds[0][0], bounds[1][1] - bounds[0][1], bounds[1][2] - bounds[0][2])
    return TargetAsset(
        asset_id=asset_id,
        category=category,
        component_role=role,
        target_dimensions_m=tuple(round(value, 6) for value in dimensions),  # type: ignore[arg-type]
        target_bounds_m=tuple(tuple(round(value, 6) for value in vector) for vector in bounds),  # type: ignore[arg-type]
        source_entity_ids=tuple(sorted(str(item.get("entity_id")) for item in entities)),
    )


def validate_target_dimensions(dimensions: Sequence[float], asset_id: str = "asset") -> None:
    values = _v3(dimensions, f"{asset_id}.target_dimensions_m")
    if any(value < 0.005 or value > 5.0 for value in values):
        raise HssdBindingError(f"target dimensions outside [0.005m, 5m]: {asset_id}")


def derive_target_assets(manifest: Mapping[str, Any]) -> tuple[dict[str, TargetAsset], list[dict[str, str]]]:
    validate_normalized_manifest(manifest)
    entities_by_asset: dict[str, list[Mapping[str, Any]]] = {}
    preserved: dict[str, dict[str, str]] = {}
    for entity in manifest["entities"]:
        if not isinstance(entity, dict):
            raise HssdBindingError("normalized entity must be an object")
        asset_id = entity.get("asset_ref")
        category = entity.get("category")
        if not isinstance(asset_id, str) or not _ASSET_ID_RE.fullmatch(asset_id):
            raise HssdBindingError("normalized entity has unsafe asset_ref")
        if not isinstance(category, str):
            raise HssdBindingError(f"normalized entity has invalid category: {asset_id}")
        if category in PRESERVE_CATEGORY_REASONS:
            preserved[asset_id] = {
                "asset_id": asset_id,
                "category": category,
                "reason": PRESERVE_CATEGORY_REASONS[category],
                "provider": "vista_playable_home_procedural_forge",
            }
        elif category in CATEGORY_RULES:
            entities_by_asset.setdefault(asset_id, []).append(entity)
        else:
            raise HssdBindingError(f"category is neither HSSD-bound nor explicitly preserved: {category}")
    for bundle in manifest["room_bundles"]:
        if not isinstance(bundle, dict) or not isinstance(bundle.get("asset_ref"), str):
            raise HssdBindingError("invalid room bundle binding")
        asset_id = bundle["asset_ref"]
        if not _ASSET_ID_RE.fullmatch(asset_id):
            raise HssdBindingError("room bundle has unsafe asset_ref")
        preserved[asset_id] = {
            "asset_id": asset_id,
            "category": "room_bundle",
            "reason": "room_shell_ceiling_and_collision_proxy_remain_procedural",
            "provider": "vista_playable_home_procedural_forge",
        }
    targets = {asset_id: _target_from_entities(asset_id, entries) for asset_id, entries in sorted(entities_by_asset.items())}
    return targets, [preserved[key] for key in sorted(preserved)]


def _read_git_revision(root: pathlib.Path) -> str:
    git_dir = root / ".git"
    if git_dir.is_symlink() or not git_dir.is_dir():
        raise HssdBindingError("HSSD dataset must retain its local .git identity")
    head_path = _contained_file(root, ".git/HEAD", "HSSD git HEAD")
    head = head_path.read_text(encoding="utf-8").strip()
    if _GIT_SHA_RE.fullmatch(head):
        return head
    if not head.startswith("ref: refs/") or ".." in head or not re.fullmatch(r"ref: refs/[A-Za-z0-9._/-]+", head):
        raise HssdBindingError("invalid HSSD git HEAD")
    ref = head[5:]
    ref_path = git_dir / ref
    if ref_path.is_file() and not ref_path.is_symlink():
        revision = ref_path.read_text(encoding="utf-8").strip()
        if _GIT_SHA_RE.fullmatch(revision):
            return revision
    packed = _contained_file(root, ".git/packed-refs", "HSSD packed refs")
    for line in packed.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or line.startswith("^"):
            continue
        fields = line.split(" ", 1)
        if len(fields) == 2 and fields[1] == ref and _GIT_SHA_RE.fullmatch(fields[0]):
            return fields[0]
    raise HssdBindingError("unable to resolve pinned HSSD git revision")


def dataset_identity(root: pathlib.Path, expected_readme_sha256: str = PINNED_HSSD_README_SHA256) -> dict[str, Any]:
    root = _dataset_root(root)
    readme = _contained_file(root, "README.md", "HSSD README")
    readme_hash = sha256_file(readme)
    if not _SHA256_RE.fullmatch(expected_readme_sha256) or readme_hash != expected_readme_sha256:
        raise HssdBindingError("HSSD README/license receipt hash mismatch")
    text = readme.read_text(encoding="utf-8").casefold()
    if "license: cc-by-nc-4.0" not in text or "creative commons attribution-noncommercial 4.0" not in text:
        # The local README uses the SPDX frontmatter and the full license name
        # in its gate prompt/link context.  Keep the second check tolerant of
        # the common short form used by local test fixtures.
        if "license: cc-by-nc-4.0" not in text or "cc by-nc 4.0" not in text:
            raise HssdBindingError("HSSD README does not declare CC BY-NC 4.0")
    return {
        "dataset": HSSD_DATASET_NAME,
        "dataset_revision": _read_git_revision(root),
        "readme_relpath": "README.md",
        "readme_sha256": readme_hash,
        "project_url": HSSD_PROJECT_URL,
        "license": {
            "spdx": HSSD_LICENSE_SPDX,
            "url": HSSD_LICENSE_URL,
            "commercial_use": "prohibited_without_separate_permission",
            "attribution_required": True,
            "modification_notice_required": True,
        },
    }


def _parse_dimensions(value: str, label: str) -> tuple[float, float, float]:
    try:
        parts = tuple(float(item.strip()) for item in value.split(","))
    except ValueError as error:
        raise HssdBindingError(f"invalid dimensions for {label}") from error
    if len(parts) != 3 or any(not math.isfinite(item) or item <= 0 for item in parts):
        raise HssdBindingError(f"invalid dimensions for {label}")
    return parts  # type: ignore[return-value]


def _read_csv_rows(path: pathlib.Path) -> tuple[list[str], list[dict[str, str]]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                raise HssdBindingError(f"CSV has no header: {path.name}")
            return list(reader.fieldnames), [dict(row) for row in reader]
    except (OSError, UnicodeDecodeError, csv.Error) as error:
        raise HssdBindingError(f"invalid CSV {path.name}: {error}") from error


def _load_metadata(root: pathlib.Path) -> tuple[dict[str, tuple[str, int]], dict[str, dict[str, str]]]:
    semantics_path = _contained_file(root, "metadata/hssd_obj_semantics_condensed.csv", "HSSD semantics")
    model_path = _contained_file(root, "metadata/fpmodels-with-decomposed.csv", "HSSD dimensions")
    semantic_fields, semantic_rows = _read_csv_rows(semantics_path)
    id_field = next((field for field in semantic_fields if field.strip().casefold() in {"object hash", "id"}), None)
    category_field = next((field for field in semantic_fields if "condensed" in field.casefold()), None)
    if id_field is None or category_field is None:
        raise HssdBindingError("HSSD semantic metadata lacks object id/condensed category")
    semantics: dict[str, tuple[str, int]] = {}
    for row in semantic_rows:
        object_id = (row.get(id_field) or "").strip().casefold()
        category = (row.get(category_field) or "").strip().casefold()
        if not object_id or not category:
            continue
        if object_id in semantics:
            raise HssdBindingError(f"duplicate HSSD semantic id: {object_id}")
        semantics[object_id] = (category, len(semantics))
    model_fields, model_rows = _read_csv_rows(model_path)
    required = {"id", "name", "aligned.dims"}
    if not required.issubset(set(model_fields)):
        raise HssdBindingError("HSSD model metadata lacks id/name/aligned.dims")
    models: dict[str, dict[str, str]] = {}
    for row in model_rows:
        object_id = (row.get("id") or "").strip().casefold()
        if not object_id:
            continue
        if object_id in models:
            raise HssdBindingError(f"duplicate HSSD model id: {object_id}")
        models[object_id] = row
    return semantics, models


def inspect_glb(path: pathlib.Path) -> dict[str, int]:
    """Inspect GLB structure/PBR slots without decoding textures or geometry."""

    path = _regular_absolute_file(path, "GLB")
    try:
        with path.open("rb") as handle:
            header = handle.read(12)
            if len(header) != 12:
                raise HssdBindingError(f"truncated GLB: {path.name}")
            magic, version, declared_length = struct.unpack("<4sII", header)
            if magic != b"glTF" or version != 2 or declared_length != path.stat().st_size:
                raise HssdBindingError(f"invalid GLB header: {path.name}")
            chunk_header = handle.read(8)
            if len(chunk_header) != 8:
                raise HssdBindingError(f"missing GLB JSON chunk: {path.name}")
            chunk_length, chunk_type = struct.unpack("<II", chunk_header)
            if chunk_type != _GLB_JSON_CHUNK or chunk_length > _MAX_GLB_JSON_BYTES:
                raise HssdBindingError(f"invalid GLB JSON chunk: {path.name}")
            document = json.loads(handle.read(chunk_length).rstrip(b"\x00 \t\r\n").decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, struct.error) as error:
        raise HssdBindingError(f"invalid GLB {path.name}: {error}") from error
    if not isinstance(document, dict) or document.get("asset", {}).get("version") != "2.0":
        raise HssdBindingError(f"unsupported glTF document: {path.name}")
    meshes = document.get("meshes", [])
    accessors = document.get("accessors", [])
    materials = document.get("materials", [])
    textures = document.get("textures", [])
    images = document.get("images", [])
    if not all(isinstance(value, list) for value in (meshes, accessors, materials, textures, images)):
        raise HssdBindingError(f"invalid glTF arrays: {path.name}")
    primitive_count = 0
    triangle_count = 0
    material_bound_primitive_count = 0
    for mesh in meshes:
        if not isinstance(mesh, dict) or not isinstance(mesh.get("primitives"), list):
            raise HssdBindingError(f"invalid glTF mesh: {path.name}")
        for primitive in mesh["primitives"]:
            if not isinstance(primitive, dict):
                raise HssdBindingError(f"invalid glTF primitive: {path.name}")
            primitive_count += 1
            material_index = primitive.get("material")
            if isinstance(material_index, int) and 0 <= material_index < len(materials):
                material_bound_primitive_count += 1
            if primitive.get("mode", 4) != 4:
                continue
            accessor_index = primitive.get("indices")
            if not isinstance(accessor_index, int):
                accessor_index = primitive.get("attributes", {}).get("POSITION")
            if isinstance(accessor_index, int) and 0 <= accessor_index < len(accessors):
                accessor = accessors[accessor_index]
                if isinstance(accessor, dict) and isinstance(accessor.get("count"), int):
                    triangle_count += accessor["count"] // 3
    pbr_material_count = 0
    pbr_texture_slot_count = 0
    base_normal_orm_texture_slot_count = 0
    for material in materials:
        if not isinstance(material, dict):
            continue
        pbr = material.get("pbrMetallicRoughness")
        if isinstance(pbr, dict):
            pbr_material_count += 1
            for field in ("baseColorTexture", "metallicRoughnessTexture"):
                present = int(isinstance(pbr.get(field), dict))
                pbr_texture_slot_count += present
                base_normal_orm_texture_slot_count += present
        for field in ("normalTexture", "occlusionTexture", "emissiveTexture"):
            present = int(isinstance(material.get(field), dict))
            pbr_texture_slot_count += present
            if field in {"normalTexture", "occlusionTexture"}:
                base_normal_orm_texture_slot_count += present
    return {
        "mesh_count": len(meshes),
        "primitive_count": primitive_count,
        "material_bound_primitive_count": material_bound_primitive_count,
        "all_primitives_material_bound": int(primitive_count > 0 and material_bound_primitive_count == primitive_count),
        "triangle_count": triangle_count,
        "material_count": len(materials),
        "pbr_material_count": pbr_material_count,
        "texture_count": len(textures),
        "image_count": len(images),
        "pbr_texture_slot_count": pbr_texture_slot_count,
        "base_normal_orm_texture_slot_count": base_normal_orm_texture_slot_count,
        "basisu_required": int("KHR_texture_basisu" in document.get("extensionsRequired", [])),
    }


def _fit_transform(source_dimensions: Sequence[float], target_dimensions: Sequence[float]) -> tuple[int, tuple[float, float, float], float, float]:
    source = _v3(source_dimensions, "source dimensions")
    target = _v3(target_dimensions, "target dimensions")
    choices: list[tuple[float, float, int, tuple[float, float, float]]] = []
    for rotation, oriented in ((0, source), (90, (source[1], source[0], source[2]))):
        scales = tuple(target[axis] / oriented[axis] for axis in range(3))
        anisotropy = max(scales) / min(scales)
        uniform = math.exp(sum(math.log(value) for value in scales) / 3)
        choices.append((anisotropy, abs(math.log(uniform)), rotation, scales))
    anisotropy, _size_error, rotation, scales = min(choices, key=lambda value: (round(value[0], 10), round(value[1], 10), value[2]))
    uniform = math.exp(sum(math.log(value) for value in scales) / 3)
    return rotation, scales, anisotropy, uniform


def _candidate_files(root: pathlib.Path, object_id: str) -> tuple[pathlib.Path, pathlib.Path, str, str]:
    if not _OBJECT_ID_RE.fullmatch(object_id):
        raise HssdBindingError(f"unsafe HSSD object id: {object_id}")
    base = f"objects/{object_id[0]}/{object_id}"
    config_relpath = f"{base}.object_config.json"
    config_path = _contained_file(root, config_relpath, "HSSD object config")
    config = _load_json(config_path)
    render_asset = config.get("render_asset")
    expected_name = f"{object_id}.glb"
    if render_asset != expected_name:
        raise HssdBindingError(f"HSSD render_asset is not the closed expected basename: {object_id}")
    source_relpath = f"objects/{object_id[0]}/{expected_name}"
    source_path = _contained_file(root, source_relpath, "HSSD render asset")
    return source_path, config_path, source_relpath, config_relpath


def _select_candidate(
    root: pathlib.Path,
    target: TargetAsset,
    semantics: Mapping[str, tuple[str, int]],
    models: Mapping[str, Mapping[str, str]],
) -> Candidate:
    aliases = CATEGORY_RULES[target.category].aliases
    alias_priority = {alias: index for index, alias in enumerate(aliases)}
    preliminary: list[tuple[tuple[Any, ...], str, str, tuple[float, float, float], int, tuple[float, float, float], float, float]] = []
    invalid_reasons: list[str] = []
    for object_id, (semantic_category, _row_index) in semantics.items():
        if semantic_category not in alias_priority or not _OBJECT_ID_RE.fullmatch(object_id):
            continue
        model = models.get(object_id)
        if not model or not (model.get("aligned.dims") or "").strip():
            continue
        try:
            habitat_dimensions = _parse_dimensions(model["aligned.dims"], object_id)
        except HssdBindingError:
            continue
        # HSSD/glTF is Y-up. Blender's glTF importer converts it to Z-up.
        blender_dimensions = (habitat_dimensions[0], habitat_dimensions[2], habitat_dimensions[1])
        rotation, scales, anisotropy, uniform = _fit_transform(blender_dimensions, target.target_dimensions_m)
        if anisotropy > _MAX_AXIS_SCALE_ANISOTROPY or not (_MIN_UNIFORM_SCALE <= uniform <= _MAX_UNIFORM_SCALE):
            continue
        score = (
            alias_priority[semantic_category],
            round(anisotropy, 3),
            round(abs(math.log(uniform)), 2),
            object_id,
        )
        preliminary.append((score, object_id, semantic_category, habitat_dimensions, rotation, scales, anisotropy, uniform))
    preliminary.sort(key=lambda item: item[0])
    viable: list[tuple[tuple[Any, ...], Candidate]] = []
    for base_score, object_id, semantic_category, habitat_dimensions, rotation, scales, anisotropy, uniform in preliminary[:_MAX_INSPECTED_PER_TARGET]:
        try:
            source_path, config_path, source_relpath, config_relpath = _candidate_files(root, object_id)
            config = _load_json(config_path)
            up = _v3(config.get("up"), f"{object_id}.up")
            front = _v3(config.get("front"), f"{object_id}.front")
            if up != (0.0, 1.0, 0.0) or front != (0.0, 0.0, -1.0):
                invalid_reasons.append(f"{object_id}:unsupported_axes")
                continue
            inspection = inspect_glb(source_path)
            if (
                inspection["material_count"] < 1
                or inspection["pbr_material_count"] < 1
                or inspection["texture_count"] < 1
                or inspection["image_count"] < 1
                or inspection["pbr_texture_slot_count"] < 1
                or inspection["base_normal_orm_texture_slot_count"] < 1
                or inspection["all_primitives_material_bound"] != 1
                or inspection["triangle_count"] < _MIN_TRIANGLES
            ):
                invalid_reasons.append(f"{object_id}:not_high_detail_pbr")
                continue
            candidate = Candidate(
                object_id=object_id,
                semantic_category=semantic_category,
                alias_priority=base_score[0],
                name=(models[object_id].get("name") or "").strip(),
                source_relpath=source_relpath,
                config_relpath=config_relpath,
                source_dimensions_habitat_m=habitat_dimensions,
                source_dimensions_blender_m=(habitat_dimensions[0], habitat_dimensions[2], habitat_dimensions[1]),
                up=up,
                front=front,
                planned_rotate_z_deg=rotation,
                planned_scale_xyz=scales,
                scale_anisotropy=anisotropy,
                uniform_scale=uniform,
                inspection=inspection,
            )
            quality_score = (
                base_score[0],
                base_score[1],
                base_score[2],
                -min(inspection["pbr_texture_slot_count"], 8),
                -min(inspection["triangle_count"], 500_000),
                object_id,
            )
            viable.append((quality_score, candidate))
        except HssdBindingError as error:
            invalid_reasons.append(f"{object_id}:{error}")
    if not viable:
        suffix = f"; inspected failures={invalid_reasons[:3]}" if invalid_reasons else ""
        raise HssdBindingError(f"no licensed high-detail PBR HSSD candidate for category {target.category}{suffix}")
    viable.sort(key=lambda item: item[0])
    return viable[0][1]


def _binding_entry(root: pathlib.Path, target: TargetAsset, candidate: Candidate) -> dict[str, Any]:
    source_path = _contained_file(root, candidate.source_relpath, "selected HSSD render asset")
    return {
        "logical_asset_id": target.asset_id,
        "semantic_category": target.category,
        "component_role": target.component_role,
        "source_entity_ids": list(target.source_entity_ids),
        "target_bounds_m": {"min_m": list(target.target_bounds_m[0]), "max_m": list(target.target_bounds_m[1])},
        "target_dimensions_m": list(target.target_dimensions_m),
        "normalization_plan": {
            "source_coordinate_system": "glTF_y_up_front_negative_z",
            "destination_coordinate_system": "blender_z_up_front_negative_y",
            "planned_rotate_z_deg": candidate.planned_rotate_z_deg,
            "planned_scale_xyz": list(candidate.planned_scale_xyz),
            "scale_anisotropy": candidate.scale_anisotropy,
            "uniform_scale": candidate.uniform_scale,
            "origin_policy": "footprint_center_bottom_z_zero",
            "dimension_policy": "exact_target_aabb_after_import",
        },
        "source": {
            "dataset": HSSD_DATASET_NAME,
            "object_id": candidate.object_id,
            "name": candidate.name,
            "semantic_category": candidate.semantic_category,
            "render_asset_relpath": candidate.source_relpath,
            "object_config_relpath": candidate.config_relpath,
            "render_asset_sha256": sha256_file(source_path),
            "source_dimensions_habitat_m": list(candidate.source_dimensions_habitat_m),
            "source_dimensions_blender_m": list(candidate.source_dimensions_blender_m),
            "up": list(candidate.up),
            "front": list(candidate.front),
            "license_spdx": HSSD_LICENSE_SPDX,
            "license_url": HSSD_LICENSE_URL,
        },
        "source_inspection": dict(candidate.inspection),
    }


def build_binding_plan(
    normalized_manifest: Mapping[str, Any],
    hssd_root: pathlib.Path,
    *,
    requested_asset_ids: Iterable[str] | None = None,
    expected_readme_sha256: str = PINNED_HSSD_README_SHA256,
) -> dict[str, Any]:
    """Select and fully attribute HSSD sources for the requested logical assets."""

    validate_normalized_manifest(normalized_manifest)
    root = _dataset_root(hssd_root)
    targets, preserved = derive_target_assets(normalized_manifest)
    if requested_asset_ids is None:
        requested = tuple(sorted(targets))
        mode = "full"
    else:
        requested = tuple(sorted(set(requested_asset_ids)))
        if not requested or any(asset_id not in targets for asset_id in requested):
            missing = sorted(set(requested) - set(targets))
            raise HssdBindingError(f"requested logical assets are not HSSD targets: {missing}")
        mode = "subset_smoke"
    identity = dataset_identity(root, expected_readme_sha256)
    semantics, models = _load_metadata(root)
    bindings = [_binding_entry(root, targets[asset_id], _select_candidate(root, targets[asset_id], semantics, models)) for asset_id in requested]
    preserved_for_plan = preserved if mode == "full" else []
    accounted = sorted([entry["logical_asset_id"] for entry in bindings] + [entry["asset_id"] for entry in preserved_for_plan])
    target_universe = sorted(list(targets) + [entry["asset_id"] for entry in preserved]) if mode == "full" else list(requested)
    plan = {
        "schema_version": BINDING_PLAN_SCHEMA,
        "house_id": normalized_manifest.get("house_id"),
        "revision": normalized_manifest.get("revision"),
        "source_normalized_manifest": {
            "schema_version": normalized_manifest.get("schema_version"),
            "content_digest": normalized_manifest.get("content_digest"),
        },
        "dataset": identity,
        "license_receipt": {
            "accepted_spdx": HSSD_LICENSE_SPDX,
            "scope": "research_and_noncommercial_demo_only",
            "attribution_notice": "HSSD source models; deterministically reoriented, rescaled, joined, and re-exported for VISTA Playable Home.",
            "commercial_release_gate": "replace_assets_or_obtain_separate_permission",
        },
        "selection_policy": {
            "version": SELECTION_POLICY_VERSION,
            "ordered_category_aliases": {category: list(rule.aliases) for category, rule in sorted(CATEGORY_RULES.items())},
            "minimum_triangles": _MIN_TRIANGLES,
            "require_pbr_texture_slot": True,
            "maximum_axis_scale_anisotropy": _MAX_AXIS_SCALE_ANISOTROPY,
            "tie_breaker": "semantic_alias_then_dimension_bucket_then_pbr_slots_then_triangles_then_object_id",
        },
        "mode": mode,
        "closed_world": {
            "target_asset_ids": target_universe,
            "bound_asset_ids": sorted(entry["logical_asset_id"] for entry in bindings),
            "preserved_asset_ids": sorted(entry["asset_id"] for entry in preserved_for_plan),
            "unaccounted_asset_ids": sorted(set(target_universe) - set(accounted)),
        },
        "bindings": bindings,
        "preserved_assets": preserved_for_plan,
    }
    plan = seal_document(plan)
    validate_binding_plan(plan)
    return plan


def validate_binding_plan(plan: Mapping[str, Any]) -> None:
    if plan.get("schema_version") != BINDING_PLAN_SCHEMA or plan.get("content_digest") != content_digest(plan):
        raise HssdBindingError("HSSD binding plan identity/digest mismatch")
    dataset = plan.get("dataset")
    receipt = plan.get("license_receipt")
    if not isinstance(dataset, dict) or dataset.get("license", {}).get("spdx") != HSSD_LICENSE_SPDX:
        raise HssdBindingError("HSSD binding plan lacks dataset license")
    if not isinstance(receipt, dict) or receipt.get("accepted_spdx") != HSSD_LICENSE_SPDX:
        raise HssdBindingError("HSSD binding plan lacks license receipt")
    bindings = plan.get("bindings")
    preserved = plan.get("preserved_assets")
    closed = plan.get("closed_world")
    if not isinstance(bindings, list) or not isinstance(preserved, list) or not isinstance(closed, dict):
        raise HssdBindingError("HSSD binding plan arrays are invalid")
    bound_ids: list[str] = []
    for entry in bindings:
        if not isinstance(entry, dict):
            raise HssdBindingError("HSSD binding entry must be an object")
        asset_id = entry.get("logical_asset_id")
        if not isinstance(asset_id, str) or not _ASSET_ID_RE.fullmatch(asset_id):
            raise HssdBindingError("HSSD binding has unsafe logical asset id")
        validate_target_dimensions(entry.get("target_dimensions_m"), asset_id)
        source = entry.get("source")
        inspection = entry.get("source_inspection")
        if not isinstance(source, dict) or source.get("license_spdx") != HSSD_LICENSE_SPDX:
            raise HssdBindingError(f"HSSD binding lacks per-source attribution: {asset_id}")
        if not isinstance(source.get("render_asset_sha256"), str) or not _SHA256_RE.fullmatch(source["render_asset_sha256"]):
            raise HssdBindingError(f"HSSD binding lacks source hash: {asset_id}")
        if not isinstance(inspection, dict) or inspection.get("pbr_texture_slot_count", 0) < 1:
            raise HssdBindingError(f"HSSD source is not PBR-textured: {asset_id}")
        bound_ids.append(asset_id)
    if len(bound_ids) != len(set(bound_ids)):
        raise HssdBindingError("duplicate logical asset binding")
    preserved_ids = [entry.get("asset_id") for entry in preserved if isinstance(entry, dict)]
    target_ids = closed.get("target_asset_ids")
    if not isinstance(target_ids, list) or closed.get("unaccounted_asset_ids") != []:
        raise HssdBindingError("HSSD binding plan is not closed")
    if sorted(set(bound_ids + preserved_ids)) != sorted(target_ids):
        raise HssdBindingError("HSSD binding coverage does not equal target universe")
    if sorted(bound_ids) != closed.get("bound_asset_ids") or sorted(preserved_ids) != closed.get("preserved_asset_ids"):
        raise HssdBindingError("HSSD closed-world indexes disagree")


def validate_built_manifest(manifest: Mapping[str, Any]) -> None:
    if manifest.get("schema_version") != BUILT_MANIFEST_SCHEMA or manifest.get("content_digest") != content_digest(manifest):
        raise HssdBindingError("HSSD built attribution manifest identity/digest mismatch")
    if manifest.get("source_plan", {}).get("schema_version") != BINDING_PLAN_SCHEMA:
        raise HssdBindingError("HSSD built manifest lacks source plan identity")
    if manifest.get("license_receipt", {}).get("accepted_spdx") != HSSD_LICENSE_SPDX:
        raise HssdBindingError("HSSD built manifest lacks license receipt")
    builder_source = manifest.get("builder_source")
    if (
        not isinstance(builder_source, dict)
        or builder_source.get("worktree_clean") is not True
        or not isinstance(builder_source.get("repository_commit"), str)
        or not _GIT_SHA_RE.fullmatch(builder_source["repository_commit"])
        or not isinstance(builder_source.get("source_files"), list)
        or not builder_source["source_files"]
    ):
        raise HssdBindingError("HSSD built manifest lacks a clean builder source identity")
    for source_file in builder_source["source_files"]:
        if (
            not isinstance(source_file, dict)
            or not isinstance(source_file.get("path"), str)
            or not isinstance(source_file.get("sha256"), str)
            or not _SHA256_RE.fullmatch(source_file["sha256"])
        ):
            raise HssdBindingError("HSSD built manifest has an invalid builder source file receipt")
    outputs = manifest.get("outputs")
    closed = manifest.get("closed_world")
    normalization_policy = manifest.get("normalization_policy")
    if not isinstance(outputs, list) or not isinstance(closed, dict):
        raise HssdBindingError("HSSD built manifest structure is invalid")
    policy_maximum_anisotropy = (
        normalization_policy.get("maximum_axis_scale_anisotropy")
        if isinstance(normalization_policy, dict)
        else None
    )
    if (
        isinstance(policy_maximum_anisotropy, bool)
        or not isinstance(policy_maximum_anisotropy, (int, float))
        or not math.isfinite(float(policy_maximum_anisotropy))
        or not 1.0 <= float(policy_maximum_anisotropy) <= _MAX_AXIS_SCALE_ANISOTROPY
    ):
        raise HssdBindingError("HSSD built manifest has an invalid normalization policy")
    output_ids: list[str] = []
    for entry in outputs:
        if not isinstance(entry, dict):
            raise HssdBindingError("HSSD output receipt must be an object")
        asset_id = entry.get("logical_asset_id")
        inspection = entry.get("inspection")
        if not isinstance(asset_id, str) or not isinstance(inspection, dict):
            raise HssdBindingError("HSSD output receipt identity is invalid")
        if inspection.get("mesh_count") != 1:
            raise HssdBindingError(f"HSSD output violates one-primary-mesh policy: {asset_id}")
        if (
            inspection.get("material_count", 0) < 1
            or inspection.get("pbr_texture_slot_count", 0) < 1
            or inspection.get("base_normal_orm_texture_slot_count", 0) < 1
            or inspection.get("all_primitives_material_bound") != 1
        ):
            raise HssdBindingError(f"HSSD output lost PBR material/texture slots: {asset_id}")
        if inspection.get("basisu_required") != 0:
            raise HssdBindingError(f"HSSD output still requires UE-incompatible KHR_texture_basisu: {asset_id}")
        texture_transport = entry.get("texture_transport")
        if texture_transport not in {"blender_native_texture_import", "KHR_texture_basisu_to_core_png"}:
            raise HssdBindingError(f"HSSD output has an unknown texture transport: {asset_id}")
        if texture_transport == "KHR_texture_basisu_to_core_png":
            transport = entry.get("texture_transport_receipt")
            if (
                not isinstance(transport, dict)
                or transport.get("blender_decoded_textures") is not False
                or transport.get("source_basisu_required") is not True
                or transport.get("output_basisu_required") is not False
                or transport.get("core_texture_sources_valid") is not True
                or transport.get("embedded_png_images_valid") is not True
                or not isinstance(transport.get("image_payloads"), list)
                or not transport["image_payloads"]
            ):
                raise HssdBindingError(f"HSSD BasisU-to-core-PNG receipt is incomplete: {asset_id}")
            decoder = transport.get("decoder")
            if not isinstance(decoder, dict) or decoder.get("basis_universal_license") != "Apache-2.0":
                raise HssdBindingError(f"HSSD BasisU decoder provenance is incomplete: {asset_id}")
            for decoder_field in ("node", "transcoder_js", "transcoder_wasm", "decode_wrapper"):
                record = decoder.get(decoder_field)
                if not isinstance(record, dict) or not isinstance(record.get("sha256"), str) or not _SHA256_RE.fullmatch(record["sha256"]):
                    raise HssdBindingError(f"HSSD BasisU decoder pin is incomplete: {asset_id}.{decoder_field}")
            for image in transport["image_payloads"]:
                if (
                    not isinstance(image, dict)
                    or not isinstance(image.get("source_ktx2_sha256"), str)
                    or not _SHA256_RE.fullmatch(image["source_ktx2_sha256"])
                    or not isinstance(image.get("output_png_sha256"), str)
                    or not _SHA256_RE.fullmatch(image["output_png_sha256"])
                    or not isinstance(image.get("width"), int)
                    or image["width"] < 1
                    or not isinstance(image.get("height"), int)
                    or image["height"] < 1
                ):
                    raise HssdBindingError(f"HSSD texture transcode image receipt is incomplete: {asset_id}")
        normalization = entry.get("normalization")
        if not isinstance(normalization, dict):
            raise HssdBindingError(f"HSSD output lacks normalization receipt: {asset_id}")
        actual_anisotropy = normalization.get("actual_scale_anisotropy")
        maximum_anisotropy = normalization.get("maximum_axis_scale_anisotropy")
        if (
            isinstance(actual_anisotropy, bool)
            or not isinstance(actual_anisotropy, (int, float))
            or not math.isfinite(float(actual_anisotropy))
            or isinstance(maximum_anisotropy, bool)
            or not isinstance(maximum_anisotropy, (int, float))
            or not math.isfinite(float(maximum_anisotropy))
            or float(maximum_anisotropy) < 1.0
            or float(maximum_anisotropy) > _MAX_AXIS_SCALE_ANISOTROPY
            or abs(float(maximum_anisotropy) - float(policy_maximum_anisotropy)) > 1e-9
            or float(actual_anisotropy) > float(maximum_anisotropy) + 1e-9
            or normalization.get("anisotropy_accepted") is not True
            or normalization.get("rotation_mode") != "XYZ"
        ):
            raise HssdBindingError(f"HSSD output normalization anisotropy is not accepted: {asset_id}")
        validate_target_dimensions(entry.get("actual_dimensions_m"), asset_id)
        target = _v3(entry.get("target_dimensions_m"), f"{asset_id}.target_dimensions_m")
        actual = _v3(entry.get("actual_dimensions_m"), f"{asset_id}.actual_dimensions_m")
        if any(abs(target[axis] - actual[axis]) > 0.001 for axis in range(3)):
            raise HssdBindingError(f"HSSD output dimensions drifted from target: {asset_id}")
        if not isinstance(entry.get("sha256"), str) or not _SHA256_RE.fullmatch(entry["sha256"]):
            raise HssdBindingError(f"HSSD output lacks hash: {asset_id}")
        output_ids.append(asset_id)
    if sorted(output_ids) != closed.get("bound_asset_ids") or closed.get("unaccounted_asset_ids") != []:
        raise HssdBindingError("HSSD built output coverage is not closed")


def write_new_private_json(path: pathlib.Path, value: Mapping[str, Any]) -> pathlib.Path:
    if not path.is_absolute():
        raise HssdBindingError("output JSON path must be absolute")
    if path.exists() or path.is_symlink():
        raise HssdBindingError("refusing to overwrite output JSON")
    parent = path.parent.resolve(strict=True)
    temporary = parent / f".{path.name}.tmp-{os.getpid()}"
    try:
        with temporary.open("xb") as handle:
            handle.write(canonical_json_bytes(value))
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return path


def parse_plan_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plan deterministic CC BY-NC HSSD visual bindings")
    parser.add_argument("--normalized-manifest", required=True, type=pathlib.Path)
    parser.add_argument("--hssd-root", required=True, type=pathlib.Path)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    parser.add_argument("--asset-id", action="append", dest="asset_ids")
    parser.add_argument("--license-accept", required=True, choices=[HSSD_LICENSE_SPDX])
    return parser.parse_args(argv)


def plan_main(argv: Sequence[str] | None = None) -> int:
    args = parse_plan_args(argv)
    manifest = load_normalized_manifest(args.normalized_manifest)
    plan = build_binding_plan(manifest, args.hssd_root, requested_asset_ids=args.asset_ids)
    write_new_private_json(args.output, plan)
    print(canonical_json_bytes({"status": "planned", "output": str(args.output), "content_digest": plan["content_digest"], "binding_count": len(plan["bindings"])}).decode("utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(plan_main())
