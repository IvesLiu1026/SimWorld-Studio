"""UE 5.3.2 commandlet: compose MMG040_Office_BlenderR1 from pinned inputs."""

import hashlib
import json
import math
import os
import pathlib

import unreal


PLAN_SCHEMA = "simworld.vista.blender-ue-preparation-plan/v1"
IMPORT_RECEIPT_SCHEMA = "simworld.vista.blender-ue-import-receipt/v1"
SCENE_RECEIPT_SCHEMA = "simworld.vista.blender-ue-scene-receipt/v1"
IMPORT_CONTENT_ROOT = "/Game/VISTA/External/Procedural/MMG040OfficeR1"
SOURCE_MAP = "/Game/VISTA/Scenes/MMG040_Office_CommandletR3"
OUTPUT_MAP = "/Game/VISTA/Scenes/MMG040_Office_BlenderR1"
SOURCE_MAP_RELATIVE = "Content/VISTA/Scenes/MMG040_Office_CommandletR3.umap"
OUTPUT_MAP_RELATIVE = "Content/VISTA/Scenes/MMG040_Office_BlenderR1.umap"
MARKER = "VISTA_BLENDER_UE_SCENE_RESULT:"
SURROGATE_LABELS = {
    "VISTA_runtime_ground",
    "VISTA_back_wall",
    "VISTA_side_wall",
    "VISTA_high_shelf",
    "VISTA_office_chair_provisional",
}
PHYSICS_PROP_LABEL = "VISTA_high_cardboard_box"
PLAYER_START_LABEL = "VISTA_PlayerStart"
REFERENCE_CHARACTER_LABEL = "VISTA_camera_wearer_reference"
FIRST_PERSON_CAMERA_LABEL = "VISTA_FirstPersonCamera"
PAWN_CLASS = "/Game/Human_Avatar/DefaultCharacter/ThirdPerson/Blueprints/BP_ThirdPersonCharacter.BP_ThirdPersonCharacter_C"

# The generated room floor spans x=[40, 560], y=[-250, 250] centimetres after
# its approved world placement.  The source scene's PlayerStart at
# (0, -350, 100) is outside that footprint and would fall forever once the
# large basic-geometry surrogate ground is removed.  The third-person camera
# also needs clearance behind the pawn; placing it near a wall collapses the
# spring arm into the character mesh.  The first centre candidate overlapped
# the provisional source-scene chair and UE rejected it at runtime.  A fixed
# capsule diagnostic measured radius=35 cm / half-height=90 cm and proved the
# southwest candidate clear; its yaw keeps the spring arm inside the room.
PLAYER_START_TRANSFORM = {
    "location_cm": [150.0, -150.0, 100.0],
    "rotation_deg": [0.0, -75.0, 0.0],
}
FIRST_PERSON_CAMERA_TRANSFORM = {
    "location_cm": [100.0, -80.0, 170.0],
    "rotation_deg": [0.0, 0.0, 0.0],
}
REFERENCE_CHARACTER_TRANSFORM = {
    "location_cm": [220.0, 140.0, 95.0],
    "rotation_deg": [0.0, -30.0, 0.0],
}
PHYSICS_PROP_LOCATION_CM = [430.0, 20.0, 120.0]
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
    require(os.path.isfile(plan_path) and sha256_file(plan_path) == expected_sha, "plan pin mismatch")
    with open(plan_path, "r", encoding="utf-8") as source:
        plan = json.load(source)
    require(plan.get("schema") == PLAN_SCHEMA and plan.get("status") == "prepared_not_executed", "plan contract mismatch")
    policy = plan.get("policy", {})
    require(policy.get("append_only") is True and policy.get("studio_socket_fallback_allowed") is False, "plan safety policy mismatch")
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
    script_contract = plan["unreal"]["scripts"]["compose"]
    require(script == canonical_path(script_contract["path"]), "commandlet script identity mismatch")
    require(sha256_file(script) == script_contract["sha256"], "commandlet script digest mismatch")
    require(plan["unreal"]["import_content_root"] == IMPORT_CONTENT_ROOT, "import content path mismatch")
    require(plan["unreal"]["source_map"] == SOURCE_MAP and plan["unreal"]["output_map"] == OUTPUT_MAP, "map contract mismatch")
    return engine, project, script


