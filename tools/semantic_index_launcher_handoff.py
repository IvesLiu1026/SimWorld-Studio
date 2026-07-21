"""Privilege-separated launcher handoff for the semantic-index coordinator.

This module is an offline, pure-standard-library vertical slice.  It accepts
exactly one already-connected ``AF_UNIX/SOCK_STREAM`` worker descriptor over a
one-shot ``SCM_RIGHTS`` channel, authenticates the launcher with Linux
``SO_PEERCRED``, and binds the descriptor to a closed canonical receipt.

The actual trust boundary is OS privilege separation: the launcher must be the
coordinator's direct parent, must have the exact deployment-pinned UID/GID/PID,
and must have a UID different from the coordinator's effective UID.  A trusted
service manager (for example systemd) must attest and start that launcher and
the isolated coordinator source closure.  The module-private construction
sentinel makes accidental in-process fabrication difficult, but it is not a
signature, a cryptographic capability, or a defence against modified trusted
Python source.  The Production adapter must remain unregistered until it
accepts this opaque handoff directly and the remaining protocol gates pass.

No pathname is opened, no service is started, and no network connection is
created here.
"""

from __future__ import annotations

import array
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


HANDOFF_PROTOCOL = "simworld-semantic-index-launcher-handoff/v1"
HANDOFF_RECEIPT_SCHEMA = "simworld-semantic-index-launcher-handoff-receipt/v1"
WORKER_PROTOCOL_REVISION = "simworld-semantic-index-worker/v1"
MAX_HANDOFF_FRAME_BYTES = 64 * 1024
MAX_HANDOFF_TTL_SECONDS = 60
MAX_CLOCK_SKEW_SECONDS = 5
MAX_HANDSHAKE_NS = 5 * 1_000_000_000
MAX_JSON_INTEGER = 9_007_199_254_740_991
_READ_CHUNK_BYTES = 16 * 1024
_PROC_READ_BYTES = 8192

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,159}$")
_GENERATION_RE = re.compile(r"^semantic-generation:[0-9a-f]{64}$")
_IMAGE_RE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
_RFC3339_SECONDS_RE = re.compile(
    r"^[0-9]{4}-(0[1-9]|1[0-2])-([0-2][0-9]|3[01])T"
    r"([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]Z$"
)

_SERIALIZATION_CONTRACT = {
    "canonicalization": "rfc8785-jcs-ascii-keys-integer-domain-v1",
    "duplicate_keys": "reject",
    "encoding": "utf-8",
    "numbers": "integers_only",
    "trailing_newline": False,
    "unicode_normalization": "require_already_nfc",
}

