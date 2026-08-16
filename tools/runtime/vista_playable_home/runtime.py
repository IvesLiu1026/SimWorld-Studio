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
R2_SCHEMA = "simworld.vista.playable-home-runtime/v2"
PREFLIGHT_SCHEMA = "simworld.vista.playable-home-preflight/v1"
RUNTIME_POINTER_SCHEMA = "simworld.vista.playable-home-runtime-pointer/v1"
DEFAULT_DISPLAY = ":117"
DEFAULT_GPU = 0
DEFAULT_VISTA_WORLD_PORT = 55620
DEFAULT_WORLD_REVISION = "vista_playable_home_r1"
R2_RUNTIME_PROFILE = "realistic_interior_r2"
R2_CAMERA_PROFILE = "realistic_interior_r2"
R2_DISPLAY = ":119"
R2_GPU = 0
R2_VISTA_WORLD_PORT = 55630
R2_WIDTH = 1920
R2_HEIGHT = 1080
R2_FPS = 60
TYPED_RESPONSE_MAX_BYTES = 64 * 1024
RESERVED_GPU_INDICES = frozenset({1})
RESERVED_PORTS = frozenset(
    {3012, 3022, 55570, 55582, 8595, 8596, 8615, 8616, 8899, 8919, 8400}
)
MAP_RE = re.compile(r"^/Game/[A-Za-z0-9_./-]+$")
DISPLAY_RE = re.compile(r"^:([0-9]{1,4})$")
ATTEMPT_RE = re.compile(r"^attempt-[0-9]{8}T[0-9]{6}\.[0-9]{6}Z-[0-9]+$")


class RuntimeSafetyError(RuntimeError):
    """Raised before a request can affect an unowned runtime."""


@dataclass(frozen=True)
class RuntimeProfileSpec:
    runtime_profile: str | None
    camera_profile: str | None
    display: str
    gpu: int
    vista_world_port: int
    width: int
    height: int
    fps: int


LEGACY_RUNTIME_SPEC = RuntimeProfileSpec(
    runtime_profile=None,
    camera_profile=None,
    display=DEFAULT_DISPLAY,
    gpu=DEFAULT_GPU,
    vista_world_port=DEFAULT_VISTA_WORLD_PORT,
    width=1280,
    height=720,
    fps=60,
)
R2_RUNTIME_SPEC = RuntimeProfileSpec(
    runtime_profile=R2_RUNTIME_PROFILE,
    camera_profile=R2_CAMERA_PROFILE,
    display=R2_DISPLAY,
    gpu=R2_GPU,
    vista_world_port=R2_VISTA_WORLD_PORT,
    width=R2_WIDTH,
    height=R2_HEIGHT,
    fps=R2_FPS,
)


@dataclass(frozen=True)
class GameRuntimeConfig:
    workspace: Path
    project: Path
    ue_editor: Path
    map_path: str
    display: str = DEFAULT_DISPLAY
    gpu: int = DEFAULT_GPU
    vista_world_port: int = DEFAULT_VISTA_WORLD_PORT
    width: int = 1280
    height: int = 720
    fps: int = 60
    title: str = "VISTA World"
    nvidia_icd: Path | None = None
    nvidia_compat: Path | None = None
    runtime_profile: str | None = None


def resolve_runtime_profile(value: str | None) -> RuntimeProfileSpec:
    if value is None:
        return LEGACY_RUNTIME_SPEC
    if value == R2_RUNTIME_PROFILE:
        return R2_RUNTIME_SPEC
    raise RuntimeSafetyError("runtime profile is not one of the closed profiles")


def validate_runtime_profile_binding(config: GameRuntimeConfig) -> RuntimeProfileSpec:
    spec = resolve_runtime_profile(config.runtime_profile)
    if spec.runtime_profile is not None and (
        config.display,
        config.gpu,
        config.vista_world_port,
        config.width,
        config.height,
        config.fps,
    ) != (
        spec.display,
        spec.gpu,
        spec.vista_world_port,
        spec.width,
        spec.height,
        spec.fps,
    ):
        raise RuntimeSafetyError(
            "realistic_interior_r2 runtime must use :119, GPU 0, port 55630, "
            "1920x1080, and 60 fps"
        )
    return spec


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


