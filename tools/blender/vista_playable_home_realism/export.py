"""Role-aware GLB export and normalized forge manifests."""

from __future__ import annotations

import pathlib
import re
from dataclasses import asdict
from typing import Any, Mapping, Sequence

from .architecture import ForgePlan
from .config import canonical_json_bytes, normalized, sha256_file


def safe_slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def normalized_manifest(
    plan: ForgePlan,
    *,
    material_receipts: Sequence[Mapping[str, Any]] | None = None,
    texture_size_px: int,
) -> dict[str, Any]:
    role_counts: dict[str, int] = {}
    room_counts: dict[str, int] = {}
    for component in plan.components:
        role_counts[component.export_role] = role_counts.get(component.export_role, 0) + 1
        room_counts[component.room_id] = room_counts.get(component.room_id, 0) + 1
    quality_class = "production_candidate" if texture_size_px >= 512 else "smoke_only"
    payload: dict[str, Any] = {
        "schema_version": plan.schema_version,
        "forge_id": plan.forge_id,
        "house_revision": plan.house_revision,
        "visual_profile_id": plan.visual_profile_id,
        "seed": plan.seed,
        "source_house_digest": plan.source_house_digest,
        "source_profile_digest": plan.source_profile_digest,
        "forge_plan_digest": plan.content_digest,
        "build_quality": {
            "quality_class": quality_class,
            "texture_size_px": texture_size_px,
            "production_minimum_texture_size_px": 512,
            "accepted_as_r2_visual_evidence": quality_class == "production_candidate",
        },
        "rooms": [asdict(item) for item in plan.rooms],
        "openings": [asdict(item) for item in plan.openings],
        "components": [asdict(item) for item in plan.components],
        "dressing": asdict(plan.dressing),
        "materials": list(material_receipts) if material_receipts is not None else list(plan.material_plan),
        "role_counts": role_counts,
        "room_component_counts": room_counts,
        "export_contract": {
            "coordinate_system": "Blender metric metres, glTF Y-up export",
            "semantic_policy": "presentation_only_preserve_r1_authority",
            "collision_policy": "presentation_no_collision_use_hidden_r1_proxies",
            "cameras_exported": False,
            "lights_exported": False,
            "custom_properties_exported_as_extras": True,
        },
    }
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


def export_role_aware_glbs(
    bpy: Any,
    output_root: pathlib.Path,
    plan: ForgePlan,
    *,
    room_roots: Mapping[str, Any],
    component_objects: Mapping[str, Any],
    metadata_objects: Mapping[str, Sequence[Any]],
) -> list[dict[str, Any]]:
    """Export one room-local presentation GLB plus a complete slice GLB."""

    glb_root = output_root / "glb"
    glb_root.mkdir(mode=0o700)
    artifacts: list[dict[str, Any]] = []
    room_by_id = {room.room_id: room for room in plan.rooms}
    for room_id in sorted(room_by_id):
        room = room_by_id[room_id]
        selected = [room_roots[room_id]]
        selected.extend(
            component_objects[item.component_id]
            for item in plan.components
            if item.room_id == room_id
        )
        selected.extend(metadata_objects.get(room_id, ()))
        path = glb_root / f"{safe_slug(room.kind)}_presentation.glb"
        _export_one(bpy, path, selected)
        artifacts.append(
            {
                "artifact_id": f"glb.room.{room.kind}",
                "room_id": room_id,
                "relative_path": path.relative_to(output_root).as_posix(),
                "media_type": "model/gltf-binary",
                "sha256": sha256_file(path),
                "size_bytes": path.stat().st_size,
                "component_roles": sorted({item.export_role for item in plan.components if item.room_id == room_id}),
            }
        )
    all_objects = list(room_roots.values()) + list(component_objects.values())
    for values in metadata_objects.values():
        all_objects.extend(values)
    full_path = glb_root / "vertical_slice_presentation.glb"
    _export_one(bpy, full_path, all_objects)
    artifacts.append(
        {
            "artifact_id": "glb.vertical_slice",
            "room_id": None,
            "relative_path": full_path.relative_to(output_root).as_posix(),
            "media_type": "model/gltf-binary",
            "sha256": sha256_file(full_path),
            "size_bytes": full_path.stat().st_size,
            "component_roles": sorted({item.export_role for item in plan.components}),
        }
    )
    return artifacts


def artifact_receipt(artifacts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return normalized(
        {
            "schema_version": "simworld.vista.playable-home-realism-artifacts/v1",
            "artifacts": list(artifacts),
        }
    )
