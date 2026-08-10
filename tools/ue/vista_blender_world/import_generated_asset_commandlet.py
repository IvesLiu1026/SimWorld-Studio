"""UE 5.3.2 commandlet: import one pinned VISTA GLB/glTF bundle.

This file is executed only by ``UnrealEditor-Cmd -run=pythonscript``.  It does
not accept caller-authored Python, an alternate destination, replacement, or a
Studio socket transport.
"""

import hashlib
import json
import math
import os
import pathlib

import unreal


PLAN_SCHEMA = "simworld.vista.blender-ue-preparation-plan/v1"
RECEIPT_SCHEMA = "simworld.vista.blender-ue-import-receipt/v1"
IMPORT_CONTENT_ROOT = "/Game/VISTA/External/Procedural/MMG040OfficeR1"
MARKER = "VISTA_BLENDER_UE_IMPORT_RESULT:"
FORBIDDEN_COMPONENTS = {
    "archive",
    "archives",
    "archived",
    "canonical",
    "production",
    "release",
    "releases",
    "r8",
    "disposable-project-r8",
}


def canonical_json(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        while True:
            block = source.read(1024 * 1024)
            if not block:
                return digest.hexdigest()
            digest.update(block)


def canonical_path(value):
    return os.path.realpath(os.path.abspath(str(value))).replace("\\", "/")


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def require_sha(value, label):
    require(isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value), label + " digest invalid")


def require_safe_attempt_path(path, attempt_root, label):
    path = canonical_path(path)
    attempt_root = canonical_path(attempt_root)
    require(path == attempt_root or path.startswith(attempt_root + "/"), label + " escapes attempt root")
    require(not any(part.casefold() in FORBIDDEN_COMPONENTS for part in pathlib.PurePosixPath(path).parts), label + " uses a forbidden destination")
    return path


def load_plan():
    plan_path = os.environ.get("VISTA_BLENDER_UE_PLAN", "")
    expected_sha = os.environ.get("VISTA_BLENDER_UE_PLAN_SHA256", "")
    require(plan_path and expected_sha, "pinned plan path and digest environment are required")
    require_sha(expected_sha, "plan")
    plan_path = canonical_path(plan_path)
    require(os.path.isfile(plan_path), "pinned plan file missing")
    require(sha256_file(plan_path) == expected_sha, "plan digest mismatch")
    with open(plan_path, "r", encoding="utf-8") as source:
        plan = json.load(source)
    require(plan.get("schema") == PLAN_SCHEMA, "plan schema mismatch")
    require(plan.get("status") == "prepared_not_executed", "plan state mismatch")
    policy = plan.get("policy", {})
    require(policy.get("append_only") is True, "append-only policy missing")
    require(policy.get("studio_socket_fallback_allowed") is False, "Studio socket fallback must remain disabled")
    require(policy.get("r8_allowed") is False, "r8 must remain rejected")
    attempt_root = canonical_path(plan["destination"]["attempt_root"])
    require_safe_attempt_path(plan_path, attempt_root, "plan")
    return plan, plan_path, expected_sha


def verify_runtime(plan):
    engine = str(unreal.SystemLibrary.get_engine_version())
    require(engine.startswith("5.3.2-29314046"), "engine identity mismatch")
    project = canonical_path(unreal.Paths.get_project_file_path())
    expected_project = canonical_path(plan["destination"]["project_file"])
    attempt_root = canonical_path(plan["destination"]["attempt_root"])
    require_safe_attempt_path(project, attempt_root, "project")
    require(project == expected_project, "project identity mismatch")
    require(sha256_file(project) == plan["source"]["project_sha256"], "copied .uproject digest mismatch")
    script = canonical_path(__file__)
    script_contract = plan["unreal"]["scripts"]["import"]
    require(script == canonical_path(script_contract["path"]), "commandlet script identity mismatch")
    require(sha256_file(script) == script_contract["sha256"], "commandlet script digest mismatch")
    require(plan["unreal"]["route"] == "UnrealEditor-Cmd -run=pythonscript", "import route mismatch")
    require(plan["unreal"]["import_content_root"] == IMPORT_CONTENT_ROOT, "content destination mismatch")
    return engine, project, script


def property_or_none(value, name):
    try:
        return value.get_editor_property(name)
    except Exception:
        return None


