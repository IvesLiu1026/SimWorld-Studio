"""Pure-stdlib semantic validation for sealed semantic-index v2 documents.

JSON Schema validation is necessary but cannot establish relationships between
independently sealed documents.  This module performs those relationships
without ambient configuration or third-party imports.  Every raw document is
hashed independently before it is parsed, canonical JSON is required, and
errors contain only fixed public text and allowlisted JSON paths.

The validator deliberately accepts the Production approval basis as a separate
sealed document.  ``derive_expected_approval_basis`` is an explicit,
field-by-field constructor; callers cannot provide exclusion paths or a generic
projection policy.
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
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import unquote_to_bytes

from semantic_index_schema_runtime import (
    SchemaRuntimeError,
    validate_instance as validate_schema_instance,
    validate_schema as validate_schema_definition,
)


MAX_DOCUMENT_BYTES = 16 * 1024 * 1024
MAX_JSON_DEPTH = 64
MAX_JSON_CONTAINER_ITEMS = 150_000
MAX_JSON_STRING_BYTES = 1 * 1024 * 1024
MAX_JSON_INTEGER = 2**63 - 1
MAX_PERCENT_DECODE_PASSES = 8
DEFAULT_CLOCK_SKEW_SECONDS = 60
DEFAULT_LIVE_AUDIT_TTL_SECONDS = 15 * 60
MAX_CLOCK_SKEW_SECONDS = 5 * 60
MAX_LIVE_AUDIT_TTL_SECONDS = 60 * 60

APPROVAL_KINDS = (
    "data_owner_review",
    "cost_owner",
    "admin_state_change",
    "deployment",
    "runtime",
    "rollback_readiness",
)
PHASES = ("inspect", "render", "caption", "embed", "postgres", "qdrant", "reconcile")
PHASE_OPERATION = {
    "inspect": "inspect_exact_assets",
    "render": "render_exact_views",
    "caption": "caption_exact_render_set",
    "embed": "embed_exact_text_set",
    "postgres": "upsert_postgres_exact",
    "qdrant": "upsert_qdrant_exact",
    "reconcile": "reconcile_exact_snapshot",
}
PHASE_SUCCESS_MUTATION = {
    "inspect": "none",
    "render": "staged",
    "caption": "committed",
    "embed": "staged",
    "postgres": "committed",
    "qdrant": "committed",
    "reconcile": "none",
}
PHASE_SCOPES = {
    "inspect": ("ue_disposable_scene_inspect_spawn_cleanup",),
    "render": ("ue_disposable_scene_render_staging_cleanup",),
    "caption": ("caption_bounded_idempotent_call",),
    "embed": ("embedding_immutable_vector_staging",),
    "postgres": ("postgres_generation_write",),
    "qdrant": ("qdrant_generation_write",),
    "reconcile": (
        "postgres_generation_read_only",
        "qdrant_generation_read_only",
        "ue_runtime_read_only",
    ),
}
PHASE_COMPONENTS = {
    "inspect": ("unreal",),
    "render": ("unreal",),
    "caption": ("caption",),
    "embed": ("embedding",),
    "postgres": ("postgres",),
    "qdrant": ("qdrant",),
    "reconcile": ("postgres", "qdrant", "unreal"),
}
CONTROL_SCOPES = {
    "query_phase_status": "semantic_index_phase_status_read",
    "recover_phase_receipt": "semantic_index_phase_receipt_recover",
    "cancel_phase_work": "semantic_index_phase_work_cancel",
    "quarantine_generation": "semantic_index_generation_quarantine",
}
OUTPUT_ARTIFACT_FIELDS = {
    "inspect": "records_sha256",
    "render": "render_manifest_sha256",
    "caption": "catalog_sha256",
    "embed": "vector_bundle_manifest_sha256",
    "postgres": "row_set_sha256",
    "qdrant": "point_set_sha256",
}

APPROVAL_BASIS_SCHEMA = "simworld-semantic-index-production-approval-basis/v1"
APPROVAL_BASIS_REVISION = "semantic-index-production-approval-basis:v1"
GENERATION_DERIVATION = (
    "sha256-ascii-nul-separated(simworld-semantic-index-generation-id/v1,"
    "reviewed_job_revision,approval_basis_sha256)-v1"
)
GENERATION_NONCE_DERIVATION = "sha256(decoded-lowercase-hex-16-bytes)-v1"
GENERATION_DOMAIN = b"simworld-semantic-index-generation-id/v1\x00"
SERIALIZATION_CONTRACT = {
    "encoding": "utf-8",
    "canonicalization": "rfc8785-jcs-v1",
    "duplicate_keys": "reject",
    "unicode_normalization": "require_already_nfc",
    "numbers": "integers_only",
    "nonfinite_numbers": "reject",
    "trailing_newline": False,
}

FORMAL_SCHEMA_CONTRACTS = {
    "reviewed_job_schema_sha256": (
        "https://simworld.org/schemas/simworld-semantic-asset-index-job-v2.schema.json",
        "simworld-semantic-asset-index-job/v2",
        "5aac4a7475f9c39596c05c82ab1203d47679e00819931a6cd7cbd3859349f405",
    ),
    "execution_plan_schema_sha256": (
        "https://simworld.org/schemas/simworld-semantic-asset-index-execution-plan-v2.schema.json",
        "simworld-semantic-asset-index-execution-plan/v2",
        "45c1165e1983a4ff9ef83012045f4de936d71c32bd85e867ad5058621a95f2c5",
    ),
    "approval_basis_schema_sha256": (
        "https://simworld.org/schemas/simworld-semantic-index-production-approval-basis-v1.schema.json",
        "simworld-semantic-index-production-approval-basis/v1",
        "2e315686bfca7bb4f512ec29dae7b371dc6116da5acd0138162f45436782f5a5",
    ),
    "approval_receipt_schema_sha256": (
        "https://simworld.org/schemas/simworld-semantic-index-approval-receipt-v1.schema.json",
        "simworld-semantic-index-approval-receipt/v1",
        "bc8dd4278f7ddcaeefd3586be631d9d9c64e60d5364549f89ccbb74ca4ec36ca",
    ),
    "launcher_verification_receipt_schema_sha256": (
        "https://simworld.org/schemas/simworld-semantic-index-launcher-verification-receipt-v1.schema.json",
        "simworld-semantic-index-launcher-verification-receipt/v1",
        "e396e1ac1bc29f81a43ae19fab569e707e6296ecd32915fc80c47a938032ad2b",
    ),
    "phase_evidence_schema_sha256": (
        "https://simworld.org/schemas/simworld-semantic-index-phase-evidence-v1.schema.json",
        "simworld-semantic-index-phase-evidence/v1",
        "e869720568ba8876f88df83ed8540ad46a9809ea678214f5302f09554c480ce7",
    ),
    "phase_request_schema_sha256": (
        "https://simworld.org/schemas/simworld-semantic-index-phase-request-v1.schema.json",
        "simworld-semantic-index-phase-request/v1",
        "aba8ff478b154e5062d99822fd1cf9f8feea9c7f969bfb8b8bf094a202241f77",
    ),
    "worker_result_schema_sha256": (
        "https://simworld.org/schemas/simworld-semantic-index-worker-result-v1.schema.json",
        "simworld-semantic-index-worker-result/v1",
        "58df118a4b1e80cbd5b64d84415821fd3890bbc76f339ef98c697b7be61a7649",
    ),
    "control_request_schema_sha256": (
        "https://simworld.org/schemas/simworld-semantic-index-control-request-v1.schema.json",
        "simworld-semantic-index-control-request/v1",
        "a25034b2c08f14688850f6784f89dded9e9118c4e608b2c2e110bae4d28c5888",
    ),
    "control_result_schema_sha256": (
        "https://simworld.org/schemas/simworld-semantic-index-control-result-v1.schema.json",
        "simworld-semantic-index-control-result/v1",
        "55a780df3644e678393012e32241ee1ef6505d7804438ad20cf25f34ed6e709e",
    ),
    "worker_artifact_root_schema_sha256": (
        "https://simworld.org/schemas/simworld-semantic-index-worker-artifact-root-v1.schema.json",
        "simworld-semantic-index-worker-artifact-root/v1",
        "b2c536915cd3236753aa6eb8d6fe86f1975d7fbdcdda8b41cc7d0e14d96ef52c",
    ),
    "state_schema_sha256": (
        "https://simworld.org/schemas/simworld-semantic-asset-index-execution-state-v2.schema.json",
        "simworld-semantic-asset-index-execution-state/v2",
        "529a48ab15e11c9aa767ee5ca8f588396b3371947c0780c1fa18ea7e615c103a",
    ),
    "terminal_receipt_schema_sha256": (
        "https://simworld.org/schemas/simworld-semantic-asset-index-terminal-receipt-v2.schema.json",
        "simworld-semantic-asset-index-terminal-receipt/v2",
        "436d4a0139b31b51586bff3d963a89b537e1158ee5ba837a60a05775b9fb2a84",
    ),
    "adapter_contract_sha256": (
        "https://simworld.org/schemas/semantic-index-production-adapter-v2.schema.json",
        "semantic-index-production-adapter/v2",
        "9aaba0d7eaa7bcbc3a6a8dcd8edbce06004600afa7b2694607b4938f0d75c27b",
    ),
    "inspect_artifact_schema_sha256": (
        "https://simworld.org/schemas/simworld-semantic-index-inspect-artifact-v1.schema.json",
        "simworld-semantic-index-inspect-artifact/v1",
        "717164dba3dd1b2bc3def77d03b28dd46d17493f28bf519d2f3390b5809ba4e1",
    ),
    "render_artifact_schema_sha256": (
        "https://simworld.org/schemas/simworld-semantic-index-render-artifact-v1.schema.json",
        "simworld-semantic-index-render-artifact/v1",
        "2e401b1dba04011e68679fe51b26b18a46721bfef47c4bd6cdf47d0b2acc9898",
    ),
    "caption_artifact_schema_sha256": (
        "https://simworld.org/schemas/simworld-semantic-index-caption-artifact-v1.schema.json",
        "simworld-semantic-index-caption-artifact/v1",
        "ed1a7207ad017d4fa13b33333e154d1abc776c0e9656f2caa803cc37d3e230b7",
    ),
    "embed_artifact_schema_sha256": (
        "https://simworld.org/schemas/simworld-semantic-index-embed-artifact-v1.schema.json",
        "simworld-semantic-index-embed-artifact/v1",
        "71d280d5785748c165b32491f9c75fafa2bd3bde814947a5fd79c61c2bed71e0",
    ),
    "postgres_artifact_schema_sha256": (
        "https://simworld.org/schemas/simworld-semantic-index-postgres-artifact-v1.schema.json",
        "simworld-semantic-index-postgres-artifact/v1",
        "fc74bc6ea124dd78fc3f215033573c9c840e8d666fabbe56374167c796e4c604",
    ),
    "qdrant_artifact_schema_sha256": (
        "https://simworld.org/schemas/simworld-semantic-index-qdrant-artifact-v1.schema.json",
        "simworld-semantic-index-qdrant-artifact/v1",
        "b635a695fe85f0819ef38c3ae71c002a9f6d44e7371801174b223209c9c36915",
    ),
    "reconcile_artifact_schema_sha256": (
        "https://simworld.org/schemas/simworld-semantic-index-reconcile-artifact-v1.schema.json",
        "simworld-semantic-index-reconcile-artifact/v1",
        "1d8522aafbfc0c6605694a8b4f4ab0f29b663a5ce42e5b63eceba22f86f27f60",
    ),
}

_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_SHA256_REVISION_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_ERROR_DEPENDENCIES = frozenset(
    {"worker", "unreal", "caption", "embedding", "postgres", "qdrant", "evidence_store"}
)
_PUBLIC_ERRORS = {
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


@dataclasses.dataclass(frozen=True)
class ContractValidationError(Exception):
    """Bounded semantic-validation failure with no document values."""

    code: str
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.code} at {self.path}: {self.message}"


@dataclasses.dataclass(frozen=True)
class SealedDocument:
    """Raw canonical document and its independently supplied digest."""

    raw: bytes
    expected_sha256: str


@dataclasses.dataclass(frozen=True)
class ProductionContractBundle:
    """All independently sealed inputs required for a complete v2 build."""

    source_candidates: SealedDocument
    reviewed_job: SealedDocument
    approval_basis: SealedDocument
    execution_plan: SealedDocument
    approvals: Mapping[str, SealedDocument]
    phase_requests: Sequence[SealedDocument]
    phase_evidence: Sequence[SealedDocument]
    contract_schemas: Mapping[str, SealedDocument] = dataclasses.field(
        default_factory=dict
    )
    secret_values: Sequence[bytes] = ()


@dataclasses.dataclass(frozen=True)
class ValidatedProductionContracts:
    reviewed_job_sha256: str
    approval_basis_sha256: str
    execution_plan_sha256: str
    generation_id: str
    accepted_asset_ids: tuple[str, ...]
    evidence_sha256_by_phase: tuple[tuple[str, str], ...]


def _fail(code: str, path: str, message: str) -> None:
    raise ContractValidationError(code=code, path=path, message=message)


def worker_operation_revision(operation: str) -> str:
    """Return the closed worker-protocol revision for one allowlisted operation."""

    if operation not in PHASE_OPERATION.values() and operation not in CONTROL_SCOPES:
        _fail(
            "CONTRACT_OPERATION_INVALID",
            "$.operation",
            "Operation is outside the closed worker protocol",
        )
    material = (
        f"simworld-semantic-index-worker/v1\0{operation}\0closed-operation-v1"
    ).encode("ascii")
    return "sha256:" + hashlib.sha256(material).hexdigest()


def _valid_text(value: Any, path: str) -> str:
    if not isinstance(value, str):
        _fail("CONTRACT_TYPE_INVALID", path, "Expected text")
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeError:
        _fail("CONTRACT_TEXT_INVALID", path, "Text is not valid UTF-8")
    if len(encoded) > MAX_JSON_STRING_BYTES:
        _fail("CONTRACT_LIMIT_EXCEEDED", path, "Text exceeds the byte limit")
    if unicodedata.normalize("NFC", value) != value:
        _fail("CONTRACT_TEXT_NONCANONICAL", path, "Text is not already Unicode NFC")
    if any(unicodedata.category(character) in {"Cc", "Cs"} for character in value):
        _fail("CONTRACT_TEXT_INVALID", path, "Text contains a forbidden control character")
    return value


def _validate_json_value(value: Any, path: str = "$", depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        _fail("CONTRACT_LIMIT_EXCEEDED", path, "JSON nesting exceeds the limit")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int) and not isinstance(value, bool):
        if not -MAX_JSON_INTEGER <= value <= MAX_JSON_INTEGER:
            _fail("CONTRACT_INTEGER_INVALID", path, "Integer is outside the allowed range")
        return
    if isinstance(value, str):
        _valid_text(value, path)
        return
    if isinstance(value, list):
        if len(value) > MAX_JSON_CONTAINER_ITEMS:
            _fail("CONTRACT_LIMIT_EXCEEDED", path, "JSON array exceeds the item limit")
        for index, item in enumerate(value):
            _validate_json_value(item, f"{path}[{index}]", depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > MAX_JSON_CONTAINER_ITEMS:
            _fail("CONTRACT_LIMIT_EXCEEDED", path, "JSON object exceeds the item limit")
        for key, item in value.items():
            _valid_text(key, f"{path}.<key>")
            _validate_json_value(item, f"{path}.{key}", depth + 1)
        return
    _fail("CONTRACT_JSON_INVALID", path, "JSON contains a forbidden scalar type")


def _jcs_string(value: str) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8", "strict")
    except (TypeError, ValueError, UnicodeError):
        _fail("CONTRACT_JSON_INVALID", "$", "JSON string cannot be encoded")


def _utf16_sort_key(value: str) -> bytes:
    try:
        return value.encode("utf-16-be", "strict")
    except UnicodeError:
        _fail("CONTRACT_TEXT_INVALID", "$", "JSON object key is not valid Unicode")


def _encode_jcs(value: Any) -> bytes:
    if value is None:
        return b"null"
    if value is True:
        return b"true"
    if value is False:
        return b"false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value).encode("ascii")
    if isinstance(value, str):
        return _jcs_string(value)
    if isinstance(value, list):
        return b"[" + b",".join(_encode_jcs(item) for item in value) + b"]"
    if isinstance(value, dict):
        members = []
        for key in sorted(value, key=_utf16_sort_key):
            members.append(_jcs_string(key) + b":" + _encode_jcs(value[key]))
        return b"{" + b",".join(members) + b"}"
    _fail("CONTRACT_JSON_INVALID", "$", "JSON contains a forbidden scalar type")


def canonical_jcs_bytes(value: Any) -> bytes:
    """Encode integer-only, already-NFC JSON using RFC 8785 key ordering."""

    _validate_json_value(value)
    return _encode_jcs(value)


def _reject_float(_value: str) -> None:
    _fail("CONTRACT_FLOAT_FORBIDDEN", "$", "Floating-point JSON numbers are forbidden")


def _reject_constant(_value: str) -> None:
    _fail("CONTRACT_FLOAT_FORBIDDEN", "$", "Non-finite JSON numbers are forbidden")


def _parse_integer(value: str) -> int:
    try:
        parsed = int(value, 10)
    except ValueError:
        _fail("CONTRACT_INTEGER_INVALID", "$", "JSON integer is invalid")
    if not -MAX_JSON_INTEGER <= parsed <= MAX_JSON_INTEGER:
        _fail("CONTRACT_INTEGER_INVALID", "$", "Integer is outside the allowed range")
    return parsed


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("CONTRACT_DUPLICATE_KEY", "$", "JSON contains a duplicate object key")
        result[key] = value
    return result


def decode_canonical_jcs(raw: bytes) -> Any:
    """Parse canonical integer-only JSON and reject alternate encodings."""

    if type(raw) is not bytes or not raw or len(raw) > MAX_DOCUMENT_BYTES:
        _fail("CONTRACT_DOCUMENT_SIZE_INVALID", "$", "Document byte length is invalid")
    try:
        text = raw.decode("utf-8", "strict")
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_int=_parse_integer,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except ContractValidationError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError):
        _fail("CONTRACT_JSON_INVALID", "$", "Document is not valid JSON")
    _validate_json_value(value)
    if not hmac.compare_digest(canonical_jcs_bytes(value), raw):
        _fail("CONTRACT_JSON_NONCANONICAL", "$", "Document bytes are not canonical JCS")
    return value


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_jcs_bytes(value)).hexdigest()


def sorted_id_set_sha256(asset_ids: Sequence[str]) -> str:
    """Hash an exact ID set as sorted NFC UTF-8 values, each terminated by LF."""

    values = [_valid_text(value, "$.asset_ids") for value in asset_ids]
    if len(values) != len(set(values)):
        _fail("CONTRACT_SET_INVALID", "$.asset_ids", "Asset IDs are not unique")
    payload = b"".join(value.encode("utf-8") + b"\n" for value in sorted(values))
    return hashlib.sha256(payload).hexdigest()


def derive_generation_id(reviewed_job_revision: str, approval_basis_sha256: str) -> str:
    if not isinstance(reviewed_job_revision, str) or not _SHA256_REVISION_RE.fullmatch(
        reviewed_job_revision
    ):
        _fail("CONTRACT_GENERATION_INVALID", "$.reviewed_job.job_revision", "Job revision is invalid")
    if not isinstance(approval_basis_sha256, str) or not _SHA256_RE.fullmatch(
        approval_basis_sha256
    ):
        _fail("CONTRACT_GENERATION_INVALID", "$.approval_basis.sha256", "Basis digest is invalid")
    preimage = (
        GENERATION_DOMAIN
        + reviewed_job_revision.encode("ascii")
        + b"\x00"
        + approval_basis_sha256.encode("ascii")
    )
    return "semantic-generation:" + hashlib.sha256(preimage).hexdigest()


def _object(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        _fail("CONTRACT_TYPE_INVALID", path, "Expected an object")
    return value


def _field(value: Mapping[str, Any], key: str, path: str) -> Any:
    if key not in value:
        _fail("CONTRACT_FIELD_MISSING", f"{path}.{key}", "Required field is missing")
    return value[key]


def _array(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        _fail("CONTRACT_TYPE_INVALID", path, "Expected an array")
    return value


def _integer(value: Any, path: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        _fail("CONTRACT_INTEGER_INVALID", path, "Expected an integer in the allowed range")
    return value


def _same(actual: Any, expected: Any, path: str, *, code: str = "CONTRACT_BINDING_MISMATCH") -> None:
    if canonical_jcs_bytes(actual) != canonical_jcs_bytes(expected):
        _fail(code, path, "Cross-document binding does not match")


def _sha(value: Any, path: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        _fail("CONTRACT_DIGEST_INVALID", path, "Expected a lowercase SHA-256 digest")
    return value


def _document_digest(document: SealedDocument, path: str) -> str:
    if type(document) is not SealedDocument:
        _fail("CONTRACT_DOCUMENT_INVALID", path, "Expected an exact sealed document")
    expected = _sha(document.expected_sha256, f"{path}.expected_sha256")
    if type(document.raw) is not bytes or not document.raw or len(document.raw) > MAX_DOCUMENT_BYTES:
        _fail("CONTRACT_DOCUMENT_SIZE_INVALID", path, "Document byte length is invalid")
    observed = hashlib.sha256(document.raw).hexdigest()
    if not hmac.compare_digest(observed, expected):
        _fail("CONTRACT_DOCUMENT_HASH_MISMATCH", path, "Independent document digest does not match")
    return observed


def _load_document(document: SealedDocument, path: str) -> tuple[dict[str, Any], str]:
    digest = _document_digest(document, path)
    value = decode_canonical_jcs(document.raw)
    if not isinstance(value, dict):
        _fail("CONTRACT_TYPE_INVALID", path, "Top-level document must be an object")
    return value, digest


def _load_schema_document(
    document: SealedDocument, path: str
) -> tuple[dict[str, Any], str]:
    """Load exact source bytes for a pinned JSON Schema document.

    Schema source files are hashed byte-for-byte and need not themselves be
    serialized as JCS.  Duplicate keys, floats, non-JSON values, invalid UTF-8,
    and resource overflows are still rejected before the closed runtime audits
    their keyword subset.
    """

    digest = _document_digest(document, path)
    try:
        value = json.loads(
            document.raw.decode("utf-8", "strict"),
            object_pairs_hook=_unique_object,
            parse_int=_parse_integer,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except ContractValidationError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError):
        _fail("CONTRACT_SCHEMA_DEFINITION_INVALID", path, "Schema source is not valid JSON")
    _validate_json_value(value, path)
    if not isinstance(value, dict):
        _fail("CONTRACT_SCHEMA_DEFINITION_INVALID", path, "Schema source must be an object")
    return value, digest


def _load_formal_schema_bundle(
    bundle: ProductionContractBundle,
) -> tuple[dict[str, dict[str, Any]], dict[str, str], dict[str, dict[str, Any]]]:
    if not isinstance(bundle.contract_schemas, Mapping) or set(
        bundle.contract_schemas
    ) != set(FORMAL_SCHEMA_CONTRACTS):
        _fail(
            "CONTRACT_SCHEMA_SET_INVALID",
            "$.contract_schemas",
            "Pinned formal schema set is incomplete or contains unknown entries",
        )
    schemas_by_digest_key: dict[str, dict[str, Any]] = {}
    digests: dict[str, str] = {}
    registry: dict[str, dict[str, Any]] = {}
    for digest_key, (
        expected_id,
        expected_document_schema,
        trusted_source_sha256,
    ) in FORMAL_SCHEMA_CONTRACTS.items():
        schema, digest = _load_schema_document(
            bundle.contract_schemas[digest_key],
            f"$.contract_schemas.{digest_key}",
        )
        if not hmac.compare_digest(digest, trusted_source_sha256):
            _fail(
                "CONTRACT_SCHEMA_TRUST_MISMATCH",
                f"$.contract_schemas.{digest_key}",
                "Pinned schema source does not match the trusted release closure",
            )
        _same(
            schema.get("$id"),
            expected_id,
            f"$.contract_schemas.{digest_key}.$id",
            code="CONTRACT_SCHEMA_DEFINITION_INVALID",
        )
        properties = _object(
            schema.get("properties"),
            f"$.contract_schemas.{digest_key}.properties",
        )
        schema_property = _object(
            properties.get("schema"),
            f"$.contract_schemas.{digest_key}.properties.schema",
        )
        _same(
            schema_property.get("const"),
            expected_document_schema,
            f"$.contract_schemas.{digest_key}.properties.schema.const",
            code="CONTRACT_SCHEMA_DEFINITION_INVALID",
        )
        if expected_id in registry:
            _fail(
                "CONTRACT_SCHEMA_DEFINITION_INVALID",
                f"$.contract_schemas.{digest_key}.$id",
                "Formal schema identifier is duplicated",
            )
        schemas_by_digest_key[digest_key] = schema
        digests[digest_key] = digest
        registry[expected_id] = schema
    try:
        for schema in schemas_by_digest_key.values():
            validate_schema_definition(schema, registry=registry)
    except SchemaRuntimeError:
        _fail(
            "CONTRACT_SCHEMA_DEFINITION_INVALID",
            "$.contract_schemas",
            "Pinned schema is outside the supported closed contract subset",
        )
    return schemas_by_digest_key, digests, registry


def _validate_formal_instance(
    schema: Mapping[str, Any],
    instance: Any,
    registry: Mapping[str, Mapping[str, Any]],
    path: str,
) -> None:
    try:
        validate_schema_instance(schema, instance, registry=registry)
    except SchemaRuntimeError:
        _fail(
            "CONTRACT_SCHEMA_INSTANCE_INVALID",
            path,
            "Document violates its pinned formal schema",
        )


def _bind_formal_schema_digests(
    plan: Mapping[str, Any], digests: Mapping[str, str]
) -> None:
    contract_digests = _object(
        plan.get("contract_digests"), "$.execution_plan.contract_digests"
    )
    for key, digest in digests.items():
        _same(
            contract_digests.get(key),
            digest,
            f"$.execution_plan.contract_digests.{key}",
            code="CONTRACT_SCHEMA_DIGEST_MISMATCH",
        )


def _secret_needles(secret_values: Sequence[bytes]) -> tuple[bytes, ...]:
    needles: set[bytes] = set()
    for index, secret in enumerate(secret_values):
        if type(secret) is not bytes or not secret:
            _fail("CONTRACT_SECRET_INPUT_INVALID", f"$.secret_values[{index}]", "Secret input is invalid")
        standard = base64.b64encode(secret)
        urlsafe = base64.urlsafe_b64encode(secret)
        digest = hashlib.sha256(secret).hexdigest().encode("ascii")
        needles.update(
            {
                secret,
                standard,
                standard.rstrip(b"="),
                urlsafe,
                urlsafe.rstrip(b"="),
                digest,
                digest.upper(),
            }
        )
    return tuple(sorted((needle for needle in needles if needle), key=lambda item: (len(item), item)))


def _scan_secrets(bundle: ProductionContractBundle) -> None:
    needles = _secret_needles(bundle.secret_values)
    documents = [
        bundle.source_candidates,
        bundle.reviewed_job,
        bundle.approval_basis,
        bundle.execution_plan,
        *bundle.approvals.values(),
        *bundle.phase_requests,
        *bundle.phase_evidence,
        *bundle.contract_schemas.values(),
    ]
    for document in documents:
        if type(document) is not SealedDocument or type(document.raw) is not bytes:
            _fail("CONTRACT_DOCUMENT_INVALID", "$.bundle", "Expected exact sealed documents")
        decoded_forms = [document.raw]
        current = document.raw
        for _ in range(MAX_PERCENT_DECODE_PASSES):
            decoded = unquote_to_bytes(current)
            if decoded == current:
                break
            decoded_forms.append(decoded)
            current = decoded
        else:
            if re.search(br"%[0-9A-Fa-f]{2}", current):
                _fail(
                    "CONTRACT_SECRET_ENCODING_TOO_DEEP",
                    "$.bundle",
                    "A sealed document contains excessively nested percent encoding",
                )
        if any(needle in form for needle in needles for form in decoded_forms):
            _fail("CONTRACT_SECRET_LEAK", "$.bundle", "A sealed document contains a secret transform")


def _parse_time(value: Any, path: str) -> dt.datetime:
    text = _valid_text(value, path)
    try:
        parsed = dt.datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        _fail("CONTRACT_TIME_INVALID", path, "Timestamp is not valid RFC 3339")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail("CONTRACT_TIME_INVALID", path, "Timestamp must include an offset")
    return parsed.astimezone(dt.timezone.utc)


def _fresh_interval(
    issued_value: Any,
    expires_value: Any,
    *,
    now: dt.datetime,
    skew: dt.timedelta,
    path: str,
) -> tuple[dt.datetime, dt.datetime]:
    issued = _parse_time(issued_value, f"{path}.issued_at")
    expires = _parse_time(expires_value, f"{path}.expires_at")
    if expires <= issued:
        _fail("CONTRACT_TIME_INVALID", path, "Expiry must follow issue time")
    if issued > now + skew:
        _fail("CONTRACT_NOT_YET_VALID", path, "Document issue time is in the future")
    if expires <= now:
        _fail("CONTRACT_EXPIRED", path, "Document is expired")
    return issued, expires


def _credential_targets(plan: Mapping[str, Any]) -> dict[str, Any]:
    credentials = _object(plan.get("credentials"), "$.execution_plan.credentials")
    ordinary = _object(credentials.get("ordinary_phases"), "$.execution_plan.credentials.ordinary_phases")
    reconcile = _object(credentials.get("reconcile"), "$.execution_plan.credentials.reconcile")
    controls = _object(
        credentials.get("control_operations"),
        "$.execution_plan.credentials.control_operations",
    )

    def project(capability: Any, path: str) -> dict[str, Any]:
        value = _object(capability, path)
        return {
            "credential_generation": value.get("credential_generation"),
            "target_identity": value.get("target_identity"),
            "scope": value.get("scope"),
        }

    return {
        "transport": credentials.get("transport"),
        "ordinary_phases": {
            phase: project(ordinary.get(phase), f"$.execution_plan.credentials.ordinary_phases.{phase}")
            for phase in PHASES[:-1]
        },
        "reconcile": {
            key: project(reconcile.get(key), f"$.execution_plan.credentials.reconcile.{key}")
            for key in ("postgres_read", "qdrant_read", "ue_runtime_read")
        },
        "control_operations": {
            key: project(
                controls.get(key), f"$.execution_plan.credentials.control_operations.{key}"
            )
            for key in CONTROL_SCOPES
        },
    }


def derive_target_generation_identities(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Return the exact generation-independent target identity commitment."""

    generation = _object(plan.get("generation"), "$.execution_plan.generation")
    artifact = _object(generation.get("artifact_namespace"), "$.execution_plan.generation.artifact_namespace")
    storage = _object(plan.get("storage"), "$.execution_plan.storage")
    catalog = _object(storage.get("catalog"), "$.execution_plan.storage.catalog")
    postgres = _object(storage.get("postgres"), "$.execution_plan.storage.postgres")
    qdrant = _object(storage.get("qdrant"), "$.execution_plan.storage.qdrant")
    return {
        "artifact_store": {
            "store_identity": artifact.get("store_identity"),
            "workspace_root": artifact.get("workspace_root"),
            "layout": artifact.get("layout"),
            "generation_name_policy": artifact.get("generation_name_policy"),
            "namespace_revision": artifact.get("namespace_revision"),
        },
        "catalog": {
            "namespace_revision": catalog.get("namespace_revision"),
        },
        "postgres": {
            "deployment_identity": postgres.get("deployment_identity"),
            "schema_revision": postgres.get("schema_revision"),
            "generation_name_policy": postgres.get("generation_name_policy"),
        },
        "qdrant": {
            "cluster_identity": qdrant.get("cluster_identity"),
            "dense_vector_name": qdrant.get("dense_vector_name"),
            "sparse_vector_name": qdrant.get("sparse_vector_name"),
            "generation_name_policy": qdrant.get("generation_name_policy"),
        },
    }


