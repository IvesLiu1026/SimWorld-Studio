#!/usr/bin/env python3
"""Prepare a pinned three-record VISTA ``mmg_040`` semantic smoke catalog.

This tool is deliberately NON-PRODUCTION.  It validates four independently
pinned, local evidence files and emits three operator-curated catalog records.
It never opens a network connection, starts Unreal, calls a model, or touches a
database.  The default mode writes nothing.  ``--apply`` can publish one new,
private, sealed nonproduction bundle for an isolated operator smoke only.
"""

from __future__ import annotations

import argparse
import copy
import ctypes
import errno
import hashlib
import json
import math
import os
import re
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


BUNDLE_SCHEMA = "simworld-vista-mmg040-semantic-smoke-catalog/v1"
RECEIPT_SCHEMA = "simworld-vista-mmg040-semantic-smoke-catalog-receipt/v1"
SOURCE_SCHEMA = "simworld-external-asset-source/v1"
IMPORT_JOB_SCHEMA = "simworld-ue-interchange-import-job/v1"
INTERCHANGE_SCHEMA = "vista-ue-interchange-commandlet-observation/v1"
SCENE_BUILD_SCHEMA = "vista-mmg040-commandlet-scene-build-observation/v1"
SOURCE_REVISION = "vista-mmg-040-polyhaven-2026-07-22-v2"
SOURCE_MANIFEST_SHA256 = "f887de3303fc9ad513fb0c75172e8332d75f8e0dc29e2af9fd542972160ab5f2"
IMPORT_JOB_SHA256 = "d1e929b7466384b5661e999653889f3fd68fe8d06fc80887204cedcc867999b9"
INTERCHANGE_OBSERVATION_SHA256 = "ec473e1be15613bf857702b829c39edad63b3292c5b69db6699a3959d51bf0d3"
SCENE_BUILD_OBSERVATION_SHA256 = "1689f72e1f88205edb17d8056ddec7cb27d135f62542619a232435cf2f90025b"
SOURCE_TREE_SHA256 = "0d3858dc07a1cf77d9dd592a6eb897865f2fb7c3e4796a4f86d215ac4969ac36"
SCENE_SOURCE_REVISION = "ec5ed8dd4beb-mmg040-live-r1"
SCENE_ASSET_PATH = "/Game/VISTA/Scenes/MMG040_Office_CommandletR3"
SCENE_MAP_SHA256 = "afa9ecddf4133a443080827922686b44b4f61bd28418d087da378d429d7bfd14"
NONPRODUCTION_ACK = "NON_PRODUCTION_ISOLATED_SMOKE_ONLY"
SCOPE = "operator_curated_isolated_nonproduction_smoke_only"
SMOKE_NAMESPACE = "vista_mmg040_nonproduction_smoke"
SMOKE_SNAPSHOT_ID = "vista-mmg040-nonproduction-smoke-20260722-f887de33-ec473e1b-1689f72e"
CATALOG_DIGEST_DOMAIN = b"simworld-catalog-canonical-json/v1\0"
PREPARED_SEAL_DOMAIN = b"simworld-vista-mmg040-smoke-prepared-catalog/v1\0"
MAX_INPUT_BYTES = 1024 * 1024
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
MD5_RE = re.compile(r"^[a-f0-9]{32}$")
OUTPUT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


SOURCE_FILES: dict[str, tuple[int, str]] = {
    "Shelf_01/Shelf_01.bin": (12676, "21ee76dda6320ba423e76d192cfd38885e334ae394c03978d092f96b508f175b"),
    "Shelf_01/Shelf_01_1k.gltf": (2617, "715bce6ff1886eccdd43d558f5cf9242a24396e0eb9e8ae391905186bb339e52"),
    "Shelf_01/textures/Shelf_01_arm_1k.jpg": (205542, "d5d180b15c48ed6ab7b27a663de8f399f813ed0c32ff2706520d677a1e387cdc"),
    "Shelf_01/textures/Shelf_01_diff_1k.jpg": (153220, "875bb8110dec4f409d9127cba95f7c0ead02bdfeef6dd47232df939e4de35a5c"),
    "Shelf_01/textures/Shelf_01_nor_gl_1k.jpg": (231393, "c817d7966974be2da13e4b0a57918e8e142edc50eb41d438229c3bad70df0f91"),
    "cardboard_box_01/cardboard_box_01.bin": (381936, "7212d43b4cb56459e46d31ab34dc7c310fcfbe170f4a588416dea07ae5628a4b"),
    "cardboard_box_01/cardboard_box_01_1k.gltf": (2903, "6038bcc5bba5a08a5f6643a265977e37e3d14553e62981e144c33a89f10b78e1"),
    "cardboard_box_01/textures/cardboard_box_01_arm_1k.jpg": (398433, "5db63828d0f904c6a8d9c83afc912260b2a27261c15a28d06360519384574d2b"),
    "cardboard_box_01/textures/cardboard_box_01_diff_1k.jpg": (567741, "122c7ba8c5935e289804a730b480677a4bffdf74a45c91970cba682df92e171c"),
    "cardboard_box_01/textures/cardboard_box_01_nor_gl_1k.jpg": (817424, "d2d1f529a851966b65411b5094510dd9eba03cd5c0a89921f79325c377a8e8eb"),
    "painted_wooden_stool/painted_wooden_stool.bin": (23384, "d336c1eb8832e4441f69bb5618e2289703a7ca71d3884a9087d658051eee0b66"),
    "painted_wooden_stool/painted_wooden_stool_1k.gltf": (2848, "f9a9a4a7c38b3421c30dac35d91c41c8864452d6dbb398911494b8324d312ef9"),
    "painted_wooden_stool/textures/painted_wooden_stool_arm_1k.jpg": (751171, "2088f99a25050d6666348388d279b65993fdd50ad8c14d3419a64f1caea6472d"),
    "painted_wooden_stool/textures/painted_wooden_stool_diff_1k.jpg": (607519, "db0322de5e3c588465d0894515a7c1bdacde9e2a469a9ac154206bfe96746d3f"),
    "painted_wooden_stool/textures/painted_wooden_stool_nor_gl_1k.jpg": (489911, "39fed6e0a14b464bf5b9555d4ac02df0f85b0bfc89d383e4b8e00c343598384e"),
}


