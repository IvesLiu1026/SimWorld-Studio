"""Import the three pinned r2 room bundles into a candidate UE namespace.

This is an additive third commandlet phase.  It runs only after the unchanged
r1 asset importer has produced a verified candidate namespace.
"""

import json
import os
import sys

import unreal


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import commandlet_common as base  # noqa: E402
from presentation_commandlet_common import (  # noqa: E402
    PRESENTATION_EXTERNAL_NANITE_POLICY,
    PRESENTATION_IMPORT_MARKER,
    PRESENTATION_IMPORT_RESULT_FILE,
    clear_simple_collision,
    derived_presentation_asset_path,
    load_presentation_execution,
    load_verified_receipt,
    property_or_none,
    presentation_import_receipt_schema,
    presentation_is_external,
    presentation_asset_name,
    require,
    sha256_file,
    simple_collision_count,
    write_exclusive_receipt,
)


def nanite_enabled(mesh):
    settings = property_or_none(mesh, "nanite_settings")
    require(settings is not None, "presentation Nanite settings are unavailable")
    enabled = property_or_none(settings, "enabled")
    require(isinstance(enabled, bool),
            "presentation Nanite enabled observation is unavailable")
    return enabled


def disable_external_nanite(mesh):
    # UE 5.7 exposes NaniteSettings as an editor property on StaticMesh;
    # EditorStaticMeshLibrary has no Nanite accessor in this engine build.
    settings = property_or_none(mesh, "nanite_settings")
    require(settings is not None, "presentation Nanite settings are unavailable")
    settings.set_editor_property("enabled", False)
    mesh.set_editor_property("nanite_settings", settings)
    require(nanite_enabled(mesh) is False,
            "external presentation mesh retained Nanite")


def texture2d_path(value):
    texture_class = getattr(unreal, "Texture2D", None)
    if texture_class is None or value is None or not isinstance(value, texture_class):
        return None
    path = str(value.get_path_name())
    return path if path else None


def material_texture_paths(material):
    textures = []
    library = getattr(unreal, "MaterialEditingLibrary", None)
    base_material = material
    getter = getattr(material, "get_base_material", None)
    if callable(getter):
        try:
            base_material = getter() or material
        except Exception:
            base_material = material
    used_getter = getattr(library, "get_used_textures", None) if library else None
    material_class = getattr(unreal, "Material", None)
    if material_class is not None and isinstance(base_material, material_class) and callable(used_getter):
        try:
            textures.extend(list(used_getter(base_material) or []))
        except Exception:
            pass
    parameter_names = []
    names_getter = getattr(library, "get_texture_parameter_names", None) if library else None
    if callable(names_getter):
        try:
            parameter_names = list(names_getter(material) or [])
        except Exception:
            parameter_names = []
    value_getter = getattr(material, "get_texture_parameter_value", None)
    for parameter_name in parameter_names:
        try:
            if callable(value_getter):
                textures.append(value_getter(parameter_name))
        except Exception:
            pass
    for value in property_or_none(material, "texture_parameter_values") or []:
        textures.append(property_or_none(value, "parameter_value"))
    return sorted(set(
        path for path in (texture2d_path(item) for item in textures) if path
    ))