def derive_expected_approval_basis(
    plan: Mapping[str, Any], reviewed_job: Mapping[str, Any]
) -> dict[str, Any]:
    """Construct the complete approval basis through an explicit allowlist."""

    generation = _object(plan.get("generation"), "$.execution_plan.generation")
    nonce = _valid_text(generation.get("generation_nonce"), "$.execution_plan.generation.generation_nonce")
    if not re.fullmatch(r"[a-f0-9]{32}", nonce):
        _fail("CONTRACT_GENERATION_INVALID", "$.execution_plan.generation.generation_nonce", "Nonce is invalid")
    nonce_sha256 = hashlib.sha256(bytes.fromhex(nonce)).hexdigest()
    target_identities = derive_target_generation_identities(plan)
    credential_targets = _credential_targets(plan)

    commitment_sources = {
        "run_identity_sha256": plan.get("run_identity"),
        "reviewed_job_reference_sha256": plan.get("reviewed_job"),
        "contract_digests_sha256": plan.get("contract_digests"),
        "adapter_sha256": plan.get("adapter"),
        "deployment_preflight_sha256": plan.get("deployment_preflight"),
        "worker_sha256": plan.get("worker"),
        "unreal_sha256": plan.get("unreal"),
        "caption_sha256": plan.get("caption"),
        "embedding_sha256": plan.get("embedding"),
        "runtime_images_sha256": plan.get("runtime_images"),
        "target_attestations_sha256": plan.get("target_attestations"),
        "expected_sets_sha256": plan.get("expected_sets"),
        "acceptance_sha256": plan.get("acceptance"),
        "phase_policy_sha256": plan.get("phase_policy"),
        "activation_contract_sha256": plan.get("activation_contract"),
    }
    commitments: dict[str, str] = {}
    for key, source in commitment_sources.items():
        if source is None:
            _fail("CONTRACT_FIELD_MISSING", f"$.execution_plan.{key}", "Basis commitment is missing")
        commitments[key] = canonical_sha256(source)

    reviewed_job_sha256 = _sha(
        _object(plan.get("reviewed_job"), "$.execution_plan.reviewed_job").get("sha256"),
        "$.execution_plan.reviewed_job.sha256",
    )
    if reviewed_job.get("schema") != "simworld-semantic-asset-index-job/v2":
        _fail("CONTRACT_SCHEMA_INVALID", "$.reviewed_job.schema", "Reviewed job schema is invalid")
    return {
        "schema": APPROVAL_BASIS_SCHEMA,
        "revision": APPROVAL_BASIS_REVISION,
        "reviewed_job_sha256": reviewed_job_sha256,
        "reviewed_job_revision": reviewed_job.get("job_revision"),
        "commitments": commitments,
        "generation_inputs": {
            "generation_nonce": nonce,
            "generation_nonce_sha256": nonce_sha256,
            "generation_nonce_sha256_derivation": GENERATION_NONCE_DERIVATION,
            "derivation": GENERATION_DERIVATION,
            "target_generation_identities": target_identities,
            "generation_reservation_sha256": canonical_sha256(
                _object(
                    generation.get("reservation"),
                    "$.execution_plan.generation.reservation",
                )
            ),
            "previous_active_sha256": canonical_sha256(generation.get("previous_active")),
            "active_generation_pointer_sha256": canonical_sha256(
                generation.get("active_generation_pointer")
            ),
            "cleanup_policy": generation.get("cleanup_policy"),
        },
        "credential_targets": credential_targets,
        "serialization_contract": dict(SERIALIZATION_CONTRACT),
    }


