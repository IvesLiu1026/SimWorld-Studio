from __future__ import annotations

import dataclasses
import fcntl
import json
import os
import pathlib
import stat
import sys
import tempfile
import threading
import unittest
from unittest import mock


TOOLS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

import semantic_index_durable_ledger as ledger  # noqa: E402


KEY = "sha256:" + "a" * 64
OTHER_KEY = "sha256:" + "b" * 64
IDENTITY = "worker-ledger:prod"
RESULT = ledger.canonical_json_bytes({"ok": True, "receipt": {"count": 2}})
OTHER_RESULT = ledger.canonical_json_bytes({"ok": True, "receipt": {"count": 3}})
TARGET = ledger.CredentialTarget(
    credential_generation="sha256:" + "c" * 64,
    target_identity="unreal-prod",
)
BINDING = ledger.LedgerBinding(
    job_revision="sha256:" + "d" * 64,
    generation_id="semantic-generation:" + "e" * 64,
    phase="inspect",
    operation="inspect_exact_assets",
    idempotency_key=KEY,
    credential_targets=(TARGET,),
)


def request_for(binding: ledger.LedgerBinding, nonce: int) -> bytes:
    targets = [
        {
            "credential_generation": target.credential_generation,
            "generation_id": binding.generation_id,
            "target_identity": target.target_identity,
        }
        for target in binding.credential_targets
    ]
    descriptors = [
        {
            "fd_index": index,
            "credential_generation": target.credential_generation,
            "generation_id": binding.generation_id,
            "target_identity": target.target_identity,
        }
        for index, target in enumerate(binding.credential_targets)
    ]
    return ledger.canonical_json_bytes(
        {
            "schema": "simworld-semantic-index-phase-request/v1",
            "protocol": "simworld-semantic-index-worker/v1",
            "job_revision": binding.job_revision,
            "generation_id": binding.generation_id,
            "phase": binding.phase,
            "operation": binding.operation,
            "idempotency_key": binding.idempotency_key,
            "expected_targets": targets,
            "credential_transport": {"descriptors": descriptors},
            "request_nonce": nonce,
        }
    )


REQUEST = request_for(BINDING, 1)
OTHER_REQUEST = request_for(BINDING, 2)
REQUEST_SHA = ledger.canonical_request_sha256(REQUEST)


def names(binding: ledger.LedgerBinding = BINDING) -> tuple[str, str]:
    digest = ledger.derive_ledger_key(binding).removeprefix("sha256:")
    return f"ledger-{digest}.prepare.json", f"ledger-{digest}.result.json"


class DirectoryFsyncFaultLedger(ledger.DurableIdempotencyLedger):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fail_next_directory_fsync = True

    def _fsync_directory(self) -> None:
        if self.fail_next_directory_fsync:
            self.fail_next_directory_fsync = False
            raise ledger.LedgerError(
                code="LEDGER_IO_ERROR",
                message=ledger.PUBLIC_ERROR_MESSAGES["LEDGER_IO_ERROR"],
            )
        super()._fsync_directory()


class DurableLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        os.chmod(self.root, 0o700)
        self.directory_fd = os.open(
            self.root,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
        )
        self.ledger = ledger.DurableIdempotencyLedger(
            self.directory_fd, IDENTITY
        )

    def tearDown(self) -> None:
        self.ledger.close()
        os.close(self.directory_fd)
        self.temporary.cleanup()

    def assert_error(self, code: str, callback) -> ledger.LedgerError:
        with self.assertRaises(ledger.LedgerError) as caught:
            callback()
        self.assertEqual(code, caught.exception.code)
        self.assertEqual(ledger.PUBLIC_ERROR_MESSAGES[code], caught.exception.message)
        return caught.exception

    def test_prepare_commit_replay_and_idempotent_result(self) -> None:
        first = self.ledger.begin(REQUEST)
        self.assertEqual("execute", first.action)
        self.assertEqual(ledger.derive_ledger_key(BINDING), first.ledger_key)
        self.assertEqual(REQUEST_SHA, first.request_sha256)
        self.assertIsNone(first.result_bytes)

        ambiguous = self.ledger.begin(REQUEST)
        self.assertEqual("recovery_required", ambiguous.action)
        self.assertIsNone(ambiguous.result_bytes)

        self.assertEqual(RESULT, self.ledger.commit_result(REQUEST, RESULT))
        replay = self.ledger.begin(REQUEST)
        self.assertEqual("replay", replay.status)
        self.assertEqual(RESULT, replay.result_bytes)
        self.assertEqual(RESULT, self.ledger.commit_result(REQUEST, RESULT))

        prepare_name, result_name = names()
        for filename in (prepare_name, result_name):
            metadata = (self.root / filename).lstat()
            self.assertTrue(stat.S_ISREG(metadata.st_mode))
            self.assertEqual(0o600, stat.S_IMODE(metadata.st_mode))
            self.assertEqual(os.geteuid(), metadata.st_uid)
        self.assertFalse(any(path.name.endswith(".tmp") for path in self.root.iterdir()))

    def test_exact_ledger_instance_rejects_method_override(self) -> None:
        self.assertFalse(hasattr(self.ledger, "__dict__"))
        with self.assertRaises(AttributeError):
            self.ledger.begin = lambda _request: None
        with self.assertRaises(AttributeError):
            self.ledger.commit_result = lambda _request, _result: None

    def test_same_key_different_request_is_a_permanent_conflict(self) -> None:
        self.assertEqual("execute", self.ledger.begin(REQUEST).action)
        error = self.assert_error(
            "LEDGER_KEY_CONFLICT",
            lambda: self.ledger.begin(OTHER_REQUEST),
        )
        self.assertNotIn(REQUEST_SHA, str(error))
        self.ledger.commit_result(REQUEST, RESULT)
        self.assert_error(
            "LEDGER_KEY_CONFLICT",
            lambda: self.ledger.begin(OTHER_REQUEST),
        )

    def test_prepared_crash_is_recovery_required_and_never_execute(self) -> None:
        self.assertEqual("execute", self.ledger.begin(REQUEST).action)
        self.ledger.close()
        self.ledger = ledger.DurableIdempotencyLedger(self.directory_fd, IDENTITY)
        outcome = self.ledger.begin(REQUEST)
        self.assertEqual("recovery_required", outcome.action)

    def test_prepare_link_visible_but_directory_fsync_fault_never_reexecutes(self) -> None:
        faulting = DirectoryFsyncFaultLedger(self.directory_fd, IDENTITY)
        try:
            self.assert_error("LEDGER_IO_ERROR", lambda: faulting.begin(REQUEST))
        finally:
            faulting.close()
        prepare_name, _result_name = names()
        self.assertTrue((self.root / prepare_name).is_file())
        outcome = self.ledger.begin(REQUEST)
        self.assertEqual("recovery_required", outcome.action)
        self.assertIsNone(outcome.result_bytes)

    def test_result_link_visible_but_directory_fsync_fault_replays_exact(self) -> None:
        self.assertEqual("execute", self.ledger.begin(REQUEST).action)
        faulting = DirectoryFsyncFaultLedger(self.directory_fd, IDENTITY)
        try:
            self.assert_error(
                "LEDGER_IO_ERROR",
                lambda: faulting.commit_result(REQUEST, RESULT),
            )
        finally:
            faulting.close()
        _prepare_name, result_name = names()
        self.assertTrue((self.root / result_name).is_file())
        with mock.patch.object(
            ledger.DurableIdempotencyLedger,
            "_fsync_directory",
            wraps=self.ledger._fsync_directory,
        ) as confirm_durable:
            outcome = self.ledger.begin(REQUEST)
        self.assertGreaterEqual(confirm_durable.call_count, 1)
        self.assertEqual("replay", outcome.action)
        self.assertEqual(RESULT, outcome.result_bytes)

    def test_commit_requires_matching_prepare_and_result_is_immutable(self) -> None:
        self.assert_error(
            "LEDGER_PREPARE_REQUIRED",
            lambda: self.ledger.commit_result(REQUEST, RESULT),
        )
        self.ledger.begin(REQUEST)
        self.assert_error(
            "LEDGER_KEY_CONFLICT",
            lambda: self.ledger.commit_result(OTHER_REQUEST, RESULT),
        )
        self.ledger.commit_result(REQUEST, RESULT)
        self.assert_error(
            "LEDGER_RESULT_CONFLICT",
            lambda: self.ledger.commit_result(REQUEST, OTHER_RESULT),
        )

    def test_canonical_json_rejects_duplicates_floats_controls_and_spacing(self) -> None:
        invalid = (
            b'{"a":1,"a":2}',
            b'{"a":1.0}',
            b'{"a":"\\n"}',
            b'{ "a": 1 }',
            b'{"a":1}\n',
        )
        for raw in invalid:
            with self.subTest(raw=raw):
                self.assert_error(
                    "LEDGER_INPUT_INVALID",
                    lambda raw=raw: ledger.canonical_request_sha256(raw),
                )

        self.ledger.begin(REQUEST)
        for raw in invalid:
            with self.subTest(result=raw):
                self.assert_error(
                    "LEDGER_INPUT_INVALID",
                    lambda raw=raw: self.ledger.commit_result(REQUEST, raw),
                )

    def test_closed_key_digest_and_identity_validation_do_not_echo_values(self) -> None:
        secret = "secret-value\n"
        invalid_key_binding = dataclasses.replace(
            BINDING, idempotency_key="sha256:" + "A" * 64
        )
        invalid_target_binding = dataclasses.replace(
            BINDING,
            credential_targets=(
                ledger.CredentialTarget(
                    credential_generation="sha256:" + "c" * 63,
                    target_identity="unreal-prod",
                ),
            ),
        )
        for callback in (
            lambda: self.ledger.begin(request_for(invalid_key_binding, 1)),
            lambda: self.ledger.begin(request_for(invalid_target_binding, 1)),
            lambda: ledger.DurableIdempotencyLedger(self.directory_fd, secret),
        ):
            error = self.assert_error("LEDGER_INPUT_INVALID", callback)
            self.assertNotIn(secret, str(error))

    def test_composite_key_binds_every_tuple_field_and_target_order(self) -> None:
        alternatives = (
            dataclasses.replace(BINDING, job_revision="sha256:" + "1" * 64),
            dataclasses.replace(
                BINDING, generation_id="semantic-generation:" + "2" * 64
            ),
            dataclasses.replace(BINDING, idempotency_key=OTHER_KEY),
            dataclasses.replace(
                BINDING,
                credential_targets=(
                    dataclasses.replace(
                        TARGET, credential_generation="sha256:" + "3" * 64
                    ),
                ),
            ),
            dataclasses.replace(
                BINDING,
                credential_targets=(
                    dataclasses.replace(TARGET, target_identity="unreal-secondary"),
                ),
            ),
            dataclasses.replace(
                BINDING,
                phase="render",
                operation="render_exact_views",
            ),
        )
        keys = {ledger.derive_ledger_key(BINDING)}
        keys.update(ledger.derive_ledger_key(binding) for binding in alternatives)
        self.assertEqual(1 + len(alternatives), len(keys))

        targets = tuple(
            ledger.CredentialTarget(
                credential_generation="sha256:" + format(index, "064x"),
                target_identity=f"target-{index}",
            )
            for index in (1, 2, 3)
        )
        reconcile = dataclasses.replace(
            BINDING,
            phase="reconcile",
            operation="reconcile_exact_snapshot",
            credential_targets=targets,
        )
        reordered = dataclasses.replace(
            reconcile, credential_targets=tuple(reversed(targets))
        )
        self.assertNotEqual(
            ledger.derive_ledger_key(reconcile), ledger.derive_ledger_key(reordered)
        )

        self.assert_error(
            "LEDGER_INPUT_INVALID",
            lambda: ledger.derive_ledger_key(
                dataclasses.replace(BINDING, operation="render_exact_views")
            ),
        )
        self.assert_error(
            "LEDGER_INPUT_INVALID",
            lambda: ledger.derive_ledger_key(
                dataclasses.replace(reconcile, credential_targets=targets[:2])
            ),
        )

    def test_request_binding_is_derived_and_descriptor_order_is_cross_checked(self) -> None:
        self.assertEqual(BINDING, ledger.binding_from_canonical_request(REQUEST))
        value = json.loads(REQUEST.decode("utf-8"))
        value["credential_transport"]["descriptors"][0]["target_identity"] = "substituted"
        self.assert_error(
            "LEDGER_INPUT_INVALID",
            lambda: self.ledger.begin(ledger.canonical_json_bytes(value)),
        )

        value = json.loads(REQUEST.decode("utf-8"))
        value["credential_transport"]["descriptors"][0]["fd_index"] = 1
        self.assert_error(
            "LEDGER_INPUT_INVALID",
            lambda: self.ledger.begin(ledger.canonical_json_bytes(value)),
        )

    def test_json_integer_domain_is_i_json_safe(self) -> None:
        self.assertEqual(
            b'{"n":9007199254740991}',
            ledger.canonical_json_bytes({"n": 2**53 - 1}),
        )
        self.assert_error(
            "LEDGER_INPUT_INVALID",
            lambda: ledger.canonical_json_bytes({"n": 2**53}),
        )

    def test_corrupt_noncanonical_symlink_and_mode_fail_closed(self) -> None:
        prepare_name, _result_name = names()
        self.ledger.begin(REQUEST)
        prepare_path = self.root / prepare_name
        original = prepare_path.read_bytes()

        prepare_path.write_bytes(original + b"\n")
        self.assert_error(
            "LEDGER_ENTRY_CORRUPT",
            lambda: self.ledger.begin(REQUEST),
        )

        prepare_path.write_bytes(original)
        os.chmod(prepare_path, 0o644)
        self.assert_error(
            "LEDGER_ENTRY_UNSAFE",
            lambda: self.ledger.begin(REQUEST),
        )

        prepare_path.unlink()
        target = self.root / "target"
        target.write_bytes(original)
        os.chmod(target, 0o600)
        prepare_path.symlink_to(target.name)
        self.assert_error(
            "LEDGER_ENTRY_UNSAFE",
            lambda: self.ledger.begin(REQUEST),
        )

        prepare_path.unlink()
        os.link(target, prepare_path)
        self.assert_error(
            "LEDGER_ENTRY_UNSAFE",
            lambda: self.ledger.begin(REQUEST),
        )

    def test_wrong_schema_exact_keys_and_orphan_result_are_corrupt(self) -> None:
        prepare_name, result_name = names()
        record = {
            "schema": ledger.PREPARE_SCHEMA,
            "ledger_identity": IDENTITY,
            "ledger_key": ledger.derive_ledger_key(BINDING),
            "binding": {
                "schema": "simworld-semantic-index-ledger-binding/v1",
                "job_revision": BINDING.job_revision,
                "generation_id": BINDING.generation_id,
                "phase": BINDING.phase,
                "operation": BINDING.operation,
                "idempotency_key": BINDING.idempotency_key,
                "credential_targets": [
                    {
                        "credential_generation": TARGET.credential_generation,
                        "target_identity": TARGET.target_identity,
                    }
                ],
            },
            "request_sha256": REQUEST_SHA,
            "extra": False,
        }
        (self.root / prepare_name).write_bytes(ledger.canonical_json_bytes(record))
        os.chmod(self.root / prepare_name, 0o600)
        self.assert_error(
            "LEDGER_ENTRY_CORRUPT",
            lambda: self.ledger.begin(REQUEST),
        )

        (self.root / prepare_name).unlink()
        (self.root / result_name).write_bytes(ledger.canonical_json_bytes({"orphan": True}))
        os.chmod(self.root / result_name, 0o600)
        self.assert_error(
            "LEDGER_ENTRY_CORRUPT",
            lambda: self.ledger.begin(REQUEST),
        )

    def test_private_directory_owner_and_mode_are_enforced_continuously(self) -> None:
        os.chmod(self.root, 0o750)
        self.assert_error(
            "LEDGER_DIRECTORY_UNSAFE",
            lambda: self.ledger.begin(REQUEST),
        )
        os.chmod(self.root, 0o700)
        self.assert_error(
            "LEDGER_DIRECTORY_UNSAFE",
            lambda: ledger.DurableIdempotencyLedger(
                self.directory_fd,
                IDENTITY,
                expected_uid=os.geteuid() + 1,
            ),
        )

    def test_two_writers_use_no_replace_publish(self) -> None:
        other = ledger.DurableIdempotencyLedger(self.directory_fd, IDENTITY)
        barrier = threading.Barrier(2)
        outcomes: list[str] = []
        errors: list[BaseException] = []

        def writer(instance: ledger.DurableIdempotencyLedger) -> None:
            try:
                barrier.wait(timeout=5)
                outcomes.append(instance.begin(REQUEST).action)
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

    def test_two_writers_with_different_requests_conflict(self) -> None:
        other = ledger.DurableIdempotencyLedger(self.directory_fd, IDENTITY)
        barrier = threading.Barrier(2)
        outcomes: list[str] = []

        def writer(instance: ledger.DurableIdempotencyLedger, request: bytes) -> None:
            try:
                barrier.wait(timeout=5)
                outcomes.append(instance.begin(request).action)
            except ledger.LedgerError as error:
                outcomes.append(error.code)

        threads = [
            threading.Thread(target=writer, args=(self.ledger, REQUEST)),
            threading.Thread(target=writer, args=(other, OTHER_REQUEST)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        other.close()
        self.assertEqual(["LEDGER_KEY_CONFLICT", "execute"], sorted(outcomes))

    def test_directory_fd_is_duplicated_cloexec_and_lifecycle_is_owned(self) -> None:
        owned = self.ledger.fileno()
        self.assertNotEqual(self.directory_fd, owned)
        self.assertTrue(fcntl.fcntl(owned, fcntl.F_GETFD) & fcntl.FD_CLOEXEC)

        self.ledger.close()
        os.fstat(self.directory_fd)
        self.assert_error(
            "LEDGER_CLOSED",
            lambda: self.ledger.begin(REQUEST),
        )

        replacement = ledger.DurableIdempotencyLedger(self.directory_fd, IDENTITY)
        os.close(self.directory_fd)
        self.directory_fd = os.open(
            self.root,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
        )
        self.ledger = replacement
        other_binding = dataclasses.replace(BINDING, idempotency_key=OTHER_KEY)
        self.assertEqual(
            "execute", self.ledger.begin(request_for(other_binding, 1)).action
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
