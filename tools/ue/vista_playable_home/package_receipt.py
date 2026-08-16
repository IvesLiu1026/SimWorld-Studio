#!/usr/bin/env python3
"""Verify one fixed Linux Development package and seal an immutable receipt.

This verifier is intentionally narrow.  It consumes one append-only
``package-linux-development/attempt-<id>`` layout, binds it to the accepted UE
scene-build result, and invokes only fixed local inspection tools.  It does
not build, launch, extract, upload, or modify the package.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


RECEIPT_SCHEMA = "simworld.vista.playable-home-linux-package-receipt/v1"
R2_RECEIPT_SCHEMA = "simworld.vista.playable-home-linux-package-receipt/v2"
R2_EXACT_MODE_RECEIPT_SCHEMA = "simworld.vista.playable-home-linux-package-receipt/v3"
ARCHIVE_ALGORITHM_V1 = "framed-canonical-file-record-sha256/v1"
ARCHIVE_SCHEMA_EXACT_MODE_V2 = (
    "simworld.vista.playable-home-package-archive-observation/v2"
)
ARCHIVE_ALGORITHM_EXACT_MODE_V2 = "framed-canonical-file-record-exact-mode-sha256/v2"
SOURCE_BUILD_SCHEMA = "simworld.vista.playable-home-ue-build-result/v1"
SOURCE_ACCEPTANCE_SCHEMA = "simworld.vista.playable-home-runtime-acceptance/v1"
R2_SOURCE_ACCEPTANCE_SCHEMA = "simworld.vista.playable-home-runtime-acceptance/v2"
R2_RUNTIME_PROFILE = "realistic_interior_r2"
R2_CAMERA_PROFILE = "realistic_interior_r2"
R2_DISPLAY = ":119"
R2_GPU = 0
R2_VISTA_WORLD_PORT = 55630
R2_WIDTH = 1920
R2_HEIGHT = 1080
R2_FPS = 60
R2_PRESENTATION_BUNDLE_COUNT = 3
R2_PRESENTATION_COLLISION_POLICY = "presentation_no_collision_use_hidden_r1_proxies"
EXPECTED_MAP_PATH = (
    "/Game/VISTA/PlayableHome/vista_playable_home_r1/Maps/VistaPlayableHome"
)
EXPECTED_REVISION = "vista_playable_home_r1"
EXPECTED_ATTEMPT_PARENT = "package-linux-development"
PACKAGE_ATTEMPT_RE = re.compile(r"^attempt-[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
LAUNCHER_RELATIVE = Path("archive/Linux/VistaPlayableHome.sh")
EXECUTABLE_RELATIVE = Path(
    "archive/Linux/VistaPlayableHome/Binaries/Linux/VistaPlayableHome"
)
PAK_DIRECTORY_RELATIVE = Path("archive/Linux/VistaPlayableHome/Content/Paks")
UAT_LOG_RELATIVE = Path("runuat.log")
OUTPUT_RELATIVE = Path("package-receipt.json")
PROJECT_RELATIVE = Path("project/VistaPlayableHome.uproject")
PROJECT_CONFIG_RELATIVE = Path("project/Config/DefaultEngine.ini")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
MAP_RE = re.compile(r"^/Game/[A-Za-z0-9_./-]+$")
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_UAT_LOG_BYTES = 512 * 1024 * 1024
MAX_TOOL_OUTPUT_BYTES = 128 * 1024 * 1024
TOOL_TIMEOUT_SECONDS = 180.0
TRUSTED_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
TRUSTED_TOOLS = {
    "file": Path("/usr/bin/file"),
    "readelf": Path("/usr/bin/readelf"),
    "ldd": Path("/usr/bin/ldd"),
}

UAT_SUCCESS_PHASES = (
    "********** BUILD COMMAND COMPLETED **********",
    "********** COOK COMMAND COMPLETED **********",
    "********** STAGE COMMAND COMPLETED **********",
    "********** PACKAGE COMMAND COMPLETED **********",
    "********** ARCHIVE COMMAND COMPLETED **********",
    "AutomationTool exiting with ExitCode=0 (Success)",
)

# These expressions deliberately target credential *values* or the UE Android
# File Server token assignment that was previously copied into DefaultEngine.
# Ordinary names such as STUDIO_ACCESS_TOKEN are not treated as a secret.
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    ("ue_android_file_server_token", re.compile(rb"SecurityToken\s*=", re.I)),
    ("private_key", re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("anthropic_token", re.compile(rb"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai_token", re.compile(rb"sk-(?:proj-)?[A-Za-z0-9_-]{32,}")),
    ("slack_token", re.compile(rb"xox[baprs]-[A-Za-z0-9-]{20,}")),
    ("github_token", re.compile(rb"gh[pousr]_[A-Za-z0-9]{30,}")),
    (
        "credentialed_uri",
        re.compile(
            rb"(?:postgres(?:ql)?|https?)://[^\x00\s/:]{1,80}:[^\x00\s/@]{8,80}@",
            re.I,
        ),
    ),
)

R2_ACCEPTANCE_BINDING_KEYS = frozenset(
    {
        "workspace",
        "runtime_state",
        "runtime_state_sha256",
        "build_result",
        "build_result_sha256",
        "repo_root",
        "source_commit",
        "source_clean",
        "host",
        "port",
        "world_revision",
        "map_path",
        "project",
        "runtime_profile",
        "camera_profile",
        "display",
        "gpu",
        "width",
        "height",
        "fps",
        "launch_plan",
        "launch_plan_sha256",
    }
)
R2_BUILD_DIGEST_FIELDS = (
    "visual_profile_sha256",
    "visual_profile_content_digest",
    "renderer_profile_request_sha256",
    "renderer_profile_request_content_digest",
    "presentation_import_receipt_sha256",
    "presentation_scene_receipt_sha256",
    "presentation_manifest_sha256",
    "presentation_artifact_receipt_sha256",
)
R2_PACKAGE_BINDING_FIELDS = (
    "runtime_profile",
    "camera_profile",
    "visual_profile_id",
    *R2_BUILD_DIGEST_FIELDS,
    "accepted_display",
    "accepted_gpu",
    "accepted_vista_world_port",
    "accepted_width",
    "accepted_height",
    "accepted_fps",
    "presentation_bundle_count",
    "presentation_collision_policy",
)


class PackageReceiptError(RuntimeError):
    """A bounded, non-secret package verification failure."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class PackageInputs:
    attempt_root: Path
    source_build_result: Path
    source_build_result_sha256: str
    source_acceptance: Path
    source_acceptance_sha256: str
    source_commit: str
    map_path: str
    unreal_pak: Path
    engine_root: Path
    uat_log: Path
    archive_root: Path
    launcher: Path
    executable: Path
    pak: Path
    output: Path
    source_result: Mapping[str, Any]
    source_acceptance_result: Mapping[str, Any]
    project_descriptor: Path
    project_config: Path
    runtime_profile: str | None
    camera_profile: str | None


