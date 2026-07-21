from __future__ import annotations

import array
import ast
import base64
import contextlib
import copy
import dataclasses
import datetime as dt
import fcntl
import hashlib
import inspect
import os
import pathlib
import re
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import quote_from_bytes


TOOLS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

import semantic_index_worker_protocol as protocol  # noqa: E402


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
SHA_F = "f" * 64
HANDOFF_RECEIPT_SHA256 = "9" * 64
SHA_REV_A = "sha256:" + SHA_A
SHA_REV_B = "sha256:" + SHA_B
LEDGER_IDENTITY = "worker-ledger:prod"
LEDGER_REVISION = "sha256:" + SHA_C
GENERATION_ID = "semantic-generation:" + "1" * 64
FIXED_NOW = dt.datetime(2026, 7, 21, 12, 0, 0, tzinfo=dt.timezone.utc)

MEMFD_AVAILABLE = all(
    hasattr(os, name) for name in ("memfd_create", "MFD_CLOEXEC", "MFD_ALLOW_SEALING")
) and all(
    hasattr(fcntl, name)
    for name in (
        "F_ADD_SEALS",
        "F_GET_SEALS",
        "F_SEAL_SEAL",
        "F_SEAL_SHRINK",
        "F_SEAL_GROW",
        "F_SEAL_WRITE",
    )
)
REQUIRES_MEMFD = unittest.skipUnless(
    MEMFD_AVAILABLE,
    "runtime omits Linux memfd sealing APIs; Production fails closed",
)

OPERATIONS = (
    "inspect_exact_assets",
    "render_exact_views",
    "caption_exact_render_set",
    "embed_exact_text_set",
    "upsert_postgres_exact",
    "upsert_qdrant_exact",
    "reconcile_exact_snapshot",
)
CONTROL_OPERATIONS = (
    "query_phase_status",
    "recover_phase_receipt",
    "cancel_phase_work",
    "quarantine_generation",
)
FAILURE_STATES = {
    "inspect_exact_assets": {"none", "ambiguous"},
    "render_exact_views": {"none", "staged", "ambiguous"},
    "caption_exact_render_set": {"none", "committed", "ambiguous"},
    "embed_exact_text_set": {"none", "staged", "ambiguous"},
    "upsert_postgres_exact": {"none", "committed", "ambiguous"},
    "upsert_qdrant_exact": {"none", "staged", "committed", "ambiguous"},
    "reconcile_exact_snapshot": {"none"},
}
REMOTE_ERROR_PAIRS = {
    ("worker", "WORKER_BUSY"),
    ("worker", "WORKER_INTERNAL"),
    ("worker", "WORKER_LEDGER_CONFLICT"),
    ("worker", "WORKER_CANCELLED"),
    ("unreal", "UNREAL_UNAVAILABLE"),
    ("unreal", "UNREAL_OPERATION_FAILED"),
    ("caption", "CAPTION_UNAVAILABLE"),
    ("caption", "CAPTION_BUDGET_EXCEEDED"),
    ("caption", "CAPTION_RESULT_INVALID"),
    ("caption", "CAPTION_STATE_AMBIGUOUS"),
    ("embedding", "EMBEDDING_UNAVAILABLE"),
    ("embedding", "EMBEDDING_RESULT_INVALID"),
    ("postgres", "POSTGRES_UNAVAILABLE"),
    ("postgres", "POSTGRES_WRITE_FAILED"),
    ("qdrant", "QDRANT_UNAVAILABLE"),
    ("qdrant", "QDRANT_WRITE_FAILED"),
    ("evidence_store", "EVIDENCE_STORE_UNAVAILABLE"),
    ("evidence_store", "EVIDENCE_PUBLICATION_FAILED"),
}


def capabilities_for(operation: str) -> tuple[bytes, ...]:
    count = len(protocol.operation_contract(operation)["scopes"])
    return tuple(
        f"capability-{operation}-{index:02d}".encode("ascii") for index in range(count)
    )


def _target_for(component: str, scope: str, index: int) -> dict:
    return {
        "component": component,
        "scope": scope,
        "credential_generation": "sha256:" + format(index + 2, "064x"),
        "generation_id": GENERATION_ID,
        "target_identity": f"target-{component}-{index}",
        "runtime_identity": f"runtime-{component}-r1",
        "runtime_image": (
            None
            if component == "caption"
            else f"registry.local/{component}@sha256:" + format(index + 3, "064x")
        ),
        "whoami_sha256": format(index + 4, "064x"),
        "runtime_attestation_sha256": format(index + 5, "064x"),
    }


def make_request(
    client: socket.socket | None = None,
    *,
    operation: str = "inspect_exact_assets",
    deadline_ns: int | None = None,
    worker_pid: int | None = None,
) -> tuple[dict, tuple[bytes, ...], protocol.PeerPolicy | None]:
    contract = protocol.operation_contract(operation)
    capabilities = capabilities_for(operation)
    targets = [
        _target_for(component, scope, index)
        for index, (component, scope) in enumerate(
            zip(contract["components"], contract["scopes"])
        )
    ]
    descriptors = [
        {
            "fd_index": index,
            "scope": scope,
            "credential_generation": targets[index]["credential_generation"],
            "generation_id": GENERATION_ID,
            "target_identity": targets[index]["target_identity"],
            "byte_count": len(capabilities[index]),
        }
        for index, scope in enumerate(contract["scopes"])
    ]
    boot = protocol.current_host_boot_id_sha256()
    peer_pid = worker_pid or os.getpid()
    start_ticks = protocol.process_start_time_ticks(peer_pid)
    if client is None:
        socket_device, socket_inode = 1, 1
        policy = None
    else:
        metadata = os.fstat(client.fileno())
        socket_device, socket_inode = metadata.st_dev, metadata.st_ino
        policy = protocol.PeerPolicy(
            expected_uid=os.getuid(),
            expected_gid=os.getgid(),
            expected_pid=peer_pid,
            expected_process_start_time_ticks=start_ticks,
            expected_socket_device=socket_device,
            expected_socket_inode=socket_inode,
            host_boot_id_sha256=boot,
            peer_attestation_sha256=SHA_E,
        )
    metrics_by_operation = {
        "inspect_exact_assets": {"assets_inspected": 2},
        "render_exact_views": {"assets_rendered": 2, "rendered_views": 6},
        "caption_exact_render_set": {"catalog_records": 2},
        "embed_exact_text_set": {"dense_vectors": 2, "sparse_vectors": 2},
        "upsert_postgres_exact": {"postgres_rows": 2},
        "upsert_qdrant_exact": {"qdrant_points": 2},
        "reconcile_exact_snapshot": {
            "catalog_records": 2,
            "postgres_rows": 2,
            "qdrant_points": 2,
        },
    }
    request = {
        "schema": protocol.REQUEST_SCHEMA,
        "protocol": protocol.PROTOCOL_REVISION,
        "operation": operation,
        "operation_revision": contract["revision"],
        "phase": contract["phase"],
        "request_id": "request-001",
        "job_revision": SHA_REV_A,
        "reviewed_job_sha256": SHA_A,
        "approval_basis_sha256": SHA_B,
        "execution_plan_sha256": SHA_C,
        "launcher_handoff_receipt_sha256": HANDOFF_RECEIPT_SHA256,
        "generation_id": GENERATION_ID,
        "generation_binding_sha256": SHA_D,
        "execution_binding": {
            "run_id": "semantic-index-run:run-001",
            "correlation_id": "semantic-index-correlation:correlation-001",
            "owner_identity": "owner-ci",
            "lease_id": "lease-001",
            "slot_id": "gpu-slot-0",
        },
        "worker_binding": {
            "deployment_identity": "semantic-worker-r1",
            "runtime_image": "registry.local/semantic-worker@sha256:" + SHA_D,
            "whoami_sha256": SHA_A,
            "peer_attestation_sha256": SHA_E,
            "runtime_attestation_sha256": SHA_F,
            "host_boot_id_sha256": boot,
            "peer_uid": os.getuid(),
            "peer_gid": os.getgid(),
            "peer_pid": peer_pid,
            "process_start_time_ticks": start_ticks,
            "process_start_token_sha256": protocol.process_start_token_sha256(
                boot, peer_pid, start_ticks
            ),
            "socket_device": socket_device,
            "socket_inode": socket_inode,
            "socket_inode_binding_sha256": protocol.socket_inode_binding_sha256(
                boot, peer_pid, socket_device, socket_inode
            ),
        },
        "input_binding": {
            "schema": "simworld-semantic-index-input/v1",
            "sha256": SHA_D,
            "item_count": 2,
            "byte_count": 1234,
        },
        "expected_targets": targets,
        "expected_metrics": metrics_by_operation[operation],
        "credential_transport": {
            "kind": protocol.CREDENTIAL_TRANSPORT,
            "json_contains_credential_bytes": False,
            "descriptors": descriptors,
        },
        "idempotency_key": SHA_REV_B,
        "idempotency_ledger_identity": LEDGER_IDENTITY,
        "idempotency_ledger_revision": LEDGER_REVISION,
        "deadline_monotonic_ns": deadline_ns or time.monotonic_ns() + 3_000_000_000,
    }
    return request, capabilities, policy


