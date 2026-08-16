"""Fixed UE commandlet that composes and reload-verifies one home revision."""

import json
import math
import os
import sys

import unreal


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from commandlet_common import (  # noqa: E402
    IMPORT_RECEIPT_SCHEMA,
    IMPORT_RECEIPT_SHA_ENV,
    SCENE_MARKER,
    SCENE_RECEIPT_SCHEMA,
    SCENE_RESULT_FILE,
    canonical_path,
    load_build_plan,
    load_execution,
    require,
    require_sha,
    sha256_file,
    write_exclusive_receipt,
)


def vector(values):
    return unreal.Vector(x=values[0], y=values[1], z=values[2])


def rotation(values):
    # The HouseSpec contract stores right-handed XYZ Euler components.  Unreal
    # names rotations by their axis instead: roll=X, pitch=Y, yaw=Z.  Keep the
    # source axes intact when constructing an FRotator; in particular, the
    # common [0, 0, 90] doorway transform must become a 90 degree yaw rather
    # than tipping the door onto its side with a 90 degree roll.
    return unreal.Rotator(pitch=values[1], yaw=values[2], roll=values[0])


def transform(value):
    return unreal.Transform(
        location=vector(value["location_cm"]),
        rotation=rotation(value["rotation_deg"]),
        scale=vector(value["scale"]),
    )


def safe_label(semantic_id):
    return "VISTA_" + "".join(character if character.isalnum() else "_"
                               for character in semantic_id)[:180]


def load_import_receipt(execution):
    expected_sha = require_sha(os.environ.get(IMPORT_RECEIPT_SHA_ENV, ""), "import receipt")
    path = canonical_path(execution["import_receipt"])
    require(os.path.isfile(path) and sha256_file(path) == expected_sha,
            "import receipt pin mismatch")
    with open(path, "r", encoding="utf-8") as source:
        receipt = json.load(source)
    require(receipt.get("schema_version") == IMPORT_RECEIPT_SCHEMA and
            receipt.get("status") == "imported_candidate",
            "import did not reach candidate state")
    require(receipt.get("content_namespace") ==
            execution["composition_spec"]["content_namespace"],
            "import namespace mismatch")
    require(receipt.get("bindings", {}).get("execution_manifest_sha256") ==
            os.environ["VISTA_PLAYABLE_HOME_EXECUTION_SHA256"],
            "import receipt execution binding mismatch")
    return receipt, path, expected_sha


def verify_runtime(execution):
    engine = str(unreal.SystemLibrary.get_engine_version())
    require(engine.startswith("5."), "Unreal Engine major version mismatch")
    project = canonical_path(unreal.Paths.get_project_file_path())
    require(project == canonical_path(execution["project_file"]),
            "loaded project identity mismatch")
    require(sha256_file(project) == execution["project_sha256"],
            "loaded project digest mismatch")
    plugin_class = unreal.load_class(
        None, "/Script/VistaPlayableHome.VistaPlayableHomeGameMode")
    require(plugin_class is not None,
            "VistaPlayableHome compiled plugin is not loaded in this project")
    return engine, project


def set_tags(actor, tags):
    actor.set_editor_property("tags", [unreal.Name(tag) for tag in sorted(tags)])


def set_if_present(value, name, setting):
    try:
        value.set_editor_property(name, setting)
        return True
    except Exception:
        return False


def set_required(value, name, setting):
    require(set_if_present(value, name, setting),
            "required Unreal property is unavailable: " + name)


def static_mesh_component(actor):
    try:
        component = actor.get_editor_property("static_mesh_component")
        if component:
            return component
    except Exception:
        pass
    try:
        component = actor.get_editor_property("mesh")
        if isinstance(component, unreal.StaticMeshComponent):
            return component
    except Exception:
        pass
    components = actor.get_components_by_class(unreal.StaticMeshComponent)
    return components[0] if components else None


def light_component(actor):
    components = actor.get_components_by_class(unreal.LightComponentBase)
    return components[0] if components else None


def configure_r2_review_camera(camera, operation):
    require(operation["transform"]["rotation_deg"][0] == 0.0,
            "r2 look-at camera roll is not zero")
    component = camera.get_editor_property("camera_component")
    set_required(component, "field_of_view", operation["fov_deg"])
    set_required(component, "constrain_aspect_ratio", True)
    set_required(component, "aspect_ratio", 16.0 / 9.0)
    set_required(component, "post_process_blend_weight", 1.0)
    exposure = operation["exposure"]
    require(exposure.get("mode") == "pinned_physical_camera",
            "r2 review camera exposure is not pinned physical camera")
    settings = component.get_editor_property("post_process_settings")
    required_settings = {
        "override_auto_exposure_method": True,
        "auto_exposure_method": unreal.AutoExposureMethod.AEM_MANUAL,
        "override_auto_exposure_apply_physical_camera_exposure": True,
        "auto_exposure_apply_physical_camera_exposure": True,
        "override_camera_iso": True,
        "camera_iso": exposure["iso"],
        "override_camera_shutter_speed": True,
        "camera_shutter_speed": 1.0 / exposure["shutter_speed_s"],
        "override_depth_of_field_fstop": True,
        "depth_of_field_fstop": exposure["aperture_fstop"],
        "override_auto_exposure_bias": True,
        "auto_exposure_bias": exposure["exposure_compensation_ev"],
    }
    for name, setting in required_settings.items():
        set_required(settings, name, setting)
    set_required(component, "post_process_settings", settings)