def load_import_receipt(plan):
    expected_sha = os.environ.get("VISTA_BLENDER_UE_IMPORT_RECEIPT_SHA256", "")
    require_sha(expected_sha, "import receipt")
    attempt_root = canonical_path(plan["destination"]["attempt_root"])
    path = require_safe_attempt_path(plan["destination"]["import_receipt"], attempt_root, "import receipt")
    require(os.path.isfile(path) and sha256_file(path) == expected_sha, "import receipt pin mismatch")
    with open(path, "r", encoding="utf-8") as source:
        receipt = json.load(source)
    require(receipt.get("schema") == IMPORT_RECEIPT_SCHEMA, "import receipt schema mismatch")
    require(receipt.get("status") == "imported_inspected_candidate", "import did not reach accepted candidate state")
    require(receipt.get("bindings", {}).get("plan_sha256") == os.environ["VISTA_BLENDER_UE_PLAN_SHA256"], "import receipt plan binding mismatch")
    require(receipt.get("task", {}).get("destination") == IMPORT_CONTENT_ROOT, "import receipt destination mismatch")
    require(receipt.get("gates", {}).get("machine_import_inventory") == "passed", "import inventory gate is not passed")
    return receipt, path, expected_sha


def vector(values):
    return unreal.Vector(x=values[0], y=values[1], z=values[2])


def rotation(values):
    return unreal.Rotator(pitch=values[0], yaw=values[1], roll=values[2])


def actor_record(actor, actor_id, source_path):
    location = actor.get_actor_location()
    actor_rotation = actor.get_actor_rotation()
    scale = actor.get_actor_scale3d()
    return {
        "actor_id": actor_id,
        "label": str(actor.get_actor_label()),
        "actor_path": str(actor.get_path_name()),
        "class_path": str(actor.get_class().get_path_name()),
        "source_path": source_path,
        "tags": sorted(str(tag) for tag in list(actor.get_editor_property("tags"))),
        "transform": {
            "location_cm": [location.x, location.y, location.z],
            "rotation_deg": [actor_rotation.pitch, actor_rotation.yaw, actor_rotation.roll],
            "scale": [scale.x, scale.y, scale.z],
        },
    }


def all_actors_by_label(actor_subsystem):
    result = {}
    for actor in actor_subsystem.get_all_level_actors():
        label = str(actor.get_actor_label())
        result.setdefault(label, []).append(actor)
    return result


def exact_actor(by_label, label):
    matches = by_label.get(label, [])
    require(len(matches) == 1, "expected exactly one actor label: " + label)
    return matches[0]


def generated_identity(plan_sha256, asset_id, source_mesh_name, object_path, placement):
    identity = {
        "asset_id": asset_id,
        "object_path": object_path,
        "placement": placement,
        "plan_sha256": plan_sha256,
        "source_mesh_name": source_mesh_name,
    }
    fingerprint = "vsa-" + hashlib.sha256(canonical_json(identity)).hexdigest()[:24]
    operation = "vso-" + hashlib.sha256(
        canonical_json({"fingerprint": fingerprint, "operation": "spawn_generated_mesh"})
    ).hexdigest()[:24]
    return fingerprint, operation


def spawn_generated_meshes(plan, import_receipt, actor_subsystem, plan_sha256):
    mesh_entries = import_receipt["inventory"]["static_meshes"]
    by_name = {entry["name"]: entry for entry in mesh_entries}
    require(len(by_name) == len(mesh_entries), "imported static mesh names are not unique")
    records = []
    for asset in plan["blender"]["required_assets"]:
        placement = asset["ue_placement"]
        for binding in asset["mesh_bindings"]:
            source_mesh_name = binding["source_mesh_name"]
            ue_asset_name = binding["ue_asset_name"]
            entry = by_name.get(ue_asset_name)
            require(entry is not None, "imported mesh absent from receipt: " + ue_asset_name)
            require(entry.get("source_mesh_name") == source_mesh_name, "imported mesh source binding mismatch: " + ue_asset_name)
            object_path = entry["object_path"]
            require(object_path.startswith(IMPORT_CONTENT_ROOT + "/"), "imported mesh escaped procedural root")
            mesh = unreal.load_asset(object_path)
            require(isinstance(mesh, unreal.StaticMesh), "receipt object is not a StaticMesh: " + object_path)
            actor = actor_subsystem.spawn_actor_from_class(
                unreal.StaticMeshActor,
                vector(placement["location_cm"]),
                rotation(placement["rotation_deg"]),
                transient=False,
            )
            require(actor is not None, "generated mesh actor spawn failed: " + ue_asset_name)
            actor.set_actor_label("VISTA_Generated_" + ue_asset_name)
            actor.set_actor_scale3d(vector(placement["scale"]))
            component = actor.get_component_by_class(unreal.StaticMeshComponent)
            require(component is not None and component.set_static_mesh(mesh), "generated mesh assignment failed: " + ue_asset_name)
            component.set_collision_profile_name("BlockAll")
            component.set_collision_enabled(unreal.CollisionEnabled.QUERY_AND_PHYSICS)
            component.set_editor_property("generate_overlap_events", False)
            component.set_editor_property("mobility", unreal.ComponentMobility.STATIC)
            fingerprint, operation = generated_identity(
                plan_sha256,
                asset["asset_id"],
                source_mesh_name,
                object_path,
                placement,
            )
            actor.set_editor_property(
                "tags",
                [
                    unreal.Name("VISTA_FINGERPRINT=" + fingerprint),
                    unreal.Name("VISTA_OPERATION=" + operation),
                ],
            )
            records.append(actor_record(actor, asset["asset_id"] + ":" + source_mesh_name, object_path))
    return records


