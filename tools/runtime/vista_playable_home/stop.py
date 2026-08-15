#!/usr/bin/env python3
"""Stop only the Unreal game process bound in one playable-home state file."""

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
    from tools.runtime.vista_playable_home.runtime import (  # type: ignore
        RuntimeSafetyError,
        atomic_write_json,
        identity_is_live,
        process_start_ticks,
        resolve_current_runtime_state,
        utc_now,
    )
else:
    from .runtime import (
        RuntimeSafetyError,
        atomic_write_json,
        identity_is_live,
        process_start_ticks,
        resolve_current_runtime_state,
        utc_now,
    )


def load_state(workspace: Path) -> tuple[Path, dict[str, Any]]:
    return resolve_current_runtime_state(workspace)


def signal_owned(identity: Mapping[str, Any], signum: int) -> bool:
    if not identity_is_live(identity):
        return False
    try:
        pid = int(identity["pid"])
        expected_ticks = int(identity["start_ticks"])
        expected_group = int(identity["process_group"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeSafetyError("runtime process identity is incomplete") from exc
    if process_start_ticks(pid) != expected_ticks or os.getpgid(pid) != expected_group:
        return False
    try:
        os.killpg(expected_group, signum)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def signal_owned_process(identity: Mapping[str, Any], signum: int) -> bool:
    if not identity_is_live(identity):
        return False
    try:
        pid = int(identity["pid"])
        expected_ticks = int(identity["start_ticks"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeSafetyError("supervisor process identity is incomplete") from exc
    if process_start_ticks(pid) != expected_ticks:
        return False
    try:
        os.kill(pid, signum)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def stop(workspace: Path, timeout: float = 15.0) -> dict[str, Any]:
    state_path, state = load_state(workspace)
    identity = state.get("process")
    if not isinstance(identity, Mapping):
        raise RuntimeSafetyError("runtime state has no process identity")
    actions: list[str] = []
    supervisor = state.get("supervisor")
    if isinstance(supervisor, Mapping) and identity_is_live(supervisor):
        state.update(status="stop_requested", updated_at=utc_now())
        atomic_write_json(state_path, state)
        if signal_owned_process(supervisor, signal.SIGTERM):
            actions.append("SUPERVISOR_SIGTERM")
        elif signal_owned(identity, signal.SIGTERM):
            actions.append("UE_SIGTERM_FALLBACK")
    elif signal_owned(identity, signal.SIGTERM):
        actions.append("UE_SIGTERM")
    deadline = time.monotonic() + timeout
    while identity_is_live(identity) and time.monotonic() < deadline:
        time.sleep(0.2)
    if identity_is_live(identity):
        if signal_owned(identity, signal.SIGKILL):
            actions.append("SIGKILL")
        deadline = time.monotonic() + 3
        while identity_is_live(identity) and time.monotonic() < deadline:
            time.sleep(0.1)
    if identity_is_live(identity):
        raise RuntimeSafetyError("owned process remains live after bounded stop")
    state.update(status="stopped", stopped_at=utc_now(), updated_at=utc_now())
    atomic_write_json(state_path, state)
    return {"status": "stopped", "actions": actions, "state": str(state_path)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args(argv)
    print(json.dumps(stop(args.workspace, args.timeout), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeSafetyError as error:
        print(f"game stop refused: {error}", file=sys.stderr)
        raise SystemExit(2)
