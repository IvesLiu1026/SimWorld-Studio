from __future__ import annotations

import base64
import builtins
import copy
import dataclasses
import datetime as dt
import hashlib
import json
import pathlib
import pickle
import socket
import sys
import unittest
from unittest import mock


TOOLS_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import semantic_index_launcher_verification as verification  # noqa: E402
import semantic_index_launcher_handoff as launcher_handoff  # noqa: E402
import semantic_index_schema_runtime as schema_runtime  # noqa: E402

try:  # Deliberately unavailable under /usr/bin/python3 -I -S.
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
except ModuleNotFoundError:  # pragma: no cover - system-isolated test path
    serialization = None
    Ed25519PrivateKey = None


SCHEMA_PATH = TOOLS_DIR / "semantic_index_launcher_verification_receipt_schema.json"
STATE_SCHEMA_PATH = TOOLS_DIR / "semantic_asset_index_execution_state_v2_schema.json"
TERMINAL_SCHEMA_PATH = TOOLS_DIR / "semantic_asset_index_terminal_receipt_v2_schema.json"
NOW = dt.datetime(2026, 7, 21, 12, 0, 30, tzinfo=dt.timezone.utc)


def sha(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def revision(label: str) -> str:
    return "sha256:" + sha(label)


def unsigned_receipt(
    public_key_bytes: bytes,
    *,
    handoff_receipt_sha256: str | None = None,
) -> dict:
    return {
        "schema": verification.RECEIPT_SCHEMA,
        "receipt_id": "semantic-index-launcher-verification:fixture-001",
        "issuer": {
            "issuer_id": "deployment-attestor:semantic-index",
            "role": "deployment_launcher_attestor",
            "key_id": "launcher-key:production-001",
            "key_revision": revision("launcher-key"),
        },
        "trust_bundle": {
            "schema": "simworld-launcher-verification-trust-bundle/v1",
            "revision": revision("launcher-trust-bundle"),
            "sha256": sha("launcher-trust-bundle"),
            "issuer_membership_sha256": sha("launcher-membership"),
            "issuer_public_key_sha256": hashlib.sha256(public_key_bytes).hexdigest(),
        },
        "launcher": {
            "launcher_identity": "semantic-index-launcher:production",
            "launcher_uid": 0,
            "coordinator_uid": 1000,
        },
        "handoff": {
            "receipt_schema": verification.HANDOFF_RECEIPT_SCHEMA,
            "raw_receipt_sha256": (
                sha("raw-handoff-receipt")
                if handoff_receipt_sha256 is None
                else handoff_receipt_sha256
            ),
            "opaque_connected_fd_handoff_verified": True,
        },
        "pins": {
            "interpreter_sha256": sha("interpreter"),
            "coordinator_source_closure_sha256": sha("coordinator-source-closure"),
            "adapter_source_sha256": sha("adapter-source"),
            "worker_protocol_sha256": sha("worker-protocol"),
            "worker_source_closure_sha256": sha("worker-source-closure"),
            "schemas_bundle_sha256": sha("schemas-bundle"),
            "dependency_lock_sha256": sha("dependency-lock"),
            "sql_bundle_sha256": sha("sql-bundle"),
            "plan_sha256": sha("execution-plan"),
            "worker_image": "registry.example/semantic-worker@sha256:" + sha("worker-image"),
            "worker_runtime_attestation_sha256": sha("worker-runtime-attestation"),
        },
        "verification": {"verified_before_executor_start": True},
        "issued_at": "2026-07-21T12:00:00Z",
        "not_before": "2026-07-21T12:00:05Z",
        "expires_at": "2026-07-21T12:02:00Z",
        "serialization_contract": copy.deepcopy(verification.SERIALIZATION_CONTRACT),
    }


def attach_signature(unsigned: dict, signature_bytes: bytes) -> tuple[dict, bytes]:
    value = copy.deepcopy(unsigned)
    signed_payload = verification.canonical_json_bytes(value)
    value["signature"] = {
        "algorithm": "ed25519",
        "canonicalization": "rfc8785-jcs-v1",
        "signed_payload_sha256": hashlib.sha256(signed_payload).hexdigest(),
        "detached_signature_sha256": hashlib.sha256(signature_bytes).hexdigest(),
        "detached_signature_base64": base64.b64encode(signature_bytes).decode("ascii"),
    }
    return value, verification.canonical_json_bytes(value)


def fake_receipt(public_key_bytes: bytes = bytes(range(32))) -> tuple[dict, bytes]:
    return attach_signature(unsigned_receipt(public_key_bytes), bytes(range(64)))


def trust_key(receipt: dict, public_key_bytes: bytes) -> verification.TrustedEd25519Key:
    issuer = receipt["issuer"]
    trust = receipt["trust_bundle"]
    return verification.TrustedEd25519Key(
        issuer_id=issuer["issuer_id"],
        key_id=issuer["key_id"],
        key_revision=issuer["key_revision"],
        trust_bundle_revision=trust["revision"],
        trust_bundle_sha256=trust["sha256"],
        issuer_membership_sha256=trust["issuer_membership_sha256"],
        public_key_bytes=public_key_bytes,
    )


def expected_pins(receipt: dict, raw: bytes) -> verification.ExpectedLauncherVerificationPins:
    issuer = receipt["issuer"]
    trust = receipt["trust_bundle"]
    launcher = receipt["launcher"]
    pins = receipt["pins"]
    return verification.ExpectedLauncherVerificationPins(
        raw_receipt_sha256=hashlib.sha256(raw).hexdigest(),
        receipt_id=receipt["receipt_id"],
        issuer_id=issuer["issuer_id"],
        issuer_key_id=issuer["key_id"],
        issuer_key_revision=issuer["key_revision"],
        trust_bundle_revision=trust["revision"],
        trust_bundle_sha256=trust["sha256"],
        issuer_membership_sha256=trust["issuer_membership_sha256"],
        issuer_public_key_sha256=trust["issuer_public_key_sha256"],
        launcher_identity=launcher["launcher_identity"],
        launcher_uid=launcher["launcher_uid"],
        coordinator_uid=launcher["coordinator_uid"],
        handoff_receipt_sha256=receipt["handoff"]["raw_receipt_sha256"],
        interpreter_sha256=pins["interpreter_sha256"],
        coordinator_source_closure_sha256=pins["coordinator_source_closure_sha256"],
        adapter_source_sha256=pins["adapter_source_sha256"],
        worker_protocol_sha256=pins["worker_protocol_sha256"],
        worker_source_closure_sha256=pins["worker_source_closure_sha256"],
        schemas_bundle_sha256=pins["schemas_bundle_sha256"],
        dependency_lock_sha256=pins["dependency_lock_sha256"],
        sql_bundle_sha256=pins["sql_bundle_sha256"],
        plan_sha256=pins["plan_sha256"],
        worker_image=pins["worker_image"],
        worker_runtime_attestation_sha256=pins["worker_runtime_attestation_sha256"],
    )


def projection_schema(path: pathlib.Path) -> dict:
    source = json.loads(path.read_text(encoding="utf-8"))
    return {
        "$schema": schema_runtime.DRAFT_2020_12,
        "$id": "urn:simworld:test:" + path.stem,
        **copy.deepcopy(source["properties"]["launcher_verification"]),
        "$defs": copy.deepcopy(source["$defs"]),
    }


class SchemaAndCanonicalParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    def assert_code(self, code: str, callback) -> verification.LauncherVerificationError:
        with self.assertRaises(verification.LauncherVerificationError) as captured:
            callback()
        self.assertEqual(code, captured.exception.code)
        return captured.exception

    def test_closed_schema_is_meta_valid_and_fixture_valid(self) -> None:
        schema_runtime.validate_schema(self.schema)
        receipt, raw = fake_receipt()
        self.assertEqual(verification.decode_canonical_json(raw), receipt)
        schema_runtime.validate_instance(self.schema, receipt)

    def test_state_and_terminal_projection_schemas_remain_compatible(self) -> None:
        receipt, _raw = fake_receipt()
        expected = expected_pins(receipt, verification.canonical_json_bytes(receipt))
        state_projection = {
            "receipt_schema": verification.RECEIPT_SCHEMA,
            "receipt_sha256": expected.raw_receipt_sha256,
            "launcher_identity": expected.launcher_identity,
            "launcher_uid": expected.launcher_uid,
            "coordinator_uid": expected.coordinator_uid,
            "opaque_handoff_verified": True,
            "source_closure_sha256": expected.coordinator_source_closure_sha256,
            "plan_sha256": expected.plan_sha256,
            "runtime_attestation_sha256": expected.worker_runtime_attestation_sha256,
        }
        terminal_projection = {
            "receipt_schema": verification.RECEIPT_SCHEMA,
            "raw_receipt_sha256": expected.raw_receipt_sha256,
            "signature_verified": True,
            "trust_bundle_sha256": expected.trust_bundle_sha256,
            "interpreter_sha256": expected.interpreter_sha256,
            "coordinator_source_closure_sha256": expected.coordinator_source_closure_sha256,
            "adapter_source_sha256": expected.adapter_source_sha256,
            "worker_protocol_sha256": expected.worker_protocol_sha256,
            "worker_source_closure_sha256": expected.worker_source_closure_sha256,
            "schemas_bundle_sha256": expected.schemas_bundle_sha256,
            "dependency_lock_sha256": expected.dependency_lock_sha256,
            "sql_bundle_sha256": expected.sql_bundle_sha256,
            "plan_sha256": expected.plan_sha256,
            "worker_image": expected.worker_image,
            "worker_runtime_attestation_sha256": expected.worker_runtime_attestation_sha256,
            "opaque_connected_fd_handoff_verified": True,
            "verified_before_executor_start": True,
        }
        state_schema = projection_schema(STATE_SCHEMA_PATH)
        terminal_schema = projection_schema(TERMINAL_SCHEMA_PATH)
        schema_runtime.validate_schema(state_schema)
        schema_runtime.validate_schema(terminal_schema)
        schema_runtime.validate_instance(state_schema, state_projection)
        schema_runtime.validate_instance(terminal_schema, terminal_projection)

    def test_duplicate_float_noncanonical_non_nfc_and_control_fail_closed(self) -> None:
        cases = (
            ("VERIFICATION_JSON_INVALID", b'{"x":1,"x":2}'),
            ("VERIFICATION_JSON_INVALID", b'{"x":1.0}'),
            ("VERIFICATION_JSON_NONCANONICAL", b'{"x": 1}'),
            (
                "VERIFICATION_VALUE_INVALID",
                json.dumps({"x": "e\u0301"}, ensure_ascii=False, separators=(",", ":")).encode(),
            ),
            ("VERIFICATION_VALUE_INVALID", b'{"x":"\\u0001"}'),
        )
        for code, raw in cases:
            with self.subTest(code=code, raw=raw):
                self.assert_code(code, lambda raw=raw: verification.decode_canonical_json(raw))

    def test_unknown_fields_and_forbidden_claims_fail_closed(self) -> None:
        receipt, _raw = fake_receipt()
        receipt["unknown"] = True
        self.assert_code(
            "VERIFICATION_SHAPE_INVALID",
            lambda: verification._validate_receipt_shape(receipt),
        )
        receipt, _raw = fake_receipt()
        receipt["verification"]["verified_before_executor_start"] = False
        self.assert_code(
            "VERIFICATION_ORDER_INVALID",
            lambda: verification._validate_receipt_shape(receipt),
        )
        receipt, _raw = fake_receipt()
        receipt["launcher"]["coordinator_uid"] = receipt["launcher"]["launcher_uid"]
        self.assert_code(
            "VERIFICATION_LAUNCHER_INVALID",
            lambda: verification._validate_receipt_shape(receipt),
        )

        receipt, _raw = fake_receipt()
        short_signature = b"x" * 63
        receipt["signature"]["detached_signature_base64"] = base64.b64encode(
            short_signature
        ).decode("ascii")
        receipt["signature"]["detached_signature_sha256"] = hashlib.sha256(
            short_signature
        ).hexdigest()
        self.assert_code(
            "VERIFICATION_SIGNATURE_ENCODING_INVALID",
            lambda: verification._validate_receipt_shape(receipt),
        )

    def test_empty_and_oversized_raw_documents_are_rejected_before_hashing(self) -> None:
        for raw in (b"", b"x" * (verification.MAX_DOCUMENT_BYTES + 1)):
            with self.subTest(size=len(raw)):
                self.assert_code(
                    "VERIFICATION_DOCUMENT_SIZE_INVALID",
                    lambda raw=raw: verification.decode_canonical_json(raw),
                )

    def test_key_bytes_and_expectations_are_exact_and_closed(self) -> None:
        receipt, raw = fake_receipt()
        with self.assertRaisesRegex(
            verification.LauncherVerificationError, "VERIFICATION_TRUST_KEY_INVALID"
        ):
            dataclasses.replace(trust_key(receipt, bytes(range(32))), public_key_bytes=b"x" * 31)
        with self.assertRaisesRegex(
            verification.LauncherVerificationError, "VERIFICATION_TRUST_KEY_INVALID"
        ):
            dataclasses.replace(
                trust_key(receipt, bytes(range(32))), public_key_bytes=bytearray(32)
            )
        expected = expected_pins(receipt, raw)
        with self.assertRaisesRegex(
            verification.LauncherVerificationError, "VERIFICATION_EXPECTATION_INVALID"
        ):
            dataclasses.replace(expected, coordinator_uid=expected.launcher_uid)


class DependencyFailClosedTests(unittest.TestCase):
    def test_missing_cryptography_never_accepts_a_structurally_valid_receipt(self) -> None:
        public_key = bytes(range(32))
        receipt, raw = fake_receipt(public_key)
        expected = expected_pins(receipt, raw)
        trusted = trust_key(receipt, public_key)
        real_import = builtins.__import__

        def deny_crypto(name, *args, **kwargs):
            if name == "cryptography" or name.startswith("cryptography."):
                raise ModuleNotFoundError("cryptography intentionally unavailable")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=deny_crypto):
            with self.assertRaises(verification.LauncherVerificationError) as captured:
                verification.verify_launcher_verification_receipt(
                    raw,
                    expected=expected,
                    trust_keys={trusted.key_id: trusted},
                    now=NOW,
                )
        self.assertEqual("VERIFICATION_CRYPTO_UNAVAILABLE", captured.exception.code)


class ExactTypeBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.public_key = bytes(range(32))
        self.receipt, self.raw = fake_receipt(self.public_key)
        self.expected = expected_pins(self.receipt, self.raw)
        self.trusted = trust_key(self.receipt, self.public_key)

    def assert_code(self, code: str, callback) -> verification.LauncherVerificationError:
        with self.assertRaises(verification.LauncherVerificationError) as captured:
            callback()
        self.assertEqual(code, captured.exception.code)
        return captured.exception

    def test_expected_trust_map_key_and_now_subclasses_are_rejected(self) -> None:
        class ExpectedSubclass(verification.ExpectedLauncherVerificationPins):
            pass

        expected_subclass = ExpectedSubclass(
            **{
                field.name: getattr(self.expected, field.name)
                for field in dataclasses.fields(self.expected)
            }
        )
        self.assert_code(
            "VERIFICATION_EXPECTATION_INVALID",
            lambda: verification.verify_launcher_verification_receipt(
                self.raw,
                expected=expected_subclass,
                trust_keys={self.trusted.key_id: self.trusted},
                now=NOW,
            ),
        )

        class ExplodingDict(dict):
            def __len__(self):
                raise AssertionError("dict subclass must not be consulted")

        self.assert_code(
            "VERIFICATION_TRUST_MAP_INVALID",
            lambda: verification.verify_launcher_verification_receipt(
                self.raw,
                expected=self.expected,
                trust_keys=ExplodingDict({self.trusted.key_id: self.trusted}),
                now=NOW,
            ),
        )

        class TrustedKeySubclass(verification.TrustedEd25519Key):
            pass

        key_subclass = TrustedKeySubclass(
            **{
                field.name: getattr(self.trusted, field.name)
                for field in dataclasses.fields(self.trusted)
            }
        )
        self.assert_code(
            "VERIFICATION_TRUST_MAP_INVALID",
            lambda: verification.verify_launcher_verification_receipt(
                self.raw,
                expected=self.expected,
                trust_keys={key_subclass.key_id: key_subclass},
                now=NOW,
            ),
        )

        class DateTimeSubclass(dt.datetime):
            pass

        subclass_now = DateTimeSubclass(2026, 7, 21, 12, 0, 30, tzinfo=dt.timezone.utc)
        self.assert_code(
            "VERIFICATION_FRESHNESS_INVALID",
            lambda: verification.verify_launcher_verification_receipt(
                self.raw,
                expected=self.expected,
                trust_keys={self.trusted.key_id: self.trusted},
                now=subclass_now,
            ),
        )

    def test_receipt_mapping_and_scalar_subclasses_are_rejected(self) -> None:
        class ReceiptSubclass(dict):
            pass

        self.assert_code(
            "VERIFICATION_SHAPE_INVALID",
            lambda: verification._validate_receipt_shape(ReceiptSubclass(self.receipt)),
        )
        self.assert_code(
            "VERIFICATION_JSON_INVALID",
            lambda: verification.canonical_json_bytes(ReceiptSubclass(self.receipt)),
        )

        class TextSubclass(str):
            pass

        changed = copy.deepcopy(self.receipt)
        changed["receipt_id"] = TextSubclass(changed["receipt_id"])
        self.assert_code(
            "VERIFICATION_VALUE_INVALID",
            lambda: verification._validate_receipt_shape(changed),
        )

    def test_digest_echo_objects_and_handoff_subclasses_cannot_bind(self) -> None:
        class EchoHandoff:
            def __init__(self) -> None:
                self.digest_reads = 0

            @property
            def receipt_sha256(self) -> str:
                self.digest_reads += 1
                return self_outer.receipt["handoff"]["raw_receipt_sha256"]

        self_outer = self
        echo = EchoHandoff()
        self.assert_code(
            "VERIFIED_HANDOFF_TYPE_INVALID",
            lambda: verification.bind_verified_attested_worker_handoff(object(), echo),
        )
        self.assertEqual(0, echo.digest_reads)

        class HandoffSubclass(launcher_handoff.AttestedWorkerHandoff):
            pass

        worker_socket, peer_socket = socket.socketpair()
        handoff_subclass = HandoffSubclass(
            launcher_handoff._CONSTRUCTION_SENTINEL,
            worker_socket,
            b'{"fixture":"subclass"}',
        )
        try:
            self.assert_code(
                "VERIFIED_HANDOFF_TYPE_INVALID",
                lambda: verification.bind_verified_attested_worker_handoff(
                    object(), handoff_subclass
                ),
            )
        finally:
            handoff_subclass.close()
            peer_socket.close()

    def test_exact_handoff_is_closed_when_verified_object_is_only_a_digest_echo(self) -> None:
        class EchoVerified:
            handoff_receipt_sha256 = "0" * 64

        worker_socket, peer_socket = socket.socketpair()
        peer_socket.settimeout(0.2)
        handoff = launcher_handoff.AttestedWorkerHandoff(
            launcher_handoff._CONSTRUCTION_SENTINEL,
            worker_socket,
            b'{"fixture":"exact-handoff"}',
        )
        self.assert_code(
            "VERIFIED_RECEIPT_TYPE_INVALID",
            lambda: verification.bind_verified_attested_worker_handoff(
                EchoVerified(), handoff
            ),
        )
        self.assertEqual(b"", peer_socket.recv(1))
        peer_socket.close()