ASSETS: dict[str, dict[str, Any]] = {
    "polyhaven_cardboard_box_01": {
        "source_asset_id": "cardboard_box_01",
        "semantic_roles": ["cardboard_box", "retrievable_storage_box"],
        "source_gltf": "cardboard_box_01/cardboard_box_01_1k.gltf",
        "destination": "/Game/VISTA/External/PolyHaven/cardboard_box_01",
        "ue_path": "/Game/VISTA/External/PolyHaven/cardboard_box_01/cardboard_box_01_1k.cardboard_box_01_1k",
        "material_path": "/Game/VISTA/External/PolyHaven/cardboard_box_01/cardboard_box_01.cardboard_box_01",
        "dimensions_cm": [38.7024, 51.587, 34.18],
        "source_dimensions_m": [0.38702359795570374, 0.3417995572090149, 0.5158704221248627],
        "category": "indoor_clutter",
        "subcategory": "storage_box",
        "name": "Cardboard Box 01",
        "short_description": "Small cardboard storage box for indoor handling and placement tasks.",
        "description": "A compact cardboard box curated for isolated VISTA mmg_040 retrieval smoke tests; rendered appearance remains unreviewed.",
        "tags": ["box", "cardboard", "container", "indoor", "packaging", "storage"],
        "materials": ["cardboard"],
        "colors": ["brown", "kraft"],
        "placements": ["floor", "shelf", "table"],
        "function": "Stores or carries small items.",
        "affordances": ["carry", "pick_up", "place_on_surface", "stack"],
    },
    "polyhaven_painted_wooden_stool": {
        "source_asset_id": "painted_wooden_stool",
        "semantic_roles": ["indoor_seating", "stable_step_stool"],
        "source_gltf": "painted_wooden_stool/painted_wooden_stool_1k.gltf",
        "destination": "/Game/VISTA/External/PolyHaven/painted_wooden_stool",
        "ue_path": "/Game/VISTA/External/PolyHaven/painted_wooden_stool/painted_wooden_stool_1k.painted_wooden_stool_1k",
        "material_path": "/Game/VISTA/External/PolyHaven/painted_wooden_stool/painted_wooden_stool.painted_wooden_stool",
        "dimensions_cm": [38.4837, 40.6074, 57.8915],
        "source_dimensions_m": [0.38483743369579315, 0.5789150850614533, 0.40607430040836334],
        "category": "seating",
        "subcategory": "stool",
        "name": "Painted Wooden Stool",
        "short_description": "Compact painted wooden stool for indoor seating or step-adjacent scenes.",
        "description": "A small wooden stool curated for isolated VISTA mmg_040 retrieval smoke tests; rendered appearance and load-bearing use remain unreviewed.",
        "tags": ["furniture", "indoor", "painted", "seating", "stool", "wooden"],
        "materials": ["paint", "wood"],
        "colors": ["painted wood"],
        "placements": ["floor", "near desk", "near shelf"],
        "function": "Provides compact seating or a small raised surface.",
        "affordances": ["place_on_floor", "sit", "stand_near"],
    },
    "polyhaven_shelf_01": {
        "source_asset_id": "Shelf_01",
        "semantic_roles": ["high_storage_shelf", "indoor_storage"],
        "source_gltf": "Shelf_01/Shelf_01_1k.gltf",
        "destination": "/Game/VISTA/External/PolyHaven/shelf_01",
        "ue_path": "/Game/VISTA/External/PolyHaven/Shelf_01/Shelf_01_1k.Shelf_01_1k",
        "material_path": "/Game/VISTA/External/PolyHaven/Shelf_01/Shelf_01.Shelf_01",
        "dimensions_cm": [100.3444, 25.6979, 208.031],
        "source_dimensions_m": [1.003443717956543, 2.080310344696045, 0.2569795063869833],
        "category": "furniture_indoor",
        "subcategory": "shelving",
        "name": "Shelf 01",
        "short_description": "Tall narrow indoor shelf for storage and object-placement scenes.",
        "description": "A tall shelving unit curated for isolated VISTA mmg_040 retrieval smoke tests; rendered appearance and wall contact remain unreviewed.",
        "tags": ["furniture", "indoor", "shelf", "shelving", "storage", "tall"],
        "materials": ["painted surface"],
        "colors": ["neutral"],
        "placements": ["against wall", "office", "storage area"],
        "function": "Organizes and supports stored objects.",
        "affordances": ["place_items", "retrieve_items", "stand_near"],
    },
}


CATEGORY_DESCRIPTIONS = {
    "furniture_indoor": "Indoor furniture: sofas, cabinets, shelves, desks, beds (off-theme for outdoor scenes).",
    "indoor_clutter": "Off-theme indoor/random clutter: appliances, medical/lab, school/office, bathroom/sports, misc props.",
    "seating": "Seating and small tables: benches, chairs, stools, cafe/park tables.",
}


REVIEW_GATE_NAMES = (
    "interchange_pipeline_fingerprint",
    "material_texture_dependency",
    "rendered_collision",
    "rendered_contact",
    "rendered_pbr",
    "rendered_scale",
    "screenshot",
)


def review_gates() -> dict[str, str]:
    """Return a fresh gate mapping; prepared surfaces never share mutable gates."""
    return {name: "review_pending" for name in REVIEW_GATE_NAMES}


class SmokeCatalogError(RuntimeError):
    def __init__(self, code: str, message: str, *, pointer: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.pointer = pointer

    def public_dict(self) -> dict[str, str]:
        value = {"code": self.code, "message": self.message}
        if self.pointer:
            value["pointer"] = self.pointer
        return value


def fail(code: str, message: str, *, pointer: str | None = None) -> None:
    raise SmokeCatalogError(code, message, pointer=pointer)


def canonical_json(value: Any, *, pretty: bool = True) -> bytes:
    options: dict[str, Any] = {"ensure_ascii": False, "sort_keys": True}
    if pretty:
        options["indent"] = 2
    else:
        options["separators"] = (",", ":")
    return (json.dumps(value, **options) + ("\n" if pretty else "")).encode("utf-8")


def _strict_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            fail("SMOKE_INPUT_DUPLICATE_KEY", "JSON contains a duplicate object key", pointer=key)
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    fail("SMOKE_INPUT_JSON_INVALID", "JSON contains a non-finite number", pointer=value)


def strict_json(raw: bytes, pointer: str) -> Any:
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_pairs, parse_constant=_reject_constant)
    except SmokeCatalogError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        fail("SMOKE_INPUT_JSON_INVALID", "Input must be bounded UTF-8 JSON", pointer=pointer)


def exact_object(value: Any, keys: Iterable[str], pointer: str) -> dict[str, Any]:
    expected = set(keys)
    if not isinstance(value, dict) or set(value) != expected:
        fail("SMOKE_INPUT_SCHEMA_INVALID", "Object fields do not match the pinned schema", pointer=pointer)
    return value


