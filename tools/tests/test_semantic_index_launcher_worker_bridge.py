from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import fcntl
import hashlib
import inspect
import json
import os
import pathlib
import socket
import sys
import tempfile
import threading
import time
import unittest
from typing import Any


TOOLS_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))
if str(TOOLS_DIR / "tests") not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR / "tests"))

import semantic_index_launcher_handoff as handoff  # noqa: E402
import semantic_index_worker_protocol as protocol  # noqa: E402
import test_semantic_index_worker_protocol as fixtures  # noqa: E402


PLATFORM_AVAILABLE = (
    os.name == "posix"
    and hasattr(os, "fork")
    and hasattr(socket, "SO_PEERCRED")
    and hasattr(socket, "SCM_RIGHTS")
    and bool(getattr(socket, "MSG_CMSG_CLOEXEC", 0))
)
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


def _close_fd(descriptor: int) -> None:
    with contextlib.suppress(OSError):
        os.close(descriptor)


def _close_socket(sock: socket.socket | None) -> None:
    if sock is not None:
        with contextlib.suppress(OSError):
            sock.close()


def _identity_binding(pid: int, boot: str) -> dict[str, Any]:
    start = handoff.process_start_time_ticks(pid)
    return {
        "uid": os.geteuid(),
        "gid": os.getegid(),
        "pid": pid,
        "process_start_time_ticks": start,
        "process_start_token_sha256": handoff.process_start_token_sha256(
            boot,
            pid,
            start,
        ),
    }


def _expected_worker_binding(
    worker_client: socket.socket,
    boot: str,
) -> handoff.ExpectedWorkerBinding:
    observation = handoff._observe_worker_socket(worker_client, boot)
    return handoff.ExpectedWorkerBinding(
        protocol_revision=handoff.WORKER_PROTOCOL_REVISION,
        deployment_identity="semantic-worker-r1",
        runtime_image="registry.local/semantic-worker@sha256:" + fixtures.SHA_D,
        whoami_sha256=fixtures.SHA_A,
        peer_attestation_sha256=fixtures.SHA_E,
        runtime_attestation_sha256=fixtures.SHA_F,
        host_boot_id_sha256=boot,
        **dataclasses.asdict(observation),
    )


def _receipt(
    *,
    launcher_pid: int,
    coordinator_pid: int,
    boot: str,
    worker: handoff.ExpectedWorkerBinding,
) -> dict[str, Any]:
    issued = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    coordinator = _identity_binding(coordinator_pid, boot)
    coordinator["parent_pid"] = launcher_pid
    return {
        "schema": handoff.HANDOFF_RECEIPT_SCHEMA,
        "protocol": handoff.HANDOFF_PROTOCOL,
        "issued_at": issued.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": (issued + dt.timedelta(seconds=30)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "host_boot_id_sha256": boot,
        "launcher_binding": _identity_binding(launcher_pid, boot),
        "coordinator_binding": coordinator,
        "execution_binding": {
            "execution_plan_sha256": fixtures.SHA_C,
            "reviewed_job_sha256": fixtures.SHA_A,
            "approval_basis_sha256": fixtures.SHA_B,
            "generation_id": fixtures.GENERATION_ID,
        },
        "worker_binding": worker.as_dict(),
        "serialization_contract": dict(handoff._SERIALIZATION_CONTRACT),
    }


def _read_report(descriptor: int) -> dict[str, Any]:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(descriptor, 4096)
        if not chunk:
            break
        chunks.append(chunk)
    return json.loads(b"".join(chunks).decode("utf-8"))


def _write_report(descriptor: int, value: dict[str, Any]) -> None:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    offset = 0
    while offset < len(raw):
        offset += os.write(descriptor, raw[offset:])


def _error_code(error: BaseException) -> str:
    return str(getattr(error, "code", type(error).__name__))


