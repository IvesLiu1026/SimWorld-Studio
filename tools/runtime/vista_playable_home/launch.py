#!/usr/bin/env python3
"""Launch one owned Unreal game-only VISTA Playable Home process."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from tools.runtime.vista_playable_home.runtime import (  # type: ignore
        DEFAULT_DISPLAY,
        DEFAULT_GPU,
        GameRuntimeConfig,
        RuntimeSafetyError,
        atomic_write_json,
        build_game_command,
        identity_is_live,
        process_identity,
        redacted_plan,
        sanitized_environment,
        utc_now,
        validate_config,
    )
else:
    from .runtime import (
        DEFAULT_DISPLAY,
        DEFAULT_GPU,
        GameRuntimeConfig,
        RuntimeSafetyError,
        atomic_write_json,
        build_game_command,
        identity_is_live,
        process_identity,
        redacted_plan,
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
    result.add_argument("--display", default=DEFAULT_DISPLAY)
    result.add_argument("--gpu", type=int, default=DEFAULT_GPU)
    result.add_argument("--width", type=int, default=1280)
    result.add_argument("--height", type=int, default=720)
    result.add_argument("--fps", type=int, default=60)
    result.add_argument("--nvidia-icd", type=Path)
    result.add_argument("--nvidia-compat", type=Path)
    result.add_argument("--preflight-only", action="store_true")
    return result


def config_from_args(args: argparse.Namespace) -> GameRuntimeConfig:
    return GameRuntimeConfig(
        workspace=args.workspace,
        project=args.project,
        ue_editor=args.ue_editor,
        map_path=args.map_path,
        display=args.display,
        gpu=args.gpu,
        width=args.width,
        height=args.height,
        fps=args.fps,
        nvidia_icd=args.nvidia_icd,
        nvidia_compat=args.nvidia_compat,
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


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    config = validate_config(config_from_args(args), create_workspace=True)
    plan = redacted_plan(config)
    if args.preflight_only:
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0

    runtime_dir = config.workspace / "game-runtime"
    runtime_dir.mkdir(mode=0o700, exist_ok=False)
    (config.workspace / "ue-user").mkdir(mode=0o700, exist_ok=True)
    (config.workspace / "xdg-cache" / "UnrealEngine" / "DDC").mkdir(
        mode=0o700, parents=True, exist_ok=True
    )
    atomic_write_json(runtime_dir / "launch-plan.json", plan)
    log_handle = open_private_log(runtime_dir / "unreal-game.log")
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
    state_path = runtime_dir / "runtime-state.json"
    state: dict[str, Any] = {
        "schema": "simworld.vista.playable-home-runtime-state/v1",
        "status": "starting",
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "map": config.map_path,
        "display": config.display,
        "gpu": config.gpu,
        "process": identity,
    }
    atomic_write_json(state_path, state)
    stopping = False

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    time.sleep(3)
    if process.poll() is not None:
        state.update(status="failed", updated_at=utc_now(), exit_code=process.returncode)
        atomic_write_json(state_path, state)
        log_handle.close()
        return process.returncode or 1
    state.update(status="running", updated_at=utc_now())
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
    except (RuntimeSafetyError, FileExistsError) as error:
        print(f"game launch refused: {error}", file=sys.stderr)
        raise SystemExit(2)
