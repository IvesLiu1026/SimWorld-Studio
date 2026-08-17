"""Layer r2 room presentation actors over the saved r1 candidate map."""

import json
import os
import sys

import unreal


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import commandlet_common as base  # noqa: E402
from presentation_commandlet_common import (  # noqa: E402
    BASE_SCENE_SHA_ENV,
    PRESENTATION_EXTERNAL_NANITE_POLICY,
    PRESENTATION_IMPORT_SHA_ENV,
    PRESENTATION_SCENE_MARKER,
    PRESENTATION_SCENE_RESULT_FILE,
    load_presentation_execution,
    load_verified_receipt,
    property_or_none,
    presentation_import_receipt_schema,
    presentation_is_external,
    presentation_scene_receipt_schema,
    reflected_affordance_name,
    require,
    simple_collision_count,
    write_exclusive_receipt,
)


PRESENTATION_SHADOW_POLICY_TAG = "VistaShadowPolicy=visible_no_shadow"
AUTHORITY_SHADOW_POLICY_TAG = "VistaShadowPolicy=hidden_nanite_authority"


def nanite_enabled(mesh):
    settings = property_or_none(mesh, "nanite_settings")
    require(settings is not None, "presentation Nanite settings are unavailable")
    enabled = property_or_none(settings, "enabled")
    require(isinstance(enabled, bool),
            "presentation Nanite enabled observation is unavailable")
    return enabled


def require_shadow_policy(component, *, cast_shadow, cast_hidden_shadow, label):
    observed_cast_shadow = property_or_none(component, "cast_shadow")
    observed_cast_hidden_shadow = property_or_none(
        component, "cast_hidden_shadow"
    )
    require(
        isinstance(observed_cast_shadow, bool)
        and isinstance(observed_cast_hidden_shadow, bool),
        label + " shadow properties are unavailable",
    )
    require(
        observed_cast_shadow is cast_shadow
        and observed_cast_hidden_shadow is cast_hidden_shadow,
        label + " shadow policy differs",
    )


def vector(values):
    return unreal.Vector(x=values[0], y=values[1], z=values[2])


def rotation(values):
    return unreal.Rotator(pitch=values[1], yaw=values[2], roll=values[0])


def static_mesh_component(actor):
    try:
        component = actor.get_editor_property("static_mesh_component")
        if component:
            return component
    except Exception:
        pass
    components = actor.get_components_by_class(unreal.StaticMeshComponent)
    return components[0] if components else None


def set_tags(actor, tags):
    actor.set_editor_property("tags", [unreal.Name(tag) for tag in sorted(tags)])


def safe_label(value):
    return "VISTA_R2_" + "".join(
        character if character.isalnum() else "_" for character in value
    )[:170]


def observed_transform(actor):
    location = actor.get_actor_location()
    actor_rotation = actor.get_actor_rotation()
    scale = actor.get_actor_scale3d()
    return {
        "location_cm": [float(location.x), float(location.y), float(location.z)],
        "rotation_deg": [
            float(actor_rotation.roll),
            float(actor_rotation.pitch),
            float(actor_rotation.yaw),
        ],
        "scale": [float(scale.x), float(scale.y), float(scale.z)],
    }


def transform_matches(actual, expected):
    location_ok = all(
        abs(actual_value - float(expected_value)) <= 0.05
        for actual_value, expected_value in zip(
            actual["location_cm"], expected["location_cm"]
        )
    )
    rotation_ok = all(
        abs((actual_value - float(expected_value) + 180.0) % 360.0 - 180.0)
        <= 0.05
        for actual_value, expected_value in zip(
            actual["rotation_deg"], expected["rotation_deg"]
        )
    )
    scale_ok = all(
        abs(actual_value - float(expected_value)) <= 0.0001
        for actual_value, expected_value in zip(actual["scale"], expected["scale"])
    )
    return location_ok and rotation_ok and scale_ok


def actor_hidden(actor):
    try:
        hidden = actor.get_editor_property("hidden")
    except Exception as exc:
        require(False, "Actor.hidden is unavailable: " + str(exc))
    require(isinstance(hidden, bool), "Actor.hidden is not boolean")
    return hidden


def actor_class_path(actor):
    actor_class = actor.get_class()
    require(actor_class is not None, "semantic target actor class is unavailable")
    value = str(actor_class.get_path_name())
    require(value.startswith("/Script/"),
            "semantic target actor class path is unavailable")
    return value


