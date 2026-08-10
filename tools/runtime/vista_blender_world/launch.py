#!/usr/bin/env python3
"""Launch an isolated loopback-only Studio -> UE -> Pixel Streaming runtime."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from tools.runtime.vista_blender_world.runtime import (  # type: ignore
        LOOPBACK_HOST,
        Ports,
        RuntimePaths,
        RuntimeSafetyError,
        assert_ports_available,
        assert_safe_gpu,
        atomic_write_json,
        build_cirrus_config,
        build_ue_command,
        ensure_access_token,
        process_identity,
        process_start_ticks,
        redacted_runtime_plan,
        sanitized_child_environment,
        utc_now,
        validate_map,
        validate_model_policy,
        validate_paths,
        validate_render_settings,
        wait_for_loopback_listener,
        write_state,
    )
else:
    from .runtime import (
        LOOPBACK_HOST,
        Ports,
        RuntimePaths,
        RuntimeSafetyError,
        assert_ports_available,
        assert_safe_gpu,
        atomic_write_json,
        build_cirrus_config,
        build_ue_command,
        ensure_access_token,
        process_identity,
        process_start_ticks,
        redacted_runtime_plan,
        sanitized_child_environment,
        utc_now,
        validate_map,
        validate_model_policy,
        validate_paths,
        validate_render_settings,
        wait_for_loopback_listener,
        write_state,
    )


def env_value(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in {None, ""} else default


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Launch a disposable VISTA Blender world on GPU 1 and loopback-only ports."
    )
    result.add_argument("--project", default=env_value("VISTA_UE_PROJECT"))
    result.add_argument("--map", dest="map_path", default=env_value("VISTA_UE_MAP"))
    result.add_argument("--workspace", default=env_value("VISTA_RUNTIME_WORKSPACE"))
    result.add_argument("--ue-editor", default=env_value("UE_EDITOR"))
    result.add_argument("--cirrus-dir", default=env_value("CIRRUS_DIR"))
    result.add_argument(
        "--studio-workspace", default=env_value("SIMWORLD_STUDIO_WORKSPACE")
    )
    result.add_argument(
        "--node-bin", default=env_value("NODE_BIN", "/home/yhliu/.local/bin/node")
    )
    result.add_argument("--claude-bin", default=env_value("CLAUDE_BIN"))
    result.add_argument("--nvidia-icd", default=env_value("NVIDIA_ICD"))
    result.add_argument("--nvidia-compat", default=env_value("NVIDIA_COMPAT"))
    result.add_argument("--token-file", default=env_value("STUDIO_ACCESS_TOKEN_FILE"))
    result.add_argument(
        "--studio-port", type=int, default=int(env_value("PORT", "3022"))
    )
    result.add_argument(
        "--ue-mcp-port", type=int, default=int(env_value("UNREAL_PORT", "55582"))
    )
    result.add_argument(
        "--cirrus-http-port",
        type=int,
        default=int(env_value("CIRRUS_HTTP_PORT", "8615")),
    )
    result.add_argument(
        "--cirrus-streamer-port",
        type=int,
        default=int(env_value("CIRRUS_WS_PORT", "8616")),
    )
    result.add_argument(
        "--cirrus-sfu-port", type=int, default=int(env_value("CIRRUS_SFU_PORT", "8919"))
    )
    result.add_argument(
        "--gpu", type=int, default=int(env_value("VISTA_RUNTIME_GPU", "1"))
    )
    result.add_argument(
        "--width", type=int, default=int(env_value("VISTA_RENDER_WIDTH", "1280"))
    )
    result.add_argument(
        "--height", type=int, default=int(env_value("VISTA_RENDER_HEIGHT", "720"))
    )
    result.add_argument(
        "--fps", type=int, default=int(env_value("VISTA_DEMO_FPS", "60"))
    )
    result.add_argument(
        "--ue-ready-timeout-seconds",
        type=int,
        default=int(env_value("VISTA_UE_READY_TIMEOUT_SECONDS", "600")),
        help="bounded cold-start allowance for the UE MCP listener (60-900 seconds)",
    )
    result.add_argument(
        "--model-mode",
        choices=("off", "mock", "live"),
        default=env_value("STUDIO_MODEL_MODE", "off"),
    )
    result.add_argument(
        "--coding-agents",
        action="store_true",
        default=env_value("STUDIO_CODING_AGENTS_ENABLED", "0").lower() in {"1", "true"},
    )
    result.add_argument("--claude-model", default=env_value("CLAUDE_MODEL", "fable"))
    result.add_argument("--preflight-only", action="store_true")
    return result


def required_path(value: str | None, option: str) -> Path:
    if not value:
        raise RuntimeSafetyError(
            f"{option} is required as an exact argument or environment variable"
        )
    return Path(value)


def validate_ue_ready_timeout(value: int) -> int:
    """Keep cold shader-cache startup bounded while allowing slower hosts."""

    if not 60 <= value <= 900:
        raise RuntimeSafetyError(
            "UE readiness timeout must be from 60 through 900 seconds"
        )
    return value


def log_line(log_handle: Any, message: str) -> None:
    line = f"[{utc_now()}] {message}"
    print(line, flush=True)
    log_handle.write(line + "\n")
    log_handle.flush()


def ensure_private_directory(path: Path, workspace: Path) -> Path:
    try:
        path.relative_to(workspace)
    except ValueError as exc:
        raise RuntimeSafetyError("runtime directory escaped its workspace") from exc
    if path.is_symlink():
        raise RuntimeSafetyError(f"runtime directory must not be a symlink: {path}")
    if path.exists():
        metadata = path.stat()
        if not path.is_dir() or metadata.st_uid != os.getuid():
            raise RuntimeSafetyError(f"runtime directory is not a user-owned directory: {path}")
        if (metadata.st_mode & 0o777) != 0o700:
            raise RuntimeSafetyError(f"runtime directory mode must be exactly 0700: {path}")
    else:
        path.mkdir(mode=0o700)
    if path.resolve(strict=True).parent != workspace.resolve(strict=True):
        raise RuntimeSafetyError(f"runtime directory has an unexpected resolved parent: {path}")
    return path


def open_exclusive_private_log(path: Path) -> Any:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise RuntimeSafetyError(f"refusing non-fresh runtime log path: {path}") from exc
    return os.fdopen(descriptor, "w", encoding="utf-8")


def start_child(
    command: list[str],
    *,
    env: dict[str, str],
    cwd: Path,
    log_path: Path,
) -> tuple[subprocess.Popen[Any], Any]:
    handle = open_exclusive_private_log(log_path)
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except Exception:
        handle.close()
        raise
    return process, handle


def terminate_owned(process: subprocess.Popen[Any], start_ticks: int | None) -> None:
    if process.poll() is not None or start_ticks is None:
        return
    if process_start_ticks(process.pid) != start_ticks:
        return
    try:
        if os.getpgid(process.pid) != process.pid:
            return
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        process.wait(timeout=8)
        return
    except subprocess.TimeoutExpired:
        pass
    if process_start_ticks(process.pid) == start_ticks:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    workspace_arg = required_path(args.workspace, "--workspace/VISTA_RUNTIME_WORKSPACE")
    token_file = (
        Path(args.token_file) if args.token_file else workspace_arg / "access-token"
    )
    paths = validate_paths(
        RuntimePaths(
            workspace=workspace_arg,
            project=required_path(args.project, "--project/VISTA_UE_PROJECT"),
            ue_editor=required_path(args.ue_editor, "--ue-editor/UE_EDITOR"),
            cirrus_dir=required_path(args.cirrus_dir, "--cirrus-dir/CIRRUS_DIR"),
            studio_workspace=required_path(
                args.studio_workspace, "--studio-workspace/SIMWORLD_STUDIO_WORKSPACE"
            ),
            node_bin=required_path(args.node_bin, "--node-bin/NODE_BIN"),
            token_file=token_file,
            claude_bin=Path(args.claude_bin) if args.claude_bin else None,
            nvidia_icd=Path(args.nvidia_icd) if args.nvidia_icd else None,
            nvidia_compat=Path(args.nvidia_compat) if args.nvidia_compat else None,
        )
    )
    map_path = validate_map(args.map_path or "")
    ports = Ports(
        studio=args.studio_port,
        ue_mcp=args.ue_mcp_port,
        cirrus_http=args.cirrus_http_port,
        cirrus_streamer=args.cirrus_streamer_port,
        cirrus_sfu=args.cirrus_sfu_port,
    ).validate()
    gpu = assert_safe_gpu(args.gpu)
    model_mode = validate_model_policy(
        args.model_mode, args.coding_agents, paths.claude_bin
    )
    validate_render_settings(args.width, args.height, args.fps)
    ue_ready_timeout_seconds = validate_ue_ready_timeout(
        args.ue_ready_timeout_seconds
    )
    if model_mode == "live" and not re.fullmatch(
        r"[A-Za-z0-9._:-]{1,128}", args.claude_model
    ):
        raise RuntimeSafetyError(
            "Claude model must be a short exact model or alias identifier"
        )
    assert_ports_available(ports)
    plan = redacted_runtime_plan(
        paths=paths,
        map_path=map_path,
        ports=ports,
        gpu=gpu,
        model_mode=model_mode,
        coding_agents=args.coding_agents,
    )
    if args.preflight_only:
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0

    runtime_dir = ensure_private_directory(paths.workspace / "runtime", paths.workspace)
    logs = ensure_private_directory(paths.workspace / "logs", paths.workspace)
    config_path = runtime_dir / "cirrus-config.json"
    state_path = paths.workspace / "runtime-state.json"
    if state_path.exists() or config_path.exists():
        raise RuntimeSafetyError(
            "runtime workspace already contains launch state; use a fresh append-only attempt directory"
        )
    token = ensure_access_token(paths.token_file)
    atomic_write_json(config_path, build_cirrus_config(ports))
    children: list[tuple[str, subprocess.Popen[Any], int | None, Any]] = []
    stopping = False
    launcher_identity = process_identity(os.getpid(), "launcher")
    state: dict[str, Any] = {
        **plan,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "status": "starting",
        "launcher": launcher_identity,
        "cirrus_config": str(config_path),
        "logs": {
            "launcher": str(logs / "launcher.log"),
            "cirrus": str(logs / "cirrus.log"),
            "ue": str(logs / "ue.log"),
            "studio": str(logs / "studio.log"),
        },
        "processes": [],
        "render": {"width": args.width, "height": args.height, "fps": args.fps},
        "timeouts": {"ue_ready_seconds": ue_ready_timeout_seconds},
        "claude_model": args.claude_model if model_mode == "live" else None,
    }
    write_state(state_path, state)
    launcher_log = open_exclusive_private_log(logs / "launcher.log")

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGHUP, request_stop)

    service_env = sanitized_child_environment()
    service_env.update(
        {
            "STUDIO_HOST": LOOPBACK_HOST,
            "STUDIO_TRANSPORT_PROFILE": "loopback",
            "CUDA_VISIBLE_DEVICES": str(gpu),
        }
    )
    if paths.nvidia_icd:
        service_env["VK_ICD_FILENAMES"] = str(paths.nvidia_icd)
    if paths.nvidia_compat:
        existing = service_env.get("LD_LIBRARY_PATH", "")
        service_env["LD_LIBRARY_PATH"] = str(paths.nvidia_compat) + (
            f":{existing}" if existing else ""
        )

    try:
        log_line(launcher_log, "Starting reviewed loopback-only Cirrus")
        cirrus_env = service_env.copy()
        cirrus_env["STUDIO_ACCESS_TOKEN"] = token
        cirrus, handle = start_child(
            [str(paths.node_bin), "cirrus.js", f"--configFile={config_path}"],
            env=cirrus_env,
            cwd=paths.cirrus_dir,
            log_path=logs / "cirrus.log",
        )
        children.append(("cirrus", cirrus, process_start_ticks(cirrus.pid), handle))
        state["processes"] = [process_identity(cirrus.pid, "cirrus")]
        state["updated_at"] = utc_now()
        write_state(state_path, state)
        wait_for_loopback_listener(
            ports.cirrus_http,
            timeout_seconds=30,
            processes=[cirrus],
            owner_pid=cirrus.pid,
            cancelled=lambda: stopping,
        )
        wait_for_loopback_listener(
            ports.cirrus_streamer,
            timeout_seconds=30,
            processes=[cirrus],
            owner_pid=cirrus.pid,
            cancelled=lambda: stopping,
        )
        wait_for_loopback_listener(
            ports.cirrus_sfu,
            timeout_seconds=30,
            processes=[cirrus],
            owner_pid=cirrus.pid,
            cancelled=lambda: stopping,
        )
        state["status"] = "cirrus_ready"
        state["updated_at"] = utc_now()
        write_state(state_path, state)

        log_line(launcher_log, f"Starting Unreal Editor on owned GPU {gpu}")
        ue_command = build_ue_command(
            paths,
            map_path,
            ports,
            gpu=gpu,
            width=args.width,
            height=args.height,
            fps=args.fps,
        )
        ue, handle = start_child(
            ue_command,
            env=service_env,
            cwd=paths.project.parent,
            log_path=logs / "ue.log",
        )
        children.append(("ue", ue, process_start_ticks(ue.pid), handle))
        state["processes"].append(process_identity(ue.pid, "ue"))
        state["updated_at"] = utc_now()
        write_state(state_path, state)
        wait_for_loopback_listener(
            ports.ue_mcp,
            timeout_seconds=ue_ready_timeout_seconds,
            processes=[cirrus, ue],
            owner_pid=ue.pid,
            cancelled=lambda: stopping,
        )
        state["status"] = "ue_ready"
        state["updated_at"] = utc_now()
        write_state(state_path, state)

        log_line(launcher_log, "Starting authenticated loopback-only SimWorld Studio")
        studio_env = service_env.copy()
        studio_env["STUDIO_ACCESS_TOKEN"] = token
        ue_root = paths.ue_editor.parents[3]
        studio_env.update(
            {
                "PORT": str(ports.studio),
                "UNREAL_HOST": LOOPBACK_HOST,
                "UNREAL_PORT": str(ports.ue_mcp),
                "CIRRUS_HTTP_PORT": str(ports.cirrus_http),
                "CIRRUS_WS_PORT": str(ports.cirrus_streamer),
                "CIRRUS_SFU_PORT": str(ports.cirrus_sfu),
                "PIXEL_STREAMING_URL": f"http://{LOOPBACK_HOST}:{ports.cirrus_http}",
                "STUDIO_MODEL_MODE": model_mode,
                "STUDIO_CODING_AGENTS_ENABLED": "1" if args.coding_agents else "0",
                "MOCK_MODE": "1" if model_mode == "mock" else "0",
                "VISTA_DEMO_ENABLED": "1",
                "VISTA_DEMO_FPS": str(args.fps),
                "AGENT_SANDBOX": "1",
                "CLAUDE_MODEL": args.claude_model,
                "LLM_PROVIDER": "claude",
                "UE_ROOT": str(ue_root),
                # Studio's content helpers call dirname(UE_PROJECT_PATH), so
                # this contract is the exact .uproject file, not its parent.
                "UE_PROJECT_PATH": str(paths.project),
                "NODE_ENV": "development",
                "ASSET_RETRIEVAL_MODE": "catalog",
                "ASSET_PREFILTER": "false",
                "SIMWORLD_MCP_CONFIG": str(runtime_dir / f"mcp-{ports.studio}.json"),
                "XDG_CACHE_HOME": str(runtime_dir / "xdg-cache"),
                "XDG_STATE_HOME": str(runtime_dir / "xdg-state"),
                "XDG_DATA_HOME": str(runtime_dir / "xdg-data"),
                "VISTA_IMPORT_ARTIFACT_ROOT": str(paths.workspace / "ue"),
            }
        )
        if paths.claude_bin:
            studio_env["CLAUDE_BIN"] = str(paths.claude_bin)
        studio, handle = start_child(
            [
                str(paths.node_bin),
                str(paths.studio_workspace / "web" / "server" / "index.js"),
            ],
            env=studio_env,
            cwd=paths.studio_workspace / "web",
            log_path=logs / "studio.log",
        )
        children.append(("studio", studio, process_start_ticks(studio.pid), handle))
        state["processes"].append(process_identity(studio.pid, "studio"))
        state["updated_at"] = utc_now()
        write_state(state_path, state)
        wait_for_loopback_listener(
            ports.studio,
            timeout_seconds=60,
            processes=[cirrus, ue, studio],
            owner_pid=studio.pid,
            cancelled=lambda: stopping,
        )
        state["status"] = "ready"
        state["ready_at"] = utc_now()
        state["updated_at"] = utc_now()
        write_state(state_path, state)
        log_line(
            launcher_log,
            f"READY: tunnel local port {ports.studio} to 127.0.0.1:{ports.studio}",
        )

        while not stopping:
            for role, child, _ticks, _handle in children:
                if child.poll() is not None:
                    raise RuntimeSafetyError(
                        f"owned {role} process exited unexpectedly with status {child.returncode}"
                    )
            time.sleep(1)
        state["status"] = "stopping"
        state["updated_at"] = utc_now()
        write_state(state_path, state)
        return 0
    except Exception as exc:
        state["status"] = "failed"
        state["failed_at"] = utc_now()
        state["updated_at"] = utc_now()
        state["error"] = str(exc)
        write_state(state_path, state)
        log_line(launcher_log, f"ERROR: {exc}")
        return 1
    finally:
        for _role, child, ticks, _handle in reversed(children):
            terminate_owned(child, ticks)
        for _role, _child, _ticks, handle in children:
            handle.close()
        if state.get("status") != "failed":
            state["status"] = "stopped"
            state["stopped_at"] = utc_now()
            state["updated_at"] = utc_now()
            write_state(state_path, state)
        log_line(
            launcher_log, "Stopped only this launcher's recorded child process groups"
        )
        launcher_log.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeSafetyError as error:
        print(f"runtime preflight refused: {error}", file=sys.stderr)
        raise SystemExit(2)
