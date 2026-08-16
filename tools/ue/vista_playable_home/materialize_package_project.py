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
    attempt_root: Path


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
    source: SourceEvidence
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
            "sanitized_policy": SOURCE_SANITIZATION_POLICY,
            "sha256": engine.sha256,
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


def _runuat_contract(attempt: Path) -> dict[str, Any]:
    project = attempt / "project" / EXPECTED_PROJECT_NAME
    return {
        "argv": [
            "<absolute-Engine/Build/BatchFiles/RunUAT.sh>",
            "BuildCookRun",
            f"-project={project}",
            "-noP4",
            "-platform=Linux",
            "-clientconfig=Development",
            "-build",
            "-cook",
            "-stage",
            "-pak",
            "-skipiostore",
            "-archive",
            f"-archivedirectory={attempt / 'archive'}",
            f"-map={EXPECTED_MAP_PATH}",
            "-utf8output",
        ],
        "log": str(attempt / "runuat.log"),
        "policy": "operator-runs-pinned-runuat-after-materialization/v1",
    }


def plan_materialization(
    config: MaterializationConfig, *, apply: bool = False
) -> MaterializationPlan:
    source = _validate_source_evidence(config)
    attempt = _validate_destination(config, source)
    output = _build_output_snapshot(source)
    report: dict[str, Any] = {
        "schema_version": PLAN_SCHEMA,
        "status": "ready",
        "mode": "apply" if apply else "dry_run",
        "attempt_root": str(attempt),
        "project_root": str(attempt / "project"),
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
            "default_engine": "canonical_allowlisted_regeneration/v1",
            "default_input": "preserve_verified_bytes/v1",
            "copy_transport": "reflink_with_byte_fallback/v1",
            "secret_scan": "final_copy_eligible_and_output_zero_hits/v1",
            "failed_attempt_retention": "failed_quarantined_never_deleted/v1",
            "source_mutation": "pre_copy_post_copy_final_fail_closed/v1",
        },
        "runuat": _runuat_contract(attempt),
        "output": str(attempt / MATERIALIZATION_RECEIPT),
    }
    report["content_digest"] = _content_digest(report)
    if _secret_hits((canonical_json(report),)):
        _fail("SECRET_REFUSED", "materialization plan matched secret policy")
    return MaterializationPlan(
        config=config,
        attempt_root=attempt,
        source=source,
        output=output,
        report=report,
    )


def _mkdir_private(path: Path) -> None:
    try:
        path.mkdir(mode=PRIVATE_DIRECTORY_MODE, exist_ok=False)
        os.chmod(path, PRIVATE_DIRECTORY_MODE, follow_symlinks=False)
    except OSError as exc:
        raise PackageProjectError(
            "MATERIALIZATION_WRITE_FAILED", "could not create private directory"
        ) from exc


def _close_best_effort(descriptor: int) -> None:
    if descriptor < 0:
        return
    try:
        os.close(descriptor)
    except OSError:
        pass


def _unlink_best_effort(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _write_all(descriptor: int, raw: bytes) -> None:
    view = memoryview(raw)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError(errno.EIO, "short write")
        view = view[written:]


def _write_exclusive(path: Path, raw: bytes) -> None:
    descriptor = -1
    committed = False
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            PRIVATE_FILE_MODE,
        )
        os.fchmod(descriptor, PRIVATE_FILE_MODE)
        _write_all(descriptor, raw)
        os.fsync(descriptor)
        committed = True
    except BaseException:
        _close_best_effort(descriptor)
        descriptor = -1
        _unlink_best_effort(path)
        raise
    finally:
        _close_best_effort(descriptor)
    if not committed:  # pragma: no cover - defensive state assertion.
        raise AssertionError("exclusive write did not commit")


def _copy_source_file(output: OutputFile, destination: Path) -> str:
    if output.source is None:
        raise AssertionError("copy output has no source")
    source = output.source.seal
    source_descriptor = -1
    target_descriptor = -1
    target_committed = False
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
            destination,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            PRIVATE_FILE_MODE,
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
        target_committed = True
    except BaseException:
        _close_best_effort(target_descriptor)
        target_descriptor = -1
        _close_best_effort(source_descriptor)
        source_descriptor = -1
        if not target_committed:
            _unlink_best_effort(destination)
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
    observed = _seal_file(
        destination,
        label="materialized copied file",
        scan_secrets=True,
    )
    if (
        observed.sha256 != output.sha256
        or observed.size_bytes != output.size_bytes
        or observed.mode != output.mode
    ):
        _fail("COPY_DRIFT", "copied file bytes or mode differ")
    return method


