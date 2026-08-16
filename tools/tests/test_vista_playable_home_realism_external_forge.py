from __future__ import annotations

import copy
import hashlib
import json
import os
import struct
from collections import Counter
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest


from tools.blender.vista_playable_home_realism import build as forge_build
from tools.blender.vista_playable_home_realism.architecture import (
    build_external_forge_plan,
    build_forge_plan,
)
from tools.blender.vista_playable_home_realism.config import (
    ForgeInputError,
    canonical_json_bytes,
    content_digest,
)
from tools.blender.vista_playable_home_realism.export import (
    normalized_manifest,
    ue_bundle_contract,
)
from tools.blender.vista_playable_home_realism.external_assets import (
    ACQUISITION_RECEIPT_SCHEMA,
    AUTHORED_RECIPE_MATERIAL_IDS,
    AcquiredAsset,
    AcquiredFile,
    ExternalAssetSet,
    EXTERNAL_TEXTURE_MATERIAL_SOURCE_PROPERTY,
    _apply_metric_box_uv,
    _bounds_record_from_minimum_maximum,
    _canonical_acquisition_json,
    _combined_bounds,
    _load_fresh_receipt_image,
    _load_verified_blend_objects,
    _metric_box_uv,
    _mesh_bounds_record,
    _remove_source_custom_properties,
    _validate_staticization_bounds,
    _validate_authored_recipe_material_use,
    _validate_normalized_mesh_state,
    _validate_runtime_material_images,
    _validate_static_source,
    load_external_asset_set,
    staged_external_asset_set,
)
from tools.blender.vista_playable_home_realism.inspect import (
    _validate_bundle_glb,
    _validate_bundle_record,
    _validate_external_manifest_binding,
)
from tools.blender.vista_playable_home_realism.placement import (
    NORMALIZATION_POLICY,
    PLACEMENT_SCHEMA_VERSION,
    placement_manifest_document,
)


REPO_ROOT = Path(__file__).parents[2]
HOUSE_PATH = REPO_ROOT / "world_packs" / "vista_playable_home_r1" / "house.json"
PROFILE_PATH = (
    REPO_ROOT
    / "world_packs"
    / "vista_playable_home_r1"
    / "visual_profiles"
    / "realistic_interior_r2.json"
)
PLACEMENT_PATH = (
    REPO_ROOT
    / "world_packs"
    / "vista_playable_home_r1"
    / "visual_profiles"
    / "realistic_interior_r2_external_placement.json"
)


def _source_tree_digest(files: tuple[AcquiredFile, ...]) -> str:
    rows = [
        {
            "relative_path": item.relative_path,
            "size_bytes": item.size_bytes,
            "sha256": item.sha256,
        }
        for item in files
    ]
    return hashlib.sha256(_canonical_acquisition_json(rows)).hexdigest()


def _coffee_source_files() -> tuple[AcquiredFile, ...]:
    return (
        AcquiredFile(
            "modern_coffee_table_01_4k.blend",
            235731,
            "119594affca76664a182fedf0acf6b62c5d9d700681a004d442e7f3488956b6f",
            (),
            None,
        ),
        AcquiredFile(
            "textures/modern_coffee_table_01_diff_4k.jpg",
            6200991,
            "37cbe0f2aa7f00c7792ff34280e7905c263e359958144aa8e2de8429f5837b11",
            ("base_color",),
            (4096, 4096),
        ),
        AcquiredFile(
            "textures/modern_coffee_table_01_nor_gl_4k.exr",
            14235190,
            "5dd497f102a11695d3840cd1e486d12698c1f1dd32d6ccb0d583c4984d4b0bf8",
            ("normal",),
            (4096, 4096),
        ),
        AcquiredFile(
            "textures/modern_coffee_table_01_rough_4k.exr",
            10164849,
            "239df397bae3e792fab67f866a4e05cdeda44c3d19d76d6e6dc23534b985d419",
            ("roughness",),
            (4096, 4096),
        ),
    )


def _stove_source_files() -> tuple[AcquiredFile, ...]:
    return (
        AcquiredFile(
            "electric_stove_4k.blend",
            517140,
            "f485d6ec71cfb27a78ff71717c2ac4a8dd0aab6aaa594c228b3bb4a27f4c195b",
            (),
            None,
        ),
        AcquiredFile(
            "textures/electric_stove_diff_4k.jpg",
            6200676,
            "20af305630d5f4e0ee042ce0010615b0d1072194cdf9eb31e4aef493b36ea032",
            ("base_color",),
            (4096, 4096),
        ),
        AcquiredFile(
            "textures/electric_stove_metal_4k.exr",
            7193171,
            "fb2236f76c78b23e36e9d7faeb077a0024c8989eef8ed68bd4c8e98af1312ed9",
            ("metalness",),
            (4096, 4096),
        ),
        AcquiredFile(
            "textures/electric_stove_nor_gl_4k.exr",
            13682521,
            "8d017983d440ec3cbd31ec877713cc4450ff4f5b4a86d51561f75908609e903f",
            ("normal",),
            (4096, 4096),
        ),
        AcquiredFile(
            "textures/electric_stove_opacity_4k.png",
            252946,
            "304c294d75e1d6b916d1bba8018e31c798f12e33f43ff7407b5a7915e961dba9",
            ("opacity",),
            (4096, 4096),
        ),
        AcquiredFile(
            "textures/electric_stove_rough_4k.exr",
            12301920,
            "ac431bc9486799ea0cf7d46e9df7101147aafcbc6cb0d30a0a735e635d482e87",
            ("roughness",),
            (4096, 4096),
        ),
    )


def _sources(
    logical_id: str,
    asset_type: str,
    resolution: str,
    dimensions,
    *,
    asset_id: str | None = None,
    provider_hash: str | None = None,
    semantics=("base_color", "normal", "roughness"),
    files: tuple[AcquiredFile, ...] | None = None,
) -> AcquiredAsset:
    resolved_asset_id = asset_id or logical_id.rsplit(".", 1)[-1]
    texture_files = tuple(
        AcquiredFile(
            relative_path=f"textures/{logical_id.rsplit('.', 1)[-1]}_{semantic}.png",
            size_bytes=24,
            sha256=hashlib.sha256(f"{logical_id}:{semantic}".encode()).hexdigest(),
            semantic=(semantic,),
            dimensions_px=(4096, 4096) if resolution == "4k" else (2048, 2048),
        )
        for semantic in semantics
    )
    primary_file = AcquiredFile(
        relative_path="fixture.blend",
        size_bytes=24,
        sha256=hashlib.sha256(f"{logical_id}:blend".encode()).hexdigest(),
        semantic=(),
        dimensions_px=None,
    )
    acquired_files = files or (
        (primary_file, *texture_files) if asset_type == "model" else texture_files
    )
    return AcquiredAsset(
        asset_id=resolved_asset_id,
        logical_asset_id=logical_id,
        asset_type=asset_type,
        room_role="fixture",
        resolution=resolution,
        file_variant="blend" if asset_type == "model" else "pbr_jpg",
        provider_files_hash=provider_hash or hashlib.sha1(logical_id.encode()).hexdigest(),
        source_relative_root=f"assets/{resolved_asset_id}",
        primary_relative_path=f"assets/{resolved_asset_id}/{acquired_files[0].relative_path}",
        source_tree_sha256=_source_tree_digest(acquired_files),
        catalog_dimensions_m=dimensions,
        files=acquired_files,
    )


def _asset_set(tmp_path: Path) -> ExternalAssetSet:
    assets = (
        _sources("visual.material.white_oak_veneer", "texture", "4k", None),
        _sources("visual.material.poly_wool_herringbone", "texture", "4k", None),
        _sources(
            "visual.hero.living_coffee_table",
            "model",
            "4k",
            (1.2018300294876099, 0.6000000834465027, 0.38999998569488525),
            asset_id="modern_coffee_table_01",
            provider_hash="31772c0aab6f930a18de82606146c0a97f08b7d0",
            files=_coffee_source_files(),
        ),
        _sources(
            "visual.hero.kitchen_stove",
            "model",
            "4k",
            (0.5025948286056519, 0.6476211845874786, 0.8586971759796143),
            asset_id="electric_stove",
            provider_hash="750ee10bdfe78eb6b0b620ef7b5a898e436fb696",
            files=_stove_source_files(),
        ),
        _sources("visual.dressing.entry.rubber_boots", "model", "2k", (0.4, 0.2, 0.4)),
    )
    return ExternalAssetSet(
        root=tmp_path,
        receipt_digest="1" * 64,
        receipt_file_sha256="2" * 64,
        acquisition_manifest_sha256="3" * 64,
        assets=tuple(sorted(assets, key=lambda item: item.logical_asset_id)),
    )