def collision_counts(mesh):
    counts = {}
    body_setup = property_or_none(mesh, "body_setup")
    aggregate = property_or_none(body_setup, "agg_geom") if body_setup else None
    for name in (
        "box_elems",
        "sphere_elems",
        "sphyl_elems",
        "convex_elems",
        "tapered_capsule_elems",
        "level_set_elems",
        "skinned_level_set_elems",
    ):
        value = property_or_none(aggregate, name) if aggregate else None
        counts[name] = len(value) if value is not None else 0
    return counts


def material_slots(mesh):
    result = []
    for slot in list(property_or_none(mesh, "static_materials") or [])[:128]:
        material = property_or_none(slot, "material_interface")
        path = str(material.get_path_name()) if material else None
        result.append(
            {
                "slot_name": str(property_or_none(slot, "material_slot_name")),
                "material_path": path,
            }
        )
    return result


def inspect_import(destination):
    object_paths = sorted(
        {
            str(path)
            for path in unreal.EditorAssetLibrary.list_assets(
                destination,
                recursive=True,
                include_folder=False,
            )
        }
    )
    inventory = []
    static_meshes = []
    for object_path in object_paths:
        asset = unreal.load_asset(object_path)
        class_path = str(asset.get_class().get_path_name()) if asset else None
        inventory.append({"object_path": object_path, "class_path": class_path})
        if not isinstance(asset, unreal.StaticMesh):
            continue
        counts = collision_counts(asset)
        collision_generated = False
        if sum(counts.values()) == 0:
            unreal.EditorStaticMeshLibrary.add_simple_collisions(
                asset,
                unreal.ScriptingCollisionShapeType.NDOP26,
            )
            unreal.EditorAssetLibrary.save_loaded_asset(asset, only_if_is_dirty=False)
            counts = collision_counts(asset)
            collision_generated = True
        extent = asset.get_bounds().box_extent
        dimensions = [2.0 * extent.x, 2.0 * extent.y, 2.0 * extent.z]
        require(all(math.isfinite(value) and value > 0.0 for value in dimensions), "imported mesh has invalid bounds: " + object_path)
        slots = material_slots(asset)
        require(slots and all(slot["material_path"] for slot in slots), "imported mesh has an empty material slot: " + object_path)
        require(
            all("DefaultMaterial" not in slot["material_path"] and "BasicShapeMaterial" not in slot["material_path"] for slot in slots),
            "imported mesh uses a default/basic material: " + object_path,
        )
        require(sum(counts.values()) > 0, "imported mesh has no simple collision: " + object_path)
        static_meshes.append(
            {
                "name": str(asset.get_name()),
                "object_path": object_path,
                "bounds_dimensions_cm": [round(value, 4) for value in dimensions],
                "material_slots": slots,
                "simple_collision_shape_counts": counts,
                "collision_generated_by_commandlet": collision_generated,
            }
        )
    return {"objects": inventory, "static_meshes": static_meshes}


def verify_required_meshes(plan, inspection):
    observed_entries = {entry["name"]: entry for entry in inspection["static_meshes"]}
    require(len(observed_entries) == len(inspection["static_meshes"]), "imported StaticMesh names are not unique")
    observed = set(observed_entries)
    coverage = []
    for asset in plan["blender"]["required_assets"]:
        bindings = asset["mesh_bindings"]
        expected = {binding["ue_asset_name"] for binding in bindings}
        missing = sorted(expected - observed)
        require(not missing, "required generated meshes missing: " + ",".join(missing[:16]))
        for binding in bindings:
            observed_entries[binding["ue_asset_name"]]["source_mesh_name"] = binding["source_mesh_name"]
        prefixed = sorted(name for name in observed if name.startswith(asset["mesh_prefix"]))
        require(prefixed, "required generated mesh prefix missing: " + asset["mesh_prefix"])
        coverage.append(
            {
                "asset_id": asset["asset_id"],
                "mesh_prefix": asset["mesh_prefix"],
                "mesh_bindings": bindings,
                "expected_ue_asset_names": sorted(expected),
                "observed_ue_asset_names": prefixed,
            }
        )
    return coverage


def write_receipt(plan, receipt):
    attempt_root = canonical_path(plan["destination"]["attempt_root"])
    path = require_safe_attempt_path(plan["destination"]["import_receipt"], attempt_root, "import receipt")
    require(os.path.dirname(path) == attempt_root, "import receipt must be a direct attempt-root child")
    raw = canonical_json(receipt)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    return path, hashlib.sha256(raw).hexdigest()


