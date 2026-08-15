"""Fixed UE commandlet for one fresh VISTA Playable Home revision import.

Run only through ``UnrealEditor-Cmd <project> -run=pythonscript`` with a pinned
execution manifest.  No caller object path, destination, class, or Python body
is accepted.
"""

import json
import os
import sys

import unreal


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from commandlet_common import (  # noqa: E402
    BUILTIN_URI_ALLOWLIST,
    IMPORT_MARKER,
    IMPORT_RECEIPT_SCHEMA,
    asset_name,
    canonical_path,
    derived_asset_path,
    load_build_plan,
    load_execution,
    require,
    sha256_file,
    write_exclusive_receipt,
)


def property_or_none(value, name):
    try:
        return value.get_editor_property(name)
    except Exception:
        return None


def simple_collision_count(mesh):
    body_setup = property_or_none(mesh, "body_setup")
    aggregate = property_or_none(body_setup, "agg_geom") if body_setup else None
    total = 0
    for name in ("box_elems", "sphere_elems", "sphyl_elems", "convex_elems"):
        values = property_or_none(aggregate, name) if aggregate else None
        total += len(values) if values is not None else 0
    return total


def verify_runtime(execution):
    engine = str(unreal.SystemLibrary.get_engine_version())
    require(engine.startswith("5."), "Unreal Engine major version mismatch")
    project = canonical_path(unreal.Paths.get_project_file_path())
    require(project == canonical_path(execution["project_file"]), "loaded project identity mismatch")
    require(sha256_file(project) == execution["project_sha256"], "loaded project digest mismatch")
    namespace = execution["composition_spec"]["content_namespace"]
    require(namespace.startswith("/Game/VISTA/PlayableHome/") and ".." not in namespace,
            "revision namespace invalid")
    require(not unreal.EditorAssetLibrary.does_directory_exist(namespace),
            "revision namespace already exists")
    return engine, project, namespace


def asset_collision_policies(plan):
    result = {asset["asset_id"]: set() for asset in plan["assets"]}
    for room in plan["rooms"]:
        result[room["bundle"]["asset_id"]].add("world_static")
    for entity in plan["entities"]:
        result[entity["asset"]["asset_id"]].add(entity["collision_policy"])
    return result


def inspect_asset(asset, policies, imported, room_shell=False):
    record = {
        "object_path": str(asset.get_path_name()),
        "class_path": str(asset.get_class().get_path_name()),
        "collision_policies": sorted(policies),
        "material_paths": [],
        "simple_collision_shapes": None,
        "collision_generated": False,
        "collision_trace_flag": None,
        "room_shell": bool(room_shell),
    }
    if not isinstance(asset, unreal.StaticMesh):
        return record

    slots = list(property_or_none(asset, "static_materials") or [])
    for slot in slots:
        material = property_or_none(slot, "material_interface")
        record["material_paths"].append(str(material.get_path_name()) if material else None)
    if imported:
        require(record["material_paths"] and all(record["material_paths"]),
                "imported mesh has an empty material slot")
        require(all("DefaultMaterial" not in path and "BasicShapeMaterial" not in path
                    for path in record["material_paths"]),
                "imported mesh uses a default/basic material")

    blocking = bool(set(policies) - {"detail_no_collision", "trigger_only"})
    body_setup = property_or_none(asset, "body_setup")
    require(not room_shell or body_setup is not None,
            "room shell mesh is missing BodySetup")
    if room_shell:
        # A room GLB is one hollow floor/walls/ceiling mesh.  A single convex
        # hull fills the interior and traps both player and NPC capsules, so
        # collision must follow the authored triangles for this static shell.
        body_setup.set_editor_property(
            "collision_trace_flag",
            unreal.CollisionTraceFlag.CTF_USE_COMPLEX_AS_SIMPLE,
        )
        unreal.EditorAssetLibrary.save_loaded_asset(asset, only_if_is_dirty=False)
    record["collision_trace_flag"] = str(
        property_or_none(body_setup, "collision_trace_flag")) if body_setup else None
    collision_count = simple_collision_count(asset)
    if blocking and not room_shell and collision_count == 0:
        unreal.EditorStaticMeshLibrary.add_simple_collisions(
            asset, unreal.ScriptingCollisionShapeType.NDOP26)
        unreal.EditorAssetLibrary.save_loaded_asset(asset, only_if_is_dirty=False)
        collision_count = simple_collision_count(asset)
        record["collision_generated"] = True
    require(room_shell or not blocking or collision_count > 0,
            "blocking mesh has no simple collision")
    require(not room_shell or
            (isinstance(record["collision_trace_flag"], str) and
             "COMPLEX_AS_SIMPLE" in record["collision_trace_flag"].upper()),
            "room shell did not retain complex-as-simple collision")
    record["simple_collision_shapes"] = collision_count
    return record


