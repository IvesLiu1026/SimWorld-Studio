"""Pathless, append-only durable idempotency ledger.

The caller supplies an already-open directory file descriptor.  This module
duplicates that descriptor with ``CLOEXEC``, verifies that it names a private
owned directory, and performs every filesystem operation relative to the
descriptor.  It never accepts or resolves a directory path.

Each idempotency key has two immutable records.  A prepare record is published
before execution and a result record is published after execution.  Publication
uses ``fsync(temp) -> link(no-replace) -> unlink(temp) -> fsync(directory)``.
Consequently an observed prepare without a result is always ambiguous and is
never converted into permission for a blind retry.
"""

from __future__ import annotations

import dataclasses
import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import threading
import time
import unicodedata
from typing import Any, Literal


PREPARE_SCHEMA = "simworld-semantic-index-ledger-prepare/v1"
RESULT_SCHEMA = "simworld-semantic-index-ledger-result/v1"

MAX_CANONICAL_JSON_BYTES = 2 * 1024 * 1024
MAX_RESULT_BYTES = 1024 * 1024
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 200_000
MAX_CONTAINER_ITEMS = 100_000
MAX_STRING_BYTES = 1024 * 1024
MAX_JSON_INTEGER = 2**53 - 1
PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
MAX_LINK_SETTLE_ATTEMPTS = 100
LINK_SETTLE_SECONDS = 0.001

_KEY_RE = re.compile(r"\Asha256:[a-f0-9]{64}\Z")
_SHA256_RE = re.compile(r"\A[a-f0-9]{64}\Z")
_LEDGER_IDENTITY_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,159}\Z")
_GENERATION_ID_RE = re.compile(r"\Asemantic-generation:[a-f0-9]{64}\Z")
_TARGET_IDENTITY_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,159}\Z")
_PHASE_OPERATIONS = {
    "inspect": "inspect_exact_assets",
    "render": "render_exact_views",
    "caption": "caption_exact_render_set",
    "embed": "embed_exact_text_set",
    "postgres": "upsert_postgres_exact",
    "qdrant": "upsert_qdrant_exact",
    "reconcile": "reconcile_exact_snapshot",
}
_TARGET_COUNTS = {
    operation: (3 if phase == "reconcile" else 1)
    for phase, operation in _PHASE_OPERATIONS.items()
}
_BINDING_KEYS = frozenset(
    {
        "schema",
        "job_revision",
        "generation_id",
        "phase",
        "operation",
        "idempotency_key",
        "credential_targets",
    }
)
_TARGET_KEYS = frozenset({"credential_generation", "target_identity"})
_PREPARE_KEYS = frozenset(
    {"schema", "ledger_identity", "ledger_key", "binding", "request_sha256"}
)
_RESULT_KEYS = frozenset(
    {
        "schema",
        "ledger_identity",
        "ledger_key",
        "binding",
        "request_sha256",
        "result_sha256",
        "result",
    }
)

PUBLIC_ERROR_MESSAGES = {
    "LEDGER_CLOSED": "Durable ledger is closed",
    "LEDGER_INPUT_INVALID": "Durable ledger input is invalid",
    "LEDGER_DIRECTORY_UNSAFE": "Durable ledger directory is unsafe",
    "LEDGER_ENTRY_UNSAFE": "Durable ledger entry is unsafe",
    "LEDGER_ENTRY_CORRUPT": "Durable ledger entry is corrupt",
    "LEDGER_KEY_CONFLICT": "Durable ledger idempotency key conflict",
    "LEDGER_RESULT_CONFLICT": "Durable ledger result conflict",
    "LEDGER_PREPARE_REQUIRED": "Durable ledger prepare record is required",
    "LEDGER_IO_ERROR": "Durable ledger I/O failed",
    "LEDGER_UNSUPPORTED": "Durable ledger platform support is unavailable",
}


@dataclasses.dataclass(frozen=True)
class LedgerError(Exception):
    """Fixed public error that never contains request or result data."""

    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