def _row(
    placement_id: str,
    kind: str,
    room_kind: str,
    category: str,
    mode: str,
    *,
    target: str | None = None,
    anchor: str | None = None,
    support: str | None = None,
    source: str | None = None,
    recipe: str | None = None,
    materials=(),
    offset=(0, 0, 0),
    rotation=(0, 0, 0),
    scale=1,
    dimensions=None,
) -> dict:
    return {
        "placement_id": placement_id,
        "placement_kind": kind,
        "room_kind": room_kind,
        "category": category,
        "realization_mode": mode,
        "semantic_target_id": target,
        "anchor_id": anchor,
        "support_placement_id": support,
        "source_logical_asset_id": source,
        "geometry_recipe": recipe,
        "material_logical_asset_ids": list(materials),
        "location_offset_m": list(offset),
        "rotation_offset_deg": list(rotation),
        "uniform_scale": scale,
        "authored_dimensions_m": list(dimensions) if dimensions else None,
    }


def _placement_payload(asset_set: ExternalAssetSet) -> dict:
    rows = [
        _row(
            "hero.entry.shoe_bench", "semantic_fixed", "entry_hall", "shoe_bench",
            "project_authored",
            target="home.r1/room.entry_hall/entity.shoe_bench.01",
            recipe="contemporary_shoe_bench_v1",
            materials=("visual.material.white_oak_veneer", "visual.material.poly_wool_herringbone"),
            dimensions=(1, 0.38, 0.55),
        ),
        _row(
            "hero.living.sofa", "semantic_fixed", "living_room", "sofa",
            "project_authored",
            target="home.r1/room.living_room/entity.sofa.01",
            recipe="contemporary_sofa_v1",
            materials=("visual.material.white_oak_veneer", "visual.material.poly_wool_herringbone"),
            dimensions=(1.6, 0.82, 0.78),
        ),
        _row(
            "hero.living.coffee_table", "semantic_fixed", "living_room", "coffee_table",
            "external_blend",
            target="home.r1/room.living_room/entity.coffee_table.01",
            source="visual.hero.living_coffee_table",
        ),
        _row(
            "hero.kitchen.stove", "semantic_fixed", "kitchen_dining", "stove",
            "external_blend",
            target="home.r1/room.kitchen_dining/entity.stove.01",
            source="visual.hero.kitchen_stove",
        ),
        _row(
            "hero.kitchen.dining_table", "semantic_fixed", "kitchen_dining", "dining_table",
            "project_authored",
            target="home.r1/room.kitchen_dining/entity.dining_table.01",
            recipe="contemporary_dining_table_v1",
            materials=("visual.material.white_oak_veneer",),
            dimensions=(1.6, 0.9, 0.76),
        ),
        _row(
            "dress.entry.rubber_boots", "dressing", "entry_hall", "shoe",
            "external_blend",
            anchor="home.r1/room.entry_hall/dressing_anchor.shoe_drop",
            support="hero.entry.shoe_bench",
            source="visual.dressing.entry.rubber_boots",
        ),
    ]
    payload = {
        "schema_version": PLACEMENT_SCHEMA_VERSION,
        "placement_id": "fixture.external.placement",
        "acquisition": {
            **asset_set.receipt_reference(),
            "receipt_filename": "acquisition-receipt.json",
        },
        "placements": rows,
    }
    payload["content_digest"] = content_digest(payload)
    return payload


def _plan(tmp_path: Path, payload: dict | None = None):
    house = json.loads(HOUSE_PATH.read_text(encoding="utf-8"))
    profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    assets = _asset_set(tmp_path)
    return build_external_forge_plan(
        house,
        profile,
        assets,
        placement_manifest_document(payload or _placement_payload(assets)),
    )


def _redigest(payload: dict) -> dict:
    payload = copy.deepcopy(payload)
    payload.pop("content_digest", None)
    payload["content_digest"] = content_digest(payload)
    return payload


