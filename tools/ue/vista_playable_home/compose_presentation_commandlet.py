"""Layer r2 room presentation actors over the saved r1 candidate map."""

import json
import os
import sys

import unreal


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import commandlet_common as base  # noqa: E402
from presentation_commandlet_common import (  # noqa: E402
    BASE_SCENE_SHA_ENV,
    PRESENTATION_IMPORT_RECEIPT_SCHEMA,
    PRESENTATION_IMPORT_SHA_ENV,
    PRESENTATION_SCENE_MARKER,
    PRESENTATION_SCENE_RECEIPT_SCHEMA,
    PRESENTATION_SCENE_RESULT_FILE,
    load_presentation_execution,
    load_verified_receipt,
    require,
    write_exclusive_receipt,
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


def actor_record(actor):
    return {
        "label": str(actor.get_actor_label()),
        "class_path": str(actor.get_class().get_path_name()),
        "path": str(actor.get_path_name()),
        "tags": sorted(str(tag) for tag in actor.get_editor_property("tags")),
    }


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
    presentation_import_sha = base.require_sha(
        os.environ.get(PRESENTATION_IMPORT_SHA_ENV, ""),
        "presentation import receipt",
    )
    presentation_import, presentation_import_path = load_verified_receipt(
        execution["presentation_import_receipt"],
        presentation_import_sha,
        PRESENTATION_IMPORT_RECEIPT_SCHEMA,
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
    try:
        for operation in operations:
            stage = {
                "phase": "presentation_compose",
                "operation_id": operation["operation_id"],
            }
            authority = authority_by_room[operation["room_id"]]
            authority_component = static_mesh_component(authority)
            require(authority_component is not None,
                    "r1 room authority has no StaticMeshComponent")
            authority.set_actor_hidden_in_game(True)
            authority_component.set_visibility(False, True)
            authority_component.set_collision_profile_name(unreal.Name("BlockAll"))
            authority_component.set_simulate_physics(False)
            authority_component.set_editor_property("generate_overlap_events", False)
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
            ])

            imported = imports_by_artifact[operation["artifact_id"]]
            mesh = unreal.load_asset(imported["object_path"])
            require(isinstance(mesh, unreal.StaticMesh),
                    "presentation receipt object is not a StaticMesh")
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
            set_tags(actor, operation["tags"])
            component = static_mesh_component(actor)
            require(component is not None,
                    "presentation actor has no StaticMeshComponent")
            component.set_static_mesh(mesh)
            component.set_collision_profile_name(unreal.Name("NoCollision"))
            component.set_simulate_physics(False)
            component.set_editor_property("generate_overlap_events", False)
            component.set_mobility(unreal.ComponentMobility.STATIC)
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
            component = static_mesh_component(presentation_matches[0])
            require(component is not None and
                    str(component.get_collision_profile_name()) == "NoCollision" and
                    not bool(component.get_editor_property("generate_overlap_events")),
                    "reloaded presentation actor lost NoCollision policy")
            semantic_tag = unreal.Name("VistaSemanticId=" + operation["room_id"])
            authority_matches = [
                actor for actor in reloaded
                if semantic_tag in actor.get_editor_property("tags")
                and unreal.Name("VistaCollisionAuthority=r1") in
                actor.get_editor_property("tags")
            ]
            require(len(authority_matches) == 1,
                    "reloaded r1 collision authority is not exact")
            authority_component = static_mesh_component(authority_matches[0])
            require(authority_matches[0].is_hidden() or
                    not bool(authority_component.get_editor_property("visible")),
                    "reloaded r1 collision authority became visible")
            require(str(authority_component.get_collision_profile_name()) == "BlockAll",
                    "reloaded r1 collision authority lost blocking collision")
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

    inventory = [
        actor_record(actor) for actor in actor_subsystem.get_all_level_actors()
    ]
    receipt = {
        "schema_version": PRESENTATION_SCENE_RECEIPT_SCHEMA,
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
        "presentation_actor_inventory": sorted(
            inventory, key=lambda item: item["path"]
        ),
        "gates": {
            "map_saved": status == "saved_reloaded_candidate",
            "map_reloaded": reload_verified,
            "exact_three_presentation_actors": reload_verified,
            "presentation_no_collision_verified": reload_verified,
            "hidden_r1_collision_authority_verified": reload_verified,
            "semantic_authority_preserved": reload_verified,
            "quarantined": status != "saved_reloaded_candidate",
            "runtime_play_proof": "pending",
        },
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
