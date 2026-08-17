#!/usr/bin/env python3
"""Run a bounded NullRHI smoke against the sealed VISTA Linux package.

The command launches the package's sealed game executable directly, binds the
adapter to a caller-selected available loopback port, proves the typed runtime
identity, and then terminates the process group it created.  The packaged shell
launcher remains part of the sealed archive evidence but is never executed, so
the managed process is the exact listener owner.  The smoke never publishes or
replaces the interactive ``game-runtime/current.json`` pointer.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from tools.runtime.vista_playable_home.runtime import (  # type: ignore
        DEFAULT_WORLD_REVISION,
        RuntimeSafetyError,
        probe_typed_runtime,
        validate_vista_world_port,
    )
    from tools.ue.vista_playable_home import package_receipt as package_verifier  # type: ignore
else:
    from .runtime import (
        DEFAULT_WORLD_REVISION,
        RuntimeSafetyError,
        probe_typed_runtime,
        validate_vista_world_port,
    )
    from tools.ue.vista_playable_home import package_receipt as package_verifier


PACKAGE_RECEIPT_SCHEMA = "simworld.vista.playable-home-linux-package-receipt/v1"
R2_PACKAGE_RECEIPT_SCHEMA = "simworld.vista.playable-home-linux-package-receipt/v2"
R2_EXACT_MODE_PACKAGE_RECEIPT_SCHEMA = (
    "simworld.vista.playable-home-linux-package-receipt/v3"
)
SMOKE_RECEIPT_SCHEMA = "simworld.vista.playable-home-packaged-smoke/v2"
EXPECTED_MAP_PATH = (
    "/Game/VISTA/PlayableHome/vista_playable_home_r1/Maps/VistaPlayableHome"
)
EXPECTED_ATTEMPT_PARENT = "package-linux-development"
PACKAGE_ATTEMPT_RE = re.compile(r"^attempt-[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
LAUNCHER_RELATIVE = Path("archive/Linux/VistaPlayableHome.sh")
PACKAGE_RECEIPT_RELATIVE = Path("package-receipt.json")
SMOKE_ATTEMPT_RE = re.compile(r"^attempt-[0-9]{2}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_JSON_BYTES = 4 * 1024 * 1024
DEFAULT_READY_TIMEOUT_SECONDS = 180.0
TRUSTED_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
PACKAGE_PROJECT_ARGUMENT = "VistaPlayableHome"
PROC_ROOT = Path("/proc")
LISTENER_OWNER_CLOSURE_SCOPE = (
    "single-loopback-inode+exact-managed-pid+visible-foreign-rejection/v1"
)


class PackagedSmokeError(RuntimeError):
    """A bounded, non-secret smoke failure."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class SmokeInputs:
    package_attempt: Path
    package_receipt: Path
    package_receipt_sha256: str
    package_receipt_mode: int
    receipt_schema: str
    exact_mode_attestation: bool
    receipt: Mapping[str, Any]
    launcher: Path
    launcher_sha256: str
    launcher_mode: int | None
    executable: Path
    executable_sha256: str
    executable_mode: int | None
    pak: Path
    pak_sha256: str
    pak_mode: int | None
    archive_root: Path
    archive_observation: Mapping[str, Any]
    archive_schema: str | None
    archive_algorithm: str
    archive_tree_sha256: str
    trusted_engine_root: Path
    unreal_pak: Path
    unreal_pak_sha256: str
    unreal_pak_mode: int | None
    trusted_exemptions: tuple[Mapping[str, Any], ...]
    project_descriptor: Path | None
    project_descriptor_sha256: str | None
    project_descriptor_mode: int | None
    project_config: Path | None
    project_config_sha256: str | None
    project_config_mode: int | None
    map_path: str
    output_dir: Path
    port: int
    timeout_seconds: float


@dataclass(frozen=True)
class FileSeal:
    sha256: str
    size: int
    mode: int
    identity: tuple[int, int, int, int, int, int]
    raw: bytes | None = None


Probe = Callable[..., dict[str, Any]]
ListenerProver = Callable[[int, int], dict[str, Any]]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        raise PackagedSmokeError(
            "JSON_INVALID", "smoke receipt is not finite JSON"
        ) from exc


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
    capture_bytes: bool = False,
    maximum_bytes: int | None = None,
) -> FileSeal:
    digest = hashlib.sha256()
    captured = bytearray() if capture_bytes else None
    try:
        before = os.lstat(path)
        if maximum_bytes is not None and not 0 < before.st_size <= maximum_bytes:
            raise PackagedSmokeError(
                "RECEIPT_SIZE_INVALID", f"{path.name} size is invalid"
            )
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if _file_identity(opened) != _file_identity(before):
                raise PackagedSmokeError(
                    "FILE_CHANGED", f"{path.name} changed while opening"
                )
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
                if captured is not None:
                    captured.extend(block)
        after = os.lstat(path)
        if _file_identity(after) != _file_identity(before):
            raise PackagedSmokeError(
                "FILE_CHANGED", f"{path.name} changed while hashing"
            )
    except PackagedSmokeError:
        raise
    except OSError as exc:
        raise PackagedSmokeError("READ_FAILED", f"could not hash {path.name}") from exc
    return FileSeal(
        sha256=digest.hexdigest(),
        size=after.st_size,
        mode=stat.S_IMODE(after.st_mode),
        identity=_file_identity(after),
        raw=bytes(captured) if captured is not None else None,
    )


def sha256_file(path: Path) -> str:
    return _sealed_file(path).sha256


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError(f"duplicate JSON key: {key}")
        output[key] = value
    return output


