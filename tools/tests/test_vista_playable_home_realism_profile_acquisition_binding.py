from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from tools.blender.vista_playable_home_realism.architecture import (
    build_external_forge_plan,
    build_forge_plan,
)
from tools.blender.vista_playable_home_realism.config import (
    ForgeInputError,
    content_digest,
)
from tools.blender.vista_playable_home_realism.external_assets import (
    AcquiredAsset,
    AcquiredFile,
    ExternalAssetSet,
)
from tools.blender.vista_playable_home_realism.placement import (
    PLACEMENT_SCHEMA_VERSION,
    placement_manifest_document,
)
from world_packs.vista_playable_home_r1.visual_profiles.contract import (
    content_digest as visual_content_digest,
    seal_document,
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
COFFEE_LOGICAL_ID = "visual.hero.living_coffee_table"
COFFEE_RECEIPT_ID = "source.hero.living_coffee_table"
COFFEE_TARGET_ID = "home.r1/room.living_room/entity.coffee_table.01"
STOVE_LOGICAL_ID = "visual.hero.kitchen_stove"
STOVE_RECEIPT_ID = "source.hero.kitchen_stove"
STOVE_TARGET_ID = "home.r1/room.kitchen_dining/entity.stove.01"


def _profile() -> dict:
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


def _house() -> dict:
    return json.loads(HOUSE_PATH.read_text(encoding="utf-8"))


def _acquired_files(
    logical_id: str,
    resolution: str,
    semantics: tuple[str, ...],
) -> tuple[AcquiredFile, ...]:
    size = 4096 if resolution == "4k" else 2048
    return tuple(
        AcquiredFile(
            relative_path=f"textures/{logical_id.rsplit('.', 1)[-1]}_{semantic}.png",
            size_bytes=24,
            sha256=hashlib.sha256(f"{logical_id}:{semantic}".encode()).hexdigest(),
            semantic=(semantic,),
            dimensions_px=(size, size),
        )
        for semantic in semantics
    )


def _asset(
    *,
    asset_id: str,
    logical_id: str,
    asset_type: str,
    resolution: str,
    provider_hash: str,
    source_tree: str,
    dimensions: tuple[float, float, float] | None,
    semantics: tuple[str, ...] = ("base_color", "normal", "roughness"),
) -> AcquiredAsset:
    return AcquiredAsset(
        asset_id=asset_id,
        logical_asset_id=logical_id,
        asset_type=asset_type,
        room_role="fixture",
        resolution=resolution,
        file_variant="blend" if asset_type == "model" else "pbr_jpg",
        provider_files_hash=provider_hash,
        source_relative_root=f"assets/{asset_id}",
        primary_relative_path=f"assets/{asset_id}/{asset_id}.blend",
        source_tree_sha256=source_tree,
        catalog_dimensions_m=dimensions,
        files=_acquired_files(logical_id, resolution, semantics),
    )


def _asset_set(tmp_path: Path) -> ExternalAssetSet:
    assets = (
        _asset(
            asset_id="white_oak_veneer",
            logical_id="visual.material.white_oak_veneer",
            asset_type="texture",
            resolution="4k",
            provider_hash="1" * 40,
            source_tree="1" * 64,
            dimensions=None,
        ),
        _asset(
            asset_id="poly_wool_herringbone",
            logical_id="visual.material.poly_wool_herringbone",
            asset_type="texture",
            resolution="4k",
            provider_hash="2" * 40,
            source_tree="2" * 64,
            dimensions=None,
        ),
        _asset(
            asset_id="modern_coffee_table_01",
            logical_id=COFFEE_LOGICAL_ID,
            asset_type="model",
            resolution="4k",
            provider_hash="31772c0aab6f930a18de82606146c0a97f08b7d0",
            source_tree="cf5fac22ac00b8725f91ad4565ddaa32dc5f10b213a0938a92de9e2432c1ddfe",
            dimensions=(1.2018300294876099, 0.6000000834465027, 0.38999998569488525),
        ),
        _asset(
            asset_id="electric_stove",
            logical_id=STOVE_LOGICAL_ID,
            asset_type="model",
            resolution="4k",
            provider_hash="750ee10bdfe78eb6b0b620ef7b5a898e436fb696",
            source_tree="c55acbd188af4674ce5c1c8605f2447c5fb830a05b1650b0d03296b419b38795",
            dimensions=(0.5025948286056519, 0.6476211845874786, 0.8586971759796143),
            semantics=("base_color", "metalness", "normal", "opacity", "roughness"),
        ),
        _asset(
            asset_id="rubber_boots",
            logical_id="visual.dressing.entry.rubber_boots",
            asset_type="model",
            resolution="2k",
            provider_hash="3" * 40,
            source_tree="3" * 64,
            dimensions=(0.4, 0.2, 0.4),
        ),
    )
    return ExternalAssetSet(
        root=tmp_path,
        receipt_digest="4" * 64,
        receipt_file_sha256="5" * 64,
        acquisition_manifest_sha256="6" * 64,
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
    materials: tuple[str, ...] = (),
    scale: float = 1,
    dimensions: tuple[float, float, float] | None = None,
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
        "location_offset_m": [0, 0, 0],
        "rotation_offset_deg": [0, 0, 0],
        "uniform_scale": scale,
        "authored_dimensions_m": list(dimensions) if dimensions else None,
    }


def _placement_payload(asset_set: ExternalAssetSet) -> dict:
    rows = [
        _row(
            "hero.entry.shoe_bench",
            "semantic_fixed",
            "entry_hall",
            "shoe_bench",
            "project_authored",
            target="home.r1/room.entry_hall/entity.shoe_bench.01",
            recipe="contemporary_shoe_bench_v1",
            materials=(
                "visual.material.white_oak_veneer",
                "visual.material.poly_wool_herringbone",
            ),
            dimensions=(1, 0.38, 0.55),
        ),
        _row(
            "hero.living.sofa",
            "semantic_fixed",
            "living_room",
            "sofa",
            "project_authored",
            target="home.r1/room.living_room/entity.sofa.01",
            recipe="contemporary_sofa_v1",
            materials=(
                "visual.material.white_oak_veneer",
                "visual.material.poly_wool_herringbone",
            ),
            dimensions=(1.6, 0.82, 0.78),
        ),
        _row(
            "hero.living.coffee_table",
            "semantic_fixed",
            "living_room",
            "coffee_table",
            "external_blend",
            target=COFFEE_TARGET_ID,
            source=COFFEE_LOGICAL_ID,
        ),
        _row(
            "hero.kitchen.stove",
            "semantic_fixed",
            "kitchen_dining",
            "stove",
            "external_blend",
            target=STOVE_TARGET_ID,
            source=STOVE_LOGICAL_ID,
        ),
        _row(
            "hero.kitchen.dining_table",
            "semantic_fixed",
            "kitchen_dining",
            "dining_table",
            "project_authored",
            target="home.r1/room.kitchen_dining/entity.dining_table.01",
            recipe="contemporary_dining_table_v1",
            materials=("visual.material.white_oak_veneer",),
            dimensions=(1.6, 0.9, 0.76),
        ),
        _row(
            "dress.entry.rubber_boots",
            "dressing",
            "entry_hall",
            "shoe",
            "external_blend",
            anchor="home.r1/room.entry_hall/dressing_anchor.shoe_drop",
            support="hero.entry.shoe_bench",
            source="visual.dressing.entry.rubber_boots",
        ),
    ]
    payload = {
        "schema_version": PLACEMENT_SCHEMA_VERSION,
        "placement_id": "fixture.external.profile_binding",
        "acquisition": {
            **asset_set.receipt_reference(),
            "receipt_filename": "acquisition-receipt.json",
        },
        "placements": rows,
    }
    payload["content_digest"] = content_digest(payload)
    return payload


def _redigest_placement(payload: dict) -> dict:
    result = copy.deepcopy(payload)
    result.pop("content_digest", None)
    result["content_digest"] = content_digest(result)
    return result


def _receipt(profile: dict, receipt_id: str) -> dict:
    return next(
        item
        for item in profile["asset_source_receipts"]
        if item["receipt_id"] == receipt_id
    )


def _binding(profile: dict, target_id: str) -> dict:
    return next(
        item
        for item in profile["semantic_visual_bindings"]
        if item["target_entity_id"] == target_id
    )


def _build(
    tmp_path: Path,
    *,
    profile: dict | None = None,
    placement_payload: dict | None = None,
    asset_set: ExternalAssetSet | None = None,
):
    assets = asset_set or _asset_set(tmp_path)
    payload = placement_payload or _placement_payload(assets)
    return build_external_forge_plan(
        _house(),
        profile or _profile(),
        assets,
        placement_manifest_document(payload),
    )


def test_exact_profile_acquisition_binding_builds_and_v1_plan_is_unchanged(
    tmp_path: Path,
) -> None:
    house = _house()
    profile = _profile()
    v1_before = build_forge_plan(house, profile)
    external = _build(tmp_path, profile=profile)
    v1_after = build_forge_plan(house, profile)

    assert v1_after == v1_before
    assert external.external_placement.semantic_target_ids == (
        "home.r1/room.entry_hall/entity.shoe_bench.01",
        "home.r1/room.kitchen_dining/entity.dining_table.01",
        STOVE_TARGET_ID,
        COFFEE_TARGET_ID,
        "home.r1/room.living_room/entity.sofa.01",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("source_kind", "project_authored"),
        ("source_uri", "polyhaven://models/wrong_table"),
        ("source_digest", "0" * 64),
        ("source_version", "files-" + "0" * 40),
        ("logical_asset_id", STOVE_LOGICAL_ID),
    ),
)
def test_rejects_source_receipt_identity_drift(
    tmp_path: Path, field: str, value: str
) -> None:
    profile = _profile()
    _receipt(profile, COFFEE_RECEIPT_ID)[field] = value
    profile = seal_document(profile)

    with pytest.raises(ForgeInputError, match=field):
        _build(tmp_path, profile=profile)