def tune_existing_lights(by_label):
    changes = []
    sun = exact_actor(by_label, "VISTA_KeySun")
    sun_component = sun.get_component_by_class(unreal.DirectionalLightComponent)
    require(sun_component is not None, "key sun component missing")
    sun_component.set_editor_property("intensity", 2.0)
    changes.append({"label": "VISTA_KeySun", "intensity": 2.0})

    sky = exact_actor(by_label, "VISTA_SkyLight")
    sky_component = sky.get_component_by_class(unreal.SkyLightComponent)
    require(sky_component is not None, "sky light component missing")
    sky_component.set_intensity(0.8)
    changes.append({"label": "VISTA_SkyLight", "intensity": 0.8})

    fill = exact_actor(by_label, "VISTA_SoftFill")
    fill_component = fill.get_component_by_class(unreal.RectLightComponent)
    require(fill_component is not None, "soft fill component missing")
    fill_component.set_editor_property("intensity", 3200.0)
    fill_component.set_editor_property("source_width", 360.0)
    fill_component.set_editor_property("source_height", 220.0)
    changes.append(
        {
            "label": "VISTA_SoftFill",
            "intensity": 3200.0,
            "source_width": 360.0,
            "source_height": 220.0,
        }
    )
    return changes


def spawn_rect_light(actor_subsystem, label, location, target, intensity, width, height):
    light = actor_subsystem.spawn_actor_from_class(
        unreal.RectLight,
        vector(location),
        unreal.MathLibrary.find_look_at_rotation(vector(location), vector(target)),
        transient=False,
    )
    require(light is not None, "rect light spawn failed: " + label)
    light.set_actor_label(label)
    component = light.get_component_by_class(unreal.RectLightComponent)
    require(component is not None, "rect light component missing: " + label)
    component.set_editor_property("intensity", intensity)
    component.set_editor_property("source_width", width)
    component.set_editor_property("source_height", height)
    return actor_record(light, label, "/Script/Engine.RectLight")


def configure_runtime_layout(by_label):
    configured = []
    for label, transform in (
        (PLAYER_START_LABEL, PLAYER_START_TRANSFORM),
        (FIRST_PERSON_CAMERA_LABEL, FIRST_PERSON_CAMERA_TRANSFORM),
        (REFERENCE_CHARACTER_LABEL, REFERENCE_CHARACTER_TRANSFORM),
    ):
        actor = exact_actor(by_label, label)
        actor.set_actor_location(vector(transform["location_cm"]), False, False)
        actor.set_actor_rotation(rotation(transform["rotation_deg"]), False)
        configured.append(actor_record(actor, label, None))
    return configured


