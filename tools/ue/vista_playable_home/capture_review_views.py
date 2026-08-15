#!/usr/bin/env python3
"""Capture the six fixed VISTA Playable Home review cameras in Unreal Editor.

The host process validates a SHA-256-pinned build plan and an already
materialized UE project, creates a fresh append-only output attempt, then
launches a regular X11 ``UnrealEditor`` process.  The same byte-pinned file is
executed inside Unreal; callers cannot supply Python source or a script path.

Inside Unreal, the worker resolves the six materialized ``CameraActor``
instances by their stable ``VistaSemanticId`` tags.  A Slate post-tick state
machine moves the level viewport to each actor and waits for every asynchronous
high-resolution PNG before allowing the editor to exit.  The host independently
parses and unfilters each PNG, validates its exact dimensions and nonblank pixel
content, and writes the accepted receipt with ``O_EXCL``.

Normal invocations are validation-only.  Add ``--apply`` to launch Unreal::

    uv run --offline --project tools python \
      tools/ue/vista_playable_home/capture_review_views.py \
      --attempt-root /abs/path/to/ue/attempt-10 \
      --project /abs/path/to/ue/attempt-10/project/VistaPlayableHome.uproject \
      --build-plan /abs/path/to/ue/attempt-10/contracts/build-plan.json \
      --build-plan-sha256 <sha256> \
      --map-path /Game/VISTA/PlayableHome/vista_playable_home_r1/Maps/VistaPlayableHome \
      --unreal-editor /abs/path/to/Engine/Binaries/Linux/UnrealEditor \
      --output-dir /abs/path/to/ue/attempt-10/review-cameras/attempt-01 \
      --display :117 --graphics-adapter 0 --apply
"""

from __future__ import annotations

import argparse
import binascii
import hashlib
import json
import math
import os
import pathlib
import re
import signal
import stat
import struct
import subprocess
import sys
import time
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


