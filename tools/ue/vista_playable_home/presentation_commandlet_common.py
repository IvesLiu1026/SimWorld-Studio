"""Fail-closed helpers for the additive r2 presentation commandlets.

The accepted r1 import/compose scripts and their execution hashes remain
untouched.  These helpers validate that legacy execution first, then validate
the separately pinned presentation extension before either extension phase can
read a GLB or mutate the candidate map.
"""

from __future__ import annotations

import json
import os
import pathlib
import re

import commandlet_common as base


PRESENTATION_IMPORT_RECEIPT_SCHEMA = (
    "simworld.vista.playable-home-ue-presentation-import-receipt/v1"
)
PRESENTATION_SCENE_RECEIPT_SCHEMA = (
    "simworld.vista.playable-home-ue-presentation-scene-receipt/v1"
)
PRESENTATION_IMPORT_MARKER = "VISTA_PLAYABLE_HOME_PRESENTATION_IMPORT_RESULT:"
PRESENTATION_SCENE_MARKER = "VISTA_PLAYABLE_HOME_PRESENTATION_SCENE_RESULT:"
PRESENTATION_IMPORT_RESULT_FILE = "presentation-import-result.json"
PRESENTATION_SCENE_RESULT_FILE = "presentation-scene-result.json"
PRESENTATION_IMPORT_SHA_ENV = (
    "VISTA_PLAYABLE_HOME_PRESENTATION_IMPORT_RECEIPT_SHA256"
)
BASE_SCENE_SHA_ENV = "VISTA_PLAYABLE_HOME_SCENE_RECEIPT_SHA256"
PRESENTATION_BINDING_KEYS = {
    "artifact_id",
    "artifact_kind",
    "target_asset_id",
    "room_id",
    "room_kind",
    "relative_path",
    "source_file",
    "source_file_sha256",
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
}
SAFE_UE_NAME = re.compile(r"^[A-Za-z0-9_]{1,128}$")


def presentation_asset_name(target_asset_id):
    value = re.sub(r"[^A-Za-z0-9_]", "_", str(target_asset_id))
    base.require(SAFE_UE_NAME.fullmatch(value) is not None,
                 "presentation target cannot form a safe UE asset name")
    return value


def derived_presentation_asset_path(namespace, binding):
    name = presentation_asset_name(binding["target_asset_id"])
    return namespace + "/Presentation/" + name + "." + name


def _load_json_file(path, expected_sha, label):
    source = base.canonical_path(path)
    base.require(os.path.isfile(source), label + " is missing")
    base.require(base.sha256_file(source) == base.require_sha(expected_sha, label),
                 label + " digest mismatch")
    with open(source, "r", encoding="utf-8") as handle:
        value = json.load(handle)
    base.require(isinstance(value, dict), label + " root must be an object")
    return value, source


def load_presentation_execution(script_kind, script_file):
    # Reuse the unchanged r1 verifier by asking it to validate its pinned
    # legacy import script.  The presentation identity is checked immediately
    # afterward against its separate pin set.
    legacy_import = pathlib.Path(__file__).with_name(
        "import_assets_commandlet.py"
    ).resolve()
    execution, manifest_path, manifest_sha = base.load_execution(
        "import", str(legacy_import)
    )
    base.require(execution.get("presentation_runtime_proof") == "pending",
                 "presentation runtime proof must remain pending")
    base.require(isinstance(execution.get("visual_profile_path"), str),
                 "presentation execution has no selected visual profile")
    scripts = execution.get("presentation_scripts")
    base.require(isinstance(scripts, dict) and set(scripts) == {
        "import", "compose", "common"
    }, "presentation script pins differ")
    common_pin = scripts["common"]
    base.require(base.canonical_path(__file__) == base.canonical_path(common_pin["path"]),
                 "presentation common helper identity mismatch")
    base.require(base.sha256_file(__file__) == common_pin["sha256"],
                 "presentation common helper digest mismatch")
    script_pin = scripts[script_kind]
    base.require(base.canonical_path(script_file) == base.canonical_path(script_pin["path"]),
                 "presentation commandlet identity mismatch")
    base.require(base.sha256_file(script_file) == script_pin["sha256"],
                 "presentation commandlet digest mismatch")

    sources = execution.get("presentation_sources")
    base.require(isinstance(sources, dict) and set(sources) == {
        "manifest", "artifact_receipt"
    }, "presentation source pins differ")
    for name, record in sources.items():
        base.require(isinstance(record, dict) and set(record) == {"path", "sha256"},
                     "presentation source record differs")
        path = base.safe_attempt_child(
            record["path"], execution["attempt_root"], "presentation " + name
        )
        base.require(os.path.isfile(path) and
                     base.sha256_file(path) == base.require_sha(record["sha256"], name),
                     "presentation " + name + " pin mismatch")

    bindings = execution.get("presentation_bindings")
    base.require(isinstance(bindings, list) and len(bindings) == 3,
                 "presentation execution needs exactly three bindings")
    room_ids = set()
    artifact_ids = set()
    for binding in bindings:
        base.require(isinstance(binding, dict) and set(binding) == PRESENTATION_BINDING_KEYS,
                     "presentation execution binding fields differ")
        source = base.canonical_path(binding["source_file"])
        expected = base.require_sha(binding["source_file_sha256"], "presentation GLB")
        base.require(expected == binding["sha256"] and os.path.isfile(source) and
                     base.sha256_file(source) == expected,
                     "presentation GLB source pin mismatch")
        base.require(binding["artifact_kind"] == "ue_import_bundle" and
                     binding["unreal_collision_profile"] == "NoCollision" and
                     binding["mesh_count"] == 1 and
                     binding["room_id"] not in room_ids and
                     binding["artifact_id"] not in artifact_ids,
                     "presentation binding identity or policy differs")
        room_ids.add(binding["room_id"])
        artifact_ids.add(binding["artifact_id"])
    return execution, manifest_path, manifest_sha


def load_verified_receipt(path, expected_sha, schema, status, label):
    value, canonical = _load_json_file(path, expected_sha, label)
    base.require(value.get("schema_version") == schema and
                 value.get("status") == status and value.get("error") is None,
                 label + " schema, status, or error differs")
    return value, canonical


canonical_json = base.canonical_json
canonical_path = base.canonical_path
require = base.require
require_sha = base.require_sha
safe_attempt_child = base.safe_attempt_child
sha256_file = base.sha256_file
write_exclusive_receipt = base.write_exclusive_receipt
