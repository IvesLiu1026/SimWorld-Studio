#!/usr/bin/env python3
"""Plan or install one deterministic Sunshine application for VISTA World."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

APP_NAME = "VISTA World"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class SunshineConfigError(RuntimeError):
    """Raised before an unsafe or ambiguous Sunshine config change."""


def load_apps(path: Path) -> dict[str, Any]:
    if not path.is_absolute():
        raise SunshineConfigError("apps path must be absolute")
    if path.is_symlink() or not path.is_file():
        raise SunshineConfigError("apps path must be a regular non-symlink file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SunshineConfigError("apps file is invalid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("apps"), list):
        raise SunshineConfigError("apps file must contain an apps array")
    return payload


def build_entry(
    *,
    python: Path,
    launcher: Path,
    profile: Path,
    working_dir: Path,
    profile_sha256: str | None = None,
    exit_timeout: int = 20,
) -> dict[str, Any]:
    for value, label in (
        (python, "python"),
        (launcher, "launcher"),
        (profile, "profile"),
        (working_dir, "working directory"),
    ):
        if not value.is_absolute():
            raise SunshineConfigError(f"{label} path must be absolute")
    if profile_sha256 is not None and SHA256_RE.fullmatch(profile_sha256) is None:
        raise SunshineConfigError("profile SHA-256 must be lowercase hexadecimal")
    if (
        isinstance(exit_timeout, bool)
        or not isinstance(exit_timeout, int)
        or not 5 <= exit_timeout <= 300
    ):
        raise SunshineConfigError("exit timeout must be an integer from 5 through 300")
    arguments = [str(python), str(launcher), "--profile", str(profile)]
    if profile_sha256 is not None:
        arguments.extend(["--profile-sha256", profile_sha256])
    command = shlex.join(arguments)
    return {
        "name": APP_NAME,
        "cmd": command,
        "working-dir": str(working_dir),
        "image-path": "desktop.png",
        "auto-detach": "false",
        "wait-all": "true",
        "exit-timeout": str(exit_timeout),
    }


def merge_entry(payload: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    apps = payload.get("apps")
    if not isinstance(apps, list):
        raise SunshineConfigError("apps file must contain an apps array")
    retained = [
        item
        for item in apps
        if not (isinstance(item, dict) and item.get("name") == APP_NAME)
    ]
    return {**payload, "apps": [*retained, entry]}


def install(path: Path, payload: dict[str, Any]) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(path.name + f".vista-backup-{timestamp}")
    if backup.exists():
        raise SunshineConfigError(f"backup already exists: {backup}")
    shutil.copy2(path, backup)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        json.loads(temporary.read_text(encoding="utf-8"))
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return backup


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--apps", type=Path, default=Path.home() / ".config" / "sunshine" / "apps.json"
    )
    result.add_argument("--python", required=True, type=Path)
    result.add_argument("--launcher", required=True, type=Path)
    result.add_argument("--profile", required=True, type=Path)
    result.add_argument("--profile-sha256")
    result.add_argument("--exit-timeout", type=int, default=20)
    result.add_argument("--working-dir", required=True, type=Path)
    result.add_argument("--apply", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    current = load_apps(args.apps)
    entry = build_entry(
        python=args.python,
        launcher=args.launcher,
        profile=args.profile,
        working_dir=args.working_dir,
        profile_sha256=args.profile_sha256,
        exit_timeout=args.exit_timeout,
    )
    merged = merge_entry(current, entry)
    result: dict[str, Any] = {
        "status": "planned",
        "apps": str(args.apps),
        "entry": entry,
        "apply": args.apply,
    }
    if args.apply:
        result["backup"] = str(install(args.apps, merged))
        result["status"] = "installed_restart_required"
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SunshineConfigError as error:
        print(f"Sunshine app refused: {error}", file=sys.stderr)
        raise SystemExit(2)