def _asset_ids(records: Any, path: str, *, unique: bool = True) -> tuple[str, ...]:
    values = _array(records, path)
    result: list[str] = []
    for index, record in enumerate(values):
        if isinstance(record, str):
            asset_id = _valid_text(record, f"{path}[{index}]")
        else:
            item = _object(record, f"{path}[{index}]")
            asset_id = _valid_text(_field(item, "asset_id", f"{path}[{index}]"), f"{path}[{index}].asset_id")
        result.append(asset_id)
    if unique and len(result) != len(set(result)):
        _fail("CONTRACT_SET_INVALID", path, "Asset IDs are not unique")
    return tuple(result)


_SOURCE_ASSET_FIELDS = (
    "asset_id",
    "ue_name",
    "ue_path",
    "asset_type",
    "source_pack",
    "content_revision",
)


def _source_asset_records(records: Any, path: str) -> dict[str, dict[str, Any]]:
    """Return the immutable source identity for every candidate asset.

    Review state and rejection-ledger fields intentionally are not part of the
    source identity, but every field that can change what Unreal loads is.  A
    bare ID list is insufficient because it permits reviewed paths or types to
    be rebound while preserving the partition proof.
    """

    values = _array(records, path)
    result: dict[str, dict[str, Any]] = {}
    for index, record in enumerate(values):
        item_path = f"{path}[{index}]"
        item = _object(record, item_path)
        projected = {
            key: _field(item, key, item_path)
            for key in _SOURCE_ASSET_FIELDS
        }
        asset_id = _valid_text(projected["asset_id"], f"{item_path}.asset_id")
        if asset_id in result:
            _fail("CONTRACT_SET_INVALID", path, "Source asset IDs are not unique")
        result[asset_id] = projected
    return result


def _reviewed_source_projection(record: Any, path: str) -> dict[str, Any]:
    item = _object(record, path)
    return {key: _field(item, key, path) for key in _SOURCE_ASSET_FIELDS}


def _validate_reviewed_job(
    job: Mapping[str, Any],
    job_sha256: str,
    source_candidates: Any,
    source_candidates_sha256: str,
) -> tuple[dict[str, Mapping[str, Any]], tuple[str, ...], tuple[str, ...], str]:
    if job.get("schema") != "simworld-semantic-asset-index-job/v2":
        _fail("CONTRACT_SCHEMA_INVALID", "$.reviewed_job.schema", "Reviewed job schema is invalid")
    source = _object(_field(job, "source", "$.reviewed_job"), "$.reviewed_job.source")
    reviewed = _object(
        _field(job, "reviewed_selection", "$.reviewed_job"),
        "$.reviewed_job.reviewed_selection",
    )
    candidate_set = _object(
        _field(reviewed, "candidate_set", "$.reviewed_job.reviewed_selection"),
        "$.reviewed_job.reviewed_selection.candidate_set",
    )
    candidate_records = _source_asset_records(source_candidates, "$.source_candidates")
    candidate_ids = tuple(candidate_records)
    if _integer(source.get("candidate_set_count"), "$.reviewed_job.source.candidate_set_count") != len(
        candidate_ids
    ):
        _fail("CONTRACT_COUNT_MISMATCH", "$.reviewed_job.source.candidate_set_count", "Candidate count does not match")
    _same(source.get("candidate_set_sha256"), source_candidates_sha256, "$.reviewed_job.source.candidate_set_sha256")
    _same(candidate_set.get("count"), len(candidate_ids), "$.reviewed_job.reviewed_selection.candidate_set.count")
    _same(
        candidate_set.get("assets_sha256"),
        source_candidates_sha256,
        "$.reviewed_job.reviewed_selection.candidate_set.assets_sha256",
    )

    accepted_set = _object(
        _field(reviewed, "accepted_set", "$.reviewed_job.reviewed_selection"),
        "$.reviewed_job.reviewed_selection.accepted_set",
    )
    rejected_set = _object(
        _field(reviewed, "rejected_set", "$.reviewed_job.reviewed_selection"),
        "$.reviewed_job.reviewed_selection.rejected_set",
    )
    accepted_records = _array(
        _field(accepted_set, "assets", "$.reviewed_job.reviewed_selection.accepted_set"),
        "$.reviewed_job.reviewed_selection.accepted_set.assets",
    )
    rejected_records = _array(
        _field(rejected_set, "assets", "$.reviewed_job.reviewed_selection.rejected_set"),
        "$.reviewed_job.reviewed_selection.rejected_set.assets",
    )
    accepted_ids = _asset_ids(accepted_records, "$.reviewed_job.reviewed_selection.accepted_set.assets")
    rejected_ids = _asset_ids(rejected_records, "$.reviewed_job.reviewed_selection.rejected_set.assets")
    if accepted_ids != tuple(sorted(accepted_ids)) or rejected_ids != tuple(sorted(rejected_ids)):
        _fail("CONTRACT_SET_NONCANONICAL", "$.reviewed_job.reviewed_selection", "Reviewed asset lists are not sorted")
    if set(accepted_ids) & set(rejected_ids):
        _fail("CONTRACT_PARTITION_INVALID", "$.reviewed_job.reviewed_selection", "Accepted and rejected sets overlap")
    if set(accepted_ids) | set(rejected_ids) != set(candidate_ids):
        _fail("CONTRACT_PARTITION_INVALID", "$.reviewed_job.reviewed_selection", "Reviewed sets do not exactly partition candidates")
    if not accepted_ids:
        _fail("CONTRACT_PARTITION_INVALID", "$.reviewed_job.reviewed_selection.accepted_set", "Accepted set is empty")
    for set_name, records, ids in (
        ("accepted_set", accepted_records, accepted_ids),
        ("rejected_set", rejected_records, rejected_ids),
    ):
        for index, (record, asset_id) in enumerate(zip(records, ids, strict=True)):
            _same(
                _reviewed_source_projection(
                    record,
                    f"$.reviewed_job.reviewed_selection.{set_name}.assets[{index}]",
                ),
                candidate_records[asset_id],
                f"$.reviewed_job.reviewed_selection.{set_name}.assets[{index}]",
                code="CONTRACT_SOURCE_ASSET_MISMATCH",
            )
    _same(accepted_set.get("count"), len(accepted_ids), "$.reviewed_job.reviewed_selection.accepted_set.count")
    _same(rejected_set.get("count"), len(rejected_ids), "$.reviewed_job.reviewed_selection.rejected_set.count")
    accepted_sha256 = canonical_sha256(accepted_records)
    rejected_sha256 = canonical_sha256(rejected_records)
    _same(
        accepted_set.get("assets_sha256"),
        accepted_sha256,
        "$.reviewed_job.reviewed_selection.accepted_set.assets_sha256",
    )
    _same(
        rejected_set.get("assets_sha256"),
        rejected_sha256,
        "$.reviewed_job.reviewed_selection.rejected_set.assets_sha256",
    )

    proof = _object(
        _field(reviewed, "partition_proof", "$.reviewed_job.reviewed_selection"),
        "$.reviewed_job.reviewed_selection.partition_proof",
    )
    proof_expected = {
        "algorithm": "sorted-asset-id-partition-v1",
        "candidate_count": len(candidate_ids),
        "candidate_set_sha256": source_candidates_sha256,
        "accepted_count": len(accepted_ids),
        "accepted_assets_sha256": accepted_sha256,
        "rejected_count": len(rejected_ids),
        "rejected_assets_sha256": rejected_sha256,
        "disjoint": True,
        "complete": True,
    }
    for key, expected in proof_expected.items():
        _same(proof.get(key), expected, f"$.reviewed_job.reviewed_selection.partition_proof.{key}")
    _same(
        proof.get("proof_sha256"),
        canonical_sha256(proof_expected),
        "$.reviewed_job.reviewed_selection.partition_proof.proof_sha256",
    )

    revision = _valid_text(_field(job, "job_revision", "$.reviewed_job"), "$.reviewed_job.job_revision")
    revision_basis = dict(job)
    revision_basis.pop("job_revision", None)
    _same(revision, "sha256:" + canonical_sha256(revision_basis), "$.reviewed_job.job_revision")
    _same(
        job.get("serialization_contract"),
        SERIALIZATION_CONTRACT,
        "$.reviewed_job.serialization_contract",
    )
    accepted_map: dict[str, Mapping[str, Any]] = {}
    for index, record in enumerate(accepted_records):
        value = _object(record, f"$.reviewed_job.reviewed_selection.accepted_set.assets[{index}]")
        accepted_map[accepted_ids[index]] = value
    resource = _object(_field(job, "resource_contract", "$.reviewed_job"), "$.reviewed_job.resource_contract")
    _same(
        resource.get("computed_for_accepted_assets_sha256"),
        accepted_sha256,
        "$.reviewed_job.resource_contract.computed_for_accepted_assets_sha256",
    )
    for section_name in ("limits", "estimates"):
        section = _object(_field(resource, section_name, "$.reviewed_job.resource_contract"), f"$.reviewed_job.resource_contract.{section_name}")
        _same(section.get("assets"), len(accepted_ids), f"$.reviewed_job.resource_contract.{section_name}.assets")
        _same(section.get("postgres_rows"), len(accepted_ids), f"$.reviewed_job.resource_contract.{section_name}.postgres_rows")
        _same(section.get("qdrant_points"), len(accepted_ids), f"$.reviewed_job.resource_contract.{section_name}.qdrant_points")
    return accepted_map, accepted_ids, rejected_ids, accepted_sha256


def _expected_target_map(plan: Mapping[str, Any]) -> dict[str, tuple[str, str | None]]:
    unreal = _object(plan.get("unreal"), "$.execution_plan.unreal")
    caption = _object(plan.get("caption"), "$.execution_plan.caption")
    embedding = _object(plan.get("embedding"), "$.execution_plan.embedding")
    storage = _object(plan.get("storage"), "$.execution_plan.storage")
    postgres = _object(storage.get("postgres"), "$.execution_plan.storage.postgres")
    qdrant = _object(storage.get("qdrant"), "$.execution_plan.storage.qdrant")
    images = _object(plan.get("runtime_images"), "$.execution_plan.runtime_images")
    deployment = _object(plan.get("deployment_preflight"), "$.execution_plan.deployment_preflight")
    return {
        "unreal": (_valid_text(unreal.get("broker_identity"), "$.execution_plan.unreal.broker_identity"), images.get("unreal")),
        "caption": (_valid_text(caption.get("project_identity"), "$.execution_plan.caption.project_identity"), None),
        "embedding": (_valid_text(embedding.get("project_identity"), "$.execution_plan.embedding.project_identity"), images.get("embedding")),
        "postgres": (_valid_text(postgres.get("deployment_identity"), "$.execution_plan.storage.postgres.deployment_identity"), images.get("postgres")),
        "qdrant": (_valid_text(qdrant.get("cluster_identity"), "$.execution_plan.storage.qdrant.cluster_identity"), images.get("qdrant")),
        "worker": (_valid_text(deployment.get("deployment_identity"), "$.execution_plan.deployment_preflight.deployment_identity"), images.get("worker")),
    }


def _expected_credential_target_map(plan: Mapping[str, Any]) -> dict[str, str]:
    """Return the least-privilege principal/target bound to each capability."""

    target_map = _expected_target_map(plan)
    caption = _object(plan.get("caption"), "$.execution_plan.caption")
    embedding = _object(plan.get("embedding"), "$.execution_plan.embedding")
    return {
        "unreal": target_map["unreal"][0],
        "caption": _valid_text(
            caption.get("project_identity"), "$.execution_plan.caption.project_identity"
        ),
        "embedding": _valid_text(
            embedding.get("project_identity"), "$.execution_plan.embedding.project_identity"
        ),
        "postgres": target_map["postgres"][0],
        "qdrant": target_map["qdrant"][0],
    }


def _expected_target_attestation(
    plan: Mapping[str, Any], component: str
) -> dict[str, Any]:
    attestations = _object(
        plan.get("target_attestations"), "$.execution_plan.target_attestations"
    )
    facts = _object(
        attestations.get(component),
        f"$.execution_plan.target_attestations.{component}",
    )
    target_identity, runtime_image = _expected_target_map(plan)[component]
    _same(
        facts.get("runtime_image"),
        runtime_image,
        f"$.execution_plan.target_attestations.{component}.runtime_image",
    )
    return {"component": component, "target_identity": target_identity, **facts}


