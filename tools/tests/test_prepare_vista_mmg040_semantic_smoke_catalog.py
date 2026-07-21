from __future__ import annotations

import hashlib
import json
import pathlib
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


TOOLS_DIR = pathlib.Path(__file__).resolve().parents[1]
REPO_ROOT = TOOLS_DIR.parent
sys.path.insert(0, str(TOOLS_DIR))

import prepare_vista_mmg040_semantic_smoke_catalog as smoke  # noqa: E402


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Fixture:
    def __init__(self, root: pathlib.Path) -> None:
        self.root = root
        self.root.chmod(0o700)
        self.output_parent = root / "outputs"
        self.output_parent.mkdir(mode=0o700)
        self.output = self.output_parent / "mmg040-smoke-r1"
        source = json.loads((TOOLS_DIR / "assets" / "vista_mmg_040_cc0_bootstrap.json").read_text(encoding="utf-8"))
        self.documents: dict[str, dict] = {"source_manifest": source}
        self.paths: dict[str, pathlib.Path] = {
            "source_manifest": root / "source-manifest.json",
            "import_job": root / "import-job.json",
            "interchange_observation": root / "interchange-observation.json",
            "scene_build_observation": root / "scene-build-observation.json",
        }
        self._write("source_manifest")
        self.documents["import_job"] = self._import_job()
        self._write("import_job")
        self.documents["interchange_observation"] = self._interchange_observation()
        self._write("interchange_observation")
        self.documents["scene_build_observation"] = self._scene_build_observation()
        self._write("scene_build_observation")

    def _write(self, name: str) -> None:
        self.paths[name].write_bytes(smoke.canonical_json(self.documents[name]))
        self.paths[name].chmod(0o600)

    def rewrite(self, name: str) -> None:
        self._write(name)

    def digest(self, name: str) -> str:
        return sha256(self.paths[name])

    def kwargs(self) -> dict:
        return {
            "source_manifest_path": self.paths["source_manifest"],
            "source_manifest_sha256": self.digest("source_manifest"),
            "import_job_path": self.paths["import_job"],
            "import_job_sha256": self.digest("import_job"),
            "interchange_observation_path": self.paths["interchange_observation"],
            "interchange_observation_sha256": self.digest("interchange_observation"),
            "scene_build_observation_path": self.paths["scene_build_observation"],
            "scene_build_observation_sha256": self.digest("scene_build_observation"),
        }

    def argv(self, *, output: pathlib.Path | None = None, apply: bool = False, acknowledge: bool = False) -> list[str]:
        args = [
            "--source-manifest", str(self.paths["source_manifest"]),
            "--source-manifest-sha256", self.digest("source_manifest"),
            "--import-job", str(self.paths["import_job"]),
            "--import-job-sha256", self.digest("import_job"),
            "--interchange-observation", str(self.paths["interchange_observation"]),
            "--interchange-observation-sha256", self.digest("interchange_observation"),
            "--scene-build-observation", str(self.paths["scene_build_observation"]),
            "--scene-build-observation-sha256", self.digest("scene_build_observation"),
        ]
        if output is not None:
            args += ["--output-dir", str(output)]
        if apply:
            args.append("--apply")
        if acknowledge:
            args += ["--acknowledge-nonproduction-smoke", smoke.NONPRODUCTION_ACK]
        return args

    def _import_job(self) -> dict:
        assets = []
        for asset_id in sorted(smoke.ASSETS):
            definition = smoke.ASSETS[asset_id]
            source_id = definition["source_asset_id"]
            gltf_path = definition["source_gltf"]
            gltf_size, gltf_digest = smoke.SOURCE_FILES[gltf_path]
            dependencies = []
            for path in sorted(path for path in smoke.SOURCE_FILES if path.startswith(source_id + "/") and path != gltf_path):
                size, digest = smoke.SOURCE_FILES[path]
                dependencies.append({"path": path, "bytes": size, "sha256": digest, "usages": ["buffer"] if path.endswith(".bin") else ["image"]})
            dimensions = definition["source_dimensions_m"]
            assets.append(
                {
                    "asset_id": asset_id,
                    "source_asset_id": source_id,
                    "semantic_roles": definition["semantic_roles"],
                    "source_gltf": {"path": gltf_path, "bytes": gltf_size, "sha256": gltf_digest},
                    "external_dependencies": dependencies,
                    "source_bounds": {
                        "basis": "gltf_POSITION_accessor_local_extrema_aggregate",
                        "coordinate_unit": "meter",
                        "includes_node_transforms": False,
                        "position_accessors": [{"accessor_index": 0, "min": [0, 0, 0], "max": dimensions}],
                        "aggregate_min": [0, 0, 0],
                        "aggregate_max": dimensions,
                        "dimensions": dimensions,
                    },
                    "destination_content_path": definition["destination"],
                    "expected_object_paths": [],
                    "post_import_verification": {
                        "returned_objects": {
                            "require_at_least_one_class": "StaticMesh",
                            "record_exact_object_paths_after_import": True,
                            "reject_object_redirectors": True,
                        },
                        "materials_and_textures": {
                            "record_material_slots": True,
                            "account_for_every_source_image": True,
                            "reject_missing_or_default_only_materials": True,
                        },
                        "bounds": {
                            "source_units_to_unreal_centimeters": 100,
                            "relative_tolerance": 0.02,
                            "absolute_tolerance_cm": 1.0,
                            "operator_must_reconcile_node_transforms_and_mesh_splitting": True,
                        },
                        "collision": {
                            "require_nonempty_simple_collision_or_reviewed_complex_as_simple": True,
                            "record_collision_complexity_and_primitive_count": True,
                        },
                        "pbr_channel_review": {
                            "base_color_srgb": True,
                            "normal_source_convention": "OpenGL",
                            "normal_green_channel_conversion_must_be_verified": True,
                            "arm_texture_srgb": False,
                            "arm_channels": {"r": "ambient_occlusion", "g": "roughness", "b": "metallic"},
                        },
                    },
                }
            )
        return {
            "schema": smoke.IMPORT_JOB_SCHEMA,
            "source_binding": {
                "source_schema": smoke.SOURCE_SCHEMA,
                "revision": smoke.SOURCE_REVISION,
                "provider": "Poly Haven",
                "license": "CC0-1.0",
                "manifest_sha256": self.digest("source_manifest"),
                "tree_sha256": smoke.SOURCE_TREE_SHA256,
                "file_count": 15,
                "total_bytes": 4_648_718,
            },
            "engine_contract": {
                "engine_version": "5.3.2",
                "python_api_status": "experimental",
                "import_route": "Interchange",
                "official_api_references": [
                    "https://dev.epicgames.com/documentation/en-us/unreal-engine/python-api/class/AssetImportTask.html?application_version=5.3",
                    "https://dev.epicgames.com/documentation/en-us/unreal-engine/python-api/class/InterchangeManager.html?application_version=5.3",
                ],
                "asset_import_task": {"automated": True, "async": False, "replace_existing": False, "replace_existing_settings": False, "save": True},
                "destination_root": "/Game/VISTA/External/PolyHaven",
                "follow_redirectors": False,
            },
            "assets": assets,
            "execution_gate": {
                "prepared_only": True,
                "unreal_started": False,
                "content_imported": False,
                "requires_operator_approval": True,
                "requires_disposable_project_copy": True,
                "requires_ue_5_3_2_api_probe": True,
                "do_not_add_to_semantic_index_before_post_import_verification": True,
            },
        }

    def _interchange_observation(self) -> dict:
        assets = []
        for asset_id in sorted(smoke.ASSETS):
            definition = smoke.ASSETS[asset_id]
            packed_key = "packed_metallic_roughness_srgb" if asset_id == "polyhaven_shelf_01" else "packed_roughness_srgb"
            assets.append(
                {
                    "asset_id": asset_id,
                    "static_mesh": definition["ue_path"],
                    "material": definition["material_path"],
                    "bounds_dimensions_cm": definition["dimensions_cm"],
                    "simple_convex_count": 1,
                    "base_color_srgb": True,
                    packed_key: False,
                    "normal_srgb": False,
                    "normal_compression": "TC_NORMALMAP",
                    "normal_flip_green_channel": True,
                }
            )
        return {
            "schema": smoke.INTERCHANGE_SCHEMA,
            "receipt_kind": "operator_summarized_live_observation",
            "observed_at_utc": "2026-07-21T19:11:18.043Z",
            "status": "imported_inspected_machine_probe",
            "route": "UnrealEditor-Cmd -run=pythonscript",
            "transport": "local_process_no_studio_socket",
            "process_exit_code": 0,
            "mutation_state": "complete",
            "quarantine_required": False,
            "retry_performed": False,
            "bindings": {
                "engine_version": "5.3.2-fixture",
                "project_path": "/fixture/disposable/gym_citynav.uproject",
                "project_sha256": "1" * 64,
                "source_revision": smoke.SOURCE_REVISION,
                "source_manifest_sha256": self.digest("source_manifest"),
                "source_tree_sha256": smoke.SOURCE_TREE_SHA256,
                "import_job_sha256": self.digest("import_job"),
                "preparation_receipt_sha256": "2" * 64,
                "commandlet_script_sha256": "3" * 64,
                "unreal_log_sha256": "4" * 64,
            },
            "content_observation": {
                "asset_count": 3,
                "uasset_count": 15,
                "uasset_tree_digest": "5" * 64,
                "uasset_tree_digest_algorithm": "sha256(sorted absolute-path sha256sum records)",
                "all_tasks_synchronous": True,
                "all_destinations_were_absent_before_import": True,
                "all_returned_objects_present_in_post_import_inventory": True,
                "assets": assets,
            },
            "gates": {
                "machine_import_inventory": "passed",
                "material_texture_dependency": "review_pending",
                "interchange_pipeline_fingerprint": "review_pending",
                "rendered_scale_contact_collision_pbr": "review_pending",
                "production_ready": False,
                "semantic_index_eligible": False,
            },
            "limitations": ["material dependency pending", "machine observation only", "no render or publication"],
        }

    def _scene_build_observation(self) -> dict:
        attempts = [
            {"status": "failed_unsaved_quarantined"},
            {"status": "failed_unsaved_quarantined"},
            {"status": "saved_machine_probe"},
        ]
        return {
            "actor_count": 14,
            "attempts": attempts,
            "captured_at": "2026-07-21T19:46:48Z",
            "engine": "5.3.2-fixture",
            "execution": {
                "gpu_assignment": "CUDA_VISIBLE_DEVICES=0",
                "null_rhi": True,
                "pixel_streaming_enabled": False,
                "project_plugin_unreal_mcp_enabled": False,
                "timeout_seconds": 300,
            },
            "output_map": {
                "asset_path": smoke.SCENE_ASSET_PATH,
                "bytes": 23_886,
                "file": "/fixture/Content/VISTA/Scenes/MMG040_Office_CommandletR3.umap",
                "sha256": smoke.SCENE_MAP_SHA256,
            },
            "production_ready": False,
            "referenced_scene_assets": [
                "/Game/CityDatabase/meshes/SM_chair_b.SM_chair_b",
                *(definition["ue_path"] for definition in smoke.ASSETS.values()),
                "/Game/Human_Avatar/DefaultCharacter/ThirdPerson/Blueprints/BP_ThirdPersonCharacter.BP_ThirdPersonCharacter_C",
            ],
            "review_gates": {
                "animation_and_ik": "review_pending",
                "collision_and_contact": "review_pending",
                "pbr_render": "review_pending",
                "rendered_scale": "review_pending",
                "screenshot": "review_pending",
            },
            "schema": smoke.SCENE_BUILD_SCHEMA,
            "semantic_index_eligible": False,
            "status": "saved_machine_probe",
            "source_revision": smoke.SCENE_SOURCE_REVISION,
        }


class SmokeCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.fixture = Fixture(pathlib.Path(self.temporary.name))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def assert_error(self, code: str, callback) -> smoke.SmokeCatalogError:
        with self.assertRaises(smoke.SmokeCatalogError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def portable_pins(self, fixture: Fixture | None = None):
        fixture = fixture or self.fixture
        return mock.patch.multiple(
            smoke,
            SOURCE_MANIFEST_SHA256=fixture.digest("source_manifest"),
            IMPORT_JOB_SHA256=fixture.digest("import_job"),
            INTERCHANGE_OBSERVATION_SHA256=fixture.digest("interchange_observation"),
            SCENE_BUILD_OBSERVATION_SHA256=fixture.digest("scene_build_observation"),
        )

    def build_portable(self, fixture: Fixture | None = None) -> smoke.PreparedCatalog:
        fixture = fixture or self.fixture
        with self.portable_pins(fixture):
            return smoke.build_catalog(**fixture.kwargs())

    def test_builds_exact_deterministic_nonproduction_catalog(self) -> None:
        first = self.build_portable()
        second = self.build_portable()
        self.assertEqual(first.bundle, second.bundle)
        self.assertEqual(first.catalog_sha256, second.catalog_sha256)
        self.assertEqual(len(first.records), 3)
        self.assertEqual(first.bundle["asset_count"], 3)
        self.assertFalse(first.bundle["production_ready"])
        self.assertFalse(first.bundle["semantic_index_eligible"])
        self.assertEqual(set(first.bundle["review_gates"].values()), {"review_pending"})
        self.assertFalse(first.bundle["generic_consumers_permitted"])
        self.assertRegex(first.seal_sha256, r"^[a-f0-9]{64}$")
        for relative, record in first.records:
            asset_id = record["identity"]["asset_id"]
            source_asset_id = record["provenance"]["source_asset_id"]
            self.assertEqual(relative, f"{record['identity']['category']}/{asset_id}.json")
            self.assertTrue(asset_id.startswith(smoke.SMOKE_NAMESPACE + "__"))
            self.assertEqual(record["technical"]["unreal_asset_path"], smoke.ASSETS[source_asset_id]["ue_path"])
            self.assertEqual(record["indexing"]["view_count"], 0)
            self.assertEqual(record["indexing"]["view_policy"]["nonproduction_smoke_min_views"], 0)
            self.assertEqual(record["indexing"]["view_policy"]["production_min_views"], 4)
            self.assertFalse(record["indexing"]["view_policy"]["production_view_requirement_compatible"])
            self.assertFalse(record["readiness"]["production_ready"])
            self.assertFalse(record["readiness"]["semantic_index_eligible"])
            self.assertFalse(record["provenance"]["canonical_official_asset"])
            self.assertEqual(set(record["readiness"]["gates"].values()), {"review_pending"})
        cardboard = next(record for _relative, record in first.records if record["provenance"]["source_asset_id"] == "polyhaven_cardboard_box_01")
        self.assertEqual(cardboard["semantic"]["tags"], ["box", "cardboard", "container", "indoor", "packaging", "storage"])

    def test_default_preparation_writes_nothing(self) -> None:
        before = sorted(path.relative_to(self.fixture.root) for path in self.fixture.root.rglob("*"))
        prepared = self.build_portable()
        after = sorted(path.relative_to(self.fixture.root) for path in self.fixture.root.rglob("*"))
        self.assertEqual(before, after)
        self.assertFalse(self.fixture.output.exists())
        self.assertEqual(prepared.bundle["scope"], smoke.SCOPE)

    def test_apply_is_private_atomic_non_overwriting_and_generic_incompatible(self) -> None:
        prepared = self.build_portable()
        receipt = smoke.publish_catalog(prepared, self.fixture.output)
        self.assertEqual(stat.S_IMODE(self.fixture.output.stat().st_mode), 0o700)
        for path in self.fixture.output.rglob("*"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600)
        self.assertFalse(receipt["production_ready"])
        self.assertFalse(receipt["semantic_index_eligible"])
        self.assertFalse(receipt["generic_consumers_permitted"])
        self.assertFalse(receipt["database_migration_permitted"])
        self.assertFalse(receipt["category_index_rebuild_permitted"])
        self.assertEqual(receipt, json.loads((self.fixture.output / "smoke-catalog-receipt.json").read_text()))
        self.assert_error("SMOKE_OUTPUT_EXISTS", lambda: smoke.publish_catalog(prepared, self.fixture.output))
        self.assertFalse((self.fixture.output / "catalog").exists())
        self.assertFalse((self.fixture.output / "category_index.json").exists())
        self.assertTrue((self.fixture.output / "nonproduction_catalog").is_dir())
        self.assertTrue((self.fixture.output / "nonproduction_category_index.json").is_file())
        self.assertEqual(list(self.fixture.output.glob("catalog/*/*.json")), [])
        self.assertEqual(len(list(self.fixture.output.glob("nonproduction_catalog/*/*.json"))), 3)

    def test_independent_digest_pins_and_duplicate_keys_fail_closed(self) -> None:
        kwargs = self.fixture.kwargs()
        kwargs["import_job_sha256"] = "0" * 64
        self.assert_error("SMOKE_INPUT_DIGEST_MISMATCH", lambda: smoke.build_catalog(**kwargs))

        path = self.fixture.paths["source_manifest"]
        raw = path.read_bytes().replace(b'{\n  "assets"', b'{\n  "schema": "duplicate",\n  "assets"', 1)
        path.write_bytes(raw)
        path.chmod(0o600)
        kwargs = self.fixture.kwargs()
        self.assert_error("SMOKE_INPUT_DUPLICATE_KEY", lambda: smoke.build_catalog(**kwargs))

    def test_unpatched_synthetic_evidence_is_rejected_by_build_and_cli(self) -> None:
        self.assert_error("SMOKE_EVIDENCE_NOT_PINNED", lambda: smoke.build_catalog(**self.fixture.kwargs()))
        command = [sys.executable, str(TOOLS_DIR / "prepare_vista_mmg040_semantic_smoke_catalog.py"), *self.fixture.argv()]
        completed = subprocess.run(command, cwd=REPO_ROOT, check=False, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(json.loads(completed.stderr)["error"]["code"], "SMOKE_EVIDENCE_NOT_PINNED")

    def test_real_evidence_regression_when_operator_files_are_available(self) -> None:
        release = pathlib.Path("/home/yhliu/SimWorldStudio-live/0.2.0-806e869a/releases/ec5ed8dd4beb-mmg040-live-r1/evidence")
        inputs = {
            "source_manifest_path": pathlib.Path("/home/yhliu/.simworld/vendor-assets/vista-mmg-040-polyhaven-v2/source-manifest.json"),
            "source_manifest_sha256": smoke.SOURCE_MANIFEST_SHA256,
            "import_job_path": release / "ue-import-jobs/polyhaven-v2-r1/import-job.json",
            "import_job_sha256": smoke.IMPORT_JOB_SHA256,
            "interchange_observation_path": release / "ue-interchange-execution/commandlet-v2-r1/observation-receipt.json",
            "interchange_observation_sha256": smoke.INTERCHANGE_OBSERVATION_SHA256,
            "scene_build_observation_path": release / "ue-scene-build/commandlet-r3-r1/observation-receipt.json",
            "scene_build_observation_sha256": smoke.SCENE_BUILD_OBSERVATION_SHA256,
        }
        if not all(path.is_file() for key, path in inputs.items() if key.endswith("_path")):
            self.skipTest("operator evidence files are not available on this host")
        prepared = smoke.build_catalog(**inputs)
        self.assertEqual(len(prepared.records), 3)
        smoke.validate_prepared_catalog(prepared)

    def test_symlink_and_unsafe_mode_are_rejected(self) -> None:
        self.fixture.paths["import_job"].chmod(0o644)
        self.assert_error("SMOKE_INPUT_METADATA_UNSAFE", lambda: smoke.build_catalog(**self.fixture.kwargs()))

        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(pathlib.Path(temporary))
            target = fixture.paths["import_job"]
            linked = fixture.root / "linked-import.json"
            linked.symlink_to(target)
            kwargs = fixture.kwargs()
            kwargs["import_job_path"] = linked
            self.assert_error("SMOKE_INPUT_SYMLINK_REJECTED", lambda: smoke.build_catalog(**kwargs))

    def test_source_asset_and_file_closure_are_exact(self) -> None:
        self.fixture.documents["source_manifest"]["provider"] = "Official SimWorld"
        self.fixture.rewrite("source_manifest")
        self.assert_error("SMOKE_EVIDENCE_NOT_PINNED", lambda: smoke.build_catalog(**self.fixture.kwargs()))

        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(pathlib.Path(temporary))
            fixture.documents["import_job"]["assets"][0]["source_gltf"]["sha256"] = "0" * 64
            fixture.rewrite("import_job")
            fixture.documents["interchange_observation"]["bindings"]["import_job_sha256"] = fixture.digest("import_job")
            fixture.rewrite("interchange_observation")
            with self.portable_pins(fixture):
                self.assert_error("SMOKE_SOURCE_FILE_DIGEST_INVALID", lambda: smoke.build_catalog(**fixture.kwargs()))

    def test_cross_document_lineage_mismatch_is_rejected(self) -> None:
        self.fixture.documents["interchange_observation"]["bindings"]["import_job_sha256"] = "0" * 64
        self.fixture.rewrite("interchange_observation")
        with self.portable_pins():
            self.assert_error("SMOKE_EVIDENCE_MISMATCH", lambda: smoke.build_catalog(**self.fixture.kwargs()))

    def test_ue_paths_and_readiness_cannot_be_escalated(self) -> None:
        observed = self.fixture.documents["interchange_observation"]
        observed["content_observation"]["assets"][0]["static_mesh"] = "/Game/Fake/Fake.Fake"
        self.fixture.rewrite("interchange_observation")
        with self.portable_pins():
            self.assert_error("SMOKE_EVIDENCE_MISMATCH", lambda: smoke.build_catalog(**self.fixture.kwargs()))

        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(pathlib.Path(temporary))
            fixture.documents["interchange_observation"]["gates"]["semantic_index_eligible"] = True
            fixture.rewrite("interchange_observation")
            with self.portable_pins(fixture):
                self.assert_error("SMOKE_READINESS_ESCALATION_REJECTED", lambda: smoke.build_catalog(**fixture.kwargs()))

    def test_scene_asset_set_and_review_gates_are_exact(self) -> None:
        scene = self.fixture.documents["scene_build_observation"]
        scene["referenced_scene_assets"][1] = "/Game/Fake/Fake.Fake"
        self.fixture.rewrite("scene_build_observation")
        with self.portable_pins():
            self.assert_error("SMOKE_EVIDENCE_MISMATCH", lambda: smoke.build_catalog(**self.fixture.kwargs()))

        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(pathlib.Path(temporary))
            fixture.documents["scene_build_observation"]["review_gates"]["pbr_render"] = "passed"
            fixture.rewrite("scene_build_observation")
            with self.portable_pins(fixture):
                self.assert_error("SMOKE_READINESS_ESCALATION_REJECTED", lambda: smoke.build_catalog(**fixture.kwargs()))

    def test_apply_requires_explicit_scope_ack_and_private_parent(self) -> None:
        command = [sys.executable, str(TOOLS_DIR / "prepare_vista_mmg040_semantic_smoke_catalog.py"), *self.fixture.argv(output=self.fixture.output, apply=True)]
        completed = subprocess.run(command, cwd=REPO_ROOT, check=False, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 2)
        self.assertFalse(self.fixture.output.exists())

        self.fixture.output_parent.chmod(0o755)
        prepared = self.build_portable()
        self.assert_error("SMOKE_OUTPUT_INVALID", lambda: smoke.publish_catalog(prepared, self.fixture.output))

    def test_every_prepared_mutable_surface_is_sealed_before_publish(self) -> None:
        mutators = {
            "bundle": lambda prepared: prepared.bundle.__setitem__("scope", "changed"),
            "record": lambda prepared: prepared.records[0][1]["semantic"]["tags"].append("changed"),
            "category_index": lambda prepared: prepared.category_index.__setitem__("total_assets", 4),
            "bindings": lambda prepared: prepared.bindings["import_job"].__setitem__("sha256", "0" * 64),
            "gates": lambda prepared: prepared.gates.__setitem__("rendered_pbr", "passed"),
        }
        for name, mutate in mutators.items():
            with self.subTest(surface=name):
                prepared = self.build_portable()
                mutate(prepared)
                self.assert_error("SMOKE_PREPARED_MUTATED", lambda: smoke.publish_catalog(prepared, self.fixture.output))
                self.assertFalse(self.fixture.output.exists())

    def test_gate_surfaces_do_not_share_mutable_state(self) -> None:
        prepared = self.build_portable()
        record_gates = [record["readiness"]["gates"] for _relative, record in prepared.records]
        self.assertEqual(len({id(gates) for gates in record_gates}), 3)
        self.assertTrue(all(gates is not prepared.gates for gates in record_gates))
        self.assertIsNot(prepared.bundle["review_gates"], prepared.gates)

    def test_publication_failure_leaves_no_partial_output_or_owned_lock(self) -> None:
        prepared = self.build_portable()
        original = smoke._write_private
        calls = 0

        def fail_after_first(path, payload):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("synthetic write failure")
            return original(path, payload)

        with mock.patch.object(smoke, "_write_private", side_effect=fail_after_first):
            with self.assertRaises(OSError):
                smoke.publish_catalog(prepared, self.fixture.output)
        self.assertFalse(self.fixture.output.exists())
        self.assertFalse((self.fixture.output_parent / ".mmg040-smoke-r1.lock").exists())
        self.assertEqual(list(self.fixture.output_parent.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