def spawn_r2_lighting(actor_subsystem, operation):
    require(operation.get("profile") == "neutral_day" and
            operation.get("light_mobility") == "movable" and
            operation.get("runtime_observation_required") is True,
            "unsupported r2 lighting profile")
    rig_tag = "VistaLightingRig=" + operation["rig_id"]
    sun_spec = operation["sun"]
    sun = actor_subsystem.spawn_actor_from_class(
        unreal.DirectionalLight, unreal.Vector(0.0, 0.0, 500.0),
        rotation(sun_spec["rotation_deg"]), transient=False)
    sky = actor_subsystem.spawn_actor_from_class(
        unreal.SkyLight, unreal.Vector(0.0, 0.0, 400.0),
        unreal.Rotator(), transient=False)
    require(sun is not None and sky is not None, "failed to spawn r2 sun/sky")
    sun.set_actor_label("VISTA_R2_DirectionalSun")
    sky.set_actor_label("VISTA_R2_SkyLight")
    set_tags(sun, ["VistaRole=lighting", rig_tag, "VistaLightType=sun"])
    set_tags(sky, ["VistaRole=lighting", rig_tag, "VistaLightType=sky"])
    sun_component = light_component(sun)
    sky_component = light_component(sky)
    require(sun_component is not None and sky_component is not None,
            "failed to resolve r2 sun/sky components")
    sun_component.set_mobility(unreal.ComponentMobility.MOVABLE)
    sky_component.set_mobility(unreal.ComponentMobility.MOVABLE)
    set_required(sun_component, "intensity", sun_spec["illuminance_lux"])
    set_required(sun_component, "use_temperature", True)
    set_required(sun_component, "temperature", sun_spec["temperature_k"])
    set_required(sun_component, "cast_shadows", True)
    set_required(sky_component, "intensity_scale", operation["sky"]["sky_intensity"])
    created = [sun, sky]
    for light_spec in operation["practical_lights"]:
        actor_class = unreal.RectLight if light_spec["type"] == "rect" else unreal.SpotLight
        practical = actor_subsystem.spawn_actor_from_class(
            actor_class, vector(light_spec["location_cm"]),
            rotation(light_spec["rotation_deg"]), transient=False)
        require(practical is not None, "failed to place r2 practical light")
        practical.set_actor_label(safe_label(light_spec["light_id"]))
        set_tags(practical, list(light_spec["tags"]) + [rig_tag])
        component = light_component(practical)
        require(component is not None, "failed to resolve r2 practical light component")
        component.set_mobility(unreal.ComponentMobility.MOVABLE)
        set_required(component, "intensity", light_spec["intensity"])
        set_required(component, "use_temperature", True)
        set_required(component, "temperature", light_spec["temperature_k"])
        set_required(component, "cast_shadows", True)
        unit_name = "LUMENS" if light_spec["unit"] == "lumens" else "CANDELAS"
        set_required(component, "intensity_units", getattr(unreal.LightUnits, unit_name))
        created.append(practical)

    exposure = operation["gameplay_exposure"]
    post = actor_subsystem.spawn_actor_from_class(
        unreal.PostProcessVolume, unreal.Vector(), unreal.Rotator(), transient=False)
    require(post is not None, "failed to place r2 gameplay post process")
    post.set_actor_label("VISTA_R2_PostProcess")
    set_tags(post, ["VistaRole=post_process", rig_tag,
                    "VistaExposureProfile=bounded_histogram"])
    set_required(post, "unbound", True)
    set_required(post, "priority", 100.0)
    set_required(post, "blend_weight", 1.0)
    settings = post.get_editor_property("settings")
    required_exposure = {
        "override_auto_exposure_method": True,
        "auto_exposure_method": unreal.AutoExposureMethod.AEM_HISTOGRAM,
        "override_auto_exposure_min_brightness": True,
        "auto_exposure_min_brightness": exposure["min_ev100"],
        "override_auto_exposure_max_brightness": True,
        "auto_exposure_max_brightness": exposure["max_ev100"],
        "override_auto_exposure_speed_up": True,
        "auto_exposure_speed_up": exposure["speed_up"],
        "override_auto_exposure_speed_down": True,
        "auto_exposure_speed_down": exposure["speed_down"],
    }
    for name, setting in required_exposure.items():
        set_required(settings, name, setting)
    set_required(post, "settings", settings)
    created.append(post)
    return created


