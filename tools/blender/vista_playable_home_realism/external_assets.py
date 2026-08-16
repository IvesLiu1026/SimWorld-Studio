"""Closed Poly Haven receipt validation and Blender realization for forge v2.

The pure receipt path is intentionally usable without :mod:`bpy`.  Runtime
realization is duck-typed and only called by ``build.py`` inside the pinned
Blender process.  Absolute acquisition paths never enter persistent receipts.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import pathlib
import re
import shutil
import stat
import struct
import tempfile
from dataclasses import dataclass
from typing import Any, Iterator, Mapping, Sequence

from .config import ForgeInputError, normalized, sha256_file


ACQUISITION_RECEIPT_FILENAME = "acquisition-receipt.json"
ACQUISITION_RECEIPT_SCHEMA = "simworld.vista.playable-home-poly-haven-receipt/v1"
PROVIDER = "poly_haven"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_MD5 = re.compile(r"^[0-9a-f]{32}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
_REQUIRED_PBR = frozenset({"base_color", "normal", "roughness"})
_SUPPORTED_EXTERNAL_IDENTITY_SEMANTICS = frozenset(
    {"base_color", "normal", "roughness", "metalness", "opacity"}
)
EXTERNAL_MATERIAL_ALPHA_POLICY_SCHEMA = (
    "simworld.vista.playable-home-external-material-alpha/v1"
)
EXTERNAL_MODEL_MATERIAL_CONTRACT_SCHEMA = (
    "simworld.vista.playable-home-external-model-material/v3"
)
EXTERNAL_MATERIAL_IDENTITY_SCHEMA = (
    "simworld.vista.playable-home-external-material-identity/v1"
)
EXTERNAL_SOURCE_MATERIAL_REGISTRY_SCHEMA = (
    "simworld.vista.playable-home-external-source-material-registry/v1"
)
EXTERNAL_MATERIAL_ALPHA_SANITIZATION = (
    "blender-4.5.8-receipt-bound-principled-alpha-greater-than-v2"
)
EXTERNAL_MATERIAL_ALPHA_CUTOFF = 0.5
EXTERNAL_MATERIAL_SOURCE_PROPERTY = "vista_external_source_logical_asset_id"
EXTERNAL_MATERIAL_SOURCE_DIGEST_PROPERTY = "vista_external_source_tree_sha256"
EXTERNAL_MATERIAL_SEMANTICS_PROPERTY = "vista_receipt_texture_semantics_json"
EXTERNAL_MATERIAL_ALPHA_MODE_PROPERTY = "vista_gltf_alpha_mode"
EXTERNAL_MATERIAL_ALPHA_CUTOFF_PROPERTY = "vista_gltf_alpha_cutoff"
EXTERNAL_MATERIAL_ALPHA_POLICY_PROPERTY = "vista_alpha_sanitization_policy"
EXTERNAL_MATERIAL_IDENTITY_PROPERTY = "vista_external_material_identity_sha256"
EXTERNAL_MATERIAL_CONTRACT_PROPERTIES = frozenset(
    {
        EXTERNAL_MATERIAL_SOURCE_PROPERTY,
        EXTERNAL_MATERIAL_SOURCE_DIGEST_PROPERTY,
        EXTERNAL_MATERIAL_SEMANTICS_PROPERTY,
        EXTERNAL_MATERIAL_ALPHA_MODE_PROPERTY,
        EXTERNAL_MATERIAL_ALPHA_CUTOFF_PROPERTY,
        EXTERNAL_MATERIAL_ALPHA_POLICY_PROPERTY,
        EXTERNAL_MATERIAL_IDENTITY_PROPERTY,
    }
)
EXTERNAL_TEXTURE_MATERIAL_CONTRACT_SCHEMA = (
    "simworld.vista.playable-home-external-texture-material/v1"
)
EXTERNAL_TEXTURE_MATERIAL_IDENTITY_SCHEMA = (
    "simworld.vista.playable-home-external-texture-material-identity/v1"
)
EXTERNAL_TEXTURE_MATERIAL_SOURCE_PROPERTY = (
    "vista_external_texture_source_logical_asset_id"
)
EXTERNAL_TEXTURE_MATERIAL_SOURCE_DIGEST_PROPERTY = (
    "vista_external_texture_source_tree_sha256"
)
EXTERNAL_TEXTURE_MATERIAL_SEMANTICS_PROPERTY = (
    "vista_external_texture_semantics_json"
)
EXTERNAL_TEXTURE_MATERIAL_ALPHA_MODE_PROPERTY = (
    "vista_external_texture_alpha_mode"
)
EXTERNAL_TEXTURE_MATERIAL_IDENTITY_PROPERTY = (
    "vista_external_texture_material_identity_sha256"
)
EXTERNAL_TEXTURE_MATERIAL_RECEIPT_PROPERTY = "vista_external_texture_material_receipt"
EXTERNAL_TEXTURE_MATERIAL_CONTRACT_PROPERTIES = frozenset(
    {
        EXTERNAL_TEXTURE_MATERIAL_SOURCE_PROPERTY,
        EXTERNAL_TEXTURE_MATERIAL_SOURCE_DIGEST_PROPERTY,
        EXTERNAL_TEXTURE_MATERIAL_SEMANTICS_PROPERTY,
        EXTERNAL_TEXTURE_MATERIAL_ALPHA_MODE_PROPERTY,
        EXTERNAL_TEXTURE_MATERIAL_IDENTITY_PROPERTY,
        EXTERNAL_TEXTURE_MATERIAL_RECEIPT_PROPERTY,
    }
)
EXTERNAL_TEXTURE_MATERIAL_CONTRACT_KEYS = frozenset(
    {
        "schema_version",
        "material_id",
        "source_logical_asset_id",
        "source_tree_sha256",
        "material_identity_sha256",
        "active_texture_semantics",
        "alpha_mode",
        "alpha_cutoff",
        "pbr_source",
    }
)
EXTERNAL_STATICIZATION_SCHEMA = "simworld.vista.playable-home-external-staticization/v1"
EXTERNAL_STATICIZATION_LEDGER_SCHEMA = (
    "simworld.vista.playable-home-external-staticization-ledger/v1"
)
EXTERNAL_STATICIZATION_SELECTION_SCHEMA = (
    "simworld.vista.playable-home-external-staticization-selection/v1"
)
EXTERNAL_STATICIZATION_POLICY = (
    "blender-4.5.8-frame-1-depsgraph-viewport-render-equivalent-evaluated-mesh/v1"
)
EXTERNAL_STATICIZATION_FRAME = 1
EXTERNAL_STATICIZATION_DEPSGRAPH_MODE = "VIEWPORT"
EXTERNAL_STATICIZATION_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "source_logical_asset_id",
        "source_tree_sha256",
        "blender_version",
        "frame",
        "depsgraph_mode",
        "evaluation_policy",
        "selection_policy",
        "input_inventory",
        "input_inventory_sha256",
        "input_actions",
        "exclusions",
        "output_meshes",
        "output_bounds_m",
        "output_digest",
        "content_digest",
    }
)
EXTERNAL_STATICIZATION_LEDGER_KEYS = frozenset(
    {"schema_version", "blender_version", "sources", "content_digest"}
)
EXTERNAL_STATICIZATION_OUTPUT_MESH_KEYS = frozenset(
    {
        "source_object_name",
        "object_name",
        "topology",
        "bounds_m",
        "material_ids",
        "mesh_sha256",
        "stripped_state",
    }
)
EXTERNAL_STATICIZATION_TOPOLOGY_KEYS = frozenset(
    {"vertices", "edges", "loops", "polygons", "uv_layers"}
)
EXTERNAL_STATICIZATION_BOUNDS_KEYS = frozenset(
    {"minimum", "maximum", "dimensions"}
)
EXTERNAL_STATICIZATION_STRIPPED_STATE = {
    "parent": None,
    "modifier_count": 0,
    "constraint_count": 0,
    "object_animation": False,
    "mesh_animation": False,
    "shape_keys": False,
    "identity_transform": True,
}
EXTERNAL_STATICIZATION_INPUT_OBJECT_KEYS = frozenset(
    {
        "object_name",
        "object_type",
        "data_name",
        "parent_name",
        "parent_type",
        "hide_render",
        "hide_viewport",
        "matrix_world",
        "source_topology",
        "material_slots",
        "modifiers",
        "constraints",
        "action",
    }
)
EXTERNAL_STATICIZATION_EXCLUSION_KEYS = frozenset(
    {"object_name", "reason", "evaluated_polygon_count", "used_materials"}
)
AUTHORED_UV_METERS_PER_TILE = 1.0
AUTHORED_RECIPE_MATERIAL_IDS: Mapping[str, tuple[str, ...]] = {
    "contemporary_shoe_bench_v1": (
        "visual.material.white_oak_veneer",
        "visual.material.poly_wool_herringbone",
    ),
    "contemporary_sofa_v1": (
        "visual.material.white_oak_veneer",
        "visual.material.poly_wool_herringbone",
    ),
    "contemporary_dining_table_v1": ("visual.material.white_oak_veneer",),
}


@dataclass(frozen=True)
class AcquiredFile:
    relative_path: str
    size_bytes: int
    sha256: str
    semantic: tuple[str, ...]
    dimensions_px: tuple[int, int] | None


@dataclass(frozen=True)
class AcquiredAsset:
    asset_id: str
    logical_asset_id: str
    asset_type: str
    room_role: str
    resolution: str
    file_variant: str
    provider_files_hash: str
    source_relative_root: str
    primary_relative_path: str
    source_tree_sha256: str
    catalog_dimensions_m: tuple[float, float, float] | None
    files: tuple[AcquiredFile, ...]

    @property
    def pbr_semantics(self) -> frozenset[str]:
        return frozenset(item for file in self.files for item in file.semantic)


@dataclass(frozen=True)
class ExternalAssetSet:
    """Verified local bytes plus public, serializable provenance.

    ``root`` is runtime-only.  Callers must serialize ``receipt_reference`` and
    per-asset digest records instead of applying ``dataclasses.asdict`` here.
    """

    root: pathlib.Path
    receipt_digest: str
    receipt_file_sha256: str
    acquisition_manifest_sha256: str
    assets: tuple[AcquiredAsset, ...]

    def asset(self, logical_asset_id: str) -> AcquiredAsset:
        matches = [item for item in self.assets if item.logical_asset_id == logical_asset_id]
        if len(matches) != 1:
            raise ForgeInputError(f"external asset is absent or duplicated: {logical_asset_id}")
        return matches[0]

    def source_path(self, logical_asset_id: str) -> pathlib.Path:
        asset = self.asset(logical_asset_id)
        return _safe_existing_path(self.root, asset.primary_relative_path, file_required=True)

    def receipt_reference(self) -> dict[str, Any]:
        return {
            "provider": PROVIDER,
            "receipt_schema_version": ACQUISITION_RECEIPT_SCHEMA,
            "receipt_digest": self.receipt_digest,
            "receipt_file_sha256": self.receipt_file_sha256,
            "acquisition_manifest_sha256": self.acquisition_manifest_sha256,
        }


@dataclass(frozen=True)
class ExternalSourceSelectionPolicy:
    """Exact receipt-pinned render selection for one retained model source."""

    source_tree_sha256: str
    selected_object_names: tuple[str, ...]
    excluded_renderable_objects: tuple[tuple[str, str], ...] = ()
    selected_dimensions_m: tuple[float, float, float] | None = None


_POSTCARD_OBJECT_NAMES = tuple(f"postcard_{index:02d}" for index in range(1, 21))
_BOOK_OBJECT_NAMES = tuple(
    f"book_encyclopedia_set_01_book{index:02d}" for index in range(1, 21)
)
_STOVE_OBJECT_NAMES = tuple(
    sorted(
        (
            *(f"dial_{index}" for index in range(1, 7)),
            "door_top",
            "grid",
            "handle_bottom_attachment",
            "hinge_bottom_attachment_left",
            "hinge_bottom_attachment_right",
            "hinge_bottom_rotator_left",
            "hinge_bottom_rotator_right",
            "hinge_top_attachment_left",
            "hinge_top_attachment_right",
            "hinge_top_rotator_left",
            "hinge_top_rotator_right",
            "sheet",
            "stovetop",
        )
    )
)
EXTERNAL_SOURCE_SELECTION_POLICIES: Mapping[str, ExternalSourceSelectionPolicy] = {
    "visual.dressing.entry.postcards": ExternalSourceSelectionPolicy(
        "1a9a8add26df9b5dfc7afdd99d1ebe1a91823a0a451b3209a4db85094e5273f2",
        _POSTCARD_OBJECT_NAMES,
    ),
    "visual.dressing.entry.rubber_boots": ExternalSourceSelectionPolicy(
        "696eecc64d80fe7413b1cb524c6a74995b2d0ed424a93430dcd5f61052a20362",
        ("rubber_boots_dirt_r_LOD0", "rubber_boots_dirty_l_LOD0"),
        tuple(
            sorted(
                (
                    ("rubber_boots_dirty_l_LOD1", "alternate_dirty_lod1"),
                    ("rubber_boots_dirty_r_LOD1", "alternate_dirty_lod1"),
                    ("rubber_boots_l_LOD0", "alternate_clean_variant"),
                    ("rubber_boots_l_LOD1", "alternate_clean_lod1"),
                    ("rubber_boots_r_LOD0", "alternate_clean_variant"),
                    ("rubber_boots_r_LOD1", "alternate_clean_lod1"),
                )
            )
        ),
        # Poly Haven's catalog envelope spans every clean/dirty and LOD
        # alternate in the .blend.  The closed dirty-LOD0 pair is narrower.
        selected_dimensions_m=(
            0.2906568646430969,
            0.2662065029144287,
            0.37276914715766907,
        ),
    ),
    "visual.dressing.entry.wicker_basket": ExternalSourceSelectionPolicy(
        "7762ce8b5ff25ad68de6d86a437384eb6c922a3a5153a4f68f170d0b10fd0d00",
        ("wicker_basket_02_base", "wicker_basket_02_lid"),
    ),
    "visual.dressing.kitchen.cardboard_box": ExternalSourceSelectionPolicy(
        "ded40d48a23ad5e2a01604be031dbc6d4ff17bc7ceb3668840473931c34dcadd",
        ("cardboard_box_01",),
    ),
    "visual.dressing.kitchen.cutting_board": ExternalSourceSelectionPolicy(
        "2ade9177a149033cd46f0c8afab3612d719489caf95705376250d6d93bced414",
        ("wooden_cutting_board",),
    ),
    "visual.dressing.kitchen.dining_chair": ExternalSourceSelectionPolicy(
        "9ec44864c1d376f0f51347d880701aec48d50f0b7c4cb41d899dbc9f01c9a3d0",
        ("dining_chair_02",),
    ),
    "visual.dressing.kitchen.wooden_bowl": ExternalSourceSelectionPolicy(
        "cf2db4b371e9aa675bbca0e7fdf98df31bb55f00a5893ebc0264d337f3e45949",
        ("wooden_bowl_01",),
    ),
    "visual.dressing.kitchen.wooden_plate": ExternalSourceSelectionPolicy(
        "d963bc0402232c124c0c996dd46df1d79bd2cf88b73ba3755c8747c6de892279",
        ("carved_wooden_plate",),
    ),
    "visual.dressing.kitchen.wooden_spoon": ExternalSourceSelectionPolicy(
        "0b9bf4a5098f1164596af4244fef51b87be6f1b8ccc57dcc5a7a7ecba54e1cd2",
        ("wooden_spoon",),
    ),
    "visual.dressing.living.armchair": ExternalSourceSelectionPolicy(
        "79118e13383af850d801f14b9ace7a65f91d520ce54b3a5bc8b4cf5e2089a7b1",
        ("modern_arm_chair_01",),
    ),
    "visual.dressing.living.books": ExternalSourceSelectionPolicy(
        "d2f5d87cc2c90c4f23c25de10b0e6642835d82dcb0106c480c4ab146909cae27",
        _BOOK_OBJECT_NAMES,
    ),
    "visual.dressing.living.media_cabinet": ExternalSourceSelectionPolicy(
        "0e0e1f733d0beee82ca9ecd7428b4731765836b56fa9a54aeea0dda80c718123",
        (
            "modern_wooden_cabinet_body",
            "modern_wooden_cabinet_door_l",
            "modern_wooden_cabinet_door_r",
        ),
    ),
    "visual.dressing.living.potted_plant": ExternalSourceSelectionPolicy(
        "084884b0a341699d76764e4f0186cb0cabbff1cd87f0536cfe168ba38e683da2",
        (
            "potted_plant_04_dirt",
            "potted_plant_04_ground",
            "potted_plant_04_plant",
            "potted_plant_04_pot",
        ),
    ),
    "visual.dressing.living.side_table": ExternalSourceSelectionPolicy(
        "c4d1d727051e9b443a0a1496b7ef2e3b018d83d45b55b2bd2c737becccf1f1e4",
        ("side_table_01",),
    ),
    "visual.dressing.shared.ceramic_vase": ExternalSourceSelectionPolicy(
        "a00fa716f81ea8536163618a4d34a4753e4fc0b6872783660258a29f33486597",
        ("ceramic_vase_02",),
    ),
    "visual.hero.kitchen_stove": ExternalSourceSelectionPolicy(
        "c55acbd188af4674ce5c1c8605f2447c5fb830a05b1650b0d03296b419b38795",
        _STOVE_OBJECT_NAMES,
    ),
    "visual.hero.living_coffee_table": ExternalSourceSelectionPolicy(
        "cf5fac22ac00b8725f91ad4565ddaa32dc5f10b213a0938a92de9e2432c1ddfe",
        ("modern_coffee_table_01",),
    ),
}


def _canonical_acquisition_json(value: Any) -> bytes:
    """Match the downloader's canonical JSON policy (not forge JSON + newline)."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _closed(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise ForgeInputError(f"{label} fields differ from the closed external-asset contract")
    return dict(value)


