"""Host-side pinning contract for the two VISTA home UE commandlets."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import pathlib
import re
from collections.abc import Mapping, Sequence
from typing import Any

from .planning import CompositionSpec, build_composition_spec, canonical_json


EXECUTION_SCHEMA = "simworld.vista.playable-home-ue-execution/v1"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
FORBIDDEN_PATH_PARTS = {
    "archive", "archives", "canonical", "production", "release", "releases",
    "r8", "disposable-project-r8",
}


class VistaPlayableHomeContractError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclasses.dataclass(frozen=True)
class ExecutionManifest:
    value: dict[str, Any]
    raw: bytes
    sha256: str
    composition: CompositionSpec


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _error(code: str, detail: str) -> None:
    raise VistaPlayableHomeContractError(code, detail)


def _canonical_path(path: os.PathLike[str] | str) -> pathlib.Path:
    return pathlib.Path(path).expanduser().resolve(strict=False)


def _safe_attempt_child(path: pathlib.Path, attempt_root: pathlib.Path, label: str) -> pathlib.Path:
    try:
        path.relative_to(attempt_root)
    except ValueError:
        _error("VISTA_HOME_UE_PATH_ESCAPE", f"{label} escapes the attempt root")
    if any(part.casefold() in FORBIDDEN_PATH_PARTS for part in path.parts):
        _error("VISTA_HOME_UE_PATH_FORBIDDEN", f"{label} uses a forbidden path")
    return path


def build_execution_manifest(
    *,
    build_plan_path: os.PathLike[str] | str,
    build_plan: Mapping[str, Any],
    project_file: os.PathLike[str] | str,
    attempt_root: os.PathLike[str] | str,
    artifact_bindings: Sequence[Mapping[str, Any]],
    import_receipt: os.PathLike[str] | str,
    scene_receipt: os.PathLike[str] | str,
) -> ExecutionManifest:
    """Pin host files without placing host paths in the world content digest."""

    composition = build_composition_spec(build_plan)
    root = _canonical_path(attempt_root)
    plan_path = _safe_attempt_child(_canonical_path(build_plan_path), root, "build plan")
    project = _safe_attempt_child(_canonical_path(project_file), root, "project")
    import_output = _safe_attempt_child(_canonical_path(import_receipt), root, "import receipt")
    scene_output = _safe_attempt_child(_canonical_path(scene_receipt), root, "scene receipt")
    if not plan_path.is_file() or not project.is_file():
        _error("VISTA_HOME_UE_PIN_MISSING", "build plan and project must already exist")
    plan_sha = sha256_file(plan_path)
    if plan_sha != hashlib.sha256(canonical_json(build_plan)).hexdigest():
        _error("VISTA_HOME_UE_PLAN_PIN_MISMATCH", "build plan bytes are not canonical or differ")

    declared = {asset["asset_id"]: asset for asset in build_plan["assets"]}
    bindings: list[dict[str, Any]] = []
    seen: set[str] = set()
    expected_keys = {
        "asset_id", "source_file", "source_file_sha256", "source_binding_digest",
    }
    for raw_binding in artifact_bindings:
        binding = dict(raw_binding)
        if set(binding) != expected_keys:
            _error("VISTA_HOME_UE_BINDING_INVALID", "artifact binding fields differ")
        asset_id = binding.get("asset_id")
        if asset_id in seen or asset_id not in declared:
            _error("VISTA_HOME_UE_BINDING_INVALID", "artifact binding ID missing or duplicated")
        seen.add(asset_id)
        asset = declared[asset_id]
        if binding["source_binding_digest"] != asset["source_digest"]:
            _error("VISTA_HOME_UE_BINDING_INVALID", "artifact binding does not match the build plan")
        if asset["source_kind"] != "builtin":
            source = _canonical_path(binding["source_file"])
            if not isinstance(binding["source_file_sha256"], str) or \
                    SHA256.fullmatch(binding["source_file_sha256"]) is None or \
                    not source.is_file() or sha256_file(source) != binding["source_file_sha256"]:
                _error("VISTA_HOME_UE_SOURCE_PIN_MISMATCH", f"asset {asset_id} source mismatch")
            binding["source_file"] = str(source)
        elif binding["source_file"] is not None or binding["source_file_sha256"] is not None:
            _error("VISTA_HOME_UE_BINDING_INVALID", "builtin asset cannot carry a host source file")
        bindings.append(binding)
    if seen != set(declared):
        _error("VISTA_HOME_UE_BINDING_INCOMPLETE", "every declared asset needs exactly one binding")

    manifest = {
        "schema_version": EXECUTION_SCHEMA,
        "attempt_root": str(root),
        "project_file": str(project),
        "project_sha256": sha256_file(project),
        "build_plan_path": str(plan_path),
        "build_plan_sha256": plan_sha,
        "build_plan_content_digest": build_plan["content_digest"],
        "composition_spec": composition.value,
        "composition_spec_sha256": composition.sha256,
        "artifact_bindings": sorted(bindings, key=lambda item: item["asset_id"]),
        "scripts": {
            "import": {
                "path": str(pathlib.Path(__file__).with_name("import_assets_commandlet.py").resolve()),
                "sha256": sha256_file(pathlib.Path(__file__).with_name("import_assets_commandlet.py")),
            },
            "compose": {
                "path": str(pathlib.Path(__file__).with_name("compose_home_commandlet.py").resolve()),
                "sha256": sha256_file(pathlib.Path(__file__).with_name("compose_home_commandlet.py")),
            },
        },
        "import_receipt": str(import_output),
        "scene_receipt": str(scene_output),
        "policy": {
            "append_only_namespace": True,
            "replace_existing": False,
            "save_reload_required": True,
            "quarantine_on_failure": True,
            "studio_socket_fallback_allowed": False,
        },
    }
    raw = canonical_json(manifest)
    return ExecutionManifest(manifest, raw, hashlib.sha256(raw).hexdigest(), composition)
