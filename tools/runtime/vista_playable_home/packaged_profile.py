#!/usr/bin/env python3
"""Seal and load one Sunshine profile for the accepted Linux package.

The profile is intentionally a different closed contract from the preview
profile.  It can name only the accepted Playable Home package, fixed display,
GPU, loopback port, map, and render dimensions.  Both profile creation and
loading re-hash the complete packaged archive against the pinned package
receipt before returning.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from tools.runtime.vista_playable_home.runtime import (  # type: ignore
        R2_CAMERA_PROFILE,
        R2_RUNTIME_PROFILE,
        RuntimeSafetyError,
        resolve_runtime_profile,
    )
    from tools.ue.vista_playable_home import package_receipt as package_verifier  # type: ignore
else:
    from .runtime import (
        R2_CAMERA_PROFILE,
        R2_RUNTIME_PROFILE,
        RuntimeSafetyError,
        resolve_runtime_profile,
    )
    from tools.ue.vista_playable_home import package_receipt as package_verifier


PROFILE_SCHEMA = "simworld.vista.playable-home-sunshine-packaged-profile/v1"
PROFILE_MODE = "linux-development-package"
R2_PROFILE_SCHEMA = "simworld.vista.playable-home-sunshine-packaged-profile/v2"
R2_EXACT_MODE_PROFILE_SCHEMA = (
    "simworld.vista.playable-home-sunshine-packaged-profile/v3"
)
R2_PROFILE_MODE = "linux-development-package-realistic"
EXACT_MODE_POLICY = "sealed-exact-stat-imode/v1"
PROFILE_FILE_MODE = 0o600
EXPECTED_MAP_PATH = package_verifier.EXPECTED_MAP_PATH
EXPECTED_WORLD_REVISION = package_verifier.EXPECTED_REVISION
EXPECTED_DISPLAY = ":117"
EXPECTED_GPU = 0
EXPECTED_PORT = 55620
EXPECTED_WIDTH = 1280
EXPECTED_HEIGHT = 720
EXPECTED_FPS = 60
EXPECTED_TITLE = "VISTA World"
MAX_PROFILE_BYTES = 64 * 1024
MAX_RECEIPT_BYTES = 4 * 1024 * 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
OUTPUT_RE = re.compile(
    r"^sunshine-profile-packaged(?:-[A-Za-z0-9][A-Za-z0-9_-]{0,63})?\.json$"
)
PROFILE_KEYS = frozenset(
    {
        "schema",
        "mode",
        "package_attempt",
        "package_receipt",
        "package_receipt_sha256",
        "archive_tree_sha256",
        "executable",
        "executable_sha256",
        "pak",
        "pak_sha256",
        "map",
        "world_revision",
        "display",
        "gpu",
        "vista_world_port",
        "width",
        "height",
        "fps",
        "title",
        "nvidia_icd",
        "nvidia_icd_sha256",
        "trusted_engine_root",
        "unreal_pak",
        "unreal_pak_sha256",
    }
)
R2_PROFILE_KEYS = PROFILE_KEYS | frozenset({"runtime_profile", "camera_profile"})
R2_EXACT_MODE_PROFILE_KEYS = R2_PROFILE_KEYS | frozenset(
    {
        "archive_schema",
        "archive_algorithm",
        "mode_policy",
        "package_receipt_mode",
        "profile_file_mode",
        "nvidia_icd_mode",
    }
)
RECEIPT_KEYS = frozenset(
    {
        "schema",
        "status",
        "created_at",
        "attempt_root",
        "bindings",
        "artifacts",
        "uat",
        "project_policy",
        "tools",
        "trusted_upstream",
        "archive",
        "output",
    }
)
BINDING_KEYS = frozenset(
    {
        "source_build_result",
        "source_build_result_sha256",
        "source_commit",
        "source_runtime_acceptance",
        "source_runtime_acceptance_sha256",
        "map_path",
        "world_revision",
    }
)
R2_BINDING_KEYS = BINDING_KEYS | frozenset(package_verifier.R2_PACKAGE_BINDING_FIELDS)
ARTIFACT_KEYS = frozenset({"archive_root", "launcher", "executable", "pak"})
ARTIFACT_RECORD_KEYS = frozenset({"relative_path", "sha256", "bytes", "executable"})
EXACT_MODE_ARTIFACT_RECORD_KEYS = ARTIFACT_RECORD_KEYS | frozenset({"mode"})
ARCHIVE_KEYS = frozenset(
    {"algorithm", "file_count", "total_bytes", "tree_sha256", "secret_scan"}
)
EXACT_MODE_ARCHIVE_KEYS = ARCHIVE_KEYS | frozenset({"schema"})
TRUSTED_UPSTREAM_KEYS = frozenset(
    {"policy", "engine_root", "unreal_pak", "unreal_pak_sha256"}
)
EXACT_MODE_TRUSTED_UPSTREAM_KEYS = TRUSTED_UPSTREAM_KEYS | frozenset(
    {"mode_policy", "unreal_pak_mode"}
)


class PackagedProfileError(RuntimeError):
    """Raised before a package or profile can affect a runtime."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class PackageBinding:
    attempt_root: Path
    receipt: Path
    receipt_sha256: str
    archive_root: Path
    archive_tree_sha256: str
    archive_file_count: int
    archive_total_bytes: int
    trusted_engine_root: Path
    unreal_pak: Path
    unreal_pak_sha256: str
    launcher: Path
    launcher_sha256: str
    executable: Path
    executable_sha256: str
    pak: Path
    pak_sha256: str
    receipt_schema: str
    receipt_mode: int
    archive_schema: str | None
    archive_algorithm: str
    launcher_mode: int | None
    executable_mode: int | None
    pak_mode: int | None
    unreal_pak_mode: int | None
    project_descriptor: Path | None
    project_descriptor_sha256: str | None
    project_descriptor_mode: int | None
    project_config: Path | None
    project_config_sha256: str | None
    project_config_mode: int | None
    exact_mode_attestation: bool
    map_path: str
    world_revision: str
    runtime_profile: str | None = None
    camera_profile: str | None = None
    visual_profile_sha256: str | None = None
    visual_profile_content_digest: str | None = None
    renderer_profile_request_sha256: str | None = None
    renderer_profile_request_content_digest: str | None = None
    presentation_import_receipt_sha256: str | None = None
    presentation_scene_receipt_sha256: str | None = None
    presentation_manifest_sha256: str | None = None
    presentation_artifact_receipt_sha256: str | None = None


