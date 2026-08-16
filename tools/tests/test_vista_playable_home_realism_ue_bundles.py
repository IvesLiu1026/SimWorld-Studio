from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]

from tools.blender.vista_playable_home_realism.architecture import build_forge_plan
from tools.blender.vista_playable_home_realism.config import (
    ForgeInputError,
    canonical_json_bytes,
    sha256_file,
)
from tools.blender.vista_playable_home_realism.export import (
    artifact_receipt,
    normalized_manifest,
    ue_bundle_contract,
)
from tools.blender.vista_playable_home_realism.inspect import (
    GLB_JSON_CHUNK,
    GLB_MAGIC,
    inspect_glb,
    inspect_output,
)


HOUSE_PATH = ROOT / "world_packs/vista_playable_home_r1/house.json"
PROFILE_PATH = (
    ROOT
    / "world_packs/vista_playable_home_r1/visual_profiles/realistic_interior_r2.json"
)


@pytest.fixture()
def plan():
    house = json.loads(HOUSE_PATH.read_text(encoding="utf-8"))
    profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    return build_forge_plan(house, profile)


def _bundle_glb_bytes(contract: dict) -> bytes:
    material_count = len(contract["material_ids"])
    extras = {
        "vista_bundle_contract": "one_room_one_mesh_v1",
        "vista_artifact_id": contract["artifact_id"],
        "vista_target_asset_id": contract["target_asset_id"],
        "vista_room_id": contract["room_id"],
        "vista_room_kind": contract["room_kind"],
        "vista_root_transform_policy": contract["root_transform_policy"],
        "vista_expected_world_transform_cm_json": json.dumps(
            contract["expected_world_transform_cm"], sort_keys=True, separators=(",", ":")
        ),
        "vista_semantic_policy": contract["semantic_policy"],
        "vista_collision_policy": contract["collision_policy"],
        "vista_unreal_collision_profile": contract["unreal_collision_profile"],
        "vista_material_ids_json": json.dumps(contract["material_ids"], separators=(",", ":")),
        "vista_source_house_sha256": contract["source_hashes"]["house_sha256"],
        "vista_source_visual_profile_sha256": contract["source_hashes"][
            "visual_profile_sha256"
        ],
        "vista_source_forge_plan_sha256": contract["source_hashes"]["forge_plan_sha256"],
    }
    materials = []
    for index, material_id in enumerate(contract["material_ids"]):
        first_texture = index * 3
        materials.append(
            {
                "name": material_id,
                "pbrMetallicRoughness": {
                    "baseColorTexture": {"index": first_texture},
                    "metallicRoughnessTexture": {"index": first_texture + 1},
                },
                "normalTexture": {"index": first_texture + 2},
            }
        )
    document = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"name": contract["artifact_id"], "mesh": 0, "extras": extras}],
        "meshes": [
            {
                "primitives": [
                    {"attributes": {}, "material": index}
                    for index in range(material_count)
                ]
            }
        ],
        "materials": materials,
        "textures": [{"source": index} for index in range(material_count * 3)],
        "images": [{} for _ in range(material_count * 3)],
    }
    chunk = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    chunk += b" " * ((4 - len(chunk) % 4) % 4)
    total = 12 + 8 + len(chunk)
    return (
        struct.pack("<III", GLB_MAGIC, 2, total)
        + struct.pack("<II", len(chunk), GLB_JSON_CHUNK)
        + chunk
    )


def _write_closed_output(root: Path, plan) -> list[dict]:
    root.mkdir()
    records = []
    for room in plan.rooms:
        contract = ue_bundle_contract(plan, room)
        path = root / contract["relative_path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_bundle_glb_bytes(contract))
        material_count = len(contract["material_ids"])
        records.append(
            {
                **contract,
                "sha256": sha256_file(path),
                "size_bytes": path.stat().st_size,
                "mesh_count": 1,
                "material_count": material_count,
                "pbr_complete_material_count": material_count,
                "texture_count": material_count * 3,
            }
        )
    manifest = normalized_manifest(
        plan,
        texture_size_px=64,
        ue_import_bundles=records,
    )
    (root / "normalized-manifest.json").write_bytes(canonical_json_bytes(manifest))
    (root / "artifact-receipt.json").write_bytes(
        canonical_json_bytes(artifact_receipt(records))
    )
    return records


def _mutate_all_bundle_copies(root: Path, mutation) -> None:
    manifest_path = root / "normalized-manifest.json"
    receipt_path = root / "artifact-receipt.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    for collection in (
        manifest["ue_import_bundles"],
        receipt["ue_import_bundles"],
        receipt["artifacts"],
    ):
        for item in collection:
            mutation(item)
    manifest_path.write_bytes(canonical_json_bytes(manifest))
    receipt_path.write_bytes(canonical_json_bytes(receipt))


