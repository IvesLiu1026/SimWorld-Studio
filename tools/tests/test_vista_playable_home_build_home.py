from __future__ import annotations

import copy
import hashlib
import json
import os
import pathlib
import stat
import sys

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.blender.vista_playable_home import contract_scene as blender_contract  # noqa: E402
from tools.ue.vista_playable_home import build_home, planning  # noqa: E402
from tools.ue.vista_playable_home.commandlet_common import (  # noqa: E402
    IMPORT_RECEIPT_SCHEMA,
    SCENE_RECEIPT_SCHEMA,
)
from tools.worlds import playable_home as world_contract  # noqa: E402


PACK = ROOT / "world_packs" / "vista_playable_home_r1"


def _write(path: pathlib.Path, raw: bytes, mode: int = 0o600) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    path.chmod(mode)
    return path


def _entry(path: pathlib.Path, root: pathlib.Path, media_type: str) -> dict:
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": build_home.sha256_file(path),
        "bytes": path.stat().st_size,
        "media_type": media_type,
    }


class Fixture:
    def __init__(self, root: pathlib.Path) -> None:
        self.root = root
        self.root.chmod(0o700)
        self.run_root = root / "run"
        self.run_root.mkdir(mode=0o700)
        self.attempt = self.run_root / "ue" / "attempt-test"

        house = world_contract.load_json(PACK / "house.json")
        events = world_contract.load_events(PACK / "events")
        self.plan = world_contract.compile_build_plan(house, events)
        self.plan_path = _write(root / "inputs/build-plan.json", planning.canonical_json(self.plan))

        self.blender_root = root / "inputs/blender"
        self.blender_root.mkdir(parents=True)
        normalized = blender_contract.normalized_manifest(blender_contract.build_contract_plan(house))
        self.normalized = normalized
        normalized_path = _write(
            self.blender_root / "normalized-manifest.json",
            blender_contract.canonical_json_bytes(normalized),
        )
        outputs = {
            "blend": _entry(_write(self.blender_root / "source.blend", b"synthetic blend"), self.blender_root, "application/x-blender"),
            "glb": _entry(_write(self.blender_root / "world.glb", b"glTF synthetic world"), self.blender_root, "model/gltf-binary"),
            "normalized_manifest": _entry(normalized_path, self.blender_root, "application/json"),
            "preview_interior": _entry(_write(self.blender_root / "interior.png", b"synthetic png interior"), self.blender_root, "image/png"),
            "preview_overview": _entry(_write(self.blender_root / "overview.png", b"synthetic png overview"), self.blender_root, "image/png"),
        }
        source_nodes: dict[str, list[str]] = {}
        for entity in normalized["entities"]:
            source_nodes.setdefault(entity["asset_ref"], []).append(entity["blender_node_id"])
        for bundle in normalized["room_bundles"]:
            source_nodes.setdefault(bundle["asset_ref"], []).append(bundle["node_id"])
        nonbuiltin = {
            item["asset_id"]
            for item in self.plan["assets"]
            if item["source_kind"] != "builtin"
        }
        artifacts = {}
        for asset_id in sorted(nonbuiltin):
            path = _write(
                self.blender_root / "assets" / f"{asset_id}.glb",
                b"glTF synthetic asset\0" + asset_id.encode("utf-8"),
            )
            artifacts[asset_id] = {
                **_entry(path, self.blender_root, "model/gltf-binary"),
                "mesh_count": 1,
                "source_node_ids": sorted(source_nodes[asset_id]),
            }
        self.blender_manifest = {
            "schema_version": build_home.BLENDER_BUILD_RECEIPT_SCHEMA,
            "house_id": self.plan["house"]["house_id"],
            "revision": self.plan["house"]["revision"],
            "source_house_digest": self.plan["house"]["content_digest"],
            "normalized_manifest_digest": normalized["content_digest"],
            "build": {
                "seed": house["seed"],
                "timestamp_utc": "2026-08-15T00:00:00Z",
                "blender_version": "4.5.8 LTS",
            },
            "outputs": outputs,
            "asset_artifacts": artifacts,
        }
        self.blender_manifest_path = _write(
            self.blender_root / "manifest.json",
            blender_contract.canonical_json_bytes(self.blender_manifest),
        )

        self.plugin = root / "inputs/plugin"
        plugin_descriptor = {
            "FileVersion": 3,
            "FriendlyName": "VISTA Playable Home",
            "Modules": [
                {"Name": "VistaPlayableHome", "Type": "Runtime", "LoadingPhase": "Default"},
                {"Name": "VistaPlayableHomeEditor", "Type": "Editor", "LoadingPhase": "Default"},
            ],
        }
        _write(self.plugin / "VistaPlayableHome.uplugin", build_home.canonical_json(plugin_descriptor))
        _write(self.plugin / "Binaries/Linux/libUnrealEditor-VistaPlayableHome.so", b"compiled plugin")
        _write(
            self.plugin / "Binaries/Linux/libUnrealEditor-VistaPlayableHomeEditor.so",
            b"compiled editor plugin",
        )
        _write(
            self.plugin / "Binaries/Linux/UnrealEditor.modules",
            build_home.canonical_json({
                "BuildId": "fixture",
                "Modules": {
                    "VistaPlayableHome": "libUnrealEditor-VistaPlayableHome.so",
                    "VistaPlayableHomeEditor": "libUnrealEditor-VistaPlayableHomeEditor.so",
                },
            }),
        )
        _write(self.plugin / "Config/DefaultVistaPlayableHome.ini", b"[Vista]\n")
        _write(self.plugin / "README.md", b"synthetic package\n")

        self.characters = root / "inputs/Characters"
        _write(self.characters / "Mannequins/Meshes/SKM_Manny.uasset", b"Manny mesh")
        _write(self.characters / "Mannequins/Animations/ABP_Manny.uasset", b"Manny animation")

        self.missing_editor = root / "missing-engine/Engine/Binaries/Linux/UnrealEditor-Cmd"
        self.plugin_snapshot = build_home.snapshot_tree(self.plugin, "plugin fixture")
        self.characters_snapshot = build_home.snapshot_tree(self.characters, "Characters fixture")

    def config(self, *, editor: pathlib.Path | None = None, editor_sha: str | None = None) -> build_home.BuildConfig:
        return build_home.BuildConfig(
            run_root=self.run_root,
            attempt_root=self.attempt,
            build_plan=self.plan_path,
            build_plan_sha256=build_home.sha256_file(self.plan_path),
            blender_manifest=self.blender_manifest_path,
            blender_manifest_sha256=build_home.sha256_file(self.blender_manifest_path),
            plugin_package=self.plugin,
            plugin_package_tree_sha256=self.plugin_snapshot.sha256,
            characters_content=self.characters,
            characters_content_tree_sha256=self.characters_snapshot.sha256,
            unreal_editor_cmd=editor or self.missing_editor,
            unreal_editor_cmd_sha256=editor_sha,
        )

    def editor(self) -> tuple[pathlib.Path, str]:
        path = _write(
            self.root / "engine/Engine/Binaries/Linux/UnrealEditor-Cmd",
            b"#!/bin/sh\nexit 99\n",
            0o700,
        )
        return path, build_home.sha256_file(path)

    def visual_manifest(self, asset_id: str) -> tuple[pathlib.Path, str, pathlib.Path]:
        root = self.root / "inputs/hssd"
        output = _write(root / "assets" / f"{asset_id}.glb", b"glTF HSSD presentation")
        source_sha = hashlib.sha256(b"HSSD source object").hexdigest()
        source_object_id = "c" * 40
        source_dimensions = [2.0, 0.8, 0.85]
        actual_glb_geometry = {
            "measurement_policy": "decoded_position_accessors_active_scene_world_aabb_v1",
            "coordinate_conversion": "gltf_y_up_to_blender_x_negative_z_y",
            "mesh_node_count": 1,
            "position_accessor_count": 1,
            "position_vertex_count": 24,
            "gltf_bounds_m": {
                "min_m": [-1.0, -0.425, -0.4],
                "max_m": [1.0, 0.425, 0.4],
            },
            "gltf_dimensions_m": [2.0, 0.85, 0.8],
            "blender_bounds_m": {
                "min_m": [-1.0, -0.4, -0.425],
                "max_m": [1.0, 0.4, 0.425],
            },
            "blender_dimensions_m": source_dimensions,
        }
        dataset = {
            "dataset": build_home.hssd_contract.HSSD_DATASET_NAME,
            "dataset_revision": "a" * 40,
            "readme_relpath": "README.md",
            "readme_sha256": build_home.hssd_contract.PINNED_HSSD_README_SHA256,
            "project_url": build_home.hssd_contract.HSSD_PROJECT_URL,
            "license": {
                "spdx": "CC-BY-NC-4.0",
                "url": build_home.hssd_contract.HSSD_LICENSE_URL,
                "commercial_use": "prohibited_without_separate_permission",
                "attribution_required": True,
                "modification_notice_required": True,
            },
        }
        license_receipt = {
            "accepted_spdx": "CC-BY-NC-4.0",
            "scope": "research_and_noncommercial_demo_only",
            "attribution_notice": "HSSD, modified for normalized demo dimensions",
            "commercial_release_gate": "replace_assets_or_obtain_separate_permission",
        }
        nonbuiltin = sorted(
            item["asset_id"]
            for item in self.plan["assets"]
            if item["source_kind"] != "builtin"
        )
        preserved = [item for item in nonbuiltin if item != asset_id]
        source_contract = {
            "dataset": build_home.hssd_contract.HSSD_DATASET_NAME,
            "object_id": source_object_id,
            "name": "synthetic sofa",
            "semantic_category": "couch",
            "render_asset_relpath": f"objects/c/{source_object_id}.glb",
            "object_config_relpath": f"objects/c/{source_object_id}.object_config.json",
            "render_asset_sha256": source_sha,
            "catalog_aligned_dimensions_m": source_dimensions,
            "catalog_dimensions_provenance": "metadata/fpmodels-with-decomposed.csv:aligned.dims",
            "source_dimensions_blender_m": source_dimensions,
            "actual_glb_geometry": actual_glb_geometry,
            "up": [0.0, 1.0, 0.0],
            "front": [0.0, 0.0, -1.0],
            "license_spdx": "CC-BY-NC-4.0",
            "license_url": build_home.hssd_contract.HSSD_LICENSE_URL,
        }
        output_source_contract = {
            "dataset": source_contract["dataset"],
            "object_id": source_contract["object_id"],
            "render_asset_sha256": source_contract["render_asset_sha256"],
            "license_spdx": source_contract["license_spdx"],
            "license_url": source_contract["license_url"],
            "catalog_aligned_dimensions_m": source_contract["catalog_aligned_dimensions_m"],
            "actual_glb_geometry": source_contract["actual_glb_geometry"],
        }
        binding_plan = {
            "schema_version": build_home.HSSD_BINDING_PLAN_SCHEMA,
            "house_id": self.plan["house"]["house_id"],
            "revision": self.plan["house"]["revision"],
            "source_normalized_manifest": {
                "schema_version": blender_contract.MANIFEST_SCHEMA,
                "content_digest": self.normalized["content_digest"],
            },
            "dataset": dataset,
            "license_receipt": license_receipt,
            "selection_policy": {
                "version": build_home.hssd_contract.SELECTION_POLICY_VERSION,
                "dimension_source": "decoded_glb_position_accessors_active_scene_world_aabb",
                "catalog_dimensions_role": "provenance_only_not_selection",
                "maximum_axis_scale_anisotropy": 2.75,
            },
            "mode": "full",
            "closed_world": {
                "target_asset_ids": nonbuiltin,
                "bound_asset_ids": [asset_id],
                "preserved_asset_ids": preserved,
                "unaccounted_asset_ids": [],
            },
            "bindings": [{
                "logical_asset_id": asset_id,
                "target_dimensions_m": [2.2, 0.9, 0.9],
                "source": source_contract,
                "source_inspection": {"pbr_texture_slot_count": 1},
                "normalization_plan": {
                    "dimension_source": "decoded_glb_position_accessors_active_scene_world_aabb",
                    "anisotropy_accepted": True,
                    "planned_rotate_z_deg": 0,
                    "scale_anisotropy": 1.0625,
                },
                "selection_receipt": {
                    "geometry_measurement_policy": "decoded_position_accessors_active_scene_world_aabb_v1",
                    "catalog_dimensions_used_for_selection": False,
                    "matching_candidate_count": 3,
                    "evaluated_candidate_count": 3,
                    "eligible_candidate_count": 1,
                    "candidate_decision_digest": "d" * 64,
                    "selected_object_id": source_object_id,
                    "selected_actual_scale_anisotropy": 1.0625,
                    "maximum_axis_scale_anisotropy": 2.75,
                    "accepted": True,
                },
            }],
            "preserved_assets": [{"asset_id": item} for item in preserved],
        }
        binding_plan = build_home.hssd_contract.seal_document(binding_plan)
        _write(root / "binding-plan.json", build_home.hssd_contract.canonical_json_bytes(binding_plan))
        value = {
            "schema_version": build_home.HSSD_MANIFEST_SCHEMA,
            "house_id": self.plan["house"]["house_id"],
            "revision": self.plan["house"]["revision"],
            "source_plan": {
                "schema_version": build_home.HSSD_BINDING_PLAN_SCHEMA,
                "content_digest": binding_plan["content_digest"],
                "path": "binding-plan.json",
            },
            "dataset": dataset,
            "license_receipt": license_receipt,
            "blender": {"version": "4.5.8 LTS", "mode": "full"},
            "builder_source": {
                "repository_commit": "b" * 40,
                "worktree_clean": True,
                "source_files": [
                    {
                        "path": relative,
                        "sha256": build_home.sha256_file(ROOT / relative),
                    }
                    for relative in build_home.HSSD_BUILDER_SOURCE_FILES
                ],
            },
            "normalization_policy": {"maximum_axis_scale_anisotropy": 2.75},
            "closed_world": {"bound_asset_ids": [asset_id], "unaccounted_asset_ids": []},
            "outputs": [{
                "logical_asset_id": asset_id,
                "semantic_category": "sofa",
                "path": output.relative_to(root).as_posix(),
                "sha256": build_home.sha256_file(output),
                "bytes": output.stat().st_size,
                "media_type": "model/gltf-binary",
                "target_dimensions_m": [2.2, 0.9, 0.9],
                "actual_dimensions_m": [2.2, 0.9, 0.9],
                "normalization": {
                    "source_import_dimensions_m": source_dimensions,
                    "planned_source_dimensions_m": source_dimensions,
                    "source_dimensions_match_plan": True,
                    "rotate_z_deg": 0,
                    "planned_rotate_z_deg": 0,
                    "fit_matches_plan": True,
                    "rotation_mode": "XYZ",
                    "scale_xyz": [1.1, 1.125, 1.058824],
                    "actual_scale_anisotropy": 1.0625,
                    "maximum_axis_scale_anisotropy": 2.75,
                    "anisotropy_accepted": True,
                    "origin_policy": "footprint_center_bottom_z_zero",
                    "actual_bounds_m": {
                        "min_m": [-1.1, -0.45, 0.0],
                        "max_m": [1.1, 0.45, 0.9],
                    },
                    "actual_dimensions_m": [2.2, 0.9, 0.9],
                },
                "texture_transport": "blender_native_texture_import",
                "texture_transport_receipt": {"mode": "blender_native_texture_import"},
                "source": output_source_contract,
                "inspection": {
                    "mesh_count": 1,
                    "primitive_count": 1,
                    "material_bound_primitive_count": 1,
                    "triangle_count": 12,
                    "material_count": 1,
                    "pbr_material_count": 1,
                    "texture_count": 1,
                    "image_count": 1,
                    "pbr_texture_slot_count": 1,
                    "base_normal_orm_texture_slot_count": 1,
                    "all_primitives_material_bound": 1,
                    "basisu_required": 0,
                },
            }],
        }
        value = build_home.hssd_contract.seal_document(value)
        path = _write(root / "manifest.json", build_home.hssd_contract.canonical_json_bytes(value))
        return path, build_home.sha256_file(path), output


