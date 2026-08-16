#!/usr/bin/env python3
"""Seal one immutable, live UE renderer observation for realistic r2.

The staged renderer request is configuration, not proof.  This command sends
one read-only ``renderer_status`` request to the current loopback runtime,
validates the effective values against the byte-pinned VisualProfile contract,
and writes the only receipt allowed to call the renderer observed/accepted.
It never launches, stops, or mutates Unreal.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import re
import socket
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from tools.runtime.vista_playable_home import acceptance, runtime  # type: ignore
    from tools.runtime.vista_playable_home import packaged_entrypoint  # type: ignore
    from tools.runtime.vista_playable_home import packaged_profile  # type: ignore
    from tools.runtime.vista_playable_home import packaged_smoke  # type: ignore
    from tools.ue.vista_playable_home import build_home  # type: ignore
    from tools.ue.vista_playable_home import package_receipt  # type: ignore
else:
    from . import (
        acceptance,
        packaged_entrypoint,
        packaged_profile,
        packaged_smoke,
        runtime,
    )
    from tools.ue.vista_playable_home import build_home, package_receipt


RECEIPT_SCHEMA = "simworld.vista.playable-home-renderer-runtime-acceptance/v1"
EXPECTED_PROFILE = acceptance.R2_RUNTIME_PROFILE
EXPECTED_MAP = package_receipt.EXPECTED_MAP_PATH
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
COMMAND_ID_RE = re.compile(r"^vwc-[0-9a-f]{24}$")
OUTPUT_RE = re.compile(r"^renderer-acceptance(?:-[A-Za-z0-9._-]{1,80})?\.json$")
RENDERER_REQUEST_KEYS = {
    "schema_version",
    "status",
    "runtime_proof",
    "visual_profile_id",
    "visual_profile_content_digest",
    "renderer_profile",
    "renderer_profile_digest",
    "engine_config_sha256",
    "observation_contract",
    "content_digest",
}


class RendererAcceptanceError(RuntimeError):
    """A stable fail-closed renderer observation error."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class RendererAcceptanceConfig:
    workspace: Path
    repo_root: Path
    package_receipt: Path
    output: Path
    runtime_state_sha256: str
    build_result_sha256: str
    package_receipt_sha256: str
    source_commit: str
    socket_timeout_s: float = 2.0


@dataclass(frozen=True)
class PackagedRuntimeBinding:
    package_attempt: Path
    state_path: Path
    state_sha256: str
    launch_plan_path: Path
    launch_plan_sha256: str
    port: int


@dataclass(frozen=True)
class RendererInputs:
    config: RendererAcceptanceConfig
    runtime_binding: PackagedRuntimeBinding
    runtime_state: Mapping[str, Any]
    build_result: Mapping[str, Any]
    visual_profile_path: Path
    visual_profile_sha256: str
    visual_profile: Mapping[str, Any]
    renderer_request_path: Path
    renderer_request_sha256: str
    renderer_request: Mapping[str, Any]
    renderer_compilation: build_home.RendererProfileCompilation
    execution_path: Path
    execution_sha256: str
    preparation_path: Path
    preparation_sha256: str
    project_path: Path
    project_sha256: str
    engine_config_path: Path
    engine_config_sha256: str
    plugin_path: Path
    plugin_snapshot: build_home.TreeSnapshot
    package_path: Path
    package_sha256: str
    package: Mapping[str, Any]
    package_projection: Mapping[str, Any]
    package_project_path: Path
    package_project_sha256: str
    package_config_path: Path
    package_config_sha256: str


Exchange = Callable[[Mapping[str, Any], float, int], tuple[bytes, Any]]
ListenerProver = Callable[[int, int], Mapping[str, Any]]


def _fail(code: str, message: str) -> None:
    raise RendererAcceptanceError(code, message)


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
        raise RendererAcceptanceError(
            "JSON_INVALID", "renderer evidence is not finite JSON"
        ) from exc


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        before = os.lstat(path)
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if (opened.st_dev, opened.st_ino, opened.st_size) != (
                before.st_dev,
                before.st_ino,
                before.st_size,
            ):
                _fail("EVIDENCE_CHANGED", f"{path.name} changed while opening")
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        after = os.lstat(path)
    except RendererAcceptanceError:
        raise
    except OSError as exc:
        raise RendererAcceptanceError(
            "EVIDENCE_READ_FAILED", f"could not hash {path.name}"
        ) from exc
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        _fail("EVIDENCE_CHANGED", f"{path.name} changed while hashing")
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


def strict_json_bytes(raw: bytes, *, label: str) -> Any:
    if not raw:
        _fail("JSON_EMPTY", f"{label} is empty")
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        _reject_nonfinite(value)
        return value
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RendererAcceptanceError(
            "JSON_INVALID", f"{label} is not one strict JSON value"
        ) from exc


def _canonical_existing(path: Path, label: str, *, directory: bool = False) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute() or ".." in candidate.parts or candidate.is_symlink():
        _fail(
            "PATH_IDENTITY_INVALID",
            f"{label} must be an absolute non-symlink path",
        )
    try:
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise RendererAcceptanceError(
            "PATH_MISSING", f"{label} does not exist"
        ) from exc
    correct_kind = resolved.is_dir() if directory else resolved.is_file()
    if resolved != candidate or not correct_kind:
        _fail("PATH_IDENTITY_INVALID", f"{label} path identity differs")
    return resolved