@dataclass(frozen=True)
class PackagedProfileInputs:
    profile: Path
    profile_sha256: str
    package: PackageBinding
    nvidia_icd: Path
    nvidia_icd_sha256: str
    profile_mode: int
    nvidia_icd_mode: int | None
    exact_mode_attestation: bool
    runtime_profile: str | None = None
    camera_profile: str | None = None
    display: str = EXPECTED_DISPLAY
    gpu: int = EXPECTED_GPU
    vista_world_port: int = EXPECTED_PORT
    width: int = EXPECTED_WIDTH
    height: int = EXPECTED_HEIGHT
    fps: int = EXPECTED_FPS


@dataclass(frozen=True)
class ProfileWriteResult:
    output: Path
    profile_sha256: str
    package_receipt: Path
    package_receipt_sha256: str
    archive_tree_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "written",
            "output": str(self.output),
            "profile_sha256": self.profile_sha256,
            "package_receipt": str(self.package_receipt),
            "package_receipt_sha256": self.package_receipt_sha256,
            "archive_tree_sha256": self.archive_tree_sha256,
        }


@dataclass(frozen=True)
class FileSeal:
    sha256: str | None
    size: int
    mode: int
    identity: tuple[int, int, int, int, int, int]
    raw: bytes | None = None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


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
    except (TypeError, ValueError) as exc:
        raise PackagedProfileError("JSON_INVALID", "value is not finite JSON") from exc


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        stat.S_IMODE(metadata.st_mode),
    )


def _sealed_file(
    path: Path,
    *,
    hash_bytes: bool = True,
    capture_bytes: bool = False,
    maximum_bytes: int | None = None,
) -> FileSeal:
    digest = hashlib.sha256()
    captured = bytearray() if capture_bytes else None
    try:
        before = os.lstat(path)
        if maximum_bytes is not None and not 0 < before.st_size <= maximum_bytes:
            raise PackagedProfileError(
                "JSON_SIZE_INVALID", f"{path.name} size is invalid"
            )
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if _file_identity(opened) != _file_identity(before):
                raise PackagedProfileError(
                    "FILE_CHANGED", f"{path.name} changed while opening"
                )
            if hash_bytes or capture_bytes:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    if hash_bytes:
                        digest.update(block)
                    if captured is not None:
                        captured.extend(block)
        after = os.lstat(path)
        if _file_identity(after) != _file_identity(before):
            raise PackagedProfileError(
                "FILE_CHANGED", f"{path.name} changed while hashing"
            )
    except PackagedProfileError:
        raise
    except OSError as exc:
        raise PackagedProfileError(
            "READ_FAILED", f"could not hash {path.name}"
        ) from exc
    return FileSeal(
        sha256=digest.hexdigest() if hash_bytes else None,
        size=after.st_size,
        mode=stat.S_IMODE(after.st_mode),
        identity=_file_identity(after),
        raw=bytes(captured) if captured is not None else None,
    )


def sha256_file(path: Path) -> str:
    digest = _sealed_file(path).sha256
    if digest is None:  # pragma: no cover - hash_bytes defaults true.
        raise PackagedProfileError("READ_FAILED", f"could not hash {path.name}")
    return digest


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _reject_nonfinite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite JSON number")
    if isinstance(value, list):
        for item in value:
            _reject_nonfinite(item)
    elif isinstance(value, dict):
        for item in value.values():
            _reject_nonfinite(item)


