#!/usr/bin/env python3
"""Import selected HSSD GLBs, normalize them, and emit UE-ready visual GLBs.

Run through the pinned Blender binary::

    blender --background --factory-startup \
      --python tools/blender/vista_playable_home_hssd/build.py -- \
      --normalized-manifest /abs/normalized-manifest.json \
      --hssd-root /abs/hssd-hab --output-root /abs/empty-run \
      --license-accept CC-BY-NC-4.0
"""

from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import re
import sys
import tempfile
from typing import Any, Sequence


if __package__ in {None, ""}:
    package_root = pathlib.Path(__file__).resolve().parents[2]
    if str(package_root) not in sys.path:
        sys.path.insert(0, str(package_root))
    from blender.vista_playable_home_hssd.planner import (  # type: ignore[import-not-found]
        BUILT_MANIFEST_SCHEMA,
        HSSD_LICENSE_SPDX,
        HssdBindingError,
        _contained_file,
        _dataset_root,
        _fit_transform,
        build_binding_plan,
        canonical_json_bytes,
        inspect_glb,
        load_normalized_manifest,
        seal_document,
        sha256_file,
        validate_built_manifest,
    )
    from blender.vista_playable_home_hssd.glb_transport import (  # type: ignore[import-not-found]
        read_glb,
        rehydrate_basisu_materials,
        uses_required_basisu,
        write_blender_surrogate,
    )
else:
    from .planner import (
        BUILT_MANIFEST_SCHEMA,
        HSSD_LICENSE_SPDX,
        HssdBindingError,
        _contained_file,
        _dataset_root,
        _fit_transform,
        build_binding_plan,
        canonical_json_bytes,
        inspect_glb,
        load_normalized_manifest,
        seal_document,
        sha256_file,
        validate_built_manifest,
    )
    from .glb_transport import read_glb, rehydrate_basisu_materials, uses_required_basisu, write_blender_surrogate


EXPECTED_BLENDER_VERSION = (4, 5, 8)


def parse_blender_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    raw = list(sys.argv if argv is None else argv)
    forwarded = raw[raw.index("--") + 1 :] if "--" in raw else raw
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--normalized-manifest", required=True, type=pathlib.Path)
    parser.add_argument("--hssd-root", required=True, type=pathlib.Path)
    parser.add_argument("--output-root", required=True, type=pathlib.Path)
    parser.add_argument("--asset-id", action="append", dest="asset_ids")
    parser.add_argument("--license-accept", required=True, choices=[HSSD_LICENSE_SPDX])
    return parser.parse_args(forwarded)


def prepare_output_root(path: pathlib.Path) -> pathlib.Path:
    if not path.is_absolute():
        raise HssdBindingError("--output-root must be absolute")
    if path.is_symlink():
        raise HssdBindingError("--output-root may not be a symbolic link")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    resolved = path.resolve(strict=True)
    if any(resolved.iterdir()):
        raise HssdBindingError(f"refusing to write into non-empty append-only output root: {resolved}")
    return resolved


