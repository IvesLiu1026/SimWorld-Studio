from __future__ import annotations

import copy
import dataclasses
import json
import py_compile
import struct
from pathlib import Path

import pytest

from tools.tests.test_vista_playable_home_build_home import Fixture as BuildFixture
from tools.ue.vista_playable_home import build_home, planning


ROOT = Path(__file__).resolve().parents[2]
PROFILE_PATH = (
    ROOT
    / "world_packs"
    / "vista_playable_home_r1"
    / "visual_profiles"
    / "realistic_interior_r2.json"
)


def _write_glb(path: Path, document: dict) -> Path:
    payload = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    payload += b" " * ((-len(payload)) % 4)
    raw = (
        struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(payload))
        + struct.pack("<II", len(payload), 0x4E4F534A)
        + payload
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    path.chmod(0o600)
    return path


def _glb_document(record: dict, *, default_material: bool = False) -> dict:
    materials = []
    for index in range(2):
        materials.append({
            "name": "DefaultMaterial" if default_material and index == 0 else f"r2.synthetic.{index}",
            "pbrMetallicRoughness": {
                "baseColorTexture": {"index": index * 3},
                "metallicRoughnessTexture": {"index": index * 3 + 1},
            },
            "normalTexture": {"index": index * 3 + 2},
        })
    extras = {
        "vista_bundle_contract": "one_room_one_mesh_v1",
        "vista_artifact_id": record["artifact_id"],
        "vista_target_asset_id": record["target_asset_id"],
        "vista_room_id": record["room_id"],
        "vista_room_kind": record["room_kind"],
        "vista_root_transform_policy": record["root_transform_policy"],
        "vista_expected_world_transform_cm_json": json.dumps(
            record["expected_world_transform_cm"],
            sort_keys=True,
            separators=(",", ":"),
        ),
        "vista_semantic_policy": record["semantic_policy"],
        "vista_collision_policy": record["collision_policy"],
        "vista_unreal_collision_profile": record["unreal_collision_profile"],
        "vista_material_ids_json": json.dumps(
            record["material_ids"], separators=(",", ":")
        ),
        "vista_source_house_sha256": record["source_hashes"]["house_sha256"],
        "vista_source_visual_profile_sha256": record["source_hashes"]["visual_profile_sha256"],
        "vista_source_forge_plan_sha256": record["source_hashes"]["forge_plan_sha256"],
    }
    return {
        "asset": {"version": "2.0", "generator": "focused-test"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"name": "VISTA_TestBundle", "mesh": 0, "extras": extras}],
        "meshes": [{
            "name": "VISTA_TestBundle_Mesh",
            "primitives": [
                {"attributes": {}, "material": 0},
                {"attributes": {}, "material": 1},
            ],
        }],
        "materials": materials,
        "textures": [{"source": index} for index in range(6)],
        "images": [{"name": f"texture-{index}"} for index in range(6)],
    }


def _presentation_contracts(
    root: Path,
    fixture: BuildFixture,
    *,
    default_material_kind: str | None = None,
) -> tuple[Path, Path, dict, dict]:
    profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    forge_sha = "f" * 64
    bundles = []
    for room in fixture.plan["rooms"]:
        kind = room["kind"]
        if kind not in planning.PRESENTATION_ROOM_KINDS:
            continue
        record = {
            "artifact_id": f"ue_bundle.room.{kind}",
            "artifact_kind": planning.PRESENTATION_ARTIFACT_KIND,
            "target_asset_id": f"asset.bundle.{kind}",
            "room_id": room["room_id"],
            "room_kind": kind,
            "relative_path": f"ue_import_bundles/{kind}_presentation_bundle.glb",
            "media_type": "model/gltf-binary",
            "sha256": "0" * 64,
            "size_bytes": 1,
            "mesh_count": 1,
            "material_count": 2,
            "pbr_complete_material_count": 2,
            "texture_count": 6,
            "material_ids": [f"r2.{kind}.a", f"r2.{kind}.b"],
            "expected_world_transform_cm": copy.deepcopy(room["world_transform_cm"]),
            "bundle_root_transform": {
                "location_m": [0, 0, 0],
                "rotation_deg": [0, 0, 0],
                "scale": [1, 1, 1],
            },
            "root_transform_policy": planning.PRESENTATION_ROOT_TRANSFORM_POLICY,
            "semantic_policy": planning.PRESENTATION_SEMANTIC_POLICY,
            "collision_policy": planning.PRESENTATION_COLLISION_POLICY,
            "unreal_collision_profile": planning.PRESENTATION_UNREAL_COLLISION_PROFILE,
            "cameras_exported": False,
            "lights_exported": False,
            "source_hashes": {
                "house_sha256": fixture.plan["house"]["content_digest"],
                "visual_profile_sha256": profile["content_digest"],
                "forge_plan_sha256": forge_sha,
            },
        }
        path = root / record["relative_path"]
        _write_glb(
            path,
            _glb_document(
                record,
                default_material=default_material_kind == kind,
            ),
        )
        record["sha256"] = build_home.sha256_file(path)
        record["size_bytes"] = path.stat().st_size
        bundles.append(record)
    manifest = {
        "schema_version": build_home.PRESENTATION_FORGE_SCHEMA,
        "house_revision": fixture.plan["house"]["revision"],
        "visual_profile_id": profile["visual_profile_id"],
        "source_house_digest": fixture.plan["house"]["content_digest"],
        "source_profile_digest": profile["content_digest"],
        "forge_plan_digest": forge_sha,
        "ue_import_bundles": bundles,
    }
    receipt = {
        "schema_version": build_home.PRESENTATION_ARTIFACT_RECEIPT_SCHEMA,
        "artifacts": copy.deepcopy(bundles),
        "ue_import_bundles": copy.deepcopy(bundles),
    }
    manifest_path = root / "normalized-manifest.json"
    receipt_path = root / "artifact-receipt.json"
    manifest_path.write_bytes(build_home.canonical_json(manifest))
    receipt_path.write_bytes(build_home.canonical_json(receipt))
    return manifest_path, receipt_path, manifest, receipt


