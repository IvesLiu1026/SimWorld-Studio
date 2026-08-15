from __future__ import annotations

import csv
import hashlib
import json
import struct
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.blender.vista_playable_home_hssd import build as blender_build
from tools.blender.vista_playable_home_hssd.glb_transport import (
    read_glb,
    rehydrate_basisu_materials,
    validate_preserved_basisu_glb,
    write_blender_surrogate,
    write_glb,
)
from tools.blender.vista_playable_home_hssd.planner import (
    BINDING_PLAN_SCHEMA,
    BUILT_MANIFEST_SCHEMA,
    HSSD_LICENSE_SPDX,
    HssdBindingError,
    _candidate_files,
    build_binding_plan,
    derive_target_assets,
    seal_document,
    validate_built_manifest,
    validate_target_dimensions,
)


def _write_glb(path: Path, *, mesh_count: int = 1, triangles: int = 100, pbr: bool = True) -> None:
    meshes = []
    accessors = []
    for index in range(mesh_count):
        accessors.append({"count": triangles * 3, "componentType": 5123, "type": "SCALAR"})
        meshes.append({"primitives": [{"attributes": {}, "indices": index, "material": 0}]})
    document = {
        "asset": {"version": "2.0"},
        "meshes": meshes,
        "accessors": accessors,
        "materials": [{"pbrMetallicRoughness": {"baseColorTexture": {"index": 0}}}] if pbr else [],
        "textures": [{"source": 0}] if pbr else [],
        "images": [{"uri": "data:image/png;base64,AA=="}] if pbr else [],
    }
    payload = json.dumps(document, separators=(",", ":")).encode("utf-8")
    payload += b" " * ((4 - len(payload) % 4) % 4)
    total = 12 + 8 + len(payload)
    path.write_bytes(struct.pack("<4sII", b"glTF", 2, total) + struct.pack("<II", len(payload), 0x4E4F534A) + payload)


def _primitive(dimensions: tuple[float, float, float], *, rotation_z: float = 0.0) -> dict:
    return {
        "primitive_id": "fixture",
        "kind": "box",
        "material_id": "fixture",
        "location_m": [0, 0, dimensions[2] / 2],
        "dimensions_m": list(dimensions),
        "rotation_deg": [0, 0, rotation_z],
        "radius_m": None,
        "major_radius_m": None,
        "minor_radius_m": None,
        "grid_bounds_m": None,
    }


def _entity(
    asset_id: str,
    category: str,
    dimensions: tuple[float, float, float],
    *,
    entity_id: str | None = None,
    rotation_z: float = 0.0,
) -> dict:
    return {
        "entity_id": entity_id or f"home.r1/entity.{category}.01",
        "category": category,
        "asset_ref": asset_id,
        "component_role": "furniture" if category not in {"interior_door", "resident"} else category,
        "transform": {"location_m": [0, 0, 0], "rotation_deg": [0, 0, 0], "scale": [1, 1, 1]},
        "geometry": {
            "assembly_policy": "single_semantic_mesh" if category != "resident" else "runtime_actor",
            "primitive_count": 1 if category != "resident" else 0,
            "primitives": [_primitive(dimensions, rotation_z=rotation_z)] if category != "resident" else [],
            "instance_group": None,
        },
    }


def _normalized_manifest(entities: list[dict], *, room_bundles: list[dict] | None = None) -> dict:
    return seal_document({
        "schema_version": "simworld.vista.playable-home-blender-manifest/v1",
        "house_id": "home.r1",
        "revision": "vista_playable_home_r1",
        "units": "meters",
        "entities": entities,
        "room_bundles": room_bundles or [],
    })