@dataclasses.dataclass(frozen=True)
class BeginOutcome:
    """Outcome of a durable ``begin`` operation."""

    action: Literal["execute", "replay", "recovery_required"]
    ledger_key: str
    request_sha256: str
    result_bytes: bytes | None = None

    @property
    def status(self) -> str:
        return self.action


@dataclasses.dataclass(frozen=True)
class CredentialTarget:
    credential_generation: str
    target_identity: str


@dataclasses.dataclass(frozen=True)
class LedgerBinding:
    """Closed tuple that defines one production idempotency namespace."""

    job_revision: str
    generation_id: str
    phase: str
    operation: str
    idempotency_key: str
    credential_targets: tuple[CredentialTarget, ...]


class _JsonRejected(Exception):
    pass


@dataclasses.dataclass
class _JsonBudget:
    nodes: int = 0
    approximate_bytes: int = 0

    def add_node(self, approximate_bytes: int = 1) -> None:
        self.nodes += 1
        self.approximate_bytes += approximate_bytes
        if self.nodes > MAX_JSON_NODES or self.approximate_bytes > MAX_CANONICAL_JSON_BYTES:
            raise _JsonRejected


def _fail(code: str) -> None:
    raise LedgerError(code=code, message=PUBLIC_ERROR_MESSAGES[code])


def _validate_text(value: Any, *, maximum_bytes: int) -> str:
    if type(value) is not str:
        raise _JsonRejected
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeError as error:
        raise _JsonRejected from error
    if len(encoded) > maximum_bytes or unicodedata.normalize("NFC", value) != value:
        raise _JsonRejected
    if any(unicodedata.category(character) in {"Cc", "Cs"} for character in value):
        raise _JsonRejected
    return value


def _quoted_text(value: str, budget: _JsonBudget) -> str:
    _validate_text(value, maximum_bytes=MAX_STRING_BYTES)
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    budget.approximate_bytes += len(encoded.encode("utf-8", "strict"))
    if budget.approximate_bytes > MAX_CANONICAL_JSON_BYTES:
        raise _JsonRejected
    return encoded


def _canonical_text(value: Any, budget: _JsonBudget, depth: int) -> str:
    if depth > MAX_JSON_DEPTH:
        raise _JsonRejected
    budget.add_node()
    if value is None:
        return "null"
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is int:
        if value < -MAX_JSON_INTEGER or value > MAX_JSON_INTEGER:
            raise _JsonRejected
        rendered = str(value)
        budget.approximate_bytes += len(rendered)
        return rendered
    if type(value) is str:
        return _quoted_text(value, budget)
    if type(value) is list:
        if len(value) > MAX_CONTAINER_ITEMS:
            raise _JsonRejected
        rendered = [
            _canonical_text(item, budget, depth + 1)
            for item in value
        ]
        budget.approximate_bytes += len(rendered) + 1
        if budget.approximate_bytes > MAX_CANONICAL_JSON_BYTES:
            raise _JsonRejected
        return "[" + ",".join(rendered) + "]"
    if type(value) is dict:
        if len(value) > MAX_CONTAINER_ITEMS:
            raise _JsonRejected
        keys: list[str] = []
        for key in value:
            keys.append(_validate_text(key, maximum_bytes=MAX_STRING_BYTES))
        keys.sort(key=lambda item: item.encode("utf-16-be", "strict"))
        rendered = [
            _quoted_text(key, budget)
            + ":"
            + _canonical_text(value[key], budget, depth + 1)
            for key in keys
        ]
        budget.approximate_bytes += len(rendered) + 1
        if budget.approximate_bytes > MAX_CANONICAL_JSON_BYTES:
            raise _JsonRejected
        return "{" + ",".join(rendered) + "}"
    raise _JsonRejected


def _canonical_bytes(value: Any, *, error_code: str) -> bytes:
    try:
        raw = _canonical_text(value, _JsonBudget(), 0).encode("utf-8", "strict")
    except (UnicodeError, ValueError, RecursionError, _JsonRejected):
        _fail(error_code)
    if not raw or len(raw) > MAX_CANONICAL_JSON_BYTES:
        _fail(error_code)
    return raw