def _strict_object(raw: bytes, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        _reject_nonfinite(value)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise PackagedProfileError(
            "JSON_INVALID", f"{label} is not strict JSON"
        ) from exc
    if not isinstance(value, dict):
        raise PackagedProfileError("JSON_SHAPE_INVALID", f"{label} must be an object")
    if canonical_json(value) != raw:
        raise PackagedProfileError(
            "JSON_CANONICAL_INVALID", f"{label} is not canonical JSON"
        )
    return value


def _canonical_existing(path: Path, label: str, *, directory: bool = False) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise PackagedProfileError(
            "PATH_IDENTITY_INVALID", f"{label} must be an absolute canonical path"
        )
    try:
        metadata = candidate.lstat()
    except (FileNotFoundError, OSError) as exc:
        raise PackagedProfileError("PATH_MISSING", f"{label} does not exist") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise PackagedProfileError(
            "PATH_IDENTITY_INVALID", f"{label} must not be a symlink"
        )
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise PackagedProfileError(
            "PATH_IDENTITY_INVALID", f"{label} identity could not be resolved"
        ) from exc
    expected_kind = resolved.is_dir() if directory else resolved.is_file()
    if resolved != candidate or not expected_kind:
        raise PackagedProfileError(
            "PATH_IDENTITY_INVALID", f"{label} must name its real path identity"
        )
    return resolved


def _read_pinned_json(
    path: Path,
    expected_sha256: str,
    label: str,
    *,
    maximum_bytes: int,
) -> tuple[Mapping[str, Any], bytes, FileSeal]:
    candidate = _canonical_existing(path, label)
    if (
        not isinstance(expected_sha256, str)
        or SHA256_RE.fullmatch(expected_sha256) is None
    ):
        raise PackagedProfileError("PIN_INVALID", f"{label} SHA-256 is invalid")
    seal = _sealed_file(
        candidate,
        capture_bytes=True,
        maximum_bytes=maximum_bytes,
    )
    if seal.raw is None or seal.sha256 is None:  # pragma: no cover - fixed options.
        raise PackagedProfileError("READ_FAILED", f"could not read {label}")
    raw = seal.raw
    if len(raw) != seal.size or not hmac.compare_digest(seal.sha256, expected_sha256):
        raise PackagedProfileError("PIN_MISMATCH", f"{label} SHA-256 differs")
    return _strict_object(raw, label), raw, seal


def _mapping(
    value: Any, label: str, keys: frozenset[str] | None = None
) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise PackagedProfileError(
            "RECEIPT_SHAPE_INVALID", f"{label} must be an object"
        )
    if keys is not None and set(value) != keys:
        raise PackagedProfileError("RECEIPT_SHAPE_INVALID", f"{label} fields differ")
    return value


def _validate_attempt(path: Path) -> Path:
    root = _canonical_existing(path, "package attempt", directory=True)
    if (
        root.parent.name != package_verifier.EXPECTED_ATTEMPT_PARENT
        or package_verifier.PACKAGE_ATTEMPT_RE.fullmatch(root.name) is None
    ):
        raise PackagedProfileError(
            "ATTEMPT_IDENTITY_INVALID",
            "package attempt must be package-linux-development/attempt-<id>",
        )
    return root


def _artifact(
    root: Path,
    record_value: Any,
    label: str,
    *,
    expected_relative: str | None,
    expected_executable: bool,
    verify_bytes: bool,
    exact_modes: bool,
) -> tuple[Path, str, int | None, FileSeal]:
    record = _mapping(
        record_value,
        label,
        EXACT_MODE_ARTIFACT_RECORD_KEYS if exact_modes else ARTIFACT_RECORD_KEYS,
    )
    relative_value = record.get("relative_path")
    if not isinstance(relative_value, str) or not relative_value:
        raise PackagedProfileError(
            "ARTIFACT_INVALID", f"{label} relative path is invalid"
        )
    relative = Path(relative_value)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or relative.as_posix() != relative_value
    ):
        raise PackagedProfileError(
            "ARTIFACT_INVALID", f"{label} relative path is unsafe"
        )
    if expected_relative is not None and relative_value != expected_relative:
        raise PackagedProfileError("ARTIFACT_INVALID", f"{label} path differs")
    candidate = _canonical_existing(root / relative, label)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise PackagedProfileError(
            "PATH_ESCAPE_REFUSED", f"{label} escaped the package"
        ) from exc
    digest = record.get("sha256")
    size = record.get("bytes")
    executable = record.get("executable")
    observed = _sealed_file(candidate, hash_bytes=verify_bytes)
    observed_mode = observed.mode
    observed_executable = bool(observed_mode & 0o111)
    expected_mode = record.get("mode") if exact_modes else None
    if (
        not isinstance(digest, str)
        or SHA256_RE.fullmatch(digest) is None
        or not _is_int(size)
        or size <= 0
        or observed.size != size
        or executable is not expected_executable
        or observed_executable is not expected_executable
        or (
            exact_modes
            and (
                isinstance(expected_mode, bool)
                or not isinstance(expected_mode, int)
                or not 0 <= expected_mode <= 0o7777
                or observed_mode != expected_mode
            )
        )
        or (
            verify_bytes
            and (
                observed.sha256 is None
                or not hmac.compare_digest(observed.sha256, digest)
            )
        )
    ):
        raise PackagedProfileError(
            "ARTIFACT_PIN_MISMATCH", f"{label} bytes or mode differ"
        )
    return candidate, digest, expected_mode, observed


