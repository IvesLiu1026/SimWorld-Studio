#!/usr/bin/env python3
"""Build the deterministic VISTA Playable Home r2 architectural slice.

Run with the pinned Blender binary::

    blender --background --factory-startup --disable-autoexec --python build.py -- \
      --house /absolute/house.json \
      --visual-profile /absolute/realistic_interior_r2.json \
      --output-root /absolute/fresh-output

Production manifests default to 512 px procedural PBR textures.  That output
is only an architecture-source candidate: this forge cannot accept final r2
visual evidence without downstream assets, Unreal observation, and human
review.  Fast smoke tests must explicitly pass ``--texture-size-px 64`` and
are labeled smoke-only.
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys
from typing import Any, Mapping, Sequence


if __package__ in {None, ""}:
    package_root = pathlib.Path(__file__).resolve().parents[2]
    if str(package_root) not in sys.path:
        sys.path.insert(0, str(package_root))
    from blender.vista_playable_home_realism.architecture import (  # type: ignore[import-not-found]
        ForgePlan,
        build_external_forge_plan,
        build_forge_plan,
    )
    from blender.vista_playable_home_realism.config import (  # type: ignore[import-not-found]
        DEFAULT_TEXTURE_SIZE_PX,
        EXPECTED_BLENDER_VERSION,
        ForgeInputError,
        canonical_json_bytes,
        content_digest,
        load_json_object,
        prepare_output_root,
        sha256_file,
    )
    from blender.vista_playable_home_realism.export import (  # type: ignore[import-not-found]
        artifact_receipt,
        build_quality_claims,
        export_role_aware_glbs,
        normalized_manifest,
        write_json,
    )
    from blender.vista_playable_home_realism.inspect import inspect_output  # type: ignore[import-not-found]
    from blender.vista_playable_home_realism.materials import realize_blender_materials  # type: ignore[import-not-found]
    from blender.vista_playable_home_realism.external_assets import (  # type: ignore[import-not-found]
        ExternalAssetSet,
        load_external_asset_set,
        realize_external_placements,
        staged_external_asset_set,
    )
    from blender.vista_playable_home_realism.placement import (  # type: ignore[import-not-found]
        PlacementManifestDocument,
        load_placement_manifest,
    )
else:
    from .architecture import ForgePlan, build_external_forge_plan, build_forge_plan
    from .config import (
        DEFAULT_TEXTURE_SIZE_PX,
        EXPECTED_BLENDER_VERSION,
        ForgeInputError,
        canonical_json_bytes,
        content_digest,
        load_json_object,
        prepare_output_root,
        sha256_file,
    )
    from .export import (
        artifact_receipt,
        build_quality_claims,
        export_role_aware_glbs,
        normalized_manifest,
        write_json,
    )
    from .inspect import inspect_output
    from .materials import realize_blender_materials
    from .external_assets import (
        ExternalAssetSet,
        load_external_asset_set,
        realize_external_placements,
        staged_external_asset_set,
    )
    from .placement import PlacementManifestDocument, load_placement_manifest


PREVIEW_WIDTH = 1280
PREVIEW_HEIGHT = 720
BUILD_ACCEPTANCE_SCHEMA = "simworld.vista.playable-home-realism-forge-acceptance/v1"
BUILD_ACCEPTANCE_FILENAME = "forge-accepted.json"
_BUILD_ACCEPTANCE_KEYS = frozenset(
    {
        "schema_version",
        "status",
        "normalized_manifest_sha256",
        "artifact_receipt_sha256",
        "inspection_receipt_sha256",
        "build_receipt_sha256",
        "external_staticization_receipt_sha256",
    }
)


def _acceptance_output_root(output_root: pathlib.Path) -> pathlib.Path:
    if not output_root.is_absolute():
        raise ForgeInputError("accepted forge output root must be absolute")
    if output_root.is_symlink():
        raise ForgeInputError("accepted forge output root may not be a symbolic link")
    try:
        resolved = output_root.resolve(strict=True)
    except OSError as error:
        raise ForgeInputError("accepted forge output root is unavailable") from error
    if not resolved.is_dir():
        raise ForgeInputError("accepted forge output root must be a directory")
    return resolved


def _acceptance_file(output_root: pathlib.Path, name: str) -> pathlib.Path:
    path = output_root / name
    if path.is_symlink() or not path.is_file():
        raise ForgeInputError(f"accepted forge output is missing regular file {name}")
    return path


def _is_sha256(value: Any) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _validate_build_acceptance_payload(
    output_root: pathlib.Path,
    acceptance: Mapping[str, Any],
) -> dict[str, Any]:
    if set(acceptance) != _BUILD_ACCEPTANCE_KEYS:
        raise ForgeInputError("forge acceptance marker fields are not closed")
    if (
        acceptance.get("schema_version") != BUILD_ACCEPTANCE_SCHEMA
        or acceptance.get("status") != "accepted"
    ):
        raise ForgeInputError("forge acceptance marker schema/status is invalid")
    for field in (
        "normalized_manifest_sha256",
        "artifact_receipt_sha256",
        "inspection_receipt_sha256",
        "build_receipt_sha256",
    ):
        if not _is_sha256(acceptance.get(field)):
            raise ForgeInputError(f"forge acceptance marker {field} is invalid")

    manifest_path = _acceptance_file(output_root, "normalized-manifest.json")
    artifact_path = _acceptance_file(output_root, "artifact-receipt.json")
    inspection_path = _acceptance_file(output_root, "inspection-receipt.json")
    build_path = _acceptance_file(output_root, "build-receipt.json")
    bound_files = {
        "normalized_manifest_sha256": manifest_path,
        "artifact_receipt_sha256": artifact_path,
        "inspection_receipt_sha256": inspection_path,
        "build_receipt_sha256": build_path,
    }
    for field, path in bound_files.items():
        if acceptance[field] != sha256_file(path):
            raise ForgeInputError(
                f"forge acceptance marker {field} differs from {path.name}"
            )

    manifest = load_json_object(manifest_path, label="accepted normalized manifest")
    inspection_receipt = load_json_object(
        inspection_path,
        label="accepted inspection receipt",
    )
    build_receipt = load_json_object(build_path, label="accepted build receipt")
    recomputed_inspection = inspect_output(output_root)
    if inspection_receipt != recomputed_inspection:
        raise ForgeInputError("accepted inspection receipt differs from fresh inspection")
    if build_receipt.get("normalized_manifest_sha256") != acceptance[
        "normalized_manifest_sha256"
    ]:
        raise ForgeInputError("accepted build receipt normalized manifest binding differs")
    if build_receipt.get("inspection_digest") != content_digest(recomputed_inspection):
        raise ForgeInputError("accepted build receipt inspection binding differs")

    manifest_schema = manifest.get("schema_version")
    staticization_marker = acceptance.get("external_staticization_receipt_sha256")
    if manifest_schema == "simworld.vista.playable-home-realism-forge/v2":
        staticization_path = _acceptance_file(
            output_root,
            "external-staticization-receipt.json",
        )
        if not _is_sha256(staticization_marker):
            raise ForgeInputError(
                "external forge acceptance lacks a staticization receipt binding"
            )
        staticization_sha256 = sha256_file(staticization_path)
        if staticization_marker != staticization_sha256:
            raise ForgeInputError(
                "forge acceptance staticization binding differs from its receipt"
            )
        if (
            build_receipt.get("schema_version")
            != "simworld.vista.playable-home-realism-blender-build/v2"
            or build_receipt.get("external_staticization_receipt_sha256")
            != staticization_sha256
        ):
            raise ForgeInputError("accepted external build receipt binding differs")
    elif manifest_schema == "simworld.vista.playable-home-realism-forge/v1":
        if (
            staticization_marker is not None
            or build_receipt.get("schema_version")
            != "simworld.vista.playable-home-realism-blender-build/v1"
        ):
            raise ForgeInputError("accepted v1 build receipt binding differs")
    else:
        raise ForgeInputError("accepted normalized manifest schema is unsupported")
    return dict(acceptance)


def validate_build_acceptance(output_root: pathlib.Path) -> dict[str, Any]:
    """Require the terminal marker and independently revalidate all bound receipts."""

    root = _acceptance_output_root(output_root)
    marker_path = _acceptance_file(root, BUILD_ACCEPTANCE_FILENAME)
    marker = load_json_object(marker_path, label="forge acceptance marker")
    return _validate_build_acceptance_payload(root, marker)


def _write_build_acceptance(output_root: pathlib.Path) -> dict[str, Any]:
    root = _acceptance_output_root(output_root)
    manifest_path = _acceptance_file(root, "normalized-manifest.json")
    artifact_path = _acceptance_file(root, "artifact-receipt.json")
    inspection_path = _acceptance_file(root, "inspection-receipt.json")
    build_path = _acceptance_file(root, "build-receipt.json")
    staticization_path = root / "external-staticization-receipt.json"
    marker = {
        "schema_version": BUILD_ACCEPTANCE_SCHEMA,
        "status": "accepted",
        "normalized_manifest_sha256": sha256_file(manifest_path),
        "artifact_receipt_sha256": sha256_file(artifact_path),
        "inspection_receipt_sha256": sha256_file(inspection_path),
        "build_receipt_sha256": sha256_file(build_path),
        "external_staticization_receipt_sha256": (
            sha256_file(_acceptance_file(root, staticization_path.name))
            if staticization_path.exists()
            else None
        ),
    }
    # Validate the complete output before emitting the terminal marker.  A
    # failed forge therefore cannot leave behind a truthful-looking accepted
    # status file merely because Blender returned an OS success code.
    _validate_build_acceptance_payload(root, marker)
    write_json(root / BUILD_ACCEPTANCE_FILENAME, marker)
    return validate_build_acceptance(root)


def parse_blender_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    raw = list(sys.argv if argv is None else argv)
    forwarded = raw[raw.index("--") + 1 :] if "--" in raw else raw
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--house", type=pathlib.Path, required=True)
    parser.add_argument("--visual-profile", type=pathlib.Path, required=True)
    parser.add_argument("--output-root", type=pathlib.Path, required=True)
    parser.add_argument("--texture-size-px", type=int, default=DEFAULT_TEXTURE_SIZE_PX)
    parser.add_argument("--external-acquisition-root", type=pathlib.Path)
    parser.add_argument("--external-placement-manifest", type=pathlib.Path)
    args = parser.parse_args(forwarded)
    if args.texture_size_px < 64 or args.texture_size_px > 2048 or args.texture_size_px & (args.texture_size_px - 1):
        parser.error("--texture-size-px must be a power of two from 64 through 2048")
    if (args.external_acquisition_root is None) != (args.external_placement_manifest is None):
        parser.error("external mode requires both --external-acquisition-root and --external-placement-manifest")
    return args


def _reset_scene(bpy: Any) -> None:
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for collection in tuple(bpy.data.collections):
        if collection != bpy.context.scene.collection:
            bpy.data.collections.remove(collection)
    for material in tuple(bpy.data.materials):
        bpy.data.materials.remove(material)


def _configure_scene(bpy: Any) -> None:
    scene = bpy.context.scene
    scene.unit_settings.system = "METRIC"
    scene.unit_settings.length_unit = "METERS"
    scene.unit_settings.scale_length = 1.0
    # Cycles CPU is deliberate: the shared server denies /dev/dri render nodes,
    # so Eevee can return a formally valid black frame after EGL failures.
    # This small architectural scene renders quickly on CPU and remains
    # independent of GPU/service ownership.
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = 16
    scene.cycles.use_denoising = True
    scene.cycles.max_bounces = 4
    scene.render.resolution_x = PREVIEW_WIDTH
    scene.render.resolution_y = PREVIEW_HEIGHT
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.render.image_settings.color_depth = "8"
    scene.render.image_settings.compression = 35
    scene.render.film_transparent = False
    scene.render.use_file_extension = True
    try:
        scene.view_settings.look = "AgX - Medium High Contrast"
    except (TypeError, ValueError):
        try:
            scene.view_settings.look = "Medium High Contrast"
        except (TypeError, ValueError):
            pass
    scene.world.use_nodes = True
    background = scene.world.node_tree.nodes.get("Background")
    if background is not None:
        background.inputs["Color"].default_value = (0.018, 0.025, 0.032, 1.0)
        background.inputs["Strength"].default_value = 0.38


def _relink_object(obj: Any, collection: Any) -> None:
    for current in tuple(obj.users_collection):
        current.objects.unlink(obj)
    collection.objects.link(obj)


def _safe_name(value: str, prefix: str = "VISTA") -> str:
    cleaned = "_".join(part for part in value.replace(".", "_").replace("/", "_").split("_") if part)
    return f"{prefix}_{cleaned}"[:63]


def _apply_edge_softening(bpy: Any, obj: Any, dimensions: Sequence[float], role: str) -> None:
    if role in {"wall_opaque", "floor_finish", "ceiling_finish", "window_glass", "exterior_treatment"}:
        return
    width = min(0.008, min(float(item) for item in dimensions) * 0.15)
    if width < 0.001:
        return
    modifier = obj.modifiers.new(name="VISTA_EdgeSoftening", type="BEVEL")
    modifier.width = width
    modifier.segments = 2
    modifier.limit_method = "ANGLE"
    modifier.angle_limit = math.radians(25.0)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.modifier_apply(modifier=modifier.name)


def _component_object(bpy: Any, component: Any, material: Any, room_root: Any, collection: Any) -> Any:
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0.0, 0.0, 0.0))
    obj = bpy.context.active_object
    obj.name = _safe_name(component.component_id, "VISTA_R2")
    obj.data.name = f"{obj.name}_Mesh"
    _relink_object(obj, collection)
    obj.parent = room_root
    obj.matrix_parent_inverse.identity()
    obj.location = component.location_m
    obj.rotation_euler = tuple(math.radians(value) for value in component.rotation_deg)
    obj.dimensions = component.dimensions_m
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    _apply_edge_softening(bpy, obj, component.dimensions_m, component.role)
    obj.data.materials.append(material)
    obj["vista_component_id"] = component.component_id
    obj["vista_room_id"] = component.room_id
    obj["vista_room_kind"] = component.room_kind
    obj["vista_component_role"] = component.role
    obj["vista_export_role"] = component.export_role
    obj["vista_material_id"] = component.material_id
    obj["vista_collision_policy"] = component.collision_policy
    obj["vista_semantic_policy"] = component.semantic_policy
    obj["vista_source_opening_id"] = component.source_opening_id or ""
    obj["vista_metric_dimensions_m"] = list(component.dimensions_m)
    obj.hide_render = not component.preview_visible
    return obj


def _metadata_empty(
    bpy: Any,
    collection: Any,
    room_root: Any,
    name: str,
    location: Sequence[float],
    properties: Mapping[str, Any],
    *,
    display_type: str,
    display_size: float,
    scale: Sequence[float] = (1.0, 1.0, 1.0),
) -> Any:
    obj = bpy.data.objects.new(_safe_name(name, "VISTA_Meta"), None)
    collection.objects.link(obj)
    obj.parent = room_root
    obj.location = location
    obj.empty_display_type = display_type
    obj.empty_display_size = display_size
    obj.scale = scale
    obj.hide_render = True
    for key, value in properties.items():
        obj[key] = value
    return obj


def _build_geometry(bpy: Any, plan: ForgePlan, materials: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, list[Any]], dict[str, Any]]:
    room_roots: dict[str, Any] = {}
    room_collections: dict[str, Any] = {}
    for room in plan.rooms:
        collection = bpy.data.collections.new(_safe_name(room.kind, "VISTA_Room"))
        bpy.context.scene.collection.children.link(collection)
        root = bpy.data.objects.new(_safe_name(room.room_id, "VISTA_RoomRoot"), None)
        collection.objects.link(root)
        root.location = room.location_m
        root.rotation_euler = tuple(math.radians(value) for value in room.rotation_deg)
        root.scale = room.scale
        root["vista_room_id"] = room.room_id
        root["vista_room_kind"] = room.kind
        root["vista_house_revision"] = plan.house_revision
        root["vista_visual_profile_id"] = plan.visual_profile_id
        room_roots[room.room_id] = root
        room_collections[room.room_id] = collection

    component_objects: dict[str, Any] = {}
    for component in plan.components:
        obj = _component_object(
            bpy,
            component,
            materials[component.material_id],
            room_roots[component.room_id],
            room_collections[component.room_id],
        )
        component_objects[component.component_id] = obj

    metadata_objects: dict[str, list[Any]] = {room.room_id: [] for room in plan.rooms}
    for anchor in plan.dressing.anchors:
        metadata_objects[anchor.room_id].append(
            _metadata_empty(
                bpy,
                room_collections[anchor.room_id],
                room_roots[anchor.room_id],
                anchor.anchor_id,
                anchor.location_m,
                {
                    "vista_metadata_role": "dressing_anchor",
                    "vista_anchor_id": anchor.anchor_id,
                    "vista_room_id": anchor.room_id,
                    "vista_purpose": anchor.purpose,
                    "vista_allowed_categories_json": json.dumps(anchor.allowed_categories, separators=(",", ":")),
                    "vista_clearance_radius_m": anchor.clearance_radius_m,
                    "vista_deterministic_yaw_deg": anchor.deterministic_yaw_deg,
                },
                display_type="SPHERE",
                display_size=0.10,
            )
        )
    for volume in plan.dressing.exclusions:
        center = tuple((volume.min_m[index] + volume.max_m[index]) / 2 for index in range(3))
        half = tuple((volume.max_m[index] - volume.min_m[index]) / 2 for index in range(3))
        metadata_objects[volume.room_id].append(
            _metadata_empty(
                bpy,
                room_collections[volume.room_id],
                room_roots[volume.room_id],
                volume.exclusion_id,
                center,
                {
                    "vista_metadata_role": "dressing_exclusion",
                    "vista_exclusion_id": volume.exclusion_id,
                    "vista_room_id": volume.room_id,
                    "vista_exclusion_kind": volume.exclusion_kind,
                    "vista_source_id": volume.source_id,
                    "vista_min_m": list(volume.min_m),
                    "vista_max_m": list(volume.max_m),
                },
                display_type="CUBE",
                display_size=1.0,
                scale=half,
            )
        )
    return room_roots, component_objects, metadata_objects, room_collections


def _point_at(mathutils: Any, obj: Any, target: Sequence[float]) -> None:
    direction = mathutils.Vector(target) - obj.location
    obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()


def _create_preview_camera_and_lights(bpy: Any, mathutils: Any) -> Any:
    collection = bpy.data.collections.new("VISTA_R2_Preview_Only")
    bpy.context.scene.collection.children.link(collection)
    camera_data = bpy.data.cameras.new("VISTA_R2_OverviewCamera_Data")
    camera = bpy.data.objects.new("VISTA_R2_OverviewCamera", camera_data)
    collection.objects.link(camera)
    camera.location = (0.0, -14.2, 10.2)
    camera_data.lens = 48.0
    camera_data.sensor_width = 36.0
    _point_at(mathutils, camera, (0.0, -1.15, 0.75))
    bpy.context.scene.camera = camera

    def area(name: str, location: Sequence[float], target: Sequence[float], energy: float, size: float, color: Sequence[float]) -> None:
        data = bpy.data.lights.new(name=f"{name}_Data", type="AREA")
        data.energy = energy
        data.shape = "DISK"
        data.size = size
        data.color = color
        light = bpy.data.objects.new(name, data)
        collection.objects.link(light)
        light.location = location
        _point_at(mathutils, light, target)

    area("VISTA_R2_Key", (-4.0, -4.0, 6.8), (-3.2, -1.4, 0.9), 1650.0, 5.0, (1.0, 0.83, 0.66))
    area("VISTA_R2_KitchenFill", (4.4, -2.0, 6.3), (3.7, -1.0, 1.0), 1450.0, 4.0, (0.76, 0.88, 1.0))
    area("VISTA_R2_EntryFill", (0.0, 1.5, 5.6), (0.0, -1.2, 0.8), 1150.0, 3.0, (0.86, 0.92, 1.0))
    sun_data = bpy.data.lights.new(name="VISTA_R2_Sun_Data", type="SUN")
    sun_data.energy = 2.0
    sun_data.angle = math.radians(18.0)
    sun = bpy.data.objects.new("VISTA_R2_Sun", sun_data)
    collection.objects.link(sun)
    sun.rotation_euler = (math.radians(28.0), math.radians(-18.0), math.radians(-32.0))
    return camera


def _render_preview(bpy: Any, output_root: pathlib.Path) -> tuple[pathlib.Path, dict[str, float]]:
    preview_root = output_root / "preview"
    preview_root.mkdir(mode=0o700)
    path = preview_root / "vertical_slice_overview.png"
    bpy.context.scene.render.filepath = str(path)
    bpy.ops.render.render(write_still=True)
    if not path.is_file() or path.stat().st_size < 10_000:
        raise RuntimeError("Blender preview is missing or unexpectedly blank-sized")
    rendered_image = bpy.data.images.load(str(path), check_existing=False)
    pixels = list(rendered_image.pixels[:])
    luminance = [
        0.2126 * pixels[index] + 0.7152 * pixels[index + 1] + 0.0722 * pixels[index + 2]
        for index in range(0, len(pixels), 4)
    ]
    statistics = {
        "linear_luminance_min": round(min(luminance), 6),
        "linear_luminance_mean": round(sum(luminance) / len(luminance), 6),
        "linear_luminance_max": round(max(luminance), 6),
    }
    if statistics["linear_luminance_max"] < 0.15 or statistics["linear_luminance_mean"] < 0.025:
        raise RuntimeError(f"Blender preview failed luminance validation: {statistics}")
    bpy.data.images.remove(rendered_image)
    path.chmod(0o600)
    return path, statistics


def _artifact(path: pathlib.Path, output_root: pathlib.Path, artifact_id: str, media_type: str) -> dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "relative_path": path.relative_to(output_root).as_posix(),
        "media_type": media_type,
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def _build_with_blender_runtime(
    bpy: Any,
    mathutils: Any,
    house: Mapping[str, Any],
    profile: Mapping[str, Any],
    output_root: pathlib.Path,
    *,
    texture_size_px: int,
    external_asset_set: ExternalAssetSet | None = None,
    external_placement_manifest: PlacementManifestDocument | None = None,
) -> dict[str, Any]:
    if tuple(bpy.app.version) != EXPECTED_BLENDER_VERSION:
        raise RuntimeError(
            f"requires Blender {'.'.join(map(str, EXPECTED_BLENDER_VERSION))}, got {bpy.app.version_string}"
        )
    if (external_asset_set is None) != (external_placement_manifest is None):
        raise RuntimeError("external asset set and placement manifest must be supplied together")
    plan = (
        build_external_forge_plan(house, profile, external_asset_set, external_placement_manifest)
        if external_asset_set is not None and external_placement_manifest is not None
        else build_forge_plan(house, profile)
    )
    _reset_scene(bpy)
    _configure_scene(bpy)
    materials, material_receipts = realize_blender_materials(
        bpy, output_root, texture_size_px=texture_size_px
    )
    room_roots, component_objects, metadata_objects, room_collections = _build_geometry(
        bpy, plan, materials
    )
    external_objects: dict[str, list[Any]] = {}
    external_staticization: dict[str, Any] | None = None
    if external_asset_set is not None:
        (
            external_objects,
            external_material_receipts,
            external_staticization,
        ) = realize_external_placements(
            bpy,
            mathutils,
            external_asset_set,
            plan.external_placement,
            room_roots=room_roots,
            room_collections=room_collections,
        )
        material_receipts.extend(external_material_receipts)
    artifacts = export_role_aware_glbs(
        bpy,
        output_root,
        plan,
        room_roots=room_roots,
        component_objects=component_objects,
        metadata_objects=metadata_objects,
        external_objects=external_objects,
    )
    staticization_path: pathlib.Path | None = None
    if external_staticization is not None:
        staticization_path = output_root / "external-staticization-receipt.json"
        write_json(staticization_path, external_staticization)
        artifacts.append(
            _artifact(
                staticization_path,
                output_root,
                "receipt.external_staticization",
                "application/json",
            )
        )
    # The normalized manifest binds the exact import-ready GLB bytes.  Export
    # precedes manifest emission deliberately; the GLBs do not embed the
    # manifest hash, so this ordering is deterministic and non-circular.
    ue_import_bundles = [
        item for item in artifacts if item.get("artifact_kind") == "ue_import_bundle"
    ]
    manifest = normalized_manifest(
        plan,
        material_receipts=material_receipts,
        texture_size_px=texture_size_px,
        ue_import_bundles=ue_import_bundles,
        external_staticization=external_staticization,
    )
    manifest_path = output_root / "normalized-manifest.json"
    write_json(manifest_path, manifest)
    _create_preview_camera_and_lights(bpy, mathutils)
    preview_path, preview_statistics = _render_preview(bpy, output_root)
    scene_root = output_root / "scene"
    scene_root.mkdir(mode=0o700)
    blend_path = scene_root / "vista_playable_home_realistic_interior_r2.blend"
    if external_asset_set is not None:
        bpy.ops.file.pack_all()
    bpy.ops.wm.save_as_mainfile(filepath=str(blend_path), check_existing=False, compress=True)
    if not blend_path.is_file() or blend_path.stat().st_size == 0:
        raise RuntimeError("Blender did not save the source scene")
    blend_path.chmod(0o600)

    artifacts.extend(
        [
            _artifact(manifest_path, output_root, "manifest.normalized", "application/json"),
            _artifact(preview_path, output_root, "preview.vertical_slice_overview", "image/png"),
            _artifact(blend_path, output_root, "scene.blend", "application/x-blender"),
        ]
    )
    for path in sorted((output_root / "textures").glob("*.png")):
        artifacts.append(_artifact(path, output_root, f"texture.{path.stem}", "image/png"))
    artifact_path = output_root / "artifact-receipt.json"
    write_json(
        artifact_path,
        artifact_receipt(artifacts, external=external_asset_set is not None),
    )
    inspection = inspect_output(output_root)
    inspection_path = output_root / "inspection-receipt.json"
    write_json(inspection_path, inspection)
    build_receipt = {
        "schema_version": (
            "simworld.vista.playable-home-realism-blender-build/v2"
            if external_asset_set is not None
            else "simworld.vista.playable-home-realism-blender-build/v1"
        ),
        "forge_plan_digest": plan.content_digest,
        "normalized_manifest_sha256": sha256_file(manifest_path),
        "blender_version": list(bpy.app.version),
        "blender_version_string": bpy.app.version_string,
        **build_quality_claims(texture_size_px),
        "component_count": len(plan.components),
        "opening_count": len(plan.openings),
        "dressing_anchor_count": len(plan.dressing.anchors),
        "exclusion_volume_count": len(plan.dressing.exclusions),
        "glb_count": len([item for item in artifacts if item["media_type"] == "model/gltf-binary"]),
        "preview_statistics": preview_statistics,
        "inspection_digest": content_digest(inspection),
    }
    if external_asset_set is not None:
        if staticization_path is None or external_staticization is None:
            raise RuntimeError("external build lacks its staticization receipt")
        build_receipt["external_placement_plan_sha256"] = plan.external_placement.content_digest
        build_receipt["acquisition_receipt"] = external_asset_set.receipt_reference()
        build_receipt["external_placement_count"] = len(plan.external_placement.placements)
        build_receipt["external_semantic_target_count"] = len(
            plan.external_placement.semantic_target_ids
        )
        build_receipt["external_dressing_count"] = len(plan.external_placement.dressing_ids)
        build_receipt["external_staticization_content_digest"] = (
            external_staticization["content_digest"]
        )
        build_receipt["external_staticization_receipt_sha256"] = sha256_file(
            staticization_path
        )
    build_path = output_root / "build-receipt.json"
    write_json(build_path, build_receipt)
    _write_build_acceptance(output_root)
    return build_receipt


def build_with_blender(
    bpy: Any,
    mathutils: Any,
    house: Mapping[str, Any],
    profile: Mapping[str, Any],
    output_root: pathlib.Path,
    *,
    texture_size_px: int,
    external_asset_set: ExternalAssetSet | None = None,
    external_placement_manifest: PlacementManifestDocument | None = None,
) -> dict[str, Any]:
    if (external_asset_set is None) != (external_placement_manifest is None):
        raise RuntimeError("external asset set and placement manifest must be supplied together")
    if external_asset_set is None:
        return _build_with_blender_runtime(
            bpy,
            mathutils,
            house,
            profile,
            output_root,
            texture_size_px=texture_size_px,
        )
    # Keep the verified private snapshot alive through GLB export, preview
    # rendering, source-scene packing, and all receipt inspection. Runtime
    # absolute paths never enter the normalized/public manifests.
    with staged_external_asset_set(external_asset_set) as runtime_asset_set:
        return _build_with_blender_runtime(
            bpy,
            mathutils,
            house,
            profile,
            output_root,
            texture_size_px=texture_size_px,
            external_asset_set=runtime_asset_set,
            external_placement_manifest=external_placement_manifest,
        )


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_blender_args(argv)
    output_root = prepare_output_root(args.output_root)
    house = load_json_object(args.house, label="HouseSpec")
    profile = load_json_object(args.visual_profile, label="VisualProfile")
    external_asset_set = (
        load_external_asset_set(args.external_acquisition_root)
        if args.external_acquisition_root is not None
        else None
    )
    external_placement_manifest = (
        load_placement_manifest(args.external_placement_manifest)
        if args.external_placement_manifest is not None
        else None
    )
    try:
        import bpy  # type: ignore[import-not-found]
        import mathutils  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("this script must run inside Blender") from exc
    receipt = build_with_blender(
        bpy,
        mathutils,
        house,
        profile,
        output_root,
        texture_size_px=args.texture_size_px,
        external_asset_set=external_asset_set,
        external_placement_manifest=external_placement_manifest,
    )
    print(canonical_json_bytes(receipt).decode("utf-8"), end="")


if __name__ == "__main__":
    main()
