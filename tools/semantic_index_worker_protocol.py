"""Fail-closed UDS protocol for the Production semantic-index worker.

Only an already-connected ``AF_UNIX`` stream (or its file descriptor) is
accepted.  The externally attested launcher owns pathname resolution and
service lifecycle.  Capability bytes never enter JSON: ordinary operations
receive one sealed memfd and reconcile receives exactly three, transferred in
the fixed order with ``SCM_RIGHTS``.

The module deliberately uses only the Python standard library and is suitable
for the isolated ``/usr/bin/python3 -I -S`` coordinator.
"""

from __future__ import annotations

import array
import base64
import dataclasses
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import select
import socket
import stat
import struct
import time
import unicodedata
from typing import Any, Mapping, Sequence
from urllib.parse import unquote_to_bytes


PROTOCOL_REVISION = "simworld-semantic-index-worker/v1"
REQUEST_SCHEMA = "simworld-semantic-index-phase-request/v1"
RESULT_SCHEMA = "simworld-semantic-index-worker-result/v1"
ARTIFACT_ROOT_SCHEMA = "simworld-semantic-index-worker-artifact-root/v1"
CONTROL_REQUEST_SCHEMA = "simworld-semantic-index-control-request/v1"
CONTROL_RESULT_SCHEMA = "simworld-semantic-index-control-result/v1"
CONTROL_RECEIPT_SCHEMA = "simworld-semantic-index-control-receipt/v1"
CREDENTIAL_TRANSPORT = "sealed_memfd_scm_rights_v1"

MAX_FRAME_BYTES = 2 * 1024 * 1024
MAX_CAPABILITY_BYTES = 16 * 1024
MIN_CAPABILITY_BYTES = 16
MAX_ARTIFACT_BYTES = 9_007_199_254_740_991
MAX_PLAN_INTEGER = 9_007_199_254_740_991
MAX_ITEMS = 100_000
MAX_CHUNKS = 100_000
MAX_JSON_INTEGER = 2**63 - 1
MAX_DEADLINE_AHEAD_NS = 24 * 60 * 60 * 1_000_000_000
MAX_HANDSHAKE_AHEAD_NS = 5 * 1_000_000_000
MAX_EVIDENCE_TTL_SECONDS = 15 * 60
MAX_CLOCK_SKEW_SECONDS = 5
MAX_JSON_DEPTH = 24
MAX_JSON_CONTAINER_ITEMS = 512
MAX_JSON_STRING_BYTES = 4096
_READ_CHUNK_BYTES = 64 * 1024
_MAX_CREDENTIAL_FDS = 3
_PROC_READ_BYTES = 4096
_MAX_PERCENT_DECODE_PASSES = 8

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SHA256_REVISION_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,159}$")
_REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+\-]{1,239}$")
_GENERATION_ID_RE = re.compile(r"^semantic-generation:[0-9a-f]{64}$")
_RUN_ID_RE = re.compile(r"^semantic-index-run:[A-Za-z0-9][A-Za-z0-9._:@+\-]{3,140}$")
_CORRELATION_ID_RE = re.compile(
    r"^semantic-index-correlation:[A-Za-z0-9][A-Za-z0-9._:@+\-]*$"
)
_IMAGE_RE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
_RFC3339_RE = re.compile(
    r"^[0-9]{4}-(0[1-9]|1[0-2])-([0-2][0-9]|3[01])T"
    r"([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
    r"(\.[0-9]{1,9})?(Z|[+-]([01][0-9]|2[0-3]):[0-5][0-9])$"
)

_SERIALIZATION_CONTRACT = {
    "encoding": "utf-8",
    "canonicalization": "rfc8785-jcs-v1",
    "duplicate_keys": "reject",
    "unicode_normalization": "require_already_nfc",
    "numbers": "integers_only",
    "nonfinite_numbers": "reject",
    "trailing_newline": False,
}


def _operation_revision(operation: str) -> str:
    material = f"{PROTOCOL_REVISION}\0{operation}\0closed-operation-v1".encode("ascii")
    return "sha256:" + hashlib.sha256(material).hexdigest()


_OPERATION_CONTRACTS = {
    "inspect_exact_assets": {
        "phase": "inspect",
        "scopes": ("ue_disposable_scene_inspect_spawn_cleanup",),
        "components": ("unreal",),
        "metrics": frozenset({"assets_inspected"}),
        "success_mutation_state": "none",
        "failure_mutation_states": frozenset({"none", "ambiguous"}),
        "error_dependencies": frozenset({"worker", "unreal", "evidence_store"}),
        "artifact_schema": "simworld-semantic-index-inspect-artifact/v1",
    },
    "render_exact_views": {
        "phase": "render",
        "scopes": ("ue_disposable_scene_render_staging_cleanup",),
        "components": ("unreal",),
        "metrics": frozenset({"assets_rendered", "rendered_views"}),
        "success_mutation_state": "staged",
        "failure_mutation_states": frozenset({"none", "staged", "ambiguous"}),
        "error_dependencies": frozenset({"worker", "unreal", "evidence_store"}),
        "artifact_schema": "simworld-semantic-index-render-artifact/v1",
    },
    "caption_exact_render_set": {
        "phase": "caption",
        "scopes": ("caption_bounded_idempotent_call",),
        "components": ("caption",),
        "metrics": frozenset({"catalog_records"}),
        "success_mutation_state": "committed",
        "failure_mutation_states": frozenset({"none", "committed", "ambiguous"}),
        "error_dependencies": frozenset({"worker", "caption", "evidence_store"}),
        "artifact_schema": "simworld-semantic-index-caption-artifact/v1",
    },
    "embed_exact_text_set": {
        "phase": "embed",
        "scopes": ("embedding_immutable_vector_staging",),
        "components": ("embedding",),
        "metrics": frozenset({"dense_vectors", "sparse_vectors"}),
        "success_mutation_state": "staged",
        "failure_mutation_states": frozenset({"none", "staged", "ambiguous"}),
        "error_dependencies": frozenset({"worker", "embedding", "evidence_store"}),
        "artifact_schema": "simworld-semantic-index-embed-artifact/v1",
    },
    "upsert_postgres_exact": {
        "phase": "postgres",
        "scopes": ("postgres_generation_write",),
        "components": ("postgres",),
        "metrics": frozenset({"postgres_rows"}),
        "success_mutation_state": "committed",
        "failure_mutation_states": frozenset({"none", "committed", "ambiguous"}),
        "error_dependencies": frozenset({"worker", "postgres", "evidence_store"}),
        "artifact_schema": "simworld-semantic-index-postgres-artifact/v1",
    },
    "upsert_qdrant_exact": {
        "phase": "qdrant",
        "scopes": ("qdrant_generation_write",),
        "components": ("qdrant",),
        "metrics": frozenset({"qdrant_points"}),
        "success_mutation_state": "committed",
        "failure_mutation_states": frozenset(
            {"none", "staged", "committed", "ambiguous"}
        ),
        "error_dependencies": frozenset({"worker", "qdrant", "evidence_store"}),
        "artifact_schema": "simworld-semantic-index-qdrant-artifact/v1",
    },
    "reconcile_exact_snapshot": {
        "phase": "reconcile",
        "scopes": (
            "postgres_generation_read_only",
            "qdrant_generation_read_only",
            "ue_runtime_read_only",
        ),
        "components": ("postgres", "qdrant", "unreal"),
        "metrics": frozenset({"catalog_records", "postgres_rows", "qdrant_points"}),
        "success_mutation_state": "none",
        "failure_mutation_states": frozenset({"none"}),
        "error_dependencies": frozenset(
            {"worker", "unreal", "postgres", "qdrant", "evidence_store"}
        ),
        "artifact_schema": "simworld-semantic-index-reconcile-artifact/v1",
    },
}
for _name, _contract in _OPERATION_CONTRACTS.items():
    _contract["revision"] = _operation_revision(_name)

_CONTROL_OPERATION_CONTRACTS = {
    "query_phase_status": {
        "scope": "semantic_index_phase_status_read",
        "success_mutation_state": "none",
        "failure_mutation_states": frozenset({"none"}),
    },
    "recover_phase_receipt": {
        "scope": "semantic_index_phase_receipt_recover",
        "success_mutation_state": "none",
        "failure_mutation_states": frozenset({"none"}),
    },
    "cancel_phase_work": {
        "scope": "semantic_index_phase_work_cancel",
        "success_mutation_state": "committed",
        "failure_mutation_states": frozenset({"none", "committed", "ambiguous"}),
    },
    "quarantine_generation": {
        "scope": "semantic_index_generation_quarantine",
        "success_mutation_state": "committed",
        "failure_mutation_states": frozenset({"none", "committed", "ambiguous"}),
    },
}
for _name, _contract in _CONTROL_OPERATION_CONTRACTS.items():
    _contract["revision"] = _operation_revision(_name)

_REMOTE_ERRORS = {
    ("worker", "WORKER_BUSY"): "Worker is temporarily busy",
    ("worker", "WORKER_INTERNAL"): "Worker operation failed",
    ("worker", "WORKER_LEDGER_CONFLICT"): "Worker idempotency ledger conflict",
    ("worker", "WORKER_CANCELLED"): "Worker operation was cancelled",
    ("unreal", "UNREAL_UNAVAILABLE"): "Unreal runtime is unavailable",
    ("unreal", "UNREAL_OPERATION_FAILED"): "Unreal operation failed",
    ("caption", "CAPTION_UNAVAILABLE"): "Caption provider is unavailable",
    ("caption", "CAPTION_BUDGET_EXCEEDED"): "Caption budget was exceeded",
    ("caption", "CAPTION_RESULT_INVALID"): "Caption result was invalid",
    ("caption", "CAPTION_STATE_AMBIGUOUS"): "Caption request state is ambiguous",
    ("embedding", "EMBEDDING_UNAVAILABLE"): "Embedding service is unavailable",
    ("embedding", "EMBEDDING_RESULT_INVALID"): "Embedding result was invalid",
    ("postgres", "POSTGRES_UNAVAILABLE"): "PostgreSQL is unavailable",
    ("postgres", "POSTGRES_WRITE_FAILED"): "PostgreSQL generation write failed",
    ("qdrant", "QDRANT_UNAVAILABLE"): "Qdrant is unavailable",
    ("qdrant", "QDRANT_WRITE_FAILED"): "Qdrant generation write failed",
    ("evidence_store", "EVIDENCE_STORE_UNAVAILABLE"): "Evidence store is unavailable",
    ("evidence_store", "EVIDENCE_PUBLICATION_FAILED"): "Evidence publication failed",
}

# Every public dependency/code identity has a closed mutation/retry surface.
# Phase/control contracts further intersect this set with their own legal
# mutation states.  ``none`` intentionally permits either retry decision;
# all evidence-bearing states categorically forbid automatic retry.
_REMOTE_ERROR_STATE_RETRY = {
    pair: frozenset(
        {
            ("none", False),
            ("none", True),
            ("staged", False),
            ("committed", False),
            ("ambiguous", False),
        }
    )
    for pair in _REMOTE_ERRORS
}

