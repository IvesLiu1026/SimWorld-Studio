#!/usr/bin/env python3
"""Capture the six fixed VISTA Playable Home review cameras in Unreal Editor.

The host process validates a SHA-256-pinned build plan and an already
materialized UE project, creates a fresh append-only output attempt, then
launches six regular X11 ``UnrealEditor`` children sequentially.  Each child is
bound to one immutable ordinal manifest and executes this same byte-pinned
file; callers cannot supply Python source or a script path.

Inside Unreal, every worker revalidates the full materialized ``CameraActor``
tag set, pilots its selected actor, and requests exactly one native
``HighResShot`` into a host-created profile-bound private scratch directory.
Only the host may strict-decode those exact bytes, copy them with ``O_EXCL``
into final evidence paths, rehash them, and aggregate a receipt after all six
distinct images have passed.

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
import copy
import hashlib
import json
import math
import os
import pathlib
import re
import secrets
import shlex
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


Path = pathlib.Path
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

BUILD_PLAN_SCHEMA = "simworld.vista.playable-home-build-plan/v1"
EXECUTION_SCHEMA = "simworld.vista.playable-home-review-capture-execution/v2"
R2_EXECUTION_SCHEMA = "simworld.vista.playable-home-review-capture-execution/v4"
WORKER_EXECUTION_SCHEMA = (
    "simworld.vista.playable-home-review-capture-worker-execution/v1"
)
R2_WORKER_EXECUTION_SCHEMA = (
    "simworld.vista.playable-home-review-capture-worker-execution/v3"
)
UE_RESULT_SCHEMA = "simworld.vista.playable-home-review-capture-ue-result/v2"
R2_UE_RESULT_SCHEMA = "simworld.vista.playable-home-review-capture-ue-result/v3"
RECEIPT_SCHEMA = "simworld.vista.playable-home-review-capture-receipt/v2"
R2_RECEIPT_SCHEMA = "simworld.vista.playable-home-review-capture-receipt/v4"
EXPECTED_REVISION = "vista_playable_home_r1"
EXPECTED_HOUSE_ID = "home.r1"
R1_CAPTURE_PROFILE = "fixed_r1"
R2_CAPTURE_PROFILE = "realistic_interior_r2"
CAPTURE_PROFILES = (R1_CAPTURE_PROFILE, R2_CAPTURE_PROFILE)
R2_CAMERA_ACTOR_TAG = "VistaVisualRevision=realistic_interior_r2"
EXPECTED_MAP_PATH = (
    "/Game/VISTA/PlayableHome/vista_playable_home_r1/Maps/VistaPlayableHome"
)
EXPECTED_MAP_ASSET_RELATIVE = Path(
    "project/Content/VISTA/PlayableHome/vista_playable_home_r1/Maps/"
    "VistaPlayableHome.umap"
)
EXPECTED_PROJECT_NAME = "VistaPlayableHome.uproject"
EXPECTED_BUILD_RESULT_NAME = "result-receipt.json"
EXPECTED_BUILD_RESULT_SCHEMA = "simworld.vista.playable-home-ue-build-result/v1"
EXPECTED_VISUAL_PROFILE_RELATIVE = Path("contracts/visual-profile.json")
EXPECTED_VISUAL_PROFILE_SCHEMA = "simworld.vista.playable-home-visual-profile/v1"
EXPECTED_ENGINE_PREFIX = "5.7."
WIDTH = 1280
HEIGHT = 720
R2_WIDTH = 1920
R2_HEIGHT = 1080
R2_DISPLAY = ":119"
CAPTURE_METHOD = "camera_actor_pilot_highres_console"
SCREENSHOT_TIMEOUT_SECONDS = 120.0
WORKER_PROOF_POLL_INTERVAL_SECONDS = 0.25
WORKER_PROOF_STABILITY_SECONDS = 0.5
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_PNG_BYTES = 128 * 1024 * 1024
MAX_DDC_SEED_FILES = 20_000
MAX_DDC_SEED_BYTES = 2 * 1024 * 1024 * 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DISPLAY_RE = re.compile(r"^:[0-9]{1,5}(?:\.[0-9]{1,3})?$")
ATTEMPT_RE = re.compile(r"^attempt-[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
SAFE_LOCAL_PATH_RE = re.compile(r"^/[A-Za-z0-9_./-]+$")
SAFE_PATH_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
EXECUTION_ENV = "VISTA_PLAYABLE_HOME_REVIEW_EXECUTION"
EXECUTION_SHA_ENV = "VISTA_PLAYABLE_HOME_REVIEW_EXECUTION_SHA256"
WORKER_ENV = "VISTA_PLAYABLE_HOME_REVIEW_WORKER"
EXECUTION_FILE = "execution.json"
UE_RESULT_FILE = "ue-result.json"
RECEIPT_FILE = "review-capture-receipt.json"
EDITOR_LOG_FILE = "unreal-editor.log"
EDITOR_STDOUT_FILE = "unreal-editor-stdout.log"
IMAGES_DIR = "images"
WORKERS_DIR = "workers"
LOCAL_SCRATCH_PARENT = Path("/tmp")
R1_SCRATCH_PREFIX = "vista-home-review-"
R2_SCRATCH_PREFIX = "vista-home-review-r2-"
R2_SCRATCH_LIFECYCLE = "append_only_retained_evidence"
R2_SCRATCH_CLEANUP_POLICY = "descriptor_close_only"
NVIDIA_VULKAN_ICD = Path("/usr/share/vulkan/icd.d/nvidia_icd.json")
PASSTHROUGH_ENV_KEYS = (
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "PATH",
    "SHELL",
    "USER",
    "XDG_DATA_DIRS",
)

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

# The r2 evidence lane is deliberately a closed vertical slice: two shots for
# each of the three presentation-finished rooms.  It is not a caller-defined
# camera surface and its order is part of the execution and receipt contract.
R2_REVIEW_SHOTS: tuple[tuple[str, str, str, str], ...] = (
    (
        "shot.entry_hall.overview",
        "entry_hall",
        "home.r1/room.entry_hall",
        "overview",
    ),
    (
        "shot.entry_hall.hero",
        "entry_hall",
        "home.r1/room.entry_hall",
        "hero",
    ),
    (
        "shot.living_room.overview",
        "living_room",
        "home.r1/room.living_room",
        "overview",
    ),
    (
        "shot.living_room.hero",
        "living_room",
        "home.r1/room.living_room",
        "hero",
    ),
    (
        "shot.kitchen_dining.overview",
        "kitchen_dining",
        "home.r1/room.kitchen_dining",
        "overview",
    ),
    (
        "shot.kitchen_dining.hero",
        "kitchen_dining",
        "home.r1/room.kitchen_dining",
        "hero",
    ),
)
R2_ORDERED_SHOT_IDS = tuple(item[0] for item in R2_REVIEW_SHOTS)


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


_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | os.O_DIRECTORY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_R2_ALLOWED_SCRATCH_FILESYSTEMS = frozenset({"nfs", "nfs4"})
_MOUNTINFO_DECIMAL_RE = re.compile(br"^[1-9][0-9]*$")
_MOUNTINFO_MAJOR_MINOR_RE = re.compile(br"^[0-9]+:[0-9]+$")
_MOUNTINFO_FILESYSTEM_RE = re.compile(br"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_MOUNTINFO_PROPAGATION_RE = re.compile(
    br"^(?:shared|master|propagate_from):[1-9][0-9]*$"
)


def _fd_mount_id(descriptor: int) -> int:
    try:
        raw = Path(f"/proc/self/fdinfo/{descriptor}").read_bytes()
    except OSError as exc:
        _fail("VISTA_HOME_REVIEW_SCRATCH_INVALID", "cannot read scratch mount identity")
        raise AssertionError("unreachable") from exc
    matches = re.findall(br"^mnt_id:\s*([0-9]+)\s*$", raw, re.MULTILINE)
    if len(matches) != 1 or _MOUNTINFO_DECIMAL_RE.fullmatch(matches[0]) is None:
        _fail("VISTA_HOME_REVIEW_SCRATCH_INVALID", "scratch mount identity is unavailable")
    return int(matches[0])


def _decode_mountinfo_field(value: bytes, label: str) -> bytes:
    if not value:
        _fail("VISTA_HOME_REVIEW_SCRATCH_INVALID", f"mountinfo {label} is empty")
    decoded = bytearray()
    index = 0
    while index < len(value):
        current = value[index]
        if current == ord("\\"):
            escape = value[index + 1 : index + 4]
            if len(escape) != 3 or any(byte not in b"01234567" for byte in escape):
                _fail(
                    "VISTA_HOME_REVIEW_SCRATCH_INVALID",
                    f"mountinfo {label} has an invalid octal escape",
                )
            decoded_byte = int(escape, 8)
            if decoded_byte > 0xFF:
                _fail(
                    "VISTA_HOME_REVIEW_SCRATCH_INVALID",
                    f"mountinfo {label} octal escape is outside one byte",
                )
            decoded.append(decoded_byte)
            index += 4
            continue
        if current <= 0x20 or current == 0x7F:
            _fail(
                "VISTA_HOME_REVIEW_SCRATCH_INVALID",
                f"mountinfo {label} contains an unescaped control byte",
            )
        decoded.append(current)
        index += 1
    if b"\x00" in decoded:
        _fail("VISTA_HOME_REVIEW_SCRATCH_INVALID", f"mountinfo {label} contains NUL")
    return bytes(decoded)


def _validate_mountinfo_options(value: bytes, label: str) -> None:
    decoded = _decode_mountinfo_field(value, label)
    if any(not option for option in decoded.split(b",")):
        _fail(
            "VISTA_HOME_REVIEW_SCRATCH_INVALID",
            f"mountinfo {label} has an empty option",
        )


def _parse_mountinfo_identity(raw: bytes, mount_id: int) -> tuple[int, str, str]:
    expected_id = str(mount_id).encode("ascii")
    matches: list[list[bytes]] = []
    for line in raw.splitlines():
        fields = line.split(b" ")
        if fields and fields[0] == expected_id:
            matches.append(fields)
    if len(matches) != 1:
        _fail(
            "VISTA_HOME_REVIEW_SCRATCH_INVALID",
            "scratch mount table identity is missing or ambiguous",
        )
    fields = matches[0]
    if any(not field for field in fields) or fields.count(b"-") != 1:
        _fail("VISTA_HOME_REVIEW_SCRATCH_INVALID", "scratch mount table entry is malformed")
    separator = fields.index(b"-")
    if separator < 6 or len(fields) != separator + 4:
        _fail(
            "VISTA_HOME_REVIEW_SCRATCH_INVALID",
            "scratch mount table entry field count differs",
        )
    if (
        _MOUNTINFO_DECIMAL_RE.fullmatch(fields[0]) is None
        or _MOUNTINFO_DECIMAL_RE.fullmatch(fields[1]) is None
        or _MOUNTINFO_MAJOR_MINOR_RE.fullmatch(fields[2]) is None
        or int(fields[0]) != mount_id
    ):
        _fail("VISTA_HOME_REVIEW_SCRATCH_INVALID", "scratch mount numeric identity is invalid")
    root = _decode_mountinfo_field(fields[3], "root")
    mount_point = _decode_mountinfo_field(fields[4], "mount point")
    if not root.startswith(b"/") or not mount_point.startswith(b"/"):
        _fail(
            "VISTA_HOME_REVIEW_SCRATCH_INVALID",
            "scratch mount root and mount point must be absolute",
        )
    _validate_mountinfo_options(fields[5], "mount options")
    for optional in fields[6:separator]:
        decoded = _decode_mountinfo_field(optional, "optional field")
        if (
            decoded != b"unbindable"
            and _MOUNTINFO_PROPAGATION_RE.fullmatch(decoded) is None
        ):
            _fail(
                "VISTA_HOME_REVIEW_SCRATCH_INVALID",
                "scratch mount optional field is not recognized",
            )
    filesystem_raw = fields[separator + 1]
    if _MOUNTINFO_FILESYSTEM_RE.fullmatch(filesystem_raw) is None:
        _fail("VISTA_HOME_REVIEW_SCRATCH_INVALID", "scratch filesystem type is invalid")
    mount_source = _decode_mountinfo_field(fields[separator + 2], "mount source")
    _validate_mountinfo_options(fields[separator + 3], "super options")
    return mount_id, filesystem_raw.decode("ascii"), sha256_bytes(mount_source)


def _fd_mount_identity(descriptor: int) -> tuple[int, str, str]:
    """Return the mount id, filesystem type, and hashed mount source for an fd."""

    mount_id = _fd_mount_id(descriptor)
    try:
        raw = Path("/proc/self/mountinfo").read_bytes()
    except OSError as exc:
        _fail("VISTA_HOME_REVIEW_SCRATCH_INVALID", "cannot read scratch mount table")
        raise AssertionError("unreachable") from exc
    return _parse_mountinfo_identity(raw, mount_id)


def _require_r2_nas_mount(identity: tuple[int, str, str]) -> None:
    _, filesystem_type, mount_source_sha256 = identity
    if (
        filesystem_type not in _R2_ALLOWED_SCRATCH_FILESYSTEMS
        or SHA256_RE.fullmatch(mount_source_sha256) is None
    ):
        _fail(
            "VISTA_HOME_REVIEW_SCRATCH_STORAGE_INVALID",
            "r2 scratch policy root must be on an approved NFS or NFS4 mount",
        )


def _open_directory_fd(path: Path, label: str) -> tuple[Path, int, os.stat_result]:
    candidate = _existing_directory(path, label)
    if SAFE_LOCAL_PATH_RE.fullmatch(str(candidate)) is None or not str(candidate).isascii():
        _fail("VISTA_HOME_REVIEW_SCRATCH_INVALID", f"{label} is not safe ASCII")
    try:
        descriptor = os.open(candidate, _DIRECTORY_FLAGS)
    except OSError as exc:
        _fail("VISTA_HOME_REVIEW_SCRATCH_INVALID", f"cannot open {label}: {exc}", pointer=str(candidate))
        raise AssertionError("unreachable") from exc
    try:
        metadata = os.fstat(descriptor)
        observed = os.lstat(candidate)
        if (metadata.st_dev, metadata.st_ino) != (observed.st_dev, observed.st_ino):
            _fail("VISTA_HOME_REVIEW_SCRATCH_AUTHORITY_MISMATCH", f"{label} changed while opening")
    except Exception:
        os.close(descriptor)
        raise
    return candidate, descriptor, metadata


def _open_directory_entry_at(parent_fd: int, name: str, label: str) -> tuple[int, os.stat_result]:
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or not name.isascii()
        or SAFE_PATH_COMPONENT_RE.fullmatch(name) is None
    ):
        _fail("VISTA_HOME_REVIEW_SCRATCH_INVALID", f"{label} has an unsafe component")
    descriptor = -1
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        opened = os.fstat(descriptor)
        after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not _same_inode(before, opened)
            or not _same_inode(opened, after)
        ):
            _fail(
                "VISTA_HOME_REVIEW_SCRATCH_AUTHORITY_MISMATCH",
                f"{label} entry changed while opening",
            )
        return descriptor, opened
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        _fail(
            "VISTA_HOME_REVIEW_SCRATCH_AUTHORITY_MISMATCH",
            f"cannot open {label} relative to retained authority: {exc}",
        )
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    raise AssertionError("unreachable")


def _require_private_directory_fd(
    descriptor: int,
    label: str,
) -> os.stat_result:
    """Force and verify mode 0700 on a newly created owned directory."""

    try:
        os.fchmod(descriptor, 0o700)
        metadata = os.fstat(descriptor)
    except OSError as exc:
        _fail(
            "VISTA_HOME_REVIEW_PRIVATE_DIRECTORY_INVALID",
            f"cannot secure {label}: {exc}",
        )
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700:
        _fail(
            "VISTA_HOME_REVIEW_PRIVATE_DIRECTORY_INVALID",
            f"{label} did not retain required mode 0700",
        )
    return metadata


def _mkdir_private_directory_at(
    parent_fd: int,
    name: str,
    label: str,
) -> tuple[int, os.stat_result]:
    """Create and secure a directory relative to retained parent authority."""

    os.mkdir(name, 0o700, dir_fd=parent_fd)
    descriptor = -1
    try:
        descriptor, _ = _open_directory_entry_at(parent_fd, name, label)
        metadata = _require_private_directory_fd(descriptor, label)
        entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not _same_inode(metadata, entry):
            _fail(
                "VISTA_HOME_REVIEW_PRIVATE_DIRECTORY_INVALID",
                f"{label} entry changed while securing it",
            )
        return descriptor, metadata
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        raise


def _open_relative_directory_fd(
    root_fd: int,
    parts: Sequence[str],
    label: str,
) -> tuple[int, os.stat_result]:
    if not parts:
        _fail("VISTA_HOME_REVIEW_SCRATCH_INVALID", f"{label} must be below policy root")
    current = os.dup(root_fd)
    try:
        metadata = os.fstat(current)
        for index, component in enumerate(parts, start=1):
            child, metadata = _open_directory_entry_at(
                current,
                component,
                f"{label} component {index}",
            )
            os.close(current)
            current = child
        return current, metadata
    except Exception:
        os.close(current)
        raise


def _validate_directory_path_entry(path: Path, descriptor: int, label: str) -> None:
    try:
        entry = os.lstat(path)
        opened = os.fstat(descriptor)
    except OSError as exc:
        _fail(
            "VISTA_HOME_REVIEW_SCRATCH_AUTHORITY_MISMATCH",
            f"cannot rebind {label} path to retained descriptor: {exc}",
        )
    if not _same_inode(entry, opened):
        _fail(
            "VISTA_HOME_REVIEW_SCRATCH_AUTHORITY_MISMATCH",
            f"{label} path no longer names its retained descriptor",
        )


def _validate_relative_directory_entry(
    root_fd: int,
    parts: Sequence[str],
    expected_fd: int,
    label: str,
) -> None:
    observed_fd, observed = _open_relative_directory_fd(root_fd, parts, label)
    try:
        if not _same_inode(observed, os.fstat(expected_fd)):
            _fail(
                "VISTA_HOME_REVIEW_SCRATCH_AUTHORITY_MISMATCH",
                f"{label} path no longer names its retained descriptor",
            )
    finally:
        os.close(observed_fd)


def _prove_directory_control(
    descriptor: int,
    *,
    device: int,
    mount_id: int,
    filesystem_type: str,
    mount_source_sha256: str,
    mapped_owner_uid: int,
) -> None:
    name = f".vista-home-authority-{secrets.token_hex(16)}"
    token = -1
    created = False
    try:
        token = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=descriptor,
        )
        created = True
        proof = b"vista-home-scratch-authority-v1\n"
        if os.write(token, proof) != len(proof):
            _fail("VISTA_HOME_REVIEW_SCRATCH_AUTHORITY_MISMATCH", "scratch authority token write was partial")
        os.fsync(token)
        metadata = os.fstat(token)
        if (
            metadata.st_dev != device
            or metadata.st_uid != mapped_owner_uid
            or _fd_mount_identity(token)
            != (mount_id, filesystem_type, mount_source_sha256)
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            _fail("VISTA_HOME_REVIEW_SCRATCH_AUTHORITY_MISMATCH", "scratch authority token differs")
        os.fsync(descriptor)
        os.unlink(name, dir_fd=descriptor)
        created = False
        os.fsync(descriptor)
    except OSError as exc:
        _fail("VISTA_HOME_REVIEW_SCRATCH_AUTHORITY_MISMATCH", f"scratch authority proof failed: {exc}")
    finally:
        if token >= 0:
            os.close(token)
        if created:
            try:
                os.unlink(name, dir_fd=descriptor)
            except OSError:
                pass


@dataclass
class R2ScratchAuthority:
    policy_root: Path
    parent: Path
    policy_root_fd: int
    parent_fd: int
    policy_root_stat: os.stat_result
    parent_stat: os.stat_result
    parent_relative_parts: tuple[str, ...]
    mount_id: int
    filesystem_type: str
    mount_source_sha256: str
    closed: bool = False

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            for descriptor in (self.parent_fd, self.policy_root_fd):
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def __del__(self) -> None:
        self.close()


def _trees_overlap(first: Path, second: Path) -> bool:
    for child, parent in ((first, second), (second, first)):
        try:
            child.relative_to(parent)
        except ValueError:
            continue
        return True
    return False


def _validate_r2_scratch_parent(
    path: Path,
    policy_root_path: Path,
    attempt_root: Path,
) -> R2ScratchAuthority:
    policy_root, root_fd, root_stat = _open_directory_fd(policy_root_path, "r2 scratch policy root")
    parent_fd = -1
    try:
        candidate = _absolute_lexical(path, "r2 scratch parent")
        _reject_symlink_components(candidate, "r2 scratch parent")
        _require_child(candidate, policy_root, "r2 scratch parent")
        relative_parts = candidate.relative_to(policy_root).parts
        parent_fd, parent_stat = _open_relative_directory_fd(
            root_fd,
            relative_parts,
            "r2 scratch parent",
        )
        _validate_directory_path_entry(policy_root, root_fd, "r2 scratch policy root")
        _validate_relative_directory_entry(
            root_fd,
            relative_parts,
            parent_fd,
            "r2 scratch parent",
        )
        root_mount = _fd_mount_identity(root_fd)
        parent_mount = _fd_mount_identity(parent_fd)
        _require_r2_nas_mount(root_mount)
        _require_r2_nas_mount(parent_mount)
        if root_stat.st_dev != parent_stat.st_dev or root_mount != parent_mount:
            _fail(
                "VISTA_HOME_REVIEW_SCRATCH_MOUNT_MISMATCH",
                "r2 scratch parent crosses a nested mount or bind mount",
            )
        if (
            root_stat.st_uid != parent_stat.st_uid
            or stat.S_IMODE(root_stat.st_mode) != 0o700
            or stat.S_IMODE(parent_stat.st_mode) != 0o700
        ):
            _fail(
                "VISTA_HOME_REVIEW_SCRATCH_AUTHORITY_MISMATCH",
                "r2 policy root and parent require the same mapped owner and mode 0700",
            )
        if _trees_overlap(candidate, attempt_root):
            _fail(
                "VISTA_HOME_REVIEW_SCRATCH_INVALID",
                "r2 scratch parent and UE attempt trees must be disjoint",
            )
        _prove_directory_control(
            parent_fd,
            device=parent_stat.st_dev,
            mount_id=root_mount[0],
            filesystem_type=root_mount[1],
            mount_source_sha256=root_mount[2],
            mapped_owner_uid=root_stat.st_uid,
        )
        return R2ScratchAuthority(
            policy_root=policy_root,
            parent=candidate,
            policy_root_fd=root_fd,
            parent_fd=parent_fd,
            policy_root_stat=root_stat,
            parent_stat=parent_stat,
            parent_relative_parts=tuple(relative_parts),
            mount_id=root_mount[0],
            filesystem_type=root_mount[1],
            mount_source_sha256=root_mount[2],
        )
    except Exception:
        if parent_fd >= 0:
            os.close(parent_fd)
        os.close(root_fd)
        raise


def _validate_r2_scratch_authority(authority: R2ScratchAuthority) -> None:
    if authority.closed:
        _fail("VISTA_HOME_REVIEW_SCRATCH_AUTHORITY_MISMATCH", "r2 scratch authority is closed")
    root = os.fstat(authority.policy_root_fd)
    parent = os.fstat(authority.parent_fd)
    _validate_directory_path_entry(
        authority.policy_root,
        authority.policy_root_fd,
        "r2 scratch policy root",
    )
    _validate_relative_directory_entry(
        authority.policy_root_fd,
        authority.parent_relative_parts,
        authority.parent_fd,
        "r2 scratch parent",
    )
    expected_mount = (
        authority.mount_id,
        authority.filesystem_type,
        authority.mount_source_sha256,
    )
    _require_r2_nas_mount(expected_mount)
    if (
        (root.st_dev, root.st_ino)
        != (authority.policy_root_stat.st_dev, authority.policy_root_stat.st_ino)
        or (parent.st_dev, parent.st_ino)
        != (authority.parent_stat.st_dev, authority.parent_stat.st_ino)
        or _fd_mount_identity(authority.policy_root_fd) != expected_mount
        or _fd_mount_identity(authority.parent_fd) != expected_mount
        or root.st_dev != parent.st_dev
        or root.st_uid != parent.st_uid
        or stat.S_IMODE(root.st_mode) != 0o700
        or stat.S_IMODE(parent.st_mode) != 0o700
    ):
        _fail("VISTA_HOME_REVIEW_SCRATCH_AUTHORITY_MISMATCH", "retained scratch authority changed")


def _r2_scratch_parent_binding(authority: R2ScratchAuthority) -> dict[str, Any]:
    _validate_r2_scratch_authority(authority)
    root = authority.policy_root_stat
    parent = authority.parent_stat
    return {
        "storage_class": "private_nas_retained_evidence",
        "policy_root_path_sha256": sha256_bytes(str(authority.policy_root).encode("ascii")),
        "policy_root_device": root.st_dev,
        "policy_root_inode": root.st_ino,
        "policy_root_mount_id": authority.mount_id,
        "filesystem_type": authority.filesystem_type,
        "mount_source_sha256": authority.mount_source_sha256,
        "mapped_owner_uid": root.st_uid,
        "parent_path_sha256": sha256_bytes(str(authority.parent).encode("ascii")),
        "parent_device": parent.st_dev,
        "parent_inode": parent.st_ino,
        "parent_mount_id": authority.mount_id,
        "parent_relative_components": len(authority.parent_relative_parts),
        "parent_mode": "0700",
        "authority_check": "parent_dirfd_o_excl_create_fsync_unlink",
        "child_creation": "unique_mkdirat_eexist_fail",
        "lifecycle": R2_SCRATCH_LIFECYCLE,
        "cleanup_policy": R2_SCRATCH_CLEANUP_POLICY,
        "receipt_discloses_scratch_absolute_path": False,
    }


def _validate_r2_scratch_parent_binding(value: Any) -> dict[str, Any]:
    integers = {
        "policy_root_device", "policy_root_inode", "policy_root_mount_id",
        "mapped_owner_uid", "parent_device", "parent_inode", "parent_mount_id",
        "parent_relative_components",
    }
    expected = integers | {
        "storage_class", "policy_root_path_sha256", "parent_path_sha256",
        "filesystem_type", "mount_source_sha256",
        "parent_mode", "authority_check", "child_creation", "lifecycle",
        "cleanup_policy",
        "receipt_discloses_scratch_absolute_path",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        _fail("VISTA_HOME_REVIEW_EXECUTION_INVALID", "r2 scratch-parent binding fields differ")
    if (
        value.get("storage_class") != "private_nas_retained_evidence"
        or any(isinstance(value.get(key), bool) or not isinstance(value.get(key), int) or value[key] < 0 for key in integers)
        or any(value[key] <= 0 for key in ("policy_root_inode", "policy_root_mount_id", "parent_inode", "parent_mount_id"))
        or value["parent_relative_components"] <= 0
        or any(
            SHA256_RE.fullmatch(value.get(key, "")) is None
            for key in (
                "policy_root_path_sha256",
                "parent_path_sha256",
                "mount_source_sha256",
            )
        )
        or value.get("filesystem_type") not in _R2_ALLOWED_SCRATCH_FILESYSTEMS
        or value["policy_root_device"] != value["parent_device"]
        or value["policy_root_mount_id"] != value["parent_mount_id"]
        or value.get("parent_mode") != "0700"
        or value.get("authority_check") != "parent_dirfd_o_excl_create_fsync_unlink"
        or value.get("child_creation") != "unique_mkdirat_eexist_fail"
        or value.get("lifecycle") != R2_SCRATCH_LIFECYCLE
        or value.get("cleanup_policy") != R2_SCRATCH_CLEANUP_POLICY
        or value.get("receipt_discloses_scratch_absolute_path") is not False
    ):
        _fail("VISTA_HOME_REVIEW_EXECUTION_INVALID", "r2 scratch-parent binding policy differs")
    return dict(value)


def _open_observed_r2_parent(parent: Path, binding: Mapping[str, Any]) -> int:
    expected = _validate_r2_scratch_parent_binding(binding)
    component_count = expected["parent_relative_components"]
    if len(parent.parts) <= component_count:
        _fail("VISTA_HOME_REVIEW_SCRATCH_AUTHORITY_MISMATCH", "scratch parent depth differs")
    policy_root = parent.parents[component_count - 1]
    if sha256_bytes(str(policy_root).encode("ascii")) != expected["policy_root_path_sha256"]:
        _fail("VISTA_HOME_REVIEW_SCRATCH_AUTHORITY_MISMATCH", "scratch policy-root path differs")
    candidate, root_fd, root_metadata = _open_directory_fd(
        policy_root,
        "r2 scratch policy root",
    )
    parent_fd = -1
    try:
        relative_parts = parent.relative_to(candidate).parts
        if len(relative_parts) != component_count:
            _fail("VISTA_HOME_REVIEW_SCRATCH_AUTHORITY_MISMATCH", "scratch parent depth differs")
        parent_fd, metadata = _open_relative_directory_fd(
            root_fd,
            relative_parts,
            "r2 scratch parent",
        )
        mount_identity = _fd_mount_identity(parent_fd)
        _require_r2_nas_mount(mount_identity)
        if (
            sha256_bytes(str(parent).encode("ascii")) != expected["parent_path_sha256"]
            or root_metadata.st_dev != expected["policy_root_device"]
            or root_metadata.st_ino != expected["policy_root_inode"]
            or metadata.st_dev != expected["parent_device"]
            or metadata.st_ino != expected["parent_inode"]
            or root_metadata.st_uid != expected["mapped_owner_uid"]
            or metadata.st_uid != root_metadata.st_uid
            or _fd_mount_identity(root_fd) != mount_identity
            or mount_identity
            != (
                expected["parent_mount_id"],
                expected["filesystem_type"],
                expected["mount_source_sha256"],
            )
            or stat.S_IMODE(root_metadata.st_mode) != 0o700
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            _fail("VISTA_HOME_REVIEW_SCRATCH_AUTHORITY_MISMATCH", "observed scratch parent differs")
        _validate_directory_path_entry(candidate, root_fd, "r2 scratch policy root")
        _validate_relative_directory_entry(
            root_fd,
            relative_parts,
            parent_fd,
            "r2 scratch parent",
        )
        result = parent_fd
        parent_fd = -1
        return result
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)
        os.close(root_fd)


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


def compile_realistic_cameras(
    visual_profile: Mapping[str, Any],
    map_path: str,
    *,
    room_bounds_by_id: Mapping[str, Mapping[str, Any]] | None = None,
    approved_doorway_bounds_by_room: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    blocking_bounds: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Compile r2 look-at shots for a future 1080p capture execution.

    This is intentionally separate from ``compile_fixed_cameras`` so the
    accepted r1 six-camera CLI and receipts retain their exact v2 contract.
    Preflight AABBs are optional; when they are absent, each camera explicitly
    retains a runtime-observation requirement instead of claiming clearance.
    """

    if map_path != EXPECTED_MAP_PATH:
        _fail("VISTA_HOME_REVIEW_MAP_MISMATCH", "requested r2 map differs from the pinned home map")
    from tools.ue.vista_playable_home import planning as realism_planning

    try:
        operations = realism_planning.compile_realistic_review_operations(
            visual_profile,
            room_bounds_by_id=room_bounds_by_id,
            approved_doorway_bounds_by_room=approved_doorway_bounds_by_room,
            blocking_bounds=blocking_bounds,
        )
    except realism_planning.VistaPlayableHomePlanError as exc:
        _fail(exc.code, exc.detail)
    ordered = operations
    room_kind_by_id = {
        room_id: room_kind
        for _shot_id, room_kind, room_id, _purpose in R2_REVIEW_SHOTS
    }
    cameras: list[dict[str, Any]] = []
    for ordinal, operation in enumerate(ordered, start=1):
        shot_id = operation["review_shot_id"]
        file_id = re.sub(r"[^A-Za-z0-9._-]", "_", shot_id)
        cameras.append({
            "ordinal": ordinal,
            "visual_profile_id": visual_profile.get("visual_profile_id"),
            "room_kind": room_kind_by_id.get(
                operation["room_id"],
                operation["room_id"].rsplit(".", 1)[-1],
            ),
            "room_id": operation["room_id"],
            "camera_id": shot_id,
            "purpose": operation["purpose"],
            "semantic_id": operation["semantic_id"],
            "semantic_tag": f"VistaSemanticId={operation['semantic_id']}",
            "eye_location_cm": list(operation["eye_location_cm"]),
            "look_at_target_cm": list(operation["look_at_target_cm"]),
            "expected_transform": dict(operation["transform"]),
            "expected_fov_deg": operation["fov_deg"],
            "near_field_clearance_cm": operation["near_field_clearance_cm"],
            "exposure": dict(operation["exposure"]),
            "expected_hero_ids": list(operation["expected_hero_ids"]),
            "forbidden_foreground_ids": list(operation["forbidden_foreground_ids"]),
            "preflight": dict(operation["preflight"]),
            "width": R2_WIDTH,
            "height": R2_HEIGHT,
            "relative_path": f"{IMAGES_DIR}/{ordinal:02d}-{file_id}.png",
        })
    if len({camera["semantic_id"] for camera in cameras}) != len(cameras):
        _fail("VISTA_HOME_REVIEW_CAMERA_SET_INVALID", "r2 semantic camera IDs are duplicated")
    if any(camera["expected_transform"]["rotation_deg"][0] != 0.0 for camera in cameras):
        _fail("VISTA_HOME_REVIEW_LOOK_AT_INVALID", "r2 camera compiler produced nonzero roll")
    return cameras


