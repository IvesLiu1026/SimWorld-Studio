"""Fail-closed aggregation of six verified semantic-index approvals.

This module deliberately accepts only opaque :class:`VerifiedApprovalReceipt`
objects produced by ``semantic_index_approval_verification``.  Raw receipts,
request documents, and caller-provided receipt summaries are not accepted as
evidence.  Every verified result is compared with an independently supplied
``ExpectedApprovalVerificationPins`` object before it contributes to the
aggregate proof.

The proof is deterministic canonical JSON.  It is intentionally not projected
into launcher, execution-state, or terminal-receipt schemas here: a future
release-pinned signed contract must bind ``proof_sha256`` before this aggregate
can authorize a production launch.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import hashlib
import hmac
import re
from typing import Any, NoReturn

import semantic_index_approval_verification as approval_verification


AGGREGATE_PROOF_SCHEMA = (
    "simworld-semantic-index-verified-approval-aggregate-proof/v1"
)
EXPECTED_REFERENCES_SCHEMA = (
    "simworld-semantic-index-approval-aggregate-expected-references/v1"
)
APPROVAL_PURPOSES = tuple(approval_verification.APPROVAL_KINDS)
PRODUCTION_APPROVAL_PURPOSES = APPROVAL_PURPOSES[1:]

_VERIFIED_RECEIPT_CLASS = approval_verification.VerifiedApprovalReceipt
_EXPECTED_RECEIPT_PINS_CLASS = (
    approval_verification.ExpectedApprovalVerificationPins
)
_CANONICAL_JSON_BYTES = approval_verification.canonical_json_bytes
_DECODE_CANONICAL_JSON = approval_verification.decode_canonical_json

_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_SHA256_REVISION_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,159}$")
_RECEIPT_ID_RE = re.compile(
    r"^semantic-index-approval:[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,130}$"
)


@dataclasses.dataclass(frozen=True, slots=True)
class ApprovalAggregateError(Exception):
    """Typed, bounded aggregate failure without receipt-controlled text."""

    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def _raise(code: str, message: str) -> NoReturn:
    raise ApprovalAggregateError(code=code, message=message)


def _sha256(value: Any, label: str) -> str:
    if type(value) is not str or not _SHA256_RE.fullmatch(value):
        _raise("APPROVAL_AGGREGATE_VALUE_INVALID", f"{label} is not a SHA-256 digest")
    return value


def _sha256_revision(value: Any, label: str) -> str:
    if type(value) is not str or not _SHA256_REVISION_RE.fullmatch(value):
        _raise(
            "APPROVAL_AGGREGATE_VALUE_INVALID",
            f"{label} is not a SHA-256 revision",
        )
    return value


def _safe_id(value: Any, label: str) -> str:
    if type(value) is not str or not _SAFE_ID_RE.fullmatch(value):
        _raise("APPROVAL_AGGREGATE_VALUE_INVALID", f"{label} is invalid")
    return value


def _receipt_id(value: Any) -> str:
    if type(value) is not str or not _RECEIPT_ID_RE.fullmatch(value):
        _raise("APPROVAL_AGGREGATE_VALUE_INVALID", "Receipt identity is invalid")
    return value


def _bytes_digest(value: Any, expected_digest: Any, label: str) -> bytes:
    if type(value) is not bytes:
        _raise("APPROVAL_AGGREGATE_INTEGRITY_INVALID", f"{label} bytes are invalid")
    expected = _sha256(expected_digest, f"{label} digest")
    observed = hashlib.sha256(value).hexdigest()
    if not hmac.compare_digest(observed, expected):
        _raise("APPROVAL_AGGREGATE_INTEGRITY_INVALID", f"{label} digest changed")
    return value


def _same_text(observed: Any, expected: Any) -> bool:
    return type(observed) is str and type(expected) is str and observed == expected


def _same_bytes(observed: Any, expected: Any) -> bool:
    return (
        type(observed) is bytes
        and type(expected) is bytes
        and hmac.compare_digest(observed, expected)
    )


def _reference_document(
    references: tuple[approval_verification.ExpectedApprovalVerificationPins, ...],
) -> dict[str, Any]:
    approvals: list[dict[str, Any]] = []
    for reference in references:
        scope_sha256 = hashlib.sha256(reference.scope_bytes).hexdigest()
        authorization_sha256 = hashlib.sha256(
            reference.authorization_bytes
        ).hexdigest()
        approvals.append(
            {
                "purpose": reference.purpose,
                "receipt_id": reference.receipt_id,
                "raw_receipt_sha256": reference.raw_receipt_sha256,
                "environment_id": reference.environment_id,
                "approval_basis_sha256": reference.approval_basis_sha256,
                "scope_sha256": scope_sha256,
                "authorization_sha256": authorization_sha256,
                "issuer": {
                    "issuer_id": reference.issuer_id,
                    "issuer_role": reference.issuer_role,
                    "key_id": reference.issuer_key_id,
                    "key_revision": reference.issuer_key_revision,
                    "public_key_sha256": reference.issuer_public_key_sha256,
                },
                "trust_bundle": {
                    "revision": reference.trust_bundle_revision,
                    "sha256": reference.trust_bundle_sha256,
                    "issuer_membership_sha256": (
                        reference.issuer_membership_sha256
                    ),
                },
                "issued_at": reference.issued_at,
                "expires_at": reference.expires_at,
                "maximum_lifetime_seconds": reference.maximum_lifetime_seconds,
            }
        )
    return {"schema": EXPECTED_REFERENCES_SCHEMA, "approvals": approvals}


@dataclasses.dataclass(frozen=True, slots=True)
class ExpectedApprovalAggregatePins:
    """Canonical six-party policy references supplied outside receipt data."""

    references: tuple[
        approval_verification.ExpectedApprovalVerificationPins, ...
    ]

    def __post_init__(self) -> None:
        references = self.references
        if type(references) is not tuple or len(references) != len(APPROVAL_PURPOSES):
            _raise(
                "APPROVAL_AGGREGATE_EXPECTATION_INVALID",
                "Expected approval references must be one exact six-item tuple",
            )
        for reference in references:
            if type(reference) is not _EXPECTED_RECEIPT_PINS_CLASS:
                _raise(
                    "APPROVAL_AGGREGATE_EXPECTATION_INVALID",
                    "Expected approval reference type is invalid",
                )
        if tuple(reference.purpose for reference in references) != APPROVAL_PURPOSES:
            _raise(
                "APPROVAL_AGGREGATE_EXPECTATION_INVALID",
                "Expected approval purposes are incomplete or not canonical",
            )
        receipt_ids = tuple(reference.receipt_id for reference in references)
        receipt_digests = tuple(
            reference.raw_receipt_sha256 for reference in references
        )
        if len(set(receipt_ids)) != len(receipt_ids) or len(set(receipt_digests)) != len(
            receipt_digests
        ):
            _raise(
                "APPROVAL_AGGREGATE_EXPECTATION_INVALID",
                "Expected approval receipt identities are not unique",
            )

        production = references[1:]
        baseline = production[0]
        for reference in production[1:]:
            if not (
                _same_text(reference.environment_id, baseline.environment_id)
                and _same_text(
                    reference.approval_basis_sha256,
                    baseline.approval_basis_sha256,
                )
                and _same_bytes(reference.scope_bytes, baseline.scope_bytes)
            ):
                _raise(
                    "APPROVAL_AGGREGATE_EXPECTATION_INVALID",
                    "Production approval references do not share one build binding",
                )
        _CANONICAL_JSON_BYTES(_reference_document(references))

    @property
    def canonical_bytes(self) -> bytes:
        return _CANONICAL_JSON_BYTES(_reference_document(self.references))

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()


def _validate_verified_receipt_integrity(
    receipt: approval_verification.VerifiedApprovalReceipt,
) -> None:
    raw_receipt = _bytes_digest(
        receipt.raw_receipt_bytes,
        receipt.raw_receipt_sha256,
        "Raw approval receipt",
    )
    signed_payload = _bytes_digest(
        receipt.signed_payload_bytes,
        receipt.signed_payload_sha256,
        "Signed approval payload",
    )
    signature = _bytes_digest(
        receipt.signature_bytes,
        receipt.signature_sha256,
        "Approval signature",
    )
    if len(signature) != 64:
        _raise(
            "APPROVAL_AGGREGATE_INTEGRITY_INVALID",
            "Approval signature length changed",
        )
    _bytes_digest(receipt.scope_bytes, receipt.scope_sha256, "Approval scope")
    _bytes_digest(
        receipt.authorization_bytes,
        receipt.authorization_sha256,
        "Approval authorization",
    )
    if type(receipt.purpose) is not str or receipt.purpose not in APPROVAL_PURPOSES:
        _raise(
            "APPROVAL_AGGREGATE_INTEGRITY_INVALID",
            "Verified approval purpose is invalid",
        )
    _receipt_id(receipt.receipt_id)
    _safe_id(receipt.environment_id, "Approval environment")
    _sha256(receipt.approval_basis_sha256, "Approval basis")
    _safe_id(receipt.issuer_id, "Approval issuer")
    if receipt.issuer_role != approval_verification.APPROVAL_ROLES[receipt.purpose]:
        _raise(
            "APPROVAL_AGGREGATE_INTEGRITY_INVALID",
            "Verified approval issuer role changed",
        )
    _safe_id(receipt.issuer_key_id, "Approval issuer key")
    _sha256_revision(receipt.issuer_key_revision, "Approval issuer key revision")
    _sha256_revision(receipt.trust_bundle_revision, "Approval trust bundle revision")
    _sha256(receipt.trust_bundle_sha256, "Approval trust bundle")
    _sha256(receipt.issuer_membership_sha256, "Approval issuer membership")
    _sha256(receipt.issuer_public_key_sha256, "Approval issuer public key")
    if (
        type(receipt.maximum_lifetime_seconds) is not int
        or not 1
        <= receipt.maximum_lifetime_seconds
        <= approval_verification.MAX_POLICY_LIFETIME_SECONDS
    ):
        _raise(
            "APPROVAL_AGGREGATE_INTEGRITY_INVALID",
            "Verified approval lifetime policy changed",
        )
    if type(receipt.issued_at) is not str or type(receipt.expires_at) is not str:
        _raise(
            "APPROVAL_AGGREGATE_INTEGRITY_INVALID",
            "Verified approval validity window changed",
        )

    try:
        document = approval_verification.decode_canonical_json(raw_receipt)
        if type(document) is not dict:
            raise TypeError
        unsigned = dict(document)
        signature_record = unsigned.pop("signature")
        if type(signature_record) is not dict:
            raise TypeError
        raw_signature = base64.b64decode(
            signature_record["detached_signature_base64"].encode("ascii"),
            validate=True,
        )
    except (
        ApprovalAggregateError,
        approval_verification.ApprovalVerificationError,
        binascii.Error,
        KeyError,
        TypeError,
        UnicodeError,
        ValueError,
    ):
        _raise(
            "APPROVAL_AGGREGATE_INTEGRITY_INVALID",
            "Verified approval receipt bytes changed",
        )

    scope = document.get("scope")
    authorization = document.get("authorization")
    issuer = document.get("issuer")
    trust = document.get("trust_bundle")
    if not all(type(value) is dict for value in (scope, authorization, issuer, trust)):
        _raise(
            "APPROVAL_AGGREGATE_INTEGRITY_INVALID",
            "Verified approval receipt structure changed",
        )
    assert isinstance(scope, dict)
    assert isinstance(authorization, dict)
    assert isinstance(issuer, dict)
    assert isinstance(trust, dict)
    environment_id = (
        scope.get("content_revision")
        if receipt.purpose == "data_owner_review"
        else scope.get("worker_deployment_identity")
    )
    text_comparisons = (
        (document.get("receipt_id"), receipt.receipt_id),
        (document.get("approval_kind"), receipt.purpose),
        (environment_id, receipt.environment_id),
        (document.get("approval_basis_sha256"), receipt.approval_basis_sha256),
        (issuer.get("issuer_id"), receipt.issuer_id),
        (issuer.get("role"), receipt.issuer_role),
        (issuer.get("key_id"), receipt.issuer_key_id),
        (issuer.get("key_revision"), receipt.issuer_key_revision),
        (trust.get("revision"), receipt.trust_bundle_revision),
        (trust.get("sha256"), receipt.trust_bundle_sha256),
        (
            trust.get("issuer_membership_sha256"),
            receipt.issuer_membership_sha256,
        ),
        (document.get("issued_at"), receipt.issued_at),
        (document.get("expires_at"), receipt.expires_at),
        (
            signature_record.get("signed_payload_sha256"),
            receipt.signed_payload_sha256,
        ),
        (
            signature_record.get("detached_signature_sha256"),
            receipt.signature_sha256,
        ),
    )
    if (
        any(not _same_text(observed, expected) for observed, expected in text_comparisons)
        or not _same_bytes(_CANONICAL_JSON_BYTES(unsigned), signed_payload)
        or not _same_bytes(_CANONICAL_JSON_BYTES(scope), receipt.scope_bytes)
        or not _same_bytes(
            _CANONICAL_JSON_BYTES(authorization),
            receipt.authorization_bytes,
        )
        or not _same_bytes(raw_signature, signature)
    ):
        _raise(
            "APPROVAL_AGGREGATE_INTEGRITY_INVALID",
            "Verified approval fields do not match the signed receipt bytes",
        )


def _matches_expected_reference(
    receipt: approval_verification.VerifiedApprovalReceipt,
    expected: approval_verification.ExpectedApprovalVerificationPins,
) -> bool:
    text_comparisons = (
        (receipt.raw_receipt_sha256, expected.raw_receipt_sha256),
        (receipt.receipt_id, expected.receipt_id),
        (receipt.purpose, expected.purpose),
        (receipt.environment_id, expected.environment_id),
        (receipt.approval_basis_sha256, expected.approval_basis_sha256),
        (receipt.issuer_id, expected.issuer_id),
        (receipt.issuer_role, expected.issuer_role),
        (receipt.issuer_key_id, expected.issuer_key_id),
        (receipt.issuer_key_revision, expected.issuer_key_revision),
        (receipt.trust_bundle_revision, expected.trust_bundle_revision),
        (receipt.trust_bundle_sha256, expected.trust_bundle_sha256),
        (
            receipt.issuer_membership_sha256,
            expected.issuer_membership_sha256,
        ),
        (
            receipt.issuer_public_key_sha256,
            expected.issuer_public_key_sha256,
        ),
        (receipt.issued_at, expected.issued_at),
        (receipt.expires_at, expected.expires_at),
    )
    return (
        all(_same_text(observed, required) for observed, required in text_comparisons)
        and _same_bytes(receipt.scope_bytes, expected.scope_bytes)
        and _same_bytes(receipt.authorization_bytes, expected.authorization_bytes)
        and type(expected.maximum_lifetime_seconds) is int
        and receipt.maximum_lifetime_seconds == expected.maximum_lifetime_seconds
    )


def _approval_proof_entry(
    receipt: approval_verification.VerifiedApprovalReceipt,
) -> dict[str, Any]:
    return {
        "purpose": receipt.purpose,
        "receipt_id": receipt.receipt_id,
        "raw_receipt_sha256": receipt.raw_receipt_sha256,
        "signed_payload_sha256": receipt.signed_payload_sha256,
        "signature_sha256": receipt.signature_sha256,
        "environment_id": receipt.environment_id,
        "approval_basis_sha256": receipt.approval_basis_sha256,
        "scope_sha256": receipt.scope_sha256,
        "authorization_sha256": receipt.authorization_sha256,
        "issuer": {
            "issuer_id": receipt.issuer_id,
            "issuer_role": receipt.issuer_role,
            "key_id": receipt.issuer_key_id,
            "key_revision": receipt.issuer_key_revision,
            "public_key_sha256": receipt.issuer_public_key_sha256,
        },
        "trust_bundle": {
            "revision": receipt.trust_bundle_revision,
            "sha256": receipt.trust_bundle_sha256,
            "issuer_membership_sha256": receipt.issuer_membership_sha256,
        },
        "issued_at": receipt.issued_at,
        "expires_at": receipt.expires_at,
        "maximum_lifetime_seconds": receipt.maximum_lifetime_seconds,
    }


_AGGREGATE_SENTINEL = object()


@dataclasses.dataclass(frozen=True, slots=True, init=False)
class VerifiedApprovalAggregate:
    """Opaque immutable proof that all six independently pinned approvals match."""

    proof_bytes: bytes
    proof_sha256: str
    expected_references_sha256: str
    receipt_sha256s: tuple[tuple[str, str], ...]
    data_owner_environment_id: str
    data_owner_approval_basis_sha256: str
    data_owner_scope_sha256: str
    production_environment_id: str
    production_approval_basis_sha256: str
    production_scope_sha256: str

    def __init__(
        self,
        sentinel: object,
        *,
        proof_bytes: bytes,
        proof_sha256: str,
        expected_references_sha256: str,
        receipt_sha256s: tuple[tuple[str, str], ...],
        data_owner_environment_id: str,
        data_owner_approval_basis_sha256: str,
        data_owner_scope_sha256: str,
        production_environment_id: str,
        production_approval_basis_sha256: str,
        production_scope_sha256: str,
    ) -> None:
        if sentinel is not _AGGREGATE_SENTINEL:
            raise TypeError("VerifiedApprovalAggregate cannot be constructed directly")
        values = locals()
        for field in dataclasses.fields(self):
            object.__setattr__(self, field.name, values[field.name])

    @property
    def proof(self) -> dict[str, Any]:
        value = _DECODE_CANONICAL_JSON(self.proof_bytes)
        if type(value) is not dict:
            raise TypeError("Verified approval aggregate proof is not an object")
        return value

    @property
    def all_signatures_verified(self) -> bool:
        return True

    def __reduce__(self) -> Any:
        raise TypeError("VerifiedApprovalAggregate cannot be serialized")

    def __reduce_ex__(self, _protocol: int) -> Any:
        raise TypeError("VerifiedApprovalAggregate cannot be serialized")

    def __copy__(self) -> Any:
        raise TypeError("VerifiedApprovalAggregate cannot be copied")

    def __deepcopy__(self, _memo: Any) -> Any:
        raise TypeError("VerifiedApprovalAggregate cannot be copied")


def aggregate_verified_approval_receipts(
    receipts: tuple[approval_verification.VerifiedApprovalReceipt, ...],
    *,
    expected: ExpectedApprovalAggregatePins,
) -> VerifiedApprovalAggregate:
    """Bind six opaque verified receipts to six independent policy references."""

    if type(expected) is not ExpectedApprovalAggregatePins:
        _raise(
            "APPROVAL_AGGREGATE_EXPECTATION_INVALID",
            "Expected aggregate policy type is invalid",
        )
    if type(receipts) is not tuple or len(receipts) != len(APPROVAL_PURPOSES):
        _raise(
            "APPROVAL_AGGREGATE_SET_INVALID",
            "Verified approvals must be one exact six-item tuple",
        )
    for receipt in receipts:
        if type(receipt) is not _VERIFIED_RECEIPT_CLASS:
            _raise(
                "APPROVAL_AGGREGATE_RECEIPT_TYPE_INVALID",
                "Approval must be the exact opaque verified receipt type",
            )
        _validate_verified_receipt_integrity(receipt)

    by_purpose: dict[str, approval_verification.VerifiedApprovalReceipt] = {}
    for receipt in receipts:
        if receipt.purpose in by_purpose:
            _raise(
                "APPROVAL_AGGREGATE_SET_INVALID",
                "Verified approval purposes are not unique",
            )
        by_purpose[receipt.purpose] = receipt
    if tuple(sorted(by_purpose)) != tuple(sorted(APPROVAL_PURPOSES)):
        _raise(
            "APPROVAL_AGGREGATE_SET_INVALID",
            "Verified approval purpose set is incomplete",
        )

    ordered = tuple(by_purpose[purpose] for purpose in APPROVAL_PURPOSES)
    for receipt, reference in zip(ordered, expected.references, strict=True):
        if not _matches_expected_reference(receipt, reference):
            _raise(
                "APPROVAL_AGGREGATE_REFERENCE_MISMATCH",
                "Verified approval does not match independent policy pins",
            )

    production = ordered[1:]
    production_baseline = production[0]
    for receipt in production[1:]:
        if not (
            _same_text(
                receipt.environment_id,
                production_baseline.environment_id,
            )
            and _same_text(
                receipt.approval_basis_sha256,
                production_baseline.approval_basis_sha256,
            )
            and _same_bytes(receipt.scope_bytes, production_baseline.scope_bytes)
        ):
            _raise(
                "APPROVAL_AGGREGATE_PRODUCTION_BINDING_MISMATCH",
                "Production approvals do not share one exact build binding",
            )

    expected_references_sha256 = expected.sha256
    data_owner = ordered[0]
    proof = {
        "schema": AGGREGATE_PROOF_SCHEMA,
        "approval_count": len(ordered),
        "expected_references_sha256": expected_references_sha256,
        "data_owner_binding": {
            "environment_id": data_owner.environment_id,
            "approval_basis_sha256": data_owner.approval_basis_sha256,
            "scope_sha256": data_owner.scope_sha256,
        },
        "production_binding": {
            "environment_id": production_baseline.environment_id,
            "approval_basis_sha256": production_baseline.approval_basis_sha256,
            "scope_sha256": production_baseline.scope_sha256,
        },
        "approvals": [_approval_proof_entry(receipt) for receipt in ordered],
    }
    proof_bytes = _CANONICAL_JSON_BYTES(proof)
    proof_sha256 = hashlib.sha256(proof_bytes).hexdigest()
    receipt_sha256s = tuple(
        (receipt.purpose, receipt.raw_receipt_sha256) for receipt in ordered
    )
    return VerifiedApprovalAggregate(
        _AGGREGATE_SENTINEL,
        proof_bytes=proof_bytes,
        proof_sha256=proof_sha256,
        expected_references_sha256=expected_references_sha256,
        receipt_sha256s=receipt_sha256s,
        data_owner_environment_id=data_owner.environment_id,
        data_owner_approval_basis_sha256=data_owner.approval_basis_sha256,
        data_owner_scope_sha256=data_owner.scope_sha256,
        production_environment_id=production_baseline.environment_id,
        production_approval_basis_sha256=(
            production_baseline.approval_basis_sha256
        ),
        production_scope_sha256=production_baseline.scope_sha256,
    )


__all__ = [
    "AGGREGATE_PROOF_SCHEMA",
    "APPROVAL_PURPOSES",
    "ApprovalAggregateError",
    "EXPECTED_REFERENCES_SCHEMA",
    "ExpectedApprovalAggregatePins",
    "PRODUCTION_APPROVAL_PURPOSES",
    "VerifiedApprovalAggregate",
    "aggregate_verified_approval_receipts",
]