def canonical_json_bytes(value: Any) -> bytes:
    """Encode the ledger's bounded, integer-only RFC 8785 JSON domain."""

    return _canonical_bytes(value, error_code="LEDGER_INPUT_INVALID")


def _decode_canonical_json(raw: bytes, *, maximum: int, error_code: str) -> Any:
    if type(raw) is not bytes or not raw or len(raw) > maximum:
        _fail(error_code)

    def reject_number(_value: str) -> Any:
        raise _JsonRejected

    def parse_integer(value: str) -> int:
        if len(value) > 20:
            raise _JsonRejected
        parsed = int(value)
        if parsed < -MAX_JSON_INTEGER or parsed > MAX_JSON_INTEGER:
            raise _JsonRejected
        return parsed

    def closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        if len(pairs) > MAX_CONTAINER_ITEMS:
            raise _JsonRejected
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise _JsonRejected
            value[key] = item
        return value

    try:
        text = raw.decode("utf-8", "strict")
        value = json.loads(
            text,
            object_pairs_hook=closed_object,
            parse_int=parse_integer,
            parse_float=reject_number,
            parse_constant=reject_number,
        )
    except (UnicodeError, ValueError, RecursionError, json.JSONDecodeError, _JsonRejected):
        _fail(error_code)
    if _canonical_bytes(value, error_code=error_code) != raw:
        _fail(error_code)
    return value


def canonical_request_sha256(canonical_request: bytes) -> str:
    """Validate canonical request bytes and return their lowercase SHA-256."""

    _decode_canonical_json(
        canonical_request,
        maximum=MAX_CANONICAL_JSON_BYTES,
        error_code="LEDGER_INPUT_INVALID",
    )
    return hashlib.sha256(canonical_request).hexdigest()


def _validated_key(value: Any) -> str:
    if type(value) is not str or _KEY_RE.fullmatch(value) is None:
        _fail("LEDGER_INPUT_INVALID")
    return value


def _validated_sha256(value: Any) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        _fail("LEDGER_INPUT_INVALID")
    return value


def _validated_identity(value: Any) -> str:
    try:
        _validate_text(value, maximum_bytes=160)
    except _JsonRejected:
        _fail("LEDGER_INPUT_INVALID")
    if _LEDGER_IDENTITY_RE.fullmatch(value) is None:
        _fail("LEDGER_INPUT_INVALID")
    return value


def _validated_target_identity(value: Any) -> str:
    try:
        _validate_text(value, maximum_bytes=160)
    except _JsonRejected:
        _fail("LEDGER_INPUT_INVALID")
    if _TARGET_IDENTITY_RE.fullmatch(value) is None:
        _fail("LEDGER_INPUT_INVALID")
    return value


def _binding_value(binding: LedgerBinding) -> dict[str, Any]:
    if type(binding) is not LedgerBinding:
        _fail("LEDGER_INPUT_INVALID")
    if type(binding.job_revision) is not str or _KEY_RE.fullmatch(binding.job_revision) is None:
        _fail("LEDGER_INPUT_INVALID")
    if (
        type(binding.generation_id) is not str
        or _GENERATION_ID_RE.fullmatch(binding.generation_id) is None
        or type(binding.phase) is not str
        or type(binding.operation) is not str
        or _PHASE_OPERATIONS.get(binding.phase) != binding.operation
    ):
        _fail("LEDGER_INPUT_INVALID")
    _validated_key(binding.idempotency_key)
    if (
        type(binding.credential_targets) is not tuple
        or len(binding.credential_targets) != _TARGET_COUNTS[binding.operation]
    ):
        _fail("LEDGER_INPUT_INVALID")
    targets: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for target in binding.credential_targets:
        if type(target) is not CredentialTarget:
            _fail("LEDGER_INPUT_INVALID")
        _validated_key(target.credential_generation)
        _validated_target_identity(target.target_identity)
        identity = (target.credential_generation, target.target_identity)
        if identity in seen:
            _fail("LEDGER_INPUT_INVALID")
        seen.add(identity)
        targets.append(
            {
                "credential_generation": target.credential_generation,
                "target_identity": target.target_identity,
            }
        )
    return {
        "schema": "simworld-semantic-index-ledger-binding/v1",
        "job_revision": binding.job_revision,
        "generation_id": binding.generation_id,
        "phase": binding.phase,
        "operation": binding.operation,
        "idempotency_key": binding.idempotency_key,
        "credential_targets": targets,
    }


