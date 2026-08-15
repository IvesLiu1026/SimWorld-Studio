#!/usr/bin/env python3
"""Fail-closed primitives for the VISTA Playable Home game-only lane."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import socket
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA = "simworld.vista.playable-home-runtime/v1"
PREFLIGHT_SCHEMA = "simworld.vista.playable-home-preflight/v1"
DEFAULT_DISPLAY = ":117"
DEFAULT_GPU = 0
RESERVED_GPU_INDICES = frozenset({1})
RESERVED_PORTS = frozenset(
    {3012, 3022, 55570, 55582, 8595, 8596, 8615, 8616, 8899, 8919, 8400}
)
MAP_RE = re.compile(r"^/Game/[A-Za-z0-9_./-]+$")
DISPLAY_RE = re.compile(r"^:([0-9]{1,4})$")


class RuntimeSafetyError(RuntimeError):
    """Raised before a request can affect an unowned runtime."""


@dataclass(frozen=True)
class GameRuntimeConfig:
    workspace: Path
    project: Path
    ue_editor: Path
    map_path: str
    display: str = DEFAULT_DISPLAY
    gpu: int = DEFAULT_GPU
    width: int = 1280
    height: int = 720
    fps: int = 60
    title: str = "VISTA World"
    nvidia_icd: Path | None = None
    nvidia_compat: Path | None = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _absolute(path: Path, label: str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise RuntimeSafetyError(f"{label} must be an absolute path")
    return candidate


def _existing(path: Path, label: str, *, directory: bool = False) -> Path:
    candidate = _absolute(path, label)
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise RuntimeSafetyError(f"{label} does not exist: {candidate}") from exc
    if directory and not resolved.is_dir():
        raise RuntimeSafetyError(f"{label} must be a directory: {resolved}")
    if not directory and not resolved.is_file():
        raise RuntimeSafetyError(f"{label} must be a regular file: {resolved}")
    return resolved


def validate_map(value: str) -> str:
    map_path = str(value or "").strip()
    if not MAP_RE.fullmatch(map_path) or ".." in map_path.split("/"):
        raise RuntimeSafetyError("map must be a safe exact /Game/... package path")
    return map_path


def validate_display(value: str) -> str:
    display = str(value or "").strip()
    match = DISPLAY_RE.fullmatch(display)
    if not match or int(match.group(1)) > 4095:
        raise RuntimeSafetyError("display must be an X11 display such as :117")
    return display


def validate_gpu(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeSafetyError("GPU index must be a non-negative integer")
    if value in RESERVED_GPU_INDICES:
        raise RuntimeSafetyError(
            f"GPU {value} is reserved by accepted live runtimes; use an owned GPU"
        )
    return value


def validate_dimensions(width: int, height: int, fps: int) -> tuple[int, int, int]:
    if not 640 <= width <= 3840 or not 480 <= height <= 2160:
        raise RuntimeSafetyError("render size must be between 640x480 and 3840x2160")
    if not 15 <= fps <= 120:
        raise RuntimeSafetyError("fps must be from 15 through 120")
    return width, height, fps


def _ensure_contained(candidate: Path, root: Path, label: str) -> None:
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise RuntimeSafetyError(f"{label} must be contained by {root}") from exc


def validate_config(config: GameRuntimeConfig, *, create_workspace: bool) -> GameRuntimeConfig:
    workspace_lexical = _absolute(config.workspace, "workspace")
    if create_workspace:
        workspace_lexical.mkdir(parents=True, mode=0o700, exist_ok=True)
    workspace = _existing(workspace_lexical, "workspace", directory=True)
    if workspace.is_symlink() or not os.access(workspace, os.R_OK | os.W_OK | os.X_OK):
        raise RuntimeSafetyError("workspace must be a user-accessible real directory")

    project = _existing(config.project, "UE project")
    if project.suffix != ".uproject":
        raise RuntimeSafetyError("UE project must end in .uproject")
    _ensure_contained(project, workspace, "UE project")
    try:
        descriptor = json.loads(project.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeSafetyError("UE project descriptor is invalid JSON") from exc
    if not isinstance(descriptor, dict):
        raise RuntimeSafetyError("UE project descriptor must be a JSON object")

    ue_editor = _existing(config.ue_editor, "Unreal Editor")
    if not os.access(ue_editor, os.X_OK):
        raise RuntimeSafetyError("Unreal Editor is not executable")
    if tuple(part.name for part in (ue_editor.parent, ue_editor.parent.parent)) != (
        "Linux",
        "Binaries",
    ) or ue_editor.name != "UnrealEditor":
        raise RuntimeSafetyError(
            "Unreal Editor must be an exact Engine/Binaries/Linux/UnrealEditor"
        )

    nvidia_icd = None
    if config.nvidia_icd is not None:
        nvidia_icd = _existing(config.nvidia_icd, "NVIDIA ICD")
    nvidia_compat = None
    if config.nvidia_compat is not None:
        nvidia_compat = _existing(
            config.nvidia_compat, "NVIDIA compatibility directory", directory=True
        )

    width, height, fps = validate_dimensions(config.width, config.height, config.fps)
    return GameRuntimeConfig(
        workspace=workspace,
        project=project,
        ue_editor=ue_editor,
        map_path=validate_map(config.map_path),
        display=validate_display(config.display),
        gpu=validate_gpu(config.gpu),
        width=width,
        height=height,
        fps=fps,
        title=str(config.title or "VISTA World")[:80],
        nvidia_icd=nvidia_icd,
        nvidia_compat=nvidia_compat,
    )


def build_game_command(config: GameRuntimeConfig) -> list[str]:
    """Build a fixed game-only command; notably, it never uses RenderOffScreen."""

    return [
        str(config.ue_editor),
        str(config.project),
        config.map_path,
        "-game",
        "-Windowed",
        "-ForceRes",
        f"-ResX={config.width}",
        f"-ResY={config.height}",
        f"-graphicsadapter={config.gpu}",
        "-NOSPLASH",
        "-NOSOUND",
        "-NoAnalytics",
        "-UDPMESSAGING_TRANSPORT_ENABLE=0",
        "-ini:Engine:[/Script/TcpMessaging.TcpMessagingSettings]:EnableTransport=False",
        f"-ExecCmds=t.MaxFPS {config.fps}",
        "-SaveToUserDir",
        f"-UserDir={config.workspace / 'ue-user'}",
        f"-LocalDataCachePath={config.workspace / 'xdg-cache' / 'UnrealEngine' / 'DDC'}",
        "-log",
    ]


def sanitized_environment(config: GameRuntimeConfig) -> dict[str, str]:
    allowed = {
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "PULSE_SERVER",
        "XDG_RUNTIME_DIR",
        "XDG_DATA_DIRS",
    }
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    environment["DISPLAY"] = config.display
    environment["SDL_VIDEODRIVER"] = "x11"
    environment["VK_ICD_FILENAMES"] = (
        str(config.nvidia_icd) if config.nvidia_icd else os.environ.get("VK_ICD_FILENAMES", "")
    )
    environment["VISTA_RUNTIME_GPU"] = str(config.gpu)
    if config.nvidia_compat:
        existing = os.environ.get("LD_LIBRARY_PATH", "")
        environment["LD_LIBRARY_PATH"] = str(config.nvidia_compat) + (
            f":{existing}" if existing else ""
        )
    environment.pop("STUDIO_ACCESS_TOKEN", None)
    environment.pop("ANTHROPIC_API_KEY", None)
    environment.pop("OPENAI_API_KEY", None)
    return environment


def process_start_ticks(pid: int) -> int | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
    except (FileNotFoundError, PermissionError, OSError):
        return None
    return int(fields[21]) if len(fields) > 21 else None


def process_identity(pid: int, role: str) -> dict[str, Any]:
    ticks = process_start_ticks(pid)
    if ticks is None:
        raise RuntimeSafetyError(f"could not bind process identity for {role}")
    return {
        "role": role,
        "pid": pid,
        "start_ticks": ticks,
        "process_group": os.getpgid(pid),
    }


def identity_is_live(identity: Mapping[str, Any]) -> bool:
    try:
        return process_start_ticks(int(identity["pid"])) == int(identity["start_ticks"])
    except (KeyError, TypeError, ValueError):
        return False


def port_is_available(port: int, host: str = "127.0.0.1") -> bool:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def inspect_toolchain(ue_editor: Path) -> dict[str, Any]:
    editor = Path(ue_editor).resolve(strict=False)
    engine = editor.parents[2] if len(editor.parents) >= 3 else Path("/")
    root = engine.parent
    fixed_candidates = {
        "run_uat": root / "Engine" / "Build" / "BatchFiles" / "RunUAT.sh",
        "build_sh": root / "Engine" / "Build" / "BatchFiles" / "Linux" / "Build.sh",
        "engine_source": root / "Engine" / "Source",
    }
    alternatives = {
        "unreal_build_tool": (
            root / "Engine" / "Binaries" / "DotNET" / "UnrealBuildTool" / "UnrealBuildTool",
            root / "Engine" / "Binaries" / "DotNET" / "UnrealBuildTool",
        ),
        # Installed/source engines do not always retain a standalone UHT ELF.
        # RunUAT can build UHT from this program source before compiling a
        # plugin, so either representation is a real build capability.
        "unreal_header_tool": (
            root / "Engine" / "Binaries" / "Linux" / "UnrealHeaderTool",
            root / "Engine" / "Programs" / "UnrealHeaderTool",
            root / "Engine" / "Source" / "Programs" / "UnrealHeaderTool",
        ),
    }
    selected = dict(fixed_candidates)
    for name, choices in alternatives.items():
        selected[name] = next((choice for choice in choices if choice.exists()), choices[0])
    present = {name: path.exists() for name, path in selected.items()}
    return {
        "engine_root": str(root),
        "paths": {name: str(path) for name, path in selected.items()},
        "present": present,
        "cook_ready": all(present.values()),
    }


def command_result(command: Sequence[str], timeout: float = 5.0) -> dict[str, Any]:
    executable = shutil.which(command[0]) if command else None
    if not executable:
        return {"available": False, "command": list(command), "returncode": None}
    try:
        result = subprocess.run(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
            env={key: value for key, value in os.environ.items() if key not in {"STUDIO_ACCESS_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"}},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "available": True,
            "command": list(command),
            "returncode": None,
            "error": type(exc).__name__,
        }
    output = result.stdout[-16000:]
    return {
        "available": True,
        "command": list(command),
        "returncode": result.returncode,
        "output": output,
    }


def redacted_plan(config: GameRuntimeConfig) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "created_at": utc_now(),
        "mode": "unreal-editor-game-preview",
        "config": {
            **asdict(config),
            "workspace": str(config.workspace),
            "project": str(config.project),
            "ue_editor": str(config.ue_editor),
            "nvidia_icd": str(config.nvidia_icd) if config.nvidia_icd else None,
            "nvidia_compat": str(config.nvidia_compat) if config.nvidia_compat else None,
        },
        "command": build_game_command(config),
        "command_shell_preview": shlex.join(build_game_command(config)),
        "security": {
            "editor_chrome": False,
            "render_offscreen": False,
            "reserved_gpu_indices": sorted(RESERVED_GPU_INDICES),
            "reserved_ports": sorted(RESERVED_PORTS),
            "arbitrary_command": False,
        },
    }