_RECEIPT_KEYS = frozenset(
    {
        "schema",
        "protocol",
        "issued_at",
        "expires_at",
        "host_boot_id_sha256",
        "launcher_binding",
        "coordinator_binding",
        "execution_binding",
        "worker_binding",
        "serialization_contract",
    }
)
_LAUNCHER_KEYS = frozenset(
    {
        "uid",
        "gid",
        "pid",
        "process_start_time_ticks",
        "process_start_token_sha256",
    }
)
_COORDINATOR_KEYS = frozenset(
    {
        "uid",
        "gid",
        "pid",
        "parent_pid",
        "process_start_time_ticks",
        "process_start_token_sha256",
    }
)
_EXECUTION_KEYS = frozenset(
    {
        "execution_plan_sha256",
        "reviewed_job_sha256",
        "approval_basis_sha256",
        "generation_id",
    }
)
_WORKER_KEYS = frozenset(
    {
        "protocol_revision",
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


@dataclasses.dataclass(frozen=True)
class LauncherHandoffError(Exception):
    """Bounded local error; peer-controlled strings are never retained."""

    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def _raise(code: str, message: str) -> None:
    raise LauncherHandoffError(code, message)


def _integer(
    value: Any,
    label: str,
    *,
    minimum: int = 0,
    maximum: int = MAX_JSON_INTEGER,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _raise("HANDOFF_VALUE_INVALID", f"{label} is not an integer")
    if not minimum <= value <= maximum:
        _raise("HANDOFF_VALUE_INVALID", f"{label} is outside its allowed range")
    return value


def _uid_gid(value: Any, label: str) -> int:
    return _integer(value, label, minimum=0, maximum=2**31 - 1)


def _pid(value: Any, label: str) -> int:
    return _integer(value, label, minimum=1, maximum=2**31 - 1)


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        _raise("HANDOFF_VALUE_INVALID", f"{label} is not a SHA-256 digest")
    return value


def _nfc_text(value: Any, label: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value:
        _raise("HANDOFF_VALUE_INVALID", f"{label} must be non-empty text")
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeError:
        _raise("HANDOFF_VALUE_INVALID", f"{label} is not UTF-8 text")
    if len(encoded) > maximum or unicodedata.normalize("NFC", value) != value:
        _raise("HANDOFF_VALUE_INVALID", f"{label} is not canonical text")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        _raise("HANDOFF_VALUE_INVALID", f"{label} contains a control character")
    return value


def _matched(value: Any, label: str, pattern: re.Pattern[str], maximum: int) -> str:
    text = _nfc_text(value, label, maximum=maximum)
    if not pattern.fullmatch(text) or "://" in text:
        _raise("HANDOFF_VALUE_INVALID", f"{label} has an invalid identity")
    return text


def _exact_mapping(value: Any, keys: frozenset[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or frozenset(value) != keys:
        _raise("HANDOFF_SHAPE_INVALID", f"{label} has an invalid field set")
    return value


def _validate_json_value(value: Any, *, depth: int = 0) -> None:
    if depth > 12:
        _raise("HANDOFF_JSON_INVALID", "Handoff JSON nesting is too deep")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int) and not isinstance(value, bool):
        _integer(value, "JSON integer", minimum=-MAX_JSON_INTEGER)
        return
    if isinstance(value, str):
        _nfc_text(value, "JSON string", maximum=4096)
        return
    if isinstance(value, list):
        if len(value) > 128:
            _raise("HANDOFF_JSON_INVALID", "Handoff JSON array is too large")
        for item in value:
            _validate_json_value(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > 128:
            _raise("HANDOFF_JSON_INVALID", "Handoff JSON object is too large")
        for key, item in value.items():
            _nfc_text(key, "JSON object key", maximum=128)
            _validate_json_value(item, depth=depth + 1)
        return
    _raise("HANDOFF_JSON_INVALID", "Handoff JSON contains a forbidden scalar")


def canonical_json_bytes(value: Any) -> bytes:
    """Encode the closed handoff contract's ASCII-key/integer JCS subset."""

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
        _raise("HANDOFF_JSON_INVALID", "Handoff JSON cannot be encoded canonically")


def _reject_constant(_value: str) -> None:
    _raise("HANDOFF_JSON_INVALID", "Handoff JSON contains a forbidden number")


def _reject_float(_value: str) -> None:
    _raise("HANDOFF_JSON_INVALID", "Handoff JSON floats are forbidden")


def _parse_integer(value: str) -> int:
    try:
        parsed = int(value, 10)
    except ValueError:
        _raise("HANDOFF_JSON_INVALID", "Handoff JSON integer is invalid")
    return _integer(parsed, "JSON integer", minimum=-MAX_JSON_INTEGER)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            _raise("HANDOFF_JSON_INVALID", "Handoff JSON contains a duplicate key")
        value[key] = item
    return value


def decode_canonical_json(raw: bytes) -> Any:
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_HANDOFF_FRAME_BYTES:
        _raise("HANDOFF_FRAME_SIZE_INVALID", "Handoff frame has an invalid size")
    try:
        value = json.loads(
            raw.decode("utf-8", "strict"),
            object_pairs_hook=_unique_object,
            parse_int=_parse_integer,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except LauncherHandoffError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        _raise("HANDOFF_JSON_INVALID", "Handoff frame is not canonical JSON")
    _validate_json_value(value)
    if canonical_json_bytes(value) != raw:
        _raise("HANDOFF_JSON_NONCANONICAL", "Handoff frame is not canonical JSON")
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
        "HANDOFF_HOST_BOOT_UNVERIFIED",
        "Host boot identity could not be verified",
    ).strip()
    if not re.fullmatch(
        rb"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", raw
    ):
        _raise("HANDOFF_HOST_BOOT_UNVERIFIED", "Host boot identity could not be verified")
    return hashlib.sha256(raw).hexdigest()


def _process_stat(pid: int) -> tuple[int, int]:
    exact_pid = _pid(pid, "process PID")
    raw = _fixed_read(
        f"/proc/{exact_pid}/stat",
        _PROC_READ_BYTES,
        "HANDOFF_PROCESS_UNVERIFIED",
        "Process identity could not be verified",
    )
    closing = raw.rfind(b")")
    if closing <= 0:
        _raise("HANDOFF_PROCESS_UNVERIFIED", "Process identity could not be verified")
    fields = raw[closing + 2 :].split()
    if len(fields) <= 19 or not fields[1].isdigit() or not fields[19].isdigit():
        _raise("HANDOFF_PROCESS_UNVERIFIED", "Process identity could not be verified")
    parent_pid = _pid(int(fields[1], 10), "process parent PID")
    start_ticks = _integer(
        int(fields[19], 10),
        "process start time",
        minimum=1,
    )
    return parent_pid, start_ticks


def process_start_time_ticks(pid: int) -> int:
    return _process_stat(pid)[1]


def _process_effective_credentials(pid: int) -> tuple[int, int]:
    raw = _fixed_read(
        f"/proc/{_pid(pid, 'process PID')}/status",
        _PROC_READ_BYTES,
        "HANDOFF_PROCESS_UNVERIFIED",
        "Process credentials could not be verified",
    )
    uid: int | None = None
    gid: int | None = None
    for line in raw.splitlines():
        if line.startswith(b"Uid:"):
            fields = line.split()
            if len(fields) == 5 and fields[2].isdigit():
                uid = int(fields[2], 10)
        elif line.startswith(b"Gid:"):
            fields = line.split()
            if len(fields) == 5 and fields[2].isdigit():
                gid = int(fields[2], 10)
    if uid is None or gid is None:
        _raise("HANDOFF_PROCESS_UNVERIFIED", "Process credentials could not be verified")
    return _uid_gid(uid, "process effective UID"), _uid_gid(gid, "process effective GID")


def process_start_token_sha256(host_boot_sha256: str, pid: int, start_ticks: int) -> str:
    _sha256(host_boot_sha256, "host boot identity")
    exact_pid = _pid(pid, "process PID")
    exact_ticks = _integer(start_ticks, "process start time", minimum=1)
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "host_boot_id_sha256": host_boot_sha256,
                "peer_pid": exact_pid,
                "process_start_time_ticks": exact_ticks,
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
    exact_pid = _pid(pid, "worker peer PID")
    exact_device = _integer(device, "worker socket device")
    exact_inode = _integer(inode, "worker socket inode", minimum=1)
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "host_boot_id_sha256": host_boot_sha256,
                "peer_pid": exact_pid,
                "socket_device": exact_device,
                "socket_inode": exact_inode,
            }
        )
    ).hexdigest()


@dataclasses.dataclass(frozen=True)
class ExpectedWorkerBinding:
    protocol_revision: str
    deployment_identity: str
    runtime_image: str
    whoami_sha256: str
    peer_attestation_sha256: str
    runtime_attestation_sha256: str
    host_boot_id_sha256: str
    peer_uid: int
    peer_gid: int
    peer_pid: int
    process_start_time_ticks: int
    process_start_token_sha256: str
    socket_device: int
    socket_inode: int
    socket_inode_binding_sha256: str

    def __post_init__(self) -> None:
        if self.protocol_revision != WORKER_PROTOCOL_REVISION:
            _raise("HANDOFF_POLICY_INVALID", "Worker protocol revision is not allowed")
        _matched(self.deployment_identity, "worker deployment identity", _SAFE_ID_RE, 160)
        image = _nfc_text(self.runtime_image, "worker runtime image", maximum=512)
        if not _IMAGE_RE.fullmatch(image) or "://" in image:
            _raise("HANDOFF_POLICY_INVALID", "Worker runtime image is not digest-pinned")
        _sha256(self.whoami_sha256, "worker WhoAmI")
        _sha256(self.peer_attestation_sha256, "worker peer attestation")
        _sha256(self.runtime_attestation_sha256, "worker runtime attestation")
        _sha256(self.host_boot_id_sha256, "worker host boot identity")
        _uid_gid(self.peer_uid, "worker peer UID")
        _uid_gid(self.peer_gid, "worker peer GID")
        _pid(self.peer_pid, "worker peer PID")
        _integer(self.process_start_time_ticks, "worker process start time", minimum=1)
        _integer(self.socket_device, "worker socket device")
        _integer(self.socket_inode, "worker socket inode", minimum=1)
        expected_process = process_start_token_sha256(
            self.host_boot_id_sha256,
            self.peer_pid,
            self.process_start_time_ticks,
        )
        expected_socket = socket_inode_binding_sha256(
            self.host_boot_id_sha256,
            self.peer_pid,
            self.socket_device,
            self.socket_inode,
        )
        if self.process_start_token_sha256 != expected_process:
            _raise("HANDOFF_POLICY_INVALID", "Worker process token is inconsistent")
        if self.socket_inode_binding_sha256 != expected_socket:
            _raise("HANDOFF_POLICY_INVALID", "Worker socket token is inconsistent")

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class LauncherHandoffPolicy:
    """Deployment-pinned values supplied to the isolated coordinator."""

    expected_launcher_uid: int
    expected_launcher_gid: int
    expected_launcher_pid: int
    expected_launcher_process_start_time_ticks: int
    expected_launcher_process_start_token_sha256: str
    host_boot_id_sha256: str
    expected_receipt_sha256: str
    execution_plan_sha256: str
    reviewed_job_sha256: str
    approval_basis_sha256: str
    generation_id: str
    worker: ExpectedWorkerBinding

    def __post_init__(self) -> None:
        _uid_gid(self.expected_launcher_uid, "expected launcher UID")
        _uid_gid(self.expected_launcher_gid, "expected launcher GID")
        _pid(self.expected_launcher_pid, "expected launcher PID")
        _integer(
            self.expected_launcher_process_start_time_ticks,
            "expected launcher process start time",
            minimum=1,
        )
        _sha256(self.host_boot_id_sha256, "host boot identity")
        _sha256(self.expected_receipt_sha256, "expected handoff receipt")
        _sha256(self.execution_plan_sha256, "execution plan")
        _sha256(self.reviewed_job_sha256, "reviewed job")
        _sha256(self.approval_basis_sha256, "approval basis")
        if not isinstance(self.generation_id, str) or not _GENERATION_RE.fullmatch(
            self.generation_id
        ):
            _raise("HANDOFF_POLICY_INVALID", "Generation identity is invalid")
        if not isinstance(self.worker, ExpectedWorkerBinding):
            _raise("HANDOFF_POLICY_INVALID", "Worker binding policy is invalid")
        if self.worker.host_boot_id_sha256 != self.host_boot_id_sha256:
            _raise("HANDOFF_POLICY_INVALID", "Worker host boot identity is inconsistent")
        expected_token = process_start_token_sha256(
            self.host_boot_id_sha256,
            self.expected_launcher_pid,
            self.expected_launcher_process_start_time_ticks,
        )
        if self.expected_launcher_process_start_token_sha256 != expected_token:
            _raise("HANDOFF_POLICY_INVALID", "Launcher process token is inconsistent")


@dataclasses.dataclass(frozen=True)
class _RuntimeIdentity:
    uid: int
    gid: int
    pid: int
    start_ticks: int
    start_token_sha256: str


@dataclasses.dataclass(frozen=True)
class _WorkerSocketObservation:
    peer_uid: int
    peer_gid: int
    peer_pid: int
    process_start_time_ticks: int
    process_start_token_sha256: str
    socket_device: int
    socket_inode: int
    socket_inode_binding_sha256: str


def _peer_credentials(sock: socket.socket, label: str) -> tuple[int, int, int]:
    if not hasattr(socket, "SO_PEERCRED"):
        _raise("HANDOFF_PLATFORM_UNSUPPORTED", "Unix peer credentials are unavailable")
    try:
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        pid, uid, gid = struct.unpack("3i", raw)
    except (OSError, struct.error):
        _raise("HANDOFF_PEER_UNVERIFIED", f"{label} peer credentials could not be verified")
    return _pid(pid, f"{label} peer PID"), _uid_gid(uid, f"{label} peer UID"), _uid_gid(
        gid, f"{label} peer GID"
    )


def _require_connected_unix_stream(sock: socket.socket, label: str) -> None:
    if not isinstance(sock, socket.socket) or sock.family != socket.AF_UNIX:
        _raise("HANDOFF_SOCKET_INVALID", f"{label} is not an AF_UNIX socket")
    try:
        socket_type = sock.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE)
    except OSError:
        _raise("HANDOFF_SOCKET_INVALID", f"{label} type could not be verified")
    if socket_type != socket.SOCK_STREAM:
        _raise("HANDOFF_SOCKET_INVALID", f"{label} is not a stream socket")
    try:
        if sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN):
            _raise("HANDOFF_SOCKET_INVALID", f"{label} is a listener")
        sock.getpeername()
    except LauncherHandoffError:
        raise
    except OSError:
        _raise("HANDOFF_SOCKET_INVALID", f"{label} is not connected")


def _observe_worker_socket(
    worker_socket: socket.socket,
    host_boot_sha256: str,
) -> _WorkerSocketObservation:
    _require_connected_unix_stream(worker_socket, "Worker descriptor")
    try:
        metadata = os.fstat(worker_socket.fileno())
        descriptor_flags = fcntl.fcntl(worker_socket.fileno(), fcntl.F_GETFD)
    except OSError:
        _raise("HANDOFF_WORKER_SOCKET_INVALID", "Worker descriptor could not be inspected")
    if not stat.S_ISSOCK(metadata.st_mode):
        _raise("HANDOFF_WORKER_SOCKET_INVALID", "Worker descriptor is not a socket")
    if not descriptor_flags & fcntl.FD_CLOEXEC:
        _raise("HANDOFF_WORKER_SOCKET_INVALID", "Worker descriptor is not close-on-exec")
    peer_pid, peer_uid, peer_gid = _peer_credentials(worker_socket, "Worker")
    start_ticks = process_start_time_ticks(peer_pid)
    return _WorkerSocketObservation(
        peer_uid=peer_uid,
        peer_gid=peer_gid,
        peer_pid=peer_pid,
        process_start_time_ticks=start_ticks,
        process_start_token_sha256=process_start_token_sha256(
            host_boot_sha256, peer_pid, start_ticks
        ),
        socket_device=metadata.st_dev,
        socket_inode=metadata.st_ino,
        socket_inode_binding_sha256=socket_inode_binding_sha256(
            host_boot_sha256, peer_pid, metadata.st_dev, metadata.st_ino
        ),
    )


def _validate_worker_observation(
    observation: _WorkerSocketObservation,
    expected: ExpectedWorkerBinding,
) -> None:
    observed = dataclasses.asdict(observation)
    for key, value in observed.items():
        if getattr(expected, key) != value:
            _raise(
                "HANDOFF_WORKER_SOCKET_BINDING_MISMATCH",
                f"Worker {key} does not match the attested binding",
            )


def _parse_time(value: Any, label: str) -> dt.datetime:
    if not isinstance(value, str) or not _RFC3339_SECONDS_RE.fullmatch(value):
        _raise("HANDOFF_FRESHNESS_INVALID", f"{label} is not canonical RFC3339 time")
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc
        )
    except ValueError:
        _raise("HANDOFF_FRESHNESS_INVALID", f"{label} is not a real timestamp")


