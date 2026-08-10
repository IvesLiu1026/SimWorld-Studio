#!/usr/bin/env python3
"""Validate a VISTA Blender asset bundle without importing Blender.

The validator deliberately uses only the Python standard library so it can run
before Blender is started and in the UE import planner.  It verifies the
cryptographic binding between the manifest and every output, inspects the GLB
container, and decodes the two PNG previews far enough to reject blank frames.
"""

from __future__ import annotations

import argparse
import binascii
import hashlib
import json
import math
import pathlib
import re
import struct
import sys
import zlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

SCHEMA = "simworld.vista.blender-asset-manifest/v1"
EXPECTED_ASSET_ID = "vista_mmg040_office_r1"
EXPECTED_BLENDER_VERSION = "4.5.8"
EXPECTED_SCRIPT = "tools/blender/build_vista_mmg040_office.py"
EXPECTED_COLLECTIONS = {
    "VISTA_ErgonomicOfficeChair",
    "VISTA_RoomShell",
    "VISTA_TallOfficeCabinet",
}
EXPECTED_ASSETS = {
    "ergonomic_office_chair": "VISTA_ErgonomicOfficeChair",
    "room_shell_kit": "VISTA_RoomShell",
    "tall_office_cabinet": "VISTA_TallOfficeCabinet",
}
EXPECTED_ASSET_PLACEMENT = {
    "ergonomic_office_chair": {
        "root_node": "VISTA_Chair_Root",
        "origin_m": (-0.4, -0.8, 0.0),
        "location_cm": (220.0, -40.0, 0.0),
    },
    "room_shell_kit": {
        "root_node": "VISTA_Room_Root",
        "origin_m": (0.0, 0.0, 0.0),
        "location_cm": (300.0, 0.0, 0.0),
    },
    "tall_office_cabinet": {
        "root_node": "VISTA_Cabinet_Root",
        "origin_m": (0.0, 2.14, 0.0),
        "location_cm": (514.0, 0.0, 0.0),
    },
}
EXPECTED_OUTPUTS = {
    "blend": ("source.blend", "application/x-blender"),
    "glb": ("vista_mmg040_office.glb", "model/gltf-binary"),
    "gltf": ("vista_mmg040_office.gltf", "model/gltf+json"),
    "gltf_bin": ("vista_mmg040_office.bin", "application/octet-stream"),
    "preview_overview": ("preview-overview.png", "image/png"),
    "preview_detail": ("preview-detail.png", "image/png"),
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
GLB_JSON_CHUNK = 0x4E4F534A
GLB_BIN_CHUNK = 0x004E4942
COMPONENT_FORMATS = {
    5120: ("b", 1),
    5121: ("B", 1),
    5122: ("h", 2),
    5123: ("H", 2),
    5125: ("I", 4),
    5126: ("f", 4),
}
ACCESSOR_SHAPES = {
    "SCALAR": (1, 1),
    "VEC2": (1, 2),
    "VEC3": (1, 3),
    "VEC4": (1, 4),
    "MAT2": (2, 2),
    "MAT3": (3, 3),
    "MAT4": (4, 4),
}
BOUNDS_ABS_TOLERANCE_M = 2e-5
BOUNDS_REL_TOLERANCE = 2e-6


class AssetValidationError(ValueError):
    """A stable, machine-readable validation failure."""

    def __init__(self, code: str, message: str, *, pointer: str = "$") -> None:
        super().__init__(message)
        self.code = code
        self.pointer = pointer

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": str(self), "pointer": self.pointer}


@dataclass(frozen=True)
class PngInspection:
    width: int
    height: int
    channel_range: int
    luminance_range: int
    unique_sampled_colors: int
    opaque_fraction: float


@dataclass(frozen=True)
class GlbInspection:
    mesh_count: int
    material_count: int
    triangle_count: int
    mesh_names: tuple[str, ...]
    material_names: tuple[str, ...]
    bounds_min: tuple[float, float, float]
    bounds_max: tuple[float, float, float]

    @property
    def dimensions(self) -> tuple[float, float, float]:
        return tuple(
            self.bounds_max[index] - self.bounds_min[index] for index in range(3)
        )


@dataclass(frozen=True)
class AccessorLayout:
    component_type: int
    element_type: str
    count: int
    component_count: int
    component_format: str
    component_size: int
    element_offsets: tuple[int, ...]
    element_size: int
    stride: int
    absolute_offset: int
    normalized: bool
    pointer: str


def fail(code: str, message: str, *, pointer: str = "$") -> None:
    raise AssetValidationError(code, message, pointer=pointer)


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            fail("MANIFEST_DUPLICATE_KEY", f"Duplicate JSON key: {key}")
        value[key] = item
    return value


def strict_json_bytes(raw: bytes, *, code: str = "MANIFEST_JSON_INVALID") -> Any:
    try:
        return json.loads(
            raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_pairs
        )
    except AssetValidationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        fail(code, f"Invalid UTF-8 JSON: {error}")


def _mapping(value: Any, pointer: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        fail("MANIFEST_TYPE_INVALID", "Expected an object", pointer=pointer)
    return value


def _list(value: Any, pointer: str) -> list[Any]:
    if not isinstance(value, list):
        fail("MANIFEST_TYPE_INVALID", "Expected an array", pointer=pointer)
    return value


def _string(value: Any, pointer: str) -> str:
    if not isinstance(value, str) or not value:
        fail("MANIFEST_TYPE_INVALID", "Expected a non-empty string", pointer=pointer)
    return value


def _integer(value: Any, pointer: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        fail(
            "MANIFEST_TYPE_INVALID",
            f"Expected an integer >= {minimum}",
            pointer=pointer,
        )
    return value


def _finite_vector(value: Any, pointer: str) -> tuple[float, float, float]:
    items = _list(value, pointer)
    if len(items) != 3:
        fail(
            "MANIFEST_BOUNDS_INVALID",
            "Expected a three-element vector",
            pointer=pointer,
        )
    result: list[float] = []
    for index, item in enumerate(items):
        if (
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(float(item))
        ):
            fail(
                "MANIFEST_BOUNDS_INVALID",
                "Bounds must contain finite numbers",
                pointer=f"{pointer}/{index}",
            )
        result.append(float(item))
    return tuple(result)  # type: ignore[return-value]


def _validate_bounds(
    value: Any, pointer: str
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    bounds = _mapping(value, pointer)
    if set(bounds) != {"min", "max", "dimensions"}:
        fail(
            "MANIFEST_BOUNDS_INVALID",
            "Bounds require exactly min, max and dimensions",
            pointer=pointer,
        )
    minimum = _finite_vector(bounds["min"], f"{pointer}/min")
    maximum = _finite_vector(bounds["max"], f"{pointer}/max")
    dimensions = _finite_vector(bounds["dimensions"], f"{pointer}/dimensions")
    for index, (lower, upper, size) in enumerate(zip(minimum, maximum, dimensions)):
        if (
            upper <= lower
            or size <= 0.0
            or not math.isclose(upper - lower, size, rel_tol=1e-6, abs_tol=1e-6)
        ):
            fail(
                "MANIFEST_BOUNDS_INVALID",
                "Bounds must be finite, non-zero and self-consistent",
                pointer=f"{pointer}/{index}",
            )
    return minimum, maximum


def _safe_relative(base: pathlib.Path, value: Any, pointer: str) -> pathlib.Path:
    relative_text = _string(value, pointer)
    relative = pathlib.PurePosixPath(relative_text)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        fail(
            "OUTPUT_PATH_INVALID",
            "Output paths must be normalized relative POSIX paths",
            pointer=pointer,
        )
    candidate = base.joinpath(*relative.parts)
    try:
        candidate.resolve(strict=False).relative_to(base.resolve(strict=True))
    except (OSError, ValueError):
        fail(
            "OUTPUT_PATH_INVALID",
            "Output path escapes the manifest directory",
            pointer=pointer,
        )
    if candidate.is_symlink():
        fail(
            "OUTPUT_PATH_INVALID",
            "Output files may not be symbolic links",
            pointer=pointer,
        )
    return candidate


def _paeth(left: int, above: int, upper_left: int) -> int:
    estimate = left + above - upper_left
    left_distance = abs(estimate - left)
    above_distance = abs(estimate - above)
    upper_left_distance = abs(estimate - upper_left)
    if left_distance <= above_distance and left_distance <= upper_left_distance:
        return left
    if above_distance <= upper_left_distance:
        return above
    return upper_left


def inspect_png(path: pathlib.Path) -> PngInspection:
    raw = path.read_bytes()
    if not raw.startswith(PNG_SIGNATURE):
        fail("PREVIEW_PNG_INVALID", "Preview is not a PNG", pointer=str(path))

    offset = len(PNG_SIGNATURE)
    ihdr: tuple[int, int, int, int, int, int, int] | None = None
    compressed = bytearray()
    saw_end = False
    while offset < len(raw):
        if offset + 12 > len(raw):
            fail("PREVIEW_PNG_INVALID", "Truncated PNG chunk", pointer=str(path))
        length = struct.unpack_from(">I", raw, offset)[0]
        chunk_type = raw[offset + 4 : offset + 8]
        data_start = offset + 8
        data_end = data_start + length
        crc_end = data_end + 4
        if crc_end > len(raw):
            fail("PREVIEW_PNG_INVALID", "Truncated PNG chunk data", pointer=str(path))
        chunk_data = raw[data_start:data_end]
        expected_crc = struct.unpack_from(">I", raw, data_end)[0]
        actual_crc = binascii.crc32(chunk_type + chunk_data) & 0xFFFFFFFF
        if expected_crc != actual_crc:
            fail("PREVIEW_PNG_INVALID", "PNG chunk CRC mismatch", pointer=str(path))
        if chunk_type == b"IHDR":
            if ihdr is not None or len(chunk_data) != 13:
                fail("PREVIEW_PNG_INVALID", "Invalid IHDR chunk", pointer=str(path))
            ihdr = struct.unpack(">IIBBBBB", chunk_data)
        elif chunk_type == b"IDAT":
            compressed.extend(chunk_data)
        elif chunk_type == b"IEND":
            saw_end = True
            break
        offset = crc_end

    if ihdr is None or not compressed or not saw_end:
        fail(
            "PREVIEW_PNG_INVALID", "PNG requires IHDR, IDAT and IEND", pointer=str(path)
        )
    width, height, bit_depth, color_type, compression, filtering, interlace = ihdr
    if width < 320 or height < 320:
        fail(
            "PREVIEW_RESOLUTION_INVALID",
            "Preview must be at least 320x320",
            pointer=str(path),
        )
    channels = {2: 3, 6: 4}.get(color_type)
    if (
        bit_depth != 8
        or channels is None
        or compression != 0
        or filtering != 0
        or interlace != 0
    ):
        fail(
            "PREVIEW_PNG_UNSUPPORTED",
            "Preview must be non-interlaced 8-bit RGB or RGBA PNG",
            pointer=str(path),
        )

    try:
        decoded = zlib.decompress(bytes(compressed))
    except zlib.error as error:
        fail(
            "PREVIEW_PNG_INVALID",
            f"PNG decompression failed: {error}",
            pointer=str(path),
        )
    row_bytes = width * channels
    expected_bytes = height * (row_bytes + 1)
    if len(decoded) != expected_bytes:
        fail(
            "PREVIEW_PNG_INVALID",
            "Unexpected decoded PNG byte count",
            pointer=str(path),
        )

    reconstructed = bytearray(width * height * channels)
    previous = bytearray(row_bytes)
    input_offset = 0
    output_offset = 0
    for _row in range(height):
        filter_type = decoded[input_offset]
        input_offset += 1
        source = decoded[input_offset : input_offset + row_bytes]
        input_offset += row_bytes
        current = bytearray(row_bytes)
        for index, byte in enumerate(source):
            left = current[index - channels] if index >= channels else 0
            above = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            if filter_type == 0:
                predictor = 0
            elif filter_type == 1:
                predictor = left
            elif filter_type == 2:
                predictor = above
            elif filter_type == 3:
                predictor = (left + above) // 2
            elif filter_type == 4:
                predictor = _paeth(left, above, upper_left)
            else:
                fail(
                    "PREVIEW_PNG_INVALID",
                    f"Unknown PNG filter {filter_type}",
                    pointer=str(path),
                )
            current[index] = (byte + predictor) & 0xFF
        reconstructed[output_offset : output_offset + row_bytes] = current
        output_offset += row_bytes
        previous = current

    pixel_count = width * height
    sample_stride = max(1, pixel_count // 100_000)
    channel_min = 255
    channel_max = 0
    luminance_min = 255
    luminance_max = 0
    opaque = 0
    sampled = 0
    colors: set[tuple[int, int, int]] = set()
    for pixel_index in range(0, pixel_count, sample_stride):
        start = pixel_index * channels
        red, green, blue = reconstructed[start : start + 3]
        alpha = reconstructed[start + 3] if channels == 4 else 255
        channel_min = min(channel_min, red, green, blue)
        channel_max = max(channel_max, red, green, blue)
        luminance = (54 * red + 183 * green + 19 * blue) // 256
        luminance_min = min(luminance_min, luminance)
        luminance_max = max(luminance_max, luminance)
        opaque += int(alpha >= 16)
        sampled += 1
        if len(colors) < 4096:
            colors.add((red, green, blue))

    inspection = PngInspection(
        width=width,
        height=height,
        channel_range=channel_max - channel_min,
        luminance_range=luminance_max - luminance_min,
        unique_sampled_colors=len(colors),
        opaque_fraction=opaque / max(sampled, 1),
    )
    if (
        inspection.channel_range < 12
        or inspection.luminance_range < 8
        or inspection.unique_sampled_colors < 16
        or inspection.opaque_fraction < 0.95
    ):
        fail(
            "PREVIEW_BLANK",
            "Preview is blank, transparent or lacks visible tonal variation",
            pointer=str(path),
        )
    return inspection


def _glb_payload(raw: bytes) -> tuple[dict[str, Any], bytes]:
    if len(raw) < 28:
        fail("GLB_INVALID", "GLB is too short to contain JSON and BIN chunks")
    magic, version, declared_length = struct.unpack_from("<4sII", raw, 0)
    if magic != b"glTF" or version != 2 or declared_length != len(raw):
        fail("GLB_INVALID", "Invalid GLB 2.0 header")

    offset = 12
    chunks: list[tuple[int, bytes]] = []
    while offset < len(raw):
        if offset + 8 > len(raw):
            fail("GLB_INVALID", "Truncated GLB chunk header")
        length, chunk_type = struct.unpack_from("<II", raw, offset)
        offset += 8
        end = offset + length
        if length == 0 or length % 4 or end > len(raw):
            fail("GLB_INVALID", "Invalid GLB chunk length")
        chunks.append((chunk_type, raw[offset:end]))
        offset = end
    if offset != len(raw):
        fail("GLB_INVALID", "GLB chunk table does not consume the declared file")
    if (
        len(chunks) != 2
        or chunks[0][0] != GLB_JSON_CHUNK
        or chunks[1][0] != GLB_BIN_CHUNK
    ):
        fail(
            "GLB_BIN_REQUIRED",
            "Production GLB requires one JSON chunk followed by one BIN chunk",
        )

    json_chunk = chunks[0][1]
    if not json_chunk.rstrip(b" "):
        fail("GLB_JSON_INVALID", "GLB JSON chunk is empty")
    parsed = strict_json_bytes(json_chunk.rstrip(b" "), code="GLB_JSON_INVALID")
    return _mapping(parsed, "$/glb"), chunks[1][1]


def _contains_light_extension(value: Any) -> bool:
    if value == "KHR_lights_punctual":
        return True
    if isinstance(value, dict):
        return "KHR_lights_punctual" in value or any(
            _contains_light_extension(item) for item in value.values()
        )
    if isinstance(value, list):
        return any(_contains_light_extension(item) for item in value)
    return False


def _accessor_element_offsets(
    component_size: int, element_type: str
) -> tuple[tuple[int, ...], int]:
    columns, rows = ACCESSOR_SHAPES[element_type]
    if columns == 1:
        offsets = tuple(component * component_size for component in range(rows))
        return offsets, rows * component_size
    column_size = rows * component_size
    column_stride = (column_size + 3) & ~3
    offsets = tuple(
        column * column_stride + row * component_size
        for column in range(columns)
        for row in range(rows)
    )
    return offsets, columns * column_stride


def _iter_accessor_values(
    layout: AccessorLayout, binary: bytes
) -> Iterable[tuple[int | float, ...]]:
    unpack = struct.Struct("<" + layout.component_format)
    for item_index in range(layout.count):
        item_offset = layout.absolute_offset + item_index * layout.stride
        values: list[int | float] = []
        for component_offset in layout.element_offsets:
            value = unpack.unpack_from(binary, item_offset + component_offset)[0]
            if isinstance(value, float) and not math.isfinite(value):
                fail(
                    "GLB_ACCESSOR_PAYLOAD_INVALID",
                    "Accessor payload contains NaN or infinity",
                    pointer=layout.pointer,
                )
            values.append(value)
        yield tuple(values)


def _accessor_payload_bounds(
    layout: AccessorLayout, binary: bytes
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    minimum = [math.inf] * layout.component_count
    maximum = [-math.inf] * layout.component_count
    for values in _iter_accessor_values(layout, binary):
        for index, value in enumerate(values):
            numeric = float(value)
            minimum[index] = min(minimum[index], numeric)
            maximum[index] = max(maximum[index], numeric)
    return tuple(minimum), tuple(maximum)


def _number_array(value: Any, pointer: str, *, length: int) -> tuple[float, ...]:
    items = _list(value, pointer)
    if len(items) != length:
        fail(
            "GLB_ACCESSOR_BOUNDS_INVALID",
            f"Expected {length} accessor bound components",
            pointer=pointer,
        )
    result: list[float] = []
    for index, item in enumerate(items):
        if (
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(float(item))
        ):
            fail(
                "GLB_ACCESSOR_BOUNDS_INVALID",
                "Accessor bounds must be finite numbers",
                pointer=f"{pointer}/{index}",
            )
        result.append(float(item))
    return tuple(result)


def _validate_binary_layout(
    gltf: dict[str, Any],
    binary: bytes,
    *,
    pointer: str,
    expected_uri: str | None,
) -> tuple[AccessorLayout, ...]:
    buffers = _list(gltf.get("buffers"), f"{pointer}/buffers")
    if len(buffers) != 1:
        fail(
            "GLB_BUFFER_INVALID",
            "VISTA assets require exactly one binary buffer",
            pointer=f"{pointer}/buffers",
        )
    buffer = _mapping(buffers[0], f"{pointer}/buffers/0")
    declared_length = _integer(
        buffer.get("byteLength"), f"{pointer}/buffers/0/byteLength", minimum=1
    )
    if expected_uri is None:
        if "uri" in buffer:
            fail(
                "GLB_BUFFER_INVALID",
                "A GLB buffer must be bound to its BIN chunk, not a URI",
                pointer=f"{pointer}/buffers/0/uri",
            )
        padding = len(binary) - declared_length
        if (
            padding < 0
            or padding > 3
            or (padding and binary[declared_length:] != b"\x00" * padding)
        ):
            fail(
                "GLB_BUFFER_INVALID",
                "GLB BIN chunk length/padding differs from buffers[0].byteLength",
                pointer=f"{pointer}/buffers/0",
            )
    else:
        if buffer.get("uri") != expected_uri:
            fail(
                "GLTF_BUFFER_INVALID",
                "Separate glTF must reference its manifest-bound sibling .bin",
                pointer=f"{pointer}/buffers/0/uri",
            )
        if declared_length != len(binary):
            fail(
                "GLTF_BUFFER_INVALID",
                "Separate glTF buffer size differs from the bound .bin",
                pointer=f"{pointer}/buffers/0/byteLength",
            )

    raw_views = _list(gltf.get("bufferViews"), f"{pointer}/bufferViews")
    if not raw_views:
        fail(
            "GLB_BUFFER_VIEW_INVALID",
            "Binary geometry requires bufferViews",
            pointer=f"{pointer}/bufferViews",
        )
    views: list[tuple[int, int, int | None]] = []
    for view_index, raw_view in enumerate(raw_views):
        view_pointer = f"{pointer}/bufferViews/{view_index}"
        view = _mapping(raw_view, view_pointer)
        if _integer(view.get("buffer"), f"{view_pointer}/buffer") != 0:
            fail(
                "GLB_BUFFER_VIEW_INVALID",
                "bufferView references an unavailable buffer",
                pointer=f"{view_pointer}/buffer",
            )
        byte_offset = _integer(view.get("byteOffset", 0), f"{view_pointer}/byteOffset")
        byte_length = _integer(
            view.get("byteLength"), f"{view_pointer}/byteLength", minimum=1
        )
        if byte_offset + byte_length > declared_length:
            fail(
                "GLB_BUFFER_VIEW_INVALID",
                "bufferView exceeds the declared binary buffer",
                pointer=view_pointer,
            )
        raw_stride = view.get("byteStride")
        stride = (
            None
            if raw_stride is None
            else _integer(raw_stride, f"{view_pointer}/byteStride", minimum=4)
        )
        if stride is not None and (stride > 252 or stride % 4):
            fail(
                "GLB_BUFFER_VIEW_INVALID",
                "bufferView byteStride must be a 4-byte multiple from 4 through 252",
                pointer=f"{view_pointer}/byteStride",
            )
        target = view.get("target")
        if target is not None and target not in {34962, 34963}:
            fail(
                "GLB_BUFFER_VIEW_INVALID",
                "bufferView target is not ARRAY_BUFFER or ELEMENT_ARRAY_BUFFER",
                pointer=f"{view_pointer}/target",
            )
        views.append((byte_offset, byte_length, stride))

    accessors = _list(gltf.get("accessors"), f"{pointer}/accessors")
    if not accessors:
        fail(
            "GLB_ACCESSOR_INVALID",
            "Binary geometry requires accessors",
            pointer=f"{pointer}/accessors",
        )
    layouts: list[AccessorLayout] = []
    for accessor_index, raw_accessor in enumerate(accessors):
        accessor_pointer = f"{pointer}/accessors/{accessor_index}"
        accessor = _mapping(raw_accessor, accessor_pointer)
        if "sparse" in accessor:
            fail(
                "GLB_ACCESSOR_UNSUPPORTED",
                "Sparse accessors are not accepted in the canonical VISTA asset",
                pointer=f"{accessor_pointer}/sparse",
            )
        view_index = _integer(
            accessor.get("bufferView"), f"{accessor_pointer}/bufferView"
        )
        if view_index >= len(views):
            fail(
                "GLB_ACCESSOR_INVALID",
                "Accessor bufferView is out of range",
                pointer=f"{accessor_pointer}/bufferView",
            )
        component_type = _integer(
            accessor.get("componentType"), f"{accessor_pointer}/componentType"
        )
        if component_type not in COMPONENT_FORMATS:
            fail(
                "GLB_ACCESSOR_INVALID",
                "Unsupported accessor componentType",
                pointer=f"{accessor_pointer}/componentType",
            )
        element_type = _string(accessor.get("type"), f"{accessor_pointer}/type")
        if element_type not in ACCESSOR_SHAPES:
            fail(
                "GLB_ACCESSOR_INVALID",
                "Unsupported accessor type",
                pointer=f"{accessor_pointer}/type",
            )
        count = _integer(accessor.get("count"), f"{accessor_pointer}/count", minimum=1)
        normalized = accessor.get("normalized", False)
        if not isinstance(normalized, bool):
            fail(
                "GLB_ACCESSOR_INVALID",
                "Accessor normalized must be boolean",
                pointer=f"{accessor_pointer}/normalized",
            )

        component_format, component_size = COMPONENT_FORMATS[component_type]
        element_offsets, element_size = _accessor_element_offsets(
            component_size, element_type
        )
        view_offset, view_length, view_stride = views[view_index]
        stride = view_stride if view_stride is not None else element_size
        if stride < element_size:
            fail(
                "GLB_ACCESSOR_INVALID",
                "Accessor element does not fit its bufferView byteStride",
                pointer=accessor_pointer,
            )
        accessor_offset = _integer(
            accessor.get("byteOffset", 0), f"{accessor_pointer}/byteOffset"
        )
        if (
            accessor_offset % component_size
            or (view_offset + accessor_offset) % component_size
        ):
            fail(
                "GLB_ACCESSOR_INVALID",
                "Accessor offset is not aligned to its component size",
                pointer=f"{accessor_pointer}/byteOffset",
            )
        relative_end = accessor_offset + (count - 1) * stride + element_size
        if relative_end > view_length:
            fail(
                "GLB_ACCESSOR_INVALID",
                "Accessor payload exceeds its bufferView",
                pointer=accessor_pointer,
            )
        layout = AccessorLayout(
            component_type=component_type,
            element_type=element_type,
            count=count,
            component_count=len(element_offsets),
            component_format=component_format,
            component_size=component_size,
            element_offsets=element_offsets,
            element_size=element_size,
            stride=stride,
            absolute_offset=view_offset + accessor_offset,
            normalized=normalized,
            pointer=accessor_pointer,
        )
        layouts.append(layout)

        has_minimum = "min" in accessor
        has_maximum = "max" in accessor
        if has_minimum != has_maximum:
            fail(
                "GLB_ACCESSOR_BOUNDS_INVALID",
                "Accessor min and max must either both be present or both be absent",
                pointer=accessor_pointer,
            )
        actual_minimum, actual_maximum = _accessor_payload_bounds(layout, binary)
        if has_minimum:
            declared_minimum = _number_array(
                accessor["min"],
                f"{accessor_pointer}/min",
                length=layout.component_count,
            )
            declared_maximum = _number_array(
                accessor["max"],
                f"{accessor_pointer}/max",
                length=layout.component_count,
            )
            for component_index, (
                declared_min,
                declared_max,
                actual_min,
                actual_max,
            ) in enumerate(
                zip(declared_minimum, declared_maximum, actual_minimum, actual_maximum)
            ):
                if declared_max < declared_min:
                    fail(
                        "GLB_ACCESSOR_BOUNDS_INVALID",
                        "Accessor max is below min",
                        pointer=f"{accessor_pointer}/{component_index}",
                    )
                if not math.isclose(
                    declared_min,
                    actual_min,
                    rel_tol=BOUNDS_REL_TOLERANCE,
                    abs_tol=BOUNDS_ABS_TOLERANCE_M,
                ) or not math.isclose(
                    declared_max,
                    actual_max,
                    rel_tol=BOUNDS_REL_TOLERANCE,
                    abs_tol=BOUNDS_ABS_TOLERANCE_M,
                ):
                    fail(
                        "GLB_ACCESSOR_BOUNDS_MISMATCH",
                        "Accessor bounds differ from decoded BIN payload",
                        pointer=accessor_pointer,
                    )
    return tuple(layouts)


def _identity_matrix() -> tuple[float, ...]:
    return (
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
    )


def _matrix_multiply(
    left: tuple[float, ...], right: tuple[float, ...]
) -> tuple[float, ...]:
    return tuple(
        sum(left[row * 4 + inner] * right[inner * 4 + column] for inner in range(4))
        for row in range(4)
        for column in range(4)
    )


def _finite_sequence(value: Any, pointer: str, *, length: int) -> tuple[float, ...]:
    items = _list(value, pointer)
    if len(items) != length:
        fail(
            "GLB_NODE_TRANSFORM_INVALID",
            f"Expected {length} transform components",
            pointer=pointer,
        )
    result: list[float] = []
    for index, item in enumerate(items):
        if (
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(float(item))
        ):
            fail(
                "GLB_NODE_TRANSFORM_INVALID",
                "Node transform must contain finite numbers",
                pointer=f"{pointer}/{index}",
            )
        result.append(float(item))
    return tuple(result)


def _node_local_matrix(node: dict[str, Any], pointer: str) -> tuple[float, ...]:
    if "matrix" in node:
        if any(key in node for key in ("translation", "rotation", "scale")):
            fail(
                "GLB_NODE_TRANSFORM_INVALID",
                "Node matrix cannot be combined with TRS properties",
                pointer=pointer,
            )
        column_major = _finite_sequence(node["matrix"], f"{pointer}/matrix", length=16)
        return tuple(
            column_major[column * 4 + row] for row in range(4) for column in range(4)
        )

    translation = _finite_sequence(
        node.get("translation", [0.0, 0.0, 0.0]), f"{pointer}/translation", length=3
    )
    rotation = _finite_sequence(
        node.get("rotation", [0.0, 0.0, 0.0, 1.0]), f"{pointer}/rotation", length=4
    )
    scale = _finite_sequence(
        node.get("scale", [1.0, 1.0, 1.0]), f"{pointer}/scale", length=3
    )
    if any(value == 0.0 for value in scale):
        fail(
            "GLB_NODE_TRANSFORM_INVALID",
            "Node scale must be non-zero",
            pointer=f"{pointer}/scale",
        )
    x, y, z, w = rotation
    norm = math.sqrt(x * x + y * y + z * z + w * w)
    if not math.isclose(norm, 1.0, rel_tol=1e-6, abs_tol=1e-6):
        fail(
            "GLB_NODE_TRANSFORM_INVALID",
            "Node rotation quaternion must be normalized",
            pointer=f"{pointer}/rotation",
        )
    sx, sy, sz = scale
    return (
        (1.0 - 2.0 * (y * y + z * z)) * sx,
        (2.0 * (x * y - z * w)) * sy,
        (2.0 * (x * z + y * w)) * sz,
        translation[0],
        (2.0 * (x * y + z * w)) * sx,
        (1.0 - 2.0 * (x * x + z * z)) * sy,
        (2.0 * (y * z - x * w)) * sz,
        translation[1],
        (2.0 * (x * z - y * w)) * sx,
        (2.0 * (y * z + x * w)) * sy,
        (1.0 - 2.0 * (x * x + y * y)) * sz,
        translation[2],
        0.0,
        0.0,
        0.0,
        1.0,
    )


def _transform_point(
    matrix: tuple[float, ...], point: tuple[int | float, ...]
) -> tuple[float, float, float]:
    x, y, z = (float(value) for value in point)
    return (
        matrix[0] * x + matrix[1] * y + matrix[2] * z + matrix[3],
        matrix[4] * x + matrix[5] * y + matrix[6] * z + matrix[7],
        matrix[8] * x + matrix[9] * y + matrix[10] * z + matrix[11],
    )


def _gltf_y_up_to_blender_z_up(
    point: tuple[float, float, float],
) -> tuple[float, float, float]:
    return (point[0], -point[2], point[1])


def _inspect_gltf_document(
    gltf: dict[str, Any], binary: bytes, *, pointer: str, expected_uri: str | None
) -> GlbInspection:
    asset = _mapping(gltf.get("asset"), f"{pointer}/asset")
    if asset.get("version") != "2.0":
        fail(
            "GLB_INVALID",
            "glTF asset version must be 2.0",
            pointer=f"{pointer}/asset/version",
        )
    nodes = [
        _mapping(node, f"{pointer}/nodes/{index}")
        for index, node in enumerate(_list(gltf.get("nodes", []), f"{pointer}/nodes"))
    ]
    if gltf.get("cameras") or any("camera" in node for node in nodes):
        fail("GLB_CAMERA_FORBIDDEN", "Production GLB must not contain cameras")
    if _contains_light_extension(gltf):
        fail("GLB_LIGHT_FORBIDDEN", "Production GLB must not contain punctual lights")

    accessors = _list(gltf.get("accessors"), f"{pointer}/accessors")
    layouts = _validate_binary_layout(
        gltf, binary, pointer=pointer, expected_uri=expected_uri
    )
    meshes = _list(gltf.get("meshes"), f"{pointer}/meshes")
    materials = _list(gltf.get("materials"), f"{pointer}/materials")
    if not meshes or not materials:
        fail("GLB_GEOMETRY_INVALID", "GLB requires meshes and materials")

    triangle_count = 0
    mesh_names: list[str] = []
    mesh_positions: list[tuple[int, ...]] = []
    for mesh_index, raw_mesh in enumerate(meshes):
        mesh_pointer = f"{pointer}/meshes/{mesh_index}"
        mesh = _mapping(raw_mesh, mesh_pointer)
        mesh_names.append(_string(mesh.get("name"), f"{mesh_pointer}/name"))
        primitives = _list(mesh.get("primitives"), f"{mesh_pointer}/primitives")
        if not primitives:
            fail(
                "GLB_GEOMETRY_INVALID",
                "Each mesh requires a primitive",
                pointer=mesh_pointer,
            )
        position_accessors: list[int] = []
        for primitive_index, raw_primitive in enumerate(primitives):
            primitive_pointer = f"{mesh_pointer}/primitives/{primitive_index}"
            primitive = _mapping(raw_primitive, primitive_pointer)
            if primitive.get("mode", 4) != 4:
                fail(
                    "GLB_GEOMETRY_INVALID",
                    "Only triangle primitives are accepted",
                    pointer=f"{primitive_pointer}/mode",
                )
            attributes = _mapping(
                primitive.get("attributes"), f"{primitive_pointer}/attributes"
            )
            position_index = _integer(
                attributes.get("POSITION"), f"{primitive_pointer}/attributes/POSITION"
            )
            if position_index >= len(accessors):
                fail(
                    "GLB_GEOMETRY_INVALID",
                    "POSITION accessor is out of range",
                    pointer=f"{primitive_pointer}/attributes/POSITION",
                )
            position = _mapping(
                accessors[position_index], f"{pointer}/accessors/{position_index}"
            )
            position_layout = layouts[position_index]
            if (
                position_layout.component_type != 5126
                or position_layout.element_type != "VEC3"
                or position_layout.normalized
            ):
                fail(
                    "GLB_GEOMETRY_INVALID",
                    "POSITION must be a non-normalized float VEC3 accessor",
                    pointer=f"{primitive_pointer}/attributes/POSITION",
                )
            if (
                "min" not in position
                or "max" not in position
                or position_layout.absolute_offset % 4
            ):
                fail(
                    "GLB_GEOMETRY_INVALID",
                    "POSITION requires declared bounds and 4-byte alignment",
                    pointer=f"{pointer}/accessors/{position_index}",
                )
            count = position_layout.count
            if count < 3:
                fail(
                    "GLB_GEOMETRY_INVALID",
                    "POSITION requires at least three vertices",
                    pointer=f"{pointer}/accessors/{position_index}/count",
                )
            for semantic, accessor_value in attributes.items():
                attribute_index = _integer(
                    accessor_value, f"{primitive_pointer}/attributes/{semantic}"
                )
                if (
                    attribute_index >= len(layouts)
                    or layouts[attribute_index].count != count
                ):
                    fail(
                        "GLB_GEOMETRY_INVALID",
                        "Vertex attribute accessor is invalid or has a different count",
                        pointer=f"{primitive_pointer}/attributes/{semantic}",
                    )
            position_accessors.append(position_index)
            if "indices" in primitive:
                indices_index = _integer(
                    primitive["indices"], f"{primitive_pointer}/indices"
                )
                if indices_index >= len(accessors):
                    fail(
                        "GLB_GEOMETRY_INVALID",
                        "Index accessor is out of range",
                        pointer=f"{primitive_pointer}/indices",
                    )
                index_layout = layouts[indices_index]
                if (
                    index_layout.element_type != "SCALAR"
                    or index_layout.component_type not in {5121, 5123, 5125}
                    or index_layout.normalized
                ):
                    fail(
                        "GLB_GEOMETRY_INVALID",
                        "Indices must use a non-normalized unsigned SCALAR accessor",
                        pointer=f"{primitive_pointer}/indices",
                    )
                if index_layout.stride != index_layout.element_size:
                    fail(
                        "GLB_GEOMETRY_INVALID",
                        "Index accessors may not use interleaved byteStride",
                        pointer=f"{primitive_pointer}/indices",
                    )
                count = index_layout.count
                for index_value in _iter_accessor_values(index_layout, binary):
                    if int(index_value[0]) >= position_layout.count:
                        fail(
                            "GLB_GEOMETRY_INVALID",
                            "Index payload references a vertex outside POSITION",
                            pointer=f"{primitive_pointer}/indices",
                        )
            if count % 3:
                fail(
                    "GLB_GEOMETRY_INVALID",
                    "Triangle index/vertex count must be divisible by three",
                    pointer=primitive_pointer,
                )
            triangle_count += count // 3
            material_index = _integer(
                primitive.get("material"), f"{primitive_pointer}/material"
            )
            if material_index >= len(materials):
                fail(
                    "GLB_GEOMETRY_INVALID",
                    "Material index is out of range",
                    pointer=f"{primitive_pointer}/material",
                )
        mesh_positions.append(tuple(position_accessors))

    parents: dict[int, int] = {}
    for node_index, node in enumerate(nodes):
        for child_offset, child_value in enumerate(
            _list(node.get("children", []), f"{pointer}/nodes/{node_index}/children")
        ):
            child_index = _integer(
                child_value, f"{pointer}/nodes/{node_index}/children/{child_offset}"
            )
            if child_index >= len(nodes):
                fail(
                    "GLB_SCENE_INVALID",
                    "Node child is out of range",
                    pointer=f"{pointer}/nodes/{node_index}/children/{child_offset}",
                )
            if child_index in parents:
                fail(
                    "GLB_SCENE_INVALID",
                    "A node may not have multiple parents",
                    pointer=f"{pointer}/nodes/{child_index}",
                )
            parents[child_index] = node_index

    scenes = _list(gltf.get("scenes"), f"{pointer}/scenes")
    if not scenes:
        fail(
            "GLB_SCENE_INVALID",
            "GLB requires an active scene",
            pointer=f"{pointer}/scenes",
        )
    scene_index = _integer(gltf.get("scene", 0), f"{pointer}/scene")
    if scene_index >= len(scenes):
        fail(
            "GLB_SCENE_INVALID",
            "Active scene index is out of range",
            pointer=f"{pointer}/scene",
        )
    scene = _mapping(scenes[scene_index], f"{pointer}/scenes/{scene_index}")
    roots = _list(scene.get("nodes"), f"{pointer}/scenes/{scene_index}/nodes")
    if not roots:
        fail(
            "GLB_SCENE_INVALID",
            "Active scene has no root nodes",
            pointer=f"{pointer}/scenes/{scene_index}/nodes",
        )

    minimum = [math.inf, math.inf, math.inf]
    maximum = [-math.inf, -math.inf, -math.inf]
    seen_nodes: set[int] = set()
    seen_meshes: set[int] = set()
    position_cache: dict[int, tuple[tuple[int | float, ...], ...]] = {}

    def visit(
        node_index: int, parent_matrix: tuple[float, ...], ancestors: frozenset[int]
    ) -> None:
        if node_index >= len(nodes):
            fail(
                "GLB_SCENE_INVALID",
                "Scene root node is out of range",
                pointer=f"{pointer}/scenes/{scene_index}/nodes",
            )
        if node_index in ancestors:
            fail(
                "GLB_SCENE_INVALID",
                "Node hierarchy contains a cycle",
                pointer=f"{pointer}/nodes/{node_index}",
            )
        if node_index in seen_nodes:
            fail(
                "GLB_SCENE_INVALID",
                "Active scene reaches the same node more than once",
                pointer=f"{pointer}/nodes/{node_index}",
            )
        seen_nodes.add(node_index)
        node = nodes[node_index]
        world_matrix = _matrix_multiply(
            parent_matrix, _node_local_matrix(node, f"{pointer}/nodes/{node_index}")
        )
        if "mesh" in node:
            mesh_index = _integer(node["mesh"], f"{pointer}/nodes/{node_index}/mesh")
            if mesh_index >= len(meshes):
                fail(
                    "GLB_SCENE_INVALID",
                    "Node mesh is out of range",
                    pointer=f"{pointer}/nodes/{node_index}/mesh",
                )
            seen_meshes.add(mesh_index)
            for position_index in mesh_positions[mesh_index]:
                positions = position_cache.setdefault(
                    position_index,
                    tuple(_iter_accessor_values(layouts[position_index], binary)),
                )
                for position in positions:
                    transformed = _gltf_y_up_to_blender_z_up(
                        _transform_point(world_matrix, position)
                    )
                    for axis, value in enumerate(transformed):
                        minimum[axis] = min(minimum[axis], value)
                        maximum[axis] = max(maximum[axis], value)
        next_ancestors = ancestors | {node_index}
        for child_value in _list(
            node.get("children", []), f"{pointer}/nodes/{node_index}/children"
        ):
            visit(
                _integer(child_value, f"{pointer}/nodes/{node_index}/children"),
                world_matrix,
                next_ancestors,
            )

    for root_offset, root_value in enumerate(roots):
        visit(
            _integer(root_value, f"{pointer}/scenes/{scene_index}/nodes/{root_offset}"),
            _identity_matrix(),
            frozenset(),
        )
    if seen_meshes != set(range(len(meshes))):
        fail(
            "GLB_SCENE_INVALID",
            "Every declared mesh must be reachable from the active scene",
            pointer=f"{pointer}/scenes/{scene_index}",
        )
    if any(not math.isfinite(value) for value in minimum + maximum):
        fail("GLB_GEOMETRY_INVALID", "Active scene has no finite geometry bounds")

    material_names = tuple(
        _string(
            _mapping(item, f"{pointer}/materials/{index}").get("name"),
            f"{pointer}/materials/{index}/name",
        )
        for index, item in enumerate(materials)
    )
    return GlbInspection(
        mesh_count=len(meshes),
        material_count=len(materials),
        triangle_count=triangle_count,
        mesh_names=tuple(mesh_names),
        material_names=material_names,
        bounds_min=tuple(minimum),  # type: ignore[arg-type]
        bounds_max=tuple(maximum),  # type: ignore[arg-type]
    )


def inspect_glb(path: pathlib.Path) -> GlbInspection:
    gltf, binary = _glb_payload(path.read_bytes())
    return _inspect_gltf_document(gltf, binary, pointer="$/glb", expected_uri=None)


def inspect_gltf_json(path: pathlib.Path, expected_bin: pathlib.Path) -> GlbInspection:
    gltf = _mapping(
        strict_json_bytes(path.read_bytes(), code="GLTF_JSON_INVALID"), "$/gltf"
    )
    return _inspect_gltf_document(
        gltf,
        expected_bin.read_bytes(),
        pointer="$/gltf",
        expected_uri=expected_bin.name,
    )


def _validate_named_geometry(value: Any, pointer: str) -> dict[str, Any]:
    geometry = _mapping(value, pointer)
    _validate_bounds(geometry.get("bounds"), f"{pointer}/bounds")
    _integer(geometry.get("mesh_count"), f"{pointer}/mesh_count", minimum=1)
    _integer(geometry.get("material_count"), f"{pointer}/material_count", minimum=1)
    _integer(geometry.get("triangle_count"), f"{pointer}/triangle_count", minimum=1)
    mesh_names = [
        _string(item, f"{pointer}/mesh_names")
        for item in _list(geometry.get("mesh_names"), f"{pointer}/mesh_names")
    ]
    material_names = [
        _string(item, f"{pointer}/material_names")
        for item in _list(geometry.get("material_names"), f"{pointer}/material_names")
    ]
    if len(mesh_names) != len(set(mesh_names)) or len(material_names) != len(
        set(material_names)
    ):
        fail(
            "MANIFEST_GEOMETRY_INVALID",
            "Mesh and material names must be unique",
            pointer=pointer,
        )
    if (
        len(mesh_names) != geometry["mesh_count"]
        or len(material_names) != geometry["material_count"]
    ):
        fail(
            "MANIFEST_GEOMETRY_INVALID",
            "Named geometry counts do not match arrays",
            pointer=pointer,
        )
    return geometry


def validate_manifest_path(
    manifest_path: pathlib.Path, *, selected_glb: pathlib.Path | None = None
) -> dict[str, Any]:
    manifest_path = manifest_path.resolve(strict=True)
    if manifest_path.is_symlink() or not manifest_path.is_file():
        fail(
            "MANIFEST_PATH_INVALID",
            "Manifest must be a regular file",
            pointer=str(manifest_path),
        )
    manifest = _mapping(strict_json_bytes(manifest_path.read_bytes()), "$")
    if manifest.get("schema") != SCHEMA:
        fail("MANIFEST_SCHEMA_INVALID", f"Expected schema {SCHEMA}", pointer="$/schema")
    if manifest.get("asset_id") != EXPECTED_ASSET_ID:
        fail(
            "MANIFEST_ASSET_INVALID",
            f"Expected asset_id {EXPECTED_ASSET_ID}",
            pointer="$/asset_id",
        )

    build = _mapping(manifest.get("build"), "$/build")
    if _integer(build.get("seed"), "$/build/seed") != 4040:
        fail(
            "MANIFEST_BUILD_INVALID",
            "The canonical seed must be 4040",
            pointer="$/build/seed",
        )
    timestamp = _string(build.get("timestamp_utc"), "$/build/timestamp_utc")
    try:
        datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        fail(
            "MANIFEST_BUILD_INVALID",
            "timestamp_utc must be ISO-8601",
            pointer="$/build/timestamp_utc",
        )
    blender = _mapping(build.get("blender"), "$/build/blender")
    if blender.get("version") != EXPECTED_BLENDER_VERSION:
        fail(
            "MANIFEST_BUILD_INVALID",
            f"Expected Blender {EXPECTED_BLENDER_VERSION}",
            pointer="$/build/blender/version",
        )
    _string(blender.get("version_string"), "$/build/blender/version_string")

    units = _mapping(manifest.get("units"), "$/units")
    if units != {
        "system": "METRIC",
        "length": "meter",
        "scale_length": 1.0,
        "up_axis": "Z",
    }:
        fail(
            "MANIFEST_UNITS_INVALID",
            "Asset units must be metric metres with Blender Z-up",
            pointer="$/units",
        )
    source = _mapping(manifest.get("source"), "$/source")
    for key in ("description", "license", "license_url", "generator"):
        _string(source.get(key), f"$/source/{key}")

    repository_root = pathlib.Path(__file__).resolve().parents[2]
    script = _mapping(manifest.get("script"), "$/script")
    if script.get("path") != EXPECTED_SCRIPT:
        fail(
            "SCRIPT_BINDING_INVALID",
            f"Expected script path {EXPECTED_SCRIPT}",
            pointer="$/script/path",
        )
    script_sha = _string(script.get("sha256"), "$/script/sha256")
    if not SHA256_RE.fullmatch(script_sha):
        fail(
            "SCRIPT_BINDING_INVALID",
            "Invalid script SHA-256",
            pointer="$/script/sha256",
        )
    script_path = repository_root / EXPECTED_SCRIPT
    if not script_path.is_file() or sha256_file(script_path) != script_sha:
        fail(
            "SCRIPT_BINDING_INVALID",
            "Manifest is not bound to the current canonical generator",
            pointer="$/script/sha256",
        )

    collections = {
        _string(item, "$/collections")
        for item in _list(manifest.get("collections"), "$/collections")
    }
    if collections != EXPECTED_COLLECTIONS:
        fail(
            "MANIFEST_COLLECTIONS_INVALID",
            "Unexpected export collection set",
            pointer="$/collections",
        )

    geometry = _validate_named_geometry(manifest.get("geometry"), "$/geometry")
    assets = _mapping(manifest.get("assets"), "$/assets")
    if set(assets) != set(EXPECTED_ASSETS):
        fail(
            "MANIFEST_ASSETS_INVALID",
            "Manifest requires chair, cabinet and room-shell assets",
            pointer="$/assets",
        )
    all_asset_meshes: set[str] = set()
    for asset_key, collection in EXPECTED_ASSETS.items():
        asset = _validate_named_geometry(assets[asset_key], f"$/assets/{asset_key}")
        if asset.get("collection") != collection:
            fail(
                "MANIFEST_ASSETS_INVALID",
                "Asset collection binding is invalid",
                pointer=f"$/assets/{asset_key}/collection",
            )
        placement_contract = EXPECTED_ASSET_PLACEMENT[asset_key]
        if asset.get("root_node") != placement_contract["root_node"]:
            fail(
                "MANIFEST_ASSETS_INVALID",
                "Asset root node binding is invalid",
                pointer=f"$/assets/{asset_key}/root_node",
            )
        origin = _finite_vector(asset.get("origin_m"), f"$/assets/{asset_key}/origin_m")
        if origin != placement_contract["origin_m"]:
            fail(
                "MANIFEST_ASSETS_INVALID",
                "Asset origin differs from the canonical room assembly",
                pointer=f"$/assets/{asset_key}/origin_m",
            )
        ue_placement = _mapping(
            asset.get("ue_placement"), f"$/assets/{asset_key}/ue_placement"
        )
        if set(ue_placement) != {"location_cm", "rotation_deg", "scale"}:
            fail(
                "MANIFEST_ASSETS_INVALID",
                "UE placement requires location_cm, rotation_deg and scale",
                pointer=f"$/assets/{asset_key}/ue_placement",
            )
        if (
            _finite_vector(
                ue_placement.get("location_cm"),
                f"$/assets/{asset_key}/ue_placement/location_cm",
            )
            != placement_contract["location_cm"]
        ):
            fail(
                "MANIFEST_ASSETS_INVALID",
                "UE placement location differs from the canonical mmg_040 composition",
                pointer=f"$/assets/{asset_key}/ue_placement/location_cm",
            )
        if _finite_vector(
            ue_placement.get("rotation_deg"),
            f"$/assets/{asset_key}/ue_placement/rotation_deg",
        ) != (0.0, 0.0, 0.0):
            fail(
                "MANIFEST_ASSETS_INVALID",
                "UE placement rotation must be zero after Interchange conversion",
                pointer=f"$/assets/{asset_key}/ue_placement/rotation_deg",
            )
        if _finite_vector(
            ue_placement.get("scale"), f"$/assets/{asset_key}/ue_placement/scale"
        ) != (1.0, 1.0, 1.0):
            fail(
                "MANIFEST_ASSETS_INVALID",
                "UE placement scale must be one",
                pointer=f"$/assets/{asset_key}/ue_placement/scale",
            )
        all_asset_meshes.update(asset["mesh_names"])
    if all_asset_meshes != set(geometry["mesh_names"]):
        fail(
            "MANIFEST_ASSETS_INVALID",
            "Per-asset mesh names must partition top-level geometry",
            pointer="$/assets",
        )

    outputs = _mapping(manifest.get("outputs"), "$/outputs")
    if set(outputs) != set(EXPECTED_OUTPUTS):
        fail("MANIFEST_OUTPUTS_INVALID", "Unexpected output set", pointer="$/outputs")
    resolved: dict[str, pathlib.Path] = {}
    for key, (expected_path, expected_media_type) in EXPECTED_OUTPUTS.items():
        output = _mapping(outputs[key], f"$/outputs/{key}")
        if (
            output.get("path") != expected_path
            or output.get("media_type") != expected_media_type
        ):
            fail(
                "MANIFEST_OUTPUTS_INVALID",
                "Output filename or media type is invalid",
                pointer=f"$/outputs/{key}",
            )
        path = _safe_relative(
            manifest_path.parent, output.get("path"), f"$/outputs/{key}/path"
        )
        if not path.is_file():
            fail(
                "OUTPUT_MISSING",
                "Manifest-bound output does not exist",
                pointer=f"$/outputs/{key}/path",
            )
        expected_bytes = _integer(
            output.get("bytes"), f"$/outputs/{key}/bytes", minimum=1
        )
        expected_sha = _string(output.get("sha256"), f"$/outputs/{key}/sha256")
        if not SHA256_RE.fullmatch(expected_sha):
            fail(
                "OUTPUT_HASH_INVALID",
                "Invalid output SHA-256",
                pointer=f"$/outputs/{key}/sha256",
            )
        if path.stat().st_size != expected_bytes:
            fail(
                "OUTPUT_SIZE_MISMATCH",
                "Output byte count differs from manifest",
                pointer=f"$/outputs/{key}/bytes",
            )
        if sha256_file(path) != expected_sha:
            fail(
                "OUTPUT_HASH_MISMATCH",
                "Output SHA-256 differs from manifest",
                pointer=f"$/outputs/{key}/sha256",
            )
        resolved[key] = path

    if selected_glb is not None and selected_glb.resolve(strict=True) != resolved[
        "glb"
    ].resolve(strict=True):
        fail("GLB_BINDING_INVALID", "Selected GLB differs from the manifest-bound GLB")

    previews: dict[str, dict[str, Any]] = {}
    for key in ("preview_overview", "preview_detail"):
        inspection = inspect_png(resolved[key])
        output = outputs[key]
        if (
            output.get("width") != inspection.width
            or output.get("height") != inspection.height
        ):
            fail(
                "PREVIEW_DIMENSION_MISMATCH",
                "Preview dimensions differ from manifest",
                pointer=f"$/outputs/{key}",
            )
        previews[key] = {
            "width": inspection.width,
            "height": inspection.height,
            "channel_range": inspection.channel_range,
            "luminance_range": inspection.luminance_range,
            "unique_sampled_colors": inspection.unique_sampled_colors,
            "opaque_fraction": round(inspection.opaque_fraction, 6),
        }

    glb = inspect_glb(resolved["glb"])
    if (
        glb.mesh_count != geometry["mesh_count"]
        or glb.material_count != geometry["material_count"]
        or glb.triangle_count != geometry["triangle_count"]
    ):
        fail(
            "GLB_MANIFEST_MISMATCH",
            "GLB geometry counts differ from manifest",
            pointer="$/geometry",
        )
    if set(glb.mesh_names) != set(geometry["mesh_names"]) or set(
        glb.material_names
    ) != set(geometry["material_names"]):
        fail(
            "GLB_MANIFEST_MISMATCH",
            "GLB mesh/material names differ from manifest",
            pointer="$/geometry",
        )
    if any(size <= 0.0 or not math.isfinite(size) for size in glb.dimensions):
        fail("GLB_GEOMETRY_INVALID", "GLB aggregate bounds must be finite and non-zero")
    manifest_minimum, manifest_maximum = _validate_bounds(
        geometry["bounds"], "$/geometry/bounds"
    )
    for axis, (
        computed_minimum,
        computed_maximum,
        expected_minimum,
        expected_maximum,
    ) in enumerate(
        zip(glb.bounds_min, glb.bounds_max, manifest_minimum, manifest_maximum)
    ):
        if not math.isclose(
            computed_minimum,
            expected_minimum,
            rel_tol=BOUNDS_REL_TOLERANCE,
            abs_tol=BOUNDS_ABS_TOLERANCE_M,
        ) or not math.isclose(
            computed_maximum,
            expected_maximum,
            rel_tol=BOUNDS_REL_TOLERANCE,
            abs_tol=BOUNDS_ABS_TOLERANCE_M,
        ):
            fail(
                "GLB_MANIFEST_BOUNDS_MISMATCH",
                "Decoded and node-transformed GLB geometry bounds differ from the manifest metric bounds",
                pointer=f"$/geometry/bounds/{axis}",
            )

    gltf = inspect_gltf_json(resolved["gltf"], resolved["gltf_bin"])
    if (
        gltf.mesh_count != glb.mesh_count
        or gltf.material_count != glb.material_count
        or gltf.triangle_count != glb.triangle_count
        or set(gltf.mesh_names) != set(glb.mesh_names)
        or set(gltf.material_names) != set(glb.material_names)
        or any(
            not math.isclose(
                gltf.bounds_min[axis],
                glb.bounds_min[axis],
                rel_tol=BOUNDS_REL_TOLERANCE,
                abs_tol=BOUNDS_ABS_TOLERANCE_M,
            )
            or not math.isclose(
                gltf.bounds_max[axis],
                glb.bounds_max[axis],
                rel_tol=BOUNDS_REL_TOLERANCE,
                abs_tol=BOUNDS_ABS_TOLERANCE_M,
            )
            for axis in range(3)
        )
    ):
        fail(
            "GLTF_GLB_MISMATCH",
            "Separate glTF geometry differs from the canonical GLB",
            pointer="$/outputs/gltf",
        )

    return {
        "schema": "simworld.vista.blender-asset-validation/v1",
        "status": "passed",
        "asset_id": EXPECTED_ASSET_ID,
        "manifest": str(manifest_path),
        "manifest_sha256": sha256_file(manifest_path),
        "glb": {
            "path": str(resolved["glb"]),
            "sha256": outputs["glb"]["sha256"],
            "mesh_count": glb.mesh_count,
            "material_count": glb.material_count,
            "triangle_count": glb.triangle_count,
            "bounds_min": list(glb.bounds_min),
            "bounds_max": list(glb.bounds_max),
            "dimensions": list(glb.dimensions),
            "contains_cameras": False,
            "contains_lights": False,
        },
        "gltf": {
            "path": str(resolved["gltf"]),
            "sha256": outputs["gltf"]["sha256"],
            "bin_path": str(resolved["gltf_bin"]),
            "bin_sha256": outputs["gltf_bin"]["sha256"],
            "equivalent_geometry": True,
        },
        "previews": previews,
    }


def validate_path(path: pathlib.Path) -> dict[str, Any]:
    path = path.expanduser().resolve(strict=True)
    if path.suffix.lower() == ".glb":
        manifest = path.parent / "manifest.json"
        if not manifest.is_file():
            fail(
                "MANIFEST_MISSING",
                "A GLB must have a sibling manifest.json",
                pointer=str(path),
            )
        return validate_manifest_path(manifest, selected_glb=path)
    return validate_manifest_path(path)


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "asset", type=pathlib.Path, help="manifest.json or its bound .glb"
    )
    parser.add_argument(
        "--json", action="store_true", help="emit compact machine-readable JSON"
    )
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = validate_path(args.asset)
    except (AssetValidationError, FileNotFoundError, OSError) as error:
        if isinstance(error, AssetValidationError):
            payload = {
                "schema": "simworld.vista.blender-asset-validation/v1",
                "status": "failed",
                "error": error.as_dict(),
            }
        else:
            payload = {
                "schema": "simworld.vista.blender-asset-validation/v1",
                "status": "failed",
                "error": {
                    "code": "IO_ERROR",
                    "message": str(error),
                    "pointer": str(args.asset),
                },
            }
        print(
            json.dumps(payload, sort_keys=True, separators=(",", ":"))
            if args.json
            else json.dumps(payload, indent=2, sort_keys=True),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(result, sort_keys=True, separators=(",", ":"))
        if args.json
        else json.dumps(result, indent=2, sort_keys=True)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
