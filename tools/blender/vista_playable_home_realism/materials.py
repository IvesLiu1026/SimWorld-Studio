"""Deterministic procedural PBR material plans and Blender realization."""

from __future__ import annotations

import hashlib
import math
import pathlib
from dataclasses import dataclass
from typing import Any, Iterable

from .config import DEFAULT_TEXTURE_SIZE_PX, sha256_file


PROJECT_MATERIAL_ID_PROPERTY = "vista_material_id"
PROJECT_MATERIAL_SEMANTICS_PROPERTY = "vista_pbr_semantics"
PROJECT_MATERIAL_RECEIPT_PROPERTY = "vista_material_receipt"
PROJECT_MATERIAL_PBR_SEMANTICS = "base_color+normal+roughness"
PROJECT_MATERIAL_CONTRACT_PROPERTIES = frozenset(
    {
        PROJECT_MATERIAL_ID_PROPERTY,
        PROJECT_MATERIAL_SEMANTICS_PROPERTY,
        PROJECT_MATERIAL_RECEIPT_PROPERTY,
    }
)


@dataclass(frozen=True)
class MaterialSpec:
    material_id: str
    label: str
    base_color: tuple[float, float, float]
    roughness: float
    metallic: float
    pattern: str
    normal_strength: float
    shader_class: str = "PrincipledBSDF"
    blend_mode: str = "OPAQUE"
    transmission: float = 0.0
    ior: float = 1.46
    texel_density_px_per_m: int = 768


def material_specs() -> tuple[MaterialSpec, ...]:
    """Return the pinned, project-authored architectural material palette."""

    return (
        MaterialSpec("r2.plaster_warm", "Warm lime plaster", (0.72, 0.68, 0.59), 0.82, 0.0, "plaster", 0.18),
        MaterialSpec("r2.ceiling_matte", "Matte ceiling", (0.84, 0.84, 0.79), 0.91, 0.0, "plaster", 0.08),
        MaterialSpec("r2.trim_satin", "Warm satin trim", (0.78, 0.77, 0.70), 0.48, 0.0, "paint", 0.06),
        MaterialSpec("r2.slate_honed", "Honed entry slate", (0.12, 0.15, 0.16), 0.43, 0.05, "stone", 0.24, texel_density_px_per_m=1024),
        MaterialSpec("r2.oak_natural", "Natural oak floor", (0.42, 0.23, 0.09), 0.56, 0.0, "wood", 0.32, texel_density_px_per_m=1024),
        MaterialSpec("r2.terrazzo_warm", "Warm terrazzo", (0.52, 0.50, 0.44), 0.34, 0.02, "terrazzo", 0.20, texel_density_px_per_m=1024),
        MaterialSpec("r2.threshold_brass", "Brushed brass threshold", (0.48, 0.27, 0.055), 0.27, 0.88, "brushed", 0.11),
        MaterialSpec("r2.window_frame", "Powder-coated window frame", (0.035, 0.045, 0.047), 0.33, 0.72, "paint", 0.04),
        MaterialSpec(
            "r2.window_glass",
            "Architectural glass",
            (0.12, 0.22, 0.25),
            0.08,
            0.0,
            "glass",
            0.015,
            blend_mode="BLEND",
            transmission=0.92,
            ior=1.45,
        ),
        MaterialSpec("r2.exterior_scrim", "Exterior daylight scrim", (0.16, 0.27, 0.34), 0.68, 0.0, "paint", 0.03),
        MaterialSpec("r2.cabinet_sage", "Satin sage cabinetry", (0.20, 0.31, 0.23), 0.42, 0.0, "paint", 0.07, texel_density_px_per_m=1024),
        MaterialSpec("r2.cabinet_walnut", "Walnut cabinet interior", (0.20, 0.075, 0.025), 0.48, 0.0, "wood", 0.24, texel_density_px_per_m=1024),
        MaterialSpec("r2.counter_quartz", "Honed quartz counter", (0.66, 0.64, 0.57), 0.29, 0.01, "stone", 0.17, texel_density_px_per_m=1024),
        MaterialSpec("r2.backsplash_tile", "Handmade backsplash tile", (0.34, 0.44, 0.39), 0.24, 0.0, "tile", 0.22, texel_density_px_per_m=1024),
        MaterialSpec("r2.hardware_brass", "Cabinet hardware brass", (0.51, 0.30, 0.06), 0.23, 0.90, "brushed", 0.08, texel_density_px_per_m=1024),
    )


def material_by_id() -> dict[str, MaterialSpec]:
    return {item.material_id: item for item in material_specs()}


