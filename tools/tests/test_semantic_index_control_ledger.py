from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import fcntl
import hashlib
import json
import os
import pathlib
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


TOOLS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

import semantic_index_control_ledger as control  # noqa: E402
import semantic_index_worker_protocol as protocol  # noqa: E402


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
SHA_F = "f" * 64
SHA_1 = "1" * 64
SHA_2 = "2" * 64
REV_A = "sha256:" + SHA_A
REV_B = "sha256:" + SHA_B
REV_C = "sha256:" + SHA_C
REV_D = "sha256:" + SHA_D
REV_E = "sha256:" + SHA_E
GENERATION_ID = "semantic-generation:" + SHA_1

PINS = control.ControlLedgerPins(
    ledger_identity="worker-ledger:prod",
    ledger_revision=REV_C,
    job_revision=REV_A,
    reviewed_job_sha256=SHA_A,
    approval_basis_sha256=SHA_B,
    execution_plan_sha256=SHA_C,
    generation_id=GENERATION_ID,
    generation_nonce_sha256=SHA_E,
    generation_binding_sha256=SHA_D,
    run_id="semantic-index-run:run-001",
    correlation_id="semantic-index-correlation:correlation-001",
    owner_identity="owner-ci",
    lease_id="lease-001",
    slot_id="gpu-slot-0",
    worker_deployment_identity="semantic-worker-r1",
)