Path = pathlib.Path
BUILD_PLAN_SCHEMA = "simworld.vista.playable-home-build-plan/v1"
EXECUTION_SCHEMA = "simworld.vista.playable-home-review-capture-execution/v1"
UE_RESULT_SCHEMA = "simworld.vista.playable-home-review-capture-ue-result/v1"
RECEIPT_SCHEMA = "simworld.vista.playable-home-review-capture-receipt/v1"
EXPECTED_REVISION = "vista_playable_home_r1"
EXPECTED_HOUSE_ID = "home.r1"
EXPECTED_MAP_PATH = (
    "/Game/VISTA/PlayableHome/vista_playable_home_r1/Maps/VistaPlayableHome"
)
EXPECTED_PROJECT_NAME = "VistaPlayableHome.uproject"
WIDTH = 1280
HEIGHT = 720
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_PNG_BYTES = 128 * 1024 * 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DISPLAY_RE = re.compile(r"^:[0-9]{1,5}(?:\.[0-9]{1,3})?$")
ATTEMPT_RE = re.compile(r"^attempt-[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
EXECUTION_ENV = "VISTA_PLAYABLE_HOME_REVIEW_EXECUTION"
EXECUTION_SHA_ENV = "VISTA_PLAYABLE_HOME_REVIEW_EXECUTION_SHA256"
WORKER_ENV = "VISTA_PLAYABLE_HOME_REVIEW_WORKER"
EXECUTION_FILE = "execution.json"
UE_RESULT_FILE = "ue-result.json"
RECEIPT_FILE = "review-capture-receipt.json"
EDITOR_LOG_FILE = "unreal-editor.log"
EDITOR_STDOUT_FILE = "unreal-editor-stdout.log"
IMAGES_DIR = "images"

# This is deliberately a fixed r1 evidence surface, not a generic camera or
# Python execution API.  The order is also the receipt/capture order.
FIXED_REVIEW_CAMERAS: tuple[tuple[str, str, str], ...] = (
    ("entry_hall", "home.r1/room.entry_hall", "entry_overview"),
    ("living_room", "home.r1/room.living_room", "living_overview"),
    ("kitchen_dining", "home.r1/room.kitchen_dining", "kitchen_overview"),
    ("bedroom", "home.r1/room.bedroom", "bedroom_overview"),
    ("office", "home.r1/room.office", "office_overview"),
    (
        "bathroom_laundry",
        "home.r1/room.bathroom_laundry",
        "bathroom_overview",
    ),
)


class ReviewCaptureError(RuntimeError):
    """Stable fail-closed error for host validation or capture rejection."""

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
    raise ReviewCaptureError(code, detail, pointer=pointer)


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
        _fail("VISTA_HOME_REVIEW_JSON_INVALID", "value is not canonical finite JSON")
        raise AssertionError from exc


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            _fail("VISTA_HOME_REVIEW_JSON_DUPLICATE_KEY", "JSON has a duplicate key")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    _fail("VISTA_HOME_REVIEW_JSON_NON_FINITE", f"JSON constant {value!r} is forbidden")


def _assert_finite(value: Any, pointer: str = "$", depth: int = 0) -> None:
    if depth > 96:
        _fail("VISTA_HOME_REVIEW_JSON_INVALID", "JSON nesting exceeds safety bound", pointer=pointer)
    if isinstance(value, float) and not math.isfinite(value):
        _fail("VISTA_HOME_REVIEW_JSON_NON_FINITE", "JSON number is not finite", pointer=pointer)
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                _fail("VISTA_HOME_REVIEW_JSON_INVALID", "JSON key is not a string", pointer=pointer)
            _assert_finite(child, f"{pointer}.{key}", depth + 1)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_finite(child, f"{pointer}[{index}]", depth + 1)


def _load_json(path: Path, *, label: str, expected_sha256: str | None = None) -> tuple[dict[str, Any], bytes]:
    source = _existing_file(path, label)
    size = source.stat().st_size
    if size <= 0 or size > MAX_JSON_BYTES:
        _fail("VISTA_HOME_REVIEW_JSON_INVALID", f"{label} size is outside safety bound", pointer=str(source))
    raw = source.read_bytes()
    if expected_sha256 is not None:
        if SHA256_RE.fullmatch(expected_sha256) is None:
            _fail("VISTA_HOME_REVIEW_PIN_INVALID", f"{label} pin is not a lowercase SHA-256")
        if sha256_bytes(raw) != expected_sha256:
            _fail("VISTA_HOME_REVIEW_PIN_MISMATCH", f"{label} SHA-256 differs", pointer=str(source))
    try:
        value = json.loads(
            raw.decode("utf-8", "strict"),
            object_pairs_hook=_duplicate_object,
            parse_constant=_reject_constant,
        )
    except ReviewCaptureError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        _fail("VISTA_HOME_REVIEW_JSON_INVALID", f"{label} is not strict UTF-8 JSON", pointer=str(source))
        raise AssertionError from exc
    if not isinstance(value, dict):
        _fail("VISTA_HOME_REVIEW_JSON_INVALID", f"{label} root is not an object", pointer=str(source))
    _assert_finite(value)
    return value, raw


def _absolute_lexical(path: Path, label: str) -> Path:
    candidate = Path(path).expanduser()
    text = str(candidate)
    if not candidate.is_absolute() or os.path.normpath(text) != text:
        _fail("VISTA_HOME_REVIEW_PATH_INVALID", f"{label} must be absolute and normalized", pointer=text)
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
            _fail("VISTA_HOME_REVIEW_PATH_MISSING", f"{label} is missing", pointer=str(candidate))
        if stat.S_ISLNK(metadata.st_mode):
            _fail("VISTA_HOME_REVIEW_SYMLINK_REJECTED", f"{label} contains a symlink", pointer=str(current))


def _existing_file(path: Path, label: str, *, executable: bool = False) -> Path:
    candidate = _absolute_lexical(path, label)
    _reject_symlink_components(candidate, label)
    try:
        metadata = os.lstat(candidate)
    except OSError as exc:
        _fail("VISTA_HOME_REVIEW_PATH_MISSING", f"{label} is missing", pointer=str(candidate))
        raise AssertionError from exc
    if not stat.S_ISREG(metadata.st_mode) or candidate.resolve(strict=True) != candidate:
        _fail("VISTA_HOME_REVIEW_PATH_INVALID", f"{label} is not a canonical regular file", pointer=str(candidate))
    if executable and not os.access(candidate, os.X_OK):
        _fail("VISTA_HOME_REVIEW_PATH_INVALID", f"{label} is not executable", pointer=str(candidate))
    return candidate


def _existing_directory(path: Path, label: str) -> Path:
    candidate = _absolute_lexical(path, label)
    _reject_symlink_components(candidate, label)
    try:
        metadata = os.lstat(candidate)
    except OSError as exc:
        _fail("VISTA_HOME_REVIEW_PATH_MISSING", f"{label} is missing", pointer=str(candidate))
        raise AssertionError from exc
    if not stat.S_ISDIR(metadata.st_mode) or candidate.resolve(strict=True) != candidate:
        _fail("VISTA_HOME_REVIEW_PATH_INVALID", f"{label} is not a canonical directory", pointer=str(candidate))
    return candidate


def _require_child(path: Path, root: Path, label: str, *, strict: bool = True) -> Path:
    try:
        relative = path.relative_to(root)
    except ValueError:
        _fail("VISTA_HOME_REVIEW_PATH_ESCAPE", f"{label} escapes attempt root", pointer=str(path))
    if strict and not relative.parts:
        _fail("VISTA_HOME_REVIEW_PATH_ESCAPE", f"{label} must be below attempt root", pointer=str(path))
    return path


def _write_exclusive(path: Path, raw: bytes) -> None:
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
        with os.fdopen(descriptor, "wb", closefd=False) as target:
            target.write(raw)
            target.flush()
            os.fsync(target.fileno())
    finally:
        os.close(descriptor)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _v3(value: Any, label: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 3 or any(
        isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(float(item))
        for item in value
    ):
        _fail("VISTA_HOME_REVIEW_PLAN_INVALID", f"{label} is not a finite three-vector")
    return [float(item) for item in value]


def _transform(value: Any, label: str) -> dict[str, list[float]]:
    if not isinstance(value, Mapping) or set(value) != {"location_cm", "rotation_deg", "scale"}:
        _fail("VISTA_HOME_REVIEW_PLAN_INVALID", f"{label} transform fields differ")
    return {
        "location_cm": _v3(value["location_cm"], f"{label}.location_cm"),
        "rotation_deg": _v3(value["rotation_deg"], f"{label}.rotation_deg"),
        "scale": _v3(value["scale"], f"{label}.scale"),
    }


def compile_fixed_cameras(plan: Mapping[str, Any], map_path: str) -> list[dict[str, Any]]:
    """Validate r1 and derive exactly one fixed materialized camera per room."""

    if plan.get("schema_version") != BUILD_PLAN_SCHEMA:
        _fail("VISTA_HOME_REVIEW_PLAN_INVALID", "build plan schema differs")
    house = plan.get("house")
    if not isinstance(house, Mapping) or house.get("house_id") != EXPECTED_HOUSE_ID or house.get("revision") != EXPECTED_REVISION:
        _fail("VISTA_HOME_REVIEW_PLAN_INVALID", "build plan is not VISTA Playable Home r1")
    digest = plan.get("content_digest")
    if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
        _fail("VISTA_HOME_REVIEW_PLAN_INVALID", "build plan content digest is invalid")
    unreal_plan = plan.get("unreal")
    if not isinstance(unreal_plan, Mapping) or unreal_plan.get("map_path") != EXPECTED_MAP_PATH:
        _fail("VISTA_HOME_REVIEW_MAP_MISMATCH", "build plan map is not the fixed r1 map")
    if map_path != EXPECTED_MAP_PATH or map_path != unreal_plan.get("map_path"):
        _fail("VISTA_HOME_REVIEW_MAP_MISMATCH", "requested map differs from pinned r1 map")
    rooms = plan.get("rooms")
    if not isinstance(rooms, list) or len(rooms) != len(FIXED_REVIEW_CAMERAS):
        _fail("VISTA_HOME_REVIEW_ROOM_SET_INVALID", "build plan does not have exactly six rooms")
    by_kind: dict[str, Mapping[str, Any]] = {}
    for index, room in enumerate(rooms):
        if not isinstance(room, Mapping) or not isinstance(room.get("kind"), str):
            _fail("VISTA_HOME_REVIEW_ROOM_SET_INVALID", f"rooms[{index}] is invalid")
        kind = room["kind"]
        if kind in by_kind:
            _fail("VISTA_HOME_REVIEW_ROOM_SET_INVALID", f"room kind {kind!r} is duplicated")
        by_kind[kind] = room
    expected_kinds = {item[0] for item in FIXED_REVIEW_CAMERAS}
    if set(by_kind) != expected_kinds:
        _fail("VISTA_HOME_REVIEW_ROOM_SET_INVALID", "room kinds differ from the fixed six-room set")

    cameras: list[dict[str, Any]] = []
    for ordinal, (kind, room_id, camera_id) in enumerate(FIXED_REVIEW_CAMERAS, start=1):
        room = by_kind[kind]
        if room.get("room_id") != room_id:
            _fail("VISTA_HOME_REVIEW_ROOM_SET_INVALID", f"{kind} semantic room ID differs")
        review_cameras = room.get("review_cameras")
        if not isinstance(review_cameras, list) or len(review_cameras) != 1:
            _fail("VISTA_HOME_REVIEW_CAMERA_SET_INVALID", f"{kind} must have exactly one review camera")
        camera = review_cameras[0]
        if not isinstance(camera, Mapping) or camera.get("camera_id") != camera_id:
            _fail("VISTA_HOME_REVIEW_CAMERA_SET_INVALID", f"{kind} review camera ID differs")
        fov = camera.get("fov_deg")
        if isinstance(fov, bool) or not isinstance(fov, (int, float)) or not math.isfinite(float(fov)) or not 5.0 <= float(fov) <= 170.0:
            _fail("VISTA_HOME_REVIEW_CAMERA_SET_INVALID", f"{kind} review camera FOV is invalid")
        semantic_id = f"{room_id}/camera.{camera_id}"
        cameras.append(
            {
                "ordinal": ordinal,
                "room_kind": kind,
                "room_id": room_id,
                "camera_id": camera_id,
                "semantic_id": semantic_id,
                "semantic_tag": f"VistaSemanticId={semantic_id}",
                "expected_transform": _transform(camera.get("world_transform_cm"), f"{kind}.review_camera"),
                "expected_fov_deg": float(fov),
                "relative_path": f"{IMAGES_DIR}/{ordinal:02d}-{kind}-{camera_id}.png",
            }
        )
    return cameras


def _validate_project(path: Path) -> tuple[dict[str, Any], str]:
    if path.name != EXPECTED_PROJECT_NAME:
        _fail("VISTA_HOME_REVIEW_PROJECT_INVALID", "project filename differs", pointer=str(path))
    value, raw = _load_json(path, label="project")
    plugins = value.get("Plugins")
    if not isinstance(plugins, list):
        _fail("VISTA_HOME_REVIEW_PROJECT_INVALID", "project Plugins array is missing")
    enabled = {
        item.get("Name")
        for item in plugins
        if isinstance(item, Mapping) and item.get("Enabled") is True and isinstance(item.get("Name"), str)
    }
    required = {"VistaPlayableHome", "PythonScriptPlugin", "EditorScriptingUtilities"}
    if not required.issubset(enabled):
        _fail("VISTA_HOME_REVIEW_PROJECT_INVALID", "required fixed capture plugins are not enabled")
    return value, sha256_bytes(raw)


@dataclass(frozen=True)
class CaptureInputs:
    attempt_root: Path
    project: Path
    project_sha256: str
    build_plan: Path
    build_plan_sha256: str
    plan: dict[str, Any]
    map_path: str
    unreal_editor: Path
    output_dir: Path
    display: str
    graphics_adapter: int
    timeout_seconds: int
    script: Path
    script_sha256: str
    cameras: tuple[dict[str, Any], ...]


def validate_inputs(args: argparse.Namespace) -> CaptureInputs:
    attempt_root = _existing_directory(Path(args.attempt_root), "attempt root")
    project = _require_child(_existing_file(Path(args.project), "project"), attempt_root, "project")
    _, project_sha = _validate_project(project)
    build_plan = _require_child(_existing_file(Path(args.build_plan), "build plan"), attempt_root, "build plan")
    if SHA256_RE.fullmatch(args.build_plan_sha256 or "") is None:
        _fail("VISTA_HOME_REVIEW_PIN_INVALID", "build plan pin must be a lowercase SHA-256")
    plan, raw = _load_json(build_plan, label="build plan", expected_sha256=args.build_plan_sha256)
    plan_sha = sha256_bytes(raw)
    cameras = compile_fixed_cameras(plan, args.map_path)
    unreal_editor = _existing_file(Path(args.unreal_editor), "UnrealEditor", executable=True)
    if unreal_editor.name != "UnrealEditor":
        _fail("VISTA_HOME_REVIEW_ENGINE_INVALID", "engine executable must be UnrealEditor")
    if DISPLAY_RE.fullmatch(args.display or "") is None:
        _fail("VISTA_HOME_REVIEW_DISPLAY_INVALID", "DISPLAY must be a local X11 display such as :117")
    if isinstance(args.graphics_adapter, bool) or not 0 <= args.graphics_adapter <= 31:
        _fail("VISTA_HOME_REVIEW_ENGINE_INVALID", "graphics adapter must be between 0 and 31")
    if isinstance(args.timeout_seconds, bool) or not 60 <= args.timeout_seconds <= 900:
        _fail("VISTA_HOME_REVIEW_TIMEOUT_INVALID", "timeout must be between 60 and 900 seconds")
    output_dir = _absolute_lexical(Path(args.output_dir), "output directory")
    _reject_symlink_components(output_dir, "output directory", allow_missing_tail=True)
    _require_child(output_dir, attempt_root, "output directory")
    if ATTEMPT_RE.fullmatch(output_dir.name) is None:
        _fail("VISTA_HOME_REVIEW_OUTPUT_INVALID", "output directory basename must be attempt-<id>")
    parent = _existing_directory(output_dir.parent, "output parent")
    _require_child(parent, attempt_root, "output parent", strict=False)
    if output_dir.exists():
        _fail("VISTA_HOME_REVIEW_OUTPUT_EXISTS", "append-only output attempt already exists", pointer=str(output_dir))
    script = _existing_file(Path(__file__).resolve(strict=True), "fixed review capture script")
    return CaptureInputs(
        attempt_root=attempt_root,
        project=project,
        project_sha256=project_sha,
        build_plan=build_plan,
        build_plan_sha256=plan_sha,
        plan=plan,
        map_path=args.map_path,
        unreal_editor=unreal_editor,
        output_dir=output_dir,
        display=args.display,
        graphics_adapter=args.graphics_adapter,
        timeout_seconds=args.timeout_seconds,
        script=script,
        script_sha256=sha256_file(script),
        cameras=tuple(cameras),
    )


def build_execution(inputs: CaptureInputs) -> dict[str, Any]:
    output = inputs.output_dir
    return {
        "schema_version": EXECUTION_SCHEMA,
        "attempt_root": str(inputs.attempt_root),
        "project": {"path": str(inputs.project), "sha256": inputs.project_sha256},
        "build_plan": {
            "path": str(inputs.build_plan),
            "sha256": inputs.build_plan_sha256,
            "content_digest": inputs.plan["content_digest"],
        },
        "map_path": inputs.map_path,
        "output_root": str(output),
        "script": {"path": str(inputs.script), "sha256": inputs.script_sha256},
        "capture": {
            "width": WIDTH,
            "height": HEIGHT,
            "room_kinds": [camera["room_kind"] for camera in inputs.cameras],
            "cameras": [dict(camera) for camera in inputs.cameras],
        },
        "artifacts": {
            "ue_result": str(output / UE_RESULT_FILE),
            "editor_log": str(output / EDITOR_LOG_FILE),
            "editor_stdout": str(output / EDITOR_STDOUT_FILE),
        },
        "policy": {
            "append_only_output": True,
            "caller_python_allowed": False,
            "fixed_camera_actor_tags": True,
            "regular_editor_x11": True,
            "receipt_requires_host_png_validation": True,
        },
    }


def build_editor_command(inputs: CaptureInputs) -> list[str]:
    output = inputs.output_dir
    return [
        str(inputs.unreal_editor),
        str(inputs.project),
        inputs.map_path,
        f"-ExecutePythonScript={inputs.script}",
        "-unattended",
        "-Windowed",
        "-ForceRes",
        f"-ResX={WIDTH}",
        f"-ResY={HEIGHT}",
        f"-graphicsadapter={inputs.graphics_adapter}",
        "-NOSPLASH",
        "-NOSOUND",
        "-NoAnalytics",
        "-UDPMESSAGING_TRANSPORT_ENABLE=0",
        "-ini:Engine:[/Script/TcpMessaging.TcpMessagingSettings]:EnableTransport=False",
        "-ddc=InstalledNoZenLocalFallback",
        "-ExecCmds=t.MaxFPS 60",
        "-SaveToUserDir",
        f"-UserDir={output / 'ue-user'}",
        f"-LocalDataCachePath={output / 'ddc'}",
        f"-abslog={output / EDITOR_LOG_FILE}",
        "-stdout",
        "-FullStdOutLogOutput",
    ]


def _prepare_output(inputs: CaptureInputs, execution_raw: bytes) -> None:
    try:
        os.mkdir(inputs.output_dir, 0o700)
        os.mkdir(inputs.output_dir / IMAGES_DIR, 0o700)
        os.mkdir(inputs.output_dir / "ue-user", 0o700)
        os.mkdir(inputs.output_dir / "ddc", 0o700)
    except FileExistsError:
        _fail("VISTA_HOME_REVIEW_OUTPUT_EXISTS", "append-only output attempt already exists", pointer=str(inputs.output_dir))
    except OSError as exc:
        _fail("VISTA_HOME_REVIEW_OUTPUT_CREATE_FAILED", f"cannot create output attempt: {exc}", pointer=str(inputs.output_dir))
    _write_exclusive(inputs.output_dir / EXECUTION_FILE, execution_raw)


def _terminate_owned(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=10)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=10)


def run_editor(inputs: CaptureInputs, execution_sha256: str) -> int:
    env = os.environ.copy()
    env["DISPLAY"] = inputs.display
    env[WORKER_ENV] = "1"
    env[EXECUTION_ENV] = str(inputs.output_dir / EXECUTION_FILE)
    env[EXECUTION_SHA_ENV] = execution_sha256
    command = build_editor_command(inputs)
    stdout_path = inputs.output_dir / EDITOR_STDOUT_FILE
    with stdout_path.open("xb") as stdout:
        try:
            process = subprocess.Popen(
                command,
                cwd=str(inputs.output_dir),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            _fail("VISTA_HOME_REVIEW_EDITOR_LAUNCH_FAILED", f"UnrealEditor launch failed: {exc}")
        try:
            return process.wait(timeout=inputs.timeout_seconds)
        except subprocess.TimeoutExpired:
            _terminate_owned(process)
            _fail("VISTA_HOME_REVIEW_EDITOR_TIMEOUT", "UnrealEditor did not finish the fixed capture before timeout")
    raise AssertionError("unreachable")


@dataclass(frozen=True)
class PngInspection:
    width: int
    height: int
    bit_depth: int
    color_type: int
    bytes_per_pixel: int
    unique_rgb_count_capped: int
    luma_min: int
    luma_max: int
    opaque_pixel_count: int
    pixel_count: int

    @property
    def nonblank(self) -> bool:
        return (
            self.opaque_pixel_count >= max(1, self.pixel_count // 100)
            and self.unique_rgb_count_capped >= 16
            and self.luma_max - self.luma_min >= 8
        )


def _paeth(a: int, b: int, c: int) -> int:
    estimate = a + b - c
    pa = abs(estimate - a)
    pb = abs(estimate - b)
    pc = abs(estimate - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def inspect_png(path: Path, *, expected_width: int = WIDTH, expected_height: int = HEIGHT) -> PngInspection:
    """Strictly decode a bounded 8-bit RGB/RGBA PNG and prove it is nonblank."""

    source = _existing_file(path, "captured PNG")
    size = source.stat().st_size
    if size <= 0 or size > MAX_PNG_BYTES:
        _fail("VISTA_HOME_REVIEW_PNG_INVALID", "PNG size is outside safety bound", pointer=str(source))
    raw = source.read_bytes()
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        _fail("VISTA_HOME_REVIEW_PNG_INVALID", "PNG signature differs", pointer=str(source))
    offset = 8
    ihdr: tuple[int, int, int, int, int, int, int] | None = None
    compressed = bytearray()
    saw_iend = False
    while offset < len(raw):
        if offset + 12 > len(raw):
            _fail("VISTA_HOME_REVIEW_PNG_INVALID", "PNG chunk header is truncated", pointer=str(source))
        length = struct.unpack(">I", raw[offset : offset + 4])[0]
        kind = raw[offset + 4 : offset + 8]
        end = offset + 12 + length
        if length > MAX_PNG_BYTES or end > len(raw):
            _fail("VISTA_HOME_REVIEW_PNG_INVALID", "PNG chunk is truncated or oversized", pointer=str(source))
        payload = raw[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack(">I", raw[offset + 8 + length : end])[0]
        if binascii.crc32(kind + payload) & 0xFFFFFFFF != expected_crc:
            _fail("VISTA_HOME_REVIEW_PNG_INVALID", "PNG chunk CRC differs", pointer=str(source))
        if kind == b"IHDR":
            if ihdr is not None or length != 13:
                _fail("VISTA_HOME_REVIEW_PNG_INVALID", "PNG IHDR is duplicated or malformed", pointer=str(source))
            ihdr = struct.unpack(">IIBBBBB", payload)
        elif kind == b"IDAT":
            compressed.extend(payload)
            if len(compressed) > MAX_PNG_BYTES:
                _fail("VISTA_HOME_REVIEW_PNG_INVALID", "PNG compressed pixels exceed safety bound", pointer=str(source))
        elif kind == b"IEND":
            if length != 0:
                _fail("VISTA_HOME_REVIEW_PNG_INVALID", "PNG IEND is malformed", pointer=str(source))
            saw_iend = True
            offset = end
            break
        offset = end
    if ihdr is None or not saw_iend or offset != len(raw) or not compressed:
        _fail("VISTA_HOME_REVIEW_PNG_INVALID", "PNG structure is incomplete", pointer=str(source))
    width, height, bit_depth, color_type, compression, filter_method, interlace = ihdr
    if (width, height) != (expected_width, expected_height):
        _fail("VISTA_HOME_REVIEW_PNG_DIMENSIONS", f"PNG is {width}x{height}, expected {expected_width}x{expected_height}", pointer=str(source))
    if bit_depth != 8 or color_type not in {2, 6} or compression != 0 or filter_method != 0 or interlace != 0:
        _fail("VISTA_HOME_REVIEW_PNG_UNSUPPORTED", "PNG must be non-interlaced 8-bit RGB or RGBA", pointer=str(source))
    bytes_per_pixel = 3 if color_type == 2 else 4
    stride = width * bytes_per_pixel
    expected_bytes = height * (stride + 1)
    try:
        decompressor = zlib.decompressobj()
        pixels = decompressor.decompress(bytes(compressed), expected_bytes + 1)
        if decompressor.unconsumed_tail:
            _fail("VISTA_HOME_REVIEW_PNG_INVALID", "PNG decompressed pixels exceed safety bound", pointer=str(source))
        pixels += decompressor.flush()
        if decompressor.unused_data or not decompressor.eof:
            _fail("VISTA_HOME_REVIEW_PNG_INVALID", "PNG zlib stream has trailing or incomplete data", pointer=str(source))
    except zlib.error as exc:
        _fail("VISTA_HOME_REVIEW_PNG_INVALID", f"PNG pixel stream cannot be decompressed: {exc}", pointer=str(source))
    if len(pixels) != expected_bytes:
        _fail("VISTA_HOME_REVIEW_PNG_INVALID", "PNG decompressed pixel length differs", pointer=str(source))

    previous = bytearray(stride)
    unique: set[tuple[int, int, int]] = set()
    luma_min = 255
    luma_max = 0
    opaque_count = 0
    cursor = 0
    for _ in range(height):
        filter_type = pixels[cursor]
        cursor += 1
        encoded = pixels[cursor : cursor + stride]
        cursor += stride
        if filter_type > 4:
            _fail("VISTA_HOME_REVIEW_PNG_INVALID", "PNG scanline filter is invalid", pointer=str(source))
        row = bytearray(stride)
        for index, byte in enumerate(encoded):
            left = row[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
            above = previous[index]
            upper_left = previous[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
            if filter_type == 0:
                predictor = 0
            elif filter_type == 1:
                predictor = left
            elif filter_type == 2:
                predictor = above
            elif filter_type == 3:
                predictor = (left + above) // 2
            else:
                predictor = _paeth(left, above, upper_left)
            row[index] = (byte + predictor) & 0xFF
        for index in range(0, stride, bytes_per_pixel):
            red, green, blue = row[index], row[index + 1], row[index + 2]
            alpha = row[index + 3] if bytes_per_pixel == 4 else 255
            if alpha:
                opaque_count += 1
                if len(unique) < 256:
                    unique.add((red, green, blue))
                luma = (54 * red + 183 * green + 19 * blue) >> 8
                luma_min = min(luma_min, luma)
                luma_max = max(luma_max, luma)
        previous = row
    inspection = PngInspection(
        width=width,
        height=height,
        bit_depth=bit_depth,
        color_type=color_type,
        bytes_per_pixel=bytes_per_pixel,
        unique_rgb_count_capped=len(unique),
        luma_min=luma_min if opaque_count else 0,
        luma_max=luma_max if opaque_count else 0,
        opaque_pixel_count=opaque_count,
        pixel_count=width * height,
    )
    if not inspection.nonblank:
        _fail("VISTA_HOME_REVIEW_PNG_BLANK", "PNG does not contain sufficient visible scene variation", pointer=str(source))
    return inspection


def _load_ue_result(inputs: CaptureInputs, execution_sha256: str) -> tuple[dict[str, Any], str]:
    result_path = inputs.output_dir / UE_RESULT_FILE
    result, raw = _load_json(result_path, label="Unreal capture result")
    expected_keys = {
        "schema_version",
        "status",
        "captured_at",
        "engine_version",
        "project_path",
        "map_path",
        "execution_sha256",
        "camera_actor_set_exact",
        "captures",
        "error",
    }
    if set(result) != expected_keys or result.get("schema_version") != UE_RESULT_SCHEMA:
        _fail("VISTA_HOME_REVIEW_UE_RESULT_INVALID", "Unreal result fields or schema differ")
    if result.get("status") != "captured_candidate" or result.get("error") is not None:
        error = result.get("error")
        _fail("VISTA_HOME_REVIEW_UE_CAPTURE_FAILED", f"Unreal rejected capture: {error}")
    if result.get("execution_sha256") != execution_sha256:
        _fail("VISTA_HOME_REVIEW_UE_RESULT_INVALID", "Unreal result execution binding differs")
    if result.get("project_path") != str(inputs.project) or result.get("map_path") != inputs.map_path:
        _fail("VISTA_HOME_REVIEW_UE_RESULT_INVALID", "Unreal loaded project or map differs")
    if result.get("camera_actor_set_exact") is not True:
        _fail("VISTA_HOME_REVIEW_CAMERA_SET_INVALID", "materialized camera actor set was not exact")
    captures = result.get("captures")
    if not isinstance(captures, list) or len(captures) != len(inputs.cameras):
        _fail("VISTA_HOME_REVIEW_UE_RESULT_INVALID", "Unreal capture count differs")
    expected_semantics = [camera["semantic_id"] for camera in inputs.cameras]
    actual_semantics = [capture.get("semantic_id") for capture in captures if isinstance(capture, Mapping)]
    if actual_semantics != expected_semantics:
        _fail("VISTA_HOME_REVIEW_UE_RESULT_INVALID", "Unreal capture camera order or IDs differ")
    return result, sha256_bytes(raw)


def build_receipt(
    inputs: CaptureInputs,
    execution_sha256: str,
    ue_result: Mapping[str, Any],
    ue_result_sha256: str,
    editor_returncode: int,
) -> dict[str, Any]:
    if editor_returncode != 0:
        _fail("VISTA_HOME_REVIEW_EDITOR_FAILED", f"UnrealEditor exited with status {editor_returncode}")
    captures_by_semantic = {
        capture["semantic_id"]: capture
        for capture in ue_result["captures"]
        if isinstance(capture, Mapping)
    }
    images: list[dict[str, Any]] = []
    for camera in inputs.cameras:
        capture = captures_by_semantic.get(camera["semantic_id"])
        if capture is None or capture.get("relative_path") != camera["relative_path"]:
            _fail("VISTA_HOME_REVIEW_UE_RESULT_INVALID", "Unreal image path differs from fixed plan")
        image_path = _require_child(
            _existing_file(inputs.output_dir / camera["relative_path"], "captured PNG"),
            inputs.output_dir,
            "captured PNG",
        )
        inspection = inspect_png(image_path)
        if capture.get("bytes") != image_path.stat().st_size:
            _fail("VISTA_HOME_REVIEW_UE_RESULT_INVALID", "Unreal-reported PNG size differs")
        images.append(
            {
                "ordinal": camera["ordinal"],
                "room_kind": camera["room_kind"],
                "room_id": camera["room_id"],
                "camera_id": camera["camera_id"],
                "semantic_id": camera["semantic_id"],
                "actor_label": capture.get("actor_label"),
                "capture_method": capture.get("capture_method"),
                "actual_transform": capture.get("actual_transform"),
                "actual_fov_deg": capture.get("actual_fov_deg"),
                "path": image_path.relative_to(inputs.output_dir).as_posix(),
                "bytes": image_path.stat().st_size,
                "sha256": sha256_file(image_path),
                "png": {
                    "width": inspection.width,
                    "height": inspection.height,
                    "bit_depth": inspection.bit_depth,
                    "color_type": inspection.color_type,
                    "unique_rgb_count_capped": inspection.unique_rgb_count_capped,
                    "luma_min": inspection.luma_min,
                    "luma_max": inspection.luma_max,
                    "opaque_pixel_count": inspection.opaque_pixel_count,
                    "pixel_count": inspection.pixel_count,
                    "nonblank": inspection.nonblank,
                },
            }
        )
    room_kinds = [image["room_kind"] for image in images]
    expected_room_kinds = [camera[0] for camera in FIXED_REVIEW_CAMERAS]
    if room_kinds != expected_room_kinds:
        _fail("VISTA_HOME_REVIEW_ROOM_SET_INVALID", "receipt room order differs")
    return {
        "schema_version": RECEIPT_SCHEMA,
        "status": "accepted",
        "accepted_at": _utc_now(),
        "attempt_root": str(inputs.attempt_root),
        "output_root": str(inputs.output_dir),
        "map_path": inputs.map_path,
        "engine": {
            "executable": str(inputs.unreal_editor),
            "version": ue_result.get("engine_version"),
            "display": inputs.display,
            "graphics_adapter": inputs.graphics_adapter,
            "regular_editor_x11": True,
        },
        "bindings": {
            "project_path": str(inputs.project),
            "project_sha256": inputs.project_sha256,
            "build_plan_path": str(inputs.build_plan),
            "build_plan_sha256": inputs.build_plan_sha256,
            "build_plan_content_digest": inputs.plan["content_digest"],
            "script_path": str(inputs.script),
            "script_sha256": inputs.script_sha256,
            "execution_sha256": execution_sha256,
            "ue_result_sha256": ue_result_sha256,
        },
        "capture": {
            "width": WIDTH,
            "height": HEIGHT,
            "room_kinds": room_kinds,
            "images": images,
        },
        "verification": {
            "exact_room_set": True,
            "exact_materialized_camera_actor_set": True,
            "every_png_exact_dimensions": True,
            "every_png_nonblank": True,
            "caller_python_allowed": False,
        },
    }


def _host_main(args: argparse.Namespace) -> int:
    try:
        inputs = validate_inputs(args)
        execution = build_execution(inputs)
        execution_raw = canonical_json(execution)
        execution_sha = sha256_bytes(execution_raw)
        preview = {
            "status": "validated_dry_run" if not args.apply else "capture_pending",
            "execution_sha256": execution_sha,
            "output_root": str(inputs.output_dir),
            "room_kinds": [camera["room_kind"] for camera in inputs.cameras],
            "command": build_editor_command(inputs),
            "policy": execution["policy"],
        }
        if not args.apply:
            sys.stdout.buffer.write(canonical_json(preview))
            return 0
        _prepare_output(inputs, execution_raw)
        returncode = run_editor(inputs, execution_sha)
        ue_result, ue_result_sha = _load_ue_result(inputs, execution_sha)
        receipt = build_receipt(inputs, execution_sha, ue_result, ue_result_sha, returncode)
        receipt_raw = canonical_json(receipt)
        receipt_path = inputs.output_dir / RECEIPT_FILE
        _write_exclusive(receipt_path, receipt_raw)
        sys.stdout.buffer.write(
            canonical_json(
                {
                    "status": "accepted",
                    "receipt": str(receipt_path),
                    "receipt_sha256": sha256_bytes(receipt_raw),
                    "image_count": len(receipt["capture"]["images"]),
                }
            )
        )
        return 0
    except ReviewCaptureError as exc:
        sys.stderr.buffer.write(canonical_json({"status": "failed", "error": exc.public_dict()}))
        return 2


def _angle_delta(first: float, second: float) -> float:
    return abs((first - second + 180.0) % 360.0 - 180.0)


def _close_vector(actual: Sequence[float], expected: Sequence[float], tolerance: float = 0.05) -> bool:
    return len(actual) == len(expected) and all(abs(float(a) - float(b)) <= tolerance for a, b in zip(actual, expected))


def _actual_transform(actor: Any) -> dict[str, list[float]]:
    location = actor.get_actor_location()
    rotation = actor.get_actor_rotation()
    scale = actor.get_actor_scale3d()
    return {
        "location_cm": [float(location.x), float(location.y), float(location.z)],
        # HouseSpec rotation_deg stores XYZ = Unreal roll, pitch, yaw.
        "rotation_deg": [float(rotation.roll), float(rotation.pitch), float(rotation.yaw)],
        "scale": [float(scale.x), float(scale.y), float(scale.z)],
    }


def _transform_matches(actual: Mapping[str, Sequence[float]], expected: Mapping[str, Sequence[float]]) -> bool:
    return (
        _close_vector(actual["location_cm"], expected["location_cm"])
        and _close_vector(actual["scale"], expected["scale"])
        and all(
            _angle_delta(float(a), float(b)) <= 0.05
            for a, b in zip(actual["rotation_deg"], expected["rotation_deg"])
        )
    )


def _safe_execution_child(value: Any, output_root: Path, label: str) -> Path:
    if not isinstance(value, str):
        _fail("VISTA_HOME_REVIEW_EXECUTION_INVALID", f"{label} path is not a string")
    path = _absolute_lexical(Path(value), label)
    _reject_symlink_components(path, label, allow_missing_tail=True)
    return _require_child(path, output_root, label)


def _load_worker_execution() -> tuple[dict[str, Any], str]:
    manifest_text = os.environ.get(EXECUTION_ENV, "")
    expected_sha = os.environ.get(EXECUTION_SHA_ENV, "")
    if SHA256_RE.fullmatch(expected_sha) is None:
        _fail("VISTA_HOME_REVIEW_EXECUTION_INVALID", "worker execution pin is invalid")
    manifest_path = _existing_file(Path(manifest_text), "review execution manifest")
    execution, raw = _load_json(manifest_path, label="review execution manifest", expected_sha256=expected_sha)
    expected_policy = {
        "append_only_output": True,
        "caller_python_allowed": False,
        "fixed_camera_actor_tags": True,
        "regular_editor_x11": True,
        "receipt_requires_host_png_validation": True,
    }
    if execution.get("schema_version") != EXECUTION_SCHEMA or execution.get("policy") != expected_policy:
        _fail("VISTA_HOME_REVIEW_EXECUTION_INVALID", "worker execution schema or policy differs")
    output_root = _existing_directory(Path(execution.get("output_root", "")), "review output root")
    if manifest_path != output_root / EXECUTION_FILE:
        _fail("VISTA_HOME_REVIEW_EXECUTION_INVALID", "execution manifest location differs")
    script = execution.get("script")
    if not isinstance(script, Mapping) or set(script) != {"path", "sha256"}:
        _fail("VISTA_HOME_REVIEW_EXECUTION_INVALID", "fixed script binding differs")
    current_script = _existing_file(Path(__file__).resolve(strict=True), "fixed review capture script")
    if script["path"] != str(current_script) or script["sha256"] != sha256_file(current_script):
        _fail("VISTA_HOME_REVIEW_EXECUTION_INVALID", "fixed script identity or digest differs")
    project = execution.get("project")
    build_plan = execution.get("build_plan")
    if not isinstance(project, Mapping) or set(project) != {"path", "sha256"}:
        _fail("VISTA_HOME_REVIEW_EXECUTION_INVALID", "project binding differs")
    if not isinstance(build_plan, Mapping) or set(build_plan) != {"path", "sha256", "content_digest"}:
        _fail("VISTA_HOME_REVIEW_EXECUTION_INVALID", "build plan binding differs")
    project_path = _existing_file(Path(project["path"]), "project")
    plan_path = _existing_file(Path(build_plan["path"]), "build plan")
    if sha256_file(project_path) != project["sha256"] or sha256_file(plan_path) != build_plan["sha256"]:
        _fail("VISTA_HOME_REVIEW_PIN_MISMATCH", "worker project or build plan pin differs")
    plan, _ = _load_json(plan_path, label="build plan", expected_sha256=build_plan["sha256"])
    if plan.get("content_digest") != build_plan["content_digest"]:
        _fail("VISTA_HOME_REVIEW_PIN_MISMATCH", "worker build plan content digest differs")
    cameras = compile_fixed_cameras(plan, execution.get("map_path"))
    capture = execution.get("capture")
    if not isinstance(capture, Mapping) or capture.get("width") != WIDTH or capture.get("height") != HEIGHT:
        _fail("VISTA_HOME_REVIEW_EXECUTION_INVALID", "worker capture dimensions differ")
    if capture.get("room_kinds") != [camera["room_kind"] for camera in cameras] or capture.get("cameras") != cameras:
        _fail("VISTA_HOME_REVIEW_EXECUTION_INVALID", "worker fixed camera plan differs")
    artifacts = execution.get("artifacts")
    if not isinstance(artifacts, Mapping) or set(artifacts) != {"ue_result", "editor_log", "editor_stdout"}:
        _fail("VISTA_HOME_REVIEW_EXECUTION_INVALID", "worker artifact bindings differ")
    if _safe_execution_child(artifacts["ue_result"], output_root, "Unreal result") != output_root / UE_RESULT_FILE:
        _fail("VISTA_HOME_REVIEW_EXECUTION_INVALID", "worker Unreal result path differs")
    for camera in cameras:
        expected_path = output_root / camera["relative_path"]
        _safe_execution_child(str(expected_path), output_root, "fixed image")
        if expected_path.exists():
            _fail("VISTA_HOME_REVIEW_OUTPUT_EXISTS", "fixed screenshot already exists", pointer=str(expected_path))
    return execution, expected_sha


def _worker_write_result(execution: Mapping[str, Any], result: Mapping[str, Any]) -> None:
    output_root = Path(execution["output_root"])
    target = output_root / UE_RESULT_FILE
    _write_exclusive(target, canonical_json(dict(result)))


def _worker_result(
    execution: Mapping[str, Any],
    execution_sha: str,
    *,
    status: str,
    captures: Sequence[Mapping[str, Any]],
    camera_actor_set_exact: bool,
    error: Mapping[str, Any] | None,
    engine_version: str | None,
    project_path: str | None,
    map_path: str | None,
) -> dict[str, Any]:
    return {
        "schema_version": UE_RESULT_SCHEMA,
        "status": status,
        "captured_at": _utc_now(),
        "engine_version": engine_version,
        "project_path": project_path,
        "map_path": map_path,
        "execution_sha256": execution_sha,
        "camera_actor_set_exact": camera_actor_set_exact,
        "captures": [dict(item) for item in captures],
        "error": dict(error) if error is not None else None,
    }


def _unreal_worker() -> int:
    """Run only when this byte-pinned file is invoked by UnrealEditor."""

    try:
        import unreal  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - only possible in misconfigured UE
        raise RuntimeError("Unreal Python module is unavailable") from exc

    execution: dict[str, Any] | None = None
    execution_sha = os.environ.get(EXECUTION_SHA_ENV, "")
    keep_alive = False
    try:
        execution, execution_sha = _load_worker_execution()
        project_path = str(Path(unreal.Paths.get_project_file_path()).resolve(strict=True))
        if project_path != execution["project"]["path"] or sha256_file(Path(project_path)) != execution["project"]["sha256"]:
            _fail("VISTA_HOME_REVIEW_PROJECT_INVALID", "loaded Unreal project differs from the pin")
        engine_version = str(unreal.SystemLibrary.get_engine_version())
        if not engine_version.startswith("5."):
            _fail("VISTA_HOME_REVIEW_ENGINE_INVALID", "Unreal Engine major version differs")
        editor = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
        actor_subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        world = editor.get_editor_world()
        if world is None:
            _fail("VISTA_HOME_REVIEW_MAP_MISMATCH", "editor world is unavailable")
        loaded_map = str(world.get_path_name()).split(".", 1)[0]
        if loaded_map != execution["map_path"]:
            _fail("VISTA_HOME_REVIEW_MAP_MISMATCH", f"loaded map {loaded_map!r} differs")

        expected_cameras = execution["capture"]["cameras"]
        expected_tags = {camera["semantic_tag"] for camera in expected_cameras}
        actors_by_tag: dict[str, list[Any]] = {tag: [] for tag in expected_tags}
        vista_camera_tags: set[str] = set()
        for actor in actor_subsystem.get_all_level_actors():
            if not isinstance(actor, unreal.CameraActor):
                continue
            tags = {str(tag) for tag in actor.get_editor_property("tags")}
            for tag in tags:
                if tag.startswith("VistaSemanticId=home.r1/room.") and "/camera." in tag:
                    vista_camera_tags.add(tag)
                if tag in actors_by_tag:
                    actors_by_tag[tag].append(actor)
        if vista_camera_tags != expected_tags or any(len(actors_by_tag[tag]) != 1 for tag in expected_tags):
            _fail("VISTA_HOME_REVIEW_CAMERA_SET_INVALID", "materialized review CameraActor set is not exact")

        resolved: list[tuple[dict[str, Any], Any, dict[str, list[float]], float]] = []
        for camera in expected_cameras:
            actor = actors_by_tag[camera["semantic_tag"]][0]
            actual_transform = _actual_transform(actor)
            if not _transform_matches(actual_transform, camera["expected_transform"]):
                _fail("VISTA_HOME_REVIEW_CAMERA_DRIFT", f"materialized camera {camera['semantic_id']} transform differs")
            component = actor.get_editor_property("camera_component")
            fov = float(component.get_editor_property("field_of_view"))
            if abs(fov - float(camera["expected_fov_deg"])) > 0.05:
                _fail("VISTA_HOME_REVIEW_CAMERA_DRIFT", f"materialized camera {camera['semantic_id']} FOV differs")
            resolved.append((camera, actor, actual_transform, fov))

        unreal.EditorPythonScripting.set_keep_python_script_alive(True)
        keep_alive = True
        unreal.SystemLibrary.execute_console_command(world, "Realtime 1")
        unreal.SystemLibrary.execute_console_command(world, "r.Streaming.FullyLoadUsedTextures 1")
        unreal.EditorLevelLibrary.editor_set_game_view(True)
        unreal.EditorLevelLibrary.editor_invalidate_viewports()

        state: dict[str, Any] = {
            "handle": None,
            "phase": "warmup",
            "phase_started": time.monotonic(),
            "index": 0,
            "stable_size": None,
            "stable_since": None,
            "captures": [],
            "finished": False,
        }

        def finish(error: ReviewCaptureError | Exception | None = None) -> None:
            if state["finished"]:
                return
            state["finished"] = True
            handle = state.get("handle")
            if handle is not None:
                unreal.unregister_slate_post_tick_callback(handle)
                state["handle"] = None
            try:
                if error is None:
                    result = _worker_result(
                        execution,
                        execution_sha,
                        status="captured_candidate",
                        captures=state["captures"],
                        camera_actor_set_exact=True,
                        error=None,
                        engine_version=engine_version,
                        project_path=project_path,
                        map_path=loaded_map,
                    )
                else:
                    if isinstance(error, ReviewCaptureError):
                        public_error = error.public_dict()
                    else:
                        public_error = {"code": "VISTA_HOME_REVIEW_UE_EXCEPTION", "message": str(error)}
                    result = _worker_result(
                        execution,
                        execution_sha,
                        status="failed",
                        captures=state["captures"],
                        camera_actor_set_exact=True,
                        error=public_error,
                        engine_version=engine_version,
                        project_path=project_path,
                        map_path=loaded_map,
                    )
                _worker_write_result(execution, result)
            finally:
                unreal.EditorPythonScripting.set_keep_python_script_alive(False)

        def on_tick(_delta_seconds: float) -> None:
            try:
                now = time.monotonic()
                phase = state["phase"]
                if phase == "warmup":
                    if now - state["phase_started"] < 2.0:
                        return
                    state["phase"] = "set_camera"
                if state["phase"] == "set_camera":
                    if state["index"] >= len(resolved):
                        finish()
                        return
                    camera, actor, _actual, _fov = resolved[state["index"]]
                    location = actor.get_actor_location()
                    rotation = actor.get_actor_rotation()
                    editor.set_level_viewport_camera_info(location, rotation)
                    capture_method = "camera_actor_transform"
                    pilot = getattr(unreal.EditorLevelLibrary, "pilot_level_actor", None)
                    if callable(pilot):
                        pilot(actor)
                        capture_method = "camera_actor_pilot"
                    unreal.EditorLevelLibrary.editor_invalidate_viewports()
                    state["capture_method"] = capture_method
                    state["phase"] = "settle"
                    state["phase_started"] = now
                    return
                if state["phase"] == "settle":
                    if now - state["phase_started"] < 0.5:
                        return
                    camera, _actor, _actual, _fov = resolved[state["index"]]
                    image_path = Path(execution["output_root"]) / camera["relative_path"]
                    result = unreal.AutomationLibrary.take_high_res_screenshot(WIDTH, HEIGHT, str(image_path))
                    if result is False:
                        _fail("VISTA_HOME_REVIEW_SCREENSHOT_REJECTED", f"Unreal rejected {camera['semantic_id']} screenshot")
                    state["phase"] = "await_file"
                    state["phase_started"] = now
                    state["stable_size"] = None
                    state["stable_since"] = None
                    return
                if state["phase"] == "await_file":
                    camera, actor, actual, fov = resolved[state["index"]]
                    image_path = Path(execution["output_root"]) / camera["relative_path"]
                    if now - state["phase_started"] > 30.0:
                        _fail("VISTA_HOME_REVIEW_SCREENSHOT_TIMEOUT", f"PNG for {camera['semantic_id']} did not stabilize")
                    if not image_path.is_file():
                        return
                    size = image_path.stat().st_size
                    if size <= 0:
                        return
                    if state["stable_size"] != size:
                        state["stable_size"] = size
                        state["stable_since"] = now
                        return
                    if state["stable_since"] is None or now - state["stable_since"] < 0.25:
                        return
                    state["captures"].append(
                        {
                            "ordinal": camera["ordinal"],
                            "room_kind": camera["room_kind"],
                            "room_id": camera["room_id"],
                            "camera_id": camera["camera_id"],
                            "semantic_id": camera["semantic_id"],
                            "actor_label": str(actor.get_actor_label()),
                            "capture_method": state["capture_method"],
                            "actual_transform": actual,
                            "actual_fov_deg": fov,
                            "relative_path": camera["relative_path"],
                            "bytes": size,
                        }
                    )
                    eject = getattr(unreal.EditorLevelLibrary, "eject_pilot_level_actor", None)
                    if callable(eject):
                        eject()
                    state["index"] += 1
                    state["phase"] = "set_camera"
                    state["phase_started"] = now
            except Exception as exc:  # Unreal callback boundary
                finish(exc)

        state["handle"] = unreal.register_slate_post_tick_callback(on_tick)
        unreal.log("VISTA_PLAYABLE_HOME_REVIEW_CAPTURE_STARTED")
        return 0
    except Exception as exc:
        if execution is not None:
            try:
                if isinstance(exc, ReviewCaptureError):
                    public_error = exc.public_dict()
                else:
                    public_error = {"code": "VISTA_HOME_REVIEW_UE_EXCEPTION", "message": str(exc)}
                _worker_write_result(
                    execution,
                    _worker_result(
                        execution,
                        execution_sha,
                        status="failed",
                        captures=[],
                        camera_actor_set_exact=False,
                        error=public_error,
                        engine_version=None,
                        project_path=None,
                        map_path=None,
                    ),
                )
            except Exception:
                pass
        try:
            unreal.log_error(f"VISTA_PLAYABLE_HOME_REVIEW_CAPTURE_FAILED: {exc}")
        finally:
            if keep_alive:
                unreal.EditorPythonScripting.set_keep_python_script_alive(False)
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempt-root", required=True, help="existing append-only UE build attempt root")
    parser.add_argument("--project", required=True, help="materialized VistaPlayableHome.uproject inside the attempt")
    parser.add_argument("--build-plan", required=True, help="pinned build-plan.json inside the attempt")
    parser.add_argument("--build-plan-sha256", required=True, help="expected lowercase SHA-256 for build-plan.json")
    parser.add_argument("--map-path", required=True, help="must equal the fixed r1 map in the pinned plan")
    parser.add_argument("--unreal-editor", required=True, help="regular Linux UnrealEditor executable")
    parser.add_argument("--output-dir", required=True, help="new attempt-<id> directory below the UE attempt")
    parser.add_argument("--display", required=True, help="local X11 display, for example :117")
    parser.add_argument("--graphics-adapter", type=int, default=0, help="bounded Unreal graphics adapter index")
    parser.add_argument("--timeout-seconds", type=int, default=300, help="owned editor timeout (60-900 seconds)")
    parser.add_argument("--apply", action="store_true", help="create the output attempt and launch fixed Unreal capture")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    if os.environ.get(WORKER_ENV) == "1":
        return _unreal_worker()
    return _host_main(build_parser().parse_args(argv))


if __name__ == "__main__":
    # ``SystemExit`` is useful for the host CLI, but the Unreal Python plugin
    # treats it as a script exception even with status zero.  The worker leaves
    # normally after registering its keep-alive post-tick callback.
    if os.environ.get(WORKER_ENV) == "1":
        _unreal_worker()
    else:
        raise SystemExit(main())