def _safe_sha(value: Any, pointer: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        fail("SMOKE_INPUT_SCHEMA_INVALID", "Expected a lowercase SHA-256 digest", pointer=pointer)
    return value


def _safe_string(value: Any, pointer: str, *, maximum: int = 2048) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        fail("SMOKE_INPUT_SCHEMA_INVALID", "Expected a bounded non-empty string", pointer=pointer)
    return value


def _finite_vector(value: Any, expected: Sequence[float], pointer: str, *, tolerance: float = 1e-6) -> list[float]:
    if not isinstance(value, list) or len(value) != len(expected):
        fail("SMOKE_INPUT_SCHEMA_INVALID", "Expected a fixed-length numeric vector", pointer=pointer)
    result: list[float] = []
    for observed, pinned in zip(value, expected):
        if isinstance(observed, bool) or not isinstance(observed, (int, float)) or not math.isfinite(observed):
            fail("SMOKE_INPUT_SCHEMA_INVALID", "Vector values must be finite", pointer=pointer)
        if not math.isclose(float(observed), float(pinned), rel_tol=tolerance, abs_tol=tolerance):
            fail("SMOKE_EVIDENCE_MISMATCH", "Observed dimensions differ from the pinned asset", pointer=pointer)
        result.append(float(observed))
    return result


def _reject_symlink_components(path: Path, pointer: str, *, missing_ok: bool = False) -> None:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            if missing_ok:
                return
            fail("SMOKE_INPUT_UNAVAILABLE", "Pinned input path does not exist", pointer=pointer)
        except OSError:
            fail("SMOKE_INPUT_UNAVAILABLE", "Pinned input path cannot be inspected", pointer=pointer)
        if stat.S_ISLNK(metadata.st_mode):
            fail("SMOKE_INPUT_SYMLINK_REJECTED", "Pinned input path must not traverse symlinks", pointer=pointer)


@dataclass(frozen=True)
class PinnedJson:
    label: str
    sha256: str
    value: dict[str, Any]


def read_pinned_json(path: Path, expected_sha256: str, label: str) -> PinnedJson:
    expected_sha256 = _safe_sha(expected_sha256, f"--{label}-sha256")
    if not path.is_absolute() or path != Path(os.path.abspath(path)):
        fail("SMOKE_INPUT_PATH_INVALID", "Pinned input path must be absolute and normalized", pointer=f"--{label}")
    _reject_symlink_components(path, f"--{label}")
    try:
        metadata = os.lstat(path)
    except OSError:
        fail("SMOKE_INPUT_UNAVAILABLE", "Pinned input cannot be inspected", pointer=f"--{label}")
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_size < 2
        or metadata.st_size > MAX_INPUT_BYTES
    ):
        fail("SMOKE_INPUT_METADATA_UNSAFE", "Pinned input must be a private, owned, single-link regular file", pointer=f"--{label}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        fail("SMOKE_INPUT_UNAVAILABLE", "Pinned input cannot be opened safely", pointer=f"--{label}")
    try:
        before = os.fstat(descriptor)
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    raw = b"".join(chunks)
    stable_fields = ("st_dev", "st_ino", "st_uid", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")
    if remaining or any(getattr(before, field) != getattr(after, field) for field in stable_fields):
        fail("SMOKE_INPUT_CHANGED", "Pinned input changed while being read", pointer=f"--{label}")
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256:
        fail("SMOKE_INPUT_DIGEST_MISMATCH", "Pinned input SHA-256 does not match", pointer=f"--{label}-sha256")
    parsed = strict_json(raw, f"--{label}")
    if not isinstance(parsed, dict):
        fail("SMOKE_INPUT_SCHEMA_INVALID", "Pinned input must contain a JSON object", pointer=f"--{label}")
    return PinnedJson(label=label, sha256=digest, value=parsed)


def _asset_file_paths(source_asset_id: str) -> list[str]:
    return sorted(path for path in SOURCE_FILES if path.split("/", 1)[0] == source_asset_id)


def validate_source_manifest(document: PinnedJson) -> dict[str, dict[str, Any]]:
    if document.sha256 != SOURCE_MANIFEST_SHA256:
        fail("SMOKE_SOURCE_MANIFEST_NOT_PINNED", "Source manifest is not the acquired canonical v2 manifest", pointer="--source-manifest-sha256")
    manifest = exact_object(
        document.value,
        {"schema", "revision", "provider", "provider_url", "license", "license_url", "attribution", "assets"},
        "source_manifest",
    )
    fixed = {
        "schema": SOURCE_SCHEMA,
        "revision": SOURCE_REVISION,
        "provider": "Poly Haven",
        "provider_url": "https://polyhaven.com/",
        "license": "CC0-1.0",
        "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
        "attribution": "3D assets sourced from Poly Haven",
    }
    if any(manifest[key] != value for key, value in fixed.items()):
        fail("SMOKE_SOURCE_PROVENANCE_INVALID", "Source provenance is not the pinned external CC0 lineage", pointer="source_manifest")
    raw_assets = manifest["assets"]
    if not isinstance(raw_assets, list) or len(raw_assets) != 3:
        fail("SMOKE_ASSET_SET_INVALID", "Source manifest must contain exactly three assets", pointer="source_manifest/assets")
    assets: dict[str, dict[str, Any]] = {}
    observed_files: set[str] = set()
    for index, raw in enumerate(raw_assets):
        pointer = f"source_manifest/assets/{index}"
        asset = exact_object(raw, {"asset_id", "source_asset_id", "source_page", "semantic_roles", "format", "files"}, pointer)
        asset_id = asset.get("asset_id")
        definition = ASSETS.get(asset_id) if isinstance(asset_id, str) else None
        if definition is None or asset_id in assets:
            fail("SMOKE_ASSET_SET_INVALID", "Source asset IDs must match the fixed smoke set", pointer=pointer)
        source_id = definition["source_asset_id"]
        if (
            asset["source_asset_id"] != source_id
            or asset["source_page"] != f"https://polyhaven.com/a/{source_id}"
            or asset["format"] != "gltf-2.0"
            or sorted(asset["semantic_roles"]) != sorted(definition["semantic_roles"])
        ):
            fail("SMOKE_SOURCE_PROVENANCE_INVALID", "Source asset provenance does not match the fixed smoke set", pointer=pointer)
        files = asset["files"]
        expected_paths = _asset_file_paths(source_id)
        if not isinstance(files, list) or len(files) != len(expected_paths):
            fail("SMOKE_SOURCE_FILE_SET_INVALID", "Source asset file closure is incomplete", pointer=f"{pointer}/files")
        for file_index, raw_file in enumerate(files):
            file_pointer = f"{pointer}/files/{file_index}"
            entry = exact_object(raw_file, {"path", "url", "bytes", "md5", "sha256"}, file_pointer)
            relative = entry.get("path")
            if not isinstance(relative, str) or relative not in SOURCE_FILES or relative in observed_files:
                fail("SMOKE_SOURCE_FILE_SET_INVALID", "Source file closure contains an unknown or duplicate path", pointer=file_pointer)
            size, digest = SOURCE_FILES[relative]
            if entry["bytes"] != size or entry["sha256"] != digest or not isinstance(entry["md5"], str) or not MD5_RE.fullmatch(entry["md5"]):
                fail("SMOKE_SOURCE_FILE_DIGEST_INVALID", "Source file metadata differs from the fixed SHA-256 closure", pointer=file_pointer)
            url = entry["url"]
            if not isinstance(url, str) or not url.startswith("https://dl.polyhaven.org/file/ph-assets/Models/") or "?" in url or "#" in url:
                fail("SMOKE_SOURCE_PROVENANCE_INVALID", "Source file URL is outside the pinned Poly Haven origin", pointer=file_pointer)
            observed_files.add(relative)
        if sorted(entry["path"] for entry in files) != expected_paths:
            fail("SMOKE_SOURCE_FILE_SET_INVALID", "Source files do not match their asset directory", pointer=f"{pointer}/files")
        assets[asset_id] = asset
    if set(assets) != set(ASSETS) or observed_files != set(SOURCE_FILES):
        fail("SMOKE_ASSET_SET_INVALID", "Source manifest differs from the exact three-asset closure", pointer="source_manifest/assets")
    return assets


def validate_independent_evidence_pin(document: PinnedJson, expected_sha256: str) -> None:
    if document.sha256 != expected_sha256:
        fail(
            "SMOKE_EVIDENCE_NOT_PINNED",
            "Input is not the independently pinned real evidence file",
            pointer=f"--{document.label}-sha256",
        )


def validate_import_job(document: PinnedJson, source_manifest_sha256: str) -> dict[str, dict[str, Any]]:
    job = exact_object(document.value, {"schema", "source_binding", "engine_contract", "assets", "execution_gate"}, "import_job")
    if job["schema"] != IMPORT_JOB_SCHEMA:
        fail("SMOKE_IMPORT_JOB_INVALID", "Import job schema is unsupported", pointer="import_job/schema")
    binding = exact_object(job["source_binding"], {"source_schema", "revision", "provider", "license", "manifest_sha256", "tree_sha256", "file_count", "total_bytes"}, "import_job/source_binding")
    expected_binding = {
        "source_schema": SOURCE_SCHEMA,
        "revision": SOURCE_REVISION,
        "provider": "Poly Haven",
        "license": "CC0-1.0",
        "manifest_sha256": source_manifest_sha256,
        "tree_sha256": SOURCE_TREE_SHA256,
        "file_count": 15,
        "total_bytes": 4_648_718,
    }
    if binding != expected_binding:
        fail("SMOKE_EVIDENCE_MISMATCH", "Import job does not bind the exact source manifest and tree", pointer="import_job/source_binding")
    engine = exact_object(job["engine_contract"], {"engine_version", "python_api_status", "import_route", "official_api_references", "asset_import_task", "destination_root", "follow_redirectors"}, "import_job/engine_contract")
    task = exact_object(engine["asset_import_task"], {"automated", "async", "replace_existing", "replace_existing_settings", "save"}, "import_job/engine_contract/asset_import_task")
    if (
        engine["engine_version"] != "5.3.2"
        or engine["python_api_status"] != "experimental"
        or engine["import_route"] != "Interchange"
        or engine["destination_root"] != "/Game/VISTA/External/PolyHaven"
        or engine["follow_redirectors"] is not False
        or task != {"automated": True, "async": False, "replace_existing": False, "replace_existing_settings": False, "save": True}
        or not isinstance(engine["official_api_references"], list)
        or len(engine["official_api_references"]) != 2
    ):
        fail("SMOKE_IMPORT_JOB_INVALID", "Import engine contract differs from the fixed UE 5.3 Interchange plan", pointer="import_job/engine_contract")
    gate = exact_object(job["execution_gate"], {"prepared_only", "unreal_started", "content_imported", "requires_operator_approval", "requires_disposable_project_copy", "requires_ue_5_3_2_api_probe", "do_not_add_to_semantic_index_before_post_import_verification"}, "import_job/execution_gate")
    expected_gate = {
        "prepared_only": True,
        "unreal_started": False,
        "content_imported": False,
        "requires_operator_approval": True,
        "requires_disposable_project_copy": True,
        "requires_ue_5_3_2_api_probe": True,
        "do_not_add_to_semantic_index_before_post_import_verification": True,
    }
    if gate != expected_gate:
        fail("SMOKE_IMPORT_JOB_INVALID", "Import job preparation gate is not fail-closed", pointer="import_job/execution_gate")
    raw_assets = job["assets"]
    if not isinstance(raw_assets, list) or len(raw_assets) != 3:
        fail("SMOKE_ASSET_SET_INVALID", "Import job must contain exactly three assets", pointer="import_job/assets")
    assets: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(raw_assets):
        pointer = f"import_job/assets/{index}"
        asset = exact_object(raw, {"asset_id", "source_asset_id", "semantic_roles", "source_gltf", "external_dependencies", "source_bounds", "destination_content_path", "expected_object_paths", "post_import_verification"}, pointer)
        asset_id = asset.get("asset_id")
        definition = ASSETS.get(asset_id) if isinstance(asset_id, str) else None
        if definition is None or asset_id in assets:
            fail("SMOKE_ASSET_SET_INVALID", "Import asset IDs differ from the fixed smoke set", pointer=pointer)
        if (
            asset["source_asset_id"] != definition["source_asset_id"]
            or sorted(asset["semantic_roles"]) != sorted(definition["semantic_roles"])
            or asset["destination_content_path"] != definition["destination"]
            or asset["expected_object_paths"] != []
        ):
            fail("SMOKE_EVIDENCE_MISMATCH", "Import asset identity or destination differs", pointer=pointer)
        gltf = exact_object(asset["source_gltf"], {"path", "bytes", "sha256"}, f"{pointer}/source_gltf")
        gltf_path = definition["source_gltf"]
        gltf_size, gltf_sha = SOURCE_FILES[gltf_path]
        if gltf != {"path": gltf_path, "bytes": gltf_size, "sha256": gltf_sha}:
            fail("SMOKE_SOURCE_FILE_DIGEST_INVALID", "Import job glTF binding differs", pointer=f"{pointer}/source_gltf")
        dependencies = asset["external_dependencies"]
        expected_dependencies = set(_asset_file_paths(definition["source_asset_id"])) - {gltf_path}
        if not isinstance(dependencies, list) or len(dependencies) != len(expected_dependencies):
            fail("SMOKE_SOURCE_FILE_SET_INVALID", "Import dependency closure differs", pointer=f"{pointer}/external_dependencies")
        observed_dependencies: set[str] = set()
        for dep_index, raw_dependency in enumerate(dependencies):
            dep_pointer = f"{pointer}/external_dependencies/{dep_index}"
            dependency = exact_object(raw_dependency, {"path", "bytes", "sha256", "usages"}, dep_pointer)
            dep_path = dependency.get("path")
            if not isinstance(dep_path, str) or dep_path not in expected_dependencies or dep_path in observed_dependencies:
                fail("SMOKE_SOURCE_FILE_SET_INVALID", "Import dependency is unknown or duplicated", pointer=dep_pointer)
            size, digest = SOURCE_FILES[dep_path]
            expected_usage = ["buffer"] if dep_path.endswith(".bin") else ["image"]
            if dependency != {"path": dep_path, "bytes": size, "sha256": digest, "usages": expected_usage}:
                fail("SMOKE_SOURCE_FILE_DIGEST_INVALID", "Import dependency metadata differs", pointer=dep_pointer)
            observed_dependencies.add(dep_path)
        if observed_dependencies != expected_dependencies:
            fail("SMOKE_SOURCE_FILE_SET_INVALID", "Import dependency closure is incomplete", pointer=f"{pointer}/external_dependencies")
        bounds = exact_object(asset["source_bounds"], {"basis", "coordinate_unit", "includes_node_transforms", "position_accessors", "aggregate_min", "aggregate_max", "dimensions"}, f"{pointer}/source_bounds")
        if bounds["basis"] != "gltf_POSITION_accessor_local_extrema_aggregate" or bounds["coordinate_unit"] != "meter" or bounds["includes_node_transforms"] is not False:
            fail("SMOKE_IMPORT_JOB_INVALID", "Source bounds basis differs from the fixed job", pointer=f"{pointer}/source_bounds")
        _finite_vector(bounds["dimensions"], definition["source_dimensions_m"], f"{pointer}/source_bounds/dimensions")
        if not isinstance(bounds["position_accessors"], list) or not bounds["position_accessors"]:
            fail("SMOKE_IMPORT_JOB_INVALID", "Source bounds lack accessor evidence", pointer=f"{pointer}/source_bounds/position_accessors")
        verification = exact_object(asset["post_import_verification"], {"returned_objects", "materials_and_textures", "bounds", "collision", "pbr_channel_review"}, f"{pointer}/post_import_verification")
        returned = exact_object(verification["returned_objects"], {"require_at_least_one_class", "record_exact_object_paths_after_import", "reject_object_redirectors"}, f"{pointer}/post_import_verification/returned_objects")
        materials = exact_object(verification["materials_and_textures"], {"record_material_slots", "account_for_every_source_image", "reject_missing_or_default_only_materials"}, f"{pointer}/post_import_verification/materials_and_textures")
        if returned != {"require_at_least_one_class": "StaticMesh", "record_exact_object_paths_after_import": True, "reject_object_redirectors": True} or not all(materials.values()):
            fail("SMOKE_IMPORT_JOB_INVALID", "Post-import material/object verification is not pinned", pointer=f"{pointer}/post_import_verification")
        assets[asset_id] = asset
    if set(assets) != set(ASSETS):
        fail("SMOKE_ASSET_SET_INVALID", "Import job differs from the exact three-asset closure", pointer="import_job/assets")
    return assets


def validate_interchange_observation(
    document: PinnedJson,
    *,
    source_manifest_sha256: str,
    import_job_sha256: str,
) -> dict[str, dict[str, Any]]:
    receipt = exact_object(document.value, {"schema", "receipt_kind", "observed_at_utc", "status", "route", "transport", "process_exit_code", "mutation_state", "quarantine_required", "retry_performed", "bindings", "content_observation", "gates", "limitations"}, "interchange_observation")
    if (
        receipt["schema"] != INTERCHANGE_SCHEMA
        or receipt["receipt_kind"] != "operator_summarized_live_observation"
        or receipt["status"] != "imported_inspected_machine_probe"
        or receipt["route"] != "UnrealEditor-Cmd -run=pythonscript"
        or receipt["transport"] != "local_process_no_studio_socket"
        or receipt["process_exit_code"] != 0
        or receipt["mutation_state"] != "complete"
        or receipt["quarantine_required"] is not False
        or receipt["retry_performed"] is not False
    ):
        fail("SMOKE_INTERCHANGE_OBSERVATION_INVALID", "Interchange observation is not the completed commandlet machine probe", pointer="interchange_observation")
    _safe_string(receipt["observed_at_utc"], "interchange_observation/observed_at_utc", maximum=64)
    bindings = exact_object(receipt["bindings"], {"engine_version", "project_path", "project_sha256", "source_revision", "source_manifest_sha256", "source_tree_sha256", "import_job_sha256", "preparation_receipt_sha256", "commandlet_script_sha256", "unreal_log_sha256"}, "interchange_observation/bindings")
    if (
        not str(bindings["engine_version"]).startswith("5.3.2-")
        or bindings["source_revision"] != SOURCE_REVISION
        or bindings["source_manifest_sha256"] != source_manifest_sha256
        or bindings["source_tree_sha256"] != SOURCE_TREE_SHA256
        or bindings["import_job_sha256"] != import_job_sha256
        or not isinstance(bindings["project_path"], str)
        or not bindings["project_path"].startswith("/")
    ):
        fail("SMOKE_EVIDENCE_MISMATCH", "Interchange observation does not bind the exact import inputs", pointer="interchange_observation/bindings")
    for key in ("project_sha256", "preparation_receipt_sha256", "commandlet_script_sha256", "unreal_log_sha256"):
        _safe_sha(bindings[key], f"interchange_observation/bindings/{key}")
    content = exact_object(receipt["content_observation"], {"asset_count", "uasset_count", "uasset_tree_digest", "uasset_tree_digest_algorithm", "all_tasks_synchronous", "all_destinations_were_absent_before_import", "all_returned_objects_present_in_post_import_inventory", "assets"}, "interchange_observation/content_observation")
    if (
        content["asset_count"] != 3
        or content["uasset_count"] != 15
        or content["uasset_tree_digest_algorithm"] != "sha256(sorted absolute-path sha256sum records)"
        or content["all_tasks_synchronous"] is not True
        or content["all_destinations_were_absent_before_import"] is not True
        or content["all_returned_objects_present_in_post_import_inventory"] is not True
    ):
        fail("SMOKE_INTERCHANGE_OBSERVATION_INVALID", "Interchange inventory completeness differs", pointer="interchange_observation/content_observation")
    _safe_sha(content["uasset_tree_digest"], "interchange_observation/content_observation/uasset_tree_digest")
    raw_assets = content["assets"]
    if not isinstance(raw_assets, list) or len(raw_assets) != 3:
        fail("SMOKE_ASSET_SET_INVALID", "Interchange observation must contain exactly three assets", pointer="interchange_observation/content_observation/assets")
    assets: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(raw_assets):
        pointer = f"interchange_observation/content_observation/assets/{index}"
        if not isinstance(raw, dict):
            fail("SMOKE_INPUT_SCHEMA_INVALID", "Observed asset must be an object", pointer=pointer)
        asset_id = raw.get("asset_id")
        definition = ASSETS.get(asset_id) if isinstance(asset_id, str) else None
        if definition is None or asset_id in assets:
            fail("SMOKE_ASSET_SET_INVALID", "Observed asset IDs differ from the fixed smoke set", pointer=pointer)
        packed_key = "packed_metallic_roughness_srgb" if asset_id == "polyhaven_shelf_01" else "packed_roughness_srgb"
        asset = exact_object(raw, {"asset_id", "static_mesh", "material", "bounds_dimensions_cm", "simple_convex_count", "base_color_srgb", packed_key, "normal_srgb", "normal_compression", "normal_flip_green_channel"}, pointer)
        if (
            asset["static_mesh"] != definition["ue_path"]
            or asset["material"] != definition["material_path"]
            or asset["simple_convex_count"] != 1
            or asset["base_color_srgb"] is not True
            or asset[packed_key] is not False
            or asset["normal_srgb"] is not False
            or asset["normal_compression"] != "TC_NORMALMAP"
            or asset["normal_flip_green_channel"] is not True
        ):
            fail("SMOKE_EVIDENCE_MISMATCH", "Observed UE asset path or machine PBR metadata differs", pointer=pointer)
        _finite_vector(asset["bounds_dimensions_cm"], definition["dimensions_cm"], f"{pointer}/bounds_dimensions_cm", tolerance=1e-5)
        assets[asset_id] = asset
    gates = exact_object(receipt["gates"], {"machine_import_inventory", "material_texture_dependency", "interchange_pipeline_fingerprint", "rendered_scale_contact_collision_pbr", "production_ready", "semantic_index_eligible"}, "interchange_observation/gates")
    if gates != {
        "machine_import_inventory": "passed",
        "material_texture_dependency": "review_pending",
        "interchange_pipeline_fingerprint": "review_pending",
        "rendered_scale_contact_collision_pbr": "review_pending",
        "production_ready": False,
        "semantic_index_eligible": False,
    }:
        fail("SMOKE_READINESS_ESCALATION_REJECTED", "Interchange gates must remain non-production and review-pending", pointer="interchange_observation/gates")
    if not isinstance(receipt["limitations"], list) or len(receipt["limitations"]) != 3 or not all(isinstance(item, str) and item for item in receipt["limitations"]):
        fail("SMOKE_INTERCHANGE_OBSERVATION_INVALID", "Interchange limitations are incomplete", pointer="interchange_observation/limitations")
    return assets


def validate_scene_build_observation(document: PinnedJson, observed_ue_paths: set[str]) -> dict[str, Any]:
    receipt = exact_object(document.value, {"actor_count", "attempts", "captured_at", "engine", "execution", "output_map", "production_ready", "referenced_scene_assets", "review_gates", "schema", "semantic_index_eligible", "status", "source_revision"}, "scene_build_observation")
    if (
        receipt["schema"] != SCENE_BUILD_SCHEMA
        or receipt["status"] != "saved_machine_probe"
        or receipt["source_revision"] != SCENE_SOURCE_REVISION
        or receipt["actor_count"] != 14
        or receipt["production_ready"] is not False
        or receipt["semantic_index_eligible"] is not False
        or not str(receipt["engine"]).startswith("5.3.2-")
    ):
        fail("SMOKE_SCENE_OBSERVATION_INVALID", "Scene-build receipt is not the fixed non-production machine probe", pointer="scene_build_observation")
    _safe_string(receipt["captured_at"], "scene_build_observation/captured_at", maximum=64)
    execution = exact_object(receipt["execution"], {"gpu_assignment", "null_rhi", "pixel_streaming_enabled", "project_plugin_unreal_mcp_enabled", "timeout_seconds"}, "scene_build_observation/execution")
    if execution != {"gpu_assignment": "CUDA_VISIBLE_DEVICES=0", "null_rhi": True, "pixel_streaming_enabled": False, "project_plugin_unreal_mcp_enabled": False, "timeout_seconds": 300}:
        fail("SMOKE_SCENE_OBSERVATION_INVALID", "Scene execution contract differs from the fixed NullRHI probe", pointer="scene_build_observation/execution")
    output_map = exact_object(receipt["output_map"], {"asset_path", "bytes", "file", "sha256"}, "scene_build_observation/output_map")
    if (
        output_map["asset_path"] != SCENE_ASSET_PATH
        or output_map["bytes"] != 23_886
        or output_map["sha256"] != SCENE_MAP_SHA256
        or not isinstance(output_map["file"], str)
        or not output_map["file"].endswith("/Content/VISTA/Scenes/MMG040_Office_CommandletR3.umap")
    ):
        fail("SMOKE_EVIDENCE_MISMATCH", "Scene map binding differs from the fixed saved probe", pointer="scene_build_observation/output_map")
    references = receipt["referenced_scene_assets"]
    expected_references = observed_ue_paths | {
        "/Game/CityDatabase/meshes/SM_chair_b.SM_chair_b",
        "/Game/Human_Avatar/DefaultCharacter/ThirdPerson/Blueprints/BP_ThirdPersonCharacter.BP_ThirdPersonCharacter_C",
    }
    if not isinstance(references, list) or len(references) != 5 or set(references) != expected_references or len(set(references)) != 5:
        fail("SMOKE_EVIDENCE_MISMATCH", "Scene does not reference the exact observed asset set", pointer="scene_build_observation/referenced_scene_assets")
    gates = exact_object(receipt["review_gates"], {"animation_and_ik", "collision_and_contact", "pbr_render", "rendered_scale", "screenshot"}, "scene_build_observation/review_gates")
    if set(gates.values()) != {"review_pending"}:
        fail("SMOKE_READINESS_ESCALATION_REJECTED", "Scene review gates must all remain pending", pointer="scene_build_observation/review_gates")
    attempts = receipt["attempts"]
    if not isinstance(attempts, list) or len(attempts) != 3 or [attempt.get("status") if isinstance(attempt, dict) else None for attempt in attempts] != ["failed_unsaved_quarantined", "failed_unsaved_quarantined", "saved_machine_probe"]:
        fail("SMOKE_SCENE_OBSERVATION_INVALID", "Scene-build attempt lineage is incomplete", pointer="scene_build_observation/attempts")
    return output_map


@dataclass(frozen=True)
class PreparedCatalog:
    bundle: dict[str, Any]
    records: tuple[tuple[str, dict[str, Any]], ...]
    category_index: dict[str, Any]
    bindings: dict[str, dict[str, str]]
    gates: dict[str, str]
    catalog_sha256: str
    seal_sha256: str


def _frame(digest: Any, value: bytes) -> None:
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def _catalog_digest(records: Sequence[tuple[str, dict[str, Any]]]) -> str:
    digest = hashlib.sha256(CATALOG_DIGEST_DOMAIN)
    for relative, record in records:
        _frame(digest, relative.encode("utf-8"))
        _frame(digest, canonical_json(record, pretty=False))
    return digest.hexdigest()


def _prepared_seal_sha256(
    *,
    bundle_without_seal: Mapping[str, Any],
    records: Sequence[tuple[str, dict[str, Any]]],
    category_index: Mapping[str, Any],
    bindings: Mapping[str, Any],
    gates: Mapping[str, Any],
    catalog_sha256: str,
) -> str:
    material = {
        "bundle": bundle_without_seal,
        "records": [{"relative_path": relative, "record": record} for relative, record in records],
        "category_index": category_index,
        "bindings": bindings,
        "gates": gates,
        "catalog_sha256": catalog_sha256,
        "snapshot_identity": SMOKE_SNAPSHOT_ID,
    }
    digest = hashlib.sha256(PREPARED_SEAL_DOMAIN)
    digest.update(canonical_json(material, pretty=False))
    return digest.hexdigest()


def smoke_asset_id(source_asset_id: str) -> str:
    return f"{SMOKE_NAMESPACE}__{source_asset_id}"


def _record(asset_id: str, definition: Mapping[str, Any], observation: Mapping[str, Any], bindings: Mapping[str, Mapping[str, str]], uasset_tree_sha256: str) -> dict[str, Any]:
    width, depth, height = (float(value) / 100.0 for value in observation["bounds_dimensions_cm"])
    radius = math.sqrt((width / 2) ** 2 + (depth / 2) ** 2 + (height / 2) ** 2)
    source_prefix = str(definition["source_asset_id"]) + "/"
    source_files = [
        {"path": path, "sha256": digest, "bytes": size}
        for path, (size, digest) in sorted(SOURCE_FILES.items())
        if path.startswith(source_prefix)
    ]
    namespaced_asset_id = smoke_asset_id(asset_id)
    return {
        "identity": {
            "asset_id": namespaced_asset_id,
            "namespace": SMOKE_NAMESPACE,
            "snapshot_identity": SMOKE_SNAPSHOT_ID,
            "name": definition["name"],
            "category": definition["category"],
            "subcategory": definition["subcategory"],
            "source_pack": "polyhaven_cc0_vista_mmg040_nonproduction_smoke",
        },
        "semantic": {
            "short_description": definition["short_description"],
            "description": definition["description"],
            "tags": definition["tags"],
            "style": "operator_curated_unreviewed",
            "materials": definition["materials"],
            "color_palette": definition["colors"],
            "mood": ["neutral"],
            "typical_placement": definition["placements"],
            "function": definition["function"],
            "affordances": definition["affordances"],
            "scene_types": ["indoor", "office", "storage"],
            "condition": "unreviewed",
            "setting": "indoor",
        },
        "geometry": {
            "dimensions_m": {"width": round(width, 6), "depth": round(depth, 6), "height": round(height, 6)},
            "footprint_m": {"width": round(width, 6), "depth": round(depth, 6)},
            "bounding_radius_m": round(radius, 6),
            "up_axis": "Z",
            "pivot": "unreviewed",
            "is_symmetric": None,
            "default_scale": 1.0,
        },
        "technical": {
            "unreal_asset_path": definition["ue_path"],
            "asset_type": "StaticMesh",
            "mobility": "Static",
            "has_collision": True,
            "triangle_count": None,
            "lod_count": None,
            "material_slots": [definition["material_path"]],
            "observed_simple_convex_count": 1,
        },
        "indexing": {
            "render_views": [],
            "view_count": 0,
            "asset_snapshot_revision": SMOKE_SNAPSHOT_ID,
            "caption_model": "none_operator_curated_nonproduction_smoke",
            "schema_version": "1.0",
            "view_policy": {
                "available_views": 0,
                "nonproduction_smoke_min_views": 0,
                "production_min_views": 4,
                "production_view_requirement_compatible": False,
            },
        },
        "provenance": {
            "lineage": "external_polyhaven_cc0_operator_observed_noncanonical",
            "canonical_official_asset": False,
            "source_asset_id": asset_id,
            "provider": "Poly Haven",
            "license": "CC0-1.0",
            "source_revision": SOURCE_REVISION,
            "source_files": source_files,
            "evidence": copy.deepcopy(bindings),
            "ue_observation": {
                "static_mesh": definition["ue_path"],
                "material": definition["material_path"],
                "uasset_tree_sha256": uasset_tree_sha256,
                "uasset_tree_digest_scope": "three_asset_import_aggregate_not_individual_package_hash",
                "scene_asset_path": SCENE_ASSET_PATH,
                "scene_map_sha256": SCENE_MAP_SHA256,
            },
        },
        "readiness": {
            "scope": SCOPE,
            "snapshot_identity": SMOKE_SNAPSHOT_ID,
            "production_ready": False,
            "semantic_index_eligible": False,
            "generic_consumers_permitted": False,
            "database_migration_permitted": False,
            "category_index_rebuild_permitted": False,
            "gates": review_gates(),
            "limitations": [
                "operator_curated_semantics_without_text_or_visual_review_provider",
                "no_rendered_asset_screenshot_or_scale_contact_collision_pbr_review",
                "not_canonical_official_asset_lineage_and_not_for_production_registration",
            ],
        },
    }


def build_catalog(
    *,
    source_manifest_path: Path,
    source_manifest_sha256: str,
    import_job_path: Path,
    import_job_sha256: str,
    interchange_observation_path: Path,
    interchange_observation_sha256: str,
    scene_build_observation_path: Path,
    scene_build_observation_sha256: str,
) -> PreparedCatalog:
    source = read_pinned_json(source_manifest_path, source_manifest_sha256, "source-manifest")
    import_job = read_pinned_json(import_job_path, import_job_sha256, "import-job")
    interchange = read_pinned_json(interchange_observation_path, interchange_observation_sha256, "interchange-observation")
    scene = read_pinned_json(scene_build_observation_path, scene_build_observation_sha256, "scene-build-observation")
    for document, expected in (
        (source, SOURCE_MANIFEST_SHA256),
        (import_job, IMPORT_JOB_SHA256),
        (interchange, INTERCHANGE_OBSERVATION_SHA256),
        (scene, SCENE_BUILD_OBSERVATION_SHA256),
    ):
        validate_independent_evidence_pin(document, expected)
    validate_source_manifest(source)
    validate_import_job(import_job, source.sha256)
    observations = validate_interchange_observation(interchange, source_manifest_sha256=source.sha256, import_job_sha256=import_job.sha256)
    validate_scene_build_observation(scene, {definition["ue_path"] for definition in ASSETS.values()})
    bindings = {
        "source_manifest": {"schema": SOURCE_SCHEMA, "sha256": source.sha256},
        "import_job": {"schema": IMPORT_JOB_SCHEMA, "sha256": import_job.sha256},
        "interchange_observation": {"schema": INTERCHANGE_SCHEMA, "sha256": interchange.sha256},
        "scene_build_observation": {"schema": SCENE_BUILD_SCHEMA, "sha256": scene.sha256},
    }
    uasset_tree_sha256 = interchange.value["content_observation"]["uasset_tree_digest"]
    records_list: list[tuple[str, dict[str, Any]]] = []
    for asset_id in sorted(ASSETS):
        definition = ASSETS[asset_id]
        relative = f"{definition['category']}/{smoke_asset_id(asset_id)}.json"
        records_list.append((relative, _record(asset_id, definition, observations[asset_id], bindings, uasset_tree_sha256)))
    records = tuple(sorted(records_list, key=lambda item: item[0]))
    catalog_sha256 = _catalog_digest(records)
    categories: list[dict[str, Any]] = []
    for category_id in sorted({definition["category"] for definition in ASSETS.values()}):
        category_assets = []
        for _relative, record in records:
            if record["identity"]["category"] == category_id:
                category_assets.append(
                    {
                        "asset_id": record["identity"]["asset_id"],
                        "name": record["identity"]["name"],
                        "subcategory": record["identity"]["subcategory"],
                        "setting": record["semantic"]["setting"],
                    }
                )
        categories.append({"id": category_id, "description": CATEGORY_DESCRIPTIONS[category_id], "count": len(category_assets), "assets": category_assets})
    category_index = {
        "schema_version": "1.0",
        "total_assets": 3,
        "categories": categories,
        "scope": SCOPE,
        "snapshot_identity": SMOKE_SNAPSHOT_ID,
        "production_ready": False,
        "semantic_index_eligible": False,
        "generic_consumers_permitted": False,
        "database_migration_permitted": False,
        "category_index_rebuild_permitted": False,
    }
    gates = review_gates()
    bundle_without_seal = {
        "schema": BUNDLE_SCHEMA,
        "scope": SCOPE,
        "namespace": SMOKE_NAMESPACE,
        "snapshot_identity": SMOKE_SNAPSHOT_ID,
        "production_ready": False,
        "semantic_index_eligible": False,
        "generic_consumers_permitted": False,
        "database_migration_permitted": False,
        "category_index_rebuild_permitted": False,
        "asset_count": 3,
        "catalog_sha256": catalog_sha256,
        "bindings": copy.deepcopy(bindings),
        "review_gates": review_gates(),
        "view_policy": {
            "available_views": 0,
            "nonproduction_smoke_min_views": 0,
            "production_min_views": 4,
            "production_view_requirement_compatible": False,
        },
        "records": copy.deepcopy([{"relative_path": relative, "record": record} for relative, record in records]),
        "nonproduction_category_index": copy.deepcopy(category_index),
    }
    seal_sha256 = _prepared_seal_sha256(
        bundle_without_seal=bundle_without_seal,
        records=records,
        category_index=category_index,
        bindings=bindings,
        gates=gates,
        catalog_sha256=catalog_sha256,
    )
    bundle = copy.deepcopy(bundle_without_seal)
    bundle["prepared_seal"] = {
        "algorithm": "sha256",
        "domain": PREPARED_SEAL_DOMAIN[:-1].decode("ascii"),
        "sha256": seal_sha256,
    }
    return PreparedCatalog(
        bundle=bundle,
        records=records,
        category_index=category_index,
        bindings=copy.deepcopy(bindings),
        gates=gates,
        catalog_sha256=catalog_sha256,
        seal_sha256=seal_sha256,
    )


def validate_prepared_catalog(prepared: PreparedCatalog) -> None:
    """Reject any mutation between evidence validation and publication."""
    try:
        if len(prepared.records) != 3 or prepared.catalog_sha256 != _catalog_digest(prepared.records):
            raise ValueError
        if prepared.gates != review_gates():
            raise ValueError
        observed_ids: set[str] = set()
        for relative, record in prepared.records:
            identity = record["identity"]
            source_id = record["provenance"]["source_asset_id"]
            definition = ASSETS[source_id]
            namespaced_id = smoke_asset_id(source_id)
            if (
                identity["asset_id"] != namespaced_id
                or identity["namespace"] != SMOKE_NAMESPACE
                or identity["snapshot_identity"] != SMOKE_SNAPSHOT_ID
                or relative != f"{definition['category']}/{namespaced_id}.json"
                or record["technical"]["unreal_asset_path"] != definition["ue_path"]
                or record["provenance"]["evidence"] != prepared.bindings
                or record["readiness"]["gates"] != review_gates()
                or record["readiness"]["production_ready"] is not False
                or record["readiness"]["semantic_index_eligible"] is not False
                or record["readiness"]["generic_consumers_permitted"] is not False
                or record["indexing"]["view_count"] != 0
                or record["indexing"]["view_policy"]["nonproduction_smoke_min_views"] != 0
                or record["indexing"]["view_policy"]["production_min_views"] != 4
                or record["indexing"]["view_policy"]["production_view_requirement_compatible"] is not False
            ):
                raise ValueError
            observed_ids.add(namespaced_id)
        if observed_ids != {smoke_asset_id(asset_id) for asset_id in ASSETS}:
            raise ValueError
        index_ids = {
            asset["asset_id"]
            for category in prepared.category_index["categories"]
            for asset in category["assets"]
        }
        if (
            prepared.category_index["total_assets"] != 3
            or index_ids != observed_ids
            or prepared.category_index["generic_consumers_permitted"] is not False
            or prepared.category_index["snapshot_identity"] != SMOKE_SNAPSHOT_ID
        ):
            raise ValueError
        bundle_without_seal = copy.deepcopy(prepared.bundle)
        seal = bundle_without_seal.pop("prepared_seal")
        if (
            seal != {
                "algorithm": "sha256",
                "domain": PREPARED_SEAL_DOMAIN[:-1].decode("ascii"),
                "sha256": prepared.seal_sha256,
            }
            or bundle_without_seal["records"] != [{"relative_path": relative, "record": record} for relative, record in prepared.records]
            or bundle_without_seal["nonproduction_category_index"] != prepared.category_index
            or bundle_without_seal["bindings"] != prepared.bindings
            or bundle_without_seal["review_gates"] != prepared.gates
            or bundle_without_seal["catalog_sha256"] != prepared.catalog_sha256
            or bundle_without_seal["generic_consumers_permitted"] is not False
        ):
            raise ValueError
        recomputed_seal = _prepared_seal_sha256(
            bundle_without_seal=bundle_without_seal,
            records=prepared.records,
            category_index=prepared.category_index,
            bindings=prepared.bindings,
            gates=prepared.gates,
            catalog_sha256=prepared.catalog_sha256,
        )
        if recomputed_seal != prepared.seal_sha256:
            raise ValueError
    except (KeyError, TypeError, ValueError, IndexError):
        fail("SMOKE_PREPARED_MUTATED", "Prepared catalog changed after validation and cannot be published")


def validate_output_dir(output_dir: Path) -> Path:
    if not output_dir.is_absolute() or output_dir != Path(os.path.abspath(output_dir)) or output_dir == Path(output_dir.anchor) or not OUTPUT_NAME_RE.fullmatch(output_dir.name):
        fail("SMOKE_OUTPUT_INVALID", "Output must be an absolute, normalized, non-root safe directory", pointer="--output-dir")
    parent = output_dir.parent
    _reject_symlink_components(parent, "--output-dir")
    try:
        metadata = os.lstat(parent)
    except OSError:
        fail("SMOKE_OUTPUT_INVALID", "Output parent must already exist", pointer="--output-dir")
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        fail("SMOKE_OUTPUT_INVALID", "Output parent must be private and owned by the current user", pointer="--output-dir")
    try:
        os.lstat(output_dir)
    except FileNotFoundError:
        return output_dir
    fail("SMOKE_OUTPUT_EXISTS", "Publication is non-overwriting; choose a new output directory", pointer="--output-dir")


def _write_private(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rename_directory_no_replace(source: Path, destination: Path) -> None:
    renameat2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if renameat2 is None:
        fail("SMOKE_ATOMIC_PUBLISH_UNAVAILABLE", "Host does not expose atomic no-replace directory publication")
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1)
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        fail("SMOKE_OUTPUT_EXISTS", "Publication is non-overwriting; choose a new output directory")
    if error_number in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP}:
        fail("SMOKE_ATOMIC_PUBLISH_UNAVAILABLE", "Filesystem does not support atomic no-replace directory publication")
    raise OSError(error_number, os.strerror(error_number), str(destination))


def publish_catalog(prepared: PreparedCatalog, output_dir: Path) -> dict[str, Any]:
    # Seal a private publication snapshot so later mutations of the caller's
    # dictionaries cannot race the files written below.
    prepared = copy.deepcopy(prepared)
    validate_prepared_catalog(prepared)
    output_dir = validate_output_dir(output_dir)
    lock_path = output_dir.parent / f".{output_dir.name}.lock"
    lock_descriptor: int | None = None
    owns_lock = False
    temporary: Path | None = None
    try:
        lock_descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0), 0o600)
        owns_lock = True
        os.close(lock_descriptor)
        lock_descriptor = None
        validate_output_dir(output_dir)
        temporary = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.tmp-", dir=output_dir.parent))
        os.chmod(temporary, 0o700)
        catalog_dir = temporary / "nonproduction_catalog"
        catalog_dir.mkdir(mode=0o700)
        file_descriptors: dict[str, dict[str, Any]] = {}
        category_dirs: set[Path] = set()
        for relative, record in prepared.records:
            category, filename = relative.split("/", 1)
            category_dir = catalog_dir / category
            if not category_dir.exists():
                category_dir.mkdir(mode=0o700)
                category_dirs.add(category_dir)
            payload = canonical_json(record)
            target = category_dir / filename
            _write_private(target, payload)
            file_descriptors[f"nonproduction_catalog/{relative}"] = {"bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
        for category_dir in sorted(category_dirs):
            _fsync_directory(category_dir)
        _fsync_directory(catalog_dir)
        category_payload = canonical_json(prepared.category_index)
        _write_private(temporary / "nonproduction_category_index.json", category_payload)
        file_descriptors["nonproduction_category_index.json"] = {"bytes": len(category_payload), "sha256": hashlib.sha256(category_payload).hexdigest()}
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "bundle_schema": BUNDLE_SCHEMA,
            "scope": SCOPE,
            "namespace": SMOKE_NAMESPACE,
            "snapshot_identity": SMOKE_SNAPSHOT_ID,
            "production_ready": False,
            "semantic_index_eligible": False,
            "generic_consumers_permitted": False,
            "database_migration_permitted": False,
            "category_index_rebuild_permitted": False,
            "asset_count": 3,
            "catalog_sha256": prepared.catalog_sha256,
            "prepared_seal_sha256": prepared.seal_sha256,
            "bindings": copy.deepcopy(prepared.bindings),
            "files": {key: file_descriptors[key] for key in sorted(file_descriptors)},
            "review_gates": review_gates(),
            "view_policy": {
                "available_views": 0,
                "nonproduction_smoke_min_views": 0,
                "production_min_views": 4,
                "production_view_requirement_compatible": False,
            },
            "publication_policy": "private_atomic_non_overwriting_directory",
            "database_connected": False,
            "network_used": False,
            "unreal_started": False,
            "model_called": False,
        }
        _write_private(temporary / "smoke-catalog-receipt.json", canonical_json(receipt))
        _fsync_directory(temporary)
        _rename_directory_no_replace(temporary, output_dir)
        temporary = None
        _fsync_directory(output_dir.parent)
        return receipt
    except FileExistsError:
        fail("SMOKE_OUTPUT_BUSY", "Another publication owns the output lock")
    finally:
        if lock_descriptor is not None:
            os.close(lock_descriptor)
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)
        if owns_lock:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass


class SafeParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        fail("SMOKE_ARGUMENT_INVALID", "CLI arguments are missing or invalid; consult --help")


def build_parser() -> argparse.ArgumentParser:
    parser = SafeParser(description=__doc__)
    for name in ("source-manifest", "import-job", "interchange-observation", "scene-build-observation"):
        parser.add_argument(f"--{name}", required=True)
        parser.add_argument(f"--{name}-sha256", required=True)
    parser.add_argument("--output-dir")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--acknowledge-nonproduction-smoke", default="")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    os.umask(0o077)
    try:
        args = build_parser().parse_args(argv)
        if args.apply and (not args.output_dir or args.acknowledge_nonproduction_smoke != NONPRODUCTION_ACK):
            fail("SMOKE_ARGUMENT_INVALID", f"--apply requires --output-dir and --acknowledge-nonproduction-smoke {NONPRODUCTION_ACK}")
        if not args.apply and args.acknowledge_nonproduction_smoke:
            fail("SMOKE_ARGUMENT_INVALID", "Non-production acknowledgement is accepted only with --apply")
        output_dir = Path(args.output_dir) if args.output_dir else None
        if output_dir is not None:
            validate_output_dir(output_dir)
        prepared = build_catalog(
            source_manifest_path=Path(args.source_manifest),
            source_manifest_sha256=args.source_manifest_sha256,
            import_job_path=Path(args.import_job),
            import_job_sha256=args.import_job_sha256,
            interchange_observation_path=Path(args.interchange_observation),
            interchange_observation_sha256=args.interchange_observation_sha256,
            scene_build_observation_path=Path(args.scene_build_observation),
            scene_build_observation_sha256=args.scene_build_observation_sha256,
        )
        if args.apply:
            assert output_dir is not None
            publish_catalog(prepared, output_dir)
        sys.stdout.buffer.write(canonical_json(prepared.bundle))
        return 0
    except SmokeCatalogError as error:
        sys.stderr.buffer.write(canonical_json({"schema": BUNDLE_SCHEMA, "valid": False, "error": error.public_dict()}))
        return 2
    except Exception:
        sys.stderr.buffer.write(canonical_json({"schema": BUNDLE_SCHEMA, "valid": False, "error": {"code": "SMOKE_INTERNAL_ERROR", "message": "Catalog preparation failed before a safe result was produced"}}))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
