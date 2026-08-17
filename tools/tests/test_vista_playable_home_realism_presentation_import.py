from __future__ import annotations

import ast
import copy
import dataclasses
import enum
import hashlib
import importlib
import json
import py_compile
import struct
import sys
from pathlib import Path

import pytest

from tools.tests.test_vista_playable_home_build_home import Fixture as BuildFixture
from tools.tests.test_vista_playable_home_realism_glb_alpha_gate import (
    _staticization_ledger_fixture,
)
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
    is_external = "external_content" in record
    materials = []
    for index in range(record["material_count"]):
        material_name = (
            record["material_ids"][index]
            if is_external
            else f"r2.synthetic.{index}"
        )
        base_index = 0 if is_external else index * 3
        materials.append({
            "name": "DefaultMaterial" if default_material and index == 0 else material_name,
            "pbrMetallicRoughness": {
                "baseColorTexture": {"index": base_index},
                "metallicRoughnessTexture": {"index": base_index + 1},
            },
            "normalTexture": {"index": base_index + 2},
        })
    extras = {
        "vista_bundle_contract": (
            "one_room_one_mesh_v2" if is_external else "one_room_one_mesh_v1"
        ),
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
    if is_external:
        extras["vista_external_content_json"] = json.dumps(
            record["external_content"], sort_keys=True, separators=(",", ":")
        )
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
        "textures": [
            {"source": index} for index in range(record["texture_count"])
        ],
        "images": [
            {"name": f"texture-{index}"}
            for index in range(record["texture_count"])
        ],
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


def _external_source_record(logical_id: str, asset_type: str) -> dict:
    paths = (
        ["source.blend"] if asset_type == "model" else []
    ) + ["base_color.jpg", "normal.jpg", "roughness.jpg"]
    semantics_by_path = {
        "base_color.jpg": ["base_color"],
        "normal.jpg": ["normal"],
        "roughness.jpg": ["roughness"],
    }
    files = []
    tree_rows = []
    for index, relative_path in enumerate(paths):
        digest = hashlib.sha256(
            f"{logical_id}:{relative_path}".encode("utf-8")
        ).hexdigest()
        semantics = semantics_by_path.get(relative_path, [])
        row = {
            "relative_path": relative_path,
            "size_bytes": 128 + index,
            "sha256": digest,
            "texture_semantics": semantics,
            "dimensions_px": [4096, 4096] if semantics else None,
        }
        files.append(row)
        tree_rows.append({
            "relative_path": relative_path,
            "size_bytes": row["size_bytes"],
            "sha256": digest,
        })
    tree_raw = json.dumps(
        tree_rows,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "logical_asset_id": logical_id,
        "asset_id": "polyhaven." + logical_id.replace(".", "_"),
        "asset_type": asset_type,
        "resolution": "4k" if logical_id.startswith(("visual.hero.", "visual.material.")) else "2k",
        "provider_files_hash": hashlib.sha1(logical_id.encode("utf-8")).hexdigest(),
        "source_tree_sha256": hashlib.sha256(tree_raw).hexdigest(),
        "files": files,
    }


def _external_presentation_contracts(
    root: Path,
    fixture: BuildFixture,
) -> tuple[Path, Path, dict, dict]:
    profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    acquisition = {
        "provider": "poly_haven",
        "receipt_schema_version": build_home.PRESENTATION_EXTERNAL_ACQUISITION_SCHEMA,
        "receipt_digest": "a" * 64,
        "receipt_file_sha256": "b" * 64,
        "acquisition_manifest_sha256": "c" * 64,
    }
    source_ids = {
        source_id
        for source_id in build_home.PRESENTATION_EXTERNAL_DRESSING_SOURCES.values()
    }
    for hero in build_home.PRESENTATION_EXTERNAL_HERO_PLACEMENTS.values():
        if hero["source_logical_asset_id"] is not None:
            source_ids.add(hero["source_logical_asset_id"])
        source_ids.update(hero["material_logical_asset_ids"])
    sources = [
        _external_source_record(
            logical_id,
            "texture" if logical_id.startswith("visual.material.") else "model",
        )
        for logical_id in sorted(source_ids)
    ]
    source_by_id = {item["logical_asset_id"]: item for item in sources}
    semantic_room = {
        semantic_id: room_id
        for room_id, semantic_ids in
        build_home.PRESENTATION_EXTERNAL_SEMANTIC_TARGETS_BY_ROOM.items()
        for semantic_id in semantic_ids
    }
    placements = []
    for index, semantic_id in enumerate(sorted(semantic_room)):
        room_id = semantic_room[semantic_id]
        hero = build_home.PRESENTATION_EXTERNAL_HERO_PLACEMENTS[semantic_id]
        source_id = hero["source_logical_asset_id"]
        placements.append({
            "placement_id": hero["placement_id"],
            "placement_kind": "semantic_fixed",
            "room_id": room_id,
            "room_kind": room_id.rsplit(".", 1)[-1],
            "category": build_home.PRESENTATION_EXTERNAL_SEMANTIC_TARGET_CATEGORIES[semantic_id],
            "realization_mode": hero["realization_mode"],
            "semantic_target_id": semantic_id,
            "anchor_id": None,
            "support_placement_id": None,
            "source_logical_asset_id": source_id,
            "geometry_recipe": hero["geometry_recipe"],
            "material_logical_asset_ids": list(hero["material_logical_asset_ids"]),
            "location_m": [index * 0.25, 0, 0],
            "rotation_deg": [0, 0, 0],
            "uniform_scale": 1,
            "source_dimensions_m": [0.2, 0.2, 0.2],
            "room_local_aabb": {
                "min_m": [index * 0.25, 0, 0],
                "max_m": [index * 0.25 + 0.2, 0.2, 0.2],
            },
            "source_tree_sha256": (
                source_by_id[source_id]["source_tree_sha256"]
                if source_id is not None else None
            ),
        })
    dressing_room = {
        dressing_id: room_id
        for room_id, dressing_ids in
        build_home.PRESENTATION_EXTERNAL_DRESSING_IDS_BY_ROOM.items()
        for dressing_id in dressing_ids
    }
    for index, dressing_id in enumerate(sorted(dressing_room), start=5):
        room_id = dressing_room[dressing_id]
        source_id = build_home.PRESENTATION_EXTERNAL_DRESSING_SOURCES[dressing_id]
        placements.append({
            "placement_id": dressing_id,
            "placement_kind": "dressing",
            "room_id": room_id,
            "room_kind": room_id.rsplit(".", 1)[-1],
            "category": "decorative_object",
            "realization_mode": "external_blend",
            "semantic_target_id": None,
            "anchor_id": room_id + "/dressing_anchor.synthetic",
            "support_placement_id": None,
            "source_logical_asset_id": source_id,
            "geometry_recipe": None,
            "material_logical_asset_ids": [],
            "location_m": [index * 0.25, 0, 0],
            "rotation_deg": [0, 0, 0],
            "uniform_scale": 1,
            "source_dimensions_m": [0.2, 0.2, 0.2],
            "room_local_aabb": {
                "min_m": [index * 0.25, 0, 0],
                "max_m": [index * 0.25 + 0.2, 0.2, 0.2],
            },
            "source_tree_sha256": source_by_id[source_id]["source_tree_sha256"],
        })
    placements.sort(key=lambda item: item["placement_id"])
    external_placement = {
        "schema_version": build_home.PRESENTATION_EXTERNAL_PLACEMENT_SCHEMA,
        "placement_id": "vista_playable_home.realistic_interior_r2.external_v1",
        "normalization_policy": build_home.PRESENTATION_EXTERNAL_NORMALIZATION_POLICY,
        "acquisition_receipt": copy.deepcopy(acquisition),
        "placement_manifest_sha256": "d" * 64,
        "semantic_target_ids": sorted(semantic_room),
        "dressing_ids": sorted(dressing_room),
        "asset_sources": sources,
        "placements": placements,
    }
    external_placement["content_digest"] = build_home._content_digest(
        external_placement
    )
    forge_sha = "f" * 64
    bundles = []
    for room in fixture.plan["rooms"]:
        kind = room["kind"]
        if kind not in planning.PRESENTATION_ROOM_KINDS:
            continue
        room_id = room["room_id"]
        room_placements = [
            item for item in placements if item["room_id"] == room_id
        ]
        room_source_ids = {
            logical_id
            for placement in room_placements
            for logical_id in (
                ([placement["source_logical_asset_id"]]
                 if placement["source_logical_asset_id"] is not None else [])
                + placement["material_logical_asset_ids"]
            )
        }
        external_content = {
            "schema_version": build_home.PRESENTATION_EXTERNAL_PLACEMENT_SCHEMA,
            "normalization_policy": build_home.PRESENTATION_EXTERNAL_NORMALIZATION_POLICY,
            "acquisition_receipt": copy.deepcopy(acquisition),
            "placement_manifest_sha256": external_placement["placement_manifest_sha256"],
            "placement_plan_sha256": external_placement["content_digest"],
            "semantic_target_ids": sorted(
                build_home.PRESENTATION_EXTERNAL_SEMANTIC_TARGETS_BY_ROOM[room_id]
            ),
            "dressing_ids": sorted(
                build_home.PRESENTATION_EXTERNAL_DRESSING_IDS_BY_ROOM[room_id]
            ),
            "asset_sources": [
                copy.deepcopy(source_by_id[logical_id])
                for logical_id in sorted(room_source_ids)
            ],
        }
        record = {
            "artifact_id": f"ue_bundle.room.{kind}",
            "artifact_kind": planning.PRESENTATION_ARTIFACT_KIND,
            "target_asset_id": f"asset.bundle.{kind}",
            "room_id": room_id,
            "room_kind": kind,
            "relative_path": f"ue_import_bundles/{kind}_presentation_bundle.glb",
            "media_type": "model/gltf-binary",
            "sha256": "0" * 64,
            "size_bytes": 1,
            "mesh_count": 1,
            "material_count": 2,
            "pbr_complete_material_count": 2,
            "texture_count": 3,
            "material_ids": [
                f"VISTA_M_r2_{kind}_architecture",
                f"VISTA_M_visual_{kind}_dressing",
            ],
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
            "external_content": external_content,
        }
        path = root / record["relative_path"]
        _write_glb(path, _glb_document(record))
        record["sha256"] = build_home.sha256_file(path)
        record["size_bytes"] = path.stat().st_size
        bundles.append(record)
    staticization = _staticization_ledger_fixture()
    staticization_path = (
        root / build_home.PRESENTATION_EXTERNAL_STATICIZATION_FILENAME
    )
    staticization_path.write_bytes(build_home.canonical_json(staticization))
    staticization_artifact = {
        "artifact_id": build_home.PRESENTATION_EXTERNAL_STATICIZATION_ARTIFACT_ID,
        "relative_path": build_home.PRESENTATION_EXTERNAL_STATICIZATION_FILENAME,
        "media_type": "application/json",
        "sha256": build_home.sha256_file(staticization_path),
        "size_bytes": staticization_path.stat().st_size,
    }
    manifest = {
        "schema_version": build_home.PRESENTATION_FORGE_SCHEMA_V2,
        "forge_id": "vista_playable_home.realistic_interior_r2",
        "house_revision": fixture.plan["house"]["revision"],
        "visual_profile_id": profile["visual_profile_id"],
        "seed": 43117,
        "source_house_digest": fixture.plan["house"]["content_digest"],
        "source_profile_digest": profile["content_digest"],
        "forge_plan_digest": forge_sha,
        "build_quality": {},
        "rooms": [],
        "openings": [],
        "components": [],
        "dressing": {},
        "materials": [],
        "role_counts": {},
        "room_component_counts": {},
        "export_contract": {},
        "ue_import_bundles": bundles,
        "external_placement": external_placement,
        "external_staticization": staticization,
    }
    receipt = {
        "schema_version": build_home.PRESENTATION_ARTIFACT_RECEIPT_SCHEMA_V2,
        "artifacts": [*copy.deepcopy(bundles), staticization_artifact],
        "ue_import_bundles": copy.deepcopy(bundles),
    }
    manifest_path = root / "normalized-manifest.json"
    receipt_path = root / "artifact-receipt.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_bytes(build_home.canonical_json(manifest))
    receipt_path.write_bytes(build_home.canonical_json(receipt))
    return manifest_path, receipt_path, manifest, receipt


def _rewrite_first_bundle(
    root: Path,
    manifest_path: Path,
    receipt_path: Path,
    manifest: dict,
    receipt: dict,
    mutate,
) -> None:
    record = manifest["ue_import_bundles"][0]
    document = _glb_document(record)
    mutate(document)
    bundle_path = root / record["relative_path"]
    _write_glb(bundle_path, document)
    digest = build_home.sha256_file(bundle_path)
    size = bundle_path.stat().st_size
    artifact_id = record["artifact_id"]
    for inventory in (
        manifest["ue_import_bundles"],
        receipt["ue_import_bundles"],
        receipt["artifacts"],
    ):
        matched = [item for item in inventory if item["artifact_id"] == artifact_id]
        assert len(matched) == 1
        matched[0]["sha256"] = digest
        matched[0]["size_bytes"] = size
    manifest_path.write_bytes(build_home.canonical_json(manifest))
    receipt_path.write_bytes(build_home.canonical_json(receipt))


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


def _presentation_vulkan_icd(root: Path) -> tuple[Path, Path, str]:
    library = root / "driver" / "libEGL_nvidia.so.0"
    library.parent.mkdir(parents=True, exist_ok=True)
    library.write_bytes(b"synthetic NVIDIA EGL library")
    library.chmod(0o600)
    manifest = root / "nvidia-headless-icd.json"
    manifest.write_bytes(build_home.canonical_json({
        "file_format_version": "1.0.1",
        "ICD": {
            "library_path": str(library),
            "api_version": "1.4.325",
        },
    }))
    manifest.chmod(0o600)
    return manifest, library, build_home.sha256_file(manifest)


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
    assert build_home._presentation_is_external(planned.execution) is False
    assert build_home._presentation_import_schema(planned.execution) == (
        build_home.PRESENTATION_IMPORT_RECEIPT_SCHEMA
    )
    assert build_home._presentation_scene_schema(planned.execution) == (
        build_home.PRESENTATION_SCENE_RECEIPT_SCHEMA
    )
    assert all(
        "external_content" not in binding
        for binding in planned.execution["presentation_bindings"]
    )
    assert "external_nanite_policy" not in (
        planned.dry_run_report["project"]["presentation"]
    )
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
    for command in planned.dry_run_report["commands"]:
        assert "-nullrhi" in command["argv"]
        assert not any("graphicsadapter" in item.lower() for item in command["argv"])
        phase_root = (
            fixture.attempt
            / build_home.COMMANDLET_RUNTIME_DIRECTORY
            / command["phase"]
        )
        assert command["env"]["HOME"] == str(phase_root / "home")
        assert command["env"]["TMPDIR"] == str(phase_root / "tmp")

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


def test_presentation_import_gpu0_retry_is_explicit_and_phase_scoped(
    tmp_path: Path,
) -> None:
    fixture = BuildFixture(tmp_path)
    manifest_path, receipt_path, _manifest, _receipt = _presentation_contracts(
        tmp_path / "inputs" / "presentation", fixture
    )
    vulkan_icd, vulkan_library, vulkan_icd_sha = _presentation_vulkan_icd(
        tmp_path / "inputs" / "vulkan"
    )
    config = dataclasses.replace(
        _presentation_config(fixture, manifest_path, receipt_path),
        presentation_import_gpu0_rendering=True,
        presentation_vulkan_icd=vulkan_icd,
        presentation_vulkan_icd_sha256=vulkan_icd_sha,
    )
    planned = build_home.plan_build(config)
    commands = {
        command["phase"]: command
        for command in planned.dry_run_report["commands"]
    }

    assert "-AllowCommandletRendering" in commands["presentation_import"]["argv"]
    assert "-RenderOffScreen" in commands["presentation_import"]["argv"]
    assert "-graphicsadapter=0" in commands["presentation_import"]["argv"]
    assert "-nullrhi" not in commands["presentation_import"]["argv"]
    staged_icd = (
        fixture.attempt
        / "contracts"
        / build_home.PRESENTATION_VULKAN_ICD_ATTEMPT_FILE
    )
    assert commands["presentation_import"]["env"]["VK_ICD_FILENAMES"] == str(
        staged_icd
    )
    for phase in ("import", "compose", "presentation_compose"):
        assert "-nullrhi" in commands[phase]["argv"]
        assert not any(
            "graphicsadapter" in item.lower() for item in commands[phase]["argv"]
        )
        assert "VK_ICD_FILENAMES" not in commands[phase]["env"]

    icd_report = planned.dry_run_report["inputs"]["presentation_vulkan_icd"]
    assert icd_report == {
        "path": str(vulkan_icd),
        "sha256": vulkan_icd_sha,
        "file_format_version": "1.0.1",
        "library_path": str(vulkan_library),
        "resolved_library_path": str(vulkan_library),
        "api_version": "1.4.325",
        "staged_path": str(staged_icd),
    }

    with pytest.raises(build_home.BuildHomeError, match="requires presentation inputs"):
        build_home.plan_build(
            dataclasses.replace(
                fixture.config(),
                presentation_import_gpu0_rendering=True,
            )
        )

    with pytest.raises(build_home.BuildHomeError, match="requires a Vulkan ICD"):
        build_home.plan_build(
            dataclasses.replace(
                _presentation_config(fixture, manifest_path, receipt_path),
                presentation_import_gpu0_rendering=True,
            )
        )
    with pytest.raises(build_home.BuildHomeError, match="require GPU rendering"):
        build_home.plan_build(
            dataclasses.replace(
                _presentation_config(fixture, manifest_path, receipt_path),
                presentation_vulkan_icd=vulkan_icd,
                presentation_vulkan_icd_sha256=vulkan_icd_sha,
            )
        )
    with pytest.raises(build_home.BuildHomeError, match="SHA-256 differs"):
        build_home.plan_build(
            dataclasses.replace(config, presentation_vulkan_icd_sha256="0" * 64)
        )

    attempt, _counts = build_home._materialize_inputs(planned)
    assert staged_icd.read_bytes() == vulkan_icd.read_bytes()
    preparation = json.loads((attempt / "preparation-receipt.json").read_text())
    assert preparation["presentation_vulkan_icd"] == str(staged_icd)
    assert preparation["presentation_vulkan_icd_sha256"] == vulkan_icd_sha
    assert preparation["presentation_vulkan_library_path"] == str(vulkan_library)


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


@pytest.mark.parametrize("case", ["decoy_extras", "parented_mesh"])
def test_presentation_glb_requires_the_active_identity_mesh_root(
    tmp_path: Path,
    case: str,
) -> None:
    fixture = BuildFixture(tmp_path)
    root = tmp_path / "inputs" / case
    manifest_path, receipt_path, manifest, receipt = _presentation_contracts(
        root, fixture
    )

    def mutate(document: dict) -> None:
        if case == "decoy_extras":
            extras = document["nodes"][0].pop("extras")
            document["nodes"][0]["translation"] = [1.0, 0.0, 0.0]
            document["nodes"].append({"name": "DecoyContract", "extras": extras})
        else:
            document["nodes"].append({
                "name": "DecoyParent",
                "children": [0],
            })
            document["scenes"][0]["nodes"] = [1]

    _rewrite_first_bundle(
        root, manifest_path, receipt_path, manifest, receipt, mutate
    )
    with pytest.raises(
        build_home.BuildHomeError,
        match="active scene root identity differs",
    ):
        build_home.plan_build(
            _presentation_config(fixture, manifest_path, receipt_path)
        )


def test_external_v2_contract_compiles_exact_content_and_nanite_policy(
    tmp_path: Path,
) -> None:
    fixture = BuildFixture(tmp_path)
    manifest_path, receipt_path, manifest, _receipt = (
        _external_presentation_contracts(
            tmp_path / "inputs" / "presentation-external", fixture
        )
    )

    planned = build_home.plan_build(
        _presentation_config(fixture, manifest_path, receipt_path)
    )

    assert planned.presentation is not None
    assert planned.presentation.manifest["schema_version"] == (
        build_home.PRESENTATION_FORGE_SCHEMA_V2
    )
    assert planned.presentation.artifact_receipt["schema_version"] == (
        build_home.PRESENTATION_ARTIFACT_RECEIPT_SCHEMA_V2
    )
    assert build_home._presentation_is_external(planned.execution) is True
    assert build_home._presentation_import_schema(planned.execution) == (
        build_home.PRESENTATION_IMPORT_RECEIPT_SCHEMA_V2
    )
    assert build_home._presentation_scene_schema(planned.execution) == (
        build_home.PRESENTATION_SCENE_RECEIPT_SCHEMA_V2
    )
    assert all(
        set(binding)
        == build_home.PRESENTATION_BUNDLE_RECORD_KEYS_V2
        | {"source_file", "source_file_sha256"}
        for binding in planned.execution["presentation_bindings"]
    )
    assert all(
        binding["external_content"]["placement_plan_sha256"]
        == manifest["external_placement"]["content_digest"]
        for binding in planned.execution["presentation_bindings"]
    )
    assert all(
        binding["texture_count"] == 3
        and binding["pbr_complete_material_count"] == binding["material_count"]
        for binding in planned.execution["presentation_bindings"]
    )
    presentation_report = planned.dry_run_report["project"]["presentation"]
    assert presentation_report["external_nanite_policy"] == (
        build_home.PRESENTATION_EXTERNAL_NANITE_POLICY
    )
    assert presentation_report["external_nanite_runtime_observation"] == "required"

    attempt, _counts = build_home._materialize_inputs(planned)
    assert json.loads((attempt / "execution.json").read_text()) == planned.execution
    assert all(
        operation["material_ids"] == binding["material_ids"]
        and operation["texture_count"] == binding["texture_count"]
        for operation, binding in zip(
            sorted(
                (
                    item
                    for item in planned.execution["composition_spec"]["operations"]
                    if item["kind"] == "place_room_presentation_bundle"
                ),
                key=lambda item: item["artifact_id"],
            ),
            sorted(
                planned.execution["presentation_bindings"],
                key=lambda item: item["artifact_id"],
            ),
        )
    )


@pytest.mark.parametrize(
    "case",
    [
        "missing_manifest_ledger",
        "missing_artifact",
        "artifact_pin",
        "manifest_ledger_mismatch",
        "nested_type",
        "invalid_ledger",
    ],
)
def test_external_v2_staticization_ledger_is_exact_and_fail_closed(
    tmp_path: Path,
    case: str,
) -> None:
    fixture = BuildFixture(tmp_path)
    root = tmp_path / "inputs" / case
    manifest_path, receipt_path, manifest, receipt = (
        _external_presentation_contracts(root, fixture)
    )
    artifact = next(
        item
        for item in receipt["artifacts"]
        if item.get("artifact_id")
        == build_home.PRESENTATION_EXTERNAL_STATICIZATION_ARTIFACT_ID
    )
    staticization_path = (
        root / build_home.PRESENTATION_EXTERNAL_STATICIZATION_FILENAME
    )
    if case == "missing_manifest_ledger":
        manifest.pop("external_staticization")
    elif case == "missing_artifact":
        receipt["artifacts"].remove(artifact)
    elif case == "artifact_pin":
        artifact["sha256"] = "0" * 64
    elif case == "manifest_ledger_mismatch":
        manifest["external_staticization"]["content_digest"] = "0" * 64
    else:
        if case == "nested_type":
            manifest["external_staticization"]["sources"][0][
                "input_actions"
            ] = [
                {
                    "name": [],
                    "frame_range": [1.0, 1.0],
                    "fcurve_count": 0,
                }
            ]
        else:
            manifest["external_staticization"]["blender_version"] = [4, 5, 9]
        staticization_path.write_bytes(
            build_home.canonical_json(manifest["external_staticization"])
        )
        artifact["sha256"] = build_home.sha256_file(staticization_path)
        artifact["size_bytes"] = staticization_path.stat().st_size
    manifest_path.write_bytes(build_home.canonical_json(manifest))
    receipt_path.write_bytes(build_home.canonical_json(receipt))

    with pytest.raises(
        build_home.BuildHomeError,
        match=r"PRESENTATION(?:_EXTERNAL)?_INVALID|BUILD_PIN_MISMATCH",
    ):
        build_home.plan_build(
            _presentation_config(fixture, manifest_path, receipt_path)
        )


@pytest.mark.parametrize(
    "manifest_schema,receipt_schema",
    [
        (
            build_home.PRESENTATION_FORGE_SCHEMA_V2,
            build_home.PRESENTATION_ARTIFACT_RECEIPT_SCHEMA,
        ),
        (
            build_home.PRESENTATION_FORGE_SCHEMA,
            build_home.PRESENTATION_ARTIFACT_RECEIPT_SCHEMA_V2,
        ),
    ],
)
def test_external_v2_requires_a_matched_manifest_receipt_schema_pair(
    tmp_path: Path,
    manifest_schema: str,
    receipt_schema: str,
) -> None:
    fixture = BuildFixture(tmp_path)
    manifest_path, receipt_path, manifest, receipt = (
        _external_presentation_contracts(
            tmp_path / "inputs" / "schema-pair", fixture
        )
    )
    manifest["schema_version"] = manifest_schema
    receipt["schema_version"] = receipt_schema
    manifest_path.write_bytes(build_home.canonical_json(manifest))
    receipt_path.write_bytes(build_home.canonical_json(receipt))

    with pytest.raises(build_home.BuildHomeError, match="matched v1/v2 pair"):
        build_home.plan_build(
            _presentation_config(fixture, manifest_path, receipt_path)
        )


@pytest.mark.parametrize(
    "case",
    [
        "manifest_extra_field",
        "acquisition_provider",
        "semantic_target",
        "dressing_id",
        "per_file_sha256",
        "source_tree_sha256",
    ],
)
def test_external_v2_manifest_provenance_and_identity_fail_closed(
    tmp_path: Path,
    case: str,
) -> None:
    fixture = BuildFixture(tmp_path)
    manifest_path, receipt_path, manifest, _receipt = (
        _external_presentation_contracts(
            tmp_path / "inputs" / case, fixture
        )
    )
    external = manifest["external_placement"]
    if case == "manifest_extra_field":
        manifest["unexpected"] = True
    elif case == "acquisition_provider":
        external["acquisition_receipt"]["provider"] = "untrusted"
    elif case == "semantic_target":
        external["semantic_target_ids"].pop()
    elif case == "dressing_id":
        external["dressing_ids"][0] = "dress.entry.unapproved"
        external["dressing_ids"].sort()
    elif case == "per_file_sha256":
        external["asset_sources"][0]["files"][0]["sha256"] = "z" * 64
    else:
        external["asset_sources"][0]["source_tree_sha256"] = "e" * 64
    if case != "manifest_extra_field":
        external["content_digest"] = build_home._content_digest(external)
    manifest_path.write_bytes(build_home.canonical_json(manifest))

    with pytest.raises(
        build_home.BuildHomeError,
        match=r"PRESENTATION(?:_EXTERNAL)?_INVALID",
    ):
        build_home.plan_build(
            _presentation_config(fixture, manifest_path, receipt_path)
        )


def test_external_v2_bundle_room_coverage_and_closed_field_fail_closed(
    tmp_path: Path,
) -> None:
    fixture = BuildFixture(tmp_path)
    manifest_path, receipt_path, manifest, receipt = (
        _external_presentation_contracts(
            tmp_path / "inputs" / "bundle-coverage", fixture
        )
    )
    first_artifact = manifest["ue_import_bundles"][0]["artifact_id"]
    for inventory in (
        manifest["ue_import_bundles"],
        receipt["ue_import_bundles"],
        receipt["artifacts"],
    ):
        record = next(item for item in inventory if item["artifact_id"] == first_artifact)
        record["external_content"]["unexpected"] = True
    manifest_path.write_bytes(build_home.canonical_json(manifest))
    receipt_path.write_bytes(build_home.canonical_json(receipt))
    with pytest.raises(build_home.BuildHomeError, match="external_content fields"):
        build_home.plan_build(
            _presentation_config(fixture, manifest_path, receipt_path)
        )

    second_root = tmp_path / "second"
    second_root.mkdir()
    second_fixture = BuildFixture(second_root)
    manifest_path, receipt_path, manifest, receipt = (
        _external_presentation_contracts(
            tmp_path / "second" / "inputs" / "bundle-room", second_fixture
        )
    )
    first_artifact = manifest["ue_import_bundles"][0]["artifact_id"]
    for inventory in (
        manifest["ue_import_bundles"],
        receipt["ue_import_bundles"],
        receipt["artifacts"],
    ):
        record = next(item for item in inventory if item["artifact_id"] == first_artifact)
        record["external_content"]["dressing_ids"] = []
    manifest_path.write_bytes(build_home.canonical_json(manifest))
    receipt_path.write_bytes(build_home.canonical_json(receipt))
    with pytest.raises(build_home.BuildHomeError, match="dressing IDs"):
        build_home.plan_build(
            _presentation_config(second_fixture, manifest_path, receipt_path)
        )


def test_external_v2_glb_material_names_must_match_exact_inventory(
    tmp_path: Path,
) -> None:
    fixture = BuildFixture(tmp_path)
    root = tmp_path / "inputs" / "material-inventory"
    manifest_path, receipt_path, manifest, receipt = (
        _external_presentation_contracts(root, fixture)
    )

    def mutate(document: dict) -> None:
        document["materials"][0]["name"] = "r2.external.unreported"

    _rewrite_first_bundle(
        root, manifest_path, receipt_path, manifest, receipt, mutate
    )
    with pytest.raises(build_home.BuildHomeError, match="material names differ"):
        build_home.plan_build(
            _presentation_config(fixture, manifest_path, receipt_path)
        )


def _external_presentation_import_receipt(
    planned: build_home.PlannedBuild,
    base_import_sha: str,
) -> dict:
    execution = planned.execution
    namespace = execution["composition_spec"]["content_namespace"]
    assets = []
    for binding in execution["presentation_bindings"]:
        texture_path = namespace + "/Presentation/Imports/T_Test.T_Test"
        assets.append({
            "artifact_id": binding["artifact_id"],
            "target_asset_id": binding["target_asset_id"],
            "room_id": binding["room_id"],
            "room_kind": binding["room_kind"],
            "source_file_sha256": binding["source_file_sha256"],
            "object_path": build_home._presentation_object_path(
                namespace, binding["target_asset_id"]
            ),
            "expected_world_transform_cm": copy.deepcopy(
                binding["expected_world_transform_cm"]
            ),
            "root_transform_policy": binding["root_transform_policy"],
            "semantic_policy": binding["semantic_policy"],
            "collision_policy": binding["collision_policy"],
            "unreal_collision_profile": "NoCollision",
            "material_ids": copy.deepcopy(binding["material_ids"]),
            "source_hashes": copy.deepcopy(binding["source_hashes"]),
            "raw_returned_object_paths": [texture_path],
            "returned_object_paths": [texture_path],
            "inspection": {
                "class_path": "/Script/Engine.StaticMesh",
                "material_paths": [
                    namespace + f"/Presentation/Materials/M_{index}.M_{index}"
                    for index in range(binding["material_count"])
                ],
                "returned_texture2d_paths": [texture_path],
                "material_texture2d_paths": [texture_path],
                "simple_collision_shapes": 0,
                "collision_profile_for_components": "NoCollision",
                "can_ever_affect_navigation": False,
                "nanite_enabled": False,
            },
            "external_content": copy.deepcopy(binding["external_content"]),
            "nanite_policy": build_home.PRESENTATION_EXTERNAL_NANITE_POLICY,
        })
    return {
        "schema_version": build_home.PRESENTATION_IMPORT_RECEIPT_SCHEMA_V2,
        "status": "imported_candidate",
        "error": None,
        "bindings": {
            "engine": "5.7.0-test",
            "project": execution["project_file"],
            "execution_manifest": str(Path(execution["attempt_root"]) / "execution.json"),
            "execution_manifest_sha256": build_home.sha256_bytes(
                planning.canonical_json(execution)
            ),
            "base_import_receipt": execution["import_receipt"],
            "base_import_receipt_sha256": base_import_sha,
            "composition_spec_sha256": execution["composition_spec_sha256"],
        },
        "content_namespace": namespace,
        "presentation_content_root": namespace + "/Presentation",
        "assets": sorted(assets, key=lambda item: item["room_id"]),
        "gates": {
            "base_import_verified": True,
            "exact_three_room_bundles": True,
            "one_mesh_per_bundle": True,
            "materials_and_textures_inspected": True,
            "no_collision_source_policy": True,
            "external_content_preserved": True,
            "external_nanite_disabled": True,
            "quarantined": False,
            "runtime_play_proof": "pending",
        },
    }


def test_external_v2_receipts_retain_content_and_verify_nanite_disabled(
    tmp_path: Path,
) -> None:
    fixture = BuildFixture(tmp_path)
    manifest_path, receipt_path, _manifest, _receipt = (
        _external_presentation_contracts(
            tmp_path / "inputs" / "external-receipts", fixture
        )
    )
    planned = build_home.plan_build(
        _presentation_config(fixture, manifest_path, receipt_path)
    )
    base_import_sha = "a" * 64
    import_receipt = _external_presentation_import_receipt(
        planned, base_import_sha
    )
    build_home._verify_presentation_import_receipt(
        import_receipt, planned.execution, base_import_sha
    )

    nanite_enabled = copy.deepcopy(import_receipt)
    nanite_enabled["assets"][0]["inspection"]["nanite_enabled"] = True
    with pytest.raises(build_home.BuildHomeError, match="import asset"):
        build_home._verify_presentation_import_receipt(
            nanite_enabled, planned.execution, base_import_sha
        )

    base_scene_sha = "b" * 64
    presentation_import_sha = "c" * 64
    scene_receipt = _presentation_scene_receipt(
        planned, base_scene_sha, presentation_import_sha
    )
    build_home._verify_presentation_scene_receipt(
        scene_receipt,
        planned.execution,
        base_scene_sha,
        presentation_import_sha,
    )
    scene_receipt["room_observations"][0]["nanite_enabled"] = True
    with pytest.raises(build_home.BuildHomeError, match="room observation"):
        build_home._verify_presentation_scene_receipt(
            scene_receipt,
            planned.execution,
            base_scene_sha,
            presentation_import_sha,
        )


def test_external_v2_scene_receipt_proves_exact_hidden_r1_semantic_targets(
    tmp_path: Path,
) -> None:
    fixture = BuildFixture(tmp_path)
    manifest_path, receipt_path, _manifest, _receipt = (
        _external_presentation_contracts(
            tmp_path / "inputs" / "external-semantic-visuals", fixture
        )
    )
    planned = build_home.plan_build(
        _presentation_config(fixture, manifest_path, receipt_path)
    )
    base_scene_sha = "b" * 64
    presentation_import_sha = "c" * 64
    receipt = _presentation_scene_receipt(
        planned, base_scene_sha, presentation_import_sha
    )
    build_home._verify_presentation_scene_receipt(
        receipt,
        planned.execution,
        base_scene_sha,
        presentation_import_sha,
    )

    targets = [
        target
        for room in receipt["room_observations"]
        for target in room["r1_semantic_visual_observations"]
    ]
    assert len(targets) == 5
    corruptions = []

    missing = copy.deepcopy(receipt)
    missing["room_observations"][0][
        "r1_semantic_visual_observations"
    ].pop()
    corruptions.append(missing)

    duplicate_actor = copy.deepcopy(receipt)
    duplicate_targets = [
        target
        for room in duplicate_actor["room_observations"]
        for target in room["r1_semantic_visual_observations"]
    ]
    duplicate_targets[1]["actor_path"] = duplicate_targets[0]["actor_path"]
    corruptions.append(duplicate_actor)

    visible = copy.deepcopy(receipt)
    visible["room_observations"][0][
        "r1_semantic_visual_observations"
    ][0]["render_components"][0]["visible"] = True
    corruptions.append(visible)

    collision_disabled = copy.deepcopy(receipt)
    collision_disabled["room_observations"][0][
        "r1_semantic_visual_observations"
    ][0]["render_components"][0]["collision_enabled"] = False
    corruptions.append(collision_disabled)

    collision_profile = copy.deepcopy(receipt)
    collision_profile["room_observations"][0][
        "r1_semantic_visual_observations"
    ][0]["render_components"][0]["collision_profile"] = "NoCollision"
    corruptions.append(collision_profile)

    unhidden = copy.deepcopy(receipt)
    unhidden["room_observations"][0][
        "r1_semantic_visual_observations"
    ][0]["actor_hidden_in_game"] = False
    corruptions.append(unhidden)

    interaction_lost = copy.deepcopy(receipt)
    interaction_lost["room_observations"][0][
        "r1_semantic_visual_observations"
    ][0]["interaction_affordances"] = []
    corruptions.append(interaction_lost)

    for corrupted in corruptions:
        with pytest.raises(
            build_home.BuildHomeError,
            match=r"semantic visual target|semantic visual observations",
        ):
            build_home._verify_presentation_scene_receipt(
                corrupted,
                planned.execution,
                base_scene_sha,
                presentation_import_sha,
            )


def _presentation_scene_receipt(
    planned: build_home.PlannedBuild,
    base_scene_sha: str,
    presentation_import_sha: str,
) -> dict:
    execution = planned.execution
    namespace = execution["composition_spec"]["content_namespace"]
    bindings = {
        item["artifact_id"]: item for item in execution["presentation_bindings"]
    }
    operations = [
        item for item in execution["composition_spec"]["operations"]
        if item["kind"] == "place_room_presentation_bundle"
    ]
    entity_operations = {
        item["semantic_id"]: item
        for item in execution["composition_spec"]["operations"]
        if item["kind"] == "place_entity"
    }
    observations = []
    semantic_target_index = 0
    for index, operation in enumerate(operations):
        source = bindings[operation["artifact_id"]]
        authority_path = f"{execution['composition_spec']['map_path']}:PersistentLevel.R1_{index}"
        observation = {
            "artifact_id": operation["artifact_id"],
            "presentation_id": operation["presentation_id"],
            "room_id": operation["room_id"],
            "room_kind": operation["room_kind"],
            "actor_path": (
                f"{execution['composition_spec']['map_path']}:PersistentLevel.R2_{index}"
            ),
            "static_mesh_object_path": build_home._presentation_object_path(
                namespace, source["target_asset_id"]
            ),
            "world_transform_cm": copy.deepcopy(operation["transform"]),
            "collision_profile": "NoCollision",
            "material_slot_count": source["material_count"],
            "attach_parent_actor_path": authority_path,
            "r1_authority_actor_path": authority_path,
            "r1_authority_collision_profile": "BlockAll",
            "r1_authority_hidden_in_game": True,
            "r1_authority_component_visible": False,
        }
        if "external_content" in source:
            semantic_visual_observations = []
            for semantic_target_id in source["external_content"][
                "semantic_target_ids"
            ]:
                expected_entity = entity_operations[semantic_target_id]
                target_path = (
                    f"{execution['composition_spec']['map_path']}:"
                    f"PersistentLevel.R1Semantic_{semantic_target_index}"
                )
                semantic_visual_observations.append({
                    "semantic_target_id": semantic_target_id,
                    "actor_path": target_path,
                    "actor_class_path": expected_entity["actor_class"],
                    "semantic_id_property": semantic_target_id,
                    "actor_hidden_in_game": True,
                    "interaction_affordances": sorted(
                        expected_entity["affordances"]
                    ),
                    "render_components": [{
                        "component_path": target_path + ".Mesh",
                        "visible": False,
                        "collision_profile": expected_entity["collision"][
                            "profile"
                        ],
                        "collision_enabled": True,
                    }],
                })
                semantic_target_index += 1
            observation.update({
                "external_content": copy.deepcopy(source["external_content"]),
                "nanite_policy": build_home.PRESENTATION_EXTERNAL_NANITE_POLICY,
                "nanite_enabled": False,
                "r1_semantic_visual_observations": (
                    semantic_visual_observations
                ),
            })
        observations.append(observation)
    gates = {
        "map_saved": True,
        "map_reloaded": True,
        "exact_three_presentation_actors": True,
        "presentation_no_collision_verified": True,
        "hidden_r1_collision_authority_verified": True,
        "semantic_authority_preserved": True,
        "quarantined": False,
        "runtime_play_proof": "pending",
    }
    if build_home._presentation_is_external(execution):
        gates["external_nanite_disabled_verified"] = True
        gates["external_r1_semantic_visual_targets_verified"] = True
    return {
        "schema_version": build_home._presentation_scene_schema(execution),
        "status": "saved_reloaded_candidate",
        "error": None,
        "bindings": {
            "engine": "5.7.0-test",
            "project": execution["project_file"],
            "execution_manifest": str(Path(execution["attempt_root"]) / "execution.json"),
            "execution_manifest_sha256": build_home.sha256_bytes(
                planning.canonical_json(execution)
            ),
            "base_scene_receipt": execution["scene_receipt"],
            "base_scene_receipt_sha256": base_scene_sha,
            "presentation_import_receipt": execution["presentation_import_receipt"],
            "presentation_import_receipt_sha256": presentation_import_sha,
            "composition_spec_sha256": execution["composition_spec_sha256"],
        },
        "content_namespace": namespace,
        "map_path": execution["composition_spec"]["map_path"],
        "room_observations": sorted(
            observations, key=lambda item: item["room_id"]
        ),
        "gates": gates,
    }


def test_presentation_scene_receipt_recomputes_each_room_observation(
    tmp_path: Path,
) -> None:
    fixture = BuildFixture(tmp_path)
    manifest_path, receipt_path, _manifest, _receipt = _presentation_contracts(
        tmp_path / "inputs" / "presentation", fixture
    )
    planned = build_home.plan_build(
        _presentation_config(fixture, manifest_path, receipt_path)
    )
    base_scene_sha = "a" * 64
    presentation_import_sha = "b" * 64
    receipt = _presentation_scene_receipt(
        planned, base_scene_sha, presentation_import_sha
    )
    build_home._verify_presentation_scene_receipt(
        receipt, planned.execution, base_scene_sha, presentation_import_sha
    )

    corruptions = []
    wrong_mesh = copy.deepcopy(receipt)
    wrong_mesh["room_observations"][0]["static_mesh_object_path"] += "_Wrong"
    corruptions.append(wrong_mesh)
    wrong_transform = copy.deepcopy(receipt)
    wrong_transform["room_observations"][0]["world_transform_cm"][
        "location_cm"
    ][0] += 1.0
    corruptions.append(wrong_transform)
    wrong_parent = copy.deepcopy(receipt)
    wrong_parent["room_observations"][0]["attach_parent_actor_path"] += "_Wrong"
    corruptions.append(wrong_parent)
    for corrupted in corruptions:
        with pytest.raises(build_home.BuildHomeError, match="room observation"):
            build_home._verify_presentation_scene_receipt(
                corrupted,
                planned.execution,
                base_scene_sha,
                presentation_import_sha,
            )


def test_result_scene_receipt_pins_preserve_legacy_semantics() -> None:
    base_scene_sha = "a" * 64
    presentation_scene_sha = "b" * 64
    assert build_home._result_scene_receipt_pins(base_scene_sha, None) == {
        "scene_receipt_sha256": base_scene_sha
    }
    assert build_home._result_scene_receipt_pins(
        base_scene_sha, presentation_scene_sha
    ) == {
        "scene_receipt_sha256": base_scene_sha,
        "presentation_scene_receipt_sha256": presentation_scene_sha,
    }


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

    for relative in (
        "tools/ue/vista_playable_home/import_presentation_commandlet.py",
        "tools/ue/vista_playable_home/compose_presentation_commandlet.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert 'property_or_none(mesh, "nanite_settings")' in source
        assert "get_nanite_settings" not in source

    composer = (
        ROOT / "tools/ue/vista_playable_home/compose_presentation_commandlet.py"
    ).read_text(encoding="utf-8")
    assert 'actor.get_editor_property("hidden")' in composer
    assert "Actor.hidden is unavailable" in composer
    assert 'getattr(actor, "is_hidden"' not in composer
    assert 'binding["external_content"]["semantic_target_ids"]' in composer
    assert "hide_semantic_target_visuals(actor)" in composer
    assert "r1_semantic_visual_observations" in composer


def test_presentation_shadow_delegation_is_nanite_backed_and_reloaded() -> None:
    composer = (
        ROOT / "tools/ue/vista_playable_home/compose_presentation_commandlet.py"
    ).read_text(encoding="utf-8")

    assert (
        'PRESENTATION_SHADOW_POLICY_TAG = "VistaShadowPolicy=visible_no_shadow"'
        in composer
    )
    assert (
        'AUTHORITY_SHADOW_POLICY_TAG = "VistaShadowPolicy=hidden_nanite_authority"'
        in composer
    )
    before_save, after_save = composer.split(
        'stage = {"phase": "presentation_save", "operation_id": None}', 1
    )
    authority_nanite_check = (
        'require(nanite_enabled(authority_mesh) is True,\n'
        '                    "r1 room shadow authority is not Nanite-enabled")'
    )
    assert authority_nanite_check in before_save
    assert before_save.index(authority_nanite_check) < before_save.index(
        "authority_component.set_cast_shadow(True)"
    )
    assert "authority_component.set_cast_hidden_shadow(True)" in before_save
    assert "component.set_cast_shadow(False)" in before_save
    assert "component.set_cast_hidden_shadow(False)" in before_save
    assert "AUTHORITY_SHADOW_POLICY_TAG" in before_save
    assert "PRESENTATION_SHADOW_POLICY_TAG" in before_save

    for evidence in (
        'label="reloaded visible presentation component"',
        'label="reloaded r1 room authority"',
        '"reloaded r1 room shadow authority is not Nanite-enabled"',
        '"reloaded presentation actor lost shadow policy tag"',
        '"reloaded r1 authority lost shadow policy tag"',
    ):
        assert evidence in after_save
    assert "shadow_delegation_verified = True" in after_save
    assert (
        '"hidden_r1_collision_authority_verified": (\n'
        "            reload_verified and shadow_delegation_verified\n"
        "        )"
    ) in after_save


def test_reflected_affordance_names_use_typed_enum_members(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commandlet_root = ROOT / "tools/ue/vista_playable_home"
    monkeypatch.syspath_prepend(str(commandlet_root))
    sys.modules.pop("presentation_commandlet_common", None)
    common = importlib.import_module("presentation_commandlet_common")

    class VistaAffordance(enum.Enum):
        PICK_UP = 2
        SIT = 6
        INSPECT = 7

    assert common.reflected_affordance_name(
        VistaAffordance.PICK_UP, VistaAffordance
    ) == "pick_up"
    assert common.reflected_affordance_name(
        VistaAffordance.SIT, VistaAffordance
    ) == "sit"
    assert common.reflected_affordance_name(
        VistaAffordance.INSPECT, VistaAffordance
    ) == "inspect"
    with pytest.raises(RuntimeError, match="member name"):
        common.reflected_affordance_name("<VistaAffordance.SIT: 6>", VistaAffordance)

    class LookalikeAffordance(enum.IntEnum):
        SIT = 6

    with pytest.raises(RuntimeError, match="closed VISTA enum"):
        common.reflected_affordance_name(
            LookalikeAffordance.SIT, VistaAffordance
        )

    composer = (
        ROOT / "tools/ue/vista_playable_home/compose_presentation_commandlet.py"
    ).read_text(encoding="utf-8")
    assert "reflected_affordance_name(value, unreal.VistaAffordance)" in composer
    assert 'str(value).rsplit(".", 1)' not in composer


def test_presentation_collision_clear_is_commandlet_safe_and_reloaded() -> None:
    common = (
        ROOT
        / "tools/ue/vista_playable_home/presentation_commandlet_common.py"
    ).read_text(encoding="utf-8")
    importer = (
        ROOT
        / "tools/ue/vista_playable_home/import_presentation_commandlet.py"
    ).read_text(encoding="utf-8")
    composer = (
        ROOT
        / "tools/ue/vista_playable_home/compose_presentation_commandlet.py"
    ).read_text(encoding="utf-8")

    expected_properties = (
        "box_elems",
        "sphere_elems",
        "sphyl_elems",
        "convex_elems",
        "tapered_capsule_elems",
        "level_set_elems",
        "ml_level_set_elems",
        "skinned_level_set_elems",
        "skinned_triangle_mesh_elems",
    )
    tree = ast.parse(common)
    assignments = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "SIMPLE_COLLISION_ELEMENT_PROPERTIES"
            for target in node.targets
        )
    ]
    assert len(assignments) == 1
    assert ast.literal_eval(assignments[0].value) == expected_properties
    assert 'body_setup.set_editor_property("agg_geom", aggregate)' in common
    assert "clear_simple_collision(loaded)" in importer
    assert ".remove_collisions" not in importer
    assert "remove_collisions(" not in importer
    assert 'simple_collision_count(mesh) == 0' in composer
    assert "reloaded presentation mesh retained simple collision" in composer
