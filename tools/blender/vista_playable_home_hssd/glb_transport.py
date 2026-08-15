"""Safe GLB surgery for Blender-incompatible HSSD BasisU textures.

Blender 4.5.8 cannot import ``KHR_texture_basisu``.  Geometry still needs to
pass through Blender for deterministic joining and AABB normalization, while
the original compressed PBR payload must remain intact.  This module creates
a material-index surrogate for Blender and then reattaches the byte-identical
KTX2 images and original glTF material records to Blender's normalized GLB.
"""

from __future__ import annotations

import copy
import hashlib
import json
import pathlib
import re
import struct
from typing import Any

from .planner import HssdBindingError


GLB_JSON_CHUNK = 0x4E4F534A
GLB_BIN_CHUNK = 0x004E4942
MATERIAL_PREFIX = "VISTA_HSSD_MAT_"
_MATERIAL_RE = re.compile(r"^VISTA_HSSD_MAT_(\d{4})__")
_MATERIAL_ONLY_EXTENSIONS = {
    "KHR_materials_clearcoat",
    "KHR_materials_emissive_strength",
    "KHR_materials_ior",
    "KHR_materials_iridescence",
    "KHR_materials_sheen",
    "KHR_materials_specular",
    "KHR_materials_transmission",
    "KHR_materials_unlit",
    "KHR_materials_variants",
    "KHR_materials_volume",
    "KHR_texture_basisu",
    "KHR_texture_transform",
}