def _validate_receipt_shape(value: Any) -> dict[str, Any]:
    receipt = _exact_mapping(value, _RECEIPT_KEYS, "Handoff receipt")
    if receipt["schema"] != HANDOFF_RECEIPT_SCHEMA or receipt["protocol"] != HANDOFF_PROTOCOL:
        _raise("HANDOFF_PROTOCOL_INVALID", "Handoff receipt protocol is not allowed")
    _sha256(receipt["host_boot_id_sha256"], "receipt host boot identity")
    launcher = _exact_mapping(receipt["launcher_binding"], _LAUNCHER_KEYS, "Launcher binding")
    coordinator = _exact_mapping(
        receipt["coordinator_binding"], _COORDINATOR_KEYS, "Coordinator binding"
    )
    execution = _exact_mapping(receipt["execution_binding"], _EXECUTION_KEYS, "Execution binding")
    worker = _exact_mapping(receipt["worker_binding"], _WORKER_KEYS, "Worker binding")
    _uid_gid(launcher["uid"], "launcher UID")
    _uid_gid(launcher["gid"], "launcher GID")
    _pid(launcher["pid"], "launcher PID")
    _integer(launcher["process_start_time_ticks"], "launcher process start time", minimum=1)
    _sha256(launcher["process_start_token_sha256"], "launcher process token")
    _uid_gid(coordinator["uid"], "coordinator UID")
    _uid_gid(coordinator["gid"], "coordinator GID")
    _pid(coordinator["pid"], "coordinator PID")
    _pid(coordinator["parent_pid"], "coordinator parent PID")
    _integer(coordinator["process_start_time_ticks"], "coordinator process start time", minimum=1)
    _sha256(coordinator["process_start_token_sha256"], "coordinator process token")
    for key in ("execution_plan_sha256", "reviewed_job_sha256", "approval_basis_sha256"):
        _sha256(execution[key], key)
    if not isinstance(execution["generation_id"], str) or not _GENERATION_RE.fullmatch(
        execution["generation_id"]
    ):
        _raise("HANDOFF_VALUE_INVALID", "Receipt generation identity is invalid")
    ExpectedWorkerBinding(**dict(worker))
    if receipt["serialization_contract"] != _SERIALIZATION_CONTRACT:
        _raise("HANDOFF_SERIALIZATION_INVALID", "Handoff serialization contract changed")
    _parse_time(receipt["issued_at"], "receipt issue time")
    _parse_time(receipt["expires_at"], "receipt expiry time")
    return dict(receipt)