def import_bundle(binding, namespace):
    is_external = "external_content" in binding
    source = base.canonical_path(binding["source_file"])
    require(os.path.isfile(source) and sha256_file(source) == binding["source_file_sha256"],
            "presentation source pin mismatch")
    expected_path = derived_presentation_asset_path(namespace, binding)
    name = presentation_asset_name(binding["target_asset_id"])
    destination = namespace + "/Presentation/Imports/" + name
    require(not unreal.EditorAssetLibrary.does_directory_exist(destination),
            "presentation import destination already exists")

    manager = unreal.InterchangeManager.get_interchange_manager_scripted()
    source_data = unreal.InterchangeManager.create_source_data(source)
    require(manager is not None and source_data is not None,
            "Interchange manager or source data unavailable")
    parameters = unreal.ImportAssetParameters()
    parameters.set_editor_property("is_automated", True)
    parameters.set_editor_property("follow_redirectors", False)
    parameters.set_editor_property("destination_name", name)
    parameters.set_editor_property("replace_existing", False)
    parameters.set_editor_property("force_show_dialog", False)
    imported_objects = list(
        manager.import_asset(destination, source_data, parameters) or []
    )
    require(imported_objects, "presentation Interchange import returned no objects")
    raw_paths = sorted(
        str(item.get_path_name()) for item in imported_objects if item is not None
    )
    meshes = [item for item in imported_objects if isinstance(item, unreal.StaticMesh)]
    require(len(meshes) == 1,
            "presentation GLB did not import as exactly one StaticMesh")
    mesh = meshes[0]
    raw_mesh_path = str(mesh.get_path_name())
    expected_package = expected_path.rsplit(".", 1)[0]
    if raw_mesh_path != expected_path:
        require(unreal.EditorAssetLibrary.rename_asset(raw_mesh_path, expected_package),
                "failed to move presentation mesh to its deterministic path")
    loaded = unreal.load_asset(expected_path)
    require(isinstance(loaded, unreal.StaticMesh),
            "deterministic presentation StaticMesh cannot be loaded")

    slots = list(property_or_none(loaded, "static_materials") or [])
    require(len(slots) == binding["material_count"],
            "presentation material slot count differs from its receipt")
    material_paths = []
    effective_texture_paths = []
    for slot in slots:
        material = property_or_none(slot, "material_interface")
        material_path = str(material.get_path_name()) if material else ""
        require(material_path and "DefaultMaterial" not in material_path and
                "BasicShapeMaterial" not in material_path,
                "presentation mesh uses a missing/default/basic material")
        material_paths.append(material_path)
        effective_texture_paths.extend(material_texture_paths(material))
    effective_texture_paths = sorted(set(effective_texture_paths))
    returned_texture_paths = sorted(set(
        path for path in (texture2d_path(item) for item in imported_objects) if path
    ))
    require(returned_texture_paths and effective_texture_paths and
            set(returned_texture_paths) & set(effective_texture_paths),
            "presentation PBR textures were not imported and used")

    clear_simple_collision(loaded)
    if is_external:
        # External room bundles can include glass/translucency.  No opaque-only
        # eligibility proof exists yet, so this import path always disables
        # Nanite and records the post-save observation instead of inferring it.
        disable_external_nanite(loaded)
    require(
        unreal.EditorAssetLibrary.save_loaded_asset(
            loaded, only_if_is_dirty=False
        ),
        "failed to save presentation StaticMesh",
    )
    require(simple_collision_count(loaded) == 0,
            "presentation mesh retained simple collision")
    if is_external:
        require(nanite_enabled(loaded) is False,
                "saved external presentation mesh enabled Nanite")
    returned_paths = sorted(
        str(item.get_path_name()) for item in imported_objects if item is not None
    )
    require(expected_path in returned_paths or unreal.load_asset(expected_path) is loaded,
            "presentation mesh path was not retained after import")
    result = {
        "artifact_id": binding["artifact_id"],
        "target_asset_id": binding["target_asset_id"],
        "room_id": binding["room_id"],
        "room_kind": binding["room_kind"],
        "source_file_sha256": binding["source_file_sha256"],
        "object_path": expected_path,
        "expected_world_transform_cm": binding["expected_world_transform_cm"],
        "root_transform_policy": binding["root_transform_policy"],
        "semantic_policy": binding["semantic_policy"],
        "collision_policy": binding["collision_policy"],
        "unreal_collision_profile": "NoCollision",
        "material_ids": binding["material_ids"],
        "source_hashes": binding["source_hashes"],
        "raw_returned_object_paths": raw_paths,
        "returned_object_paths": returned_paths,
        "inspection": {
            "class_path": str(loaded.get_class().get_path_name()),
            "material_paths": material_paths,
            "returned_texture2d_paths": returned_texture_paths,
            "material_texture2d_paths": effective_texture_paths,
            "simple_collision_shapes": simple_collision_count(loaded),
            "collision_profile_for_components": "NoCollision",
            "can_ever_affect_navigation": False,
        },
    }
    if is_external:
        result.update({
            "external_content": binding["external_content"],
            "nanite_policy": PRESENTATION_EXTERNAL_NANITE_POLICY,
        })
        result["inspection"]["nanite_enabled"] = nanite_enabled(loaded)
    return result


