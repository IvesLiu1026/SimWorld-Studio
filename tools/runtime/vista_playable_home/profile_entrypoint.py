#!/usr/bin/env python3
"""Launch VISTA World from a closed JSON profile used by Sunshine."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from tools.runtime.vista_playable_home import launch  # type: ignore
else:
    from . import launch

ALLOWED_FIELDS = {
    "workspace",
    "project",
    "ue_editor",
    "map",
    "display",
    "gpu",
    "width",
    "height",
    "fps",
    "nvidia_icd",
    "nvidia_compat",
}


def load_profile(path: Path) -> list[str]:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError("profile must be an absolute regular non-symlink file")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or set(payload) - ALLOWED_FIELDS:
        raise ValueError("profile contains unknown fields")
    required = {"workspace", "project", "ue_editor", "map"}
    if not required.issubset(payload):
        raise ValueError("profile is missing required fields")
    arguments: list[str] = []
    for field in (
        "workspace",
        "project",
        "ue_editor",
        "map",
        "display",
        "gpu",
        "width",
        "height",
        "fps",
        "nvidia_icd",
        "nvidia_compat",
    ):
        if field in payload and payload[field] is not None:
            arguments.extend([f"--{field.replace('_', '-')}", str(payload[field])])
    return arguments


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, type=Path)
    args = parser.parse_args(argv)
    return launch.main(load_profile(args.profile))


if __name__ == "__main__":
    raise SystemExit(main())