@dataclass(frozen=True)
class ToolResult:
    name: str
    returncode: int
    stdout: bytes


ToolRunner = Callable[[str, Sequence[str], float], ToolResult]


@dataclass(frozen=True)
class FileSeal:
    sha256: str
    size: int
    mode: int
    identity: tuple[int, int, int, int, int, int]
    raw: bytes | None = None


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
        raise PackageReceiptError("JSON_INVALID", "receipt is not finite JSON") from exc


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
            raise PackageReceiptError(
                "FILE_SIZE_INVALID", f"{path.name} size is invalid"
            )
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if _file_identity(opened) != _file_identity(before):
                raise PackageReceiptError(
                    "FILE_CHANGED", f"{path.name} changed while opening"
                )
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
                if captured is not None:
                    captured.extend(block)
        after = os.lstat(path)
        if _file_identity(after) != _file_identity(before):
            raise PackageReceiptError(
                "FILE_CHANGED", f"{path.name} changed while hashing"
            )
    except PackageReceiptError:
        raise
    except OSError as exc:
        raise PackageReceiptError("READ_FAILED", f"could not hash {path.name}") from exc
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


def load_strict_json(path: Path, *, label: str) -> Mapping[str, Any]:
    try:
        size = path.stat().st_size
        if not 0 < size <= MAX_JSON_BYTES:
            raise PackageReceiptError("JSON_SIZE_INVALID", f"{label} size is invalid")
        raw = path.read_bytes()
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite constant: {value}")
            ),
        )
    except PackageReceiptError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise PackageReceiptError(
            "JSON_INVALID", f"{label} is not strict JSON"
        ) from exc
    if not isinstance(value, dict):
        raise PackageReceiptError("JSON_SHAPE_INVALID", f"{label} must be an object")
    return value


def _canonical_existing(path: Path, label: str, *, directory: bool = False) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute() or ".." in candidate.parts or candidate.is_symlink():
        raise PackageReceiptError(
            "PATH_IDENTITY_INVALID", f"{label} must be an absolute non-symlink path"
        )
    try:
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise PackageReceiptError("PATH_MISSING", f"{label} does not exist") from exc
    correct_kind = resolved.is_dir() if directory else resolved.is_file()
    if resolved != candidate or not correct_kind:
        raise PackageReceiptError(
            "PATH_IDENTITY_INVALID", f"{label} must name its real path identity"
        )
    return resolved


def _exact_child(
    root: Path, relative: Path, label: str, *, directory: bool = False
) -> Path:
    candidate = root / relative
    resolved = _canonical_existing(candidate, label, directory=directory)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PackageReceiptError(
            "PATH_ESCAPE_REFUSED", f"{label} escaped the attempt"
        ) from exc
    return resolved


def _validate_attempt_root(path: Path) -> Path:
    root = _canonical_existing(path, "package attempt", directory=True)
    if (
        root.parent.name != EXPECTED_ATTEMPT_PARENT
        or PACKAGE_ATTEMPT_RE.fullmatch(root.name) is None
    ):
        raise PackageReceiptError(
            "ATTEMPT_IDENTITY_INVALID",
            "package attempt must be a safe package-linux-development/attempt-<id>",
        )
    return root


def _validate_map(value: str) -> str:
    map_path = str(value or "").strip()
    if not MAP_RE.fullmatch(map_path) or ".." in map_path.split("/"):
        raise PackageReceiptError("MAP_INVALID", "map path is unsafe")
    if map_path != EXPECTED_MAP_PATH:
        raise PackageReceiptError(
            "MAP_MISMATCH", "map is not the fixed playable-home map"
        )
    return map_path


def _content_digest(value: Mapping[str, Any]) -> str:
    body = dict(value)
    body.pop("content_digest", None)
    return sha256_bytes(canonical_json(body))


