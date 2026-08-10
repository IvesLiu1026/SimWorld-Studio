#!/usr/bin/env python3
"""Shared fail-closed primitives for the isolated VISTA Blender world runtime."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import socket
import stat
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


SCHEMA = "vista-blender-world-runtime/v1"
VERIFY_SCHEMA = "vista-blender-world-runtime-verification/v1"
LOOPBACK_HOST = "127.0.0.1"
DEFAULT_STUDIO_PORT = 3022
DEFAULT_UE_MCP_PORT = 55582
DEFAULT_CIRRUS_HTTP_PORT = 8615
DEFAULT_CIRRUS_STREAMER_PORT = 8616
DEFAULT_CIRRUS_SFU_PORT = 8919
DEFAULT_GPU = 1
MIN_TOKEN_LENGTH = 32
EXPECTED_CIRRUS_SHA256 = (
    "133a12cf843c69914263a41c3ea3d7f09914ad9241125358850ea0318e55300e"
)
EXPECTED_STUDIO_PLAYER_SHA256 = (
    "5d2eab57ef9e91fd352cb53cd85882d58ec2aaef31baedbfc314bbb5b10a5a17"
)
FORBIDDEN_COMPONENTS = frozenset(
    {
        "archive",
        "archives",
        "archived",
        "canonical",
        "disposable-project-r7",
        "disposable-project-r8",
        "project-r7",
        "project-r8",
        "r7",
        "r8",
    }
)
MAP_RE = re.compile(r"^/Game/[A-Za-z0-9_./-]+(?:\.umap)?$")


class RuntimeSafetyError(RuntimeError):
    """Raised when a requested runtime would violate the isolation contract."""


@dataclass(frozen=True)
class Ports:
    studio: int = DEFAULT_STUDIO_PORT
    ue_mcp: int = DEFAULT_UE_MCP_PORT
    cirrus_http: int = DEFAULT_CIRRUS_HTTP_PORT
    cirrus_streamer: int = DEFAULT_CIRRUS_STREAMER_PORT
    cirrus_sfu: int = DEFAULT_CIRRUS_SFU_PORT

    def values(self) -> tuple[int, ...]:
        return (
            self.studio,
            self.ue_mcp,
            self.cirrus_http,
            self.cirrus_streamer,
            self.cirrus_sfu,
        )

    def validate(self) -> "Ports":
        for value in self.values():
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= 65535
            ):
                raise RuntimeSafetyError(f"invalid TCP port: {value!r}")
        if len(set(self.values())) != len(self.values()):
            raise RuntimeSafetyError(
                "Studio, UE MCP, Cirrus HTTP/streamer/SFU ports must be distinct"
            )
        return self


@dataclass(frozen=True)
class RuntimePaths:
    workspace: Path
    project: Path
    ue_editor: Path
    cirrus_dir: Path
    studio_workspace: Path
    node_bin: Path
    token_file: Path
    claude_bin: Path | None = None
    nvidia_icd: Path | None = None
    nvidia_compat: Path | None = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _lexical_absolute(path: Path, label: str) -> Path:
    path = Path(path).expanduser()
    if not path.is_absolute():
        raise RuntimeSafetyError(f"{label} must be an absolute path")
    return path


def _has_forbidden_component(path: Path) -> str | None:
    for component in path.parts:
        normalized = component.strip().lower()
        if normalized in FORBIDDEN_COMPONENTS:
            return component
        if re.fullmatch(r"(?:disposable-)?project-r[78]", normalized):
            return component
    return None


def resolve_existing(path: Path, label: str, *, kind: str) -> Path:
    lexical = _lexical_absolute(path, label)
    try:
        resolved = lexical.resolve(strict=True)
    except FileNotFoundError as exc:
        raise RuntimeSafetyError(f"{label} does not exist: {lexical}") from exc
    forbidden = _has_forbidden_component(resolved)
    if forbidden:
        raise RuntimeSafetyError(f"{label} contains forbidden component {forbidden!r}")
    if kind == "file" and not resolved.is_file():
        raise RuntimeSafetyError(f"{label} must be a regular file: {resolved}")
    if kind == "dir" and not resolved.is_dir():
        raise RuntimeSafetyError(f"{label} must be a directory: {resolved}")
    return resolved


def resolve_workspace(path: Path) -> Path:
    lexical = _lexical_absolute(path, "workspace")
    lexical_forbidden = _has_forbidden_component(lexical)
    if lexical_forbidden:
        raise RuntimeSafetyError(
            f"workspace contains forbidden component {lexical_forbidden!r}"
        )
    lexical.mkdir(mode=0o700, parents=True, exist_ok=True)
    resolved = lexical.resolve(strict=True)
    forbidden = _has_forbidden_component(resolved)
    if forbidden:
        raise RuntimeSafetyError(
            f"workspace contains forbidden component {forbidden!r}"
        )
    if not resolved.is_dir():
        raise RuntimeSafetyError(f"workspace must be a directory: {resolved}")
    if not os.access(resolved, os.R_OK | os.W_OK | os.X_OK):
        raise RuntimeSafetyError(
            f"workspace is not readable/writable/searchable: {resolved}"
        )
    return resolved


def ensure_contained(candidate: Path, root: Path, label: str) -> None:
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise RuntimeSafetyError(
            f"{label} must be contained by workspace {root}"
        ) from exc


def validate_map(map_path: str) -> str:
    value = str(map_path or "").strip()
    if not MAP_RE.fullmatch(value) or ".." in value.split("/"):
        raise RuntimeSafetyError(
            "map must be an exact /Game/... package path using safe filename characters"
        )
    return value


def validate_paths(paths: RuntimePaths) -> RuntimePaths:
    workspace = resolve_workspace(paths.workspace)
    project = resolve_existing(paths.project, "UE project", kind="file")
    if project.suffix != ".uproject":
        raise RuntimeSafetyError(f"UE project must end in .uproject: {project}")
    ensure_contained(project, workspace, "UE project")
    try:
        project_contract = json.loads(project.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeSafetyError(
            "UE project descriptor is unreadable or invalid JSON"
        ) from exc
    if not isinstance(project_contract, dict):
        raise RuntimeSafetyError("UE project descriptor must contain a JSON object")
    plugin_entries = {
        str(entry.get("Name")): entry.get("Enabled")
        for entry in project_contract.get("Plugins", [])
        if isinstance(entry, dict)
    }
    disabled = [
        name
        for name in ("UnrealMCP", "PixelStreaming")
        if plugin_entries.get(name) is not True
    ]
    if disabled:
        raise RuntimeSafetyError(
            "disposable UE project must explicitly enable required runtime plugins: "
            + ", ".join(disabled)
        )
    ue_editor = resolve_existing(paths.ue_editor, "Unreal Editor", kind="file")
    if not os.access(ue_editor, os.X_OK):
        raise RuntimeSafetyError(f"Unreal Editor is not executable: {ue_editor}")
    if (
        ue_editor.name != "UnrealEditor"
        or ue_editor.parent.name != "Linux"
        or ue_editor.parent.parent.name != "Binaries"
        or ue_editor.parent.parent.parent.name != "Engine"
    ):
        raise RuntimeSafetyError(
            "Unreal Editor path must be an exact Engine/Binaries/Linux/UnrealEditor"
        )
    cirrus_dir = resolve_existing(paths.cirrus_dir, "Cirrus directory", kind="dir")
    studio_workspace = resolve_existing(
        paths.studio_workspace, "Studio workspace", kind="dir"
    )
    ensure_contained(studio_workspace, workspace, "Studio workspace")
    node_bin = resolve_existing(paths.node_bin, "Node executable", kind="file")
    if not os.access(node_bin, os.X_OK):
        raise RuntimeSafetyError(f"Node is not executable: {node_bin}")
    server = studio_workspace / "web" / "server" / "index.js"
    if not server.is_file():
        raise RuntimeSafetyError(f"Studio server entrypoint is missing: {server}")
    player_paths = (
        studio_workspace / "web" / "public" / "ue-player.html",
        studio_workspace / "web" / "dist" / "ue-player.html",
    )
    for player in player_paths:
        if not player.is_file() or player.is_symlink():
            raise RuntimeSafetyError(
                f"Studio player must be a regular non-symlink file: {player}"
            )
        if sha256_file(player) != EXPECTED_STUDIO_PLAYER_SHA256:
            raise RuntimeSafetyError(
                "Studio player is not the reviewed fixed-viewport VISTA build: "
                + str(player)
            )
    cirrus = cirrus_dir / "cirrus.js"
    if not cirrus.is_file() or cirrus.is_symlink():
        raise RuntimeSafetyError(
            f"Cirrus entrypoint must be a regular non-symlink file: {cirrus}"
        )
    source_prefix = cirrus.read_text(encoding="utf-8", errors="replace")[:4096]
    if "VISTA_LOOPBACK_PATCH_V1" not in source_prefix:
        raise RuntimeSafetyError(
            "Cirrus is not the reviewed loopback/token-authenticated VISTA patch"
        )
    receipt = cirrus_dir / "cirrus.js.vista-receipt.json"
    if not receipt.is_file() or receipt.is_symlink():
        raise RuntimeSafetyError(f"Cirrus patch receipt is missing: {receipt}")
    try:
        patch_receipt = json.loads(receipt.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeSafetyError(
            "Cirrus patch receipt is unreadable or invalid"
        ) from exc
    if not isinstance(patch_receipt, dict) or (
        patch_receipt.get("schema") != "vista-cirrus-loopback-patch/v1"
        or patch_receipt.get("patch_version") != "VISTA_LOOPBACK_PATCH_V1"
        or patch_receipt.get("patched_sha256") != EXPECTED_CIRRUS_SHA256
        or sha256_file(cirrus) != EXPECTED_CIRRUS_SHA256
    ):
        raise RuntimeSafetyError(
            "Cirrus patch receipt does not bind the reviewed source bytes"
        )
    claude_bin = None
    if paths.claude_bin is not None:
        claude_bin = resolve_existing(
            paths.claude_bin, "Claude executable", kind="file"
        )
        if not os.access(claude_bin, os.X_OK):
            raise RuntimeSafetyError(f"Claude is not executable: {claude_bin}")
    nvidia_icd = None
    if paths.nvidia_icd is not None:
        nvidia_icd = resolve_existing(paths.nvidia_icd, "NVIDIA ICD", kind="file")
    nvidia_compat = None
    if paths.nvidia_compat is not None:
        nvidia_compat = resolve_existing(
            paths.nvidia_compat, "NVIDIA compatibility directory", kind="dir"
        )
    token_file = _lexical_absolute(paths.token_file, "Studio token file")
    ensure_contained(token_file.resolve(strict=False), workspace, "Studio token file")
    if token_file.is_symlink():
        raise RuntimeSafetyError(
            f"Studio token file must not be a symlink: {token_file}"
        )
    return RuntimePaths(
        workspace=workspace,
        project=project,
        ue_editor=ue_editor,
        cirrus_dir=cirrus_dir,
        studio_workspace=studio_workspace,
        node_bin=node_bin,
        token_file=token_file,
        claude_bin=claude_bin,
        nvidia_icd=nvidia_icd,
        nvidia_compat=nvidia_compat,
    )


def _open_private(path: Path, flags: int) -> int:
    return os.open(path, flags | os.O_NOFOLLOW, 0o600)


def atomic_write_bytes(path: Path, payload: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    serialized = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    atomic_write_bytes(path, serialized)


def ensure_access_token(path: Path) -> str:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or path.is_symlink():
            raise RuntimeSafetyError(
                f"Studio token path must be a regular non-symlink file: {path}"
            )
        if info.st_uid != os.getuid():
            raise RuntimeSafetyError(
                "Studio token file must be owned by the runtime user"
            )
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise RuntimeSafetyError("Studio token file mode must be exactly 0600")
        fd = _open_private(path, os.O_RDONLY)
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            token = handle.read().rstrip("\r\n")
    else:
        token = secrets.token_urlsafe(48)
        fd = _open_private(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    if (
        len(token) < MIN_TOKEN_LENGTH
        or len(token) > 4096
        or any(ord(char) < 32 for char in token)
    ):
        raise RuntimeSafetyError("Studio token file does not meet token policy")
    return token


def build_cirrus_config(ports: Ports) -> dict[str, Any]:
    ports.validate()
    return {
        "UseFrontend": True,
        "UseMatchmaker": False,
        "BindAddress": LOOPBACK_HOST,
        "HttpPort": ports.cirrus_http,
        "StreamerPort": ports.cirrus_streamer,
        "SFUPort": ports.cirrus_sfu,
    }


def process_start_ticks(pid: int) -> int | None:
    try:
        value = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        # Field 2 is parenthesized and may itself contain spaces. Fields after
        # the final ')' begin at field 3; starttime is field 22.
        suffix = value[value.rindex(")") + 2 :].split()
        return int(suffix[19])
    except (FileNotFoundError, IndexError, OSError, ValueError):
        return None


def process_identity(pid: int, role: str) -> dict[str, Any]:
    ticks = process_start_ticks(pid)
    if ticks is None:
        raise RuntimeSafetyError(
            f"{role} process exited before its identity was recorded"
        )
    try:
        executable = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        executable = None
    try:
        process_group = os.getpgid(pid)
    except (ProcessLookupError, PermissionError):
        process_group = None
    return {
        "role": role,
        "pid": pid,
        "start_ticks": ticks,
        "executable": executable,
        "process_group": process_group,
    }


def identity_is_live(identity: Mapping[str, Any]) -> bool:
    try:
        pid = int(identity["pid"])
        expected = int(identity["start_ticks"])
    except (KeyError, TypeError, ValueError):
        return False
    if process_start_ticks(pid) != expected:
        return False
    expected_executable = identity.get("executable")
    if expected_executable:
        try:
            if os.readlink(f"/proc/{pid}/exe") != expected_executable:
                return False
        except OSError:
            return False
    expected_group = identity.get("process_group")
    if expected_group is not None:
        try:
            if os.getpgid(pid) != int(expected_group):
                return False
        except (ProcessLookupError, PermissionError, TypeError, ValueError):
            return False
    return True


def _split_host_port(local: str) -> tuple[str, int] | None:
    value = local.strip()
    if value.startswith("["):
        closing = value.rfind("]:")
        if closing < 0:
            return None
        host, raw_port = value[1:closing], value[closing + 2 :]
    else:
        host, separator, raw_port = value.rpartition(":")
        if not separator:
            return None
    try:
        return host, int(raw_port)
    except ValueError:
        return None


def listeners_for_port(port: int) -> list[str]:
    try:
        completed = subprocess.run(
            ["ss", "-ltnH", "sport", "=", f":{port}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        raise RuntimeSafetyError(
            "ss is required to verify loopback listener ownership"
        ) from exc
    if completed.returncode != 0:
        raise RuntimeSafetyError(f"ss failed while checking port {port}")
    listeners: list[str] = []
    for line in completed.stdout.splitlines():
        columns = line.split()
        if len(columns) < 4:
            continue
        parsed = _split_host_port(columns[3])
        if parsed and parsed[1] == port:
            listeners.append(parsed[0])
    return listeners


def listener_pids_for_port(port: int) -> set[int]:
    """Return same-user listener PIDs for one exact TCP port.

    The runtime is user-owned, so ``ss -p`` exposes each expected process.  An
    empty owner set is not acceptable evidence for a live runtime even if an
    address happens to be listening.
    """

    try:
        completed = subprocess.run(
            ["ss", "-ltnpH", "sport", "=", f":{port}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        raise RuntimeSafetyError(
            "ss is required to verify loopback listener process ownership"
        ) from exc
    if completed.returncode != 0:
        raise RuntimeSafetyError(f"ss failed while checking owner of port {port}")
    return {
        int(match)
        for line in completed.stdout.splitlines()
        for match in re.findall(r"\bpid=(\d+)\b", line)
    }


def host_is_loopback(host: str) -> bool:
    normalized = host.strip().strip("[]").split("%", 1)[0]
    try:
        return socket.inet_pton(socket.AF_INET, normalized) == socket.inet_pton(
            socket.AF_INET, LOOPBACK_HOST
        )
    except OSError:
        pass
    return normalized == "::1"


def assert_port_available(port: int) -> None:
    listeners = listeners_for_port(port)
    if listeners:
        raise RuntimeSafetyError(f"required port {port} is already listening")


def assert_loopback_listener(port: int) -> list[str]:
    listeners = listeners_for_port(port)
    if not listeners:
        raise RuntimeSafetyError(f"expected listener is absent on port {port}")
    public = [host for host in listeners if not host_is_loopback(host)]
    if public:
        raise RuntimeSafetyError(f"port {port} has a non-loopback listener")
    return listeners


def assert_owned_loopback_listener(port: int, owner_pid: int) -> list[str]:
    listeners = assert_loopback_listener(port)
    owners = listener_pids_for_port(port)
    if owners != {owner_pid}:
        raise RuntimeSafetyError(
            f"port {port} listener process does not match recorded owner {owner_pid}"
        )
    return listeners


def wait_for_loopback_listener(
    port: int,
    *,
    timeout_seconds: float,
    processes: Iterable[subprocess.Popen[Any]] = (),
    owner_pid: int | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> list[str]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if cancelled is not None and cancelled():
            raise RuntimeSafetyError("runtime launch stop was requested")
        for process in processes:
            if process.poll() is not None:
                raise RuntimeSafetyError(
                    f"child process {process.pid} exited with status {process.returncode}"
                )
        listeners = listeners_for_port(port)
        if listeners:
            public = [host for host in listeners if not host_is_loopback(host)]
            if public:
                raise RuntimeSafetyError(
                    f"port {port} opened on a non-loopback interface"
                )
            if owner_pid is not None and listener_pids_for_port(port) != {owner_pid}:
                raise RuntimeSafetyError(
                    f"port {port} listener process does not match expected child {owner_pid}"
                )
            return listeners
        time.sleep(0.5)
    raise RuntimeSafetyError(f"timed out waiting for loopback listener on port {port}")


def load_json_private(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RuntimeSafetyError(f"receipt must be a regular non-symlink file: {path}")
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise RuntimeSafetyError(
            f"receipt must be owned by the runtime user with mode 0600: {path}"
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeSafetyError(
            f"receipt is unreadable or invalid JSON: {path}"
        ) from exc
    if not isinstance(value, dict):
        raise RuntimeSafetyError(f"receipt must contain a JSON object: {path}")
    return value


def redacted_runtime_plan(
    *,
    paths: RuntimePaths,
    map_path: str,
    ports: Ports,
    gpu: int,
    model_mode: str,
    coding_agents: bool,
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "status": "preflight",
        "bind_host": LOOPBACK_HOST,
        "workspace": str(paths.workspace),
        "project": str(paths.project),
        "project_sha256": sha256_file(paths.project),
        "map": map_path,
        "ue_editor": str(paths.ue_editor),
        "cirrus_dir": str(paths.cirrus_dir),
        "studio_workspace": str(paths.studio_workspace),
        "studio_server_sha256": sha256_file(
            paths.studio_workspace / "web" / "server" / "index.js"
        ),
        "node_bin": str(paths.node_bin),
        "claude_bin": str(paths.claude_bin) if paths.claude_bin else None,
        "nvidia_icd": str(paths.nvidia_icd) if paths.nvidia_icd else None,
        "nvidia_compat": str(paths.nvidia_compat) if paths.nvidia_compat else None,
        "token_file": str(paths.token_file),
        "gpu": gpu,
        "ports": asdict(ports),
        "model_mode": model_mode,
        "coding_agents": coding_agents,
        "studio_revision": git_revision(paths.studio_workspace),
    }


def validate_model_policy(
    model_mode: str, coding_agents: bool, claude_bin: Path | None
) -> str:
    mode = str(model_mode or "off").strip().lower()
    if mode not in {"off", "mock", "live"}:
        raise RuntimeSafetyError("Studio model mode must be off, mock, or live")
    if coding_agents and mode != "live":
        raise RuntimeSafetyError(
            "coding agents may be enabled only with model mode live"
        )
    if mode == "live" and claude_bin is None:
        raise RuntimeSafetyError("live model mode requires an exact Claude executable")
    return mode


def assert_safe_gpu(gpu: int) -> int:
    if isinstance(gpu, bool) or gpu != DEFAULT_GPU:
        raise RuntimeSafetyError(
            f"this isolated runtime owns GPU {DEFAULT_GPU} only; requested GPU {gpu!r}"
        )
    return gpu


def assert_ports_available(ports: Ports) -> None:
    ports.validate()
    for port in ports.values():
        assert_port_available(port)


def validate_render_settings(width: int, height: int, fps: int) -> tuple[int, int, int]:
    if width < 320 or height < 240 or width > 7680 or height > 4320:
        raise RuntimeSafetyError("render resolution is outside the supported bounds")
    if fps not in {30, 60}:
        raise RuntimeSafetyError("Pixel Streaming FPS must be 30 or 60")
    return width, height, fps


def build_ue_command(
    paths: RuntimePaths,
    map_path: str,
    ports: Ports,
    *,
    gpu: int,
    width: int,
    height: int,
    fps: int,
) -> list[str]:
    validate_render_settings(width, height, fps)
    runtime = paths.workspace / "runtime"
    user_dir = runtime / "ue-user"
    cache_dir = runtime / "xdg-cache" / "UnrealEngine" / "DDC"
    user_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    return [
        str(paths.ue_editor),
        str(paths.project),
        map_path,
        f"-MCPPort={ports.ue_mcp}",
        "-Unattended",
        "-NOSPLASH",
        "-NOSOUND",
        "-NoAnalytics",
        "-ini:EditorSettings:[/Script/UnrealEd.AnalyticsPrivacySettings]:bSendUsageData=False",
        "-UDPMESSAGING_TRANSPORT_ENABLE=0",
        "-ini:Engine:[/Script/TcpMessaging.TcpMessagingSettings]:EnableTransport=False",
        f"-ResX={width}",
        f"-ResY={height}",
        f"-ExecCmds=t.MaxFPS {fps},t.MaxFPS",
        f"-graphicsadapter={gpu}",
        "-RenderOffScreen",
        f"-EditorPixelStreamingRes={width}x{height}",
        "-EditorPixelStreamingStartOnLaunch=true",
        "-EditorPixelStreamingUseRemoteSignallingServer=true",
        f"-PixelStreamingWebRTCFps={fps}",
        f"-PixelStreamingURL=ws://{LOOPBACK_HOST}:{ports.cirrus_streamer}",
        "-log",
        "-NOWRITE",
        "-ini:Engine:[/Script/Engine.RendererSettings]:r.Shadow.Virtual.Enable=0",
        "-SaveToUserDir",
        f"-UserDir={user_dir}",
        "-ini:EditorPerProjectUserSettings:[/Script/UnrealEd.EditorLoadingSavingSettings]:bAutoSaveEnable=False",
        f"-LocalDataCachePath={cache_dir}",
    ]


def environment_without_secret_values(env: Mapping[str, str]) -> dict[str, str]:
    secret_fragments = ("TOKEN", "PASSWORD", "SECRET", "API_KEY", "POSTGRES_URL")
    return {
        key: value
        for key, value in env.items()
        if not any(fragment in key.upper() for fragment in secret_fragments)
    }


def sanitized_child_environment(env: Mapping[str, str] = os.environ) -> dict[str, str]:
    """Keep process basics while dropping inherited credentials and runtime authority."""

    exact = {
        "HOME",
        "USER",
        "LOGNAME",
        "PATH",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "SHELL",
        "TERM",
        "TMPDIR",
        "TZ",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    }
    return {
        key: value
        for key, value in env.items()
        if key in exact or key.startswith("LC_")
    }


def safe_public_state(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively reject accidental secret-bearing keys before writing receipts."""

    def check(value: Any, path: tuple[str, ...]) -> Any:
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            for key, child in value.items():
                normalized = str(key).upper()
                token_key = "TOKEN" in normalized and not normalized.endswith(
                    "TOKEN_FILE"
                )
                if token_key or any(
                    fragment in normalized
                    for fragment in ("PASSWORD", "API_KEY", "SECRET_VALUE")
                ):
                    raise RuntimeSafetyError(
                        f"secret-bearing receipt key rejected: {'.'.join((*path, str(key)))}"
                    )
                result[str(key)] = check(child, (*path, str(key)))
            return result
        if isinstance(value, (list, tuple)):
            return [check(child, path) for child in value]
        return value

    return check(payload, ())


def git_revision(worktree: Path) -> dict[str, Any]:
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--short"],
            cwd=worktree,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except (FileNotFoundError, subprocess.SubprocessError):
        return {"commit": None, "dirty": None}
    return {"commit": revision or None, "dirty": bool(status.strip())}


def write_state(path: Path, payload: Mapping[str, Any]) -> None:
    atomic_write_json(path, safe_public_state(payload))


def load_runtime_state(workspace: Path) -> tuple[Path, dict[str, Any]]:
    workspace = resolve_existing(workspace, "workspace", kind="dir")
    state_path = workspace / "runtime-state.json"
    state = load_json_private(state_path)
    if state.get("schema") != SCHEMA:
        raise RuntimeSafetyError("runtime state schema is not supported")
    if Path(str(state.get("workspace", ""))).resolve(strict=False) != workspace:
        raise RuntimeSafetyError("runtime state workspace binding does not match")
    return state_path, state


def pid_entries(state: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    processes = state.get("processes")
    if not isinstance(processes, list):
        raise RuntimeSafetyError("runtime state has no process identity list")
    if any(not isinstance(item, Mapping) for item in processes):
        raise RuntimeSafetyError("runtime state process identities are malformed")
    return processes
