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
import sys
import unittest
from unittest import mock


TOOLS_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import semantic_index_approval_verification as verification  # noqa: E402
import semantic_index_schema_runtime as schema_runtime  # noqa: E402

try:  # Deliberately unavailable under /usr/bin/python3 -I -S.
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
except ModuleNotFoundError:  # pragma: no cover - system-isolated test path
    serialization = None
    Ed25519PrivateKey = None


SCHEMA_PATH = TOOLS_DIR / "semantic_index_approval_receipt_schema.json"
NOW = dt.datetime(2026, 7, 21, 10, 30, tzinfo=dt.timezone.utc)
ISSUED = "2026-07-21T10:00:00Z"
EXPIRES = "2026-07-21T11:00:00Z"


def sha(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def revision(label: str) -> str:
    return "sha256:" + sha(label)


def generation(label: str) -> str:
    return "semantic-generation:" + sha(label)


def scope_for(purpose: str) -> dict:
    if purpose == "data_owner_review":
        return {
            "scope_kind": "reviewed_selection",
            "source_v1_job_sha256": sha("source-v1-job"),
            "source_v1_job_revision": revision("source-v1-job"),
            "reviewed_selection_basis_sha256": sha("selection-basis"),
            "accepted_assets_sha256": sha("accepted-assets"),
            "accepted_asset_count": 2,
            "rejection_ledger_sha256": sha("rejection-ledger"),
            "content_revision": revision("content"),
        }
    return {
        "scope_kind": "production_build",
        "reviewed_job_sha256": sha("reviewed-job"),
        "reviewed_job_revision": revision("reviewed-job"),
        "plan_approval_basis_sha256": sha("production-basis"),
        "generation_id": generation("production"),
        "generation_nonce_sha256": sha("generation-nonce"),
        "target_snapshot_revision": "asset-snapshot-20260721-reviewed",
        "worker_deployment_identity": "semantic-worker:production-a",
        "active_generation_pointer_identity": "semantic-index-pointer:production",
        "expected_active_generation_epoch": 3,
    }


def authorization_for(purpose: str) -> dict:
    if purpose == "data_owner_review":
        return {
            "kind": purpose,
            "reviewed_selection_approved": True,
            "production_execution_authorized": False,
        }
    if purpose == "cost_owner":
        return {
            "kind": purpose,
            "max_caption_calls": 16,
            "max_output_tokens": 8192,
            "currency": "USD",
            "minor_unit_exponent": 6,
            "max_cost_minor_units": 500000,
            "rounding_mode": "ceiling_each_provider_charge_to_minor_unit",
            "retry_charge_policy": "all_provider_accepted_requests_counted",
        }
    if purpose == "admin_state_change":
        return {
            "kind": purpose,
            "allowed_mutating_phases": [
                "inspect",
                "render",
                "caption",
                "embed",
                "postgres",
                "qdrant",
            ],
            "allowed_control_operations": [
                "query_phase_status",
                "recover_phase_receipt",
                "cancel_phase_work",
                "quarantine_generation",
            ],
            "activation_authorized": False,
        }
    if purpose == "deployment":
        return {
            "kind": purpose,
            "deployment_preflight_sha256": sha("preflight"),
            "worker_image": "registry.example/simworld/worker@sha256:" + sha("image"),
            "worker_protocol_sha256": sha("protocol"),
        }
    if purpose == "runtime":
        return {
            "kind": purpose,
            "ue_lease_id": "ue-lease:semantic-index-001",
            "ue_lease_expires_at": "2026-07-21T10:55:00Z",
            "worker_deployment_identity": "semantic-worker:production-a",
        }
    if purpose == "rollback_readiness":
        return {
            "kind": purpose,
            "bootstrap": False,
            "previous_generation_id": generation("previous"),
            "previous_snapshot_revision": "asset-snapshot-20260720-active",
            "rollback_readiness_receipt_sha256": sha("rollback-receipt"),
            "active_generation_pointer_identity": "semantic-index-pointer:production",
            "expected_active_generation_epoch": 3,
        }
    raise AssertionError(f"unhandled fixture purpose: {purpose}")


def unsigned_receipt(purpose: str = "cost_owner") -> dict:
    scope = scope_for(purpose)
    basis = (
        scope["reviewed_selection_basis_sha256"]
        if purpose == "data_owner_review"
        else scope["plan_approval_basis_sha256"]
    )
    return {
        "schema": verification.RECEIPT_SCHEMA,
        "receipt_id": f"semantic-index-approval:{purpose}-001",
        "approval_kind": purpose,
        "decision": "approved",
        "approval_basis_sha256": basis,
        "scope": scope,
        "authorization": authorization_for(purpose),
        "issuer": {
            "issuer_id": f"issuer:{purpose}",
            "role": verification.APPROVAL_ROLES[purpose],
            "key_id": f"approval-key:{purpose}",
            "key_revision": revision(f"key-{purpose}"),
        },
        "trust_bundle": {
            "schema": "simworld-approval-trust-bundle/v1",
            "revision": revision("approval-trust-bundle"),
            "sha256": sha("approval-trust-bundle"),
            "issuer_membership_sha256": sha(f"membership-{purpose}"),
        },
        "issued_at": ISSUED,
        "expires_at": EXPIRES,
        "serialization_contract": copy.deepcopy(verification.SERIALIZATION_CONTRACT),
    }


def attach_signature(unsigned: dict, signature_bytes: bytes) -> tuple[dict, bytes]:
    value = copy.deepcopy(unsigned)
    payload = verification.canonical_json_bytes(value)
    value["signature"] = {
        "algorithm": "ed25519",
        "canonicalization": "rfc8785-jcs-v1",
        "signed_payload_sha256": hashlib.sha256(payload).hexdigest(),
        "detached_signature_sha256": hashlib.sha256(signature_bytes).hexdigest(),
        "detached_signature_base64": base64.b64encode(signature_bytes).decode("ascii"),
    }
    return value, verification.canonical_json_bytes(value)


def fake_receipt(purpose: str = "cost_owner") -> tuple[dict, bytes]:
    return attach_signature(unsigned_receipt(purpose), bytes(range(64)))


def environment_for(receipt: dict) -> str:
    if receipt["approval_kind"] == "data_owner_review":
        return receipt["scope"]["content_revision"]
    return receipt["scope"]["worker_deployment_identity"]


def expected_pins(
    receipt: dict,
    raw: bytes,
    public_key_bytes: bytes,
) -> verification.ExpectedApprovalVerificationPins:
    issuer = receipt["issuer"]
    trust = receipt["trust_bundle"]
    return verification.ExpectedApprovalVerificationPins(
        raw_receipt_sha256=hashlib.sha256(raw).hexdigest(),
        receipt_id=receipt["receipt_id"],
        purpose=receipt["approval_kind"],
        environment_id=environment_for(receipt),
        approval_basis_sha256=receipt["approval_basis_sha256"],
        scope_bytes=verification.canonical_json_bytes(receipt["scope"]),
        authorization_bytes=verification.canonical_json_bytes(receipt["authorization"]),
        issuer_id=issuer["issuer_id"],
        issuer_role=issuer["role"],
        issuer_key_id=issuer["key_id"],
        issuer_key_revision=issuer["key_revision"],
        trust_bundle_revision=trust["revision"],
        trust_bundle_sha256=trust["sha256"],
        issuer_membership_sha256=trust["issuer_membership_sha256"],
        issuer_public_key_sha256=hashlib.sha256(public_key_bytes).hexdigest(),
        issued_at=receipt["issued_at"],
        expires_at=receipt["expires_at"],
        maximum_lifetime_seconds=7200,
    )


def trust_key(
    receipt: dict,
    public_key_bytes: bytes,
) -> verification.TrustedApprovalEd25519Key:
    issuer = receipt["issuer"]
    trust = receipt["trust_bundle"]
    return verification.TrustedApprovalEd25519Key(
        issuer_id=issuer["issuer_id"],
        issuer_role=issuer["role"],
        key_id=issuer["key_id"],
        key_revision=issuer["key_revision"],
        trust_bundle_revision=trust["revision"],
        trust_bundle_sha256=trust["sha256"],
        issuer_membership_sha256=trust["issuer_membership_sha256"],
        purpose=receipt["approval_kind"],
        environment_id=environment_for(receipt),
        public_key_bytes=public_key_bytes,
    )


class ShapeAndCanonicalParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    def assert_code(self, code: str, callback) -> verification.ApprovalVerificationError:
        with self.assertRaises(verification.ApprovalVerificationError) as captured:
            callback()
        self.assertEqual(code, captured.exception.code)
        return captured.exception

    def test_all_approval_purposes_are_closed_schema_valid(self) -> None:
        schema_runtime.validate_schema(self.schema)
        for purpose in verification.APPROVAL_KINDS:
            with self.subTest(purpose=purpose):
                receipt, raw = fake_receipt(purpose)
                self.assertEqual(receipt, verification.decode_canonical_json(raw))
                schema_runtime.validate_instance(self.schema, receipt)
                validated, environment = verification._validate_receipt_shape(receipt)
                self.assertEqual(receipt, validated)
                self.assertEqual(environment_for(receipt), environment)

    def test_duplicate_float_noncanonical_non_nfc_and_control_fail_closed(self) -> None:
        cases = (
            ("APPROVAL_JSON_INVALID", b'{"x":1,"x":2}'),
            ("APPROVAL_JSON_INVALID", b'{"x":1.0}'),
            ("APPROVAL_JSON_NONCANONICAL", b'{"x": 1}'),
            (
                "APPROVAL_VALUE_INVALID",
                json.dumps(
                    {"x": "e\u0301"}, ensure_ascii=False, separators=(",", ":")
                ).encode(),
            ),
            ("APPROVAL_VALUE_INVALID", b'{"x":"\\u0001"}'),
        )
        for code, raw in cases:
            with self.subTest(code=code):
                self.assert_code(code, lambda raw=raw: verification.decode_canonical_json(raw))

    def test_unknown_fields_wrong_role_scope_and_authorization_fail_closed(self) -> None:
        receipt, _raw = fake_receipt()
        receipt["unknown"] = True
        self.assert_code(
            "APPROVAL_SHAPE_INVALID",
            lambda: verification._validate_receipt_shape(receipt),
        )

        receipt, _raw = fake_receipt("runtime")
        receipt["issuer"]["role"] = "cost_owner"
        self.assert_code(
            "APPROVAL_ISSUER_INVALID",
            lambda: verification._validate_receipt_shape(receipt),
        )

        receipt, _raw = fake_receipt("runtime")
        receipt["authorization"]["worker_deployment_identity"] = "semantic-worker:other"
        self.assert_code(
            "APPROVAL_SCOPE_INVALID",
            lambda: verification._validate_receipt_shape(receipt),
        )

        receipt, _raw = fake_receipt("admin_state_change")
        receipt["authorization"]["activation_authorized"] = True
        self.assert_code(
            "APPROVAL_AUTHORIZATION_INVALID",
            lambda: verification._validate_receipt_shape(receipt),
        )

    def test_signature_shape_and_basis_scope_cross_binding_fail_closed(self) -> None:
        receipt, _raw = fake_receipt()
        receipt["signature"]["detached_signature_base64"] = base64.b64encode(
            b"x" * 63
        ).decode("ascii")
        self.assert_code(
            "APPROVAL_SIGNATURE_ENCODING_INVALID",
            lambda: verification._validate_receipt_shape(receipt),
        )

        receipt, _raw = fake_receipt()
        receipt["approval_basis_sha256"] = sha("other-basis")
        self.assert_code(
            "APPROVAL_SCOPE_INVALID",
            lambda: verification._validate_receipt_shape(receipt),
        )

    def test_expectations_and_trust_keys_are_exact_closed_policy(self) -> None:
        public_key = bytes(range(32))
        receipt, raw = fake_receipt()
        expected = expected_pins(receipt, raw, public_key)
        trusted = trust_key(receipt, public_key)

        with self.assertRaisesRegex(
            verification.ApprovalVerificationError, "APPROVAL_EXPECTATION_INVALID"
        ):
            dataclasses.replace(expected, environment_id="semantic-worker:other")
        with self.assertRaisesRegex(
            verification.ApprovalVerificationError, "APPROVAL_EXPECTATION_INVALID"
        ):
            dataclasses.replace(expected, maximum_lifetime_seconds=30)
        with self.assertRaisesRegex(
            verification.ApprovalVerificationError, "APPROVAL_TRUST_KEY_INVALID"
        ):
            dataclasses.replace(trusted, public_key_bytes=bytearray(32))
        with self.assertRaisesRegex(
            verification.ApprovalVerificationError, "APPROVAL_TRUST_KEY_INVALID"
        ):
            dataclasses.replace(trusted, purpose="runtime")

    def test_document_bounds_are_checked_before_parsing(self) -> None:
        for raw in (b"", b"x" * (verification.MAX_DOCUMENT_BYTES + 1)):
            with self.subTest(length=len(raw)):
                self.assert_code(
                    "APPROVAL_DOCUMENT_SIZE_INVALID",
                    lambda raw=raw: verification.decode_canonical_json(raw),
                )


class DependencyAndEvidenceFailClosedTests(unittest.TestCase):
    def test_missing_cryptography_never_accepts_shape_only_signature(self) -> None:
        public_key = bytes(range(32))
        receipt, raw = fake_receipt()
        expected = expected_pins(receipt, raw, public_key)
        trusted = trust_key(receipt, public_key)
        real_import = builtins.__import__

        def deny_crypto(name, *args, **kwargs):
            if name == "cryptography" or name.startswith("cryptography."):
                raise ModuleNotFoundError("cryptography intentionally unavailable")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=deny_crypto):
            with self.assertRaises(verification.ApprovalVerificationError) as captured:
                verification.verify_approval_receipt(
                    raw,
                    expected=expected,
                    trust_keys={trusted.key_id: trusted},
                    now=NOW,
                )
        self.assertEqual("APPROVAL_CRYPTO_UNAVAILABLE", captured.exception.code)

    def test_digest_only_phase_evidence_is_never_reported_as_verified(self) -> None:
        with self.assertRaises(verification.ApprovalVerificationError) as captured:
            verification.verify_phase_evidence_wire_signature(
                {
                    "key_revision": revision("worker-key"),
                    "signed_payload_sha256": sha("payload"),
                    "signature_sha256": sha("signature"),
                }
            )
        self.assertEqual(
            "PHASE_EVIDENCE_SIGNATURE_CONTRACT_INSUFFICIENT",
            captured.exception.code,
        )
        self.assertEqual(
            {
                "algorithm",
                "canonicalization",
                "issuer_id",
                "issuer_role",
                "key_id",
                "key_revision",
                "trust_bundle_revision",
                "trust_bundle_sha256",
                "issuer_membership_sha256",
                "issuer_public_key_sha256",
                "purpose",
                "environment_id",
                "issued_at",
                "expires_at",
                "signed_payload_sha256",
                "detached_signature_sha256",
                "detached_signature_base64",
            },
            set(verification.PHASE_EVIDENCE_WIRE_SIGNATURE_REQUIRED_FIELDS),
        )


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
        unsigned = unsigned_receipt()
        payload = verification.canonical_json_bytes(unsigned)
        self.receipt, self.raw = attach_signature(unsigned, self.private_key.sign(payload))
        self.expected = expected_pins(self.receipt, self.raw, self.public_key)
        self.trusted = trust_key(self.receipt, self.public_key)

    def assert_code(self, code: str, callback) -> verification.ApprovalVerificationError:
        with self.assertRaises(verification.ApprovalVerificationError) as captured:
            callback()
        self.assertEqual(code, captured.exception.code)
        return captured.exception

    def verify(
        self,
        raw: bytes | None = None,
        *,
        expected: verification.ExpectedApprovalVerificationPins | None = None,
        trusted: verification.TrustedApprovalEd25519Key | None = None,
        now: dt.datetime = NOW,
    ) -> verification.VerifiedApprovalReceipt:
        selected = self.trusted if trusted is None else trusted
        return verification.verify_approval_receipt(
            self.raw if raw is None else raw,
            expected=self.expected if expected is None else expected,
            trust_keys={selected.key_id: selected},
            now=now,
        )

    def signed_receipt(self, value: dict) -> tuple[dict, bytes]:
        payload = verification.canonical_json_bytes(value)
        return attach_signature(value, self.private_key.sign(payload))

    def test_real_signature_returns_opaque_exact_policy_bound_result(self) -> None:
        result = self.verify()
        unsigned = copy.deepcopy(self.receipt)
        unsigned.pop("signature")
        self.assertTrue(result.signature_verified)
        self.assertEqual(self.raw, result.raw_receipt_bytes)
        self.assertEqual(hashlib.sha256(self.raw).hexdigest(), result.raw_receipt_sha256)
        self.assertEqual(verification.canonical_json_bytes(unsigned), result.signed_payload_bytes)
        self.assertEqual(self.expected.environment_id, result.environment_id)
        self.assertEqual(self.expected.scope_bytes, result.scope_bytes)
        self.assertEqual(self.expected.authorization_bytes, result.authorization_bytes)
        self.assertEqual(hashlib.sha256(result.scope_bytes).hexdigest(), result.scope_sha256)
        self.assertEqual(
            hashlib.sha256(result.authorization_bytes).hexdigest(),
            result.authorization_sha256,
        )
        self.assertEqual(hashlib.sha256(self.public_key).hexdigest(), result.issuer_public_key_sha256)
        self.assertEqual(self.receipt, result.receipt)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.purpose = "runtime"
        with self.assertRaises(TypeError):
            pickle.dumps(result)
        with self.assertRaises(TypeError):
            copy.copy(result)
        with self.assertRaises(TypeError):
            copy.deepcopy(result)
        with self.assertRaises(TypeError):
            verification.VerifiedApprovalReceipt(
                None,
                **{
                    field.name: getattr(result, field.name)
                    for field in dataclasses.fields(result)
                },
            )

    def test_data_owner_environment_is_signed_content_revision(self) -> None:
        unsigned = unsigned_receipt("data_owner_review")
        payload = verification.canonical_json_bytes(unsigned)
        receipt, raw = attach_signature(unsigned, self.private_key.sign(payload))
        expected = expected_pins(receipt, raw, self.public_key)
        trusted = trust_key(receipt, self.public_key)
        result = verification.verify_approval_receipt(
            raw,
            expected=expected,
            trust_keys={trusted.key_id: trusted},
            now=NOW,
        )
        self.assertEqual(receipt["scope"]["content_revision"], result.environment_id)

    def test_real_signatures_verify_for_every_closed_approval_purpose(self) -> None:
        for purpose in verification.APPROVAL_KINDS:
            with self.subTest(purpose=purpose):
                unsigned = unsigned_receipt(purpose)
                payload = verification.canonical_json_bytes(unsigned)
                receipt, raw = attach_signature(
                    unsigned, self.private_key.sign(payload)
                )
                expected = expected_pins(receipt, raw, self.public_key)
                trusted = trust_key(receipt, self.public_key)
                result = verification.verify_approval_receipt(
                    raw,
                    expected=expected,
                    trust_keys={trusted.key_id: trusted},
                    now=NOW,
                )
                self.assertEqual(purpose, result.purpose)
                self.assertEqual(environment_for(receipt), result.environment_id)

    def test_raw_scope_authorization_and_environment_pins_cannot_be_echoed_away(self) -> None:
        self.assert_code(
            "APPROVAL_RECEIPT_DIGEST_MISMATCH",
            lambda: self.verify(
                expected=dataclasses.replace(
                    self.expected, raw_receipt_sha256=sha("different-receipt")
                )
            ),
        )

        changed = copy.deepcopy(self.receipt)
        changed.pop("signature")
        changed["scope"]["reviewed_job_sha256"] = sha("other-reviewed-job")
        changed_receipt, changed_raw = self.signed_receipt(changed)
        scope_expected = dataclasses.replace(
            self.expected,
            raw_receipt_sha256=hashlib.sha256(changed_raw).hexdigest(),
        )
        self.assert_code(
            "APPROVAL_SCOPE_MISMATCH",
            lambda: self.verify(changed_raw, expected=scope_expected),
        )

        changed = copy.deepcopy(self.receipt)
        changed.pop("signature")
        changed["authorization"]["max_cost_minor_units"] += 1
        _changed_receipt, changed_raw = self.signed_receipt(changed)
        authorization_expected = dataclasses.replace(
            self.expected,
            raw_receipt_sha256=hashlib.sha256(changed_raw).hexdigest(),
        )
        self.assert_code(
            "APPROVAL_AUTHORIZATION_MISMATCH",
            lambda: self.verify(changed_raw, expected=authorization_expected),
        )

        wrong_environment_key = dataclasses.replace(
            self.trusted, environment_id="semantic-worker:other"
        )
        self.assert_code(
            "APPROVAL_TRUST_KEY_MISMATCH",
            lambda: self.verify(trusted=wrong_environment_key),
        )

    def test_signature_payload_and_signature_digest_tampering_fail(self) -> None:
        changed = copy.deepcopy(self.receipt)
        changed["authorization"]["max_cost_minor_units"] += 1
        unsigned = copy.deepcopy(changed)
        unsigned.pop("signature")
        changed["signature"]["signed_payload_sha256"] = hashlib.sha256(
            verification.canonical_json_bytes(unsigned)
        ).hexdigest()
        changed_raw = verification.canonical_json_bytes(changed)
        changed_expected = expected_pins(changed, changed_raw, self.public_key)
        self.assert_code(
            "APPROVAL_SIGNATURE_INVALID",
            lambda: self.verify(changed_raw, expected=changed_expected),
        )

        wrong_payload = copy.deepcopy(self.receipt)
        wrong_payload["signature"]["signed_payload_sha256"] = sha("wrong-payload")
        wrong_payload_raw = verification.canonical_json_bytes(wrong_payload)
        self.assert_code(
            "APPROVAL_SIGNED_PAYLOAD_DIGEST_MISMATCH",
            lambda: self.verify(
                wrong_payload_raw,
                expected=expected_pins(wrong_payload, wrong_payload_raw, self.public_key),
            ),
        )

        wrong_signature = copy.deepcopy(self.receipt)
        wrong_signature["signature"]["detached_signature_sha256"] = sha("wrong-signature")
        wrong_signature_raw = verification.canonical_json_bytes(wrong_signature)
        self.assert_code(
            "APPROVAL_SIGNATURE_DIGEST_MISMATCH",
            lambda: self.verify(
                wrong_signature_raw,
                expected=expected_pins(
                    wrong_signature, wrong_signature_raw, self.public_key
                ),
            ),
        )

    def test_unknown_key_revision_public_key_purpose_and_role_fail(self) -> None:
        unknown = dataclasses.replace(self.trusted, key_id="approval-key:unknown")
        self.assert_code(
            "APPROVAL_TRUST_KEY_UNKNOWN",
            lambda: verification.verify_approval_receipt(
                self.raw,
                expected=self.expected,
                trust_keys={unknown.key_id: unknown},
                now=NOW,
            ),
        )
        for changed in (
            dataclasses.replace(self.trusted, key_revision=revision("other-key")),
            dataclasses.replace(self.trusted, public_key_bytes=b"z" * 32),
        ):
            with self.subTest(changed=changed):
                self.assert_code(
                    "APPROVAL_TRUST_KEY_MISMATCH",
                    lambda changed=changed: self.verify(trusted=changed),
                )

    def test_explicit_utc_time_window_is_fail_closed(self) -> None:
        self.assert_code(
            "APPROVAL_NOT_YET_VALID",
            lambda: self.verify(
                now=dt.datetime(2026, 7, 21, 9, 59, 59, tzinfo=dt.timezone.utc)
            ),
        )
        self.assert_code(
            "APPROVAL_EXPIRED",
            lambda: self.verify(
                now=dt.datetime(2026, 7, 21, 11, 0, tzinfo=dt.timezone.utc)
            ),
        )
        self.assert_code(
            "APPROVAL_FRESHNESS_INVALID",
            lambda: self.verify(now=dt.datetime(2026, 7, 21, 10, 30)),
        )

        class DateTimeSubclass(dt.datetime):
            pass

        self.assert_code(
            "APPROVAL_FRESHNESS_INVALID",
            lambda: self.verify(
                now=DateTimeSubclass(
                    2026, 7, 21, 10, 30, tzinfo=dt.timezone.utc
                )
            ),
        )

    def test_exact_expected_trust_map_and_key_types_are_required(self) -> None:
        class ExpectedSubclass(verification.ExpectedApprovalVerificationPins):
            pass

        expected_subclass = ExpectedSubclass(
            **{
                field.name: getattr(self.expected, field.name)
                for field in dataclasses.fields(self.expected)
            }
        )
        self.assert_code(
            "APPROVAL_EXPECTATION_INVALID",
            lambda: verification.verify_approval_receipt(
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
            "APPROVAL_TRUST_MAP_INVALID",
            lambda: verification.verify_approval_receipt(
                self.raw,
                expected=self.expected,
                trust_keys=ExplodingDict({self.trusted.key_id: self.trusted}),
                now=NOW,
            ),
        )

        class TrustedSubclass(verification.TrustedApprovalEd25519Key):
            pass

        trusted_subclass = TrustedSubclass(
            **{
                field.name: getattr(self.trusted, field.name)
                for field in dataclasses.fields(self.trusted)
            }
        )
        self.assert_code(
            "APPROVAL_TRUST_MAP_INVALID",
            lambda: verification.verify_approval_receipt(
                self.raw,
                expected=self.expected,
                trust_keys={trusted_subclass.key_id: trusted_subclass},
                now=NOW,
            ),
        )


if __name__ == "__main__":
    unittest.main()