def _binding_from_value(value: Any, *, error_code: str) -> LedgerBinding:
    try:
        if type(value) is not dict or set(value) != _BINDING_KEYS:
            _fail("LEDGER_INPUT_INVALID")
        if value.get("schema") != "simworld-semantic-index-ledger-binding/v1":
            _fail("LEDGER_INPUT_INVALID")
        raw_targets = value.get("credential_targets")
        if type(raw_targets) is not list:
            _fail("LEDGER_INPUT_INVALID")
        targets: list[CredentialTarget] = []
        for target in raw_targets:
            if type(target) is not dict or set(target) != _TARGET_KEYS:
                _fail("LEDGER_INPUT_INVALID")
            targets.append(
                CredentialTarget(
                    credential_generation=target["credential_generation"],
                    target_identity=target["target_identity"],
                )
            )
        binding = LedgerBinding(
            job_revision=value["job_revision"],
            generation_id=value["generation_id"],
            phase=value["phase"],
            operation=value["operation"],
            idempotency_key=value["idempotency_key"],
            credential_targets=tuple(targets),
        )
        _binding_value(binding)
        return binding
    except (KeyError, LedgerError):
        _fail(error_code)


def derive_ledger_key(binding: LedgerBinding) -> str:
    """Derive the closed storage key from the complete ordered binding tuple."""

    material = _canonical_bytes(
        _binding_value(binding), error_code="LEDGER_INPUT_INVALID"
    )
    return "sha256:" + hashlib.sha256(material).hexdigest()


def _binding_from_request_value(value: Any) -> LedgerBinding:
    try:
        required = {
            "schema",
            "protocol",
            "job_revision",
            "generation_id",
            "phase",
            "operation",
            "idempotency_key",
            "expected_targets",
            "credential_transport",
        }
        if type(value) is not dict or not required.issubset(value):
            _fail("LEDGER_INPUT_INVALID")
        if (
            value["schema"] != "simworld-semantic-index-phase-request/v1"
            or value["protocol"] != "simworld-semantic-index-worker/v1"
            or type(value["expected_targets"]) is not list
            or type(value["credential_transport"]) is not dict
            or type(value["credential_transport"].get("descriptors")) is not list
        ):
            _fail("LEDGER_INPUT_INVALID")
        expected_targets = value["expected_targets"]
        descriptors = value["credential_transport"]["descriptors"]
        if len(descriptors) != len(expected_targets):
            _fail("LEDGER_INPUT_INVALID")
        targets: list[CredentialTarget] = []
        for index, (target, descriptor) in enumerate(
            zip(expected_targets, descriptors)
        ):
            target_fields = {
                "credential_generation",
                "generation_id",
                "target_identity",
            }
            descriptor_fields = target_fields | {"fd_index"}
            if (
                type(target) is not dict
                or not target_fields.issubset(target)
                or type(descriptor) is not dict
                or not descriptor_fields.issubset(descriptor)
                or descriptor["fd_index"] != index
                or target["generation_id"] != value["generation_id"]
                or descriptor["generation_id"] != value["generation_id"]
                or descriptor["credential_generation"]
                != target["credential_generation"]
                or descriptor["target_identity"] != target["target_identity"]
            ):
                _fail("LEDGER_INPUT_INVALID")
            targets.append(
                CredentialTarget(
                    credential_generation=target["credential_generation"],
                    target_identity=target["target_identity"],
                )
            )
        binding = LedgerBinding(
            job_revision=value["job_revision"],
            generation_id=value["generation_id"],
            phase=value["phase"],
            operation=value["operation"],
            idempotency_key=value["idempotency_key"],
            credential_targets=tuple(targets),
        )
        _binding_value(binding)
        return binding
    except (KeyError, LedgerError):
        _fail("LEDGER_INPUT_INVALID")


