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
R2_PROFILE_MODE = "linux-development-package-realistic"
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
R2_PROFILE_KEYS = PROFILE_KEYS | frozenset(
    {"runtime_profile", "camera_profile"}
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
ARTIFACT_KEYS = frozenset({"archive_root", "launcher", "executable", "pak"})
ARTIFACT_RECORD_KEYS = frozenset(
    {"relative_path", "sha256", "bytes", "executable"}
)
ARCHIVE_KEYS = frozenset(
    {"algorithm", "file_count", "total_bytes", "tree_sha256", "secret_scan"}
)
TRUSTED_UPSTREAM_KEYS = frozenset(
    {"policy", "engine_root", "unreal_pak", "unreal_pak_sha256"}
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
    map_path: str
    world_revision: str


@dataclass(frozen=True)
class PackagedProfileInputs:
    profile: Path
    profile_sha256: str
    package: PackageBinding
    nvidia_icd: Path
    nvidia_icd_sha256: str
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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise PackagedProfileError("READ_FAILED", f"could not hash {path.name}") from exc
    return digest.hexdigest()


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
        raise PackagedProfileError("JSON_INVALID", f"{label} is not strict JSON") from exc
    if not isinstance(value, dict):
        raise PackagedProfileError("JSON_SHAPE_INVALID", f"{label} must be an object")
    if canonical_json(value) != raw:
        raise PackagedProfileError("JSON_CANONICAL_INVALID", f"{label} is not canonical JSON")
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
        raise PackagedProfileError("PATH_IDENTITY_INVALID", f"{label} must not be a symlink")
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
) -> tuple[Mapping[str, Any], bytes]:
    candidate = _canonical_existing(path, label)
    if not isinstance(expected_sha256, str) or SHA256_RE.fullmatch(expected_sha256) is None:
        raise PackagedProfileError("PIN_INVALID", f"{label} SHA-256 is invalid")
    before = os.lstat(candidate)
    if not 0 < before.st_size <= maximum_bytes:
        raise PackagedProfileError("JSON_SIZE_INVALID", f"{label} size is invalid")
    descriptor = -1
    try:
        descriptor = os.open(
            candidate,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino, opened.st_size) != (
            before.st_dev,
            before.st_ino,
            before.st_size,
        ):
            raise PackagedProfileError("FILE_CHANGED", f"{label} changed while opening")
        chunks = bytearray()
        while len(chunks) <= maximum_bytes:
            block = os.read(descriptor, min(1024 * 1024, maximum_bytes + 1 - len(chunks)))
            if not block:
                break
            chunks.extend(block)
    except PackagedProfileError:
        raise
    except OSError as exc:
        raise PackagedProfileError("READ_FAILED", f"could not read {label}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    after = os.lstat(candidate)
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise PackagedProfileError("FILE_CHANGED", f"{label} changed while reading")
    raw = bytes(chunks)
    if len(raw) != before.st_size or not hmac.compare_digest(sha256_bytes(raw), expected_sha256):
        raise PackagedProfileError("PIN_MISMATCH", f"{label} SHA-256 differs")
    return _strict_object(raw, label), raw


def _mapping(value: Any, label: str, keys: frozenset[str] | None = None) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise PackagedProfileError("RECEIPT_SHAPE_INVALID", f"{label} must be an object")
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
) -> tuple[Path, str]:
    record = _mapping(record_value, label, ARTIFACT_RECORD_KEYS)
    relative_value = record.get("relative_path")
    if not isinstance(relative_value, str) or not relative_value:
        raise PackagedProfileError("ARTIFACT_INVALID", f"{label} relative path is invalid")
    relative = Path(relative_value)
    if relative.is_absolute() or ".." in relative.parts or relative.as_posix() != relative_value:
        raise PackagedProfileError("ARTIFACT_INVALID", f"{label} relative path is unsafe")
    if expected_relative is not None and relative_value != expected_relative:
        raise PackagedProfileError("ARTIFACT_INVALID", f"{label} path differs")
    candidate = _canonical_existing(root / relative, label)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise PackagedProfileError("PATH_ESCAPE_REFUSED", f"{label} escaped the package") from exc
    digest = record.get("sha256")
    size = record.get("bytes")
    executable = record.get("executable")
    observed_executable = bool(candidate.stat().st_mode & 0o111)
    if (
        not isinstance(digest, str)
        or SHA256_RE.fullmatch(digest) is None
        or not _is_int(size)
        or size <= 0
        or candidate.stat().st_size != size
        or executable is not expected_executable
        or observed_executable is not expected_executable
        or (
            verify_bytes
            and not hmac.compare_digest(sha256_file(candidate), digest)
        )
    ):
        raise PackagedProfileError("ARTIFACT_PIN_MISMATCH", f"{label} bytes or mode differ")
    return candidate, digest


def validate_package_attempt(
    package_attempt: Path,
    package_receipt_sha256: str,
    *,
    verify_archive: bool = True,
) -> PackageBinding:
    root = _validate_attempt(package_attempt)
    receipt_path = _canonical_existing(root / package_verifier.OUTPUT_RELATIVE, "package receipt")
    receipt, _raw = _read_pinned_json(
        receipt_path,
        package_receipt_sha256,
        "package receipt",
        maximum_bytes=MAX_RECEIPT_BYTES,
    )
    if stat.S_IMODE(receipt_path.stat().st_mode) != 0o600:
        raise PackagedProfileError(
            "RECEIPT_MODE_INVALID", "package receipt mode must be 0600"
        )
    if set(receipt) != RECEIPT_KEYS:
        raise PackagedProfileError("RECEIPT_SHAPE_INVALID", "package receipt fields differ")
    if (
        receipt.get("schema") != package_verifier.RECEIPT_SCHEMA
        or receipt.get("status") != "accepted"
        or receipt.get("attempt_root") != str(root)
        or receipt.get("output") != str(receipt_path)
    ):
        raise PackagedProfileError("RECEIPT_IDENTITY_INVALID", "package receipt identity differs")

    bindings = _mapping(receipt.get("bindings"), "package bindings", BINDING_KEYS)
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
        raise PackagedProfileError("PACKAGE_BINDING_INVALID", "package source/map bindings differ")

    artifacts = _mapping(receipt.get("artifacts"), "package artifacts", ARTIFACT_KEYS)
    archive_root_value = artifacts.get("archive_root")
    archive_root = _canonical_existing(
        root / "archive" / "Linux", "package archive", directory=True
    )
    if archive_root_value != str(archive_root):
        raise PackagedProfileError("ARCHIVE_IDENTITY_INVALID", "archive root differs")
    launcher, launcher_sha = _artifact(
        root,
        artifacts.get("launcher"),
        "package launcher",
        expected_relative=package_verifier.LAUNCHER_RELATIVE.as_posix(),
        expected_executable=True,
        verify_bytes=verify_archive,
    )
    executable, executable_sha = _artifact(
        root,
        artifacts.get("executable"),
        "package executable",
        expected_relative=package_verifier.EXECUTABLE_RELATIVE.as_posix(),
        expected_executable=True,
        verify_bytes=verify_archive,
    )
    pak_record = _mapping(artifacts.get("pak"), "package pak", ARTIFACT_RECORD_KEYS)
    pak_relative = pak_record.get("relative_path")
    pak_parent = package_verifier.PAK_DIRECTORY_RELATIVE.as_posix() + "/"
    if (
        not isinstance(pak_relative, str)
        or not pak_relative.startswith(pak_parent)
        or "/" in pak_relative.removeprefix(pak_parent)
        or not pak_relative.endswith(".pak")
    ):
        raise PackagedProfileError("ARTIFACT_INVALID", "package pak path differs")
    pak, pak_sha = _artifact(
        root,
        pak_record,
        "package pak",
        expected_relative=pak_relative,
        expected_executable=False,
        verify_bytes=verify_archive,
    )

    trusted = _mapping(
        receipt.get("trusted_upstream"),
        "trusted upstream",
        TRUSTED_UPSTREAM_KEYS,
    )
    trusted_engine_root_value = trusted.get("engine_root")
    unreal_pak_value = trusted.get("unreal_pak")
    unreal_pak_sha = trusted.get("unreal_pak_sha256")
    if (
        trusted.get("policy") != "engine-root-derived-from-pinned-unrealpak/v1"
        or not isinstance(trusted_engine_root_value, str)
        or not isinstance(unreal_pak_value, str)
        or not isinstance(unreal_pak_sha, str)
        or SHA256_RE.fullmatch(unreal_pak_sha) is None
    ):
        raise PackagedProfileError(
            "TRUSTED_UPSTREAM_INVALID", "trusted engine binding differs"
        )
    trusted_engine_root = _canonical_existing(
        Path(trusted_engine_root_value), "trusted engine root", directory=True
    )
    unreal_pak = _canonical_existing(Path(unreal_pak_value), "trusted UnrealPak")
    if (
        unreal_pak != trusted_engine_root / "Engine/Binaries/Linux/UnrealPak"
        or not os.access(unreal_pak, os.X_OK)
        or (
            verify_archive
            and not hmac.compare_digest(sha256_file(unreal_pak), unreal_pak_sha)
        )
    ):
        raise PackagedProfileError(
            "TRUSTED_UPSTREAM_PIN_MISMATCH", "trusted UnrealPak bytes or path differ"
        )

    archive = _mapping(receipt.get("archive"), "archive observation", ARCHIVE_KEYS)
    tree_sha = archive.get("tree_sha256")
    file_count = archive.get("file_count")
    total_bytes = archive.get("total_bytes")
    secret_scan = _mapping(archive.get("secret_scan"), "archive secret scan")
    if (
        archive.get("algorithm") != "framed-canonical-file-record-sha256/v1"
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
            )
        except package_verifier.PackageReceiptError as exc:
            raise PackagedProfileError(
                "ARCHIVE_REHASH_FAILED", "package archive could not be re-hashed"
            ) from exc
        if live_archive != archive:
            raise PackagedProfileError(
                "ARCHIVE_PIN_MISMATCH", "package archive differs from its receipt"
            )

    return PackageBinding(
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
        map_path=EXPECTED_MAP_PATH,
        world_revision=EXPECTED_WORLD_REVISION,
    )


def revalidate_package(binding: PackageBinding) -> PackageBinding:
    observed = validate_package_attempt(binding.attempt_root, binding.receipt_sha256)
    if observed != binding:
        raise PackagedProfileError(
            "PACKAGE_IDENTITY_CHANGED", "package identity changed after profile validation"
        )
    return observed


def _validate_nvidia_icd(path: Path) -> Path:
    icd = _canonical_existing(path, "NVIDIA ICD")
    if icd.suffix != ".json":
        raise PackagedProfileError("NVIDIA_ICD_INVALID", "NVIDIA ICD must be a JSON file")
    return icd


def profile_from_binding(
    binding: PackageBinding,
    nvidia_icd: Path,
    *,
    runtime_profile: str | None = None,
) -> dict[str, Any]:
    validated_icd = _validate_nvidia_icd(nvidia_icd)
    try:
        spec = resolve_runtime_profile(runtime_profile)
    except RuntimeSafetyError as exc:
        raise PackagedProfileError(
            "PROFILE_FIXED_VALUE_INVALID",
            "runtime profile is not one of the closed profiles",
        ) from exc
    profile = {
        "schema": R2_PROFILE_SCHEMA if runtime_profile is not None else PROFILE_SCHEMA,
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
        "nvidia_icd_sha256": sha256_file(validated_icd),
    }
    if runtime_profile is not None:
        profile.update(
            {
                "runtime_profile": spec.runtime_profile,
                "camera_profile": spec.camera_profile,
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
        raise PackagedProfileError("OUTPUT_EXISTS", "profile output already exists") from exc
    except OSError as exc:
        raise PackagedProfileError("OUTPUT_WRITE_FAILED", "could not create profile") from exc
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
        raise PackagedProfileError("OUTPUT_WRITE_FAILED", "could not commit profile") from exc


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
    if stat.S_IMODE(profile_path.stat().st_mode) != 0o600:
        raise PackagedProfileError("PROFILE_MODE_INVALID", "packaged profile mode must be 0600")
    payload, _raw = _read_pinned_json(
        profile_path,
        expected_sha256,
        "packaged profile",
        maximum_bytes=MAX_PROFILE_BYTES,
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
    else:
        raise PackagedProfileError(
            "PROFILE_FIXED_VALUE_INVALID",
            "packaged profile schema differs",
        )
    if set(payload) != expected_keys:
        raise PackagedProfileError("PROFILE_SHAPE_INVALID", "packaged profile fields differ")
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
    if any(payload.get(key) != value for key, value in fixed.items()):
        raise PackagedProfileError("PROFILE_FIXED_VALUE_INVALID", "fixed profile values differ")
    attempt_value = payload.get("package_attempt")
    receipt_pin = payload.get("package_receipt_sha256")
    icd_value = payload.get("nvidia_icd")
    if not all(
        isinstance(value, str) and value
        for value in (attempt_value, receipt_pin, icd_value)
    ):
        raise PackagedProfileError("PROFILE_SHAPE_INVALID", "profile path fields are invalid")
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
            "PROFILE_IDENTITY_INVALID", "profile is not a direct package-attempt profile"
        )
    nvidia_icd = _validate_nvidia_icd(Path(icd_value))
    expected = profile_from_binding(
        binding,
        nvidia_icd,
        runtime_profile=runtime_profile,
    )
    if dict(payload) != expected:
        raise PackagedProfileError(
            "PROFILE_BINDING_MISMATCH", "profile differs from its sealed package receipt"
        )
    return PackagedProfileInputs(
        profile=profile_path,
        profile_sha256=expected_sha256,
        package=binding,
        nvidia_icd=nvidia_icd,
        nvidia_icd_sha256=expected["nvidia_icd_sha256"],
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
