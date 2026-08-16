#!/usr/bin/env python3
"""Launch the sealed Playable Home Linux executable from a pinned profile."""

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
from typing import Any, Callable


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from tools.runtime.vista_playable_home import launch as preview_launch  # type: ignore
    from tools.runtime.vista_playable_home import packaged_profile  # type: ignore
    from tools.runtime.vista_playable_home.packaged_smoke import (  # type: ignore
        prove_loopback_listener_ownership,
    )
    from tools.runtime.vista_playable_home.runtime import (  # type: ignore
        RuntimeSafetyError,
        allocate_runtime_attempt,
        atomic_write_json,
        identity_is_live,
        process_identity,
        probe_typed_runtime,
        publish_current_runtime,
        runtime_root,
        utc_now,
        validate_display,
        validate_gpu,
        validate_vista_world_port,
    )
else:
    from . import launch as preview_launch
    from . import packaged_profile
    from .packaged_smoke import prove_loopback_listener_ownership
    from .runtime import (
        RuntimeSafetyError,
        allocate_runtime_attempt,
        atomic_write_json,
        identity_is_live,
        process_identity,
        probe_typed_runtime,
        publish_current_runtime,
        runtime_root,
        utc_now,
        validate_display,
        validate_gpu,
        validate_vista_world_port,
    )


PLAN_SCHEMA = "simworld.vista.playable-home-packaged-launch-plan/v1"
R2_PLAN_SCHEMA = "simworld.vista.playable-home-packaged-launch-plan/v2"
STATE_SCHEMA = "simworld.vista.playable-home-runtime-state/v1"
R2_STATE_SCHEMA = "simworld.vista.playable-home-runtime-state/v2"
DEFAULT_READY_TIMEOUT_SECONDS = 480.0
TRUSTED_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
ReadinessWaiter = Callable[..., dict[str, Any]]
PopenFactory = Callable[..., subprocess.Popen[Any]]
ListenerProver = Callable[[int, int], dict[str, Any]]


def build_command(inputs: packaged_profile.PackagedProfileInputs) -> list[str]:
    """Return the only interactive package command accepted by this entrypoint."""

    package = inputs.package
    user_root = package.attempt_root / "interactive-user"
    command = [
        str(package.executable),
        "VistaPlayableHome",
        package.map_path,
        "-Windowed",
        "-ForceRes",
        f"-ResX={inputs.width}",
        f"-ResY={inputs.height}",
        f"-graphicsadapter={inputs.gpu}",
        f"-VistaWorldPort={inputs.vista_world_port}",
        "-NOSPLASH",
        "-NOSOUND",
        "-NoAnalytics",
        "-UDPMESSAGING_TRANSPORT_ENABLE=0",
        "-ini:Engine:[/Script/TcpMessaging.TcpMessagingSettings]:EnableTransport=False",
        "-ddc=InstalledNoZenLocalFallback",
        f"-ExecCmds=t.MaxFPS {inputs.fps}",
        "-SaveToUserDir",
        f"-UserDir={user_root / 'ue-user'}",
        f"-LocalDataCachePath={user_root / 'xdg-cache' / 'UnrealEngine' / 'DDC'}",
        "-stdout",
        "-FullStdOutLogOutput",
        "-log",
    ]
    if inputs.camera_profile is not None:
        command.insert(9, f"-VistaCameraProfile={inputs.camera_profile}")
    return command


def sanitized_environment(
    inputs: packaged_profile.PackagedProfileInputs,
) -> dict[str, str]:
    user_root = inputs.package.attempt_root / "interactive-user"
    environment = {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": TRUSTED_PATH,
        "HOME": str(user_root / "home"),
        "XDG_CACHE_HOME": str(user_root / "xdg-cache"),
        "XDG_CONFIG_HOME": str(user_root / "xdg-config"),
        "DISPLAY": inputs.display,
        "SDL_VIDEODRIVER": "x11",
        "VK_ICD_FILENAMES": str(inputs.nvidia_icd),
        "VISTA_RUNTIME_GPU": str(inputs.gpu),
    }
    if inputs.runtime_profile is not None:
        environment.update(
            {
                "TMPDIR": str(user_root / "tmp"),
                "TMP": str(user_root / "tmp"),
                "TEMP": str(user_root / "tmp"),
                "XDG_DATA_HOME": str(user_root / "xdg-data"),
                "VISTA_RUNTIME_PROFILE": inputs.runtime_profile,
                "VISTA_CAMERA_PROFILE": inputs.camera_profile or "",
            }
        )
    return environment


