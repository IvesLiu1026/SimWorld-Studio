"""Safe GLB surgery for Blender- and Unreal-incompatible HSSD textures.

Blender 4.5.8 cannot import required ``KHR_texture_basisu`` and Unreal 5.7.3's
Interchange importer cannot use it as a required-only texture source. Geometry
therefore passes through Blender as a material-index surrogate. The source
KTX2 base levels are then decoded by an explicitly hash-pinned, offline Basis
Universal WASM transcoder and embedded as core glTF PNG images. Original PBR
materials, texture slots, samplers, source hashes, and licensing remain closed
and attributable; no texture is fabricated or silently dropped.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import pathlib
import re
import struct
import subprocess
import tempfile
import zlib
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
_BASIS_TRANSCODER_PINS = {
    (
        "8478b5b6d6b74e7d3082b89f6417321d8d1dc0307f2b30d4484bb11b441696a1",
        "6cf17dc889352c42e9acf8897107978d127005fe3386c36a0e3845e27967630a",
    ): {
        "distribution": "three",
        "distribution_version": "0.185.1",
        "basis_universal_license": "Apache-2.0",
        "three_license": "MIT",
        "provenance": "three/examples/jsm/libs/basis",
    },
}
_DECODE_SCRIPT = pathlib.Path(__file__).with_name("basisu_decode.mjs")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_MAX_DECODED_RGBA_BYTES = 256 * 1024 * 1024


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


def _sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_absolute_file(path: pathlib.Path, label: str, *, executable: bool = False) -> pathlib.Path:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise HssdBindingError(f"{label} must be an absolute regular non-symlink file")
    resolved = path.resolve(strict=True)
    if executable and not os.access(resolved, os.X_OK):
        raise HssdBindingError(f"{label} must be executable")
    return resolved


def _decoder_identity(
    node_path: pathlib.Path,
    transcoder_js_path: pathlib.Path,
    transcoder_wasm_path: pathlib.Path,
) -> dict[str, Any]:
    node = _regular_absolute_file(node_path, "Node executable", executable=True)
    javascript = _regular_absolute_file(transcoder_js_path, "Basis transcoder JS")
    wasm = _regular_absolute_file(transcoder_wasm_path, "Basis transcoder WASM")
    script = _regular_absolute_file(_DECODE_SCRIPT.resolve(), "repository Basis decode wrapper")
    js_sha = _sha256_file(javascript)
    wasm_sha = _sha256_file(wasm)
    pin = _BASIS_TRANSCODER_PINS.get((js_sha, wasm_sha))
    if pin is None:
        raise HssdBindingError("Basis transcoder JS/WASM hashes are not an approved offline pin")
    return {
        **pin,
        "node": {"path": str(node), "sha256": _sha256_file(node)},
        "transcoder_js": {"path": str(javascript), "sha256": js_sha},
        "transcoder_wasm": {"path": str(wasm), "sha256": wasm_sha},
        "decode_wrapper": {"path": str(script), "sha256": _sha256_file(script)},
    }


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)


def encode_rgba8_png(width: int, height: int, rgba: bytes) -> bytes:
    """Create a timestamp-free RGBA8 PNG using fixed filter and zlib policy."""

    if (
        isinstance(width, bool)
        or not isinstance(width, int)
        or isinstance(height, bool)
        or not isinstance(height, int)
        or width < 1
        or height < 1
    ):
        raise HssdBindingError("PNG dimensions must be positive integers")
    expected = width * height * 4
    if expected > _MAX_DECODED_RGBA_BYTES:
        raise HssdBindingError("decoded RGBA8 image exceeds the closed memory limit")
    if len(rgba) != expected:
        raise HssdBindingError(f"RGBA8 byte count mismatch: expected {expected}, got {len(rgba)}")
    stride = width * 4
    scanlines = b"".join(b"\x00" + rgba[offset : offset + stride] for offset in range(0, len(rgba), stride))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return _PNG_SIGNATURE + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", zlib.compress(scanlines, 9)) + _png_chunk(b"IEND", b"")


def _validate_rgba8_png(payload: bytes, label: str) -> tuple[int, int]:
    if not payload.startswith(_PNG_SIGNATURE):
        raise HssdBindingError(f"{label} does not have a PNG signature")
    offset = len(_PNG_SIGNATURE)
    width = height = 0
    idat = bytearray()
    seen_ihdr = False
    seen_iend = False
    while offset < len(payload):
        if offset + 12 > len(payload):
            raise HssdBindingError(f"{label} has a truncated PNG chunk")
        length = struct.unpack_from(">I", payload, offset)[0]
        kind = payload[offset + 4 : offset + 8]
        start = offset + 8
        end = start + length
        if end + 4 > len(payload):
            raise HssdBindingError(f"{label} PNG chunk exceeds payload")
        data = payload[start:end]
        expected_crc = struct.unpack_from(">I", payload, end)[0]
        if zlib.crc32(kind + data) & 0xFFFFFFFF != expected_crc:
            raise HssdBindingError(f"{label} PNG CRC mismatch")
        if kind == b"IHDR":
            if seen_ihdr or offset != len(_PNG_SIGNATURE) or len(data) != 13:
                raise HssdBindingError(f"{label} PNG IHDR is invalid")
            width, height, bit_depth, color_type, compression, filtering, interlace = struct.unpack(">IIBBBBB", data)
            if width < 1 or height < 1 or (bit_depth, color_type, compression, filtering, interlace) != (8, 6, 0, 0, 0):
                raise HssdBindingError(f"{label} is not non-interlaced RGBA8 PNG")
            seen_ihdr = True
        elif kind == b"IDAT":
            idat.extend(data)
        elif kind == b"IEND":
            if length != 0 or end + 4 != len(payload):
                raise HssdBindingError(f"{label} PNG IEND is invalid")
            seen_iend = True
        offset = end + 4
    if not seen_ihdr or not seen_iend or not idat:
        raise HssdBindingError(f"{label} PNG is incomplete")
    expected_scanline_bytes = height * (width * 4 + 1)
    if expected_scanline_bytes > _MAX_DECODED_RGBA_BYTES + height:
        raise HssdBindingError(f"{label} decoded image exceeds the closed memory limit")
    try:
        inflater = zlib.decompressobj()
        scanlines = inflater.decompress(bytes(idat), expected_scanline_bytes + 1)
        if inflater.unconsumed_tail or len(scanlines) > expected_scanline_bytes:
            raise HssdBindingError(f"{label} PNG expands beyond its declared dimensions")
        scanlines += inflater.flush()
    except zlib.error as error:
        raise HssdBindingError(f"{label} PNG IDAT is invalid") from error
    if not inflater.eof or inflater.unused_data or len(scanlines) > expected_scanline_bytes:
        raise HssdBindingError(f"{label} PNG compressed stream is incomplete or has trailing data")
    stride = width * 4
    if len(scanlines) != expected_scanline_bytes:
        raise HssdBindingError(f"{label} PNG scanline length is invalid")
    if any(scanlines[row * (stride + 1)] != 0 for row in range(height)):
        raise HssdBindingError(f"{label} PNG uses an unexpected scanline filter")
    return width, height


def _decode_ktx2_to_png(
    payload: bytes,
    node_path: pathlib.Path,
    transcoder_js_path: pathlib.Path,
    transcoder_wasm_path: pathlib.Path,
    decoder_identity: dict[str, Any],
) -> tuple[bytes, dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="vista-basisu-decode-") as temporary:
        root = pathlib.Path(temporary)
        source = root / "source.ktx2"
        rgba_path = root / "decoded.rgba"
        metadata_path = root / "decoded.json"
        source.write_bytes(payload)
        source.chmod(0o600)
        command = [
            str(node_path),
            str(_DECODE_SCRIPT.resolve()),
            "--transcoder-js", str(transcoder_js_path),
            "--transcoder-wasm", str(transcoder_wasm_path),
            "--input", str(source),
            "--output", str(rgba_path),
            "--metadata", str(metadata_path),
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=root,
                env={"HOME": str(root), "LANG": "C", "LC_ALL": "C", "TZ": "UTC"},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise HssdBindingError(f"offline Basis Universal decoder failed to execute: {error}") from error
        if completed.returncode != 0:
            stderr = completed.stderr.decode("utf-8", errors="replace")[-2000:]
            raise HssdBindingError(f"offline Basis Universal decoder rejected KTX2: {stderr}")
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            rgba = rgba_path.read_bytes()
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise HssdBindingError("offline Basis Universal decoder emitted invalid outputs") from error
    if not isinstance(metadata, dict) or metadata.get("schema_version") != "simworld.basisu-rgba8-decode/v1":
        raise HssdBindingError("offline Basis Universal decoder metadata schema mismatch")
    width = metadata.get("width")
    height = metadata.get("height")
    if (
        isinstance(width, bool)
        or not isinstance(width, int)
        or isinstance(height, bool)
        or not isinstance(height, int)
        or width < 1
        or height < 1
        or width * height * 4 > _MAX_DECODED_RGBA_BYTES
        or len(rgba) != width * height * 4
    ):
        raise HssdBindingError("offline Basis Universal decoder RGBA dimensions mismatch")
    if metadata.get("output_format") != "RGBA8" or metadata.get("mip_policy") != "base_level_only":
        raise HssdBindingError("offline Basis Universal decoder violated output policy")
    png = encode_rgba8_png(width, height, rgba)
    _validate_rgba8_png(png, "transcoded image")
    return png, {
        **metadata,
        "source_mime_type": "image/ktx2",
        "source_ktx2_bytes": len(payload),
        "source_ktx2_sha256": hashlib.sha256(payload).hexdigest(),
        "output_mime_type": "image/png",
        "output_png_bytes": len(png),
        "output_png_sha256": hashlib.sha256(png).hexdigest(),
        "png_encoder": {
            "policy": "rgba8_filter_none_zlib_level_9_no_metadata",
            "zlib_runtime_version": zlib.ZLIB_RUNTIME_VERSION,
        },
        "decoder": decoder_identity,
    }


def _embedded_image_bytes(
    document: dict[str, Any], binary: bytes, image: Any, index: int, *, mime_type: str, label: str
) -> bytes:
    views = document.get("bufferViews", [])
    if not isinstance(views, list) or not isinstance(image, dict):
        raise HssdBindingError(f"{label} image[{index}] is invalid")
    if image.get("mimeType") != mime_type or "uri" in image:
        raise HssdBindingError(f"{label} image[{index}] must be embedded {mime_type}")
    view_index = image.get("bufferView")
    if not isinstance(view_index, int) or not 0 <= view_index < len(views):
        raise HssdBindingError(f"{label} image[{index}] has a dangling bufferView")
    view = views[view_index]
    if not isinstance(view, dict) or view.get("buffer", 0) != 0:
        raise HssdBindingError(f"{label} image[{index}] must use embedded buffer zero")
    offset = view.get("byteOffset", 0)
    length = view.get("byteLength")
    if not isinstance(offset, int) or not isinstance(length, int) or offset < 0 or length <= 0 or offset + length > len(binary):
        raise HssdBindingError(f"{label} image[{index}] bytes are out of range")
    return binary[offset : offset + length]


def _core_textures_from_basisu(source: dict[str, Any]) -> list[dict[str, Any]]:
    source_images = source.get("images")
    source_textures = source.get("textures")
    if not isinstance(source_images, list) or not source_images or not isinstance(source_textures, list) or not source_textures:
        raise HssdBindingError("BasisU source texture arrays are invalid")
    result: list[dict[str, Any]] = []
    for index, texture in enumerate(source_textures):
        if not isinstance(texture, dict):
            raise HssdBindingError(f"BasisU source texture[{index}] is invalid")
        extensions = texture.get("extensions")
        basis = extensions.get("KHR_texture_basisu") if isinstance(extensions, dict) else None
        source_index = basis.get("source") if isinstance(basis, dict) else None
        if not isinstance(source_index, int) or not 0 <= source_index < len(source_images):
            raise HssdBindingError(f"BasisU source texture[{index}] has a dangling extension source")
        converted = copy.deepcopy(texture)
        converted["source"] = source_index
        converted_extensions = converted.get("extensions")
        if isinstance(converted_extensions, dict):
            converted_extensions.pop("KHR_texture_basisu", None)
            if not converted_extensions:
                converted.pop("extensions", None)
        result.append(converted)
    return result


def _restore_material_indices(source: dict[str, Any], output: dict[str, Any]) -> int:
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
    mapped: set[int] = set()
    for mesh in output.get("meshes", []):
        if not isinstance(mesh, dict):
            raise HssdBindingError("normalized surrogate has invalid mesh")
        for primitive in mesh.get("primitives", []):
            if not isinstance(primitive, dict):
                raise HssdBindingError("normalized surrogate has invalid primitive")
            material_index = primitive.get("material")
            if not isinstance(material_index, int) or material_index not in material_map:
                raise HssdBindingError("normalized surrogate lost a material index marker")
            primitive["material"] = material_map[material_index]
            mapped.add(material_map[material_index])
    expected = _source_used_material_indices(source)
    if mapped != expected:
        raise HssdBindingError(f"normalized surrogate material coverage drifted: expected={sorted(expected)}, actual={sorted(mapped)}")
    output["materials"] = copy.deepcopy(source_materials)
    return len(mapped)


def rehydrate_core_png_materials(
    source_path: pathlib.Path,
    normalized_surrogate_path: pathlib.Path,
    output_path: pathlib.Path,
    *,
    node_path: pathlib.Path,
    transcoder_js_path: pathlib.Path,
    transcoder_wasm_path: pathlib.Path,
) -> dict[str, Any]:
    """Restore source PBR records with UE-compatible embedded core PNGs."""

    source, source_bin = read_glb(source_path)
    output, output_bin = read_glb(normalized_surrogate_path)
    if not uses_required_basisu(source):
        raise HssdBindingError("BasisU-to-PNG transport requested for a non-BasisU source")
    # HSSD source image bufferViews may be byte-packed at non-4-byte offsets;
    # glTF permits this for opaque image payloads. Newly emitted GLB views are
    # still held to the stricter 4-byte alignment contract below.
    _validate_buffer_graph(source, source_bin, "source", require_view_alignment=False)
    decoder = _decoder_identity(node_path, transcoder_js_path, transcoder_wasm_path)
    mapped_material_count = _restore_material_indices(source, output)
    if "samplers" in source:
        output["samplers"] = copy.deepcopy(source["samplers"])
    else:
        output.pop("samplers", None)
    output["textures"] = _core_textures_from_basisu(source)

    output_views = output.setdefault("bufferViews", [])
    source_images = source.get("images")
    if not isinstance(output_views, list) or not isinstance(source_images, list) or not source_images:
        raise HssdBindingError("BasisU image/bufferView arrays are invalid")
    combined = bytearray(output_bin)
    converted_images: list[dict[str, Any]] = []
    image_receipts: list[dict[str, Any]] = []
    for image_index, image in enumerate(source_images):
        source_payload = _embedded_image_bytes(source, source_bin, image, image_index, mime_type="image/ktx2", label="source")
        png, receipt = _decode_ktx2_to_png(
            source_payload,
            pathlib.Path(decoder["node"]["path"]),
            pathlib.Path(decoder["transcoder_js"]["path"]),
            pathlib.Path(decoder["transcoder_wasm"]["path"]),
            decoder,
        )
        while len(combined) % 4:
            combined.append(0)
        destination_offset = len(combined)
        combined.extend(png)
        output_views.append({"buffer": 0, "byteOffset": destination_offset, "byteLength": len(png)})
        converted = copy.deepcopy(image)
        converted.pop("uri", None)
        converted["mimeType"] = "image/png"
        converted["bufferView"] = len(output_views) - 1
        converted_images.append(converted)
        image_receipts.append({"image_index": image_index, **receipt})
    output["images"] = converted_images
    output["buffers"] = [{"byteLength": len(combined)}]

    used = (set(output.get("extensionsUsed", [])) | set(source.get("extensionsUsed", []))) - {"KHR_texture_basisu"}
    required = (set(output.get("extensionsRequired", [])) | set(source.get("extensionsRequired", []))) - {"KHR_texture_basisu"}
    if required - used:
        raise HssdBindingError(f"required extension declarations are missing from extensionsUsed: {sorted(required - used)}")
    if used:
        output["extensionsUsed"] = sorted(used)
    else:
        output.pop("extensionsUsed", None)
    if required:
        output["extensionsRequired"] = sorted(required)
    else:
        output.pop("extensionsRequired", None)
    write_glb(output_path, output, bytes(combined))
    validation = validate_core_png_glb(source_path, output_path, expected_image_receipts=image_receipts)
    return {
        "mode": "KHR_texture_basisu_to_core_png",
        "mechanism": "offline_hash_pinned_basisu_wasm_base_level_rgba8_to_embedded_core_png",
        "source_basisu_required": True,
        "output_basisu_required": False,
        "blender_decoded_textures": False,
        "mapped_material_count": mapped_material_count,
        "converted_image_count": len(converted_images),
        "decoder": decoder,
        "self_contained": validation["self_contained"],
        "single_buffer": validation["single_buffer"],
        "single_mesh": validation["single_mesh"],
        "buffer_views_aligned_and_in_range": validation["buffer_views_aligned_and_in_range"],
        "primitive_material_indices_valid": validation["primitive_material_indices_valid"],
        "core_texture_sources_valid": validation["core_texture_sources_valid"],
        "embedded_png_images_valid": validation["embedded_png_images_valid"],
        "extension_declarations_complete": validation["extension_declarations_complete"],
        "base_normal_orm_texture_slots": validation["base_normal_orm_texture_slots"],
        "image_payloads": validation["image_payloads"],
    }


def _validate_buffer_graph(
    document: dict[str, Any], binary: bytes, label: str, *, require_view_alignment: bool = True
) -> None:
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
        if (require_view_alignment and offset % 4 != 0) or offset + length > byte_length:
            raise HssdBindingError(f"{label} bufferView[{index}] is unaligned or out of range")


def _validate_texture_info(info: Any, texture_count: int, label: str) -> bool:
    if not isinstance(info, dict) or not isinstance(info.get("index"), int):
        raise HssdBindingError(f"{label} texture info is invalid")
    if not 0 <= info["index"] < texture_count:
        raise HssdBindingError(f"{label} texture index is dangling")
    return True


def _validate_material_texture_indices(materials: Any, texture_count: int) -> int:
    if not isinstance(materials, list) or not materials:
        raise HssdBindingError("transported GLB must contain materials")
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
        raise HssdBindingError("transported GLB lacks baseColor/normal/ORM texture slots")
    return base_normal_orm_slots


def _validate_mesh_indices(document: dict[str, Any]) -> None:
    materials = document.get("materials", [])
    accessors = document.get("accessors", [])
    meshes = document.get("meshes")
    if not isinstance(accessors, list) or not isinstance(meshes, list) or len(meshes) != 1:
        raise HssdBindingError("transported output must contain exactly one mesh")
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


def validate_core_png_glb(
    source_path: pathlib.Path,
    output_path: pathlib.Path,
    *,
    expected_image_receipts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Fail closed unless output uses embedded, core glTF RGBA8 PNG images."""

    source, source_bin = read_glb(source_path)
    output, output_bin = read_glb(output_path)
    if not uses_required_basisu(source):
        raise HssdBindingError("source does not require KHR_texture_basisu")
    _validate_buffer_graph(source, source_bin, "source", require_view_alignment=False)
    _validate_buffer_graph(output, output_bin, "output")
    _validate_mesh_indices(output)

    textures = output.get("textures")
    images = output.get("images")
    samplers = output.get("samplers", [])
    if not isinstance(textures, list) or not textures or not isinstance(images, list) or not images or not isinstance(samplers, list):
        raise HssdBindingError("core PNG texture arrays are invalid")
    expected_textures = _core_textures_from_basisu(source)
    if textures != expected_textures:
        raise HssdBindingError("core PNG texture records do not preserve source sampler/extension semantics")
    for texture_index, texture in enumerate(textures):
        if not isinstance(texture, dict):
            raise HssdBindingError(f"texture[{texture_index}] is invalid")
        source_index = texture.get("source")
        if not isinstance(source_index, int) or not 0 <= source_index < len(images):
            raise HssdBindingError(f"texture[{texture_index}] has dangling core source")
        sampler = texture.get("sampler")
        if sampler is not None and (not isinstance(sampler, int) or not 0 <= sampler < len(samplers)):
            raise HssdBindingError(f"texture[{texture_index}] has dangling sampler")
        if "KHR_texture_basisu" in texture.get("extensions", {}):
            raise HssdBindingError(f"texture[{texture_index}] retains KHR_texture_basisu")

    source_materials = source.get("materials")
    if output.get("materials") != source_materials:
        raise HssdBindingError("core PNG output does not preserve exact source PBR materials")
    base_normal_orm_slots = _validate_material_texture_indices(source_materials, len(textures))

    source_images = source.get("images")
    if not isinstance(source_images, list) or len(source_images) != len(images):
        raise HssdBindingError("core PNG image count does not match source")
    expected_by_index: dict[int, dict[str, Any]] = {}
    if expected_image_receipts is not None:
        for receipt in expected_image_receipts:
            index = receipt.get("image_index") if isinstance(receipt, dict) else None
            if not isinstance(index, int) or index in expected_by_index:
                raise HssdBindingError("expected image receipts have invalid or duplicate indices")
            expected_by_index[index] = receipt
        if set(expected_by_index) != set(range(len(images))):
            raise HssdBindingError("expected image receipts do not cover every output image")

    image_receipts: list[dict[str, Any]] = []
    for image_index, (source_image, output_image) in enumerate(zip(source_images, images, strict=True)):
        source_payload = _embedded_image_bytes(
            source, source_bin, source_image, image_index, mime_type="image/ktx2", label="source"
        )
        output_payload = _embedded_image_bytes(
            output, output_bin, output_image, image_index, mime_type="image/png", label="output"
        )
        width, height = _validate_rgba8_png(output_payload, f"output image[{image_index}]")
        source_sha = hashlib.sha256(source_payload).hexdigest()
        output_sha = hashlib.sha256(output_payload).hexdigest()
        source_metadata = copy.deepcopy(source_image)
        output_metadata = copy.deepcopy(output_image)
        source_metadata.pop("bufferView", None)
        source_metadata.pop("mimeType", None)
        output_metadata.pop("bufferView", None)
        output_metadata.pop("mimeType", None)
        if source_metadata != output_metadata:
            raise HssdBindingError(f"output image[{image_index}] metadata drifted during transcode")
        expected = expected_by_index.get(image_index)
        if expected is not None and (
            expected.get("source_ktx2_sha256") != source_sha
            or expected.get("output_png_sha256") != output_sha
            or expected.get("width") != width
            or expected.get("height") != height
            or expected.get("source_mime_type") != "image/ktx2"
            or expected.get("output_mime_type") != "image/png"
        ):
            raise HssdBindingError(f"output image[{image_index}] does not match transcode receipt")
        image_receipts.append({
            "image_index": image_index,
            "width": width,
            "height": height,
            "source_ktx2_bytes": len(source_payload),
            "source_ktx2_sha256": source_sha,
            "output_png_bytes": len(output_payload),
            "output_png_sha256": output_sha,
            **({"transcode": expected} if expected is not None else {}),
        })

    used = output.get("extensionsUsed", [])
    required = output.get("extensionsRequired", [])
    if not isinstance(used, list) or not isinstance(required, list) or not set(required).issubset(set(used)):
        raise HssdBindingError("core PNG extension declarations are invalid")
    referenced_extensions = _collect_extension_keys(output)
    if "KHR_texture_basisu" in set(used) | set(required) | referenced_extensions:
        raise HssdBindingError("core PNG output still declares or references KHR_texture_basisu")
    if not referenced_extensions.issubset(set(used)):
        raise HssdBindingError(f"extensionsUsed misses referenced extensions: {sorted(referenced_extensions - set(used))}")
    return {
        "mode": "KHR_texture_basisu_to_core_png",
        "self_contained": True,
        "single_buffer": True,
        "single_mesh": True,
        "buffer_views_aligned_and_in_range": True,
        "primitive_material_indices_valid": True,
        "core_texture_sources_valid": True,
        "embedded_png_images_valid": True,
        "extension_declarations_complete": True,
        "base_normal_orm_texture_slots": base_normal_orm_slots,
        "image_payloads": image_receipts,
    }