def semantic_id_property(actor):
    try:
        value = actor.get_editor_property("semantic_id")
    except Exception as exc:
        require(False, "semantic target identity property is unavailable: " + str(exc))
    require(isinstance(value, str) and value,
            "semantic target identity property is invalid")
    return value


def interaction_affordances(actor):
    try:
        values = actor.get_editor_property("allowed_affordances")
        result = []
        for value in values:
            name = reflected_affordance_name(value, unreal.VistaAffordance)
            require(name and name not in result,
                    "semantic target affordance inventory is invalid")
            result.append(name)
    except Exception as exc:
        require(False, "semantic target affordances are unavailable: " + str(exc))
    return sorted(result)


def render_component_observations(actor):
    components = actor.get_components_by_class(unreal.StaticMeshComponent)
    require(components, "semantic target has no StaticMeshComponent")
    result = []
    paths = set()
    for component in components:
        path = str(component.get_path_name())
        require(path and path not in paths,
                "semantic target render component identity is not exact")
        paths.add(path)
        try:
            visible = component.get_editor_property("visible")
            collision_enabled = (
                component.get_collision_enabled()
                != unreal.CollisionEnabled.NO_COLLISION
            )
            collision_profile = str(component.get_collision_profile_name())
        except Exception as exc:
            require(False,
                    "semantic target render/collision state is unavailable: "
                    + str(exc))
        require(isinstance(visible, bool) and collision_profile,
                "semantic target render/collision state is invalid")
        result.append({
            "component_path": path,
            "visible": visible,
            "collision_profile": collision_profile,
            "collision_enabled": collision_enabled,
        })
    return sorted(result, key=lambda item: item["component_path"])


def semantic_target_observation(actor, semantic_target_id):
    tags = actor.get_editor_property("tags")
    require(unreal.Name("VistaSemanticId=" + semantic_target_id) in tags,
            "semantic target actor lost its exact semantic tag")
    return {
        "semantic_target_id": semantic_target_id,
        "actor_path": str(actor.get_path_name()),
        "actor_class_path": actor_class_path(actor),
        "semantic_id_property": semantic_id_property(actor),
        "actor_hidden_in_game": actor_hidden(actor),
        "interaction_affordances": interaction_affordances(actor),
        "render_components": render_component_observations(actor),
    }


def hide_semantic_target_visuals(actor):
    actor.set_actor_hidden_in_game(True)
    components = actor.get_components_by_class(unreal.StaticMeshComponent)
    require(components, "semantic target has no visual render component")
    for component in components:
        component.set_visibility(False, True)


def attach_keep_world(child, parent):
    try:
        child.attach_to_actor(
            parent,
            unreal.Name(),
            unreal.AttachmentRule.KEEP_WORLD,
            unreal.AttachmentRule.KEEP_WORLD,
            unreal.AttachmentRule.KEEP_WORLD,
            False,
        )
    except Exception as exc:
        require(False, "failed to attach presentation actor to r1 authority: " + str(exc))