def _presentation_config(
    fixture: BuildFixture,
    manifest_path: Path,
    receipt_path: Path,
) -> build_home.BuildConfig:
    return dataclasses.replace(
        fixture.config(),
        visual_profile=PROFILE_PATH,
        visual_profile_sha256=build_home.sha256_file(PROFILE_PATH),
        presentation_manifest=manifest_path,
        presentation_manifest_sha256=build_home.sha256_file(manifest_path),
        presentation_artifact_receipt=receipt_path,
        presentation_artifact_receipt_sha256=build_home.sha256_file(receipt_path),
    )


def test_presentation_contracts_compile_three_source_pinned_operations(
    tmp_path: Path,
) -> None:
    fixture = BuildFixture(tmp_path)
    manifest_path, receipt_path, _manifest, _receipt = _presentation_contracts(
        tmp_path / "inputs" / "presentation", fixture
    )

    planned = build_home.plan_build(
        _presentation_config(fixture, manifest_path, receipt_path)
    )

    assert planned.presentation is not None
    assert len(planned.presentation.bindings) == 3
    assert planned.execution["presentation_runtime_proof"] == "pending"
    assert planned.execution["presentation_sources"] == {
        "manifest": {
            "path": str(
                fixture.attempt
                / "contracts"
                / build_home.PRESENTATION_MANIFEST_ATTEMPT_FILE
            ),
            "sha256": build_home.sha256_file(manifest_path),
        },
        "artifact_receipt": {
            "path": str(
                fixture.attempt
                / "contracts"
                / build_home.PRESENTATION_ARTIFACT_RECEIPT_ATTEMPT_FILE
            ),
            "sha256": build_home.sha256_file(receipt_path),
        },
    }
    assert set(planned.execution["presentation_scripts"]) == {
        "import", "compose", "common"
    }
    assert planned.execution["scripts"]["import"]["path"].endswith(
        "/import_assets_commandlet.py"
    )
    operations = [
        item for item in planned.execution["composition_spec"]["operations"]
        if item["kind"] == "place_room_presentation_bundle"
    ]
    assert len(operations) == 3
    assert {item["room_kind"] for item in operations} == set(
        planning.PRESENTATION_ROOM_KINDS
    )
    assert all("semantic_id" not in item for item in operations)
    assert all(item["unreal_collision_profile"] == "NoCollision" for item in operations)
    assert [item["phase"] for item in planned.dry_run_report["commands"]] == [
        "import", "presentation_import", "compose", "presentation_compose"
    ]

    attempt, _counts = build_home._materialize_inputs(planned)
    assert (
        attempt / "contracts" / build_home.PRESENTATION_MANIFEST_ATTEMPT_FILE
    ).read_bytes() == manifest_path.read_bytes()
    assert (
        attempt
        / "contracts"
        / build_home.PRESENTATION_ARTIFACT_RECEIPT_ATTEMPT_FILE
    ).read_bytes() == receipt_path.read_bytes()
    assert json.loads((attempt / "execution.json").read_text()) == planned.execution
    preparation = json.loads((attempt / "preparation-receipt.json").read_text())
    assert preparation["presentation_bundle_count"] == 3
    assert preparation["presentation_ue_import_observation"] == "pending"
    assert preparation["presentation_runtime_play_proof"] == "pending"


