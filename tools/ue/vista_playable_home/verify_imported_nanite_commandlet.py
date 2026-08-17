"""Fresh-process, read-only verification of imported Nanite persistence.

This script is intentionally separate from the importing commandlet.  It must
run in a new ``UnrealEditor-Cmd`` process after the import process exits, so all
observations come from serialized packages rather than live objects retained by
the importer.  The only filesystem writes are two append-only evidence files
inside the pinned attempt root.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys

import unreal


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from commandlet_common import (  # noqa: E402
    IMPORT_RECEIPT_SCHEMA,
    canonical_path,
    require,
    require_sha,
    sha256_file,
    write_exclusive_receipt,
)


VERIFY_RECEIPT_SCHEMA = "simworld.vista.playable-home-nanite-verification/v1"
VERIFY_MARKER = "VISTA_PLAYABLE_HOME_NANITE_VERIFY_RESULT:"
IMPORT_RECEIPT_ENV = "VISTA_PLAYABLE_HOME_VERIFY_IMPORT_RECEIPT"
IMPORT_RECEIPT_SHA_ENV = "VISTA_PLAYABLE_HOME_VERIFY_IMPORT_RECEIPT_SHA256"
PROJECT_ENV = "VISTA_PLAYABLE_HOME_VERIFY_PROJECT"
ATTEMPT_ROOT_ENV = "VISTA_PLAYABLE_HOME_VERIFY_ATTEMPT_ROOT"
VERIFY_RECEIPT_ENV = "VISTA_PLAYABLE_HOME_VERIFY_RECEIPT"
VERIFY_RESULT_ENV = "VISTA_PLAYABLE_HOME_VERIFY_RESULT"
MAX_PARENT_DEPTH = 16
MAX_MESH_COUNT = 4096
SHARED_DEFAULT_MATERIAL = "/InterchangeAssets/gltf/M_Default.M_Default"
PRIVATE_DEFAULT_SOURCE = SHARED_DEFAULT_MATERIAL
PRIVATE_OPAQUE_DS_SOURCE = (
    "/InterchangeAssets/gltf/MaterialInstances/"
    "MI_Default_Opaque_DS.MI_Default_Opaque_DS"
)


def property_or_none(value, name):
    try:
        return value.get_editor_property(name)
    except Exception:
        return None


def _strict_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _absolute_canonical_env(name):
    raw = os.environ.get(name, "")
    require(isinstance(raw, str) and raw and os.path.isabs(raw),
            name + " must be an absolute path")
    normalized = raw.replace("\\", "/")
    resolved = canonical_path(raw)
    require(normalized == resolved, name + " must already be canonical")
    return resolved


def _direct_attempt_child(path, attempt_root, label):
    require(os.path.dirname(path) == attempt_root,
            label + " must be a direct attempt-root child")
    require(path != attempt_root, label + " cannot be the attempt root")
    return path


def load_environment_contract():
    """Load a path-only, symlink-free verifier invocation contract."""

    attempt_root = _absolute_canonical_env(ATTEMPT_ROOT_ENV)
    require(os.path.isdir(attempt_root) and not os.path.islink(attempt_root),
            "verification attempt root is missing or is a symlink")
    project = _absolute_canonical_env(PROJECT_ENV)
    import_receipt = _absolute_canonical_env(IMPORT_RECEIPT_ENV)
    verify_receipt = _absolute_canonical_env(VERIFY_RECEIPT_ENV)
    verify_result = _absolute_canonical_env(VERIFY_RESULT_ENV)
    _direct_attempt_child(import_receipt, attempt_root, "import receipt")
    _direct_attempt_child(verify_receipt, attempt_root, "verification receipt")
    _direct_attempt_child(verify_result, attempt_root, "verification result")
    require(
        project.startswith(attempt_root + "/") and
        os.path.dirname(project).startswith(attempt_root + "/"),
        "project escapes verification attempt root",
    )
    require(os.path.isfile(project) and not os.path.islink(project),
            "pinned project is missing or is a symlink")
    require(os.path.isfile(import_receipt) and not os.path.islink(import_receipt),
            "pinned import receipt is missing or is a symlink")
    require(not os.path.lexists(verify_receipt),
            "verification receipt already exists")
    require(not os.path.lexists(verify_result),
            "verification result already exists")
    require(len({import_receipt, verify_receipt, verify_result}) == 3,
            "receipt and result paths must be distinct")
    expected_import_sha = require_sha(
        os.environ.get(IMPORT_RECEIPT_SHA_ENV, ""), "import receipt")
    require(sha256_file(import_receipt) == expected_import_sha,
            "import receipt digest mismatch")
    return {
        "attempt_root": attempt_root,
        "project": project,
        "import_receipt": import_receipt,
        "import_receipt_sha256": expected_import_sha,
        "verify_receipt": verify_receipt,
        "verify_result": verify_result,
    }


def sanitize_asset_name(source_object_path):
    """Mirror the native bridge's deterministic source-object sanitization."""

    require(isinstance(source_object_path, str) and "." in source_object_path,
            "source material object path is invalid")
    source_name = source_object_path.rsplit(".", 1)[1]
    name = re.sub(r"[^A-Za-z0-9_]+", "_", source_name).strip("_")
    require(bool(name), "source material object name is empty")
    return name


