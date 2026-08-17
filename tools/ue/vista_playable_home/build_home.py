#!/usr/bin/env python3
"""Build one append-only VISTA Playable Home UE project from pinned inputs.

The default CLI mode is a zero-write dry run.  It validates every source pin,
compiles the exact UE execution manifest, and prints both fixed commandlet
commands.  ``--apply`` materializes a fresh content-only project, copies the
compiled plugin and Manny content, then runs import followed by composition.

This host-side tool never accepts caller-authored Unreal Python.  The two
commandlet paths are fixed beside this file and are byte-pinned in the
execution manifest produced by :mod:`contract`.
"""

from __future__ import annotations

import argparse
import copy
import errno
import fcntl
import hashlib
import json
import math
import os
import pathlib
import re
import secrets
import shutil
import signal
import stat
import struct
import subprocess
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


Path = pathlib.Path
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.blender.vista_playable_home import contract_scene as blender_contract  # noqa: E402
from tools.blender.vista_playable_home_realism import (  # noqa: E402
    external_assets as realism_external_assets,
)
from tools.blender.vista_playable_home_hssd import planner as hssd_contract  # noqa: E402
from tools.ue.vista_playable_home import contract, planning  # noqa: E402
from tools.ue.vista_playable_home.commandlet_common import (  # noqa: E402
    IMPORT_MARKER,
    IMPORT_RECEIPT_SCHEMA,
    IMPORT_RESULT_FILE,
    SCENE_MARKER,
    SCENE_RECEIPT_SCHEMA,
    SCENE_RESULT_FILE,
    derived_asset_path,
)
from tools.worlds import playable_home as world_contract  # noqa: E402
from world_packs.vista_playable_home_r1.visual_profiles import (  # noqa: E402
    contract as visual_profile_contract,
)


ORCHESTRATOR_PLAN_SCHEMA = "simworld.vista.playable-home-ue-build-plan/v1"
PREPARATION_RECEIPT_SCHEMA = "simworld.vista.playable-home-ue-preparation-receipt/v1"
RESULT_RECEIPT_SCHEMA = "simworld.vista.playable-home-ue-build-result/v1"
POINTER_SCHEMA = "simworld.vista.playable-home-ue-build-pointer/v1"
ATTEMPT_OWNER_SCHEMA = "simworld.vista.playable-home-ue-attempt-owner/v1"
BLENDER_BUILD_RECEIPT_SCHEMA = "simworld.vista.playable-home-blender-build-receipt/v1"
HSSD_MANIFEST_SCHEMA = "simworld.vista.playable-home-hssd-attribution/v1"
HSSD_BINDING_PLAN_SCHEMA = "simworld.vista.playable-home-hssd-binding-plan/v1"
EXPECTED_REVISION = "vista_playable_home_r1"
EXPECTED_PROJECT_NAME = "VistaPlayableHome.uproject"
EXPECTED_PLUGIN_NAME = "VistaPlayableHome"
MAX_JSON_BYTES = 64 * 1024 * 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ATTEMPT_RE = re.compile(r"^attempt-[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
FICLONE = 0x40049409
FORBIDDEN_ATTEMPT_PARTS = frozenset(
    {"archive", "archives", "canonical", "production", "release", "releases", "r8", "disposable-project-r8"}
)
PROJECT_PLUGINS = (
    "VistaPlayableHome",
    "PythonScriptPlugin",
    "EditorScriptingUtilities",
    "Interchange",
)
PLUGIN_REQUIRED_FILES = (
    "VistaPlayableHome.uplugin",
    "Binaries/Linux/libUnrealEditor-VistaPlayableHome.so",
    "Binaries/Linux/libUnrealEditor-VistaPlayableHomeEditor.so",
    "Binaries/Linux/UnrealEditor.modules",
    "Config/DefaultVistaPlayableHome.ini",
    "README.md",
)
MANNY_REQUIRED_FILES = (
    "Mannequins/Meshes/SKM_Manny.uasset",
    "Mannequins/Animations/ABP_Manny.uasset",
)
HSSD_BUILDER_SOURCE_FILES = (
    "tools/blender/vista_playable_home_hssd/basisu_decode.mjs",
    "tools/blender/vista_playable_home_hssd/build.py",
    "tools/blender/vista_playable_home_hssd/glb_transport.py",
    "tools/blender/vista_playable_home_hssd/planner.py",
)
HSSD_BASIS_TRANSCODER_JS_SHA256 = (
    "8478b5b6d6b74e7d3082b89f6417321d8d1dc0307f2b30d4484bb11b441696a1"
)
HSSD_BASIS_TRANSCODER_WASM_SHA256 = (
    "6cf17dc889352c42e9acf8897107978d127005fe3386c36a0e3845e27967630a"
)
RENDERER_OBSERVATION_SCHEMA = "simworld.vista.playable-home-renderer-observation-contract/v2"
RENDERER_REQUEST_SCHEMA = "simworld.vista.playable-home-renderer-request/v2"
RENDERER_STATUS_SCHEMA = "simworld.vista.playable-home-renderer-status/v1"
RENDERER_REGISTRY_SCHEMA = "simworld.vista.playable-home-ue-renderer-registry/v1"
RENDERER_REGISTRY_ID = "ue_5_7_3_registered_cvars_v1"
RENDERER_REGISTRY_PATH = (
    REPO_ROOT
    / "world_packs/vista_playable_home_r1/visual_profiles/ue_5_7_3_renderer_registry.json"
)
PINNED_UNREAL_ENGINE_VERSION = "5.7.3"
PINNED_UNREAL_ENGINE_RUNTIME_VERSION = (
    "5.7.3-50162420+++UE5+Release-5.7"
)
VISUAL_PROFILE_ATTEMPT_FILE = "visual-profile.json"
RENDERER_REQUEST_ATTEMPT_FILE = "renderer-profile-request.json"
PRESENTATION_MANIFEST_ATTEMPT_FILE = "presentation-manifest.json"
PRESENTATION_ARTIFACT_RECEIPT_ATTEMPT_FILE = "presentation-artifact-receipt.json"
PRESENTATION_VULKAN_ICD_ATTEMPT_FILE = "presentation-vulkan-icd.json"
PRESENTATION_FORGE_SCHEMA = "simworld.vista.playable-home-realism-forge/v1"
PRESENTATION_ARTIFACT_RECEIPT_SCHEMA = "simworld.vista.playable-home-realism-artifacts/v1"
PRESENTATION_FORGE_SCHEMA_V2 = "simworld.vista.playable-home-realism-forge/v2"
PRESENTATION_ARTIFACT_RECEIPT_SCHEMA_V2 = (
    "simworld.vista.playable-home-realism-artifacts/v2"
)
PRESENTATION_IMPORT_RECEIPT_SCHEMA = (
    "simworld.vista.playable-home-ue-presentation-import-receipt/v1"
)
PRESENTATION_IMPORT_RECEIPT_SCHEMA_V2 = (
    "simworld.vista.playable-home-ue-presentation-import-receipt/v2"
)
PRESENTATION_SCENE_RECEIPT_SCHEMA = (
    "simworld.vista.playable-home-ue-presentation-scene-receipt/v1"
)
PRESENTATION_SCENE_RECEIPT_SCHEMA_V2 = (
    "simworld.vista.playable-home-ue-presentation-scene-receipt/v2"
)
PRESENTATION_IMPORT_RESULT_FILE = "presentation-import-result.json"
PRESENTATION_SCENE_RESULT_FILE = "presentation-scene-result.json"
PRESENTATION_IMPORT_MARKER = "VISTA_PLAYABLE_HOME_PRESENTATION_IMPORT_RESULT:"
PRESENTATION_SCENE_MARKER = "VISTA_PLAYABLE_HOME_PRESENTATION_SCENE_RESULT:"
PRESENTATION_VULKAN_ICD_ENV = "VK_ICD_FILENAMES"
VULKAN_DRIVER_ENVIRONMENT_KEYS = (
    "VK_ICD_FILENAMES",
    "VK_DRIVER_FILES",
    "VK_ADD_DRIVER_FILES",
    "VK_LOADER_DRIVERS_SELECT",
    "VK_LOADER_DRIVERS_DISABLE",
)
COMMANDLET_RUNTIME_DIRECTORY = "commandlet-runtime"
COMMANDLET_PHASES = (
    "import",
    "presentation_import",
    "compose",
    "presentation_compose",
)
PRESENTATION_BUNDLE_RECORD_KEYS = frozenset({
    "artifact_id",
    "artifact_kind",
    "target_asset_id",
    "room_id",
    "room_kind",
    "relative_path",
    "media_type",
    "sha256",
    "size_bytes",
    "mesh_count",
    "material_count",
    "pbr_complete_material_count",
    "texture_count",
    "material_ids",
    "expected_world_transform_cm",
    "bundle_root_transform",
    "root_transform_policy",
    "semantic_policy",
    "collision_policy",
    "unreal_collision_profile",
    "cameras_exported",
    "lights_exported",
    "source_hashes",
})
PRESENTATION_BUNDLE_RECORD_KEYS_V2 = (
    PRESENTATION_BUNDLE_RECORD_KEYS | frozenset({"external_content"})
)
PRESENTATION_EXTERNAL_PLACEMENT_SCHEMA = (
    "simworld.vista.playable-home-external-placement/v1"
)
PRESENTATION_EXTERNAL_NORMALIZATION_POLICY = (
    "measured_combined_bounds_floor_center_uniform_scale_v1"
)
PRESENTATION_EXTERNAL_NANITE_POLICY = (
    "disabled_unproven_opaque_or_translucent_external_bundle_v1"
)
PRESENTATION_EXTERNAL_ACQUISITION_SCHEMA = (
    "simworld.vista.playable-home-poly-haven-receipt/v1"
)
PRESENTATION_EXTERNAL_MANIFEST_KEYS = frozenset({
    "schema_version", "forge_id", "house_revision", "visual_profile_id",
    "seed", "source_house_digest", "source_profile_digest",
    "forge_plan_digest", "build_quality", "rooms", "openings",
    "components", "dressing", "materials", "role_counts",
    "room_component_counts", "export_contract", "ue_import_bundles",
    "external_placement", "external_staticization",
})
PRESENTATION_EXTERNAL_STATICIZATION_ARTIFACT_KEYS = frozenset({
    "artifact_id", "relative_path", "media_type", "sha256", "size_bytes",
})
PRESENTATION_EXTERNAL_STATICIZATION_ARTIFACT_ID = "receipt.external_staticization"
PRESENTATION_EXTERNAL_STATICIZATION_FILENAME = "external-staticization-receipt.json"
PRESENTATION_EXTERNAL_PLACEMENT_KEYS = frozenset({
    "schema_version", "placement_id", "normalization_policy",
    "acquisition_receipt", "placement_manifest_sha256",
    "semantic_target_ids", "dressing_ids", "asset_sources", "placements",
    "content_digest",
})
PRESENTATION_EXTERNAL_CONTENT_KEYS = frozenset({
    "schema_version", "normalization_policy", "acquisition_receipt",
    "placement_manifest_sha256", "placement_plan_sha256",
    "semantic_target_ids", "dressing_ids", "asset_sources",
})
PRESENTATION_EXTERNAL_PLACEMENT_RECORD_KEYS = frozenset({
    "placement_id", "placement_kind", "room_id", "room_kind", "category",
    "realization_mode", "semantic_target_id", "anchor_id",
    "support_placement_id", "source_logical_asset_id", "geometry_recipe",
    "material_logical_asset_ids", "location_m", "rotation_deg",
    "uniform_scale", "source_dimensions_m", "room_local_aabb",
    "source_tree_sha256",
})
PRESENTATION_EXTERNAL_ASSET_SOURCE_KEYS = frozenset({
    "logical_asset_id", "asset_id", "asset_type", "resolution",
    "provider_files_hash", "source_tree_sha256", "files",
})
PRESENTATION_EXTERNAL_ASSET_FILE_KEYS = frozenset({
    "relative_path", "size_bytes", "sha256", "texture_semantics",
    "dimensions_px",
})
PRESENTATION_EXTERNAL_SEMANTIC_TARGETS_BY_ROOM = {
    "home.r1/room.entry_hall": frozenset({
        "home.r1/room.entry_hall/entity.shoe_bench.01",
    }),
    "home.r1/room.living_room": frozenset({
        "home.r1/room.living_room/entity.coffee_table.01",
        "home.r1/room.living_room/entity.sofa.01",
    }),
    "home.r1/room.kitchen_dining": frozenset({
        "home.r1/room.kitchen_dining/entity.dining_table.01",
        "home.r1/room.kitchen_dining/entity.stove.01",
    }),
}
PRESENTATION_EXTERNAL_SEMANTIC_TARGET_CATEGORIES = {
    "home.r1/room.entry_hall/entity.shoe_bench.01": "shoe_bench",
    "home.r1/room.living_room/entity.coffee_table.01": "coffee_table",
    "home.r1/room.living_room/entity.sofa.01": "sofa",
    "home.r1/room.kitchen_dining/entity.dining_table.01": "dining_table",
    "home.r1/room.kitchen_dining/entity.stove.01": "stove",
}
PRESENTATION_EXTERNAL_HERO_PLACEMENTS = {
    "home.r1/room.entry_hall/entity.shoe_bench.01": {
        "placement_id": "hero.entry.shoe_bench",
        "realization_mode": "project_authored",
        "source_logical_asset_id": None,
        "geometry_recipe": "contemporary_shoe_bench_v1",
        "material_logical_asset_ids": (
            "visual.material.white_oak_veneer",
            "visual.material.poly_wool_herringbone",
        ),
    },
    "home.r1/room.living_room/entity.sofa.01": {
        "placement_id": "hero.living.sofa",
        "realization_mode": "project_authored",
        "source_logical_asset_id": None,
        "geometry_recipe": "contemporary_sofa_v1",
        "material_logical_asset_ids": (
            "visual.material.white_oak_veneer",
            "visual.material.poly_wool_herringbone",
        ),
    },
    "home.r1/room.living_room/entity.coffee_table.01": {
        "placement_id": "hero.living.coffee_table",
        "realization_mode": "external_blend",
        "source_logical_asset_id": "visual.hero.living_coffee_table",
        "geometry_recipe": None,
        "material_logical_asset_ids": (),
    },
    "home.r1/room.kitchen_dining/entity.stove.01": {
        "placement_id": "hero.kitchen.stove",
        "realization_mode": "external_blend",
        "source_logical_asset_id": "visual.hero.kitchen_stove",
        "geometry_recipe": None,
        "material_logical_asset_ids": (),
    },
    "home.r1/room.kitchen_dining/entity.dining_table.01": {
        "placement_id": "hero.kitchen.dining_table",
        "realization_mode": "project_authored",
        "source_logical_asset_id": None,
        "geometry_recipe": "contemporary_dining_table_v1",
        "material_logical_asset_ids": ("visual.material.white_oak_veneer",),
    },
}
PRESENTATION_EXTERNAL_DRESSING_IDS_BY_ROOM = {
    "home.r1/room.entry_hall": frozenset({
        "dress.entry.postcards",
        "dress.entry.rubber_boots",
        "dress.entry.wicker_basket",
    }),
    "home.r1/room.living_room": frozenset({
        "dress.living.armchair",
        "dress.living.books",
        "dress.living.ceramic_vase",
        "dress.living.media_cabinet",
        "dress.living.potted_plant",
        "dress.living.side_table",
    }),
    "home.r1/room.kitchen_dining": frozenset({
        "dress.kitchen.cardboard_box",
        "dress.kitchen.chair_east",
        "dress.kitchen.chair_north",
        "dress.kitchen.chair_south",
        "dress.kitchen.cutting_board",
        "dress.kitchen.wooden_bowl",
        "dress.kitchen.wooden_plate",
        "dress.kitchen.wooden_spoon",
    }),
}
PRESENTATION_EXTERNAL_DRESSING_SOURCES = {
    "dress.entry.postcards": "visual.dressing.entry.postcards",
    "dress.entry.rubber_boots": "visual.dressing.entry.rubber_boots",
    "dress.entry.wicker_basket": "visual.dressing.entry.wicker_basket",
    "dress.living.armchair": "visual.dressing.living.armchair",
    "dress.living.books": "visual.dressing.living.books",
    "dress.living.ceramic_vase": "visual.dressing.shared.ceramic_vase",
    "dress.living.media_cabinet": "visual.dressing.living.media_cabinet",
    "dress.living.potted_plant": "visual.dressing.living.potted_plant",
    "dress.living.side_table": "visual.dressing.living.side_table",
    "dress.kitchen.cardboard_box": "visual.dressing.kitchen.cardboard_box",
    "dress.kitchen.chair_east": "visual.dressing.kitchen.dining_chair",
    "dress.kitchen.chair_north": "visual.dressing.kitchen.dining_chair",
    "dress.kitchen.chair_south": "visual.dressing.kitchen.dining_chair",
    "dress.kitchen.cutting_board": "visual.dressing.kitchen.cutting_board",
    "dress.kitchen.wooden_bowl": "visual.dressing.kitchen.wooden_bowl",
    "dress.kitchen.wooden_plate": "visual.dressing.kitchen.wooden_plate",
    "dress.kitchen.wooden_spoon": "visual.dressing.kitchen.wooden_spoon",
}
SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
PRESENTATION_EXTERNAL_ID_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$"
)
RENDERER_PROFILE_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
RENDERER_SCALABILITY_KEYS = (
    "view_distance",
    "anti_aliasing",
    "shadow",
    "global_illumination",
    "reflection",
    "post_process",
    "texture",
    "effects",
    "foliage",
    "shading",
)
RENDERER_SCALABILITY_CVARS = {
    "view_distance": "sg.ViewDistanceQuality",
    "anti_aliasing": "sg.AntiAliasingQuality",
    "shadow": "sg.ShadowQuality",
    "global_illumination": "sg.GlobalIlluminationQuality",
    "reflection": "sg.ReflectionQuality",
    "post_process": "sg.PostProcessQuality",
    "texture": "sg.TextureQuality",
    "effects": "sg.EffectsQuality",
    "foliage": "sg.FoliageQuality",
    "shading": "sg.ShadingQuality",
}


def load_renderer_cvar_registry() -> dict[str, Any]:
    """Load the repository-pinned UE 5.7.3 registration evidence."""

    try:
        value = visual_profile_contract.load_json(RENDERER_REGISTRY_PATH)
    except visual_profile_contract.VisualProfileContractError as exc:
        _fail(
            "VISTA_HOME_RENDERER_REGISTRY_INVALID",
            "UE 5.7.3 renderer registration evidence is unavailable",
        )
        raise AssertionError from exc
    return value


def validate_renderer_cvar_registry(
    registry: Mapping[str, Any], required_names: Sequence[str]
) -> dict[str, Mapping[str, Any]]:
    """Fail closed unless every observed CVar has pinned registration proof."""

    expected_top = {
        "schema_version",
        "registry_id",
        "engine",
        "registration_evidence_policy",
        "pre_exposure_policy",
        "registrations",
        "content_digest",
    }
    if not isinstance(registry, Mapping) or set(registry) != expected_top:
        _fail("VISTA_HOME_RENDERER_REGISTRY_INVALID", "renderer registry fields differ")
    if (
        registry.get("schema_version") != RENDERER_REGISTRY_SCHEMA
        or registry.get("registry_id") != RENDERER_REGISTRY_ID
        or registry.get("registration_evidence_policy")
        != "pinned-ue-source-file-and-declaration-symbol-sha256/v1"
        or registry.get("content_digest") != _content_digest(registry)
    ):
        _fail("VISTA_HOME_RENDERER_REGISTRY_INVALID", "renderer registry identity differs")
    engine = registry.get("engine")
    if not isinstance(engine, Mapping) or dict(engine) != {
        "version": PINNED_UNREAL_ENGINE_VERSION,
        "runtime_version": PINNED_UNREAL_ENGINE_RUNTIME_VERSION,
        "changelist": 50162420,
        "branch_name": "++UE5+Release-5.7",
        "build_version_relative_path": "Engine/Build/Build.version",
        "build_version_sha256": (
            "ffe01f6d1e96ef86cd06158cfb561150971823fc77e5c8df352910bcf4d365ef"
        ),
    }:
        _fail("VISTA_HOME_RENDERER_REGISTRY_INVALID", "pinned UE engine evidence differs")
    pre_exposure = registry.get("pre_exposure_policy")
    if not isinstance(pre_exposure, Mapping) or dict(pre_exposure) != {
        "semantic_enabled": True,
        "runtime_policy": "ue5_always_on_engine_managed",
        "policy_source_relative_path": (
            "Engine/Source/Runtime/RenderCore/Private/Shader.cpp"
        ),
        "policy_source_sha256": (
            "a4ce752f788e5f9c4b73516175b0b8c39a0b21bb601382f51ae4dcca71a7904a"
        ),
        "policy_evidence": "PreExposure is always used",
        "override_cvar": "r.EyeAdaptation.PreExposureOverride",
        "override_expected": 0,
    }:
        _fail("VISTA_HOME_RENDERER_REGISTRY_INVALID", "pre-exposure evidence differs")
    registrations = registry.get("registrations")
    if not isinstance(registrations, list) or not registrations:
        _fail("VISTA_HOME_RENDERER_REGISTRY_INVALID", "renderer registrations are empty")
    evidence: dict[str, Mapping[str, Any]] = {}
    expected_fields = {
        "name",
        "declaration_symbol",
        "registration_kind",
        "source_relative_path",
        "source_sha256",
    }
    for registration in registrations:
        if not isinstance(registration, Mapping) or set(registration) != expected_fields:
            _fail("VISTA_HOME_RENDERER_REGISTRY_INVALID", "registration fields differ")
        name = registration.get("name")
        source = registration.get("source_relative_path")
        if (
            not isinstance(name, str)
            or not name
            or name in evidence
            or name == "r.UsePreExposure"
            or registration.get("registration_kind") != "TAutoConsoleVariable"
            or not isinstance(registration.get("declaration_symbol"), str)
            or not registration["declaration_symbol"]
            or not isinstance(source, str)
            or not source.startswith("Engine/Source/")
            or Path(source).is_absolute()
            or ".." in Path(source).parts
            or not isinstance(registration.get("source_sha256"), str)
            or SHA256_RE.fullmatch(registration["source_sha256"]) is None
        ):
            _fail("VISTA_HOME_RENDERER_REGISTRY_INVALID", "registration evidence is invalid")
        evidence[name] = registration
    required = list(required_names)
    if len(required) != len(set(required)) or set(required) != set(evidence):
        missing = sorted(set(required) - set(evidence))
        unexpected = sorted(set(evidence) - set(required))
        _fail(
            "VISTA_HOME_RENDERER_CVAR_UNREGISTERED",
            f"required CVar evidence differs (missing={missing}, unexpected={unexpected})",
        )
    return evidence


class BuildHomeError(RuntimeError):
    """Stable fail-closed error raised before unsafe or ambiguous work."""

    def __init__(self, code: str, detail: str, *, pointer: str | None = None) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.pointer = pointer

    def public_dict(self) -> dict[str, str]:
        value = {"code": self.code, "message": self.detail}
        if self.pointer:
            value["pointer"] = self.pointer
        return value


@dataclass(frozen=True)
class RendererProfileCompilation:
    """Deterministic config request plus a separate runtime-observation gate."""

    profile: dict[str, Any]
    linux_target_lines: tuple[str, ...]
    renderer_lines: tuple[str, ...]
    console_lines: tuple[str, ...]
    observation_contract: dict[str, Any]
    content_digest: str


def _fail(code: str, detail: str, *, pointer: str | None = None) -> None:
    raise BuildHomeError(code, detail, pointer=pointer)


