#!/usr/bin/env python3
"""Stop only the process identities recorded by one VISTA Blender world launch."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from tools.runtime.vista_blender_world.runtime import (  # type: ignore
        RuntimeSafetyError,
        identity_is_live,
        load_runtime_state,
        pid_entries,
        process_start_ticks,
        utc_now,
        write_state,
    )
else:
    from .runtime import (
        RuntimeSafetyError,
        identity_is_live,
        load_runtime_state,
        pid_entries,
        process_start_ticks,
        utc_now,
        write_state,
    )


def signal_identity(identity: Mapping[str, Any], signum: int) -> bool:
    if not identity_is_live(identity):
        return False
    pid = int(identity["pid"])
    expected = int(identity["start_ticks"])
    if process_start_ticks(pid) != expected:
        return False
    try:
        expected_group = identity.get("process_group")
        current_group = os.getpgid(pid)
        if expected_group is not None and current_group != int(expected_group):
            return False
        if current_group == pid:
            os.killpg(pid, signum)
        else:
            os.kill(pid, signum)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def stop(workspace: Path, *, timeout_seconds: float = 15.0) -> dict[str, Any]:
    state_path, state = load_runtime_state(workspace)
    launcher = state.get("launcher")
    if not isinstance(launcher, Mapping):
        raise RuntimeSafetyError("runtime state has no launcher identity")
    actions: list[dict[str, Any]] = []
    if signal_identity(launcher, signal.SIGTERM):
        actions.append({"role": "launcher", "signal": "SIGTERM"})
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline and identity_is_live(launcher):
            time.sleep(0.25)
    if identity_is_live(launcher):
        raise RuntimeSafetyError("launcher did not stop within the bounded timeout")
    for identity in reversed(list(pid_entries(state))):
        role = str(identity.get("role", "unknown"))
        if signal_identity(identity, signal.SIGTERM):
            actions.append({"role": role, "signal": "SIGTERM"})
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if not any(identity_is_live(item) for item in pid_entries(state)):
            break
        time.sleep(0.25)
    for identity in reversed(list(pid_entries(state))):
        role = str(identity.get("role", "unknown"))
        if signal_identity(identity, signal.SIGKILL):
            actions.append({"role": role, "signal": "SIGKILL"})
    final_deadline = time.monotonic() + 3
    while time.monotonic() < final_deadline:
        if not any(identity_is_live(item) for item in pid_entries(state)):
            break
        time.sleep(0.1)
    lingering = [
        str(item.get("role", "unknown"))
        for item in pid_entries(state)
        if identity_is_live(item)
    ]
    if lingering:
        raise RuntimeSafetyError(
            f"owned processes remain live after scoped cleanup: {', '.join(lingering)}"
        )
    state["status"] = "stopped"
    state["stopped_at"] = utc_now()
    state["updated_at"] = utc_now()
    write_state(state_path, state)
    return {
        "workspace": str(workspace.resolve(strict=True)),
        "actions": actions,
        "status": "stopped",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args(argv)
    print(
        json.dumps(
            stop(args.workspace, timeout_seconds=args.timeout), indent=2, sort_keys=True
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeSafetyError as error:
        print(f"runtime stop refused: {error}", file=sys.stderr)
        raise SystemExit(2)