def test_build_acceptance_rejects_zero_exit_traceback_without_terminal_marker(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "failed-blender-run"
    output_root.mkdir()
    (output_root / "blender.log").write_text(
        "Traceback (most recent call last):\nRuntimeError: inspector rejected output\n",
        encoding="utf-8",
    )
    with pytest.raises(ForgeInputError, match="missing regular file forge-accepted.json"):
        forge_build.validate_build_acceptance(output_root)


def test_build_acceptance_rejects_marker_with_stale_receipt_hash(tmp_path: Path) -> None:
    output_root = tmp_path / "stale-marker"
    output_root.mkdir()
    receipt_names = (
        "normalized-manifest.json",
        "artifact-receipt.json",
        "inspection-receipt.json",
        "build-receipt.json",
    )
    for name in receipt_names:
        (output_root / name).write_bytes(b"{}\n")
    empty_object_sha256 = hashlib.sha256(b"{}\n").hexdigest()
    marker = {
        "schema_version": forge_build.BUILD_ACCEPTANCE_SCHEMA,
        "status": "accepted",
        "normalized_manifest_sha256": "0" * 64,
        "artifact_receipt_sha256": empty_object_sha256,
        "inspection_receipt_sha256": empty_object_sha256,
        "build_receipt_sha256": empty_object_sha256,
        "external_staticization_receipt_sha256": None,
    }
    (output_root / forge_build.BUILD_ACCEPTANCE_FILENAME).write_bytes(
        canonical_json_bytes(marker)
    )
    with pytest.raises(
        ForgeInputError,
        match="normalized_manifest_sha256 differs from normalized-manifest.json",
    ):
        forge_build.validate_build_acceptance(output_root)


def test_external_plan_uses_room_local_meters_and_keeps_world_room_offset(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    coffee = next(item for item in plan.external_placement.placements if item.category == "coffee_table")
    living = next(item for item in plan.rooms if item.kind == "living_room")
    assert coffee.location_m == (0, 0.3, 0)
    assert living.location_m == (-4, -2, 0)
    assert coffee.room_local_aabb.min_m == pytest.approx((-0.600915, 0, 0))
    assert coffee.room_local_aabb.max_m == pytest.approx((0.600915, 0.6, 0.39))
    contract = ue_bundle_contract(
        plan,
        living,
        exported_material_names=("r2.external.coffee", "r2.external.wool"),
    )
    assert contract["expected_world_transform_cm"]["location_cm"] == [-400, -200, 0]
    assert contract["material_ids"] == ["r2.external.coffee", "r2.external.wool"]
    assert contract["external_content"]["acquisition_receipt"]["receipt_file_sha256"] == "2" * 64
    assert contract["external_content"]["semantic_target_ids"] == [
        "home.r1/room.living_room/entity.coffee_table.01",
        "home.r1/room.living_room/entity.sofa.01",
    ]


def test_external_bundle_receipts_are_bound_back_to_top_level_plan(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    bundles = [
        ue_bundle_contract(
            plan,
            room,
            exported_material_names=(f"r2.{room.kind}.a", f"r2.{room.kind}.b"),
        )
        for room in plan.rooms
    ]
    manifest = normalized_manifest(plan, texture_size_px=512)
    _validate_external_manifest_binding(manifest, bundles)
    tampered = copy.deepcopy(bundles)
    tampered[0]["external_content"]["asset_sources"][0]["files"][0]["sha256"] = "0" * 64
    with pytest.raises(ForgeInputError, match="differs from normalized manifest"):
        _validate_external_manifest_binding(manifest, tampered)


def test_external_plan_rejects_overlap_room_escape_and_movable_target(tmp_path: Path) -> None:
    assets = _asset_set(tmp_path)
    overlap = _placement_payload(assets)
    boots = next(item for item in overlap["placements"] if item["placement_id"] == "dress.entry.rubber_boots")
    boots["support_placement_id"] = None
    boots["location_offset_m"] = [0, 0.2, -0.56]
    with pytest.raises(ForgeInputError, match="AABBs overlap"):
        _plan(tmp_path, _redigest(overlap))

    outside = _placement_payload(assets)
    bench = next(item for item in outside["placements"] if item["placement_id"] == "hero.entry.shoe_bench")
    bench["authored_dimensions_m"] = [3.2, 0.38, 0.55]
    with pytest.raises(ForgeInputError, match="leaves room-local bounds"):
        _plan(tmp_path, _redigest(outside))

    movable = _placement_payload(assets)
    coffee = next(item for item in movable["placements"] if item["placement_id"] == "hero.living.coffee_table")
    coffee["semantic_target_id"] = "home.r1/room.living_room/entity.keys.01"
    with pytest.raises(ForgeInputError, match="movable/forbidden"):
        _plan(tmp_path, _redigest(movable))


@pytest.mark.parametrize(
    "recipe,materials",
    (
        (
            "contemporary_shoe_bench_v1",
            ("visual.material.poly_wool_herringbone", "visual.material.white_oak_veneer"),
        ),
        ("contemporary_sofa_v1", ("visual.material.white_oak_veneer",)),
        (
            "contemporary_dining_table_v1",
            ("visual.material.white_oak_veneer", "visual.material.poly_wool_herringbone"),
        ),
    ),
)
def test_project_authored_recipe_requires_exact_material_logical_ids(
    tmp_path: Path,
    recipe: str,
    materials: tuple[str, ...],
) -> None:
    assets = _asset_set(tmp_path)
    payload = _placement_payload(assets)
    row = next(item for item in payload["placements"] if item["geometry_recipe"] == recipe)
    row["material_logical_asset_ids"] = list(materials)
    with pytest.raises(ForgeInputError, match="project-authored placement source is invalid"):
        _plan(tmp_path, _redigest(payload))


def test_project_authored_recipe_rejects_non_string_recipe_without_type_leak(tmp_path: Path) -> None:
    assets = _asset_set(tmp_path)
    payload = _placement_payload(assets)
    row = next(item for item in payload["placements"] if item["geometry_recipe"] is not None)
    row["geometry_recipe"] = ["contemporary_sofa_v1"]
    with pytest.raises(ForgeInputError, match="project-authored placement source is invalid"):
        _plan(tmp_path, _redigest(payload))


def test_authored_recipe_contract_is_explicit_and_complete() -> None:
    assert AUTHORED_RECIPE_MATERIAL_IDS == {
        "contemporary_shoe_bench_v1": (
            "visual.material.white_oak_veneer",
            "visual.material.poly_wool_herringbone",
        ),
        "contemporary_sofa_v1": (
            "visual.material.white_oak_veneer",
            "visual.material.poly_wool_herringbone",
        ),
        "contemporary_dining_table_v1": ("visual.material.white_oak_veneer",),
    }


def _png_header(size: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", size, size)


def _write_acquisition(root: Path) -> dict:
    asset_root = root / "assets" / "fixture_model"
    texture_root = asset_root / "textures"
    texture_root.mkdir(parents=True)
    rows = []
    file_payloads = [("fixture_model_2k.blend", b"BLENDER-v300")]
    file_payloads += [
        (f"textures/fixture_{name}_2k.png", _png_header(2048))
        for name in ("diff", "nor_gl", "rough")
    ]
    for relative, data in file_payloads:
        path = asset_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        rows.append(
            {
                "relative_path": relative,
                "url": f"https://dl.polyhaven.org/file/ph-assets/{Path(relative).name}",
                "size_bytes": len(data),
                "provider_md5": hashlib.md5(data).hexdigest(),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    tree = [{key: row[key] for key in ("relative_path", "size_bytes", "sha256")} for row in rows]
    asset = {
        "asset_id": "fixture_model",
        "logical_asset_id": "visual.dressing.fixture_model",
        "asset_type": "model",
        "room_role": "fixture",
        "resolution": "2k",
        "file_variant": "blend",
        "catalog": {"dimensions": [400, 200, 500]},
        "provider_files_hash": "a" * 40,
        "source_relative_root": "assets/fixture_model",
        "primary_relative_path": "assets/fixture_model/fixture_model_2k.blend",
        "files": rows,
        "source_tree_sha256": hashlib.sha256(_canonical_acquisition_json(tree)).hexdigest(),
    }
    receipt = {
        "schema_version": ACQUISITION_RECEIPT_SCHEMA,
        "provider": "poly_haven",
        "catalog_urls": {},
        "license": {
            "license_id": "CC0-1.0",
            "entitlement_status": "verified",
            "commercial_use": "allowed",
        },
        "manifest_sha256": "b" * 64,
        "acquired_at_utc": "2026-08-16T00:00:00Z",
        "asset_count": 1,
        "total_size_bytes": sum(row["size_bytes"] for row in rows),
        "assets": [asset],
    }
    receipt["receipt_digest"] = hashlib.sha256(_canonical_acquisition_json(receipt)).hexdigest()
    (root / "acquisition-receipt.json").write_bytes(_canonical_acquisition_json(receipt))
    return receipt


def test_acquisition_root_verifies_sha_resolution_and_rejects_symlink(tmp_path: Path) -> None:
    root = tmp_path / "attempt"
    root.mkdir()
    receipt = _write_acquisition(root)
    assets = load_external_asset_set(root)
    assert assets.receipt_digest == receipt["receipt_digest"]
    assert assets.asset("visual.dressing.fixture_model").pbr_semantics == {
        "base_color", "normal", "roughness"
    }
    texture = root / "assets/fixture_model/textures/fixture_diff_2k.png"
    data = bytearray(texture.read_bytes())
    data[-1] ^= 1
    texture.write_bytes(data)
    with pytest.raises(ForgeInputError, match="SHA-256 mismatch"):
        load_external_asset_set(root)

    link = tmp_path / "linked-attempt"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(ForgeInputError, match="non-symlink"):
        load_external_asset_set(link)


def _private_tmpdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    private_tmp = tmp_path / "private-tmp"
    private_tmp.mkdir(mode=0o700)
    monkeypatch.setenv("TMPDIR", str(private_tmp.resolve()))
    return private_tmp


def test_private_snapshot_pins_content_for_full_context_lifetime_and_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "acquisition"
    root.mkdir()
    _write_acquisition(root)
    asset_set = load_external_asset_set(root)
    private_tmp = _private_tmpdir(tmp_path, monkeypatch)
    source = root / "assets/fixture_model/fixture_model_2k.blend"
    original = source.read_bytes()

    with staged_external_asset_set(asset_set) as staged:
        stage_root = staged.root
        staged_primary = staged.source_path("visual.dressing.fixture_model")
        assert stage_root.parent == private_tmp.resolve()
        assert stage_root.exists()
        assert staged_primary.read_bytes() == original
        assert hashlib.sha256(
            (stage_root / "acquisition-receipt.json").read_bytes()
        ).hexdigest() == asset_set.receipt_file_sha256
        # A concurrent write to the acquisition pathname cannot affect the
        # already verified private tree consumed by Blender.
        source.write_bytes(b"X" * len(original))
        assert staged_primary.read_bytes() == original
    assert not stage_root.exists()
    assert list(private_tmp.glob("vista-external-assets-*")) == []


def test_external_build_wrapper_keeps_snapshot_alive_through_runtime_then_removes_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "wrapped-acquisition"
    root.mkdir()
    _write_acquisition(root)
    asset_set = load_external_asset_set(root)
    _private_tmpdir(tmp_path, monkeypatch)
    observed: dict[str, Path] = {}

    def fake_runtime(*_args, external_asset_set=None, **_kwargs):
        observed["root"] = external_asset_set.root
        assert observed["root"].exists()
        assert external_asset_set.source_path("visual.dressing.fixture_model").is_file()
        return {"status": "ok"}

    monkeypatch.setattr(forge_build, "_build_with_blender_runtime", fake_runtime)
    result = forge_build.build_with_blender(
        object(),
        object(),
        {},
        {},
        tmp_path / "output",
        texture_size_px=512,
        external_asset_set=asset_set,
        external_placement_manifest=object(),
    )
    assert result == {"status": "ok"}
    assert not observed["root"].exists()


def test_private_snapshot_requires_a_current_user_nonwritable_staging_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "mode-acquisition"
    root.mkdir()
    _write_acquisition(root)
    asset_set = load_external_asset_set(root)
    insecure = tmp_path / "insecure-tmp"
    insecure.mkdir(mode=0o700)
    insecure.chmod(0o777)
    monkeypatch.setenv("TMPDIR", str(insecure.resolve()))
    with pytest.raises(RuntimeError, match="private to and accessible by the current process"):
        with staged_external_asset_set(asset_set):
            pytest.fail("insecure staging parent must fail closed")


@pytest.mark.parametrize("mutation", ("changed_content", "symlink"))
def test_private_snapshot_rejects_unverified_source_mutation_and_removes_partial_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    root = tmp_path / mutation / "acquisition"
    root.mkdir(parents=True)
    _write_acquisition(root)
    asset_set = load_external_asset_set(root)
    private_tmp = _private_tmpdir(tmp_path / mutation, monkeypatch)
    source = root / "assets/fixture_model/fixture_model_2k.blend"
    if mutation == "changed_content":
        source.write_bytes(b"Y" * len(source.read_bytes()))
        expected = "SHA-256 differs from receipt"
    else:
        backing = source.with_name("backing.blend")
        source.rename(backing)
        source.symlink_to(backing)
        expected = "symbolic link"
    with pytest.raises((RuntimeError, ForgeInputError), match=expected):
        with staged_external_asset_set(asset_set):
            pytest.fail("unverified source must not enter the staging context")
    assert list(private_tmp.glob("vista-external-assets-*")) == []


class _FakeLibraryContext:
    def __init__(self, callback=None):
        self.callback = callback
        self.source = SimpleNamespace(objects=["LoadedMesh"])
        self.target = SimpleNamespace(objects=[])

    def __enter__(self):
        return self.source, self.target

    def __exit__(self, exc_type, exc, traceback):
        if self.callback is not None:
            self.callback()
        return False


class _FakeLibraries:
    def __init__(self, callback=None):
        self.callback = callback
        self.calls: list[tuple[str, bool]] = []

    def load(self, path: str, *, link: bool):
        self.calls.append((path, link))
        return _FakeLibraryContext(self.callback)


def _verified_blend_fixture(tmp_path: Path, callback=None):
    root = tmp_path / "acquisition"
    root.mkdir(parents=True)
    _write_acquisition(root)
    asset_set = load_external_asset_set(root)
    asset = asset_set.asset("visual.dressing.fixture_model")
    libraries = _FakeLibraries(callback)
    bpy = SimpleNamespace(data=SimpleNamespace(libraries=libraries))
    return bpy, libraries, asset_set, asset, asset_set.source_path(asset.logical_asset_id)


def test_primary_blend_is_bound_to_exact_receipt_before_and_after_library_load(tmp_path: Path) -> None:
    bpy, libraries, asset_set, asset, path = _verified_blend_fixture(tmp_path)
    assert _load_verified_blend_objects(bpy, asset_set, asset) == ["LoadedMesh"]
    assert libraries.calls == [(str(path), False)]


@pytest.mark.parametrize("mutation", ("changed_sha", "replaced_inode"))
def test_primary_blend_rejects_changed_bytes_or_path_replacement_across_library_load(
    tmp_path: Path,
    mutation: str,
) -> None:
    holder: dict[str, Path] = {}

    def mutate() -> None:
        path = holder["path"]
        original = path.read_bytes()
        if mutation == "changed_sha":
            path.write_bytes(b"Z" * len(original))
        else:
            replacement = path.with_name("replacement.blend")
            replacement.write_bytes(original)
            os.replace(replacement, path)

    bpy, _libraries, asset_set, asset, path = _verified_blend_fixture(tmp_path, mutate)
    holder["path"] = path
    expected = "SHA-256 differs from receipt" if mutation == "changed_sha" else "changed across"
    with pytest.raises(RuntimeError, match=expected):
        _load_verified_blend_objects(bpy, asset_set, asset)


class _FakeSockets(dict):
    def __iter__(self):
        return iter(self.values())


class _FakeSocket:
    def __init__(self, name: str):
        self.name = name
        self.links: list[object] = []


class _FakeNode:
    def __init__(
        self,
        node_type: str,
        name: str,
        *,
        inputs: tuple[str, ...] = (),
        outputs: tuple[str, ...] = (),
        image=None,
        active_output: bool = False,
        node_tree=None,
    ):
        self.type = node_type
        self.name = name
        self.inputs = _FakeSockets((name, _FakeSocket(name)) for name in inputs)
        self.outputs = _FakeSockets((name, _FakeSocket(name)) for name in outputs)
        self.image = image
        self.is_active_output = active_output
        self.node_tree = node_tree


class _FakeLink:
    def __init__(self, source: _FakeNode, source_socket: str, target: _FakeNode, target_socket: str):
        self.from_node = source
        self.from_socket = source.outputs[source_socket]
        self.to_node = target
        self.to_socket = target.inputs[target_socket]
        self.from_socket.links.append(self)
        self.to_socket.links.append(self)


class _FakeMaterial:
    def __init__(self, name: str, nodes=(), *, source: str | None = None):
        self.name = name
        self.use_nodes = True
        self.animation_data = None
        self.library = None
        self.node_tree = SimpleNamespace(nodes=list(nodes), animation_data=None, library=None)
        self._properties = {}
        if source is not None:
            self._properties[EXTERNAL_TEXTURE_MATERIAL_SOURCE_PROPERTY] = source

    def get(self, key: str, default=None):
        return self._properties.get(key, default)


class _FakeImage:
    def __init__(
        self,
        path: Path,
        dimensions: tuple[int, int],
        library=None,
        *,
        colorspace: str = "Non-Color",
        reload_callback=None,
        pointer: int | None = None,
    ):
        self.source = "FILE"
        self.filepath_raw = str(path)
        self.filepath = "//shared.png"
        self.library = library
        self.packed_file = None
        self.packed_files = ()
        self.size = dimensions
        self.colorspace_settings = SimpleNamespace(name=colorspace)
        self.reload_count = 0
        self._reload_callback = reload_callback
        self._pointer = pointer

    def reload(self) -> None:
        self.reload_count += 1
        if self._reload_callback is not None:
            self._reload_callback(self)

    def as_pointer(self) -> int:
        return self._pointer or id(self)


class _FakeBpyPath:
    def __init__(self):
        self.calls: list[tuple[str, object]] = []

    def abspath(self, raw: str, *, library=None) -> str:
        self.calls.append((raw, library))
        return raw


def _runtime_material_fixture(tmp_path: Path):
    root = tmp_path / "runtime-acquisition"
    source_root = root / "assets" / "fixture_model"
    specs = (
        ("textures/base/shared.png", b"base-color", (17, 19), ("base_color",), "sRGB"),
        ("textures/normal/shared.png", b"normal-map", (23, 29), ("normal",), "Non-Color"),
        ("textures/rough/shared.png", b"roughness-map", (31, 37), ("roughness",), "Non-Color"),
        ("textures/metal/shared.png", b"metalness-map", (41, 43), ("metalness",), "Non-Color"),
        ("textures/opacity/shared.png", b"opacity-map", (47, 53), ("opacity",), "Non-Color"),
    )
    files = []
    images = []
    library = None
    for relative, payload, dimensions, semantics, colorspace in specs:
        path = source_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        files.append(
            AcquiredFile(
                relative_path=relative,
                size_bytes=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
                semantic=semantics,
                dimensions_px=dimensions,
            )
        )
        images.append(_FakeImage(path.resolve(), dimensions, library, colorspace=colorspace))
    asset = AcquiredAsset(
        asset_id="fixture_model",
        logical_asset_id="visual.dressing.fixture_model",
        asset_type="model",
        room_role="fixture",
        resolution="2k",
        file_variant="blend",
        provider_files_hash="a" * 40,
        source_relative_root="assets/fixture_model",
        primary_relative_path="assets/fixture_model/fixture.blend",
        source_tree_sha256="b" * 64,
        catalog_dimensions_m=(1.0, 1.0, 1.0),
        files=tuple(files),
    )
    asset_set = ExternalAssetSet(
        root=root.resolve(),
        receipt_digest="1" * 64,
        receipt_file_sha256="2" * 64,
        acquisition_manifest_sha256="3" * 64,
        assets=(asset,),
    )
    output = _FakeNode(
        "OUTPUT_MATERIAL",
        "Active Material Output",
        inputs=("Surface", "Volume", "Displacement"),
        active_output=True,
    )
    shader = _FakeNode(
        "BSDF_PRINCIPLED",
        "Principled BSDF",
        inputs=("Base Color", "Roughness", "Normal", "Metallic", "Alpha"),
        outputs=("BSDF",),
    )
    base = _FakeNode("TEX_IMAGE", "Base Color Texture", outputs=("Color", "Alpha"), image=images[0])
    normal = _FakeNode("TEX_IMAGE", "Normal Texture", outputs=("Color", "Alpha"), image=images[1])
    rough = _FakeNode("TEX_IMAGE", "Roughness Texture", outputs=("Color", "Alpha"), image=images[2])
    metal = _FakeNode("TEX_IMAGE", "Metalness Texture", outputs=("Color", "Alpha"), image=images[3])
    opacity = _FakeNode("TEX_IMAGE", "Opacity Texture", outputs=("Color", "Alpha"), image=images[4])
    normal_map = _FakeNode(
        "NORMAL_MAP",
        "Normal Map",
        inputs=("Color",),
        outputs=("Normal",),
    )
    _FakeLink(shader, "BSDF", output, "Surface")
    _FakeLink(base, "Color", shader, "Base Color")
    _FakeLink(rough, "Color", shader, "Roughness")
    _FakeLink(normal, "Color", normal_map, "Color")
    _FakeLink(normal_map, "Normal", shader, "Normal")
    _FakeLink(metal, "Color", shader, "Metallic")
    _FakeLink(opacity, "Alpha", shader, "Alpha")
    material = _FakeMaterial(
        "FixtureMaterial",
        nodes=[output, shader, base, normal, rough, metal, opacity, normal_map],
    )
    mesh = SimpleNamespace(material_slots=[SimpleNamespace(material=material)])
    path_api = _FakeBpyPath()
    bpy = SimpleNamespace(path=path_api)
    return bpy, path_api, mesh, asset_set, asset, images, library


class _FakeImages(list):
    def __init__(
        self,
        dimensions: tuple[int, int],
        *,
        existing=None,
        returned_existing=None,
        reload_callback=None,
    ):
        super().__init__([existing] if existing is not None else [])
        self.dimensions = dimensions
        self.existing = existing
        self.returned_existing = returned_existing
        self.reload_callback = reload_callback
        self.load_calls: list[tuple[str, bool]] = []

    def load(self, path: str, *, check_existing: bool):
        self.load_calls.append((path, check_existing))
        if self.existing is not None:
            return self.returned_existing or self.existing
        image = _FakeImage(
            Path(path),
            self.dimensions,
            reload_callback=self.reload_callback,
        )
        self.append(image)
        return image

    def remove(self, image) -> None:
        super().remove(image)


def _fresh_image_bpy(
    asset_set: ExternalAssetSet,
    asset: AcquiredAsset,
    *,
    stale=False,
    stale_proxy=False,
    callback=None,
):
    receipt_file = next(item for item in asset.files if "base_color" in item.semantic)
    path = (
        asset_set.root
        / asset.source_relative_root
        / Path(receipt_file.relative_path)
    ).resolve()
    existing = _FakeImage(path, receipt_file.dimensions_px, pointer=8675309) if stale else None
    returned_existing = (
        _FakeImage(path, receipt_file.dimensions_px, pointer=8675309)
        if stale and stale_proxy
        else None
    )
    images = _FakeImages(
        receipt_file.dimensions_px,
        existing=existing,
        returned_existing=returned_existing,
        reload_callback=callback,
    )
    path_api = _FakeBpyPath()
    bpy = SimpleNamespace(path=path_api, data=SimpleNamespace(images=images))
    return bpy, images, path_api, path, existing


def test_authored_pbr_loads_a_fresh_receipt_bound_datablock_with_check_existing_false(
    tmp_path: Path,
) -> None:
    _fixture_bpy, _path_api, _mesh, asset_set, asset, _runtime_images, _library = (
        _runtime_material_fixture(tmp_path)
    )
    bpy, images, path_api, path, _existing = _fresh_image_bpy(asset_set, asset)
    image = _load_fresh_receipt_image(bpy, asset_set, asset, "base_color")
    assert image in images
    assert images.load_calls == [(str(path), False)]
    assert image.reload_count == 1
    assert path_api.calls == [(str(path), None), (str(path), None)]


def test_authored_pbr_rejects_stale_reuse_and_removes_fresh_image_on_byte_change(
    tmp_path: Path,
) -> None:
    _fixture_bpy, _path_api, _mesh, asset_set, asset, _runtime_images, _library = (
        _runtime_material_fixture(tmp_path / "stale")
    )
    bpy, images, _path_api, path, existing = _fresh_image_bpy(
        asset_set,
        asset,
        stale=True,
        stale_proxy=True,
    )
    with pytest.raises(RuntimeError, match="reused a stale datablock"):
        _load_fresh_receipt_image(bpy, asset_set, asset, "base_color")
    assert images.load_calls == [(str(path), False)]
    assert list(images) == [existing]

    _fixture_bpy, _path_api, _mesh, asset_set, asset, _runtime_images, _library = (
        _runtime_material_fixture(tmp_path / "changed")
    )

    def mutate_reloaded_bytes(_image) -> None:
        path.write_bytes(b"Q" * len(path.read_bytes()))

    bpy, images, _path_api, path, _existing = _fresh_image_bpy(
        asset_set,
        asset,
        callback=mutate_reloaded_bytes,
    )
    with pytest.raises(RuntimeError, match="SHA-256 differs from receipt"):
        _load_fresh_receipt_image(bpy, asset_set, asset, "base_color")
    assert images == []


def test_authored_pbr_rejects_duplicate_receipt_semantics(tmp_path: Path) -> None:
    _fixture_bpy, _path_api, _mesh, asset_set, asset, _runtime_images, _library = (
        _runtime_material_fixture(tmp_path)
    )
    base = next(item for item in asset.files if "base_color" in item.semantic)
    ambiguous = replace(asset, files=asset.files + (base,))
    bpy, _images, _path_api, _path, _existing = _fresh_image_bpy(asset_set, asset)
    with pytest.raises(RuntimeError, match="base_color texture is absent or ambiguous"):
        _load_fresh_receipt_image(bpy, asset_set, ambiguous, "base_color")


def test_runtime_material_images_bind_active_surface_semantics_to_exact_full_paths(tmp_path: Path) -> None:
    bpy, path_api, mesh, asset_set, asset, images, library = _runtime_material_fixture(tmp_path)
    # All receipt textures intentionally share a basename. Their
    # dimensions differ, so a basename-keyed implementation cannot pass.
    _validate_runtime_material_images(bpy, [mesh], asset_set, asset)
    assert Counter(path_api.calls) == Counter(
        (image.filepath_raw, library) for image in images for _ in range(6)
    )
    assert [image.reload_count for image in images] == [2] * len(images)


def _runtime_material(tmp_path: Path):
    fixture = _runtime_material_fixture(tmp_path)
    return fixture, fixture[2].material_slots[0].material


def test_active_surface_rejects_disconnected_impostor_swapped_semantics_and_ambiguous_link(
    tmp_path: Path,
) -> None:
    (bpy, _path_api, mesh, asset_set, asset, images, _library), material = _runtime_material(
        tmp_path / "disconnected"
    )
    material.node_tree.nodes.append(
        _FakeNode("TEX_IMAGE", "Disconnected Impostor", outputs=("Color",), image=images[0])
    )
    validated = _validate_runtime_material_images(bpy, [mesh], asset_set, asset)
    assert [node.name for node in material.node_tree.nodes].count("Disconnected Impostor") == 0
    assert validated[0][2] == [
        {
            "node_name": "Disconnected Impostor",
            "image_name": asset.files[0].relative_path,
            "relative_path": asset.files[0].relative_path,
            "sha256": asset.files[0].sha256,
            "reason": "inactive_disconnected_receipt_bound_image",
        }
    ]

    (bpy, _path_api, mesh, asset_set, asset, _images, _library), material = _runtime_material(
        tmp_path / "outside-receipt"
    )
    outside_path = tmp_path / "outside.png"
    outside_path.write_bytes(b"outside receipt")
    material.node_tree.nodes.append(
        _FakeNode(
            "TEX_IMAGE",
            "Outside Receipt",
            outputs=("Color",),
            image=_FakeImage(outside_path, (2048, 2048)),
        )
    )
    with pytest.raises(RuntimeError, match="outside its verified receipt"):
        _validate_runtime_material_images(bpy, [mesh], asset_set, asset)

    (bpy, _path_api, mesh, asset_set, asset, _images, _library), material = _runtime_material(
        tmp_path / "swapped"
    )
    base = next(node for node in material.node_tree.nodes if node.name == "Base Color Texture")
    rough = next(node for node in material.node_tree.nodes if node.name == "Roughness Texture")
    base.image, rough.image = rough.image, base.image
    with pytest.raises(RuntimeError, match="base_color socket uses receipt semantics"):
        _validate_runtime_material_images(bpy, [mesh], asset_set, asset)

    (bpy, _path_api, mesh, asset_set, asset, images, _library), material = _runtime_material(
        tmp_path / "ambiguous"
    )
    shader = next(node for node in material.node_tree.nodes if node.type == "BSDF_PRINCIPLED")
    extra = _FakeNode("TEX_IMAGE", "Second Base", outputs=("Color",), image=images[0])
    material.node_tree.nodes.append(extra)
    _FakeLink(extra, "Color", shader, "Base Color")
    with pytest.raises(RuntimeError, match="exactly one input link"):
        _validate_runtime_material_images(bpy, [mesh], asset_set, asset)


def test_active_surface_rejects_nested_groups_and_linked_library_images(tmp_path: Path) -> None:
    (bpy, _path_api, mesh, asset_set, asset, _images, _library), material = _runtime_material(
        tmp_path / "group"
    )
    material.node_tree.nodes.append(
        _FakeNode("GROUP", "Nested Group", node_tree=SimpleNamespace(nodes=[]))
    )
    with pytest.raises(RuntimeError, match="nested node groups"):
        _validate_runtime_material_images(bpy, [mesh], asset_set, asset)

    (bpy, path_api, mesh, asset_set, asset, images, _library), _material = _runtime_material(
        tmp_path / "linked-image"
    )
    linked_library = object()
    images[0].library = linked_library
    with pytest.raises(RuntimeError, match="nested linked-library ID: material image"):
        _validate_runtime_material_images(bpy, [mesh], asset_set, asset)
    assert path_api.calls == [(images[0].filepath_raw, linked_library)]

    (bpy, _path_api, mesh, asset_set, asset, images, _library), _material = _runtime_material(
        tmp_path / "override-image"
    )
    images[0].override_library = object()
    with pytest.raises(RuntimeError, match="library override ID: material image"):
        _validate_runtime_material_images(bpy, [mesh], asset_set, asset)


def test_active_surface_rejects_volume_unsupported_shader_inputs_and_node_linked_ids(
    tmp_path: Path,
) -> None:
    (bpy, _path_api, mesh, asset_set, asset, _images, _library), material = _runtime_material(
        tmp_path / "volume"
    )
    output = next(node for node in material.node_tree.nodes if node.type == "OUTPUT_MATERIAL")
    volume = _FakeNode("VOLUME_ABSORPTION", "Hidden Volume", outputs=("Volume",))
    material.node_tree.nodes.append(volume)
    _FakeLink(volume, "Volume", output, "Volume")
    with pytest.raises(RuntimeError, match="Material Output Volume is unsupported"):
        _validate_runtime_material_images(bpy, [mesh], asset_set, asset)

    (bpy, _path_api, mesh, asset_set, asset, _images, _library), material = _runtime_material(
        tmp_path / "emission"
    )
    shader = next(node for node in material.node_tree.nodes if node.type == "BSDF_PRINCIPLED")
    shader.inputs["Emission Color"] = _FakeSocket("Emission Color")
    emission = _FakeNode("RGB", "Hidden Emission", outputs=("Color",))
    material.node_tree.nodes.append(emission)
    _FakeLink(emission, "Color", shader, "Emission Color")
    with pytest.raises(RuntimeError, match="unsupported linked inputs.*Emission Color"):
        _validate_runtime_material_images(bpy, [mesh], asset_set, asset)

    (bpy, _path_api, mesh, asset_set, asset, _images, _library), material = _runtime_material(
        tmp_path / "node-id"
    )
    coordinate = _FakeNode("TEX_COORD", "Hidden Coordinate", outputs=("UV",))
    coordinate.object = SimpleNamespace(library=object(), override_library=None)
    material.node_tree.nodes.append(coordinate)
    with pytest.raises(RuntimeError, match="nested linked-library ID: material node"):
        _validate_runtime_material_images(bpy, [mesh], asset_set, asset)


@pytest.mark.parametrize(
    "mutation,error",
    (
        (lambda image, _tmp: setattr(image, "source", "TILED"), "must be FILE"),
        (lambda image, _tmp: setattr(image, "source", "SEQUENCE"), "must be FILE"),
        (lambda image, _tmp: setattr(image, "source", "GENERATED"), "must be FILE"),
        (lambda image, _tmp: setattr(image, "packed_file", object()), "may not be packed"),
        (lambda image, _tmp: setattr(image, "packed_files", (object(),)), "may not be packed"),
    ),
)
def test_runtime_material_images_reject_non_file_and_packed_sources(
    tmp_path: Path,
    mutation,
    error: str,
) -> None:
    bpy, _path_api, mesh, asset_set, asset, images, _library = _runtime_material_fixture(tmp_path)
    mutation(images[0], tmp_path)
    with pytest.raises(RuntimeError, match=error):
        _validate_runtime_material_images(bpy, [mesh], asset_set, asset)


def test_runtime_material_images_reject_symlink_outside_path_and_changed_bytes(tmp_path: Path) -> None:
    bpy, _path_api, mesh, asset_set, asset, images, _library = _runtime_material_fixture(tmp_path)
    original = Path(images[0].filepath_raw)
    link = tmp_path / "linked-shared.png"
    link.symlink_to(original)
    images[0].filepath_raw = str(link)
    with pytest.raises(RuntimeError, match="symbolic links"):
        _validate_runtime_material_images(bpy, [mesh], asset_set, asset)

    bpy, _path_api, mesh, asset_set, asset, images, _library = _runtime_material_fixture(
        tmp_path / "outside-case"
    )
    outside = tmp_path / "outside" / "shared.png"
    outside.parent.mkdir()
    outside.write_bytes(Path(images[0].filepath_raw).read_bytes())
    images[0].filepath_raw = str(outside.resolve())
    with pytest.raises(RuntimeError, match="outside its verified receipt"):
        _validate_runtime_material_images(bpy, [mesh], asset_set, asset)

    bpy, _path_api, mesh, asset_set, asset, images, _library = _runtime_material_fixture(
        tmp_path / "changed-case"
    )
    Path(images[0].filepath_raw).write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="size differs from receipt|SHA-256 differs from receipt"):
        _validate_runtime_material_images(bpy, [mesh], asset_set, asset)


def _static_mesh_fixture():
    tree = SimpleNamespace(nodes=[], animation_data=None, library=None)
    material = SimpleNamespace(
        name="StaticMaterial",
        animation_data=None,
        node_tree=tree,
        library=None,
    )
    data = SimpleNamespace(
        name="StaticMeshData",
        animation_data=None,
        shape_keys=None,
        library=None,
        polygons=[SimpleNamespace(material_index=0)],
    )
    obj = SimpleNamespace(
        name="StaticMesh",
        type="MESH",
        library=None,
        modifiers=[],
        constraints=[],
        rigid_body=None,
        rigid_body_constraint=None,
        soft_body=None,
        particle_systems=[],
        field=SimpleNamespace(type="NONE"),
        instance_type="NONE",
        instance_collection=None,
        rotation_mode="XYZ",
        delta_location=(0.0, 0.0, 0.0),
        delta_rotation_euler=(0.0, 0.0, 0.0),
        delta_scale=(1.0, 1.0, 1.0),
        parent=None,
        animation_data=None,
        data=data,
        material_slots=[SimpleNamespace(material=material)],
    )
    return obj, material, tree


@pytest.mark.parametrize(
    "case,error",
    (
        ("modifier", "contains modifiers"),
        ("constraint", "contains constraints"),
        ("object_driver", "animations or drivers"),
        ("data_driver", "animations or drivers"),
        ("material_driver", "material contains animations or drivers"),
        ("node_driver", "material nodes contain animations or drivers"),
        ("rotation", "rotation mode is not deterministic XYZ"),
        ("delta", "non-identity delta transforms"),
        ("drawable", "unsupported drawable object types"),
        ("instancing", "unsupported instancing"),
        ("shape_keys", "contains shape keys"),
        ("rigid_body", "rigid-body state"),
        ("rigid_constraint", "rigid-body constraint"),
        ("soft_body", "soft-body state"),
        ("particles", "particle-system state"),
        ("force_field", "non-NONE force field"),
        ("object_library", "nested linked-library ID: object"),
        ("data_library", "nested linked-library ID: object data"),
        ("material_library", "nested linked-library ID: material"),
        ("tree_library", "nested linked-library ID: material node tree"),
        ("object_override", "library override ID: object"),
        ("new_action", "contains animations"),
    ),
)
def test_static_external_source_rejects_nondeterministic_blender_state(case: str, error: str) -> None:
    obj, material, tree = _static_mesh_fixture()
    new_actions = []
    if case == "modifier":
        obj.modifiers.append(object())
    elif case == "constraint":
        obj.constraints.append(object())
    elif case == "object_driver":
        obj.animation_data = SimpleNamespace(drivers=[object()])
    elif case == "data_driver":
        obj.data.animation_data = SimpleNamespace(drivers=[object()])
    elif case == "material_driver":
        material.animation_data = SimpleNamespace(drivers=[object()])
    elif case == "node_driver":
        tree.animation_data = SimpleNamespace(drivers=[object()])
    elif case == "rotation":
        obj.rotation_mode = "QUATERNION"
    elif case == "delta":
        obj.delta_scale = (1.0, 2.0, 1.0)
    elif case == "drawable":
        obj.type = "CURVE"
    elif case == "instancing":
        obj.instance_type = "COLLECTION"
    elif case == "shape_keys":
        obj.data.shape_keys = object()
    elif case == "rigid_body":
        obj.rigid_body = object()
    elif case == "rigid_constraint":
        obj.rigid_body_constraint = object()
    elif case == "soft_body":
        obj.soft_body = object()
    elif case == "particles":
        obj.particle_systems.append(object())
    elif case == "force_field":
        obj.field.type = "FORCE"
    elif case == "object_library":
        obj.library = object()
    elif case == "data_library":
        obj.data.library = object()
    elif case == "material_library":
        material.library = object()
    elif case == "tree_library":
        tree.library = object()
    elif case == "object_override":
        obj.override_library = object()
    elif case == "new_action":
        new_actions.append(object())
    with pytest.raises(RuntimeError, match=error):
        _validate_static_source(SimpleNamespace(), [obj], new_actions)


def test_static_external_source_accepts_only_plain_static_mesh_and_local_helper_parent() -> None:
    obj, _material, _tree = _static_mesh_fixture()
    helper = SimpleNamespace(
        name="Helper",
        type="EMPTY",
        library=None,
        modifiers=[],
        constraints=[],
        rigid_body=None,
        rigid_body_constraint=None,
        soft_body=None,
        particle_systems=[],
        field=SimpleNamespace(type="NONE"),
        instance_type="NONE",
        instance_collection=None,
        rotation_mode="XYZ",
        delta_location=(0.0, 0.0, 0.0),
        delta_rotation_euler=(0.0, 0.0, 0.0),
        delta_scale=(1.0, 1.0, 1.0),
        parent=None,
        animation_data=None,
        data=None,
        material_slots=[],
    )
    obj.parent = helper
    assert _validate_static_source(SimpleNamespace(), [obj, helper], []) == [obj]
    obj.parent = SimpleNamespace()
    with pytest.raises(RuntimeError, match="parent outside"):
        _validate_static_source(SimpleNamespace(), [obj, helper], [])


def test_normalized_external_mesh_state_rejects_parent_and_matrix_residue() -> None:
    identity = (
        (1.0, 0.0, 0.0, 0.0),
        (0.0, 1.0, 0.0, 0.0),
        (0.0, 0.0, 1.0, 0.0),
        (0.0, 0.0, 0.0, 1.0),
    )
    obj = SimpleNamespace(
        name="NormalizedMesh",
        type="MESH",
        parent=None,
        modifiers=[],
        constraints=[],
        animation_data=None,
        data=SimpleNamespace(animation_data=None, shape_keys=None),
        rotation_mode="XYZ",
        location=(0.0, 0.0, 0.0),
        rotation_euler=(0.0, 0.0, 0.0),
        scale=(1.0, 1.0, 1.0),
        delta_location=(0.0, 0.0, 0.0),
        delta_rotation_euler=(0.0, 0.0, 0.0),
        delta_scale=(1.0, 1.0, 1.0),
        matrix_basis=identity,
        matrix_local=identity,
        matrix_parent_inverse=identity,
        matrix_world=identity,
    )
    _validate_normalized_mesh_state(obj)
    obj.matrix_local = (
        (1.0, 0.0, 0.0, 0.25),
        *identity[1:],
    )
    with pytest.raises(RuntimeError, match="matrix_local influence"):
        _validate_normalized_mesh_state(obj)
    obj.matrix_local = identity
    obj.parent = object()
    with pytest.raises(RuntimeError, match="parent helper"):
        _validate_normalized_mesh_state(obj)


def test_combined_bounds_use_transformed_vertices_and_survive_transform_bake() -> None:
    class RotateZ45:
        def __matmul__(self, value):
            x, y, z = value
            scale = 2**-0.5
            return ((x - y) * scale, (x + y) * scale, z)

    class Identity:
        def __matmul__(self, value):
            return value

    local = [(0.0, 0.0, 0.0), (2.0, 0.0, 0.0), (2.0, 1.0, 1.0), (0.0, 1.0, 1.0)]
    source = SimpleNamespace(
        matrix_world=RotateZ45(),
        data=SimpleNamespace(vertices=[SimpleNamespace(co=value) for value in local]),
        # A transformed local AABB would use these corners and is deliberately
        # a loose/poisoned envelope.  Exact bounds must ignore it.
        bound_box=[(-100.0, -100.0, -100.0), (100.0, 100.0, 100.0)],
    )
    before = _combined_bounds(None, [source])
    baked = [source.matrix_world @ value for value in local]
    normalized = SimpleNamespace(
        matrix_world=Identity(),
        data=SimpleNamespace(vertices=[SimpleNamespace(co=value) for value in baked]),
        bound_box=[],
    )
    assert _combined_bounds(None, [normalized]) == before
    assert tuple(before[1][index] - before[0][index] for index in range(3)) == pytest.approx(
        (3 * 2**-0.5, 3 * 2**-0.5, 1.0)
    )


def test_staticization_bounds_derive_dimensions_from_persistent_endpoints() -> None:
    # The exact endpoint difference rounds to 0.166679, while the difference
    # between independently persisted six-decimal endpoints is 0.166678.
    # Deriving dimensions after endpoint normalization prevents that one-micro
    # discrepancy from invalidating a receipt after its JSON round trip.
    minimum = (-0.0833394, -0.0000004, 0.0)
    maximum = (0.0833394, 0.0000004, 1.0)
    expected = {
        "minimum": [-0.083339, 0.0, 0.0],
        "maximum": [0.083339, 0.0, 1.0],
        "dimensions": [0.166678, 0.0, 1.0],
    }
    combined = _bounds_record_from_minimum_maximum(minimum, maximum)
    mesh = SimpleNamespace(
        vertices=[
            SimpleNamespace(co=minimum),
            SimpleNamespace(co=maximum),
        ]
    )
    per_mesh = _mesh_bounds_record(mesh)
    assert combined == expected
    assert per_mesh == expected
    for record in (combined, per_mesh):
        persisted = json.loads(canonical_json_bytes(record))
        assert persisted == record
        _validate_staticization_bounds(persisted, label="round-trip bounds")


def test_source_custom_properties_are_removed_with_digest_only_receipts() -> None:
    material = {
        "scalar": 1.234567891,
        "yp": {"private_note": "source-only", "weights": [1, 2.5, False]},
    }
    rows = _remove_source_custom_properties(material)
    assert material == {}
    assert [row["property_name"] for row in rows] == ["scalar", "yp"]
    assert [row["value_type"] for row in rows] == ["number", "mapping"]
    assert all(len(row["value_sha256"]) == 64 for row in rows)
    assert "source-only" not in json.dumps(rows)


def test_metric_box_uv_scales_in_metres_and_uses_deterministic_axis_ties() -> None:
    assert _metric_box_uv((0.0, 0.0, 0.0), (0.0, 0.0, 1.0)) == (0.0, 0.0)
    assert _metric_box_uv((2.0, 3.0, 0.5), (0.0, 0.0, 1.0)) == (2.0, 3.0)
    assert _metric_box_uv((2.0, 3.0, 0.5), (0.0, 0.0, 1.0), meters_per_tile=0.5) == (
        4.0,
        6.0,
    )
    assert _metric_box_uv((2.0, 3.0, 0.5), (1.0, 1.0, 0.0)) == (-3.0, 0.5)


def test_metric_box_uv_replaces_primitive_uv_layer_with_metric_coordinates() -> None:
    class UVLayers(list):
        active = None

        def new(self, *, name: str):
            layer = SimpleNamespace(
                name=name,
                data=[SimpleNamespace(uv=None) for _ in range(4)],
                active_render=False,
            )
            self.append(layer)
            return layer

    uv_layers = UVLayers([SimpleNamespace(name="UVMap")])
    mesh = SimpleNamespace(
        uv_layers=uv_layers,
        polygons=[SimpleNamespace(normal=(0.0, 0.0, 1.0), loop_indices=(0, 1, 2, 3))],
        loops=[SimpleNamespace(vertex_index=index) for index in range(4)],
        vertices=[
            SimpleNamespace(co=(0.0, 0.0, 0.0)),
            SimpleNamespace(co=(2.0, 0.0, 0.0)),
            SimpleNamespace(co=(2.0, 3.0, 0.0)),
            SimpleNamespace(co=(0.0, 3.0, 0.0)),
        ],
        update=lambda: None,
    )

    class FakeObject(dict):
        data = mesh

    obj = FakeObject()
    _apply_metric_box_uv(obj)
    assert [item.uv for item in uv_layers[0].data] == [
        (0.0, 0.0),
        (2.0, 0.0),
        (2.0, 3.0),
        (0.0, 3.0),
    ]
    assert uv_layers[0].name == "VISTA_MetricUV"
    assert uv_layers[0].active_render is True
    assert obj["vista_uv_mapping"] == "metric_box_v1"


def test_authored_material_provenance_must_match_actual_mesh_use() -> None:
    oak_id = "visual.material.white_oak_veneer"
    wool_id = "visual.material.poly_wool_herringbone"
    oak = _FakeMaterial("Oak", source=oak_id)
    wool = _FakeMaterial("Wool", source=wool_id)
    mesh = SimpleNamespace(
        name="SofaPart",
        data=SimpleNamespace(polygons=[SimpleNamespace(material_index=0), SimpleNamespace(material_index=1)]),
        material_slots=[SimpleNamespace(material=oak), SimpleNamespace(material=wool)],
    )
    assert _validate_authored_recipe_material_use(
        "contemporary_sofa_v1",
        [mesh],
        {oak_id: oak, wool_id: wool},
    ) == tuple(sorted((oak_id, wool_id)))

    impostor = _FakeMaterial("Impostor", source=oak_id)
    mesh.material_slots[0].material = impostor
    with pytest.raises(RuntimeError, match="wrong datablock"):
        _validate_authored_recipe_material_use(
            "contemporary_sofa_v1",
            [mesh],
            {oak_id: oak, wool_id: wool},
        )


def _external_content() -> dict:
    return {
        "schema_version": PLACEMENT_SCHEMA_VERSION,
        "normalization_policy": NORMALIZATION_POLICY,
        "acquisition_receipt": {
            "provider": "poly_haven",
            "receipt_schema_version": ACQUISITION_RECEIPT_SCHEMA,
            "receipt_digest": "1" * 64,
            "receipt_file_sha256": "2" * 64,
            "acquisition_manifest_sha256": "3" * 64,
        },
        "placement_manifest_sha256": "4" * 64,
        "placement_plan_sha256": "5" * 64,
        "semantic_target_ids": ["home.r1/room.entry_hall/entity.shoe_bench.01"],
        "dressing_ids": ["dress.entry.boots"],
        "asset_sources": [
            {
                "logical_asset_id": "visual.dressing.entry.boots",
                "asset_id": "boots",
                "asset_type": "model",
                "resolution": "2k",
                "provider_files_hash": "a" * 40,
                "source_tree_sha256": "6" * 64,
                "files": [
                    {
                        "relative_path": "boots.blend",
                        "size_bytes": 10,
                        "sha256": "7" * 64,
                        "texture_semantics": [],
                        "dimensions_px": None,
                    }
                ],
            }
        ],
    }


def test_v2_bundle_allows_shared_packed_textures_but_exact_material_names() -> None:
    house = json.loads(HOUSE_PATH.read_text(encoding="utf-8"))
    profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    plan = build_forge_plan(house, profile)
    room = next(item for item in plan.rooms if item.kind == "entry_hall")
    record = ue_bundle_contract(plan, room)
    record["external_content"] = _external_content()
    record.update(
        {
            "sha256": "8" * 64,
            "size_bytes": 100,
            "mesh_count": 1,
            "material_count": 4,
            "pbr_complete_material_count": 4,
            "texture_count": 3,
            "material_ids": ["r2.mat.a", "r2.mat.b", "r2.mat.c", "r2.mat.d"],
        }
    )
    assert _validate_bundle_record(record) is record
    metadata = {
        "vista_bundle_contract": "one_room_one_mesh_v2",
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
        "vista_expected_world_transform_cm_json": json.dumps(record["expected_world_transform_cm"]),
        "vista_material_ids_json": json.dumps(record["material_ids"]),
        "vista_external_content_json": json.dumps(record["external_content"]),
    }
    inspection = {
        "bundle_metadata": metadata,
        "sha256": record["sha256"],
        "size_bytes": record["size_bytes"],
        "mesh_count": 1,
        "mesh_node_count": 1,
        "bundle_node_count": 1,
        "bundle_root_is_identity": True,
        "material_count": 4,
        "material_names": list(record["material_ids"]),
        "pbr_complete_material_count": 4,
        "texture_count": 3,
        "camera_count": 0,
        "light_count": 0,
    }
    _validate_bundle_glb(record, inspection)
    inspection["material_names"] = ["r2.mat.a", "r2.mat.b", "r2.mat.c", "wrong"]
    with pytest.raises(ForgeInputError, match="structure differs"):
        _validate_bundle_glb(record, inspection)


def test_no_external_v1_path_is_byte_stable_and_runtime_source_is_fail_closed() -> None:
    house = json.loads(HOUSE_PATH.read_text(encoding="utf-8"))
    profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    plan = build_forge_plan(house, profile)
    manifest_bytes = canonical_json_bytes(normalized_manifest(plan, texture_size_px=512))
    # The byte lock advances with the deterministic entry-millwork geometry;
    # the v1 schema and checked-in profile bytes remain unchanged.
    assert plan.content_digest == "e357358eaff879f4de578fa14412f0fe715e9c89e80117b5b946b81eecfd3d13"
    assert hashlib.sha256(canonical_json_bytes(plan)).hexdigest() == "2bcef9c03707d6c42772748e7db21cf20de4cbc180d82cfe4f07cc000b633fac"
    assert hashlib.sha256(manifest_bytes).hexdigest() == "848ed9134b11f0731c7fd704bb6d6fddfe8b5a38a3c6cf705d477cdecb79535f"
    assert len(manifest_bytes) == 130171

    import tools.blender.vista_playable_home_realism.external_assets as runtime

    source = Path(runtime.__file__).read_text(encoding="utf-8")
    for required in (
        'obj.type in {"ARMATURE", "CAMERA", "LIGHT"}',
        'getattr(obj.data, "shape_keys", None)',
        "external source contains animations",
        "measured normalized placement bounds differ from plan",
        "seat_cushion_",
        "back_cushion_",
    ):
        assert required in source


def test_checked_in_manifest_uses_acquired_assets_without_baking_movable_targets() -> None:
    payload = json.loads(PLACEMENT_PATH.read_text(encoding="utf-8"))
    body = {key: payload[key] for key in payload if key != "content_digest"}
    assert payload["content_digest"] == content_digest(body)
    assert len(payload["placements"]) == 22
    assert sum(item["placement_kind"] == "semantic_fixed" for item in payload["placements"]) == 5
    assert {
        "visual.dressing.kitchen.wooden_plate",
        "visual.dressing.kitchen.wooden_spoon",
    }.issubset({item["source_logical_asset_id"] for item in payload["placements"]})
    serialized = json.dumps(payload)
    for forbidden in ("entity.keys", "entity.coffee_cup", "entity.resident", "entity.exit_door"):
        assert forbidden not in serialized