def private_material_object_path(namespace, source_object_path):
    """Return the exact private path generated by the native editor bridge."""

    require(
        isinstance(namespace, str) and
        re.fullmatch(r"/Game/VISTA/PlayableHome/[A-Za-z0-9_]{1,128}", namespace),
        "revision namespace invalid",
    )
    digest = hashlib.sha256(source_object_path.encode("utf-8")).hexdigest()[:16]
    name = "VISTA_{}_{}".format(sanitize_asset_name(source_object_path), digest)
    return "{}/Internal/Materials/{}.{}".format(namespace, name, name)


def blend_mode_name(blend_mode):
    match = re.search(r"\b(BLEND_[A-Z0-9_]+)\b", str(blend_mode))
    require(match is not None, "material blend mode name is unavailable")
    return match.group(1)


def effective_material_blend_mode(material):
    base = material
    getter = getattr(material, "get_base_material", None)
    if callable(getter):
        try:
            base = getter() or material
        except Exception:
            base = material
    blend_mode = property_or_none(base, "blend_mode")
    if blend_mode is None:
        blend_mode = property_or_none(material, "blend_mode")
    require(blend_mode is not None, "material blend mode is unavailable")
    return blend_mode_name(blend_mode)


def trace_material_parent_chain(material, namespace):
    """Trace an instance chain without modifying or saving any UObject."""

    internal_root = namespace + "/Internal/Materials"
    seen = set()
    chain = []
    current = material
    for depth in range(MAX_PARENT_DEPTH + 1):
        require(current is not None, "material parent is missing")
        path = str(current.get_path_name())
        require(path and path not in seen, "material parent chain contains a cycle")
        seen.add(path)
        chain.append(path)
        if isinstance(current, unreal.Material):
            require(
                path == internal_root or path.startswith(internal_root + "/"),
                "ultimate material root is outside the private material namespace",
            )
            return chain, current
        require(isinstance(current, unreal.MaterialInstanceConstant),
                "material parent chain contains an unsupported interface")
        require(depth < MAX_PARENT_DEPTH,
                "material parent chain exceeds maximum depth")
        current = property_or_none(current, "parent")
    require(False, "material parent chain exceeds maximum depth")


def material_used_with_nanite(material):
    value = property_or_none(material, "used_with_nanite")
    require(isinstance(value, bool), "material used_with_nanite is unavailable")
    return value


def material_has_nanite_usage(material):
    usage_enum = getattr(unreal, "MaterialUsage", None)
    usage = getattr(usage_enum, "MATUSAGE_NANITE", None)
    checker = getattr(unreal.MaterialEditingLibrary, "has_material_usage", None)
    require(usage is not None and callable(checker),
            "Nanite material-usage inspection API is unavailable")
    value = checker(material, usage)
    require(isinstance(value, bool), "Nanite material usage result is invalid")
    return value


def _load_asset(object_path, expected_class, label):
    loader = getattr(unreal.EditorAssetLibrary, "load_asset", None)
    require(callable(loader), "EditorAssetLibrary.load_asset is unavailable")
    loaded = loader(object_path)
    require(isinstance(loaded, expected_class), label + " class mismatch")
    require(str(loaded.get_path_name()) == object_path, label + " path mismatch")
    return loaded


def _load_import_receipt(path):
    try:
        with open(path, "r", encoding="utf-8") as source:
            receipt = json.load(source, object_pairs_hook=_strict_json_object)
    except (OSError, UnicodeError, ValueError):
        require(False, "import receipt is not strict valid JSON")
    require(isinstance(receipt, dict), "import receipt root is not an object")
    require(receipt.get("schema_version") == IMPORT_RECEIPT_SCHEMA,
            "import receipt schema mismatch")
    require(receipt.get("status") == "imported_candidate",
            "import receipt is not an imported candidate")
    return receipt


