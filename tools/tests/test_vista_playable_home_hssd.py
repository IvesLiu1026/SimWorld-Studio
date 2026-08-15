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
from tools.blender.vista_playable_home_hssd import glb_transport
from tools.blender.vista_playable_home_hssd.glb_transport import (
    encode_rgba8_png,
    read_glb,
    rehydrate_core_png_materials,
    validate_core_png_glb,
    write_blender_surrogate,
    write_glb,
)
from tools.blender.vista_playable_home_hssd.planner import (
    BINDING_PLAN_SCHEMA,
    BUILT_MANIFEST_SCHEMA,
    HSSD_LICENSE_SPDX,
    HssdBindingError,
    _candidate_files,
    _fit_transform,
    build_binding_plan,
    derive_target_assets,
    inspect_glb_geometry,
    seal_document,
    validate_binding_plan,
    validate_built_manifest,
    validate_target_dimensions,
)


def _write_glb(
    path: Path,
    *,
    mesh_count: int = 1,
    triangles: int = 100,
    pbr: bool = True,
    gltf_dimensions: tuple[float, float, float] = (1.0, 1.0, 1.0),
) -> None:
    meshes = []
    accessors = []
    for index in range(mesh_count):
        accessors.append({"count": triangles * 3, "componentType": 5123, "type": "SCALAR"})
    position_accessor = len(accessors)
    minimum = [-value / 2 for value in gltf_dimensions]
    maximum = [value / 2 for value in gltf_dimensions]
    accessors.append({
        "bufferView": 0,
        "componentType": 5126,
        "count": 2,
        "type": "VEC3",
        "min": minimum,
        "max": maximum,
    })
    for index in range(mesh_count):
        meshes.append({"primitives": [{"attributes": {"POSITION": position_accessor}, "indices": index, "material": 0}]})
    binary = struct.pack("<ffffff", *minimum, *maximum)
    document = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": list(range(mesh_count))}],
        "nodes": [{"mesh": index} for index in range(mesh_count)],
        "meshes": meshes,
        "accessors": accessors,
        "buffers": [{"byteLength": len(binary)}],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(binary)}],
        "materials": [{"pbrMetallicRoughness": {"baseColorTexture": {"index": 0}}}] if pbr else [],
        "textures": [{"source": 0}] if pbr else [],
        "images": [{"uri": "data:image/png;base64,AA=="}] if pbr else [],
    }
    payload = json.dumps(document, separators=(",", ":")).encode("utf-8")
    payload += b" " * ((4 - len(payload) % 4) % 4)
    total = 12 + 8 + len(payload) + 8 + len(binary)
    path.write_bytes(
        struct.pack("<4sII", b"glTF", 2, total)
        + struct.pack("<II", len(payload), 0x4E4F534A)
        + payload
        + struct.pack("<II", len(binary), 0x004E4942)
        + binary
    )


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