@pytest.fixture()
def fixture(tmp_path: pathlib.Path) -> Fixture:
    return Fixture(tmp_path)


def test_dry_run_validates_full_execution_and_writes_nothing(fixture: Fixture) -> None:
    before = sorted(path.relative_to(fixture.root) for path in fixture.root.rglob("*"))
    planned = build_home.plan_build(fixture.config())
    after = sorted(path.relative_to(fixture.root) for path in fixture.root.rglob("*"))

    assert before == after
    assert not fixture.attempt.exists()
    assert planned.dry_run_report["mode"] == "dry_run"
    assert planned.execution["schema_version"] == build_home.contract.EXECUTION_SCHEMA
    assert planned.execution["build_plan_content_digest"] == fixture.plan["content_digest"]
    assert len(planned.execution["artifact_bindings"]) == 38
    assert len([item for item in planned.bindings if item["source_file"] is not None]) == 35
    assert len(planned.execution["composition_spec"]["operations"]) > 100
    import_command = planned.dry_run_report["commands"][0]
    assert import_command["argv"][2:8] == [
        "-run=pythonscript",
        "-script=" + str(ROOT / "tools/ue/vista_playable_home/import_assets_commandlet.py"),
        "-nocrashreports",
        "-unattended",
        "-nop4",
        "-nosplash",
    ]
    assert "-nullrhi" in import_command["argv"]
    assert not any("graphicsadapter" in item.lower() for item in import_command["argv"])
    runtime_root = fixture.attempt / build_home.COMMANDLET_RUNTIME_DIRECTORY
    assert f"-UserDir={runtime_root / 'import/user'}" in import_command["argv"]
    assert f"-LocalDataCachePath={runtime_root / 'ddc'}" in import_command["argv"]
    assert import_command["env"]["HOME"] == str(runtime_root / "import/home")
    assert import_command["env"]["TMPDIR"] == str(runtime_root / "import/tmp")
    assert import_command["env"]["XDG_CACHE_HOME"] == str(
        runtime_root / "import/xdg-cache"
    )
    assert import_command["env"]["PYTHONDONTWRITEBYTECODE"] == "1"
    assert planned.dry_run_report["commands"][1]["env"][
        "VISTA_PLAYABLE_HOME_IMPORT_RECEIPT_SHA256"
    ] == "<sha256-from-verified-import-receipt>"
    body = copy.deepcopy(planned.dry_run_report)
    digest = body.pop("content_digest")
    assert digest == build_home.sha256_bytes(build_home.canonical_json(body))


