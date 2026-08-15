#!/usr/bin/env python3
"""Read-only host and remote-play preflight for VISTA Playable Home."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from tools.runtime.vista_playable_home.runtime import (  # type: ignore
        DEFAULT_DISPLAY,
        PREFLIGHT_SCHEMA,
        RESERVED_GPU_INDICES,
        RuntimeSafetyError,
        command_result,
        inspect_toolchain,
        utc_now,
        validate_display,
    )
else:
    from .runtime import (
        DEFAULT_DISPLAY,
        PREFLIGHT_SCHEMA,
        RESERVED_GPU_INDICES,
        RuntimeSafetyError,
        command_result,
        inspect_toolchain,
        utc_now,
        validate_display,
    )


def device_access(path: Path) -> dict[str, Any]:
    exists = path.exists()
    return {
        "path": str(path),
        "exists": exists,
        "readable": exists and os.access(path, os.R_OK),
        "writable": exists and os.access(path, os.W_OK),
        "ready": exists and os.access(path, os.R_OK | os.W_OK),
    }


def display_access(display: str) -> dict[str, Any]:
    number = int(validate_display(display)[1:])
    socket_path = Path(f"/tmp/.X11-unix/X{number}")
    env = {**os.environ, "DISPLAY": display}
    executable = shutil.which("xdpyinfo")
    result: dict[str, Any] = {
        "display": display,
        "socket": str(socket_path),
        "socket_exists": socket_path.exists(),
        "xdpyinfo_available": bool(executable),
    }
    if executable:
        try:
            import subprocess

            probe = subprocess.run(
                [executable],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
                check=False,
            )
            result["connectable"] = probe.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            result["connectable"] = False
    else:
        result["connectable"] = socket_path.exists()
    return result


def sunshine_inspection(config_root: Path) -> dict[str, Any]:
    apps_path = config_root / "apps.json"
    conf_path = config_root / "sunshine.conf"
    result: dict[str, Any] = {
        "binary": shutil.which("sunshine"),
        "config_root": str(config_root),
        "apps_path": str(apps_path),
        "config_path": str(conf_path),
        "apps_readable": apps_path.is_file() and os.access(apps_path, os.R_OK),
        "configuration_readable": conf_path.is_file() and os.access(conf_path, os.R_OK),
        "vista_world_registered": False,
    }
    if result["apps_readable"]:
        try:
            payload = json.loads(apps_path.read_text(encoding="utf-8"))
            apps = payload.get("apps", []) if isinstance(payload, dict) else []
            result["vista_world_registered"] = any(
                isinstance(item, dict) and item.get("name") == "VISTA World"
                for item in apps
            )
        except (OSError, json.JSONDecodeError):
            result["apps_invalid"] = True
    return result


def tailscale_inspection() -> dict[str, Any]:
    status = command_result(["tailscale", "status", "--json"], timeout=5)
    netcheck = command_result(["tailscale", "netcheck"], timeout=10)
    result: dict[str, Any] = {
        "binary": shutil.which("tailscale"),
        "status_returncode": status.get("returncode"),
        "netcheck_returncode": netcheck.get("returncode"),
        "backend_state": None,
        "self_addresses": [],
        "udp": None,
        "nearest_derp": None,
    }
    output = status.get("output")
    if isinstance(output, str):
        try:
            payload = json.loads(output)
            result["backend_state"] = payload.get("BackendState")
            self_node = payload.get("Self") if isinstance(payload, dict) else None
            if isinstance(self_node, dict):
                result["self_addresses"] = self_node.get("TailscaleIPs", [])
        except json.JSONDecodeError:
            result["status_invalid"] = True
    net_output = netcheck.get("output")
    if isinstance(net_output, str):
        for raw in net_output.splitlines():
            line = raw.strip()
            if line.startswith("* UDP:"):
                result["udp"] = line.split(":", 1)[1].strip().lower() == "true"
            elif line.startswith("* Nearest DERP:"):
                result["nearest_derp"] = line.split(":", 1)[1].strip()
    return result


def nvidia_inspection() -> dict[str, Any]:
    probe = command_result(
        ["nvidia-smi", "--query-gpu=index,name,memory.total,memory.used", "--format=csv,noheader,nounits"],
        timeout=5,
    )
    rows: list[dict[str, Any]] = []
    output = probe.get("output")
    if isinstance(output, str) and probe.get("returncode") == 0:
        for line in output.splitlines():
            fields = [field.strip() for field in line.split(",")]
            if len(fields) != 4:
                continue
            try:
                index = int(fields[0])
                total = int(fields[2])
                used = int(fields[3])
            except ValueError:
                continue
            rows.append(
                {
                    "index": index,
                    "name": fields[1],
                    "memory_total_mib": total,
                    "memory_used_mib": used,
                    "reserved": index in RESERVED_GPU_INDICES,
                }
            )
    return {
        "binary": shutil.which("nvidia-smi"),
        "returncode": probe.get("returncode"),
        "gpus": rows,
    }


def listener(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def build_report(
    *,
    ue_editor: Path,
    display: str,
    sunshine_config: Path,
    sunshine_host: str,
    sunshine_port: int,
) -> dict[str, Any]:
    toolchain = inspect_toolchain(ue_editor)
    input_devices = {
        "uinput": device_access(Path("/dev/uinput")),
        "uhid": device_access(Path("/dev/uhid")),
    }
    display_report = display_access(display)
    sunshine = sunshine_inspection(sunshine_config)
    sunshine["listener"] = {
        "host": sunshine_host,
        "port": sunshine_port,
        "reachable": listener(sunshine_host, sunshine_port),
    }
    tailscale = tailscale_inspection()
    nvidia = nvidia_inspection()
    blockers: list[str] = []
    if not display_report.get("connectable"):
        blockers.append("capture_display_unavailable")
    if not sunshine.get("binary") or not sunshine["listener"]["reachable"]:
        blockers.append("sunshine_unavailable")
    if not all(item["ready"] for item in input_devices.values()):
        blockers.append("moonlight_input_view_only")
    if not toolchain.get("cook_ready"):
        blockers.append("full_ue_build_toolchain_missing")
    if tailscale.get("backend_state") != "Running":
        blockers.append("tailscale_not_running")
    if not any(not gpu.get("reserved") for gpu in nvidia.get("gpus", [])):
        blockers.append("no_unreserved_gpu_detected")
    return {
        "schema": PREFLIGHT_SCHEMA,
        "created_at": utc_now(),
        "mode": "read_only",
        "display": display_report,
        "sunshine": sunshine,
        "tailscale": tailscale,
        "input_devices": input_devices,
        "nvidia": nvidia,
        "toolchain": toolchain,
        "blockers": blockers,
        "preview_ready": not any(
            item in blockers
            for item in (
                "capture_display_unavailable",
                "sunshine_unavailable",
                "tailscale_not_running",
                "no_unreserved_gpu_detected",
            )
        ),
        "moonlight_control_ready": "moonlight_input_view_only" not in blockers,
        "packaged_build_ready": toolchain.get("cook_ready", False),
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--ue-editor", required=True, type=Path)
    result.add_argument("--display", default=DEFAULT_DISPLAY)
    result.add_argument(
        "--sunshine-config", type=Path, default=Path.home() / ".config" / "sunshine"
    )
    result.add_argument("--sunshine-host", default="100.114.80.121")
    result.add_argument("--sunshine-port", type=int, default=47989)
    result.add_argument("--output", type=Path)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    report = build_report(
        ue_editor=args.ue_editor,
        display=args.display,
        sunshine_config=args.sunshine_config,
        sunshine_host=args.sunshine_host,
        sunshine_port=args.sunshine_port,
    )
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeSafetyError as error:
        print(f"preflight refused: {error}", file=sys.stderr)
        raise SystemExit(2)