def _validate_mesh_contract(item, namespace):
    require(isinstance(item, dict) and item.get("source_kind") != "builtin",
            "non-builtin asset contract is invalid")
    object_path = item.get("object_path")
    require(
        isinstance(object_path, str) and
        object_path.startswith(namespace + "/Assets/") and
        object_path.count(".") == 1,
        "imported StaticMesh object path is invalid",
    )
    inspection = item.get("inspection")
    require(isinstance(inspection, dict) and
            inspection.get("object_path") == object_path,
            "imported mesh inspection path mismatch")
    material_paths = inspection.get("material_paths")
    blend_modes = inspection.get("material_blend_modes")
    policy = inspection.get("nanite_policy")
    enabled = inspection.get("nanite_enabled")
    require(isinstance(material_paths, list) and bool(material_paths) and
            all(isinstance(path, str) and path for path in material_paths),
            "receipt material slot paths are invalid")
    require(isinstance(blend_modes, list) and len(blend_modes) == len(material_paths) and
            all(isinstance(mode, str) and
                re.fullmatch(r"BLEND_[A-Z0-9_]+", mode) for mode in blend_modes),
            "receipt material blend modes are invalid")
    require(policy in {"eligible_static_opaque", "disabled_nonopaque_material"},
            "receipt Nanite policy is invalid")
    require(isinstance(enabled, bool), "receipt Nanite state is invalid")
    require(enabled is (policy == "eligible_static_opaque"),
            "receipt Nanite policy and state disagree")
    return object_path, inspection


def verify_persisted_assets(import_receipt, project):
    """Return value-only observations for every freshly loaded imported mesh."""

    loaded_project = canonical_path(unreal.Paths.get_project_file_path())
    require(loaded_project == project, "loaded project identity mismatch")
    namespace = import_receipt.get("content_namespace")
    require(
        isinstance(namespace, str) and
        re.fullmatch(r"/Game/VISTA/PlayableHome/[A-Za-z0-9_]{1,128}", namespace),
        "import receipt namespace invalid",
    )
    assets = import_receipt.get("assets")
    require(isinstance(assets, list), "import receipt assets are not an array")
    require(all(isinstance(item, dict) and
                isinstance(item.get("source_kind"), str) for item in assets),
            "import receipt contains an invalid asset record")
    imported = [item for item in assets if item.get("source_kind") != "builtin"]
    require(0 < len(imported) <= MAX_MESH_COUNT,
            "import receipt non-builtin mesh count is invalid")

    expected_default = private_material_object_path(
        namespace, PRIVATE_DEFAULT_SOURCE)
    expected_opaque_ds = private_material_object_path(
        namespace, PRIVATE_OPAQUE_DS_SOURCE)
    private_default = _load_asset(
        expected_default, unreal.Material, "private M_Default")
    private_opaque_ds = _load_asset(
        expected_opaque_ds,
        unreal.MaterialInstanceConstant,
        "private MI_Default_Opaque_DS",
    )
    opaque_ds_chain, opaque_ds_root = trace_material_parent_chain(
        private_opaque_ds, namespace)
    require(str(opaque_ds_root.get_path_name()) == expected_default,
            "private MI_Default_Opaque_DS does not resolve to private M_Default")

    roots = {}
    meshes = []
    seen_mesh_paths = set()
    for item in sorted(imported, key=lambda entry: str(entry.get("object_path", ""))):
        object_path, inspection = _validate_mesh_contract(item, namespace)
        require(object_path not in seen_mesh_paths,
                "import receipt contains a duplicate mesh path")
        seen_mesh_paths.add(object_path)
        mesh = _load_asset(object_path, unreal.StaticMesh, "imported StaticMesh")
        settings = property_or_none(mesh, "nanite_settings")
        enabled = property_or_none(settings, "enabled") if settings is not None else None
        require(isinstance(enabled, bool), "persisted Nanite enabled state is unavailable")
        require(enabled == inspection["nanite_enabled"],
                "persisted Nanite enabled state differs from receipt")

        valid_data = None
        valid_data_getter = getattr(mesh, "has_valid_nanite_data", None)
        if callable(valid_data_getter):
            valid_data = valid_data_getter()
            require(isinstance(valid_data, bool), "persisted Nanite data result is invalid")
            if inspection["nanite_policy"] == "eligible_static_opaque":
                require(valid_data, "eligible mesh has no valid persisted Nanite data")

        slots = list(property_or_none(mesh, "static_materials") or [])
        materials = [property_or_none(slot, "material_interface") for slot in slots]
        require(materials and all(material is not None for material in materials),
                "persisted StaticMesh contains an unresolved material slot")
        slot_paths = [str(material.get_path_name()) for material in materials]
        require(slot_paths == inspection["material_paths"],
                "persisted material slot paths differ from receipt")
        blend_modes = [effective_material_blend_mode(material) for material in materials]
        require(blend_modes == inspection["material_blend_modes"],
                "persisted material blend modes differ from receipt")

        material_chains = []
        for material in materials:
            chain, root = trace_material_parent_chain(material, namespace)
            root_path = str(root.get_path_name())
            if root_path not in roots:
                roots[root_path] = {
                    "object_path": root_path,
                    "used_with_nanite": material_used_with_nanite(root),
                    "has_nanite_usage": material_has_nanite_usage(root),
                }
            if inspection["nanite_policy"] == "eligible_static_opaque":
                require(roots[root_path]["used_with_nanite"],
                        "eligible mesh root is not marked used_with_nanite")
                require(roots[root_path]["has_nanite_usage"],
                        "eligible mesh root has no MATUSAGE_NANITE")
            material_chains.append({"slot_path": chain[0], "chain": chain,
                                    "root_path": root_path})

        meshes.append({
            "object_path": object_path,
            "nanite_policy": inspection["nanite_policy"],
            "nanite_enabled": enabled,
            "nanite_data_valid": valid_data,
            "material_paths": slot_paths,
            "material_blend_modes": blend_modes,
            "material_chains": material_chains,
        })

    exists = getattr(unreal.EditorAssetLibrary, "does_asset_exist", None)
    require(callable(exists), "EditorAssetLibrary.does_asset_exist is unavailable")
    shared_exists = exists(SHARED_DEFAULT_MATERIAL)
    require(isinstance(shared_exists, bool), "shared M_Default existence result is invalid")
    shared_observation = {
        "object_path": SHARED_DEFAULT_MATERIAL,
        "loadable": shared_exists,
    }
    if shared_exists:
        shared = unreal.EditorAssetLibrary.load_asset(SHARED_DEFAULT_MATERIAL)
        require(isinstance(shared, unreal.Material),
                "shared M_Default has an unexpected class")
        shared_used = material_used_with_nanite(shared)
        require(shared_used is False,
                "shared /InterchangeAssets M_Default was mutated for Nanite")
        shared_observation["used_with_nanite"] = shared_used

    return {
        "content_namespace": namespace,
        "meshes": meshes,
        "material_roots": [roots[path] for path in sorted(roots)],
        "required_private_assets": {
            "m_default": expected_default,
            "mi_default_opaque_ds": expected_opaque_ds,
            "mi_default_opaque_ds_chain": opaque_ds_chain,
        },
        "shared_default_material": shared_observation,
    }