def _dataset(tmp_path: Path, candidates: list[tuple[str, str, tuple[float, float, float]]]) -> tuple[Path, str]:
    root = tmp_path / "hssd-hab"
    (root / "metadata").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("1" * 40 + "\n", encoding="utf-8")
    readme = "---\nlicense: cc-by-nc-4.0\n---\nHSSD fixture under CC BY-NC 4.0.\n"
    (root / "README.md").write_text(readme, encoding="utf-8")
    readme_hash = hashlib.sha256(readme.encode("utf-8")).hexdigest()
    with (root / "metadata" / "hssd_obj_semantics_condensed.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["Object Hash", "Semantic Category: CONDENSED"])
        writer.writeheader()
        for object_id, category, _dimensions in candidates:
            writer.writerow({"Object Hash": object_id, "Semantic Category: CONDENSED": category})
    with (root / "metadata" / "fpmodels-with-decomposed.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["id", "name", "aligned.dims"])
        writer.writeheader()
        for object_id, category, dimensions in candidates:
            writer.writerow({"id": object_id, "name": f"fixture {category}", "aligned.dims": ",".join(map(str, dimensions))})
            object_dir = root / "objects" / object_id[0]
            object_dir.mkdir(parents=True, exist_ok=True)
            (object_dir / f"{object_id}.object_config.json").write_text(
                json.dumps({"up": [0, 1, 0], "front": [0, 0, -1], "render_asset": f"{object_id}.glb"}),
                encoding="utf-8",
            )
            _write_glb(object_dir / f"{object_id}.glb")
    return root.resolve(), readme_hash


def test_selection_is_deterministic_and_independent_of_csv_order(tmp_path: Path) -> None:
    close_id = "a" * 40
    far_id = "b" * 40
    candidates = [
        (far_id, "couch", (3.8, 1.0, 0.6)),
        (close_id, "couch", (2.25, 1.06, 0.84)),
    ]
    root, readme_hash = _dataset(tmp_path, candidates)
    manifest = _normalized_manifest([_entity("asset.prop.sofa", "sofa", (0.84, 2.25, 1.06))])
    first = build_binding_plan(manifest, root, expected_readme_sha256=readme_hash)

    semantics = root / "metadata" / "hssd_obj_semantics_condensed.csv"
    rows = semantics.read_text(encoding="utf-8").splitlines()
    semantics.write_text("\n".join([rows[0], *reversed(rows[1:])]) + "\n", encoding="utf-8")
    second = build_binding_plan(manifest, root, expected_readme_sha256=readme_hash)

    assert first == second
    assert first["content_digest"] == second["content_digest"]
    assert first["bindings"][0]["source"]["object_id"] == close_id


def test_dataset_paths_are_contained_and_symlinks_fail_closed(tmp_path: Path) -> None:
    object_id = "a" * 40
    root, _readme_hash = _dataset(tmp_path, [(object_id, "couch", (2.25, 1.06, 0.84))])
    config = root / "objects" / "a" / f"{object_id}.object_config.json"
    config.write_text(json.dumps({"up": [0, 1, 0], "front": [0, 0, -1], "render_asset": "../../outside.glb"}), encoding="utf-8")
    with pytest.raises(HssdBindingError, match="closed expected basename"):
        _candidate_files(root, object_id)

    config.write_text(json.dumps({"up": [0, 1, 0], "front": [0, 0, -1], "render_asset": f"{object_id}.glb"}), encoding="utf-8")
    source = root / "objects" / "a" / f"{object_id}.glb"
    source.unlink()
    outside = tmp_path / "outside.glb"
    _write_glb(outside)
    source.symlink_to(outside)
    with pytest.raises(HssdBindingError, match="non-symlink"):
        _candidate_files(root, object_id)


def test_license_receipt_and_closed_preservation_are_explicit(tmp_path: Path) -> None:
    object_id = "a" * 40
    root, readme_hash = _dataset(tmp_path, [(object_id, "couch", (2.25, 1.06, 0.84))])
    manifest = _normalized_manifest(
        [
            _entity("asset.prop.sofa", "sofa", (0.84, 2.25, 1.06)),
            _entity("asset.door.interior", "interior_door", (1.0, 0.1, 2.1)),
        ],
        room_bundles=[{"asset_ref": "asset.bundle.living", "category": "room_shell"}],
    )
    plan = build_binding_plan(manifest, root, expected_readme_sha256=readme_hash)
    assert plan["dataset"]["license"]["spdx"] == HSSD_LICENSE_SPDX
    assert plan["license_receipt"]["scope"] == "research_and_noncommercial_demo_only"
    assert plan["bindings"][0]["source"]["license_spdx"] == HSSD_LICENSE_SPDX
    assert plan["bindings"][0]["source"]["render_asset_sha256"]
    assert set(plan["closed_world"]["preserved_asset_ids"]) == {"asset.bundle.living", "asset.door.interior"}
    assert plan["closed_world"]["unaccounted_asset_ids"] == []


def test_target_bounds_include_rotation_and_normalization_is_bounded() -> None:
    manifest = _normalized_manifest([_entity("asset.prop.table", "table", (2.0, 1.0, 0.8), rotation_z=90)])
    targets, _preserved = derive_target_assets(manifest)
    target = targets["asset.prop.table"]
    assert target.target_dimensions_m == pytest.approx((1.0, 2.0, 0.8))
    assert target.target_bounds_m[0] == pytest.approx((-0.5, -1.0, 0.0))
    assert target.target_bounds_m[1] == pytest.approx((0.5, 1.0, 0.8))
    validate_target_dimensions(target.target_dimensions_m)
    with pytest.raises(HssdBindingError, match="outside"):
        validate_target_dimensions((0.0, 1.0, 1.0))
    with pytest.raises(HssdBindingError, match="outside"):
        validate_target_dimensions((6.0, 1.0, 1.0))


def test_missing_semantic_category_fails_closed(tmp_path: Path) -> None:
    root, readme_hash = _dataset(tmp_path, [("a" * 40, "bed", (2.0, 1.0, 1.8))])
    manifest = _normalized_manifest([_entity("asset.prop.sofa", "sofa", (0.84, 2.25, 1.06))])
    with pytest.raises(HssdBindingError, match="no licensed high-detail PBR HSSD candidate for category sofa"):
        build_binding_plan(manifest, root, expected_readme_sha256=readme_hash)


def test_unknown_category_is_never_a_silent_procedural_fallback() -> None:
    manifest = _normalized_manifest([_entity("asset.prop.mystery", "mystery", (1.0, 1.0, 1.0))])
    with pytest.raises(HssdBindingError, match="neither HSSD-bound nor explicitly preserved"):
        derive_target_assets(manifest)


def _built_manifest(mesh_count: int) -> dict:
    return seal_document({
        "schema_version": BUILT_MANIFEST_SCHEMA,
        "source_plan": {"schema_version": BINDING_PLAN_SCHEMA, "content_digest": "1" * 64},
        "license_receipt": {"accepted_spdx": HSSD_LICENSE_SPDX},
        "closed_world": {"bound_asset_ids": ["asset.prop.sofa"], "unaccounted_asset_ids": []},
        "outputs": [{
            "logical_asset_id": "asset.prop.sofa",
            "target_dimensions_m": [0.84, 2.25, 1.06],
            "actual_dimensions_m": [0.84, 2.25, 1.06],
            "sha256": "2" * 64,
            "inspection": {
                "mesh_count": mesh_count,
                "material_count": 1,
                "pbr_texture_slot_count": 1,
                "base_normal_orm_texture_slot_count": 1,
                "all_primitives_material_bound": 1,
            },
        }],
    })


def test_built_manifest_enforces_one_primary_mesh_and_pbr_slots() -> None:
    validate_built_manifest(_built_manifest(mesh_count=1))
    with pytest.raises(HssdBindingError, match="one-primary-mesh"):
        validate_built_manifest(_built_manifest(mesh_count=2))
    no_pbr = _built_manifest(mesh_count=1)
    no_pbr["outputs"][0]["inspection"]["pbr_texture_slot_count"] = 0
    no_pbr = seal_document(no_pbr)
    with pytest.raises(HssdBindingError, match="lost PBR"):
        validate_built_manifest(no_pbr)


def test_build_module_is_importable_without_blender_and_cli_is_pinned() -> None:
    args = blender_build.parse_blender_args([
        "--normalized-manifest", "/tmp/normalized.json",
        "--hssd-root", "/tmp/hssd",
        "--output-root", "/tmp/output",
        "--license-accept", HSSD_LICENSE_SPDX,
        "--asset-id", "asset.prop.sofa",
    ])
    assert args.license_accept == HSSD_LICENSE_SPDX
    assert args.asset_ids == ["asset.prop.sofa"]
    source = Path(blender_build.__file__).read_text(encoding="utf-8")
    assert "EXPECTED_BLENDER_VERSION = (4, 5, 8)" in source
    assert "one_logical_asset_one_primary_mesh" in source
    assert "export_scene.gltf" in source


def test_basisu_surrogate_rehydrates_exact_pbr_payload(tmp_path: Path) -> None:
    image_payload = b"KTX2DATA"
    source_document = {
        "asset": {"version": "2.0"},
        "buffers": [{"byteLength": len(image_payload)}],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(image_payload)}],
        "images": [{"mimeType": "image/ktx2", "bufferView": 0}],
        "samplers": [{"magFilter": 9729, "minFilter": 9987}],
        "textures": [{"sampler": 0, "extensions": {"KHR_texture_basisu": {"source": 0}}}],
        "materials": [{"name": "Fabric", "pbrMetallicRoughness": {"baseColorTexture": {"index": 0}}}],
        "accessors": [{"count": 300, "componentType": 5123, "type": "SCALAR"}],
        "meshes": [{"primitives": [{"attributes": {}, "indices": 0, "material": 0}]}],
        "extensionsUsed": ["KHR_texture_basisu"],
        "extensionsRequired": ["KHR_texture_basisu"],
    }
    source = tmp_path / "source.glb"
    write_glb(source, source_document, image_payload)
    surrogate = tmp_path / "surrogate.glb"
    write_blender_surrogate(source, surrogate)
    surrogate_document, _ = read_glb(surrogate)
    assert "textures" not in surrogate_document
    assert "images" not in surrogate_document
    assert surrogate_document["materials"][0]["name"].startswith("VISTA_HSSD_MAT_0000__")
    assert "baseColorTexture" not in surrogate_document["materials"][0]["pbrMetallicRoughness"]

    normalized_document = {
        "asset": {"version": "2.0"},
        "buffers": [{"byteLength": 4}],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": 4}],
        "materials": [{"name": "VISTA_HSSD_MAT_0000__Fabric", "pbrMetallicRoughness": {}}],
        "accessors": [{"count": 300, "componentType": 5123, "type": "SCALAR"}],
        "meshes": [{"primitives": [{"attributes": {}, "indices": 0, "material": 0}]}],
    }
    normalized = tmp_path / "normalized.glb"
    write_glb(normalized, normalized_document, b"GEOM")
    output = tmp_path / "output.glb"
    receipt = rehydrate_basisu_materials(source, normalized, output)
    output_document, output_binary = read_glb(output)
    assert receipt["mode"] == "KHR_texture_basisu_preserved"
    assert receipt["blender_decoded_textures"] is False
    assert output_document["materials"] == source_document["materials"]
    assert output_document["textures"] == source_document["textures"]
    assert output_document["meshes"][0]["primitives"][0]["material"] == 0
    image_view = output_document["bufferViews"][output_document["images"][0]["bufferView"]]
    start = image_view["byteOffset"]
    assert output_binary[start : start + image_view["byteLength"]] == image_payload
    validation = validate_preserved_basisu_glb(source, output)
    assert validation["self_contained"] is True
    assert validation["image_payloads"][0]["source_sha256"] == hashlib.sha256(image_payload).hexdigest()
    assert validation["image_payloads"][0]["output_sha256"] == hashlib.sha256(image_payload).hexdigest()
    assert validation["image_payloads"][0]["match"] is True


