#!/usr/bin/env python3
"""Materialize one deterministic packaged-game project from an accepted build.

The default mode is a zero-write dry run.  ``--apply`` creates exactly one
fresh ``package-linux-development/attempt-*`` child, copies only the accepted
runtime project inputs, regenerates the package-only descriptor/config/source,
and seals an append-only receipt.  It never runs Unreal, packages an archive,
deletes a failed attempt, or modifies the accepted source project.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import hmac
import json
import os
import re
import stat
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence


PLAN_SCHEMA = "simworld.vista.playable-home-package-project-plan/v1"
RECEIPT_SCHEMA = "simworld.vista.playable-home-package-project-receipt/v1"
SOURCE_RESULT_SCHEMA = "simworld.vista.playable-home-ue-build-result/v1"
SOURCE_SCENE_SCHEMA = "simworld.vista.playable-home-ue-scene-receipt/v1"
EXPECTED_REVISION = "vista_playable_home_r1"
EXPECTED_MAP_PATH = (
    "/Game/VISTA/PlayableHome/vista_playable_home_r1/Maps/VistaPlayableHome"
)
EXPECTED_PROJECT_NAME = "VistaPlayableHome.uproject"
EXPECTED_PLUGIN_NAME = "VistaPlayableHome"
EXPECTED_PARENT_NAME = "package-linux-development"
ATTEMPT_RE = re.compile(r"^attempt-[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_SOURCE_FILE_BYTES = 8 * 1024 * 1024 * 1024
MAX_SOURCE_FILES = 100_000
FICLONE = 0x40049409
PRIVATE_FILE_MODE = 0o600
PRIVATE_DIRECTORY_MODE = 0o700
TREE_ALGORITHM = "framed-canonical-project-entry-exact-mode-sha256/v1"
SOURCE_SANITIZATION_POLICY = "regenerate_not_copy"
MATERIALIZATION_RECEIPT = "materialization-receipt.json"
EXPECTED_RUN_UAT_SUFFIX = Path("Engine/Build/BatchFiles/RunUAT.sh")
PINNED_ENGINE_BUILD_VERSION_SHA256 = (
    "ffe01f6d1e96ef86cd06158cfb561150971823fc77e5c8df352910bcf4d365ef"
)
PINNED_ENGINE_VERSION = "5.7.3"
PINNED_ENGINE_CHANGELIST = 50162420
PINNED_RENDERER_CONTRACT_COMMIT = "3ce8ef48a2cb0aee881efeff94c3ea3a634fc56c"
PACKAGE_ENGINE_CONFIG_POLICY = (
    "3ce8-linux-targeted-rhis-sm6-plus-token-free-afs-regeneration+"
    "vsm-non-nanite-page-pressure-hardening/v3"
)
PROVEN_RUN_UAT_LOG = Path(
    "/mnt/NAS2/yhliu/SimWorldStudio/vista-playable-home/runs/"
    "20260815T110115Z-navfix/ue/package-linux-development/"
    "attempt-04-no-afs-clean/runuat.log"
)
PROVEN_RUN_UAT_LOG_SHA256 = (
    "acfb038af51bcc9ac14ede87e654be432b3c64e5256e87a4c0c351eeaae1b517"
)
PROVEN_PACKAGE_RECEIPT_SHA256 = (
    "c7dcd0bea0c2cb0de8f874857add910acfeca43af4caaf28295210c224734787"
)

EXCLUDED_DIRECTORY_NAMES = frozenset(
    {
        "binaries",
        "intermediate",
        "saved",
        "ddc",
        "deriveddatacache",
    }
)

SECRET_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    ("ue_android_file_server_token", re.compile(rb"SecurityToken\s*=", re.I)),
    (
        "private_key",
        re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    ),
    ("anthropic_token", re.compile(rb"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai_token", re.compile(rb"sk-(?:proj-)?[A-Za-z0-9_-]{32,}")),
    ("slack_token", re.compile(rb"xox[baprs]-[A-Za-z0-9-]{20,}")),
    ("github_token", re.compile(rb"gh[pousr]_[A-Za-z0-9]{20,}")),
    (
        "credentialed_uri",
        re.compile(
            rb"(?:postgres(?:ql)?|https?)://[^\x00\s/:]{1,80}:"
            rb"[^\x00\s/@]{8,80}@",
            re.I,
        ),
    ),
)


class PackageProjectError(RuntimeError):
    """Stable fail-closed materializer error."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _fail(code: str, message: str) -> None:
    raise PackageProjectError(code, message)


def canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PackageProjectError(
            "MATERIALIZER_JSON_INVALID", "value is not finite canonical JSON"
        ) from exc


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _content_digest(value: Mapping[str, Any]) -> str:
    body = dict(value)
    body.pop("content_digest", None)
    return sha256_bytes(canonical_json(body))


def _source_content_digest(value: Mapping[str, Any]) -> str:
    """Reproduce build_home.py's newline-terminated digest convention."""

    body = dict(value)
    body.pop("content_digest", None)
    return sha256_bytes(canonical_json(body) + b"\n")


def _duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("SOURCE_JSON_INVALID", "source JSON contains a duplicate key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    _fail("SOURCE_JSON_INVALID", "source JSON contains a non-finite value")


def _strict_json(
    raw: bytes, *, label: str, require_canonical: bool = True
) -> dict[str, Any]:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_duplicate_object,
            parse_constant=_reject_constant,
        )
    except PackageProjectError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackageProjectError(
            "SOURCE_JSON_INVALID", f"{label} is not strict UTF-8 JSON"
        ) from exc
    canonical = canonical_json(value) if isinstance(value, dict) else b""
    if not isinstance(value, dict) or (
        require_canonical and raw not in {canonical, canonical + b"\n"}
    ):
        _fail(
            "SOURCE_JSON_INVALID",
            f"{label} is not an allowed{' canonical' if require_canonical else ''} JSON object",
        )
    return value