def test_rejects_stale_source_receipt_digest(tmp_path: Path) -> None:
    profile = _profile()
    _receipt(profile, COFFEE_RECEIPT_ID)["receipt_digest"] = "0" * 64
    profile["content_digest"] = visual_content_digest(profile)

    with pytest.raises(ForgeInputError, match="source receipt digest is stale"):
        _build(tmp_path, profile=profile)


def test_rejects_unscaled_or_off_center_metric_bounds(tmp_path: Path) -> None:
    assets = _asset_set(tmp_path)
    placement = _placement_payload(assets)
    coffee = next(
        item
        for item in placement["placements"]
        if item["placement_id"] == "hero.living.coffee_table"
    )
    coffee["uniform_scale"] = 0.5
    placement = _redigest_placement(placement)

    with pytest.raises(ForgeInputError, match="measured scaled bounds"):
        _build(tmp_path, placement_payload=placement, asset_set=assets)

    profile = _profile()
    receipt = _receipt(profile, COFFEE_RECEIPT_ID)
    receipt["metric_bounds_m"] = {
        "min_m": [-0.30045750737190247, -0.15000002086162567, 0],
        "max_m": [0.30045750737190247, 0.15000002086162567, 0.19499999284744263],
    }
    profile = seal_document(profile)
    plan = _build(
        tmp_path,
        profile=profile,
        placement_payload=placement,
        asset_set=assets,
    )
    coffee_plan = next(
        item
        for item in plan.external_placement.placements
        if item.placement_id == "hero.living.coffee_table"
    )
    assert coffee_plan.uniform_scale == 0.5


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("license_id", "LicenseRef-unknown"),
        ("license_url", "https://example.invalid/license"),
        ("entitlement_status", "unverified"),
        ("commercial_use", "unknown"),
        ("redistribution_restriction", "unknown"),
    ),
)
def test_rejects_non_verified_cc0_license(
    tmp_path: Path, field: str, value: str
) -> None:
    profile = _profile()
    _receipt(profile, COFFEE_RECEIPT_ID)["license"][field] = value
    profile = seal_document(profile)

    with pytest.raises(ForgeInputError, match="verified CC0"):
        _build(tmp_path, profile=profile)


