#!/usr/bin/env python3
"""Build the HouseSpec-pinned VISTA Playable Home with Blender 4.5.8.

Run only through Blender::

    blender --background --factory-startup \
      --python tools/blender/vista_playable_home/build.py -- \
      --house /absolute/path/to/house.json \
      --output-root /absolute/append-only/run/blender

The build emits a full-world GLB/.blend and fixed previews for visual evidence,
plus one local-origin GLB per non-builtin ``asset_id`` for Unreal import.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pathlib
import re
import sys
from datetime import datetime, timezone
from typing import Any, Sequence


if __package__ in {None, ""}:
    package_root = pathlib.Path(__file__).resolve().parents[2]
    if str(package_root) not in sys.path:
        sys.path.insert(0, str(package_root))
    from blender.vista_playable_home.contract_scene import (  # type: ignore[import-not-found]
        OUTPUT_FILENAMES,
        ContractScenePlan,
        RenderNode,
        build_contract_plan,
        canonical_json_bytes,
        load_house,
        normalized_manifest,
    )
else:
    from .contract_scene import (
        OUTPUT_FILENAMES,
        ContractScenePlan,
        RenderNode,
        build_contract_plan,
        canonical_json_bytes,
        load_house,
        normalized_manifest,
    )


EXPECTED_BLENDER_VERSION = (4, 5, 8)
PREVIEW_WIDTH = 960
PREVIEW_HEIGHT = 720
MEDIA_TYPES = {
    "blend": "application/x-blender",
    "glb": "model/gltf-binary",
    "preview_overview": "image/png",
    "preview_interior": "image/png",
    "normalized_manifest": "application/json",
}


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_blender_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    raw = list(sys.argv if argv is None else argv)
    forwarded = raw[raw.index("--") + 1 :] if "--" in raw else raw
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--house", required=True, type=pathlib.Path, help="Absolute pinned HouseSpec JSON")
    parser.add_argument("--output-root", required=True, type=pathlib.Path, help="Absolute append-only output directory")
    parser.add_argument("--seed", type=int, help="Optional assertion against HouseSpec.seed")
    return parser.parse_args(forwarded)


def prepare_output_root(path: pathlib.Path) -> pathlib.Path:
    if not path.is_absolute():
        raise RuntimeError("--output-root must be absolute")
    if path.is_symlink():
        raise RuntimeError("--output-root may not be a symbolic link")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    resolved = path.resolve(strict=True)
    if any(resolved.iterdir()):
        raise RuntimeError(f"refusing to write into non-empty append-only output root: {resolved}")
    return resolved


def _safe_node_name(prefix: str, value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_")
    return f"VISTA_{prefix}_{slug}"[:63]


def _asset_filename(asset_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", asset_id) + ".glb"


def _configure_scene(bpy: Any) -> None:
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for collection in tuple(bpy.data.collections):
        bpy.data.collections.remove(collection)
    for material in tuple(bpy.data.materials):
        bpy.data.materials.remove(material)

    scene = bpy.context.scene
    scene.unit_settings.system = "METRIC"
    scene.unit_settings.length_unit = "METERS"
    scene.unit_settings.scale_length = 1.0
    scene.render.engine = "BLENDER_EEVEE_NEXT"
    scene.render.resolution_x = PREVIEW_WIDTH
    scene.render.resolution_y = PREVIEW_HEIGHT
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.render.image_settings.color_depth = "8"
    scene.render.image_settings.compression = 28
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
        background.inputs["Color"].default_value = (0.012, 0.018, 0.025, 1.0)
        background.inputs["Strength"].default_value = 0.28


def _make_materials(bpy: Any, plan: ContractScenePlan) -> dict[str, Any]:
    materials: dict[str, Any] = {}
    for source in plan.materials:
        material = bpy.data.materials.new(_safe_node_name("M", source.material_id))
        material.use_nodes = True
        material.diffuse_color = source.base_color
        material["vista_material_id"] = source.material_id
        material["vista_pbr_contract"] = "Principled baseColor+roughness+metallic"
        principled = material.node_tree.nodes.get("Principled BSDF")
        if principled is None:
            raise RuntimeError(f"missing Principled BSDF for {source.material_id}")
        principled.inputs["Base Color"].default_value = source.base_color
        principled.inputs["Roughness"].default_value = source.roughness
        principled.inputs["Metallic"].default_value = source.metallic
        if principled.inputs.get("IOR") is not None:
            principled.inputs["IOR"].default_value = 1.46
        materials[source.material_id] = material
    return materials


def _apply_bevel(bpy: Any, obj: Any, dimensions: Sequence[float]) -> None:
    width = min(0.025, min(float(value) for value in dimensions) * 0.16)
    if width <= 0.0015:
        return
    modifier = obj.modifiers.new(name="VISTA_EdgeSoftening", type="BEVEL")
    modifier.width = width
    modifier.segments = 2
    modifier.limit_method = "ANGLE"
    modifier.angle_limit = math.radians(25.0)
    if hasattr(modifier, "harden_normals"):
        modifier.harden_normals = True
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.modifier_apply(modifier=modifier.name)


def _primitive_object(bpy: Any, primitive: Any, material: Any) -> Any:
    rotation = tuple(math.radians(value) for value in primitive.rotation_deg)
    if primitive.kind == "box":
        bpy.ops.mesh.primitive_cube_add(size=1.0, location=primitive.location_m, rotation=rotation)
        obj = bpy.context.active_object
        obj.dimensions = primitive.dimensions_m
        bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
        _apply_bevel(bpy, obj, primitive.dimensions_m)
    elif primitive.kind == "cylinder":
        bpy.ops.mesh.primitive_cylinder_add(
            vertices=28,
            radius=primitive.radius_m,
            depth=primitive.dimensions_m[2],
            end_fill_type="NGON",
            location=primitive.location_m,
            rotation=rotation,
        )
        obj = bpy.context.active_object
        bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
        for polygon in obj.data.polygons:
            polygon.use_smooth = True
    elif primitive.kind == "sphere":
        bpy.ops.mesh.primitive_uv_sphere_add(segments=24, ring_count=12, radius=primitive.radius_m, location=primitive.location_m)
        obj = bpy.context.active_object
        bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
        for polygon in obj.data.polygons:
            polygon.use_smooth = True
    elif primitive.kind == "torus":
        bpy.ops.mesh.primitive_torus_add(
            major_segments=28,
            minor_segments=10,
            major_radius=primitive.major_radius_m,
            minor_radius=primitive.minor_radius_m,
            location=primitive.location_m,
            rotation=rotation,
        )
        obj = bpy.context.active_object
        bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
        for polygon in obj.data.polygons:
            polygon.use_smooth = True
    else:
        raise RuntimeError(f"unsupported primitive kind: {primitive.kind}")
    obj.name = _safe_node_name("Part", primitive.primitive_id)
    obj.data.materials.append(material)
    return obj


def _join_parts(bpy: Any, node: RenderNode, parts: list[Any]) -> Any:
    if not parts:
        return bpy.data.objects.new(_safe_node_name("Anchor", node.node_id), None)
    bpy.ops.object.select_all(action="DESELECT")
    for part in parts:
        part.select_set(True)
    bpy.context.view_layer.objects.active = parts[0]
    bpy.ops.object.join()
    obj = bpy.context.active_object
    obj.name = _safe_node_name("Node", node.node_id)
    obj.data.name = f"{obj.name}_Mesh"
    return obj


def _set_node_metadata(obj: Any, node: RenderNode) -> None:
    obj["vista_node_id"] = node.node_id
    obj["semantic_id"] = node.semantic_entity_id or ""
    obj["room_id"] = node.room_id
    obj["category"] = node.category
    obj["asset_ref"] = node.asset_ref
    obj["component_role"] = node.component_role
    obj["mobility"] = node.mobility
    obj["collision_policy"] = node.collision_policy
    obj["nav_obstacle"] = node.nav_obstacle
    obj["affordances_json"] = json.dumps(node.affordances, separators=(",", ":"))
    obj["initial_state_json"] = json.dumps(node.initial_state, sort_keys=True, separators=(",", ":"))
    obj["tags_json"] = json.dumps(node.tags, separators=(",", ":"))
    obj["instance_group"] = node.instance_group or ""
    obj["semantic_actor_policy"] = "one_asset_one_mesh" if node.primitives else "runtime_actor"


def _build_world(bpy: Any, mathutils: Any, plan: ContractScenePlan, materials: dict[str, Any]) -> tuple[list[Any], dict[str, Any], list[Any]]:
    room_roots: dict[str, Any] = {}
    export_objects: list[Any] = []
    node_objects: dict[str, Any] = {}
    preview_hidden: list[Any] = []
    room_source = {room["room_id"]: room for room in plan.house["rooms"]}
    room_plans = {room.room_id: room for room in plan.rooms}
    for room_id, room in sorted(room_source.items()):
        collection = bpy.data.collections.new(_safe_node_name("Room", room["kind"]))
        bpy.context.scene.collection.children.link(collection)
        root = bpy.data.objects.new(_safe_node_name("RoomRoot", room_id), None)
        root.location = room["transform"]["location_m"]
        root.rotation_euler = tuple(math.radians(value) for value in room["transform"]["rotation_deg"])
        root.scale = room["transform"]["scale"]
        root["room_id"] = room_id
        root["room_kind"] = room["kind"]
        root["bundle_asset_ref"] = room["bundle_asset_ref"]
        collection.objects.link(root)
        room_roots[room_id] = root
        export_objects.append(root)

    collections = {room_id: room_roots[room_id].users_collection[0] for room_id in room_roots}
    for node in plan.nodes:
        parts = [_primitive_object(bpy, primitive, materials[primitive.material_id]) for primitive in node.primitives]
        obj = _join_parts(bpy, node, parts)
        # Newly joined parts are in the scene collection; relink once.
        for current in tuple(obj.users_collection):
            current.objects.unlink(obj)
        collections[node.room_id].objects.link(obj)
        obj.parent = room_roots[node.room_id]
        obj.matrix_parent_inverse = mathutils.Matrix.Identity(4)
        obj.location = node.transform_location_m
        obj.rotation_euler = tuple(math.radians(value) for value in node.transform_rotation_deg)
        obj.scale = node.transform_scale
        _set_node_metadata(obj, node)
        if not node.preview_visible:
            obj.hide_render = True
            preview_hidden.append(obj)
        node_objects[node.node_id] = obj
        export_objects.append(obj)

    portal_collection = bpy.data.collections.new("VISTA_Portals")
    bpy.context.scene.collection.children.link(portal_collection)
    for portal in plan.house["portals"]:
        obj = bpy.data.objects.new(_safe_node_name("Portal", portal["portal_id"]), None)
        obj.location = portal["world_transform"]["location_m"]
        obj.rotation_euler = tuple(math.radians(value) for value in portal["world_transform"]["rotation_deg"])
        obj.scale = portal["world_transform"]["scale"]
        obj["portal_id"] = portal["portal_id"]
        obj["from_room_id"] = portal["from_room_id"]
        obj["to_room_id"] = portal["to_room_id"]
        obj["door_entity_id"] = portal["door_entity_id"] or ""
        obj["clearance_json"] = json.dumps(portal["clearance"], sort_keys=True, separators=(",", ":"))
        obj["nav_policy"] = portal["nav_policy"]
        portal_collection.objects.link(obj)
        export_objects.append(obj)
    return export_objects, node_objects, preview_hidden


def _point_at(mathutils: Any, obj: Any, target: Sequence[float]) -> None:
    direction = mathutils.Vector(target) - obj.location
    obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()


def _presentation(bpy: Any, mathutils: Any, plan: ContractScenePlan) -> dict[str, Any]:
    collection = bpy.data.collections.new("VISTA_Presentation")
    bpy.context.scene.collection.children.link(collection)
    cameras: dict[str, Any] = {}
    for view in plan.preview_views:
        data = bpy.data.cameras.new(_safe_node_name("CameraData", view.view_id))
        data.lens = view.lens_mm
        data.sensor_width = 36.0
        camera = bpy.data.objects.new(_safe_node_name("Camera", view.view_id), data)
        camera.location = view.location_m
        _point_at(mathutils, camera, view.target_m)
        collection.objects.link(camera)
        cameras[view.view_id] = camera

    def light(name: str, light_type: str, location: Sequence[float], target: Sequence[float], energy: float, color: Sequence[float], size: float = 1.0) -> None:
        data = bpy.data.lights.new(name=f"{name}_Data", type=light_type)
        data.energy = energy
        data.color = color
        if light_type == "AREA":
            data.shape = "DISK"
            data.size = size
        obj = bpy.data.objects.new(name, data)
        obj.location = location
        _point_at(mathutils, obj, target)
        collection.objects.link(obj)

    light("VISTA_Key", "AREA", (-5.0, -5.0, 10.0), (0.0, 2.0, 0.0), 2300.0, (1.0, 0.79, 0.62), 6.0)
    light("VISTA_Fill", "AREA", (7.0, 0.0, 9.0), (0.0, 2.0, 0.0), 1800.0, (0.58, 0.76, 1.0), 5.0)
    light("VISTA_Rim", "AREA", (0.0, 9.0, 8.0), (0.0, 2.0, 0.5), 1600.0, (0.75, 0.91, 1.0), 4.0)
    return cameras


def _select(bpy: Any, objects: Sequence[Any]) -> None:
    bpy.ops.object.select_all(action="DESELECT")
    for obj in objects:
        obj.hide_set(False)
        obj.select_set(True)
    if objects:
        bpy.context.view_layer.objects.active = objects[0]


def _export_glb(bpy: Any, path: pathlib.Path, objects: Sequence[Any]) -> None:
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
    )
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(f"Blender did not produce {path}")
    path.chmod(0o600)


def _geometry_signature(node: RenderNode) -> bytes:
    return canonical_json_bytes([vars(primitive) for primitive in node.primitives])


def _export_asset_artifacts(
    bpy: Any,
    output_root: pathlib.Path,
    plan: ContractScenePlan,
    node_objects: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    source_kind = {item["asset_id"]: item["source_kind"] for item in plan.house["asset_catalog"]}
    nodes_by_asset: dict[str, list[RenderNode]] = {}
    for node in plan.nodes:
        nodes_by_asset.setdefault(node.asset_ref, []).append(node)
    required = {asset_id for asset_id, kind in source_kind.items() if kind != "builtin"}
    if required != set(nodes_by_asset) - {asset_id for asset_id, kind in source_kind.items() if kind == "builtin"}:
        missing = sorted(required - set(nodes_by_asset))
        extra = sorted(set(nodes_by_asset) - set(source_kind))
        raise RuntimeError(f"asset binding is not total; missing={missing}, extra={extra}")

    artifacts_dir = output_root / "assets"
    artifacts_dir.mkdir(mode=0o700)
    receipts: dict[str, dict[str, Any]] = {}
    temp_collection = bpy.data.collections.new("VISTA_ArtifactExportTemp")
    bpy.context.scene.collection.children.link(temp_collection)
    for asset_id in sorted(required):
        candidates = sorted(nodes_by_asset[asset_id], key=lambda item: item.node_id)
        # Room bundles intentionally combine shell and ceiling.  Repeated
        # semantic assets (interior doors) must share one geometry signature.
        if any("nonsemantic_bundle" in node.tags for node in candidates):
            representatives = candidates
        else:
            signatures = {_geometry_signature(node) for node in candidates}
            if len(signatures) != 1:
                raise RuntimeError(f"shared asset_ref has divergent geometry: {asset_id}")
            representatives = candidates[:1]
        duplicates: list[Any] = []
        for node in representatives:
            source = node_objects[node.node_id]
            if source.type != "MESH":
                continue
            duplicate = source.copy()
            duplicate.data = source.data.copy()
            duplicate.parent = None
            duplicate.location = (0.0, 0.0, 0.0)
            duplicate.rotation_euler = (0.0, 0.0, 0.0)
            duplicate.scale = (1.0, 1.0, 1.0)
            duplicate.name = _safe_node_name("ArtifactPart", node.node_id)
            temp_collection.objects.link(duplicate)
            duplicates.append(duplicate)
        if not duplicates:
            raise RuntimeError(f"non-builtin asset has no mesh geometry: {asset_id}")
        if len(duplicates) > 1:
            _select(bpy, duplicates)
            bpy.context.view_layer.objects.active = duplicates[0]
            bpy.ops.object.join()
            artifact_object = bpy.context.active_object
        else:
            artifact_object = duplicates[0]
        artifact_object.name = _safe_node_name("Asset", asset_id)
        artifact_object.data.name = f"{artifact_object.name}_Mesh"
        artifact_object["asset_id"] = asset_id
        artifact_object["source_house_digest"] = plan.house["content_digest"]
        artifact_object["artifact_contract"] = "one_asset_one_mesh"
        path = artifacts_dir / _asset_filename(asset_id)
        _export_glb(bpy, path, [artifact_object])
        receipts[asset_id] = {
            "path": path.relative_to(output_root).as_posix(),
            "sha256": sha256_file(path),
            "bytes": path.stat().st_size,
            "media_type": "model/gltf-binary",
            "mesh_count": 1,
            "source_node_ids": [node.node_id for node in representatives],
        }
        bpy.data.objects.remove(artifact_object, do_unlink=True)
    bpy.data.collections.remove(temp_collection)
    return receipts


def _render(bpy: Any, camera: Any, path: pathlib.Path) -> None:
    bpy.context.scene.camera = camera
    bpy.context.scene.render.filepath = str(path)
    bpy.ops.render.render(write_still=True)
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(f"Blender did not produce preview {path}")
    path.chmod(0o600)


def _output_entry(path: pathlib.Path, media_type: str, *, relative_to: pathlib.Path) -> dict[str, Any]:
    return {
        "path": path.relative_to(relative_to).as_posix(),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
        "media_type": media_type,
    }


def build(house_path: pathlib.Path, output_root: pathlib.Path, seed: int | None = None) -> pathlib.Path:
    try:
        import bpy  # type: ignore[import-not-found]
        import mathutils  # type: ignore[import-not-found]
    except ModuleNotFoundError as error:
        raise RuntimeError("run this generator inside Blender") from error
    if tuple(bpy.app.version) != EXPECTED_BLENDER_VERSION:
        raise RuntimeError(
            f"pinned Blender {'.'.join(map(str, EXPECTED_BLENDER_VERSION))} is required; running {bpy.app.version_string}"
        )
    house = load_house(house_path)
    if seed is not None and seed != house["seed"]:
        raise RuntimeError(f"--seed {seed} does not match HouseSpec.seed {house['seed']}")
    output_root = prepare_output_root(output_root)
    plan = build_contract_plan(house)
    normalized = normalized_manifest(plan)
    normalized_path = output_root / OUTPUT_FILENAMES["normalized_manifest"]
    normalized_path.write_bytes(canonical_json_bytes(normalized))
    normalized_path.chmod(0o600)

    _configure_scene(bpy)
    materials = _make_materials(bpy, plan)
    export_objects, node_objects, _preview_hidden = _build_world(bpy, mathutils, plan, materials)
    cameras = _presentation(bpy, mathutils, plan)

    full_glb = output_root / OUTPUT_FILENAMES["glb"]
    _export_glb(bpy, full_glb, export_objects)
    artifacts = _export_asset_artifacts(bpy, output_root, plan, node_objects)

    _render(bpy, cameras["overview"], output_root / OUTPUT_FILENAMES["preview_overview"])
    _render(bpy, cameras["interior"], output_root / OUTPUT_FILENAMES["preview_interior"])

    blend_path = output_root / OUTPUT_FILENAMES["blend"]
    bpy.context.scene.camera = cameras["overview"]
    bpy.ops.wm.save_as_mainfile(filepath=str(blend_path), check_existing=False, compress=True)
    if not blend_path.is_file() or blend_path.stat().st_size == 0:
        raise RuntimeError(f"Blender did not save {blend_path}")
    blend_path.chmod(0o600)

    outputs = {
        key: _output_entry(output_root / filename, MEDIA_TYPES[key], relative_to=output_root)
        for key, filename in OUTPUT_FILENAMES.items()
        if key in MEDIA_TYPES
    }
    receipt = {
        "schema_version": "simworld.vista.playable-home-blender-build-receipt/v1",
        "house_id": house["house_id"],
        "revision": house["revision"],
        "source_house_digest": house["content_digest"],
        "normalized_manifest_digest": normalized["content_digest"],
        "build": {
            "seed": house["seed"],
            "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "blender_version": bpy.app.version_string,
        },
        "outputs": outputs,
        "asset_artifacts": artifacts,
    }
    receipt_path = output_root / OUTPUT_FILENAMES["manifest"]
    temporary = output_root / ".manifest.json.tmp"
    temporary.write_bytes(canonical_json_bytes(receipt))
    temporary.chmod(0o600)
    os.replace(temporary, receipt_path)
    print(
        canonical_json_bytes(
            {
                "status": "built",
                "manifest": str(receipt_path),
                "normalized_digest": normalized["content_digest"],
                "asset_artifact_count": len(artifacts),
            }
        ).decode("utf-8"),
        end="",
    )
    return receipt_path


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_blender_args(argv)
    build(args.house, args.output_root, args.seed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
