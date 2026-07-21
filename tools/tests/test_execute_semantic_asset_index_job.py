from __future__ import annotations

import ast
import copy
import contextlib
import dataclasses
import datetime as dt
import hashlib
import io
import json
import os
import pathlib
import inspect
import signal
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock


TOOLS_DIR = pathlib.Path(__file__).resolve().parents[1]
REPO_ROOT = TOOLS_DIR.parent
sys.path.insert(0, str(TOOLS_DIR))

import execute_semantic_asset_index_job as executor  # noqa: E402
import prepare_semantic_asset_index_job as preparation  # noqa: E402
import semantic_index_job_adapters as adapter_contract  # noqa: E402
from semantic_index_job_adapters import (  # noqa: E402
    OfflineFixtureAdapter,
    PhaseRequest,
    PhaseResult,
    SecretSnapshot,
)
from tests.test_prepare_semantic_asset_index_job import Fixture  # noqa: E402


APPROVAL = "CHANGE-SEMANTIC-EXECUTOR-20260721"
NO_SECRET_PATHS = executor.SecretPathArguments(None, None, None, None)
NO_SECRET_SNAPSHOT = SecretSnapshot(None, None, None, None)
NO_SECRETS = NO_SECRET_SNAPSHOT
REAL_VERIFY_GIT_SOURCE_CLOSURE = executor._verify_git_source_closure
REAL_REQUIRE_ISOLATED_PYTHON = executor._require_isolated_python


class CountingAdapter(OfflineFixtureAdapter):
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def run_phase(self, request):
        self.calls.append((request.phase, request.idempotency_key))
        return super().run_phase(request)


class FlakyAdapter(CountingAdapter):
    def __init__(self, phase: str) -> None:
        super().__init__()
        self.phase = phase
        self.failed = False

    def run_phase(self, request):
        self.calls.append((request.phase, request.idempotency_key))
        if request.phase == self.phase and not self.failed:
            self.failed = True
            raise RuntimeError("secret-bearing synthetic adapter failure")
        return OfflineFixtureAdapter.run_phase(self, request)


class WrongCountAdapter(OfflineFixtureAdapter):
    def run_phase(self, request):
        result = super().run_phase(request)
        if request.phase != "caption":
            return result
        return PhaseResult(
            phase=result.phase,
            idempotency_key=result.idempotency_key,
            metrics={"catalog_records": 1},
            evidence_sha256=result.evidence_sha256,
            observed_live=False,
        )


class TimeoutAdapter(OfflineFixtureAdapter):
    def run_phase(self, request):
        raise TimeoutError("synthetic secret-bearing timeout")


class SleepingAdapter(OfflineFixtureAdapter):
    def run_phase(self, request):
        time.sleep(2)
        return super().run_phase(request)


class MutatingAdapter(OfflineFixtureAdapter):
    def run_phase(self, request):
        request.job["pending_objects"]["count"] = 999
        request.execution_plan["limits"]["expected_postgres_rows"] = 999
        request.job["pending_objects"]["count"] = len(request.job["pending_objects"]["assets"])
        request.execution_plan["limits"]["expected_postgres_rows"] = request.job["pending_objects"]["count"]
        return super().run_phase(request)


class ProductionFixtureAdapter:
    """Offline-only live-shaped adapter for executor journal contract tests."""

    adapter_id = "production-metadata-fixture"
    adapter_contract_revision = "production-metadata-fixture/v1"
    production_capable = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def run_phase(self, request):
        self.calls.append((request.phase, request.idempotency_key))
        count = request.job["pending_objects"]["count"]
        views = request.execution_plan["limits"]["expected_rendered_views"]
        metrics = {
            "inspect": {"assets_inspected": count},
            "render": {"assets_rendered": count, "rendered_views": views},
            "caption": {"catalog_records": count},
            "embed": {"dense_vectors": count, "sparse_vectors": count},
            "postgres": {"postgres_rows": count},
            "qdrant": {"qdrant_points": count},
            "reconcile": {
                "catalog_records": count,
                "postgres_rows": count,
                "qdrant_points": count,
            },
        }[request.phase]
        reconcile = request.phase == "reconcile"
        return PhaseResult(
            phase=request.phase,
            idempotency_key=request.idempotency_key,
            metrics=metrics,
            evidence_sha256=hashlib.sha256(
                f"{request.phase}:{request.idempotency_key}".encode("ascii")
            ).hexdigest(),
            observed_live=True,
            snapshot_revision=(
                request.job["snapshot_target"]["asset_snapshot_revision"]
                if reconcile
                else None
            ),
            live_audit_receipt_sha256="d" * 64 if reconcile else None,
            observed_runtime_images=(
                copy.deepcopy(request.execution_plan["runtime_images"])
                if reconcile
                else None
            ),
            credential_target_identities={
                label: binding["target_identity"]
                for label, binding in request.execution_plan["credential_bindings"].items()
            },
        )


class SemanticIndexExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.root.chmod(0o700)
        preparation_root = self.root / "preparation"
        preparation_root.mkdir(mode=0o700)
        self.fixture = Fixture(preparation_root)
        prepared = self.fixture.prepare()
        preparation.publish_job(prepared, self.fixture.output_dir, "CHANGE-SEMANTIC-INDEX-20260721")
        self.job_path = self.fixture.output_dir / "semantic-index-job.json"
        self.receipt_path = self.fixture.output_dir / "preparation-receipt.json"
        self.job = json.loads(self.job_path.read_text(encoding="utf-8"))
        self.receipt = json.loads(self.receipt_path.read_text(encoding="utf-8"))
        self.plan_path = self.root / "execution-plan.json"
        self.run_parent = self.root / "runs"
        self.run_parent.mkdir(mode=0o700)
        self.run_dir = self.run_parent / "run-r1"
        self.git_commit = executor._run_fixed_git(
            REPO_ROOT,
            ["rev-parse", "--verify", "HEAD^{commit}"],
            maximum_stdout=128,
        ).decode("ascii").strip()
        self.git_object_format = executor._run_fixed_git(
            REPO_ROOT,
            ["rev-parse", "--show-object-format"],
            maximum_stdout=32,
        ).decode("ascii").strip()
        self.attestation_patcher = mock.patch.object(
            executor,
            "_verify_git_source_closure",
            side_effect=self.fake_source_attestation,
        )
        self.attestation_patcher.start()
        self.isolation_patcher = mock.patch.object(executor, "_require_isolated_python")
        self.isolation_patcher.start()
        self.write_plan(self.make_plan())

    def tearDown(self) -> None:
        self.isolation_patcher.stop()
        self.attestation_patcher.stop()
        self.temporary.cleanup()

    def fake_source_attestation(self, repo_root, pins):
        if pathlib.Path(repo_root) != REPO_ROOT:
            executor.fail("SEMANTIC_EXECUTOR_SOURCE_PIN_MISMATCH", "Synthetic repository mismatch")
        if pins["git_commit"] != self.git_commit:
            executor.fail("SEMANTIC_EXECUTOR_GIT_PIN_MISMATCH", "Synthetic Git mismatch")
        if pins["git_object_format"] != self.git_object_format:
            executor.fail("SEMANTIC_EXECUTOR_GIT_PIN_MISMATCH", "Synthetic Git format mismatch")
        sources = {}
        for pin_key, relative_path in executor.SOURCE_PIN_PATHS.items():
            raw = (REPO_ROOT / relative_path).read_bytes()
            if hashlib.sha256(raw).hexdigest() != pins[pin_key]:
                executor.fail("SEMANTIC_EXECUTOR_SOURCE_PIN_MISMATCH", "Synthetic source mismatch")
            sources[relative_path] = raw
        return self.git_commit, sources

    def source_hash(self, name: str) -> str:
        return hashlib.sha256((TOOLS_DIR / name).read_bytes()).hexdigest()

    def make_plan(self) -> dict:
        count = self.job["pending_objects"]["count"]
        return {
            "schema": executor.EXECUTION_PLAN_SCHEMA,
            "profile": "offline_fixture",
            "job": {
                "job_schema": self.job["schema"],
                "job_sha256": hashlib.sha256(self.job_path.read_bytes()).hexdigest(),
                "job_revision": self.job["job_revision"],
                "preparation_receipt_sha256": hashlib.sha256(self.receipt_path.read_bytes()).hexdigest(),
                "bootstrap_receipt_sha256": self.job["input_contract"]["bootstrap_receipt_sha256"],
                "bootstrap_bundle_revision": self.job["input_contract"]["bootstrap_bundle_revision"],
                "object_manifest_sha256": self.job["input_contract"]["object_manifest_sha256"],
                "recipe_sha256": self.job["recipe_contract"]["recipe_sha256"],
                "project_revision": self.job["input_contract"]["project"]["revision"],
                "content_revision": self.job["input_contract"]["content"]["revision"],
                "pending_objects_sha256": self.job["pending_objects"]["assets_sha256"],
                "pending_object_count": count,
                "asset_snapshot_revision": self.job["snapshot_target"]["asset_snapshot_revision"],
            },
            "executor": {
                "git_commit": self.git_commit,
                "git_object_format": self.git_object_format,
                "executor_source_sha256": self.source_hash("execute_semantic_asset_index_job.py"),
                "adapter_source_sha256": self.source_hash("semantic_index_job_adapters.py"),
                "preparer_source_sha256": self.source_hash("prepare_semantic_asset_index_job.py"),
                "bootstrap_source_sha256": self.source_hash("build_ue_asset_registry_bootstrap.py"),
                "job_schema_sha256": self.source_hash("semantic_asset_index_job_schema.json"),
                "execution_plan_schema_sha256": self.source_hash("semantic_asset_index_execution_plan_schema.json"),
            },
            "adapter": {
                "adapter_id": OfflineFixtureAdapter.adapter_id,
                "adapter_contract_revision": OfflineFixtureAdapter.adapter_contract_revision,
                "production_capable": False,
            },
            "caption": copy.deepcopy(self.job["recipe_contract"]["caption"]),
            "embedding": copy.deepcopy(self.job["recipe_contract"]["embedding"]),
            "runtime_images": {
                "unreal": "registry.example/unreal@sha256:" + "6" * 64,
                "postgres": "postgres@sha256:" + "7" * 64,
                "qdrant": "qdrant/qdrant@sha256:" + "8" * 64,
                "embedding": "registry.example/embedding@sha256:" + "9" * 64,
            },
            "storage": copy.deepcopy(self.job["recipe_contract"]["storage"]),
            "limits": {
                "phase_timeout_seconds": 10,
                "max_total_elapsed_seconds": 300,
                "expected_catalog_records": count,
                "expected_postgres_rows": count,
                "expected_qdrant_points": count,
                "expected_dense_vectors": count,
                "expected_sparse_vectors": count,
                "expected_rendered_views": count * self.job["recipe_contract"]["caption"]["views_per_asset"],
            },
            "credential_files_required": [],
            "credential_bindings": {},
            "approval_ref_sha256": executor._approval_hash(APPROVAL),
        }

    def production_credential_bindings(self, generation: str = "r1") -> dict:
        credential_generation = f"credential-generation:{generation}"
        return {
            "caption": {
                "credential_generation": credential_generation,
                "target_identity": "provider-project:simworld-caption-prod",
            },
            "postgres": {
                "credential_generation": credential_generation,
                "target_identity": "postgres-deployment:simworld-prod/schema:semantic_assets_v2",
            },
            "qdrant": {
                "credential_generation": credential_generation,
                "target_identity": "qdrant-cluster:simworld-prod/collection:simworld_assets_v1",
            },
            "embedding": {
                "credential_generation": credential_generation,
                "target_identity": "embedding-project:simworld-prod/model:bge-m3-pinned",
            },
        }

    def write_plan(self, plan: dict) -> None:
        self.plan_path.write_bytes(executor.canonical_json(plan))
        self.plan_path.chmod(0o600)

    def pins(self) -> dict:
        return {
            "expected_job_sha256": hashlib.sha256(self.job_path.read_bytes()).hexdigest(),
            "expected_preparation_receipt_sha256": hashlib.sha256(self.receipt_path.read_bytes()).hexdigest(),
            "expected_execution_plan_sha256": hashlib.sha256(self.plan_path.read_bytes()).hexdigest(),
            "expected_job_schema_sha256": self.source_hash("semantic_asset_index_job_schema.json"),
            "expected_execution_plan_schema_sha256": self.source_hash("semantic_asset_index_execution_plan_schema.json"),
        }

    def validate(self) -> executor.ValidatedBundle:
        return executor.validate_bundle(
            job_path=self.job_path,
            preparation_receipt_path=self.receipt_path,
            execution_plan_path=self.plan_path,
            repo_root=REPO_ROOT,
            **self.pins(),
        )

    def assert_error(self, code: str, callback) -> executor.SemanticIndexExecutorError:
        with self.assertRaises(executor.SemanticIndexExecutorError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def execute_bundle(self, **kwargs):
        adapter = kwargs.pop("adapter")
        secret_snapshot = kwargs.pop("secret_files")
        kwargs.pop("requested_adapter_id")
        runtime = executor.AdapterRuntime(
            module=adapter_contract,
            instance=adapter,
            phase_request_type=PhaseRequest,
            phase_result_type=PhaseResult,
            secret_snapshot_type=SecretSnapshot,
        )
        return executor._execute_with_adapter(
            adapter=runtime,
            secret_snapshot=secret_snapshot,
            **kwargs,
        )

    def production_runtime(
        self,
        adapter: ProductionFixtureAdapter | None = None,
    ) -> executor.AdapterRuntime:
        return executor.AdapterRuntime(
            module=adapter_contract,
            instance=adapter or ProductionFixtureAdapter(),
            phase_request_type=PhaseRequest,
            phase_result_type=PhaseResult,
            secret_snapshot_type=SecretSnapshot,
        )

    def write_abrupt_termination_harness(self) -> pathlib.Path:
        harness = self.root / "semantic-executor-abrupt-harness.py"
        harness.write_text(
            r'''from __future__ import annotations

import copy
import hashlib
import json
import os
import pathlib
import signal
import sys

config = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
sys.path.insert(0, config["tools_dir"])

import execute_semantic_asset_index_job as executor
import semantic_index_job_adapters as adapter_contract


class ProductionFixtureAdapter:
    adapter_id = "production-metadata-fixture"
    adapter_contract_revision = "production-metadata-fixture/v1"
    production_capable = True

    def run_phase(self, request):
        count = request.job["pending_objects"]["count"]
        views = request.execution_plan["limits"]["expected_rendered_views"]
        metrics = {
            "inspect": {"assets_inspected": count},
            "render": {"assets_rendered": count, "rendered_views": views},
            "caption": {"catalog_records": count},
            "embed": {"dense_vectors": count, "sparse_vectors": count},
            "postgres": {"postgres_rows": count},
            "qdrant": {"qdrant_points": count},
            "reconcile": {
                "catalog_records": count,
                "postgres_rows": count,
                "qdrant_points": count,
            },
        }[request.phase]
        reconcile = request.phase == "reconcile"
        return adapter_contract.PhaseResult(
            phase=request.phase,
            idempotency_key=request.idempotency_key,
            metrics=metrics,
            evidence_sha256=hashlib.sha256(
                f"{request.phase}:{request.idempotency_key}".encode("ascii")
            ).hexdigest(),
            observed_live=True,
            snapshot_revision=(
                request.job["snapshot_target"]["asset_snapshot_revision"]
                if reconcile
                else None
            ),
            live_audit_receipt_sha256="d" * 64 if reconcile else None,
            observed_runtime_images=(
                copy.deepcopy(request.execution_plan["runtime_images"])
                if reconcile
                else None
            ),
            credential_target_identities={
                label: binding["target_identity"]
                for label, binding in request.execution_plan["credential_bindings"].items()
            },
        )


def fake_source_attestation(repo_root, pins):
    root = pathlib.Path(repo_root)
    sources = {}
    for pin_key, relative_path in executor.SOURCE_PIN_PATHS.items():
        raw = (root / relative_path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != pins[pin_key]:
            executor.fail(
                "SEMANTIC_EXECUTOR_SOURCE_PIN_MISMATCH",
                "Synthetic subprocess source mismatch",
            )
        sources[relative_path] = raw
    return pins["git_commit"], sources


executor._verify_git_source_closure = fake_source_attestation
bundle = executor.validate_bundle(
    job_path=pathlib.Path(config["job_path"]),
    preparation_receipt_path=pathlib.Path(config["receipt_path"]),
    execution_plan_path=pathlib.Path(config["plan_path"]),
    repo_root=pathlib.Path(config["repo_root"]),
    **config["pins"],
)
runtime = executor.AdapterRuntime(
    module=adapter_contract,
    instance=ProductionFixtureAdapter(),
    phase_request_type=adapter_contract.PhaseRequest,
    phase_result_type=adapter_contract.PhaseResult,
    secret_snapshot_type=adapter_contract.SecretSnapshot,
)

real_os_open = os.open
real_os_write = os.write
real_os_fsync = os.fsync
real_os_close = os.close
triggered = False
released = False


def stop_here():
    global released
    def release(_signum, _frame):
        global released
        released = True

    signal.signal(signal.SIGUSR1, release)
    marker_fd = real_os_open(
        config["marker_path"],
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        raw = config["point"].encode("ascii")
        offset = 0
        while offset < len(raw):
            offset += real_os_write(marker_fd, raw[offset:])
        real_os_fsync(marker_fd)
    finally:
        real_os_close(marker_fd)
    while not released:
        signal.pause()


def trigger_once():
    global triggered
    if not triggered:
        triggered = True
        stop_here()


point = config["point"]
if point == "state_temp_after_write":
    real = executor.os.write

    def injected_write(descriptor, raw):
        result = real(descriptor, raw)
        trigger_once()
        return result

    executor.os.write = injected_write
elif point == "state_temp_after_fsync":
    real = executor.os.fsync

    def injected_fsync(descriptor):
        result = real(descriptor)
        trigger_once()
        return result

    executor.os.fsync = injected_fsync
elif point == "state_after_rename":
    real = executor.os.replace

    def injected_replace(source, destination, *, src_dir_fd=None, dst_dir_fd=None):
        result = real(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )
        trigger_once()
        return result

    executor.os.replace = injected_replace
elif point == "state_after_dir_fsync":
    real = executor._fsync_directory_fd

    def injected_directory_fsync(descriptor):
        result = real(descriptor)
        trigger_once()
        return result

    executor._fsync_directory_fd = injected_directory_fsync
elif point in {"adapter_after_return", "hold_adapter_before_return"}:
    real = executor._invoke_phase

    def injected_phase(adapter, request, *, timeout_seconds):
        if point == "hold_adapter_before_return":
            trigger_once()
        result = real(adapter, request, timeout_seconds=timeout_seconds)
        trigger_once()
        return result

    executor._invoke_phase = injected_phase
elif point in {"phase_completed_after_commit", "terminal_state_after_commit"}:
    real = executor._atomic_write_at

    def injected_atomic(handle, name, raw, *, replace):
        result = real(handle, name, raw, replace=replace)
        if name == "execution-state.json":
            document = json.loads(raw)
            if (
                point == "phase_completed_after_commit"
                and document["phases"]["inspect"]["status"] == "completed"
                and document["terminal_receipt_sha256"] is None
            ) or (
                point == "terminal_state_after_commit"
                and document["terminal_receipt_sha256"] is not None
            ):
                trigger_once()
        return result

    executor._atomic_write_at = injected_atomic
elif point in {"terminal_before_rename", "terminal_after_rename"}:
    real = executor._rename_no_replace_at

    def injected_rename(source_fd, source_name, destination_fd, destination_name):
        if destination_name == "terminal-receipt.json" and point == "terminal_before_rename":
            trigger_once()
        result = real(source_fd, source_name, destination_fd, destination_name)
        if destination_name == "terminal-receipt.json":
            trigger_once()
        return result

    executor._rename_no_replace_at = injected_rename
else:
    raise RuntimeError("unknown crash point")

try:
    terminal = executor._execute_with_adapter(
        bundle=bundle,
        adapter=runtime,
        run_dir=pathlib.Path(config["run_dir"]),
        approval_ref=config["approval_ref"],
        secret_snapshot=adapter_contract.SecretSnapshot(None, None, None, None),
    )
except executor.SemanticIndexExecutorError as error:
    print(executor.compact_json({"error": error.public_dict()}))
    raise SystemExit(2)
print(executor.compact_json(terminal))
''',
            encoding="utf-8",
        )
        harness.chmod(0o600)
        return harness

    def launch_abrupt_subprocess(
        self,
        *,
        harness: pathlib.Path,
        point: str,
        run_dir: pathlib.Path,
        marker: pathlib.Path,
    ) -> subprocess.Popen[str]:
        config_path = self.root / f"abrupt-{point}-{run_dir.name}.json"
        config = {
            "tools_dir": str(TOOLS_DIR),
            "repo_root": str(REPO_ROOT),
            "job_path": str(self.job_path),
            "receipt_path": str(self.receipt_path),
            "plan_path": str(self.plan_path),
            "pins": self.pins(),
            "run_dir": str(run_dir),
            "approval_ref": APPROVAL,
            "marker_path": str(marker),
            "point": point,
        }
        config_path.write_bytes(executor.canonical_json(config))
        config_path.chmod(0o600)
        return subprocess.Popen(
            [executor.SYSTEM_PYTHON, "-I", "-S", str(harness), str(config_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )

    def wait_for_subprocess_marker(
        self,
        process: subprocess.Popen[str],
        marker: pathlib.Path,
    ) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if marker.is_file():
                return
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                self.fail(
                    f"abrupt subprocess exited before marker: {process.returncode}; "
                    f"stdout={stdout!r}; stderr={stderr!r}"
                )
            time.sleep(0.01)
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate(timeout=5)
        self.fail(f"abrupt subprocess marker timeout; stdout={stdout!r}; stderr={stderr!r}")

    def test_dry_run_validates_all_pins_and_writes_nothing(self) -> None:
        bundle = self.validate()
        result = executor.dry_run_result(bundle)
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["phase_order"], list(executor.PHASES))
        self.assertFalse(result["execution_started"])
        self.assertFalse(result["production_complete"])
        self.assertFalse(self.run_dir.exists())

    def test_fixture_execution_is_private_resumable_and_never_production(self) -> None:
        bundle = self.validate()
        adapter = CountingAdapter()
        receipt = self.execute_bundle(
            bundle=bundle,
            adapter=adapter,
            requested_adapter_id=adapter.adapter_id,
            run_dir=self.run_dir,
            approval_ref=APPROVAL,
            secret_files=NO_SECRETS,
        )
        self.assertEqual([phase for phase, _key in adapter.calls], list(executor.PHASES))
        self.assertEqual(receipt["status"], "offline_fixture_complete")
        self.assertFalse(receipt["production_complete"])
        self.assertFalse(receipt["catalog_complete"])
        self.assertFalse(receipt["snapshot_complete"])
        self.assertFalse(receipt["legacy_runner_used"])
        self.assertFalse(receipt["shell_used"])
        self.assertIsNone(receipt["live_audit_receipt_sha256"])
        self.assertEqual(stat.S_IMODE(self.run_dir.stat().st_mode), 0o700)
        for name in ("executor.lock", "execution-state.json", "terminal-receipt.json"):
            self.assertEqual(stat.S_IMODE((self.run_dir / name).stat().st_mode), 0o600)

        resumed = self.execute_bundle(
            bundle=bundle,
            adapter=adapter,
            requested_adapter_id=adapter.adapter_id,
            run_dir=self.run_dir,
            approval_ref=APPROVAL,
            secret_files=NO_SECRETS,
        )
        self.assertEqual(resumed, receipt)
        self.assertEqual(len(adapter.calls), len(executor.PHASES))

    def test_failed_phase_resumes_with_same_idempotency_key(self) -> None:
        bundle = self.validate()
        adapter = FlakyAdapter("caption")
        error = self.assert_error(
            "SEMANTIC_EXECUTOR_ADAPTER_FAILED",
            lambda: self.execute_bundle(
                bundle=bundle,
                adapter=adapter,
                requested_adapter_id=adapter.adapter_id,
                run_dir=self.run_dir,
                approval_ref=APPROVAL,
                secret_files=NO_SECRETS,
            ),
        )
        self.assertNotIn("secret-bearing", json.dumps(error.public_dict()))
        state = json.loads((self.run_dir / "execution-state.json").read_text())
        self.assertEqual(state["phases"]["inspect"]["status"], "completed")
        self.assertEqual(state["phases"]["caption"]["status"], "failed")

        terminal = self.execute_bundle(
            bundle=bundle,
            adapter=adapter,
            requested_adapter_id=adapter.adapter_id,
            run_dir=self.run_dir,
            approval_ref=APPROVAL,
            secret_files=NO_SECRETS,
        )
        caption_keys = [key for phase, key in adapter.calls if phase == "caption"]
        self.assertEqual(len(caption_keys), 2)
        self.assertEqual(len(set(caption_keys)), 1)
        self.assertEqual(terminal["status"], "offline_fixture_complete")

    def test_production_profile_fails_closed_without_reviewed_adapter(self) -> None:
        plan = self.make_plan()
        plan["profile"] = "production"
        plan["credential_files_required"] = list(executor.SECRET_LABELS)
        plan["credential_bindings"] = self.production_credential_bindings()
        self.write_plan(plan)
        bundle = self.validate()
        self.assert_error(
            "SEMANTIC_EXECUTOR_ADAPTER_NOT_PRODUCTION",
            lambda: executor._validate_requested_adapter_plan(
                bundle,
                requested_adapter_id=OfflineFixtureAdapter.adapter_id,
            ),
        )

        secret = self.root / "shared.secret"
        secret.write_text("x" * 64 + "\n", encoding="utf-8")
        secret.chmod(0o600)
        shared = str(secret)
        self.assert_error(
            "SEMANTIC_EXECUTOR_SECRET_FILE_INVALID",
            lambda: executor._validate_secret_path_contract(
                bundle.plan,
                executor.SecretPathArguments(shared, shared, shared, shared),
            ),
        )

        plan["credential_files_required"] = []
        self.write_plan(plan)
        self.assert_error("SEMANTIC_EXECUTOR_PLAN_INVALID", self.validate)

    def test_credential_binding_contract_is_profile_closed(self) -> None:
        offline = self.make_plan()
        offline["credential_bindings"] = self.production_credential_bindings()
        self.write_plan(offline)
        self.assert_error("SEMANTIC_EXECUTOR_PLAN_INVALID", self.validate)

        production = self.make_plan()
        production["profile"] = "production"
        production["credential_files_required"] = list(executor.SECRET_LABELS)
        production["credential_bindings"] = self.production_credential_bindings()
        del production["credential_bindings"]["embedding"]
        self.write_plan(production)
        self.assert_error("SEMANTIC_EXECUTOR_SCHEMA_INVALID", self.validate)

        production = self.make_plan()
        production["profile"] = "production"
        production["credential_files_required"] = list(executor.SECRET_LABELS)
        production["credential_bindings"] = self.production_credential_bindings()
        production["credential_bindings"]["qdrant"]["target_identity"] = (
            "postgres-deployment:wrong-kind/schema:semantic_assets_v2"
        )
        self.write_plan(production)
        self.assert_error("SEMANTIC_EXECUTOR_PLAN_INVALID", self.validate)

    def test_phase_results_must_report_every_sealed_credential_target(self) -> None:
        plan = self.make_plan()
        plan["profile"] = "production"
        plan["credential_files_required"] = list(executor.SECRET_LABELS)
        plan["credential_bindings"] = self.production_credential_bindings()
        self.write_plan(plan)
        bundle = self.validate()
        instance = types.SimpleNamespace(
            adapter_id="production-metadata-fixture",
            adapter_contract_revision="production-metadata-fixture/v1",
            production_capable=True,
        )
        runtime = executor.AdapterRuntime(
            module=adapter_contract,
            instance=instance,
            phase_request_type=PhaseRequest,
            phase_result_type=PhaseResult,
            secret_snapshot_type=SecretSnapshot,
        )
        idempotency_key = executor._phase_key(bundle, runtime, "inspect")
        result = {
            "phase": "inspect",
            "idempotency_key": idempotency_key,
            "metrics": {"assets_inspected": bundle.job["pending_objects"]["count"]},
            "evidence_sha256": "a" * 64,
            "observed_live": True,
            "snapshot_revision": None,
            "live_audit_receipt_sha256": None,
            "observed_runtime_images": None,
            "credential_target_identities": {
                label: binding["target_identity"]
                for label, binding in bundle.plan["credential_bindings"].items()
            },
        }
        self.assertEqual(
            executor._validate_phase_result_document(
                copy.deepcopy(result),
                phase="inspect",
                idempotency_key=idempotency_key,
                bundle=bundle,
                adapter=runtime,
            ),
            result,
        )
        result["credential_target_identities"]["caption"] = "provider-project:other"
        self.assert_error(
            "SEMANTIC_EXECUTOR_CREDENTIAL_TARGET_MISMATCH",
            lambda: executor._validate_phase_result_document(
                result,
                phase="inspect",
                idempotency_key=idempotency_key,
                bundle=bundle,
                adapter=runtime,
            ),
        )

    def test_failed_run_rejects_binding_changes_but_allows_secret_rotation(self) -> None:
        plan = self.make_plan()
        plan["profile"] = "production"
        plan["credential_files_required"] = list(executor.SECRET_LABELS)
        plan["credential_bindings"] = self.production_credential_bindings("r1")
        self.write_plan(plan)
        bundle = self.validate()
        runtime = executor.AdapterRuntime(
            module=adapter_contract,
            instance=OfflineFixtureAdapter(),
            phase_request_type=PhaseRequest,
            phase_result_type=PhaseResult,
            secret_snapshot_type=SecretSnapshot,
        )
        state = executor._new_state(bundle, runtime, now=dt.datetime.now(dt.timezone.utc))
        state["phases"]["inspect"]["status"] = "failed"
        state["phases"]["inspect"]["attempts"] = 1
        state["phases"]["inspect"]["last_error_code"] = "SEMANTIC_EXECUTOR_ADAPTER_FAILED"

        secret_files = []
        for index, label in enumerate(executor.SECRET_LABELS):
            path = self.root / f"production-{label}.secret"
            path.write_text(f"secret-r1-{index}\n", encoding="utf-8")
            path.chmod(0o600)
            secret_files.append(path)
        paths = executor.SecretPathArguments(*(str(path) for path in secret_files))
        first_snapshot = executor._load_secret_snapshot(bundle.plan, paths, runtime)
        for index, path in enumerate(secret_files):
            path.write_text(f"secret-r2-{index}\n", encoding="utf-8")
            path.chmod(0o600)
        second_snapshot = executor._load_secret_snapshot(bundle.plan, paths, runtime)
        self.assertNotEqual(first_snapshot.caption, second_snapshot.caption)
        self.assertEqual(executor._validate_state(copy.deepcopy(state), bundle, runtime), state)
        serialized_state = executor.canonical_json(state)
        self.assertNotIn(b"secret-r1", serialized_state)
        self.assertNotIn(b"secret-r2", serialized_state)

        changed_generation = copy.deepcopy(plan)
        changed_generation["credential_bindings"]["caption"]["credential_generation"] = (
            "credential-generation:r2"
        )
        self.write_plan(changed_generation)
        generation_bundle = self.validate()
        self.assert_error(
            "SEMANTIC_EXECUTOR_STATE_MISMATCH",
            lambda: executor._validate_state(copy.deepcopy(state), generation_bundle, runtime),
        )

        changed_target = copy.deepcopy(plan)
        changed_target["credential_bindings"]["postgres"]["target_identity"] = (
            "postgres-deployment:simworld-prod/schema:semantic_assets_v3"
        )
        self.write_plan(changed_target)
        target_bundle = self.validate()
        self.assert_error(
            "SEMANTIC_EXECUTOR_STATE_MISMATCH",
            lambda: executor._validate_state(copy.deepcopy(state), target_bundle, runtime),
        )

    def test_terminal_recovery_rejects_a_different_credential_target(self) -> None:
        plan = self.make_plan()
        plan["profile"] = "production"
        plan["credential_files_required"] = list(executor.SECRET_LABELS)
        plan["credential_bindings"] = self.production_credential_bindings()
        self.write_plan(plan)
        bundle = self.validate()
        runtime = executor.AdapterRuntime(
            module=adapter_contract,
            instance=types.SimpleNamespace(
                adapter_id="production-metadata-fixture",
                adapter_contract_revision="production-metadata-fixture/v1",
                production_capable=True,
            ),
            phase_request_type=PhaseRequest,
            phase_result_type=PhaseResult,
            secret_snapshot_type=SecretSnapshot,
        )
        started = dt.datetime.now(dt.timezone.utc)
        state = executor._new_state(bundle, runtime, now=started)
        count = bundle.job["pending_objects"]["count"]
        views = bundle.plan["limits"]["expected_rendered_views"]
        metrics = {
            "inspect": {"assets_inspected": count},
            "render": {"assets_rendered": count, "rendered_views": views},
            "caption": {"catalog_records": count},
            "embed": {"dense_vectors": count, "sparse_vectors": count},
            "postgres": {"postgres_rows": count},
            "qdrant": {"qdrant_points": count},
            "reconcile": {
                "catalog_records": count,
                "postgres_rows": count,
                "qdrant_points": count,
            },
        }
        targets = {
            label: binding["target_identity"]
            for label, binding in bundle.plan["credential_bindings"].items()
        }
        for phase in executor.PHASES:
            phase_state = state["phases"][phase]
            phase_state["status"] = "completed"
            phase_state["attempts"] = 1
            phase_state["result"] = {
                "phase": phase,
                "idempotency_key": phase_state["idempotency_key"],
                "metrics": metrics[phase],
                "evidence_sha256": hashlib.sha256(phase.encode("ascii")).hexdigest(),
                "observed_live": True,
                "snapshot_revision": (
                    bundle.job["snapshot_target"]["asset_snapshot_revision"]
                    if phase == "reconcile"
                    else None
                ),
                "live_audit_receipt_sha256": "d" * 64 if phase == "reconcile" else None,
                "observed_runtime_images": (
                    copy.deepcopy(bundle.plan["runtime_images"])
                    if phase == "reconcile"
                    else None
                ),
                "credential_target_identities": copy.deepcopy(targets),
            }
        completed = started + dt.timedelta(seconds=1)
        state["updated_at_utc"] = executor._timestamp(completed)
        terminal = executor._terminal_receipt(bundle, runtime, state, completed_at=completed)
        self.assertEqual(
            executor._validate_terminal_receipt(
                copy.deepcopy(terminal),
                bundle,
                runtime,
                state,
            ),
            terminal,
        )

        changed_plan = copy.deepcopy(plan)
        changed_plan["credential_bindings"]["qdrant"]["target_identity"] = (
            "qdrant-cluster:simworld-prod/collection:simworld-assets-v2"
        )
        self.write_plan(changed_plan)
        changed_bundle = self.validate()
        self.assert_error(
            "SEMANTIC_EXECUTOR_TERMINAL_INVALID",
            lambda: executor._validate_terminal_receipt(
                copy.deepcopy(terminal),
                changed_bundle,
                runtime,
                state,
            ),
        )

    def test_forged_job_cannot_exploit_boolean_numbers_or_gate_text(self) -> None:
        cases = []
        boolean_estimate = copy.deepcopy(self.job)
        boolean_estimate["resource_contract"]["estimates"]["embedding_batches_upper_bound"] = True
        cases.append(boolean_estimate)
        forged_gate = copy.deepcopy(self.job)
        forged_gate["live_gates"][0]["required_evidence"] = "operator said yes"
        cases.append(forged_gate)
        for job in cases:
            with self.subTest(mutation=len(cases)):
                basis = {key: value for key, value in job.items() if key != "job_revision"}
                job["job_revision"] = "sha256:" + executor.json_sha256(basis)
                self.assert_error(
                    "SEMANTIC_EXECUTOR_JOB_INVALID",
                    lambda job=job: executor._validate_job(executor.canonical_json(job)),
                )

    def test_job_duplicate_schema_revisions_reject_numeric_aliases(self) -> None:
        locations = {
            "snapshot": lambda job, alias: job["snapshot_target"]["postgres"].__setitem__(
                "schema_revision", alias
            ),
            "recipe": lambda job, alias: job["recipe_contract"]["storage"].__setitem__(
                "postgres_schema_revision", alias
            ),
        }
        for location, mutate in locations.items():
            for alias in (True, 2.0):
                with self.subTest(location=location, alias=alias):
                    job = copy.deepcopy(self.job)
                    mutate(job, alias)
                    basis = {
                        key: value for key, value in job.items() if key != "job_revision"
                    }
                    job["job_revision"] = "sha256:" + executor.json_sha256(basis)
                    self.assert_error(
                        "SEMANTIC_EXECUTOR_JOB_INVALID",
                        lambda job=job: executor._validate_job(executor.canonical_json(job)),
                    )

    def test_every_sealed_input_requires_an_independent_raw_digest(self) -> None:
        cases = {
            "expected_job_sha256": "0" * 64,
            "expected_preparation_receipt_sha256": "1" * 64,
            "expected_execution_plan_sha256": "2" * 64,
        }
        for key, value in cases.items():
            with self.subTest(key=key):
                pins = self.pins()
                pins[key] = value
                self.assert_error(
                    "SEMANTIC_EXECUTOR_EXTERNAL_PIN_MISMATCH",
                    lambda pins=pins: executor.validate_bundle(
                        job_path=self.job_path,
                        preparation_receipt_path=self.receipt_path,
                        execution_plan_path=self.plan_path,
                        repo_root=REPO_ROOT,
                        **pins,
                    ),
                )

    def test_job_input_model_image_schema_git_and_count_drift_fail_closed(self) -> None:
        mutations = [
            (lambda plan: plan["job"].__setitem__("project_revision", "source-patch:" + "0" * 40), "SEMANTIC_EXECUTOR_PLAN_PIN_MISMATCH"),
            (lambda plan: plan["caption"].__setitem__("output_schema_revision", "sha256:" + "a" * 64), "SEMANTIC_EXECUTOR_MODEL_PIN_MISMATCH"),
            (lambda plan: plan["embedding"].__setitem__("dense_model_revision", "sha256:" + "b" * 64), "SEMANTIC_EXECUTOR_MODEL_PIN_MISMATCH"),
            (lambda plan: plan["runtime_images"].__setitem__("postgres", "postgres:latest"), "SEMANTIC_EXECUTOR_IMAGE_PIN_INVALID"),
            (lambda plan: plan["runtime_images"].__setitem__("postgres", "r" * 500 + "@sha256:" + "7" * 64), "SEMANTIC_EXECUTOR_IMAGE_PIN_INVALID"),
            (lambda plan: plan["limits"].__setitem__("expected_postgres_rows", 999), "SEMANTIC_EXECUTOR_ROW_LIMIT_MISMATCH"),
            (lambda plan: plan["executor"].__setitem__("git_commit", "c" * 40), "SEMANTIC_EXECUTOR_GIT_PIN_MISMATCH"),
            (lambda plan: plan["executor"].__setitem__("job_schema_sha256", "d" * 64), "SEMANTIC_EXECUTOR_SOURCE_PIN_MISMATCH"),
            (lambda plan: plan["executor"].__setitem__("bootstrap_source_sha256", "e" * 64), "SEMANTIC_EXECUTOR_SOURCE_PIN_MISMATCH"),
        ]
        for mutate, code in mutations:
            with self.subTest(code=code):
                plan = self.make_plan()
                mutate(plan)
                self.write_plan(plan)
                self.assert_error(code, self.validate)

    def test_plan_scalar_contract_rejects_boolean_and_float_aliases(self) -> None:
        mutations = {
            "job_count_bool": lambda plan: plan["job"].__setitem__("pending_object_count", True),
            "job_count_float": lambda plan: plan["job"].__setitem__(
                "pending_object_count", float(plan["job"]["pending_object_count"])
            ),
            "adapter_capability_int": lambda plan: plan["adapter"].__setitem__(
                "production_capable", 0
            ),
            "caption_temperature_bool": lambda plan: plan["caption"].__setitem__(
                "temperature_milli", False
            ),
            "embedding_dimension_float": lambda plan: plan["embedding"].__setitem__(
                "dense_size", float(plan["embedding"]["dense_size"])
            ),
            "storage_revision_bool": lambda plan: plan["storage"].__setitem__(
                "postgres_schema_revision", True
            ),
            "storage_revision_float": lambda plan: plan["storage"].__setitem__(
                "postgres_schema_revision", 2.0
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                plan = self.make_plan()
                mutate(plan)
                self.write_plan(plan)
                self.assert_error("SEMANTIC_EXECUTOR_PLAN_INVALID", self.validate)

    def test_preparation_receipt_must_still_describe_an_unstarted_pending_job(self) -> None:
        receipt = dict(self.receipt)
        receipt["execution_started"] = True
        raw = executor.canonical_json(receipt)
        self.receipt_path.write_bytes(raw)
        self.receipt_path.chmod(0o600)
        plan = self.make_plan()
        plan["job"]["preparation_receipt_sha256"] = hashlib.sha256(raw).hexdigest()
        self.write_plan(plan)
        self.assert_error("SEMANTIC_EXECUTOR_RECEIPT_INVALID", self.validate)

    def test_duplicate_keys_noncanonical_json_and_unsafe_files_are_rejected(self) -> None:
        original = self.plan_path.read_bytes()
        self.plan_path.write_bytes(original.replace(b'{\n  "adapter"', b'{\n  "schema": "duplicate",\n  "adapter"', 1))
        self.plan_path.chmod(0o600)
        self.assert_error("SEMANTIC_EXECUTOR_JSON_INVALID", self.validate)

        self.write_plan(self.make_plan())
        self.plan_path.write_bytes(self.plan_path.read_bytes() + b"\n")
        self.plan_path.chmod(0o600)
        self.assert_error("SEMANTIC_EXECUTOR_PLAN_INVALID", self.validate)

        self.write_plan(self.make_plan())
        self.plan_path.chmod(0o644)
        self.assert_error("SEMANTIC_EXECUTOR_INPUT_UNSAFE", self.validate)

    def test_sealed_bundle_pair_rejects_symlinks_hardlinks_and_split_directories(self) -> None:
        link_dir = self.root / "bundle-link"
        link_dir.symlink_to(self.fixture.output_dir, target_is_directory=True)
        self.assert_error(
            "SEMANTIC_EXECUTOR_PATH_UNSAFE",
            lambda: executor.validate_bundle(
                job_path=link_dir / "semantic-index-job.json",
                preparation_receipt_path=link_dir / "preparation-receipt.json",
                execution_plan_path=self.plan_path,
                repo_root=REPO_ROOT,
                **self.pins(),
            ),
        )

        hardlink = self.root / "job-hardlink.json"
        os.link(self.job_path, hardlink)
        self.assert_error("SEMANTIC_EXECUTOR_INPUT_UNSAFE", self.validate)
        hardlink.unlink()

        split_dir = self.root / "split"
        split_dir.mkdir(mode=0o700)
        split_receipt = split_dir / "preparation-receipt.json"
        split_receipt.write_bytes(self.receipt_path.read_bytes())
        split_receipt.chmod(0o600)
        self.assert_error(
            "SEMANTIC_EXECUTOR_BUNDLE_INVALID",
            lambda: executor.validate_bundle(
                job_path=self.job_path,
                preparation_receipt_path=split_receipt,
                execution_plan_path=self.plan_path,
                repo_root=REPO_ROOT,
                **self.pins(),
            ),
        )

    def test_fixture_rejects_all_secret_files_and_receipts_never_contain_paths_or_values(self) -> None:
        secret = self.root / "postgres.secret"
        secret_value = "postgresql://secret-user:secret-password@localhost/db"
        secret.write_text(secret_value + "\n", encoding="utf-8")
        secret.chmod(0o600)
        bundle = self.validate()
        paths = executor.SecretPathArguments(None, str(secret), None, None)
        self.assert_error(
            "SEMANTIC_EXECUTOR_FIXTURE_SECRET_PROHIBITED",
            lambda: executor.execute_bundle(
                bundle=bundle,
                requested_adapter_id=OfflineFixtureAdapter.adapter_id,
                run_dir=self.run_dir,
                approval_ref=APPROVAL,
                secret_paths=paths,
            ),
        )
        self.assertFalse(self.run_dir.exists())

        terminal = executor.execute_bundle(
            bundle=bundle,
            requested_adapter_id=OfflineFixtureAdapter.adapter_id,
            run_dir=self.run_dir,
            approval_ref=APPROVAL,
            secret_paths=NO_SECRET_PATHS,
        )
        evidence = (self.run_dir / "execution-state.json").read_bytes() + (self.run_dir / "terminal-receipt.json").read_bytes() + executor.canonical_json(terminal)
        self.assertNotIn(str(secret).encode(), evidence)
        self.assertNotIn(secret_value.encode(), evidence)

    def test_wrong_phase_counts_fail_before_later_phases(self) -> None:
        bundle = self.validate()
        self.assert_error(
            "SEMANTIC_EXECUTOR_RECONCILIATION_FAILED",
            lambda: self.execute_bundle(
                bundle=bundle,
                adapter=WrongCountAdapter(),
                requested_adapter_id=OfflineFixtureAdapter.adapter_id,
                run_dir=self.run_dir,
                approval_ref=APPROVAL,
                secret_files=NO_SECRETS,
            ),
        )
        state = json.loads((self.run_dir / "execution-state.json").read_text())
        self.assertEqual(state["phases"]["caption"]["status"], "failed")
        self.assertEqual(state["phases"]["embed"]["status"], "pending")

    def test_adapter_timeout_is_bounded_and_public_error_is_secret_free(self) -> None:
        bundle = self.validate()
        error = self.assert_error(
            "SEMANTIC_EXECUTOR_PHASE_TIMEOUT",
            lambda: self.execute_bundle(
                bundle=bundle,
                adapter=TimeoutAdapter(),
                requested_adapter_id=OfflineFixtureAdapter.adapter_id,
                run_dir=self.run_dir,
                approval_ref=APPROVAL,
                secret_files=NO_SECRETS,
            ),
        )
        self.assertNotIn("secret-bearing", json.dumps(error.public_dict()))
        state = json.loads((self.run_dir / "execution-state.json").read_text())
        self.assertEqual(state["phases"]["inspect"]["status"], "failed")
        self.assertEqual(state["phases"]["render"]["status"], "pending")

    def test_executor_alarm_interrupts_a_slow_adapter(self) -> None:
        plan = self.make_plan()
        plan["limits"]["phase_timeout_seconds"] = 1
        self.write_plan(plan)
        bundle = self.validate()
        started = time.monotonic()
        self.assert_error(
            "SEMANTIC_EXECUTOR_PHASE_TIMEOUT",
            lambda: self.execute_bundle(
                bundle=bundle,
                adapter=SleepingAdapter(),
                requested_adapter_id=OfflineFixtureAdapter.adapter_id,
                run_dir=self.run_dir,
                approval_ref=APPROVAL,
                secret_files=NO_SECRETS,
            ),
        )
        self.assertLess(time.monotonic() - started, 1.8)

    def test_total_deadline_uses_a_current_process_monotonic_anchor(self) -> None:
        plan = self.make_plan()
        plan["limits"]["max_total_elapsed_seconds"] = 1
        self.write_plan(plan)
        bundle = self.validate()
        adapter = CountingAdapter()
        with mock.patch.object(
            executor.time,
            "monotonic_ns",
            side_effect=[1_000_000_000, 3_000_000_000],
        ):
            self.assert_error(
                "SEMANTIC_EXECUTOR_TOTAL_TIMEOUT",
                lambda: self.execute_bundle(
                    bundle=bundle,
                    adapter=adapter,
                    requested_adapter_id=adapter.adapter_id,
                    run_dir=self.run_dir,
                    approval_ref=APPROVAL,
                    secret_files=NO_SECRETS,
                ),
            )
        self.assertEqual(adapter.calls, [])
        state = json.loads((self.run_dir / "execution-state.json").read_text())
        self.assertEqual(state["phases"]["inspect"]["status"], "pending")

    def test_resume_rejects_expired_state_and_clock_before_durable_state(self) -> None:
        bundle = self.validate()
        runtime = self.production_runtime(ProductionFixtureAdapter())
        current = dt.datetime(2026, 7, 21, 12, 0, tzinfo=dt.timezone.utc)

        expired_start = current - dt.timedelta(
            seconds=bundle.plan["limits"]["max_total_elapsed_seconds"] + 1
        )
        expired_state = executor._new_state(bundle, runtime, now=expired_start)
        self.run_dir.mkdir(mode=0o700)
        state_path = self.run_dir / "execution-state.json"
        state_path.write_bytes(executor.canonical_json(expired_state))
        state_path.chmod(0o600)
        self.assert_error(
            "SEMANTIC_EXECUTOR_TOTAL_TIMEOUT",
            lambda: executor._execute_with_adapter(
                bundle=bundle,
                adapter=runtime,
                run_dir=self.run_dir,
                approval_ref=APPROVAL,
                secret_snapshot=NO_SECRETS,
                now=lambda: current,
            ),
        )

        state_path.unlink()
        future_state = executor._new_state(
            bundle,
            runtime,
            now=current + dt.timedelta(seconds=1),
        )
        state_path.write_bytes(executor.canonical_json(future_state))
        state_path.chmod(0o600)
        self.assert_error(
            "SEMANTIC_EXECUTOR_CLOCK_INVALID",
            lambda: executor._execute_with_adapter(
                bundle=bundle,
                adapter=runtime,
                run_dir=self.run_dir,
                approval_ref=APPROVAL,
                secret_snapshot=NO_SECRETS,
                now=lambda: current,
            ),
        )

    def test_every_wall_clock_sample_is_aware_and_nondecreasing(self) -> None:
        bundle = self.validate()
        base = dt.datetime(2026, 7, 21, 12, 0, tzinfo=dt.timezone.utc)
        cases = {
            "regression": [
                base,
                base + dt.timedelta(seconds=1),
                base,
                base + dt.timedelta(seconds=2),
            ],
            "naive": [
                base,
                base + dt.timedelta(seconds=1),
                base.replace(tzinfo=None),
                base + dt.timedelta(seconds=2),
            ],
        }
        for name, samples in cases.items():
            with self.subTest(name=name):
                run_dir = self.run_parent / f"clock-{name}"
                iterator = iter(samples)
                adapter = CountingAdapter()
                self.assert_error(
                    "SEMANTIC_EXECUTOR_CLOCK_INVALID",
                    lambda: self.execute_bundle(
                        bundle=bundle,
                        adapter=adapter,
                        requested_adapter_id=adapter.adapter_id,
                        run_dir=run_dir,
                        approval_ref=APPROVAL,
                        secret_files=NO_SECRETS,
                        now=lambda: next(iterator),
                    ),
                )
                state = json.loads((run_dir / "execution-state.json").read_text())
                self.assertEqual(state["phases"]["inspect"]["status"], "failed")
                self.assertEqual(
                    state["phases"]["inspect"]["last_error_code"],
                    "SEMANTIC_EXECUTOR_CLOCK_INVALID",
                )

    def test_adapter_receives_disposable_copies_of_validated_job_and_plan(self) -> None:
        bundle = self.validate()
        original_job = executor.canonical_json(bundle.job)
        original_plan = executor.canonical_json(bundle.plan)
        terminal = self.execute_bundle(
            bundle=bundle,
            adapter=MutatingAdapter(),
            requested_adapter_id=OfflineFixtureAdapter.adapter_id,
            run_dir=self.run_dir,
            approval_ref=APPROVAL,
            secret_files=NO_SECRETS,
        )
        self.assertEqual(executor.canonical_json(bundle.job), original_job)
        self.assertEqual(executor.canonical_json(bundle.plan), original_plan)
        self.assertEqual(terminal["status"], "offline_fixture_complete")

    def test_resume_rejects_out_of_order_or_extended_state(self) -> None:
        bundle = self.validate()
        adapter = FlakyAdapter("caption")
        self.assert_error(
            "SEMANTIC_EXECUTOR_ADAPTER_FAILED",
            lambda: self.execute_bundle(
                bundle=bundle,
                adapter=adapter,
                requested_adapter_id=adapter.adapter_id,
                run_dir=self.run_dir,
                approval_ref=APPROVAL,
                secret_files=NO_SECRETS,
            ),
        )
        state_path = self.run_dir / "execution-state.json"
        state = json.loads(state_path.read_text())
        state["phases"]["embed"]["status"] = "running"
        state["phases"]["embed"]["attempts"] = 1
        state_path.write_bytes(executor.canonical_json(state))
        state_path.chmod(0o600)
        self.assert_error(
            "SEMANTIC_EXECUTOR_STATE_INVALID",
            lambda: self.execute_bundle(
                bundle=bundle,
                adapter=adapter,
                requested_adapter_id=adapter.adapter_id,
                run_dir=self.run_dir,
                approval_ref=APPROVAL,
                secret_files=NO_SECRETS,
            ),
        )

        state["phases"]["embed"]["status"] = "pending"
        state["phases"]["embed"]["attempts"] = 0
        state["deadline_at_utc"] = "2099-01-01T00:00:00+00:00"
        state_path.write_bytes(executor.canonical_json(state))
        state_path.chmod(0o600)
        self.assert_error(
            "SEMANTIC_EXECUTOR_STATE_INVALID",
            lambda: self.execute_bundle(
                bundle=bundle,
                adapter=adapter,
                requested_adapter_id=adapter.adapter_id,
                run_dir=self.run_dir,
                approval_ref=APPROVAL,
                secret_files=NO_SECRETS,
            ),
        )

    def test_public_executor_cannot_receive_or_monkeypatch_an_adapter_instance(self) -> None:
        bundle = self.validate()
        self.assertNotIn("adapter", inspect.signature(executor.execute_bundle).parameters)
        fixture = OfflineFixtureAdapter()
        with self.assertRaises(AttributeError):
            fixture.run_phase = lambda _request: None
        with self.assertRaises(TypeError):
            executor.execute_bundle(
                bundle=bundle,
                adapter=fixture,
                requested_adapter_id=OfflineFixtureAdapter.adapter_id,
                run_dir=self.run_dir,
                approval_ref=APPROVAL,
                secret_paths=NO_SECRET_PATHS,
            )
        self.assertFalse(self.run_dir.exists())

    def test_adapter_load_requires_the_registered_bundle_and_rehashes_source_bytes(self) -> None:
        bundle = self.validate()
        copied = dataclasses.replace(bundle)
        self.assert_error(
            "SEMANTIC_EXECUTOR_BUNDLE_NOT_VALIDATED",
            lambda: executor._load_adapter_runtime(
                copied,
                requested_adapter_id=executor.OFFLINE_ADAPTER_ID,
            ),
        )

        object.__setattr__(bundle, "adapter_source_bytes", b"raise RuntimeError('changed')\n")
        self.assert_error(
            "SEMANTIC_EXECUTOR_SOURCE_PIN_MISMATCH",
            lambda: executor._load_adapter_runtime(
                bundle,
                requested_adapter_id=executor.OFFLINE_ADAPTER_ID,
            ),
        )
        self.assertFalse(self.run_dir.exists())

    def test_nonisolated_python_rejects_execution_before_adapter_load(self) -> None:
        self.assertEqual(sys.flags.isolated, 0)
        self.assert_error("SEMANTIC_EXECUTOR_PYTHON_NOT_ISOLATED", REAL_REQUIRE_ISOLATED_PYTHON)
        bundle = self.validate()
        with mock.patch.object(
            executor,
            "_require_isolated_python",
            side_effect=REAL_REQUIRE_ISOLATED_PYTHON,
        ), mock.patch.object(executor, "_load_adapter_runtime") as loader:
            self.assert_error(
                "SEMANTIC_EXECUTOR_PYTHON_NOT_ISOLATED",
                lambda: executor.execute_bundle(
                    bundle=bundle,
                    requested_adapter_id=executor.OFFLINE_ADAPTER_ID,
                    run_dir=self.run_dir,
                    approval_ref=APPROVAL,
                    secret_paths=NO_SECRET_PATHS,
                ),
            )
            loader.assert_not_called()
        self.assertFalse(self.run_dir.exists())

    def test_isolation_gate_supports_system_python_310_and_rejects_site_or_venv(self) -> None:
        python310_flags = types.SimpleNamespace(
            isolated=1,
            ignore_environment=1,
            no_user_site=1,
            no_site=1,
        )
        self.assertTrue(executor._isolated_python_runtime_supported(
            version=(3, 10),
            flags=python310_flags,
            executable=executor.SYSTEM_PYTHON,
            prefix="/usr",
            base_prefix="/usr",
            loaded_modules={},
        ))
        python312_flags = types.SimpleNamespace(
            isolated=1,
            ignore_environment=1,
            no_user_site=1,
            no_site=1,
            safe_path=True,
        )
        self.assertTrue(executor._isolated_python_runtime_supported(
            version=(3, 12),
            flags=python312_flags,
            executable=executor.SYSTEM_PYTHON,
            prefix="/usr",
            base_prefix="/usr",
            loaded_modules={},
        ))
        for mutation in (
            {"flags": types.SimpleNamespace(**{**vars(python312_flags), "no_site": 0})},
            {"prefix": "/tmp/venv"},
            {"loaded_modules": {"site": object()}},
            {"executable": "/tmp/venv/bin/python"},
            {"version": (3, 13)},
        ):
            arguments = {
                "version": (3, 12),
                "flags": python312_flags,
                "executable": executor.SYSTEM_PYTHON,
                "prefix": "/usr",
                "base_prefix": "/usr",
                "loaded_modules": {},
                **mutation,
            }
            with self.subTest(mutation=mutation):
                self.assertFalse(executor._isolated_python_runtime_supported(**arguments))

    def test_exact_system_python_isolated_no_site_command_passes_execution_gate(self) -> None:
        source = TOOLS_DIR / "execute_semantic_asset_index_job.py"
        program = (
            "import runpy; "
            f"namespace=runpy.run_path({str(source)!r}); "
            "namespace['_require_isolated_python'](); "
            "print('isolated-ok')"
        )
        completed = subprocess.run(
            [executor.SYSTEM_PYTHON, "-I", "-S", "-c", program],
            cwd="/",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "LANG": "C", "LC_ALL": "C"},
            timeout=10,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", "replace"))
        self.assertEqual(completed.stdout, b"isolated-ok\n")

    def test_no_repo_local_dependency_import_occurs_before_attestation(self) -> None:
        trap = self.root / "pythonpath-trap"
        trap.mkdir(mode=0o700)
        marker = self.root / "imported-local-module"
        payload = f"from pathlib import Path\nPath({str(marker)!r}).write_text('imported')\n"
        (trap / "prepare_semantic_asset_index_job.py").write_text(payload, encoding="utf-8")
        (trap / "semantic_index_job_adapters.py").write_text(payload, encoding="utf-8")
        environment = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(self.root),
            "PYTHONPATH": os.pathsep.join((str(trap), str(TOOLS_DIR))),
        }
        completed = subprocess.run(
            [sys.executable, "-c", "import execute_semantic_asset_index_job"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            timeout=10,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", "replace"))
        self.assertFalse(marker.exists())

        bundle = self.validate()
        with mock.patch.object(
            executor,
            "_verify_git_source_closure",
            side_effect=executor.SemanticIndexExecutorError(
                "SEMANTIC_EXECUTOR_SOURCE_PIN_MISMATCH",
                "synthetic attestation failure",
            ),
        ), mock.patch.object(executor, "_load_adapter_runtime") as loader:
            self.assert_error(
                "SEMANTIC_EXECUTOR_SOURCE_PIN_MISMATCH",
                lambda: executor.validate_bundle(
                    job_path=self.job_path,
                    preparation_receipt_path=self.receipt_path,
                    execution_plan_path=self.plan_path,
                    repo_root=REPO_ROOT,
                    **self.pins(),
                ),
            )
            loader.assert_not_called()
        self.assertEqual(bundle.plan["adapter"]["adapter_id"], executor.OFFLINE_ADAPTER_ID)

    def test_real_git_attestation_rejects_dirty_transitive_source(self) -> None:
        repository = self.root / "attested-repository"
        repository.mkdir(mode=0o700)
        for relative_path in executor.SOURCE_CLOSURE:
            destination = repository / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((REPO_ROOT / relative_path).read_bytes())
        commands = (
            ["init", "--quiet"],
            ["config", "user.name", "Semantic Executor Test"],
            ["config", "user.email", "semantic-executor@example.invalid"],
            ["add", "--", *executor.SOURCE_CLOSURE],
            ["commit", "--quiet", "-m", "attested source closure"],
        )
        for arguments in commands:
            subprocess.run(
                ["/usr/bin/git", *arguments],
                cwd=repository,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=True,
            )
        commit = subprocess.run(
            ["/usr/bin/git", "rev-parse", "--verify", "HEAD^{commit}"],
            cwd=repository,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=True,
        ).stdout.decode("ascii").strip()
        pins = {
            "git_commit": commit,
            "git_object_format": "sha1",
            **{
                pin_key: hashlib.sha256((repository / relative_path).read_bytes()).hexdigest()
                for pin_key, relative_path in executor.SOURCE_PIN_PATHS.items()
            },
        }
        entrypoint = repository / executor.SOURCE_PIN_PATHS["executor_source_sha256"]
        with mock.patch.object(executor, "__file__", str(entrypoint)):
            observed_commit, sources = REAL_VERIFY_GIT_SOURCE_CLOSURE(repository, pins)
            self.assertEqual(observed_commit, commit)
            self.assertEqual(set(sources), set(executor.SOURCE_CLOSURE))
            bootstrap = repository / executor.SOURCE_PIN_PATHS["bootstrap_source_sha256"]
            bootstrap.write_bytes(bootstrap.read_bytes() + b"\n")
            self.assert_error(
                "SEMANTIC_EXECUTOR_SOURCE_PIN_MISMATCH",
                lambda: REAL_VERIFY_GIT_SOURCE_CLOSURE(repository, pins),
            )
            subprocess.run(
                ["/usr/bin/git", "config", "extensions.partialClone", "origin"],
                cwd=repository,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=True,
            )
            self.assert_error(
                "SEMANTIC_EXECUTOR_GIT_PARTIAL_CLONE_UNSUPPORTED",
                lambda: REAL_VERIFY_GIT_SOURCE_CLOSURE(repository, pins),
            )

    def test_fixed_git_timeout_kills_group_and_reaps_its_direct_child(self) -> None:
        real_popen = subprocess.Popen
        child_pid_file = self.root / "hanging-helper.pid"
        launched = []
        helper = (
            "import pathlib,subprocess,time; "
            "child=subprocess.Popen(['/usr/bin/python3','-I','-S','-c','import time; time.sleep(60)']); "
            f"pathlib.Path({str(child_pid_file)!r}).write_text(str(child.pid)); "
            "time.sleep(60)"
        )

        def launch_helper(_command, **kwargs):
            self.assertTrue(kwargs.get("start_new_session"))
            process = real_popen(
                ["/usr/bin/python3", "-I", "-S", "-c", helper],
                **kwargs,
            )
            launched.append(process)
            return process

        with mock.patch.object(executor.subprocess, "Popen", side_effect=launch_helper):
            self.assert_error(
                "SEMANTIC_EXECUTOR_GIT_INVALID",
                lambda: executor._run_fixed_git(
                    REPO_ROOT,
                    ["rev-parse", "--verify", "HEAD^{commit}"],
                    maximum_stdout=128,
                    timeout_seconds=0.5,
                ),
            )
        self.assertEqual(len(launched), 1)
        self.assertEqual(launched[0].returncode, -signal.SIGKILL)
        self.assertTrue(child_pid_file.is_file())
        helper_pid = int(child_pid_file.read_text(encoding="utf-8"))
        state_path = pathlib.Path(f"/proc/{helper_pid}/stat")
        if state_path.exists():
            state = state_path.read_text(encoding="utf-8").split()[2]
            self.assertEqual(state, "Z")

    def test_fixed_git_kills_descendant_after_direct_leader_exits(self) -> None:
        real_popen = subprocess.Popen
        child_pid_file = self.root / "orphaned-pipe-holder.pid"
        launched = []
        helper = (
            "import pathlib,subprocess; "
            "child=subprocess.Popen(['/usr/bin/python3','-I','-S','-c','import time; time.sleep(60)']); "
            f"pathlib.Path({str(child_pid_file)!r}).write_text(str(child.pid))"
        )

        def launch_helper(_command, **kwargs):
            self.assertTrue(kwargs.get("start_new_session"))
            process = real_popen(
                ["/usr/bin/python3", "-I", "-S", "-c", helper],
                **kwargs,
            )
            launched.append(process)
            return process

        started = time.monotonic()
        with (
            mock.patch.object(executor.subprocess, "Popen", side_effect=launch_helper),
            mock.patch.object(executor.os, "killpg", wraps=os.killpg) as killpg,
        ):
            self.assert_error(
                "SEMANTIC_EXECUTOR_GIT_INVALID",
                lambda: executor._run_fixed_git(
                    REPO_ROOT,
                    ["rev-parse", "--verify", "HEAD^{commit}"],
                    maximum_stdout=128,
                    timeout_seconds=0.5,
                ),
            )
        elapsed = time.monotonic() - started
        self.assertEqual(len(launched), 1)
        leader = launched[0]
        self.assertEqual(leader.returncode, 0)
        killpg.assert_any_call(leader.pid, signal.SIGKILL)
        self.assertLess(elapsed, 0.5 + executor.GIT_CLEANUP_TIMEOUT_SECONDS + 1.0)
        self.assertTrue(child_pid_file.is_file())
        child_pid = int(child_pid_file.read_text(encoding="utf-8"))
        state_path = pathlib.Path(f"/proc/{child_pid}/stat")
        deadline = time.monotonic() + executor.GIT_CLEANUP_TIMEOUT_SECONDS
        state = None
        while time.monotonic() < deadline:
            if not state_path.exists():
                state = None
                break
            state = state_path.read_text(encoding="utf-8").split()[2]
            if state in {"Z", "X"}:
                break
            time.sleep(0.01)
        try:
            self.assertTrue(state is None or state in {"Z", "X"})
        finally:
            if state_path.exists():
                current_state = state_path.read_text(encoding="utf-8").split()[2]
                if current_state not in {"Z", "X"}:
                    os.kill(child_pid, signal.SIGKILL)

    def test_fixed_git_cleanup_never_uses_an_unbounded_wait(self) -> None:
        class StubbornProcess:
            pid = 424242

            def __init__(self) -> None:
                self.wait_timeouts: list[float] = []
                self.kill_calls = 0

            def poll(self):
                return None

            def wait(self, *, timeout):
                self.wait_timeouts.append(timeout)
                raise subprocess.TimeoutExpired(["git"], timeout)

            def kill(self):
                self.kill_calls += 1

        process = StubbornProcess()
        with (
            mock.patch.object(executor.os, "killpg"),
            mock.patch.object(
                executor.time,
                "monotonic",
                side_effect=[10.0, 10.25, 11.5],
            ),
        ):
            self.assert_error(
                "SEMANTIC_EXECUTOR_GIT_INVALID",
                lambda: executor._terminate_process_group(process),
            )
        self.assertEqual(len(process.wait_timeouts), 2)
        self.assertTrue(
            all(0 < timeout <= executor.GIT_CLEANUP_TIMEOUT_SECONDS for timeout in process.wait_timeouts)
        )
        self.assertLess(process.wait_timeouts[1], process.wait_timeouts[0])
        self.assertEqual(process.kill_calls, 1)

    def test_secret_snapshot_survives_path_replacement_without_retaining_paths(self) -> None:
        paths = []
        values = {}
        for index, label in enumerate(executor.SECRET_LABELS):
            path = self.root / f"{label}.secret"
            value = f"credential-{index}-before".encode("utf-8")
            path.write_bytes(value + b"\n")
            path.chmod(0o600)
            paths.append(str(path))
            values[label] = value
        runtime = executor.AdapterRuntime(
            module=adapter_contract,
            instance=OfflineFixtureAdapter(),
            phase_request_type=PhaseRequest,
            phase_result_type=PhaseResult,
            secret_snapshot_type=SecretSnapshot,
        )
        snapshot = executor._load_secret_snapshot(
            {
                "profile": "production",
                "credential_files_required": list(executor.SECRET_LABELS),
            },
            executor.SecretPathArguments(*paths),
            runtime,
        )
        replacement = self.root / "caption-replacement.secret"
        replacement.write_text("credential-after\n", encoding="utf-8")
        replacement.chmod(0o600)
        os.replace(replacement, paths[0])
        self.assertEqual(snapshot.caption, values["caption"])
        self.assertNotEqual(snapshot.caption, pathlib.Path(paths[0]).read_bytes().rstrip())
        representation = repr(snapshot)
        self.assertNotIn("credential", representation)
        self.assertNotIn(str(self.root), representation)
        self.assertIn("secret_snapshot", PhaseRequest.__dataclass_fields__)
        self.assertNotIn("secret_files", PhaseRequest.__dataclass_fields__)

    def test_locked_run_with_temp_residue_reports_busy_before_inspection(self) -> None:
        bundle = self.validate()
        self.run_dir.mkdir(mode=0o700)
        temporary = self.run_dir / ".execution-state.json.tmp-held"
        temporary.write_bytes(b"partial")
        temporary.chmod(0o600)
        lock_path = self.run_dir / "executor.lock"
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assert_error(
                "SEMANTIC_EXECUTOR_RUN_BUSY",
                lambda: executor.execute_bundle(
                    bundle=bundle,
                    requested_adapter_id=executor.OFFLINE_ADAPTER_ID,
                    run_dir=self.run_dir,
                    approval_ref=APPROVAL,
                    secret_paths=NO_SECRET_PATHS,
                ),
            )
        finally:
            os.close(descriptor)

    def test_orphan_temp_is_quarantined_and_resume_completes(self) -> None:
        bundle = self.validate()
        self.run_dir.mkdir(mode=0o700)
        temporary = self.run_dir / ".execution-state.json.tmp-orphan"
        temporary.write_bytes(b"interrupted-write")
        temporary.chmod(0o600)
        terminal = executor.execute_bundle(
            bundle=bundle,
            requested_adapter_id=executor.OFFLINE_ADAPTER_ID,
            run_dir=self.run_dir,
            approval_ref=APPROVAL,
            secret_paths=NO_SECRET_PATHS,
        )
        self.assertEqual(terminal["status"], "offline_fixture_complete")
        self.assertFalse(temporary.exists())
        quarantine = self.run_dir / ".recovery-quarantine"
        recovered = list(quarantine.iterdir())
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].read_bytes(), b"interrupted-write")
        self.assertEqual(stat.S_IMODE(recovered[0].stat().st_mode), 0o600)

    def test_legacy_linked_temp_is_repaired_without_leaving_two_links(self) -> None:
        bundle = self.validate()
        terminal = executor.execute_bundle(
            bundle=bundle,
            requested_adapter_id=executor.OFFLINE_ADAPTER_ID,
            run_dir=self.run_dir,
            approval_ref=APPROVAL,
            secret_paths=NO_SECRET_PATHS,
        )
        terminal_path = self.run_dir / "terminal-receipt.json"
        temporary = self.run_dir / ".terminal-receipt.json.tmp-legacy"
        os.link(terminal_path, temporary)
        self.assertEqual(terminal_path.stat().st_nlink, 2)
        resumed = executor.execute_bundle(
            bundle=bundle,
            requested_adapter_id=executor.OFFLINE_ADAPTER_ID,
            run_dir=self.run_dir,
            approval_ref=APPROVAL,
            secret_paths=NO_SECRET_PATHS,
        )
        self.assertEqual(resumed, terminal)
        self.assertFalse(temporary.exists())
        self.assertEqual(terminal_path.stat().st_nlink, 1)

    def test_unsafe_recognized_temp_residue_fails_closed(self) -> None:
        bundle = self.validate()
        self.run_dir.mkdir(mode=0o700)
        temporary = self.run_dir / ".execution-state.json.tmp-unsafe"
        temporary.write_bytes(b"unsafe")
        temporary.chmod(0o644)
        self.assert_error(
            "SEMANTIC_EXECUTOR_RUN_DIR_INVALID",
            lambda: executor.execute_bundle(
                bundle=bundle,
                requested_adapter_id=executor.OFFLINE_ADAPTER_ID,
                run_dir=self.run_dir,
                approval_ref=APPROVAL,
                secret_paths=NO_SECRET_PATHS,
            ),
        )

    def test_terminal_receipt_is_recovery_authority_after_state_update_crash(self) -> None:
        bundle = self.validate()
        adapter = CountingAdapter()
        real_write = executor._atomic_write_at
        terminal_written = False
        clock_value = dt.datetime.now(dt.timezone.utc)

        def advancing_clock():
            nonlocal clock_value
            clock_value += dt.timedelta(microseconds=1)
            return clock_value

        def crash_after_terminal(handle, name, raw, *, replace):
            nonlocal terminal_written
            if name == "terminal-receipt.json":
                real_write(handle, name, raw, replace=replace)
                terminal_written = True
                return
            if terminal_written and name == "execution-state.json":
                raise OSError("synthetic crash")
            real_write(handle, name, raw, replace=replace)

        with mock.patch.object(executor, "_atomic_write_at", side_effect=crash_after_terminal):
            with self.assertRaises(OSError):
                self.execute_bundle(
                    bundle=bundle,
                    adapter=adapter,
                    requested_adapter_id=adapter.adapter_id,
                    run_dir=self.run_dir,
                    approval_ref=APPROVAL,
                    secret_files=NO_SECRETS,
                    now=advancing_clock,
                )
        self.assertTrue((self.run_dir / "terminal-receipt.json").exists())
        state_before_repair = json.loads(
            (self.run_dir / "execution-state.json").read_text()
        )
        terminal_before_repair = json.loads(
            (self.run_dir / "terminal-receipt.json").read_text()
        )
        self.assertIsNone(state_before_repair["terminal_receipt_sha256"])
        state_updated = executor._rfc3339(state_before_repair["updated_at_utc"])
        terminal_completed = executor._rfc3339(
            terminal_before_repair["completed_at_utc"]
        )
        self.assertLess(state_updated, terminal_completed)
        self.assert_error(
            "SEMANTIC_EXECUTOR_CLOCK_INVALID",
            lambda: self.execute_bundle(
                bundle=bundle,
                adapter=adapter,
                requested_adapter_id=adapter.adapter_id,
                run_dir=self.run_dir,
                approval_ref=APPROVAL,
                secret_files=NO_SECRETS,
                now=lambda: state_updated,
            ),
        )
        self.assertIsNone(
            json.loads((self.run_dir / "execution-state.json").read_text())[
                "terminal_receipt_sha256"
            ]
        )
        recovered = self.execute_bundle(
            bundle=bundle,
            adapter=adapter,
            requested_adapter_id=adapter.adapter_id,
            run_dir=self.run_dir,
            approval_ref=APPROVAL,
            secret_files=NO_SECRETS,
        )
        self.assertEqual(recovered["status"], "offline_fixture_complete")
        self.assertEqual(len(adapter.calls), len(executor.PHASES))

    def test_terminal_is_self_validated_before_publication_and_rejects_numeric_aliases(self) -> None:
        bundle = self.validate()
        adapter = CountingAdapter()
        real_terminal_receipt = executor._terminal_receipt

        def forged_terminal(*args, **kwargs):
            terminal = real_terminal_receipt(*args, **kwargs)
            terminal["pending_object_count"] = True
            return terminal

        with mock.patch.object(
            executor,
            "_terminal_receipt",
            side_effect=forged_terminal,
        ):
            self.assert_error(
                "SEMANTIC_EXECUTOR_TERMINAL_INVALID",
                lambda: self.execute_bundle(
                    bundle=bundle,
                    adapter=adapter,
                    requested_adapter_id=adapter.adapter_id,
                    run_dir=self.run_dir,
                    approval_ref=APPROVAL,
                    secret_files=NO_SECRETS,
                ),
            )
        self.assertFalse((self.run_dir / "terminal-receipt.json").exists())
        state = json.loads((self.run_dir / "execution-state.json").read_text())
        self.assertIsNone(state["terminal_receipt_sha256"])
        self.assertTrue(
            all(state["phases"][phase]["status"] == "completed" for phase in executor.PHASES)
        )

        terminal = self.execute_bundle(
            bundle=bundle,
            adapter=adapter,
            requested_adapter_id=adapter.adapter_id,
            run_dir=self.run_dir,
            approval_ref=APPROVAL,
            secret_files=NO_SECRETS,
        )
        self.assertEqual(len(adapter.calls), len(executor.PHASES))
        durable_state = json.loads((self.run_dir / "execution-state.json").read_text())
        runtime = executor.AdapterRuntime(
            module=adapter_contract,
            instance=adapter,
            phase_request_type=PhaseRequest,
            phase_result_type=PhaseResult,
            secret_snapshot_type=SecretSnapshot,
        )
        for alias in (True, float(bundle.job["pending_objects"]["count"])):
            with self.subTest(alias=alias):
                forged = copy.deepcopy(terminal)
                forged["pending_object_count"] = alias
                self.assert_error(
                    "SEMANTIC_EXECUTOR_TERMINAL_INVALID",
                    lambda forged=forged: executor._validate_terminal_receipt(
                        forged,
                        bundle,
                        runtime,
                        durable_state,
                    ),
                )

    def test_committed_evidence_fsync_failure_reports_durability_uncertainty(self) -> None:
        evidence_dir = self.root / "evidence"
        evidence_dir.mkdir(mode=0o700)
        path = evidence_dir / "terminal-receipt.json"
        with executor._open_run_directory(evidence_dir, create=False) as handle:
            with executor._run_lock(handle):
                with mock.patch.object(
                    executor,
                    "_fsync_directory_fd",
                    side_effect=OSError("synthetic directory fsync failure"),
                ):
                    error = self.assert_error(
                        "SEMANTIC_EXECUTOR_EVIDENCE_COMMITTED_NOT_DURABLE",
                        lambda: executor._atomic_write_at(
                            handle,
                            path.name,
                            executor.canonical_json({"status": "fixture"}),
                            replace=False,
                        ),
                    )
        self.assertTrue(error.committed)
        self.assertTrue(error.durability_uncertain)
        self.assertTrue(path.is_file())
        self.assertEqual(path.stat().st_nlink, 1)
        self.assertEqual(list(evidence_dir.glob(".terminal-receipt.json.tmp-*")), [])

    def test_two_processes_contend_on_the_same_held_lock_inode(self) -> None:
        if not hasattr(os, "fork"):
            self.skipTest("requires fork")
        bundle = self.validate()
        read_fd, write_fd = os.pipe()
        child_pid = os.fork()
        if child_pid == 0:
            os.close(read_fd)
            try:
                with executor._open_run_directory(self.run_dir, create=True) as handle:
                    with executor._run_lock(handle):
                        os.write(write_fd, b"1")
                        time.sleep(5)
            except BaseException:
                try:
                    os.write(write_fd, b"E")
                except OSError:
                    pass
            finally:
                os.close(write_fd)
                os._exit(0)
        os.close(write_fd)
        try:
            self.assertEqual(os.read(read_fd, 1), b"1")
            self.assert_error(
                "SEMANTIC_EXECUTOR_RUN_BUSY",
                lambda: executor.execute_bundle(
                    bundle=bundle,
                    requested_adapter_id=executor.OFFLINE_ADAPTER_ID,
                    run_dir=self.run_dir,
                    approval_ref=APPROVAL,
                    secret_paths=NO_SECRET_PATHS,
                ),
            )
        finally:
            os.close(read_fd)
            try:
                os.kill(child_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            os.waitpid(child_pid, 0)

    def test_run_directory_rename_and_recreate_fails_closed(self) -> None:
        moved = self.run_parent / "run-r1-moved"
        with executor._open_run_directory(self.run_dir, create=True) as handle:
            parent_fd = handle.parent_fd
            run_fd = handle.run_fd
            with executor._run_lock(handle):
                os.rename(self.run_dir, moved)
                self.run_dir.mkdir(mode=0o700)
                self.assert_error(
                    "SEMANTIC_EXECUTOR_RUN_DIR_INVALID",
                    lambda: executor._atomic_write_at(
                        handle,
                        "execution-state.json",
                        executor.canonical_json({"status": "must-not-write"}),
                        replace=False,
                    ),
                )
                self.assertEqual(list(self.run_dir.iterdir()), [])
                self.assertEqual(
                    {path.name for path in moved.iterdir()},
                    {"executor.lock"},
                )
        with self.assertRaises(OSError):
            os.fstat(run_fd)
        with self.assertRaises(OSError):
            os.fstat(parent_fd)

    def test_dirfd_cleanup_preserves_primary_error_and_closes_every_descriptor(self) -> None:
        real_close = os.close
        opened_descriptors: list[int] = []

        def close_then_report_error(descriptor: int) -> None:
            real_close(descriptor)
            raise OSError("synthetic close error after close")

        def fail_inside_locked_run() -> None:
            with executor._open_run_directory(self.run_dir, create=True) as handle:
                opened_descriptors.extend([handle.parent_fd, handle.run_fd])
                with executor._run_lock(handle):
                    assert handle.lock_fd is not None
                    opened_descriptors.append(handle.lock_fd)
                    executor.fail(
                        "SEMANTIC_EXECUTOR_ADAPTER_FAILED",
                        "primary bounded failure",
                    )

        with mock.patch.object(executor.os, "close", side_effect=close_then_report_error):
            self.assert_error("SEMANTIC_EXECUTOR_ADAPTER_FAILED", fail_inside_locked_run)
        self.assertEqual(len(opened_descriptors), 3)
        for descriptor in opened_descriptors:
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_real_subprocess_sigkill_crash_point_matrix_resumes_safely(self) -> None:
        if not hasattr(os, "killpg"):
            self.skipTest("requires process-group signaling")
        plan = self.make_plan()
        plan["profile"] = "production"
        plan["credential_files_required"] = list(executor.SECRET_LABELS)
        plan["credential_bindings"] = self.production_credential_bindings("crash-r1")
        self.write_plan(plan)
        bundle = self.validate()

        wrong_bundles = []
        for mutation in ("generation", "target"):
            wrong_plan = copy.deepcopy(plan)
            if mutation == "generation":
                wrong_plan["credential_bindings"]["caption"]["credential_generation"] = (
                    "credential-generation:wrong-crash-generation"
                )
            else:
                wrong_plan["credential_bindings"]["caption"]["target_identity"] = (
                    "provider-project:wrong-crash-target"
                )
            wrong_plan_path = self.root / f"wrong-crash-{mutation}-plan.json"
            wrong_plan_path.write_bytes(executor.canonical_json(wrong_plan))
            wrong_plan_path.chmod(0o600)
            wrong_pins = self.pins()
            wrong_pins["expected_execution_plan_sha256"] = hashlib.sha256(
                wrong_plan_path.read_bytes()
            ).hexdigest()
            wrong_bundles.append(
                executor.validate_bundle(
                    job_path=self.job_path,
                    preparation_receipt_path=self.receipt_path,
                    execution_plan_path=wrong_plan_path,
                    repo_root=REPO_ROOT,
                    **wrong_pins,
                )
            )

        harness = self.write_abrupt_termination_harness()
        points = (
            "state_temp_after_write",
            "state_temp_after_fsync",
            "state_after_rename",
            "state_after_dir_fsync",
            "adapter_after_return",
            "phase_completed_after_commit",
            "terminal_before_rename",
            "terminal_after_rename",
            "terminal_state_after_commit",
        )
        resume_runs_inspect = frozenset(
            {
                "state_temp_after_write",
                "state_temp_after_fsync",
                "state_after_rename",
                "state_after_dir_fsync",
                "adapter_after_return",
            }
        )
        for point in points:
            with self.subTest(point=point):
                run_dir = self.run_parent / f"run-{point}"
                seed_runtime = self.production_runtime()
                seed_time = dt.datetime.now(dt.timezone.utc)
                seed_state = executor._new_state(bundle, seed_runtime, now=seed_time)
                inspect_key = seed_state["phases"]["inspect"]["idempotency_key"]
                with executor._open_run_directory(run_dir, create=True) as handle:
                    with executor._run_lock(handle):
                        executor._atomic_write_at(
                            handle,
                            "execution-state.json",
                            executor.canonical_json(seed_state),
                            replace=False,
                        )

                marker = self.root / f"marker-{point}"
                process = self.launch_abrupt_subprocess(
                    harness=harness,
                    point=point,
                    run_dir=run_dir,
                    marker=marker,
                )
                self.wait_for_subprocess_marker(process, marker)
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, -signal.SIGKILL, (stdout, stderr))

                state_path = run_dir / "execution-state.json"
                terminal_path = run_dir / "terminal-receipt.json"
                for wrong_bundle in wrong_bundles:
                    state_before_wrong_resume = state_path.read_bytes()
                    terminal_before_wrong_resume = (
                        terminal_path.read_bytes() if terminal_path.exists() else None
                    )
                    self.assert_error(
                        "SEMANTIC_EXECUTOR_STATE_MISMATCH",
                        lambda wrong_bundle=wrong_bundle: executor._execute_with_adapter(
                            bundle=wrong_bundle,
                            adapter=self.production_runtime(),
                            run_dir=run_dir,
                            approval_ref=APPROVAL,
                            secret_snapshot=NO_SECRET_SNAPSHOT,
                        ),
                    )
                    self.assertEqual(state_path.read_bytes(), state_before_wrong_resume)
                    self.assertEqual(
                        terminal_path.read_bytes() if terminal_path.exists() else None,
                        terminal_before_wrong_resume,
                    )

                resume_adapter = ProductionFixtureAdapter()
                terminal = self.execute_bundle(
                    bundle=bundle,
                    adapter=resume_adapter,
                    requested_adapter_id=resume_adapter.adapter_id,
                    run_dir=run_dir,
                    approval_ref=APPROVAL,
                    secret_files=NO_SECRET_SNAPSHOT,
                )
                self.assertEqual(terminal["status"], "production_complete")
                self.assertTrue(terminal["production_complete"])
                final_state = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual(
                    final_state["credential_bindings"],
                    plan["credential_bindings"],
                )
                self.assertEqual(
                    final_state["phases"]["inspect"]["idempotency_key"],
                    inspect_key,
                )
                self.assertEqual(
                    any(phase == "inspect" for phase, _key in resume_adapter.calls),
                    point in resume_runs_inspect,
                )
                for phase, idempotency_key in resume_adapter.calls:
                    self.assertEqual(
                        idempotency_key,
                        final_state["phases"][phase]["idempotency_key"],
                    )
                expected_targets = {
                    label: binding["target_identity"]
                    for label, binding in plan["credential_bindings"].items()
                }
                for phase_result in terminal["phase_results"]:
                    self.assertEqual(
                        phase_result["credential_target_identities"],
                        expected_targets,
                    )
                self.assertEqual(list(run_dir.glob(".*.tmp-*")), [])

    def test_two_real_subprocesses_contend_on_the_same_plan_and_run(self) -> None:
        if not hasattr(os, "killpg"):
            self.skipTest("requires process-group signaling")
        plan = self.make_plan()
        plan["profile"] = "production"
        plan["credential_files_required"] = list(executor.SECRET_LABELS)
        plan["credential_bindings"] = self.production_credential_bindings("contend-r1")
        self.write_plan(plan)
        bundle = self.validate()
        run_dir = self.run_parent / "run-real-subprocess-contention"
        seed_state = executor._new_state(
            bundle,
            self.production_runtime(),
            now=dt.datetime.now(dt.timezone.utc),
        )
        with executor._open_run_directory(run_dir, create=True) as handle:
            with executor._run_lock(handle):
                executor._atomic_write_at(
                    handle,
                    "execution-state.json",
                    executor.canonical_json(seed_state),
                    replace=False,
                )

        harness = self.write_abrupt_termination_harness()
        holder_marker = self.root / "holder-marker"
        holder = self.launch_abrupt_subprocess(
            harness=harness,
            point="hold_adapter_before_return",
            run_dir=run_dir,
            marker=holder_marker,
        )
        self.wait_for_subprocess_marker(holder, holder_marker)
        state_path = run_dir / "execution-state.json"
        state_while_held = state_path.read_bytes()
        lock_identity = os.lstat(run_dir / "executor.lock")

        contender_marker = self.root / "contender-marker"
        contender = self.launch_abrupt_subprocess(
            harness=harness,
            point="hold_adapter_before_return",
            run_dir=run_dir,
            marker=contender_marker,
        )
        contender_stdout, contender_stderr = contender.communicate(timeout=10)
        self.assertEqual(contender.returncode, 2, contender_stderr)
        contender_result = json.loads(contender_stdout)
        self.assertEqual(
            contender_result["error"]["code"],
            "SEMANTIC_EXECUTOR_RUN_BUSY",
        )
        self.assertFalse(contender_marker.exists())
        self.assertEqual(state_path.read_bytes(), state_while_held)
        current_lock = os.lstat(run_dir / "executor.lock")
        self.assertEqual(
            (current_lock.st_dev, current_lock.st_ino),
            (lock_identity.st_dev, lock_identity.st_ino),
        )

        os.killpg(holder.pid, signal.SIGKILL)
        holder_stdout, holder_stderr = holder.communicate(timeout=5)
        self.assertEqual(holder.returncode, -signal.SIGKILL, (holder_stdout, holder_stderr))
        resume_adapter = ProductionFixtureAdapter()
        terminal = self.execute_bundle(
            bundle=bundle,
            adapter=resume_adapter,
            requested_adapter_id=resume_adapter.adapter_id,
            run_dir=run_dir,
            approval_ref=APPROVAL,
            secret_files=NO_SECRET_SNAPSHOT,
        )
        self.assertEqual(terminal["status"], "production_complete")
        self.assertEqual(
            resume_adapter.calls[0],
            ("inspect", seed_state["phases"]["inspect"]["idempotency_key"]),
        )

    def test_live_subprocess_rejects_run_parent_and_lock_rebinding_without_writes(self) -> None:
        plan = self.make_plan()
        plan["profile"] = "production"
        plan["credential_files_required"] = list(executor.SECRET_LABELS)
        plan["credential_bindings"] = self.production_credential_bindings("rebind-r1")
        self.write_plan(plan)
        bundle = self.validate()
        harness = self.write_abrupt_termination_harness()

        for attack in ("run", "parent", "lock"):
            with self.subTest(attack=attack):
                private_parent = self.root / f"rebind-parent-{attack}"
                private_parent.mkdir(mode=0o700)
                run_dir = private_parent / "run-r1"
                seed_state = executor._new_state(
                    bundle,
                    self.production_runtime(),
                    now=dt.datetime.now(dt.timezone.utc),
                )
                with executor._open_run_directory(run_dir, create=True) as handle:
                    with executor._run_lock(handle):
                        executor._atomic_write_at(
                            handle,
                            "execution-state.json",
                            executor.canonical_json(seed_state),
                            replace=False,
                        )
                marker = self.root / f"rebind-marker-{attack}"
                process = self.launch_abrupt_subprocess(
                    harness=harness,
                    point="hold_adapter_before_return",
                    run_dir=run_dir,
                    marker=marker,
                )
                self.wait_for_subprocess_marker(process, marker)

                state_before = (run_dir / "execution-state.json").read_bytes()
                if attack == "run":
                    moved_run = private_parent / "run-r1-moved"
                    os.rename(run_dir, moved_run)
                    run_dir.mkdir(mode=0o700)
                    authoritative_state_path = moved_run / "execution-state.json"
                    expected_error = "SEMANTIC_EXECUTOR_RUN_DIR_INVALID"
                elif attack == "parent":
                    moved_parent = self.root / "rebind-parent-parent-moved"
                    os.rename(private_parent, moved_parent)
                    private_parent.mkdir(mode=0o700)
                    run_dir.mkdir(mode=0o700)
                    authoritative_state_path = moved_parent / "run-r1" / "execution-state.json"
                    expected_error = "SEMANTIC_EXECUTOR_RUN_DIR_INVALID"
                else:
                    old_lock = run_dir / "executor.lock.old"
                    os.rename(run_dir / "executor.lock", old_lock)
                    replacement_lock_fd = os.open(
                        run_dir / "executor.lock",
                        os.O_RDWR | os.O_CREAT | os.O_EXCL,
                        0o600,
                    )
                    os.close(replacement_lock_fd)
                    authoritative_state_path = run_dir / "execution-state.json"
                    expected_error = "SEMANTIC_EXECUTOR_RUN_BUSY"

                os.kill(process.pid, signal.SIGUSR1)
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 2, stderr)
                self.assertEqual(json.loads(stdout)["error"]["code"], expected_error)
                self.assertEqual(authoritative_state_path.read_bytes(), state_before)
                if attack in {"run", "parent"}:
                    self.assertFalse((run_dir / "execution-state.json").exists())
                    self.assertEqual(list(run_dir.iterdir()), [])
                else:
                    self.assertEqual((run_dir / "executor.lock").stat().st_size, 0)

    def test_unexpected_run_files_state_identity_and_approval_mismatch_are_rejected(self) -> None:
        bundle = self.validate()
        self.run_dir.mkdir(mode=0o700)
        (self.run_dir / "foreign.txt").write_text("foreign", encoding="utf-8")
        self.assert_error(
            "SEMANTIC_EXECUTOR_RUN_DIR_INVALID",
            lambda: self.execute_bundle(
                bundle=bundle,
                adapter=OfflineFixtureAdapter(),
                requested_adapter_id=OfflineFixtureAdapter.adapter_id,
                run_dir=self.run_dir,
                approval_ref=APPROVAL,
                secret_files=NO_SECRETS,
            ),
        )
        (self.run_dir / "foreign.txt").unlink()
        self.assert_error(
            "SEMANTIC_EXECUTOR_APPROVAL_MISMATCH",
            lambda: self.execute_bundle(
                bundle=bundle,
                adapter=OfflineFixtureAdapter(),
                requested_adapter_id=OfflineFixtureAdapter.adapter_id,
                run_dir=self.run_dir,
                approval_ref="CHANGE-WRONG-APPROVAL",
                secret_files=NO_SECRETS,
            ),
        )

    def test_cli_default_is_dry_run_and_unknown_adapter_cannot_be_loaded(self) -> None:
        pins = self.pins()
        args = [
            "--job", str(self.job_path),
            "--preparation-receipt", str(self.receipt_path),
            "--execution-plan", str(self.plan_path),
            "--expected-job-sha256", pins["expected_job_sha256"],
            "--expected-preparation-receipt-sha256", pins["expected_preparation_receipt_sha256"],
            "--expected-execution-plan-sha256", pins["expected_execution_plan_sha256"],
            "--expected-job-schema-sha256", pins["expected_job_schema_sha256"],
            "--expected-execution-plan-schema-sha256", pins["expected_execution_plan_schema_sha256"],
            "--repo-root", str(REPO_ROOT),
            "--adapter", OfflineFixtureAdapter.adapter_id,
        ]
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(executor.main(args), 0)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "dry_run")
        self.assertFalse(self.run_dir.exists())

        stderr = io.StringIO()
        unknown = list(args)
        unknown[-1] = "dynamic-production-plugin"
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(executor.main(unknown), 2)
        self.assertEqual(json.loads(stderr.getvalue())["error"]["code"], "SEMANTIC_EXECUTOR_ADAPTER_UNAVAILABLE")

    def test_source_allows_only_fixed_git_subprocess_and_no_network_or_legacy_runner(self) -> None:
        source = (TOOLS_DIR / "execute_semantic_asset_index_job.py").read_text(encoding="utf-8")
        adapter_source = (TOOLS_DIR / "semantic_index_job_adapters.py").read_text(encoding="utf-8")
        tree = ast.parse(source + "\n" + adapter_source)
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".")[0])
        self.assertIn("subprocess", imports)
        self.assertTrue({"socket", "urllib", "requests", "importlib"}.isdisjoint(imports))
        self.assertNotIn("import full_asset_index_runner", source + adapter_source)
        self.assertNotIn("from full_asset_index_runner", source + adapter_source)
        self.assertNotIn("shell=True", source + adapter_source)
        self.assertIn('"/usr/bin/git"', source)
        self.assertNotIn("subprocess.run", source)
        self.assertEqual(executor._fixed_git_environment()["GIT_NO_LAZY_FETCH"], "1")
        self.assertIn("start_new_session=True", source)
        self.assertIn("O_DIRECTORY", source)
        self.assertIn("O_NOFOLLOW", source)
        self.assertIn("dir_fd=handle.run_fd", source)
        self.assertNotIn("renameat2(-100", source)
        parser_actions = {action.dest for action in executor.build_parser()._actions}
        self.assertFalse(any(dest.endswith(("password", "token", "dsn", "api_key")) for dest in parser_actions))
        self.assertTrue({"caption_secret_file", "postgres_secret_file", "qdrant_secret_file", "embedding_secret_file"}.issubset(parser_actions))

    def test_execution_plan_schema_matches_closed_plan(self) -> None:
        schema = json.loads((TOOLS_DIR / "semantic_asset_index_execution_plan_schema.json").read_text())
        job_schema = json.loads((TOOLS_DIR / "semantic_asset_index_job_schema.json").read_text())
        plan = self.make_plan()
        self.assertEqual(schema["properties"]["schema"]["const"], executor.EXECUTION_PLAN_SCHEMA)
        self.assertEqual(set(schema["required"]), set(plan))
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["properties"]["job"]["required"]), set(plan["job"]))
        self.assertEqual(set(schema["properties"]["executor"]["required"]), set(plan["executor"]))
        self.assertFalse(schema["properties"]["executor"]["additionalProperties"])
        self.assertEqual(schema["$defs"]["image"]["maxLength"], 512)
        self.assertEqual(
            schema["$defs"]["image"]["pattern"],
            r"^[^\s@]+@sha256:[a-f0-9]{64}$",
        )
        credentials = schema["properties"]["credential_files_required"]
        self.assertEqual(credentials["items"]["enum"], list(executor.SECRET_LABELS))
        self.assertTrue(credentials["uniqueItems"])
        self.assertEqual(credentials["maxItems"], 4)
        profile_contract = schema["allOf"][0]
        self.assertEqual(
            profile_contract["then"]["properties"]["credential_files_required"],
            {"maxItems": 0},
        )
        production_credentials = profile_contract["else"]["properties"][
            "credential_files_required"
        ]
        self.assertEqual(
            production_credentials["prefixItems"],
            [{"const": label} for label in executor.SECRET_LABELS],
        )
        self.assertFalse(production_credentials["items"])
        self.assertEqual(production_credentials["minItems"], 4)
        self.assertEqual(production_credentials["maxItems"], 4)
        self.assertEqual(schema["$defs"]["revision"], job_schema["$defs"]["revision"])
        self.assertEqual(schema["$defs"]["id"], job_schema["$defs"]["id"])
        self.assertEqual(schema["$defs"]["caption"], job_schema["$defs"]["captionRecipe"])
        self.assertEqual(schema["$defs"]["embedding"], job_schema["$defs"]["embeddingRecipe"])
        self.assertEqual(schema["$defs"]["storage"], job_schema["$defs"]["storageRecipe"])
        self.assertEqual(
            schema["properties"]["job"]["properties"]["asset_snapshot_revision"],
            job_schema["properties"]["snapshot_target"]["properties"][
                "asset_snapshot_revision"
            ],
        )
        bindings = schema["$defs"]["credentialBindings"]
        self.assertFalse(bindings["additionalProperties"])
        self.assertEqual(bindings["maxProperties"], 4)
        self.assertEqual(set(bindings["properties"]), set(executor.SECRET_LABELS))
        binding = schema["$defs"]["credentialBinding"]
        self.assertFalse(binding["additionalProperties"])
        self.assertEqual(
            set(binding["required"]),
            {"credential_generation", "target_identity"},
        )
        self.assertEqual(
            binding["properties"]["credential_generation"],
            {
                "type": "string",
                "minLength": 23,
                "maxLength": 213,
                "pattern": r"^credential-generation:[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,190}$",
            },
        )
        self.assertEqual(binding["properties"]["target_identity"], {"type": "string"})
        for label in executor.SECRET_LABELS:
            target = bindings["properties"][label]["allOf"][1]["properties"][
                "target_identity"
            ]
            self.assertEqual(target["pattern"], executor.TARGET_IDENTITY_PATTERNS[label].pattern)
            self.assertEqual(
                (target["minLength"], target["maxLength"]),
                executor.TARGET_IDENTITY_LENGTHS[label],
            )


if __name__ == "__main__":
    unittest.main()