def _asset_filename(asset_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", asset_id) + ".glb"


def _reset_scene(bpy: Any) -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.unit_settings.system = "METRIC"
    scene.unit_settings.length_unit = "METERS"
    scene.unit_settings.scale_length = 1.0


def _select(bpy: Any, objects: Sequence[Any]) -> None:
    bpy.ops.object.select_all(action="DESELECT")
    for obj in objects:
        obj.hide_set(False)
        obj.hide_viewport = False
        obj.select_set(True)
    if objects:
        bpy.context.view_layer.objects.active = objects[0]


def _bounds(obj: Any) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    points = [obj.matrix_world @ obj.data.vertices[index].co for index in range(len(obj.data.vertices))]
    if not points:
        raise HssdBindingError("imported HSSD primary mesh has no vertices")
    minimum = tuple(min(point[axis] for point in points) for axis in range(3))
    maximum = tuple(max(point[axis] for point in points) for axis in range(3))
    return minimum, maximum


def _dimensions(bounds: tuple[Sequence[float], Sequence[float]]) -> tuple[float, float, float]:
    return tuple(float(bounds[1][axis] - bounds[0][axis]) for axis in range(3))  # type: ignore[return-value]


def _join_imported_meshes(bpy: Any) -> Any:
    meshes = sorted(
        (obj for obj in bpy.context.scene.objects if obj.type == "MESH" and obj.data is not None and len(obj.data.vertices) > 0),
        key=lambda obj: obj.name,
    )
    if not meshes:
        raise HssdBindingError("HSSD import produced no mesh")
    for obj in meshes:
        matrix = obj.matrix_world.copy()
        obj.parent = None
        obj.matrix_world = matrix
        _select(bpy, [obj])
        bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    _select(bpy, meshes)
    bpy.context.view_layer.objects.active = meshes[0]
    if len(meshes) > 1:
        bpy.ops.object.join()
    primary = bpy.context.view_layer.objects.active
    if primary is None or primary.type != "MESH":
        raise HssdBindingError("HSSD mesh join did not produce one primary mesh")
    return primary


def _normalize_primary(bpy: Any, mathutils: Any, primary: Any, target_dimensions: Sequence[float]) -> dict[str, Any]:
    target = tuple(float(value) for value in target_dimensions)
    source_bounds = _bounds(primary)
    source_dimensions = _dimensions(source_bounds)
    rotation, _planned_scales, _anisotropy, _uniform = _fit_transform(source_dimensions, target)
    primary.rotation_euler = (0.0, 0.0, math.radians(rotation))
    _select(bpy, [primary])
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    oriented_dimensions = _dimensions(_bounds(primary))
    scales = tuple(target[axis] / oriented_dimensions[axis] for axis in range(3))
    if any(not math.isfinite(value) or value <= 0 for value in scales):
        raise HssdBindingError("invalid HSSD normalization scale")
    primary.scale = scales
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    minimum, maximum = _bounds(primary)
    translation = mathutils.Vector((
        -(minimum[0] + maximum[0]) / 2,
        -(minimum[1] + maximum[1]) / 2,
        -minimum[2],
    ))
    primary.data.transform(mathutils.Matrix.Translation(translation))
    primary.data.update()
    final_bounds = _bounds(primary)
    actual_dimensions = _dimensions(final_bounds)
    if any(abs(actual_dimensions[axis] - target[axis]) > 0.0005 for axis in range(3)):
        raise HssdBindingError("normalized HSSD dimensions do not match target bounds")
    if any(abs(final_bounds[0][axis] - (-target[axis] / 2 if axis < 2 else 0.0)) > 0.0005 for axis in range(3)):
        raise HssdBindingError("normalized HSSD origin is not footprint-center/bottom-zero")
    return {
        "source_import_dimensions_m": list(source_dimensions),
        "rotate_z_deg": rotation,
        "scale_xyz": list(scales),
        "origin_policy": "footprint_center_bottom_z_zero",
        "actual_bounds_m": {"min_m": list(final_bounds[0]), "max_m": list(final_bounds[1])},
        "actual_dimensions_m": list(actual_dimensions),
    }


def _export_primary(bpy: Any, path: pathlib.Path, primary: Any) -> None:
    _select(bpy, [primary])
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
        raise HssdBindingError(f"Blender did not export {path.name}")
    path.chmod(0o600)


def _build_one(bpy: Any, mathutils: Any, root: pathlib.Path, output_dir: pathlib.Path, binding: dict[str, Any]) -> dict[str, Any]:
    _reset_scene(bpy)
    source = binding["source"]
    source_path = _contained_file(root, source["render_asset_relpath"], "selected HSSD render asset")
    if sha256_file(source_path) != source["render_asset_sha256"]:
        raise HssdBindingError(f"HSSD source hash drift: {binding['logical_asset_id']}")
    output_path = output_dir / _asset_filename(binding["logical_asset_id"])
    source_document, _source_binary = read_glb(source_path)
    basisu = uses_required_basisu(source_document)
    with tempfile.TemporaryDirectory(prefix="vista-hssd-import-") as temporary:
        temporary_root = pathlib.Path(temporary)
        import_path = source_path
        transport: dict[str, Any] = {"mode": "blender_native_texture_import"}
        if basisu:
            import_path = temporary_root / "surrogate.glb"
            transport = write_blender_surrogate(source_path, import_path)
        bpy.ops.import_scene.gltf(filepath=str(import_path), import_pack_images=True, merge_vertices=False)
        primary = _join_imported_meshes(bpy)
        primary.name = f"VISTA_HSSD_{_asset_filename(binding['logical_asset_id'])[:-4]}"[:63]
        primary.data.name = f"{primary.name}_Mesh"[:63]
        primary["logical_asset_id"] = binding["logical_asset_id"]
        primary["hssd_object_id"] = source["object_id"]
        primary["hssd_source_sha256"] = source["render_asset_sha256"]
        primary["hssd_license_spdx"] = HSSD_LICENSE_SPDX
        primary["artifact_contract"] = "one_logical_asset_one_primary_mesh"
        transform = _normalize_primary(bpy, mathutils, primary, binding["target_dimensions_m"])
        if basisu:
            normalized_surrogate = temporary_root / "normalized-surrogate.glb"
            _export_primary(bpy, normalized_surrogate, primary)
            transport.update(rehydrate_basisu_materials(source_path, normalized_surrogate, output_path))
        else:
            _export_primary(bpy, output_path, primary)
    inspection = inspect_glb(output_path.resolve())
    if inspection["mesh_count"] != 1:
        raise HssdBindingError(f"exported HSSD asset has {inspection['mesh_count']} meshes")
    if inspection["material_count"] < 1 or inspection["pbr_texture_slot_count"] < 1:
        raise HssdBindingError("exported HSSD asset lost its PBR material/texture slots")
    return {
        "logical_asset_id": binding["logical_asset_id"],
        "semantic_category": binding["semantic_category"],
        "path": output_path.relative_to(output_dir.parent).as_posix(),
        "sha256": sha256_file(output_path),
        "bytes": output_path.stat().st_size,
        "media_type": "model/gltf-binary",
        "target_dimensions_m": binding["target_dimensions_m"],
        "actual_dimensions_m": transform["actual_dimensions_m"],
        "normalization": transform,
        "texture_transport": transport["mode"],
        "texture_transport_receipt": transport,
        "source": {
            "dataset": source["dataset"],
            "object_id": source["object_id"],
            "render_asset_sha256": source["render_asset_sha256"],
            "license_spdx": source["license_spdx"],
            "license_url": source["license_url"],
        },
        "inspection": inspection,
    }


def build(
    normalized_manifest_path: pathlib.Path,
    hssd_root: pathlib.Path,
    output_root: pathlib.Path,
    requested_asset_ids: Sequence[str] | None = None,
) -> pathlib.Path:
    try:
        import bpy  # type: ignore[import-not-found]
        import mathutils  # type: ignore[import-not-found]
    except ModuleNotFoundError as error:
        raise RuntimeError("run this HSSD builder inside Blender") from error
    if tuple(bpy.app.version) != EXPECTED_BLENDER_VERSION:
        raise HssdBindingError(
            f"pinned Blender {'.'.join(map(str, EXPECTED_BLENDER_VERSION))} is required; running {bpy.app.version_string}"
        )
    normalized = load_normalized_manifest(normalized_manifest_path)
    root = _dataset_root(hssd_root)
    output_root = prepare_output_root(output_root)
    plan = build_binding_plan(normalized, root, requested_asset_ids=requested_asset_ids)
    plan_path = output_root / "binding-plan.json"
    plan_path.write_bytes(canonical_json_bytes(plan))
    plan_path.chmod(0o600)
    output_dir = output_root / "assets"
    output_dir.mkdir(mode=0o700)
    outputs = [_build_one(bpy, mathutils, root, output_dir, binding) for binding in plan["bindings"]]
    built = seal_document({
        "schema_version": BUILT_MANIFEST_SCHEMA,
        "house_id": plan["house_id"],
        "revision": plan["revision"],
        "source_plan": {"schema_version": plan["schema_version"], "content_digest": plan["content_digest"], "path": "binding-plan.json"},
        "dataset": plan["dataset"],
        "license_receipt": plan["license_receipt"],
        "blender": {"version": bpy.app.version_string, "mode": plan["mode"]},
        "closed_world": {
            "bound_asset_ids": sorted(entry["logical_asset_id"] for entry in outputs),
            "unaccounted_asset_ids": [],
        },
        "outputs": outputs,
    })
    validate_built_manifest(built)
    manifest_path = output_root / "binding-attribution-manifest.json"
    temporary = output_root / ".binding-attribution-manifest.json.tmp"
    temporary.write_bytes(canonical_json_bytes(built))
    temporary.chmod(0o600)
    os.replace(temporary, manifest_path)
    print(canonical_json_bytes({
        "status": "built",
        "manifest": str(manifest_path),
        "content_digest": built["content_digest"],
        "asset_count": len(outputs),
    }).decode("utf-8"), end="")
    return manifest_path


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_blender_args(argv)
    build(args.normalized_manifest, args.hssd_root, args.output_root, args.asset_ids)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