def validate_package_attempt(
    package_attempt: Path,
    package_receipt_sha256: str,
    *,
    verify_archive: bool = True,
) -> PackageBinding:
    root = _validate_attempt(package_attempt)
    receipt_path = _canonical_existing(
        root / package_verifier.OUTPUT_RELATIVE, "package receipt"
    )
    receipt, _raw, receipt_seal = _read_pinned_json(
        receipt_path,
        package_receipt_sha256,
        "package receipt",
        maximum_bytes=MAX_RECEIPT_BYTES,
    )
    if receipt_seal.mode != 0o600:
        raise PackagedProfileError(
            "RECEIPT_MODE_INVALID", "package receipt mode must be 0600"
        )
    if set(receipt) != RECEIPT_KEYS:
        raise PackagedProfileError(
            "RECEIPT_SHAPE_INVALID", "package receipt fields differ"
        )
    receipt_schema = receipt.get("schema")
    if receipt_schema == package_verifier.RECEIPT_SCHEMA:
        package_runtime_profile = None
        binding_keys = BINDING_KEYS
    elif receipt_schema == package_verifier.R2_RECEIPT_SCHEMA:
        package_runtime_profile = R2_RUNTIME_PROFILE
        binding_keys = R2_BINDING_KEYS
        exact_modes = False
    elif receipt_schema == package_verifier.R2_EXACT_MODE_RECEIPT_SCHEMA:
        package_runtime_profile = R2_RUNTIME_PROFILE
        binding_keys = R2_BINDING_KEYS
        exact_modes = True
    else:
        raise PackagedProfileError(
            "RECEIPT_IDENTITY_INVALID", "package receipt schema differs"
        )
    if receipt_schema == package_verifier.RECEIPT_SCHEMA:
        exact_modes = False
    if (
        receipt.get("status") != "accepted"
        or receipt.get("attempt_root") != str(root)
        or receipt.get("output") != str(receipt_path)
    ):
        raise PackagedProfileError(
            "RECEIPT_IDENTITY_INVALID", "package receipt identity differs"
        )

    bindings = _mapping(receipt.get("bindings"), "package bindings", binding_keys)
    if (
        bindings.get("map_path") != EXPECTED_MAP_PATH
        or bindings.get("world_revision") != EXPECTED_WORLD_REVISION
        or not isinstance(bindings.get("source_commit"), str)
        or package_verifier.COMMIT_RE.fullmatch(bindings["source_commit"]) is None
        or any(
            not isinstance(bindings.get(field), str)
            or SHA256_RE.fullmatch(bindings[field]) is None
            for field in (
                "source_build_result_sha256",
                "source_runtime_acceptance_sha256",
            )
        )
    ):
        raise PackagedProfileError(
            "PACKAGE_BINDING_INVALID", "package source/map bindings differ"
        )
    if package_runtime_profile == R2_RUNTIME_PROFILE:
        r2_fixed = {
            "runtime_profile": R2_RUNTIME_PROFILE,
            "camera_profile": R2_CAMERA_PROFILE,
            "visual_profile_id": R2_RUNTIME_PROFILE,
            "accepted_display": package_verifier.R2_DISPLAY,
            "accepted_gpu": package_verifier.R2_GPU,
            "accepted_vista_world_port": package_verifier.R2_VISTA_WORLD_PORT,
            "accepted_width": package_verifier.R2_WIDTH,
            "accepted_height": package_verifier.R2_HEIGHT,
            "accepted_fps": package_verifier.R2_FPS,
            "presentation_bundle_count": (
                package_verifier.R2_PRESENTATION_BUNDLE_COUNT
            ),
            "presentation_collision_policy": (
                package_verifier.R2_PRESENTATION_COLLISION_POLICY
            ),
        }
        if any(bindings.get(key) != value for key, value in r2_fixed.items()):
            raise PackagedProfileError(
                "PACKAGE_BINDING_INVALID", "r2 package source/profile bindings differ"
            )
        for field in package_verifier.R2_BUILD_DIGEST_FIELDS:
            value = bindings.get(field)
            if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
                raise PackagedProfileError(
                    "PACKAGE_BINDING_INVALID", f"r2 package {field} is invalid"
                )

    artifacts = _mapping(receipt.get("artifacts"), "package artifacts", ARTIFACT_KEYS)
    archive_root_value = artifacts.get("archive_root")
    archive_root = _canonical_existing(
        root / "archive" / "Linux", "package archive", directory=True
    )
    if archive_root_value != str(archive_root):
        raise PackagedProfileError("ARCHIVE_IDENTITY_INVALID", "archive root differs")
    launcher, launcher_sha, launcher_mode, launcher_seal = _artifact(
        root,
        artifacts.get("launcher"),
        "package launcher",
        expected_relative=package_verifier.LAUNCHER_RELATIVE.as_posix(),
        expected_executable=True,
        verify_bytes=verify_archive,
        exact_modes=exact_modes,
    )
    executable, executable_sha, executable_mode, executable_seal = _artifact(
        root,
        artifacts.get("executable"),
        "package executable",
        expected_relative=package_verifier.EXECUTABLE_RELATIVE.as_posix(),
        expected_executable=True,
        verify_bytes=verify_archive,
        exact_modes=exact_modes,
    )
    pak_record = _mapping(
        artifacts.get("pak"),
        "package pak",
        EXACT_MODE_ARTIFACT_RECORD_KEYS if exact_modes else ARTIFACT_RECORD_KEYS,
    )
    pak_relative = pak_record.get("relative_path")
    pak_parent = package_verifier.PAK_DIRECTORY_RELATIVE.as_posix() + "/"
    if (
        not isinstance(pak_relative, str)
        or not pak_relative.startswith(pak_parent)
        or "/" in pak_relative.removeprefix(pak_parent)
        or not pak_relative.endswith(".pak")
    ):
        raise PackagedProfileError("ARTIFACT_INVALID", "package pak path differs")
    pak, pak_sha, pak_mode, pak_seal = _artifact(
        root,
        pak_record,
        "package pak",
        expected_relative=pak_relative,
        expected_executable=False,
        verify_bytes=verify_archive,
        exact_modes=exact_modes,
    )

    trusted = _mapping(
        receipt.get("trusted_upstream"),
        "trusted upstream",
        (EXACT_MODE_TRUSTED_UPSTREAM_KEYS if exact_modes else TRUSTED_UPSTREAM_KEYS),
    )
    trusted_engine_root_value = trusted.get("engine_root")
    unreal_pak_value = trusted.get("unreal_pak")
    unreal_pak_sha = trusted.get("unreal_pak_sha256")
    unreal_pak_mode = trusted.get("unreal_pak_mode") if exact_modes else None
    if (
        trusted.get("policy") != "engine-root-derived-from-pinned-unrealpak/v1"
        or not isinstance(trusted_engine_root_value, str)
        or not isinstance(unreal_pak_value, str)
        or not isinstance(unreal_pak_sha, str)
        or SHA256_RE.fullmatch(unreal_pak_sha) is None
        or (
            exact_modes
            and (
                trusted.get("mode_policy") != EXACT_MODE_POLICY
                or isinstance(unreal_pak_mode, bool)
                or not isinstance(unreal_pak_mode, int)
                or not 0 <= unreal_pak_mode <= 0o7777
            )
        )
    ):
        raise PackagedProfileError(
            "TRUSTED_UPSTREAM_INVALID", "trusted engine binding differs"
        )
    trusted_engine_root = _canonical_existing(
        Path(trusted_engine_root_value), "trusted engine root", directory=True
    )
    unreal_pak = _canonical_existing(Path(unreal_pak_value), "trusted UnrealPak")
    unreal_pak_seal = _sealed_file(unreal_pak, hash_bytes=verify_archive)
    if (
        unreal_pak != trusted_engine_root / "Engine/Binaries/Linux/UnrealPak"
        or not os.access(unreal_pak, os.X_OK)
        or (exact_modes and unreal_pak_seal.mode != unreal_pak_mode)
        or (
            verify_archive
            and (
                unreal_pak_seal.sha256 is None
                or not hmac.compare_digest(unreal_pak_seal.sha256, unreal_pak_sha)
            )
        )
    ):
        raise PackagedProfileError(
            "TRUSTED_UPSTREAM_PIN_MISMATCH", "trusted UnrealPak bytes or path differ"
        )

    project_policy = _mapping(receipt.get("project_policy"), "project policy")
    project_descriptor: Path | None = None
    project_descriptor_sha: str | None = None
    project_descriptor_mode: int | None = None
    project_config: Path | None = None
    project_config_sha: str | None = None
    project_config_mode: int | None = None
    if exact_modes:
        expected_policy_keys = {
            "project_descriptor",
            "project_descriptor_sha256",
            "project_config",
            "project_config_sha256",
            "enabled_plugins",
            "disabled_plugins",
            "host_module",
            "android_file_server_enabled",
            "mode_policy",
            "project_descriptor_mode",
            "project_config_mode",
        }
        if set(project_policy) != expected_policy_keys:
            raise PackagedProfileError(
                "PROJECT_POLICY_INVALID", "exact-mode project policy fields differ"
            )
        project_descriptor = _canonical_existing(
            root / package_verifier.PROJECT_RELATIVE,
            "package project descriptor",
        )
        project_config = _canonical_existing(
            root / package_verifier.PROJECT_CONFIG_RELATIVE,
            "package project config",
        )
        project_descriptor_seal = _sealed_file(
            project_descriptor,
            hash_bytes=verify_archive,
        )
        project_config_seal = _sealed_file(
            project_config,
            hash_bytes=verify_archive,
        )
        project_descriptor_sha = project_policy.get("project_descriptor_sha256")
        project_config_sha = project_policy.get("project_config_sha256")
        project_descriptor_mode = project_policy.get("project_descriptor_mode")
        project_config_mode = project_policy.get("project_config_mode")
        if (
            project_policy.get("project_descriptor") != str(project_descriptor)
            or project_policy.get("project_config") != str(project_config)
            or project_policy.get("mode_policy") != EXACT_MODE_POLICY
            or any(
                not isinstance(value, str) or SHA256_RE.fullmatch(value) is None
                for value in (project_descriptor_sha, project_config_sha)
            )
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= 0o7777
                for value in (project_descriptor_mode, project_config_mode)
            )
            or project_descriptor_seal.mode != project_descriptor_mode
            or project_config_seal.mode != project_config_mode
            or (
                verify_archive
                and (
                    project_descriptor_seal.sha256 is None
                    or not hmac.compare_digest(
                        project_descriptor_seal.sha256, project_descriptor_sha
                    )
                    or project_config_seal.sha256 is None
                    or not hmac.compare_digest(
                        project_config_seal.sha256, project_config_sha
                    )
                )
            )
        ):
            raise PackagedProfileError(
                "PROJECT_POLICY_PIN_MISMATCH",
                "package project/config bytes or exact modes differ",
            )

    archive = _mapping(
        receipt.get("archive"),
        "archive observation",
        EXACT_MODE_ARCHIVE_KEYS if exact_modes else ARCHIVE_KEYS,
    )
    tree_sha = archive.get("tree_sha256")
    file_count = archive.get("file_count")
    total_bytes = archive.get("total_bytes")
    secret_scan = _mapping(archive.get("secret_scan"), "archive secret scan")
    if (
        archive.get("algorithm")
        != (
            package_verifier.ARCHIVE_ALGORITHM_EXACT_MODE_V2
            if exact_modes
            else package_verifier.ARCHIVE_ALGORITHM_V1
        )
        or (
            exact_modes
            and archive.get("schema") != package_verifier.ARCHIVE_SCHEMA_EXACT_MODE_V2
        )
        or not isinstance(tree_sha, str)
        or SHA256_RE.fullmatch(tree_sha) is None
        or not _is_int(file_count)
        or file_count <= 0
        or not _is_int(total_bytes)
        or total_bytes <= 0
        or secret_scan.get("matches") != 0
    ):
        raise PackagedProfileError("ARCHIVE_RECEIPT_INVALID", "archive receipt differs")
    if verify_archive:
        try:
            live_archive = package_verifier.inspect_archive(
                archive_root,
                trusted_engine_root=trusted_engine_root,
                exact_modes=exact_modes,
            )
        except package_verifier.PackageReceiptError as exc:
            raise PackagedProfileError(
                "ARCHIVE_REHASH_FAILED", "package archive could not be re-hashed"
            ) from exc
        if live_archive != archive:
            raise PackagedProfileError(
                "ARCHIVE_PIN_MISMATCH", "package archive differs from its receipt"
            )

    binding = PackageBinding(
        attempt_root=root,
        receipt=receipt_path,
        receipt_sha256=package_receipt_sha256,
        archive_root=archive_root,
        archive_tree_sha256=tree_sha,
        archive_file_count=file_count,
        archive_total_bytes=total_bytes,
        trusted_engine_root=trusted_engine_root,
        unreal_pak=unreal_pak,
        unreal_pak_sha256=unreal_pak_sha,
        launcher=launcher,
        launcher_sha256=launcher_sha,
        executable=executable,
        executable_sha256=executable_sha,
        pak=pak,
        pak_sha256=pak_sha,
        receipt_schema=receipt_schema,
        receipt_mode=0o600,
        archive_schema=(archive.get("schema") if exact_modes else None),
        archive_algorithm=archive["algorithm"],
        launcher_mode=launcher_mode,
        executable_mode=executable_mode,
        pak_mode=pak_mode,
        unreal_pak_mode=unreal_pak_mode,
        project_descriptor=project_descriptor,
        project_descriptor_sha256=project_descriptor_sha,
        project_descriptor_mode=project_descriptor_mode,
        project_config=project_config,
        project_config_sha256=project_config_sha,
        project_config_mode=project_config_mode,
        exact_mode_attestation=exact_modes,
        map_path=EXPECTED_MAP_PATH,
        world_revision=EXPECTED_WORLD_REVISION,
        runtime_profile=package_runtime_profile,
        camera_profile=(
            R2_CAMERA_PROFILE if package_runtime_profile == R2_RUNTIME_PROFILE else None
        ),
        visual_profile_sha256=bindings.get("visual_profile_sha256"),
        visual_profile_content_digest=bindings.get("visual_profile_content_digest"),
        renderer_profile_request_sha256=bindings.get("renderer_profile_request_sha256"),
        renderer_profile_request_content_digest=bindings.get(
            "renderer_profile_request_content_digest"
        ),
        presentation_import_receipt_sha256=bindings.get(
            "presentation_import_receipt_sha256"
        ),
        presentation_scene_receipt_sha256=bindings.get(
            "presentation_scene_receipt_sha256"
        ),
        presentation_manifest_sha256=bindings.get("presentation_manifest_sha256"),
        presentation_artifact_receipt_sha256=bindings.get(
            "presentation_artifact_receipt_sha256"
        ),
    )
    final_receipt_seal = _sealed_file(
        receipt_path,
        capture_bytes=True,
        maximum_bytes=MAX_RECEIPT_BYTES,
    )
    final_launcher_seal = _sealed_file(launcher, hash_bytes=verify_archive)
    final_executable_seal = _sealed_file(executable, hash_bytes=verify_archive)
    final_pak_seal = _sealed_file(pak, hash_bytes=verify_archive)
    final_unreal_pak_seal = _sealed_file(unreal_pak, hash_bytes=verify_archive)
    exact_project_changed = False
    if exact_modes:
        final_project_descriptor_seal = _sealed_file(
            project_descriptor,
            hash_bytes=verify_archive,
        )
        final_project_config_seal = _sealed_file(
            project_config,
            hash_bytes=verify_archive,
        )
        exact_project_changed = (
            final_project_descriptor_seal != project_descriptor_seal
            or final_project_config_seal != project_config_seal
        )
    archive_changed = False
    if verify_archive:
        try:
            final_archive = package_verifier.inspect_archive(
                archive_root,
                trusted_engine_root=trusted_engine_root,
                exact_modes=exact_modes,
            )
        except package_verifier.PackageReceiptError as exc:
            raise PackagedProfileError(
                "ARCHIVE_REHASH_FAILED", "package archive could not be re-hashed"
            ) from exc
        archive_changed = final_archive != archive
    if (
        final_receipt_seal != receipt_seal
        or final_launcher_seal != launcher_seal
        or final_executable_seal != executable_seal
        or final_pak_seal != pak_seal
        or final_unreal_pak_seal != unreal_pak_seal
        or exact_project_changed
        or archive_changed
    ):
        raise PackagedProfileError(
            "PACKAGE_IDENTITY_CHANGED",
            "package identity changed during profile validation",
        )
    return binding