def utc_text(value: dt.datetime) -> str:
    return (
        value.astimezone(dt.timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def request_binding(request: dict) -> dict:
    return {
        "request_sha256": hashlib.sha256(
            protocol.canonical_json_bytes(request)
        ).hexdigest(),
        "request_id": request["request_id"],
        "operation": request["operation"],
        "target_phase": request["target_phase"],
        "phase_request_sha256": request["phase_request_sha256"],
        "phase_idempotency_key": request["phase_idempotency_key"],
        "control_idempotency_key": request["control_idempotency_key"],
        "idempotency_ledger_identity": request["idempotency_ledger_identity"],
        "idempotency_ledger_revision": request["idempotency_ledger_revision"],
        "job_revision": request["job_revision"],
        "reviewed_job_sha256": request["reviewed_job_sha256"],
        "approval_basis_sha256": request["approval_basis_sha256"],
        "execution_plan_sha256": request["execution_plan_sha256"],
        "launcher_handoff_receipt_sha256": request["launcher_handoff_receipt_sha256"],
        "generation_id": request["generation_id"],
        "generation_binding_sha256": request["generation_binding_sha256"],
        "host_boot_id_sha256": request["worker_binding"]["host_boot_id_sha256"],
        "deadline_monotonic_ns": request["deadline_monotonic_ns"],
    }


def make_request(
    operation: str = "query_phase_status",
    *,
    deadline_ns: int | None = None,
    control_key: str = REV_E,
    request_id: str | None = None,
) -> dict:
    contract = protocol.control_operation_contract(operation)
    boot = protocol.current_host_boot_id_sha256()
    peer_pid = os.getpid()
    start_ticks = protocol.process_start_time_ticks(peer_pid)
    return {
        "schema": protocol.CONTROL_REQUEST_SCHEMA,
        "protocol": protocol.PROTOCOL_REVISION,
        "operation": operation,
        "operation_revision": contract["revision"],
        "request_id": request_id or f"control-request-{operation}",
        "job_revision": PINS.job_revision,
        "reviewed_job_sha256": PINS.reviewed_job_sha256,
        "approval_basis_sha256": PINS.approval_basis_sha256,
        "execution_plan_sha256": PINS.execution_plan_sha256,
        "launcher_handoff_receipt_sha256": SHA_F,
        "generation_id": PINS.generation_id,
        "generation_binding_sha256": PINS.generation_binding_sha256,
        "execution_binding": {
            "run_id": PINS.run_id,
            "correlation_id": PINS.correlation_id,
            "owner_identity": PINS.owner_identity,
            "lease_id": PINS.lease_id,
            "slot_id": PINS.slot_id,
        },
        "worker_binding": {
            "deployment_identity": PINS.worker_deployment_identity,
            "runtime_image": "registry.local/semantic-worker@sha256:" + SHA_D,
            "whoami_sha256": SHA_A,
            "peer_attestation_sha256": SHA_B,
            "runtime_attestation_sha256": SHA_C,
            "host_boot_id_sha256": boot,
            "peer_uid": os.getuid(),
            "peer_gid": os.getgid(),
            "peer_pid": peer_pid,
            "process_start_time_ticks": start_ticks,
            "process_start_token_sha256": protocol.process_start_token_sha256(
                boot, peer_pid, start_ticks
            ),
            "socket_device": 1,
            "socket_inode": 1,
            "socket_inode_binding_sha256": protocol.socket_inode_binding_sha256(
                boot, peer_pid, 1, 1
            ),
        },
        "target_phase": "caption",
        "phase_request_sha256": SHA_2,
        "phase_idempotency_key": REV_B,
        "control_idempotency_key": control_key,
        "idempotency_ledger_identity": PINS.ledger_identity,
        "idempotency_ledger_revision": PINS.ledger_revision,
        "credential_transport": {
            "kind": protocol.CREDENTIAL_TRANSPORT,
            "json_contains_credential_bytes": False,
            "descriptors": [
                {
                    "fd_index": 0,
                    "scope": contract["scope"],
                    "credential_generation": REV_D,
                    "generation_id": PINS.generation_id,
                    "target_identity": PINS.worker_deployment_identity,
                    "byte_count": 64,
                }
            ],
        },
        "deadline_monotonic_ns": deadline_ns
        if deadline_ns is not None
        else time.monotonic_ns() + 5_000_000_000,
    }


def make_success_result(request: dict, *, now: dt.datetime | None = None) -> dict:
    current = now or dt.datetime.now(dt.timezone.utc)
    operation = request["operation"]
    contract = protocol.control_operation_contract(operation)
    return {
        "schema": protocol.CONTROL_RESULT_SCHEMA,
        "protocol": protocol.PROTOCOL_REVISION,
        "request_binding": request_binding(request),
        "status": "succeeded",
        "mutation_state": contract["success_mutation_state"],
        "receipt": {
            "schema": protocol.CONTROL_RECEIPT_SCHEMA,
            "operation": operation,
            "generation_id": request["generation_id"],
            "target_phase": request["target_phase"],
            "phase_request_sha256": request["phase_request_sha256"],
            "phase_idempotency_key": request["phase_idempotency_key"],
            "control_idempotency_key": request["control_idempotency_key"],
            "observed_phase_state": "committed",
            "immutable_phase_receipt_sha256": SHA_A
            if operation == "recover_phase_receipt"
            else None,
            "cancelled": operation == "cancel_phase_work",
            "quarantined": operation == "quarantine_generation",
            "issued_at": utc_text(current - dt.timedelta(seconds=1)),
            "expires_at": utc_text(current + dt.timedelta(minutes=5)),
        },
        "error": None,
    }


def make_failed_result(request: dict, mutation_state: str = "none") -> dict:
    return {
        "schema": protocol.CONTROL_RESULT_SCHEMA,
        "protocol": protocol.PROTOCOL_REVISION,
        "request_binding": request_binding(request),
        "status": "failed",
        "mutation_state": mutation_state,
        "receipt": None,
        "error": {
            "dependency": "worker",
            "code": "WORKER_BUSY",
            "retryable": mutation_state == "none",
            "mutation_state": mutation_state,
        },
    }


def request_bytes(request: dict) -> bytes:
    return protocol.canonical_json_bytes(request)


def result_bytes(result: dict) -> bytes:
    return protocol.canonical_json_bytes(result)


def binding_for(
    request: dict, pins: control.ControlLedgerPins = PINS
) -> control.ControlLedgerBinding:
    descriptor = request["credential_transport"]["descriptors"][0]
    return control.ControlLedgerBinding(
        pins=pins,
        operation=request["operation"],
        operation_revision=request["operation_revision"],
        target_phase=request["target_phase"],
        phase_request_sha256=request["phase_request_sha256"],
        phase_idempotency_key=request["phase_idempotency_key"],
        control_idempotency_key=request["control_idempotency_key"],
        credential_scope=descriptor["scope"],
        credential_generation=descriptor["credential_generation"],
        target_identity=descriptor["target_identity"],
    )


def entry_names(request: dict) -> tuple[str, str]:
    digest = control.derive_control_ledger_key(binding_for(request)).removeprefix(
        "sha256:"
    )
    return (
        f"control-ledger-{digest}.prepare.json",
        f"control-ledger-{digest}.result.json",
    )


class DurableControlLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        os.chmod(self.root, 0o700)
        self.directory_fd = os.open(
            self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
        )
        self.ledger = control.DurableControlLedger(self.directory_fd, PINS)
        self.request = make_request()
        self.request_bytes = request_bytes(self.request)
        self.result = make_success_result(self.request)
        self.result_bytes = result_bytes(self.result)

    def tearDown(self) -> None:
        self.ledger.close()
        os.close(self.directory_fd)
        self.temporary.cleanup()

    def assert_error(self, code: str, callback) -> control.ControlLedgerError:
        with self.assertRaises(control.ControlLedgerError) as caught:
            callback()
        self.assertEqual(code, caught.exception.code)
        self.assertEqual(control.PUBLIC_ERROR_MESSAGES[code], caught.exception.message)
        return caught.exception

    def test_prepare_commit_exact_replay_and_private_files(self) -> None:
        first = self.ledger.begin(self.request_bytes)
        self.assertEqual("execute", first.action)
        self.assertEqual(
            control.derive_control_ledger_key(binding_for(self.request)),
            first.ledger_key,
        )
        self.assertEqual(
            hashlib.sha256(self.request_bytes).hexdigest(), first.request_sha256
        )
        self.assertIsNone(first.result_bytes)

        incomplete = self.ledger.begin(self.request_bytes)
        self.assertEqual("recovery_required", incomplete.action)
        self.assertIsNone(incomplete.result_bytes)

        self.assertEqual(
            self.result_bytes,
            self.ledger.commit_result(self.request_bytes, self.result_bytes),
        )
        replay = self.ledger.begin(self.request_bytes)
        self.assertEqual("replay", replay.status)
        self.assertEqual(self.result_bytes, replay.result_bytes)
        self.assertEqual(
            self.result_bytes,
            self.ledger.commit_result(self.request_bytes, self.result_bytes),
        )

        for filename in entry_names(self.request):
            metadata = (self.root / filename).lstat()
            self.assertTrue(stat.S_ISREG(metadata.st_mode))
            self.assertEqual(0o600, stat.S_IMODE(metadata.st_mode))
            self.assertEqual(os.geteuid(), metadata.st_uid)
            self.assertEqual(1, metadata.st_nlink)
        self.assertFalse(
            any(path.name.endswith(".tmp") for path in self.root.iterdir())
        )

    def test_all_four_fixed_control_operations_commit_and_replay(self) -> None:
        for index, operation in enumerate(
            (
                "query_phase_status",
                "recover_phase_receipt",
                "cancel_phase_work",
                "quarantine_generation",
            ),
            start=10,
        ):
            with self.subTest(operation=operation):
                request = make_request(
                    operation,
                    control_key="sha256:" + format(index, "064x"),
                )
                raw_request = request_bytes(request)
                raw_result = result_bytes(make_success_result(request))
                self.assertEqual("execute", self.ledger.begin(raw_request).action)
                self.assertEqual(
                    raw_result,
                    self.ledger.commit_result(raw_request, raw_result),
                )
                self.assertEqual(
                    raw_result, self.ledger.begin(raw_request).result_bytes
                )

    def test_same_control_tuple_with_changed_request_is_a_conflict(self) -> None:
        self.assertEqual("execute", self.ledger.begin(self.request_bytes).action)
        changed = copy.deepcopy(self.request)
        changed["request_id"] = "control-request-substituted"
        changed_bytes = request_bytes(changed)
        error = self.assert_error(
            "CONTROL_LEDGER_KEY_CONFLICT",
            lambda: self.ledger.begin(changed_bytes),
        )
        self.assertNotIn(hashlib.sha256(changed_bytes).hexdigest(), str(error))

    def test_result_is_immutable_and_requires_prepare(self) -> None:
        self.assert_error(
            "CONTROL_LEDGER_PREPARE_REQUIRED",
            lambda: self.ledger.commit_result(self.request_bytes, self.result_bytes),
        )
        self.ledger.begin(self.request_bytes)
        self.ledger.commit_result(self.request_bytes, self.result_bytes)
        different = make_failed_result(self.request)
        self.assert_error(
            "CONTROL_LEDGER_RESULT_CONFLICT",
            lambda: self.ledger.commit_result(
                self.request_bytes, result_bytes(different)
            ),
        )

    def test_release_job_generation_and_execution_pins_are_independent(self) -> None:
        mismatches = {
            "idempotency_ledger_revision": REV_D,
            "job_revision": REV_D,
            "reviewed_job_sha256": SHA_D,
            "approval_basis_sha256": SHA_D,
            "execution_plan_sha256": SHA_D,
            "generation_id": "semantic-generation:" + SHA_2,
            "generation_binding_sha256": SHA_E,
        }
        for field, value in mismatches.items():
            request = copy.deepcopy(self.request)
            request[field] = value
            if field == "generation_id":
                request["credential_transport"]["descriptors"][0][field] = value
            with self.subTest(field=field):
                self.assert_error(
                    "CONTROL_LEDGER_CONTEXT_MISMATCH",
                    lambda request=request: self.ledger.begin(request_bytes(request)),
                )

        for field, value in {
            "run_id": "semantic-index-run:run-002",
            "correlation_id": "semantic-index-correlation:correlation-002",
            "owner_identity": "owner-other",
            "lease_id": "lease-002",
            "slot_id": "gpu-slot-2",
        }.items():
            request = copy.deepcopy(self.request)
            request["execution_binding"][field] = value
            with self.subTest(execution_field=field):
                self.assert_error(
                    "CONTROL_LEDGER_CONTEXT_MISMATCH",
                    lambda request=request: self.ledger.begin(request_bytes(request)),
                )
        self.assertEqual([], list(self.root.iterdir()))

    def test_generation_nonce_digest_is_part_of_the_durable_key_and_record(
        self,
    ) -> None:
        alternate = dataclasses.replace(PINS, generation_nonce_sha256=SHA_F)
        original_key = control.derive_control_ledger_key(binding_for(self.request))
        alternate_key = control.derive_control_ledger_key(
            binding_for(self.request, alternate)
        )
        self.assertNotEqual(original_key, alternate_key)

        self.ledger.begin(self.request_bytes)
        prepare_name, _result_name = entry_names(self.request)
        record = json.loads((self.root / prepare_name).read_text(encoding="utf-8"))
        self.assertEqual(
            PINS.generation_nonce_sha256,
            record["binding"]["pins"]["generation_nonce_sha256"],
        )
        record["binding"]["pins"]["generation_nonce_sha256"] = SHA_F
        (self.root / prepare_name).write_bytes(protocol.canonical_json_bytes(record))
        self.assert_error(
            "CONTROL_LEDGER_ENTRY_CORRUPT",
            lambda: self.ledger.begin(self.request_bytes),
        )

    def test_expired_fresh_control_is_rejected_before_prepare(self) -> None:
        request = make_request(deadline_ns=max(1, time.monotonic_ns() - 1))
        self.assert_error(
            "CONTROL_LEDGER_REQUEST_EXPIRED",
            lambda: self.ledger.begin(request_bytes(request)),
        )
        self.assertEqual([], list(self.root.iterdir()))

    def test_fresh_control_deadline_cannot_exceed_protocol_horizon(self) -> None:
        request = make_request(
            deadline_ns=(
                time.monotonic_ns() + protocol.MAX_DEADLINE_AHEAD_NS + 10_000_000_000
            )
        )
        self.assert_error(
            "CONTROL_LEDGER_DEADLINE_INVALID",
            lambda: self.ledger.begin(request_bytes(request)),
        )
        self.assertEqual([], list(self.root.iterdir()))

    def test_deadline_crossing_after_prepare_never_returns_execute(self) -> None:
        deadline = time.monotonic_ns() + 5_000_000_000
        request = make_request(deadline_ns=deadline)
        with mock.patch.object(
            control.time,
            "monotonic_ns",
            side_effect=(deadline - 1, deadline),
        ):
            outcome = self.ledger.begin(request_bytes(request))
        self.assertEqual("recovery_required", outcome.action)
        self.assertIsNone(outcome.result_bytes)
        self.assertEqual(
            "recovery_required", self.ledger.begin(request_bytes(request)).action
        )

    def test_committed_result_replays_without_reauthorizing_expired_request(
        self,
    ) -> None:
        deadline = time.monotonic_ns() + 5_000_000_000
        request = make_request(deadline_ns=deadline)
        raw_request = request_bytes(request)
        raw_result = result_bytes(make_success_result(request))
        self.ledger.begin(raw_request)
        self.ledger.commit_result(raw_request, raw_result)
        with mock.patch.object(
            control.time, "monotonic_ns", return_value=deadline + 1
        ) as monotonic:
            replay = self.ledger.begin(raw_request)
        self.assertEqual("replay", replay.action)
        self.assertEqual(raw_result, replay.result_bytes)
        monotonic.assert_not_called()

    def test_expired_ordinary_phase_request_cannot_be_recast_as_control(self) -> None:
        ordinary = {
            "schema": protocol.REQUEST_SCHEMA,
            "protocol": protocol.PROTOCOL_REVISION,
            "operation": "caption_exact_render_set",
            "deadline_monotonic_ns": max(1, time.monotonic_ns() - 1),
        }
        self.assert_error(
            "CONTROL_LEDGER_INPUT_INVALID",
            lambda: self.ledger.begin(protocol.canonical_json_bytes(ordinary)),
        )
        self.assertEqual([], list(self.root.iterdir()))

    def test_control_result_is_semantically_validated_before_publication(self) -> None:
        self.ledger.begin(self.request_bytes)
        invalid_results = []

        wrong_binding = copy.deepcopy(self.result)
        wrong_binding["request_binding"]["phase_request_sha256"] = SHA_D
        invalid_results.append(wrong_binding)

        expired = copy.deepcopy(self.result)
        past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)
        expired["receipt"]["issued_at"] = utc_text(past - dt.timedelta(minutes=1))
        expired["receipt"]["expires_at"] = utc_text(past)
        invalid_results.append(expired)

        wrong_flags = copy.deepcopy(self.result)
        wrong_flags["receipt"]["cancelled"] = True
        invalid_results.append(wrong_flags)

        wrong_state = copy.deepcopy(self.result)
        wrong_state["mutation_state"] = "committed"
        invalid_results.append(wrong_state)

        for result in invalid_results:
            with self.subTest(result=result):
                self.assert_error(
                    "CONTROL_LEDGER_RESULT_INVALID",
                    lambda result=result: self.ledger.commit_result(
                        self.request_bytes, result_bytes(result)
                    ),
                )
        _prepare_name, result_name = entry_names(self.request)
        self.assertFalse((self.root / result_name).exists())

    def test_failed_control_result_obeys_closed_mutation_and_retry_rules(self) -> None:
        request = make_request(
            "cancel_phase_work",
            control_key="sha256:" + format(44, "064x"),
        )
        raw_request = request_bytes(request)
        self.ledger.begin(raw_request)
        committed = make_failed_result(request, mutation_state="committed")
        committed["error"]["retryable"] = True
        self.assert_error(
            "CONTROL_LEDGER_RESULT_INVALID",
            lambda: self.ledger.commit_result(raw_request, result_bytes(committed)),
        )
        valid = make_failed_result(request, mutation_state="ambiguous")
        raw_valid = result_bytes(valid)
        self.assertEqual(raw_valid, self.ledger.commit_result(raw_request, raw_valid))

    def test_noncanonical_duplicate_and_float_inputs_fail_without_echo(self) -> None:
        secret = "peer-secret-value"
        invalid_requests = (
            b'{"a":1,"a":2}',
            b'{"a":1.0}',
            b'{ "schema": "simworld-semantic-index-control-request/v1" }',
            protocol.canonical_json_bytes({"secret": secret}),
        )
        for raw in invalid_requests:
            with self.subTest(raw=raw):
                error = self.assert_error(
                    "CONTROL_LEDGER_INPUT_INVALID",
                    lambda raw=raw: self.ledger.begin(raw),
                )
                self.assertNotIn(secret, str(error))
        self.assertEqual([], list(self.root.iterdir()))

    def test_directory_fd_is_owned_cloexec_and_no_path_is_retained(self) -> None:
        owned = self.ledger.fileno()
        self.assertNotEqual(self.directory_fd, owned)
        self.assertTrue(fcntl.fcntl(owned, fcntl.F_GETFD) & fcntl.FD_CLOEXEC)
        self.assertFalse(hasattr(self.ledger, "__dict__"))
        self.assertFalse(
            any(
                "path" in slot.lower()
                for slot in control.DurableControlLedger.__slots__
            )
        )

        self.ledger.close()
        os.fstat(self.directory_fd)
        self.assert_error(
            "CONTROL_LEDGER_CLOSED", lambda: self.ledger.begin(self.request_bytes)
        )

    def test_private_directory_owner_and_mode_are_checked_continuously(self) -> None:
        os.chmod(self.root, 0o750)
        self.assert_error(
            "CONTROL_LEDGER_DIRECTORY_UNSAFE",
            lambda: self.ledger.begin(self.request_bytes),
        )
        os.chmod(self.root, 0o700)
        self.assert_error(
            "CONTROL_LEDGER_DIRECTORY_UNSAFE",
            lambda: control.DurableControlLedger(
                self.directory_fd,
                PINS,
                expected_uid=os.geteuid() + 1,
            ),
        )

    def test_corrupt_noncanonical_symlink_mode_and_hardlink_fail_closed(self) -> None:
        self.ledger.begin(self.request_bytes)
        prepare_name, _result_name = entry_names(self.request)
        prepare_path = self.root / prepare_name
        original = prepare_path.read_bytes()

        prepare_path.write_bytes(original + b"\n")
        self.assert_error(
            "CONTROL_LEDGER_ENTRY_CORRUPT",
            lambda: self.ledger.begin(self.request_bytes),
        )

        prepare_path.write_bytes(original)
        os.chmod(prepare_path, 0o644)
        self.assert_error(
            "CONTROL_LEDGER_ENTRY_UNSAFE",
            lambda: self.ledger.begin(self.request_bytes),
        )

        prepare_path.unlink()
        target = self.root / "target"
        target.write_bytes(original)
        os.chmod(target, 0o600)
        prepare_path.symlink_to(target.name)
        self.assert_error(
            "CONTROL_LEDGER_ENTRY_UNSAFE",
            lambda: self.ledger.begin(self.request_bytes),
        )

        prepare_path.unlink()
        os.link(target, prepare_path)
        self.assert_error(
            "CONTROL_LEDGER_ENTRY_UNSAFE",
            lambda: self.ledger.begin(self.request_bytes),
        )

    def test_prepare_link_visible_after_fsync_fault_is_never_reexecuted(self) -> None:
        failure = control.ControlLedgerError(
            "CONTROL_LEDGER_IO_ERROR",
            control.PUBLIC_ERROR_MESSAGES["CONTROL_LEDGER_IO_ERROR"],
        )
        with mock.patch.object(
            control.DurableControlLedger,
            "_fsync_directory",
            side_effect=failure,
        ):
            self.assert_error(
                "CONTROL_LEDGER_IO_ERROR",
                lambda: self.ledger.begin(self.request_bytes),
            )
        prepare_name, _result_name = entry_names(self.request)
        self.assertTrue((self.root / prepare_name).is_file())
        self.assertEqual(
            "recovery_required", self.ledger.begin(self.request_bytes).action
        )

    def test_result_link_visible_after_fsync_fault_replays_exact(self) -> None:
        self.ledger.begin(self.request_bytes)
        failure = control.ControlLedgerError(
            "CONTROL_LEDGER_IO_ERROR",
            control.PUBLIC_ERROR_MESSAGES["CONTROL_LEDGER_IO_ERROR"],
        )
        with mock.patch.object(
            control.DurableControlLedger,
            "_fsync_directory",
            side_effect=failure,
        ):
            self.assert_error(
                "CONTROL_LEDGER_IO_ERROR",
                lambda: self.ledger.commit_result(
                    self.request_bytes, self.result_bytes
                ),
            )
        _prepare_name, result_name = entry_names(self.request)
        self.assertTrue((self.root / result_name).is_file())
        replay = self.ledger.begin(self.request_bytes)
        self.assertEqual("replay", replay.action)
        self.assertEqual(self.result_bytes, replay.result_bytes)

    def test_concurrent_publish_uses_no_replace(self) -> None:
        other = control.DurableControlLedger(self.directory_fd, PINS)
        barrier = threading.Barrier(2)
        outcomes: list[str] = []
        errors: list[BaseException] = []

        def writer(instance: control.DurableControlLedger) -> None:
            try:
                barrier.wait(timeout=5)
                outcomes.append(instance.begin(self.request_bytes).action)
            except BaseException as error:  # surfaced after both writers join
                errors.append(error)

        threads = [
            threading.Thread(target=writer, args=(instance,))
            for instance in (self.ledger, other)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        other.close()
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual([], errors)
        self.assertEqual(["execute", "recovery_required"], sorted(outcomes))


if __name__ == "__main__":
    unittest.main(verbosity=2)