def canonical_json(value: Any) -> bytes:
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
    except (TypeError, ValueError, OverflowError) as exc:
        _fail("VISTA_HOME_BUILD_JSON_INVALID", "value is not finite canonical JSON")
        raise AssertionError from exc


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_sha(value: str | None, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        _fail("VISTA_HOME_BUILD_PIN_INVALID", f"{label} must be a lowercase SHA-256 digest")
    return value


def _duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            _fail("VISTA_HOME_BUILD_JSON_DUPLICATE_KEY", "JSON contains a duplicate object key")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    _fail("VISTA_HOME_BUILD_JSON_NON_FINITE", f"JSON constant {value!r} is forbidden")


def _assert_finite(value: Any, pointer: str = "$", depth: int = 0) -> None:
    if depth > 96:
        _fail("VISTA_HOME_BUILD_JSON_INVALID", "JSON nesting exceeds the safety limit", pointer=pointer)
    if isinstance(value, float) and not math.isfinite(value):
        _fail("VISTA_HOME_BUILD_JSON_NON_FINITE", "JSON contains a non-finite number", pointer=pointer)
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                _fail("VISTA_HOME_BUILD_JSON_INVALID", "JSON object keys must be strings", pointer=pointer)
            _assert_finite(child, f"{pointer}.{key}", depth + 1)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_finite(child, f"{pointer}[{index}]", depth + 1)


def _load_json(path: Path, *, expected_sha256: str, label: str) -> tuple[dict[str, Any], bytes]:
    source = _existing_file(path, label)
    expected = _require_sha(expected_sha256, f"{label} pin")
    try:
        size = source.stat().st_size
    except (OSError, RuntimeError, ValueError) as exc:
        _fail("VISTA_HOME_BUILD_INPUT_UNREADABLE", f"{label} cannot be read", pointer=str(source))
        raise AssertionError from exc
    if size <= 0 or size > MAX_JSON_BYTES:
        _fail("VISTA_HOME_BUILD_JSON_INVALID", f"{label} size is outside the safety bound", pointer=str(source))
    raw = source.read_bytes()
    if sha256_bytes(raw) != expected:
        _fail("VISTA_HOME_BUILD_PIN_MISMATCH", f"{label} SHA-256 differs", pointer=str(source))
    try:
        value = json.loads(
            raw.decode("utf-8", "strict"),
            object_pairs_hook=_duplicate_object,
            parse_constant=_reject_constant,
        )
    except BuildHomeError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        _fail("VISTA_HOME_BUILD_JSON_INVALID", f"{label} is not strict UTF-8 JSON", pointer=str(source))
        raise AssertionError from exc
    if not isinstance(value, dict):
        _fail("VISTA_HOME_BUILD_JSON_INVALID", f"{label} root must be an object", pointer=str(source))
    _assert_finite(value)
    return value, raw


def _absolute_lexical(path: Path, label: str) -> Path:
    candidate = Path(path).expanduser()
    value = str(candidate)
    if not candidate.is_absolute() or os.path.normpath(value) != value:
        _fail("VISTA_HOME_BUILD_PATH_INVALID", f"{label} must be absolute and normalized", pointer=value)
    return candidate


def _reject_symlink_components(path: Path, label: str, *, allow_missing_tail: bool = False) -> None:
    candidate = _absolute_lexical(path, label)
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current = current / part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            if allow_missing_tail:
                return
            _fail("VISTA_HOME_BUILD_PATH_MISSING", f"{label} does not exist", pointer=str(candidate))
        if stat.S_ISLNK(metadata.st_mode):
            _fail("VISTA_HOME_BUILD_SYMLINK_REJECTED", f"{label} contains a symlink component", pointer=str(current))


def _existing_file(path: Path, label: str) -> Path:
    candidate = _absolute_lexical(path, label)
    _reject_symlink_components(candidate, label)
    try:
        metadata = os.lstat(candidate)
    except OSError as exc:
        _fail("VISTA_HOME_BUILD_PATH_MISSING", f"{label} is missing", pointer=str(candidate))
        raise AssertionError from exc
    if not stat.S_ISREG(metadata.st_mode):
        _fail("VISTA_HOME_BUILD_PATH_INVALID", f"{label} must be a regular file", pointer=str(candidate))
    if candidate.resolve(strict=True) != candidate:
        _fail("VISTA_HOME_BUILD_PATH_INVALID", f"{label} must already be canonical", pointer=str(candidate))
    return candidate


def _existing_directory(path: Path, label: str) -> Path:
    candidate = _absolute_lexical(path, label)
    _reject_symlink_components(candidate, label)
    try:
        metadata = os.lstat(candidate)
    except OSError as exc:
        _fail("VISTA_HOME_BUILD_PATH_MISSING", f"{label} is missing", pointer=str(candidate))
        raise AssertionError from exc
    if not stat.S_ISDIR(metadata.st_mode):
        _fail("VISTA_HOME_BUILD_PATH_INVALID", f"{label} must be a directory", pointer=str(candidate))
    if candidate.resolve(strict=True) != candidate:
        _fail("VISTA_HOME_BUILD_PATH_INVALID", f"{label} must already be canonical", pointer=str(candidate))
    return candidate


def _safe_relative_path(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        _fail("VISTA_HOME_BUILD_PATH_INVALID", f"{label} must be a non-empty POSIX relative path")
    pure = pathlib.PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        _fail("VISTA_HOME_BUILD_PATH_INVALID", f"{label} contains traversal or is absolute")
    return Path(*pure.parts)


def _contained_artifact(root: Path, relative_value: Any, label: str) -> Path:
    relative = _safe_relative_path(relative_value, label)
    candidate = root / relative
    source = _existing_file(candidate, label)
    try:
        source.relative_to(root)
    except ValueError:
        _fail("VISTA_HOME_BUILD_PATH_ESCAPE", f"{label} escapes its manifest root", pointer=str(source))
    return source


@dataclass(frozen=True)
class TreeSnapshot:
    sha256: str
    file_count: int
    total_bytes: int
    records: tuple[tuple[str, int, int, str], ...]


def snapshot_tree(root: Path, label: str) -> TreeSnapshot:
    directory = _existing_directory(root, label)
    records: list[tuple[str, int, int, str]] = []
    for current, directories, files in os.walk(directory, topdown=True, followlinks=False):
        current_path = Path(current)
        directories.sort()
        files.sort()
        for name in directories:
            child = current_path / name
            metadata = os.lstat(child)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                _fail("VISTA_HOME_BUILD_TREE_UNSAFE", f"{label} contains an unsafe directory", pointer=str(child))
        for name in files:
            child = current_path / name
            metadata = os.lstat(child)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                _fail("VISTA_HOME_BUILD_TREE_UNSAFE", f"{label} contains a non-regular file", pointer=str(child))
            records.append(
                (
                    child.relative_to(directory).as_posix(),
                    stat.S_IMODE(metadata.st_mode),
                    metadata.st_size,
                    sha256_file(child),
                )
            )
    if not records:
        _fail("VISTA_HOME_BUILD_TREE_EMPTY", f"{label} contains no files", pointer=str(directory))
    raw = b"".join(
        f"{relative}\0{mode:o}\0{size}\0{digest}\n".encode("utf-8")
        for relative, mode, size, digest in records
    )
    return TreeSnapshot(
        sha256=sha256_bytes(raw),
        file_count=len(records),
        total_bytes=sum(record[2] for record in records),
        records=tuple(records),
    )


def _validate_tree_pin(root: Path, expected_sha256: str, label: str) -> TreeSnapshot:
    expected = _require_sha(expected_sha256, f"{label} tree pin")
    snapshot = snapshot_tree(root, label)
    if snapshot.sha256 != expected:
        _fail("VISTA_HOME_BUILD_PIN_MISMATCH", f"{label} tree SHA-256 differs", pointer=str(root))
    return snapshot


def _validate_output_entry(entry: Any, root: Path, label: str) -> Path:
    if not isinstance(entry, Mapping) or set(entry) != {"path", "sha256", "bytes", "media_type"}:
        _fail("VISTA_HOME_BUILD_MANIFEST_INVALID", f"{label} output fields differ")
    source = _contained_artifact(root, entry["path"], label)
    expected = _require_sha(entry.get("sha256"), f"{label} SHA-256")
    size = entry.get("bytes")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        _fail("VISTA_HOME_BUILD_MANIFEST_INVALID", f"{label} bytes is invalid")
    if source.stat().st_size != size or sha256_file(source) != expected:
        _fail("VISTA_HOME_BUILD_PIN_MISMATCH", f"{label} output bytes or SHA-256 differ", pointer=str(source))
    if not isinstance(entry.get("media_type"), str) or not entry["media_type"]:
        _fail("VISTA_HOME_BUILD_MANIFEST_INVALID", f"{label} media type is invalid")
    return source


@dataclass(frozen=True)
class BlenderInputs:
    manifest: dict[str, Any]
    normalized: dict[str, Any]
    artifacts: dict[str, tuple[Path, str]]


@dataclass(frozen=True)
class PresentationInputs:
    """Pinned Blender r2 room bundles ready for the fixed UE extension."""

    manifest: dict[str, Any]
    manifest_raw: bytes
    artifact_receipt: dict[str, Any]
    artifact_receipt_raw: bytes
    bindings: tuple[dict[str, Any], ...]


def _presentation_transform(value: Any, label: str, *, location_key: str) -> dict[str, Any]:
    expected_keys = {location_key, "rotation_deg", "scale"}
    if not isinstance(value, Mapping) or set(value) != expected_keys:
        _fail("VISTA_HOME_PRESENTATION_INVALID", f"{label} fields differ")
    result = {key: list(value[key]) for key in expected_keys}
    for key in expected_keys:
        vector = result[key]
        if (
            len(vector) != 3
            or any(
                isinstance(number, bool)
                or not isinstance(number, (int, float))
                or not math.isfinite(float(number))
                for number in vector
            )
        ):
            _fail("VISTA_HOME_PRESENTATION_INVALID", f"{label}.{key} is invalid")
    if any(float(number) <= 0.0 for number in result["scale"]):
        _fail("VISTA_HOME_PRESENTATION_INVALID", f"{label}.scale must be positive")
    return result


def _external_fail(detail: str) -> None:
    _fail("VISTA_HOME_PRESENTATION_EXTERNAL_INVALID", detail)


def _external_string_inventory(
    value: Any,
    label: str,
    *,
    expected: set[str] | frozenset[str] | None = None,
) -> list[str]:
    if (
        not isinstance(value, list)
        or any(
            not isinstance(item, str)
            or PRESENTATION_EXTERNAL_ID_RE.fullmatch(item) is None
            for item in value
        )
        or value != sorted(set(value))
        or (expected is not None and set(value) != set(expected))
    ):
        _external_fail(f"{label} is not the exact sorted identity inventory")
    return list(value)


def _validate_external_acquisition_reference(value: Any, label: str) -> dict[str, str]:
    keys = {
        "provider", "receipt_schema_version", "receipt_digest",
        "receipt_file_sha256", "acquisition_manifest_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        _external_fail(f"{label} fields differ")
    result = dict(value)
    if (
        result.get("provider") != "poly_haven"
        or result.get("receipt_schema_version")
        != PRESENTATION_EXTERNAL_ACQUISITION_SCHEMA
        or any(
            not isinstance(result.get(key), str)
            or SHA256_RE.fullmatch(result[key]) is None
            for key in (
                "receipt_digest", "receipt_file_sha256",
                "acquisition_manifest_sha256",
            )
        )
    ):
        _external_fail(f"{label} provider, schema, or hashes differ")
    return result


def _external_vector(value: Any, label: str, *, positive: bool = False) -> list[float]:
    if (
        not isinstance(value, list)
        or len(value) != 3
        or any(
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(float(item))
            or (positive and float(item) <= 0.0)
            for item in value
        )
    ):
        _external_fail(f"{label} is not a finite three-vector")
    return [float(item) for item in value]


def _validate_external_asset_sources(
    value: Any,
    label: str,
) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list) or not value:
        _external_fail(f"{label} is empty or not an array")
    logical_ids = [
        item.get("logical_asset_id") if isinstance(item, Mapping) else None
        for item in value
    ]
    if (
        any(not isinstance(item, str) or not item.startswith("visual.") for item in logical_ids)
        or logical_ids != sorted(set(logical_ids))
    ):
        _external_fail(f"{label} logical identities differ")
    result: dict[str, dict[str, Any]] = {}
    allowed_semantics = {
        "ao", "base_color", "metalness", "normal", "opacity", "roughness",
    }
    for index, raw_source in enumerate(value):
        if (
            not isinstance(raw_source, Mapping)
            or set(raw_source) != PRESENTATION_EXTERNAL_ASSET_SOURCE_KEYS
        ):
            _external_fail(f"{label}[{index}] fields differ")
        source = copy.deepcopy(dict(raw_source))
        logical_id = source["logical_asset_id"]
        if (
            not isinstance(source.get("asset_id"), str)
            or PRESENTATION_EXTERNAL_ID_RE.fullmatch(source["asset_id"]) is None
            or source.get("asset_type") not in {"model", "texture"}
            or source.get("resolution") not in {"2k", "4k"}
            or not isinstance(source.get("provider_files_hash"), str)
            or SHA1_RE.fullmatch(source["provider_files_hash"]) is None
            or not isinstance(source.get("source_tree_sha256"), str)
            or SHA256_RE.fullmatch(source["source_tree_sha256"]) is None
            or not isinstance(source.get("files"), list)
            or not source["files"]
        ):
            _external_fail(f"{label}[{index}] identity or source digest differs")
        tree_rows: list[dict[str, Any]] = []
        relative_paths: set[str] = set()
        for file_index, raw_file in enumerate(source["files"]):
            if (
                not isinstance(raw_file, Mapping)
                or set(raw_file) != PRESENTATION_EXTERNAL_ASSET_FILE_KEYS
            ):
                _external_fail(f"{label}[{index}].files[{file_index}] fields differ")
            file = dict(raw_file)
            relative = file.get("relative_path")
            _safe_relative_path(
                relative,
                f"{label}[{index}].files[{file_index}].relative_path",
            )
            size = file.get("size_bytes")
            digest = file.get("sha256")
            semantics = file.get("texture_semantics")
            dimensions = file.get("dimensions_px")
            if (
                relative in relative_paths
                or isinstance(size, bool)
                or not isinstance(size, int)
                or size <= 0
                or not isinstance(digest, str)
                or SHA256_RE.fullmatch(digest) is None
                or not isinstance(semantics, list)
                or semantics != sorted(set(semantics))
                or any(item not in allowed_semantics for item in semantics)
                or (
                    semantics
                    and (
                        not isinstance(dimensions, list)
                        or len(dimensions) != 2
                        or any(
                            isinstance(item, bool)
                            or not isinstance(item, int)
                            or item <= 0
                            for item in dimensions
                        )
                    )
                )
                or (not semantics and dimensions is not None)
            ):
                _external_fail(
                    f"{label}[{index}].files[{file_index}] hash, size, or texture evidence differs"
                )
            relative_paths.add(relative)
            tree_rows.append({
                "relative_path": relative,
                "size_bytes": size,
                "sha256": digest,
            })
        tree_raw = json.dumps(
            tree_rows,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if sha256_bytes(tree_raw) != source["source_tree_sha256"]:
            _external_fail(f"{label}[{index}] source tree digest differs")
        result[logical_id] = source
    return result


def _validate_external_placement_contract(
    value: Any,
    plan: Mapping[str, Any],
    expected_room_ids: set[str],
) -> dict[str, Any]:
    if (
        not isinstance(value, Mapping)
        or set(value) != PRESENTATION_EXTERNAL_PLACEMENT_KEYS
    ):
        _external_fail("forge external_placement fields differ")
    external = copy.deepcopy(dict(value))
    if (
        external.get("schema_version") != PRESENTATION_EXTERNAL_PLACEMENT_SCHEMA
        or external.get("placement_id")
        != "vista_playable_home.realistic_interior_r2.external_v1"
        or external.get("normalization_policy")
        != PRESENTATION_EXTERNAL_NORMALIZATION_POLICY
        or not isinstance(external.get("placement_manifest_sha256"), str)
        or SHA256_RE.fullmatch(external["placement_manifest_sha256"]) is None
        or not isinstance(external.get("content_digest"), str)
        or SHA256_RE.fullmatch(external["content_digest"]) is None
        or external["content_digest"] != _content_digest(external)
    ):
        _external_fail("forge external_placement schema, policy, or digest differs")
    acquisition = _validate_external_acquisition_reference(
        external.get("acquisition_receipt"),
        "forge external acquisition receipt",
    )
    expected_semantic_by_room = {
        room_id: set(PRESENTATION_EXTERNAL_SEMANTIC_TARGETS_BY_ROOM[room_id])
        for room_id in expected_room_ids
    }
    expected_dressing_by_room = {
        room_id: set(PRESENTATION_EXTERNAL_DRESSING_IDS_BY_ROOM[room_id])
        for room_id in expected_room_ids
    }
    expected_semantic = set().union(*expected_semantic_by_room.values())
    expected_dressing = set().union(*expected_dressing_by_room.values())
    semantic_ids = _external_string_inventory(
        external.get("semantic_target_ids"),
        "forge external semantic targets",
        expected=expected_semantic,
    )
    dressing_ids = _external_string_inventory(
        external.get("dressing_ids"),
        "forge external dressing IDs",
        expected=expected_dressing,
    )
    entities = {
        item.get("entity_id"): item
        for item in plan.get("entities", [])
        if isinstance(item, Mapping) and isinstance(item.get("entity_id"), str)
    }
    for room_id, ids in expected_semantic_by_room.items():
        for entity_id in ids:
            entity = entities.get(entity_id)
            if (
                not isinstance(entity, Mapping)
                or entity.get("room_id") != room_id
                or entity.get("category")
                != PRESENTATION_EXTERNAL_SEMANTIC_TARGET_CATEGORIES[entity_id]
            ):
                _external_fail("fixed external semantic target differs from the build plan")
    source_by_id = _validate_external_asset_sources(
        external.get("asset_sources"),
        "forge external asset sources",
    )
    placements = external.get("placements")
    if not isinstance(placements, list) or len(placements) != 22:
        _external_fail("forge external placement inventory must contain exactly 22 records")
    room_kind_by_id = {
        room["room_id"]: room["kind"]
        for room in plan["rooms"]
        if room["room_id"] in expected_room_ids
    }
    seen_placement_ids: set[str] = set()
    seen_semantic: set[str] = set()
    seen_dressing: set[str] = set()
    used_source_ids: set[str] = set()
    placements_by_room: dict[str, list[dict[str, Any]]] = {
        room_id: [] for room_id in expected_room_ids
    }
    for index, raw_placement in enumerate(placements):
        if (
            not isinstance(raw_placement, Mapping)
            or set(raw_placement) != PRESENTATION_EXTERNAL_PLACEMENT_RECORD_KEYS
        ):
            _external_fail(f"forge external placement {index} fields differ")
        placement = copy.deepcopy(dict(raw_placement))
        placement_id = placement.get("placement_id")
        room_id = placement.get("room_id")
        kind = placement.get("placement_kind")
        mode = placement.get("realization_mode")
        if (
            not isinstance(placement_id, str)
            or PRESENTATION_EXTERNAL_ID_RE.fullmatch(placement_id) is None
            or placement_id in seen_placement_ids
            or room_id not in expected_room_ids
            or placement.get("room_kind") != room_kind_by_id.get(room_id)
            or not isinstance(placement.get("category"), str)
            or kind not in {"semantic_fixed", "dressing"}
            or mode not in {"project_authored", "external_blend"}
        ):
            _external_fail(f"forge external placement {index} identity differs")
        _external_vector(placement.get("location_m"), f"placement {placement_id}.location_m")
        _external_vector(placement.get("rotation_deg"), f"placement {placement_id}.rotation_deg")
        _external_vector(
            placement.get("source_dimensions_m"),
            f"placement {placement_id}.source_dimensions_m",
            positive=True,
        )
        scale = placement.get("uniform_scale")
        bounds = placement.get("room_local_aabb")
        if (
            isinstance(scale, bool)
            or not isinstance(scale, (int, float))
            or not math.isfinite(float(scale))
            or float(scale) <= 0.0
            or not isinstance(bounds, Mapping)
            or set(bounds) != {"min_m", "max_m"}
        ):
            _external_fail(f"forge external placement {placement_id} geometry differs")
        minimum = _external_vector(bounds.get("min_m"), f"placement {placement_id}.min_m")
        maximum = _external_vector(bounds.get("max_m"), f"placement {placement_id}.max_m")
        if any(left >= right for left, right in zip(minimum, maximum)):
            _external_fail(f"forge external placement {placement_id} bounds differ")
        semantic_target = placement.get("semantic_target_id")
        anchor_id = placement.get("anchor_id")
        if kind == "semantic_fixed":
            expected_hero = PRESENTATION_EXTERNAL_HERO_PLACEMENTS.get(
                semantic_target
            )
            if (
                semantic_target not in expected_semantic_by_room[room_id]
                or semantic_target in seen_semantic
                or anchor_id is not None
                or placement_id in expected_dressing
                or expected_hero is None
                or placement_id != expected_hero["placement_id"]
                or mode != expected_hero["realization_mode"]
                or placement.get("source_logical_asset_id")
                != expected_hero["source_logical_asset_id"]
                or placement.get("geometry_recipe")
                != expected_hero["geometry_recipe"]
                or tuple(placement.get("material_logical_asset_ids", ()))
                != expected_hero["material_logical_asset_ids"]
            ):
                _external_fail(f"forge external semantic placement {placement_id} differs")
            seen_semantic.add(semantic_target)
        elif (
            placement_id not in expected_dressing_by_room[room_id]
            or placement_id in seen_dressing
            or semantic_target is not None
            or not isinstance(anchor_id, str)
            or PRESENTATION_EXTERNAL_ID_RE.fullmatch(anchor_id) is None
            or mode != "external_blend"
            or placement.get("source_logical_asset_id")
            != PRESENTATION_EXTERNAL_DRESSING_SOURCES.get(placement_id)
            or placement.get("geometry_recipe") is not None
            or placement.get("material_logical_asset_ids") != []
        ):
            _external_fail(f"forge external dressing placement {placement_id} differs")
        else:
            seen_dressing.add(placement_id)
        source_id = placement.get("source_logical_asset_id")
        material_ids = placement.get("material_logical_asset_ids")
        source_tree = placement.get("source_tree_sha256")
        if (
            not isinstance(material_ids, list)
            or any(not isinstance(item, str) for item in material_ids)
            or len(material_ids) != len(set(material_ids))
            or any(item not in source_by_id for item in material_ids)
        ):
            _external_fail(f"forge external placement {placement_id} material sources differ")
        if mode == "external_blend":
            if (
                not isinstance(source_id, str)
                or source_id not in source_by_id
                or source_by_id[source_id]["asset_type"] != "model"
                or material_ids
                or placement.get("geometry_recipe") is not None
                or source_tree != source_by_id[source_id]["source_tree_sha256"]
            ):
                _external_fail(f"forge external model placement {placement_id} differs")
            used_source_ids.add(source_id)
        elif (
            source_id is not None
            or not isinstance(placement.get("geometry_recipe"), str)
            or not material_ids
            or source_tree is not None
            or any(source_by_id[item]["asset_type"] != "texture" for item in material_ids)
        ):
            _external_fail(f"forge project-authored placement {placement_id} differs")
        else:
            used_source_ids.update(material_ids)
        support = placement.get("support_placement_id")
        if support is not None and (
            not isinstance(support, str)
            or PRESENTATION_EXTERNAL_ID_RE.fullmatch(support) is None
        ):
            _external_fail(f"forge external placement {placement_id} support differs")
        seen_placement_ids.add(placement_id)
        placements_by_room[room_id].append(placement)
    placements_by_id = {
        placement["placement_id"]: placement
        for room_placements in placements_by_room.values()
        for placement in room_placements
    }
    for placement in placements_by_id.values():
        support = placement["support_placement_id"]
        if support is not None and (
            support not in placements_by_id
            or placements_by_id[support]["room_id"] != placement["room_id"]
            or support == placement["placement_id"]
        ):
            _external_fail(f"forge external placement {placement['placement_id']} support differs")
    if (
        seen_semantic != set(semantic_ids)
        or seen_dressing != set(dressing_ids)
        or used_source_ids != set(source_by_id)
    ):
        _external_fail("forge external placements do not exactly cover IDs and sources")
    return {
        "value": external,
        "acquisition": acquisition,
        "source_by_id": source_by_id,
        "placements_by_room": placements_by_room,
        "semantic_by_room": expected_semantic_by_room,
        "dressing_by_room": expected_dressing_by_room,
    }


def _validate_bundle_external_content(
    value: Any,
    *,
    room_id: str,
    external: Mapping[str, Any],
) -> dict[str, Any]:
    if (
        not isinstance(value, Mapping)
        or set(value) != PRESENTATION_EXTERNAL_CONTENT_KEYS
    ):
        _external_fail(f"presentation bundle {room_id} external_content fields differ")
    content = copy.deepcopy(dict(value))
    external_value = external["value"]
    if (
        content.get("schema_version") != PRESENTATION_EXTERNAL_PLACEMENT_SCHEMA
        or content.get("normalization_policy")
        != PRESENTATION_EXTERNAL_NORMALIZATION_POLICY
        or content.get("acquisition_receipt") != external["acquisition"]
        or content.get("placement_manifest_sha256")
        != external_value["placement_manifest_sha256"]
        or content.get("placement_plan_sha256") != external_value["content_digest"]
    ):
        _external_fail(f"presentation bundle {room_id} external policy or hashes differ")
    _validate_external_acquisition_reference(
        content.get("acquisition_receipt"),
        f"presentation bundle {room_id} acquisition receipt",
    )
    _external_string_inventory(
        content.get("semantic_target_ids"),
        f"presentation bundle {room_id} semantic targets",
        expected=external["semantic_by_room"][room_id],
    )
    _external_string_inventory(
        content.get("dressing_ids"),
        f"presentation bundle {room_id} dressing IDs",
        expected=external["dressing_by_room"][room_id],
    )
    used_source_ids = {
        logical_id
        for placement in external["placements_by_room"][room_id]
        for logical_id in (
            ([placement["source_logical_asset_id"]]
             if placement["source_logical_asset_id"] is not None else [])
            + list(placement["material_logical_asset_ids"])
        )
    }
    expected_sources = [
        external["source_by_id"][logical_id]
        for logical_id in sorted(used_source_ids)
    ]
    _validate_external_asset_sources(
        content.get("asset_sources"),
        f"presentation bundle {room_id} asset sources",
    )
    if content.get("asset_sources") != expected_sources:
        _external_fail(f"presentation bundle {room_id} source inventory differs")
    return content


def _identity_gltf_node(node: Mapping[str, Any]) -> bool:
    identity_matrix = (
        1.0, 0.0, 0.0, 0.0,
        0.0, 1.0, 0.0, 0.0,
        0.0, 0.0, 1.0, 0.0,
        0.0, 0.0, 0.0, 1.0,
    )
    matrix = node.get("matrix")
    if matrix is not None and (
        not isinstance(matrix, list)
        or len(matrix) != 16
        or any(
            isinstance(number, bool)
            or not isinstance(number, (int, float))
            or not math.isfinite(float(number))
            or abs(float(number) - identity_matrix[index]) > 1e-6
            for index, number in enumerate(matrix)
        )
    ):
        return False
    identity_vectors = {
        "translation": (0.0, 0.0, 0.0),
        "rotation": (0.0, 0.0, 0.0, 1.0),
        "scale": (1.0, 1.0, 1.0),
    }
    for key, expected in identity_vectors.items():
        vector = node.get(key)
        if vector is None:
            continue
        if (
            not isinstance(vector, list)
            or len(vector) != len(expected)
            or any(
                isinstance(number, bool)
                or not isinstance(number, (int, float))
                or not math.isfinite(float(number))
                or abs(float(number) - expected[index]) > 1e-6
                for index, number in enumerate(vector)
            )
        ):
            return False
    return True


def _load_presentation_glb(path: Path) -> dict[str, Any]:
    """Read only the bounded GLB JSON graph used by the host-side gate."""

    size = path.stat().st_size
    if size < 20:
        _fail("VISTA_HOME_PRESENTATION_GLB_INVALID", "presentation GLB is truncated", pointer=str(path))
    with path.open("rb") as source:
        header = source.read(12)
        if len(header) != 12:
            _fail("VISTA_HOME_PRESENTATION_GLB_INVALID", "presentation GLB header is truncated", pointer=str(path))
        magic, version, declared_size = struct.unpack("<III", header)
        if magic != 0x46546C67 or version != 2 or declared_size != size:
            _fail("VISTA_HOME_PRESENTATION_GLB_INVALID", "presentation source is not an exact GLB 2.0 container", pointer=str(path))
        offset = 12
        json_payload: bytes | None = None
        while offset < declared_size:
            chunk_header = source.read(8)
            if len(chunk_header) != 8:
                _fail("VISTA_HOME_PRESENTATION_GLB_INVALID", "presentation GLB chunk header is truncated", pointer=str(path))
            chunk_length, chunk_type = struct.unpack("<II", chunk_header)
            offset += 8
            if chunk_length % 4 != 0 or offset + chunk_length > declared_size:
                _fail("VISTA_HOME_PRESENTATION_GLB_INVALID", "presentation GLB chunk bounds differ", pointer=str(path))
            if chunk_type == 0x4E4F534A:
                if json_payload is not None or chunk_length <= 0 or chunk_length > 16 * 1024 * 1024:
                    _fail("VISTA_HOME_PRESENTATION_GLB_INVALID", "presentation GLB JSON chunk differs", pointer=str(path))
                json_payload = source.read(chunk_length)
                if len(json_payload) != chunk_length:
                    _fail("VISTA_HOME_PRESENTATION_GLB_INVALID", "presentation GLB JSON chunk is truncated", pointer=str(path))
            else:
                source.seek(chunk_length, os.SEEK_CUR)
            offset += chunk_length
        if offset != declared_size or json_payload is None:
            _fail("VISTA_HOME_PRESENTATION_GLB_INVALID", "presentation GLB lacks one complete JSON chunk", pointer=str(path))
    try:
        document = json.loads(
            json_payload.rstrip(b" \t\r\n\x00").decode("utf-8", "strict"),
            object_pairs_hook=_duplicate_object,
            parse_constant=_reject_constant,
        )
    except BuildHomeError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        _fail("VISTA_HOME_PRESENTATION_GLB_INVALID", "presentation GLB JSON is invalid", pointer=str(path))
        raise AssertionError from exc
    if not isinstance(document, dict):
        _fail("VISTA_HOME_PRESENTATION_GLB_INVALID", "presentation GLB JSON root is not an object", pointer=str(path))
    _assert_finite(document)
    return document


def _validate_presentation_glb(
    path: Path,
    record: Mapping[str, Any],
) -> None:
    document = _load_presentation_glb(path)
    is_external = "external_content" in record
    expected_bundle_contract = (
        "one_room_one_mesh_v2" if is_external else "one_room_one_mesh_v1"
    )
    meshes = document.get("meshes")
    nodes = document.get("nodes")
    materials = document.get("materials")
    textures = document.get("textures")
    if not all(isinstance(value, list) for value in (meshes, nodes, materials, textures)):
        _fail("VISTA_HOME_PRESENTATION_GLB_INVALID", "presentation GLB inventories are not arrays", pointer=str(path))
    assert isinstance(meshes, list) and isinstance(nodes, list)
    assert isinstance(materials, list) and isinstance(textures, list)
    mesh_nodes = [
        (index, node)
        for index, node in enumerate(nodes)
        if isinstance(node, Mapping)
        and isinstance(node.get("mesh"), int)
        and not isinstance(node.get("mesh"), bool)
    ]
    bundle_nodes = [
        (index, node)
        for index, node in enumerate(nodes)
        if isinstance(node, Mapping)
        and isinstance(node.get("extras"), Mapping)
        and node["extras"].get("vista_bundle_contract")
        == expected_bundle_contract
    ]
    scenes = document.get("scenes")
    active_scene_index = document.get("scene")
    if (
        len(meshes) != 1
        or len(mesh_nodes) != 1
        or len(bundle_nodes) != 1
        or not isinstance(scenes, list)
        or isinstance(active_scene_index, bool)
        or not isinstance(active_scene_index, int)
        or not 0 <= active_scene_index < len(scenes)
    ):
        _fail("VISTA_HOME_PRESENTATION_GLB_INVALID", "presentation GLB is not one identity-root mesh", pointer=str(path))
    mesh_node_index, mesh_node = mesh_nodes[0]
    bundle_node_index, bundle_node = bundle_nodes[0]
    active_scene = scenes[active_scene_index]
    active_roots = active_scene.get("nodes") if isinstance(active_scene, Mapping) else None
    has_parent = any(
        isinstance(node, Mapping)
        and isinstance(node.get("children"), list)
        and mesh_node_index in node["children"]
        for node in nodes
    )
    if (
        mesh_node_index != bundle_node_index
        or mesh_node.get("mesh") != 0
        or not isinstance(active_roots, list)
        or len(active_roots) != 1
        or isinstance(active_roots[0], bool)
        or active_roots[0] != mesh_node_index
        or has_parent
        or not _identity_gltf_node(mesh_node)
    ):
        _fail("VISTA_HOME_PRESENTATION_GLB_INVALID", "presentation GLB active scene root identity differs", pointer=str(path))
    if document.get("cameras") not in (None, []):
        _fail("VISTA_HOME_PRESENTATION_GLB_INVALID", "presentation GLB contains a camera", pointer=str(path))
    extensions = document.get("extensions", {})
    punctual = extensions.get("KHR_lights_punctual", {}) if isinstance(extensions, Mapping) else {}
    if isinstance(punctual, Mapping) and punctual.get("lights") not in (None, []):
        _fail("VISTA_HOME_PRESENTATION_GLB_INVALID", "presentation GLB contains a light", pointer=str(path))

    if len(materials) != record["material_count"] or len(textures) != record["texture_count"]:
        _fail("VISTA_HOME_PRESENTATION_GLB_INVALID", "presentation GLB material or texture count differs", pointer=str(path))
    used_material_indices: set[int] = set()
    primitives = meshes[0].get("primitives") if isinstance(meshes[0], Mapping) else None
    if not isinstance(primitives, list) or not primitives:
        _fail("VISTA_HOME_PRESENTATION_GLB_INVALID", "presentation GLB has no mesh primitives", pointer=str(path))
    for primitive in primitives:
        material_index = primitive.get("material") if isinstance(primitive, Mapping) else None
        if (
            not isinstance(material_index, int)
            or isinstance(material_index, bool)
            or not 0 <= material_index < len(materials)
        ):
            _fail("VISTA_HOME_PRESENTATION_GLB_INVALID", "presentation GLB primitive material is invalid", pointer=str(path))
        used_material_indices.add(material_index)
    if used_material_indices != set(range(len(materials))):
        _fail("VISTA_HOME_PRESENTATION_GLB_INVALID", "presentation GLB has unused or missing material slots", pointer=str(path))

    glb_material_names: list[str] = []
    for index, material in enumerate(materials):
        if not isinstance(material, Mapping):
            _fail("VISTA_HOME_PRESENTATION_GLB_INVALID", f"presentation material {index} is invalid", pointer=str(path))
        name = material.get("name")
        folded_name = str(name or "").replace("_", "").replace("-", "").casefold()
        if not isinstance(name, str) or not name or "defaultmaterial" in folded_name or "basicshapematerial" in folded_name:
            _fail("VISTA_HOME_PRESENTATION_DEFAULT_MATERIAL", f"presentation material {index} is default/basic", pointer=str(path))
        glb_material_names.append(name)
        pbr = material.get("pbrMetallicRoughness")
        bindings = (
            pbr.get("baseColorTexture") if isinstance(pbr, Mapping) else None,
            pbr.get("metallicRoughnessTexture") if isinstance(pbr, Mapping) else None,
            material.get("normalTexture"),
        )
        for binding in bindings:
            texture_index = binding.get("index") if isinstance(binding, Mapping) else None
            if (
                not isinstance(texture_index, int)
                or isinstance(texture_index, bool)
                or not 0 <= texture_index < len(textures)
            ):
                _fail("VISTA_HOME_PRESENTATION_GLB_INVALID", f"presentation material {index} lacks complete PBR texture bindings", pointer=str(path))
    if is_external and sorted(glb_material_names) != record["material_ids"]:
        _fail(
            "VISTA_HOME_PRESENTATION_GLB_INVALID",
            "presentation external material names differ from their exact receipt inventory",
            pointer=str(path),
        )

    metadata = bundle_node["extras"]
    try:
        embedded_transform = json.loads(str(metadata.get("vista_expected_world_transform_cm_json")))
        embedded_material_ids = json.loads(str(metadata.get("vista_material_ids_json")))
        embedded_external = (
            json.loads(str(metadata.get("vista_external_content_json")))
            if is_external
            else None
        )
    except json.JSONDecodeError as exc:
        _fail("VISTA_HOME_PRESENTATION_GLB_INVALID", "presentation GLB extras contain invalid JSON", pointer=str(path))
        raise AssertionError from exc
    expected_metadata = {
        "vista_artifact_id": record["artifact_id"],
        "vista_target_asset_id": record["target_asset_id"],
        "vista_room_id": record["room_id"],
        "vista_room_kind": record["room_kind"],
        "vista_root_transform_policy": record["root_transform_policy"],
        "vista_semantic_policy": record["semantic_policy"],
        "vista_collision_policy": record["collision_policy"],
        "vista_unreal_collision_profile": record["unreal_collision_profile"],
        "vista_source_house_sha256": record["source_hashes"]["house_sha256"],
        "vista_source_visual_profile_sha256": record["source_hashes"]["visual_profile_sha256"],
        "vista_source_forge_plan_sha256": record["source_hashes"]["forge_plan_sha256"],
    }
    if (
        any(metadata.get(key) != value for key, value in expected_metadata.items())
        or embedded_transform != record["expected_world_transform_cm"]
        or embedded_material_ids != record["material_ids"]
        or (is_external and embedded_external != record["external_content"])
    ):
        _fail("VISTA_HOME_PRESENTATION_GLB_INVALID", "presentation GLB extras differ from their receipt", pointer=str(path))


def _validate_presentation_staticization(
    manifest_root: Path,
    manifest_ledger: Any,
    artifacts: Sequence[Any],
) -> None:
    """Bind the v2 staticization ledger to its exact retained artifact bytes."""

    matches = [
        item
        for item in artifacts
        if isinstance(item, Mapping)
        and item.get("artifact_id") == PRESENTATION_EXTERNAL_STATICIZATION_ARTIFACT_ID
    ]
    if len(matches) != 1:
        _fail(
            "VISTA_HOME_PRESENTATION_EXTERNAL_INVALID",
            "presentation requires exactly one external staticization artifact",
        )
    record = matches[0]
    if (
        set(record) != PRESENTATION_EXTERNAL_STATICIZATION_ARTIFACT_KEYS
        or record.get("relative_path") != PRESENTATION_EXTERNAL_STATICIZATION_FILENAME
        or record.get("media_type") != "application/json"
    ):
        _fail(
            "VISTA_HOME_PRESENTATION_EXTERNAL_INVALID",
            "presentation external staticization artifact fields differ",
        )
    source = _contained_artifact(
        manifest_root,
        record["relative_path"],
        "presentation external staticization artifact",
    )
    expected_sha256 = _require_sha(
        record.get("sha256"),
        "presentation external staticization artifact SHA-256",
    )
    size_bytes = record.get("size_bytes")
    if (
        isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or size_bytes <= 0
        or source.stat().st_size != size_bytes
        or sha256_file(source) != expected_sha256
    ):
        _fail(
            "VISTA_HOME_BUILD_PIN_MISMATCH",
            "presentation external staticization artifact bytes or SHA-256 differ",
            pointer=str(source),
        )
    ledger, raw = _load_json(
        source,
        expected_sha256=expected_sha256,
        label="presentation external staticization artifact",
    )
    if raw != canonical_json(ledger) or ledger != manifest_ledger:
        _fail(
            "VISTA_HOME_PRESENTATION_EXTERNAL_INVALID",
            "presentation external staticization ledger differs from its manifest",
        )
    try:
        realism_external_assets.validate_external_staticization_ledger(ledger)
    except Exception as exc:
        _fail(
            "VISTA_HOME_PRESENTATION_EXTERNAL_INVALID",
            "presentation external staticization ledger is invalid",
        )
        raise AssertionError from exc


def validate_presentation_inputs(
    manifest_path: Path,
    manifest_sha256: str,
    artifact_receipt_path: Path,
    artifact_receipt_sha256: str,
    plan: Mapping[str, Any],
    visual_profile: Mapping[str, Any],
) -> PresentationInputs:
    """Bind manifest, receipt, exact three-room inventory, and GLB bytes."""

    manifest, manifest_raw = _load_json(
        manifest_path,
        expected_sha256=manifest_sha256,
        label="presentation manifest",
    )
    receipt, receipt_raw = _load_json(
        artifact_receipt_path,
        expected_sha256=artifact_receipt_sha256,
        label="presentation artifact receipt",
    )
    if manifest_raw != canonical_json(manifest) or receipt_raw != canonical_json(receipt):
        _fail("VISTA_HOME_PRESENTATION_INVALID", "presentation contracts must be canonical JSON")
    manifest_root = _existing_directory(manifest_path.parent, "presentation output root")
    receipt_root = _existing_directory(artifact_receipt_path.parent, "presentation receipt root")
    if manifest_root != receipt_root:
        _fail("VISTA_HOME_PRESENTATION_INVALID", "presentation manifest and receipt must share one output root")
    manifest_schema = manifest.get("schema_version")
    receipt_schema = receipt.get("schema_version")
    schema_pair = (manifest_schema, receipt_schema)
    if schema_pair not in {
        (PRESENTATION_FORGE_SCHEMA, PRESENTATION_ARTIFACT_RECEIPT_SCHEMA),
        (PRESENTATION_FORGE_SCHEMA_V2, PRESENTATION_ARTIFACT_RECEIPT_SCHEMA_V2),
    }:
        _fail(
            "VISTA_HOME_PRESENTATION_SOURCE_MISMATCH",
            "presentation forge and artifact receipt schemas are not one matched v1/v2 pair",
        )
    is_external = manifest_schema == PRESENTATION_FORGE_SCHEMA_V2
    if is_external and set(manifest) != PRESENTATION_EXTERNAL_MANIFEST_KEYS:
        _fail(
            "VISTA_HOME_PRESENTATION_INVALID",
            "presentation external forge manifest fields differ",
        )
    if (
        manifest_schema not in {PRESENTATION_FORGE_SCHEMA, PRESENTATION_FORGE_SCHEMA_V2}
        or manifest.get("house_revision") != plan["house"]["revision"]
        or manifest.get("visual_profile_id") != visual_profile["visual_profile_id"]
        or manifest.get("source_house_digest") != plan["house"]["content_digest"]
        or manifest.get("source_profile_digest") != visual_profile["content_digest"]
        or SHA256_RE.fullmatch(str(manifest.get("forge_plan_digest", ""))) is None
    ):
        _fail("VISTA_HOME_PRESENTATION_SOURCE_MISMATCH", "presentation manifest source identity differs")
    if set(receipt) != {"schema_version", "artifacts", "ue_import_bundles"}:
        _fail("VISTA_HOME_PRESENTATION_INVALID", "presentation artifact receipt fields or schema differ")
    manifest_bundles = manifest.get("ue_import_bundles")
    receipt_bundles = receipt.get("ue_import_bundles")
    artifacts = receipt.get("artifacts")
    if not isinstance(manifest_bundles, list) or not isinstance(receipt_bundles, list) or not isinstance(artifacts, list):
        _fail("VISTA_HOME_PRESENTATION_INVALID", "presentation bundle inventories must be arrays")
    if is_external:
        _validate_presentation_staticization(
            manifest_root,
            manifest.get("external_staticization"),
            artifacts,
        )
    artifact_bundles = [
        item for item in artifacts
        if isinstance(item, Mapping) and item.get("artifact_kind") == planning.PRESENTATION_ARTIFACT_KIND
    ]
    if (
        manifest_bundles != receipt_bundles
        or receipt_bundles != artifact_bundles
        or len(receipt_bundles) != len(planning.PRESENTATION_ROOM_KINDS)
    ):
        _fail("VISTA_HOME_PRESENTATION_INVENTORY_MISMATCH", "manifest, receipt, and artifact bundle inventories differ")

    rooms_by_id = {room["room_id"]: room for room in plan["rooms"]}
    expected_room_ids = {
        room_id for room_id, room in rooms_by_id.items()
        if room.get("kind") in set(planning.PRESENTATION_ROOM_KINDS)
    }
    if set(visual_profile.get("finished_room_ids", [])) != expected_room_ids:
        _fail("VISTA_HOME_PRESENTATION_INVENTORY_MISMATCH", "visual profile finished rooms differ from the presentation contract")
    external_contract: dict[str, Any] | None = None
    if is_external:
        if expected_room_ids != set(PRESENTATION_EXTERNAL_SEMANTIC_TARGETS_BY_ROOM):
            _external_fail("presentation external room slice differs from the fixed contract")
        external_contract = _validate_external_placement_contract(
            manifest.get("external_placement"),
            plan,
            expected_room_ids,
        )
    source_hashes = {
        "house_sha256": plan["house"]["content_digest"],
        "visual_profile_sha256": visual_profile["content_digest"],
        "forge_plan_sha256": manifest["forge_plan_digest"],
    }
    seen_rooms: set[str] = set()
    seen_artifacts: set[str] = set()
    seen_targets: set[str] = set()
    seen_paths: set[str] = set()
    bindings: list[dict[str, Any]] = []
    for index, raw_record in enumerate(receipt_bundles):
        expected_record_keys = (
            PRESENTATION_BUNDLE_RECORD_KEYS_V2
            if is_external
            else PRESENTATION_BUNDLE_RECORD_KEYS
        )
        if not isinstance(raw_record, Mapping) or set(raw_record) != expected_record_keys:
            _fail("VISTA_HOME_PRESENTATION_INVALID", f"presentation bundle {index} fields differ")
        record = copy.deepcopy(dict(raw_record))
        room_id = record.get("room_id")
        room_kind = record.get("room_kind")
        _safe_relative_path(
            record.get("relative_path"),
            f"presentation bundle {index}",
        )
        room = rooms_by_id.get(room_id)
        expected_relative = f"ue_import_bundles/{room_kind}_presentation_bundle.glb"
        if (
            room is None
            or room_id not in expected_room_ids
            or room_id in seen_rooms
            or room_kind != room.get("kind")
            or room_kind not in planning.PRESENTATION_ROOM_KINDS
            or record.get("artifact_id") != f"ue_bundle.room.{room_kind}"
            or record.get("artifact_id") in seen_artifacts
            or record.get("artifact_kind") != planning.PRESENTATION_ARTIFACT_KIND
            or record.get("target_asset_id") != room["bundle"]["asset_id"]
            or record.get("target_asset_id") != f"asset.bundle.{room_kind}"
            or record.get("target_asset_id") in seen_targets
            or record.get("relative_path") != expected_relative
            or record.get("relative_path") in seen_paths
            or record.get("media_type") != "model/gltf-binary"
        ):
            _fail("VISTA_HOME_PRESENTATION_INVALID", f"presentation bundle {index} identity differs")
        source = _contained_artifact(manifest_root, record["relative_path"], f"presentation bundle {index}")
        expected_sha = _require_sha(record.get("sha256"), f"presentation bundle {index} SHA-256")
        size = record.get("size_bytes")
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size <= 0
            or source.stat().st_size != size
            or sha256_file(source) != expected_sha
        ):
            _fail("VISTA_HOME_BUILD_PIN_MISMATCH", f"presentation bundle {index} bytes or SHA-256 differ", pointer=str(source))
        material_ids = record.get("material_ids")
        integer_contract = {
            "mesh_count": 1,
            "material_count": 2,
            "pbr_complete_material_count": 2,
            "texture_count": 3 if is_external else 6,
        }
        if any(
            isinstance(record.get(key), bool)
            or not isinstance(record.get(key), int)
            or record[key] < minimum
            for key, minimum in integer_contract.items()
        ) or (
            record["mesh_count"] != 1
            or not isinstance(material_ids, list)
            or len(material_ids) < 2
            or material_ids != sorted(set(material_ids))
            or any(
                not isinstance(item, str)
                or (
                    is_external
                    and (
                        not item
                        or len(item) > 256
                        or item != item.strip()
                        or any(ord(character) < 32 for character in item)
                    )
                )
                or (not is_external and not item.startswith("r2."))
                for item in material_ids
            )
            or record["material_count"] != len(material_ids)
            or record["pbr_complete_material_count"] != record["material_count"]
            or record["texture_count"]
            < (3 if is_external else record["material_count"] * 3)
        ):
            _fail("VISTA_HOME_PRESENTATION_INVALID", f"presentation bundle {index} mesh/material inventory differs")
        expected_world = _presentation_transform(
            record.get("expected_world_transform_cm"),
            f"presentation bundle {index} world transform",
            location_key="location_cm",
        )
        _presentation_transform(
            record.get("bundle_root_transform"),
            f"presentation bundle {index} root transform",
            location_key="location_m",
        )
        if (
            expected_world != room["world_transform_cm"]
            or record["bundle_root_transform"] != {
                "location_m": [0, 0, 0],
                "rotation_deg": [0, 0, 0],
                "scale": [1, 1, 1],
            }
            or record.get("root_transform_policy") != planning.PRESENTATION_ROOT_TRANSFORM_POLICY
            or record.get("semantic_policy") != planning.PRESENTATION_SEMANTIC_POLICY
            or record.get("collision_policy") != planning.PRESENTATION_COLLISION_POLICY
            or record.get("unreal_collision_profile") != planning.PRESENTATION_UNREAL_COLLISION_PROFILE
            or record.get("cameras_exported") is not False
            or record.get("lights_exported") is not False
            or record.get("source_hashes") != source_hashes
        ):
            _fail("VISTA_HOME_PRESENTATION_SOURCE_MISMATCH", f"presentation bundle {index} transform, policy, or source hashes differ")
        if is_external:
            assert external_contract is not None
            _validate_bundle_external_content(
                record.get("external_content"),
                room_id=room_id,
                external=external_contract,
            )
        _validate_presentation_glb(source, record)
        bindings.append({
            **record,
            "source_file": str(source),
            "source_file_sha256": expected_sha,
        })
        seen_rooms.add(room_id)
        seen_artifacts.add(record["artifact_id"])
        seen_targets.add(record["target_asset_id"])
        seen_paths.add(record["relative_path"])
    if seen_rooms != expected_room_ids:
        _fail("VISTA_HOME_PRESENTATION_INVENTORY_MISMATCH", "presentation bundles do not cover the exact three-room slice")
    return PresentationInputs(
        manifest=manifest,
        manifest_raw=manifest_raw,
        artifact_receipt=receipt,
        artifact_receipt_raw=receipt_raw,
        bindings=tuple(sorted(bindings, key=lambda item: item["room_id"])),
    )


def validate_build_plan(path: Path, expected_sha256: str, expected_revision: str) -> dict[str, Any]:
    plan, raw = _load_json(path, expected_sha256=expected_sha256, label="build plan")
    if raw != planning.canonical_json(plan):
        _fail("VISTA_HOME_BUILD_PLAN_NONCANONICAL", "build plan bytes must be canonical", pointer=str(path))
    try:
        world_contract.validate_build_plan(plan)
        planning.build_composition_spec(plan)
    except (world_contract.PlayableHomeContractError, planning.VistaPlayableHomePlanError) as exc:
        _fail("VISTA_HOME_BUILD_PLAN_INVALID", str(exc), pointer=str(path))
    revision = plan.get("house", {}).get("revision")
    if revision != expected_revision:
        _fail("VISTA_HOME_BUILD_REVISION_MISMATCH", "build plan revision differs from the requested revision")
    expected_namespace = f"/Game/VISTA/PlayableHome/{expected_revision}"
    if plan["unreal"]["content_namespace"] != expected_namespace or plan["unreal"]["map_path"] != expected_namespace + "/Maps/VistaPlayableHome":
        _fail("VISTA_HOME_BUILD_REVISION_MISMATCH", "build plan namespace is not bound to the requested revision")
    return plan


def _visual_profile_house_view(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Project a validated build plan into the pure VisualProfile house view.

    The visual-profile contract needs only the immutable house identity,
    world-space room bounds, and semantic entity IDs.  Reconstructing that
    view from the already pinned build plan avoids a second, unbound HouseSpec
    filesystem input while preserving the exact source-house digest.
    """

    rooms: list[dict[str, Any]] = []
    for raw_room in plan["rooms"]:
        bounds = raw_room["world_bounds_cm"]
        rooms.append({
            "room_id": raw_room["room_id"],
            "transform": {
                "location_m": [0.0, 0.0, 0.0],
                "scale": [1.0, 1.0, 1.0],
            },
            "bounds_m": {
                "min_m": [float(value) / 100.0 for value in bounds["min_cm"]],
                "max_m": [float(value) / 100.0 for value in bounds["max_cm"]],
            },
        })
    return {
        "revision": plan["house"]["revision"],
        "content_digest": plan["house"]["content_digest"],
        "rooms": rooms,
        "entities": [
            {"entity_id": raw_entity["entity_id"]}
            for raw_entity in plan["entities"]
        ],
    }


def validate_visual_profile(
    path: Path,
    expected_sha256: str,
    plan: Mapping[str, Any],
) -> tuple[dict[str, Any], bytes]:
    """Load one absolute, byte-pinned r2 profile and validate it fail closed."""

    profile, raw = _load_json(
        path,
        expected_sha256=expected_sha256,
        label="visual profile",
    )
    try:
        visual_profile_contract.validate_profile(
            profile,
            _visual_profile_house_view(plan),
        )
    except visual_profile_contract.VisualProfileContractError as exc:
        _fail(
            "VISTA_HOME_BUILD_VISUAL_PROFILE_INVALID",
            str(exc),
            pointer=str(path),
        )
    return profile, raw


def validate_blender_manifest(
    path: Path,
    expected_sha256: str,
    plan: Mapping[str, Any],
) -> BlenderInputs:
    manifest, raw = _load_json(path, expected_sha256=expected_sha256, label="Blender build manifest")
    if raw != blender_contract.canonical_json_bytes(manifest):
        _fail("VISTA_HOME_BUILD_MANIFEST_INVALID", "Blender build manifest is not canonical", pointer=str(path))
    expected_keys = {
        "schema_version",
        "house_id",
        "revision",
        "source_house_digest",
        "normalized_manifest_digest",
        "build",
        "outputs",
        "asset_artifacts",
    }
    if set(manifest) != expected_keys or manifest.get("schema_version") != BLENDER_BUILD_RECEIPT_SCHEMA:
        _fail("VISTA_HOME_BUILD_MANIFEST_INVALID", "Blender build manifest fields or schema differ")
    house = plan["house"]
    if (
        manifest.get("house_id") != house["house_id"]
        or manifest.get("revision") != house["revision"]
        or manifest.get("source_house_digest") != house["content_digest"]
    ):
        _fail("VISTA_HOME_BUILD_REVISION_MISMATCH", "Blender build manifest disagrees with the build plan")
    root = _existing_directory(path.parent, "Blender output root")
    outputs = manifest.get("outputs")
    expected_outputs = {"blend", "glb", "normalized_manifest", "preview_interior", "preview_overview"}
    if not isinstance(outputs, Mapping) or set(outputs) != expected_outputs:
        _fail("VISTA_HOME_BUILD_MANIFEST_INVALID", "Blender output inventory differs")
    output_paths = {
        name: _validate_output_entry(entry, root, f"Blender output {name}")
        for name, entry in outputs.items()
    }
    normalized_path = output_paths["normalized_manifest"]
    normalized, normalized_raw = _load_json(
        normalized_path,
        expected_sha256=outputs["normalized_manifest"]["sha256"],
        label="normalized Blender manifest",
    )
    if normalized_raw != blender_contract.canonical_json_bytes(normalized):
        _fail("VISTA_HOME_BUILD_MANIFEST_INVALID", "normalized Blender manifest is not canonical")
    normalized_body = copy.deepcopy(normalized)
    normalized_digest = normalized_body.pop("content_digest", None)
    if not isinstance(normalized_digest, str) or normalized_digest != sha256_bytes(
        blender_contract.canonical_json_bytes(normalized_body)
    ):
        _fail("VISTA_HOME_BUILD_MANIFEST_INVALID", "normalized Blender content digest differs")
    if (
        normalized.get("schema_version") != blender_contract.MANIFEST_SCHEMA
        or normalized.get("house_id") != house["house_id"]
        or normalized.get("revision") != house["revision"]
        or normalized.get("source_house")
        != {"schema_version": world_contract.HOUSE_SCHEMA_VERSION, "content_digest": house["content_digest"]}
        or manifest.get("normalized_manifest_digest") != normalized_digest
    ):
        _fail("VISTA_HOME_BUILD_REVISION_MISMATCH", "normalized Blender manifest disagrees with the build plan")

    plan_entities = {item["entity_id"]: item for item in plan["entities"]}
    normalized_entities = normalized.get("entities")
    if not isinstance(normalized_entities, list) or {item.get("entity_id") for item in normalized_entities if isinstance(item, Mapping)} != set(plan_entities):
        _fail("VISTA_HOME_BUILD_MANIFEST_INVALID", "normalized semantic entity inventory differs")
    for item in normalized_entities:
        source = plan_entities[item["entity_id"]]
        if item.get("asset_ref") != source["asset"]["asset_id"] or item.get("room_id") != source["room_id"]:
            _fail("VISTA_HOME_BUILD_MANIFEST_INVALID", "normalized semantic binding differs from build plan")

    declared = {asset["asset_id"]: asset for asset in plan["assets"]}
    expected_nonbuiltin = {asset_id for asset_id, asset in declared.items() if asset["source_kind"] != "builtin"}
    entries = manifest.get("asset_artifacts")
    if not isinstance(entries, Mapping) or set(entries) != expected_nonbuiltin:
        _fail("VISTA_HOME_BUILD_BINDING_INCOMPLETE", "Blender artifact IDs are not the exact non-builtin set")
    artifacts: dict[str, tuple[Path, str]] = {}
    artifact_keys = {"path", "sha256", "bytes", "media_type", "mesh_count", "source_node_ids"}
    for asset_id in sorted(expected_nonbuiltin):
        entry = entries[asset_id]
        if not isinstance(entry, Mapping) or set(entry) != artifact_keys:
            _fail("VISTA_HOME_BUILD_MANIFEST_INVALID", f"Blender artifact {asset_id} fields differ")
        if entry.get("media_type") != "model/gltf-binary" or entry.get("mesh_count") != 1:
            _fail("VISTA_HOME_BUILD_MANIFEST_INVALID", f"Blender artifact {asset_id} is not one GLB mesh")
        node_ids = entry.get("source_node_ids")
        if not isinstance(node_ids, list) or not node_ids or len(node_ids) != len(set(node_ids)) or not all(isinstance(item, str) and item for item in node_ids):
            _fail("VISTA_HOME_BUILD_MANIFEST_INVALID", f"Blender artifact {asset_id} source nodes are invalid")
        source = _contained_artifact(root, entry["path"], f"Blender artifact {asset_id}")
        expected = _require_sha(entry.get("sha256"), f"Blender artifact {asset_id} SHA-256")
        size = entry.get("bytes")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            _fail("VISTA_HOME_BUILD_MANIFEST_INVALID", f"Blender artifact {asset_id} bytes is invalid")
        if source.stat().st_size != size or sha256_file(source) != expected:
            _fail("VISTA_HOME_BUILD_PIN_MISMATCH", f"Blender artifact {asset_id} bytes or SHA-256 differ")
        artifacts[asset_id] = (source, expected)
    return BlenderInputs(manifest=manifest, normalized=normalized, artifacts=artifacts)


def _content_digest(value: Mapping[str, Any]) -> str:
    body = copy.deepcopy(dict(value))
    body.pop("content_digest", None)
    return sha256_bytes(canonical_json(body))


def validate_visual_binding_manifest(
    path: Path,
    expected_sha256: str,
    plan: Mapping[str, Any],
    normalized_manifest_digest: str,
) -> dict[str, tuple[Path, str]]:
    visual, raw = _load_json(path, expected_sha256=expected_sha256, label="visual binding manifest")
    try:
        hssd_contract.validate_built_manifest(visual)
    except hssd_contract.HssdBindingError as exc:
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", str(exc), pointer=str(path))
    if raw != hssd_contract.canonical_json_bytes(visual):
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual binding manifest is not canonical", pointer=str(path))
    expected_top = {
        "schema_version",
        "house_id",
        "revision",
        "source_plan",
        "dataset",
        "license_receipt",
        "blender",
        "builder_source",
        "normalization_policy",
        "closed_world",
        "outputs",
        "content_digest",
    }
    if set(visual) != expected_top or visual.get("schema_version") != HSSD_MANIFEST_SCHEMA:
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual binding schema or fields differ")
    if visual.get("content_digest") != hssd_contract.content_digest(visual):
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual binding content digest differs")
    if visual.get("house_id") != plan["house"]["house_id"] or visual.get("revision") != plan["house"]["revision"]:
        _fail("VISTA_HOME_BUILD_REVISION_MISMATCH", "visual binding house or revision differs")
    source_plan = visual.get("source_plan")
    if not isinstance(source_plan, Mapping) or set(source_plan) != {"schema_version", "content_digest", "path"}:
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual source plan binding fields differ")
    if source_plan.get("schema_version") != HSSD_BINDING_PLAN_SCHEMA:
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual source plan schema differs")
    source_plan_digest = _require_sha(source_plan.get("content_digest"), "visual source plan content digest")
    root = _existing_directory(path.parent, "visual output root")
    binding_plan_path = _contained_artifact(root, source_plan.get("path"), "visual binding plan")
    binding_plan, binding_raw = _load_json(
        binding_plan_path,
        expected_sha256=sha256_file(binding_plan_path),
        label="visual binding plan",
    )
    try:
        hssd_contract.validate_binding_plan(binding_plan)
    except hssd_contract.HssdBindingError as exc:
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", str(exc), pointer=str(binding_plan_path))
    if (
        binding_raw != hssd_contract.canonical_json_bytes(binding_plan)
        or binding_plan.get("content_digest") != hssd_contract.content_digest(binding_plan)
    ):
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual binding plan is noncanonical or has digest drift")
    binding_plan_keys = {
        "schema_version",
        "house_id",
        "revision",
        "source_normalized_manifest",
        "dataset",
        "license_receipt",
        "selection_policy",
        "mode",
        "closed_world",
        "bindings",
        "preserved_assets",
        "content_digest",
    }
    if set(binding_plan) != binding_plan_keys:
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual binding plan fields differ")
    if (
        binding_plan.get("schema_version") != HSSD_BINDING_PLAN_SCHEMA
        or binding_plan.get("content_digest") != source_plan_digest
        or binding_plan.get("house_id") != plan["house"]["house_id"]
        or binding_plan.get("revision") != plan["house"]["revision"]
        or binding_plan.get("source_normalized_manifest")
        != {
            "schema_version": blender_contract.MANIFEST_SCHEMA,
            "content_digest": normalized_manifest_digest,
        }
    ):
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual binding plan does not bind the normalized forge manifest")
    if binding_plan.get("mode") != "full" or binding_plan.get("closed_world", {}).get("unaccounted_asset_ids") != []:
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual binding plan is not a closed full plan")
    blender = visual.get("blender")
    if not isinstance(blender, Mapping) or blender.get("mode") != "full":
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "only a full visual build may override presentation assets")
    builder_source = visual.get("builder_source")
    if (
        not isinstance(builder_source, Mapping)
        or set(builder_source) != {"repository_commit", "worktree_clean", "source_files"}
        or re.fullmatch(r"[0-9a-f]{40}", str(builder_source.get("repository_commit", ""))) is None
        or builder_source.get("worktree_clean") is not True
        or not isinstance(builder_source.get("source_files"), list)
    ):
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual builder source identity differs")
    builder_files: dict[str, str] = {}
    for record in builder_source["source_files"]:
        if (
            not isinstance(record, Mapping)
            or set(record) != {"path", "sha256"}
            or not isinstance(record.get("path"), str)
            or record["path"] in builder_files
            or SHA256_RE.fullmatch(str(record.get("sha256", ""))) is None
        ):
            _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual builder source file receipt differs")
        builder_files[record["path"]] = record["sha256"]
    if set(builder_files) != set(HSSD_BUILDER_SOURCE_FILES):
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual builder source file inventory differs")
    for relative, expected in builder_files.items():
        source_file = REPO_ROOT / relative
        if source_file.is_symlink() or not source_file.is_file() or sha256_file(source_file) != expected:
            _fail("VISTA_HOME_BUILD_VISUAL_INVALID", f"visual builder source bytes differ: {relative}")
    normalization_policy = visual.get("normalization_policy")
    if (
        not isinstance(normalization_policy, Mapping)
        or set(normalization_policy) != {"maximum_axis_scale_anisotropy"}
        or normalization_policy.get("maximum_axis_scale_anisotropy") != 2.75
    ):
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual normalization policy differs")
    dataset = visual.get("dataset")
    if not isinstance(dataset, Mapping) or not isinstance(dataset.get("license"), Mapping):
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual dataset license is missing")
    license_value = dataset["license"]
    if (
        dataset.get("dataset") != hssd_contract.HSSD_DATASET_NAME
        or dataset.get("project_url") != hssd_contract.HSSD_PROJECT_URL
        or dataset.get("readme_relpath") != "README.md"
        or dataset.get("readme_sha256") != hssd_contract.PINNED_HSSD_README_SHA256
        or re.fullmatch(r"[0-9a-f]{40}", str(dataset.get("dataset_revision", ""))) is None
        or license_value.get("spdx") != "CC-BY-NC-4.0"
        or license_value.get("url") != hssd_contract.HSSD_LICENSE_URL
        or license_value.get("commercial_use") != "prohibited_without_separate_permission"
        or license_value.get("attribution_required") is not True
        or license_value.get("modification_notice_required") is not True
    ):
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual dataset license policy differs")
    license_receipt = visual.get("license_receipt")
    if (
        not isinstance(license_receipt, Mapping)
        or license_receipt.get("accepted_spdx") != "CC-BY-NC-4.0"
        or license_receipt.get("scope") != "research_and_noncommercial_demo_only"
        or license_receipt.get("commercial_release_gate") != "replace_assets_or_obtain_separate_permission"
        or not isinstance(license_receipt.get("attribution_notice"), str)
        or not license_receipt.get("attribution_notice")
    ):
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual attribution receipt is incomplete")
    if binding_plan.get("dataset") != dataset or binding_plan.get("license_receipt") != license_receipt:
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "built attribution differs from the pinned binding plan")
    closed = visual.get("closed_world")
    if not isinstance(closed, Mapping) or set(closed) != {"bound_asset_ids", "unaccounted_asset_ids"}:
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual closed-world fields differ")
    if closed.get("unaccounted_asset_ids") != []:
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual manifest has unaccounted asset IDs")
    bound_ids = closed.get("bound_asset_ids")
    if not isinstance(bound_ids, list) or len(bound_ids) != len(set(bound_ids)) or not all(isinstance(item, str) for item in bound_ids):
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual bound IDs are invalid or duplicated")
    outputs = visual.get("outputs")
    if not isinstance(outputs, list):
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual outputs must be an array")
    logical_ids = [item.get("logical_asset_id") for item in outputs if isinstance(item, Mapping)]
    if len(logical_ids) != len(outputs) or len(logical_ids) != len(set(logical_ids)) or set(logical_ids) != set(bound_ids):
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual output IDs disagree with the closed-world inventory")
    declared = {asset["asset_id"]: asset for asset in plan["assets"]}
    nonbuiltin = {asset_id for asset_id, asset in declared.items() if asset["source_kind"] != "builtin"}
    if not set(logical_ids).issubset(nonbuiltin):
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual output contains an unknown or builtin asset ID")
    plan_closed = binding_plan["closed_world"]
    required_closed_keys = {
        "target_asset_ids",
        "bound_asset_ids",
        "preserved_asset_ids",
        "unaccounted_asset_ids",
    }
    if not isinstance(plan_closed, Mapping) or set(plan_closed) != required_closed_keys:
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual binding plan closed-world fields differ")
    for key in ("target_asset_ids", "bound_asset_ids", "preserved_asset_ids", "unaccounted_asset_ids"):
        values = plan_closed.get(key)
        if not isinstance(values, list) or len(values) != len(set(values)) or not all(isinstance(item, str) for item in values):
            _fail("VISTA_HOME_BUILD_VISUAL_INVALID", f"visual binding plan {key} is invalid or duplicated")
    target_ids = set(plan_closed["target_asset_ids"])
    plan_bound_ids = set(plan_closed["bound_asset_ids"])
    preserved_ids = set(plan_closed["preserved_asset_ids"])
    if (
        not nonbuiltin.issubset(target_ids)
        or not target_ids.issubset(declared)
        or plan_bound_ids != set(logical_ids)
        or target_ids != plan_bound_ids | preserved_ids
        or plan_bound_ids & preserved_ids
        or plan_closed["unaccounted_asset_ids"] != []
    ):
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual binding plan does not close the non-builtin asset universe")
    binding_entries = binding_plan.get("bindings")
    preserved_entries = binding_plan.get("preserved_assets")
    if not isinstance(binding_entries, list) or not isinstance(preserved_entries, list):
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual binding or preserved arrays are invalid")
    binding_by_id: dict[str, Mapping[str, Any]] = {}
    for entry in binding_entries:
        asset_id = entry.get("logical_asset_id") if isinstance(entry, Mapping) else None
        if not isinstance(asset_id, str) or asset_id in binding_by_id:
            _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual binding IDs are invalid or duplicated")
        source = entry.get("source")
        if (
            not isinstance(source, Mapping)
            or source.get("license_spdx") != "CC-BY-NC-4.0"
            or source.get("license_url") != license_value.get("url")
            or SHA256_RE.fullmatch(str(source.get("render_asset_sha256", ""))) is None
        ):
            _fail("VISTA_HOME_BUILD_VISUAL_INVALID", f"visual binding {asset_id} lacks source attribution")
        binding_by_id[asset_id] = entry
    preserved_entry_ids = [entry.get("asset_id") for entry in preserved_entries if isinstance(entry, Mapping)]
    if set(binding_by_id) != plan_bound_ids or len(preserved_entry_ids) != len(preserved_entries) or set(preserved_entry_ids) != preserved_ids:
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual binding/preserved arrays disagree with closed-world indexes")
    result: dict[str, tuple[Path, str]] = {}
    required_output_keys = {
        "logical_asset_id",
        "semantic_category",
        "path",
        "sha256",
        "bytes",
        "media_type",
        "target_dimensions_m",
        "actual_dimensions_m",
        "normalization",
        "texture_transport",
        "texture_transport_receipt",
        "source",
        "inspection",
    }
    for index, output in enumerate(outputs):
        if set(output) != required_output_keys:
            _fail("VISTA_HOME_BUILD_VISUAL_INVALID", f"visual output {index} fields differ")
        asset_id = output["logical_asset_id"]
        source_contract = output.get("source")
        output_source_keys = {
            "dataset",
            "object_id",
            "render_asset_sha256",
            "license_spdx",
            "license_url",
            "catalog_aligned_dimensions_m",
            "actual_glb_geometry",
        }
        if (
            output.get("media_type") != "model/gltf-binary"
            or not isinstance(source_contract, Mapping)
            or set(source_contract) != output_source_keys
            or source_contract.get("license_spdx") != "CC-BY-NC-4.0"
            or source_contract.get("license_url") != license_value.get("url")
            or SHA256_RE.fullmatch(str(source_contract.get("render_asset_sha256", ""))) is None
        ):
            _fail("VISTA_HOME_BUILD_VISUAL_INVALID", f"visual output {asset_id} provenance differs")
        pinned_source = binding_by_id[asset_id].get("source")
        if (
            not isinstance(pinned_source, Mapping)
            or source_contract.get("dataset") != pinned_source.get("dataset")
            or source_contract.get("object_id") != pinned_source.get("object_id")
            or source_contract.get("render_asset_sha256") != pinned_source.get("render_asset_sha256")
            or source_contract.get("catalog_aligned_dimensions_m")
            != pinned_source.get("catalog_aligned_dimensions_m")
            or source_contract.get("actual_glb_geometry")
            != pinned_source.get("actual_glb_geometry")
        ):
            _fail("VISTA_HOME_BUILD_VISUAL_INVALID", f"visual output {asset_id} source differs from the binding plan")
        inspection = output.get("inspection")
        if (
            not isinstance(inspection, Mapping)
            or inspection.get("mesh_count") != 1
            or inspection.get("material_count", 0) < 1
            or inspection.get("pbr_texture_slot_count", 0) < 1
            or inspection.get("base_normal_orm_texture_slot_count", 0) < 1
            or inspection.get("all_primitives_material_bound") != 1
            or inspection.get("basisu_required") != 0
        ):
            _fail("VISTA_HOME_BUILD_VISUAL_INVALID", f"visual output {asset_id} lost its PBR one-mesh contract")
        normalization = output.get("normalization")
        normalization_keys = {
            "source_import_dimensions_m",
            "planned_source_dimensions_m",
            "source_dimensions_match_plan",
            "rotate_z_deg",
            "planned_rotate_z_deg",
            "fit_matches_plan",
            "rotation_mode",
            "scale_xyz",
            "actual_scale_anisotropy",
            "maximum_axis_scale_anisotropy",
            "anisotropy_accepted",
            "origin_policy",
            "actual_bounds_m",
            "actual_dimensions_m",
        }
        pinned_normalization = binding_by_id[asset_id].get("normalization_plan")
        if (
            not isinstance(normalization, Mapping)
            or set(normalization) != normalization_keys
            or not isinstance(pinned_normalization, Mapping)
            or normalization.get("source_dimensions_match_plan") is not True
            or normalization.get("fit_matches_plan") is not True
            or normalization.get("source_import_dimensions_m")
            != pinned_source.get("source_dimensions_blender_m")
            or normalization.get("planned_source_dimensions_m")
            != pinned_source.get("source_dimensions_blender_m")
            or normalization.get("rotate_z_deg")
            != pinned_normalization.get("planned_rotate_z_deg")
            or normalization.get("planned_rotate_z_deg")
            != pinned_normalization.get("planned_rotate_z_deg")
            or normalization.get("rotation_mode") != "XYZ"
            or normalization.get("origin_policy") != "footprint_center_bottom_z_zero"
            or normalization.get("anisotropy_accepted") is not True
            or normalization.get("maximum_axis_scale_anisotropy") != 2.75
            or isinstance(normalization.get("actual_scale_anisotropy"), bool)
            or not isinstance(normalization.get("actual_scale_anisotropy"), (int, float))
            or not math.isfinite(float(normalization["actual_scale_anisotropy"]))
            or not 1.0 <= float(normalization["actual_scale_anisotropy"]) <= 2.75
            or isinstance(pinned_normalization.get("scale_anisotropy"), bool)
            or not isinstance(pinned_normalization.get("scale_anisotropy"), (int, float))
            or abs(
                float(normalization["actual_scale_anisotropy"])
                - float(pinned_normalization["scale_anisotropy"])
            )
            > 0.00001
            or normalization.get("actual_dimensions_m") != output.get("actual_dimensions_m")
        ):
            _fail("VISTA_HOME_BUILD_VISUAL_INVALID", f"visual output {asset_id} normalization receipt differs")
        transport_mode = output.get("texture_transport")
        transport = output.get("texture_transport_receipt")
        if transport_mode == "blender_native_texture_import":
            if not isinstance(transport, Mapping) or dict(transport) != {"mode": "blender_native_texture_import"}:
                _fail("VISTA_HOME_BUILD_VISUAL_INVALID", f"visual output {asset_id} native texture receipt differs")
        elif transport_mode == "KHR_texture_basisu_to_core_png":
            transport = output.get("texture_transport_receipt")
            required_true = {
                "self_contained",
                "single_buffer",
                "single_mesh",
                "buffer_views_aligned_and_in_range",
                "primitive_material_indices_valid",
                "core_texture_sources_valid",
                "embedded_png_images_valid",
                "extension_declarations_complete",
            }
            if (
                not isinstance(transport, Mapping)
                or transport.get("mode") != transport_mode
                or transport.get("blender_decoded_textures") is not False
                or transport.get("source_basisu_required") is not True
                or transport.get("output_basisu_required") is not False
                or not all(transport.get(key) is True for key in required_true)
                or isinstance(transport.get("base_normal_orm_texture_slots"), bool)
                or not isinstance(transport.get("base_normal_orm_texture_slots"), int)
                or transport.get("base_normal_orm_texture_slots") < 1
                or not isinstance(transport.get("image_payloads"), list)
                or not transport.get("image_payloads")
            ):
                _fail("VISTA_HOME_BUILD_VISUAL_INVALID", f"visual output {asset_id} lacks a complete core-PNG transport receipt")
            decoder = transport.get("decoder")
            if (
                not isinstance(decoder, Mapping)
                or decoder.get("distribution") != "three"
                or decoder.get("distribution_version") != "0.185.1"
                or decoder.get("basis_universal_license") != "Apache-2.0"
                or decoder.get("three_license") != "MIT"
                or decoder.get("provenance") != "three/examples/jsm/libs/basis"
                or not isinstance(decoder.get("transcoder_js"), Mapping)
                or decoder["transcoder_js"].get("sha256") != HSSD_BASIS_TRANSCODER_JS_SHA256
                or not isinstance(decoder.get("transcoder_wasm"), Mapping)
                or decoder["transcoder_wasm"].get("sha256") != HSSD_BASIS_TRANSCODER_WASM_SHA256
                or not isinstance(decoder.get("decode_wrapper"), Mapping)
                or decoder["decode_wrapper"].get("sha256") != builder_files[
                    "tools/blender/vista_playable_home_hssd/basisu_decode.mjs"
                ]
            ):
                _fail("VISTA_HOME_BUILD_VISUAL_INVALID", f"visual output {asset_id} decoder provenance differs")
            for payload in transport["image_payloads"]:
                if (
                    not isinstance(payload, Mapping)
                    or SHA256_RE.fullmatch(str(payload.get("source_ktx2_sha256", ""))) is None
                    or SHA256_RE.fullmatch(str(payload.get("output_png_sha256", ""))) is None
                    or isinstance(payload.get("width"), bool)
                    or not isinstance(payload.get("width"), int)
                    or payload.get("width") < 1
                    or isinstance(payload.get("height"), bool)
                    or not isinstance(payload.get("height"), int)
                    or payload.get("height") < 1
                ):
                    _fail("VISTA_HOME_BUILD_VISUAL_INVALID", f"visual output {asset_id} texture payload receipt differs")
        else:
            _fail("VISTA_HOME_BUILD_VISUAL_INVALID", f"visual output {asset_id} texture transport differs")
        source = _contained_artifact(root, output["path"], f"visual output {asset_id}")
        expected = _require_sha(output.get("sha256"), f"visual output {asset_id} SHA-256")
        size = output.get("bytes")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            _fail("VISTA_HOME_BUILD_VISUAL_INVALID", f"visual output {asset_id} bytes is invalid")
        if source.stat().st_size != size or sha256_file(source) != expected:
            _fail("VISTA_HOME_BUILD_PIN_MISMATCH", f"visual output {asset_id} bytes or SHA-256 differ")
        try:
            observed_inspection = hssd_contract.inspect_glb(source.resolve(strict=True))
        except hssd_contract.HssdBindingError as exc:
            _fail("VISTA_HOME_BUILD_VISUAL_INVALID", f"visual output {asset_id} GLB inspection failed: {exc}")
        if observed_inspection != dict(inspection):
            _fail("VISTA_HOME_BUILD_VISUAL_INVALID", f"visual output {asset_id} inspection does not match its GLB bytes")
        result[asset_id] = (source, expected)
    return result


def build_artifact_bindings(
    plan: Mapping[str, Any],
    blender: BlenderInputs,
    visual_overrides: Mapping[str, tuple[Path, str]] | None = None,
) -> list[dict[str, Any]]:
    visual = dict(visual_overrides or {})
    declared = {asset["asset_id"]: asset for asset in plan["assets"]}
    if not set(visual).issubset(declared):
        _fail("VISTA_HOME_BUILD_VISUAL_INVALID", "visual overrides contain an undeclared asset")
    bindings: list[dict[str, Any]] = []
    for asset_id in sorted(declared):
        asset = declared[asset_id]
        source_file: str | None
        source_sha: str | None
        if asset["source_kind"] == "builtin":
            source_file = None
            source_sha = None
        else:
            source, source_sha = visual.get(asset_id, blender.artifacts[asset_id])
            source_file = str(source)
        bindings.append(
            {
                "asset_id": asset_id,
                "source_file": source_file,
                "source_file_sha256": source_sha,
                "source_binding_digest": asset["source_digest"],
            }
        )
    return bindings


def _validate_plugin_package(path: Path, expected_tree_sha256: str) -> TreeSnapshot:
    root = _existing_directory(path, "compiled plugin package")
    for relative in PLUGIN_REQUIRED_FILES:
        _existing_file(root / relative, f"compiled plugin file {relative}")
    descriptor, _raw = _load_json(
        root / "VistaPlayableHome.uplugin",
        expected_sha256=sha256_file(root / "VistaPlayableHome.uplugin"),
        label="compiled plugin descriptor",
    )
    declared_modules = descriptor.get("Modules", [])
    required_modules = {
        (EXPECTED_PLUGIN_NAME, "Runtime"),
        ("VistaPlayableHomeEditor", "Editor"),
    }
    observed_modules = {
        (module.get("Name"), module.get("Type"))
        for module in declared_modules
        if isinstance(module, Mapping)
    }
    if (
        descriptor.get("FriendlyName") != "VISTA Playable Home"
        or not required_modules.issubset(observed_modules)
    ):
        _fail(
            "VISTA_HOME_BUILD_PLUGIN_INVALID",
            "compiled plugin descriptor does not declare the runtime and editor modules",
        )
    binaries = {
        EXPECTED_PLUGIN_NAME: root / "Binaries/Linux/libUnrealEditor-VistaPlayableHome.so",
        "VistaPlayableHomeEditor": root / "Binaries/Linux/libUnrealEditor-VistaPlayableHomeEditor.so",
    }
    if any(binary.stat().st_size <= 0 for binary in binaries.values()):
        _fail("VISTA_HOME_BUILD_PLUGIN_INVALID", "compiled plugin binary is empty")
    modules_manifest, _modules_raw = _load_json(
        root / "Binaries/Linux/UnrealEditor.modules",
        expected_sha256=sha256_file(root / "Binaries/Linux/UnrealEditor.modules"),
        label="compiled plugin modules manifest",
    )
    module_files = modules_manifest.get("Modules")
    if (
        not isinstance(module_files, Mapping)
        or module_files.get(EXPECTED_PLUGIN_NAME) != binaries[EXPECTED_PLUGIN_NAME].name
        or module_files.get("VistaPlayableHomeEditor") != binaries["VistaPlayableHomeEditor"].name
    ):
        _fail(
            "VISTA_HOME_BUILD_PLUGIN_INVALID",
            "compiled plugin modules manifest does not bind both editor binaries",
        )
    return _validate_tree_pin(root, expected_tree_sha256, "compiled plugin package")


def _validate_characters_content(path: Path, expected_tree_sha256: str) -> TreeSnapshot:
    root = _existing_directory(path, "Characters content")
    for relative in MANNY_REQUIRED_FILES:
        _existing_file(root / relative, f"Manny content {relative}")
    return _validate_tree_pin(root, expected_tree_sha256, "Characters content")


def project_descriptor() -> dict[str, Any]:
    return {
        "FileVersion": 3,
        "EngineAssociation": "5.7",
        "Category": "Simulation",
        "Description": "Disposable VISTA Playable Home runtime project",
        "Plugins": [
            *[{"Name": name, "Enabled": True} for name in PROJECT_PLUGINS],
            # Some UE installations enable this plugin by default. Its editor
            # settings append a random SecurityToken to DefaultEngine.ini on
            # first commandlet startup, invalidating the renderer config pin.
            {"Name": "AndroidFileServer", "Enabled": False},
        ],
    }


def compile_renderer_profile(profile: Mapping[str, Any]) -> RendererProfileCompilation:
    """Compile the approved Linux high-desktop profile without claiming proof.

    Config generation is a request.  The returned observation contract must be
    satisfied by a packaged runtime receipt before the renderer tier can be
    promoted; it deliberately separates requested settings from observed RHI,
    feature level, shader platform, and effective CVars.
    """

    if not isinstance(profile, Mapping):
        _fail("VISTA_HOME_RENDERER_PROFILE_INVALID", "renderer profile must be an object")
    registry = load_renderer_cvar_registry()
    profile_id = profile.get("profile_id")
    if not isinstance(profile_id, str) or RENDERER_PROFILE_SAFE_ID_RE.fullmatch(profile_id) is None:
        _fail("VISTA_HOME_RENDERER_PROFILE_INVALID", "renderer profile ID is invalid")
    exact_values = {
        "platform": "linux",
        "rhi": "vulkan",
        "feature_level": "sm6",
        "shading_path": "deferred",
        "dynamic_gi": "lumen",
        "reflections": "lumen",
        "shadow_method": "virtual_shadow_maps",
        "anti_aliasing": "tsr",
        "nanite_policy": "eligible_static_opaque_only",
        "engine_version": PINNED_UNREAL_ENGINE_VERSION,
        "registered_cvar_manifest": RENDERER_REGISTRY_ID,
        "registered_cvar_manifest_digest": registry["content_digest"],
        "pre_exposure_runtime_policy": "ue5_always_on_engine_managed",
    }
    for key, expected in exact_values.items():
        if profile.get(key) != expected:
            _fail("VISTA_HOME_RENDERER_PROFILE_INVALID", f"renderer profile {key} must be {expected}")
    for key in ("extended_luminance_range", "pre_exposure"):
        if profile.get(key) is not True:
            _fail("VISTA_HOME_RENDERER_PROFILE_INVALID", f"renderer profile {key} must be enabled")
    if profile.get("hardware_ray_tracing") is not False:
        _fail("VISTA_HOME_RENDERER_PROFILE_INVALID",
              "the first Linux realism profile must use software Lumen")
    pre_exposure_override = profile.get("pre_exposure_override")
    if (
        isinstance(pre_exposure_override, bool)
        or not isinstance(pre_exposure_override, (int, float))
        or float(pre_exposure_override) != 0.0
    ):
        _fail(
            "VISTA_HOME_RENDERER_PROFILE_INVALID",
            "UE 5.7.3 pre-exposure override must remain engine-managed at zero",
        )

    screen_percentage = profile.get("screen_percentage")
    if (isinstance(screen_percentage, bool) or
            not isinstance(screen_percentage, (int, float)) or
            not math.isfinite(float(screen_percentage)) or
            not 50.0 <= float(screen_percentage) <= 200.0):
        _fail("VISTA_HOME_RENDERER_PROFILE_INVALID", "screen percentage is invalid")
    texture_pool_mb = profile.get("texture_pool_mb")
    if (isinstance(texture_pool_mb, bool) or not isinstance(texture_pool_mb, int) or
            not 1024 <= texture_pool_mb <= 24 * 1024):
        _fail("VISTA_HOME_RENDERER_PROFILE_INVALID", "texture pool must be 1024 through 24576 MiB")
    scalability = profile.get("scalability")
    if not isinstance(scalability, Mapping) or set(scalability) != set(RENDERER_SCALABILITY_KEYS):
        _fail("VISTA_HOME_RENDERER_PROFILE_INVALID", "renderer scalability fields differ")
    normalized_scalability: dict[str, int] = {}
    for key in RENDERER_SCALABILITY_KEYS:
        value = scalability[key]
        if isinstance(value, bool) or not isinstance(value, int) or not 3 <= value <= 4:
            _fail("VISTA_HOME_RENDERER_PROFILE_INVALID",
                  f"high desktop scalability {key} must be 3 or 4")
        normalized_scalability[key] = value

    normalized = {
        "profile_id": profile_id,
        **exact_values,
        "hardware_ray_tracing": False,
        "extended_luminance_range": True,
        "pre_exposure": True,
        "pre_exposure_override": 0,
        "screen_percentage": float(screen_percentage),
        "texture_pool_mb": texture_pool_mb,
        "scalability": normalized_scalability,
    }
    # UE 5.7's Linux target platform and runtime RHI both read the
    # ``TargetedRHIs`` array.  ``VulkanTargetedShaderFormats`` and
    # ``DefaultGraphicsRHI`` are Windows target settings and are ignored by
    # Linux, which otherwise leaves the engine default (SM5) active.
    linux_target_lines = (
        "-TargetedRHIs=SF_VULKAN_SM5",
        "+TargetedRHIs=SF_VULKAN_SM6",
    )
    renderer_lines = (
        "r.DynamicGlobalIlluminationMethod=1",
        "r.ReflectionMethod=1",
        "r.Shadow.Virtual.Enable=1",
        "r.AntiAliasingMethod=4",
        "r.Nanite.ProjectEnabled=True",
        "r.GenerateMeshDistanceFields=True",
        "r.DefaultFeature.AutoExposure.ExtendDefaultLuminanceRange=True",
        "r.EyeAdaptation.PreExposureOverride=0",
        "r.RayTracing=False",
        "r.Lumen.HardwareRayTracing=0",
    )
    console_lines = (
        f"r.ScreenPercentage={float(screen_percentage):.6f}",
        f"r.Streaming.PoolSize={texture_pool_mb}",
        *(
            f"{RENDERER_SCALABILITY_CVARS[key]}={normalized_scalability[key]}"
            for key in RENDERER_SCALABILITY_KEYS
        ),
    )
    required_runtime_observations: list[dict[str, Any]] = [
        {"source": "runtime", "name": "rhi", "comparison": "casefold_exact", "expected": "Vulkan"},
        {"source": "runtime", "name": "feature_level", "comparison": "casefold_exact", "expected": "SM6"},
        {"source": "runtime", "name": "shader_platform", "comparison": "contains", "expected": "VULKAN_SM6"},
    ]
    required_cvars: tuple[tuple[str, int | float], ...] = (
        ("r.DynamicGlobalIlluminationMethod", 1),
        ("r.ReflectionMethod", 1),
        ("r.Shadow.Virtual.Enable", 1),
        ("r.AntiAliasingMethod", 4),
        ("r.Nanite", 1),
        ("r.GenerateMeshDistanceFields", 1),
        ("r.DefaultFeature.AutoExposure.ExtendDefaultLuminanceRange", 1),
        ("r.EyeAdaptation.PreExposureOverride", 0),
        ("r.RayTracing", 0),
        ("r.Lumen.HardwareRayTracing", 0),
        ("r.ScreenPercentage", float(screen_percentage)),
        ("r.Streaming.PoolSize", texture_pool_mb),
        *tuple(
            (RENDERER_SCALABILITY_CVARS[key], normalized_scalability[key])
            for key in RENDERER_SCALABILITY_KEYS
        ),
    )
    required_runtime_observations.extend(
        {"source": "cvar", "name": name, "comparison": "numeric_exact", "expected": expected}
        for name, expected in required_cvars
    )
    registration_evidence = validate_renderer_cvar_registry(
        registry, [name for name, _expected in required_cvars]
    )
    observation_contract = {
        "schema_version": RENDERER_OBSERVATION_SCHEMA,
        "profile_id": profile_id,
        "status": "runtime_observation_required",
        "config_is_runtime_proof": False,
        "pinned_unreal_engine": dict(registry["engine"]),
        "cvar_registration_evidence": {
            "registry_id": registry["registry_id"],
            "content_digest": registry["content_digest"],
            "policy": registry["registration_evidence_policy"],
            "registrations": [
                dict(registration_evidence[name])
                for name, _expected in required_cvars
            ],
        },
        "pre_exposure_policy": dict(registry["pre_exposure_policy"]),
        "required_runtime_observations": required_runtime_observations,
        "nanite_policy": {
            "mode": "eligible_static_opaque_only",
            "per_mesh_receipt_required": True,
            "eligible": ["static", "opaque", "non_deforming"],
            "excluded": ["skeletal", "deforming", "cloth", "translucent", "glass", "pickup", "door"],
        },
    }
    digest_input = {
        "profile": normalized,
        "linux_target_lines": list(linux_target_lines),
        "renderer_lines": list(renderer_lines),
        "console_lines": list(console_lines),
        "observation_contract": observation_contract,
    }
    digest = sha256_bytes(canonical_json(digest_input))
    return RendererProfileCompilation(
        profile=normalized,
        linux_target_lines=linux_target_lines,
        renderer_lines=renderer_lines,
        console_lines=console_lines,
        observation_contract=observation_contract,
        content_digest=digest,
    )


def build_renderer_request(
    visual_profile: Mapping[str, Any],
    compilation: RendererProfileCompilation,
    engine_ini_raw: bytes,
) -> dict[str, Any]:
    """Build a deterministic staging receipt without claiming runtime proof."""

    request = {
        "schema_version": RENDERER_REQUEST_SCHEMA,
        "status": "staged_runtime_observation_required",
        "runtime_proof": False,
        "visual_profile_id": visual_profile["visual_profile_id"],
        "visual_profile_content_digest": visual_profile["content_digest"],
        "renderer_profile": compilation.profile,
        "renderer_profile_digest": compilation.content_digest,
        "engine_config_sha256": sha256_bytes(engine_ini_raw),
        "observation_contract": compilation.observation_contract,
    }
    request["content_digest"] = _content_digest(request)
    return request


def evaluate_renderer_observations(
    compilation: RendererProfileCompilation,
    observations: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare retained runtime observations against a compiled request."""

    failures: list[dict[str, Any]] = []
    for requirement in compilation.observation_contract["required_runtime_observations"]:
        name = requirement["name"]
        expected = requirement["expected"]
        actual = observations.get(name)
        comparison = requirement["comparison"]
        matched = False
        if comparison == "casefold_exact" and isinstance(actual, str):
            matched = actual.casefold() == str(expected).casefold()
        elif comparison == "contains" and isinstance(actual, str):
            matched = str(expected).casefold() in actual.casefold()
        elif comparison == "numeric_exact" and not isinstance(actual, bool):
            try:
                matched = math.isfinite(float(actual)) and abs(float(actual) - float(expected)) <= 1e-4
            except (TypeError, ValueError):
                matched = False
        if not matched:
            failures.append({"name": name, "expected": expected, "actual": actual})
    return {
        "schema_version": RENDERER_OBSERVATION_SCHEMA,
        "profile_id": compilation.profile["profile_id"],
        "renderer_profile_digest": compilation.content_digest,
        "status": "accepted_observation" if not failures else "rejected_observation",
        "runtime_proof": not failures,
        "failures": failures,
    }


def evaluate_renderer_status_response(
    compilation: RendererProfileCompilation,
    response: Mapping[str, Any],
    *,
    command_id: str,
) -> dict[str, Any]:
    """Strictly evaluate one observation emitted by the typed UE runtime.

    This is deliberately separate from :func:`evaluate_renderer_observations`,
    whose small mapping API remains useful for pure configuration tests.  The
    runtime gate accepts a closed response schema, requires the complete CVar
    set described by the pinned observation contract, and never treats the
    staged request itself as evidence.
    """

    top_level_keys = {
        "command_id",
        "status",
        "code",
        "schema_version",
        "unreal_engine_version",
        "rhi",
        "feature_level",
        "shader_platform",
        "cvars",
    }
    if not isinstance(response, Mapping) or set(response) != top_level_keys:
        _fail(
            "VISTA_HOME_RENDERER_OBSERVATION_INVALID",
            "renderer status response fields differ",
        )
    engine_version = response.get("unreal_engine_version")
    if (
        response.get("command_id") != command_id
        or response.get("status") != "success"
        or response.get("code") != "RENDERER_STATUS_OBSERVED"
        or response.get("schema_version") != RENDERER_STATUS_SCHEMA
        or not isinstance(engine_version, str)
        or engine_version != PINNED_UNREAL_ENGINE_RUNTIME_VERSION
    ):
        _fail(
            "VISTA_HOME_RENDERER_OBSERVATION_INVALID",
            "renderer status identity or pinned UE 5.7.3 version differs",
        )

    contract = compilation.observation_contract
    requirements = contract.get("required_runtime_observations")
    if not isinstance(requirements, list) or not requirements:
        _fail(
            "VISTA_HOME_RENDERER_OBSERVATION_INVALID",
            "renderer observation contract is empty",
        )
    runtime_names: set[str] = set()
    cvar_names: set[str] = set()
    for requirement in requirements:
        if not isinstance(requirement, Mapping) or set(requirement) != {
            "source", "name", "comparison", "expected"
        }:
            _fail(
                "VISTA_HOME_RENDERER_OBSERVATION_INVALID",
                "renderer observation requirement fields differ",
            )
        source = requirement.get("source")
        name = requirement.get("name")
        if not isinstance(name, str) or not name:
            _fail(
                "VISTA_HOME_RENDERER_OBSERVATION_INVALID",
                "renderer observation requirement name is invalid",
            )
        names = runtime_names if source == "runtime" else cvar_names if source == "cvar" else None
        if names is None or name in names:
            _fail(
                "VISTA_HOME_RENDERER_OBSERVATION_INVALID",
                "renderer observation requirement source or uniqueness differs",
            )
        names.add(name)
    if runtime_names != {"rhi", "feature_level", "shader_platform"}:
        _fail(
            "VISTA_HOME_RENDERER_OBSERVATION_INVALID",
            "renderer runtime identity observations differ",
        )
    cvars = response.get("cvars")
    if not isinstance(cvars, Mapping) or set(cvars) != cvar_names:
        _fail(
            "VISTA_HOME_RENDERER_OBSERVATION_INVALID",
            "renderer CVar observation set differs",
        )
    for name, value in cvars.items():
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            _fail(
                "VISTA_HOME_RENDERER_OBSERVATION_INVALID",
                f"renderer CVar {name} is not finite numeric evidence",
            )
    observations = {
        "rhi": response["rhi"],
        "feature_level": response["feature_level"],
        "shader_platform": response["shader_platform"],
        **dict(cvars),
    }
    evaluation = evaluate_renderer_observations(compilation, observations)
    if not evaluation["runtime_proof"]:
        _fail(
            "VISTA_HOME_RENDERER_OBSERVATION_REJECTED",
            "effective renderer observations do not satisfy the pinned contract",
        )
    return {
        **evaluation,
        "renderer_status_schema": RENDERER_STATUS_SCHEMA,
        "unreal_engine_version": engine_version,
        "observations": observations,
    }


def default_engine_ini(
    plan: Mapping[str, Any],
    visual_profile: Mapping[str, Any] | None = None,
) -> bytes:
    map_path = plan["unreal"]["map_path"]
    lines = [
        "[/Script/EngineSettings.GameMapsSettings]",
        f"GameDefaultMap={map_path}",
        f"EditorStartupMap={map_path}",
        "GlobalDefaultGameMode=/Script/VistaPlayableHome.VistaPlayableHomeGameMode",
        "",
        "[/Script/NavigationSystem.RecastNavMesh]",
        # UE 5.7 no longer exposes the editor-only synchronous Build() call to
        # Python.  Generate the base tiles from the saved bounds at runtime;
        # the typed acceptance test proves that the NPC can traverse them.
        "RuntimeGeneration=Dynamic",
        "",
        "[/Script/Engine.RendererSettings]",
        "r.AllowStaticLighting=False",
    ]
    if visual_profile is not None:
        if not isinstance(visual_profile, Mapping) or not isinstance(
                visual_profile.get("renderer_profile"), Mapping):
            _fail("VISTA_HOME_RENDERER_PROFILE_INVALID",
                  "visual profile renderer_profile is missing")
        compiled = compile_renderer_profile(visual_profile["renderer_profile"])
        lines.extend(compiled.renderer_lines)
        lines.extend([
            "",
            "[/Script/LinuxTargetPlatform.LinuxTargetSettings]",
            *compiled.linux_target_lines,
            "",
            "[ConsoleVariables]",
            *compiled.console_lines,
            "",
            "[/Script/AndroidFileServerEditor.AndroidFileServerRuntimeSettings]",
            "bEnablePlugin=False",
            "bAllowNetworkConnection=False",
            "bIncludeInShipping=False",
            "bAllowExternalStartInShipping=False",
            "bCompileAFSProject=False",
        ])
    lines.append("")
    return "\n".join(lines).encode("utf-8")


def default_input_ini() -> bytes:
    """Return the fixed legacy input contract used by the C++ playable pawn."""

    lines = [
        "[/Script/Engine.InputSettings]",
        "bCaptureMouseOnLaunch=True",
        "DefaultViewportMouseCaptureMode=CapturePermanently_IncludingInitialMouseDown",
        "DefaultViewportMouseLockMode=LockOnCapture",
        '+AxisMappings=(AxisName="MoveForward",Scale=1.000000,Key=W)',
        '+AxisMappings=(AxisName="MoveForward",Scale=-1.000000,Key=S)',
        '+AxisMappings=(AxisName="MoveRight",Scale=1.000000,Key=D)',
        '+AxisMappings=(AxisName="MoveRight",Scale=-1.000000,Key=A)',
        '+AxisMappings=(AxisName="Turn",Scale=1.000000,Key=MouseX)',
        '+AxisMappings=(AxisName="LookUp",Scale=-1.000000,Key=MouseY)',
        '+ActionMappings=(ActionName="Jump",bShift=False,bCtrl=False,bAlt=False,bCmd=False,Key=SpaceBar)',
        '+ActionMappings=(ActionName="Sprint",bShift=False,bCtrl=False,bAlt=False,bCmd=False,Key=LeftShift)',
        '+ActionMappings=(ActionName="Crouch",bShift=False,bCtrl=False,bAlt=False,bCmd=False,Key=C)',
        '+ActionMappings=(ActionName="Interact",bShift=False,bCtrl=False,bAlt=False,bCmd=False,Key=E)',
        '+ActionMappings=(ActionName="Drop",bShift=False,bCtrl=False,bAlt=False,bCmd=False,Key=Q)',
        "",
    ]
    return "\n".join(lines).encode("utf-8")


def _commandlet_phase_root(attempt_root: Path, phase: str) -> Path:
    if phase not in COMMANDLET_PHASES:
        _fail(
            "VISTA_HOME_BUILD_PHASE_INVALID",
            f"unknown commandlet phase {phase!r}",
        )
    return attempt_root / COMMANDLET_RUNTIME_DIRECTORY / phase


def _fixed_command(
    editor: Path,
    project_file: Path,
    script: Path,
    *,
    attempt_root: Path,
    phase: str,
    presentation_import_gpu0_rendering: bool = False,
) -> list[str]:
    if presentation_import_gpu0_rendering and phase != "presentation_import":
        _fail(
            "VISTA_HOME_BUILD_GPU_MODE_INVALID",
            "GPU rendering is allowed only for the explicit presentation import retry",
        )
    phase_root = _commandlet_phase_root(attempt_root, phase)
    command = [
        str(editor),
        str(project_file),
        "-run=pythonscript",
        f"-script={script}",
        "-nocrashreports",
        "-unattended",
        "-nop4",
        "-nosplash",
        "-NOSOUND",
        "-NoAnalytics",
        "-UDPMESSAGING_TRANSPORT_ENABLE=0",
        "-ini:Engine:[/Script/TcpMessaging.TcpMessagingSettings]:EnableTransport=False",
        "-ddc=InstalledNoZenLocalFallback",
        "-SaveToUserDir",
        f"-UserDir={phase_root / 'user'}",
        f"-LocalDataCachePath={attempt_root / COMMANDLET_RUNTIME_DIRECTORY / 'ddc'}",
        f"-abslog={phase_root / 'unreal.log'}",
        "-stdout",
        "-FullStdOutLogOutput",
    ]
    if presentation_import_gpu0_rendering:
        command.extend([
            "-AllowCommandletRendering",
            "-RenderOffScreen",
            "-graphicsadapter=0",
        ])
    else:
        command.append("-nullrhi")
    return command


def _commandlet_environment(
    attempt_root: Path,
    phase: str,
    bindings: Mapping[str, str],
) -> dict[str, str]:
    phase_root = _commandlet_phase_root(attempt_root, phase)
    return {
        **bindings,
        "HOME": str(phase_root / "home"),
        "TMPDIR": str(phase_root / "tmp"),
        "TMP": str(phase_root / "tmp"),
        "TEMP": str(phase_root / "tmp"),
        "XDG_CACHE_HOME": str(phase_root / "xdg-cache"),
        "XDG_CONFIG_HOME": str(phase_root / "xdg-config"),
        "XDG_DATA_HOME": str(phase_root / "xdg-data"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def _validate_presentation_vulkan_icd(
    path: Path,
    expected_sha256: str,
) -> tuple[dict[str, str], bytes]:
    """Validate the explicitly pinned NVIDIA headless Vulkan ICD contract."""

    value, raw = _load_json(
        path,
        expected_sha256=expected_sha256,
        label="presentation Vulkan ICD",
    )
    if set(value) != {"file_format_version", "ICD"}:
        _fail(
            "VISTA_HOME_BUILD_VULKAN_ICD_INVALID",
            "presentation Vulkan ICD root fields differ",
            pointer=str(path),
        )
    if not isinstance(value["file_format_version"], str) or re.fullmatch(
        r"[0-9]+\.[0-9]+\.[0-9]+", value["file_format_version"]
    ) is None:
        _fail(
            "VISTA_HOME_BUILD_VULKAN_ICD_INVALID",
            "presentation Vulkan ICD file_format_version is invalid",
            pointer=str(path),
        )
    icd = value["ICD"]
    if not isinstance(icd, Mapping) or set(icd) != {"library_path", "api_version"}:
        _fail(
            "VISTA_HOME_BUILD_VULKAN_ICD_INVALID",
            "presentation Vulkan ICD fields differ",
            pointer=str(path),
        )
    if not isinstance(icd["api_version"], str) or re.fullmatch(
        r"[0-9]+\.[0-9]+\.[0-9]+", icd["api_version"]
    ) is None:
        _fail(
            "VISTA_HOME_BUILD_VULKAN_ICD_INVALID",
            "presentation Vulkan ICD api_version is invalid",
            pointer=str(path),
        )
    if not isinstance(icd["library_path"], str):
        _fail(
            "VISTA_HOME_BUILD_VULKAN_ICD_INVALID",
            "presentation Vulkan ICD library_path is invalid",
            pointer=str(path),
        )
    library = _absolute_lexical(
        Path(icd["library_path"]),
        "presentation Vulkan ICD library",
    )
    if library.name != "libEGL_nvidia.so.0":
        _fail(
            "VISTA_HOME_BUILD_VULKAN_ICD_INVALID",
            "presentation Vulkan ICD must select the NVIDIA EGL headless library",
            pointer=str(library),
        )
    try:
        resolved_library = library.resolve(strict=True)
        metadata = resolved_library.stat()
    except OSError as exc:
        _fail(
            "VISTA_HOME_BUILD_VULKAN_ICD_INVALID",
            "presentation Vulkan ICD library is missing or unreadable",
            pointer=str(library),
        )
        raise AssertionError from exc
    if not stat.S_ISREG(metadata.st_mode):
        _fail(
            "VISTA_HOME_BUILD_VULKAN_ICD_INVALID",
            "presentation Vulkan ICD library must resolve to a regular file",
            pointer=str(library),
        )
    return {
        "path": str(_existing_file(path, "presentation Vulkan ICD")),
        "sha256": _require_sha(expected_sha256, "presentation Vulkan ICD pin"),
        "file_format_version": value["file_format_version"],
        "library_path": str(library),
        "resolved_library_path": str(resolved_library),
        "api_version": icd["api_version"],
    }, raw


def _prepare_commandlet_runtime(attempt_root: Path, phases: Sequence[str]) -> None:
    runtime_root = attempt_root / COMMANDLET_RUNTIME_DIRECTORY
    runtime_root.mkdir(mode=0o700, exist_ok=False)
    (runtime_root / "ddc").mkdir(mode=0o700, exist_ok=False)
    for phase in phases:
        phase_root = _commandlet_phase_root(attempt_root, phase)
        phase_root.mkdir(mode=0o700, exist_ok=False)
        for relative in (
            "home",
            "tmp",
            "xdg-cache",
            "xdg-config",
            "xdg-data",
            "user",
        ):
            (phase_root / relative).mkdir(mode=0o700, exist_ok=False)


@dataclass(frozen=True)
class BuildConfig:
    run_root: Path
    attempt_root: Path
    build_plan: Path
    build_plan_sha256: str
    blender_manifest: Path
    blender_manifest_sha256: str
    plugin_package: Path
    plugin_package_tree_sha256: str
    characters_content: Path
    characters_content_tree_sha256: str
    unreal_editor_cmd: Path
    unreal_editor_cmd_sha256: str | None
    visual_binding_manifest: Path | None = None
    visual_binding_manifest_sha256: str | None = None
    visual_profile: Path | None = None
    visual_profile_sha256: str | None = None
    presentation_manifest: Path | None = None
    presentation_manifest_sha256: str | None = None
    presentation_artifact_receipt: Path | None = None
    presentation_artifact_receipt_sha256: str | None = None
    expected_revision: str = EXPECTED_REVISION
    command_timeout_s: int = 3600
    presentation_import_gpu0_rendering: bool = False
    presentation_vulkan_icd: Path | None = None
    presentation_vulkan_icd_sha256: str | None = None


@dataclass(frozen=True)
class PlannedBuild:
    config: BuildConfig
    plan: dict[str, Any]
    blender: BlenderInputs
    bindings: list[dict[str, Any]]
    plugin_snapshot: TreeSnapshot
    characters_snapshot: TreeSnapshot
    project_raw: bytes
    engine_ini_raw: bytes
    input_ini_raw: bytes
    visual_profile: dict[str, Any] | None
    visual_profile_raw: bytes | None
    presentation: PresentationInputs | None
    presentation_vulkan_icd: dict[str, str] | None
    presentation_vulkan_icd_raw: bytes | None
    renderer_request: dict[str, Any] | None
    renderer_request_raw: bytes | None
    execution: dict[str, Any]
    execution_raw: bytes
    execution_sha256: str
    dry_run_report: dict[str, Any]


def _validate_destination(config: BuildConfig) -> tuple[Path, Path]:
    run_root = _existing_directory(config.run_root, "append-only run root")
    attempt = _absolute_lexical(config.attempt_root, "attempt root")
    _reject_symlink_components(attempt, "attempt root", allow_missing_tail=True)
    expected_parent = run_root / "ue"
    if attempt.parent != expected_parent or ATTEMPT_RE.fullmatch(attempt.name) is None:
        _fail("VISTA_HOME_BUILD_ATTEMPT_INVALID", "attempt root must be a named direct child of <run-root>/ue")
    if any(part.casefold() in FORBIDDEN_ATTEMPT_PARTS for part in attempt.parts):
        _fail("VISTA_HOME_BUILD_ATTEMPT_INVALID", "attempt root uses a forbidden destination component")
    if attempt.exists():
        _fail("VISTA_HOME_BUILD_ATTEMPT_EXISTS", "append-only attempt root already exists", pointer=str(attempt))
    return run_root, attempt


def _validate_editor(config: BuildConfig, *, require_existing: bool) -> tuple[Path, str | None]:
    editor = _absolute_lexical(config.unreal_editor_cmd, "UnrealEditor-Cmd")
    if editor.name != "UnrealEditor-Cmd" or tuple(
        parent.name for parent in (editor.parent, editor.parent.parent, editor.parent.parent.parent)
    ) != ("Linux", "Binaries", "Engine"):
        _fail("VISTA_HOME_BUILD_EDITOR_INVALID", "editor must be an exact Engine/Binaries/Linux/UnrealEditor-Cmd path")
    if not editor.exists():
        if require_existing:
            _fail("VISTA_HOME_BUILD_EDITOR_MISSING", "UnrealEditor-Cmd is required for --apply", pointer=str(editor))
        if config.unreal_editor_cmd_sha256 is not None:
            _require_sha(config.unreal_editor_cmd_sha256, "UnrealEditor-Cmd pin")
        return editor, config.unreal_editor_cmd_sha256
    source = _existing_file(editor, "UnrealEditor-Cmd")
    if not os.access(source, os.X_OK):
        _fail("VISTA_HOME_BUILD_EDITOR_INVALID", "UnrealEditor-Cmd is not executable", pointer=str(source))
    expected = _require_sha(config.unreal_editor_cmd_sha256, "UnrealEditor-Cmd pin")
    actual = sha256_file(source)
    if actual != expected:
        _fail("VISTA_HOME_BUILD_PIN_MISMATCH", "UnrealEditor-Cmd SHA-256 differs", pointer=str(source))
    return source, actual


def _presentation_planner_projection(
    presentation: PresentationInputs | None,
) -> tuple[tuple[dict[str, Any], ...] | None, bool]:
    if presentation is None:
        return None, False
    external_flags = [
        "external_content" in binding for binding in presentation.bindings
    ]
    if any(external_flags) and not all(external_flags):
        _fail(
            "VISTA_HOME_PRESENTATION_INVALID",
            "presentation bindings mix v1 and external v2 contracts",
        )
    external_presentation = all(external_flags)
    projected_bindings: list[dict[str, Any]] = []
    for binding in presentation.bindings:
        projected = {
            key: copy.deepcopy(value)
            for key, value in binding.items()
            if key != "external_content"
        }
        if external_presentation:
            # The unchanged v1 planner assumes unique, safe-ID texture
            # triplets. External GLBs may share maps and retain source material
            # names, so use transient compatibility evidence only for that
            # structural gate. The exact inspected values are restored below.
            projected["texture_count"] = max(
                projected["texture_count"],
                projected["material_count"] * 3,
            )
            projected["material_ids"] = [
                f"r2.external.compat.material_{index:03d}"
                for index in range(projected["material_count"])
            ]
        projected_bindings.append(projected)
    return tuple(projected_bindings), external_presentation


def _planned_execution(
    *,
    plan: Mapping[str, Any],
    attempt: Path,
    bindings: Sequence[Mapping[str, Any]],
    project_sha256: str,
    build_plan_sha256: str,
    visual_profile: Mapping[str, Any] | None = None,
    visual_profile_sha256: str | None = None,
    renderer_request: Mapping[str, Any] | None = None,
    presentation: PresentationInputs | None = None,
    presentation_manifest_sha256: str | None = None,
    presentation_artifact_receipt_sha256: str | None = None,
) -> tuple[dict[str, Any], bytes, str]:
    if (visual_profile is None) != (visual_profile_sha256 is None) or (
        visual_profile is None
    ) != (renderer_request is None):
        _fail(
            "VISTA_HOME_BUILD_ARGUMENT_INVALID",
            "visual profile, profile pin, and renderer request must be supplied together",
        )
    if presentation is not None and visual_profile is None:
        _fail(
            "VISTA_HOME_BUILD_ARGUMENT_INVALID",
            "presentation bundles require a selected visual profile",
        )
    if presentation is not None:
        _require_sha(presentation_manifest_sha256, "presentation manifest pin")
        _require_sha(
            presentation_artifact_receipt_sha256,
            "presentation artifact receipt pin",
        )
    # The unchanged composition planner consumes only placement identity,
    # transform, and presentation policy. Keep its v1 operation shape
    # byte-stable while the full v2 evidence remains in execution bindings.
    presentation_operation_bindings, external_presentation = (
        _presentation_planner_projection(presentation)
    )
    composition = planning.build_composition_spec(
        plan,
        visual_profile,
        presentation_operation_bindings,
    )
    if external_presentation:
        actual_material_inventories = {
            binding["artifact_id"]: {
                "material_ids": list(binding["material_ids"]),
                "texture_count": binding["texture_count"],
            }
            for binding in presentation.bindings
        }
        composition_value = copy.deepcopy(composition.value)
        presentation_operations = [
            operation
            for operation in composition_value["operations"]
            if operation.get("kind") == "place_room_presentation_bundle"
        ]
        if (
            len(presentation_operations) != len(actual_material_inventories)
            or {operation.get("artifact_id") for operation in presentation_operations}
            != set(actual_material_inventories)
        ):
            _fail(
                "VISTA_HOME_PRESENTATION_INVALID",
                "external presentation operation inventory differs",
            )
        for operation in presentation_operations:
            inventory = actual_material_inventories[operation["artifact_id"]]
            operation["material_ids"] = inventory["material_ids"]
            operation["texture_count"] = inventory["texture_count"]
        composition_raw = planning.canonical_json(composition_value)
        composition = planning.CompositionSpec(
            composition_value,
            composition_raw,
            sha256_bytes(composition_raw),
        )
    scripts = {
        "import": Path(__file__).with_name("import_assets_commandlet.py").resolve(strict=True),
        "compose": Path(__file__).with_name("compose_home_commandlet.py").resolve(strict=True),
        "common": Path(__file__).with_name("commandlet_common.py").resolve(strict=True),
    }
    value = {
        "schema_version": contract.EXECUTION_SCHEMA,
        "attempt_root": str(attempt),
        "project_file": str(attempt / "project" / EXPECTED_PROJECT_NAME),
        "project_sha256": project_sha256,
        "build_plan_path": str(attempt / "contracts" / "build-plan.json"),
        "build_plan_sha256": build_plan_sha256,
        "build_plan_content_digest": plan["content_digest"],
        "composition_spec": composition.value,
        "composition_spec_sha256": composition.sha256,
        "artifact_bindings": [dict(binding) for binding in sorted(bindings, key=lambda item: item["asset_id"])],
        "scripts": {
            name: {"path": str(path), "sha256": sha256_file(path)}
            for name, path in scripts.items()
        },
        "import_receipt": str(attempt / "import-receipt.json"),
        "scene_receipt": str(attempt / "scene-receipt.json"),
        "policy": {
            "append_only_namespace": True,
            "replace_existing": False,
            "save_reload_required": True,
            "quarantine_on_failure": True,
            "studio_socket_fallback_allowed": False,
        },
    }
    if visual_profile is not None:
        profile_sha = _require_sha(visual_profile_sha256, "visual profile pin")
        request = dict(renderer_request or {})
        if (
            request.get("schema_version") != RENDERER_REQUEST_SCHEMA
            or request.get("status") != "staged_runtime_observation_required"
            or request.get("runtime_proof") is not False
            or request.get("visual_profile_id") != visual_profile["visual_profile_id"]
            or request.get("visual_profile_content_digest")
            != visual_profile["content_digest"]
            or request.get("content_digest") != _content_digest(request)
        ):
            _fail(
                "VISTA_HOME_RENDERER_PROFILE_INVALID",
                "renderer request does not bind the selected visual profile",
            )
        request_raw = canonical_json(request)
        value.update({
            "visual_profile_path": str(
                attempt / "contracts" / VISUAL_PROFILE_ATTEMPT_FILE
            ),
            "visual_profile_sha256": profile_sha,
            "visual_profile_content_digest": visual_profile["content_digest"],
            "renderer_profile_request": {
                "path": str(
                    attempt / "contracts" / RENDERER_REQUEST_ATTEMPT_FILE
                ),
                "sha256": sha256_bytes(request_raw),
                "content_digest": request["content_digest"],
                "status": "staged_runtime_observation_required",
                "runtime_proof": False,
            },
        })
    if presentation is not None:
        value.update({
            "presentation_sources": {
                "manifest": {
                    "path": str(
                        attempt / "contracts" / PRESENTATION_MANIFEST_ATTEMPT_FILE
                    ),
                    "sha256": presentation_manifest_sha256,
                },
                "artifact_receipt": {
                    "path": str(
                        attempt
                        / "contracts"
                        / PRESENTATION_ARTIFACT_RECEIPT_ATTEMPT_FILE
                    ),
                    "sha256": presentation_artifact_receipt_sha256,
                },
            },
            "presentation_bindings": [
                dict(binding) for binding in presentation.bindings
            ],
            "presentation_scripts": contract.presentation_script_pins(),
            "presentation_import_receipt": str(
                attempt / "presentation-import-receipt.json"
            ),
            "presentation_scene_receipt": str(
                attempt / "presentation-scene-receipt.json"
            ),
            "presentation_runtime_proof": "pending",
        })
    raw = planning.canonical_json(value)
    return value, raw, sha256_bytes(raw)


def plan_build(config: BuildConfig, *, require_editor: bool = False) -> PlannedBuild:
    run_root, attempt = _validate_destination(config)
    if not isinstance(config.presentation_import_gpu0_rendering, bool):
        _fail(
            "VISTA_HOME_BUILD_ARGUMENT_INVALID",
            "presentation import GPU rendering selection must be boolean",
        )
    if (
        isinstance(config.command_timeout_s, bool)
        or not isinstance(config.command_timeout_s, int)
        or not 60 <= config.command_timeout_s <= 14_400
    ):
        _fail("VISTA_HOME_BUILD_ARGUMENT_INVALID", "command timeout must be an integer from 60 through 14400 seconds")
    plan = validate_build_plan(config.build_plan, config.build_plan_sha256, config.expected_revision)
    selected_profile: dict[str, Any] | None = None
    selected_profile_raw: bytes | None = None
    if config.visual_profile is not None:
        if config.visual_profile_sha256 is None:
            _fail("VISTA_HOME_BUILD_PIN_INVALID", "visual profile pin is required")
        selected_profile, selected_profile_raw = validate_visual_profile(
            config.visual_profile,
            config.visual_profile_sha256,
            plan,
        )
    elif config.visual_profile_sha256 is not None:
        _fail("VISTA_HOME_BUILD_ARGUMENT_INVALID", "visual profile pin requires a profile path")
    presentation_values = (
        config.presentation_manifest,
        config.presentation_manifest_sha256,
        config.presentation_artifact_receipt,
        config.presentation_artifact_receipt_sha256,
    )
    has_presentation = any(value is not None for value in presentation_values)
    if config.presentation_import_gpu0_rendering and not has_presentation:
        _fail(
            "VISTA_HOME_BUILD_ARGUMENT_INVALID",
            "presentation import GPU rendering requires presentation inputs",
        )
    if has_presentation and not all(value is not None for value in presentation_values):
        _fail(
            "VISTA_HOME_BUILD_ARGUMENT_INVALID",
            "presentation manifest and artifact receipt require paired paths and SHA-256 pins",
        )
    if has_presentation and selected_profile is None:
        _fail(
            "VISTA_HOME_BUILD_ARGUMENT_INVALID",
            "presentation bundles require --visual-profile and its pin",
        )
    vulkan_icd_values = (
        config.presentation_vulkan_icd,
        config.presentation_vulkan_icd_sha256,
    )
    has_vulkan_icd = any(value is not None for value in vulkan_icd_values)
    if config.presentation_import_gpu0_rendering and not all(
        value is not None for value in vulkan_icd_values
    ):
        _fail(
            "VISTA_HOME_BUILD_ARGUMENT_INVALID",
            "presentation import GPU rendering requires a Vulkan ICD path and SHA-256 pin",
        )
    if not config.presentation_import_gpu0_rendering and has_vulkan_icd:
        _fail(
            "VISTA_HOME_BUILD_ARGUMENT_INVALID",
            "presentation Vulkan ICD inputs require GPU rendering mode",
        )
    presentation_vulkan_icd: dict[str, str] | None = None
    presentation_vulkan_icd_raw: bytes | None = None
    if config.presentation_import_gpu0_rendering:
        presentation_vulkan_icd, presentation_vulkan_icd_raw = (
            _validate_presentation_vulkan_icd(
            config.presentation_vulkan_icd,
            config.presentation_vulkan_icd_sha256,
        )
        )
    presentation: PresentationInputs | None = None
    if has_presentation:
        presentation = validate_presentation_inputs(
            config.presentation_manifest,
            config.presentation_manifest_sha256,
            config.presentation_artifact_receipt,
            config.presentation_artifact_receipt_sha256,
            plan,
            selected_profile,
        )
    blender = validate_blender_manifest(config.blender_manifest, config.blender_manifest_sha256, plan)
    visual: dict[str, tuple[Path, str]] | None = None
    if config.visual_binding_manifest is not None:
        if config.visual_binding_manifest_sha256 is None:
            _fail("VISTA_HOME_BUILD_PIN_INVALID", "visual binding manifest pin is required")
        visual = validate_visual_binding_manifest(
            config.visual_binding_manifest,
            config.visual_binding_manifest_sha256,
            plan,
            blender.normalized["content_digest"],
        )
    elif config.visual_binding_manifest_sha256 is not None:
        _fail("VISTA_HOME_BUILD_ARGUMENT_INVALID", "visual binding pin requires a manifest path")
    bindings = build_artifact_bindings(plan, blender, visual)
    plugin_snapshot = _validate_plugin_package(config.plugin_package, config.plugin_package_tree_sha256)
    characters_snapshot = _validate_characters_content(config.characters_content, config.characters_content_tree_sha256)
    editor, editor_sha = _validate_editor(config, require_existing=require_editor)
    descriptor_raw = canonical_json(project_descriptor())
    engine_ini_raw = default_engine_ini(plan, selected_profile)
    input_ini_raw = default_input_ini()
    renderer_request: dict[str, Any] | None = None
    renderer_request_raw: bytes | None = None
    if selected_profile is not None:
        renderer_compilation = compile_renderer_profile(
            selected_profile["renderer_profile"]
        )
        renderer_request = build_renderer_request(
            selected_profile,
            renderer_compilation,
            engine_ini_raw,
        )
        renderer_request_raw = canonical_json(renderer_request)
    execution, execution_raw, execution_sha = _planned_execution(
        plan=plan,
        attempt=attempt,
        bindings=bindings,
        project_sha256=sha256_bytes(descriptor_raw),
        build_plan_sha256=config.build_plan_sha256,
        visual_profile=selected_profile,
        visual_profile_sha256=config.visual_profile_sha256,
        renderer_request=renderer_request,
        presentation=presentation,
        presentation_manifest_sha256=config.presentation_manifest_sha256,
        presentation_artifact_receipt_sha256=(
            config.presentation_artifact_receipt_sha256
        ),
    )
    execution_path = attempt / "execution.json"
    project_path = attempt / "project" / EXPECTED_PROJECT_NAME
    common_env = {
        "VISTA_PLAYABLE_HOME_EXECUTION": str(execution_path),
        "VISTA_PLAYABLE_HOME_EXECUTION_SHA256": execution_sha,
        "VISTA_PLAYABLE_HOME_PROJECT": str(project_path),
    }
    report = {
        "schema_version": ORCHESTRATOR_PLAN_SCHEMA,
        "mode": "apply" if require_editor else "dry_run",
        "run_root": str(run_root),
        "attempt_root": str(attempt),
        "revision": config.expected_revision,
        "inputs": {
            "build_plan": {"path": str(config.build_plan), "sha256": config.build_plan_sha256},
            "blender_manifest": {"path": str(config.blender_manifest), "sha256": config.blender_manifest_sha256},
            "visual_binding_manifest": (
                {"path": str(config.visual_binding_manifest), "sha256": config.visual_binding_manifest_sha256}
                if config.visual_binding_manifest is not None
                else None
            ),
            "plugin_package": {
                "path": str(config.plugin_package),
                "tree_sha256": plugin_snapshot.sha256,
                "file_count": plugin_snapshot.file_count,
                "bytes": plugin_snapshot.total_bytes,
            },
            "characters_content": {
                "path": str(config.characters_content),
                "tree_sha256": characters_snapshot.sha256,
                "file_count": characters_snapshot.file_count,
                "bytes": characters_snapshot.total_bytes,
            },
            "unreal_editor_cmd": {"path": str(editor), "sha256": editor_sha},
        },
        "project": {
            "file": str(project_path),
            "sha256": sha256_bytes(descriptor_raw),
            "plugin_destination": str(attempt / "project" / "Plugins" / EXPECTED_PLUGIN_NAME),
            "characters_destination": str(attempt / "project" / "Content" / "Characters"),
            "engine_config_sha256": sha256_bytes(engine_ini_raw),
            "input_config_sha256": sha256_bytes(input_ini_raw),
        },
        "execution": {"path": str(execution_path), "sha256": execution_sha, "value": execution},
        "commands": [
            {
                "phase": "import",
                "argv": _fixed_command(
                    editor,
                    project_path,
                    Path(execution["scripts"]["import"]["path"]),
                    attempt_root=attempt,
                    phase="import",
                ),
                "env": _commandlet_environment(attempt, "import", common_env),
                "log": str(attempt / "import.log"),
                "result": str(attempt / IMPORT_RESULT_FILE),
                "timeout_s": config.command_timeout_s,
            },
            {
                "phase": "compose",
                "argv": _fixed_command(
                    editor,
                    project_path,
                    Path(execution["scripts"]["compose"]["path"]),
                    attempt_root=attempt,
                    phase="compose",
                ),
                "env": _commandlet_environment(
                    attempt,
                    "compose",
                    {
                        **common_env,
                        "VISTA_PLAYABLE_HOME_IMPORT_RECEIPT_SHA256": "<sha256-from-verified-import-receipt>",
                    },
                ),
                "log": str(attempt / "compose.log"),
                "result": str(attempt / SCENE_RESULT_FILE),
                "timeout_s": config.command_timeout_s,
            },
        ],
        "publication": {
            "condition": "both commandlets succeeded and receipts were verified",
            "accepted_pointer": str(run_root / "ue" / "accepted.json"),
            "current_pointer": str(run_root / "ue" / "current.json"),
        },
    }
    if selected_profile is not None:
        report["inputs"]["visual_profile"] = {
            "path": str(config.visual_profile),
            "sha256": config.visual_profile_sha256,
            "content_digest": selected_profile["content_digest"],
            "visual_profile_id": selected_profile["visual_profile_id"],
        }
        report["project"]["renderer_profile_request"] = {
            "path": str(
                attempt / "contracts" / RENDERER_REQUEST_ATTEMPT_FILE
            ),
            "sha256": sha256_bytes(renderer_request_raw or b""),
            "content_digest": renderer_request["content_digest"],
            "status": "staged_runtime_observation_required",
            "runtime_proof": False,
        }
    if presentation is not None:
        report["inputs"]["presentation_manifest"] = {
            "path": str(config.presentation_manifest),
            "sha256": config.presentation_manifest_sha256,
            "forge_plan_sha256": presentation.manifest["forge_plan_digest"],
        }
        report["inputs"]["presentation_artifact_receipt"] = {
            "path": str(config.presentation_artifact_receipt),
            "sha256": config.presentation_artifact_receipt_sha256,
        }
        if presentation_vulkan_icd is not None:
            report["inputs"]["presentation_vulkan_icd"] = {
                **presentation_vulkan_icd,
                "staged_path": str(
                    attempt
                    / "contracts"
                    / PRESENTATION_VULKAN_ICD_ATTEMPT_FILE
                ),
            }
        report["project"]["presentation"] = {
            "bundle_count": len(presentation.bindings),
            "content_destination": (
                plan["unreal"]["content_namespace"] + "/Presentation"
            ),
            "collision_profile": planning.PRESENTATION_UNREAL_COLLISION_PROFILE,
            "semantic_policy": planning.PRESENTATION_SEMANTIC_POLICY,
            "runtime_proof": "pending",
        }
        if _presentation_is_external(execution):
            report["project"]["presentation"].update({
                "external_content_contract": PRESENTATION_EXTERNAL_PLACEMENT_SCHEMA,
                "external_nanite_policy": PRESENTATION_EXTERNAL_NANITE_POLICY,
                "external_nanite_runtime_observation": "required",
            })
        base_compose = report["commands"].pop()
        presentation_env = {
            **common_env,
            "VISTA_PLAYABLE_HOME_IMPORT_RECEIPT_SHA256": (
                "<sha256-from-verified-import-receipt>"
            ),
        }
        presentation_import_env = dict(presentation_env)
        if presentation_vulkan_icd is not None:
            presentation_import_env[PRESENTATION_VULKAN_ICD_ENV] = (
                str(
                    attempt
                    / "contracts"
                    / PRESENTATION_VULKAN_ICD_ATTEMPT_FILE
                )
            )
        report["commands"].extend([
            {
                "phase": "presentation_import",
                "argv": _fixed_command(
                    editor,
                    project_path,
                    Path(execution["presentation_scripts"]["import"]["path"]),
                    attempt_root=attempt,
                    phase="presentation_import",
                    presentation_import_gpu0_rendering=(
                        config.presentation_import_gpu0_rendering
                    ),
                ),
                "env": _commandlet_environment(
                    attempt,
                    "presentation_import",
                    presentation_import_env,
                ),
                "log": str(attempt / "presentation-import.log"),
                "result": str(attempt / PRESENTATION_IMPORT_RESULT_FILE),
                "timeout_s": config.command_timeout_s,
            },
            base_compose,
            {
                "phase": "presentation_compose",
                "argv": _fixed_command(
                    editor,
                    project_path,
                    Path(execution["presentation_scripts"]["compose"]["path"]),
                    attempt_root=attempt,
                    phase="presentation_compose",
                ),
                "env": _commandlet_environment(
                    attempt,
                    "presentation_compose",
                    {
                        **presentation_env,
                        "VISTA_PLAYABLE_HOME_PRESENTATION_IMPORT_RECEIPT_SHA256": (
                            "<sha256-from-verified-presentation-import-receipt>"
                        ),
                        "VISTA_PLAYABLE_HOME_SCENE_RECEIPT_SHA256": (
                            "<sha256-from-verified-scene-receipt>"
                        ),
                    },
                ),
                "log": str(attempt / "presentation-compose.log"),
                "result": str(attempt / PRESENTATION_SCENE_RESULT_FILE),
                "timeout_s": config.command_timeout_s,
            },
        ])
        report["publication"]["condition"] = (
            "base import/compose and presentation import/compose succeeded "
            "and all receipts were verified"
        )
    report["content_digest"] = _content_digest(report)
    return PlannedBuild(
        config=config,
        plan=plan,
        blender=blender,
        bindings=bindings,
        plugin_snapshot=plugin_snapshot,
        characters_snapshot=characters_snapshot,
        project_raw=descriptor_raw,
        engine_ini_raw=engine_ini_raw,
        input_ini_raw=input_ini_raw,
        visual_profile=selected_profile,
        visual_profile_raw=selected_profile_raw,
        presentation=presentation,
        presentation_vulkan_icd=presentation_vulkan_icd,
        presentation_vulkan_icd_raw=presentation_vulkan_icd_raw,
        renderer_request=renderer_request,
        renderer_request_raw=renderer_request_raw,
        execution=execution,
        execution_raw=execution_raw,
        execution_sha256=execution_sha,
        dry_run_report=report,
    )


def _write_exclusive(path: Path, raw: bytes, mode: int = 0o600) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def _attempt_owner_document(token: str) -> dict[str, Any]:
    return {
        "schema_version": ATTEMPT_OWNER_SCHEMA,
        "pid": os.getpid(),
        "token": token,
    }


def _attempt_is_owned(attempt: Path, token: str) -> bool:
    """Return true only for the exact sentinel this process created."""

    owner = attempt / ".orchestrator-owner.json"
    try:
        metadata = os.lstat(owner)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            return False
        raw = owner.read_bytes()
        value = json.loads(
            raw.decode("utf-8", "strict"),
            object_pairs_hook=_duplicate_object,
            parse_constant=_reject_constant,
        )
    except (BuildHomeError, OSError, UnicodeError, json.JSONDecodeError):
        return False
    return value == _attempt_owner_document(token) and raw == canonical_json(value)


def _atomic_pointer(path: Path, value: Mapping[str, Any]) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        _fail("VISTA_HOME_BUILD_POINTER_UNSAFE", "build pointer is unsafe", pointer=str(path))
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}.{secrets.token_hex(8)}")
    _write_exclusive(temporary, canonical_json(value))
    try:
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _atomic_pointer_raw(path: Path, raw: bytes) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        _fail("VISTA_HOME_BUILD_POINTER_UNSAFE", "build pointer is unsafe", pointer=str(path))
    temporary = path.with_name(path.name + f".rollback.{os.getpid()}.{secrets.token_hex(8)}")
    _write_exclusive(temporary, raw)
    try:
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _publish_pointers_transactionally(ue_root: Path, value: Mapping[str, Any]) -> None:
    """Publish the two mutable pointers or restore their exact prior bytes."""

    lock_path = ue_root / ".publication.lock"
    try:
        lock_fd = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as exc:
        _fail("VISTA_HOME_BUILD_POINTER_UNSAFE", f"cannot open publication lock: {exc}", pointer=str(lock_path))
        raise AssertionError from exc
    try:
        if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
            _fail("VISTA_HOME_BUILD_POINTER_UNSAFE", "publication lock is not a regular file", pointer=str(lock_path))
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        paths = (ue_root / "accepted.json", ue_root / "current.json")
        previous: dict[Path, bytes | None] = {}
        for path in paths:
            if path.is_symlink() or (path.exists() and not path.is_file()):
                _fail("VISTA_HOME_BUILD_POINTER_UNSAFE", "build pointer is unsafe", pointer=str(path))
            previous[path] = path.read_bytes() if path.exists() else None
        changed: list[Path] = []
        try:
            for path in paths:
                _atomic_pointer(path, value)
                changed.append(path)
        except Exception:
            rollback_errors: list[str] = []
            for path in reversed(changed):
                try:
                    prior = previous[path]
                    if prior is None:
                        path.unlink(missing_ok=True)
                    else:
                        _atomic_pointer_raw(path, prior)
                except Exception as exc:
                    rollback_errors.append(f"{path.name}: {exc}")
            if rollback_errors:
                _fail(
                    "VISTA_HOME_BUILD_POINTER_ROLLBACK_FAILED",
                    "pointer publication failed and rollback was incomplete: " + "; ".join(rollback_errors),
                )
            raise
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def _copy_reflink_or_copy(source: str, destination: str, *, counts: Counter[str]) -> str:
    src = Path(source)
    dst = Path(destination)
    metadata = os.lstat(src)
    if not stat.S_ISREG(metadata.st_mode):
        _fail("VISTA_HOME_BUILD_COPY_UNSAFE", "copy source is not a regular file", pointer=str(src))
    source_fd = os.open(src, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    destination_fd = os.open(
        dst,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        stat.S_IMODE(metadata.st_mode),
    )
    try:
        try:
            fcntl.ioctl(destination_fd, FICLONE, source_fd)
            method = "reflink"
        except OSError as exc:
            if exc.errno not in {errno.EXDEV, errno.EOPNOTSUPP, errno.ENOTTY, errno.EINVAL, errno.ENOSYS}:
                raise
            os.close(destination_fd)
            destination_fd = -1
            dst.unlink()
            shutil.copy2(src, dst, follow_symlinks=False)
            method = "copy"
    finally:
        os.close(source_fd)
        if destination_fd >= 0:
            os.close(destination_fd)
    if method == "reflink":
        shutil.copystat(src, dst, follow_symlinks=False)
    counts[method] += 1
    return str(dst)


def _copy_tree(source: Path, destination: Path, label: str) -> Counter[str]:
    counts: Counter[str] = Counter()

    def copier(src: str, dst: str) -> str:
        return _copy_reflink_or_copy(src, dst, counts=counts)

    try:
        shutil.copytree(source, destination, copy_function=copier, symlinks=False)
    except Exception:
        # The new attempt remains quarantined evidence; never delete a partial
        # destination from an append-only attempt.
        raise
    if not destination.is_dir():
        _fail("VISTA_HOME_BUILD_COPY_FAILED", f"{label} destination was not created")
    return counts


def _load_receipt(path: Path, expected_schema: str, expected_status: str, label: str) -> tuple[dict[str, Any], str]:
    source = _existing_file(path, label)
    raw = source.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_duplicate_object, parse_constant=_reject_constant)
    except BuildHomeError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        _fail("VISTA_HOME_BUILD_RECEIPT_INVALID", f"{label} is invalid JSON", pointer=str(path))
        raise AssertionError from exc
    if not isinstance(value, dict) or value.get("schema_version") != expected_schema or value.get("status") != expected_status:
        _fail("VISTA_HOME_BUILD_RECEIPT_INVALID", f"{label} schema or status differs", pointer=str(path))
    if value.get("error") is not None:
        _fail("VISTA_HOME_BUILD_RECEIPT_INVALID", f"{label} success carries an error", pointer=str(path))
    return value, sha256_bytes(raw)


def _verify_import_receipt(receipt: Mapping[str, Any], execution: Mapping[str, Any], plan: Mapping[str, Any]) -> None:
    expected_keys = {"schema_version", "status", "error", "bindings", "content_namespace", "assets", "gates"}
    if set(receipt) != expected_keys or receipt.get("content_namespace") != plan["unreal"]["content_namespace"]:
        _fail("VISTA_HOME_BUILD_RECEIPT_INVALID", "import receipt fields or namespace differ")
    gates = receipt.get("gates")
    if gates != {
        "namespace_fresh": True,
        "all_assets_bound": True,
        "material_and_collision_inspected": True,
        "core_textures_imported_and_used": True,
        "nanite_material_policy_verified": True,
        "quarantined": False,
    }:
        _fail("VISTA_HOME_BUILD_RECEIPT_INVALID", "import receipt gates did not pass")
    bindings = receipt.get("bindings")
    expected_binding_keys = {
        "engine",
        "project",
        "execution_manifest",
        "execution_manifest_sha256",
        "build_plan_sha256",
        "composition_spec_sha256",
    }
    if not isinstance(bindings, Mapping) or set(bindings) != expected_binding_keys or (
        not isinstance(bindings.get("engine"), str)
        or not bindings.get("engine", "").startswith("5.")
        or bindings.get("project") != execution["project_file"]
        or bindings.get("execution_manifest") != str(Path(execution["attempt_root"]) / "execution.json")
        or bindings.get("execution_manifest_sha256") != sha256_bytes(planning.canonical_json(execution))
        or bindings.get("build_plan_sha256") != execution["build_plan_sha256"]
        or bindings.get("composition_spec_sha256") != execution["composition_spec_sha256"]
    ):
        _fail("VISTA_HOME_BUILD_RECEIPT_INVALID", "import receipt pins differ")
    assets = receipt.get("assets")
    if not isinstance(assets, list) or len(assets) != len(plan["assets"]):
        _fail("VISTA_HOME_BUILD_RECEIPT_INVALID", "import receipt asset inventory differs")
    plan_by_id = {item["asset_id"]: item for item in plan["assets"]}
    binding_by_id = {item["asset_id"]: item for item in execution["artifact_bindings"]}
    receipt_ids = [item.get("asset_id") for item in assets if isinstance(item, Mapping)]
    if len(receipt_ids) != len(assets) or len(receipt_ids) != len(set(receipt_ids)) or set(receipt_ids) != set(plan_by_id):
        _fail("VISTA_HOME_BUILD_RECEIPT_INVALID", "import receipt asset IDs differ or are duplicated")
    common_keys = {"asset_id", "source_kind", "uri", "source_digest", "object_path", "inspection"}
    imported_keys = common_keys | {"source_file_sha256", "raw_returned_object_paths", "returned_object_paths"}
    inspection_keys = {
        "object_path",
        "class_path",
        "collision_policies",
        "material_paths",
        "simple_collision_shapes",
        "collision_generated",
        "collision_trace_flag",
        "room_shell",
        "declared_core_texture_count",
        "returned_texture2d_paths",
        "material_texture2d_paths",
        "material_blend_modes",
        "nanite_policy",
        "nanite_enabled",
    }
    for item in assets:
        asset_id = item["asset_id"]
        source = plan_by_id[asset_id]
        expected_keys = common_keys if source["source_kind"] == "builtin" else imported_keys
        inspection = item.get("inspection")
        if (
            set(item) != expected_keys
            or item.get("source_kind") != source["source_kind"]
            or item.get("uri") != source["uri"]
            or item.get("source_digest") != source["source_digest"]
            or item.get("object_path") != derived_asset_path(plan["unreal"]["content_namespace"], source)
            or not isinstance(inspection, Mapping)
            or set(inspection) != inspection_keys
            or inspection.get("object_path") != item.get("object_path")
            or not isinstance(inspection.get("class_path"), str)
            or not inspection.get("class_path")
            or not isinstance(inspection.get("collision_policies"), list)
            or not isinstance(inspection.get("material_paths"), list)
            or isinstance(inspection.get("declared_core_texture_count"), bool)
            or not isinstance(inspection.get("declared_core_texture_count"), int)
            or inspection.get("declared_core_texture_count") < 0
            or not isinstance(inspection.get("returned_texture2d_paths"), list)
            or not all(isinstance(path, str) and path
                       for path in inspection.get("returned_texture2d_paths"))
            or inspection.get("returned_texture2d_paths")
            != sorted(set(inspection.get("returned_texture2d_paths")))
            or not isinstance(inspection.get("material_texture2d_paths"), list)
            or not all(isinstance(path, str) and path
                       for path in inspection.get("material_texture2d_paths"))
            or inspection.get("material_texture2d_paths")
            != sorted(set(inspection.get("material_texture2d_paths")))
            or not isinstance(inspection.get("material_blend_modes"), list)
            or not isinstance(inspection.get("nanite_policy"), str)
            or inspection.get("nanite_policy") not in {
                "not_applicable",
                "eligible_static_opaque",
                "disabled_nonopaque_material",
            }
            or not (
                inspection.get("nanite_enabled") is None
                or isinstance(inspection.get("nanite_enabled"), bool)
            )
        ):
            _fail("VISTA_HOME_BUILD_RECEIPT_INVALID", f"import receipt asset {asset_id} fields differ")
        returned_textures = set(inspection["returned_texture2d_paths"])
        material_textures = set(inspection["material_texture2d_paths"])
        if source["source_kind"] == "builtin" and (
            inspection["declared_core_texture_count"] != 0
            or returned_textures
            or material_textures
            or inspection["material_blend_modes"]
            or inspection["nanite_policy"] != "not_applicable"
            or inspection["nanite_enabled"] is not None
        ):
            _fail("VISTA_HOME_BUILD_RECEIPT_INVALID", f"builtin receipt asset {asset_id} carries texture evidence")
        if source["source_kind"] != "builtin" and (
            item.get("source_file_sha256") != binding_by_id[asset_id]["source_file_sha256"]
            or not isinstance(item.get("raw_returned_object_paths"), list)
            or not all(isinstance(path, str) and path for path in item["raw_returned_object_paths"])
            or not isinstance(item.get("returned_object_paths"), list)
            or not all(isinstance(path, str) and path for path in item["returned_object_paths"])
            or not returned_textures.issubset(set(item["returned_object_paths"]))
        ):
            _fail("VISTA_HOME_BUILD_RECEIPT_INVALID", f"import receipt asset {asset_id} binding differs")
        if inspection["declared_core_texture_count"] > 0 and not (
            returned_textures and material_textures and returned_textures & material_textures
        ):
            _fail(
                "VISTA_HOME_BUILD_RECEIPT_INVALID",
                f"import receipt asset {asset_id} has no imported Texture2D used by its material",
            )
        if source["source_kind"] != "builtin" and (
            not inspection["material_paths"]
            or not all(
                isinstance(path, str) and path
                for path in inspection["material_paths"]
            )
            or len(inspection["material_blend_modes"])
            != len(inspection["material_paths"])
            or not all(
                isinstance(mode, str) and
                re.fullmatch(r"BLEND_[A-Z0-9_]+", mode) is not None
                for mode in inspection["material_blend_modes"]
            )
            or not isinstance(inspection["nanite_enabled"], bool)
            or (
                inspection["nanite_policy"] == "eligible_static_opaque"
                and (
                    inspection["nanite_enabled"] is not True
                    or any(
                        mode not in {"BLEND_OPAQUE", "BLEND_MASKED"}
                        for mode in inspection["material_blend_modes"]
                    )
                )
            )
            or (
                inspection["nanite_policy"] == "disabled_nonopaque_material"
                and (
                    inspection["nanite_enabled"] is not False
                    or all(
                        mode in {"BLEND_OPAQUE", "BLEND_MASKED"}
                        for mode in inspection["material_blend_modes"]
                    )
                )
            )
        ):
            _fail(
                "VISTA_HOME_BUILD_RECEIPT_INVALID",
                f"import receipt asset {asset_id} Nanite/material policy differs",
            )


def _verify_scene_receipt(
    receipt: Mapping[str, Any],
    execution: Mapping[str, Any],
    plan: Mapping[str, Any],
    import_sha256: str,
) -> None:
    expected_keys = {
        "schema_version",
        "status",
        "error",
        "bindings",
        "content_namespace",
        "map_path",
        "actor_inventory",
        "gates",
    }
    if (
        set(receipt) != expected_keys
        or receipt.get("content_namespace") != plan["unreal"]["content_namespace"]
        or receipt.get("map_path") != plan["unreal"]["map_path"]
    ):
        _fail("VISTA_HOME_BUILD_RECEIPT_INVALID", "scene receipt fields or revision paths differ")
    gates = receipt.get("gates")
    if gates != {
        "map_saved": True,
        "map_reloaded": True,
        "semantic_tags_verified": True,
        "player_start_verified": True,
        "game_mode_configured": True,
        "navmesh_bounds_verified": True,
        "dynamic_lighting_verified": True,
        "deterministic_exposure_verified": True,
        "input_mappings_verified": True,
        "quarantined": False,
        "runtime_play_proof": "pending",
    }:
        _fail("VISTA_HOME_BUILD_RECEIPT_INVALID", "scene receipt gates did not pass")
    bindings = receipt.get("bindings")
    expected_binding_keys = {
        "engine",
        "project",
        "execution_manifest",
        "execution_manifest_sha256",
        "import_receipt",
        "import_receipt_sha256",
        "composition_spec_sha256",
        "input_config",
        "input_config_sha256",
    }
    expected_input_config = (
        Path(execution["project_file"]).parent / "Config" / "DefaultInput.ini"
    )
    expected_input_sha = sha256_bytes(default_input_ini())
    actual_input_sha = sha256_file(
        _existing_file(expected_input_config, "project input config")
    )
    if not isinstance(bindings, Mapping) or set(bindings) != expected_binding_keys or (
        not isinstance(bindings.get("engine"), str)
        or not bindings.get("engine", "").startswith("5.")
        or bindings.get("project") != execution["project_file"]
        or bindings.get("execution_manifest") != str(Path(execution["attempt_root"]) / "execution.json")
        or bindings.get("execution_manifest_sha256") != sha256_bytes(planning.canonical_json(execution))
        or bindings.get("import_receipt") != execution["import_receipt"]
        or bindings.get("import_receipt_sha256") != import_sha256
        or bindings.get("composition_spec_sha256") != execution["composition_spec_sha256"]
        or bindings.get("input_config")
        != str(expected_input_config)
        or bindings.get("input_config_sha256") != expected_input_sha
        or actual_input_sha != expected_input_sha
    ):
        _fail("VISTA_HOME_BUILD_RECEIPT_INVALID", "scene receipt pins differ")
    if not isinstance(receipt.get("actor_inventory"), list):
        _fail("VISTA_HOME_BUILD_RECEIPT_INVALID", "scene actor inventory is invalid")


def _presentation_object_path(namespace: str, target_asset_id: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_]", "_", target_asset_id)
    if not name or len(name) > 128:
        _fail(
            "VISTA_HOME_BUILD_RECEIPT_INVALID",
            "presentation target cannot form a deterministic UE object path",
        )
    return namespace + "/Presentation/" + name + "." + name


def _presentation_is_external(execution: Mapping[str, Any]) -> bool:
    bindings = execution.get("presentation_bindings")
    if not isinstance(bindings, list) or len(bindings) != 3:
        _fail(
            "VISTA_HOME_BUILD_RECEIPT_INVALID",
            "presentation execution binding inventory differs",
        )
    flags = [
        isinstance(binding, Mapping) and "external_content" in binding
        for binding in bindings
    ]
    if any(flags) and not all(flags):
        _fail(
            "VISTA_HOME_BUILD_RECEIPT_INVALID",
            "presentation execution mixes v1 and external v2 bindings",
        )
    return all(flags)


def _presentation_import_schema(execution: Mapping[str, Any]) -> str:
    return (
        PRESENTATION_IMPORT_RECEIPT_SCHEMA_V2
        if _presentation_is_external(execution)
        else PRESENTATION_IMPORT_RECEIPT_SCHEMA
    )


def _presentation_scene_schema(execution: Mapping[str, Any]) -> str:
    return (
        PRESENTATION_SCENE_RECEIPT_SCHEMA_V2
        if _presentation_is_external(execution)
        else PRESENTATION_SCENE_RECEIPT_SCHEMA
    )


def _receipt_transform_matches(value: Any, expected: Mapping[str, Any]) -> bool:
    if not isinstance(value, Mapping) or set(value) != {
        "location_cm", "rotation_deg", "scale"
    }:
        return False
    vectors: dict[str, list[float]] = {}
    for key in ("location_cm", "rotation_deg", "scale"):
        raw = value.get(key)
        if (
            not isinstance(raw, list)
            or len(raw) != 3
            or any(
                isinstance(item, bool)
                or not isinstance(item, (int, float))
                or not math.isfinite(float(item))
                for item in raw
            )
        ):
            return False
        vectors[key] = [float(item) for item in raw]
    location_ok = all(
        abs(actual - float(wanted)) <= 0.05
        for actual, wanted in zip(
            vectors["location_cm"], expected["location_cm"]
        )
    )
    rotation_ok = all(
        abs((actual - float(wanted) + 180.0) % 360.0 - 180.0) <= 0.05
        for actual, wanted in zip(
            vectors["rotation_deg"], expected["rotation_deg"]
        )
    )
    scale_ok = all(
        abs(actual - float(wanted)) <= 0.0001
        for actual, wanted in zip(vectors["scale"], expected["scale"])
    )
    return location_ok and rotation_ok and scale_ok


def _verify_presentation_import_receipt(
    receipt: Mapping[str, Any],
    execution: Mapping[str, Any],
    base_import_sha256: str,
) -> None:
    is_external = _presentation_is_external(execution)
    expected_keys = {
        "schema_version", "status", "error", "bindings", "content_namespace",
        "presentation_content_root", "assets", "gates",
    }
    namespace = execution["composition_spec"]["content_namespace"]
    if (
        set(receipt) != expected_keys
        or receipt.get("schema_version") != _presentation_import_schema(execution)
        or receipt.get("content_namespace") != namespace
        or receipt.get("presentation_content_root") != namespace + "/Presentation"
    ):
        _fail(
            "VISTA_HOME_BUILD_RECEIPT_INVALID",
            "presentation import receipt fields or namespace differ",
        )
    expected_gates = {
        "base_import_verified": True,
        "exact_three_room_bundles": True,
        "one_mesh_per_bundle": True,
        "materials_and_textures_inspected": True,
        "no_collision_source_policy": True,
        "quarantined": False,
        "runtime_play_proof": "pending",
    }
    if is_external:
        expected_gates.update({
            "external_content_preserved": True,
            "external_nanite_disabled": True,
        })
    if receipt.get("gates") != expected_gates:
        _fail(
            "VISTA_HOME_BUILD_RECEIPT_INVALID",
            "presentation import receipt gates did not pass",
        )
    bindings = receipt.get("bindings")
    expected_binding_keys = {
        "engine", "project", "execution_manifest",
        "execution_manifest_sha256", "base_import_receipt",
        "base_import_receipt_sha256", "composition_spec_sha256",
    }
    if (
        not isinstance(bindings, Mapping)
        or set(bindings) != expected_binding_keys
        or not isinstance(bindings.get("engine"), str)
        or not bindings["engine"].startswith("5.")
        or bindings.get("project") != execution["project_file"]
        or bindings.get("execution_manifest")
        != str(Path(execution["attempt_root"]) / "execution.json")
        or bindings.get("execution_manifest_sha256")
        != sha256_bytes(planning.canonical_json(execution))
        or bindings.get("base_import_receipt") != execution["import_receipt"]
        or bindings.get("base_import_receipt_sha256") != base_import_sha256
        or bindings.get("composition_spec_sha256")
        != execution["composition_spec_sha256"]
    ):
        _fail(
            "VISTA_HOME_BUILD_RECEIPT_INVALID",
            "presentation import receipt pins differ",
        )
    assets = receipt.get("assets")
    execution_bindings = {
        item["artifact_id"]: item for item in execution["presentation_bindings"]
    }
    if (
        not isinstance(assets, list)
        or len(assets) != 3
        or len(execution_bindings) != 3
        or {item.get("artifact_id") for item in assets if isinstance(item, Mapping)}
        != set(execution_bindings)
    ):
        _fail(
            "VISTA_HOME_BUILD_RECEIPT_INVALID",
            "presentation import asset inventory differs",
        )
    asset_keys = {
        "artifact_id", "target_asset_id", "room_id", "room_kind",
        "source_file_sha256", "object_path", "expected_world_transform_cm",
        "root_transform_policy", "semantic_policy", "collision_policy",
        "unreal_collision_profile", "material_ids", "source_hashes",
        "raw_returned_object_paths", "returned_object_paths", "inspection",
    }
    inspection_keys = {
        "class_path", "material_paths", "returned_texture2d_paths",
        "material_texture2d_paths", "simple_collision_shapes",
        "collision_profile_for_components", "can_ever_affect_navigation",
    }
    if is_external:
        asset_keys.update({"external_content", "nanite_policy"})
        inspection_keys.add("nanite_enabled")
    for asset in assets:
        artifact_id = asset.get("artifact_id") if isinstance(asset, Mapping) else None
        source = execution_bindings.get(artifact_id)
        inspection = asset.get("inspection") if isinstance(asset, Mapping) else None
        if (
            not isinstance(asset, Mapping)
            or source is None
            or set(asset) != asset_keys
            or asset.get("target_asset_id") != source["target_asset_id"]
            or asset.get("room_id") != source["room_id"]
            or asset.get("room_kind") != source["room_kind"]
            or asset.get("source_file_sha256") != source["source_file_sha256"]
            or asset.get("object_path")
            != _presentation_object_path(namespace, source["target_asset_id"])
            or asset.get("expected_world_transform_cm")
            != source["expected_world_transform_cm"]
            or asset.get("root_transform_policy") != source["root_transform_policy"]
            or asset.get("semantic_policy") != source["semantic_policy"]
            or asset.get("collision_policy") != source["collision_policy"]
            or asset.get("unreal_collision_profile") != "NoCollision"
            or asset.get("material_ids") != source["material_ids"]
            or asset.get("source_hashes") != source["source_hashes"]
            or not isinstance(asset.get("raw_returned_object_paths"), list)
            or not isinstance(asset.get("returned_object_paths"), list)
            or not isinstance(inspection, Mapping)
            or set(inspection) != inspection_keys
            or inspection.get("simple_collision_shapes") != 0
            or inspection.get("collision_profile_for_components") != "NoCollision"
            or inspection.get("can_ever_affect_navigation") is not False
            or not isinstance(inspection.get("material_paths"), list)
            or len(inspection["material_paths"]) != source["material_count"]
            or any(
                not isinstance(path, str)
                or not path
                or "DefaultMaterial" in path
                or "BasicShapeMaterial" in path
                for path in inspection["material_paths"]
            )
            or not set(inspection.get("returned_texture2d_paths", []))
            & set(inspection.get("material_texture2d_paths", []))
            or (
                is_external
                and (
                    asset.get("external_content") != source["external_content"]
                    or asset.get("nanite_policy")
                    != PRESENTATION_EXTERNAL_NANITE_POLICY
                    or inspection.get("nanite_enabled") is not False
                )
            )
        ):
            _fail(
                "VISTA_HOME_BUILD_RECEIPT_INVALID",
                f"presentation import asset {asset.get('artifact_id')} differs",
            )


def _verify_presentation_scene_receipt(
    receipt: Mapping[str, Any],
    execution: Mapping[str, Any],
    base_scene_sha256: str,
    presentation_import_sha256: str,
) -> None:
    is_external = _presentation_is_external(execution)
    expected_keys = {
        "schema_version", "status", "error", "bindings", "content_namespace",
        "map_path", "room_observations", "gates",
    }
    spec = execution["composition_spec"]
    if (
        set(receipt) != expected_keys
        or receipt.get("schema_version") != _presentation_scene_schema(execution)
        or receipt.get("content_namespace") != spec["content_namespace"]
        or receipt.get("map_path") != spec["map_path"]
    ):
        _fail(
            "VISTA_HOME_BUILD_RECEIPT_INVALID",
            "presentation scene receipt fields or revision paths differ",
        )
    expected_gates = {
        "map_saved": True,
        "map_reloaded": True,
        "exact_three_presentation_actors": True,
        "presentation_no_collision_verified": True,
        "hidden_r1_collision_authority_verified": True,
        "semantic_authority_preserved": True,
        "quarantined": False,
        "runtime_play_proof": "pending",
    }
    if is_external:
        expected_gates["external_nanite_disabled_verified"] = True
        expected_gates["external_r1_semantic_visual_targets_verified"] = True
    if receipt.get("gates") != expected_gates:
        _fail(
            "VISTA_HOME_BUILD_RECEIPT_INVALID",
            "presentation scene receipt gates did not pass",
        )
    bindings = receipt.get("bindings")
    expected_binding_keys = {
        "engine", "project", "execution_manifest",
        "execution_manifest_sha256", "base_scene_receipt",
        "base_scene_receipt_sha256", "presentation_import_receipt",
        "presentation_import_receipt_sha256", "composition_spec_sha256",
    }
    if (
        not isinstance(bindings, Mapping)
        or set(bindings) != expected_binding_keys
        or not isinstance(bindings.get("engine"), str)
        or not bindings["engine"].startswith("5.")
        or bindings.get("project") != execution["project_file"]
        or bindings.get("execution_manifest")
        != str(Path(execution["attempt_root"]) / "execution.json")
        or bindings.get("execution_manifest_sha256")
        != sha256_bytes(planning.canonical_json(execution))
        or bindings.get("base_scene_receipt") != execution["scene_receipt"]
        or bindings.get("base_scene_receipt_sha256") != base_scene_sha256
        or bindings.get("presentation_import_receipt")
        != execution["presentation_import_receipt"]
        or bindings.get("presentation_import_receipt_sha256")
        != presentation_import_sha256
        or bindings.get("composition_spec_sha256")
        != execution["composition_spec_sha256"]
    ):
        _fail(
            "VISTA_HOME_BUILD_RECEIPT_INVALID",
            "presentation scene receipt pins differ",
        )
    observations = receipt.get("room_observations")
    operations = {
        item["artifact_id"]: item
        for item in spec["operations"]
        if item.get("kind") == "place_room_presentation_bundle"
    }
    presentation_bindings = {
        item["artifact_id"]: item
        for item in execution["presentation_bindings"]
    }
    if (
        not isinstance(observations, list)
        or len(observations) != 3
        or len(operations) != 3
        or set(operations) != set(presentation_bindings)
    ):
        _fail(
            "VISTA_HOME_BUILD_RECEIPT_INVALID",
            "presentation room observation inventory differs",
        )
    observation_keys = {
        "artifact_id", "presentation_id", "room_id", "room_kind",
        "actor_path", "static_mesh_object_path", "world_transform_cm",
        "collision_profile", "material_slot_count",
        "attach_parent_actor_path", "r1_authority_actor_path",
        "r1_authority_collision_profile", "r1_authority_hidden_in_game",
        "r1_authority_component_visible",
    }
    if is_external:
        observation_keys.update({
            "external_content", "nanite_policy", "nanite_enabled",
            "r1_semantic_visual_observations",
        })
    entity_operations = {
        item["semantic_id"]: item
        for item in spec["operations"]
        if item.get("kind") == "place_entity"
    }
    expected_semantic_target_ids = [
        semantic_target_id
        for binding in presentation_bindings.values()
        for semantic_target_id in binding.get("external_content", {}).get(
            "semantic_target_ids", []
        )
    ]
    if is_external and (
        not expected_semantic_target_ids
        or len(expected_semantic_target_ids)
        != len(set(expected_semantic_target_ids))
        or any(
            semantic_target_id not in entity_operations
            for semantic_target_id in expected_semantic_target_ids
        )
    ):
        _fail(
            "VISTA_HOME_BUILD_RECEIPT_INVALID",
            "presentation semantic target execution inventory differs",
        )
    seen_artifacts: set[str] = set()
    actor_paths: set[str] = set()
    authority_paths: set[str] = set()
    seen_semantic_target_ids: set[str] = set()
    semantic_target_actor_paths: set[str] = set()
    semantic_target_component_paths: set[str] = set()
    for observation in observations:
        artifact_id = (
            observation.get("artifact_id")
            if isinstance(observation, Mapping)
            else None
        )
        operation = operations.get(artifact_id)
        source = presentation_bindings.get(artifact_id)
        if (
            not isinstance(observation, Mapping)
            or set(observation) != observation_keys
            or operation is None
            or source is None
            or artifact_id in seen_artifacts
            or observation.get("presentation_id") != operation["presentation_id"]
            or observation.get("room_id") != operation["room_id"]
            or observation.get("room_kind") != operation["room_kind"]
            or not isinstance(observation.get("actor_path"), str)
            or not observation["actor_path"]
            or observation["actor_path"] in actor_paths
            or observation.get("static_mesh_object_path")
            != _presentation_object_path(
                spec["content_namespace"], source["target_asset_id"]
            )
            or not _receipt_transform_matches(
                observation.get("world_transform_cm"), operation["transform"]
            )
            or observation.get("collision_profile") != "NoCollision"
            or isinstance(observation.get("material_slot_count"), bool)
            or not isinstance(observation.get("material_slot_count"), int)
            or observation.get("material_slot_count") != source["material_count"]
            or not isinstance(observation.get("attach_parent_actor_path"), str)
            or not observation["attach_parent_actor_path"]
            or observation.get("attach_parent_actor_path")
            != observation.get("r1_authority_actor_path")
            or observation.get("actor_path")
            == observation.get("r1_authority_actor_path")
            or observation["r1_authority_actor_path"] in authority_paths
            or observation.get("r1_authority_collision_profile") != "BlockAll"
            or observation.get("r1_authority_hidden_in_game") is not True
            or observation.get("r1_authority_component_visible") is not False
            or (
                is_external
                and (
                    observation.get("external_content")
                    != source["external_content"]
                    or observation.get("nanite_policy")
                    != PRESENTATION_EXTERNAL_NANITE_POLICY
                    or observation.get("nanite_enabled") is not False
                )
            )
        ):
            _fail(
                "VISTA_HOME_BUILD_RECEIPT_INVALID",
                f"presentation room observation {artifact_id} differs",
            )
        if is_external:
            target_observations = observation.get(
                "r1_semantic_visual_observations"
            )
            expected_room_target_ids = source["external_content"][
                "semantic_target_ids"
            ]
            if (
                not isinstance(target_observations, list)
                or len(target_observations) != len(expected_room_target_ids)
                or [
                    item.get("semantic_target_id")
                    if isinstance(item, Mapping) else None
                    for item in target_observations
                ] != expected_room_target_ids
            ):
                _fail(
                    "VISTA_HOME_BUILD_RECEIPT_INVALID",
                    f"presentation room semantic visual target inventory "
                    f"{artifact_id} differs",
                )
            for target_observation in target_observations:
                target_keys = {
                    "semantic_target_id", "actor_path", "actor_class_path",
                    "semantic_id_property", "actor_hidden_in_game",
                    "interaction_affordances", "render_components",
                }
                component_keys = {
                    "component_path", "visible", "collision_profile",
                    "collision_enabled",
                }
                semantic_target_id = (
                    target_observation.get("semantic_target_id")
                    if isinstance(target_observation, Mapping) else None
                )
                expected_entity = entity_operations.get(semantic_target_id)
                target_actor_path = (
                    target_observation.get("actor_path")
                    if isinstance(target_observation, Mapping) else None
                )
                components = (
                    target_observation.get("render_components")
                    if isinstance(target_observation, Mapping) else None
                )
                if (
                    not isinstance(target_observation, Mapping)
                    or set(target_observation) != target_keys
                    or expected_entity is None
                    or semantic_target_id in seen_semantic_target_ids
                    or target_observation.get("semantic_id_property")
                    != semantic_target_id
                    or target_observation.get("actor_class_path")
                    != expected_entity["actor_class"]
                    or not isinstance(target_actor_path, str)
                    or not target_actor_path
                    or target_actor_path in semantic_target_actor_paths
                    or target_actor_path in actor_paths
                    or target_actor_path in authority_paths
                    or target_observation.get("actor_hidden_in_game") is not True
                    or target_observation.get("interaction_affordances")
                    != sorted(expected_entity["affordances"])
                    or not isinstance(components, list)
                    or not components
                ):
                    _fail(
                        "VISTA_HOME_BUILD_RECEIPT_INVALID",
                        f"presentation semantic visual target "
                        f"{semantic_target_id} differs",
                    )
                local_component_paths: set[str] = set()
                for component in components:
                    component_path = (
                        component.get("component_path")
                        if isinstance(component, Mapping) else None
                    )
                    if (
                        not isinstance(component, Mapping)
                        or set(component) != component_keys
                        or not isinstance(component_path, str)
                        or not component_path
                        or component_path in local_component_paths
                        or component_path in semantic_target_component_paths
                        or component.get("visible") is not False
                        or component.get("collision_enabled") is not True
                        or component.get("collision_profile")
                        != expected_entity["collision"]["profile"]
                    ):
                        _fail(
                            "VISTA_HOME_BUILD_RECEIPT_INVALID",
                            f"presentation semantic visual target component "
                            f"{semantic_target_id} differs",
                        )
                    local_component_paths.add(component_path)
                    semantic_target_component_paths.add(component_path)
                seen_semantic_target_ids.add(semantic_target_id)
                semantic_target_actor_paths.add(target_actor_path)
        seen_artifacts.add(artifact_id)
        actor_paths.add(observation["actor_path"])
        authority_paths.add(observation["r1_authority_actor_path"])
    if seen_artifacts != set(operations):
        _fail(
            "VISTA_HOME_BUILD_RECEIPT_INVALID",
            "presentation room observations do not cover the exact room slice",
        )
    if is_external and seen_semantic_target_ids != set(
        expected_semantic_target_ids
    ):
        _fail(
            "VISTA_HOME_BUILD_RECEIPT_INVALID",
            "presentation semantic visual observations do not cover every target",
        )
    if is_external and semantic_target_actor_paths & (
        actor_paths | authority_paths
    ):
        _fail(
            "VISTA_HOME_BUILD_RECEIPT_INVALID",
            "presentation semantic visual actors overlap presentation authorities",
        )


def _result_scene_receipt_pins(
    scene_sha256: str,
    presentation_scene_sha256: str | None,
) -> dict[str, str]:
    """Keep the legacy scene receipt pin stable and name the r2 pin separately."""

    result = {
        "scene_receipt_sha256": _require_sha(
            scene_sha256, "base scene receipt SHA-256"
        )
    }
    if presentation_scene_sha256 is not None:
        result["presentation_scene_receipt_sha256"] = _require_sha(
            presentation_scene_sha256,
            "presentation scene receipt SHA-256",
        )
    return result


def _terminate_owned_process_group(process: subprocess.Popen[bytes]) -> None:
    """Best-effort bounded reap for the process group started by this tool."""

    try:
        if process.poll() is not None:
            return
    except BaseException:
        pass
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        try:
            process.terminate()
        except BaseException:
            pass
    try:
        process.wait(timeout=10)
        return
    except BaseException:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        try:
            process.kill()
        except BaseException:
            pass
    try:
        process.wait(timeout=10)
    except BaseException:
        pass


def _run_command(
    *,
    phase: str,
    argv: Sequence[str],
    environment: Mapping[str, str],
    log_path: Path,
    marker_prefix: str,
    timeout_s: int,
    marker_path: Path | None = None,
) -> dict[str, Any]:
    descriptor = os.open(
        log_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    process: subprocess.Popen[bytes] | None = None
    timed_out = False
    return_code: int | None = None
    inherited_environment = {
        key: value
        for key, value in os.environ.items()
        if key not in VULKAN_DRIVER_ENVIRONMENT_KEYS
    }
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as log:
            process = subprocess.Popen(
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                env={**inherited_environment, **dict(environment)},
                start_new_session=True,
            )
            try:
                return_code = process.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                _terminate_owned_process_group(process)
                return_code = process.poll()
            log.flush()
            os.fsync(log.fileno())
    except BaseException:
        if process is not None and process.poll() is None:
            _terminate_owned_process_group(process)
        raise
    finally:
        os.close(descriptor)
    if timed_out:
        _fail("VISTA_HOME_BUILD_COMMAND_TIMEOUT", f"{phase} commandlet exceeded {timeout_s} seconds", pointer=str(log_path))
    if process is None or return_code != 0:
        _fail("VISTA_HOME_BUILD_COMMAND_FAILED", f"{phase} commandlet exited nonzero", pointer=str(log_path))
    if marker_path is not None:
        if marker_path.parent != log_path.parent:
            _fail(
                "VISTA_HOME_BUILD_MARKER_INVALID",
                f"{phase} result marker must be beside its process log",
                pointer=str(marker_path),
            )
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            marker_descriptor = os.open(marker_path, flags)
        except FileNotFoundError:
            _fail(
                "VISTA_HOME_BUILD_MARKER_MISSING",
                f"{phase} commandlet did not publish its result marker",
                pointer=str(marker_path),
            )
        except OSError as exc:
            _fail(
                "VISTA_HOME_BUILD_MARKER_INVALID",
                f"{phase} result marker cannot be opened safely: {exc}",
                pointer=str(marker_path),
            )
        try:
            metadata = os.fstat(marker_descriptor)
            # NFSv4 servers may intentionally map client UIDs/GIDs to a
            # server-side identity.  Bind provenance to the host-created,
            # O_EXCL transcript and its private attempt directory instead of
            # comparing the mapped server UID with the client euid.
            log_metadata = os.stat(log_path, follow_symlinks=False)
            parent_metadata = os.stat(log_path.parent, follow_symlinks=False)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size > 4096
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_nlink != 1
                or metadata.st_uid != log_metadata.st_uid
                or metadata.st_gid != log_metadata.st_gid
                or metadata.st_uid != parent_metadata.st_uid
                or metadata.st_gid != parent_metadata.st_gid
            ):
                _fail(
                    "VISTA_HOME_BUILD_MARKER_INVALID",
                    f"{phase} result marker has unsafe type, size, provenance, links, or permissions",
                    pointer=str(marker_path),
                )
            chunks: list[bytes] = []
            remaining = metadata.st_size
            while remaining:
                block = os.read(marker_descriptor, remaining)
                if not block:
                    break
                chunks.append(block)
                remaining -= len(block)
            raw = b"".join(chunks)
        finally:
            os.close(marker_descriptor)
        if len(raw) != metadata.st_size:
            _fail(
                "VISTA_HOME_BUILD_MARKER_INVALID",
                f"{phase} result marker changed or could not be read completely",
                pointer=str(marker_path),
            )
        try:
            marker = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError):
            _fail(
                "VISTA_HOME_BUILD_MARKER_INVALID",
                f"{phase} result marker is not valid JSON",
                pointer=str(marker_path),
            )
        if not isinstance(marker, dict) or canonical_json(marker) != raw:
            _fail(
                "VISTA_HOME_BUILD_MARKER_INVALID",
                f"{phase} result marker is not canonical JSON",
                pointer=str(marker_path),
            )
        return marker
    marker: dict[str, Any] | None = None
    prefix = marker_prefix.encode("utf-8")
    with log_path.open("rb") as log:
        for line in log:
            marker_offset = line.find(prefix)
            if marker_offset >= 0:
                try:
                    candidate = json.loads(
                        line[marker_offset + len(prefix) :].strip().decode("utf-8")
                    )
                except (UnicodeError, json.JSONDecodeError):
                    candidate = None
                if isinstance(candidate, dict):
                    marker = candidate
    if marker is None:
        _fail("VISTA_HOME_BUILD_MARKER_MISSING", f"{phase} commandlet did not emit its result marker", pointer=str(log_path))
    return marker


def _verify_marker(marker: Mapping[str, Any], *, status: str, receipt: Path, sha256: str, phase: str) -> None:
    if set(marker) != {"status", "receipt", "sha256"} or marker != {
        "status": status,
        "receipt": str(receipt),
        "sha256": sha256,
    }:
        _fail("VISTA_HOME_BUILD_MARKER_INVALID", f"{phase} marker disagrees with its verified receipt")


def _materialize_inputs(planned: PlannedBuild, *, owner_token: str | None = None) -> tuple[Path, Counter[str]]:
    config = planned.config
    run_root, attempt = _validate_destination(config)
    ue_root = run_root / "ue"
    ue_root.mkdir(mode=0o700, exist_ok=True)
    if ue_root.is_symlink() or not ue_root.is_dir():
        _fail("VISTA_HOME_BUILD_ATTEMPT_INVALID", "UE run root is unsafe", pointer=str(ue_root))
    attempt.mkdir(mode=0o700, exist_ok=False)
    if owner_token is not None:
        _write_exclusive(attempt / ".orchestrator-owner.json", canonical_json(_attempt_owner_document(owner_token)))
    contracts_dir = attempt / "contracts"
    project_root = attempt / "project"
    config_dir = project_root / "Config"
    content_root = project_root / "Content"
    plugins_root = project_root / "Plugins"
    for directory in (contracts_dir, project_root, config_dir, content_root, plugins_root):
        directory.mkdir(mode=0o700, exist_ok=False)
    build_plan_target = contracts_dir / "build-plan.json"
    _write_exclusive(build_plan_target, planning.canonical_json(planned.plan))
    visual_profile_target: Path | None = None
    renderer_request_target: Path | None = None
    presentation_manifest_target: Path | None = None
    presentation_artifact_receipt_target: Path | None = None
    presentation_vulkan_icd_target: Path | None = None
    if planned.visual_profile is not None:
        if planned.visual_profile_raw is None or planned.renderer_request_raw is None:
            _fail(
                "VISTA_HOME_BUILD_EXECUTION_DRIFT",
                "r2 plan lost its pinned profile or renderer request bytes",
            )
        visual_profile_target = contracts_dir / VISUAL_PROFILE_ATTEMPT_FILE
        renderer_request_target = contracts_dir / RENDERER_REQUEST_ATTEMPT_FILE
        _write_exclusive(visual_profile_target, planned.visual_profile_raw)
        _write_exclusive(renderer_request_target, planned.renderer_request_raw)
    if planned.presentation is not None:
        presentation_manifest_target = (
            contracts_dir / PRESENTATION_MANIFEST_ATTEMPT_FILE
        )
        presentation_artifact_receipt_target = (
            contracts_dir / PRESENTATION_ARTIFACT_RECEIPT_ATTEMPT_FILE
        )
        _write_exclusive(
            presentation_manifest_target,
            planned.presentation.manifest_raw,
        )
        _write_exclusive(
            presentation_artifact_receipt_target,
            planned.presentation.artifact_receipt_raw,
        )
    if planned.presentation_vulkan_icd is not None:
        if planned.presentation_vulkan_icd_raw is None:
            _fail(
                "VISTA_HOME_BUILD_EXECUTION_DRIFT",
                "GPU presentation plan lost its pinned Vulkan ICD bytes",
            )
        presentation_vulkan_icd_target = (
            contracts_dir / PRESENTATION_VULKAN_ICD_ATTEMPT_FILE
        )
        _write_exclusive(
            presentation_vulkan_icd_target,
            planned.presentation_vulkan_icd_raw,
        )
    project_file = project_root / EXPECTED_PROJECT_NAME
    _write_exclusive(project_file, planned.project_raw)
    _write_exclusive(config_dir / "DefaultEngine.ini", planned.engine_ini_raw)
    _write_exclusive(config_dir / "DefaultInput.ini", planned.input_ini_raw)
    copy_counts: Counter[str] = Counter()
    copy_counts.update(_copy_tree(config.plugin_package, plugins_root / EXPECTED_PLUGIN_NAME, "compiled plugin"))
    copy_counts.update(_copy_tree(config.characters_content, content_root / "Characters", "Characters content"))
    installed_plugin = snapshot_tree(plugins_root / EXPECTED_PLUGIN_NAME, "installed compiled plugin")
    installed_characters = snapshot_tree(content_root / "Characters", "installed Characters content")
    if installed_plugin.sha256 != planned.plugin_snapshot.sha256 or installed_characters.sha256 != planned.characters_snapshot.sha256:
        _fail("VISTA_HOME_BUILD_COPY_FAILED", "installed project content differs from its pinned source")
    contract_presentation_bindings, external_presentation = (
        _presentation_planner_projection(planned.presentation)
    )
    contract_generated = contract.build_execution_manifest(
        build_plan_path=build_plan_target,
        build_plan=planned.plan,
        project_file=project_file,
        attempt_root=attempt,
        artifact_bindings=planned.bindings,
        import_receipt=attempt / "import-receipt.json",
        scene_receipt=attempt / "scene-receipt.json",
        visual_profile=planned.visual_profile,
        visual_profile_path=visual_profile_target,
        visual_profile_sha256=(
            planned.config.visual_profile_sha256
            if planned.visual_profile is not None
            else None
        ),
        renderer_request_path=renderer_request_target,
        renderer_request_sha256=(
            sha256_bytes(planned.renderer_request_raw)
            if planned.renderer_request_raw is not None
            else None
        ),
        renderer_request_content_digest=(
            planned.renderer_request["content_digest"]
            if planned.renderer_request is not None
            else None
        ),
        presentation_manifest_path=presentation_manifest_target,
        presentation_manifest_sha256=(
            planned.config.presentation_manifest_sha256
            if planned.presentation is not None
            else None
        ),
        presentation_artifact_receipt_path=(
            presentation_artifact_receipt_target
        ),
        presentation_artifact_receipt_sha256=(
            planned.config.presentation_artifact_receipt_sha256
            if planned.presentation is not None
            else None
        ),
        presentation_bindings=contract_presentation_bindings,
    )
    if external_presentation:
        generated_value, generated_raw, generated_sha = _planned_execution(
            plan=planned.plan,
            attempt=attempt,
            bindings=planned.bindings,
            project_sha256=contract_generated.value["project_sha256"],
            build_plan_sha256=contract_generated.value["build_plan_sha256"],
            visual_profile=planned.visual_profile,
            visual_profile_sha256=planned.config.visual_profile_sha256,
            renderer_request=planned.renderer_request,
            presentation=planned.presentation,
            presentation_manifest_sha256=(
                planned.config.presentation_manifest_sha256
            ),
            presentation_artifact_receipt_sha256=(
                planned.config.presentation_artifact_receipt_sha256
            ),
        )
    else:
        generated_value = contract_generated.value
        generated_raw = contract_generated.raw
        generated_sha = contract_generated.sha256
    if generated_raw != planned.execution_raw or generated_sha != planned.execution_sha256:
        _fail("VISTA_HOME_BUILD_EXECUTION_DRIFT", "materialized execution manifest differs from the dry-run plan")
    _write_exclusive(attempt / "execution.json", generated_raw)
    preparation = {
        "schema_version": PREPARATION_RECEIPT_SCHEMA,
        "status": "prepared",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "attempt_root": str(attempt),
        "orchestrator_plan_digest": planned.dry_run_report["content_digest"],
        "execution_sha256": generated_sha,
        "project_sha256": generated_value["project_sha256"],
        "input_config_sha256": sha256_bytes(planned.input_ini_raw),
        "build_plan_sha256": generated_value["build_plan_sha256"],
        "plugin_tree_sha256": installed_plugin.sha256,
        "characters_tree_sha256": installed_characters.sha256,
        "copy_methods": dict(sorted(copy_counts.items())),
    }
    if planned.visual_profile is not None:
        preparation.update({
            "visual_profile_sha256": planned.config.visual_profile_sha256,
            "visual_profile_content_digest": planned.visual_profile["content_digest"],
            "renderer_profile_request_sha256": sha256_bytes(
                planned.renderer_request_raw or b""
            ),
            "renderer_profile_request_content_digest": planned.renderer_request[
                "content_digest"
            ],
            "renderer_runtime_observation": "pending",
        })
    if planned.presentation is not None:
        preparation.update({
            "presentation_manifest_sha256": (
                planned.config.presentation_manifest_sha256
            ),
            "presentation_artifact_receipt_sha256": (
                planned.config.presentation_artifact_receipt_sha256
            ),
            "presentation_bundle_count": len(planned.presentation.bindings),
            "presentation_ue_import_observation": "pending",
            "presentation_runtime_play_proof": "pending",
        })
    if planned.presentation_vulkan_icd is not None:
        preparation.update({
            "presentation_vulkan_icd": str(presentation_vulkan_icd_target),
            "presentation_vulkan_icd_sha256": (
                planned.presentation_vulkan_icd["sha256"]
            ),
            "presentation_vulkan_library_path": (
                planned.presentation_vulkan_icd["library_path"]
            ),
        })
    _write_exclusive(attempt / "preparation-receipt.json", canonical_json(preparation))
    return attempt, copy_counts


def apply_build(planned: PlannedBuild) -> dict[str, Any]:
    # Revalidate the executable immediately before the first side effect.
    _validate_editor(planned.config, require_existing=True)
    # Keep the lexical attempt identity before materialization so a copy or
    # execution-manifest failure can still receive an append-only quarantine
    # receipt.  The path is inspected again before any receipt is written.
    attempt = planned.config.attempt_root
    owner_token = secrets.token_hex(32)
    try:
        attempt, copy_counts = _materialize_inputs(planned, owner_token=owner_token)
        commandlet_phases = ["import", "compose"]
        if planned.presentation is not None:
            commandlet_phases = list(COMMANDLET_PHASES)
        _prepare_commandlet_runtime(attempt, commandlet_phases)
        execution_path = attempt / "execution.json"
        project_path = attempt / "project" / EXPECTED_PROJECT_NAME
        common_env = {
            "VISTA_PLAYABLE_HOME_EXECUTION": str(execution_path),
            "VISTA_PLAYABLE_HOME_EXECUTION_SHA256": planned.execution_sha256,
            "VISTA_PLAYABLE_HOME_PROJECT": str(project_path),
        }
        import_receipt_path = attempt / "import-receipt.json"
        import_marker = _run_command(
            phase="import",
            argv=_fixed_command(
                planned.config.unreal_editor_cmd,
                project_path,
                Path(planned.execution["scripts"]["import"]["path"]),
                attempt_root=attempt,
                phase="import",
            ),
            environment=_commandlet_environment(attempt, "import", common_env),
            log_path=attempt / "import.log",
            marker_prefix=IMPORT_MARKER,
            timeout_s=planned.config.command_timeout_s,
            marker_path=attempt / IMPORT_RESULT_FILE,
        )
        import_receipt, import_sha = _load_receipt(
            import_receipt_path,
            IMPORT_RECEIPT_SCHEMA,
            "imported_candidate",
            "import receipt",
        )
        _verify_import_receipt(import_receipt, planned.execution, planned.plan)
        _verify_marker(
            import_marker,
            status="imported_candidate",
            receipt=import_receipt_path,
            sha256=import_sha,
            phase="import",
        )

        presentation_import_sha: str | None = None
        if planned.presentation is not None:
            presentation_import_bindings = {
                **common_env,
                "VISTA_PLAYABLE_HOME_IMPORT_RECEIPT_SHA256": import_sha,
            }
            if planned.presentation_vulkan_icd is not None:
                presentation_import_bindings[PRESENTATION_VULKAN_ICD_ENV] = str(
                    attempt
                    / "contracts"
                    / PRESENTATION_VULKAN_ICD_ATTEMPT_FILE
                )
            presentation_import_receipt_path = (
                attempt / "presentation-import-receipt.json"
            )
            presentation_import_marker = _run_command(
                phase="presentation_import",
                argv=_fixed_command(
                    planned.config.unreal_editor_cmd,
                    project_path,
                    Path(
                        planned.execution["presentation_scripts"]["import"]["path"]
                    ),
                    attempt_root=attempt,
                    phase="presentation_import",
                    presentation_import_gpu0_rendering=(
                        planned.config.presentation_import_gpu0_rendering
                    ),
                ),
                environment=_commandlet_environment(
                    attempt,
                    "presentation_import",
                    presentation_import_bindings,
                ),
                log_path=attempt / "presentation-import.log",
                marker_prefix=PRESENTATION_IMPORT_MARKER,
                timeout_s=planned.config.command_timeout_s,
                marker_path=attempt / PRESENTATION_IMPORT_RESULT_FILE,
            )
            presentation_import_receipt, presentation_import_sha = _load_receipt(
                presentation_import_receipt_path,
                _presentation_import_schema(planned.execution),
                "imported_candidate",
                "presentation import receipt",
            )
            _verify_presentation_import_receipt(
                presentation_import_receipt,
                planned.execution,
                import_sha,
            )
            _verify_marker(
                presentation_import_marker,
                status="imported_candidate",
                receipt=presentation_import_receipt_path,
                sha256=presentation_import_sha,
                phase="presentation_import",
            )

        scene_receipt_path = attempt / "scene-receipt.json"
        scene_marker = _run_command(
            phase="compose",
            argv=_fixed_command(
                planned.config.unreal_editor_cmd,
                project_path,
                Path(planned.execution["scripts"]["compose"]["path"]),
                attempt_root=attempt,
                phase="compose",
            ),
            environment=_commandlet_environment(
                attempt,
                "compose",
                {
                    **common_env,
                    "VISTA_PLAYABLE_HOME_IMPORT_RECEIPT_SHA256": import_sha,
                },
            ),
            log_path=attempt / "compose.log",
            marker_prefix=SCENE_MARKER,
            timeout_s=planned.config.command_timeout_s,
            marker_path=attempt / SCENE_RESULT_FILE,
        )
        scene_receipt, scene_sha = _load_receipt(
            scene_receipt_path,
            SCENE_RECEIPT_SCHEMA,
            "saved_reloaded_candidate",
            "scene receipt",
        )
        _verify_scene_receipt(scene_receipt, planned.execution, planned.plan, import_sha)
        _verify_marker(
            scene_marker,
            status="saved_reloaded_candidate",
            receipt=scene_receipt_path,
            sha256=scene_sha,
            phase="compose",
        )
        presentation_scene_sha: str | None = None
        if planned.presentation is not None:
            if presentation_import_sha is None:
                _fail(
                    "VISTA_HOME_BUILD_RECEIPT_INVALID",
                    "presentation compose lost its verified import receipt",
                )
            presentation_scene_receipt_path = (
                attempt / "presentation-scene-receipt.json"
            )
            presentation_scene_marker = _run_command(
                phase="presentation_compose",
                argv=_fixed_command(
                    planned.config.unreal_editor_cmd,
                    project_path,
                    Path(
                        planned.execution["presentation_scripts"]["compose"]["path"]
                    ),
                    attempt_root=attempt,
                    phase="presentation_compose",
                ),
                environment=_commandlet_environment(
                    attempt,
                    "presentation_compose",
                    {
                        **common_env,
                        "VISTA_PLAYABLE_HOME_IMPORT_RECEIPT_SHA256": import_sha,
                        "VISTA_PLAYABLE_HOME_PRESENTATION_IMPORT_RECEIPT_SHA256": (
                            presentation_import_sha
                        ),
                        "VISTA_PLAYABLE_HOME_SCENE_RECEIPT_SHA256": scene_sha,
                    },
                ),
                log_path=attempt / "presentation-compose.log",
                marker_prefix=PRESENTATION_SCENE_MARKER,
                timeout_s=planned.config.command_timeout_s,
                marker_path=attempt / PRESENTATION_SCENE_RESULT_FILE,
            )
            presentation_scene_receipt, presentation_scene_sha = _load_receipt(
                presentation_scene_receipt_path,
                _presentation_scene_schema(planned.execution),
                "saved_reloaded_candidate",
                "presentation scene receipt",
            )
            _verify_presentation_scene_receipt(
                presentation_scene_receipt,
                planned.execution,
                scene_sha,
                presentation_import_sha,
            )
            _verify_marker(
                presentation_scene_marker,
                status="saved_reloaded_candidate",
                receipt=presentation_scene_receipt_path,
                sha256=presentation_scene_sha,
                phase="presentation_compose",
            )
        result = {
            "schema_version": RESULT_RECEIPT_SCHEMA,
            "status": "accepted_candidate",
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "attempt_root": str(attempt),
            "revision": planned.config.expected_revision,
            "map_path": planned.plan["unreal"]["map_path"],
            "execution_sha256": planned.execution_sha256,
            "import_receipt_sha256": import_sha,
            **_result_scene_receipt_pins(scene_sha, presentation_scene_sha),
            "copy_methods": dict(sorted(copy_counts.items())),
            "runtime_play_proof": "pending",
        }
        if planned.visual_profile is not None:
            result.update({
                "visual_profile_id": planned.visual_profile["visual_profile_id"],
                "visual_profile_sha256": planned.config.visual_profile_sha256,
                "visual_profile_content_digest": planned.visual_profile["content_digest"],
                "renderer_profile_request_sha256": sha256_bytes(
                    planned.renderer_request_raw or b""
                ),
                "renderer_profile_request_content_digest": planned.renderer_request[
                    "content_digest"
                ],
                "renderer_runtime_observation": "pending",
            })
        if planned.presentation is not None:
            result.update({
                "base_scene_receipt_sha256": scene_sha,
                "presentation_import_receipt_sha256": presentation_import_sha,
                "presentation_manifest_sha256": (
                    planned.config.presentation_manifest_sha256
                ),
                "presentation_artifact_receipt_sha256": (
                    planned.config.presentation_artifact_receipt_sha256
                ),
                "presentation_bundle_count": len(planned.presentation.bindings),
                "presentation_collision_policy": (
                    planning.PRESENTATION_COLLISION_POLICY
                ),
                "presentation_ue_import_observation": "verified_by_commandlet",
                "presentation_runtime_play_proof": "pending",
            })
            if _presentation_is_external(planned.execution):
                result.update({
                    "presentation_external_content_verified": True,
                    "presentation_external_nanite_policy": (
                        PRESENTATION_EXTERNAL_NANITE_POLICY
                    ),
                    "presentation_external_nanite_disabled_verified": True,
                })
        result["content_digest"] = _content_digest(result)
        result_path = attempt / "result-receipt.json"
        _write_exclusive(result_path, canonical_json(result))
        pointer = {
            "schema_version": POINTER_SCHEMA,
            "attempt": attempt.name,
            "result_receipt": f"{attempt.name}/result-receipt.json",
            "result_receipt_sha256": sha256_file(result_path),
            "revision": planned.config.expected_revision,
        }
        ue_root = attempt.parent
        _publish_pointers_transactionally(ue_root, pointer)
        return result
    except BaseException as exc:
        if _attempt_is_owned(attempt, owner_token):
            failure_path = attempt / "result-receipt.json"
            if not failure_path.exists():
                failure = {
                    "schema_version": RESULT_RECEIPT_SCHEMA,
                    "status": "failed_quarantined",
                    "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                    "attempt_root": str(attempt),
                    "error": {
                        "type": type(exc).__name__,
                        "code": exc.code if isinstance(exc, BuildHomeError) else "VISTA_HOME_BUILD_UNEXPECTED",
                        "message": str(exc)[:512],
                    },
                }
                try:
                    _write_exclusive(failure_path, canonical_json(failure))
                except Exception:
                    pass
            else:
                try:
                    publication_failure_path = attempt / "publication-failure.json"
                    publication_failure = {
                        "schema_version": RESULT_RECEIPT_SCHEMA,
                        "status": "publication_failed_quarantined",
                        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                        "attempt_root": str(attempt),
                        "result_receipt_sha256": sha256_file(failure_path),
                        "error": {
                            "type": type(exc).__name__,
                            "code": exc.code if isinstance(exc, BuildHomeError) else "VISTA_HOME_BUILD_UNEXPECTED",
                            "message": str(exc)[:512],
                        },
                    }
                    _write_exclusive(publication_failure_path, canonical_json(publication_failure))
                except Exception:
                    pass
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate or execute one append-only VISTA Playable Home Unreal build",
    )
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--attempt-root", required=True, type=Path)
    parser.add_argument("--build-plan", required=True, type=Path)
    parser.add_argument("--build-plan-sha256", required=True)
    parser.add_argument("--blender-manifest", required=True, type=Path)
    parser.add_argument("--blender-manifest-sha256", required=True)
    parser.add_argument("--visual-binding-manifest", type=Path)
    parser.add_argument("--visual-binding-manifest-sha256")
    parser.add_argument(
        "--visual-profile",
        type=Path,
        help="absolute path to the closed realistic_interior_r2 visual profile",
    )
    parser.add_argument(
        "--visual-profile-sha256",
        help="expected lowercase SHA-256 for --visual-profile",
    )
    parser.add_argument(
        "--presentation-manifest",
        type=Path,
        help="absolute normalized r2 forge manifest containing ue_import_bundles",
    )
    parser.add_argument(
        "--presentation-manifest-sha256",
        help="expected lowercase SHA-256 for --presentation-manifest",
    )
    parser.add_argument(
        "--presentation-artifact-receipt",
        type=Path,
        help="absolute r2 artifact receipt paired with the presentation manifest",
    )
    parser.add_argument(
        "--presentation-artifact-receipt-sha256",
        help="expected lowercase SHA-256 for --presentation-artifact-receipt",
    )
    parser.add_argument("--plugin-package", required=True, type=Path)
    parser.add_argument("--plugin-package-tree-sha256", required=True)
    parser.add_argument("--characters-content", required=True, type=Path)
    parser.add_argument("--characters-content-tree-sha256", required=True)
    parser.add_argument("--unreal-editor-cmd", required=True, type=Path)
    parser.add_argument("--unreal-editor-cmd-sha256")
    parser.add_argument("--expected-revision", default=EXPECTED_REVISION, choices=[EXPECTED_REVISION])
    parser.add_argument("--command-timeout-s", type=int, default=3600)
    parser.add_argument(
        "--presentation-import-gpu0-rendering",
        action="store_true",
        help=(
            "explicit fresh-attempt retry mode for presentation import; enables "
            "commandlet rendering offscreen on graphics adapter 0"
        ),
    )
    parser.add_argument(
        "--presentation-vulkan-icd",
        type=Path,
        help=(
            "absolute pinned NVIDIA EGL Vulkan ICD JSON; required with "
            "--presentation-import-gpu0-rendering"
        ),
    )
    parser.add_argument(
        "--presentation-vulkan-icd-sha256",
        help="expected lowercase SHA-256 for --presentation-vulkan-icd",
    )
    parser.add_argument("--apply", action="store_true", help="materialize and run the two fixed UE commandlets")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = BuildConfig(
        run_root=args.run_root,
        attempt_root=args.attempt_root,
        build_plan=args.build_plan,
        build_plan_sha256=args.build_plan_sha256,
        blender_manifest=args.blender_manifest,
        blender_manifest_sha256=args.blender_manifest_sha256,
        visual_binding_manifest=args.visual_binding_manifest,
        visual_binding_manifest_sha256=args.visual_binding_manifest_sha256,
        visual_profile=args.visual_profile,
        visual_profile_sha256=args.visual_profile_sha256,
        presentation_manifest=args.presentation_manifest,
        presentation_manifest_sha256=args.presentation_manifest_sha256,
        presentation_artifact_receipt=args.presentation_artifact_receipt,
        presentation_artifact_receipt_sha256=(
            args.presentation_artifact_receipt_sha256
        ),
        plugin_package=args.plugin_package,
        plugin_package_tree_sha256=args.plugin_package_tree_sha256,
        characters_content=args.characters_content,
        characters_content_tree_sha256=args.characters_content_tree_sha256,
        unreal_editor_cmd=args.unreal_editor_cmd,
        unreal_editor_cmd_sha256=args.unreal_editor_cmd_sha256,
        expected_revision=args.expected_revision,
        command_timeout_s=args.command_timeout_s,
        presentation_import_gpu0_rendering=(
            args.presentation_import_gpu0_rendering
        ),
        presentation_vulkan_icd=args.presentation_vulkan_icd,
        presentation_vulkan_icd_sha256=(
            args.presentation_vulkan_icd_sha256
        ),
    )
    try:
        planned = plan_build(config, require_editor=args.apply)
        if args.apply:
            result = apply_build(planned)
        else:
            result = planned.dry_run_report
        sys.stdout.buffer.write(canonical_json(result))
        return 0
    except BuildHomeError as exc:
        sys.stderr.buffer.write(canonical_json({"ok": False, "error": exc.public_dict()}))
        return 2
    except (contract.VistaPlayableHomeContractError, planning.VistaPlayableHomePlanError) as exc:
        sys.stderr.buffer.write(
            canonical_json(
                {
                    "ok": False,
                    "error": {
                        "code": getattr(exc, "code", "VISTA_HOME_BUILD_CONTRACT_ERROR"),
                        "message": str(exc),
                    },
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