@pytest.mark.parametrize(
    "drift",
    (
        "texture_semantics",
        "texture_count",
        "minimum_texture_size_px",
        "all_primitives_material_bound",
        "blend_mode",
    ),
)
def test_rejects_material_inventory_drift_from_acquired_images(
    tmp_path: Path, drift: str
) -> None:
    profile = _profile()
    inventory = _receipt(profile, COFFEE_RECEIPT_ID)["material_inventory"]
    slot = inventory["slots"][0]
    if drift == "texture_semantics":
        slot["texture_semantics"] = ["base_color", "normal"]
    elif drift == "texture_count":
        inventory["texture_count"] = 99
    elif drift == "minimum_texture_size_px":
        slot["minimum_texture_size_px"] = 2048
    elif drift == "all_primitives_material_bound":
        inventory["all_primitives_material_bound"] = False
    else:
        slot["blend_mode"] = "masked"
    profile = seal_document(profile)

    with pytest.raises(ForgeInputError, match="material (slot|inventory)"):
        _build(tmp_path, profile=profile)


def test_opacity_requires_masked_material_inventory(tmp_path: Path) -> None:
    profile = _profile()
    stove_inventory = _receipt(profile, STOVE_RECEIPT_ID)["material_inventory"]
    stove_inventory["slots"][0]["blend_mode"] = "opaque"
    profile = seal_document(profile)

    with pytest.raises(ForgeInputError, match="material slot differs"):
        _build(tmp_path, profile=profile)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("nanite", "eligible_static_opaque"),
        ("mobility", "movable"),
        ("lod_policy", "nanite"),
        ("collision_policy", "generated_complex"),
    ),
)
def test_rejects_non_conservative_external_import_policy(
    tmp_path: Path, field: str, value: str
) -> None:
    profile = _profile()
    _receipt(profile, COFFEE_RECEIPT_ID)["import_policy"][field] = value
    profile = seal_document(profile)

    with pytest.raises(ForgeInputError, match="non-conservative import policy"):
        _build(tmp_path, profile=profile)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("collision_policy", "inherit_source"),
        ("semantic_authority", "replace_parent"),
    ),
)
def test_rejects_binding_that_replaces_r1_authority_or_collision(
    tmp_path: Path, field: str, value: str
) -> None:
    profile = _profile()
    _binding(profile, COFFEE_TARGET_ID)[field] = value
    profile = seal_document(profile)

    with pytest.raises(ForgeInputError, match="preserve r1 semantics and collision"):
        _build(tmp_path, profile=profile)


