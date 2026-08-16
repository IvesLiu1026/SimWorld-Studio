"""Independent structural inspection for r2 GLB and manifest outputs."""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import re
import struct
from typing import Any, Mapping, Sequence

from .config import ForgeInputError, canonical_json_bytes, load_json_object, sha256_file
from .external_assets import (
    EXTERNAL_MATERIAL_ALPHA_CUTOFF,
    EXTERNAL_MATERIAL_ALPHA_CUTOFF_PROPERTY,
    EXTERNAL_MATERIAL_ALPHA_MODE_PROPERTY,
    EXTERNAL_MATERIAL_ALPHA_POLICY_PROPERTY,
    EXTERNAL_MATERIAL_ALPHA_SANITIZATION,
    EXTERNAL_MATERIAL_SEMANTICS_PROPERTY,
    EXTERNAL_MATERIAL_SOURCE_DIGEST_PROPERTY,
    EXTERNAL_MATERIAL_SOURCE_PROPERTY,
    external_material_alpha_policy,
    external_material_name_prefix,
)


GLB_MAGIC = 0x46546C67
GLB_JSON_CHUNK = 0x4E4F534A
SHA256 = re.compile(r"^[0-9a-f]{64}$")
UE_BUNDLE_ARTIFACT_KIND = "ue_import_bundle"
UE_BUNDLE_ROOT_TRANSFORM_POLICY = "room_local_geometry_identity_root"
UE_BUNDLE_SEMANTIC_POLICY = "presentation_only_preserve_r1_authority"
UE_BUNDLE_COLLISION_POLICY = "presentation_no_collision_use_hidden_r1_proxies"
UE_BUNDLE_UNREAL_COLLISION_PROFILE = "NoCollision"
UE_BUNDLE_REQUIRED_KEYS = {
    "artifact_id",
    "artifact_kind",
    "target_asset_id",
    "room_id",
    "room_kind",
    "relative_path",
    "media_type",
    "sha256",
    "size_bytes",
    "mesh_count",
    "material_count",
    "pbr_complete_material_count",
    "texture_count",
    "material_ids",
    "expected_world_transform_cm",
    "bundle_root_transform",
    "root_transform_policy",
    "semantic_policy",
    "collision_policy",
    "unreal_collision_profile",
    "cameras_exported",
    "lights_exported",
    "source_hashes",
}
UE_BUNDLE_V2_REQUIRED_KEYS = UE_BUNDLE_REQUIRED_KEYS | {"external_content"}


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ForgeInputError(f"GLB JSON contains duplicate key {key!r}")
        result[key] = value
    return result


def _identity_node_transform(node: Mapping[str, Any]) -> bool:
    identity_matrix = (
        1.0, 0.0, 0.0, 0.0,
        0.0, 1.0, 0.0, 0.0,
        0.0, 0.0, 1.0, 0.0,
        0.0, 0.0, 0.0, 1.0,
    )
    matrix = node.get("matrix")
    if matrix is not None and any(key in node for key in ("translation", "rotation", "scale")):
        return False
    if matrix is not None and (
        not isinstance(matrix, list)
        or len(matrix) != 16
        or any(abs(float(value) - identity_matrix[index]) > 1e-6 for index, value in enumerate(matrix))
    ):
        return False
    expected = {
        "translation": (0.0, 0.0, 0.0),
        "rotation": (0.0, 0.0, 0.0, 1.0),
        "scale": (1.0, 1.0, 1.0),
    }
    for key, wanted in expected.items():
        value = node.get(key)
        if value is None:
            continue
        if (
            not isinstance(value, list)
            or len(value) != len(wanted)
            or any(abs(float(item) - wanted[index]) > 1e-6 for index, item in enumerate(value))
        ):
            return False
    return True


def _pbr_material_record(material: Any) -> dict[str, Any]:
    if not isinstance(material, Mapping):
        material = {}
    pbr = material.get("pbrMetallicRoughness", {})
    if not isinstance(pbr, Mapping):
        pbr = {}
    base_color_texture = isinstance(pbr.get("baseColorTexture"), Mapping)
    metallic_roughness_texture = isinstance(pbr.get("metallicRoughnessTexture"), Mapping)
    normal_texture = isinstance(material.get("normalTexture"), Mapping)
    return {
        "name": material.get("name"),
        "base_color_texture": base_color_texture,
        "metallic_roughness_texture": metallic_roughness_texture,
        "normal_texture": normal_texture,
        "complete_base_normal_roughness": bool(
            base_color_texture and metallic_roughness_texture and normal_texture
        ),
    }