def renderer_observation_package_projection(
    receipt: Mapping[str, Any],
    *,
    source_build_result: Path,
    source_build_result_sha256: str,
    source_commit: str,
    visual_profile_id: str,
    visual_profile_sha256: str,
    visual_profile_content_digest: str,
    renderer_profile_request_sha256: str,
    renderer_profile_request_content_digest: str,
) -> dict[str, Any]:
    """Validate and project the immutable r2 package identity.

    Package creation remains renderer-observation ``pending``.  This helper is
    consumed later by the live renderer acceptance lane and does not mutate or
    widen legacy v1 or v2 package receipt bytes.  Renderer acceptance requires
    the versioned r2 exact-mode receipt and never promotes a legacy package.
    """

    expected_top = {
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
    if (
        set(receipt) != expected_top
        or receipt.get("schema") != R2_EXACT_MODE_RECEIPT_SCHEMA
        or receipt.get("status") != "accepted"
    ):
        raise PackageReceiptError(
            "RENDERER_PACKAGE_INVALID",
            "renderer observation requires one exact-mode r2 package receipt",
        )
    bindings = receipt.get("bindings")
    expected_binding_keys = {
        "source_build_result",
        "source_build_result_sha256",
        "source_commit",
        "source_runtime_acceptance",
        "source_runtime_acceptance_sha256",
        "map_path",
        "world_revision",
        *R2_PACKAGE_BINDING_FIELDS,
    }
    if not isinstance(bindings, dict) or set(bindings) != expected_binding_keys:
        raise PackageReceiptError(
            "RENDERER_PACKAGE_INVALID", "r2 package renderer binding fields differ"
        )
    expected = {
        "source_build_result": str(source_build_result),
        "source_build_result_sha256": source_build_result_sha256,
        "source_commit": source_commit,
        "map_path": EXPECTED_MAP_PATH,
        "world_revision": EXPECTED_REVISION,
        "runtime_profile": R2_RUNTIME_PROFILE,
        "camera_profile": R2_CAMERA_PROFILE,
        "visual_profile_id": visual_profile_id,
        "visual_profile_sha256": visual_profile_sha256,
        "visual_profile_content_digest": visual_profile_content_digest,
        "renderer_profile_request_sha256": renderer_profile_request_sha256,
        "renderer_profile_request_content_digest": (
            renderer_profile_request_content_digest
        ),
    }
    if any(bindings.get(key) != value for key, value in expected.items()):
        raise PackageReceiptError(
            "RENDERER_PACKAGE_INVALID",
            "r2 package does not bind the observed renderer inputs",
        )
    for name in (
        "source_runtime_acceptance_sha256",
        *R2_BUILD_DIGEST_FIELDS,
    ):
        value = bindings.get(name)
        if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
            raise PackageReceiptError(
                "RENDERER_PACKAGE_INVALID", f"r2 package {name} digest is invalid"
            )
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != {
        "archive_root",
        "launcher",
        "executable",
        "pak",
    }:
        raise PackageReceiptError(
            "RENDERER_PACKAGE_INVALID", "r2 package artifact fields differ"
        )
    projected_artifacts: dict[str, Any] = {}
    for name in ("launcher", "executable", "pak"):
        record = artifacts.get(name)
        if (
            not isinstance(record, dict)
            or set(record) != {"relative_path", "sha256", "bytes", "executable", "mode"}
            or not isinstance(record.get("relative_path"), str)
            or not isinstance(record.get("sha256"), str)
            or SHA256_RE.fullmatch(record["sha256"]) is None
            or isinstance(record.get("bytes"), bool)
            or not isinstance(record.get("bytes"), int)
            or record["bytes"] <= 0
            or not isinstance(record.get("executable"), bool)
            or isinstance(record.get("mode"), bool)
            or not isinstance(record.get("mode"), int)
            or not 0 <= record["mode"] <= 0o7777
        ):
            raise PackageReceiptError(
                "RENDERER_PACKAGE_INVALID", f"r2 package {name} artifact is invalid"
            )
        projected_artifacts[name] = dict(record)
    project_policy = receipt.get("project_policy")
    if (
        not isinstance(project_policy, dict)
        or set(project_policy)
        != {
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
        or project_policy.get("mode_policy") != "sealed-exact-stat-imode/v1"
        or not isinstance(project_policy.get("project_descriptor"), str)
        or not isinstance(project_policy.get("project_config"), str)
        or any(
            not isinstance(project_policy.get(name), str)
            or SHA256_RE.fullmatch(project_policy[name]) is None
            for name in (
                "project_descriptor_sha256",
                "project_config_sha256",
            )
        )
        or any(
            isinstance(project_policy.get(name), bool)
            or not isinstance(project_policy.get(name), int)
            or not 0 <= project_policy[name] <= 0o7777
            for name in (
                "project_descriptor_mode",
                "project_config_mode",
            )
        )
    ):
        raise PackageReceiptError(
            "RENDERER_PACKAGE_INVALID",
            "r2 package project/plugin policy differs",
        )
    trusted = receipt.get("trusted_upstream")
    if (
        not isinstance(trusted, dict)
        or set(trusted)
        != {
            "policy",
            "engine_root",
            "unreal_pak",
            "unreal_pak_sha256",
            "mode_policy",
            "unreal_pak_mode",
        }
        or trusted.get("policy") != "engine-root-derived-from-pinned-unrealpak/v1"
        or trusted.get("mode_policy") != "sealed-exact-stat-imode/v1"
        or not isinstance(trusted.get("engine_root"), str)
        or not isinstance(trusted.get("unreal_pak"), str)
        or not isinstance(trusted.get("unreal_pak_sha256"), str)
        or SHA256_RE.fullmatch(trusted["unreal_pak_sha256"]) is None
        or isinstance(trusted.get("unreal_pak_mode"), bool)
        or not isinstance(trusted.get("unreal_pak_mode"), int)
        or not 0 <= trusted["unreal_pak_mode"] <= 0o7777
    ):
        raise PackageReceiptError(
            "RENDERER_PACKAGE_INVALID",
            "r2 package trusted UnrealPak mode binding differs",
        )
    archive = receipt.get("archive")
    if (
        not isinstance(archive, dict)
        or set(archive)
        != {
            "schema",
            "algorithm",
            "file_count",
            "total_bytes",
            "tree_sha256",
            "secret_scan",
        }
        or archive.get("schema") != ARCHIVE_SCHEMA_EXACT_MODE_V2
        or archive.get("algorithm") != ARCHIVE_ALGORITHM_EXACT_MODE_V2
        or not isinstance(archive.get("tree_sha256"), str)
        or SHA256_RE.fullmatch(archive["tree_sha256"]) is None
        or isinstance(archive.get("file_count"), bool)
        or not isinstance(archive.get("file_count"), int)
        or archive["file_count"] <= 0
        or isinstance(archive.get("total_bytes"), bool)
        or not isinstance(archive.get("total_bytes"), int)
        or archive["total_bytes"] <= 0
    ):
        raise PackageReceiptError(
            "RENDERER_PACKAGE_INVALID", "r2 package archive identity is invalid"
        )
    return {
        "schema": R2_EXACT_MODE_RECEIPT_SCHEMA,
        "attempt_root": receipt["attempt_root"],
        "archive_schema": archive["schema"],
        "archive_algorithm": archive["algorithm"],
        "archive_tree_sha256": archive["tree_sha256"],
        "archive_file_count": archive["file_count"],
        "archive_total_bytes": archive["total_bytes"],
        "artifacts": projected_artifacts,
        "project_policy": dict(project_policy),
    }


def _validate_r2_source_chain(
    *,
    source_result: Mapping[str, Any],
    acceptance: Mapping[str, Any],
    bindings: Mapping[str, Any],
    source: Path,
    source_pin: str,
    source_acceptance: Path,
    commit: str,
    map_path: str,
) -> None:
    if set(bindings) != R2_ACCEPTANCE_BINDING_KEYS:
        raise PackageReceiptError(
            "SOURCE_ACCEPTANCE_INVALID",
            "r2 source acceptance binding fields differ",
        )
    fixed_acceptance = {
        "build_result": str(source),
        "build_result_sha256": source_pin,
        "source_commit": commit,
        "source_clean": True,
        "host": "127.0.0.1",
        "port": R2_VISTA_WORLD_PORT,
        "world_revision": EXPECTED_REVISION,
        "map_path": map_path,
        "runtime_profile": R2_RUNTIME_PROFILE,
        "camera_profile": R2_CAMERA_PROFILE,
        "display": R2_DISPLAY,
        "gpu": R2_GPU,
        "width": R2_WIDTH,
        "height": R2_HEIGHT,
        "fps": R2_FPS,
    }
    if any(bindings.get(key) != value for key, value in fixed_acceptance.items()):
        raise PackageReceiptError(
            "SOURCE_ACCEPTANCE_INVALID",
            "r2 source acceptance profile/build binding differs",
        )
    for field in (
        "runtime_state_sha256",
        "launch_plan_sha256",
    ):
        value = bindings.get(field)
        if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
            raise PackageReceiptError(
                "SOURCE_ACCEPTANCE_INVALID",
                f"r2 source acceptance {field} is invalid",
            )
    if (
        acceptance.get("output") != str(source_acceptance)
        or acceptance.get("error") is not None
        or acceptance.get("initial_generation") != 0
        or not isinstance(acceptance.get("checks"), list)
        or not acceptance["checks"]
    ):
        raise PackageReceiptError(
            "SOURCE_ACCEPTANCE_INVALID",
            "r2 source acceptance did not retain a successful observed run",
        )
    if (
        source_result.get("content_digest") != _content_digest(source_result)
        or source_result.get("visual_profile_id") != R2_RUNTIME_PROFILE
        or source_result.get("renderer_runtime_observation") != "pending"
        or source_result.get("presentation_bundle_count")
        != R2_PRESENTATION_BUNDLE_COUNT
        or source_result.get("presentation_collision_policy")
        != R2_PRESENTATION_COLLISION_POLICY
        or source_result.get("presentation_ue_import_observation")
        != "verified_by_commandlet"
        or source_result.get("presentation_runtime_play_proof") != "pending"
    ):
        raise PackageReceiptError(
            "SOURCE_RESULT_INVALID",
            "r2 source build presentation identity differs",
        )
    for field in R2_BUILD_DIGEST_FIELDS:
        value = source_result.get(field)
        if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
            raise PackageReceiptError(
                "SOURCE_RESULT_INVALID",
                f"r2 source build {field} is invalid",
            )


def _only_pak(directory: Path) -> Path:
    entries: list[Path] = []
    try:
        for candidate in directory.iterdir():
            if candidate.suffix.lower() == ".pak":
                entries.append(_canonical_existing(candidate, "package pak"))
    except OSError as exc:
        raise PackageReceiptError(
            "PAK_ENUMERATION_FAILED", "could not enumerate Paks"
        ) from exc
    if len(entries) != 1:
        raise PackageReceiptError(
            "PAK_SET_INVALID", "package must contain exactly one .pak"
        )
    return entries[0]


def validate_inputs(args: argparse.Namespace) -> PackageInputs:
    root = _validate_attempt_root(Path(args.attempt_root))
    source = _canonical_existing(Path(args.source_build_result), "source build result")
    if source.name != "result-receipt.json":
        raise PackageReceiptError(
            "SOURCE_RESULT_INVALID", "source build result must be result-receipt.json"
        )
    pin = str(args.source_build_result_sha256 or "")
    if not SHA256_RE.fullmatch(pin) or not hmac.compare_digest(
        sha256_file(source), pin
    ):
        raise PackageReceiptError(
            "SOURCE_PIN_MISMATCH", "source build-result SHA differs"
        )
    commit = str(args.source_commit or "")
    if not COMMIT_RE.fullmatch(commit):
        raise PackageReceiptError(
            "SOURCE_COMMIT_INVALID", "source commit must be full lowercase SHA-1"
        )
    map_path = _validate_map(args.map_path)
    source_result = load_strict_json(source, label="source build result")
    if (
        source_result.get("schema_version") != SOURCE_BUILD_SCHEMA
        or source_result.get("status") != "accepted_candidate"
        or source_result.get("map_path") != map_path
        or source_result.get("revision") != EXPECTED_REVISION
    ):
        raise PackageReceiptError(
            "SOURCE_RESULT_INVALID",
            "source build result is not the accepted fixed scene",
        )
    if source_result.get("attempt_root") != str(source.parent):
        raise PackageReceiptError(
            "SOURCE_RESULT_INVALID", "source build-result attempt identity differs"
        )

    source_acceptance = _canonical_existing(
        Path(args.source_acceptance), "source runtime acceptance"
    )
    acceptance_pin = str(args.source_acceptance_sha256 or "")
    if not SHA256_RE.fullmatch(acceptance_pin) or not hmac.compare_digest(
        sha256_file(source_acceptance), acceptance_pin
    ):
        raise PackageReceiptError(
            "SOURCE_ACCEPTANCE_PIN_MISMATCH", "source acceptance SHA differs"
        )
    source_acceptance_result = load_strict_json(
        source_acceptance, label="source runtime acceptance"
    )
    acceptance_bindings = source_acceptance_result.get("bindings")
    acceptance_schema = source_acceptance_result.get("schema")
    if (
        acceptance_schema
        not in {
            SOURCE_ACCEPTANCE_SCHEMA,
            R2_SOURCE_ACCEPTANCE_SCHEMA,
        }
        or source_acceptance_result.get("status") != "accepted"
        or not isinstance(acceptance_bindings, dict)
        or acceptance_bindings.get("build_result") != str(source)
        or acceptance_bindings.get("build_result_sha256") != pin
        or acceptance_bindings.get("source_commit") != commit
        or acceptance_bindings.get("map_path") != map_path
    ):
        raise PackageReceiptError(
            "SOURCE_ACCEPTANCE_INVALID",
            "source acceptance does not bind the build, commit, and map",
        )
    runtime_profile: str | None = None
    camera_profile: str | None = None
    if acceptance_schema == R2_SOURCE_ACCEPTANCE_SCHEMA:
        _validate_r2_source_chain(
            source_result=source_result,
            acceptance=source_acceptance_result,
            bindings=acceptance_bindings,
            source=source,
            source_pin=pin,
            source_acceptance=source_acceptance,
            commit=commit,
            map_path=map_path,
        )
        runtime_profile = R2_RUNTIME_PROFILE
        camera_profile = R2_CAMERA_PROFILE

    unreal_pak = _canonical_existing(Path(args.unreal_pak), "UnrealPak")
    if len(unreal_pak.parents) < 4:
        raise PackageReceiptError(
            "UNREALPAK_INVALID",
            "UnrealPak must be the pinned Engine/Binaries/Linux/UnrealPak",
        )
    engine_root = unreal_pak.parents[3]
    expected_unreal_pak = engine_root / "Engine/Binaries/Linux/UnrealPak"
    if (
        unreal_pak.name != "UnrealPak"
        or unreal_pak != expected_unreal_pak
        or not engine_root.is_dir()
        or engine_root.is_symlink()
        or not os.access(unreal_pak, os.X_OK)
    ):
        raise PackageReceiptError(
            "UNREALPAK_INVALID",
            "UnrealPak must be the pinned Engine/Binaries/Linux/UnrealPak",
        )

    archive_root = _exact_child(
        root, Path("archive/Linux"), "archive root", directory=True
    )
    launcher = _exact_child(root, LAUNCHER_RELATIVE, "package launcher")
    executable = _exact_child(root, EXECUTABLE_RELATIVE, "package executable")
    if not os.access(launcher, os.X_OK) or not os.access(executable, os.X_OK):
        raise PackageReceiptError(
            "EXECUTABLE_INVALID", "launcher and game binary must be executable"
        )
    pak_directory = _exact_child(
        root, PAK_DIRECTORY_RELATIVE, "Paks directory", directory=True
    )
    pak = _only_pak(pak_directory)
    uat_log = _exact_child(root, UAT_LOG_RELATIVE, "RunUAT log")
    output = root / OUTPUT_RELATIVE
    if output.exists() or output.is_symlink():
        raise PackageReceiptError("OUTPUT_EXISTS", "package receipt already exists")

    project_descriptor = _exact_child(
        root, PROJECT_RELATIVE, "package project descriptor"
    )
    project_config = _exact_child(
        root, PROJECT_CONFIG_RELATIVE, "package project config"
    )

    return PackageInputs(
        attempt_root=root,
        source_build_result=source,
        source_build_result_sha256=pin,
        source_acceptance=source_acceptance,
        source_acceptance_sha256=acceptance_pin,
        source_commit=commit,
        map_path=map_path,
        unreal_pak=unreal_pak,
        engine_root=engine_root,
        uat_log=uat_log,
        archive_root=archive_root,
        launcher=launcher,
        executable=executable,
        pak=pak,
        output=output,
        source_result=source_result,
        source_acceptance_result=source_acceptance_result,
        project_descriptor=project_descriptor,
        project_config=project_config,
        runtime_profile=runtime_profile,
        camera_profile=camera_profile,
    )


def inspect_uat_log(path: Path) -> dict[str, Any]:
    try:
        size = path.stat().st_size
        if not 0 < size <= MAX_UAT_LOG_BYTES:
            raise PackageReceiptError(
                "UAT_LOG_SIZE_INVALID", "RunUAT log size is invalid"
            )
        raw = path.read_bytes()
    except PackageReceiptError:
        raise
    except OSError as exc:
        raise PackageReceiptError(
            "UAT_LOG_READ_FAILED", "could not read RunUAT log"
        ) from exc
    text = raw.decode("utf-8", errors="replace")
    command_requirements = (
        "BuildCookRun",
        "-platform=Linux",
        "-clientconfig=Development",
        f"-map={EXPECTED_MAP_PATH}",
        "-pak",
        "-skipiostore",
        "-archive",
    )
    if not all(requirement in text for requirement in command_requirements):
        raise PackageReceiptError(
            "UAT_CONFIGURATION_INVALID",
            "RunUAT is not the fixed Linux Development pak/archive build",
        )
    final_exit_codes = re.findall(
        r"AutomationTool exiting with ExitCode=([0-9]+)", text
    )
    if final_exit_codes != ["0"]:
        raise PackageReceiptError(
            "UAT_EXIT_INVALID", "RunUAT does not have one final successful exit"
        )
    offsets = [text.find(marker) for marker in UAT_SUCCESS_PHASES]
    if any(offset < 0 for offset in offsets) or offsets != sorted(offsets):
        raise PackageReceiptError(
            "UAT_PHASES_INCOMPLETE",
            "RunUAT did not complete every required phase in order",
        )
    return {
        "path": str(path),
        "sha256": sha256_bytes(raw),
        "bytes": len(raw),
        "success_phases": list(UAT_SUCCESS_PHASES),
        "configuration": {
            "platform": "Linux",
            "client": "Development",
            "map_path": EXPECTED_MAP_PATH,
            "pak": True,
            "io_store": False,
            "archive": True,
        },
    }


def _inspect_project_policy_with_seals(
    inputs: PackageInputs, *, exact_modes: bool = False
) -> tuple[dict[str, Any], dict[str, FileSeal]]:
    descriptor_seal = _sealed_file(
        inputs.project_descriptor,
        capture_bytes=True,
        maximum_bytes=MAX_JSON_BYTES,
    )
    if descriptor_seal.raw is None:  # pragma: no cover - capture_bytes guarantees it.
        raise PackageReceiptError(
            "PROJECT_POLICY_INVALID", "project descriptor bytes are unavailable"
        )
    try:
        descriptor = json.loads(
            descriptor_seal.raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite constant: {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise PackageReceiptError(
            "PROJECT_POLICY_INVALID", "project descriptor is not strict JSON"
        ) from exc
    if not isinstance(descriptor, dict):
        raise PackageReceiptError(
            "PROJECT_POLICY_INVALID", "project descriptor must be an object"
        )
    plugins = descriptor.get("Plugins")
    if not isinstance(plugins, list):
        raise PackageReceiptError(
            "PROJECT_POLICY_INVALID", "project Plugins must be a list"
        )
    observed: dict[str, bool] = {}
    for entry in plugins:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("Name"), str)
            or not isinstance(entry.get("Enabled"), bool)
            or entry["Name"] in observed
        ):
            raise PackageReceiptError(
                "PROJECT_POLICY_INVALID", "project plugin entry is invalid"
            )
        observed[entry["Name"]] = entry["Enabled"]
    required = {
        "VistaPlayableHome": True,
        "AndroidFileServer": False,
        "PythonScriptPlugin": False,
        "EditorScriptingUtilities": False,
        "Interchange": False,
    }
    if any(observed.get(name) is not enabled for name, enabled in required.items()) or {
        name for name, enabled in observed.items() if enabled
    } != {"VistaPlayableHome"}:
        raise PackageReceiptError(
            "PROJECT_POLICY_INVALID", "runtime and editor-only plugin policy differs"
        )
    modules = descriptor.get("Modules")
    expected_module = {
        "LoadingPhase": "Default",
        "Name": "VistaPlayableHomeHost",
        "Type": "Runtime",
    }
    if modules != [expected_module]:
        raise PackageReceiptError(
            "PROJECT_POLICY_INVALID", "host module policy differs"
        )

    config_seal = _sealed_file(
        inputs.project_config,
        capture_bytes=True,
        maximum_bytes=MAX_JSON_BYTES,
    )
    if config_seal.raw is None:  # pragma: no cover - capture_bytes guarantees it.
        raise PackageReceiptError(
            "PROJECT_POLICY_INVALID", "project config bytes are unavailable"
        )
    config = config_seal.raw
    for rule, pattern in SECRET_PATTERNS:
        if pattern.search(config):
            raise PackageReceiptError(
                "PROJECT_SECRET_REFUSED", f"project config matched secret rule {rule}"
            )
    try:
        config_text = config.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise PackageReceiptError(
            "PROJECT_POLICY_INVALID", "project config is not UTF-8"
        ) from exc
    required_config = (
        f"GameDefaultMap={EXPECTED_MAP_PATH}",
        "GlobalDefaultGameMode=/Script/VistaPlayableHome.VistaPlayableHomeGameMode",
        "[/Script/AndroidFileServerEditor.AndroidFileServerRuntimeSettings]",
        "bEnablePlugin=False",
        "bAllowNetworkConnection=False",
        "bCompileAFSProject=False",
    )
    if not all(value in config_text for value in required_config):
        raise PackageReceiptError(
            "PROJECT_POLICY_INVALID", "fixed project config differs"
        )
    policy = {
        "project_descriptor": str(inputs.project_descriptor),
        "project_descriptor_sha256": descriptor_seal.sha256,
        "project_config": str(inputs.project_config),
        "project_config_sha256": config_seal.sha256,
        "enabled_plugins": ["VistaPlayableHome"],
        "disabled_plugins": sorted(
            name for name, enabled in required.items() if not enabled
        ),
        "host_module": "VistaPlayableHomeHost",
        "android_file_server_enabled": False,
    }
    if exact_modes:
        policy.update(
            {
                "mode_policy": "sealed-exact-stat-imode/v1",
                "project_descriptor_mode": descriptor_seal.mode,
                "project_config_mode": config_seal.mode,
            }
        )
    return policy, {
        "project_descriptor": descriptor_seal,
        "project_config": config_seal,
    }


def inspect_project_policy(
    inputs: PackageInputs, *, exact_modes: bool = False
) -> dict[str, Any]:
    policy, _seals = _inspect_project_policy_with_seals(
        inputs,
        exact_modes=exact_modes,
    )
    return policy


def _safe_tool_environment() -> dict[str, str]:
    return {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": TRUSTED_PATH}


def run_fixed_tool(name: str, arguments: Sequence[str], timeout: float) -> ToolResult:
    if name not in {"file", "readelf", "ldd", "UnrealPak"}:
        raise PackageReceiptError("TOOL_REFUSED", "inspection tool is not allowlisted")
    executable: str | Path = (
        TRUSTED_TOOLS[name] if name != "UnrealPak" else arguments[0]
    )
    argv = list(arguments if name == "UnrealPak" else [str(executable), *arguments])
    if not Path(executable).is_file() or not os.access(executable, os.X_OK):
        raise PackageReceiptError(
            "TOOL_MISSING", f"required tool {name} is unavailable"
        )
    process: subprocess.Popen[bytes] | None = None
    selector: selectors.BaseSelector | None = None
    try:
        process = subprocess.Popen(
            argv,
            cwd="/",
            env=_safe_tool_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        if process.stdout is None:  # pragma: no cover - PIPE guarantees this.
            raise PackageReceiptError("TOOL_FAILED", f"{name} has no output pipe")
        os.set_blocking(process.stdout.fileno(), False)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        output = bytearray()
        pipe_open = True
        while pipe_open or process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PackageReceiptError(
                    "TOOL_TIMEOUT", f"{name} exceeded its timeout"
                )
            for key, _events in selector.select(min(remaining, 0.25)):
                try:
                    block = os.read(key.fd, 64 * 1024)
                except BlockingIOError:
                    continue
                if not block:
                    selector.unregister(process.stdout)
                    pipe_open = False
                    continue
                output.extend(block)
                if len(output) > MAX_TOOL_OUTPUT_BYTES:
                    raise PackageReceiptError(
                        "TOOL_OUTPUT_TOO_LARGE", f"{name} output exceeded its bound"
                    )
        returncode = process.wait(timeout=1.0)
    except PackageReceiptError:
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                pass
        raise
    except (OSError, subprocess.TimeoutExpired) as exc:
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                pass
        raise PackageReceiptError("TOOL_FAILED", f"{name} could not complete") from exc
    finally:
        if selector is not None:
            selector.close()
        if process is not None and process.stdout is not None:
            process.stdout.close()
    return ToolResult(name=name, returncode=returncode, stdout=bytes(output))


def _tool_observation(result: ToolResult) -> dict[str, Any]:
    return {
        "name": result.name,
        "returncode": result.returncode,
        "stdout_bytes": len(result.stdout),
        "stdout_sha256": sha256_bytes(result.stdout),
    }


def inspect_executable(
    executable: Path, runner: ToolRunner = run_fixed_tool
) -> dict[str, Any]:
    file_result = runner("file", [str(executable)], TOOL_TIMEOUT_SECONDS)
    file_text = file_result.stdout.decode("utf-8", errors="replace")
    if file_result.returncode != 0 or not all(
        token in file_text for token in ("ELF 64-bit", "x86-64")
    ):
        raise PackageReceiptError(
            "FILE_IDENTITY_INVALID", "game binary is not Linux x86-64 ELF"
        )

    readelf_result = runner("readelf", ["-h", str(executable)], TOOL_TIMEOUT_SECONDS)
    readelf_text = readelf_result.stdout.decode("utf-8", errors="replace")
    if (
        readelf_result.returncode != 0
        or re.search(r"Class:\s+ELF64", readelf_text) is None
        or re.search(r"Machine:\s+Advanced Micro Devices X86-64", readelf_text) is None
        or re.search(r"Type:\s+(?:DYN|EXEC)\b", readelf_text) is None
    ):
        raise PackageReceiptError(
            "READELF_IDENTITY_INVALID", "ELF header identity differs"
        )

    program_headers = runner("readelf", ["-l", str(executable)], TOOL_TIMEOUT_SECONDS)
    program_text = program_headers.stdout.decode("utf-8", errors="replace")
    interpreters = re.findall(
        r"Requesting program interpreter:\s*([^\]]+)\]", program_text
    )
    trusted_interpreters = {
        "/lib64/ld-linux-x86-64.so.2",
        "/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2",
    }
    if (
        program_headers.returncode != 0
        or len(interpreters) != 1
        or interpreters[0] not in trusted_interpreters
    ):
        raise PackageReceiptError(
            "ELF_INTERPRETER_INVALID", "ELF interpreter is not a trusted system loader"
        )

    ldd_result = runner("ldd", [str(executable)], TOOL_TIMEOUT_SECONDS)
    ldd_text = ldd_result.stdout.decode("utf-8", errors="replace")
    if ldd_result.returncode != 0 or "not found" in ldd_text.lower():
        raise PackageReceiptError(
            "LDD_DEPENDENCY_MISSING", "game binary has unresolved libraries"
        )

    return {
        "file": _tool_observation(file_result),
        "readelf": _tool_observation(readelf_result),
        "readelf_program_headers": _tool_observation(program_headers),
        "ldd": {
            **_tool_observation(ldd_result),
            "dependency_lines": len(
                [line for line in ldd_text.splitlines() if line.strip()]
            ),
            "missing": 0,
        },
    }


def _expected_map_entry(map_path: str) -> str:
    return "Content/" + map_path.removeprefix("/Game/") + ".umap"


def inspect_pak(
    inputs: PackageInputs, runner: ToolRunner = run_fixed_tool
) -> dict[str, Any]:
    result = runner(
        "UnrealPak",
        [str(inputs.unreal_pak), str(inputs.pak), "-List"],
        TOOL_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise PackageReceiptError("UNREALPAK_LIST_FAILED", "UnrealPak -List failed")
    text = result.stdout.decode("utf-8", errors="replace")
    expected = _expected_map_entry(inputs.map_path)
    quoted = [match.replace("\\", "/") for match in re.findall(r'"([^"\r\n]+)"', text)]
    matches = [entry for entry in quoted if entry.lstrip("./").endswith(expected)]
    if len(matches) != 1:
        raise PackageReceiptError(
            "PAK_MAP_MISSING",
            "pak does not contain exactly one fixed playable-home map",
        )
    return {
        **_tool_observation(result),
        "map_path": inputs.map_path,
        "map_entry": matches[0],
    }


def _archive_files(root: Path) -> list[Path]:
    output: list[Path] = []

    def refuse_walk_error(error: OSError) -> None:
        raise PackageReceiptError(
            "ARCHIVE_ENUMERATION_FAILED", "archive could not be enumerated completely"
        ) from error

    for current, directory_names, file_names in os.walk(
        root, followlinks=False, onerror=refuse_walk_error
    ):
        current_path = Path(current)
        for name in list(directory_names):
            candidate = current_path / name
            if candidate.is_symlink():
                raise PackageReceiptError(
                    "ARCHIVE_SYMLINK_REFUSED", "archive contains a symlink"
                )
        for name in file_names:
            candidate = current_path / name
            metadata = os.lstat(candidate)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise PackageReceiptError(
                    "ARCHIVE_ENTRY_REFUSED", "archive contains a non-regular file"
                )
            if candidate.suffix.lower() in {
                ".utoc",
                ".ucas",
                ".cpp",
                ".h",
                ".cs",
                ".py",
            }:
                raise PackageReceiptError(
                    "ARCHIVE_POLICY_INVALID",
                    "archive contains IoStore or source-code output",
                )
            if "UnrealEditor" in candidate.name:
                raise PackageReceiptError(
                    "ARCHIVE_POLICY_INVALID", "archive contains an editor executable"
                )
            output.append(candidate)
    return sorted(
        output, key=lambda path: path.relative_to(root).as_posix().encode("utf-8")
    )


def _trusted_engine_exemption(
    *,
    root: Path,
    path: Path,
    relative: str,
    archive_sha256: str,
    rules: set[str],
    trusted_engine_root: Path | None,
) -> dict[str, Any]:
    relative_path = Path(relative)
    if (
        trusted_engine_root is None
        or not relative_path.parts
        or relative_path.parts[0] != "Engine"
    ):
        raise PackageReceiptError(
            "SECRET_SCAN_FAILED",
            "package-specific archive content matched a secret rule",
        )
    engine_root = _canonical_existing(
        trusted_engine_root, "trusted engine root", directory=True
    )
    upstream = _canonical_existing(
        engine_root / relative_path, "trusted engine counterpart"
    )
    try:
        upstream.relative_to(engine_root)
    except ValueError as exc:  # pragma: no cover - canonical containment defense.
        raise PackageReceiptError(
            "TRUSTED_ENGINE_ESCAPE", "trusted engine counterpart escaped its root"
        ) from exc
    upstream_sha256 = sha256_file(upstream)
    if not hmac.compare_digest(archive_sha256, upstream_sha256):
        raise PackageReceiptError(
            "SECRET_SCAN_FAILED",
            "modified Engine archive content matched a secret rule",
        )
    return {
        "policy": "byte-identical-pinned-engine-counterpart/v1",
        "archive_relative_path": path.relative_to(root).as_posix(),
        "upstream_relative_path": upstream.relative_to(engine_root).as_posix(),
        "sha256": archive_sha256,
        "rules": sorted(rules),
    }


def inspect_archive(
    root: Path,
    *,
    trusted_engine_root: Path | None = None,
    exact_modes: bool = False,
    _identity_sink: dict[str, tuple[int, int, int, int, int, int]] | None = None,
) -> dict[str, Any]:
    files_before = _archive_files(root)
    tree = hashlib.sha256()
    file_count = 0
    total_bytes = 0
    exemptions: list[dict[str, Any]] = []
    pattern_hit_count = 0
    sealed_identities: dict[str, tuple[int, int, int, int, int, int]] = {}
    # Preserve enough preceding bytes for the largest bounded credential form,
    # including regex quantifiers whose source representation is shorter than
    # the byte string they match.
    maximum_pattern = 512
    for path in files_before:
        relative = path.relative_to(root).as_posix()
        before = os.lstat(path)
        digest = hashlib.sha256()
        overlap = b""
        matched_rules: set[str] = set()
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            with os.fdopen(descriptor, "rb") as handle:
                opened = os.fstat(handle.fileno())
                if _file_identity(opened) != _file_identity(before):
                    raise PackageReceiptError(
                        "ARCHIVE_CHANGED", "archive changed while opening"
                    )
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
                    window = overlap + block
                    for rule, pattern in SECRET_PATTERNS:
                        if pattern.search(window):
                            matched_rules.add(rule)
                    overlap = window[-maximum_pattern:]
        except PackageReceiptError:
            raise
        except OSError as exc:
            raise PackageReceiptError(
                "ARCHIVE_READ_FAILED", "could not read archive"
            ) from exc
        after = os.lstat(path)
        if _file_identity(before) != _file_identity(after):
            raise PackageReceiptError(
                "ARCHIVE_CHANGED", "archive changed while hashing"
            )
        sealed_identities[relative] = _file_identity(after)
        if matched_rules:
            archive_sha256 = digest.hexdigest()
            exemptions.append(
                _trusted_engine_exemption(
                    root=root,
                    path=path,
                    relative=relative,
                    archive_sha256=archive_sha256,
                    rules=matched_rules,
                    trusted_engine_root=trusted_engine_root,
                )
            )
            pattern_hit_count += len(matched_rules)
        record_value = {
            "path": relative,
            "sha256": digest.hexdigest(),
            "size": before.st_size,
        }
        if exact_modes:
            record_value["mode"] = stat.S_IMODE(before.st_mode)
        else:
            record_value["executable"] = bool(before.st_mode & 0o111)
        record = canonical_json(record_value)
        tree.update(len(record).to_bytes(8, "big"))
        tree.update(record)
        file_count += 1
        total_bytes += before.st_size
    files_after = _archive_files(root)
    if [path.relative_to(root) for path in files_after] != [
        path.relative_to(root) for path in files_before
    ]:
        raise PackageReceiptError(
            "ARCHIVE_CHANGED", "archive entry set changed while hashing"
        )
    for path in files_after:
        relative = path.relative_to(root).as_posix()
        metadata = os.lstat(path)
        if sealed_identities.get(relative) != _file_identity(metadata):
            raise PackageReceiptError(
                "ARCHIVE_CHANGED", "archive identity changed after hashing"
            )
    observation = {
        "algorithm": (
            ARCHIVE_ALGORITHM_EXACT_MODE_V2 if exact_modes else ARCHIVE_ALGORITHM_V1
        ),
        "file_count": file_count,
        "total_bytes": total_bytes,
        "tree_sha256": tree.hexdigest(),
        "secret_scan": {
            "policy": "high-confidence-credential-byte-patterns/v1",
            "patterns": [name for name, _pattern in SECRET_PATTERNS],
            "files_scanned": file_count,
            "bytes_scanned": total_bytes,
            "matches": 0,
            "pattern_hits": pattern_hit_count,
            "trusted_upstream_exemption_policy": (
                "archive-Engine-path-and-byte-identical-pinned-engine-counterpart/v1"
            ),
            "trusted_upstream_exemption_count": len(exemptions),
            "trusted_upstream_exemptions": exemptions,
        },
    }
    if exact_modes:
        observation["schema"] = ARCHIVE_SCHEMA_EXACT_MODE_V2
    if _identity_sink is not None:
        _identity_sink.update(sealed_identities)
    return observation


def _artifact(
    path: Path,
    root: Path,
    *,
    exact_mode: bool = False,
    seal: FileSeal | None = None,
) -> dict[str, Any]:
    observed = seal if seal is not None else _sealed_file(path)
    record = {
        "relative_path": path.relative_to(root).as_posix(),
        "sha256": observed.sha256,
        "bytes": observed.size,
        "executable": bool(observed.mode & 0o111),
    }
    if exact_mode:
        record["mode"] = observed.mode
    return record


def verify_package(
    inputs: PackageInputs, runner: ToolRunner = run_fixed_tool
) -> dict[str, Any]:
    exact_modes = inputs.runtime_profile == R2_RUNTIME_PROFILE
    project_policy, project_seals = _inspect_project_policy_with_seals(
        inputs,
        exact_modes=exact_modes,
    )
    uat = inspect_uat_log(inputs.uat_log)
    artifact_seals = {
        "launcher": _sealed_file(inputs.launcher),
        "executable": _sealed_file(inputs.executable),
        "pak": _sealed_file(inputs.pak),
    }
    executable_tools = inspect_executable(inputs.executable, runner)
    unreal_pak_before_tool = _sealed_file(inputs.unreal_pak)
    pak_tool = inspect_pak(inputs, runner)
    unreal_pak_after_tool = _sealed_file(inputs.unreal_pak)
    if unreal_pak_after_tool != unreal_pak_before_tool:
        raise PackageReceiptError(
            "UNREALPAK_CHANGED",
            "UnrealPak changed while executing the fixed inspection",
        )
    archive_identities: dict[str, tuple[int, int, int, int, int, int]] = {}
    archive = inspect_archive(
        inputs.archive_root,
        trusted_engine_root=inputs.engine_root,
        exact_modes=exact_modes,
        _identity_sink=archive_identities,
    )
    bindings: dict[str, Any] = {
        "source_build_result": str(inputs.source_build_result),
        "source_build_result_sha256": inputs.source_build_result_sha256,
        "source_commit": inputs.source_commit,
        "source_runtime_acceptance": str(inputs.source_acceptance),
        "source_runtime_acceptance_sha256": inputs.source_acceptance_sha256,
        "map_path": inputs.map_path,
        "world_revision": EXPECTED_REVISION,
    }
    if inputs.runtime_profile == R2_RUNTIME_PROFILE:
        source = inputs.source_result
        acceptance_bindings = inputs.source_acceptance_result["bindings"]
        bindings.update(
            {
                "runtime_profile": R2_RUNTIME_PROFILE,
                "camera_profile": R2_CAMERA_PROFILE,
                "visual_profile_id": source["visual_profile_id"],
                **{field: source[field] for field in R2_BUILD_DIGEST_FIELDS},
                "accepted_display": acceptance_bindings["display"],
                "accepted_gpu": acceptance_bindings["gpu"],
                "accepted_vista_world_port": acceptance_bindings["port"],
                "accepted_width": acceptance_bindings["width"],
                "accepted_height": acceptance_bindings["height"],
                "accepted_fps": acceptance_bindings["fps"],
                "presentation_bundle_count": source["presentation_bundle_count"],
                "presentation_collision_policy": source[
                    "presentation_collision_policy"
                ],
            }
        )
    receipt = {
        "schema": (R2_EXACT_MODE_RECEIPT_SCHEMA if exact_modes else RECEIPT_SCHEMA),
        "status": "accepted",
        "created_at": utc_now(),
        "attempt_root": str(inputs.attempt_root),
        "bindings": bindings,
        "artifacts": {
            "archive_root": str(inputs.archive_root),
            "launcher": _artifact(
                inputs.launcher,
                inputs.attempt_root,
                exact_mode=exact_modes,
                seal=artifact_seals["launcher"],
            ),
            "executable": _artifact(
                inputs.executable,
                inputs.attempt_root,
                exact_mode=exact_modes,
                seal=artifact_seals["executable"],
            ),
            "pak": _artifact(
                inputs.pak,
                inputs.attempt_root,
                exact_mode=exact_modes,
                seal=artifact_seals["pak"],
            ),
        },
        "uat": uat,
        "project_policy": project_policy,
        "tools": {**executable_tools, "unreal_pak": pak_tool},
        "trusted_upstream": {
            "policy": "engine-root-derived-from-pinned-unrealpak/v1",
            "engine_root": str(inputs.engine_root),
            "unreal_pak": str(inputs.unreal_pak),
            "unreal_pak_sha256": unreal_pak_after_tool.sha256,
            **(
                {
                    "mode_policy": "sealed-exact-stat-imode/v1",
                    "unreal_pak_mode": unreal_pak_after_tool.mode,
                }
                if exact_modes
                else {}
            ),
        },
        "archive": archive,
        "output": str(inputs.output),
    }

    # Close the complete package identity immediately before returning the
    # receipt.  Each individual sealed read rejects in-window replacement;
    # comparing the complete second snapshot rejects phase exchanges between
    # policy/tool/archive inspection and receipt construction.
    final_project_policy, final_project_seals = _inspect_project_policy_with_seals(
        inputs,
        exact_modes=exact_modes,
    )
    final_artifact_seals = {
        "launcher": _sealed_file(inputs.launcher),
        "executable": _sealed_file(inputs.executable),
        "pak": _sealed_file(inputs.pak),
    }
    final_unreal_pak = _sealed_file(inputs.unreal_pak)
    final_archive_identities: dict[str, tuple[int, int, int, int, int, int]] = {}
    final_archive = inspect_archive(
        inputs.archive_root,
        trusted_engine_root=inputs.engine_root,
        exact_modes=exact_modes,
        _identity_sink=final_archive_identities,
    )
    if (
        final_project_policy != project_policy
        or final_project_seals != project_seals
        or final_artifact_seals != artifact_seals
        or final_unreal_pak != unreal_pak_after_tool
        or final_archive != archive
        or final_archive_identities != archive_identities
    ):
        raise PackageReceiptError(
            "PACKAGE_CHANGED",
            "package identity changed between inspection phases",
        )
    return receipt


def write_exclusive_receipt(path: Path, receipt: Mapping[str, Any]) -> str:
    raw = canonical_json(receipt)
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
        # Creation modes are filtered through the caller's umask.  The exact-mode
        # receipt contract requires 0600 even under a maximally restrictive
        # umask, so set the final mode on the already-open, O_EXCL descriptor.
        os.fchmod(descriptor, 0o600)
        handle = os.fdopen(descriptor, "wb")
        descriptor = -1
        with handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                # fdopen may have closed the descriptor before raising.  Never
                # let a best-effort EBADF mask the original failure.
                pass
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            # Preserve the original commit failure.  A later exclusive write
            # will still fail closed if cleanup was not possible.
            pass
        raise
    return sha256_bytes(raw)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempt-root", required=True, type=Path)
    parser.add_argument("--source-build-result", required=True, type=Path)
    parser.add_argument("--source-build-result-sha256", required=True)
    parser.add_argument("--source-acceptance", required=True, type=Path)
    parser.add_argument("--source-acceptance-sha256", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--map-path", required=True)
    parser.add_argument("--unreal-pak", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    inputs = validate_inputs(build_parser().parse_args(argv))
    receipt = verify_package(inputs)
    receipt_sha256 = write_exclusive_receipt(inputs.output, receipt)
    print(
        json.dumps(
            {
                "status": "accepted",
                "receipt": str(inputs.output),
                "receipt_sha256": receipt_sha256,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (PackageReceiptError, FileExistsError, OSError) as error:
        print(f"package verification refused: {error}", file=sys.stderr)
        raise SystemExit(2)