def test_rejects_binding_logical_asset_and_source_receipt_drift(tmp_path: Path) -> None:
    profile = _profile()
    _binding(profile, COFFEE_TARGET_ID)["logical_asset_id"] = STOVE_LOGICAL_ID
    profile = seal_document(profile)
    with pytest.raises(ForgeInputError, match="logical asset differs from placement"):
        _build(tmp_path, profile=profile)

    profile = _profile()
    _binding(profile, COFFEE_TARGET_ID)["source_receipt_id"] = STOVE_RECEIPT_ID
    profile = seal_document(profile)
    with pytest.raises(ForgeInputError, match="source receipt is absent or reused"):
        _build(tmp_path, profile=profile)


def test_rejects_orphan_and_duplicate_profile_identities(tmp_path: Path) -> None:
    profile = _profile()
    profile["semantic_visual_bindings"] = [
        item
        for item in profile["semantic_visual_bindings"]
        if item["target_entity_id"] != COFFEE_TARGET_ID
    ]
    profile = seal_document(profile)
    with pytest.raises(ForgeInputError, match="has no VisualProfile binding"):
        _build(tmp_path, profile=profile)

    profile = _profile()
    duplicate_binding = copy.deepcopy(_binding(profile, COFFEE_TARGET_ID))
    duplicate_binding["binding_id"] = "binding.duplicate.coffee_table"
    profile["semantic_visual_bindings"].append(duplicate_binding)
    profile = seal_document(profile)
    with pytest.raises(ForgeInputError, match="target_entity_id identities are duplicated"):
        _build(tmp_path, profile=profile)

    profile = _profile()
    profile["asset_source_receipts"].append(
        copy.deepcopy(_receipt(profile, COFFEE_RECEIPT_ID))
    )
    profile = seal_document(profile)
    with pytest.raises(ForgeInputError, match="receipt_id identities are duplicated"):
        _build(tmp_path, profile=profile)


def test_rejects_reused_acquired_model_across_semantic_heroes(tmp_path: Path) -> None:
    assets = _asset_set(tmp_path)
    placement = _placement_payload(assets)
    stove = next(
        item
        for item in placement["placements"]
        if item["placement_id"] == "hero.kitchen.stove"
    )
    stove["source_logical_asset_id"] = COFFEE_LOGICAL_ID
    placement = _redigest_placement(placement)

    with pytest.raises(ForgeInputError, match="unique target and source identities"):
        _build(tmp_path, placement_payload=placement, asset_set=assets)


def test_cross_contract_drift_does_not_change_or_gate_v1_plan(tmp_path: Path) -> None:
    profile = _profile()
    _receipt(profile, COFFEE_RECEIPT_ID)["source_digest"] = "0" * 64
    profile = seal_document(profile)

    v1 = build_forge_plan(_house(), profile)
    assert v1.schema_version == "simworld.vista.playable-home-realism-forge/v1"
    with pytest.raises(ForgeInputError, match="source_digest differs from acquisition"):
        _build(tmp_path, profile=profile)
