#!/usr/bin/env python3
"""Build one append-only VISTA Playable Home UE project from pinned inputs.

The default CLI mode is a zero-write dry run.  It validates every source pin,
compiles the exact UE execution manifest, and prints both fixed commandlet
commands.  ``--apply`` materializes a fresh content-only project, copies the
compiled plugin and Manny content, then runs import followed by composition.

This host-side tool never accepts caller-authored Unreal Python.  The two
commandlet paths are fixed beside this file and are byte-pinned in the
execution manifest produced by :mod:`contract`.
"""

from __future__ import annotations

import argparse
import copy
import errno
import fcntl
import hashlib
import json
import math
import os
import pathlib
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


Path = pathlib.Path
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.blender.vista_playable_home import contract_scene as blender_contract  # noqa: E402
from tools.blender.vista_playable_home_hssd import planner as hssd_contract  # noqa: E402
from tools.ue.vista_playable_home import contract, planning  # noqa: E402
from tools.ue.vista_playable_home.commandlet_common import (  # noqa: E402
    IMPORT_MARKER,
    IMPORT_RECEIPT_SCHEMA,
    SCENE_MARKER,
    SCENE_RECEIPT_SCHEMA,
    derived_asset_path,
)
from tools.worlds import playable_home as world_contract  # noqa: E402