def revalidate_package(binding: PackageBinding) -> PackageBinding:
    observed = validate_package_attempt(binding.attempt_root, binding.receipt_sha256)
    if observed != binding:
        raise PackagedProfileError(
            "PACKAGE_IDENTITY_CHANGED",
            "package identity changed after profile validation",
        )
    return observed


def _validate_nvidia_icd(path: Path) -> Path:
    icd = _canonical_existing(path, "NVIDIA ICD")
    if icd.suffix != ".json":
        raise PackagedProfileError(
            "NVIDIA_ICD_INVALID", "NVIDIA ICD must be a JSON file"
        )
    return icd


def profile_from_binding(
    binding: PackageBinding,
    nvidia_icd: Path,
    *,
    runtime_profile: str | None = None,
    _nvidia_icd_seal: FileSeal | None = None,
) -> dict[str, Any]:
    validated_icd = _validate_nvidia_icd(nvidia_icd)
    nvidia_icd_seal = (
        _nvidia_icd_seal
        if _nvidia_icd_seal is not None
        else _sealed_file(validated_icd)
    )
    if nvidia_icd_seal.sha256 is None:  # pragma: no cover - fixed sealed read.
        raise PackagedProfileError("READ_FAILED", "could not hash NVIDIA ICD")
    try:
        spec = resolve_runtime_profile(runtime_profile)
    except RuntimeSafetyError as exc:
        raise PackagedProfileError(
            "PROFILE_FIXED_VALUE_INVALID",
            "runtime profile is not one of the closed profiles",
        ) from exc
    if (
        binding.runtime_profile != spec.runtime_profile
        or binding.camera_profile != spec.camera_profile
    ):
        raise PackagedProfileError(
            "PACKAGE_PROFILE_MISMATCH",
            "package receipt and requested runtime profile differ",
        )
    profile = {
        "schema": (
            R2_EXACT_MODE_PROFILE_SCHEMA
            if runtime_profile is not None and binding.exact_mode_attestation
            else R2_PROFILE_SCHEMA
            if runtime_profile is not None
            else PROFILE_SCHEMA
        ),
        "mode": R2_PROFILE_MODE if runtime_profile is not None else PROFILE_MODE,
        "package_attempt": str(binding.attempt_root),
        "package_receipt": str(binding.receipt),
        "package_receipt_sha256": binding.receipt_sha256,
        "archive_tree_sha256": binding.archive_tree_sha256,
        "executable": str(binding.executable),
        "executable_sha256": binding.executable_sha256,
        "pak": str(binding.pak),
        "pak_sha256": binding.pak_sha256,
        "trusted_engine_root": str(binding.trusted_engine_root),
        "unreal_pak": str(binding.unreal_pak),
        "unreal_pak_sha256": binding.unreal_pak_sha256,
        "map": binding.map_path,
        "world_revision": binding.world_revision,
        "display": spec.display,
        "gpu": spec.gpu,
        "vista_world_port": spec.vista_world_port,
        "width": spec.width,
        "height": spec.height,
        "fps": spec.fps,
        "title": EXPECTED_TITLE,
        "nvidia_icd": str(validated_icd),
        "nvidia_icd_sha256": nvidia_icd_seal.sha256,
    }
    if runtime_profile is not None:
        profile.update(
            {
                "runtime_profile": spec.runtime_profile,
                "camera_profile": spec.camera_profile,
            }
        )
    if runtime_profile is not None and binding.exact_mode_attestation:
        profile.update(
            {
                "archive_schema": binding.archive_schema,
                "archive_algorithm": binding.archive_algorithm,
                "mode_policy": EXACT_MODE_POLICY,
                "package_receipt_mode": binding.receipt_mode,
                "profile_file_mode": PROFILE_FILE_MODE,
                "nvidia_icd_mode": nvidia_icd_seal.mode,
            }
        )
    return profile


