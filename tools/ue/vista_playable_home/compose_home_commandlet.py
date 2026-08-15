"""Fixed UE commandlet that composes and reload-verifies one home revision."""

import json
import os
import sys

import unreal


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from commandlet_common import (  # noqa: E402
    IMPORT_RECEIPT_SCHEMA,
    IMPORT_RECEIPT_SHA_ENV,
    SCENE_MARKER,
    SCENE_RECEIPT_SCHEMA,
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
    return unreal.Rotator(pitch=values[0], yaw=values[1], roll=values[2])


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
        component.set_generate_overlap_events(collision["generate_overlap"])
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


def add_legacy_input_mappings():
    settings = unreal.InputSettings.get_input_settings()
    axes = [
        ("MoveForward", "W", 1.0), ("MoveForward", "S", -1.0),
        ("MoveRight", "D", 1.0), ("MoveRight", "A", -1.0),
        ("Turn", "MouseX", 1.0), ("LookUp", "MouseY", -1.0),
    ]
    actions = [
        ("Jump", "SpaceBar"), ("Sprint", "LeftShift"),
        ("Crouch", "C"), ("Interact", "E"), ("Drop", "Q"),
    ]
    existing_axes = {(str(item.axis_name), str(item.key), float(item.scale))
                     for item in settings.get_editor_property("axis_mappings")}
    existing_actions = {(str(item.action_name), str(item.key))
                        for item in settings.get_editor_property("action_mappings")}
    for name, key, scale in axes:
        if (name, key, scale) not in existing_axes:
            mapping = unreal.InputAxisKeyMapping()
            mapping.set_editor_property("axis_name", unreal.Name(name))
            mapping.set_editor_property("key", unreal.Key(key))
            mapping.set_editor_property("scale", scale)
            settings.add_axis_mapping(mapping, False)
    for name, key in actions:
        if (name, key) not in existing_actions:
            mapping = unreal.InputActionKeyMapping()
            mapping.set_editor_property("action_name", unreal.Name(name))
            mapping.set_editor_property("key", unreal.Key(key))
            settings.add_action_mapping(mapping, False)
    settings.save_key_mappings()
    settings.save_config()


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
    spec = execution["composition_spec"]
    map_path = spec["map_path"]
    require(not unreal.EditorAssetLibrary.does_asset_exist(map_path),
            "target map already exists")
    assets = asset_objects(import_receipt)
    actor_subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    level_subsystem = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    require(level_subsystem.new_level(map_path), "failed to create fresh target map")
    world = unreal.EditorLevelLibrary.get_editor_world()
    require(world is not None, "new map world unavailable")
    created = []
    status = "failed_unsaved_quarantined"
    error = None
    reload_verified = False
    try:
        for operation in spec["operations"]:
            kind = operation["kind"]
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
                directional = actor_subsystem.spawn_actor_from_class(
                    unreal.DirectionalLight, unreal.Vector(0.0, 0.0, 500.0),
                    unreal.Rotator(-35.0, -45.0, 0.0), transient=False)
                skylight = actor_subsystem.spawn_actor_from_class(
                    unreal.SkyLight, unreal.Vector(0.0, 0.0, 400.0),
                    unreal.Rotator(), transient=False)
                directional.set_actor_label("VISTA_DirectionalLight")
                skylight.set_actor_label("VISTA_SkyLight")
                created.extend([directional, skylight])
                for light_spec in operation["indoor_lights"]:
                    point = actor_subsystem.spawn_actor_from_class(
                        unreal.PointLight, vector(light_spec["location_cm"]),
                        unreal.Rotator(), transient=False)
                    require(point is not None, "failed to place deterministic indoor light")
                    point.set_actor_label(safe_label(light_spec["semantic_id"]))
                    set_tags(point, light_spec["tags"])
                    component = point.get_editor_property("point_light_component")
                    component.set_editor_property("intensity", 3200.0)
                    component.set_editor_property(
                        "attenuation_radius", light_spec["attenuation_radius_cm"])
                    component.set_editor_property("use_temperature", True)
                    component.set_editor_property("temperature", 4000.0)
                    component.set_editor_property("cast_shadows", True)
                    created.append(point)
            elif kind == "configure_game_mode":
                game_mode_path = assets[operation["game_mode"]["asset_id"]]["object_path"]
                pawn_path = assets[operation["pawn"]["asset_id"]]["object_path"]
                require(game_mode_path == "/Script/VistaPlayableHome.VistaPlayableHomeGameMode" and
                        pawn_path == "/Script/VistaPlayableHome.VistaPlayableHomeCharacter",
                        "runtime classes are not the fixed playable-home classes")
                game_mode = unreal.load_class(None, game_mode_path)
                world.get_world_settings().set_editor_property("default_game_mode", game_mode)
                add_legacy_input_mappings()
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
                created.append(nav)

        unreal.NavigationSystemV1.build_navigation(world)
        require(unreal.EditorLoadingAndSavingUtils.save_map(world, map_path),
                "map save failed")
        status = "saved_candidate"
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
        reload_verified = True
        status = "saved_reloaded_candidate"
    except Exception as exc:
        error = {"type": type(exc).__name__, "message": str(exc)[:512]}
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
            "quarantined": status != "saved_reloaded_candidate",
            "runtime_play_proof": "pending",
        },
    }
    receipt_sha = write_exclusive_receipt(
        execution["scene_receipt"], execution["attempt_root"], receipt)
    print(SCENE_MARKER + json.dumps({"status": status, "receipt": execution["scene_receipt"],
                                    "sha256": receipt_sha}, sort_keys=True))
    if status != "saved_reloaded_candidate":
        raise RuntimeError("VISTA Playable Home composition failed; fresh revision quarantined")


run()