def make_success_response(request: dict, *, now: dt.datetime = FIXED_NOW) -> dict:
    contract = protocol.operation_contract(request["operation"])
    binding = protocol.request_binding_for(request)
    worker = request["worker_binding"]
    artifact = {
        "schema": protocol.ARTIFACT_ROOT_SCHEMA,
        "protocol": protocol.PROTOCOL_REVISION,
        "phase": request["phase"],
        "operation": request["operation"],
        "request_sha256": binding["request_sha256"],
        "job_revision": request["job_revision"],
        "reviewed_job_sha256": request["reviewed_job_sha256"],
        "approval_basis_sha256": request["approval_basis_sha256"],
        "execution_plan_sha256": request["execution_plan_sha256"],
        "generation_id": request["generation_id"],
        "generation_binding_sha256": request["generation_binding_sha256"],
        "idempotency_key": request["idempotency_key"],
        "idempotency_ledger_identity": request["idempotency_ledger_identity"],
        "idempotency_ledger_revision": request["idempotency_ledger_revision"],
        "input_artifact_sha256": request["input_binding"]["sha256"],
        "artifact_root": {
            "schema": contract["artifact_schema"],
            "sha256": SHA_A,
            "byte_count": 512,
            "chunk_count": 1,
            "chunks_sha256": SHA_B,
        },
        "observed_targets": copy.deepcopy(request["expected_targets"]),
        "worker_attestation": {
            "deployment_identity": worker["deployment_identity"],
            "runtime_image": worker["runtime_image"],
            "whoami_sha256": worker["whoami_sha256"],
            "peer_attestation_sha256": worker["peer_attestation_sha256"],
            "runtime_attestation_sha256": worker["runtime_attestation_sha256"],
            "host_boot_id_sha256": worker["host_boot_id_sha256"],
            "process_start_token_sha256": worker["process_start_token_sha256"],
            "socket_inode_binding_sha256": worker["socket_inode_binding_sha256"],
        },
        "issued_at": (now - dt.timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
        "expires_at": (now + dt.timedelta(minutes=5))
        .isoformat()
        .replace("+00:00", "Z"),
        "serialization_contract": {
            "encoding": "utf-8",
            "canonicalization": "rfc8785-jcs-v1",
            "duplicate_keys": "reject",
            "unicode_normalization": "require_already_nfc",
            "numbers": "integers_only",
            "nonfinite_numbers": "reject",
            "trailing_newline": False,
        },
    }
    return {
        "schema": protocol.RESULT_SCHEMA,
        "protocol": protocol.PROTOCOL_REVISION,
        "request_binding": binding,
        "status": "succeeded",
        "mutation_state": contract["success_mutation_state"],
        "metrics": copy.deepcopy(request["expected_metrics"]),
        "artifact": artifact,
        "error": None,
    }


def make_failed_response(
    request: dict,
    *,
    dependency: str = "worker",
    code: str = "WORKER_BUSY",
    mutation_state: str = "none",
    retryable: bool = True,
) -> dict:
    return {
        "schema": protocol.RESULT_SCHEMA,
        "protocol": protocol.PROTOCOL_REVISION,
        "request_binding": protocol.request_binding_for(request),
        "status": "failed",
        "mutation_state": mutation_state,
        "metrics": {key: 0 for key in request["expected_metrics"]},
        "artifact": None,
        "error": {
            "dependency": dependency,
            "code": code,
            "retryable": retryable,
            "mutation_state": mutation_state,
        },
    }


def make_control_request(
    client: socket.socket | None = None,
    *,
    operation: str = "query_phase_status",
    deadline_ns: int | None = None,
    worker_pid: int | None = None,
) -> tuple[dict, tuple[bytes, ...], protocol.PeerPolicy | None]:
    ordinary, _ordinary_capabilities, policy = make_request(
        client,
        deadline_ns=deadline_ns,
        worker_pid=worker_pid,
    )
    contract = protocol.control_operation_contract(operation)
    capability = f"control-capability-{operation}".encode("ascii")
    request = {
        "schema": protocol.CONTROL_REQUEST_SCHEMA,
        "protocol": protocol.PROTOCOL_REVISION,
        "operation": operation,
        "operation_revision": contract["revision"],
        "request_id": "control-request-001",
        "job_revision": ordinary["job_revision"],
        "reviewed_job_sha256": ordinary["reviewed_job_sha256"],
        "approval_basis_sha256": ordinary["approval_basis_sha256"],
        "execution_plan_sha256": ordinary["execution_plan_sha256"],
        "launcher_handoff_receipt_sha256": ordinary[
            "launcher_handoff_receipt_sha256"
        ],
        "generation_id": ordinary["generation_id"],
        "generation_binding_sha256": ordinary["generation_binding_sha256"],
        "execution_binding": copy.deepcopy(ordinary["execution_binding"]),
        "worker_binding": copy.deepcopy(ordinary["worker_binding"]),
        "target_phase": "qdrant",
        "phase_request_sha256": SHA_F,
        "phase_idempotency_key": SHA_REV_B,
        "control_idempotency_key": "sha256:" + SHA_C,
        "idempotency_ledger_identity": ordinary[
            "idempotency_ledger_identity"
        ],
        "idempotency_ledger_revision": ordinary[
            "idempotency_ledger_revision"
        ],
        "credential_transport": {
            "kind": protocol.CREDENTIAL_TRANSPORT,
            "json_contains_credential_bytes": False,
            "descriptors": [
                {
                    "fd_index": 0,
                    "scope": contract["scope"],
                    "credential_generation": "sha256:" + SHA_D,
                    "generation_id": GENERATION_ID,
                    "target_identity": ordinary["worker_binding"][
                        "deployment_identity"
                    ],
                    "byte_count": len(capability),
                }
            ],
        },
        "deadline_monotonic_ns": ordinary["deadline_monotonic_ns"],
    }
    return request, (capability,), policy


def make_control_success_response(
    request: dict,
    *,
    now: dt.datetime = FIXED_NOW,
) -> dict:
    operation = request["operation"]
    contract = protocol.control_operation_contract(operation)
    return {
        "schema": protocol.CONTROL_RESULT_SCHEMA,
        "protocol": protocol.PROTOCOL_REVISION,
        "request_binding": protocol.control_request_binding_for(request),
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
            "immutable_phase_receipt_sha256": (
                SHA_A if operation == "recover_phase_receipt" else None
            ),
            "cancelled": operation == "cancel_phase_work",
            "quarantined": operation == "quarantine_generation",
            "issued_at": (now - dt.timedelta(seconds=1))
            .isoformat()
            .replace("+00:00", "Z"),
            "expires_at": (now + dt.timedelta(minutes=5))
            .isoformat()
            .replace("+00:00", "Z"),
        },
        "error": None,
    }


def make_control_failed_response(
    request: dict,
    *,
    mutation_state: str = "none",
    retryable: bool = True,
) -> dict:
    return {
        "schema": protocol.CONTROL_RESULT_SCHEMA,
        "protocol": protocol.PROTOCOL_REVISION,
        "request_binding": protocol.control_request_binding_for(request),
        "status": "failed",
        "mutation_state": mutation_state,
        "receipt": None,
        "error": {
            "dependency": "worker",
            "code": "WORKER_BUSY",
            "retryable": retryable,
            "mutation_state": mutation_state,
        },
    }


def client_policy_for(
    connection: socket.socket,
    *,
    peer_pid: int | None = None,
    launcher_handoff_receipt_sha256: str = HANDOFF_RECEIPT_SHA256,
) -> protocol.ClientPeerPolicy:
    pid = peer_pid or os.getpid()
    metadata = os.fstat(connection.fileno())
    return protocol.ClientPeerPolicy(
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        expected_pid=pid,
        expected_process_start_time_ticks=protocol.process_start_time_ticks(pid),
        expected_socket_device=metadata.st_dev,
        expected_socket_inode=metadata.st_ino,
        host_boot_id_sha256=protocol.current_host_boot_id_sha256(),
        expected_launcher_handoff_receipt_sha256=(
            launcher_handoff_receipt_sha256
        ),
    )


def send_response(received: protocol.ReceivedWorkerRequest, value: dict) -> None:
    protocol.send_worker_response(received, value)


def send_raw_response(
    received: protocol.ReceivedWorkerRequest,
    raw: bytes,
    *,
    trailing: bytes = b"",
    partial_at: int | None = None,
) -> None:
    connection = received._take_response_socket()
    try:
        frame = struct.pack("!I", len(raw)) + raw
        if partial_at is not None:
            connection.sendall(frame[:partial_at])
        else:
            connection.sendall(frame + trailing)
        connection.shutdown(socket.SHUT_WR)
    finally:
        connection.close()


