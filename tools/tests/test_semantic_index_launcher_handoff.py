from __future__ import annotations

import array
import copy
import datetime as dt
import fcntl
import hashlib
import inspect
import os
import pathlib
import socket
import struct
import sys
import time
import unittest


TOOLS_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import semantic_index_launcher_handoff as handoff  # noqa: E402


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
SHA_F = "f" * 64
GENERATION_A = "semantic-generation:" + "1" * 64
GENERATION_B = "semantic-generation:" + "2" * 64


def _timestamps() -> tuple[str, str]:
    issued = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    expires = issued + dt.timedelta(seconds=30)
    return issued.strftime("%Y-%m-%dT%H:%M:%SZ"), expires.strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _worker_binding(
    worker_socket: socket.socket,
    boot: str,
) -> handoff.ExpectedWorkerBinding:
    observation = handoff._observe_worker_socket(worker_socket, boot)
    return handoff.ExpectedWorkerBinding(
        protocol_revision=handoff.WORKER_PROTOCOL_REVISION,
        deployment_identity="semantic-worker.production.v1",
        runtime_image="registry.example/semantic-worker@sha256:" + "9" * 64,
        whoami_sha256=SHA_A,
        peer_attestation_sha256=SHA_B,
        runtime_attestation_sha256=SHA_C,
        host_boot_id_sha256=boot,
        **handoff.dataclasses.asdict(observation),
    )


def _identity_binding(uid: int, gid: int, pid: int, boot: str) -> dict[str, int | str]:
    start = handoff.process_start_time_ticks(pid)
    return {
        "uid": uid,
        "gid": gid,
        "pid": pid,
        "process_start_time_ticks": start,
        "process_start_token_sha256": handoff.process_start_token_sha256(
            boot, pid, start
        ),
    }


def _receipt(
    *,
    boot: str,
    launcher_binding: dict,
    coordinator_binding: dict,
    worker: handoff.ExpectedWorkerBinding,
    issued_at: str,
    expires_at: str,
    execution_plan_sha256: str = SHA_D,
    reviewed_job_sha256: str = SHA_E,
    approval_basis_sha256: str = SHA_F,
    generation_id: str = GENERATION_A,
) -> dict:
    return {
        "schema": handoff.HANDOFF_RECEIPT_SCHEMA,
        "protocol": handoff.HANDOFF_PROTOCOL,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "host_boot_id_sha256": boot,
        "launcher_binding": launcher_binding,
        "coordinator_binding": coordinator_binding,
        "execution_binding": {
            "execution_plan_sha256": execution_plan_sha256,
            "reviewed_job_sha256": reviewed_job_sha256,
            "approval_basis_sha256": approval_basis_sha256,
            "generation_id": generation_id,
        },
        "worker_binding": worker.as_dict(),
        "serialization_contract": dict(handoff._SERIALIZATION_CONTRACT),
    }


def _policy(
    receipt: dict,
    worker: handoff.ExpectedWorkerBinding,
) -> handoff.LauncherHandoffPolicy:
    launcher = receipt["launcher_binding"]
    execution = receipt["execution_binding"]
    raw = handoff.canonical_json_bytes(receipt)
    return handoff.LauncherHandoffPolicy(
        expected_launcher_uid=launcher["uid"],
        expected_launcher_gid=launcher["gid"],
        expected_launcher_pid=launcher["pid"],
        expected_launcher_process_start_time_ticks=launcher[
            "process_start_time_ticks"
        ],
        expected_launcher_process_start_token_sha256=launcher[
            "process_start_token_sha256"
        ],
        host_boot_id_sha256=receipt["host_boot_id_sha256"],
        expected_receipt_sha256=hashlib.sha256(raw).hexdigest(),
        execution_plan_sha256=execution["execution_plan_sha256"],
        reviewed_job_sha256=execution["reviewed_job_sha256"],
        approval_basis_sha256=execution["approval_basis_sha256"],
        generation_id=execution["generation_id"],
        worker=worker,
    )


def _fd_count() -> int:
    return len(os.listdir("/proc/self/fd"))