def _validate_generation_names(plan: Mapping[str, Any], generation_id: str) -> None:
    """Verify the three policy-labelled names are exact generation derivatives."""

    digest = generation_id.removeprefix("semantic-generation:")
    generation = _object(plan.get("generation"), "$.execution_plan.generation")
    artifact = _object(
        generation.get("artifact_namespace"),
        "$.execution_plan.generation.artifact_namespace",
    )
    workspace_root = _valid_text(
        artifact.get("workspace_root"),
        "$.execution_plan.generation.artifact_namespace.workspace_root",
    ).rstrip("/")
    _same(
        artifact.get("workspace"),
        f"{workspace_root}/semantic_generation_{digest}",
        "$.execution_plan.generation.artifact_namespace.workspace",
        code="CONTRACT_GENERATION_OWNERSHIP_INVALID",
    )
    storage = _object(plan.get("storage"), "$.execution_plan.storage")
    postgres = _object(storage.get("postgres"), "$.execution_plan.storage.postgres")
    qdrant = _object(storage.get("qdrant"), "$.execution_plan.storage.qdrant")
    _same(
        postgres.get("database"),
        f"semantic_gen_{digest[:50]}",
        "$.execution_plan.storage.postgres.database",
        code="CONTRACT_GENERATION_OWNERSHIP_INVALID",
    )
    _same(
        postgres.get("schema_name"),
        f"snapshot_{digest[:54]}",
        "$.execution_plan.storage.postgres.schema_name",
        code="CONTRACT_GENERATION_OWNERSHIP_INVALID",
    )
    _same(
        qdrant.get("collection"),
        f"semantic_gen_{digest}",
        "$.execution_plan.storage.qdrant.collection",
        code="CONTRACT_GENERATION_OWNERSHIP_INVALID",
    )


def _validate_plan(
    plan: Mapping[str, Any],
    plan_sha256: str,
    job: Mapping[str, Any],
    job_sha256: str,
    basis: Mapping[str, Any],
    basis_sha256: str,
    accepted_ids: tuple[str, ...],
    accepted_assets_sha256: str,
    now: dt.datetime,
    skew: dt.timedelta,
) -> str:
    if plan.get("schema") != "simworld-semantic-asset-index-execution-plan/v2" or plan.get("profile") != "production":
        _fail("CONTRACT_SCHEMA_INVALID", "$.execution_plan", "Execution plan schema or profile is invalid")
    reviewed_ref = _object(plan.get("reviewed_job"), "$.execution_plan.reviewed_job")
    reviewed_selection = _object(job.get("reviewed_selection"), "$.reviewed_job.reviewed_selection")
    accepted_set = _object(reviewed_selection.get("accepted_set"), "$.reviewed_job.reviewed_selection.accepted_set")
    rejected_set = _object(reviewed_selection.get("rejected_set"), "$.reviewed_job.reviewed_selection.rejected_set")
    rejection_ledger = _object(reviewed_selection.get("rejection_ledger"), "$.reviewed_job.reviewed_selection.rejection_ledger")
    source = _object(job.get("source"), "$.reviewed_job.source")
    snapshot_target = _object(job.get("snapshot_target"), "$.reviewed_job.snapshot_target")
    reviewed_expected = {
        "schema": job.get("schema"),
        "sha256": job_sha256,
        "revision": job.get("job_revision"),
        "source_v1_job_sha256": source.get("v1_job_sha256"),
        "preparation_receipt_sha256": source.get("preparation_receipt_sha256"),
        "accepted_asset_count": len(accepted_ids),
        "accepted_assets_sha256": accepted_assets_sha256,
        "rejected_asset_count": rejected_set.get("count"),
        "rejected_assets_sha256": rejected_set.get("assets_sha256"),
        "rejection_ledger_sha256": rejection_ledger.get("sha256"),
        "content_revision": _object(source.get("content"), "$.reviewed_job.source.content").get("revision"),
        "target_snapshot_revision": source.get("target_snapshot_revision"),
    }
    _same(reviewed_ref, reviewed_expected, "$.execution_plan.reviewed_job")

    basis_ref = _object(plan.get("approval_basis"), "$.execution_plan.approval_basis")
    basis_ref_expected = {
        "schema": APPROVAL_BASIS_SCHEMA,
        "revision": APPROVAL_BASIS_REVISION,
        "sha256": basis_sha256,
        "canonicalization": "rfc8785-jcs-v1",
    }
    _same(basis_ref, basis_ref_expected, "$.execution_plan.approval_basis")
    expected_basis = derive_expected_approval_basis(plan, job)
    _same(basis, expected_basis, "$.approval_basis", code="CONTRACT_APPROVAL_BASIS_MISMATCH")
    _same(basis.get("reviewed_job_sha256"), job_sha256, "$.approval_basis.reviewed_job_sha256")

    generation = _object(plan.get("generation"), "$.execution_plan.generation")
    _same(generation.get("reviewed_job_sha256"), job_sha256, "$.execution_plan.generation.reviewed_job_sha256")
    _same(generation.get("reviewed_job_revision"), job.get("job_revision"), "$.execution_plan.generation.reviewed_job_revision")
    _same(generation.get("approval_basis_sha256"), basis_sha256, "$.execution_plan.generation.approval_basis_sha256")
    _same(generation.get("generation_nonce_sha256"), basis["generation_inputs"]["generation_nonce_sha256"], "$.execution_plan.generation.generation_nonce_sha256")
    _same(generation.get("generation_nonce_sha256_derivation"), GENERATION_NONCE_DERIVATION, "$.execution_plan.generation.generation_nonce_sha256_derivation")
    _same(
        generation.get("target_generation_identities_sha256"),
        canonical_sha256(basis["generation_inputs"]["target_generation_identities"]),
        "$.execution_plan.generation.target_generation_identities_sha256",
    )
    _same(generation.get("derivation"), GENERATION_DERIVATION, "$.execution_plan.generation.derivation")
    generation_id = derive_generation_id(str(job.get("job_revision")), basis_sha256)
    _same(generation.get("generation_id"), generation_id, "$.execution_plan.generation.generation_id")
    _validate_generation_names(plan, generation_id)
    artifact = _object(
        generation.get("artifact_namespace"),
        "$.execution_plan.generation.artifact_namespace",
    )
    _same(
        artifact.get("generation_name_policy"),
        "semantic-generation-id-path-segment-v1",
        "$.execution_plan.generation.artifact_namespace.generation_name_policy",
    )
    _same(
        artifact.get("layout"),
        "content-addressed-sha256-no-replace-v1",
        "$.execution_plan.generation.artifact_namespace.layout",
    )
    reservation = _object(generation.get("reservation"), "$.execution_plan.generation.reservation")
    _same(reservation.get("owner_identity"), _object(plan.get("run_identity"), "$.execution_plan.run_identity").get("service_owner_id"), "$.execution_plan.generation.reservation.owner_identity")
    _fresh_interval(reservation.get("issued_at"), reservation.get("expires_at"), now=now, skew=skew, path="$.execution_plan.generation.reservation")
    previous = _object(generation.get("previous_active"), "$.execution_plan.generation.previous_active")
    if (previous.get("generation_id") is None) != (previous.get("snapshot_revision") is None):
        _fail("CONTRACT_GENERATION_INVALID", "$.execution_plan.generation.previous_active", "Previous active generation is partial")
    if previous.get("generation_id") == generation_id:
        _fail("CONTRACT_GENERATION_INVALID", "$.execution_plan.generation.previous_active", "New generation equals previous active generation")

    storage = _object(plan.get("storage"), "$.execution_plan.storage")
    for name in ("catalog", "postgres", "qdrant"):
        component = _object(storage.get(name), f"$.execution_plan.storage.{name}")
        _same(component.get("generation_id"), generation_id, f"$.execution_plan.storage.{name}.generation_id")
        _same(component.get("active_target"), False, f"$.execution_plan.storage.{name}.active_target")
    postgres_storage = _object(storage.get("postgres"), "$.execution_plan.storage.postgres")
    qdrant_storage = _object(storage.get("qdrant"), "$.execution_plan.storage.qdrant")
    _same(
        postgres_storage.get("generation_name_policy"),
        "semantic-generation-id-sql-identifiers-v1",
        "$.execution_plan.storage.postgres.generation_name_policy",
    )
    _same(
        qdrant_storage.get("generation_name_policy"),
        "semantic-generation-id-qdrant-collection-v1",
        "$.execution_plan.storage.qdrant.generation_name_policy",
    )
    _same(
        postgres_storage.get("schema_revision"),
        snapshot_target.get("postgres_schema_revision"),
        "$.execution_plan.storage.postgres.schema_revision",
    )
    _same(
        qdrant_storage.get("dense_vector_name"),
        snapshot_target.get("qdrant_dense_vector_name"),
        "$.execution_plan.storage.qdrant.dense_vector_name",
    )
    _same(
        qdrant_storage.get("sparse_vector_name"),
        snapshot_target.get("qdrant_sparse_vector_name"),
        "$.execution_plan.storage.qdrant.sparse_vector_name",
    )

    credentials = _object(plan.get("credentials"), "$.execution_plan.credentials")
    _same(credentials.get("transport"), "sealed_memfd_scm_rights_v1", "$.execution_plan.credentials.transport")
    _same(credentials.get("json_contains_credential_bytes"), False, "$.execution_plan.credentials.json_contains_credential_bytes")
    ordinary = _object(credentials.get("ordinary_phases"), "$.execution_plan.credentials.ordinary_phases")
    reconcile = _object(credentials.get("reconcile"), "$.execution_plan.credentials.reconcile")
    target_map = _expected_credential_target_map(plan)
    ordinary_components = {"inspect": "unreal", "render": "unreal", "caption": "caption", "embed": "embedding", "postgres": "postgres", "qdrant": "qdrant"}
    for phase, component_name in ordinary_components.items():
        capability = _object(ordinary.get(phase), f"$.execution_plan.credentials.ordinary_phases.{phase}")
        _same(capability.get("generation_id"), generation_id, f"$.execution_plan.credentials.ordinary_phases.{phase}.generation_id")
        _same(capability.get("scope"), PHASE_SCOPES[phase][0], f"$.execution_plan.credentials.ordinary_phases.{phase}.scope")
        _same(capability.get("target_identity"), target_map[component_name], f"$.execution_plan.credentials.ordinary_phases.{phase}.target_identity")
    reconcile_components = {"postgres_read": "postgres", "qdrant_read": "qdrant", "ue_runtime_read": "unreal"}
    for key, component_name in reconcile_components.items():
        capability = _object(reconcile.get(key), f"$.execution_plan.credentials.reconcile.{key}")
        _same(capability.get("generation_id"), generation_id, f"$.execution_plan.credentials.reconcile.{key}.generation_id")
        expected_scope = PHASE_SCOPES["reconcile"][("postgres_read", "qdrant_read", "ue_runtime_read").index(key)]
        _same(capability.get("scope"), expected_scope, f"$.execution_plan.credentials.reconcile.{key}.scope")
        _same(capability.get("target_identity"), target_map[component_name], f"$.execution_plan.credentials.reconcile.{key}.target_identity")
    controls = _object(
        credentials.get("control_operations"),
        "$.execution_plan.credentials.control_operations",
    )
    worker_identity = _object(
        plan.get("deployment_preflight"), "$.execution_plan.deployment_preflight"
    ).get("deployment_identity")
    for key, expected_scope in CONTROL_SCOPES.items():
        capability = _object(
            controls.get(key), f"$.execution_plan.credentials.control_operations.{key}"
        )
        _same(
            capability.get("generation_id"),
            generation_id,
            f"$.execution_plan.credentials.control_operations.{key}.generation_id",
        )
        _same(
            capability.get("scope"),
            expected_scope,
            f"$.execution_plan.credentials.control_operations.{key}.scope",
        )
        _same(
            capability.get("target_identity"),
            worker_identity,
            f"$.execution_plan.credentials.control_operations.{key}.target_identity",
        )

    images = _object(plan.get("runtime_images"), "$.execution_plan.runtime_images")
    unreal = _object(plan.get("unreal"), "$.execution_plan.unreal")
    deployment = _object(plan.get("deployment_preflight"), "$.execution_plan.deployment_preflight")
    _same(unreal.get("runtime_image"), images.get("unreal"), "$.execution_plan.unreal.runtime_image")
    _same(
        unreal.get("content_revision"),
        _object(source.get("content"), "$.reviewed_job.source.content").get("revision"),
        "$.execution_plan.unreal.content_revision",
    )
    _same(deployment.get("worker_image"), images.get("worker"), "$.execution_plan.deployment_preflight.worker_image")
    _same(deployment.get("deployment_identity"), plan.get("worker", {}).get("peer_deployment_identity"), "$.execution_plan.worker.peer_deployment_identity")
    worker = _object(plan.get("worker"), "$.execution_plan.worker")
    _same(
        worker.get("phase_operations"),
        [PHASE_OPERATION[phase] for phase in PHASES],
        "$.execution_plan.worker.phase_operations",
    )
    _same(
        worker.get("control_operations"),
        list(CONTROL_SCOPES),
        "$.execution_plan.worker.control_operations",
    )
    _same(
        worker.get("runtime_attestation_sha256"),
        deployment.get("worker_attestation_sha256"),
        "$.execution_plan.worker.runtime_attestation_sha256",
    )
    for component in ("unreal", "caption", "embedding", "postgres", "qdrant"):
        _expected_target_attestation(plan, component)
    _fresh_interval(deployment.get("issued_at"), deployment.get("expires_at"), now=now, skew=skew, path="$.execution_plan.deployment_preflight")
    lease_issued, lease_expires = _fresh_interval(unreal.get("lease_issued_at"), unreal.get("lease_expires_at"), now=now, skew=skew, path="$.execution_plan.unreal")
    if lease_expires <= lease_issued:
        _fail("CONTRACT_TIME_INVALID", "$.execution_plan.unreal", "UE lease interval is invalid")

    accepted_id_sha256 = sorted_id_set_sha256(accepted_ids)
    expected_sets = _object(plan.get("expected_sets"), "$.execution_plan.expected_sets")
    for name in ("assets", "catalog_records", "dense_vectors", "sparse_vectors", "postgres_rows", "qdrant_points"):
        expected_set = _object(expected_sets.get(name), f"$.execution_plan.expected_sets.{name}")
        _same(expected_set.get("count"), len(accepted_ids), f"$.execution_plan.expected_sets.{name}.count")
        _same(expected_set.get("sorted_ids_sha256"), accepted_id_sha256, f"$.execution_plan.expected_sets.{name}.sorted_ids_sha256")
    acceptance = _object(plan.get("acceptance"), "$.execution_plan.acceptance")
    for index, query_value in enumerate(_array(acceptance.get("shadow_queries"), "$.execution_plan.acceptance.shadow_queries")):
        query = _object(query_value, f"$.execution_plan.acceptance.shadow_queries[{index}]")
        query_text = _valid_text(query.get("query_text"), f"$.execution_plan.acceptance.shadow_queries[{index}].query_text")
        _same(query.get("query_text_sha256"), hashlib.sha256(query_text.encode("utf-8")).hexdigest(), f"$.execution_plan.acceptance.shadow_queries[{index}].query_text_sha256")
        expected_ids = _asset_ids(query.get("expected_asset_ids"), f"$.execution_plan.acceptance.shadow_queries[{index}].expected_asset_ids")
        if not set(expected_ids) <= set(accepted_ids):
            _fail("CONTRACT_ACCEPTANCE_INVALID", f"$.execution_plan.acceptance.shadow_queries[{index}]", "Query expects an unaccepted asset")
    for index, smoke_value in enumerate(_array(acceptance.get("smoke_targets"), "$.execution_plan.acceptance.smoke_targets")):
        smoke = _object(smoke_value, f"$.execution_plan.acceptance.smoke_targets[{index}]")
        asset_id = _valid_text(smoke.get("asset_id"), f"$.execution_plan.acceptance.smoke_targets[{index}].asset_id")
        if asset_id not in set(accepted_ids):
            _fail("CONTRACT_ACCEPTANCE_INVALID", f"$.execution_plan.acceptance.smoke_targets[{index}]", "Smoke target is not accepted")
        accepted_assets = {
            _valid_text(record.get("asset_id"), "$.reviewed_job.reviewed_selection.accepted_set.assets.asset_id"): record
            for record in _array(accepted_set.get("assets"), "$.reviewed_job.reviewed_selection.accepted_set.assets")
            if isinstance(record, dict)
        }
        source_asset = accepted_assets[asset_id]
        for key in ("ue_path", "asset_type"):
            _same(
                smoke.get(key),
                source_asset.get(key),
                f"$.execution_plan.acceptance.smoke_targets[{index}].{key}",
            )
    _same(
        plan.get("serialization_contract"),
        SERIALIZATION_CONTRACT,
        "$.execution_plan.serialization_contract",
    )
    return generation_id


