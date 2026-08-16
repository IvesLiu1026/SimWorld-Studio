from __future__ import annotations

import copy
import hashlib
import json
import struct
from pathlib import Path

import pytest


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
    AcquiredAsset,
    AcquiredFile,
    ExternalAssetSet,
    _canonical_acquisition_json,
    load_external_asset_set,
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


def _sources(
    logical_id: str,
    asset_type: str,
    resolution: str,
    dimensions,
    *,
    asset_id: str | None = None,
    provider_hash: str | None = None,
    source_tree: str | None = None,
    semantics=("base_color", "normal", "roughness"),
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
    files = (primary_file, *texture_files) if asset_type == "model" else texture_files
    return AcquiredAsset(
        asset_id=resolved_asset_id,
        logical_asset_id=logical_id,
        asset_type=asset_type,
        room_role="fixture",
        resolution=resolution,
        file_variant="blend" if asset_type == "model" else "pbr_jpg",
        provider_files_hash=provider_hash or hashlib.sha1(logical_id.encode()).hexdigest(),
        source_relative_root=f"assets/{resolved_asset_id}",
        primary_relative_path=(
            f"assets/{resolved_asset_id}/fixture.blend"
            if asset_type == "model"
            else f"assets/{resolved_asset_id}/{texture_files[0].relative_path}"
        ),
        source_tree_sha256=source_tree or hashlib.sha256(f"tree:{logical_id}".encode()).hexdigest(),
        catalog_dimensions_m=dimensions,
        files=files,
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
            source_tree="cf5fac22ac00b8725f91ad4565ddaa32dc5f10b213a0938a92de9e2432c1ddfe",
        ),
        _sources(
            "visual.hero.kitchen_stove",
            "model",
            "4k",
            (0.5025948286056519, 0.6476211845874786, 0.8586971759796143),
            asset_id="electric_stove",
            provider_hash="750ee10bdfe78eb6b0b620ef7b5a898e436fb696",
            source_tree="c55acbd188af4674ce5c1c8605f2447c5fb830a05b1650b0d03296b419b38795",
            semantics=("base_color", "metalness", "normal", "opacity", "roughness"),
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
    assert plan.content_digest == "56d07c9664dfe6d054124ca08acac007f6aef7338f0fdbaa81377fcaff34f008"
    assert hashlib.sha256(canonical_json_bytes(plan)).hexdigest() == "8f9ef316cbc5a20fd1ccb5413eac704ef3d39a58bf48bd2d3cca995491c26b13"
    assert hashlib.sha256(manifest_bytes).hexdigest() == "9e11ad06fe7f7bff581a097e451eba02e500ae7c8af386fb79ae52fcc7b5ac8a"
    assert len(manifest_bytes) == 116578

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