@pytest.mark.parametrize(
    "corruption",
    ["material", "basisu_source", "external_image", "external_buffer", "missing_extension", "unaligned_buffer_view"],
)
def test_basisu_validator_rejects_dangling_or_external_records(tmp_path: Path, corruption: str) -> None:
    image_payload = b"KTX2DATA"
    source_document = {
        "asset": {"version": "2.0"},
        "buffers": [{"byteLength": len(image_payload)}],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(image_payload)}],
        "images": [{"mimeType": "image/ktx2", "bufferView": 0}],
        "samplers": [{}],
        "textures": [{"sampler": 0, "extensions": {"KHR_texture_basisu": {"source": 0}}}],
        "materials": [{"name": "Fabric", "pbrMetallicRoughness": {"baseColorTexture": {"index": 0}}}],
        "accessors": [{"count": 300, "componentType": 5123, "type": "SCALAR"}],
        "meshes": [{"primitives": [{"attributes": {}, "indices": 0, "material": 0}]}],
        "extensionsUsed": ["KHR_texture_basisu"],
        "extensionsRequired": ["KHR_texture_basisu"],
    }
    source = tmp_path / "source.glb"
    write_glb(source, source_document, image_payload)
    output_document = json.loads(json.dumps(source_document))
    if corruption == "material":
        output_document["meshes"][0]["primitives"][0]["material"] = 99
    elif corruption == "basisu_source":
        output_document["textures"][0]["extensions"]["KHR_texture_basisu"]["source"] = 99
    elif corruption == "external_image":
        output_document["images"][0] = {"mimeType": "image/ktx2", "uri": "outside.ktx2"}
    elif corruption == "external_buffer":
        output_document["buffers"][0]["uri"] = "outside.bin"
    elif corruption == "unaligned_buffer_view":
        output_document["bufferViews"][0]["byteOffset"] = 1
    else:
        output_document["extensionsRequired"] = []
    output = tmp_path / "corrupt.glb"
    write_glb(output, output_document, image_payload)
    with pytest.raises(HssdBindingError):
        validate_preserved_basisu_glb(source, output)
