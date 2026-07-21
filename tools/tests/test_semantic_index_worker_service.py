from __future__ import annotations

import ast
import copy
import dataclasses
import datetime as dt
import hashlib
import os
import pathlib
import socket
import struct
import sys
import tempfile
import time
import unittest
from unittest import mock


TESTS_DIR = pathlib.Path(__file__).resolve().parent
TOOLS_DIR = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(TOOLS_DIR))

import semantic_index_control_ledger as control_ledger  # noqa: E402
import semantic_index_durable_ledger as durable_ledger  # noqa: E402
import semantic_index_worker_protocol as worker_protocol  # noqa: E402
import semantic_index_worker_service as worker_service  # noqa: E402
from test_semantic_index_control_ledger import (  # noqa: E402
    PINS as CONTROL_PINS,
    make_request as make_ledger_control_request,
    make_success_result as make_control_success_result,
)
from test_semantic_index_worker_protocol import (  # noqa: E402
    LEDGER_IDENTITY,
    LEDGER_REVISION,
    make_control_request,
    make_request,
    make_success_response,
)


def live_success(request: dict) -> dict:
    return make_success_response(request, now=dt.datetime.now(dt.timezone.utc))


class WorkerServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        os.chmod(self.root, 0o700)
        self.directory_fd = os.open(
            self.root,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
        )
        self.ledger = durable_ledger.DurableIdempotencyLedger(
            self.directory_fd,
            LEDGER_IDENTITY,
        )
        self.control_ledger = control_ledger.DurableControlLedger(
            self.directory_fd,
            CONTROL_PINS,
        )

    def tearDown(self) -> None:
        self.control_ledger.close()
        self.ledger.close()
        os.close(self.directory_fd)
        self.temporary.cleanup()

    def execute(
        self,
        request: dict,
        capabilities: tuple[bytes, ...],
        executor,
    ) -> worker_service.WorkerServiceOutcome:
        return worker_service.execute_phase_request(
            request,
            canonical_request=worker_protocol.canonical_json_bytes(request),
            capabilities=capabilities,
            ledger=self.ledger,
            expected_ledger_revision=LEDGER_REVISION,
            executor=executor,
        )

    def assert_protocol_error(self, code: str, callback) -> None:
        with self.assertRaises(worker_protocol.WorkerProtocolError) as caught:
            callback()
        self.assertEqual(code, caught.exception.code)
        self.assertFalse(caught.exception.retryable)

    def test_mutation_executes_once_then_replays_exact_committed_bytes(self) -> None:
        request, capabilities, _policy = make_request(operation="upsert_postgres_exact")
        calls: list[tuple[dict, tuple[bytes, ...]]] = []

        def executor(value, secrets):
            calls.append((value, secrets))
            value["request_id"] = "executor-mutated-copy"
            return live_success(request)

        first = self.execute(request, capabilities, executor)
        second = self.execute(request, capabilities, executor)

        self.assertEqual("executed", first.disposition)
        self.assertEqual("replay", second.disposition)
        self.assertEqual(first.canonical_bytes, second.canonical_bytes)
        self.assertEqual(first.value, second.value)
        self.assertEqual(1, len(calls))
        self.assertEqual(capabilities, calls[0][1])
        self.assertEqual("request-001", request["request_id"])
        self.assertEqual(
            "replay",
            self.ledger.begin(worker_protocol.canonical_json_bytes(request)).action,
        )

    def test_isolated_path_executes_then_replays_without_reforking(self) -> None:
        request, capabilities, _policy = make_request(
            operation="upsert_postgres_exact"
        )

        def executor(value, _secrets):
            return live_success(value)

        first = worker_service.execute_phase_request_isolated(
            request,
            canonical_request=worker_protocol.canonical_json_bytes(request),
            capabilities=capabilities,
            ledger=self.ledger,
            expected_ledger_revision=LEDGER_REVISION,
            executor=executor,
        )
        self.assertEqual("executed", first.disposition)

        with mock.patch.object(
            worker_service,
            "_EXECUTE_ISOLATED",
            side_effect=AssertionError("replay must not fork"),
        ) as execute_isolated:
            second = worker_service.execute_phase_request_isolated(
                request,
                canonical_request=worker_protocol.canonical_json_bytes(request),
                capabilities=capabilities,
                ledger=self.ledger,
                expected_ledger_revision=LEDGER_REVISION,
                executor=executor,
            )
        self.assertEqual("replay", second.disposition)
        self.assertEqual(first.canonical_bytes, second.canonical_bytes)
        execute_isolated.assert_not_called()

    def test_isolated_callback_failure_leaves_recovery_required_prepare(self) -> None:
        request, capabilities, _policy = make_request(
            operation="upsert_qdrant_exact"
        )

        def executor(_value, _secrets):
            raise RuntimeError("must remain inside child")

        self.assert_protocol_error(
            "WORKER_INTERNAL",
            lambda: worker_service.execute_phase_request_isolated(
                request,
                canonical_request=worker_protocol.canonical_json_bytes(request),
                capabilities=capabilities,
                ledger=self.ledger,
                expected_ledger_revision=LEDGER_REVISION,
                executor=executor,
            ),
        )
        self.assertEqual(
            "recovery_required",
            self.ledger.begin(worker_protocol.canonical_json_bytes(request)).action,
        )

    def test_isolated_deadline_kills_callback_and_preserves_prepare(self) -> None:
        request, capabilities, _policy = make_request(
            operation="upsert_postgres_exact",
            deadline_ns=time.monotonic_ns() + 150_000_000,
        )

        def executor(_value, _secrets):
            time.sleep(60)
            raise AssertionError("deadline did not terminate child")

        started = time.monotonic()
        self.assert_protocol_error(
            "WORKER_INTERNAL",
            lambda: worker_service.execute_phase_request_isolated(
                request,
                canonical_request=worker_protocol.canonical_json_bytes(request),
                capabilities=capabilities,
                ledger=self.ledger,
                expected_ledger_revision=LEDGER_REVISION,
                executor=executor,
            ),
        )
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertEqual(
            "recovery_required",
            self.ledger.begin(worker_protocol.canonical_json_bytes(request)).action,
        )

    def test_isolated_control_operations_commit_and_replay_without_reforking(
        self,
    ) -> None:
        for operation in (
            "query_phase_status",
            "recover_phase_receipt",
            "cancel_phase_work",
            "quarantine_generation",
        ):
            with self.subTest(operation=operation):
                request = make_ledger_control_request(operation)
                capabilities = (bytes(range(64)),)

                def executor(value, _secrets):
                    return make_control_success_result(value)

                first = worker_service.execute_control_request_isolated(
                    request,
                    canonical_request=worker_protocol.canonical_json_bytes(request),
                    capabilities=capabilities,
                    ledger=self.control_ledger,
                    expected_pins=CONTROL_PINS,
                    executor=executor,
                )
                self.assertEqual("executed", first.disposition)
                with mock.patch.object(
                    worker_service,
                    "_EXECUTE_ISOLATED",
                    side_effect=AssertionError("control replay must not fork"),
                ) as execute_isolated:
                    second = worker_service.execute_control_request_isolated(
                        request,
                        canonical_request=worker_protocol.canonical_json_bytes(request),
                        capabilities=capabilities,
                        ledger=self.control_ledger,
                        expected_pins=CONTROL_PINS,
                        executor=executor,
                    )
                self.assertEqual("replay", second.disposition)
                self.assertEqual(first.canonical_bytes, second.canonical_bytes)
                execute_isolated.assert_not_called()

    def test_control_service_requires_exact_independent_pins_object(self) -> None:
        request = make_ledger_control_request()
        capabilities = (bytes(range(64)),)
        copied_pins = dataclasses.replace(CONTROL_PINS)

        def executor(value, _secrets):
            return make_control_success_result(value)

        self.assert_protocol_error(
            "WORKER_LEDGER_REVISION_INVALID",
            lambda: worker_service.execute_control_request_isolated(
                request,
                canonical_request=worker_protocol.canonical_json_bytes(request),
                capabilities=capabilities,
                ledger=self.control_ledger,
                expected_pins=copied_pins,
                executor=executor,
            ),
        )
        self.assertEqual([], list(self.root.iterdir()))

    def test_isolated_control_failure_leaves_prepare_and_never_blind_retries(
        self,
    ) -> None:
        request = make_ledger_control_request("cancel_phase_work")
        raw = worker_protocol.canonical_json_bytes(request)
        capabilities = (bytes(range(64)),)

        def executor(_value, _secrets):
            raise RuntimeError("control target failure stays in child")

        self.assert_protocol_error(
            "WORKER_INTERNAL",
            lambda: worker_service.execute_control_request_isolated(
                request,
                canonical_request=raw,
                capabilities=capabilities,
                ledger=self.control_ledger,
                expected_pins=CONTROL_PINS,
                executor=executor,
            ),
        )
        outcome = worker_service.execute_control_request_isolated(
            request,
            canonical_request=raw,
            capabilities=capabilities,
            ledger=self.control_ledger,
            expected_pins=CONTROL_PINS,
            executor=executor,
        )
        self.assertEqual("recovery_required", outcome.disposition)
        self.assertEqual("ambiguous", outcome.value["mutation_state"])
        self.assertEqual("WORKER_LEDGER_CONFLICT", outcome.value["error"]["code"])

    def test_control_capability_transform_is_rejected_before_commit(self) -> None:
        request = make_ledger_control_request("recover_phase_receipt")
        raw = worker_protocol.canonical_json_bytes(request)
        capabilities = (b"control-capability-secret" * 2,)
        request["credential_transport"]["descriptors"][0]["byte_count"] = len(
            capabilities[0]
        )
        raw = worker_protocol.canonical_json_bytes(request)

        def executor(value, _secrets):
            result = make_control_success_result(value)
            result["receipt"]["immutable_phase_receipt_sha256"] = hashlib.sha256(
                capabilities[0]
            ).hexdigest()
            return result

        self.assert_protocol_error(
            "WORKER_RESPONSE_CAPABILITY_LEAK",
            lambda: worker_service.execute_control_request_isolated(
                request,
                canonical_request=raw,
                capabilities=capabilities,
                ledger=self.control_ledger,
                expected_pins=CONTROL_PINS,
                executor=executor,
            ),
        )
        self.assertEqual("recovery_required", self.control_ledger.begin(raw).action)

    def test_isolated_control_session_commits_before_exact_response_send(
        self,
    ) -> None:
        request = make_ledger_control_request("quarantine_generation")
        raw_request = worker_protocol.canonical_json_bytes(request)
        capabilities = (bytes(range(64)),)
        worker_side, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        received = worker_protocol._new_authenticated_received_request(
            value=request,
            canonical_bytes=raw_request,
            capabilities=capabilities,
            connection=worker_side,
            response_deadline_monotonic_ns=request["deadline_monotonic_ns"],
        )
        original_send = worker_protocol.send_committed_worker_response
        send_observations: list[str] = []

        def observed_send(session, canonical_result):
            send_observations.append(self.control_ledger.begin(raw_request).action)
            return original_send(session, canonical_result)

        def phase_executor(_value, _secrets):
            raise AssertionError("control request reached the phase executor")

        def control_executor(value, _secrets):
            return make_control_success_result(value)

        try:
            with mock.patch.object(
                worker_protocol,
                "send_committed_worker_response",
                side_effect=observed_send,
            ):
                outcome = worker_service.serve_received_request_isolated(
                    received,
                    ledger=self.ledger,
                    expected_ledger_revision=LEDGER_REVISION,
                    executor=phase_executor,
                    control_ledger_instance=self.control_ledger,
                    expected_control_pins=CONTROL_PINS,
                    control_executor=control_executor,
                )
            header = peer.recv(4)
            self.assertEqual(4, len(header))
            (length,) = struct.unpack("!I", header)
            body = bytearray()
            while len(body) < length:
                body.extend(peer.recv(length - len(body)))
            self.assertEqual(outcome.canonical_bytes, bytes(body))
            self.assertEqual(b"", peer.recv(1))
        finally:
            received.close()
            peer.close()

        self.assertEqual("executed", outcome.disposition)
        self.assertEqual(["replay"], send_observations)
        self.assertEqual("replay", self.control_ledger.begin(raw_request).action)

    def test_prepared_without_result_is_deterministic_recovery_required(self) -> None:
        request, capabilities, _policy = make_request(operation="upsert_qdrant_exact")
        raw = worker_protocol.canonical_json_bytes(request)
        self.assertEqual("execute", self.ledger.begin(raw).action)
        calls = 0

        def executor(_value, _secrets):
            nonlocal calls
            calls += 1
            return live_success(request)

        first = self.execute(request, capabilities, executor)
        second = self.execute(request, capabilities, executor)

        self.assertEqual("recovery_required", first.disposition)
        self.assertEqual(first.canonical_bytes, second.canonical_bytes)
        self.assertEqual(0, calls)
        self.assertEqual("failed", first.value["status"])
        self.assertEqual("ambiguous", first.value["mutation_state"])
        self.assertEqual("WORKER_LEDGER_CONFLICT", first.value["error"]["code"])
        self.assertFalse(first.value["error"]["retryable"])
        self.assertEqual("recovery_required", self.ledger.begin(raw).action)

    def test_reconcile_recovery_is_non_mutating_and_never_reexecutes(self) -> None:
        request, capabilities, _policy = make_request(
            operation="reconcile_exact_snapshot"
        )
        self.ledger.begin(worker_protocol.canonical_json_bytes(request))
        calls = 0

        def executor(_value, _secrets):
            nonlocal calls
            calls += 1
            return live_success(request)

        outcome = self.execute(request, capabilities, executor)
        self.assertEqual("recovery_required", outcome.disposition)
        self.assertEqual("none", outcome.value["mutation_state"])
        self.assertEqual(0, calls)

    def test_executor_exception_preserves_prepare_and_next_call_does_not_execute(
        self,
    ) -> None:
        request, capabilities, _policy = make_request(operation="upsert_postgres_exact")
        calls = 0

        def exploding(_value, _secrets):
            nonlocal calls
            calls += 1
            raise RuntimeError("must not cross the service boundary")

        self.assert_protocol_error(
            "WORKER_INTERNAL",
            lambda: self.execute(request, capabilities, exploding),
        )
        outcome = self.execute(request, capabilities, exploding)
        self.assertEqual("recovery_required", outcome.disposition)
        self.assertEqual(1, calls)

    def test_commit_exception_preserves_prepare_and_next_call_does_not_execute(
        self,
    ) -> None:
        request, capabilities, _policy = make_request(operation="upsert_postgres_exact")
        calls = 0

        def executor(_value, _secrets):
            nonlocal calls
            calls += 1
            return live_success(request)

        failure = durable_ledger.LedgerError(
            code="LEDGER_IO_ERROR",
            message=durable_ledger.PUBLIC_ERROR_MESSAGES["LEDGER_IO_ERROR"],
        )
        with mock.patch.object(
            worker_service,
            "_LEDGER_COMMIT_RESULT",
            side_effect=failure,
        ):
            with self.assertRaises(durable_ledger.LedgerError) as caught:
                self.execute(request, capabilities, executor)
        self.assertEqual("LEDGER_IO_ERROR", caught.exception.code)

        outcome = self.execute(request, capabilities, executor)
        self.assertEqual("recovery_required", outcome.disposition)
        self.assertEqual(1, calls)

    def test_invalid_executor_result_is_never_committed_or_reexecuted(self) -> None:
        request, capabilities, _policy = make_request(operation="upsert_postgres_exact")
        calls = 0

        def invalid(_value, _secrets):
            nonlocal calls
            calls += 1
            result = live_success(request)
            result["metrics"]["postgres_rows"] = 1
            return result

        self.assert_protocol_error(
            "WORKER_RESPONSE_BINDING_MISMATCH",
            lambda: self.execute(request, capabilities, invalid),
        )
        outcome = self.execute(request, capabilities, invalid)
        self.assertEqual("recovery_required", outcome.disposition)
        self.assertEqual(1, calls)

    def test_capability_leak_is_rejected_before_durable_result_commit(self) -> None:
        request, capabilities, _policy = make_request(operation="upsert_postgres_exact")
        calls = 0

        def leaking(_value, _secrets):
            nonlocal calls
            calls += 1
            result = live_success(request)
            result["artifact"]["artifact_root"]["sha256"] = hashlib.sha256(
                capabilities[0]
            ).hexdigest()
            return result

        self.assert_protocol_error(
            "WORKER_RESPONSE_CAPABILITY_LEAK",
            lambda: self.execute(request, capabilities, leaking),
        )
        self.assertEqual(1, calls)
        self.assertEqual(
            "recovery_required",
            self.ledger.begin(worker_protocol.canonical_json_bytes(request)).action,
        )

    def test_bad_request_and_raw_mismatch_do_not_prepare_or_call_executor(self) -> None:
        request, capabilities, _policy = make_request()
        calls = 0

        def executor(_value, _secrets):
            nonlocal calls
            calls += 1
            return live_success(request)

        malformed = copy.deepcopy(request)
        malformed["unexpected"] = True
        self.assert_protocol_error(
            "WORKER_PROTOCOL_SHAPE_INVALID",
            lambda: worker_service.execute_phase_request(
                malformed,
                canonical_request=worker_protocol.canonical_json_bytes(malformed),
                capabilities=capabilities,
                ledger=self.ledger,
                expected_ledger_revision=LEDGER_REVISION,
                executor=executor,
            ),
        )
        self.assert_protocol_error(
            worker_service.REQUEST_BYTES_INVALID_CODE,
            lambda: worker_service.execute_phase_request(
                request,
                canonical_request=worker_protocol.canonical_json_bytes(request) + b" ",
                capabilities=capabilities,
                ledger=self.ledger,
                expected_ledger_revision=LEDGER_REVISION,
                executor=executor,
            ),
        )
        self.assertEqual(0, calls)
        self.assertEqual([], list(self.root.iterdir()))

    def test_ledger_subclass_cannot_override_the_durability_boundary(self) -> None:
        request, capabilities, _policy = make_request()
        calls = 0

        class BypassLedger(durable_ledger.DurableIdempotencyLedger):
            def begin(self, _canonical_request):
                raise AssertionError("overridden begin must never run")

            def commit_result(self, _canonical_request, _canonical_result):
                raise AssertionError("overridden commit must never run")

        bypass = object.__new__(BypassLedger)

        def executor(_value, _secrets):
            nonlocal calls
            calls += 1
            return live_success(request)

        self.assert_protocol_error(
            "WORKER_LEDGER_INVALID",
            lambda: worker_service.execute_phase_request(
                request,
                canonical_request=worker_protocol.canonical_json_bytes(request),
                capabilities=capabilities,
                ledger=bypass,
                expected_ledger_revision=LEDGER_REVISION,
                executor=executor,
            ),
        )
        self.assertEqual(0, calls)
        self.assertEqual([], list(self.root.iterdir()))

    def test_ledger_instance_or_class_method_override_cannot_bypass_audited_calls(
        self,
    ) -> None:
        request, capabilities, _policy = make_request()
        calls = 0

        with self.assertRaises(AttributeError):
            self.ledger.begin = lambda _raw: None
        with self.assertRaises(AttributeError):
            self.ledger.commit_result = lambda _request, _result: b"{}"

        def executor(_value, _secrets):
            nonlocal calls
            calls += 1
            return live_success(request)

        with (
            mock.patch.object(
                durable_ledger.DurableIdempotencyLedger,
                "begin",
                side_effect=AssertionError("class method drift must not run"),
            ),
            mock.patch.object(
                durable_ledger.DurableIdempotencyLedger,
                "commit_result",
                side_effect=AssertionError("class method drift must not run"),
            ),
        ):
            outcome = self.execute(request, capabilities, executor)
        self.assertEqual("executed", outcome.disposition)
        self.assertEqual(1, calls)

    def test_ledger_identity_and_plan_pinned_revision_fail_before_prepare(self) -> None:
        calls = 0

        def executor(_value, _secrets):
            nonlocal calls
            calls += 1
            raise AssertionError("ledger mismatch reached executor")

        wrong_identity, capabilities, _policy = make_request()
        wrong_identity["idempotency_ledger_identity"] = "worker-ledger:foreign"
        self.assert_protocol_error(
            "WORKER_LEDGER_IDENTITY_MISMATCH",
            lambda: self.execute(wrong_identity, capabilities, executor),
        )

        wrong_revision, capabilities, _policy = make_request()
        with self.assertRaises(worker_protocol.WorkerProtocolError) as caught:
            worker_service.execute_phase_request(
                wrong_revision,
                canonical_request=worker_protocol.canonical_json_bytes(wrong_revision),
                capabilities=capabilities,
                ledger=self.ledger,
                expected_ledger_revision="sha256:" + "f" * 64,
                executor=executor,
            )
        self.assertEqual("WORKER_LEDGER_REVISION_MISMATCH", caught.exception.code)
        self.assertEqual(0, calls)
        self.assertEqual([], list(self.root.iterdir()))

    def test_expected_ledger_revision_requires_an_exact_pinned_digest_string(
        self,
    ) -> None:
        request, capabilities, _policy = make_request()
        calls = 0

        def executor(_value, _secrets):
            nonlocal calls
            calls += 1
            return live_success(request)

        class RevisionSubclass(str):
            pass

        class EqualityBypass:
            def __eq__(self, _other):
                return True

            def __ne__(self, _other):
                return False

        for configured in (
            RevisionSubclass(LEDGER_REVISION),
            EqualityBypass(),
            "sha256:not-a-digest",
        ):
            with self.subTest(configured_type=type(configured).__name__):
                self.assert_protocol_error(
                    "WORKER_LEDGER_REVISION_INVALID",
                    lambda configured=configured: worker_service.execute_phase_request(
                        request,
                        canonical_request=worker_protocol.canonical_json_bytes(request),
                        capabilities=capabilities,
                        ledger=self.ledger,
                        expected_ledger_revision=configured,
                        executor=executor,
                    ),
                )
        self.assertEqual(0, calls)
        self.assertEqual([], list(self.root.iterdir()))

    def test_received_session_is_opaque_exact_noncopyable_and_authenticated(
        self,
    ) -> None:
        request, capabilities, _policy = make_request()
        raw_request = worker_protocol.canonical_json_bytes(request)
        worker_side, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            with self.assertRaises(TypeError):
                worker_protocol.ReceivedWorkerRequest(
                    object(),
                    value=request,
                    canonical_bytes=raw_request,
                    capabilities=capabilities,
                    connection=worker_side,
                    response_deadline_monotonic_ns=request["deadline_monotonic_ns"],
                )
            with self.assertRaises(TypeError):
                class ForgedSubclass(worker_protocol.ReceivedWorkerRequest):
                    pass

            received = worker_protocol._new_authenticated_received_request(
                value=request,
                canonical_bytes=raw_request,
                capabilities=capabilities,
                connection=worker_side,
                response_deadline_monotonic_ns=request["deadline_monotonic_ns"],
            )
            with self.assertRaises(TypeError):
                copy.copy(received)
            received.close()
            self.assertEqual(b"", peer.recv(1))
        finally:
            worker_side.close()
            peer.close()

        forged = object.__new__(worker_protocol.ReceivedWorkerRequest)
        self.assert_protocol_error(
            "WORKER_SESSION_INVALID",
            lambda: worker_service.serve_received_request(
                forged,
                ledger=self.ledger,
                expected_ledger_revision=LEDGER_REVISION,
                executor=lambda _value, _secrets: live_success(request),
            ),
        )
        self.assert_protocol_error(
            "WORKER_SESSION_INVALID",
            lambda: worker_protocol.send_worker_response(forged, {}),
        )
        self.assertEqual([], list(self.root.iterdir()))

    def test_deadline_expiring_during_begin_leaves_prepare_and_skips_executor(
        self,
    ) -> None:
        base = time.monotonic_ns()
        deadline = base + 10
        request, capabilities, _policy = make_request(deadline_ns=deadline)
        calls = 0

        def executor(_value, _secrets):
            nonlocal calls
            calls += 1
            return live_success(request)

        with mock.patch.object(
            worker_service.time,
            "monotonic_ns",
            side_effect=[base, deadline],
        ):
            self.assert_protocol_error(
                "WORKER_DEADLINE_EXCEEDED",
                lambda: self.execute(request, capabilities, executor),
            )
        self.assertEqual(0, calls)
        self.assertEqual(
            "recovery_required",
            self.ledger.begin(worker_protocol.canonical_json_bytes(request)).action,
        )

    def test_deadline_expiring_before_callback_never_starts_mutation(self) -> None:
        base = time.monotonic_ns()
        deadline = base + 10
        request, capabilities, _policy = make_request(deadline_ns=deadline)
        calls = 0

        def executor(_value, _secrets):
            nonlocal calls
            calls += 1
            return live_success(request)

        with mock.patch.object(
            worker_service.time,
            "monotonic_ns",
            side_effect=[base, base, deadline],
        ):
            self.assert_protocol_error(
                "WORKER_DEADLINE_EXCEEDED",
                lambda: self.execute(request, capabilities, executor),
            )
        self.assertEqual(0, calls)
        self.assertEqual(
            "recovery_required",
            self.ledger.begin(worker_protocol.canonical_json_bytes(request)).action,
        )

    def test_control_request_is_explicitly_unsupported_and_never_uses_phase_ledger(
        self,
    ) -> None:
        request, capabilities, _policy = make_control_request()
        worker_side, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        calls = 0

        def executor(_value, _secrets):
            nonlocal calls
            calls += 1
            raise AssertionError("control request reached the phase executor")

        received = worker_protocol._new_authenticated_received_request(
            value=request,
            canonical_bytes=worker_protocol.canonical_json_bytes(request),
            capabilities=capabilities,
            connection=worker_side,
            response_deadline_monotonic_ns=request["deadline_monotonic_ns"],
        )
        try:
            self.assert_protocol_error(
                worker_service.CONTROL_UNSUPPORTED_CODE,
                lambda: worker_service.serve_received_request(
                    received,
                    ledger=self.ledger,
                    expected_ledger_revision=LEDGER_REVISION,
                    executor=executor,
                ),
            )
            self.assertEqual(b"", peer.recv(1))
        finally:
            received.close()
            peer.close()
        self.assertEqual(0, calls)
        self.assertEqual([], list(self.root.iterdir()))

    def test_durable_commit_occurs_before_response_send(self) -> None:
        request, capabilities, _policy = make_request(operation="upsert_postgres_exact")
        raw_request = worker_protocol.canonical_json_bytes(request)
        worker_side, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        received = worker_protocol._new_authenticated_received_request(
            value=request,
            canonical_bytes=raw_request,
            capabilities=capabilities,
            connection=worker_side,
            response_deadline_monotonic_ns=request["deadline_monotonic_ns"],
        )
        original_send = worker_protocol.send_committed_worker_response
        send_observations: list[str] = []

        def observed_send(session, canonical_result):
            send_observations.append(self.ledger.begin(raw_request).action)
            return original_send(session, canonical_result)

        try:
            with mock.patch.object(
                worker_protocol,
                "send_committed_worker_response",
                side_effect=observed_send,
            ):
                outcome = worker_service.serve_received_request(
                    received,
                    ledger=self.ledger,
                    expected_ledger_revision=LEDGER_REVISION,
                    executor=lambda _value, _secrets: live_success(request),
                )
            header = peer.recv(4)
            self.assertEqual(4, len(header))
            (length,) = struct.unpack("!I", header)
            body = bytearray()
            while len(body) < length:
                body.extend(peer.recv(length - len(body)))
            self.assertEqual(outcome.canonical_bytes, bytes(body))
            self.assertEqual(b"", peer.recv(1))
        finally:
            received.close()
            peer.close()
        self.assertEqual(["replay"], send_observations)

    def test_send_failure_after_commit_replays_without_reexecuting(self) -> None:
        request, capabilities, _policy = make_request(operation="upsert_postgres_exact")
        raw_request = worker_protocol.canonical_json_bytes(request)
        worker_side, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        received = worker_protocol._new_authenticated_received_request(
            value=request,
            canonical_bytes=raw_request,
            capabilities=capabilities,
            connection=worker_side,
            response_deadline_monotonic_ns=request["deadline_monotonic_ns"],
        )
        calls = 0

        def executor(_value, _secrets):
            nonlocal calls
            calls += 1
            return live_success(request)

        send_failure = worker_protocol.WorkerProtocolError(
            code="WORKER_SEND_FAILED",
            message="Worker frame transmission failed",
            dependency="worker",
            retryable=False,
            mutation_state="committed",
        )
        try:
            with mock.patch.object(
                worker_protocol,
                "send_committed_worker_response",
                side_effect=send_failure,
            ):
                with self.assertRaises(worker_protocol.WorkerProtocolError) as caught:
                    worker_service.serve_received_request(
                        received,
                        ledger=self.ledger,
                        expected_ledger_revision=LEDGER_REVISION,
                        executor=executor,
                    )
            self.assertEqual("WORKER_SEND_FAILED", caught.exception.code)
            self.assertEqual(b"", peer.recv(1))
        finally:
            received.close()
            peer.close()

        replay = self.execute(request, capabilities, executor)
        self.assertEqual("replay", replay.disposition)
        self.assertEqual(1, calls)

    def test_service_source_has_no_ambient_execution_or_path_surface(self) -> None:
        source_path = TOOLS_DIR / "semantic_index_worker_service.py"
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported_roots = {
            alias.name.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        self.assertTrue(
            {"os", "pathlib", "socket", "subprocess", "urllib", "requests"}.isdisjoint(
                imported_roots
            )
        )
        for forbidden in (
            "__file__",
            "getenv",
            "environ",
            "open(",
            "Path(",
            "sha256(source",
            "http://",
            "https://",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