def _close_fd(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        pass


def _close_socket(sock: socket.socket) -> None:
    try:
        sock.close()
    except OSError:
        pass


class CanonicalReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        if os.name != "posix" or not hasattr(socket, "SO_PEERCRED"):
            self.skipTest("Linux SO_PEERCRED is required")
        self.boot = handoff.current_host_boot_id_sha256()
        self.worker_socket, self.worker_peer = socket.socketpair()
        self.worker = _worker_binding(self.worker_socket, self.boot)
        issued, expires = _timestamps()
        launcher = _identity_binding(os.geteuid(), os.getegid(), os.getpid(), self.boot)
        coordinator = {
            **launcher,
            "parent_pid": launcher["pid"],
        }
        self.receipt = _receipt(
            boot=self.boot,
            launcher_binding=launcher,
            coordinator_binding=coordinator,
            worker=self.worker,
            issued_at=issued,
            expires_at=expires,
        )

    def tearDown(self) -> None:
        _close_socket(self.worker_socket)
        _close_socket(self.worker_peer)

    def test_closed_canonical_receipt_and_duplicate_rejection(self) -> None:
        raw = handoff.canonical_json_bytes(self.receipt)
        self.assertEqual(handoff._validate_receipt_shape(handoff.decode_canonical_json(raw)), self.receipt)
        noncanonical = b'{"protocol":"x", "protocol":"y"}'
        with self.assertRaisesRegex(handoff.LauncherHandoffError, "HANDOFF_JSON_INVALID"):
            handoff.decode_canonical_json(noncanonical)
        changed = copy.deepcopy(self.receipt)
        changed["unexpected"] = True
        with self.assertRaisesRegex(handoff.LauncherHandoffError, "HANDOFF_SHAPE_INVALID"):
            handoff._validate_receipt_shape(changed)

    def test_receipt_digest_is_out_of_band_and_exact(self) -> None:
        raw = handoff.canonical_json_bytes(self.receipt)
        digest = hashlib.sha256(raw).hexdigest()
        self.assertEqual(handoff._validate_receipt_digest(raw, digest), digest)
        with self.assertRaisesRegex(
            handoff.LauncherHandoffError, "HANDOFF_RECEIPT_DIGEST_MISMATCH"
        ):
            handoff._validate_receipt_digest(raw, SHA_A)

    def test_receipt_freshness_rejects_expired_future_and_long_ttl(self) -> None:
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        cases = [
            (
                now - dt.timedelta(seconds=30),
                now - dt.timedelta(seconds=1),
            ),
            (
                now + dt.timedelta(seconds=6),
                now + dt.timedelta(seconds=20),
            ),
            (
                now,
                now + dt.timedelta(seconds=handoff.MAX_HANDOFF_TTL_SECONDS + 1),
            ),
        ]
        for issued, expires in cases:
            with self.subTest(issued=issued, expires=expires):
                changed = copy.deepcopy(self.receipt)
                changed["issued_at"] = issued.strftime("%Y-%m-%dT%H:%M:%SZ")
                changed["expires_at"] = expires.strftime("%Y-%m-%dT%H:%M:%SZ")
                with self.assertRaisesRegex(
                    handoff.LauncherHandoffError, "HANDOFF_FRESHNESS_INVALID"
                ):
                    handoff._validate_receipt_freshness(changed, now)

    def test_receipt_binds_plan_job_basis_generation_and_worker(self) -> None:
        launcher_runtime = handoff._RuntimeIdentity(
            uid=self.receipt["launcher_binding"]["uid"],
            gid=self.receipt["launcher_binding"]["gid"],
            pid=self.receipt["launcher_binding"]["pid"],
            start_ticks=self.receipt["launcher_binding"]["process_start_time_ticks"],
            start_token_sha256=self.receipt["launcher_binding"][
                "process_start_token_sha256"
            ],
        )
        coordinator_runtime = handoff._RuntimeIdentity(
            uid=self.receipt["coordinator_binding"]["uid"],
            gid=self.receipt["coordinator_binding"]["gid"],
            pid=self.receipt["coordinator_binding"]["pid"],
            start_ticks=self.receipt["coordinator_binding"][
                "process_start_time_ticks"
            ],
            start_token_sha256=self.receipt["coordinator_binding"][
                "process_start_token_sha256"
            ],
        )
        observation = handoff._observe_worker_socket(self.worker_socket, self.boot)
        policy = _policy(self.receipt, self.worker)
        handoff._validate_receipt_bindings(
            self.receipt,
            policy,
            launcher_runtime,
            coordinator_runtime,
            observation,
        )

        execution_cases = {
            "execution_plan_sha256": SHA_A,
            "reviewed_job_sha256": SHA_A,
            "approval_basis_sha256": SHA_A,
            "generation_id": GENERATION_B,
        }
        for key, replacement in execution_cases.items():
            with self.subTest(key=key):
                changed = copy.deepcopy(self.receipt)
                changed["execution_binding"][key] = replacement
                with self.assertRaisesRegex(
                    handoff.LauncherHandoffError,
                    "HANDOFF_EXECUTION_BINDING_MISMATCH",
                ):
                    handoff._validate_receipt_bindings(
                        changed,
                        policy,
                        launcher_runtime,
                        coordinator_runtime,
                        observation,
                    )

        changed = copy.deepcopy(self.receipt)
        changed["worker_binding"]["deployment_identity"] = "substituted-worker"
        with self.assertRaisesRegex(
            handoff.LauncherHandoffError, "HANDOFF_WORKER_BINDING_MISMATCH"
        ):
            handoff._validate_receipt_bindings(
                changed,
                policy,
                launcher_runtime,
                coordinator_runtime,
                observation,
            )

        changed = copy.deepcopy(self.receipt)
        changed["launcher_binding"]["pid"] += 1
        with self.assertRaisesRegex(
            handoff.LauncherHandoffError, "HANDOFF_LAUNCHER_BINDING_MISMATCH"
        ):
            handoff._validate_receipt_bindings(
                changed,
                policy,
                launcher_runtime,
                coordinator_runtime,
                observation,
            )
        changed = copy.deepcopy(self.receipt)
        changed["coordinator_binding"]["parent_pid"] += 1
        with self.assertRaisesRegex(
            handoff.LauncherHandoffError, "HANDOFF_COORDINATOR_BINDING_MISMATCH"
        ):
            handoff._validate_receipt_bindings(
                changed,
                policy,
                launcher_runtime,
                coordinator_runtime,
                observation,
            )

    def test_observed_worker_peer_credentials_are_not_receipt_echoes(self) -> None:
        observation = handoff._observe_worker_socket(self.worker_socket, self.boot)
        values = self.worker.as_dict()
        values["peer_uid"] += 1
        forged = handoff.ExpectedWorkerBinding(**values)
        with self.assertRaisesRegex(
            handoff.LauncherHandoffError,
            "HANDOFF_WORKER_SOCKET_BINDING_MISMATCH",
        ):
            handoff._validate_worker_observation(observation, forged)

    def test_policy_rejects_inconsistent_process_and_socket_tokens(self) -> None:
        values = self.worker.as_dict()
        values["process_start_token_sha256"] = SHA_A
        with self.assertRaisesRegex(handoff.LauncherHandoffError, "HANDOFF_POLICY_INVALID"):
            handoff.ExpectedWorkerBinding(**values)
        values = self.worker.as_dict()
        values["socket_inode_binding_sha256"] = SHA_A
        with self.assertRaisesRegex(handoff.LauncherHandoffError, "HANDOFF_POLICY_INVALID"):
            handoff.ExpectedWorkerBinding(**values)

        policy = _policy(self.receipt, self.worker)
        values = handoff.dataclasses.asdict(policy)
        values["worker"] = self.worker
        values["expected_launcher_process_start_token_sha256"] = SHA_A
        with self.assertRaisesRegex(handoff.LauncherHandoffError, "HANDOFF_POLICY_INVALID"):
            handoff.LauncherHandoffPolicy(**values)

    def test_opaque_handoff_rejects_normal_construction(self) -> None:
        with self.assertRaises(TypeError):
            handoff.AttestedWorkerHandoff(object(), self.worker_socket, b"{}")
        self.assertNotIn("_CONSTRUCTION_SENTINEL", handoff.__all__)

    def test_public_receiver_has_no_path_or_test_bypass(self) -> None:
        parameters = set(inspect.signature(handoff.receive_attested_worker_handoff).parameters)
        self.assertEqual(parameters, {"handoff_channel", "policy"})
        source = pathlib.Path(handoff.__file__).read_text(encoding="utf-8")
        for forbidden in (
            ".connect(",
            ".bind(",
            ".listen(",
            "subprocess",
            "os.system",
            "os.environ",
            "allow_unattested",
            "socket_path_for_test",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


class RawTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        if (
            os.name != "posix"
            or not hasattr(os, "fork")
            or not hasattr(socket, "MSG_CMSG_CLOEXEC")
        ):
            self.skipTest("Linux fork/SCM_RIGHTS support is required")

    def _run_transport_case(
        self,
        *,
        descriptor_count: int,
        trailing: bytes = b"",
    ) -> tuple[bytes, int]:
        receiver, sender = socket.socketpair()
        pipes = [os.pipe2(os.O_CLOEXEC) for _ in range(descriptor_count)]
        pid = os.fork()
        if pid == 0:
            try:
                receiver.close()
                raw = b'{"closed":"receipt"}'
                frame = struct.pack("!I", len(raw)) + raw
                ancillary = []
                if pipes:
                    rights = array.array("i", [read_fd for read_fd, _write_fd in pipes])
                    ancillary = [
                        (socket.SOL_SOCKET, socket.SCM_RIGHTS, rights.tobytes())
                    ]
                sender.sendmsg([frame], ancillary)
                if trailing:
                    sender.sendall(trailing)
                sender.shutdown(socket.SHUT_WR)
            except BaseException:
                pass
            finally:
                sender.close()
                for read_fd, write_fd in pipes:
                    _close_fd(read_fd)
                    _close_fd(write_fd)
                os._exit(0)
        sender.close()
        for read_fd, write_fd in pipes:
            _close_fd(read_fd)
            _close_fd(write_fd)
        try:
            return handoff._receive_frame_and_one_fd(receiver, time.monotonic_ns() + 2_000_000_000)
        finally:
            receiver.close()
            os.waitpid(pid, 0)

    def test_exactly_one_received_fd_is_cloexec(self) -> None:
        baseline = _fd_count()
        raw, descriptor = self._run_transport_case(descriptor_count=1)
        try:
            self.assertEqual(raw, b'{"closed":"receipt"}')
            self.assertTrue(fcntl.fcntl(descriptor, fcntl.F_GETFD) & fcntl.FD_CLOEXEC)
        finally:
            _close_fd(descriptor)
        self.assertEqual(_fd_count(), baseline)

    def test_missing_extra_and_trailing_fd_paths_close_every_descriptor(self) -> None:
        cases = [
            ("missing", 0, b"", "HANDOFF_ANCILLARY_INVALID"),
            ("extra", 2, b"", "HANDOFF_ANCILLARY_INVALID"),
            ("trailing", 1, b"x", "HANDOFF_TRAILING_DATA"),
            ("truncated-control", 8, b"", "HANDOFF_ANCILLARY"),
        ]
        for name, count, trailing, code in cases:
            with self.subTest(name=name):
                baseline = _fd_count()
                with self.assertRaisesRegex(handoff.LauncherHandoffError, code):
                    self._run_transport_case(
                        descriptor_count=count,
                        trailing=trailing,
                    )
                self.assertEqual(_fd_count(), baseline)

    def test_socket_substitution_is_rejected_even_with_same_peer_credentials(self) -> None:
        baseline = _fd_count()
        boot = handoff.current_host_boot_id_sha256()
        original, original_peer = socket.socketpair()
        substitute, substitute_peer = socket.socketpair()
        expected = _worker_binding(original, boot)
        receiver, sender = socket.socketpair()
        pid = os.fork()
        if pid == 0:
            try:
                receiver.close()
                original.close()
                original_peer.close()
                substitute_peer.close()
                raw = b'{"closed":"receipt"}'
                frame = struct.pack("!I", len(raw)) + raw
                rights = array.array("i", [substitute.fileno()])
                sender.sendmsg(
                    [frame],
                    [(socket.SOL_SOCKET, socket.SCM_RIGHTS, rights.tobytes())],
                )
                sender.shutdown(socket.SHUT_WR)
            finally:
                sender.close()
                substitute.close()
                os._exit(0)
        sender.close()
        substitute.close()
        try:
            raw, descriptor = handoff._receive_frame_and_one_fd(
                receiver, time.monotonic_ns() + 2_000_000_000
            )
            self.assertEqual(raw, b'{"closed":"receipt"}')
            received = socket.socket(fileno=descriptor)
            try:
                observation = handoff._observe_worker_socket(received, boot)
                self.assertEqual(observation.peer_pid, expected.peer_pid)
                self.assertEqual(observation.peer_uid, expected.peer_uid)
                with self.assertRaisesRegex(
                    handoff.LauncherHandoffError,
                    "HANDOFF_WORKER_SOCKET_BINDING_MISMATCH",
                ):
                    handoff._validate_worker_observation(observation, expected)
            finally:
                received.close()
        finally:
            receiver.close()
            original.close()
            original_peer.close()
            substitute_peer.close()
            os.waitpid(pid, 0)
        self.assertEqual(_fd_count(), baseline)

    def test_non_stream_unix_socket_is_rejected(self) -> None:
        left, right = socket.socketpair(type=socket.SOCK_DGRAM)
        try:
            with self.assertRaisesRegex(
                handoff.LauncherHandoffError, "HANDOFF_SOCKET_INVALID"
            ):
                handoff._require_connected_unix_stream(left, "test socket")
        finally:
            left.close()
            right.close()

    def test_sender_binds_channel_peer_to_attested_coordinator(self) -> None:
        left, right = socket.socketpair()
        try:
            boot = handoff.current_host_boot_id_sha256()
            coordinator = _identity_binding(
                os.geteuid(), os.getegid(), os.getpid(), boot
            )
            coordinator["parent_pid"] = os.getppid()
            handoff._validate_handoff_channel_peer(left, coordinator, boot)
            changed = dict(coordinator)
            changed["pid"] += 1
            with self.assertRaisesRegex(
                handoff.LauncherHandoffError,
                "HANDOFF_COORDINATOR_PEER_MISMATCH",
            ):
                handoff._validate_handoff_channel_peer(left, changed, boot)
        finally:
            left.close()
            right.close()

    def test_nonblocking_send_honors_absolute_deadline_when_peer_stalls(self) -> None:
        sender, receiver = socket.socketpair()
        try:
            sender.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            started = time.monotonic()
            with self.assertRaisesRegex(
                handoff.LauncherHandoffError, "HANDOFF_DEADLINE_EXCEEDED"
            ):
                handoff._send_all(
                    sender,
                    b"x" * handoff.MAX_HANDOFF_FRAME_BYTES,
                    time.monotonic_ns() + 50_000_000,
                )
            self.assertLess(time.monotonic() - started, 0.75)
        finally:
            sender.close()
            receiver.close()

    def test_invalid_second_descriptor_does_not_leak_owned_channel(self) -> None:
        baseline = _fd_count()
        channel, peer = socket.socketpair()
        invalid_descriptor = os.open("/dev/null", os.O_RDONLY | os.O_CLOEXEC)
        os.close(invalid_descriptor)
        with self.assertRaisesRegex(
            handoff.LauncherHandoffError, "HANDOFF_SOCKET_INVALID"
        ):
            handoff.send_attested_worker_handoff(channel, invalid_descriptor, {})
        self.assertEqual(channel.fileno(), -1)
        peer.close()
        self.assertEqual(_fd_count(), baseline)


class LauncherPeerGateTests(unittest.TestCase):
    def setUp(self) -> None:
        if (
            os.name != "posix"
            or not hasattr(os, "fork")
            or not hasattr(socket, "SO_PEERCRED")
        ):
            self.skipTest("Linux fork/SO_PEERCRED support is required")

    def _fork_receiver_rejection(self, mode: str) -> str:
        launcher_channel, coordinator_channel = socket.socketpair()
        worker_socket, worker_peer = socket.socketpair()
        result_read, result_write = os.pipe2(os.O_CLOEXEC)
        boot = handoff.current_host_boot_id_sha256()
        issued, expires = _timestamps()
        launcher_pid = os.getpid()
        launcher_uid = os.geteuid()
        launcher_gid = os.getegid()
        launcher_start = handoff.process_start_time_ticks(launcher_pid)
        child_pid = os.fork()
        if child_pid == 0:
            try:
                launcher_channel.close()
                _close_fd(result_read)
                expected_boot = SHA_A if mode == "wrong_boot" else boot
                expected_pid = launcher_pid + 100_000 if mode == "wrong_parent" else launcher_pid
                expected_uid = launcher_uid if mode == "same_uid" else launcher_uid + 1
                expected_start = launcher_start + 1 if mode == "wrong_start" else launcher_start
                expected_worker = _worker_binding(worker_socket, expected_boot)
                coordinator = _identity_binding(
                    os.geteuid(), os.getegid(), os.getpid(), expected_boot
                )
                coordinator["parent_pid"] = expected_pid
                launcher = {
                    "uid": expected_uid,
                    "gid": launcher_gid,
                    "pid": expected_pid,
                    "process_start_time_ticks": expected_start,
                    "process_start_token_sha256": handoff.process_start_token_sha256(
                        expected_boot, expected_pid, expected_start
                    ),
                }
                receipt = _receipt(
                    boot=expected_boot,
                    launcher_binding=launcher,
                    coordinator_binding=coordinator,
                    worker=expected_worker,
                    issued_at=issued,
                    expires_at=expires,
                )
                policy = _policy(receipt, expected_worker)
                worker_socket.close()
                worker_peer.close()
                try:
                    result = handoff.receive_attested_worker_handoff(
                        coordinator_channel,
                        policy=policy,
                    )
                except handoff.LauncherHandoffError as error:
                    code = error.code
                else:
                    result.close()
                    code = "UNEXPECTED_SUCCESS"
                os.write(result_write, code.encode("ascii"))
            except BaseException as error:
                os.write(result_write, ("CHILD_ERROR:" + type(error).__name__).encode("ascii"))
            finally:
                _close_fd(result_write)
                os._exit(0)

        coordinator_channel.close()
        _close_fd(result_write)
        # Send a bounded frame in case a regression incorrectly reaches the
        # transport read.  The receipt need not be accepted for these pre-read
        # policy/peer rejection cases.
        try:
            handoff._send_frame_with_fd(
                launcher_channel,
                b"{}",
                worker_socket.fileno(),
                time.monotonic_ns() + 1_000_000_000,
            )
        except handoff.LauncherHandoffError:
            pass
        launcher_channel.close()
        worker_socket.close()
        worker_peer.close()
        try:
            raw = os.read(result_read, 256)
        finally:
            _close_fd(result_read)
            os.waitpid(child_pid, 0)
        return raw.decode("ascii")

    def test_same_uid_launcher_forgery_is_rejected(self) -> None:
        self.assertEqual(
            self._fork_receiver_rejection("same_uid"),
            "HANDOFF_LAUNCHER_UID_NOT_PRIVILEGED",
        )

    def test_wrong_parent_pid_is_rejected(self) -> None:
        self.assertEqual(
            self._fork_receiver_rejection("wrong_parent"),
            "HANDOFF_PARENT_MISMATCH",
        )

    def test_wrong_launcher_start_is_rejected(self) -> None:
        self.assertEqual(
            self._fork_receiver_rejection("wrong_start"),
            "HANDOFF_LAUNCHER_START_MISMATCH",
        )

    def test_wrong_host_boot_is_rejected(self) -> None:
        self.assertEqual(
            self._fork_receiver_rejection("wrong_boot"),
            "HANDOFF_HOST_BOOT_MISMATCH",
        )


@unittest.skipUnless(
    os.name == "posix"
    and hasattr(os, "fork")
    and hasattr(socket, "SO_PEERCRED")
    and os.geteuid() == 0,
    "A safe positive privilege-separated fork requires a root test launcher",
)
class PrivilegedPositiveHandoffTests(unittest.TestCase):
    def test_root_parent_hands_one_socket_to_dropped_uid_child(self) -> None:
        target_uid = 65534
        target_gid = 65534
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        channel_address = (
            "\0simworld-handoff-"
            + str(os.getpid())
            + "-"
            + str(time.monotonic_ns())
        )
        listener.bind(channel_address)
        listener.listen(1)
        worker_socket, worker_peer = socket.socketpair()
        ready_read, ready_write = os.pipe2(os.O_CLOEXEC)
        result_read, result_write = os.pipe2(os.O_CLOEXEC)
        boot = handoff.current_host_boot_id_sha256()
        issued, expires = _timestamps()
        launcher_pid = os.getpid()
        launcher_binding = _identity_binding(os.geteuid(), os.getegid(), launcher_pid, boot)
        expected_worker = _worker_binding(worker_socket, boot)
        child_pid = os.fork()
        if child_pid == 0:
            try:
                listener.close()
                _close_fd(ready_read)
                _close_fd(result_read)
                os.setgroups([])
                os.setgid(target_gid)
                os.setuid(target_uid)
                coordinator_channel = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                coordinator_channel.connect(channel_address)
                os.write(ready_write, b"1")
                _close_fd(ready_write)
                coordinator = _identity_binding(target_uid, target_gid, os.getpid(), boot)
                coordinator["parent_pid"] = launcher_pid
                receipt = _receipt(
                    boot=boot,
                    launcher_binding=launcher_binding,
                    coordinator_binding=coordinator,
                    worker=expected_worker,
                    issued_at=issued,
                    expires_at=expires,
                )
                policy = _policy(receipt, expected_worker)
                worker_socket.close()
                worker_peer.close()
                result = handoff.receive_attested_worker_handoff(
                    coordinator_channel,
                    policy=policy,
                )
                received_worker = result.take_worker_socket()
                self_peer = handoff._peer_credentials(received_worker, "Worker")
                received_worker.close()
                result.close()
                os.write(
                    result_write,
                    ("ok:" + ":".join(str(value) for value in self_peer)).encode("ascii"),
                )
            except BaseException as error:
                os.write(result_write, ("error:" + repr(error)).encode("utf-8"))
            finally:
                _close_fd(result_write)
                os._exit(0)

        launcher_channel, _unused_address = listener.accept()
        listener.close()
        _close_fd(ready_write)
        _close_fd(result_write)
        self.assertEqual(os.read(ready_read, 1), b"1")
        _close_fd(ready_read)
        coordinator = _identity_binding(target_uid, target_gid, child_pid, boot)
        coordinator["parent_pid"] = launcher_pid
        receipt = _receipt(
            boot=boot,
            launcher_binding=launcher_binding,
            coordinator_binding=coordinator,
            worker=expected_worker,
            issued_at=issued,
            expires_at=expires,
        )
        expected_digest = hashlib.sha256(handoff.canonical_json_bytes(receipt)).hexdigest()
        observed_digest = handoff.send_attested_worker_handoff(
            launcher_channel,
            worker_socket,
            receipt,
        )
        self.assertEqual(observed_digest, expected_digest)
        worker_peer.close()
        try:
            result = os.read(result_read, 4096).decode("utf-8")
        finally:
            _close_fd(result_read)
            os.waitpid(child_pid, 0)
        self.assertTrue(result.startswith(f"ok:{launcher_pid}:0:"), result)


if __name__ == "__main__":
    unittest.main()