@unittest.skipUnless(
    PLATFORM_AVAILABLE,
    "Linux fork, SO_PEERCRED, and close-on-exec SCM_RIGHTS are required",
)
class NonPrivilegedScmRightsBridgeModelTests(unittest.TestCase):
    """Model the public handoff output without pretending same-UID is privileged.

    The public receiver correctly requires a different-UID launcher and is
    covered by its root-only positive test.  This model deliberately exercises
    the kernel descriptor-transfer semantics on every Linux test runtime,
    including ordinary non-root CI.
    """

    def test_origin_peer_and_receipt_digest_survive_one_scm_rights_handoff(
        self,
    ) -> None:
        launcher_pid = os.getpid()
        boot = handoff.current_host_boot_id_sha256()
        worker_endpoint, launcher_worker_endpoint = socket.socketpair(
            socket.AF_UNIX,
            socket.SOCK_STREAM,
        )
        launcher_channel, coordinator_channel = socket.socketpair(
            socket.AF_UNIX,
            socket.SOCK_STREAM,
        )
        report_read, report_write = os.pipe2(os.O_CLOEXEC)
        coordinator_pid = os.fork()
        if coordinator_pid == 0:
            try:
                _close_socket(worker_endpoint)
                _close_socket(launcher_worker_endpoint)
                _close_socket(launcher_channel)
                _close_fd(report_read)
                raw, descriptor = handoff._receive_frame_and_one_fd(
                    coordinator_channel,
                    time.monotonic_ns() + 3_000_000_000,
                )
                coordinator_channel.close()
                received_socket = socket.socket(fileno=descriptor)
                opaque = handoff.AttestedWorkerHandoff(
                    handoff._CONSTRUCTION_SENTINEL,
                    received_socket,
                    raw,
                )
                receipt = handoff._validate_receipt_shape(opaque.receipt)
                transferred_socket = opaque.take_worker_socket()
                request, _capabilities, _peer_policy = fixtures.make_request(
                    transferred_socket,
                    worker_pid=launcher_pid,
                )
                cases: dict[str, dict[str, Any]] = {}
                for name in ("matching", "wrong", "missing"):
                    candidate = dict(request)
                    if name == "matching":
                        candidate["launcher_handoff_receipt_sha256"] = (
                            opaque.receipt_sha256
                        )
                    elif name == "wrong":
                        candidate["launcher_handoff_receipt_sha256"] = "0" * 64
                    else:
                        candidate.pop("launcher_handoff_receipt_sha256", None)
                    executed = False
                    try:
                        validated = protocol._validate_wire_request(candidate)
                        protocol._verify_launcher_handoff_receipt_binding(
                            validated,
                            opaque.receipt_sha256,
                        )
                        executed = True
                        cases[name] = {"code": "ok", "executed": executed}
                    except protocol.WorkerProtocolError as error:
                        cases[name] = {
                            "code": error.code,
                            "executed": executed,
                        }
                worker_from_receipt = dict(receipt["worker_binding"])
                worker_from_receipt.pop("protocol_revision")
                _write_report(
                    report_write,
                    {
                        "cases": cases,
                        "digest": opaque.receipt_sha256,
                        "peer": list(protocol._peer_credentials(transferred_socket)),
                        "worker_binding_matches": (
                            request["worker_binding"] == worker_from_receipt
                        ),
                    },
                )
                opaque.close()
                transferred_socket.close()
            except BaseException as error:
                _write_report(report_write, {"fatal": repr(error)})
            finally:
                _close_socket(coordinator_channel)
                _close_fd(report_write)
                os._exit(0)

        _close_socket(coordinator_channel)
        _close_fd(report_write)
        expected_worker = _expected_worker_binding(launcher_worker_endpoint, boot)
        receipt = _receipt(
            launcher_pid=launcher_pid,
            coordinator_pid=coordinator_pid,
            boot=boot,
            worker=expected_worker,
        )
        raw = handoff.canonical_json_bytes(handoff._validate_receipt_shape(receipt))
        digest = hashlib.sha256(raw).hexdigest()
        worker_metadata = os.fstat(worker_endpoint.fileno())
        launcher_policy = protocol.ClientPeerPolicy(
            expected_uid=os.geteuid(),
            expected_gid=os.getegid(),
            expected_pid=launcher_pid,
            expected_process_start_time_ticks=protocol.process_start_time_ticks(
                launcher_pid
            ),
            expected_socket_device=worker_metadata.st_dev,
            expected_socket_inode=worker_metadata.st_ino,
            host_boot_id_sha256=boot,
            expected_launcher_handoff_receipt_sha256=digest,
        )
        protocol.verify_client_peer(worker_endpoint, launcher_policy)
        observed_origin = protocol._peer_credentials(worker_endpoint)
        self.assertEqual(observed_origin[0], launcher_pid)
        self.assertNotEqual(observed_origin[0], coordinator_pid)

        old_coordinator_policy = dataclasses.replace(
            launcher_policy,
            expected_pid=coordinator_pid,
            expected_process_start_time_ticks=protocol.process_start_time_ticks(
                coordinator_pid
            ),
        )
        with self.assertRaises(protocol.WorkerProtocolError) as captured:
            protocol.verify_client_peer(worker_endpoint, old_coordinator_policy)
        self.assertEqual(captured.exception.code, "WORKER_PEER_CREDENTIAL_MISMATCH")

        handoff._send_frame_with_fd(
            launcher_channel,
            raw,
            launcher_worker_endpoint.fileno(),
            time.monotonic_ns() + 3_000_000_000,
        )
        launcher_channel.close()
        launcher_worker_endpoint.close()
        report = _read_report(report_read)
        _close_fd(report_read)
        _waited_pid, status = os.waitpid(coordinator_pid, 0)
        worker_endpoint.close()

        self.assertTrue(os.WIFEXITED(status), report)
        self.assertEqual(os.WEXITSTATUS(status), 0, report)
        self.assertNotIn("fatal", report)
        self.assertEqual(report["digest"], digest)
        self.assertEqual(report["peer"], [launcher_pid, os.geteuid(), os.getegid()])
        self.assertTrue(report["worker_binding_matches"])
        self.assertEqual(
            report["cases"],
            {
                "matching": {"code": "ok", "executed": True},
                "wrong": {
                    "code": "WORKER_LAUNCHER_HANDOFF_MISMATCH",
                    "executed": False,
                },
                "missing": {
                    "code": "WORKER_PROTOCOL_SHAPE_INVALID",
                    "executed": False,
                },
            },
        )

    def test_coordinator_call_requires_an_explicit_handoff_digest(self) -> None:
        parameter = inspect.signature(protocol.call_worker).parameters[
            "launcher_handoff_receipt_sha256"
        ]
        self.assertIs(parameter.default, inspect.Parameter.empty)
        policy_fields = {
            field.name for field in dataclasses.fields(protocol.ClientPeerPolicy)
        }
        self.assertIn("expected_launcher_handoff_receipt_sha256", policy_fields)