def _external_material_alpha_record(material: Any, material_index: int) -> dict[str, Any]:
    if not isinstance(material, Mapping):
        material = {}
    extras = material.get("extras", {})
    if not isinstance(extras, Mapping):
        extras = {}
    raw_mode = material.get("alphaMode", "OPAQUE")
    explicit_cutoff = "alphaCutoff" in material
    raw_cutoff = material.get("alphaCutoff")
    effective_cutoff = (
        (EXTERNAL_MATERIAL_ALPHA_CUTOFF if raw_cutoff is None else raw_cutoff)
        if raw_mode == "MASK"
        else None
    )
    return {
        "material_index": material_index,
        "name": material.get("name"),
        "source_logical_asset_id": extras.get(EXTERNAL_MATERIAL_SOURCE_PROPERTY),
        "source_tree_sha256": extras.get(EXTERNAL_MATERIAL_SOURCE_DIGEST_PROPERTY),
        "receipt_texture_semantics_json": extras.get(EXTERNAL_MATERIAL_SEMANTICS_PROPERTY),
        "declared_alpha_mode": extras.get(EXTERNAL_MATERIAL_ALPHA_MODE_PROPERTY),
        "declared_alpha_cutoff": extras.get(EXTERNAL_MATERIAL_ALPHA_CUTOFF_PROPERTY),
        "sanitization_policy": extras.get(EXTERNAL_MATERIAL_ALPHA_POLICY_PROPERTY),
        "gltf_alpha_mode": raw_mode,
        "gltf_alpha_cutoff": effective_cutoff,
        "gltf_alpha_cutoff_explicit": explicit_cutoff,
    }


