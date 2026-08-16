"""Fixed UE commandlet for one fresh VISTA Playable Home revision import.

Run only through ``UnrealEditor-Cmd <project> -run=pythonscript`` with a pinned
execution manifest.  No caller object path, destination, class, or Python body
is accepted.
"""

import json
import os
import re
import struct
import sys

import unreal


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from commandlet_common import (  # noqa: E402
    BUILTIN_URI_ALLOWLIST,
    IMPORT_MARKER,
    IMPORT_RECEIPT_SCHEMA,
    IMPORT_RESULT_FILE,
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


def _integer(value):
    return isinstance(value, int) and not isinstance(value, bool)


def load_glb_texture_graph(path):
    """Return the pinned GLB JSON graph and actual binary-chunk byte length.

    Interchange can successfully create a material even when it cannot decode
    the texture transport used by that material.  Read only the fixed GLB
    container metadata here so the receipt records whether the source exposes
    a core glTF ``texture.source`` backed by an embedded PNG/JPEG.  Binary
    payloads are skipped rather than loaded into the commandlet process.
    """

    size = os.path.getsize(path)
    with open(path, "rb") as source:
        header = source.read(12)
        require(len(header) == 12, "source GLB header is truncated")
        magic, version, declared_length = struct.unpack("<4sII", header)
        require(magic == b"glTF" and version == 2, "source artifact is not a GLB 2 container")
        require(declared_length == size and declared_length >= 20,
                "source GLB declared length differs from its pinned bytes")
        offset = 12
        json_payload = None
        binary_length = None
        while offset < declared_length:
            chunk_header = source.read(8)
            require(len(chunk_header) == 8, "source GLB chunk header is truncated")
            chunk_length, chunk_type = struct.unpack("<I4s", chunk_header)
            offset += 8
            require(chunk_length % 4 == 0 and offset + chunk_length <= declared_length,
                    "source GLB chunk bounds are invalid")
            if chunk_type == b"JSON":
                require(json_payload is None and chunk_length <= 16 * 1024 * 1024,
                        "source GLB JSON chunk is duplicated or too large")
                json_payload = source.read(chunk_length)
                require(len(json_payload) == chunk_length, "source GLB JSON chunk is truncated")
            else:
                if chunk_type == b"BIN\x00":
                    require(binary_length is None, "source GLB binary chunk is duplicated")
                    binary_length = chunk_length
                source.seek(chunk_length, os.SEEK_CUR)
            offset += chunk_length
        require(offset == declared_length and json_payload is not None,
                "source GLB has no complete JSON chunk")
    try:
        graph = json.loads(json_payload.rstrip(b" \t\r\n\x00").decode("utf-8", "strict"))
    except (UnicodeError, json.JSONDecodeError):
        require(False, "source GLB JSON is invalid")
    require(isinstance(graph, dict), "source GLB JSON root is not an object")
    return graph, binary_length


def declared_core_texture_count(path):
    """Count valid core texture sources backed by embedded PNG/JPEG bytes."""

    graph, binary_length = load_glb_texture_graph(path)
    textures = graph.get("textures", [])
    images = graph.get("images", [])
    views = graph.get("bufferViews", [])
    buffers = graph.get("buffers", [])
    require(isinstance(textures, list) and isinstance(images, list) and
            isinstance(views, list) and isinstance(buffers, list),
            "source GLB texture graph arrays are invalid")
    if not textures:
        return 0
    count = 0
    for texture in textures:
        require(isinstance(texture, dict), "source GLB texture record is invalid")
        if "source" not in texture:
            continue
        image_index = texture["source"]
        require(_integer(image_index) and 0 <= image_index < len(images),
                "source GLB core texture source index is invalid")
        image = images[image_index]
        require(isinstance(image, dict), "source GLB image record is invalid")
        if image.get("mimeType") not in {"image/png", "image/jpeg"}:
            continue
        require("uri" not in image,
                "source GLB core PNG/JPEG must use embedded bytes, not a URI")
        view_index = image.get("bufferView")
        require(_integer(view_index) and 0 <= view_index < len(views),
                "source GLB core PNG/JPEG is not embedded in a valid buffer view")
        view = views[view_index]
        require(isinstance(view, dict), "source GLB image buffer view is invalid")
        buffer_index = view.get("buffer", 0)
        byte_offset = view.get("byteOffset", 0)
        byte_length = view.get("byteLength")
        require(
            _integer(buffer_index) and buffer_index == 0 and
            _integer(byte_offset) and byte_offset >= 0 and
            _integer(byte_length) and byte_length > 0 and
            len(buffers) == 1 and isinstance(buffers[0], dict) and
            "uri" not in buffers[0] and
            _integer(buffers[0].get("byteLength")) and buffers[0]["byteLength"] > 0 and
            _integer(binary_length) and
            byte_offset + byte_length <= buffers[0]["byteLength"] <= binary_length,
            "source GLB core PNG/JPEG payload is outside its embedded binary buffer",
        )
        count += 1
    return count


def _texture2d_path(value):
    texture_class = getattr(unreal, "Texture2D", None)
    if texture_class is None or value is None or not isinstance(value, texture_class):
        return None
    path = str(value.get_path_name())
    return path if path else None


def _material_texture2d_paths(material):
    """Inspect real material references with UE reflection, including instances."""

    textures = []
    library = getattr(unreal, "MaterialEditingLibrary", None)
    base = material
    base_getter = getattr(material, "get_base_material", None)
    if callable(base_getter):
        try:
            base = base_getter() or material
        except Exception:
            base = material
    material_class = getattr(unreal, "Material", None)
    used_getter = getattr(library, "get_used_textures", None) if library is not None else None
    if material_class is not None and isinstance(base, material_class) and callable(used_getter):
        try:
            textures.extend(list(used_getter(base) or []))
        except Exception:
            pass

    # A MaterialInstanceConstant can override a texture used by its base
    # material.  Resolve the effective values through public reflected methods
    # (the same APIs exposed to UE Python), then retain the property fallback
    # for pipelines that return an instance subclass without the convenience
    # method.
    parameter_names = []
    names_getter = getattr(library, "get_texture_parameter_names", None) if library is not None else None
    if callable(names_getter):
        try:
            parameter_names = list(names_getter(material) or [])
        except Exception:
            parameter_names = []
    value_getter = getattr(material, "get_texture_parameter_value", None)
    instance_getter = (
        getattr(library, "get_material_instance_texture_parameter_value", None)
        if library is not None else None
    )
    instance_class = getattr(unreal, "MaterialInstanceConstant", None)
    for parameter_name in parameter_names:
        texture = None
        try:
            if callable(value_getter):
                texture = value_getter(parameter_name)
            elif (
                instance_class is not None and
                isinstance(material, instance_class) and
                callable(instance_getter)
            ):
                texture = instance_getter(material, parameter_name)
        except Exception:
            texture = None
        if texture is not None:
            textures.append(texture)
    for value in property_or_none(material, "texture_parameter_values") or []:
        texture = property_or_none(value, "parameter_value")
        if texture is not None:
            textures.append(texture)
    return sorted(set(path for path in (_texture2d_path(item) for item in textures) if path))


def simple_collision_count(mesh):
    body_setup = property_or_none(mesh, "body_setup")
    aggregate = property_or_none(body_setup, "agg_geom") if body_setup else None
    total = 0
    for name in ("box_elems", "sphere_elems", "sphyl_elems", "convex_elems"):
        values = property_or_none(aggregate, name) if aggregate else None
        total += len(values) if values is not None else 0
    return total


def nanite_enabled(mesh):
    settings = property_or_none(mesh, "nanite_settings")
    require(settings is not None, "StaticMesh Nanite settings are unavailable")
    enabled = property_or_none(settings, "enabled")
    require(isinstance(enabled, bool), "StaticMesh Nanite enabled state is unavailable")
    return enabled


def effective_material_blend_mode(material):
    """Return the reflected blend mode used by the effective base material."""

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
    return blend_mode


def blend_mode_name(blend_mode):
    text = str(blend_mode)
    match = re.search(r"\b(BLEND_[A-Z0-9_]+)\b", text)
    require(match is not None, "material blend mode name is unavailable")
    return match.group(1)


def enforce_nanite_material_policy(mesh, materials):
    """Exclude a whole mesh from Nanite when any slot is non-opaque.

    Interchange maps glTF transmission materials to translucent UE materials,
    including source records whose transmission factor is zero. The source GLB
    remains byte-pinned; this UE-side policy also preserves genuinely
    transmissive slots such as appliance glass.
    """

    modes = [effective_material_blend_mode(material) for material in materials]
    allowed = {unreal.BlendMode.BLEND_OPAQUE, unreal.BlendMode.BLEND_MASKED}
    nonopaque = any(mode not in allowed for mode in modes)
    if nonopaque and nanite_enabled(mesh):
        settings = property_or_none(mesh, "nanite_settings")
        settings.set_editor_property("enabled", False)
        mesh.set_editor_property("nanite_settings", settings)
        require(nanite_enabled(mesh) is False,
                "non-opaque StaticMesh retained Nanite")
        unreal.EditorAssetLibrary.save_loaded_asset(mesh, only_if_is_dirty=False)
    return {
        "material_blend_modes": [blend_mode_name(mode) for mode in modes],
        "nanite_policy": (
            "disabled_nonopaque_material" if nonopaque
            else "eligible_static_opaque"
        ),
        "nanite_enabled": nanite_enabled(mesh),
    }


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


def inspect_asset(asset, policies, imported, room_shell=False,
                  core_texture_count=0, returned_texture2d_paths=None):
    returned_texture2d_paths = sorted(set(returned_texture2d_paths or []))
    record = {
        "object_path": str(asset.get_path_name()),
        "class_path": str(asset.get_class().get_path_name()),
        "collision_policies": sorted(policies),
        "material_paths": [],
        "simple_collision_shapes": None,
        "collision_generated": False,
        "collision_trace_flag": None,
        "room_shell": bool(room_shell),
        "declared_core_texture_count": core_texture_count,
        "returned_texture2d_paths": returned_texture2d_paths,
        "material_texture2d_paths": [],
        "material_blend_modes": [],
        "nanite_policy": "not_applicable",
        "nanite_enabled": None,
    }
    if not isinstance(asset, unreal.StaticMesh):
        return record

    slots = list(property_or_none(asset, "static_materials") or [])
    materials = []
    for slot in slots:
        material = property_or_none(slot, "material_interface")
        record["material_paths"].append(str(material.get_path_name()) if material else None)
        if material is not None:
            materials.append(material)
            record["material_texture2d_paths"].extend(_material_texture2d_paths(material))
    record["material_texture2d_paths"] = sorted(set(record["material_texture2d_paths"]))
    if imported:
        require(record["material_paths"] and all(record["material_paths"]),
                "imported mesh has an empty material slot")
        require(all("DefaultMaterial" not in path and "BasicShapeMaterial" not in path
                    for path in record["material_paths"]),
                "imported mesh uses a default/basic material")
        if core_texture_count > 0:
            require(returned_texture2d_paths,
                    "source declares core PNG/JPEG textures but Interchange returned no Texture2D")
            require(record["material_texture2d_paths"],
                    "source declares core PNG/JPEG textures but the mesh material uses no Texture2D")
            require(set(returned_texture2d_paths) & set(record["material_texture2d_paths"]),
                    "source core PNG/JPEG did not bind an imported Texture2D to the mesh material")
    require(len(materials) == len(slots),
            "StaticMesh has an unresolved material slot")
    record.update(enforce_nanite_material_policy(asset, materials))

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
                "declared_core_texture_count": 0,
                "returned_texture2d_paths": [],
                "material_texture2d_paths": [],
                "material_blend_modes": [],
                "nanite_policy": "not_applicable",
                "nanite_enabled": None,
            },
        }

    source = canonical_path(binding["source_file"])
    require(os.path.isfile(source) and sha256_file(source) == binding["source_file_sha256"],
            "source artifact pin mismatch")
    core_texture_count = declared_core_texture_count(source)
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
    returned_texture2d_paths = sorted(set(
        path for path in (_texture2d_path(obj) for obj in imported_objects) if path
    ))
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
        "inspection": inspect_asset(
            loaded,
            policies,
            True,
            room_shell=room_shell,
            core_texture_count=core_texture_count,
            returned_texture2d_paths=returned_texture2d_paths,
        ),
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
            "core_textures_imported_and_used": status == "imported_candidate" and all(
                item["inspection"]["declared_core_texture_count"] == 0 or
                bool(set(item["inspection"]["returned_texture2d_paths"]) &
                     set(item["inspection"]["material_texture2d_paths"]))
                for item in imported
            ),
            "nanite_material_policy_verified": status == "imported_candidate" and all(
                item["inspection"]["nanite_policy"] != "disabled_nonopaque_material" or
                item["inspection"]["nanite_enabled"] is False
                for item in imported
            ),
            "quarantined": status != "imported_candidate",
        },
    }
    receipt_sha = write_exclusive_receipt(
        execution["import_receipt"], execution["attempt_root"], receipt)
    result = {
        "status": status,
        "receipt": execution["import_receipt"],
        "sha256": receipt_sha,
    }
    # The Unreal commandlet keeps Python stdout in a different sink from its
    # project log on some Linux builds.  Publish an fsync'd, O_EXCL handshake
    # inside this fresh attempt so the host never has to infer success from a
    # shutdown-time console line.
    write_exclusive_receipt(
        os.path.join(execution["attempt_root"], IMPORT_RESULT_FILE),
        execution["attempt_root"],
        result,
    )
    marker = IMPORT_MARKER + json.dumps(result, sort_keys=True)
    # Unreal's embedded Python stdout is not guaranteed to be copied to the
    # commandlet log before shutdown.  The engine logger is the authoritative
    # transport; flushed stdout remains useful for compatible hosts.
    unreal.log(marker)
    print(marker, flush=True)
    if status != "imported_candidate":
        raise RuntimeError("VISTA Playable Home import failed; fresh namespace quarantined")


run()