def _prepare_user_directories(inputs: packaged_profile.PackagedProfileInputs) -> None:
    user_root = inputs.package.attempt_root / "interactive-user"
    if user_root.is_symlink():
        raise RuntimeSafetyError("interactive user root must not be a symlink")
    user_root.mkdir(mode=0o700, exist_ok=True)
    if user_root.resolve(strict=True) != user_root or not user_root.is_dir():
        raise RuntimeSafetyError("interactive user root identity differs")
    for relative in (
        Path("home"),
        Path("ue-user"),
        Path("xdg-cache"),
        Path("xdg-config"),
        Path("xdg-cache/UnrealEngine/DDC"),
        *(
            (Path("tmp"), Path("xdg-data"))
            if inputs.runtime_profile is not None
            else ()
        ),
    ):
        target = user_root / relative
        if target.is_symlink():
            raise RuntimeSafetyError("interactive user directory must not be a symlink")
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        if target.resolve(strict=True) != target or not target.is_dir():
            raise RuntimeSafetyError("interactive user directory identity differs")


def launch_plan(inputs: packaged_profile.PackagedProfileInputs) -> dict[str, Any]:
    package = inputs.package
    plan = {
        "schema": R2_PLAN_SCHEMA if inputs.runtime_profile is not None else PLAN_SCHEMA,
        "created_at": utc_now(),
        "mode": (
            packaged_profile.R2_PROFILE_MODE
            if inputs.runtime_profile is not None
            else packaged_profile.PROFILE_MODE
        ),
        "profile": {
            "path": str(inputs.profile),
            "sha256": inputs.profile_sha256,
        },
        "package": {
            "attempt_root": str(package.attempt_root),
            "receipt": str(package.receipt),
            "receipt_sha256": package.receipt_sha256,
            "archive_tree_sha256": package.archive_tree_sha256,
            "executable": str(package.executable),
            "executable_sha256": package.executable_sha256,
            "pak": str(package.pak),
            "pak_sha256": package.pak_sha256,
            "trusted_engine_root": str(package.trusted_engine_root),
            "unreal_pak": str(package.unreal_pak),
            "unreal_pak_sha256": package.unreal_pak_sha256,
            "map": package.map_path,
            "world_revision": package.world_revision,
        },
        "runtime": {
            "display": inputs.display,
            "gpu": inputs.gpu,
            "vista_world_port": inputs.vista_world_port,
            "width": inputs.width,
            "height": inputs.height,
            "fps": inputs.fps,
            "nvidia_icd": str(inputs.nvidia_icd),
            "nvidia_icd_sha256": inputs.nvidia_icd_sha256,
        },
        "command": build_command(inputs),
        "security": {
            "unreal_editor": False,
            "uproject": False,
            "game_flag": False,
            "arbitrary_command": False,
            "archive_rehash_before_spawn": True,
            "archive_rehash_after_readiness": True,
            "loopback_listener_process_group_proof": True,
        },
    }
    if inputs.runtime_profile is not None:
        plan["runtime"].update(
            {
                "runtime_profile": inputs.runtime_profile,
                "camera_profile": inputs.camera_profile,
            }
        )
    return plan