def import_one(asset, binding, namespace, policies, room_shell=False):
    expected_path = derived_asset_path(namespace, asset)
    if asset["source_kind"] == "builtin":
        builtin = BUILTIN_URI_ALLOWLIST.get(asset["uri"])
        require(builtin is not None, "builtin URI is not allowlisted")
        if builtin["kind"] == "class":
            loaded = unreal.load_class(None, builtin["object_path"])
        else:
            loaded = unreal.load_asset(builtin["object_path"])
        require(loaded is not None, "allowlisted builtin asset unavailable: " + asset["uri"])
        return {
            "asset_id": asset["asset_id"],
            "source_kind": "builtin",
            "uri": asset["uri"],
            "source_digest": asset["source_digest"],
            "object_path": expected_path,
            "inspection": {
                "object_path": expected_path,
                "class_path": str(loaded.get_class().get_path_name()),
                "collision_policies": sorted(policies),
                "material_paths": [],
                "simple_collision_shapes": None,
                "collision_generated": False,
                "collision_trace_flag": None,
                "room_shell": bool(room_shell),
            },
        }

    source = canonical_path(binding["source_file"])
    require(os.path.isfile(source) and sha256_file(source) == binding["source_file_sha256"],
            "source artifact pin mismatch")
    name = asset_name(asset["asset_id"])
    destination = namespace + "/Assets/" + name
    require(not unreal.EditorAssetLibrary.does_directory_exist(destination),
            "asset destination already exists in fresh revision namespace")

    # AssetTools.import_asset_tasks synchronizes the Content Browser after a
    # successful Interchange import.  UE 5.7 Python commandlets intentionally
    # have no Slate application, so that editor-only side effect asserts after
    # the first asset.  Calling the public scripted Interchange manager avoids
    # all Content Browser/UI code while retaining the same automated pipeline.
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
    imported_objects = list(manager.import_asset(destination, source_data, parameters) or [])
    require(imported_objects, "Interchange import returned no objects")
    raw_imported_paths = sorted(str(obj.get_path_name()) for obj in imported_objects if obj is not None)
    imported_meshes = [obj for obj in imported_objects if isinstance(obj, unreal.StaticMesh)]
    require(len(imported_meshes) == 1,
            "each source asset must import as exactly one combined primary StaticMesh")

    # glTF Interchange pipelines may place the primary mesh below a generated
    # StaticMeshes subfolder.  Move only that one mesh to the contract-derived
    # object path; material/texture dependencies remain referenced in their
    # private per-asset folder.  No caller controls either path.
    mesh = imported_meshes[0]
    raw_mesh_path = str(mesh.get_path_name())
    expected_package_path = expected_path.rsplit(".", 1)[0]
    if raw_mesh_path != expected_path:
        require(unreal.EditorAssetLibrary.rename_asset(raw_mesh_path, expected_package_path),
                "failed to move primary mesh to deterministic derived object path")
    imported_paths = sorted(str(obj.get_path_name()) for obj in imported_objects if obj is not None)
    imported_mesh_paths = sorted(
        str(obj.get_path_name()) for obj in imported_objects
        if isinstance(obj, unreal.StaticMesh)
    )
    require(imported_mesh_paths == [expected_path],
            "primary StaticMesh path does not match deterministic contract")
    loaded = unreal.load_asset(expected_path)
    require(isinstance(loaded, unreal.StaticMesh),
            "derived imported StaticMesh cannot be loaded")
    return {
        "asset_id": asset["asset_id"],
        "source_kind": asset["source_kind"],
        "uri": asset["uri"],
        "source_digest": asset["source_digest"],
        "source_file_sha256": binding["source_file_sha256"],
        "object_path": expected_path,
        "raw_returned_object_paths": raw_imported_paths,
        "returned_object_paths": imported_paths,
        "inspection": inspect_asset(loaded, policies, True, room_shell=room_shell),
    }


def run():
    execution, manifest_path, manifest_sha = load_execution("import", __file__)
    plan = load_build_plan(execution)
    engine, project, namespace = verify_runtime(execution)
    bindings = {binding["asset_id"]: binding for binding in execution["artifact_bindings"]}
    require(set(bindings) == {asset["asset_id"] for asset in plan["assets"]},
            "artifact bindings are incomplete")
    policies = asset_collision_policies(plan)
    room_bundle_asset_ids = {room["bundle"]["asset_id"] for room in plan["rooms"]}
    imported = []
    status = "failed_clean_quarantined"
    error = None
    try:
        require(unreal.EditorAssetLibrary.make_directory(namespace),
                "failed to create fresh revision namespace")
        for asset in sorted(plan["assets"], key=lambda item: item["asset_id"]):
            imported.append(import_one(asset, bindings[asset["asset_id"]], namespace,
                                       policies[asset["asset_id"]],
                                       room_shell=asset["asset_id"] in room_bundle_asset_ids))
        require(unreal.EditorAssetLibrary.save_directory(namespace, only_if_is_dirty=False, recursive=True),
                "failed to save imported namespace")
        status = "imported_candidate"
    except Exception as exc:
        error = {"type": type(exc).__name__, "message": str(exc)[:512]}
        status = "partial_import_quarantined" if imported else "failed_clean_quarantined"

    receipt = {
        "schema_version": IMPORT_RECEIPT_SCHEMA,
        "status": status,
        "error": error,
        "bindings": {
            "engine": engine,
            "project": project,
            "execution_manifest": manifest_path,
            "execution_manifest_sha256": manifest_sha,
            "build_plan_sha256": execution["build_plan_sha256"],
            "composition_spec_sha256": execution["composition_spec_sha256"],
        },
        "content_namespace": namespace,
        "assets": imported,
        "gates": {
            "namespace_fresh": status == "imported_candidate",
            "all_assets_bound": status == "imported_candidate" and len(imported) == len(plan["assets"]),
            "material_and_collision_inspected": status == "imported_candidate",
            "quarantined": status != "imported_candidate",
        },
    }
    receipt_sha = write_exclusive_receipt(
        execution["import_receipt"], execution["attempt_root"], receipt)
    print(IMPORT_MARKER + json.dumps({"status": status, "receipt": execution["import_receipt"],
                                     "sha256": receipt_sha}, sort_keys=True))
    if status != "imported_candidate":
        raise RuntimeError("VISTA Playable Home import failed; fresh namespace quarantined")


run()