def _validate_receipt_freshness(receipt: Mapping[str, Any], now: dt.datetime) -> None:
    issued = _parse_time(receipt["issued_at"], "receipt issue time")
    expires = _parse_time(receipt["expires_at"], "receipt expiry time")
    if now.tzinfo is None or now.utcoffset() is None:
        _raise("HANDOFF_FRESHNESS_INVALID", "Coordinator clock is not timezone-aware")
    current = now.astimezone(dt.timezone.utc)
    if expires <= issued:
        _raise("HANDOFF_FRESHNESS_INVALID", "Handoff receipt lifetime is invalid")
    if (expires - issued).total_seconds() > MAX_HANDOFF_TTL_SECONDS:
        _raise("HANDOFF_FRESHNESS_INVALID", "Handoff receipt lifetime exceeds the limit")
    if issued > current + dt.timedelta(seconds=MAX_CLOCK_SKEW_SECONDS):
        _raise("HANDOFF_FRESHNESS_INVALID", "Handoff receipt is from the future")
    if expires <= current:
        _raise("HANDOFF_FRESHNESS_INVALID", "Handoff receipt has expired")


def _validate_receipt_digest(raw: bytes, expected_sha256: str) -> str:
    expected = _sha256(expected_sha256, "expected handoff receipt")
    observed = hashlib.sha256(raw).hexdigest()
    if observed != expected:
        _raise("HANDOFF_RECEIPT_DIGEST_MISMATCH", "Handoff receipt digest changed")
    return observed


