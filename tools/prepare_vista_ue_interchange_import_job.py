#!/usr/bin/env python3
"""Prepare a deterministic, offline UE 5.3 Interchange import job."""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import errno
import hashlib
import json
import math
import os
import re
import shutil
import stat
import sys
import tempfile
import urllib.parse
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

import fetch_vista_bootstrap_assets as acquisition


JOB_SCHEMA = "simworld-ue-interchange-import-job/v1"
RESULT_SCHEMA = "simworld-ue-interchange-import-job-result/v1"
RECEIPT_SCHEMA = "simworld-ue-interchange-import-job-preparation-receipt/v1"
ENGINE_VERSION = "5.3.2"
DESTINATION_ROOT = "/Game/VISTA/External/PolyHaven"
MAX_GLTF_BYTES = 16 * 1024 * 1024
MAX_GLTF_ITEMS = 100_000
APPROVAL_RE = re.compile(r"^(?:APPROVAL|CHANGE|TICKET|VISTA)-[A-Za-z0-9][A-Za-z0-9._-]{2,95}$")
OUTPUT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
UE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class ImportJobError(RuntimeError):
    def __init__(self, code: str, message: str, *, pointer: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.pointer = pointer

    def public_dict(self) -> dict[str, str]:
        value = {"code": self.code, "message": self.message}
        if self.pointer:
            value["pointer"] = self.pointer
        return value


def fail(code: str, message: str, *, pointer: str | None = None) -> None:
    raise ImportJobError(code, message, pointer=pointer)


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _strict_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            fail("UE_IMPORT_GLTF_DUPLICATE_KEY", "glTF JSON contains a duplicate object key", pointer=key)
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    fail("UE_IMPORT_GLTF_JSON_INVALID", "glTF JSON contains a non-finite number", pointer=value)


def strict_json(raw: bytes, *, pointer: str) -> Any:
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_strict_pairs,
            parse_constant=_reject_json_constant,
        )
    except ImportJobError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        fail("UE_IMPORT_GLTF_JSON_INVALID", "glTF must be bounded UTF-8 JSON", pointer=pointer)


def _secure_read_pinned(root: Path, entry: Mapping[str, Any]) -> bytes:
    relative = str(entry["path"])
    path = root.joinpath(*PurePosixPath(relative).parts)
    try:
        acquisition._reject_symlink_components(path, relative)
    except acquisition.AcquisitionError:
        fail("UE_IMPORT_SOURCE_UNSAFE", "Pinned source path contains an unsafe or missing component", pointer=relative)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        fail("UE_IMPORT_SOURCE_UNSAFE", "Pinned source file cannot be opened safely", pointer=relative)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size != int(entry["bytes"])
        ):
            fail("UE_IMPORT_SOURCE_UNSAFE", "Pinned source file metadata is unsafe", pointer=relative)
        if before.st_size > MAX_GLTF_BYTES:
            fail("UE_IMPORT_GLTF_TOO_LARGE", "glTF JSON exceeds the preparation limit", pointer=relative)
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    raw = b"".join(chunks)
    if (
        remaining
        or after.st_dev != before.st_dev
        or after.st_ino != before.st_ino
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
        or hashlib.sha256(raw).hexdigest() != str(entry["sha256"])
    ):
        fail("UE_IMPORT_SOURCE_CHANGED", "Pinned source file changed during preparation", pointer=relative)
    return raw


def _list(value: Any, pointer: str, *, required: bool = True) -> list[Any]:
    if value is None and not required:
        return []
    if not isinstance(value, list) or (required and not value) or len(value) > MAX_GLTF_ITEMS:
        fail("UE_IMPORT_GLTF_SCHEMA_INVALID", "Expected a bounded JSON array", pointer=pointer)
    return value