def test_materialization_creates_content_only_project_and_matches_contract(fixture: Fixture) -> None:
    planned = build_home.plan_build(fixture.config())
    attempt, copy_counts = build_home._materialize_inputs(planned)

    assert attempt == fixture.attempt
    descriptor = json.loads((attempt / "project/VistaPlayableHome.uproject").read_text())
    assert "Modules" not in descriptor
    assert {item["Name"] for item in descriptor["Plugins"]} >= {
        "VistaPlayableHome",
        "PythonScriptPlugin",
        "EditorScriptingUtilities",
        "Interchange",
    }
    assert (attempt / "project/Plugins/VistaPlayableHome/Binaries/Linux/libUnrealEditor-VistaPlayableHome.so").is_file()
    assert (attempt / "project/Plugins/VistaPlayableHome/Binaries/Linux/libUnrealEditor-VistaPlayableHomeEditor.so").is_file()
    assert (attempt / "project/Content/Characters/Mannequins/Meshes/SKM_Manny.uasset").is_file()
    assert (attempt / "project/Config/DefaultInput.ini").read_bytes() == planned.input_ini_raw
    assert (attempt / "contracts/build-plan.json").read_bytes() == planning.canonical_json(fixture.plan)
    assert (attempt / "execution.json").read_bytes() == planned.execution_raw
    assert sum(copy_counts.values()) == fixture.plugin_snapshot.file_count + fixture.characters_snapshot.file_count