def wait_for_readiness(
    process: subprocess.Popen[Any],
    *,
    stop_requested: Callable[[], bool],
    timeout_seconds: float = DEFAULT_READY_TIMEOUT_SECONDS,
    port: int = packaged_profile.EXPECTED_PORT,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_error: RuntimeSafetyError | None = None
    while time.monotonic() < deadline:
        if stop_requested():
            raise RuntimeSafetyError("packaged launch was cancelled before readiness")
        if process.poll() is not None:
            raise RuntimeSafetyError("packaged executable exited before typed readiness")
        try:
            return probe_typed_runtime(
                port,
                expected_revision=packaged_profile.EXPECTED_WORLD_REVISION,
                timeout=1.0,
            )
        except RuntimeSafetyError as error:
            last_error = error
            time.sleep(0.5)
    failure = RuntimeSafetyError("packaged executable did not become ready")
    failure.__cause__ = last_error
    raise failure


def _open_private_log(path: Path) -> Any:
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


def run_packaged(
    inputs: packaged_profile.PackagedProfileInputs,
    *,
    stop_requested: Callable[[], bool],
    popen_factory: PopenFactory = subprocess.Popen,
    readiness_waiter: ReadinessWaiter = wait_for_readiness,
    listener_prover: ListenerProver = prove_loopback_listener_ownership,
) -> int:
    validate_display(inputs.display)
    validate_gpu(inputs.gpu)
    validate_vista_world_port(inputs.vista_world_port)
    _prepare_user_directories(inputs)

    runtime_root_path = runtime_root(inputs.package.attempt_root)
    lock_descriptor = os.open(
        runtime_root_path / ".launch.lock",
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(lock_descriptor)
        raise RuntimeSafetyError("another VISTA World launch is in progress") from exc

    process: subprocess.Popen[Any] | None = None
    log_handle = None
    state_path: Path | None = None
    state: dict[str, Any] | None = None
    try:
        runtime_dir = allocate_runtime_attempt(inputs.package.attempt_root)
        launch_plan_path = runtime_dir / "launch-plan.json"
        atomic_write_json(launch_plan_path, launch_plan(inputs))
        launch_plan_sha256 = hashlib.sha256(launch_plan_path.read_bytes()).hexdigest()
        log_handle = _open_private_log(runtime_dir / "packaged-game.log")
        if stop_requested():
            raise RuntimeSafetyError("packaged launch was cancelled")
        packaged_profile.revalidate_package(inputs.package)
        process = popen_factory(
            build_command(inputs),
            cwd=inputs.package.archive_root,
            env=sanitized_environment(inputs),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        identity = process_identity(process.pid, "packaged-game")
        if identity["process_group"] != process.pid:
            raise RuntimeSafetyError("packaged game did not create an owned process group")
        supervisor = process_identity(os.getpid(), "vista-world-packaged-supervisor")
        state_path = runtime_dir / "runtime-state.json"
        state = {
            "schema": R2_STATE_SCHEMA if inputs.runtime_profile is not None else STATE_SCHEMA,
            "status": "starting",
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "mode": (
                packaged_profile.R2_PROFILE_MODE
                if inputs.runtime_profile is not None
                else packaged_profile.PROFILE_MODE
            ),
            "map": inputs.package.map_path,
            "world_revision": inputs.package.world_revision,
            "display": inputs.display,
            "gpu": inputs.gpu,
            "vista_world_port": inputs.vista_world_port,
            "profile": str(inputs.profile),
            "profile_sha256": inputs.profile_sha256,
            "package_receipt": str(inputs.package.receipt),
            "package_receipt_sha256": inputs.package.receipt_sha256,
            "archive_tree_sha256": inputs.package.archive_tree_sha256,
            "executable": str(inputs.package.executable),
            "executable_sha256": inputs.package.executable_sha256,
            "trusted_engine_root": str(inputs.package.trusted_engine_root),
            "unreal_pak": str(inputs.package.unreal_pak),
            "unreal_pak_sha256": inputs.package.unreal_pak_sha256,
            "nvidia_icd": str(inputs.nvidia_icd),
            "nvidia_icd_sha256": inputs.nvidia_icd_sha256,
            "process": identity,
            "supervisor": supervisor,
        }
        if inputs.runtime_profile is not None:
            state.update(
                {
                    "runtime_profile": inputs.runtime_profile,
                    "camera_profile": inputs.camera_profile,
                    "width": inputs.width,
                    "height": inputs.height,
                    "fps": inputs.fps,
                    "launch_plan_sha256": launch_plan_sha256,
                }
            )
        atomic_write_json(state_path, state)
        publish_current_runtime(inputs.package.attempt_root, state_path)
    except BaseException:
        if process is not None:
            preview_launch.terminate_owned_process(process)
        if log_handle is not None:
            log_handle.close()
        raise
    finally:
        fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        os.close(lock_descriptor)

    assert process is not None and log_handle is not None and state is not None
    assert state_path is not None
    try:
        if readiness_waiter is wait_for_readiness:
            typed_readiness = readiness_waiter(
                process,
                stop_requested=stop_requested,
                port=inputs.vista_world_port,
            )
        else:
            typed_readiness = readiness_waiter(
                process,
                stop_requested=stop_requested,
            )
        packaged_profile.revalidate_package(inputs.package)
        if process.poll() is not None:
            raise RuntimeSafetyError("packaged executable exited during archive re-hash")
        listener_ownership = listener_prover(
            inputs.vista_world_port,
            process.pid,
        )
    except BaseException as error:
        preview_launch.terminate_owned_process(process)
        state.update(
            status="failed",
            updated_at=utc_now(),
            stopped_at=utc_now(),
            exit_code=process.returncode,
            failure=type(error).__name__,
        )
        atomic_write_json(state_path, state)
        log_handle.close()
        raise

    state.update(
        status="running",
        updated_at=utc_now(),
        readiness={
            "typed": typed_readiness,
            "listener_ownership": listener_ownership,
        },
        archive_reverified_after_readiness=True,
    )
    atomic_write_json(state_path, state)
    print(
        json.dumps(
            {"status": "running", "state": str(state_path), "pid": process.pid},
            sort_keys=True,
        ),
        flush=True,
    )
    while process.poll() is None and not stop_requested():
        time.sleep(0.5)
    requested = stop_requested()
    if requested and identity_is_live(state["process"]):
        preview_launch.terminate_owned_process(process, timeout=15.0)
    exit_code = process.wait()
    state.update(
        status="stopped" if requested or exit_code == 0 else "failed",
        stopped_at=utc_now(),
        updated_at=utc_now(),
        exit_code=exit_code,
    )
    atomic_write_json(state_path, state)
    log_handle.close()
    return 0 if requested else exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--profile-sha256", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Structural/profile/receipt pins are checked here.  ``run_packaged``
    # performs the full 1+ GiB archive re-hash immediately before spawn and
    # again after typed readiness, avoiding a redundant third NAS pass.
    inputs = packaged_profile.load_profile(
        args.profile,
        args.profile_sha256,
        verify_archive=False,
    )
    stopping = False

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    return run_packaged(inputs, stop_requested=lambda: stopping)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        packaged_profile.PackagedProfileError,
        RuntimeSafetyError,
        FileExistsError,
        OSError,
    ) as error:
        print(f"packaged launch refused: {error}", file=sys.stderr)
        raise SystemExit(2)