def _object(value: Any, pointer: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        fail("UE_IMPORT_GLTF_SCHEMA_INVALID", "Expected a JSON object", pointer=pointer)
    return value


def _index(value: Any, count: int, pointer: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value >= count:
        fail("UE_IMPORT_GLTF_SCHEMA_INVALID", "glTF index is outside its target array", pointer=pointer)
    return value


def _positive_integer(value: Any, pointer: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        fail("UE_IMPORT_GLTF_SCHEMA_INVALID", "Expected a positive integer", pointer=pointer)
    return value


def _finite_vec3(value: Any, pointer: str) -> list[float | int]:
    if not isinstance(value, list) or len(value) != 3:
        fail("UE_IMPORT_GLTF_BOUNDS_MISSING", "POSITION bounds must contain three values", pointer=pointer)
    result: list[float | int] = []
    for component in value:
        if isinstance(component, bool) or not isinstance(component, (int, float)) or not math.isfinite(component):
            fail("UE_IMPORT_GLTF_BOUNDS_INVALID", "POSITION bounds must be finite numbers", pointer=pointer)
        number: float | int = 0 if component == 0 else component
        if abs(float(number)) > 1_000_000_000:
            fail("UE_IMPORT_GLTF_BOUNDS_INVALID", "POSITION bounds exceed the preparation limit", pointer=pointer)
        result.append(number)
    return result


def _safe_dependency_uri(uri: Any, pointer: str, *, gltf_path: str, asset_dir: str) -> str:
    if not isinstance(uri, str) or not uri or len(uri) > 1024 or any(ord(char) < 32 or ord(char) == 127 for char in uri):
        fail("UE_IMPORT_GLTF_URI_INVALID", "External dependency URI must be a bounded string", pointer=pointer)
    parsed = urllib.parse.urlsplit(uri)
    if (
        uri.startswith("/")
        or "%" in uri
        or "\\" in uri
        or parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
    ):
        fail("UE_IMPORT_GLTF_URI_INVALID", "Only literal relative POSIX dependency URIs are allowed", pointer=pointer)
    pure = PurePosixPath(uri)
    if pure.as_posix() != uri or any(part in {"", ".", ".."} for part in pure.parts):
        fail("UE_IMPORT_GLTF_URI_INVALID", "Dependency URI aliases or traverses its asset root", pointer=pointer)
    gltf_parent = PurePosixPath(gltf_path).parent
    resolved = gltf_parent.joinpath(*pure.parts).as_posix()
    if PurePosixPath(resolved).parts[0] != asset_dir:
        fail("UE_IMPORT_GLTF_URI_INVALID", "Dependency URI escapes its asset root", pointer=pointer)
    return resolved


def _ue_safe_id(source_asset_id: str) -> str:
    candidate = re.sub(r"[^a-z0-9]+", "_", source_asset_id.lower()).strip("_")
    if not candidate or candidate[0].isdigit():
        candidate = "asset_" + candidate
    if not UE_ID_RE.fullmatch(candidate):
        fail("UE_IMPORT_DESTINATION_INVALID", "Source asset ID cannot map to a safe UE destination folder")
    return candidate


@dataclass(frozen=True)
class AssetPreparation:
    asset: dict[str, Any]
    gltf_path: str
    dependencies: tuple[dict[str, Any], ...]
    source_bounds: dict[str, Any]
    safe_id: str


@dataclass(frozen=True)
class PreparedJob:
    job: dict[str, Any]
    job_bytes: bytes
    manifest_sha256: str
    tree_sha256: str
    revision: str

    @property
    def job_sha256(self) -> str:
        return hashlib.sha256(self.job_bytes).hexdigest()


def _dependency_closure(
    gltf: dict[str, Any],
    *,
    gltf_path: str,
    asset_dir: str,
    pinned: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[Any], list[Any]]:
    buffers = _list(gltf.get("buffers"), "$/buffers")
    images = _list(gltf.get("images"), "$/images", required=False)
    dependencies: dict[str, dict[str, Any]] = {}

    def register(uri: Any, pointer: str, usage: str) -> tuple[str, Mapping[str, Any]]:
        resolved = _safe_dependency_uri(uri, pointer, gltf_path=gltf_path, asset_dir=asset_dir)
        entry = pinned.get(resolved)
        if entry is None:
            fail("UE_IMPORT_DEPENDENCY_MISSING", "glTF dependency is absent from the pinned source manifest", pointer=pointer)
        existing = dependencies.setdefault(
            resolved,
            {
                "path": resolved,
                "bytes": int(entry["bytes"]),
                "sha256": str(entry["sha256"]),
                "usages": [],
            },
        )
        if usage not in existing["usages"]:
            existing["usages"].append(usage)
        return resolved, entry

    for index, raw_buffer in enumerate(buffers):
        pointer = f"$/buffers/{index}"
        buffer = _object(raw_buffer, pointer)
        if "uri" not in buffer:
            fail("UE_IMPORT_GLTF_URI_INVALID", "JSON glTF buffers must use an external pinned URI", pointer=f"{pointer}/uri")
        _resolved, entry = register(buffer["uri"], f"{pointer}/uri", "buffer")
        declared = _positive_integer(buffer.get("byteLength"), f"{pointer}/byteLength")
        if declared != int(entry["bytes"]):
            fail("UE_IMPORT_BUFFER_SIZE_MISMATCH", "glTF buffer byteLength differs from the pinned file size", pointer=pointer)

    for index, raw_image in enumerate(images):
        pointer = f"$/images/{index}"
        image = _object(raw_image, pointer)
        if "uri" not in image:
            fail("UE_IMPORT_GLTF_URI_INVALID", "Embedded glTF images are outside this import contract", pointer=f"{pointer}/uri")
        register(image["uri"], f"{pointer}/uri", "image")

    expected = set(pinned) - {gltf_path}
    observed = set(dependencies)
    orphaned = sorted(expected - observed)
    if orphaned:
        fail(
            "UE_IMPORT_DEPENDENCY_ORPHANED",
            "Pinned asset files are not referenced by the glTF dependency closure",
            pointer=orphaned[0],
        )
    return dependencies, buffers, images


def _source_bounds(gltf: dict[str, Any]) -> dict[str, Any]:
    meshes = _list(gltf.get("meshes"), "$/meshes")
    accessors = _list(gltf.get("accessors"), "$/accessors")
    used: set[int] = set()
    for mesh_index, raw_mesh in enumerate(meshes):
        mesh = _object(raw_mesh, f"$/meshes/{mesh_index}")
        primitives = _list(mesh.get("primitives"), f"$/meshes/{mesh_index}/primitives")
        for primitive_index, raw_primitive in enumerate(primitives):
            pointer = f"$/meshes/{mesh_index}/primitives/{primitive_index}"
            primitive = _object(raw_primitive, pointer)
            attributes = _object(primitive.get("attributes"), f"{pointer}/attributes")
            if "POSITION" not in attributes:
                fail("UE_IMPORT_GLTF_BOUNDS_MISSING", "Every imported primitive needs a POSITION accessor", pointer=pointer)
            used.add(_index(attributes["POSITION"], len(accessors), f"{pointer}/attributes/POSITION"))
    if not used:
        fail("UE_IMPORT_GLTF_BOUNDS_MISSING", "No POSITION accessor was referenced")

    minima = [math.inf, math.inf, math.inf]
    maxima = [-math.inf, -math.inf, -math.inf]
    evidence: list[dict[str, Any]] = []
    for index in sorted(used):
        pointer = f"$/accessors/{index}"
        accessor = _object(accessors[index], pointer)
        if accessor.get("type") != "VEC3":
            fail("UE_IMPORT_GLTF_BOUNDS_INVALID", "POSITION accessor type must be VEC3", pointer=f"{pointer}/type")
        _positive_integer(accessor.get("count"), f"{pointer}/count")
        minimum = _finite_vec3(accessor.get("min"), f"{pointer}/min")
        maximum = _finite_vec3(accessor.get("max"), f"{pointer}/max")
        if any(float(low) > float(high) for low, high in zip(minimum, maximum)):
            fail("UE_IMPORT_GLTF_BOUNDS_INVALID", "POSITION minimum exceeds maximum", pointer=pointer)
        for axis in range(3):
            minima[axis] = min(minima[axis], float(minimum[axis]))
            maxima[axis] = max(maxima[axis], float(maximum[axis]))
        evidence.append({"accessor_index": index, "min": minimum, "max": maximum})
    aggregate_min = [0 if value == 0 else value for value in minima]
    aggregate_max = [0 if value == 0 else value for value in maxima]
    dimensions = [0 if high - low == 0 else high - low for low, high in zip(aggregate_min, aggregate_max)]
    return {
        "basis": "gltf_POSITION_accessor_local_extrema_aggregate",
        "coordinate_unit": "meter",
        "includes_node_transforms": False,
        "position_accessors": evidence,
        "aggregate_min": aggregate_min,
        "aggregate_max": aggregate_max,
        "dimensions": dimensions,
    }


def _prepare_asset(asset: dict[str, Any], acquisition_dir: Path) -> AssetPreparation:
    files = asset["files"]
    gltf_entries = [entry for entry in files if str(entry["path"]).endswith(".gltf")]
    if len(gltf_entries) != 1:
        fail("UE_IMPORT_GLTF_CARDINALITY_INVALID", "Each source asset must pin exactly one .gltf file", pointer=asset["asset_id"])
    gltf_entry = gltf_entries[0]
    gltf_path = str(gltf_entry["path"])
    gltf = strict_json(_secure_read_pinned(acquisition_dir, gltf_entry), pointer=gltf_path)
    gltf = _object(gltf, "$")
    asset_metadata = _object(gltf.get("asset"), "$/asset")
    if asset_metadata.get("version") != "2.0":
        fail("UE_IMPORT_GLTF_VERSION_UNSUPPORTED", "Only glTF 2.0 sources are accepted", pointer="$/asset/version")
    pinned = {str(entry["path"]): entry for entry in files}
    asset_dir = PurePosixPath(gltf_path).parts[0]
    dependencies, _buffers, _images = _dependency_closure(
        gltf,
        gltf_path=gltf_path,
        asset_dir=asset_dir,
        pinned=pinned,
    )
    source_bounds = _source_bounds(gltf)
    safe_id = _ue_safe_id(str(asset["source_asset_id"]))
    normalized_dependencies = tuple(
        {**dependencies[path], "usages": sorted(dependencies[path]["usages"])} for path in sorted(dependencies)
    )
    return AssetPreparation(asset=asset, gltf_path=gltf_path, dependencies=normalized_dependencies, source_bounds=source_bounds, safe_id=safe_id)


def build_job(manifest_path: Path, acquisition_dir: Path) -> PreparedJob:
    try:
        source_plan = acquisition.build_plan(manifest_path, acquisition_dir)
        if not acquisition.verify_existing(source_plan):
            fail("UE_IMPORT_ACQUISITION_MISSING", "A complete verified acquisition output is required")
    except acquisition.AcquisitionError as error:
        fail("UE_IMPORT_ACQUISITION_INVALID", "Acquisition manifest, receipt, or tree verification failed", pointer=error.pointer)

    prepared = [_prepare_asset(asset, acquisition_dir) for asset in source_plan.manifest["assets"]]
    if len({item.safe_id for item in prepared}) != len(prepared):
        fail("UE_IMPORT_DESTINATION_COLLISION", "Source assets map to the same UE destination folder")

    assets: list[dict[str, Any]] = []
    for item in sorted(prepared, key=lambda value: value.safe_id):
        assets.append(
            {
                "asset_id": item.asset["asset_id"],
                "source_asset_id": item.asset["source_asset_id"],
                "semantic_roles": sorted(item.asset["semantic_roles"]),
                "source_gltf": {
                    "path": item.gltf_path,
                    "bytes": next(entry["bytes"] for entry in item.asset["files"] if entry["path"] == item.gltf_path),
                    "sha256": next(entry["sha256"] for entry in item.asset["files"] if entry["path"] == item.gltf_path),
                },
                "external_dependencies": list(item.dependencies),
                "source_bounds": item.source_bounds,
                "destination_content_path": f"{DESTINATION_ROOT}/{item.safe_id}",
                "expected_object_paths": [],
                "post_import_verification": {
                    "returned_objects": {
                        "require_at_least_one_class": "StaticMesh",
                        "record_exact_object_paths_after_import": True,
                        "reject_object_redirectors": True,
                    },
                    "materials_and_textures": {
                        "record_material_slots": True,
                        "account_for_every_source_image": True,
                        "reject_missing_or_default_only_materials": True,
                    },
                    "bounds": {
                        "source_units_to_unreal_centimeters": 100,
                        "relative_tolerance": 0.02,
                        "absolute_tolerance_cm": 1.0,
                        "operator_must_reconcile_node_transforms_and_mesh_splitting": True,
                    },
                    "collision": {
                        "require_nonempty_simple_collision_or_reviewed_complex_as_simple": True,
                        "record_collision_complexity_and_primitive_count": True,
                    },
                    "pbr_channel_review": {
                        "base_color_srgb": True,
                        "normal_source_convention": "OpenGL",
                        "normal_green_channel_conversion_must_be_verified": True,
                        "arm_texture_srgb": False,
                        "arm_channels": {"r": "ambient_occlusion", "g": "roughness", "b": "metallic"},
                    },
                },
            }
        )

    job = {
        "schema": JOB_SCHEMA,
        "source_binding": {
            "source_schema": source_plan.manifest["schema"],
            "revision": source_plan.manifest["revision"],
            "provider": source_plan.manifest["provider"],
            "license": source_plan.manifest["license"],
            "manifest_sha256": source_plan.manifest_sha256,
            "tree_sha256": acquisition._tree_digest(source_plan.entries),
            "file_count": len(source_plan.entries),
            "total_bytes": source_plan.total_bytes,
        },
        "engine_contract": {
            "engine_version": ENGINE_VERSION,
            "python_api_status": "experimental",
            "import_route": "Interchange",
            "official_api_references": [
                "https://dev.epicgames.com/documentation/en-us/unreal-engine/python-api/class/AssetImportTask.html?application_version=5.3",
                "https://dev.epicgames.com/documentation/en-us/unreal-engine/python-api/class/InterchangeManager.html?application_version=5.3",
            ],
            "asset_import_task": {
                "automated": True,
                "async": False,
                "replace_existing": False,
                "replace_existing_settings": False,
                "save": True,
            },
            "destination_root": DESTINATION_ROOT,
            "follow_redirectors": False,
        },
        "assets": assets,
        "execution_gate": {
            "prepared_only": True,
            "unreal_started": False,
            "content_imported": False,
            "requires_disposable_project_copy": True,
            "requires_operator_approval": True,
            "requires_ue_5_3_2_api_probe": True,
            "do_not_add_to_semantic_index_before_post_import_verification": True,
        },
    }
    job_bytes = canonical_json(job)

    # Re-run the source verification after parsing so an in-place change cannot
    # silently bridge the verified acquisition and emitted job.
    try:
        acquisition.verify_existing(source_plan)
    except acquisition.AcquisitionError as error:
        fail("UE_IMPORT_SOURCE_CHANGED", "Acquisition changed during job preparation", pointer=error.pointer)
    return PreparedJob(
        job=job,
        job_bytes=job_bytes,
        manifest_sha256=source_plan.manifest_sha256,
        tree_sha256=acquisition._tree_digest(source_plan.entries),
        revision=source_plan.manifest["revision"],
    )


def validate_approval_ref(value: str) -> str:
    if not APPROVAL_RE.fullmatch(value) or any(word in value.lower() for word in ("token", "secret", "password", "bearer", "apikey")):
        fail(
            "UE_IMPORT_APPROVAL_INVALID",
            "Approval reference must be a non-secret APPROVAL-/CHANGE-/TICKET-/VISTA- identifier",
            pointer="--approval-ref",
        )
    return hashlib.sha256(b"simworld-ue-import-approval/v1\0" + value.encode("utf-8")).hexdigest()


def validate_output_dir(output_dir: Path) -> Path:
    if not output_dir.is_absolute() or output_dir == Path(output_dir.anchor) or not OUTPUT_NAME_RE.fullmatch(output_dir.name):
        fail("UE_IMPORT_OUTPUT_INVALID", "Output must be an absolute non-root safe directory", pointer="--output-dir")
    parent = output_dir.parent
    try:
        acquisition._reject_symlink_components(parent, "--output-dir")
        metadata = os.lstat(parent)
    except acquisition.AcquisitionError:
        fail("UE_IMPORT_OUTPUT_INVALID", "Output parent must exist without symlink components", pointer="--output-dir")
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        fail("UE_IMPORT_OUTPUT_INVALID", "Output parent must be current-user-owned and private", pointer="--output-dir")
    try:
        os.lstat(output_dir)
    except FileNotFoundError:
        return output_dir
    fail("UE_IMPORT_OUTPUT_EXISTS", "Evidence publication is non-overwriting; choose a new output directory", pointer="--output-dir")


def _write_private(path: Path, raw: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rename_directory_no_replace(source: Path, destination: Path) -> None:
    """Atomically publish a directory without replacing any destination."""

    renameat2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if renameat2 is None:
        fail(
            "UE_IMPORT_ATOMIC_PUBLISH_UNAVAILABLE",
            "The host does not expose renameat2(RENAME_NOREPLACE); publication was not attempted",
        )
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    at_fdcwd = -100
    rename_noreplace = 1
    result = renameat2(
        at_fdcwd,
        os.fsencode(source),
        at_fdcwd,
        os.fsencode(destination),
        rename_noreplace,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        fail("UE_IMPORT_OUTPUT_EXISTS", "Evidence publication is non-overwriting; choose a new output directory")
    if error_number in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP}:
        fail(
            "UE_IMPORT_ATOMIC_PUBLISH_UNAVAILABLE",
            "The filesystem does not support atomic no-replace publication",
        )
    raise OSError(error_number, os.strerror(error_number), str(destination))


def publish_job(prepared: PreparedJob, output_dir: Path, approval_ref: str) -> dict[str, Any]:
    output_dir = validate_output_dir(output_dir)
    approval_sha256 = validate_approval_ref(approval_ref)
    lock = output_dir.parent / f".{output_dir.name}.lock"
    lock_descriptor: int | None = None
    owns_lock = False
    temporary: Path | None = None
    try:
        lock_descriptor = os.open(
            lock,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        owns_lock = True
        os.close(lock_descriptor)
        lock_descriptor = None
        validate_output_dir(output_dir)
        temporary = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.tmp-", dir=output_dir.parent))
        os.chmod(temporary, 0o700)
        _write_private(temporary / "import-job.json", prepared.job_bytes)
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "job_schema": JOB_SCHEMA,
            "job_sha256": prepared.job_sha256,
            "source_revision": prepared.revision,
            "source_manifest_sha256": prepared.manifest_sha256,
            "source_tree_sha256": prepared.tree_sha256,
            "approval_ref_sha256": approval_sha256,
            "prepared_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "publication_policy": "non_overwriting",
            "unreal_started": False,
            "content_imported": False,
        }
        # Receipt is deliberately the final file written; its presence marks a
        # complete preparation bundle, not a completed UE import.
        _write_private(temporary / "preparation-receipt.json", canonical_json(receipt))
        _fsync_directory(temporary)
        _rename_directory_no_replace(temporary, output_dir)
        temporary = None
        _fsync_directory(output_dir.parent)
        return receipt
    except FileExistsError:
        fail("UE_IMPORT_OUTPUT_BUSY", "Another preparation owns the output lock")
    finally:
        if lock_descriptor is not None:
            os.close(lock_descriptor)
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)
        if owns_lock:
            try:
                lock.unlink()
            except FileNotFoundError:
                pass


def result(prepared: PreparedJob, status: str, *, output_dir: Path | None = None) -> dict[str, Any]:
    return {
        "schema": RESULT_SCHEMA,
        "valid": True,
        "status": status,
        "source_revision": prepared.revision,
        "source_manifest_sha256": prepared.manifest_sha256,
        "source_tree_sha256": prepared.tree_sha256,
        "job_sha256": prepared.job_sha256,
        "asset_count": len(prepared.job["assets"]),
        "output_dir": str(output_dir) if output_dir else None,
        "network_used": False,
        "unreal_started": False,
        "content_imported": False,
    }


class SafeParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        fail("UE_IMPORT_ARGUMENT_INVALID", "CLI arguments are missing or invalid; consult --help")


def build_parser() -> argparse.ArgumentParser:
    parser = SafeParser(description=__doc__)
    default_manifest = Path(__file__).resolve().parent / "assets" / "vista_mmg_040_cc0_bootstrap.json"
    parser.add_argument("--manifest", default=str(default_manifest))
    parser.add_argument("--acquisition-dir", required=True)
    parser.add_argument("--output-dir")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approval-ref", default="")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    os.umask(0o077)
    try:
        args = build_parser().parse_args(argv)
        if args.apply and not args.output_dir:
            fail("UE_IMPORT_ARGUMENT_INVALID", "--apply requires --output-dir")
        if not args.apply and args.approval_ref:
            fail("UE_IMPORT_ARGUMENT_INVALID", "--approval-ref is accepted only with --apply")
        prepared = build_job(Path(args.manifest), Path(args.acquisition_dir))
        output_dir = Path(args.output_dir) if args.output_dir else None
        if output_dir is not None:
            validate_output_dir(output_dir)
        if args.apply:
            assert output_dir is not None
            publish_job(prepared, output_dir, args.approval_ref)
            status = "published"
        else:
            status = "dry_run"
        print(compact_json(result(prepared, status, output_dir=output_dir)))
        return 0
    except ImportJobError as error:
        print(
            compact_json({"schema": RESULT_SCHEMA, "valid": False, "status": "failed", "error": error.public_dict()}),
            file=sys.stderr,
        )
        return 2
    except Exception:
        print(
            compact_json(
                {
                    "schema": RESULT_SCHEMA,
                    "valid": False,
                    "status": "failed",
                    "error": {
                        "code": "UE_IMPORT_INTERNAL_ERROR",
                        "message": "Preparation failed before a safe result was produced",
                    },
                }
            ),
            file=sys.stderr,
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