def _visual_profile_content_digest(profile: Mapping[str, Any]) -> str:
    body = copy.deepcopy(dict(profile))
    body.pop("content_digest", None)
    try:
        raw = json.dumps(
            body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", "strict")
    except (TypeError, ValueError, OverflowError, UnicodeError) as exc:
        _fail(
            "VISTA_HOME_REVIEW_VISUAL_PROFILE_INVALID",
            "visual profile is not finite canonical JSON",
        )
        raise AssertionError from exc
    return sha256_bytes(raw)


def _visual_profile_house_view(plan: Mapping[str, Any]) -> dict[str, Any]:
    try:
        rooms = [
            {
                "room_id": room["room_id"],
                "transform": {
                    "location_m": [0.0, 0.0, 0.0],
                    "scale": [1.0, 1.0, 1.0],
                },
                "bounds_m": {
                    "min_m": [
                        float(value) / 100.0
                        for value in room["world_bounds_cm"]["min_cm"]
                    ],
                    "max_m": [
                        float(value) / 100.0
                        for value in room["world_bounds_cm"]["max_cm"]
                    ],
                },
            }
            for room in plan["rooms"]
        ]
        entities = [
            {"entity_id": entity["entity_id"]}
            for entity in plan["entities"]
        ]
        return {
            "revision": plan["house"]["revision"],
            "content_digest": plan["house"]["content_digest"],
            "rooms": rooms,
            "entities": entities,
        }
    except (KeyError, TypeError, ValueError) as exc:
        _fail(
            "VISTA_HOME_REVIEW_PLAN_INVALID",
            "build plan cannot provide the visual-profile house view",
        )
        raise AssertionError from exc


def _room_bounds_by_id(plan: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    try:
        return {
            room["room_id"]: room["world_bounds_cm"]
            for room in plan["rooms"]
        }
    except (KeyError, TypeError) as exc:
        _fail(
            "VISTA_HOME_REVIEW_PLAN_INVALID",
            "build plan room bounds are unavailable",
        )
        raise AssertionError from exc


def _compile_r2_capture_cameras(
    profile: Mapping[str, Any],
    plan: Mapping[str, Any],
    map_path: str,
) -> list[dict[str, Any]]:
    if (
        profile.get("schema_version") != EXPECTED_VISUAL_PROFILE_SCHEMA
        or profile.get("visual_profile_id") != R2_CAPTURE_PROFILE
        or profile.get("house_revision") != EXPECTED_REVISION
        or profile.get("content_digest") != _visual_profile_content_digest(profile)
    ):
        _fail(
            "VISTA_HOME_REVIEW_VISUAL_PROFILE_INVALID",
            "r2 visual profile identity, revision, or content digest differs",
        )
    budget = profile.get("performance_budget")
    if not isinstance(budget, Mapping) or budget.get("resolution_px") != [
        R2_WIDTH,
        R2_HEIGHT,
    ]:
        _fail(
            "VISTA_HOME_REVIEW_VISUAL_PROFILE_INVALID",
            "r2 visual profile does not pin 1920x1080 capture",
        )
    cameras = compile_realistic_cameras(
        profile,
        map_path,
        room_bounds_by_id=_room_bounds_by_id(plan),
    )
    observed = tuple(
        (
            camera["camera_id"],
            camera["room_kind"],
            camera["room_id"],
            camera["purpose"],
        )
        for camera in cameras
    )
    if observed != R2_REVIEW_SHOTS:
        _fail(
            "VISTA_HOME_REVIEW_CAMERA_SET_INVALID",
            "r2 profile does not contain the exact ordered six-shot contract",
        )
    return cameras


def validate_realistic_camera_observation(
    camera: Mapping[str, Any],
    observation: Mapping[str, Any],
) -> dict[str, Any]:
    """Fail closed on retained UE obstruction/hero visibility observations."""

    measured_clearance = observation.get("nearest_blocker_clearance_cm")
    occlusion = observation.get("foreground_occlusion_fraction")
    visible_hero_ids = observation.get("visible_hero_ids")
    blocker_id = observation.get("nearest_blocker_id")
    if (isinstance(measured_clearance, bool) or
            not isinstance(measured_clearance, (int, float)) or
            not math.isfinite(float(measured_clearance)) or float(measured_clearance) < 0.0):
        _fail("VISTA_HOME_REVIEW_OBSERVATION_INVALID", "near-field clearance observation is invalid")
    if (isinstance(occlusion, bool) or not isinstance(occlusion, (int, float)) or
            not math.isfinite(float(occlusion)) or not 0.0 <= float(occlusion) <= 1.0):
        _fail("VISTA_HOME_REVIEW_OBSERVATION_INVALID", "foreground occlusion observation is invalid")
    if not isinstance(visible_hero_ids, list) or not all(isinstance(item, str) for item in visible_hero_ids):
        _fail("VISTA_HOME_REVIEW_OBSERVATION_INVALID", "visible hero observation is invalid")
    failures: list[dict[str, Any]] = []
    minimum = float(camera["near_field_clearance_cm"])
    if float(measured_clearance) < minimum:
        failures.append({
            "gate": "near_field_clearance",
            "expected_minimum_cm": minimum,
            "actual_cm": float(measured_clearance),
            "blocker_id": blocker_id,
        })
    if camera.get("purpose") == "overview" and float(occlusion) > 0.25:
        failures.append({
            "gate": "overview_foreground_occlusion",
            "expected_maximum_fraction": 0.25,
            "actual_fraction": float(occlusion),
        })
    expected_heroes = set(camera.get("expected_hero_ids", []))
    visible = set(visible_hero_ids)
    minimum_visible = min(3, len(expected_heroes))
    if len(expected_heroes & visible) < minimum_visible:
        failures.append({
            "gate": "expected_hero_visibility",
            "expected_minimum_count": minimum_visible,
            "actual_visible_ids": sorted(expected_heroes & visible),
        })
    forbidden = set(camera.get("forbidden_foreground_ids", []))
    observed_foreground = set(observation.get("foreground_semantic_ids", []))
    if forbidden & observed_foreground:
        failures.append({
            "gate": "forbidden_foreground",
            "actual_ids": sorted(forbidden & observed_foreground),
        })
    return {
        "status": "accepted_observation" if not failures else "rejected_observation",
        "runtime_observation": True,
        "camera_semantic_id": camera["semantic_id"],
        "failures": failures,
        "nearest_blocker_id": blocker_id,
        "nearest_blocker_clearance_cm": float(measured_clearance),
        "foreground_occlusion_fraction": float(occlusion),
        "visible_hero_ids": sorted(visible),
    }


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
    map_asset: Path
    map_asset_sha256: str
    build_plan: Path
    build_plan_sha256: str
    plan: dict[str, Any]
    build_result: Path
    build_result_sha256: str
    map_path: str
    unreal_editor: Path
    unreal_editor_sha256: str
    output_dir: Path
    display: str
    graphics_adapter: int
    timeout_seconds: int
    script: Path
    script_sha256: str
    nvidia_icd_sha256: str
    ddc_seed: Path | None
    ddc_seed_tree_sha256: str | None
    cameras: tuple[dict[str, Any], ...]
    capture_profile: str = R1_CAPTURE_PROFILE
    visual_profile: dict[str, Any] | None = None
    visual_profile_path: Path | None = None
    visual_profile_sha256: str | None = None
    visual_profile_content_digest: str | None = None
    scratch_policy_root: Path | None = None
    scratch_parent: Path | None = None
    scratch_authority: R2ScratchAuthority | None = None

    def close(self) -> None:
        if self.scratch_authority is not None:
            self.scratch_authority.close()


def _tree_snapshot(root: Path) -> tuple[str, int, int]:
    records: list[dict[str, Any]] = []
    total_bytes = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        _reject_symlink_components(path, "DDC seed entry")
        relative = path.relative_to(root).as_posix()
        metadata = os.lstat(path)
        if stat.S_ISDIR(metadata.st_mode):
            records.append({"path": relative, "type": "directory"})
            continue
        if not stat.S_ISREG(metadata.st_mode):
            _fail("VISTA_HOME_REVIEW_DDC_SEED_INVALID", "DDC seed has a non-regular entry", pointer=str(path))
        total_bytes += metadata.st_size
        records.append(
            {
                "path": relative,
                "type": "file",
                "bytes": metadata.st_size,
                "sha256": sha256_file(path),
            }
        )
        if len(records) > MAX_DDC_SEED_FILES or total_bytes > MAX_DDC_SEED_BYTES:
            _fail("VISTA_HOME_REVIEW_DDC_SEED_INVALID", "DDC seed exceeds its safety bound")
    if not records:
        _fail("VISTA_HOME_REVIEW_DDC_SEED_INVALID", "DDC seed is empty")
    return sha256_bytes(canonical_json(records)), len(records), total_bytes


def _validate_build_result(
    path: Path,
    attempt_root: Path,
    map_path: str,
    *,
    visual_profile_id: str | None = None,
    visual_profile_sha256: str | None = None,
    visual_profile_content_digest: str | None = None,
) -> tuple[dict[str, Any], str]:
    if path != attempt_root / EXPECTED_BUILD_RESULT_NAME:
        _fail("VISTA_HOME_REVIEW_BUILD_RESULT_INVALID", "build result location differs")
    result, raw = _load_json(path, label="accepted UE build result")
    if (
        result.get("schema_version") != EXPECTED_BUILD_RESULT_SCHEMA
        or result.get("status") != "accepted_candidate"
        or result.get("attempt_root") != str(attempt_root)
        or result.get("revision") != EXPECTED_REVISION
        or result.get("map_path") != map_path
    ):
        _fail("VISTA_HOME_REVIEW_BUILD_RESULT_INVALID", "accepted UE build result binding differs")
    if visual_profile_id is not None:
        if (
            result.get("visual_profile_id") != visual_profile_id
            or result.get("visual_profile_sha256") != visual_profile_sha256
            or result.get("visual_profile_content_digest")
            != visual_profile_content_digest
            or result.get("renderer_runtime_observation") != "pending"
            or not isinstance(result.get("renderer_profile_request_sha256"), str)
            or SHA256_RE.fullmatch(result["renderer_profile_request_sha256"])
            is None
            or not isinstance(
                result.get("renderer_profile_request_content_digest"), str
            )
            or SHA256_RE.fullmatch(
                result["renderer_profile_request_content_digest"]
            )
            is None
        ):
            _fail(
                "VISTA_HOME_REVIEW_BUILD_RESULT_INVALID",
                "accepted UE build result r2 visual-profile binding differs",
            )
        presentation_keys = {
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
        if (
            not presentation_keys.issubset(result)
            or any(
                not isinstance(result.get(key), str)
                or SHA256_RE.fullmatch(result[key]) is None
                for key in (
                    "base_scene_receipt_sha256",
                    "presentation_import_receipt_sha256",
                    "presentation_scene_receipt_sha256",
                    "presentation_manifest_sha256",
                    "presentation_artifact_receipt_sha256",
                )
            )
            or isinstance(result.get("presentation_bundle_count"), bool)
            or not isinstance(result.get("presentation_bundle_count"), int)
            or result["presentation_bundle_count"] <= 0
            or result.get("presentation_collision_policy")
            != "presentation_no_collision_use_hidden_r1_proxies"
            or result.get("presentation_ue_import_observation")
            != "verified_by_commandlet"
            or result.get("presentation_runtime_play_proof") != "pending"
        ):
            _fail(
                "VISTA_HOME_REVIEW_BUILD_RESULT_INVALID",
                "accepted UE build result presentation status differs",
            )
    return result, sha256_bytes(raw)


def _load_r2_visual_profile(
    path: Path,
    expected_sha256: str,
    plan: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    if SHA256_RE.fullmatch(expected_sha256 or "") is None:
        _fail(
            "VISTA_HOME_REVIEW_PIN_INVALID",
            "visual profile pin must be a lowercase SHA-256",
        )
    profile, raw = _load_json(
        path,
        label="r2 visual profile",
        expected_sha256=expected_sha256,
    )
    try:
        from world_packs.vista_playable_home_r1.visual_profiles import (
            contract as visual_profile_contract,
        )

        visual_profile_contract.validate_profile(
            profile,
            _visual_profile_house_view(plan),
        )
    except ImportError as exc:
        _fail(
            "VISTA_HOME_REVIEW_VISUAL_PROFILE_INVALID",
            "visual-profile contract validator is unavailable",
        )
        raise AssertionError from exc
    except visual_profile_contract.VisualProfileContractError as exc:
        _fail(
            "VISTA_HOME_REVIEW_VISUAL_PROFILE_INVALID",
            str(exc),
            pointer=str(path),
        )
    _compile_r2_capture_cameras(profile, plan, EXPECTED_MAP_PATH)
    return profile, sha256_bytes(raw)


def validate_inputs(args: argparse.Namespace) -> CaptureInputs:
    attempt_root = _existing_directory(Path(args.attempt_root), "attempt root")
    project = _require_child(_existing_file(Path(args.project), "project"), attempt_root, "project")
    _, project_sha = _validate_project(project)
    map_asset = _require_child(
        _existing_file(attempt_root / EXPECTED_MAP_ASSET_RELATIVE, "materialized map asset"),
        attempt_root,
        "materialized map asset",
    )
    build_plan = _require_child(_existing_file(Path(args.build_plan), "build plan"), attempt_root, "build plan")
    if SHA256_RE.fullmatch(args.build_plan_sha256 or "") is None:
        _fail("VISTA_HOME_REVIEW_PIN_INVALID", "build plan pin must be a lowercase SHA-256")
    plan, raw = _load_json(build_plan, label="build plan", expected_sha256=args.build_plan_sha256)
    plan_sha = sha256_bytes(raw)
    capture_profile = getattr(args, "capture_profile", R1_CAPTURE_PROFILE)
    if capture_profile not in CAPTURE_PROFILES:
        _fail(
            "VISTA_HOME_REVIEW_CAPTURE_PROFILE_INVALID",
            "capture profile is not one of the closed profiles",
        )
    scratch_policy_root_arg = getattr(args, "scratch_policy_root", None)
    scratch_parent_arg = getattr(args, "scratch_parent", None)
    scratch_policy_root: Path | None = None
    scratch_parent: Path | None = None
    if capture_profile == R1_CAPTURE_PROFILE:
        if scratch_policy_root_arg is not None or scratch_parent_arg is not None:
            _fail(
                "VISTA_HOME_REVIEW_SCRATCH_INVALID",
                "the fixed r1 capture does not accept r2 scratch options",
            )
    else:
        if scratch_policy_root_arg is None or scratch_parent_arg is None:
            _fail(
                "VISTA_HOME_REVIEW_SCRATCH_INVALID",
                "realistic_interior_r2 requires --scratch-policy-root and --scratch-parent",
            )
        scratch_policy_root = Path(scratch_policy_root_arg)
        scratch_parent = Path(scratch_parent_arg)
    visual_profile_arg = getattr(args, "visual_profile", None)
    visual_profile_sha_arg = getattr(args, "visual_profile_sha256", None)
    if bool(visual_profile_arg) != bool(visual_profile_sha_arg):
        _fail(
            "VISTA_HOME_REVIEW_VISUAL_PROFILE_INVALID",
            "visual profile path and SHA-256 pin must be supplied together",
        )
    visual_profile: dict[str, Any] | None = None
    visual_profile_path: Path | None = None
    visual_profile_sha256: str | None = None
    visual_profile_content_digest: str | None = None
    if capture_profile == R1_CAPTURE_PROFILE:
        if visual_profile_arg is not None:
            _fail(
                "VISTA_HOME_REVIEW_VISUAL_PROFILE_INVALID",
                "the fixed r1 capture does not accept a visual profile",
            )
        cameras = compile_fixed_cameras(plan, args.map_path)
    else:
        if visual_profile_arg is None or visual_profile_sha_arg is None:
            _fail(
                "VISTA_HOME_REVIEW_VISUAL_PROFILE_INVALID",
                "realistic_interior_r2 requires a visual profile and SHA-256 pin",
            )
        visual_profile_path = _require_child(
            _existing_file(Path(visual_profile_arg), "r2 visual profile"),
            attempt_root,
            "r2 visual profile",
        )
        if visual_profile_path != attempt_root / EXPECTED_VISUAL_PROFILE_RELATIVE:
            _fail(
                "VISTA_HOME_REVIEW_VISUAL_PROFILE_INVALID",
                "r2 visual profile must be the attempt-local materialized contract",
                pointer=str(visual_profile_path),
            )
        visual_profile, visual_profile_sha256 = _load_r2_visual_profile(
            visual_profile_path,
            visual_profile_sha_arg,
            plan,
        )
        visual_profile_content_digest = visual_profile["content_digest"]
        cameras = _compile_r2_capture_cameras(
            visual_profile,
            plan,
            args.map_path,
        )
    build_result = _existing_file(attempt_root / EXPECTED_BUILD_RESULT_NAME, "accepted UE build result")
    _, build_result_sha = _validate_build_result(
        build_result,
        attempt_root,
        args.map_path,
        visual_profile_id=(
            R2_CAPTURE_PROFILE
            if capture_profile == R2_CAPTURE_PROFILE
            else None
        ),
        visual_profile_sha256=visual_profile_sha256,
        visual_profile_content_digest=visual_profile_content_digest,
    )
    unreal_editor = _existing_file(Path(args.unreal_editor), "UnrealEditor", executable=True)
    if unreal_editor.name != "UnrealEditor":
        _fail("VISTA_HOME_REVIEW_ENGINE_INVALID", "engine executable must be UnrealEditor")
    if not NVIDIA_VULKAN_ICD.is_file():
        _fail("VISTA_HOME_REVIEW_ENGINE_INVALID", "pinned NVIDIA Vulkan ICD is unavailable")
    if DISPLAY_RE.fullmatch(args.display or "") is None:
        _fail("VISTA_HOME_REVIEW_DISPLAY_INVALID", "DISPLAY must be a local X11 display such as :117")
    if capture_profile == R2_CAPTURE_PROFILE and args.display != R2_DISPLAY:
        _fail(
            "VISTA_HOME_REVIEW_DISPLAY_INVALID",
            f"realistic_interior_r2 capture is pinned to DISPLAY {R2_DISPLAY}",
        )
    if isinstance(args.graphics_adapter, bool) or not 0 <= args.graphics_adapter <= 31:
        _fail("VISTA_HOME_REVIEW_ENGINE_INVALID", "graphics adapter must be between 0 and 31")
    if capture_profile == R2_CAPTURE_PROFILE and args.graphics_adapter != 0:
        _fail(
            "VISTA_HOME_REVIEW_ENGINE_INVALID",
            "realistic_interior_r2 capture is pinned to graphics adapter 0",
        )
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
    ddc_seed: Path | None = None
    ddc_seed_tree_sha256: str | None = None
    if bool(args.ddc_seed) != bool(args.ddc_seed_tree_sha256):
        _fail("VISTA_HOME_REVIEW_DDC_SEED_INVALID", "DDC seed path and tree pin must be provided together")
    if args.ddc_seed:
        ddc_seed = _require_child(
            _existing_directory(Path(args.ddc_seed), "DDC seed"),
            attempt_root,
            "DDC seed",
        )
        if SHA256_RE.fullmatch(args.ddc_seed_tree_sha256 or "") is None:
            _fail("VISTA_HOME_REVIEW_DDC_SEED_INVALID", "DDC seed tree pin is invalid")
        actual_tree, _entries, _bytes = _tree_snapshot(ddc_seed)
        if actual_tree != args.ddc_seed_tree_sha256:
            _fail("VISTA_HOME_REVIEW_PIN_MISMATCH", "DDC seed tree SHA-256 differs")
        ddc_seed_tree_sha256 = actual_tree
    script = _existing_file(Path(__file__).resolve(strict=True), "fixed review capture script")
    map_asset_sha256 = sha256_file(map_asset)
    unreal_editor_sha256 = sha256_file(unreal_editor)
    script_sha256 = sha256_file(script)
    nvidia_icd_sha256 = sha256_file(NVIDIA_VULKAN_ICD)
    scratch_authority: R2ScratchAuthority | None = None
    if capture_profile == R2_CAPTURE_PROFILE:
        assert scratch_policy_root is not None and scratch_parent is not None
        scratch_authority = _validate_r2_scratch_parent(
            scratch_parent,
            scratch_policy_root,
            attempt_root,
        )
        scratch_policy_root = scratch_authority.policy_root
        scratch_parent = scratch_authority.parent
    return CaptureInputs(
        attempt_root=attempt_root,
        project=project,
        project_sha256=project_sha,
        map_asset=map_asset,
        map_asset_sha256=map_asset_sha256,
        build_plan=build_plan,
        build_plan_sha256=plan_sha,
        plan=plan,
        build_result=build_result,
        build_result_sha256=build_result_sha,
        map_path=args.map_path,
        unreal_editor=unreal_editor,
        unreal_editor_sha256=unreal_editor_sha256,
        output_dir=output_dir,
        display=args.display,
        graphics_adapter=args.graphics_adapter,
        timeout_seconds=args.timeout_seconds,
        script=script,
        script_sha256=script_sha256,
        nvidia_icd_sha256=nvidia_icd_sha256,
        ddc_seed=ddc_seed,
        ddc_seed_tree_sha256=ddc_seed_tree_sha256,
        cameras=tuple(cameras),
        capture_profile=capture_profile,
        visual_profile=visual_profile,
        visual_profile_path=visual_profile_path,
        visual_profile_sha256=visual_profile_sha256,
        visual_profile_content_digest=visual_profile_content_digest,
        scratch_policy_root=scratch_policy_root,
        scratch_parent=scratch_parent,
        scratch_authority=scratch_authority,
    )


def _is_r2(inputs: CaptureInputs) -> bool:
    return inputs.capture_profile == R2_CAPTURE_PROFILE


def _capture_dimensions(inputs: CaptureInputs) -> tuple[int, int]:
    return (R2_WIDTH, R2_HEIGHT) if _is_r2(inputs) else (WIDTH, HEIGHT)


def _execution_schema(inputs: CaptureInputs) -> str:
    return R2_EXECUTION_SCHEMA if _is_r2(inputs) else EXECUTION_SCHEMA


def _worker_execution_schema(inputs: CaptureInputs) -> str:
    return R2_WORKER_EXECUTION_SCHEMA if _is_r2(inputs) else WORKER_EXECUTION_SCHEMA


def _ue_result_schema(inputs: CaptureInputs) -> str:
    return R2_UE_RESULT_SCHEMA if _is_r2(inputs) else UE_RESULT_SCHEMA


def build_execution(inputs: CaptureInputs) -> dict[str, Any]:
    """Build the immutable aggregate execution manifest.

    Per-child scratch paths deliberately do not live here: the host creates
    them only after the append-only output root exists, then binds each one in
    its own ordinal worker manifest.
    """

    output = inputs.output_dir
    width, height = _capture_dimensions(inputs)
    capture: dict[str, Any] = {
        "width": width,
        "height": height,
        "room_kinds": [camera["room_kind"] for camera in inputs.cameras],
        "cameras": [dict(camera) for camera in inputs.cameras],
    }
    policy: dict[str, Any] = {
        "append_only_output": True,
        "caller_python_allowed": False,
        "fixed_camera_actor_tags": True,
        "regular_editor_x11": True,
        "receipt_requires_host_png_validation": True,
        "sequential_owned_editor_children": True,
        "one_native_highres_shot_per_child": True,
        "native_png_uses_private_local_scratch": True,
    }
    execution = {
        "schema_version": _execution_schema(inputs),
        "attempt_root": str(inputs.attempt_root),
        "project": {"path": str(inputs.project), "sha256": inputs.project_sha256},
        "map_asset": {"path": str(inputs.map_asset), "sha256": inputs.map_asset_sha256},
        "build_plan": {
            "path": str(inputs.build_plan),
            "sha256": inputs.build_plan_sha256,
            "content_digest": inputs.plan["content_digest"],
        },
        "build_result": {
            "path": str(inputs.build_result),
            "sha256": inputs.build_result_sha256,
        },
        "engine": {
            "executable": str(inputs.unreal_editor),
            "executable_sha256": inputs.unreal_editor_sha256,
            "nvidia_icd": str(NVIDIA_VULKAN_ICD),
            "nvidia_icd_sha256": inputs.nvidia_icd_sha256,
            "required_version_prefix": EXPECTED_ENGINE_PREFIX,
        },
        "ddc_seed": (
            {"path": str(inputs.ddc_seed), "tree_sha256": inputs.ddc_seed_tree_sha256}
            if inputs.ddc_seed is not None
            else None
        ),
        "map_path": inputs.map_path,
        "output_root": str(output),
        "script": {"path": str(inputs.script), "sha256": inputs.script_sha256},
        "capture": capture,
        "artifacts": {
            "images_dir": str(output / IMAGES_DIR),
            "workers_dir": str(output / WORKERS_DIR),
            "receipt": str(output / RECEIPT_FILE),
        },
        "policy": policy,
    }
    if _is_r2(inputs):
        if (
            inputs.visual_profile is None
            or inputs.visual_profile_path is None
            or inputs.visual_profile_sha256 is None
            or inputs.visual_profile_content_digest is None
        ):
            _fail(
                "VISTA_HOME_REVIEW_VISUAL_PROFILE_INVALID",
                "r2 capture inputs lost their visual-profile binding",
            )
        execution["visual_profile"] = {
            "path": str(inputs.visual_profile_path),
            "sha256": inputs.visual_profile_sha256,
            "content_digest": inputs.visual_profile_content_digest,
        }
        if inputs.scratch_parent is None or inputs.scratch_authority is None:
            _fail(
                "VISTA_HOME_REVIEW_SCRATCH_INVALID",
                "r2 capture inputs lost their scratch-parent binding",
            )
        execution["scratch"] = _r2_scratch_parent_binding(
            inputs.scratch_authority,
        )
        execution["engine"]["graphics_adapter"] = 0
        execution["engine"]["display"] = R2_DISPLAY
        capture.update(
            {
                "profile_id": R2_CAPTURE_PROFILE,
                "shot_ids": [camera["camera_id"] for camera in inputs.cameras],
                "runtime_observation_status": "pending",
            }
        )
        policy.update(
            {
                "visual_profile_sha256_required": True,
                "graphics_adapter_zero_required": True,
                "runtime_camera_observations_required": True,
                "scratch_policy_root_required": True,
                "scratch_parent_required": True,
                "native_png_uses_private_nas_retained_evidence": True,
                "scratch_retained_append_only": True,
                "scratch_cleanup_descriptor_close_only": True,
            }
        )
        del policy["native_png_uses_private_local_scratch"]
    return execution


@dataclass(frozen=True)
class WorkerRun:
    ordinal: int
    camera: dict[str, Any]
    worker_dir: Path
    manifest_path: Path
    manifest_sha256: str
    scratch_dir: Path
    scratch_png: Path
    result_path: Path
    editor_log: Path
    editor_stdout: Path
    scratch_ownership: ScratchOwnership | None = None


@dataclass(frozen=True)
class WorkerSuccessProof:
    result_sha256: str
    png_sha256: str
    png_bytes: int


def _camera_for_ordinal(inputs: CaptureInputs, ordinal: int) -> dict[str, Any]:
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or not 1 <= ordinal <= len(inputs.cameras):
        _fail("VISTA_HOME_REVIEW_ORDINAL_INVALID", "worker ordinal is outside the fixed six-camera plan")
    camera = inputs.cameras[ordinal - 1]
    if camera["ordinal"] != ordinal:
        _fail("VISTA_HOME_REVIEW_ORDINAL_INVALID", "fixed camera ordinal ordering differs")
    return dict(camera)


def _validate_scratch_png(
    path: Path,
    *,
    ordinal: int,
    attempt_root: Path,
    require_parent: bool,
    r2_scratch_binding: Mapping[str, Any] | None = None,
    r2_worker_binding: Mapping[str, Any] | None = None,
    expected_scratch_parent: Path | None = None,
) -> Path:
    candidate = _absolute_lexical(path, "worker scratch PNG")
    if SAFE_LOCAL_PATH_RE.fullmatch(str(candidate)) is None or not str(candidate).isascii():
        _fail("VISTA_HOME_REVIEW_SCRATCH_INVALID", "scratch PNG path is not safe ASCII", pointer=str(candidate))
    if candidate.name != "capture.png" or candidate.parent.name != f"worker-{ordinal:02d}":
        _fail("VISTA_HOME_REVIEW_SCRATCH_INVALID", "scratch PNG path does not bind the immutable ordinal")
    scratch_root = candidate.parent.parent
    scratch_parent = scratch_root.parent
    if r2_scratch_binding is None:
        if r2_worker_binding is not None:
            _fail(
                "VISTA_HOME_REVIEW_EXECUTION_INVALID",
                "r1 scratch path cannot carry an r2 capability",
            )
        if (
            scratch_parent != LOCAL_SCRATCH_PARENT
            or not scratch_root.name.startswith(R1_SCRATCH_PREFIX)
        ):
            _fail(
                "VISTA_HOME_REVIEW_SCRATCH_INVALID",
                "scratch PNG must use a direct host-owned local scratch root",
            )
    else:
        capability = _validate_r2_worker_scratch_binding(
            r2_worker_binding,
            ordinal,
        )
        if _trees_overlap(scratch_parent, attempt_root):
            _fail(
                "VISTA_HOME_REVIEW_SCRATCH_INVALID",
                "r2 scratch parent and UE attempt trees must be disjoint",
            )
        if (
            expected_scratch_parent is not None
            and scratch_parent != expected_scratch_parent
        ):
            _fail(
                "VISTA_HOME_REVIEW_SCRATCH_INVALID",
                "r2 scratch PNG parent differs from validated inputs",
                pointer=str(scratch_parent),
            )
        if not scratch_root.name.startswith(R2_SCRATCH_PREFIX):
            _fail(
                "VISTA_HOME_REVIEW_SCRATCH_INVALID",
                "r2 scratch root prefix differs",
                pointer=str(scratch_root),
            )
        parent_fd = _open_observed_r2_parent(
            scratch_parent,
            r2_scratch_binding,
        )
        scratch_root_fd = -1
        worker_fd = -1
        try:
            scratch_root_fd, root_metadata = _open_directory_entry_at(
                parent_fd,
                scratch_root.name,
                "r2 scratch root",
            )
            worker_fd, worker_metadata = _open_directory_entry_at(
                scratch_root_fd,
                candidate.parent.name,
                "r2 worker directory",
            )
            expected_mount = (
                r2_scratch_binding["parent_mount_id"],
                r2_scratch_binding["filesystem_type"],
                r2_scratch_binding["mount_source_sha256"],
            )
            if (
                (root_metadata.st_dev, root_metadata.st_ino)
                != (
                    capability["scratch_root_device"],
                    capability["scratch_root_inode"],
                )
                or (worker_metadata.st_dev, worker_metadata.st_ino)
                != (capability["worker_device"], capability["worker_inode"])
                or root_metadata.st_uid
                != r2_scratch_binding["mapped_owner_uid"]
                or worker_metadata.st_uid != root_metadata.st_uid
                or stat.S_IMODE(root_metadata.st_mode) != 0o700
                or stat.S_IMODE(worker_metadata.st_mode) != 0o700
                or _fd_mount_identity(scratch_root_fd) != expected_mount
                or _fd_mount_identity(worker_fd) != expected_mount
            ):
                _fail(
                    "VISTA_HOME_REVIEW_SCRATCH_AUTHORITY_MISMATCH",
                    "r2 scratch path differs from retained host capability",
                )
            try:
                os.stat(
                    capability["png_name"],
                    dir_fd=worker_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                _fail(
                    "VISTA_HOME_REVIEW_OUTPUT_EXISTS",
                    "r2 native PNG already exists before capture",
                )
        finally:
            if worker_fd >= 0:
                os.close(worker_fd)
            if scratch_root_fd >= 0:
                os.close(scratch_root_fd)
            os.close(parent_fd)
    try:
        candidate.relative_to(attempt_root)
    except ValueError:
        pass
    else:
        _fail("VISTA_HOME_REVIEW_SCRATCH_INVALID", "scratch PNG must remain outside the UE attempt")
    if require_parent and r2_scratch_binding is None:
        root = _existing_directory(scratch_root, "scratch root")
        parent = _existing_directory(candidate.parent, "worker scratch directory")
        if stat.S_IMODE(os.lstat(root).st_mode) != 0o700 or stat.S_IMODE(os.lstat(parent).st_mode) != 0o700:
            _fail("VISTA_HOME_REVIEW_SCRATCH_INVALID", "scratch root or worker directory mode is not 0700")
    elif r2_scratch_binding is None:
        _reject_symlink_components(candidate, "worker scratch PNG", allow_missing_tail=True)
    return candidate


def build_worker_execution(
    inputs: CaptureInputs,
    aggregate_execution_sha256: str,
    ordinal: int,
    scratch_png: Path,
    scratch_ownership: ScratchOwnership | None = None,
) -> dict[str, Any]:
    if SHA256_RE.fullmatch(aggregate_execution_sha256) is None:
        _fail("VISTA_HOME_REVIEW_PIN_INVALID", "aggregate execution pin is invalid")
    if _is_r2(inputs) and (
        inputs.scratch_parent is None or inputs.scratch_authority is None
    ):
        _fail(
            "VISTA_HOME_REVIEW_SCRATCH_INVALID",
            "r2 capture inputs lost their scratch-parent binding",
        )
    camera = _camera_for_ordinal(inputs, ordinal)
    scratch_capability = None
    if _is_r2(inputs):
        if scratch_ownership is None:
            _fail(
                "VISTA_HOME_REVIEW_SCRATCH_OWNERSHIP_MISMATCH",
                "r2 worker manifest requires retained scratch ownership",
            )
        scratch_capability = _r2_worker_scratch_binding(
            scratch_ownership,
            ordinal,
        )
    scratch = _validate_scratch_png(
        scratch_png,
        ordinal=ordinal,
        attempt_root=inputs.attempt_root,
        require_parent=True,
        r2_scratch_binding=(
            _r2_scratch_parent_binding(
                inputs.scratch_authority,
            )
            if _is_r2(inputs) and inputs.scratch_authority is not None
            else None
        ),
        r2_worker_binding=scratch_capability,
        expected_scratch_parent=(
            inputs.scratch_parent if _is_r2(inputs) else None
        ),
    )
    worker_dir = inputs.output_dir / WORKERS_DIR / f"{ordinal:02d}"
    manifest = {
        "schema_version": _worker_execution_schema(inputs),
        "aggregate_execution": {
            "path": str(inputs.output_dir / EXECUTION_FILE),
            "sha256": aggregate_execution_sha256,
        },
        "ordinal": ordinal,
        "camera": camera,
        "scratch_png": str(scratch),
        "artifacts": {
            "ue_result": str(worker_dir / UE_RESULT_FILE),
            "editor_log": str(worker_dir / EDITOR_LOG_FILE),
            "editor_stdout": str(worker_dir / EDITOR_STDOUT_FILE),
            "final_image": str(inputs.output_dir / camera["relative_path"]),
        },
        "policy": {
            "immutable_ordinal": True,
            "exactly_one_camera_capture": True,
            "at_most_one_native_highres_shot": True,
            "host_accepts_and_copies_png": True,
        },
    }
    if _is_r2(inputs):
        manifest["scratch_capability"] = scratch_capability
    return manifest


def build_editor_command(inputs: CaptureInputs, ordinal: int = 1) -> list[str]:
    worker_dir = inputs.output_dir / WORKERS_DIR / f"{ordinal:02d}"
    width, height = _capture_dimensions(inputs)
    return [
        str(inputs.unreal_editor),
        str(inputs.project),
        inputs.map_path,
        f"-ExecutePythonScript={inputs.script}",
        "-unattended",
        "-Windowed",
        "-ForceRes",
        f"-ResX={width}",
        f"-ResY={height}",
        f"-graphicsadapter={inputs.graphics_adapter}",
        "-NOSPLASH",
        "-NOSOUND",
        "-NoAnalytics",
        "-UDPMESSAGING_TRANSPORT_ENABLE=0",
        "-ini:Engine:[/Script/TcpMessaging.TcpMessagingSettings]:EnableTransport=False",
        "-ini:EditorPerProjectUserSettings:[/Script/UnrealEd.EditorLoadingSavingSettings]:bAutoSaveEnable=False",
        "-ddc=InstalledNoZenLocalFallback",
        "-ExecCmds=t.MaxFPS 60",
        "-SaveToUserDir",
        f"-UserDir={inputs.output_dir / 'ue-user'}",
        f"-LocalDataCachePath={inputs.output_dir / 'ddc'}",
        f"-abslog={worker_dir / EDITOR_LOG_FILE}",
        "-stdout",
        "-FullStdOutLogOutput",
    ]


def _prepare_output(inputs: CaptureInputs, execution_raw: bytes) -> None:
    parent_fd = -1
    output_fd = -1
    try:
        _, parent_fd, _ = _open_directory_fd(
            inputs.output_dir.parent,
            "review output parent",
        )
        output_fd, _ = _mkdir_private_directory_at(
            parent_fd,
            inputs.output_dir.name,
            "review output attempt",
        )
        directory_names = [
            IMAGES_DIR,
            WORKERS_DIR,
            "ue-user",
            "ddc",
            "xdg-cache",
            "xdg-config",
        ]
        if _is_r2(inputs):
            directory_names.extend(("tmp", "xdg-data"))
        for name in directory_names:
            child_fd, _ = _mkdir_private_directory_at(
                output_fd,
                name,
                f"review output {name} directory",
            )
            os.close(child_fd)
    except FileExistsError:
        _fail("VISTA_HOME_REVIEW_OUTPUT_EXISTS", "append-only output attempt already exists", pointer=str(inputs.output_dir))
    except OSError as exc:
        _fail("VISTA_HOME_REVIEW_OUTPUT_CREATE_FAILED", f"cannot create output attempt: {exc}", pointer=str(inputs.output_dir))
    finally:
        if output_fd >= 0:
            os.close(output_fd)
        if parent_fd >= 0:
            os.close(parent_fd)
    _write_exclusive(inputs.output_dir / EXECUTION_FILE, execution_raw)
    if inputs.ddc_seed is not None:
        target_root = (
            inputs.output_dir
            / "ue-user/.config/Epic/UnrealEngine/Common/DerivedDataCache"
        )
        target_root.mkdir(mode=0o700, parents=True, exist_ok=False)
        for source in sorted(inputs.ddc_seed.rglob("*"), key=lambda item: item.relative_to(inputs.ddc_seed).as_posix()):
            relative = source.relative_to(inputs.ddc_seed)
            target = target_root / relative
            if source.is_dir():
                target.mkdir(mode=0o700, exist_ok=False)
                continue
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                with source.open("rb") as reader, os.fdopen(descriptor, "wb", closefd=False) as writer:
                    shutil.copyfileobj(reader, writer, length=1024 * 1024)
                    writer.flush()
                    os.fsync(writer.fileno())
            finally:
                os.close(descriptor)


def _owned_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        _fail("VISTA_HOME_REVIEW_EDITOR_LIFECYCLE_FAILED", "owned editor process group became inaccessible")
    return True


def _wait_owned_group_exit(process_group_id: int, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while _owned_group_exists(process_group_id) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not _owned_group_exists(process_group_id)


def _terminate_owned(process: subprocess.Popen[bytes]) -> None:
    """Terminate the entire start_new_session process group, even if its leader exited."""

    process_group_id = process.pid
    if _owned_group_exists(process_group_id):
        try:
            os.killpg(process_group_id, signal.SIGTERM)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
    if not _wait_owned_group_exit(process_group_id, 2.0):
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if process.poll() is None:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                _fail("VISTA_HOME_REVIEW_EDITOR_LIFECYCLE_FAILED", "owned editor leader survived SIGKILL")
        if not _wait_owned_group_exit(process_group_id, 2.0):
            _fail("VISTA_HOME_REVIEW_EDITOR_LIFECYCLE_FAILED", "owned editor process group survived SIGKILL")


def build_editor_environment(
    inputs: CaptureInputs,
    worker_manifest: Path,
    worker_manifest_sha256: str,
) -> dict[str, str]:
    """Return the minimum fixed environment needed by the owned editor.

    Provider credentials, database URLs, Codex state, SSH metadata, and other
    ambient process variables must never be forwarded into Unreal.
    """

    env = {
        key: value
        for key in PASSTHROUGH_ENV_KEYS
        if (value := os.environ.get(key)) is not None
    }
    env.update(
        {
            "DISPLAY": inputs.display,
            "HOME": str(inputs.output_dir / "ue-user"),
            "XDG_CACHE_HOME": str(inputs.output_dir / "xdg-cache"),
            "XDG_CONFIG_HOME": str(inputs.output_dir / "xdg-config"),
            "SDL_VIDEODRIVER": "x11",
            "VK_ICD_FILENAMES": str(NVIDIA_VULKAN_ICD),
            "__GLX_VENDOR_LIBRARY_NAME": "nvidia",
            WORKER_ENV: "1",
            EXECUTION_ENV: str(worker_manifest),
            EXECUTION_SHA_ENV: worker_manifest_sha256,
        }
    )
    if _is_r2(inputs):
        env.update(
            {
                "TMPDIR": str(inputs.output_dir / "tmp"),
                "TMP": str(inputs.output_dir / "tmp"),
                "TEMP": str(inputs.output_dir / "tmp"),
                "XDG_DATA_HOME": str(inputs.output_dir / "xdg-data"),
            }
        )
    return env


def run_editor(inputs: CaptureInputs, worker: WorkerRun) -> int:
    if not NVIDIA_VULKAN_ICD.is_file():
        _fail(
            "VISTA_HOME_REVIEW_ENGINE_INVALID",
            "pinned NVIDIA Vulkan ICD is unavailable",
            pointer=str(NVIDIA_VULKAN_ICD),
        )
    if _is_r2(inputs):
        if worker.scratch_ownership is None:
            _fail(
                "VISTA_HOME_REVIEW_SCRATCH_OWNERSHIP_MISMATCH",
                "r2 editor launch lost retained scratch ownership",
            )
        _validate_owned_r2_worker_dir(worker.scratch_ownership, worker.ordinal)
    env = build_editor_environment(inputs, worker.manifest_path, worker.manifest_sha256)
    command = build_editor_command(inputs, worker.ordinal)
    stdout_path = worker.editor_stdout
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
        terminated_after_proof = False
        try:
            deadline = time.monotonic() + inputs.timeout_seconds
            previous_proof: WorkerSuccessProof | None = None
            proof_first_seen = 0.0
            proof_observations = 0
            while True:
                returncode = process.poll()
                if returncode is not None:
                    return returncode
                now = time.monotonic()
                if now >= deadline:
                    _fail("VISTA_HOME_REVIEW_EDITOR_TIMEOUT", "UnrealEditor did not finish the fixed capture before timeout")
                proof = _probe_worker_success(inputs, worker)
                if proof is None:
                    previous_proof = None
                    proof_first_seen = 0.0
                    proof_observations = 0
                elif proof != previous_proof:
                    previous_proof = proof
                    proof_first_seen = now
                    proof_observations = 1
                else:
                    proof_observations += 1
                    if (
                        proof_observations >= 2
                        and now - proof_first_seen >= WORKER_PROOF_STABILITY_SECONDS
                    ):
                        # Do not mask a real early process failure that raced
                        # with the second proof observation.  Once the child is
                        # still live and the proof is stable, terminate only its
                        # owned process group and synthesize success; the caller
                        # immediately repeats all final validation gates.
                        returncode = process.poll()
                        if returncode is not None:
                            return returncode
                        _terminate_owned(process)
                        terminated_after_proof = True
                        return 0
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(min(WORKER_PROOF_POLL_INTERVAL_SECONDS, remaining))
        finally:
            # Covers timeout, host cancellation/KeyboardInterrupt, unexpected
            # exceptions, and helper descendants left after a normal leader
            # exit.  No owned Unreal process group may outlive this call.
            if not terminated_after_proof:
                _terminate_owned(process)
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
    raw = source.read_bytes()
    return inspect_png_bytes(
        raw,
        expected_width=expected_width,
        expected_height=expected_height,
        source_label=str(source),
    )


def inspect_png_bytes(
    raw: bytes,
    *,
    expected_width: int = WIDTH,
    expected_height: int = HEIGHT,
    source_label: str = "<PNG bytes>",
) -> PngInspection:
    """Strictly decode the exact bytes that the host will later copy."""

    source = source_label
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_PNG_BYTES:
        _fail("VISTA_HOME_REVIEW_PNG_INVALID", "PNG size is outside safety bound", pointer=str(source))
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


def _read_exact_regular(path: Path, label: str) -> bytes:
    source = _existing_file(path, label)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(source, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_PNG_BYTES:
            _fail("VISTA_HOME_REVIEW_PNG_INVALID", f"{label} size or type is invalid", pointer=str(source))
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            block = os.read(descriptor, min(1024 * 1024, remaining))
            if not block:
                _fail("VISTA_HOME_REVIEW_PNG_INVALID", f"{label} ended before its pinned size", pointer=str(source))
            chunks.append(block)
            remaining -= len(block)
        if os.read(descriptor, 1):
            _fail("VISTA_HOME_REVIEW_PNG_INVALID", f"{label} grew during host acceptance", pointer=str(source))
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            _fail("VISTA_HOME_REVIEW_PNG_INVALID", f"{label} changed during host acceptance", pointer=str(source))
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _load_worker_result(inputs: CaptureInputs, worker: WorkerRun) -> tuple[dict[str, Any], str]:
    _load_json(
        worker.manifest_path,
        label=f"worker execution manifest {worker.ordinal}",
        expected_sha256=worker.manifest_sha256,
    )
    result, raw = _load_json(worker.result_path, label=f"Unreal capture result {worker.ordinal}")
    expected_keys = {
        "schema_version",
        "status",
        "captured_at",
        "engine_version",
        "project_path",
        "map_path",
        "execution_sha256",
        "worker_ordinal",
        "camera_actor_set_exact",
        "captures",
        "error",
    }
    if (
        set(result) != expected_keys
        or result.get("schema_version") != _ue_result_schema(inputs)
    ):
        _fail("VISTA_HOME_REVIEW_UE_RESULT_INVALID", "Unreal result fields or schema differ")
    if result.get("status") != "captured_candidate" or result.get("error") is not None:
        error = result.get("error")
        _fail("VISTA_HOME_REVIEW_UE_CAPTURE_FAILED", f"Unreal rejected capture: {error}")
    if result.get("execution_sha256") != worker.manifest_sha256 or result.get("worker_ordinal") != worker.ordinal:
        _fail("VISTA_HOME_REVIEW_UE_RESULT_INVALID", "Unreal result execution binding differs")
    if result.get("project_path") != str(inputs.project) or result.get("map_path") != inputs.map_path:
        _fail("VISTA_HOME_REVIEW_UE_RESULT_INVALID", "Unreal loaded project or map differs")
    engine_version = result.get("engine_version")
    if not isinstance(engine_version, str) or not engine_version.startswith(EXPECTED_ENGINE_PREFIX):
        _fail("VISTA_HOME_REVIEW_ENGINE_INVALID", "Unreal capture did not use UE 5.7")
    if result.get("camera_actor_set_exact") is not True:
        _fail("VISTA_HOME_REVIEW_CAMERA_SET_INVALID", "materialized camera actor set was not exact")
    captures = result.get("captures")
    if not isinstance(captures, list) or len(captures) != 1:
        _fail("VISTA_HOME_REVIEW_UE_RESULT_INVALID", "each Unreal child must report exactly one capture")
    capture_keys = {
        "ordinal",
        "room_kind",
        "room_id",
        "camera_id",
        "semantic_id",
        "actor_label",
        "capture_method",
        "actual_transform",
        "actual_fov_deg",
        "relative_path",
        "bytes",
        "sha256",
        "native_png_path",
    }
    expected = worker.camera
    capture = captures[0]
    if not isinstance(capture, Mapping) or set(capture) != capture_keys:
        _fail("VISTA_HOME_REVIEW_UE_RESULT_INVALID", "Unreal capture fields differ")
    for key in ("ordinal", "room_kind", "room_id", "camera_id", "semantic_id", "relative_path"):
        if capture.get(key) != expected[key]:
            _fail("VISTA_HOME_REVIEW_UE_RESULT_INVALID", f"Unreal capture {key} differs")
    if capture.get("native_png_path") != str(worker.scratch_png):
        _fail("VISTA_HOME_REVIEW_UE_RESULT_INVALID", "Unreal native PNG path differs from host scratch binding")
    if not isinstance(capture.get("actor_label"), str) or not capture["actor_label"]:
        _fail("VISTA_HOME_REVIEW_UE_RESULT_INVALID", "Unreal capture actor label is invalid")
    if capture.get("capture_method") != CAPTURE_METHOD:
        _fail("VISTA_HOME_REVIEW_UE_RESULT_INVALID", "Unreal capture did not pilot the fixed CameraActor")
    actual_transform = capture.get("actual_transform")
    if not isinstance(actual_transform, Mapping) or set(actual_transform) != {"location_cm", "rotation_deg", "scale"}:
        _fail("VISTA_HOME_REVIEW_UE_RESULT_INVALID", "Unreal capture transform is invalid")
    try:
        actual_transform = _transform(actual_transform, "Unreal capture transform")
    except (KeyError, TypeError):
        _fail("VISTA_HOME_REVIEW_UE_RESULT_INVALID", "Unreal capture transform is invalid")
    if not _transform_matches(actual_transform, expected["expected_transform"]):
        _fail("VISTA_HOME_REVIEW_CAMERA_DRIFT", "Unreal capture transform differs from the fixed camera")
    actual_fov = capture.get("actual_fov_deg")
    if (
        isinstance(actual_fov, bool)
        or not isinstance(actual_fov, (int, float))
        or not math.isfinite(float(actual_fov))
        or abs(float(actual_fov) - float(expected["expected_fov_deg"])) > 0.05
    ):
        _fail("VISTA_HOME_REVIEW_CAMERA_DRIFT", "Unreal capture FOV differs from the fixed camera")
    byte_count = capture.get("bytes")
    if isinstance(byte_count, bool) or not isinstance(byte_count, int) or not 0 < byte_count <= MAX_PNG_BYTES:
        _fail("VISTA_HOME_REVIEW_UE_RESULT_INVALID", "Unreal capture byte count is invalid")
    if not isinstance(capture.get("sha256"), str) or SHA256_RE.fullmatch(capture["sha256"]) is None:
        _fail("VISTA_HOME_REVIEW_UE_RESULT_INVALID", "Unreal capture SHA-256 is invalid")
    return result, sha256_bytes(raw)


def _probe_worker_success(inputs: CaptureInputs, worker: WorkerRun) -> WorkerSuccessProof | None:
    """Return a strict proof observation without copying or accepting artifacts.

    Result and PNG files are visible before their writers necessarily finish,
    so every parse/read/decode failure is treated as "not ready".  The caller
    requires repeated identical observations before it may stop the owned UE
    process.  Final acceptance still happens later through the normal result,
    PNG, pin, distinctness, and receipt gates.
    """

    if not worker.result_path.exists():
        return None
    try:
        result, result_sha256 = _load_worker_result(inputs, worker)
        if _is_r2(inputs):
            if worker.scratch_ownership is None:
                _fail(
                    "VISTA_HOME_REVIEW_SCRATCH_OWNERSHIP_MISMATCH",
                    "r2 PNG proof lost retained scratch ownership",
                )
            raw = _read_owned_r2_worker_png(
                worker.scratch_ownership,
                worker.ordinal,
                "native worker PNG proof",
            )
        else:
            if not worker.scratch_png.exists():
                return None
            raw = _read_exact_regular(worker.scratch_png, "native worker PNG proof")
        capture_result = result["captures"][0]
        png_sha256 = sha256_bytes(raw)
        if capture_result["bytes"] != len(raw) or capture_result["sha256"] != png_sha256:
            return None
        width, height = _capture_dimensions(inputs)
        inspect_png_bytes(
            raw,
            expected_width=width,
            expected_height=height,
            source_label=str(worker.scratch_png),
        )
    except (OSError, ReviewCaptureError):
        return None
    return WorkerSuccessProof(
        result_sha256=result_sha256,
        png_sha256=png_sha256,
        png_bytes=len(raw),
    )


def _verify_input_pins(inputs: CaptureInputs) -> None:
    pins: tuple[tuple[Path, str, str], ...] = (
        (inputs.project, inputs.project_sha256, "project"),
        (inputs.map_asset, inputs.map_asset_sha256, "materialized map asset"),
        (inputs.build_plan, inputs.build_plan_sha256, "build plan"),
        (inputs.build_result, inputs.build_result_sha256, "accepted UE build result"),
        (inputs.script, inputs.script_sha256, "fixed capture script"),
        (inputs.unreal_editor, inputs.unreal_editor_sha256, "UnrealEditor"),
        (NVIDIA_VULKAN_ICD, inputs.nvidia_icd_sha256, "NVIDIA Vulkan ICD"),
    )
    if _is_r2(inputs):
        if inputs.visual_profile_path is None or inputs.visual_profile_sha256 is None:
            _fail(
                "VISTA_HOME_REVIEW_VISUAL_PROFILE_INVALID",
                "r2 capture inputs lost their visual-profile pin",
            )
        pins += (
            (
                inputs.visual_profile_path,
                inputs.visual_profile_sha256,
                "r2 visual profile",
            ),
        )
    for path, expected, label in pins:
        if sha256_file(_existing_file(path, label)) != expected:
            _fail("VISTA_HOME_REVIEW_PIN_MISMATCH", f"{label} changed during capture")
    _validate_build_result(
        inputs.build_result,
        inputs.attempt_root,
        inputs.map_path,
        visual_profile_id=R2_CAPTURE_PROFILE if _is_r2(inputs) else None,
        visual_profile_sha256=inputs.visual_profile_sha256,
        visual_profile_content_digest=inputs.visual_profile_content_digest,
    )


def _inspection_dict(inspection: PngInspection) -> dict[str, Any]:
    return {
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
    }


def _accept_worker_png(
    inputs: CaptureInputs,
    worker: WorkerRun,
    ue_result: Mapping[str, Any],
) -> dict[str, Any]:
    if _is_r2(inputs) and (
        inputs.scratch_parent is None or inputs.scratch_authority is None
    ):
        _fail(
            "VISTA_HOME_REVIEW_SCRATCH_INVALID",
            "r2 capture inputs lost their scratch-parent binding",
        )
    capture = ue_result["captures"][0]
    if _is_r2(inputs):
        if worker.scratch_ownership is None:
            _fail(
                "VISTA_HOME_REVIEW_SCRATCH_OWNERSHIP_MISMATCH",
                "r2 PNG acceptance lost retained scratch ownership",
            )
        raw = _read_owned_r2_worker_png(
            worker.scratch_ownership,
            worker.ordinal,
            "native worker PNG",
        )
    else:
        _validate_scratch_png(
            worker.scratch_png,
            ordinal=worker.ordinal,
            attempt_root=inputs.attempt_root,
            require_parent=True,
        )
        raw = _read_exact_regular(worker.scratch_png, "native worker PNG")
    source_sha = sha256_bytes(raw)
    if capture.get("bytes") != len(raw) or capture.get("sha256") != source_sha:
        _fail("VISTA_HOME_REVIEW_UE_RESULT_INVALID", "Unreal native PNG size or hash differs")
    width, height = _capture_dimensions(inputs)
    inspection = inspect_png_bytes(
        raw,
        expected_width=width,
        expected_height=height,
        source_label=str(worker.scratch_png),
    )
    final_path = inputs.output_dir / worker.camera["relative_path"]
    _require_child(final_path, inputs.output_dir, "final PNG")
    try:
        _write_exclusive(final_path, raw)
    except FileExistsError:
        _fail("VISTA_HOME_REVIEW_OUTPUT_EXISTS", "accepted final PNG already exists", pointer=str(final_path))
    accepted_raw = _read_exact_regular(final_path, "accepted final PNG")
    accepted_sha = sha256_bytes(accepted_raw)
    if accepted_raw != raw or accepted_sha != source_sha:
        _fail("VISTA_HOME_REVIEW_PNG_COPY_MISMATCH", "accepted PNG bytes differ from native scratch bytes")
    inspect_png_bytes(
        accepted_raw,
        expected_width=width,
        expected_height=height,
        source_label=str(final_path),
    )
    image = {
        "ordinal": worker.camera["ordinal"],
        "room_kind": worker.camera["room_kind"],
        "room_id": worker.camera["room_id"],
        "camera_id": worker.camera["camera_id"],
        "semantic_id": worker.camera["semantic_id"],
        "actor_label": capture.get("actor_label"),
        "capture_method": capture.get("capture_method"),
        "actual_transform": capture.get("actual_transform"),
        "actual_fov_deg": capture.get("actual_fov_deg"),
        "path": final_path.relative_to(inputs.output_dir).as_posix(),
        "bytes": len(accepted_raw),
        "sha256": accepted_sha,
        "native_and_final_sha256_equal": True,
        "png": _inspection_dict(inspection),
    }
    if _is_r2(inputs):
        image.update(
            {
                "purpose": worker.camera["purpose"],
                "visual_profile_id": R2_CAPTURE_PROFILE,
                "runtime_observation": {
                    "status": "pending",
                    "required_gates": [
                        "near_field_clearance",
                        "foreground_occlusion",
                        "expected_hero_visibility",
                        "forbidden_foreground",
                    ],
                },
            }
        )
    return image


@dataclass(frozen=True)
class WorkerOutcome:
    worker: WorkerRun
    ue_result: dict[str, Any]
    ue_result_sha256: str
    image: dict[str, Any]


@dataclass
class ScratchOwnership:
    """Single owner for retained r2 descriptors or one legacy r1 path."""

    path: Path
    parent: Path
    attempt_root: Path
    capture_profile: str
    parent_device: int
    parent_inode: int
    device: int
    inode: int
    policy_root: Path | None = None
    policy_root_fd: int | None = None
    parent_fd: int | None = None
    child_fd: int | None = None
    parent_relative_parts: tuple[str, ...] = ()
    worker_fds: dict[int, int] = field(default_factory=dict)
    mount_id: int | None = None
    filesystem_type: str | None = None
    mount_source_sha256: str | None = None
    mapped_owner_uid: int | None = None
    closed: bool = False

    def register_worker_fd(self, ordinal: int, descriptor: int) -> None:
        if self.closed or ordinal in self.worker_fds:
            _fail(
                "VISTA_HOME_REVIEW_SCRATCH_OWNERSHIP_MISMATCH",
                "r2 scratch worker descriptor ownership differs",
            )
        self.worker_fds[ordinal] = descriptor

    def worker_fd(self, ordinal: int) -> int:
        if self.closed or ordinal not in self.worker_fds:
            _fail(
                "VISTA_HOME_REVIEW_SCRATCH_OWNERSHIP_MISMATCH",
                "r2 scratch worker descriptor is unavailable",
            )
        return self.worker_fds[ordinal]

    def close(self) -> None:
        descriptors = list(self.worker_fds.values())
        self.worker_fds.clear()
        for field_name in ("child_fd", "parent_fd", "policy_root_fd"):
            descriptor = getattr(self, field_name)
            setattr(self, field_name, None)
            if descriptor is not None:
                descriptors.append(descriptor)
        self.closed = True
        for descriptor in dict.fromkeys(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _validate_owned_r2_scratch(ownership: ScratchOwnership) -> os.stat_result:
    if (
        ownership.closed
        or ownership.policy_root is None
        or ownership.policy_root_fd is None
        or ownership.parent_fd is None
        or ownership.child_fd is None
        or ownership.mount_id is None
        or ownership.filesystem_type is None
        or ownership.mount_source_sha256 is None
        or ownership.mapped_owner_uid is None
        or not ownership.parent_relative_parts
    ):
        _fail("VISTA_HOME_REVIEW_SCRATCH_OWNERSHIP_MISMATCH", "r2 scratch descriptors are missing")
    root = os.fstat(ownership.policy_root_fd)
    parent = os.fstat(ownership.parent_fd)
    child = os.fstat(ownership.child_fd)
    expected_mount = (
        ownership.mount_id,
        ownership.filesystem_type,
        ownership.mount_source_sha256,
    )
    _require_r2_nas_mount(expected_mount)
    _validate_directory_path_entry(
        ownership.policy_root,
        ownership.policy_root_fd,
        "r2 scratch policy root",
    )
    _validate_relative_directory_entry(
        ownership.policy_root_fd,
        ownership.parent_relative_parts,
        ownership.parent_fd,
        "r2 scratch parent",
    )
    try:
        entry = os.stat(ownership.path.name, dir_fd=ownership.parent_fd, follow_symlinks=False)
    except OSError as exc:
        _fail("VISTA_HOME_REVIEW_SCRATCH_OWNERSHIP_MISMATCH", f"r2 scratch entry is missing: {exc}")
    if (
        (parent.st_dev, parent.st_ino) != (ownership.parent_device, ownership.parent_inode)
        or (child.st_dev, child.st_ino) != (ownership.device, ownership.inode)
        or (entry.st_dev, entry.st_ino) != (ownership.device, ownership.inode)
        or root.st_dev != parent.st_dev
        or parent.st_dev != child.st_dev
        or not stat.S_ISDIR(child.st_mode)
        or any(stat.S_IMODE(item.st_mode) != 0o700 for item in (root, parent, child))
        or any(
            _fd_mount_identity(fd) != expected_mount
            for fd in (
                ownership.policy_root_fd,
                ownership.parent_fd,
                ownership.child_fd,
            )
        )
        or any(item.st_uid != ownership.mapped_owner_uid for item in (root, parent, child))
    ):
        _fail("VISTA_HOME_REVIEW_SCRATCH_OWNERSHIP_MISMATCH", "r2 scratch descriptor or entry changed")
    return child


def _validate_owned_r2_worker_dir(
    ownership: ScratchOwnership,
    ordinal: int,
) -> os.stat_result:
    root = _validate_owned_r2_scratch(ownership)
    if ownership.child_fd is None:
        raise AssertionError("validated r2 child descriptor is missing")
    worker_fd = ownership.worker_fd(ordinal)
    name = f"worker-{ordinal:02d}"
    try:
        entry = os.stat(name, dir_fd=ownership.child_fd, follow_symlinks=False)
        opened = os.fstat(worker_fd)
    except OSError as exc:
        _fail(
            "VISTA_HOME_REVIEW_SCRATCH_OWNERSHIP_MISMATCH",
            f"r2 worker directory entry is unavailable: {exc}",
        )
    expected_mount = (
        ownership.mount_id,
        ownership.filesystem_type,
        ownership.mount_source_sha256,
    )
    if (
        not _same_inode(entry, opened)
        or not stat.S_ISDIR(opened.st_mode)
        or stat.S_IMODE(opened.st_mode) != 0o700
        or opened.st_dev != root.st_dev
        or opened.st_uid != ownership.mapped_owner_uid
        or _fd_mount_identity(worker_fd) != expected_mount
    ):
        _fail(
            "VISTA_HOME_REVIEW_SCRATCH_OWNERSHIP_MISMATCH",
            "r2 worker directory descriptor or entry changed",
        )
    return opened


def _r2_worker_scratch_binding(
    ownership: ScratchOwnership,
    ordinal: int,
) -> dict[str, Any]:
    root = _validate_owned_r2_scratch(ownership)
    worker = _validate_owned_r2_worker_dir(ownership, ordinal)
    return {
        "lifecycle": R2_SCRATCH_LIFECYCLE,
        "scratch_root_device": root.st_dev,
        "scratch_root_inode": root.st_ino,
        "worker_device": worker.st_dev,
        "worker_inode": worker.st_ino,
        "worker_name": f"worker-{ordinal:02d}",
        "png_name": "capture.png",
    }


def _validate_r2_worker_scratch_binding(
    value: Any,
    ordinal: int,
) -> dict[str, Any]:
    expected_keys = {
        "lifecycle",
        "scratch_root_device",
        "scratch_root_inode",
        "worker_device",
        "worker_inode",
        "worker_name",
        "png_name",
    }
    if not isinstance(value, Mapping) or set(value) != expected_keys:
        _fail(
            "VISTA_HOME_REVIEW_EXECUTION_INVALID",
            "r2 worker scratch-capability fields differ",
        )
    integer_keys = (
        "scratch_root_device",
        "scratch_root_inode",
        "worker_device",
        "worker_inode",
    )
    if (
        value.get("lifecycle") != R2_SCRATCH_LIFECYCLE
        or any(
            isinstance(value.get(key), bool)
            or not isinstance(value.get(key), int)
            or value[key] < 0
            for key in integer_keys
        )
        or value["scratch_root_inode"] <= 0
        or value["worker_inode"] <= 0
        or value["scratch_root_device"] != value["worker_device"]
        or value.get("worker_name") != f"worker-{ordinal:02d}"
        or value.get("png_name") != "capture.png"
    ):
        _fail(
            "VISTA_HOME_REVIEW_EXECUTION_INVALID",
            "r2 worker scratch-capability policy differs",
        )
    return dict(value)


def _read_owned_r2_worker_png(
    ownership: ScratchOwnership,
    ordinal: int,
    label: str,
) -> bytes:
    _validate_owned_r2_worker_dir(ownership, ordinal)
    worker_fd = ownership.worker_fd(ordinal)
    descriptor = -1
    try:
        descriptor = os.open(
            "capture.png",
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=worker_fd,
        )
        opened = os.fstat(descriptor)
        entry = os.stat("capture.png", dir_fd=worker_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not _same_inode(opened, entry)
            or not 0 < opened.st_size <= MAX_PNG_BYTES
        ):
            _fail(
                "VISTA_HOME_REVIEW_SCRATCH_OWNERSHIP_MISMATCH",
                f"{label} entry does not match its retained descriptor",
            )
        chunks: list[bytes] = []
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                _fail("VISTA_HOME_REVIEW_PNG_TRUNCATED", f"{label} ended early")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            _fail("VISTA_HOME_REVIEW_PNG_TOO_LARGE", f"{label} grew while reading")
        final = os.fstat(descriptor)
        final_entry = os.stat(
            "capture.png",
            dir_fd=worker_fd,
            follow_symlinks=False,
        )
        if (
            not _same_inode(opened, final)
            or not _same_inode(final, final_entry)
            or opened.st_size != final.st_size
            or opened.st_mtime_ns != final.st_mtime_ns
            or opened.st_ctime_ns != final.st_ctime_ns
        ):
            _fail(
                "VISTA_HOME_REVIEW_SCRATCH_OWNERSHIP_MISMATCH",
                f"{label} changed while reading",
            )
        raw = b"".join(chunks)
    except OSError as exc:
        _fail("VISTA_HOME_REVIEW_PATH_MISSING", f"{label} is unavailable: {exc}")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    _validate_owned_r2_worker_dir(ownership, ordinal)
    return raw


def _validate_owned_scratch(
    ownership: ScratchOwnership,
    *,
    require_exists: bool = True,
) -> os.stat_result | None:
    if not isinstance(ownership, ScratchOwnership):
        _fail(
            "VISTA_HOME_REVIEW_SCRATCH_INVALID",
            "scratch cleanup requires an ownership record",
        )
    if ownership.capture_profile == R2_CAPTURE_PROFILE:
        return _validate_owned_r2_scratch(ownership)
    parent = _existing_directory(ownership.parent, "owned scratch parent")
    parent_metadata = os.lstat(parent)
    if (
        parent_metadata.st_dev != ownership.parent_device
        or parent_metadata.st_ino != ownership.parent_inode
    ):
        _fail(
            "VISTA_HOME_REVIEW_SCRATCH_OWNERSHIP_MISMATCH",
            "scratch parent device/inode changed",
            pointer=str(parent),
        )
    if ownership.capture_profile == R1_CAPTURE_PROFILE:
        if parent != LOCAL_SCRATCH_PARENT:
            _fail(
                "VISTA_HOME_REVIEW_SCRATCH_OWNERSHIP_MISMATCH",
                "r1 scratch ownership parent differs",
                pointer=str(parent),
            )
        expected_prefix = R1_SCRATCH_PREFIX
    else:
        _fail(
            "VISTA_HOME_REVIEW_SCRATCH_OWNERSHIP_MISMATCH",
            "scratch ownership capture profile differs",
        )
    if (
        ownership.path.parent != parent
        or not ownership.path.name.startswith(expected_prefix)
        or SAFE_LOCAL_PATH_RE.fullmatch(str(ownership.path)) is None
        or not str(ownership.path).isascii()
    ):
        _fail(
            "VISTA_HOME_REVIEW_SCRATCH_OWNERSHIP_MISMATCH",
            "scratch ownership path differs",
            pointer=str(ownership.path),
        )
    try:
        metadata = os.lstat(ownership.path)
    except FileNotFoundError:
        if not require_exists:
            return None
        _fail(
            "VISTA_HOME_REVIEW_SCRATCH_OWNERSHIP_MISMATCH",
            "owned scratch child is missing",
            pointer=str(ownership.path),
        )
        raise AssertionError("unreachable")
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_dev != ownership.device
        or metadata.st_ino != ownership.inode
    ):
        _fail(
            "VISTA_HOME_REVIEW_SCRATCH_OWNERSHIP_MISMATCH",
            "scratch child device/inode changed",
            pointer=str(ownership.path),
        )
    return metadata


def build_receipt(
    inputs: CaptureInputs,
    execution_sha256: str,
    outcomes: Sequence[WorkerOutcome],
    *,
    scratch_ownership: ScratchOwnership | None = None,
) -> dict[str, Any]:
    _verify_input_pins(inputs)
    _load_json(
        inputs.output_dir / EXECUTION_FILE,
        label="aggregate execution manifest",
        expected_sha256=execution_sha256,
    )
    if len(outcomes) != len(inputs.cameras):
        _fail("VISTA_HOME_REVIEW_UE_RESULT_INVALID", "receipt requires all six worker outcomes")
    if [item.worker.ordinal for item in outcomes] != list(range(1, len(inputs.cameras) + 1)):
        _fail("VISTA_HOME_REVIEW_ORDINAL_INVALID", "worker outcomes are not the fixed sequential ordinals")
    images = [dict(item.image) for item in outcomes]
    room_kinds = [image["room_kind"] for image in images]
    if _is_r2(inputs):
        shot_ids = [image["camera_id"] for image in images]
        if tuple(shot_ids) != R2_ORDERED_SHOT_IDS:
            _fail(
                "VISTA_HOME_REVIEW_CAMERA_SET_INVALID",
                "receipt r2 shot order differs",
            )
    else:
        expected_room_kinds = [camera[0] for camera in FIXED_REVIEW_CAMERAS]
        if room_kinds != expected_room_kinds:
            _fail("VISTA_HOME_REVIEW_ROOM_SET_INVALID", "receipt room order differs")
    if len({image["sha256"] for image in images}) != len(images):
        _fail("VISTA_HOME_REVIEW_PNG_DUPLICATE", "fixed room screenshots are not all distinct")
    engine_versions = {item.ue_result.get("engine_version") for item in outcomes}
    if len(engine_versions) != 1:
        _fail("VISTA_HOME_REVIEW_ENGINE_INVALID", "worker Unreal Engine versions differ")
    worker_bindings: list[dict[str, Any]] = []
    logs: list[dict[str, Any]] = []
    for outcome in outcomes:
        worker = outcome.worker
        _load_json(
            worker.manifest_path,
            label=f"worker execution manifest {worker.ordinal}",
            expected_sha256=worker.manifest_sha256,
        )
        result_path = _existing_file(worker.result_path, "Unreal worker result")
        if sha256_file(result_path) != outcome.ue_result_sha256:
            _fail("VISTA_HOME_REVIEW_PIN_MISMATCH", "Unreal worker result changed before receipt")
        final_path = inputs.output_dir / worker.camera["relative_path"]
        final_raw = _read_exact_regular(final_path, "accepted final PNG")
        if len(final_raw) != outcome.image["bytes"] or sha256_bytes(final_raw) != outcome.image["sha256"]:
            _fail("VISTA_HOME_REVIEW_PIN_MISMATCH", "accepted final PNG changed before receipt")
        width, height = _capture_dimensions(inputs)
        inspect_png_bytes(
            final_raw,
            expected_width=width,
            expected_height=height,
            source_label=str(final_path),
        )
        editor_log = _existing_file(worker.editor_log, "UnrealEditor log")
        editor_stdout = _existing_file(worker.editor_stdout, "UnrealEditor stdout")
        worker_bindings.append(
            {
                "ordinal": worker.ordinal,
                "manifest_path": worker.manifest_path.relative_to(inputs.output_dir).as_posix(),
                "manifest_sha256": worker.manifest_sha256,
                "ue_result_path": worker.result_path.relative_to(inputs.output_dir).as_posix(),
                "ue_result_sha256": outcome.ue_result_sha256,
            }
        )
        logs.append(
            {
                "ordinal": worker.ordinal,
                "editor": {
                    "path": editor_log.relative_to(inputs.output_dir).as_posix(),
                    "bytes": editor_log.stat().st_size,
                    "sha256": sha256_file(editor_log),
                },
                "stdout": {
                    "path": editor_stdout.relative_to(inputs.output_dir).as_posix(),
                    "bytes": editor_stdout.stat().st_size,
                    "sha256": sha256_file(editor_stdout),
                },
            }
        )
    width, height = _capture_dimensions(inputs)
    receipt: dict[str, Any] = {
        "schema_version": R2_RECEIPT_SCHEMA if _is_r2(inputs) else RECEIPT_SCHEMA,
        "status": (
            "captured_pending_runtime_observation"
            if _is_r2(inputs)
            else "accepted"
        ),
        "accepted_at": _utc_now(),
        "attempt_root": str(inputs.attempt_root),
        "output_root": str(inputs.output_dir),
        "map_path": inputs.map_path,
        "engine": {
            "executable": str(inputs.unreal_editor),
            "executable_sha256": inputs.unreal_editor_sha256,
            "version": next(iter(engine_versions)),
            "nvidia_icd": str(NVIDIA_VULKAN_ICD),
            "nvidia_icd_sha256": inputs.nvidia_icd_sha256,
            "display": inputs.display,
            "graphics_adapter": inputs.graphics_adapter,
            "regular_editor_x11": True,
        },
        "bindings": {
            "project_path": str(inputs.project),
            "project_sha256": inputs.project_sha256,
            "map_asset_path": str(inputs.map_asset),
            "map_asset_sha256": inputs.map_asset_sha256,
            "build_plan_path": str(inputs.build_plan),
            "build_plan_sha256": inputs.build_plan_sha256,
            "build_plan_content_digest": inputs.plan["content_digest"],
            "build_result_path": str(inputs.build_result),
            "build_result_sha256": inputs.build_result_sha256,
            "script_path": str(inputs.script),
            "script_sha256": inputs.script_sha256,
            "execution_sha256": execution_sha256,
            "worker_results": worker_bindings,
            "ddc_seed_path": str(inputs.ddc_seed) if inputs.ddc_seed is not None else None,
            "ddc_seed_tree_sha256": inputs.ddc_seed_tree_sha256,
        },
        "logs": logs,
        "capture": {
            "width": width,
            "height": height,
            "room_kinds": room_kinds,
            "images": images,
        },
        "verification": {
            "exact_room_set": True,
            "exact_materialized_camera_actor_set": True,
            "every_png_exact_dimensions": True,
            "every_png_nonblank": True,
            "every_room_png_distinct": True,
            "map_asset_pre_and_post_pinned": True,
            "accepted_build_result_pinned": True,
            "caller_python_allowed": False,
            "six_sequential_owned_editor_children": True,
            "one_native_highres_shot_per_child": True,
            "native_png_private_local_scratch": True,
            "native_and_final_bytes_equal": True,
        },
    }
    if _is_r2(inputs):
        if (
            inputs.visual_profile_path is None
            or inputs.visual_profile_sha256 is None
            or inputs.visual_profile_content_digest is None
        ):
            _fail(
                "VISTA_HOME_REVIEW_VISUAL_PROFILE_INVALID",
                "r2 receipt lost its visual-profile binding",
            )
        receipt["bindings"].update(
            {
                "visual_profile_path": str(inputs.visual_profile_path),
                "visual_profile_sha256": inputs.visual_profile_sha256,
                "visual_profile_content_digest": (
                    inputs.visual_profile_content_digest
                ),
            }
        )
        receipt["capture"].update(
            {
                "profile_id": R2_CAPTURE_PROFILE,
                "shot_ids": [image["camera_id"] for image in images],
                "runtime_observation_status": "pending",
            }
        )
        if scratch_ownership is None:
            _fail(
                "VISTA_HOME_REVIEW_SCRATCH_INVALID",
                "r2 receipt requires its live scratch ownership record",
            )
        _validate_owned_scratch(scratch_ownership)
        if (
            scratch_ownership.capture_profile != R2_CAPTURE_PROFILE
            or scratch_ownership.attempt_root != inputs.attempt_root
            or scratch_ownership.parent != inputs.scratch_parent
            or scratch_ownership.policy_root != inputs.scratch_policy_root
        ):
            _fail(
                "VISTA_HOME_REVIEW_SCRATCH_OWNERSHIP_MISMATCH",
                "r2 receipt scratch ownership binding differs",
            )
        receipt["scratch"] = {
            "storage_class": "private_nas_retained_evidence",
            "policy_root_path_sha256": sha256_bytes(
                str(scratch_ownership.policy_root).encode("ascii")
            ),
            "policy_root_device": os.fstat(
                scratch_ownership.policy_root_fd
            ).st_dev,
            "policy_root_mount_id": scratch_ownership.mount_id,
            "filesystem_type": scratch_ownership.filesystem_type,
            "mount_source_sha256": scratch_ownership.mount_source_sha256,
            "mapped_owner_uid": scratch_ownership.mapped_owner_uid,
            "parent_path_sha256": sha256_bytes(
                str(scratch_ownership.parent).encode("ascii")
            ),
            "parent_device": scratch_ownership.parent_device,
            "parent_inode": scratch_ownership.parent_inode,
            "parent_mount_id": scratch_ownership.mount_id,
            "owned_child_device": scratch_ownership.device,
            "owned_child_inode": scratch_ownership.inode,
            "owned_child_mount_id": scratch_ownership.mount_id,
            "mode": "0700",
            "authority_check": "parent_dirfd_o_excl_create_fsync_unlink",
            "child_creation": "unique_mkdirat_eexist_fail",
            "lifecycle": R2_SCRATCH_LIFECYCLE,
            "cleanup_policy": R2_SCRATCH_CLEANUP_POLICY,
            "cleanup_status_at_receipt": "retained",
            "scratch_absolute_path_disclosed": False,
        }
        receipt["verification"] = {
            "exact_review_shot_set": True,
            "exact_materialized_r2_camera_actor_set": True,
            "every_png_exact_dimensions": True,
            "every_png_nonblank": True,
            "every_shot_png_distinct": True,
            "map_asset_pre_and_post_pinned": True,
            "accepted_build_result_pinned": True,
            "visual_profile_bytes_pinned": True,
            "graphics_adapter_zero": inputs.graphics_adapter == 0,
            "display_119": inputs.display == R2_DISPLAY,
            "caller_python_allowed": False,
            "six_sequential_owned_editor_children": True,
            "one_native_highres_shot_per_child": True,
            "native_png_private_nas_retained_evidence": True,
            "scratch_retained_append_only": True,
            "scratch_cleanup_descriptor_close_only": True,
            "native_and_final_bytes_equal": True,
            "near_field_clearance_observation": "pending",
            "foreground_occlusion_observation": "pending",
            "expected_hero_visibility_observation": "pending",
            "forbidden_foreground_observation": "pending",
            "physical_exposure_observation": "pending",
        }
    return receipt


def _create_scratch_root(inputs: CaptureInputs) -> ScratchOwnership:
    if _is_r2(inputs):
        return _create_r2_scratch_root(inputs)
    parent = _existing_directory(
        LOCAL_SCRATCH_PARENT,
        "local scratch parent",
    )
    prefix = R1_SCRATCH_PREFIX
    parent_metadata = os.lstat(parent)
    try:
        scratch = Path(tempfile.mkdtemp(prefix=prefix, dir=str(parent)))
    except OSError as exc:
        _fail(
            "VISTA_HOME_REVIEW_SCRATCH_INVALID",
            f"cannot create unique scratch child: {exc}",
            pointer=str(parent),
        )
        raise AssertionError("unreachable") from exc
    scratch_fd = -1
    try:
        _, scratch_fd, _ = _open_directory_fd(
            scratch,
            "host-created scratch child",
        )
        metadata = _require_private_directory_fd(
            scratch_fd,
            "host-created scratch child",
        )
        if not _same_inode(metadata, os.lstat(scratch)):
            _fail(
                "VISTA_HOME_REVIEW_SCRATCH_OWNERSHIP_MISMATCH",
                "host-created scratch child changed while securing it",
                pointer=str(scratch),
            )
    finally:
        if scratch_fd >= 0:
            os.close(scratch_fd)
    ownership = ScratchOwnership(
        path=scratch,
        parent=parent,
        attempt_root=inputs.attempt_root,
        capture_profile=inputs.capture_profile,
        parent_device=parent_metadata.st_dev,
        parent_inode=parent_metadata.st_ino,
        device=metadata.st_dev,
        inode=metadata.st_ino,
    )
    try:
        if (
            scratch.parent != parent
            or not scratch.name.startswith(prefix)
            or SAFE_LOCAL_PATH_RE.fullmatch(str(scratch)) is None
            or not str(scratch).isascii()
            or not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or scratch.resolve(strict=True) != scratch
        ):
            _fail(
                "VISTA_HOME_REVIEW_SCRATCH_INVALID",
                "host-created scratch child is not canonical private storage",
                pointer=str(scratch),
            )
        if (
            os.lstat(parent).st_dev != ownership.parent_device
            or os.lstat(parent).st_ino != ownership.parent_inode
        ):
            _fail(
                "VISTA_HOME_REVIEW_SCRATCH_OWNERSHIP_MISMATCH",
                "scratch parent changed during child creation",
                pointer=str(parent),
            )
        try:
            scratch.relative_to(inputs.attempt_root)
        except ValueError:
            pass
        else:
            _fail(
                "VISTA_HOME_REVIEW_SCRATCH_INVALID",
                "host-created scratch child is inside the UE attempt",
                pointer=str(scratch),
            )
        _validate_owned_scratch(ownership)
        return ownership
    except ReviewCaptureError:
        try:
            _remove_scratch_root(ownership)
        except ReviewCaptureError:
            pass
        raise


def _create_r2_scratch_root(inputs: CaptureInputs) -> ScratchOwnership:
    authority = inputs.scratch_authority
    if authority is None:
        _fail("VISTA_HOME_REVIEW_SCRATCH_INVALID", "r2 capture lost its scratch authority")
    _validate_r2_scratch_authority(authority)
    name = f"{R2_SCRATCH_PREFIX}{secrets.token_hex(16)}"
    created = False
    created_metadata: os.stat_result | None = None
    child_fd = -1
    policy_root_fd = -1
    parent_fd = -1
    try:
        os.mkdir(name, 0o700, dir_fd=authority.parent_fd)
        created = True
        child_fd, _ = _open_directory_entry_at(
            authority.parent_fd,
            name,
            "r2 scratch child",
        )
        child = _require_private_directory_fd(child_fd, "r2 scratch child")
        created_metadata = os.stat(
            name,
            dir_fd=authority.parent_fd,
            follow_symlinks=False,
        )
        if not _same_inode(child, created_metadata):
            raise OSError("r2 scratch child changed while opening")
        os.fsync(authority.parent_fd)
        policy_root_fd = os.dup(authority.policy_root_fd)
        parent_fd = os.dup(authority.parent_fd)
        ownership = ScratchOwnership(
            path=authority.parent / name,
            parent=authority.parent,
            attempt_root=inputs.attempt_root,
            capture_profile=R2_CAPTURE_PROFILE,
            parent_device=authority.parent_stat.st_dev,
            parent_inode=authority.parent_stat.st_ino,
            device=child.st_dev,
            inode=child.st_ino,
            policy_root=authority.policy_root,
            policy_root_fd=policy_root_fd,
            parent_fd=parent_fd,
            child_fd=child_fd,
            parent_relative_parts=authority.parent_relative_parts,
            mount_id=authority.mount_id,
            filesystem_type=authority.filesystem_type,
            mount_source_sha256=authority.mount_source_sha256,
            mapped_owner_uid=authority.policy_root_stat.st_uid,
        )
        _validate_owned_r2_scratch(ownership)
        created = False
        policy_root_fd = -1
        parent_fd = -1
        child_fd = -1
        return ownership
    except (OSError, ReviewCaptureError) as exc:
        for descriptor in (parent_fd, policy_root_fd):
            if descriptor >= 0:
                os.close(descriptor)
        if child_fd >= 0:
            os.close(child_fd)
        if isinstance(exc, ReviewCaptureError):
            if created:
                raise ReviewCaptureError(
                    exc.code,
                    f"{exc.detail}; append-only partial evidence was retained",
                    pointer=exc.pointer,
                ) from exc
            raise
        detail = f"cannot create r2 scratch child: {exc}"
        if created:
            detail = f"{detail}; append-only partial evidence was retained"
        _fail("VISTA_HOME_REVIEW_SCRATCH_INVALID", detail)


def _prepare_worker_runs(
    inputs: CaptureInputs,
    execution_sha256: str,
    scratch: ScratchOwnership | Path,
) -> tuple[WorkerRun, ...]:
    if _is_r2(inputs):
        if not isinstance(scratch, ScratchOwnership):
            _fail(
                "VISTA_HOME_REVIEW_SCRATCH_OWNERSHIP_MISMATCH",
                "r2 worker preparation requires retained scratch ownership",
            )
        scratch_ownership: ScratchOwnership | None = scratch
        scratch_root = scratch.path
    else:
        scratch_ownership = (
            scratch if isinstance(scratch, ScratchOwnership) else None
        )
        scratch_root = scratch.path if isinstance(scratch, ScratchOwnership) else scratch
    workers: list[WorkerRun] = []
    for ordinal in range(1, len(inputs.cameras) + 1):
        worker_dir = inputs.output_dir / WORKERS_DIR / f"{ordinal:02d}"
        scratch_dir = scratch_root / f"worker-{ordinal:02d}"
        output_workers_fd = -1
        worker_dir_fd = -1
        try:
            _, output_workers_fd, _ = _open_directory_fd(
                inputs.output_dir / WORKERS_DIR,
                "review output workers directory",
            )
            worker_dir_fd, _ = _mkdir_private_directory_at(
                output_workers_fd,
                f"{ordinal:02d}",
                f"review output worker directory {ordinal}",
            )
        finally:
            if worker_dir_fd >= 0:
                os.close(worker_dir_fd)
            if output_workers_fd >= 0:
                os.close(output_workers_fd)
        if _is_r2(inputs):
            assert scratch_ownership is not None
            _validate_owned_r2_scratch(scratch_ownership)
            if scratch_ownership.child_fd is None:
                raise AssertionError("validated r2 child descriptor is missing")
            scratch_name = f"worker-{ordinal:02d}"
            scratch_dir_fd = -1
            try:
                scratch_dir_fd, _ = _mkdir_private_directory_at(
                    scratch_ownership.child_fd,
                    scratch_name,
                    f"r2 worker directory {ordinal}",
                )
                scratch_ownership.register_worker_fd(ordinal, scratch_dir_fd)
                scratch_dir_fd = -1
                _validate_owned_r2_worker_dir(scratch_ownership, ordinal)
            finally:
                if scratch_dir_fd >= 0:
                    os.close(scratch_dir_fd)
        else:
            scratch_root_fd = -1
            scratch_dir_fd = -1
            try:
                _, scratch_root_fd, _ = _open_directory_fd(
                    scratch_root,
                    "local scratch root",
                )
                scratch_dir_fd, _ = _mkdir_private_directory_at(
                    scratch_root_fd,
                    f"worker-{ordinal:02d}",
                    f"local scratch worker directory {ordinal}",
                )
            finally:
                if scratch_dir_fd >= 0:
                    os.close(scratch_dir_fd)
                if scratch_root_fd >= 0:
                    os.close(scratch_root_fd)
        scratch_png = scratch_dir / "capture.png"
        manifest = build_worker_execution(
            inputs,
            execution_sha256,
            ordinal,
            scratch_png,
            scratch_ownership=scratch_ownership,
        )
        manifest_raw = canonical_json(manifest)
        manifest_path = worker_dir / EXECUTION_FILE
        _write_exclusive(manifest_path, manifest_raw)
        workers.append(
            WorkerRun(
                ordinal=ordinal,
                camera=_camera_for_ordinal(inputs, ordinal),
                worker_dir=worker_dir,
                manifest_path=manifest_path,
                manifest_sha256=sha256_bytes(manifest_raw),
                scratch_dir=scratch_dir,
                scratch_png=scratch_png,
                result_path=worker_dir / UE_RESULT_FILE,
                editor_log=worker_dir / EDITOR_LOG_FILE,
                editor_stdout=worker_dir / EDITOR_STDOUT_FILE,
                scratch_ownership=scratch_ownership,
            )
        )
    return tuple(workers)


def _same_inode(first: os.stat_result, second: os.stat_result) -> bool:
    return (first.st_dev, first.st_ino, stat.S_IFMT(first.st_mode)) == (
        second.st_dev,
        second.st_ino,
        stat.S_IFMT(second.st_mode),
    )


def _remove_scratch_root(ownership: ScratchOwnership) -> None:
    if ownership.capture_profile == R2_CAPTURE_PROFILE:
        ownership.close()
        return
    _validate_owned_scratch(ownership)
    if not getattr(shutil.rmtree, "avoids_symlink_attacks", False):
        _fail(
            "VISTA_HOME_REVIEW_SCRATCH_INVALID",
            "platform cannot safely remove the owned scratch child",
        )
    try:
        shutil.rmtree(ownership.path)
    except OSError as exc:
        _fail(
            "VISTA_HOME_REVIEW_SCRATCH_INVALID",
            f"cannot remove owned scratch child: {exc}",
            pointer=str(ownership.path),
        )
    if os.path.lexists(ownership.path):
        _fail(
            "VISTA_HOME_REVIEW_SCRATCH_INVALID",
            "owned scratch child remains after cleanup",
            pointer=str(ownership.path),
        )


def execute_capture(inputs: CaptureInputs, execution_raw: bytes, execution_sha256: str) -> dict[str, Any]:
    """Run six owned Unreal children sequentially and accept their exact PNG bytes."""

    _prepare_output(inputs, execution_raw)
    scratch_ownership: ScratchOwnership | None = None
    try:
        scratch_ownership = _create_scratch_root(inputs)
        workers = _prepare_worker_runs(
            inputs,
            execution_sha256,
            scratch_ownership,
        )
        outcomes: list[WorkerOutcome] = []
        for worker in workers:
            _verify_input_pins(inputs)
            returncode = run_editor(inputs, worker)
            if returncode != 0:
                _fail(
                    "VISTA_HOME_REVIEW_EDITOR_FAILED",
                    f"UnrealEditor child {worker.ordinal} exited with status {returncode}",
                )
            ue_result, ue_result_sha = _load_worker_result(inputs, worker)
            image = _accept_worker_png(inputs, worker, ue_result)
            outcomes.append(
                WorkerOutcome(
                    worker=worker,
                    ue_result=ue_result,
                    ue_result_sha256=ue_result_sha,
                    image=image,
                )
            )
        receipt = build_receipt(
            inputs,
            execution_sha256,
            outcomes,
            scratch_ownership=scratch_ownership,
        )
        receipt_raw = canonical_json(receipt)
        receipt_path = inputs.output_dir / RECEIPT_FILE
        _write_exclusive(receipt_path, receipt_raw)
        return {
            "status": receipt["status"],
            "receipt": str(receipt_path),
            "receipt_sha256": sha256_bytes(receipt_raw),
            "image_count": len(receipt["capture"]["images"]),
        }
    finally:
        if scratch_ownership is not None:
            _remove_scratch_root(scratch_ownership)


def _host_main(args: argparse.Namespace) -> int:
    inputs: CaptureInputs | None = None
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
            "command": build_editor_command(inputs, 1),
            "commands": [build_editor_command(inputs, ordinal) for ordinal in range(1, 7)],
            "policy": execution["policy"],
        }
        if _is_r2(inputs):
            preview.update(
                {
                    "capture_profile": R2_CAPTURE_PROFILE,
                    "shot_ids": [camera["camera_id"] for camera in inputs.cameras],
                    "runtime_observation_status": "pending",
                }
            )
        if not args.apply:
            sys.stdout.buffer.write(canonical_json(preview))
            return 0
        result = execute_capture(inputs, execution_raw, execution_sha)
        sys.stdout.buffer.write(canonical_json(result))
        return 0
    except ReviewCaptureError as exc:
        sys.stderr.buffer.write(canonical_json({"status": "failed", "error": exc.public_dict()}))
        return 2
    finally:
        if inputs is not None:
            inputs.close()


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


def _validate_aggregate_worker_inputs(
    execution: Mapping[str, Any],
    aggregate_manifest_path: Path,
) -> tuple[Path, list[dict[str, Any]]]:
    schema = execution.get("schema_version")
    if schema not in {EXECUTION_SCHEMA, R2_EXECUTION_SCHEMA}:
        _fail(
            "VISTA_HOME_REVIEW_EXECUTION_INVALID",
            "aggregate execution schema differs",
        )
    is_r2 = schema == R2_EXECUTION_SCHEMA
    expected_fields = {
        "schema_version",
        "attempt_root",
        "project",
        "map_asset",
        "build_plan",
        "build_result",
        "engine",
        "ddc_seed",
        "map_path",
        "output_root",
        "script",
        "capture",
        "artifacts",
        "policy",
    }
    if is_r2:
        expected_fields.update({"visual_profile", "scratch"})
    expected_policy = {
        "append_only_output": True,
        "caller_python_allowed": False,
        "fixed_camera_actor_tags": True,
        "regular_editor_x11": True,
        "receipt_requires_host_png_validation": True,
        "sequential_owned_editor_children": True,
        "one_native_highres_shot_per_child": True,
        "native_png_uses_private_local_scratch": True,
    }
    if is_r2:
        del expected_policy["native_png_uses_private_local_scratch"]
        expected_policy.update(
            {
                "visual_profile_sha256_required": True,
                "graphics_adapter_zero_required": True,
                "runtime_camera_observations_required": True,
                "scratch_policy_root_required": True,
                "scratch_parent_required": True,
                "native_png_uses_private_nas_retained_evidence": True,
                "scratch_retained_append_only": True,
                "scratch_cleanup_descriptor_close_only": True,
            }
        )
    if (
        set(execution) != expected_fields
        or execution.get("policy") != expected_policy
    ):
        _fail("VISTA_HOME_REVIEW_EXECUTION_INVALID", "aggregate execution schema or policy differs")
    output_root = _existing_directory(Path(execution.get("output_root", "")), "review output root")
    if aggregate_manifest_path != output_root / EXECUTION_FILE:
        _fail("VISTA_HOME_REVIEW_EXECUTION_INVALID", "aggregate execution manifest location differs")
    script = execution.get("script")
    if not isinstance(script, Mapping) or set(script) != {"path", "sha256"}:
        _fail("VISTA_HOME_REVIEW_EXECUTION_INVALID", "fixed script binding differs")
    current_script = _existing_file(Path(__file__).resolve(strict=True), "fixed review capture script")
    if script["path"] != str(current_script) or script["sha256"] != sha256_file(current_script):
        _fail("VISTA_HOME_REVIEW_EXECUTION_INVALID", "fixed script identity or digest differs")
    project = execution.get("project")
    map_asset = execution.get("map_asset")
    build_plan = execution.get("build_plan")
    build_result = execution.get("build_result")
    engine = execution.get("engine")
    if not isinstance(project, Mapping) or set(project) != {"path", "sha256"}:
        _fail("VISTA_HOME_REVIEW_EXECUTION_INVALID", "project binding differs")
    if not isinstance(map_asset, Mapping) or set(map_asset) != {"path", "sha256"}:
        _fail("VISTA_HOME_REVIEW_EXECUTION_INVALID", "map asset binding differs")
    if not isinstance(build_plan, Mapping) or set(build_plan) != {"path", "sha256", "content_digest"}:
        _fail("VISTA_HOME_REVIEW_EXECUTION_INVALID", "build plan binding differs")
    if not isinstance(build_result, Mapping) or set(build_result) != {"path", "sha256"}:
        _fail("VISTA_HOME_REVIEW_EXECUTION_INVALID", "build result binding differs")
    expected_engine_fields = {
        "executable",
        "executable_sha256",
        "nvidia_icd",
        "nvidia_icd_sha256",
        "required_version_prefix",
    }
    if is_r2:
        expected_engine_fields.update({"graphics_adapter", "display"})
    if (
        not isinstance(engine, Mapping)
        or set(engine) != expected_engine_fields
        or engine.get("required_version_prefix") != EXPECTED_ENGINE_PREFIX
        or (is_r2 and engine.get("graphics_adapter") != 0)
        or (is_r2 and engine.get("display") != R2_DISPLAY)
    ):
        _fail("VISTA_HOME_REVIEW_EXECUTION_INVALID", "engine binding differs")
    project_path = _existing_file(Path(project["path"]), "project")
    map_asset_path = _existing_file(Path(map_asset["path"]), "materialized map asset")
    plan_path = _existing_file(Path(build_plan["path"]), "build plan")
    build_result_path = _existing_file(Path(build_result["path"]), "accepted UE build result")
    engine_path = _existing_file(Path(engine["executable"]), "UnrealEditor")
    icd_path = _existing_file(Path(engine["nvidia_icd"]), "NVIDIA Vulkan ICD")
    if (
        sha256_file(project_path) != project["sha256"]
        or sha256_file(map_asset_path) != map_asset["sha256"]
        or sha256_file(plan_path) != build_plan["sha256"]
        or sha256_file(build_result_path) != build_result["sha256"]
        or sha256_file(engine_path) != engine["executable_sha256"]
        or sha256_file(icd_path) != engine["nvidia_icd_sha256"]
    ):
        _fail("VISTA_HOME_REVIEW_PIN_MISMATCH", "worker evidence or engine pin differs")
    if Path("/proc/self/exe").resolve(strict=True) != engine_path:
        _fail("VISTA_HOME_REVIEW_ENGINE_INVALID", "running UnrealEditor identity differs")
    if os.environ.get("VK_ICD_FILENAMES") != str(icd_path):
        _fail("VISTA_HOME_REVIEW_ENGINE_INVALID", "running Vulkan ICD binding differs")
    if is_r2 and os.environ.get("DISPLAY") != R2_DISPLAY:
        _fail(
            "VISTA_HOME_REVIEW_ENGINE_INVALID",
            "running r2 DISPLAY binding differs",
        )
    plan, _ = _load_json(plan_path, label="build plan", expected_sha256=build_plan["sha256"])
    if plan.get("content_digest") != build_plan["content_digest"]:
        _fail("VISTA_HOME_REVIEW_PIN_MISMATCH", "worker build plan content digest differs")
    attempt_root = _existing_directory(Path(execution.get("attempt_root", "")), "attempt root")
    if is_r2:
        _validate_r2_scratch_parent_binding(execution.get("scratch"))
    visual_profile_sha256: str | None = None
    visual_profile_content_digest: str | None = None
    if is_r2:
        visual_profile_binding = execution.get("visual_profile")
        if (
            not isinstance(visual_profile_binding, Mapping)
            or set(visual_profile_binding) != {"path", "sha256", "content_digest"}
            or not isinstance(visual_profile_binding.get("path"), str)
            or not isinstance(visual_profile_binding.get("sha256"), str)
            or SHA256_RE.fullmatch(visual_profile_binding["sha256"]) is None
            or not isinstance(visual_profile_binding.get("content_digest"), str)
            or SHA256_RE.fullmatch(visual_profile_binding["content_digest"])
            is None
        ):
            _fail(
                "VISTA_HOME_REVIEW_EXECUTION_INVALID",
                "r2 visual-profile binding differs",
            )
        visual_profile_path = _require_child(
            _existing_file(
                Path(visual_profile_binding["path"]),
                "r2 visual profile",
            ),
            attempt_root,
            "r2 visual profile",
        )
        if visual_profile_path != attempt_root / EXPECTED_VISUAL_PROFILE_RELATIVE:
            _fail(
                "VISTA_HOME_REVIEW_EXECUTION_INVALID",
                "r2 visual-profile location differs",
            )
        visual_profile, visual_profile_raw = _load_json(
            visual_profile_path,
            label="r2 visual profile",
            expected_sha256=visual_profile_binding["sha256"],
        )
        visual_profile_sha256 = sha256_bytes(visual_profile_raw)
        visual_profile_content_digest = visual_profile.get("content_digest")
        if (
            visual_profile_content_digest
            != visual_profile_binding["content_digest"]
            or visual_profile_content_digest
            != _visual_profile_content_digest(visual_profile)
        ):
            _fail(
                "VISTA_HOME_REVIEW_PIN_MISMATCH",
                "r2 visual-profile content digest differs",
            )
        cameras = _compile_r2_capture_cameras(
            visual_profile,
            plan,
            execution.get("map_path"),
        )
    else:
        cameras = compile_fixed_cameras(plan, execution.get("map_path"))
    _validate_build_result(
        build_result_path,
        attempt_root,
        execution.get("map_path"),
        visual_profile_id=R2_CAPTURE_PROFILE if is_r2 else None,
        visual_profile_sha256=visual_profile_sha256,
        visual_profile_content_digest=visual_profile_content_digest,
    )
    ddc_seed = execution.get("ddc_seed")
    if ddc_seed is not None:
        if not isinstance(ddc_seed, Mapping) or set(ddc_seed) != {"path", "tree_sha256"}:
            _fail("VISTA_HOME_REVIEW_EXECUTION_INVALID", "DDC seed binding differs")
        seed_path = _existing_directory(Path(ddc_seed["path"]), "DDC seed")
        seed_sha, _entries, _bytes = _tree_snapshot(seed_path)
        if seed_sha != ddc_seed["tree_sha256"]:
            _fail("VISTA_HOME_REVIEW_PIN_MISMATCH", "worker DDC seed pin differs")
    capture = execution.get("capture")
    expected_capture_fields = {"width", "height", "room_kinds", "cameras"}
    expected_width, expected_height = WIDTH, HEIGHT
    if is_r2:
        expected_capture_fields.update(
            {"profile_id", "shot_ids", "runtime_observation_status"}
        )
        expected_width, expected_height = R2_WIDTH, R2_HEIGHT
    if (
        not isinstance(capture, Mapping)
        or set(capture) != expected_capture_fields
        or capture.get("width") != expected_width
        or capture.get("height") != expected_height
    ):
        _fail("VISTA_HOME_REVIEW_EXECUTION_INVALID", "worker capture dimensions differ")
    if (
        capture.get("room_kinds") != [camera["room_kind"] for camera in cameras]
        or capture.get("cameras") != cameras
        or (
            is_r2
            and (
                capture.get("profile_id") != R2_CAPTURE_PROFILE
                or tuple(capture.get("shot_ids", ())) != R2_ORDERED_SHOT_IDS
                or capture.get("runtime_observation_status") != "pending"
            )
        )
    ):
        _fail("VISTA_HOME_REVIEW_EXECUTION_INVALID", "worker fixed camera plan differs")
    aggregate_artifacts = execution.get("artifacts")
    if not isinstance(aggregate_artifacts, Mapping) or set(aggregate_artifacts) != {
        "images_dir", "workers_dir", "receipt"
    }:
        _fail("VISTA_HOME_REVIEW_EXECUTION_INVALID", "aggregate artifact bindings differ")
    if (
        _safe_execution_child(aggregate_artifacts["images_dir"], output_root, "images directory")
        != output_root / IMAGES_DIR
        or _safe_execution_child(aggregate_artifacts["workers_dir"], output_root, "workers directory")
        != output_root / WORKERS_DIR
        or _safe_execution_child(aggregate_artifacts["receipt"], output_root, "receipt")
        != output_root / RECEIPT_FILE
    ):
        _fail("VISTA_HOME_REVIEW_EXECUTION_INVALID", "aggregate artifact path differs")
    if (output_root / RECEIPT_FILE).exists():
        _fail("VISTA_HOME_REVIEW_OUTPUT_EXISTS", "aggregate receipt already exists")
    return output_root, cameras


def _load_worker_execution() -> tuple[dict[str, Any], dict[str, Any], str]:
    manifest_text = os.environ.get(EXECUTION_ENV, "")
    expected_sha = os.environ.get(EXECUTION_SHA_ENV, "")
    if SHA256_RE.fullmatch(expected_sha) is None:
        _fail("VISTA_HOME_REVIEW_EXECUTION_INVALID", "worker execution pin is invalid")
    manifest_path = _existing_file(Path(manifest_text), "worker execution manifest")
    worker, _raw = _load_json(
        manifest_path,
        label="worker execution manifest",
        expected_sha256=expected_sha,
    )
    expected_worker_policy = {
        "immutable_ordinal": True,
        "exactly_one_camera_capture": True,
        "at_most_one_native_highres_shot": True,
        "host_accepts_and_copies_png": True,
    }
    worker_schema = worker.get("schema_version")
    expected_worker_keys = {
        "schema_version",
        "aggregate_execution",
        "ordinal",
        "camera",
        "scratch_png",
        "artifacts",
        "policy",
    }
    if worker_schema == R2_WORKER_EXECUTION_SCHEMA:
        expected_worker_keys.add("scratch_capability")
    if (
        set(worker) != expected_worker_keys
        or worker_schema not in {WORKER_EXECUTION_SCHEMA, R2_WORKER_EXECUTION_SCHEMA}
        or worker.get("policy") != expected_worker_policy
    ):
        _fail("VISTA_HOME_REVIEW_EXECUTION_INVALID", "worker execution schema or policy differs")
    aggregate_ref = worker.get("aggregate_execution")
    if not isinstance(aggregate_ref, Mapping) or set(aggregate_ref) != {"path", "sha256"}:
        _fail("VISTA_HOME_REVIEW_EXECUTION_INVALID", "aggregate execution binding differs")
    aggregate_path = _existing_file(Path(aggregate_ref["path"]), "aggregate execution manifest")
    execution, _aggregate_raw = _load_json(
        aggregate_path,
        label="aggregate execution manifest",
        expected_sha256=aggregate_ref["sha256"],
    )
    output_root, cameras = _validate_aggregate_worker_inputs(execution, aggregate_path)
    expected_worker_schema = (
        R2_WORKER_EXECUTION_SCHEMA
        if execution.get("schema_version") == R2_EXECUTION_SCHEMA
        else WORKER_EXECUTION_SCHEMA
    )
    if worker.get("schema_version") != expected_worker_schema:
        _fail(
            "VISTA_HOME_REVIEW_EXECUTION_INVALID",
            "worker and aggregate capture profiles differ",
        )
    ordinal = worker.get("ordinal")
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or not 1 <= ordinal <= len(cameras):
        _fail("VISTA_HOME_REVIEW_ORDINAL_INVALID", "worker ordinal is invalid")
    expected_camera = cameras[ordinal - 1]
    if worker.get("camera") != expected_camera or expected_camera["ordinal"] != ordinal:
        _fail("VISTA_HOME_REVIEW_ORDINAL_INVALID", "worker camera does not match its immutable ordinal")
    worker_dir = output_root / WORKERS_DIR / f"{ordinal:02d}"
    if manifest_path != worker_dir / EXECUTION_FILE:
        _fail("VISTA_HOME_REVIEW_EXECUTION_INVALID", "worker manifest path differs from ordinal")
    scratch_png = _validate_scratch_png(
        Path(worker.get("scratch_png", "")),
        ordinal=ordinal,
        attempt_root=Path(execution["attempt_root"]),
        require_parent=True,
        r2_scratch_binding=(
            execution.get("scratch")
            if execution.get("schema_version") == R2_EXECUTION_SCHEMA
            else None
        ),
        r2_worker_binding=(
            worker.get("scratch_capability")
            if execution.get("schema_version") == R2_EXECUTION_SCHEMA
            else None
        ),
    )
    artifacts = worker.get("artifacts")
    if not isinstance(artifacts, Mapping) or set(artifacts) != {
        "ue_result", "editor_log", "editor_stdout", "final_image"
    }:
        _fail("VISTA_HOME_REVIEW_EXECUTION_INVALID", "worker artifact bindings differ")
    expected_artifacts = {
        "ue_result": worker_dir / UE_RESULT_FILE,
        "editor_log": worker_dir / EDITOR_LOG_FILE,
        "editor_stdout": worker_dir / EDITOR_STDOUT_FILE,
        "final_image": output_root / expected_camera["relative_path"],
    }
    for key, expected_path in expected_artifacts.items():
        if _safe_execution_child(artifacts[key], output_root, key) != expected_path:
            _fail("VISTA_HOME_REVIEW_EXECUTION_INVALID", f"worker {key} path differs")
    if (
        expected_artifacts["ue_result"].exists()
        or expected_artifacts["final_image"].exists()
        or (
            execution.get("schema_version") != R2_EXECUTION_SCHEMA
            and scratch_png.exists()
        )
    ):
        _fail("VISTA_HOME_REVIEW_OUTPUT_EXISTS", "worker output already exists before capture")
    return execution, worker, expected_sha


def _worker_write_result(worker: Mapping[str, Any], result: Mapping[str, Any]) -> None:
    target = Path(worker["artifacts"]["ue_result"])
    _write_exclusive(target, canonical_json(dict(result)))


def _worker_result(
    worker: Mapping[str, Any],
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
        "schema_version": (
            R2_UE_RESULT_SCHEMA
            if worker.get("schema_version") == R2_WORKER_EXECUTION_SCHEMA
            else UE_RESULT_SCHEMA
        ),
        "status": status,
        "captured_at": _utc_now(),
        "engine_version": engine_version,
        "project_path": project_path,
        "map_path": map_path,
        "execution_sha256": execution_sha,
        "worker_ordinal": worker["ordinal"],
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
    worker_execution: dict[str, Any] | None = None
    execution_sha = os.environ.get(EXECUTION_SHA_ENV, "")
    keep_alive = False
    try:
        execution, worker_execution, execution_sha = _load_worker_execution()
        project_path = str(Path(unreal.Paths.get_project_file_path()).resolve(strict=True))
        if project_path != execution["project"]["path"] or sha256_file(Path(project_path)) != execution["project"]["sha256"]:
            _fail("VISTA_HOME_REVIEW_PROJECT_INVALID", "loaded Unreal project differs from the pin")
        engine_version = str(unreal.SystemLibrary.get_engine_version())
        if not engine_version.startswith(EXPECTED_ENGINE_PREFIX):
            _fail("VISTA_HOME_REVIEW_ENGINE_INVALID", "Unreal Engine version is not 5.7")
        if execution.get("schema_version") == R2_EXECUTION_SCHEMA:
            command_line = str(unreal.SystemLibrary.get_command_line())
            try:
                adapter_flags = [
                    token.lower()
                    for token in shlex.split(command_line)
                    if token.lower().startswith("-graphicsadapter=")
                ]
            except ValueError:
                _fail(
                    "VISTA_HOME_REVIEW_ENGINE_INVALID",
                    "running Unreal command line cannot be parsed",
                )
            if adapter_flags != ["-graphicsadapter=0"]:
                _fail(
                    "VISTA_HOME_REVIEW_ENGINE_INVALID",
                    "running r2 Unreal process is not uniquely pinned to graphics adapter 0",
                )
        editor = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
        actor_subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        world = editor.get_editor_world()
        if world is None:
            _fail("VISTA_HOME_REVIEW_MAP_MISMATCH", "editor world is unavailable")
        loaded_map = str(world.get_path_name()).split(".", 1)[0]
        if loaded_map != execution["map_path"]:
            _fail("VISTA_HOME_REVIEW_MAP_MISMATCH", f"loaded map {loaded_map!r} differs")

        expected_cameras = execution["capture"]["cameras"]
        selected_camera = worker_execution["camera"]
        is_r2_execution = execution.get("schema_version") == R2_EXECUTION_SCHEMA
        expected_tags = {camera["semantic_tag"] for camera in expected_cameras}
        actors_by_tag: dict[str, list[Any]] = {tag: [] for tag in expected_tags}
        vista_camera_tags: set[str] = set()
        for actor in actor_subsystem.get_all_level_actors():
            if not isinstance(actor, unreal.CameraActor):
                continue
            tags = {str(tag) for tag in actor.get_editor_property("tags")}
            if is_r2_execution and R2_CAMERA_ACTOR_TAG not in tags:
                continue
            for tag in tags:
                if tag.startswith("VistaSemanticId=home.r1/room.") and "/camera." in tag:
                    vista_camera_tags.add(tag)
                if tag in actors_by_tag:
                    actors_by_tag[tag].append(actor)
        if vista_camera_tags != expected_tags or any(len(actors_by_tag[tag]) != 1 for tag in expected_tags):
            _fail("VISTA_HOME_REVIEW_CAMERA_SET_INVALID", "materialized review CameraActor set is not exact")
        actor_paths = [
            str(actors_by_tag[camera["semantic_tag"]][0].get_path_name())
            for camera in expected_cameras
        ]
        if len(set(actor_paths)) != len(expected_cameras):
            _fail("VISTA_HOME_REVIEW_CAMERA_SET_INVALID", "fixed semantic tags do not map to distinct CameraActors")

        for camera in expected_cameras:
            actor_tags = {
                str(tag)
                for tag in actors_by_tag[camera["semantic_tag"]][0].get_editor_property("tags")
            }
            if actor_tags & expected_tags != {camera["semantic_tag"]}:
                _fail("VISTA_HOME_REVIEW_CAMERA_SET_INVALID", "CameraActor has an ambiguous fixed semantic tag")
        actor = actors_by_tag[selected_camera["semantic_tag"]][0]
        actual_transform = _actual_transform(actor)
        if not _transform_matches(actual_transform, selected_camera["expected_transform"]):
            _fail("VISTA_HOME_REVIEW_CAMERA_DRIFT", "selected materialized CameraActor transform differs")
        component = actor.get_editor_property("camera_component")
        fov = float(component.get_editor_property("field_of_view"))
        if abs(fov - float(selected_camera["expected_fov_deg"])) > 0.05:
            _fail("VISTA_HOME_REVIEW_CAMERA_DRIFT", "selected materialized CameraActor FOV differs")

        pilot = getattr(unreal.EditorLevelLibrary, "pilot_level_actor", None)
        eject = getattr(unreal.EditorLevelLibrary, "eject_pilot_level_actor", None)
        if not callable(pilot) or not callable(eject):
            _fail("VISTA_HOME_REVIEW_ENGINE_INVALID", "UE 5.7 CameraActor pilot API is unavailable")

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
            "stable_size": None,
            "stable_since": None,
            "captures": [],
            "shot_requested": False,
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
                        worker_execution,
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
                        worker_execution,
                        execution_sha,
                        status="failed",
                        captures=state["captures"],
                        camera_actor_set_exact=True,
                        error=public_error,
                        engine_version=engine_version,
                        project_path=project_path,
                        map_path=loaded_map,
                    )
                _worker_write_result(worker_execution, result)
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
                    location = actor.get_actor_location()
                    rotation = actor.get_actor_rotation()
                    editor.set_level_viewport_camera_info(location, rotation)
                    pilot(actor)
                    unreal.EditorLevelLibrary.editor_invalidate_viewports()
                    state["capture_method"] = CAPTURE_METHOD
                    state["phase"] = "settle"
                    state["phase_started"] = now
                    return
                if state["phase"] == "settle":
                    if now - state["phase_started"] < 0.5:
                        return
                    if state["shot_requested"]:
                        _fail("VISTA_HOME_REVIEW_SCREENSHOT_REJECTED", "worker attempted a second native capture")
                    image_path = Path(worker_execution["scratch_png"])
                    capture_width = execution["capture"]["width"]
                    capture_height = execution["capture"]["height"]
                    command = (
                        f'HighResShot {capture_width}x{capture_height} '
                        f'filename="{image_path}"'
                    )
                    state["shot_requested"] = True
                    unreal.log(f"VISTA_PLAYABLE_HOME_REVIEW_SCREENSHOT_REQUESTED {selected_camera['semantic_id']}")
                    unreal.SystemLibrary.execute_console_command(world, command)
                    state["phase"] = "await_file"
                    state["phase_started"] = now
                    state["stable_size"] = None
                    state["stable_since"] = None
                    return
                if state["phase"] == "await_file":
                    image_path = Path(worker_execution["scratch_png"])
                    if now - state["phase_started"] > SCREENSHOT_TIMEOUT_SECONDS:
                        _fail("VISTA_HOME_REVIEW_SCREENSHOT_TIMEOUT", f"PNG for {selected_camera['semantic_id']} did not stabilize")
                    if image_path.is_file():
                        size = image_path.stat().st_size
                        if size > 0:
                            if state["stable_size"] != size:
                                state["stable_size"] = size
                                state["stable_since"] = now
                                return
                            if state["stable_since"] is None or now - state["stable_since"] < 0.25:
                                return
                        else:
                            size = 0
                    else:
                        size = 0
                    if size <= 0:
                        return
                    if size > MAX_PNG_BYTES:
                        _fail("VISTA_HOME_REVIEW_PNG_INVALID", "native PNG exceeds safety bound")
                    state["captures"].append(
                        {
                            "ordinal": selected_camera["ordinal"],
                            "room_kind": selected_camera["room_kind"],
                            "room_id": selected_camera["room_id"],
                            "camera_id": selected_camera["camera_id"],
                            "semantic_id": selected_camera["semantic_id"],
                            "actor_label": str(actor.get_actor_label()),
                            "capture_method": state["capture_method"],
                            "actual_transform": actual_transform,
                            "actual_fov_deg": fov,
                            "relative_path": selected_camera["relative_path"],
                            "bytes": size,
                            "sha256": sha256_file(image_path),
                            "native_png_path": str(image_path),
                        }
                    )
                    eject()
                    finish()
            except Exception as exc:  # Unreal callback boundary
                finish(exc)

        state["handle"] = unreal.register_slate_post_tick_callback(on_tick)
        unreal.log("VISTA_PLAYABLE_HOME_REVIEW_CAPTURE_STARTED")
        return 0
    except Exception as exc:
        if worker_execution is not None:
            try:
                if isinstance(exc, ReviewCaptureError):
                    public_error = exc.public_dict()
                else:
                    public_error = {"code": "VISTA_HOME_REVIEW_UE_EXCEPTION", "message": str(exc)}
                _worker_write_result(
                    worker_execution,
                    _worker_result(
                        worker_execution,
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
    parser.add_argument(
        "--capture-profile",
        choices=CAPTURE_PROFILES,
        default=R1_CAPTURE_PROFILE,
        help="closed review-camera profile; fixed_r1 remains the compatibility default",
    )
    parser.add_argument(
        "--visual-profile",
        help=(
            "attempt-local contracts/visual-profile.json; required only for "
            "realistic_interior_r2"
        ),
    )
    parser.add_argument(
        "--visual-profile-sha256",
        help="lowercase SHA-256 pin paired with --visual-profile",
    )
    parser.add_argument(
        "--scratch-policy-root",
        help=(
            "approved private 0700 NFS/NFS4 policy root for append-only "
            "retained r2 evidence; rejected by fixed_r1"
        ),
    )
    parser.add_argument(
        "--scratch-parent",
        help=(
            "existing private 0700 same-mount directory below "
            "--scratch-policy-root; r2 children are retained and cleanup "
            "closes descriptors only"
        ),
    )
    parser.add_argument("--display", required=True, help="local X11 display, for example :117")
    parser.add_argument("--graphics-adapter", type=int, default=0, help="bounded Unreal graphics adapter index")
    parser.add_argument("--timeout-seconds", type=int, default=300, help="owned editor timeout (60-900 seconds)")
    parser.add_argument("--ddc-seed", help="optional pinned prior local DDC directory inside the UE attempt")
    parser.add_argument("--ddc-seed-tree-sha256", help="tree SHA-256 for --ddc-seed")
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