ORCHESTRATOR_PLAN_SCHEMA = "simworld.vista.playable-home-ue-build-plan/v1"
PREPARATION_RECEIPT_SCHEMA = "simworld.vista.playable-home-ue-preparation-receipt/v1"
RESULT_RECEIPT_SCHEMA = "simworld.vista.playable-home-ue-build-result/v1"
POINTER_SCHEMA = "simworld.vista.playable-home-ue-build-pointer/v1"
ATTEMPT_OWNER_SCHEMA = "simworld.vista.playable-home-ue-attempt-owner/v1"
BLENDER_BUILD_RECEIPT_SCHEMA = "simworld.vista.playable-home-blender-build-receipt/v1"
HSSD_MANIFEST_SCHEMA = "simworld.vista.playable-home-hssd-attribution/v1"
HSSD_BINDING_PLAN_SCHEMA = "simworld.vista.playable-home-hssd-binding-plan/v1"
EXPECTED_REVISION = "vista_playable_home_r1"
EXPECTED_PROJECT_NAME = "VistaPlayableHome.uproject"
EXPECTED_PLUGIN_NAME = "VistaPlayableHome"
MAX_JSON_BYTES = 64 * 1024 * 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ATTEMPT_RE = re.compile(r"^attempt-[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
FICLONE = 0x40049409
FORBIDDEN_ATTEMPT_PARTS = frozenset(
    {"archive", "archives", "canonical", "production", "release", "releases", "r8", "disposable-project-r8"}
)
PROJECT_PLUGINS = (
    "VistaPlayableHome",
    "PythonScriptPlugin",
    "EditorScriptingUtilities",
    "Interchange",
)
PLUGIN_REQUIRED_FILES = (
    "VistaPlayableHome.uplugin",
    "Binaries/Linux/libUnrealEditor-VistaPlayableHome.so",
    "Binaries/Linux/UnrealEditor.modules",
    "Config/DefaultVistaPlayableHome.ini",
    "README.md",
)
MANNY_REQUIRED_FILES = (
    "Mannequins/Meshes/SKM_Manny.uasset",
    "Mannequins/Animations/ABP_Manny.uasset",
)


class BuildHomeError(RuntimeError):
    """Stable fail-closed error raised before unsafe or ambiguous work."""

    def __init__(self, code: str, detail: str, *, pointer: str | None = None) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.pointer = pointer

    def public_dict(self) -> dict[str, str]:
        value = {"code": self.code, "message": self.detail}
        if self.pointer:
            value["pointer"] = self.pointer
        return value


def _fail(code: str, detail: str, *, pointer: str | None = None) -> None:
    raise BuildHomeError(code, detail, pointer=pointer)


def canonical_json(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        _fail("VISTA_HOME_BUILD_JSON_INVALID", "value is not finite canonical JSON")
        raise AssertionError from exc


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_sha(value: str | None, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        _fail("VISTA_HOME_BUILD_PIN_INVALID", f"{label} must be a lowercase SHA-256 digest")
    return value


def _duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            _fail("VISTA_HOME_BUILD_JSON_DUPLICATE_KEY", "JSON contains a duplicate object key")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    _fail("VISTA_HOME_BUILD_JSON_NON_FINITE", f"JSON constant {value!r} is forbidden")


def _assert_finite(value: Any, pointer: str = "$", depth: int = 0) -> None:
    if depth > 96:
        _fail("VISTA_HOME_BUILD_JSON_INVALID", "JSON nesting exceeds the safety limit", pointer=pointer)
    if isinstance(value, float) and not math.isfinite(value):
        _fail("VISTA_HOME_BUILD_JSON_NON_FINITE", "JSON contains a non-finite number", pointer=pointer)
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                _fail("VISTA_HOME_BUILD_JSON_INVALID", "JSON object keys must be strings", pointer=pointer)
            _assert_finite(child, f"{pointer}.{key}", depth + 1)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_finite(child, f"{pointer}[{index}]", depth + 1)


def _load_json(path: Path, *, expected_sha256: str, label: str) -> tuple[dict[str, Any], bytes]:
    source = _existing_file(path, label)
    expected = _require_sha(expected_sha256, f"{label} pin")
    try:
        size = source.stat().st_size
    except OSError as exc:
        _fail("VISTA_HOME_BUILD_INPUT_UNREADABLE", f"{label} cannot be read", pointer=str(source))
        raise AssertionError from exc
    if size <= 0 or size > MAX_JSON_BYTES:
        _fail("VISTA_HOME_BUILD_JSON_INVALID", f"{label} size is outside the safety bound", pointer=str(source))
    raw = source.read_bytes()
    if sha256_bytes(raw) != expected:
        _fail("VISTA_HOME_BUILD_PIN_MISMATCH", f"{label} SHA-256 differs", pointer=str(source))
    try:
        value = json.loads(
            raw.decode("utf-8", "strict"),
            object_pairs_hook=_duplicate_object,
            parse_constant=_reject_constant,
        )
    except BuildHomeError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        _fail("VISTA_HOME_BUILD_JSON_INVALID", f"{label} is not strict UTF-8 JSON", pointer=str(source))
        raise AssertionError from exc
    if not isinstance(value, dict):
        _fail("VISTA_HOME_BUILD_JSON_INVALID", f"{label} root must be an object", pointer=str(source))
    _assert_finite(value)
    return value, raw


def _absolute_lexical(path: Path, label: str) -> Path:
    candidate = Path(path).expanduser()
    value = str(candidate)
    if not candidate.is_absolute() or os.path.normpath(value) != value:
        _fail("VISTA_HOME_BUILD_PATH_INVALID", f"{label} must be absolute and normalized", pointer=value)
    return candidate


def _reject_symlink_components(path: Path, label: str, *, allow_missing_tail: bool = False) -> None:
    candidate = _absolute_lexical(path, label)
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current = current / part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            if allow_missing_tail:
                return
            _fail("VISTA_HOME_BUILD_PATH_MISSING", f"{label} does not exist", pointer=str(candidate))
        if stat.S_ISLNK(metadata.st_mode):
            _fail("VISTA_HOME_BUILD_SYMLINK_REJECTED", f"{label} contains a symlink component", pointer=str(current))


def _existing_file(path: Path, label: str) -> Path:
    candidate = _absolute_lexical(path, label)
    _reject_symlink_components(candidate, label)
    try:
        metadata = os.lstat(candidate)
    except OSError as exc:
        _fail("VISTA_HOME_BUILD_PATH_MISSING", f"{label} is missing", pointer=str(candidate))
        raise AssertionError from exc
    if not stat.S_ISREG(metadata.st_mode):
        _fail("VISTA_HOME_BUILD_PATH_INVALID", f"{label} must be a regular file", pointer=str(candidate))
    if candidate.resolve(strict=True) != candidate:
        _fail("VISTA_HOME_BUILD_PATH_INVALID", f"{label} must already be canonical", pointer=str(candidate))
    return candidate


def _existing_directory(path: Path, label: str) -> Path:
    candidate = _absolute_lexical(path, label)
    _reject_symlink_components(candidate, label)
    try:
        metadata = os.lstat(candidate)
    except OSError as exc:
        _fail("VISTA_HOME_BUILD_PATH_MISSING", f"{label} is missing", pointer=str(candidate))
        raise AssertionError from exc
    if not stat.S_ISDIR(metadata.st_mode):
        _fail("VISTA_HOME_BUILD_PATH_INVALID", f"{label} must be a directory", pointer=str(candidate))
    if candidate.resolve(strict=True) != candidate:
        _fail("VISTA_HOME_BUILD_PATH_INVALID", f"{label} must already be canonical", pointer=str(candidate))
    return candidate


def _safe_relative_path(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        _fail("VISTA_HOME_BUILD_PATH_INVALID", f"{label} must be a non-empty POSIX relative path")
    pure = pathlib.PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        _fail("VISTA_HOME_BUILD_PATH_INVALID", f"{label} contains traversal or is absolute")
    return Path(*pure.parts)


def _contained_artifact(root: Path, relative_value: Any, label: str) -> Path:
    relative = _safe_relative_path(relative_value, label)
    candidate = root / relative
    source = _existing_file(candidate, label)
    try:
        source.relative_to(root)
    except ValueError:
        _fail("VISTA_HOME_BUILD_PATH_ESCAPE", f"{label} escapes its manifest root", pointer=str(source))
    return source


@dataclass(frozen=True)
class TreeSnapshot:
    sha256: str
    file_count: int
    total_bytes: int
    records: tuple[tuple[str, int, int, str], ...]


def snapshot_tree(root: Path, label: str) -> TreeSnapshot:
    directory = _existing_directory(root, label)
    records: list[tuple[str, int, int, str]] = []
    for current, directories, files in os.walk(directory, topdown=True, followlinks=False):
        current_path = Path(current)
        directories.sort()
        files.sort()
        for name in directories:
            child = current_path / name
            metadata = os.lstat(child)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                _fail("VISTA_HOME_BUILD_TREE_UNSAFE", f"{label} contains an unsafe directory", pointer=str(child))
        for name in files:
            child = current_path / name
            metadata = os.lstat(child)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                _fail("VISTA_HOME_BUILD_TREE_UNSAFE", f"{label} contains a non-regular file", pointer=str(child))
            records.append(
                (
                    child.relative_to(directory).as_posix(),
                    stat.S_IMODE(metadata.st_mode),
                    metadata.st_size,
                    sha256_file(child),
                )
            )
    if not records:
        _fail("VISTA_HOME_BUILD_TREE_EMPTY", f"{label} contains no files", pointer=str(directory))
    raw = b"".join(
        f"{relative}\0{mode:o}\0{size}\0{digest}\n".encode("utf-8")
        for relative, mode, size, digest in records
    )
    return TreeSnapshot(
        sha256=sha256_bytes(raw),
        file_count=len(records),
        total_bytes=sum(record[2] for record in records),
        records=tuple(records),
    )


def _validate_tree_pin(root: Path, expected_sha256: str, label: str) -> TreeSnapshot:
    expected = _require_sha(expected_sha256, f"{label} tree pin")
    snapshot = snapshot_tree(root, label)
    if snapshot.sha256 != expected:
        _fail("VISTA_HOME_BUILD_PIN_MISMATCH", f"{label} tree SHA-256 differs", pointer=str(root))
    return snapshot


def _validate_output_entry(entry: Any, root: Path, label: str) -> Path:
    if not isinstance(entry, Mapping) or set(entry) != {"path", "sha256", "bytes", "media_type"}:
        _fail("VISTA_HOME_BUILD_MANIFEST_INVALID", f"{label} output fields differ")
    source = _contained_artifact(root, entry["path"], label)
    expected = _require_sha(entry.get("sha256"), f"{label} SHA-256")
    size = entry.get("bytes")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        _fail("VISTA_HOME_BUILD_MANIFEST_INVALID", f"{label} bytes is invalid")
    if source.stat().st_size != size or sha256_file(source) != expected:
        _fail("VISTA_HOME_BUILD_PIN_MISMATCH", f"{label} output bytes or SHA-256 differ", pointer=str(source))
    if not isinstance(entry.get("media_type"), str) or not entry["media_type"]:
        _fail("VISTA_HOME_BUILD_MANIFEST_INVALID", f"{label} media type is invalid")
    return source


@dataclass(frozen=True)
class BlenderInputs:
    manifest: dict[str, Any]
    normalized: dict[str, Any]
    artifacts: dict[str, tuple[Path, str]]


def validate_build_plan(path: Path, expected_sha256: str, expected_revision: str) -> dict[str, Any]:
    plan, raw = _load_json(path, expected_sha256=expected_sha256, label="build plan")
    if raw != planning.canonical_json(plan):
        _fail("VISTA_HOME_BUILD_PLAN_NONCANONICAL", "build plan bytes must be canonical", pointer=str(path))
    try:
        world_contract.validate_build_plan(plan)
        planning.build_composition_spec(plan)
    except (world_contract.PlayableHomeContractError, planning.VistaPlayableHomePlanError) as exc:
        _fail("VISTA_HOME_BUILD_PLAN_INVALID", str(exc), pointer=str(path))
    revision = plan.get("house", {}).get("revision")
    if revision != expected_revision:
        _fail("VISTA_HOME_BUILD_REVISION_MISMATCH", "build plan revision differs from the requested revision")
    expected_namespace = f"/Game/VISTA/PlayableHome/{expected_revision}"
    if plan["unreal"]["content_namespace"] != expected_namespace or plan["unreal"]["map_path"] != expected_namespace + "/Maps/VistaPlayableHome":
        _fail("VISTA_HOME_BUILD_REVISION_MISMATCH", "build plan namespace is not bound to the requested revision")
    return plan


def validate_blender_manifest(
    path: Path,
    expected_sha256: str,
    plan: Mapping[str, Any],
) -> BlenderInputs:
    manifest, raw = _load_json(path, expected_sha256=expected_sha256, label="Blender build manifest")
    if raw != blender_contract.canonical_json_bytes(manifest):
        _fail("VISTA_HOME_BUILD_MANIFEST_INVALID", "Blender build manifest is not canonical", pointer=str(path))
    expected_keys = {
        "schema_version",
        "house_id",
        "revision",
        "source_house_digest",
        "normalized_manifest_digest",
        "build",
        "outputs",
        "asset_artifacts",
    }
    if set(manifest) != expected_keys or manifest.get("schema_version") != BLENDER_BUILD_RECEIPT_SCHEMA:
        _fail("VISTA_HOME_BUILD_MANIFEST_INVALID", "Blender build manifest fields or schema differ")
    house = plan["house"]
    if (
        manifest.get("house_id") != house["house_id"]
        or manifest.get("revision") != house["revision"]
        or manifest.get("source_house_digest") != house["content_digest"]
    ):
        _fail("VISTA_HOME_BUILD_REVISION_MISMATCH", "Blender build manifest disagrees with the build plan")
    root = _existing_directory(path.parent, "Blender output root")
    outputs = manifest.get("outputs")
    expected_outputs = {"blend", "glb", "normalized_manifest", "preview_interior", "preview_overview"}
    if not isinstance(outputs, Mapping) or set(outputs) != expected_outputs:
        _fail("VISTA_HOME_BUILD_MANIFEST_INVALID", "Blender output inventory differs")
    output_paths = {
        name: _validate_output_entry(entry, root, f"Blender output {name}")
        for name, entry in outputs.items()
    }
    normalized_path = output_paths["normalized_manifest"]
    normalized, normalized_raw = _load_json(
        normalized_path,
        expected_sha256=outputs["normalized_manifest"]["sha256"],
        label="normalized Blender manifest",
    )
    if normalized_raw != blender_contract.canonical_json_bytes(normalized):
        _fail("VISTA_HOME_BUILD_MANIFEST_INVALID", "normalized Blender manifest is not canonical")
    normalized_body = copy.deepcopy(normalized)
    normalized_digest = normalized_body.pop("content_digest", None)
    if not isinstance(normalized_digest, str) or normalized_digest != sha256_bytes(
        blender_contract.canonical_json_bytes(normalized_body)
    ):
        _fail("VISTA_HOME_BUILD_MANIFEST_INVALID", "normalized Blender content digest differs")
    if (
        normalized.get("schema_version") != blender_contract.MANIFEST_SCHEMA
        or normalized.get("house_id") != house["house_id"]
        or normalized.get("revision") != house["revision"]
        or normalized.get("source_house")
        != {"schema_version": world_contract.HOUSE_SCHEMA_VERSION, "content_digest": house["content_digest"]}
        or manifest.get("normalized_manifest_digest") != normalized_digest
    ):
        _fail("VISTA_HOME_BUILD_REVISION_MISMATCH", "normalized Blender manifest disagrees with the build plan")

    plan_entities = {item["entity_id"]: item for item in plan["entities"]}
    normalized_entities = normalized.get("entities")
    if not isinstance(normalized_entities, list) or {item.get("entity_id") for item in normalized_entities if isinstance(item, Mapping)} != set(plan_entities):
        _fail("VISTA_HOME_BUILD_MANIFEST_INVALID", "normalized semantic entity inventory differs")
    for item in normalized_entities:
        source = plan_entities[item["entity_id"]]
        if item.get("asset_ref") != source["asset"]["asset_id"] or item.get("room_id") != source["room_id"]:
            _fail("VISTA_HOME_BUILD_MANIFEST_INVALID", "normalized semantic binding differs from build plan")

    declared = {asset["asset_id"]: asset for asset in plan["assets"]}
    expected_nonbuiltin = {asset_id for asset_id, asset in declared.items() if asset["source_kind"] != "builtin"}
    entries = manifest.get("asset_artifacts")
    if not isinstance(entries, Mapping) or set(entries) != expected_nonbuiltin:
        _fail("VISTA_HOME_BUILD_BINDING_INCOMPLETE", "Blender artifact IDs are not the exact non-builtin set")
    artifacts: dict[str, tuple[Path, str]] = {}
    artifact_keys = {"path", "sha256", "bytes", "media_type", "mesh_count", "source_node_ids"}
    for asset_id in sorted(expected_nonbuiltin):
        entry = entries[asset_id]
        if not isinstance(entry, Mapping) or set(entry) != artifact_keys:
            _fail("VISTA_HOME_BUILD_MANIFEST_INVALID", f"Blender artifact {asset_id} fields differ")
        if entry.get("media_type") != "model/gltf-binary" or entry.get("mesh_count") != 1:
            _fail("VISTA_HOME_BUILD_MANIFEST_INVALID", f"Blender artifact {asset_id} is not one GLB mesh")
        node_ids = entry.get("source_node_ids")
        if not isinstance(node_ids, list) or not node_ids or len(node_ids) != len(set(node_ids)) or not all(isinstance(item, str) and item for item in node_ids):
            _fail("VISTA_HOME_BUILD_MANIFEST_INVALID", f"Blender artifact {asset_id} source nodes are invalid")
        source = _contained_artifact(root, entry["path"], f"Blender artifact {asset_id}")
        expected = _require_sha(entry.get("sha256"), f"Blender artifact {asset_id} SHA-256")
        size = entry.get("bytes")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            _fail("VISTA_HOME_BUILD_MANIFEST_INVALID", f"Blender artifact {asset_id} bytes is invalid")
        if source.stat().st_size != size or sha256_file(source) != expected:
            _fail("VISTA_HOME_BUILD_PIN_MISMATCH", f"Blender artifact {asset_id} bytes or SHA-256 differ")
        artifacts[asset_id] = (source, expected)
    return BlenderInputs(manifest=manifest, normalized=normalized, artifacts=artifacts)


def _content_digest(value: Mapping[str, Any]) -> str:
    body = copy.deepcopy(dict(value))
    body.pop("content_digest", None)
    return sha256_bytes(canonical_json(body))


def validate_visual_binding_manifest(
    path: Path,
    expected_sha256: str,
    plan: Mapping[str, Any],
    normalized_manifest_digest: str,
) -> dict[str, tuple[Path, str]]:
    visual, raw = _load_json(path, expected_sha256=expected_sha256, label="visual binding manifest")
    try:
        hssd_contract.validate_built_manifest(visual)
    except hssd_contract.HssdBindingError as exc:
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", str(exc), pointer=str(path))
    if raw != hssd_contract.canonical_json_bytes(visual):
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual binding manifest is not canonical", pointer=str(path))
    expected_top = {
        "schema_version",
        "house_id",
        "revision",
        "source_plan",
        "dataset",
        "license_receipt",
        "blender",
        "closed_world",
        "outputs",
        "content_digest",
    }
    if set(visual) != expected_top or visual.get("schema_version") != HSSD_MANIFEST_SCHEMA:
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual binding schema or fields differ")
    if visual.get("content_digest") != hssd_contract.content_digest(visual):
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual binding content digest differs")
    if visual.get("house_id") != plan["house"]["house_id"] or visual.get("revision") != plan["house"]["revision"]:
        _fail("VISTA_HOME_BUILD_REVISION_MISMATCH", "visual binding house or revision differs")
    source_plan = visual.get("source_plan")
    if not isinstance(source_plan, Mapping) or set(source_plan) != {"schema_version", "content_digest", "path"}:
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual source plan binding fields differ")
    if source_plan.get("schema_version") != HSSD_BINDING_PLAN_SCHEMA:
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual source plan schema differs")
    source_plan_digest = _require_sha(source_plan.get("content_digest"), "visual source plan content digest")
    root = _existing_directory(path.parent, "visual output root")
    binding_plan_path = _contained_artifact(root, source_plan.get("path"), "visual binding plan")
    binding_plan, binding_raw = _load_json(
        binding_plan_path,
        expected_sha256=sha256_file(binding_plan_path),
        label="visual binding plan",
    )
    try:
        hssd_contract.validate_binding_plan(binding_plan)
    except hssd_contract.HssdBindingError as exc:
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", str(exc), pointer=str(binding_plan_path))
    if (
        binding_raw != hssd_contract.canonical_json_bytes(binding_plan)
        or binding_plan.get("content_digest") != hssd_contract.content_digest(binding_plan)
    ):
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual binding plan is noncanonical or has digest drift")
    binding_plan_keys = {
        "schema_version",
        "house_id",
        "revision",
        "source_normalized_manifest",
        "dataset",
        "license_receipt",
        "selection_policy",
        "mode",
        "closed_world",
        "bindings",
        "preserved_assets",
        "content_digest",
    }
    if set(binding_plan) != binding_plan_keys:
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual binding plan fields differ")
    if (
        binding_plan.get("schema_version") != HSSD_BINDING_PLAN_SCHEMA
        or binding_plan.get("content_digest") != source_plan_digest
        or binding_plan.get("house_id") != plan["house"]["house_id"]
        or binding_plan.get("revision") != plan["house"]["revision"]
        or binding_plan.get("source_normalized_manifest")
        != {
            "schema_version": blender_contract.MANIFEST_SCHEMA,
            "content_digest": normalized_manifest_digest,
        }
    ):
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual binding plan does not bind the normalized forge manifest")
    if binding_plan.get("mode") != "full" or binding_plan.get("closed_world", {}).get("unaccounted_asset_ids") != []:
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual binding plan is not a closed full plan")
    blender = visual.get("blender")
    if not isinstance(blender, Mapping) or blender.get("mode") != "full":
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "only a full visual build may override presentation assets")
    dataset = visual.get("dataset")
    if not isinstance(dataset, Mapping) or not isinstance(dataset.get("license"), Mapping):
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual dataset license is missing")
    license_value = dataset["license"]
    if (
        dataset.get("dataset") != hssd_contract.HSSD_DATASET_NAME
        or dataset.get("project_url") != hssd_contract.HSSD_PROJECT_URL
        or dataset.get("readme_relpath") != "README.md"
        or dataset.get("readme_sha256") != hssd_contract.PINNED_HSSD_README_SHA256
        or re.fullmatch(r"[0-9a-f]{40}", str(dataset.get("dataset_revision", ""))) is None
        or license_value.get("spdx") != "CC-BY-NC-4.0"
        or license_value.get("url") != hssd_contract.HSSD_LICENSE_URL
        or license_value.get("commercial_use") != "prohibited_without_separate_permission"
        or license_value.get("attribution_required") is not True
        or license_value.get("modification_notice_required") is not True
    ):
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual dataset license policy differs")
    license_receipt = visual.get("license_receipt")
    if (
        not isinstance(license_receipt, Mapping)
        or license_receipt.get("accepted_spdx") != "CC-BY-NC-4.0"
        or license_receipt.get("scope") != "research_and_noncommercial_demo_only"
        or license_receipt.get("commercial_release_gate") != "replace_assets_or_obtain_separate_permission"
        or not isinstance(license_receipt.get("attribution_notice"), str)
        or not license_receipt.get("attribution_notice")
    ):
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual attribution receipt is incomplete")
    if binding_plan.get("dataset") != dataset or binding_plan.get("license_receipt") != license_receipt:
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "built attribution differs from the pinned binding plan")
    closed = visual.get("closed_world")
    if not isinstance(closed, Mapping) or set(closed) != {"bound_asset_ids", "unaccounted_asset_ids"}:
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual closed-world fields differ")
    if closed.get("unaccounted_asset_ids") != []:
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual manifest has unaccounted asset IDs")
    bound_ids = closed.get("bound_asset_ids")
    if not isinstance(bound_ids, list) or len(bound_ids) != len(set(bound_ids)) or not all(isinstance(item, str) for item in bound_ids):
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual bound IDs are invalid or duplicated")
    outputs = visual.get("outputs")
    if not isinstance(outputs, list):
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual outputs must be an array")
    logical_ids = [item.get("logical_asset_id") for item in outputs if isinstance(item, Mapping)]
    if len(logical_ids) != len(outputs) or len(logical_ids) != len(set(logical_ids)) or set(logical_ids) != set(bound_ids):
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual output IDs disagree with the closed-world inventory")
    declared = {asset["asset_id"]: asset for asset in plan["assets"]}
    nonbuiltin = {asset_id for asset_id, asset in declared.items() if asset["source_kind"] != "builtin"}
    if not set(logical_ids).issubset(nonbuiltin):
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual output contains an unknown or builtin asset ID")
    plan_closed = binding_plan["closed_world"]
    required_closed_keys = {
        "target_asset_ids",
        "bound_asset_ids",
        "preserved_asset_ids",
        "unaccounted_asset_ids",
    }
    if not isinstance(plan_closed, Mapping) or set(plan_closed) != required_closed_keys:
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual binding plan closed-world fields differ")
    for key in ("target_asset_ids", "bound_asset_ids", "preserved_asset_ids", "unaccounted_asset_ids"):
        values = plan_closed.get(key)
        if not isinstance(values, list) or len(values) != len(set(values)) or not all(isinstance(item, str) for item in values):
            _fail("VISTA_HOME_BUILD_VISUAL_INVALID", f"visual binding plan {key} is invalid or duplicated")
    target_ids = set(plan_closed["target_asset_ids"])
    plan_bound_ids = set(plan_closed["bound_asset_ids"])
    preserved_ids = set(plan_closed["preserved_asset_ids"])
    if (
        not nonbuiltin.issubset(target_ids)
        or not target_ids.issubset(declared)
        or plan_bound_ids != set(logical_ids)
        or target_ids != plan_bound_ids | preserved_ids
        or plan_bound_ids & preserved_ids
        or plan_closed["unaccounted_asset_ids"] != []
    ):
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual binding plan does not close the non-builtin asset universe")
    binding_entries = binding_plan.get("bindings")
    preserved_entries = binding_plan.get("preserved_assets")
    if not isinstance(binding_entries, list) or not isinstance(preserved_entries, list):
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual binding or preserved arrays are invalid")
    binding_by_id: dict[str, Mapping[str, Any]] = {}
    for entry in binding_entries:
        asset_id = entry.get("logical_asset_id") if isinstance(entry, Mapping) else None
        if not isinstance(asset_id, str) or asset_id in binding_by_id:
            _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual binding IDs are invalid or duplicated")
        source = entry.get("source")
        if (
            not isinstance(source, Mapping)
            or source.get("license_spdx") != "CC-BY-NC-4.0"
            or source.get("license_url") != license_value.get("url")
            or SHA256_RE.fullmatch(str(source.get("render_asset_sha256", ""))) is None
        ):
            _fail("VISTA_HOME_BUILD_VISUAL_INVALID", f"visual binding {asset_id} lacks source attribution")
        binding_by_id[asset_id] = entry
    preserved_entry_ids = [entry.get("asset_id") for entry in preserved_entries if isinstance(entry, Mapping)]
    if set(binding_by_id) != plan_bound_ids or len(preserved_entry_ids) != len(preserved_entries) or set(preserved_entry_ids) != preserved_ids:
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual binding/preserved arrays disagree with closed-world indexes")
    result: dict[str, tuple[Path, str]] = {}
    required_output_keys = {
        "logical_asset_id",
        "semantic_category",
        "path",
        "sha256",
        "bytes",
        "media_type",
        "target_dimensions_m",
        "actual_dimensions_m",
        "normalization",
        "texture_transport",
        "texture_transport_receipt",
        "source",
        "inspection",
    }
    for index, output in enumerate(outputs):
        if set(output) != required_output_keys:
            _fail("VISTA_HOME_BUILD_VISUAL_INVALID", f"visual output {index} fields differ")
        asset_id = output["logical_asset_id"]
        source_contract = output.get("source")
        if (
            output.get("media_type") != "model/gltf-binary"
            or not isinstance(source_contract, Mapping)
            or source_contract.get("license_spdx") != "CC-BY-NC-4.0"
            or source_contract.get("license_url") != license_value.get("url")
            or SHA256_RE.fullmatch(str(source_contract.get("render_asset_sha256", ""))) is None
        ):
            _fail("VISTA_HOME_BUILD_VISUAL_INVALID", f"visual output {asset_id} provenance differs")
        pinned_source = binding_by_id[asset_id].get("source")
        if (
            not isinstance(pinned_source, Mapping)
            or source_contract.get("dataset") != pinned_source.get("dataset")
            or source_contract.get("object_id") != pinned_source.get("object_id")
            or source_contract.get("render_asset_sha256") != pinned_source.get("render_asset_sha256")
        ):
            _fail("VISTA_HOME_BUILD_VISUAL_INVALID", f"visual output {asset_id} source differs from the binding plan")
        inspection = output.get("inspection")
        if (
            not isinstance(inspection, Mapping)
            or inspection.get("mesh_count") != 1
            or inspection.get("material_count", 0) < 1
            or inspection.get("pbr_texture_slot_count", 0) < 1
            or inspection.get("base_normal_orm_texture_slot_count", 0) < 1
            or inspection.get("all_primitives_material_bound") != 1
        ):
            _fail("VISTA_HOME_BUILD_VISUAL_INVALID", f"visual output {asset_id} lost its PBR one-mesh contract")
        if inspection.get("basisu_required") == 1:
            transport = output.get("texture_transport_receipt")
            required_true = {
                "self_contained",
                "single_buffer",
                "single_mesh",
                "buffer_views_aligned_and_in_range",
                "primitive_material_indices_valid",
                "basisu_texture_sources_valid",
                "extension_declarations_complete",
            }
            if (
                output.get("texture_transport") != "KHR_texture_basisu_preserved"
                or not isinstance(transport, Mapping)
                or transport.get("blender_decoded_textures") is not False
                or not all(transport.get(key) is True for key in required_true)
                or not isinstance(transport.get("base_normal_orm_texture_slots"), Mapping)
                or not isinstance(transport.get("image_payloads"), list)
                or not transport.get("image_payloads")
            ):
                _fail("VISTA_HOME_BUILD_VISUAL_INVALID", f"visual output {asset_id} lacks a complete BasisU transport receipt")
            for payload in transport["image_payloads"]:
                if (
                    not isinstance(payload, Mapping)
                    or payload.get("match") is not True
                    or SHA256_RE.fullmatch(str(payload.get("source_sha256", ""))) is None
                    or payload.get("output_sha256") != payload.get("source_sha256")
                ):
                    _fail("VISTA_HOME_BUILD_VISUAL_INVALID", f"visual output {asset_id} BasisU payload differs")
        source = _contained_artifact(root, output["path"], f"visual output {asset_id}")
        expected = _require_sha(output.get("sha256"), f"visual output {asset_id} SHA-256")
        size = output.get("bytes")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            _fail("VISTA_HOME_BUILD_VISUAL_INVALID", f"visual output {asset_id} bytes is invalid")
        if source.stat().st_size != size or sha256_file(source) != expected:
            _fail("VISTA_HOME_BUILD_PIN_MISMATCH", f"visual output {asset_id} bytes or SHA-256 differ")
        try:
            observed_inspection = hssd_contract.inspect_glb(source.resolve(strict=True))
        except hssd_contract.HssdBindingError as exc:
            _fail("VISTA_HOME_BUILD_VISUAL_INVALID", f"visual output {asset_id} GLB inspection failed: {exc}")
        if observed_inspection != dict(inspection):
            _fail("VISTA_HOME_BUILD_VISUAL_INVALID", f"visual output {asset_id} inspection does not match its GLB bytes")
        result[asset_id] = (source, expected)
    return result


def build_artifact_bindings(
    plan: Mapping[str, Any],
    blender: BlenderInputs,
    visual_overrides: Mapping[str, tuple[Path, str]] | None = None,
) -> list[dict[str, Any]]:
    visual = dict(visual_overrides or {})
    declared = {asset["asset_id"]: asset for asset in plan["assets"]}
    if not set(visual).issubset(declared):
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual overrides contain an undeclared asset")
    bindings: list[dict[str, Any]] = []
    for asset_id in sorted(declared):
        asset = declared[asset_id]
        source_file: str | None
        source_sha: str | None
        if asset["source_kind"] == "builtin":
            source_file = None
            source_sha = None
        else:
            source, source_sha = visual.get(asset_id, blender.artifacts[asset_id])
            source_file = str(source)
        bindings.append(
            {
                "asset_id": asset_id,
                "source_file": source_file,
                "source_file_sha256": source_sha,
                "source_binding_digest": asset["source_digest"],
            }
        )
    return bindings


def _validate_plugin_package(path: Path, expected_tree_sha256: str) -> TreeSnapshot:
    root = _existing_directory(path, "compiled plugin package")
    for relative in PLUGIN_REQUIRED_FILES:
        _existing_file(root / relative, f"compiled plugin file {relative}")
    descriptor, _raw = _load_json(
        root / "VistaPlayableHome.uplugin",
        expected_sha256=sha256_file(root / "VistaPlayableHome.uplugin"),
        label="compiled plugin descriptor",
    )
    if descriptor.get("FriendlyName") != "VISTA Playable Home" or not any(
        isinstance(module, Mapping) and module.get("Name") == EXPECTED_PLUGIN_NAME and module.get("Type") == "Runtime"
        for module in descriptor.get("Modules", [])
    ):
        _fail("VISTA_HOME_BUILD_PLUGIN_INVALID", "compiled plugin descriptor does not declare the runtime module")
    binary = root / "Binaries/Linux/libUnrealEditor-VistaPlayableHome.so"
    if binary.stat().st_size <= 0:
        _fail("VISTA_HOME_BUILD_PLUGIN_INVALID", "compiled plugin binary is empty")
    return _validate_tree_pin(root, expected_tree_sha256, "compiled plugin package")


def _validate_characters_content(path: Path, expected_tree_sha256: str) -> TreeSnapshot:
    root = _existing_directory(path, "Characters content")
    for relative in MANNY_REQUIRED_FILES:
        _existing_file(root / relative, f"Manny content {relative}")
    return _validate_tree_pin(root, expected_tree_sha256, "Characters content")


def project_descriptor() -> dict[str, Any]:
    return {
        "FileVersion": 3,
        "EngineAssociation": "5.7",
        "Category": "Simulation",
        "Description": "Disposable VISTA Playable Home runtime project",
        "Plugins": [{"Name": name, "Enabled": True} for name in PROJECT_PLUGINS],
    }


def default_engine_ini(plan: Mapping[str, Any]) -> bytes:
    map_path = plan["unreal"]["map_path"]
    lines = [
        "[/Script/EngineSettings.GameMapsSettings]",
        f"GameDefaultMap={map_path}",
        f"EditorStartupMap={map_path}",
        "GlobalDefaultGameMode=/Script/VistaPlayableHome.VistaPlayableHomeGameMode",
        "",
    ]
    return "\n".join(lines).encode("utf-8")


def _fixed_command(editor: Path, project_file: Path, script: Path) -> list[str]:
    return [
        str(editor),
        str(project_file),
        "-run=pythonscript",
        f"-script={script}",
        "-nocrashreports",
        "-unattended",
        "-nop4",
        "-nosplash",
    ]


@dataclass(frozen=True)
class BuildConfig:
    run_root: Path
    attempt_root: Path
    build_plan: Path
    build_plan_sha256: str
    blender_manifest: Path
    blender_manifest_sha256: str
    plugin_package: Path
    plugin_package_tree_sha256: str
    characters_content: Path
    characters_content_tree_sha256: str
    unreal_editor_cmd: Path
    unreal_editor_cmd_sha256: str | None
    visual_binding_manifest: Path | None = None
    visual_binding_manifest_sha256: str | None = None
    expected_revision: str = EXPECTED_REVISION
    command_timeout_s: int = 3600


@dataclass(frozen=True)
class PlannedBuild:
    config: BuildConfig
    plan: dict[str, Any]
    blender: BlenderInputs
    bindings: list[dict[str, Any]]
    plugin_snapshot: TreeSnapshot
    characters_snapshot: TreeSnapshot
    project_raw: bytes
    engine_ini_raw: bytes
    execution: dict[str, Any]
    execution_raw: bytes
    execution_sha256: str
    dry_run_report: dict[str, Any]


def _validate_destination(config: BuildConfig) -> tuple[Path, Path]:
    run_root = _existing_directory(config.run_root, "append-only run root")
    attempt = _absolute_lexical(config.attempt_root, "attempt root")
    _reject_symlink_components(attempt, "attempt root", allow_missing_tail=True)
    expected_parent = run_root / "ue"
    if attempt.parent != expected_parent or ATTEMPT_RE.fullmatch(attempt.name) is None:
        _fail("VISTA_HOME_BUILD_ATTEMPT_INVALID", "attempt root must be a named direct child of <run-root>/ue")
    if any(part.casefold() in FORBIDDEN_ATTEMPT_PARTS for part in attempt.parts):
        _fail("VISTA_HOME_BUILD_ATTEMPT_INVALID", "attempt root uses a forbidden destination component")
    if attempt.exists():
        _fail("VISTA_HOME_BUILD_ATTEMPT_EXISTS", "append-only attempt root already exists", pointer=str(attempt))
    return run_root, attempt


def _validate_editor(config: BuildConfig, *, require_existing: bool) -> tuple[Path, str | None]:
    editor = _absolute_lexical(config.unreal_editor_cmd, "UnrealEditor-Cmd")
    if editor.name != "UnrealEditor-Cmd" or tuple(
        parent.name for parent in (editor.parent, editor.parent.parent, editor.parent.parent.parent)
    ) != ("Linux", "Binaries", "Engine"):
        _fail("VISTA_HOME_BUILD_EDITOR_INVALID", "editor must be an exact Engine/Binaries/Linux/UnrealEditor-Cmd path")
    if not editor.exists():
        if require_existing:
            _fail("VISTA_HOME_BUILD_EDITOR_MISSING", "UnrealEditor-Cmd is required for --apply", pointer=str(editor))
        if config.unreal_editor_cmd_sha256 is not None:
            _require_sha(config.unreal_editor_cmd_sha256, "UnrealEditor-Cmd pin")
        return editor, config.unreal_editor_cmd_sha256
    source = _existing_file(editor, "UnrealEditor-Cmd")
    if not os.access(source, os.X_OK):
        _fail("VISTA_HOME_BUILD_EDITOR_INVALID", "UnrealEditor-Cmd is not executable", pointer=str(source))
    expected = _require_sha(config.unreal_editor_cmd_sha256, "UnrealEditor-Cmd pin")
    actual = sha256_file(source)
    if actual != expected:
        _fail("VISTA_HOME_BUILD_PIN_MISMATCH", "UnrealEditor-Cmd SHA-256 differs", pointer=str(source))
    return source, actual


def _planned_execution(
    *,
    plan: Mapping[str, Any],
    attempt: Path,
    bindings: Sequence[Mapping[str, Any]],
    project_sha256: str,
    build_plan_sha256: str,
) -> tuple[dict[str, Any], bytes, str]:
    composition = planning.build_composition_spec(plan)
    scripts = {
        "import": Path(__file__).with_name("import_assets_commandlet.py").resolve(strict=True),
        "compose": Path(__file__).with_name("compose_home_commandlet.py").resolve(strict=True),
    }
    value = {
        "schema_version": contract.EXECUTION_SCHEMA,
        "attempt_root": str(attempt),
        "project_file": str(attempt / "project" / EXPECTED_PROJECT_NAME),
        "project_sha256": project_sha256,
        "build_plan_path": str(attempt / "contracts" / "build-plan.json"),
        "build_plan_sha256": build_plan_sha256,
        "build_plan_content_digest": plan["content_digest"],
        "composition_spec": composition.value,
        "composition_spec_sha256": composition.sha256,
        "artifact_bindings": [dict(binding) for binding in sorted(bindings, key=lambda item: item["asset_id"])],
        "scripts": {
            name: {"path": str(path), "sha256": sha256_file(path)}
            for name, path in scripts.items()
        },
        "import_receipt": str(attempt / "import-receipt.json"),
        "scene_receipt": str(attempt / "scene-receipt.json"),
        "policy": {
            "append_only_namespace": True,
            "replace_existing": False,
            "save_reload_required": True,
            "quarantine_on_failure": True,
            "studio_socket_fallback_allowed": False,
        },
    }
    raw = planning.canonical_json(value)
    return value, raw, sha256_bytes(raw)


def plan_build(config: BuildConfig, *, require_editor: bool = False) -> PlannedBuild:
    run_root, attempt = _validate_destination(config)
    if (
        isinstance(config.command_timeout_s, bool)
        or not isinstance(config.command_timeout_s, int)
        or not 60 <= config.command_timeout_s <= 14_400
    ):
        _fail("VISTA_HOME_BUILD_ARGUMENT_INVALID", "command timeout must be an integer from 60 through 14400 seconds")
    plan = validate_build_plan(config.build_plan, config.build_plan_sha256, config.expected_revision)
    blender = validate_blender_manifest(config.blender_manifest, config.blender_manifest_sha256, plan)
    visual: dict[str, tuple[Path, str]] | None = None
    if config.visual_binding_manifest is not None:
        if config.visual_binding_manifest_sha256 is None:
            _fail("VISTA_HOME_BUILD_PIN_INVALID", "visual binding manifest pin is required")
        visual = validate_visual_binding_manifest(
            config.visual_binding_manifest,
            config.visual_binding_manifest_sha256,
            plan,
            blender.normalized["content_digest"],
        )
    elif config.visual_binding_manifest_sha256 is not None:
        _fail("VISTA_HOME_BUILD_ARGUMENT_INVALID", "visual binding pin requires a manifest path")
    bindings = build_artifact_bindings(plan, blender, visual)
    plugin_snapshot = _validate_plugin_package(config.plugin_package, config.plugin_package_tree_sha256)
    characters_snapshot = _validate_characters_content(config.characters_content, config.characters_content_tree_sha256)
    editor, editor_sha = _validate_editor(config, require_existing=require_editor)
    descriptor_raw = canonical_json(project_descriptor())
    engine_ini_raw = default_engine_ini(plan)
    execution, execution_raw, execution_sha = _planned_execution(
        plan=plan,
        attempt=attempt,
        bindings=bindings,
        project_sha256=sha256_bytes(descriptor_raw),
        build_plan_sha256=config.build_plan_sha256,
    )
    execution_path = attempt / "execution.json"
    project_path = attempt / "project" / EXPECTED_PROJECT_NAME
    common_env = {
        "VISTA_PLAYABLE_HOME_EXECUTION": str(execution_path),
        "VISTA_PLAYABLE_HOME_EXECUTION_SHA256": execution_sha,
        "VISTA_PLAYABLE_HOME_PROJECT": str(project_path),
    }
    report = {
        "schema_version": ORCHESTRATOR_PLAN_SCHEMA,
        "mode": "apply" if require_editor else "dry_run",
        "run_root": str(run_root),
        "attempt_root": str(attempt),
        "revision": config.expected_revision,
        "inputs": {
            "build_plan": {"path": str(config.build_plan), "sha256": config.build_plan_sha256},
            "blender_manifest": {"path": str(config.blender_manifest), "sha256": config.blender_manifest_sha256},
            "visual_binding_manifest": (
                {"path": str(config.visual_binding_manifest), "sha256": config.visual_binding_manifest_sha256}
                if config.visual_binding_manifest is not None
                else None
            ),
            "plugin_package": {
                "path": str(config.plugin_package),
                "tree_sha256": plugin_snapshot.sha256,
                "file_count": plugin_snapshot.file_count,
                "bytes": plugin_snapshot.total_bytes,
            },
            "characters_content": {
                "path": str(config.characters_content),
                "tree_sha256": characters_snapshot.sha256,
                "file_count": characters_snapshot.file_count,
                "bytes": characters_snapshot.total_bytes,
            },
            "unreal_editor_cmd": {"path": str(editor), "sha256": editor_sha},
        },
        "project": {
            "file": str(project_path),
            "sha256": sha256_bytes(descriptor_raw),
            "plugin_destination": str(attempt / "project" / "Plugins" / EXPECTED_PLUGIN_NAME),
            "characters_destination": str(attempt / "project" / "Content" / "Characters"),
            "engine_config_sha256": sha256_bytes(engine_ini_raw),
        },
        "execution": {"path": str(execution_path), "sha256": execution_sha, "value": execution},
        "commands": [
            {
                "phase": "import",
                "argv": _fixed_command(editor, project_path, Path(execution["scripts"]["import"]["path"])),
                "env": common_env,
                "log": str(attempt / "import.log"),
                "timeout_s": config.command_timeout_s,
            },
            {
                "phase": "compose",
                "argv": _fixed_command(editor, project_path, Path(execution["scripts"]["compose"]["path"])),
                "env": {
                    **common_env,
                    "VISTA_PLAYABLE_HOME_IMPORT_RECEIPT_SHA256": "<sha256-from-verified-import-receipt>",
                },
                "log": str(attempt / "compose.log"),
                "timeout_s": config.command_timeout_s,
            },
        ],
        "publication": {
            "condition": "both commandlets succeeded and receipts were verified",
            "accepted_pointer": str(run_root / "ue" / "accepted.json"),
            "current_pointer": str(run_root / "ue" / "current.json"),
        },
    }
    report["content_digest"] = _content_digest(report)
    return PlannedBuild(
        config=config,
        plan=plan,
        blender=blender,
        bindings=bindings,
        plugin_snapshot=plugin_snapshot,
        characters_snapshot=characters_snapshot,
        project_raw=descriptor_raw,
        engine_ini_raw=engine_ini_raw,
        execution=execution,
        execution_raw=execution_raw,
        execution_sha256=execution_sha,
        dry_run_report=report,
    )


def _write_exclusive(path: Path, raw: bytes, mode: int = 0o600) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def _attempt_owner_document(token: str) -> dict[str, Any]:
    return {
        "schema_version": ATTEMPT_OWNER_SCHEMA,
        "pid": os.getpid(),
        "token": token,
    }


def _attempt_is_owned(attempt: Path, token: str) -> bool:
    """Return true only for the exact sentinel this process created."""

    owner = attempt / ".orchestrator-owner.json"
    try:
        metadata = os.lstat(owner)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            return False
        raw = owner.read_bytes()
        value = json.loads(
            raw.decode("utf-8", "strict"),
            object_pairs_hook=_duplicate_object,
            parse_constant=_reject_constant,
        )
    except (BuildHomeError, OSError, UnicodeError, json.JSONDecodeError):
        return False
    return value == _attempt_owner_document(token) and raw == canonical_json(value)


def _atomic_pointer(path: Path, value: Mapping[str, Any]) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        _fail("VISTA_HOME_BUILD_POINTER_UNSAFE", "build pointer is unsafe", pointer=str(path))
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}.{secrets.token_hex(8)}")
    _write_exclusive(temporary, canonical_json(value))
    try:
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _atomic_pointer_raw(path: Path, raw: bytes) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        _fail("VISTA_HOME_BUILD_POINTER_UNSAFE", "build pointer is unsafe", pointer=str(path))
    temporary = path.with_name(path.name + f".rollback.{os.getpid()}.{secrets.token_hex(8)}")
    _write_exclusive(temporary, raw)
    try:
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _publish_pointers_transactionally(ue_root: Path, value: Mapping[str, Any]) -> None:
    """Publish the two mutable pointers or restore their exact prior bytes."""

    lock_path = ue_root / ".publication.lock"
    try:
        lock_fd = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as exc:
        _fail("VISTA_HOME_BUILD_POINTER_UNSAFE", f"cannot open publication lock: {exc}", pointer=str(lock_path))
        raise AssertionError from exc
    try:
        if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
            _fail("VISTA_HOME_BUILD_POINTER_UNSAFE", "publication lock is not a regular file", pointer=str(lock_path))
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        paths = (ue_root / "accepted.json", ue_root / "current.json")
        previous: dict[Path, bytes | None] = {}
        for path in paths:
            if path.is_symlink() or (path.exists() and not path.is_file()):
                _fail("VISTA_HOME_BUILD_POINTER_UNSAFE", "build pointer is unsafe", pointer=str(path))
            previous[path] = path.read_bytes() if path.exists() else None
        changed: list[Path] = []
        try:
            for path in paths:
                _atomic_pointer(path, value)
                changed.append(path)
        except Exception:
            rollback_errors: list[str] = []
            for path in reversed(changed):
                try:
                    prior = previous[path]
                    if prior is None:
                        path.unlink(missing_ok=True)
                    else:
                        _atomic_pointer_raw(path, prior)
                except Exception as exc:
                    rollback_errors.append(f"{path.name}: {exc}")
            if rollback_errors:
                _fail(
                    "VISTA_HOME_BUILD_POINTER_ROLLBACK_FAILED",
                    "pointer publication failed and rollback was incomplete: " + "; ".join(rollback_errors),
                )
            raise
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def _copy_reflink_or_copy(source: str, destination: str, *, counts: Counter[str]) -> str:
    src = Path(source)
    dst = Path(destination)
    metadata = os.lstat(src)
    if not stat.S_ISREG(metadata.st_mode):
        _fail("VISTA_HOME_BUILD_COPY_UNSAFE", "copy source is not a regular file", pointer=str(src))
    source_fd = os.open(src, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    destination_fd = os.open(
        dst,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        stat.S_IMODE(metadata.st_mode),
    )
    try:
        try:
            fcntl.ioctl(destination_fd, FICLONE, source_fd)
            method = "reflink"
        except OSError as exc:
            if exc.errno not in {errno.EXDEV, errno.EOPNOTSUPP, errno.ENOTTY, errno.EINVAL, errno.ENOSYS}:
                raise
            os.close(destination_fd)
            destination_fd = -1
            dst.unlink()
            shutil.copy2(src, dst, follow_symlinks=False)
            method = "copy"
    finally:
        os.close(source_fd)
        if destination_fd >= 0:
            os.close(destination_fd)
    if method == "reflink":
        shutil.copystat(src, dst, follow_symlinks=False)
    counts[method] += 1
    return str(dst)


def _copy_tree(source: Path, destination: Path, label: str) -> Counter[str]:
    counts: Counter[str] = Counter()

    def copier(src: str, dst: str) -> str:
        return _copy_reflink_or_copy(src, dst, counts=counts)

    try:
        shutil.copytree(source, destination, copy_function=copier, symlinks=False)
    except Exception:
        # The new attempt remains quarantined evidence; never delete a partial
        # destination from an append-only attempt.
        raise
    if not destination.is_dir():
        _fail("VISTA_HOME_BUILD_COPY_FAILED", f"{label} destination was not created")
    return counts


def _load_receipt(path: Path, expected_schema: str, expected_status: str, label: str) -> tuple[dict[str, Any], str]:
    source = _existing_file(path, label)
    raw = source.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_duplicate_object, parse_constant=_reject_constant)
    except BuildHomeError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        _fail("VISTA_HOME_BUILD_RECEIPT_INVALID", f"{label} is invalid JSON", pointer=str(path))
        raise AssertionError from exc
    if not isinstance(value, dict) or value.get("schema_version") != expected_schema or value.get("status") != expected_status:
        _fail("VISTA_HOME_BUILD_RECEIPT_INVALID", f"{label} schema or status differs", pointer=str(path))
    if value.get("error") is not None:
        _fail("VISTA_HOME_BUILD_RECEIPT_INVALID", f"{label} success carries an error", pointer=str(path))
    return value, sha256_bytes(raw)


def _verify_import_receipt(receipt: Mapping[str, Any], execution: Mapping[str, Any], plan: Mapping[str, Any]) -> None:
    expected_keys = {"schema_version", "status", "error", "bindings", "content_namespace", "assets", "gates"}
    if set(receipt) != expected_keys or receipt.get("content_namespace") != plan["unreal"]["content_namespace"]:
        _fail("VISTA_HOME_BUILD_RECEIPT_INVALID", "import receipt fields or namespace differ")
    gates = receipt.get("gates")
    if gates != {
        "namespace_fresh": True,
        "all_assets_bound": True,
        "material_and_collision_inspected": True,
        "quarantined": False,
    }:
        _fail("VISTA_HOME_BUILD_RECEIPT_INVALID", "import receipt gates did not pass")
    bindings = receipt.get("bindings")
    expected_binding_keys = {
        "engine",
        "project",
        "execution_manifest",
        "execution_manifest_sha256",
        "build_plan_sha256",
        "composition_spec_sha256",
    }
    if not isinstance(bindings, Mapping) or set(bindings) != expected_binding_keys or (
        not isinstance(bindings.get("engine"), str)
        or not bindings.get("engine", "").startswith("5.")
        or bindings.get("project") != execution["project_file"]
        or bindings.get("execution_manifest") != str(Path(execution["attempt_root"]) / "execution.json")
        or bindings.get("execution_manifest_sha256") != sha256_bytes(planning.canonical_json(execution))
        or bindings.get("build_plan_sha256") != execution["build_plan_sha256"]
        or bindings.get("composition_spec_sha256") != execution["composition_spec_sha256"]
    ):
        _fail("VISTA_HOME_BUILD_RECEIPT_INVALID", "import receipt pins differ")
    assets = receipt.get("assets")
    if not isinstance(assets, list) or len(assets) != len(plan["assets"]):
        _fail("VISTA_HOME_BUILD_RECEIPT_INVALID", "import receipt asset inventory differs")
    plan_by_id = {item["asset_id"]: item for item in plan["assets"]}
    binding_by_id = {item["asset_id"]: item for item in execution["artifact_bindings"]}
    receipt_ids = [item.get("asset_id") for item in assets if isinstance(item, Mapping)]
    if len(receipt_ids) != len(assets) or len(receipt_ids) != len(set(receipt_ids)) or set(receipt_ids) != set(plan_by_id):
        _fail("VISTA_HOME_BUILD_RECEIPT_INVALID", "import receipt asset IDs differ or are duplicated")
    common_keys = {"asset_id", "source_kind", "uri", "source_digest", "object_path", "inspection"}
    imported_keys = common_keys | {"source_file_sha256", "raw_returned_object_paths", "returned_object_paths"}
    inspection_keys = {
        "object_path",
        "class_path",
        "collision_policies",
        "material_paths",
        "simple_collision_shapes",
        "collision_generated",
        "collision_trace_flag",
        "room_shell",
    }
    for item in assets:
        asset_id = item["asset_id"]
        source = plan_by_id[asset_id]
        expected_keys = common_keys if source["source_kind"] == "builtin" else imported_keys
        inspection = item.get("inspection")
        if (
            set(item) != expected_keys
            or item.get("source_kind") != source["source_kind"]
            or item.get("uri") != source["uri"]
            or item.get("source_digest") != source["source_digest"]
            or item.get("object_path") != derived_asset_path(plan["unreal"]["content_namespace"], source)
            or not isinstance(inspection, Mapping)
            or set(inspection) != inspection_keys
            or inspection.get("object_path") != item.get("object_path")
            or not isinstance(inspection.get("class_path"), str)
            or not inspection.get("class_path")
            or not isinstance(inspection.get("collision_policies"), list)
            or not isinstance(inspection.get("material_paths"), list)
        ):
            _fail("VISTA_HOME_BUILD_RECEIPT_INVALID", f"import receipt asset {asset_id} fields differ")
        if source["source_kind"] != "builtin" and (
            item.get("source_file_sha256") != binding_by_id[asset_id]["source_file_sha256"]
            or not isinstance(item.get("raw_returned_object_paths"), list)
            or not all(isinstance(path, str) and path for path in item["raw_returned_object_paths"])
            or not isinstance(item.get("returned_object_paths"), list)
            or not all(isinstance(path, str) and path for path in item["returned_object_paths"])
        ):
            _fail("VISTA_HOME_BUILD_RECEIPT_INVALID", f"import receipt asset {asset_id} binding differs")


def _verify_scene_receipt(
    receipt: Mapping[str, Any],
    execution: Mapping[str, Any],
    plan: Mapping[str, Any],
    import_sha256: str,
) -> None:
    expected_keys = {
        "schema_version",
        "status",
        "error",
        "bindings",
        "content_namespace",
        "map_path",
        "actor_inventory",
        "gates",
    }
    if (
        set(receipt) != expected_keys
        or receipt.get("content_namespace") != plan["unreal"]["content_namespace"]
        or receipt.get("map_path") != plan["unreal"]["map_path"]
    ):
        _fail("VISTA_HOME_BUILD_RECEIPT_INVALID", "scene receipt fields or revision paths differ")
    gates = receipt.get("gates")
    if gates != {
        "map_saved": True,
        "map_reloaded": True,
        "semantic_tags_verified": True,
        "player_start_verified": True,
        "game_mode_configured": True,
        "navmesh_bounds_verified": True,
        "quarantined": False,
        "runtime_play_proof": "pending",
    }:
        _fail("VISTA_HOME_BUILD_RECEIPT_INVALID", "scene receipt gates did not pass")
    bindings = receipt.get("bindings")
    expected_binding_keys = {
        "engine",
        "project",
        "execution_manifest",
        "execution_manifest_sha256",
        "import_receipt",
        "import_receipt_sha256",
        "composition_spec_sha256",
    }
    if not isinstance(bindings, Mapping) or set(bindings) != expected_binding_keys or (
        not isinstance(bindings.get("engine"), str)
        or not bindings.get("engine", "").startswith("5.")
        or bindings.get("project") != execution["project_file"]
        or bindings.get("execution_manifest") != str(Path(execution["attempt_root"]) / "execution.json")
        or bindings.get("execution_manifest_sha256") != sha256_bytes(planning.canonical_json(execution))
        or bindings.get("import_receipt") != execution["import_receipt"]
        or bindings.get("import_receipt_sha256") != import_sha256
        or bindings.get("composition_spec_sha256") != execution["composition_spec_sha256"]
    ):
        _fail("VISTA_HOME_BUILD_RECEIPT_INVALID", "scene receipt pins differ")
    if not isinstance(receipt.get("actor_inventory"), list):
        _fail("VISTA_HOME_BUILD_RECEIPT_INVALID", "scene actor inventory is invalid")


def _terminate_owned_process_group(process: subprocess.Popen[bytes]) -> None:
    """Best-effort bounded reap for the process group started by this tool."""

    try:
        if process.poll() is not None:
            return
    except BaseException:
        pass
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        try:
            process.terminate()
        except BaseException:
            pass
    try:
        process.wait(timeout=10)
        return
    except BaseException:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        try:
            process.kill()
        except BaseException:
            pass
    try:
        process.wait(timeout=10)
    except BaseException:
        pass


def _run_command(
    *,
    phase: str,
    argv: Sequence[str],
    environment: Mapping[str, str],
    log_path: Path,
    marker_prefix: str,
    timeout_s: int,
) -> dict[str, Any]:
    descriptor = os.open(
        log_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    process: subprocess.Popen[bytes] | None = None
    timed_out = False
    return_code: int | None = None
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as log:
            process = subprocess.Popen(
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                env={**os.environ, **dict(environment)},
                start_new_session=True,
            )
            try:
                return_code = process.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                _terminate_owned_process_group(process)
                return_code = process.poll()
            log.flush()
            os.fsync(log.fileno())
    except BaseException:
        if process is not None and process.poll() is None:
            _terminate_owned_process_group(process)
        raise
    finally:
        os.close(descriptor)
    if timed_out:
        _fail("VISTA_HOME_BUILD_COMMAND_TIMEOUT", f"{phase} commandlet exceeded {timeout_s} seconds", pointer=str(log_path))
    if process is None or return_code != 0:
        _fail("VISTA_HOME_BUILD_COMMAND_FAILED", f"{phase} commandlet exited nonzero", pointer=str(log_path))
    marker: dict[str, Any] | None = None
    prefix = marker_prefix.encode("utf-8")
    with log_path.open("rb") as log:
        for line in log:
            if line.startswith(prefix):
                try:
                    candidate = json.loads(line[len(prefix) :].decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError):
                    candidate = None
                if isinstance(candidate, dict):
                    marker = candidate
    if marker is None:
        _fail("VISTA_HOME_BUILD_MARKER_MISSING", f"{phase} commandlet did not emit its result marker", pointer=str(log_path))
    return marker


def _verify_marker(marker: Mapping[str, Any], *, status: str, receipt: Path, sha256: str, phase: str) -> None:
    if set(marker) != {"status", "receipt", "sha256"} or marker != {
        "status": status,
        "receipt": str(receipt),
        "sha256": sha256,
    }:
        _fail("VISTA_HOME_BUILD_MARKER_INVALID", f"{phase} marker disagrees with its verified receipt")


def _materialize_inputs(planned: PlannedBuild, *, owner_token: str | None = None) -> tuple[Path, Counter[str]]:
    config = planned.config
    run_root, attempt = _validate_destination(config)
    ue_root = run_root / "ue"
    ue_root.mkdir(mode=0o700, exist_ok=True)
    if ue_root.is_symlink() or not ue_root.is_dir():
        _fail("VISTA_HOME_BUILD_ATTEMPT_INVALID", "UE run root is unsafe", pointer=str(ue_root))
    attempt.mkdir(mode=0o700, exist_ok=False)
    if owner_token is not None:
        _write_exclusive(attempt / ".orchestrator-owner.json", canonical_json(_attempt_owner_document(owner_token)))
    contracts_dir = attempt / "contracts"
    project_root = attempt / "project"
    config_dir = project_root / "Config"
    content_root = project_root / "Content"
    plugins_root = project_root / "Plugins"
    for directory in (contracts_dir, project_root, config_dir, content_root, plugins_root):
        directory.mkdir(mode=0o700, exist_ok=False)
    build_plan_target = contracts_dir / "build-plan.json"
    _write_exclusive(build_plan_target, planning.canonical_json(planned.plan))
    project_file = project_root / EXPECTED_PROJECT_NAME
    _write_exclusive(project_file, planned.project_raw)
    _write_exclusive(config_dir / "DefaultEngine.ini", planned.engine_ini_raw)
    copy_counts: Counter[str] = Counter()
    copy_counts.update(_copy_tree(config.plugin_package, plugins_root / EXPECTED_PLUGIN_NAME, "compiled plugin"))
    copy_counts.update(_copy_tree(config.characters_content, content_root / "Characters", "Characters content"))
    installed_plugin = snapshot_tree(plugins_root / EXPECTED_PLUGIN_NAME, "installed compiled plugin")
    installed_characters = snapshot_tree(content_root / "Characters", "installed Characters content")
    if installed_plugin.sha256 != planned.plugin_snapshot.sha256 or installed_characters.sha256 != planned.characters_snapshot.sha256:
        _fail("VISTA_HOME_BUILD_COPY_FAILED", "installed project content differs from its pinned source")
    generated = contract.build_execution_manifest(
        build_plan_path=build_plan_target,
        build_plan=planned.plan,
        project_file=project_file,
        attempt_root=attempt,
        artifact_bindings=planned.bindings,
        import_receipt=attempt / "import-receipt.json",
        scene_receipt=attempt / "scene-receipt.json",
    )
    if generated.raw != planned.execution_raw or generated.sha256 != planned.execution_sha256:
        _fail("VISTA_HOME_BUILD_EXECUTION_DRIFT", "materialized execution manifest differs from the dry-run plan")
    _write_exclusive(attempt / "execution.json", generated.raw)
    preparation = {
        "schema_version": PREPARATION_RECEIPT_SCHEMA,
        "status": "prepared",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "attempt_root": str(attempt),
        "orchestrator_plan_digest": planned.dry_run_report["content_digest"],
        "execution_sha256": generated.sha256,
        "project_sha256": generated.value["project_sha256"],
        "build_plan_sha256": generated.value["build_plan_sha256"],
        "plugin_tree_sha256": installed_plugin.sha256,
        "characters_tree_sha256": installed_characters.sha256,
        "copy_methods": dict(sorted(copy_counts.items())),
    }
    _write_exclusive(attempt / "preparation-receipt.json", canonical_json(preparation))
    return attempt, copy_counts


def apply_build(planned: PlannedBuild) -> dict[str, Any]:
    # Revalidate the executable immediately before the first side effect.
    _validate_editor(planned.config, require_existing=True)
    # Keep the lexical attempt identity before materialization so a copy or
    # execution-manifest failure can still receive an append-only quarantine
    # receipt.  The path is inspected again before any receipt is written.
    attempt = planned.config.attempt_root
    owner_token = secrets.token_hex(32)
    try:
        attempt, copy_counts = _materialize_inputs(planned, owner_token=owner_token)
        execution_path = attempt / "execution.json"
        project_path = attempt / "project" / EXPECTED_PROJECT_NAME
        common_env = {
            "VISTA_PLAYABLE_HOME_EXECUTION": str(execution_path),
            "VISTA_PLAYABLE_HOME_EXECUTION_SHA256": planned.execution_sha256,
            "VISTA_PLAYABLE_HOME_PROJECT": str(project_path),
        }
        import_receipt_path = attempt / "import-receipt.json"
        import_marker = _run_command(
            phase="import",
            argv=_fixed_command(
                planned.config.unreal_editor_cmd,
                project_path,
                Path(planned.execution["scripts"]["import"]["path"]),
            ),
            environment=common_env,
            log_path=attempt / "import.log",
            marker_prefix=IMPORT_MARKER,
            timeout_s=planned.config.command_timeout_s,
        )
        import_receipt, import_sha = _load_receipt(
            import_receipt_path,
            IMPORT_RECEIPT_SCHEMA,
            "imported_candidate",
            "import receipt",
        )
        _verify_import_receipt(import_receipt, planned.execution, planned.plan)
        _verify_marker(
            import_marker,
            status="imported_candidate",
            receipt=import_receipt_path,
            sha256=import_sha,
            phase="import",
        )

        scene_receipt_path = attempt / "scene-receipt.json"
        scene_marker = _run_command(
            phase="compose",
            argv=_fixed_command(
                planned.config.unreal_editor_cmd,
                project_path,
                Path(planned.execution["scripts"]["compose"]["path"]),
            ),
            environment={**common_env, "VISTA_PLAYABLE_HOME_IMPORT_RECEIPT_SHA256": import_sha},
            log_path=attempt / "compose.log",
            marker_prefix=SCENE_MARKER,
            timeout_s=planned.config.command_timeout_s,
        )
        scene_receipt, scene_sha = _load_receipt(
            scene_receipt_path,
            SCENE_RECEIPT_SCHEMA,
            "saved_reloaded_candidate",
            "scene receipt",
        )
        _verify_scene_receipt(scene_receipt, planned.execution, planned.plan, import_sha)
        _verify_marker(
            scene_marker,
            status="saved_reloaded_candidate",
            receipt=scene_receipt_path,
            sha256=scene_sha,
            phase="compose",
        )
        result = {
            "schema_version": RESULT_RECEIPT_SCHEMA,
            "status": "accepted_candidate",
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "attempt_root": str(attempt),
            "revision": planned.config.expected_revision,
            "map_path": planned.plan["unreal"]["map_path"],
            "execution_sha256": planned.execution_sha256,
            "import_receipt_sha256": import_sha,
            "scene_receipt_sha256": scene_sha,
            "copy_methods": dict(sorted(copy_counts.items())),
            "runtime_play_proof": "pending",
        }
        result["content_digest"] = _content_digest(result)
        result_path = attempt / "result-receipt.json"
        _write_exclusive(result_path, canonical_json(result))
        pointer = {
            "schema_version": POINTER_SCHEMA,
            "attempt": attempt.name,
            "result_receipt": f"{attempt.name}/result-receipt.json",
            "result_receipt_sha256": sha256_file(result_path),
            "revision": planned.config.expected_revision,
        }
        ue_root = attempt.parent
        _publish_pointers_transactionally(ue_root, pointer)
        return result
    except BaseException as exc:
        if _attempt_is_owned(attempt, owner_token):
            failure_path = attempt / "result-receipt.json"
            if not failure_path.exists():
                failure = {
                    "schema_version": RESULT_RECEIPT_SCHEMA,
                    "status": "failed_quarantined",
                    "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                    "attempt_root": str(attempt),
                    "error": {
                        "type": type(exc).__name__,
                        "code": exc.code if isinstance(exc, BuildHomeError) else "VISTA_HOME_BUILD_UNEXPECTED",
                        "message": str(exc)[:512],
                    },
                }
                try:
                    _write_exclusive(failure_path, canonical_json(failure))
                except Exception:
                    pass
            else:
                try:
                    publication_failure_path = attempt / "publication-failure.json"
                    publication_failure = {
                        "schema_version": RESULT_RECEIPT_SCHEMA,
                        "status": "publication_failed_quarantined",
                        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                        "attempt_root": str(attempt),
                        "result_receipt_sha256": sha256_file(failure_path),
                        "error": {
                            "type": type(exc).__name__,
                            "code": exc.code if isinstance(exc, BuildHomeError) else "VISTA_HOME_BUILD_UNEXPECTED",
                            "message": str(exc)[:512],
                        },
                    }
                    _write_exclusive(publication_failure_path, canonical_json(publication_failure))
                except Exception:
                    pass
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate or execute one append-only VISTA Playable Home Unreal build",
    )
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--attempt-root", required=True, type=Path)
    parser.add_argument("--build-plan", required=True, type=Path)
    parser.add_argument("--build-plan-sha256", required=True)
    parser.add_argument("--blender-manifest", required=True, type=Path)
    parser.add_argument("--blender-manifest-sha256", required=True)
    parser.add_argument("--visual-binding-manifest", type=Path)
    parser.add_argument("--visual-binding-manifest-sha256")
    parser.add_argument("--plugin-package", required=True, type=Path)
    parser.add_argument("--plugin-package-tree-sha256", required=True)
    parser.add_argument("--characters-content", required=True, type=Path)
    parser.add_argument("--characters-content-tree-sha256", required=True)
    parser.add_argument("--unreal-editor-cmd", required=True, type=Path)
    parser.add_argument("--unreal-editor-cmd-sha256")
    parser.add_argument("--expected-revision", default=EXPECTED_REVISION, choices=[EXPECTED_REVISION])
    parser.add_argument("--command-timeout-s", type=int, default=3600)
    parser.add_argument("--apply", action="store_true", help="materialize and run the two fixed UE commandlets")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = BuildConfig(
        run_root=args.run_root,
        attempt_root=args.attempt_root,
        build_plan=args.build_plan,
        build_plan_sha256=args.build_plan_sha256,
        blender_manifest=args.blender_manifest,
        blender_manifest_sha256=args.blender_manifest_sha256,
        visual_binding_manifest=args.visual_binding_manifest,
        visual_binding_manifest_sha256=args.visual_binding_manifest_sha256,
        plugin_package=args.plugin_package,
        plugin_package_tree_sha256=args.plugin_package_tree_sha256,
        characters_content=args.characters_content,
        characters_content_tree_sha256=args.characters_content_tree_sha256,
        unreal_editor_cmd=args.unreal_editor_cmd,
        unreal_editor_cmd_sha256=args.unreal_editor_cmd_sha256,
        expected_revision=args.expected_revision,
        command_timeout_s=args.command_timeout_s,
    )
    try:
        planned = plan_build(config, require_editor=args.apply)
        if args.apply:
            result = apply_build(planned)
        else:
            result = planned.dry_run_report
        sys.stdout.buffer.write(canonical_json(result))
        return 0
    except BuildHomeError as exc:
        sys.stderr.buffer.write(canonical_json({"ok": False, "error": exc.public_dict()}))
        return 2
    except (contract.VistaPlayableHomeContractError, planning.VistaPlayableHomePlanError) as exc:
        sys.stderr.buffer.write(
            canonical_json(
                {
                    "ok": False,
                    "error": {
                        "code": getattr(exc, "code", "VISTA_HOME_BUILD_CONTRACT_ERROR"),
                        "message": str(exc),
                    },
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
