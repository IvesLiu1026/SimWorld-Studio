"""Verify signed semantic-index launcher receipts without ambient authority.

The launcher handoff receipt proves a local process/FD observation, but it is
deliberately not a signature or a deployment trust boundary.  This module
verifies a separate, closed launcher-verification receipt whose signed payload
binds that raw handoff digest and every source/runtime/plan pin consumed by the
v2 execution-state and terminal-receipt contracts.

All authority is explicit: callers provide the exact expected receipt and pin
set, a deployment-owned Ed25519 trust-key map, and a timezone-aware current
time.  The module does not read paths or environment variables, inspect its own
source, query a clock, open a socket, start a process, or access a network.
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
from typing import Any

import semantic_index_launcher_handoff as launcher_handoff


RECEIPT_SCHEMA = "simworld-semantic-index-launcher-verification-receipt/v1"
HANDOFF_RECEIPT_SCHEMA = "simworld-semantic-index-launcher-handoff-receipt/v1"
TRUST_BUNDLE_SCHEMA = "simworld-launcher-verification-trust-bundle/v1"
ISSUER_ROLE = "deployment_launcher_attestor"
SIGNATURE_ALGORITHM = "ed25519"
SIGNATURE_CANONICALIZATION = "rfc8785-jcs-v1"
MAX_DOCUMENT_BYTES = 64 * 1024
MAX_JSON_INTEGER = 9_007_199_254_740_991
MAX_RECEIPT_LIFETIME_SECONDS = 300

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SHA256_REVISION_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,159}$")
_RECEIPT_ID_RE = re.compile(
    r"^semantic-index-launcher-verification:[A-Za-z0-9]"
    r"[A-Za-z0-9._:@+\-]{0,122}$"
)
_IMAGE_RE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
_RFC3339_SECONDS_RE = re.compile(
    r"^[0-9]{4}-(0[1-9]|1[0-2])-([0-2][0-9]|3[01])T"
    r"([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]Z$"
)
_BASE64_SIGNATURE_RE = re.compile(r"^[A-Za-z0-9+/]{86}==$")

SERIALIZATION_CONTRACT = {
    "encoding": "utf-8",
    "canonicalization": "rfc8785-jcs-v1",
    "duplicate_keys": "reject",
    "unicode_normalization": "require_already_nfc",
    "numbers": "integers_only",
    "nonfinite_numbers": "reject",
    "trailing_newline": False,
}

_RECEIPT_KEYS = frozenset(
    {
        "schema",
        "receipt_id",
        "issuer",
        "trust_bundle",
        "launcher",
        "handoff",
        "pins",
        "verification",
        "issued_at",
        "not_before",
        "expires_at",
        "signature",
        "serialization_contract",
    }
)
_ISSUER_KEYS = frozenset({"issuer_id", "role", "key_id", "key_revision"})
_TRUST_BUNDLE_KEYS = frozenset(
    {
        "schema",
        "revision",
        "sha256",
        "issuer_membership_sha256",
        "issuer_public_key_sha256",
    }
)
_LAUNCHER_KEYS = frozenset({"launcher_identity", "launcher_uid", "coordinator_uid"})
_HANDOFF_KEYS = frozenset(
    {"receipt_schema", "raw_receipt_sha256", "opaque_connected_fd_handoff_verified"}
)
_PIN_KEYS = frozenset(
    {
        "interpreter_sha256",
        "coordinator_source_closure_sha256",
        "adapter_source_sha256",
        "worker_protocol_sha256",
        "worker_source_closure_sha256",
        "schemas_bundle_sha256",
        "dependency_lock_sha256",
        "sql_bundle_sha256",
        "plan_sha256",
        "worker_image",
        "worker_runtime_attestation_sha256",
    }
)
_VERIFICATION_KEYS = frozenset({"verified_before_executor_start"})
_SIGNATURE_KEYS = frozenset(
    {
        "algorithm",
        "canonicalization",
        "signed_payload_sha256",
        "detached_signature_sha256",
        "detached_signature_base64",
    }
)


@dataclasses.dataclass(frozen=True, slots=True)
class LauncherVerificationError(Exception):
    """Typed, bounded failure that never retains receipt-controlled text."""

    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def _raise(code: str, message: str) -> None:
    raise LauncherVerificationError(code, message)


def _integer(
    value: Any,
    label: str,
    *,
    minimum: int = 0,
    maximum: int = MAX_JSON_INTEGER,
) -> int:
    if type(value) is not int:
        _raise("VERIFICATION_VALUE_INVALID", f"{label} is not an integer")
    if not minimum <= value <= maximum:
        _raise("VERIFICATION_VALUE_INVALID", f"{label} is outside its allowed range")
    return value


def _nfc_text(value: Any, label: str, *, maximum: int = 512) -> str:
    if type(value) is not str or not value:
        _raise("VERIFICATION_VALUE_INVALID", f"{label} must be non-empty text")
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeError:
        _raise("VERIFICATION_VALUE_INVALID", f"{label} is not UTF-8 text")
    if len(encoded) > maximum or unicodedata.normalize("NFC", value) != value:
        _raise("VERIFICATION_VALUE_INVALID", f"{label} is not canonical text")
    if any(unicodedata.category(character) in {"Cc", "Cs"} for character in value):
        _raise("VERIFICATION_VALUE_INVALID", f"{label} contains a control character")
    return value


def _matched(value: Any, label: str, pattern: re.Pattern[str], maximum: int) -> str:
    text = _nfc_text(value, label, maximum=maximum)
    if not pattern.fullmatch(text) or "://" in text:
        _raise("VERIFICATION_VALUE_INVALID", f"{label} has an invalid identity")
    return text


def _sha256(value: Any, label: str) -> str:
    if type(value) is not str or not _SHA256_RE.fullmatch(value):
        _raise("VERIFICATION_VALUE_INVALID", f"{label} is not a SHA-256 digest")
    return value


def _sha256_revision(value: Any, label: str) -> str:
    if type(value) is not str or not _SHA256_REVISION_RE.fullmatch(value):
        _raise("VERIFICATION_VALUE_INVALID", f"{label} is not a SHA-256 revision")
    return value


def _uid(value: Any, label: str) -> int:
    return _integer(value, label, minimum=0, maximum=2**31 - 1)


def _image(value: Any, label: str) -> str:
    text = _nfc_text(value, label, maximum=512)
    if not _IMAGE_RE.fullmatch(text) or "://" in text:
        _raise("VERIFICATION_VALUE_INVALID", f"{label} is not a digest-pinned image")
    return text


def _exact_mapping(value: Any, keys: frozenset[str], label: str) -> dict[str, Any]:
    if type(value) is not dict or frozenset(value) != keys:
        _raise("VERIFICATION_SHAPE_INVALID", f"{label} has an invalid field set")
    return value


def _validate_json_value(value: Any, *, depth: int = 0) -> None:
    if depth > 12:
        _raise("VERIFICATION_JSON_INVALID", "Receipt JSON nesting is too deep")
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        _integer(value, "JSON integer", minimum=-MAX_JSON_INTEGER)
        return
    if type(value) is str:
        _nfc_text(value, "JSON string", maximum=4096)
        return
    if type(value) is list:
        if len(value) > 128:
            _raise("VERIFICATION_JSON_INVALID", "Receipt JSON array is too large")
        for item in value:
            _validate_json_value(item, depth=depth + 1)
        return
    if type(value) is dict:
        if len(value) > 128:
            _raise("VERIFICATION_JSON_INVALID", "Receipt JSON object is too large")
        for key, item in value.items():
            _nfc_text(key, "JSON object key", maximum=128)
            _validate_json_value(item, depth=depth + 1)
        return
    _raise("VERIFICATION_JSON_INVALID", "Receipt JSON contains a forbidden scalar")


def canonical_json_bytes(value: Any) -> bytes:
    """Encode the closed ASCII-key, integer-domain RFC 8785 subset."""

    _validate_json_value(value)
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", "strict")
    except (TypeError, ValueError, UnicodeError):
        _raise("VERIFICATION_JSON_INVALID", "Receipt JSON cannot be encoded canonically")


def _reject_constant(_value: str) -> None:
    _raise("VERIFICATION_JSON_INVALID", "Receipt JSON contains a forbidden number")


def _reject_float(_value: str) -> None:
    _raise("VERIFICATION_JSON_INVALID", "Receipt JSON floats are forbidden")


def _parse_integer(value: str) -> int:
    try:
        parsed = int(value, 10)
    except ValueError:
        _raise("VERIFICATION_JSON_INVALID", "Receipt JSON integer is invalid")
    return _integer(parsed, "JSON integer", minimum=-MAX_JSON_INTEGER)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            _raise("VERIFICATION_JSON_INVALID", "Receipt JSON contains a duplicate key")
        value[key] = item
    return value


def decode_canonical_json(raw: bytes) -> Any:
    """Parse exact canonical JSON, rejecting alternate byte representations."""

    if type(raw) is not bytes or not raw or len(raw) > MAX_DOCUMENT_BYTES:
        _raise("VERIFICATION_DOCUMENT_SIZE_INVALID", "Receipt byte length is invalid")
    try:
        value = json.loads(
            raw.decode("utf-8", "strict"),
            object_pairs_hook=_unique_object,
            parse_int=_parse_integer,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except LauncherVerificationError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError):
        _raise("VERIFICATION_JSON_INVALID", "Receipt is not valid canonical JSON")
    _validate_json_value(value)
    if not hmac.compare_digest(canonical_json_bytes(value), raw):
        _raise("VERIFICATION_JSON_NONCANONICAL", "Receipt is not canonical JSON")
    return value


def _parse_time(value: Any, label: str) -> dt.datetime:
    if type(value) is not str or not _RFC3339_SECONDS_RE.fullmatch(value):
        _raise("VERIFICATION_FRESHNESS_INVALID", f"{label} is not canonical RFC3339 time")
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc
        )
    except ValueError:
        _raise("VERIFICATION_FRESHNESS_INVALID", f"{label} is not a real timestamp")


def _signature_bytes(value: Any) -> bytes:
    if (
        type(value) is not str
        or not _BASE64_SIGNATURE_RE.fullmatch(value)
        or len(value) != 88
    ):
        _raise("VERIFICATION_SIGNATURE_ENCODING_INVALID", "Signature encoding is invalid")
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeError, binascii.Error, ValueError):
        _raise("VERIFICATION_SIGNATURE_ENCODING_INVALID", "Signature encoding is invalid")
    if len(decoded) != 64 or base64.b64encode(decoded).decode("ascii") != value:
        _raise("VERIFICATION_SIGNATURE_ENCODING_INVALID", "Signature encoding is invalid")
    return decoded


def _validate_receipt_shape(value: Any) -> dict[str, Any]:
    receipt = _exact_mapping(value, _RECEIPT_KEYS, "Launcher verification receipt")
    if receipt["schema"] != RECEIPT_SCHEMA:
        _raise("VERIFICATION_SCHEMA_INVALID", "Receipt schema is not allowed")
    _matched(receipt["receipt_id"], "receipt identity", _RECEIPT_ID_RE, 160)

    issuer = _exact_mapping(receipt["issuer"], _ISSUER_KEYS, "Receipt issuer")
    _matched(issuer["issuer_id"], "issuer identity", _SAFE_ID_RE, 160)
    if issuer["role"] != ISSUER_ROLE:
        _raise("VERIFICATION_ISSUER_INVALID", "Receipt issuer role is not allowed")
    _matched(issuer["key_id"], "issuer key identity", _SAFE_ID_RE, 160)
    _sha256_revision(issuer["key_revision"], "issuer key revision")

    trust = _exact_mapping(receipt["trust_bundle"], _TRUST_BUNDLE_KEYS, "Trust bundle")
    if trust["schema"] != TRUST_BUNDLE_SCHEMA:
        _raise("VERIFICATION_TRUST_INVALID", "Trust bundle schema is not allowed")
    _sha256_revision(trust["revision"], "trust bundle revision")
    for key in ("sha256", "issuer_membership_sha256", "issuer_public_key_sha256"):
        _sha256(trust[key], f"trust bundle {key}")

    launcher = _exact_mapping(receipt["launcher"], _LAUNCHER_KEYS, "Launcher binding")
    _matched(launcher["launcher_identity"], "launcher identity", _SAFE_ID_RE, 160)
    _uid(launcher["launcher_uid"], "launcher UID")
    _uid(launcher["coordinator_uid"], "coordinator UID")
    if launcher["launcher_uid"] == launcher["coordinator_uid"]:
        _raise("VERIFICATION_LAUNCHER_INVALID", "Launcher and coordinator UIDs must differ")

    handoff = _exact_mapping(receipt["handoff"], _HANDOFF_KEYS, "Handoff binding")
    if handoff["receipt_schema"] != HANDOFF_RECEIPT_SCHEMA:
        _raise("VERIFICATION_HANDOFF_INVALID", "Handoff receipt schema is not allowed")
    _sha256(handoff["raw_receipt_sha256"], "raw handoff receipt")
    if handoff["opaque_connected_fd_handoff_verified"] is not True:
        _raise("VERIFICATION_HANDOFF_INVALID", "Opaque connected FD handoff was not verified")

    pins = _exact_mapping(receipt["pins"], _PIN_KEYS, "Verification pins")
    for key in _PIN_KEYS - {"worker_image"}:
        _sha256(pins[key], f"verification pin {key}")
    _image(pins["worker_image"], "worker image")

    verification = _exact_mapping(
        receipt["verification"], _VERIFICATION_KEYS, "Verification claims"
    )
    if verification["verified_before_executor_start"] is not True:
        _raise("VERIFICATION_ORDER_INVALID", "Verification did not precede executor start")

    signature = _exact_mapping(receipt["signature"], _SIGNATURE_KEYS, "Receipt signature")
    if signature["algorithm"] != SIGNATURE_ALGORITHM:
        _raise("VERIFICATION_SIGNATURE_INVALID", "Signature algorithm is not allowed")
    if signature["canonicalization"] != SIGNATURE_CANONICALIZATION:
        _raise("VERIFICATION_SIGNATURE_INVALID", "Signature canonicalization is not allowed")
    _sha256(signature["signed_payload_sha256"], "signed payload")
    _sha256(signature["detached_signature_sha256"], "detached signature")
    _signature_bytes(signature["detached_signature_base64"])

    _parse_time(receipt["issued_at"], "receipt issue time")
    _parse_time(receipt["not_before"], "receipt not-before time")
    _parse_time(receipt["expires_at"], "receipt expiry time")
    if receipt["serialization_contract"] != SERIALIZATION_CONTRACT:
        _raise("VERIFICATION_SERIALIZATION_INVALID", "Serialization contract changed")
    return dict(receipt)


@dataclasses.dataclass(frozen=True, slots=True)
class TrustedEd25519Key:
    """One deployment-pinned key record; raw key bytes never enter a receipt."""

    issuer_id: str
    key_id: str
    key_revision: str
    trust_bundle_revision: str
    trust_bundle_sha256: str
    issuer_membership_sha256: str
    public_key_bytes: bytes

    def __post_init__(self) -> None:
        _matched(self.issuer_id, "trusted issuer identity", _SAFE_ID_RE, 160)
        _matched(self.key_id, "trusted key identity", _SAFE_ID_RE, 160)
        _sha256_revision(self.key_revision, "trusted key revision")
        _sha256_revision(self.trust_bundle_revision, "trusted bundle revision")
        _sha256(self.trust_bundle_sha256, "trusted bundle")
        _sha256(self.issuer_membership_sha256, "trusted issuer membership")
        if type(self.public_key_bytes) is not bytes or len(self.public_key_bytes) != 32:
            _raise(
                "VERIFICATION_TRUST_KEY_INVALID",
                "Ed25519 public key must be exactly 32 canonical bytes",
            )

    @property
    def public_key_sha256(self) -> str:
        return hashlib.sha256(self.public_key_bytes).hexdigest()


@dataclasses.dataclass(frozen=True, slots=True)
class ExpectedLauncherVerificationPins:
    """Independent deployment policy; receipt values never authorize themselves."""

    raw_receipt_sha256: str
    receipt_id: str
    issuer_id: str
    issuer_key_id: str
    issuer_key_revision: str
    trust_bundle_revision: str
    trust_bundle_sha256: str
    issuer_membership_sha256: str
    issuer_public_key_sha256: str
    launcher_identity: str
    launcher_uid: int
    coordinator_uid: int
    handoff_receipt_sha256: str
    interpreter_sha256: str
    coordinator_source_closure_sha256: str
    adapter_source_sha256: str
    worker_protocol_sha256: str
    worker_source_closure_sha256: str
    schemas_bundle_sha256: str
    dependency_lock_sha256: str
    sql_bundle_sha256: str
    plan_sha256: str
    worker_image: str
    worker_runtime_attestation_sha256: str

    def __post_init__(self) -> None:
        _sha256(self.raw_receipt_sha256, "expected verification receipt")
        _matched(self.receipt_id, "expected receipt identity", _RECEIPT_ID_RE, 160)
        _matched(self.issuer_id, "expected issuer identity", _SAFE_ID_RE, 160)
        _matched(self.issuer_key_id, "expected issuer key identity", _SAFE_ID_RE, 160)
        _sha256_revision(self.issuer_key_revision, "expected issuer key revision")
        _sha256_revision(self.trust_bundle_revision, "expected trust bundle revision")
        for label, value in (
            ("expected trust bundle", self.trust_bundle_sha256),
            ("expected issuer membership", self.issuer_membership_sha256),
            ("expected issuer public key", self.issuer_public_key_sha256),
            ("expected handoff receipt", self.handoff_receipt_sha256),
            ("expected interpreter", self.interpreter_sha256),
            ("expected coordinator source closure", self.coordinator_source_closure_sha256),
            ("expected adapter source", self.adapter_source_sha256),
            ("expected worker protocol", self.worker_protocol_sha256),
            ("expected worker source closure", self.worker_source_closure_sha256),
            ("expected schemas bundle", self.schemas_bundle_sha256),
            ("expected dependency lock", self.dependency_lock_sha256),
            ("expected SQL bundle", self.sql_bundle_sha256),
            ("expected plan", self.plan_sha256),
            ("expected worker runtime attestation", self.worker_runtime_attestation_sha256),
        ):
            _sha256(value, label)
        _matched(self.launcher_identity, "expected launcher identity", _SAFE_ID_RE, 160)
        _uid(self.launcher_uid, "expected launcher UID")
        _uid(self.coordinator_uid, "expected coordinator UID")
        if self.launcher_uid == self.coordinator_uid:
            _raise(
                "VERIFICATION_EXPECTATION_INVALID",
                "Expected launcher and coordinator UIDs must differ",
            )
        _image(self.worker_image, "expected worker image")


_RESULT_SENTINEL = object()


@dataclasses.dataclass(frozen=True, slots=True, init=False)
class VerifiedLauncherReceipt:
    """Opaque immutable result produced only after real signature verification."""

    raw_receipt_bytes: bytes
    raw_receipt_sha256: str
    signed_payload_bytes: bytes
    signed_payload_sha256: str
    signature_bytes: bytes
    signature_sha256: str
    handoff_receipt_sha256: str
    issuer_key_id: str
    issuer_key_revision: str
    trust_bundle_sha256: str
    launcher_identity: str
    launcher_uid: int
    coordinator_uid: int
    interpreter_sha256: str
    coordinator_source_closure_sha256: str
    adapter_source_sha256: str
    worker_protocol_sha256: str
    worker_source_closure_sha256: str
    schemas_bundle_sha256: str
    dependency_lock_sha256: str
    sql_bundle_sha256: str
    plan_sha256: str
    worker_image: str
    worker_runtime_attestation_sha256: str

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
        handoff_receipt_sha256: str,
        issuer_key_id: str,
        issuer_key_revision: str,
        trust_bundle_sha256: str,
        launcher_identity: str,
        launcher_uid: int,
        coordinator_uid: int,
        interpreter_sha256: str,
        coordinator_source_closure_sha256: str,
        adapter_source_sha256: str,
        worker_protocol_sha256: str,
        worker_source_closure_sha256: str,
        schemas_bundle_sha256: str,
        dependency_lock_sha256: str,
        sql_bundle_sha256: str,
        plan_sha256: str,
        worker_image: str,
        worker_runtime_attestation_sha256: str,
    ) -> None:
        if sentinel is not _RESULT_SENTINEL:
            raise TypeError("VerifiedLauncherReceipt cannot be constructed directly")
        values = locals()
        for field in dataclasses.fields(self):
            object.__setattr__(self, field.name, values[field.name])

    @property
    def receipt(self) -> dict[str, Any]:
        return _validate_receipt_shape(decode_canonical_json(self.raw_receipt_bytes))

    @property
    def signature_verified(self) -> bool:
        return True

    def __reduce__(self) -> Any:
        raise TypeError("VerifiedLauncherReceipt cannot be serialized")


_COMPOSITE_SENTINEL = object()


class VerifiedAttestedWorkerHandoff:
    """Exclusive owner of one FD bound to one verified signed receipt.

    Instances can only be produced by :func:`bind_verified_attested_worker_handoff`.
    The binder consumes the exact ``AttestedWorkerHandoff`` object, compares its
    internally recorded receipt digest to the signed verification result, and
    transfers the worker socket here.  The socket may then be taken exactly once.
    """

    __slots__ = ("_verified", "_worker_socket")

    def __init__(
        self,
        sentinel: object,
        verified: VerifiedLauncherReceipt,
        worker_socket: Any,
    ) -> None:
        if sentinel is not _COMPOSITE_SENTINEL:
            raise TypeError("VerifiedAttestedWorkerHandoff cannot be constructed directly")
        object.__setattr__(self, "_verified", verified)
        object.__setattr__(self, "_worker_socket", worker_socket)

    def __setattr__(self, _name: str, _value: Any) -> None:
        raise AttributeError("VerifiedAttestedWorkerHandoff is immutable")

    @property
    def verified_receipt(self) -> VerifiedLauncherReceipt:
        return self._verified

    @property
    def verification_receipt_sha256(self) -> str:
        return self._verified.raw_receipt_sha256

    @property
    def handoff_receipt_sha256(self) -> str:
        return self._verified.handoff_receipt_sha256

    def state_launcher_verification(self) -> dict[str, Any]:
        verified = self._verified
        return {
            "receipt_schema": RECEIPT_SCHEMA,
            "receipt_sha256": verified.raw_receipt_sha256,
            "launcher_identity": verified.launcher_identity,
            "launcher_uid": verified.launcher_uid,
            "coordinator_uid": verified.coordinator_uid,
            "opaque_handoff_verified": True,
            "source_closure_sha256": verified.coordinator_source_closure_sha256,
            "plan_sha256": verified.plan_sha256,
            "runtime_attestation_sha256": verified.worker_runtime_attestation_sha256,
        }

    def terminal_launcher_verification(self) -> dict[str, Any]:
        verified = self._verified
        return {
            "receipt_schema": RECEIPT_SCHEMA,
            "raw_receipt_sha256": verified.raw_receipt_sha256,
            "signature_verified": True,
            "trust_bundle_sha256": verified.trust_bundle_sha256,
            "interpreter_sha256": verified.interpreter_sha256,
            "coordinator_source_closure_sha256": (
                verified.coordinator_source_closure_sha256
            ),
            "adapter_source_sha256": verified.adapter_source_sha256,
            "worker_protocol_sha256": verified.worker_protocol_sha256,
            "worker_source_closure_sha256": verified.worker_source_closure_sha256,
            "schemas_bundle_sha256": verified.schemas_bundle_sha256,
            "dependency_lock_sha256": verified.dependency_lock_sha256,
            "sql_bundle_sha256": verified.sql_bundle_sha256,
            "plan_sha256": verified.plan_sha256,
            "worker_image": verified.worker_image,
            "worker_runtime_attestation_sha256": (
                verified.worker_runtime_attestation_sha256
            ),
            "opaque_connected_fd_handoff_verified": True,
            "verified_before_executor_start": True,
        }

    def take_worker_socket(self) -> Any:
        worker_socket = self._worker_socket
        if worker_socket is None:
            _raise(
                "VERIFIED_HANDOFF_ALREADY_CONSUMED",
                "Verified worker handoff was already consumed or closed",
            )
        object.__setattr__(self, "_worker_socket", None)
        return worker_socket

    def close(self) -> None:
        worker_socket = self._worker_socket
        if worker_socket is not None:
            object.__setattr__(self, "_worker_socket", None)
            worker_socket.close()

    def __enter__(self) -> VerifiedAttestedWorkerHandoff:
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.close()

    def __reduce__(self) -> Any:
        raise TypeError("VerifiedAttestedWorkerHandoff cannot be serialized")

    def __copy__(self) -> Any:
        raise TypeError("VerifiedAttestedWorkerHandoff cannot be copied")

    def __deepcopy__(self, _memo: Any) -> Any:
        raise TypeError("VerifiedAttestedWorkerHandoff cannot be copied")

    def __del__(self) -> None:
        try:
            self.close()
        except BaseException:
            pass


def bind_verified_attested_worker_handoff(
    verified: VerifiedLauncherReceipt,
    handoff: launcher_handoff.AttestedWorkerHandoff,
) -> VerifiedAttestedWorkerHandoff:
    """Consume and bind two exact opaque results; closes the handoff on failure.

    A caller-provided digest is intentionally not accepted.  The compared values
    come only from the exact signed-verification result and exact launcher handoff.
    """

    if type(handoff) is not launcher_handoff.AttestedWorkerHandoff:
        _raise(
            "VERIFIED_HANDOFF_TYPE_INVALID",
            "Launcher handoff must be the exact attested handoff type",
        )
    try:
        if type(verified) is not VerifiedLauncherReceipt:
            _raise(
                "VERIFIED_RECEIPT_TYPE_INVALID",
                "Launcher verification must be the exact verified receipt type",
            )
        signed_handoff_sha256 = _sha256(
            verified.handoff_receipt_sha256,
            "signed handoff receipt",
        )
        observed_handoff_sha256 = _sha256(
            handoff.receipt_sha256,
            "attested handoff receipt",
        )
        if not hmac.compare_digest(signed_handoff_sha256, observed_handoff_sha256):
            _raise(
                "VERIFIED_HANDOFF_DIGEST_MISMATCH",
                "Signed and attested handoff receipts do not match",
            )
        try:
            worker_socket = handoff.take_worker_socket()
        except launcher_handoff.LauncherHandoffError as error:
            if error.code == "HANDOFF_ALREADY_CONSUMED":
                _raise(
                    "VERIFIED_HANDOFF_ALREADY_CONSUMED",
                    "Attested worker handoff was already consumed",
                )
            _raise(
                "VERIFIED_HANDOFF_INVALID",
                "Attested worker handoff could not transfer ownership",
            )
        try:
            return VerifiedAttestedWorkerHandoff(
                _COMPOSITE_SENTINEL,
                verified,
                worker_socket,
            )
        except BaseException:
            worker_socket.close()
            raise
    finally:
        handoff.close()


def _validate_expected_pins(
    receipt: dict[str, Any],
    expected: ExpectedLauncherVerificationPins,
) -> None:
    issuer = receipt["issuer"]
    trust = receipt["trust_bundle"]
    launcher = receipt["launcher"]
    handoff = receipt["handoff"]
    pins = receipt["pins"]
    comparisons = (
        (receipt["receipt_id"], expected.receipt_id),
        (issuer["issuer_id"], expected.issuer_id),
        (issuer["key_id"], expected.issuer_key_id),
        (issuer["key_revision"], expected.issuer_key_revision),
        (trust["revision"], expected.trust_bundle_revision),
        (trust["sha256"], expected.trust_bundle_sha256),
        (trust["issuer_membership_sha256"], expected.issuer_membership_sha256),
        (trust["issuer_public_key_sha256"], expected.issuer_public_key_sha256),
        (launcher["launcher_identity"], expected.launcher_identity),
        (launcher["launcher_uid"], expected.launcher_uid),
        (launcher["coordinator_uid"], expected.coordinator_uid),
        (handoff["raw_receipt_sha256"], expected.handoff_receipt_sha256),
        (pins["interpreter_sha256"], expected.interpreter_sha256),
        (
            pins["coordinator_source_closure_sha256"],
            expected.coordinator_source_closure_sha256,
        ),
        (pins["adapter_source_sha256"], expected.adapter_source_sha256),
        (pins["worker_protocol_sha256"], expected.worker_protocol_sha256),
        (pins["worker_source_closure_sha256"], expected.worker_source_closure_sha256),
        (pins["schemas_bundle_sha256"], expected.schemas_bundle_sha256),
        (pins["dependency_lock_sha256"], expected.dependency_lock_sha256),
        (pins["sql_bundle_sha256"], expected.sql_bundle_sha256),
        (pins["plan_sha256"], expected.plan_sha256),
        (pins["worker_image"], expected.worker_image),
        (
            pins["worker_runtime_attestation_sha256"],
            expected.worker_runtime_attestation_sha256,
        ),
    )
    if any(observed != required for observed, required in comparisons):
        _raise("VERIFICATION_PIN_MISMATCH", "Receipt does not match deployment-pinned policy")


def _validated_trust_key(
    receipt: dict[str, Any],
    trust_keys: dict[str, TrustedEd25519Key],
) -> TrustedEd25519Key:
    if type(trust_keys) is not dict or not 1 <= len(trust_keys) <= 64:
        _raise("VERIFICATION_TRUST_MAP_INVALID", "Trust key map is invalid")
    copied: dict[str, TrustedEd25519Key] = {}
    for key_id, key in trust_keys.items():
        _matched(key_id, "trust map key identity", _SAFE_ID_RE, 160)
        if type(key) is not TrustedEd25519Key or key.key_id != key_id:
            _raise("VERIFICATION_TRUST_MAP_INVALID", "Trust key map is invalid")
        copied[key_id] = key

    issuer = receipt["issuer"]
    trust = receipt["trust_bundle"]
    trusted = copied.get(issuer["key_id"])
    if trusted is None:
        _raise("VERIFICATION_TRUST_KEY_UNKNOWN", "Receipt issuer key is not trusted")
    comparisons = (
        (trusted.issuer_id, issuer["issuer_id"]),
        (trusted.key_revision, issuer["key_revision"]),
        (trusted.trust_bundle_revision, trust["revision"]),
        (trusted.trust_bundle_sha256, trust["sha256"]),
        (trusted.issuer_membership_sha256, trust["issuer_membership_sha256"]),
        (trusted.public_key_sha256, trust["issuer_public_key_sha256"]),
    )
    if any(left != right for left, right in comparisons):
        _raise("VERIFICATION_TRUST_KEY_MISMATCH", "Trusted issuer key binding changed")
    return trusted


def _validate_freshness(receipt: dict[str, Any], now: dt.datetime) -> None:
    if type(now) is not dt.datetime or now.tzinfo is not dt.timezone.utc:
        _raise("VERIFICATION_FRESHNESS_INVALID", "Explicit current time is not exact UTC")
    issued = _parse_time(receipt["issued_at"], "receipt issue time")
    not_before = _parse_time(receipt["not_before"], "receipt not-before time")
    expires = _parse_time(receipt["expires_at"], "receipt expiry time")
    current = now
    if not issued <= not_before < expires:
        _raise("VERIFICATION_FRESHNESS_INVALID", "Receipt validity interval is invalid")
    if (expires - issued).total_seconds() > MAX_RECEIPT_LIFETIME_SECONDS:
        _raise("VERIFICATION_FRESHNESS_INVALID", "Receipt lifetime exceeds the limit")
    if current < not_before:
        _raise("VERIFICATION_NOT_YET_VALID", "Receipt is not yet valid")
    if current >= expires:
        _raise("VERIFICATION_EXPIRED", "Receipt has expired")


def _verify_ed25519(public_key_bytes: bytes, signature: bytes, payload: bytes) -> None:
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.exceptions import UnsupportedAlgorithm
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except (ImportError, ModuleNotFoundError):
        _raise(
            "VERIFICATION_CRYPTO_UNAVAILABLE",
            "Approved Ed25519 verification dependency is unavailable",
        )
    try:
        public_key = Ed25519PublicKey.from_public_bytes(public_key_bytes)
        public_key.verify(signature, payload)
    except InvalidSignature:
        _raise("VERIFICATION_SIGNATURE_INVALID", "Ed25519 signature verification failed")
    except UnsupportedAlgorithm:
        _raise(
            "VERIFICATION_CRYPTO_UNAVAILABLE",
            "Approved Ed25519 verification dependency is unavailable",
        )
    except (TypeError, ValueError):
        _raise("VERIFICATION_TRUST_KEY_INVALID", "Ed25519 public key is invalid")


def verify_launcher_verification_receipt(
    raw_receipt: bytes,
    *,
    expected: ExpectedLauncherVerificationPins,
    trust_keys: dict[str, TrustedEd25519Key],
    now: dt.datetime,
) -> VerifiedLauncherReceipt:
    """Verify one exact signed receipt and return an opaque immutable result."""

    if type(expected) is not ExpectedLauncherVerificationPins:
        _raise("VERIFICATION_EXPECTATION_INVALID", "Expected receipt policy is invalid")
    if (
        type(raw_receipt) is not bytes
        or not raw_receipt
        or len(raw_receipt) > MAX_DOCUMENT_BYTES
    ):
        _raise("VERIFICATION_DOCUMENT_SIZE_INVALID", "Receipt byte length is invalid")
    raw_receipt_sha256 = hashlib.sha256(raw_receipt).hexdigest()
    if not hmac.compare_digest(raw_receipt_sha256, expected.raw_receipt_sha256):
        _raise("VERIFICATION_RECEIPT_DIGEST_MISMATCH", "Verification receipt digest changed")

    receipt = _validate_receipt_shape(decode_canonical_json(raw_receipt))
    _validate_expected_pins(receipt, expected)
    trusted_key = _validated_trust_key(receipt, trust_keys)
    _validate_freshness(receipt, now)

    unsigned = dict(receipt)
    signature_record = dict(unsigned.pop("signature"))
    signed_payload_bytes = canonical_json_bytes(unsigned)
    signed_payload_sha256 = hashlib.sha256(signed_payload_bytes).hexdigest()
    if not hmac.compare_digest(
        signed_payload_sha256, signature_record["signed_payload_sha256"]
    ):
        _raise(
            "VERIFICATION_SIGNED_PAYLOAD_DIGEST_MISMATCH",
            "Signed payload digest changed",
        )
    signature_bytes = _signature_bytes(signature_record["detached_signature_base64"])
    signature_sha256 = hashlib.sha256(signature_bytes).hexdigest()
    if not hmac.compare_digest(
        signature_sha256, signature_record["detached_signature_sha256"]
    ):
        _raise("VERIFICATION_SIGNATURE_DIGEST_MISMATCH", "Signature digest changed")
    _verify_ed25519(trusted_key.public_key_bytes, signature_bytes, signed_payload_bytes)

    pins = receipt["pins"]
    launcher = receipt["launcher"]
    issuer = receipt["issuer"]
    return VerifiedLauncherReceipt(
        _RESULT_SENTINEL,
        raw_receipt_bytes=raw_receipt,
        raw_receipt_sha256=raw_receipt_sha256,
        signed_payload_bytes=signed_payload_bytes,
        signed_payload_sha256=signed_payload_sha256,
        signature_bytes=signature_bytes,
        signature_sha256=signature_sha256,
        handoff_receipt_sha256=receipt["handoff"]["raw_receipt_sha256"],
        issuer_key_id=issuer["key_id"],
        issuer_key_revision=issuer["key_revision"],
        trust_bundle_sha256=receipt["trust_bundle"]["sha256"],
        launcher_identity=launcher["launcher_identity"],
        launcher_uid=launcher["launcher_uid"],
        coordinator_uid=launcher["coordinator_uid"],
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


__all__ = [
    "ExpectedLauncherVerificationPins",
    "HANDOFF_RECEIPT_SCHEMA",
    "LauncherVerificationError",
    "MAX_DOCUMENT_BYTES",
    "RECEIPT_SCHEMA",
    "SERIALIZATION_CONTRACT",
    "TrustedEd25519Key",
    "VerifiedAttestedWorkerHandoff",
    "VerifiedLauncherReceipt",
    "bind_verified_attested_worker_handoff",
    "canonical_json_bytes",
    "decode_canonical_json",
    "verify_launcher_verification_receipt",
]