def runtime_root(workspace: Path) -> Path:
    root = _existing(workspace, "workspace", directory=True) / "game-runtime"
    root.mkdir(mode=0o700, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise RuntimeSafetyError("runtime root must be a real directory")
    return root


def resolve_current_runtime_state(workspace: Path) -> tuple[Path, dict[str, Any]]:
    root = runtime_root(workspace)
    pointer_path = root / "current.json"
    if pointer_path.is_symlink() or not pointer_path.is_file():
        raise RuntimeSafetyError("runtime pointer is missing or unsafe")
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeSafetyError("runtime pointer is invalid") from exc
    if not isinstance(pointer, dict) or set(pointer) != {"schema", "state"} \
            or pointer.get("schema") != RUNTIME_POINTER_SCHEMA:
        raise RuntimeSafetyError("runtime pointer has an invalid shape")
    relative = Path(str(pointer.get("state", "")))
    if relative.is_absolute() or len(relative.parts) != 2 \
            or not ATTEMPT_RE.fullmatch(relative.parts[0]) \
            or relative.parts[1] != "runtime-state.json":
        raise RuntimeSafetyError("runtime pointer target is invalid")
    candidate = root / relative
    if candidate.is_symlink() or candidate.parent.is_symlink() or not candidate.is_file():
        raise RuntimeSafetyError("runtime state is missing or unsafe")
    state_path = candidate.resolve(strict=True)
    _ensure_contained(state_path, root.resolve(strict=True), "runtime state")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeSafetyError("runtime state is invalid") from exc
    if not isinstance(state, dict):
        raise RuntimeSafetyError("runtime state must be an object")
    return state_path, state


def allocate_runtime_attempt(workspace: Path) -> Path:
    root = runtime_root(workspace)
    pointer = root / "current.json"
    if pointer.exists():
        _state_path, state = resolve_current_runtime_state(workspace)
        identity = state.get("process")
        if isinstance(identity, Mapping) and identity_is_live(identity):
            raise RuntimeSafetyError("a VISTA World runtime is already live")
    attempt = (
        "attempt-"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        + f"-{os.getpid()}"
    )
    if not ATTEMPT_RE.fullmatch(attempt):
        raise RuntimeSafetyError("runtime attempt identity is invalid")
    attempt_root = root / attempt
    attempt_root.mkdir(mode=0o700, exist_ok=False)
    return attempt_root


def publish_current_runtime(workspace: Path, state_path: Path) -> Path:
    root = runtime_root(workspace).resolve(strict=True)
    state = state_path.resolve(strict=True)
    _ensure_contained(state, root, "runtime state")
    relative = state.relative_to(root)
    if len(relative.parts) != 2 or not ATTEMPT_RE.fullmatch(relative.parts[0]) \
            or relative.parts[1] != "runtime-state.json":
        raise RuntimeSafetyError("runtime state location is invalid")
    pointer = root / "current.json"
    atomic_write_json(pointer, {
        "schema": RUNTIME_POINTER_SCHEMA,
        "state": relative.as_posix(),
    })
    return pointer


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


def validate_vista_world_port(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1024 <= value <= 65535:
        raise RuntimeSafetyError("VISTA World port must be an integer from 1024 through 65535")
    if value in RESERVED_PORTS:
        raise RuntimeSafetyError(f"port {value} is reserved by an existing runtime")
    if not port_is_available(value):
        raise RuntimeSafetyError(f"port {value} is already in use")
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
    if workspace_lexical.is_symlink():
        raise RuntimeSafetyError("workspace must not be a symlink")
    if create_workspace:
        workspace_lexical.mkdir(parents=True, mode=0o700, exist_ok=True)
    workspace = _existing(workspace_lexical, "workspace", directory=True)
    if not os.access(workspace, os.R_OK | os.W_OK | os.X_OK):
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
    validated = GameRuntimeConfig(
        workspace=workspace,
        project=project,
        ue_editor=ue_editor,
        map_path=validate_map(config.map_path),
        display=validate_display(config.display),
        gpu=validate_gpu(config.gpu),
        vista_world_port=validate_vista_world_port(config.vista_world_port),
        width=width,
        height=height,
        fps=fps,
        title=str(config.title or "VISTA World")[:80],
        nvidia_icd=nvidia_icd,
        nvidia_compat=nvidia_compat,
        runtime_profile=config.runtime_profile,
    )
    validate_runtime_profile_binding(validated)
    return validated


def build_game_command(config: GameRuntimeConfig) -> list[str]:
    """Build a fixed game-only command; notably, it never uses RenderOffScreen."""

    spec = validate_runtime_profile_binding(config)
    user_root = (
        config.workspace / "runtime-user"
        if spec.runtime_profile is not None
        else config.workspace
    )
    command = [
        str(config.ue_editor),
        str(config.project),
        config.map_path,
        "-game",
        "-Windowed",
        "-ForceRes",
        f"-ResX={config.width}",
        f"-ResY={config.height}",
        f"-graphicsadapter={config.gpu}",
        f"-VistaWorldPort={config.vista_world_port}",
        "-NOSPLASH",
        "-NOSOUND",
        "-NoAnalytics",
        "-UDPMESSAGING_TRANSPORT_ENABLE=0",
        "-ini:Engine:[/Script/TcpMessaging.TcpMessagingSettings]:EnableTransport=False",
        "-ddc=InstalledNoZenLocalFallback",
        f"-ExecCmds=t.MaxFPS {config.fps}",
        "-SaveToUserDir",
        f"-UserDir={user_root / 'ue-user'}",
        f"-LocalDataCachePath={user_root / 'xdg-cache' / 'UnrealEngine' / 'DDC'}",
        "-log",
    ]
    if spec.camera_profile is not None:
        command.insert(
            10,
            f"-VistaCameraProfile={spec.camera_profile}",
        )
    return command


def sanitized_environment(config: GameRuntimeConfig) -> dict[str, str]:
    spec = validate_runtime_profile_binding(config)
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
    if spec.runtime_profile is not None:
        user_root = config.workspace / "runtime-user"
        environment.update(
            {
                "HOME": str(user_root / "home"),
                "TMPDIR": str(user_root / "tmp"),
                "TMP": str(user_root / "tmp"),
                "TEMP": str(user_root / "tmp"),
                "XDG_CACHE_HOME": str(user_root / "xdg-cache"),
                "XDG_CONFIG_HOME": str(user_root / "xdg-config"),
                "XDG_DATA_HOME": str(user_root / "xdg-data"),
                "VISTA_RUNTIME_PROFILE": spec.runtime_profile,
                "VISTA_CAMERA_PROFILE": spec.camera_profile or "",
            }
        )
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


def validate_typed_readiness_response(
    response: Any,
    *,
    command_id: str,
    expected_revision: str = DEFAULT_WORLD_REVISION,
) -> dict[str, Any]:
    required = {
        "command_id", "status", "code", "world_revision",
        "session_generation", "event_status", "active_event",
    }
    if not isinstance(response, dict) or set(response) != required:
        raise RuntimeSafetyError("typed runtime readiness response has an invalid shape")
    if response.get("command_id") != command_id \
            or response.get("status") != "success" \
            or response.get("code") != "READY" \
            or response.get("world_revision") != expected_revision \
            or response.get("session_generation") != 0 \
            or not isinstance(response.get("event_status"), str) \
            or not 1 <= len(response["event_status"]) <= 80 \
            or (response.get("active_event") is not None and (
                not isinstance(response["active_event"], str)
                or re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,79}", response["active_event"]) is None
            )):
        raise RuntimeSafetyError("typed runtime readiness identity does not match")
    return dict(response)


def probe_typed_runtime(
    port: int,
    *,
    expected_revision: str = DEFAULT_WORLD_REVISION,
    timeout: float = 1.0,
) -> dict[str, Any]:
    if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
        raise RuntimeSafetyError("typed runtime readiness port is invalid")
    if not 0 < timeout <= 5:
        raise RuntimeSafetyError("typed runtime readiness timeout is invalid")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,79}", expected_revision):
        raise RuntimeSafetyError("typed runtime readiness revision is invalid")
    command_id = "vwc-" + os.urandom(12).hex()
    request = {
        "type": "vista_world_action",
        "params": {"operation": "status", "command_id": command_id},
    }
    encoded = json.dumps(request, separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n"
    chunks = bytearray()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as connection:
            connection.settimeout(timeout)
            connection.sendall(encoded)
            while len(chunks) <= TYPED_RESPONSE_MAX_BYTES:
                block = connection.recv(min(8192, TYPED_RESPONSE_MAX_BYTES + 1 - len(chunks)))
                if not block:
                    break
                chunks.extend(block)
                try:
                    response = json.loads(chunks.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                return validate_typed_readiness_response(
                    response,
                    command_id=command_id,
                    expected_revision=expected_revision,
                )
    except (OSError, TimeoutError) as exc:
        raise RuntimeSafetyError("typed runtime readiness connection failed") from exc
    if len(chunks) > TYPED_RESPONSE_MAX_BYTES:
        raise RuntimeSafetyError("typed runtime readiness response exceeded its limit")
    raise RuntimeSafetyError("typed runtime readiness response was incomplete")


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
    spec = validate_runtime_profile_binding(config)
    config_payload = {
        **asdict(config),
        "workspace": str(config.workspace),
        "project": str(config.project),
        "ue_editor": str(config.ue_editor),
        "nvidia_icd": str(config.nvidia_icd) if config.nvidia_icd else None,
        "nvidia_compat": str(config.nvidia_compat) if config.nvidia_compat else None,
    }
    if spec.runtime_profile is None:
        config_payload.pop("runtime_profile")
    else:
        config_payload["camera_profile"] = spec.camera_profile
    security = {
        "editor_chrome": False,
        "render_offscreen": False,
        "reserved_gpu_indices": sorted(RESERVED_GPU_INDICES),
        "reserved_ports": sorted(RESERVED_PORTS),
        "arbitrary_command": False,
    }
    if spec.runtime_profile is not None:
        security.update(
            {
                "runtime_profile_closed": True,
                "camera_profile_closed": True,
            }
        )
    return {
        "schema": R2_SCHEMA if spec.runtime_profile is not None else SCHEMA,
        "created_at": utc_now(),
        "mode": (
            "unreal-editor-game-preview-realistic"
            if spec.runtime_profile is not None
            else "unreal-editor-game-preview"
        ),
        "config": config_payload,
        "command": build_game_command(config),
        "command_shell_preview": shlex.join(build_game_command(config)),
        "security": security,
    }
