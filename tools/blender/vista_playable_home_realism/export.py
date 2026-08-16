"""Role-aware GLB export and normalized forge manifests.

The review GLBs intentionally preserve the authored hierarchy.  Unreal's
headless Interchange path has a narrower contract: one source GLB must resolve
to exactly one primary StaticMesh.  The UE bundle path therefore joins a
room's presentation components into one room-local mesh while retaining the
source material slots and image-backed PBR materials.
"""

from __future__ import annotations

import json
import pathlib
import re
from dataclasses import asdict
from typing import Any, Mapping, Sequence

from .architecture import ForgePlan
from .config import canonical_json_bytes, normalized, sha256_file
from .external_assets import (
    external_material_alpha_policy,
    validate_external_staticization_ledger,
)
from .placement import bundle_external_content


UE_BUNDLE_ARTIFACT_KIND = "ue_import_bundle"
UE_BUNDLE_ROOT_TRANSFORM_POLICY = "room_local_geometry_identity_root"
UE_BUNDLE_SEMANTIC_POLICY = "presentation_only_preserve_r1_authority"
UE_BUNDLE_COLLISION_POLICY = "presentation_no_collision_use_hidden_r1_proxies"
UE_BUNDLE_UNREAL_COLLISION_PROFILE = "NoCollision"


def safe_slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def build_quality_claims(texture_size_px: int) -> dict[str, Any]:
    """Describe forge quality without claiming downstream r2 acceptance.

    Texture resolution can make this architecture eligible as source evidence,
    but hero assets, Unreal renderer observation, gameplay regression, and a
    retained human review are outside Blender's authority.  Consequently this
    function intentionally has no code path that accepts final r2 visuals.
    """

    production_candidate = texture_size_px >= 512
    return {
        "quality_class": "production_candidate" if production_candidate else "smoke_only",
        "texture_size_px": texture_size_px,
        "production_minimum_texture_size_px": 512,
        "eligible_as_architecture_source_evidence": production_candidate,
        "requires_downstream_asset_and_ue_review": True,
        "accepted_as_r2_visual_evidence": False,
        "r2_visual_acceptance_authority": "downstream_seal_and_human_review",
    }


def normalized_manifest(
    plan: ForgePlan,
    *,
    material_receipts: Sequence[Mapping[str, Any]] | None = None,
    texture_size_px: int,
    ue_import_bundles: Sequence[Mapping[str, Any]] | None = None,
    external_staticization: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    role_counts: dict[str, int] = {}
    room_counts: dict[str, int] = {}
    for component in plan.components:
        role_counts[component.export_role] = role_counts.get(component.export_role, 0) + 1
        room_counts[component.room_id] = room_counts.get(component.room_id, 0) + 1
    export_contract: dict[str, Any] = {
        "coordinate_system": "Blender metric metres, glTF Y-up export",
        "semantic_policy": "presentation_only_preserve_r1_authority",
        "collision_policy": "presentation_no_collision_use_hidden_r1_proxies",
        "cameras_exported": False,
        "lights_exported": False,
        "custom_properties_exported_as_extras": True,
    }
    external = getattr(plan, "external_placement", None)
    if external is not None:
        export_contract["external_material_alpha_policy"] = external_material_alpha_policy()
    payload: dict[str, Any] = {
        "schema_version": plan.schema_version,
        "forge_id": plan.forge_id,
        "house_revision": plan.house_revision,
        "visual_profile_id": plan.visual_profile_id,
        "seed": plan.seed,
        "source_house_digest": plan.source_house_digest,
        "source_profile_digest": plan.source_profile_digest,
        "forge_plan_digest": plan.content_digest,
        "build_quality": build_quality_claims(texture_size_px),
        "rooms": [asdict(item) for item in plan.rooms],
        "openings": [asdict(item) for item in plan.openings],
        "components": [asdict(item) for item in plan.components],
        "dressing": asdict(plan.dressing),
        "materials": list(material_receipts) if material_receipts is not None else list(plan.material_plan),
        "role_counts": role_counts,
        "room_component_counts": room_counts,
        "export_contract": export_contract,
        "ue_import_bundles": list(ue_import_bundles or ()),
    }
    if external is not None:
        payload["external_placement"] = asdict(external)
        payload["external_staticization"] = (
            validate_external_staticization_ledger(external_staticization)
            if external_staticization is not None
            else None
        )
    return normalized(payload)


def write_json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value))
    path.chmod(0o600)


