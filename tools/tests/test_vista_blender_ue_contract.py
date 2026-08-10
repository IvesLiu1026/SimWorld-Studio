from __future__ import annotations

import contextlib
import hashlib
import io
import json
import pathlib
import py_compile
import stat
import sys
import tempfile
import unittest

import jsonschema


TOOLS_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_ROOT))

from ue.vista_blender_world import contract  # noqa: E402


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def minimal_glb() -> bytes:
    document = b'{"asset":{"version":"2.0"}}'
    document += b" " * ((4 - len(document) % 4) % 4)
    chunk = len(document).to_bytes(4, "little") + b"JSON" + document
    total = 12 + len(chunk)
    return b"glTF" + (2).to_bytes(4, "little") + total.to_bytes(4, "little") + chunk


class Fixture:
    def __init__(self, root: pathlib.Path) -> None:
        self.root = root
        self.root.chmod(0o700)
        self.run_root = root / "run-r1"
        self.run_root.mkdir(mode=0o700)
        self.attempt_root = self.run_root / "ue" / "attempt-r1"

        self.source_project = root / "sources" / "disposable-project-r7" / "gym_citynav"
        self.source_project.mkdir(parents=True)
        self.project_file = self.source_project / contract.PROJECT_FILE_NAME
        descriptor = {
            "FileVersion": 3,
            "Plugins": [
                {"Name": "UnrealMCP", "Enabled": False},
                {"Name": "PixelStreaming", "Enabled": False},
                {"Name": "EditorScriptingUtilities", "Enabled": True},
                {"Name": "PythonScriptPlugin", "Enabled": True},
            ],
        }
        self.project_file.write_bytes(contract.canonical_json(descriptor))
        self.map_file = self.source_project / contract.SOURCE_MAP_RELATIVE
        self.map_file.parent.mkdir(parents=True)
        self.map_file.write_bytes(b"synthetic pinned R3 map")
        config = self.source_project / "Config/DefaultEngine.ini"
        config.parent.mkdir()
        config.write_text("[/Script/Engine.Engine]\n", encoding="utf-8")
        excluded = self.source_project / "DerivedDataCache/ignored.bin"
        excluded.parent.mkdir()
        excluded.write_bytes(b"not copied")
        self.source_pins = contract.SourcePins(
            project_sha256=contract.sha256_file(self.project_file),
            map_sha256=contract.sha256_file(self.map_file),
        )

        engine_root = root / "runtime/Engine"
        self.editor = engine_root / "Binaries/Linux/UnrealEditor-Cmd"
        self.editor.parent.mkdir(parents=True)
        self.editor.write_bytes(b"synthetic UE commandlet")
        self.editor.chmod(0o755)
        version = {
            "MajorVersion": 5,
            "MinorVersion": 3,
            "PatchVersion": 2,
            "Changelist": 29314046,
            "BranchName": "++UE5+Release-5.3",
        }
        (self.editor.parent / "UnrealEditor.version").write_bytes(contract.canonical_json(version))
        self.translator = engine_root / "Plugins/Interchange/Runtime/Binaries/Linux/libUnrealEditor-InterchangeImport.so"
        self.translator.parent.mkdir(parents=True)
        self.translator.write_bytes(
            b"binary-prefix"
            + "gltf;GL Transmission Format".encode("utf-16le")
            + b"middle"
            + "glb;GL Transmission Format (Binary)".encode("utf-16le")
        )
        self.engine_pins = contract.EnginePins(
            editor_sha256=contract.sha256_file(self.editor),
            translator_sha256=contract.sha256_file(self.translator),
        )

        self.blender_root = self.run_root / "blender"
        self.blender_root.mkdir()
        self.glb = self.blender_root / "vista_mmg040_office.glb"
        self.glb.write_bytes(minimal_glb())
        self.gltf = self.blender_root / "vista_mmg040_office.gltf"
        self.binary = self.blender_root / "vista_mmg040_office.bin"
        self.binary.write_bytes(b"synthetic geometry payload")
        self.gltf.write_bytes(
            contract.canonical_json(
                {
                    "asset": {"version": "2.0"},
                    "buffers": [{"uri": self.binary.name, "byteLength": self.binary.stat().st_size}],
                    "meshes": [],
                }
            )
        )
        self.manifest_path = self.blender_root / "manifest.json"
        self.manifest = self.valid_manifest()
        self.refresh_manifest()

    @staticmethod
    def output(path: pathlib.Path, media_type: str) -> dict:
        return {
            "path": path.name,
            "sha256": contract.sha256_file(path),
            "bytes": path.stat().st_size,
            "media_type": media_type,
        }

    def valid_manifest(self) -> dict:
        return {
            "schema": contract.BLENDER_MANIFEST_SCHEMA,
            "outputs": {
                "glb": self.output(self.glb, "model/gltf-binary"),
                "gltf": self.output(self.gltf, "model/gltf+json"),
                "gltf_bin": self.output(self.binary, "application/octet-stream"),
            },
            "assets": {
                "tall_office_cabinet": {
                    "collection": "VISTA_TallOfficeCabinet",
                    "bounds": {"dimensions_m": [1.2, 0.5, 2.4]},
                    "origin_m": [0.0, 2.14, 0.0],
                    "ue_placement": {
                        "location_cm": [514.0, 0.0, 0.0],
                        "rotation_deg": [0.0, 0.0, 0.0],
                        "scale": [1.0, 1.0, 1.0],
                    },
                    "mesh_names": ["VISTA_Cabinet_Carcass", "VISTA_Cabinet_Doors"],
                    "material_names": ["VISTA_Mat_Cabinet", "VISTA_Mat_Handle"],
                },
                "room_shell_kit": {
                    "collection": "VISTA_RoomShell",
                    "bounds": {"dimensions_m": [8.0, 8.0, 3.0]},
                    "origin_m": [0.0, 0.0, 0.0],
                    "ue_placement": {
                        "location_cm": [300.0, 0.0, 0.0],
                        "rotation_deg": [0.0, 0.0, 0.0],
                        "scale": [1.0, 1.0, 1.0],
                    },
                    "mesh_names": ["VISTA_Room_Floor", "VISTA_Room_Walls"],
                    "material_names": ["VISTA_Mat_Floor", "VISTA_Mat_Walls"],
                },
                "ergonomic_office_chair": {
                    "collection": "VISTA_ErgonomicOfficeChair",
                    "bounds": {"dimensions_m": [1.14, 1.22, 1.23]},
                    "origin_m": [-0.4, -0.8, 0.0],
                    "ue_placement": {
                        "location_cm": [220.0, -40.0, 0.0],
                        "rotation_deg": [0.0, 0.0, 0.0],
                        "scale": [1.0, 1.0, 1.0],
                    },
                    "mesh_names": [
                        "VISTA_Chair_SeatCushion_Mesh",
                        "VISTA_Chair_BaseHub_Mesh",
                    ],
                    "material_names": [
                        "VISTA_M_ChairSeatFabric",
                        "VISTA_M_ChairGlassNylon",
                    ],
                }
            },
        }

    def refresh_manifest(self) -> None:
        self.manifest_path.write_bytes(contract.canonical_json(self.manifest))

    def prepare(self, **overrides) -> contract.PreparedPlan:
        values = {
            "source_project": self.source_project,
            "run_root": self.run_root,
            "attempt_root": self.attempt_root,
            "blender_manifest": self.manifest_path,
            "unreal_editor_cmd": self.editor,
            "source_pins": self.source_pins,
            "engine_pins": self.engine_pins,
        }
        values.update(overrides)
        return contract.build_plan(**values)


class VistaBlenderUEContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.fixture = Fixture(pathlib.Path(self.temporary.name))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def assert_contract_error(self, code: str, callback) -> contract.VistaBlenderUEContractError:
        with self.assertRaises(contract.VistaBlenderUEContractError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_dry_run_builds_exact_plan_and_writes_nothing(self) -> None:
        before = sorted(path.relative_to(self.fixture.root) for path in self.fixture.root.rglob("*"))
        prepared = self.fixture.prepare()
        after = sorted(path.relative_to(self.fixture.root) for path in self.fixture.root.rglob("*"))
        self.assertEqual(before, after)
        self.assertFalse(self.fixture.attempt_root.exists())
        self.assertEqual(prepared.plan["unreal"]["import_content_root"], contract.IMPORT_CONTENT_ROOT)
        self.assertEqual(prepared.plan["unreal"]["source_map"], contract.SOURCE_MAP)
        self.assertEqual(prepared.plan["unreal"]["output_map"], contract.OUTPUT_MAP)
        self.assertFalse(prepared.plan["policy"]["studio_socket_fallback_allowed"])
        self.assertFalse(prepared.plan["policy"]["r8_allowed"])

    def test_plan_is_deterministic_and_schema_valid(self) -> None:
        first = self.fixture.prepare()
        second = self.fixture.prepare()
        self.assertEqual(first.plan_bytes, second.plan_bytes)
        self.assertEqual(first.plan_sha256, second.plan_sha256)
        schema_path = TOOLS_ROOT / "ue/vista_blender_world/schemas/preparation-plan.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(schema).validate(first.plan)

    def test_glb_is_preferred_but_gltf_bin_is_also_pinned(self) -> None:
        prepared = self.fixture.prepare()
        blender = prepared.plan["blender"]
        self.assertEqual(blender["selected_import_source"]["format"], "glb")
        gltf = next(entry for entry in blender["available_import_sources"] if entry["format"] == "gltf")
        self.assertEqual([entry["path"] for entry in gltf["external_dependencies"]], [str(self.fixture.binary)])
        capability = prepared.plan["unreal"]["engine"]["static_format_capability"]
        self.assertEqual(capability["glb"], "advertised_by_pinned_packaged_translator")
        self.assertEqual(capability["live_glb_commandlet_observation"], "pending")

    def test_blender_datablock_names_are_explicitly_bound_to_ue_asset_names(self) -> None:
        self.fixture.manifest["assets"]["tall_office_cabinet"]["mesh_names"] = [
            "VISTA_Cabinet_Carcass_Mesh",
            "VISTA_Cabinet_Doors_Mesh",
        ]
        self.fixture.refresh_manifest()
        prepared = self.fixture.prepare()
        cabinet = next(
            asset
            for asset in prepared.plan["blender"]["required_assets"]
            if asset["asset_id"] == "tall_office_cabinet"
        )
        self.assertEqual(
            cabinet["mesh_bindings"],
            [
                {
                    "source_mesh_name": "VISTA_Cabinet_Carcass_Mesh",
                    "ue_asset_name": "VISTA_Cabinet_Carcass",
                },
                {
                    "source_mesh_name": "VISTA_Cabinet_Doors_Mesh",
                    "ue_asset_name": "VISTA_Cabinet_Doors",
                },
            ],
        )

    def test_missing_ergonomic_chair_is_rejected(self) -> None:
        del self.fixture.manifest["assets"]["ergonomic_office_chair"]
        self.fixture.refresh_manifest()
        self.assert_contract_error(
            "VISTA_BLENDER_UE_MANIFEST_INVALID",
            self.fixture.prepare,
        )

    def test_explicit_gltf_fallback_is_pinned_without_silent_retry(self) -> None:
        prepared = self.fixture.prepare(import_format="gltf")
        selected = prepared.plan["blender"]["selected_import_source"]
        self.assertEqual(selected["format"], "gltf")
        self.assertEqual([entry["path"] for entry in selected["external_dependencies"]], [str(self.fixture.binary)])

    def test_gltf_dependency_requires_manifest_pin(self) -> None:
        del self.fixture.manifest["outputs"]["gltf_bin"]
        self.fixture.refresh_manifest()
        self.assert_contract_error("VISTA_BLENDER_UE_GLTF_DEPENDENCY_UNPINNED", self.fixture.prepare)

    def test_r8_source_and_destination_are_explicitly_rejected(self) -> None:
        r8 = self.fixture.source_project.parents[1] / "disposable-project-r8" / "gym_citynav"
        r8.parent.mkdir()
        r8.mkdir()
        self.assert_contract_error(
            "VISTA_BLENDER_UE_R8_REJECTED",
            lambda: self.fixture.prepare(source_project=r8),
        )
        self.assert_contract_error(
            "VISTA_BLENDER_UE_R8_REJECTED",
            lambda: self.fixture.prepare(attempt_root=self.fixture.run_root / "ue/r8/attempt-r1"),
        )

    def test_canonical_archive_release_and_outside_destinations_are_rejected(self) -> None:
        for name in ("canonical", "archive", "releases", "production"):
            with self.subTest(name=name):
                self.assert_contract_error(
                    "VISTA_BLENDER_UE_DESTINATION_FORBIDDEN",
                    lambda name=name: self.fixture.prepare(attempt_root=self.fixture.run_root / f"ue/{name}/attempt-r1"),
                )
        self.assert_contract_error(
            "VISTA_BLENDER_UE_DESTINATION_INVALID",
            lambda: self.fixture.prepare(attempt_root=self.fixture.root / "outside/attempt-r1"),
        )

    def test_source_manifest_engine_and_glb_pins_fail_closed(self) -> None:
        wrong_source = contract.SourcePins(project_sha256="0" * 64, map_sha256=self.fixture.source_pins.map_sha256)
        self.assert_contract_error(
            "VISTA_BLENDER_UE_SOURCE_PIN_MISMATCH",
            lambda: self.fixture.prepare(source_pins=wrong_source),
        )

        self.fixture.manifest["outputs"]["glb"]["sha256"] = "0" * 64
        self.fixture.refresh_manifest()
        self.assert_contract_error("VISTA_BLENDER_UE_ASSET_PIN_MISMATCH", self.fixture.prepare)

    def test_missing_explicit_ue_placement_is_rejected(self) -> None:
        del self.fixture.manifest["assets"]["tall_office_cabinet"]["ue_placement"]
        self.fixture.refresh_manifest()
        self.assert_contract_error("VISTA_BLENDER_UE_MANIFEST_INVALID", self.fixture.prepare)

    def test_engine_translator_must_advertise_both_formats(self) -> None:
        self.fixture.translator.write_bytes("gltf;GL Transmission Format".encode("utf-16le"))
        pins = contract.EnginePins(
            editor_sha256=self.fixture.engine_pins.editor_sha256,
            translator_sha256=contract.sha256_file(self.fixture.translator),
        )
        self.assert_contract_error(
            "VISTA_BLENDER_UE_GLTF_TRANSLATOR_UNAVAILABLE",
            lambda: self.fixture.prepare(engine_pins=pins),
        )

    def test_synthetic_apply_copies_fresh_project_and_preserves_source(self) -> None:
        prepared = self.fixture.prepare()
        before = contract.snapshot_project(self.fixture.source_project)
        receipt = contract.materialize_fresh_project(prepared, approval_ref="CHANGE-VISTA-UE-001")
        after = contract.snapshot_project(self.fixture.source_project)
        copied = self.fixture.attempt_root / "project/gym_citynav"
        self.assertEqual(before, after)
        self.assertEqual(contract.snapshot_project(copied), before)
        self.assertFalse((copied / "DerivedDataCache").exists())
        self.assertEqual((self.fixture.attempt_root / "preparation-plan.json").read_bytes(), prepared.plan_bytes)
        self.assertEqual(stat.S_IMODE((self.fixture.attempt_root / "preparation-receipt.json").stat().st_mode), 0o600)
        self.assertFalse(receipt["unreal_started"])
        self.assertFalse(receipt["content_imported"])
        schema = json.loads(
            (TOOLS_ROOT / "ue/vista_blender_world/schemas/preparation-receipt.schema.json").read_text(encoding="utf-8")
        )
        jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(receipt)
        self.assert_contract_error(
            "VISTA_BLENDER_UE_DESTINATION_EXISTS",
            lambda: contract.materialize_fresh_project(prepared, approval_ref="CHANGE-VISTA-UE-002"),
        )

    def test_receipt_schemas_keep_runtime_and_production_gates_open(self) -> None:
        actor_record = {
            "actor_id": "room_shell_kit:VISTA_Room_FloorSlab_Mesh",
            "label": "VISTA_Generated_VISTA_Room_FloorSlab",
            "actor_path": "/Game/VISTA/Scenes/MMG040_Office_BlenderR1.PersistentLevel.StaticMeshActor_1",
            "class_path": "/Script/Engine.StaticMeshActor",
            "source_path": "/Game/VISTA/External/Procedural/MMG040OfficeR1/VISTA_Room_FloorSlab.VISTA_Room_FloorSlab",
            "tags": [
                "VISTA_FINGERPRINT=vsa-0123456789abcdef01234567",
                "VISTA_OPERATION=vso-0123456789abcdef01234567",
            ],
            "transform": {
                "location_cm": [300.0, 0.0, 0.0],
                "rotation_deg": [0.0, 0.0, 0.0],
                "scale": [1.0, 1.0, 1.0],
            },
        }
        import_receipt = {
            "schema": "simworld.vista.blender-ue-import-receipt/v1",
            "status": "imported_inspected_candidate",
            "error": None,
            "bindings": {
                "engine": "5.3.2-test",
                "project": "/tmp/gym_citynav.uproject",
                "project_sha256": "1" * 64,
                "plan": "/tmp/preparation-plan.json",
                "plan_sha256": "2" * 64,
                "commandlet_script": "/tmp/import.py",
                "commandlet_script_sha256": "3" * 64,
                "import_source": "/tmp/world.glb",
                "import_source_format": "glb",
                "import_source_sha256": "4" * 64,
                "blender_manifest": "/tmp/manifest.json",
                "blender_manifest_sha256": "5" * 64,
            },
            "task": {
                "attempted": True,
                "destination": "/Game/VISTA/External/Procedural/MMG040OfficeR1",
                "imported_object_paths": [actor_record["source_path"]],
                "returned_objects": [actor_record["source_path"]],
            },
            "inventory": {
                "objects": [
                    {
                        "class_path": "/Script/Engine.StaticMesh",
                        "object_path": actor_record["source_path"],
                    }
                ],
                "static_meshes": [
                    {
                        "name": "VISTA_Room_FloorSlab",
                        "source_mesh_name": "VISTA_Room_FloorSlab_Mesh",
                        "object_path": actor_record["source_path"],
                        "bounds_dimensions_cm": [500.0, 500.0, 8.0],
                        "material_slots": [],
                        "collision_generated_by_commandlet": True,
                        "simple_collision_shape_counts": {"convex_elems": 1},
                    }
                ],
            },
            "required_mesh_coverage": [{"asset_id": "room_shell_kit"}],
            "gates": {
                "machine_import_inventory": "passed",
                "live_glb_commandlet_import": "passed",
                "rendered_review": "pending",
                "production_ready": False,
                "semantic_index_eligible": False,
            },
        }
        scene_receipt = {
            "schema": "simworld.vista.blender-ue-scene-receipt/v1",
            "status": "saved_machine_candidate",
            "error": None,
            "bindings": {
                "engine": "5.3.2-test",
                "project": "/tmp/gym_citynav.uproject",
                "project_sha256": "1" * 64,
                "plan": "/tmp/preparation-plan.json",
                "plan_sha256": "2" * 64,
                "commandlet_script": "/tmp/compose.py",
                "commandlet_script_sha256": "3" * 64,
                "import_receipt": "/tmp/import-receipt.json",
                "import_receipt_sha256": "4" * 64,
                "blender_manifest_sha256": "5" * 64,
                "generated_source_sha256": "6" * 64,
            },
            "source_map": {
                "asset_path": "/Game/VISTA/Scenes/MMG040_Office_CommandletR3",
                "file": "/tmp/source.umap",
                "sha256_before": "7" * 64,
                "sha256_after": "7" * 64,
            },
            "output_map": {
                "asset_path": "/Game/VISTA/Scenes/MMG040_Office_BlenderR1",
                "file": "/tmp/output.umap",
                "bytes": 1024,
                "sha256": "8" * 64,
                "save_succeeded": True,
            },
            "removed_surrogates": [],
            "generated_actors": [actor_record],
            "lighting": {},
            "runtime_layout": [actor_record, actor_record, actor_record],
            "player_start_clearance": {
                "status": "passed",
                "pawn_class": "/Game/VISTA/BP_ThirdPersonCharacter.BP_ThirdPersonCharacter_C",
                "location_cm": [150.0, -150.0, 100.0],
                "capsule_radius_cm": 35.0,
                "capsule_half_height_cm": 90.0,
                "blocking_actors": [],
            },
            "physics_prop": {},
            "runtime_project_descriptor": {},
            "actor_inventory": [actor_record],
            "gates": {
                "map_saved": "passed",
                "source_map_immutable": "passed",
                "generated_asset_coverage": "passed",
                "physics_prop_configured": "passed",
                "runtime_proof_tags": "passed",
                "player_start_clearance": "passed",
                "rendered_review": "pending",
                "runtime_input": "pending",
                "production_ready": False,
                "semantic_index_eligible": False,
            },
        }
        for name, value in (("import-receipt.schema.json", import_receipt), ("scene-receipt.schema.json", scene_receipt)):
            with self.subTest(name=name):
                schema = json.loads((TOOLS_ROOT / "ue/vista_blender_world/schemas" / name).read_text(encoding="utf-8"))
                jsonschema.Draft202012Validator(schema).validate(value)

        invalid_import = json.loads(json.dumps(import_receipt))
        invalid_import["inventory"]["static_meshes"] = []
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(
                json.loads(
                    (TOOLS_ROOT / "ue/vista_blender_world/schemas/import-receipt.schema.json").read_text(
                        encoding="utf-8"
                    )
                )
            ).validate(invalid_import)

        invalid_scene = json.loads(json.dumps(scene_receipt))
        invalid_scene["generated_actors"] = []
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(
                json.loads(
                    (TOOLS_ROOT / "ue/vista_blender_world/schemas/scene-receipt.schema.json").read_text(
                        encoding="utf-8"
                    )
                )
            ).validate(invalid_scene)

    def test_commandlets_compile_and_retain_r3_r7_compatibility_contract(self) -> None:
        import_path = TOOLS_ROOT / "ue/vista_blender_world/import_generated_asset_commandlet.py"
        compose_path = TOOLS_ROOT / "ue/vista_blender_world/compose_mmg040_scene_commandlet.py"
        for path in (import_path, compose_path):
            py_compile.compile(str(path), doraise=True)
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("import socket", source)
            self.assertNotIn("execute_python_script", source)
        import_source = import_path.read_text(encoding="utf-8")
        self.assertIn("unreal.AssetImportTask()", import_source)
        self.assertIn('task.set_editor_property("async_", False)', import_source)
        self.assertIn('task.set_editor_property("replace_existing", False)', import_source)
        self.assertIn("unreal.EditorStaticMeshLibrary.add_simple_collisions", import_source)
        compose_source = compose_path.read_text(encoding="utf-8")
        self.assertIn('set_editor_property("generate_overlap_events", False)', compose_source)
        self.assertIn("sky_component.set_intensity(0.8)", compose_source)
        self.assertIn("unreal.EditorLoadingAndSavingUtils.save_map(world, OUTPUT_MAP)", compose_source)
        self.assertIn("component.set_simulate_physics(True)", compose_source)
        self.assertIn('entry["Enabled"] = True', compose_source)
        self.assertIn("VISTA_FINGERPRINT=", compose_source)
        self.assertIn("VISTA_OPERATION=", compose_source)
        self.assertIn("runtime_proof_tags", compose_source)
        self.assertIn("PLAYER_START_TRANSFORM", compose_source)
        self.assertIn("[150.0, -150.0, 100.0]", compose_source)
        self.assertIn("validate_player_start_clearance", compose_source)
        self.assertIn('"VISTA_office_chair_provisional"', compose_source)
        self.assertIn("third-person camera", compose_source)

    def test_default_cli_error_is_structured_and_does_not_start_unreal(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = contract.main([])
        self.assertEqual(code, 2)
        result = json.loads(stderr.getvalue())
        self.assertEqual(result["error"]["code"], "VISTA_BLENDER_UE_ARGUMENT_INVALID")
        self.assertFalse(self.fixture.attempt_root.exists())


if __name__ == "__main__":
    unittest.main()
