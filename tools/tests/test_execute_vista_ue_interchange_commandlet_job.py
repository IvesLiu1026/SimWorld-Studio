from __future__ import annotations

import ast
import contextlib
import hashlib
import io
import json
import pathlib
import subprocess
import stat
import sys
import tempfile
import unittest
from unittest import mock


TOOLS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

import execute_vista_ue_interchange_commandlet_job as executor  # noqa: E402
import fetch_vista_bootstrap_assets as acquisition  # noqa: E402
import prepare_vista_ue_interchange_import_job as preparation  # noqa: E402


def digest(raw: bytes, algorithm: str = "sha256") -> str:
    return hashlib.new(algorithm, raw).hexdigest()


class Fixture:
    def __init__(self, root: pathlib.Path) -> None:
        self.root = root
        self.root.chmod(0o700)
        self.manifest_path = root / "source-manifest.json"
        self.acquisition_parent = root / "acquisitions"
        self.acquisition_parent.mkdir(mode=0o700)
        self.acquisition_dir = self.acquisition_parent / "pinned-v2"
        self.job_parent = root / "jobs"
        self.job_parent.mkdir(mode=0o700)
        self.job_dir = self.job_parent / "job-r1"
        self.evidence_parent = root / "evidence"
        self.evidence_parent.mkdir(mode=0o700)
        self.evidence_dir = self.evidence_parent / "execution-r1"
        self.engine = root / "synthetic-runtime" / "Engine" / "Binaries" / "Linux" / "UnrealEditor-Cmd"
        self._make_engine()
        self.engine_sha256 = digest(self.engine.read_bytes())
        self.project_root = root / "disposable-project"
        self._make_project()
        self.project = self.project_root / "gym_citynav.uproject"
        self.project_sha256 = digest(self.project.read_bytes())
        self.payloads: dict[str, bytes] = {}
        self.manifest = self._make_manifest()
        self.manifest_raw = preparation.canonical_json(self.manifest)
        self.manifest_path.write_bytes(self.manifest_raw)
        self.manifest_path.chmod(0o600)
        self.manifest_sha256 = digest(self.manifest_raw)
        self._acquire_and_prepare()

    def _make_project(self) -> None:
        self.project_root.mkdir(mode=0o700)
        for relative in ("Content", "Config", "Plugins"):
            path = self.project_root / relative
            path.mkdir(mode=0o700)
        files = {
            "gym_citynav.uproject": (
                b'{"FileVersion":3,"Plugins":['
                b'{"Name":"EditorScriptingUtilities","Enabled":true},'
                b'{"Name":"PythonScriptPlugin","Enabled":true}]}\n'
            ),
            "Config/DefaultEngine.ini": b"[/Script/Engine.Engine]\n",
        }
        for relative, raw in files.items():
            path = self.project_root / relative
            path.write_bytes(raw)
            path.chmod(0o600)

    def _make_engine(self) -> None:
        engine_root = self.engine.parents[2]
        directories = (
            "Binaries/Linux",
            "Config",
            "Plugins/Interchange/Runtime/Content/Pipelines",
        )
        for relative in directories:
            path = engine_root / relative
            path.mkdir(parents=True, exist_ok=True)
        for path in (engine_root, *engine_root.rglob("*")):
            if path.is_dir():
                path.chmod(0o700)
        files = {
            "Binaries/Linux/UnrealEditor-Cmd": b"synthetic-engine-binary\n",
            "Binaries/Linux/libUnrealEditor-InterchangeCore.so": b"synthetic-interchange-core\n",
            "Binaries/Linux/libUnrealEditor-InterchangeEngine.so": b"synthetic-interchange-engine\n",
            "Config/BaseEngine.ini": (
                b"/Interchange/Pipelines/DefaultGLTFAssetsPipeline.DefaultGLTFAssetsPipeline\n"
                b"/Interchange/Pipelines/DefaultGLTFPipeline.DefaultGLTFPipeline\n"
                b"/Script/InterchangeImport.InterchangeGltfTranslator\n"
            ),
            "Plugins/Interchange/Runtime/Interchange.uplugin": b'{"FileVersion":3}\n',
            "Plugins/Interchange/Runtime/Content/Pipelines/DefaultGLTFAssetsPipeline.uasset": b"pipeline-assets\n",
            "Plugins/Interchange/Runtime/Content/Pipelines/DefaultGLTFPipeline.uasset": b"pipeline-gltf\n",
        }
        for relative, raw in files.items():
            path = engine_root / relative
            path.write_bytes(raw)
            path.chmod(0o600)
        self.engine.chmod(0o700)

    def _make_manifest(self) -> dict:
        assets = []
        definitions = (
            ("polyhaven_painted_wooden_stool", "painted_wooden_stool", "stable_step_stool"),
            ("polyhaven_shelf_01", "Shelf_01", "high_storage_shelf"),
            ("polyhaven_cardboard_box_01", "cardboard_box_01", "cardboard_box"),
        )
        for index, (asset_id, source_id, role) in enumerate(definitions):
            binary = bytes([index + 1]) * 36
            images = {
                f"{source_id}_diff_1k.jpg": f"diff-{source_id}".encode(),
                f"{source_id}_nor_gl_1k.jpg": f"normal-{source_id}".encode(),
                f"{source_id}_arm_1k.jpg": f"arm-{source_id}".encode(),
            }
            gltf = {
                "asset": {"version": "2.0", "generator": "synthetic-test"},
                "buffers": [{"uri": f"{source_id}.bin", "byteLength": len(binary)}],
                "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(binary)}],
                "accessors": [
                    {
                        "bufferView": 0,
                        "componentType": 5126,
                        "count": 3,
                        "type": "VEC3",
                        "min": [-1.0, 0.0, -0.5],
                        "max": [1.0, 2.0, 0.5],
                    }
                ],
                "images": [{"uri": f"textures/{name}"} for name in images],
                "textures": [{"source": image_index} for image_index in range(3)],
                "materials": [{"pbrMetallicRoughness": {"baseColorTexture": {"index": 0}}}],
                "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "material": 0}]}],
                "nodes": [{"mesh": 0}],
                "scenes": [{"nodes": [0]}],
                "scene": 0,
            }
            files: list[dict] = []
            raw_by_relative = {
                f"{source_id}_1k.gltf": preparation.canonical_json(gltf),
                f"{source_id}.bin": binary,
                **{f"textures/{name}": raw for name, raw in images.items()},
            }
            for relative, raw in raw_by_relative.items():
                url = acquisition.DOWNLOAD_PREFIX + source_id + "/" + relative
                self.payloads[url] = raw
                files.append(
                    {
                        "path": source_id + "/" + relative,
                        "url": url,
                        "bytes": len(raw),
                        "md5": digest(raw, "md5"),
                        "sha256": digest(raw),
                    }
                )
            assets.append(
                {
                    "asset_id": asset_id,
                    "source_asset_id": source_id,
                    "source_page": acquisition.SOURCE_PREFIX + source_id,
                    "semantic_roles": [role],
                    "format": "gltf-2.0",
                    "files": files,
                }
            )
        return {
            "schema": acquisition.SCHEMA,
            "revision": executor.PINNED_REVISION,
            "provider": "Poly Haven",
            "provider_url": "https://polyhaven.com/",
            "license": acquisition.EXPECTED_LICENSE,
            "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
            "attribution": "Synthetic test fixture",
            "assets": assets,
        }

    def opener(self, url: str):
        @contextlib.contextmanager
        def opened():
            yield io.BytesIO(self.payloads[url])

        return opened()

    def _acquire_and_prepare(self) -> None:
        plan = acquisition.build_plan(self.manifest_path, self.acquisition_dir)
        self.assert_equal(acquisition.apply_plan(plan, stream_opener=self.opener), "created")
        prepared = preparation.build_job(self.manifest_path, self.acquisition_dir)
        preparation.publish_job(prepared, self.job_dir, "CHANGE-COMMANDLET-TEST-001")

    @staticmethod
    def assert_equal(observed, expected) -> None:
        if observed != expected:
            raise AssertionError(f"expected {expected!r}, got {observed!r}")

    def control_sha256(self) -> str:
        return executor.project_control_fingerprint(executor.project_control_manifest(self.project))

    def interchange_sha256(self) -> str:
        return executor.interchange_runtime_fingerprint(executor.interchange_runtime_manifest(self.engine))

    def plan(
        self,
        *,
        pin_control_tree: bool = True,
        pin_interchange_tree: bool = True,
        evidence_name: str = "execution-r1",
        apply: bool = True,
    ) -> executor.ExecutionPlan:
        with mock.patch.object(executor, "PINNED_MANIFEST_SHA256", self.manifest_sha256):
            return executor.build_execution_plan(
                manifest_path=self.manifest_path,
                acquisition_dir=self.acquisition_dir,
                job_dir=self.job_dir,
                engine_executable=self.engine,
                engine_sha256=self.engine_sha256,
                interchange_tree_sha256=self.interchange_sha256() if pin_interchange_tree else None,
                project_file=self.project,
                project_sha256=self.project_sha256,
                project_control_sha256=self.control_sha256() if pin_control_tree else None,
                evidence_dir=self.evidence_parent / evidence_name,
                timeout_seconds=60,
                approval_ref="CHANGE-COMMANDLET-TEST-002" if apply else None,
                apply=apply,
            )

    def replan_after_claim(self, previous: executor.ExecutionPlan, evidence_name: str) -> executor.ExecutionPlan:
        with mock.patch.object(executor, "PINNED_MANIFEST_SHA256", self.manifest_sha256):
            return executor.build_execution_plan(
                manifest_path=self.manifest_path,
                acquisition_dir=self.acquisition_dir,
                job_dir=self.job_dir,
                engine_executable=self.engine,
                engine_sha256=self.engine_sha256,
                interchange_tree_sha256=previous.interchange_runtime_sha256,
                project_file=self.project,
                project_sha256=self.project_sha256,
                project_control_sha256=previous.project_control_sha256,
                evidence_dir=self.evidence_parent / evidence_name,
                timeout_seconds=60,
                approval_ref="CHANGE-COMMANDLET-TEST-003",
                apply=True,
            )

    def write_imported_assets(self, plan: executor.ExecutionPlan, count: int = 3) -> None:
        for asset in plan.job_bundle.prepared.job["assets"][:count]:
            safe_id = asset["destination_content_path"].rsplit("/", 1)[-1]
            directory = self.project_root / "Content" / "VISTA" / "External" / "PolyHaven" / safe_id
            directory.mkdir(parents=True, mode=0o700)
            for parent in directory.parents:
                if parent == self.project_root:
                    break
                parent.chmod(0o700)
            value = directory / f"SM_{safe_id}.uasset"
            value.write_bytes(b"synthetic-uasset")
            value.chmod(0o600)

    @staticmethod
    def write_stdout(path: pathlib.Path, value: dict) -> None:
        with path.open("ab") as handle:
            handle.write(b"LogPython: " + executor.MARKER_PREFIX + executor.compact_json(value).encode() + b"\n")

    def success_marker(self, plan: executor.ExecutionPlan) -> dict:
        assets = []
        for asset in plan.job_bundle.prepared.job["assets"]:
            safe_id = asset["destination_content_path"].rsplit("/", 1)[-1]
            package = asset["destination_content_path"] + f"/SM_{safe_id}"
            assets.append(
                {
                    "asset_id": asset["asset_id"],
                    "destination_content_path": asset["destination_content_path"],
                    "object_records": [{"class": "StaticMesh", "object_path": package + f".SM_{safe_id}"}],
                    "package_paths": [package],
                    "source_gltf_sha256": asset["source_gltf"]["sha256"],
                }
            )
        return {
            "assets": assets,
            "job_sha256": plan.job_bundle.prepared.job_sha256,
            "post_import_review": {
                "collision": "review_pending",
                "pipeline_fingerprint": "review_pending",
                "pbr_channels": "review_pending",
                "rendered_visual": "review_pending",
                "scale_and_bounds": "review_pending",
            },
            "production_ready": False,
            "project_sha256": plan.project.sha256,
            "schema": executor.MARKER_SCHEMA,
            "semantic_index_eligible": False,
            "source_file_count": executor.PINNED_FILE_COUNT,
            "source_tree_sha256": plan.job_bundle.prepared.tree_sha256,
            "status": "imported_pending_review",
        }

    @staticmethod
    def receipt(plan: executor.ExecutionPlan) -> dict:
        return json.loads((plan.evidence_dir / "terminal-receipt.json").read_text(encoding="utf-8"))


class CommandletExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.fixture = Fixture(pathlib.Path(self.temporary.name))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def assert_error(self, code: str, callback) -> executor.CommandletExecutionError:
        with self.assertRaises(executor.CommandletExecutionError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_repository_v2_manifest_matches_non_overridable_pin(self) -> None:
        manifest = TOOLS_DIR / "assets" / "vista_mmg_040_cc0_bootstrap.json"
        self.assertEqual(digest(manifest.read_bytes()), executor.PINNED_MANIFEST_SHA256)

    def test_dry_run_writes_nothing_and_labels_unpinned_tree_as_probe(self) -> None:
        plan = self.fixture.plan(
            pin_control_tree=False,
            pin_interchange_tree=False,
            apply=False,
        )
        before = sorted(path.relative_to(self.fixture.root) for path in self.fixture.root.rglob("*"))
        result = executor.dry_run_result(plan)
        after = sorted(path.relative_to(self.fixture.root) for path in self.fixture.root.rglob("*"))
        self.assertEqual(before, after)
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["execution_class"], "api_probe_review_pending")
        self.assertFalse(result["project_control_pin_supplied"])
        self.assertFalse(result["interchange_runtime_pin_supplied"])
        self.assertFalse(result["unreal_started"])
        self.assertFalse(result["production_ready"])
        self.assertFalse(result["semantic_index_eligible"])
        self.assertFalse(plan.evidence_dir.exists())

    def test_apply_requires_independent_project_and_interchange_tree_pins(self) -> None:
        self.assert_error(
            "UE_COMMANDLET_PROJECT_TREE_REQUIRED",
            lambda: self.fixture.plan(pin_control_tree=False),
        )
        self.assert_error(
            "UE_COMMANDLET_INTERCHANGE_TREE_REQUIRED",
            lambda: self.fixture.plan(pin_interchange_tree=False),
        )

    def test_complete_project_influence_surface_rejects_startup_bypasses(self) -> None:
        cases = (
            ("Content/Python/init_unreal.py", b"raise RuntimeError('ambient startup')\n", "UE_COMMANDLET_PROJECT_SHAPE_INVALID"),
            (
                "Saved/Config/LinuxEditor/EditorPerProjectUserSettings.ini",
                b"[Startup]\n",
                "UE_COMMANDLET_PROJECT_SHAPE_INVALID",
            ),
            ("Plugins/Evil/Evil.uplugin", b'{"FileVersion":3}\n', "UE_COMMANDLET_PROJECT_SHAPE_INVALID"),
            (
                "Config/DefaultEngine.ini",
                b"[/Script/PythonScriptPlugin.PythonScriptPluginSettings]\nStartupScripts=evil.py\n",
                "UE_COMMANDLET_PROJECT_STARTUP_HOOK_REJECTED",
            ),
        )
        for relative, raw, expected_code in cases:
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as temporary:
                fixture = Fixture(pathlib.Path(temporary))
                target = fixture.project_root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                for parent in target.parents:
                    if parent == fixture.project_root:
                        break
                    parent.chmod(0o700)
                target.write_bytes(raw)
                target.chmod(0o600)
                self.assert_error(expected_code, fixture.plan)

    def test_engine_project_and_control_tree_pins_fail_closed(self) -> None:
        self.assert_error(
            "UE_COMMANDLET_SHA256_MISMATCH",
            lambda: executor.build_execution_plan(
                manifest_path=self.fixture.manifest_path,
                acquisition_dir=self.fixture.acquisition_dir,
                job_dir=self.fixture.job_dir,
                engine_executable=self.fixture.engine,
                engine_sha256="0" * 64,
                interchange_tree_sha256=self.fixture.interchange_sha256(),
                project_file=self.fixture.project,
                project_sha256=self.fixture.project_sha256,
                project_control_sha256=self.fixture.control_sha256(),
                evidence_dir=self.fixture.evidence_dir,
                timeout_seconds=60,
                approval_ref=None,
                apply=False,
            ),
        )
        self.assert_error(
            "UE_COMMANDLET_PROJECT_TREE_MISMATCH",
            lambda: executor.build_execution_plan(
                manifest_path=self.fixture.manifest_path,
                acquisition_dir=self.fixture.acquisition_dir,
                job_dir=self.fixture.job_dir,
                engine_executable=self.fixture.engine,
                engine_sha256=self.fixture.engine_sha256,
                interchange_tree_sha256=self.fixture.interchange_sha256(),
                project_file=self.fixture.project,
                project_sha256=self.fixture.project_sha256,
                project_control_sha256="0" * 64,
                evidence_dir=self.fixture.evidence_dir,
                timeout_seconds=60,
                approval_ref=None,
                apply=False,
            ),
        )
        self.fixture.engine.chmod(0o722)
        self.assert_error("UE_COMMANDLET_FILE_UNSAFE", self.fixture.plan)

    def test_write_ahead_intent_precedes_runner_and_success_stays_review_pending(self) -> None:
        plan = self.fixture.plan()
        runner_called = False

        def runner(command, environment, stdout_path, _stderr_path, timeout_seconds):
            nonlocal runner_called
            runner_called = True
            intent_path = plan.evidence_dir / "execution-intent.json"
            self.assertTrue(intent_path.exists())
            self.assertFalse((plan.evidence_dir / "terminal-receipt.json").exists())
            self.assertEqual(stat.S_IMODE(plan.evidence_dir.stat().st_mode), 0o700)
            intent = json.loads(intent_path.read_text(encoding="utf-8"))
            self.assertEqual(intent["command"], list(command))
            self.assertEqual(intent["environment"], dict(environment))
            self.assertEqual(intent["working_directory"], str(self.fixture.project_root))
            self.assertEqual(timeout_seconds, 60)
            self.assertEqual(command[2:5], ("-run=pythonscript", f"-script={plan.evidence_dir / 'commandlet-import.py'}", "-unattended"))
            self.assertNotIn("PYTHONPATH", environment)
            self.fixture.write_imported_assets(plan)
            self.fixture.write_stdout(stdout_path, self.fixture.success_marker(plan))
            return executor.ProcessOutcome("exited", 0, None)

        result, code = executor.execute_apply(plan, runner=runner)
        self.assertTrue(runner_called)
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "imported_pending_review")
        self.assertEqual(result["mutation_state"], "imported_unreviewed")
        self.assertFalse(result["production_ready"])
        self.assertFalse(result["semantic_index_eligible"])
        receipt = self.fixture.receipt(plan)
        self.assertFalse(receipt["production_ready"])
        self.assertFalse(receipt["semantic_index_eligible"])
        self.assertTrue(receipt["quarantine_required"])
        self.assertEqual(set(receipt["review_gates"].values()), {"review_pending"})
        terminal = plan.evidence_dir / "terminal-receipt.json"
        terminal_metadata = terminal.stat()
        self.assertEqual(stat.S_IMODE(terminal_metadata.st_mode), 0o600)
        self.assertEqual(terminal_metadata.st_nlink, 1)
        self.assertFalse((plan.evidence_dir / ".terminal-receipt.json.tmp").exists())

    def test_project_one_shot_lock_survives_failure_and_blocks_other_evidence_dirs(self) -> None:
        plan = self.fixture.plan(evidence_name="first-attempt")

        def runner(_command, _environment, _stdout_path, _stderr_path, _timeout_seconds):
            return executor.ProcessOutcome("timeout", None, None)

        _result, code = executor.execute_apply(plan, runner=runner)
        self.assertEqual(code, 2)
        claim = self.fixture.project_root / executor.PROJECT_CLAIM_NAME
        metadata = claim.stat()
        self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o600)
        self.assertEqual(metadata.st_nlink, 1)
        self.assert_error(
            "UE_COMMANDLET_PROJECT_ALREADY_CLAIMED",
            lambda: self.fixture.replan_after_claim(plan, "second-attempt"),
        )

    def test_prelaunch_recomputes_project_and_acquisition_fingerprints(self) -> None:
        plan = self.fixture.plan(evidence_name="project-tamper")
        config = self.fixture.project_root / "Config" / "DefaultEngine.ini"
        config.write_bytes(b"[/Script/Engine.Engine]\nbUseFixedFrameRate=True\n")
        config.chmod(0o600)
        called = False

        def runner(*_args):
            nonlocal called
            called = True
            return executor.ProcessOutcome("exited", 0, None)

        result, code = executor.execute_apply(plan, runner=runner)
        self.assertFalse(called)
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "prelaunch_failed")
        self.assertEqual(self.fixture.receipt(plan)["error"]["code"], "UE_COMMANDLET_PROJECT_TREE_MISMATCH")

        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(pathlib.Path(temporary))
            acquisition_plan = fixture.plan(evidence_name="acquisition-tamper")
            called = False
            with mock.patch.object(acquisition, "verify_existing", return_value=False):
                result, code = executor.execute_apply(acquisition_plan, runner=runner)
            self.assertFalse(called)
            self.assertEqual(code, 2)
            self.assertEqual(result["status"], "prelaunch_failed")
            self.assertEqual(fixture.receipt(acquisition_plan)["error"]["code"], "UE_COMMANDLET_ACQUISITION_MISSING")

    def test_prelaunch_recomputes_interchange_pipeline_fingerprint(self) -> None:
        plan = self.fixture.plan(evidence_name="pipeline-tamper")
        pipeline = (
            self.fixture.engine.parents[2]
            / "Plugins/Interchange/Runtime/Content/Pipelines/DefaultGLTFPipeline.uasset"
        )
        pipeline.write_bytes(b"tampered-pipeline\n")
        pipeline.chmod(0o600)
        called = False

        def runner(*_args):
            nonlocal called
            called = True
            return executor.ProcessOutcome("exited", 0, None)

        result, code = executor.execute_apply(plan, runner=runner)
        self.assertFalse(called)
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "prelaunch_failed")
        self.assertEqual(self.fixture.receipt(plan)["error"]["code"], "UE_COMMANDLET_INTERCHANGE_TREE_MISMATCH")

    def test_timeout_is_ambiguous_quarantined_durable_and_never_retryable(self) -> None:
        plan = self.fixture.plan(evidence_name="timeout-r1")

        def runner(_command, _environment, _stdout_path, _stderr_path, _timeout_seconds):
            self.fixture.write_imported_assets(plan, count=1)
            return executor.ProcessOutcome("timeout", -15, None)

        result, code = executor.execute_apply(plan, runner=runner)
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "quarantined")
        self.assertEqual(result["mutation_state"], "ambiguous")
        self.assertTrue(result["quarantine_required"])
        self.assertFalse(result["automatic_retry_permitted"])
        receipt = self.fixture.receipt(plan)
        self.assertEqual(receipt["error"]["code"], "UE_COMMANDLET_TIMEOUT")
        self.assertFalse(receipt["automatic_retry_permitted"])
        self.assertFalse(receipt["delete_on_failure_permitted"])

    def test_signal_exit_is_ambiguous_and_has_a_durable_terminal_receipt(self) -> None:
        plan = self.fixture.plan(evidence_name="signal-r1")

        def runner(_command, _environment, _stdout_path, _stderr_path, _timeout_seconds):
            return executor.ProcessOutcome("exited", -11, 11)

        result, code = executor.execute_apply(plan, runner=runner)
        self.assertEqual(code, 2)
        self.assertEqual(result["mutation_state"], "ambiguous")
        self.assertTrue(result["quarantine_required"])
        receipt = self.fixture.receipt(plan)
        self.assertEqual(receipt["error"]["code"], "UE_COMMANDLET_SIGNAL")
        self.assertEqual(receipt["process"]["signal"], 11)

    def test_sigkill_path_never_uses_an_unbounded_wait(self) -> None:
        stdout = self.fixture.root / "bounded-stdout.log"
        stderr = self.fixture.root / "bounded-stderr.log"
        stdout.write_bytes(b"")
        stderr.write_bytes(b"")
        waits: list[float | int | None] = []
        process = mock.Mock()
        process.pid = 424242
        process.returncode = None
        process.poll.return_value = None

        def wait(*, timeout=None):
            waits.append(timeout)
            raise subprocess.TimeoutExpired(cmd="synthetic", timeout=timeout)

        process.wait.side_effect = wait
        with (
            mock.patch.object(executor.subprocess, "Popen", return_value=process),
            mock.patch.object(executor.os, "killpg"),
            mock.patch.object(executor, "_wait_process_group_gone", return_value=False),
        ):
            outcome = executor.run_commandlet(
                ("synthetic",),
                {},
                stdout,
                stderr,
                30,
                cwd=self.fixture.root,
            )
        self.assertEqual(outcome.kind, "timeout_unreaped")
        self.assertFalse(outcome.process_group_clean)
        self.assertEqual(waits, [30, executor.TERMINATION_GRACE_SECONDS, executor.KILL_REAP_GRACE_SECONDS])
        self.assertNotIn(None, waits)

    def test_normal_leader_exit_quarantines_observed_process_group_children(self) -> None:
        stdout = self.fixture.root / "children-stdout.log"
        stderr = self.fixture.root / "children-stderr.log"
        stdout.write_bytes(b"")
        stderr.write_bytes(b"")
        process = mock.Mock()
        process.pid = 434343
        process.wait.return_value = 0
        with (
            mock.patch.object(executor.subprocess, "Popen", return_value=process),
            mock.patch.object(executor, "_bounded_shutdown_process_group", return_value=(True, True)) as shutdown,
        ):
            outcome = executor.run_commandlet(
                ("synthetic",),
                {},
                stdout,
                stderr,
                30,
                cwd=self.fixture.root,
            )
        self.assertEqual(outcome.kind, "descendants_terminated")
        self.assertTrue(outcome.descendants_observed)
        shutdown.assert_called_once_with(process.pid)

    def test_strict_partial_marker_is_partial_and_quarantined(self) -> None:
        plan = self.fixture.plan(evidence_name="partial-r1")
        expected_ids = [asset["asset_id"] for asset in plan.job_bundle.prepared.job["assets"]]

        def runner(_command, _environment, stdout_path, _stderr_path, _timeout_seconds):
            self.fixture.write_imported_assets(plan, count=1)
            self.fixture.write_stdout(
                stdout_path,
                {
                    "completed_asset_ids": expected_ids[:1],
                    "error_code": "UE_COMMANDLET_IMPORT_FAILED",
                    "job_sha256": plan.job_bundle.prepared.job_sha256,
                    "production_ready": False,
                    "schema": executor.MARKER_SCHEMA,
                    "semantic_index_eligible": False,
                    "status": "failed_after_launch",
                },
            )
            return executor.ProcessOutcome("exited", 0, None)

        result, code = executor.execute_apply(plan, runner=runner)
        self.assertEqual(code, 2)
        self.assertEqual(result["mutation_state"], "partial")
        self.assertTrue(result["quarantine_required"])
        self.assertEqual(self.fixture.receipt(plan)["marker"]["completed_asset_ids"], expected_ids[:1])

    def test_nonzero_with_strict_partial_marker_preserves_partial_but_never_retries(self) -> None:
        plan = self.fixture.plan(evidence_name="nonzero-partial-r1")
        expected_ids = [asset["asset_id"] for asset in plan.job_bundle.prepared.job["assets"]]

        def runner(_command, _environment, stdout_path, _stderr_path, _timeout_seconds):
            self.fixture.write_imported_assets(plan, count=1)
            self.fixture.write_stdout(
                stdout_path,
                {
                    "completed_asset_ids": expected_ids[:1],
                    "error_code": "UE_COMMANDLET_IMPORT_FAILED",
                    "job_sha256": plan.job_bundle.prepared.job_sha256,
                    "production_ready": False,
                    "schema": executor.MARKER_SCHEMA,
                    "semantic_index_eligible": False,
                    "status": "failed_after_launch",
                },
            )
            return executor.ProcessOutcome("exited", 1, None)

        result, code = executor.execute_apply(plan, runner=runner)
        self.assertEqual(code, 2)
        self.assertEqual(result["mutation_state"], "partial")
        self.assertTrue(result["quarantine_required"])
        receipt = self.fixture.receipt(plan)
        self.assertEqual(receipt["process"]["returncode"], 1)
        self.assertFalse(receipt["automatic_retry_permitted"])

    def test_missing_and_malformed_markers_are_ambiguous(self) -> None:
        for name, malformed in (("missing-r1", False), ("malformed-r1", True)):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                fixture = Fixture(pathlib.Path(temporary))
                plan = fixture.plan(evidence_name=name)

                def runner(_command, _environment, stdout_path, _stderr_path, _timeout_seconds):
                    fixture.write_imported_assets(plan)
                    if malformed:
                        fixture.write_stdout(stdout_path, {**fixture.success_marker(plan), "unexpected": True})
                    return executor.ProcessOutcome("exited", 0, None)

                result, code = executor.execute_apply(plan, runner=runner)
                self.assertEqual(code, 2)
                self.assertEqual(result["mutation_state"], "ambiguous")
                self.assertTrue(result["quarantine_required"])
                self.assertIsNone(fixture.receipt(plan)["marker"])

    def test_marker_must_be_terminal_line_anchored_and_keep_original_log_inode(self) -> None:
        for case in ("unanchored", "replaced-inode"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                fixture = Fixture(pathlib.Path(temporary))
                plan = fixture.plan(evidence_name=case)

                def runner(_command, _environment, stdout_path, _stderr_path, _timeout_seconds):
                    fixture.write_imported_assets(plan)
                    if case == "unanchored":
                        with stdout_path.open("ab") as handle:
                            handle.write(
                                b"noise "
                                + executor.MARKER_PREFIX
                                + executor.compact_json(fixture.success_marker(plan)).encode()
                                + b"\n"
                            )
                    else:
                        # Keep the original inode alive so the replacement
                        # cannot accidentally reuse its inode number.
                        with stdout_path.open("rb"):
                            stdout_path.unlink()
                            stdout_path.write_bytes(
                                b"LogPython: "
                                + executor.MARKER_PREFIX
                                + executor.compact_json(fixture.success_marker(plan)).encode()
                                + b"\n"
                            )
                            stdout_path.chmod(0o600)
                    return executor.ProcessOutcome("exited", 0, None)

                result, code = executor.execute_apply(plan, runner=runner)
                self.assertEqual(code, 2)
                self.assertEqual(result["mutation_state"], "ambiguous")
                receipt = fixture.receipt(plan)
                expected = "UE_COMMANDLET_MARKER_INVALID" if case == "unanchored" else "UE_COMMANDLET_FILE_CHANGED"
                self.assertEqual(receipt["error"]["code"], expected)
                self.assertTrue((plan.evidence_dir / "terminal-receipt.json").exists())

    def test_postlaunch_content_observation_error_still_publishes_terminal_receipt(self) -> None:
        plan = self.fixture.plan(evidence_name="observation-fault")

        def runner(_command, _environment, _stdout_path, _stderr_path, _timeout_seconds):
            return executor.ProcessOutcome("timeout", None, None)

        with mock.patch.object(executor, "observe_vista_content", side_effect=OSError("fault injection")):
            result, code = executor.execute_apply(plan, runner=runner)
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "quarantined")
        receipt = self.fixture.receipt(plan)
        self.assertEqual(
            receipt["content_vista_observation"]["observation_error"],
            "UE_COMMANDLET_CONTENT_OBSERVATION_FAILED",
        )
        self.assertTrue((plan.evidence_dir / "terminal-receipt.json").exists())

    def test_postlaunch_parse_and_identity_hash_faults_still_publish_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(pathlib.Path(temporary))
            plan = fixture.plan(evidence_name="parse-fault")

            def success_runner(_command, _environment, stdout_path, _stderr_path, _timeout_seconds):
                fixture.write_imported_assets(plan)
                fixture.write_stdout(stdout_path, fixture.success_marker(plan))
                return executor.ProcessOutcome("exited", 0, None)

            with mock.patch.object(executor, "parse_single_marker", side_effect=OSError("parse fault")):
                result, code = executor.execute_apply(plan, runner=success_runner)
            self.assertEqual(code, 2)
            self.assertEqual(result["mutation_state"], "ambiguous")
            self.assertEqual(
                fixture.receipt(plan)["error"]["code"],
                "UE_COMMANDLET_MARKER_OBSERVATION_FAILED",
            )

        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(pathlib.Path(temporary))
            plan = fixture.plan(evidence_name="identity-hash-fault")
            calls = 0

            def flaky_validate_engine(*_args, **_kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    return plan.engine, plan.interchange_runtime_entries, plan.interchange_runtime_sha256
                raise OSError("postlaunch hash fault")

            def timeout_runner(_command, _environment, _stdout_path, _stderr_path, _timeout_seconds):
                return executor.ProcessOutcome("timeout", None, None)

            with mock.patch.object(executor, "validate_engine", side_effect=flaky_validate_engine):
                result, code = executor.execute_apply(plan, runner=timeout_runner)
            self.assertEqual(code, 2)
            self.assertEqual(result["status"], "quarantined")
            receipt = fixture.receipt(plan)
            self.assertFalse(receipt["identity_unchanged_after_launch"])
            self.assertTrue((plan.evidence_dir / "terminal-receipt.json").exists())

    def test_source_tamper_after_plan_writes_terminal_receipt_but_never_launches(self) -> None:
        plan = self.fixture.plan(evidence_name="tamper-r1")
        target = pathlib.Path(plan.job_bundle.source_files[0]["absolute_path"])
        raw = target.read_bytes()
        target.write_bytes(bytes([raw[0] ^ 0xFF]) + raw[1:])
        target.chmod(0o600)
        called = False

        def runner(*_args):
            nonlocal called
            called = True
            return executor.ProcessOutcome("exited", 0, None)

        result, code = executor.execute_apply(plan, runner=runner)
        self.assertFalse(called)
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "prelaunch_failed")
        self.assertEqual(result["mutation_state"], "none")
        self.assertTrue(result["quarantine_required"])
        receipt = self.fixture.receipt(plan)
        self.assertFalse(receipt["unreal_started"])
        self.assertTrue((plan.evidence_dir / "execution-intent.json").exists())
        self.assertEqual(receipt["error"]["code"], "UE_COMMANDLET_PRELAUNCH_TAMPER")

    def test_generated_script_seals_all_fifteen_sources_before_first_import(self) -> None:
        plan = self.fixture.plan(evidence_name="script-audit-r1")
        script = executor.render_fixed_commandlet_script(plan).decode("utf-8")
        ast.parse(script)
        self.assertEqual(len(plan.job_bundle.source_files), 15)
        self.assertLess(script.index('for source in PAYLOAD["source_files"]'), script.index("manager.import_asset("))
        self.assertNotIn("AssetImportTask", script)
        self.assertIn("unreal.StaticMesh.static_class()", script)
        payload = executor._script_payload(plan)
        self.assertEqual(tuple(payload["interchange_pipelines"]), executor.INTERCHANGE_PIPELINES)
        self.assertEqual(tuple(payload["interchange_pipeline_classes"]), executor.INTERCHANGE_PIPELINE_CLASSES)
        self.assertEqual(payload["interchange_static_mesh_factory_class"], executor.INTERCHANGE_STATIC_MESH_FACTORY_CLASS)
        self.assertIn('parameters.set_editor_property("is_automated", True)', script)
        self.assertIn('parameters.set_editor_property("follow_redirectors", False)', script)
        self.assertIn('parameters.set_editor_property("override_pipelines", pipeline_paths)', script)
        self.assertIn('"semantic_index_eligible": False', script)

    def test_existing_vista_or_evidence_path_is_never_overwritten(self) -> None:
        vista = self.fixture.project_root / "Content" / "VISTA"
        vista.mkdir(mode=0o700)
        self.assert_error("UE_COMMANDLET_PROJECT_SHAPE_INVALID", self.fixture.plan)
        vista.rmdir()
        self.fixture.evidence_dir.mkdir(mode=0o700)
        self.assert_error("UE_COMMANDLET_EVIDENCE_EXISTS", self.fixture.plan)


