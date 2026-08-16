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
PRESENTATION_IMPORT_RECEIPT_SCHEMA_V2 = (
    "simworld.vista.playable-home-ue-presentation-import-receipt/v2"
)
PRESENTATION_SCENE_RECEIPT_SCHEMA = (
    "simworld.vista.playable-home-ue-presentation-scene-receipt/v1"
)
PRESENTATION_SCENE_RECEIPT_SCHEMA_V2 = (
    "simworld.vista.playable-home-ue-presentation-scene-receipt/v2"
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
PRESENTATION_BINDING_KEYS_V2 = PRESENTATION_BINDING_KEYS | {"external_content"}
PRESENTATION_EXTERNAL_CONTENT_KEYS = {
    "schema_version", "normalization_policy", "acquisition_receipt",
    "placement_manifest_sha256", "placement_plan_sha256",
    "semantic_target_ids", "dressing_ids", "asset_sources",
}
SAFE_UE_NAME = re.compile(r"^[A-Za-z0-9_]{1,128}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SIMPLE_COLLISION_ELEMENT_PROPERTIES = (
    "box_elems",
    "sphere_elems",
    "sphyl_elems",
    "convex_elems",
    "tapered_capsule_elems",
    "level_set_elems",
    "ml_level_set_elems",
    "skinned_level_set_elems",
    "skinned_triangle_mesh_elems",
)
PRESENTATION_AFFORDANCE_NAMES = frozenset({
    "open",
    "close",
    "pick_up",
    "drop",
    "place",
    "toggle",
    "sit",
    "inspect",
})


def reflected_affordance_name(value, enum_type):
    """Normalize a reflected UE enum member without parsing its repr."""

    member_name = getattr(value, "name", None)
    base.require(
        isinstance(member_name, str)
        and member_name
        and member_name == member_name.upper(),
        "semantic target affordance member name is invalid",
    )
    normalized = member_name.lower()
    base.require(
        normalized in PRESENTATION_AFFORDANCE_NAMES
        and getattr(enum_type, member_name, None) is value,
        "semantic target affordance member is not a closed VISTA enum value",
    )
    return normalized


def property_or_none(value, name):
    try:
        return value.get_editor_property(name)
    except Exception:
        return None


def simple_collision_count(mesh):
    body_setup = property_or_none(mesh, "body_setup")
    require(body_setup is not None,
            "presentation StaticMesh BodySetup is unavailable")
    aggregate = property_or_none(body_setup, "agg_geom")
    require(aggregate is not None,
            "presentation StaticMesh aggregate collision is unavailable")
    total = 0
    for name in SIMPLE_COLLISION_ELEMENT_PROPERTIES:
        values = property_or_none(aggregate, name)
        require(values is not None,
                "presentation collision element array is unavailable: " + name)
        total += len(values)
    return total


def clear_simple_collision(mesh):
    """Clear every UE 5.7 aggregate collision array without editor subsystems.

    EditorStaticMeshLibrary.remove_collisions delegates to
    StaticMeshEditorSubsystem, which is unavailable in commandlets.  Mutating
    the BodySetup aggregate directly is the commandlet-safe path and keeps the
    no-collision policy fail closed when UE adds non-legacy shape families.
    """

    body_setup = property_or_none(mesh, "body_setup")
    require(body_setup is not None,
            "presentation StaticMesh BodySetup is unavailable")
    aggregate = property_or_none(body_setup, "agg_geom")
    require(aggregate is not None,
            "presentation StaticMesh aggregate collision is unavailable")
    for name in SIMPLE_COLLISION_ELEMENT_PROPERTIES:
        values = property_or_none(aggregate, name)
        require(values is not None,
                "presentation collision element array is unavailable: " + name)
        aggregate.set_editor_property(name, [])
    body_setup.set_editor_property("agg_geom", aggregate)
    require(simple_collision_count(mesh) == 0,
            "presentation mesh retained simple collision in memory")


def _external_content_is_closed(value):
    if not isinstance(value, dict) or set(value) != PRESENTATION_EXTERNAL_CONTENT_KEYS:
        return False
    acquisition = value.get("acquisition_receipt")
    if not isinstance(acquisition, dict) or set(acquisition) != {
        "provider", "receipt_schema_version", "receipt_digest",
        "receipt_file_sha256", "acquisition_manifest_sha256",
    }:
        return False
    if (
        value.get("schema_version") != PRESENTATION_EXTERNAL_PLACEMENT_SCHEMA
        or value.get("normalization_policy")
        != PRESENTATION_EXTERNAL_NORMALIZATION_POLICY
        or acquisition.get("provider") != "poly_haven"
        or acquisition.get("receipt_schema_version")
        != "simworld.vista.playable-home-poly-haven-receipt/v1"
    ):
        return False
    hashes = (
        value.get("placement_manifest_sha256"),
        value.get("placement_plan_sha256"),
        acquisition.get("receipt_digest"),
        acquisition.get("receipt_file_sha256"),
        acquisition.get("acquisition_manifest_sha256"),
    )
    if any(not isinstance(item, str) or SHA256.fullmatch(item) is None for item in hashes):
        return False
    for key in ("semantic_target_ids", "dressing_ids"):
        identities = value.get(key)
        if (
            not isinstance(identities, list)
            or any(not isinstance(item, str) or not item for item in identities)
            or identities != sorted(set(identities))
        ):
            return False
    sources = value.get("asset_sources")
    return isinstance(sources, list) and bool(sources)


def presentation_is_external(execution):
    bindings = execution.get("presentation_bindings")
    base.require(isinstance(bindings, list) and len(bindings) == 3,
                 "presentation execution needs exactly three bindings")
    flags = [isinstance(binding, dict) and "external_content" in binding
             for binding in bindings]
    base.require(not any(flags) or all(flags),
                 "presentation execution mixes v1 and external v2 bindings")
    return all(flags)


def presentation_import_receipt_schema(execution):
    return (
        PRESENTATION_IMPORT_RECEIPT_SCHEMA_V2
        if presentation_is_external(execution)
        else PRESENTATION_IMPORT_RECEIPT_SCHEMA
    )


def presentation_scene_receipt_schema(execution):
    return (
        PRESENTATION_SCENE_RECEIPT_SCHEMA_V2
        if presentation_is_external(execution)
        else PRESENTATION_SCENE_RECEIPT_SCHEMA
    )


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
        expected_keys = (
            PRESENTATION_BINDING_KEYS_V2
            if isinstance(binding, dict) and "external_content" in binding
            else PRESENTATION_BINDING_KEYS
        )
        base.require(isinstance(binding, dict) and set(binding) == expected_keys,
                     "presentation execution binding fields differ")
        if "external_content" in binding:
            base.require(_external_content_is_closed(binding["external_content"]),
                         "presentation external content fields or policy differ")
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
    presentation_is_external(execution)
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