def _validate_receipt_signature(receipt: Mapping[str, Any], path: str) -> None:
    signature = _object(_field(receipt, "signature", path), f"{path}.signature")
    unsigned = dict(receipt)
    unsigned.pop("signature", None)
    _same(
        signature.get("signed_payload_sha256"),
        canonical_sha256(unsigned),
        f"{path}.signature.signed_payload_sha256",
    )
    encoded = _valid_text(
        signature.get("detached_signature_base64"),
        f"{path}.signature.detached_signature_base64",
    )
    try:
        raw_signature = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeError, binascii.Error):
        _fail("CONTRACT_SIGNATURE_INVALID", f"{path}.signature", "Detached signature encoding is invalid")
    if len(raw_signature) != 64 or base64.b64encode(raw_signature).decode("ascii") != encoded:
        _fail("CONTRACT_SIGNATURE_INVALID", f"{path}.signature", "Detached signature encoding is invalid")
    _same(
        signature.get("detached_signature_sha256"),
        hashlib.sha256(raw_signature).hexdigest(),
        f"{path}.signature.detached_signature_sha256",
    )
    _same(signature.get("algorithm"), "ed25519", f"{path}.signature.algorithm")


def _production_scope_expected(
    plan: Mapping[str, Any], job: Mapping[str, Any], job_sha256: str, basis_sha256: str, generation_id: str
) -> dict[str, Any]:
    generation = _object(plan.get("generation"), "$.execution_plan.generation")
    pointer = _object(generation.get("active_generation_pointer"), "$.execution_plan.generation.active_generation_pointer")
    return {
        "scope_kind": "production_build",
        "reviewed_job_sha256": job_sha256,
        "reviewed_job_revision": job.get("job_revision"),
        "plan_approval_basis_sha256": basis_sha256,
        "generation_id": generation_id,
        "generation_nonce_sha256": generation.get("generation_nonce_sha256"),
        "target_snapshot_revision": _object(job.get("source"), "$.reviewed_job.source").get("target_snapshot_revision"),
        "worker_deployment_identity": _object(plan.get("deployment_preflight"), "$.execution_plan.deployment_preflight").get("deployment_identity"),
        "active_generation_pointer_identity": pointer.get("identity"),
        "expected_active_generation_epoch": pointer.get("observed_epoch"),
    }


def _validate_approvals(
    approvals: Mapping[str, Mapping[str, Any]],
    approval_sha256: Mapping[str, str],
    plan: Mapping[str, Any],
    job: Mapping[str, Any],
    job_sha256: str,
    basis_sha256: str,
    generation_id: str,
    *,
    now: dt.datetime,
    skew: dt.timedelta,
) -> None:
    if set(approvals) != set(APPROVAL_KINDS) or set(approval_sha256) != set(APPROVAL_KINDS):
        _fail("CONTRACT_APPROVAL_SET_INVALID", "$.approvals", "Approval receipt set is incomplete")
    plan_refs = _object(plan.get("approvals"), "$.execution_plan.approvals")
    job_reviewed = _object(job.get("reviewed_selection"), "$.reviewed_job.reviewed_selection")
    accepted = _object(job_reviewed.get("accepted_set"), "$.reviewed_job.reviewed_selection.accepted_set")
    rejection = _object(job_reviewed.get("rejection_ledger"), "$.reviewed_job.reviewed_selection.rejection_ledger")
    source = _object(job.get("source"), "$.reviewed_job.source")
    selection_basis_sha256 = _sha(
        job_reviewed.get("selection_approval_basis_sha256"),
        "$.reviewed_job.reviewed_selection.selection_approval_basis_sha256",
    )
    production_scope = _production_scope_expected(plan, job, job_sha256, basis_sha256, generation_id)
    expected_roles = {
        "data_owner_review": "data_owner",
        "cost_owner": "cost_owner",
        "admin_state_change": "state_change_admin",
        "deployment": "deployment_attestor",
        "runtime": "runtime_owner",
        "rollback_readiness": "rollback_owner",
    }
    for kind in APPROVAL_KINDS:
        receipt = approvals[kind]
        path = f"$.approvals.{kind}"
        if receipt.get("schema") != "simworld-semantic-index-approval-receipt/v1":
            _fail("CONTRACT_SCHEMA_INVALID", f"{path}.schema", "Approval receipt schema is invalid")
        _same(receipt.get("approval_kind"), kind, f"{path}.approval_kind")
        _same(receipt.get("decision"), "approved", f"{path}.decision")
        expected_basis = selection_basis_sha256 if kind == "data_owner_review" else basis_sha256
        _same(receipt.get("approval_basis_sha256"), expected_basis, f"{path}.approval_basis_sha256")
        _fresh_interval(receipt.get("issued_at"), receipt.get("expires_at"), now=now, skew=skew, path=path)
        issuer = _object(receipt.get("issuer"), f"{path}.issuer")
        _same(issuer.get("role"), expected_roles[kind], f"{path}.issuer.role")
        _validate_receipt_signature(receipt, path)
        trust = _object(receipt.get("trust_bundle"), f"{path}.trust_bundle")
        signature = _object(receipt.get("signature"), f"{path}.signature")
        plan_ref = _object(plan_refs.get(kind), f"$.execution_plan.approvals.{kind}")
        plan_ref_expected = {
            "receipt_schema": receipt.get("schema"),
            "receipt_id": receipt.get("receipt_id"),
            "receipt_sha256": approval_sha256[kind],
            "approval_kind": kind,
            "approval_basis_sha256": expected_basis,
            "trust_bundle_sha256": trust.get("sha256"),
            "issuer_key_id": issuer.get("key_id"),
            "issuer_key_revision": issuer.get("key_revision"),
            "signature_algorithm": signature.get("algorithm"),
            "expires_at": receipt.get("expires_at"),
        }
        _same(plan_ref, plan_ref_expected, f"$.execution_plan.approvals.{kind}")
        authorization = _object(receipt.get("authorization"), f"{path}.authorization")
        _same(authorization.get("kind"), kind, f"{path}.authorization.kind")
        if kind == "data_owner_review":
            expected_scope = {
                "scope_kind": "reviewed_selection",
                "source_v1_job_sha256": source.get("v1_job_sha256"),
                "source_v1_job_revision": source.get("v1_job_revision"),
                "reviewed_selection_basis_sha256": selection_basis_sha256,
                "accepted_assets_sha256": accepted.get("assets_sha256"),
                "accepted_asset_count": accepted.get("count"),
                "rejection_ledger_sha256": rejection.get("sha256"),
                "content_revision": _object(source.get("content"), "$.reviewed_job.source.content").get("revision"),
            }
            _same(receipt.get("scope"), expected_scope, f"{path}.scope")
            data_ref = _object(job.get("data_owner_approval"), "$.reviewed_job.data_owner_approval")
            _same(
                data_ref,
                {
                    "receipt_schema": receipt.get("schema"),
                    "receipt_id": receipt.get("receipt_id"),
                    "receipt_sha256": approval_sha256[kind],
                    "approval_basis_sha256": selection_basis_sha256,
                    "approval_kind": kind,
                    "accepted_assets_sha256": accepted.get("assets_sha256"),
                    "rejection_ledger_sha256": rejection.get("sha256"),
                    "content_revision": _object(source.get("content"), "$.reviewed_job.source.content").get("revision"),
                },
                "$.reviewed_job.data_owner_approval",
            )
        else:
            _same(receipt.get("scope"), production_scope, f"{path}.scope")

    cost = _object(approvals["cost_owner"].get("authorization"), "$.approvals.cost_owner.authorization")
    caption = _object(plan.get("caption"), "$.execution_plan.caption")
    for plan_key, approval_key in (
        ("max_calls", "max_caption_calls"),
        ("max_output_tokens", "max_output_tokens"),
        ("max_cost_minor_units", "max_cost_minor_units"),
    ):
        plan_value = _integer(caption.get(plan_key), f"$.execution_plan.caption.{plan_key}", minimum=1)
        approval_value = _integer(cost.get(approval_key), f"$.approvals.cost_owner.authorization.{approval_key}", minimum=1)
        if plan_value > approval_value:
            _fail("CONTRACT_COST_UNAUTHORIZED", f"$.execution_plan.caption.{plan_key}", "Plan cost ceiling exceeds approval")
    for key in ("currency", "minor_unit_exponent", "rounding_mode", "retry_charge_policy"):
        _same(caption.get(key), cost.get(key), f"$.execution_plan.caption.{key}")

    admin = _object(approvals["admin_state_change"].get("authorization"), "$.approvals.admin_state_change.authorization")
    _same(admin.get("allowed_mutating_phases"), list(PHASES[:6]), "$.approvals.admin_state_change.authorization.allowed_mutating_phases")
    _same(
        admin.get("allowed_control_operations"),
        list(CONTROL_SCOPES),
        "$.approvals.admin_state_change.authorization.allowed_control_operations",
    )
    _same(admin.get("activation_authorized"), False, "$.approvals.admin_state_change.authorization.activation_authorized")
    deployment_auth = _object(approvals["deployment"].get("authorization"), "$.approvals.deployment.authorization")
    deployment = _object(plan.get("deployment_preflight"), "$.execution_plan.deployment_preflight")
    _same(deployment_auth.get("deployment_preflight_sha256"), deployment.get("receipt_sha256"), "$.approvals.deployment.authorization.deployment_preflight_sha256")
    _same(deployment_auth.get("worker_image"), deployment.get("worker_image"), "$.approvals.deployment.authorization.worker_image")
    _same(deployment_auth.get("worker_protocol_sha256"), _object(plan.get("contract_digests"), "$.execution_plan.contract_digests").get("worker_protocol_sha256"), "$.approvals.deployment.authorization.worker_protocol_sha256")
    runtime_auth = _object(approvals["runtime"].get("authorization"), "$.approvals.runtime.authorization")
    unreal = _object(plan.get("unreal"), "$.execution_plan.unreal")
    _same(runtime_auth.get("ue_lease_id"), unreal.get("lease_id"), "$.approvals.runtime.authorization.ue_lease_id")
    _same(runtime_auth.get("ue_lease_expires_at"), unreal.get("lease_expires_at"), "$.approvals.runtime.authorization.ue_lease_expires_at")
    _same(runtime_auth.get("worker_deployment_identity"), deployment.get("deployment_identity"), "$.approvals.runtime.authorization.worker_deployment_identity")
    rollback_auth = _object(approvals["rollback_readiness"].get("authorization"), "$.approvals.rollback_readiness.authorization")
    previous = _object(_object(plan.get("generation"), "$.execution_plan.generation").get("previous_active"), "$.execution_plan.generation.previous_active")
    pointer = _object(_object(plan.get("generation"), "$.execution_plan.generation").get("active_generation_pointer"), "$.execution_plan.generation.active_generation_pointer")
    if previous.get("snapshot_revision") is not None:
        _same(rollback_auth.get("previous_snapshot_revision"), previous.get("snapshot_revision"), "$.approvals.rollback_readiness.authorization.previous_snapshot_revision")
    _same(rollback_auth.get("active_generation_pointer_identity"), pointer.get("identity"), "$.approvals.rollback_readiness.authorization.active_generation_pointer_identity")
    _same(rollback_auth.get("expected_active_generation_epoch"), pointer.get("observed_epoch"), "$.approvals.rollback_readiness.authorization.expected_active_generation_epoch")


def _validate_outcome(evidence: Mapping[str, Any], phase: str, path: str) -> bool:
    outcome = _object(evidence.get("outcome"), f"{path}.outcome")
    status = outcome.get("status")
    mutation_state = outcome.get("mutation_state")
    retryable = outcome.get("retryable")
    error = outcome.get("error")
    if status == "success":
        _same(mutation_state, PHASE_SUCCESS_MUTATION[phase], f"{path}.outcome.mutation_state", code="CONTRACT_MUTATION_STATE_INVALID")
        _same(retryable, False, f"{path}.outcome.retryable", code="CONTRACT_RETRYABILITY_INVALID")
        _same(error, None, f"{path}.outcome.error", code="CONTRACT_ERROR_INVALID")
        return True
    if status != "failed":
        _fail("CONTRACT_ERROR_INVALID", f"{path}.outcome.status", "Outcome status is invalid")
    typed = _object(error, f"{path}.outcome.error")
    if typed.get("dependency") not in _ERROR_DEPENDENCIES:
        _fail("CONTRACT_ERROR_INVALID", f"{path}.outcome.error.dependency", "Error dependency is invalid")
    error_pair = (typed.get("dependency"), typed.get("public_code"))
    if error_pair not in _PUBLIC_ERRORS:
        _fail("CONTRACT_ERROR_INVALID", f"{path}.outcome.error.public_code", "Public error code is invalid")
    if not isinstance(retryable, bool):
        _fail("CONTRACT_ERROR_INVALID", f"{path}.outcome.retryable", "Retryability must be boolean")
    _same(typed.get("retryable"), retryable, f"{path}.outcome.error.retryable", code="CONTRACT_RETRYABILITY_INVALID")
    _same(typed.get("mutation_state"), mutation_state, f"{path}.outcome.error.mutation_state", code="CONTRACT_MUTATION_STATE_INVALID")
    if mutation_state not in {"none", "staged", "committed", "ambiguous"}:
        _fail("CONTRACT_MUTATION_STATE_INVALID", f"{path}.outcome.mutation_state", "Mutation state is invalid")
    if retryable and mutation_state != "none":
        _fail("CONTRACT_RETRYABILITY_INVALID", f"{path}.outcome.retryable", "Mutated or ambiguous failures are not retryable")
    message = _valid_text(typed.get("redacted_message"), f"{path}.outcome.error.redacted_message")
    if len(message.encode("utf-8")) > 240:
        _fail("CONTRACT_ERROR_INVALID", f"{path}.outcome.error.redacted_message", "Public error message is too long")
    _same(
        message,
        _PUBLIC_ERRORS[error_pair],
        f"{path}.outcome.error.redacted_message",
        code="CONTRACT_ERROR_INVALID",
    )
    payload = _object(evidence.get("payload"), f"{path}.payload")
    action = payload.get("operator_action")
    allowed_actions = {
        "none": {"none", "retry_same_key"},
        "staged": {"recover_receipt", "quarantine_generation"},
        "committed": {"recover_receipt", "manual_reconcile"},
        "ambiguous": {"manual_reconcile", "quarantine_generation"},
    }
    if action not in allowed_actions[mutation_state]:
        _fail("CONTRACT_ERROR_INVALID", f"{path}.payload.operator_action", "Operator action conflicts with mutation state")
    return False


def _expected_capabilities_for_phase(plan: Mapping[str, Any], phase: str) -> list[Mapping[str, Any]]:
    credentials = _object(plan.get("credentials"), "$.execution_plan.credentials")
    if phase != "reconcile":
        ordinary = _object(credentials.get("ordinary_phases"), "$.execution_plan.credentials.ordinary_phases")
        return [_object(ordinary.get(phase), f"$.execution_plan.credentials.ordinary_phases.{phase}")]
    reconcile = _object(credentials.get("reconcile"), "$.execution_plan.credentials.reconcile")
    return [
        _object(reconcile.get(key), f"$.execution_plan.credentials.reconcile.{key}")
        for key in ("postgres_read", "qdrant_read", "ue_runtime_read")
    ]


