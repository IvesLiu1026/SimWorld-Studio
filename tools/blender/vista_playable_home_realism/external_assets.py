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

from .config import ForgeInputError, sha256_file


ACQUISITION_RECEIPT_FILENAME = "acquisition-receipt.json"
ACQUISITION_RECEIPT_SCHEMA = "simworld.vista.playable-home-poly-haven-receipt/v1"
PROVIDER = "poly_haven"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_MD5 = re.compile(r"^[0-9a-f]{32}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
_REQUIRED_PBR = frozenset({"base_color", "normal", "roughness"})
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
    material = bpy.data.materials.new(name=f"r2.external.{_slug(logical_id)}")
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
        material["vista_external_material_source"] = logical_id
        material["vista_source_tree_sha256"] = asset.source_tree_sha256
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
            logical_id = material.get("vista_external_material_source")
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
                if slot.material is not None and slot.material.get("vista_external_material_source") == logical_id:
                    if slot.material is not expected_material:
                        raise RuntimeError(
                            f"project-authored material provenance points at the wrong datablock: {logical_id}"
                        )
    return tuple(sorted(actual))


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


def _has_collection_items(value: Any) -> bool:
    if value is None:
        return False
    try:
        return len(value) > 0
    except (TypeError, AttributeError):
        return bool(tuple(value))


def _node_trees(material: Any) -> tuple[Any, ...]:
    pending = [material.node_tree]
    result: list[Any] = []
    seen: set[int] = set()
    while pending:
        tree = pending.pop()
        identity = id(tree)
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
    seen: set[int] = set()
    while pending:
        node = pending.pop()
        identity = id(node)
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


def _active_surface_semantic_images(material: Any) -> dict[str, Any]:
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
    if len({id(node) for node in semantic_nodes.values()}) != len(semantic_nodes):
        raise RuntimeError("external material reuses one image node for ambiguous PBR semantics")
    reachable = _reachable_upstream_nodes(shader)
    reachable_images = {
        id(node): node for node in reachable if getattr(node, "image", None) is not None
    }
    all_images = {id(node): node for node in nodes if getattr(node, "image", None) is not None}
    disconnected = sorted(
        getattr(node, "name", "<unnamed>")
        for identity, node in all_images.items()
        if identity not in reachable_images
    )
    if disconnected:
        raise RuntimeError(f"external material contains disconnected image impostors: {disconnected}")
    mapped = {id(node) for node in semantic_nodes.values()}
    unexpected = sorted(
        getattr(node, "name", "<unnamed>")
        for identity, node in reachable_images.items()
        if identity not in mapped
    )
    if unexpected:
        raise RuntimeError(f"external material routes images through unsupported sockets: {unexpected}")
    return semantic_nodes


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
) -> None:
    expected = _runtime_receipt_texture_paths(asset_set, asset)
    materials: list[Any] = []
    for obj in meshes:
        for slot in obj.material_slots:
            if slot.material not in materials:
                materials.append(slot.material)
    used_semantics: set[str] = set()
    available_semantics = {semantic for receipt_file in expected.values() for semantic in receipt_file.semantic}
    for material in materials:
        if not material.use_nodes or material.node_tree is None:
            raise RuntimeError(f"external material is not node-based PBR: {material.name}")
        semantic_nodes = _active_surface_semantic_images(material)
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


def _normalize_external_mesh(obj: Any, transform: Any) -> None:
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
    if getattr(obj, "parent", None) is not None:
        raise RuntimeError(f"external mesh retains a parent helper after normalization: {obj.name}")
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


def _append_static_blend(
    bpy: Any,
    mathutils: Any,
    asset_set: ExternalAssetSet,
    asset: AcquiredAsset,
    collection: Any,
) -> tuple[list[Any], tuple[float, float, float]]:
    logical_id = asset.logical_asset_id
    expected_dimensions_m = asset.catalog_dimensions_m
    if expected_dimensions_m is None:
        raise RuntimeError(f"external model lacks a pinned measurement: {logical_id}")
    before_actions = set(bpy.data.actions)
    loaded = _load_verified_blend_objects(bpy, asset_set, asset)
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
        transform = mathutils.Matrix.Translation(-origin) @ obj.matrix_world.copy()
        _normalize_external_mesh(obj, transform)
        _relink(obj, collection)
        obj.name = f"VISTA_External_{_slug(logical_id)}_{index:02d}"[:63]
    for obj in loaded:
        if obj not in meshes:
            bpy.data.objects.remove(obj, do_unlink=True)
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
            obj["vista_material_logical_asset_ids_json"] = json.dumps(
                actual_recipe_materials,
                separators=(",", ":"),
            )
            obj["vista_collision_policy"] = "presentation_no_collision"
            obj["vista_unreal_collision_profile"] = "NoCollision"
        objects[placement.placement_id] = meshes
    if used_authored_materials != required_authored_materials:
        raise RuntimeError(
            "project-authored material provenance differs from realized recipe use: "
            f"actual={sorted(used_authored_materials)}, expected={sorted(required_authored_materials)}"
        )
    material_receipts = [
        {
            "material_id": materials_by_logical_id[logical_id].name,
            "source": logical_id,
            "pbr_source": asset_digest_record(asset_set.asset(logical_id)),
        }
        for logical_id in sorted(used_authored_materials)
    ]
    return objects, material_receipts