def test_presentation_inputs_require_profile_and_complete_pair(tmp_path: Path) -> None:
    fixture = BuildFixture(tmp_path)
    manifest_path, receipt_path, _manifest, _receipt = _presentation_contracts(
        tmp_path / "inputs" / "presentation", fixture
    )
    without_profile = dataclasses.replace(
        fixture.config(),
        presentation_manifest=manifest_path,
        presentation_manifest_sha256=build_home.sha256_file(manifest_path),
        presentation_artifact_receipt=receipt_path,
        presentation_artifact_receipt_sha256=build_home.sha256_file(receipt_path),
    )
    with pytest.raises(build_home.BuildHomeError, match="require --visual-profile"):
        build_home.plan_build(without_profile)
    incomplete = dataclasses.replace(
        fixture.config(),
        visual_profile=PROFILE_PATH,
        visual_profile_sha256=build_home.sha256_file(PROFILE_PATH),
        presentation_manifest=manifest_path,
        presentation_manifest_sha256=build_home.sha256_file(manifest_path),
    )
    with pytest.raises(build_home.BuildHomeError, match="paired paths"):
        build_home.plan_build(incomplete)


def test_presentation_manifest_receipt_and_glb_fail_closed(tmp_path: Path) -> None:
    fixture = BuildFixture(tmp_path)
    root = tmp_path / "inputs" / "presentation"
    manifest_path, receipt_path, _manifest, receipt = _presentation_contracts(
        root, fixture
    )
    receipt["ue_import_bundles"][0]["sha256"] = "a" * 64
    receipt_path.write_bytes(build_home.canonical_json(receipt))
    with pytest.raises(build_home.BuildHomeError, match="inventories differ"):
        build_home.plan_build(_presentation_config(fixture, manifest_path, receipt_path))

    other_root = tmp_path / "inputs" / "presentation-default"
    bad_manifest, bad_receipt, _manifest, _receipt = _presentation_contracts(
        other_root, fixture, default_material_kind="living_room"
    )
    with pytest.raises(build_home.BuildHomeError, match="DEFAULT_MATERIAL"):
        build_home.plan_build(
            _presentation_config(fixture, bad_manifest, bad_receipt)
        )

    escape_root = tmp_path / "inputs" / "presentation-escape"
    escape_manifest, escape_receipt, manifest, receipt = _presentation_contracts(
        escape_root, fixture
    )
    manifest["ue_import_bundles"][0]["relative_path"] = "../escape.glb"
    receipt["ue_import_bundles"][0]["relative_path"] = "../escape.glb"
    receipt["artifacts"][0]["relative_path"] = "../escape.glb"
    escape_manifest.write_bytes(build_home.canonical_json(manifest))
    escape_receipt.write_bytes(build_home.canonical_json(receipt))
    with pytest.raises(build_home.BuildHomeError, match="PATH_INVALID"):
        build_home.plan_build(
            _presentation_config(fixture, escape_manifest, escape_receipt)
        )


def test_r1_execution_and_config_remain_on_legacy_two_phase_path(
    tmp_path: Path,
) -> None:
    fixture = BuildFixture(tmp_path)
    planned = build_home.plan_build(fixture.config())

    assert planning.build_composition_spec(fixture.plan).sha256 == (
        "342d8262470fedbce4ce9be8125bf1d181b5291c0d18e50787d68015c394e72e"
    )
    assert build_home.sha256_bytes(planned.engine_ini_raw) == (
        "0933e82b84dc3dfec5928f961e1bc3ffb704476163d0689c24defdabbf388811"
    )
    assert "presentation_bindings" not in planned.execution
    assert "presentation_scripts" not in planned.execution
    assert "presentation_sources" not in planned.execution
    assert [item["phase"] for item in planned.dry_run_report["commands"]] == [
        "import", "compose"
    ]
    assert planned.execution["scripts"]["import"]["path"] == str(
        ROOT / "tools/ue/vista_playable_home/import_assets_commandlet.py"
    )
    assert planned.execution["scripts"]["compose"]["path"] == str(
        ROOT / "tools/ue/vista_playable_home/compose_home_commandlet.py"
    )


def test_presentation_sources_compile_without_launching_unreal() -> None:
    for relative in (
        "tools/ue/vista_playable_home/contract.py",
        "tools/ue/vista_playable_home/planning.py",
        "tools/ue/vista_playable_home/build_home.py",
        "tools/ue/vista_playable_home/presentation_commandlet_common.py",
        "tools/ue/vista_playable_home/import_presentation_commandlet.py",
        "tools/ue/vista_playable_home/compose_presentation_commandlet.py",
    ):
        py_compile.compile(str(ROOT / relative), doraise=True)
