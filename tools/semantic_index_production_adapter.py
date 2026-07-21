"""Unregistered offline control surface for the semantic-index v2 adapter.

This module is deliberately incapable of Production execution.  It builds a
closed canonical contract only from caller-supplied pins, exposes an immutable
empty registry, and fails every phase, control, or live execution entry point
with ``ADAPTER_NOT_REGISTERED``.  It performs no self-hashing, environment or
filesystem discovery, socket activity, process creation, service lifecycle,
or legacy-runner import.

The optional deterministic pair verifier checks only canonical bytes, explicit
digests, the closed request/response schema pair, and the response's request
digest binding.  It is not a schema validator, transport, registration proof,
Production verifier, or execution path.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import unicodedata
from typing import Any, NoReturn


ADAPTER_SCHEMA = "semantic-index-production-adapter/v2"
ADAPTER_ID = "semantic-index-production-v2-uds"
ADAPTER_TYPE = "privilege_separated_preconnected_uds_exact_static_adapter_v2"
ADAPTER_NOT_REGISTERED = "ADAPTER_NOT_REGISTERED"

WORKER_PROTOCOL_REVISION = "simworld-semantic-index-worker/v1"
WORKER_TRANSPORT = "launcher_attested_preconnected_af_unix_framed_canonical_json_v1"
LAUNCHER_HANDOFF_MODE = "externally_attested_opaque_connected_fd_handoff_v1"
LAUNCHER_FD_TRANSPORT = "scm_rights_unix_socket_v1"
LAUNCHER_PEER_CREDENTIAL_VERIFICATION = "so_peercred_uid_gid_pid_v1"
LAUNCHER_PROCESS_START_VERIFICATION = "boot_id_pid_start_token_v1"
LAUNCHER_SOCKET_INODE_VERIFICATION = "device_inode_and_peer_binding_v1"

PHASE_OPERATIONS = (
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

SCHEMA_DIGEST_FIELDS = (
    "reviewed_job_v2",
    "execution_plan_v2",
    "approval_basis_v1",
    "approval_receipt_v1",
    "launcher_verification_receipt_v1",
    "phase_request_v1",
    "worker_result_v1",
    "worker_artifact_root_v1",
    "phase_evidence_v1",
    "control_request_v1",
    "control_result_v1",
    "execution_state_v2",
    "terminal_receipt_v2",
    "inspect_artifact_v1",
    "render_artifact_v1",
    "caption_artifact_v1",
    "embed_artifact_v1",
    "postgres_artifact_v1",
    "qdrant_artifact_v1",
    "reconcile_artifact_v1",
)
SOURCE_CLOSURE_DIGEST_FIELDS = (
    "adapter_source_sha256",
    "adapter_transitive_source_closure_sha256",
    "coordinator_source_closure_sha256",
    "worker_source_closure_sha256",
    "launcher_source_closure_sha256",
    "dependency_lock_sha256",
    "sql_bundle_sha256",
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SHA256_REVISION_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_CANONICAL_BYTES = 2 * 1024 * 1024
_MAX_JSON_DEPTH = 64
_MAX_JSON_CONTAINER_ITEMS = 4096
_MAX_JSON_STRING_BYTES = 1024 * 1024
_MAX_JSON_INTEGER = 2**53 - 1

_SERIALIZATION_CONTRACT = {
    "encoding": "utf-8",
    "canonicalization": "rfc8785-jcs-v1",
    "duplicate_keys": "reject",
    "unicode_normalization": "require_already_nfc",
    "numbers": "integers_only",
    "nonfinite_numbers": "reject",
    "bom": False,
    "trailing_newline": False,
}


@dataclasses.dataclass(frozen=True, slots=True)
class AdapterContractError(Exception):
    """Bounded contract construction or offline verification failure."""

    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


@dataclasses.dataclass(frozen=True, slots=True)
class AdapterControlError(Exception):
    """Fixed public failure emitted by every unavailable execution gate."""

    code: str = dataclasses.field(default=ADAPTER_NOT_REGISTERED, init=False)
    message: str = dataclasses.field(
        default="Semantic-index Production adapter is not registered",
        init=False,
    )
    retryable: bool = dataclasses.field(default=False, init=False)
    production_capable: bool = dataclasses.field(default=False, init=False)
    registered: bool = dataclasses.field(default=False, init=False)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def public_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "production_capable": self.production_capable,
            "registered": self.registered,
        }


def _contract_fail(code: str, message: str) -> NoReturn:
    raise AdapterContractError(code=code, message=message)


def _sha256(value: Any, label: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        _contract_fail(
            "ADAPTER_CONTRACT_DIGEST_INVALID",
            f"{label} must be a lowercase SHA-256 digest",
        )
    return value


def _sha256_revision(value: Any, label: str) -> str:
    if type(value) is not str or _SHA256_REVISION_RE.fullmatch(value) is None:
        _contract_fail(
            "ADAPTER_CONTRACT_REVISION_INVALID",
            f"{label} must be a digest-pinned revision",
        )
    return value


def _exact_pin(value: Any, expected: str, label: str) -> str:
    if type(value) is not str or value != expected:
        _contract_fail(
            "ADAPTER_CONTRACT_PIN_INVALID",
            f"{label} does not match the closed adapter contract",
        )
    return value


@dataclasses.dataclass(frozen=True, slots=True)
class SchemaDigests:
    """Caller-supplied digests for all twenty formal adapter schemas."""

    reviewed_job_v2: str
    execution_plan_v2: str
    approval_basis_v1: str
    approval_receipt_v1: str
    launcher_verification_receipt_v1: str
    phase_request_v1: str
    worker_result_v1: str
    worker_artifact_root_v1: str
    phase_evidence_v1: str
    control_request_v1: str
    control_result_v1: str
    execution_state_v2: str
    terminal_receipt_v2: str
    inspect_artifact_v1: str
    render_artifact_v1: str
    caption_artifact_v1: str
    embed_artifact_v1: str
    postgres_artifact_v1: str
    qdrant_artifact_v1: str
    reconcile_artifact_v1: str

    def __post_init__(self) -> None:
        for name in SCHEMA_DIGEST_FIELDS:
            _sha256(getattr(self, name), f"schema digest {name}")

    def as_dict(self) -> dict[str, str]:
        return {name: getattr(self, name) for name in SCHEMA_DIGEST_FIELDS}


@dataclasses.dataclass(frozen=True, slots=True)
class SourceClosureDigests:
    """Caller-supplied source/dependency closure digests; no path is read."""

    adapter_source_sha256: str
    adapter_transitive_source_closure_sha256: str
    coordinator_source_closure_sha256: str
    worker_source_closure_sha256: str
    launcher_source_closure_sha256: str
    dependency_lock_sha256: str
    sql_bundle_sha256: str

    def __post_init__(self) -> None:
        for name in SOURCE_CLOSURE_DIGEST_FIELDS:
            _sha256(getattr(self, name), f"source closure digest {name}")

    def as_dict(self) -> dict[str, str | bool]:
        return {
            **{name: getattr(self, name) for name in SOURCE_CLOSURE_DIGEST_FIELDS},
            "legacy_source_in_closure": False,
        }


@dataclasses.dataclass(frozen=True, slots=True)
class ProtocolPins:
    """Caller-supplied exact worker protocol identity and source pins."""

    revision: str
    protocol_source_sha256: str
    wire_schema_bundle_sha256: str
    transport: str

    def __post_init__(self) -> None:
        _exact_pin(self.revision, WORKER_PROTOCOL_REVISION, "worker protocol revision")
        _sha256(self.protocol_source_sha256, "worker protocol source")
        _sha256(self.wire_schema_bundle_sha256, "wire schema bundle")
        _exact_pin(self.transport, WORKER_TRANSPORT, "worker protocol transport")


@dataclasses.dataclass(frozen=True, slots=True)
class LauncherPins:
    """Caller-supplied exact launcher handoff and kernel-attestation pins."""

    mode: str
    fd_transport: str
    peer_credential_verification: str
    peer_process_start_verification: str
    socket_inode_verification: str

    def __post_init__(self) -> None:
        _exact_pin(self.mode, LAUNCHER_HANDOFF_MODE, "launcher handoff mode")
        _exact_pin(
            self.fd_transport,
            LAUNCHER_FD_TRANSPORT,
            "launcher descriptor transport",
        )
        _exact_pin(
            self.peer_credential_verification,
            LAUNCHER_PEER_CREDENTIAL_VERIFICATION,
            "launcher peer credential verification",
        )
        _exact_pin(
            self.peer_process_start_verification,
            LAUNCHER_PROCESS_START_VERIFICATION,
            "launcher process start verification",
        )
        _exact_pin(
            self.socket_inode_verification,
            LAUNCHER_SOCKET_INODE_VERIFICATION,
            "launcher socket inode verification",
        )


@dataclasses.dataclass(frozen=True, slots=True)
class AdapterContractInputs:
    """Complete explicit input set for one offline adapter contract."""

    implementation_revision: str
    schema_digests: SchemaDigests
    source_closure: SourceClosureDigests
    protocol: ProtocolPins
    launcher: LauncherPins

    def __post_init__(self) -> None:
        _sha256_revision(
            self.implementation_revision, "adapter implementation revision"
        )
        expected_types = (
            (self.schema_digests, SchemaDigests, "schema digests"),
            (self.source_closure, SourceClosureDigests, "source closure"),
            (self.protocol, ProtocolPins, "protocol pins"),
            (self.launcher, LauncherPins, "launcher pins"),
        )
        for value, expected, label in expected_types:
            if type(value) is not expected:
                _contract_fail(
                    "ADAPTER_CONTRACT_INPUT_INVALID",
                    f"{label} must use the closed dataclass",
                )


def _validate_json_value(value: Any, *, depth: int = 0) -> None:
    if depth > _MAX_JSON_DEPTH:
        _contract_fail("ADAPTER_CANONICAL_JSON_INVALID", "JSON nesting is too deep")
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        if not -_MAX_JSON_INTEGER <= value <= _MAX_JSON_INTEGER:
            _contract_fail(
                "ADAPTER_CANONICAL_JSON_INVALID",
                "JSON integer is outside the closed domain",
            )
        return
    if type(value) is str:
        try:
            encoded = value.encode("utf-8", "strict")
        except UnicodeError:
            _contract_fail("ADAPTER_CANONICAL_JSON_INVALID", "JSON text is not UTF-8")
        if (
            len(encoded) > _MAX_JSON_STRING_BYTES
            or unicodedata.normalize("NFC", value) != value
            or any(
                ord(character) < 0x20 or ord(character) == 0x7F for character in value
            )
        ):
            _contract_fail(
                "ADAPTER_CANONICAL_JSON_INVALID",
                "JSON text is outside the canonical domain",
            )
        return
    if type(value) is list:
        if len(value) > _MAX_JSON_CONTAINER_ITEMS:
            _contract_fail("ADAPTER_CANONICAL_JSON_INVALID", "JSON array is too large")
        for item in value:
            _validate_json_value(item, depth=depth + 1)
        return
    if type(value) is dict:
        if len(value) > _MAX_JSON_CONTAINER_ITEMS:
            _contract_fail("ADAPTER_CANONICAL_JSON_INVALID", "JSON object is too large")
        for key, item in value.items():
            if type(key) is not str:
                _contract_fail(
                    "ADAPTER_CANONICAL_JSON_INVALID",
                    "JSON object key is not text",
                )
            _validate_json_value(key, depth=depth + 1)
            _validate_json_value(item, depth=depth + 1)
        return
    _contract_fail(
        "ADAPTER_CANONICAL_JSON_INVALID",
        "JSON contains an unsupported scalar",
    )


def canonical_json_bytes(value: Any) -> bytes:
    """Encode the integer-only JCS subset used by the closed contract."""

    _validate_json_value(value)
    try:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", "strict")
    except (TypeError, ValueError, UnicodeError):
        _contract_fail("ADAPTER_CANONICAL_JSON_INVALID", "JSON encoding failed")
    if not raw or len(raw) > _MAX_CANONICAL_BYTES:
        _contract_fail(
            "ADAPTER_CANONICAL_JSON_INVALID",
            "Canonical JSON byte length is invalid",
        )
    return raw


def _reject_float(_value: str) -> NoReturn:
    _contract_fail("ADAPTER_CANONICAL_JSON_INVALID", "JSON floats are forbidden")


def _reject_constant(_value: str) -> NoReturn:
    _contract_fail(
        "ADAPTER_CANONICAL_JSON_INVALID", "JSON nonfinite values are forbidden"
    )


def _parse_integer(value: str) -> int:
    try:
        parsed = int(value, 10)
    except ValueError:
        _contract_fail("ADAPTER_CANONICAL_JSON_INVALID", "JSON integer is invalid")
    if not -_MAX_JSON_INTEGER <= parsed <= _MAX_JSON_INTEGER:
        _contract_fail(
            "ADAPTER_CANONICAL_JSON_INVALID",
            "JSON integer is outside the closed domain",
        )
    return parsed


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _contract_fail(
                "ADAPTER_CANONICAL_JSON_INVALID",
                "JSON contains a duplicate object key",
            )
        result[key] = value
    return result


def decode_canonical_json(raw: bytes) -> Any:
    if type(raw) is not bytes or not raw or len(raw) > _MAX_CANONICAL_BYTES:
        _contract_fail(
            "ADAPTER_CANONICAL_JSON_INVALID",
            "Canonical JSON byte length is invalid",
        )
    try:
        value = json.loads(
            raw.decode("utf-8", "strict"),
            object_pairs_hook=_unique_object,
            parse_int=_parse_integer,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except AdapterContractError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        _contract_fail("ADAPTER_CANONICAL_JSON_INVALID", "JSON decoding failed")
    _validate_json_value(value)
    if canonical_json_bytes(value) != raw:
        _contract_fail(
            "ADAPTER_CANONICAL_JSON_INVALID",
            "JSON bytes are not canonical",
        )
    return value


@dataclasses.dataclass(frozen=True, slots=True)
class SealedAdapterContract:
    """Canonical offline contract bytes and their deterministic digest."""

    canonical_bytes: bytes
    sha256: str

    def __post_init__(self) -> None:
        instance = decode_canonical_json(self.canonical_bytes)
        _sha256(self.sha256, "sealed adapter contract")
        if hashlib.sha256(self.canonical_bytes).hexdigest() != self.sha256:
            _contract_fail(
                "ADAPTER_CONTRACT_DIGEST_MISMATCH",
                "Adapter contract digest does not match canonical bytes",
            )
        if type(instance) is not dict or instance.get("schema") != ADAPTER_SCHEMA:
            _contract_fail(
                "ADAPTER_CONTRACT_INPUT_INVALID",
                "Sealed document is not an adapter contract",
            )

    @property
    def instance(self) -> dict[str, Any]:
        value = decode_canonical_json(self.canonical_bytes)
        if type(value) is not dict:
            _contract_fail(
                "ADAPTER_CONTRACT_INPUT_INVALID",
                "Sealed document is not an object",
            )
        return value


def build_offline_adapter_contract(
    inputs: AdapterContractInputs,
) -> SealedAdapterContract:
    """Build the schema-v2 offline contract without consulting ambient state."""

    if type(inputs) is not AdapterContractInputs:
        _contract_fail(
            "ADAPTER_CONTRACT_INPUT_INVALID",
            "Adapter inputs must use the closed dataclass",
        )
    protocol = inputs.protocol
    launcher = inputs.launcher
    instance = {
        "schema": ADAPTER_SCHEMA,
        "adapter_id": ADAPTER_ID,
        "adapter_type": ADAPTER_TYPE,
        "contract_revision": ADAPTER_SCHEMA,
        "implementation_revision": inputs.implementation_revision,
        "production_capable": False,
        "registration": {
            "mode": "not_registered_offline_contract_only",
            "registered": False,
            "explicit_release_registration_required": True,
            "dynamic_registration_allowed": False,
            "monkeypatch_registration_allowed": False,
            "copied_instance_allowed": False,
        },
        "source_closure": inputs.source_closure.as_dict(),
        "protocol_contract": {
            "revision": protocol.revision,
            "protocol_source_sha256": protocol.protocol_source_sha256,
            "wire_schema_bundle_sha256": protocol.wire_schema_bundle_sha256,
            "transport": protocol.transport,
            "request_count_per_fd": 1,
            "response_count_per_fd": 1,
            "client_write_half_close_required": True,
            "worker_eof_before_execute_required": True,
            "worker_write_half_close_required": True,
            "client_eof_before_accept_required": True,
            "trailing_bytes_rejected": True,
            "fd_reuse_allowed": False,
        },
        "schema_digests": inputs.schema_digests.as_dict(),
        "launcher_handoff": {
            "mode": launcher.mode,
            "launcher_attestation_receipt_required": True,
            "launcher_uid_distinct_from_coordinator_required": True,
            "connected_fd_only": True,
            "fd_transport": launcher.fd_transport,
            "opaque_to_executor": True,
            "path_reconnect_allowed": False,
            "socket_path_discovery_allowed": False,
            "peer_credential_verification": launcher.peer_credential_verification,
            "peer_process_start_verification": (
                launcher.peer_process_start_verification
            ),
            "socket_inode_verification": launcher.socket_inode_verification,
            "ownership_transferred": True,
            "close_on_every_exit": True,
        },
        "operations": {
            "phase": list(PHASE_OPERATIONS),
            "control": list(CONTROL_OPERATIONS),
            "free_form_operation_allowed": False,
        },
        "ambient_discovery": {
            "environment": False,
            "filesystem": False,
            "network": False,
            "service_registry": False,
            "plugin": False,
            "endpoint": False,
            "workspace": False,
            "model": False,
            "collection": False,
            "runtime_image": False,
            "command": False,
        },
        "commands": {
            "shell": False,
            "subprocess": False,
            "python_eval": False,
            "ue_python": False,
            "sql_ddl": False,
            "compose": False,
            "legacy_runner": False,
            "caller_supplied_path": False,
            "redirect": False,
        },
        "lifecycle": {
            "starts_unreal": False,
            "starts_database": False,
            "starts_qdrant": False,
            "starts_embedding": False,
            "starts_caption_provider": False,
            "starts_container": False,
            "starts_coturn": False,
            "starts_cirrus": False,
            "opens_public_listener": False,
            "activates_generation": False,
            "rolls_back_generation": False,
            "destructive_cleanup": False,
        },
        "credential_transport": {
            "mode": "sealed_memfd_scm_rights_v1",
            "json_contains_secret_bytes": False,
            "argv_contains_secret_bytes": False,
            "environment_contains_secret_bytes": False,
            "path_contains_secret_bytes": False,
            "per_request_exact_scope": True,
            "read_once": True,
            "close_after_request": True,
            "declared_secret_scan_policy": (
                "declared-secret-bytes-percent1-8-base64-base64url-sha256-"
                "hex-capability-hex-v1"
            ),
            "undeclared_transform_coverage_claimed": False,
        },
        "release_gate": {
            "contract_state": "offline_unregistered",
            "offline_pa01_pa21_required": True,
            "independent_security_review_required": True,
            "explicit_registration_change_required": True,
            "live_approval_implied": False,
            "activation_approval_implied": False,
        },
        "serialization_contract": dict(_SERIALIZATION_CONTRACT),
    }
    raw = canonical_json_bytes(instance)
    return SealedAdapterContract(
        canonical_bytes=raw,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


@dataclasses.dataclass(frozen=True, slots=True)
class ExplicitSealedJson:
    """Caller-supplied canonical JSON bytes with an independently supplied pin."""

    canonical_bytes: bytes
    expected_sha256: str

    def __post_init__(self) -> None:
        value = decode_canonical_json(self.canonical_bytes)
        _sha256(self.expected_sha256, "explicit sealed JSON")
        if hashlib.sha256(self.canonical_bytes).hexdigest() != self.expected_sha256:
            _contract_fail(
                "ADAPTER_OFFLINE_PAIR_DIGEST_MISMATCH",
                "Explicit sealed JSON digest does not match its bytes",
            )
        if type(value) is not dict:
            _contract_fail(
                "ADAPTER_OFFLINE_PAIR_INVALID",
                "Explicit sealed JSON must be an object",
            )

    @property
    def value(self) -> dict[str, Any]:
        value = decode_canonical_json(self.canonical_bytes)
        if type(value) is not dict:
            _contract_fail(
                "ADAPTER_OFFLINE_PAIR_INVALID",
                "Explicit sealed JSON must be an object",
            )
        return value


@dataclasses.dataclass(frozen=True, slots=True)
class OfflinePairVerification:
    """Digest-binding-only result that cannot claim execution or Production."""

    request_sha256: str
    response_sha256: str
    mode: str = dataclasses.field(
        default="deterministic_offline_explicit_pair_digest_binding_only_v1",
        init=False,
    )
    digest_binding_verified: bool = dataclasses.field(default=True, init=False)
    schema_pair_verified: bool = dataclasses.field(default=True, init=False)
    schema_validation_performed: bool = dataclasses.field(default=False, init=False)
    execution_performed: bool = dataclasses.field(default=False, init=False)
    network_used: bool = dataclasses.field(default=False, init=False)
    production_verified: bool = dataclasses.field(default=False, init=False)

    def __post_init__(self) -> None:
        _sha256(self.request_sha256, "offline request")
        _sha256(self.response_sha256, "offline response")


_REQUEST_RESPONSE_SCHEMA_PAIRS = {
    "simworld-semantic-index-phase-request/v1": (
        "simworld-semantic-index-worker-result/v1"
    ),
    "simworld-semantic-index-control-request/v1": (
        "simworld-semantic-index-control-result/v1"
    ),
}


def verify_explicit_sealed_pair(
    request: ExplicitSealedJson,
    response: ExplicitSealedJson,
) -> OfflinePairVerification:
    """Verify one explicit pair offline; perform no transport or execution."""

    if (
        type(request) is not ExplicitSealedJson
        or type(response) is not ExplicitSealedJson
    ):
        _contract_fail(
            "ADAPTER_OFFLINE_PAIR_INVALID",
            "Offline verification requires two explicit sealed documents",
        )
    request_value = request.value
    response_value = response.value
    expected_response_schema = _REQUEST_RESPONSE_SCHEMA_PAIRS.get(
        request_value.get("schema")
    )
    if (
        expected_response_schema is None
        or response_value.get("schema") != expected_response_schema
    ):
        _contract_fail(
            "ADAPTER_OFFLINE_PAIR_INVALID",
            "Offline request and response schemas do not form a closed pair",
        )
    binding = response_value.get("request_binding")
    if (
        type(binding) is not dict
        or binding.get("request_sha256") != request.expected_sha256
    ):
        _contract_fail(
            "ADAPTER_OFFLINE_PAIR_BINDING_MISMATCH",
            "Offline response does not bind the explicit request digest",
        )
    return OfflinePairVerification(
        request_sha256=request.expected_sha256,
        response_sha256=response.expected_sha256,
    )


_STATIC_REGISTERED_ADAPTERS: tuple[()] = ()


def registered_adapters() -> tuple[()]:
    """Return the immutable static registry; this release intentionally has none."""

    return _STATIC_REGISTERED_ADAPTERS


def lookup_registered_adapter(_adapter_id: object) -> None:
    """Return no adapter; dynamic lookup or registration is not supported."""

    return None


def _raise_not_registered() -> NoReturn:
    raise AdapterControlError()


@dataclasses.dataclass(frozen=True, slots=True)
class UnregisteredOfflineAdapterControl:
    """Offline-only holder with no path that can transition to registered."""

    contract: SealedAdapterContract

    def __post_init__(self) -> None:
        if type(self.contract) is not SealedAdapterContract:
            _contract_fail(
                "ADAPTER_CONTRACT_INPUT_INVALID",
                "Offline control requires a sealed adapter contract",
            )
        instance = self.contract.instance
        registration = instance.get("registration")
        if (
            instance.get("production_capable") is not False
            or type(registration) is not dict
            or registration.get("registered") is not False
            or registration.get("mode") != "not_registered_offline_contract_only"
        ):
            _contract_fail(
                "ADAPTER_CONTRACT_REGISTRATION_INVALID",
                "Offline control cannot hold a registered or Production-capable contract",
            )

    def registry_snapshot(self) -> tuple[()]:
        return registered_adapters()

    def lookup(self, adapter_id: object = ADAPTER_ID) -> None:
        return lookup_registered_adapter(adapter_id)

    def verify_pair(
        self,
        request: ExplicitSealedJson,
        response: ExplicitSealedJson,
    ) -> OfflinePairVerification:
        return verify_explicit_sealed_pair(request, response)

    def execute_phase(self, _operation: object, _request: object) -> NoReturn:
        _raise_not_registered()

    def execute_control(self, _operation: object, _request: object) -> NoReturn:
        _raise_not_registered()

    def execute_live(self) -> NoReturn:
        _raise_not_registered()


__all__ = [
    "ADAPTER_ID",
    "ADAPTER_NOT_REGISTERED",
    "ADAPTER_SCHEMA",
    "ADAPTER_TYPE",
    "AdapterContractError",
    "AdapterContractInputs",
    "AdapterControlError",
    "CONTROL_OPERATIONS",
    "ExplicitSealedJson",
    "LAUNCHER_FD_TRANSPORT",
    "LAUNCHER_HANDOFF_MODE",
    "LAUNCHER_PEER_CREDENTIAL_VERIFICATION",
    "LAUNCHER_PROCESS_START_VERIFICATION",
    "LAUNCHER_SOCKET_INODE_VERIFICATION",
    "LauncherPins",
    "OfflinePairVerification",
    "PHASE_OPERATIONS",
    "ProtocolPins",
    "SCHEMA_DIGEST_FIELDS",
    "SOURCE_CLOSURE_DIGEST_FIELDS",
    "SchemaDigests",
    "SealedAdapterContract",
    "SourceClosureDigests",
    "UnregisteredOfflineAdapterControl",
    "WORKER_PROTOCOL_REVISION",
    "WORKER_TRANSPORT",
    "build_offline_adapter_contract",
    "canonical_json_bytes",
    "decode_canonical_json",
    "lookup_registered_adapter",
    "registered_adapters",
    "verify_explicit_sealed_pair",
]
