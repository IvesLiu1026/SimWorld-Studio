from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.blender.vista_playable_home import build as blender_build
from tools.blender.vista_playable_home.contract_scene import (
    HousePlanError,
    asset_binding_plan,
    build_contract_plan,
    canonical_json_bytes,
    compose_world_transform,
    load_house,
    normalized_manifest,
    validate_contract_plan,
)


CONTRACT_HOUSE = REPO_ROOT / "world_packs" / "vista_playable_home_r1" / "house.json"


def _house_path() -> Path:
    override = os.environ.get("VISTA_PLAYABLE_HOME_HOUSE")
    candidate = Path(override) if override else CONTRACT_HOUSE
    if not candidate.is_file():
        pytest.skip("T6 depends on the T4 HouseSpec fixture; set VISTA_PLAYABLE_HOME_HOUSE in the child worktree")
    return candidate.resolve()


@pytest.fixture()
def house() -> dict:
    return load_house(_house_path())


def test_contract_semantics_are_preserved_exactly(house: dict) -> None:
    plan = build_contract_plan(house)
    manifest = normalized_manifest(plan)

    assert [room["room_id"] for room in manifest["rooms"]] == [room["room_id"] for room in house["rooms"]]
    assert [portal["portal_id"] for portal in manifest["portals"]] == [portal["portal_id"] for portal in house["portals"]]
    assert [entity["entity_id"] for entity in manifest["entities"]] == [entity["entity_id"] for entity in house["entities"]]

    source_by_id = {entity["entity_id"]: entity for entity in house["entities"]}
    for entity in manifest["entities"]:
        source = source_by_id[entity["entity_id"]]
        assert entity["transform"] == source["transform"]
        assert entity["asset_ref"] == source["asset_ref"]
        assert entity["component_role"] == source["component_role"]
        assert entity["mobility"] == source["mobility"]
        assert entity["collision_policy"] == source["collision_policy"]
        assert entity["affordances"] == source["affordances"]


def test_cross_topology_and_composed_world_transforms(house: dict) -> None:
    room_by_kind = {room["kind"]: room for room in house["rooms"]}
    assert room_by_kind["entry_hall"]["bounds_m"] == {"min_m": [-1.5, -4, 0], "max_m": [1.5, 4, 3]}
    assert room_by_kind["living_room"]["transform"]["location_m"] == [-4, -2, 0]
    assert room_by_kind["kitchen_dining"]["transform"]["location_m"] == [4, -2, 0]
    assert room_by_kind["bedroom"]["transform"]["location_m"] == [-4, 2, 0]
    assert room_by_kind["office"]["transform"]["location_m"] == [4, 2, 0]
    assert room_by_kind["bathroom_laundry"]["transform"]["location_m"] == [0, 6, 0]

    entity_by_id = {entity["entity_id"]: entity for entity in house["entities"]}
    sofa = entity_by_id["home.r1/room.living_room/entity.sofa.01"]
    washer = entity_by_id["home.r1/room.bathroom_laundry/entity.washer.01"]
    assert compose_world_transform(house, sofa)["location_m"] == [-5.6, -0.8, 0.0]
    assert compose_world_transform(house, washer)["location_m"] == [0.85, 7.2, 0.0]

    manifest_by_id = {entity["entity_id"]: entity for entity in normalized_manifest(build_contract_plan(house))["entities"]}
    assert manifest_by_id[sofa["entity_id"]]["world_transform"] == compose_world_transform(house, sofa)
    assert manifest_by_id[washer["entity_id"]]["world_transform"] == compose_world_transform(house, washer)


