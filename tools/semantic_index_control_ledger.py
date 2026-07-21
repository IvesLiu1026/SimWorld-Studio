"""Pathless durable ledger for fixed semantic-index recovery controls.

This module is the storage core for a future control service.  It accepts only
canonical ``simworld-semantic-index-control-request/v1`` bytes, binds every
entry to caller-supplied release/job/generation pins (including the approved
generation nonce digest), and publishes immutable prepare/result records below
an already-open private directory file descriptor.

The ledger deliberately does not execute a control operation, receive a
capability, or validate where the independent pins came from.  Future service
wiring must authenticate the one-shot worker request and capability before it
calls this core.  Ordinary phase requests are never accepted here.  A fresh
control request must still be live when ``begin`` returns ``execute``; an
expired request can only replay an already durable exact result or surface an
existing prepare as ``recovery_required``.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import errno
import fcntl
import hashlib
import os
import re
import stat
import threading
import time
from typing import Any, Literal, Mapping

import semantic_index_worker_protocol as worker_protocol


PREPARE_SCHEMA = "simworld-semantic-index-control-ledger-prepare/v1"
RESULT_SCHEMA = "simworld-semantic-index-control-ledger-result/v1"
BINDING_SCHEMA = "simworld-semantic-index-control-ledger-binding/v1"

MAX_RESULT_BYTES = worker_protocol.MAX_FRAME_BYTES
MAX_LEDGER_RECORD_BYTES = worker_protocol.MAX_FRAME_BYTES + 64 * 1024
PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
MAX_LINK_SETTLE_ATTEMPTS = 100
LINK_SETTLE_SECONDS = 0.001

_SHA256_RE = re.compile(r"\A[a-f0-9]{64}\Z")
_SHA256_REVISION_RE = re.compile(r"\Asha256:[a-f0-9]{64}\Z")
_SAFE_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,159}\Z")
_GENERATION_ID_RE = re.compile(r"\Asemantic-generation:[a-f0-9]{64}\Z")
_RUN_ID_RE = re.compile(r"\Asemantic-index-run:[A-Za-z0-9][A-Za-z0-9._:@+\-]{3,140}\Z")
_CORRELATION_ID_RE = re.compile(
    r"\Asemantic-index-correlation:[A-Za-z0-9][A-Za-z0-9._:@+\-]*\Z"
)
_RFC3339_RE = re.compile(
    r"\A[0-9]{4}-(0[1-9]|1[0-2])-([0-2][0-9]|3[01])T"
    r"([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
    r"(\.[0-9]{1,9})?(Z|[+-]([01][0-9]|2[0-3]):[0-5][0-9])\Z"
)

_PHASES = frozenset(
    {"inspect", "render", "caption", "embed", "postgres", "qdrant", "reconcile"}
)
_MUTATION_STATES = frozenset({"none", "staged", "committed", "ambiguous"})
_CONTROL_RESULT_KEYS = frozenset(
    {
        "schema",
        "protocol",
        "request_binding",
        "status",
        "mutation_state",
        "receipt",
        "error",
    }
)
_CONTROL_REQUEST_BINDING_KEYS = frozenset(
    {
        "request_sha256",
        "request_id",
        "operation",
        "target_phase",
        "phase_request_sha256",
        "phase_idempotency_key",
        "control_idempotency_key",
        "idempotency_ledger_identity",
        "idempotency_ledger_revision",
        "job_revision",
        "reviewed_job_sha256",
        "approval_basis_sha256",
        "execution_plan_sha256",
        "launcher_handoff_receipt_sha256",
        "generation_id",
        "generation_binding_sha256",
        "host_boot_id_sha256",
        "deadline_monotonic_ns",
    }
)
_CONTROL_RECEIPT_KEYS = frozenset(
    {
        "schema",
        "operation",
        "generation_id",
        "target_phase",
        "phase_request_sha256",
        "phase_idempotency_key",
        "control_idempotency_key",
        "observed_phase_state",
        "immutable_phase_receipt_sha256",
        "cancelled",
        "quarantined",
        "issued_at",
        "expires_at",
    }
)
_REMOTE_ERROR_KEYS = frozenset({"dependency", "code", "retryable", "mutation_state"})
_REMOTE_ERROR_PAIRS = frozenset(
    {
        ("worker", "WORKER_BUSY"),
        ("worker", "WORKER_INTERNAL"),
        ("worker", "WORKER_LEDGER_CONFLICT"),
        ("worker", "WORKER_CANCELLED"),
        ("evidence_store", "EVIDENCE_STORE_UNAVAILABLE"),
        ("evidence_store", "EVIDENCE_PUBLICATION_FAILED"),
    }
)
_PREPARE_KEYS = frozenset(
    {
        "schema",
        "ledger_identity",
        "ledger_revision",
        "ledger_key",
        "binding",
        "request_sha256",
    }
)
_RESULT_KEYS = frozenset(
    {
        "schema",
        "ledger_identity",
        "ledger_revision",
        "ledger_key",
        "binding",
        "request_sha256",
        "result_sha256",
        "validated_at",
        "result",
    }
)

PUBLIC_ERROR_MESSAGES = {
    "CONTROL_LEDGER_CLOSED": "Durable control ledger is closed",
    "CONTROL_LEDGER_INPUT_INVALID": "Durable control ledger input is invalid",
    "CONTROL_LEDGER_CONTEXT_MISMATCH": "Control request does not match the sealed ledger context",
    "CONTROL_LEDGER_REQUEST_EXPIRED": "Control request deadline has expired",
    "CONTROL_LEDGER_DEADLINE_INVALID": "Control request deadline exceeds the allowed horizon",
    "CONTROL_LEDGER_RESULT_INVALID": "Durable control result is invalid",
    "CONTROL_LEDGER_DIRECTORY_UNSAFE": "Durable control ledger directory is unsafe",
    "CONTROL_LEDGER_ENTRY_UNSAFE": "Durable control ledger entry is unsafe",
    "CONTROL_LEDGER_ENTRY_CORRUPT": "Durable control ledger entry is corrupt",
    "CONTROL_LEDGER_KEY_CONFLICT": "Durable control idempotency key conflict",
    "CONTROL_LEDGER_RESULT_CONFLICT": "Durable control result conflict",
    "CONTROL_LEDGER_PREPARE_REQUIRED": "Durable control prepare record is required",
    "CONTROL_LEDGER_IO_ERROR": "Durable control ledger I/O failed",
    "CONTROL_LEDGER_UNSUPPORTED": "Durable control ledger platform support is unavailable",
}


@dataclasses.dataclass(frozen=True, slots=True)
class ControlLedgerError(Exception):
    """Fixed public error that never retains peer-controlled values."""

    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


@dataclasses.dataclass(frozen=True, slots=True)
class ControlLedgerPins:
    """Independent immutable plan pins supplied by future service wiring."""

    ledger_identity: str
    ledger_revision: str
    job_revision: str
    reviewed_job_sha256: str
    approval_basis_sha256: str
    execution_plan_sha256: str
    generation_id: str
    generation_nonce_sha256: str
    generation_binding_sha256: str
    run_id: str
    correlation_id: str
    owner_identity: str
    lease_id: str
    slot_id: str
    worker_deployment_identity: str


@dataclasses.dataclass(frozen=True, slots=True)
class ControlLedgerBinding:
    """Closed control idempotency tuple persisted below one ledger key."""

    pins: ControlLedgerPins
    operation: str
    operation_revision: str
    target_phase: str
    phase_request_sha256: str
    phase_idempotency_key: str
    control_idempotency_key: str
    credential_scope: str
    credential_generation: str
    target_identity: str


@dataclasses.dataclass(frozen=True, slots=True)
class ControlBeginOutcome:
    """One immutable disposition from ``DurableControlLedger.begin``."""

    action: Literal["execute", "replay", "recovery_required"]
    ledger_key: str
    request_sha256: str
    result_bytes: bytes | None = None

    @property
    def status(self) -> str:
        return self.action


def _fail(code: str) -> None:
    raise ControlLedgerError(code=code, message=PUBLIC_ERROR_MESSAGES[code])


def _sha256(value: Any, *, code: str = "CONTROL_LEDGER_INPUT_INVALID") -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        _fail(code)
    return value


def _sha256_revision(value: Any, *, code: str = "CONTROL_LEDGER_INPUT_INVALID") -> str:
    if type(value) is not str or _SHA256_REVISION_RE.fullmatch(value) is None:
        _fail(code)
    return value


def _matched_text(
    value: Any,
    pattern: re.Pattern[str],
    *,
    code: str = "CONTROL_LEDGER_INPUT_INVALID",
) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        _fail(code)
    return value


def _safe_id(value: Any, *, code: str = "CONTROL_LEDGER_INPUT_INVALID") -> str:
    return _matched_text(value, _SAFE_ID_RE, code=code)


def _canonical_bytes(value: Any, *, code: str) -> bytes:
    try:
        raw = worker_protocol.canonical_json_bytes(value)
    except worker_protocol.WorkerProtocolError:
        _fail(code)
    if type(raw) is not bytes or not raw or len(raw) > MAX_LEDGER_RECORD_BYTES:
        _fail(code)
    return raw


def _decode(raw: bytes, *, maximum: int, code: str) -> Any:
    if type(raw) is not bytes or not raw or len(raw) > maximum:
        _fail(code)
    try:
        return worker_protocol.decode_canonical_json(raw, maximum=maximum)
    except worker_protocol.WorkerProtocolError:
        _fail(code)


def _pins_value(pins: ControlLedgerPins) -> dict[str, str]:
    if type(pins) is not ControlLedgerPins:
        _fail("CONTROL_LEDGER_INPUT_INVALID")
    _safe_id(pins.ledger_identity)
    _sha256_revision(pins.ledger_revision)
    _sha256_revision(pins.job_revision)
    _sha256(pins.reviewed_job_sha256)
    _sha256(pins.approval_basis_sha256)
    _sha256(pins.execution_plan_sha256)
    _matched_text(pins.generation_id, _GENERATION_ID_RE)
    _sha256(pins.generation_nonce_sha256)
    _sha256(pins.generation_binding_sha256)
    _matched_text(pins.run_id, _RUN_ID_RE)
    _matched_text(pins.correlation_id, _CORRELATION_ID_RE)
    for identity in (
        pins.owner_identity,
        pins.lease_id,
        pins.slot_id,
        pins.worker_deployment_identity,
    ):
        _safe_id(identity)
    return {
        "ledger_identity": pins.ledger_identity,
        "ledger_revision": pins.ledger_revision,
        "job_revision": pins.job_revision,
        "reviewed_job_sha256": pins.reviewed_job_sha256,
        "approval_basis_sha256": pins.approval_basis_sha256,
        "execution_plan_sha256": pins.execution_plan_sha256,
        "generation_id": pins.generation_id,
        "generation_nonce_sha256": pins.generation_nonce_sha256,
        "generation_binding_sha256": pins.generation_binding_sha256,
        "run_id": pins.run_id,
        "correlation_id": pins.correlation_id,
        "owner_identity": pins.owner_identity,
        "lease_id": pins.lease_id,
        "slot_id": pins.slot_id,
        "worker_deployment_identity": pins.worker_deployment_identity,
    }


def _binding_value(binding: ControlLedgerBinding) -> dict[str, Any]:
    if type(binding) is not ControlLedgerBinding:
        _fail("CONTROL_LEDGER_INPUT_INVALID")
    pins = _pins_value(binding.pins)
    try:
        contract = worker_protocol.control_operation_contract(binding.operation)
    except worker_protocol.WorkerProtocolError:
        _fail("CONTROL_LEDGER_INPUT_INVALID")
    if binding.operation_revision != contract["revision"]:
        _fail("CONTROL_LEDGER_INPUT_INVALID")
    if binding.credential_scope != contract["scope"]:
        _fail("CONTROL_LEDGER_INPUT_INVALID")
    if binding.target_phase not in _PHASES:
        _fail("CONTROL_LEDGER_INPUT_INVALID")
    _sha256(binding.phase_request_sha256)
    _sha256_revision(binding.phase_idempotency_key)
    _sha256_revision(binding.control_idempotency_key)
    _sha256_revision(binding.credential_generation)
    _safe_id(binding.target_identity)
    if binding.target_identity != binding.pins.worker_deployment_identity:
        _fail("CONTROL_LEDGER_INPUT_INVALID")
    return {
        "schema": BINDING_SCHEMA,
        "pins": pins,
        "operation": binding.operation,
        "operation_revision": binding.operation_revision,
        "target_phase": binding.target_phase,
        "phase_request_sha256": binding.phase_request_sha256,
        "phase_idempotency_key": binding.phase_idempotency_key,
        "control_idempotency_key": binding.control_idempotency_key,
        "credential_scope": binding.credential_scope,
        "credential_generation": binding.credential_generation,
        "target_identity": binding.target_identity,
    }


def derive_control_ledger_key(binding: ControlLedgerBinding) -> str:
    """Derive one storage key from the complete sealed control tuple."""

    material = _canonical_bytes(
        _binding_value(binding), code="CONTROL_LEDGER_INPUT_INVALID"
    )
    return "sha256:" + hashlib.sha256(material).hexdigest()


def _validate_request_pins(request: Mapping[str, Any], pins: ControlLedgerPins) -> None:
    expected_scalars = {
        "idempotency_ledger_identity": pins.ledger_identity,
        "idempotency_ledger_revision": pins.ledger_revision,
        "job_revision": pins.job_revision,
        "reviewed_job_sha256": pins.reviewed_job_sha256,
        "approval_basis_sha256": pins.approval_basis_sha256,
        "execution_plan_sha256": pins.execution_plan_sha256,
        "generation_id": pins.generation_id,
        "generation_binding_sha256": pins.generation_binding_sha256,
    }
    if any(request[key] != value for key, value in expected_scalars.items()):
        _fail("CONTROL_LEDGER_CONTEXT_MISMATCH")
    execution = request["execution_binding"]
    expected_execution = {
        "run_id": pins.run_id,
        "correlation_id": pins.correlation_id,
        "owner_identity": pins.owner_identity,
        "lease_id": pins.lease_id,
        "slot_id": pins.slot_id,
    }
    if any(execution[key] != value for key, value in expected_execution.items()):
        _fail("CONTROL_LEDGER_CONTEXT_MISMATCH")
    if (
        request["worker_binding"]["deployment_identity"]
        != pins.worker_deployment_identity
    ):
        _fail("CONTROL_LEDGER_CONTEXT_MISMATCH")


def _request_context(
    canonical_request: bytes, pins: ControlLedgerPins
) -> tuple[dict[str, Any], ControlLedgerBinding, str]:
    value = _decode(
        canonical_request,
        maximum=worker_protocol.MAX_FRAME_BYTES,
        code="CONTROL_LEDGER_INPUT_INVALID",
    )
    if type(value) is not dict:
        _fail("CONTROL_LEDGER_INPUT_INVALID")
    deadline = value.get("deadline_monotonic_ns")
    if type(deadline) is not int or deadline < 1:
        _fail("CONTROL_LEDGER_INPUT_INVALID")
    # Structural validation must not turn a later exact replay into a deadline
    # failure, so it is anchored immediately before the sealed request's own
    # deadline.  ``begin`` independently uses the real monotonic clock before
    # it grants a new execution disposition.
    structural_now = deadline - 1
    try:
        request = worker_protocol.validate_control_request(
            value, now_monotonic_ns=structural_now
        )
    except worker_protocol.WorkerProtocolError:
        _fail("CONTROL_LEDGER_INPUT_INVALID")
    _validate_request_pins(request, pins)
    descriptor = request["credential_transport"]["descriptors"][0]
    binding = ControlLedgerBinding(
        pins=pins,
        operation=request["operation"],
        operation_revision=request["operation_revision"],
        target_phase=request["target_phase"],
        phase_request_sha256=request["phase_request_sha256"],
        phase_idempotency_key=request["phase_idempotency_key"],
        control_idempotency_key=request["control_idempotency_key"],
        credential_scope=descriptor["scope"],
        credential_generation=descriptor["credential_generation"],
        target_identity=descriptor["target_identity"],
    )
    _binding_value(binding)
    return request, binding, hashlib.sha256(canonical_request).hexdigest()


def canonical_control_request_sha256(
    canonical_request: bytes, pins: ControlLedgerPins
) -> str:
    """Validate a canonical fixed control request and return its digest."""

    _request, _binding, request_sha256 = _request_context(canonical_request, pins)
    return request_sha256


def _control_request_binding(request: Mapping[str, Any]) -> dict[str, Any]:
    value = {
        "request_sha256": hashlib.sha256(
            worker_protocol.canonical_json_bytes(request)
        ).hexdigest(),
        "request_id": request["request_id"],
        "operation": request["operation"],
        "target_phase": request["target_phase"],
        "phase_request_sha256": request["phase_request_sha256"],
        "phase_idempotency_key": request["phase_idempotency_key"],
        "control_idempotency_key": request["control_idempotency_key"],
        "idempotency_ledger_identity": request["idempotency_ledger_identity"],
        "idempotency_ledger_revision": request["idempotency_ledger_revision"],
        "job_revision": request["job_revision"],
        "reviewed_job_sha256": request["reviewed_job_sha256"],
        "approval_basis_sha256": request["approval_basis_sha256"],
        "execution_plan_sha256": request["execution_plan_sha256"],
        "launcher_handoff_receipt_sha256": request["launcher_handoff_receipt_sha256"],
        "generation_id": request["generation_id"],
        "generation_binding_sha256": request["generation_binding_sha256"],
        "host_boot_id_sha256": request["worker_binding"]["host_boot_id_sha256"],
        "deadline_monotonic_ns": request["deadline_monotonic_ns"],
    }
    if frozenset(value) != _CONTROL_REQUEST_BINDING_KEYS:
        _fail("CONTROL_LEDGER_RESULT_INVALID")
    return value


def _parse_rfc3339(value: Any) -> dt.datetime:
    if type(value) is not str or _RFC3339_RE.fullmatch(value) is None:
        _fail("CONTROL_LEDGER_RESULT_INVALID")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _fail("CONTROL_LEDGER_RESULT_INVALID")
    if parsed.tzinfo is None:
        _fail("CONTROL_LEDGER_RESULT_INVALID")
    return parsed.astimezone(dt.timezone.utc)


def _utc_timestamp(value: dt.datetime) -> str:
    if type(value) is not dt.datetime or value.tzinfo is None:
        _fail("CONTROL_LEDGER_RESULT_INVALID")
    return (
        value.astimezone(dt.timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _validate_control_receipt(
    receipt: Any,
    request: Mapping[str, Any],
    *,
    now_utc: dt.datetime,
) -> None:
    if type(receipt) is not dict or frozenset(receipt) != _CONTROL_RECEIPT_KEYS:
        _fail("CONTROL_LEDGER_RESULT_INVALID")
    expected = {
        "schema": worker_protocol.CONTROL_RECEIPT_SCHEMA,
        "operation": request["operation"],
        "generation_id": request["generation_id"],
        "target_phase": request["target_phase"],
        "phase_request_sha256": request["phase_request_sha256"],
        "phase_idempotency_key": request["phase_idempotency_key"],
        "control_idempotency_key": request["control_idempotency_key"],
    }
    if any(receipt[key] != value for key, value in expected.items()):
        _fail("CONTROL_LEDGER_RESULT_INVALID")
    if receipt["observed_phase_state"] not in _MUTATION_STATES:
        _fail("CONTROL_LEDGER_RESULT_INVALID")
    immutable_receipt = receipt["immutable_phase_receipt_sha256"]
    if immutable_receipt is not None:
        _sha256(immutable_receipt, code="CONTROL_LEDGER_RESULT_INVALID")
    if (
        type(receipt["cancelled"]) is not bool
        or type(receipt["quarantined"]) is not bool
    ):
        _fail("CONTROL_LEDGER_RESULT_INVALID")
    expected_flags = {
        "query_phase_status": (False, False),
        "recover_phase_receipt": (False, False),
        "cancel_phase_work": (True, False),
        "quarantine_generation": (False, True),
    }[request["operation"]]
    if (receipt["cancelled"], receipt["quarantined"]) != expected_flags:
        _fail("CONTROL_LEDGER_RESULT_INVALID")
    if request["operation"] == "recover_phase_receipt" and immutable_receipt is None:
        _fail("CONTROL_LEDGER_RESULT_INVALID")

    issued_at = _parse_rfc3339(receipt["issued_at"])
    expires_at = _parse_rfc3339(receipt["expires_at"])
    if type(now_utc) is not dt.datetime or now_utc.tzinfo is None:
        _fail("CONTROL_LEDGER_RESULT_INVALID")
    now = now_utc.astimezone(dt.timezone.utc)
    if issued_at > now + dt.timedelta(seconds=worker_protocol.MAX_CLOCK_SKEW_SECONDS):
        _fail("CONTROL_LEDGER_RESULT_INVALID")
    if expires_at <= now:
        _fail("CONTROL_LEDGER_RESULT_INVALID")
    if expires_at <= issued_at or expires_at - issued_at > dt.timedelta(
        seconds=worker_protocol.MAX_EVIDENCE_TTL_SECONDS
    ):
        _fail("CONTROL_LEDGER_RESULT_INVALID")


def _validate_remote_error(error: Any, mutation_state: str) -> None:
    if type(error) is not dict or frozenset(error) != _REMOTE_ERROR_KEYS:
        _fail("CONTROL_LEDGER_RESULT_INVALID")
    if (error["dependency"], error["code"]) not in _REMOTE_ERROR_PAIRS:
        _fail("CONTROL_LEDGER_RESULT_INVALID")
    if type(error["retryable"]) is not bool:
        _fail("CONTROL_LEDGER_RESULT_INVALID")
    if error["mutation_state"] != mutation_state:
        _fail("CONTROL_LEDGER_RESULT_INVALID")
    if mutation_state != "none" and error["retryable"]:
        _fail("CONTROL_LEDGER_RESULT_INVALID")


def _validate_control_result(
    result: Any,
    request: Mapping[str, Any],
    *,
    now_utc: dt.datetime,
) -> dict[str, Any]:
    if type(result) is not dict or frozenset(result) != _CONTROL_RESULT_KEYS:
        _fail("CONTROL_LEDGER_RESULT_INVALID")
    if (
        result["schema"] != worker_protocol.CONTROL_RESULT_SCHEMA
        or result["protocol"] != worker_protocol.PROTOCOL_REVISION
    ):
        _fail("CONTROL_LEDGER_RESULT_INVALID")
    binding = result["request_binding"]
    if type(binding) is not dict or frozenset(binding) != _CONTROL_REQUEST_BINDING_KEYS:
        _fail("CONTROL_LEDGER_RESULT_INVALID")
    if _canonical_bytes(
        binding, code="CONTROL_LEDGER_RESULT_INVALID"
    ) != _canonical_bytes(
        _control_request_binding(request), code="CONTROL_LEDGER_RESULT_INVALID"
    ):
        _fail("CONTROL_LEDGER_RESULT_INVALID")
    if result["status"] not in {"succeeded", "failed"}:
        _fail("CONTROL_LEDGER_RESULT_INVALID")
    if result["mutation_state"] not in {"none", "committed", "ambiguous"}:
        _fail("CONTROL_LEDGER_RESULT_INVALID")
    try:
        contract = worker_protocol.control_operation_contract(request["operation"])
    except worker_protocol.WorkerProtocolError:
        _fail("CONTROL_LEDGER_RESULT_INVALID")
    mutation_state = result["mutation_state"]
    if result["status"] == "succeeded":
        if (
            result["error"] is not None
            or result["receipt"] is None
            or mutation_state != contract["success_mutation_state"]
        ):
            _fail("CONTROL_LEDGER_RESULT_INVALID")
        _validate_control_receipt(result["receipt"], request, now_utc=now_utc)
    else:
        if (
            result["receipt"] is not None
            or result["error"] is None
            or mutation_state not in contract["failure_mutation_states"]
        ):
            _fail("CONTROL_LEDGER_RESULT_INVALID")
        _validate_remote_error(result["error"], mutation_state)
    return result


def _entry_names(ledger_key: str) -> tuple[str, str]:
    digest = _sha256_revision(ledger_key).removeprefix("sha256:")
    return (
        f"control-ledger-{digest}.prepare.json",
        f"control-ledger-{digest}.result.json",
    )


class DurableControlLedger:
    """Append-only control ledger rooted at an owned directory descriptor."""

    __slots__ = (
        "pins",
        "expected_uid",
        "expected_gid",
        "_directory_fd",
        "_closed",
        "_lifecycle_lock",
    )

    def __init__(
        self,
        directory_fd: int,
        pins: ControlLedgerPins,
        *,
        expected_uid: int | None = None,
        expected_gid: int | None = None,
    ) -> None:
        _pins_value(pins)
        self.pins = pins
        self.expected_uid = os.geteuid() if expected_uid is None else expected_uid
        self.expected_gid = os.getegid() if expected_gid is None else expected_gid
        if (
            type(directory_fd) is not int
            or type(self.expected_uid) is not int
            or type(self.expected_gid) is not int
            or directory_fd < 0
            or self.expected_uid < 0
            or self.expected_gid < 0
        ):
            _fail("CONTROL_LEDGER_INPUT_INVALID")
        required = ("O_NOFOLLOW", "O_CLOEXEC", "O_DIRECTORY")
        if not all(hasattr(os, name) for name in required) or not hasattr(
            fcntl, "F_DUPFD_CLOEXEC"
        ):
            _fail("CONTROL_LEDGER_UNSUPPORTED")
        try:
            owned_fd = fcntl.fcntl(directory_fd, fcntl.F_DUPFD_CLOEXEC, 3)
        except OSError:
            _fail("CONTROL_LEDGER_DIRECTORY_UNSAFE")
        self._directory_fd = owned_fd
        self._closed = False
        self._lifecycle_lock = threading.RLock()
        try:
            self._verify_directory()
        except BaseException:
            os.close(self._directory_fd)
            self._closed = True
            raise

    def __enter__(self) -> "DurableControlLedger":
        self._ensure_open()
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.close()

    def _ensure_open(self) -> None:
        if self._closed:
            _fail("CONTROL_LEDGER_CLOSED")

    def fileno(self) -> int:
        with self._lifecycle_lock:
            self._ensure_open()
            return self._directory_fd

    def close(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                return
            descriptor = self._directory_fd
            self._directory_fd = -1
            self._closed = True
            try:
                os.close(descriptor)
            except OSError:
                _fail("CONTROL_LEDGER_IO_ERROR")

    def _verify_directory(self) -> None:
        self._ensure_open()
        try:
            metadata = os.fstat(self._directory_fd)
        except OSError:
            _fail("CONTROL_LEDGER_DIRECTORY_UNSAFE")
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != self.expected_uid
            or metadata.st_gid != self.expected_gid
            or stat.S_IMODE(metadata.st_mode) != PRIVATE_DIRECTORY_MODE
        ):
            _fail("CONTROL_LEDGER_DIRECTORY_UNSAFE")

    def _verify_file_metadata(
        self, metadata: os.stat_result, *, expected_links: int
    ) -> None:
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != self.expected_uid
            or metadata.st_gid != self.expected_gid
            or stat.S_IMODE(metadata.st_mode) != PRIVATE_FILE_MODE
            or metadata.st_nlink != expected_links
        ):
            _fail("CONTROL_LEDGER_ENTRY_UNSAFE")

    def _settled_read_metadata(self, descriptor: int) -> os.stat_result:
        for attempt in range(MAX_LINK_SETTLE_ATTEMPTS + 1):
            try:
                metadata = os.fstat(descriptor)
            except OSError:
                _fail("CONTROL_LEDGER_ENTRY_UNSAFE")
            if metadata.st_nlink not in {1, 2}:
                _fail("CONTROL_LEDGER_ENTRY_UNSAFE")
            self._verify_file_metadata(metadata, expected_links=metadata.st_nlink)
            if metadata.st_nlink == 1:
                return metadata
            if attempt == MAX_LINK_SETTLE_ATTEMPTS:
                _fail("CONTROL_LEDGER_ENTRY_UNSAFE")
            time.sleep(LINK_SETTLE_SECONDS)
        _fail("CONTROL_LEDGER_ENTRY_UNSAFE")

    def _read_entry(self, name: str) -> bytes | None:
        flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            descriptor = os.open(name, flags, dir_fd=self._directory_fd)
        except FileNotFoundError:
            return None
        except OSError as error:
            if error.errno in {errno.ELOOP, errno.EISDIR, errno.ENXIO}:
                _fail("CONTROL_LEDGER_ENTRY_UNSAFE")
            _fail("CONTROL_LEDGER_IO_ERROR")
        try:
            before = self._settled_read_metadata(descriptor)
            if before.st_size <= 0 or before.st_size > MAX_LEDGER_RECORD_BYTES:
                _fail("CONTROL_LEDGER_ENTRY_CORRUPT")
            chunks: list[bytes] = []
            total = 0
            while True:
                try:
                    chunk = os.read(
                        descriptor,
                        min(64 * 1024, MAX_LEDGER_RECORD_BYTES + 1 - total),
                    )
                except InterruptedError:
                    continue
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_LEDGER_RECORD_BYTES:
                    _fail("CONTROL_LEDGER_ENTRY_CORRUPT")
            after = os.fstat(descriptor)
            self._verify_file_metadata(after, expected_links=1)
            if (
                before.st_dev != after.st_dev
                or before.st_ino != after.st_ino
                or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
                or total != after.st_size
            ):
                _fail("CONTROL_LEDGER_ENTRY_CORRUPT")
            return b"".join(chunks)
        except ControlLedgerError:
            raise
        except OSError:
            _fail("CONTROL_LEDGER_IO_ERROR")
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass

    def _fsync_directory(self) -> None:
        try:
            os.fsync(self._directory_fd)
        except OSError:
            _fail("CONTROL_LEDGER_IO_ERROR")

    def _publish_no_replace(self, final_name: str, raw: bytes) -> bool:
        if type(raw) is not bytes or not raw or len(raw) > MAX_LEDGER_RECORD_BYTES:
            _fail("CONTROL_LEDGER_INPUT_INVALID")
        temp_name = ""
        descriptor = -1
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            for _attempt in range(16):
                temp_name = f".{final_name}.{os.urandom(16).hex()}.tmp"
                try:
                    descriptor = os.open(
                        temp_name,
                        flags,
                        PRIVATE_FILE_MODE,
                        dir_fd=self._directory_fd,
                    )
                    break
                except FileExistsError:
                    continue
                except OSError:
                    _fail("CONTROL_LEDGER_IO_ERROR")
            if descriptor < 0:
                _fail("CONTROL_LEDGER_IO_ERROR")
            os.fchmod(descriptor, PRIVATE_FILE_MODE)
            offset = 0
            while offset < len(raw):
                try:
                    written = os.write(descriptor, raw[offset:])
                except InterruptedError:
                    continue
                if written <= 0:
                    _fail("CONTROL_LEDGER_IO_ERROR")
                offset += written
            os.fsync(descriptor)
            temporary_metadata = os.fstat(descriptor)
            self._verify_file_metadata(temporary_metadata, expected_links=1)
            try:
                os.link(
                    temp_name,
                    final_name,
                    src_dir_fd=self._directory_fd,
                    dst_dir_fd=self._directory_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                os.unlink(temp_name, dir_fd=self._directory_fd)
                temp_name = ""
                self._fsync_directory()
                return False
            except OSError:
                _fail("CONTROL_LEDGER_IO_ERROR")
            published = os.stat(
                final_name,
                dir_fd=self._directory_fd,
                follow_symlinks=False,
            )
            self._verify_file_metadata(published, expected_links=2)
            if (
                published.st_dev != temporary_metadata.st_dev
                or published.st_ino != temporary_metadata.st_ino
            ):
                _fail("CONTROL_LEDGER_ENTRY_UNSAFE")
            os.unlink(temp_name, dir_fd=self._directory_fd)
            temp_name = ""
            self._fsync_directory()
            return True
        except ControlLedgerError:
            raise
        except OSError:
            _fail("CONTROL_LEDGER_IO_ERROR")
        finally:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if temp_name:
                try:
                    os.unlink(temp_name, dir_fd=self._directory_fd)
                    self._fsync_directory()
                except (OSError, ControlLedgerError):
                    pass

    def _validate_record_common(
        self,
        record: Mapping[str, Any],
        binding: ControlLedgerBinding,
        ledger_key: str,
        request_sha256: str,
        *,
        conflict: bool,
    ) -> None:
        expected_binding = _binding_value(binding)
        if (
            record.get("ledger_identity") != self.pins.ledger_identity
            or record.get("ledger_revision") != self.pins.ledger_revision
            or record.get("ledger_key") != ledger_key
            or _canonical_bytes(
                record.get("binding"), code="CONTROL_LEDGER_ENTRY_CORRUPT"
            )
            != _canonical_bytes(expected_binding, code="CONTROL_LEDGER_ENTRY_CORRUPT")
        ):
            _fail("CONTROL_LEDGER_ENTRY_CORRUPT")
        if record.get("request_sha256") != request_sha256:
            _fail(
                "CONTROL_LEDGER_KEY_CONFLICT"
                if conflict
                else "CONTROL_LEDGER_ENTRY_CORRUPT"
            )

    def _prepare_record(
        self,
        raw: bytes,
        binding: ControlLedgerBinding,
        ledger_key: str,
        request_sha256: str,
    ) -> dict[str, Any]:
        record = _decode(
            raw,
            maximum=MAX_LEDGER_RECORD_BYTES,
            code="CONTROL_LEDGER_ENTRY_CORRUPT",
        )
        if type(record) is not dict or frozenset(record) != _PREPARE_KEYS:
            _fail("CONTROL_LEDGER_ENTRY_CORRUPT")
        if record.get("schema") != PREPARE_SCHEMA:
            _fail("CONTROL_LEDGER_ENTRY_CORRUPT")
        self._validate_record_common(
            record,
            binding,
            ledger_key,
            request_sha256,
            conflict=True,
        )
        return record

    def _result_record(
        self,
        raw: bytes,
        request: Mapping[str, Any],
        binding: ControlLedgerBinding,
        ledger_key: str,
        request_sha256: str,
    ) -> tuple[dict[str, Any], bytes]:
        record = _decode(
            raw,
            maximum=MAX_LEDGER_RECORD_BYTES,
            code="CONTROL_LEDGER_ENTRY_CORRUPT",
        )
        if type(record) is not dict or frozenset(record) != _RESULT_KEYS:
            _fail("CONTROL_LEDGER_ENTRY_CORRUPT")
        if record.get("schema") != RESULT_SCHEMA:
            _fail("CONTROL_LEDGER_ENTRY_CORRUPT")
        self._validate_record_common(
            record,
            binding,
            ledger_key,
            request_sha256,
            conflict=False,
        )
        try:
            _sha256(record.get("result_sha256"), code="CONTROL_LEDGER_ENTRY_CORRUPT")
            validated_at = _parse_rfc3339(record.get("validated_at"))
        except ControlLedgerError:
            _fail("CONTROL_LEDGER_ENTRY_CORRUPT")
        result_bytes = _canonical_bytes(
            record.get("result"), code="CONTROL_LEDGER_ENTRY_CORRUPT"
        )
        if (
            len(result_bytes) > MAX_RESULT_BYTES
            or hashlib.sha256(result_bytes).hexdigest() != record["result_sha256"]
        ):
            _fail("CONTROL_LEDGER_ENTRY_CORRUPT")
        try:
            _validate_control_result(record["result"], request, now_utc=validated_at)
        except ControlLedgerError:
            _fail("CONTROL_LEDGER_ENTRY_CORRUPT")
        return record, result_bytes

    def begin(self, canonical_request: bytes) -> ControlBeginOutcome:
        """Prepare, replay, or fail closed on an incomplete control request."""

        with self._lifecycle_lock:
            self._verify_directory()
            request, binding, request_sha256 = _request_context(
                canonical_request, self.pins
            )
            binding_value = _binding_value(binding)
            ledger_key = derive_control_ledger_key(binding)
            prepare_name, result_name = _entry_names(ledger_key)
            prepare_raw = self._read_entry(prepare_name)
            won_prepare = False
            if prepare_raw is None:
                if self._read_entry(result_name) is not None:
                    _fail("CONTROL_LEDGER_ENTRY_CORRUPT")
                now_monotonic_ns = time.monotonic_ns()
                if request["deadline_monotonic_ns"] <= now_monotonic_ns:
                    _fail("CONTROL_LEDGER_REQUEST_EXPIRED")
                if (
                    request["deadline_monotonic_ns"] - now_monotonic_ns
                    > worker_protocol.MAX_DEADLINE_AHEAD_NS
                ):
                    _fail("CONTROL_LEDGER_DEADLINE_INVALID")
                prepare_raw = _canonical_bytes(
                    {
                        "schema": PREPARE_SCHEMA,
                        "ledger_identity": self.pins.ledger_identity,
                        "ledger_revision": self.pins.ledger_revision,
                        "ledger_key": ledger_key,
                        "binding": binding_value,
                        "request_sha256": request_sha256,
                    },
                    code="CONTROL_LEDGER_INPUT_INVALID",
                )
                won_prepare = self._publish_no_replace(prepare_name, prepare_raw)
                if not won_prepare:
                    prepare_raw = self._read_entry(prepare_name)
                    if prepare_raw is None:
                        _fail("CONTROL_LEDGER_ENTRY_CORRUPT")

            self._prepare_record(
                prepare_raw,
                binding,
                ledger_key,
                request_sha256,
            )
            result_raw = self._read_entry(result_name)
            if result_raw is None:
                self._fsync_directory()
                action: Literal["execute", "recovery_required"] = (
                    "execute"
                    if won_prepare
                    and request["deadline_monotonic_ns"] > time.monotonic_ns()
                    else "recovery_required"
                )
                return ControlBeginOutcome(
                    action=action,
                    ledger_key=ledger_key,
                    request_sha256=request_sha256,
                )
            _record, result_bytes = self._result_record(
                result_raw,
                request,
                binding,
                ledger_key,
                request_sha256,
            )
            self._fsync_directory()
            return ControlBeginOutcome(
                action="replay",
                ledger_key=ledger_key,
                request_sha256=request_sha256,
                result_bytes=result_bytes,
            )

    def commit_result(
        self,
        canonical_request: bytes,
        canonical_result: bytes,
    ) -> bytes:
        """Publish one exact validated result for an existing prepare record."""

        with self._lifecycle_lock:
            self._verify_directory()
            request, binding, request_sha256 = _request_context(
                canonical_request, self.pins
            )
            result_value = _decode(
                canonical_result,
                maximum=MAX_RESULT_BYTES,
                code="CONTROL_LEDGER_RESULT_INVALID",
            )
            ledger_key = derive_control_ledger_key(binding)
            prepare_name, result_name = _entry_names(ledger_key)
            prepare_raw = self._read_entry(prepare_name)
            if prepare_raw is None:
                if self._read_entry(result_name) is not None:
                    _fail("CONTROL_LEDGER_ENTRY_CORRUPT")
                _fail("CONTROL_LEDGER_PREPARE_REQUIRED")
            self._prepare_record(
                prepare_raw,
                binding,
                ledger_key,
                request_sha256,
            )

            result_digest = hashlib.sha256(canonical_result).hexdigest()
            existing_raw = self._read_entry(result_name)
            if existing_raw is not None:
                existing, existing_result = self._result_record(
                    existing_raw,
                    request,
                    binding,
                    ledger_key,
                    request_sha256,
                )
                if (
                    existing["result_sha256"] != result_digest
                    or existing_result != canonical_result
                ):
                    _fail("CONTROL_LEDGER_RESULT_CONFLICT")
                self._fsync_directory()
                return existing_result

            validated_at = dt.datetime.now(dt.timezone.utc)
            _validate_control_result(result_value, request, now_utc=validated_at)
            wrapper = _canonical_bytes(
                {
                    "schema": RESULT_SCHEMA,
                    "ledger_identity": self.pins.ledger_identity,
                    "ledger_revision": self.pins.ledger_revision,
                    "ledger_key": ledger_key,
                    "binding": _binding_value(binding),
                    "request_sha256": request_sha256,
                    "result_sha256": result_digest,
                    "validated_at": _utc_timestamp(validated_at),
                    "result": result_value,
                },
                code="CONTROL_LEDGER_RESULT_INVALID",
            )
            published = self._publish_no_replace(result_name, wrapper)
            if published:
                return canonical_result
            existing_raw = self._read_entry(result_name)
            if existing_raw is None:
                _fail("CONTROL_LEDGER_ENTRY_CORRUPT")
            existing, existing_result = self._result_record(
                existing_raw,
                request,
                binding,
                ledger_key,
                request_sha256,
            )
            if (
                existing["result_sha256"] != result_digest
                or existing_result != canonical_result
            ):
                _fail("CONTROL_LEDGER_RESULT_CONFLICT")
            self._fsync_directory()
            return existing_result


__all__ = [
    "BINDING_SCHEMA",
    "ControlBeginOutcome",
    "ControlLedgerBinding",
    "ControlLedgerError",
    "ControlLedgerPins",
    "DurableControlLedger",
    "MAX_LEDGER_RECORD_BYTES",
    "MAX_RESULT_BYTES",
    "PREPARE_SCHEMA",
    "PUBLIC_ERROR_MESSAGES",
    "RESULT_SCHEMA",
    "canonical_control_request_sha256",
    "derive_control_ledger_key",
]