_REQUEST_KEYS = frozenset(
    {
        "schema",
        "protocol",
        "operation",
        "operation_revision",
        "phase",
        "request_id",
        "job_revision",
        "reviewed_job_sha256",
        "approval_basis_sha256",
        "execution_plan_sha256",
        "launcher_handoff_receipt_sha256",
        "generation_id",
        "generation_binding_sha256",
        "execution_binding",
        "worker_binding",
        "input_binding",
        "expected_targets",
        "expected_metrics",
        "credential_transport",
        "idempotency_key",
        "idempotency_ledger_identity",
        "idempotency_ledger_revision",
        "deadline_monotonic_ns",
    }
)
_CONTROL_REQUEST_KEYS = frozenset(
    {
        "schema",
        "protocol",
        "operation",
        "operation_revision",
        "request_id",
        "job_revision",
        "reviewed_job_sha256",
        "approval_basis_sha256",
        "execution_plan_sha256",
        "launcher_handoff_receipt_sha256",
        "generation_id",
        "generation_binding_sha256",
        "execution_binding",
        "worker_binding",
        "target_phase",
        "phase_request_sha256",
        "phase_idempotency_key",
        "control_idempotency_key",
        "idempotency_ledger_identity",
        "idempotency_ledger_revision",
        "credential_transport",
        "deadline_monotonic_ns",
    }
)
_EXECUTION_BINDING_KEYS = frozenset(
    {"run_id", "correlation_id", "owner_identity", "lease_id", "slot_id"}
)
_WORKER_BINDING_KEYS = frozenset(
    {
        "deployment_identity",
        "runtime_image",
        "whoami_sha256",
        "peer_attestation_sha256",
        "runtime_attestation_sha256",
        "host_boot_id_sha256",
        "peer_uid",
        "peer_gid",
        "peer_pid",
        "process_start_time_ticks",
        "process_start_token_sha256",
        "socket_device",
        "socket_inode",
        "socket_inode_binding_sha256",
    }
)
_INPUT_BINDING_KEYS = frozenset({"schema", "sha256", "item_count", "byte_count"})
_CREDENTIAL_TRANSPORT_KEYS = frozenset(
    {"kind", "json_contains_credential_bytes", "descriptors"}
)
_CREDENTIAL_DESCRIPTOR_KEYS = frozenset(
    {
        "fd_index",
        "scope",
        "credential_generation",
        "generation_id",
        "target_identity",
        "byte_count",
    }
)
_TARGET_KEYS = frozenset(
    {
        "component",
        "scope",
        "credential_generation",
        "generation_id",
        "target_identity",
        "runtime_identity",
        "runtime_image",
        "whoami_sha256",
        "runtime_attestation_sha256",
    }
)
_RESULT_KEYS = frozenset(
    {
        "schema",
        "protocol",
        "request_binding",
        "status",
        "mutation_state",
        "metrics",
        "artifact",
        "error",
    }
)
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
_REQUEST_BINDING_KEYS = frozenset(
    {
        "request_sha256",
        "request_id",
        "operation",
        "phase",
        "idempotency_key",
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
_ARTIFACT_KEYS = frozenset(
    {
        "schema",
        "protocol",
        "phase",
        "operation",
        "request_sha256",
        "job_revision",
        "reviewed_job_sha256",
        "approval_basis_sha256",
        "execution_plan_sha256",
        "generation_id",
        "generation_binding_sha256",
        "idempotency_key",
        "idempotency_ledger_identity",
        "idempotency_ledger_revision",
        "input_artifact_sha256",
        "artifact_root",
        "observed_targets",
        "worker_attestation",
        "issued_at",
        "expires_at",
        "serialization_contract",
    }
)
_ARTIFACT_ROOT_KEYS = frozenset(
    {"schema", "sha256", "byte_count", "chunk_count", "chunks_sha256"}
)
_WORKER_ATTESTATION_KEYS = frozenset(
    {
        "deployment_identity",
        "runtime_image",
        "whoami_sha256",
        "peer_attestation_sha256",
        "runtime_attestation_sha256",
        "host_boot_id_sha256",
        "process_start_token_sha256",
        "socket_inode_binding_sha256",
    }
)
_REMOTE_ERROR_KEYS = frozenset({"dependency", "code", "retryable", "mutation_state"})
_MUTATION_STATES = frozenset({"none", "staged", "committed", "ambiguous"})
_PHASES = frozenset(contract["phase"] for contract in _OPERATION_CONTRACTS.values())


def operation_contract(operation: str) -> dict[str, Any]:
    """Return a detached public copy of one static operation contract."""

    if not isinstance(operation, str) or operation not in _OPERATION_CONTRACTS:
        _raise("WORKER_OPERATION_INVALID", "Worker operation is not allowed")
    contract = _OPERATION_CONTRACTS[operation]
    return {
        "phase": contract["phase"],
        "revision": contract["revision"],
        "scopes": list(contract["scopes"]),
        "components": list(contract["components"]),
        "metrics": sorted(contract["metrics"]),
        "success_mutation_state": contract["success_mutation_state"],
        "failure_mutation_states": sorted(contract["failure_mutation_states"]),
        "error_dependencies": sorted(contract["error_dependencies"]),
        "artifact_schema": contract["artifact_schema"],
    }


def control_operation_contract(operation: str) -> dict[str, Any]:
    """Return a detached copy of one fixed recovery/control contract."""

    if not isinstance(operation, str) or operation not in _CONTROL_OPERATION_CONTRACTS:
        _raise("WORKER_OPERATION_INVALID", "Worker control operation is not allowed")
    contract = _CONTROL_OPERATION_CONTRACTS[operation]
    return {
        "revision": contract["revision"],
        "scope": contract["scope"],
        "success_mutation_state": contract["success_mutation_state"],
        "failure_mutation_states": sorted(contract["failure_mutation_states"]),
        "error_dependencies": ["evidence_store", "worker"],
    }


@dataclasses.dataclass
class WorkerProtocolError(Exception):
    """Bounded local public error; no peer-controlled message is retained."""

    code: str
    message: str
    dependency: str = "worker"
    retryable: bool = False
    mutation_state: str = "none"

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def public_dict(self) -> dict[str, Any]:
        return {
            "dependency": self.dependency,
            "code": self.code,
            "retryable": self.retryable,
            "mutation_state": self.mutation_state,
            "message": self.message,
        }


@dataclasses.dataclass(frozen=True)
class WorkerResponse:
    value: dict[str, Any]
    canonical_bytes: bytes


_RECEIVED_REQUEST_SENTINEL = object()


@dataclasses.dataclass(slots=True, init=False)
class ReceivedWorkerRequest:
    """Validated request plus the one-shot response channel it now owns.

    ``receive_worker_request`` consumes the caller's socket object or integer
    descriptor.  Ownership then lives here until ``send_worker_response`` (or
    ``close``) consumes it exactly once.
    """

    value: dict[str, Any]
    canonical_bytes: bytes
    capabilities: tuple[bytes, ...] = dataclasses.field(repr=False)
    _connection: socket.socket = dataclasses.field(repr=False, compare=False)
    _response_deadline_monotonic_ns: int = dataclasses.field(repr=False, compare=False)
    _authentication_token: object = dataclasses.field(repr=False, compare=False)
    _consumed: bool = dataclasses.field(
        repr=False, compare=False
    )

    def __init__(
        self,
        sentinel: object,
        *,
        value: dict[str, Any],
        canonical_bytes: bytes,
        capabilities: tuple[bytes, ...],
        connection: socket.socket,
        response_deadline_monotonic_ns: int,
    ) -> None:
        if sentinel is not _RECEIVED_REQUEST_SENTINEL:
            raise TypeError(
                "ReceivedWorkerRequest can only be created by authenticated receive"
            )
        self.value = value
        self.canonical_bytes = canonical_bytes
        self.capabilities = capabilities
        self._connection = connection
        self._response_deadline_monotonic_ns = response_deadline_monotonic_ns
        self._authentication_token = _RECEIVED_REQUEST_SENTINEL
        self._consumed = False

    def __init_subclass__(cls, **_kwargs: Any) -> None:
        raise TypeError("ReceivedWorkerRequest cannot be subclassed")

    def _take_response_socket(self) -> socket.socket:
        if self._consumed:
            _raise(
                "WORKER_SESSION_CONSUMED",
                "Worker response channel was already consumed",
            )
        self._consumed = True
        return self._connection

    def close(self) -> None:
        if not self._consumed:
            self._consumed = True
            self._connection.close()

    def __enter__(self) -> ReceivedWorkerRequest:
        return self

    def __exit__(self, _exc_type: Any, _exc_value: Any, _traceback: Any) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except BaseException:
            pass

    def __reduce__(self) -> Any:
        raise TypeError("ReceivedWorkerRequest cannot be serialized")

    def __copy__(self) -> Any:
        raise TypeError("ReceivedWorkerRequest cannot be copied")

    def __deepcopy__(self, _memo: Any) -> Any:
        raise TypeError("ReceivedWorkerRequest cannot be copied")


def _require_authenticated_received_request(value: Any) -> ReceivedWorkerRequest:
    if (
        type(value) is not ReceivedWorkerRequest
        or getattr(value, "_authentication_token", None)
        is not _RECEIVED_REQUEST_SENTINEL
    ):
        _raise("WORKER_SESSION_INVALID", "Worker request session is invalid")
    return value


def _new_authenticated_received_request(
    *,
    value: dict[str, Any],
    canonical_bytes: bytes,
    capabilities: tuple[bytes, ...],
    connection: socket.socket,
    response_deadline_monotonic_ns: int,
) -> ReceivedWorkerRequest:
    return ReceivedWorkerRequest(
        _RECEIVED_REQUEST_SENTINEL,
        value=value,
        canonical_bytes=canonical_bytes,
        capabilities=capabilities,
        connection=connection,
        response_deadline_monotonic_ns=response_deadline_monotonic_ns,
    )


@dataclasses.dataclass(frozen=True)
class PeerPolicy:
    """Exact launcher-attested identity of one already-connected worker FD."""

    expected_uid: int
    expected_gid: int
    expected_pid: int
    expected_process_start_time_ticks: int
    expected_socket_device: int
    expected_socket_inode: int
    host_boot_id_sha256: str
    peer_attestation_sha256: str

    def __post_init__(self) -> None:
        _uid_gid(self.expected_uid, "expected UID")
        _uid_gid(self.expected_gid, "expected GID")
        _integer(self.expected_pid, "expected PID", minimum=1, maximum=2**31 - 1)
        _integer(
            self.expected_process_start_time_ticks,
            "expected process start time",
            minimum=1,
            maximum=MAX_PLAN_INTEGER,
        )
        _integer(
            self.expected_socket_device,
            "expected socket device",
            minimum=0,
            maximum=MAX_PLAN_INTEGER,
        )
        _integer(
            self.expected_socket_inode,
            "expected socket inode",
            minimum=1,
            maximum=MAX_PLAN_INTEGER,
        )
        _sha256(self.host_boot_id_sha256, "host boot identity")
        _sha256(self.peer_attestation_sha256, "peer attestation")


@dataclasses.dataclass(frozen=True)
class ClientPeerPolicy:
    """Worker-owned policy for the launcher/origin peer and accepted FD.

    Linux captures ``SO_PEERCRED`` when the launcher originates the worker
    connection.  Passing the connected client endpoint to the coordinator via
    ``SCM_RIGHTS`` does not replace those credentials.  The independently
    pinned receipt digest binds the later coordinator request to that exact
    launcher handoff without claiming that the coordinator originated the
    worker transport.
    """

    expected_uid: int
    expected_gid: int
    expected_pid: int
    expected_process_start_time_ticks: int
    expected_socket_device: int
    expected_socket_inode: int
    host_boot_id_sha256: str
    expected_launcher_handoff_receipt_sha256: str

    def __post_init__(self) -> None:
        _uid_gid(self.expected_uid, "expected launcher/origin UID")
        _uid_gid(self.expected_gid, "expected launcher/origin GID")
        _integer(
            self.expected_pid,
            "expected launcher/origin PID",
            minimum=1,
            maximum=2**31 - 1,
        )
        _integer(
            self.expected_process_start_time_ticks,
            "expected launcher/origin process start time",
            minimum=1,
            maximum=MAX_PLAN_INTEGER,
        )
        _integer(
            self.expected_socket_device,
            "expected accepted socket device",
            minimum=0,
            maximum=MAX_PLAN_INTEGER,
        )
        _integer(
            self.expected_socket_inode,
            "expected accepted socket inode",
            minimum=1,
            maximum=MAX_PLAN_INTEGER,
        )
        _sha256(self.host_boot_id_sha256, "launcher/origin host boot identity")
        _sha256(
            self.expected_launcher_handoff_receipt_sha256,
            "expected launcher handoff receipt",
        )


def _raise(
    code: str,
    message: str,
    *,
    retryable: bool = False,
    mutation_state: str = "none",
    dependency: str = "worker",
) -> None:
    raise WorkerProtocolError(code, message, dependency, retryable, mutation_state)


def _exact_keys(value: Any, keys: frozenset[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or frozenset(value) != keys:
        _raise("WORKER_PROTOCOL_SHAPE_INVALID", f"{label} has an invalid field set")
    return value


def _integer(
    value: Any,
    label: str,
    *,
    minimum: int = 0,
    maximum: int = MAX_JSON_INTEGER,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        _raise("WORKER_PROTOCOL_VALUE_INVALID", f"{label} is outside its allowed range")
    return value


def _uid_gid(value: Any, label: str) -> int:
    return _integer(value, label, minimum=0, maximum=2**31 - 1)


def _nfc_text(value: Any, label: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value:
        _raise("WORKER_PROTOCOL_VALUE_INVALID", f"{label} must be non-empty text")
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeError:
        _raise("WORKER_PROTOCOL_VALUE_INVALID", f"{label} is not valid UTF-8 text")
    if len(encoded) > maximum or unicodedata.normalize("NFC", value) != value:
        _raise("WORKER_PROTOCOL_VALUE_INVALID", f"{label} is not canonical text")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        _raise("WORKER_PROTOCOL_VALUE_INVALID", f"{label} contains a control character")
    return value


def _matched_text(
    value: Any, label: str, pattern: re.Pattern[str], maximum: int
) -> str:
    text = _nfc_text(value, label, maximum=maximum)
    if not pattern.fullmatch(text) or "://" in text:
        _raise("WORKER_PROTOCOL_VALUE_INVALID", f"{label} has an invalid identity")
    return text


def _safe_id(value: Any, label: str) -> str:
    return _matched_text(value, label, _SAFE_ID_RE, 160)


def _revision(value: Any, label: str) -> str:
    return _matched_text(value, label, _REVISION_RE, 240)


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        _raise("WORKER_PROTOCOL_VALUE_INVALID", f"{label} is not a SHA-256 digest")
    return value


def _sha256_revision(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_REVISION_RE.fullmatch(value):
        _raise("WORKER_PROTOCOL_VALUE_INVALID", f"{label} is not a pinned revision")
    return value


def _generation_id(value: Any) -> str:
    if not isinstance(value, str) or not _GENERATION_ID_RE.fullmatch(value):
        _raise("WORKER_PROTOCOL_VALUE_INVALID", "Generation identity is invalid")
    return value


def _image(value: Any, label: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    text = _nfc_text(value, label, maximum=512)
    if not _IMAGE_RE.fullmatch(text) or "://" in text:
        _raise("WORKER_PROTOCOL_VALUE_INVALID", f"{label} is not a digest-pinned image")
    return text


def _validate_json_value(value: Any, *, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        _raise("WORKER_JSON_INVALID", "JSON nesting exceeds the protocol limit")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int) and not isinstance(value, bool):
        _integer(
            value, "JSON integer", minimum=-MAX_JSON_INTEGER, maximum=MAX_JSON_INTEGER
        )
        return
    if isinstance(value, str):
        _nfc_text(value, "JSON string", maximum=MAX_JSON_STRING_BYTES)
        return
    if isinstance(value, list):
        if len(value) > MAX_JSON_CONTAINER_ITEMS:
            _raise("WORKER_JSON_INVALID", "JSON array exceeds the protocol limit")
        for item in value:
            _validate_json_value(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > MAX_JSON_CONTAINER_ITEMS:
            _raise("WORKER_JSON_INVALID", "JSON object exceeds the protocol limit")
        for key, item in value.items():
            _nfc_text(key, "JSON object key", maximum=128)
            _validate_json_value(item, depth=depth + 1)
        return
    _raise("WORKER_JSON_INVALID", "JSON contains a forbidden scalar type")


def canonical_json_bytes(value: Any) -> bytes:
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
        _raise("WORKER_JSON_INVALID", "JSON cannot be encoded canonically")


def _reject_constant(_value: str) -> None:
    _raise("WORKER_JSON_INVALID", "JSON contains a forbidden numeric value")


def _reject_float(_value: str) -> None:
    _raise("WORKER_JSON_INVALID", "JSON floating-point values are forbidden")


def _parse_integer(value: str) -> int:
    try:
        parsed = int(value, 10)
    except ValueError:
        _raise("WORKER_JSON_INVALID", "JSON integer is invalid")
    return _integer(
        parsed, "JSON integer", minimum=-MAX_JSON_INTEGER, maximum=MAX_JSON_INTEGER
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            _raise("WORKER_JSON_INVALID", "JSON contains a duplicate object key")
        value[key] = item
    return value


def decode_canonical_json(raw: bytes, *, maximum: int = MAX_FRAME_BYTES) -> Any:
    if not isinstance(raw, bytes) or not raw or len(raw) > maximum:
        _raise("WORKER_FRAME_SIZE_INVALID", "Worker frame has an invalid byte length")
    try:
        value = json.loads(
            raw.decode("utf-8", "strict"),
            object_pairs_hook=_unique_object,
            parse_int=_parse_integer,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except WorkerProtocolError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        _raise("WORKER_JSON_INVALID", "Worker frame is not valid canonical JSON")
    try:
        _validate_json_value(value)
    except WorkerProtocolError as error:
        if error.code == "WORKER_JSON_INVALID":
            raise
        _raise("WORKER_JSON_INVALID", "Worker frame contains non-canonical JSON data")
    if canonical_json_bytes(value) != raw:
        _raise("WORKER_JSON_NONCANONICAL", "Worker frame is not canonical JSON")
    return value


def _fixed_read(path: str, maximum: int, code: str, message: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            raw = os.read(descriptor, maximum + 1)
        finally:
            os.close(descriptor)
    except OSError:
        _raise(code, message)
    if not raw or len(raw) > maximum:
        _raise(code, message)
    return raw


def current_host_boot_id_sha256() -> str:
    raw = _fixed_read(
        "/proc/sys/kernel/random/boot_id",
        128,
        "WORKER_HOST_BOOT_UNVERIFIED",
        "Host boot identity could not be verified",
    ).strip()
    if not re.fullmatch(
        rb"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", raw
    ):
        _raise(
            "WORKER_HOST_BOOT_UNVERIFIED", "Host boot identity could not be verified"
        )
    return hashlib.sha256(raw).hexdigest()


def process_start_time_ticks(pid: int) -> int:
    exact_pid = _integer(pid, "peer PID", minimum=1, maximum=2**31 - 1)
    raw = _fixed_read(
        f"/proc/{exact_pid}/stat",
        _PROC_READ_BYTES,
        "WORKER_PROCESS_START_UNVERIFIED",
        "Worker process start identity could not be verified",
    )
    closing = raw.rfind(b")")
    if closing <= 0:
        _raise(
            "WORKER_PROCESS_START_UNVERIFIED",
            "Worker process start identity could not be verified",
        )
    fields = raw[closing + 2 :].split()
    if len(fields) <= 19 or not fields[19].isdigit():
        _raise(
            "WORKER_PROCESS_START_UNVERIFIED",
            "Worker process start identity could not be verified",
        )
    return _integer(
        int(fields[19], 10),
        "process start time",
        minimum=1,
        maximum=MAX_PLAN_INTEGER,
    )


def process_start_token_sha256(
    host_boot_sha256: str, pid: int, start_ticks: int
) -> str:
    _sha256(host_boot_sha256, "host boot identity")
    _integer(pid, "peer PID", minimum=1, maximum=2**31 - 1)
    _integer(start_ticks, "process start time", minimum=1, maximum=MAX_PLAN_INTEGER)
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "host_boot_id_sha256": host_boot_sha256,
                "peer_pid": pid,
                "process_start_time_ticks": start_ticks,
            }
        )
    ).hexdigest()


def socket_inode_binding_sha256(
    host_boot_sha256: str,
    pid: int,
    device: int,
    inode: int,
) -> str:
    _sha256(host_boot_sha256, "host boot identity")
    _integer(pid, "peer PID", minimum=1, maximum=2**31 - 1)
    _integer(device, "socket device", minimum=0, maximum=MAX_PLAN_INTEGER)
    _integer(inode, "socket inode", minimum=1, maximum=MAX_PLAN_INTEGER)
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "host_boot_id_sha256": host_boot_sha256,
                "peer_pid": pid,
                "socket_device": device,
                "socket_inode": inode,
            }
        )
    ).hexdigest()


def _validate_metrics(value: Any, operation: str, *, expected: bool) -> dict[str, int]:
    metrics = _exact_keys(value, _OPERATION_CONTRACTS[operation]["metrics"], "metrics")
    minimum = 1 if expected else 0
    return {
        key: _integer(metrics[key], f"metric {key}", minimum=minimum, maximum=MAX_ITEMS)
        for key in sorted(metrics)
    }


def _validate_credential_descriptors(
    value: Any, operation: str, generation_id: str
) -> list[dict]:
    contract = _OPERATION_CONTRACTS[operation]
    if not isinstance(value, list) or len(value) != len(contract["scopes"]):
        _raise(
            "WORKER_CREDENTIAL_SCOPE_INVALID", "Capability descriptor count is invalid"
        )
    validated: list[dict] = []
    for index, (descriptor_value, scope) in enumerate(zip(value, contract["scopes"])):
        descriptor = _exact_keys(
            descriptor_value, _CREDENTIAL_DESCRIPTOR_KEYS, "capability descriptor"
        )
        if descriptor["fd_index"] != index or descriptor["scope"] != scope:
            _raise(
                "WORKER_CREDENTIAL_SCOPE_INVALID",
                "Capability descriptor order is invalid",
            )
        _sha256_revision(descriptor["credential_generation"], "capability generation")
        if descriptor["generation_id"] != generation_id:
            _raise(
                "WORKER_CREDENTIAL_SCOPE_INVALID",
                "Capability generation identity is invalid",
            )
        _safe_id(descriptor["target_identity"], "capability target identity")
        _integer(
            descriptor["byte_count"],
            "capability byte count",
            minimum=MIN_CAPABILITY_BYTES,
            maximum=MAX_CAPABILITY_BYTES,
        )
        validated.append(dict(descriptor))
    return validated


def _validate_targets(
    value: Any,
    operation: str,
    generation_id: str,
    descriptors: Sequence[Mapping[str, Any]],
) -> list[dict]:
    contract = _OPERATION_CONTRACTS[operation]
    if not isinstance(value, list) or len(value) != len(contract["components"]):
        _raise("WORKER_TARGET_BINDING_INVALID", "Expected target count is invalid")
    validated: list[dict] = []
    for target_value, component, descriptor in zip(
        value, contract["components"], descriptors
    ):
        target = _exact_keys(target_value, _TARGET_KEYS, "expected target")
        if (
            target["component"] != component
            or target["scope"] != descriptor["scope"]
            or target["credential_generation"] != descriptor["credential_generation"]
            or target["generation_id"] != generation_id
            or target["target_identity"] != descriptor["target_identity"]
        ):
            _raise(
                "WORKER_TARGET_BINDING_INVALID", "Expected target binding is invalid"
            )
        _safe_id(target["runtime_identity"], "target runtime identity")
        _image(
            target["runtime_image"],
            "target runtime image",
            nullable=component == "caption",
        )
        if component == "caption" and target["runtime_image"] is not None:
            _raise("WORKER_TARGET_BINDING_INVALID", "Caption target image must be null")
        if component != "caption" and target["runtime_image"] is None:
            _raise("WORKER_TARGET_BINDING_INVALID", "Runtime target image is required")
        _sha256(target["whoami_sha256"], "target whoami")
        _sha256(target["runtime_attestation_sha256"], "target runtime attestation")
        validated.append(dict(target))
    if len({canonical_json_bytes(item) for item in validated}) != len(validated):
        _raise("WORKER_TARGET_BINDING_INVALID", "Expected targets must be unique")
    return validated


def validate_request(
    value: Any, *, now_monotonic_ns: int | None = None
) -> dict[str, Any]:
    request = _exact_keys(value, _REQUEST_KEYS, "request")
    if request["schema"] != REQUEST_SCHEMA or request["protocol"] != PROTOCOL_REVISION:
        _raise(
            "WORKER_PROTOCOL_REVISION_INVALID", "Request protocol revision is invalid"
        )
    operation = request["operation"]
    if not isinstance(operation, str) or operation not in _OPERATION_CONTRACTS:
        _raise("WORKER_OPERATION_INVALID", "Worker operation is not allowed")
    contract = _OPERATION_CONTRACTS[operation]
    if (
        request["operation_revision"] != contract["revision"]
        or request["phase"] != contract["phase"]
    ):
        _raise("WORKER_OPERATION_INVALID", "Worker operation contract is invalid")

    _safe_id(request["request_id"], "request ID")
    _sha256_revision(request["job_revision"], "job revision")
    _sha256(request["reviewed_job_sha256"], "reviewed job")
    _sha256(request["approval_basis_sha256"], "approval basis")
    _sha256(request["execution_plan_sha256"], "execution plan")
    _sha256(
        request["launcher_handoff_receipt_sha256"],
        "launcher handoff receipt",
    )
    generation_id = _generation_id(request["generation_id"])
    _sha256(request["generation_binding_sha256"], "generation binding")
    _sha256_revision(request["idempotency_key"], "idempotency key")
    _safe_id(request["idempotency_ledger_identity"], "idempotency ledger identity")
    _sha256_revision(
        request["idempotency_ledger_revision"],
        "idempotency ledger revision",
    )

    execution = _exact_keys(
        request["execution_binding"], _EXECUTION_BINDING_KEYS, "execution binding"
    )
    _matched_text(execution["run_id"], "run ID", _RUN_ID_RE, 160)
    _matched_text(
        execution["correlation_id"], "correlation ID", _CORRELATION_ID_RE, 160
    )
    for key in ("owner_identity", "lease_id", "slot_id"):
        _safe_id(execution[key], key)

    worker = _exact_keys(
        request["worker_binding"], _WORKER_BINDING_KEYS, "worker binding"
    )
    _safe_id(worker["deployment_identity"], "worker deployment identity")
    _image(worker["runtime_image"], "worker runtime image")
    for key in (
        "whoami_sha256",
        "peer_attestation_sha256",
        "runtime_attestation_sha256",
        "host_boot_id_sha256",
        "process_start_token_sha256",
        "socket_inode_binding_sha256",
    ):
        _sha256(worker[key], key)
    _uid_gid(worker["peer_uid"], "worker peer UID")
    _uid_gid(worker["peer_gid"], "worker peer GID")
    _integer(worker["peer_pid"], "worker peer PID", minimum=1, maximum=2**31 - 1)
    _integer(
        worker["process_start_time_ticks"],
        "worker process start time",
        minimum=1,
        maximum=MAX_PLAN_INTEGER,
    )
    _integer(worker["socket_device"], "worker socket device", maximum=MAX_PLAN_INTEGER)
    _integer(
        worker["socket_inode"],
        "worker socket inode",
        minimum=1,
        maximum=MAX_PLAN_INTEGER,
    )
    if worker["host_boot_id_sha256"] != current_host_boot_id_sha256():
        _raise("WORKER_HOST_BOOT_MISMATCH", "Request host boot identity is stale")
    if worker["process_start_token_sha256"] != process_start_token_sha256(
        worker["host_boot_id_sha256"],
        worker["peer_pid"],
        worker["process_start_time_ticks"],
    ):
        _raise("WORKER_PROCESS_START_MISMATCH", "Worker process binding is invalid")
    if worker["socket_inode_binding_sha256"] != socket_inode_binding_sha256(
        worker["host_boot_id_sha256"],
        worker["peer_pid"],
        worker["socket_device"],
        worker["socket_inode"],
    ):
        _raise("WORKER_SOCKET_BINDING_MISMATCH", "Worker socket binding is invalid")

    input_binding = _exact_keys(
        request["input_binding"], _INPUT_BINDING_KEYS, "input binding"
    )
    _revision(input_binding["schema"], "input schema")
    _sha256(input_binding["sha256"], "input artifact")
    _integer(
        input_binding["item_count"], "input item count", minimum=1, maximum=MAX_ITEMS
    )
    _integer(
        input_binding["byte_count"],
        "input byte count",
        maximum=MAX_ARTIFACT_BYTES,
    )
    _validate_metrics(request["expected_metrics"], operation, expected=True)

    transport = _exact_keys(
        request["credential_transport"],
        _CREDENTIAL_TRANSPORT_KEYS,
        "credential transport",
    )
    if (
        transport["kind"] != CREDENTIAL_TRANSPORT
        or transport["json_contains_credential_bytes"] is not False
    ):
        _raise("WORKER_CREDENTIAL_SCOPE_INVALID", "Capability transport is invalid")
    descriptors = _validate_credential_descriptors(
        transport["descriptors"], operation, generation_id
    )
    _validate_targets(
        request["expected_targets"], operation, generation_id, descriptors
    )

    deadline = _integer(
        request["deadline_monotonic_ns"],
        "absolute monotonic deadline",
        minimum=1,
        maximum=MAX_JSON_INTEGER,
    )
    now = (
        time.monotonic_ns()
        if now_monotonic_ns is None
        else _integer(
            now_monotonic_ns, "current monotonic time", maximum=MAX_JSON_INTEGER
        )
    )
    if deadline <= now:
        _raise("WORKER_DEADLINE_EXCEEDED", "Worker request deadline has expired")
    if deadline - now > MAX_DEADLINE_AHEAD_NS:
        _raise(
            "WORKER_DEADLINE_INVALID",
            "Worker request deadline exceeds the allowed horizon",
        )
    return json.loads(canonical_json_bytes(request).decode("utf-8"))


def _validate_control_credential_descriptor(
    value: Any,
    operation: str,
    generation_id: str,
    worker_deployment_identity: str,
) -> dict[str, Any]:
    transport = _exact_keys(value, _CREDENTIAL_TRANSPORT_KEYS, "credential transport")
    if (
        transport["kind"] != CREDENTIAL_TRANSPORT
        or transport["json_contains_credential_bytes"] is not False
        or not isinstance(transport["descriptors"], list)
        or len(transport["descriptors"]) != 1
    ):
        _raise(
            "WORKER_CREDENTIAL_SCOPE_INVALID", "Control capability transport is invalid"
        )
    descriptor = _exact_keys(
        transport["descriptors"][0],
        _CREDENTIAL_DESCRIPTOR_KEYS,
        "control capability descriptor",
    )
    if (
        descriptor["fd_index"] != 0
        or descriptor["scope"] != _CONTROL_OPERATION_CONTRACTS[operation]["scope"]
        or descriptor["generation_id"] != generation_id
        or descriptor["target_identity"] != worker_deployment_identity
    ):
        _raise(
            "WORKER_CREDENTIAL_SCOPE_INVALID", "Control capability binding is invalid"
        )
    _sha256_revision(
        descriptor["credential_generation"], "control capability generation"
    )
    _integer(
        descriptor["byte_count"],
        "control capability byte count",
        minimum=MIN_CAPABILITY_BYTES,
        maximum=MAX_CAPABILITY_BYTES,
    )
    return dict(descriptor)


def validate_control_request(
    value: Any,
    *,
    now_monotonic_ns: int | None = None,
) -> dict[str, Any]:
    """Validate one of four closed recovery operations; no command field exists."""

    request = _exact_keys(value, _CONTROL_REQUEST_KEYS, "control request")
    if (
        request["schema"] != CONTROL_REQUEST_SCHEMA
        or request["protocol"] != PROTOCOL_REVISION
    ):
        _raise(
            "WORKER_PROTOCOL_REVISION_INVALID", "Control request revision is invalid"
        )
    operation = request["operation"]
    if not isinstance(operation, str) or operation not in _CONTROL_OPERATION_CONTRACTS:
        _raise("WORKER_OPERATION_INVALID", "Worker control operation is not allowed")
    contract = _CONTROL_OPERATION_CONTRACTS[operation]
    if request["operation_revision"] != contract["revision"]:
        _raise(
            "WORKER_OPERATION_INVALID", "Worker control operation contract is invalid"
        )

    _safe_id(request["request_id"], "control request ID")
    _sha256_revision(request["job_revision"], "job revision")
    _sha256(request["reviewed_job_sha256"], "reviewed job")
    _sha256(request["approval_basis_sha256"], "approval basis")
    _sha256(request["execution_plan_sha256"], "execution plan")
    _sha256(
        request["launcher_handoff_receipt_sha256"],
        "launcher handoff receipt",
    )
    generation_id = _generation_id(request["generation_id"])
    _sha256(request["generation_binding_sha256"], "generation binding")
    if request["target_phase"] not in _PHASES:
        _raise("WORKER_OPERATION_INVALID", "Control target phase is not allowed")
    _sha256(request["phase_request_sha256"], "original phase request")
    _sha256_revision(request["phase_idempotency_key"], "phase idempotency key")
    _sha256_revision(request["control_idempotency_key"], "control idempotency key")
    _safe_id(request["idempotency_ledger_identity"], "idempotency ledger identity")
    _sha256_revision(
        request["idempotency_ledger_revision"],
        "idempotency ledger revision",
    )

    execution = _exact_keys(
        request["execution_binding"], _EXECUTION_BINDING_KEYS, "execution binding"
    )
    _matched_text(execution["run_id"], "run ID", _RUN_ID_RE, 160)
    _matched_text(
        execution["correlation_id"], "correlation ID", _CORRELATION_ID_RE, 160
    )
    for key in ("owner_identity", "lease_id", "slot_id"):
        _safe_id(execution[key], key)

    worker = _exact_keys(
        request["worker_binding"], _WORKER_BINDING_KEYS, "worker binding"
    )
    deployment_identity = _safe_id(
        worker["deployment_identity"], "worker deployment identity"
    )
    _image(worker["runtime_image"], "worker runtime image")
    for key in (
        "whoami_sha256",
        "peer_attestation_sha256",
        "runtime_attestation_sha256",
        "host_boot_id_sha256",
        "process_start_token_sha256",
        "socket_inode_binding_sha256",
    ):
        _sha256(worker[key], key)
    _uid_gid(worker["peer_uid"], "worker peer UID")
    _uid_gid(worker["peer_gid"], "worker peer GID")
    _integer(worker["peer_pid"], "worker peer PID", minimum=1, maximum=2**31 - 1)
    _integer(
        worker["process_start_time_ticks"],
        "worker process start time",
        minimum=1,
        maximum=MAX_PLAN_INTEGER,
    )
    _integer(worker["socket_device"], "worker socket device", maximum=MAX_PLAN_INTEGER)
    _integer(
        worker["socket_inode"],
        "worker socket inode",
        minimum=1,
        maximum=MAX_PLAN_INTEGER,
    )
    if worker["host_boot_id_sha256"] != current_host_boot_id_sha256():
        _raise(
            "WORKER_HOST_BOOT_MISMATCH", "Control request host boot identity is stale"
        )
    if worker["process_start_token_sha256"] != process_start_token_sha256(
        worker["host_boot_id_sha256"],
        worker["peer_pid"],
        worker["process_start_time_ticks"],
    ):
        _raise(
            "WORKER_PROCESS_START_MISMATCH", "Control worker process binding is invalid"
        )
    if worker["socket_inode_binding_sha256"] != socket_inode_binding_sha256(
        worker["host_boot_id_sha256"],
        worker["peer_pid"],
        worker["socket_device"],
        worker["socket_inode"],
    ):
        _raise(
            "WORKER_SOCKET_BINDING_MISMATCH", "Control worker socket binding is invalid"
        )

    _validate_control_credential_descriptor(
        request["credential_transport"],
        operation,
        generation_id,
        deployment_identity,
    )
    deadline = _integer(
        request["deadline_monotonic_ns"],
        "absolute monotonic deadline",
        minimum=1,
        maximum=MAX_JSON_INTEGER,
    )
    now = (
        time.monotonic_ns()
        if now_monotonic_ns is None
        else _integer(
            now_monotonic_ns,
            "current monotonic time",
            maximum=MAX_JSON_INTEGER,
        )
    )
    if deadline <= now:
        _raise("WORKER_DEADLINE_EXCEEDED", "Worker control deadline has expired")
    if deadline - now > MAX_DEADLINE_AHEAD_NS:
        _raise(
            "WORKER_DEADLINE_INVALID",
            "Worker control deadline exceeds the allowed horizon",
        )
    return json.loads(canonical_json_bytes(request).decode("utf-8"))


def _request_binding_from_validated(request: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "request_sha256": hashlib.sha256(canonical_json_bytes(request)).hexdigest(),
        "request_id": request["request_id"],
        "operation": request["operation"],
        "phase": request["phase"],
        "idempotency_key": request["idempotency_key"],
        "idempotency_ledger_identity": request["idempotency_ledger_identity"],
        "idempotency_ledger_revision": request["idempotency_ledger_revision"],
        "job_revision": request["job_revision"],
        "reviewed_job_sha256": request["reviewed_job_sha256"],
        "approval_basis_sha256": request["approval_basis_sha256"],
        "execution_plan_sha256": request["execution_plan_sha256"],
        "launcher_handoff_receipt_sha256": request[
            "launcher_handoff_receipt_sha256"
        ],
        "generation_id": request["generation_id"],
        "generation_binding_sha256": request["generation_binding_sha256"],
        "host_boot_id_sha256": request["worker_binding"]["host_boot_id_sha256"],
        "deadline_monotonic_ns": request["deadline_monotonic_ns"],
    }


def request_binding_for(request: Mapping[str, Any]) -> dict[str, Any]:
    return _request_binding_from_validated(validate_request(request))


def _control_request_binding_from_validated(
    request: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "request_sha256": hashlib.sha256(canonical_json_bytes(request)).hexdigest(),
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
        "launcher_handoff_receipt_sha256": request[
            "launcher_handoff_receipt_sha256"
        ],
        "generation_id": request["generation_id"],
        "generation_binding_sha256": request["generation_binding_sha256"],
        "host_boot_id_sha256": request["worker_binding"]["host_boot_id_sha256"],
        "deadline_monotonic_ns": request["deadline_monotonic_ns"],
    }


def control_request_binding_for(request: Mapping[str, Any]) -> dict[str, Any]:
    return _control_request_binding_from_validated(validate_control_request(request))


def _parse_timestamp(value: Any, label: str) -> dt.datetime:
    if not isinstance(value, str) or not _RFC3339_RE.fullmatch(value):
        _raise("WORKER_ARTIFACT_INVALID", f"{label} is not RFC3339 time")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _raise("WORKER_ARTIFACT_INVALID", f"{label} is not valid time")
    if parsed.tzinfo is None:
        _raise("WORKER_ARTIFACT_INVALID", f"{label} has no timezone")
    return parsed.astimezone(dt.timezone.utc)


def _validate_remote_error(
    value: Any,
    mutation_state: str,
    allowed_dependencies: frozenset[str],
) -> dict[str, Any]:
    error = _exact_keys(value, _REMOTE_ERROR_KEYS, "worker error")
    pair = (error["dependency"], error["code"])
    if pair not in _REMOTE_ERROR_STATE_RETRY:
        _raise("WORKER_ERROR_INVALID", "Worker error identity is not allowed")
    if error["dependency"] not in allowed_dependencies:
        _raise(
            "WORKER_ERROR_INVALID", "Worker error dependency does not match operation"
        )
    if not isinstance(error["retryable"], bool):
        _raise("WORKER_ERROR_INVALID", "Worker retryability is invalid")
    if error["mutation_state"] != mutation_state:
        _raise("WORKER_ERROR_INVALID", "Worker error mutation state is inconsistent")
    if (mutation_state, error["retryable"]) not in _REMOTE_ERROR_STATE_RETRY[pair]:
        _raise(
            "WORKER_ERROR_INVALID",
            "Worker error state/retry combination is not allowed",
        )
    return dict(error)


def _validate_worker_attestation(
    value: Any,
    request: Mapping[str, Any],
) -> dict[str, Any]:
    attestation = _exact_keys(
        value, _WORKER_ATTESTATION_KEYS, "worker artifact attestation"
    )
    worker = request["worker_binding"]
    expected = {
        "deployment_identity": worker["deployment_identity"],
        "runtime_image": worker["runtime_image"],
        "whoami_sha256": worker["whoami_sha256"],
        "peer_attestation_sha256": worker["peer_attestation_sha256"],
        "runtime_attestation_sha256": worker["runtime_attestation_sha256"],
        "host_boot_id_sha256": worker["host_boot_id_sha256"],
        "process_start_token_sha256": worker["process_start_token_sha256"],
        "socket_inode_binding_sha256": worker["socket_inode_binding_sha256"],
    }
    if canonical_json_bytes(attestation) != canonical_json_bytes(expected):
        _raise(
            "WORKER_RESPONSE_BINDING_MISMATCH",
            "Worker attestation does not match request",
        )
    return dict(attestation)


def _validate_artifact(
    value: Any,
    request: Mapping[str, Any],
    *,
    now_utc: dt.datetime,
) -> dict[str, Any]:
    artifact = _exact_keys(value, _ARTIFACT_KEYS, "worker artifact")
    request_binding = _request_binding_from_validated(request)
    expected = {
        "schema": ARTIFACT_ROOT_SCHEMA,
        "protocol": PROTOCOL_REVISION,
        "phase": request["phase"],
        "operation": request["operation"],
        "request_sha256": request_binding["request_sha256"],
        "job_revision": request["job_revision"],
        "reviewed_job_sha256": request["reviewed_job_sha256"],
        "approval_basis_sha256": request["approval_basis_sha256"],
        "execution_plan_sha256": request["execution_plan_sha256"],
        "generation_id": request["generation_id"],
        "generation_binding_sha256": request["generation_binding_sha256"],
        "idempotency_key": request["idempotency_key"],
        "idempotency_ledger_identity": request["idempotency_ledger_identity"],
        "idempotency_ledger_revision": request["idempotency_ledger_revision"],
        "input_artifact_sha256": request["input_binding"]["sha256"],
    }
    for key, expected_value in expected.items():
        if artifact[key] != expected_value:
            _raise(
                "WORKER_RESPONSE_BINDING_MISMATCH",
                "Worker artifact does not match request",
            )

    root = _exact_keys(artifact["artifact_root"], _ARTIFACT_ROOT_KEYS, "artifact root")
    if root["schema"] != _OPERATION_CONTRACTS[request["operation"]]["artifact_schema"]:
        _raise("WORKER_ARTIFACT_INVALID", "Worker artifact schema does not match phase")
    _sha256(root["sha256"], "artifact root")
    byte_count = _integer(
        root["byte_count"], "artifact byte count", minimum=1, maximum=MAX_ARTIFACT_BYTES
    )
    chunk_count = _integer(
        root["chunk_count"], "artifact chunk count", minimum=1, maximum=MAX_CHUNKS
    )
    if byte_count < chunk_count:
        _raise(
            "WORKER_ARTIFACT_INVALID", "Worker artifact chunk manifest is inconsistent"
        )
    _sha256(root["chunks_sha256"], "artifact chunks manifest")

    observed_targets = _validate_targets(
        artifact["observed_targets"],
        request["operation"],
        request["generation_id"],
        request["credential_transport"]["descriptors"],
    )
    if canonical_json_bytes(observed_targets) != canonical_json_bytes(
        request["expected_targets"]
    ):
        _raise(
            "WORKER_RESPONSE_BINDING_MISMATCH", "Observed targets do not match request"
        )
    _validate_worker_attestation(artifact["worker_attestation"], request)
    if artifact["serialization_contract"] != _SERIALIZATION_CONTRACT:
        _raise(
            "WORKER_ARTIFACT_INVALID",
            "Worker artifact serialization contract is invalid",
        )

    issued_at = _parse_timestamp(artifact["issued_at"], "artifact issue time")
    expires_at = _parse_timestamp(artifact["expires_at"], "artifact expiry time")
    if not isinstance(now_utc, dt.datetime) or now_utc.tzinfo is None:
        _raise("WORKER_ARTIFACT_INVALID", "Artifact validation clock is invalid")
    now = now_utc.astimezone(dt.timezone.utc)
    if issued_at > now + dt.timedelta(seconds=MAX_CLOCK_SKEW_SECONDS):
        _raise("WORKER_ARTIFACT_STALE", "Worker artifact issue time is in the future")
    if expires_at <= now:
        _raise("WORKER_ARTIFACT_STALE", "Worker artifact has expired")
    if expires_at <= issued_at or (expires_at - issued_at) > dt.timedelta(
        seconds=MAX_EVIDENCE_TTL_SECONDS
    ):
        _raise("WORKER_ARTIFACT_STALE", "Worker artifact validity window is invalid")
    return dict(artifact)


def validate_response(
    value: Any,
    request: Mapping[str, Any],
    *,
    now_utc: dt.datetime | None = None,
) -> dict[str, Any]:
    validated_request = validate_request(request)
    result = _exact_keys(value, _RESULT_KEYS, "worker result")
    if result["schema"] != RESULT_SCHEMA or result["protocol"] != PROTOCOL_REVISION:
        _raise(
            "WORKER_PROTOCOL_REVISION_INVALID", "Response protocol revision is invalid"
        )
    binding = _exact_keys(
        result["request_binding"], _REQUEST_BINDING_KEYS, "request binding"
    )
    if canonical_json_bytes(binding) != canonical_json_bytes(
        _request_binding_from_validated(validated_request)
    ):
        _raise(
            "WORKER_RESPONSE_BINDING_MISMATCH", "Worker response does not match request"
        )
    if result["status"] not in {"succeeded", "failed"}:
        _raise("WORKER_RESPONSE_STATUS_INVALID", "Worker response status is invalid")
    mutation_state = result["mutation_state"]
    if mutation_state not in _MUTATION_STATES:
        _raise("WORKER_RESPONSE_STATUS_INVALID", "Worker mutation state is invalid")
    metrics = _validate_metrics(
        result["metrics"], validated_request["operation"], expected=False
    )
    if result["status"] == "succeeded":
        if result["error"] is not None or result["artifact"] is None:
            _raise(
                "WORKER_RESPONSE_STATUS_INVALID",
                "Successful worker response shape is invalid",
            )
        expected_state = _OPERATION_CONTRACTS[validated_request["operation"]][
            "success_mutation_state"
        ]
        if mutation_state != expected_state:
            _raise(
                "WORKER_RESPONSE_STATUS_INVALID",
                "Successful worker mutation state is invalid",
            )
        if canonical_json_bytes(metrics) != canonical_json_bytes(
            validated_request["expected_metrics"]
        ):
            _raise(
                "WORKER_RESPONSE_BINDING_MISMATCH",
                "Worker metrics do not match request",
            )
        _validate_artifact(
            result["artifact"],
            validated_request,
            now_utc=now_utc or dt.datetime.now(dt.timezone.utc),
        )
    else:
        if result["artifact"] is not None or result["error"] is None:
            _raise(
                "WORKER_RESPONSE_STATUS_INVALID",
                "Failed worker response shape is invalid",
            )
        if (
            mutation_state
            not in _OPERATION_CONTRACTS[validated_request["operation"]][
                "failure_mutation_states"
            ]
        ):
            _raise(
                "WORKER_RESPONSE_STATUS_INVALID",
                "Failed worker mutation state is invalid",
            )
        _validate_remote_error(
            result["error"],
            mutation_state,
            _OPERATION_CONTRACTS[validated_request["operation"]]["error_dependencies"],
        )
    return json.loads(canonical_json_bytes(result).decode("utf-8"))


def _validate_control_receipt(
    value: Any,
    request: Mapping[str, Any],
    *,
    now_utc: dt.datetime,
) -> dict[str, Any]:
    receipt = _exact_keys(value, _CONTROL_RECEIPT_KEYS, "control receipt")
    expected = {
        "schema": CONTROL_RECEIPT_SCHEMA,
        "operation": request["operation"],
        "generation_id": request["generation_id"],
        "target_phase": request["target_phase"],
        # This is deliberately the digest of the original ordinary request,
        # not the digest of the recovery request.
        "phase_request_sha256": request["phase_request_sha256"],
        "phase_idempotency_key": request["phase_idempotency_key"],
        "control_idempotency_key": request["control_idempotency_key"],
    }
    if any(receipt[key] != expected_value for key, expected_value in expected.items()):
        _raise(
            "WORKER_RESPONSE_BINDING_MISMATCH", "Control receipt does not match request"
        )
    if receipt["observed_phase_state"] not in _MUTATION_STATES:
        _raise("WORKER_ARTIFACT_INVALID", "Control receipt phase state is invalid")
    immutable_receipt = receipt["immutable_phase_receipt_sha256"]
    if immutable_receipt is not None:
        _sha256(immutable_receipt, "immutable phase receipt")
    if not isinstance(receipt["cancelled"], bool) or not isinstance(
        receipt["quarantined"], bool
    ):
        _raise("WORKER_ARTIFACT_INVALID", "Control receipt outcome flags are invalid")

    operation = request["operation"]
    expected_flags = {
        "query_phase_status": (False, False),
        "recover_phase_receipt": (False, False),
        "cancel_phase_work": (True, False),
        "quarantine_generation": (False, True),
    }[operation]
    if (receipt["cancelled"], receipt["quarantined"]) != expected_flags:
        _raise(
            "WORKER_ARTIFACT_INVALID",
            "Control receipt outcome does not match operation",
        )
    if operation == "recover_phase_receipt" and immutable_receipt is None:
        _raise("WORKER_ARTIFACT_INVALID", "Recovered immutable receipt is required")

    issued_at = _parse_timestamp(receipt["issued_at"], "control receipt issue time")
    expires_at = _parse_timestamp(receipt["expires_at"], "control receipt expiry time")
    if not isinstance(now_utc, dt.datetime) or now_utc.tzinfo is None:
        _raise("WORKER_ARTIFACT_INVALID", "Control receipt validation clock is invalid")
    now = now_utc.astimezone(dt.timezone.utc)
    if issued_at > now + dt.timedelta(seconds=MAX_CLOCK_SKEW_SECONDS):
        _raise("WORKER_ARTIFACT_STALE", "Control receipt issue time is in the future")
    if expires_at <= now:
        _raise("WORKER_ARTIFACT_STALE", "Control receipt has expired")
    if expires_at <= issued_at or (expires_at - issued_at) > dt.timedelta(
        seconds=MAX_EVIDENCE_TTL_SECONDS
    ):
        _raise("WORKER_ARTIFACT_STALE", "Control receipt validity window is invalid")
    return dict(receipt)


def validate_control_response(
    value: Any,
    request: Mapping[str, Any],
    *,
    now_utc: dt.datetime | None = None,
) -> dict[str, Any]:
    validated_request = validate_control_request(request)
    result = _exact_keys(value, _CONTROL_RESULT_KEYS, "control result")
    if (
        result["schema"] != CONTROL_RESULT_SCHEMA
        or result["protocol"] != PROTOCOL_REVISION
    ):
        _raise(
            "WORKER_PROTOCOL_REVISION_INVALID", "Control response revision is invalid"
        )
    binding = _exact_keys(
        result["request_binding"],
        _CONTROL_REQUEST_BINDING_KEYS,
        "control request binding",
    )
    if canonical_json_bytes(binding) != canonical_json_bytes(
        _control_request_binding_from_validated(validated_request)
    ):
        _raise(
            "WORKER_RESPONSE_BINDING_MISMATCH",
            "Control response does not match request",
        )
    if result["status"] not in {"succeeded", "failed"}:
        _raise("WORKER_RESPONSE_STATUS_INVALID", "Control response status is invalid")
    mutation_state = result["mutation_state"]
    if mutation_state not in _MUTATION_STATES:
        _raise("WORKER_RESPONSE_STATUS_INVALID", "Control mutation state is invalid")
    contract = _CONTROL_OPERATION_CONTRACTS[validated_request["operation"]]
    if result["status"] == "succeeded":
        if result["error"] is not None or result["receipt"] is None:
            _raise(
                "WORKER_RESPONSE_STATUS_INVALID",
                "Successful control response is invalid",
            )
        if mutation_state != contract["success_mutation_state"]:
            _raise(
                "WORKER_RESPONSE_STATUS_INVALID", "Successful control state is invalid"
            )
        _validate_control_receipt(
            result["receipt"],
            validated_request,
            now_utc=now_utc or dt.datetime.now(dt.timezone.utc),
        )
    else:
        if result["receipt"] is not None or result["error"] is None:
            _raise(
                "WORKER_RESPONSE_STATUS_INVALID", "Failed control response is invalid"
            )
        if mutation_state not in contract["failure_mutation_states"]:
            _raise("WORKER_RESPONSE_STATUS_INVALID", "Failed control state is invalid")
        _validate_remote_error(
            result["error"],
            mutation_state,
            frozenset({"worker", "evidence_store"}),
        )
    return json.loads(canonical_json_bytes(result).decode("utf-8"))


def _remaining_nanoseconds(deadline_monotonic_ns: int, *, mutation_state: str) -> int:
    remaining = deadline_monotonic_ns - time.monotonic_ns()
    if remaining <= 0:
        _raise(
            "WORKER_DEADLINE_EXCEEDED",
            "Worker request exceeded its absolute deadline",
            retryable=mutation_state == "none",
            mutation_state=mutation_state,
        )
    return remaining


def _wait_socket_ready(
    sock: socket.socket,
    event: int,
    deadline_monotonic_ns: int,
    *,
    mutation_state: str,
) -> None:
    """Wait without mutating O_NONBLOCK or the socket object's timeout state."""

    if not hasattr(socket, "MSG_DONTWAIT") or not hasattr(select, "poll"):
        _raise(
            "WORKER_PLATFORM_UNSUPPORTED", "Bounded nonblocking UDS I/O is unavailable"
        )
    try:
        descriptor = sock.fileno()
    except OSError:
        _raise(
            "WORKER_SOCKET_INVALID",
            "Worker socket is not usable",
            mutation_state=mutation_state,
        )
    if descriptor < 0:
        _raise(
            "WORKER_SOCKET_INVALID",
            "Worker socket is closed",
            mutation_state=mutation_state,
        )
    poller = select.poll()
    poller.register(
        descriptor,
        event | select.POLLERR | select.POLLHUP | select.POLLNVAL,
    )
    while True:
        remaining = _remaining_nanoseconds(
            deadline_monotonic_ns,
            mutation_state=mutation_state,
        )
        timeout_ms = max(1, (remaining + 999_999) // 1_000_000)
        try:
            ready = poller.poll(timeout_ms)
        except InterruptedError:
            continue
        except OSError:
            _raise(
                "WORKER_SOCKET_INVALID",
                "Worker socket readiness could not be checked",
                mutation_state=mutation_state,
            )
        if not ready:
            continue
        flags = ready[0][1]
        if flags & select.POLLNVAL:
            _raise(
                "WORKER_SOCKET_INVALID",
                "Worker socket became invalid",
                mutation_state=mutation_state,
            )
        # HUP is readable EOF; ERR/HUP is also allowed through to send/recv so
        # the syscall determines the exact bounded transport outcome.
        if flags & (event | select.POLLERR | select.POLLHUP):
            _remaining_nanoseconds(
                deadline_monotonic_ns,
                mutation_state=mutation_state,
            )
            return


def _peer_credentials(sock: socket.socket) -> tuple[int, int, int]:
    if not hasattr(socket, "SO_PEERCRED"):
        _raise("WORKER_PLATFORM_UNSUPPORTED", "Unix peer credentials are unavailable")
    try:
        raw = sock.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
        )
        pid, uid, gid = struct.unpack("3i", raw)
    except (OSError, struct.error):
        _raise(
            "WORKER_PEER_UNVERIFIED", "Worker peer credentials could not be verified"
        )
    return pid, uid, gid


def _require_unix_stream(sock: socket.socket) -> None:
    if not isinstance(sock, socket.socket) or sock.family != socket.AF_UNIX:
        _raise("WORKER_SOCKET_INVALID", "Worker connection is not an AF_UNIX socket")
    try:
        socket_type = sock.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE)
        sock.getpeername()
    except OSError:
        _raise("WORKER_SOCKET_INVALID", "Worker connection is not connected")
    if socket_type != socket.SOCK_STREAM:
        _raise("WORKER_SOCKET_INVALID", "Worker connection is not a stream socket")


def verify_peer(
    sock: socket.socket, policy: PeerPolicy, request: Mapping[str, Any]
) -> None:
    if not isinstance(policy, PeerPolicy):
        _raise("WORKER_PEER_UNVERIFIED", "Worker peer policy is invalid")
    _require_unix_stream(sock)
    worker = request["worker_binding"]
    expected_policy = {
        "peer_uid": policy.expected_uid,
        "peer_gid": policy.expected_gid,
        "peer_pid": policy.expected_pid,
        "process_start_time_ticks": policy.expected_process_start_time_ticks,
        "socket_device": policy.expected_socket_device,
        "socket_inode": policy.expected_socket_inode,
        "host_boot_id_sha256": policy.host_boot_id_sha256,
        "peer_attestation_sha256": policy.peer_attestation_sha256,
    }
    if any(worker[key] != expected for key, expected in expected_policy.items()):
        _raise(
            "WORKER_PEER_ATTESTATION_MISMATCH",
            "Worker peer policy does not match request",
        )
    if current_host_boot_id_sha256() != policy.host_boot_id_sha256:
        _raise("WORKER_HOST_BOOT_MISMATCH", "Worker host boot identity changed")
    pid, uid, gid = _peer_credentials(sock)
    if (pid, uid, gid) != (
        policy.expected_pid,
        policy.expected_uid,
        policy.expected_gid,
    ):
        _raise(
            "WORKER_PEER_CREDENTIAL_MISMATCH",
            "Worker peer credentials do not match policy",
        )
    if process_start_time_ticks(pid) != policy.expected_process_start_time_ticks:
        _raise("WORKER_PROCESS_START_MISMATCH", "Worker process start identity changed")
    try:
        metadata = os.fstat(sock.fileno())
    except OSError:
        _raise("WORKER_SOCKET_INVALID", "Worker socket could not be inspected")
    if not stat.S_ISSOCK(metadata.st_mode):
        _raise("WORKER_SOCKET_INVALID", "Worker descriptor is not a socket")
    if (metadata.st_dev, metadata.st_ino) != (
        policy.expected_socket_device,
        policy.expected_socket_inode,
    ):
        _raise("WORKER_SOCKET_BINDING_MISMATCH", "Worker socket identity changed")
    if worker["process_start_token_sha256"] != process_start_token_sha256(
        policy.host_boot_id_sha256,
        pid,
        policy.expected_process_start_time_ticks,
    ):
        _raise("WORKER_PROCESS_START_MISMATCH", "Worker process token is invalid")
    if worker["socket_inode_binding_sha256"] != socket_inode_binding_sha256(
        policy.host_boot_id_sha256,
        pid,
        metadata.st_dev,
        metadata.st_ino,
    ):
        _raise("WORKER_SOCKET_BINDING_MISMATCH", "Worker socket token is invalid")


def verify_client_peer(sock: socket.socket, policy: ClientPeerPolicy) -> None:
    """Verify the launcher/origin transport peer and accepted endpoint.

    The coordinator may own the handed-off descriptor at call time, but Linux
    ``SO_PEERCRED`` on this accepted endpoint remains the identity that
    originated the connection.
    """

    if not isinstance(policy, ClientPeerPolicy):
        _raise("WORKER_PEER_UNVERIFIED", "Launcher/origin peer policy is invalid")
    _require_unix_stream(sock)
    if current_host_boot_id_sha256() != policy.host_boot_id_sha256:
        _raise(
            "WORKER_HOST_BOOT_MISMATCH",
            "Launcher/origin host boot identity changed",
        )
    pid, uid, gid = _peer_credentials(sock)
    if (pid, uid, gid) != (
        policy.expected_pid,
        policy.expected_uid,
        policy.expected_gid,
    ):
        _raise(
            "WORKER_PEER_CREDENTIAL_MISMATCH",
            "Launcher/origin peer credentials do not match policy",
        )
    if process_start_time_ticks(pid) != policy.expected_process_start_time_ticks:
        _raise(
            "WORKER_PROCESS_START_MISMATCH",
            "Launcher/origin process start identity changed",
        )
    try:
        metadata = os.fstat(sock.fileno())
    except OSError:
        _raise("WORKER_SOCKET_INVALID", "Accepted worker socket could not be inspected")
    if not stat.S_ISSOCK(metadata.st_mode):
        _raise("WORKER_SOCKET_INVALID", "Accepted worker descriptor is not a socket")
    if (metadata.st_dev, metadata.st_ino) != (
        policy.expected_socket_device,
        policy.expected_socket_inode,
    ):
        _raise(
            "WORKER_SOCKET_BINDING_MISMATCH", "Accepted worker socket identity changed"
        )


def _memfd_api_available() -> bool:
    return all(
        hasattr(os, name)
        for name in ("memfd_create", "MFD_CLOEXEC", "MFD_ALLOW_SEALING")
    ) and all(
        hasattr(fcntl, name)
        for name in (
            "F_ADD_SEALS",
            "F_GET_SEALS",
            "F_SEAL_SEAL",
            "F_SEAL_SHRINK",
            "F_SEAL_GROW",
            "F_SEAL_WRITE",
        )
    )


def _required_seals() -> int:
    if not _memfd_api_available():
        _raise(
            "WORKER_PROTOCOL_MEMFD_UNAVAILABLE",
            "Sealed capability transport is unavailable in this runtime",
        )
    return (
        fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE
    )


def _sealed_capability_fd(capability: bytes) -> int:
    if (
        not isinstance(capability, bytes)
        or not MIN_CAPABILITY_BYTES <= len(capability) <= MAX_CAPABILITY_BYTES
    ):
        _raise(
            "WORKER_CAPABILITY_INVALID", "Worker capability has an invalid byte length"
        )
    seals = _required_seals()
    descriptor: int | None = None
    try:
        descriptor = os.memfd_create(
            "simworld-worker-capability",
            os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING,
        )
        offset = 0
        while offset < len(capability):
            written = os.write(descriptor, capability[offset:])
            if written <= 0:
                raise OSError
            offset += written
        os.lseek(descriptor, 0, os.SEEK_SET)
        fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS, seals)
        return descriptor
    except OSError:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        _raise(
            "WORKER_CAPABILITY_TRANSPORT_FAILED",
            "Capability transport could not be prepared",
        )


def _sealed_capability_fds(capabilities: Sequence[bytes]) -> list[int]:
    descriptors: list[int] = []
    try:
        for capability in capabilities:
            descriptors.append(_sealed_capability_fd(capability))
        return descriptors
    except BaseException:
        for descriptor in descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise


def _capability_markers(capabilities: Sequence[bytes]) -> frozenset[bytes]:
    markers: set[bytes] = set()
    for capability in capabilities:
        digest = hashlib.sha256(capability)
        digest_bytes = digest.digest()
        for material in (capability, digest_bytes):
            standard = base64.b64encode(material)
            urlsafe = base64.urlsafe_b64encode(material)
            hexadecimal = material.hex().encode("ascii")
            markers.update(
                {
                    material,
                    standard,
                    standard.rstrip(b"="),
                    urlsafe,
                    urlsafe.rstrip(b"="),
                    hexadecimal,
                    hexadecimal.upper(),
                }
            )
        hexadecimal = digest.hexdigest().encode("ascii")
        markers.update({hexadecimal, hexadecimal.upper()})
    return frozenset(marker for marker in markers if marker)


def _percent_decoded_views(
    raw: bytes,
    *,
    code: str,
    mutation_state: str,
) -> tuple[bytes, ...]:
    """Decode the complete bounded frame, including mixed-case/partial escapes."""

    views = [raw]
    current = raw
    for _unused in range(_MAX_PERCENT_DECODE_PASSES):
        decoded = unquote_to_bytes(current)
        if decoded == current:
            break
        views.append(decoded)
        current = decoded
    else:
        if re.search(br"%[0-9A-Fa-f]{2}", current):
            _raise(
                code,
                "Worker message used excessively nested percent encoding",
                mutation_state=mutation_state,
            )
    return tuple(views)


def _assert_no_capability_transforms(
    raw: bytes,
    capabilities: Sequence[bytes],
    *,
    code: str = "WORKER_RESPONSE_CAPABILITY_LEAK",
    mutation_state: str = "ambiguous",
) -> None:
    if not isinstance(raw, bytes):
        _raise("WORKER_PROTOCOL_VALUE_INVALID", "Worker message bytes are invalid")
    markers = _capability_markers(capabilities)
    if any(
        marker in view
        for view in _percent_decoded_views(
            raw,
            code=code,
            mutation_state=mutation_state,
        )
        for marker in markers
    ):
        _raise(
            code,
            "Worker message contained protected capability material",
            mutation_state=mutation_state,
        )


_RECV_FLAGS = getattr(socket, "MSG_DONTWAIT", 0)
_SEND_FLAGS = getattr(socket, "MSG_DONTWAIT", 0) | getattr(socket, "MSG_NOSIGNAL", 0)


def _send_framed_bytes(
    sock: socket.socket,
    payload: bytes,
    deadline_monotonic_ns: int,
    *,
    capability_fds: Sequence[int] = (),
    started_mutation_state: str,
) -> None:
    if not hasattr(socket, "MSG_NOSIGNAL"):
        _raise("WORKER_PLATFORM_UNSUPPORTED", "Signal-safe UDS writes are unavailable")
    if not payload or len(payload) > MAX_FRAME_BYTES:
        _raise(
            "WORKER_FRAME_SIZE_INVALID",
            "Worker frame exceeds the fixed byte limit",
            mutation_state="none",
        )
    frame = struct.pack("!I", len(payload)) + payload
    ancillary = (
        [
            (
                socket.SOL_SOCKET,
                socket.SCM_RIGHTS,
                array.array("i", capability_fds).tobytes(),
            )
        ]
        if capability_fds
        else []
    )
    offset = 0
    while offset < len(frame):
        state = "none" if offset == 0 else started_mutation_state
        _wait_socket_ready(
            sock,
            select.POLLOUT,
            deadline_monotonic_ns,
            mutation_state=state,
        )
        try:
            if offset == 0 and ancillary:
                sent = sock.sendmsg([frame], ancillary, _SEND_FLAGS)
            else:
                sent = sock.send(frame[offset:], _SEND_FLAGS)
        except (BlockingIOError, InterruptedError, TimeoutError):
            continue
        except OSError:
            _raise(
                "WORKER_SEND_FAILED",
                "Worker frame transmission failed",
                retryable=state == "none",
                mutation_state=state,
            )
        if sent <= 0:
            _raise(
                "WORKER_SEND_FAILED",
                "Worker frame transmission was interrupted",
                retryable=state == "none",
                mutation_state=state,
            )
        offset += sent
        _remaining_nanoseconds(
            deadline_monotonic_ns,
            mutation_state=("none" if offset == 0 else started_mutation_state),
        )


def _send_frame_with_fds(
    sock: socket.socket,
    payload: bytes,
    capability_fds: Sequence[int],
    deadline_monotonic_ns: int,
) -> None:
    if not capability_fds or len(capability_fds) > _MAX_CREDENTIAL_FDS:
        _raise(
            "WORKER_ANCILLARY_INVALID", "Worker capability descriptor count is invalid"
        )
    _send_framed_bytes(
        sock,
        payload,
        deadline_monotonic_ns,
        capability_fds=capability_fds,
        started_mutation_state="ambiguous",
    )


def _shutdown_write(
    sock: socket.socket,
    *,
    mutation_state: str,
    deadline_monotonic_ns: int | None = None,
) -> None:
    if deadline_monotonic_ns is not None:
        _remaining_nanoseconds(
            deadline_monotonic_ns,
            mutation_state=mutation_state,
        )
    try:
        sock.shutdown(socket.SHUT_WR)
    except OSError:
        _raise(
            "WORKER_SHUTDOWN_FAILED",
            "Worker one-shot write boundary could not be established",
            retryable=mutation_state == "none",
            mutation_state=mutation_state,
        )
    if deadline_monotonic_ns is not None:
        _remaining_nanoseconds(
            deadline_monotonic_ns,
            mutation_state=mutation_state,
        )


def _collect_received_rights(
    ancillary: Sequence[tuple[int, int, bytes]],
    received_fds: list[int],
) -> int:
    invalid = False
    rights_messages = 0
    for level, kind, payload in ancillary:
        if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS:
            invalid = True
            continue
        rights_messages += 1
        received = array.array("i")
        usable = len(payload) - (len(payload) % received.itemsize)
        if usable:
            received.frombytes(payload[:usable])
            received_fds.extend(received.tolist())
        if usable == 0 or usable != len(payload):
            invalid = True
    if len(received_fds) > _MAX_CREDENTIAL_FDS:
        invalid = True
    if invalid:
        _raise("WORKER_ANCILLARY_INVALID", "Worker capability control data is invalid")
    return rights_messages


def _recvmsg_bounded(
    sock: socket.socket,
    size: int,
    ancillary_size: int,
    deadline_monotonic_ns: int,
    *,
    mutation_state: str,
) -> tuple[bytes, list[tuple[int, int, bytes]], int]:
    if not hasattr(socket, "MSG_CMSG_CLOEXEC"):
        _raise(
            "WORKER_PLATFORM_UNSUPPORTED", "Atomic close-on-exec receipt is unavailable"
        )
    flags = _RECV_FLAGS | getattr(socket, "MSG_CMSG_CLOEXEC", 0)
    while True:
        _wait_socket_ready(
            sock,
            select.POLLIN,
            deadline_monotonic_ns,
            mutation_state=mutation_state,
        )
        try:
            data, ancillary, message_flags, _address = sock.recvmsg(
                size,
                ancillary_size,
                flags,
            )
            _remaining_nanoseconds(
                deadline_monotonic_ns,
                mutation_state=mutation_state,
            )
            return data, ancillary, message_flags
        except (BlockingIOError, InterruptedError, TimeoutError):
            continue
        except OSError:
            _raise(
                "WORKER_RECEIVE_FAILED",
                "Worker frame could not be received",
                mutation_state=mutation_state,
            )


def _recv_response_frame(
    sock: socket.socket,
    deadline_monotonic_ns: int,
    *,
    mutation_state: str,
) -> bytes:
    """Receive an exact response frame while rejecting every ancillary item."""

    ancillary_size = socket.CMSG_SPACE(_MAX_CREDENTIAL_FDS * array.array("i").itemsize)
    received_fds: list[int] = []

    def exact(length: int) -> bytes:
        value = bytearray()
        while len(value) < length:
            data, ancillary, flags = _recvmsg_bounded(
                sock,
                min(length - len(value), _READ_CHUNK_BYTES),
                ancillary_size,
                deadline_monotonic_ns,
                mutation_state=mutation_state,
            )
            _collect_received_rights(ancillary, received_fds)
            if flags & (socket.MSG_CTRUNC | getattr(socket, "MSG_TRUNC", 0)):
                _raise(
                    "WORKER_ANCILLARY_INVALID",
                    "Worker response control data was truncated",
                    mutation_state=mutation_state,
                )
            if received_fds:
                _raise(
                    "WORKER_ANCILLARY_INVALID",
                    "Worker response must not contain descriptors",
                    mutation_state=mutation_state,
                )
            if not data:
                _raise(
                    "WORKER_FRAME_TRUNCATED",
                    "Worker response ended before its declared length",
                    mutation_state=mutation_state,
                )
            value.extend(data)
        return bytes(value)

    try:
        header = exact(4)
        (length,) = struct.unpack("!I", header)
        if length == 0 or length > MAX_FRAME_BYTES:
            _raise(
                "WORKER_FRAME_SIZE_INVALID",
                "Worker response exceeds the fixed byte limit",
                mutation_state=mutation_state,
            )
        return exact(length)
    finally:
        for descriptor in received_fds:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _recv_request_frame_and_fds(
    sock: socket.socket,
    deadline_monotonic_ns: int,
) -> tuple[bytes, list[int]]:
    if not hasattr(socket, "MSG_CMSG_CLOEXEC"):
        _raise(
            "WORKER_PLATFORM_UNSUPPORTED", "Atomic close-on-exec receipt is unavailable"
        )
    received_fds: list[int] = []
    rights_messages = 0
    header = bytearray()
    ancillary_size = socket.CMSG_SPACE(_MAX_CREDENTIAL_FDS * array.array("i").itemsize)

    try:
        while len(header) < 4:
            data, ancillary, flags = _recvmsg_bounded(
                sock,
                4 - len(header),
                ancillary_size,
                deadline_monotonic_ns,
                mutation_state="none",
            )
            rights_messages += _collect_received_rights(ancillary, received_fds)
            if rights_messages > 1:
                _raise(
                    "WORKER_ANCILLARY_INVALID",
                    "Worker request must contain one capability control record",
                )
            if flags & (socket.MSG_CTRUNC | getattr(socket, "MSG_TRUNC", 0)):
                _raise(
                    "WORKER_ANCILLARY_INVALID",
                    "Worker capability control data was truncated",
                )
            if not data:
                _raise("WORKER_FRAME_TRUNCATED", "Worker request header was truncated")
            header.extend(data)
        (length,) = struct.unpack("!I", bytes(header))
        if length == 0 or length > MAX_FRAME_BYTES:
            _raise(
                "WORKER_FRAME_SIZE_INVALID",
                "Worker request exceeds the fixed byte limit",
            )
        body = bytearray()
        while len(body) < length:
            data, ancillary, flags = _recvmsg_bounded(
                sock,
                min(length - len(body), _READ_CHUNK_BYTES),
                ancillary_size,
                deadline_monotonic_ns,
                mutation_state="none",
            )
            rights_messages += _collect_received_rights(ancillary, received_fds)
            if rights_messages > 1:
                _raise(
                    "WORKER_ANCILLARY_INVALID",
                    "Worker request must contain one capability control record",
                )
            if flags & (socket.MSG_CTRUNC | getattr(socket, "MSG_TRUNC", 0)):
                _raise(
                    "WORKER_ANCILLARY_INVALID",
                    "Worker capability control data was truncated",
                )
            if not data:
                _raise("WORKER_FRAME_TRUNCATED", "Worker request body was truncated")
            body.extend(data)
        return bytes(body), received_fds
    except BaseException:
        for descriptor in received_fds:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise


def _require_eof(
    sock: socket.socket,
    deadline_monotonic_ns: int,
    *,
    mutation_state: str,
    trailing_code: str,
) -> None:
    ancillary_size = socket.CMSG_SPACE(_MAX_CREDENTIAL_FDS * array.array("i").itemsize)
    received_fds: list[int] = []
    try:
        data, ancillary, flags = _recvmsg_bounded(
            sock,
            1,
            ancillary_size,
            deadline_monotonic_ns,
            mutation_state=mutation_state,
        )
        _collect_received_rights(ancillary, received_fds)
        if flags & (socket.MSG_CTRUNC | getattr(socket, "MSG_TRUNC", 0)):
            _raise(
                "WORKER_ANCILLARY_INVALID",
                "Worker trailing control data was truncated",
                mutation_state=mutation_state,
            )
        if data or received_fds:
            _raise(
                trailing_code,
                "Worker one-shot frame has trailing data",
                mutation_state=mutation_state,
            )
    finally:
        for descriptor in received_fds:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _read_received_capabilities_unbound(
    descriptors: Sequence[int],
) -> tuple[bytes, ...]:
    """Validate/read/close capabilities before any request JSON is decoded."""

    capabilities: list[bytes] = []
    identities: set[tuple[int, int]] = set()
    try:
        if not 1 <= len(descriptors) <= _MAX_CREDENTIAL_FDS:
            _raise(
                "WORKER_ANCILLARY_INVALID",
                "Worker capability descriptor count is invalid",
            )
        required_seals = _required_seals()
        for descriptor in descriptors:
            try:
                metadata = os.fstat(descriptor)
                close_flags = fcntl.fcntl(descriptor, fcntl.F_GETFD)
                seals = fcntl.fcntl(descriptor, fcntl.F_GET_SEALS)
            except OSError:
                _raise(
                    "WORKER_CAPABILITY_INVALID",
                    "Received capability descriptor is invalid",
                )
            if not stat.S_ISREG(metadata.st_mode):
                _raise(
                    "WORKER_CAPABILITY_INVALID", "Received capability is not a memfd"
                )
            identity = (metadata.st_dev, metadata.st_ino)
            if identity in identities:
                _raise(
                    "WORKER_CAPABILITY_INVALID",
                    "Received capability descriptors are aliased",
                )
            identities.add(identity)
            if not close_flags & fcntl.FD_CLOEXEC:
                _raise(
                    "WORKER_CAPABILITY_INVALID",
                    "Received capability is not close-on-exec",
                )
            if seals & required_seals != required_seals:
                _raise(
                    "WORKER_CAPABILITY_INVALID",
                    "Received capability is not fully sealed",
                )
            if not MIN_CAPABILITY_BYTES <= metadata.st_size <= MAX_CAPABILITY_BYTES:
                _raise(
                    "WORKER_CAPABILITY_INVALID", "Received capability size is invalid"
                )
            try:
                value = os.pread(descriptor, metadata.st_size + 1, 0)
            except OSError:
                _raise(
                    "WORKER_CAPABILITY_INVALID", "Received capability could not be read"
                )
            if len(value) != metadata.st_size:
                _raise(
                    "WORKER_CAPABILITY_INVALID",
                    "Received capability bytes are truncated",
                )
            capabilities.append(value)
        return tuple(capabilities)
    finally:
        for descriptor in descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _bind_received_capabilities(
    capabilities: Sequence[bytes],
    bindings: Sequence[Mapping[str, Any]],
) -> tuple[bytes, ...]:
    if len(capabilities) != len(bindings):
        _raise(
            "WORKER_ANCILLARY_INVALID", "Worker capability descriptor count is invalid"
        )
    if len(capabilities) > 1 and len(set(capabilities)) != len(capabilities):
        _raise("WORKER_CAPABILITY_INVALID", "Worker capabilities must be distinct")
    for capability, binding in zip(capabilities, bindings):
        if len(capability) != binding["byte_count"]:
            _raise("WORKER_CAPABILITY_INVALID", "Received capability size is invalid")
    return tuple(capabilities)


def _validate_wire_request(value: Any) -> dict[str, Any]:
    if isinstance(value, dict) and value.get("schema") == REQUEST_SCHEMA:
        return validate_request(value)
    if isinstance(value, dict) and value.get("schema") == CONTROL_REQUEST_SCHEMA:
        return validate_control_request(value)
    _raise("WORKER_PROTOCOL_REVISION_INVALID", "Worker request schema is not allowed")


def _verify_launcher_handoff_receipt_binding(
    request: Mapping[str, Any],
    expected_receipt_sha256: Any,
) -> None:
    """Bind one validated request to an independently supplied handoff digest."""

    expected = _sha256(
        expected_receipt_sha256,
        "expected launcher handoff receipt",
    )
    if request["launcher_handoff_receipt_sha256"] != expected:
        _raise(
            "WORKER_LAUNCHER_HANDOFF_MISMATCH",
            "Worker request does not match the attested launcher handoff",
        )


def _validate_wire_response(value: Any, request: Mapping[str, Any]) -> dict[str, Any]:
    if request["schema"] == REQUEST_SCHEMA:
        return validate_response(value, request)
    if request["schema"] == CONTROL_REQUEST_SCHEMA:
        return validate_control_response(value, request)
    _raise("WORKER_PROTOCOL_REVISION_INVALID", "Worker request schema is not allowed")


def _validate_handshake_deadline(deadline_monotonic_ns: Any) -> int:
    deadline = _integer(
        deadline_monotonic_ns,
        "worker-owned handshake deadline",
        minimum=1,
        maximum=MAX_JSON_INTEGER,
    )
    now = time.monotonic_ns()
    if deadline <= now:
        _raise("WORKER_DEADLINE_EXCEEDED", "Worker handshake deadline has expired")
    if deadline - now > MAX_HANDSHAKE_AHEAD_NS:
        _raise(
            "WORKER_DEADLINE_INVALID",
            "Worker handshake deadline exceeds the fixed horizon",
        )
    return deadline


def receive_worker_request(
    connected_socket: socket.socket | int,
    *,
    handshake_deadline_monotonic_ns: int,
    client_peer_policy: ClientPeerPolicy,
) -> ReceivedWorkerRequest:
    """Authenticate and receive exactly one request, EOF, and sealed capabilities.

    The worker supplies its own short handshake deadline.  The independently
    embedded operation deadline is validated only after FD bytes have been
    read and the raw request has passed capability-leak scanning; equality is
    deliberately neither required nor allowed as an authentication shortcut.
    """

    connection = _coerce_connected_socket(connected_socket)
    descriptors: list[int] = []
    transfer_succeeded = False
    try:
        _require_unix_stream(connection)
        handshake_deadline = _validate_handshake_deadline(
            handshake_deadline_monotonic_ns
        )
        verify_client_peer(connection, client_peer_policy)
        raw, descriptors = _recv_request_frame_and_fds(connection, handshake_deadline)
        _require_eof(
            connection,
            handshake_deadline,
            mutation_state="none",
            trailing_code="WORKER_REQUEST_TRAILING_DATA",
        )
        capability_descriptors = descriptors
        descriptors = []
        capabilities = _read_received_capabilities_unbound(capability_descriptors)
        _assert_no_capability_transforms(
            raw,
            capabilities,
            code="WORKER_REQUEST_CAPABILITY_LEAK",
            mutation_state="none",
        )
        decoded = decode_canonical_json(raw)
        request = _validate_wire_request(decoded)
        _verify_launcher_handoff_receipt_binding(
            request,
            client_peer_policy.expected_launcher_handoff_receipt_sha256,
        )
        capabilities = _bind_received_capabilities(
            capabilities,
            request["credential_transport"]["descriptors"],
        )
        received = _new_authenticated_received_request(
            value=request,
            canonical_bytes=raw,
            capabilities=capabilities,
            connection=connection,
            response_deadline_monotonic_ns=request["deadline_monotonic_ns"],
        )
        transfer_succeeded = True
        return received
    finally:
        for descriptor in descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if not transfer_succeeded:
            connection.close()


def _coerce_connected_socket(value: socket.socket | int) -> socket.socket:
    """Take ownership without dup; even an invalid integer FD is consumed."""

    if isinstance(value, socket.socket):
        return value
    descriptor = _integer(
        value, "worker socket descriptor", minimum=0, maximum=2**31 - 1
    )
    try:
        return socket.socket(fileno=descriptor)
    except (OSError, ValueError):
        try:
            os.close(descriptor)
        except OSError:
            pass
        _raise("WORKER_SOCKET_INVALID", "Worker socket descriptor is invalid")


def _send_canonical_worker_response(
    received: ReceivedWorkerRequest,
    canonical_result: bytes,
) -> WorkerResponse:
    received = _require_authenticated_received_request(received)
    connection = received._take_response_socket()
    try:
        if type(canonical_result) is not bytes:
            _raise(
                "WORKER_JSON_INVALID",
                "Worker committed response must be exact canonical bytes",
            )
        decoded = decode_canonical_json(canonical_result)
        if type(decoded) is not dict:
            _raise("WORKER_JSON_INVALID", "Worker response must be an object")
        _assert_no_capability_transforms(
            canonical_result,
            received.capabilities,
        )
        validated = _validate_wire_response(decoded, received.value)
        canonical = canonical_json_bytes(validated)
        if canonical != canonical_result:
            _raise("WORKER_JSON_NONCANONICAL", "Worker response is not canonical")
        _send_framed_bytes(
            connection,
            canonical_result,
            received._response_deadline_monotonic_ns,
            started_mutation_state=validated["mutation_state"],
        )
        _shutdown_write(
            connection,
            mutation_state=validated["mutation_state"],
            deadline_monotonic_ns=received._response_deadline_monotonic_ns,
        )
        return WorkerResponse(validated, canonical_result)
    finally:
        connection.close()


def send_worker_response(
    received: ReceivedWorkerRequest,
    result: Mapping[str, Any],
) -> WorkerResponse:
    """Validate, send one response frame, SHUT_WR, and consume the session."""

    _require_authenticated_received_request(received)
    return _send_canonical_worker_response(received, canonical_json_bytes(result))


def send_committed_worker_response(
    received: ReceivedWorkerRequest,
    canonical_result: bytes,
) -> WorkerResponse:
    """Validate and transmit the exact canonical bytes already durably committed."""

    return _send_canonical_worker_response(received, canonical_result)


def call_worker(
    request: Mapping[str, Any],
    *,
    capabilities: Sequence[bytes],
    launcher_handoff_receipt_sha256: str,
    peer_policy: PeerPolicy,
    connected_socket: socket.socket | int,
) -> WorkerResponse:
    """Perform one closed call over a descriptor from an attested handoff.

    ``launcher_handoff_receipt_sha256`` is deliberately required and has no
    ambient or caller-selected default.  The coordinator must supply the exact
    digest exposed by its opaque launcher handoff object.
    """

    connection = _coerce_connected_socket(connected_socket)
    capability_fds: list[int] = []
    try:
        _require_unix_stream(connection)
        validated = _validate_wire_request(request)
        _verify_launcher_handoff_receipt_binding(
            validated,
            launcher_handoff_receipt_sha256,
        )
        expected_bindings = validated["credential_transport"]["descriptors"]
        if isinstance(capabilities, (bytes, bytearray, str)) or not isinstance(
            capabilities, Sequence
        ):
            _raise("WORKER_CAPABILITY_INVALID", "Worker capabilities must be ordered")
        exact_capabilities = tuple(capabilities)
        if len(exact_capabilities) != len(expected_bindings):
            _raise(
                "WORKER_CAPABILITY_INVALID", "Capability count does not match request"
            )
        for capability, binding in zip(exact_capabilities, expected_bindings):
            if (
                not isinstance(capability, bytes)
                or len(capability) != binding["byte_count"]
            ):
                _raise(
                    "WORKER_CAPABILITY_INVALID",
                    "Capability length does not match request",
                )
        if len(exact_capabilities) > 1 and len(set(exact_capabilities)) != len(
            exact_capabilities
        ):
            _raise("WORKER_CAPABILITY_INVALID", "Worker capabilities must be distinct")

        _remaining_nanoseconds(
            validated["deadline_monotonic_ns"], mutation_state="none"
        )
        verify_peer(connection, peer_policy, validated)
        payload = canonical_json_bytes(validated)
        if len(payload) > MAX_FRAME_BYTES:
            _raise(
                "WORKER_FRAME_SIZE_INVALID",
                "Worker request exceeds the fixed byte limit",
            )
        _assert_no_capability_transforms(
            payload,
            exact_capabilities,
            code="WORKER_REQUEST_CAPABILITY_LEAK",
            mutation_state="none",
        )
        capability_fds = _sealed_capability_fds(exact_capabilities)
        _send_frame_with_fds(
            connection,
            payload,
            capability_fds,
            validated["deadline_monotonic_ns"],
        )
        for descriptor in capability_fds:
            try:
                os.close(descriptor)
            except OSError:
                pass
        capability_fds = []
        _shutdown_write(
            connection,
            mutation_state="ambiguous",
            deadline_monotonic_ns=validated["deadline_monotonic_ns"],
        )
        raw = _recv_response_frame(
            connection,
            validated["deadline_monotonic_ns"],
            mutation_state="ambiguous",
        )
        _require_eof(
            connection,
            validated["deadline_monotonic_ns"],
            mutation_state="ambiguous",
            trailing_code="WORKER_RESPONSE_TRAILING_DATA",
        )
        _assert_no_capability_transforms(raw, exact_capabilities)
        try:
            decoded = decode_canonical_json(raw)
            result = _validate_wire_response(decoded, validated)
        except WorkerProtocolError as error:
            if error.mutation_state == "none":
                raise dataclasses.replace(
                    error, retryable=False, mutation_state="ambiguous"
                )
            raise
        if result["status"] == "failed":
            error = result["error"]
            raise WorkerProtocolError(
                code=error["code"],
                message=_REMOTE_ERRORS[(error["dependency"], error["code"])],
                dependency=error["dependency"],
                retryable=error["retryable"],
                mutation_state=error["mutation_state"],
            )
        return WorkerResponse(result, raw)
    finally:
        for descriptor in capability_fds:
            try:
                os.close(descriptor)
            except OSError:
                pass
        connection.close()


__all__ = [
    "ARTIFACT_ROOT_SCHEMA",
    "CONTROL_RECEIPT_SCHEMA",
    "CONTROL_REQUEST_SCHEMA",
    "CONTROL_RESULT_SCHEMA",
    "CREDENTIAL_TRANSPORT",
    "ClientPeerPolicy",
    "MAX_CAPABILITY_BYTES",
    "MAX_FRAME_BYTES",
    "MAX_HANDSHAKE_AHEAD_NS",
    "PROTOCOL_REVISION",
    "PeerPolicy",
    "REQUEST_SCHEMA",
    "RESULT_SCHEMA",
    "ReceivedWorkerRequest",
    "WorkerProtocolError",
    "WorkerResponse",
    "call_worker",
    "canonical_json_bytes",
    "control_operation_contract",
    "control_request_binding_for",
    "current_host_boot_id_sha256",
    "decode_canonical_json",
    "operation_contract",
    "process_start_time_ticks",
    "process_start_token_sha256",
    "receive_worker_request",
    "request_binding_for",
    "send_committed_worker_response",
    "send_worker_response",
    "socket_inode_binding_sha256",
    "validate_request",
    "validate_response",
    "validate_control_request",
    "validate_control_response",
    "verify_client_peer",
    "verify_peer",
]
