from __future__ import annotations

import copy
import dataclasses
import hashlib
import pathlib
import pickle
import sys
import unittest


TESTS_DIR = pathlib.Path(__file__).resolve().parent
TOOLS_DIR = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(TOOLS_DIR))

import semantic_index_approval_aggregate as aggregate  # noqa: E402
import semantic_index_approval_verification as verification  # noqa: E402
from test_semantic_index_approval_verification import (  # noqa: E402
    NOW,
    attach_signature,
    authorization_for,
    expected_pins,
    fake_receipt,
    scope_for,
    sha,
    trust_key,
    unsigned_receipt,
)

try:  # Deliberately unavailable under /usr/bin/python3 -I -S.
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
except ModuleNotFoundError:  # pragma: no cover - system-isolated test path
    serialization = None
    Ed25519PrivateKey = None


PUBLIC_KEY_BYTES = bytes(range(32))


def fake_expected_references(
) -> tuple[verification.ExpectedApprovalVerificationPins, ...]:
    references = []
    for purpose in verification.APPROVAL_KINDS:
        receipt, raw = fake_receipt(purpose)
        references.append(expected_pins(receipt, raw, PUBLIC_KEY_BYTES))
    return tuple(references)


class ExpectedAggregatePinsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.references = fake_expected_references()

    def assert_code(self, code: str, callback) -> aggregate.ApprovalAggregateError:
        with self.assertRaises(aggregate.ApprovalAggregateError) as captured:
            callback()
        self.assertEqual(code, captured.exception.code)
        return captured.exception

    def test_exact_canonical_six_reference_set_is_required(self) -> None:
        expected = aggregate.ExpectedApprovalAggregatePins(self.references)
        self.assertEqual(
            hashlib.sha256(expected.canonical_bytes).hexdigest(),
            expected.sha256,
        )
        document = verification.decode_canonical_json(expected.canonical_bytes)
        self.assertEqual(aggregate.EXPECTED_REFERENCES_SCHEMA, document["schema"])
        self.assertEqual(
            list(verification.APPROVAL_KINDS),
            [item["purpose"] for item in document["approvals"]],
        )

        cases = (
            list(self.references),
            self.references[:-1],
            tuple(reversed(self.references)),
            (self.references[0],) + self.references[1:-1] + (self.references[0],),
        )
        for references in cases:
            with self.subTest(type=type(references), length=len(references)):
                self.assert_code(
                    "APPROVAL_AGGREGATE_EXPECTATION_INVALID",
                    lambda references=references: aggregate.ExpectedApprovalAggregatePins(
                        references  # type: ignore[arg-type]
                    ),
                )

    def test_production_references_must_share_environment_basis_and_scope(self) -> None:
        changed_scope = scope_for("runtime")
        changed_scope["reviewed_job_sha256"] = sha("other-reviewed-job")
        changed = list(self.references)
        changed[2] = dataclasses.replace(
            changed[2],
            scope_bytes=verification.canonical_json_bytes(changed_scope),
        )
        self.assert_code(
            "APPROVAL_AGGREGATE_EXPECTATION_INVALID",
            lambda: aggregate.ExpectedApprovalAggregatePins(tuple(changed)),
        )

    def test_raw_receipts_dicts_and_subclasses_are_never_aggregate_evidence(self) -> None:
        expected = aggregate.ExpectedApprovalAggregatePins(self.references)

        class ExpectedSubclass(aggregate.ExpectedApprovalAggregatePins):
            pass

        expected_subclass = ExpectedSubclass(self.references)
        self.assert_code(
            "APPROVAL_AGGREGATE_EXPECTATION_INVALID",
            lambda: aggregate.aggregate_verified_approval_receipts(
                tuple(b"raw-receipt" for _ in range(6)),
                expected=expected_subclass,
            ),
        )

        class ReceiptSubclass(verification.VerifiedApprovalReceipt):
            pass

        forged_subclass = object.__new__(ReceiptSubclass)
        candidates = (
            tuple(b"raw-receipt" for _ in range(6)),
            tuple({"approval_kind": purpose} for purpose in verification.APPROVAL_KINDS),
            tuple(forged_subclass for _ in range(6)),
        )
        for receipts in candidates:
            with self.subTest(receipt_type=type(receipts[0])):
                self.assert_code(
                    "APPROVAL_AGGREGATE_RECEIPT_TYPE_INVALID",
                    lambda receipts=receipts: aggregate.aggregate_verified_approval_receipts(
                        receipts,  # type: ignore[arg-type]
                        expected=expected,
                    ),
                )

        self.assert_code(
            "APPROVAL_AGGREGATE_SET_INVALID",
            lambda: aggregate.aggregate_verified_approval_receipts(
                list(b"x" for _ in range(6)),  # type: ignore[arg-type]
                expected=expected,
            ),
        )