def _snapshot_materialized_project(project_root: Path) -> TreeSnapshot:
    root = _existing_path(project_root, label="materialized project", directory=True)
    directories, files = _walk_source_tree(
        root,
        project_root=root,
        copy_to_target=False,
        scan_secrets=True,
        excluded_names=frozenset(),
    )
    directories = sorted(directories, key=lambda item: item.relative_path)
    files = sorted(files, key=lambda item: item.relative_path)
    return TreeSnapshot(
        root=root,
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
        "source": {
            "build_result_sha256": plan.source.result_seal.sha256,
            "project_tree_sha256": plan.source.project_snapshot.tree_sha256,
            "source_default_engine": {
                "bytes": _source_file(
                    plan.source.project_snapshot, "Config/DefaultEngine.ini"
                ).seal.size_bytes,
                "sanitized_policy": SOURCE_SANITIZATION_POLICY,
                "sha256": _source_file(
                    plan.source.project_snapshot, "Config/DefaultEngine.ini"
                ).seal.sha256,
            },
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


def _retain_failure_receipt(plan: MaterializationPlan, error: BaseException) -> None:
    path = plan.attempt_root / MATERIALIZATION_RECEIPT
    if path.exists() or path.is_symlink():
        return
    try:
        failure = _failure_receipt(plan, error)
        if "content_digest" not in failure:
            failure["content_digest"] = _content_digest(failure)
        _write_exclusive(path, canonical_json(failure))
    except BaseException:
        # The append-only attempt itself remains quarantine evidence.  Never
        # delete it or mutate the accepted source when receipt sealing fails.
        pass


def apply_materialization(
    plan: MaterializationPlan,
) -> tuple[dict[str, Any], str]:
    if plan.report.get("mode") != "apply":
        _fail("APPLY_PLAN_REQUIRED", "apply requires an apply-mode plan")
    if _validate_destination(plan.config, plan.source) != plan.attempt_root:
        raise AssertionError("apply destination differs from the validated plan")
    _assert_source_stable(plan)
    if _validate_destination(plan.config, plan.source) != plan.attempt_root:
        raise AssertionError("apply destination changed after source validation")

    created_attempt = False
    try:
        plan.attempt_root.mkdir(mode=PRIVATE_DIRECTORY_MODE, exist_ok=False)
        created_attempt = True
        os.chmod(
            plan.attempt_root,
            PRIVATE_DIRECTORY_MODE,
            follow_symlinks=False,
        )
        project_root = plan.attempt_root / "project"
        for relative in sorted(
            plan.output.directories,
            key=lambda value: (len(PurePosixPath(value).parts), value),
        ):
            destination = project_root if relative == "." else project_root / relative
            _mkdir_private(destination)

        copy_methods: Counter[str] = Counter()
        for output in plan.output.files:
            destination = project_root / output.relative_path
            if output.raw is not None:
                _write_exclusive(destination, output.raw)
            elif output.source is not None:
                copy_methods[_copy_source_file(output, destination)] += 1
            else:  # pragma: no cover - OutputFile invariant.
                raise AssertionError("output has neither source nor generated bytes")

        _assert_source_stable(plan)
        observed = _snapshot_materialized_project(project_root)
        _assert_materialized_project(observed, plan.output)
        _assert_source_stable(plan)
        receipt = _accepted_receipt(plan, observed, copy_methods)
        receipt_path = plan.attempt_root / MATERIALIZATION_RECEIPT
        raw = canonical_json(receipt)
        _write_exclusive(receipt_path, raw)
        return receipt, sha256_bytes(raw)
    except BaseException as exc:
        if created_attempt:
            _retain_failure_receipt(plan, exc)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-build-result", required=True, type=Path)
    parser.add_argument("--source-build-result-sha256", required=True)
    parser.add_argument("--source-project", required=True, type=Path)
    parser.add_argument("--source-project-tree-sha256", required=True)
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