def spawn(actor_subsystem, actor_class, value_transform, label, tags):
    actor = actor_subsystem.spawn_actor_from_class(
        actor_class,
        vector(value_transform["location_cm"]),
        rotation(value_transform["rotation_deg"]),
        transient=False,
    )
    require(actor is not None, "failed to spawn " + label)
    actor.set_actor_scale3d(vector(value_transform["scale"]))
    actor.set_actor_label(label)
    set_tags(actor, tags)
    return actor


def asset_objects(import_receipt):
    result = {}
    for entry in import_receipt["assets"]:
        require(entry["asset_id"] not in result, "duplicate asset in import receipt")
        object_path = entry["object_path"]
        loaded = unreal.load_class(None, object_path) if object_path.startswith("/Script/") \
            else unreal.load_asset(object_path)
        require(loaded is not None, "receipt asset unavailable: " + object_path)
        result[entry["asset_id"]] = {"object": loaded, "object_path": object_path}
    return result


def apply_entity_properties(actor, operation, asset_entry):
    semantic_id = operation["semantic_id"]
    set_if_present(actor, "semantic_id", semantic_id)
    set_if_present(actor, "world_revision", unreal.Name(operation.get("world_revision", "")))
    component = static_mesh_component(actor)
    asset = asset_entry["object"]
    if component and isinstance(asset, unreal.StaticMesh):
        component.set_static_mesh(asset)
        collision = operation["collision"]
        component.set_collision_profile_name(unreal.Name(collision["profile"]))
        # UE 5.7 no longer exposes UPrimitiveComponent::SetGenerateOverlapEvents
        # as a Python method.  The reflected property remains writable and is
        # the commandlet-safe API (it does not require an editor UI).
        component.set_editor_property(
            "generate_overlap_events", bool(collision["generate_overlap"])
        )
        component.set_simulate_physics(collision["simulate_physics"])
        mobility = operation["mobility"]
        component.set_mobility(
            unreal.ComponentMobility.STATIC if mobility == "static"
            else unreal.ComponentMobility.MOVABLE)
        set_if_present(component, "can_ever_affect_navigation",
                       bool(operation["nav_obstacle"]))
    baseline = operation["baseline_state"]
    set_if_present(
        actor,
        "initial_state_values",
        {unreal.Name(key): str(value).lower() if isinstance(value, bool) else str(value)
         for key, value in baseline.items()},
    )
    if operation["component_role"] in {"door", "container"}:
        set_if_present(actor, "initially_open", bool(baseline.get("open", False)))
    if operation["component_role"] == "appliance":
        set_if_present(actor, "initially_on",
                       bool(baseline.get("active", baseline.get("powered", False))))
        set_if_present(actor, "appliance_kind", unreal.Name(operation["category"]))
    if operation["component_role"] == "pickup":
        set_if_present(actor, "portable", bool(baseline.get("portable", True)))
    if operation["component_role"] == "npc":
        set_if_present(actor, "semantic_id", semantic_id)
        profile = operation["npc_profile"]
        require(set_if_present(actor, "patrol_target_semantic_ids",
                               list(profile["patrol_target_semantic_ids"])),
                "NPC patrol targets property is unavailable")
        require(set_if_present(actor, "patrol_action_timeout_seconds",
                               float(profile["action_timeout_s"])),
                "NPC patrol timeout property is unavailable")
        require(set_if_present(actor, "auto_start_patrol", True),
                "NPC auto-patrol property is unavailable")
    try:
        enum_values = [getattr(unreal.VistaAffordance, name.upper())
                       for name in operation["affordances"]]
        set_if_present(actor, "allowed_affordances", enum_values)
    except Exception as exc:
        require(not operation["affordances"],
                "failed to bind typed affordances: " + str(exc))


LEGACY_AXIS_MAPPINGS = {
    ("MoveForward", "W", 1.0), ("MoveForward", "S", -1.0),
    ("MoveRight", "D", 1.0), ("MoveRight", "A", -1.0),
    ("Turn", "MouseX", 1.0), ("LookUp", "MouseY", -1.0),
}
LEGACY_ACTION_MAPPINGS = {
    ("Jump", "SpaceBar"), ("Sprint", "LeftShift"),
    ("Crouch", "C"), ("Interact", "E"), ("Drop", "Q"),
}