def project_material_export_name(material_id: str) -> str:
    """Return the exact Blender/glTF name for one canonical project material."""

    if material_id not in material_by_id():
        raise ValueError(f"unknown project material ID: {material_id!r}")
    return f"VISTA_M_{material_id.replace('.', '_')}"


def _noise(material_id: str, x: int, y: int, channel: str) -> float:
    payload = f"{material_id}:{channel}:{x}:{y}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:4], "big") / 0xFFFFFFFF


def _height(spec: MaterialSpec, x: int, y: int, size: int) -> float:
    nx = x / max(1, size - 1)
    ny = y / max(1, size - 1)
    grain = _noise(spec.material_id, x, y, "height") - 0.5
    if spec.pattern == "wood":
        return 0.55 * math.sin((ny * 9.0 + 0.18 * math.sin(nx * 7.0)) * math.tau) + grain * 0.22
    if spec.pattern == "terrazzo":
        return (1.0 if _noise(spec.material_id, x // 3, y // 3, "chip") > 0.91 else 0.0) + grain * 0.08
    if spec.pattern == "tile":
        grout_x = min(nx % 0.25, 0.25 - (nx % 0.25)) < 0.012
        grout_y = min(ny % 0.25, 0.25 - (ny % 0.25)) < 0.012
        return -0.8 if grout_x or grout_y else grain * 0.12
    if spec.pattern == "brushed":
        return math.sin(ny * 42.0 * math.tau) * 0.18 + grain * 0.04
    if spec.pattern == "stone":
        return grain * 0.34 + math.sin((nx + ny * 0.31) * 5.0 * math.tau) * 0.08
    if spec.pattern == "plaster":
        return grain * 0.32
    if spec.pattern == "glass":
        return grain * 0.015
    return grain * 0.10


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, value))