def _validate_evidence_targets(
    evidence: Mapping[str, Any], plan: Mapping[str, Any], phase: str, path: str
) -> None:
    generation_id = _object(plan.get("generation"), "$.execution_plan.generation").get("generation_id")
    observed = _array(evidence.get("credentials_observed"), f"{path}.credentials_observed")
    expected = _expected_capabilities_for_phase(plan, phase)
    if len(observed) != len(expected):
        _fail("CONTRACT_CAPABILITY_MISMATCH", f"{path}.credentials_observed", "Capability count does not match phase")
    observed_by_scope: dict[str, Mapping[str, Any]] = {}
    for index, value in enumerate(observed):
        item = _object(value, f"{path}.credentials_observed[{index}]")
        scope = _valid_text(item.get("scope"), f"{path}.credentials_observed[{index}].scope")
        if scope in observed_by_scope:
            _fail("CONTRACT_CAPABILITY_MISMATCH", f"{path}.credentials_observed", "Capability scope is duplicated")
        observed_by_scope[scope] = item
    for capability in expected:
        scope = capability.get("scope")
        if scope not in observed_by_scope:
            _fail("CONTRACT_CAPABILITY_MISMATCH", f"{path}.credentials_observed", "Expected capability scope is absent")
        _same(
            observed_by_scope[scope],
            {
                "scope": scope,
                "credential_generation": capability.get("credential_generation"),
                "generation_id": generation_id,
                "target_identity": capability.get("target_identity"),
            },
            f"{path}.credentials_observed",
            code="CONTRACT_CAPABILITY_MISMATCH",
        )

    attestation = _object(evidence.get("target_attestation"), f"{path}.target_attestation")
    deployment = _object(plan.get("deployment_preflight"), "$.execution_plan.deployment_preflight")
    images = _object(plan.get("runtime_images"), "$.execution_plan.runtime_images")
    worker = _object(plan.get("worker"), "$.execution_plan.worker")
    _same(attestation.get("worker_deployment_identity"), deployment.get("deployment_identity"), f"{path}.target_attestation.worker_deployment_identity")
    _same(attestation.get("worker_image"), images.get("worker"), f"{path}.target_attestation.worker_image")
    _same(attestation.get("deployment_preflight_sha256"), deployment.get("receipt_sha256"), f"{path}.target_attestation.deployment_preflight_sha256")
    _same(attestation.get("worker_whoami_sha256"), worker.get("whoami_sha256"), f"{path}.target_attestation.worker_whoami_sha256")
    _same(attestation.get("worker_runtime_attestation_sha256"), worker.get("runtime_attestation_sha256"), f"{path}.target_attestation.worker_runtime_attestation_sha256")
    targets = _array(attestation.get("targets"), f"{path}.target_attestation.targets")
    expected_components = set(PHASE_COMPONENTS[phase])
    observed_components: dict[str, Mapping[str, Any]] = {}
    for index, value in enumerate(targets):
        item = _object(value, f"{path}.target_attestation.targets[{index}]")
        component = _valid_text(item.get("component"), f"{path}.target_attestation.targets[{index}].component")
        if component in observed_components:
            _fail("CONTRACT_TARGET_MISMATCH", f"{path}.target_attestation.targets", "Target component is duplicated")
        observed_components[component] = item
    if set(observed_components) != expected_components:
        _fail("CONTRACT_TARGET_MISMATCH", f"{path}.target_attestation.targets", "Observed target set does not match phase")
    for component in expected_components:
        item = observed_components[component]
        _same(
            item,
            _expected_target_attestation(plan, component),
            f"{path}.target_attestation.targets.{component}",
        )


def _validate_record_membership(
    records: Any,
    accepted: Mapping[str, Mapping[str, Any]],
    path: str,
    *,
    exact: bool,
    unique: bool = True,
) -> tuple[str, ...]:
    record_values = _array(records, path)
    ids = _asset_ids(record_values, path, unique=unique)
    if not set(ids) <= set(accepted):
        _fail("CONTRACT_ASSET_SET_MISMATCH", path, "Evidence contains an unaccepted asset")
    if exact and set(ids) != set(accepted):
        _fail("CONTRACT_ASSET_SET_MISMATCH", path, "Evidence does not contain the exact accepted set")
    return ids


def _validate_phase_payload(
    evidence: Mapping[str, Any],
    phase: str,
    plan: Mapping[str, Any],
    accepted: Mapping[str, Mapping[str, Any]],
    approvals: Mapping[str, Mapping[str, Any]],
    *,
    now: dt.datetime,
    phase_completed: dt.datetime,
    max_live_audit_ttl: dt.timedelta,
    path: str,
) -> None:
    payload = _object(evidence.get("payload"), f"{path}.payload")
    if phase == "inspect":
        records = _array(payload.get("records"), f"{path}.payload.records")
        ids = _validate_record_membership(records, accepted, f"{path}.payload.records", exact=True)
        _same(payload.get("records_sha256"), canonical_sha256(records), f"{path}.payload.records_sha256")
        for index, value in enumerate(records):
            item = _object(value, f"{path}.payload.records[{index}]")
            source = accepted[ids[index]]
            _same(item.get("ue_path"), source.get("ue_path"), f"{path}.payload.records[{index}].ue_path")
            _same(item.get("asset_type"), source.get("asset_type"), f"{path}.payload.records[{index}].asset_type")
    elif phase == "render":
        records = _array(payload.get("records"), f"{path}.payload.records")
        ids = _validate_record_membership(
            records, accepted, f"{path}.payload.records", exact=False, unique=False
        )
        if set(ids) != set(accepted):
            _fail("CONTRACT_ASSET_SET_MISMATCH", f"{path}.payload.records", "Render set omits an accepted asset")
        expected_render = _object(_object(plan.get("expected_sets"), "$.execution_plan.expected_sets").get("renders"), "$.execution_plan.expected_sets.renders")
        _same(payload.get("render_set_count"), len(records), f"{path}.payload.render_set_count")
        _same(len(records), expected_render.get("count"), f"{path}.payload.records")
        _same(payload.get("render_set_sha256"), expected_render.get("sorted_ids_sha256"), f"{path}.payload.render_set_sha256")
    elif phase == "caption":
        records = _array(payload.get("records"), f"{path}.payload.records")
        _validate_record_membership(records, accepted, f"{path}.payload.records", exact=True)
        usage = _object(payload.get("usage"), f"{path}.payload.usage")
        caption = _object(plan.get("caption"), "$.execution_plan.caption")
        cost = _object(approvals["cost_owner"].get("authorization"), "$.approvals.cost_owner.authorization")
        limits = (
            ("calls", "max_calls", "max_caption_calls"),
            ("output_tokens", "max_output_tokens", "max_output_tokens"),
            ("cost_minor_units", "max_cost_minor_units", "max_cost_minor_units"),
        )
        for usage_key, plan_key, approval_key in limits:
            value = _integer(usage.get(usage_key), f"{path}.payload.usage.{usage_key}")
            if value > _integer(caption.get(plan_key), f"$.execution_plan.caption.{plan_key}") or value > _integer(cost.get(approval_key), f"$.approvals.cost_owner.authorization.{approval_key}"):
                _fail("CONTRACT_COST_UNAUTHORIZED", f"{path}.payload.usage.{usage_key}", "Observed provider usage exceeds approval")
        output_sum = sum(_integer(_object(record, f"{path}.payload.records").get("output_tokens"), f"{path}.payload.records.output_tokens", minimum=1) for record in records)
        _same(usage.get("output_tokens"), output_sum, f"{path}.payload.usage.output_tokens")
        for key in ("currency", "minor_unit_exponent", "rounding_mode", "retry_charge_policy"):
            _same(usage.get(key), caption.get(key), f"{path}.payload.usage.{key}")
    elif phase == "embed":
        records = _array(payload.get("records"), f"{path}.payload.records")
        _validate_record_membership(records, accepted, f"{path}.payload.records", exact=True)
        embedding = _object(plan.get("embedding"), "$.execution_plan.embedding")
        _same(payload.get("dense_model_revision"), embedding.get("dense_model_revision"), f"{path}.payload.dense_model_revision")
        _same(payload.get("sparse_model_revision"), embedding.get("sparse_model_revision"), f"{path}.payload.sparse_model_revision")
        for index, value in enumerate(records):
            record = _object(value, f"{path}.payload.records[{index}]")
            _same(record.get("dense_dimensions"), embedding.get("dense_dimensions"), f"{path}.payload.records[{index}].dense_dimensions")
            _same(record.get("finite"), True, f"{path}.payload.records[{index}].finite")
    elif phase == "postgres":
        records = _array(payload.get("rows"), f"{path}.payload.rows")
        _validate_record_membership(records, accepted, f"{path}.payload.rows", exact=True)
        storage = _object(_object(plan.get("storage"), "$.execution_plan.storage").get("postgres"), "$.execution_plan.storage.postgres")
        for key in ("deployment_identity", "database", "schema_name", "schema_revision", "generation_id"):
            _same(payload.get(key), storage.get(key), f"{path}.payload.{key}")
    elif phase == "qdrant":
        records = _array(payload.get("points"), f"{path}.payload.points")
        _validate_record_membership(records, accepted, f"{path}.payload.points", exact=True)
        storage = _object(_object(plan.get("storage"), "$.execution_plan.storage").get("qdrant"), "$.execution_plan.storage.qdrant")
        for key in ("cluster_identity", "collection", "dense_vector_name", "sparse_vector_name", "generation_id", "point_id_policy", "same_id_replay_policy"):
            _same(payload.get(key), storage.get(key), f"{path}.payload.{key}")
    elif phase == "reconcile":
        _validate_reconcile_payload(
            payload,
            plan,
            accepted,
            now=now,
            phase_completed=phase_completed,
            max_live_audit_ttl=max_live_audit_ttl,
            path=f"{path}.payload",
        )


def _validate_reconcile_payload(
    payload: Mapping[str, Any],
    plan: Mapping[str, Any],
    accepted: Mapping[str, Mapping[str, Any]],
    *,
    now: dt.datetime,
    phase_completed: dt.datetime,
    max_live_audit_ttl: dt.timedelta,
    path: str,
) -> None:
    compatibility = payload.get("compatibility_snapshot_v1_sha256")
    if compatibility is None:
        _fail("CONTRACT_COMPATIBILITY_RECEIPT_MISSING", f"{path}.compatibility_snapshot_v1_sha256", "Successful reconcile requires compatibility receipt")
    _sha(compatibility, f"{path}.compatibility_snapshot_v1_sha256")
    authoritative = _object(payload.get("authoritative_snapshot"), f"{path}.authoritative_snapshot")
    expected_assets = _object(_object(plan.get("expected_sets"), "$.execution_plan.expected_sets").get("assets"), "$.execution_plan.expected_sets.assets")
    _same(authoritative.get("asset_set"), expected_assets, f"{path}.authoritative_snapshot.asset_set")
    _same(authoritative.get("snapshot_revision"), _object(plan.get("reviewed_job"), "$.execution_plan.reviewed_job").get("target_snapshot_revision"), f"{path}.authoritative_snapshot.snapshot_revision")
    live = _object(payload.get("live_audit"), f"{path}.live_audit")
    issued = _parse_time(live.get("issued_at"), f"{path}.live_audit.issued_at")
    expires = _parse_time(live.get("expires_at"), f"{path}.live_audit.expires_at")
    if (
        issued > phase_completed
        or expires <= now
        or expires <= issued
        or expires - issued > max_live_audit_ttl
    ):
        _fail("CONTRACT_LIVE_AUDIT_STALE", f"{path}.live_audit", "Live audit is stale or exceeds its TTL")
    _same(live.get("manifest_sha256"), compatibility, f"{path}.live_audit.manifest_sha256")

    acceptance = _object(plan.get("acceptance"), "$.execution_plan.acceptance")
    planned_queries: dict[str, Mapping[str, Any]] = {}
    for index, item in enumerate(
        _array(
            acceptance.get("shadow_queries"),
            "$.execution_plan.acceptance.shadow_queries",
        )
    ):
        item_path = f"$.execution_plan.acceptance.shadow_queries[{index}]"
        planned = _object(item, item_path)
        query_id = _valid_text(planned.get("query_id"), f"{item_path}.query_id")
        if query_id in planned_queries:
            _fail(
                "CONTRACT_QUERY_INVALID",
                "$.execution_plan.acceptance.shadow_queries",
                "Planned query ID is duplicated",
            )
        planned_queries[query_id] = planned
    observed_queries: dict[str, Mapping[str, Any]] = {}
    for index, value in enumerate(_array(payload.get("query_results"), f"{path}.query_results")):
        result = _object(value, f"{path}.query_results[{index}]")
        query_id = _valid_text(result.get("query_id"), f"{path}.query_results[{index}].query_id")
        if query_id in observed_queries:
            _fail("CONTRACT_QUERY_INVALID", f"{path}.query_results", "Query result is duplicated")
        observed_queries[query_id] = result
    if set(observed_queries) != set(planned_queries):
        _fail("CONTRACT_QUERY_INVALID", f"{path}.query_results", "Query result set does not match plan")
    for query_id, planned in planned_queries.items():
        result = observed_queries[query_id]
        for key in ("language", "query_text_sha256", "normalization_revision", "expected_asset_ids", "minimum_rank"):
            _same(result.get(key), planned.get(key), f"{path}.query_results.{query_id}.{key}")
        _same(result.get("passed"), True, f"{path}.query_results.{query_id}.passed")
        returned = _asset_ids(result.get("returned_asset_ids"), f"{path}.query_results.{query_id}.returned_asset_ids")
        if not set(returned) <= set(accepted):
            _fail("CONTRACT_QUERY_INVALID", f"{path}.query_results.{query_id}", "Query returned an unaccepted asset")
        expected_ids = set(_asset_ids(planned.get("expected_asset_ids"), f"$.execution_plan.acceptance.shadow_queries.{query_id}.expected_asset_ids"))
        ranks = [index + 1 for index, asset_id in enumerate(returned) if asset_id in expected_ids]
        if not ranks:
            _fail("CONTRACT_QUERY_INVALID", f"{path}.query_results.{query_id}", "Passed query returned no expected asset")
        best = min(ranks)
        _same(result.get("best_expected_rank"), best, f"{path}.query_results.{query_id}.best_expected_rank")
        if best > _integer(planned.get("minimum_rank"), f"$.execution_plan.acceptance.shadow_queries.{query_id}.minimum_rank", minimum=1):
            _fail("CONTRACT_QUERY_INVALID", f"{path}.query_results.{query_id}", "Expected asset rank misses threshold")

    planned_smoke = _array(acceptance.get("smoke_targets"), "$.execution_plan.acceptance.smoke_targets")
    observed_smoke = _array(payload.get("smoke_results"), f"{path}.smoke_results")
    if len(planned_smoke) != len(observed_smoke):
        _fail("CONTRACT_SMOKE_INVALID", f"{path}.smoke_results", "Smoke result count does not match plan")
    for index, planned_value in enumerate(planned_smoke):
        planned = _object(planned_value, f"$.execution_plan.acceptance.smoke_targets[{index}]")
        observed = _object(observed_smoke[index], f"{path}.smoke_results[{index}]")
        asset_id = _valid_text(observed.get("asset_id"), f"{path}.smoke_results[{index}].asset_id")
        if asset_id not in accepted:
            _fail("CONTRACT_SMOKE_INVALID", f"{path}.smoke_results[{index}]", "Smoke result asset is not accepted")
        for key in ("asset_id", "asset_type"):
            _same(observed.get(key), planned.get(key), f"{path}.smoke_results[{index}].{key}")

    target_map = _expected_target_map(plan)
    runtime = _array(payload.get("runtime_attestations"), f"{path}.runtime_attestations")
    expected_components = ("unreal", "worker", "embedding", "postgres", "qdrant")
    if len(runtime) != len(expected_components):
        _fail("CONTRACT_RUNTIME_ATTESTATION_MISMATCH", f"{path}.runtime_attestations", "Runtime attestation set is incomplete")
    for index, component in enumerate(expected_components):
        observed = _object(runtime[index], f"{path}.runtime_attestations[{index}]")
        identity, image = target_map[component]
        _same(observed.get("component"), component, f"{path}.runtime_attestations[{index}].component")
        _same(observed.get("target_identity"), identity, f"{path}.runtime_attestations[{index}].target_identity")
        _same(observed.get("runtime_image"), image, f"{path}.runtime_attestations[{index}].runtime_image")
        if component == "worker":
            expected_attestation = _object(
                plan.get("worker"), "$.execution_plan.worker"
            ).get("runtime_attestation_sha256")
        else:
            expected_attestation = _object(
                _object(
                    plan.get("target_attestations"),
                    "$.execution_plan.target_attestations",
                ).get(component),
                f"$.execution_plan.target_attestations.{component}",
            ).get("runtime_attestation_sha256")
        _same(
            observed.get("attestation_sha256"),
            expected_attestation,
            f"{path}.runtime_attestations[{index}].attestation_sha256",
        )

    generation = _object(plan.get("generation"), "$.execution_plan.generation")
    previous = _object(generation.get("previous_active"), "$.execution_plan.generation.previous_active")
    observed_previous = _object(payload.get("previous_active"), f"{path}.previous_active")
    _same(observed_previous.get("generation_id"), previous.get("generation_id"), f"{path}.previous_active.generation_id")
    _same(observed_previous.get("snapshot_revision"), previous.get("snapshot_revision"), f"{path}.previous_active.snapshot_revision")
    if previous.get("generation_id") is not None:
        _same(observed_previous.get("ready"), True, f"{path}.previous_active.ready")
    pointer = _object(generation.get("active_generation_pointer"), "$.execution_plan.generation.active_generation_pointer")
    observed_pointer = _object(payload.get("active_generation_pointer"), f"{path}.active_generation_pointer")
    _same(observed_pointer.get("identity"), pointer.get("identity"), f"{path}.active_generation_pointer.identity")
    _same(observed_pointer.get("observed_epoch"), pointer.get("observed_epoch"), f"{path}.active_generation_pointer.observed_epoch")
    _same(observed_pointer.get("unchanged"), True, f"{path}.active_generation_pointer.unchanged")
    _same(payload.get("activation_performed"), False, f"{path}.activation_performed")