def read_glb(path: pathlib.Path) -> tuple[dict[str, Any], bytes]:
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise HssdBindingError(f"unable to read GLB {path.name}: {error}") from error
    if len(payload) < 20:
        raise HssdBindingError(f"truncated GLB: {path.name}")
    magic, version, declared_length = struct.unpack_from("<4sII", payload, 0)
    if magic != b"glTF" or version != 2 or declared_length != len(payload):
        raise HssdBindingError(f"invalid GLB header: {path.name}")
    offset = 12
    document: dict[str, Any] | None = None
    binary = b""
    while offset < len(payload):
        if offset + 8 > len(payload):
            raise HssdBindingError(f"truncated GLB chunk: {path.name}")
        length, kind = struct.unpack_from("<II", payload, offset)
        offset += 8
        end = offset + length
        if end > len(payload):
            raise HssdBindingError(f"GLB chunk exceeds file: {path.name}")
        chunk = payload[offset:end]
        offset = end
        if kind == GLB_JSON_CHUNK:
            if document is not None:
                raise HssdBindingError(f"duplicate GLB JSON chunk: {path.name}")
            try:
                parsed = json.loads(chunk.rstrip(b"\x00 \t\r\n").decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise HssdBindingError(f"invalid GLB JSON: {path.name}") from error
            if not isinstance(parsed, dict):
                raise HssdBindingError(f"GLB JSON root is not an object: {path.name}")
            document = parsed
        elif kind == GLB_BIN_CHUNK:
            if binary:
                raise HssdBindingError(f"duplicate GLB BIN chunk: {path.name}")
            binary = bytes(chunk)
    if document is None:
        raise HssdBindingError(f"GLB has no JSON document: {path.name}")
    return document, binary


def write_glb(path: pathlib.Path, document: dict[str, Any], binary: bytes) -> None:
    json_payload = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    json_payload += b" " * ((4 - len(json_payload) % 4) % 4)
    binary_payload = binary + b"\x00" * ((4 - len(binary) % 4) % 4)
    chunks = struct.pack("<II", len(json_payload), GLB_JSON_CHUNK) + json_payload
    if binary_payload:
        chunks += struct.pack("<II", len(binary_payload), GLB_BIN_CHUNK) + binary_payload
    payload = struct.pack("<4sII", b"glTF", 2, 12 + len(chunks)) + chunks
    try:
        with path.open("xb") as handle:
            handle.write(payload)
        path.chmod(0o600)
    except OSError as error:
        raise HssdBindingError(f"unable to write GLB {path.name}: {error}") from error


def uses_required_basisu(document: dict[str, Any]) -> bool:
    return "KHR_texture_basisu" in document.get("extensionsRequired", [])


def _strip_texture_fields(material: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(material)
    pbr = result.get("pbrMetallicRoughness")
    if isinstance(pbr, dict):
        pbr.pop("baseColorTexture", None)
        pbr.pop("metallicRoughnessTexture", None)
    for key in ("normalTexture", "occlusionTexture", "emissiveTexture"):
        result.pop(key, None)
    result.pop("extensions", None)
    return result


def write_blender_surrogate(source_path: pathlib.Path, surrogate_path: pathlib.Path) -> dict[str, Any]:
    source, binary = read_glb(source_path)
    if not uses_required_basisu(source):
        raise HssdBindingError("BasisU surrogate requested for a non-BasisU GLB")
    required = set(source.get("extensionsRequired", []))
    unsupported_required = required - _MATERIAL_ONLY_EXTENSIONS
    if unsupported_required:
        raise HssdBindingError(f"BasisU surrogate cannot drop required geometry extensions: {sorted(unsupported_required)}")
    materials = source.get("materials", [])
    if not isinstance(materials, list) or not materials:
        raise HssdBindingError("BasisU source has no materials")
    surrogate = copy.deepcopy(source)
    stripped: list[dict[str, Any]] = []
    for index, material in enumerate(materials):
        if not isinstance(material, dict):
            raise HssdBindingError("BasisU source has invalid material")
        entry = _strip_texture_fields(material)
        original_name = str(material.get("name", "material"))
        entry["name"] = f"{MATERIAL_PREFIX}{index:04d}__{original_name}"
        stripped.append(entry)
    surrogate["materials"] = stripped
    surrogate.pop("textures", None)
    surrogate.pop("images", None)
    surrogate.pop("samplers", None)
    used = [name for name in surrogate.get("extensionsUsed", []) if name not in _MATERIAL_ONLY_EXTENSIONS]
    required_after = [name for name in surrogate.get("extensionsRequired", []) if name not in _MATERIAL_ONLY_EXTENSIONS]
    if used:
        surrogate["extensionsUsed"] = used
    else:
        surrogate.pop("extensionsUsed", None)
    if required_after:
        surrogate["extensionsRequired"] = required_after
    else:
        surrogate.pop("extensionsRequired", None)
    write_glb(surrogate_path, surrogate, binary)
    return {
        "mode": "basisu_material_index_surrogate",
        "source_material_count": len(materials),
        "source_image_count": len(source.get("images", [])),
        "source_texture_count": len(source.get("textures", [])),
    }


def _source_used_material_indices(document: dict[str, Any]) -> set[int]:
    result: set[int] = set()
    for mesh in document.get("meshes", []):
        if not isinstance(mesh, dict):
            continue
        for primitive in mesh.get("primitives", []):
            if isinstance(primitive, dict) and isinstance(primitive.get("material"), int):
                result.add(primitive["material"])
    return result


def rehydrate_basisu_materials(
    source_path: pathlib.Path,
    normalized_surrogate_path: pathlib.Path,
    output_path: pathlib.Path,
) -> dict[str, Any]:
    source, source_bin = read_glb(source_path)
    output, output_bin = read_glb(normalized_surrogate_path)
    if not uses_required_basisu(source):
        raise HssdBindingError("BasisU rehydration requested for a non-BasisU source")
    source_materials = source.get("materials", [])
    output_materials = output.get("materials", [])
    if not isinstance(source_materials, list) or not isinstance(output_materials, list):
        raise HssdBindingError("BasisU material arrays are invalid")
    material_map: dict[int, int] = {}
    for output_index, material in enumerate(output_materials):
        if not isinstance(material, dict):
            continue
        match = _MATERIAL_RE.match(str(material.get("name", "")))
        if match:
            source_index = int(match.group(1))
            if source_index >= len(source_materials) or source_index in material_map.values():
                raise HssdBindingError("BasisU material index marker is invalid or duplicated")
            material_map[output_index] = source_index
    mapped_source_indices: set[int] = set()
    for mesh in output.get("meshes", []):
        if not isinstance(mesh, dict):
            raise HssdBindingError("normalized surrogate has invalid mesh")
        for primitive in mesh.get("primitives", []):
            if not isinstance(primitive, dict):
                raise HssdBindingError("normalized surrogate has invalid primitive")
            material_index = primitive.get("material")
            if material_index is None:
                continue
            if not isinstance(material_index, int) or material_index not in material_map:
                raise HssdBindingError("normalized surrogate lost a material index marker")
            primitive["material"] = material_map[material_index]
            mapped_source_indices.add(material_map[material_index])
    expected_used = _source_used_material_indices(source)
    if mapped_source_indices != expected_used:
        raise HssdBindingError(
            f"normalized surrogate material coverage drifted: expected={sorted(expected_used)}, actual={sorted(mapped_source_indices)}"
        )
    output["materials"] = copy.deepcopy(source_materials)
    if "samplers" in source:
        output["samplers"] = copy.deepcopy(source["samplers"])
    else:
        output.pop("samplers", None)
    output["textures"] = copy.deepcopy(source.get("textures", []))

    output_views = output.setdefault("bufferViews", [])
    source_views = source.get("bufferViews", [])
    source_images = source.get("images", [])
    if not isinstance(output_views, list) or not isinstance(source_views, list) or not isinstance(source_images, list):
        raise HssdBindingError("BasisU image/bufferView arrays are invalid")
    combined = bytearray(output_bin)
    copied_images: list[dict[str, Any]] = []
    image_payload_bytes = 0
    for image in source_images:
        if not isinstance(image, dict) or not isinstance(image.get("bufferView"), int):
            raise HssdBindingError("BasisU source image must be an embedded bufferView")
        source_view_index = image["bufferView"]
        if not (0 <= source_view_index < len(source_views)):
            raise HssdBindingError("BasisU source image bufferView is out of range")
        view = source_views[source_view_index]
        if not isinstance(view, dict) or view.get("buffer", 0) != 0:
            raise HssdBindingError("BasisU source image must use embedded buffer zero")
        start = int(view.get("byteOffset", 0))
        length = view.get("byteLength")
        if not isinstance(length, int) or start < 0 or start + length > len(source_bin):
            raise HssdBindingError("BasisU source image bytes are out of range")
        while len(combined) % 4:
            combined.append(0)
        destination_offset = len(combined)
        combined.extend(source_bin[start : start + length])
        destination_view = {"buffer": 0, "byteOffset": destination_offset, "byteLength": length}
        output_views.append(destination_view)
        copied = copy.deepcopy(image)
        copied["bufferView"] = len(output_views) - 1
        copied_images.append(copied)
        image_payload_bytes += length
    output["images"] = copied_images
    output["buffers"] = [{"byteLength": len(combined)}]

    used = sorted(set(output.get("extensionsUsed", [])) | set(source.get("extensionsUsed", [])))
    required = sorted(set(output.get("extensionsRequired", [])) | set(source.get("extensionsRequired", [])))
    if used:
        output["extensionsUsed"] = used
    if required:
        output["extensionsRequired"] = required
    write_glb(output_path, output, bytes(combined))
    validation = validate_preserved_basisu_glb(source_path, output_path)
    return {
        "mode": "KHR_texture_basisu_preserved",
        "mechanism": "byte_preserving_rehydration_after_blender_material_index_surrogate",
        "blender_decoded_textures": False,
        "mapped_material_count": len(mapped_source_indices),
        "copied_image_count": len(copied_images),
        "copied_image_payload_bytes": image_payload_bytes,
        "self_contained": validation["self_contained"],
        "single_buffer": validation["single_buffer"],
        "single_mesh": validation["single_mesh"],
        "buffer_views_aligned_and_in_range": validation["buffer_views_aligned_and_in_range"],
        "primitive_material_indices_valid": validation["primitive_material_indices_valid"],
        "basisu_texture_sources_valid": validation["basisu_texture_sources_valid"],
        "extension_declarations_complete": validation["extension_declarations_complete"],
        "base_normal_orm_texture_slots": validation["base_normal_orm_texture_slots"],
        "image_payloads": validation["image_payloads"],
    }


def _validate_buffer_graph(document: dict[str, Any], binary: bytes, label: str) -> None:
    buffers = document.get("buffers")
    views = document.get("bufferViews", [])
    if not isinstance(buffers, list) or len(buffers) != 1 or not isinstance(buffers[0], dict):
        raise HssdBindingError(f"{label} must be a self-contained single-buffer GLB")
    if "uri" in buffers[0]:
        raise HssdBindingError(f"{label} buffer must not have an external URI")
    byte_length = buffers[0].get("byteLength")
    if not isinstance(byte_length, int) or byte_length < 0 or byte_length > len(binary):
        raise HssdBindingError(f"{label} buffer byteLength is invalid")
    if any(binary[byte_length:]):
        raise HssdBindingError(f"{label} GLB padding must be zero")
    if not isinstance(views, list):
        raise HssdBindingError(f"{label} bufferViews must be an array")
    for index, view in enumerate(views):
        if not isinstance(view, dict) or view.get("buffer", 0) != 0:
            raise HssdBindingError(f"{label} bufferView[{index}] must reference embedded buffer zero")
        offset = view.get("byteOffset", 0)
        length = view.get("byteLength")
        if not isinstance(offset, int) or not isinstance(length, int) or offset < 0 or length <= 0:
            raise HssdBindingError(f"{label} bufferView[{index}] range is invalid")
        if offset % 4 != 0 or offset + length > byte_length:
            raise HssdBindingError(f"{label} bufferView[{index}] is unaligned or out of range")


def _image_payloads(document: dict[str, Any], binary: bytes, label: str) -> list[dict[str, Any]]:
    views = document.get("bufferViews", [])
    images = document.get("images")
    if not isinstance(images, list) or not images:
        raise HssdBindingError(f"{label} must contain embedded BasisU images")
    result: list[dict[str, Any]] = []
    for index, image in enumerate(images):
        if not isinstance(image, dict) or image.get("mimeType") != "image/ktx2" or "uri" in image:
            raise HssdBindingError(f"{label} image[{index}] must be a self-contained image/ktx2 bufferView")
        view_index = image.get("bufferView")
        if not isinstance(view_index, int) or not (0 <= view_index < len(views)):
            raise HssdBindingError(f"{label} image[{index}] has a dangling bufferView")
        view = views[view_index]
        offset = int(view.get("byteOffset", 0))
        length = int(view["byteLength"])
        payload = binary[offset : offset + length]
        result.append({"image_index": index, "bytes": length, "sha256": hashlib.sha256(payload).hexdigest()})
    return result


def _validate_texture_info(info: Any, texture_count: int, label: str) -> bool:
    if not isinstance(info, dict) or not isinstance(info.get("index"), int):
        raise HssdBindingError(f"{label} texture info is invalid")
    if not 0 <= info["index"] < texture_count:
        raise HssdBindingError(f"{label} texture index is dangling")
    return True


def _validate_material_texture_indices(materials: Any, texture_count: int) -> int:
    if not isinstance(materials, list) or not materials:
        raise HssdBindingError("preserved BasisU GLB must contain materials")
    base_normal_orm_slots = 0
    for material_index, material in enumerate(materials):
        if not isinstance(material, dict):
            raise HssdBindingError(f"material[{material_index}] is invalid")
        pbr = material.get("pbrMetallicRoughness", {})
        if not isinstance(pbr, dict):
            raise HssdBindingError(f"material[{material_index}].pbrMetallicRoughness is invalid")
        for field in ("baseColorTexture", "metallicRoughnessTexture"):
            if field in pbr:
                base_normal_orm_slots += int(_validate_texture_info(pbr[field], texture_count, f"material[{material_index}].{field}"))
        for field in ("normalTexture", "occlusionTexture", "emissiveTexture"):
            if field in material:
                _validate_texture_info(material[field], texture_count, f"material[{material_index}].{field}")
                if field in {"normalTexture", "occlusionTexture"}:
                    base_normal_orm_slots += 1

        # Validate extension texture infos such as KHR_materials_specular.
        def walk(node: Any, pointer: str) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    child = f"{pointer}.{key}"
                    if key.endswith("Texture"):
                        _validate_texture_info(value, texture_count, child)
                    else:
                        walk(value, child)
            elif isinstance(node, list):
                for item_index, value in enumerate(node):
                    walk(value, f"{pointer}[{item_index}]")

        walk(material.get("extensions", {}), f"material[{material_index}].extensions")
    if base_normal_orm_slots < 1:
        raise HssdBindingError("preserved BasisU GLB lacks baseColor/normal/ORM texture slots")
    return base_normal_orm_slots


def _validate_mesh_indices(document: dict[str, Any]) -> None:
    materials = document.get("materials", [])
    accessors = document.get("accessors", [])
    meshes = document.get("meshes")
    if not isinstance(accessors, list) or not isinstance(meshes, list) or len(meshes) != 1:
        raise HssdBindingError("preserved BasisU output must contain exactly one mesh")
    for mesh_index, mesh in enumerate(meshes):
        if not isinstance(mesh, dict) or not isinstance(mesh.get("primitives"), list) or not mesh["primitives"]:
            raise HssdBindingError(f"mesh[{mesh_index}] primitives are invalid")
        for primitive_index, primitive in enumerate(mesh["primitives"]):
            if not isinstance(primitive, dict):
                raise HssdBindingError(f"mesh[{mesh_index}].primitive[{primitive_index}] is invalid")
            material = primitive.get("material")
            if not isinstance(material, int) or not 0 <= material < len(materials):
                raise HssdBindingError(f"mesh[{mesh_index}].primitive[{primitive_index}] has dangling material")
            indices = primitive.get("indices")
            if indices is not None and (not isinstance(indices, int) or not 0 <= indices < len(accessors)):
                raise HssdBindingError(f"mesh[{mesh_index}].primitive[{primitive_index}] has dangling indices accessor")
            attributes = primitive.get("attributes", {})
            if not isinstance(attributes, dict):
                raise HssdBindingError(f"mesh[{mesh_index}].primitive[{primitive_index}] attributes are invalid")
            for semantic, accessor in attributes.items():
                if not isinstance(accessor, int) or not 0 <= accessor < len(accessors):
                    raise HssdBindingError(f"mesh[{mesh_index}].primitive[{primitive_index}].{semantic} accessor is dangling")


def _collect_extension_keys(node: Any) -> set[str]:
    result: set[str] = set()
    if isinstance(node, dict):
        extensions = node.get("extensions")
        if isinstance(extensions, dict):
            result.update(extensions)
        for value in node.values():
            result.update(_collect_extension_keys(value))
    elif isinstance(node, list):
        for value in node:
            result.update(_collect_extension_keys(value))
    return result


def validate_preserved_basisu_glb(source_path: pathlib.Path, output_path: pathlib.Path) -> dict[str, Any]:
    """Validate that normalized output is closed and KTX2 bytes are exact."""

    source, source_bin = read_glb(source_path)
    output, output_bin = read_glb(output_path)
    if not uses_required_basisu(source):
        raise HssdBindingError("source does not require KHR_texture_basisu")
    _validate_buffer_graph(output, output_bin, "output")
    _validate_mesh_indices(output)
    textures = output.get("textures")
    images = output.get("images")
    samplers = output.get("samplers", [])
    if not isinstance(textures, list) or not textures or not isinstance(images, list) or not isinstance(samplers, list):
        raise HssdBindingError("preserved BasisU texture arrays are invalid")
    for texture_index, texture in enumerate(textures):
        if not isinstance(texture, dict):
            raise HssdBindingError(f"texture[{texture_index}] is invalid")
        extension = texture.get("extensions", {}).get("KHR_texture_basisu")
        source_index = extension.get("source") if isinstance(extension, dict) else None
        if not isinstance(source_index, int) or not 0 <= source_index < len(images):
            raise HssdBindingError(f"texture[{texture_index}] has dangling KHR_texture_basisu.source")
        sampler = texture.get("sampler")
        if sampler is not None and (not isinstance(sampler, int) or not 0 <= sampler < len(samplers)):
            raise HssdBindingError(f"texture[{texture_index}] has dangling sampler")
    base_normal_orm_slots = _validate_material_texture_indices(output.get("materials"), len(textures))
    source_images = _image_payloads(source, source_bin, "source")
    output_images = _image_payloads(output, output_bin, "output")
    if source_images != output_images:
        raise HssdBindingError("output KTX2 image bytes do not match source image-by-image")
    image_receipts = [
        {
            "image_index": source_entry["image_index"],
            "bytes": source_entry["bytes"],
            "source_sha256": source_entry["sha256"],
            "output_sha256": output_entry["sha256"],
            "match": source_entry["sha256"] == output_entry["sha256"],
        }
        for source_entry, output_entry in zip(source_images, output_images, strict=True)
    ]
    if output.get("materials") != source.get("materials") or output.get("textures") != source.get("textures"):
        raise HssdBindingError("output PBR material/texture records do not exactly match source")
    used = output.get("extensionsUsed")
    required = output.get("extensionsRequired")
    if not isinstance(used, list) or not isinstance(required, list):
        raise HssdBindingError("output extension declarations are missing")
    if "KHR_texture_basisu" not in used or "KHR_texture_basisu" not in required or not set(required).issubset(set(used)):
        raise HssdBindingError("output KHR_texture_basisu extension declarations are incomplete")
    referenced_extensions = _collect_extension_keys({"materials": output.get("materials"), "textures": textures})
    if not referenced_extensions.issubset(set(used)):
        raise HssdBindingError(f"output extensionsUsed misses referenced extensions: {sorted(referenced_extensions - set(used))}")
    return {
        "mode": "KHR_texture_basisu_preserved",
        "self_contained": True,
        "single_buffer": True,
        "single_mesh": True,
        "buffer_views_aligned_and_in_range": True,
        "primitive_material_indices_valid": True,
        "basisu_texture_sources_valid": True,
        "extension_declarations_complete": True,
        "base_normal_orm_texture_slots": base_normal_orm_slots,
        "image_payloads": image_receipts,
    }
