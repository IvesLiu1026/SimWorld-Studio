#!/usr/bin/env python3
"""Prepare, but never implicitly execute, a disposable Blender-to-UE job.

The default command is a zero-write dry run.  ``--apply`` only copies the
byte-pinned r7 source project into a fresh append-only attempt directory and
publishes the preparation contract.  Unreal is deliberately never launched by
this module.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import errno
import hashlib
import json
import math
import os
import pathlib
import re
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence


Path = pathlib.Path

PLAN_SCHEMA = "simworld.vista.blender-ue-preparation-plan/v1"
PREPARATION_RECEIPT_SCHEMA = "simworld.vista.blender-ue-preparation-receipt/v1"
RESULT_SCHEMA = "simworld.vista.blender-ue-preparation-result/v1"
BLENDER_MANIFEST_SCHEMA = "simworld.vista.blender-asset-manifest/v1"

IMPORT_CONTENT_ROOT = "/Game/VISTA/External/Procedural/MMG040OfficeR1"
SOURCE_MAP = "/Game/VISTA/Scenes/MMG040_Office_CommandletR3"
OUTPUT_MAP = "/Game/VISTA/Scenes/MMG040_Office_BlenderR1"
SOURCE_MAP_RELATIVE = Path("Content/VISTA/Scenes/MMG040_Office_CommandletR3.umap")
PROJECT_FILE_NAME = "gym_citynav.uproject"
SOURCE_CONTAINER_NAME = "disposable-project-r7"

PINNED_SOURCE_PROJECT_SHA256 = "f9a1471857d4be15c37cff52229d093a13d657269f37fd9f88d0b4c35726b82c"
PINNED_SOURCE_MAP_SHA256 = "afa9ecddf4133a443080827922686b44b4f61bd28418d087da378d429d7bfd14"
PINNED_EDITOR_SHA256 = "5f77cd86b04c042ff84cb3b3083eec6107a8305e051706742a4838685eba2e48"
PINNED_TRANSLATOR_SHA256 = "312f533ec5e77150d75a219c7658ceb7d44aff606d0c94d5311d9a11f9c23319"
PINNED_OFFICIAL_CONTENT_ROOT = (
    "/mnt/NAS2/yhliu/SimWorldStudio/0.2.0-806e869a/runtime/"
    "SimWorld-Studio-Minimal-806e869a/gym_citynav/Content"
)

COPY_EXCLUDED_TOP_LEVEL = frozenset({"DerivedDataCache", "Intermediate", "Saved", ".git"})
FORBIDDEN_DESTINATION_COMPONENTS = frozenset(
    {
        "archive",
        "archives",
        "archived",
        "canonical",
        "production",
        "release",
        "releases",
        "r8",
        "disposable-project-r8",
    }
)
REQUIRED_ASSET_CONTRACTS = {
    "tall_office_cabinet": {
        "collection": "VISTA_TallOfficeCabinet",
        "mesh_prefix": "VISTA_Cabinet_",
    },
    "room_shell_kit": {
        "collection": "VISTA_RoomShell",
        "mesh_prefix": "VISTA_Room_",
    },
    "ergonomic_office_chair": {
        "collection": "VISTA_ErgonomicOfficeChair",
        "mesh_prefix": "VISTA_Chair_",
    },
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
APPROVAL_RE = re.compile(r"^[A-Z][A-Z0-9_-]{7,127}$")


class VistaBlenderUEContractError(RuntimeError):
    """A stable, public fail-closed contract error."""

    def __init__(self, code: str, message: str, *, pointer: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.pointer = pointer

    def public_dict(self) -> dict[str, str]:
        result = {"code": self.code, "message": self.message}
        if self.pointer:
            result["pointer"] = self.pointer
        return result


def fail(code: str, message: str, *, pointer: str | None = None) -> None:
    raise VistaBlenderUEContractError(code, message, pointer=pointer)


@dataclass(frozen=True)
class SourcePins:
    container_name: str = SOURCE_CONTAINER_NAME
    project_sha256: str = PINNED_SOURCE_PROJECT_SHA256
    map_sha256: str = PINNED_SOURCE_MAP_SHA256
    external_content_root: str = PINNED_OFFICIAL_CONTENT_ROOT


@dataclass(frozen=True)
class EnginePins:
    editor_sha256: str = PINNED_EDITOR_SHA256
    translator_sha256: str = PINNED_TRANSLATOR_SHA256
    major: int = 5
    minor: int = 3
    patch: int = 2
    changelist: int = 29314046
    branch_name: str = "++UE5+Release-5.3"


@dataclass(frozen=True)
class ProjectSnapshot:
    tree_sha256: str
    file_count: int
    total_bytes: int
    records: tuple[tuple[str, int, int, str], ...]
    external_symlink_count: int
    external_symlink_sha256: str


@dataclass(frozen=True)
class PreparedPlan:
    plan: dict[str, Any]
    plan_bytes: bytes
    plan_sha256: str
    source_snapshot: ProjectSnapshot


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _unique_object(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            fail("VISTA_BLENDER_UE_JSON_DUPLICATE_KEY", "JSON contains a duplicate object key")
        result[key] = value
    return result


def load_json(path: Path, *, code: str) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except VistaBlenderUEContractError:
        raise
    except Exception:
        fail(code, "JSON input is unreadable or malformed", pointer=str(path))
    if not isinstance(value, dict):
        fail(code, "JSON input must contain one object", pointer=str(path))
    return value, raw


def _absolute_lexical(path: Path, *, code: str, pointer: str) -> Path:
    value = str(path)
    if not path.is_absolute() or os.path.normpath(value) != value:
        fail(code, "Path must be absolute, normalized, and contain no '..' components", pointer=pointer)
    return path


def _reject_symlink_components(path: Path, *, code: str, pointer: str) -> None:
    path = _absolute_lexical(path, code=code, pointer=pointer)
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(metadata.st_mode):
            fail(code, "Symlink path components are forbidden", pointer=pointer)


def _canonical_existing(path: Path, *, kind: str, code: str, pointer: str) -> Path:
    _reject_symlink_components(path, code=code, pointer=pointer)
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        fail(code, f"Required {kind} does not exist", pointer=pointer)
    if resolved != path:
        fail(code, "Path must already be canonical", pointer=pointer)
    metadata = os.lstat(path)
    if kind == "file" and not stat.S_ISREG(metadata.st_mode):
        fail(code, "Required input must be a regular file", pointer=pointer)
    if kind == "directory" and not stat.S_ISDIR(metadata.st_mode):
        fail(code, "Required input must be a directory", pointer=pointer)
    return path


def _relative_to(path: Path, parent: Path) -> Path | None:
    try:
        return path.relative_to(parent)
    except ValueError:
        return None


def _validate_sha256(value: Any, *, code: str, pointer: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        fail(code, "Expected a lowercase SHA-256 digest", pointer=pointer)
    return value


def _finite_vector(value: Any, *, code: str, pointer: str, positive: bool = False) -> list[float]:
    if not isinstance(value, list) or len(value) != 3:
        fail(code, "Expected a three-element numeric vector", pointer=pointer)
    result: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(float(item)):
            fail(code, "Vector values must be finite numbers", pointer=pointer)
        number = float(item)
        if positive and number <= 0:
            fail(code, "Vector dimensions must be strictly positive", pointer=pointer)
        result.append(number)
    return result


def interchange_asset_name(mesh_name: str) -> str:
    """Return UE Interchange's deterministic asset name for a Blender mesh.

    Blender exports both a node/object name and a mesh datablock name.  The
    generator deliberately names datablocks ``<object>_Mesh`` while UE 5.3's
    glTF Interchange pipeline names the imported StaticMesh from the node.
    Keep that observed conversion explicit in the signed preparation plan.
    """

    if not isinstance(mesh_name, str) or not mesh_name:
        fail(
            "VISTA_BLENDER_UE_MANIFEST_INVALID",
            "Generated mesh names must be non-empty strings",
        )
    result = mesh_name[:-5] if mesh_name.endswith("_Mesh") else mesh_name
    if not result:
        fail(
            "VISTA_BLENDER_UE_MANIFEST_INVALID",
            "Generated mesh name cannot map to an empty UE asset name",
        )
    return result


def _validate_source_descriptor(project_file: Path, pins: SourcePins) -> dict[str, Any]:
    digest = sha256_file(project_file)
    if digest != pins.project_sha256:
        fail("VISTA_BLENDER_UE_SOURCE_PIN_MISMATCH", "Source .uproject digest differs from the approved r7 pin", pointer=str(project_file))
    descriptor, _ = load_json(project_file, code="VISTA_BLENDER_UE_SOURCE_PROJECT_INVALID")
    plugin_states = {
        entry.get("Name"): entry.get("Enabled")
        for entry in descriptor.get("Plugins", [])
        if isinstance(entry, dict)
    }
    if plugin_states.get("UnrealMCP") is not False or plugin_states.get("PixelStreaming") is not False:
        fail("VISTA_BLENDER_UE_SOURCE_PROJECT_UNSAFE", "The commandlet source must retain r7's disabled network plugins", pointer=str(project_file))
    if plugin_states.get("PythonScriptPlugin") is not True or plugin_states.get("EditorScriptingUtilities") is not True:
        fail("VISTA_BLENDER_UE_SOURCE_PROJECT_UNSAFE", "The pinned commandlet plugins are not enabled", pointer=str(project_file))
    return descriptor


def _external_symlink_record(path: Path, project_root: Path, external_content_root: Path) -> tuple[str, int, int, str]:
    relative = path.relative_to(project_root).as_posix()
    target_text = os.readlink(path)
    target_lexical = Path(target_text)
    if not target_lexical.is_absolute() or os.path.normpath(target_text) != target_text:
        fail("VISTA_BLENDER_UE_SOURCE_EXTERNAL_LINK_REJECTED", "Source external links must use absolute normalized targets", pointer=str(path))
    try:
        target = target_lexical.resolve(strict=True)
        root = external_content_root.resolve(strict=True)
    except OSError:
        fail("VISTA_BLENDER_UE_SOURCE_EXTERNAL_LINK_REJECTED", "Source external link target or pinned content root is missing", pointer=str(path))
    if _relative_to(target, root) is None:
        fail("VISTA_BLENDER_UE_SOURCE_EXTERNAL_LINK_REJECTED", "Source external link escapes the pinned official Content root", pointer=str(path))
    target_metadata = os.stat(target)
    if not (stat.S_ISDIR(target_metadata.st_mode) or stat.S_ISREG(target_metadata.st_mode)):
        fail("VISTA_BLENDER_UE_SOURCE_EXTERNAL_LINK_REJECTED", "Source external link target must be a regular file or directory", pointer=str(path))
    target_kind = "directory" if stat.S_ISDIR(target_metadata.st_mode) else "file"
    if target_kind == "file":
        target_evidence = sha256_file(target)
    else:
        target_evidence = f"{target_metadata.st_dev}:{target_metadata.st_ino}:{target_metadata.st_size}:{target_metadata.st_mtime_ns}"
    raw = (
        "external-symlink\0"
        + target_text
        + "\0"
        + target_kind
        + "\0"
        + target_evidence
    ).encode("utf-8")
    metadata = os.lstat(path)
    return (relative, stat.S_IMODE(metadata.st_mode), metadata.st_size, sha256_bytes(raw))


def snapshot_project(project_root: Path, *, external_content_root: Path | None = None) -> ProjectSnapshot:
    records: list[tuple[str, int, int, str]] = []
    external_records: list[tuple[str, int, int, str]] = []
    for current, directories, files in os.walk(project_root, topdown=True, followlinks=False):
        current_path = Path(current)
        if current_path == project_root:
            directories[:] = sorted(name for name in directories if name not in COPY_EXCLUDED_TOP_LEVEL)
        else:
            directories.sort()
        for directory in list(directories):
            child = current_path / directory
            if stat.S_ISLNK(os.lstat(child).st_mode):
                if external_content_root is None:
                    fail("VISTA_BLENDER_UE_SOURCE_EXTERNAL_LINK_REJECTED", "Source project external link has no pinned root", pointer=str(child))
                record = _external_symlink_record(child, project_root, external_content_root)
                records.append(record)
                external_records.append(record)
                directories.remove(directory)
        for name in sorted(files):
            path = current_path / name
            metadata = os.lstat(path)
            if stat.S_ISLNK(metadata.st_mode):
                if external_content_root is None:
                    fail("VISTA_BLENDER_UE_SOURCE_EXTERNAL_LINK_REJECTED", "Source project external link has no pinned root", pointer=str(path))
                record = _external_symlink_record(path, project_root, external_content_root)
                records.append(record)
                external_records.append(record)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                fail("VISTA_BLENDER_UE_SOURCE_UNSAFE", "Source project may contain only regular files", pointer=str(path))
            relative = path.relative_to(project_root).as_posix()
            records.append((relative, stat.S_IMODE(metadata.st_mode), metadata.st_size, sha256_file(path)))
    if not records:
        fail("VISTA_BLENDER_UE_SOURCE_INVALID", "Source project snapshot is empty")
    raw = b"".join(
        f"{relative}\0{mode:o}\0{size}\0{digest}\n".encode("utf-8")
        for relative, mode, size, digest in records
    )
    external_raw = b"".join(
        f"{relative}\0{mode:o}\0{size}\0{digest}\n".encode("utf-8")
        for relative, mode, size, digest in sorted(external_records)
    )
    return ProjectSnapshot(
        tree_sha256=sha256_bytes(raw),
        file_count=len(records),
        total_bytes=sum(record[2] for record in records),
        records=tuple(sorted(records)),
        external_symlink_count=len(external_records),
        external_symlink_sha256=sha256_bytes(external_raw),
    )


def validate_source_project(source_project: Path, pins: SourcePins) -> tuple[Path, Path, ProjectSnapshot]:
    source_project = _canonical_existing(
        _absolute_lexical(source_project, code="VISTA_BLENDER_UE_SOURCE_INVALID", pointer="--source-project"),
        kind="directory",
        code="VISTA_BLENDER_UE_SOURCE_INVALID",
        pointer="--source-project",
    )
    if any(part.casefold() in {"r8", "disposable-project-r8"} for part in source_project.parts):
        fail("VISTA_BLENDER_UE_R8_REJECTED", "Quarantined r8 cannot be used as a source or destination")
    if source_project.parent.name != pins.container_name or pins.container_name != SOURCE_CONTAINER_NAME:
        fail("VISTA_BLENDER_UE_SOURCE_REVISION_REJECTED", "Only the byte-pinned disposable-project-r7 source is accepted", pointer="--source-project")
    project_file = _canonical_existing(
        source_project / PROJECT_FILE_NAME,
        kind="file",
        code="VISTA_BLENDER_UE_SOURCE_INVALID",
        pointer="source_project/uproject",
    )
    _validate_source_descriptor(project_file, pins)
    source_map_file = _canonical_existing(
        source_project / SOURCE_MAP_RELATIVE,
        kind="file",
        code="VISTA_BLENDER_UE_SOURCE_INVALID",
        pointer="source_project/source_map",
    )
    if sha256_file(source_map_file) != pins.map_sha256:
        fail("VISTA_BLENDER_UE_SOURCE_PIN_MISMATCH", "Source map digest differs from the approved R3/r7 pin", pointer=str(source_map_file))
    return project_file, source_map_file, snapshot_project(
        source_project,
        external_content_root=Path(pins.external_content_root),
    )


def validate_attempt_root(run_root: Path, attempt_root: Path, source_project: Path) -> tuple[Path, Path]:
    run_root = _canonical_existing(
        _absolute_lexical(run_root, code="VISTA_BLENDER_UE_DESTINATION_INVALID", pointer="--run-root"),
        kind="directory",
        code="VISTA_BLENDER_UE_DESTINATION_INVALID",
        pointer="--run-root",
    )
    attempt_root = _absolute_lexical(attempt_root, code="VISTA_BLENDER_UE_DESTINATION_INVALID", pointer="--attempt-root")
    _reject_symlink_components(attempt_root, code="VISTA_BLENDER_UE_DESTINATION_INVALID", pointer="--attempt-root")
    relative = _relative_to(attempt_root, run_root)
    if relative is None or len(relative.parts) < 2 or relative.parts[0] != "ue":
        fail("VISTA_BLENDER_UE_DESTINATION_INVALID", "Attempt root must be a fresh descendant of <run-root>/ue", pointer="--attempt-root")
    bad = sorted({part for part in attempt_root.parts if part.casefold() in FORBIDDEN_DESTINATION_COMPONENTS})
    if bad:
        code = "VISTA_BLENDER_UE_R8_REJECTED" if any(part.casefold() in {"r8", "disposable-project-r8"} for part in bad) else "VISTA_BLENDER_UE_DESTINATION_FORBIDDEN"
        fail(code, "Canonical, archive, Production, release, and r8 destinations are forbidden", pointer="--attempt-root")
    if _relative_to(attempt_root, source_project) is not None or _relative_to(source_project, attempt_root) is not None:
        fail("VISTA_BLENDER_UE_DESTINATION_INVALID", "Source and destination trees must not overlap", pointer="--attempt-root")
    try:
        os.lstat(attempt_root)
    except FileNotFoundError:
        return run_root, attempt_root
    fail("VISTA_BLENDER_UE_DESTINATION_EXISTS", "Attempt publication is append-only; choose a new path", pointer="--attempt-root")


def _validate_output_file(
    output: Mapping[str, Any],
    *,
    key: str,
    run_root: Path,
    manifest_dir: Path,
    suffixes: set[str],
) -> dict[str, Any]:
    pointer = f"manifest/outputs/{key}"
    if not isinstance(output, Mapping):
        fail("VISTA_BLENDER_UE_MANIFEST_INVALID", "Manifest output entry must be an object", pointer=pointer)
    path_value = output.get("path")
    if not isinstance(path_value, str):
        fail("VISTA_BLENDER_UE_MANIFEST_INVALID", "Manifest output path is required", pointer=pointer + "/path")
    lexical_path = Path(path_value)
    if lexical_path.is_absolute():
        path = _absolute_lexical(lexical_path, code="VISTA_BLENDER_UE_ASSET_INVALID", pointer=pointer + "/path")
    else:
        if (
            "\\" in path_value
            or ":" in path_value
            or "%" in path_value
            or any(part in {"", ".", ".."} for part in pathlib.PurePosixPath(path_value).parts)
        ):
            fail("VISTA_BLENDER_UE_ASSET_INVALID", "Relative output paths must be literal POSIX paths without traversal", pointer=pointer + "/path")
        path = manifest_dir / pathlib.PurePosixPath(path_value)
    path = _canonical_existing(
        path,
        kind="file",
        code="VISTA_BLENDER_UE_ASSET_INVALID",
        pointer=pointer + "/path",
    )
    relative = _relative_to(path, run_root)
    if relative is None or not relative.parts or relative.parts[0] != "blender":
        fail("VISTA_BLENDER_UE_ASSET_INVALID", "Generated assets must remain below <run-root>/blender", pointer=pointer + "/path")
    if path.suffix.casefold() not in suffixes:
        fail("VISTA_BLENDER_UE_ASSET_INVALID", "Generated asset has an unsupported extension", pointer=pointer + "/path")
    digest = _validate_sha256(output.get("sha256"), code="VISTA_BLENDER_UE_MANIFEST_INVALID", pointer=pointer + "/sha256")
    size = output.get("bytes")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        fail("VISTA_BLENDER_UE_MANIFEST_INVALID", "Manifest output byte count must be positive", pointer=pointer + "/bytes")
    if path.stat().st_size != size or sha256_file(path) != digest:
        fail("VISTA_BLENDER_UE_ASSET_PIN_MISMATCH", "Generated output bytes differ from the manifest pin", pointer=pointer)
    return {
        "path": str(path),
        "sha256": digest,
        "bytes": size,
        "media_type": output.get("media_type"),
    }


def _validate_glb_header(path: Path) -> None:
    raw = path.read_bytes()
    if len(raw) < 20 or raw[:4] != b"glTF" or int.from_bytes(raw[4:8], "little") != 2:
        fail("VISTA_BLENDER_UE_GLB_INVALID", "GLB must contain a glTF 2.0 binary header", pointer=str(path))
    if int.from_bytes(raw[8:12], "little") != len(raw):
        fail("VISTA_BLENDER_UE_GLB_INVALID", "GLB declared length differs from the pinned file", pointer=str(path))


def _validate_gltf_json(path: Path) -> dict[str, int | None]:
    document, _ = load_json(path, code="VISTA_BLENDER_UE_GLTF_INVALID")
    asset = document.get("asset")
    if not isinstance(asset, dict) or asset.get("version") != "2.0":
        fail("VISTA_BLENDER_UE_GLTF_INVALID", "glTF fallback must declare asset.version 2.0", pointer=str(path))
    dependencies: dict[str, int | None] = {}
    for collection_name in ("buffers", "images"):
        collection = document.get(collection_name, [])
        if not isinstance(collection, list):
            fail("VISTA_BLENDER_UE_GLTF_INVALID", f"glTF {collection_name} must be an array", pointer=str(path))
        for entry in collection:
            if not isinstance(entry, dict) or "uri" not in entry:
                continue
            uri = entry["uri"]
            if (
                not isinstance(uri, str)
                or not uri
                or "\\" in uri
                or ":" in uri
                or "?" in uri
                or "#" in uri
                or "%" in uri
                or Path(uri).is_absolute()
                or any(part in {"", ".", ".."} for part in pathlib.PurePosixPath(uri).parts)
            ):
                fail("VISTA_BLENDER_UE_GLTF_INVALID", "glTF external URIs must be literal relative POSIX paths", pointer=str(path))
            expected_bytes = None
            if collection_name == "buffers":
                expected_bytes = entry.get("byteLength")
                if isinstance(expected_bytes, bool) or not isinstance(expected_bytes, int) or expected_bytes <= 0:
                    fail("VISTA_BLENDER_UE_GLTF_INVALID", "External glTF buffers require a positive byteLength", pointer=str(path))
            if uri in dependencies and dependencies[uri] != expected_bytes:
                fail("VISTA_BLENDER_UE_GLTF_INVALID", "glTF external dependency is reused with inconsistent metadata", pointer=str(path))
            dependencies[uri] = expected_bytes
    return dict(sorted(dependencies.items()))


def validate_blender_manifest(manifest_path: Path, run_root: Path, *, import_format: str) -> dict[str, Any]:
    if import_format not in {"glb", "gltf"}:
        fail("VISTA_BLENDER_UE_IMPORT_FORMAT_INVALID", "Import format must be exactly glb or gltf")
    manifest_path = _canonical_existing(
        _absolute_lexical(manifest_path, code="VISTA_BLENDER_UE_MANIFEST_INVALID", pointer="--blender-manifest"),
        kind="file",
        code="VISTA_BLENDER_UE_MANIFEST_INVALID",
        pointer="--blender-manifest",
    )
    relative = _relative_to(manifest_path, run_root)
    if relative is None or not relative.parts or relative.parts[0] != "blender":
        fail("VISTA_BLENDER_UE_MANIFEST_INVALID", "Manifest must remain below <run-root>/blender", pointer="--blender-manifest")
    manifest, raw = load_json(manifest_path, code="VISTA_BLENDER_UE_MANIFEST_INVALID")
    if manifest.get("schema") != BLENDER_MANIFEST_SCHEMA:
        fail("VISTA_BLENDER_UE_MANIFEST_INVALID", "Unexpected Blender asset manifest schema", pointer="manifest/schema")
    outputs = manifest.get("outputs")
    if not isinstance(outputs, dict):
        fail("VISTA_BLENDER_UE_MANIFEST_INVALID", "Manifest outputs object is required", pointer="manifest/outputs")

    candidates: list[dict[str, Any]] = []
    if "glb" in outputs:
        glb = _validate_output_file(outputs["glb"], key="glb", run_root=run_root, manifest_dir=manifest_path.parent, suffixes={".glb"})
        _validate_glb_header(Path(glb["path"]))
        glb["format"] = "glb"
        candidates.append(glb)
    if "gltf" in outputs:
        gltf = _validate_output_file(outputs["gltf"], key="gltf", run_root=run_root, manifest_dir=manifest_path.parent, suffixes={".gltf"})
        dependencies = _validate_gltf_json(Path(gltf["path"]))
        pinned_dependencies: list[dict[str, Any]] = []
        output_paths: dict[str, tuple[str, Mapping[str, Any]]] = {}
        for output_key, output_value in outputs.items():
            if not isinstance(output_value, Mapping) or not isinstance(output_value.get("path"), str):
                continue
            raw_path = Path(output_value["path"])
            resolved_output = raw_path if raw_path.is_absolute() else manifest_path.parent / raw_path
            output_paths[os.path.realpath(resolved_output)] = (output_key, output_value)
        for uri, expected_bytes in dependencies.items():
            dependency_path = Path(gltf["path"]).parent / pathlib.PurePosixPath(uri)
            dependency_path = Path(os.path.realpath(dependency_path))
            matched = output_paths.get(str(dependency_path))
            if matched is None:
                fail("VISTA_BLENDER_UE_GLTF_DEPENDENCY_UNPINNED", "Every glTF external dependency must have a manifest output pin", pointer=uri)
            output_key, output_value = matched
            pinned = _validate_output_file(
                    output_value,
                    key=output_key,
                    run_root=run_root,
                    manifest_dir=manifest_path.parent,
                    suffixes={dependency_path.suffix.casefold()},
                )
            if expected_bytes is not None and pinned["bytes"] != expected_bytes:
                fail("VISTA_BLENDER_UE_GLTF_BUFFER_SIZE_MISMATCH", "glTF buffer byteLength differs from the pinned dependency", pointer=uri)
            pinned_dependencies.append(pinned)
        gltf["external_dependencies"] = pinned_dependencies
        gltf["format"] = "gltf"
        candidates.append(gltf)
    if not candidates:
        fail("VISTA_BLENDER_UE_MANIFEST_INVALID", "Manifest must pin GLB and/or JSON glTF output", pointer="manifest/outputs")

    assets_value = manifest.get("assets")
    by_id: dict[str, dict[str, Any]] = {}
    if isinstance(assets_value, dict):
        for asset_id, value in assets_value.items():
            if not isinstance(asset_id, str) or not isinstance(value, dict):
                fail("VISTA_BLENDER_UE_MANIFEST_INVALID", "Manifest asset mapping is invalid", pointer="manifest/assets")
            by_id[asset_id] = value
    elif isinstance(assets_value, list):
        for index, value in enumerate(assets_value):
            pointer = f"manifest/assets/{index}"
            if not isinstance(value, dict) or not isinstance(value.get("asset_id"), str):
                fail("VISTA_BLENDER_UE_MANIFEST_INVALID", "Each list-form asset requires a stable asset_id", pointer=pointer)
            asset_id = value["asset_id"]
            if asset_id in by_id:
                fail("VISTA_BLENDER_UE_MANIFEST_INVALID", "Asset identifiers must be unique", pointer=pointer)
            by_id[asset_id] = value
    else:
        fail("VISTA_BLENDER_UE_MANIFEST_INVALID", "Manifest assets must be an object or list", pointer="manifest/assets")

    required_assets: list[dict[str, Any]] = []
    for asset_id, expected in REQUIRED_ASSET_CONTRACTS.items():
        value = by_id.get(asset_id)
        if not isinstance(value, dict):
            fail("VISTA_BLENDER_UE_MANIFEST_INVALID", f"Required generated asset '{asset_id}' is missing", pointer="manifest/assets")
        if value.get("collection") != expected["collection"]:
            fail("VISTA_BLENDER_UE_MANIFEST_INVALID", "Generated collection identity mismatch", pointer=f"manifest/assets/{asset_id}/collection")
        mesh_names = value.get("mesh_names")
        material_names = value.get("material_names")
        if (
            not isinstance(mesh_names, list)
            or not mesh_names
            or len(set(mesh_names)) != len(mesh_names)
            or any(not isinstance(name, str) or not name.startswith(expected["mesh_prefix"]) for name in mesh_names)
        ):
            fail("VISTA_BLENDER_UE_MANIFEST_INVALID", "Mesh names must be unique and use the required VISTA prefix", pointer=f"manifest/assets/{asset_id}/mesh_names")
        if not isinstance(material_names, list) or not material_names or any(not isinstance(name, str) or not name for name in material_names):
            fail("VISTA_BLENDER_UE_MANIFEST_INVALID", "Every generated asset requires named materials", pointer=f"manifest/assets/{asset_id}/material_names")
        bounds = value.get("bounds")
        if not isinstance(bounds, dict):
            fail("VISTA_BLENDER_UE_MANIFEST_INVALID", "Every generated asset requires measured bounds", pointer=f"manifest/assets/{asset_id}/bounds")
        dimensions = bounds.get("dimensions_m", bounds.get("dimensions"))
        _finite_vector(dimensions, code="VISTA_BLENDER_UE_MANIFEST_INVALID", pointer=f"manifest/assets/{asset_id}/bounds/dimensions_m", positive=True)
        origin_m = _finite_vector(value.get("origin_m"), code="VISTA_BLENDER_UE_MANIFEST_INVALID", pointer=f"manifest/assets/{asset_id}/origin_m")
        placement = value.get("ue_placement")
        if not isinstance(placement, dict):
            fail("VISTA_BLENDER_UE_MANIFEST_INVALID", "Explicit UE placement is required; axis conversion is never guessed", pointer=f"manifest/assets/{asset_id}/ue_placement")
        location_cm = _finite_vector(placement.get("location_cm"), code="VISTA_BLENDER_UE_MANIFEST_INVALID", pointer=f"manifest/assets/{asset_id}/ue_placement/location_cm")
        rotation_deg = _finite_vector(placement.get("rotation_deg"), code="VISTA_BLENDER_UE_MANIFEST_INVALID", pointer=f"manifest/assets/{asset_id}/ue_placement/rotation_deg")
        scale = _finite_vector(placement.get("scale"), code="VISTA_BLENDER_UE_MANIFEST_INVALID", pointer=f"manifest/assets/{asset_id}/ue_placement/scale", positive=True)
        mesh_bindings = [
            {
                "source_mesh_name": name,
                "ue_asset_name": interchange_asset_name(name),
            }
            for name in sorted(mesh_names)
        ]
        ue_asset_names = [binding["ue_asset_name"] for binding in mesh_bindings]
        if len(ue_asset_names) != len(set(ue_asset_names)):
            fail(
                "VISTA_BLENDER_UE_MANIFEST_INVALID",
                "Generated mesh names collide after the pinned UE Interchange name conversion",
                pointer=f"manifest/assets/{asset_id}/mesh_names",
            )
        required_assets.append(
            {
                "asset_id": asset_id,
                "collection": expected["collection"],
                "mesh_prefix": expected["mesh_prefix"],
                "mesh_names": sorted(mesh_names),
                "mesh_bindings": mesh_bindings,
                "material_names": sorted(set(material_names)),
                "origin_m": origin_m,
                "ue_placement": {"location_cm": location_cm, "rotation_deg": rotation_deg, "scale": scale},
            }
        )

    selected = next((candidate for candidate in candidates if candidate["format"] == import_format), None)
    if selected is None:
        fail("VISTA_BLENDER_UE_IMPORT_FORMAT_UNAVAILABLE", "Requested import format is not pinned by the Blender manifest")
    return {
        "path": str(manifest_path),
        "sha256": sha256_bytes(raw),
        "schema": BLENDER_MANIFEST_SCHEMA,
        "selected_import_source": selected,
        "available_import_sources": candidates,
        "required_assets": required_assets,
    }


def validate_engine(editor_cmd: Path, pins: EnginePins) -> dict[str, Any]:
    editor_cmd = _canonical_existing(
        _absolute_lexical(editor_cmd, code="VISTA_BLENDER_UE_ENGINE_INVALID", pointer="--unreal-editor-cmd"),
        kind="file",
        code="VISTA_BLENDER_UE_ENGINE_INVALID",
        pointer="--unreal-editor-cmd",
    )
    if editor_cmd.name != "UnrealEditor-Cmd" or not os.access(editor_cmd, os.X_OK):
        fail("VISTA_BLENDER_UE_ENGINE_INVALID", "Expected an executable UnrealEditor-Cmd", pointer="--unreal-editor-cmd")
    if sha256_file(editor_cmd) != pins.editor_sha256:
        fail("VISTA_BLENDER_UE_ENGINE_PIN_MISMATCH", "UnrealEditor-Cmd differs from the approved UE 5.3.2 binary pin")
    engine_root = editor_cmd.parents[2]
    version_path = _canonical_existing(
        engine_root / "Binaries/Linux/UnrealEditor.version",
        kind="file",
        code="VISTA_BLENDER_UE_ENGINE_INVALID",
        pointer="engine/version",
    )
    version, version_raw = load_json(version_path, code="VISTA_BLENDER_UE_ENGINE_INVALID")
    expected_version = {
        "MajorVersion": pins.major,
        "MinorVersion": pins.minor,
        "PatchVersion": pins.patch,
        "Changelist": pins.changelist,
        "BranchName": pins.branch_name,
    }
    if any(version.get(key) != expected for key, expected in expected_version.items()):
        fail("VISTA_BLENDER_UE_ENGINE_PIN_MISMATCH", "Engine version metadata differs from exact UE 5.3.2 pins")
    translator = _canonical_existing(
        engine_root / "Plugins/Interchange/Runtime/Binaries/Linux/libUnrealEditor-InterchangeImport.so",
        kind="file",
        code="VISTA_BLENDER_UE_ENGINE_INVALID",
        pointer="engine/interchange_translator",
    )
    if sha256_file(translator) != pins.translator_sha256:
        fail("VISTA_BLENDER_UE_ENGINE_PIN_MISMATCH", "Interchange translator differs from the approved binary pin")
    translator_raw = translator.read_bytes()
    glb_marker = "glb;GL Transmission Format (Binary)".encode("utf-16le")
    gltf_marker = "gltf;GL Transmission Format".encode("utf-16le")
    if glb_marker not in translator_raw or gltf_marker not in translator_raw:
        fail("VISTA_BLENDER_UE_GLTF_TRANSLATOR_UNAVAILABLE", "Pinned Interchange binary does not advertise both GLB and glTF")
    return {
        "editor_cmd": str(editor_cmd),
        "editor_sha256": pins.editor_sha256,
        "version_file": str(version_path),
        "version_file_sha256": sha256_bytes(version_raw),
        "version": expected_version,
        "translator": str(translator),
        "translator_sha256": pins.translator_sha256,
        "static_format_capability": {
            "glb": "advertised_by_pinned_packaged_translator",
            "gltf": "advertised_by_pinned_packaged_translator",
            "live_glb_commandlet_observation": "pending",
        },
    }


def _commandlet_scripts() -> dict[str, dict[str, str]]:
    root = Path(__file__).resolve().parent
    result: dict[str, dict[str, str]] = {}
    for phase, name in (
        ("import", "import_generated_asset_commandlet.py"),
        ("compose", "compose_mmg040_scene_commandlet.py"),
    ):
        path = _canonical_existing(root / name, kind="file", code="VISTA_BLENDER_UE_SCRIPT_INVALID", pointer=f"scripts/{phase}")
        result[phase] = {"path": str(path), "sha256": sha256_file(path)}
    return result


def build_plan(
    *,
    source_project: Path,
    run_root: Path,
    attempt_root: Path,
    blender_manifest: Path,
    unreal_editor_cmd: Path,
    import_format: str = "glb",
    source_pins: SourcePins = SourcePins(),
    engine_pins: EnginePins = EnginePins(),
) -> PreparedPlan:
    project_file, source_map_file, source_snapshot = validate_source_project(source_project, source_pins)
    run_root, attempt_root = validate_attempt_root(run_root, attempt_root, source_project)
    blender = validate_blender_manifest(blender_manifest, run_root, import_format=import_format)
    engine = validate_engine(unreal_editor_cmd, engine_pins)
    scripts = _commandlet_scripts()

    destination_project = attempt_root / "project" / source_project.name
    destination_uproject = destination_project / PROJECT_FILE_NAME
    import_receipt = attempt_root / "import-receipt.json"
    scene_receipt = attempt_root / "scene-receipt.json"
    plan_path = attempt_root / "preparation-plan.json"
    common_argv = [
        str(unreal_editor_cmd),
        str(destination_uproject),
        "-run=pythonscript",
        "-unattended",
        "-nullrhi",
        "-nosplash",
        "-nosound",
        "-NoSourceControl",
        "-stdout",
        "-FullStdOutLogOutput",
    ]
    plan = {
        "schema": PLAN_SCHEMA,
        "status": "prepared_not_executed",
        "policy": {
            "append_only": True,
            "unreal_started": False,
            "content_imported": False,
            "scene_composed": False,
            "network_listener_allowed": False,
            "runtime_loopback_listener_allowed": True,
            "studio_socket_fallback_allowed": False,
            "canonical_or_archive_destination_allowed": False,
            "r8_allowed": False,
        },
        "source": {
            "revision": SOURCE_CONTAINER_NAME,
            "project_root": str(source_project),
            "project_file": str(project_file),
            "project_sha256": source_pins.project_sha256,
            "source_map": SOURCE_MAP,
            "source_map_file": str(source_map_file),
            "source_map_sha256": source_pins.map_sha256,
            "snapshot": {
                "algorithm": "sha256(sorted path\\0mode\\0size\\0sha256 records)",
                "excluded_top_level": sorted(COPY_EXCLUDED_TOP_LEVEL),
                "tree_sha256": source_snapshot.tree_sha256,
                "file_count": source_snapshot.file_count,
                "total_bytes": source_snapshot.total_bytes,
                "external_symlink_count": source_snapshot.external_symlink_count,
                "external_symlink_sha256": source_snapshot.external_symlink_sha256,
                "external_content_root": source_pins.external_content_root,
                "external_dependency_policy": "exact symlink topology and file targets bound; linked directory contents remain external runtime dependencies",
            },
        },
        "destination": {
            "run_root": str(run_root),
            "attempt_root": str(attempt_root),
            "project_root": str(destination_project),
            "project_file": str(destination_uproject),
            "plan_path": str(plan_path),
            "import_receipt": str(import_receipt),
            "scene_receipt": str(scene_receipt),
        },
        "blender": blender,
        "unreal": {
            "engine": engine,
            "route": "UnrealEditor-Cmd -run=pythonscript",
            "task_settings": {
                "automated": True,
                "async": False,
                "replace_existing": False,
                "replace_existing_settings": False,
                "save": True,
            },
            "import_content_root": IMPORT_CONTENT_ROOT,
            "source_map": SOURCE_MAP,
            "output_map": OUTPUT_MAP,
            "scripts": scripts,
            "phases": ["import", "compose"],
            "commands": {
                "import": {
                    "argv": common_argv
                    + [
                        "-script=" + scripts["import"]["path"],
                        "-UserDir=" + str(attempt_root / "ue-user" / "import"),
                        "-LocalDataCachePath=" + str(attempt_root / "ddc"),
                    ],
                    "required_environment": [
                        "VISTA_BLENDER_UE_PLAN",
                        "VISTA_BLENDER_UE_PLAN_SHA256",
                    ],
                },
                "compose": {
                    "argv": common_argv
                    + [
                        "-script=" + scripts["compose"]["path"],
                        "-UserDir=" + str(attempt_root / "ue-user" / "compose"),
                        "-LocalDataCachePath=" + str(attempt_root / "ddc"),
                    ],
                    "required_environment": [
                        "VISTA_BLENDER_UE_PLAN",
                        "VISTA_BLENDER_UE_PLAN_SHA256",
                        "VISTA_BLENDER_UE_IMPORT_RECEIPT_SHA256",
                    ],
                },
            },
            "execution_gate": {
                "static_glb_translator_capability": "passed",
                "prior_json_gltf_commandlet_observation": "passed_external_evidence",
                "live_glb_commandlet_import": "pending",
                "rendered_review": "pending",
                "production_ready": False,
            },
        },
    }
    plan_bytes = canonical_json(plan)
    return PreparedPlan(plan=plan, plan_bytes=plan_bytes, plan_sha256=sha256_bytes(plan_bytes), source_snapshot=source_snapshot)


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
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rename_no_replace(source: Path, destination: Path) -> None:
    renameat2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if renameat2 is None:
        fail("VISTA_BLENDER_UE_ATOMIC_PUBLISH_UNAVAILABLE", "renameat2(RENAME_NOREPLACE) is unavailable")
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1)
    if result == 0:
        return
    number = ctypes.get_errno()
    if number == errno.EEXIST:
        fail("VISTA_BLENDER_UE_DESTINATION_EXISTS", "Attempt publication is non-overwriting")
    if number in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP}:
        fail("VISTA_BLENDER_UE_ATOMIC_PUBLISH_UNAVAILABLE", "Filesystem cannot publish with RENAME_NOREPLACE")
    raise OSError(number, os.strerror(number), str(destination))


def _approval_digest(reference: str) -> str:
    if not APPROVAL_RE.fullmatch(reference) or any(word in reference.casefold() for word in ("secret", "token", "password", "bearer")):
        fail("VISTA_BLENDER_UE_APPROVAL_INVALID", "Apply requires a non-secret change identifier")
    return hashlib.sha256(("vista-blender-ue-approval-v1\0" + reference).encode("utf-8")).hexdigest()


def materialize_fresh_project(prepared: PreparedPlan, *, approval_ref: str) -> dict[str, Any]:
    plan = prepared.plan
    source_project = Path(plan["source"]["project_root"])
    run_root = Path(plan["destination"]["run_root"])
    attempt_root = Path(plan["destination"]["attempt_root"])
    validate_attempt_root(run_root, attempt_root, source_project)
    approval_sha256 = _approval_digest(approval_ref)

    external_content_root = Path(plan["source"]["snapshot"]["external_content_root"])
    before = snapshot_project(source_project, external_content_root=external_content_root)
    if before != prepared.source_snapshot:
        fail("VISTA_BLENDER_UE_SOURCE_CHANGED", "Pinned source changed after plan preparation")

    ue_parent = attempt_root.parent
    if not ue_parent.exists():
        ue_parent.mkdir(mode=0o700, parents=True)
    _reject_symlink_components(ue_parent, code="VISTA_BLENDER_UE_DESTINATION_INVALID", pointer="attempt parent")
    temporary: Path | None = Path(tempfile.mkdtemp(prefix=f".{attempt_root.name}.tmp-", dir=ue_parent))
    try:
        os.chmod(temporary, 0o700)
        destination_project = temporary / "project" / source_project.name
        destination_project.parent.mkdir(mode=0o700)
        def ignore_source_root(directory: str, names: list[str]) -> set[str]:
            if Path(directory) == source_project:
                return set(names).intersection(COPY_EXCLUDED_TOP_LEVEL)
            return set()

        shutil.copytree(
            source_project,
            destination_project,
            copy_function=shutil.copy2,
            ignore=ignore_source_root,
            symlinks=True,
        )
        copied = snapshot_project(destination_project, external_content_root=external_content_root)
        after = snapshot_project(source_project, external_content_root=external_content_root)
        if before != after or copied != before:
            fail("VISTA_BLENDER_UE_SOURCE_CHANGED", "Source or copied project snapshot changed during preparation")

        _write_private(temporary / "preparation-plan.json", prepared.plan_bytes)
        receipt = {
            "schema": PREPARATION_RECEIPT_SCHEMA,
            "status": "fresh_project_prepared",
            "prepared_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "approval_ref_sha256": approval_sha256,
            "plan_sha256": prepared.plan_sha256,
            "source_tree_sha256_before": before.tree_sha256,
            "source_tree_sha256_after": after.tree_sha256,
            "destination_initial_tree_sha256": copied.tree_sha256,
            "source_project_sha256": plan["source"]["project_sha256"],
            "source_map_sha256": plan["source"]["source_map_sha256"],
            "unreal_started": False,
            "content_imported": False,
            "scene_composed": False,
            "network_used": False,
            "publication_policy": "atomic_non_overwriting",
        }
        _write_private(temporary / "preparation-receipt.json", canonical_json(receipt))
        _fsync_directory(temporary)
        _rename_no_replace(temporary, attempt_root)
        temporary = None
        _fsync_directory(ue_parent)
        return receipt
    finally:
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)


def result(prepared: PreparedPlan, status: str) -> dict[str, Any]:
    plan = prepared.plan
    return {
        "schema": RESULT_SCHEMA,
        "valid": True,
        "status": status,
        "plan_sha256": prepared.plan_sha256,
        "attempt_root": plan["destination"]["attempt_root"],
        "selected_import_format": plan["blender"]["selected_import_source"]["format"],
        "static_glb_translator_capability": plan["unreal"]["engine"]["static_format_capability"]["glb"],
        "live_glb_commandlet_observation": "pending",
        "unreal_started": False,
        "content_imported": False,
        "scene_composed": False,
    }


class SafeParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        fail("VISTA_BLENDER_UE_ARGUMENT_INVALID", "CLI arguments are missing or invalid; consult --help")


def build_parser() -> argparse.ArgumentParser:
    parser = SafeParser(description=__doc__)
    parser.add_argument("--source-project", required=True)
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--attempt-root", required=True)
    parser.add_argument("--blender-manifest", required=True)
    parser.add_argument("--unreal-editor-cmd", required=True)
    parser.add_argument("--import-format", choices=("glb", "gltf"), default="glb")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approval-ref", default="")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    os.umask(0o077)
    try:
        arguments = build_parser().parse_args(argv)
        if arguments.apply and not arguments.approval_ref:
            fail("VISTA_BLENDER_UE_ARGUMENT_INVALID", "--apply requires --approval-ref")
        if not arguments.apply and arguments.approval_ref:
            fail("VISTA_BLENDER_UE_ARGUMENT_INVALID", "--approval-ref is accepted only with --apply")
        prepared = build_plan(
            source_project=Path(arguments.source_project),
            run_root=Path(arguments.run_root),
            attempt_root=Path(arguments.attempt_root),
            blender_manifest=Path(arguments.blender_manifest),
            unreal_editor_cmd=Path(arguments.unreal_editor_cmd),
            import_format=arguments.import_format,
        )
        if arguments.apply:
            materialize_fresh_project(prepared, approval_ref=arguments.approval_ref)
            status = "fresh_project_prepared"
        else:
            status = "dry_run"
        print(canonical_json(result(prepared, status)).decode("utf-8"), end="")
        return 0
    except VistaBlenderUEContractError as error:
        print(
            canonical_json({"schema": RESULT_SCHEMA, "valid": False, "status": "failed", "error": error.public_dict()}).decode("utf-8"),
            end="",
            file=sys.stderr,
        )
        return 2
    except Exception:
        print(
            canonical_json(
                {
                    "schema": RESULT_SCHEMA,
                    "valid": False,
                    "status": "failed",
                    "error": {
                        "code": "VISTA_BLENDER_UE_INTERNAL_ERROR",
                        "message": "Preparation failed before a safe result was produced",
                    },
                }
            ).decode("utf-8"),
            end="",
            file=sys.stderr,
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