def _contained(path: Path, root: Path, label: str) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise RendererAcceptanceError(
            "PATH_ESCAPE_REFUSED", f"{label} escaped the build attempt"
        ) from exc


def _load_json_file(
    path: Path,
    *,
    label: str,
    expected_sha256: str | None = None,
    require_canonical: bool = False,
) -> tuple[Mapping[str, Any], bytes]:
    actual = sha256_file(path)
    if expected_sha256 is not None and (
        SHA256_RE.fullmatch(expected_sha256) is None
        or not hmac.compare_digest(actual, expected_sha256)
    ):
        _fail("EVIDENCE_PIN_MISMATCH", f"{label} SHA-256 differs")
    try:
        size = path.stat().st_size
        if not 0 < size <= MAX_JSON_BYTES:
            _fail("EVIDENCE_SIZE_INVALID", f"{label} size is invalid")
        raw = path.read_bytes()
    except RendererAcceptanceError:
        raise
    except OSError as exc:
        raise RendererAcceptanceError(
            "EVIDENCE_READ_FAILED", f"could not read {label}"
        ) from exc
    value = strict_json_bytes(raw, label=label)
    if not isinstance(value, dict):
        _fail("EVIDENCE_SHAPE_INVALID", f"{label} must be an object")
    if require_canonical and canonical_json(value) != raw:
        _fail("EVIDENCE_CANONICAL_INVALID", f"{label} is not canonical JSON")
    return value, raw


def _content_digest(value: Mapping[str, Any]) -> str:
    body = dict(value)
    body.pop("content_digest", None)
    return sha256_bytes(canonical_json(body))


