#!/usr/bin/env python3
"""Build the deterministic VISTA mmg_040 furnished office kit.

Run through Blender, never the host Python interpreter:

    blender --background --factory-startup \
      --python tools/blender/build_vista_mmg040_office.py -- \
      --output-root /absolute/run/path/blender --seed 4040

The scene uses metres and Blender Z-up.  Component transforms are baked into
asset-local mesh vertices, so all components of one asset can be spawned at a
common UE actor transform.  The room, cabinet, and ergonomic-chair roots retain
their room-assembly origins in the glTF hierarchy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pathlib
import random
import sys
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence


SCHEMA = "simworld.vista.blender-asset-manifest/v1"
ASSET_ID = "vista_mmg040_office_r1"
CANONICAL_SEED = 4040
EXPECTED_BLENDER_VERSION = (4, 5, 8)
SCRIPT_REPOSITORY_PATH = "tools/blender/build_vista_mmg040_office.py"
ROOM_COLLECTION = "VISTA_RoomShell"
CABINET_COLLECTION = "VISTA_TallOfficeCabinet"
CHAIR_COLLECTION = "VISTA_ErgonomicOfficeChair"
PRESENTATION_COLLECTION = "VISTA_Presentation"
ROOM_ROOT_NAME = "VISTA_Room_Root"
CABINET_ROOT_NAME = "VISTA_Cabinet_Root"
CHAIR_ROOT_NAME = "VISTA_Chair_Root"
CABINET_ASSEMBLY_ORIGIN_M = (0.0, 2.14, 0.0)
CHAIR_ASSEMBLY_ORIGIN_M = (-0.40, -0.80, 0.0)
UE_ROOM_ORIGIN_CM = (300.0, 0.0, 0.0)
OUTPUT_FILENAMES = {
    "blend": "source.blend",
    "glb": "vista_mmg040_office.glb",
    "gltf": "vista_mmg040_office.gltf",
    "gltf_bin": "vista_mmg040_office.bin",
    "preview_overview": "preview-overview.png",
    "preview_detail": "preview-detail.png",
    "manifest": "manifest.json",
}
OUTPUT_MEDIA_TYPES = {
    "blend": "application/x-blender",
    "glb": "model/gltf-binary",
    "gltf": "model/gltf+json",
    "gltf_bin": "application/octet-stream",
    "preview_overview": "image/png",
    "preview_detail": "image/png",
}
PREVIEW_WIDTH = 720
PREVIEW_HEIGHT = 540


def blender_origin_to_ue_placement(origin_m: Sequence[float]) -> dict[str, list[float]]:
    """Map the exported Blender root to the mmg_040 UE composition frame.

    Blender's +Y architectural depth becomes UE +X after the glTF Interchange
    conversion, Blender +X becomes UE +Y, and +Z remains +Z.  The room is
    anchored at the established mmg_040 runtime-ground centre X=300 cm.
    """

    x, y, z = (float(value) for value in origin_m)
    return {
        "location_cm": [UE_ROOM_ORIGIN_CM[0] + y * 100.0, UE_ROOM_ORIGIN_CM[1] + x * 100.0, UE_ROOM_ORIGIN_CM[2] + z * 100.0],
        "rotation_deg": [0.0, 0.0, 0.0],
        "scale": [1.0, 1.0, 1.0],
    }


def derive_preview_camera_transforms(
    scene_bounds: dict[str, list[float]],
    detail_bounds: dict[str, list[float]],
    detail_origin_m: Sequence[float],
) -> dict[str, dict[str, Any]]:
    """Derive repeatable overview and furnished-asset detail framing."""

    scene_min = scene_bounds["min"]
    scene_max = scene_bounds["max"]
    scene_size = scene_bounds["dimensions"]
    span = max(scene_size)
    scene_center_x = (scene_min[0] + scene_max[0]) * 0.5
    overview = {
        "location": [scene_max[0] + span * 0.34, scene_min[1] - span * 0.38, scene_min[2] + scene_size[2] * 0.88],
        "target": [scene_center_x, scene_max[1] - scene_size[1] * 0.19, scene_min[2] + scene_size[2] * 0.42],
        "lens_mm": 40.0,
    }

    detail_min = detail_bounds["min"]
    detail_max = detail_bounds["max"]
    detail_size = detail_bounds["dimensions"]
    origin_x, origin_y, origin_z = (float(value) for value in detail_origin_m)
    detail_center_x = origin_x + (detail_min[0] + detail_max[0]) * 0.5
    detail_center_y = origin_y + (detail_min[1] + detail_max[1]) * 0.5
    detail_front_y = origin_y + detail_min[1]
    detail = {
        "location": [
            detail_center_x + detail_size[0] * 1.38,
            detail_front_y - detail_size[1] * 1.72,
            origin_z + detail_min[2] + detail_size[2] * 0.78,
        ],
        "target": [
            detail_center_x,
            detail_center_y,
            origin_z + detail_min[2] + detail_size[2] * 0.54,
        ],
        "lens_mm": 58.0,
    }

    def rounded(view: dict[str, Any]) -> dict[str, Any]:
        return {
            "location": [round(float(value), 6) for value in view["location"]],
            "target": [round(float(value), 6) for value in view["target"]],
            "lens_mm": float(view["lens_mm"]),
        }

    return {"overview": rounded(overview), "detail": rounded(detail)}


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def parse_blender_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    raw = list(sys.argv if argv is None else argv)
    forwarded = raw[raw.index("--") + 1 :] if "--" in raw else raw
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", required=True, type=pathlib.Path)
    parser.add_argument("--seed", type=int, default=CANONICAL_SEED)
    return parser.parse_args(forwarded)


def prepare_output_root(output_root: pathlib.Path, seed: int) -> pathlib.Path:
    if seed != CANONICAL_SEED:
        raise RuntimeError(f"The canonical VISTA mmg_040 seed is {CANONICAL_SEED}; received {seed}")
    if not output_root.is_absolute():
        raise RuntimeError("--output-root must be an absolute path")
    output_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if output_root.is_symlink():
        raise RuntimeError("--output-root may not be a symbolic link")
    output_root = output_root.resolve(strict=True)
    for filename in OUTPUT_FILENAMES.values():
        candidate = output_root / filename
        if candidate.exists() or candidate.is_symlink():
            raise RuntimeError(f"Refusing to replace append-only output: {candidate}")
    return output_root


class Forge:
    """Small deterministic Blender construction helper."""

    def __init__(self, bpy_module: Any, mathutils_module: Any, seed: int) -> None:
        self.bpy = bpy_module
        self.mathutils = mathutils_module
        self.random = random.Random(seed)

    def collection(self, name: str) -> Any:
        collection = self.bpy.data.collections.new(name)
        self.bpy.context.scene.collection.children.link(collection)
        return collection

    def move_to_collection(self, obj: Any, collection: Any) -> None:
        for linked in tuple(obj.users_collection):
            linked.objects.unlink(obj)
        collection.objects.link(obj)

    def empty(self, name: str, collection: Any, location: Sequence[float]) -> Any:
        obj = self.bpy.data.objects.new(name, None)
        obj.empty_display_type = "CUBE"
        obj.empty_display_size = 0.25
        obj.location = tuple(location)
        collection.objects.link(obj)
        return obj

    def material(
        self,
        name: str,
        color: Sequence[float],
        *,
        metallic: float = 0.0,
        roughness: float = 0.45,
    ) -> Any:
        material = self.bpy.data.materials.new(name)
        material.use_nodes = True
        material.diffuse_color = tuple(color)
        node = material.node_tree.nodes.get("Principled BSDF")
        if node is None:
            raise RuntimeError(f"Blender did not create a Principled BSDF for {name}")
        node.inputs["Base Color"].default_value = tuple(color)
        node.inputs["Metallic"].default_value = metallic
        node.inputs["Roughness"].default_value = roughness
        if node.inputs.get("IOR") is not None:
            node.inputs["IOR"].default_value = 1.46
        return material

    def _finalize_mesh(
        self,
        obj: Any,
        *,
        name: str,
        material: Any,
        collection: Any,
        root: Any,
        bevel: float,
        bevel_segments: int,
        smooth: bool,
    ) -> Any:
        obj.name = name
        obj.data.name = f"{name}_Mesh"
        self.bpy.context.view_layer.objects.active = obj
        obj.select_set(True)
        # Applying location, rotation and scale bakes component placement into
        # asset-local vertices.  Importers may therefore spawn every component
        # at one common transform without losing the authored assembly.
        self.bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
        if bevel > 0.0:
            modifier = obj.modifiers.new(name="VISTA_EdgeSoftening", type="BEVEL")
            modifier.width = bevel
            modifier.segments = bevel_segments
            modifier.limit_method = "ANGLE"
            modifier.angle_limit = math.radians(25.0)
            if hasattr(modifier, "harden_normals"):
                modifier.harden_normals = True
            self.bpy.ops.object.modifier_apply(modifier=modifier.name)
        if smooth:
            for polygon in obj.data.polygons:
                polygon.use_smooth = True
        obj.data.materials.append(material)
        self.move_to_collection(obj, collection)
        obj.parent = root
        obj.matrix_parent_inverse = self.mathutils.Matrix.Identity(4)
        obj.select_set(False)
        return obj

    def box(
        self,
        name: str,
        dimensions: Sequence[float],
        location: Sequence[float],
        material: Any,
        collection: Any,
        root: Any,
        *,
        rotation: Sequence[float] = (0.0, 0.0, 0.0),
        bevel: float = 0.006,
        bevel_segments: int = 3,
    ) -> Any:
        if any(float(value) <= 0.0 for value in dimensions):
            raise ValueError(f"Non-positive dimensions for {name}: {dimensions}")
        # Size the cube in its local axes before rotation.  Setting Blender's
        # world-aligned ``dimensions`` on an already rotated cube can inflate
        # thin chair members several-fold at oblique angles.
        self.bpy.ops.mesh.primitive_cube_add(size=1.0, location=tuple(location))
        obj = self.bpy.context.active_object
        obj.dimensions = tuple(dimensions)
        obj.rotation_euler = tuple(rotation)
        return self._finalize_mesh(
            obj,
            name=name,
            material=material,
            collection=collection,
            root=root,
            bevel=min(bevel, min(dimensions) * 0.45),
            bevel_segments=bevel_segments,
            smooth=False,
        )

    def cylinder(
        self,
        name: str,
        radius: float,
        depth: float,
        location: Sequence[float],
        material: Any,
        collection: Any,
        root: Any,
        *,
        rotation: Sequence[float] = (0.0, 0.0, 0.0),
        vertices: int = 32,
        bevel: float = 0.003,
    ) -> Any:
        self.bpy.ops.mesh.primitive_cylinder_add(
            vertices=vertices,
            radius=radius,
            depth=depth,
            end_fill_type="NGON",
            location=tuple(location),
            rotation=tuple(rotation),
        )
        obj = self.bpy.context.active_object
        return self._finalize_mesh(
            obj,
            name=name,
            material=material,
            collection=collection,
            root=root,
            bevel=min(bevel, radius * 0.4, depth * 0.2),
            bevel_segments=2,
            smooth=True,
        )

    def sphere(
        self,
        name: str,
        radius: float,
        location: Sequence[float],
        material: Any,
        collection: Any,
        root: Any,
    ) -> Any:
        self.bpy.ops.mesh.primitive_uv_sphere_add(segments=24, ring_count=12, radius=radius, location=tuple(location))
        obj = self.bpy.context.active_object
        return self._finalize_mesh(
            obj,
            name=name,
            material=material,
            collection=collection,
            root=root,
            bevel=0.0,
            bevel_segments=1,
            smooth=True,
        )

    def camera(self, name: str, location: Sequence[float], target: Sequence[float], collection: Any, lens: float) -> Any:
        data = self.bpy.data.cameras.new(f"{name}_Data")
        data.lens = lens
        data.sensor_width = 36.0
        obj = self.bpy.data.objects.new(name, data)
        collection.objects.link(obj)
        obj.location = tuple(location)
        self.point_at(obj, target)
        return obj

    def light(
        self,
        name: str,
        light_type: str,
        location: Sequence[float],
        target: Sequence[float],
        collection: Any,
        *,
        energy: float,
        color: Sequence[float],
        size: float = 1.0,
    ) -> Any:
        data = self.bpy.data.lights.new(f"{name}_Data", type=light_type)
        data.energy = energy
        data.color = tuple(color)
        if light_type == "AREA":
            data.shape = "DISK"
            data.size = size
        obj = self.bpy.data.objects.new(name, data)
        collection.objects.link(obj)
        obj.location = tuple(location)
        self.point_at(obj, target)
        return obj

    def point_at(self, obj: Any, target: Sequence[float]) -> None:
        direction = self.mathutils.Vector(target) - obj.location
        obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()


def configure_scene(bpy: Any) -> None:
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
    scene.render.film_transparent = False
    scene.render.use_file_extension = True
    scene.render.image_settings.compression = 35
    if hasattr(scene, "render") and hasattr(scene.render, "use_file_extension"):
        scene.render.use_file_extension = True
    try:
        scene.view_settings.look = "AgX - Medium High Contrast"
    except TypeError:
        try:
            scene.view_settings.look = "Medium High Contrast"
        except TypeError:
            pass
    scene.world.color = (0.025, 0.03, 0.04)
    if scene.world.use_nodes:
        background = scene.world.node_tree.nodes.get("Background")
        if background is not None:
            background.inputs["Color"].default_value = (0.018, 0.024, 0.032, 1.0)
            background.inputs["Strength"].default_value = 0.22


def build_room_shell(forge: Forge, collection: Any, root: Any, materials: dict[str, Any]) -> None:
    box = forge.box
    # Architectural shell: 5.2 m x 5.0 m, 2.76 m clear height.
    box("VISTA_Room_FloorSlab", (5.2, 5.0, 0.08), (0.0, 0.0, -0.04), materials["floor"], collection, root, bevel=0.004)
    box("VISTA_Room_BackWall", (5.2, 0.10, 2.76), (0.0, 2.50, 1.38), materials["wall"], collection, root, bevel=0.002)
    box("VISTA_Room_LeftWall", (0.10, 5.0, 2.76), (-2.60, 0.0, 1.38), materials["wall"], collection, root, bevel=0.002)
    box("VISTA_Room_BackSkirting", (5.08, 0.035, 0.14), (0.0, 2.435, 0.07), materials["skirting"], collection, root, bevel=0.004)
    box("VISTA_Room_LeftSkirting", (0.035, 4.86, 0.14), (-2.535, 0.0, 0.07), materials["skirting"], collection, root, bevel=0.004)

    # Recessed floor seams catch highlights and make scale legible without a
    # texture dependency.  They are geometry, not a fallback primitive floor.
    for index, x in enumerate((-1.95, -1.30, -0.65, 0.0, 0.65, 1.30, 1.95), start=1):
        box(f"VISTA_Room_FloorSeamX_{index:02d}", (0.006, 4.86, 0.003), (x, 0.0, 0.002), materials["seam"], collection, root, bevel=0.0)
    for index, y in enumerate((-1.85, -1.20, -0.55, 0.10, 0.75, 1.40, 2.05), start=1):
        box(f"VISTA_Room_FloorSeamY_{index:02d}", (5.08, 0.006, 0.003), (0.0, y, 0.002), materials["seam"], collection, root, bevel=0.0)

    # Muted acoustic wall panels and cable infrastructure give the room an
    # occupied institutional-office character suitable for mmg_040.
    for index, x in enumerate((-1.85, -1.22, -0.59), start=1):
        box(f"VISTA_Room_AcousticPanel_{index:02d}", (0.52, 0.025, 0.92), (x, 2.425, 1.65), materials["acoustic"], collection, root, bevel=0.018, bevel_segments=4)
        box(f"VISTA_Room_AcousticPanelInset_{index:02d}", (0.42, 0.012, 0.78), (x, 2.407, 1.65), materials["acoustic_inset"], collection, root, bevel=0.012, bevel_segments=3)
    box("VISTA_Room_CableTrunk_Back", (5.02, 0.055, 0.095), (0.0, 2.402, 0.55), materials["utility"], collection, root, bevel=0.008)
    for index, x in enumerate((-1.55, -0.55, 1.65), start=1):
        box(f"VISTA_Room_OutletPlate_{index:02d}", (0.13, 0.018, 0.085), (x, 2.363, 0.55), materials["outlet"], collection, root, bevel=0.006)
        for socket_index, offset in enumerate((-0.032, 0.032), start=1):
            forge.cylinder(
                f"VISTA_Room_OutletSocket_{index:02d}_{socket_index:02d}",
                0.009,
                0.009,
                (x + offset, 2.349, 0.55),
                materials["socket"],
                collection,
                root,
                rotation=(math.radians(90.0), 0.0, 0.0),
                vertices=20,
                bevel=0.001,
            )

    # Suspended acoustic ceiling with a real grid rather than one flat slab.
    tile_x = (-2.04, -1.02, 0.0, 1.02, 2.04)
    tile_y = (-2.0, -1.0, 0.0, 1.0, 2.0)
    for row, y in enumerate(tile_y, start=1):
        for column, x in enumerate(tile_x, start=1):
            box(
                f"VISTA_Room_CeilingTile_{row:02d}_{column:02d}",
                (0.96, 0.94, 0.025),
                (x, y, 2.72),
                materials["ceiling"],
                collection,
                root,
                bevel=0.009,
                bevel_segments=2,
            )
    for index, x in enumerate((-2.55, -1.53, -0.51, 0.51, 1.53, 2.55), start=1):
        box(f"VISTA_Room_CeilingGridX_{index:02d}", (0.018, 5.0, 0.035), (x, 0.0, 2.735), materials["ceiling_grid"], collection, root, bevel=0.002)
    for index, y in enumerate((-2.47, -1.5, -0.5, 0.5, 1.5, 2.47), start=1):
        box(f"VISTA_Room_CeilingGridY_{index:02d}", (5.1, 0.018, 0.035), (0.0, y, 2.735), materials["ceiling_grid"], collection, root, bevel=0.002)

    # Two inset LED panels with a rim; they remain ordinary emissive-looking
    # geometry in the production export, not glTF light extensions.
    for index, x in enumerate((-0.85, 1.25), start=1):
        box(f"VISTA_Room_LEDPanelRim_{index:02d}", (0.72, 1.0, 0.045), (x, -0.50, 2.705), materials["ceiling_grid"], collection, root, bevel=0.015)
        box(f"VISTA_Room_LEDPanelDiffuser_{index:02d}", (0.65, 0.92, 0.012), (x, -0.50, 2.675), materials["light_diffuser"], collection, root, bevel=0.012)


def build_cabinet(forge: Forge, collection: Any, root: Any, materials: dict[str, Any]) -> None:
    box = forge.box
    # 1.22 W x 0.50 D x 2.18 H metre powder-coated storage cabinet.
    box("VISTA_Cabinet_LeftSide", (0.036, 0.50, 2.16), (-0.592, 0.0, 1.20), materials["cabinet"], collection, root, bevel=0.009, bevel_segments=4)
    box("VISTA_Cabinet_RightSide", (0.036, 0.50, 2.16), (0.592, 0.0, 1.20), materials["cabinet"], collection, root, bevel=0.009, bevel_segments=4)
    box("VISTA_Cabinet_Top", (1.184, 0.50, 0.036), (0.0, 0.0, 2.262), materials["cabinet"], collection, root, bevel=0.009, bevel_segments=4)
    box("VISTA_Cabinet_Bottom", (1.184, 0.50, 0.036), (0.0, 0.0, 0.118), materials["cabinet"], collection, root, bevel=0.009, bevel_segments=4)
    box("VISTA_Cabinet_Back", (1.148, 0.018, 2.09), (0.0, 0.241, 1.20), materials["cabinet_interior"], collection, root, bevel=0.003)
    box("VISTA_Cabinet_CentreStile", (0.026, 0.46, 2.04), (0.0, 0.0, 1.20), materials["cabinet_interior"], collection, root, bevel=0.004)
    box("VISTA_Cabinet_TopCrown", (1.25, 0.53, 0.055), (0.0, 0.0, 2.315), materials["cabinet_edge"], collection, root, bevel=0.012, bevel_segments=4)

    for index, z in enumerate((0.50, 0.91, 1.32, 1.73), start=1):
        box(f"VISTA_Cabinet_Shelf_{index:02d}", (1.13, 0.44, 0.028), (0.0, 0.01, z), materials["cabinet_interior"], collection, root, bevel=0.006)
        for side, x in (("L", -0.566), ("R", 0.566)):
            forge.cylinder(
                f"VISTA_Cabinet_ShelfPin_{index:02d}_{side}",
                0.006,
                0.025,
                (x, 0.05, z - 0.02),
                materials["steel"],
                collection,
                root,
                rotation=(0.0, math.radians(90.0), 0.0),
                vertices=16,
                bevel=0.001,
            )

    # Adjustable shelf hole rails are a high-frequency detail visible through
    # the door reveal and in the close-up preview.
    for side, x in (("L", -0.557), ("R", 0.557)):
        for index, z in enumerate((0.32, 0.43, 0.54, 0.65, 0.76, 0.87, 0.98, 1.09, 1.20, 1.31, 1.42, 1.53, 1.64, 1.75, 1.86, 1.97), start=1):
            forge.cylinder(
                f"VISTA_Cabinet_AdjustHole_{side}_{index:02d}",
                0.0045,
                0.012,
                (x, -0.235, z),
                materials["shadow"],
                collection,
                root,
                rotation=(math.radians(90.0), 0.0, 0.0),
                vertices=12,
                bevel=0.0005,
            )

    door_centres = (("Left", -0.296), ("Right", 0.296))
    for side, x in door_centres:
        box(f"VISTA_Cabinet_{side}DoorCore", (0.575, 0.028, 2.05), (x, -0.274, 1.20), materials["cabinet"], collection, root, bevel=0.010, bevel_segments=4)
        box(f"VISTA_Cabinet_{side}DoorInset", (0.438, 0.014, 1.64), (x, -0.298, 1.23), materials["cabinet_inset"], collection, root, bevel=0.018, bevel_segments=4)
        for rail, z in (("Top", 2.135), ("Bottom", 0.265)):
            box(f"VISTA_Cabinet_{side}DoorFrame{rail}", (0.51, 0.022, 0.085), (x, -0.316, z), materials["cabinet_edge"], collection, root, bevel=0.012, bevel_segments=3)
        for rail, rail_x in (("Outer", x - 0.245), ("Inner", x + 0.245)):
            box(f"VISTA_Cabinet_{side}DoorFrame{rail}", (0.075, 0.022, 1.78), (rail_x, -0.316, 1.20), materials["cabinet_edge"], collection, root, bevel=0.012, bevel_segments=3)

        # Label holder and paper card.
        box(f"VISTA_Cabinet_{side}LabelHolder", (0.22, 0.020, 0.105), (x, -0.340, 1.835), materials["steel"], collection, root, bevel=0.009, bevel_segments=3)
        box(f"VISTA_Cabinet_{side}LabelCard", (0.184, 0.008, 0.069), (x, -0.355, 1.835), materials["label"], collection, root, bevel=0.004)
        box(f"VISTA_Cabinet_{side}LabelBand", (0.135, 0.004, 0.010), (x - 0.012, -0.361, 1.835), materials["label_ink"], collection, root, bevel=0.001)

        # Vent banks on each lower door.
        for vent_index, z in enumerate((0.37, 0.415, 0.46, 0.505, 0.55), start=1):
            box(f"VISTA_Cabinet_{side}Vent_{vent_index:02d}", (0.24, 0.014, 0.014), (x, -0.337, z), materials["shadow"], collection, root, bevel=0.004)

    # Hinges, twin handles and lock hardware.
    for side, x in (("L", -0.584), ("R", 0.584)):
        for index, z in enumerate((0.48, 1.20, 1.92), start=1):
            forge.cylinder(f"VISTA_Cabinet_Hinge_{side}_{index:02d}", 0.016, 0.145, (x, -0.313, z), materials["steel"], collection, root, vertices=28, bevel=0.003)
            for cap, cap_z in (("A", z - 0.079), ("B", z + 0.079)):
                forge.sphere(f"VISTA_Cabinet_HingeCap_{side}_{index:02d}_{cap}", 0.017, (x, -0.313, cap_z), materials["steel"], collection, root)

    for side, x in (("Left", -0.062), ("Right", 0.062)):
        forge.cylinder(f"VISTA_Cabinet_{side}Handle", 0.014, 0.44, (x, -0.365, 1.24), materials["handle"], collection, root, vertices=32, bevel=0.003)
        for index, z in enumerate((1.045, 1.435), start=1):
            forge.cylinder(
                f"VISTA_Cabinet_{side}HandleBracket_{index:02d}",
                0.021,
                0.095,
                (x, -0.326, z),
                materials["handle"],
                collection,
                root,
                rotation=(math.radians(90.0), 0.0, 0.0),
                vertices=28,
                bevel=0.004,
            )
    forge.cylinder(
        "VISTA_Cabinet_CamLock",
        0.030,
        0.032,
        (0.18, -0.324, 1.63),
        materials["handle"],
        collection,
        root,
        rotation=(math.radians(90.0), 0.0, 0.0),
        vertices=32,
        bevel=0.004,
    )
    box("VISTA_Cabinet_CamLockSlot", (0.008, 0.006, 0.031), (0.18, -0.344, 1.63), materials["shadow"], collection, root, bevel=0.002)

    for corner, x in (("L", -0.51), ("R", 0.51)):
        forge.cylinder(f"VISTA_Cabinet_Foot_{corner}", 0.048, 0.09, (x, 0.0, 0.055), materials["rubber"], collection, root, vertices=32, bevel=0.006)
        forge.cylinder(f"VISTA_Cabinet_FootPad_{corner}", 0.058, 0.018, (x, 0.0, 0.009), materials["rubber"], collection, root, vertices=32, bevel=0.004)

    # Deterministic, restrained edge wear.  Small warm-metal flecks prevent the
    # broad powder-coated panels from reading as untouched synthetic boxes.
    for index in range(12):
        x = forge.random.uniform(-0.53, 0.53)
        z = forge.random.uniform(0.17, 0.34)
        width = forge.random.uniform(0.010, 0.028)
        box(
            f"VISTA_Cabinet_WearFleck_{index + 1:02d}",
            (width, 0.0035, forge.random.uniform(0.004, 0.010)),
            (x, -0.344, z),
            materials["wear"],
            collection,
            root,
            rotation=(0.0, forge.random.uniform(-0.08, 0.08), forge.random.uniform(-0.25, 0.25)),
            bevel=0.001,
            bevel_segments=1,
        )


def build_ergonomic_chair(forge: Forge, collection: Any, root: Any, materials: dict[str, Any]) -> None:
    """Build a production-shaped task chair from deterministic hard surfaces.

    The chair is intentionally decomposed into semantic components so UE can
    retain useful names after Interchange import.  Its 0.86 m footprint stays
    more than 0.8 m from the canonical PlayerStart at Blender XY (-1.5, -1.5).
    """

    box = forge.box

    # Five-star base with offset twin-wheel swivel casters.  Each spoke is a
    # bevelled structural member instead of a flat radial decal.
    forge.cylinder(
        "VISTA_Chair_BaseHub",
        0.105,
        0.074,
        (0.0, 0.0, 0.132),
        materials["chair_frame"],
        collection,
        root,
        vertices=48,
        bevel=0.010,
    )
    forge.cylinder(
        "VISTA_Chair_BaseHubCollar",
        0.078,
        0.052,
        (0.0, 0.0, 0.183),
        materials["chair_metal"],
        collection,
        root,
        vertices=48,
        bevel=0.008,
    )
    for index in range(5):
        angle = math.radians(18.0 + index * 72.0)
        radial = (math.cos(angle), math.sin(angle))
        tangent = (-math.sin(angle), math.cos(angle))
        spoke_center = (radial[0] * 0.205, radial[1] * 0.205, 0.112)
        box(
            f"VISTA_Chair_BaseSpoke_{index + 1:02d}",
            (0.335, 0.064, 0.052),
            spoke_center,
            materials["chair_frame"],
            collection,
            root,
            rotation=(0.0, math.radians(-3.5), angle),
            bevel=0.021,
            bevel_segments=5,
        )
        caster_xy = (radial[0] * 0.392, radial[1] * 0.392)
        forge.cylinder(
            f"VISTA_Chair_CasterSwivel_{index + 1:02d}",
            0.027,
            0.090,
            (caster_xy[0], caster_xy[1], 0.105),
            materials["chair_metal"],
            collection,
            root,
            vertices=28,
            bevel=0.004,
        )
        box(
            f"VISTA_Chair_CasterYoke_{index + 1:02d}",
            (0.082, 0.052, 0.040),
            (caster_xy[0], caster_xy[1], 0.068),
            materials["chair_frame"],
            collection,
            root,
            rotation=(0.0, 0.0, angle),
            bevel=0.012,
            bevel_segments=4,
        )
        for side_index, side in enumerate((-1.0, 1.0), start=1):
            wheel_location = (
                caster_xy[0] + tangent[0] * 0.035 * side,
                caster_xy[1] + tangent[1] * 0.035 * side,
                0.048,
            )
            forge.cylinder(
                f"VISTA_Chair_CasterWheel_{index + 1:02d}_{side_index:02d}",
                0.044,
                0.027,
                wheel_location,
                materials["chair_tire"],
                collection,
                root,
                rotation=(math.radians(90.0), 0.0, angle),
                vertices=32,
                bevel=0.006,
            )
            forge.cylinder(
                f"VISTA_Chair_CasterWheelHub_{index + 1:02d}_{side_index:02d}",
                0.015,
                0.031,
                wheel_location,
                materials["chair_chrome"],
                collection,
                root,
                rotation=(math.radians(90.0), 0.0, angle),
                vertices=24,
                bevel=0.003,
            )

    # Gas lift and tilt mechanism use separate sleeves, plates and controls so
    # highlights convey the layered construction at close range.
    forge.cylinder(
        "VISTA_Chair_GasLiftChrome",
        0.031,
        0.300,
        (0.0, 0.0, 0.320),
        materials["chair_chrome"],
        collection,
        root,
        vertices=48,
        bevel=0.006,
    )
    forge.cylinder(
        "VISTA_Chair_GasLiftSleeve",
        0.052,
        0.215,
        (0.0, 0.0, 0.285),
        materials["chair_frame"],
        collection,
        root,
        vertices=48,
        bevel=0.008,
    )
    forge.cylinder(
        "VISTA_Chair_GasLiftBoot",
        0.071,
        0.075,
        (0.0, 0.0, 0.210),
        materials["chair_frame"],
        collection,
        root,
        vertices=48,
        bevel=0.010,
    )
    box(
        "VISTA_Chair_TiltMechanism",
        (0.300, 0.265, 0.072),
        (0.0, 0.020, 0.472),
        materials["chair_metal"],
        collection,
        root,
        bevel=0.022,
        bevel_segments=5,
    )
    box(
        "VISTA_Chair_TiltTopPlate",
        (0.380, 0.330, 0.032),
        (0.0, 0.005, 0.517),
        materials["chair_metal"],
        collection,
        root,
        bevel=0.010,
        bevel_segments=4,
    )
    forge.cylinder(
        "VISTA_Chair_TiltSpring",
        0.053,
        0.125,
        (0.0, 0.112, 0.456),
        materials["chair_frame"],
        collection,
        root,
        rotation=(math.radians(90.0), 0.0, 0.0),
        vertices=40,
        bevel=0.007,
    )
    forge.cylinder(
        "VISTA_Chair_HeightLever",
        0.011,
        0.300,
        (0.225, -0.035, 0.485),
        materials["chair_chrome"],
        collection,
        root,
        rotation=(0.0, math.radians(90.0), 0.0),
        vertices=24,
        bevel=0.002,
    )
    forge.sphere(
        "VISTA_Chair_HeightLeverGrip",
        0.032,
        (0.390, -0.035, 0.485),
        materials["chair_frame"],
        collection,
        root,
    )

    # Layered seat: rigid pan, soft waterfall cushion, perimeter piping and
    # subtle front contour all remain geometry rather than bitmap normal maps.
    box(
        "VISTA_Chair_SeatPan",
        (0.485, 0.455, 0.045),
        (0.0, -0.015, 0.535),
        materials["chair_frame"],
        collection,
        root,
        bevel=0.026,
        bevel_segments=6,
    )
    box(
        "VISTA_Chair_SeatCushion",
        (0.510, 0.475, 0.105),
        (0.0, -0.030, 0.598),
        materials["chair_fabric"],
        collection,
        root,
        rotation=(math.radians(-1.5), 0.0, 0.0),
        bevel=0.046,
        bevel_segments=7,
    )
    box(
        "VISTA_Chair_SeatFrontWaterfall",
        (0.455, 0.074, 0.072),
        (0.0, -0.256, 0.574),
        materials["chair_fabric_edge"],
        collection,
        root,
        rotation=(math.radians(8.0), 0.0, 0.0),
        bevel=0.031,
        bevel_segments=6,
    )
    for side, x in (("Left", -0.247), ("Right", 0.247)):
        box(
            f"VISTA_Chair_SeatPiping_{side}",
            (0.012, 0.392, 0.014),
            (x, -0.018, 0.636),
            materials["chair_stitch"],
            collection,
            root,
            bevel=0.006,
            bevel_segments=3,
        )
    box(
        "VISTA_Chair_SeatPipingFront",
        (0.455, 0.012, 0.014),
        (0.0, -0.252, 0.617),
        materials["chair_stitch"],
        collection,
        root,
        bevel=0.006,
        bevel_segments=3,
    )

    # Reclining spine, frame, segmented mesh upholstery and physical weave.
    box(
        "VISTA_Chair_BackSpine",
        (0.082, 0.070, 0.380),
        (0.0, 0.224, 0.705),
        materials["chair_frame"],
        collection,
        root,
        rotation=(math.radians(-8.0), 0.0, 0.0),
        bevel=0.022,
        bevel_segments=5,
    )
    box(
        "VISTA_Chair_BackLowerCrossbar",
        (0.430, 0.052, 0.048),
        (0.0, 0.235, 0.730),
        materials["chair_frame"],
        collection,
        root,
        bevel=0.018,
        bevel_segments=5,
    )
    box(
        "VISTA_Chair_BackTopCrossbar",
        (0.405, 0.052, 0.046),
        (0.0, 0.292, 1.212),
        materials["chair_frame"],
        collection,
        root,
        bevel=0.018,
        bevel_segments=5,
    )
    for side, x in (("Left", -0.218), ("Right", 0.218)):
        box(
            f"VISTA_Chair_BackFrame_{side}",
            (0.044, 0.055, 0.500),
            (x, 0.260, 0.965),
            materials["chair_frame"],
            collection,
            root,
            rotation=(math.radians(-6.5), 0.0, 0.0),
            bevel=0.018,
            bevel_segments=5,
        )
    for index, (z, y, width, height, tilt) in enumerate(
        (
            (0.810, 0.232, 0.395, 0.155, -3.0),
            (0.965, 0.253, 0.405, 0.155, -6.0),
            (1.120, 0.278, 0.378, 0.155, -8.0),
        ),
        start=1,
    ):
        box(
            f"VISTA_Chair_BackMeshPanel_{index:02d}",
            (width, 0.018, height),
            (0.0, y, z),
            materials["chair_mesh"],
            collection,
            root,
            rotation=(math.radians(tilt), 0.0, 0.0),
            bevel=0.018,
            bevel_segments=4,
        )
    for index, z in enumerate((0.765, 0.815, 0.865, 0.915, 0.965, 1.015, 1.065, 1.115, 1.165), start=1):
        progress = (z - 0.765) / 0.4
        box(
            f"VISTA_Chair_BackWeaveHorizontal_{index:02d}",
            (0.390 - abs(progress - 0.5) * 0.035, 0.009, 0.007),
            (0.0, 0.218 + progress * 0.055, z),
            materials["chair_weave"],
            collection,
            root,
            rotation=(math.radians(-6.0), 0.0, 0.0),
            bevel=0.003,
            bevel_segments=2,
        )
    for index, x in enumerate((-0.156, -0.104, -0.052, 0.0, 0.052, 0.104, 0.156), start=1):
        box(
            f"VISTA_Chair_BackWeaveVertical_{index:02d}",
            (0.006, 0.009, 0.405),
            (x, 0.247, 0.965),
            materials["chair_weave"],
            collection,
            root,
            rotation=(math.radians(-6.0), 0.0, 0.0),
            bevel=0.002,
            bevel_segments=2,
        )
    box(
        "VISTA_Chair_LumbarSupport",
        (0.315, 0.052, 0.082),
        (0.0, 0.205, 0.820),
        materials["chair_fabric_edge"],
        collection,
        root,
        rotation=(math.radians(-4.0), 0.0, 0.0),
        bevel=0.028,
        bevel_segments=6,
    )

    # Adjustable arm towers, metal brackets and soft caps.
    for side, x, outward in (("Left", -0.305, -1.0), ("Right", 0.305, 1.0)):
        box(
            f"VISTA_Chair_ArmBracket_{side}",
            (0.042, 0.062, 0.235),
            (x, 0.018, 0.694),
            materials["chair_metal"],
            collection,
            root,
            bevel=0.012,
            bevel_segments=4,
        )
        box(
            f"VISTA_Chair_ArmCantilever_{side}",
            (0.105, 0.055, 0.045),
            (x - outward * 0.034, -0.018, 0.785),
            materials["chair_metal"],
            collection,
            root,
            bevel=0.014,
            bevel_segments=4,
        )
        box(
            f"VISTA_Chair_ArmPad_{side}",
            (0.092, 0.275, 0.052),
            (x, -0.045, 0.823),
            materials["chair_arm_pad"],
            collection,
            root,
            rotation=(math.radians(-2.0), 0.0, 0.0),
            bevel=0.022,
            bevel_segments=6,
        )
        forge.cylinder(
            f"VISTA_Chair_ArmAdjustmentButton_{side}",
            0.013,
            0.012,
            (x + outward * 0.027, -0.045, 0.742),
            materials["chair_chrome"],
            collection,
            root,
            rotation=(0.0, math.radians(90.0), 0.0),
            vertices=24,
            bevel=0.002,
        )
        for screw_index, y in enumerate((-0.115, 0.035), start=1):
            forge.cylinder(
                f"VISTA_Chair_ArmPadScrew_{side}_{screw_index:02d}",
                0.007,
                0.006,
                (x, y, 0.797),
                materials["chair_chrome"],
                collection,
                root,
                vertices=16,
                bevel=0.001,
            )


def mesh_objects(collection: Any) -> list[Any]:
    return sorted((obj for obj in collection.all_objects if obj.type == "MESH"), key=lambda item: item.name)


def collection_statistics(collection: Any, mathutils: Any, *, relative_origin: Sequence[float]) -> dict[str, Any]:
    objects = mesh_objects(collection)
    if not objects:
        raise RuntimeError(f"Collection {collection.name} contains no meshes")
    origin = mathutils.Vector(relative_origin)
    minimum = mathutils.Vector((math.inf, math.inf, math.inf))
    maximum = mathutils.Vector((-math.inf, -math.inf, -math.inf))
    materials: set[str] = set()
    triangles = 0
    mesh_names: list[str] = []
    for obj in objects:
        mesh_names.append(obj.data.name)
        obj.data.calc_loop_triangles()
        triangles += len(obj.data.loop_triangles)
        materials.update(slot.material.name for slot in obj.material_slots if slot.material is not None)
        for corner in obj.bound_box:
            world = obj.matrix_world @ mathutils.Vector(corner)
            local = world - origin
            for axis in range(3):
                minimum[axis] = min(minimum[axis], local[axis])
                maximum[axis] = max(maximum[axis], local[axis])
    dimensions = maximum - minimum
    if any(not math.isfinite(value) or value <= 0.0 for value in dimensions):
        raise RuntimeError(f"Collection {collection.name} has invalid bounds")
    round_vector = lambda vector: [round(float(value), 6) for value in vector]
    return {
        "bounds": {
            "min": round_vector(minimum),
            "max": round_vector(maximum),
            "dimensions": round_vector(dimensions),
        },
        "mesh_count": len(objects),
        "material_count": len(materials),
        "triangle_count": triangles,
        "mesh_names": mesh_names,
        "material_names": sorted(materials),
    }


def combined_statistics(collections: Iterable[Any], mathutils: Any) -> dict[str, Any]:
    objects = sorted(
        (obj for collection in collections for obj in collection.all_objects if obj.type == "MESH"),
        key=lambda item: item.name,
    )
    minimum = mathutils.Vector((math.inf, math.inf, math.inf))
    maximum = mathutils.Vector((-math.inf, -math.inf, -math.inf))
    materials: set[str] = set()
    triangles = 0
    mesh_names: list[str] = []
    for obj in objects:
        mesh_names.append(obj.data.name)
        obj.data.calc_loop_triangles()
        triangles += len(obj.data.loop_triangles)
        materials.update(slot.material.name for slot in obj.material_slots if slot.material is not None)
        for corner in obj.bound_box:
            world = obj.matrix_world @ mathutils.Vector(corner)
            for axis in range(3):
                minimum[axis] = min(minimum[axis], world[axis])
                maximum[axis] = max(maximum[axis], world[axis])
    dimensions = maximum - minimum
    if len(mesh_names) != len(set(mesh_names)):
        raise RuntimeError("Generated mesh names are not unique")
    if any(not math.isfinite(value) or value <= 0.0 for value in dimensions):
        raise RuntimeError("Combined scene bounds are invalid")
    round_vector = lambda vector: [round(float(value), 6) for value in vector]
    return {
        "bounds": {"min": round_vector(minimum), "max": round_vector(maximum), "dimensions": round_vector(dimensions)},
        "mesh_count": len(mesh_names),
        "material_count": len(materials),
        "triangle_count": triangles,
        "mesh_names": mesh_names,
        "material_names": sorted(materials),
    }


def render_preview(bpy: Any, camera: Any, path: pathlib.Path) -> None:
    scene = bpy.context.scene
    scene.camera = camera
    scene.render.filepath = str(path)
    bpy.ops.render.render(write_still=True)
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(f"Blender did not produce preview {path}")
    path.chmod(0o600)


def output_entry(path: pathlib.Path, media_type: str, *, preview: bool = False) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "path": path.name,
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
        "media_type": media_type,
    }
    if preview:
        entry.update({"width": PREVIEW_WIDTH, "height": PREVIEW_HEIGHT})
    return entry


def build_asset(output_root: pathlib.Path, seed: int) -> pathlib.Path:
    try:
        import bpy  # type: ignore[import-not-found]
        import mathutils  # type: ignore[import-not-found]
    except ModuleNotFoundError as error:
        raise RuntimeError("This generator must run inside Blender") from error

    if tuple(bpy.app.version) != EXPECTED_BLENDER_VERSION:
        raise RuntimeError(
            f"Pinned Blender {'.'.join(map(str, EXPECTED_BLENDER_VERSION))} is required; "
            f"running {bpy.app.version_string}"
        )
    output_root = prepare_output_root(output_root, seed)
    configure_scene(bpy)
    forge = Forge(bpy, mathutils, seed)

    room_collection = forge.collection(ROOM_COLLECTION)
    cabinet_collection = forge.collection(CABINET_COLLECTION)
    chair_collection = forge.collection(CHAIR_COLLECTION)
    presentation_collection = forge.collection(PRESENTATION_COLLECTION)
    room_root = forge.empty(ROOM_ROOT_NAME, room_collection, (0.0, 0.0, 0.0))
    cabinet_root = forge.empty(CABINET_ROOT_NAME, cabinet_collection, CABINET_ASSEMBLY_ORIGIN_M)
    chair_root = forge.empty(CHAIR_ROOT_NAME, chair_collection, CHAIR_ASSEMBLY_ORIGIN_M)

    materials = {
        "wall": forge.material("VISTA_M_Wall_WarmWhite", (0.61, 0.64, 0.66, 1.0), roughness=0.78),
        "floor": forge.material("VISTA_M_Floor_CharcoalTerrazzo", (0.095, 0.115, 0.13, 1.0), roughness=0.42),
        "seam": forge.material("VISTA_M_FloorSeam", (0.016, 0.021, 0.026, 1.0), roughness=0.34),
        "skirting": forge.material("VISTA_M_Skirting_Anodized", (0.19, 0.22, 0.24, 1.0), metallic=0.72, roughness=0.28),
        "acoustic": forge.material("VISTA_M_Acoustic_MutedBlue", (0.18, 0.29, 0.36, 1.0), roughness=0.88),
        "acoustic_inset": forge.material("VISTA_M_AcousticInset", (0.10, 0.18, 0.23, 1.0), roughness=0.94),
        "utility": forge.material("VISTA_M_CableTrunk", (0.27, 0.30, 0.31, 1.0), roughness=0.56),
        "outlet": forge.material("VISTA_M_OutletPlate", (0.72, 0.72, 0.68, 1.0), roughness=0.58),
        "socket": forge.material("VISTA_M_OutletSocket", (0.025, 0.028, 0.03, 1.0), roughness=0.5),
        "ceiling": forge.material("VISTA_M_CeilingTile", (0.68, 0.70, 0.69, 1.0), roughness=0.91),
        "ceiling_grid": forge.material("VISTA_M_CeilingGrid", (0.40, 0.43, 0.43, 1.0), metallic=0.42, roughness=0.35),
        "light_diffuser": forge.material("VISTA_M_LED_Diffuser", (0.88, 0.91, 0.88, 1.0), roughness=0.20),
        "cabinet": forge.material("VISTA_M_CabinetPowdercoat", (0.16, 0.205, 0.235, 1.0), metallic=0.34, roughness=0.30),
        "cabinet_edge": forge.material("VISTA_M_CabinetEdge", (0.095, 0.13, 0.15, 1.0), metallic=0.48, roughness=0.24),
        "cabinet_interior": forge.material("VISTA_M_CabinetInterior", (0.26, 0.30, 0.31, 1.0), metallic=0.24, roughness=0.41),
        "cabinet_inset": forge.material("VISTA_M_CabinetDoorInset", (0.105, 0.145, 0.17, 1.0), metallic=0.28, roughness=0.34),
        "steel": forge.material("VISTA_M_BrushedSteel", (0.34, 0.37, 0.39, 1.0), metallic=0.90, roughness=0.21),
        "handle": forge.material("VISTA_M_HandleDarkSteel", (0.055, 0.066, 0.073, 1.0), metallic=0.84, roughness=0.18),
        "rubber": forge.material("VISTA_M_FootRubber", (0.018, 0.021, 0.022, 1.0), roughness=0.78),
        "label": forge.material("VISTA_M_LabelPaper", (0.79, 0.75, 0.61, 1.0), roughness=0.80),
        "label_ink": forge.material("VISTA_M_LabelInk", (0.055, 0.065, 0.07, 1.0), roughness=0.66),
        "shadow": forge.material("VISTA_M_RecessShadow", (0.008, 0.012, 0.015, 1.0), roughness=0.62),
        "wear": forge.material("VISTA_M_ExposedPrimer", (0.29, 0.19, 0.11, 1.0), metallic=0.35, roughness=0.43),
        "chair_frame": forge.material("VISTA_M_ChairGlassNylon", (0.025, 0.032, 0.038, 1.0), roughness=0.54),
        "chair_metal": forge.material("VISTA_M_ChairMechanism", (0.105, 0.125, 0.14, 1.0), metallic=0.76, roughness=0.29),
        "chair_chrome": forge.material("VISTA_M_ChairChrome", (0.52, 0.56, 0.59, 1.0), metallic=0.96, roughness=0.16),
        "chair_tire": forge.material("VISTA_M_ChairCasterRubber", (0.012, 0.016, 0.018, 1.0), roughness=0.76),
        "chair_fabric": forge.material("VISTA_M_ChairSeatFabric", (0.105, 0.155, 0.18, 1.0), roughness=0.91),
        "chair_fabric_edge": forge.material("VISTA_M_ChairFabricEdge", (0.055, 0.085, 0.105, 1.0), roughness=0.86),
        "chair_stitch": forge.material("VISTA_M_ChairStitch", (0.28, 0.38, 0.41, 1.0), roughness=0.82),
        "chair_mesh": forge.material("VISTA_M_ChairBackMesh", (0.035, 0.060, 0.072, 1.0), roughness=0.94),
        "chair_weave": forge.material("VISTA_M_ChairBackWeave", (0.12, 0.205, 0.23, 1.0), roughness=0.84),
        "chair_arm_pad": forge.material("VISTA_M_ChairArmPad", (0.035, 0.045, 0.050, 1.0), roughness=0.72),
    }

    build_room_shell(forge, room_collection, room_root, materials)
    build_cabinet(forge, cabinet_collection, cabinet_root, materials)
    build_ergonomic_chair(forge, chair_collection, chair_root, materials)

    room_stats = collection_statistics(room_collection, mathutils, relative_origin=(0.0, 0.0, 0.0))
    cabinet_stats = collection_statistics(cabinet_collection, mathutils, relative_origin=CABINET_ASSEMBLY_ORIGIN_M)
    chair_stats = collection_statistics(chair_collection, mathutils, relative_origin=CHAIR_ASSEMBLY_ORIGIN_M)
    geometry = combined_statistics((room_collection, cabinet_collection, chair_collection), mathutils)
    preview_views = derive_preview_camera_transforms(geometry["bounds"], chair_stats["bounds"], CHAIR_ASSEMBLY_ORIGIN_M)

    overview_camera = forge.camera(
        "VISTA_Preview_OverviewCamera",
        preview_views["overview"]["location"],
        preview_views["overview"]["target"],
        presentation_collection,
        preview_views["overview"]["lens_mm"],
    )
    detail_camera = forge.camera(
        "VISTA_Preview_DetailCamera",
        preview_views["detail"]["location"],
        preview_views["detail"]["target"],
        presentation_collection,
        preview_views["detail"]["lens_mm"],
    )
    forge.light("VISTA_Preview_Key", "AREA", (3.4, -1.7, 2.55), (0.0, 1.85, 1.15), presentation_collection, energy=930.0, color=(0.94, 0.97, 1.0), size=2.4)
    forge.light("VISTA_Preview_Fill", "AREA", (-1.9, -1.2, 1.85), (0.25, 1.6, 1.05), presentation_collection, energy=640.0, color=(0.72, 0.83, 1.0), size=2.0)
    forge.light("VISTA_Preview_Rim", "AREA", (1.9, 2.15, 2.42), (0.0, 1.75, 1.10), presentation_collection, energy=720.0, color=(1.0, 0.70, 0.44), size=1.3)
    forge.light("VISTA_Preview_Ceiling", "AREA", (-0.1, 0.2, 2.56), (0.0, 0.3, 0.0), presentation_collection, energy=760.0, color=(0.91, 0.95, 1.0), size=3.0)

    bpy.ops.object.select_all(action="DESELECT")
    export_objects = sorted(
        set(room_collection.all_objects) | set(cabinet_collection.all_objects) | set(chair_collection.all_objects),
        key=lambda item: item.name,
    )
    for obj in export_objects:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = chair_root

    glb_path = output_root / OUTPUT_FILENAMES["glb"]
    bpy.ops.export_scene.gltf(
        filepath=str(glb_path),
        export_format="GLB",
        use_selection=True,
        export_cameras=False,
        export_lights=False,
        export_apply=True,
        export_yup=True,
        export_extras=True,
    )
    gltf_path = output_root / OUTPUT_FILENAMES["gltf"]
    bpy.ops.export_scene.gltf(
        filepath=str(gltf_path),
        export_format="GLTF_SEPARATE",
        use_selection=True,
        export_cameras=False,
        export_lights=False,
        export_apply=True,
        export_yup=True,
        export_extras=True,
    )
    for key in ("glb", "gltf", "gltf_bin"):
        path = output_root / OUTPUT_FILENAMES[key]
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError(f"Blender did not produce {path}")
        path.chmod(0o600)

    bpy.ops.object.select_all(action="DESELECT")
    overview_path = output_root / OUTPUT_FILENAMES["preview_overview"]
    detail_path = output_root / OUTPUT_FILENAMES["preview_detail"]
    render_preview(bpy, overview_camera, overview_path)
    render_preview(bpy, detail_camera, detail_path)

    bpy.context.scene.camera = overview_camera
    blend_path = output_root / OUTPUT_FILENAMES["blend"]
    bpy.ops.wm.save_as_mainfile(filepath=str(blend_path), check_existing=False, compress=True)
    if not blend_path.is_file() or blend_path.stat().st_size == 0:
        raise RuntimeError(f"Blender did not save {blend_path}")
    blend_path.chmod(0o600)

    script_path = pathlib.Path(__file__).resolve(strict=True)
    manifest = {
        "schema": SCHEMA,
        "asset_id": ASSET_ID,
        "build": {
            "seed": seed,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "blender": {
                "version": ".".join(map(str, bpy.app.version)),
                "version_string": bpy.app.version_string,
            },
        },
        "units": {"system": "METRIC", "length": "meter", "scale_length": 1.0, "up_axis": "Z"},
        "source": {
            "description": "Procedurally generated VISTA mmg_040 architectural room shell, office cabinet, and ergonomic task chair; no external mesh or texture inputs.",
            "license": "Apache-2.0",
            "license_url": "https://www.apache.org/licenses/LICENSE-2.0",
            "generator": "SimWorld Studio VISTA script-first asset forge",
        },
        "script": {"path": SCRIPT_REPOSITORY_PATH, "sha256": sha256_file(script_path)},
        "collections": [ROOM_COLLECTION, CABINET_COLLECTION, CHAIR_COLLECTION],
        "preview_cameras": preview_views,
        "assembly": {
            "coordinate_system": "Blender right-handed Z-up metres",
            "ue_axis_mapping": "UE_cm = [300 + Blender_Y*100, Blender_X*100, Blender_Z*100] after Interchange glTF conversion",
            "component_transform_contract": "mesh vertices are baked in asset-local coordinates; component mesh nodes are identity under the asset root",
            "import_contract": "spawn all component meshes of one asset at a common actor transform or preserve the glTF root hierarchy",
        },
        "geometry": geometry,
        "assets": {
            "room_shell_kit": {
                "collection": ROOM_COLLECTION,
                "root_node": ROOM_ROOT_NAME,
                "origin_m": [0.0, 0.0, 0.0],
                "ue_placement": blender_origin_to_ue_placement((0.0, 0.0, 0.0)),
                **room_stats,
            },
            "tall_office_cabinet": {
                "collection": CABINET_COLLECTION,
                "root_node": CABINET_ROOT_NAME,
                "origin_m": list(CABINET_ASSEMBLY_ORIGIN_M),
                "ue_placement": blender_origin_to_ue_placement(CABINET_ASSEMBLY_ORIGIN_M),
                **cabinet_stats,
            },
            "ergonomic_office_chair": {
                "collection": CHAIR_COLLECTION,
                "root_node": CHAIR_ROOT_NAME,
                "origin_m": list(CHAIR_ASSEMBLY_ORIGIN_M),
                "ue_placement": blender_origin_to_ue_placement(CHAIR_ASSEMBLY_ORIGIN_M),
                **chair_stats,
            },
        },
        "outputs": {
            key: output_entry(
                output_root / OUTPUT_FILENAMES[key],
                OUTPUT_MEDIA_TYPES[key],
                preview=key.startswith("preview_"),
            )
            for key in OUTPUT_MEDIA_TYPES
        },
    }
    manifest_path = output_root / OUTPUT_FILENAMES["manifest"]
    temporary_manifest = output_root / ".manifest.json.tmp"
    temporary_manifest.write_bytes(canonical_json_bytes(manifest))
    temporary_manifest.chmod(0o600)
    os.replace(temporary_manifest, manifest_path)
    print(canonical_json_bytes({"status": "built", "manifest": str(manifest_path), "asset_id": ASSET_ID}).decode("utf-8"), end="")
    return manifest_path


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_blender_args(argv)
    build_asset(args.output_root, args.seed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