def run():
    execution, manifest_path, manifest_sha = load_presentation_execution(
        "compose", __file__
    )
    is_external = presentation_is_external(execution)
    presentation_import_sha = base.require_sha(
        os.environ.get(PRESENTATION_IMPORT_SHA_ENV, ""),
        "presentation import receipt",
    )
    presentation_import, presentation_import_path = load_verified_receipt(
        execution["presentation_import_receipt"],
        presentation_import_sha,
        presentation_import_receipt_schema(execution),
        "imported_candidate",
        "presentation import receipt",
    )
    base_scene_sha = base.require_sha(
        os.environ.get(BASE_SCENE_SHA_ENV, ""), "base scene receipt"
    )
    base_scene, base_scene_path = load_verified_receipt(
        execution["scene_receipt"],
        base_scene_sha,
        base.SCENE_RECEIPT_SCHEMA,
        "saved_reloaded_candidate",
        "base scene receipt",
    )
    require(presentation_import.get("bindings", {}).get(
                "execution_manifest_sha256") == manifest_sha and
            base_scene.get("bindings", {}).get(
                "execution_manifest_sha256") == manifest_sha,
            "presentation/base scene execution binding differs")
    namespace = execution["composition_spec"]["content_namespace"]
    map_path = execution["composition_spec"]["map_path"]
    require(presentation_import.get("content_namespace") == namespace and
            base_scene.get("content_namespace") == namespace and
            base_scene.get("map_path") == map_path,
            "presentation/base scene namespace differs")
    project = base.canonical_path(unreal.Paths.get_project_file_path())
    require(project == base.canonical_path(execution["project_file"]) and
            base.sha256_file(project) == execution["project_sha256"],
            "loaded project differs from the presentation execution")

    imports_by_artifact = {
        item["artifact_id"]: item for item in presentation_import["assets"]
    }
    require(len(imports_by_artifact) == 3 and
            set(imports_by_artifact) == {
                item["artifact_id"] for item in execution["presentation_bindings"]
            }, "presentation import asset inventory differs")
    operations = [
        item for item in execution["composition_spec"]["operations"]
        if item["kind"] == "place_room_presentation_bundle"
    ]
    require(len(operations) == 3, "presentation composition operation set differs")
    bindings_by_artifact = {
        item["artifact_id"]: item for item in execution["presentation_bindings"]
    }
    entity_operations = {
        item["semantic_id"]: item
        for item in execution["composition_spec"]["operations"]
        if item["kind"] == "place_entity"
    }

    actor_subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    level_subsystem = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    require(level_subsystem.load_level(map_path), "failed to load the base candidate map")
    world = unreal.EditorLevelLibrary.get_editor_world()
    require(world is not None, "base candidate world is unavailable")
    actors = actor_subsystem.get_all_level_actors()
    authority_by_room = {}
    for operation in operations:
        semantic_tag = unreal.Name("VistaSemanticId=" + operation["room_id"])
        matches = [
            actor for actor in actors
            if semantic_tag in actor.get_editor_property("tags")
            and unreal.Name("VistaRole=room") in actor.get_editor_property("tags")
        ]
        require(len(matches) == 1,
                "r1 room collision authority is not exact: " + operation["room_id"])
        authority_by_room[operation["room_id"]] = matches[0]

    created = []
    status = "failed_unsaved_quarantined"
    error = None
    stage = {"phase": "presentation_compose", "operation_id": None}
    reload_verified = False
    shadow_delegation_verified = False
    room_observations = []
    semantic_target_baselines = {}
    try:
        if is_external:
            seen_target_ids = set()
            seen_target_actor_paths = set()
            for operation in operations:
                binding = bindings_by_artifact[operation["artifact_id"]]
                target_ids = binding["external_content"]["semantic_target_ids"]
                require(target_ids and target_ids == sorted(set(target_ids)),
                        "presentation semantic target inventory is not exact")
                for semantic_target_id in target_ids:
                    require(semantic_target_id not in seen_target_ids,
                            "presentation semantic target is bound more than once")
                    semantic_tag = unreal.Name(
                        "VistaSemanticId=" + semantic_target_id
                    )
                    matches = [
                        actor for actor in actors
                        if semantic_tag in actor.get_editor_property("tags")
                    ]
                    require(len(matches) == 1,
                            "r1 semantic visual target is not exact: "
                            + semantic_target_id)
                    actor = matches[0]
                    baseline = semantic_target_observation(
                        actor, semantic_target_id
                    )
                    expected = entity_operations.get(semantic_target_id)
                    require(
                        expected is not None
                        and baseline["actor_path"] not in seen_target_actor_paths
                        and baseline["actor_class_path"] == expected["actor_class"]
                        and baseline["semantic_id_property"] == semantic_target_id
                        and baseline["actor_hidden_in_game"] is False
                        and baseline["interaction_affordances"]
                        == sorted(expected["affordances"])
                        and all(
                            component["visible"] is True
                            and component["collision_enabled"] is True
                            and component["collision_profile"]
                            == expected["collision"]["profile"]
                            for component in baseline["render_components"]
                        ),
                        "r1 semantic target baseline differs: "
                        + semantic_target_id,
                    )
                    seen_target_ids.add(semantic_target_id)
                    seen_target_actor_paths.add(baseline["actor_path"])
                    semantic_target_baselines[semantic_target_id] = baseline
                    hide_semantic_target_visuals(actor)

        for operation in operations:
            stage = {
                "phase": "presentation_compose",
                "operation_id": operation["operation_id"],
            }
            authority = authority_by_room[operation["room_id"]]
            authority_component = static_mesh_component(authority)
            require(authority_component is not None,
                    "r1 room authority has no StaticMeshComponent")
            authority_mesh = property_or_none(authority_component, "static_mesh")
            require(isinstance(authority_mesh, unreal.StaticMesh),
                    "r1 room authority has no StaticMesh")
            require(nanite_enabled(authority_mesh) is True,
                    "r1 room shadow authority is not Nanite-enabled")
            authority.set_actor_hidden_in_game(True)
            authority_component.set_visibility(False, True)
            authority_component.set_collision_profile_name(unreal.Name("BlockAll"))
            authority_component.set_simulate_physics(False)
            authority_component.set_editor_property("generate_overlap_events", False)
            authority_component.set_cast_shadow(True)
            authority_component.set_cast_hidden_shadow(True)
            require_shadow_policy(
                authority_component,
                cast_shadow=True,
                cast_hidden_shadow=True,
                label="r1 room authority",
            )
            try:
                authority_component.set_editor_property(
                    "can_ever_affect_navigation", True
                )
            except Exception:
                pass
            set_tags(authority, list(authority.get_editor_property("tags")) + [
                "VistaRole=room_collision_proxy",
                "VistaPresentationVisibility=hidden",
                "VistaCollisionAuthority=r1",
                AUTHORITY_SHADOW_POLICY_TAG,
            ])

            imported = imports_by_artifact[operation["artifact_id"]]
            mesh = unreal.load_asset(imported["object_path"])
            require(isinstance(mesh, unreal.StaticMesh),
                    "presentation receipt object is not a StaticMesh")
            require(simple_collision_count(mesh) == 0,
                    "reloaded presentation mesh retained simple collision")
            if is_external:
                require(
                    imported.get("external_content")
                    == bindings_by_artifact[operation["artifact_id"]]["external_content"]
                    and imported.get("nanite_policy")
                    == PRESENTATION_EXTERNAL_NANITE_POLICY
                    and nanite_enabled(mesh) is False,
                    "external presentation import lost content or disabled Nanite policy",
                )
            transform = operation["transform"]
            actor = actor_subsystem.spawn_actor_from_class(
                unreal.StaticMeshActor,
                vector(transform["location_cm"]),
                rotation(transform["rotation_deg"]),
                transient=False,
            )
            require(actor is not None, "failed to spawn presentation actor")
            actor.set_actor_scale3d(vector(transform["scale"]))
            actor.set_actor_label(safe_label(operation["presentation_id"]))
            set_tags(actor, list(operation["tags"]) + [
                PRESENTATION_SHADOW_POLICY_TAG,
            ])
            component = static_mesh_component(actor)
            require(component is not None,
                    "presentation actor has no StaticMeshComponent")
            component.set_static_mesh(mesh)
            component.set_collision_profile_name(unreal.Name("NoCollision"))
            component.set_simulate_physics(False)
            component.set_editor_property("generate_overlap_events", False)
            component.set_mobility(unreal.ComponentMobility.STATIC)
            component.set_cast_shadow(False)
            component.set_cast_hidden_shadow(False)
            require_shadow_policy(
                component,
                cast_shadow=False,
                cast_hidden_shadow=False,
                label="visible presentation component",
            )
            try:
                component.set_editor_property("can_ever_affect_navigation", False)
            except Exception:
                pass
            attach_keep_world(actor, authority)
            created.append(actor)

        stage = {"phase": "presentation_save", "operation_id": None}
        require(unreal.EditorLoadingAndSavingUtils.save_map(world, map_path),
                "presentation map save failed")
        status = "saved_candidate"
        stage = {"phase": "presentation_reload", "operation_id": None}
        require(level_subsystem.load_level(map_path),
                "presentation map reload failed")
        reloaded = actor_subsystem.get_all_level_actors()
        for operation in operations:
            presentation_tag = unreal.Name(
                "VistaPresentationId=" + operation["presentation_id"]
            )
            presentation_matches = [
                actor for actor in reloaded
                if presentation_tag in actor.get_editor_property("tags")
            ]
            require(len(presentation_matches) == 1,
                    "reloaded presentation actor is not exact")
            presentation_actor = presentation_matches[0]
            component = static_mesh_component(presentation_actor)
            mesh = component.get_editor_property("static_mesh") if component else None
            imported = imports_by_artifact[operation["artifact_id"]]
            binding = bindings_by_artifact[operation["artifact_id"]]
            transform = observed_transform(presentation_actor)
            material_slot_count = (
                int(component.get_num_materials()) if component is not None else -1
            )
            require(component is not None and
                    isinstance(mesh, unreal.StaticMesh) and
                    simple_collision_count(mesh) == 0 and
                    str(mesh.get_path_name()) == imported["object_path"] and
                    transform_matches(transform, operation["transform"]) and
                    str(component.get_collision_profile_name()) == "NoCollision" and
                    material_slot_count == binding["material_count"] and
                    not bool(component.get_editor_property("generate_overlap_events")),
                    "reloaded presentation actor lost NoCollision policy")
            require(
                unreal.Name(PRESENTATION_SHADOW_POLICY_TAG)
                in presentation_actor.get_editor_property("tags"),
                "reloaded presentation actor lost shadow policy tag",
            )
            require_shadow_policy(
                component,
                cast_shadow=False,
                cast_hidden_shadow=False,
                label="reloaded visible presentation component",
            )
            if is_external:
                require(nanite_enabled(mesh) is False,
                        "reloaded external presentation mesh enabled Nanite")
            semantic_tag = unreal.Name("VistaSemanticId=" + operation["room_id"])
            authority_matches = [
                actor for actor in reloaded
                if semantic_tag in actor.get_editor_property("tags")
                and unreal.Name("VistaCollisionAuthority=r1") in
                actor.get_editor_property("tags")
            ]
            require(len(authority_matches) == 1,
                    "reloaded r1 collision authority is not exact")
            authority = authority_matches[0]
            authority_component = static_mesh_component(authority)
            authority_mesh = (
                property_or_none(authority_component, "static_mesh")
                if authority_component else None
            )
            authority_hidden = actor_hidden(authority)
            authority_visible = bool(
                authority_component.get_editor_property("visible")
            ) if authority_component else True
            parent = presentation_actor.get_attach_parent_actor()
            parent_path = str(parent.get_path_name()) if parent else ""
            authority_path = str(authority.get_path_name())
            require(authority_component is not None and authority_hidden and
                    not authority_visible,
                    "reloaded r1 collision authority became visible")
            require(str(authority_component.get_collision_profile_name()) == "BlockAll",
                    "reloaded r1 collision authority lost blocking collision")
            require(isinstance(authority_mesh, unreal.StaticMesh) and
                    nanite_enabled(authority_mesh) is True,
                    "reloaded r1 room shadow authority is not Nanite-enabled")
            require(
                unreal.Name(AUTHORITY_SHADOW_POLICY_TAG)
                in authority.get_editor_property("tags"),
                "reloaded r1 authority lost shadow policy tag",
            )
            require_shadow_policy(
                authority_component,
                cast_shadow=True,
                cast_hidden_shadow=True,
                label="reloaded r1 room authority",
            )
            require(parent_path == authority_path,
                    "reloaded presentation actor lost its r1 authority attachment")
            observation = {
                "artifact_id": operation["artifact_id"],
                "presentation_id": operation["presentation_id"],
                "room_id": operation["room_id"],
                "room_kind": operation["room_kind"],
                "actor_path": str(presentation_actor.get_path_name()),
                "static_mesh_object_path": str(mesh.get_path_name()),
                "world_transform_cm": transform,
                "collision_profile": str(component.get_collision_profile_name()),
                "material_slot_count": material_slot_count,
                "attach_parent_actor_path": parent_path,
                "r1_authority_actor_path": authority_path,
                "r1_authority_collision_profile": str(
                    authority_component.get_collision_profile_name()
                ),
                "r1_authority_hidden_in_game": authority_hidden,
                "r1_authority_component_visible": authority_visible,
            }
            if is_external:
                target_observations = []
                target_ids = binding["external_content"]["semantic_target_ids"]
                for semantic_target_id in target_ids:
                    semantic_tag = unreal.Name(
                        "VistaSemanticId=" + semantic_target_id
                    )
                    matches = [
                        actor for actor in reloaded
                        if semantic_tag in actor.get_editor_property("tags")
                    ]
                    require(len(matches) == 1,
                            "reloaded r1 semantic visual target is not exact: "
                            + semantic_target_id)
                    target_observation = semantic_target_observation(
                        matches[0], semantic_target_id
                    )
                    baseline = semantic_target_baselines.get(semantic_target_id)
                    require(
                        baseline is not None
                        and target_observation["actor_path"]
                        == baseline["actor_path"]
                        and target_observation["actor_class_path"]
                        == baseline["actor_class_path"]
                        and target_observation["semantic_id_property"]
                        == baseline["semantic_id_property"]
                        and target_observation["interaction_affordances"]
                        == baseline["interaction_affordances"]
                        and target_observation["actor_hidden_in_game"] is True
                        and [
                            component["component_path"]
                            for component in target_observation["render_components"]
                        ] == [
                            component["component_path"]
                            for component in baseline["render_components"]
                        ]
                        and all(
                            current["visible"] is False
                            and current["collision_enabled"]
                            == original["collision_enabled"] is True
                            and current["collision_profile"]
                            == original["collision_profile"]
                            for current, original in zip(
                                target_observation["render_components"],
                                baseline["render_components"],
                            )
                        ),
                        "reloaded r1 semantic target lost identity, interaction, "
                        "hidden visuals, or collision: " + semantic_target_id,
                    )
                    target_observations.append(target_observation)
                observation.update({
                    "external_content": binding["external_content"],
                    "nanite_policy": PRESENTATION_EXTERNAL_NANITE_POLICY,
                    "nanite_enabled": nanite_enabled(mesh),
                    "r1_semantic_visual_observations": target_observations,
                })
            room_observations.append(observation)
        shadow_delegation_verified = True
        reload_verified = True
        status = "saved_reloaded_candidate"
    except Exception as exc:
        error = {
            "type": type(exc).__name__,
            "message": str(exc)[:512],
            "stage": stage,
        }
        status = (
            "partial_saved_quarantined"
            if status == "saved_candidate"
            else "failed_unsaved_quarantined"
        )

    gates = {
        "map_saved": status == "saved_reloaded_candidate",
        "map_reloaded": reload_verified,
        "exact_three_presentation_actors": reload_verified,
        "presentation_no_collision_verified": reload_verified,
        "hidden_r1_collision_authority_verified": (
            reload_verified and shadow_delegation_verified
        ),
        "semantic_authority_preserved": reload_verified,
        "quarantined": status != "saved_reloaded_candidate",
        "runtime_play_proof": "pending",
    }
    if is_external:
        expected_target_count = sum(
            len(binding["external_content"]["semantic_target_ids"])
            for binding in execution["presentation_bindings"]
        )
        gates["external_nanite_disabled_verified"] = (
            reload_verified and all(
                item.get("nanite_policy") == PRESENTATION_EXTERNAL_NANITE_POLICY
                and item.get("nanite_enabled") is False
                for item in room_observations
            )
        )
        gates["external_r1_semantic_visual_targets_verified"] = (
            reload_verified
            and len(semantic_target_baselines) == expected_target_count
            and sum(
                len(item.get("r1_semantic_visual_observations", []))
                for item in room_observations
            ) == expected_target_count
        )
    receipt = {
        "schema_version": presentation_scene_receipt_schema(execution),
        "status": status,
        "error": error,
        "bindings": {
            "engine": str(unreal.SystemLibrary.get_engine_version()),
            "project": project,
            "execution_manifest": manifest_path,
            "execution_manifest_sha256": manifest_sha,
            "base_scene_receipt": base_scene_path,
            "base_scene_receipt_sha256": base_scene_sha,
            "presentation_import_receipt": presentation_import_path,
            "presentation_import_receipt_sha256": presentation_import_sha,
            "composition_spec_sha256": execution["composition_spec_sha256"],
        },
        "content_namespace": namespace,
        "map_path": map_path,
        "room_observations": sorted(
            room_observations, key=lambda item: item["room_id"]
        ),
        "gates": gates,
    }
    receipt_sha = write_exclusive_receipt(
        execution["presentation_scene_receipt"], execution["attempt_root"], receipt
    )
    result = {
        "status": status,
        "receipt": execution["presentation_scene_receipt"],
        "sha256": receipt_sha,
    }
    write_exclusive_receipt(
        os.path.join(execution["attempt_root"], PRESENTATION_SCENE_RESULT_FILE),
        execution["attempt_root"],
        result,
    )
    marker = PRESENTATION_SCENE_MARKER + json.dumps(result, sort_keys=True)
    unreal.log(marker)
    print(marker, flush=True)
    if status != "saved_reloaded_candidate":
        raise RuntimeError("VISTA presentation composition failed; candidate quarantined")


run()