@unittest.skipUnless(
    PLATFORM_AVAILABLE and MEMFD_AVAILABLE,
    "Full bridge requires Linux SCM_RIGHTS and sealed memfd APIs",
)
class LauncherWorkerBridgeIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _run_bridge(self, mode: str) -> tuple[dict[str, Any], dict[str, Any]]:
        launcher_pid = os.getpid()
        boot = handoff.current_host_boot_id_sha256()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener_path = str(self.root / f"worker-{mode}.sock")
        listener.bind(listener_path)
        listener.listen(1)
        launcher_channel, coordinator_channel = socket.socketpair(
            socket.AF_UNIX,
            socket.SOCK_STREAM,
        )
        report_read, report_write = os.pipe2(os.O_CLOEXEC)
        coordinator_pid = os.fork()
        if coordinator_pid == 0:
            try:
                listener.close()
                launcher_channel.close()
                _close_fd(report_read)
                raw, descriptor = handoff._receive_frame_and_one_fd(
                    coordinator_channel,
                    time.monotonic_ns() + 4_000_000_000,
                )
                coordinator_channel.close()
                received_socket = socket.socket(fileno=descriptor)
                opaque = handoff.AttestedWorkerHandoff(
                    handoff._CONSTRUCTION_SENTINEL,
                    received_socket,
                    raw,
                )
                request, capabilities, peer_policy = fixtures.make_request(
                    received_socket,
                    deadline_ns=time.monotonic_ns() + 3_000_000_000,
                    worker_pid=launcher_pid,
                )
                request["launcher_handoff_receipt_sha256"] = opaque.receipt_sha256
                if mode == "wrong_request_digest":
                    request["launcher_handoff_receipt_sha256"] = "0" * 64
                elif mode == "missing_request_digest":
                    request.pop("launcher_handoff_receipt_sha256")
                response = protocol.call_worker(
                    request,
                    capabilities=capabilities,
                    launcher_handoff_receipt_sha256=opaque.receipt_sha256,
                    peer_policy=peer_policy,
                    connected_socket=opaque.take_worker_socket(),
                )
                _write_report(
                    report_write,
                    {
                        "code": "ok",
                        "digest": opaque.receipt_sha256,
                        "status": response.value["status"],
                    },
                )
                opaque.close()
            except BaseException as error:
                _write_report(report_write, {"code": _error_code(error)})
            finally:
                _close_socket(coordinator_channel)
                _close_fd(report_write)
                os._exit(0)

        coordinator_channel.close()
        _close_fd(report_write)
        worker_client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        worker_client.connect(listener_path)
        expected_worker = _expected_worker_binding(worker_client, boot)
        receipt = _receipt(
            launcher_pid=launcher_pid,
            coordinator_pid=coordinator_pid,
            boot=boot,
            worker=expected_worker,
        )
        raw = handoff.canonical_json_bytes(handoff._validate_receipt_shape(receipt))
        digest = hashlib.sha256(raw).hexdigest()
        worker_state: dict[str, Any] = {
            "executed": False,
            "error": None,
            "peer_pid": None,
        }

        def worker() -> None:
            connection: socket.socket | None = None
            received: protocol.ReceivedWorkerRequest | None = None
            try:
                connection, _address = listener.accept()
                worker_state["peer_pid"] = protocol._peer_credentials(connection)[0]
                metadata = os.fstat(connection.fileno())
                expected_pid = (
                    coordinator_pid
                    if mode == "old_coordinator_policy"
                    else launcher_pid
                )
                expected_digest = "8" * 64 if mode == "wrong_worker_digest" else digest
                policy = protocol.ClientPeerPolicy(
                    expected_uid=os.geteuid(),
                    expected_gid=os.getegid(),
                    expected_pid=expected_pid,
                    expected_process_start_time_ticks=protocol.process_start_time_ticks(
                        expected_pid
                    ),
                    expected_socket_device=metadata.st_dev,
                    expected_socket_inode=metadata.st_ino,
                    host_boot_id_sha256=boot,
                    expected_launcher_handoff_receipt_sha256=expected_digest,
                )
                received = protocol.receive_worker_request(
                    connection,
                    handshake_deadline_monotonic_ns=(
                        time.monotonic_ns() + 4_000_000_000
                    ),
                    client_peer_policy=policy,
                )
                connection = None
                worker_state["executed"] = True
                protocol.send_worker_response(
                    received,
                    fixtures.make_success_response(
                        received.value,
                        now=dt.datetime.now(dt.timezone.utc),
                    ),
                )
                received = None
            except BaseException as error:
                worker_state["error"] = _error_code(error)
            finally:
                if received is not None:
                    received.close()
                _close_socket(connection)

        worker_thread = threading.Thread(target=worker, daemon=True)
        worker_thread.start()
        handoff._send_frame_with_fd(
            launcher_channel,
            raw,
            worker_client.fileno(),
            time.monotonic_ns() + 3_000_000_000,
        )
        launcher_channel.close()
        worker_client.close()
        coordinator_report = _read_report(report_read)
        _close_fd(report_read)
        _waited_pid, status = os.waitpid(coordinator_pid, 0)
        worker_thread.join(timeout=5)
        listener.close()

        self.assertTrue(os.WIFEXITED(status), coordinator_report)
        self.assertEqual(os.WEXITSTATUS(status), 0, coordinator_report)
        self.assertFalse(worker_thread.is_alive(), worker_state)
        self.assertEqual(worker_state["peer_pid"], launcher_pid)
        self.assertNotEqual(worker_state["peer_pid"], coordinator_pid)
        return coordinator_report, worker_state

    def test_handoff_origin_and_digest_binding_are_jointly_satisfiable(self) -> None:
        coordinator, worker = self._run_bridge("success")
        self.assertEqual(coordinator["code"], "ok")
        self.assertEqual(coordinator["status"], "succeeded")
        self.assertTrue(worker["executed"])
        self.assertIsNone(worker["error"])

    def test_wrong_and_missing_request_digest_fail_before_execute(self) -> None:
        expected_codes = {
            "wrong_request_digest": "WORKER_LAUNCHER_HANDOFF_MISMATCH",
            "missing_request_digest": "WORKER_PROTOCOL_SHAPE_INVALID",
        }
        for mode, expected_code in expected_codes.items():
            with self.subTest(mode=mode):
                coordinator, worker = self._run_bridge(mode)
                self.assertEqual(coordinator["code"], expected_code)
                self.assertFalse(worker["executed"])

    def test_independently_wrong_worker_receipt_pin_fails_before_execute(self) -> None:
        _coordinator, worker = self._run_bridge("wrong_worker_digest")
        self.assertEqual(worker["error"], "WORKER_LAUNCHER_HANDOFF_MISMATCH")
        self.assertFalse(worker["executed"])

    def test_old_policy_expectation_of_coordinator_peer_is_rejected(self) -> None:
        _coordinator, worker = self._run_bridge("old_coordinator_policy")
        self.assertEqual(worker["error"], "WORKER_PEER_CREDENTIAL_MISMATCH")
        self.assertFalse(worker["executed"])


if __name__ == "__main__":
    unittest.main()