def _validate_sha(value: str, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        _fail("DIGEST_INVALID", f"{label} must be a lowercase SHA-256")
    return value


def _validate_visual_and_renderer(
    workspace: Path,
    build_result: Mapping[str, Any],
) -> tuple[
    Path,
    str,
    Mapping[str, Any],
    Path,
    str,
    Mapping[str, Any],
    build_home.RendererProfileCompilation,
]:
    contracts = _canonical_existing(
        workspace / "contracts", "contracts", directory=True
    )
    profile_path = _canonical_existing(
        contracts / build_home.VISUAL_PROFILE_ATTEMPT_FILE, "visual profile"
    )
    request_path = _canonical_existing(
        contracts / build_home.RENDERER_REQUEST_ATTEMPT_FILE,
        "renderer request",
    )
    profile_sha = _validate_sha(
        str(build_result.get("visual_profile_sha256", "")),
        "visual-profile SHA-256",
    )
    request_sha = _validate_sha(
        str(build_result.get("renderer_profile_request_sha256", "")),
        "renderer-request SHA-256",
    )
    profile, _profile_raw = _load_json_file(
        profile_path,
        label="visual profile",
        expected_sha256=profile_sha,
    )
    request, _request_raw = _load_json_file(
        request_path,
        label="renderer request",
        expected_sha256=request_sha,
        require_canonical=True,
    )
    if (
        profile.get("schema_version")
        != "simworld.vista.playable-home-visual-profile/v1"
        or profile.get("visual_profile_id") != EXPECTED_PROFILE
        or profile.get("house_revision") != acceptance.DEFAULT_WORLD_REVISION
        or profile.get("content_digest")
        != build_home.visual_profile_contract.content_digest(profile)
        or profile.get("content_digest")
        != build_result.get("visual_profile_content_digest")
        or not isinstance(profile.get("renderer_profile"), dict)
    ):
        _fail(
            "VISUAL_PROFILE_INVALID",
            "visual profile identity, revision, or content digest differs",
        )
    try:
        compilation = build_home.compile_renderer_profile(profile["renderer_profile"])
    except build_home.BuildHomeError as exc:
        raise RendererAcceptanceError(
            "RENDERER_PROFILE_INVALID", "renderer profile compilation failed"
        ) from exc
    if (
        set(request) != RENDERER_REQUEST_KEYS
        or request.get("schema_version") != build_home.RENDERER_REQUEST_SCHEMA
        or request.get("status") != "staged_runtime_observation_required"
        or request.get("runtime_proof") is not False
        or request.get("visual_profile_id") != EXPECTED_PROFILE
        or request.get("visual_profile_content_digest") != profile.get("content_digest")
        or request.get("renderer_profile") != compilation.profile
        or request.get("renderer_profile_digest") != compilation.content_digest
        or request.get("observation_contract") != compilation.observation_contract
        or request.get("content_digest") != _content_digest(request)
        or request.get("content_digest")
        != build_result.get("renderer_profile_request_content_digest")
    ):
        _fail(
            "RENDERER_REQUEST_INVALID",
            "renderer request does not exactly bind the profile and observation contract",
        )
    return (
        profile_path,
        profile_sha,
        profile,
        request_path,
        request_sha,
        request,
        compilation,
    )


def _resolve_packaged_runtime_state(package_attempt: Path) -> Path:
    runtime_root = _canonical_existing(
        package_attempt / "game-runtime", "package runtime root", directory=True
    )
    pointer_path = _canonical_existing(
        runtime_root / "current.json", "package runtime pointer"
    )
    pointer, _pointer_raw = _load_json_file(
        pointer_path, label="package runtime pointer"
    )
    if (
        set(pointer) != {"schema", "state"}
        or pointer.get("schema") != runtime.RUNTIME_POINTER_SCHEMA
        or not isinstance(pointer.get("state"), str)
    ):
        _fail("RUNTIME_POINTER_INVALID", "package runtime pointer fields differ")
    relative = Path(pointer["state"])
    if (
        relative.is_absolute()
        or len(relative.parts) != 2
        or runtime.ATTEMPT_RE.fullmatch(relative.parts[0]) is None
        or relative.parts[1] != "runtime-state.json"
    ):
        _fail("RUNTIME_POINTER_INVALID", "package runtime pointer target is invalid")
    state_path = _canonical_existing(runtime_root / relative, "package runtime state")
    _contained(state_path, package_attempt, "package runtime state")
    return state_path


def _validate_packaged_runtime(
    *,
    package_attempt: Path,
    package_path: Path,
    package_sha: str,
    package: Mapping[str, Any],
    package_projection: Mapping[str, Any],
    expected_state_sha: str,
) -> tuple[
    PackagedRuntimeBinding,
    Mapping[str, Any],
    packaged_profile.PackagedProfileInputs,
]:
    state_path = _resolve_packaged_runtime_state(package_attempt)
    state_sha = _validate_sha(expected_state_sha, "runtime-state SHA-256")
    state, _state_raw = _load_json_file(
        state_path,
        label="package runtime state",
        expected_sha256=state_sha,
    )
    expected_state_keys = {
        "schema",
        "status",
        "created_at",
        "updated_at",
        "mode",
        "map",
        "world_revision",
        "display",
        "gpu",
        "vista_world_port",
        "profile",
        "profile_sha256",
        "package_receipt",
        "package_receipt_sha256",
        "archive_tree_sha256",
        "executable",
        "executable_sha256",
        "trusted_engine_root",
        "unreal_pak",
        "unreal_pak_sha256",
        "nvidia_icd",
        "nvidia_icd_sha256",
        "process",
        "supervisor",
        "runtime_profile",
        "camera_profile",
        "width",
        "height",
        "fps",
        "launch_plan_sha256",
        "readiness",
        "archive_reverified_after_readiness",
    }
    if set(state) != expected_state_keys:
        _fail("RUNTIME_STATE_INVALID", "packaged runtime-state fields differ")
    profile_path = _canonical_existing(
        Path(str(state.get("profile", ""))), "packaged profile"
    )
    _contained(profile_path, package_attempt, "packaged profile")
    profile_sha = _validate_sha(
        str(state.get("profile_sha256", "")), "packaged profile SHA-256"
    )
    try:
        profile_inputs = packaged_profile.load_profile(
            profile_path, profile_sha, verify_archive=False
        )
    except packaged_profile.PackagedProfileError as exc:
        raise RendererAcceptanceError(
            "PACKAGED_PROFILE_INVALID", "running packaged profile is invalid"
        ) from exc
    package_binding = profile_inputs.package
    artifacts = package_projection["artifacts"]
    trusted = package.get("trusted_upstream")
    if not isinstance(trusted, dict):
        _fail("PACKAGE_IDENTITY_INVALID", "trusted package identity is missing")
    fixed = {
        "schema": packaged_entrypoint.R2_STATE_SCHEMA,
        "status": "running",
        "mode": packaged_profile.R2_PROFILE_MODE,
        "map": EXPECTED_MAP,
        "world_revision": acceptance.DEFAULT_WORLD_REVISION,
        "display": runtime.R2_DISPLAY,
        "gpu": runtime.R2_GPU,
        "vista_world_port": runtime.R2_VISTA_WORLD_PORT,
        "package_receipt": str(package_path),
        "package_receipt_sha256": package_sha,
        "archive_tree_sha256": package_projection["archive_tree_sha256"],
        "executable": str(package_binding.executable),
        "executable_sha256": artifacts["executable"]["sha256"],
        "trusted_engine_root": trusted.get("engine_root"),
        "unreal_pak": trusted.get("unreal_pak"),
        "unreal_pak_sha256": trusted.get("unreal_pak_sha256"),
        "nvidia_icd": str(profile_inputs.nvidia_icd),
        "nvidia_icd_sha256": profile_inputs.nvidia_icd_sha256,
        "runtime_profile": runtime.R2_RUNTIME_PROFILE,
        "camera_profile": runtime.R2_CAMERA_PROFILE,
        "width": runtime.R2_WIDTH,
        "height": runtime.R2_HEIGHT,
        "fps": runtime.R2_FPS,
        "archive_reverified_after_readiness": True,
    }
    if any(state.get(key) != value for key, value in fixed.items()):
        _fail(
            "RUNTIME_STATE_INVALID",
            "running packaged renderer identity differs from its sealed package",
        )
    if not isinstance(state.get("created_at"), str) or not isinstance(
        state.get("updated_at"), str
    ):
        _fail("RUNTIME_STATE_INVALID", "packaged runtime timestamps are invalid")
    try:
        acceptance._validate_identity(state["process"], "packaged-game")
        acceptance._validate_identity(
            state["supervisor"], "vista-world-packaged-supervisor"
        )
    except acceptance.AcceptanceError as exc:
        raise RendererAcceptanceError(exc.code, str(exc)) from exc
    readiness = state.get("readiness")
    if not isinstance(readiness, dict) or set(readiness) != {
        "typed",
        "listener_ownership",
    }:
        _fail("RUNTIME_STATE_INVALID", "packaged readiness fields differ")
    typed = readiness.get("typed")
    if (
        not isinstance(typed, dict)
        or set(typed) != acceptance.STATUS_KEYS
        or typed.get("status") != "success"
        or typed.get("code") != "READY"
        or typed.get("world_revision") != acceptance.DEFAULT_WORLD_REVISION
        or not isinstance(typed.get("session_generation"), int)
        or isinstance(typed.get("session_generation"), bool)
        or typed["session_generation"] < 0
        or typed.get("event_status")
        not in {
            "inactive",
            "applying",
            "active",
            "succeeded",
            "failed",
            "timed_out",
            "resetting",
        }
        or (
            typed.get("active_event") is not None
            and not isinstance(typed.get("active_event"), str)
        )
    ):
        _fail("RUNTIME_STATE_INVALID", "packaged typed readiness is invalid")
    listener = readiness.get("listener_ownership")
    process = state["process"]
    if (
        not isinstance(listener, dict)
        or set(listener)
        != {"host", "port", "socket_inode", "process_group", "owner_pids"}
        or listener.get("host") != acceptance.LOOPBACK_HOST
        or listener.get("port") != state["vista_world_port"]
        or listener.get("process_group") != process["process_group"]
        or not isinstance(listener.get("socket_inode"), int)
        or isinstance(listener.get("socket_inode"), bool)
        or listener["socket_inode"] <= 0
        or not isinstance(listener.get("owner_pids"), list)
        or not listener["owner_pids"]
        or process["pid"] not in listener["owner_pids"]
        or any(
            isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0
            for pid in listener["owner_pids"]
        )
    ):
        _fail("RUNTIME_STATE_INVALID", "packaged listener ownership differs")

    launch_plan_path = _canonical_existing(
        state_path.parent / "launch-plan.json", "packaged launch plan"
    )
    launch_plan_sha = _validate_sha(
        str(state.get("launch_plan_sha256", "")), "packaged launch-plan SHA-256"
    )
    launch_plan, _launch_raw = _load_json_file(
        launch_plan_path,
        label="packaged launch plan",
        expected_sha256=launch_plan_sha,
    )
    expected_plan = packaged_entrypoint.launch_plan(profile_inputs)
    expected_plan["created_at"] = launch_plan.get("created_at")
    if launch_plan != expected_plan:
        _fail(
            "RUNTIME_LAUNCH_PLAN_INVALID",
            "packaged launch plan differs from the running sealed profile",
        )
    return (
        PackagedRuntimeBinding(
            package_attempt=package_attempt,
            state_path=state_path,
            state_sha256=state_sha,
            launch_plan_path=launch_plan_path,
            launch_plan_sha256=launch_plan_sha,
            port=state["vista_world_port"],
        ),
        state,
        profile_inputs,
    )


def validate_inputs(config: RendererAcceptanceConfig) -> RendererInputs:
    workspace = _canonical_existing(config.workspace, "workspace", directory=True)
    repo_root = _canonical_existing(config.repo_root, "repo root", directory=True)
    if COMMIT_RE.fullmatch(config.source_commit) is None:
        _fail("SOURCE_COMMIT_INVALID", "source commit must be a full lowercase SHA-1")
    if not 0.05 <= config.socket_timeout_s <= 5.0:
        _fail("TIMEOUT_INVALID", "socket timeout must be 0.05 through 5 seconds")

    build_path = _canonical_existing(workspace / "result-receipt.json", "build result")
    build_result, _build_raw = _load_json_file(
        build_path,
        label="build result",
        expected_sha256=config.build_result_sha256,
        require_canonical=True,
    )
    try:
        acceptance._validate_build_result(
            build_path,
            config.build_result_sha256,
            workspace=workspace,
            runtime_state={
                "runtime_profile": EXPECTED_PROFILE,
                "map": EXPECTED_MAP,
            },
        )
        acceptance._validate_source(repo_root, config.source_commit)
    except acceptance.AcceptanceError as exc:
        raise RendererAcceptanceError(exc.code, str(exc)) from exc
    (
        profile_path,
        profile_sha,
        profile,
        request_path,
        request_sha,
        request,
        compilation,
    ) = _validate_visual_and_renderer(workspace, build_result)

    execution_path = _canonical_existing(workspace / "execution.json", "execution")
    execution_sha = _validate_sha(
        str(build_result.get("execution_sha256", "")), "execution SHA-256"
    )
    execution, _execution_raw = _load_json_file(
        execution_path,
        label="execution",
        expected_sha256=execution_sha,
        require_canonical=True,
    )
    project_value = execution.get("project_file")
    if not isinstance(project_value, str):
        _fail("EXECUTION_BINDING_INVALID", "execution project path is invalid")
    project_path = _canonical_existing(Path(project_value), "source build project")
    _contained(project_path, workspace, "source build project")
    project_sha = sha256_file(project_path)
    renderer_record = execution.get("renderer_profile_request")
    if (
        execution.get("attempt_root") != str(workspace)
        or execution.get("project_file") != str(project_path)
        or execution.get("project_sha256") != project_sha
        or execution.get("visual_profile_path") != str(profile_path)
        or execution.get("visual_profile_sha256") != profile_sha
        or execution.get("visual_profile_content_digest")
        != profile.get("content_digest")
        or not isinstance(renderer_record, dict)
        or renderer_record
        != {
            "path": str(request_path),
            "sha256": request_sha,
            "content_digest": request["content_digest"],
            "status": "staged_runtime_observation_required",
            "runtime_proof": False,
        }
    ):
        _fail(
            "EXECUTION_BINDING_INVALID",
            "execution does not bind the project, profile, and renderer request",
        )

    preparation_path = _canonical_existing(
        workspace / "preparation-receipt.json", "preparation receipt"
    )
    preparation, _preparation_raw = _load_json_file(
        preparation_path, label="preparation receipt", require_canonical=True
    )
    preparation_sha = sha256_file(preparation_path)
    engine_config_path = _canonical_existing(
        project_path.parent / "Config" / "DefaultEngine.ini", "renderer engine config"
    )
    _contained(engine_config_path, workspace, "renderer engine config")
    engine_config_sha = sha256_file(engine_config_path)
    plugin_path = _canonical_existing(
        project_path.parent / "Plugins" / build_home.EXPECTED_PLUGIN_NAME,
        "installed plugin",
        directory=True,
    )
    _contained(plugin_path, workspace, "installed plugin")
    try:
        plugin_snapshot = build_home.snapshot_tree(plugin_path, "installed plugin")
    except build_home.BuildHomeError as exc:
        raise RendererAcceptanceError(
            "PLUGIN_IDENTITY_INVALID", "installed plugin tree is invalid"
        ) from exc
    if (
        preparation.get("status") != "prepared"
        or preparation.get("attempt_root") != str(workspace)
        or preparation.get("execution_sha256") != execution_sha
        or preparation.get("project_sha256") != project_sha
        or preparation.get("plugin_tree_sha256") != plugin_snapshot.sha256
        or preparation.get("visual_profile_sha256") != profile_sha
        or preparation.get("visual_profile_content_digest")
        != profile.get("content_digest")
        or preparation.get("renderer_profile_request_sha256") != request_sha
        or preparation.get("renderer_profile_request_content_digest")
        != request.get("content_digest")
        or preparation.get("renderer_runtime_observation") != "pending"
        or request.get("engine_config_sha256") != engine_config_sha
    ):
        _fail(
            "PREPARATION_BINDING_INVALID",
            "preparation/plugin/config renderer binding differs",
        )

    package_path = _canonical_existing(config.package_receipt, "package receipt")
    package_sha = _validate_sha(
        config.package_receipt_sha256, "package-receipt SHA-256"
    )
    package, package_raw = _load_json_file(
        package_path,
        label="package receipt",
        expected_sha256=package_sha,
        require_canonical=True,
    )
    if (
        package.get("output") != str(package_path)
        or sha256_bytes(package_raw) != package_sha
    ):
        _fail("PACKAGE_IDENTITY_INVALID", "package receipt output identity differs")
    try:
        package_projection = package_receipt.renderer_observation_package_projection(
            package,
            source_build_result=build_path,
            source_build_result_sha256=config.build_result_sha256,
            source_commit=config.source_commit,
            visual_profile_id=str(profile["visual_profile_id"]),
            visual_profile_sha256=profile_sha,
            visual_profile_content_digest=str(profile["content_digest"]),
            renderer_profile_request_sha256=request_sha,
            renderer_profile_request_content_digest=str(request["content_digest"]),
        )
    except package_receipt.PackageReceiptError as exc:
        raise RendererAcceptanceError(exc.code, str(exc)) from exc
    if package_projection.get("attempt_root") != package.get("attempt_root"):
        _fail("PACKAGE_IDENTITY_INVALID", "package attempt projection differs")
    package_attempt = _canonical_existing(
        Path(str(package_projection["attempt_root"])),
        "package attempt",
        directory=True,
    )
    if package_path.parent != package_attempt:
        _fail("PACKAGE_IDENTITY_INVALID", "package receipt is not attempt-local")
    package_policy = package_projection["project_policy"]
    package_project_path = _canonical_existing(
        package_attempt / package_receipt.PROJECT_RELATIVE,
        "package project descriptor",
    )
    package_config_path = _canonical_existing(
        package_attempt / package_receipt.PROJECT_CONFIG_RELATIVE,
        "package project config",
    )
    package_project_sha = sha256_file(package_project_path)
    package_config_sha = sha256_file(package_config_path)
    if (
        package_policy["project_descriptor"] != str(package_project_path)
        or package_policy["project_descriptor_sha256"] != package_project_sha
        or package_policy["project_config"] != str(package_config_path)
        or package_policy["project_config_sha256"] != package_config_sha
    ):
        _fail(
            "PACKAGE_PROJECT_IDENTITY_INVALID",
            "package project/config bytes differ from their accepted policy",
        )
    runtime_binding, runtime_state, _profile_inputs = _validate_packaged_runtime(
        package_attempt=package_attempt,
        package_path=package_path,
        package_sha=package_sha,
        package=package,
        package_projection=package_projection,
        expected_state_sha=config.runtime_state_sha256,
    )
    return RendererInputs(
        config=config,
        runtime_binding=runtime_binding,
        runtime_state=runtime_state,
        build_result=build_result,
        visual_profile_path=profile_path,
        visual_profile_sha256=profile_sha,
        visual_profile=profile,
        renderer_request_path=request_path,
        renderer_request_sha256=request_sha,
        renderer_request=request,
        renderer_compilation=compilation,
        execution_path=execution_path,
        execution_sha256=execution_sha,
        preparation_path=preparation_path,
        preparation_sha256=preparation_sha,
        project_path=project_path,
        project_sha256=project_sha,
        engine_config_path=engine_config_path,
        engine_config_sha256=engine_config_sha,
        plugin_path=plugin_path,
        plugin_snapshot=plugin_snapshot,
        package_path=package_path,
        package_sha256=package_sha,
        package=package,
        package_projection=package_projection,
        package_project_path=package_project_path,
        package_project_sha256=package_project_sha,
        package_config_path=package_config_path,
        package_config_sha256=package_config_sha,
    )


def assert_inputs_stable(inputs: RendererInputs) -> None:
    current_state = _resolve_packaged_runtime_state(
        inputs.runtime_binding.package_attempt
    )
    if current_state != inputs.runtime_binding.state_path:
        _fail("RUNTIME_POINTER_CHANGED", "current packaged runtime changed")
    if not hmac.compare_digest(
        sha256_file(current_state), inputs.runtime_binding.state_sha256
    ):
        _fail("RUNTIME_STATE_CHANGED", "packaged runtime state changed")
    if not hmac.compare_digest(
        sha256_file(inputs.runtime_binding.launch_plan_path),
        inputs.runtime_binding.launch_plan_sha256,
    ):
        _fail("RUNTIME_LAUNCH_PLAN_CHANGED", "packaged launch plan changed")
    try:
        acceptance._validate_identity(inputs.runtime_state["process"], "packaged-game")
        acceptance._validate_identity(
            inputs.runtime_state["supervisor"],
            "vista-world-packaged-supervisor",
        )
        acceptance._validate_source(
            _canonical_existing(inputs.config.repo_root, "repo root", directory=True),
            inputs.config.source_commit,
        )
    except acceptance.AcceptanceError as exc:
        raise RendererAcceptanceError(exc.code, str(exc)) from exc
    fixed_files = (
        (
            _canonical_existing(
                inputs.config.workspace / "result-receipt.json", "build result"
            ),
            inputs.config.build_result_sha256,
        ),
        (inputs.visual_profile_path, inputs.visual_profile_sha256),
        (inputs.renderer_request_path, inputs.renderer_request_sha256),
        (inputs.execution_path, inputs.execution_sha256),
        (inputs.preparation_path, inputs.preparation_sha256),
        (inputs.project_path, inputs.project_sha256),
        (inputs.engine_config_path, inputs.engine_config_sha256),
        (inputs.package_path, inputs.package_sha256),
        (inputs.package_project_path, inputs.package_project_sha256),
        (inputs.package_config_path, inputs.package_config_sha256),
    )
    for path, expected in fixed_files:
        if not hmac.compare_digest(sha256_file(path), expected):
            _fail("EVIDENCE_CHANGED", f"{path.name} changed during observation")
    try:
        current_plugin = build_home.snapshot_tree(
            inputs.plugin_path, "installed plugin"
        )
    except build_home.BuildHomeError as exc:
        raise RendererAcceptanceError(
            "PLUGIN_IDENTITY_CHANGED", "installed plugin changed during observation"
        ) from exc
    if current_plugin != inputs.plugin_snapshot:
        _fail("PLUGIN_IDENTITY_CHANGED", "installed plugin changed during observation")


def prove_current_listener(
    inputs: RendererInputs,
    listener_prover: ListenerProver,
) -> dict[str, Any]:
    """Prove that the observed port still belongs to the packaged game group."""

    process = inputs.runtime_state["process"]
    try:
        proof = listener_prover(
            inputs.runtime_binding.port,
            process["process_group"],
        )
    except packaged_smoke.PackagedSmokeError as exc:
        raise RendererAcceptanceError(
            "LISTENER_OWNERSHIP_INVALID",
            "packaged renderer listener ownership could not be proven",
        ) from exc
    recorded = inputs.runtime_state["readiness"]["listener_ownership"]
    if not isinstance(proof, Mapping) or dict(proof) != recorded:
        _fail(
            "LISTENER_OWNERSHIP_CHANGED",
            "packaged renderer listener differs from its readiness proof",
        )
    return dict(proof)


def exchange_loopback_raw(
    request: Mapping[str, Any], timeout: float, port: int
) -> tuple[bytes, Any]:
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        _fail("PORT_INVALID", "renderer runtime port is invalid")
    if not 0.05 <= timeout <= 5.0:
        _fail("TIMEOUT_INVALID", "renderer runtime timeout is invalid")
    encoded = canonical_json(request)
    response = bytearray()
    deadline = time.monotonic() + timeout
    try:
        with socket.create_connection(
            (acceptance.LOOPBACK_HOST, port), timeout=timeout
        ) as connection:
            connection.settimeout(max(0.001, deadline - time.monotonic()))
            connection.sendall(encoded)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _fail("RUNTIME_TIMEOUT", "renderer response exceeded its deadline")
                connection.settimeout(remaining)
                block = connection.recv(
                    min(8192, MAX_RESPONSE_BYTES + 1 - len(response))
                )
                if not block:
                    break
                response.extend(block)
                if len(response) > MAX_RESPONSE_BYTES:
                    _fail("RESPONSE_TOO_LARGE", "renderer response exceeded 64 KiB")
    except RendererAcceptanceError:
        raise
    except (socket.timeout, TimeoutError) as exc:
        raise RendererAcceptanceError(
            "RUNTIME_TIMEOUT", "renderer response timed out"
        ) from exc
    except OSError as exc:
        raise RendererAcceptanceError(
            "RUNTIME_CONNECTION_FAILED", "renderer runtime connection failed"
        ) from exc
    raw = bytes(response)
    return raw, strict_json_bytes(raw, label="renderer runtime response")


def _new_command_id() -> str:
    value = "vwc-" + os.urandom(12).hex()
    if COMMAND_ID_RE.fullmatch(value) is None:
        _fail("COMMAND_ID_INVALID", "could not allocate renderer command ID")
    return value


def validate_renderer_response(
    inputs: RendererInputs,
    response: Any,
    *,
    command_id: str,
) -> dict[str, Any]:
    if not isinstance(response, dict):
        _fail("RESPONSE_SHAPE_INVALID", "renderer response must be an object")
    try:
        return build_home.evaluate_renderer_status_response(
            inputs.renderer_compilation,
            response,
            command_id=command_id,
        )
    except build_home.BuildHomeError as exc:
        code = (
            "RENDERER_OBSERVATION_REJECTED"
            if exc.code == "VISTA_HOME_RENDERER_OBSERVATION_REJECTED"
            else "RESPONSE_SCHEMA_INVALID"
        )
        raise RendererAcceptanceError(code, str(exc)) from exc


def _reserve_output(inputs: RendererInputs) -> int:
    output = Path(inputs.config.output).expanduser()
    if (
        not output.is_absolute()
        or ".." in output.parts
        or OUTPUT_RE.fullmatch(output.name) is None
        or output.parent != inputs.runtime_binding.state_path.parent
    ):
        _fail(
            "RECEIPT_PATH_INVALID",
            "renderer receipt must be a direct current runtime-attempt child",
        )
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        return os.open(output, flags, 0o600)
    except FileExistsError as exc:
        raise RendererAcceptanceError(
            "RECEIPT_EXISTS", "renderer receipt already exists"
        ) from exc
    except OSError as exc:
        raise RendererAcceptanceError(
            "RECEIPT_OPEN_FAILED", "could not reserve renderer receipt"
        ) from exc


def build_receipt(
    inputs: RendererInputs,
    *,
    request: Mapping[str, Any],
    raw_response: bytes,
    response: Mapping[str, Any],
    evaluation: Mapping[str, Any],
    listener_before: Mapping[str, Any],
    listener_after: Mapping[str, Any],
) -> dict[str, Any]:
    state = inputs.runtime_state
    observation_contract = inputs.renderer_request["observation_contract"]
    package_project = inputs.package.get("project_policy")
    receipt: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "status": "accepted",
        "renderer_runtime_observation": "observed_accepted",
        "created_at": utc_now(),
        "output": str(inputs.config.output),
        "bindings": {
            "source": {
                "repo_root": str(inputs.config.repo_root),
                "commit": inputs.config.source_commit,
                "clean": True,
            },
            "visual_profile": {
                "path": str(inputs.visual_profile_path),
                "sha256": inputs.visual_profile_sha256,
                "content_digest": inputs.visual_profile["content_digest"],
                "visual_profile_id": inputs.visual_profile["visual_profile_id"],
                "observation_contract_sha256": sha256_bytes(
                    canonical_json(observation_contract)
                ),
            },
            "renderer_request": {
                "path": str(inputs.renderer_request_path),
                "sha256": inputs.renderer_request_sha256,
                "content_digest": inputs.renderer_request["content_digest"],
                "renderer_profile_digest": inputs.renderer_request[
                    "renderer_profile_digest"
                ],
                "runtime_proof_in_request": False,
            },
            "build": {
                "path": str(inputs.config.workspace / "result-receipt.json"),
                "sha256": inputs.config.build_result_sha256,
                "content_digest": inputs.build_result["content_digest"],
                "execution_path": str(inputs.execution_path),
                "execution_sha256": inputs.execution_sha256,
                "preparation_path": str(inputs.preparation_path),
                "preparation_sha256": inputs.preparation_sha256,
                "pending_renderer_observation": True,
            },
            "package": {
                "path": str(inputs.package_path),
                "sha256": inputs.package_sha256,
                **dict(inputs.package_projection),
                "project_policy_sha256": sha256_bytes(canonical_json(package_project)),
            },
            "plugin": {
                "path": str(inputs.plugin_path),
                "tree_sha256": inputs.plugin_snapshot.sha256,
                "file_count": inputs.plugin_snapshot.file_count,
                "bytes": inputs.plugin_snapshot.total_bytes,
            },
            "project": {
                "path": str(inputs.project_path),
                "sha256": inputs.project_sha256,
                "engine_config_path": str(inputs.engine_config_path),
                "engine_config_sha256": inputs.engine_config_sha256,
            },
            "world": {
                "map_path": EXPECTED_MAP,
                "revision": acceptance.DEFAULT_WORLD_REVISION,
            },
            "runtime": {
                "state_path": str(inputs.runtime_binding.state_path),
                "state_sha256": inputs.config.runtime_state_sha256,
                "launch_plan_path": str(inputs.runtime_binding.launch_plan_path),
                "launch_plan_sha256": inputs.runtime_binding.launch_plan_sha256,
                "mode": state["mode"],
                "profile_path": state["profile"],
                "profile_sha256": state["profile_sha256"],
                "runtime_profile": state["runtime_profile"],
                "camera_profile": state["camera_profile"],
                "executable": state["executable"],
                "executable_sha256": state["executable_sha256"],
                "archive_reverified_after_readiness": state[
                    "archive_reverified_after_readiness"
                ],
                "display": state["display"],
                "gpu": state["gpu"],
                "port": state["vista_world_port"],
                "width": state["width"],
                "height": state["height"],
                "fps": state["fps"],
                "process": state["process"],
                "supervisor": state["supervisor"],
                "listener_before": dict(listener_before),
                "listener_after": dict(listener_after),
            },
        },
        "protocol": {
            "request": dict(request),
            "request_sha256": sha256_bytes(canonical_json(request)),
            "response": dict(response),
            "response_raw_sha256": sha256_bytes(raw_response),
            "response_canonical_sha256": sha256_bytes(canonical_json(response)),
            "one_request_one_eof_response": True,
            "read_only": True,
        },
        "evaluation": dict(evaluation),
    }
    receipt["content_digest"] = _content_digest(receipt)
    return receipt