def _require_sha256(value: str, *, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        _fail("SOURCE_PIN_INVALID", f"{label} must be a lowercase SHA-256")
    return value


def _absolute_lexical(path: Path, *, label: str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute() or ".." in candidate.parts:
        _fail("PATH_INVALID", f"{label} must be an absolute traversal-free path")
    return candidate


def _reject_symlink_components(
    path: Path, *, label: str, allow_missing_tail: bool = False
) -> None:
    candidate = _absolute_lexical(path, label=label)
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current = current / part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            if allow_missing_tail:
                return
            _fail("PATH_MISSING", f"{label} does not exist")
        except OSError as exc:
            raise PackageProjectError(
                "PATH_INSPECTION_FAILED", f"could not inspect {label}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            _fail("SYMLINK_REFUSED", f"{label} contains a symlink component")


def _existing_path(path: Path, *, label: str, directory: bool) -> Path:
    candidate = _absolute_lexical(path, label=label)
    _reject_symlink_components(candidate, label=label)
    try:
        metadata = os.lstat(candidate)
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise PackageProjectError(
            "PATH_INSPECTION_FAILED", f"could not inspect {label}"
        ) from exc
    expected = (
        stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
    )
    if not expected or resolved != candidate:
        _fail(
            "PATH_INVALID",
            f"{label} is not a canonical {'directory' if directory else 'file'}",
        )
    return candidate


def _identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        stat.S_IMODE(metadata.st_mode),
    )


@dataclass(frozen=True)
class FileSeal:
    path: Path
    sha256: str
    size_bytes: int
    mode: int
    identity: tuple[int, int, int, int, int, int]
    raw: bytes | None = None


@dataclass(frozen=True)
class SourceFile:
    relative_path: str
    seal: FileSeal
    copy_to_target: bool


@dataclass(frozen=True)
class DirectorySeal:
    relative_path: str
    mode: int
    identity: tuple[int, int, int, int, int, int]


@dataclass(frozen=True)
class TreeSnapshot:
    root: Path
    directories: tuple[DirectorySeal, ...]
    files: tuple[SourceFile, ...]
    tree_sha256: str
    total_bytes: int

    @property
    def file_count(self) -> int:
        return len(self.files)

    def summary(self) -> dict[str, Any]:
        return {
            "algorithm": TREE_ALGORITHM,
            "directory_count": len(self.directories),
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "tree_sha256": self.tree_sha256,
        }


def _secret_hits(blocks: Iterable[bytes]) -> set[str]:
    hits: set[str] = set()
    tail = b""
    for block in blocks:
        window = tail + block
        for name, pattern in SECRET_PATTERNS:
            if pattern.search(window):
                hits.add(name)
        tail = window[-512:]
    return hits


def _seal_file(
    path: Path,
    *,
    label: str,
    capture: bool = False,
    scan_secrets: bool = False,
    maximum_bytes: int = MAX_SOURCE_FILE_BYTES,
) -> FileSeal:
    digest = hashlib.sha256()
    captured = bytearray() if capture else None
    hits: set[str] = set()
    tail = b""
    descriptor = -1
    try:
        before = os.lstat(path)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size < 0
            or before.st_size > maximum_bytes
        ):
            _fail("SOURCE_FILE_INVALID", f"{label} is not an allowed regular file")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if _identity(opened) != _identity(before):
            _fail("SOURCE_CHANGED", f"{label} changed while opening")
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            digest.update(block)
            if captured is not None:
                captured.extend(block)
            if scan_secrets:
                window = tail + block
                for name, pattern in SECRET_PATTERNS:
                    if pattern.search(window):
                        hits.add(name)
                tail = window[-512:]
        after_open = os.fstat(descriptor)
        after_path = os.lstat(path)
    except PackageProjectError:
        raise
    except OSError as exc:
        raise PackageProjectError(
            "SOURCE_READ_FAILED", f"could not read {label}"
        ) from exc
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
    if _identity(after_open) != _identity(before) or _identity(after_path) != _identity(
        before
    ):
        _fail("SOURCE_CHANGED", f"{label} changed while reading")
    if hits:
        _fail(
            "SECRET_REFUSED",
            f"{label} matched forbidden credential policy {sorted(hits)[0]}",
        )
    return FileSeal(
        path=path,
        sha256=digest.hexdigest(),
        size_bytes=before.st_size,
        mode=stat.S_IMODE(before.st_mode),
        identity=_identity(before),
        raw=bytes(captured) if captured is not None else None,
    )


def _directory_seal(path: Path, relative_path: str, *, label: str) -> DirectorySeal:
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise PackageProjectError(
            "SOURCE_TREE_INVALID", f"could not inspect {label}"
        ) from exc
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        _fail("SOURCE_TREE_INVALID", f"{label} is not a real directory")
    return DirectorySeal(
        relative_path=relative_path,
        mode=stat.S_IMODE(metadata.st_mode),
        identity=_identity(metadata),
    )


def _inspect_excluded_tree(root: Path) -> None:
    """Reject links/special files even though this tree will not be copied."""

    inspected = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise PackageProjectError(
                "SOURCE_TREE_INVALID", "could not inspect excluded source tree"
            ) from exc
        for entry in entries:
            inspected += 1
            if inspected > MAX_SOURCE_FILES:
                _fail(
                    "SOURCE_TREE_INVALID",
                    "excluded source tree entry count exceeds policy",
                )
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise PackageProjectError(
                    "SOURCE_TREE_INVALID", "could not inspect excluded source entry"
                ) from exc
            if stat.S_ISLNK(metadata.st_mode):
                _fail("SYMLINK_REFUSED", "excluded source tree contains a symlink")
            if stat.S_ISDIR(metadata.st_mode):
                pending.append(Path(entry.path))
            elif not stat.S_ISREG(metadata.st_mode):
                _fail(
                    "SOURCE_TREE_INVALID",
                    "excluded source tree contains a special file",
                )


def _entry_hash(
    directories: Sequence[DirectorySeal], files: Sequence[SourceFile]
) -> str:
    digest = hashlib.sha256()
    records: list[dict[str, Any]] = [
        {"kind": "directory", "mode": item.mode, "path": item.relative_path}
        for item in directories
    ]
    records.extend(
        {
            "bytes": item.seal.size_bytes,
            "kind": "file",
            "mode": item.seal.mode,
            "path": item.relative_path,
            "sha256": item.seal.sha256,
        }
        for item in files
    )
    for record in sorted(records, key=lambda item: (item["path"], item["kind"])):
        raw = canonical_json(record)
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
    return digest.hexdigest()


def _walk_source_tree(
    root: Path,
    *,
    project_root: Path,
    copy_to_target: bool,
    scan_secrets: bool,
    excluded_names: frozenset[str] = EXCLUDED_DIRECTORY_NAMES,
) -> tuple[list[DirectorySeal], list[SourceFile]]:
    directories: list[DirectorySeal] = []
    files: list[SourceFile] = []

    def visit(directory: Path) -> None:
        relative = directory.relative_to(project_root).as_posix()
        directories.append(
            _directory_seal(directory, relative, label=f"source directory {relative}")
        )
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise PackageProjectError(
                "SOURCE_TREE_INVALID", "could not enumerate source project"
            ) from exc
        for entry in entries:
            candidate = directory / entry.name
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise PackageProjectError(
                    "SOURCE_TREE_INVALID", "could not inspect source project entry"
                ) from exc
            if stat.S_ISLNK(metadata.st_mode):
                _fail("SYMLINK_REFUSED", "source project tree contains a symlink")
            if stat.S_ISDIR(metadata.st_mode):
                if entry.name.casefold() in excluded_names:
                    _inspect_excluded_tree(candidate)
                    continue
                visit(candidate)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                _fail("SOURCE_TREE_INVALID", "source project contains a special file")
            relative_file = candidate.relative_to(project_root).as_posix()
            files.append(
                SourceFile(
                    relative_path=relative_file,
                    seal=_seal_file(
                        candidate,
                        label=f"source file {relative_file}",
                        scan_secrets=scan_secrets,
                    ),
                    copy_to_target=copy_to_target,
                )
            )
            if len(files) > MAX_SOURCE_FILES:
                _fail("SOURCE_TREE_INVALID", "source project file count exceeds policy")

    visit(root)
    return directories, files


def _snapshot_source_project(project_root: Path) -> TreeSnapshot:
    root = _existing_path(project_root, label="source project", directory=True)
    try:
        root_entries = sorted(os.scandir(root), key=lambda entry: entry.name)
    except OSError as exc:
        raise PackageProjectError(
            "SOURCE_TREE_INVALID", "could not enumerate source project root"
        ) from exc
    for entry in root_entries:
        try:
            metadata = entry.stat(follow_symlinks=False)
        except OSError as exc:
            raise PackageProjectError(
                "SOURCE_TREE_INVALID", "could not inspect source project root"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            _fail("SYMLINK_REFUSED", "source project root contains a symlink")
        if (
            stat.S_ISDIR(metadata.st_mode)
            and entry.name.casefold() in EXCLUDED_DIRECTORY_NAMES
        ):
            _inspect_excluded_tree(root / entry.name)

    descriptor = _existing_path(
        root / EXPECTED_PROJECT_NAME,
        label="source project descriptor",
        directory=False,
    )
    config = _existing_path(root / "Config", label="source Config", directory=True)
    default_engine = _existing_path(
        config / "DefaultEngine.ini", label="source DefaultEngine.ini", directory=False
    )
    default_input = _existing_path(
        config / "DefaultInput.ini", label="source DefaultInput.ini", directory=False
    )
    content = _existing_path(root / "Content", label="source Content", directory=True)
    plugin = _existing_path(
        root / "Plugins" / EXPECTED_PLUGIN_NAME,
        label="source VistaPlayableHome plugin",
        directory=True,
    )

    directories = [
        _directory_seal(root, ".", label="source project"),
        _directory_seal(config, "Config", label="source Config"),
        _directory_seal(plugin.parent, "Plugins", label="source Plugins directory"),
    ]
    files = [
        SourceFile(
            EXPECTED_PROJECT_NAME,
            _seal_file(
                descriptor,
                label="source project descriptor",
                capture=True,
                maximum_bytes=MAX_JSON_BYTES,
            ),
            False,
        ),
        SourceFile(
            "Config/DefaultEngine.ini",
            _seal_file(
                default_engine,
                label="source DefaultEngine.ini",
                capture=True,
                maximum_bytes=MAX_JSON_BYTES,
            ),
            False,
        ),
        SourceFile(
            "Config/DefaultInput.ini",
            _seal_file(
                default_input,
                label="source DefaultInput.ini",
                capture=True,
                scan_secrets=True,
                maximum_bytes=MAX_JSON_BYTES,
            ),
            True,
        ),
    ]
    content_directories, content_files = _walk_source_tree(
        content,
        project_root=root,
        copy_to_target=True,
        scan_secrets=True,
    )
    plugin_directories, plugin_files = _walk_source_tree(
        plugin,
        project_root=root,
        copy_to_target=True,
        scan_secrets=True,
    )
    directories.extend(content_directories)
    directories.extend(plugin_directories)
    files.extend(content_files)
    files.extend(plugin_files)
    directories = sorted(directories, key=lambda item: item.relative_path)
    files = sorted(files, key=lambda item: item.relative_path)
    total_bytes = sum(item.seal.size_bytes for item in files)
    return TreeSnapshot(
        root=root,
        directories=tuple(directories),
        files=tuple(files),
        tree_sha256=_entry_hash(directories, files),
        total_bytes=total_bytes,
    )


def source_project_tree_sha256(project_root: Path) -> str:
    """Return the exact source projection pin used by this materializer."""

    return _snapshot_source_project(project_root).tree_sha256


def _source_file(snapshot: TreeSnapshot, relative_path: str) -> SourceFile:
    for item in snapshot.files:
        if item.relative_path == relative_path:
            return item
    _fail("SOURCE_TREE_INVALID", f"source project is missing {relative_path}")


def _parse_ini(raw: bytes, *, label: str) -> dict[tuple[str, str], list[str]]:
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise PackageProjectError(
            "SOURCE_CONFIG_INVALID", f"{label} is not UTF-8"
        ) from exc
    section = ""
    values: dict[tuple[str, str], list[str]] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith((";", "#")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().casefold()
            continue
        if "=" not in line or not section:
            continue
        key, value = line.split("=", 1)
        values.setdefault((section, key.strip().casefold()), []).append(value.strip())
    return values


def _validate_source_engine(raw: bytes) -> None:
    values = _parse_ini(raw, label="source DefaultEngine.ini")
    required = {
        (
            "/script/enginesettings.gamemapssettings",
            "gamedefaultmap",
        ): EXPECTED_MAP_PATH,
        (
            "/script/enginesettings.gamemapssettings",
            "globaldefaultgamemode",
        ): "/Script/VistaPlayableHome.VistaPlayableHomeGameMode",
        (
            "/script/navigationsystem.recastnavmesh",
            "runtimegeneration",
        ): "Dynamic",
        (
            "/script/engine.renderersettings",
            "r.allowstaticlighting",
        ): "False",
    }
    for key, expected in required.items():
        observed = values.get(key)
        if observed != [expected]:
            _fail(
                "SOURCE_CONFIG_INVALID",
                "source DefaultEngine.ini lacks an exact required runtime setting",
            )


def _validate_default_input(raw: bytes) -> None:
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise PackageProjectError(
            "SOURCE_INPUT_INVALID", "source DefaultInput.ini is not UTF-8"
        ) from exc
    section = ""
    axes: set[tuple[str, str, float]] = set()
    actions: set[tuple[str, str]] = set()

    def field(line: str, key: str) -> str | None:
        match = re.search(
            rf"(?:\(|,){re.escape(key)}=(?:\"([^\"]*)\"|([^,)]*))",
            line,
        )
        if match is None:
            return None
        return match.group(1) if match.group(1) is not None else match.group(2)

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith((";", "#", "-")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().casefold()
            continue
        if section != "/script/engine.inputsettings":
            continue
        normalized = line[1:] if line.startswith("+") else line
        if normalized.startswith("AxisMappings=("):
            name = field(normalized, "AxisName")
            key = field(normalized, "Key")
            scale = field(normalized, "Scale")
            if name is None or key is None or scale is None:
                continue
            try:
                numeric_scale = float(scale)
            except ValueError:
                continue
            axes.add((name, key, numeric_scale))
        elif normalized.startswith("ActionMappings=("):
            name = field(normalized, "ActionName")
            key = field(normalized, "Key")
            if name is not None and key is not None:
                actions.add((name, key))

    required_axes = {
        ("MoveForward", "W", 1.0),
        ("MoveForward", "S", -1.0),
        ("MoveRight", "D", 1.0),
        ("MoveRight", "A", -1.0),
        ("Turn", "MouseX", 1.0),
        ("LookUp", "MouseY", -1.0),
    }
    required_actions = {
        ("Jump", "SpaceBar"),
        ("Sprint", "LeftShift"),
        ("Crouch", "C"),
        ("Interact", "E"),
        ("Drop", "Q"),
    }
    if not required_axes.issubset(axes) or not required_actions.issubset(actions):
        _fail("SOURCE_INPUT_INVALID", "source DefaultInput.ini lacks fixed controls")


def _canonical_project_descriptor() -> bytes:
    return canonical_json(
        {
            "Category": "Simulation",
            "Description": "Packaged VISTA Playable Home runtime project",
            "EngineAssociation": "5.7",
            "FileVersion": 3,
            "Modules": [
                {
                    "LoadingPhase": "Default",
                    "Name": "VistaPlayableHomeHost",
                    "Type": "Runtime",
                }
            ],
            "Plugins": [
                {"Enabled": True, "Name": "VistaPlayableHome"},
                {"Enabled": False, "Name": "AndroidFileServer"},
                {"Enabled": False, "Name": "PythonScriptPlugin"},
                {"Enabled": False, "Name": "EditorScriptingUtilities"},
                {"Enabled": False, "Name": "Interchange"},
            ],
        }
    )


def _canonical_engine_ini() -> bytes:
    # Exact closed projection of build_home.py at PINNED_RENDERER_CONTRACT_COMMIT
    # for realistic_interior_r2 / desktop_high_sm6, plus package-only UE 5.7.3
    # VSM hardening CVars. UE's non-Nanite marking queue is fixed at 128 jobs
    # per workgroup. Coarse-page exclusion retains directly requested visible
    # shadow pages, while the half-step LOD biases keep both skeletal characters
    # and large translucent presentation bundles below that fixed queue capacity.
    # This is configuration, not runtime renderer proof; the packaged
    # observation contract remains a separate post-package gate.
    lines = [
        "[/Script/EngineSettings.GameMapsSettings]",
        f"GameDefaultMap={EXPECTED_MAP_PATH}",
        f"EditorStartupMap={EXPECTED_MAP_PATH}",
        "GlobalDefaultGameMode=/Script/VistaPlayableHome.VistaPlayableHomeGameMode",
        "",
        "[/Script/NavigationSystem.RecastNavMesh]",
        "RuntimeGeneration=Dynamic",
        "",
        "[/Script/Engine.RendererSettings]",
        "r.AllowStaticLighting=False",
        "r.DynamicGlobalIlluminationMethod=1",
        "r.ReflectionMethod=1",
        "r.Shadow.Virtual.Enable=1",
        "r.AntiAliasingMethod=4",
        "r.Nanite.ProjectEnabled=True",
        "r.GenerateMeshDistanceFields=True",
        "r.DefaultFeature.AutoExposure.ExtendDefaultLuminanceRange=True",
        "r.EyeAdaptation.PreExposureOverride=0",
        "r.RayTracing=False",
        "r.Lumen.HardwareRayTracing=0",
        "",
        "[/Script/LinuxTargetPlatform.LinuxTargetSettings]",
        "-TargetedRHIs=SF_VULKAN_SM5",
        "+TargetedRHIs=SF_VULKAN_SM6",
        "",
        "[ConsoleVariables]",
        "r.ScreenPercentage=100.000000",
        "r.Streaming.PoolSize=8192",
        "r.Shadow.Virtual.NonNanite.IncludeInCoarsePages=0",
        "r.Shadow.Virtual.ResolutionLodBiasDirectional=0.500000",
        "r.Shadow.Virtual.ResolutionLodBiasDirectionalMoving=0.500000",
        "r.Shadow.Virtual.ResolutionLodBiasLocal=0.500000",
        "r.Shadow.Virtual.ResolutionLodBiasLocalMoving=0.500000",
        "sg.ViewDistanceQuality=3",
        "sg.AntiAliasingQuality=3",
        "sg.ShadowQuality=3",
        "sg.GlobalIlluminationQuality=3",
        "sg.ReflectionQuality=3",
        "sg.PostProcessQuality=3",
        "sg.TextureQuality=3",
        "sg.EffectsQuality=3",
        "sg.FoliageQuality=3",
        "sg.ShadingQuality=3",
        "",
        "[/Script/AndroidFileServerEditor.AndroidFileServerRuntimeSettings]",
        "bEnablePlugin=False",
        "bAllowNetworkConnection=False",
        "bIncludeInShipping=False",
        "bAllowExternalStartInShipping=False",
        "bCompileAFSProject=False",
        "",
    ]
    return "\n".join(lines).encode("utf-8")


GENERATED_SOURCE: dict[str, bytes] = {
    "Source/VistaPlayableHomeHost/Private/VistaPlayableHomeHost.cpp": b"""#include "Modules/ModuleManager.h"\n\nIMPLEMENT_PRIMARY_GAME_MODULE(\n    FDefaultGameModuleImpl,\n    VistaPlayableHomeHost,\n    "VistaPlayableHomeHost"\n);\n""",
    "Source/VistaPlayableHomeHost/VistaPlayableHomeHost.Build.cs": b"""using UnrealBuildTool;\n\npublic class VistaPlayableHomeHost : ModuleRules\n{\n    public VistaPlayableHomeHost(ReadOnlyTargetRules Target) : base(Target)\n    {\n        PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;\n        PrivateDependencyModuleNames.Add("Core");\n    }\n}\n""",
    "Source/VistaPlayableHome.Target.cs": b"""using UnrealBuildTool;\n\npublic class VistaPlayableHomeTarget : TargetRules\n{\n    public VistaPlayableHomeTarget(TargetInfo Target) : base(Target)\n    {\n        Type = TargetType.Game;\n        DefaultBuildSettings = BuildSettingsVersion.V6;\n        IncludeOrderVersion = EngineIncludeOrderVersion.Unreal5_7;\n        ExtraModuleNames.Add("VistaPlayableHomeHost");\n    }\n}\n""",
    "Source/VistaPlayableHomeEditor.Target.cs": b"""using UnrealBuildTool;\n\npublic class VistaPlayableHomeEditorTarget : TargetRules\n{\n    public VistaPlayableHomeEditorTarget(TargetInfo Target) : base(Target)\n    {\n        Type = TargetType.Editor;\n        DefaultBuildSettings = BuildSettingsVersion.V6;\n        IncludeOrderVersion = EngineIncludeOrderVersion.Unreal5_7;\n        ExtraModuleNames.Add("VistaPlayableHomeHost");\n    }\n}\n""",
}


def _generated_files() -> dict[str, bytes]:
    return {
        EXPECTED_PROJECT_NAME: _canonical_project_descriptor(),
        "Config/DefaultEngine.ini": _canonical_engine_ini(),
        **GENERATED_SOURCE,
    }


@dataclass(frozen=True)
class MaterializationConfig:
    source_build_result: Path
    source_build_result_sha256: str
    source_project: Path
    source_project_tree_sha256: str
    run_uat: Path
    run_uat_sha256: str
    attempt_root: Path


@dataclass(frozen=True)
class RunUatEvidence:
    run_uat: FileSeal
    build_version: FileSeal
    engine_root: Path

    def receipt_record(self) -> dict[str, Any]:
        return {
            "engine_root": str(self.engine_root),
            "engine_version": PINNED_ENGINE_VERSION,
            "engine_changelist": PINNED_ENGINE_CHANGELIST,
            "run_uat": {
                "path": str(self.run_uat.path),
                "sha256": self.run_uat.sha256,
                "bytes": self.run_uat.size_bytes,
                "mode": self.run_uat.mode,
            },
            "build_version": {
                "path": str(self.build_version.path),
                "sha256": self.build_version.sha256,
                "bytes": self.build_version.size_bytes,
                "mode": self.build_version.mode,
            },
        }


@dataclass(frozen=True)
class DestinationEvidence:
    parent: Path
    identity: tuple[int, int]

    def receipt_record(self) -> dict[str, Any]:
        return {
            "parent": str(self.parent),
            "parent_st_dev": self.identity[0],
            "parent_st_ino": self.identity[1],
            "policy": "plan-pinned-parent-st_dev-st_ino/v1",
        }


@dataclass(frozen=True)
class SourceEvidence:
    result: dict[str, Any]
    execution: dict[str, Any]
    scene_receipt: dict[str, Any]
    result_seal: FileSeal
    execution_seal: FileSeal
    scene_seal: FileSeal
    project_snapshot: TreeSnapshot


@dataclass(frozen=True)
class OutputFile:
    relative_path: str
    sha256: str
    size_bytes: int
    mode: int
    source: SourceFile | None
    raw: bytes | None


@dataclass(frozen=True)
class OutputSnapshot:
    directories: tuple[str, ...]
    files: tuple[OutputFile, ...]
    tree_sha256: str
    total_bytes: int

    def receipt_record(self) -> dict[str, Any]:
        return {
            "algorithm": TREE_ALGORITHM,
            "directories": [
                {"mode": PRIVATE_DIRECTORY_MODE, "path": path}
                for path in self.directories
            ],
            "directory_count": len(self.directories),
            "file_count": len(self.files),
            "files": [
                {
                    "bytes": item.size_bytes,
                    "mode": item.mode,
                    "path": item.relative_path,
                    "sha256": item.sha256,
                }
                for item in self.files
            ],
            "total_bytes": self.total_bytes,
            "tree_sha256": self.tree_sha256,
        }


@dataclass(frozen=True)
class MaterializationPlan:
    config: MaterializationConfig
    attempt_root: Path
    destination: DestinationEvidence
    source: SourceEvidence
    run_uat: RunUatEvidence
    output: OutputSnapshot
    report: dict[str, Any]


def _load_json_file(
    path: Path, *, label: str, require_canonical: bool = True
) -> tuple[dict[str, Any], FileSeal]:
    seal = _seal_file(
        path,
        label=label,
        capture=True,
        maximum_bytes=MAX_JSON_BYTES,
    )
    if seal.raw is None:
        raise AssertionError("captured JSON bytes are unavailable")
    return _strict_json(
        seal.raw, label=label, require_canonical=require_canonical
    ), seal


def _same_file_observation(first: FileSeal, second: FileSeal) -> bool:
    return (
        first.path == second.path
        and first.sha256 == second.sha256
        and first.size_bytes == second.size_bytes
        and first.mode == second.mode
        and first.identity == second.identity
    )


def _validate_run_uat(config: MaterializationConfig) -> RunUatEvidence:
    run_uat_path = _existing_path(
        config.run_uat,
        label="pinned RunUAT",
        directory=False,
    )
    if len(run_uat_path.parents) < 4:
        _fail("RUN_UAT_INVALID", "RunUAT path is outside an Unreal engine root")
    engine_root = run_uat_path.parents[3]
    if run_uat_path != engine_root / EXPECTED_RUN_UAT_SUFFIX:
        _fail("RUN_UAT_INVALID", "RunUAT path does not have the fixed engine layout")
    run_uat = _seal_file(
        run_uat_path,
        label="pinned RunUAT",
        scan_secrets=True,
        maximum_bytes=MAX_JSON_BYTES,
    )
    expected_sha = _require_sha256(config.run_uat_sha256, label="RunUAT pin")
    if not hmac.compare_digest(run_uat.sha256, expected_sha):
        _fail("RUN_UAT_PIN_MISMATCH", "RunUAT SHA-256 differs")
    if run_uat.mode & 0o111 == 0:
        _fail("RUN_UAT_INVALID", "pinned RunUAT is not executable")

    build_version_path = _existing_path(
        engine_root / "Engine/Build/Build.version",
        label="pinned Unreal Build.version",
        directory=False,
    )
    build_version = _seal_file(
        build_version_path,
        label="pinned Unreal Build.version",
        capture=True,
        maximum_bytes=MAX_JSON_BYTES,
    )
    if not hmac.compare_digest(
        build_version.sha256, PINNED_ENGINE_BUILD_VERSION_SHA256
    ):
        _fail("RUN_UAT_ENGINE_MISMATCH", "Unreal Build.version SHA-256 differs")
    if build_version.raw is None:
        raise AssertionError("captured Build.version bytes are unavailable")
    build = _strict_json(
        build_version.raw,
        label="pinned Unreal Build.version",
        require_canonical=False,
    )
    if (
        build.get("MajorVersion") != 5
        or build.get("MinorVersion") != 7
        or build.get("PatchVersion") != 3
        or build.get("Changelist") != PINNED_ENGINE_CHANGELIST
        or build.get("BranchName") != "++UE5+Release-5.7"
    ):
        _fail("RUN_UAT_ENGINE_MISMATCH", "Unreal engine identity differs")
    return RunUatEvidence(
        run_uat=run_uat,
        build_version=build_version,
        engine_root=engine_root,
    )


def _validate_source_descriptor(value: Mapping[str, Any]) -> None:
    plugins = value.get("Plugins")
    if not isinstance(plugins, list) or not any(
        isinstance(item, Mapping)
        and item.get("Name") == EXPECTED_PLUGIN_NAME
        and item.get("Enabled") is True
        for item in plugins
    ):
        _fail(
            "SOURCE_PROJECT_INVALID",
            "source descriptor does not enable VistaPlayableHome",
        )


def _validate_plugin_descriptor(value: Mapping[str, Any]) -> None:
    modules = value.get("Modules")
    if not isinstance(modules, list) or not any(
        isinstance(item, Mapping)
        and item.get("Name") == EXPECTED_PLUGIN_NAME
        and item.get("Type") == "Runtime"
        for item in modules
    ):
        _fail(
            "SOURCE_PLUGIN_INVALID",
            "source plugin does not declare the runtime module",
        )


def _validate_source_evidence(config: MaterializationConfig) -> SourceEvidence:
    result_path = _existing_path(
        config.source_build_result,
        label="source build result",
        directory=False,
    )
    if result_path.name != "result-receipt.json":
        _fail("SOURCE_RESULT_INVALID", "source build result name differs")
    expected_result_sha = _require_sha256(
        config.source_build_result_sha256,
        label="source build result pin",
    )
    result, result_seal = _load_json_file(result_path, label="source build result")
    if not hmac.compare_digest(result_seal.sha256, expected_result_sha):
        _fail("SOURCE_PIN_MISMATCH", "source build result SHA-256 differs")

    source_attempt = _existing_path(
        result_path.parent,
        label="source build attempt",
        directory=True,
    )
    source_project = _existing_path(
        config.source_project,
        label="source project",
        directory=True,
    )
    if source_project != source_attempt / "project":
        _fail(
            "SOURCE_PROJECT_INVALID",
            "source project must be the accepted result attempt's project",
        )
    if (
        result.get("schema_version") != SOURCE_RESULT_SCHEMA
        or result.get("status") != "accepted_candidate"
        or result.get("attempt_root") != str(source_attempt)
        or result.get("revision") != EXPECTED_REVISION
        or result.get("map_path") != EXPECTED_MAP_PATH
        or result.get("content_digest") != _source_content_digest(result)
    ):
        _fail("SOURCE_RESULT_INVALID", "source result is not the accepted fixed build")

    execution_sha = _require_sha256(
        str(result.get("execution_sha256", "")), label="source execution pin"
    )
    execution_path = _existing_path(
        source_attempt / "execution.json",
        label="source execution manifest",
        directory=False,
    )
    execution, execution_seal = _load_json_file(
        execution_path, label="source execution manifest"
    )
    if not hmac.compare_digest(execution_seal.sha256, execution_sha):
        _fail("SOURCE_PIN_MISMATCH", "source execution SHA-256 differs")

    source_descriptor = source_project / EXPECTED_PROJECT_NAME
    project_snapshot = _snapshot_source_project(source_project)
    expected_project_tree = _require_sha256(
        config.source_project_tree_sha256,
        label="source project tree pin",
    )
    if not hmac.compare_digest(project_snapshot.tree_sha256, expected_project_tree):
        _fail("SOURCE_PIN_MISMATCH", "source project projection SHA-256 differs")
    descriptor_entry = _source_file(project_snapshot, EXPECTED_PROJECT_NAME)
    if descriptor_entry.seal.raw is None:
        raise AssertionError("source descriptor bytes are unavailable")
    descriptor = _strict_json(
        descriptor_entry.seal.raw, label="source project descriptor"
    )
    _validate_source_descriptor(descriptor)
    if (
        execution.get("attempt_root") != str(source_attempt)
        or execution.get("project_file") != str(source_descriptor)
        or execution.get("project_sha256") != descriptor_entry.seal.sha256
    ):
        _fail(
            "SOURCE_EXECUTION_INVALID",
            "source execution does not bind the accepted project",
        )

    scene_pin = _require_sha256(
        str(result.get("scene_receipt_sha256", "")),
        label="source scene receipt pin",
    )
    expected_scene_path = source_attempt / "scene-receipt.json"
    if execution.get("scene_receipt") != str(expected_scene_path):
        _fail("SOURCE_SCENE_INVALID", "source execution scene receipt path differs")
    scene_path = _existing_path(
        expected_scene_path,
        label="source scene receipt",
        directory=False,
    )
    scene, scene_seal = _load_json_file(scene_path, label="source scene receipt")
    if not hmac.compare_digest(scene_seal.sha256, scene_pin):
        _fail("SOURCE_PIN_MISMATCH", "source scene receipt SHA-256 differs")

    input_entry = _source_file(project_snapshot, "Config/DefaultInput.ini")
    engine_entry = _source_file(project_snapshot, "Config/DefaultEngine.ini")
    if input_entry.seal.raw is None or engine_entry.seal.raw is None:
        raise AssertionError("source config bytes are unavailable")
    _validate_source_engine(engine_entry.seal.raw)
    _validate_default_input(input_entry.seal.raw)
    scene_bindings = scene.get("bindings")
    scene_gates = scene.get("gates")
    if (
        scene.get("schema_version") != SOURCE_SCENE_SCHEMA
        or scene.get("status") != "saved_reloaded_candidate"
        or not isinstance(scene_bindings, Mapping)
        or not isinstance(scene_gates, Mapping)
        or scene_bindings.get("project") != str(source_descriptor)
        or scene_bindings.get("execution_manifest") != str(execution_path)
        or scene_bindings.get("execution_manifest_sha256") != execution_sha
        or scene_bindings.get("input_config")
        != str(source_project / "Config/DefaultInput.ini")
        or not isinstance(scene_bindings.get("input_config_sha256"), str)
        or SHA256_RE.fullmatch(scene_bindings["input_config_sha256"]) is None
        or scene_gates.get("input_mappings_verified") is not True
        or scene_gates.get("map_saved") is not True
        or scene_gates.get("map_reloaded") is not True
        or scene_gates.get("game_mode_configured") is not True
        or scene_gates.get("navmesh_bounds_verified") is not True
        or scene_gates.get("quarantined") is not False
    ):
        _fail(
            "SOURCE_SCENE_INVALID",
            "source scene receipt does not verify project input and runtime gates",
        )

    map_asset = (
        source_project / "Content/VISTA/PlayableHome/vista_playable_home_r1/Maps/"
        "VistaPlayableHome.umap"
    )
    _existing_path(map_asset, label="source packaged map asset", directory=False)
    plugin_descriptor_path = (
        source_project / "Plugins" / EXPECTED_PLUGIN_NAME / "VistaPlayableHome.uplugin"
    )
    plugin_descriptor, plugin_descriptor_seal = _load_json_file(
        plugin_descriptor_path,
        label="source VistaPlayableHome plugin descriptor",
        require_canonical=False,
    )
    plugin_entry = _source_file(
        project_snapshot,
        "Plugins/VistaPlayableHome/VistaPlayableHome.uplugin",
    )
    if not _same_file_observation(plugin_descriptor_seal, plugin_entry.seal):
        _fail("SOURCE_CHANGED", "source plugin descriptor changed during validation")
    _validate_plugin_descriptor(plugin_descriptor)
    plugin_source = source_project / "Plugins/VistaPlayableHome/Source"
    _existing_path(plugin_source, label="source plugin Source", directory=True)
    if not any(
        item.relative_path.startswith("Plugins/VistaPlayableHome/Source/")
        and item.relative_path.endswith((".cpp", ".h", ".cs"))
        for item in project_snapshot.files
    ):
        _fail("SOURCE_PLUGIN_INVALID", "source plugin runtime source is empty")

    return SourceEvidence(
        result=result,
        execution=execution,
        scene_receipt=scene,
        result_seal=result_seal,
        execution_seal=execution_seal,
        scene_seal=scene_seal,
        project_snapshot=project_snapshot,
    )


def _validate_destination(
    config: MaterializationConfig, source: SourceEvidence
) -> Path:
    attempt = _absolute_lexical(config.attempt_root, label="package attempt")
    _reject_symlink_components(
        attempt, label="package attempt", allow_missing_tail=True
    )
    parent = _existing_path(
        attempt.parent,
        label="package-linux-development root",
        directory=True,
    )
    if (
        parent.name != EXPECTED_PARENT_NAME
        or ATTEMPT_RE.fullmatch(attempt.name) is None
    ):
        _fail(
            "DESTINATION_INVALID",
            "package attempt must be a named direct package-linux-development child",
        )
    if attempt.exists() or attempt.is_symlink():
        _fail("DESTINATION_EXISTS", "append-only package attempt already exists")
    source_attempt = source.result_seal.path.parent
    try:
        attempt.relative_to(source_attempt)
    except ValueError:
        pass
    else:
        _fail("DESTINATION_INVALID", "package attempt cannot be inside accepted source")
    try:
        source_attempt.relative_to(attempt)
    except ValueError:
        pass
    else:
        _fail("DESTINATION_INVALID", "accepted source cannot be inside package attempt")
    return attempt


def _seal_destination(attempt: Path) -> DestinationEvidence:
    parent = attempt.parent
    try:
        metadata = os.lstat(parent)
    except OSError as exc:
        raise PackageProjectError(
            "DESTINATION_CHANGED", "could not seal package destination parent"
        ) from exc
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        _fail("DESTINATION_CHANGED", "package destination parent is not real")
    return DestinationEvidence(
        parent=parent,
        identity=(metadata.st_dev, metadata.st_ino),
    )


def _output_tree_hash(directories: Sequence[str], files: Sequence[OutputFile]) -> str:
    digest = hashlib.sha256()
    records: list[dict[str, Any]] = [
        {"kind": "directory", "mode": PRIVATE_DIRECTORY_MODE, "path": path}
        for path in directories
    ]
    records.extend(
        {
            "bytes": item.size_bytes,
            "kind": "file",
            "mode": item.mode,
            "path": item.relative_path,
            "sha256": item.sha256,
        }
        for item in files
    )
    for record in sorted(records, key=lambda item: (item["path"], item["kind"])):
        raw = canonical_json(record)
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
    return digest.hexdigest()


def _build_output_snapshot(source: SourceEvidence) -> OutputSnapshot:
    outputs: dict[str, OutputFile] = {}
    generated = _generated_files()
    for relative_path, raw in generated.items():
        hits = _secret_hits((raw,))
        if hits:
            _fail("SECRET_REFUSED", "generated package project matched secret policy")
        outputs[relative_path] = OutputFile(
            relative_path=relative_path,
            sha256=sha256_bytes(raw),
            size_bytes=len(raw),
            mode=PRIVATE_FILE_MODE,
            source=None,
            raw=raw,
        )
    for source_file in source.project_snapshot.files:
        if not source_file.copy_to_target:
            continue
        relative_path = source_file.relative_path
        if relative_path in outputs:
            _fail("OUTPUT_COLLISION", "generated and copied package paths collide")
        outputs[relative_path] = OutputFile(
            relative_path=relative_path,
            sha256=source_file.seal.sha256,
            size_bytes=source_file.seal.size_bytes,
            mode=PRIVATE_FILE_MODE,
            source=source_file,
            raw=None,
        )
    folded: dict[str, str] = {}
    for relative_path in outputs:
        normalized = PurePosixPath(relative_path)
        if normalized.is_absolute() or ".." in normalized.parts:
            _fail("OUTPUT_PATH_INVALID", "package output path is unsafe")
        key = relative_path.casefold()
        if key in folded and folded[key] != relative_path:
            _fail("OUTPUT_COLLISION", "package output paths collide by case")
        folded[key] = relative_path
    directories = {"."}
    for relative_path in outputs:
        parent = PurePosixPath(relative_path).parent
        while parent.as_posix() != ".":
            directories.add(parent.as_posix())
            parent = parent.parent
    files = tuple(sorted(outputs.values(), key=lambda item: item.relative_path))
    directory_tuple = tuple(sorted(directories))
    return OutputSnapshot(
        directories=directory_tuple,
        files=files,
        tree_sha256=_output_tree_hash(directory_tuple, files),
        total_bytes=sum(item.size_bytes for item in files),
    )


def _source_binding_record(source: SourceEvidence) -> dict[str, Any]:
    engine = _source_file(source.project_snapshot, "Config/DefaultEngine.ini").seal
    input_config = _source_file(source.project_snapshot, "Config/DefaultInput.ini").seal
    return {
        "build_result": {
            "path": str(source.result_seal.path),
            "sha256": source.result_seal.sha256,
        },
        "execution": {
            "path": str(source.execution_seal.path),
            "sha256": source.execution_seal.sha256,
        },
        "project": {
            "path": str(source.project_snapshot.root),
            **source.project_snapshot.summary(),
        },
        "scene_receipt": {
            "path": str(source.scene_seal.path),
            "sha256": source.scene_seal.sha256,
        },
        "source_default_engine": {
            "bytes": engine.size_bytes,
            "package_config_sha256": sha256_bytes(_canonical_engine_ini()),
            "renderer_contract_commit": PINNED_RENDERER_CONTRACT_COMMIT,
            "sanitized_policy": SOURCE_SANITIZATION_POLICY,
            "sha256": engine.sha256,
            "transformation": PACKAGE_ENGINE_CONFIG_POLICY,
        },
        "verified_default_input": {
            "bytes": input_config.size_bytes,
            "sha256": input_config.sha256,
            "scene_receipt_declared_sha256": source.scene_receipt["bindings"][
                "input_config_sha256"
            ],
            "verification": (
                "accepted-scene-input-gate+current-project-tree-pin+"
                "fixed-control-semantics/v1"
            ),
        },
    }


def _runuat_contract(attempt: Path, evidence: RunUatEvidence) -> dict[str, Any]:
    project = attempt / "project" / EXPECTED_PROJECT_NAME
    return {
        "input": evidence.receipt_record(),
        "argv": [
            str(evidence.run_uat.path),
            "-nocompileuat",
            "BuildCookRun",
            f"-project={project}",
            "-target=VistaPlayableHome",
            "-nop4",
            "-platform=Linux",
            "-clientconfig=Development",
            "-build",
            "-cook",
            f"-map={EXPECTED_MAP_PATH}",
            f"-CookOutputDir={attempt / 'cooked/Linux'}",
            (
                "-AdditionalCookerOptions=-nullrhi -unattended -NoSplash "
                "-NoSound -NoAnalytics -ddc=InstalledNoZenLocalFallback"
            ),
            "-ubtargs=-NoUBA -MaxParallelActions=6",
            "-stage",
            "-package",
            "-pak",
            "-skipiostore",
            "-archive",
            f"-stagingdirectory={attempt / 'stage'}",
            f"-archivedirectory={attempt / 'archive'}",
            "-NoCodeSign",
            "-unattended",
            "-utf8output",
        ],
        "log": str(attempt / "runuat.log"),
        "policy": "operator-runs-pinned-proven-runuat-after-materialization/v2",
        "consumer_precondition": (
            "pin-terminal-accepted-receipt-sha256+recompute-project-tree-"
            "immediately-before-runuat/v1"
        ),
        "provenance": {
            "runuat_log": str(PROVEN_RUN_UAT_LOG),
            "runuat_log_sha256": PROVEN_RUN_UAT_LOG_SHA256,
            "accepted_package_receipt_sha256": PROVEN_PACKAGE_RECEIPT_SHA256,
            "contract": "successful-linux-development-build-cook-stage-package-archive/v1",
            "scope": "packaging_mechanics_only_not_r2_renderer_runtime_proof",
        },
    }


def plan_materialization(
    config: MaterializationConfig, *, apply: bool = False
) -> MaterializationPlan:
    source = _validate_source_evidence(config)
    run_uat = _validate_run_uat(config)
    attempt = _validate_destination(config, source)
    destination = _seal_destination(attempt)
    output = _build_output_snapshot(source)
    report: dict[str, Any] = {
        "schema_version": PLAN_SCHEMA,
        "status": "ready",
        "mode": "apply" if apply else "dry_run",
        "attempt_root": str(attempt),
        "project_root": str(attempt / "project"),
        "destination": destination.receipt_record(),
        "source": _source_binding_record(source),
        "project": output.receipt_record(),
        "policy": {
            "apply_requires_fresh_direct_child": True,
            "append_only_attempt": True,
            "copy_roots": [
                "Config/DefaultInput.ini",
                "Content",
                "Plugins/VistaPlayableHome",
            ],
            "excluded_directory_names": sorted(EXCLUDED_DIRECTORY_NAMES),
            "private_directory_mode": PRIVATE_DIRECTORY_MODE,
            "private_file_mode": PRIVATE_FILE_MODE,
            "project_descriptor": "canonical_runtime_only/v1",
            "default_engine": PACKAGE_ENGINE_CONFIG_POLICY,
            "default_input": "preserve_verified_bytes/v1",
            "copy_transport": "reflink_with_byte_fallback/v1",
            "secret_scan": "final_copy_eligible_and_output_zero_hits/v1",
            "failed_attempt_retention": "failed_quarantined_never_deleted/v1",
            "failed_partial_outputs": "retain_never_unlink/v1",
            "receipt_commit": (
                "pending-readback+final-resnapshot+atomic-noreplace-publication/v1"
            ),
            "source_mutation": "pre_copy_post_copy_final_fail_closed/v1",
            "run_uat_mutation": "pre_create_post_copy_final_fail_closed/v1",
            "destination_containment": (
                "plan-pinned-parent+exclusive-cooperative-lock+private-staging+"
                "retained-dirfd-openat-no-follow/v3"
            ),
        },
        "runuat": _runuat_contract(attempt, run_uat),
        "output": str(attempt / MATERIALIZATION_RECEIPT),
    }
    report["content_digest"] = _content_digest(report)
    if _secret_hits((canonical_json(report),)):
        _fail("SECRET_REFUSED", "materialization plan matched secret policy")
    return MaterializationPlan(
        config=config,
        attempt_root=attempt,
        destination=destination,
        source=source,
        run_uat=run_uat,
        output=output,
        report=report,
    )


def _close_best_effort(descriptor: int) -> None:
    if descriptor < 0:
        return
    try:
        os.close(descriptor)
    except OSError:
        pass


def _write_all(descriptor: int, raw: bytes) -> None:
    view = memoryview(raw)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError(errno.EIO, "short write")
        view = view[written:]


@dataclass(frozen=True)
class DestinationAnchor:
    parent_path: Path
    attempt_name: str
    parent_fd: int
    parent_identity: tuple[int, int]


@dataclass(frozen=True)
class AnchoredTarget:
    parent_fd: int
    name: str
    display_path: Path


def _directory_inode(metadata: os.stat_result) -> tuple[int, int]:
    return (metadata.st_dev, metadata.st_ino)


def _safe_component(name: str) -> str:
    if not name or name in {".", ".."} or "/" in name or "\x00" in name:
        _fail("OUTPUT_PATH_INVALID", "anchored output component is unsafe")
    return name


def _open_directory_at(parent_fd: int, name: str) -> int:
    component = _safe_component(name)
    descriptor = -1
    try:
        before = os.stat(
            component,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if not stat.S_ISDIR(before.st_mode) or stat.S_ISLNK(before.st_mode):
            _fail("SYMLINK_REFUSED", "anchored output directory is not real")
        descriptor = os.open(
            component,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode) or _directory_inode(
            opened
        ) != _directory_inode(before):
            _fail("DESTINATION_CHANGED", "anchored output directory changed")
        return descriptor
    except PackageProjectError:
        _close_best_effort(descriptor)
        raise
    except OSError as exc:
        _close_best_effort(descriptor)
        raise PackageProjectError(
            "MATERIALIZATION_WRITE_FAILED", "could not open anchored output directory"
        ) from exc


def _open_destination_anchor(plan: MaterializationPlan) -> DestinationAnchor:
    attempt = _validate_destination(plan.config, plan.source)
    if attempt != plan.attempt_root:
        raise AssertionError("apply destination differs from the validated plan")
    parent = attempt.parent
    if parent != plan.destination.parent:
        raise AssertionError("apply destination parent differs from the plan")
    descriptor = -1
    try:
        descriptor = os.open(
            parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        _reject_symlink_components(parent, label="package-linux-development root")
        path_metadata = os.lstat(parent)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(path_metadata.st_mode)
            or stat.S_ISLNK(path_metadata.st_mode)
            or parent.resolve(strict=True) != parent
            or _directory_inode(opened) != _directory_inode(path_metadata)
            or _directory_inode(opened) != plan.destination.identity
        ):
            _fail("DESTINATION_CHANGED", "package root changed while anchoring")
        try:
            os.stat(attempt.name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            _fail("DESTINATION_EXISTS", "append-only package attempt already exists")
        return DestinationAnchor(
            parent_path=parent,
            attempt_name=attempt.name,
            parent_fd=descriptor,
            parent_identity=_directory_inode(opened),
        )
    except PackageProjectError:
        _close_best_effort(descriptor)
        raise
    except OSError as exc:
        _close_best_effort(descriptor)
        raise PackageProjectError(
            "DESTINATION_CHANGED", "could not anchor package destination"
        ) from exc


def _assert_destination_anchor_stable(anchor: DestinationAnchor) -> None:
    try:
        _reject_symlink_components(
            anchor.parent_path,
            label="package-linux-development root",
        )
        current = os.lstat(anchor.parent_path)
        opened = os.fstat(anchor.parent_fd)
        resolved = anchor.parent_path.resolve(strict=True)
    except PackageProjectError:
        raise
    except OSError as exc:
        raise PackageProjectError(
            "DESTINATION_CHANGED", "package destination binding disappeared"
        ) from exc
    if (
        not stat.S_ISDIR(current.st_mode)
        or stat.S_ISLNK(current.st_mode)
        or resolved != anchor.parent_path
        or _directory_inode(current) != anchor.parent_identity
        or _directory_inode(opened) != anchor.parent_identity
    ):
        _fail("DESTINATION_CHANGED", "package destination binding changed")


def _assert_attempt_anchor_stable(
    anchor: DestinationAnchor,
    attempt_fd: int,
) -> None:
    try:
        entry = os.stat(
            anchor.attempt_name,
            dir_fd=anchor.parent_fd,
            follow_symlinks=False,
        )
        opened = os.fstat(attempt_fd)
    except OSError as exc:
        raise PackageProjectError(
            "DESTINATION_CHANGED", "package attempt binding disappeared"
        ) from exc
    if (
        not stat.S_ISDIR(entry.st_mode)
        or stat.S_ISLNK(entry.st_mode)
        or not stat.S_ISDIR(opened.st_mode)
        or _directory_inode(entry) != _directory_inode(opened)
    ):
        _fail("DESTINATION_CHANGED", "package attempt binding changed")


def _assert_project_directory_bindings_stable(
    attempt_fd: int,
    directory_fds: Mapping[str, int],
) -> None:
    project_fd = directory_fds.get(".")
    if project_fd is None:
        _fail("DESTINATION_CHANGED", "materialized project binding is unavailable")
    bindings: list[tuple[int, str, int]] = [(attempt_fd, "project", project_fd)]
    for relative, child_fd in directory_fds.items():
        if relative == ".":
            continue
        path = PurePosixPath(relative)
        parent_fd = directory_fds.get(path.parent.as_posix())
        if parent_fd is None:
            _fail("DESTINATION_CHANGED", "materialized directory parent is unavailable")
        bindings.append((parent_fd, path.name, child_fd))
    try:
        for parent_fd, name, child_fd in bindings:
            entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            opened = os.fstat(child_fd)
            if (
                not stat.S_ISDIR(entry.st_mode)
                or stat.S_ISLNK(entry.st_mode)
                or not stat.S_ISDIR(opened.st_mode)
                or _directory_inode(entry) != _directory_inode(opened)
            ):
                _fail(
                    "DESTINATION_CHANGED",
                    "materialized directory pathname binding changed",
                )
    except PackageProjectError:
        raise
    except OSError as exc:
        raise PackageProjectError(
            "DESTINATION_CHANGED",
            "materialized directory pathname binding disappeared",
        ) from exc


def _bind_created_directory_at(parent_fd: int, name: str) -> int:
    """Bind a just-created directory and secure it under a hostile umask.

    Linux ``O_PATH`` can bind a mode-000 directory without traversing it.  The
    procfs descriptor link applies chmod and obtains a readable directory fd
    from that exact bound inode rather than resolving the destination name a
    second time.
    """

    component = _safe_component(name)
    path_flag = getattr(os, "O_PATH", 0)
    if path_flag == 0:  # pragma: no cover - this tool is Linux-only by contract.
        _fail("MATERIALIZATION_WRITE_FAILED", "Linux O_PATH support is required")
    path_descriptor = -1
    descriptor = -1
    try:
        path_descriptor = os.open(
            component,
            path_flag
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        opened = os.fstat(path_descriptor)
        before = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
        ):
            _fail("DESTINATION_CHANGED", "created output directory was replaced")
        if _directory_inode(opened) != _directory_inode(before):
            _fail("DESTINATION_CHANGED", "created output directory changed")
        descriptor_path = Path("/proc/self/fd") / str(path_descriptor)
        os.chmod(descriptor_path, PRIVATE_DIRECTORY_MODE)
        descriptor = os.open(
            descriptor_path,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        after_open = os.fstat(descriptor)
        after_entry = os.stat(
            component,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (
            _directory_inode(after_open) != _directory_inode(before)
            or _directory_inode(after_entry) != _directory_inode(before)
            or stat.S_IMODE(after_open.st_mode) != PRIVATE_DIRECTORY_MODE
        ):
            _fail("DESTINATION_CHANGED", "created output directory changed")
        return descriptor
    except PackageProjectError:
        _close_best_effort(descriptor)
        raise
    except OSError as exc:
        _close_best_effort(descriptor)
        raise PackageProjectError(
            "MATERIALIZATION_WRITE_FAILED",
            "could not secure created output directory",
        ) from exc
    finally:
        _close_best_effort(path_descriptor)


def _link_noreplace_at(
    parent_fd: int,
    source_name: str,
    destination_name: str,
    expected: FileSeal,
) -> None:
    try:
        os.link(
            _safe_component(source_name),
            _safe_component(destination_name),
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except BaseException as exc:
        terminal_state = _terminal_receipt_state_at(
            parent_fd,
            source_name,
            destination_name,
            expected,
        )
        if terminal_state == "match":
            return
        if isinstance(exc, FileExistsError) and terminal_state == "different":
            raise PackageProjectError(
                "DESTINATION_EXISTS",
                "terminal materialization receipt already exists",
            ) from exc
        if terminal_state in {"missing", "different", "unknown"}:
            raise PackageProjectError(
                "RECEIPT_COMMIT_OUTCOME_UNKNOWN",
                "terminal receipt publication could not be reconciled",
            ) from exc
        raise AssertionError("unhandled terminal receipt reconciliation state")


def _terminal_receipt_state_at(
    parent_fd: int,
    pending_name: str,
    terminal_name: str,
    expected: FileSeal,
) -> str:
    try:
        pending = os.stat(
            _safe_component(pending_name),
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        terminal = os.stat(
            _safe_component(terminal_name),
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(pending.st_mode)
            or not stat.S_ISREG(terminal.st_mode)
            or _directory_inode(pending) != _directory_inode(terminal)
            or _directory_inode(terminal)
            != (expected.identity[0], expected.identity[1])
        ):
            return "different"
        observed = _seal_file_at(
            parent_fd,
            terminal_name,
            expected.path,
            label="terminal materialization receipt",
        )
    except FileNotFoundError:
        return "missing"
    except BaseException:
        return "unknown"
    matches = (
        observed.sha256 == expected.sha256
        and observed.size_bytes == expected.size_bytes
        and observed.mode == expected.mode
    )
    return "match" if matches else "different"


def _publish_staged_attempt_at(
    anchor: DestinationAnchor,
    staging_name: str,
    staging_fd: int,
    publication_lock_name: str,
    publication_lock: FileSeal,
) -> None:
    _assert_file_binding_stable(
        anchor.parent_fd,
        publication_lock_name,
        publication_lock,
    )
    staging_entry = os.stat(
        staging_name,
        dir_fd=anchor.parent_fd,
        follow_symlinks=False,
    )
    if (
        not stat.S_ISDIR(staging_entry.st_mode)
        or stat.S_ISLNK(staging_entry.st_mode)
        or _directory_inode(staging_entry) != _directory_inode(os.fstat(staging_fd))
    ):
        _fail("DESTINATION_CHANGED", "private attempt staging binding changed")
    try:
        os.stat(
            anchor.attempt_name,
            dir_fd=anchor.parent_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        pass
    else:
        _fail("DESTINATION_EXISTS", "append-only package attempt already exists")
    try:
        os.rename(
            staging_name,
            anchor.attempt_name,
            src_dir_fd=anchor.parent_fd,
            dst_dir_fd=anchor.parent_fd,
        )
    except OSError as exc:
        raise PackageProjectError(
            "MATERIALIZATION_WRITE_FAILED",
            "could not publish anchored package attempt",
        ) from exc


def _create_attempt_at(anchor: DestinationAnchor) -> int:
    publication_lock_name = f".attempt-publication-{anchor.attempt_name}.lock"
    publication_lock = _write_exclusive_at(
        AnchoredTarget(
            parent_fd=anchor.parent_fd,
            name=publication_lock_name,
            display_path=anchor.parent_path / publication_lock_name,
        ),
        canonical_json(
            {
                "attempt_name": anchor.attempt_name,
                "policy": "exclusive-cooperative-publication-lock/v1",
            }
        ),
    )
    staging_name = f".materializing-{anchor.attempt_name}-{os.urandom(16).hex()}"
    descriptor = -1
    try:
        os.mkdir(
            staging_name,
            PRIVATE_DIRECTORY_MODE,
            dir_fd=anchor.parent_fd,
        )
        descriptor = _bind_created_directory_at(anchor.parent_fd, staging_name)
        _publish_staged_attempt_at(
            anchor,
            staging_name,
            descriptor,
            publication_lock_name,
            publication_lock,
        )
        # Successful rename under the retained exclusive publication lock is
        # the cooperative publication commit.
        return descriptor
    except FileExistsError as exc:
        _close_best_effort(descriptor)
        raise PackageProjectError(
            "DESTINATION_EXISTS", "append-only package attempt already exists"
        ) from exc
    except PackageProjectError:
        _close_best_effort(descriptor)
        raise
    except OSError as exc:
        _close_best_effort(descriptor)
        raise PackageProjectError(
            "MATERIALIZATION_WRITE_FAILED", "could not create anchored package attempt"
        ) from exc


def _mkdir_private_at(parent_fd: int, name: str) -> int:
    component = _safe_component(name)
    try:
        os.mkdir(component, PRIVATE_DIRECTORY_MODE, dir_fd=parent_fd)
        return _bind_created_directory_at(parent_fd, component)
    except PackageProjectError:
        raise
    except OSError as exc:
        raise PackageProjectError(
            "MATERIALIZATION_WRITE_FAILED",
            "could not create private anchored directory",
        ) from exc


def _write_exclusive_at(target: AnchoredTarget, raw: bytes) -> FileSeal:
    descriptor = -1
    committed = False
    committed_seal: FileSeal | None = None
    try:
        descriptor = os.open(
            _safe_component(target.name),
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            PRIVATE_FILE_MODE,
            dir_fd=target.parent_fd,
        )
        os.fchmod(descriptor, PRIVATE_FILE_MODE)
        _write_all(descriptor, raw)
        os.fsync(descriptor)
        written = os.fstat(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        observed_bytes = 0
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            observed_bytes += len(block)
            digest.update(block)
        opened = os.fstat(descriptor)
        entry = os.stat(
            _safe_component(target.name),
            dir_fd=target.parent_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(entry.st_mode)
            or _identity(written) != _identity(opened)
            or _identity(opened) != _identity(entry)
            or _directory_inode(opened) != _directory_inode(entry)
            or observed_bytes != len(raw)
            or digest.hexdigest() != sha256_bytes(raw)
            or opened.st_size != len(raw)
            or entry.st_size != len(raw)
            or stat.S_IMODE(opened.st_mode) != PRIVATE_FILE_MODE
            or stat.S_IMODE(entry.st_mode) != PRIVATE_FILE_MODE
        ):
            _fail("DESTINATION_CHANGED", "exclusive output binding or bytes changed")
        committed_seal = FileSeal(
            path=target.display_path,
            sha256=digest.hexdigest(),
            size_bytes=observed_bytes,
            mode=stat.S_IMODE(opened.st_mode),
            identity=_identity(opened),
        )
        committed = True
    except BaseException:
        _close_best_effort(descriptor)
        descriptor = -1
        raise
    finally:
        _close_best_effort(descriptor)
    if not committed:  # pragma: no cover - defensive state assertion.
        raise AssertionError("exclusive write did not commit")
    if committed_seal is None:  # pragma: no cover - defensive state assertion.
        raise AssertionError("exclusive write seal is unavailable")
    return committed_seal


def _assert_file_binding_stable(
    parent_fd: int,
    name: str,
    expected: FileSeal,
) -> None:
    observed = _seal_file_at(
        parent_fd,
        name,
        expected.path,
        label="sealed materialization receipt",
    )
    if (
        observed.sha256 != expected.sha256
        or observed.size_bytes != expected.size_bytes
        or observed.mode != expected.mode
        or observed.identity != expected.identity
    ):
        _fail("DESTINATION_CHANGED", "sealed output binding or bytes changed")


def _copy_source_file(output: OutputFile, target: AnchoredTarget) -> str:
    if output.source is None:
        raise AssertionError("copy output has no source")
    source = output.source.seal
    source_descriptor = -1
    target_descriptor = -1
    method = ""
    try:
        before = os.lstat(source.path)
        if _identity(before) != source.identity:
            _fail("SOURCE_CHANGED", "copy source changed before opening")
        source_descriptor = os.open(
            source.path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        if _identity(os.fstat(source_descriptor)) != source.identity:
            _fail("SOURCE_CHANGED", "copy source changed while opening")
        target_descriptor = os.open(
            _safe_component(target.name),
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            PRIVATE_FILE_MODE,
            dir_fd=target.parent_fd,
        )
        os.fchmod(target_descriptor, PRIVATE_FILE_MODE)
        try:
            fcntl.ioctl(target_descriptor, FICLONE, source_descriptor)
            method = "reflink"
        except OSError as exc:
            if exc.errno not in {
                errno.EXDEV,
                errno.EOPNOTSUPP,
                errno.ENOTTY,
                errno.EINVAL,
                errno.ENOSYS,
            }:
                raise
            os.ftruncate(target_descriptor, 0)
            os.lseek(source_descriptor, 0, os.SEEK_SET)
            while True:
                block = os.read(source_descriptor, 1024 * 1024)
                if not block:
                    break
                _write_all(target_descriptor, block)
            method = "byte_copy"
        os.fsync(target_descriptor)
        target_metadata = os.fstat(target_descriptor)
        if (
            target_metadata.st_size != output.size_bytes
            or stat.S_IMODE(target_metadata.st_mode) != PRIVATE_FILE_MODE
        ):
            _fail("COPY_DRIFT", "copied file size or mode differs")
        if _identity(os.fstat(source_descriptor)) != source.identity:
            _fail("SOURCE_CHANGED", "copy source changed while copying")
        os.lseek(target_descriptor, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        hits: set[str] = set()
        tail = b""
        observed_bytes = 0
        while True:
            block = os.read(target_descriptor, 1024 * 1024)
            if not block:
                break
            observed_bytes += len(block)
            digest.update(block)
            window = tail + block
            for secret_name, pattern in SECRET_PATTERNS:
                if pattern.search(window):
                    hits.add(secret_name)
            tail = window[-512:]
        if hits:
            _fail(
                "SECRET_REFUSED",
                "materialized copied file matched forbidden credential policy",
            )
        target_path_metadata = os.stat(
            _safe_component(target.name),
            dir_fd=target.parent_fd,
            follow_symlinks=False,
        )
        if (
            _identity(target_path_metadata) != _identity(os.fstat(target_descriptor))
            or observed_bytes != output.size_bytes
            or digest.hexdigest() != output.sha256
        ):
            _fail("COPY_DRIFT", "copied file bytes or identity differ")
    except BaseException:
        _close_best_effort(target_descriptor)
        target_descriptor = -1
        _close_best_effort(source_descriptor)
        source_descriptor = -1
        raise
    finally:
        _close_best_effort(target_descriptor)
        _close_best_effort(source_descriptor)

    try:
        after = os.lstat(source.path)
    except OSError as exc:
        raise PackageProjectError(
            "SOURCE_CHANGED", "copy source disappeared after copying"
        ) from exc
    if _identity(after) != source.identity:
        _fail("SOURCE_CHANGED", "copy source changed after copying")
    return method


def _seal_file_at(
    parent_fd: int,
    name: str,
    path_hint: Path,
    *,
    label: str,
) -> FileSeal:
    descriptor = -1
    digest = hashlib.sha256()
    hits: set[str] = set()
    tail = b""
    observed_bytes = 0
    try:
        before = os.stat(
            _safe_component(name),
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size < 0
            or before.st_size > MAX_SOURCE_FILE_BYTES
        ):
            _fail("COPY_DRIFT", f"{label} is not an allowed regular file")
        descriptor = os.open(
            _safe_component(name),
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        if _identity(os.fstat(descriptor)) != _identity(before):
            _fail("COPY_DRIFT", f"{label} changed while opening")
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            observed_bytes += len(block)
            digest.update(block)
            window = tail + block
            for secret_name, pattern in SECRET_PATTERNS:
                if pattern.search(window):
                    hits.add(secret_name)
            tail = window[-512:]
        after_open = os.fstat(descriptor)
        after_entry = os.stat(
            _safe_component(name),
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except PackageProjectError:
        raise
    except OSError as exc:
        raise PackageProjectError(
            "COPY_DRIFT", f"could not read {label} through its anchor"
        ) from exc
    finally:
        _close_best_effort(descriptor)
    if (
        _identity(after_open) != _identity(before)
        or _identity(after_entry) != _identity(before)
        or observed_bytes != before.st_size
    ):
        _fail("COPY_DRIFT", f"{label} changed while reading")
    if hits:
        _fail("SECRET_REFUSED", f"{label} matched forbidden credential policy")
    return FileSeal(
        path=path_hint,
        sha256=digest.hexdigest(),
        size_bytes=observed_bytes,
        mode=stat.S_IMODE(before.st_mode),
        identity=_identity(before),
    )


def _snapshot_materialized_project_at(
    project_fd: int,
    project_root: Path,
) -> TreeSnapshot:
    directories: list[DirectorySeal] = []
    files: list[SourceFile] = []

    def visit(directory_fd: int, relative_path: str) -> None:
        metadata = os.fstat(directory_fd)
        if not stat.S_ISDIR(metadata.st_mode):
            _fail("COPY_DRIFT", "materialized directory anchor is invalid")
        directories.append(
            DirectorySeal(
                relative_path=relative_path,
                mode=stat.S_IMODE(metadata.st_mode),
                identity=_identity(metadata),
            )
        )
        try:
            names = sorted(os.listdir(directory_fd))
        except OSError as exc:
            raise PackageProjectError(
                "COPY_DRIFT", "could not enumerate anchored materialized project"
            ) from exc
        for name in names:
            try:
                entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError as exc:
                raise PackageProjectError(
                    "COPY_DRIFT", "could not inspect anchored materialized entry"
                ) from exc
            if stat.S_ISLNK(entry.st_mode):
                _fail("SYMLINK_REFUSED", "materialized project contains a symlink")
            relative = name if relative_path == "." else f"{relative_path}/{name}"
            if stat.S_ISDIR(entry.st_mode):
                child_fd = _open_directory_at(directory_fd, name)
                try:
                    visit(child_fd, relative)
                finally:
                    _close_best_effort(child_fd)
            elif stat.S_ISREG(entry.st_mode):
                files.append(
                    SourceFile(
                        relative_path=relative,
                        seal=_seal_file_at(
                            directory_fd,
                            name,
                            project_root / relative,
                            label=f"materialized file {relative}",
                        ),
                        copy_to_target=False,
                    )
                )
                if len(files) > MAX_SOURCE_FILES:
                    _fail("COPY_DRIFT", "materialized file count exceeds policy")
            else:
                _fail("COPY_DRIFT", "materialized project contains a special file")

    visit(project_fd, ".")
    directories = sorted(directories, key=lambda item: item.relative_path)
    files = sorted(files, key=lambda item: item.relative_path)
    return TreeSnapshot(
        root=project_root,
        directories=tuple(directories),
        files=tuple(files),
        tree_sha256=_entry_hash(directories, files),
        total_bytes=sum(item.seal.size_bytes for item in files),
    )


def _assert_materialized_project(
    observed: TreeSnapshot, expected: OutputSnapshot
) -> None:
    if (
        observed.tree_sha256 != expected.tree_sha256
        or observed.total_bytes != expected.total_bytes
        or len(observed.directories) != len(expected.directories)
        or len(observed.files) != len(expected.files)
    ):
        _fail("COPY_DRIFT", "materialized project tree differs from the plan")
    observed_directories = [
        (item.relative_path, item.mode) for item in observed.directories
    ]
    expected_directories = [
        (path, PRIVATE_DIRECTORY_MODE) for path in expected.directories
    ]
    observed_files = [
        (
            item.relative_path,
            item.seal.sha256,
            item.seal.size_bytes,
            item.seal.mode,
        )
        for item in observed.files
    ]
    expected_files = [
        (item.relative_path, item.sha256, item.size_bytes, item.mode)
        for item in expected.files
    ]
    if observed_directories != expected_directories or observed_files != expected_files:
        _fail("COPY_DRIFT", "materialized file or directory identity differs")


def _assert_source_stable(plan: MaterializationPlan) -> None:
    source = plan.source
    current_result = _seal_file(
        source.result_seal.path,
        label="source build result",
        capture=True,
        maximum_bytes=MAX_JSON_BYTES,
    )
    current_execution = _seal_file(
        source.execution_seal.path,
        label="source execution manifest",
        capture=True,
        maximum_bytes=MAX_JSON_BYTES,
    )
    current_scene = _seal_file(
        source.scene_seal.path,
        label="source scene receipt",
        capture=True,
        maximum_bytes=MAX_JSON_BYTES,
    )
    current_project = _snapshot_source_project(source.project_snapshot.root)
    if (
        not _same_file_observation(current_result, source.result_seal)
        or not _same_file_observation(current_execution, source.execution_seal)
        or not _same_file_observation(current_scene, source.scene_seal)
        or current_project != source.project_snapshot
    ):
        _fail("SOURCE_CHANGED", "accepted source changed after planning")


def _assert_run_uat_stable(plan: MaterializationPlan) -> None:
    current_run_uat = _seal_file(
        plan.run_uat.run_uat.path,
        label="pinned RunUAT",
        scan_secrets=True,
        maximum_bytes=MAX_JSON_BYTES,
    )
    current_build_version = _seal_file(
        plan.run_uat.build_version.path,
        label="pinned Unreal Build.version",
        capture=True,
        maximum_bytes=MAX_JSON_BYTES,
    )
    if not _same_file_observation(
        current_run_uat, plan.run_uat.run_uat
    ) or not _same_file_observation(current_build_version, plan.run_uat.build_version):
        _fail("RUN_UAT_CHANGED", "pinned RunUAT input changed after planning")


def _accepted_receipt(
    plan: MaterializationPlan,
    observed: TreeSnapshot,
    copy_methods: Mapping[str, int],
) -> dict[str, Any]:
    project_record = plan.output.receipt_record()
    if project_record["tree_sha256"] != observed.tree_sha256:
        raise AssertionError("accepted project record is not observed")
    receipt: dict[str, Any] = {
        "schema_version": RECEIPT_SCHEMA,
        "status": "accepted",
        "attempt_root": str(plan.attempt_root),
        "project_root": str(plan.attempt_root / "project"),
        "destination": plan.destination.receipt_record(),
        "plan_content_digest": plan.report["content_digest"],
        "source": _source_binding_record(plan.source),
        "project": project_record,
        "copy_methods": dict(sorted(copy_methods.items())),
        "policy": dict(plan.report["policy"]),
        "runuat": dict(plan.report["runuat"]),
        "output": str(plan.attempt_root / MATERIALIZATION_RECEIPT),
    }
    receipt["content_digest"] = _content_digest(receipt)
    raw = canonical_json(receipt)
    if _secret_hits((raw,)):
        _fail("SECRET_REFUSED", "materialization receipt matched secret policy")
    return receipt


def _failure_receipt(plan: MaterializationPlan, error: BaseException) -> dict[str, Any]:
    if isinstance(error, PackageProjectError):
        error_record = {
            "type": type(error).__name__,
            "code": error.code,
            "message": error.message,
        }
    else:
        error_record = {
            "type": type(error).__name__,
            "code": "MATERIALIZER_UNEXPECTED",
            "message": "materialization failed with an unexpected local error",
        }
    receipt: dict[str, Any] = {
        "schema_version": RECEIPT_SCHEMA,
        "status": "failed_quarantined",
        "attempt_root": str(plan.attempt_root),
        "plan_content_digest": plan.report["content_digest"],
        "destination": plan.destination.receipt_record(),
        "source": {
            "build_result_sha256": plan.source.result_seal.sha256,
            "project_tree_sha256": plan.source.project_snapshot.tree_sha256,
            "source_default_engine": _source_binding_record(plan.source)[
                "source_default_engine"
            ],
        },
        "error": error_record,
        "quarantine": {
            "attempt_retained": True,
            "source_modified": False,
            "cleanup_policy": "retain_partial_attempt_never_delete/v1",
        },
        "output": str(plan.attempt_root / MATERIALIZATION_RECEIPT),
    }
    receipt["content_digest"] = _content_digest(receipt)
    raw = canonical_json(receipt)
    if _secret_hits((raw,)):
        return {
            "schema_version": RECEIPT_SCHEMA,
            "status": "failed_quarantined",
            "attempt_root": str(plan.attempt_root),
            "plan_content_digest": plan.report["content_digest"],
            "destination": plan.destination.receipt_record(),
            "error": {
                "type": "PackageProjectError",
                "code": "SECRET_REFUSED",
                "message": "failure evidence matched secret policy",
            },
            "quarantine": {
                "attempt_retained": True,
                "source_modified": False,
                "cleanup_policy": "retain_partial_attempt_never_delete/v1",
            },
            "output": str(plan.attempt_root / MATERIALIZATION_RECEIPT),
        }
    return receipt


def _retain_failure_receipt_at(
    plan: MaterializationPlan,
    error: BaseException,
    attempt_fd: int,
) -> None:
    try:
        try:
            os.stat(
                MATERIALIZATION_RECEIPT,
                dir_fd=attempt_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            return
        failure = _failure_receipt(plan, error)
        if "content_digest" not in failure:
            failure["content_digest"] = _content_digest(failure)
        _write_exclusive_at(
            AnchoredTarget(
                parent_fd=attempt_fd,
                name=MATERIALIZATION_RECEIPT,
                display_path=plan.attempt_root / MATERIALIZATION_RECEIPT,
            ),
            canonical_json(failure),
        )
    except BaseException:
        # The append-only attempt itself remains quarantine evidence.  Never
        # delete it or mutate the accepted source when receipt sealing fails.
        pass


def apply_materialization(
    plan: MaterializationPlan,
) -> tuple[dict[str, Any], str]:
    if plan.report.get("mode") != "apply":
        _fail("APPLY_PLAN_REQUIRED", "apply requires an apply-mode plan")
    anchor = _open_destination_anchor(plan)
    created_attempt = False
    attempt_fd = -1
    project_fd = -1
    directory_fds: dict[str, int] = {}
    terminal_candidate: tuple[dict[str, Any], str, str, FileSeal] | None = None
    try:
        _assert_source_stable(plan)
        _assert_run_uat_stable(plan)
        _assert_destination_anchor_stable(anchor)
        attempt_fd = _create_attempt_at(anchor)
        created_attempt = True
        _assert_attempt_anchor_stable(anchor, attempt_fd)
        _assert_destination_anchor_stable(anchor)

        project_root = plan.attempt_root / "project"
        project_fd = _mkdir_private_at(attempt_fd, "project")
        directory_fds["."] = project_fd
        for relative in sorted(
            (path for path in plan.output.directories if path != "."),
            key=lambda value: (len(PurePosixPath(value).parts), value),
        ):
            relative_path = PurePosixPath(relative)
            parent_fd = directory_fds[relative_path.parent.as_posix()]
            directory_fds[relative] = _mkdir_private_at(
                parent_fd,
                relative_path.name,
            )
        _assert_project_directory_bindings_stable(attempt_fd, directory_fds)

        copy_methods: Counter[str] = Counter()
        for output in plan.output.files:
            relative_path = PurePosixPath(output.relative_path)
            parent_fd = directory_fds[relative_path.parent.as_posix()]
            target = AnchoredTarget(
                parent_fd=parent_fd,
                name=relative_path.name,
                display_path=project_root / output.relative_path,
            )
            if output.raw is not None:
                _write_exclusive_at(target, output.raw)
            elif output.source is not None:
                copy_methods[_copy_source_file(output, target)] += 1
            else:  # pragma: no cover - OutputFile invariant.
                raise AssertionError("output has neither source nor generated bytes")

        _assert_project_directory_bindings_stable(attempt_fd, directory_fds)
        _assert_source_stable(plan)
        _assert_run_uat_stable(plan)
        _assert_destination_anchor_stable(anchor)
        _assert_attempt_anchor_stable(anchor, attempt_fd)
        _assert_project_directory_bindings_stable(attempt_fd, directory_fds)
        observed = _snapshot_materialized_project_at(project_fd, project_root)
        _assert_materialized_project(observed, plan.output)
        _assert_project_directory_bindings_stable(attempt_fd, directory_fds)
        _assert_source_stable(plan)
        _assert_run_uat_stable(plan)
        _assert_destination_anchor_stable(anchor)
        _assert_attempt_anchor_stable(anchor, attempt_fd)
        _assert_project_directory_bindings_stable(attempt_fd, directory_fds)
        receipt = _accepted_receipt(plan, observed, copy_methods)
        raw = canonical_json(receipt)
        receipt_sha256 = sha256_bytes(raw)
        pending_receipt_name = (
            f".{MATERIALIZATION_RECEIPT}.pending-{os.urandom(16).hex()}"
        )
        pending_receipt_seal = _write_exclusive_at(
            AnchoredTarget(
                parent_fd=attempt_fd,
                name=pending_receipt_name,
                display_path=plan.attempt_root / pending_receipt_name,
            ),
            raw,
        )

        # Everything fallible happens before terminal receipt publication.
        # A raised apply therefore cannot leave this tool's accepted receipt
        # at the canonical terminal name.
        _assert_destination_anchor_stable(anchor)
        _assert_attempt_anchor_stable(anchor, attempt_fd)
        _assert_project_directory_bindings_stable(attempt_fd, directory_fds)
        final_observed = _snapshot_materialized_project_at(project_fd, project_root)
        _assert_materialized_project(final_observed, plan.output)
        _assert_source_stable(plan)
        _assert_run_uat_stable(plan)
        _assert_destination_anchor_stable(anchor)
        _assert_attempt_anchor_stable(anchor, attempt_fd)
        _assert_project_directory_bindings_stable(attempt_fd, directory_fds)
        _assert_file_binding_stable(
            attempt_fd,
            pending_receipt_name,
            pending_receipt_seal,
        )
        terminal_candidate = (
            receipt,
            receipt_sha256,
            pending_receipt_name,
            pending_receipt_seal,
        )
        _link_noreplace_at(
            attempt_fd,
            pending_receipt_name,
            MATERIALIZATION_RECEIPT,
            pending_receipt_seal,
        )
        # Successful hard-link publication is the accepted terminal commit.  Do not
        # add a fallible post-commit step that could contradict that state.
        return receipt, receipt_sha256
    except BaseException as exc:
        if terminal_candidate is not None and attempt_fd >= 0:
            (
                committed_receipt,
                committed_sha256,
                committed_pending_name,
                committed_pending_seal,
            ) = terminal_candidate
            terminal_state = _terminal_receipt_state_at(
                attempt_fd,
                committed_pending_name,
                MATERIALIZATION_RECEIPT,
                committed_pending_seal,
            )
            if terminal_state == "match":
                return committed_receipt, committed_sha256
            if terminal_state in {"missing", "unknown"} or (
                isinstance(exc, PackageProjectError)
                and exc.code == "RECEIPT_COMMIT_OUTCOME_UNKNOWN"
            ):
                raise PackageProjectError(
                    "RECEIPT_COMMIT_OUTCOME_UNKNOWN",
                    "terminal receipt state is unknown after interruption",
                ) from exc
        if created_attempt:
            if attempt_fd < 0:
                try:
                    attempt_fd = _open_directory_at(
                        anchor.parent_fd,
                        anchor.attempt_name,
                    )
                except BaseException:
                    attempt_fd = -1
            if attempt_fd >= 0:
                _retain_failure_receipt_at(plan, exc, attempt_fd)
        raise
    finally:
        for relative, descriptor in directory_fds.items():
            if relative != ".":
                _close_best_effort(descriptor)
        _close_best_effort(project_fd)
        _close_best_effort(attempt_fd)
        _close_best_effort(anchor.parent_fd)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-build-result", required=True, type=Path)
    parser.add_argument("--source-build-result-sha256", required=True)
    parser.add_argument("--source-project", required=True, type=Path)
    parser.add_argument("--source-project-tree-sha256", required=True)
    parser.add_argument("--run-uat", required=True, type=Path)
    parser.add_argument("--run-uat-sha256", required=True)
    parser.add_argument("--attempt-root", required=True, type=Path)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="materialize one fresh package attempt (default: zero-write dry run)",
    )
    return parser


def _config_from_args(args: argparse.Namespace) -> MaterializationConfig:
    return MaterializationConfig(
        source_build_result=args.source_build_result,
        source_build_result_sha256=args.source_build_result_sha256,
        source_project=args.source_project,
        source_project_tree_sha256=args.source_project_tree_sha256,
        run_uat=args.run_uat,
        run_uat_sha256=args.run_uat_sha256,
        attempt_root=args.attempt_root,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    plan = plan_materialization(_config_from_args(args), apply=args.apply)
    if not args.apply:
        print(canonical_json(plan.report).decode("utf-8"))
        return 0
    receipt, receipt_sha = apply_materialization(plan)
    print(
        canonical_json(
            {
                "status": receipt["status"],
                "attempt_root": receipt["attempt_root"],
                "receipt": receipt["output"],
                "receipt_sha256": receipt_sha,
                "project_tree_sha256": receipt["project"]["tree_sha256"],
            }
        ).decode("utf-8")
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PackageProjectError as error:
        print(f"package project materialization refused: {error}", file=sys.stderr)
        raise SystemExit(2)