def _output_path(path: Path, root: Path) -> Path:
    output = Path(path)
    if (
        not output.is_absolute()
        or ".." in output.parts
        or output.parent != root
        or OUTPUT_RE.fullmatch(output.name) is None
    ):
        raise PackagedProfileError(
            "OUTPUT_IDENTITY_INVALID",
            "profile must be a fresh direct child of the package attempt",
        )
    return output


def _write_private_exclusive(path: Path, raw: bytes) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise PackagedProfileError(
            "OUTPUT_EXISTS", "profile output already exists"
        ) from exc
    except OSError as exc:
        raise PackagedProfileError(
            "OUTPUT_WRITE_FAILED", "could not create profile"
        ) from exc
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        directory = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise PackagedProfileError(
            "OUTPUT_WRITE_FAILED", "could not commit profile"
        ) from exc


def write_profile(
    package_attempt: Path,
    package_receipt_sha256: str,
    nvidia_icd: Path,
    output: Path,
    *,
    runtime_profile: str | None = None,
) -> ProfileWriteResult:
    binding = validate_package_attempt(package_attempt, package_receipt_sha256)
    output_path = _output_path(output, binding.attempt_root)
    raw = canonical_json(
        profile_from_binding(
            binding,
            nvidia_icd,
            runtime_profile=runtime_profile,
        )
    )
    _write_private_exclusive(output_path, raw)
    return ProfileWriteResult(
        output=output_path,
        profile_sha256=sha256_bytes(raw),
        package_receipt=binding.receipt,
        package_receipt_sha256=binding.receipt_sha256,
        archive_tree_sha256=binding.archive_tree_sha256,
    )