def _runtime_identity(pid: int, uid: int, gid: int, boot: str) -> _RuntimeIdentity:
    start = process_start_time_ticks(pid)
    return _RuntimeIdentity(
        uid=uid,
        gid=gid,
        pid=pid,
        start_ticks=start,
        start_token_sha256=process_start_token_sha256(boot, pid, start),
    )


def _validate_handoff_channel_peer(
    channel: socket.socket,
    coordinator_binding: Mapping[str, Any],
    host_boot_sha256: str,
) -> None:
    """Bind the descriptor recipient to the attested coordinator process.

    Checking a PID named inside the receipt is not sufficient: without this
    SO_PEERCRED comparison an unrelated process holding the other endpoint
    could receive the worker descriptor.  The process-start token is observed
    again immediately before the send so PID reuse or a stale receipt fails
    closed.
    """

    peer_pid, peer_uid, peer_gid = _peer_credentials(channel, "Handoff channel")
    if (peer_pid, peer_uid, peer_gid) != (
        coordinator_binding["pid"],
        coordinator_binding["uid"],
        coordinator_binding["gid"],
    ):
        _raise(
            "HANDOFF_COORDINATOR_PEER_MISMATCH",
            "Handoff channel peer is not the attested coordinator",
        )
    peer_start = process_start_time_ticks(peer_pid)
    if (
        peer_start != coordinator_binding["process_start_time_ticks"]
        or process_start_token_sha256(host_boot_sha256, peer_pid, peer_start)
        != coordinator_binding["process_start_token_sha256"]
    ):
        _raise(
            "HANDOFF_COORDINATOR_PEER_MISMATCH",
            "Handoff channel peer process identity changed",
        )


def _validate_receipt_bindings(
    receipt: Mapping[str, Any],
    policy: LauncherHandoffPolicy,
    launcher: _RuntimeIdentity,
    coordinator: _RuntimeIdentity,
    worker_observation: _WorkerSocketObservation,
) -> None:
    if receipt["host_boot_id_sha256"] != policy.host_boot_id_sha256:
        _raise("HANDOFF_HOST_BOOT_MISMATCH", "Handoff receipt host boot identity changed")
    expected_launcher = {
        "uid": launcher.uid,
        "gid": launcher.gid,
        "pid": launcher.pid,
        "process_start_time_ticks": launcher.start_ticks,
        "process_start_token_sha256": launcher.start_token_sha256,
    }
    if receipt["launcher_binding"] != expected_launcher:
        _raise("HANDOFF_LAUNCHER_BINDING_MISMATCH", "Handoff launcher binding changed")
    expected_coordinator = {
        "uid": coordinator.uid,
        "gid": coordinator.gid,
        "pid": coordinator.pid,
        "parent_pid": launcher.pid,
        "process_start_time_ticks": coordinator.start_ticks,
        "process_start_token_sha256": coordinator.start_token_sha256,
    }
    if receipt["coordinator_binding"] != expected_coordinator:
        _raise("HANDOFF_COORDINATOR_BINDING_MISMATCH", "Handoff coordinator binding changed")
    expected_execution = {
        "execution_plan_sha256": policy.execution_plan_sha256,
        "reviewed_job_sha256": policy.reviewed_job_sha256,
        "approval_basis_sha256": policy.approval_basis_sha256,
        "generation_id": policy.generation_id,
    }
    if receipt["execution_binding"] != expected_execution:
        _raise("HANDOFF_EXECUTION_BINDING_MISMATCH", "Handoff execution binding changed")
    expected_worker = policy.worker.as_dict()
    if receipt["worker_binding"] != expected_worker:
        _raise("HANDOFF_WORKER_BINDING_MISMATCH", "Handoff worker binding changed")
    _validate_worker_observation(worker_observation, policy.worker)


def _coerce_owned_socket(value: socket.socket | int, label: str) -> socket.socket:
    if isinstance(value, socket.socket):
        return value
    descriptor = _integer(value, f"{label} descriptor", minimum=0, maximum=2**31 - 1)
    try:
        return socket.socket(fileno=descriptor)
    except (OSError, ValueError):
        try:
            os.close(descriptor)
        except OSError:
            pass
        _raise("HANDOFF_SOCKET_INVALID", f"{label} descriptor is invalid")


def _deadline() -> int:
    return time.monotonic_ns() + MAX_HANDSHAKE_NS


