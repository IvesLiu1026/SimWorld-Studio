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

from . import planning
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


def presentation_script_pins() -> dict[str, dict[str, str]]:
    """Return fixed extension commandlets without changing legacy script pins."""

    paths = {
        "import": pathlib.Path(__file__).with_name(
            "import_presentation_commandlet.py"
        ).resolve(),
        "compose": pathlib.Path(__file__).with_name(
            "compose_presentation_commandlet.py"
        ).resolve(),
        "common": pathlib.Path(__file__).with_name(
            "presentation_commandlet_common.py"
        ).resolve(),
    }
    return {
        name: {"path": str(path), "sha256": sha256_file(path)}
        for name, path in paths.items()
    }


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
    visual_profile: Mapping[str, Any] | None = None,
    visual_profile_path: os.PathLike[str] | str | None = None,
    visual_profile_sha256: str | None = None,
    renderer_request_path: os.PathLike[str] | str | None = None,
    renderer_request_sha256: str | None = None,
    renderer_request_content_digest: str | None = None,
    presentation_manifest_path: os.PathLike[str] | str | None = None,
    presentation_manifest_sha256: str | None = None,
    presentation_artifact_receipt_path: os.PathLike[str] | str | None = None,
    presentation_artifact_receipt_sha256: str | None = None,
    presentation_bindings: Sequence[Mapping[str, Any]] | None = None,
) -> ExecutionManifest:
    """Pin host files without placing host paths in the world content digest."""

    r2_values = (
        visual_profile,
        visual_profile_path,
        visual_profile_sha256,
        renderer_request_path,
        renderer_request_sha256,
        renderer_request_content_digest,
    )
    if any(value is not None for value in r2_values) and not all(
        value is not None for value in r2_values
    ):
        _error(
            "VISTA_HOME_UE_VISUAL_PIN_INCOMPLETE",
            "visual profile and renderer request pins must be supplied together",
        )
    presentation_values = (
        presentation_manifest_path,
        presentation_manifest_sha256,
        presentation_artifact_receipt_path,
        presentation_artifact_receipt_sha256,
        presentation_bindings,
    )
    has_presentation = any(value is not None for value in presentation_values)
    if has_presentation and not all(value is not None for value in presentation_values):
        _error(
            "VISTA_HOME_UE_PRESENTATION_PIN_INCOMPLETE",
            "presentation manifest, receipt, and bindings must be supplied together",
        )
    if has_presentation and visual_profile is None:
        _error(
            "VISTA_HOME_UE_PRESENTATION_WITHOUT_PROFILE",
            "presentation bundles require a selected visual profile",
        )
    composition = build_composition_spec(
        build_plan,
        visual_profile,
        presentation_bindings,
    )
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
            "common": {
                "path": str(pathlib.Path(__file__).with_name("commandlet_common.py").resolve()),
                "sha256": sha256_file(pathlib.Path(__file__).with_name("commandlet_common.py")),
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
    if visual_profile is not None:
        profile_path = _safe_attempt_child(
            _canonical_path(visual_profile_path), root, "visual profile"
        )
        renderer_path = _safe_attempt_child(
            _canonical_path(renderer_request_path), root, "renderer request"
        )
        if (
            not isinstance(visual_profile_sha256, str)
            or SHA256.fullmatch(visual_profile_sha256) is None
            or not profile_path.is_file()
            or sha256_file(profile_path) != visual_profile_sha256
        ):
            _error(
                "VISTA_HOME_UE_VISUAL_PIN_MISMATCH",
                "visual profile bytes differ from their pin",
            )
        try:
            materialized_profile = json.loads(
                profile_path.read_text(encoding="utf-8", errors="strict")
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            _error(
                "VISTA_HOME_UE_VISUAL_PIN_MISMATCH",
                "visual profile is not strict JSON",
            )
            raise AssertionError from exc
        if materialized_profile != dict(visual_profile):
            _error(
                "VISTA_HOME_UE_VISUAL_PIN_MISMATCH",
                "visual profile bytes do not represent the compiled profile",
            )
        if (
            not isinstance(renderer_request_sha256, str)
            or SHA256.fullmatch(renderer_request_sha256) is None
            or not isinstance(renderer_request_content_digest, str)
            or SHA256.fullmatch(renderer_request_content_digest) is None
            or not renderer_path.is_file()
            or sha256_file(renderer_path) != renderer_request_sha256
        ):
            _error(
                "VISTA_HOME_UE_RENDERER_PIN_MISMATCH",
                "renderer request bytes differ from their pin",
            )
        try:
            renderer_raw = renderer_path.read_bytes()
            renderer_request = json.loads(renderer_raw.decode("utf-8", "strict"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            _error(
                "VISTA_HOME_UE_RENDERER_PIN_MISMATCH",
                "renderer request is not strict JSON",
            )
            raise AssertionError from exc
        if not isinstance(renderer_request, dict):
            _error(
                "VISTA_HOME_UE_RENDERER_PIN_MISMATCH",
                "renderer request root must be an object",
            )
        digest_body = dict(renderer_request)
        digest_body.pop("content_digest", None)
        expected_request_keys = {
            "schema_version",
            "status",
            "runtime_proof",
            "visual_profile_id",
            "visual_profile_content_digest",
            "renderer_profile",
            "renderer_profile_digest",
            "engine_config_sha256",
            "observation_contract",
            "content_digest",
        }
        engine_config = _safe_attempt_child(
            project.parent / "Config" / "DefaultEngine.ini",
            root,
            "renderer engine config",
        )
        if (
            set(renderer_request) != expected_request_keys
            or renderer_request.get("schema_version")
            != "simworld.vista.playable-home-renderer-request/v2"
            or canonical_json(renderer_request) != renderer_raw
            or renderer_request.get("status")
            != "staged_runtime_observation_required"
            or renderer_request.get("runtime_proof") is not False
            or renderer_request.get("visual_profile_id")
            != visual_profile.get("visual_profile_id")
            or renderer_request.get("visual_profile_content_digest")
            != visual_profile.get("content_digest")
            or renderer_request.get("content_digest")
            != renderer_request_content_digest
            or not isinstance(renderer_request.get("engine_config_sha256"), str)
            or SHA256.fullmatch(renderer_request["engine_config_sha256"]) is None
            or not engine_config.is_file()
            or sha256_file(engine_config)
            != renderer_request["engine_config_sha256"]
            or hashlib.sha256(canonical_json(digest_body)).hexdigest()
            != renderer_request_content_digest
        ):
            _error(
                "VISTA_HOME_UE_RENDERER_PIN_MISMATCH",
                "renderer request contract or visual-profile binding differs",
            )
        manifest.update({
            "visual_profile_path": str(profile_path),
            "visual_profile_sha256": visual_profile_sha256,
            "visual_profile_content_digest": visual_profile["content_digest"],
            "renderer_profile_request": {
                "path": str(renderer_path),
                "sha256": renderer_request_sha256,
                "content_digest": renderer_request_content_digest,
                "status": "staged_runtime_observation_required",
                "runtime_proof": False,
            },
        })
    if has_presentation:
        presentation_manifest = _safe_attempt_child(
            _canonical_path(presentation_manifest_path),
            root,
            "presentation manifest",
        )
        presentation_receipt = _safe_attempt_child(
            _canonical_path(presentation_artifact_receipt_path),
            root,
            "presentation artifact receipt",
        )
        for path, expected, label in (
            (presentation_manifest, presentation_manifest_sha256, "presentation manifest"),
            (
                presentation_receipt,
                presentation_artifact_receipt_sha256,
                "presentation artifact receipt",
            ),
        ):
            if (
                not isinstance(expected, str)
                or SHA256.fullmatch(expected) is None
                or not path.is_file()
                or sha256_file(path) != expected
            ):
                _error(
                    "VISTA_HOME_UE_PRESENTATION_PIN_MISMATCH",
                    f"{label} bytes differ from their pin",
                )
        normalized_bindings: list[dict[str, Any]] = []
        room_ids: set[str] = set()
        artifact_ids: set[str] = set()
        for index, raw_binding in enumerate(presentation_bindings or ()):
            binding = dict(raw_binding)
            if set(binding) != planning.PRESENTATION_EXECUTION_BINDING_KEYS:
                _error(
                    "VISTA_HOME_UE_PRESENTATION_BINDING_INVALID",
                    f"presentation binding {index} fields differ",
                )
            source = _canonical_path(binding.get("source_file"))
            expected = binding.get("source_file_sha256")
            if (
                not isinstance(expected, str)
                or SHA256.fullmatch(expected) is None
                or expected != binding.get("sha256")
                or not source.is_file()
                or sha256_file(source) != expected
            ):
                _error(
                    "VISTA_HOME_UE_PRESENTATION_PIN_MISMATCH",
                    f"presentation binding {index} source bytes differ",
                )
            room_id = binding.get("room_id")
            artifact_id = binding.get("artifact_id")
            if (
                not isinstance(room_id, str)
                or room_id in room_ids
                or not isinstance(artifact_id, str)
                or artifact_id in artifact_ids
            ):
                _error(
                    "VISTA_HOME_UE_PRESENTATION_BINDING_INVALID",
                    "presentation room or artifact identity is duplicated",
                )
            binding["source_file"] = str(source)
            normalized_bindings.append(binding)
            room_ids.add(room_id)
            artifact_ids.add(artifact_id)
        if len(normalized_bindings) != len(planning.PRESENTATION_ROOM_KINDS):
            _error(
                "VISTA_HOME_UE_PRESENTATION_BINDING_INVALID",
                "presentation binding inventory must contain exactly three bundles",
            )
        manifest.update({
            "presentation_sources": {
                "manifest": {
                    "path": str(presentation_manifest),
                    "sha256": presentation_manifest_sha256,
                },
                "artifact_receipt": {
                    "path": str(presentation_receipt),
                    "sha256": presentation_artifact_receipt_sha256,
                },
            },
            "presentation_bindings": sorted(
                normalized_bindings, key=lambda item: item["room_id"]
            ),
            "presentation_scripts": presentation_script_pins(),
            "presentation_import_receipt": str(
                root / "presentation-import-receipt.json"
            ),
            "presentation_scene_receipt": str(
                root / "presentation-scene-receipt.json"
            ),
            "presentation_runtime_proof": "pending",
        })
    raw = canonical_json(manifest)
    return ExecutionManifest(manifest, raw, hashlib.sha256(raw).hexdigest(), composition)