def _evidence_output_sha256(evidence: Mapping[str, Any], phase: str, path: str) -> str:
    if phase == "reconcile":
        payload = _object(evidence.get("payload"), f"{path}.payload")
        return _sha(
            _object(payload.get("authoritative_snapshot"), f"{path}.payload.authoritative_snapshot").get("sha256"),
            f"{path}.payload.authoritative_snapshot.sha256",
        )
    field = OUTPUT_ARTIFACT_FIELDS[phase]
    return _sha(_object(evidence.get("payload"), f"{path}.payload").get(field), f"{path}.payload.{field}")


def _validate_evidence_signature(evidence: Mapping[str, Any], path: str) -> None:
    signature = _object(evidence.get("worker_signature"), f"{path}.worker_signature")
    unsigned = dict(evidence)
    unsigned.pop("worker_signature", None)
    _same(
        signature.get("signed_payload_sha256"),
        canonical_sha256(unsigned),
        f"{path}.worker_signature.signed_payload_sha256",
    )
    _sha(signature.get("signature_sha256"), f"{path}.worker_signature.signature_sha256")


def _validate_phase_request_bundle(
    requests_by_phase: Mapping[str, Mapping[str, Any]],
    request_sha256: Mapping[str, str],
    plan: Mapping[str, Any],
    plan_sha256: str,
    job: Mapping[str, Any],
    job_sha256: str,
    basis_sha256: str,
) -> None:
    if set(requests_by_phase) != set(PHASES) or set(request_sha256) != set(PHASES):
        _fail(
            "CONTRACT_REQUEST_SET_INVALID",
            "$.phase_requests",
            "Exactly seven phase requests are required",
        )
    run = _object(plan.get("run_identity"), "$.execution_plan.run_identity")
    worker = _object(plan.get("worker"), "$.execution_plan.worker")
    deployment = _object(
        plan.get("deployment_preflight"), "$.execution_plan.deployment_preflight"
    )
    generation = _object(plan.get("generation"), "$.execution_plan.generation")
    unreal = _object(plan.get("unreal"), "$.execution_plan.unreal")
    expected_sets = _object(plan.get("expected_sets"), "$.execution_plan.expected_sets")
    credentials = _object(plan.get("credentials"), "$.execution_plan.credentials")
    ordinary = _object(
        credentials.get("ordinary_phases"),
        "$.execution_plan.credentials.ordinary_phases",
    )
    reconcile_credentials = _object(
        credentials.get("reconcile"), "$.execution_plan.credentials.reconcile"
    )
    target_attestations = _object(
        plan.get("target_attestations"), "$.execution_plan.target_attestations"
    )
    expected_execution = {
        "run_id": run.get("run_id"),
        "correlation_id": run.get("correlation_id"),
        "owner_identity": run.get("service_owner_id"),
        "lease_id": unreal.get("lease_id"),
        "slot_id": unreal.get("slot_id"),
    }
    expected_worker = {
        "deployment_identity": deployment.get("deployment_identity"),
        "runtime_image": deployment.get("worker_image"),
        "whoami_sha256": worker.get("whoami_sha256"),
        "peer_attestation_sha256": worker.get("peer_attestation_sha256"),
        "runtime_attestation_sha256": worker.get("runtime_attestation_sha256"),
        "host_boot_id_sha256": worker.get("host_boot_id_sha256"),
        "peer_uid": worker.get("socket_owner_uid"),
        "peer_gid": worker.get("socket_owner_gid"),
        "peer_pid": worker.get("peer_pid"),
        "process_start_time_ticks": worker.get("process_start_time_ticks"),
        "process_start_token_sha256": worker.get("process_start_token_sha256"),
        "socket_device": worker.get("socket_device"),
        "socket_inode": worker.get("socket_inode"),
        "socket_inode_binding_sha256": worker.get("socket_inode_binding_sha256"),
    }
    counts = {
        name: _object(expected_sets.get(name), f"$.execution_plan.expected_sets.{name}").get("count")
        for name in (
            "assets",
            "renders",
            "catalog_records",
            "dense_vectors",
            "sparse_vectors",
            "postgres_rows",
            "qdrant_points",
        )
    }
    expected_metrics = {
        "inspect": {"assets_inspected": counts["assets"]},
        "render": {
            "assets_rendered": counts["assets"],
            "rendered_views": counts["renders"],
        },
        "caption": {"catalog_records": counts["catalog_records"]},
        "embed": {
            "dense_vectors": counts["dense_vectors"],
            "sparse_vectors": counts["sparse_vectors"],
        },
        "postgres": {"postgres_rows": counts["postgres_rows"]},
        "qdrant": {"qdrant_points": counts["qdrant_points"]},
        "reconcile": {
            "catalog_records": counts["catalog_records"],
            "postgres_rows": counts["postgres_rows"],
            "qdrant_points": counts["qdrant_points"],
        },
    }
    request_ids: set[str] = set()
    idempotency_keys: set[str] = set()
    for phase in PHASES:
        request = requests_by_phase[phase]
        path = f"$.phase_requests.{phase}"
        operation = PHASE_OPERATION[phase]
        _same(request.get("schema"), "simworld-semantic-index-phase-request/v1", f"{path}.schema")
        _same(request.get("protocol"), "simworld-semantic-index-worker/v1", f"{path}.protocol")
        _same(request.get("phase"), phase, f"{path}.phase")
        _same(request.get("operation"), operation, f"{path}.operation")
        _same(
            request.get("operation_revision"),
            worker_operation_revision(operation),
            f"{path}.operation_revision",
        )
        _same(request.get("job_revision"), job.get("job_revision"), f"{path}.job_revision")
        _same(request.get("reviewed_job_sha256"), job_sha256, f"{path}.reviewed_job_sha256")
        _same(request.get("approval_basis_sha256"), basis_sha256, f"{path}.approval_basis_sha256")
        _same(request.get("execution_plan_sha256"), plan_sha256, f"{path}.execution_plan_sha256")
        _same(request.get("generation_id"), generation.get("generation_id"), f"{path}.generation_id")
        _same(
            request.get("generation_binding_sha256"),
            generation.get("target_generation_identities_sha256"),
            f"{path}.generation_binding_sha256",
        )
        _same(request.get("execution_binding"), expected_execution, f"{path}.execution_binding")
        _same(request.get("worker_binding"), expected_worker, f"{path}.worker_binding")
        _same(request.get("expected_metrics"), expected_metrics[phase], f"{path}.expected_metrics")
        _same(
            request.get("idempotency_ledger_identity"),
            worker.get("idempotency_ledger_identity"),
            f"{path}.idempotency_ledger_identity",
        )
        _same(
            request.get("idempotency_ledger_revision"),
            worker.get("idempotency_ledger_revision"),
            f"{path}.idempotency_ledger_revision",
        )

        if phase == "reconcile":
            capabilities = [
                _object(
                    reconcile_credentials.get(name),
                    f"$.execution_plan.credentials.reconcile.{name}",
                )
                for name in ("postgres_read", "qdrant_read", "ue_runtime_read")
            ]
        else:
            capabilities = [
                _object(
                    ordinary.get(phase),
                    f"$.execution_plan.credentials.ordinary_phases.{phase}",
                )
            ]
        transport = _object(request.get("credential_transport"), f"{path}.credential_transport")
        _same(transport.get("kind"), "sealed_memfd_scm_rights_v1", f"{path}.credential_transport.kind")
        _same(transport.get("json_contains_credential_bytes"), False, f"{path}.credential_transport.json_contains_credential_bytes")
        descriptors = _array(transport.get("descriptors"), f"{path}.credential_transport.descriptors")
        targets = _array(request.get("expected_targets"), f"{path}.expected_targets")
        if len(descriptors) != len(capabilities) or len(targets) != len(capabilities):
            _fail(
                "CONTRACT_CAPABILITY_MISMATCH",
                path,
                "Request capability and target sets are incomplete",
            )
        for index, (capability, component) in enumerate(
            zip(capabilities, PHASE_COMPONENTS[phase])
        ):
            descriptor = _object(descriptors[index], f"{path}.credential_transport.descriptors[{index}]")
            expected_descriptor = {
                "fd_index": index,
                "scope": capability.get("scope"),
                "credential_generation": capability.get("credential_generation"),
                "generation_id": generation.get("generation_id"),
                "target_identity": capability.get("target_identity"),
                "byte_count": descriptor.get("byte_count"),
            }
            _same(descriptor, expected_descriptor, f"{path}.credential_transport.descriptors[{index}]")
            attestation = _object(
                target_attestations.get(component),
                f"$.execution_plan.target_attestations.{component}",
            )
            expected_target = {
                "component": component,
                "scope": capability.get("scope"),
                "credential_generation": capability.get("credential_generation"),
                "generation_id": generation.get("generation_id"),
                "target_identity": capability.get("target_identity"),
                "runtime_identity": attestation.get("runtime_identity"),
                "runtime_image": attestation.get("runtime_image"),
                "whoami_sha256": attestation.get("whoami_sha256"),
                "runtime_attestation_sha256": attestation.get("runtime_attestation_sha256"),
            }
            _same(targets[index], expected_target, f"{path}.expected_targets[{index}]")
        request_id = _valid_text(request.get("request_id"), f"{path}.request_id")
        idempotency_key = _valid_text(
            request.get("idempotency_key"), f"{path}.idempotency_key"
        )
        if request_id in request_ids or idempotency_key in idempotency_keys:
            _fail(
                "CONTRACT_REQUEST_SET_INVALID",
                path,
                "Phase request identities must be unique",
            )
        request_ids.add(request_id)
        idempotency_keys.add(idempotency_key)