def load_profile(
    path: Path,
    expected_sha256: str,
    *,
    verify_archive: bool = True,
) -> PackagedProfileInputs:
    profile_path = _canonical_existing(path, "packaged profile")
    payload, _raw, profile_seal = _read_pinned_json(
        profile_path,
        expected_sha256,
        "packaged profile",
        maximum_bytes=MAX_PROFILE_BYTES,
    )
    if profile_seal.mode != PROFILE_FILE_MODE:
        raise PackagedProfileError(
            "PROFILE_MODE_INVALID", "packaged profile mode must be 0600"
        )
    schema = payload.get("schema")
    if schema == PROFILE_SCHEMA:
        runtime_profile = None
        expected_keys = PROFILE_KEYS
        expected_mode = PROFILE_MODE
    elif schema == R2_PROFILE_SCHEMA:
        runtime_profile = R2_RUNTIME_PROFILE
        expected_keys = R2_PROFILE_KEYS
        expected_mode = R2_PROFILE_MODE
        exact_modes = False
    elif schema == R2_EXACT_MODE_PROFILE_SCHEMA:
        runtime_profile = R2_RUNTIME_PROFILE
        expected_keys = R2_EXACT_MODE_PROFILE_KEYS
        expected_mode = R2_PROFILE_MODE
        exact_modes = True
    else:
        raise PackagedProfileError(
            "PROFILE_FIXED_VALUE_INVALID",
            "packaged profile schema differs",
        )
    if schema == PROFILE_SCHEMA:
        exact_modes = False
    if set(payload) != expected_keys:
        raise PackagedProfileError(
            "PROFILE_SHAPE_INVALID", "packaged profile fields differ"
        )
    try:
        spec = resolve_runtime_profile(runtime_profile)
    except RuntimeSafetyError as exc:
        raise PackagedProfileError(
            "PROFILE_FIXED_VALUE_INVALID",
            "runtime profile is not one of the closed profiles",
        ) from exc
    fixed = {
        "schema": schema,
        "mode": expected_mode,
        "map": EXPECTED_MAP_PATH,
        "world_revision": EXPECTED_WORLD_REVISION,
        "display": spec.display,
        "gpu": spec.gpu,
        "vista_world_port": spec.vista_world_port,
        "width": spec.width,
        "height": spec.height,
        "fps": spec.fps,
        "title": EXPECTED_TITLE,
    }
    if runtime_profile is not None:
        fixed.update(
            {
                "runtime_profile": R2_RUNTIME_PROFILE,
                "camera_profile": R2_CAMERA_PROFILE,
            }
        )
    if exact_modes:
        fixed.update(
            {
                "archive_schema": package_verifier.ARCHIVE_SCHEMA_EXACT_MODE_V2,
                "archive_algorithm": (package_verifier.ARCHIVE_ALGORITHM_EXACT_MODE_V2),
                "mode_policy": EXACT_MODE_POLICY,
                "package_receipt_mode": 0o600,
                "profile_file_mode": PROFILE_FILE_MODE,
            }
        )
    if any(payload.get(key) != value for key, value in fixed.items()):
        raise PackagedProfileError(
            "PROFILE_FIXED_VALUE_INVALID", "fixed profile values differ"
        )
    attempt_value = payload.get("package_attempt")
    receipt_pin = payload.get("package_receipt_sha256")
    icd_value = payload.get("nvidia_icd")
    if not all(
        isinstance(value, str) and value
        for value in (attempt_value, receipt_pin, icd_value)
    ):
        raise PackagedProfileError(
            "PROFILE_SHAPE_INVALID", "profile path fields are invalid"
        )
    binding = validate_package_attempt(
        Path(attempt_value),
        receipt_pin,
        verify_archive=verify_archive,
    )
    if (
        profile_path.parent != binding.attempt_root
        or OUTPUT_RE.fullmatch(profile_path.name) is None
    ):
        raise PackagedProfileError(
            "PROFILE_IDENTITY_INVALID",
            "profile is not a direct package-attempt profile",
        )
    nvidia_icd = _validate_nvidia_icd(Path(icd_value))
    nvidia_icd_seal = _sealed_file(nvidia_icd)
    expected_icd_mode = payload.get("nvidia_icd_mode") if exact_modes else None
    if exact_modes and (
        isinstance(expected_icd_mode, bool)
        or not isinstance(expected_icd_mode, int)
        or not 0 <= expected_icd_mode <= 0o7777
        or nvidia_icd_seal.mode != expected_icd_mode
    ):
        raise PackagedProfileError(
            "NVIDIA_ICD_MODE_MISMATCH", "NVIDIA ICD exact mode differs"
        )
    expected = profile_from_binding(
        binding,
        nvidia_icd,
        runtime_profile=runtime_profile,
        _nvidia_icd_seal=nvidia_icd_seal,
    )
    if dict(payload) != expected:
        raise PackagedProfileError(
            "PROFILE_BINDING_MISMATCH",
            "profile differs from its sealed package receipt",
        )
    final_profile_seal = _sealed_file(
        profile_path,
        capture_bytes=True,
        maximum_bytes=MAX_PROFILE_BYTES,
    )
    final_nvidia_icd_seal = _sealed_file(nvidia_icd)
    if final_profile_seal != profile_seal or final_nvidia_icd_seal != nvidia_icd_seal:
        raise PackagedProfileError(
            "PROFILE_IDENTITY_CHANGED",
            "packaged profile or NVIDIA ICD changed during validation",
        )
    return PackagedProfileInputs(
        profile=profile_path,
        profile_sha256=expected_sha256,
        package=binding,
        nvidia_icd=nvidia_icd,
        nvidia_icd_sha256=expected["nvidia_icd_sha256"],
        profile_mode=PROFILE_FILE_MODE,
        nvidia_icd_mode=expected_icd_mode,
        exact_mode_attestation=exact_modes and binding.exact_mode_attestation,
        runtime_profile=runtime_profile,
        camera_profile=spec.camera_profile,
        display=spec.display,
        gpu=spec.gpu,
        vista_world_port=spec.vista_world_port,
        width=spec.width,
        height=spec.height,
        fps=spec.fps,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-attempt", required=True, type=Path)
    parser.add_argument("--package-receipt-sha256", required=True)
    parser.add_argument("--nvidia-icd", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--runtime-profile",
        choices=[R2_RUNTIME_PROFILE],
        default=None,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = write_profile(
        args.package_attempt,
        args.package_receipt_sha256,
        args.nvidia_icd,
        args.output,
        runtime_profile=args.runtime_profile,
    )
    print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PackagedProfileError as error:
        print(f"packaged profile refused: {error}", file=sys.stderr)
        raise SystemExit(2)
