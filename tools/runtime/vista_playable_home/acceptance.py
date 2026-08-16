#!/usr/bin/env python3
"""Fail-closed typed acceptance for a fresh VISTA Playable Home runtime.

The command is deliberately evidence-bound: it accepts only the current
append-only runtime attempt, the fixed loopback adapter, an exact UE build
receipt, and a clean source commit.  It never launches or stops Unreal.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import socket
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
        DEFAULT_VISTA_WORLD_PORT,
        DEFAULT_WORLD_REVISION,
        R2_CAMERA_PROFILE,
        R2_DISPLAY,
        R2_FPS,
        R2_GPU,
        R2_HEIGHT,
        R2_RUNTIME_PROFILE,
        R2_VISTA_WORLD_PORT,
        R2_WIDTH,
        TYPED_RESPONSE_MAX_BYTES,
        identity_is_live,
    )
else:
    from .runtime import (
        DEFAULT_VISTA_WORLD_PORT,
        DEFAULT_WORLD_REVISION,
        R2_CAMERA_PROFILE,
        R2_DISPLAY,
        R2_FPS,
        R2_GPU,
        R2_HEIGHT,
        R2_RUNTIME_PROFILE,
        R2_VISTA_WORLD_PORT,
        R2_WIDTH,
        TYPED_RESPONSE_MAX_BYTES,
        identity_is_live,
    )


RECEIPT_SCHEMA = "simworld.vista.playable-home-runtime-acceptance/v1"
R2_RECEIPT_SCHEMA = "simworld.vista.playable-home-runtime-acceptance/v2"
RUNTIME_POINTER_SCHEMA = "simworld.vista.playable-home-runtime-pointer/v1"
RUNTIME_STATE_SCHEMA = "simworld.vista.playable-home-runtime-state/v1"
R2_RUNTIME_STATE_SCHEMA = "simworld.vista.playable-home-runtime-state/v2"
BUILD_RESULT_SCHEMA = "simworld.vista.playable-home-ue-build-result/v1"
LOOPBACK_HOST = "127.0.0.1"
R2_PRESENTATION_COLLISION_POLICY = (
    "presentation_no_collision_use_hidden_r1_proxies"
)
R2_PRESENTATION_BUNDLE_COUNT = 3
R2_EXTERNAL_BUILD_FIELDS = frozenset({
    "presentation_external_content_verified",
    "presentation_external_nanite_policy",
    "presentation_external_nanite_disabled_verified",
})
R2_EXTERNAL_NANITE_POLICY = (
    "disabled_unproven_opaque_or_translucent_external_bundle_v1"
)

PLAYER_ID = "home.r1/player.01"
DOOR_ID = "home.r1/room.entry_hall/entity.interior_door.01"
OFFICE_DOOR_ID = "home.r1/room.entry_hall/entity.interior_door.04"
NPC_ID = "home.r1/room.entry_hall/entity.resident.01"
LIVING_ANCHOR_ID = "home.r1/room.living_room/anchor.room_center"
OFFICE_ANCHOR_ID = "home.r1/room.office/anchor.room_center"
KEYS_ID = "home.r1/room.living_room/entity.keys.01"
TABLETOP_RIGHT_ID = (
    "home.r1/room.living_room/entity.coffee_table.01/anchor.tabletop_right"
)
OFFICE_DESK_ANCHOR_ID = "home.r1/room.office/entity.desk.01/anchor.desktop"
EVENT_IDS = ("mmg_001", "mmg_044", "mmg_045")
DOOR_LOCATION_XY = (-150.0, -200.0)
LIVING_CLEAR_X_RANGE_CM = (-610.0, -330.0)
LIVING_CLEAR_Y_RANGE_CM = (-360.0, -40.0)
DOOR_CLEARANCE_RADIUS_CM = 220.0
OFFICE_X_RANGE_CM = (150.0, 650.0)
OFFICE_Y_RANGE_CM = (0.0, 400.0)
TABLETOP_RIGHT_LOCATION_CM = (-365.0, -170.0, 48.0)
OFFICE_DESK_LOCATION_CM = (520.0, 280.0, 76.0)
PLACEMENT_TOLERANCE_CM = 2.0

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
COMMAND_ID_RE = re.compile(r"^vwc-[0-9a-f]{24}$")
ATTEMPT_RE = re.compile(r"^attempt-[0-9]{8}T[0-9]{6}\.[0-9]{6}Z-[0-9]+$")
OUTPUT_RE = re.compile(r"^runtime-acceptance(?:-[A-Za-z0-9._-]{1,80})?\.json$")

STATUS_KEYS = frozenset({
    "command_id",
    "status",
    "code",
    "world_revision",
    "session_generation",
    "event_status",
    "active_event",
})
INTERACTION_KEYS = frozenset({
    "command_id",
    "status",
    "code",
    "session_generation",
    "target_semantic_id",
    "state",
})
NPC_QUEUE_KEYS = frozenset({
    "command_id",
    "status",
    "code",
    "session_generation",
    "target_semantic_id",
})
EVENT_KEYS = frozenset({
    "command_id",
    "status",
    "code",
    "session_generation",
})
STATE_KEYS = frozenset({"semantic_id", "hidden", "portable", "transform", "values"})
TRANSFORM_KEYS = frozenset({"location_cm", "rotation_deg", "scale"})


class AcceptanceError(RuntimeError):
    """A closed acceptance failure with a stable machine-readable code."""

    def __init__(self, code: str, message: str, *, step: str | None = None):
        super().__init__(message)
        self.code = code
        self.step = step


@dataclass(frozen=True)
class AcceptanceConfig:
    workspace: Path
    repo_root: Path
    output: Path
    runtime_state_sha256: str
    build_result_sha256: str
    source_commit: str
    socket_timeout_s: float = 1.0
    npc_timeout_s: float = 30.0
    npc_poll_interval_s: float = 0.25
    runtime_profile: str | None = None


@dataclass(frozen=True)
class EvidenceBinding:
    workspace: Path
    runtime_state_path: Path
    runtime_state_sha256: str
    build_result_path: Path
    build_result_sha256: str
    repo_root: Path
    source_commit: str
    map_path: str
    project_path: Path
    port: int = DEFAULT_VISTA_WORLD_PORT
    runtime_profile: str | None = None
    camera_profile: str | None = None
    display: str | None = None
    gpu: int | None = None
    width: int | None = None
    height: int | None = None
    fps: int | None = None
    launch_plan_path: Path | None = None
    launch_plan_sha256: str | None = None

    def receipt_value(self) -> dict[str, Any]:
        value = {
            "workspace": str(self.workspace),
            "runtime_state": str(self.runtime_state_path),
            "runtime_state_sha256": self.runtime_state_sha256,
            "build_result": str(self.build_result_path),
            "build_result_sha256": self.build_result_sha256,
            "repo_root": str(self.repo_root),
            "source_commit": self.source_commit,
            "source_clean": True,
            "host": LOOPBACK_HOST,
            "port": self.port,
            "world_revision": DEFAULT_WORLD_REVISION,
            "map_path": self.map_path,
            "project": str(self.project_path),
        }
        if self.runtime_profile is not None:
            value.update(
                {
                    "runtime_profile": self.runtime_profile,
                    "camera_profile": self.camera_profile,
                    "display": self.display,
                    "gpu": self.gpu,
                    "width": self.width,
                    "height": self.height,
                    "fps": self.fps,
                    "launch_plan": str(self.launch_plan_path),
                    "launch_plan_sha256": self.launch_plan_sha256,
                }
            )
        return value


Exchange = Callable[[Mapping[str, Any], float], Any]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fail(code: str, message: str, *, step: str | None = None) -> None:
    raise AcceptanceError(code, message, step=step)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _canonical_json_bytes(value: Any) -> bytes:
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
        raise AcceptanceError("JSON_INVALID", "receipt value is not finite JSON") from exc


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise AcceptanceError("EVIDENCE_READ_FAILED", f"could not hash {path}") from exc
    return digest.hexdigest()


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError(f"duplicate JSON key: {key}")
        output[key] = value
    return output


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
        text = raw.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        _reject_nonfinite(value)
        return value
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise AcceptanceError("JSON_INVALID", f"{label} is not strict JSON") from exc


def _canonical_existing_directory(path: Path, label: str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute() or ".." in candidate.parts:
        _fail("PATH_IDENTITY_INVALID", f"{label} must be an absolute canonical path")
    if candidate.is_symlink():
        _fail("PATH_SYMLINK_REFUSED", f"{label} must not be a symlink")
    try:
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise AcceptanceError("PATH_MISSING", f"{label} does not exist") from exc
    if resolved != candidate or not resolved.is_dir():
        _fail("PATH_IDENTITY_INVALID", f"{label} must name its real directory identity")
    return resolved


def _canonical_existing_file(path: Path, label: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute() or ".." in candidate.parts or candidate.is_symlink():
        _fail("PATH_IDENTITY_INVALID", f"{label} must be an absolute non-symlink path")
    try:
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise AcceptanceError("PATH_MISSING", f"{label} does not exist") from exc
    if resolved != candidate or not resolved.is_file():
        _fail("PATH_IDENTITY_INVALID", f"{label} must name its real file identity")
    return resolved


def _contained(path: Path, root: Path, label: str) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise AcceptanceError(
            "PATH_ESCAPE_REFUSED", f"{label} must be contained by {root}"
        ) from exc


def _load_strict_file(path: Path, *, label: str, max_bytes: int = 1024 * 1024) -> Any:
    try:
        size = path.stat().st_size
        if size <= 0 or size > max_bytes:
            _fail("EVIDENCE_SIZE_INVALID", f"{label} size is outside its bound")
        raw = path.read_bytes()
    except OSError as exc:
        raise AcceptanceError("EVIDENCE_READ_FAILED", f"could not read {label}") from exc
    if len(raw) != size:
        _fail("EVIDENCE_CHANGED", f"{label} changed while it was read")
    return strict_json_bytes(raw, label=label)


def resolve_current_state_path(workspace: Path) -> Path:
    root = workspace / "game-runtime"
    if root.is_symlink():
        _fail("PATH_SYMLINK_REFUSED", "game-runtime must not be a symlink")
    root = _canonical_existing_directory(root, "game-runtime")
    pointer_path = _canonical_existing_file(root / "current.json", "runtime pointer")
    pointer = _load_strict_file(pointer_path, label="runtime pointer", max_bytes=4096)
    if (
        not isinstance(pointer, dict)
        or set(pointer) != {"schema", "state"}
        or pointer.get("schema") != RUNTIME_POINTER_SCHEMA
        or not isinstance(pointer.get("state"), str)
    ):
        _fail("RUNTIME_POINTER_INVALID", "runtime pointer has an invalid shape")
    relative = Path(pointer["state"])
    if (
        relative.is_absolute()
        or len(relative.parts) != 2
        or not ATTEMPT_RE.fullmatch(relative.parts[0])
        or relative.parts[1] != "runtime-state.json"
    ):
        _fail("RUNTIME_POINTER_INVALID", "runtime pointer target is invalid")
    state_path = _canonical_existing_file(root / relative, "runtime state")
    _contained(state_path, root, "runtime state")
    if state_path.parent.parent != root:
        _fail("RUNTIME_POINTER_INVALID", "runtime state is not a direct attempt artifact")
    return state_path


class ExclusiveReceipt:
    """A single-use private receipt reserved with O_EXCL and O_NOFOLLOW."""

    def __init__(self, path: Path, descriptor: int):
        self.path = path
        self._descriptor = descriptor
        self._written = False

    @classmethod
    def reserve(cls, workspace: Path, output: Path) -> "ExclusiveReceipt":
        state_path = resolve_current_state_path(workspace)
        candidate = Path(output).expanduser()
        if (
            not candidate.is_absolute()
            or ".." in candidate.parts
            or not OUTPUT_RE.fullmatch(candidate.name)
        ):
            _fail(
                "RECEIPT_PATH_INVALID",
                "output must be an absolute runtime-acceptance[-id].json path",
            )
        try:
            parent = candidate.parent.resolve(strict=True)
        except (FileNotFoundError, OSError) as exc:
            raise AcceptanceError(
                "RECEIPT_PATH_INVALID", "receipt parent does not exist"
            ) from exc
        if parent != candidate.parent or parent != state_path.parent:
            _fail(
                "RECEIPT_PATH_INVALID",
                "receipt must be a direct child of the current runtime attempt",
            )
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(candidate, flags, 0o600)
        except FileExistsError as exc:
            raise AcceptanceError(
                "RECEIPT_EXISTS", "acceptance receipt already exists and will not be replaced"
            ) from exc
        except OSError as exc:
            raise AcceptanceError("RECEIPT_OPEN_FAILED", "could not reserve receipt") from exc
        return cls(candidate, descriptor)

    def write(self, payload: Mapping[str, Any]) -> None:
        if self._written or self._descriptor < 0:
            _fail("RECEIPT_STATE_INVALID", "receipt writer was already consumed")
        raw = _canonical_json_bytes(payload)
        try:
            with os.fdopen(self._descriptor, "wb") as handle:
                self._descriptor = -1
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            self._written = True
        except OSError as exc:
            raise AcceptanceError("RECEIPT_WRITE_FAILED", "could not commit receipt") from exc

def _validate_sha(value: str, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        _fail("DIGEST_INVALID", f"{label} must be a lowercase SHA-256")
    return value


def _validate_identity(identity: Any, role: str) -> None:
    if (
        not isinstance(identity, dict)
        or set(identity) != {"role", "pid", "start_ticks", "process_group"}
        or identity.get("role") != role
        or not _is_int(identity.get("pid"))
        or identity["pid"] <= 0
        or not _is_int(identity.get("start_ticks"))
        or identity["start_ticks"] <= 0
        or not _is_int(identity.get("process_group"))
        or identity["process_group"] <= 0
        or not identity_is_live(identity)
    ):
        _fail("RUNTIME_IDENTITY_INVALID", f"{role} process identity is not live")


def _validate_r2_launch_plan(
    path: Path,
    expected_sha: str,
    *,
    workspace: Path,
    state: Mapping[str, Any],
) -> Path:
    if __package__ in {None, ""}:
        from tools.runtime.vista_playable_home import profile as profile_contract  # type: ignore
    else:
        from . import profile as profile_contract
    try:
        validated = profile_contract.validate_launch_plan(path, expected_sha)
    except (OSError, profile_contract.ProfileError) as exc:
        raise AcceptanceError(
            "RUNTIME_LAUNCH_PLAN_INVALID",
            "r2 launch plan or its SHA-256 binding differs",
        ) from exc
    config = validated.config
    if (
        validated.workspace != workspace
        or config.runtime_profile != R2_RUNTIME_PROFILE
        or config.map_path != state.get("map")
        or str(config.project) != state.get("project")
        or config.display != R2_DISPLAY
        or config.gpu != R2_GPU
        or config.vista_world_port != R2_VISTA_WORLD_PORT
        or config.width != R2_WIDTH
        or config.height != R2_HEIGHT
        or config.fps != R2_FPS
    ):
        _fail(
            "RUNTIME_LAUNCH_PLAN_INVALID",
            "r2 launch plan does not bind the running profile and port",
        )
    return validated.path


def _validate_runtime_state(
    path: Path,
    expected_sha: str,
    workspace: Path,
    *,
    runtime_profile: str | None = None,
) -> dict[str, Any]:
    if sha256_file(path) != expected_sha:
        _fail("RUNTIME_STATE_DIGEST_MISMATCH", "runtime-state SHA-256 differs")
    state = _load_strict_file(path, label="runtime state")
    required = {
        "schema",
        "status",
        "created_at",
        "updated_at",
        "map",
        "project",
        "display",
        "gpu",
        "vista_world_port",
        "process",
        "supervisor",
        "readiness",
    }
    if runtime_profile == R2_RUNTIME_PROFILE:
        required.update(
            {
                "runtime_profile",
                "camera_profile",
                "width",
                "height",
                "fps",
                "launch_plan_sha256",
            }
        )
    if not isinstance(state, dict) or set(state) != required:
        _fail("RUNTIME_STATE_INVALID", "running runtime-state fields differ")
    expected_schema = (
        R2_RUNTIME_STATE_SCHEMA
        if runtime_profile == R2_RUNTIME_PROFILE
        else RUNTIME_STATE_SCHEMA
    )
    expected_port = (
        R2_VISTA_WORLD_PORT
        if runtime_profile == R2_RUNTIME_PROFILE
        else DEFAULT_VISTA_WORLD_PORT
    )
    if (
        state.get("schema") != expected_schema
        or state.get("status") != "running"
        or state.get("vista_world_port") != expected_port
        or not isinstance(state.get("created_at"), str)
        or not isinstance(state.get("updated_at"), str)
        or not isinstance(state.get("display"), str)
        or not _is_int(state.get("gpu"))
        or not isinstance(state.get("map"), str)
        or not state["map"].startswith("/Game/")
        or not isinstance(state.get("project"), str)
    ):
        _fail("RUNTIME_STATE_INVALID", "runtime-state identity is not accepted")
    if runtime_profile == R2_RUNTIME_PROFILE and (
        state.get("runtime_profile") != R2_RUNTIME_PROFILE
        or state.get("camera_profile") != R2_CAMERA_PROFILE
        or state.get("display") != R2_DISPLAY
        or state.get("gpu") != R2_GPU
        or state.get("width") != R2_WIDTH
        or state.get("height") != R2_HEIGHT
        or state.get("fps") != R2_FPS
        or not isinstance(state.get("launch_plan_sha256"), str)
        or SHA256_RE.fullmatch(state["launch_plan_sha256"]) is None
    ):
        _fail("RUNTIME_STATE_INVALID", "r2 runtime profile binding differs")
    _validate_identity(state["process"], "unreal-game")
    _validate_identity(state["supervisor"], "vista-world-supervisor")

    readiness = state["readiness"]
    if (
        not isinstance(readiness, dict)
        or set(readiness) != STATUS_KEYS
        or not COMMAND_ID_RE.fullmatch(str(readiness.get("command_id", "")))
        or readiness.get("status") != "success"
        or readiness.get("code") != "READY"
        or readiness.get("world_revision") != DEFAULT_WORLD_REVISION
        or readiness.get("session_generation") != 0
        or readiness.get("event_status") != "inactive"
        or readiness.get("active_event") is not None
    ):
        _fail("RUNTIME_STATE_INVALID", "runtime readiness is not a fresh generation-zero session")

    project = _canonical_existing_file(Path(state["project"]), "runtime project")
    _contained(project, workspace, "runtime project")
    if project.suffix != ".uproject":
        _fail("RUNTIME_STATE_INVALID", "runtime project is not a .uproject")
    state["_project_path"] = project
    if runtime_profile == R2_RUNTIME_PROFILE:
        state["_launch_plan_path"] = _validate_r2_launch_plan(
            path.parent / "launch-plan.json",
            state["launch_plan_sha256"],
            workspace=workspace,
            state=state,
        )
    return state


def _content_digest(value: Mapping[str, Any]) -> str:
    body = dict(value)
    body.pop("content_digest", None)
    return hashlib.sha256(_canonical_json_bytes(body)).hexdigest()


def _validate_build_result(
    path: Path,
    expected_sha: str,
    *,
    workspace: Path,
    runtime_state: Mapping[str, Any],
) -> dict[str, Any]:
    if sha256_file(path) != expected_sha:
        _fail("BUILD_RESULT_DIGEST_MISMATCH", "build-result SHA-256 differs")
    result = _load_strict_file(path, label="build result")
    required = {
        "schema_version",
        "status",
        "timestamp_utc",
        "attempt_root",
        "revision",
        "map_path",
        "execution_sha256",
        "import_receipt_sha256",
        "scene_receipt_sha256",
        "copy_methods",
        "runtime_play_proof",
        "content_digest",
    }
    r2 = runtime_state.get("runtime_profile") == R2_RUNTIME_PROFILE
    if not isinstance(result, dict):
        _fail("BUILD_RESULT_INVALID", "accepted build-result must be an object")
    if r2:
        required.update(
            {
                "visual_profile_id",
                "visual_profile_sha256",
                "visual_profile_content_digest",
                "renderer_profile_request_sha256",
                "renderer_profile_request_content_digest",
                "renderer_runtime_observation",
                "base_scene_receipt_sha256",
                "presentation_import_receipt_sha256",
                "presentation_scene_receipt_sha256",
                "presentation_manifest_sha256",
                "presentation_artifact_receipt_sha256",
                "presentation_bundle_count",
                "presentation_collision_policy",
                "presentation_ue_import_observation",
                "presentation_runtime_play_proof",
            }
        )
        external_fields = set(result) & R2_EXTERNAL_BUILD_FIELDS
        if external_fields and external_fields != R2_EXTERNAL_BUILD_FIELDS:
            _fail(
                "BUILD_RESULT_INVALID",
                "accepted r2 external presentation fields are partial",
            )
        required.update(external_fields)
    if set(result) != required:
        _fail("BUILD_RESULT_INVALID", "accepted build-result fields differ")
    digests = [
        result.get("execution_sha256"),
        result.get("import_receipt_sha256"),
        result.get("scene_receipt_sha256"),
        result.get("content_digest"),
    ]
    if r2:
        digests.extend(
            result.get(key)
            for key in (
                "visual_profile_sha256",
                "visual_profile_content_digest",
                "renderer_profile_request_sha256",
                "renderer_profile_request_content_digest",
                "base_scene_receipt_sha256",
                "presentation_import_receipt_sha256",
                "presentation_scene_receipt_sha256",
                "presentation_manifest_sha256",
                "presentation_artifact_receipt_sha256",
            )
        )
    copy_methods = result.get("copy_methods")
    if (
        result.get("schema_version") != BUILD_RESULT_SCHEMA
        or result.get("status") != "accepted_candidate"
        or result.get("attempt_root") != str(workspace)
        or result.get("revision") != DEFAULT_WORLD_REVISION
        or result.get("map_path") != runtime_state.get("map")
        or result.get("runtime_play_proof") != "pending"
        or not isinstance(result.get("timestamp_utc"), str)
        or not all(isinstance(value, str) and SHA256_RE.fullmatch(value) for value in digests)
        or not isinstance(copy_methods, dict)
        or not all(
            isinstance(key, str) and key and _is_int(value) and value >= 0
            for key, value in copy_methods.items()
        )
        or result.get("content_digest") != _content_digest(result)
    ):
        _fail("BUILD_RESULT_INVALID", "accepted build-result identity differs")
    if r2 and (
        result.get("visual_profile_id") != R2_RUNTIME_PROFILE
        or result.get("renderer_runtime_observation") != "pending"
        or result.get("base_scene_receipt_sha256")
        != result.get("scene_receipt_sha256")
        or result.get("presentation_bundle_count")
        != R2_PRESENTATION_BUNDLE_COUNT
        or result.get("presentation_collision_policy")
        != R2_PRESENTATION_COLLISION_POLICY
        or result.get("presentation_ue_import_observation")
        != "verified_by_commandlet"
        or result.get("presentation_runtime_play_proof") != "pending"
    ):
        _fail(
            "BUILD_RESULT_INVALID",
            "accepted r2 build/presentation profile binding differs",
        )
    if r2 and R2_EXTERNAL_BUILD_FIELDS <= set(result) and (
        result.get("presentation_external_content_verified") is not True
        or result.get("presentation_external_nanite_policy")
        != R2_EXTERNAL_NANITE_POLICY
        or result.get("presentation_external_nanite_disabled_verified") is not True
    ):
        _fail(
            "BUILD_RESULT_INVALID",
            "accepted r2 external presentation proof differs",
        )
    return result


def _run_git(repo: Path, arguments: Sequence[str]) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5.0,
            check=False,
            env={
                key: value
                for key, value in os.environ.items()
                if key not in {"STUDIO_ACCESS_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"}
            },
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AcceptanceError("SOURCE_GIT_FAILED", "git source check failed") from exc
    if completed.returncode != 0:
        _fail("SOURCE_GIT_FAILED", "git source check was rejected")
    return completed.stdout.strip()


def _validate_source(repo: Path, expected_commit: str) -> None:
    top = _run_git(repo, ("rev-parse", "--show-toplevel"))
    if top != str(repo):
        _fail("SOURCE_IDENTITY_MISMATCH", "repo-root is not the git top-level identity")
    head = _run_git(repo, ("rev-parse", "HEAD"))
    if head != expected_commit:
        _fail("SOURCE_COMMIT_MISMATCH", "source HEAD differs from the pinned commit")
    status = _run_git(repo, ("status", "--porcelain=v1", "--untracked-files=normal"))
    if status:
        _fail("SOURCE_DIRTY", "source checkout is not clean")


def validate_binding(config: AcceptanceConfig) -> EvidenceBinding:
    workspace = _canonical_existing_directory(config.workspace, "workspace")
    repo = _canonical_existing_directory(config.repo_root, "repo-root")
    runtime_sha = _validate_sha(config.runtime_state_sha256, "runtime-state SHA-256")
    build_sha = _validate_sha(config.build_result_sha256, "build-result SHA-256")
    if not COMMIT_RE.fullmatch(config.source_commit):
        _fail("SOURCE_COMMIT_INVALID", "source commit must be a lowercase full commit SHA")
    if not 0.05 <= config.socket_timeout_s <= 5.0:
        _fail("TIMEOUT_INVALID", "socket timeout must be from 0.05 through 5 seconds")
    if not 0.1 <= config.npc_timeout_s <= 120.0:
        _fail("TIMEOUT_INVALID", "NPC timeout must be from 0.1 through 120 seconds")
    if not 0.01 <= config.npc_poll_interval_s <= 2.0:
        _fail("TIMEOUT_INVALID", "NPC poll interval must be from 0.01 through 2 seconds")
    if config.npc_poll_interval_s > config.npc_timeout_s:
        _fail("TIMEOUT_INVALID", "NPC poll interval exceeds its deadline")
    if config.runtime_profile not in {None, R2_RUNTIME_PROFILE}:
        _fail("RUNTIME_PROFILE_INVALID", "runtime profile is not accepted")

    state_path = resolve_current_state_path(workspace)
    runtime_state = _validate_runtime_state(
        state_path,
        runtime_sha,
        workspace,
        runtime_profile=config.runtime_profile,
    )
    build_path = _canonical_existing_file(workspace / "result-receipt.json", "build result")
    _validate_build_result(
        build_path,
        build_sha,
        workspace=workspace,
        runtime_state=runtime_state,
    )
    _validate_source(repo, config.source_commit)
    return EvidenceBinding(
        workspace=workspace,
        runtime_state_path=state_path,
        runtime_state_sha256=runtime_sha,
        build_result_path=build_path,
        build_result_sha256=build_sha,
        repo_root=repo,
        source_commit=config.source_commit,
        map_path=runtime_state["map"],
        project_path=runtime_state["_project_path"],
        port=runtime_state["vista_world_port"],
        runtime_profile=runtime_state.get("runtime_profile"),
        camera_profile=runtime_state.get("camera_profile"),
        display=runtime_state.get("display"),
        gpu=runtime_state.get("gpu"),
        width=runtime_state.get("width"),
        height=runtime_state.get("height"),
        fps=runtime_state.get("fps"),
        launch_plan_path=runtime_state.get("_launch_plan_path"),
        launch_plan_sha256=runtime_state.get("launch_plan_sha256"),
    )


def assert_binding_stable(binding: EvidenceBinding) -> None:
    if resolve_current_state_path(binding.workspace) != binding.runtime_state_path:
        _fail("RUNTIME_POINTER_CHANGED", "current runtime changed during acceptance")
    if sha256_file(binding.runtime_state_path) != binding.runtime_state_sha256:
        _fail("RUNTIME_STATE_CHANGED", "runtime-state changed during acceptance")
    if sha256_file(binding.build_result_path) != binding.build_result_sha256:
        _fail("BUILD_RESULT_CHANGED", "build-result changed during acceptance")
    if binding.launch_plan_path is not None and (
        binding.launch_plan_sha256 is None
        or sha256_file(binding.launch_plan_path) != binding.launch_plan_sha256
    ):
        _fail("RUNTIME_LAUNCH_PLAN_CHANGED", "r2 launch plan changed during acceptance")
    _validate_source(binding.repo_root, binding.source_commit)


def exchange_loopback(request: Mapping[str, Any], timeout: float, *, port: int) -> Any:
    if not _is_int(port) or not 1024 <= port <= 65535:
        _fail("PORT_INVALID", "typed runtime port is invalid")
    if not 0.05 <= timeout <= 5.0:
        _fail("TIMEOUT_INVALID", "typed runtime timeout is invalid")
    encoded = _canonical_json_bytes(request)
    if len(encoded) > TYPED_RESPONSE_MAX_BYTES:
        _fail("REQUEST_TOO_LARGE", "typed request exceeded 64 KiB")
    response = bytearray()
    deadline = time.monotonic() + timeout
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _fail("RUNTIME_TIMEOUT", "typed runtime connection exceeded its deadline")
        with socket.create_connection(
            (LOOPBACK_HOST, port), timeout=remaining
        ) as connection:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _fail("RUNTIME_TIMEOUT", "typed runtime connection exceeded its deadline")
            connection.settimeout(remaining)
            connection.sendall(encoded)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _fail("RUNTIME_TIMEOUT", "typed runtime response exceeded its deadline")
                connection.settimeout(remaining)
                block = connection.recv(
                    min(8192, TYPED_RESPONSE_MAX_BYTES + 1 - len(response))
                )
                if not block:
                    break
                response.extend(block)
                if len(response) > TYPED_RESPONSE_MAX_BYTES:
                    _fail("RESPONSE_TOO_LARGE", "typed response exceeded 64 KiB")
    except (socket.timeout, TimeoutError) as exc:
        raise AcceptanceError("RUNTIME_TIMEOUT", "typed runtime response timed out") from exc
    except AcceptanceError:
        raise
    except OSError as exc:
        raise AcceptanceError("RUNTIME_CONNECTION_FAILED", "typed runtime connection failed") from exc
    return strict_json_bytes(bytes(response), label="typed runtime response")


def _command_id() -> str:
    return "vwc-" + os.urandom(12).hex()


def _finite_vector(value: Any, label: str) -> list[float]:
    if (
        not isinstance(value, list)
        or len(value) != 3
        or any(
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(float(item))
            for item in value
        )
    ):
        _fail("STATE_INVALID", f"{label} must be an exact finite XYZ vector")
    return [float(item) for item in value]


def npc_is_living_room_door_clear(location: Sequence[float]) -> bool:
    """Accept a navigable living-room point outside the complete door sweep."""

    return (
        len(location) >= 2
        and LIVING_CLEAR_X_RANGE_CM[0] <= location[0] <= LIVING_CLEAR_X_RANGE_CM[1]
        and LIVING_CLEAR_Y_RANGE_CM[0] <= location[1] <= LIVING_CLEAR_Y_RANGE_CM[1]
        and math.dist(location[:2], DOOR_LOCATION_XY) >= DOOR_CLEARANCE_RADIUS_CM
    )


def npc_is_in_office(location: Sequence[float]) -> bool:
    """Accept an NPC capsule center inside the declared office bounds."""

    return (
        len(location) >= 2
        and OFFICE_X_RANGE_CM[0] <= location[0] <= OFFICE_X_RANGE_CM[1]
        and OFFICE_Y_RANGE_CM[0] <= location[1] <= OFFICE_Y_RANGE_CM[1]
    )


def location_matches(
    location: Sequence[float], expected: Sequence[float], tolerance_cm: float
) -> bool:
    return (
        len(location) == len(expected) == 3
        and all(abs(float(actual) - float(target)) <= tolerance_cm
                for actual, target in zip(location, expected))
    )


def validate_state(value: Any, *, semantic_id: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != STATE_KEYS:
        _fail("STATE_INVALID", "runtime state fields differ")
    if (
        value.get("semantic_id") != semantic_id
        or not isinstance(value.get("hidden"), bool)
        or not isinstance(value.get("portable"), bool)
        or not isinstance(value.get("values"), dict)
        or not all(
            isinstance(key, str)
            and 0 < len(key) <= 80
            and isinstance(item, str)
            and len(item) <= 512
            for key, item in value["values"].items()
        )
    ):
        _fail("STATE_INVALID", "runtime state identity or scalar values differ")
    transform = value.get("transform")
    if not isinstance(transform, dict) or set(transform) != TRANSFORM_KEYS:
        _fail("STATE_INVALID", "runtime transform fields differ")
    _finite_vector(transform["location_cm"], "location_cm")
    _finite_vector(transform["rotation_deg"], "rotation_deg")
    _finite_vector(transform["scale"], "scale")
    return dict(value)


class ProtocolSession:
    def __init__(self, exchange: Exchange, socket_timeout_s: float):
        self.exchange = exchange
        self.socket_timeout_s = socket_timeout_s
        self.generation = 0
        self.initial_generation: int | None = None
        self.checks: list[dict[str, Any]] = []
        self.command_ids: set[str] = set()
        self.current_step: str | None = None

    def _request(self, step: str, params: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        self.current_step = step
        request = {"type": "vista_world_action", "params": dict(params)}
        try:
            raw = self.exchange(request, self.socket_timeout_s)
        except AcceptanceError as exc:
            if exc.step is None:
                exc.step = step
            raise
        except Exception as exc:
            raise AcceptanceError(
                "RUNTIME_EXCHANGE_FAILED", "typed runtime exchange failed", step=step
            ) from exc
        if not isinstance(raw, dict):
            _fail("RESPONSE_SHAPE_INVALID", "typed response must be an object", step=step)
        return request, dict(raw)

    def _new_command_id(self) -> str:
        command_id = _command_id()
        if not COMMAND_ID_RE.fullmatch(command_id) or command_id in self.command_ids:
            _fail("COMMAND_ID_INVALID", "could not allocate a unique command id")
        self.command_ids.add(command_id)
        return command_id

    def _record(
        self,
        step: str,
        *,
        mutation: bool,
        request: Mapping[str, Any],
        response: Mapping[str, Any],
        before: int,
    ) -> None:
        self.checks.append({
            "step": step,
            "mutation": mutation,
            "generation_before": before,
            "generation_after": self.generation,
            "request": dict(request),
            "response": dict(response),
        })

    def status(
        self,
        step: str,
        *,
        expected_event_status: str,
        expected_active_event: str | None,
    ) -> dict[str, Any]:
        command_id = self._new_command_id()
        before = self.generation
        request, response = self._request(
            step, {"operation": "status", "command_id": command_id}
        )
        if set(response) != STATUS_KEYS:
            _fail("RESPONSE_SHAPE_INVALID", "status response fields differ", step=step)
        if (
            response.get("command_id") != command_id
            or response.get("status") != "success"
            or response.get("code") != "READY"
            or response.get("world_revision") != DEFAULT_WORLD_REVISION
            or not _is_int(response.get("session_generation"))
            or response.get("session_generation") != before
            or response.get("event_status") != expected_event_status
            or response.get("active_event") != expected_active_event
        ):
            _fail("STATUS_MISMATCH", "authoritative status identity differs", step=step)
        if self.initial_generation is None:
            if response["session_generation"] != 0:
                _fail("INITIAL_GENERATION_MISMATCH", "fresh runtime did not begin at generation 0", step=step)
            self.initial_generation = 0
        self._record(step, mutation=False, request=request, response=response, before=before)
        return response

    def _validate_mutation_base(
        self,
        step: str,
        response: Mapping[str, Any],
        *,
        keys: frozenset[str],
        command_id: str,
        code: str,
        before: int,
    ) -> None:
        if set(response) != keys:
            _fail("RESPONSE_SHAPE_INVALID", "mutation response fields differ", step=step)
        if (
            response.get("command_id") != command_id
            or response.get("status") != "success"
            or response.get("code") != code
            or not _is_int(response.get("session_generation"))
        ):
            _fail("MUTATION_REJECTED", "typed mutation was not accepted exactly", step=step)
        if response["session_generation"] != before + 1:
            _fail("GENERATION_DRIFT", "successful mutation did not advance exactly one generation", step=step)

    def interaction(
        self,
        step: str,
        *,
        target: str,
        affordance: str,
        expected_code: str,
        placement_anchor: str | None = None,
    ) -> dict[str, Any]:
        command_id = self._new_command_id()
        before = self.generation
        params: dict[str, Any] = {
            "operation": "interaction",
            "command_id": command_id,
            "expected_revision": DEFAULT_WORLD_REVISION,
            "session_generation": before,
            "requester_semantic_id": PLAYER_ID,
            "target_semantic_id": target,
            "affordance": affordance,
        }
        if placement_anchor is not None:
            params["placement_anchor_semantic_id"] = placement_anchor
        request, response = self._request(step, params)
        self._validate_mutation_base(
            step,
            response,
            keys=INTERACTION_KEYS,
            command_id=command_id,
            code=expected_code,
            before=before,
        )
        if response.get("target_semantic_id") != target:
            _fail("TARGET_MISMATCH", "interaction target identity differs", step=step)
        state = validate_state(response.get("state"), semantic_id=target)
        self.generation = response["session_generation"]
        self._record(step, mutation=True, request=request, response=response, before=before)
        return state

    def npc_queue(self, step: str) -> None:
        command_id = self._new_command_id()
        before = self.generation
        params = {
            "operation": "npc_queue",
            "command_id": command_id,
            "expected_revision": DEFAULT_WORLD_REVISION,
            "session_generation": before,
            "npc_semantic_id": NPC_ID,
            "replace": True,
            "actions": [
                {
                    "action_id": "acceptance.navigate.keys",
                    "type": "navigate_to",
                    "target_semantic_id": KEYS_ID,
                    "timeout_sec": 20.0,
                },
                {
                    "action_id": "acceptance.pick_up.keys",
                    "type": "pick_up",
                    "target_semantic_id": KEYS_ID,
                    "timeout_sec": 10.0,
                },
                {
                    "action_id": "acceptance.navigate.office",
                    "type": "navigate_to",
                    "target_semantic_id": OFFICE_ANCHOR_ID,
                    "timeout_sec": 25.0,
                },
                {
                    "action_id": "acceptance.place.office_desk",
                    "type": "place",
                    "target_semantic_id": OFFICE_DESK_ANCHOR_ID,
                    "timeout_sec": 10.0,
                },
                {
                    "action_id": "acceptance.wait.office",
                    "type": "wait",
                    "duration_sec": 5.0,
                    "timeout_sec": 7.0,
                },
            ],
        }
        request, response = self._request(step, params)
        self._validate_mutation_base(
            step,
            response,
            keys=NPC_QUEUE_KEYS,
            command_id=command_id,
            code="QUEUE_REPLACED",
            before=before,
        )
        if response.get("target_semantic_id") != NPC_ID:
            _fail("TARGET_MISMATCH", "NPC queue target identity differs", step=step)
        self.generation = response["session_generation"]
        self._record(step, mutation=True, request=request, response=response, before=before)

    def event(self, step: str, *, operation: str, event_id: str | None, code: str) -> None:
        command_id = self._new_command_id()
        before = self.generation
        params: dict[str, Any] = {
            "operation": "event",
            "command_id": command_id,
            "expected_revision": DEFAULT_WORLD_REVISION,
            "session_generation": before,
            "event_operation": operation,
        }
        if event_id is not None:
            params["event_id"] = event_id
        request, response = self._request(step, params)
        self._validate_mutation_base(
            step,
            response,
            keys=EVENT_KEYS,
            command_id=command_id,
            code=code,
            before=before,
        )
        self.generation = response["session_generation"]
        self._record(step, mutation=True, request=request, response=response, before=before)


def run_protocol(
    port: int,
    *,
    socket_timeout_s: float = 1.0,
    npc_timeout_s: float = 30.0,
    npc_poll_interval_s: float = 0.25,
    exchange: Exchange | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    _session: ProtocolSession | None = None,
) -> ProtocolSession:
    if _session is None:
        if exchange is None:
            exchange = lambda request, timeout: exchange_loopback(  # noqa: E731
                request, timeout, port=port
            )
        session = ProtocolSession(exchange, socket_timeout_s)
    else:
        if exchange is not None:
            _fail("SESSION_INVALID", "existing protocol session cannot replace its exchange")
        session = _session
    session.status(
        "status.g0", expected_event_status="inactive", expected_active_event=None
    )

    door = session.interaction(
        "door.open", target=DOOR_ID, affordance="open", expected_code="DOOR_OPENED"
    )
    if door["values"].get("open") != "true":
        _fail("DOOR_STATE_MISMATCH", "door open mutation did not report open=true", step="door.open")
    door = session.interaction(
        "door.inspect_open",
        target=DOOR_ID,
        affordance="inspect",
        expected_code="INSPECTED",
    )
    if door["values"].get("open") != "true":
        _fail("DOOR_STATE_MISMATCH", "door inspection did not preserve open=true", step="door.inspect_open")

    office_door = session.interaction(
        "office_door.inspect_initial_open",
        target=OFFICE_DOOR_ID,
        affordance="inspect",
        expected_code="INSPECTED",
    )
    if office_door["values"].get("open") != "true":
        _fail(
            "DOOR_STATE_MISMATCH",
            "office door did not begin open for the NPC office route",
            step="office_door.inspect_initial_open",
        )

    keys = session.interaction(
        "keys.pick_up", target=KEYS_ID, affordance="pick_up", expected_code="ITEM_PICKED_UP"
    )
    if (
        keys.get("portable") is not True
        or keys["values"].get("held") != "true"
        or keys["values"].get("held_by") != PLAYER_ID
        or keys["values"].get("placed_at", "") != ""
    ):
        _fail("KEYS_STATE_MISMATCH", "keys were not authoritatively held by the player", step="keys.pick_up")
    keys = session.interaction(
        "keys.inspect_held",
        target=KEYS_ID,
        affordance="inspect",
        expected_code="INSPECTED",
    )
    if keys["values"].get("held") != "true" or keys["values"].get("held_by") != PLAYER_ID:
        _fail("KEYS_STATE_MISMATCH", "held keys inspection identity differs", step="keys.inspect_held")
    keys = session.interaction(
        "keys.place_tabletop_right",
        target=KEYS_ID,
        affordance="place",
        expected_code="ITEM_PLACED",
        placement_anchor=TABLETOP_RIGHT_ID,
    )
    tabletop_location = _finite_vector(
        keys["transform"]["location_cm"], "keys tabletop location_cm"
    )
    if (
        keys["values"].get("held") != "false"
        or keys["values"].get("held_by") != ""
        or keys["values"].get("placed_at") != TABLETOP_RIGHT_ID
        or not location_matches(
            tabletop_location, TABLETOP_RIGHT_LOCATION_CM, PLACEMENT_TOLERANCE_CM
        )
    ):
        _fail(
            "KEYS_STATE_MISMATCH",
            "player placement did not bind the exact tabletop-right semantic anchor",
            step="keys.place_tabletop_right",
        )

    npc_before = session.interaction(
        "npc.preinspect",
        target=NPC_ID,
        affordance="inspect",
        expected_code="NPC_INSPECTED",
    )
    before_location = _finite_vector(
        npc_before["transform"]["location_cm"], "NPC baseline location_cm"
    )
    if npc_is_living_room_door_clear(before_location) or npc_is_in_office(before_location):
        _fail(
            "NPC_BASELINE_INVALID",
            "NPC preinspection was already in a destination acceptance region",
            step="npc.preinspect",
        )
    session.npc_queue("npc.replace_queue")

    deadline = monotonic() + npc_timeout_s
    max_polls = max(1, math.ceil(npc_timeout_s / npc_poll_interval_s) + 1)
    observed_carried = False
    reached_office = False
    placed_in_office = False
    for index in range(1, max_polls + 1):
        if monotonic() > deadline:
            break
        state = session.interaction(
            f"npc.inspect_poll.{index}",
            target=NPC_ID,
            affordance="inspect",
            expected_code="NPC_INSPECTED",
        )
        location = _finite_vector(state["transform"]["location_cm"], "NPC location_cm")
        reached_office = reached_office or npc_is_in_office(location)
        keys = session.interaction(
            f"keys.inspect_cross_room_poll.{index}",
            target=KEYS_ID,
            affordance="inspect",
            expected_code="INSPECTED",
        )
        held_by = keys["values"].get("held_by")
        observed_carried = observed_carried or (
            keys["values"].get("held") == "true" and held_by == NPC_ID
        )
        keys_location = _finite_vector(
            keys["transform"]["location_cm"], "keys cross-room location_cm"
        )
        placed_in_office = (
            reached_office
            and keys["values"].get("held") == "false"
            and held_by == ""
            and keys["values"].get("placed_at") == OFFICE_DESK_ANCHOR_ID
            and location_matches(
                keys_location, OFFICE_DESK_LOCATION_CM, PLACEMENT_TOLERANCE_CM
            )
        )
        if observed_carried and placed_in_office:
            break
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        sleep(min(npc_poll_interval_s, remaining))
    if not observed_carried:
        _fail(
            "NPC_CARRY_NOT_OBSERVED",
            "keys were never authoritatively observed as held by the moving NPC",
            step="keys.inspect_cross_room_poll",
        )
    if not reached_office or not placed_in_office:
        _fail(
            "NPC_DESTINATION_TIMEOUT",
            "NPC did not carry and place the keys across the portal into the office before its deadline",
            step="npc.inspect_poll",
        )

    office_door = session.interaction(
        "office_door.close_after_crossing",
        target=OFFICE_DOOR_ID,
        affordance="close",
        expected_code="DOOR_CLOSED",
    )
    if office_door["values"].get("open") != "false":
        _fail(
            "DOOR_STATE_MISMATCH",
            "office door did not close after NPC crossing",
            step="office_door.close_after_crossing",
        )
    office_door = session.interaction(
        "office_door.inspect_closed_after_crossing",
        target=OFFICE_DOOR_ID,
        affordance="inspect",
        expected_code="INSPECTED",
    )
    if office_door["values"].get("open") != "false":
        _fail(
            "DOOR_STATE_MISMATCH",
            "office door inspection did not preserve the post-crossing close",
            step="office_door.inspect_closed_after_crossing",
        )

    for event_id in EVENT_IDS:
        session.event(
            f"event.{event_id}.start",
            operation="start_event",
            event_id=event_id,
            code="EVENT_STARTED",
        )
        session.status(
            f"event.{event_id}.status_active",
            expected_event_status="active",
            expected_active_event=event_id,
        )
        session.event(
            f"event.{event_id}.reset",
            operation="reset_event",
            event_id=None,
            code="EVENT_RESET",
        )
        session.status(
            f"event.{event_id}.status_inactive",
            expected_event_status="inactive",
            expected_active_event=None,
        )
    return session


def execute_acceptance(
    config: AcceptanceConfig,
    *,
    exchange: Exchange | None = None,
) -> tuple[int, dict[str, Any]]:
    workspace = _canonical_existing_directory(config.workspace, "workspace")
    writer = ExclusiveReceipt.reserve(workspace, config.output)
    created_at = utc_now()
    binding: EvidenceBinding | None = None
    session: ProtocolSession | None = None
    failure: BaseException | None = None
    try:
        binding = validate_binding(config)
        resolved_exchange = exchange
        if resolved_exchange is None:
            resolved_exchange = lambda request, timeout: exchange_loopback(  # noqa: E731
                request, timeout, port=binding.port
            )
        session = ProtocolSession(resolved_exchange, config.socket_timeout_s)
        session = run_protocol(
            binding.port,
            socket_timeout_s=config.socket_timeout_s,
            npc_timeout_s=config.npc_timeout_s,
            npc_poll_interval_s=config.npc_poll_interval_s,
            _session=session,
        )
        assert_binding_stable(binding)
    except BaseException as exc:  # the reserved receipt must record every attempted run
        failure = exc

    error: dict[str, Any] | None = None
    status = "accepted"
    if failure is not None:
        status = "failed"
        if isinstance(failure, AcceptanceError):
            error = {
                "type": type(failure).__name__,
                "code": failure.code,
                "message": str(failure)[:512],
                "step": failure.step,
            }
        else:
            error = {
                "type": type(failure).__name__,
                "code": "ACCEPTANCE_UNEXPECTED",
                "message": str(failure)[:512],
                "step": session.current_step if session is not None else None,
            }
    receipt: dict[str, Any] = {
        "schema": (
            R2_RECEIPT_SCHEMA
            if config.runtime_profile == R2_RUNTIME_PROFILE
            else RECEIPT_SCHEMA
        ),
        "status": status,
        "created_at": created_at,
        "completed_at": utc_now(),
        "output": str(writer.path),
        "bindings": binding.receipt_value() if binding is not None else None,
        "initial_generation": session.initial_generation if session is not None else None,
        "final_generation": session.generation if session is not None else None,
        "checks": session.checks if session is not None else [],
        "error": error,
    }
    writer.write(receipt)
    return (0 if failure is None else 1), receipt


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--workspace", required=True, type=Path)
    result.add_argument("--repo-root", required=True, type=Path)
    result.add_argument("--output", required=True, type=Path)
    result.add_argument("--runtime-state-sha256", required=True)
    result.add_argument("--build-result-sha256", required=True)
    result.add_argument("--source-commit", required=True)
    result.add_argument("--socket-timeout-s", type=float, default=1.0)
    result.add_argument("--npc-timeout-s", type=float, default=30.0)
    result.add_argument("--npc-poll-interval-s", type=float, default=0.25)
    result.add_argument(
        "--runtime-profile",
        choices=[R2_RUNTIME_PROFILE],
        default=None,
    )
    return result


def config_from_args(args: argparse.Namespace) -> AcceptanceConfig:
    return AcceptanceConfig(
        workspace=args.workspace,
        repo_root=args.repo_root,
        output=args.output,
        runtime_state_sha256=args.runtime_state_sha256,
        build_result_sha256=args.build_result_sha256,
        source_commit=args.source_commit,
        socket_timeout_s=args.socket_timeout_s,
        npc_timeout_s=args.npc_timeout_s,
        npc_poll_interval_s=args.npc_poll_interval_s,
        runtime_profile=getattr(args, "runtime_profile", None),
    )


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        code, receipt = execute_acceptance(config_from_args(args))
    except AcceptanceError as exc:
        print(f"runtime acceptance refused before receipt reservation: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"status": receipt["status"], "receipt": receipt["output"]}, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