def test_full_visual_manifest_overrides_only_the_presentation_source(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset_id = "asset.prop.sofa"
    visual_path, visual_sha, output = fixture.visual_manifest(asset_id)
    expected_inspection = json.loads(visual_path.read_text())["outputs"][0]["inspection"]
    monkeypatch.setattr(build_home.hssd_contract, "inspect_glb", lambda _path: expected_inspection)
    config = fixture.config()
    config = build_home.BuildConfig(
        **{
            **config.__dict__,
            "visual_binding_manifest": visual_path,
            "visual_binding_manifest_sha256": visual_sha,
        }
    )
    planned = build_home.plan_build(config)
    binding = next(item for item in planned.bindings if item["asset_id"] == asset_id)
    source_asset = next(item for item in fixture.plan["assets"] if item["asset_id"] == asset_id)
    assert binding["source_file"] == str(output)
    assert binding["source_file_sha256"] == build_home.sha256_file(output)
    assert binding["source_binding_digest"] == source_asset["source_digest"]

    value = json.loads(visual_path.read_text())
    value["outputs"][0]["logical_asset_id"] = "asset.unknown"
    value["closed_world"]["bound_asset_ids"] = ["asset.unknown"]
    value = build_home.hssd_contract.seal_document(value)
    visual_path.write_bytes(build_home.hssd_contract.canonical_json_bytes(value))
    invalid = build_home.BuildConfig(
        **{
            **fixture.config().__dict__,
            "visual_binding_manifest": visual_path,
            "visual_binding_manifest_sha256": build_home.sha256_file(visual_path),
        }
    )
    with pytest.raises(build_home.BuildHomeError, match="unknown or builtin"):
        build_home.plan_build(invalid)


def test_visual_manifest_refuses_planner_blender_geometry_receipt_drift(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    visual_path, _visual_sha, _output = fixture.visual_manifest("asset.prop.sofa")
    value = json.loads(visual_path.read_text())
    expected_inspection = value["outputs"][0]["inspection"]
    monkeypatch.setattr(build_home.hssd_contract, "inspect_glb", lambda _path: expected_inspection)

    value["outputs"][0]["normalization"]["source_dimensions_match_plan"] = False
    value = build_home.hssd_contract.seal_document(value)
    visual_path.write_bytes(build_home.hssd_contract.canonical_json_bytes(value))
    config = fixture.config()
    config = build_home.BuildConfig(
        **{
            **config.__dict__,
            "visual_binding_manifest": visual_path,
            "visual_binding_manifest_sha256": build_home.sha256_file(visual_path),
        }
    )
    with pytest.raises(build_home.BuildHomeError, match="normalization receipt differs"):
        build_home.plan_build(config)


def test_tamper_and_path_escape_fail_before_attempt_creation(fixture: Fixture) -> None:
    first = next(iter(fixture.blender_manifest["asset_artifacts"].values()))
    artifact = fixture.blender_root / first["path"]
    artifact.write_bytes(artifact.read_bytes() + b"tamper")
    with pytest.raises(build_home.BuildHomeError, match="bytes or SHA-256 differ"):
        build_home.plan_build(fixture.config())
    assert not fixture.attempt.exists()

    artifact.write_bytes(artifact.read_bytes()[:-6])
    outside = fixture.root / "outside.glb"
    outside.write_bytes(b"outside")
    first["path"] = "../outside.glb"
    first["sha256"] = build_home.sha256_file(outside)
    first["bytes"] = outside.stat().st_size
    fixture.blender_manifest_path.write_bytes(
        blender_contract.canonical_json_bytes(fixture.blender_manifest)
    )
    config = fixture.config()
    config = build_home.BuildConfig(
        **{
            **config.__dict__,
            "blender_manifest_sha256": build_home.sha256_file(fixture.blender_manifest_path),
        }
    )
    with pytest.raises(build_home.BuildHomeError, match="traversal"):
        build_home.plan_build(config)
    assert not fixture.attempt.exists()


def _successful_import_receipt(planned: build_home.PlannedBuild) -> dict:
    bindings = {item["asset_id"]: item for item in planned.execution["artifact_bindings"]}
    imported = []
    for asset in planned.plan["assets"]:
        object_path = build_home.derived_asset_path(planned.plan["unreal"]["content_namespace"], asset)
        item = {
            "asset_id": asset["asset_id"],
            "source_kind": asset["source_kind"],
            "uri": asset["uri"],
            "source_digest": asset["source_digest"],
            "object_path": object_path,
            "inspection": {
                "object_path": object_path,
                "class_path": "/Script/Engine.StaticMesh",
                "collision_policies": [],
                "material_paths": [],
                "simple_collision_shapes": None,
                "collision_generated": False,
                "collision_trace_flag": None,
                "room_shell": False,
                "declared_core_texture_count": 0,
                "returned_texture2d_paths": [],
                "material_texture2d_paths": [],
                "material_blend_modes": [],
                "nanite_policy": (
                    "not_applicable" if asset["source_kind"] == "builtin"
                    else "eligible_static_opaque"
                ),
                "nanite_enabled": (
                    None if asset["source_kind"] == "builtin" else True
                ),
            },
        }
        if asset["source_kind"] != "builtin":
            item["inspection"]["material_paths"] = [
                object_path.rsplit(".", 1)[0] + "/Materials/M_Opaque.M_Opaque"
            ]
            item["inspection"]["material_blend_modes"] = ["BLEND_OPAQUE"]
            item.update(
                {
                    "source_file_sha256": bindings[asset["asset_id"]]["source_file_sha256"],
                    "raw_returned_object_paths": [object_path],
                    "returned_object_paths": [object_path],
                }
            )
        imported.append(item)
    return {
        "schema_version": IMPORT_RECEIPT_SCHEMA,
        "status": "imported_candidate",
        "error": None,
        "bindings": {
            "engine": "5.7.3-test",
            "project": planned.execution["project_file"],
            "execution_manifest": str(planned.config.attempt_root / "execution.json"),
            "execution_manifest_sha256": planned.execution_sha256,
            "build_plan_sha256": planned.execution["build_plan_sha256"],
            "composition_spec_sha256": planned.execution["composition_spec_sha256"],
        },
        "content_namespace": planned.plan["unreal"]["content_namespace"],
        "assets": imported,
        "gates": {
            "namespace_fresh": True,
            "all_assets_bound": True,
            "material_and_collision_inspected": True,
            "core_textures_imported_and_used": True,
            "nanite_material_policy_verified": True,
            "quarantined": False,
        },
    }


def test_import_receipt_requires_returned_texture_used_by_material(fixture: Fixture) -> None:
    planned = build_home.plan_build(fixture.config())
    receipt = _successful_import_receipt(planned)
    imported = next(item for item in receipt["assets"] if item["source_kind"] != "builtin")
    texture_path = imported["object_path"].rsplit(".", 1)[0] + "/Textures/T_BaseColor.T_BaseColor"
    imported["returned_object_paths"].append(texture_path)
    imported["returned_object_paths"].sort()
    imported["inspection"].update(
        {
            "declared_core_texture_count": 1,
            "returned_texture2d_paths": [texture_path],
            "material_texture2d_paths": [texture_path],
        }
    )

    build_home._verify_import_receipt(receipt, planned.execution, planned.plan)

    imported["inspection"]["material_texture2d_paths"] = []
    with pytest.raises(build_home.BuildHomeError, match="no imported Texture2D used"):
        build_home._verify_import_receipt(receipt, planned.execution, planned.plan)

    imported["inspection"]["material_texture2d_paths"] = [texture_path]
    imported["returned_object_paths"].remove(texture_path)
    with pytest.raises(build_home.BuildHomeError, match="binding differs"):
        build_home._verify_import_receipt(receipt, planned.execution, planned.plan)

    imported["returned_object_paths"].append(texture_path)
    imported["inspection"]["returned_texture2d_paths"] = [{}]
    with pytest.raises(build_home.BuildHomeError, match="fields differ"):
        build_home._verify_import_receipt(receipt, planned.execution, planned.plan)


def test_import_receipt_enforces_nonopaque_nanite_exclusion(fixture: Fixture) -> None:
    planned = build_home.plan_build(fixture.config())
    receipt = _successful_import_receipt(planned)
    imported = next(item for item in receipt["assets"] if item["source_kind"] != "builtin")
    inspection = imported["inspection"]

    inspection["nanite_enabled"] = False
    with pytest.raises(build_home.BuildHomeError, match="Nanite/material policy differs"):
        build_home._verify_import_receipt(receipt, planned.execution, planned.plan)

    inspection["nanite_enabled"] = True
    inspection["material_blend_modes"] = ["BLEND_TRANSLUCENT"]
    inspection["nanite_policy"] = "disabled_nonopaque_material"
    inspection["nanite_enabled"] = False

    build_home._verify_import_receipt(receipt, planned.execution, planned.plan)

    inspection["nanite_enabled"] = True
    with pytest.raises(build_home.BuildHomeError, match="Nanite/material policy differs"):
        build_home._verify_import_receipt(receipt, planned.execution, planned.plan)

    inspection["nanite_enabled"] = False
    inspection["nanite_policy"] = "eligible_static_opaque"
    with pytest.raises(build_home.BuildHomeError, match="Nanite/material policy differs"):
        build_home._verify_import_receipt(receipt, planned.execution, planned.plan)


def _successful_scene_receipt(planned: build_home.PlannedBuild, import_sha: str) -> dict:
    return {
        "schema_version": SCENE_RECEIPT_SCHEMA,
        "status": "saved_reloaded_candidate",
        "error": None,
        "bindings": {
            "engine": "5.7.3-test",
            "project": planned.execution["project_file"],
            "execution_manifest": str(planned.config.attempt_root / "execution.json"),
            "execution_manifest_sha256": planned.execution_sha256,
            "import_receipt": planned.execution["import_receipt"],
            "import_receipt_sha256": import_sha,
            "composition_spec_sha256": planned.execution["composition_spec_sha256"],
            "input_config": str(
                pathlib.Path(planned.execution["project_file"]).parent
                / "Config"
                / "DefaultInput.ini"
            ),
            "input_config_sha256": build_home.sha256_bytes(
                build_home.default_input_ini()
            ),
        },
        "content_namespace": planned.plan["unreal"]["content_namespace"],
        "map_path": planned.plan["unreal"]["map_path"],
        "actor_inventory": [],
        "gates": {
            "map_saved": True,
            "map_reloaded": True,
            "semantic_tags_verified": True,
            "player_start_verified": True,
            "game_mode_configured": True,
            "navmesh_bounds_verified": True,
            "dynamic_lighting_verified": True,
            "deterministic_exposure_verified": True,
            "input_mappings_verified": True,
            "quarantined": False,
            "runtime_play_proof": "pending",
        },
    }


def test_scene_receipt_rejects_untrusted_input_contract(fixture: Fixture) -> None:
    planned = build_home.plan_build(fixture.config())
    input_config = (
        pathlib.Path(planned.execution["project_file"]).parent
        / "Config"
        / "DefaultInput.ini"
    )
    _write(input_config, planned.input_ini_raw)
    import_sha = "a" * 64
    receipt = _successful_scene_receipt(planned, import_sha)

    build_home._verify_scene_receipt(receipt, planned.execution, planned.plan, import_sha)

    wrong_path = copy.deepcopy(receipt)
    wrong_path["bindings"]["input_config"] = str(input_config.with_name("Input.ini"))
    with pytest.raises(build_home.BuildHomeError, match="scene receipt pins differ"):
        build_home._verify_scene_receipt(wrong_path, planned.execution, planned.plan, import_sha)

    wrong_sha = copy.deepcopy(receipt)
    wrong_sha["bindings"]["input_config_sha256"] = "b" * 64
    with pytest.raises(build_home.BuildHomeError, match="scene receipt pins differ"):
        build_home._verify_scene_receipt(wrong_sha, planned.execution, planned.plan, import_sha)

    false_gate = copy.deepcopy(receipt)
    false_gate["gates"]["input_mappings_verified"] = False
    with pytest.raises(build_home.BuildHomeError, match="scene receipt gates did not pass"):
        build_home._verify_scene_receipt(false_gate, planned.execution, planned.plan, import_sha)


def _successful_commandlet_runner(
    fixture: Fixture,
    planned: build_home.PlannedBuild,
    observed: list[tuple[str, dict[str, str]]] | None = None,
):
    def fake_run(**kwargs):
        phase = kwargs["phase"]
        environment = dict(kwargs["environment"])
        if observed is not None:
            observed.append((phase, environment))
        if phase == "import":
            receipt = _successful_import_receipt(planned)
            path = fixture.attempt / "import-receipt.json"
            build_home._write_exclusive(path, planning.canonical_json(receipt))
            digest = build_home.sha256_file(path)
            return {"status": "imported_candidate", "receipt": str(path), "sha256": digest}
        import_sha = build_home.sha256_file(fixture.attempt / "import-receipt.json")
        receipt = _successful_scene_receipt(planned, import_sha)
        path = fixture.attempt / "scene-receipt.json"
        build_home._write_exclusive(path, planning.canonical_json(receipt))
        digest = build_home.sha256_file(path)
        return {"status": "saved_reloaded_candidate", "receipt": str(path), "sha256": digest}

    return fake_run


def test_apply_sequences_receipts_then_publishes_pointers(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    editor, editor_sha = fixture.editor()
    planned = build_home.plan_build(fixture.config(editor=editor, editor_sha=editor_sha), require_editor=True)
    observed: list[tuple[str, dict[str, str]]] = []
    monkeypatch.setattr(build_home, "_run_command", _successful_commandlet_runner(fixture, planned, observed))
    result = build_home.apply_build(planned)

    assert result["status"] == "accepted_candidate"
    assert [item[0] for item in observed] == ["import", "compose"]
    assert "VISTA_PLAYABLE_HOME_IMPORT_RECEIPT_SHA256" not in observed[0][1]
    assert observed[1][1]["VISTA_PLAYABLE_HOME_IMPORT_RECEIPT_SHA256"] == result["import_receipt_sha256"]
    runtime_root = fixture.attempt / build_home.COMMANDLET_RUNTIME_DIRECTORY
    assert observed[0][1]["HOME"] == str(runtime_root / "import/home")
    assert observed[1][1]["HOME"] == str(runtime_root / "compose/home")
    assert (runtime_root / "ddc").is_dir()
    assert (runtime_root / "import/tmp").is_dir()
    assert (runtime_root / "compose/xdg-config").is_dir()
    accepted = (fixture.run_root / "ue/accepted.json").read_bytes()
    current = (fixture.run_root / "ue/current.json").read_bytes()
    assert accepted == current
    pointer = json.loads(accepted)
    assert pointer["attempt"] == fixture.attempt.name
    assert pointer["result_receipt_sha256"] == build_home.sha256_file(
        fixture.attempt / "result-receipt.json"
    )


def test_failed_apply_quarantines_attempt_without_changing_existing_pointers(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    editor, editor_sha = fixture.editor()
    ue_root = fixture.run_root / "ue"
    ue_root.mkdir()
    previous = build_home.canonical_json({"schema_version": "previous", "attempt": "attempt-old"})
    (ue_root / "accepted.json").write_bytes(previous)
    (ue_root / "current.json").write_bytes(previous)
    planned = build_home.plan_build(fixture.config(editor=editor, editor_sha=editor_sha), require_editor=True)

    def fail_run(**_kwargs):
        raise build_home.BuildHomeError("VISTA_HOME_BUILD_TEST_FAILURE", "synthetic command failure")

    monkeypatch.setattr(build_home, "_run_command", fail_run)
    with pytest.raises(build_home.BuildHomeError, match="synthetic command failure"):
        build_home.apply_build(planned)

    assert (ue_root / "accepted.json").read_bytes() == previous
    assert (ue_root / "current.json").read_bytes() == previous
    failure = json.loads((fixture.attempt / "result-receipt.json").read_text())
    assert failure["status"] == "failed_quarantined"
    assert failure["error"]["code"] == "VISTA_HOME_BUILD_TEST_FAILURE"


def test_pointer_publication_rolls_back_if_second_pointer_fails(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ue_root = tmp_path / "ue"
    ue_root.mkdir()
    previous_accepted = build_home.canonical_json({"generation": "accepted-old"})
    previous_current = build_home.canonical_json({"generation": "current-old"})
    (ue_root / "accepted.json").write_bytes(previous_accepted)
    (ue_root / "current.json").write_bytes(previous_current)
    original = build_home._atomic_pointer

    def fail_current(path: pathlib.Path, value):
        if path.name == "current.json":
            raise build_home.BuildHomeError("VISTA_HOME_BUILD_TEST_POINTER", "synthetic pointer failure")
        original(path, value)

    monkeypatch.setattr(build_home, "_atomic_pointer", fail_current)
    with pytest.raises(build_home.BuildHomeError, match="synthetic pointer failure"):
        build_home._publish_pointers_transactionally(
            ue_root,
            {"schema_version": build_home.POINTER_SCHEMA, "attempt": "attempt-new"},
        )
    assert (ue_root / "accepted.json").read_bytes() == previous_accepted
    assert (ue_root / "current.json").read_bytes() == previous_current


def test_pointer_publication_holds_exclusive_lock(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ue_root = tmp_path / "ue"
    ue_root.mkdir()
    locked = False
    observed_during_write: list[bool] = []
    original_flock = build_home.fcntl.flock
    original_pointer = build_home._atomic_pointer

    def tracked_flock(descriptor: int, operation: int):
        nonlocal locked
        result = original_flock(descriptor, operation)
        if operation == build_home.fcntl.LOCK_EX:
            locked = True
        elif operation == build_home.fcntl.LOCK_UN:
            locked = False
        return result

    def tracked_pointer(path: pathlib.Path, value):
        observed_during_write.append(locked)
        original_pointer(path, value)

    monkeypatch.setattr(build_home.fcntl, "flock", tracked_flock)
    monkeypatch.setattr(build_home, "_atomic_pointer", tracked_pointer)
    build_home._publish_pointers_transactionally(
        ue_root,
        {"schema_version": build_home.POINTER_SCHEMA, "attempt": "attempt-new"},
    )
    assert observed_during_write == [True, True]
    assert locked is False
    assert (ue_root / ".publication.lock").is_file()


def test_foreign_attempt_race_is_not_polluted(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    editor, editor_sha = fixture.editor()
    planned = build_home.plan_build(fixture.config(editor=editor, editor_sha=editor_sha), require_editor=True)

    def foreign_race(_planned, *, owner_token):
        assert owner_token
        fixture.attempt.mkdir(parents=True)
        (fixture.attempt / "foreign.txt").write_text("owned elsewhere")
        raise build_home.BuildHomeError("VISTA_HOME_BUILD_ATTEMPT_EXISTS", "synthetic foreign race")

    monkeypatch.setattr(build_home, "_materialize_inputs", foreign_race)
    with pytest.raises(build_home.BuildHomeError, match="synthetic foreign race"):
        build_home.apply_build(planned)
    assert sorted(path.name for path in fixture.attempt.iterdir()) == ["foreign.txt"]


def test_publication_failure_is_evidenced_and_old_pointers_survive(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    editor, editor_sha = fixture.editor()
    ue_root = fixture.run_root / "ue"
    ue_root.mkdir()
    previous = build_home.canonical_json({"schema_version": "previous", "attempt": "attempt-old"})
    (ue_root / "accepted.json").write_bytes(previous)
    (ue_root / "current.json").write_bytes(previous)
    planned = build_home.plan_build(fixture.config(editor=editor, editor_sha=editor_sha), require_editor=True)
    monkeypatch.setattr(build_home, "_run_command", _successful_commandlet_runner(fixture, planned))
    original = build_home._atomic_pointer

    def fail_current(path: pathlib.Path, value):
        if path.name == "current.json":
            raise build_home.BuildHomeError("VISTA_HOME_BUILD_TEST_POINTER", "synthetic pointer failure")
        original(path, value)

    monkeypatch.setattr(build_home, "_atomic_pointer", fail_current)
    with pytest.raises(build_home.BuildHomeError, match="synthetic pointer failure"):
        build_home.apply_build(planned)
    assert (ue_root / "accepted.json").read_bytes() == previous
    assert (ue_root / "current.json").read_bytes() == previous
    result = json.loads((fixture.attempt / "result-receipt.json").read_text())
    failure = json.loads((fixture.attempt / "publication-failure.json").read_text())
    assert result["status"] == "accepted_candidate"
    assert failure["status"] == "publication_failed_quarantined"
    assert failure["result_receipt_sha256"] == build_home.sha256_file(fixture.attempt / "result-receipt.json")


def test_run_command_reaps_child_group_on_keyboard_interrupt(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals: list[int] = []

    class InterruptedProcess:
        pid = 43210

        def __init__(self):
            self.returncode = None
            self.wait_calls = 0

        def wait(self, timeout):
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise KeyboardInterrupt
            self.returncode = -build_home.signal.SIGTERM
            return self.returncode

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -build_home.signal.SIGTERM

        def kill(self):
            self.returncode = -build_home.signal.SIGKILL

    process = InterruptedProcess()
    monkeypatch.setattr(build_home.subprocess, "Popen", lambda *_args, **_kwargs: process)

    def killpg(_pid: int, sig: int):
        signals.append(sig)
        process.returncode = -sig

    monkeypatch.setattr(build_home.os, "killpg", killpg)
    with pytest.raises(KeyboardInterrupt):
        build_home._run_command(
            phase="import",
            argv=["/bin/false"],
            environment={},
            log_path=tmp_path / "interrupted.log",
            marker_prefix="marker:",
            timeout_s=60,
        )
    assert signals == [build_home.signal.SIGTERM]
    assert process.poll() is not None


def test_run_command_accepts_marker_inside_unreal_log_prefix(tmp_path: pathlib.Path) -> None:
    payload = {"status": "ok", "receipt": "/tmp/receipt.json", "sha256": "a" * 64}
    marker_prefix = "VISTA_TEST_RESULT:"
    script = (
        "import json; "
        f"print('[2026.08.15]LogPython: {marker_prefix}' + "
        f"json.dumps({payload!r}, sort_keys=True), flush=True)"
    )
    marker = build_home._run_command(
        phase="test",
        argv=[sys.executable, "-c", script],
        environment={},
        log_path=tmp_path / "prefixed-marker.log",
        marker_prefix=marker_prefix,
        timeout_s=60,
    )
    assert marker == payload


def test_run_command_sanitizes_ambient_vulkan_driver_selectors(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker_prefix = "VISTA_TEST_VULKAN_ENV:"
    for key in build_home.VULKAN_DRIVER_ENVIRONMENT_KEYS:
        monkeypatch.setenv(key, f"ambient-{key}")
    script = (
        "import json,os; "
        f"keys={build_home.VULKAN_DRIVER_ENVIRONMENT_KEYS!r}; "
        f"print({marker_prefix!r}+json.dumps({{k:os.environ.get(k) for k in keys}}, "
        "sort_keys=True), flush=True)"
    )
    sanitized = build_home._run_command(
        phase="test",
        argv=[sys.executable, "-c", script],
        environment={},
        log_path=tmp_path / "vulkan-sanitized.log",
        marker_prefix=marker_prefix,
        timeout_s=60,
    )
    assert sanitized == {
        key: None for key in build_home.VULKAN_DRIVER_ENVIRONMENT_KEYS
    }

    explicit_icd = "/attempt/contracts/presentation-vulkan-icd.json"
    selected = build_home._run_command(
        phase="test",
        argv=[sys.executable, "-c", script],
        environment={build_home.PRESENTATION_VULKAN_ICD_ENV: explicit_icd},
        log_path=tmp_path / "vulkan-selected.log",
        marker_prefix=marker_prefix,
        timeout_s=60,
    )
    assert selected[build_home.PRESENTATION_VULKAN_ICD_ENV] == explicit_icd
    assert all(
        selected[key] is None
        for key in build_home.VULKAN_DRIVER_ENVIRONMENT_KEYS
        if key != build_home.PRESENTATION_VULKAN_ICD_ENV
    )


def test_run_command_prefers_exclusive_result_file(tmp_path: pathlib.Path) -> None:
    payload = {"status": "ok", "receipt": "/tmp/receipt.json", "sha256": "b" * 64}
    marker_path = tmp_path / "result.json"
    script = (
        "import json, os; "
        f"raw=(json.dumps({payload!r}, sort_keys=True, separators=(',', ':'))+'\\n').encode(); "
        f"fd=os.open({str(marker_path)!r}, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600); "
        "os.write(fd, raw); os.fsync(fd); os.close(fd)"
    )
    marker = build_home._run_command(
        phase="test",
        argv=[sys.executable, "-c", script],
        environment={},
        log_path=tmp_path / "process.log",
        marker_prefix="VISTA_TEST_RESULT:",
        timeout_s=60,
        marker_path=marker_path,
    )
    assert marker == payload


def test_run_command_rejects_world_readable_result_file(tmp_path: pathlib.Path) -> None:
    marker_path = tmp_path / "result.json"
    script = (
        "import json, os, pathlib; "
        f"path=pathlib.Path({str(marker_path)!r}); "
        "path.write_text(json.dumps({'status':'ok'}, separators=(',', ':'))+'\\n'); "
        "os.chmod(path, 0o644)"
    )
    with pytest.raises(build_home.BuildHomeError, match="unsafe type, size, provenance, links, or permissions"):
        build_home._run_command(
            phase="test",
            argv=[sys.executable, "-c", script],
            environment={},
            log_path=tmp_path / "process.log",
            marker_prefix="VISTA_TEST_RESULT:",
            timeout_s=60,
            marker_path=marker_path,
        )


def test_run_command_does_not_fallback_when_bound_result_is_missing(tmp_path: pathlib.Path) -> None:
    prefix = "VISTA_TEST_RESULT:"
    payload = {"status": "stdout-only", "receipt": "/tmp/wrong", "sha256": "c" * 64}
    script = f"import json; print({prefix!r}+json.dumps({payload!r}), flush=True)"
    with pytest.raises(build_home.BuildHomeError, match="did not publish its result marker"):
        build_home._run_command(
            phase="test",
            argv=[sys.executable, "-c", script],
            environment={},
            log_path=tmp_path / "process.log",
            marker_prefix=prefix,
            timeout_s=60,
            marker_path=tmp_path / "missing-result.json",
        )


def test_run_command_uses_bound_result_over_conflicting_stdout(tmp_path: pathlib.Path) -> None:
    prefix = "VISTA_TEST_RESULT:"
    stdout_payload = {"status": "wrong", "receipt": "/tmp/wrong", "sha256": "d" * 64}
    file_payload = {"status": "ok", "receipt": "/tmp/right", "sha256": "e" * 64}
    marker_path = tmp_path / "result.json"
    script = (
        "import json, os; "
        f"print({prefix!r}+json.dumps({stdout_payload!r}), flush=True); "
        f"raw=(json.dumps({file_payload!r}, sort_keys=True, separators=(',', ':'))+'\\n').encode(); "
        f"fd=os.open({str(marker_path)!r}, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600); "
        "os.write(fd, raw); os.fsync(fd); os.close(fd)"
    )
    marker = build_home._run_command(
        phase="test",
        argv=[sys.executable, "-c", script],
        environment={},
        log_path=tmp_path / "process.log",
        marker_prefix=prefix,
        timeout_s=60,
        marker_path=marker_path,
    )
    assert marker == file_payload


@pytest.mark.parametrize(
    "patch",
    [
        {"status": "wrong"},
        {"receipt": "/tmp/wrong"},
        {"sha256": "f" * 64},
    ],
)
def test_verify_marker_rejects_wrong_binding(patch: dict[str, str]) -> None:
    receipt = pathlib.Path("/tmp/right")
    expected = {"status": "ok", "receipt": str(receipt), "sha256": "a" * 64}
    with pytest.raises(build_home.BuildHomeError, match="marker disagrees"):
        build_home._verify_marker(
            {**expected, **patch},
            status="ok",
            receipt=receipt,
            sha256="a" * 64,
            phase="test",
        )


def test_source_is_static_and_does_not_accept_caller_python() -> None:
    path = ROOT / "tools/ue/vista_playable_home/build_home.py"
    source = path.read_text(encoding="utf-8")
    compile(source, str(path), "exec")
    assert "execute_python_script" not in source
    assert "shell=True" not in source
    assert 'with_name("import_assets_commandlet.py")' in source
    assert 'with_name("compose_home_commandlet.py")' in source
    assert 'with_name("commandlet_common.py")' in source
    assert "hssd_contract.inspect_glb" in source


def test_project_uses_runtime_dynamic_navigation(fixture: Fixture) -> None:
    raw = build_home.default_engine_ini(fixture.plan).decode("utf-8")
    assert "[/Script/NavigationSystem.RecastNavMesh]" in raw
    assert "RuntimeGeneration=Dynamic\n" in raw
    assert "DynamicModifiersOnly" not in raw
    assert "[/Script/Engine.RendererSettings]" in raw
    assert "r.AllowStaticLighting=False\n" in raw


def test_project_persists_fixed_gameplay_input_contract() -> None:
    raw = build_home.default_input_ini().decode("utf-8")
    assert raw.startswith("[/Script/Engine.InputSettings]\n")
    assert "bCaptureMouseOnLaunch=True\n" in raw
    assert "DefaultViewportMouseCaptureMode=CapturePermanently_IncludingInitialMouseDown\n" in raw
    for value in (
        'AxisName="MoveForward",Scale=1.000000,Key=W',
        'AxisName="MoveRight",Scale=-1.000000,Key=A',
        'AxisName="Turn",Scale=1.000000,Key=MouseX',
        'ActionName="Interact",bShift=False,bCtrl=False,bAlt=False,bCmd=False,Key=E',
        'ActionName="Drop",bShift=False,bCtrl=False,bAlt=False,bCmd=False,Key=Q',
    ):
        assert value in raw