def test_every_nonbuiltin_asset_has_one_local_glb_binding(house: dict) -> None:
    plan = build_contract_plan(house)
    bindings = asset_binding_plan(plan)
    expected = {item["asset_id"] for item in house["asset_catalog"] if item["source_kind"] != "builtin"}
    assert set(bindings) == expected
    assert len(bindings) == 35
    assert len(bindings["asset.door.interior"]) == 1
    assert bindings["asset.door.interior"][0].endswith("entity.interior_door.01")
    assert len(bindings["asset.bundle.entry_hall"]) == 2
    assert set(bindings["asset.bundle.entry_hall"]) == {
        "home.r1/room.entry_hall/bundle.ceiling",
        "home.r1/room.entry_hall/bundle.room_shell",
    }
    for asset_id in expected:
        assert bindings[asset_id]


def test_room_shell_architecture_is_grid_aligned_and_nonsemantic(house: dict) -> None:
    plan = build_contract_plan(house)
    shells = [node for node in plan.nodes if node.category == "room_shell"]
    assert len(shells) == 6
    assert all(node.semantic_entity_id is None for node in shells)
    assert all(node.collision_policy == "world_static" for node in shells)
    assert all(node.nav_obstacle for node in shells)
    for shell in shells:
        for primitive in shell.primitives:
            if primitive.grid_bounds_m is None:
                continue
            for bound in primitive.grid_bounds_m:
                for value in bound:
                    assert value / 0.1 == pytest.approx(round(value / 0.1))


def test_role_based_collision_and_recognizable_assemblies(house: dict) -> None:
    plan = build_contract_plan(house)
    nodes = {node.semantic_entity_id: node for node in plan.nodes if node.semantic_entity_id}
    assert nodes["home.r1/room.living_room/entity.keys.01"].collision_policy == "pickup_physics"
    assert nodes["home.r1/room.entry_hall/entity.interior_door.01"].collision_policy == "door_dynamic"
    assert nodes["home.r1/room.kitchen_dining/entity.fire_marker.01"].collision_policy == "trigger_only"
    assert nodes["home.r1/room.living_room/entity.sofa.01"].collision_policy == "furniture"
    assert len(nodes["home.r1/room.living_room/entity.sofa.01"].primitives) >= 6
    assert len(nodes["home.r1/room.office/entity.desk.01"].primitives) >= 6
    assert len(nodes["home.r1/room.kitchen_dining/entity.stove.01"].primitives) >= 6
    assert nodes["home.r1/room.entry_hall/entity.resident.01"].primitives == ()


def test_normalized_manifest_and_digest_are_deterministic(house: dict) -> None:
    first = normalized_manifest(build_contract_plan(copy.deepcopy(house)))
    second = normalized_manifest(build_contract_plan(copy.deepcopy(house)))
    assert first == second
    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert len(first["content_digest"]) == 64
    assert first["source_house"]["content_digest"] == house["content_digest"]


def test_tampered_house_digest_fails_closed(house: dict, tmp_path: Path) -> None:
    tampered = copy.deepcopy(house)
    tampered["rooms"][0]["label"] = "Tampered"
    path = tmp_path / "house.json"
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(HousePlanError, match="content_digest mismatch"):
        load_house(path.resolve())


def test_output_root_is_absolute_empty_and_append_only(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="must be absolute"):
        blender_build.prepare_output_root(Path("relative"))
    clean = blender_build.prepare_output_root(tmp_path / "clean")
    assert clean.is_dir()
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "existing").write_text("keep", encoding="utf-8")
    with pytest.raises(RuntimeError, match="non-empty append-only"):
        blender_build.prepare_output_root(occupied)


def test_build_module_is_importable_without_blender(house: dict) -> None:
    plan = build_contract_plan(house)
    validate_contract_plan(plan)
    args = blender_build.parse_blender_args(
        ["--house", str(_house_path()), "--output-root", "/tmp/vista-playable-home-static-test"]
    )
    assert args.house == _house_path()
    assert args.output_root.is_absolute()
    source = Path(blender_build.__file__).read_text(encoding="utf-8")
    assert "import bpy" in source
    assert "one_asset_one_mesh" in source
    assert "export_scene.gltf" in source
    assert "save_as_mainfile" in source