def _select(bpy: Any, objects: Sequence[Any]) -> None:
    bpy.ops.object.select_all(action="DESELECT")
    selectable = [item for item in objects if item is not None]
    for obj in selectable:
        obj.hide_set(False)
        obj.select_set(True)
    if selectable:
        bpy.context.view_layer.objects.active = selectable[0]


def _used_material_names(objects: Sequence[Any]) -> list[str]:
    result: set[str] = set()
    for obj in objects:
        if obj.type != "MESH":
            continue
        used_indices = {polygon.material_index for polygon in obj.data.polygons}
        for index in used_indices:
            if index < len(obj.material_slots) and obj.material_slots[index].material is not None:
                result.add(obj.material_slots[index].material.name)
    return sorted(result)


def _export_one(bpy: Any, path: pathlib.Path, objects: Sequence[Any]) -> None:
    _select(bpy, objects)
    bpy.ops.export_scene.gltf(
        filepath=str(path),
        export_format="GLB",
        use_selection=True,
        export_cameras=False,
        export_lights=False,
        export_apply=True,
        export_yup=True,
        export_extras=True,
        export_materials="EXPORT",
        export_image_format="AUTO",
    )
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(f"Blender did not produce {path}")
    path.chmod(0o600)


def ue_bundle_relative_path(room_kind: str) -> str:
    """Return the fixed relative path for one room's UE import bundle."""

    return f"ue_import_bundles/{safe_slug(room_kind)}_presentation_bundle.glb"