def run():
    contract = load_environment_contract()
    status = "failed"
    error = None
    observations = None
    try:
        import_receipt = _load_import_receipt(contract["import_receipt"])
        observations = verify_persisted_assets(import_receipt, contract["project"])
        status = "verified"
    except Exception as exc:
        error = {"type": type(exc).__name__, "message": str(exc)[:512]}

    receipt = {
        "schema_version": VERIFY_RECEIPT_SCHEMA,
        "status": status,
        "error": error,
        "bindings": {
            "project": contract["project"],
            "project_sha256": sha256_file(contract["project"]),
            "import_receipt": contract["import_receipt"],
            "import_receipt_sha256": contract["import_receipt_sha256"],
        },
        "observations": observations,
        "gates": {
            "fresh_process_read_only": True,
            "project_identity_verified": status == "verified",
            "import_receipt_pinned": status == "verified",
            "all_nonbuiltin_meshes_reloaded": status == "verified",
            "nanite_policy_persisted": status == "verified",
            "material_slots_and_blend_modes_persisted": status == "verified",
            "private_material_chains_persisted": status == "verified",
            "nanite_material_usage_persisted": status == "verified",
            "required_sha_named_private_assets_persisted": status == "verified",
            "shared_default_material_unmodified": status == "verified",
            "quarantined": status != "verified",
        },
    }
    receipt_sha = write_exclusive_receipt(
        contract["verify_receipt"], contract["attempt_root"], receipt)
    result = {
        "status": status,
        "receipt": contract["verify_receipt"],
        "sha256": receipt_sha,
    }
    write_exclusive_receipt(
        contract["verify_result"], contract["attempt_root"], result)
    marker = VERIFY_MARKER + json.dumps(result, sort_keys=True)
    unreal.log(marker)
    print(marker, flush=True)
    if status != "verified":
        raise RuntimeError("VISTA Playable Home Nanite persistence verification failed")


run()