def inspect_glb(
    path: pathlib.Path,
    *,
    include_external_material_alpha: bool = False,
) -> dict[str, Any]:
    with path.open("rb") as handle:
        header = handle.read(12)
        if len(header) != 12:
            raise ForgeInputError(f"truncated GLB header: {path}")
        magic, version, total_length = struct.unpack("<III", header)
        if magic != GLB_MAGIC or version != 2 or total_length != path.stat().st_size:
            raise ForgeInputError(f"invalid GLB 2.0 header: {path}")
        chunk_header = handle.read(8)
        if len(chunk_header) != 8:
            raise ForgeInputError(f"missing GLB JSON chunk: {path}")
        chunk_length, chunk_type = struct.unpack("<II", chunk_header)
        if chunk_type != GLB_JSON_CHUNK:
            raise ForgeInputError(f"first GLB chunk is not JSON: {path}")
        try:
            document = json.loads(
                handle.read(chunk_length).decode("utf-8").rstrip(" \t\r\n\x00"),
                object_pairs_hook=_reject_duplicate_pairs,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ForgeInputError(f"invalid GLB JSON chunk: {path}") from exc
    if not isinstance(document, Mapping):
        raise ForgeInputError(f"GLB JSON root is not an object: {path}")
    nodes = document.get("nodes", [])
    extras = [node.get("extras", {}) for node in nodes if isinstance(node, dict)]
    component_extras = [item for item in extras if isinstance(item, dict) and item.get("vista_component_id")]
    bundle_nodes = [
        node
        for node in nodes
        if isinstance(node, dict)
        and isinstance(node.get("extras"), dict)
        and node["extras"].get("vista_bundle_contract")
        in {"one_room_one_mesh_v1", "one_room_one_mesh_v2"}
    ]
    mesh_nodes = [node for node in nodes if isinstance(node, dict) and isinstance(node.get("mesh"), int)]
    materials = document.get("materials", [])
    if not isinstance(materials, list):
        materials = []
    material_records = [_pbr_material_record(item) for item in materials]
    document_extensions = document.get("extensions", {})
    if not isinstance(document_extensions, Mapping):
        document_extensions = {}
    punctual = document_extensions.get("KHR_lights_punctual", {})
    lights = punctual.get("lights", []) if isinstance(punctual, Mapping) else []
    if not isinstance(lights, list):
        lights = []
    meshes = document.get("meshes", [])
    if not isinstance(meshes, list):
        meshes = []
    result = {
        "relative_or_absolute_path": str(path),
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
        "asset_version": document.get("asset", {}).get("version"),
        "scene_count": len(document.get("scenes", [])),
        "node_count": len(nodes),
        "mesh_count": len(meshes),
        "mesh_node_count": len(mesh_nodes),
        "mesh_primitive_count": sum(
            len(mesh.get("primitives", []))
            for mesh in meshes
            if isinstance(mesh, Mapping) and isinstance(mesh.get("primitives", []), list)
        ),
        "material_count": len(materials),
        "material_names": [item["name"] for item in material_records],
        "pbr_complete_material_count": sum(
            item["complete_base_normal_roughness"] for item in material_records
        ),
        "pbr_materials": material_records,
        "image_count": len(document.get("images", [])),
        "texture_count": len(document.get("textures", [])),
        "camera_count": len(document.get("cameras", [])),
        "light_count": len(lights),
        "component_extra_count": len(component_extras),
        "component_roles": sorted({str(item.get("vista_export_role")) for item in component_extras}),
        "bundle_node_count": len(bundle_nodes),
        "bundle_root_is_identity": (
            _identity_node_transform(bundle_nodes[0]) if len(bundle_nodes) == 1 else None
        ),
        "bundle_metadata": dict(bundle_nodes[0]["extras"]) if len(bundle_nodes) == 1 else {},
        "extensions_used": sorted(document.get("extensionsUsed", [])),
        "extensions_required": sorted(document.get("extensionsRequired", [])),
    }
    if include_external_material_alpha:
        result["external_material_alpha_contracts"] = [
            _external_material_alpha_record(item, index)
            for index, item in enumerate(materials)
        ]
    return result


def _safe_artifact_path(output_root: pathlib.Path, relative_path: Any) -> tuple[pathlib.Path, str]:
    if not isinstance(relative_path, str) or not relative_path:
        raise ForgeInputError("GLB artifact relative_path must be a non-empty string")
    relative = pathlib.PurePosixPath(relative_path)
    if relative.is_absolute() or ".." in relative.parts or "\\" in relative_path:
        raise ForgeInputError(f"unsafe GLB artifact relative_path: {relative_path!r}")
    candidate = output_root
    for part in relative.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ForgeInputError(f"GLB artifact path contains a symbolic link: {relative_path!r}")
    path = candidate.resolve(strict=True)
    if not path.is_relative_to(output_root) or not path.is_file():
        raise ForgeInputError(
            f"GLB artifact escapes output root or is not a regular file: {relative_path!r}"
        )
    return path, relative.as_posix()


def _finite_vector(value: Any, length: int) -> bool:
    return (
        isinstance(value, list)
        and len(value) == length
        and all(
            not isinstance(item, bool)
            and isinstance(item, (int, float))
            and math.isfinite(float(item))
            for item in value
        )
    )


def _validate_transform(transform: Any, *, units: str) -> None:
    if not isinstance(transform, Mapping):
        raise ForgeInputError("UE bundle transform must be an object")
    location_key = "location_cm" if units == "cm" else "location_m"
    if set(transform) != {location_key, "rotation_deg", "scale"}:
        raise ForgeInputError("UE bundle transform fields are not closed")
    if not all(_finite_vector(transform[key], 3) for key in (location_key, "rotation_deg", "scale")):
        raise ForgeInputError("UE bundle transform contains invalid vectors")
    if any(float(value) <= 0 for value in transform["scale"]):
        raise ForgeInputError("UE bundle transform scale must be positive")


def _validate_bundle_record(record: Any) -> Mapping[str, Any]:
    if not isinstance(record, Mapping) or frozenset(record) not in {
        frozenset(UE_BUNDLE_REQUIRED_KEYS),
        frozenset(UE_BUNDLE_V2_REQUIRED_KEYS),
    }:
        raise ForgeInputError("UE bundle record fields are not a closed v1/v2 contract")
    is_external = "external_content" in record
    kind = record.get("room_kind")
    room_id = record.get("room_id")
    if kind not in {"entry_hall", "living_room", "kitchen_dining"}:
        raise ForgeInputError("UE bundle room_kind is unsupported")
    if room_id != f"home.r1/room.{kind}":
        raise ForgeInputError("UE bundle room identity does not match room_kind")
    if (
        record.get("artifact_id") != f"ue_bundle.room.{kind}"
        or record.get("artifact_kind") != UE_BUNDLE_ARTIFACT_KIND
        or record.get("target_asset_id") != f"asset.bundle.{kind}"
        or record.get("relative_path")
        != f"ue_import_bundles/{kind}_presentation_bundle.glb"
        or record.get("media_type") != "model/gltf-binary"
    ):
        raise ForgeInputError("UE bundle artifact identity is invalid")
    if (
        record.get("root_transform_policy") != UE_BUNDLE_ROOT_TRANSFORM_POLICY
        or record.get("semantic_policy") != UE_BUNDLE_SEMANTIC_POLICY
        or record.get("collision_policy") != UE_BUNDLE_COLLISION_POLICY
        or record.get("unreal_collision_profile") != UE_BUNDLE_UNREAL_COLLISION_PROFILE
        or record.get("cameras_exported") is not False
        or record.get("lights_exported") is not False
    ):
        raise ForgeInputError("UE bundle presentation policy is invalid")
    _validate_transform(record.get("expected_world_transform_cm"), units="cm")
    _validate_transform(record.get("bundle_root_transform"), units="m")
    if record["bundle_root_transform"] != {
        "location_m": [0, 0, 0],
        "rotation_deg": [0, 0, 0],
        "scale": [1, 1, 1],
    }:
        raise ForgeInputError("UE bundle declared root transform is not identity")
    hashes = record.get("source_hashes")
    if (
        not isinstance(hashes, Mapping)
        or set(hashes) != {"house_sha256", "visual_profile_sha256", "forge_plan_sha256"}
        or any(not isinstance(value, str) or SHA256.fullmatch(value) is None for value in hashes.values())
    ):
        raise ForgeInputError("UE bundle source hashes are invalid")
    material_ids = record.get("material_ids")
    if (
        not isinstance(material_ids, list)
        or len(material_ids) < 2
        or material_ids != sorted(set(material_ids))
        or any(
            not isinstance(item, str)
            or not item
            or len(item) > 96
            or (not is_external and not item.startswith("r2."))
            or (is_external and re.fullmatch(r"[A-Za-z0-9_.-]+", item) is None)
            for item in material_ids
        )
    ):
        raise ForgeInputError("UE bundle material IDs are invalid")
    integers = {
        "size_bytes": 1,
        "mesh_count": 1,
        "material_count": 2,
        "pbr_complete_material_count": 2,
        "texture_count": 3 if is_external else 6,
    }
    for key, minimum in integers.items():
        value = record.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
            raise ForgeInputError(f"UE bundle {key} is invalid")
    if (
        record["mesh_count"] != 1
        or record["material_count"] != len(material_ids)
        or record["pbr_complete_material_count"] != record["material_count"]
        or (
            record["texture_count"] < (3 if is_external else record["material_count"] * 3)
        )
        or not isinstance(record.get("sha256"), str)
        or SHA256.fullmatch(record["sha256"]) is None
    ):
        raise ForgeInputError("UE bundle mesh/material/hash contract is invalid")
    if is_external:
        _validate_external_content(record["external_content"])
    return record


def _validate_external_content(value: Any) -> None:
    keys = {
        "schema_version", "normalization_policy", "acquisition_receipt",
        "placement_manifest_sha256", "placement_plan_sha256",
        "semantic_target_ids", "dressing_ids", "asset_sources",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ForgeInputError("UE bundle external content fields are not closed")
    if (
        value.get("schema_version") != "simworld.vista.playable-home-external-placement/v1"
        or value.get("normalization_policy")
        != "measured_combined_bounds_floor_center_uniform_scale_v1"
    ):
        raise ForgeInputError("UE bundle external placement policy is invalid")
    for key in ("placement_manifest_sha256", "placement_plan_sha256"):
        if not isinstance(value.get(key), str) or SHA256.fullmatch(value[key]) is None:
            raise ForgeInputError("UE bundle external placement digest is invalid")
    receipt = value.get("acquisition_receipt")
    if not isinstance(receipt, Mapping) or set(receipt) != {
        "provider", "receipt_schema_version", "receipt_digest",
        "receipt_file_sha256", "acquisition_manifest_sha256",
    }:
        raise ForgeInputError("UE bundle acquisition receipt reference is invalid")
    if (
        receipt.get("provider") != "poly_haven"
        or receipt.get("receipt_schema_version")
        != "simworld.vista.playable-home-poly-haven-receipt/v1"
        or any(
            not isinstance(receipt.get(key), str) or SHA256.fullmatch(receipt[key]) is None
            for key in ("receipt_digest", "receipt_file_sha256", "acquisition_manifest_sha256")
        )
    ):
        raise ForgeInputError("UE bundle acquisition receipt digests are invalid")
    semantic_ids = value.get("semantic_target_ids")
    dressing_ids = value.get("dressing_ids")
    if (
        not isinstance(semantic_ids, list)
        or semantic_ids != sorted(set(semantic_ids))
        or any(not isinstance(item, str) or "/entity." not in item for item in semantic_ids)
        or any(any(token in item for token in ("entity.keys", "entity.coffee_cup", "entity.resident", "door")) for item in semantic_ids)
        or not isinstance(dressing_ids, list)
        or dressing_ids != sorted(set(dressing_ids))
        or any(not isinstance(item, str) or not item.startswith("dress.") for item in dressing_ids)
    ):
        raise ForgeInputError("UE bundle external semantic/dressing identities are invalid")
    sources = value.get("asset_sources")
    if not isinstance(sources, list) or not sources:
        raise ForgeInputError("UE bundle external source digest inventory is empty")
    seen: set[str] = set()
    for source in sources:
        if not isinstance(source, Mapping) or set(source) != {
            "logical_asset_id", "asset_id", "asset_type", "resolution",
            "provider_files_hash", "source_tree_sha256", "files",
        }:
            raise ForgeInputError("UE bundle external source record is invalid")
        logical_id = source.get("logical_asset_id")
        files = source.get("files")
        if (
            not isinstance(logical_id, str)
            or logical_id in seen
            or not isinstance(source.get("source_tree_sha256"), str)
            or SHA256.fullmatch(source["source_tree_sha256"]) is None
            or source.get("asset_type") not in {"model", "texture"}
            or source.get("resolution") not in {"2k", "4k"}
            or not isinstance(source.get("provider_files_hash"), str)
            or re.fullmatch(r"[0-9a-f]{40}", source["provider_files_hash"]) is None
            or not isinstance(files, list)
            or not files
        ):
            raise ForgeInputError("UE bundle external source identity/digest is invalid")
        seen.add(logical_id)
        for file in files:
            if (
                not isinstance(file, Mapping)
                or set(file) != {
                    "relative_path", "size_bytes", "sha256",
                    "texture_semantics", "dimensions_px",
                }
                or not isinstance(file.get("relative_path"), str)
                or pathlib.PurePosixPath(file["relative_path"]).is_absolute()
                or ".." in pathlib.PurePosixPath(file["relative_path"]).parts
                or not isinstance(file.get("size_bytes"), int)
                or isinstance(file.get("size_bytes"), bool)
                or file["size_bytes"] <= 0
                or not isinstance(file.get("sha256"), str)
                or SHA256.fullmatch(file["sha256"]) is None
                or not isinstance(file.get("texture_semantics"), list)
                or file["texture_semantics"] != sorted(set(file["texture_semantics"]))
                or not (
                    file.get("dimensions_px") is None
                    or (
                        isinstance(file["dimensions_px"], list)
                        and len(file["dimensions_px"]) == 2
                        and all(isinstance(item, int) and item > 0 for item in file["dimensions_px"])
                    )
                )
            ):
                raise ForgeInputError("UE bundle external per-file SHA-256 is invalid")


def _validate_bundle_glb(record: Mapping[str, Any], inspection: Mapping[str, Any]) -> None:
    metadata = inspection.get("bundle_metadata", {})
    if not isinstance(metadata, Mapping):
        raise ForgeInputError("UE bundle GLB metadata is absent")
    try:
        embedded_transform = json.loads(str(metadata.get("vista_expected_world_transform_cm_json")))
        embedded_material_ids = json.loads(str(metadata.get("vista_material_ids_json")))
    except json.JSONDecodeError as exc:
        raise ForgeInputError("UE bundle GLB JSON extras are invalid") from exc
    if "external_content" in record:
        try:
            embedded_external = json.loads(str(metadata.get("vista_external_content_json")))
        except json.JSONDecodeError as exc:
            raise ForgeInputError("UE bundle GLB external JSON extras are invalid") from exc
        if embedded_external != record["external_content"]:
            raise ForgeInputError("UE bundle embedded external receipt differs from receipt")
        if metadata.get("vista_bundle_contract") != "one_room_one_mesh_v2":
            raise ForgeInputError("UE bundle external contract version is invalid")
    elif metadata.get("vista_bundle_contract") != "one_room_one_mesh_v1":
        raise ForgeInputError("UE bundle v1 contract version is invalid")
    expected_metadata = {
        "vista_artifact_id": record["artifact_id"],
        "vista_target_asset_id": record["target_asset_id"],
        "vista_room_id": record["room_id"],
        "vista_room_kind": record["room_kind"],
        "vista_root_transform_policy": record["root_transform_policy"],
        "vista_semantic_policy": record["semantic_policy"],
        "vista_collision_policy": record["collision_policy"],
        "vista_unreal_collision_profile": record["unreal_collision_profile"],
        "vista_source_house_sha256": record["source_hashes"]["house_sha256"],
        "vista_source_visual_profile_sha256": record["source_hashes"]["visual_profile_sha256"],
        "vista_source_forge_plan_sha256": record["source_hashes"]["forge_plan_sha256"],
    }
    if any(metadata.get(key) != value for key, value in expected_metadata.items()):
        raise ForgeInputError("UE bundle GLB metadata differs from its receipt")
    if embedded_transform != record["expected_world_transform_cm"]:
        raise ForgeInputError("UE bundle embedded world transform differs from receipt")
    if embedded_material_ids != record["material_ids"]:
        raise ForgeInputError("UE bundle embedded material IDs differ from receipt")
    if (
        inspection.get("sha256") != record["sha256"]
        or inspection.get("size_bytes") != record["size_bytes"]
        or inspection.get("mesh_count") != 1
        or inspection.get("mesh_node_count") != 1
        or inspection.get("bundle_node_count") != 1
        or inspection.get("bundle_root_is_identity") is not True
        or inspection.get("material_count") != record["material_count"]
        or (
            "external_content" in record
            and sorted(inspection.get("material_names", [])) != record["material_ids"]
        )
        or inspection.get("pbr_complete_material_count") != record["material_count"]
        or inspection.get("texture_count") != record["texture_count"]
        or inspection.get("camera_count") != 0
        or inspection.get("light_count") != 0
    ):
        raise ForgeInputError("UE bundle GLB structure differs from its closed receipt")


def _validate_external_manifest_binding(
    manifest: Mapping[str, Any], bundles: Sequence[Mapping[str, Any]]
) -> None:
    external_bundles = [item for item in bundles if "external_content" in item]
    if not external_bundles:
        if manifest.get("external_placement") is not None:
            raise ForgeInputError("v1 UE bundles cannot accompany an external placement plan")
        return
    if len(external_bundles) != len(bundles):
        raise ForgeInputError("v1 and v2 UE bundle contracts may not be mixed")
    if manifest.get("schema_version") != "simworld.vista.playable-home-realism-forge/v2":
        raise ForgeInputError("external UE bundles require forge schema v2")
    external = manifest.get("external_placement")
    required = {
        "schema_version", "placement_id", "normalization_policy",
        "acquisition_receipt", "placement_manifest_sha256",
        "semantic_target_ids", "dressing_ids", "asset_sources",
        "placements", "content_digest",
    }
    if not isinstance(external, Mapping) or set(external) != required:
        raise ForgeInputError("normalized manifest external placement fields are not closed")
    placements = external.get("placements")
    sources = external.get("asset_sources")
    if (
        not isinstance(placements, list)
        or not all(isinstance(item, Mapping) for item in placements)
        or not isinstance(sources, list)
        or not all(isinstance(item, Mapping) for item in sources)
    ):
        raise ForgeInputError("normalized manifest external placements/sources are invalid")
    union_semantic: set[str] = set()
    union_dressing: set[str] = set()
    for bundle in external_bundles:
        room_id = bundle["room_id"]
        room_placements = [
            item for item in placements if isinstance(item, Mapping) and item.get("room_id") == room_id
        ]
        source_ids = {
            logical_id
            for item in room_placements
            for logical_id in (
                ([item.get("source_logical_asset_id")] if item.get("source_logical_asset_id") else [])
                + (
                    item.get("material_logical_asset_ids", [])
                    if isinstance(item.get("material_logical_asset_ids", []), list)
                    else []
                )
            )
        }
        expected = {
            "schema_version": external["schema_version"],
            "normalization_policy": external["normalization_policy"],
            "acquisition_receipt": external["acquisition_receipt"],
            "placement_manifest_sha256": external["placement_manifest_sha256"],
            "placement_plan_sha256": external["content_digest"],
            "semantic_target_ids": sorted(
                item["semantic_target_id"]
                for item in room_placements
                if item.get("semantic_target_id")
            ),
            "dressing_ids": sorted(
                item["placement_id"]
                for item in room_placements
                if item.get("placement_kind") == "dressing"
            ),
            "asset_sources": [
                item for item in sources if item.get("logical_asset_id") in source_ids
            ],
        }
        if bundle["external_content"] != expected:
            raise ForgeInputError("UE bundle external content differs from normalized manifest")
        union_semantic.update(expected["semantic_target_ids"])
        union_dressing.update(expected["dressing_ids"])
    if (
        sorted(union_semantic) != external.get("semantic_target_ids")
        or sorted(union_dressing) != external.get("dressing_ids")
    ):
        raise ForgeInputError("UE bundle external identity coverage differs from normalized manifest")


_SUPPORTED_EXTERNAL_MATERIAL_SEMANTICS = frozenset(
    {"base_color", "normal", "roughness", "metalness", "opacity"}
)


def _validate_external_material_alpha_contract(
    manifest: Mapping[str, Any],
    record: Mapping[str, Any],
    inspection: Mapping[str, Any],
) -> None:
    """Bind a room placement and receipt semantics to exact GLB materials.

    This is intentionally independent of material counts.  The proof chain is
    placement source ID -> receipt source digest/texture semantics ->
    deterministic material name and exported material extras -> actual glTF
    ``alphaMode``/effective ``alphaCutoff``.
    """

    export_contract = manifest.get("export_contract")
    expected_policy = external_material_alpha_policy()
    if (
        not isinstance(export_contract, Mapping)
        or export_contract.get("custom_properties_exported_as_extras") is not True
        or export_contract.get("external_material_alpha_policy") != expected_policy
    ):
        raise ForgeInputError("external material alpha sanitization policy is absent or changed")
    external = manifest.get("external_placement")
    if not isinstance(external, Mapping):
        raise ForgeInputError("external material alpha proof lacks a placement plan")
    placements = external.get("placements")
    sources = external.get("asset_sources")
    if not isinstance(placements, list) or not isinstance(sources, list):
        raise ForgeInputError("external material alpha proof placement/source inventory is invalid")
    stove_target = "home.r1/room.kitchen_dining/entity.stove.01"
    stove_rows = [
        item
        for item in placements
        if isinstance(item, Mapping) and item.get("semantic_target_id") == stove_target
    ]
    if (
        len(stove_rows) != 1
        or stove_rows[0].get("room_id") != "home.r1/room.kitchen_dining"
        or stove_rows[0].get("category") != "stove"
        or stove_rows[0].get("realization_mode") != "external_blend"
        or stove_rows[0].get("source_logical_asset_id") != "visual.hero.kitchen_stove"
    ):
        raise ForgeInputError("external material alpha proof lost the truthful stove placement binding")

    room_id = record.get("room_id")
    room_source_ids = sorted(
        {
            item.get("source_logical_asset_id")
            for item in placements
            if isinstance(item, Mapping)
            and item.get("room_id") == room_id
            and item.get("realization_mode") == "external_blend"
            and isinstance(item.get("source_logical_asset_id"), str)
        }
    )
    if not room_source_ids:
        raise ForgeInputError("external material alpha proof room has no acquired model sources")
    source_by_id = {
        item.get("logical_asset_id"): item
        for item in sources
        if isinstance(item, Mapping) and item.get("logical_asset_id") in room_source_ids
    }
    if set(source_by_id) != set(room_source_ids):
        raise ForgeInputError("external material alpha proof sources differ from room placements")
    receipt_external = record.get("external_content")
    if not isinstance(receipt_external, Mapping):
        raise ForgeInputError("external material alpha proof lacks bundle source provenance")
    receipt_sources = receipt_external.get("asset_sources", [])
    receipt_by_id = {
        item.get("logical_asset_id"): item
        for item in receipt_sources
        if isinstance(item, Mapping) and item.get("logical_asset_id") in room_source_ids
    }
    if receipt_by_id != source_by_id:
        raise ForgeInputError("external material alpha proof sources differ from bundle receipt")

    prefixes: dict[str, str] = {}
    expected_semantics: dict[str, tuple[str, ...]] = {}
    for source_id in room_source_ids:
        source = source_by_id[source_id]
        if source.get("asset_type") != "model":
            raise ForgeInputError("external material alpha proof source is not a model")
        prefix = external_material_name_prefix(source_id)
        if prefix in prefixes.values():
            raise ForgeInputError("external material alpha source namespaces collide")
        prefixes[source_id] = prefix
        files = source.get("files")
        if not isinstance(files, list):
            raise ForgeInputError("external material alpha source files are invalid")
        semantics = sorted(
            {
                semantic
                for file in files
                if isinstance(file, Mapping)
                for semantic in file.get("texture_semantics", [])
                if semantic in _SUPPORTED_EXTERNAL_MATERIAL_SEMANTICS
            }
        )
        if not {"base_color", "normal", "roughness"}.issubset(semantics):
            raise ForgeInputError("external material alpha source lacks required PBR semantics")
        expected_semantics[source_id] = tuple(semantics)
    if room_id == "home.r1/room.kitchen_dining" and "opacity" not in expected_semantics.get(
        "visual.hero.kitchen_stove", ()
    ):
        raise ForgeInputError("external stove acquisition receipt lacks opacity semantics")

    material_records = inspection.get("external_material_alpha_contracts")
    if not isinstance(material_records, list) or len(material_records) != record.get("material_count"):
        raise ForgeInputError("external material alpha GLB inspection inventory is absent")
    material_ids = record.get("material_ids")
    if not isinstance(material_ids, list):
        raise ForgeInputError("external material alpha bundle material inventory is invalid")
    observed_semantics: dict[str, set[str]] = {source_id: set() for source_id in room_source_ids}
    observed_materials: dict[str, int] = {source_id: 0 for source_id in room_source_ids}
    for material in material_records:
        if not isinstance(material, Mapping):
            raise ForgeInputError("external material alpha GLB material record is invalid")
        name = material.get("name")
        if not isinstance(name, str):
            raise ForgeInputError("external material alpha GLB material name is invalid")
        matching_sources = [
            source_id for source_id, prefix in prefixes.items() if name.startswith(prefix)
        ]
        declared_source = material.get("source_logical_asset_id")
        if not matching_sources:
            if declared_source is not None:
                raise ForgeInputError("external material alpha extras claim an unbound room source")
            continue
        if len(matching_sources) != 1:
            raise ForgeInputError("external material alpha material namespace is ambiguous")
        source_id = matching_sources[0]
        prefix = prefixes[source_id]
        if re.fullmatch(re.escape(prefix) + r"[0-9]{2}\.[a-z0-9_]+", name) is None:
            raise ForgeInputError("external material alpha material name is not deterministic")
        if name not in material_ids or declared_source != source_id:
            raise ForgeInputError("external material alpha name and source extras differ")
        source = source_by_id[source_id]
        if material.get("source_tree_sha256") != source.get("source_tree_sha256"):
            raise ForgeInputError("external material alpha source digest differs from acquisition")
        raw_semantics = material.get("receipt_texture_semantics_json")
        try:
            semantics = json.loads(raw_semantics) if isinstance(raw_semantics, str) else None
        except json.JSONDecodeError as exc:
            raise ForgeInputError("external material alpha semantic extras are invalid JSON") from exc
        if (
            not isinstance(semantics, list)
            or semantics != sorted(set(semantics))
            or any(
                not isinstance(item, str)
                or item not in expected_semantics[source_id]
                for item in semantics
            )
            or not {"base_color", "normal", "roughness"}.issubset(semantics)
            or raw_semantics != json.dumps(semantics, separators=(",", ":"))
        ):
            raise ForgeInputError("external material alpha semantic extras differ from receipt")
        observed_semantics[source_id].update(semantics)
        observed_materials[source_id] += 1
        if material.get("sanitization_policy") != EXTERNAL_MATERIAL_ALPHA_SANITIZATION:
            raise ForgeInputError("external material alpha sanitization extras are absent or changed")
        gltf_mode = material.get("gltf_alpha_mode")
        declared_mode = material.get("declared_alpha_mode")
        if gltf_mode == "BLEND" or declared_mode == "BLEND":
            raise ForgeInputError("external material alpha BLEND is forbidden")
        if "opacity" in semantics:
            if (
                gltf_mode != "MASK"
                or declared_mode != "MASK"
                or material.get("gltf_alpha_cutoff") != EXTERNAL_MATERIAL_ALPHA_CUTOFF
                or material.get("declared_alpha_cutoff") != EXTERNAL_MATERIAL_ALPHA_CUTOFF
            ):
                raise ForgeInputError("external opacity material is not observed as MASK cutoff 0.5")
        elif (
            gltf_mode != "OPAQUE"
            or declared_mode != "OPAQUE"
            or material.get("gltf_alpha_cutoff") is not None
            or material.get("declared_alpha_cutoff") is not None
            or material.get("gltf_alpha_cutoff_explicit") is not False
        ):
            raise ForgeInputError("external non-opacity material is not observed as OPAQUE")
    for source_id in room_source_ids:
        if observed_materials[source_id] == 0:
            raise ForgeInputError("external material alpha source has no mapped GLB material")
        if observed_semantics[source_id] != set(expected_semantics[source_id]):
            raise ForgeInputError("external material alpha GLB semantics differ from source receipt")


def inspect_output(output_root: pathlib.Path) -> dict[str, Any]:
    output_root = output_root.resolve(strict=True)
    manifest_path = output_root / "normalized-manifest.json"
    artifact_path = output_root / "artifact-receipt.json"
    if not manifest_path.is_file() or not artifact_path.is_file():
        raise ForgeInputError("output root is missing normalized-manifest.json or artifact-receipt.json")
    manifest = load_json_object(manifest_path, label="normalized manifest")
    receipt = load_json_object(artifact_path, label="artifact receipt")
    is_external_manifest = (
        manifest.get("schema_version") == "simworld.vista.playable-home-realism-forge/v2"
    )
    components = manifest.get("components", [])
    if not isinstance(components, list) or len(components) < 60:
        raise ForgeInputError("normalized manifest has insufficient architectural components")
    required_roles = {"architecture_shell", "architectural_detail", "cabinetry"}
    if not required_roles.issubset(set(manifest.get("role_counts", {}))):
        raise ForgeInputError("normalized manifest is missing required export roles")
    receipt_artifacts = receipt.get("artifacts", [])
    if not isinstance(receipt_artifacts, list):
        raise ForgeInputError("artifact receipt artifacts must be an array")
    manifest_bundles = manifest.get("ue_import_bundles", [])
    receipt_artifact_bundles = [
        item
        for item in receipt_artifacts
        if isinstance(item, Mapping) and item.get("artifact_kind") == UE_BUNDLE_ARTIFACT_KIND
    ]
    receipt_bundles = receipt.get("ue_import_bundles", [])
    if not isinstance(manifest_bundles, list):
        raise ForgeInputError("normalized manifest ue_import_bundles must be an array")
    if not isinstance(receipt_bundles, list):
        raise ForgeInputError("artifact receipt ue_import_bundles must be an array")
    if manifest_bundles or receipt_bundles or receipt_artifact_bundles:
        expected_artifact_schema = (
            "simworld.vista.playable-home-realism-artifacts/v2"
            if any(isinstance(item, Mapping) and "external_content" in item for item in receipt_bundles)
            else "simworld.vista.playable-home-realism-artifacts/v1"
        )
        if receipt.get("schema_version") != expected_artifact_schema:
            raise ForgeInputError("artifact receipt schema does not support UE bundles")
        if (
            manifest_bundles != receipt_bundles
            or receipt_bundles != receipt_artifact_bundles
            or len(receipt_bundles) != 3
        ):
            raise ForgeInputError(
                "normalized manifest and artifact receipt UE bundle arrays differ"
            )
        validated = [_validate_bundle_record(item) for item in receipt_bundles]
        _validate_external_manifest_binding(manifest, validated)
        if {item["room_kind"] for item in validated} != {
            "entry_hall", "living_room", "kitchen_dining"
        }:
            raise ForgeInputError("UE bundle room coverage is incomplete")
        if len({item["artifact_id"] for item in validated}) != 3:
            raise ForgeInputError("UE bundle artifact identities are duplicated")
        source_hashes = {
            "house_sha256": manifest.get("source_house_digest"),
            "visual_profile_sha256": manifest.get("source_profile_digest"),
            "forge_plan_sha256": manifest.get("forge_plan_digest"),
        }
        manifest_rooms = manifest.get("rooms", [])
        if not isinstance(manifest_rooms, list):
            raise ForgeInputError("normalized manifest rooms must be an array")
        rooms_by_id = {
            item.get("room_id"): item
            for item in manifest_rooms
            if isinstance(item, Mapping) and isinstance(item.get("room_id"), str)
        }
        for item in validated:
            if item["source_hashes"] != source_hashes:
                raise ForgeInputError("UE bundle source hashes differ from normalized manifest")
            room = rooms_by_id.get(item["room_id"])
            if not isinstance(room, Mapping):
                raise ForgeInputError("UE bundle room is absent from normalized manifest")
            expected_transform = {
                "location_cm": [float(value) * 100.0 for value in room.get("location_m", [])],
                "rotation_deg": room.get("rotation_deg"),
                "scale": room.get("scale"),
            }
            if item["expected_world_transform_cm"] != expected_transform:
                raise ForgeInputError("UE bundle world transform differs from normalized room")
    glbs: list[dict[str, Any]] = []
    for artifact in receipt_artifacts:
        if not isinstance(artifact, Mapping):
            raise ForgeInputError("artifact receipt entries must be objects")
        if artifact.get("media_type") != "model/gltf-binary":
            continue
        path, relative_path = _safe_artifact_path(output_root, artifact.get("relative_path"))
        is_external_bundle = (
            artifact.get("artifact_kind") == UE_BUNDLE_ARTIFACT_KIND
            and "external_content" in artifact
        )
        inspection = inspect_glb(
            path,
            include_external_material_alpha=is_external_bundle,
        )
        if inspection["camera_count"] != 0:
            raise ForgeInputError(f"production GLB unexpectedly contains cameras: {path}")
        if inspection["light_count"] != 0:
            raise ForgeInputError(f"production GLB unexpectedly contains lights: {path}")
        if artifact.get("artifact_kind") == UE_BUNDLE_ARTIFACT_KIND:
            _validate_bundle_glb(artifact, inspection)
            if is_external_bundle:
                _validate_external_material_alpha_contract(manifest, artifact, inspection)
        elif inspection["component_extra_count"] == 0:
            raise ForgeInputError(f"production GLB lacks presentation role metadata: {path}")
        # ``inspect_glb`` remains useful as a standalone diagnostic and may
        # identify the caller-provided path.  Persistent build receipts must
        # never bind a host-private attempt root, so normalize at this boundary.
        inspection.pop("relative_or_absolute_path", None)
        inspection["relative_path"] = relative_path
        glbs.append(inspection)
    result = {
        "schema_version": "simworld.vista.playable-home-realism-inspection/v1",
        "forge_plan_digest": manifest.get("forge_plan_digest"),
        "build_quality": manifest.get("build_quality"),
        "component_count": len(components),
        "glbs": glbs,
    }
    if is_external_manifest:
        result["external_material_alpha_policy"] = external_material_alpha_policy()
    return result


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=pathlib.Path, required=True)
    parser.add_argument("--write", type=pathlib.Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    root = args.output_root.resolve(strict=True)
    result = inspect_output(root)
    payload = canonical_json_bytes(result)
    if args.write:
        args.write.write_bytes(payload)
    else:
        print(payload.decode("utf-8"), end="")


if __name__ == "__main__":
    main()
