"""Closed Poly Haven receipt validation and Blender realization for forge v2.

The pure receipt path is intentionally usable without :mod:`bpy`.  Runtime
realization is duck-typed and only called by ``build.py`` inside the pinned
Blender process.  Absolute acquisition paths never enter persistent receipts.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import pathlib
import re
import struct
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .config import ForgeInputError, sha256_file


ACQUISITION_RECEIPT_FILENAME = "acquisition-receipt.json"
ACQUISITION_RECEIPT_SCHEMA = "simworld.vista.playable-home-poly-haven-receipt/v1"
PROVIDER = "poly_haven"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_MD5 = re.compile(r"^[0-9a-f]{32}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
_REQUIRED_PBR = frozenset({"base_color", "normal", "roughness"})


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


def _texture_file(asset_set: ExternalAssetSet, asset: AcquiredAsset, semantic: str) -> pathlib.Path:
    matches = [item for item in asset.files if semantic in item.semantic]
    if not matches:
        raise RuntimeError(f"verified asset lost {semantic} texture: {asset.logical_asset_id}")
    return _safe_existing_path(
        asset_set.root,
        f"{asset.source_relative_root}/{matches[0].relative_path}",
        file_required=True,
    )


def _realize_pbr_material(bpy: Any, asset_set: ExternalAssetSet, logical_id: str) -> Any:
    asset = asset_set.asset(logical_id)
    if asset.asset_type != "texture":
        raise RuntimeError(f"project-authored material source is not a texture: {logical_id}")
    material = bpy.data.materials.new(name=f"r2.external.{_slug(logical_id)}")
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
        path = _texture_file(asset_set, asset, semantic)
        image = bpy.data.images.load(str(path), check_existing=True)
        image.colorspace_settings.name = colorspace
        texture = nodes.new("ShaderNodeTexImage")
        texture.image = image
        material.node_tree.links.new(texture.outputs["Color"], shader.inputs[input_name])
    normal_path = _texture_file(asset_set, asset, "normal")
    image = bpy.data.images.load(str(normal_path), check_existing=True)
    image.colorspace_settings.name = "Non-Color"
    texture = nodes.new("ShaderNodeTexImage")
    texture.image = image
    normal = nodes.new("ShaderNodeNormalMap")
    normal.inputs["Strength"].default_value = 0.65
    material.node_tree.links.new(texture.outputs["Color"], normal.inputs["Color"])
    material.node_tree.links.new(normal.outputs["Normal"], shader.inputs["Normal"])
    material["vista_external_material_source"] = logical_id
    material["vista_source_tree_sha256"] = asset.source_tree_sha256
    return material


def _relink(obj: Any, collection: Any) -> None:
    for current in tuple(obj.users_collection):
        current.objects.unlink(obj)
    collection.objects.link(obj)


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
    obj.data.materials.append(material)
    return obj


def _authored_recipe(
    bpy: Any,
    collection: Any,
    recipe: str,
    dimensions: Sequence[float],
    oak: Any,
    wool: Any,
) -> list[Any]:
    x, y, z = (float(value) for value in dimensions)
    parts: list[Any] = []
    add = lambda suffix, center, dims, mat: parts.append(
        _cube_part(bpy, collection, f"VISTA_External_{recipe}_{suffix}", center, dims, mat)
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


def _combined_bounds(mathutils: Any, objects: Sequence[Any]) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    points = [obj.matrix_world @ mathutils.Vector(corner) for obj in objects for corner in obj.bound_box]
    if not points:
        raise RuntimeError("external source contains no measurable mesh bounds")
    minimum = tuple(min(float(point[index]) for point in points) for index in range(3))
    maximum = tuple(max(float(point[index]) for point in points) for index in range(3))
    if any(not math.isfinite(value) for value in (*minimum, *maximum)) or any(
        maximum[index] <= minimum[index] for index in range(3)
    ):
        raise RuntimeError("external source has invalid measured bounds")
    return minimum, maximum


def _validate_static_source(bpy: Any, objects: Sequence[Any], new_actions: Sequence[Any]) -> list[Any]:
    forbidden = [obj for obj in objects if obj.type in {"ARMATURE", "CAMERA", "LIGHT"}]
    if forbidden:
        raise RuntimeError(f"external source contains forbidden object types: {[obj.type for obj in forbidden]}")
    if new_actions or any(getattr(obj, "animation_data", None) is not None for obj in objects):
        raise RuntimeError("external source contains animations")
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


def _validate_runtime_material_images(
    bpy: Any,
    meshes: Sequence[Any],
    asset_set: ExternalAssetSet,
    asset: AcquiredAsset,
) -> None:
    expected = {
        pathlib.PurePosixPath(item.relative_path).name: item
        for item in asset.files
        if item.dimensions_px is not None
    }
    materials: list[Any] = []
    for obj in meshes:
        for slot in obj.material_slots:
            if slot.material not in materials:
                materials.append(slot.material)
    for material in materials:
        if not material.use_nodes or material.node_tree is None:
            raise RuntimeError(f"external material is not node-based PBR: {material.name}")
        image_nodes = [
            node
            for node in material.node_tree.nodes
            if node.type == "TEX_IMAGE" and node.image is not None
        ]
        if not image_nodes:
            raise RuntimeError(f"external material has no image-backed surface input: {material.name}")
        for node in image_nodes:
            image = node.image
            basename = pathlib.PurePosixPath(str(image.filepath).replace("\\", "/")).name
            receipt_file = expected.get(basename)
            if receipt_file is None:
                raise RuntimeError(
                    f"external material references a texture outside its verified receipt: {basename}"
                )
            if tuple(int(value) for value in image.size) != receipt_file.dimensions_px:
                try:
                    image.reload()
                except RuntimeError:
                    pass
            if tuple(int(value) for value in image.size) != receipt_file.dimensions_px:
                raise RuntimeError(
                    f"external material texture resolution differs from receipt: {basename}"
                )
            _safe_existing_path(
                asset_set.root,
                f"{asset.source_relative_root}/{receipt_file.relative_path}",
                file_required=True,
            )


def _append_static_blend(
    bpy: Any,
    mathutils: Any,
    asset_set: ExternalAssetSet,
    asset: AcquiredAsset,
    collection: Any,
) -> tuple[list[Any], tuple[float, float, float]]:
    path = asset_set.source_path(asset.logical_asset_id)
    logical_id = asset.logical_asset_id
    expected_dimensions_m = asset.catalog_dimensions_m
    if expected_dimensions_m is None:
        raise RuntimeError(f"external model lacks a pinned measurement: {logical_id}")
    before_actions = set(bpy.data.actions)
    with bpy.data.libraries.load(str(path), link=False) as (source, target):
        target.objects = list(source.objects)
    loaded = [obj for obj in target.objects if obj is not None]
    meshes = _validate_static_source(bpy, loaded, [item for item in bpy.data.actions if item not in before_actions])
    _validate_runtime_material_images(bpy, meshes, asset_set, asset)
    bpy.context.view_layer.update()
    unique_materials: list[Any] = []
    for obj in meshes:
        for slot in obj.material_slots:
            if slot.material not in unique_materials:
                unique_materials.append(slot.material)
    for ordinal, material in enumerate(unique_materials):
        original = material.name
        material.name = f"r2.external.{_slug(logical_id)}.{ordinal:02d}.{_slug(original)}"[:63]
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
        obj.data = obj.data.copy()
        obj.data.transform(mathutils.Matrix.Translation(-origin) @ obj.matrix_world)
        obj.parent = None
        obj.matrix_world.identity()
        _relink(obj, collection)
        obj.name = f"VISTA_External_{_slug(logical_id)}_{index:02d}"[:63]
    for obj in loaded:
        if obj not in meshes:
            bpy.data.objects.remove(obj, do_unlink=True)
    bpy.context.view_layer.update()
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
        raise RuntimeError(f"external source failed floor-center normalization: {logical_id}")
    return meshes, normalized_dimensions


def realize_external_placements(
    bpy: Any,
    mathutils: Any,
    asset_set: ExternalAssetSet,
    external_plan: Any,
    *,
    room_roots: Mapping[str, Any],
    room_collections: Mapping[str, Any],
) -> tuple[dict[str, list[Any]], list[dict[str, Any]]]:
    """Realize verified placements; return meshes and material provenance."""

    oak = _realize_pbr_material(bpy, asset_set, "visual.material.white_oak_veneer")
    wool = _realize_pbr_material(bpy, asset_set, "visual.material.poly_wool_herringbone")
    objects: dict[str, list[Any]] = {}
    material_receipts = [
        {"material_id": material.name, "source": logical_id, "pbr_source": asset_digest_record(asset_set.asset(logical_id))}
        for logical_id, material in (
            ("visual.material.white_oak_veneer", oak),
            ("visual.material.poly_wool_herringbone", wool),
        )
    ]
    for placement in external_plan.placements:
        collection = room_collections[placement.room_id]
        if placement.realization_mode == "project_authored":
            meshes = _authored_recipe(
                bpy,
                collection,
                placement.geometry_recipe,
                placement.source_dimensions_m,
                oak,
                wool,
            )
            measured = _combined_bounds(mathutils, meshes)
            measured_dimensions = tuple(measured[1][index] - measured[0][index] for index in range(3))
        else:
            asset = asset_set.asset(placement.source_logical_asset_id)
            if asset.catalog_dimensions_m is None:
                raise RuntimeError(f"external model lacks a pinned measurement: {asset.logical_asset_id}")
            meshes, measured_dimensions = _append_static_blend(
                bpy,
                mathutils,
                asset_set,
                asset,
                collection,
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
            obj["vista_collision_policy"] = "presentation_no_collision"
            obj["vista_unreal_collision_profile"] = "NoCollision"
        objects[placement.placement_id] = meshes
    return objects, material_receipts