def validate_player_start_clearance(by_label, actor_subsystem):
    player_start = exact_actor(by_label, PLAYER_START_LABEL)
    pawn_class = unreal.load_class(None, PAWN_CLASS)
    require(pawn_class is not None, "fixed VISTA pawn class is unavailable")
    pawn_default = unreal.get_default_object(pawn_class)
    capsule = pawn_default.get_component_by_class(unreal.CapsuleComponent)
    require(capsule is not None, "fixed VISTA pawn capsule is unavailable")
    radius = float(capsule.get_unscaled_capsule_radius())
    half_height = float(capsule.get_unscaled_capsule_half_height())
    require(radius > 0.0 and half_height >= radius, "fixed VISTA pawn capsule is invalid")
    location = player_start.get_actor_location()
    blockers = []
    for actor in actor_subsystem.get_all_level_actors():
        component = actor.get_component_by_class(unreal.StaticMeshComponent)
        if component is None or component.get_collision_enabled() == unreal.CollisionEnabled.NO_COLLISION:
            continue
        origin, extent = actor.get_actor_bounds(False)
        intersects = (
            origin.x + extent.x >= location.x - radius
            and origin.x - extent.x <= location.x + radius
            and origin.y + extent.y >= location.y - radius
            and origin.y - extent.y <= location.y + radius
            and origin.z + extent.z >= location.z - half_height
            and origin.z - extent.z <= location.z + half_height
        )
        if intersects:
            blockers.append(str(actor.get_actor_label()))
    blockers = sorted(set(blockers))
    require(not blockers, "PlayerStart capsule overlaps blocking mesh bounds: " + ", ".join(blockers))
    return {
        "status": "passed",
        "pawn_class": PAWN_CLASS,
        "location_cm": [location.x, location.y, location.z],
        "capsule_radius_cm": radius,
        "capsule_half_height_cm": half_height,
        "blocking_actors": blockers,
    }


def configure_physics_prop(by_label):
    actor = exact_actor(by_label, PHYSICS_PROP_LABEL)
    actor.set_actor_location(vector(PHYSICS_PROP_LOCATION_CM), False, False)
    component = actor.get_component_by_class(unreal.StaticMeshComponent)
    require(component is not None, "physics prop StaticMeshComponent missing")
    component.set_editor_property("mobility", unreal.ComponentMobility.MOVABLE)
    component.set_collision_profile_name("PhysicsActor")
    component.set_collision_enabled(unreal.CollisionEnabled.QUERY_AND_PHYSICS)
    component.set_editor_property("generate_overlap_events", True)
    component.set_simulate_physics(True)
    component.set_enable_gravity(True)
    # In an editor commandlet there is no active physics scene, so
    # is_simulating_physics() may remain false even though the serialized body
    # instance is configured correctly.  Record that distinction and perform
    # the live assertion after the rendered runtime starts.
    editor_world_simulating = bool(component.is_simulating_physics())
    return {
        "actor": actor_record(actor, "interactive_cardboard_box", str(component.get_editor_property("static_mesh").get_path_name())),
        "mobility": "MOVABLE",
        "collision_profile": "PhysicsActor",
        "simulate_physics": True,
        "gravity": True,
        "editor_world_simulating": editor_world_simulating,
        "live_runtime_observation": "pending",
    }


