#!/usr/bin/env python3
"""Write one evidence-bound Sunshine profile from an exact runtime launch plan."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shlex
import stat
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from tools.runtime.vista_playable_home.runtime import (  # type: ignore
        RESERVED_GPU_INDICES,
        RESERVED_PORTS,
        R2_CAMERA_PROFILE,
        R2_RUNTIME_PROFILE,
        R2_SCHEMA,
        SCHEMA,
        GameRuntimeConfig,
        RuntimeSafetyError,
        build_game_command,
        validate_dimensions,
        validate_display,
        validate_gpu,
        validate_map,
        validate_runtime_profile_binding,
    )
else:
    from .runtime import (
        RESERVED_GPU_INDICES,
        RESERVED_PORTS,
        R2_CAMERA_PROFILE,
        R2_RUNTIME_PROFILE,
        R2_SCHEMA,
        SCHEMA,
        GameRuntimeConfig,
        RuntimeSafetyError,
        build_game_command,
        validate_dimensions,
        validate_display,
        validate_gpu,
        validate_map,
        validate_runtime_profile_binding,
    )


PLAN_MODE = "unreal-editor-game-preview"
R2_PLAN_MODE = "unreal-editor-game-preview-realistic"
MAX_JSON_BYTES = 64 * 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
OUTPUT_RE = re.compile(
    r"^sunshine-profile(?:-[A-Za-z0-9][A-Za-z0-9_-]{0,63})?\.json$"
)

PLAN_KEYS = frozenset(
    {
        "schema",
        "created_at",
        "mode",
        "config",
        "command",
        "command_shell_preview",
        "security",
    }
)
PLAN_CONFIG_KEYS = frozenset(
    {
        "workspace",
        "project",
        "ue_editor",
        "map_path",
        "display",
        "gpu",
        "vista_world_port",
        "width",
        "height",
        "fps",
        "title",
        "nvidia_icd",
        "nvidia_compat",
    }
)
R2_PLAN_CONFIG_KEYS = PLAN_CONFIG_KEYS | frozenset(
    {"runtime_profile", "camera_profile"}
)
PLAN_SECURITY = {
    "editor_chrome": False,
    "render_offscreen": False,
    "reserved_gpu_indices": sorted(RESERVED_GPU_INDICES),
    "reserved_ports": sorted(RESERVED_PORTS),
    "arbitrary_command": False,
}
R2_PLAN_SECURITY = {
    **PLAN_SECURITY,
    "runtime_profile_closed": True,
    "camera_profile_closed": True,
}

PROFILE_REQUIRED_FIELDS = frozenset({"workspace", "project", "ue_editor", "map"})
PROFILE_OPTIONAL_FIELDS = frozenset(
    {
        "display",
        "gpu",
        "vista_world_port",
        "width",
        "height",
        "fps",
        "nvidia_icd",
        "nvidia_compat",
    }
)
PROFILE_FIELDS = PROFILE_REQUIRED_FIELDS | PROFILE_OPTIONAL_FIELDS
R2_PROFILE_FIELDS = PROFILE_FIELDS | frozenset(
    {"runtime_profile", "camera_profile"}
)
PROFILE_PATH_FIELDS = frozenset(
    {"workspace", "project", "ue_editor", "nvidia_icd", "nvidia_compat"}
)
PROFILE_INTEGER_FIELDS = frozenset(
    {"gpu", "vista_world_port", "width", "height", "fps"}
)


class ProfileError(RuntimeError):
    """Raised before an unbound or ambiguous profile can be published."""


@dataclass(frozen=True)
class ValidatedLaunchPlan:
    path: Path
    sha256: str
    workspace: Path
    config: GameRuntimeConfig


@dataclass(frozen=True)
class ProfileWriteResult:
    source_path: Path
    source_sha256: str
    output_path: Path
    profile_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "written",
            "source": {
                "path": str(self.source_path),
                "sha256": self.source_sha256,
            },
            "output": str(self.output_path),
            "profile_sha256": self.profile_sha256,
        }


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProfileError("profile is not finite JSON") from exc


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_nonfinite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite JSON number")
    if isinstance(value, list):
        for item in value:
            _reject_nonfinite(item)
    elif isinstance(value, dict):
        for item in value.values():
            _reject_nonfinite(item)


def strict_json_bytes(raw: bytes, *, label: str) -> Any:
    if not raw:
        raise ProfileError(f"{label} is empty")
    try:
        decoded = raw.decode("utf-8", errors="strict")
        value = json.loads(
            decoded,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        _reject_nonfinite(value)
        return value
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ProfileError(f"{label} is not strict JSON") from exc


def _absolute_lexical(path: Path, label: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise ProfileError(f"{label} must be an absolute canonical path")
    return candidate


def _canonical_file(path: Path, label: str, *, expected_name: str | None = None) -> Path:
    candidate = _absolute_lexical(path, label)
    if expected_name is not None and candidate.name != expected_name:
        raise ProfileError(f"{label} must be named {expected_name}")
    try:
        metadata = candidate.lstat()
    except (FileNotFoundError, OSError) as exc:
        raise ProfileError(f"{label} does not exist") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ProfileError(f"{label} must be a regular non-symlink file")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ProfileError(f"{label} identity could not be resolved") from exc
    if resolved != candidate:
        raise ProfileError(f"{label} must use its canonical file identity")
    return resolved


def _canonical_directory(path: Path, label: str) -> Path:
    candidate = _absolute_lexical(path, label)
    if candidate.is_symlink():
        raise ProfileError(f"{label} must not be a symlink")
    try:
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise ProfileError(f"{label} does not exist") from exc
    if resolved != candidate or not resolved.is_dir():
        raise ProfileError(f"{label} must use its canonical directory identity")
    return resolved


def _contained(path: Path, root: Path, label: str) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ProfileError(f"{label} must be contained by the plan workspace") from exc


def _read_pinned_json(path: Path, expected_sha256: str, label: str) -> tuple[Any, bytes]:
    if not isinstance(expected_sha256, str) or not SHA256_RE.fullmatch(expected_sha256):
        raise ProfileError(f"{label} SHA-256 must be lowercase hexadecimal")
    try:
        size = path.stat().st_size
        if size <= 0 or size > MAX_JSON_BYTES:
            raise ProfileError(f"{label} size is outside its bound")
        raw = path.read_bytes()
    except OSError as exc:
        raise ProfileError(f"could not read {label}") from exc
    if len(raw) != size:
        raise ProfileError(f"{label} changed while it was read")
    if sha256_bytes(raw) != expected_sha256:
        raise ProfileError(f"{label} SHA-256 differs")
    return strict_json_bytes(raw, label=label), raw


def _absolute_string(value: Any, label: str, *, optional: bool = False) -> Path | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ProfileError(f"{label} must be an absolute path string")
    return _absolute_lexical(Path(value), label)


def _validate_created_at(value: Any) -> None:
    if not isinstance(value, str) or not value:
        raise ProfileError("launch plan created_at is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProfileError("launch plan created_at is invalid") from exc
    if parsed.tzinfo is None:
        raise ProfileError("launch plan created_at must include a timezone")


def _config_from_plan(
    value: Any,
    *,
    r2: bool,
) -> tuple[GameRuntimeConfig, Path]:
    expected_keys = R2_PLAN_CONFIG_KEYS if r2 else PLAN_CONFIG_KEYS
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise ProfileError("launch plan config fields differ")
    if r2 and (
        value.get("runtime_profile") != R2_RUNTIME_PROFILE
        or value.get("camera_profile") != R2_CAMERA_PROFILE
    ):
        raise ProfileError("launch plan r2 profile binding differs")
    workspace_raw = _absolute_string(value.get("workspace"), "plan workspace")
    if workspace_raw is None:  # unreachable for a required field, kept fail-closed
        raise ProfileError("plan workspace is missing")
    workspace = _canonical_directory(workspace_raw, "plan workspace")
    project_raw = _absolute_string(value.get("project"), "plan project")
    ue_editor_raw = _absolute_string(value.get("ue_editor"), "plan Unreal Editor")
    nvidia_icd_raw = _absolute_string(
        value.get("nvidia_icd"), "plan NVIDIA ICD", optional=True
    )
    nvidia_compat_raw = _absolute_string(
        value.get("nvidia_compat"), "plan NVIDIA compatibility directory", optional=True
    )
    if project_raw is None or ue_editor_raw is None:  # unreachable for required fields
        raise ProfileError("launch plan executable paths are missing")
    project = _canonical_file(project_raw, "plan project")
    _contained(project, workspace, "plan project")
    ue_editor = _canonical_file(ue_editor_raw, "plan Unreal Editor")
    if (
        ue_editor.name != "UnrealEditor"
        or tuple(
            part.name
            for part in (
                ue_editor.parent,
                ue_editor.parent.parent,
                ue_editor.parent.parent.parent,
            )
        )
        != ("Linux", "Binaries", "Engine")
        or not os.access(ue_editor, os.X_OK)
    ):
        raise ProfileError(
            "plan Unreal Editor must be an executable Engine/Binaries/Linux/UnrealEditor"
        )
    nvidia_icd = (
        _canonical_file(nvidia_icd_raw, "plan NVIDIA ICD")
        if nvidia_icd_raw is not None
        else None
    )
    nvidia_compat = (
        _canonical_directory(nvidia_compat_raw, "plan NVIDIA compatibility directory")
        if nvidia_compat_raw is not None
        else None
    )
    if project.suffix != ".uproject":
        raise ProfileError("plan project must end in .uproject")
    if not isinstance(value.get("title"), str) or not 1 <= len(value["title"]) <= 80:
        raise ProfileError("launch plan title is invalid")
    if not all(
        _is_int(value.get(field))
        for field in ("gpu", "vista_world_port", "width", "height", "fps")
    ):
        raise ProfileError("launch plan integer fields differ")
    port = value["vista_world_port"]
    if not 1024 <= port <= 65535 or port in RESERVED_PORTS:
        raise ProfileError("launch plan VISTA World port is invalid or reserved")
    try:
        map_path = validate_map(value.get("map_path"))
        display = validate_display(value.get("display"))
        gpu = validate_gpu(value["gpu"])
        width, height, fps = validate_dimensions(
            value["width"], value["height"], value["fps"]
        )
    except RuntimeSafetyError as exc:
        raise ProfileError(f"launch plan config is invalid: {exc}") from exc
    config = GameRuntimeConfig(
            workspace=workspace,
            project=project,
            ue_editor=ue_editor,
            map_path=map_path,
            display=display,
            gpu=gpu,
            vista_world_port=port,
            width=width,
            height=height,
            fps=fps,
            title=value["title"],
            nvidia_icd=nvidia_icd,
            nvidia_compat=nvidia_compat,
            runtime_profile=R2_RUNTIME_PROFILE if r2 else None,
        )
    try:
        validate_runtime_profile_binding(config)
    except RuntimeSafetyError as exc:
        raise ProfileError(f"launch plan runtime profile is invalid: {exc}") from exc
    return (
        config,
        workspace,
    )


def validate_launch_plan(path: Path, expected_sha256: str) -> ValidatedLaunchPlan:
    plan_path = _canonical_file(path, "launch plan", expected_name="launch-plan.json")
    plan, _raw = _read_pinned_json(plan_path, expected_sha256, "launch plan")
    if not isinstance(plan, dict) or set(plan) != PLAN_KEYS:
        raise ProfileError("launch plan fields differ")
    schema = plan.get("schema")
    if schema == SCHEMA:
        r2 = False
        expected_mode = PLAN_MODE
        expected_security = PLAN_SECURITY
    elif schema == R2_SCHEMA:
        r2 = True
        expected_mode = R2_PLAN_MODE
        expected_security = R2_PLAN_SECURITY
    else:
        raise ProfileError("launch plan schema or mode differs")
    if plan.get("mode") != expected_mode:
        raise ProfileError("launch plan schema or mode differs")
    _validate_created_at(plan.get("created_at"))
    config, workspace = _config_from_plan(plan.get("config"), r2=r2)
    _contained(plan_path, workspace, "launch plan")
    expected_command = build_game_command(config)
    command = plan.get("command")
    if (
        not isinstance(command, list)
        or not all(isinstance(item, str) for item in command)
        or command != expected_command
        or plan.get("command_shell_preview") != shlex.join(expected_command)
    ):
        raise ProfileError("launch plan command/config binding differs")
    if plan.get("security") != expected_security:
        raise ProfileError("launch plan security contract differs")
    return ValidatedLaunchPlan(
        path=plan_path,
        sha256=expected_sha256,
        workspace=workspace,
        config=config,
    )


def profile_from_config(config: GameRuntimeConfig) -> dict[str, Any]:
    profile: dict[str, Any] = {
        "workspace": str(config.workspace),
        "project": str(config.project),
        "ue_editor": str(config.ue_editor),
        "map": config.map_path,
        "display": config.display,
        "gpu": config.gpu,
        "vista_world_port": config.vista_world_port,
        "width": config.width,
        "height": config.height,
        "fps": config.fps,
    }
    if config.nvidia_icd is not None:
        profile["nvidia_icd"] = str(config.nvidia_icd)
    if config.nvidia_compat is not None:
        profile["nvidia_compat"] = str(config.nvidia_compat)
    expected_fields = PROFILE_FIELDS
    if config.runtime_profile is not None:
        spec = validate_runtime_profile_binding(config)
        profile.update(
            {
                "runtime_profile": spec.runtime_profile,
                "camera_profile": spec.camera_profile,
            }
        )
        expected_fields = R2_PROFILE_FIELDS
    if (
        not PROFILE_REQUIRED_FIELDS.issubset(profile)
        or set(profile) - expected_fields
        or (
            config.runtime_profile is not None
            and not {"runtime_profile", "camera_profile"}.issubset(profile)
        )
    ):
        raise ProfileError("generated profile fields differ")
    return profile


def _validate_output(path: Path, workspace: Path) -> Path:
    output = _absolute_lexical(path, "profile output")
    if not OUTPUT_RE.fullmatch(output.name):
        raise ProfileError("profile output name is not accepted")
    try:
        parent = output.parent.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise ProfileError("profile output parent does not exist") from exc
    if parent != output.parent or parent != workspace:
        raise ProfileError("profile output must be a direct child of the plan workspace")
    return output


def write_profile(
    launch_plan: Path,
    expected_sha256: str,
    output: Path,
) -> ProfileWriteResult:
    validated = validate_launch_plan(launch_plan, expected_sha256)
    output_path = _validate_output(output, validated.workspace)
    raw = canonical_json_bytes(profile_from_config(validated.config))
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(output_path, flags, 0o600)
    except FileExistsError as exc:
        raise ProfileError("profile output already exists and will not be replaced") from exc
    except OSError as exc:
        raise ProfileError("could not create profile output") from exc
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        directory_descriptor = os.open(
            validated.workspace,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise ProfileError("could not commit profile output") from exc
    return ProfileWriteResult(
        source_path=validated.path,
        source_sha256=validated.sha256,
        output_path=output_path,
        profile_sha256=sha256_bytes(raw),
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--launch-plan", required=True, type=Path)
    result.add_argument("--launch-plan-sha256", required=True)
    result.add_argument("--output", required=True, type=Path)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    result = write_profile(
        args.launch_plan,
        args.launch_plan_sha256,
        args.output,
    )
    print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ProfileError as error:
        print(f"Sunshine profile refused: {error}", file=sys.stderr)
        raise SystemExit(2)