def _texture_pixels(spec: MaterialSpec, semantic: str, size: int) -> list[float]:
    heights = [[_height(spec, x, y, size) for x in range(size)] for y in range(size)]
    pixels: list[float] = []
    for y in range(size):
        for x in range(size):
            noise = _noise(spec.material_id, x, y, semantic) - 0.5
            height = heights[y][x]
            if semantic == "base_color":
                variation = noise * 0.055
                if spec.pattern == "wood":
                    variation += height * 0.045
                elif spec.pattern == "terrazzo" and height > 0.5:
                    variation += 0.16 * (_noise(spec.material_id, x // 3, y // 3, "chip-color") - 0.5)
                rgb = tuple(_clamp(channel + variation) for channel in spec.base_color)
            elif semantic == "roughness":
                value = _clamp(spec.roughness + noise * 0.10 + height * 0.025)
                rgb = (value, value, value)
            elif semantic == "normal":
                left = heights[y][max(0, x - 1)]
                right = heights[y][min(size - 1, x + 1)]
                down = heights[max(0, y - 1)][x]
                up = heights[min(size - 1, y + 1)][x]
                dx = (left - right) * spec.normal_strength
                dy = (down - up) * spec.normal_strength
                length = math.sqrt(dx * dx + dy * dy + 1.0)
                rgb = (0.5 + dx / length * 0.5, 0.5 + dy / length * 0.5, 0.5 + 0.5 / length)
            else:
                raise ValueError(f"unsupported material semantic: {semantic}")
            pixels.extend((*rgb, 1.0))
    return pixels


def texture_filename(material_id: str, semantic: str) -> str:
    safe = material_id.replace(".", "_")
    return f"{safe}_{semantic}.png"


def material_plan_manifest(texture_size_px: int = DEFAULT_TEXTURE_SIZE_PX) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for spec in material_specs():
        channels = {}
        for semantic in ("base_color", "normal", "roughness"):
            channels[semantic] = {
                "semantic": semantic,
                "relative_path": f"textures/{texture_filename(spec.material_id, semantic)}",
                "dimensions_px": [texture_size_px, texture_size_px],
                "color_space": "sRGB" if semantic == "base_color" else "Non-Color",
                "uv_set": 0,
                "procedural_pattern": spec.pattern,
            }
        result.append(
            {
                "material_id": spec.material_id,
                "label": spec.label,
                "shader_class": spec.shader_class,
                "blend_mode": spec.blend_mode,
                "base_color": list(spec.base_color),
                "roughness": spec.roughness,
                "metallic": spec.metallic,
                "transmission": spec.transmission,
                "ior": spec.ior,
                "texel_density_px_per_m": spec.texel_density_px_per_m,
                "channels": channels,
            }
        )
    return result


def _save_image(bpy: Any, path: pathlib.Path, name: str, pixels: Iterable[float], size: int, *, color_space: str) -> Any:
    # Blender 4.5's headless PNG writer can flush byte-backed generated images
    # as black.  A float-backed image preserves the authored pixels and still
    # writes ordinary 8-bit PNGs under the selected color-space policy.
    image = bpy.data.images.new(name=name, width=size, height=size, alpha=True, float_buffer=True)
    # Blender invalidates a generated image's pixel buffer when this property
    # changes, even when assigning the apparent default.  Set it before pixels.
    image.colorspace_settings.name = color_space
    image.pixels.foreach_set(list(pixels))
    # ``foreach_set`` only updates the RNA buffer.  Blender 4.5 otherwise saves
    # the image before the GPU/imbuf copy sees those pixels, yielding a valid
    # but black PNG in headless mode.
    image.update()
    image.filepath_raw = str(path)
    image.file_format = "PNG"
    image.save()
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(f"Blender did not save material texture: {path}")
    return image


def realize_blender_materials(
    bpy: Any,
    output_root: pathlib.Path,
    *,
    texture_size_px: int = DEFAULT_TEXTURE_SIZE_PX,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Create image-backed Principled materials and return hash-bound receipts."""

    texture_root = output_root / "textures"
    texture_root.mkdir(mode=0o700)
    materials: dict[str, Any] = {}
    receipts = material_plan_manifest(texture_size_px)
    receipt_by_id = {item["material_id"]: item for item in receipts}
    for spec in material_specs():
        images: dict[str, Any] = {}
        for semantic in ("base_color", "normal", "roughness"):
            path = texture_root / texture_filename(spec.material_id, semantic)
            color_space = "sRGB" if semantic == "base_color" else "Non-Color"
            images[semantic] = _save_image(
                bpy,
                path,
                f"VISTA_{spec.material_id}_{semantic}",
                _texture_pixels(spec, semantic, texture_size_px),
                texture_size_px,
                color_space=color_space,
            )
            receipt_by_id[spec.material_id]["channels"][semantic]["sha256"] = sha256_file(path)
            path.chmod(0o600)

        material = bpy.data.materials.new(name=project_material_export_name(spec.material_id))
        material.use_nodes = True
        material.diffuse_color = (*spec.base_color, 0.35 if spec.blend_mode == "BLEND" else 1.0)
        material[PROJECT_MATERIAL_ID_PROPERTY] = spec.material_id
        material[PROJECT_MATERIAL_SEMANTICS_PROPERTY] = PROJECT_MATERIAL_PBR_SEMANTICS
        material[PROJECT_MATERIAL_RECEIPT_PROPERTY] = f"materials/{spec.material_id}"
        nodes = material.node_tree.nodes
        links = material.node_tree.links
        for node in tuple(nodes):
            nodes.remove(node)
        output = nodes.new("ShaderNodeOutputMaterial")
        output.name = "VISTA_Output"
        principled = nodes.new("ShaderNodeBsdfPrincipled")
        principled.name = "VISTA_Principled_PBR"
        principled.inputs["Base Color"].default_value = (*spec.base_color, 1.0)
        principled.inputs["Roughness"].default_value = spec.roughness
        principled.inputs["Metallic"].default_value = spec.metallic
        if principled.inputs.get("IOR") is not None:
            principled.inputs["IOR"].default_value = spec.ior
        if principled.inputs.get("Transmission Weight") is not None:
            principled.inputs["Transmission Weight"].default_value = spec.transmission
        if principled.inputs.get("Alpha") is not None:
            principled.inputs["Alpha"].default_value = 0.35 if spec.blend_mode == "BLEND" else 1.0
        links.new(principled.outputs["BSDF"], output.inputs["Surface"])

        tex_nodes: dict[str, Any] = {}
        for index, semantic in enumerate(("base_color", "roughness", "normal")):
            tex = nodes.new("ShaderNodeTexImage")
            tex.name = f"VISTA_{semantic}"
            tex.label = semantic
            tex.image = images[semantic]
            tex.location = (-720, 220 - index * 240)
            tex.interpolation = "Linear"
            tex.extension = "REPEAT"
            tex_nodes[semantic] = tex
        links.new(tex_nodes["base_color"].outputs["Color"], principled.inputs["Base Color"])
        links.new(tex_nodes["roughness"].outputs["Color"], principled.inputs["Roughness"])
        normal_map = nodes.new("ShaderNodeNormalMap")
        normal_map.name = "VISTA_Normal_Semantic"
        normal_map.inputs["Strength"].default_value = 1.0
        links.new(tex_nodes["normal"].outputs["Color"], normal_map.inputs["Color"])
        links.new(normal_map.outputs["Normal"], principled.inputs["Normal"])
        principled.location = (0, 0)
        output.location = (320, 0)
        materials[spec.material_id] = material
    return materials, receipts