def run():
    execution, manifest_path, manifest_sha = load_presentation_execution(
        "import", __file__
    )
    base_import_sha = base.require_sha(
        os.environ.get(base.IMPORT_RECEIPT_SHA_ENV, ""), "base import receipt"
    )
    base_receipt, base_receipt_path = load_verified_receipt(
        execution["import_receipt"],
        base_import_sha,
        base.IMPORT_RECEIPT_SCHEMA,
        "imported_candidate",
        "base import receipt",
    )
    require(base_receipt.get("bindings", {}).get("execution_manifest_sha256") ==
            manifest_sha, "base import receipt execution binding differs")
    namespace = execution["composition_spec"]["content_namespace"]
    require(base_receipt.get("content_namespace") == namespace and
            unreal.EditorAssetLibrary.does_directory_exist(namespace),
            "base candidate namespace is missing")
    presentation_root = namespace + "/Presentation"
    is_external = presentation_is_external(execution)
    require(not unreal.EditorAssetLibrary.does_directory_exist(presentation_root),
            "presentation namespace already exists")

    imported = []
    status = "failed_clean_quarantined"
    error = None
    try:
        require(unreal.EditorAssetLibrary.make_directory(presentation_root),
                "failed to create presentation namespace")
        for binding in execution["presentation_bindings"]:
            imported.append(import_bundle(binding, namespace))
        require(unreal.EditorAssetLibrary.save_directory(
            presentation_root, only_if_is_dirty=False, recursive=True
        ), "failed to save presentation assets")
        status = "imported_candidate"
    except Exception as exc:
        error = {"type": type(exc).__name__, "message": str(exc)[:512]}
        status = "partial_import_quarantined" if imported else "failed_clean_quarantined"

    gates = {
        "base_import_verified": status == "imported_candidate",
        "exact_three_room_bundles": status == "imported_candidate" and len(imported) == 3,
        "one_mesh_per_bundle": status == "imported_candidate",
        "materials_and_textures_inspected": status == "imported_candidate",
        "no_collision_source_policy": status == "imported_candidate" and all(
            item["inspection"]["simple_collision_shapes"] == 0 and
            item["unreal_collision_profile"] == "NoCollision"
            for item in imported
        ),
        "quarantined": status != "imported_candidate",
        "runtime_play_proof": "pending",
    }
    if is_external:
        bindings_by_artifact = {
            item["artifact_id"]: item for item in execution["presentation_bindings"]
        }
        gates.update({
            "external_content_preserved": (
                status == "imported_candidate" and all(
                    item.get("external_content")
                    == bindings_by_artifact[item["artifact_id"]]["external_content"]
                    for item in imported
                )
            ),
            "external_nanite_disabled": (
                status == "imported_candidate" and all(
                    item.get("nanite_policy") == PRESENTATION_EXTERNAL_NANITE_POLICY
                    and item["inspection"].get("nanite_enabled") is False
                    for item in imported
                )
            ),
        })
    receipt = {
        "schema_version": presentation_import_receipt_schema(execution),
        "status": status,
        "error": error,
        "bindings": {
            "engine": str(unreal.SystemLibrary.get_engine_version()),
            "project": base.canonical_path(unreal.Paths.get_project_file_path()),
            "execution_manifest": manifest_path,
            "execution_manifest_sha256": manifest_sha,
            "base_import_receipt": base_receipt_path,
            "base_import_receipt_sha256": base_import_sha,
            "composition_spec_sha256": execution["composition_spec_sha256"],
        },
        "content_namespace": namespace,
        "presentation_content_root": presentation_root,
        "assets": imported,
        "gates": gates,
    }
    receipt_sha = write_exclusive_receipt(
        execution["presentation_import_receipt"], execution["attempt_root"], receipt
    )
    result = {
        "status": status,
        "receipt": execution["presentation_import_receipt"],
        "sha256": receipt_sha,
    }
    write_exclusive_receipt(
        os.path.join(execution["attempt_root"], PRESENTATION_IMPORT_RESULT_FILE),
        execution["attempt_root"],
        result,
    )
    marker = PRESENTATION_IMPORT_MARKER + json.dumps(result, sort_keys=True)
    unreal.log(marker)
    print(marker, flush=True)
    if status != "imported_candidate":
        raise RuntimeError("VISTA presentation import failed; candidate quarantined")


run()