class FakeUnixWorker:
    """A deterministic one-request worker that always removes its UDS."""

    def __init__(self, root: pathlib.Path, responder) -> None:
        self.path = str(root / "worker.sock")
        self.responder = responder
        self.received: protocol.ReceivedWorkerRequest | None = None
        self.error: BaseException | None = None
        self.done = threading.Event()
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(self.path)
        self.listener.listen(1)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def connect(self) -> socket.socket:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(self.path)
        return client

    def _run(self) -> None:
        try:
            connection, _ = self.listener.accept()
            policy = client_policy_for(connection)
            self.received = protocol.receive_worker_request(
                connection,
                handshake_deadline_monotonic_ns=time.monotonic_ns() + 2_000_000_000,
                client_peer_policy=policy,
            )
            self.responder(self.received)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except BaseException as error:
            self.error = error
        finally:
            if self.received is not None:
                self.received.close()
            self.done.set()

    def close(self, *, allow_error: bool = False) -> None:
        self.listener.close()
        self.done.wait(2)
        self.thread.join(timeout=2)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.path)
        if self.thread.is_alive():
            raise AssertionError("fake worker thread did not terminate")
        if self.error is not None and not allow_error:
            raise self.error

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close(allow_error=exc_type is not None)


class WorkerProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.root.chmod(0o700)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def assert_protocol_error(
        self, code: str, callback
    ) -> protocol.WorkerProtocolError:
        with self.assertRaises(protocol.WorkerProtocolError) as captured:
            callback()
        self.assertEqual(captured.exception.code, code)
        return captured.exception

    @staticmethod
    def live_success(request: dict) -> dict:
        return make_success_response(request, now=dt.datetime.now(dt.timezone.utc))

    @REQUIRES_MEMFD
    def test_happy_path_uses_preconnected_socket_and_receiver_helper(self) -> None:
        deadline = time.monotonic_ns() + 3_000_000_000
        with FakeUnixWorker(
            self.root,
            lambda received: send_response(received, self.live_success(received.value)),
        ) as worker:
            client = worker.connect()
            request, capabilities, policy = make_request(client, deadline_ns=deadline)
            response = protocol.call_worker(
                request,
                capabilities=capabilities,
                launcher_handoff_receipt_sha256=HANDOFF_RECEIPT_SHA256,
                peer_policy=policy,
                connected_socket=client,
            )
            self.assertEqual(client.fileno(), -1)
        self.assertEqual(response.value["status"], "succeeded")
        self.assertEqual(
            response.canonical_bytes, protocol.canonical_json_bytes(response.value)
        )
        self.assertEqual(worker.received.value, request)
        self.assertEqual(worker.received.capabilities, capabilities)
        for capability in capabilities:
            self.assertNotIn(capability, worker.received.canonical_bytes)

    @REQUIRES_MEMFD
    def test_reconcile_transfers_exactly_three_ordered_capabilities(self) -> None:
        deadline = time.monotonic_ns() + 3_000_000_000
        operation = "reconcile_exact_snapshot"
        with FakeUnixWorker(
            self.root,
            lambda received: send_response(received, self.live_success(received.value)),
        ) as worker:
            client = worker.connect()
            request, capabilities, policy = make_request(
                client, operation=operation, deadline_ns=deadline
            )
            descriptor = client.detach()
            response = protocol.call_worker(
                request,
                capabilities=capabilities,
                launcher_handoff_receipt_sha256=HANDOFF_RECEIPT_SHA256,
                peer_policy=policy,
                connected_socket=descriptor,
            )
            with self.assertRaises(OSError):
                os.fstat(descriptor)
        self.assertEqual(worker.received.capabilities, capabilities)
        self.assertEqual(
            [
                item["scope"]
                for item in worker.received.value["credential_transport"]["descriptors"]
            ],
            [
                "postgres_generation_read_only",
                "qdrant_generation_read_only",
                "ue_runtime_read_only",
            ],
        )
        self.assertEqual(response.value["mutation_state"], "none")

    @REQUIRES_MEMFD
    def test_control_request_uses_same_one_shot_transport_without_command_surface(
        self,
    ) -> None:
        deadline = time.monotonic_ns() + 3_000_000_000
        with FakeUnixWorker(
            self.root,
            lambda received: send_response(
                received,
                make_control_success_response(
                    received.value,
                    now=dt.datetime.now(dt.timezone.utc),
                ),
            ),
        ) as worker:
            client = worker.connect()
            request, capabilities, policy = make_control_request(
                client,
                operation="recover_phase_receipt",
                deadline_ns=deadline,
            )
            response = protocol.call_worker(
                request,
                capabilities=capabilities,
                launcher_handoff_receipt_sha256=HANDOFF_RECEIPT_SHA256,
                peer_policy=policy,
                connected_socket=client,
            )
        self.assertEqual(response.value["status"], "succeeded")
        self.assertEqual(
            response.value["receipt"]["phase_request_sha256"],
            request["phase_request_sha256"],
        )

    @unittest.skipUnless(hasattr(os, "fork"), "requires fork and Linux SO_PEERCRED")
    @REQUIRES_MEMFD
    def test_cross_process_fake_worker_enforces_both_peer_policies(self) -> None:
        path = str(self.root / "fork-worker.sock")
        ready_read, ready_write = os.pipe()
        report_read, report_write = os.pipe()
        parent_pid = os.getpid()
        child_pid = os.fork()
        if child_pid == 0:
            os.close(ready_read)
            os.close(report_read)
            listener = None
            exit_code = 1
            try:
                listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                listener.bind(path)
                listener.listen(1)
                os.write(ready_write, b"R")
                connection, _address = listener.accept()
                received = protocol.receive_worker_request(
                    connection,
                    handshake_deadline_monotonic_ns=time.monotonic_ns() + 2_000_000_000,
                    client_peer_policy=client_policy_for(
                        connection,
                        peer_pid=parent_pid,
                    ),
                )
                protocol.send_worker_response(
                    received,
                    make_success_response(
                        received.value,
                        now=dt.datetime.now(dt.timezone.utc),
                    ),
                )
                exit_code = 0
            except BaseException as error:
                public = getattr(error, "code", type(error).__name__)
                with contextlib.suppress(OSError):
                    os.write(report_write, str(public).encode("ascii", "replace")[:120])
            finally:
                if listener is not None:
                    listener.close()
                os.close(ready_write)
                os.close(report_write)
                os._exit(exit_code)

        os.close(ready_write)
        os.close(report_write)
        client = None
        response = None
        status = None
        report = b""
        try:
            self.assertEqual(os.read(ready_read, 1), b"R")
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(path)
            deadline = time.monotonic_ns() + 3_000_000_000
            request, capabilities, policy = make_request(
                client,
                deadline_ns=deadline,
                worker_pid=child_pid,
            )
            response = protocol.call_worker(
                request,
                capabilities=capabilities,
                launcher_handoff_receipt_sha256=HANDOFF_RECEIPT_SHA256,
                peer_policy=policy,
                connected_socket=client,
            )
        finally:
            os.close(ready_read)
            _waited_pid, status = os.waitpid(child_pid, 0)
            report = os.read(report_read, 256)
            os.close(report_read)
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)
            if client is not None:
                client.close()
        self.assertTrue(os.WIFEXITED(status), report.decode("ascii", "replace"))
        self.assertEqual(os.WEXITSTATUS(status), 0, report.decode("ascii", "replace"))
        self.assertEqual(response.value["status"], "succeeded")

    def test_all_operations_use_v2_generation_scopes_artifacts_and_mutation_states(
        self,
    ) -> None:
        expected_scopes = {
            "inspect_exact_assets": ["ue_disposable_scene_inspect_spawn_cleanup"],
            "render_exact_views": ["ue_disposable_scene_render_staging_cleanup"],
            "caption_exact_render_set": ["caption_bounded_idempotent_call"],
            "embed_exact_text_set": ["embedding_immutable_vector_staging"],
            "upsert_postgres_exact": ["postgres_generation_write"],
            "upsert_qdrant_exact": ["qdrant_generation_write"],
            "reconcile_exact_snapshot": [
                "postgres_generation_read_only",
                "qdrant_generation_read_only",
                "ue_runtime_read_only",
            ],
        }
        for operation in OPERATIONS:
            with self.subTest(operation=operation):
                request, _capabilities, _policy = make_request(operation=operation)
                validated = protocol.validate_request(request)
                contract = protocol.operation_contract(operation)
                self.assertEqual(validated["generation_id"], GENERATION_ID)
                self.assertIsInstance(validated["execution_binding"]["slot_id"], str)
                self.assertEqual(
                    [
                        item["scope"]
                        for item in validated["credential_transport"]["descriptors"]
                    ],
                    expected_scopes[operation],
                )
                response = make_success_response(validated)
                protocol.validate_response(response, validated, now_utc=FIXED_NOW)
                self.assertEqual(
                    response["mutation_state"], contract["success_mutation_state"]
                )
                self.assertEqual(
                    response["artifact"]["artifact_root"]["schema"],
                    contract["artifact_schema"],
                )

    def test_exact_seven_phase_failure_and_retry_matrix_is_closed(self) -> None:
        all_states = {"none", "staged", "committed", "ambiguous"}
        for operation in OPERATIONS:
            request, _capabilities, _policy = make_request(operation=operation)
            contract = protocol.operation_contract(operation)
            self.assertEqual(
                set(contract["failure_mutation_states"]),
                FAILURE_STATES[operation],
            )
            for mutation_state in all_states:
                for retryable in (False, True):
                    response = make_failed_response(
                        request,
                        mutation_state=mutation_state,
                        retryable=retryable,
                    )
                    legal = mutation_state in FAILURE_STATES[operation] and (
                        mutation_state == "none" or not retryable
                    )
                    with self.subTest(
                        operation=operation,
                        mutation_state=mutation_state,
                        retryable=retryable,
                    ):
                        if legal:
                            protocol.validate_response(response, request)
                        else:
                            with self.assertRaises(protocol.WorkerProtocolError):
                                protocol.validate_response(response, request)

    def test_every_remote_error_pair_has_closed_dependency_state_retry_combinations(
        self,
    ) -> None:
        self.assertEqual(set(protocol._REMOTE_ERRORS), REMOTE_ERROR_PAIRS)
        for operation in OPERATIONS:
            request, _capabilities, _policy = make_request(operation=operation)
            allowed_dependencies = set(
                protocol.operation_contract(operation)["error_dependencies"]
            )
            for dependency, code in sorted(REMOTE_ERROR_PAIRS):
                response = make_failed_response(
                    request,
                    dependency=dependency,
                    code=code,
                    mutation_state="none",
                    retryable=False,
                )
                with self.subTest(
                    operation=operation,
                    dependency=dependency,
                    code=code,
                ):
                    if dependency in allowed_dependencies:
                        protocol.validate_response(response, request)
                    else:
                        self.assert_protocol_error(
                            "WORKER_ERROR_INVALID",
                            lambda response=response, request=request: (
                                protocol.validate_response(response, request)
                            ),
                        )

        qdrant, _capabilities, _policy = make_request(operation="upsert_qdrant_exact")
        for dependency, code in sorted(REMOTE_ERROR_PAIRS):
            if dependency not in {"worker", "qdrant", "evidence_store"}:
                continue
            for state in ("staged", "committed", "ambiguous"):
                response = make_failed_response(
                    qdrant,
                    dependency=dependency,
                    code=code,
                    mutation_state=state,
                    retryable=True,
                )
                with self.subTest(dependency=dependency, code=code, state=state):
                    self.assert_protocol_error(
                        "WORKER_ERROR_INVALID",
                        lambda response=response: protocol.validate_response(
                            response, qdrant
                        ),
                    )

    def test_peer_policy_checks_exact_pid_start_boot_and_socket(self) -> None:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        path = str(self.root / "peer.sock")
        listener.bind(path)
        listener.listen(1)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(path)
        accepted, _ = listener.accept()
        try:
            request, _capabilities, policy = make_request(client)
            protocol.verify_peer(client, policy, request)
            cases = (
                ("expected_uid", os.getuid() + 1),
                ("expected_gid", os.getgid() + 1),
                ("expected_pid", os.getpid() + 1),
                (
                    "expected_process_start_time_ticks",
                    policy.expected_process_start_time_ticks + 1,
                ),
                ("expected_socket_inode", policy.expected_socket_inode + 1),
                ("host_boot_id_sha256", SHA_A),
            )
            for field, value in cases:
                with self.subTest(field=field):
                    changed = dataclasses.replace(policy, **{field: value})
                    self.assert_protocol_error(
                        "WORKER_PEER_ATTESTATION_MISMATCH",
                        lambda changed=changed: protocol.verify_peer(
                            client, changed, request
                        ),
                    )
            client_policy = client_policy_for(accepted)
            protocol.verify_client_peer(accepted, client_policy)
            client_cases = (
                ("expected_uid", os.getuid() + 1, "WORKER_PEER_CREDENTIAL_MISMATCH"),
                ("expected_gid", os.getgid() + 1, "WORKER_PEER_CREDENTIAL_MISMATCH"),
                ("expected_pid", os.getpid() + 1, "WORKER_PEER_CREDENTIAL_MISMATCH"),
                (
                    "expected_process_start_time_ticks",
                    client_policy.expected_process_start_time_ticks + 1,
                    "WORKER_PROCESS_START_MISMATCH",
                ),
                (
                    "expected_socket_inode",
                    client_policy.expected_socket_inode + 1,
                    "WORKER_SOCKET_BINDING_MISMATCH",
                ),
                ("host_boot_id_sha256", SHA_A, "WORKER_HOST_BOOT_MISMATCH"),
            )
            for field, value, code in client_cases:
                with self.subTest(client_field=field):
                    changed = dataclasses.replace(client_policy, **{field: value})
                    self.assert_protocol_error(
                        code,
                        lambda changed=changed: protocol.verify_client_peer(
                            accepted, changed
                        ),
                    )
        finally:
            accepted.close()
            client.close()
            listener.close()

    def test_receiver_uses_worker_owned_handshake_deadline_and_exact_client_policy(
        self,
    ) -> None:
        receiver, sender = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        policy = client_policy_for(receiver)
        wrong = dataclasses.replace(
            policy,
            expected_socket_inode=policy.expected_socket_inode + 1,
        )
        self.assert_protocol_error(
            "WORKER_SOCKET_BINDING_MISMATCH",
            lambda: protocol.receive_worker_request(
                receiver,
                handshake_deadline_monotonic_ns=time.monotonic_ns() + 500_000_000,
                client_peer_policy=wrong,
            ),
        )
        self.assertEqual(receiver.fileno(), -1)
        sender.close()

        if hasattr(socket, "SOCK_SEQPACKET"):
            left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            try:
                self.assert_protocol_error(
                    "WORKER_SOCKET_INVALID",
                    lambda: protocol._require_unix_stream(left),
                )
            finally:
                left.close()
                right.close()

        receiver, sender = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        policy = client_policy_for(receiver)
        self.assert_protocol_error(
            "WORKER_DEADLINE_INVALID",
            lambda: protocol.receive_worker_request(
                receiver,
                handshake_deadline_monotonic_ns=time.monotonic_ns()
                + protocol.MAX_HANDSHAKE_AHEAD_NS
                + 1_000_000_000,
                client_peer_policy=policy,
            ),
        )
        self.assertEqual(receiver.fileno(), -1)
        sender.close()

    def test_protocol_exposes_no_path_or_attestation_bypass(self) -> None:
        signature = inspect.signature(protocol.call_worker)
        self.assertNotIn("socket_path", signature.parameters)
        self.assertNotIn("allow_unattested_for_test", signature.parameters)
        peer_fields = {field.name for field in dataclasses.fields(protocol.PeerPolicy)}
        self.assertNotIn("allow_unattested_for_test", peer_fields)
        client_peer_fields = {
            field.name for field in dataclasses.fields(protocol.ClientPeerPolicy)
        }
        self.assertNotIn("allow_unattested_for_test", client_peer_fields)
        self.assertIn(
            "expected_launcher_handoff_receipt_sha256",
            client_peer_fields,
        )
        handoff_parameter = signature.parameters[
            "launcher_handoff_receipt_sha256"
        ]
        self.assertIs(handoff_parameter.default, inspect.Parameter.empty)
        receiver_signature = inspect.signature(protocol.receive_worker_request)
        self.assertIn("handshake_deadline_monotonic_ns", receiver_signature.parameters)
        self.assertNotIn("deadline_monotonic_ns", receiver_signature.parameters)
        source = (TOOLS_DIR / "semantic_index_worker_protocol.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("connect(", source)
        self.assertNotIn("socket_path_for_test", source)
        self.assertNotIn("os.dup(", source)
        self.assertNotIn("settimeout(", source)
        self.assertNotIn("setblocking(", source)

    @REQUIRES_MEMFD
    def test_oversized_and_truncated_response_frames_fail_bounded(self) -> None:
        def run(responder, expected_code):
            deadline = time.monotonic_ns() + 3_000_000_000
            with FakeUnixWorker(self.root, responder) as worker:
                client = worker.connect()
                request, capabilities, policy = make_request(
                    client, deadline_ns=deadline
                )
                error = self.assert_protocol_error(
                    expected_code,
                    lambda: protocol.call_worker(
                        request,
                        capabilities=capabilities,
                        launcher_handoff_receipt_sha256=HANDOFF_RECEIPT_SHA256,
                        peer_policy=policy,
                        connected_socket=client,
                    ),
                )
            self.assertEqual(error.mutation_state, "ambiguous")

        def oversized(received):
            connection = received._take_response_socket()
            try:
                connection.sendall(struct.pack("!I", protocol.MAX_FRAME_BYTES + 1))
                connection.shutdown(socket.SHUT_WR)
            finally:
                connection.close()

        run(oversized, "WORKER_FRAME_SIZE_INVALID")

        def truncated(received):
            raw = protocol.canonical_json_bytes(self.live_success(received.value))
            send_raw_response(received, raw, partial_at=15)

        run(truncated, "WORKER_FRAME_TRUNCATED")

    @REQUIRES_MEMFD
    def test_trickle_response_cannot_extend_absolute_deadline(self) -> None:
        def trickle(received):
            raw = protocol.canonical_json_bytes(self.live_success(received.value))
            frame = struct.pack("!I", len(raw)) + raw
            connection = received._take_response_socket()
            try:
                for byte in frame:
                    connection.send(bytes([byte]))
                    time.sleep(0.025)
                connection.shutdown(socket.SHUT_WR)
            finally:
                connection.close()

        deadline = time.monotonic_ns() + 180_000_000
        with FakeUnixWorker(self.root, trickle) as worker:
            client = worker.connect()
            request, capabilities, policy = make_request(client, deadline_ns=deadline)
            started = time.monotonic()
            error = self.assert_protocol_error(
                "WORKER_DEADLINE_EXCEEDED",
                lambda: protocol.call_worker(
                    request,
                    capabilities=capabilities,
                    launcher_handoff_receipt_sha256=HANDOFF_RECEIPT_SHA256,
                    peer_policy=policy,
                    connected_socket=client,
                ),
            )
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.8)
        self.assertEqual(error.mutation_state, "ambiguous")

    def test_bounded_sender_completes_deterministic_partial_sends(self) -> None:
        sender, receiver = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        payload = b"p" * (256 * 1024)
        collected = bytearray()

        class PartialSocket:
            def __init__(self, underlying: socket.socket) -> None:
                self.underlying = underlying

            def fileno(self) -> int:
                return self.underlying.fileno()

            def send(self, value, flags=0) -> int:
                return self.underlying.send(value[:17], flags)

        def read_all() -> None:
            while True:
                chunk = receiver.recv(4096)
                if not chunk:
                    return
                collected.extend(chunk)

        reader = threading.Thread(target=read_all, daemon=True)
        reader.start()
        before_flags = fcntl.fcntl(sender.fileno(), fcntl.F_GETFL)
        try:
            protocol._send_framed_bytes(
                PartialSocket(sender),
                payload,
                time.monotonic_ns() + 2_000_000_000,
                started_mutation_state="ambiguous",
            )
            protocol._shutdown_write(sender, mutation_state="ambiguous")
            reader.join(timeout=2)
            self.assertFalse(reader.is_alive())
            after_flags = fcntl.fcntl(sender.fileno(), fcntl.F_GETFL)
        finally:
            sender.close()
            receiver.close()
        self.assertEqual(before_flags, after_flags)
        self.assertEqual(collected[:4], struct.pack("!I", len(payload)))
        self.assertEqual(collected[4:], payload)

    @REQUIRES_MEMFD
    def test_call_worker_preserves_shared_nonblocking_flag_and_consumes_socket(
        self,
    ) -> None:
        deadline = time.monotonic_ns() + 3_000_000_000
        with FakeUnixWorker(
            self.root,
            lambda received: send_response(received, self.live_success(received.value)),
        ) as worker:
            client = worker.connect()
            client.setblocking(False)
            observer = os.dup(client.fileno())
            before_flags = fcntl.fcntl(observer, fcntl.F_GETFL)
            try:
                request, capabilities, policy = make_request(
                    client, deadline_ns=deadline
                )
                protocol.call_worker(
                    request,
                    capabilities=capabilities,
                    launcher_handoff_receipt_sha256=HANDOFF_RECEIPT_SHA256,
                    peer_policy=policy,
                    connected_socket=client,
                )
                self.assertEqual(client.fileno(), -1)
                after_flags = fcntl.fcntl(observer, fcntl.F_GETFL)
            finally:
                os.close(observer)
        self.assertEqual(before_flags, after_flags)

    def test_unknown_fields_operations_and_result_fields_are_rejected(self) -> None:
        request, _capabilities, _policy = make_request()
        request["payload"] = {}
        self.assert_protocol_error(
            "WORKER_PROTOCOL_SHAPE_INVALID", lambda: protocol.validate_request(request)
        )
        request, _capabilities, _policy = make_request()
        request["operation"] = "run_shell"
        self.assert_protocol_error(
            "WORKER_OPERATION_INVALID", lambda: protocol.validate_request(request)
        )
        request, _capabilities, _policy = make_request()
        response = make_success_response(request)
        response["message"] = "peer-controlled"
        self.assert_protocol_error(
            "WORKER_PROTOCOL_SHAPE_INVALID",
            lambda: protocol.validate_response(response, request, now_utc=FIXED_NOW),
        )
        request, _capabilities, _policy = make_request()
        request["execution_binding"]["run_id"] = "semantic-index-run:x"
        self.assert_protocol_error(
            "WORKER_PROTOCOL_VALUE_INVALID",
            lambda: protocol.validate_request(request),
        )

    def test_requests_and_response_bindings_pin_exact_handoff_receipt_digest(
        self,
    ) -> None:
        request, _capabilities, _policy = make_request()
        binding = protocol.request_binding_for(request)
        self.assertEqual(
            binding["launcher_handoff_receipt_sha256"],
            HANDOFF_RECEIPT_SHA256,
        )
        missing = copy.deepcopy(request)
        missing.pop("launcher_handoff_receipt_sha256")
        self.assert_protocol_error(
            "WORKER_PROTOCOL_SHAPE_INVALID",
            lambda: protocol.validate_request(missing),
        )
        malformed = copy.deepcopy(request)
        malformed["launcher_handoff_receipt_sha256"] = "not-a-digest"
        self.assert_protocol_error(
            "WORKER_PROTOCOL_VALUE_INVALID",
            lambda: protocol.validate_request(malformed),
        )
        response = make_success_response(request)
        response["request_binding"]["launcher_handoff_receipt_sha256"] = SHA_A
        self.assert_protocol_error(
            "WORKER_RESPONSE_BINDING_MISMATCH",
            lambda: protocol.validate_response(
                response,
                request,
                now_utc=FIXED_NOW,
            ),
        )

        control, _capabilities, _policy = make_control_request()
        control_binding = protocol.control_request_binding_for(control)
        self.assertEqual(
            control_binding["launcher_handoff_receipt_sha256"],
            HANDOFF_RECEIPT_SHA256,
        )
        missing_control = copy.deepcopy(control)
        missing_control.pop("launcher_handoff_receipt_sha256")
        self.assert_protocol_error(
            "WORKER_PROTOCOL_SHAPE_INVALID",
            lambda: protocol.validate_control_request(missing_control),
        )
        control_response = make_control_success_response(control)
        control_response["request_binding"][
            "launcher_handoff_receipt_sha256"
        ] = SHA_A
        self.assert_protocol_error(
            "WORKER_RESPONSE_BINDING_MISMATCH",
            lambda: protocol.validate_control_response(
                control_response,
                control,
                now_utc=FIXED_NOW,
            ),
        )

    def test_phase_and_control_results_pin_exact_ledger_identity_and_revision(
        self,
    ) -> None:
        request, _capabilities, _policy = make_request()
        binding = protocol.request_binding_for(request)
        self.assertEqual(LEDGER_IDENTITY, binding["idempotency_ledger_identity"])
        self.assertEqual(LEDGER_REVISION, binding["idempotency_ledger_revision"])

        for field in ("idempotency_ledger_identity", "idempotency_ledger_revision"):
            missing = copy.deepcopy(request)
            missing.pop(field)
            with self.subTest(kind="phase_missing", field=field):
                self.assert_protocol_error(
                    "WORKER_PROTOCOL_SHAPE_INVALID",
                    lambda missing=missing: protocol.validate_request(missing),
                )

        malformed_identity = copy.deepcopy(request)
        malformed_identity["idempotency_ledger_identity"] = "../foreign-ledger"
        self.assert_protocol_error(
            "WORKER_PROTOCOL_VALUE_INVALID",
            lambda: protocol.validate_request(malformed_identity),
        )
        malformed_revision = copy.deepcopy(request)
        malformed_revision["idempotency_ledger_revision"] = SHA_A
        self.assert_protocol_error(
            "WORKER_PROTOCOL_VALUE_INVALID",
            lambda: protocol.validate_request(malformed_revision),
        )

        response = make_success_response(request)
        response["request_binding"]["idempotency_ledger_identity"] = (
            "worker-ledger:foreign"
        )
        self.assert_protocol_error(
            "WORKER_RESPONSE_BINDING_MISMATCH",
            lambda: protocol.validate_response(response, request, now_utc=FIXED_NOW),
        )
        response = make_success_response(request)
        response["request_binding"]["idempotency_ledger_revision"] = SHA_REV_A
        self.assert_protocol_error(
            "WORKER_RESPONSE_BINDING_MISMATCH",
            lambda: protocol.validate_response(response, request, now_utc=FIXED_NOW),
        )
        response = make_success_response(request)
        response["artifact"]["idempotency_ledger_identity"] = "worker-ledger:foreign"
        self.assert_protocol_error(
            "WORKER_RESPONSE_BINDING_MISMATCH",
            lambda: protocol.validate_response(response, request, now_utc=FIXED_NOW),
        )
        response = make_success_response(request)
        response["artifact"]["idempotency_ledger_revision"] = SHA_REV_A
        self.assert_protocol_error(
            "WORKER_RESPONSE_BINDING_MISMATCH",
            lambda: protocol.validate_response(response, request, now_utc=FIXED_NOW),
        )

        control, _capabilities, _policy = make_control_request()
        control_binding = protocol.control_request_binding_for(control)
        self.assertEqual(
            control["idempotency_ledger_identity"],
            control_binding["idempotency_ledger_identity"],
        )
        self.assertEqual(
            control["idempotency_ledger_revision"],
            control_binding["idempotency_ledger_revision"],
        )
        for field in ("idempotency_ledger_identity", "idempotency_ledger_revision"):
            missing = copy.deepcopy(control)
            missing.pop(field)
            with self.subTest(kind="control_missing", field=field):
                self.assert_protocol_error(
                    "WORKER_PROTOCOL_SHAPE_INVALID",
                    lambda missing=missing: protocol.validate_control_request(missing),
                )

        control_response = make_control_success_response(control)
        control_response["request_binding"]["idempotency_ledger_identity"] = (
            "worker-ledger:foreign"
        )
        self.assert_protocol_error(
            "WORKER_RESPONSE_BINDING_MISMATCH",
            lambda: protocol.validate_control_response(
                control_response,
                control,
                now_utc=FIXED_NOW,
            ),
        )
        control_response = make_control_success_response(control)
        control_response["request_binding"]["idempotency_ledger_revision"] = SHA_REV_A
        self.assert_protocol_error(
            "WORKER_RESPONSE_BINDING_MISMATCH",
            lambda: protocol.validate_control_response(
                control_response,
                control,
                now_utc=FIXED_NOW,
            ),
        )

    def test_response_and_artifact_bindings_reject_substitution(self) -> None:
        request, _capabilities, _policy = make_request()
        response = make_success_response(request)
        response["request_binding"]["approval_basis_sha256"] = SHA_C
        self.assert_protocol_error(
            "WORKER_RESPONSE_BINDING_MISMATCH",
            lambda: protocol.validate_response(response, request, now_utc=FIXED_NOW),
        )
        response = make_success_response(request)
        response["artifact"]["observed_targets"][0]["target_identity"] = "other-target"
        self.assert_protocol_error(
            "WORKER_TARGET_BINDING_INVALID",
            lambda: protocol.validate_response(response, request, now_utc=FIXED_NOW),
        )
        response = make_success_response(request)
        response["artifact"]["worker_attestation"]["process_start_token_sha256"] = SHA_C
        self.assert_protocol_error(
            "WORKER_RESPONSE_BINDING_MISMATCH",
            lambda: protocol.validate_response(response, request, now_utc=FIXED_NOW),
        )

    def test_phase_specific_artifact_and_success_mutation_are_enforced(self) -> None:
        for operation in OPERATIONS:
            request, _capabilities, _policy = make_request(operation=operation)
            response = make_success_response(request)
            response["mutation_state"] = (
                "committed" if response["mutation_state"] != "committed" else "none"
            )
            with self.subTest(operation=operation, kind="mutation"):
                self.assert_protocol_error(
                    "WORKER_RESPONSE_STATUS_INVALID",
                    lambda response=response, request=request: (
                        protocol.validate_response(response, request, now_utc=FIXED_NOW)
                    ),
                )
            response = make_success_response(request)
            response["artifact"]["artifact_root"]["schema"] = (
                "simworld-semantic-index-wrong-artifact/v1"
            )
            with self.subTest(operation=operation, kind="artifact"):
                self.assert_protocol_error(
                    "WORKER_ARTIFACT_INVALID",
                    lambda response=response, request=request: (
                        protocol.validate_response(response, request, now_utc=FIXED_NOW)
                    ),
                )

    def test_failure_error_is_closed_and_unsafe_retry_is_rejected(self) -> None:
        request, _capabilities, _policy = make_request(operation="upsert_qdrant_exact")
        for mutation_state in ("committed", "ambiguous"):
            response = make_failed_response(
                request, mutation_state=mutation_state, retryable=True
            )
            with self.subTest(mutation_state=mutation_state):
                self.assert_protocol_error(
                    "WORKER_ERROR_INVALID",
                    lambda response=response: protocol.validate_response(
                        response, request
                    ),
                )
        response = make_failed_response(request)
        response["error"]["message"] = "capability-like peer text"
        self.assert_protocol_error(
            "WORKER_PROTOCOL_SHAPE_INVALID",
            lambda: protocol.validate_response(response, request),
        )
        response = make_failed_response(request, code="WORKER_UNKNOWN")
        self.assert_protocol_error(
            "WORKER_ERROR_INVALID",
            lambda: protocol.validate_response(response, request),
        )
        inspect_request, _capabilities, _policy = make_request()
        response = make_failed_response(
            inspect_request,
            dependency="qdrant",
            code="QDRANT_UNAVAILABLE",
        )
        self.assert_protocol_error(
            "WORKER_ERROR_INVALID",
            lambda: protocol.validate_response(response, inspect_request),
        )

    def test_four_control_operations_have_closed_scopes_results_and_recovery_bindings(
        self,
    ) -> None:
        failure_states = {
            "query_phase_status": {"none"},
            "recover_phase_receipt": {"none"},
            "cancel_phase_work": {"none", "committed", "ambiguous"},
            "quarantine_generation": {"none", "committed", "ambiguous"},
        }
        expected_scopes = {
            "query_phase_status": "semantic_index_phase_status_read",
            "recover_phase_receipt": "semantic_index_phase_receipt_recover",
            "cancel_phase_work": "semantic_index_phase_work_cancel",
            "quarantine_generation": "semantic_index_generation_quarantine",
        }
        for operation in CONTROL_OPERATIONS:
            request, _capabilities, _policy = make_control_request(operation=operation)
            validated = protocol.validate_control_request(request)
            self.assertEqual(
                validated["credential_transport"]["descriptors"][0]["scope"],
                expected_scopes[operation],
            )
            success = make_control_success_response(validated)
            protocol.validate_control_response(success, validated, now_utc=FIXED_NOW)
            self.assertEqual(
                success["receipt"]["phase_request_sha256"],
                request["phase_request_sha256"],
            )
            for state in {"none", "staged", "committed", "ambiguous"}:
                for retryable in (False, True):
                    failure = make_control_failed_response(
                        validated,
                        mutation_state=state,
                        retryable=retryable,
                    )
                    legal = state in failure_states[operation] and (
                        state == "none" or not retryable
                    )
                    with self.subTest(
                        operation=operation,
                        state=state,
                        retryable=retryable,
                    ):
                        if legal:
                            protocol.validate_control_response(failure, validated)
                        else:
                            with self.assertRaises(protocol.WorkerProtocolError):
                                protocol.validate_control_response(failure, validated)

        request, _capabilities, _policy = make_control_request(
            operation="recover_phase_receipt"
        )
        response = make_control_success_response(request)
        response["receipt"]["phase_request_sha256"] = SHA_E
        self.assert_protocol_error(
            "WORKER_RESPONSE_BINDING_MISMATCH",
            lambda: protocol.validate_control_response(
                response, request, now_utc=FIXED_NOW
            ),
        )
        response = make_control_success_response(request)
        response["receipt"]["immutable_phase_receipt_sha256"] = None
        self.assert_protocol_error(
            "WORKER_ARTIFACT_INVALID",
            lambda: protocol.validate_control_response(
                response, request, now_utc=FIXED_NOW
            ),
        )

    def test_control_requests_reject_free_form_commands_and_scope_substitution(
        self,
    ) -> None:
        request, _capabilities, _policy = make_control_request()
        request["command"] = "arbitrary"
        self.assert_protocol_error(
            "WORKER_PROTOCOL_SHAPE_INVALID",
            lambda: protocol.validate_control_request(request),
        )
        request, _capabilities, _policy = make_control_request()
        request["credential_transport"]["descriptors"][0]["scope"] = (
            "semantic_index_generation_quarantine"
        )
        self.assert_protocol_error(
            "WORKER_CREDENTIAL_SCOPE_INVALID",
            lambda: protocol.validate_control_request(request),
        )

    def test_capability_transform_scanner_covers_raw_base64_urlsafe_and_sha256(
        self,
    ) -> None:
        capability = b"scanner-capability-fixture-001"
        digest = hashlib.sha256(capability)
        markers = (
            capability,
            base64.b64encode(capability),
            base64.b64encode(capability).rstrip(b"="),
            base64.urlsafe_b64encode(capability),
            base64.urlsafe_b64encode(capability).rstrip(b"="),
            capability.hex().encode("ascii"),
            capability.hex().upper().encode("ascii"),
            digest.hexdigest().encode("ascii"),
            digest.hexdigest().upper().encode("ascii"),
            digest.digest(),
            base64.b64encode(digest.digest()),
            base64.urlsafe_b64encode(digest.digest()).rstrip(b"="),
        )
        for marker in markers:
            with self.subTest(marker=marker):
                error = self.assert_protocol_error(
                    "WORKER_RESPONSE_CAPABILITY_LEAK",
                    lambda marker=marker: protocol._assert_no_capability_transforms(
                        b'{"value":"' + marker + b'"}', (capability,)
                    ),
                )
                self.assertNotIn(capability.decode("ascii"), str(error))

        binary_capability = bytes(range(16, 48))
        for material in (binary_capability, hashlib.sha256(binary_capability).digest()):
            percent_upper = quote_from_bytes(material, safe="").encode("ascii")
            percent_lower = re.sub(
                rb"%([0-9A-F]{2})",
                lambda match: b"%" + match.group(1).lower(),
                percent_upper,
            )
            for marker in (percent_upper, percent_lower):
                self.assert_protocol_error(
                    "WORKER_RESPONSE_CAPABILITY_LEAK",
                    lambda marker=marker: protocol._assert_no_capability_transforms(
                        b'{"value":"' + marker + b'"}', (binary_capability,)
                    ),
                )

        mixed_percent = b"".join(
            (f"%{byte:02x}".encode("ascii") if index % 2 else bytes([byte]))
            for index, byte in enumerate(capability)
        )
        double_percent = quote_from_bytes(
            quote_from_bytes(capability, safe="").encode("ascii"),
            safe="",
        ).encode("ascii")
        nested_percent = capability
        for _unused in range(5):
            nested_percent = quote_from_bytes(nested_percent, safe="").encode("ascii")
        for encoded_frame in (mixed_percent, double_percent, nested_percent):
            self.assert_protocol_error(
                "WORKER_RESPONSE_CAPABILITY_LEAK",
                lambda encoded_frame=encoded_frame: (
                    protocol._assert_no_capability_transforms(
                        b'{"value":"' + encoded_frame + b'"}', (capability,)
                    )
                ),
            )

    def test_sender_rejects_transformed_capability_in_request_before_transmission(
        self,
    ) -> None:
        deadline = time.monotonic_ns() + 3_000_000_000
        worker = FakeUnixWorker(
            self.root,
            lambda received: send_response(received, self.live_success(received.value)),
        )
        client = worker.connect()
        try:
            request, capabilities, policy = make_request(client, deadline_ns=deadline)
            request["input_binding"]["sha256"] = hashlib.sha256(
                capabilities[0]
            ).hexdigest()
            error = self.assert_protocol_error(
                "WORKER_REQUEST_CAPABILITY_LEAK",
                lambda: protocol.call_worker(
                    request,
                    capabilities=capabilities,
                    launcher_handoff_receipt_sha256=HANDOFF_RECEIPT_SHA256,
                    peer_policy=policy,
                    connected_socket=client,
                ),
            )
        finally:
            worker.close(allow_error=True)
        self.assertEqual(error.mutation_state, "none")
        self.assertIsNone(worker.received)

    @REQUIRES_MEMFD
    def test_capability_digest_hidden_in_artifact_is_rejected_before_json_validation(
        self,
    ) -> None:
        deadline = time.monotonic_ns() + 3_000_000_000

        def leak(received):
            response = self.live_success(received.value)
            response["artifact"]["artifact_root"]["sha256"] = hashlib.sha256(
                received.capabilities[0]
            ).hexdigest()
            raw = protocol.canonical_json_bytes(response)
            send_raw_response(received, raw)

        with FakeUnixWorker(self.root, leak) as worker:
            client = worker.connect()
            request, capabilities, policy = make_request(client, deadline_ns=deadline)
            error = self.assert_protocol_error(
                "WORKER_RESPONSE_CAPABILITY_LEAK",
                lambda: protocol.call_worker(
                    request,
                    capabilities=capabilities,
                    launcher_handoff_receipt_sha256=HANDOFF_RECEIPT_SHA256,
                    peer_policy=policy,
                    connected_socket=client,
                ),
            )
        self.assertEqual(error.mutation_state, "ambiguous")

    @REQUIRES_MEMFD
    def test_receiver_scans_fd_capability_before_decoding_malformed_request(
        self,
    ) -> None:
        receiver, sender = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        capability = b"malformed-request-capability-001"
        descriptor = protocol._sealed_capability_fd(capability)
        leak = hashlib.sha256(capability).hexdigest().upper().encode("ascii")
        raw = b'{"malformed":"' + leak + b'"'
        frame = struct.pack("!I", len(raw)) + raw
        policy = client_policy_for(receiver)
        try:
            sender.sendmsg(
                [frame],
                [
                    (
                        socket.SOL_SOCKET,
                        socket.SCM_RIGHTS,
                        array.array("i", [descriptor]).tobytes(),
                    )
                ],
            )
            sender.shutdown(socket.SHUT_WR)
            error = self.assert_protocol_error(
                "WORKER_REQUEST_CAPABILITY_LEAK",
                lambda: protocol.receive_worker_request(
                    receiver,
                    handshake_deadline_monotonic_ns=time.monotonic_ns() + 1_000_000_000,
                    client_peer_policy=policy,
                ),
            )
        finally:
            os.close(descriptor)
            sender.close()
            receiver.close()
        self.assertEqual(error.mutation_state, "none")

    @REQUIRES_MEMFD
    def test_request_and_response_trailing_bytes_are_rejected(self) -> None:
        receiver, sender = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        deadline = time.monotonic_ns() + 2_000_000_000
        request, capabilities, _policy = make_request(deadline_ns=deadline)
        descriptor = protocol._sealed_capability_fd(capabilities[0])
        client_policy = client_policy_for(receiver)
        try:
            self._send_request_with_fds(
                sender,
                request,
                [descriptor],
                trailing=b"x",
            )
            self.assert_protocol_error(
                "WORKER_REQUEST_TRAILING_DATA",
                lambda: protocol.receive_worker_request(
                    receiver,
                    handshake_deadline_monotonic_ns=time.monotonic_ns() + 1_000_000_000,
                    client_peer_policy=client_policy,
                ),
            )
        finally:
            os.close(descriptor)
            sender.close()
            receiver.close()

        def trailing_response(received):
            raw = protocol.canonical_json_bytes(self.live_success(received.value))
            send_raw_response(received, raw, trailing=b"x")

        deadline = time.monotonic_ns() + 3_000_000_000
        with FakeUnixWorker(self.root, trailing_response) as worker:
            client = worker.connect()
            request, capabilities, policy = make_request(client, deadline_ns=deadline)
            error = self.assert_protocol_error(
                "WORKER_RESPONSE_TRAILING_DATA",
                lambda: protocol.call_worker(
                    request,
                    capabilities=capabilities,
                    launcher_handoff_receipt_sha256=HANDOFF_RECEIPT_SHA256,
                    peer_policy=policy,
                    connected_socket=client,
                ),
            )
        self.assertEqual(error.mutation_state, "ambiguous")

    @REQUIRES_MEMFD
    def test_response_ancillary_descriptor_is_rejected_and_closed(self) -> None:
        def ancillary_response(received):
            raw = protocol.canonical_json_bytes(self.live_success(received.value))
            descriptor = protocol._sealed_capability_fd(b"response-ancillary-fixture")
            connection = received._take_response_socket()
            try:
                connection.sendmsg(
                    [struct.pack("!I", len(raw)) + raw],
                    [
                        (
                            socket.SOL_SOCKET,
                            socket.SCM_RIGHTS,
                            array.array("i", [descriptor]).tobytes(),
                        )
                    ],
                )
                connection.shutdown(socket.SHUT_WR)
            finally:
                os.close(descriptor)
                connection.close()

        deadline = time.monotonic_ns() + 3_000_000_000
        before = len(os.listdir("/proc/self/fd"))
        with FakeUnixWorker(self.root, ancillary_response) as worker:
            client = worker.connect()
            request, capabilities, policy = make_request(client, deadline_ns=deadline)
            error = self.assert_protocol_error(
                "WORKER_ANCILLARY_INVALID",
                lambda: protocol.call_worker(
                    request,
                    capabilities=capabilities,
                    launcher_handoff_receipt_sha256=HANDOFF_RECEIPT_SHA256,
                    peer_policy=policy,
                    connected_socket=client,
                ),
            )
        after = len(os.listdir("/proc/self/fd"))
        self.assertEqual(error.mutation_state, "ambiguous")
        self.assertEqual(before, after)

    def _send_request_with_fds(
        self,
        sender: socket.socket,
        request: dict,
        descriptors: list[int],
        *,
        trailing: bytes = b"",
    ) -> None:
        raw = protocol.canonical_json_bytes(request)
        frame = struct.pack("!I", len(raw)) + raw + trailing
        ancillary = [
            (
                socket.SOL_SOCKET,
                socket.SCM_RIGHTS,
                array.array("i", descriptors).tobytes(),
            )
        ]
        sender.sendmsg([frame], ancillary)
        sender.shutdown(socket.SHUT_WR)

    @REQUIRES_MEMFD
    def test_receiver_rejects_missing_extra_unsealed_wrong_size_and_aliased_fds(
        self,
    ) -> None:
        def run_case(fd_builder, expected_code):
            receiver, sender = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
            deadline = time.monotonic_ns() + 2_000_000_000
            request, capabilities, _policy = make_request(deadline_ns=deadline)
            descriptors = fd_builder(capabilities)
            client_policy = client_policy_for(receiver)
            try:
                self._send_request_with_fds(sender, request, descriptors)
                self.assert_protocol_error(
                    expected_code,
                    lambda: protocol.receive_worker_request(
                        receiver,
                        handshake_deadline_monotonic_ns=time.monotonic_ns()
                        + 1_000_000_000,
                        client_peer_policy=client_policy,
                    ),
                )
            finally:
                for descriptor in descriptors:
                    with contextlib.suppress(OSError):
                        os.close(descriptor)
                sender.close()
                receiver.close()

        run_case(lambda _caps: [], "WORKER_ANCILLARY_INVALID")

        def extra(caps):
            return [protocol._sealed_capability_fd(caps[0]) for _ in range(4)]

        run_case(extra, "WORKER_ANCILLARY_INVALID")

        def unsealed(caps):
            descriptor = os.memfd_create(
                "unsealed",
                os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING,
            )
            os.write(descriptor, caps[0])
            return [descriptor]

        run_case(unsealed, "WORKER_CAPABILITY_INVALID")

        def wrong_size(caps):
            return [protocol._sealed_capability_fd(caps[0] + b"x")]

        run_case(wrong_size, "WORKER_CAPABILITY_INVALID")

        receiver, sender = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        deadline = time.monotonic_ns() + 2_000_000_000
        request, capabilities, _policy = make_request(
            operation="reconcile_exact_snapshot", deadline_ns=deadline
        )
        descriptor = protocol._sealed_capability_fd(capabilities[0])
        client_policy = client_policy_for(receiver)
        try:
            self._send_request_with_fds(
                sender, request, [descriptor, descriptor, descriptor]
            )
            self.assert_protocol_error(
                "WORKER_CAPABILITY_INVALID",
                lambda: protocol.receive_worker_request(
                    receiver,
                    handshake_deadline_monotonic_ns=time.monotonic_ns() + 1_000_000_000,
                    client_peer_policy=client_policy,
                ),
            )
        finally:
            with contextlib.suppress(OSError):
                os.close(descriptor)
            sender.close()
            receiver.close()

    @REQUIRES_MEMFD
    def test_receiver_sets_cloexec_and_closes_received_descriptors(self) -> None:
        receiver, sender = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        deadline = time.monotonic_ns() + 2_000_000_000
        request, capabilities, _policy = make_request(deadline_ns=deadline)
        sender_fd = protocol._sealed_capability_fd(capabilities[0])
        client_policy = client_policy_for(receiver)
        before = set(os.listdir("/proc/self/fd"))
        received = None
        try:
            self._send_request_with_fds(sender, request, [sender_fd])
            received = protocol.receive_worker_request(
                receiver,
                handshake_deadline_monotonic_ns=time.monotonic_ns() + 1_000_000_000,
                client_peer_policy=client_policy,
            )
            after = set(os.listdir("/proc/self/fd"))
        finally:
            os.close(sender_fd)
            sender.close()
            if received is not None:
                received.close()
        self.assertEqual(received.capabilities, capabilities)
        self.assertEqual(before, after)

    @REQUIRES_MEMFD
    def test_worker_response_session_is_consumed_once_and_client_observes_eof(
        self,
    ) -> None:
        receiver, sender = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        deadline = time.monotonic_ns() + 2_000_000_000
        request, capabilities, _policy = make_request(deadline_ns=deadline)
        descriptor = protocol._sealed_capability_fd(capabilities[0])
        client_policy = client_policy_for(receiver)
        try:
            self._send_request_with_fds(sender, request, [descriptor])
            received = protocol.receive_worker_request(
                receiver,
                handshake_deadline_monotonic_ns=time.monotonic_ns() + 1_000_000_000,
                client_peer_policy=client_policy,
            )
            response = self.live_success(received.value)
            protocol.send_worker_response(received, response)
            self.assertEqual(receiver.fileno(), -1)
            self.assert_protocol_error(
                "WORKER_SESSION_CONSUMED",
                lambda: protocol.send_worker_response(received, response),
            )
            wire = bytearray()
            while True:
                chunk = sender.recv(65536)
                if not chunk:
                    break
                wire.extend(chunk)
        finally:
            os.close(descriptor)
            sender.close()
            receiver.close()
        (size,) = struct.unpack("!I", wire[:4])
        self.assertEqual(size, len(wire) - 4)
        self.assertEqual(bytes(wire[4:]), protocol.canonical_json_bytes(response))

    @REQUIRES_MEMFD
    def test_worker_does_not_execute_complete_frame_until_request_eof(self) -> None:
        receiver, sender = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        embedded_deadline = time.monotonic_ns() + 2_000_000_000
        request, capabilities, _policy = make_request(deadline_ns=embedded_deadline)
        descriptor = protocol._sealed_capability_fd(capabilities[0])
        raw = protocol.canonical_json_bytes(request)
        policy = client_policy_for(receiver)
        try:
            sender.sendmsg(
                [struct.pack("!I", len(raw)) + raw],
                [
                    (
                        socket.SOL_SOCKET,
                        socket.SCM_RIGHTS,
                        array.array("i", [descriptor]).tobytes(),
                    )
                ],
            )
            started = time.monotonic()
            error = self.assert_protocol_error(
                "WORKER_DEADLINE_EXCEEDED",
                lambda: protocol.receive_worker_request(
                    receiver,
                    handshake_deadline_monotonic_ns=time.monotonic_ns() + 100_000_000,
                    client_peer_policy=policy,
                ),
            )
            elapsed = time.monotonic() - started
        finally:
            os.close(descriptor)
            sender.close()
            receiver.close()
        self.assertLess(elapsed, 0.5)
        self.assertEqual(error.mutation_state, "none")

    def test_invalid_regular_fd_is_consumed_without_duplication(self) -> None:
        path = self.root / "regular.bin"
        path.write_bytes(b"fixture")
        descriptor = os.open(path, os.O_RDONLY)
        before = set(os.listdir("/proc/self/fd"))
        self.assert_protocol_error(
            "WORKER_SOCKET_INVALID",
            lambda: protocol._coerce_connected_socket(descriptor),
        )
        with self.assertRaises(OSError):
            os.fstat(descriptor)
        after = set(os.listdir("/proc/self/fd"))
        self.assertEqual(len(after), len(before) - 1)

    def test_artifact_freshness_clock_skew_and_ttl_are_enforced(self) -> None:
        request, _capabilities, _policy = make_request()
        cases = []
        expired = make_success_response(request)
        expired["artifact"]["issued_at"] = "2000-01-01T00:00:00Z"
        expired["artifact"]["expires_at"] = "2000-01-01T00:05:00Z"
        cases.append(expired)
        future = make_success_response(request)
        future["artifact"]["issued_at"] = "2026-07-21T12:00:06Z"
        future["artifact"]["expires_at"] = "2026-07-21T12:05:00Z"
        cases.append(future)
        excessive = make_success_response(request)
        excessive["artifact"]["issued_at"] = "2026-07-21T11:59:59Z"
        excessive["artifact"]["expires_at"] = "2026-07-21T12:20:00Z"
        cases.append(excessive)
        for response in cases:
            with self.subTest(response=response["artifact"]["expires_at"]):
                self.assert_protocol_error(
                    "WORKER_ARTIFACT_STALE",
                    lambda response=response: protocol.validate_response(
                        response, request, now_utc=FIXED_NOW
                    ),
                )

    def test_canonical_decoder_rejects_duplicates_floats_non_nfc_and_spacing(
        self,
    ) -> None:
        cases = (
            (b'{"a":1,"a":2}', "WORKER_JSON_INVALID"),
            (b'{"value":1.0}', "WORKER_JSON_INVALID"),
            ('{"value":"e\u0301"}'.encode("utf-8"), "WORKER_JSON_INVALID"),
            (b'{ "value":1}', "WORKER_JSON_NONCANONICAL"),
        )
        for raw, code in cases:
            with self.subTest(raw=raw):
                self.assert_protocol_error(
                    code, lambda raw=raw: protocol.decode_canonical_json(raw)
                )

    def test_memfd_runtime_without_required_apis_fails_closed(self) -> None:
        if MEMFD_AVAILABLE:
            self.skipTest("runtime has complete Linux memfd sealing API")
        self.assert_protocol_error(
            "WORKER_PROTOCOL_MEMFD_UNAVAILABLE",
            lambda: protocol._sealed_capability_fd(b"capability-fixture-001"),
        )

    def test_source_has_no_ambient_dynamic_or_network_client_surface(self) -> None:
        source = (TOOLS_DIR / "semantic_index_worker_protocol.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        imported = {
            alias.name.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        self.assertTrue(
            {
                "ctypes",
                "subprocess",
                "importlib",
                "urllib",
                "http",
                "requests",
            }.isdisjoint(imported)
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                self.assertNotEqual((node.value.id, node.attr), ("os", "environ"))
        self.assertEqual(protocol.MAX_FRAME_BYTES, 2 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