def enable_runtime_plugins(project_path):
    """Enable loopback runtime plugins only after both commandlet mutations.

    This commandlet was started with the original r7 descriptor where the
    plugins were disabled, so changing the disposable descriptor here cannot
    open a listener in this process.  The next isolated runtime launch owns the
    explicit loopback ports.
    """

    with open(project_path, "rb") as source:
        raw_before = source.read()
    descriptor = json.loads(raw_before.decode("utf-8"))
    plugins = descriptor.get("Plugins")
    require(isinstance(plugins, list), "project plugin list missing")
    observed = {}
    for entry in plugins:
        if isinstance(entry, dict) and entry.get("Name") in {"UnrealMCP", "PixelStreaming"}:
            name = entry["Name"]
            require(name not in observed, "duplicate runtime plugin descriptor: " + name)
            observed[name] = entry.get("Enabled")
            require(entry.get("Enabled") is False, "runtime plugin was not disabled during commandlets: " + name)
            entry["Enabled"] = True
    require(observed == {"UnrealMCP": False, "PixelStreaming": False}, "required runtime plugin descriptors missing")
    raw_after = canonical_json(descriptor)
    require(raw_after != raw_before, "runtime descriptor finalization made no change")
    temporary = project_path + ".vista-runtime.tmp"
    descriptor_fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor_fd, "wb", closefd=False) as handle:
            handle.write(raw_after)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor_fd)
    os.replace(temporary, project_path)
    directory_fd = os.open(os.path.dirname(project_path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    with open(project_path, "r", encoding="utf-8") as source:
        verified = json.load(source)
    verified_plugins = {
        entry.get("Name"): entry.get("Enabled")
        for entry in verified.get("Plugins", [])
        if isinstance(entry, dict) and entry.get("Name") in {"UnrealMCP", "PixelStreaming"}
    }
    require(verified_plugins == {"UnrealMCP": True, "PixelStreaming": True}, "runtime plugin finalization verification failed")
    return {
        "path": project_path,
        "sha256_before": hashlib.sha256(raw_before).hexdigest(),
        "sha256_after": hashlib.sha256(raw_after).hexdigest(),
        "plugins_before": observed,
        "plugins_after": verified_plugins,
        "activation_scope": "next_explicit_loopback_runtime_launch_only",
    }


def write_receipt(plan, receipt):
    attempt_root = canonical_path(plan["destination"]["attempt_root"])
    path = require_safe_attempt_path(plan["destination"]["scene_receipt"], attempt_root, "scene receipt")
    require(os.path.dirname(path) == attempt_root, "scene receipt must be a direct attempt-root child")
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
    "schema": SCENE_RECEIPT_SCHEMA,
    "status": "started",
    "error": None,
    "bindings": {},
    "source_map": {},
    "output_map": {},
    "removed_surrogates": [],
    "generated_actors": [],
    "lighting": {"tuned": [], "added": []},
    "runtime_layout": [],
    "player_start_clearance": None,
    "physics_prop": None,
    "runtime_project_descriptor": None,
    "actor_inventory": [],
    "gates": {
        "map_saved": "pending",
        "source_map_immutable": "pending",
        "generated_asset_coverage": "pending",
        "physics_prop_configured": "pending",
        "runtime_proof_tags": "pending",
        "player_start_clearance": "pending",
        "rendered_review": "pending",
        "runtime_input": "pending",
        "production_ready": False,
        "semantic_index_eligible": False,
    },
}

try:
    plan, plan_path, plan_sha256 = load_plan()
    engine, project, script = verify_runtime(plan)
    import_receipt, import_receipt_path, import_receipt_sha256 = load_import_receipt(plan)
    project_root = os.path.dirname(project)
    source_map_file = canonical_path(os.path.join(project_root, SOURCE_MAP_RELATIVE))
    output_map_file = canonical_path(os.path.join(project_root, OUTPUT_MAP_RELATIVE))
    require(os.path.isfile(source_map_file), "copied source map file missing")
    source_map_sha_before = sha256_file(source_map_file)
    require(source_map_sha_before == plan["source"]["source_map_sha256"], "copied source map digest mismatch")
    require(not os.path.exists(output_map_file), "output map file already exists")
    require(not unreal.EditorAssetLibrary.does_asset_exist(OUTPUT_MAP), "output map asset already exists")

    world = unreal.EditorLoadingAndSavingUtils.load_map(SOURCE_MAP)
    if world is None:
        world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
    require(world is not None and SOURCE_MAP in str(world.get_path_name()), "pinned source map failed to load")
    actor_subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    by_label = all_actors_by_label(actor_subsystem)
    for label in sorted(SURROGATE_LABELS):
        actor = exact_actor(by_label, label)
        require(actor_subsystem.destroy_actor(actor), "surrogate actor deletion failed: " + label)
        receipt["removed_surrogates"].append(label)

    receipt["generated_actors"] = spawn_generated_meshes(plan, import_receipt, actor_subsystem, plan_sha256)
    require(receipt["generated_actors"], "no generated actor was composed")
    by_label = all_actors_by_label(actor_subsystem)
    receipt["lighting"]["tuned"] = tune_existing_lights(by_label)
    receipt["lighting"]["added"] = [
        spawn_rect_light(
            actor_subsystem,
            "VISTA_OverheadKey",
            [300.0, -40.0, 285.0],
            [300.0, 0.0, 40.0],
            4200.0,
            520.0,
            300.0,
        ),
        spawn_rect_light(
            actor_subsystem,
            "VISTA_CabinetWash",
            [300.0, -220.0, 210.0],
            [514.0, 0.0, 125.0],
            2800.0,
            260.0,
            180.0,
        ),
    ]
    receipt["runtime_layout"] = configure_runtime_layout(by_label)
    receipt["physics_prop"] = configure_physics_prop(by_label)
    receipt["player_start_clearance"] = validate_player_start_clearance(by_label, actor_subsystem)

    receipt["bindings"] = {
        "engine": engine,
        "project": project,
        "project_sha256": plan["source"]["project_sha256"],
        "plan": plan_path,
        "plan_sha256": plan_sha256,
        "commandlet_script": script,
        "commandlet_script_sha256": sha256_file(script),
        "import_receipt": import_receipt_path,
        "import_receipt_sha256": import_receipt_sha256,
        "blender_manifest_sha256": plan["blender"]["sha256"],
        "generated_source_sha256": plan["blender"]["selected_import_source"]["sha256"],
    }
    receipt["source_map"] = {
        "asset_path": SOURCE_MAP,
        "file": source_map_file,
        "sha256_before": source_map_sha_before,
        "sha256_after": None,
    }
    saved = bool(unreal.EditorLoadingAndSavingUtils.save_map(world, OUTPUT_MAP))
    require(saved and unreal.EditorAssetLibrary.does_asset_exist(OUTPUT_MAP), "save_map did not publish output map")
    require(os.path.isfile(output_map_file), "saved output map file missing")
    source_map_sha_after = sha256_file(source_map_file)
    require(source_map_sha_after == source_map_sha_before, "source map bytes changed during composition")
    output_map_sha256 = sha256_file(output_map_file)
    receipt["source_map"]["sha256_after"] = source_map_sha_after
    receipt["output_map"] = {
        "asset_path": OUTPUT_MAP,
        "file": output_map_file,
        "bytes": os.path.getsize(output_map_file),
        "sha256": output_map_sha256,
        "save_succeeded": True,
    }
    inventory = []
    for actor in actor_subsystem.get_all_level_actors():
        location = actor.get_actor_location()
        actor_rotation = actor.get_actor_rotation()
        scale = actor.get_actor_scale3d()
        require(all(math.isfinite(value) for value in (location.x, location.y, location.z, actor_rotation.pitch, actor_rotation.yaw, actor_rotation.roll, scale.x, scale.y, scale.z)), "actor transform is non-finite")
        inventory.append(actor_record(actor, str(actor.get_actor_label()), None))
    receipt["actor_inventory"] = sorted(inventory, key=lambda entry: entry["label"])
    labels = {entry["label"] for entry in receipt["actor_inventory"]}
    require(not labels.intersection(SURROGATE_LABELS), "basic/shelf surrogate survived composition")
    require({PLAYER_START_LABEL, FIRST_PERSON_CAMERA_LABEL, REFERENCE_CHARACTER_LABEL}.issubset(labels), "runtime layout actor missing from final map")
    require(PHYSICS_PROP_LABEL in labels, "physics prop actor missing from final map")
    require(receipt["player_start_clearance"]["blocking_actors"] == [], "PlayerStart clearance receipt regressed")
    for generated in receipt["generated_actors"]:
        tags = generated.get("tags", [])
        require(len([tag for tag in tags if tag.startswith("VISTA_FINGERPRINT=vsa-")]) == 1, "generated actor fingerprint tag missing")
        require(len([tag for tag in tags if tag.startswith("VISTA_OPERATION=vso-")]) == 1, "generated actor operation tag missing")
    receipt["runtime_project_descriptor"] = enable_runtime_plugins(project)
    receipt["status"] = "saved_machine_candidate"
    receipt["gates"].update(
        {
            "map_saved": "passed",
            "source_map_immutable": "passed",
            "generated_asset_coverage": "passed",
            "physics_prop_configured": "passed",
            "runtime_proof_tags": "passed",
            "player_start_clearance": "passed",
        }
    )
except Exception as error:
    receipt["error"] = str(error)[:512]
    if unreal.EditorAssetLibrary.does_asset_exist(OUTPUT_MAP):
        receipt["status"] = "partial_saved_quarantined"
    else:
        receipt["status"] = "failed_unsaved_quarantined"

receipt_path = None
receipt_sha256 = None
if plan:
    try:
        receipt_path, receipt_sha256 = write_receipt(plan, receipt)
    except Exception as receipt_error:
        receipt["error"] = (receipt.get("error") or "")[:256] + "; receipt publication failed: " + str(receipt_error)[:256]
        receipt["status"] = "partial_saved_quarantined"

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
if receipt["status"] != "saved_machine_candidate":
    raise RuntimeError("VISTA Blender scene composition failed; fresh project quarantined")
