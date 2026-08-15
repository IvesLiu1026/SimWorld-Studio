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
        TYPED_RESPONSE_MAX_BYTES,
        identity_is_live,
    )
else:
    from .runtime import (
        DEFAULT_VISTA_WORLD_PORT,
        DEFAULT_WORLD_REVISION,
        TYPED_RESPONSE_MAX_BYTES,
        identity_is_live,
    )


RECEIPT_SCHEMA = "simworld.vista.playable-home-runtime-acceptance/v1"
RUNTIME_POINTER_SCHEMA = "simworld.vista.playable-home-runtime-pointer/v1"
RUNTIME_STATE_SCHEMA = "simworld.vista.playable-home-runtime-state/v1"
BUILD_RESULT_SCHEMA = "simworld.vista.playable-home-ue-build-result/v1"
LOOPBACK_HOST = "127.0.0.1"

PLAYER_ID = "home.r1/player.01"
DOOR_ID = "home.r1/room.entry_hall/entity.interior_door.01"
NPC_ID = "home.r1/room.entry_hall/entity.resident.01"
LIVING_ANCHOR_ID = "home.r1/room.living_room/anchor.room_center"
KEYS_ID = "home.r1/room.living_room/entity.keys.01"
TABLETOP_RIGHT_ID = (
    "home.r1/room.living_room/entity.coffee_table.01/anchor.tabletop_right"
)
EVENT_IDS = ("mmg_001", "mmg_044", "mmg_045")
LIVING_TARGET_XY = (-400.0, -200.0)
LIVING_ACCEPTANCE_RADIUS_CM = 80.0

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

    def receipt_value(self) -> dict[str, Any]:
        return {
            "workspace": str(self.workspace),
            "runtime_state": str(self.runtime_state_path),
            "runtime_state_sha256": self.runtime_state_sha256,
            "build_result": str(self.build_result_path),
            "build_result_sha256": self.build_result_sha256,
            "repo_root": str(self.repo_root),
            "source_commit": self.source_commit,
            "source_clean": True,
            "host": LOOPBACK_HOST,
            "port": DEFAULT_VISTA_WORLD_PORT,
            "world_revision": DEFAULT_WORLD_REVISION,
            "map_path": self.map_path,
            "project": str(self.project_path),
        }


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


def _validate_runtime_state(path: Path, expected_sha: str, workspace: Path) -> dict[str, Any]:
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
    if not isinstance(state, dict) or set(state) != required:
        _fail("RUNTIME_STATE_INVALID", "running runtime-state fields differ")
    if (
        state.get("schema") != RUNTIME_STATE_SCHEMA
        or state.get("status") != "running"
        or state.get("vista_world_port") != DEFAULT_VISTA_WORLD_PORT
        or not isinstance(state.get("created_at"), str)
        or not isinstance(state.get("updated_at"), str)
        or not isinstance(state.get("display"), str)
        or not _is_int(state.get("gpu"))
        or not isinstance(state.get("map"), str)
        or not state["map"].startswith("/Game/")
        or not isinstance(state.get("project"), str)
    ):
        _fail("RUNTIME_STATE_INVALID", "runtime-state identity is not accepted")
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
    if not isinstance(result, dict) or set(result) != required:
        _fail("BUILD_RESULT_INVALID", "accepted build-result fields differ")
    digests = (
        result.get("execution_sha256"),
        result.get("import_receipt_sha256"),
        result.get("scene_receipt_sha256"),
        result.get("content_digest"),
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

    state_path = resolve_current_state_path(workspace)
    runtime_state = _validate_runtime_state(state_path, runtime_sha, workspace)
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
    )


def assert_binding_stable(binding: EvidenceBinding) -> None:
    if resolve_current_state_path(binding.workspace) != binding.runtime_state_path:
        _fail("RUNTIME_POINTER_CHANGED", "current runtime changed during acceptance")
    if sha256_file(binding.runtime_state_path) != binding.runtime_state_sha256:
        _fail("RUNTIME_STATE_CHANGED", "runtime-state changed during acceptance")
    if sha256_file(binding.build_result_path) != binding.build_result_sha256:
        _fail("BUILD_RESULT_CHANGED", "build-result changed during acceptance")
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
        with socket.create_connection((LOOPBACK_HOST, port), timeout=timeout) as connection:
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
                    "action_id": "acceptance.navigate.living",
                    "type": "navigate_to",
                    "target_semantic_id": LIVING_ANCHOR_ID,
                    "timeout_sec": 20.0,
                },
                {
                    "action_id": "acceptance.wait.living",
                    "type": "wait",
                    "duration_sec": 10.0,
                    "timeout_sec": 12.0,
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

    npc_before = session.interaction(
        "npc.preinspect",
        target=NPC_ID,
        affordance="inspect",
        expected_code="NPC_INSPECTED",
    )
    before_location = _finite_vector(
        npc_before["transform"]["location_cm"], "NPC baseline location_cm"
    )
    if math.dist(before_location[:2], LIVING_TARGET_XY) <= LIVING_ACCEPTANCE_RADIUS_CM:
        _fail(
            "NPC_BASELINE_INVALID",
            "NPC preinspection was already inside the living-room acceptance radius",
            step="npc.preinspect",
        )
    session.npc_queue("npc.replace_queue")

    deadline = monotonic() + npc_timeout_s
    max_polls = max(1, math.ceil(npc_timeout_s / npc_poll_interval_s) + 1)
    reached = False
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
        distance = math.dist(location[:2], LIVING_TARGET_XY)
        if distance <= LIVING_ACCEPTANCE_RADIUS_CM:
            reached = True
            break
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        sleep(min(npc_poll_interval_s, remaining))
    if not reached:
        _fail(
            "NPC_DESTINATION_TIMEOUT",
            "NPC did not reach the living-room acceptance radius before its deadline",
            step="npc.inspect_poll",
        )

    door = session.interaction(
        "door.close", target=DOOR_ID, affordance="close", expected_code="DOOR_CLOSED"
    )
    if door["values"].get("open") != "false":
        _fail("DOOR_STATE_MISMATCH", "door close mutation did not report open=false", step="door.close")

    keys = session.interaction(
        "keys.pick_up", target=KEYS_ID, affordance="pick_up", expected_code="ITEM_PICKED_UP"
    )
    if (
        keys.get("portable") is not True
        or keys["values"].get("held") != "true"
        or keys["values"].get("held_by") != PLAYER_ID
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
    if keys["values"].get("held") != "false" or keys["values"].get("held_by") != "":
        _fail("KEYS_STATE_MISMATCH", "placed keys still report a carrier", step="keys.place_tabletop_right")

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
                request, timeout, port=DEFAULT_VISTA_WORLD_PORT
            )
        session = ProtocolSession(resolved_exchange, config.socket_timeout_s)
        session = run_protocol(
            DEFAULT_VISTA_WORLD_PORT,
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
        "schema": RECEIPT_SCHEMA,
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