def _load_receipt(path: Path) -> tuple[Mapping[str, Any], FileSeal]:
    try:
        seal = _sealed_file(
            path,
            capture_bytes=True,
            maximum_bytes=MAX_JSON_BYTES,
        )
        if seal.raw is None:  # pragma: no cover - capture_bytes guarantees it.
            raise PackagedSmokeError(
                "RECEIPT_INVALID", "package receipt bytes are unavailable"
            )
        raw = seal.raw
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite constant: {value}")
            ),
        )
    except PackagedSmokeError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise PackagedSmokeError(
            "RECEIPT_INVALID", "package receipt is not strict JSON"
        ) from exc
    if not isinstance(value, dict):
        raise PackagedSmokeError("RECEIPT_INVALID", "package receipt must be an object")
    if canonical_json(value) != raw:
        raise PackagedSmokeError(
            "RECEIPT_INVALID", "package receipt is not canonical JSON"
        )
    return value, seal


def _canonical_existing(path: Path, label: str, *, directory: bool = False) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute() or ".." in candidate.parts or candidate.is_symlink():
        raise PackagedSmokeError(
            "PATH_IDENTITY_INVALID", f"{label} must be an absolute non-symlink path"
        )
    try:
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise PackagedSmokeError("PATH_MISSING", f"{label} does not exist") from exc
    correct_kind = resolved.is_dir() if directory else resolved.is_file()
    if resolved != candidate or not correct_kind:
        raise PackagedSmokeError(
            "PATH_IDENTITY_INVALID", f"{label} must name its real path identity"
        )
    return resolved


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise PackagedSmokeError("RECEIPT_SHAPE_INVALID", f"{label} must be an object")
    return value


def _exact_mode(value: Any, label: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= 0o7777
    ):
        raise PackagedSmokeError(
            "PACKAGE_MODE_INVALID", f"{label} exact mode is invalid"
        )
    return value


def _artifact_mode(
    record: Mapping[str, Any],
    observed: FileSeal,
    label: str,
    *,
    exact_modes: bool,
) -> int | None:
    if not exact_modes:
        return None
    if set(record) != {"relative_path", "sha256", "bytes", "executable", "mode"}:
        raise PackagedSmokeError(
            "PACKAGE_MODE_INVALID", f"{label} exact-mode fields differ"
        )
    expected = _exact_mode(record.get("mode"), label)
    if observed.mode != expected:
        raise PackagedSmokeError("PACKAGE_MODE_MISMATCH", f"{label} exact mode differs")
    return expected