def _validate_evidence_bundle(
    evidence_by_phase: Mapping[str, Mapping[str, Any]],
    evidence_sha256: Mapping[str, str],
    requests_by_phase: Mapping[str, Mapping[str, Any]],
    request_sha256: Mapping[str, str],
    plan: Mapping[str, Any],
    plan_sha256: str,
    job: Mapping[str, Any],
    job_sha256: str,
    basis_sha256: str,
    approvals: Mapping[str, Mapping[str, Any]],
    accepted: Mapping[str, Mapping[str, Any]],
    *,
    now: dt.datetime,
    skew: dt.timedelta,
    max_live_audit_ttl: dt.timedelta,
) -> None:
    if set(evidence_by_phase) != set(PHASES) or set(evidence_sha256) != set(PHASES):
        _fail("CONTRACT_EVIDENCE_SET_INVALID", "$.phase_evidence", "Exactly seven phase receipts are required")
    plan_run = _object(plan.get("run_identity"), "$.execution_plan.run_identity")
    plan_worker = _object(plan.get("worker"), "$.execution_plan.worker")
    unreal = _object(plan.get("unreal"), "$.execution_plan.unreal")
    generation = _object(plan.get("generation"), "$.execution_plan.generation")
    deployment = _object(plan.get("deployment_preflight"), "$.execution_plan.deployment_preflight")
    reservation = _object(
        generation.get("reservation"), "$.execution_plan.generation.reservation"
    )
    reservation_issued = _parse_time(
        reservation.get("issued_at"),
        "$.execution_plan.generation.reservation.issued_at",
    )
    reservation_expires = _parse_time(
        reservation.get("expires_at"),
        "$.execution_plan.generation.reservation.expires_at",
    )
    deployment_issued = _parse_time(
        deployment.get("issued_at"), "$.execution_plan.deployment_preflight.issued_at"
    )
    deployment_expires = _parse_time(
        deployment.get("expires_at"), "$.execution_plan.deployment_preflight.expires_at"
    )
    expected_asset_set = _object(
        _object(plan.get("expected_sets"), "$.execution_plan.expected_sets").get("assets"),
        "$.execution_plan.expected_sets.assets",
    )
    source = _object(job.get("source"), "$.reviewed_job.source")
    accepted_set = _object(
        _object(job.get("reviewed_selection"), "$.reviewed_job.reviewed_selection").get("accepted_set"),
        "$.reviewed_job.reviewed_selection.accepted_set",
    )
    expected_job_binding = {
        "schema": job.get("schema"),
        "sha256": job_sha256,
        "revision": job.get("job_revision"),
        "accepted_asset_count": len(accepted),
        "accepted_assets_sha256": accepted_set.get("assets_sha256"),
        "content_revision": _object(source.get("content"), "$.reviewed_job.source.content").get("revision"),
        "snapshot_revision": source.get("target_snapshot_revision"),
    }
    expected_plan_binding = {
        "schema": plan.get("schema"),
        "sha256": plan_sha256,
        "approval_basis_sha256": basis_sha256,
        "generation_id": generation.get("generation_id"),
        "generation_nonce_sha256": generation.get("generation_nonce_sha256"),
        "reservation_receipt_sha256": _object(generation.get("reservation"), "$.execution_plan.generation.reservation").get("receipt_sha256"),
        "worker_deployment_identity": deployment.get("deployment_identity"),
    }
    prior_completed: dt.datetime | None = None
    prior_success = True
    outputs: dict[str, str] = {}
    ids: dict[str, str] = {}
    for index, phase in enumerate(PHASES):
        evidence = evidence_by_phase[phase]
        path = f"$.phase_evidence.{phase}"
        if evidence.get("schema") != "simworld-semantic-index-phase-evidence/v1":
            _fail("CONTRACT_SCHEMA_INVALID", f"{path}.schema", "Phase evidence schema is invalid")
        _same(evidence.get("phase"), phase, f"{path}.phase")
        _same(evidence.get("operation"), PHASE_OPERATION[phase], f"{path}.operation")
        phase_request = requests_by_phase[phase]
        _same(
            evidence.get("operation_revision"),
            phase_request.get("operation_revision"),
            f"{path}.operation_revision",
        )
        phase_policy = _object(_object(plan.get("phase_policy"), "$.execution_plan.phase_policy").get(phase), f"$.execution_plan.phase_policy.{phase}")
        _same(phase_policy.get("operation"), PHASE_OPERATION[phase], f"$.execution_plan.phase_policy.{phase}.operation")
        _same(evidence.get("job_binding"), expected_job_binding, f"{path}.job_binding")
        _same(evidence.get("plan_binding"), expected_plan_binding, f"{path}.plan_binding")
        expected_run = {
            "run_id": plan_run.get("run_id"),
            "correlation_id": plan_run.get("correlation_id"),
            "operator_id": plan_run.get("operator_id"),
            "service_owner_id": plan_run.get("service_owner_id"),
            "ue_slot_id": unreal.get("slot_id"),
            "ue_lease_id": unreal.get("lease_id"),
            "ue_lease_expires_at": unreal.get("lease_expires_at"),
            "retention_class": plan_run.get("retention_class"),
        }
        _same(evidence.get("run_identity"), expected_run, f"{path}.run_identity")
        _same(evidence.get("asset_set"), expected_asset_set, f"{path}.asset_set")
        idempotency = _object(evidence.get("idempotency"), f"{path}.idempotency")
        _same(
            idempotency.get("key"),
            phase_request.get("idempotency_key"),
            f"{path}.idempotency.key",
        )
        _same(
            idempotency.get("request_sha256"),
            request_sha256[phase],
            f"{path}.idempotency.request_sha256",
        )
        _same(
            idempotency.get("ledger_identity"),
            phase_request.get("idempotency_ledger_identity"),
            f"{path}.idempotency.ledger_identity",
        )
        _same(
            idempotency.get("ledger_identity"),
            plan_worker.get("idempotency_ledger_identity"),
            f"{path}.idempotency.ledger_identity",
        )
        _same(
            idempotency.get("ledger_revision"),
            phase_request.get("idempotency_ledger_revision"),
            f"{path}.idempotency.ledger_revision",
        )
        _same(
            idempotency.get("ledger_revision"),
            plan_worker.get("idempotency_ledger_revision"),
            f"{path}.idempotency.ledger_revision",
        )
        request = _object(evidence.get("request"), f"{path}.request")
        expected_request_summary = {
            "schema": phase_request.get("schema"),
            "sha256": request_sha256[phase],
            "input_artifact_sha256": _object(
                phase_request.get("input_binding"), f"$.phase_requests.{phase}.input_binding"
            ).get("sha256"),
            "deadline_monotonic_ns": phase_request.get("deadline_monotonic_ns"),
            "host_boot_id_sha256": _object(
                phase_request.get("worker_binding"), f"$.phase_requests.{phase}.worker_binding"
            ).get("host_boot_id_sha256"),
            "credential_transport": _object(
                phase_request.get("credential_transport"),
                f"$.phase_requests.{phase}.credential_transport",
            ).get("kind"),
            "json_contains_credential_bytes": _object(
                phase_request.get("credential_transport"),
                f"$.phase_requests.{phase}.credential_transport",
            ).get("json_contains_credential_bytes"),
        }
        _same(request, expected_request_summary, f"{path}.request")
        _validate_evidence_signature(evidence, path)
        _validate_evidence_targets(evidence, plan, phase, path)

        started = _parse_time(evidence.get("started_at"), f"{path}.started_at")
        completed = _parse_time(evidence.get("completed_at"), f"{path}.completed_at")
        if completed < started or completed > now + skew:
            _fail("CONTRACT_TIME_INVALID", path, "Phase completion interval is invalid")
        lease_issued = _parse_time(unreal.get("lease_issued_at"), "$.execution_plan.unreal.lease_issued_at")
        lease_expires = _parse_time(unreal.get("lease_expires_at"), "$.execution_plan.unreal.lease_expires_at")
        if started < lease_issued or completed >= lease_expires:
            _fail("CONTRACT_TIME_INVALID", path, "Phase falls outside the UE lease")
        if started < reservation_issued or completed >= reservation_expires:
            _fail("CONTRACT_TIME_INVALID", path, "Phase falls outside the generation reservation")
        if started < deployment_issued or completed >= deployment_expires:
            _fail("CONTRACT_TIME_INVALID", path, "Phase falls outside deployment attestation validity")
        if prior_completed is not None and started < prior_completed:
            _fail("CONTRACT_TIME_INVALID", path, "Phase overlaps or precedes its dependency")
        for approval_kind, receipt in approvals.items():
            issued = _parse_time(receipt.get("issued_at"), f"$.approvals.{approval_kind}.issued_at")
            expires = _parse_time(receipt.get("expires_at"), f"$.approvals.{approval_kind}.expires_at")
            if started < issued or completed >= expires:
                _fail("CONTRACT_APPROVAL_EXPIRED", path, "Phase is outside an approval interval")

        expected_priors = []
        for prior_phase in PHASES[:index]:
            expected_priors.append(
                {
                    "phase": prior_phase,
                    "evidence_id": ids[prior_phase],
                    "evidence_sha256": evidence_sha256[prior_phase],
                    "asset_set_sha256": expected_asset_set.get("sorted_ids_sha256"),
                    "output_artifact_sha256": outputs[prior_phase],
                }
            )
        _same(evidence.get("prior_receipts"), expected_priors, f"{path}.prior_receipts")
        success = _validate_outcome(evidence, phase, path)
        outcome = _object(evidence.get("outcome"), f"{path}.outcome")
        allowed_mutation_states = _array(
            phase_policy.get("allowed_mutation_states"),
            f"$.execution_plan.phase_policy.{phase}.allowed_mutation_states",
        )
        if outcome.get("mutation_state") not in allowed_mutation_states:
            _fail(
                "CONTRACT_MUTATION_STATE_INVALID",
                f"{path}.outcome.mutation_state",
                "Mutation state is not allowed by the phase policy",
            )
        if success and not prior_success:
            _fail("CONTRACT_EVIDENCE_CHAIN_INVALID", path, "A successful phase follows a failed dependency")
        if success:
            _validate_phase_payload(
                evidence,
                phase,
                plan,
                accepted,
                approvals,
                now=now,
                phase_completed=completed,
                max_live_audit_ttl=max_live_audit_ttl,
                path=path,
            )
            outputs[phase] = _evidence_output_sha256(evidence, phase, path)
        else:
            prior_success = False
            payload = _object(evidence.get("payload"), f"{path}.payload")
            fallback = payload.get("last_safe_artifact_sha256")
            outputs[phase] = "0" * 64 if fallback is None else _sha(fallback, f"{path}.payload.last_safe_artifact_sha256")
        ids[phase] = _valid_text(evidence.get("evidence_id"), f"{path}.evidence_id")
        prior_completed = completed

    reconcile = evidence_by_phase["reconcile"]
    if _object(reconcile.get("outcome"), "$.phase_evidence.reconcile.outcome").get("status") != "success":
        _fail("CONTRACT_TERMINAL_STATE_INVALID", "$.phase_evidence.reconcile", "A complete Production bundle requires successful reconcile")
    reconcile_payload = _object(
        reconcile.get("payload"), "$.phase_evidence.reconcile.payload"
    )
    for reconcile_key, source_phase in (
        ("catalog_sha256", "caption"),
        ("postgres_row_set_sha256", "postgres"),
        ("qdrant_point_set_sha256", "qdrant"),
    ):
        _same(
            reconcile_payload.get(reconcile_key),
            outputs[source_phase],
            f"$.phase_evidence.reconcile.payload.{reconcile_key}",
        )
    _same(
        reconcile_payload.get("exact_set_parity"),
        True,
        "$.phase_evidence.reconcile.payload.exact_set_parity",
    )
    rollback_ref = _object(
        _object(plan.get("approvals"), "$.execution_plan.approvals").get(
            "rollback_readiness"
        ),
        "$.execution_plan.approvals.rollback_readiness",
    )
    _same(
        reconcile_payload.get("rollback_readiness_receipt_sha256"),
        rollback_ref.get("receipt_sha256"),
        "$.phase_evidence.reconcile.payload.rollback_readiness_receipt_sha256",
    )
    observed_smoke = _array(
        reconcile_payload.get("smoke_results"),
        "$.phase_evidence.reconcile.payload.smoke_results",
    )
    for index, value in enumerate(observed_smoke):
        result = _object(
            value, f"$.phase_evidence.reconcile.payload.smoke_results[{index}]"
        )
        _same(
            result.get("inspect_receipt_sha256"),
            evidence_sha256["inspect"],
            f"$.phase_evidence.reconcile.payload.smoke_results[{index}].inspect_receipt_sha256",
        )


def validate_production_contracts(
    bundle: ProductionContractBundle,
    *,
    now_utc: dt.datetime,
    max_clock_skew_seconds: int = DEFAULT_CLOCK_SKEW_SECONDS,
    max_live_audit_ttl_seconds: int = DEFAULT_LIVE_AUDIT_TTL_SECONDS,
) -> ValidatedProductionContracts:
    """Validate a complete, sealed v2 Production contract bundle.

    ``now_utc`` is mandatory so tests and launchers do not depend on ambient
    wall-clock discovery.  Ed25519 verification remains the launcher's trust
    boundary; this function validates the signed-payload and signature-byte
    hashes, identities, scopes, freshness, and all semantic relationships.
    """

    if type(bundle) is not ProductionContractBundle:
        _fail("CONTRACT_BUNDLE_INVALID", "$.bundle", "Expected an exact Production bundle")
    if type(bundle.approvals) is not dict:
        _fail("CONTRACT_BUNDLE_INVALID", "$.approvals", "Approvals must be an exact mapping")
    if type(bundle.phase_requests) is not tuple:
        _fail(
            "CONTRACT_BUNDLE_INVALID",
            "$.phase_requests",
            "Phase requests must be an immutable tuple",
        )
    if type(bundle.phase_evidence) is not tuple:
        _fail(
            "CONTRACT_BUNDLE_INVALID",
            "$.phase_evidence",
            "Phase evidence must be an immutable tuple",
        )
    if type(bundle.contract_schemas) is not dict:
        _fail(
            "CONTRACT_BUNDLE_INVALID",
            "$.contract_schemas",
            "Contract schemas must be an exact mapping",
        )
    if type(bundle.secret_values) is not tuple:
        _fail(
            "CONTRACT_BUNDLE_INVALID",
            "$.secret_values",
            "Secret values must be an immutable tuple",
        )
    if not isinstance(now_utc, dt.datetime) or now_utc.tzinfo is None or now_utc.utcoffset() is None:
        _fail("CONTRACT_TIME_INVALID", "$.now_utc", "Current time must be timezone-aware")
    now = now_utc.astimezone(dt.timezone.utc)
    skew_seconds = _integer(max_clock_skew_seconds, "$.max_clock_skew_seconds")
    ttl_seconds = _integer(max_live_audit_ttl_seconds, "$.max_live_audit_ttl_seconds", minimum=1)
    if skew_seconds > MAX_CLOCK_SKEW_SECONDS:
        _fail(
            "CONTRACT_TIME_POLICY_INVALID",
            "$.max_clock_skew_seconds",
            "Clock-skew allowance exceeds the hard safety limit",
        )
    if ttl_seconds > MAX_LIVE_AUDIT_TTL_SECONDS:
        _fail(
            "CONTRACT_TIME_POLICY_INVALID",
            "$.max_live_audit_ttl_seconds",
            "Live-audit TTL exceeds the hard safety limit",
        )
    skew = dt.timedelta(seconds=skew_seconds)
    live_ttl = dt.timedelta(seconds=ttl_seconds)
    _scan_secrets(bundle)
    formal_schemas, formal_schema_digests, formal_registry = (
        _load_formal_schema_bundle(bundle)
    )

    source_digest = _document_digest(bundle.source_candidates, "$.source_candidates")
    source_candidates = decode_canonical_jcs(bundle.source_candidates.raw)
    if not isinstance(source_candidates, list):
        _fail("CONTRACT_TYPE_INVALID", "$.source_candidates", "Source candidates must be an array")
    job, job_sha256 = _load_document(bundle.reviewed_job, "$.reviewed_job")
    basis, basis_sha256 = _load_document(bundle.approval_basis, "$.approval_basis")
    plan, plan_sha256 = _load_document(bundle.execution_plan, "$.execution_plan")
    _bind_formal_schema_digests(plan, formal_schema_digests)
    _validate_formal_instance(
        formal_schemas["reviewed_job_schema_sha256"],
        job,
        formal_registry,
        "$.reviewed_job",
    )
    _validate_formal_instance(
        formal_schemas["approval_basis_schema_sha256"],
        basis,
        formal_registry,
        "$.approval_basis",
    )
    _validate_formal_instance(
        formal_schemas["execution_plan_schema_sha256"],
        plan,
        formal_registry,
        "$.execution_plan",
    )
    accepted, accepted_ids, _rejected_ids, accepted_assets_sha256 = _validate_reviewed_job(
        job, job_sha256, source_candidates, source_digest
    )
    generation_id = _validate_plan(
        plan,
        plan_sha256,
        job,
        job_sha256,
        basis,
        basis_sha256,
        accepted_ids,
        accepted_assets_sha256,
        now,
        skew,
    )

    if set(bundle.approvals) != set(APPROVAL_KINDS):
        _fail("CONTRACT_APPROVAL_SET_INVALID", "$.approvals", "Approval receipt set is incomplete")
    approvals: dict[str, Mapping[str, Any]] = {}
    approval_sha256: dict[str, str] = {}
    for kind in APPROVAL_KINDS:
        receipt, digest = _load_document(bundle.approvals[kind], f"$.approvals.{kind}")
        _validate_formal_instance(
            formal_schemas["approval_receipt_schema_sha256"],
            receipt,
            formal_registry,
            f"$.approvals.{kind}",
        )
        approvals[kind] = receipt
        approval_sha256[kind] = digest
    _validate_approvals(
        approvals,
        approval_sha256,
        plan,
        job,
        job_sha256,
        basis_sha256,
        generation_id,
        now=now,
        skew=skew,
    )

    if len(bundle.phase_requests) != len(PHASES):
        _fail(
            "CONTRACT_REQUEST_SET_INVALID",
            "$.phase_requests",
            "Exactly seven phase requests are required",
        )
    requests_by_phase: dict[str, Mapping[str, Any]] = {}
    request_sha256: dict[str, str] = {}
    for index, document in enumerate(bundle.phase_requests):
        request, digest = _load_document(document, f"$.phase_requests[{index}]")
        _validate_formal_instance(
            formal_schemas["phase_request_schema_sha256"],
            request,
            formal_registry,
            f"$.phase_requests[{index}]",
        )
        phase = request.get("phase")
        if phase not in PHASES or phase in requests_by_phase:
            _fail(
                "CONTRACT_REQUEST_SET_INVALID",
                f"$.phase_requests[{index}].phase",
                "Request phase is missing, unknown, or duplicated",
            )
        requests_by_phase[phase] = request
        request_sha256[phase] = digest
    _validate_phase_request_bundle(
        requests_by_phase,
        request_sha256,
        plan,
        plan_sha256,
        job,
        job_sha256,
        basis_sha256,
    )

    if len(bundle.phase_evidence) != len(PHASES):
        _fail("CONTRACT_EVIDENCE_SET_INVALID", "$.phase_evidence", "Exactly seven phase receipts are required")
    evidence_by_phase: dict[str, Mapping[str, Any]] = {}
    evidence_sha256: dict[str, str] = {}
    for index, document in enumerate(bundle.phase_evidence):
        evidence, digest = _load_document(document, f"$.phase_evidence[{index}]")
        _validate_formal_instance(
            formal_schemas["phase_evidence_schema_sha256"],
            evidence,
            formal_registry,
            f"$.phase_evidence[{index}]",
        )
        phase = evidence.get("phase")
        if phase not in PHASES or phase in evidence_by_phase:
            _fail("CONTRACT_EVIDENCE_SET_INVALID", f"$.phase_evidence[{index}].phase", "Phase is missing, unknown, or duplicated")
        evidence_by_phase[phase] = evidence
        evidence_sha256[phase] = digest
    _validate_evidence_bundle(
        evidence_by_phase,
        evidence_sha256,
        requests_by_phase,
        request_sha256,
        plan,
        plan_sha256,
        job,
        job_sha256,
        basis_sha256,
        approvals,
        accepted,
        now=now,
        skew=skew,
        max_live_audit_ttl=live_ttl,
    )
    return ValidatedProductionContracts(
        reviewed_job_sha256=job_sha256,
        approval_basis_sha256=basis_sha256,
        execution_plan_sha256=plan_sha256,
        generation_id=generation_id,
        accepted_asset_ids=tuple(accepted_ids),
        evidence_sha256_by_phase=tuple((phase, evidence_sha256[phase]) for phase in PHASES),
    )


__all__ = [
    "APPROVAL_BASIS_REVISION",
    "APPROVAL_BASIS_SCHEMA",
    "ContractValidationError",
    "GENERATION_DERIVATION",
    "ProductionContractBundle",
    "SealedDocument",
    "ValidatedProductionContracts",
    "canonical_jcs_bytes",
    "canonical_sha256",
    "decode_canonical_jcs",
    "derive_expected_approval_basis",
    "derive_generation_id",
    "derive_target_generation_identities",
    "sorted_id_set_sha256",
    "validate_production_contracts",
    "worker_operation_revision",
]
