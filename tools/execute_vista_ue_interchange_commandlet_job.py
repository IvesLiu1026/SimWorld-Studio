#!/usr/bin/env python3
"""Plan the pinned VISTA CC0 Interchange job for a disposable UE commandlet.

This is a quarantined draft. The default mode is a zero-write dry run and every
live-apply entry point fails before creating a claim, evidence directory, or
process. Independent review found unresolved supervision and execution-surface
sealing P0s; do not re-enable ``--apply`` until those findings are closed and a
new review explicitly approves the executor.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

import fetch_vista_bootstrap_assets as acquisition
import prepare_vista_ue_interchange_import_job as preparation


RESULT_SCHEMA = "simworld-ue-interchange-commandlet-execution-result/v1"
INTENT_SCHEMA = "simworld-ue-interchange-commandlet-intent/v1"
MARKER_SCHEMA = "simworld-ue-interchange-commandlet-marker/v1"
RECEIPT_SCHEMA = "simworld-ue-interchange-commandlet-terminal-receipt/v1"
LIVE_APPLY_QUARANTINED = True
LIVE_APPLY_QUARANTINE_CODE = "UE_COMMANDLET_LIVE_APPLY_QUARANTINED"
PINNED_MANIFEST_SHA256 = "a21c80a3a88f2ba84b641a2b3737651898abbce14c822ce577c10fe7f6256905"
PINNED_REVISION = "vista-mmg-040-polyhaven-2026-07-22-v2"
PINNED_FILE_COUNT = 15
PINNED_ASSET_IDS = (
    "polyhaven_cardboard_box_01",
    "polyhaven_painted_wooden_stool",
    "polyhaven_shelf_01",
)
PINNED_SOURCE_IDS = (
    "cardboard_box_01",
    "painted_wooden_stool",
    "Shelf_01",
)
ENGINE_VERSION = "5.3.2"
DESTINATION_ROOT = "/Game/VISTA/External/PolyHaven"
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
EVIDENCE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_LOG_BYTES = 512 * 1024 * 1024
MIN_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 1800
TERMINATION_GRACE_SECONDS = 10
KILL_REAP_GRACE_SECONDS = 5
MARKER_PREFIX = b"VISTA_INTERCHANGE_COMMANDLET_RESULT:"
MARKER_LOG_PREFIX = b"LogPython: " + MARKER_PREFIX
PROJECT_CLAIM_NAME = ".vista-commandlet-one-shot-lock.json"
PROJECT_REQUIRED_DIRECTORIES = ("Config", "Content", "Plugins")
PROJECT_ALLOWED_PLUGIN_NAMES = frozenset({"EditorScriptingUtilities", "PythonScriptPlugin"})
PROJECT_CONFIG_FORBIDDEN_TOKENS = (
    "init_unreal",
    "interchange",
    "pipeline",
    "python",
    "startup",
)
PROJECT_FINGERPRINT_DOMAIN = b"simworld-ue-commandlet-project-influence-tree/v2\0"
INTERCHANGE_FINGERPRINT_DOMAIN = b"simworld-ue-commandlet-interchange-runtime-tree/v1\0"
INTERCHANGE_PIPELINES = (
    "/Interchange/Pipelines/DefaultGLTFAssetsPipeline.DefaultGLTFAssetsPipeline",
    "/Interchange/Pipelines/DefaultGLTFPipeline.DefaultGLTFPipeline",
)
INTERCHANGE_PIPELINE_CLASSES = (
    "/Script/InterchangePipelines.InterchangeGenericAssetsPipeline",
    "/Script/InterchangeImport.InterchangeGLTFPipeline",
)
INTERCHANGE_STATIC_MESH_FACTORY_CLASS = "/Script/InterchangeImport.InterchangeStaticMeshFactory"
MAX_PROJECT_FILES = 4096
MAX_PROJECT_TOTAL_BYTES = 1024 * 1024 * 1024


class CommandletExecutionError(RuntimeError):
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
    raise CommandletExecutionError(code, message, pointer=pointer)


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _reject_constant(value: str) -> None:
    fail("UE_COMMANDLET_JSON_INVALID", "JSON contains a non-finite number", pointer=value)


def _strict_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            fail("UE_COMMANDLET_JSON_INVALID", "JSON contains a duplicate object key", pointer=key)
        result[key] = value
    return result


def strict_json(raw: bytes, *, pointer: str) -> Any:
    if not raw or len(raw) > MAX_JSON_BYTES:
        fail("UE_COMMANDLET_JSON_INVALID", "JSON input is empty or too large", pointer=pointer)
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_strict_pairs,
            parse_constant=_reject_constant,
        )
    except CommandletExecutionError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        fail("UE_COMMANDLET_JSON_INVALID", "JSON input is not bounded UTF-8 JSON", pointer=pointer)


def exact_object(value: Any, keys: set[str], pointer: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        fail("UE_COMMANDLET_CONTRACT_INVALID", "Object fields differ from the fixed contract", pointer=pointer)
    return value


def _safe_sha256(value: Any, pointer: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        fail("UE_COMMANDLET_SHA256_INVALID", "Expected a lowercase SHA-256 digest", pointer=pointer)
    return value


def _safe_absolute_path(path: Path, pointer: str) -> Path:
    if not path.is_absolute() or path == Path(path.anchor):
        fail("UE_COMMANDLET_PATH_INVALID", "Path must be absolute and non-root", pointer=pointer)
    return path


def _lstat_no_symlink_components(path: Path, pointer: str, *, allow_leaf_missing: bool = False) -> None:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for index, part in enumerate(absolute.parts[1:]):
        current /= part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            if allow_leaf_missing and index == len(absolute.parts[1:]) - 1:
                return
            fail("UE_COMMANDLET_PATH_INVALID", "Required path component is missing", pointer=pointer)
        if stat.S_ISLNK(metadata.st_mode):
            fail("UE_COMMANDLET_PATH_INVALID", "Symlinked path components are not accepted", pointer=pointer)


@dataclass(frozen=True)
class FileIdentity:
    path: Path
    sha256: str
    size: int
    mode: int
    uid: int
    device: int
    inode: int
    mtime_ns: int


def inspect_regular_file(
    path: Path,
    *,
    pointer: str,
    expected_sha256: str | None = None,
    require_owner: bool = True,
    require_executable: bool = False,
    maximum_bytes: int | None = None,
) -> FileIdentity:
    _safe_absolute_path(path, pointer)
    _lstat_no_symlink_components(path, pointer)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        fail("UE_COMMANDLET_FILE_UNSAFE", "File cannot be opened safely", pointer=pointer)
    try:
        before = os.fstat(descriptor)
        trusted_uids = {os.geteuid()} if require_owner else {0, os.geteuid()}
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid not in trusted_uids
            or stat.S_IMODE(before.st_mode) & 0o022
            or (require_executable and not before.st_mode & stat.S_IXUSR)
            or (maximum_bytes is not None and before.st_size > maximum_bytes)
        ):
            fail("UE_COMMANDLET_FILE_UNSAFE", "File ownership, mode, type, or size is unsafe", pointer=pointer)
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    observed = digest.hexdigest()
    if (
        total != before.st_size
        or after.st_dev != before.st_dev
        or after.st_ino != before.st_ino
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        fail("UE_COMMANDLET_FILE_CHANGED", "File changed while it was inspected", pointer=pointer)
    if expected_sha256 is not None and observed != expected_sha256:
        fail("UE_COMMANDLET_SHA256_MISMATCH", "File differs from its independently supplied SHA-256", pointer=pointer)
    return FileIdentity(
        path=path,
        sha256=observed,
        size=total,
        mode=stat.S_IMODE(before.st_mode),
        uid=before.st_uid,
        device=before.st_dev,
        inode=before.st_ino,
        mtime_ns=before.st_mtime_ns,
    )


def read_stable_private_file(
    path: Path,
    *,
    pointer: str,
    maximum_bytes: int,
    expected_identity: FileIdentity | None = None,
) -> tuple[bytes, FileIdentity]:
    """Read one mode-0600 file through O_NOFOLLOW with stable inode evidence."""

    _safe_absolute_path(path, pointer)
    _lstat_no_symlink_components(path, pointer)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        fail("UE_COMMANDLET_FILE_UNSAFE", "Evidence file cannot be opened safely", pointer=pointer)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size > maximum_bytes
        ):
            fail("UE_COMMANDLET_FILE_UNSAFE", "Evidence file metadata is unsafe", pointer=pointer)
        if expected_identity is not None and (
            before.st_dev != expected_identity.device or before.st_ino != expected_identity.inode
        ):
            fail("UE_COMMANDLET_FILE_CHANGED", "Evidence file inode changed", pointer=pointer)
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
        or len(raw) != before.st_size
        or after.st_dev != before.st_dev
        or after.st_ino != before.st_ino
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
        or after.st_nlink != 1
    ):
        fail("UE_COMMANDLET_FILE_CHANGED", "Evidence file changed while it was read", pointer=pointer)
    return raw, FileIdentity(
        path=path,
        sha256=sha256_bytes(raw),
        size=len(raw),
        mode=stat.S_IMODE(before.st_mode),
        uid=before.st_uid,
        device=before.st_dev,
        inode=before.st_ino,
        mtime_ns=before.st_mtime_ns,
    )


def read_stable_trusted_file(
    path: Path,
    *,
    pointer: str,
    maximum_bytes: int,
    expected_identity: FileIdentity,
) -> bytes:
    """Re-read one root/current-user trusted immutable file without TOCTOU."""

    _safe_absolute_path(path, pointer)
    _lstat_no_symlink_components(path, pointer)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        fail("UE_COMMANDLET_FILE_UNSAFE", "Trusted file cannot be opened safely", pointer=pointer)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid not in {0, os.geteuid()}
            or stat.S_IMODE(before.st_mode) & 0o022
            or before.st_size > maximum_bytes
            or before.st_dev != expected_identity.device
            or before.st_ino != expected_identity.inode
        ):
            fail("UE_COMMANDLET_FILE_UNSAFE", "Trusted file metadata changed", pointer=pointer)
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
        or len(raw) != before.st_size
        or after.st_dev != before.st_dev
        or after.st_ino != before.st_ino
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
        or after.st_nlink != 1
        or sha256_bytes(raw) != expected_identity.sha256
    ):
        fail("UE_COMMANDLET_FILE_CHANGED", "Trusted file changed while it was read", pointer=pointer)
    return raw


def _validate_private_directory(path: Path, pointer: str, *, writable: bool) -> None:
    _safe_absolute_path(path, pointer)
    _lstat_no_symlink_components(path, pointer)
    metadata = os.lstat(path)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
        or (writable and not os.access(path, os.W_OK | os.X_OK))
    ):
        fail("UE_COMMANDLET_DIRECTORY_UNSAFE", "Directory must be private, current-user-owned, and usable", pointer=pointer)


def validate_evidence_target(path: Path) -> None:
    _safe_absolute_path(path, "--evidence-dir")
    if not EVIDENCE_NAME_RE.fullmatch(path.name):
        fail("UE_COMMANDLET_EVIDENCE_INVALID", "Evidence directory name is invalid", pointer="--evidence-dir")
    _validate_private_directory(path.parent, "--evidence-dir", writable=True)
    try:
        os.lstat(path)
    except FileNotFoundError:
        return
    fail(
        "UE_COMMANDLET_EVIDENCE_EXISTS",
        "Execution evidence is append-only; choose a new evidence directory",
        pointer="--evidence-dir",
    )


def _secure_json_file(path: Path, *, pointer: str, expected_mode: int = 0o600) -> tuple[dict[str, Any], bytes]:
    raw, identity = read_stable_private_file(path, pointer=pointer, maximum_bytes=MAX_JSON_BYTES)
    if identity.mode != expected_mode:
        fail("UE_COMMANDLET_FILE_UNSAFE", "Contract JSON must be current-user-owned mode 0600", pointer=pointer)
    value = strict_json(raw, pointer=pointer)
    if not isinstance(value, dict):
        fail("UE_COMMANDLET_CONTRACT_INVALID", "Contract JSON root must be an object", pointer=pointer)
    return value, raw


@dataclass(frozen=True)
class JobBundle:
    prepared: preparation.PreparedJob
    acquisition_plan: acquisition.AcquisitionPlan
    job_dir: Path
    source_files: tuple[dict[str, Any], ...]


def load_job_bundle(
    manifest_path: Path,
    acquisition_dir: Path,
    job_dir: Path,
) -> JobBundle:
    manifest_identity = inspect_regular_file(
        manifest_path,
        pointer="--manifest",
        expected_sha256=PINNED_MANIFEST_SHA256,
        maximum_bytes=acquisition.MAX_MANIFEST_BYTES,
    )
    if manifest_identity.sha256 != PINNED_MANIFEST_SHA256:
        fail("UE_COMMANDLET_SOURCE_CONTRACT_INVALID", "Source manifest pin does not match")
    try:
        source_plan = acquisition.build_plan(manifest_path, acquisition_dir)
        if not acquisition.verify_existing(source_plan):
            fail("UE_COMMANDLET_ACQUISITION_MISSING", "Verified acquisition output is required")
        prepared = preparation.build_job(manifest_path, acquisition_dir)
    except acquisition.AcquisitionError as error:
        fail("UE_COMMANDLET_ACQUISITION_INVALID", "Pinned acquisition verification failed", pointer=error.pointer)
    except preparation.ImportJobError as error:
        fail("UE_COMMANDLET_JOB_INVALID", "Pinned import job cannot be rebuilt", pointer=error.pointer)

    if (
        source_plan.manifest["revision"] != PINNED_REVISION
        or len(source_plan.entries) != PINNED_FILE_COUNT
        or tuple(sorted(asset["asset_id"] for asset in source_plan.manifest["assets"])) != PINNED_ASSET_IDS
        or tuple(sorted(asset["source_asset_id"] for asset in source_plan.manifest["assets"])) != tuple(sorted(PINNED_SOURCE_IDS))
        or len(prepared.job["assets"]) != len(PINNED_ASSET_IDS)
    ):
        fail("UE_COMMANDLET_SOURCE_CONTRACT_INVALID", "Only the pinned VISTA Poly Haven v2 source set is accepted")

    _validate_private_directory(job_dir, "--job-dir", writable=False)
    observed_names = {entry.name for entry in job_dir.iterdir()}
    if observed_names != {"import-job.json", "preparation-receipt.json"}:
        fail("UE_COMMANDLET_JOB_INVALID", "Prepared job directory file set differs from the sealed contract")
    _job, job_raw = _secure_json_file(job_dir / "import-job.json", pointer="import-job.json")
    receipt, _receipt_raw = _secure_json_file(
        job_dir / "preparation-receipt.json",
        pointer="preparation-receipt.json",
    )
    if job_raw != prepared.job_bytes:
        fail("UE_COMMANDLET_JOB_INVALID", "Prepared import job differs from the freshly verified pinned job")
    receipt = exact_object(
        receipt,
        {
            "schema",
            "job_schema",
            "job_sha256",
            "source_revision",
            "source_manifest_sha256",
            "source_tree_sha256",
            "approval_ref_sha256",
            "prepared_at_utc",
            "publication_policy",
            "unreal_started",
            "content_imported",
        },
        "preparation-receipt.json",
    )
    try:
        prepared_at = dt.datetime.fromisoformat(receipt["prepared_at_utc"])
    except (TypeError, ValueError):
        prepared_at = None
    expected_receipt = {
        "schema": preparation.RECEIPT_SCHEMA,
        "job_schema": preparation.JOB_SCHEMA,
        "job_sha256": prepared.job_sha256,
        "source_revision": prepared.revision,
        "source_manifest_sha256": prepared.manifest_sha256,
        "source_tree_sha256": prepared.tree_sha256,
        "publication_policy": "non_overwriting",
        "unreal_started": False,
        "content_imported": False,
    }
    if (
        {key: receipt.get(key) for key in expected_receipt} != expected_receipt
        or not SHA256_RE.fullmatch(str(receipt.get("approval_ref_sha256", "")))
        or prepared_at is None
        or prepared_at.tzinfo is None
    ):
        fail("UE_COMMANDLET_JOB_INVALID", "Preparation receipt does not seal this exact import job")

    source_files = tuple(
        {
            "relative_path": str(entry["path"]),
            "absolute_path": str(acquisition_dir.joinpath(*PurePosixPath(str(entry["path"])).parts)),
            "bytes": int(entry["bytes"]),
            "sha256": str(entry["sha256"]),
        }
        for entry in sorted(source_plan.entries, key=lambda item: str(item["path"]))
    )
    return JobBundle(
        prepared=prepared,
        acquisition_plan=source_plan,
        job_dir=job_dir,
        source_files=source_files,
    )


def _project_directory_entry(path: Path, relative: str) -> dict[str, Any]:
    metadata = os.lstat(path)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        fail("UE_COMMANDLET_PROJECT_TREE_UNSAFE", "Minimal project directory must be current-user-owned mode 0700", pointer=relative)
    return {"path": relative, "type": "directory", "mode": stat.S_IMODE(metadata.st_mode)}


def _validate_minimal_project_descriptor(project_file: Path) -> dict[str, Any]:
    raw, _identity = read_stable_private_file(
        project_file,
        pointer="--project",
        maximum_bytes=MAX_JSON_BYTES,
    )
    value = strict_json(raw, pointer="--project")
    if not isinstance(value, dict):
        fail("UE_COMMANDLET_PROJECT_SHAPE_INVALID", "Project descriptor must be a JSON object")
    allowed = {"FileVersion", "EngineAssociation", "Category", "Description", "TargetPlatforms", "Plugins"}
    if set(value) - allowed or set(value) < {"FileVersion", "Plugins"} or value["FileVersion"] != 3:
        fail(
            "UE_COMMANDLET_PROJECT_SHAPE_INVALID",
            "Disposable project descriptor contains executable modules or unsupported roots",
        )
    plugins = value["Plugins"]
    if not isinstance(plugins, list) or len(plugins) != len(PROJECT_ALLOWED_PLUGIN_NAMES):
        fail("UE_COMMANDLET_PROJECT_SHAPE_INVALID", "Only the fixed commandlet engine plugins may be enabled")
    observed_plugins: set[str] = set()
    for index, plugin in enumerate(plugins):
        if not isinstance(plugin, dict) or set(plugin) != {"Name", "Enabled"} or plugin["Enabled"] is not True:
            fail("UE_COMMANDLET_PROJECT_SHAPE_INVALID", "Project plugin declaration is not minimal", pointer=f"Plugins/{index}")
        name = plugin["Name"]
        if not isinstance(name, str) or name not in PROJECT_ALLOWED_PLUGIN_NAMES or name in observed_plugins:
            fail("UE_COMMANDLET_PROJECT_SHAPE_INVALID", "Project enables an unapproved plugin", pointer=f"Plugins/{index}")
        observed_plugins.add(name)
    if observed_plugins != PROJECT_ALLOWED_PLUGIN_NAMES:
        fail("UE_COMMANDLET_PROJECT_SHAPE_INVALID", "Fixed commandlet plugins are missing")
    return value


def project_control_manifest(
    project_file: Path,
    *,
    project_claim_sha256: str | None = None,
    allow_imported_vista: bool = False,
) -> tuple[dict[str, Any], ...]:
    """Seal the complete allowlisted pre-execution project influence surface."""

    project_root = project_file.parent
    expected_top_level = {project_file.name, *PROJECT_REQUIRED_DIRECTORIES}
    if project_claim_sha256 is not None:
        expected_top_level.add(PROJECT_CLAIM_NAME)
    try:
        observed_top_level = {entry.name for entry in os.scandir(project_root)}
    except OSError:
        fail("UE_COMMANDLET_PROJECT_TREE_UNSAFE", "Project root cannot be enumerated safely")
    if observed_top_level != expected_top_level:
        fail(
            "UE_COMMANDLET_PROJECT_SHAPE_INVALID",
            "Disposable project must contain only descriptor, Config, empty Content, empty Plugins, and optional one-shot lock",
        )

    _validate_minimal_project_descriptor(project_file)
    entries: list[dict[str, Any]] = []
    project_identity = inspect_regular_file(
        project_file,
        pointer="--project",
        require_owner=True,
        maximum_bytes=MAX_JSON_BYTES,
    )
    entries.append(
        {
            "path": project_file.name,
            "type": "file",
            "mode": project_identity.mode,
            "bytes": project_identity.size,
            "sha256": project_identity.sha256,
        }
    )
    total_bytes = project_identity.size
    for relative_root in PROJECT_REQUIRED_DIRECTORIES:
        root = project_root.joinpath(*PurePosixPath(relative_root).parts)
        _lstat_no_symlink_components(root, relative_root)
        entries.append(_project_directory_entry(root, relative_root))
        for current, directories, files in os.walk(root, topdown=True, followlinks=False):
            current_path = Path(current)
            directories.sort()
            files.sort()
            if relative_root == "Content" and current_path == root and "VISTA" in directories:
                if not allow_imported_vista:
                    fail(
                        "UE_COMMANDLET_PROJECT_SHAPE_INVALID",
                        "Disposable project Content must be empty before import",
                        pointer="Content/VISTA",
                    )
                # Content/VISTA is the only post-launch mutable subtree.  It is
                # deliberately excluded from the pre-execution influence-tree
                # digest, but the subtree root must still be a private real
                # directory; observe_vista_content validates its descendants.
                _project_directory_entry(root / "VISTA", "Content/VISTA")
                directories.remove("VISTA")
            for name in directories:
                path = current_path / name
                relative = path.relative_to(project_root).as_posix()
                if relative_root != "Config":
                    fail(
                        "UE_COMMANDLET_PROJECT_SHAPE_INVALID",
                        "Disposable project Content and Plugins must be empty before import",
                        pointer=relative,
                    )
                entries.append(_project_directory_entry(path, relative))
                if len(entries) >= MAX_PROJECT_FILES:
                    fail("UE_COMMANDLET_PROJECT_TREE_UNSAFE", "Project influence surface exceeds fixed bounds")
            for name in files:
                path = current_path / name
                relative = path.relative_to(project_root).as_posix()
                raw, identity = read_stable_private_file(
                    path,
                    pointer=relative,
                    maximum_bytes=512 * 1024 * 1024,
                )
                total_bytes += identity.size
                if len(entries) >= MAX_PROJECT_FILES or total_bytes > MAX_PROJECT_TOTAL_BYTES:
                    fail("UE_COMMANDLET_PROJECT_TREE_UNSAFE", "Project influence surface exceeds fixed bounds")
                if relative_root == "Config":
                    if path.suffix.lower() != ".ini":
                        fail("UE_COMMANDLET_PROJECT_SHAPE_INVALID", "Config accepts only .ini files", pointer=relative)
                    try:
                        config_text = raw.decode("utf-8").casefold()
                    except UnicodeDecodeError:
                        fail("UE_COMMANDLET_PROJECT_SHAPE_INVALID", "Config must be UTF-8", pointer=relative)
                    if any(token in config_text for token in PROJECT_CONFIG_FORBIDDEN_TOKENS):
                        fail(
                            "UE_COMMANDLET_PROJECT_STARTUP_HOOK_REJECTED",
                            "Project Config contains a startup or Interchange override",
                            pointer=relative,
                        )
                else:
                    # A production commandlet project has no ambient Content,
                    # Python/init_unreal hook, project plugin, or plugin config.
                    fail(
                        "UE_COMMANDLET_PROJECT_SHAPE_INVALID",
                        "Disposable project Content and Plugins must be empty before import",
                        pointer=relative,
                    )
                entries.append(
                    {
                        "path": relative,
                        "type": "file",
                        "mode": identity.mode,
                        "bytes": identity.size,
                        "sha256": identity.sha256,
                    }
                )
    if not any(entry["path"].startswith("Config/") and entry["type"] == "file" for entry in entries):
        fail("UE_COMMANDLET_PROJECT_TREE_UNSAFE", "Project Config contains no pinned files")
    if project_claim_sha256 is not None:
        claim = inspect_regular_file(
            project_root / PROJECT_CLAIM_NAME,
            pointer=PROJECT_CLAIM_NAME,
            expected_sha256=project_claim_sha256,
            require_owner=True,
            maximum_bytes=MAX_JSON_BYTES,
        )
        if claim.mode != 0o600:
            fail("UE_COMMANDLET_PROJECT_ALREADY_CLAIMED", "Project one-shot lock metadata changed")
    return tuple(sorted(entries, key=lambda entry: (entry["path"], entry["type"])))


def project_control_fingerprint(entries: Sequence[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256(PROJECT_FINGERPRINT_DOMAIN)
    for entry in entries:
        digest.update(canonical_json(dict(entry)))
    return digest.hexdigest()


def validate_disposable_project(
    project_file: Path,
    project_sha256: str,
    *,
    project_claim_sha256: str | None = None,
    allow_imported_vista: bool = False,
) -> tuple[FileIdentity, tuple[dict[str, Any], ...], str]:
    project_file = _safe_absolute_path(project_file, "--project")
    if project_file.suffix != ".uproject":
        fail("UE_COMMANDLET_PROJECT_INVALID", "Project must be an exact .uproject file", pointer="--project")
    project_root = project_file.parent
    _validate_private_directory(project_root, "--project", writable=True)
    identity = inspect_regular_file(
        project_file,
        pointer="--project",
        expected_sha256=project_sha256,
        require_owner=True,
        maximum_bytes=MAX_JSON_BYTES,
    )
    claim_path = project_root / PROJECT_CLAIM_NAME
    if project_claim_sha256 is None:
        try:
            os.lstat(claim_path)
        except FileNotFoundError:
            pass
        else:
            fail(
                "UE_COMMANDLET_PROJECT_ALREADY_CLAIMED",
                "Disposable project has already been reserved, launched, quarantined, or imported",
                pointer=str(claim_path),
            )
    entries = project_control_manifest(
        project_file,
        project_claim_sha256=project_claim_sha256,
        allow_imported_vista=allow_imported_vista,
    )
    return identity, entries, project_control_fingerprint(entries)


def _trusted_directory_entry(path: Path, relative: str) -> dict[str, Any]:
    metadata = os.lstat(path)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid not in {0, os.geteuid()}
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        fail("UE_COMMANDLET_INTERCHANGE_TREE_UNSAFE", "Interchange runtime directory is unsafe", pointer=relative)
    return {"path": relative, "type": "directory", "mode": stat.S_IMODE(metadata.st_mode)}


def interchange_runtime_manifest(engine_executable: Path) -> tuple[dict[str, Any], ...]:
    if tuple(engine_executable.parts[-4:-1]) != ("Engine", "Binaries", "Linux"):
        fail("UE_COMMANDLET_ENGINE_LAYOUT_INVALID", "Engine executable must use Engine/Binaries/Linux layout")
    engine_root = engine_executable.parents[2]
    required_files = {
        "Config/BaseEngine.ini",
        "Binaries/Linux/libUnrealEditor-InterchangeCore.so",
        "Binaries/Linux/libUnrealEditor-InterchangeEngine.so",
        "Plugins/Interchange/Runtime/Interchange.uplugin",
        "Plugins/Interchange/Runtime/Content/Pipelines/DefaultGLTFAssetsPipeline.uasset",
        "Plugins/Interchange/Runtime/Content/Pipelines/DefaultGLTFPipeline.uasset",
    }
    paths: list[Path] = [engine_root / "Config/BaseEngine.ini"]
    paths.extend(
        [
            engine_root / "Binaries/Linux/libUnrealEditor-InterchangeCore.so",
            engine_root / "Binaries/Linux/libUnrealEditor-InterchangeEngine.so",
        ]
    )
    plugin_root = engine_root / "Plugins/Interchange"
    _lstat_no_symlink_components(plugin_root, "Engine/Plugins/Interchange")
    entries: list[dict[str, Any]] = [_trusted_directory_entry(plugin_root, "Plugins/Interchange")]
    observed_files: set[str] = set()
    total_bytes = 0
    for current, directories, files in os.walk(plugin_root, topdown=True, followlinks=False):
        current_path = Path(current)
        directories.sort()
        files.sort()
        for name in directories:
            path = current_path / name
            relative = path.relative_to(engine_root).as_posix()
            entries.append(_trusted_directory_entry(path, relative))
            if len(entries) >= MAX_PROJECT_FILES * 4:
                fail("UE_COMMANDLET_INTERCHANGE_TREE_UNSAFE", "Interchange runtime influence surface exceeds fixed bounds")
        for name in files:
            paths.append(current_path / name)
    seen: set[Path] = set()
    identities: dict[str, FileIdentity] = {}
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        relative = path.relative_to(engine_root).as_posix()
        identity = inspect_regular_file(
            path,
            pointer=relative,
            require_owner=False,
            maximum_bytes=512 * 1024 * 1024,
        )
        total_bytes += identity.size
        if len(entries) >= MAX_PROJECT_FILES * 4 or total_bytes > MAX_PROJECT_TOTAL_BYTES * 4:
            fail("UE_COMMANDLET_INTERCHANGE_TREE_UNSAFE", "Interchange runtime influence surface exceeds fixed bounds")
        observed_files.add(relative)
        identities[relative] = identity
        entries.append(
            {
                "path": relative,
                "type": "file",
                "mode": identity.mode,
                "bytes": identity.size,
                "sha256": identity.sha256,
            }
        )
    if not required_files <= observed_files:
        fail("UE_COMMANDLET_INTERCHANGE_TREE_UNSAFE", "Required factory, translator, and pipeline files are missing")
    base_engine_path = engine_root / "Config/BaseEngine.ini"
    base_engine = read_stable_trusted_file(
        base_engine_path,
        pointer="Config/BaseEngine.ini",
        maximum_bytes=512 * 1024 * 1024,
        expected_identity=identities["Config/BaseEngine.ini"],
    )
    for token in (
        b"/Interchange/Pipelines/DefaultGLTFAssetsPipeline.DefaultGLTFAssetsPipeline",
        b"/Interchange/Pipelines/DefaultGLTFPipeline.DefaultGLTFPipeline",
        b"/Script/InterchangeImport.InterchangeGltfTranslator",
    ):
        if token not in base_engine:
            fail("UE_COMMANDLET_INTERCHANGE_TREE_UNSAFE", "BaseEngine does not contain the fixed glTF Interchange contract")
    return tuple(sorted(entries, key=lambda entry: (entry["path"], entry["type"])))


def interchange_runtime_fingerprint(entries: Sequence[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256(INTERCHANGE_FINGERPRINT_DOMAIN)
    for entry in entries:
        digest.update(canonical_json(dict(entry)))
    return digest.hexdigest()


def validate_engine(
    engine: Path,
    engine_sha256: str,
) -> tuple[FileIdentity, tuple[dict[str, Any], ...], str]:
    identity = inspect_regular_file(
        _safe_absolute_path(engine, "--engine-executable"),
        pointer="--engine-executable",
        expected_sha256=engine_sha256,
        require_owner=False,
        require_executable=True,
    )
    entries = interchange_runtime_manifest(engine)
    return identity, entries, interchange_runtime_fingerprint(entries)


@dataclass(frozen=True)
class ExecutionPlan:
    engine: FileIdentity
    interchange_runtime_entries: tuple[dict[str, Any], ...]
    interchange_runtime_sha256: str
    supplied_interchange_runtime_sha256: str | None
    project: FileIdentity
    project_control_entries: tuple[dict[str, Any], ...]
    project_control_sha256: str
    supplied_project_control_sha256: str | None
    execution_class: str
    job_bundle: JobBundle
    evidence_dir: Path
    timeout_seconds: int
    approval_ref_sha256: str | None


def build_execution_plan(
    *,
    manifest_path: Path,
    acquisition_dir: Path,
    job_dir: Path,
    engine_executable: Path,
    engine_sha256: str,
    interchange_tree_sha256: str | None,
    project_file: Path,
    project_sha256: str,
    project_control_sha256: str | None,
    evidence_dir: Path,
    timeout_seconds: int,
    approval_ref: str | None,
    apply: bool,
) -> ExecutionPlan:
    engine_sha256 = _safe_sha256(engine_sha256, "--engine-sha256")
    project_sha256 = _safe_sha256(project_sha256, "--project-sha256")
    if project_control_sha256 is not None:
        project_control_sha256 = _safe_sha256(project_control_sha256, "--project-tree-sha256")
    if interchange_tree_sha256 is not None:
        interchange_tree_sha256 = _safe_sha256(interchange_tree_sha256, "--interchange-tree-sha256")
    if isinstance(timeout_seconds, bool) or not MIN_TIMEOUT_SECONDS <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        fail(
            "UE_COMMANDLET_TIMEOUT_INVALID",
            f"Timeout must be between {MIN_TIMEOUT_SECONDS} and {MAX_TIMEOUT_SECONDS} seconds",
            pointer="--timeout-seconds",
        )
    if apply and not approval_ref:
        fail("UE_COMMANDLET_APPROVAL_REQUIRED", "--apply requires --approval-ref", pointer="--approval-ref")
    if apply and project_control_sha256 is None:
        fail(
            "UE_COMMANDLET_PROJECT_TREE_REQUIRED",
            "--apply requires an independently supplied --project-tree-sha256",
            pointer="--project-tree-sha256",
        )
    if apply and interchange_tree_sha256 is None:
        fail(
            "UE_COMMANDLET_INTERCHANGE_TREE_REQUIRED",
            "--apply requires an independently supplied --interchange-tree-sha256",
            pointer="--interchange-tree-sha256",
        )
    approval_sha256 = preparation.validate_approval_ref(approval_ref) if approval_ref else None
    engine, interchange_entries, observed_interchange_sha256 = validate_engine(engine_executable, engine_sha256)
    if interchange_tree_sha256 is not None and interchange_tree_sha256 != observed_interchange_sha256:
        fail(
            "UE_COMMANDLET_INTERCHANGE_TREE_MISMATCH",
            "Engine Interchange factory/translator/pipeline tree differs from its supplied fingerprint",
            pointer="--interchange-tree-sha256",
        )
    project, control_entries, observed_control_sha256 = validate_disposable_project(project_file, project_sha256)
    if project_control_sha256 is not None and project_control_sha256 != observed_control_sha256:
        fail(
            "UE_COMMANDLET_PROJECT_TREE_MISMATCH",
            "Complete disposable-project influence tree differs from its supplied fingerprint",
            pointer="--project-tree-sha256",
        )
    bundle = load_job_bundle(manifest_path, acquisition_dir, job_dir)
    validate_evidence_target(evidence_dir)
    return ExecutionPlan(
        engine=engine,
        interchange_runtime_entries=interchange_entries,
        interchange_runtime_sha256=observed_interchange_sha256,
        supplied_interchange_runtime_sha256=interchange_tree_sha256,
        project=project,
        project_control_entries=control_entries,
        project_control_sha256=observed_control_sha256,
        supplied_project_control_sha256=project_control_sha256,
        execution_class=(
            "pinned_execution_review_pending"
            if project_control_sha256 is not None and interchange_tree_sha256 is not None
            else "api_probe_review_pending"
        ),
        job_bundle=bundle,
        evidence_dir=evidence_dir,
        timeout_seconds=timeout_seconds,
        approval_ref_sha256=approval_sha256,
    )


def project_claim_value(plan: ExecutionPlan) -> dict[str, Any]:
    return {
        "schema": "simworld-ue-commandlet-project-one-shot-lock/v1",
        "status": "reserved_one_shot",
        "automatic_retry_permitted": False,
        "evidence_dir": str(plan.evidence_dir),
        "approval_ref_sha256": plan.approval_ref_sha256,
        "engine_sha256": plan.engine.sha256,
        "interchange_runtime_sha256": plan.interchange_runtime_sha256,
        "job_sha256": plan.job_bundle.prepared.job_sha256,
        "project_sha256": plan.project.sha256,
        "project_control_sha256": plan.project_control_sha256,
        "project_reuse_permitted": False,
    }


def project_claim_bytes(plan: ExecutionPlan) -> bytes:
    return canonical_json(project_claim_value(plan))


def _script_payload(plan: ExecutionPlan) -> dict[str, Any]:
    assets: list[dict[str, Any]] = []
    for asset in plan.job_bundle.prepared.job["assets"]:
        source = next(
            item for item in plan.job_bundle.source_files if item["relative_path"] == asset["source_gltf"]["path"]
        )
        assets.append(
            {
                "asset_id": asset["asset_id"],
                "source_asset_id": asset["source_asset_id"],
                "source_gltf": source,
                "destination_content_path": asset["destination_content_path"],
            }
        )
    return {
        "engine_version": ENGINE_VERSION,
        "engine_root": str(plan.engine.path.parents[2]),
        "interchange_runtime_entries": list(plan.interchange_runtime_entries),
        "interchange_runtime_sha256": plan.interchange_runtime_sha256,
        "interchange_pipelines": list(INTERCHANGE_PIPELINES),
        "interchange_pipeline_classes": list(INTERCHANGE_PIPELINE_CLASSES),
        "interchange_static_mesh_factory_class": INTERCHANGE_STATIC_MESH_FACTORY_CLASS,
        "project_file": str(plan.project.path),
        "project_sha256": plan.project.sha256,
        "project_root": str(plan.project.path.parent),
        "project_control_entries": list(plan.project_control_entries),
        "project_control_sha256": plan.project_control_sha256,
        "project_claim_path": str(plan.project.path.parent / PROJECT_CLAIM_NAME),
        "project_claim_sha256": sha256_bytes(project_claim_bytes(plan)),
        "project_required_directories": list(PROJECT_REQUIRED_DIRECTORIES),
        "job_sha256": plan.job_bundle.prepared.job_sha256,
        "source_tree_sha256": plan.job_bundle.prepared.tree_sha256,
        "source_files": list(plan.job_bundle.source_files),
        "assets": assets,
    }


def render_fixed_commandlet_script(plan: ExecutionPlan) -> bytes:
    payload_b64 = base64.b64encode(canonical_json(_script_payload(plan))).decode("ascii")
    # The payload contains only host-validated fixed paths and sealed digests.
    # It is data, never caller-authored Python, destination, or pipeline code.
    template = r'''# Generated by execute_vista_ue_interchange_commandlet_job.py; do not edit.
import base64
import hashlib
import json
import os
import stat

import unreal

MARKER_PREFIX = "VISTA_INTERCHANGE_COMMANDLET_RESULT:"
PAYLOAD = json.loads(base64.b64decode("__PAYLOAD_B64__").decode("utf-8"))


def emit(value):
    print(MARKER_PREFIX + json.dumps(value, sort_keys=True, separators=(",", ":")))


def hash_file(path, expected_size, expected_sha256, expected_mode=None, require_current_owner=True):
    metadata = os.lstat(path)
    allowed_uids = {os.geteuid()} if require_current_owner else {0, os.geteuid()}
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid not in allowed_uids
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or metadata.st_size != expected_size
        or (expected_mode is not None and stat.S_IMODE(metadata.st_mode) != expected_mode)
    ):
        raise RuntimeError("sealed_file_metadata_mismatch")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        total != expected_size
        or digest.hexdigest() != expected_sha256
        or after.st_dev != before.st_dev
        or after.st_ino != before.st_ino
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise RuntimeError("sealed_file_digest_mismatch")


def verify_project_influence_surface():
    expected_shape = [(entry["path"], entry["type"]) for entry in PAYLOAD["project_control_entries"]]
    observed_shape = [(os.path.basename(PAYLOAD["project_file"]), "file")]
    expected_top_level = {
        os.path.basename(PAYLOAD["project_file"]),
        PAYLOAD["project_claim_path"].rsplit(os.sep, 1)[-1],
        *PAYLOAD["project_required_directories"],
    }
    if set(os.listdir(PAYLOAD["project_root"])) != expected_top_level:
        raise RuntimeError("project_control_tree_mismatch")
    for relative_root in PAYLOAD["project_required_directories"]:
        root = os.path.join(PAYLOAD["project_root"], *relative_root.split("/"))
        observed_shape.append((relative_root, "directory"))
        for current, directories, files in os.walk(root, topdown=True, followlinks=False):
            directories.sort()
            files.sort()
            for name in directories:
                path = os.path.join(current, name)
                metadata = os.lstat(path)
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                    raise RuntimeError("project_control_tree_mismatch")
                observed_shape.append((os.path.relpath(path, PAYLOAD["project_root"]).replace(os.sep, "/"), "directory"))
            for name in files:
                path = os.path.join(current, name)
                metadata = os.lstat(path)
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                    raise RuntimeError("project_control_tree_mismatch")
                observed_shape.append((os.path.relpath(path, PAYLOAD["project_root"]).replace(os.sep, "/"), "file"))
    if sorted(observed_shape) != sorted(expected_shape):
        raise RuntimeError("project_control_tree_mismatch")

    observed = []
    for expected in PAYLOAD["project_control_entries"]:
        path = os.path.join(PAYLOAD["project_root"], *expected["path"].split("/"))
        metadata = os.lstat(path)
        mode = stat.S_IMODE(metadata.st_mode)
        if expected["type"] == "directory":
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or mode != expected["mode"]
            ):
                raise RuntimeError("project_control_tree_mismatch")
            observed.append({"mode": mode, "path": expected["path"], "type": "directory"})
        else:
            hash_file(path, expected["bytes"], expected["sha256"], expected["mode"])
            observed.append({
                "bytes": expected["bytes"],
                "mode": mode,
                "path": expected["path"],
                "sha256": expected["sha256"],
                "type": "file",
            })
    digest = hashlib.sha256(b"simworld-ue-commandlet-project-influence-tree/v2\0")
    for entry in observed:
        digest.update((json.dumps(entry, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"))
    if digest.hexdigest() != PAYLOAD["project_control_sha256"]:
        raise RuntimeError("project_control_tree_mismatch")
    claim_size = os.path.getsize(PAYLOAD["project_claim_path"])
    hash_file(PAYLOAD["project_claim_path"], claim_size, PAYLOAD["project_claim_sha256"], 0o600)


def verify_interchange_runtime():
    plugin_prefix = "Plugins/Interchange"
    expected_plugin_shape = sorted(
        (entry["path"], entry["type"])
        for entry in PAYLOAD["interchange_runtime_entries"]
        if entry["path"] == plugin_prefix or entry["path"].startswith(plugin_prefix + "/")
    )
    plugin_root = os.path.join(PAYLOAD["engine_root"], "Plugins", "Interchange")
    observed_plugin_shape = [(plugin_prefix, "directory")]
    for current, directories, files in os.walk(plugin_root, topdown=True, followlinks=False):
        directories.sort()
        files.sort()
        for name in directories:
            path = os.path.join(current, name)
            metadata = os.lstat(path)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise RuntimeError("interchange_runtime_mismatch")
            observed_plugin_shape.append((os.path.relpath(path, PAYLOAD["engine_root"]).replace(os.sep, "/"), "directory"))
        for name in files:
            path = os.path.join(current, name)
            metadata = os.lstat(path)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise RuntimeError("interchange_runtime_mismatch")
            observed_plugin_shape.append((os.path.relpath(path, PAYLOAD["engine_root"]).replace(os.sep, "/"), "file"))
    if sorted(observed_plugin_shape) != expected_plugin_shape:
        raise RuntimeError("interchange_runtime_mismatch")

    observed = []
    for expected in PAYLOAD["interchange_runtime_entries"]:
        path = os.path.join(PAYLOAD["engine_root"], *expected["path"].split("/"))
        metadata = os.lstat(path)
        mode = stat.S_IMODE(metadata.st_mode)
        if expected["type"] == "directory":
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or metadata.st_uid not in {0, os.geteuid()}
                or mode != expected["mode"]
            ):
                raise RuntimeError("interchange_runtime_mismatch")
            observed.append({"mode": mode, "path": expected["path"], "type": "directory"})
        else:
            hash_file(
                path,
                expected["bytes"],
                expected["sha256"],
                expected["mode"],
                require_current_owner=False,
            )
            observed.append({
                "bytes": expected["bytes"],
                "mode": mode,
                "path": expected["path"],
                "sha256": expected["sha256"],
                "type": "file",
            })
    digest = hashlib.sha256(b"simworld-ue-commandlet-interchange-runtime-tree/v1\0")
    for entry in observed:
        digest.update((json.dumps(entry, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"))
    if digest.hexdigest() != PAYLOAD["interchange_runtime_sha256"]:
        raise RuntimeError("interchange_runtime_mismatch")


completed = []
try:
    engine_version = unreal.SystemLibrary.get_engine_version()
    if not (engine_version == PAYLOAD["engine_version"] or engine_version.startswith(PAYLOAD["engine_version"] + "-")):
        raise RuntimeError("engine_version_mismatch")
    observed_project = os.path.abspath(unreal.Paths.get_project_file_path())
    if observed_project != PAYLOAD["project_file"]:
        raise RuntimeError("project_identity_mismatch")
    hash_file(PAYLOAD["project_file"], os.path.getsize(PAYLOAD["project_file"]), PAYLOAD["project_sha256"])
    verify_project_influence_surface()
    verify_interchange_runtime()

    # This complete 15-file pass is deliberately contiguous and precedes the
    # first destination check/import.  No source is trusted from host state.
    for source in PAYLOAD["source_files"]:
        hash_file(source["absolute_path"], source["bytes"], source["sha256"], 0o600)
    for asset in PAYLOAD["assets"]:
        if unreal.EditorAssetLibrary.does_directory_exist(asset["destination_content_path"]):
            raise RuntimeError("destination_already_exists")

    manager = unreal.InterchangeManager.get_interchange_manager_scripted()
    factory_class = manager.get_registered_factory_class(unreal.StaticMesh.static_class())
    expected_factory_class = unreal.load_class(None, PAYLOAD["interchange_static_mesh_factory_class"])
    if factory_class is None or expected_factory_class is None or factory_class != expected_factory_class:
        raise RuntimeError("interchange_factory_mismatch")
    pipeline_paths = []
    for pipeline_path, expected_class in zip(
        PAYLOAD["interchange_pipelines"],
        PAYLOAD["interchange_pipeline_classes"],
    ):
        pipeline = unreal.EditorAssetLibrary.load_asset(pipeline_path)
        expected_pipeline_class = unreal.load_class(None, expected_class)
        if pipeline is None or expected_pipeline_class is None or pipeline.get_class() != expected_pipeline_class:
            raise RuntimeError("interchange_pipeline_mismatch")
        pipeline_paths.append(unreal.SoftObjectPath(pipeline_path))

    for asset in PAYLOAD["assets"]:
        source_data = manager.create_source_data(asset["source_gltf"]["absolute_path"])
        if source_data is None:
            raise RuntimeError("interchange_source_data_failed")
        parameters = unreal.ImportAssetParameters()
        parameters.set_editor_property("is_automated", True)
        parameters.set_editor_property("follow_redirectors", False)
        parameters.set_editor_property("reimport_asset", None)
        parameters.set_editor_property("reimport_source_index", 0)
        parameters.set_editor_property("override_pipelines", pipeline_paths)
        if not manager.import_asset(asset["destination_content_path"], source_data, parameters):
            raise RuntimeError("interchange_import_failed")
        packages = sorted(unreal.EditorAssetLibrary.list_assets(asset["destination_content_path"], recursive=True, include_folder=False))
        if not unreal.EditorAssetLibrary.save_directory(asset["destination_content_path"], only_if_is_dirty=False, recursive=True):
            raise RuntimeError("interchange_save_failed")
        records = []
        for package in packages:
            value = unreal.EditorAssetLibrary.load_asset(package)
            if value is not None:
                records.append({"class": value.get_class().get_name(), "object_path": value.get_path_name()})
        records = sorted(records, key=lambda item: (item["object_path"], item["class"]))
        if (
            not records
            or not packages
            or not any(item["class"] == "StaticMesh" for item in records)
            or any(item["class"] == "ObjectRedirector" for item in records)
        ):
            raise RuntimeError("post_import_object_contract_failed")
        completed.append({
            "asset_id": asset["asset_id"],
            "destination_content_path": asset["destination_content_path"],
            "object_records": records,
            "package_paths": packages,
            "source_gltf_sha256": asset["source_gltf"]["sha256"],
        })

    emit({
        "assets": completed,
        "job_sha256": PAYLOAD["job_sha256"],
        "post_import_review": {
            "collision": "review_pending",
            "pipeline_fingerprint": "review_pending",
            "pbr_channels": "review_pending",
            "rendered_visual": "review_pending",
            "scale_and_bounds": "review_pending",
        },
        "production_ready": False,
        "project_sha256": PAYLOAD["project_sha256"],
        "schema": "simworld-ue-interchange-commandlet-marker/v1",
        "semantic_index_eligible": False,
        "source_file_count": len(PAYLOAD["source_files"]),
        "source_tree_sha256": PAYLOAD["source_tree_sha256"],
        "status": "imported_pending_review",
    })
except Exception:
    emit({
        "completed_asset_ids": [item["asset_id"] for item in completed],
        "error_code": "UE_COMMANDLET_IMPORT_FAILED",
        "job_sha256": PAYLOAD["job_sha256"],
        "production_ready": False,
        "schema": "simworld-ue-interchange-commandlet-marker/v1",
        "semantic_index_eligible": False,
        "status": "failed_after_launch",
    })
    raise
'''
    return template.replace("__PAYLOAD_B64__", payload_b64).encode("utf-8")


def _write_exclusive(path: Path, raw: bytes, *, mode: int = 0o600) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
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


@dataclass(frozen=True)
class EvidencePaths:
    root: Path
    script: Path
    intent: Path
    stdout: Path
    stderr: Path
    unreal_log: Path
    terminal_receipt: Path
    home: Path
    tmp: Path
    ue_user: Path


def evidence_paths(root: Path) -> EvidencePaths:
    return EvidencePaths(
        root=root,
        script=root / "commandlet-import.py",
        intent=root / "execution-intent.json",
        stdout=root / "stdout.log",
        stderr=root / "stderr.log",
        unreal_log=root / "unreal.log",
        terminal_receipt=root / "terminal-receipt.json",
        home=root / "home",
        tmp=root / "tmp",
        ue_user=root / "ue-user",
    )


def fixed_environment(paths: EvidencePaths) -> dict[str, str]:
    return {
        "HOME": str(paths.home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "TMPDIR": str(paths.tmp),
        "XDG_CACHE_HOME": str(paths.home / ".cache"),
        "XDG_CONFIG_HOME": str(paths.home / ".config"),
        "XDG_DATA_HOME": str(paths.home / ".local/share"),
    }


def fixed_command(plan: ExecutionPlan, paths: EvidencePaths) -> tuple[str, ...]:
    return (
        str(plan.engine.path),
        str(plan.project.path),
        "-run=pythonscript",
        f"-script={paths.script}",
        "-unattended",
        "-nop4",
        "-nosplash",
        "-nullrhi",
        "-NoSound",
        "-stdout",
        "-FullStdOutLogOutput",
        f"-abslog={paths.unreal_log}",
        f"-userdir={paths.ue_user}",
    )


def _intent(plan: ExecutionPlan, paths: EvidencePaths, script_sha256: str) -> dict[str, Any]:
    return {
        "schema": INTENT_SCHEMA,
        "approval_ref_sha256": plan.approval_ref_sha256,
        "command": list(fixed_command(plan, paths)),
        "environment": fixed_environment(paths),
        "working_directory": str(plan.project.path.parent),
        "engine": {
            "path": str(plan.engine.path),
            "sha256": plan.engine.sha256,
            "bytes": plan.engine.size,
            "mode": plan.engine.mode,
            "uid": plan.engine.uid,
        },
        "interchange_runtime": {
            "sha256": plan.interchange_runtime_sha256,
            "independently_supplied_sha256": plan.supplied_interchange_runtime_sha256,
            "factory_class": INTERCHANGE_STATIC_MESH_FACTORY_CLASS,
            "pipeline_paths": list(INTERCHANGE_PIPELINES),
            "pipeline_classes": list(INTERCHANGE_PIPELINE_CLASSES),
            "options": {
                "follow_redirectors": False,
                "is_automated": True,
                "reimport_asset": None,
                "reimport_source_index": 0,
            },
        },
        "execution_class": plan.execution_class,
        "import_contract": {
            "asset_count": len(plan.job_bundle.prepared.job["assets"]),
            "asset_ids": [asset["asset_id"] for asset in plan.job_bundle.prepared.job["assets"]],
            "destination_root": DESTINATION_ROOT,
            "job_sha256": plan.job_bundle.prepared.job_sha256,
            "source_file_count": len(plan.job_bundle.source_files),
            "source_manifest_sha256": plan.job_bundle.prepared.manifest_sha256,
            "source_tree_sha256": plan.job_bundle.prepared.tree_sha256,
        },
        "mutation_policy": {
            "automatic_retry": False,
            "delete_on_failure": False,
            "evidence_policy": "append_only_non_overwriting",
            "semantic_index_before_review": False,
        },
        "post_import_gates": {
            "collision": "review_pending",
            "pipeline_fingerprint": "review_pending",
            "pbr_channels": "review_pending",
            "rendered_visual": "review_pending",
            "scale_and_bounds": "review_pending",
        },
        "production_ready": False,
        "project": {
            "path": str(plan.project.path),
            "sha256": plan.project.sha256,
            "control_tree_sha256": plan.project_control_sha256,
            "independently_supplied_control_tree_sha256": plan.supplied_project_control_sha256,
            "content_vista_required_absent": True,
            "one_shot_lock_path": str(plan.project.path.parent / PROJECT_CLAIM_NAME),
            "one_shot_lock_sha256": sha256_bytes(project_claim_bytes(plan)),
        },
        "script_sha256": script_sha256,
        "semantic_index_eligible": False,
        "timeout_seconds": plan.timeout_seconds,
    }


def claim_project_one_shot(plan: ExecutionPlan) -> str:
    claim_path = plan.project.path.parent / PROJECT_CLAIM_NAME
    raw = project_claim_bytes(plan)
    try:
        _write_exclusive(claim_path, raw)
    except FileExistsError:
        fail(
            "UE_COMMANDLET_PROJECT_ALREADY_CLAIMED",
            "Disposable project was already reserved, launched, quarantined, or imported",
            pointer=str(claim_path),
        )
    _fsync_directory(plan.project.path.parent)
    identity = inspect_regular_file(
        claim_path,
        pointer=PROJECT_CLAIM_NAME,
        expected_sha256=sha256_bytes(raw),
        maximum_bytes=MAX_JSON_BYTES,
    )
    if identity.mode != 0o600:
        fail("UE_COMMANDLET_PROJECT_ALREADY_CLAIMED", "Project one-shot lock mode is unsafe")
    return identity.sha256


def publish_write_ahead_intent(
    plan: ExecutionPlan,
    project_claim_sha256: str,
) -> tuple[EvidencePaths, dict[str, Any], str, FileIdentity, FileIdentity]:
    if project_claim_sha256 != sha256_bytes(project_claim_bytes(plan)):
        fail("UE_COMMANDLET_PROJECT_ALREADY_CLAIMED", "Project one-shot lock differs from the planned claim")
    validate_evidence_target(plan.evidence_dir)
    plan.evidence_dir.mkdir(mode=0o700)
    os.chmod(plan.evidence_dir, 0o700)
    paths = evidence_paths(plan.evidence_dir)
    runtime_directories = (
        paths.home,
        paths.tmp,
        paths.ue_user,
        paths.home / ".cache",
        paths.home / ".config",
        paths.home / ".local",
        paths.home / ".local/share",
    )
    for directory in runtime_directories:
        directory.mkdir(mode=0o700, exist_ok=False)
    script = render_fixed_commandlet_script(plan)
    _write_exclusive(paths.script, script)
    _write_exclusive(paths.stdout, b"")
    _write_exclusive(paths.stderr, b"")
    stdout_identity = inspect_regular_file(paths.stdout, pointer="stdout.log", maximum_bytes=MAX_LOG_BYTES)
    stderr_identity = inspect_regular_file(paths.stderr, pointer="stderr.log", maximum_bytes=MAX_LOG_BYTES)
    intent = _intent(plan, paths, sha256_bytes(script))
    intent_raw = canonical_json(intent)
    # This is the write-ahead mutation boundary: it is durable before Popen.
    _write_exclusive(paths.intent, intent_raw)
    _fsync_directory(paths.root)
    _fsync_directory(paths.root.parent)
    return paths, intent, sha256_bytes(intent_raw), stdout_identity, stderr_identity


@dataclass(frozen=True)
class ProcessOutcome:
    kind: str
    returncode: int | None
    signal_number: int | None
    launch_error_code: str | None = None
    process_group_clean: bool = True
    descendants_observed: bool = False


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _wait_process_group_gone(process_group_id: int, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while True:
        if not _process_group_exists(process_group_id):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _bounded_shutdown_process_group(process_group_id: int) -> tuple[bool, bool]:
    descendants_observed = _process_group_exists(process_group_id)
    if not descendants_observed:
        return True, False
    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        return True, True
    if _wait_process_group_gone(process_group_id, TERMINATION_GRACE_SECONDS):
        return True, True
    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        return True, True
    return _wait_process_group_gone(process_group_id, KILL_REAP_GRACE_SECONDS), True


def run_commandlet(
    command: Sequence[str],
    environment: Mapping[str, str],
    stdout_path: Path,
    stderr_path: Path,
    timeout_seconds: int,
    *,
    cwd: Path,
) -> ProcessOutcome:
    if LIVE_APPLY_QUARANTINED:
        fail(
            LIVE_APPLY_QUARANTINE_CODE,
            "Live commandlet execution is quarantined pending independent P0 closure",
        )
    process: subprocess.Popen[bytes] | None = None
    try:
        with stdout_path.open("ab", buffering=0) as stdout_handle, stderr_path.open("ab", buffering=0) as stderr_handle:
            process = subprocess.Popen(
                list(command),
                cwd=str(cwd),
                env=dict(environment),
                stdin=subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                close_fds=True,
                start_new_session=True,
            )
            try:
                returncode = process.wait(timeout=timeout_seconds)
                signal_number = -returncode if returncode < 0 else None
                group_clean, descendants = _bounded_shutdown_process_group(process.pid)
                if not group_clean:
                    return ProcessOutcome(
                        "unreaped_process_group",
                        returncode,
                        signal_number,
                        "UE_COMMANDLET_PROCESS_GROUP_UNREAPED",
                        False,
                        descendants,
                    )
                if descendants:
                    return ProcessOutcome(
                        "descendants_terminated",
                        returncode,
                        signal_number,
                        "UE_COMMANDLET_DESCENDANTS_OBSERVED",
                        True,
                        True,
                    )
                return ProcessOutcome("exited", returncode, signal_number)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=TERMINATION_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    try:
                        process.wait(timeout=KILL_REAP_GRACE_SECONDS)
                    except subprocess.TimeoutExpired:
                        _wait_process_group_gone(process.pid, KILL_REAP_GRACE_SECONDS)
                        return ProcessOutcome(
                            "timeout_unreaped",
                            process.poll(),
                            None,
                            "UE_COMMANDLET_PROCESS_GROUP_UNREAPED",
                            False,
                            True,
                        )
                group_clean, descendants = _bounded_shutdown_process_group(process.pid)
                return ProcessOutcome(
                    "timeout" if group_clean else "timeout_unreaped",
                    process.returncode,
                    None,
                    None if group_clean else "UE_COMMANDLET_PROCESS_GROUP_UNREAPED",
                    group_clean,
                    descendants,
                )
    except OSError:
        if process is not None:
            try:
                group_clean, descendants = _bounded_shutdown_process_group(process.pid)
            except Exception:
                group_clean, descendants = False, True
            return ProcessOutcome(
                "runner_error" if group_clean else "unreaped_process_group",
                process.poll(),
                None,
                "UE_COMMANDLET_RUNNER_FAILED" if group_clean else "UE_COMMANDLET_PROCESS_GROUP_UNREAPED",
                group_clean,
                descendants,
            )
        return ProcessOutcome(
            "launch_error",
            None,
            None,
            "UE_COMMANDLET_LAUNCH_FAILED",
            True,
            False,
        )


Runner = Callable[[Sequence[str], Mapping[str, str], Path, Path, int], ProcessOutcome]


def validate_process_outcome(value: Any) -> ProcessOutcome:
    allowed = {
        "exited",
        "timeout",
        "timeout_unreaped",
        "launch_error",
        "runner_error",
        "descendants_terminated",
        "unreaped_process_group",
    }
    if not isinstance(value, ProcessOutcome) or value.kind not in allowed:
        fail("UE_COMMANDLET_RUNNER_INVALID", "Runner returned an invalid process outcome")
    if value.returncode is not None and (isinstance(value.returncode, bool) or not isinstance(value.returncode, int)):
        fail("UE_COMMANDLET_RUNNER_INVALID", "Runner return code is invalid")
    if value.signal_number is not None and (
        isinstance(value.signal_number, bool) or not isinstance(value.signal_number, int) or value.signal_number <= 0
    ):
        fail("UE_COMMANDLET_RUNNER_INVALID", "Runner signal is invalid")
    if not isinstance(value.process_group_clean, bool) or not isinstance(value.descendants_observed, bool):
        fail("UE_COMMANDLET_RUNNER_INVALID", "Runner process-group evidence is invalid")
    return value


def _default_runner(plan: ExecutionPlan, paths: EvidencePaths) -> ProcessOutcome:
    return run_commandlet(
        fixed_command(plan, paths),
        fixed_environment(paths),
        paths.stdout,
        paths.stderr,
        plan.timeout_seconds,
        cwd=plan.project.path.parent,
    )


def _bounded_string(value: Any, pointer: str, *, maximum: int = 1024) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        fail("UE_COMMANDLET_MARKER_INVALID", "Marker string is invalid", pointer=pointer)
    return value


def _marker_asset(value: Any, expected: Mapping[str, Any], pointer: str) -> dict[str, Any]:
    asset = exact_object(
        value,
        {"asset_id", "destination_content_path", "object_records", "package_paths", "source_gltf_sha256"},
        pointer,
    )
    if (
        asset["asset_id"] != expected["asset_id"]
        or asset["destination_content_path"] != expected["destination_content_path"]
        or asset["source_gltf_sha256"] != expected["source_gltf"]["sha256"]
    ):
        fail("UE_COMMANDLET_MARKER_INVALID", "Marker asset identity differs from the sealed job", pointer=pointer)
    records = asset["object_records"]
    packages = asset["package_paths"]
    if (
        not isinstance(records, list)
        or not 1 <= len(records) <= 256
        or not isinstance(packages, list)
        or not 1 <= len(packages) <= 512
    ):
        fail("UE_COMMANDLET_MARKER_INVALID", "Marker object/package evidence is missing or unbounded", pointer=pointer)
    normalized_records: list[dict[str, str]] = []
    for index, raw in enumerate(records):
        record = exact_object(raw, {"class", "object_path"}, f"{pointer}/object_records/{index}")
        class_name = _bounded_string(record["class"], f"{pointer}/object_records/{index}/class", maximum=128)
        object_path = _bounded_string(record["object_path"], f"{pointer}/object_records/{index}/object_path")
        if not object_path.startswith(expected["destination_content_path"] + "/"):
            fail("UE_COMMANDLET_MARKER_INVALID", "Returned object escaped its fixed destination", pointer=pointer)
        normalized_records.append({"class": class_name, "object_path": object_path})
    if (
        normalized_records != sorted(normalized_records, key=lambda item: (item["object_path"], item["class"]))
        or len({(item["object_path"], item["class"]) for item in normalized_records}) != len(normalized_records)
        or not any(item["class"] == "StaticMesh" for item in normalized_records)
        or any(item["class"] == "ObjectRedirector" for item in normalized_records)
    ):
        fail("UE_COMMANDLET_MARKER_INVALID", "Returned objects violate the fixed StaticMesh contract", pointer=pointer)
    normalized_packages = [_bounded_string(item, f"{pointer}/package_paths") for item in packages]
    if (
        normalized_packages != sorted(normalized_packages)
        or len(set(normalized_packages)) != len(normalized_packages)
        or any(not item.startswith(expected["destination_content_path"] + "/") for item in normalized_packages)
    ):
        fail("UE_COMMANDLET_MARKER_INVALID", "Package paths violate the fixed destination contract", pointer=pointer)
    return asset


def validate_marker(value: Any, plan: ExecutionPlan) -> tuple[dict[str, Any], str]:
    if not isinstance(value, dict):
        fail("UE_COMMANDLET_MARKER_INVALID", "Commandlet marker must be an object")
    common = {
        "schema",
        "status",
        "job_sha256",
        "production_ready",
        "semantic_index_eligible",
    }
    if (
        value.get("schema") != MARKER_SCHEMA
        or value.get("production_ready") is not False
        or value.get("semantic_index_eligible") is not False
    ):
        fail("UE_COMMANDLET_MARKER_INVALID", "Marker readiness fields violate the review-pending contract")
    if value.get("job_sha256") != plan.job_bundle.prepared.job_sha256:
        fail("UE_COMMANDLET_MARKER_INVALID", "Marker job digest differs from the sealed job")
    if value.get("status") == "failed_after_launch":
        marker = exact_object(value, common | {"completed_asset_ids", "error_code"}, "marker")
        completed = marker["completed_asset_ids"]
        expected_ids = [asset["asset_id"] for asset in plan.job_bundle.prepared.job["assets"]]
        if (
            marker["error_code"] != "UE_COMMANDLET_IMPORT_FAILED"
            or not isinstance(completed, list)
            or len(completed) >= len(expected_ids)
            or completed != expected_ids[: len(completed)]
        ):
            fail("UE_COMMANDLET_MARKER_INVALID", "Failure marker shape or completed prefix is invalid")
        return marker, "partial" if completed else "ambiguous"
    if value.get("status") != "imported_pending_review":
        fail("UE_COMMANDLET_MARKER_INVALID", "Marker status is unsupported")
    marker = exact_object(
        value,
        common
        | {
            "assets",
            "post_import_review",
            "project_sha256",
            "source_file_count",
            "source_tree_sha256",
        },
        "marker",
    )
    if (
        marker["project_sha256"] != plan.project.sha256
        or marker["source_file_count"] != PINNED_FILE_COUNT
        or marker["source_tree_sha256"] != plan.job_bundle.prepared.tree_sha256
    ):
        fail("UE_COMMANDLET_MARKER_INVALID", "Marker project/source binding differs from the sealed execution")
    review = exact_object(
        marker["post_import_review"],
        {"collision", "pipeline_fingerprint", "pbr_channels", "rendered_visual", "scale_and_bounds"},
        "marker/post_import_review",
    )
    if set(review.values()) != {"review_pending"}:
        fail("UE_COMMANDLET_MARKER_INVALID", "Post-import gates must remain review_pending")
    assets = marker["assets"]
    expected_assets = plan.job_bundle.prepared.job["assets"]
    if not isinstance(assets, list) or len(assets) != len(expected_assets):
        fail("UE_COMMANDLET_MARKER_INVALID", "Marker must contain exactly three ordered asset results")
    for index, (asset, expected) in enumerate(zip(assets, expected_assets)):
        _marker_asset(asset, expected, f"marker/assets/{index}")
    return marker, "imported_unreviewed"


def parse_single_marker(
    stdout_path: Path,
    plan: ExecutionPlan,
    expected_identity: FileIdentity,
) -> tuple[dict[str, Any], str]:
    try:
        raw, _identity = read_stable_private_file(
            stdout_path,
            pointer="stdout.log",
            maximum_bytes=MAX_LOG_BYTES,
            expected_identity=expected_identity,
        )
    except CommandletExecutionError:
        raise
    except OSError:
        fail("UE_COMMANDLET_MARKER_INVALID", "Commandlet stdout could not be read safely")
    candidates: list[bytes] = []
    unanchored_marker = False
    for raw_line in raw.splitlines():
        line = raw_line.rstrip(b"\r")
        if line.startswith(MARKER_PREFIX):
            candidates.append(line[len(MARKER_PREFIX) :])
        elif line.startswith(MARKER_LOG_PREFIX):
            candidates.append(line[len(MARKER_LOG_PREFIX) :])
        elif MARKER_PREFIX in line:
            unanchored_marker = True
    if unanchored_marker:
        fail("UE_COMMANDLET_MARKER_INVALID", "Commandlet marker prefix appeared on an unanchored line")
    if not candidates:
        fail("UE_COMMANDLET_MARKER_MISSING", "Commandlet produced no terminal marker")
    if len(candidates) != 1:
        fail("UE_COMMANDLET_MARKER_INVALID", "Commandlet produced multiple terminal markers")
    return validate_marker(strict_json(candidates[0], pointer="stdout-marker"), plan)


def _safe_log_digest(
    path: Path,
    expected_identity: FileIdentity | None = None,
) -> dict[str, Any] | None:
    try:
        raw, identity = read_stable_private_file(
            path,
            pointer=path.name,
            maximum_bytes=MAX_LOG_BYTES,
            expected_identity=expected_identity,
        )
        return {"status": "captured", "bytes": identity.size, "sha256": sha256_bytes(raw)}
    except CommandletExecutionError as error:
        if error.code == "UE_COMMANDLET_PATH_INVALID":
            return None
        return {"status": "unsafe_or_unbounded", "error_code": error.code}
    except Exception:
        return {"status": "observation_failed", "error_code": "UE_COMMANDLET_LOG_OBSERVATION_FAILED"}


def observe_vista_content(project_root: Path) -> dict[str, Any]:
    root = project_root / "Content" / "VISTA"
    try:
        metadata = os.lstat(root)
    except FileNotFoundError:
        return {"exists": False, "regular_file_count": 0, "relative_paths": [], "unsafe_entry_observed": False}
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        return {"exists": True, "regular_file_count": 0, "relative_paths": [], "unsafe_entry_observed": True}
    relative_paths: list[str] = []
    unsafe = metadata.st_uid != os.geteuid()
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        directories.sort()
        files.sort()
        for directory in directories:
            entry = os.lstat(current_path / directory)
            if stat.S_ISLNK(entry.st_mode) or not stat.S_ISDIR(entry.st_mode) or entry.st_uid != os.geteuid():
                unsafe = True
        for filename in files:
            path = current_path / filename
            entry = os.lstat(path)
            if stat.S_ISLNK(entry.st_mode) or not stat.S_ISREG(entry.st_mode) or entry.st_uid != os.geteuid():
                unsafe = True
            elif path.suffix == ".uasset":
                relative_paths.append(path.relative_to(root).as_posix())
    return {
        "exists": True,
        "regular_file_count": len(relative_paths),
        "relative_paths": relative_paths[:4096],
        "unsafe_entry_observed": unsafe or len(relative_paths) > 4096,
    }


def safe_observe_vista_content(project_root: Path) -> dict[str, Any]:
    try:
        return observe_vista_content(project_root)
    except Exception:
        return {
            "exists": None,
            "regular_file_count": 0,
            "relative_paths": [],
            "unsafe_entry_observed": True,
            "observation_error": "UE_COMMANDLET_CONTENT_OBSERVATION_FAILED",
        }


def _success_content_shape(marker: Mapping[str, Any], observation: Mapping[str, Any]) -> bool:
    if not observation["exists"] or observation["regular_file_count"] < len(PINNED_ASSET_IDS) or observation["unsafe_entry_observed"]:
        return False
    relative_paths = observation["relative_paths"]
    for asset in marker["assets"]:
        relative_destination = asset["destination_content_path"][len("/Game/VISTA/") :]
        if not any(path.startswith(relative_destination + "/") for path in relative_paths):
            return False
    return True


def _write_terminal_receipt(paths: EvidencePaths, receipt: Mapping[str, Any]) -> str:
    raw = canonical_json(dict(receipt))
    temporary = paths.root / ".terminal-receipt.json.tmp"
    _write_exclusive(temporary, raw)
    _fsync_directory(paths.root)
    try:
        os.link(temporary, paths.terminal_receipt, follow_symlinks=False)
    except FileExistsError:
        fail("UE_COMMANDLET_TERMINAL_EXISTS", "Terminal receipt publication is non-overwriting")
    except OSError:
        fail("UE_COMMANDLET_TERMINAL_PUBLISH_FAILED", "Terminal receipt could not be atomically published")
    _fsync_directory(paths.root)
    try:
        os.unlink(temporary)
    except OSError:
        # The final receipt is already complete and durably linked.  A leftover
        # private temp is evidence, never a reason to hide the terminal result.
        pass
    _fsync_directory(paths.root)
    _fsync_directory(paths.root.parent)
    return sha256_bytes(raw)


def _post_launch_identity_unchanged(plan: ExecutionPlan, project_claim_sha256: str) -> bool:
    try:
        engine, interchange_entries, interchange_sha = validate_engine(plan.engine.path, plan.engine.sha256)
        project, entries, control_sha = validate_disposable_project_after_launch(
            plan.project.path,
            plan.project.sha256,
            project_claim_sha256,
        )
        acquisition_ok = acquisition.verify_existing(plan.job_bundle.acquisition_plan)
    except Exception:
        return False
    return (
        acquisition_ok
        and engine.device == plan.engine.device
        and engine.inode == plan.engine.inode
        and interchange_sha == plan.interchange_runtime_sha256
        and interchange_entries == plan.interchange_runtime_entries
        and project.device == plan.project.device
        and project.inode == plan.project.inode
        and control_sha == plan.project_control_sha256
        and entries == plan.project_control_entries
    )


def validate_disposable_project_after_launch(
    project_file: Path,
    project_sha256: str,
    project_claim_sha256: str,
) -> tuple[FileIdentity, tuple[dict[str, Any], ...], str]:
    return validate_disposable_project(
        project_file,
        project_sha256,
        project_claim_sha256=project_claim_sha256,
        allow_imported_vista=True,
    )


def execute_apply(
    plan: ExecutionPlan,
    *,
    runner: Runner | None = None,
) -> tuple[dict[str, Any], int]:
    if LIVE_APPLY_QUARANTINED:
        fail(
            LIVE_APPLY_QUARANTINE_CODE,
            "Live commandlet execution is quarantined pending independent P0 closure",
        )
    project_claim_sha256 = claim_project_one_shot(plan)
    paths, _intent_value, intent_sha256, stdout_identity, stderr_identity = publish_write_ahead_intent(
        plan,
        project_claim_sha256,
    )
    unreal_started = False
    process_outcome = ProcessOutcome("not_started", None, None)
    marker: dict[str, Any] | None = None
    mutation_state = "none"
    error: dict[str, str] | None = None
    identity_unchanged = False
    try:
        # Revalidate after the intent is durable and immediately before Popen.
        engine, interchange_entries, interchange_sha = validate_engine(plan.engine.path, plan.engine.sha256)
        if (
            engine.device != plan.engine.device
            or engine.inode != plan.engine.inode
            or interchange_entries != plan.interchange_runtime_entries
            or interchange_sha != plan.interchange_runtime_sha256
        ):
            fail("UE_COMMANDLET_INTERCHANGE_TREE_MISMATCH", "Engine Interchange runtime changed before launch")
        project, project_entries, project_sha = validate_disposable_project(
            plan.project.path,
            plan.project.sha256,
            project_claim_sha256=project_claim_sha256,
        )
        if (
            project.device != plan.project.device
            or project.inode != plan.project.inode
            or project_entries != plan.project_control_entries
            or project_sha != plan.project_control_sha256
        ):
            fail("UE_COMMANDLET_PROJECT_TREE_MISMATCH", "Project influence surface changed before launch")
        if not acquisition.verify_existing(plan.job_bundle.acquisition_plan):
            fail("UE_COMMANDLET_ACQUISITION_MISSING", "Pinned acquisition disappeared before launch")
        observed_script = inspect_regular_file(
            paths.script,
            pointer="commandlet-import.py",
            expected_sha256=sha256_bytes(render_fixed_commandlet_script(plan)),
            maximum_bytes=MAX_JSON_BYTES,
        )
        if observed_script.mode != 0o600:
            fail("UE_COMMANDLET_SCRIPT_CHANGED", "Generated commandlet script mode changed before launch")
        unreal_started = True
        if runner is None:
            candidate_outcome = _default_runner(plan, paths)
        else:
            candidate_outcome = runner(
                fixed_command(plan, paths),
                fixed_environment(paths),
                paths.stdout,
                paths.stderr,
                plan.timeout_seconds,
            )
        process_outcome = validate_process_outcome(candidate_outcome)
    except CommandletExecutionError as caught:
        error = caught.public_dict()
    except (acquisition.AcquisitionError, preparation.ImportJobError):
        error = {"code": "UE_COMMANDLET_PRELAUNCH_TAMPER", "message": "Pinned input changed before launch"}
    except Exception:
        error = {"code": "UE_COMMANDLET_RUNNER_FAILED", "message": "Commandlet runner failed after intent publication"}

    try:
        observation = safe_observe_vista_content(plan.project.path.parent)
    except Exception:
        observation = {
            "exists": None,
            "regular_file_count": 0,
            "relative_paths": [],
            "unsafe_entry_observed": True,
            "observation_error": "UE_COMMANDLET_CONTENT_OBSERVATION_FAILED",
        }
    if unreal_started:
        identity_unchanged = _post_launch_identity_unchanged(plan, project_claim_sha256)
        parsed_marker: dict[str, Any] | None = None
        parsed_mutation_state = "ambiguous"
        marker_error: CommandletExecutionError | None = None
        if process_outcome.kind == "exited" and identity_unchanged:
            try:
                parsed_marker, parsed_mutation_state = parse_single_marker(
                    paths.stdout,
                    plan,
                    stdout_identity,
                )
            except CommandletExecutionError as caught:
                marker_error = caught
            except Exception:
                marker_error = CommandletExecutionError(
                    "UE_COMMANDLET_MARKER_OBSERVATION_FAILED",
                    "Commandlet marker observation failed",
                )

        if process_outcome.kind == "exited" and process_outcome.returncode == 0 and identity_unchanged:
            if marker_error is not None:
                error = marker_error.public_dict()
                mutation_state = "ambiguous"
            elif parsed_marker is None:
                error = {"code": "UE_COMMANDLET_MARKER_MISSING", "message": "Commandlet produced no terminal marker"}
                mutation_state = "ambiguous"
            elif parsed_marker["status"] == "imported_pending_review":
                try:
                    content_shape_valid = _success_content_shape(parsed_marker, observation)
                except Exception:
                    content_shape_valid = False
                if content_shape_valid:
                    marker = parsed_marker
                    mutation_state = parsed_mutation_state
                else:
                    error = {
                        "code": "UE_COMMANDLET_CONTENT_EVIDENCE_INVALID",
                        "message": "Saved Content/VISTA does not match the marker",
                    }
                    mutation_state = "ambiguous"
            else:
                marker = parsed_marker
                mutation_state = parsed_mutation_state
                error = {
                    "code": "UE_COMMANDLET_IMPORT_FAILED",
                    "message": "Commandlet reported a failed import after launch",
                }
        else:
            # A strict failure marker can establish an ordered completed prefix
            # even when UE exits nonzero.  It never makes retry safe.  A success
            # marker accompanying a nonzero exit is contradictory and ignored.
            if parsed_marker is not None and parsed_marker["status"] == "failed_after_launch":
                marker = parsed_marker
                mutation_state = parsed_mutation_state
            else:
                mutation_state = "ambiguous"
            if error is None:
                if process_outcome.kind in {"timeout", "timeout_unreaped"}:
                    error = {"code": "UE_COMMANDLET_TIMEOUT", "message": "Commandlet exceeded its bounded timeout"}
                    if process_outcome.kind == "timeout_unreaped":
                        error = {
                            "code": "UE_COMMANDLET_PROCESS_GROUP_UNREAPED",
                            "message": "Commandlet process group could not be reaped within the fixed bound",
                        }
                elif process_outcome.kind in {"descendants_terminated", "unreaped_process_group"}:
                    error = {
                        "code": process_outcome.launch_error_code or "UE_COMMANDLET_PROCESS_GROUP_UNSAFE",
                        "message": "Commandlet left process-group descendants after leader exit",
                    }
                elif process_outcome.signal_number is not None:
                    error = {"code": "UE_COMMANDLET_SIGNAL", "message": "Commandlet exited after a signal"}
                elif process_outcome.kind == "launch_error":
                    error = {"code": "UE_COMMANDLET_LAUNCH_FAILED", "message": "Commandlet could not be launched"}
                elif not identity_unchanged:
                    error = {"code": "UE_COMMANDLET_IDENTITY_CHANGED", "message": "A sealed execution input changed after launch"}
                else:
                    error = {"code": "UE_COMMANDLET_NONZERO", "message": "Commandlet exited nonzero"}
    elif error is None:
        error = {"code": "UE_COMMANDLET_NOT_STARTED", "message": "Commandlet was not started"}

    full_import_observed = marker is not None and marker.get("status") == "imported_pending_review"
    status = "imported_pending_review" if full_import_observed else ("prelaunch_failed" if not unreal_started else "quarantined")
    # Every apply is a one-shot disposable-project claim.  Even a complete
    # import remains quarantined for visual/PBR review and can never be reused.
    quarantine_required = True
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "status": status,
        "execution_class": plan.execution_class,
        "intent_sha256": intent_sha256,
        "job_sha256": plan.job_bundle.prepared.job_sha256,
        "engine_sha256": plan.engine.sha256,
        "interchange_runtime_sha256": plan.interchange_runtime_sha256,
        "interchange_runtime_pin_supplied": plan.supplied_interchange_runtime_sha256 is not None,
        "project_sha256": plan.project.sha256,
        "project_control_sha256": plan.project_control_sha256,
        "project_control_pin_supplied": plan.supplied_project_control_sha256 is not None,
        "project_one_shot_lock_sha256": project_claim_sha256,
        "project_reuse_permitted": False,
        "unreal_started": unreal_started,
        "process": {
            "kind": process_outcome.kind,
            "returncode": process_outcome.returncode,
            "signal": process_outcome.signal_number,
            "process_group_clean": process_outcome.process_group_clean,
            "descendants_observed": process_outcome.descendants_observed,
        },
        "mutation_state": mutation_state,
        "quarantine_required": quarantine_required,
        "automatic_retry_permitted": False,
        "delete_on_failure_permitted": False,
        "identity_unchanged_after_launch": identity_unchanged if unreal_started else None,
        "content_vista_observation": observation,
        "marker": marker,
        "error": error,
        "logs": {
            "stdout": _safe_log_digest(paths.stdout, stdout_identity),
            "stderr": _safe_log_digest(paths.stderr, stderr_identity),
            "unreal": _safe_log_digest(paths.unreal_log),
        },
        "review_gates": {
            "collision": "review_pending",
            "pipeline_fingerprint": "review_pending",
            "pbr_channels": "review_pending",
            "rendered_visual": "review_pending",
            "scale_and_bounds": "review_pending",
        },
        "production_ready": False,
        "semantic_index_eligible": False,
        "finished_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    receipt_sha256 = _write_terminal_receipt(paths, receipt)
    result = {
        "schema": RESULT_SCHEMA,
        "valid": full_import_observed,
        "status": status,
        "execution_class": plan.execution_class,
        "evidence_dir": str(paths.root),
        "terminal_receipt_sha256": receipt_sha256,
        "mutation_state": mutation_state,
        "quarantine_required": quarantine_required,
        "automatic_retry_permitted": False,
        "project_reuse_permitted": False,
        "production_ready": False,
        "semantic_index_eligible": False,
    }
    return result, 0 if full_import_observed else 2


def dry_run_result(plan: ExecutionPlan) -> dict[str, Any]:
    return {
        "schema": RESULT_SCHEMA,
        "valid": True,
        "status": "dry_run",
        "execution_class": plan.execution_class,
        "engine_sha256": plan.engine.sha256,
        "observed_interchange_runtime_sha256": plan.interchange_runtime_sha256,
        "interchange_runtime_pin_supplied": plan.supplied_interchange_runtime_sha256 is not None,
        "project_sha256": plan.project.sha256,
        "observed_project_control_sha256": plan.project_control_sha256,
        "project_control_pin_supplied": plan.supplied_project_control_sha256 is not None,
        "job_sha256": plan.job_bundle.prepared.job_sha256,
        "source_file_count": len(plan.job_bundle.source_files),
        "asset_count": len(plan.job_bundle.prepared.job["assets"]),
        "evidence_written": False,
        "unreal_started": False,
        "content_imported": False,
        "review_gates": {
            "collision": "review_pending",
            "pipeline_fingerprint": "review_pending",
            "pbr_channels": "review_pending",
            "rendered_visual": "review_pending",
            "scale_and_bounds": "review_pending",
        },
        "production_ready": False,
        "semantic_index_eligible": False,
    }


class SafeParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        fail("UE_COMMANDLET_ARGUMENT_INVALID", "CLI arguments are missing or invalid; consult --help")


def build_parser() -> argparse.ArgumentParser:
    parser = SafeParser(description=__doc__)
    default_manifest = Path(__file__).resolve().parent / "assets" / "vista_mmg_040_cc0_bootstrap.json"
    parser.add_argument("--manifest", default=str(default_manifest))
    parser.add_argument("--acquisition-dir", required=True)
    parser.add_argument("--job-dir", required=True)
    parser.add_argument("--engine-executable", required=True)
    parser.add_argument("--engine-sha256", required=True)
    parser.add_argument("--interchange-tree-sha256")
    parser.add_argument("--project", required=True)
    parser.add_argument("--project-sha256", required=True)
    parser.add_argument("--project-tree-sha256")
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approval-ref")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    os.umask(0o077)
    try:
        args = build_parser().parse_args(argv)
        if args.apply and LIVE_APPLY_QUARANTINED:
            fail(
                LIVE_APPLY_QUARANTINE_CODE,
                "Live commandlet execution is quarantined pending independent P0 closure",
            )
        plan = build_execution_plan(
            manifest_path=Path(args.manifest),
            acquisition_dir=Path(args.acquisition_dir),
            job_dir=Path(args.job_dir),
            engine_executable=Path(args.engine_executable),
            engine_sha256=args.engine_sha256,
            interchange_tree_sha256=args.interchange_tree_sha256,
            project_file=Path(args.project),
            project_sha256=args.project_sha256,
            project_control_sha256=args.project_tree_sha256,
            evidence_dir=Path(args.evidence_dir),
            timeout_seconds=args.timeout_seconds,
            approval_ref=args.approval_ref,
            apply=args.apply,
        )
        if args.apply:
            result, code = execute_apply(plan)
        else:
            result, code = dry_run_result(plan), 0
        print(compact_json(result))
        return code
    except (CommandletExecutionError, preparation.ImportJobError) as error:
        public = error.public_dict()
        print(
            compact_json(
                {
                    "schema": RESULT_SCHEMA,
                    "valid": False,
                    "status": "failed",
                    "error": public,
                    "production_ready": False,
                    "semantic_index_eligible": False,
                }
            ),
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
                        "code": "UE_COMMANDLET_INTERNAL_ERROR",
                        "message": "Execution planning failed before a safe result was produced",
                    },
                    "production_ready": False,
                    "semantic_index_eligible": False,
                }
            ),
            file=sys.stderr,
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