def _load_json(path: pathlib.Path, label: str) -> tuple[bytes, dict[str, Any]]:
    raw = path.read_bytes()

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ForgeInputError(f"{label} contains duplicate JSON key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, OSError) as error:
        raise ForgeInputError(f"cannot read {label}: {error}") from error
    if type(value) is not dict:
        raise ForgeInputError(f"{label} must contain one JSON object")
    return raw, value


def _lexical_absolute(path: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(os.path.abspath(os.fspath(path)))


def _validate_root(root: pathlib.Path) -> pathlib.Path:
    root = pathlib.Path(root)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise ForgeInputError("external acquisition root must be an absolute non-symlink directory")
    resolved = root.resolve(strict=True)
    if _lexical_absolute(root) != resolved:
        raise ForgeInputError("external acquisition root may not traverse symbolic links")
    return resolved


def _safe_relative(value: Any, label: str) -> str:
    if type(value) is not str or not value or "\\" in value or "\x00" in value:
        raise ForgeInputError(f"{label} is not a safe relative path")
    path = pathlib.PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ForgeInputError(f"{label} is not a safe relative path")
    return path.as_posix()


def _safe_existing_path(
    root: pathlib.Path,
    relative_path: str,
    *,
    file_required: bool,
) -> pathlib.Path:
    relative = pathlib.PurePosixPath(_safe_relative(relative_path, "receipt path"))
    candidate = root
    for part in relative.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ForgeInputError(f"external asset path contains a symbolic link: {relative_path}")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise ForgeInputError(f"external asset path is unavailable: {relative_path}") from error
    if not resolved.is_relative_to(root):
        raise ForgeInputError(f"external asset path escapes acquisition root: {relative_path}")
    if file_required and not resolved.is_file():
        raise ForgeInputError(f"external asset path is not a regular file: {relative_path}")
    return resolved


def _texture_semantics(relative_path: str) -> tuple[str, ...]:
    pure = pathlib.PurePosixPath(relative_path)
    if pure.suffix.lower() not in {".jpg", ".jpeg", ".png", ".exr"}:
        return ()
    stem = pure.stem.lower()
    result: list[str] = []
    if any(token in stem for token in ("_diff", "diffuse", "albedo", "basecolor", "base_color")):
        result.append("base_color")
    if any(token in stem for token in ("_nor", "normal")):
        result.append("normal")
    if "rough" in stem:
        result.append("roughness")
    packed = bool(re.search(r"(?:^|_)(?:orm|arm)(?:_|$)", stem))
    if "metal" in stem or packed:
        result.append("metalness")
    if "_ao" in stem or "occlusion" in stem or packed:
        result.append("ao")
    if any(token in stem for token in ("opacity", "alpha")):
        result.append("opacity")
    return tuple(sorted(set(result)))


def _jpeg_dimensions(path: pathlib.Path) -> tuple[int, int]:
    sof = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    with path.open("rb") as handle:
        if handle.read(2) != b"\xff\xd8":
            raise ForgeInputError(f"invalid JPEG texture: {path.name}")
        while True:
            byte = handle.read(1)
            if not byte:
                break
            if byte != b"\xff":
                continue
            marker = handle.read(1)
            while marker == b"\xff":
                marker = handle.read(1)
            if not marker:
                break
            code = marker[0]
            if code in {0xD8, 0xD9} or 0xD0 <= code <= 0xD7:
                continue
            length_raw = handle.read(2)
            if len(length_raw) != 2:
                break
            length = struct.unpack(">H", length_raw)[0]
            if length < 2:
                break
            if code in sof:
                payload = handle.read(length - 2)
                if len(payload) < 5:
                    break
                height, width = struct.unpack(">HH", payload[1:5])
                return int(width), int(height)
            handle.seek(length - 2, os.SEEK_CUR)
    raise ForgeInputError(f"JPEG texture has no dimensions: {path.name}")


def _exr_dimensions(path: pathlib.Path) -> tuple[int, int]:
    with path.open("rb") as handle:
        if handle.read(4) != b"v/1\x01":
            raise ForgeInputError(f"invalid OpenEXR texture: {path.name}")
        handle.read(4)  # version/flags
        while True:
            name = bytearray()
            while True:
                value = handle.read(1)
                if not value:
                    raise ForgeInputError(f"truncated OpenEXR header: {path.name}")
                if value == b"\x00":
                    break
                name.extend(value)
            if not name:
                break
            kind = bytearray()
            while True:
                value = handle.read(1)
                if not value:
                    raise ForgeInputError(f"truncated OpenEXR header: {path.name}")
                if value == b"\x00":
                    break
                kind.extend(value)
            size_raw = handle.read(4)
            if len(size_raw) != 4:
                raise ForgeInputError(f"truncated OpenEXR attribute: {path.name}")
            size = struct.unpack("<I", size_raw)[0]
            payload = handle.read(size)
            if len(payload) != size:
                raise ForgeInputError(f"truncated OpenEXR attribute: {path.name}")
            if name == b"dataWindow" and kind == b"box2i" and size == 16:
                x_min, y_min, x_max, y_max = struct.unpack("<iiii", payload)
                if x_max < x_min or y_max < y_min:
                    break
                return x_max - x_min + 1, y_max - y_min + 1
    raise ForgeInputError(f"OpenEXR texture lacks a dataWindow: {path.name}")


def image_dimensions(path: pathlib.Path) -> tuple[int, int]:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return _jpeg_dimensions(path)
    if suffix == ".png":
        with path.open("rb") as handle:
            header = handle.read(24)
        if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
            raise ForgeInputError(f"invalid PNG texture: {path.name}")
        width, height = struct.unpack(">II", header[16:24])
        return int(width), int(height)
    if suffix == ".exr":
        return _exr_dimensions(path)
    raise ForgeInputError(f"unsupported acquired texture format: {path.name}")


def _catalog_dimensions_m(catalog: Mapping[str, Any], asset_type: str) -> tuple[float, float, float] | None:
    if not isinstance(catalog, Mapping):
        raise ForgeInputError("external acquisition catalog record must be an object")
    if asset_type != "model":
        return None
    raw = catalog.get("dimensions")
    if type(raw) is not list or len(raw) != 3:
        raise ForgeInputError("model receipt lacks three provider dimensions")
    result = tuple(float(value) / 1000.0 for value in raw)
    if not all(math.isfinite(value) and value > 0 for value in result):
        raise ForgeInputError("model receipt contains invalid provider dimensions")
    return result  # type: ignore[return-value]


def load_external_asset_set(root: pathlib.Path, *, verify_files: bool = True) -> ExternalAssetSet:
    """Validate the exact append-only acquisition root and every recorded byte."""

    resolved_root = _validate_root(root)
    receipt_path = _safe_existing_path(
        resolved_root, ACQUISITION_RECEIPT_FILENAME, file_required=True
    )
    raw, payload = _load_json(receipt_path, "Poly Haven acquisition receipt")
    receipt = _closed(
        payload,
        {
            "schema_version", "provider", "catalog_urls", "license",
            "manifest_sha256", "acquired_at_utc", "asset_count",
            "total_size_bytes", "assets", "receipt_digest",
        },
        "acquisition receipt",
    )
    if receipt["schema_version"] != ACQUISITION_RECEIPT_SCHEMA or receipt["provider"] != PROVIDER:
        raise ForgeInputError("external acquisition receipt provider/schema is unsupported")
    declared_digest = receipt["receipt_digest"]
    body = dict(receipt)
    body.pop("receipt_digest")
    actual_digest = hashlib.sha256(_canonical_acquisition_json(body)).hexdigest()
    if declared_digest != actual_digest:
        raise ForgeInputError("external acquisition receipt_digest mismatch")
    if not isinstance(receipt["manifest_sha256"], str) or not _SHA256.fullmatch(receipt["manifest_sha256"]):
        raise ForgeInputError("external acquisition manifest SHA-256 is invalid")
    license_record = receipt["license"]
    if (
        type(license_record) is not dict
        or license_record.get("license_id") != "CC0-1.0"
        or license_record.get("entitlement_status") != "verified"
        or license_record.get("commercial_use") != "allowed"
    ):
        raise ForgeInputError("external acquisition lacks verified CC0 provenance")
    rows = receipt["assets"]
    if (
        type(rows) is not list
        or not rows
        or type(receipt["asset_count"]) is not int
        or receipt["asset_count"] != len(rows)
        or type(receipt["total_size_bytes"]) is not int
        or receipt["total_size_bytes"] <= 0
    ):
        raise ForgeInputError("external acquisition asset count is invalid")
    assets: list[AcquiredAsset] = []
    seen_assets: set[str] = set()
    seen_logical: set[str] = set()
    total_size = 0
    for index, raw_asset in enumerate(rows):
        asset = _closed(
            raw_asset,
            {
                "asset_id", "logical_asset_id", "asset_type", "room_role",
                "resolution", "file_variant", "catalog", "provider_files_hash",
                "source_relative_root", "primary_relative_path", "files",
                "source_tree_sha256",
            },
            f"acquisition assets[{index}]",
        )
        asset_id = asset["asset_id"]
        logical_id = asset["logical_asset_id"]
        if (
            type(asset_id) is not str or not _SAFE_ID.fullmatch(asset_id)
            or type(logical_id) is not str or not logical_id.startswith("visual.")
            or not _SAFE_ID.fullmatch(logical_id)
            or logical_id in seen_logical or asset_id in seen_assets
        ):
            raise ForgeInputError("external acquisition asset identity is invalid or duplicated")
        seen_assets.add(asset_id)
        seen_logical.add(logical_id)
        if asset["asset_type"] not in {"model", "texture"} or asset["resolution"] not in {"2k", "4k"}:
            raise ForgeInputError(f"external acquisition type/resolution is invalid: {logical_id}")
        if asset["file_variant"] != ("blend" if asset["asset_type"] == "model" else "pbr_jpg"):
            raise ForgeInputError(f"external acquisition file variant is invalid: {logical_id}")
        if type(asset["room_role"]) is not str or not asset["room_role"] or len(asset["room_role"]) > 96:
            raise ForgeInputError(f"external acquisition room role is invalid: {logical_id}")
        if not isinstance(asset["provider_files_hash"], str) or not _SHA1.fullmatch(asset["provider_files_hash"]):
            raise ForgeInputError(f"external provider files hash is invalid: {logical_id}")
        source_root = _safe_relative(asset["source_relative_root"], "source_relative_root")
        if source_root != f"assets/{asset_id}":
            raise ForgeInputError(f"external source root does not match asset identity: {logical_id}")
        primary = _safe_relative(asset["primary_relative_path"], "primary_relative_path")
        if not pathlib.PurePosixPath(primary).is_relative_to(pathlib.PurePosixPath(source_root)):
            raise ForgeInputError(f"external primary path escapes its asset root: {logical_id}")
        files_raw = asset["files"]
        if type(files_raw) is not list or not files_raw:
            raise ForgeInputError(f"external acquisition has no files: {logical_id}")
        files: list[AcquiredFile] = []
        tree_rows: list[dict[str, Any]] = []
        listed: set[str] = set()
        required_resolution = 2048 if asset["resolution"] == "2k" else 4096
        for file_index, raw_file in enumerate(files_raw):
            file = _closed(
                raw_file,
                {"relative_path", "url", "size_bytes", "provider_md5", "sha256"},
                f"acquisition assets[{index}].files[{file_index}]",
            )
            relative_within_asset = _safe_relative(file["relative_path"], "file relative_path")
            if relative_within_asset in listed:
                raise ForgeInputError(f"external acquisition repeats a file: {logical_id}")
            listed.add(relative_within_asset)
            full_relative = f"{source_root}/{relative_within_asset}"
            path = _safe_existing_path(resolved_root, full_relative, file_required=True)
            size = file["size_bytes"]
            digest = file["sha256"]
            if (
                type(size) is not int or isinstance(size, bool) or size <= 0
                or type(digest) is not str or not _SHA256.fullmatch(digest)
                or type(file["provider_md5"]) is not str or not _MD5.fullmatch(file["provider_md5"])
                or type(file["url"]) is not str
                or not file["url"].startswith("https://dl.polyhaven.org/file/ph-assets/")
                or "?" in file["url"]
                or "#" in file["url"]
                or path.stat().st_size != size
            ):
                raise ForgeInputError(f"external file size/hash metadata is invalid: {full_relative}")
            if verify_files and sha256_file(path) != digest:
                raise ForgeInputError(f"external file SHA-256 mismatch: {full_relative}")
            semantic = _texture_semantics(relative_within_asset)
            dimensions = image_dimensions(path) if semantic else None
            if dimensions and any(value < 1 for value in dimensions):
                raise ForgeInputError(f"external texture dimensions are invalid: {full_relative}")
            files.append(AcquiredFile(relative_within_asset, size, digest, semantic, dimensions))
            tree_rows.append({"relative_path": relative_within_asset, "size_bytes": size, "sha256": digest})
            total_size += size
        if primary != f"{source_root}/{files[0].relative_path}":
            raise ForgeInputError(f"external primary file differs from receipt order: {logical_id}")
        tree_digest = hashlib.sha256(_canonical_acquisition_json(tree_rows)).hexdigest()
        if asset["source_tree_sha256"] != tree_digest:
            raise ForgeInputError(f"external source tree SHA-256 mismatch: {logical_id}")
        semantics = frozenset(item for file in files for item in file.semantic)
        if not _REQUIRED_PBR.issubset(semantics):
            raise ForgeInputError(f"external asset lacks base/normal/roughness PBR maps: {logical_id}")
        for semantic in _REQUIRED_PBR:
            dimensions = [file.dimensions_px for file in files if semantic in file.semantic]
            if not dimensions or max(min(item) for item in dimensions if item is not None) < required_resolution:
                raise ForgeInputError(f"external {semantic} map is below {asset['resolution']}: {logical_id}")
        if asset["asset_type"] == "model" and not primary.lower().endswith(".blend"):
            raise ForgeInputError(f"external model primary is not Blender data: {logical_id}")
        assets.append(
            AcquiredAsset(
                asset_id=asset_id,
                logical_asset_id=logical_id,
                asset_type=asset["asset_type"],
                room_role=str(asset["room_role"]),
                resolution=asset["resolution"],
                file_variant=str(asset["file_variant"]),
                provider_files_hash=asset["provider_files_hash"],
                source_relative_root=source_root,
                primary_relative_path=primary,
                source_tree_sha256=tree_digest,
                catalog_dimensions_m=_catalog_dimensions_m(asset["catalog"], asset["asset_type"]),
                files=tuple(files),
            )
        )
    if receipt["total_size_bytes"] != total_size:
        raise ForgeInputError("external acquisition total byte count differs from files")
    assets.sort(key=lambda item: item.logical_asset_id)
    return ExternalAssetSet(
        root=resolved_root,
        receipt_digest=actual_digest,
        receipt_file_sha256=hashlib.sha256(raw).hexdigest(),
        acquisition_manifest_sha256=receipt["manifest_sha256"],
        assets=tuple(assets),
    )


def asset_digest_record(asset: AcquiredAsset) -> dict[str, Any]:
    """Return the public per-asset bytes bound into a placement/bundle receipt."""

    return {
        "logical_asset_id": asset.logical_asset_id,
        "asset_id": asset.asset_id,
        "asset_type": asset.asset_type,
        "resolution": asset.resolution,
        "provider_files_hash": asset.provider_files_hash,
        "source_tree_sha256": asset.source_tree_sha256,
        "files": [
            {
                "relative_path": item.relative_path,
                "size_bytes": item.size_bytes,
                "sha256": item.sha256,
                "texture_semantics": list(item.semantic),
                "dimensions_px": list(item.dimensions_px) if item.dimensions_px else None,
            }
            for item in asset.files
        ],
    }


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _canonical_runtime_json(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _runtime_json_sha256(value: Any) -> str:
    # Persistent forge JSON is recursively normalized to six decimals.  Hash
    # that exact representation so receipt digests survive both standalone
    # serialization and embedding in the normalized manifest.
    return hashlib.sha256(_canonical_runtime_json(normalized(value))).hexdigest()


def _receipt_float(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise RuntimeError("external receipt value is not numeric") from error
    if not math.isfinite(result):
        raise RuntimeError("external receipt value is non-finite")
    # Receipt JSON is normalized to six decimal places before it is written.
    # Round once at this boundary so derived values (especially dimensions)
    # are calculated from the exact endpoints that persist on disk.
    rounded = round(result, 6)
    return 0.0 if rounded == 0.0 else rounded


def _external_source_selection_policy_for_identity(
    logical_asset_id: str,
    source_tree_sha256: str,
) -> dict[str, Any]:
    policy = EXTERNAL_SOURCE_SELECTION_POLICIES.get(logical_asset_id)
    if policy is None or policy.source_tree_sha256 != source_tree_sha256:
        raise RuntimeError(
            f"external source lacks an exact retained staticization policy: {logical_asset_id}"
        )
    selected = list(policy.selected_object_names)
    excluded = [
        {"object_name": name, "reason": reason}
        for name, reason in policy.excluded_renderable_objects
    ]
    if (
        not selected
        or selected != sorted(set(selected))
        or [item["object_name"] for item in excluded]
        != sorted({item["object_name"] for item in excluded})
        or set(selected) & {item["object_name"] for item in excluded}
        or any(not item["reason"] for item in excluded)
    ):
        raise RuntimeError("external staticization selection policy is not closed")
    body = {
        "schema_version": EXTERNAL_STATICIZATION_SELECTION_SCHEMA,
        "source_logical_asset_id": logical_asset_id,
        "source_tree_sha256": source_tree_sha256,
        "selected_object_names": selected,
        "excluded_renderable_objects": excluded,
    }
    return {**body, "content_digest": _runtime_json_sha256(body)}


def external_source_selection_policy(asset: AcquiredAsset) -> dict[str, Any]:
    """Return one exact-name, exact-source selection contract."""

    return _external_source_selection_policy_for_identity(
        asset.logical_asset_id,
        asset.source_tree_sha256,
    )


def external_source_selected_dimensions_m(
    asset: AcquiredAsset,
    *,
    require_exact_policy: bool = True,
) -> tuple[float, float, float]:
    """Return the pinned envelope for the exact retained object selection."""

    # Validate the logical ID and source digest even though the selected
    # dimensions are runtime planning data rather than a serialized policy
    # field.  This prevents a measurement from being reused for different
    # source bytes.
    policy = EXTERNAL_SOURCE_SELECTION_POLICIES.get(asset.logical_asset_id)
    exact_policy = policy is not None and policy.source_tree_sha256 == asset.source_tree_sha256
    if require_exact_policy:
        _external_source_selection_policy_for_identity(
            asset.logical_asset_id,
            asset.source_tree_sha256,
        )
    dimensions = (
        policy.selected_dimensions_m
        if exact_policy and policy is not None and policy.selected_dimensions_m is not None
        else asset.catalog_dimensions_m
    )
    if (
        dimensions is None
        or len(dimensions) != 3
        or any(not math.isfinite(float(value)) or float(value) <= 0 for value in dimensions)
    ):
        raise RuntimeError(
            f"external source lacks a pinned selected-object measurement: "
            f"{asset.logical_asset_id}"
        )
    return tuple(float(value) for value in dimensions)  # type: ignore[return-value]


def external_texture_material_identity_for_source(
    logical_asset_id: str,
    source_tree_sha256: str,
    active_texture_semantics: Sequence[str],
) -> str:
    semantics = list(active_texture_semantics)
    if (
        type(logical_asset_id) is not str
        or _SAFE_ID.fullmatch(logical_asset_id) is None
        or type(source_tree_sha256) is not str
        or _SHA256.fullmatch(source_tree_sha256) is None
        or semantics != sorted(_REQUIRED_PBR)
    ):
        raise RuntimeError("external texture material identity source is invalid")
    payload = {
        "schema_version": EXTERNAL_TEXTURE_MATERIAL_IDENTITY_SCHEMA,
        "source_logical_asset_id": logical_asset_id,
        "source_tree_sha256": source_tree_sha256,
        "active_texture_semantics": semantics,
        "alpha_mode": "OPAQUE",
        "alpha_cutoff": None,
    }
    return _runtime_json_sha256(payload)


def external_texture_material_identity_sha256(asset: AcquiredAsset) -> str:
    if asset.asset_type != "texture" or not _REQUIRED_PBR.issubset(asset.pbr_semantics):
        raise RuntimeError("external texture material identity source is not a complete texture")
    return external_texture_material_identity_for_source(
        asset.logical_asset_id,
        asset.source_tree_sha256,
        sorted(_REQUIRED_PBR),
    )


def external_texture_material_name_for_source(
    logical_asset_id: str,
    source_tree_sha256: str,
    active_texture_semantics: Sequence[str],
) -> str:
    identity = external_texture_material_identity_for_source(
        logical_asset_id,
        source_tree_sha256,
        active_texture_semantics,
    )
    slug = _slug(logical_asset_id)
    if not slug:
        raise RuntimeError("external texture material source has no safe slug")
    name = f"r2.external.texture.{slug[:24]}.{identity[:16]}"
    if len(name) > 63:
        raise RuntimeError("external texture material identity exceeds Blender's name limit")
    return name


def external_texture_material_name(asset: AcquiredAsset) -> str:
    return external_texture_material_name_for_source(
        asset.logical_asset_id,
        asset.source_tree_sha256,
        sorted(_REQUIRED_PBR),
    )


def external_material_alpha_policy() -> dict[str, Any]:
    """Return the closed v2 source-to-GLB alpha sanitization contract.

    Blender 4.5's glTF exporter derives ``alphaMode`` from the Principled
    Alpha node graph.  ``surface_render_method`` is deliberately documented as
    non-authoritative here: both imported OPAQUE and MASK materials use
    ``DITHERED`` in Blender 4.5.
    """

    return {
        "schema_version": EXTERNAL_MATERIAL_ALPHA_POLICY_SCHEMA,
        "blender_version": [4, 5, 8],
        "gltf_exporter_alpha_detection": "gather_alpha_info.detect_alpha_clip",
        "source_mapping": "material_extras_source_material_identity_v2",
        "material_contract_schema": EXTERNAL_MODEL_MATERIAL_CONTRACT_SCHEMA,
        "material_identity_schema": EXTERNAL_MATERIAL_IDENTITY_SCHEMA,
        "source_registry_schema": EXTERNAL_SOURCE_MATERIAL_REGISTRY_SCHEMA,
        "external_texture_material_contract_schema": (
            EXTERNAL_TEXTURE_MATERIAL_CONTRACT_SCHEMA
        ),
        "staticization_schema": EXTERNAL_STATICIZATION_SCHEMA,
        "staticization_ledger_schema": EXTERNAL_STATICIZATION_LEDGER_SCHEMA,
        "staticization_policy": EXTERNAL_STATICIZATION_POLICY,
        "sanitization": EXTERNAL_MATERIAL_ALPHA_SANITIZATION,
        "opacity_semantic": "opacity",
        "masked_alpha_mode": "MASK",
        "masked_alpha_cutoff": EXTERNAL_MATERIAL_ALPHA_CUTOFF,
        "non_opacity_alpha_mode": "OPAQUE",
        "external_model_blend_alpha_mode_forbidden": True,
        "export_extras_required": True,
        "surface_render_method_authoritative": False,
    }


def external_material_name_prefix(logical_asset_id: str) -> str:
    """Return the deterministic material namespace for one acquired model."""

    if type(logical_asset_id) is not str or _SAFE_ID.fullmatch(logical_asset_id) is None:
        raise RuntimeError("external material source logical asset ID is invalid")
    slug = _slug(logical_asset_id)
    if not slug:
        raise RuntimeError("external material source logical asset ID has no safe slug")
    source_digest = hashlib.sha256(logical_asset_id.encode("utf-8")).hexdigest()[:16]
    # The full source ID is hash-bound so IDs that slugify alike, or IDs longer
    # than Blender's 63-character datablock limit, never share a namespace.
    prefix = f"r2.external.{slug[:14].ljust(14, '_')}.{source_digest}."
    if len(prefix) != 44:
        raise RuntimeError("external material source namespace length is not closed")
    return prefix


def external_material_identity_sha256(
    logical_asset_id: str,
    source_tree_sha256: str,
    ordinal: int,
    source_material_name: str,
    active_texture_semantics: Sequence[str],
) -> str:
    """Hash the exact receipt-pinned source material identity.

    The source-tree digest pins the original Blender graph and its texture
    bytes.  The remaining fields prevent graph slots or active semantics from
    being silently reassigned while still producing the same exported name.
    """

    if type(logical_asset_id) is not str or _SAFE_ID.fullmatch(logical_asset_id) is None:
        raise RuntimeError("external material source logical asset ID is invalid")
    if type(source_tree_sha256) is not str or _SHA256.fullmatch(source_tree_sha256) is None:
        raise RuntimeError("external material source tree SHA-256 is invalid")
    if type(ordinal) is not int or not 0 <= ordinal <= 99:
        raise RuntimeError("external source has too many materials for a stable two-digit identity")
    if (
        type(source_material_name) is not str
        or not source_material_name
        or "\x00" in source_material_name
    ):
        raise RuntimeError("external source material name is invalid")
    semantics = list(active_texture_semantics)
    if (
        any(type(item) is not str or item not in _SUPPORTED_EXTERNAL_IDENTITY_SEMANTICS for item in semantics)
        or semantics != sorted(set(semantics))
        or not _REQUIRED_PBR.issubset(semantics)
    ):
        raise RuntimeError("external source material identity semantics are invalid")
    alpha_mode = "MASK" if "opacity" in semantics else "OPAQUE"
    payload = {
        "schema_version": EXTERNAL_MATERIAL_IDENTITY_SCHEMA,
        "source_logical_asset_id": logical_asset_id,
        "source_tree_sha256": source_tree_sha256,
        "material_ordinal": ordinal,
        "source_material_name": source_material_name,
        "active_texture_semantics": semantics,
        "alpha_mode": alpha_mode,
        "alpha_cutoff": EXTERNAL_MATERIAL_ALPHA_CUTOFF if alpha_mode == "MASK" else None,
        "sanitization_policy": EXTERNAL_MATERIAL_ALPHA_SANITIZATION,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def external_material_name(
    logical_asset_id: str,
    ordinal: int,
    material_identity_sha256: str,
) -> str:
    if type(ordinal) is not int or not 0 <= ordinal <= 99:
        raise RuntimeError("external source has too many materials for a stable two-digit identity")
    if (
        type(material_identity_sha256) is not str
        or _SHA256.fullmatch(material_identity_sha256) is None
    ):
        raise RuntimeError("external material identity SHA-256 is invalid")
    name = (
        f"{external_material_name_prefix(logical_asset_id)}"
        f"{ordinal:02d}.{material_identity_sha256[:16]}"
    )
    if len(name) != 63:
        raise RuntimeError("external material identity does not fit Blender's closed name limit")
    return name


EXTERNAL_MODEL_MATERIAL_CONTRACT_KEYS = frozenset(
    {
        "schema_version",
        "material_id",
        "source_logical_asset_id",
        "source_tree_sha256",
        "source_material_name",
        "material_ordinal",
        "material_identity_sha256",
        "active_texture_semantics",
        "inactive_image_normalizations",
        "removed_source_custom_properties",
        "alpha_mode",
        "alpha_cutoff",
        "sanitization_policy",
    }
)


def external_source_material_registry_sha256(
    logical_asset_id: str,
    source_tree_sha256: str,
    material_contracts: Sequence[Mapping[str, Any]],
) -> str:
    """Validate and digest one exact source-level material inventory."""

    if type(logical_asset_id) is not str or _SAFE_ID.fullmatch(logical_asset_id) is None:
        raise RuntimeError("external material registry source logical asset ID is invalid")
    if type(source_tree_sha256) is not str or _SHA256.fullmatch(source_tree_sha256) is None:
        raise RuntimeError("external material registry source tree SHA-256 is invalid")
    if any(not isinstance(item, Mapping) for item in material_contracts):
        raise RuntimeError("external material registry contains a non-object contract")
    contracts = [dict(item) for item in material_contracts]
    if not contracts:
        raise RuntimeError("external material registry inventory is empty")
    if any(type(item.get("material_ordinal")) is not int for item in contracts):
        raise RuntimeError("external material registry ordinal is invalid")
    contracts.sort(key=lambda item: item.get("material_ordinal", -1))
    seen_ids: set[str] = set()
    for expected_ordinal, item in enumerate(contracts):
        if set(item) != EXTERNAL_MODEL_MATERIAL_CONTRACT_KEYS:
            raise RuntimeError("external material registry contract fields are not closed")
        semantics = item.get("active_texture_semantics")
        normalizations = item.get("inactive_image_normalizations")
        property_normalizations = item.get("removed_source_custom_properties")
        source_name = item.get("source_material_name")
        if (
            item.get("schema_version") != EXTERNAL_MODEL_MATERIAL_CONTRACT_SCHEMA
            or item.get("source_logical_asset_id") != logical_asset_id
            or item.get("source_tree_sha256") != source_tree_sha256
            or item.get("material_ordinal") != expected_ordinal
            or type(source_name) is not str
            or not source_name
            or "\x00" in source_name
            or not isinstance(semantics, list)
            or semantics != sorted(set(semantics))
            or not isinstance(normalizations, list)
            or any(
                not isinstance(row, Mapping)
                or set(row)
                != {"node_name", "image_name", "relative_path", "sha256", "reason"}
                or type(row.get("node_name")) is not str
                or not row.get("node_name")
                or type(row.get("image_name")) is not str
                or not row.get("image_name")
                or type(row.get("relative_path")) is not str
                or not row.get("relative_path")
                or type(row.get("sha256")) is not str
                or _SHA256.fullmatch(row["sha256"]) is None
                or row.get("reason") != "inactive_disconnected_receipt_bound_image"
                for row in normalizations
            )
            or normalizations
            != sorted(
                normalizations,
                key=lambda row: (row["node_name"], row["relative_path"]),
            )
            or not isinstance(property_normalizations, list)
            or any(
                not isinstance(row, Mapping)
                or set(row)
                != {"property_name", "value_type", "value_sha256", "reason"}
                or type(row.get("property_name")) is not str
                or not row["property_name"]
                or row.get("value_type")
                not in {"boolean", "integer", "number", "string", "mapping", "array"}
                or type(row.get("value_sha256")) is not str
                or _SHA256.fullmatch(row["value_sha256"]) is None
                or row.get("reason")
                != "receipt_bound_source_only_custom_property_removed"
                for row in property_normalizations
            )
            or property_normalizations
            != sorted(property_normalizations, key=lambda row: row["property_name"])
        ):
            raise RuntimeError("external material registry source identity differs")
        identity = external_material_identity_sha256(
            logical_asset_id,
            source_tree_sha256,
            expected_ordinal,
            source_name,
            semantics,
        )
        material_id = external_material_name(logical_asset_id, expected_ordinal, identity)
        expected_mode = "MASK" if "opacity" in semantics else "OPAQUE"
        expected_cutoff = (
            EXTERNAL_MATERIAL_ALPHA_CUTOFF if expected_mode == "MASK" else None
        )
        if (
            item.get("material_identity_sha256") != identity
            or item.get("material_id") != material_id
            or material_id in seen_ids
            or item.get("alpha_mode") != expected_mode
            or item.get("alpha_cutoff") != expected_cutoff
            or item.get("sanitization_policy") != EXTERNAL_MATERIAL_ALPHA_SANITIZATION
        ):
            raise RuntimeError("external material registry contract identity differs")
        seen_ids.add(material_id)
    payload = {
        "schema_version": EXTERNAL_SOURCE_MATERIAL_REGISTRY_SCHEMA,
        "source_logical_asset_id": logical_asset_id,
        "source_tree_sha256": source_tree_sha256,
        "materials": contracts,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class _ExternalSourcePrototype:
    logical_asset_id: str
    source_tree_sha256: str
    material_registry_sha256: str
    meshes: tuple[Any, ...]
    normalized_dimensions_m: tuple[float, float, float]
    material_contracts: tuple[Mapping[str, Any], ...]


class _ExternalSourceMaterialRegistry:
    """Closed source prototypes reused by every placement in one forge run."""

    def __init__(self) -> None:
        self._by_source: dict[str, _ExternalSourcePrototype] = {}
        self._material_owner: dict[str, str] = {}

    def get(self, asset: AcquiredAsset) -> _ExternalSourcePrototype | None:
        prototype = self._by_source.get(asset.logical_asset_id)
        if prototype is None:
            return None
        if (
            prototype.source_tree_sha256 != asset.source_tree_sha256
            or prototype.material_registry_sha256
            != external_source_material_registry_sha256(
                asset.logical_asset_id,
                asset.source_tree_sha256,
                prototype.material_contracts,
            )
        ):
            raise RuntimeError("external material registry source digest or inventory changed")
        return prototype

    def add(
        self,
        asset: AcquiredAsset,
        meshes: Sequence[Any],
        normalized_dimensions_m: Sequence[float],
        material_contracts: Sequence[Mapping[str, Any]],
    ) -> _ExternalSourcePrototype:
        if asset.logical_asset_id in self._by_source:
            raise RuntimeError("external material registry source was registered twice")
        registry_digest = external_source_material_registry_sha256(
            asset.logical_asset_id,
            asset.source_tree_sha256,
            material_contracts,
        )
        for contract in material_contracts:
            material_id = str(contract["material_id"])
            owner = self._material_owner.get(material_id)
            if owner is not None and owner != asset.logical_asset_id:
                raise RuntimeError("external material registry identities collide across sources")
        dimensions = tuple(float(value) for value in normalized_dimensions_m)
        if len(dimensions) != 3 or any(not math.isfinite(value) or value <= 0 for value in dimensions):
            raise RuntimeError("external material registry normalized dimensions are invalid")
        prototype = _ExternalSourcePrototype(
            logical_asset_id=asset.logical_asset_id,
            source_tree_sha256=asset.source_tree_sha256,
            material_registry_sha256=registry_digest,
            meshes=tuple(meshes),
            normalized_dimensions_m=dimensions,
            material_contracts=tuple(dict(item) for item in material_contracts),
        )
        if not prototype.meshes:
            raise RuntimeError("external material registry prototype has no meshes")
        self._by_source[asset.logical_asset_id] = prototype
        for contract in material_contracts:
            self._material_owner[str(contract["material_id"])] = asset.logical_asset_id
        return prototype

    def values(self) -> tuple[_ExternalSourcePrototype, ...]:
        return tuple(self._by_source[source_id] for source_id in sorted(self._by_source))


def _receipt_file_fingerprint(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _verify_receipt_file_bytes(
    path: pathlib.Path,
    receipt_file: AcquiredFile,
    *,
    label: str,
) -> tuple[int, int, int, int, int, int]:
    """Recheck one already-resolved receipt file immediately before use."""

    try:
        before = path.stat(follow_symlinks=False)
    except OSError as error:
        raise RuntimeError(f"{label} is unavailable: {receipt_file.relative_path}") from error
    if not stat.S_ISREG(before.st_mode) or before.st_size != receipt_file.size_bytes:
        raise RuntimeError(f"{label} size differs from receipt: {receipt_file.relative_path}")
    digest = sha256_file(path)
    try:
        after = path.stat(follow_symlinks=False)
    except OSError as error:
        raise RuntimeError(f"{label} changed while hashing: {receipt_file.relative_path}") from error
    if _receipt_file_fingerprint(before) != _receipt_file_fingerprint(after):
        raise RuntimeError(f"{label} changed while hashing: {receipt_file.relative_path}")
    if digest != receipt_file.sha256:
        raise RuntimeError(f"{label} SHA-256 differs from receipt: {receipt_file.relative_path}")
    return _receipt_file_fingerprint(after)


def _sha256_descriptor(file_descriptor: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while True:
        chunk = os.pread(file_descriptor, 1024 * 1024, offset)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)
        offset += len(chunk)


def _verify_receipt_descriptor(
    file_descriptor: int,
    receipt_file: AcquiredFile,
    *,
    label: str,
) -> tuple[int, int, int, int, int, int]:
    try:
        before = os.fstat(file_descriptor)
    except OSError as error:
        raise RuntimeError(f"{label} descriptor is unavailable") from error
    if not stat.S_ISREG(before.st_mode) or before.st_size != receipt_file.size_bytes:
        raise RuntimeError(f"{label} descriptor size differs from receipt")
    digest = _sha256_descriptor(file_descriptor)
    try:
        after = os.fstat(file_descriptor)
    except OSError as error:
        raise RuntimeError(f"{label} descriptor changed while hashing") from error
    if _receipt_file_fingerprint(before) != _receipt_file_fingerprint(after):
        raise RuntimeError(f"{label} descriptor changed while hashing")
    if digest != receipt_file.sha256:
        raise RuntimeError(f"{label} descriptor SHA-256 differs from receipt")
    return _receipt_file_fingerprint(after)


def _write_all(file_descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(file_descriptor, payload[offset:])
        if written <= 0:
            raise RuntimeError("runtime snapshot write made no progress")
        offset += written


def _copy_verified_file_to_snapshot(
    source: pathlib.Path,
    destination: pathlib.Path,
    *,
    expected_size: int | None,
    expected_sha256: str,
    label: str,
) -> None:
    source_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    destination_flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        source_fd = os.open(source, source_flags)
    except OSError as error:
        raise RuntimeError(f"cannot open {label} for runtime snapshot") from error
    destination_fd: int | None = None
    try:
        source_before = os.fstat(source_fd)
        if not stat.S_ISREG(source_before.st_mode) or (
            expected_size is not None and source_before.st_size != expected_size
        ):
            raise RuntimeError(f"{label} size differs from receipt during snapshot")
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination_fd = os.open(destination, destination_flags, 0o600)
        digest = hashlib.sha256()
        offset = 0
        while True:
            chunk = os.pread(source_fd, 1024 * 1024, offset)
            if not chunk:
                break
            digest.update(chunk)
            _write_all(destination_fd, chunk)
            offset += len(chunk)
        os.fsync(destination_fd)
        source_after = os.fstat(source_fd)
        destination_state = os.fstat(destination_fd)
        if _receipt_file_fingerprint(source_before) != _receipt_file_fingerprint(source_after):
            raise RuntimeError(f"{label} changed while creating runtime snapshot")
        if expected_size is not None and offset != expected_size:
            raise RuntimeError(f"{label} copied byte count differs from receipt")
        if digest.hexdigest() != expected_sha256:
            raise RuntimeError(f"{label} SHA-256 differs from receipt during snapshot")
        if not stat.S_ISREG(destination_state.st_mode) or destination_state.st_size != offset:
            raise RuntimeError(f"{label} runtime snapshot is not a complete regular file")
    finally:
        if destination_fd is not None:
            os.close(destination_fd)
        os.close(source_fd)
    destination.chmod(0o400)
    if sha256_file(destination) != expected_sha256:
        raise RuntimeError(f"{label} runtime snapshot SHA-256 verification failed")


def _private_staging_parent() -> pathlib.Path:
    raw = os.environ.get("TMPDIR")
    if not raw:
        raise RuntimeError("TMPDIR must name an absolute private filesystem for external asset staging")
    parent = pathlib.Path(raw)
    if not parent.is_absolute() or parent.is_symlink() or not parent.is_dir():
        raise RuntimeError("TMPDIR must be an absolute non-symlink directory for external asset staging")
    resolved = parent.resolve(strict=True)
    if _lexical_absolute(parent) != resolved:
        raise RuntimeError("TMPDIR may not traverse symbolic links for external asset staging")
    state = resolved.stat(follow_symlinks=False)
    # NAS mounts may map the authenticated user's numeric UID to a server-side
    # owner, so st_uid equality is not portable here.  Require the stronger
    # observable property instead: the effective process can use the parent,
    # while POSIX group/other bits grant no access at all.
    if state.st_mode & (stat.S_IRWXG | stat.S_IRWXO) or not os.access(
        resolved,
        os.R_OK | os.W_OK | os.X_OK,
    ):
        raise RuntimeError(
            "TMPDIR must be private to and accessible by the current process"
        )
    return resolved


@contextlib.contextmanager
def staged_external_asset_set(asset_set: ExternalAssetSet) -> Iterator[ExternalAssetSet]:
    """Yield a private content-verified snapshot for the full Blender build.

    Every receipt file is copied from an ``O_NOFOLLOW`` descriptor while its
    bytes and inode fingerprint remain stable. Blender then consumes only the
    private snapshot, so concurrent replacement of the acquisition pathname
    cannot change bytes after verification. The random runtime path is never
    serialized; public provenance remains bound to the original receipt.
    """

    parent = _private_staging_parent()
    stage = pathlib.Path(tempfile.mkdtemp(prefix="vista-external-assets-", dir=parent))
    stage.chmod(0o700)
    try:
        receipt_source = _safe_existing_path(
            asset_set.root,
            ACQUISITION_RECEIPT_FILENAME,
            file_required=True,
        )
        _copy_verified_file_to_snapshot(
            receipt_source,
            stage / ACQUISITION_RECEIPT_FILENAME,
            expected_size=None,
            expected_sha256=asset_set.receipt_file_sha256,
            label="external acquisition receipt",
        )
        copied: set[str] = set()
        for asset in asset_set.assets:
            for receipt_file in asset.files:
                relative = f"{asset.source_relative_root}/{receipt_file.relative_path}"
                if relative in copied:
                    raise RuntimeError(f"external receipt repeats a runtime snapshot path: {relative}")
                copied.add(relative)
                source = _safe_existing_path(asset_set.root, relative, file_required=True)
                _copy_verified_file_to_snapshot(
                    source,
                    stage / pathlib.PurePosixPath(relative),
                    expected_size=receipt_file.size_bytes,
                    expected_sha256=receipt_file.sha256,
                    label=f"external receipt file {relative}",
                )
        yield ExternalAssetSet(
            root=stage.resolve(strict=True),
            receipt_digest=asset_set.receipt_digest,
            receipt_file_sha256=asset_set.receipt_file_sha256,
            acquisition_manifest_sha256=asset_set.acquisition_manifest_sha256,
            assets=asset_set.assets,
        )
    finally:
        shutil.rmtree(stage)


def _texture_receipt_file(asset: AcquiredAsset, semantic: str) -> AcquiredFile:
    matches = [item for item in asset.files if semantic in item.semantic]
    if len(matches) != 1:
        raise RuntimeError(
            f"verified asset {semantic} texture is absent or ambiguous: {asset.logical_asset_id}"
        )
    return matches[0]


def _texture_file(asset_set: ExternalAssetSet, asset: AcquiredAsset, semantic: str) -> pathlib.Path:
    receipt_file = _texture_receipt_file(asset, semantic)
    path = _safe_existing_path(
        asset_set.root,
        f"{asset.source_relative_root}/{receipt_file.relative_path}",
        file_required=True,
    )
    _verify_receipt_file_bytes(path, receipt_file, label="project-authored material texture")
    return path


def _realize_pbr_material(bpy: Any, asset_set: ExternalAssetSet, logical_id: str) -> Any:
    asset = asset_set.asset(logical_id)
    if asset.asset_type != "texture":
        raise RuntimeError(f"project-authored material source is not a texture: {logical_id}")
    identity = external_texture_material_identity_sha256(asset)
    material = bpy.data.materials.new(name=external_texture_material_name(asset))
    created_images: list[Any] = []
    try:
        material.use_nodes = True
        nodes = material.node_tree.nodes
        nodes.clear()
        output = nodes.new("ShaderNodeOutputMaterial")
        shader = nodes.new("ShaderNodeBsdfPrincipled")
        material.node_tree.links.new(shader.outputs["BSDF"], output.inputs["Surface"])
        for semantic, input_name, colorspace in (
            ("base_color", "Base Color", "sRGB"),
            ("roughness", "Roughness", "Non-Color"),
        ):
            image = _load_fresh_receipt_image(bpy, asset_set, asset, semantic)
            created_images.append(image)
            image.colorspace_settings.name = colorspace
            texture = nodes.new("ShaderNodeTexImage")
            texture.image = image
            material.node_tree.links.new(texture.outputs["Color"], shader.inputs[input_name])
        image = _load_fresh_receipt_image(bpy, asset_set, asset, "normal")
        created_images.append(image)
        image.colorspace_settings.name = "Non-Color"
        texture = nodes.new("ShaderNodeTexImage")
        texture.image = image
        normal = nodes.new("ShaderNodeNormalMap")
        normal.inputs["Strength"].default_value = 0.65
        material.node_tree.links.new(texture.outputs["Color"], normal.inputs["Color"])
        material.node_tree.links.new(normal.outputs["Normal"], shader.inputs["Normal"])
        material[EXTERNAL_TEXTURE_MATERIAL_SOURCE_PROPERTY] = logical_id
        material[EXTERNAL_TEXTURE_MATERIAL_SOURCE_DIGEST_PROPERTY] = asset.source_tree_sha256
        material[EXTERNAL_TEXTURE_MATERIAL_SEMANTICS_PROPERTY] = json.dumps(
            sorted(_REQUIRED_PBR), separators=(",", ":")
        )
        material[EXTERNAL_TEXTURE_MATERIAL_ALPHA_MODE_PROPERTY] = "OPAQUE"
        material[EXTERNAL_TEXTURE_MATERIAL_IDENTITY_PROPERTY] = identity
        material[EXTERNAL_TEXTURE_MATERIAL_RECEIPT_PROPERTY] = (
            f"external_placement/asset_sources/{logical_id}"
        )
        if set(material.keys()) != EXTERNAL_TEXTURE_MATERIAL_CONTRACT_PROPERTIES:
            raise RuntimeError("external texture material extras are not a closed contract")
        return material
    except BaseException:
        for image in reversed(created_images):
            try:
                bpy.data.images.remove(image)
            except (ReferenceError, RuntimeError, TypeError):
                pass
        try:
            bpy.data.materials.remove(material)
        except (ReferenceError, RuntimeError, TypeError):
            pass
        raise


def _external_texture_material_contract(
    material: Any,
    asset: AcquiredAsset,
) -> dict[str, Any]:
    identity = external_texture_material_identity_sha256(asset)
    semantics = sorted(_REQUIRED_PBR)
    expected_name = external_texture_material_name(asset)
    if (
        material.name != expected_name
        or set(material.keys()) != EXTERNAL_TEXTURE_MATERIAL_CONTRACT_PROPERTIES
        or material.get(EXTERNAL_TEXTURE_MATERIAL_SOURCE_PROPERTY) != asset.logical_asset_id
        or material.get(EXTERNAL_TEXTURE_MATERIAL_SOURCE_DIGEST_PROPERTY)
        != asset.source_tree_sha256
        or material.get(EXTERNAL_TEXTURE_MATERIAL_SEMANTICS_PROPERTY)
        != json.dumps(semantics, separators=(",", ":"))
        or material.get(EXTERNAL_TEXTURE_MATERIAL_ALPHA_MODE_PROPERTY) != "OPAQUE"
        or material.get(EXTERNAL_TEXTURE_MATERIAL_IDENTITY_PROPERTY) != identity
        or material.get(EXTERNAL_TEXTURE_MATERIAL_RECEIPT_PROPERTY)
        != f"external_placement/asset_sources/{asset.logical_asset_id}"
    ):
        raise RuntimeError("external texture material differs from its closed receipt contract")
    contract = {
        "schema_version": EXTERNAL_TEXTURE_MATERIAL_CONTRACT_SCHEMA,
        "material_id": expected_name,
        "source_logical_asset_id": asset.logical_asset_id,
        "source_tree_sha256": asset.source_tree_sha256,
        "material_identity_sha256": identity,
        "active_texture_semantics": semantics,
        "alpha_mode": "OPAQUE",
        "alpha_cutoff": None,
        "pbr_source": asset_digest_record(asset),
    }
    if set(contract) != EXTERNAL_TEXTURE_MATERIAL_CONTRACT_KEYS:
        raise RuntimeError("external texture material receipt fields are not closed")
    return contract


def _relink(obj: Any, collection: Any) -> None:
    for current in tuple(obj.users_collection):
        current.objects.unlink(obj)
    collection.objects.link(obj)


def _metric_box_uv(
    coordinate_m: Sequence[float],
    normal: Sequence[float],
    *,
    meters_per_tile: float = AUTHORED_UV_METERS_PER_TILE,
) -> tuple[float, float]:
    """Return a stable box projection whose UV distance is measured in metres."""

    if len(coordinate_m) != 3 or len(normal) != 3:
        raise RuntimeError("metric box UV input must contain three coordinates")
    try:
        point = tuple(float(value) for value in coordinate_m)
        direction = tuple(float(value) for value in normal)
        tile_size = float(meters_per_tile)
    except (TypeError, ValueError, OverflowError) as error:
        raise RuntimeError("metric box UV input is invalid") from error
    if (
        not math.isfinite(tile_size)
        or tile_size <= 0
        or not all(math.isfinite(value) for value in (*point, *direction))
        or max(abs(value) for value in direction) <= 1e-12
    ):
        raise RuntimeError("metric box UV input is invalid")
    # Ties deliberately prefer X, then Y, then Z so bevel normals never make
    # the mapping dependent on collection iteration order.
    axis = max(range(3), key=lambda index: (abs(direction[index]), -index))
    sign = 1.0 if direction[axis] >= 0 else -1.0
    scale = 1.0 / tile_size
    if axis == 0:
        return -sign * point[1] * scale, point[2] * scale
    if axis == 1:
        return sign * point[0] * scale, point[2] * scale
    return sign * point[0] * scale, point[1] * scale


def _apply_metric_box_uv(obj: Any) -> None:
    """Replace primitive UVs with deterministic metric box projection."""

    mesh = obj.data
    while len(mesh.uv_layers):
        mesh.uv_layers.remove(mesh.uv_layers[0])
    layer = mesh.uv_layers.new(name="VISTA_MetricUV")
    for polygon in mesh.polygons:
        normal = tuple(float(value) for value in polygon.normal)
        for loop_index in polygon.loop_indices:
            loop = mesh.loops[loop_index]
            coordinate = tuple(float(value) for value in mesh.vertices[loop.vertex_index].co)
            layer.data[loop_index].uv = _metric_box_uv(coordinate, normal)
    mesh.uv_layers.active = layer
    layer.active_render = True
    mesh.update()
    obj["vista_uv_mapping"] = "metric_box_v1"
    obj["vista_uv_meters_per_tile"] = AUTHORED_UV_METERS_PER_TILE


def _cube_part(
    bpy: Any,
    collection: Any,
    name: str,
    center: Sequence[float],
    dimensions: Sequence[float],
    material: Any,
) -> Any:
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=tuple(center))
    obj = bpy.context.active_object
    obj.name = name[:63]
    _relink(obj, collection)
    obj.dimensions = tuple(dimensions)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.transform_apply(location=True, rotation=False, scale=True)
    bevel = obj.modifiers.new(name="VISTA_AuthoredEdge", type="BEVEL")
    bevel.width = min(0.018, min(float(value) for value in dimensions) * 0.12)
    bevel.segments = 3
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.modifier_apply(modifier=bevel.name)
    for polygon in obj.data.polygons:
        polygon.use_smooth = True
    _apply_metric_box_uv(obj)
    obj.data.materials.append(material)
    return obj


def _authored_recipe(
    bpy: Any,
    collection: Any,
    recipe: str,
    dimensions: Sequence[float],
    materials_by_logical_id: Mapping[str, Any],
) -> list[Any]:
    expected_materials = AUTHORED_RECIPE_MATERIAL_IDS.get(recipe)
    if expected_materials is None:
        raise RuntimeError(f"unsupported project-authored furniture recipe: {recipe}")
    missing = [logical_id for logical_id in expected_materials if logical_id not in materials_by_logical_id]
    if missing:
        raise RuntimeError(f"project-authored recipe material is unavailable: {recipe}: {missing}")
    oak = materials_by_logical_id["visual.material.white_oak_veneer"]
    wool = materials_by_logical_id.get("visual.material.poly_wool_herringbone")
    x, y, z = (float(value) for value in dimensions)
    parts: list[Any] = []

    def add(suffix: str, center: Sequence[float], dims: Sequence[float], material: Any) -> None:
        parts.append(
            _cube_part(
                bpy,
                collection,
                f"VISTA_External_{recipe}_{suffix}",
                center,
                dims,
                material,
            )
        )

    if recipe == "contemporary_shoe_bench_v1":
        add("seat", (0, 0, z - 0.055), (x, y, 0.11), wool)
        add("shelf", (0, 0, 0.16), (x * 0.88, y * 0.82, 0.055), oak)
        for sx in (-1, 1):
            add(
                f"leg_{sx}",
                (sx * (x / 2 - 0.055), 0, (z - 0.11) / 2),
                (0.07, y * 0.82, z - 0.11),
                oak,
            )
    elif recipe == "contemporary_sofa_v1":
        add("base", (0, 0, z * 0.25), (x * 0.90, y, z * 0.28), wool)
        add("plinth", (0, 0, z * 0.11), (x * 0.76, y * 0.68, z * 0.12), oak)
        for sx in (-1, 1):
            for sy in (-1, 1):
                add(
                    f"low_leg_{sx}_{sy}",
                    (sx * x * 0.34, sy * y * 0.27, z * 0.035),
                    (x * 0.055, y * 0.055, z * 0.07),
                    oak,
                )
        cushion_x = x * 0.238
        gap = x * 0.018
        for index, cx in enumerate((-(cushion_x + gap), 0.0, cushion_x + gap)):
            add(
                f"seat_cushion_{index + 1}",
                (cx, -y * 0.055, z * 0.49),
                (cushion_x, y * 0.66, z * 0.18),
                wool,
            )
            add(
                f"back_cushion_{index + 1}",
                (cx, y * 0.34, z * 0.68),
                (cushion_x, y * 0.16, z * 0.64),
                wool,
            )
        for sx in (-1, 1):
            add(f"arm_{sx}", (sx * x * 0.45, 0, z * 0.47), (x * 0.10, y * 0.88, z * 0.56), wool)
    elif recipe == "contemporary_dining_table_v1":
        add("top", (0, 0, z - 0.055), (x, y, 0.11), oak)
        for sx in (-1, 1):
            for sy in (-1, 1):
                add(
                    f"leg_{sx}_{sy}",
                    (sx * (x / 2 - 0.10), sy * (y / 2 - 0.09), (z - 0.11) / 2),
                    (0.075, 0.075, z - 0.11),
                    oak,
                )
    else:
        raise RuntimeError(f"unsupported project-authored furniture recipe: {recipe}")
    return parts


def _authored_recipe_material_sources(meshes: Sequence[Any]) -> frozenset[str]:
    sources: set[str] = set()
    for obj in meshes:
        used_indices = {int(polygon.material_index) for polygon in obj.data.polygons}
        if not used_indices:
            raise RuntimeError(f"project-authored recipe part has no material use: {obj.name}")
        if any(index < 0 or index >= len(obj.material_slots) for index in used_indices):
            raise RuntimeError(f"project-authored recipe part has an invalid material binding: {obj.name}")
        if len(used_indices) != len(obj.material_slots):
            raise RuntimeError(f"project-authored recipe part has an unused material slot: {obj.name}")
        for index in used_indices:
            material = obj.material_slots[index].material
            if material is None:
                raise RuntimeError(f"project-authored recipe part has an unbound material: {obj.name}")
            logical_id = material.get(EXTERNAL_TEXTURE_MATERIAL_SOURCE_PROPERTY)
            if type(logical_id) is not str:
                raise RuntimeError(f"project-authored recipe material lacks provenance: {material.name}")
            sources.add(logical_id)
    return frozenset(sources)


def _validate_authored_recipe_material_use(
    recipe: str,
    meshes: Sequence[Any],
    materials_by_logical_id: Mapping[str, Any],
) -> tuple[str, ...]:
    expected = AUTHORED_RECIPE_MATERIAL_IDS.get(recipe)
    if expected is None:
        raise RuntimeError(f"unsupported project-authored furniture recipe: {recipe}")
    actual = _authored_recipe_material_sources(meshes)
    if actual != frozenset(expected):
        raise RuntimeError(
            f"project-authored recipe material use differs from contract: {recipe}: "
            f"actual={sorted(actual)}, expected={list(expected)}"
        )
    for logical_id in actual:
        expected_material = materials_by_logical_id.get(logical_id)
        if expected_material is None:
            raise RuntimeError(f"project-authored recipe used an unrealized material: {logical_id}")
        for obj in meshes:
            for slot in obj.material_slots:
                if (
                    slot.material is not None
                    and slot.material.get(EXTERNAL_TEXTURE_MATERIAL_SOURCE_PROPERTY) == logical_id
                ):
                    if slot.material is not expected_material:
                        raise RuntimeError(
                            f"project-authored material provenance points at the wrong datablock: {logical_id}"
                        )
    return tuple(sorted(actual))


def _combined_bounds(mathutils: Any, objects: Sequence[Any]) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    # Transform actual vertices.  A transformed local AABB is only an outer
    # envelope for rotated meshes and changes when the same transform is baked
    # into vertex coordinates, which would make normalization non-invariant.
    points = [
        obj.matrix_world @ vertex.co
        for obj in objects
        for vertex in getattr(getattr(obj, "data", None), "vertices", ())
    ]
    if not points:
        raise RuntimeError("external source contains no measurable mesh bounds")
    minimum = tuple(min(float(point[index]) for point in points) for index in range(3))
    maximum = tuple(max(float(point[index]) for point in points) for index in range(3))
    if any(not math.isfinite(value) for value in (*minimum, *maximum)) or any(
        maximum[index] <= minimum[index] for index in range(3)
    ):
        raise RuntimeError("external source has invalid measured bounds")
    return minimum, maximum


def _has_collection_items(value: Any) -> bool:
    if value is None:
        return False
    try:
        return len(value) > 0
    except (TypeError, AttributeError):
        return bool(tuple(value))


def _runtime_identity(value: Any) -> tuple[str, int]:
    as_pointer = getattr(value, "as_pointer", None)
    if callable(as_pointer):
        try:
            pointer = int(as_pointer())
        except (ReferenceError, RuntimeError, TypeError, ValueError, OverflowError):
            pointer = 0
        if pointer > 0:
            return "bpy", pointer
    return "python", id(value)


def _node_trees(material: Any) -> tuple[Any, ...]:
    pending = [material.node_tree]
    result: list[Any] = []
    seen: set[tuple[str, int]] = set()
    while pending:
        tree = pending.pop()
        identity = _runtime_identity(tree)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(tree)
        for node in tree.nodes:
            child = getattr(node, "node_tree", None)
            if child is not None:
                pending.append(child)
    return tuple(result)


def _block_has_animation_or_drivers(block: Any) -> bool:
    return block is not None and getattr(block, "animation_data", None) is not None


def _require_local_id(block: Any, *, label: str) -> None:
    if block is not None and getattr(block, "library", None) is not None:
        raise RuntimeError(f"external source contains a nested linked-library ID: {label}")
    if block is not None and getattr(block, "override_library", None) is not None:
        raise RuntimeError(f"external source contains a library override ID: {label}")


def _identity_vector(value: Any, expected: Sequence[float], tolerance: float = 1e-9) -> bool:
    try:
        actual = tuple(float(item) for item in value)
    except (TypeError, ValueError, OverflowError):
        return False
    return len(actual) == len(expected) and all(
        math.isfinite(actual[index]) and abs(actual[index] - float(expected[index])) <= tolerance
        for index in range(len(expected))
    )


@dataclass(frozen=True)
class _StaticizedSource:
    meshes: tuple[Any, ...]
    source_object_names: tuple[str, ...]
    selection_policy: Mapping[str, Any]
    input_inventory: tuple[Mapping[str, Any], ...]
    input_inventory_sha256: str
    input_actions: tuple[Mapping[str, Any], ...]
    exclusions: tuple[Mapping[str, Any], ...]
    depsgraph_mode: str
    source_collection: Any


def _flat_matrix(value: Any) -> list[float]:
    try:
        result = [
            _receipt_float(value[row][column])
            for row in range(4)
            for column in range(4)
        ]
    except (IndexError, TypeError, ValueError, OverflowError) as error:
        raise RuntimeError("external source matrix cannot be serialized") from error
    if any(not math.isfinite(item) for item in result):
        raise RuntimeError("external source matrix contains a non-finite value")
    return result


def _driver_count(block: Any) -> int:
    animation = getattr(block, "animation_data", None)
    return len(getattr(animation, "drivers", ())) if animation is not None else 0


def _action_name(block: Any) -> str | None:
    animation = getattr(block, "animation_data", None)
    action = getattr(animation, "action", None) if animation is not None else None
    return str(action.name) if action is not None else None


def _staticization_input_inventory(objects: Sequence[Any]) -> tuple[dict[str, Any], ...]:
    loaded_identities = {_runtime_identity(obj) for obj in objects}
    rows: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    materials: list[Any] = []
    supported_types = {"MESH", "CURVE", "ARMATURE", "EMPTY"}
    for obj in sorted(objects, key=lambda item: item.name):
        if obj.name in seen_names:
            raise RuntimeError("external staticization input object names are duplicated")
        seen_names.add(obj.name)
        if obj.type not in supported_types:
            raise RuntimeError(
                f"external staticization input type is unsupported: {obj.name}: {obj.type}"
            )
        _require_local_id(obj, label=f"object {obj.name}")
        _require_local_id(getattr(obj, "data", None), label=f"object data {obj.name}")
        parent = getattr(obj, "parent", None)
        if parent is not None and _runtime_identity(parent) not in loaded_identities:
            raise RuntimeError(f"external object has a parent outside the appended source: {obj.name}")
        if _driver_count(obj) or _driver_count(getattr(obj, "data", None)):
            raise RuntimeError(f"external source contains drivers: {obj.name}")
        if getattr(obj, "rigid_body", None) is not None:
            raise RuntimeError(f"external object contains rigid-body state: {obj.name}")
        if getattr(obj, "rigid_body_constraint", None) is not None:
            raise RuntimeError(f"external object contains a rigid-body constraint: {obj.name}")
        if getattr(obj, "soft_body", None) is not None:
            raise RuntimeError(f"external object contains soft-body state: {obj.name}")
        if _has_collection_items(getattr(obj, "particle_systems", ())):
            raise RuntimeError(f"external object contains particle-system state: {obj.name}")
        force_field = getattr(obj, "field", None)
        if force_field is not None and getattr(force_field, "type", "NONE") != "NONE":
            raise RuntimeError(f"external object contains a non-NONE force field: {obj.name}")
        if (
            getattr(obj, "instance_type", "NONE") != "NONE"
            or getattr(obj, "instance_collection", None) is not None
        ):
            raise RuntimeError(f"external object uses unsupported instancing: {obj.name}")
        modifier_rows: list[dict[str, Any]] = []
        for modifier in getattr(obj, "modifiers", ()):
            if bool(modifier.show_viewport) != bool(modifier.show_render):
                raise RuntimeError(
                    f"external modifier viewport/render evaluation differs: {obj.name}.{modifier.name}"
                )
            node_group = getattr(modifier, "node_group", None)
            _require_local_id(node_group, label=f"modifier node group {obj.name}.{modifier.name}")
            if node_group is not None and _driver_count(node_group):
                raise RuntimeError(
                    f"external modifier node group contains drivers: {obj.name}.{modifier.name}"
                )
            modifier_rows.append(
                {
                    "name": str(modifier.name),
                    "type": str(modifier.type),
                    "show_viewport": bool(modifier.show_viewport),
                    "show_render": bool(modifier.show_render),
                    "node_group": str(node_group.name) if node_group is not None else None,
                }
            )
        constraint_rows = [
            {
                "name": str(constraint.name),
                "type": str(constraint.type),
                "mute": bool(getattr(constraint, "mute", False)),
                "influence": _receipt_float(getattr(constraint, "influence", 1.0)),
            }
            for constraint in getattr(obj, "constraints", ())
        ]
        if any(not math.isfinite(item["influence"]) for item in constraint_rows):
            raise RuntimeError(f"external constraint influence is non-finite: {obj.name}")
        slots = [
            str(slot.material.name) if slot.material is not None else None
            for slot in getattr(obj, "material_slots", ())
        ]
        for slot in getattr(obj, "material_slots", ()):
            material = getattr(slot, "material", None)
            if material is not None and material not in materials:
                materials.append(material)
        data = getattr(obj, "data", None)
        topology = None
        if obj.type == "MESH":
            topology = {
                "vertices": len(data.vertices),
                "edges": len(data.edges),
                "loops": len(data.loops),
                "polygons": len(data.polygons),
            }
        rows.append(
            {
                "object_name": str(obj.name),
                "object_type": str(obj.type),
                "data_name": str(data.name) if data is not None else None,
                "parent_name": str(parent.name) if parent is not None else None,
                "parent_type": str(getattr(obj, "parent_type", "OBJECT")),
                "hide_render": bool(getattr(obj, "hide_render", False)),
                "hide_viewport": bool(getattr(obj, "hide_viewport", False)),
                "matrix_world": _flat_matrix(obj.matrix_world),
                "source_topology": topology,
                "material_slots": slots,
                "modifiers": modifier_rows,
                "constraints": constraint_rows,
                "action": _action_name(obj),
            }
        )
    for material in materials:
        _require_local_id(material, label=f"material {material.name}")
        if _block_has_animation_or_drivers(material):
            raise RuntimeError(f"external material contains animations or drivers: {material.name}")
        if getattr(material, "node_tree", None) is not None:
            for tree in _node_trees(material):
                _require_local_id(tree, label=f"material node tree {material.name}")
                if _block_has_animation_or_drivers(tree):
                    raise RuntimeError(
                        f"external material nodes contain animations or drivers: {material.name}"
                    )
    return tuple(rows)


def _staticization_action_inventory(actions: Sequence[Any]) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    for action in sorted(actions, key=lambda item: item.name):
        _require_local_id(action, label=f"action {action.name}")
        start, end = (_receipt_float(value) for value in action.frame_range)
        rows.append(
            {
                "name": str(action.name),
                "frame_range": [start, end],
                "fcurve_count": len(getattr(action, "fcurves", ())),
            }
        )
    return tuple(rows)


def _validate_static_source(bpy: Any, objects: Sequence[Any], new_actions: Sequence[Any]) -> list[Any]:
    forbidden = [obj for obj in objects if obj.type in {"ARMATURE", "CAMERA", "LIGHT"}]
    if forbidden:
        raise RuntimeError(f"external source contains forbidden object types: {[obj.type for obj in forbidden]}")
    unsupported = sorted({obj.type for obj in objects if obj.type not in {"MESH", "EMPTY"}})
    if unsupported:
        raise RuntimeError(f"external source contains unsupported drawable object types: {unsupported}")
    if new_actions:
        raise RuntimeError("external source contains animations")
    loaded_identities = {id(obj) for obj in objects}
    materials: list[Any] = []
    for obj in objects:
        _require_local_id(obj, label=f"object {obj.name}")
        _require_local_id(getattr(obj, "data", None), label=f"object data {obj.name}")
        if _has_collection_items(getattr(obj, "modifiers", ())):
            raise RuntimeError(f"external object contains modifiers: {obj.name}")
        if _has_collection_items(getattr(obj, "constraints", ())):
            raise RuntimeError(f"external object contains constraints: {obj.name}")
        if getattr(obj, "rigid_body", None) is not None:
            raise RuntimeError(f"external object contains rigid-body state: {obj.name}")
        if getattr(obj, "rigid_body_constraint", None) is not None:
            raise RuntimeError(f"external object contains a rigid-body constraint: {obj.name}")
        if getattr(obj, "soft_body", None) is not None:
            raise RuntimeError(f"external object contains soft-body state: {obj.name}")
        if _has_collection_items(getattr(obj, "particle_systems", ())):
            raise RuntimeError(f"external object contains particle-system state: {obj.name}")
        force_field = getattr(obj, "field", None)
        if force_field is not None and getattr(force_field, "type", "NONE") != "NONE":
            raise RuntimeError(f"external object contains a non-NONE force field: {obj.name}")
        if getattr(obj, "instance_type", "NONE") != "NONE" or getattr(obj, "instance_collection", None) is not None:
            raise RuntimeError(f"external object uses unsupported instancing: {obj.name}")
        if getattr(obj, "rotation_mode", None) != "XYZ":
            raise RuntimeError(f"external object rotation mode is not deterministic XYZ: {obj.name}")
        if (
            not _identity_vector(getattr(obj, "delta_location", (0.0, 0.0, 0.0)), (0.0, 0.0, 0.0))
            or not _identity_vector(getattr(obj, "delta_rotation_euler", (0.0, 0.0, 0.0)), (0.0, 0.0, 0.0))
            or not _identity_vector(getattr(obj, "delta_scale", (1.0, 1.0, 1.0)), (1.0, 1.0, 1.0))
        ):
            raise RuntimeError(f"external object contains non-identity delta transforms: {obj.name}")
        parent = getattr(obj, "parent", None)
        if parent is not None and id(parent) not in loaded_identities:
            raise RuntimeError(f"external object has a parent outside the appended source: {obj.name}")
        if _block_has_animation_or_drivers(obj) or _block_has_animation_or_drivers(getattr(obj, "data", None)):
            raise RuntimeError(f"external source contains animations or drivers: {obj.name}")
        for slot in getattr(obj, "material_slots", ()):
            material = getattr(slot, "material", None)
            if material is not None and material not in materials:
                materials.append(material)
    for material in materials:
        _require_local_id(material, label=f"material {material.name}")
        if _block_has_animation_or_drivers(material):
            raise RuntimeError(f"external material contains animations or drivers: {material.name}")
        if getattr(material, "node_tree", None) is not None:
            for tree in _node_trees(material):
                _require_local_id(tree, label=f"material node tree {material.name}")
                if _block_has_animation_or_drivers(tree):
                    raise RuntimeError(f"external material nodes contain animations or drivers: {material.name}")
    meshes = [obj for obj in objects if obj.type == "MESH"]
    if not meshes:
        raise RuntimeError("external source contains no static meshes")
    for obj in meshes:
        if getattr(obj.data, "shape_keys", None) is not None:
            raise RuntimeError("external source contains shape keys")
        if not obj.material_slots or any(slot.material is None for slot in obj.material_slots):
            raise RuntimeError(f"external mesh has an unbound material: {obj.name}")
        if any(poly.material_index >= len(obj.material_slots) for poly in obj.data.polygons):
            raise RuntimeError(f"external mesh primitive has an invalid material binding: {obj.name}")
    return meshes


def _runtime_receipt_texture_paths(
    asset_set: ExternalAssetSet,
    asset: AcquiredAsset,
) -> dict[pathlib.Path, AcquiredFile]:
    expected: dict[pathlib.Path, AcquiredFile] = {}
    for receipt_file in asset.files:
        if receipt_file.dimensions_px is None:
            continue
        path = _safe_existing_path(
            asset_set.root,
            f"{asset.source_relative_root}/{receipt_file.relative_path}",
            file_required=True,
        )
        if path in expected:
            raise RuntimeError(
                f"external receipt maps multiple textures to one path: {receipt_file.relative_path}"
            )
        expected[path] = receipt_file
    if not expected:
        raise RuntimeError(f"external model receipt has no verified texture paths: {asset.logical_asset_id}")
    return expected


def _resolved_runtime_image_path(bpy: Any, image: Any) -> pathlib.Path:
    source = getattr(image, "source", None)
    if source != "FILE":
        raise RuntimeError(f"external material image source must be FILE, not {source!r}")
    packed_files = getattr(image, "packed_files", ())
    if getattr(image, "packed_file", None) is not None or _has_collection_items(packed_files):
        raise RuntimeError("external material image may not be packed")
    raw = getattr(image, "filepath_raw", None)
    if type(raw) is not str or not raw:
        raise RuntimeError("external material FILE image lacks filepath_raw")
    library = getattr(image, "library", None)
    try:
        expanded = bpy.path.abspath(raw, library=library)
    except Exception as error:
        raise RuntimeError("external material image filepath_raw cannot be resolved") from error
    _require_local_id(image, label="material image")
    try:
        candidate = pathlib.Path(os.fspath(expanded))
    except TypeError as error:
        raise RuntimeError("external material image resolved path is invalid") from error
    if not candidate.is_absolute():
        raise RuntimeError("external material image did not resolve to an absolute path")
    lexical = _lexical_absolute(candidate)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise RuntimeError("external material image path is unavailable") from error
    if lexical != resolved:
        raise RuntimeError("external material image path may not traverse symbolic links")
    if not resolved.is_file():
        raise RuntimeError("external material image path is not a regular file")
    return resolved


def _named_socket(sockets: Any, name: str, *, label: str) -> Any:
    socket = sockets.get(name) if hasattr(sockets, "get") else None
    if socket is None:
        for candidate in sockets:
            if getattr(candidate, "name", None) == name:
                socket = candidate
                break
    if socket is None:
        raise RuntimeError(f"external material lacks required {label} socket {name!r}")
    return socket


def _socket_links(socket: Any) -> tuple[Any, ...]:
    return tuple(getattr(socket, "links", ()))


def _single_input_link(node: Any, socket_name: str, *, label: str) -> tuple[Any, Any]:
    socket = _named_socket(node.inputs, socket_name, label=label)
    links = _socket_links(socket)
    if len(links) != 1:
        raise RuntimeError(f"external material {label} must have exactly one input link")
    link = links[0]
    if getattr(link, "to_socket", None) != socket or getattr(link, "from_node", None) is None:
        raise RuntimeError(f"external material {label} contains an ambiguous input link")
    return link, socket


def _all_output_links(node: Any) -> tuple[Any, ...]:
    return tuple(
        link
        for socket in getattr(node, "outputs", ())
        for link in _socket_links(socket)
    )


def _require_exclusive_output_link(
    node: Any,
    expected_link: Any,
    expected_target: Any,
    *,
    output_names: frozenset[str],
    label: str,
) -> None:
    links = _all_output_links(node)
    if (
        len(links) != 1
        or getattr(links[0], "from_node", None) != node
        or getattr(links[0], "to_socket", None) != expected_target
        or getattr(expected_link, "to_socket", None) != expected_target
        or getattr(getattr(links[0], "from_socket", None), "name", None) not in output_names
    ):
        raise RuntimeError(f"external material {label} output link is ambiguous or misrouted")


def _direct_semantic_image_node(
    shader: Any,
    input_name: str,
    semantic: str,
    *,
    output_names: frozenset[str] = frozenset({"Color"}),
) -> Any:
    link, target = _single_input_link(shader, input_name, label=semantic)
    node = link.from_node
    if getattr(node, "type", None) != "TEX_IMAGE" or getattr(node, "image", None) is None:
        raise RuntimeError(f"external material {semantic} must link directly from one image texture")
    _require_exclusive_output_link(
        node,
        link,
        target,
        output_names=output_names,
        label=semantic,
    )
    return node


def _normal_semantic_image_node(shader: Any) -> Any:
    shader_link, shader_target = _single_input_link(shader, "Normal", label="normal")
    normal_node = shader_link.from_node
    if getattr(normal_node, "type", None) != "NORMAL_MAP":
        raise RuntimeError("external material normal must link through one Normal Map node")
    _require_exclusive_output_link(
        normal_node,
        shader_link,
        shader_target,
        output_names=frozenset({"Normal"}),
        label="normal-map",
    )
    image_link, image_target = _single_input_link(normal_node, "Color", label="normal-map color")
    image_node = image_link.from_node
    if getattr(image_node, "type", None) != "TEX_IMAGE" or getattr(image_node, "image", None) is None:
        raise RuntimeError("external material normal map must link directly from one image texture")
    _require_exclusive_output_link(
        image_node,
        image_link,
        image_target,
        output_names=frozenset({"Color"}),
        label="normal",
    )
    return image_node


def _reachable_upstream_nodes(start: Any) -> tuple[Any, ...]:
    pending = [start]
    result: list[Any] = []
    seen: set[tuple[str, int]] = set()
    while pending:
        node = pending.pop()
        identity = _runtime_identity(node)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(node)
        for socket in getattr(node, "inputs", ()):
            for link in _socket_links(socket):
                source = getattr(link, "from_node", None)
                if source is not None:
                    pending.append(source)
    return tuple(result)


def _active_surface_semantic_images(
    material: Any,
    *,
    allow_inactive_receipt_images: bool = False,
) -> dict[str, Any]:
    tree = material.node_tree
    nodes = tuple(tree.nodes)
    if any(
        getattr(node, "type", None) == "GROUP" or getattr(node, "node_tree", None) is not None
        for node in nodes
    ):
        raise RuntimeError(
            f"external material nested node groups require a separately verified sanitization: {material.name}"
        )
    for node in nodes:
        for attribute in ("object", "texture", "collection"):
            reference = getattr(node, attribute, None)
            if reference is not None:
                _require_local_id(
                    reference,
                    label=f"material node {getattr(node, 'name', '<unnamed>')}.{attribute}",
                )
    active_outputs = [
        node
        for node in nodes
        if getattr(node, "type", None) == "OUTPUT_MATERIAL"
        and getattr(node, "is_active_output", False) is True
    ]
    if len(active_outputs) != 1:
        raise RuntimeError(f"external material must have exactly one active Material Output: {material.name}")
    for socket_name in ("Volume", "Displacement"):
        socket = _named_socket(
            active_outputs[0].inputs,
            socket_name,
            label="active Material Output",
        )
        if _socket_links(socket):
            raise RuntimeError(
                f"external material active Material Output {socket_name} is unsupported"
            )
    surface_link, surface_target = _single_input_link(
        active_outputs[0],
        "Surface",
        label="active Material Output Surface",
    )
    shader = surface_link.from_node
    if getattr(shader, "type", None) != "BSDF_PRINCIPLED":
        raise RuntimeError("external material active Surface must link directly from Principled BSDF")
    allowed_shader_links = {"Base Color", "Roughness", "Normal", "Metallic", "Alpha"}
    unsupported_shader_links = sorted(
        getattr(socket, "name", "<unnamed>")
        for socket in shader.inputs
        if _socket_links(socket) and getattr(socket, "name", None) not in allowed_shader_links
    )
    if unsupported_shader_links:
        raise RuntimeError(
            f"external material Principled BSDF has unsupported linked inputs: "
            f"{unsupported_shader_links}"
        )
    _require_exclusive_output_link(
        shader,
        surface_link,
        surface_target,
        output_names=frozenset({"BSDF"}),
        label="Principled Surface",
    )
    semantic_nodes = {
        "base_color": _direct_semantic_image_node(shader, "Base Color", "base_color"),
        "roughness": _direct_semantic_image_node(shader, "Roughness", "roughness"),
        "normal": _normal_semantic_image_node(shader),
    }
    for semantic, input_name, output_names in (
        ("metalness", "Metallic", frozenset({"Color"})),
        ("opacity", "Alpha", frozenset({"Color", "Alpha"})),
    ):
        socket = _named_socket(shader.inputs, input_name, label=semantic)
        if _socket_links(socket):
            semantic_nodes[semantic] = _direct_semantic_image_node(
                shader,
                input_name,
                semantic,
                output_names=output_names,
            )
    if len({_runtime_identity(node) for node in semantic_nodes.values()}) != len(semantic_nodes):
        raise RuntimeError("external material reuses one image node for ambiguous PBR semantics")
    reachable = _reachable_upstream_nodes(shader)
    reachable_images = {
        _runtime_identity(node): node
        for node in reachable
        if getattr(node, "image", None) is not None
    }
    all_images = {
        _runtime_identity(node): node
        for node in nodes
        if getattr(node, "image", None) is not None
    }
    disconnected = sorted(
        getattr(node, "name", "<unnamed>")
        for identity, node in all_images.items()
        if identity not in reachable_images
    )
    if disconnected and not allow_inactive_receipt_images:
        raise RuntimeError(f"external material contains disconnected image impostors: {disconnected}")
    mapped = {_runtime_identity(node) for node in semantic_nodes.values()}
    unexpected = sorted(
        getattr(node, "name", "<unnamed>")
        for identity, node in reachable_images.items()
        if identity not in mapped
    )
    if unexpected:
        raise RuntimeError(f"external material routes images through unsupported sockets: {unexpected}")
    return semantic_nodes


def _inactive_image_nodes(
    material: Any,
    semantic_nodes: Mapping[str, Any],
) -> tuple[Any, ...]:
    base_links = _all_output_links(semantic_nodes["base_color"])
    if len(base_links) != 1:
        raise RuntimeError("external material base color graph is no longer closed")
    shader = getattr(base_links[0], "to_node", None)
    if getattr(shader, "type", None) != "BSDF_PRINCIPLED":
        raise RuntimeError("external material base color no longer identifies Principled BSDF")
    reachable = {
        _runtime_identity(node)
        for node in _reachable_upstream_nodes(shader)
        if getattr(node, "image", None) is not None
    }
    return tuple(
        sorted(
            (
                node
                for node in material.node_tree.nodes
                if getattr(node, "image", None) is not None
                and _runtime_identity(node) not in reachable
            ),
            key=lambda node: getattr(node, "name", ""),
        )
    )


def _indexed_socket(sockets: Any, index: int, *, label: str) -> Any:
    try:
        socket = sockets[index]
    except (IndexError, KeyError, TypeError) as error:
        raise RuntimeError(f"external material lacks required {label} socket {index}") from error
    if socket is None:
        raise RuntimeError(f"external material lacks required {label} socket {index}")
    return socket


def _require_dithered_surface(material: Any) -> None:
    """Set Blender's viewport mode, while refusing to treat it as glTF proof."""

    if not hasattr(material, "surface_render_method"):
        raise RuntimeError("external material lacks Blender 4.5 surface_render_method")
    try:
        material.surface_render_method = "DITHERED"
    except (AttributeError, TypeError, ValueError) as error:
        raise RuntimeError("external material cannot use Blender 4.5 DITHERED rendering") from error
    if material.surface_render_method != "DITHERED":
        raise RuntimeError("external material did not retain Blender 4.5 DITHERED rendering")


def _validate_masked_alpha_graph(material: Any, opacity_node: Any) -> None:
    """Revalidate the exact graph Blender 4.5 exports as glTF MASK."""

    opacity_links = _all_output_links(opacity_node)
    if len(opacity_links) != 1:
        raise RuntimeError("external opacity image must feed exactly one alpha-clip node")
    opacity_link = opacity_links[0]
    clip = getattr(opacity_link, "to_node", None)
    clip_input = getattr(opacity_link, "to_socket", None)
    if (
        getattr(opacity_link, "from_node", None) != opacity_node
        or getattr(getattr(opacity_link, "from_socket", None), "name", None)
        not in {"Color", "Alpha"}
        or getattr(clip, "type", None) != "MATH"
        or getattr(clip, "operation", None) != "GREATER_THAN"
        or clip_input != _indexed_socket(clip.inputs, 0, label="alpha-clip input")
    ):
        raise RuntimeError("external opacity image is not connected to the exact alpha-clip input")
    threshold = _indexed_socket(clip.inputs, 1, label="alpha-clip threshold")
    if _socket_links(threshold) or float(getattr(threshold, "default_value", math.nan)) != EXTERNAL_MATERIAL_ALPHA_CUTOFF:
        raise RuntimeError("external alpha-clip threshold differs from the closed 0.5 policy")
    clip_output = _indexed_socket(clip.outputs, 0, label="alpha-clip output")
    output_links = _socket_links(clip_output)
    if len(output_links) != 1:
        raise RuntimeError("external alpha-clip output must feed exactly one Principled Alpha socket")
    output_link = output_links[0]
    shader = getattr(output_link, "to_node", None)
    alpha_socket = getattr(output_link, "to_socket", None)
    if (
        getattr(output_link, "from_node", None) != clip
        or getattr(output_link, "from_socket", None) != clip_output
        or getattr(shader, "type", None) != "BSDF_PRINCIPLED"
        or getattr(alpha_socket, "name", None) != "Alpha"
        or alpha_socket != _named_socket(shader.inputs, "Alpha", label="Principled Alpha")
        or _socket_links(alpha_socket) != (output_link,)
    ):
        raise RuntimeError("external alpha-clip output is not exclusively connected to Principled Alpha")
    _require_dithered_surface(material)


def _validate_opaque_alpha_graph(material: Any, semantic_nodes: Mapping[str, Any]) -> None:
    """Require an unlinked constant-one Alpha input for non-opacity materials."""

    base_links = _all_output_links(semantic_nodes["base_color"])
    if len(base_links) != 1:
        raise RuntimeError("external base color does not identify one Principled shader")
    shader = getattr(base_links[0], "to_node", None)
    if getattr(shader, "type", None) != "BSDF_PRINCIPLED":
        raise RuntimeError("external base color does not feed Principled BSDF")
    alpha_socket = _named_socket(shader.inputs, "Alpha", label="Principled Alpha")
    if _socket_links(alpha_socket):
        raise RuntimeError("external non-opacity material retains a linked Alpha socket")
    try:
        alpha_socket.default_value = 1.0
    except (AttributeError, TypeError, ValueError) as error:
        raise RuntimeError("external non-opacity material Alpha cannot be fixed to one") from error
    if float(getattr(alpha_socket, "default_value", math.nan)) != 1.0:
        raise RuntimeError("external non-opacity material Alpha differs from one")
    _require_dithered_surface(material)


def _source_custom_property_digest_value(value: Any) -> tuple[str, Any]:
    if isinstance(value, bool):
        return "boolean", value
    if isinstance(value, int):
        return "integer", value
    if isinstance(value, float):
        return "number", _receipt_float(value)
    if isinstance(value, str):
        return "string", value
    value_type_name = type(value).__name__
    if isinstance(value, Mapping) or value_type_name == "IDPropertyGroup":
        normalized: dict[str, Any] = {}
        try:
            keys = sorted(value.keys())
        except (AttributeError, RuntimeError, TypeError, ValueError) as error:
            raise RuntimeError("external source custom property mapping cannot be read") from error
        for key in keys:
            if type(key) is not str:
                raise RuntimeError("external source custom property mapping key is not a string")
            child_type, child = _source_custom_property_digest_value(value[key])
            normalized[key] = {"type": child_type, "value": child}
        return "mapping", normalized
    if (
        isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))
    ) or value_type_name == "IDPropertyArray":
        normalized_items = []
        for item in value:
            child_type, child = _source_custom_property_digest_value(item)
            normalized_items.append({"type": child_type, "value": child})
        return "array", normalized_items
    raise RuntimeError(
        f"external source custom property type cannot be normalized: {type(value).__name__}"
    )


def _remove_source_custom_properties(material: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for key in sorted(material.keys()):
        if type(key) is not str or not key or "\x00" in key:
            raise RuntimeError("external source custom property name is invalid")
        value_type, normalized = _source_custom_property_digest_value(material[key])
        result.append(
            {
                "property_name": key,
                "value_type": value_type,
                "value_sha256": _runtime_json_sha256(
                    {"type": value_type, "value": normalized}
                ),
                "reason": "receipt_bound_source_only_custom_property_removed",
            }
        )
        del material[key]
    if set(material.keys()):
        raise RuntimeError("external source custom properties could not be removed")
    return result


def _configure_external_material_alpha_contract(
    material: Any,
    asset: AcquiredAsset,
    semantic_nodes: Mapping[str, Any],
    *,
    material_identity_sha256: str,
) -> None:
    """Sanitize one verified source material and persist its export mapping."""

    existing = set(material.keys())
    if existing:
        raise RuntimeError(
            f"external source material custom properties are not in the closed contract: "
            f"{sorted(existing)}"
        )
    semantics = tuple(sorted(semantic_nodes))
    if not _REQUIRED_PBR.issubset(semantics):
        raise RuntimeError("external source material lacks the required active PBR semantics")
    if (
        type(material_identity_sha256) is not str
        or _SHA256.fullmatch(material_identity_sha256) is None
    ):
        raise RuntimeError("external material identity SHA-256 is invalid")
    opacity_node = semantic_nodes.get("opacity")
    if opacity_node is not None:
        direct_links = _all_output_links(opacity_node)
        if len(direct_links) != 1:
            raise RuntimeError("external opacity image lacks one direct Principled Alpha link")
        direct = direct_links[0]
        shader = getattr(direct, "to_node", None)
        alpha_socket = getattr(direct, "to_socket", None)
        if (
            getattr(shader, "type", None) != "BSDF_PRINCIPLED"
            or getattr(alpha_socket, "name", None) != "Alpha"
            or alpha_socket != _named_socket(shader.inputs, "Alpha", label="Principled Alpha")
        ):
            raise RuntimeError("external receipt opacity is not connected directly to Principled Alpha")
        tree = material.node_tree
        source_socket = getattr(direct, "from_socket", None)
        try:
            tree.links.remove(direct)
            clip = tree.nodes.new("ShaderNodeMath")
            clip.name = "VISTA_GLTF_MASK_0_5"
            clip.label = "VISTA glTF MASK cutoff 0.5"
            clip.operation = "GREATER_THAN"
            _indexed_socket(clip.inputs, 1, label="alpha-clip threshold").default_value = (
                EXTERNAL_MATERIAL_ALPHA_CUTOFF
            )
            tree.links.new(source_socket, _indexed_socket(clip.inputs, 0, label="alpha-clip input"))
            tree.links.new(_indexed_socket(clip.outputs, 0, label="alpha-clip output"), alpha_socket)
        except (AttributeError, RuntimeError, TypeError, ValueError) as error:
            raise RuntimeError("external material alpha-clip graph could not be constructed") from error
        _validate_masked_alpha_graph(material, opacity_node)
        alpha_mode = "MASK"
    else:
        _validate_opaque_alpha_graph(material, semantic_nodes)
        alpha_mode = "OPAQUE"
    try:
        material[EXTERNAL_MATERIAL_SOURCE_PROPERTY] = asset.logical_asset_id
        material[EXTERNAL_MATERIAL_SOURCE_DIGEST_PROPERTY] = asset.source_tree_sha256
        material[EXTERNAL_MATERIAL_SEMANTICS_PROPERTY] = json.dumps(
            semantics, separators=(",", ":")
        )
        material[EXTERNAL_MATERIAL_ALPHA_MODE_PROPERTY] = alpha_mode
        material[EXTERNAL_MATERIAL_ALPHA_POLICY_PROPERTY] = EXTERNAL_MATERIAL_ALPHA_SANITIZATION
        material[EXTERNAL_MATERIAL_IDENTITY_PROPERTY] = material_identity_sha256
        if alpha_mode == "MASK":
            material[EXTERNAL_MATERIAL_ALPHA_CUTOFF_PROPERTY] = EXTERNAL_MATERIAL_ALPHA_CUTOFF
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise RuntimeError("external material alpha provenance could not be persisted") from error
    expected_properties = EXTERNAL_MATERIAL_CONTRACT_PROPERTIES - (
        set() if alpha_mode == "MASK" else {EXTERNAL_MATERIAL_ALPHA_CUTOFF_PROPERTY}
    )
    if set(material.keys()) != expected_properties:
        raise RuntimeError("external material alpha provenance did not persist as a closed contract")


def _external_model_material_contract(
    material: Any,
    asset: AcquiredAsset,
    *,
    ordinal: int,
    source_material_name: str,
    semantic_nodes: Mapping[str, Any],
    inactive_image_normalizations: Sequence[Mapping[str, Any]],
    removed_source_custom_properties: Sequence[Mapping[str, Any]],
    material_identity_sha256: str,
) -> dict[str, Any]:
    """Seal the per-material inventory produced from receipt-validated nodes."""

    semantics = sorted(semantic_nodes)
    alpha_mode = "MASK" if "opacity" in semantics else "OPAQUE"
    expected_identity = external_material_identity_sha256(
        asset.logical_asset_id,
        asset.source_tree_sha256,
        ordinal,
        source_material_name,
        semantics,
    )
    expected_name = external_material_name(
        asset.logical_asset_id,
        ordinal,
        expected_identity,
    )
    if (
        material_identity_sha256 != expected_identity
        or material.name != expected_name
        or material.get(EXTERNAL_MATERIAL_SOURCE_PROPERTY) != asset.logical_asset_id
        or material.get(EXTERNAL_MATERIAL_SOURCE_DIGEST_PROPERTY) != asset.source_tree_sha256
        or material.get(EXTERNAL_MATERIAL_SEMANTICS_PROPERTY)
        != json.dumps(semantics, separators=(",", ":"))
        or material.get(EXTERNAL_MATERIAL_ALPHA_MODE_PROPERTY) != alpha_mode
        or material.get(EXTERNAL_MATERIAL_ALPHA_POLICY_PROPERTY)
        != EXTERNAL_MATERIAL_ALPHA_SANITIZATION
        or material.get(EXTERNAL_MATERIAL_IDENTITY_PROPERTY) != expected_identity
        or material.get(EXTERNAL_MATERIAL_ALPHA_CUTOFF_PROPERTY)
        != (EXTERNAL_MATERIAL_ALPHA_CUTOFF if alpha_mode == "MASK" else None)
    ):
        raise RuntimeError("external model material contract differs from realized Blender material")
    return {
        "schema_version": EXTERNAL_MODEL_MATERIAL_CONTRACT_SCHEMA,
        "material_id": expected_name,
        "source_logical_asset_id": asset.logical_asset_id,
        "source_tree_sha256": asset.source_tree_sha256,
        "source_material_name": source_material_name,
        "material_ordinal": ordinal,
        "material_identity_sha256": expected_identity,
        "active_texture_semantics": semantics,
        "inactive_image_normalizations": [
            dict(item) for item in inactive_image_normalizations
        ],
        "removed_source_custom_properties": [
            dict(item) for item in removed_source_custom_properties
        ],
        "alpha_mode": alpha_mode,
        "alpha_cutoff": EXTERNAL_MATERIAL_ALPHA_CUTOFF if alpha_mode == "MASK" else None,
        "sanitization_policy": EXTERNAL_MATERIAL_ALPHA_SANITIZATION,
    }


def _validate_receipt_image(
    bpy: Any,
    image: Any,
    expected_path: pathlib.Path,
    receipt_file: AcquiredFile,
    *,
    label: str,
    reload_image: bool,
) -> None:
    resolved = _resolved_runtime_image_path(bpy, image)
    if resolved != expected_path:
        raise RuntimeError(f"{label} references a texture outside its verified receipt: {resolved}")
    _verify_receipt_file_bytes(resolved, receipt_file, label=label)
    dimensions = tuple(int(value) for value in image.size)
    if reload_image or dimensions != receipt_file.dimensions_px:
        try:
            image.reload()
        except RuntimeError as error:
            raise RuntimeError(f"{label} could not reload its receipt-bound bytes") from error
    if tuple(int(value) for value in image.size) != receipt_file.dimensions_px:
        raise RuntimeError(f"{label} resolution differs from receipt: {receipt_file.relative_path}")
    reloaded_path = _resolved_runtime_image_path(bpy, image)
    if reloaded_path != resolved:
        raise RuntimeError(f"{label} path changed during reload: {receipt_file.relative_path}")
    _verify_receipt_file_bytes(reloaded_path, receipt_file, label=label)


def _load_fresh_receipt_image(
    bpy: Any,
    asset_set: ExternalAssetSet,
    asset: AcquiredAsset,
    semantic: str,
) -> Any:
    receipt_file = _texture_receipt_file(asset, semantic)
    path = _texture_file(asset_set, asset, semantic)
    def datablock_identity(value: Any) -> tuple[str, int]:
        as_pointer = getattr(value, "as_pointer", None)
        if callable(as_pointer):
            try:
                pointer = int(as_pointer())
            except (ReferenceError, RuntimeError, TypeError, ValueError, OverflowError):
                pointer = 0
            if pointer > 0:
                return "bpy", pointer
        return "python", id(value)

    existing = {datablock_identity(image) for image in bpy.data.images}
    image = bpy.data.images.load(str(path), check_existing=False)
    fresh = datablock_identity(image) not in existing
    if not fresh:
        raise RuntimeError(f"project-authored {semantic} image loader reused a stale datablock")
    try:
        _validate_receipt_image(
            bpy,
            image,
            path,
            receipt_file,
            label=f"project-authored {semantic} material texture",
            reload_image=True,
        )
        return image
    except BaseException:
        try:
            bpy.data.images.remove(image)
        except (ReferenceError, RuntimeError, TypeError):
            pass
        raise


def _validate_runtime_material_images(
    bpy: Any,
    meshes: Sequence[Any],
    asset_set: ExternalAssetSet,
    asset: AcquiredAsset,
) -> list[tuple[Any, dict[str, Any], list[dict[str, Any]]]]:
    expected = _runtime_receipt_texture_paths(asset_set, asset)
    materials: list[Any] = []
    for obj in meshes:
        for slot in obj.material_slots:
            if slot.material not in materials:
                materials.append(slot.material)
    used_semantics: set[str] = set()
    material_semantics: list[tuple[Any, dict[str, Any], list[dict[str, Any]]]] = []
    available_semantics = {semantic for receipt_file in expected.values() for semantic in receipt_file.semantic}
    for material in materials:
        if not material.use_nodes or material.node_tree is None:
            raise RuntimeError(f"external material is not node-based PBR: {material.name}")
        semantic_nodes = _active_surface_semantic_images(
            material,
            allow_inactive_receipt_images=True,
        )
        bound_images: dict[tuple[str, int], tuple[pathlib.Path, AcquiredFile]] = {}
        for node in material.node_tree.nodes:
            image = getattr(node, "image", None)
            if image is None:
                continue
            resolved = _resolved_runtime_image_path(bpy, image)
            receipt_file = expected.get(resolved)
            if receipt_file is None:
                raise RuntimeError(
                    f"external material references a texture outside its verified receipt: {resolved}"
                )
            _validate_receipt_image(
                bpy,
                image,
                resolved,
                receipt_file,
                label="external material image normalization input",
                reload_image=True,
            )
            bound_images[_runtime_identity(node)] = (resolved, receipt_file)
        inactive_nodes = _inactive_image_nodes(material, semantic_nodes)
        normalizations: list[dict[str, Any]] = []
        for node in inactive_nodes:
            bound = bound_images.get(_runtime_identity(node))
            if bound is None:
                raise RuntimeError(
                    "external inactive image cannot be normalized without an exact receipt path"
                )
            _resolved, receipt_file = bound
            normalizations.append(
                {
                    "node_name": str(node.name),
                    "image_name": str(
                        getattr(node.image, "name", receipt_file.relative_path)
                    ),
                    "relative_path": receipt_file.relative_path,
                    "sha256": receipt_file.sha256,
                    "reason": "inactive_disconnected_receipt_bound_image",
                }
            )
        for node in inactive_nodes:
            material.node_tree.nodes.remove(node)
        normalizations.sort(key=lambda row: (row["node_name"], row["relative_path"]))
        semantic_nodes = _active_surface_semantic_images(material)
        material_semantics.append((material, semantic_nodes, normalizations))
        for semantic, node in semantic_nodes.items():
            image = node.image
            resolved = _resolved_runtime_image_path(bpy, image)
            receipt_file = expected.get(resolved)
            if receipt_file is None:
                raise RuntimeError(
                    f"external material references a texture outside its verified receipt: {resolved}"
                )
            if semantic not in receipt_file.semantic:
                raise RuntimeError(
                    f"external material {semantic} socket uses receipt semantics "
                    f"{list(receipt_file.semantic)}: {receipt_file.relative_path}"
                )
            expected_colorspace = "sRGB" if semantic == "base_color" else "Non-Color"
            colorspace = getattr(getattr(image, "colorspace_settings", None), "name", None)
            if colorspace != expected_colorspace:
                raise RuntimeError(
                    f"external material {semantic} colorspace must be {expected_colorspace}: "
                    f"{receipt_file.relative_path}"
                )
            _validate_receipt_image(
                bpy,
                image,
                resolved,
                receipt_file,
                label=f"external material {semantic} texture",
                reload_image=True,
            )
            used_semantics.add(semantic)
    required_semantics = set(_REQUIRED_PBR) | (available_semantics & {"metalness", "opacity"})
    if not required_semantics.issubset(used_semantics):
        raise RuntimeError(
            f"external runtime materials do not use all receipt-bound PBR semantics: "
            f"{asset.logical_asset_id}: missing={sorted(required_semantics - used_semantics)}"
        )
    return material_semantics


def _matrix_is_identity(value: Any, tolerance: float = 1e-9) -> bool:
    try:
        rows = tuple(tuple(float(item) for item in row) for row in value)
    except (TypeError, ValueError):
        return False
    if len(rows) != 4 or any(len(row) != 4 for row in rows):
        return False
    return all(
        math.isfinite(rows[row][column])
        and abs(rows[row][column] - (1.0 if row == column else 0.0)) <= tolerance
        for row in range(4)
        for column in range(4)
    )


def _normalize_external_mesh(obj: Any, transform: Any, *, copy_data: bool = True) -> None:
    if copy_data:
        obj.data = obj.data.copy()
    obj.data.transform(transform)
    obj.parent = None
    if hasattr(obj, "parent_type"):
        obj.parent_type = "OBJECT"
    if hasattr(obj, "parent_bone"):
        obj.parent_bone = ""
    obj.matrix_parent_inverse.identity()
    obj.rotation_mode = "XYZ"
    obj.location = (0.0, 0.0, 0.0)
    obj.rotation_euler = (0.0, 0.0, 0.0)
    obj.scale = (1.0, 1.0, 1.0)
    obj.delta_location = (0.0, 0.0, 0.0)
    obj.delta_rotation_euler = (0.0, 0.0, 0.0)
    obj.delta_scale = (1.0, 1.0, 1.0)
    if hasattr(obj, "rotation_quaternion"):
        obj.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
    if hasattr(obj, "delta_rotation_quaternion"):
        obj.delta_rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
    if hasattr(obj, "rotation_axis_angle"):
        obj.rotation_axis_angle = (0.0, 0.0, 1.0, 0.0)
    obj.matrix_basis.identity()
    obj.matrix_world.identity()


def _validate_normalized_mesh_state(obj: Any) -> None:
    if getattr(obj, "type", "MESH") != "MESH":
        raise RuntimeError(f"external normalized output is not a mesh: {obj.name}")
    if getattr(obj, "parent", None) is not None:
        raise RuntimeError(f"external mesh retains a parent helper after normalization: {obj.name}")
    if _has_collection_items(getattr(obj, "modifiers", ())):
        raise RuntimeError(f"external mesh retains modifiers after staticization: {obj.name}")
    if _has_collection_items(getattr(obj, "constraints", ())):
        raise RuntimeError(f"external mesh retains constraints after staticization: {obj.name}")
    if getattr(obj, "animation_data", None) is not None:
        raise RuntimeError(f"external mesh retains object animation after staticization: {obj.name}")
    data = getattr(obj, "data", None)
    if data is None or getattr(data, "animation_data", None) is not None:
        raise RuntimeError(f"external mesh retains mesh animation after staticization: {obj.name}")
    if getattr(data, "shape_keys", None) is not None:
        raise RuntimeError(f"external mesh retains shape keys after staticization: {obj.name}")
    if getattr(obj, "rotation_mode", None) != "XYZ":
        raise RuntimeError(f"external mesh rotation mode changed after normalization: {obj.name}")
    if (
        not _identity_vector(obj.location, (0.0, 0.0, 0.0))
        or not _identity_vector(obj.rotation_euler, (0.0, 0.0, 0.0))
        or not _identity_vector(obj.scale, (1.0, 1.0, 1.0))
        or not _identity_vector(obj.delta_location, (0.0, 0.0, 0.0))
        or not _identity_vector(obj.delta_rotation_euler, (0.0, 0.0, 0.0))
        or not _identity_vector(obj.delta_scale, (1.0, 1.0, 1.0))
    ):
        raise RuntimeError(f"external mesh retains transform influence after normalization: {obj.name}")
    for label, matrix in (
        ("matrix_basis", obj.matrix_basis),
        ("matrix_local", obj.matrix_local),
        ("matrix_parent_inverse", obj.matrix_parent_inverse),
        ("matrix_world", obj.matrix_world),
    ):
        if not _matrix_is_identity(matrix):
            raise RuntimeError(
                f"external mesh retains {label} influence after normalization: {obj.name}"
            )


def _mesh_bounds_record(mesh: Any) -> dict[str, list[float]]:
    coordinates = [
        tuple(_receipt_float(value) for value in vertex.co)
        for vertex in mesh.vertices
    ]
    if not coordinates:
        raise RuntimeError("external staticized mesh has no vertices")
    minimum = [min(item[index] for item in coordinates) for index in range(3)]
    maximum = [max(item[index] for item in coordinates) for index in range(3)]
    dimensions = [
        _receipt_float(maximum[index] - minimum[index]) for index in range(3)
    ]
    return {
        "minimum": minimum,
        "maximum": maximum,
        "dimensions": dimensions,
    }


def _staticized_mesh_sha256(obj: Any) -> str:
    mesh = obj.data
    materials = [str(item.name) for item in mesh.materials]
    uv_layers = [str(item.name) for item in mesh.uv_layers]
    metadata = {
        "schema_version": "simworld.vista.playable-home-staticized-mesh-digest/v1",
        "topology": {
            "vertices": len(mesh.vertices),
            "edges": len(mesh.edges),
            "loops": len(mesh.loops),
            "polygons": len(mesh.polygons),
            "uv_layers": len(mesh.uv_layers),
        },
        "materials": materials,
        "uv_layer_names": uv_layers,
    }
    digest = hashlib.sha256()
    digest.update(_canonical_runtime_json(metadata))

    def update_floats(values: Sequence[Any]) -> None:
        numbers = [float(value) for value in values]
        if any(not math.isfinite(value) for value in numbers):
            raise RuntimeError("external staticized mesh contains non-finite geometry")
        digest.update(struct.pack(f"<{len(numbers)}d", *numbers))

    for vertex in mesh.vertices:
        update_floats(vertex.co)
    for edge in mesh.edges:
        digest.update(struct.pack("<2q", *(int(value) for value in edge.vertices)))
    for loop in mesh.loops:
        digest.update(struct.pack("<q", int(loop.vertex_index)))
    for polygon in mesh.polygons:
        digest.update(
            struct.pack(
                "<4q?",
                int(polygon.loop_start),
                int(polygon.loop_total),
                int(polygon.material_index),
                int(getattr(polygon, "index", 0)),
                bool(polygon.use_smooth),
            )
        )
    corner_normals = getattr(mesh, "corner_normals", ())
    digest.update(struct.pack("<q", len(corner_normals)))
    for normal in corner_normals:
        update_floats(normal.vector)
    for layer in mesh.uv_layers:
        digest.update(_canonical_runtime_json(str(layer.name)))
        for value in layer.data:
            update_floats(value.uv)
    return digest.hexdigest()


def _staticization_output_mesh_row(
    obj: Any,
    source_object_name: str,
) -> dict[str, Any]:
    _validate_normalized_mesh_state(obj)
    mesh = obj.data
    topology = {
        "vertices": len(mesh.vertices),
        "edges": len(mesh.edges),
        "loops": len(mesh.loops),
        "polygons": len(mesh.polygons),
        "uv_layers": len(mesh.uv_layers),
    }
    material_ids = [str(item.name) for item in mesh.materials]
    used_indices = {int(item.material_index) for item in mesh.polygons}
    if (
        not mesh.polygons
        or not material_ids
        or len(set(material_ids)) != len(material_ids)
        or used_indices != set(range(len(material_ids)))
    ):
        raise RuntimeError(
            f"external staticized mesh material topology is not compact: {obj.name}: "
            f"material_ids={material_ids}, used_indices={sorted(used_indices)}"
        )
    return {
        "source_object_name": source_object_name,
        "object_name": str(obj.name),
        "topology": topology,
        "bounds_m": _mesh_bounds_record(mesh),
        "material_ids": material_ids,
        "mesh_sha256": _staticized_mesh_sha256(obj),
        "stripped_state": dict(EXTERNAL_STATICIZATION_STRIPPED_STATE),
    }


def _bounds_record_from_minimum_maximum(
    minimum: Sequence[Any],
    maximum: Sequence[Any],
) -> dict[str, list[float]]:
    low = [_receipt_float(value) for value in minimum]
    high = [_receipt_float(value) for value in maximum]
    if len(low) != 3 or len(high) != 3 or any(
        high[index] < low[index] for index in range(3)
    ):
        raise RuntimeError("external staticization bounds are invalid")
    return {
        "minimum": low,
        "maximum": high,
        "dimensions": [
            _receipt_float(high[index] - low[index]) for index in range(3)
        ],
    }


def _staticization_output_digest_payload(
    source_logical_asset_id: str,
    source_tree_sha256: str,
    output_meshes: Sequence[Mapping[str, Any]],
    output_bounds_m: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "simworld.vista.playable-home-staticized-output/v1",
        "source_logical_asset_id": source_logical_asset_id,
        "source_tree_sha256": source_tree_sha256,
        "output_meshes": [dict(item) for item in output_meshes],
        "output_bounds_m": dict(output_bounds_m),
    }


def _external_staticization_receipt(
    bpy: Any,
    mathutils: Any,
    asset: AcquiredAsset,
    staticized: _StaticizedSource,
) -> dict[str, Any]:
    meshes = list(staticized.meshes)
    if len(meshes) != len(staticized.source_object_names):
        raise RuntimeError("external staticization output/source-name cardinality differs")
    output_meshes = [
        _staticization_output_mesh_row(obj, source_name)
        for obj, source_name in zip(meshes, staticized.source_object_names)
    ]
    minimum, maximum = _combined_bounds(mathutils, meshes)
    output_bounds = _bounds_record_from_minimum_maximum(minimum, maximum)
    output_digest = _runtime_json_sha256(
        _staticization_output_digest_payload(
            asset.logical_asset_id,
            asset.source_tree_sha256,
            output_meshes,
            output_bounds,
        )
    )
    body = {
        "schema_version": EXTERNAL_STATICIZATION_SCHEMA,
        "source_logical_asset_id": asset.logical_asset_id,
        "source_tree_sha256": asset.source_tree_sha256,
        "blender_version": list(bpy.app.version),
        "frame": EXTERNAL_STATICIZATION_FRAME,
        "depsgraph_mode": staticized.depsgraph_mode,
        "evaluation_policy": EXTERNAL_STATICIZATION_POLICY,
        "selection_policy": dict(staticized.selection_policy),
        "input_inventory": [dict(item) for item in staticized.input_inventory],
        "input_inventory_sha256": staticized.input_inventory_sha256,
        "input_actions": [dict(item) for item in staticized.input_actions],
        "exclusions": [dict(item) for item in staticized.exclusions],
        "output_meshes": output_meshes,
        "output_bounds_m": output_bounds,
        "output_digest": output_digest,
    }
    receipt = {**body, "content_digest": _runtime_json_sha256(body)}
    validate_external_staticization_receipt(receipt)
    return receipt


def _validate_staticization_bounds(value: Any, *, label: str) -> None:
    if not isinstance(value, Mapping) or set(value) != EXTERNAL_STATICIZATION_BOUNDS_KEYS:
        raise RuntimeError(f"{label} fields are not closed")
    vectors = []
    for key in ("minimum", "maximum", "dimensions"):
        vector = value.get(key)
        if (
            not isinstance(vector, list)
            or len(vector) != 3
            or any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in vector)
        ):
            raise RuntimeError(f"{label} vector is invalid")
        vectors.append([_receipt_float(item) for item in vector])
    minimum, maximum, dimensions = vectors
    if any(
        maximum[index] < minimum[index]
        or dimensions[index] != _receipt_float(maximum[index] - minimum[index])
        for index in range(3)
    ):
        raise RuntimeError(f"{label} dimensions differ from its bounds")


def validate_external_staticization_receipt(value: Any) -> dict[str, Any]:
    """Validate one closed, source-pinned evaluated-mesh receipt."""

    if not isinstance(value, Mapping) or set(value) != EXTERNAL_STATICIZATION_RECEIPT_KEYS:
        raise RuntimeError("external staticization receipt fields are not closed")
    receipt = dict(value)
    source_id = receipt.get("source_logical_asset_id")
    source_digest = receipt.get("source_tree_sha256")
    if (
        receipt.get("schema_version") != EXTERNAL_STATICIZATION_SCHEMA
        or type(source_id) is not str
        or _SAFE_ID.fullmatch(source_id) is None
        or type(source_digest) is not str
        or _SHA256.fullmatch(source_digest) is None
        or receipt.get("blender_version") != [4, 5, 8]
        or receipt.get("frame") != EXTERNAL_STATICIZATION_FRAME
        or receipt.get("depsgraph_mode") != EXTERNAL_STATICIZATION_DEPSGRAPH_MODE
        or receipt.get("evaluation_policy") != EXTERNAL_STATICIZATION_POLICY
    ):
        raise RuntimeError("external staticization receipt identity or evaluation policy differs")
    expected_selection = _external_source_selection_policy_for_identity(
        source_id,
        source_digest,
    )
    if receipt.get("selection_policy") != expected_selection:
        raise RuntimeError("external staticization exact selection policy differs")
    inventory = receipt.get("input_inventory")
    actions = receipt.get("input_actions")
    exclusions = receipt.get("exclusions")
    outputs = receipt.get("output_meshes")
    if (
        not isinstance(inventory, list)
        or not inventory
        or any(not isinstance(item, Mapping) for item in inventory)
        or not isinstance(actions, list)
        or any(not isinstance(item, Mapping) for item in actions)
        or not isinstance(exclusions, list)
        or any(not isinstance(item, Mapping) for item in exclusions)
        or not isinstance(outputs, list)
        or any(not isinstance(item, Mapping) for item in outputs)
    ):
        raise RuntimeError("external staticization receipt inventories are invalid")
    input_names: list[str] = []
    action_names = [item.get("name") for item in actions]
    if (
        action_names != sorted(set(action_names))
        or any(
            set(item) != {"name", "frame_range", "fcurve_count"}
            or type(item.get("name")) is not str
            or not item["name"]
            or not isinstance(item.get("frame_range"), list)
            or len(item["frame_range"]) != 2
            or any(
                isinstance(number, bool) or not isinstance(number, (int, float))
                for number in item["frame_range"]
            )
            or type(item.get("fcurve_count")) is not int
            or item["fcurve_count"] < 0
            for item in actions
        )
    ):
        raise RuntimeError("external staticization action inventory is not closed")
    for item in inventory:
        if set(item) != EXTERNAL_STATICIZATION_INPUT_OBJECT_KEYS:
            raise RuntimeError("external staticization input object fields are not closed")
        name = item.get("object_name")
        object_type = item.get("object_type")
        if (
            type(name) is not str
            or not name
            or object_type not in {"MESH", "CURVE", "ARMATURE", "EMPTY"}
            or not isinstance(item.get("matrix_world"), list)
            or len(item["matrix_world"]) != 16
            or any(
                isinstance(number, bool) or not isinstance(number, (int, float))
                for number in item["matrix_world"]
            )
            or not isinstance(item.get("material_slots"), list)
            or any(value is not None and type(value) is not str for value in item["material_slots"])
            or not isinstance(item.get("modifiers"), list)
            or not isinstance(item.get("constraints"), list)
            or item.get("action") not in ({None} | set(action_names))
        ):
            raise RuntimeError("external staticization input object inventory is invalid")
        input_names.append(name)
    if input_names != sorted(set(input_names)):
        raise RuntimeError("external staticization input object identities are not deterministic")
    expected_input_digest = _runtime_json_sha256(
        {"objects": inventory, "actions": actions}
    )
    if receipt.get("input_inventory_sha256") != expected_input_digest:
        raise RuntimeError("external staticization input inventory digest differs")
    selected = list(expected_selection["selected_object_names"])
    if not set(selected).issubset(input_names):
        raise RuntimeError("external staticization selected inputs are absent")
    exclusion_names = [item.get("object_name") for item in exclusions]
    if (
        exclusion_names != sorted(set(exclusion_names))
        or set(exclusion_names) != set(input_names) - set(selected)
        or any(
            set(item) != EXTERNAL_STATICIZATION_EXCLUSION_KEYS
            or type(item.get("reason")) is not str
            or not item["reason"]
            or type(item.get("evaluated_polygon_count")) is not int
            or item["evaluated_polygon_count"] < 0
            or not isinstance(item.get("used_materials"), list)
            or any(type(name) is not str or not name for name in item["used_materials"])
            for item in exclusions
        )
    ):
        raise RuntimeError("external staticization exclusion inventory differs from inputs")
    explicit = {
        item["object_name"]: item["reason"]
        for item in expected_selection["excluded_renderable_objects"]
    }
    if any(
        name in explicit and item["reason"] != explicit[name]
        for name, item in zip(exclusion_names, exclusions)
    ):
        raise RuntimeError("external staticization explicit renderable exclusions differ")
    output_names = [item.get("source_object_name") for item in outputs]
    if output_names != selected:
        raise RuntimeError("external staticization output selection differs")
    for index, item in enumerate(outputs):
        if set(item) != EXTERNAL_STATICIZATION_OUTPUT_MESH_KEYS:
            raise RuntimeError("external staticization output mesh fields are not closed")
        topology = item.get("topology")
        material_ids = item.get("material_ids")
        expected_name = f"VISTA_External_{_slug(source_id)}_{index:02d}"[:63]
        if (
            item.get("object_name") != expected_name
            or not isinstance(topology, Mapping)
            or set(topology) != EXTERNAL_STATICIZATION_TOPOLOGY_KEYS
            or any(type(topology.get(key)) is not int or topology[key] < 0 for key in topology)
            or topology["vertices"] == 0
            or topology["polygons"] == 0
            or not isinstance(material_ids, list)
            or not material_ids
            or material_ids != list(dict.fromkeys(material_ids))
            or any(type(name) is not str or not name for name in material_ids)
            or type(item.get("mesh_sha256")) is not str
            or _SHA256.fullmatch(item["mesh_sha256"]) is None
            or item.get("stripped_state") != EXTERNAL_STATICIZATION_STRIPPED_STATE
        ):
            raise RuntimeError("external staticization output mesh inventory is invalid")
        _validate_staticization_bounds(
            item.get("bounds_m"),
            label="external staticization output mesh bounds",
        )
    _validate_staticization_bounds(
        receipt.get("output_bounds_m"),
        label="external staticization combined bounds",
    )
    expected_output_digest = _runtime_json_sha256(
        _staticization_output_digest_payload(
            source_id,
            source_digest,
            outputs,
            receipt["output_bounds_m"],
        )
    )
    if receipt.get("output_digest") != expected_output_digest:
        raise RuntimeError("external staticization output digest differs")
    body = {key: receipt[key] for key in receipt if key != "content_digest"}
    if receipt.get("content_digest") != _runtime_json_sha256(body):
        raise RuntimeError("external staticization receipt content digest differs")
    return receipt


def external_staticization_ledger(
    receipts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    sources = [validate_external_staticization_receipt(item) for item in receipts]
    sources.sort(key=lambda item: item["source_logical_asset_id"])
    expected_sources = sorted(EXTERNAL_SOURCE_SELECTION_POLICIES)
    if [item["source_logical_asset_id"] for item in sources] != expected_sources:
        raise RuntimeError("external staticization ledger source inventory differs")
    body = {
        "schema_version": EXTERNAL_STATICIZATION_LEDGER_SCHEMA,
        "blender_version": [4, 5, 8],
        "sources": sources,
    }
    ledger = {**body, "content_digest": _runtime_json_sha256(body)}
    validate_external_staticization_ledger(ledger)
    return ledger


def validate_external_staticization_ledger(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != EXTERNAL_STATICIZATION_LEDGER_KEYS:
        raise RuntimeError("external staticization ledger fields are not closed")
    ledger = dict(value)
    sources = ledger.get("sources")
    if (
        ledger.get("schema_version") != EXTERNAL_STATICIZATION_LEDGER_SCHEMA
        or ledger.get("blender_version") != [4, 5, 8]
        or not isinstance(sources, list)
    ):
        raise RuntimeError("external staticization ledger identity is invalid")
    validated = [validate_external_staticization_receipt(item) for item in sources]
    expected_sources = sorted(EXTERNAL_SOURCE_SELECTION_POLICIES)
    if [item["source_logical_asset_id"] for item in validated] != expected_sources:
        raise RuntimeError("external staticization ledger source inventory differs")
    body = {key: ledger[key] for key in ledger if key != "content_digest"}
    if ledger.get("content_digest") != _runtime_json_sha256(body):
        raise RuntimeError("external staticization ledger content digest differs")
    return ledger


def _primary_receipt_file(asset: AcquiredAsset) -> AcquiredFile:
    source_root = pathlib.PurePosixPath(asset.source_relative_root)
    primary = pathlib.PurePosixPath(asset.primary_relative_path)
    try:
        relative = primary.relative_to(source_root).as_posix()
    except ValueError as error:
        raise RuntimeError(f"external primary file escapes its asset source root: {asset.logical_asset_id}") from error
    matches = [item for item in asset.files if item.relative_path == relative]
    if len(matches) != 1 or pathlib.PurePosixPath(relative).suffix.lower() != ".blend":
        raise RuntimeError(f"external primary .blend is absent or ambiguous: {asset.logical_asset_id}")
    return matches[0]


def _load_verified_blend_objects(
    bpy: Any,
    asset_set: ExternalAssetSet,
    asset: AcquiredAsset,
) -> list[Any]:
    """Load one staged .blend while pinning and rechecking its exact inode.

    The enclosing build consumes a private, content-verified source-tree
    snapshot. This descriptor/path seal is defense in depth against accidental
    mutation inside that private directory; the same OS user remains trusted
    because Unix permissions cannot stop that user from changing its own files.
    """

    receipt_file = _primary_receipt_file(asset)
    path = asset_set.source_path(asset.logical_asset_id)
    before_path = _verify_receipt_file_bytes(path, receipt_file, label="external primary .blend")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        file_descriptor = os.open(path, flags)
    except OSError as error:
        raise RuntimeError("cannot open external primary .blend descriptor") from error
    try:
        before_descriptor = _verify_receipt_descriptor(
            file_descriptor,
            receipt_file,
            label="external primary .blend",
        )
        if before_path != before_descriptor:
            raise RuntimeError("external primary .blend path and descriptor identify different files")
        try:
            with bpy.data.libraries.load(str(path), link=False) as (source, target):
                target.objects = list(source.objects)
        finally:
            after_path_value = asset_set.source_path(asset.logical_asset_id)
            after_path = _verify_receipt_file_bytes(
                after_path_value,
                receipt_file,
                label="external primary .blend",
            )
            after_descriptor = _verify_receipt_descriptor(
                file_descriptor,
                receipt_file,
                label="external primary .blend",
            )
            if (
                after_path_value != path
                or after_path != before_path
                or after_descriptor != before_descriptor
                or after_path != after_descriptor
            ):
                raise RuntimeError("external primary .blend changed across Blender library load")
    finally:
        os.close(file_descriptor)
    return [obj for obj in target.objects if obj is not None]


def _remove_loaded_source_state(
    bpy: Any,
    objects: Sequence[Any],
    actions: Sequence[Any],
    source_collection: Any,
) -> None:
    scene_children = bpy.context.scene.collection.children
    if source_collection.name not in scene_children:
        raise RuntimeError("external source dependency collection left the active scene unexpectedly")
    scene_children.unlink(source_collection)
    bpy.context.view_layer.update()
    removal_ids: list[Any] = [*objects, source_collection, *actions]
    for obj in objects:
        data = getattr(obj, "data", None)
        if data is not None and all(
            _runtime_identity(data) != _runtime_identity(item)
            for item in removal_ids
        ):
            removal_ids.append(data)
    bpy.data.batch_remove(ids=tuple(removal_ids))
    bpy.context.view_layer.update()
    # This function is called only after the staticizer has returned and its
    # evaluated depsgraph/RNA proxies have been released. The collection is
    # detached and flushed while all IDs remain alive, then its dependency IDs
    # are removed in one batch. Output meshes bind original writable materials,
    # never evaluated material proxies, so no source/helper IDs remain live.


def _evaluated_mesh_snapshot(
    bpy: Any,
    depsgraph: Any,
    source: Any,
) -> tuple[Any | None, list[Any], list[int]]:
    if source.type not in {"MESH", "CURVE"}:
        return None, [], []
    evaluated = source.evaluated_get(depsgraph)
    try:
        mesh = bpy.data.meshes.new_from_object(
            evaluated,
            preserve_all_data_layers=True,
            depsgraph=depsgraph,
        )
    except RuntimeError as error:
        raise RuntimeError(
            f"external source could not be evaluated as a static mesh: {source.name}"
        ) from error
    evaluated_slots = [
        slot.material for slot in getattr(evaluated, "material_slots", ())
    ]
    slots = [slot.material for slot in getattr(source, "material_slots", ())]
    if [getattr(item, "name", None) for item in evaluated_slots] != [
        getattr(item, "name", None) for item in slots
    ]:
        raise RuntimeError(
            f"external evaluated material slots differ from source slots: {source.name}"
        )
    used_indices = sorted({int(polygon.material_index) for polygon in mesh.polygons})
    return mesh, slots, used_indices


def _compact_evaluated_material_slots(
    mesh: Any,
    slot_materials: Sequence[Any],
    used_indices: Sequence[int],
    *,
    object_name: str,
) -> list[str]:
    if not mesh.polygons:
        return []
    polygon_material_indices = [int(polygon.material_index) for polygon in mesh.polygons]
    if (
        not used_indices
        or sorted(set(polygon_material_indices)) != list(used_indices)
        or any(index < 0 or index >= len(slot_materials) for index in used_indices)
        or any(slot_materials[index] is None for index in used_indices)
    ):
        return []
    index_map = {old: new for new, old in enumerate(used_indices)}
    mesh.materials.clear()
    names: list[str] = []
    for index in used_indices:
        material = slot_materials[index]
        _require_local_id(material, label=f"evaluated material {object_name}")
        mesh.materials.append(material)
        names.append(str(material.name))
    # Blender resets polygon material indices when the slot list is cleared,
    # so remap from the captured evaluated indices rather than reading the
    # post-clear values back from the mesh.
    for polygon, old_index in zip(mesh.polygons, polygon_material_indices):
        polygon.material_index = index_map[old_index]
    if len(mesh.materials) != len(names) or any(item is None for item in mesh.materials):
        raise RuntimeError(f"external evaluated mesh lost material bindings: {object_name}")
    return names


def _staticize_external_source(
    bpy: Any,
    asset: AcquiredAsset,
    loaded: Sequence[Any],
    new_actions: Sequence[Any],
    output_collection: Any,
) -> _StaticizedSource:
    """Bake one receipt-pinned source through a fixed depsgraph frame."""

    selection_policy = external_source_selection_policy(asset)
    selected_names = tuple(selection_policy["selected_object_names"])
    explicit_exclusions = {
        item["object_name"]: item["reason"]
        for item in selection_policy["excluded_renderable_objects"]
    }
    loaded_by_name = {str(obj.name): obj for obj in loaded}
    if len(loaded_by_name) != len(loaded):
        raise RuntimeError("external staticization loaded duplicate object names")
    required_names = set(selected_names) | set(explicit_exclusions)
    if not required_names.issubset(loaded_by_name):
        raise RuntimeError(
            f"external staticization exact-name policy differs from source: {asset.logical_asset_id}"
        )
    scene = bpy.context.scene
    scene.frame_start = EXTERNAL_STATICIZATION_FRAME
    scene.frame_end = EXTERNAL_STATICIZATION_FRAME
    scene.frame_set(EXTERNAL_STATICIZATION_FRAME)
    source_collection = bpy.data.collections.new(
        f"VISTA_Staticization_Source_{_slug(asset.logical_asset_id)}"[:63]
    )
    bpy.context.scene.collection.children.link(source_collection)
    created: list[Any] = []
    output_by_name: dict[str, Any] = {}
    exclusions: list[dict[str, Any]] = []
    depsgraph: Any | None = None
    try:
        for obj in loaded:
            _relink(obj, source_collection)
        bpy.context.view_layer.update()
        input_inventory = _staticization_input_inventory(loaded)
        action_inventory = _staticization_action_inventory(new_actions)
        input_inventory_sha256 = _runtime_json_sha256(
            {"objects": list(input_inventory), "actions": list(action_inventory)}
        )
        depsgraph = bpy.context.evaluated_depsgraph_get()
        depsgraph_mode = str(getattr(depsgraph, "mode", ""))
        if depsgraph_mode != EXTERNAL_STATICIZATION_DEPSGRAPH_MODE:
            raise RuntimeError(
                f"external staticization depsgraph mode differs: {depsgraph_mode!r}"
            )
        for source_name in sorted(loaded_by_name):
            source = loaded_by_name[source_name]
            mesh, slot_materials, used_indices = _evaluated_mesh_snapshot(
                bpy,
                depsgraph,
                source,
            )
            polygon_count = len(mesh.polygons) if mesh is not None else 0
            bound_materials: list[str] = []
            if mesh is not None and polygon_count:
                bound_materials = _compact_evaluated_material_slots(
                    mesh,
                    slot_materials,
                    used_indices,
                    object_name=source_name,
                )
            is_renderable = bool(
                source.type == "MESH"
                and not bool(source.hide_render)
                and polygon_count > 0
                and bound_materials
            )
            if source_name in selected_names:
                if not is_renderable or mesh is None:
                    if mesh is not None:
                        bpy.data.meshes.remove(mesh)
                    raise RuntimeError(
                        f"external selected object is not an evaluated material-bound mesh: {source_name}"
                    )
                output = bpy.data.objects.new(
                    f"VISTA_Staticized_{_slug(asset.logical_asset_id)}_{len(created):02d}"[:63],
                    mesh,
                )
                output_collection.objects.link(output)
                output.matrix_world = source.evaluated_get(depsgraph).matrix_world.copy()
                output.hide_render = False
                output["vista_staticization_source_object"] = source_name
                created.append(output)
                output_by_name[source_name] = output
                continue
            explicit_reason = explicit_exclusions.get(source_name)
            if explicit_reason is not None:
                if not is_renderable:
                    if mesh is not None:
                        bpy.data.meshes.remove(mesh)
                    raise RuntimeError(
                        f"external explicit renderable exclusion no longer matches source: {source_name}"
                    )
                reason = explicit_reason
            elif is_renderable:
                if mesh is not None:
                    bpy.data.meshes.remove(mesh)
                raise RuntimeError(
                    f"external receipt-pinned source has an unselected renderable object: {source_name}"
                )
            elif source.type not in {"MESH", "CURVE"}:
                reason = f"dependency_only_{source.type.lower()}"
            elif polygon_count == 0:
                reason = "zero_face_dependency"
            elif not bound_materials:
                reason = "materialless_dependency"
            elif bool(source.hide_render):
                reason = "hidden_render_dependency"
            else:
                reason = "non_renderable_dependency"
            exclusions.append(
                {
                    "object_name": source_name,
                    "reason": reason,
                    "evaluated_polygon_count": polygon_count,
                    "used_materials": bound_materials,
                }
            )
            if mesh is not None:
                bpy.data.meshes.remove(mesh)
        if set(output_by_name) != set(selected_names):
            raise RuntimeError("external staticization did not realize every exact selected object")
        ordered = tuple(output_by_name[name] for name in selected_names)
        depsgraph = None
        return _StaticizedSource(
            meshes=ordered,
            source_object_names=selected_names,
            selection_policy=selection_policy,
            input_inventory=input_inventory,
            input_inventory_sha256=input_inventory_sha256,
            input_actions=action_inventory,
            exclusions=tuple(sorted(exclusions, key=lambda row: row["object_name"])),
            depsgraph_mode=depsgraph_mode,
            source_collection=source_collection,
        )
    except BaseException:
        depsgraph = None
        # The process aborts this forge on any error. Do not free IDs while
        # exception frames may still retain evaluated RNA proxies.
        raise


def _append_static_blend(
    bpy: Any,
    mathutils: Any,
    asset_set: ExternalAssetSet,
    asset: AcquiredAsset,
    collection: Any,
) -> tuple[
    list[Any],
    tuple[float, float, float],
    list[dict[str, Any]],
    dict[str, Any],
]:
    logical_id = asset.logical_asset_id
    expected_dimensions_m = external_source_selected_dimensions_m(asset)
    before_actions = {_runtime_identity(item) for item in bpy.data.actions}
    loaded = _load_verified_blend_objects(bpy, asset_set, asset)
    new_actions = [
        item
        for item in bpy.data.actions
        if _runtime_identity(item) not in before_actions
    ]
    staticized = _staticize_external_source(
        bpy,
        asset,
        loaded,
        new_actions,
        collection,
    )
    # The staticizer must return before source IDs are removed: its evaluation
    # frame owns depsgraph/evaluated RNA proxies. Refresh once with sources
    # intact, then clean them from the outer lifetime.
    bpy.context.view_layer.update()
    _remove_loaded_source_state(
        bpy,
        loaded,
        new_actions,
        staticized.source_collection,
    )
    meshes = list(staticized.meshes)
    material_semantics = _validate_runtime_material_images(bpy, meshes, asset_set, asset)
    bpy.context.view_layer.update()
    unique_materials: list[Any] = []
    for obj in meshes:
        for slot in obj.material_slots:
            if slot.material not in unique_materials:
                unique_materials.append(slot.material)
    semantics_by_material = {
        _runtime_identity(material): semantics
        for material, semantics, _normalizations in material_semantics
    }
    normalizations_by_material = {
        _runtime_identity(material): normalizations
        for material, _semantics, normalizations in material_semantics
    }
    material_identities = {_runtime_identity(material) for material in unique_materials}
    if (
        set(semantics_by_material) != material_identities
        or set(normalizations_by_material) != material_identities
    ):
        raise RuntimeError("external material validation inventory differs from mesh material slots")
    realized_names: set[str] = set()
    material_contracts: list[dict[str, Any]] = []
    for ordinal, material in enumerate(unique_materials):
        original = material.name
        runtime_identity = _runtime_identity(material)
        semantics = sorted(semantics_by_material[runtime_identity])
        material_identity = external_material_identity_sha256(
            logical_id,
            asset.source_tree_sha256,
            ordinal,
            original,
            semantics,
        )
        expected_name = external_material_name(logical_id, ordinal, material_identity)
        material.name = expected_name
        if material.name != expected_name or material.name in realized_names:
            raise RuntimeError("external material name is not a unique deterministic source identity")
        realized_names.add(material.name)
        property_normalizations = _remove_source_custom_properties(material)
        _configure_external_material_alpha_contract(
            material,
            asset,
            semantics_by_material[runtime_identity],
            material_identity_sha256=material_identity,
        )
        material_contracts.append(
            _external_model_material_contract(
                material,
                asset,
                ordinal=ordinal,
                source_material_name=original,
                semantic_nodes=semantics_by_material[runtime_identity],
                inactive_image_normalizations=normalizations_by_material[runtime_identity],
                removed_source_custom_properties=property_normalizations,
                material_identity_sha256=material_identity,
            )
        )
    minimum, maximum = _combined_bounds(mathutils, meshes)
    measured = tuple(maximum[index] - minimum[index] for index in range(3))
    for actual, expected in zip(measured, expected_dimensions_m):
        if abs(actual - float(expected)) > max(0.025, float(expected) * 0.08):
            raise RuntimeError(
                f"measured external bounds differ from pinned provider envelope for {logical_id}: "
                f"measured={measured}, expected={tuple(expected_dimensions_m)}"
            )
    origin = mathutils.Vector(((minimum[0] + maximum[0]) / 2, (minimum[1] + maximum[1]) / 2, minimum[2]))
    for index, obj in enumerate(meshes):
        transform = mathutils.Matrix.Translation(-origin) @ obj.matrix_world.copy()
        _normalize_external_mesh(obj, transform, copy_data=False)
        _relink(obj, collection)
        obj.name = f"VISTA_External_{_slug(logical_id)}_{index:02d}"[:63]
    bpy.context.view_layer.update()
    for obj in meshes:
        _validate_normalized_mesh_state(obj)
    normalized_minimum, normalized_maximum = _combined_bounds(mathutils, meshes)
    normalized_dimensions = tuple(
        normalized_maximum[index] - normalized_minimum[index] for index in range(3)
    )
    if (
        abs((normalized_minimum[0] + normalized_maximum[0]) / 2) > 1e-5
        or abs((normalized_minimum[1] + normalized_maximum[1]) / 2) > 1e-5
        or abs(normalized_minimum[2]) > 1e-5
        or any(abs(normalized_dimensions[index] - measured[index]) > 1e-5 for index in range(3))
    ):
        raise RuntimeError(
            f"external source failed floor-center normalization: {logical_id}: "
            f"before_min={minimum}, before_max={maximum}, "
            f"after_min={normalized_minimum}, after_max={normalized_maximum}, "
            f"before_dimensions={measured}, after_dimensions={normalized_dimensions}"
        )
    staticization_receipt = _external_staticization_receipt(
        bpy,
        mathutils,
        asset,
        staticized,
    )
    return meshes, normalized_dimensions, material_contracts, staticization_receipt


def _detach_external_source_prototype(meshes: Sequence[Any]) -> None:
    for obj in meshes:
        for collection in tuple(obj.users_collection):
            collection.objects.unlink(obj)
        if tuple(obj.users_collection):
            raise RuntimeError("external source prototype could not be detached")


def _clone_external_source_prototype(
    prototype: _ExternalSourcePrototype,
    collection: Any,
    placement_id: str,
) -> list[Any]:
    """Instantiate normalized geometry while reusing verified materials."""

    placement_slug = _slug(placement_id)[:24] or "placement"
    placement_digest = hashlib.sha256(placement_id.encode("utf-8")).hexdigest()[:12]
    clones: list[Any] = []
    try:
        for index, source in enumerate(prototype.meshes):
            _validate_normalized_mesh_state(source)
            duplicate = source.copy()
            duplicate.data = source.data.copy()
            duplicate.parent = None
            if hasattr(duplicate, "parent_type"):
                duplicate.parent_type = "OBJECT"
            if hasattr(duplicate, "parent_bone"):
                duplicate.parent_bone = ""
            duplicate.rotation_mode = "XYZ"
            duplicate.location = (0.0, 0.0, 0.0)
            duplicate.rotation_euler = (0.0, 0.0, 0.0)
            duplicate.scale = (1.0, 1.0, 1.0)
            duplicate.delta_location = (0.0, 0.0, 0.0)
            duplicate.delta_rotation_euler = (0.0, 0.0, 0.0)
            duplicate.delta_scale = (1.0, 1.0, 1.0)
            if hasattr(duplicate, "rotation_quaternion"):
                duplicate.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
            if hasattr(duplicate, "delta_rotation_quaternion"):
                duplicate.delta_rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
            if hasattr(duplicate, "rotation_axis_angle"):
                duplicate.rotation_axis_angle = (0.0, 0.0, 1.0, 0.0)
            duplicate.matrix_parent_inverse.identity()
            duplicate.matrix_basis.identity()
            duplicate.matrix_world.identity()
            for key in tuple(duplicate.keys()):
                del duplicate[key]
            duplicate.name = (
                f"VISTA_External_{placement_slug}_{placement_digest}_{index:02d}"
            )[:63]
            collection.objects.link(duplicate)
            _validate_normalized_mesh_state(duplicate)
            clones.append(duplicate)
    except BaseException:
        for duplicate in reversed(clones):
            try:
                for current in tuple(duplicate.users_collection):
                    current.objects.unlink(duplicate)
            except (ReferenceError, RuntimeError, TypeError):
                pass
        raise
    return clones


def _dispose_external_source_prototypes(
    bpy: Any,
    registry: _ExternalSourceMaterialRegistry,
) -> None:
    for prototype in registry.values():
        for obj in prototype.meshes:
            mesh = getattr(obj, "data", None)
            try:
                bpy.data.objects.remove(obj, do_unlink=True)
            except (ReferenceError, RuntimeError, TypeError):
                continue
            if mesh is not None and getattr(mesh, "users", 1) == 0:
                try:
                    bpy.data.meshes.remove(mesh)
                except (ReferenceError, RuntimeError, TypeError):
                    pass


def realize_external_placements(
    bpy: Any,
    mathutils: Any,
    asset_set: ExternalAssetSet,
    external_plan: Any,
    *,
    room_roots: Mapping[str, Any],
    room_collections: Mapping[str, Any],
) -> tuple[dict[str, list[Any]], list[dict[str, Any]], dict[str, Any]]:
    """Realize verified placements and return material/staticization receipts."""

    required_authored_materials: set[str] = set()
    for placement in external_plan.placements:
        if placement.realization_mode != "project_authored":
            continue
        expected = AUTHORED_RECIPE_MATERIAL_IDS.get(placement.geometry_recipe)
        if expected is None or tuple(placement.material_logical_asset_ids) != expected:
            raise RuntimeError(
                f"project-authored placement material contract differs from its recipe: "
                f"{placement.placement_id}"
            )
        required_authored_materials.update(expected)
    materials_by_logical_id = {
        logical_id: _realize_pbr_material(bpy, asset_set, logical_id)
        for logical_id in sorted(required_authored_materials)
    }
    objects: dict[str, list[Any]] = {}
    used_authored_materials: set[str] = set()
    external_model_material_contracts: list[dict[str, Any]] = []
    staticization_receipts: list[dict[str, Any]] = []
    source_registry = _ExternalSourceMaterialRegistry()
    for placement in external_plan.placements:
        collection = room_collections[placement.room_id]
        actual_recipe_materials: tuple[str, ...] = ()
        if placement.realization_mode == "project_authored":
            meshes = _authored_recipe(
                bpy,
                collection,
                placement.geometry_recipe,
                placement.source_dimensions_m,
                materials_by_logical_id,
            )
            actual_recipe_materials = _validate_authored_recipe_material_use(
                placement.geometry_recipe,
                meshes,
                materials_by_logical_id,
            )
            used_authored_materials.update(actual_recipe_materials)
            measured = _combined_bounds(mathutils, meshes)
            measured_dimensions = tuple(measured[1][index] - measured[0][index] for index in range(3))
        else:
            asset = asset_set.asset(placement.source_logical_asset_id)
            external_source_selected_dimensions_m(asset)
            prototype = source_registry.get(asset)
            if prototype is None:
                (
                    prototype_meshes,
                    measured_dimensions,
                    source_material_contracts,
                    staticization_receipt,
                ) = (
                    _append_static_blend(
                        bpy,
                        mathutils,
                        asset_set,
                        asset,
                        collection,
                    )
                )
                staticization_receipts.append(staticization_receipt)
                _detach_external_source_prototype(prototype_meshes)
                prototype = source_registry.add(
                    asset,
                    prototype_meshes,
                    measured_dimensions,
                    source_material_contracts,
                )
                external_model_material_contracts.extend(source_material_contracts)
            measured_dimensions = prototype.normalized_dimensions_m
            meshes = _clone_external_source_prototype(
                prototype,
                collection,
                placement.placement_id,
            )
        scaled_dimensions = tuple(value * placement.uniform_scale for value in measured_dimensions)
        planned = placement.source_dimensions_m
        if any(abs(scaled_dimensions[index] - planned[index]) > max(0.03, planned[index] * 0.08) for index in range(3)):
            raise RuntimeError(
                f"measured normalized placement bounds differ from plan: {placement.placement_id}"
            )
        for obj in meshes:
            obj.parent = room_roots[placement.room_id]
            obj.matrix_parent_inverse.identity()
            obj.location = placement.location_m
            obj.rotation_euler = tuple(math.radians(value) for value in placement.rotation_deg)
            obj.scale = (placement.uniform_scale,) * 3 if placement.realization_mode != "project_authored" else (1.0, 1.0, 1.0)
            obj["vista_external_placement_id"] = placement.placement_id
            obj["vista_semantic_target_id"] = placement.semantic_target_id or ""
            obj["vista_dressing_id"] = placement.placement_id if placement.placement_kind == "dressing" else ""
            obj["vista_source_logical_asset_id"] = placement.source_logical_asset_id or "project_authored"
            obj["vista_source_tree_sha256"] = placement.source_tree_sha256 or ""
            obj["vista_measured_normalized_dimensions_m"] = list(measured_dimensions)
            obj["vista_normalization_policy"] = "measured_combined_bounds_floor_center_uniform_scale_v1"
            obj["vista_material_logical_asset_ids_json"] = json.dumps(
                actual_recipe_materials,
                separators=(",", ":"),
            )
            obj["vista_collision_policy"] = "presentation_no_collision"
            obj["vista_unreal_collision_profile"] = "NoCollision"
        objects[placement.placement_id] = meshes
    _dispose_external_source_prototypes(bpy, source_registry)
    if used_authored_materials != required_authored_materials:
        raise RuntimeError(
            "project-authored material provenance differs from realized recipe use: "
            f"actual={sorted(used_authored_materials)}, expected={sorted(required_authored_materials)}"
        )
    material_receipts = [
        _external_texture_material_contract(
            materials_by_logical_id[logical_id],
            asset_set.asset(logical_id),
        )
        for logical_id in sorted(used_authored_materials)
    ]
    if len({item["material_id"] for item in external_model_material_contracts}) != len(
        external_model_material_contracts
    ):
        raise RuntimeError("external model material contract identities are duplicated")
    material_receipts.extend(
        sorted(external_model_material_contracts, key=lambda item: item["material_id"])
    )
    return objects, material_receipts, external_staticization_ledger(
        staticization_receipts
    )
