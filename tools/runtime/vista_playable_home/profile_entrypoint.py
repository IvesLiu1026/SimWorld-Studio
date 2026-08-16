#!/usr/bin/env python3
"""Launch VISTA World from a closed JSON profile used by Sunshine."""

from __future__ import annotations

import argparse
import stat
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from tools.runtime.vista_playable_home import launch, profile as profile_contract  # type: ignore
else:
    from . import launch, profile as profile_contract

REQUIRED_FIELDS = profile_contract.PROFILE_REQUIRED_FIELDS
MAX_PROFILE_BYTES = profile_contract.MAX_JSON_BYTES


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _profile_path(path: Path) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("profile must be an absolute canonical path")
    try:
        metadata = candidate.lstat()
    except (FileNotFoundError, OSError) as exc:
        raise ValueError("profile does not exist") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("profile must be a regular non-symlink file")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ValueError("profile mode must be exactly 0600")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValueError("profile identity could not be resolved") from exc
    if resolved != candidate:
        raise ValueError("profile must use its canonical file identity")
    return resolved


def _validate_payload(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ValueError("profile must be a JSON object")
    has_runtime_profile = "runtime_profile" in payload
    has_camera_profile = "camera_profile" in payload
    r2 = has_runtime_profile or has_camera_profile
    allowed_fields = (
        profile_contract.R2_PROFILE_FIELDS
        if r2
        else profile_contract.PROFILE_FIELDS
    )
    unknown = set(payload) - allowed_fields
    if unknown:
        raise ValueError("profile contains unknown fields")
    if not REQUIRED_FIELDS.issubset(payload):
        raise ValueError("profile is missing required fields")
    if r2 and (
        not has_runtime_profile
        or not has_camera_profile
        or payload["runtime_profile"] != profile_contract.R2_RUNTIME_PROFILE
        or payload["camera_profile"] != profile_contract.R2_CAMERA_PROFILE
    ):
        raise ValueError("profile r2 runtime/camera binding differs")
    for field in profile_contract.PROFILE_PATH_FIELDS:
        if field not in payload:
            continue
        value = payload[field]
        if (
            not isinstance(value, str)
            or not value
            or "\x00" in value
            or not Path(value).is_absolute()
            or ".." in Path(value).parts
        ):
            raise ValueError(f"profile field {field} must be an absolute path string")
    for field in ("map", "display"):
        if field in payload and (
            not isinstance(payload[field], str)
            or not payload[field]
            or "\x00" in payload[field]
        ):
            raise ValueError(f"profile field {field} must be a non-empty string")
    for field in profile_contract.PROFILE_INTEGER_FIELDS:
        if field in payload and not _is_int(payload[field]):
            raise ValueError(f"profile field {field} must be an integer")
    return dict(payload)


def load_profile(path: Path) -> list[str]:
    profile_path = _profile_path(path)
    try:
        size = profile_path.stat().st_size
        if size <= 0 or size > MAX_PROFILE_BYTES:
            raise ValueError("profile size is outside its bound")
        raw = profile_path.read_bytes()
    except OSError as exc:
        raise ValueError("profile could not be read") from exc
    if len(raw) != size:
        raise ValueError("profile changed while it was read")
    try:
        payload = _validate_payload(
            profile_contract.strict_json_bytes(raw, label="profile")
        )
    except profile_contract.ProfileError as exc:
        raise ValueError(str(exc)) from exc
    arguments: list[str] = []
    for field in (
        "workspace",
        "project",
        "ue_editor",
        "map",
        "runtime_profile",
        "display",
        "gpu",
        "vista_world_port",
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