def _request_context(canonical_request: bytes) -> tuple[LedgerBinding, str]:
    value = _decode_canonical_json(
        canonical_request,
        maximum=MAX_CANONICAL_JSON_BYTES,
        error_code="LEDGER_INPUT_INVALID",
    )
    return _binding_from_request_value(value), hashlib.sha256(canonical_request).hexdigest()


def binding_from_canonical_request(canonical_request: bytes) -> LedgerBinding:
    """Extract and cross-check the complete ledger tuple from a phase request."""

    binding, _request_sha256 = _request_context(canonical_request)
    return binding


def _entry_names(ledger_key: str) -> tuple[str, str]:
    digest = _validated_key(ledger_key).removeprefix("sha256:")
    return f"ledger-{digest}.prepare.json", f"ledger-{digest}.result.json"


class DurableIdempotencyLedger:
    """Append-only ledger rooted at an owned duplicate of a directory FD."""

    # The worker service accepts this exact type.  Keep the instance surface
    # closed so a caller cannot replace ``begin`` or ``commit_result`` with
    # instance attributes while still passing an exact-type check.
    __slots__ = (
        "ledger_identity",
        "expected_uid",
        "expected_gid",
        "_directory_fd",
        "_closed",
        "_lifecycle_lock",
    )

    def __init__(
        self,
        directory_fd: int,
        ledger_identity: str,
        *,
        expected_uid: int | None = None,
        expected_gid: int | None = None,
    ) -> None:
        self.ledger_identity = _validated_identity(ledger_identity)
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
            _fail("LEDGER_INPUT_INVALID")
        required = ("O_NOFOLLOW", "O_CLOEXEC", "O_DIRECTORY")
        if not all(hasattr(os, name) for name in required) or not hasattr(
            fcntl, "F_DUPFD_CLOEXEC"
        ):
            _fail("LEDGER_UNSUPPORTED")
        try:
            owned_fd = fcntl.fcntl(directory_fd, fcntl.F_DUPFD_CLOEXEC, 3)
        except OSError:
            _fail("LEDGER_DIRECTORY_UNSAFE")
        self._directory_fd = owned_fd
        self._closed = False
        self._lifecycle_lock = threading.RLock()
        try:
            self._verify_directory()
        except BaseException:
            os.close(self._directory_fd)
            self._closed = True
            raise

    def __enter__(self) -> "DurableIdempotencyLedger":
        self._ensure_open()
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.close()

    def _ensure_open(self) -> None:
        if self._closed:
            _fail("LEDGER_CLOSED")

    def fileno(self) -> int:
        with self._lifecycle_lock:
            self._ensure_open()
            return self._directory_fd

    def close(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                return
            descriptor = self._directory_fd
            self._closed = True
            self._directory_fd = -1
            try:
                os.close(descriptor)
            except OSError:
                _fail("LEDGER_IO_ERROR")

    def _verify_directory(self) -> None:
        self._ensure_open()
        try:
            metadata = os.fstat(self._directory_fd)
        except OSError:
            _fail("LEDGER_DIRECTORY_UNSAFE")
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != self.expected_uid
            or metadata.st_gid != self.expected_gid
            or stat.S_IMODE(metadata.st_mode) != PRIVATE_DIRECTORY_MODE
        ):
            _fail("LEDGER_DIRECTORY_UNSAFE")

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
            _fail("LEDGER_ENTRY_UNSAFE")

    def _settled_read_metadata(self, descriptor: int) -> os.stat_result:
        """Wait briefly for a concurrent publisher to drop its temp hard link."""

        for attempt in range(MAX_LINK_SETTLE_ATTEMPTS + 1):
            try:
                metadata = os.fstat(descriptor)
            except OSError:
                _fail("LEDGER_ENTRY_UNSAFE")
            if metadata.st_nlink not in {1, 2}:
                _fail("LEDGER_ENTRY_UNSAFE")
            self._verify_file_metadata(
                metadata, expected_links=metadata.st_nlink
            )
            if metadata.st_nlink == 1:
                return metadata
            # A legitimate no-replace publisher has a two-link window between
            # link(final) and unlink(temp).  A persistent same-UID alias fails
            # closed after this small bounded grace period.
            if attempt == MAX_LINK_SETTLE_ATTEMPTS:
                _fail("LEDGER_ENTRY_UNSAFE")
            time.sleep(LINK_SETTLE_SECONDS)
        _fail("LEDGER_ENTRY_UNSAFE")

    def _read_entry(self, name: str) -> bytes | None:
        flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            descriptor = os.open(name, flags, dir_fd=self._directory_fd)
        except FileNotFoundError:
            return None
        except OSError as error:
            if error.errno in {errno.ELOOP, errno.EISDIR, errno.ENXIO}:
                _fail("LEDGER_ENTRY_UNSAFE")
            _fail("LEDGER_IO_ERROR")
        try:
            before = self._settled_read_metadata(descriptor)
            if before.st_size <= 0 or before.st_size > MAX_CANONICAL_JSON_BYTES:
                _fail("LEDGER_ENTRY_CORRUPT")
            chunks: list[bytes] = []
            total = 0
            while True:
                try:
                    chunk = os.read(
                        descriptor,
                        min(64 * 1024, MAX_CANONICAL_JSON_BYTES + 1 - total),
                    )
                except InterruptedError:
                    continue
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_CANONICAL_JSON_BYTES:
                    _fail("LEDGER_ENTRY_CORRUPT")
            after = os.fstat(descriptor)
            self._verify_file_metadata(after, expected_links=1)
            if (
                before.st_dev != after.st_dev
                or before.st_ino != after.st_ino
                or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
                or total != after.st_size
            ):
                _fail("LEDGER_ENTRY_CORRUPT")
            return b"".join(chunks)
        except LedgerError:
            raise
        except OSError:
            _fail("LEDGER_IO_ERROR")
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass

    def _fsync_directory(self) -> None:
        try:
            os.fsync(self._directory_fd)
        except OSError:
            _fail("LEDGER_IO_ERROR")

    def _publish_no_replace(self, final_name: str, raw: bytes) -> bool:
        if type(raw) is not bytes or not raw or len(raw) > MAX_CANONICAL_JSON_BYTES:
            _fail("LEDGER_INPUT_INVALID")
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
                    _fail("LEDGER_IO_ERROR")
            if descriptor < 0:
                _fail("LEDGER_IO_ERROR")
            os.fchmod(descriptor, PRIVATE_FILE_MODE)
            offset = 0
            while offset < len(raw):
                try:
                    written = os.write(descriptor, raw[offset:])
                except InterruptedError:
                    continue
                if written <= 0:
                    _fail("LEDGER_IO_ERROR")
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
                _fail("LEDGER_IO_ERROR")
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
                _fail("LEDGER_ENTRY_UNSAFE")
            os.unlink(temp_name, dir_fd=self._directory_fd)
            temp_name = ""
            self._fsync_directory()
            return True
        except LedgerError:
            raise
        except OSError:
            _fail("LEDGER_IO_ERROR")
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
                except (OSError, LedgerError):
                    pass

    def _prepare_record(self, raw: bytes) -> tuple[dict[str, Any], LedgerBinding]:
        record = _decode_canonical_json(
            raw,
            maximum=MAX_CANONICAL_JSON_BYTES,
            error_code="LEDGER_ENTRY_CORRUPT",
        )
        if type(record) is not dict or set(record) != _PREPARE_KEYS:
            _fail("LEDGER_ENTRY_CORRUPT")
        if record.get("schema") != PREPARE_SCHEMA:
            _fail("LEDGER_ENTRY_CORRUPT")
        if record.get("ledger_identity") != self.ledger_identity:
            _fail("LEDGER_ENTRY_CORRUPT")
        try:
            _validated_key(record.get("ledger_key"))
            _validated_sha256(record.get("request_sha256"))
        except LedgerError:
            _fail("LEDGER_ENTRY_CORRUPT")
        binding = _binding_from_value(
            record.get("binding"), error_code="LEDGER_ENTRY_CORRUPT"
        )
        if derive_ledger_key(binding) != record["ledger_key"]:
            _fail("LEDGER_ENTRY_CORRUPT")
        return record, binding

    def _result_record(
        self, raw: bytes
    ) -> tuple[dict[str, Any], LedgerBinding, bytes]:
        record = _decode_canonical_json(
            raw,
            maximum=MAX_CANONICAL_JSON_BYTES,
            error_code="LEDGER_ENTRY_CORRUPT",
        )
        if type(record) is not dict or set(record) != _RESULT_KEYS:
            _fail("LEDGER_ENTRY_CORRUPT")
        if record.get("schema") != RESULT_SCHEMA:
            _fail("LEDGER_ENTRY_CORRUPT")
        if record.get("ledger_identity") != self.ledger_identity:
            _fail("LEDGER_ENTRY_CORRUPT")
        try:
            _validated_key(record.get("ledger_key"))
            _validated_sha256(record.get("request_sha256"))
            _validated_sha256(record.get("result_sha256"))
        except LedgerError:
            _fail("LEDGER_ENTRY_CORRUPT")
        binding = _binding_from_value(
            record.get("binding"), error_code="LEDGER_ENTRY_CORRUPT"
        )
        if derive_ledger_key(binding) != record["ledger_key"]:
            _fail("LEDGER_ENTRY_CORRUPT")
        result_bytes = _canonical_bytes(
            record["result"], error_code="LEDGER_ENTRY_CORRUPT"
        )
        if (
            len(result_bytes) > MAX_RESULT_BYTES
            or hashlib.sha256(result_bytes).hexdigest() != record["result_sha256"]
        ):
            _fail("LEDGER_ENTRY_CORRUPT")
        return record, binding, result_bytes

    def _validate_binding(
        self,
        record: dict[str, Any],
        recorded_binding: LedgerBinding,
        expected_binding: LedgerBinding,
        ledger_key: str,
        request_sha256: str,
        *,
        conflict: bool,
    ) -> None:
        if record["ledger_key"] != ledger_key:
            _fail("LEDGER_ENTRY_CORRUPT")
        if _binding_value(recorded_binding) != _binding_value(expected_binding):
            _fail("LEDGER_ENTRY_CORRUPT")
        if record["request_sha256"] != request_sha256:
            _fail("LEDGER_KEY_CONFLICT" if conflict else "LEDGER_ENTRY_CORRUPT")

    def begin(self, canonical_request: bytes) -> BeginOutcome:
        """Prepare a key or return an exact replay/recovery decision.

        The request is validated as bounded canonical JSON and hashed here; an
        externally supplied digest is never trusted as a substitute.  The
        complete ledger tuple is extracted from that same request, including
        cross-checks between ordered targets and credential descriptors.
        """

        with self._lifecycle_lock:
            self._verify_directory()
            binding, request_digest = _request_context(canonical_request)
            binding_value = _binding_value(binding)
            ledger_key = derive_ledger_key(binding)
            prepare_name, result_name = _entry_names(ledger_key)
            prepare_raw = self._read_entry(prepare_name)
            won_prepare = False
            if prepare_raw is None:
                if self._read_entry(result_name) is not None:
                    _fail("LEDGER_ENTRY_CORRUPT")
                prepare_raw = _canonical_bytes(
                    {
                        "schema": PREPARE_SCHEMA,
                        "ledger_identity": self.ledger_identity,
                        "ledger_key": ledger_key,
                        "binding": binding_value,
                        "request_sha256": request_digest,
                    },
                    error_code="LEDGER_INPUT_INVALID",
                )
                won_prepare = self._publish_no_replace(prepare_name, prepare_raw)
                if not won_prepare:
                    prepare_raw = self._read_entry(prepare_name)
                    if prepare_raw is None:
                        _fail("LEDGER_ENTRY_CORRUPT")

            prepare, recorded_binding = self._prepare_record(prepare_raw)
            self._validate_binding(
                prepare,
                recorded_binding,
                binding,
                ledger_key,
                request_digest,
                conflict=True,
            )
            result_raw = self._read_entry(result_name)
            if result_raw is None:
                # A prior publisher may have made the prepare link visible but
                # failed its directory fsync.  Never return an authoritative
                # disposition until this held directory FD confirms the entry.
                self._fsync_directory()
                return BeginOutcome(
                    action="execute" if won_prepare else "recovery_required",
                    ledger_key=ledger_key,
                    request_sha256=request_digest,
                )
            result, result_binding, result_bytes = self._result_record(result_raw)
            self._validate_binding(
                result,
                result_binding,
                binding,
                ledger_key,
                request_digest,
                conflict=False,
            )
            # This also repairs the durability-uncertain case where the result
            # link became visible before a previous directory fsync failed.
            self._fsync_directory()
            return BeginOutcome(
                action="replay",
                ledger_key=ledger_key,
                request_sha256=request_digest,
                result_bytes=result_bytes,
            )

    def commit_result(
        self,
        canonical_request: bytes,
        canonical_result: bytes,
    ) -> bytes:
        """Publish an immutable canonical result for a matching prepare."""

        with self._lifecycle_lock:
            self._verify_directory()
            binding, request_digest = _request_context(canonical_request)
            binding_value = _binding_value(binding)
            ledger_key = derive_ledger_key(binding)
            result_value = _decode_canonical_json(
                canonical_result,
                maximum=MAX_RESULT_BYTES,
                error_code="LEDGER_INPUT_INVALID",
            )
            result_digest = hashlib.sha256(canonical_result).hexdigest()
            prepare_name, result_name = _entry_names(ledger_key)
            prepare_raw = self._read_entry(prepare_name)
            if prepare_raw is None:
                if self._read_entry(result_name) is not None:
                    _fail("LEDGER_ENTRY_CORRUPT")
                _fail("LEDGER_PREPARE_REQUIRED")
            prepare, recorded_binding = self._prepare_record(prepare_raw)
            self._validate_binding(
                prepare,
                recorded_binding,
                binding,
                ledger_key,
                request_digest,
                conflict=True,
            )
            wrapper = _canonical_bytes(
                {
                    "schema": RESULT_SCHEMA,
                    "ledger_identity": self.ledger_identity,
                    "ledger_key": ledger_key,
                    "binding": binding_value,
                    "request_sha256": request_digest,
                    "result_sha256": result_digest,
                    "result": result_value,
                },
                error_code="LEDGER_INPUT_INVALID",
            )
            published = self._publish_no_replace(result_name, wrapper)
            if published:
                return canonical_result
            existing_raw = self._read_entry(result_name)
            if existing_raw is None:
                _fail("LEDGER_ENTRY_CORRUPT")
            existing, existing_binding, existing_result = self._result_record(
                existing_raw
            )
            self._validate_binding(
                existing,
                existing_binding,
                binding,
                ledger_key,
                request_digest,
                conflict=False,
            )
            if (
                existing["result_sha256"] != result_digest
                or existing_result != canonical_result
            ):
                _fail("LEDGER_RESULT_CONFLICT")
            self._fsync_directory()
            return existing_result


__all__ = [
    "BeginOutcome",
    "CredentialTarget",
    "DurableIdempotencyLedger",
    "LedgerBinding",
    "LedgerError",
    "MAX_CANONICAL_JSON_BYTES",
    "MAX_RESULT_BYTES",
    "PREPARE_SCHEMA",
    "PUBLIC_ERROR_MESSAGES",
    "RESULT_SCHEMA",
    "binding_from_canonical_request",
    "canonical_json_bytes",
    "canonical_request_sha256",
    "derive_ledger_key",
]