def validate_inputs(args: argparse.Namespace) -> SmokeInputs:
    root = _canonical_existing(
        Path(args.package_attempt), "package attempt", directory=True
    )
    if (
        root.parent.name != EXPECTED_ATTEMPT_PARENT
        or PACKAGE_ATTEMPT_RE.fullmatch(root.name) is None
    ):
        raise PackagedSmokeError(
            "ATTEMPT_IDENTITY_INVALID",
            "package attempt must be a safe package-linux-development/attempt-<id>",
        )
    receipt_path = _canonical_existing(
        root / PACKAGE_RECEIPT_RELATIVE, "package receipt"
    )
    pin = str(args.package_receipt_sha256 or "")
    receipt, receipt_seal = _load_receipt(receipt_path)
    receipt_mode = receipt_seal.mode
    if receipt_mode != 0o600:
        raise PackagedSmokeError(
            "PACKAGE_RECEIPT_MODE_INVALID", "package receipt mode must be 0600"
        )
    if not SHA256_RE.fullmatch(pin) or not hmac.compare_digest(
        receipt_seal.sha256, pin
    ):
        raise PackagedSmokeError("PACKAGE_PIN_MISMATCH", "package receipt SHA differs")
    receipt_schema = receipt.get("schema")
    if (
        receipt_schema
        not in {
            PACKAGE_RECEIPT_SCHEMA,
            R2_PACKAGE_RECEIPT_SCHEMA,
            R2_EXACT_MODE_PACKAGE_RECEIPT_SCHEMA,
        }
        or receipt.get("status") != "accepted"
        or receipt.get("attempt_root") != str(root)
    ):
        raise PackagedSmokeError(
            "PACKAGE_RECEIPT_INVALID", "package receipt identity differs"
        )
    exact_modes = receipt_schema == R2_EXACT_MODE_PACKAGE_RECEIPT_SCHEMA

    bindings = _mapping(receipt.get("bindings"), "package bindings")
    map_path = bindings.get("map_path")
    if (
        map_path != EXPECTED_MAP_PATH
        or bindings.get("world_revision") != DEFAULT_WORLD_REVISION
    ):
        raise PackagedSmokeError(
            "PACKAGE_MAP_INVALID", "package map or revision differs"
        )
    if receipt_schema in {
        R2_PACKAGE_RECEIPT_SCHEMA,
        R2_EXACT_MODE_PACKAGE_RECEIPT_SCHEMA,
    } and (
        bindings.get("runtime_profile") != package_verifier.R2_RUNTIME_PROFILE
        or bindings.get("camera_profile") != package_verifier.R2_CAMERA_PROFILE
        or bindings.get("accepted_display") != package_verifier.R2_DISPLAY
        or bindings.get("accepted_gpu") != package_verifier.R2_GPU
        or bindings.get("accepted_vista_world_port")
        != package_verifier.R2_VISTA_WORLD_PORT
        or bindings.get("accepted_width") != package_verifier.R2_WIDTH
        or bindings.get("accepted_height") != package_verifier.R2_HEIGHT
        or bindings.get("accepted_fps") != package_verifier.R2_FPS
    ):
        raise PackagedSmokeError(
            "PACKAGE_PROFILE_INVALID", "r2 package profile binding differs"
        )
    artifacts = _mapping(receipt.get("artifacts"), "package artifacts")
    launcher_record = _mapping(artifacts.get("launcher"), "launcher artifact")
    if launcher_record.get("relative_path") != LAUNCHER_RELATIVE.as_posix():
        raise PackagedSmokeError("LAUNCHER_IDENTITY_INVALID", "launcher path differs")
    launcher = _canonical_existing(root / LAUNCHER_RELATIVE, "package launcher")
    launcher_sha = launcher_record.get("sha256")
    launcher_seal = _sealed_file(launcher)
    if (
        not isinstance(launcher_sha, str)
        or not SHA256_RE.fullmatch(launcher_sha)
        or not hmac.compare_digest(launcher_seal.sha256, launcher_sha)
        or launcher_seal.size != launcher_record.get("bytes")
        or launcher_record.get("executable") is not True
        or not bool(launcher_seal.mode & 0o111)
    ):
        raise PackagedSmokeError(
            "LAUNCHER_PIN_MISMATCH", "launcher bytes or mode differ"
        )
    launcher_mode = _artifact_mode(
        launcher_record,
        launcher_seal,
        "package launcher",
        exact_modes=exact_modes,
    )
    archive = _mapping(receipt.get("archive"), "package archive")
    tree_sha = archive.get("tree_sha256")
    secret_scan = _mapping(archive.get("secret_scan"), "package secret scan")
    if (
        not isinstance(tree_sha, str)
        or SHA256_RE.fullmatch(tree_sha) is None
        or secret_scan.get("matches") != 0
        or (
            exact_modes
            and (
                set(archive)
                != {
                    "schema",
                    "algorithm",
                    "file_count",
                    "total_bytes",
                    "tree_sha256",
                    "secret_scan",
                }
                or archive.get("schema")
                != package_verifier.ARCHIVE_SCHEMA_EXACT_MODE_V2
                or archive.get("algorithm")
                != package_verifier.ARCHIVE_ALGORITHM_EXACT_MODE_V2
            )
        )
        or (
            not exact_modes
            and archive.get("algorithm") != package_verifier.ARCHIVE_ALGORITHM_V1
        )
    ):
        raise PackagedSmokeError(
            "PACKAGE_ARCHIVE_INVALID", "package archive or secret-scan evidence differs"
        )
    exemptions = secret_scan.get("trusted_upstream_exemptions")
    if (
        secret_scan.get("policy") != "high-confidence-credential-byte-patterns/v1"
        or secret_scan.get("trusted_upstream_exemption_policy")
        != "archive-Engine-path-and-byte-identical-pinned-engine-counterpart/v1"
        or not isinstance(exemptions, list)
        or secret_scan.get("trusted_upstream_exemption_count") != len(exemptions)
    ):
        raise PackagedSmokeError(
            "PACKAGE_ARCHIVE_INVALID", "trusted-upstream exemption evidence differs"
        )
    allowed_rules = {name for name, _pattern in package_verifier.SECRET_PATTERNS}
    normalized_exemptions: list[Mapping[str, Any]] = []
    for exemption in exemptions:
        if not isinstance(exemption, dict):
            raise PackagedSmokeError(
                "PACKAGE_ARCHIVE_INVALID", "trusted-upstream exemption is invalid"
            )
        archive_relative = exemption.get("archive_relative_path")
        upstream_relative = exemption.get("upstream_relative_path")
        rules = exemption.get("rules")
        if (
            exemption.get("policy") != "byte-identical-pinned-engine-counterpart/v1"
            or not isinstance(archive_relative, str)
            or not archive_relative.startswith("Engine/")
            or upstream_relative != archive_relative
            or not isinstance(exemption.get("sha256"), str)
            or SHA256_RE.fullmatch(exemption["sha256"]) is None
            or not isinstance(rules, list)
            or not rules
            or rules != sorted(set(rules))
            or not set(rules).issubset(allowed_rules)
        ):
            raise PackagedSmokeError(
                "PACKAGE_ARCHIVE_INVALID",
                "trusted-upstream exemption is outside policy",
            )
        normalized_exemptions.append(dict(exemption))
    if secret_scan.get("pattern_hits") != sum(
        len(exemption["rules"]) for exemption in normalized_exemptions
    ):
        raise PackagedSmokeError(
            "PACKAGE_ARCHIVE_INVALID", "trusted-upstream hit count differs"
        )

    trusted_upstream = _mapping(receipt.get("trusted_upstream"), "trusted upstream")
    engine_root = _canonical_existing(
        Path(str(trusted_upstream.get("engine_root", ""))),
        "trusted engine root",
        directory=True,
    )
    unreal_pak = _canonical_existing(
        Path(str(trusted_upstream.get("unreal_pak", ""))), "trusted UnrealPak"
    )
    unreal_pak_sha = trusted_upstream.get("unreal_pak_sha256")
    unreal_pak_seal = _sealed_file(unreal_pak)
    unreal_pak_mode = (
        _exact_mode(trusted_upstream.get("unreal_pak_mode"), "trusted UnrealPak")
        if exact_modes
        else None
    )
    if (
        trusted_upstream.get("policy") != "engine-root-derived-from-pinned-unrealpak/v1"
        or unreal_pak != engine_root / "Engine/Binaries/Linux/UnrealPak"
        or not isinstance(unreal_pak_sha, str)
        or SHA256_RE.fullmatch(unreal_pak_sha) is None
        or not hmac.compare_digest(unreal_pak_seal.sha256, unreal_pak_sha)
        or not bool(unreal_pak_seal.mode & 0o111)
        or (
            exact_modes
            and (
                set(trusted_upstream)
                != {
                    "policy",
                    "engine_root",
                    "unreal_pak",
                    "unreal_pak_sha256",
                    "mode_policy",
                    "unreal_pak_mode",
                }
                or trusted_upstream.get("mode_policy") != "sealed-exact-stat-imode/v1"
                or unreal_pak_seal.mode != unreal_pak_mode
            )
        )
    ):
        raise PackagedSmokeError(
            "TRUSTED_UPSTREAM_INVALID", "trusted engine binding differs"
        )
    archive_root = _canonical_existing(
        root / "archive" / "Linux", "package archive", directory=True
    )
    project_descriptor: Path | None = None
    project_descriptor_sha: str | None = None
    project_descriptor_mode: int | None = None
    project_config: Path | None = None
    project_config_sha: str | None = None
    project_config_mode: int | None = None
    if exact_modes:
        project_policy = _mapping(receipt.get("project_policy"), "project policy")
        if set(project_policy) != {
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
        }:
            raise PackagedSmokeError(
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
        project_descriptor_seal = _sealed_file(project_descriptor)
        project_config_seal = _sealed_file(project_config)
        project_descriptor_sha = project_policy.get("project_descriptor_sha256")
        project_config_sha = project_policy.get("project_config_sha256")
        project_descriptor_mode = _exact_mode(
            project_policy.get("project_descriptor_mode"),
            "package project descriptor",
        )
        project_config_mode = _exact_mode(
            project_policy.get("project_config_mode"),
            "package project config",
        )
        if (
            project_policy.get("project_descriptor") != str(project_descriptor)
            or project_policy.get("project_config") != str(project_config)
            or project_policy.get("mode_policy") != "sealed-exact-stat-imode/v1"
            or project_policy.get("enabled_plugins") != ["VistaPlayableHome"]
            or project_policy.get("disabled_plugins")
            != [
                "AndroidFileServer",
                "EditorScriptingUtilities",
                "Interchange",
                "PythonScriptPlugin",
            ]
            or project_policy.get("host_module") != "VistaPlayableHomeHost"
            or project_policy.get("android_file_server_enabled") is not False
            or not isinstance(project_descriptor_sha, str)
            or SHA256_RE.fullmatch(project_descriptor_sha) is None
            or not isinstance(project_config_sha, str)
            or SHA256_RE.fullmatch(project_config_sha) is None
            or not hmac.compare_digest(
                project_descriptor_seal.sha256, project_descriptor_sha
            )
            or not hmac.compare_digest(project_config_seal.sha256, project_config_sha)
            or project_descriptor_seal.mode != project_descriptor_mode
            or project_config_seal.mode != project_config_mode
        ):
            raise PackagedSmokeError(
                "PROJECT_POLICY_PIN_MISMATCH",
                "package project/config bytes or exact modes differ",
            )

    executable_record = _mapping(artifacts.get("executable"), "executable artifact")
    expected_executable_relative = (
        "archive/Linux/VistaPlayableHome/Binaries/Linux/VistaPlayableHome"
    )
    if executable_record.get("relative_path") != expected_executable_relative:
        raise PackagedSmokeError(
            "EXECUTABLE_IDENTITY_INVALID", "game executable path differs"
        )
    executable = _canonical_existing(
        root / expected_executable_relative, "game executable"
    )
    executable_sha = executable_record.get("sha256")
    executable_seal = _sealed_file(executable)
    if (
        not isinstance(executable_sha, str)
        or SHA256_RE.fullmatch(executable_sha) is None
        or not hmac.compare_digest(executable_seal.sha256, executable_sha)
        or executable_seal.size != executable_record.get("bytes")
        or executable_record.get("executable") is not True
        or not bool(executable_seal.mode & 0o111)
    ):
        raise PackagedSmokeError(
            "EXECUTABLE_PIN_MISMATCH", "game executable bytes differ"
        )
    executable_mode = _artifact_mode(
        executable_record,
        executable_seal,
        "package executable",
        exact_modes=exact_modes,
    )

    pak_record = _mapping(artifacts.get("pak"), "pak artifact")
    pak_relative = pak_record.get("relative_path")
    expected_pak_parent = "archive/Linux/VistaPlayableHome/Content/Paks/"
    if (
        not isinstance(pak_relative, str)
        or not pak_relative.startswith(expected_pak_parent)
        or "/" in pak_relative.removeprefix(expected_pak_parent)
        or not pak_relative.endswith(".pak")
    ):
        raise PackagedSmokeError("PAK_IDENTITY_INVALID", "pak path differs")
    pak = _canonical_existing(root / pak_relative, "package pak")
    pak_sha = pak_record.get("sha256")
    pak_seal = _sealed_file(pak)
    if (
        not isinstance(pak_sha, str)
        or SHA256_RE.fullmatch(pak_sha) is None
        or not hmac.compare_digest(pak_seal.sha256, pak_sha)
        or pak_seal.size != pak_record.get("bytes")
        or pak_record.get("executable") is not False
        or bool(pak_seal.mode & 0o111)
    ):
        raise PackagedSmokeError("PAK_PIN_MISMATCH", "pak bytes differ")
    pak_mode = _artifact_mode(
        pak_record,
        pak_seal,
        "package pak",
        exact_modes=exact_modes,
    )

    output = Path(args.output_dir).expanduser()
    expected_parent = root / "smoke"
    if (
        not output.is_absolute()
        or ".." in output.parts
        or output.parent != expected_parent
        or SMOKE_ATTEMPT_RE.fullmatch(output.name) is None
        or output.exists()
        or output.is_symlink()
    ):
        raise PackagedSmokeError(
            "OUTPUT_IDENTITY_INVALID",
            "output must be a fresh package-attempt/smoke/attempt-NN directory",
        )
    timeout = float(args.timeout_seconds)
    if not 5.0 <= timeout <= 600.0:
        raise PackagedSmokeError(
            "TIMEOUT_INVALID", "timeout must be from 5 through 600 seconds"
        )
    try:
        port = validate_vista_world_port(args.vista_world_port)
    except RuntimeSafetyError as exc:
        raise PackagedSmokeError("PORT_INVALID", str(exc)) from exc

    return SmokeInputs(
        package_attempt=root,
        package_receipt=receipt_path,
        package_receipt_sha256=pin,
        package_receipt_mode=receipt_mode,
        receipt_schema=str(receipt_schema),
        exact_mode_attestation=exact_modes,
        receipt=receipt,
        launcher=launcher,
        launcher_sha256=launcher_sha,
        launcher_mode=launcher_mode,
        executable=executable,
        executable_sha256=executable_sha,
        executable_mode=executable_mode,
        pak=pak,
        pak_sha256=pak_sha,
        pak_mode=pak_mode,
        archive_root=archive_root,
        archive_observation=archive,
        archive_schema=(archive.get("schema") if exact_modes else None),
        archive_algorithm=str(archive["algorithm"]),
        archive_tree_sha256=tree_sha,
        trusted_engine_root=engine_root,
        unreal_pak=unreal_pak,
        unreal_pak_sha256=unreal_pak_sha,
        unreal_pak_mode=unreal_pak_mode,
        trusted_exemptions=tuple(normalized_exemptions),
        project_descriptor=project_descriptor,
        project_descriptor_sha256=project_descriptor_sha,
        project_descriptor_mode=project_descriptor_mode,
        project_config=project_config,
        project_config_sha256=project_config_sha,
        project_config_mode=project_config_mode,
        map_path=str(map_path),
        output_dir=output,
        port=port,
        timeout_seconds=timeout,
    )


def build_command(inputs: SmokeInputs) -> list[str]:
    user_dir = inputs.output_dir / "ue-user"
    return [
        str(inputs.executable),
        PACKAGE_PROJECT_ARGUMENT,
        inputs.map_path,
        "-nullrhi",
        "-unattended",
        "-NOSPLASH",
        "-NOSOUND",
        "-NoAnalytics",
        f"-VistaWorldPort={inputs.port}",
        "-UDPMESSAGING_TRANSPORT_ENABLE=0",
        "-ini:Engine:[/Script/TcpMessaging.TcpMessagingSettings]:EnableTransport=False",
        "-SaveToUserDir",
        f"-UserDir={user_dir}",
        "-stdout",
        "-FullStdOutLogOutput",
        "-log",
    ]


def sanitized_environment(inputs: SmokeInputs) -> dict[str, str]:
    return {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": TRUSTED_PATH,
        "HOME": str(inputs.output_dir / "home"),
        "XDG_CACHE_HOME": str(inputs.output_dir / "xdg-cache"),
        "XDG_CONFIG_HOME": str(inputs.output_dir / "xdg-config"),
        "SDL_VIDEODRIVER": "dummy",
        "CUDA_VISIBLE_DEVICES": "",
    }


def open_private_exclusive(path: Path):
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        # os.open's creation mode is umask-filtered.  The smoke log and receipt
        # are private evidence files, so establish the required final mode on
        # the open descriptor before exposing it to a buffered writer.
        os.fchmod(descriptor, 0o600)
        handle = os.fdopen(descriptor, "wb")
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            # Wrapper construction may already have closed the descriptor.
            # Cleanup must still unlink and preserve the original exception.
            pass
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        raise
    return handle


def _process_start_ticks(pid: int) -> int | None:
    try:
        fields = (PROC_ROOT / str(pid) / "stat").read_text(encoding="utf-8").split()
    except (FileNotFoundError, PermissionError, OSError):
        return None
    return int(fields[21]) if len(fields) > 21 else None


def process_effective_uid(pid: int) -> int:
    """Return the effective UID recorded by procfs, or fail closed.

    The managed process identity and its descriptor table are mandatory
    listener proof.  Linux can hide descriptor tables of unrelated same-UID
    non-dumpable processes, so those tables are scanned only as additional
    visible-foreign-holder evidence.
    """

    try:
        lines = (
            (PROC_ROOT / str(pid) / "status").read_text(encoding="utf-8").splitlines()
        )
    except (FileNotFoundError, PermissionError, OSError, UnicodeDecodeError) as exc:
        raise PackagedSmokeError(
            "LISTENER_VISIBILITY_INCOMPLETE",
            "could not establish the expected process effective UID",
        ) from exc
    uid_line = next((line for line in lines if line.startswith("Uid:")), None)
    if uid_line is None:
        raise PackagedSmokeError(
            "LISTENER_VISIBILITY_INCOMPLETE",
            "expected process effective UID is absent from procfs",
        )
    fields = uid_line.split()
    try:
        effective_uid = int(fields[2])
    except (IndexError, ValueError) as exc:
        raise PackagedSmokeError(
            "LISTENER_VISIBILITY_INCOMPLETE",
            "expected process effective UID is malformed",
        ) from exc
    if effective_uid < 0:
        raise PackagedSmokeError(
            "LISTENER_VISIBILITY_INCOMPLETE",
            "expected process effective UID is invalid",
        )
    return effective_uid


def verify_sealed_archive(inputs: SmokeInputs) -> dict[str, Any]:
    """Recompute the sealed archive without trusting the earlier receipt."""

    try:
        observation = package_verifier.inspect_archive(
            inputs.archive_root,
            trusted_engine_root=inputs.trusted_engine_root,
            exact_modes=inputs.exact_mode_attestation,
        )
    except package_verifier.PackageReceiptError as exc:
        raise PackagedSmokeError(
            "PACKAGE_ARCHIVE_DRIFT", "package archive could not be reverified"
        ) from exc
    observed_scan = _mapping(observation.get("secret_scan"), "live secret scan")
    launcher_seal = _sealed_file(inputs.launcher)
    executable_seal = _sealed_file(inputs.executable)
    pak_seal = _sealed_file(inputs.pak)
    receipt_seal = _sealed_file(inputs.package_receipt)
    unreal_pak_seal = _sealed_file(inputs.unreal_pak)
    project_drift = False
    if inputs.exact_mode_attestation:
        if (
            inputs.project_descriptor is None
            or inputs.project_descriptor_sha256 is None
            or inputs.project_descriptor_mode is None
            or inputs.project_config is None
            or inputs.project_config_sha256 is None
            or inputs.project_config_mode is None
        ):
            project_drift = True
        else:
            project_descriptor_seal = _sealed_file(inputs.project_descriptor)
            project_config_seal = _sealed_file(inputs.project_config)
            project_drift = (
                not hmac.compare_digest(
                    project_descriptor_seal.sha256,
                    inputs.project_descriptor_sha256,
                )
                or project_descriptor_seal.mode != inputs.project_descriptor_mode
                or not hmac.compare_digest(
                    project_config_seal.sha256,
                    inputs.project_config_sha256,
                )
                or project_config_seal.mode != inputs.project_config_mode
            )
    if (
        observation != inputs.archive_observation
        or observation.get("tree_sha256") != inputs.archive_tree_sha256
        or observed_scan.get("matches") != 0
        or observed_scan.get("trusted_upstream_exemptions")
        != [dict(value) for value in inputs.trusted_exemptions]
        or not hmac.compare_digest(launcher_seal.sha256, inputs.launcher_sha256)
        or not hmac.compare_digest(executable_seal.sha256, inputs.executable_sha256)
        or not hmac.compare_digest(pak_seal.sha256, inputs.pak_sha256)
        or not hmac.compare_digest(receipt_seal.sha256, inputs.package_receipt_sha256)
        or receipt_seal.mode != inputs.package_receipt_mode
        or not hmac.compare_digest(unreal_pak_seal.sha256, inputs.unreal_pak_sha256)
        or (
            inputs.exact_mode_attestation
            and (
                inputs.launcher_mode is None
                or inputs.executable_mode is None
                or inputs.pak_mode is None
                or inputs.unreal_pak_mode is None
                or launcher_seal.mode != inputs.launcher_mode
                or executable_seal.mode != inputs.executable_mode
                or pak_seal.mode != inputs.pak_mode
                or unreal_pak_seal.mode != inputs.unreal_pak_mode
                or project_drift
            )
        )
    ):
        raise PackagedSmokeError(
            "PACKAGE_ARCHIVE_DRIFT", "package archive differs from its sealed receipt"
        )
    return {
        "tree_sha256": observation["tree_sha256"],
        "file_count": observation["file_count"],
        "total_bytes": observation["total_bytes"],
        "secret_matches": 0,
        "trusted_upstream_exemption_count": len(inputs.trusted_exemptions),
    }


def _listening_loopback_inodes(port: int) -> set[int]:
    output: set[int] = set()
    expected_port = f"{port:04X}"
    for proc_path, ipv4 in (
        (Path("/proc/net/tcp"), True),
        (Path("/proc/net/tcp6"), False),
    ):
        try:
            lines = proc_path.read_text(encoding="ascii").splitlines()[1:]
        except (OSError, UnicodeDecodeError) as exc:
            raise PackagedSmokeError(
                "LISTENER_PROOF_FAILED", "could not inspect kernel TCP listeners"
            ) from exc
        for line in lines:
            fields = line.split()
            if len(fields) < 10 or fields[3] != "0A":
                continue
            address, separator, port_hex = fields[1].partition(":")
            if separator != ":" or port_hex.upper() != expected_port:
                continue
            # The runtime adapter is required to bind IPv4 127.0.0.1.  Do not
            # accept wildcard or externally reachable listeners as proof.
            if not ipv4 or address.upper() != "0100007F":
                continue
            try:
                output.add(int(fields[9]))
            except ValueError as exc:
                raise PackagedSmokeError(
                    "LISTENER_PROOF_FAILED", "kernel listener inode is invalid"
                ) from exc
    return output


def _global_socket_owners(
    inodes: set[int],
    expected_effective_uid: int,
    expected_process_group: int,
) -> dict[int, list[dict[str, int]]]:
    """Enumerate every visible process holding listener inodes.

    The expected process group's descriptor tables are mandatory proof and
    therefore fail closed when unreadable.  Other same-UID processes are also
    scanned so any visible inherited or passed listener descriptor is
    rejected by the caller.  Linux may legitimately hide an unrelated
    same-UID process's descriptor table (for example after it becomes
    non-dumpable); that unrelated table is not part of the sealed launch
    group and does not invalidate proof that the single kernel listener is
    held by the managed group.
    """

    owner_sets: dict[int, set[tuple[int, int]]] = {inode: set() for inode in inodes}
    try:
        proc_entries = list(PROC_ROOT.iterdir())
    except OSError as exc:
        raise PackagedSmokeError(
            "LISTENER_VISIBILITY_INCOMPLETE", "could not enumerate processes"
        ) from exc
    for entry in proc_entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        is_expected_process = pid == expected_process_group
        try:
            effective_uid = process_effective_uid(pid)
        except PackagedSmokeError:
            # A process may disappear at any point during procfs traversal.
            if not entry.exists():
                continue
            if is_expected_process:
                raise
            # Foreign descriptor inspection is additional visible-holder
            # rejection, not part of the exact managed-PID proof.
            continue
        if is_expected_process and effective_uid != expected_effective_uid:
            raise PackagedSmokeError(
                "LISTENER_VISIBILITY_INCOMPLETE",
                "managed process effective UID changed during ownership proof",
            )
        try:
            raw_stat = (entry / "stat").read_text(encoding="utf-8")
            closing = raw_stat.rfind(")")
            fields = raw_stat[closing + 2 :].split()
            if closing < 0 or len(fields) < 3:
                if is_expected_process:
                    raise PackagedSmokeError(
                        "LISTENER_VISIBILITY_INCOMPLETE",
                        "managed process identity is malformed",
                    )
                continue
            process_group = int(fields[2])
        except (FileNotFoundError, ProcessLookupError):
            continue
        except (PermissionError, OSError, UnicodeDecodeError, ValueError) as exc:
            if is_expected_process:
                raise PackagedSmokeError(
                    "LISTENER_VISIBILITY_INCOMPLETE",
                    "managed process identity is unreadable",
                ) from exc
            continue
        try:
            descriptors = list((entry / "fd").iterdir())
        except (FileNotFoundError, ProcessLookupError):
            continue
        except (PermissionError, OSError) as exc:
            if is_expected_process:
                raise PackagedSmokeError(
                    "LISTENER_VISIBILITY_INCOMPLETE",
                    "managed process-group descriptor table is unreadable",
                ) from exc
            continue
        for descriptor in descriptors:
            try:
                target = os.readlink(descriptor)
            except FileNotFoundError:
                continue
            except (PermissionError, OSError) as exc:
                if is_expected_process:
                    raise PackagedSmokeError(
                        "LISTENER_VISIBILITY_INCOMPLETE",
                        "managed process-group descriptor link is unreadable",
                    ) from exc
                continue
            match = re.fullmatch(r"socket:\[([0-9]+)\]", target)
            if match and int(match.group(1)) in owner_sets:
                owner_sets[int(match.group(1))].add((pid, process_group))
    return {
        inode: [
            {"pid": pid, "process_group": process_group}
            for pid, process_group in sorted(records)
        ]
        for inode, records in owner_sets.items()
    }


def prove_loopback_listener_ownership(port: int, process_group: int) -> dict[str, Any]:
    inodes_before = _listening_loopback_inodes(port)
    if len(inodes_before) != 1:
        raise PackagedSmokeError(
            "LISTENER_OWNERSHIP_INVALID", "expected exactly one loopback listener"
        )
    expected_effective_uid = process_effective_uid(process_group)
    owners = _global_socket_owners(
        inodes_before,
        expected_effective_uid,
        process_group,
    )
    inodes_after = _listening_loopback_inodes(port)
    if inodes_after != inodes_before:
        raise PackagedSmokeError(
            "LISTENER_OWNERSHIP_INVALID",
            "loopback listener identity changed during ownership proof",
        )
    inode = next(iter(inodes_before))
    owner_records = owners.get(inode, [])
    if owner_records != [{"pid": process_group, "process_group": process_group}]:
        raise PackagedSmokeError(
            "LISTENER_OWNERSHIP_INVALID",
            "typed listener is not owned only by the exact managed process",
        )
    return {
        "host": "127.0.0.1",
        "port": port,
        "socket_inode": inode,
        "process_group": process_group,
        "owner_pids": [process_group],
    }


def wait_for_readiness(
    process: subprocess.Popen[Any],
    port: int,
    timeout_seconds: float,
    *,
    probe: Probe = probe_typed_runtime,
    listener_prover: ListenerProver = prove_loopback_listener_ownership,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise PackagedSmokeError(
                "PROCESS_EXITED", "package exited before readiness"
            )
        try:
            first = probe(
                port,
                expected_revision=DEFAULT_WORLD_REVISION,
                timeout=1.0,
            )
            if process.poll() is not None:
                raise PackagedSmokeError(
                    "PROCESS_EXITED", "package exited after the first typed probe"
                )
            second = probe(
                port,
                expected_revision=DEFAULT_WORLD_REVISION,
                timeout=1.0,
            )
            listener = listener_prover(port, process.pid)
            return {
                "probe_count": 2,
                "stable": True,
                "responses": [first, second],
                "listener_ownership": listener,
            }
        except RuntimeSafetyError as exc:
            last_error = exc
            time.sleep(0.25)
    raise PackagedSmokeError(
        "READINESS_TIMEOUT",
        "typed runtime did not become ready before the bounded timeout",
    ) from last_error


def terminate_owned_process(
    process: subprocess.Popen[Any], *, term_timeout_seconds: float = 15.0
) -> dict[str, Any]:
    process_group = process.pid
    if process.poll() is None:
        try:
            observed_group = os.getpgid(process.pid)
        except (ProcessLookupError, PermissionError, OSError) as exc:
            raise PackagedSmokeError(
                "PROCESS_IDENTITY_LOST", "owned process identity was lost"
            ) from exc
        if observed_group != process_group:
            raise PackagedSmokeError(
                "PROCESS_GROUP_INVALID",
                "smoke process is not leader of its owned session",
            )

    def group_exists() -> bool:
        try:
            os.killpg(process_group, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def wait_group_gone(timeout_seconds: float) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if not group_exists():
                return True
            time.sleep(0.05)
        return not group_exists()

    if not group_exists():
        return {
            "signal": None,
            "escalated": False,
            "exit_code": process.returncode,
            "process_exited": process.poll() is not None,
            "process_group_exited": True,
        }
    escalated = False
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        pass
    if process.poll() is None:
        try:
            process.wait(timeout=term_timeout_seconds)
        except subprocess.TimeoutExpired:
            pass
    if not wait_group_gone(min(term_timeout_seconds, 2.0)):
        escalated = True
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if process.poll() is None:
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                pass
        if not wait_group_gone(5.0):
            raise PackagedSmokeError(
                "TERMINATION_FAILED", "owned process group survived SIGKILL"
            )
    return {
        "signal": "SIGKILL" if escalated else "SIGTERM",
        "escalated": escalated,
        "exit_code": process.returncode,
        "process_exited": process.poll() is not None,
        "process_group_exited": not group_exists(),
    }


def _write_receipt(path: Path, receipt: Mapping[str, Any]) -> str:
    raw = canonical_json(receipt)
    handle = open_private_exclusive(path)
    try:
        with handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        raise
    return sha256_bytes(raw)


def _error_record(error: BaseException) -> dict[str, str]:
    if isinstance(error, PackagedSmokeError):
        return {"type": type(error).__name__, "code": error.code, "message": str(error)}
    if isinstance(error, OSError):
        return {
            "type": type(error).__name__,
            "code": "OS_ERROR",
            "message": "the packaged smoke encountered a local operating-system error",
        }
    return {
        "type": type(error).__name__,
        "code": "SMOKE_INTERNAL_ERROR",
        "message": "the packaged smoke failed before acceptance",
    }


def run_smoke(
    inputs: SmokeInputs,
    *,
    popen_factory: Callable[..., subprocess.Popen[Any]] = subprocess.Popen,
    probe: Probe = probe_typed_runtime,
    listener_prover: ListenerProver = prove_loopback_listener_ownership,
) -> tuple[dict[str, Any], str]:
    smoke_parent = inputs.package_attempt / "smoke"
    if smoke_parent.exists():
        smoke_parent = _canonical_existing(smoke_parent, "smoke root", directory=True)
    else:
        smoke_parent.mkdir(mode=0o700)
    inputs.output_dir.mkdir(mode=0o700, exist_ok=False)
    for directory in ("ue-user", "home", "xdg-cache", "xdg-config"):
        (inputs.output_dir / directory).mkdir(mode=0o700)

    log_path = inputs.output_dir / "packaged-smoke.log"
    receipt_path = inputs.output_dir / "smoke-receipt.json"
    command = build_command(inputs)
    created_at = utc_now()
    process: subprocess.Popen[Any] | None = None
    readiness: dict[str, Any] | None = None
    termination: dict[str, Any] | None = None
    error: BaseException | None = None
    process_record: dict[str, Any] | None = None
    archive_before: dict[str, Any] | None = None
    archive_after: dict[str, Any] | None = None
    with open_private_exclusive(log_path) as log_handle:
        try:
            archive_before = verify_sealed_archive(inputs)
            process = popen_factory(
                command,
                cwd=inputs.archive_root,
                env=sanitized_environment(inputs),
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            start_ticks = _process_start_ticks(process.pid)
            if start_ticks is None:
                raise PackagedSmokeError(
                    "PROCESS_IDENTITY_INVALID",
                    "could not bind the owned process identity",
                )
            process_record = {
                "pid": process.pid,
                "start_ticks": start_ticks,
                "process_group": os.getpgid(process.pid),
            }
            if process_record["process_group"] != process.pid:
                raise PackagedSmokeError(
                    "PROCESS_GROUP_INVALID",
                    "smoke process did not create an owned session",
                )
            readiness = wait_for_readiness(
                process,
                inputs.port,
                inputs.timeout_seconds,
                probe=probe,
                listener_prover=listener_prover,
            )
        except BaseException as exc:
            error = exc
        finally:
            if process is not None:
                try:
                    termination = terminate_owned_process(process)
                except BaseException as terminate_error:
                    if error is None:
                        error = terminate_error
            try:
                archive_after = verify_sealed_archive(inputs)
            except BaseException as archive_error:
                if error is None:
                    error = archive_error
            log_handle.flush()
            os.fsync(log_handle.fileno())

    accepted = (
        error is None
        and readiness is not None
        and termination is not None
        and termination.get("process_exited") is True
        and termination.get("process_group_exited") is True
        and archive_before is not None
        and archive_after == archive_before
    )
    receipt: dict[str, Any] = {
        "schema": SMOKE_RECEIPT_SCHEMA,
        "status": "accepted" if accepted else "failed",
        "created_at": created_at,
        "completed_at": utc_now(),
        "bindings": {
            "package_attempt": str(inputs.package_attempt),
            "package_receipt": str(inputs.package_receipt),
            "package_receipt_sha256": inputs.package_receipt_sha256,
            "launcher_sha256": inputs.launcher_sha256,
            "executable_sha256": inputs.executable_sha256,
            "archive_tree_sha256": _mapping(
                inputs.receipt.get("archive"), "archive"
            ).get("tree_sha256"),
            "map_path": inputs.map_path,
            "world_revision": DEFAULT_WORLD_REVISION,
            "host": "127.0.0.1",
            "port": inputs.port,
        },
        "launch": {
            "target": str(inputs.executable),
            "target_sha256": inputs.executable_sha256,
            "package_launcher": str(inputs.launcher),
            "package_launcher_sha256": inputs.launcher_sha256,
            "package_launcher_executed": False,
            "command": command,
            "cwd": str(inputs.archive_root),
            "target_policy": "direct-sealed-executable-no-shell/v1",
            "environment_policy": "minimal-nullrhi-no-display-no-gpu-no-secrets/v1",
            "process": process_record,
        },
        "readiness": readiness,
        "archive_verification": {
            "before_launch": archive_before,
            "after_termination": archive_after,
            "stable": archive_before is not None and archive_after == archive_before,
        },
        "termination": termination,
        "log": {
            "path": str(log_path),
            "sha256": sha256_file(log_path),
            "bytes": log_path.stat().st_size,
        },
        "error": None if error is None else _error_record(error),
        "output": str(receipt_path),
    }
    receipt_sha = _write_receipt(receipt_path, receipt)
    return receipt, receipt_sha


def plan(inputs: SmokeInputs) -> dict[str, Any]:
    return {
        "schema": SMOKE_RECEIPT_SCHEMA,
        "mode": "preflight",
        "package_receipt": str(inputs.package_receipt),
        "package_receipt_sha256": inputs.package_receipt_sha256,
        "target": str(inputs.executable),
        "target_sha256": inputs.executable_sha256,
        "package_launcher": str(inputs.launcher),
        "package_launcher_sha256": inputs.launcher_sha256,
        "package_launcher_executed": False,
        "target_policy": "direct-sealed-executable-no-shell/v1",
        "map_path": inputs.map_path,
        "host": "127.0.0.1",
        "port": inputs.port,
        "output_dir": str(inputs.output_dir),
        "command": build_command(inputs),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-attempt", required=True, type=Path)
    parser.add_argument("--package-receipt-sha256", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--vista-world-port", required=True, type=int)
    parser.add_argument(
        "--timeout-seconds", type=float, default=DEFAULT_READY_TIMEOUT_SECONDS
    )
    parser.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    inputs = validate_inputs(args)
    if not args.apply:
        print(json.dumps(plan(inputs), indent=2, sort_keys=True))
        return 0
    receipt, receipt_sha = run_smoke(inputs)
    print(
        json.dumps(
            {
                "status": receipt["status"],
                "receipt": receipt["output"],
                "receipt_sha256": receipt_sha,
            },
            sort_keys=True,
        )
    )
    return 0 if receipt["status"] == "accepted" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (PackagedSmokeError, FileExistsError, OSError) as error:
        print(f"packaged smoke refused: {error}", file=sys.stderr)
        raise SystemExit(2)