def test_ue_bundle_contract_is_room_local_closed_and_world_positioned(plan) -> None:
    records = [ue_bundle_contract(plan, room) for room in plan.rooms]
    assert [item["room_kind"] for item in records] == [
        "entry_hall",
        "living_room",
        "kitchen_dining",
    ]
    assert [item["expected_world_transform_cm"]["location_cm"] for item in records] == [
        [0, 0, 0],
        [-400, -200, 0],
        [400, -200, 0],
    ]
    for record in records:
        assert record["bundle_root_transform"] == {
            "location_m": [0, 0, 0],
            "rotation_deg": [0, 0, 0],
            "scale": [1, 1, 1],
        }
        assert record["target_asset_id"] == f"asset.bundle.{record['room_kind']}"
        assert record["unreal_collision_profile"] == "NoCollision"
        assert record["semantic_policy"] == "presentation_only_preserve_r1_authority"
        assert len(record["material_ids"]) >= 5
        assert all(len(value) == 64 for value in record["source_hashes"].values())


def test_closed_output_cross_checks_manifest_receipt_and_actual_glbs(plan, tmp_path: Path) -> None:
    root = tmp_path / "valid"
    records = _write_closed_output(root, plan)
    inspection = inspect_output(root)
    bundles = [item for item in inspection["glbs"] if item["bundle_node_count"] == 1]
    assert len(bundles) == 3
    assert all(item["mesh_count"] == item["mesh_node_count"] == 1 for item in bundles)
    assert all(item["bundle_root_is_identity"] is True for item in bundles)
    assert all(item["camera_count"] == item["light_count"] == 0 for item in bundles)
    assert all(
        item["material_count"] == item["pbr_complete_material_count"] >= 5
        for item in bundles
    )
    for record in records:
        path = root / record["relative_path"]
        actual = inspect_glb(path)
        assert actual["sha256"] == record["sha256"]
        assert actual["texture_count"] == record["texture_count"]


def test_bundle_arrays_must_exist_and_match(plan, tmp_path: Path) -> None:
    root = tmp_path / "missing-array"
    _write_closed_output(root, plan)
    receipt_path = root / "artifact-receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt.pop("ue_import_bundles")
    receipt_path.write_bytes(canonical_json_bytes(receipt))
    with pytest.raises(ForgeInputError, match="arrays differ"):
        inspect_output(root)


@pytest.mark.parametrize(
    ("name", "mutation", "error"),
    [
        (
            "path-escape",
            lambda item: item.__setitem__("relative_path", "../private.glb"),
            "artifact identity",
        ),
        (
            "hash-drift",
            lambda item: item.__setitem__("sha256", "0" * 64),
            "structure differs",
        ),
        (
            "collision-drift",
            lambda item: item.__setitem__("unreal_collision_profile", "BlockAll"),
            "presentation policy",
        ),
        (
            "mesh-count-drift",
            lambda item: item.__setitem__("mesh_count", 2),
            "mesh/material/hash",
        ),
    ],
)
def test_bundle_contract_tampering_fails_closed(
    plan,
    tmp_path: Path,
    name: str,
    mutation,
    error: str,
) -> None:
    root = tmp_path / name
    _write_closed_output(root, plan)
    _mutate_all_bundle_copies(root, mutation)
    with pytest.raises(ForgeInputError, match=error):
        inspect_output(root)


def test_bundle_glb_cannot_drop_pbr_texture_semantics(plan, tmp_path: Path) -> None:
    root = tmp_path / "pbr-drift"
    records = _write_closed_output(root, plan)
    target = root / records[0]["relative_path"]
    raw = target.read_bytes()
    chunk_length, _chunk_type = struct.unpack("<II", raw[12:20])
    document = json.loads(raw[20 : 20 + chunk_length].decode("utf-8").rstrip(" \t\r\n\x00"))
    document["materials"][0].pop("normalTexture")
    chunk = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    chunk += b" " * ((4 - len(chunk) % 4) % 4)
    target.write_bytes(
        struct.pack("<III", GLB_MAGIC, 2, 12 + 8 + len(chunk))
        + struct.pack("<II", len(chunk), GLB_JSON_CHUNK)
        + chunk
    )
    with pytest.raises(ForgeInputError, match="structure differs"):
        inspect_output(root)