def verify_legacy_input_mappings():
    settings = unreal.InputSettings.get_input_settings()
    existing_axes = {
        (
            str(item.get_editor_property("axis_name")),
            str(item.get_editor_property("key").get_editor_property("key_name")),
            float(item.get_editor_property("scale")),
        )
        for item in settings.get_editor_property("axis_mappings")
    }
    existing_actions = {
        (
            str(item.get_editor_property("action_name")),
            str(item.get_editor_property("key").get_editor_property("key_name")),
        )
        for item in settings.get_editor_property("action_mappings")
    }
    require(LEGACY_AXIS_MAPPINGS.issubset(existing_axes),
            "DefaultInput.ini is missing required axis mappings")
    require(LEGACY_ACTION_MAPPINGS.issubset(existing_actions),
            "DefaultInput.ini is missing required action mappings")


def event_definitions(plan, assets, room_anchor_ids):
    operation_types = {
        "spawn_fixture": unreal.VistaEventOperationType.SPAWN_FIXTURE,
        "set_transform": unreal.VistaEventOperationType.SET_TRANSFORM,
        "set_state": unreal.VistaEventOperationType.SET_STATE,
        "set_visibility": unreal.VistaEventOperationType.SET_VISIBILITY,
        "set_portable": unreal.VistaEventOperationType.SET_PORTABLE,
        "set_npc_queue": unreal.VistaEventOperationType.SET_NPC_QUEUE,
        "set_goal": unreal.VistaEventOperationType.SET_GOAL,
    }
    action_types = {
        "navigate_to": unreal.VistaNpcActionType.NAVIGATE_TO,
        "look_at": unreal.VistaNpcActionType.LOOK_AT,
        "pick_up": unreal.VistaNpcActionType.PICK_UP,
        "place": unreal.VistaNpcActionType.PLACE,
        "open_door": unreal.VistaNpcActionType.OPEN_DOOR,
        "close_door": unreal.VistaNpcActionType.CLOSE_DOOR,
        "sit": unreal.VistaNpcActionType.SIT,
        "wait": unreal.VistaNpcActionType.WAIT,
        "speak": unreal.VistaNpcActionType.SPEAK,
    }
    definitions = []
    for event_plan in plan["event_plans"]:
        definition = unreal.VistaEventDefinition()
        definition.set_editor_property("event_id", unreal.Name(event_plan["event_id"]))
        definition.set_editor_property("compatible_revision", unreal.Name(plan["house"]["revision"]))
        definition.set_editor_property("public_title", event_plan["title"])
        public_goals = sorted(event_plan["public_goals"], key=lambda goal: goal["goal_id"])
        definition.set_editor_property(
            "public_goal",
            " ".join(goal["description"] for goal in public_goals),
        )
        definition.set_editor_property("timeout_seconds", min(float(event_plan["timeout_s"]), 3600.0))
        operations = []
        for op_index, source_op in enumerate(event_plan["operations"]):
            operation = unreal.VistaEventOperation()
            operation.set_editor_property("operation_id", unreal.Name(source_op["op_id"]))
            operation.set_editor_property("type", operation_types[source_op["op"]])
            target_id = source_op.get("target_id", source_op.get("entity_id", ""))
            operation.set_editor_property("target_semantic_id", target_id)
            if "world_transform_cm" in source_op:
                operation.set_editor_property("transform", transform(source_op["world_transform_cm"]))
            if source_op["op"] == "set_state":
                operation.set_editor_property(
                    "state_values",
                    {unreal.Name(key): str(value).lower() if isinstance(value, bool) else str(value)
                     for key, value in source_op["state_patch"].items()},
                )
            elif source_op["op"] in {"set_visibility", "set_portable"}:
                operation.set_editor_property(
                    "boolean_value",
                    bool(source_op["visible"] if source_op["op"] == "set_visibility"
                         else source_op["portable"]),
                )
            elif source_op["op"] == "spawn_fixture":
                fixture = assets[source_op["asset"]["asset_id"]]["object"]
                require(isinstance(fixture, unreal.Class),
                        "spawn fixture asset must resolve to an allowlisted class")
                operation.set_editor_property("fixture_class", fixture)
            elif source_op["op"] == "set_npc_queue":
                npc_actions = []
                for action_index, source_action in enumerate(source_op["actions"]):
                    action = unreal.VistaNpcAction()
                    action.set_editor_property(
                        "action_id", unreal.Name("%s_%02d" % (source_op["op_id"], action_index)))
                    action.set_editor_property("type", action_types[source_action["action"]])
                    target = source_action.get("target_id", "")
                    if not target and source_action.get("room_id"):
                        target = room_anchor_ids[source_action["room_id"]]
                    action.set_editor_property("target_semantic_id", target)
                    action.set_editor_property("duration_seconds", float(source_action.get("duration_s", 0.0)))
                    action.set_editor_property("timeout_seconds", min(float(event_plan["timeout_s"]), 60.0))
                    action.set_editor_property("speech", source_action.get("utterance", ""))
                    npc_actions.append(action)
                operation.set_editor_property("npc_actions", npc_actions)
            operations.append(operation)
        definition.set_editor_property("initial_operations", operations)
        definitions.append(definition)
    return definitions