@unittest.skipIf(Ed25519PrivateKey is None, "cryptography is unavailable in system isolation")
class RealVerifiedAggregateTests(unittest.TestCase):
    def setUp(self) -> None:
        assert Ed25519PrivateKey is not None
        assert serialization is not None
        private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
        public_key = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        receipts = []
        references = []
        for purpose in verification.APPROVAL_KINDS:
            unsigned = unsigned_receipt(purpose)
            payload = verification.canonical_json_bytes(unsigned)
            receipt, raw = attach_signature(unsigned, private_key.sign(payload))
            expected = expected_pins(receipt, raw, public_key)
            trusted = trust_key(receipt, public_key)
            receipts.append(
                verification.verify_approval_receipt(
                    raw,
                    expected=expected,
                    trust_keys={trusted.key_id: trusted},
                    now=NOW,
                )
            )
            references.append(expected)
        self.receipts = tuple(receipts)
        self.references = tuple(references)
        self.expected = aggregate.ExpectedApprovalAggregatePins(self.references)

    def assert_code(self, code: str, callback) -> aggregate.ApprovalAggregateError:
        with self.assertRaises(aggregate.ApprovalAggregateError) as captured:
            callback()
        self.assertEqual(code, captured.exception.code)
        return captured.exception

    def build(
        self,
        receipts=None,
        *,
        expected: aggregate.ExpectedApprovalAggregatePins | None = None,
    ) -> aggregate.VerifiedApprovalAggregate:
        return aggregate.aggregate_verified_approval_receipts(
            self.receipts if receipts is None else receipts,
            expected=self.expected if expected is None else expected,
        )

    def test_six_real_signatures_produce_one_deterministic_canonical_proof(self) -> None:
        result = self.build()
        reversed_result = self.build(tuple(reversed(self.receipts)))
        self.assertTrue(result.all_signatures_verified)
        self.assertEqual(result.proof_bytes, reversed_result.proof_bytes)
        self.assertEqual(result.proof_sha256, reversed_result.proof_sha256)
        self.assertEqual(
            hashlib.sha256(result.proof_bytes).hexdigest(), result.proof_sha256
        )
        self.assertEqual(self.expected.sha256, result.expected_references_sha256)
        self.assertEqual(
            verification.canonical_json_bytes(result.proof), result.proof_bytes
        )
        self.assertEqual(aggregate.AGGREGATE_PROOF_SCHEMA, result.proof["schema"])
        self.assertEqual(6, result.proof["approval_count"])
        self.assertEqual(
            list(verification.APPROVAL_KINDS),
            [item["purpose"] for item in result.proof["approvals"]],
        )
        self.assertEqual(
            tuple(
                (receipt.purpose, receipt.raw_receipt_sha256)
                for receipt in self.receipts
            ),
            result.receipt_sha256s,
        )
        self.assertNotIn("raw_receipt_bytes", result.proof_bytes.decode("utf-8"))
        self.assertNotIn("detached_signature_base64", result.proof_bytes.decode("utf-8"))

        data_owner = self.receipts[0]
        production = self.receipts[1]
        self.assertEqual(data_owner.environment_id, result.data_owner_environment_id)
        self.assertEqual(
            data_owner.approval_basis_sha256,
            result.data_owner_approval_basis_sha256,
        )
        self.assertEqual(production.environment_id, result.production_environment_id)
        self.assertEqual(
            production.approval_basis_sha256,
            result.production_approval_basis_sha256,
        )

    def test_aggregate_is_opaque_frozen_noncopyable_and_nonpickleable(self) -> None:
        result = self.build()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.proof_sha256 = sha("other")
        with self.assertRaises(TypeError):
            copy.copy(result)
        with self.assertRaises(TypeError):
            copy.deepcopy(result)
        with self.assertRaises(TypeError):
            pickle.dumps(result)
        with self.assertRaises(TypeError):
            aggregate.VerifiedApprovalAggregate(
                None,
                **{
                    field.name: getattr(result, field.name)
                    for field in dataclasses.fields(result)
                },
            )

    def _expected_with_production_scope(
        self,
        scope: dict,
        *,
        runtime_authorization: dict | None = None,
    ) -> aggregate.ExpectedApprovalAggregatePins:
        scope_bytes = verification.canonical_json_bytes(scope)
        references = [self.references[0]]
        for reference in self.references[1:]:
            replacements = {
                "environment_id": scope["worker_deployment_identity"],
                "approval_basis_sha256": scope["plan_approval_basis_sha256"],
                "scope_bytes": scope_bytes,
            }
            if reference.purpose == "runtime" and runtime_authorization is not None:
                replacements["authorization_bytes"] = verification.canonical_json_bytes(
                    runtime_authorization
                )
            references.append(dataclasses.replace(reference, **replacements))
        return aggregate.ExpectedApprovalAggregatePins(tuple(references))

    def test_environment_basis_scope_and_authorization_require_independent_pins(self) -> None:
        changed_scope = scope_for("runtime")
        changed_scope["reviewed_job_sha256"] = sha("other-reviewed-job")
        mismatched_scope = self._expected_with_production_scope(changed_scope)

        changed_basis_scope = scope_for("runtime")
        changed_basis_scope["plan_approval_basis_sha256"] = sha("other-basis")
        mismatched_basis = self._expected_with_production_scope(changed_basis_scope)

        changed_environment_scope = scope_for("runtime")
        changed_environment_scope["worker_deployment_identity"] = (
            "semantic-worker:production-b"
        )
        changed_runtime_authorization = authorization_for("runtime")
        changed_runtime_authorization["worker_deployment_identity"] = (
            "semantic-worker:production-b"
        )
        mismatched_environment = self._expected_with_production_scope(
            changed_environment_scope,
            runtime_authorization=changed_runtime_authorization,
        )

        changed_authorization = authorization_for("cost_owner")
        changed_authorization["max_cost_minor_units"] += 1
        references = list(self.references)
        references[1] = dataclasses.replace(
            references[1],
            authorization_bytes=verification.canonical_json_bytes(
                changed_authorization
            ),
        )
        mismatched_authorization = aggregate.ExpectedApprovalAggregatePins(
            tuple(references)
        )

        for expected in (
            mismatched_environment,
            mismatched_basis,
            mismatched_scope,
            mismatched_authorization,
        ):
            with self.subTest(expected_sha256=expected.sha256):
                self.assert_code(
                    "APPROVAL_AGGREGATE_REFERENCE_MISMATCH",
                    lambda expected=expected: self.build(expected=expected),
                )

    def test_trust_and_lifetime_policy_require_the_exact_verification_pins(
        self,
    ) -> None:
        replacements = (
            {"trust_bundle_revision": "sha256:" + sha("other-trust-revision")},
            {"issuer_membership_sha256": sha("other-membership")},
            {
                "maximum_lifetime_seconds": (
                    self.references[1].maximum_lifetime_seconds + 1
                )
            },
        )
        for replacement in replacements:
            references = list(self.references)
            references[1] = dataclasses.replace(references[1], **replacement)
            expected = aggregate.ExpectedApprovalAggregatePins(tuple(references))
            with self.subTest(replacement=tuple(replacement)):
                self.assert_code(
                    "APPROVAL_AGGREGATE_REFERENCE_MISMATCH",
                    lambda expected=expected: self.build(expected=expected),
                )

    def test_duplicate_missing_and_internally_tampered_receipts_fail_closed(self) -> None:
        duplicate = self.receipts[:-1] + (self.receipts[0],)
        self.assert_code(
            "APPROVAL_AGGREGATE_SET_INVALID",
            lambda: self.build(duplicate),
        )

        object.__setattr__(self.receipts[2], "scope_sha256", sha("tampered-scope"))
        self.assert_code(
            "APPROVAL_AGGREGATE_INTEGRITY_INVALID",
            self.build,
        )

    def test_detached_signature_and_payload_must_match_raw_receipt(self) -> None:
        original_payload = self.receipts[2].signed_payload_bytes
        original_payload_sha256 = self.receipts[2].signed_payload_sha256
        signed_payload = verification.canonical_json_bytes(
            {"forged": "same-process-object-field"}
        )
        object.__setattr__(self.receipts[2], "signed_payload_bytes", signed_payload)
        object.__setattr__(
            self.receipts[2],
            "signed_payload_sha256",
            hashlib.sha256(signed_payload).hexdigest(),
        )
        self.assert_code("APPROVAL_AGGREGATE_INTEGRITY_INVALID", self.build)

        object.__setattr__(
            self.receipts[2],
            "signed_payload_bytes",
            original_payload,
        )
        object.__setattr__(
            self.receipts[2],
            "signed_payload_sha256",
            original_payload_sha256,
        )
        forged_signature = b"z" * 64
        object.__setattr__(self.receipts[2], "signature_bytes", forged_signature)
        object.__setattr__(
            self.receipts[2],
            "signature_sha256",
            hashlib.sha256(forged_signature).hexdigest(),
        )
        self.assert_code("APPROVAL_AGGREGATE_INTEGRITY_INVALID", self.build)


if __name__ == "__main__":
    unittest.main()