@unittest.skipIf(Ed25519PrivateKey is None, "cryptography is unavailable in system isolation")
class RealEd25519VerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        assert Ed25519PrivateKey is not None
        assert serialization is not None
        self.private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
        self.public_key = self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        unsigned = unsigned_receipt(self.public_key)
        payload = verification.canonical_json_bytes(unsigned)
        self.receipt, self.raw = attach_signature(unsigned, self.private_key.sign(payload))
        self.expected = expected_pins(self.receipt, self.raw)
        self.trusted = trust_key(self.receipt, self.public_key)

    def assert_code(self, code: str, callback) -> verification.LauncherVerificationError:
        with self.assertRaises(verification.LauncherVerificationError) as captured:
            callback()
        self.assertEqual(code, captured.exception.code)
        return captured.exception

    def verify(
        self,
        raw: bytes | None = None,
        *,
        expected: verification.ExpectedLauncherVerificationPins | None = None,
        trusted: verification.TrustedEd25519Key | None = None,
        now: dt.datetime = NOW,
    ) -> verification.VerifiedLauncherReceipt:
        selected = self.trusted if trusted is None else trusted
        return verification.verify_launcher_verification_receipt(
            self.raw if raw is None else raw,
            expected=self.expected if expected is None else expected,
            trust_keys={selected.key_id: selected},
            now=now,
        )

    def verified_for_handoff(
        self,
        raw_handoff_receipt: bytes,
    ) -> verification.VerifiedLauncherReceipt:
        unsigned = unsigned_receipt(
            self.public_key,
            handoff_receipt_sha256=hashlib.sha256(raw_handoff_receipt).hexdigest(),
        )
        payload = verification.canonical_json_bytes(unsigned)
        receipt, raw = attach_signature(unsigned, self.private_key.sign(payload))
        return verification.verify_launcher_verification_receipt(
            raw,
            expected=expected_pins(receipt, raw),
            trust_keys={self.trusted.key_id: self.trusted},
            now=NOW,
        )

    def exact_handoff(
        self,
        raw_handoff_receipt: bytes,
    ) -> tuple[launcher_handoff.AttestedWorkerHandoff, socket.socket]:
        worker_socket, peer_socket = socket.socketpair()
        peer_socket.settimeout(0.2)
        return (
            launcher_handoff.AttestedWorkerHandoff(
                launcher_handoff._CONSTRUCTION_SENTINEL,
                worker_socket,
                raw_handoff_receipt,
            ),
            peer_socket,
        )

    def test_real_signature_returns_distinct_opaque_frozen_receipt_only(self) -> None:
        result = self.verify()
        unsigned = copy.deepcopy(self.receipt)
        unsigned.pop("signature")
        self.assertTrue(result.signature_verified)
        self.assertEqual(self.raw, result.raw_receipt_bytes)
        self.assertEqual(hashlib.sha256(self.raw).hexdigest(), result.raw_receipt_sha256)
        self.assertEqual(verification.canonical_json_bytes(unsigned), result.signed_payload_bytes)
        self.assertEqual(
            hashlib.sha256(result.signed_payload_bytes).hexdigest(),
            result.signed_payload_sha256,
        )
        self.assertEqual(64, len(result.signature_bytes))
        self.assertEqual(
            hashlib.sha256(result.signature_bytes).hexdigest(), result.signature_sha256
        )
        self.assertNotEqual(result.raw_receipt_sha256, result.handoff_receipt_sha256)
        self.assertNotEqual(result.raw_receipt_sha256, result.signed_payload_sha256)
        self.assertFalse(hasattr(result, "state_launcher_verification"))
        self.assertFalse(hasattr(result, "terminal_launcher_verification"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.plan_sha256 = sha("replacement")
        with self.assertRaises(TypeError):
            verification.VerifiedLauncherReceipt(
                None,
                **{
                    field.name: getattr(result, field.name)
                    for field in dataclasses.fields(result)
                },
            )

    def test_composite_binds_exact_objects_projects_and_transfers_socket_once(self) -> None:
        raw_handoff = b'{"fixture":"bound-handoff"}'
        verified = self.verified_for_handoff(raw_handoff)
        handoff, peer_socket = self.exact_handoff(raw_handoff)
        composite = verification.bind_verified_attested_worker_handoff(verified, handoff)
        self.assertIs(verified, composite.verified_receipt)
        self.assertEqual(verified.raw_receipt_sha256, composite.verification_receipt_sha256)
        self.assertEqual(verified.handoff_receipt_sha256, composite.handoff_receipt_sha256)
        schema_runtime.validate_instance(
            projection_schema(STATE_SCHEMA_PATH),
            composite.state_launcher_verification(),
        )
        schema_runtime.validate_instance(
            projection_schema(TERMINAL_SCHEMA_PATH),
            composite.terminal_launcher_verification(),
        )
        self.assert_code(
            "VERIFIED_HANDOFF_ALREADY_CONSUMED",
            lambda: verification.bind_verified_attested_worker_handoff(
                verified, handoff
            ),
        )
        with self.assertRaisesRegex(
            launcher_handoff.LauncherHandoffError, "HANDOFF_ALREADY_CONSUMED"
        ):
            handoff.take_worker_socket()
        with self.assertRaises(TypeError):
            pickle.dumps(composite)
        with self.assertRaises(TypeError):
            copy.copy(composite)
        with self.assertRaises(TypeError):
            copy.deepcopy(composite)
        with self.assertRaises(AttributeError):
            composite._worker_socket = None
        with self.assertRaises(TypeError):
            verification.VerifiedAttestedWorkerHandoff(None, verified, object())

        worker_socket = composite.take_worker_socket()
        self.assert_code(
            "VERIFIED_HANDOFF_ALREADY_CONSUMED",
            composite.take_worker_socket,
        )
        worker_socket.close()
        self.assertEqual(b"", peer_socket.recv(1))
        peer_socket.close()

    def test_composite_close_and_context_manager_close_are_idempotent(self) -> None:
        raw_handoff = b'{"fixture":"close-handoff"}'
        verified = self.verified_for_handoff(raw_handoff)
        handoff, peer_socket = self.exact_handoff(raw_handoff)
        with verification.bind_verified_attested_worker_handoff(
            verified, handoff
        ) as composite:
            self.assertIs(verified, composite.verified_receipt)
        composite.close()
        self.assert_code(
            "VERIFIED_HANDOFF_ALREADY_CONSUMED",
            composite.take_worker_socket,
        )
        self.assertEqual(b"", peer_socket.recv(1))
        peer_socket.close()

    def test_handoff_digest_mismatch_closes_socket_and_never_creates_composite(self) -> None:
        verified = self.verified_for_handoff(b'{"fixture":"signed-handoff"}')
        handoff, peer_socket = self.exact_handoff(b'{"fixture":"different-handoff"}')
        self.assert_code(
            "VERIFIED_HANDOFF_DIGEST_MISMATCH",
            lambda: verification.bind_verified_attested_worker_handoff(verified, handoff),
        )
        self.assertEqual(b"", peer_socket.recv(1))
        peer_socket.close()

    def test_verified_receipt_subclass_is_rejected_and_exact_handoff_is_closed(self) -> None:
        raw_handoff = b'{"fixture":"verified-subclass"}'
        verified = self.verified_for_handoff(raw_handoff)

        class VerifiedSubclass(verification.VerifiedLauncherReceipt):
            pass

        verified_subclass = VerifiedSubclass(
            verification._RESULT_SENTINEL,
            **{
                field.name: getattr(verified, field.name)
                for field in dataclasses.fields(verified)
            },
        )
        handoff, peer_socket = self.exact_handoff(raw_handoff)
        self.assert_code(
            "VERIFIED_RECEIPT_TYPE_INVALID",
            lambda: verification.bind_verified_attested_worker_handoff(
                verified_subclass, handoff
            ),
        )
        self.assertEqual(b"", peer_socket.recv(1))
        peer_socket.close()

    def test_raw_receipt_substitution_and_independent_pin_substitution_fail(self) -> None:
        self.assert_code(
            "VERIFICATION_RECEIPT_DIGEST_MISMATCH",
            lambda: self.verify(expected=dataclasses.replace(self.expected, raw_receipt_sha256=sha("other"))),
        )
        self.assert_code(
            "VERIFICATION_PIN_MISMATCH",
            lambda: self.verify(expected=dataclasses.replace(self.expected, plan_sha256=sha("other-plan"))),
        )

    def test_signed_payload_signature_and_signature_digest_tampering_fail(self) -> None:
        changed = copy.deepcopy(self.receipt)
        changed["pins"]["plan_sha256"] = sha("tampered-plan")
        unsigned = copy.deepcopy(changed)
        unsigned.pop("signature")
        changed["signature"]["signed_payload_sha256"] = hashlib.sha256(
            verification.canonical_json_bytes(unsigned)
        ).hexdigest()
        changed_raw = verification.canonical_json_bytes(changed)
        changed_expected = expected_pins(changed, changed_raw)
        self.assert_code(
            "VERIFICATION_SIGNATURE_INVALID",
            lambda: self.verify(changed_raw, expected=changed_expected),
        )

        wrong_payload_hash = copy.deepcopy(self.receipt)
        wrong_payload_hash["signature"]["signed_payload_sha256"] = sha("wrong-payload")
        wrong_payload_raw = verification.canonical_json_bytes(wrong_payload_hash)
        self.assert_code(
            "VERIFICATION_SIGNED_PAYLOAD_DIGEST_MISMATCH",
            lambda: self.verify(
                wrong_payload_raw,
                expected=expected_pins(wrong_payload_hash, wrong_payload_raw),
            ),
        )

        wrong_signature_hash = copy.deepcopy(self.receipt)
        wrong_signature_hash["signature"]["detached_signature_sha256"] = sha("wrong-signature")
        wrong_signature_raw = verification.canonical_json_bytes(wrong_signature_hash)
        self.assert_code(
            "VERIFICATION_SIGNATURE_DIGEST_MISMATCH",
            lambda: self.verify(
                wrong_signature_raw,
                expected=expected_pins(wrong_signature_hash, wrong_signature_raw),
            ),
        )

    def test_unknown_key_revision_and_public_key_substitution_fail(self) -> None:
        unknown = dataclasses.replace(self.trusted, key_id="launcher-key:unknown")
        self.assert_code(
            "VERIFICATION_TRUST_KEY_UNKNOWN",
            lambda: verification.verify_launcher_verification_receipt(
                self.raw,
                expected=self.expected,
                trust_keys={unknown.key_id: unknown},
                now=NOW,
            ),
        )
        wrong_revision = dataclasses.replace(self.trusted, key_revision=revision("other-key"))
        self.assert_code(
            "VERIFICATION_TRUST_KEY_MISMATCH",
            lambda: self.verify(trusted=wrong_revision),
        )
        wrong_public_key = dataclasses.replace(self.trusted, public_key_bytes=b"z" * 32)
        self.assert_code(
            "VERIFICATION_TRUST_KEY_MISMATCH",
            lambda: self.verify(trusted=wrong_public_key),
        )

    def test_not_before_expiry_lifetime_and_ambient_time_are_fail_closed(self) -> None:
        self.assert_code(
            "VERIFICATION_NOT_YET_VALID",
            lambda: self.verify(now=dt.datetime(2026, 7, 21, 12, 0, 4, tzinfo=dt.timezone.utc)),
        )
        self.assert_code(
            "VERIFICATION_EXPIRED",
            lambda: self.verify(now=dt.datetime(2026, 7, 21, 12, 2, 0, tzinfo=dt.timezone.utc)),
        )
        self.assert_code(
            "VERIFICATION_FRESHNESS_INVALID",
            lambda: self.verify(now=dt.datetime(2026, 7, 21, 12, 0, 30)),
        )

        unsigned = unsigned_receipt(self.public_key)
        unsigned["expires_at"] = "2026-07-21T12:06:00Z"
        payload = verification.canonical_json_bytes(unsigned)
        receipt, raw = attach_signature(unsigned, self.private_key.sign(payload))
        self.assert_code(
            "VERIFICATION_FRESHNESS_INVALID",
            lambda: self.verify(raw, expected=expected_pins(receipt, raw)),
        )


if __name__ == "__main__":
    unittest.main()
