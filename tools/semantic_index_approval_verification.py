"""Offline Ed25519 verification for semantic-index approval receipts.

The v2 contract validator proves that an approval receipt is canonical and
semantically consistent with a job/plan bundle.  It intentionally does not
make an issuer public-key trust decision.  This module supplies that missing
cryptographic boundary without ambient authority:

* the exact canonical receipt bytes and their digest are caller supplied;
* purpose, environment, complete job scope, authorization and validity window
  are independently pinned by the caller;
* an explicit, deployment-owned key map binds issuer, role, key revision,
  trust bundle, purpose and environment to raw Ed25519 public-key bytes; and
* no clock, path, environment variable, service, socket or network is read.

For ``production_build`` approvals, ``environment_id`` is the signed
``worker_deployment_identity``.  A data-owner selection is deliberately
deployment-independent, so its environment is the signed ``content_revision``.

The current phase-evidence schema is *not* cryptographically verifiable: its
``worker_signature`` carries only a key revision and two SHA-256 declarations,
not a detached signature, algorithm, key identity or trust-bundle binding.
``verify_phase_evidence_wire_signature`` therefore fails closed until a new
release-pinned evidence contract transports those fields.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import datetime as dt
import hashlib
import hmac
import json
import re
import unicodedata
from typing import Any, NoReturn


RECEIPT_SCHEMA = "simworld-semantic-index-approval-receipt/v1"
TRUST_BUNDLE_SCHEMA = "simworld-approval-trust-bundle/v1"
SIGNATURE_ALGORITHM = "ed25519"
SIGNATURE_CANONICALIZATION = "rfc8785-jcs-v1"
MAX_DOCUMENT_BYTES = 64 * 1024
MAX_POLICY_LIFETIME_SECONDS = 366 * 24 * 60 * 60
MAX_JSON_INTEGER = 9_007_199_254_740_991

APPROVAL_KINDS = (
    "data_owner_review",
    "cost_owner",
    "admin_state_change",
    "deployment",
    "runtime",
    "rollback_readiness",
)
APPROVAL_ROLES = {
    "data_owner_review": "data_owner",
    "cost_owner": "cost_owner",
    "admin_state_change": "state_change_admin",
    "deployment": "deployment_attestor",
    "runtime": "runtime_owner",
    "rollback_readiness": "rollback_owner",
}

PHASE_EVIDENCE_WIRE_SIGNATURE_REQUIRED_FIELDS = (
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
)

SERIALIZATION_CONTRACT = {
    "encoding": "utf-8",
    "canonicalization": "rfc8785-jcs-v1",
    "duplicate_keys": "reject",
    "unicode_normalization": "require_already_nfc",
    "numbers": "integers_only",
    "nonfinite_numbers": "reject",
    "trailing_newline": False,
}

_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_SHA256_REVISION_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,159}$")
_RECEIPT_ID_RE = re.compile(
    r"^semantic-index-approval:[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,130}$"
)
_GENERATION_ID_RE = re.compile(r"^semantic-generation:[a-f0-9]{64}$")
_SNAPSHOT_REVISION_RE = re.compile(
    r"^asset-snapshot-[A-Za-z0-9][A-Za-z0-9._:@/+\-]{0,223}$"
)
_IMAGE_RE = re.compile(r"^[^\s@]+@sha256:[a-f0-9]{64}$")
_RFC3339_RE = re.compile(
    r"^[0-9]{4}-(0[1-9]|1[0-2])-([0-2][0-9]|3[01])T"
    r"([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
    r"(\.[0-9]{1,9})?(Z|[+-]([01][0-9]|2[0-3]):[0-5][0-9])$"
)
_BASE64_SIGNATURE_RE = re.compile(r"^[A-Za-z0-9+/]{86}==$")

_RECEIPT_KEYS = frozenset(
    {
        "schema",
        "receipt_id",
        "approval_kind",
        "decision",
        "approval_basis_sha256",
        "scope",
        "authorization",
        "issuer",
        "trust_bundle",
        "issued_at",
        "expires_at",
        "signature",
        "serialization_contract",
    }
)
_ISSUER_KEYS = frozenset({"issuer_id", "role", "key_id", "key_revision"})
_TRUST_BUNDLE_KEYS = frozenset(
    {"schema", "revision", "sha256", "issuer_membership_sha256"}
)
_SIGNATURE_KEYS = frozenset(
    {
        "algorithm",
        "canonicalization",
        "signed_payload_sha256",
        "detached_signature_sha256",
        "detached_signature_base64",
    }
)
_DATA_OWNER_SCOPE_KEYS = frozenset(
    {
        "scope_kind",
        "source_v1_job_sha256",
        "source_v1_job_revision",
        "reviewed_selection_basis_sha256",
        "accepted_assets_sha256",
        "accepted_asset_count",
        "rejection_ledger_sha256",
        "content_revision",
    }
)
_PRODUCTION_SCOPE_KEYS = frozenset(
    {
        "scope_kind",
        "reviewed_job_sha256",
        "reviewed_job_revision",
        "plan_approval_basis_sha256",
        "generation_id",
        "generation_nonce_sha256",
        "target_snapshot_revision",
        "worker_deployment_identity",
        "active_generation_pointer_identity",
        "expected_active_generation_epoch",
    }
)
_AUTHORIZATION_KEYS = {
    "data_owner_review": frozenset(
        {"kind", "reviewed_selection_approved", "production_execution_authorized"}
    ),
    "cost_owner": frozenset(
        {
            "kind",
            "max_caption_calls",
            "max_output_tokens",
            "currency",
            "minor_unit_exponent",
            "max_cost_minor_units",
            "rounding_mode",
            "retry_charge_policy",
        }
    ),
    "admin_state_change": frozenset(
        {
            "kind",
            "allowed_mutating_phases",
            "allowed_control_operations",
            "activation_authorized",
        }
    ),
    "deployment": frozenset(
        {"kind", "deployment_preflight_sha256", "worker_image", "worker_protocol_sha256"}
    ),
    "runtime": frozenset(
        {"kind", "ue_lease_id", "ue_lease_expires_at", "worker_deployment_identity"}
    ),
    "rollback_readiness": frozenset(
        {
            "kind",
            "bootstrap",
            "previous_generation_id",
            "previous_snapshot_revision",
            "rollback_readiness_receipt_sha256",
            "active_generation_pointer_identity",
            "expected_active_generation_epoch",
        }
    ),
}

_MUTATING_PHASES = ["inspect", "render", "caption", "embed", "postgres", "qdrant"]
_CONTROL_OPERATIONS = [
    "query_phase_status",
    "recover_phase_receipt",
    "cancel_phase_work",
    "quarantine_generation",
]


@dataclasses.dataclass(frozen=True, slots=True)
class ApprovalVerificationError(Exception):
    """Typed, bounded failure that never retains receipt-controlled text."""

    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def _raise(code: str, message: str) -> NoReturn:
    raise ApprovalVerificationError(code=code, message=message)


def _integer(
    value: Any,
    label: str,
    *,
    minimum: int = 0,
    maximum: int = MAX_JSON_INTEGER,
) -> int:
    if type(value) is not int:
        _raise("APPROVAL_VALUE_INVALID", f"{label} is not an integer")
    if not minimum <= value <= maximum:
        _raise("APPROVAL_VALUE_INVALID", f"{label} is outside its allowed range")
    return value


def _nfc_text(value: Any, label: str, *, maximum: int = 4096) -> str:
    if type(value) is not str or not value:
        _raise("APPROVAL_VALUE_INVALID", f"{label} must be non-empty text")
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeError:
        _raise("APPROVAL_VALUE_INVALID", f"{label} is not UTF-8 text")
    if len(encoded) > maximum or unicodedata.normalize("NFC", value) != value:
        _raise("APPROVAL_VALUE_INVALID", f"{label} is not canonical text")
    if any(unicodedata.category(character) in {"Cc", "Cs"} for character in value):
        _raise("APPROVAL_VALUE_INVALID", f"{label} contains a control character")
    return value


def _matched(value: Any, label: str, pattern: re.Pattern[str], maximum: int) -> str:
    text = _nfc_text(value, label, maximum=maximum)
    if not pattern.fullmatch(text) or "://" in text:
        _raise("APPROVAL_VALUE_INVALID", f"{label} has an invalid identity")
    return text


def _safe_id(value: Any, label: str) -> str:
    return _matched(value, label, _SAFE_ID_RE, 160)


def _sha256(value: Any, label: str) -> str:
    if type(value) is not str or not _SHA256_RE.fullmatch(value):
        _raise("APPROVAL_VALUE_INVALID", f"{label} is not a SHA-256 digest")
    return value


def _sha256_revision(value: Any, label: str) -> str:
    if type(value) is not str or not _SHA256_REVISION_RE.fullmatch(value):
        _raise("APPROVAL_VALUE_INVALID", f"{label} is not a SHA-256 revision")
    return value


def _generation_id(value: Any, label: str) -> str:
    return _matched(value, label, _GENERATION_ID_RE, 160)


def _snapshot_revision(value: Any, label: str) -> str:
    return _matched(value, label, _SNAPSHOT_REVISION_RE, 240)


def _image(value: Any, label: str) -> str:
    return _matched(value, label, _IMAGE_RE, 512)


def _exact_mapping(value: Any, keys: frozenset[str], label: str) -> dict[str, Any]:
    if type(value) is not dict or frozenset(value) != keys:
        _raise("APPROVAL_SHAPE_INVALID", f"{label} has an invalid field set")
    return value


def _validate_json_value(value: Any, *, depth: int = 0) -> None:
    if depth > 16:
        _raise("APPROVAL_JSON_INVALID", "Approval JSON nesting is too deep")
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        _integer(value, "JSON integer", minimum=-MAX_JSON_INTEGER)
        return
    if type(value) is str:
        _nfc_text(value, "JSON string")
        return
    if type(value) is list:
        if len(value) > 128:
            _raise("APPROVAL_JSON_INVALID", "Approval JSON array is too large")
        for item in value:
            _validate_json_value(item, depth=depth + 1)
        return
    if type(value) is dict:
        if len(value) > 128:
            _raise("APPROVAL_JSON_INVALID", "Approval JSON object is too large")
        for key, item in value.items():
            _nfc_text(key, "JSON object key", maximum=128)
            _validate_json_value(item, depth=depth + 1)
        return
    _raise("APPROVAL_JSON_INVALID", "Approval JSON contains a forbidden scalar")


def _jcs_string(value: str) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False).encode(
            "utf-8", "strict"
        )
    except (TypeError, ValueError, UnicodeError):
        _raise("APPROVAL_JSON_INVALID", "Approval JSON string cannot be encoded")


def _utf16_sort_key(value: str) -> bytes:
    try:
        return value.encode("utf-16-be", "strict")
    except UnicodeError:
        _raise("APPROVAL_JSON_INVALID", "Approval JSON key is not valid Unicode")


def _encode_jcs(value: Any) -> bytes:
    if value is None:
        return b"null"
    if value is True:
        return b"true"
    if value is False:
        return b"false"
    if type(value) is int:
        return str(value).encode("ascii")
    if type(value) is str:
        return _jcs_string(value)
    if type(value) is list:
        return b"[" + b",".join(_encode_jcs(item) for item in value) + b"]"
    if type(value) is dict:
        members = []
        for key in sorted(value, key=_utf16_sort_key):
            members.append(_jcs_string(key) + b":" + _encode_jcs(value[key]))
        return b"{" + b",".join(members) + b"}"
    _raise("APPROVAL_JSON_INVALID", "Approval JSON contains a forbidden scalar")


def canonical_json_bytes(value: Any) -> bytes:
    """Encode the integer-only, already-NFC RFC 8785 subset used here."""

    _validate_json_value(value)
    return _encode_jcs(value)


def _reject_constant(_value: str) -> NoReturn:
    _raise("APPROVAL_JSON_INVALID", "Approval JSON contains a forbidden number")


def _reject_float(_value: str) -> NoReturn:
    _raise("APPROVAL_JSON_INVALID", "Approval JSON floats are forbidden")


def _parse_integer(value: str) -> int:
    try:
        parsed = int(value, 10)
    except ValueError:
        _raise("APPROVAL_JSON_INVALID", "Approval JSON integer is invalid")
    return _integer(parsed, "JSON integer", minimum=-MAX_JSON_INTEGER)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            _raise("APPROVAL_JSON_INVALID", "Approval JSON contains a duplicate key")
        value[key] = item
    return value


def decode_canonical_json(raw: bytes) -> Any:
    """Parse exact canonical JSON and reject alternate byte representations."""

    if type(raw) is not bytes or not raw or len(raw) > MAX_DOCUMENT_BYTES:
        _raise("APPROVAL_DOCUMENT_SIZE_INVALID", "Approval byte length is invalid")
    try:
        value = json.loads(
            raw.decode("utf-8", "strict"),
            object_pairs_hook=_unique_object,
            parse_int=_parse_integer,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except ApprovalVerificationError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError):
        _raise("APPROVAL_JSON_INVALID", "Approval is not valid canonical JSON")
    _validate_json_value(value)
    if not hmac.compare_digest(canonical_json_bytes(value), raw):
        _raise("APPROVAL_JSON_NONCANONICAL", "Approval is not canonical JSON")
    return value


def _parse_time(value: Any, label: str) -> dt.datetime:
    if type(value) is not str or not _RFC3339_RE.fullmatch(value):
        _raise("APPROVAL_FRESHNESS_INVALID", f"{label} is not canonical RFC3339 time")
    if value.endswith("-00:00"):
        _raise("APPROVAL_FRESHNESS_INVALID", f"{label} has an unknown UTC offset")
    try:
        parsed = dt.datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value
        )
    except ValueError:
        _raise("APPROVAL_FRESHNESS_INVALID", f"{label} is not a real timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _raise("APPROVAL_FRESHNESS_INVALID", f"{label} has no UTC offset")
    return parsed.astimezone(dt.timezone.utc)


def _signature_bytes(value: Any) -> bytes:
    if type(value) is not str or not _BASE64_SIGNATURE_RE.fullmatch(value):
        _raise("APPROVAL_SIGNATURE_ENCODING_INVALID", "Signature encoding is invalid")
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeError, binascii.Error, ValueError):
        _raise("APPROVAL_SIGNATURE_ENCODING_INVALID", "Signature encoding is invalid")
    if len(decoded) != 64 or base64.b64encode(decoded).decode("ascii") != value:
        _raise("APPROVAL_SIGNATURE_ENCODING_INVALID", "Signature encoding is invalid")
    return decoded


def _validate_scope(value: Any, purpose: str) -> tuple[dict[str, Any], str]:
    if type(value) is not dict:
        _raise("APPROVAL_SHAPE_INVALID", "Approval scope is not an exact object")
    scope_kind = value.get("scope_kind")
    if purpose == "data_owner_review":
        scope = _exact_mapping(value, _DATA_OWNER_SCOPE_KEYS, "Data-owner scope")
        if scope_kind != "reviewed_selection":
            _raise("APPROVAL_SCOPE_INVALID", "Data-owner scope kind is invalid")
        for key in (
            "source_v1_job_sha256",
            "reviewed_selection_basis_sha256",
            "accepted_assets_sha256",
            "rejection_ledger_sha256",
        ):
            _sha256(scope[key], f"data-owner scope {key}")
        _sha256_revision(scope["source_v1_job_revision"], "source v1 job revision")
        _integer(scope["accepted_asset_count"], "accepted asset count", minimum=1, maximum=4096)
        environment_id = _sha256_revision(scope["content_revision"], "content revision")
        if scope["reviewed_selection_basis_sha256"] is None:
            _raise("APPROVAL_SCOPE_INVALID", "Selection approval basis is missing")
        return dict(scope), environment_id

    scope = _exact_mapping(value, _PRODUCTION_SCOPE_KEYS, "Production scope")
    if scope_kind != "production_build":
        _raise("APPROVAL_SCOPE_INVALID", "Production scope kind is invalid")
    for key in (
        "reviewed_job_sha256",
        "plan_approval_basis_sha256",
        "generation_nonce_sha256",
    ):
        _sha256(scope[key], f"production scope {key}")
    _sha256_revision(scope["reviewed_job_revision"], "reviewed job revision")
    _generation_id(scope["generation_id"], "generation identity")
    _snapshot_revision(scope["target_snapshot_revision"], "target snapshot revision")
    environment_id = _safe_id(
        scope["worker_deployment_identity"], "worker deployment environment"
    )
    _safe_id(scope["active_generation_pointer_identity"], "active pointer identity")
    _integer(scope["expected_active_generation_epoch"], "active generation epoch")
    return dict(scope), environment_id


def _validate_authorization(value: Any, purpose: str) -> dict[str, Any]:
    authorization = _exact_mapping(
        value, _AUTHORIZATION_KEYS[purpose], "Approval authorization"
    )
    if authorization["kind"] != purpose:
        _raise("APPROVAL_AUTHORIZATION_INVALID", "Authorization purpose changed")

    if purpose == "data_owner_review":
        if authorization["reviewed_selection_approved"] is not True:
            _raise("APPROVAL_AUTHORIZATION_INVALID", "Selection was not approved")
        if authorization["production_execution_authorized"] is not False:
            _raise(
                "APPROVAL_AUTHORIZATION_INVALID",
                "Data-owner receipt must not authorize production execution",
            )
    elif purpose == "cost_owner":
        _integer(
            authorization["max_caption_calls"],
            "maximum caption calls",
            minimum=1,
            maximum=4096,
        )
        _integer(authorization["max_output_tokens"], "maximum output tokens", minimum=1)
        _integer(
            authorization["max_cost_minor_units"],
            "maximum cost minor units",
            minimum=1,
        )
        expected = {
            "currency": "USD",
            "minor_unit_exponent": 6,
            "rounding_mode": "ceiling_each_provider_charge_to_minor_unit",
            "retry_charge_policy": "all_provider_accepted_requests_counted",
        }
        if any(authorization[key] != required for key, required in expected.items()):
            _raise("APPROVAL_AUTHORIZATION_INVALID", "Cost policy changed")
    elif purpose == "admin_state_change":
        if authorization["allowed_mutating_phases"] != _MUTATING_PHASES:
            _raise("APPROVAL_AUTHORIZATION_INVALID", "Mutation phase policy changed")
        if authorization["allowed_control_operations"] != _CONTROL_OPERATIONS:
            _raise("APPROVAL_AUTHORIZATION_INVALID", "Control-operation policy changed")
        if authorization["activation_authorized"] is not False:
            _raise("APPROVAL_AUTHORIZATION_INVALID", "Build receipt authorized activation")
    elif purpose == "deployment":
        _sha256(authorization["deployment_preflight_sha256"], "deployment preflight")
        _image(authorization["worker_image"], "worker image")
        _sha256(authorization["worker_protocol_sha256"], "worker protocol")
    elif purpose == "runtime":
        _safe_id(authorization["ue_lease_id"], "UE lease identity")
        _parse_time(authorization["ue_lease_expires_at"], "UE lease expiry")
        _safe_id(
            authorization["worker_deployment_identity"],
            "runtime worker deployment identity",
        )
    elif purpose == "rollback_readiness":
        if type(authorization["bootstrap"]) is not bool:
            _raise("APPROVAL_AUTHORIZATION_INVALID", "Rollback bootstrap flag is invalid")
        bootstrap = authorization["bootstrap"]
        previous_generation = authorization["previous_generation_id"]
        previous_snapshot = authorization["previous_snapshot_revision"]
        epoch = _integer(
            authorization["expected_active_generation_epoch"],
            "rollback active generation epoch",
        )
        if bootstrap:
            if previous_generation is not None or previous_snapshot is not None or epoch != 0:
                _raise("APPROVAL_AUTHORIZATION_INVALID", "Bootstrap rollback state is invalid")
        else:
            _generation_id(previous_generation, "previous generation identity")
            _snapshot_revision(previous_snapshot, "previous snapshot revision")
            if epoch < 1:
                _raise("APPROVAL_AUTHORIZATION_INVALID", "Rollback epoch is invalid")
        _sha256(
            authorization["rollback_readiness_receipt_sha256"],
            "rollback readiness receipt",
        )
        _safe_id(
            authorization["active_generation_pointer_identity"],
            "rollback active pointer identity",
        )
    return dict(authorization)


def _validate_receipt_shape(value: Any) -> tuple[dict[str, Any], str]:
    receipt = _exact_mapping(value, _RECEIPT_KEYS, "Approval receipt")
    if receipt["schema"] != RECEIPT_SCHEMA:
        _raise("APPROVAL_SCHEMA_INVALID", "Approval receipt schema is not allowed")
    _matched(receipt["receipt_id"], "receipt identity", _RECEIPT_ID_RE, 160)
    purpose = receipt["approval_kind"]
    if type(purpose) is not str or purpose not in APPROVAL_KINDS:
        _raise("APPROVAL_PURPOSE_INVALID", "Approval purpose is not allowed")
    if receipt["decision"] != "approved":
        _raise("APPROVAL_DECISION_INVALID", "Approval decision is not approved")
    approval_basis_sha256 = _sha256(receipt["approval_basis_sha256"], "approval basis")

    scope, environment_id = _validate_scope(receipt["scope"], purpose)
    authorization = _validate_authorization(receipt["authorization"], purpose)
    expected_basis = (
        scope["reviewed_selection_basis_sha256"]
        if purpose == "data_owner_review"
        else scope["plan_approval_basis_sha256"]
    )
    if not hmac.compare_digest(approval_basis_sha256, expected_basis):
        _raise("APPROVAL_SCOPE_INVALID", "Approval basis and job scope do not match")
    if purpose == "runtime" and (
        authorization["worker_deployment_identity"] != environment_id
    ):
        _raise("APPROVAL_SCOPE_INVALID", "Runtime authorization changed environment")
    if purpose == "rollback_readiness":
        comparisons = (
            (
                authorization["active_generation_pointer_identity"],
                scope["active_generation_pointer_identity"],
            ),
            (
                authorization["expected_active_generation_epoch"],
                scope["expected_active_generation_epoch"],
            ),
        )
        if any(observed != expected for observed, expected in comparisons):
            _raise("APPROVAL_SCOPE_INVALID", "Rollback authorization changed job scope")

    issuer = _exact_mapping(receipt["issuer"], _ISSUER_KEYS, "Approval issuer")
    _safe_id(issuer["issuer_id"], "issuer identity")
    if issuer["role"] != APPROVAL_ROLES[purpose]:
        _raise("APPROVAL_ISSUER_INVALID", "Approval issuer role is not allowed")
    _safe_id(issuer["key_id"], "issuer key identity")
    _sha256_revision(issuer["key_revision"], "issuer key revision")

    trust = _exact_mapping(receipt["trust_bundle"], _TRUST_BUNDLE_KEYS, "Trust bundle")
    if trust["schema"] != TRUST_BUNDLE_SCHEMA:
        _raise("APPROVAL_TRUST_INVALID", "Trust bundle schema is not allowed")
    _sha256_revision(trust["revision"], "trust bundle revision")
    _sha256(trust["sha256"], "trust bundle")
    _sha256(trust["issuer_membership_sha256"], "issuer membership")

    issued = _parse_time(receipt["issued_at"], "approval issue time")
    expires = _parse_time(receipt["expires_at"], "approval expiry time")
    if expires <= issued:
        _raise("APPROVAL_FRESHNESS_INVALID", "Approval validity interval is invalid")

    signature = _exact_mapping(receipt["signature"], _SIGNATURE_KEYS, "Approval signature")
    if signature["algorithm"] != SIGNATURE_ALGORITHM:
        _raise("APPROVAL_SIGNATURE_INVALID", "Signature algorithm is not allowed")
    if signature["canonicalization"] != SIGNATURE_CANONICALIZATION:
        _raise("APPROVAL_SIGNATURE_INVALID", "Signature canonicalization is not allowed")
    _sha256(signature["signed_payload_sha256"], "signed approval payload")
    _sha256(signature["detached_signature_sha256"], "detached approval signature")
    _signature_bytes(signature["detached_signature_base64"])

    if receipt["serialization_contract"] != SERIALIZATION_CONTRACT:
        _raise("APPROVAL_SERIALIZATION_INVALID", "Serialization contract changed")
    return dict(receipt), environment_id


@dataclasses.dataclass(frozen=True, slots=True)
class TrustedApprovalEd25519Key:
    """One least-privilege approval key in an explicit deployment trust map."""

    issuer_id: str
    issuer_role: str
    key_id: str
    key_revision: str
    trust_bundle_revision: str
    trust_bundle_sha256: str
    issuer_membership_sha256: str
    purpose: str
    environment_id: str
    public_key_bytes: bytes

    def __post_init__(self) -> None:
        _safe_id(self.issuer_id, "trusted issuer identity")
        _safe_id(self.issuer_role, "trusted issuer role")
        _safe_id(self.key_id, "trusted key identity")
        _sha256_revision(self.key_revision, "trusted key revision")
        _sha256_revision(self.trust_bundle_revision, "trusted bundle revision")
        _sha256(self.trust_bundle_sha256, "trusted bundle")
        _sha256(self.issuer_membership_sha256, "trusted issuer membership")
        if type(self.purpose) is not str or self.purpose not in APPROVAL_KINDS:
            _raise("APPROVAL_TRUST_KEY_INVALID", "Trusted key purpose is invalid")
        if self.issuer_role != APPROVAL_ROLES[self.purpose]:
            _raise("APPROVAL_TRUST_KEY_INVALID", "Trusted key role and purpose differ")
        _safe_id(self.environment_id, "trusted environment identity")
        if type(self.public_key_bytes) is not bytes or len(self.public_key_bytes) != 32:
            _raise(
                "APPROVAL_TRUST_KEY_INVALID",
                "Ed25519 public key must be exactly 32 canonical bytes",
            )

    @property
    def public_key_sha256(self) -> str:
        return hashlib.sha256(self.public_key_bytes).hexdigest()


@dataclasses.dataclass(frozen=True, slots=True)
class ExpectedApprovalVerificationPins:
    """Independent policy pins; receipt fields never authorize themselves."""

    raw_receipt_sha256: str
    receipt_id: str
    purpose: str
    environment_id: str
    approval_basis_sha256: str
    scope_bytes: bytes
    authorization_bytes: bytes
    issuer_id: str
    issuer_role: str
    issuer_key_id: str
    issuer_key_revision: str
    trust_bundle_revision: str
    trust_bundle_sha256: str
    issuer_membership_sha256: str
    issuer_public_key_sha256: str
    issued_at: str
    expires_at: str
    maximum_lifetime_seconds: int

    def __post_init__(self) -> None:
        _sha256(self.raw_receipt_sha256, "expected approval receipt")
        _matched(self.receipt_id, "expected receipt identity", _RECEIPT_ID_RE, 160)
        if type(self.purpose) is not str or self.purpose not in APPROVAL_KINDS:
            _raise("APPROVAL_EXPECTATION_INVALID", "Expected purpose is invalid")
        _safe_id(self.environment_id, "expected environment identity")
        _sha256(self.approval_basis_sha256, "expected approval basis")
        expected_scope = decode_canonical_json(self.scope_bytes)
        scope, environment_id = _validate_scope(expected_scope, self.purpose)
        expected_authorization = decode_canonical_json(self.authorization_bytes)
        _validate_authorization(expected_authorization, self.purpose)
        if environment_id != self.environment_id:
            _raise("APPROVAL_EXPECTATION_INVALID", "Expected environment and scope differ")
        expected_basis = (
            scope["reviewed_selection_basis_sha256"]
            if self.purpose == "data_owner_review"
            else scope["plan_approval_basis_sha256"]
        )
        if not hmac.compare_digest(expected_basis, self.approval_basis_sha256):
            _raise("APPROVAL_EXPECTATION_INVALID", "Expected basis and scope differ")

        _safe_id(self.issuer_id, "expected issuer identity")
        _safe_id(self.issuer_role, "expected issuer role")
        if self.issuer_role != APPROVAL_ROLES[self.purpose]:
            _raise("APPROVAL_EXPECTATION_INVALID", "Expected issuer role is invalid")
        _safe_id(self.issuer_key_id, "expected issuer key identity")
        _sha256_revision(self.issuer_key_revision, "expected issuer key revision")
        _sha256_revision(self.trust_bundle_revision, "expected trust bundle revision")
        _sha256(self.trust_bundle_sha256, "expected trust bundle")
        _sha256(self.issuer_membership_sha256, "expected issuer membership")
        _sha256(self.issuer_public_key_sha256, "expected issuer public key")

        issued = _parse_time(self.issued_at, "expected approval issue time")
        expires = _parse_time(self.expires_at, "expected approval expiry time")
        maximum = _integer(
            self.maximum_lifetime_seconds,
            "maximum approval lifetime",
            minimum=1,
            maximum=MAX_POLICY_LIFETIME_SECONDS,
        )
        if expires <= issued or (expires - issued).total_seconds() > maximum:
            _raise("APPROVAL_EXPECTATION_INVALID", "Expected time window is invalid")


_RESULT_SENTINEL = object()


@dataclasses.dataclass(frozen=True, slots=True, init=False)
class VerifiedApprovalReceipt:
    """Opaque immutable result created only after real Ed25519 verification."""

    raw_receipt_bytes: bytes
    raw_receipt_sha256: str
    signed_payload_bytes: bytes
    signed_payload_sha256: str
    signature_bytes: bytes
    signature_sha256: str
    receipt_id: str
    purpose: str
    environment_id: str
    approval_basis_sha256: str
    scope_bytes: bytes
    scope_sha256: str
    authorization_bytes: bytes
    authorization_sha256: str
    issuer_id: str
    issuer_role: str
    issuer_key_id: str
    issuer_key_revision: str
    trust_bundle_revision: str
    trust_bundle_sha256: str
    issuer_membership_sha256: str
    issuer_public_key_sha256: str
    issued_at: str
    expires_at: str
    maximum_lifetime_seconds: int

    def __init__(
        self,
        sentinel: object,
        *,
        raw_receipt_bytes: bytes,
        raw_receipt_sha256: str,
        signed_payload_bytes: bytes,
        signed_payload_sha256: str,
        signature_bytes: bytes,
        signature_sha256: str,
        receipt_id: str,
        purpose: str,
        environment_id: str,
        approval_basis_sha256: str,
        scope_bytes: bytes,
        scope_sha256: str,
        authorization_bytes: bytes,
        authorization_sha256: str,
        issuer_id: str,
        issuer_role: str,
        issuer_key_id: str,
        issuer_key_revision: str,
        trust_bundle_revision: str,
        trust_bundle_sha256: str,
        issuer_membership_sha256: str,
        issuer_public_key_sha256: str,
        issued_at: str,
        expires_at: str,
        maximum_lifetime_seconds: int,
    ) -> None:
        if sentinel is not _RESULT_SENTINEL:
            raise TypeError("VerifiedApprovalReceipt cannot be constructed directly")
        values = locals()
        for field in dataclasses.fields(self):
            object.__setattr__(self, field.name, values[field.name])

    @property
    def receipt(self) -> dict[str, Any]:
        receipt, _environment = _validate_receipt_shape(
            decode_canonical_json(self.raw_receipt_bytes)
        )
        return receipt

    @property
    def signature_verified(self) -> bool:
        return True

    def __reduce__(self) -> Any:
        raise TypeError("VerifiedApprovalReceipt cannot be serialized")

    def __copy__(self) -> Any:
        raise TypeError("VerifiedApprovalReceipt cannot be copied")

    def __deepcopy__(self, _memo: Any) -> Any:
        raise TypeError("VerifiedApprovalReceipt cannot be copied")


def _validate_expected_pins(
    receipt: dict[str, Any],
    environment_id: str,
    expected: ExpectedApprovalVerificationPins,
) -> None:
    issuer = receipt["issuer"]
    trust = receipt["trust_bundle"]
    comparisons = (
        (receipt["receipt_id"], expected.receipt_id),
        (receipt["approval_kind"], expected.purpose),
        (environment_id, expected.environment_id),
        (receipt["approval_basis_sha256"], expected.approval_basis_sha256),
        (issuer["issuer_id"], expected.issuer_id),
        (issuer["role"], expected.issuer_role),
        (issuer["key_id"], expected.issuer_key_id),
        (issuer["key_revision"], expected.issuer_key_revision),
        (trust["revision"], expected.trust_bundle_revision),
        (trust["sha256"], expected.trust_bundle_sha256),
        (trust["issuer_membership_sha256"], expected.issuer_membership_sha256),
        (receipt["issued_at"], expected.issued_at),
        (receipt["expires_at"], expected.expires_at),
    )
    if any(observed != required for observed, required in comparisons):
        _raise("APPROVAL_PIN_MISMATCH", "Approval does not match deployment-pinned policy")
    if not hmac.compare_digest(
        canonical_json_bytes(receipt["scope"]), expected.scope_bytes
    ):
        _raise("APPROVAL_SCOPE_MISMATCH", "Approval job scope changed")
    if not hmac.compare_digest(
        canonical_json_bytes(receipt["authorization"]), expected.authorization_bytes
    ):
        _raise("APPROVAL_AUTHORIZATION_MISMATCH", "Approval authorization changed")


def _validated_trust_key(
    receipt: dict[str, Any],
    environment_id: str,
    expected: ExpectedApprovalVerificationPins,
    trust_keys: dict[str, TrustedApprovalEd25519Key],
) -> TrustedApprovalEd25519Key:
    if type(trust_keys) is not dict or not 1 <= len(trust_keys) <= 64:
        _raise("APPROVAL_TRUST_MAP_INVALID", "Approval trust key map is invalid")
    copied: dict[str, TrustedApprovalEd25519Key] = {}
    for key_id, key in trust_keys.items():
        _safe_id(key_id, "trust map key identity")
        if type(key) is not TrustedApprovalEd25519Key or key.key_id != key_id:
            _raise("APPROVAL_TRUST_MAP_INVALID", "Approval trust key map is invalid")
        copied[key_id] = key

    issuer = receipt["issuer"]
    trust = receipt["trust_bundle"]
    trusted = copied.get(issuer["key_id"])
    if trusted is None:
        _raise("APPROVAL_TRUST_KEY_UNKNOWN", "Approval issuer key is not trusted")
    comparisons = (
        (trusted.issuer_id, issuer["issuer_id"]),
        (trusted.issuer_role, issuer["role"]),
        (trusted.key_revision, issuer["key_revision"]),
        (trusted.trust_bundle_revision, trust["revision"]),
        (trusted.trust_bundle_sha256, trust["sha256"]),
        (trusted.issuer_membership_sha256, trust["issuer_membership_sha256"]),
        (trusted.purpose, receipt["approval_kind"]),
        (trusted.environment_id, environment_id),
        (trusted.public_key_sha256, expected.issuer_public_key_sha256),
    )
    if any(observed != required for observed, required in comparisons):
        _raise("APPROVAL_TRUST_KEY_MISMATCH", "Trusted approval key binding changed")
    return trusted


def _validate_freshness(
    receipt: dict[str, Any],
    expected: ExpectedApprovalVerificationPins,
    now: dt.datetime,
) -> None:
    if type(now) is not dt.datetime or now.tzinfo is not dt.timezone.utc:
        _raise("APPROVAL_FRESHNESS_INVALID", "Explicit current time is not exact UTC")
    issued = _parse_time(receipt["issued_at"], "approval issue time")
    expires = _parse_time(receipt["expires_at"], "approval expiry time")
    maximum = expected.maximum_lifetime_seconds
    if expires <= issued or (expires - issued).total_seconds() > maximum:
        _raise("APPROVAL_FRESHNESS_INVALID", "Approval validity interval is invalid")
    if now < issued:
        _raise("APPROVAL_NOT_YET_VALID", "Approval is not yet valid")
    if now >= expires:
        _raise("APPROVAL_EXPIRED", "Approval has expired")


def _verify_ed25519(public_key_bytes: bytes, signature: bytes, payload: bytes) -> None:
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.exceptions import UnsupportedAlgorithm
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except (ImportError, ModuleNotFoundError):
        _raise(
            "APPROVAL_CRYPTO_UNAVAILABLE",
            "Approved Ed25519 verification dependency is unavailable",
        )
    try:
        public_key = Ed25519PublicKey.from_public_bytes(public_key_bytes)
        public_key.verify(signature, payload)
    except InvalidSignature:
        _raise("APPROVAL_SIGNATURE_INVALID", "Ed25519 signature verification failed")
    except UnsupportedAlgorithm:
        _raise(
            "APPROVAL_CRYPTO_UNAVAILABLE",
            "Approved Ed25519 verification dependency is unavailable",
        )
    except (TypeError, ValueError):
        _raise("APPROVAL_TRUST_KEY_INVALID", "Ed25519 public key is invalid")


def verify_approval_receipt(
    raw_receipt: bytes,
    *,
    expected: ExpectedApprovalVerificationPins,
    trust_keys: dict[str, TrustedApprovalEd25519Key],
    now: dt.datetime,
) -> VerifiedApprovalReceipt:
    """Verify one exact approval receipt and return an opaque immutable result."""

    if type(expected) is not ExpectedApprovalVerificationPins:
        _raise("APPROVAL_EXPECTATION_INVALID", "Expected approval policy is invalid")
    if type(raw_receipt) is not bytes or not raw_receipt or len(raw_receipt) > MAX_DOCUMENT_BYTES:
        _raise("APPROVAL_DOCUMENT_SIZE_INVALID", "Approval byte length is invalid")
    raw_receipt_sha256 = hashlib.sha256(raw_receipt).hexdigest()
    if not hmac.compare_digest(raw_receipt_sha256, expected.raw_receipt_sha256):
        _raise("APPROVAL_RECEIPT_DIGEST_MISMATCH", "Approval receipt digest changed")

    receipt, environment_id = _validate_receipt_shape(decode_canonical_json(raw_receipt))
    _validate_expected_pins(receipt, environment_id, expected)
    trusted_key = _validated_trust_key(receipt, environment_id, expected, trust_keys)
    _validate_freshness(receipt, expected, now)

    unsigned = dict(receipt)
    signature_record = dict(unsigned.pop("signature"))
    signed_payload_bytes = canonical_json_bytes(unsigned)
    signed_payload_sha256 = hashlib.sha256(signed_payload_bytes).hexdigest()
    if not hmac.compare_digest(
        signed_payload_sha256, signature_record["signed_payload_sha256"]
    ):
        _raise(
            "APPROVAL_SIGNED_PAYLOAD_DIGEST_MISMATCH",
            "Signed approval payload digest changed",
        )
    signature_bytes = _signature_bytes(signature_record["detached_signature_base64"])
    signature_sha256 = hashlib.sha256(signature_bytes).hexdigest()
    if not hmac.compare_digest(
        signature_sha256, signature_record["detached_signature_sha256"]
    ):
        _raise("APPROVAL_SIGNATURE_DIGEST_MISMATCH", "Approval signature digest changed")
    _verify_ed25519(trusted_key.public_key_bytes, signature_bytes, signed_payload_bytes)

    scope_bytes = canonical_json_bytes(receipt["scope"])
    authorization_bytes = canonical_json_bytes(receipt["authorization"])
    issuer = receipt["issuer"]
    return VerifiedApprovalReceipt(
        _RESULT_SENTINEL,
        raw_receipt_bytes=raw_receipt,
        raw_receipt_sha256=raw_receipt_sha256,
        signed_payload_bytes=signed_payload_bytes,
        signed_payload_sha256=signed_payload_sha256,
        signature_bytes=signature_bytes,
        signature_sha256=signature_sha256,
        receipt_id=receipt["receipt_id"],
        purpose=receipt["approval_kind"],
        environment_id=environment_id,
        approval_basis_sha256=receipt["approval_basis_sha256"],
        scope_bytes=scope_bytes,
        scope_sha256=hashlib.sha256(scope_bytes).hexdigest(),
        authorization_bytes=authorization_bytes,
        authorization_sha256=hashlib.sha256(authorization_bytes).hexdigest(),
        issuer_id=issuer["issuer_id"],
        issuer_role=issuer["role"],
        issuer_key_id=issuer["key_id"],
        issuer_key_revision=issuer["key_revision"],
        trust_bundle_revision=receipt["trust_bundle"]["revision"],
        trust_bundle_sha256=receipt["trust_bundle"]["sha256"],
        issuer_membership_sha256=receipt["trust_bundle"][
            "issuer_membership_sha256"
        ],
        issuer_public_key_sha256=trusted_key.public_key_sha256,
        issued_at=receipt["issued_at"],
        expires_at=receipt["expires_at"],
        maximum_lifetime_seconds=expected.maximum_lifetime_seconds,
    )


def verify_phase_evidence_wire_signature(*_args: Any, **_kwargs: Any) -> NoReturn:
    """Reject digest-only v1 phase evidence until a signed wire contract exists."""

    _raise(
        "PHASE_EVIDENCE_SIGNATURE_CONTRACT_INSUFFICIENT",
        "Phase evidence v1 does not transport a verifiable Ed25519 signature",
    )


__all__ = [
    "APPROVAL_KINDS",
    "APPROVAL_ROLES",
    "ApprovalVerificationError",
    "ExpectedApprovalVerificationPins",
    "MAX_DOCUMENT_BYTES",
    "MAX_POLICY_LIFETIME_SECONDS",
    "PHASE_EVIDENCE_WIRE_SIGNATURE_REQUIRED_FIELDS",
    "RECEIPT_SCHEMA",
    "SERIALIZATION_CONTRACT",
    "TrustedApprovalEd25519Key",
    "VerifiedApprovalReceipt",
    "canonical_json_bytes",
    "decode_canonical_json",
    "verify_approval_receipt",
    "verify_phase_evidence_wire_signature",
]
