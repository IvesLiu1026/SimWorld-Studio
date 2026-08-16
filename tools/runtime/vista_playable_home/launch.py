#!/usr/bin/env python3
"""Launch one owned Unreal game-only VISTA Playable Home process."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

DEFAULT_READY_TIMEOUT_S = 480.0
R2_RUNTIME_STATE_SCHEMA = "simworld.vista.playable-home-runtime-state/v2"

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from tools.runtime.vista_playable_home.runtime import (  # type: ignore
        DEFAULT_DISPLAY,
        DEFAULT_GPU,
        DEFAULT_VISTA_WORLD_PORT,
        DEFAULT_WORLD_REVISION,
        R2_RUNTIME_PROFILE,
        GameRuntimeConfig,
        RuntimeSafetyError,
        allocate_runtime_attempt,
        atomic_write_json,
        build_game_command,
        identity_is_live,
        process_identity,
        probe_typed_runtime,
        publish_current_runtime,
        redacted_plan,
        resolve_runtime_profile,
        runtime_root,
        sanitized_environment,
        utc_now,
        validate_config,
    )
else:
    from .runtime import (
        DEFAULT_DISPLAY,
        DEFAULT_GPU,
        DEFAULT_VISTA_WORLD_PORT,
        DEFAULT_WORLD_REVISION,
        R2_RUNTIME_PROFILE,
        GameRuntimeConfig,
        RuntimeSafetyError,
        allocate_runtime_attempt,
        atomic_write_json,
        build_game_command,
        identity_is_live,
        process_identity,
        probe_typed_runtime,
        publish_current_runtime,
        redacted_plan,
        resolve_runtime_profile,
        runtime_root,
        sanitized_environment,
        utc_now,
        validate_config,
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--workspace", required=True, type=Path)
    result.add_argument("--project", required=True, type=Path)
    result.add_argument("--ue-editor", required=True, type=Path)
    result.add_argument("--map", dest="map_path", required=True)
    result.add_argument(
        "--runtime-profile",
        choices=[R2_RUNTIME_PROFILE],
        default=None,
    )
    result.add_argument("--display", default=None)
    result.add_argument("--gpu", type=int, default=None)
    result.add_argument("--vista-world-port", type=int, default=None)
    result.add_argument("--width", type=int, default=None)
    result.add_argument("--height", type=int, default=None)
    result.add_argument("--fps", type=int, default=None)
    result.add_argument("--nvidia-icd", type=Path)
    result.add_argument("--nvidia-compat", type=Path)
    result.add_argument("--preflight-only", action="store_true")
    return result


def config_from_args(args: argparse.Namespace) -> GameRuntimeConfig:
    runtime_profile = getattr(args, "runtime_profile", None)
    spec = resolve_runtime_profile(runtime_profile)
    return GameRuntimeConfig(
        workspace=args.workspace,
        project=args.project,
        ue_editor=args.ue_editor,
        map_path=args.map_path,
        display=args.display if args.display is not None else spec.display,
        gpu=args.gpu if args.gpu is not None else spec.gpu,
        vista_world_port=(
            args.vista_world_port
            if args.vista_world_port is not None
            else spec.vista_world_port
        ),
        width=args.width if args.width is not None else spec.width,
        height=args.height if args.height is not None else spec.height,
        fps=args.fps if args.fps is not None else spec.fps,
        nvidia_icd=args.nvidia_icd,
        nvidia_compat=args.nvidia_compat,
        runtime_profile=runtime_profile,
    )


def open_private_log(path: Path) -> Any:
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    return os.fdopen(descriptor, "w", encoding="utf-8")


def terminate_owned_process(process: subprocess.Popen[Any], timeout: float = 5.0) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=timeout)
    except (ProcessLookupError, PermissionError, subprocess.TimeoutExpired):
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass


def wait_for_typed_runtime(
    process: subprocess.Popen[Any],
    port: int,
    *,
    timeout: float = DEFAULT_READY_TIMEOUT_S,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_error: RuntimeSafetyError | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeSafetyError("Unreal exited before typed runtime readiness")
        try:
            return probe_typed_runtime(
                port,
                expected_revision=DEFAULT_WORLD_REVISION,
                timeout=1.0,
            )
        except RuntimeSafetyError as error:
            last_error = error
            time.sleep(0.5)
    failure = RuntimeSafetyError("typed Unreal runtime did not become ready")
    failure.__cause__ = last_error
    raise failure


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    config = validate_config(config_from_args(args), create_workspace=True)
    runtime_spec = resolve_runtime_profile(config.runtime_profile)
    plan = redacted_plan(config)
    if args.preflight_only:
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0

    stopping = False

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    if runtime_spec.runtime_profile is None:
        (config.workspace / "ue-user").mkdir(mode=0o700, exist_ok=True)
        (config.workspace / "xdg-cache" / "UnrealEngine" / "DDC").mkdir(
            mode=0o700, parents=True, exist_ok=True
        )
    else:
        user_root = config.workspace / "runtime-user"
        for relative in (
            "home",
            "tmp",
            "ue-user",
            "xdg-cache",
            "xdg-config",
            "xdg-data",
            "xdg-cache/UnrealEngine/DDC",
        ):
            target = user_root / relative
            if target.is_symlink():
                raise RuntimeSafetyError("r2 runtime user directory must not be a symlink")
            target.mkdir(mode=0o700, parents=True, exist_ok=True)
            if target.resolve(strict=True) != target or not target.is_dir():
                raise RuntimeSafetyError("r2 runtime user directory identity differs")
    runtime_root_path = runtime_root(config.workspace)
    lock_descriptor = os.open(
        runtime_root_path / ".launch.lock",
        os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(lock_descriptor)
        raise RuntimeSafetyError("another VISTA World launch is in progress") from exc
    log_handle = None
    process = None
    try:
        runtime_dir = allocate_runtime_attempt(config.workspace)
        launch_plan_path = runtime_dir / "launch-plan.json"
        atomic_write_json(launch_plan_path, plan)
        launch_plan_sha256 = hashlib.sha256(launch_plan_path.read_bytes()).hexdigest()
        log_handle = open_private_log(runtime_dir / "unreal-game.log")
        if stopping:
            raise RuntimeSafetyError("VISTA World launch was cancelled")
        process = subprocess.Popen(
            build_game_command(config),
            cwd=config.project.parent,
            env=sanitized_environment(config),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        identity = process_identity(process.pid, "unreal-game")
        supervisor = process_identity(os.getpid(), "vista-world-supervisor")
        state_path = runtime_dir / "runtime-state.json"
        state: dict[str, Any] = {
            "schema": (
                R2_RUNTIME_STATE_SCHEMA
                if runtime_spec.runtime_profile is not None
                else "simworld.vista.playable-home-runtime-state/v1"
            ),
            "status": "starting",
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "map": config.map_path,
            "project": str(config.project),
            "display": config.display,
            "gpu": config.gpu,
            "vista_world_port": config.vista_world_port,
            "process": identity,
            "supervisor": supervisor,
        }
        if runtime_spec.runtime_profile is not None:
            state.update(
                {
                    "runtime_profile": runtime_spec.runtime_profile,
                    "camera_profile": runtime_spec.camera_profile,
                    "width": runtime_spec.width,
                    "height": runtime_spec.height,
                    "fps": runtime_spec.fps,
                    "launch_plan_sha256": launch_plan_sha256,
                }
            )
        atomic_write_json(state_path, state)
        publish_current_runtime(config.workspace, state_path)
    except BaseException:
        if process is not None:
            terminate_owned_process(process)
        if log_handle is not None:
            log_handle.close()
        raise
    finally:
        fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        os.close(lock_descriptor)
    try:
        readiness = wait_for_typed_runtime(process, config.vista_world_port)
    except RuntimeSafetyError as error:
        terminate_owned_process(process)
        state.update(
            status="failed",
            updated_at=utc_now(),
            exit_code=process.returncode,
            failure="typed_runtime_not_ready",
        )
        atomic_write_json(state_path, state)
        log_handle.close()
        print(f"game launch refused: {error}", file=sys.stderr)
        return process.returncode or 1
    state.update(status="running", updated_at=utc_now(), readiness=readiness)
    atomic_write_json(state_path, state)
    print(json.dumps({"status": "running", "state": str(state_path), "pid": process.pid}))
    while process.poll() is None and not stopping:
        time.sleep(0.5)
    if stopping and identity_is_live(identity):
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            if identity_is_live(identity):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
    exit_code = process.wait()
    state.update(
        status="stopped" if stopping or exit_code == 0 else "failed",
        stopped_at=utc_now(),
        updated_at=utc_now(),
        exit_code=exit_code,
    )
    atomic_write_json(state_path, state)
    log_handle.close()
    return 0 if stopping else exit_code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeSafetyError, FileExistsError, OSError) as error:
        print(f"game launch refused: {error}", file=sys.stderr)
        raise SystemExit(2)