def execute_acceptance(
    config: RendererAcceptanceConfig,
    *,
    exchange: Exchange = exchange_loopback_raw,
    listener_prover: ListenerProver = (
        packaged_smoke.prove_loopback_listener_ownership
    ),
) -> tuple[dict[str, Any], str]:
    inputs = validate_inputs(config)
    listener_before = prove_current_listener(inputs, listener_prover)
    command_id = _new_command_id()
    request = {
        "type": "vista_world_action",
        "params": {"operation": "renderer_status", "command_id": command_id},
    }
    raw_response, response = exchange(
        request,
        config.socket_timeout_s,
        inputs.runtime_binding.port,
    )
    if not isinstance(raw_response, bytes):
        _fail("RESPONSE_BYTES_INVALID", "exchange did not retain raw response bytes")
    parsed_raw_response = strict_json_bytes(
        raw_response, label="renderer runtime response"
    )
    if parsed_raw_response != response:
        _fail(
            "RESPONSE_BYTES_MISMATCH",
            "parsed renderer response differs from retained wire bytes",
        )
    evaluation = validate_renderer_response(inputs, response, command_id=command_id)
    assert_inputs_stable(inputs)
    listener_after = prove_current_listener(inputs, listener_prover)
    if listener_after != listener_before:
        _fail(
            "LISTENER_OWNERSHIP_CHANGED",
            "packaged renderer listener changed during observation",
        )
    receipt = build_receipt(
        inputs,
        request=request,
        raw_response=raw_response,
        response=response,
        evaluation=evaluation,
        listener_before=listener_before,
        listener_after=listener_after,
    )
    descriptor = _reserve_output(inputs)
    raw = canonical_json(receipt)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise RendererAcceptanceError(
            "RECEIPT_WRITE_FAILED", "could not commit renderer receipt"
        ) from exc
    return receipt, sha256_bytes(raw)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--package-receipt", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--runtime-state-sha256", required=True)
    parser.add_argument("--build-result-sha256", required=True)
    parser.add_argument("--package-receipt-sha256", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--socket-timeout", type=float, default=2.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = RendererAcceptanceConfig(
        workspace=args.workspace,
        repo_root=args.repo_root,
        package_receipt=args.package_receipt,
        output=args.output,
        runtime_state_sha256=args.runtime_state_sha256,
        build_result_sha256=args.build_result_sha256,
        package_receipt_sha256=args.package_receipt_sha256,
        source_commit=args.source_commit,
        socket_timeout_s=args.socket_timeout,
    )
    try:
        receipt, receipt_sha = execute_acceptance(config)
    except RendererAcceptanceError as exc:
        print(
            json.dumps(
                {"status": "rejected", "code": exc.code, "message": str(exc)},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "status": receipt["status"],
                "renderer_runtime_observation": receipt["renderer_runtime_observation"],
                "receipt": str(config.output),
                "receipt_sha256": receipt_sha,
                "content_digest": receipt["content_digest"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