plan = None
receipt = {
    "schema": RECEIPT_SCHEMA,
    "status": "started",
    "error": None,
    "bindings": {},
    "task": {},
    "inventory": {"objects": [], "static_meshes": []},
    "required_mesh_coverage": [],
    "gates": {
        "machine_import_inventory": "pending",
        "live_glb_commandlet_import": "pending",
        "rendered_review": "pending",
        "production_ready": False,
        "semantic_index_eligible": False,
    },
}

try:
    plan, plan_path, plan_sha256 = load_plan()
    engine, project, script = verify_runtime(plan)
    source = plan["blender"]["selected_import_source"]
    source_path = canonical_path(source["path"])
    require(os.path.isfile(source_path), "generated import source missing")
    require_sha(source["sha256"], "generated source")
    require(sha256_file(source_path) == source["sha256"], "generated source digest mismatch")
    require(os.path.getsize(source_path) == source["bytes"], "generated source size mismatch")
    require(pathlib.PurePosixPath(source_path).suffix.casefold() in {".glb", ".gltf"}, "generated import format rejected")
    manifest_path = canonical_path(plan["blender"]["path"])
    require(sha256_file(manifest_path) == plan["blender"]["sha256"], "Blender manifest digest mismatch")
    require(not unreal.EditorAssetLibrary.does_directory_exist(IMPORT_CONTENT_ROOT), "procedural import destination already exists")

    settings = plan["unreal"]["task_settings"]
    require(
        settings
        == {
            "automated": True,
            "async": False,
            "replace_existing": False,
            "replace_existing_settings": False,
            "save": True,
        },
        "AssetImportTask settings mismatch",
    )
    task = unreal.AssetImportTask()
    task.set_editor_property("automated", True)
    task.set_editor_property("async_", False)
    task.set_editor_property("replace_existing", False)
    task.set_editor_property("replace_existing_settings", False)
    task.set_editor_property("save", True)
    task.set_editor_property("filename", source_path)
    task.set_editor_property("destination_path", IMPORT_CONTENT_ROOT)

    receipt["bindings"] = {
        "engine": engine,
        "project": project,
        "project_sha256": plan["source"]["project_sha256"],
        "plan": plan_path,
        "plan_sha256": plan_sha256,
        "commandlet_script": script,
        "commandlet_script_sha256": sha256_file(script),
        "blender_manifest": manifest_path,
        "blender_manifest_sha256": plan["blender"]["sha256"],
        "import_source": source_path,
        "import_source_format": source["format"],
        "import_source_sha256": source["sha256"],
    }
    receipt["task"] = {
        "route": "UnrealEditor-Cmd -run=pythonscript",
        "destination": IMPORT_CONTENT_ROOT,
        "settings": settings,
        "attempted": True,
        "returned_objects": [],
        "imported_object_paths": [],
    }

    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
    receipt["task"]["returned_objects"] = sorted(
        {str(value.get_path_name()) for value in task.get_objects() if value}
    )
    receipt["task"]["imported_object_paths"] = sorted(
        {str(value) for value in task.get_editor_property("imported_object_paths")}
    )
    receipt["inventory"] = inspect_import(IMPORT_CONTENT_ROOT)
    require(receipt["task"]["returned_objects"], "Interchange returned no imported object")
    require(receipt["inventory"]["static_meshes"], "post-import static mesh inventory is empty")
    receipt["required_mesh_coverage"] = verify_required_meshes(plan, receipt["inventory"])
    receipt["status"] = "imported_inspected_candidate"
    receipt["gates"]["machine_import_inventory"] = "passed"
    if source["format"] == "glb":
        receipt["gates"]["live_glb_commandlet_import"] = "passed"
except Exception as error:
    receipt["error"] = str(error)[:512]
    if plan and unreal.EditorAssetLibrary.does_directory_exist(IMPORT_CONTENT_ROOT):
        receipt["status"] = "partial_import_quarantined"
    else:
        receipt["status"] = "failed_clean_quarantined"

receipt_path = None
receipt_sha256 = None
if plan:
    try:
        receipt_path, receipt_sha256 = write_receipt(plan, receipt)
    except Exception as receipt_error:
        receipt["error"] = (receipt.get("error") or "")[:256] + "; receipt publication failed: " + str(receipt_error)[:256]
        receipt["status"] = "partial_import_quarantined"

print(
    MARKER
    + json.dumps(
        {
            "status": receipt["status"],
            "receipt": receipt_path,
            "receipt_sha256": receipt_sha256,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
)
if receipt["status"] != "imported_inspected_candidate":
    raise RuntimeError("VISTA Blender Interchange import failed; fresh project quarantined")