def _wait(sock: socket.socket, event: int, deadline_ns: int) -> None:
    poller = select.poll()
    try:
        descriptor = sock.fileno()
    except (OSError, ValueError):
        _raise("HANDOFF_SOCKET_INVALID", "Handoff channel is closed")
    if descriptor < 0:
        _raise("HANDOFF_SOCKET_INVALID", "Handoff channel is closed")
    poller.register(descriptor, event | select.POLLERR | select.POLLHUP | select.POLLNVAL)
    while True:
        remaining = deadline_ns - time.monotonic_ns()
        if remaining <= 0:
            _raise("HANDOFF_DEADLINE_EXCEEDED", "Handoff deadline expired")
        try:
            ready = poller.poll(max(1, (remaining + 999_999) // 1_000_000))
        except InterruptedError:
            continue
        except OSError:
            _raise("HANDOFF_SOCKET_INVALID", "Handoff readiness could not be checked")
        if not ready:
            continue
        flags = ready[0][1]
        if flags & select.POLLNVAL:
            _raise("HANDOFF_SOCKET_INVALID", "Handoff channel became invalid")
        if flags & (event | select.POLLERR | select.POLLHUP):
            return


def _recvmsg(
    sock: socket.socket,
    size: int,
    ancillary_size: int,
    deadline_ns: int,
) -> tuple[bytes, Sequence[tuple[int, int, bytes]], int]:
    close_on_exec = getattr(socket, "MSG_CMSG_CLOEXEC", 0)
    nonblocking = getattr(socket, "MSG_DONTWAIT", 0)
    if not close_on_exec or not nonblocking:
        _raise(
            "HANDOFF_PLATFORM_UNSUPPORTED",
            "Atomic close-on-exec nonblocking receipt is unavailable",
        )
    flags = close_on_exec | nonblocking
    while True:
        _wait(sock, select.POLLIN, deadline_ns)
        try:
            data, ancillary, message_flags, _address = sock.recvmsg(size, ancillary_size, flags)
            return data, ancillary, message_flags
        except (BlockingIOError, InterruptedError):
            continue
        except OSError:
            _raise("HANDOFF_RECEIVE_FAILED", "Handoff bytes could not be received")


def _collect_rights(
    ancillary: Sequence[tuple[int, int, bytes]],
    descriptors: list[int],
) -> None:
    invalid = False
    for level, kind, payload in ancillary:
        if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS:
            invalid = True
            continue
        values = array.array("i")
        usable = len(payload) - (len(payload) % values.itemsize)
        if usable:
            values.frombytes(payload[:usable])
            descriptors.extend(values.tolist())
        if usable != len(payload):
            invalid = True
    if len(descriptors) > 1:
        invalid = True
    if invalid:
        _raise("HANDOFF_ANCILLARY_INVALID", "Handoff must contain exactly one descriptor")


def _receive_frame_and_one_fd(
    channel: socket.socket,
    deadline_ns: int,
) -> tuple[bytes, int]:
    """Private transport primitive; closes every received FD on any failure."""

    descriptors: list[int] = []
    ancillary_size = socket.CMSG_SPACE(2 * array.array("i").itemsize)
    header = bytearray()
    try:
        while len(header) < 4:
            data, ancillary, flags = _recvmsg(
                channel, 4 - len(header), ancillary_size, deadline_ns
            )
            _collect_rights(ancillary, descriptors)
            if flags & (socket.MSG_CTRUNC | getattr(socket, "MSG_TRUNC", 0)):
                _raise("HANDOFF_ANCILLARY_TRUNCATED", "Handoff control data was truncated")
            if not data:
                _raise("HANDOFF_FRAME_TRUNCATED", "Handoff frame header was truncated")
            header.extend(data)
        (length,) = struct.unpack("!I", bytes(header))
        if length == 0 or length > MAX_HANDOFF_FRAME_BYTES:
            _raise("HANDOFF_FRAME_SIZE_INVALID", "Handoff frame exceeds its byte limit")
        body = bytearray()
        while len(body) < length:
            data, ancillary, flags = _recvmsg(
                channel,
                min(length - len(body), _READ_CHUNK_BYTES),
                ancillary_size,
                deadline_ns,
            )
            _collect_rights(ancillary, descriptors)
            if flags & (socket.MSG_CTRUNC | getattr(socket, "MSG_TRUNC", 0)):
                _raise("HANDOFF_ANCILLARY_TRUNCATED", "Handoff control data was truncated")
            if not data:
                _raise("HANDOFF_FRAME_TRUNCATED", "Handoff frame body was truncated")
            body.extend(data)

        data, ancillary, flags = _recvmsg(channel, 1, ancillary_size, deadline_ns)
        _collect_rights(ancillary, descriptors)
        if flags & (socket.MSG_CTRUNC | getattr(socket, "MSG_TRUNC", 0)):
            _raise("HANDOFF_ANCILLARY_TRUNCATED", "Handoff trailing control data was truncated")
        if data:
            _raise("HANDOFF_TRAILING_DATA", "Handoff frame contains trailing bytes")
        if len(descriptors) != 1:
            _raise("HANDOFF_ANCILLARY_INVALID", "Handoff must contain exactly one descriptor")
        descriptor = descriptors.pop()
        return bytes(body), descriptor
    finally:
        for descriptor in descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _send_all(sock: socket.socket, payload: bytes, deadline_ns: int) -> None:
    nonblocking = getattr(socket, "MSG_DONTWAIT", 0)
    no_signal = getattr(socket, "MSG_NOSIGNAL", 0)
    if not nonblocking or not no_signal:
        _raise(
            "HANDOFF_PLATFORM_UNSUPPORTED",
            "Deadline-safe signal-safe writes are unavailable",
        )
    flags = nonblocking | no_signal
    offset = 0
    while offset < len(payload):
        _wait(sock, select.POLLOUT, deadline_ns)
        try:
            sent = sock.send(payload[offset:], flags)
        except (BlockingIOError, InterruptedError):
            continue
        except OSError:
            _raise("HANDOFF_SEND_FAILED", "Handoff bytes could not be sent")
        if sent <= 0:
            _raise("HANDOFF_SEND_FAILED", "Handoff bytes could not be sent")
        offset += sent


def _send_frame_with_fd(
    channel: socket.socket,
    raw: bytes,
    worker_fd: int,
    deadline_ns: int,
) -> None:
    frame = struct.pack("!I", len(raw)) + raw
    rights = array.array("i", [worker_fd])
    nonblocking = getattr(socket, "MSG_DONTWAIT", 0)
    no_signal = getattr(socket, "MSG_NOSIGNAL", 0)
    if not nonblocking or not no_signal:
        _raise(
            "HANDOFF_PLATFORM_UNSUPPORTED",
            "Deadline-safe signal-safe writes are unavailable",
        )
    flags = nonblocking | no_signal
    while True:
        _wait(channel, select.POLLOUT, deadline_ns)
        try:
            sent = channel.sendmsg(
                [frame],
                [(socket.SOL_SOCKET, socket.SCM_RIGHTS, rights.tobytes())],
                flags,
            )
            break
        except (BlockingIOError, InterruptedError):
            continue
        except OSError:
            _raise("HANDOFF_SEND_FAILED", "Handoff frame could not be sent")
    if sent <= 0:
        _raise("HANDOFF_SEND_FAILED", "Handoff frame could not be sent")
    if sent < len(frame):
        _send_all(channel, frame[sent:], deadline_ns)
    try:
        channel.shutdown(socket.SHUT_WR)
    except OSError:
        _raise("HANDOFF_SEND_FAILED", "Handoff EOF boundary could not be established")


_CONSTRUCTION_SENTINEL = object()


class AttestedWorkerHandoff:
    """Opaque owner of one launcher-attested connected worker socket.

    Normal construction is rejected.  See the module docstring for the limits
    of this in-process opacity and the required OS/service-manager trust.
    """

    __slots__ = ("_worker_socket", "_receipt_bytes", "_receipt_sha256")

    def __init__(
        self,
        sentinel: object,
        worker_socket: socket.socket,
        receipt_bytes: bytes,
    ) -> None:
        if sentinel is not _CONSTRUCTION_SENTINEL:
            raise TypeError("AttestedWorkerHandoff cannot be constructed directly")
        self._worker_socket: socket.socket | None = worker_socket
        self._receipt_bytes = receipt_bytes
        self._receipt_sha256 = hashlib.sha256(receipt_bytes).hexdigest()

    @property
    def receipt_sha256(self) -> str:
        return self._receipt_sha256

    @property
    def receipt_bytes(self) -> bytes:
        return self._receipt_bytes

    @property
    def receipt(self) -> dict[str, Any]:
        return decode_canonical_json(self._receipt_bytes)

    @property
    def worker_binding(self) -> dict[str, Any]:
        return dict(self.receipt["worker_binding"])

    def take_worker_socket(self) -> socket.socket:
        if self._worker_socket is None:
            _raise("HANDOFF_ALREADY_CONSUMED", "Attested worker handoff was already consumed")
        worker_socket = self._worker_socket
        self._worker_socket = None
        return worker_socket

    def close(self) -> None:
        if self._worker_socket is not None:
            self._worker_socket.close()
            self._worker_socket = None

    def __enter__(self) -> AttestedWorkerHandoff:
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.close()

    def __reduce__(self) -> Any:
        raise TypeError("AttestedWorkerHandoff cannot be serialized")

    def __del__(self) -> None:
        try:
            self.close()
        except BaseException:
            pass


def receive_attested_worker_handoff(
    handoff_channel: socket.socket | int,
    *,
    policy: LauncherHandoffPolicy,
) -> AttestedWorkerHandoff:
    """Consume a one-shot launcher channel and return one opaque handoff."""

    channel = _coerce_owned_socket(handoff_channel, "Handoff channel")
    worker_socket: socket.socket | None = None
    raw_worker_fd: int | None = None
    try:
        if not isinstance(policy, LauncherHandoffPolicy):
            _raise("HANDOFF_POLICY_INVALID", "Launcher handoff policy is invalid")
        _require_connected_unix_stream(channel, "Handoff channel")
        current_boot = current_host_boot_id_sha256()
        if current_boot != policy.host_boot_id_sha256:
            _raise("HANDOFF_HOST_BOOT_MISMATCH", "Host boot identity changed")
        coordinator_pid = os.getpid()
        coordinator_uid = os.geteuid()
        coordinator_gid = os.getegid()
        parent_pid = os.getppid()
        if policy.expected_launcher_uid == coordinator_uid:
            _raise(
                "HANDOFF_LAUNCHER_UID_NOT_PRIVILEGED",
                "Launcher UID must differ from coordinator effective UID",
            )
        if policy.expected_launcher_pid != parent_pid:
            _raise("HANDOFF_PARENT_MISMATCH", "Expected launcher is not coordinator parent")
        peer_pid, peer_uid, peer_gid = _peer_credentials(channel, "Launcher")
        if peer_pid != policy.expected_launcher_pid:
            _raise("HANDOFF_LAUNCHER_PEER_MISMATCH", "Launcher peer credentials changed")
        launcher_start = process_start_time_ticks(peer_pid)
        if launcher_start != policy.expected_launcher_process_start_time_ticks:
            _raise("HANDOFF_LAUNCHER_START_MISMATCH", "Launcher process start identity changed")
        launcher_token = process_start_token_sha256(current_boot, peer_pid, launcher_start)
        if launcher_token != policy.expected_launcher_process_start_token_sha256:
            _raise("HANDOFF_LAUNCHER_START_MISMATCH", "Launcher process token changed")
        if (peer_uid, peer_gid) != (
            policy.expected_launcher_uid,
            policy.expected_launcher_gid,
        ):
            _raise("HANDOFF_LAUNCHER_PEER_MISMATCH", "Launcher peer credentials changed")

        raw, raw_worker_fd = _receive_frame_and_one_fd(channel, _deadline())
        try:
            worker_socket = socket.socket(fileno=raw_worker_fd)
            raw_worker_fd = None
        except (OSError, ValueError):
            _raise("HANDOFF_WORKER_SOCKET_INVALID", "Transferred worker descriptor is invalid")
        observation = _observe_worker_socket(worker_socket, current_boot)
        receipt = _validate_receipt_shape(decode_canonical_json(raw))
        _validate_receipt_digest(raw, policy.expected_receipt_sha256)
        _validate_receipt_freshness(receipt, dt.datetime.now(dt.timezone.utc))
        launcher = _RuntimeIdentity(
            uid=peer_uid,
            gid=peer_gid,
            pid=peer_pid,
            start_ticks=launcher_start,
            start_token_sha256=launcher_token,
        )
        coordinator = _runtime_identity(
            coordinator_pid, coordinator_uid, coordinator_gid, current_boot
        )
        _validate_receipt_bindings(receipt, policy, launcher, coordinator, observation)
        handoff = AttestedWorkerHandoff(_CONSTRUCTION_SENTINEL, worker_socket, raw)
        worker_socket = None
        return handoff
    finally:
        if raw_worker_fd is not None:
            try:
                os.close(raw_worker_fd)
            except OSError:
                pass
        if worker_socket is not None:
            worker_socket.close()
        channel.close()


def send_attested_worker_handoff(
    handoff_channel: socket.socket | int,
    worker_socket: socket.socket | int,
    receipt: Mapping[str, Any],
) -> str:
    """Launcher-side one-shot send; consumes both supplied descriptors.

    The returned SHA-256 is the canonical receipt digest that the independently
    sealed coordinator policy must pin.  Returning it does not itself transport
    that policy or create provenance.
    """

    channel = _coerce_owned_socket(handoff_channel, "Handoff channel")
    try:
        worker = _coerce_owned_socket(worker_socket, "Worker socket")
        try:
            _require_connected_unix_stream(channel, "Handoff channel")
            current_boot = current_host_boot_id_sha256()
            validated = _validate_receipt_shape(dict(receipt))
            _validate_receipt_freshness(validated, dt.datetime.now(dt.timezone.utc))
            launcher = _runtime_identity(os.getpid(), os.geteuid(), os.getegid(), current_boot)
            launcher_binding = validated["launcher_binding"]
            if launcher_binding != {
                "uid": launcher.uid,
                "gid": launcher.gid,
                "pid": launcher.pid,
                "process_start_time_ticks": launcher.start_ticks,
                "process_start_token_sha256": launcher.start_token_sha256,
            }:
                _raise("HANDOFF_LAUNCHER_BINDING_MISMATCH", "Receipt does not bind this launcher")
            coordinator = validated["coordinator_binding"]
            if coordinator["parent_pid"] != launcher.pid:
                _raise("HANDOFF_PARENT_MISMATCH", "Receipt coordinator is not launcher child")
            parent_pid, coordinator_start = _process_stat(coordinator["pid"])
            coordinator_uid, coordinator_gid = _process_effective_credentials(coordinator["pid"])
            if parent_pid != launcher.pid or coordinator_start != coordinator["process_start_time_ticks"]:
                _raise("HANDOFF_COORDINATOR_BINDING_MISMATCH", "Coordinator process identity changed")
            if (coordinator_uid, coordinator_gid) != (coordinator["uid"], coordinator["gid"]):
                _raise("HANDOFF_COORDINATOR_BINDING_MISMATCH", "Coordinator credentials changed")
            if coordinator["process_start_token_sha256"] != process_start_token_sha256(
                current_boot, coordinator["pid"], coordinator_start
            ):
                _raise("HANDOFF_COORDINATOR_BINDING_MISMATCH", "Coordinator process token changed")
            if coordinator_uid == launcher.uid:
                _raise(
                    "HANDOFF_LAUNCHER_UID_NOT_PRIVILEGED",
                    "Launcher UID must differ from coordinator effective UID",
                )
            if validated["host_boot_id_sha256"] != current_boot:
                _raise("HANDOFF_HOST_BOOT_MISMATCH", "Receipt host boot identity changed")
            expected_worker = ExpectedWorkerBinding(**dict(validated["worker_binding"]))
            observation = _observe_worker_socket(worker, current_boot)
            _validate_worker_observation(observation, expected_worker)
            _validate_handoff_channel_peer(channel, coordinator, current_boot)
            raw = canonical_json_bytes(validated)
            _send_frame_with_fd(channel, raw, worker.fileno(), _deadline())
            return hashlib.sha256(raw).hexdigest()
        finally:
            worker.close()
    finally:
        channel.close()


__all__ = [
    "AttestedWorkerHandoff",
    "ExpectedWorkerBinding",
    "HANDOFF_PROTOCOL",
    "HANDOFF_RECEIPT_SCHEMA",
    "LauncherHandoffError",
    "LauncherHandoffPolicy",
    "MAX_HANDOFF_FRAME_BYTES",
    "WORKER_PROTOCOL_REVISION",
    "canonical_json_bytes",
    "current_host_boot_id_sha256",
    "decode_canonical_json",
    "process_start_time_ticks",
    "process_start_token_sha256",
    "receive_attested_worker_handoff",
    "send_attested_worker_handoff",
    "socket_inode_binding_sha256",
]