def ue_bundle_contract(
    plan: ForgePlan,
    room: Any,
    *,
    exported_material_names: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Return the deterministic, pre-export portion of a UE bundle receipt."""

    components = [item for item in plan.components if item.room_id == room.room_id]
    material_ids = sorted({item.material_id for item in components})
    external = getattr(plan, "external_placement", None)
    if external is not None:
        if exported_material_names is None:
            raise RuntimeError("external bundle contract requires the realized material-name inventory")
        material_ids = sorted(set(exported_material_names))
    if not components or len(material_ids) < 2:
        raise RuntimeError(f"room {room.room_id} cannot form a multi-material UE bundle")
    payload = {
            "artifact_id": f"ue_bundle.room.{room.kind}",
            "artifact_kind": UE_BUNDLE_ARTIFACT_KIND,
            "target_asset_id": f"asset.bundle.{room.kind}",
            "room_id": room.room_id,
            "room_kind": room.kind,
            "relative_path": ue_bundle_relative_path(room.kind),
            "media_type": "model/gltf-binary",
            "expected_world_transform_cm": {
                "location_cm": [value * 100.0 for value in room.location_m],
                "rotation_deg": list(room.rotation_deg),
                "scale": list(room.scale),
            },
            "bundle_root_transform": {
                "location_m": [0.0, 0.0, 0.0],
                "rotation_deg": [0.0, 0.0, 0.0],
                "scale": [1.0, 1.0, 1.0],
            },
            "root_transform_policy": UE_BUNDLE_ROOT_TRANSFORM_POLICY,
            "semantic_policy": UE_BUNDLE_SEMANTIC_POLICY,
            "collision_policy": UE_BUNDLE_COLLISION_POLICY,
            "unreal_collision_profile": UE_BUNDLE_UNREAL_COLLISION_PROFILE,
            "cameras_exported": False,
            "lights_exported": False,
            "material_ids": material_ids,
            "source_hashes": {
                "house_sha256": plan.source_house_digest,
                "visual_profile_sha256": plan.source_profile_digest,
                "forge_plan_sha256": plan.content_digest,
            },
        }
    if external is not None:
        payload["external_content"] = bundle_external_content(external, room.room_id)
    return normalized(payload)


def _ue_bundle_object(
    bpy: Any,
    plan: ForgePlan,
    room: Any,
    component_objects: Mapping[str, Any],
    external_objects: Mapping[str, Sequence[Any]],
    collection: Any,
    contract: Mapping[str, Any],
) -> Any:
    """Create one identity-root mesh with room-local component geometry."""

    components = [item for item in plan.components if item.room_id == room.room_id]
    duplicates: list[Any] = []
    for component in components:
        source = component_objects[component.component_id]
        if source.type != "MESH":
            raise RuntimeError(f"UE bundle component is not a mesh: {component.component_id}")
        duplicate = source.copy()
        duplicate.data = source.data.copy()
        duplicate.parent = None
        # The source object is parented under a world-positioned room root, but
        # its matrix_local is the authored room-local placement.  Bake that
        # matrix into the copied vertices, leaving a true identity export root.
        duplicate.data.transform(source.matrix_local)
        duplicate.location = (0.0, 0.0, 0.0)
        duplicate.rotation_euler = (0.0, 0.0, 0.0)
        duplicate.scale = (1.0, 1.0, 1.0)
        duplicate.name = f"VISTA_UEBundlePart_{safe_slug(component.component_id)}"[:63]
        collection.objects.link(duplicate)
        duplicates.append(duplicate)
    external = getattr(plan, "external_placement", None)
    if external is not None:
        for placement in external.placements:
            if placement.room_id != room.room_id:
                continue
            for source in external_objects.get(placement.placement_id, ()):
                if source.type != "MESH":
                    raise RuntimeError(f"external UE bundle part is not a mesh: {placement.placement_id}")
                duplicate = source.copy()
                duplicate.data = source.data.copy()
                duplicate.parent = None
                duplicate.data.transform(source.matrix_local)
                duplicate.location = (0.0, 0.0, 0.0)
                duplicate.rotation_euler = (0.0, 0.0, 0.0)
                duplicate.scale = (1.0, 1.0, 1.0)
                duplicate.name = f"VISTA_UEBundleExternal_{safe_slug(placement.placement_id)}"[:63]
                collection.objects.link(duplicate)
                duplicates.append(duplicate)
    if not duplicates:
        raise RuntimeError(f"room {room.room_id} has no mesh components")
    _select(bpy, duplicates)
    bpy.context.view_layer.objects.active = duplicates[0]
    if len(duplicates) > 1:
        bpy.ops.object.join()
    bundle = bpy.context.active_object
    bundle.name = f"VISTA_UEBundle_{safe_slug(room.kind)}"[:63]
    bundle.data.name = f"{bundle.name}_Mesh"
    bundle.location = (0.0, 0.0, 0.0)
    bundle.rotation_euler = (0.0, 0.0, 0.0)
    bundle.scale = (1.0, 1.0, 1.0)
    for key in tuple(bundle.keys()):
        if str(key).startswith("vista_"):
            del bundle[key]
    bundle["vista_bundle_contract"] = (
        "one_room_one_mesh_v2" if external is not None else "one_room_one_mesh_v1"
    )
    bundle["vista_artifact_id"] = contract["artifact_id"]
    bundle["vista_target_asset_id"] = contract["target_asset_id"]
    bundle["vista_room_id"] = room.room_id
    bundle["vista_room_kind"] = room.kind
    bundle["vista_root_transform_policy"] = contract["root_transform_policy"]
    bundle["vista_expected_world_transform_cm_json"] = json.dumps(
        contract["expected_world_transform_cm"], sort_keys=True, separators=(",", ":")
    )
    bundle["vista_semantic_policy"] = contract["semantic_policy"]
    bundle["vista_collision_policy"] = contract["collision_policy"]
    bundle["vista_unreal_collision_profile"] = contract["unreal_collision_profile"]
    bundle["vista_material_ids_json"] = json.dumps(contract["material_ids"], separators=(",", ":"))
    bundle["vista_source_house_sha256"] = plan.source_house_digest
    bundle["vista_source_visual_profile_sha256"] = plan.source_profile_digest
    bundle["vista_source_forge_plan_sha256"] = plan.content_digest
    if external is not None:
        bundle["vista_external_content_json"] = json.dumps(
            contract["external_content"], sort_keys=True, separators=(",", ":")
        )
    return bundle


def _export_ue_import_bundles(
    bpy: Any,
    output_root: pathlib.Path,
    plan: ForgePlan,
    *,
    component_objects: Mapping[str, Any],
    external_objects: Mapping[str, Sequence[Any]],
) -> list[dict[str, Any]]:
    """Export and independently inspect three one-mesh Unreal bundles."""

    from .inspect import inspect_glb

    bundle_root = output_root / "ue_import_bundles"
    bundle_root.mkdir(mode=0o700)
    temp_collection = bpy.data.collections.new("VISTA_R2_UEBundle_Export_Temp")
    bpy.context.scene.collection.children.link(temp_collection)
    artifacts: list[dict[str, Any]] = []
    try:
        for room in plan.rooms:
            external = getattr(plan, "external_placement", None)
            exported_material_names: list[str] | None = None
            if external is not None:
                source_objects = [
                    component_objects[item.component_id]
                    for item in plan.components
                    if item.room_id == room.room_id
                ]
                for placement in external.placements:
                    if placement.room_id == room.room_id:
                        source_objects.extend(external_objects.get(placement.placement_id, ()))
                exported_material_names = _used_material_names(source_objects)
            contract = ue_bundle_contract(
                plan,
                room,
                exported_material_names=exported_material_names,
            )
            path = output_root / contract["relative_path"]
            bundle = _ue_bundle_object(
                bpy,
                plan,
                room,
                component_objects,
                external_objects,
                temp_collection,
                contract,
            )
            _export_one(bpy, path, [bundle])
            is_external = "external_content" in contract
            inspection = inspect_glb(
                path,
                include_external_material_alpha=is_external,
            )
            if inspection["mesh_count"] != 1 or inspection["mesh_node_count"] != 1:
                raise RuntimeError(f"UE bundle did not export as exactly one mesh: {path}")
            if inspection["camera_count"] != 0 or inspection["light_count"] != 0:
                raise RuntimeError(f"UE bundle unexpectedly contains a camera or light: {path}")
            if inspection["bundle_root_is_identity"] is not True:
                raise RuntimeError(f"UE bundle root transform is not identity: {path}")
            if inspection["material_count"] != len(contract["material_ids"]):
                raise RuntimeError(f"UE bundle material set differs from its source contract: {path}")
            if is_external and sorted(inspection["material_names"]) != contract["material_ids"]:
                raise RuntimeError(f"UE bundle material names differ from realized inventory: {path}")
            if inspection["material_count"] < 2:
                raise RuntimeError(f"UE bundle is not multi-material: {path}")
            if inspection["pbr_complete_material_count"] != inspection["material_count"]:
                raise RuntimeError(f"UE bundle has an incomplete PBR material: {path}")
            if (not is_external and inspection["texture_count"] < inspection["material_count"] * 3) or (
                is_external and inspection["texture_count"] < 3
            ):
                raise RuntimeError(f"UE bundle lost required PBR textures: {path}")
            if inspection["bundle_metadata"].get("vista_room_id") != room.room_id:
                raise RuntimeError(f"UE bundle lost room identity: {path}")
            record = {
                **contract,
                "sha256": inspection["sha256"],
                "size_bytes": inspection["size_bytes"],
                "mesh_count": inspection["mesh_count"],
                "material_count": inspection["material_count"],
                "pbr_complete_material_count": inspection["pbr_complete_material_count"],
                "texture_count": inspection["texture_count"],
            }
            artifacts.append(normalized(record))
            bpy.data.objects.remove(bundle, do_unlink=True)
    finally:
        if temp_collection.name in bpy.data.collections:
            bpy.data.collections.remove(temp_collection)
    return artifacts


def export_role_aware_glbs(
    bpy: Any,
    output_root: pathlib.Path,
    plan: ForgePlan,
    *,
    room_roots: Mapping[str, Any],
    component_objects: Mapping[str, Any],
    metadata_objects: Mapping[str, Sequence[Any]],
    external_objects: Mapping[str, Sequence[Any]] | None = None,
) -> list[dict[str, Any]]:
    """Export review GLBs and one import-ready bundle per finished room."""

    glb_root = output_root / "glb"
    glb_root.mkdir(mode=0o700)
    artifacts: list[dict[str, Any]] = []
    room_by_id = {room.room_id: room for room in plan.rooms}
    external_objects = external_objects or {}
    external = getattr(plan, "external_placement", None)
    for room_id in sorted(room_by_id):
        room = room_by_id[room_id]
        selected = [room_roots[room_id]]
        selected.extend(
            component_objects[item.component_id]
            for item in plan.components
            if item.room_id == room_id
        )
        selected.extend(metadata_objects.get(room_id, ()))
        if external is not None:
            for placement in external.placements:
                if placement.room_id == room_id:
                    selected.extend(external_objects.get(placement.placement_id, ()))
        path = glb_root / f"{safe_slug(room.kind)}_presentation.glb"
        _export_one(bpy, path, selected)
        artifacts.append(
            {
                "artifact_id": f"glb.room.{room.kind}",
                "artifact_kind": "review_presentation",
                "room_id": room_id,
                "relative_path": path.relative_to(output_root).as_posix(),
                "media_type": "model/gltf-binary",
                "sha256": sha256_file(path),
                "size_bytes": path.stat().st_size,
                "component_roles": sorted({item.export_role for item in plan.components if item.room_id == room_id}),
            }
        )
    all_objects = list(room_roots.values()) + list(component_objects.values())
    for values in external_objects.values():
        all_objects.extend(values)
    for values in metadata_objects.values():
        all_objects.extend(values)
    full_path = glb_root / "vertical_slice_presentation.glb"
    _export_one(bpy, full_path, all_objects)
    artifacts.append(
        {
            "artifact_id": "glb.vertical_slice",
            "artifact_kind": "review_presentation",
            "room_id": None,
            "relative_path": full_path.relative_to(output_root).as_posix(),
            "media_type": "model/gltf-binary",
            "sha256": sha256_file(full_path),
            "size_bytes": full_path.stat().st_size,
            "component_roles": sorted({item.export_role for item in plan.components}),
        }
    )
    artifacts.extend(
        _export_ue_import_bundles(
            bpy,
            output_root,
            plan,
            component_objects=component_objects,
            external_objects=external_objects,
        )
    )
    return artifacts


def artifact_receipt(
    artifacts: Sequence[Mapping[str, Any]], *, external: bool = False
) -> dict[str, Any]:
    ue_import_bundles = [
        item for item in artifacts if item.get("artifact_kind") == UE_BUNDLE_ARTIFACT_KIND
    ]
    return normalized(
        {
            "schema_version": (
                "simworld.vista.playable-home-realism-artifacts/v2"
                if external
                else "simworld.vista.playable-home-realism-artifacts/v1"
            ),
            "artifacts": list(artifacts),
            "ue_import_bundles": ue_import_bundles,
        }
    )