class LiveApplyQuarantineTests(unittest.TestCase):
    def assert_quarantined(self, callback) -> None:
        with self.assertRaises(executor.CommandletExecutionError) as caught:
            callback()
        self.assertEqual(caught.exception.code, executor.LIVE_APPLY_QUARANTINE_CODE)

    def test_all_programmatic_live_entry_points_fail_before_touching_arguments(self) -> None:
        self.assertTrue(executor.LIVE_APPLY_QUARANTINED)
        self.assert_quarantined(lambda: executor.execute_apply(None))
        self.assert_quarantined(
            lambda: executor.run_commandlet(
                (), {}, pathlib.Path("/does/not/exist/stdout"),
                pathlib.Path("/does/not/exist/stderr"), 30,
                cwd=pathlib.Path("/does/not/exist"),
            )
        )

    def test_cli_apply_fails_before_planning_or_filesystem_inspection(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = executor.main([
                "--acquisition-dir", "/does/not/exist/acquisition",
                "--job-dir", "/does/not/exist/job",
                "--engine-executable", "/does/not/exist/UnrealEditor-Cmd",
                "--engine-sha256", "0" * 64,
                "--project", "/does/not/exist/project.uproject",
                "--project-sha256", "0" * 64,
                "--evidence-dir", "/does/not/exist/evidence",
                "--apply",
            ])
        self.assertEqual(code, 2)
        result = json.loads(stderr.getvalue())
        self.assertEqual(result["error"]["code"], executor.LIVE_APPLY_QUARANTINE_CODE)


if __name__ == "__main__":
    unittest.main()