def actor_record(actor):
    return {
        "label": str(actor.get_actor_label()),
        "class_path": str(actor.get_class().get_path_name()),
        "path": str(actor.get_path_name()),
        "tags": sorted(str(tag) for tag in actor.get_editor_property("tags")),
    }


def run():
    execution, manifest_path, manifest_sha = load_execution("compose", __file__)
    plan = load_build_plan(execution)
    import_receipt, import_path, import_sha = load_import_receipt(execution)
    engine, project = verify_runtime(execution)
    input_config = canonical_path(os.path.join(
        os.path.dirname(project), "Config", "DefaultInput.ini"))
    require(os.path.isfile(input_config), "DefaultInput.ini is missing")
    input_config_sha = sha256_file(input_config)
    spec = execution["composition_spec"]
    is_r2 = "visual_profile_id" in spec
    map_path = spec["map_path"]
    require(not unreal.EditorAssetLibrary.does_asset_exist(map_path),
            "target map already exists")
    assets = asset_objects(import_receipt)
    actor_subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    level_subsystem = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    require(level_subsystem.new_level(map_path), "failed to create fresh target map")
    world = unreal.EditorLevelLibrary.get_editor_world()
    require(world is not None, "new map world unavailable")
    world_settings = world.get_world_settings()
    require(world_settings is not None, "new map world settings unavailable")
    world_settings.set_editor_property("force_no_precomputed_lighting", True)
    created = []
    status = "failed_unsaved_quarantined"
    error = None
    reload_verified = False
    dynamic_lighting_verified = False
    deterministic_exposure_verified = False
    input_mappings_verified = False
    stage = {"phase": "compose_operations", "operation_id": None, "kind": None}
    try:
        for operation in spec["operations"]:
            kind = operation["kind"]
            stage = {
                "phase": "compose_operation",
                "operation_id": operation["operation_id"],
                "kind": kind,
            }
            if kind == "place_room_bundle":
                actor = spawn(actor_subsystem, unreal.StaticMeshActor, operation["transform"],
                              safe_label(operation["semantic_id"]), operation["tags"])
                mesh = assets[operation["asset"]["asset_id"]]["object"]
                require(isinstance(mesh, unreal.StaticMesh), "room bundle is not a StaticMesh")
                component = static_mesh_component(actor)
                component.set_static_mesh(mesh)
                component.set_collision_profile_name(unreal.Name("BlockAll"))
                component.set_mobility(unreal.ComponentMobility.STATIC)
                created.append(actor)
            elif kind in {"place_room_anchor", "place_portal_anchor"}:
                if "transform" in operation:
                    value_transform = operation["transform"]
                else:
                    value_transform = {"location_cm": operation["location_cm"],
                                       "rotation_deg": [0.0, 0.0, 0.0], "scale": [1.0, 1.0, 1.0]}
                created.append(spawn(actor_subsystem, unreal.TargetPoint, value_transform,
                                     safe_label(operation["semantic_id"]), operation["tags"]))
            elif kind == "place_review_camera":
                camera = spawn(actor_subsystem, unreal.CameraActor, operation["transform"],
                               safe_label(operation["semantic_id"]), operation["tags"])
                if "review_shot_id" in operation:
                    configure_r2_review_camera(camera, operation)
                else:
                    camera.get_editor_property("camera_component").set_editor_property(
                        "field_of_view", operation["fov_deg"])
                created.append(camera)
            elif kind == "place_entity":
                actor_class = unreal.load_class(None, operation["actor_class"])
                require(actor_class is not None, "typed gameplay class unavailable")
                actor = spawn(actor_subsystem, actor_class, operation["transform"],
                              safe_label(operation["semantic_id"]), operation["tags"])
                operation["world_revision"] = plan["house"]["revision"]
                apply_entity_properties(actor, operation,
                                        assets[operation["asset"]["asset_id"]])
                created.append(actor)
            elif kind == "place_placement_anchor":
                created.append(spawn(actor_subsystem, unreal.TargetPoint, operation["transform"],
                                     safe_label(operation["semantic_id"]), operation["tags"]))
            elif kind == "place_player_start":
                created.append(spawn(actor_subsystem, unreal.PlayerStart, operation["transform"],
                                     "VISTA_PlayerStart", operation["tags"]))
            elif kind == "place_lighting":
                require(operation.get("profile") == "vista_playable_home_neutral_day_v2" and
                        operation.get("light_mobility") == "movable",
                        "unsupported lighting profile")
                exposure = operation.get("exposure", {})
                require(exposure.get("method") == "manual" and
                        exposure.get("apply_physical_camera_exposure") is False and
                        float(exposure.get("bias")) == -6.0,
                        "unsupported exposure profile")
                directional = actor_subsystem.spawn_actor_from_class(
                    unreal.DirectionalLight, unreal.Vector(0.0, 0.0, 500.0),
                    unreal.Rotator(pitch=-35.0, yaw=-45.0, roll=0.0), transient=False)
                skylight = actor_subsystem.spawn_actor_from_class(
                    unreal.SkyLight, unreal.Vector(0.0, 0.0, 400.0),
                    unreal.Rotator(), transient=False)
                directional.set_actor_label("VISTA_DirectionalLight")
                skylight.set_actor_label("VISTA_SkyLight")
                set_tags(directional, ["VistaRole=lighting"])
                set_tags(skylight, ["VistaRole=lighting"])
                directional_component = light_component(directional)
                skylight_component = light_component(skylight)
                require(directional_component is not None and skylight_component is not None,
                        "failed to resolve deterministic environment lights")
                directional_component.set_mobility(unreal.ComponentMobility.MOVABLE)
                skylight_component.set_mobility(unreal.ComponentMobility.MOVABLE)
                created.extend([directional, skylight])
                for light_spec in operation["indoor_lights"]:
                    point = actor_subsystem.spawn_actor_from_class(
                        unreal.PointLight, vector(light_spec["location_cm"]),
                        unreal.Rotator(), transient=False)
                    require(point is not None, "failed to place deterministic indoor light")
                    point.set_actor_label(safe_label(light_spec["semantic_id"]))
                    set_tags(point, list(light_spec["tags"]) + ["VistaRole=lighting"])
                    component = point.get_editor_property("point_light_component")
                    component.set_mobility(unreal.ComponentMobility.MOVABLE)
                    component.set_editor_property("intensity", 3200.0)
                    component.set_editor_property(
                        "attenuation_radius", light_spec["attenuation_radius_cm"])
                    component.set_editor_property("use_temperature", True)
                    component.set_editor_property("temperature", 4000.0)
                    component.set_editor_property("cast_shadows", True)
                    created.append(point)
                post = actor_subsystem.spawn_actor_from_class(
                    unreal.PostProcessVolume, unreal.Vector(), unreal.Rotator(), transient=False)
                require(post is not None, "failed to place deterministic post process")
                post.set_actor_label("VISTA_PostProcess")
                set_tags(post, ["VistaRole=post_process"])
                post.set_editor_property("unbound", True)
                post.set_editor_property("priority", 100.0)
                post.set_editor_property("blend_weight", 1.0)
                settings = post.get_editor_property("settings")
                settings.set_editor_property("override_auto_exposure_method", True)
                settings.set_editor_property(
                    "auto_exposure_method", unreal.AutoExposureMethod.AEM_MANUAL)
                settings.set_editor_property(
                    "override_auto_exposure_apply_physical_camera_exposure", True)
                settings.set_editor_property(
                    "auto_exposure_apply_physical_camera_exposure", False)
                settings.set_editor_property("override_auto_exposure_bias", True)
                settings.set_editor_property("auto_exposure_bias", exposure["bias"])
                post.set_editor_property("settings", settings)
                created.append(post)
            elif kind == "place_realistic_lighting":
                require(is_r2, "r2 lighting operation requires a visual profile")
                created.extend(spawn_r2_lighting(actor_subsystem, operation))
            elif kind == "configure_game_mode":
                game_mode_path = assets[operation["game_mode"]["asset_id"]]["object_path"]
                pawn_path = assets[operation["pawn"]["asset_id"]]["object_path"]
                require(game_mode_path == "/Script/VistaPlayableHome.VistaPlayableHomeGameMode" and
                        pawn_path == "/Script/VistaPlayableHome.VistaPlayableHomeCharacter",
                        "runtime classes are not the fixed playable-home classes")
                game_mode = unreal.load_class(None, game_mode_path)
                stage = {
                    "phase": "configure_game_mode_world_settings",
                    "operation_id": operation["operation_id"],
                    "kind": kind,
                }
                world.get_world_settings().set_editor_property("default_game_mode", game_mode)
                stage = {
                    "phase": "configure_game_mode_input",
                    "operation_id": operation["operation_id"],
                    "kind": kind,
                }
                verify_legacy_input_mappings()
                stage = {
                    "phase": "configure_game_mode_events",
                    "operation_id": operation["operation_id"],
                    "kind": kind,
                }
                definition_class = unreal.load_class(
                    None, "/Script/VistaPlayableHome.VistaEventDefinitionActor")
                definition_actor = actor_subsystem.spawn_actor_from_class(
                    definition_class, unreal.Vector(), unreal.Rotator(), transient=False)
                definition_actor.set_actor_label("VISTA_EventDefinitions")
                room_anchor_ids = {room["room_id"]: room["room_id"] + "/anchor.room_center"
                                   for room in plan["rooms"]}
                definition_actor.set_editor_property(
                    "definitions", event_definitions(plan, assets, room_anchor_ids))
                created.append(definition_actor)
            elif kind == "place_navmesh_bounds":
                bounds = operation["bounds"]
                center = [(low + high) / 2.0 for low, high in
                          zip(bounds["min_cm"], bounds["max_cm"])]
                desired_extent = [(high - low) / 2.0 for low, high in
                                  zip(bounds["min_cm"], bounds["max_cm"])]
                nav = actor_subsystem.spawn_actor_from_class(
                    unreal.NavMeshBoundsVolume, vector(center), unreal.Rotator(), transient=False)
                nav.set_actor_label("VISTA_NavMeshBounds")
                origin, current_extent = nav.get_actor_bounds(False)
                require(current_extent.x > 0 and current_extent.y > 0 and current_extent.z > 0,
                        "NavMesh volume default brush has invalid bounds")
                nav.set_actor_scale3d(unreal.Vector(
                    desired_extent[0] / current_extent.x,
                    desired_extent[1] / current_extent.y,
                    desired_extent[2] / current_extent.z,
                ))
                set_tags(nav, ["VistaRole=navmesh_bounds"])
                navigation_system = unreal.NavigationSystemV1.get_navigation_system(world)
                require(navigation_system is not None, "navigation system unavailable")
                navigation_system.on_navigation_bounds_updated(nav)
                created.append(nav)

        stage = {"phase": "save_map", "operation_id": None, "kind": None}
        require(unreal.EditorLoadingAndSavingUtils.save_map(world, map_path),
                "map save failed")
        status = "saved_candidate"
        stage = {"phase": "reload_map", "operation_id": None, "kind": None}
        require(level_subsystem.load_level(map_path), "saved map reload failed")
        reloaded = actor_subsystem.get_all_level_actors()
        observed_tags = {str(tag) for actor in reloaded
                         for tag in actor.get_editor_property("tags")}
        expected_tags = {spec["stable_tag_prefix"] + semantic_id
                         for operation in spec["operations"]
                         for semantic_id in ([operation["semantic_id"]]
                                             if "semantic_id" in operation else [])}
        require(expected_tags.issubset(observed_tags), "reloaded map lost semantic actors")
        require(any(isinstance(actor, unreal.PlayerStart) for actor in reloaded),
                "reloaded map lost PlayerStart")
        require(any(isinstance(actor, unreal.NavMeshBoundsVolume) for actor in reloaded),
                "reloaded map lost NavMesh bounds")
        reloaded_world = unreal.EditorLevelLibrary.get_editor_world()
        require(reloaded_world is not None and
                reloaded_world.get_world_settings().get_editor_property(
                    "force_no_precomputed_lighting"),
                "reloaded map lost dynamic-lighting world policy")
        vista_lights = [actor for actor in reloaded
                        if unreal.Name("VistaRole=lighting") in
                        actor.get_editor_property("tags")]
        lighting_operation = next(
            operation for operation in spec["operations"]
            if operation["kind"] in {"place_lighting", "place_realistic_lighting"})
        expected_light_count = 2 + len(
            lighting_operation["practical_lights"] if is_r2
            else lighting_operation["indoor_lights"])
        require(len(vista_lights) == expected_light_count,
                "reloaded map lost VISTA lights")
        require(all(light_component(actor) is not None and
                    light_component(actor).get_editor_property("mobility") ==
                    unreal.ComponentMobility.MOVABLE for actor in vista_lights),
                "reloaded map contains a non-movable VISTA light")
        post_volumes = [actor for actor in reloaded
                        if unreal.Name("VistaRole=post_process") in
                        actor.get_editor_property("tags")]
        require(len(post_volumes) == 1 and
                post_volumes[0].get_editor_property("unbound") and
                float(post_volumes[0].get_editor_property("blend_weight")) == 1.0 and
                float(post_volumes[0].get_editor_property("priority")) == 100.0,
                "reloaded map lost unbound VISTA post process")
        post_settings = post_volumes[0].get_editor_property("settings")
        if is_r2:
            exposure = lighting_operation["gameplay_exposure"]
            require(bool(post_settings.get_editor_property(
                        "override_auto_exposure_method")) and
                    post_settings.get_editor_property("auto_exposure_method") ==
                    unreal.AutoExposureMethod.AEM_HISTOGRAM and
                    float(post_settings.get_editor_property("auto_exposure_min_brightness")) ==
                    exposure["min_ev100"] and
                    float(post_settings.get_editor_property("auto_exposure_max_brightness")) ==
                    exposure["max_ev100"] and
                    float(post_settings.get_editor_property("auto_exposure_speed_up")) ==
                    exposure["speed_up"] and
                    float(post_settings.get_editor_property("auto_exposure_speed_down")) ==
                    exposure["speed_down"],
                    "reloaded map lost bounded histogram exposure")
            camera_operations = [operation for operation in spec["operations"]
                                 if operation["kind"] == "place_review_camera"]
            for camera_operation in camera_operations:
                semantic_tag = unreal.Name(
                    spec["stable_tag_prefix"] + camera_operation["semantic_id"])
                matches = [actor for actor in reloaded
                           if isinstance(actor, unreal.CameraActor) and semantic_tag in
                           actor.get_editor_property("tags")]
                require(len(matches) == 1, "reloaded r2 review camera set is not exact")
                actual_rotation = matches[0].get_actor_rotation()
                expected_rotation = camera_operation["transform"]["rotation_deg"]
                require(abs(float(actual_rotation.roll)) <= 0.01 and
                        abs((float(actual_rotation.pitch) - expected_rotation[1] + 180.0) % 360.0 - 180.0) <= 0.05 and
                        abs((float(actual_rotation.yaw) - expected_rotation[2] + 180.0) % 360.0 - 180.0) <= 0.05,
                        "reloaded r2 review camera lost zero-roll look-at rotation")
                component = matches[0].get_editor_property("camera_component")
                camera_settings = component.get_editor_property("post_process_settings")
                require(math.isclose(float(component.get_editor_property("field_of_view")),
                                     camera_operation["fov_deg"], abs_tol=0.05) and
                        bool(camera_settings.get_editor_property(
                            "auto_exposure_apply_physical_camera_exposure")),
                        "reloaded r2 review camera lost FOV or pinned physical exposure")
        else:
            require(bool(post_settings.get_editor_property(
                        "override_auto_exposure_method")) and
                    bool(post_settings.get_editor_property(
                        "override_auto_exposure_apply_physical_camera_exposure")) and
                    bool(post_settings.get_editor_property("override_auto_exposure_bias")) and
                    post_settings.get_editor_property("auto_exposure_method") ==
                    unreal.AutoExposureMethod.AEM_MANUAL and
                    not bool(post_settings.get_editor_property(
                        "auto_exposure_apply_physical_camera_exposure")) and
                    float(post_settings.get_editor_property("auto_exposure_bias")) == -6.0,
                    "reloaded map lost deterministic manual exposure")
        verify_legacy_input_mappings()
        dynamic_lighting_verified = True
        deterministic_exposure_verified = True
        input_mappings_verified = True
        reload_verified = True
        status = "saved_reloaded_candidate"
    except Exception as exc:
        error = {
            "type": type(exc).__name__,
            "message": str(exc)[:512],
            "stage": stage,
        }
        if status == "saved_candidate":
            status = "partial_saved_quarantined"
        else:
            status = "failed_unsaved_quarantined"

    actors = [actor_record(actor) for actor in actor_subsystem.get_all_level_actors()]
    receipt = {
        "schema_version": SCENE_RECEIPT_SCHEMA,
        "status": status,
        "error": error,
        "bindings": {
            "engine": engine,
            "project": project,
            "execution_manifest": manifest_path,
            "execution_manifest_sha256": manifest_sha,
            "import_receipt": import_path,
            "import_receipt_sha256": import_sha,
            "composition_spec_sha256": execution["composition_spec_sha256"],
            "input_config": input_config,
            "input_config_sha256": input_config_sha,
        },
        "content_namespace": spec["content_namespace"],
        "map_path": map_path,
        "actor_inventory": sorted(actors, key=lambda item: item["path"]),
        "gates": {
            "map_saved": status == "saved_reloaded_candidate",
            "map_reloaded": reload_verified,
            "semantic_tags_verified": reload_verified,
            "player_start_verified": reload_verified,
            "game_mode_configured": reload_verified,
            "navmesh_bounds_verified": reload_verified,
            "dynamic_lighting_verified": dynamic_lighting_verified,
            "deterministic_exposure_verified": deterministic_exposure_verified,
            "input_mappings_verified": input_mappings_verified,
            "quarantined": status != "saved_reloaded_candidate",
            "runtime_play_proof": "pending",
        },
    }
    receipt_sha = write_exclusive_receipt(
        execution["scene_receipt"], execution["attempt_root"], receipt)
    result = {
        "status": status,
        "receipt": execution["scene_receipt"],
        "sha256": receipt_sha,
    }
    write_exclusive_receipt(
        os.path.join(execution["attempt_root"], SCENE_RESULT_FILE),
        execution["attempt_root"],
        result,
    )
    marker = SCENE_MARKER + json.dumps(result, sort_keys=True)
    # Commandlets reliably retain engine log messages even when embedded
    # Python stdout is discarded during shutdown.
    unreal.log(marker)
    print(marker, flush=True)
    if status != "saved_reloaded_candidate":
        raise RuntimeError("VISTA Playable Home composition failed; fresh revision quarantined")


run()