def _dataset(tmp_path: Path, candidates: list[tuple]) -> tuple[Path, str]:
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
        for object_id, category, *_dimensions in candidates:
            writer.writerow({"Object Hash": object_id, "Semantic Category: CONDENSED": category})
    with (root / "metadata" / "fpmodels-with-decomposed.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["id", "name", "aligned.dims"])
        writer.writeheader()
        for candidate in candidates:
            object_id, category, dimensions = candidate[:3]
            gltf_dimensions = candidate[3] if len(candidate) == 4 else dimensions
            writer.writerow({"id": object_id, "name": f"fixture {category}", "aligned.dims": ",".join(map(str, dimensions))})
            object_dir = root / "objects" / object_id[0]
            object_dir.mkdir(parents=True, exist_ok=True)
            (object_dir / f"{object_id}.object_config.json").write_text(
                json.dumps({"up": [0, 1, 0], "front": [0, 0, -1], "render_asset": f"{object_id}.glb"}),
                encoding="utf-8",
            )
            _write_glb(object_dir / f"{object_id}.glb", gltf_dimensions=gltf_dimensions)
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


def test_ladder_catalog_geometry_mismatch_is_rejected_then_falls_back_deterministically(tmp_path: Path) -> None:
    mismatched_id = "a" * 40
    fallback_id = "b" * 40
    candidates = [
        # Mirrors the failed canonical Rope Towel Ladder: catalog dimensions
        # predict a standing object, while decoded GLB POSITION data imports
        # into Blender as a 6 cm-high object.
        (mismatched_id, "ladder", (0.48, 1.491183, 0.06), (0.48, 0.06, 1.491183)),
        (fallback_id, "ladder", (0.50, 1.50, 0.08), (0.50, 1.50, 0.08)),
    ]
    root, readme_hash = _dataset(tmp_path, candidates)
    target_dimensions = (0.893748, 0.14, 1.891251)
    manifest = _normalized_manifest([_entity("asset.prop.ladder", "ladder", target_dimensions)])

    first = build_binding_plan(manifest, root, expected_readme_sha256=readme_hash)
    semantics = root / "metadata" / "hssd_obj_semantics_condensed.csv"
    models = root / "metadata" / "fpmodels-with-decomposed.csv"
    for path in (semantics, models):
        rows = path.read_text(encoding="utf-8").splitlines()
        path.write_text("\n".join([rows[0], *reversed(rows[1:])]) + "\n", encoding="utf-8")
    second = build_binding_plan(manifest, root, expected_readme_sha256=readme_hash)

    assert first == second
    binding = first["bindings"][0]
    assert binding["source"]["object_id"] == fallback_id
    assert binding["source"]["catalog_aligned_dimensions_m"] == [0.5, 1.5, 0.08]
    assert binding["source"]["source_dimensions_blender_m"] == pytest.approx([0.5, 0.08, 1.5])
    assert binding["normalization_plan"]["dimension_source"] == "decoded_glb_position_accessors_active_scene_world_aabb"
    assert binding["normalization_plan"]["scale_anisotropy"] <= 2.75
    receipt = binding["selection_receipt"]
    assert receipt["catalog_dimensions_used_for_selection"] is False
    assert receipt["matching_candidate_count"] == 2
    assert receipt["eligible_candidate_count"] == 1
    assert receipt["rejection_counts"] == {"actual_geometry_anisotropy_exceeded": 1}
    assert len(receipt["candidate_decision_digest"]) == 64
    validate_binding_plan(first)

    mismatch_geometry = inspect_glb_geometry(
        root / "objects" / "a" / f"{mismatched_id}.glb"
    )
    assert mismatch_geometry["blender_dimensions_m"] == pytest.approx([0.48, 1.491183, 0.06])
    _rotation, _scales, mismatch_anisotropy, _uniform = _fit_transform(
        mismatch_geometry["blender_dimensions_m"], target_dimensions
    )
    assert mismatch_anisotropy == pytest.approx(108.071486, abs=1e-6)

    tampered = json.loads(json.dumps(first))
    tampered["bindings"][0]["selection_receipt"]["catalog_dimensions_used_for_selection"] = True
    tampered = seal_document(tampered)
    with pytest.raises(HssdBindingError, match="selection receipt"):
        validate_binding_plan(tampered)


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
        "builder_source": {
            "repository_commit": "3" * 40,
            "worktree_clean": True,
            "source_files": [{"path": "tools/blender/builder.py", "sha256": "4" * 64}],
        },
        "normalization_policy": {"maximum_axis_scale_anisotropy": 2.75},
        "closed_world": {"bound_asset_ids": ["asset.prop.sofa"], "unaccounted_asset_ids": []},
        "outputs": [{
            "logical_asset_id": "asset.prop.sofa",
            "target_dimensions_m": [0.84, 2.25, 1.06],
            "actual_dimensions_m": [0.84, 2.25, 1.06],
            "sha256": "2" * 64,
            "texture_transport": "blender_native_texture_import",
            "normalization": {
                "rotation_mode": "XYZ",
                "actual_scale_anisotropy": 1.25,
                "maximum_axis_scale_anisotropy": 2.75,
                "anisotropy_accepted": True,
            },
            "inspection": {
                "mesh_count": mesh_count,
                "material_count": 1,
                "pbr_texture_slot_count": 1,
                "base_normal_orm_texture_slot_count": 1,
                "all_primitives_material_bound": 1,
                "basisu_required": 0,
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
    bad_anisotropy = _built_manifest(mesh_count=1)
    bad_anisotropy["outputs"][0]["normalization"]["actual_scale_anisotropy"] = 2.76
    bad_anisotropy = seal_document(bad_anisotropy)
    with pytest.raises(HssdBindingError, match="anisotropy"):
        validate_built_manifest(bad_anisotropy)


def test_build_module_is_importable_without_blender_and_cli_is_pinned() -> None:
    args = blender_build.parse_blender_args([
        "--normalized-manifest", "/tmp/normalized.json",
        "--hssd-root", "/tmp/hssd",
        "--output-root", "/tmp/output",
        "--license-accept", HSSD_LICENSE_SPDX,
        "--asset-id", "asset.prop.sofa",
        "--node", "/opt/node/bin/node",
        "--basis-transcoder-js", "/opt/basis/basis_transcoder.js",
        "--basis-transcoder-wasm", "/opt/basis/basis_transcoder.wasm",
    ])
    assert args.license_accept == HSSD_LICENSE_SPDX
    assert args.asset_ids == ["asset.prop.sofa"]
    source = Path(blender_build.__file__).read_text(encoding="utf-8")
    assert "EXPECTED_BLENDER_VERSION = (4, 5, 8)" in source
    assert "one_logical_asset_one_primary_mesh" in source
    assert "export_scene.gltf" in source
    assert 'primary.rotation_mode = "XYZ"' in source
    assert "actual_scale_anisotropy" in source


def _decoder_receipt() -> dict:
    pin = {"path": "/pinned/file", "sha256": "a" * 64}
    return {
        "distribution": "three",
        "distribution_version": "0.185.1",
        "basis_universal_license": "Apache-2.0",
        "three_license": "MIT",
        "provenance": "three/examples/jsm/libs/basis",
        "node": dict(pin),
        "transcoder_js": dict(pin),
        "transcoder_wasm": dict(pin),
        "decode_wrapper": dict(pin),
    }


def test_basisu_surrogate_transcodes_to_self_contained_core_png(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
    decoder = _decoder_receipt()
    monkeypatch.setattr(glb_transport, "_decoder_identity", lambda *_args: decoder)

    def fake_decode(payload: bytes, *_args: object) -> tuple[bytes, dict]:
        png = encode_rgba8_png(2, 1, bytes([255, 0, 0, 255, 0, 255, 0, 255]))
        return png, {
            "schema_version": "simworld.basisu-rgba8-decode/v1",
            "width": 2,
            "height": 1,
            "source_levels": 1,
            "source_layers": 1,
            "source_faces": 1,
            "source_encoding": "ETC1S",
            "has_alpha": True,
            "output_format": "RGBA8",
            "output_bytes": 8,
            "mip_policy": "base_level_only",
            "node_version": "v22.22.2",
            "source_mime_type": "image/ktx2",
            "source_ktx2_bytes": len(payload),
            "source_ktx2_sha256": hashlib.sha256(payload).hexdigest(),
            "output_mime_type": "image/png",
            "output_png_bytes": len(png),
            "output_png_sha256": hashlib.sha256(png).hexdigest(),
            "png_encoder": {"policy": "test"},
            "decoder": decoder,
        }

    monkeypatch.setattr(glb_transport, "_decode_ktx2_to_png", fake_decode)
    receipt = rehydrate_core_png_materials(
        source,
        normalized,
        output,
        node_path=tmp_path / "node",
        transcoder_js_path=tmp_path / "basis.js",
        transcoder_wasm_path=tmp_path / "basis.wasm",
    )
    output_document, output_binary = read_glb(output)
    assert receipt["mode"] == "KHR_texture_basisu_to_core_png"
    assert receipt["blender_decoded_textures"] is False
    assert output_document["materials"] == source_document["materials"]
    assert output_document["textures"] == [{"sampler": 0, "source": 0}]
    assert "KHR_texture_basisu" not in output_document.get("extensionsRequired", [])
    assert output_document["meshes"][0]["primitives"][0]["material"] == 0
    image_view = output_document["bufferViews"][output_document["images"][0]["bufferView"]]
    start = image_view["byteOffset"]
    png_payload = output_binary[start : start + image_view["byteLength"]]
    assert png_payload.startswith(b"\x89PNG\r\n\x1a\n")
    validation = validate_core_png_glb(source, output)
    assert validation["self_contained"] is True
    assert validation["core_texture_sources_valid"] is True
    assert validation["image_payloads"][0]["source_ktx2_sha256"] == hashlib.sha256(image_payload).hexdigest()
    assert validation["image_payloads"][0]["output_png_sha256"] == hashlib.sha256(png_payload).hexdigest()


@pytest.mark.parametrize(
    "corruption",
    ["material", "core_source", "external_image", "external_buffer", "required_basisu", "unaligned_buffer_view", "wrong_mime"],
)
def test_core_png_validator_rejects_dangling_external_or_basisu_records(tmp_path: Path, corruption: str) -> None:
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
    png_payload = encode_rgba8_png(1, 1, b"\x10\x20\x30\xff")
    output_document = json.loads(json.dumps(source_document))
    output_document["buffers"] = [{"byteLength": len(png_payload)}]
    output_document["bufferViews"] = [{"buffer": 0, "byteOffset": 0, "byteLength": len(png_payload)}]
    output_document["images"] = [{"mimeType": "image/png", "bufferView": 0}]
    output_document["textures"] = [{"sampler": 0, "source": 0}]
    output_document.pop("extensionsUsed")
    output_document.pop("extensionsRequired")
    if corruption == "material":
        output_document["meshes"][0]["primitives"][0]["material"] = 99
    elif corruption == "core_source":
        output_document["textures"][0]["source"] = 99
    elif corruption == "external_image":
        output_document["images"][0] = {"mimeType": "image/png", "uri": "outside.png"}
    elif corruption == "external_buffer":
        output_document["buffers"][0]["uri"] = "outside.bin"
    elif corruption == "unaligned_buffer_view":
        output_document["bufferViews"][0]["byteOffset"] = 1
    elif corruption == "wrong_mime":
        output_document["images"][0]["mimeType"] = "image/jpeg"
    else:
        output_document["textures"][0]["extensions"] = {"KHR_texture_basisu": {"source": 0}}
        output_document["extensionsUsed"] = ["KHR_texture_basisu"]
        output_document["extensionsRequired"] = ["KHR_texture_basisu"]
    output = tmp_path / "corrupt.glb"
    write_glb(output, output_document, png_payload)
    with pytest.raises(HssdBindingError):
        validate_core_png_glb(source, output)


def test_rgba_png_encoder_is_deterministic_and_strict() -> None:
    rgba = bytes(range(16))
    first = encode_rgba8_png(2, 2, rgba)
    second = encode_rgba8_png(2, 2, rgba)
    assert first == second
    assert hashlib.sha256(first).hexdigest() == hashlib.sha256(second).hexdigest()
    assert glb_transport._validate_rgba8_png(first, "fixture") == (2, 2)
    with pytest.raises(HssdBindingError, match="byte count"):
        encode_rgba8_png(2, 2, rgba[:-1])


def test_unpinned_basis_transcoder_is_rejected_before_execution(tmp_path: Path) -> None:
    node = Path(sys.executable).resolve()
    javascript = tmp_path / "basis.js"
    wasm = tmp_path / "basis.wasm"
    javascript.write_text("module.exports = {};", encoding="utf-8")
    wasm.write_bytes(b"not wasm")
    with pytest.raises(HssdBindingError, match="not an approved offline pin"):
        glb_transport._decoder_identity(node, javascript.resolve(), wasm.resolve())
