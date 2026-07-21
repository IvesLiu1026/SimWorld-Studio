#!/usr/bin/env python3
"""Fail-closed executor for a sealed semantic asset-index job.

The default is a validation-only dry run.  Production execution remains
unavailable until a concrete adapter is added to the static reviewed registry.
The bundled offline fixture exercises orchestration, resume, limits, and
receipt semantics without network, Unreal, providers, databases, or secrets.
"""

from __future__ import annotations

import argparse
import copy
import contextlib
import ctypes
import datetime as dt
import errno
import fcntl
import hashlib
import json
import math
import os
import re
import selectors
import signal
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType
from types import ModuleType
from typing import Any, Callable, Mapping, Sequence


EXECUTION_PLAN_SCHEMA = "simworld-semantic-asset-index-execution-plan/v1"
STATE_SCHEMA = "simworld-semantic-asset-index-execution-state/v1"
TERMINAL_RECEIPT_SCHEMA = "simworld-semantic-asset-index-terminal-receipt/v1"
RESULT_SCHEMA = "simworld-semantic-asset-index-executor-result/v1"
PREPARATION_RECEIPT_SCHEMA = "simworld-semantic-asset-index-job-preparation-receipt/v1"
JOB_SCHEMA = "simworld-semantic-asset-index-job/v1"
RECIPE_SCHEMA = "simworld-semantic-asset-index-recipe/v1"
BOOTSTRAP_RECEIPT_SCHEMA = "simworld-ue-asset-bootstrap-receipt/v1"
OBJECT_MANIFEST_SCHEMA = "simworld-ue-object-manifest/v2"
OFFLINE_ADAPTER_ID = "offline-fixture-v1"
OFFLINE_ADAPTER_REVISION = "semantic-index-offline-fixture-adapter/v1"

PHASES = (
    "inspect",
    "render",
    "caption",
    "embed",
    "postgres",
    "qdrant",
    "reconcile",
)
PHASE_METRIC_KEYS: Mapping[str, frozenset[str]] = {
    "inspect": frozenset({"assets_inspected"}),
    "render": frozenset({"assets_rendered", "rendered_views"}),
    "caption": frozenset({"catalog_records"}),
    "embed": frozenset({"dense_vectors", "sparse_vectors"}),
    "postgres": frozenset({"postgres_rows"}),
    "qdrant": frozenset({"qdrant_points"}),
    "reconcile": frozenset({"catalog_records", "postgres_rows", "qdrant_points"}),
}

MAX_JOB_BYTES = 128 * 1024 * 1024
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_SCHEMA_BYTES = 2 * 1024 * 1024
MAX_SECRET_BYTES = 16 * 1024
MAX_OBJECTS = 100_000
MAX_SAFE_INTEGER = 9_007_199_254_740_991
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
SHA256_REVISION_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
GIT_COMMIT_RE = re.compile(r"^[a-f0-9]{40}(?:[a-f0-9]{24})?$")
IMAGE_DIGEST_RE = re.compile(r"^[^\s@]+@sha256:[a-f0-9]{64}$")
SAFE_REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+\-]{2,239}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+\-]{0,239}$")
SAFE_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,127}$")
ASSET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,239}$")
UE_NAME_RE = re.compile(r"^[A-Za-z0-9_+\-]{1,240}$")
SOURCE_PACK_RE = re.compile(r"^[A-Za-z0-9_+\-]{1,128}$")
UE_PATH_RE = re.compile(
    r"^(?P<package>/Game(?:/[A-Za-z0-9_+\-]+)+)\.(?P<name>[A-Za-z0-9_+\-]{1,240})$"
)
PROVIDER_SNAPSHOT_RE = re.compile(
    r"^provider-snapshot:[A-Za-z0-9][A-Za-z0-9._:@/+\-]{0,221}$"
)
ASSET_SNAPSHOT_RE = re.compile(
    r"^asset-snapshot-[A-Za-z0-9][A-Za-z0-9._:@/+\-]{0,223}$"
)
FLOATING_TOKENS = frozenset({"dev", "head", "latest", "main", "master", "trunk", "unknown"})
OBJECT_TYPES = frozenset({"Blueprint", "StaticMesh"})
APPROVAL_RE = re.compile(
    r"^(?:APPROVAL|CHANGE|TICKET|VISTA)-[A-Za-z0-9][A-Za-z0-9._-]{2,95}$"
)
SAFE_ADAPTER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$")
CREDENTIAL_GENERATION_RE = re.compile(
    r"^credential-generation:[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,190}$"
)
TARGET_IDENTITY_PATTERNS: Mapping[str, re.Pattern[str]] = {
    "caption": re.compile(
        r"^provider-project:[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,190}$"
    ),
    "postgres": re.compile(
        r"^postgres-deployment:[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,95}"
        r"/schema:[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$"
    ),
    "qdrant": re.compile(
        r"^qdrant-cluster:[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,95}"
        r"/collection:[A-Za-z0-9][A-Za-z0-9._\-]{0,127}$"
    ),
    "embedding": re.compile(
        r"^embedding-project:[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,95}"
        r"/model:[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,127}$"
    ),
}
TARGET_IDENTITY_LENGTHS: Mapping[str, tuple[int, int]] = {
    "caption": (18, 208),
    "postgres": (30, 188),
    "qdrant": (29, 251),
    "embedding": (27, 249),
}
GIT_CLEANUP_TIMEOUT_SECONDS = 2.0
SYSTEM_PYTHON = "/usr/bin/python3"
SUPPORTED_EXECUTION_PYTHON_MINORS = frozenset({(3, 10), (3, 11), (3, 12)})
PARTIAL_CLONE_CONFIG_RE = (
    r"^(extensions\.partialclone|remote\..*\.promisor|"
    r"remote\..*\.partialclonefilter)$"
)
PLAN_KEYS = frozenset(
    {
        "schema",
        "profile",
        "job",
        "executor",
        "adapter",
        "caption",
        "embedding",
        "runtime_images",
        "storage",
        "limits",
        "credential_files_required",
        "credential_bindings",
        "approval_ref_sha256",
    }
)
PREPARATION_RECEIPT_KEYS = frozenset(
    {
        "schema",
        "job_schema",
        "job_revision",
        "job_sha256",
        "bootstrap_receipt_sha256",
        "bootstrap_bundle_revision",
        "object_manifest_sha256",
        "recipe_sha256",
        "asset_snapshot_revision",
        "pending_object_count",
        "pending_objects_sha256",
        "approval_ref_sha256",
        "prepared_at_utc",
        "publication_policy",
        "bundle_complete",
        "execution_started",
        "network_used",
        "unreal_started",
        "caption_provider_called",
        "postgres_contacted",
        "qdrant_contacted",
        "embedding_called",
        "catalog_complete",
        "snapshot_complete",
    }
)
JOB_TOP_KEYS = frozenset(
    {
        "schema",
        "input_contract",
        "recipe_contract",
        "snapshot_target",
        "pending_objects",
        "resource_contract",
        "live_gates",
        "execution_contract",
        "catalog_complete",
        "snapshot_complete",
        "job_revision",
    }
)
SECRET_LABELS = ("caption", "postgres", "qdrant", "embedding")
SOURCE_CLOSURE = (
    "tools/execute_semantic_asset_index_job.py",
    "tools/semantic_index_job_adapters.py",
    "tools/prepare_semantic_asset_index_job.py",
    "tools/build_ue_asset_registry_bootstrap.py",
    "tools/semantic_asset_index_job_schema.json",
    "tools/semantic_asset_index_execution_plan_schema.json",
)
SOURCE_PIN_PATHS: Mapping[str, str] = {
    "executor_source_sha256": "tools/execute_semantic_asset_index_job.py",
    "adapter_source_sha256": "tools/semantic_index_job_adapters.py",
    "preparer_source_sha256": "tools/prepare_semantic_asset_index_job.py",
    "bootstrap_source_sha256": "tools/build_ue_asset_registry_bootstrap.py",
    "job_schema_sha256": "tools/semantic_asset_index_job_schema.json",
    "execution_plan_schema_sha256": "tools/semantic_asset_index_execution_plan_schema.json",
}


class SemanticIndexExecutorError(RuntimeError):
    """Bounded public error that never includes caller data or secret material."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        committed: bool = False,
        durability_uncertain: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.committed = committed
        self.durability_uncertain = durability_uncertain

    def public_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.retryable:
            value["retryable"] = True
        if self.committed:
            value["committed"] = True
        if self.durability_uncertain:
            value["durability_uncertain"] = True
        return value


def fail(
    code: str,
    message: str,
    *,
    retryable: bool = False,
    committed: bool = False,
    durability_uncertain: bool = False,
) -> None:
    raise SemanticIndexExecutorError(
        code,
        message,
        retryable=retryable,
        committed=committed,
        durability_uncertain=durability_uncertain,
    )


def canonical_json(value: Any, *, pretty: bool = True) -> bytes:
    separators = None if pretty else (",", ":")
    text = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2 if pretty else None,
        separators=separators,
    )
    if pretty:
        text += "\n"
    return text.encode("utf-8")


def compact_json(value: Any) -> str:
    return canonical_json(value, pretty=False).decode("utf-8")


def json_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value, pretty=False)).hexdigest()


def _strict_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            fail("SEMANTIC_EXECUTOR_JSON_INVALID", "Input JSON is not canonical")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    fail("SEMANTIC_EXECUTOR_JSON_INVALID", "Input JSON is not canonical")


def strict_json(raw: bytes) -> Any:
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_strict_pairs,
            parse_constant=_reject_constant,
        )
    except SemanticIndexExecutorError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        fail("SEMANTIC_EXECUTOR_JSON_INVALID", "Input must be bounded UTF-8 JSON")


def _exact_object(value: Any, keys: set[str] | frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(keys):
        fail("SEMANTIC_EXECUTOR_SCHEMA_INVALID", "Input does not match the closed contract")
    return value


def _sha256(value: Any) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        fail("SEMANTIC_EXECUTOR_PIN_INVALID", "A required SHA-256 pin is invalid")
    return value


def _sha256_revision(value: Any) -> str:
    if not isinstance(value, str) or SHA256_REVISION_RE.fullmatch(value) is None:
        fail("SEMANTIC_EXECUTOR_PIN_INVALID", "A required revision pin is invalid")
    return value


def _positive_int(value: Any, *, maximum: int = 9_007_199_254_740_991) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        fail("SEMANTIC_EXECUTOR_SCHEMA_INVALID", "A bounded positive integer is required")
    return value


def _bounded_integer(value: Any, *, minimum: int, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        fail("SEMANTIC_EXECUTOR_SCHEMA_INVALID", "A bounded integer is required")
    return value


def _safe_string(value: Any, *, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        fail("SEMANTIC_EXECUTOR_SCHEMA_INVALID", "A bounded safe identifier is required")
    return value


def _reject_floating_tokens(value: str) -> str:
    tokens = set(filter(None, re.split(r"[:/@._+\-]+", value.lower())))
    if tokens.intersection(FLOATING_TOKENS):
        fail("SEMANTIC_EXECUTOR_PIN_INVALID", "A revision uses a floating label")
    return value


def _pinned_revision(value: Any) -> str:
    return _reject_floating_tokens(_safe_string(value, pattern=SAFE_REVISION_RE))


CAPTION_CONTRACT_KEYS = frozenset(
    {
        "provider_id", "model_id", "model_revision", "prompt_revision",
        "output_schema_revision", "render_recipe_revision", "views_per_asset",
        "image_width_px", "image_height_px", "max_output_tokens_per_asset",
        "temperature_milli",
    }
)
EMBEDDING_CONTRACT_KEYS = frozenset(
    {
        "recipe_revision", "dense_model_id", "dense_model_revision", "dense_size",
        "sparse_model_id", "sparse_model_revision", "batch_size",
    }
)
STORAGE_CONTRACT_KEYS = frozenset(
    {
        "postgres_schema_revision", "qdrant_collection",
        "qdrant_dense_vector_name", "qdrant_sparse_vector_name",
    }
)


def _validate_caption_contract(value: Any) -> dict[str, Any]:
    caption = _exact_object(value, CAPTION_CONTRACT_KEYS)
    _safe_string(caption["provider_id"], pattern=SAFE_ID_RE)
    _safe_string(caption["model_id"], pattern=SAFE_ID_RE)
    model_revision = _pinned_revision(caption["model_revision"])
    if not (
        SHA256_REVISION_RE.fullmatch(model_revision)
        or PROVIDER_SNAPSHOT_RE.fullmatch(model_revision)
    ):
        fail("SEMANTIC_EXECUTOR_SCHEMA_INVALID", "Caption model revision is not immutable")
    for key in ("prompt_revision", "output_schema_revision", "render_recipe_revision"):
        _sha256_revision(caption[key])
    _bounded_integer(caption["views_per_asset"], minimum=1, maximum=32)
    _bounded_integer(caption["image_width_px"], minimum=64, maximum=8192)
    _bounded_integer(caption["image_height_px"], minimum=64, maximum=8192)
    _bounded_integer(caption["max_output_tokens_per_asset"], minimum=1, maximum=65_536)
    _bounded_integer(caption["temperature_milli"], minimum=0, maximum=2_000)
    return caption


def _validate_embedding_contract(value: Any) -> dict[str, Any]:
    embedding = _exact_object(value, EMBEDDING_CONTRACT_KEYS)
    _pinned_revision(embedding["recipe_revision"])
    _safe_string(embedding["dense_model_id"], pattern=SAFE_ID_RE)
    _sha256_revision(embedding["dense_model_revision"])
    _bounded_integer(embedding["dense_size"], minimum=1, maximum=65_536)
    _safe_string(embedding["sparse_model_id"], pattern=SAFE_ID_RE)
    _sha256_revision(embedding["sparse_model_revision"])
    _bounded_integer(embedding["batch_size"], minimum=1, maximum=4096)
    return embedding


def _validate_storage_contract(value: Any) -> dict[str, Any]:
    storage = _exact_object(value, STORAGE_CONTRACT_KEYS)
    if type(storage["postgres_schema_revision"]) is not int or storage["postgres_schema_revision"] != 2:
        fail("SEMANTIC_EXECUTOR_SCHEMA_INVALID", "PostgreSQL schema revision is unsupported")
    collection = _safe_string(storage["qdrant_collection"], pattern=SAFE_SLUG_RE)
    _reject_floating_tokens(collection)
    if collection.lower() in {"asset", "assets", "default", "latest", "main"}:
        fail("SEMANTIC_EXECUTOR_SCHEMA_INVALID", "Qdrant collection is not versioned")
    _safe_string(storage["qdrant_dense_vector_name"], pattern=SAFE_SLUG_RE)
    _safe_string(storage["qdrant_sparse_vector_name"], pattern=SAFE_SLUG_RE)
    if storage["qdrant_dense_vector_name"] == storage["qdrant_sparse_vector_name"]:
        fail("SEMANTIC_EXECUTOR_SCHEMA_INVALID", "Dense and sparse vector names must differ")
    return storage


def _validate_recipe(value: Any) -> dict[str, Any]:
    """Validate a sealed recipe without importing the preparation program."""

    recipe = _exact_object(value, {"schema", "caption", "embedding", "storage", "limits"})
    if recipe["schema"] != RECIPE_SCHEMA:
        fail("SEMANTIC_EXECUTOR_JOB_INVALID", "Job recipe schema is unsupported")
    _validate_caption_contract(recipe["caption"])
    _validate_embedding_contract(recipe["embedding"])
    _validate_storage_contract(recipe["storage"])

    limits = _exact_object(
        recipe["limits"],
        {
            "max_assets", "max_rendered_views", "max_render_pixels",
            "max_total_caption_output_tokens", "max_catalog_record_bytes",
            "max_total_catalog_bytes", "max_postgres_rows", "max_qdrant_points",
            "max_dense_vector_payload_bytes_estimate",
        },
    )
    bounds = {
        "max_assets": (1, MAX_OBJECTS),
        "max_rendered_views": (1, MAX_SAFE_INTEGER),
        "max_render_pixels": (1, MAX_SAFE_INTEGER),
        "max_total_caption_output_tokens": (1, MAX_SAFE_INTEGER),
        "max_catalog_record_bytes": (1024, 16 * 1024 * 1024),
        "max_total_catalog_bytes": (1024, MAX_SAFE_INTEGER),
        "max_postgres_rows": (1, MAX_OBJECTS),
        "max_qdrant_points": (1, MAX_OBJECTS),
        "max_dense_vector_payload_bytes_estimate": (4, MAX_SAFE_INTEGER),
    }
    for key, (minimum, maximum) in bounds.items():
        _bounded_integer(limits[key], minimum=minimum, maximum=maximum)
    return recipe


def _rfc3339(value: Any) -> dt.datetime:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        fail("SEMANTIC_EXECUTOR_SCHEMA_INVALID", "A bounded timestamp is required")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        fail("SEMANTIC_EXECUTOR_SCHEMA_INVALID", "A valid timestamp is required")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        fail("SEMANTIC_EXECUTOR_SCHEMA_INVALID", "Timestamp must include a timezone")
    return parsed


def _stable_metadata(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _normalized_absolute(path: Path) -> Path:
    if not path.is_absolute() or path != Path(os.path.abspath(path)):
        fail("SEMANTIC_EXECUTOR_PATH_UNSAFE", "Every input path must be absolute and normalized")
    return path


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            metadata = os.lstat(current)
        except OSError:
            fail("SEMANTIC_EXECUTOR_PATH_UNSAFE", "An input path is unavailable")
        if stat.S_ISLNK(metadata.st_mode):
            fail("SEMANTIC_EXECUTOR_PATH_UNSAFE", "Symbolic-link path traversal is prohibited")


def secure_read(
    path: Path,
    *,
    maximum: int,
    private: bool,
    allow_root_owner: bool = False,
) -> bytes:
    path = _normalized_absolute(path)
    _reject_symlink_components(path)
    try:
        before = os.lstat(path)
    except OSError:
        fail("SEMANTIC_EXECUTOR_INPUT_UNAVAILABLE", "A required input is unavailable")
    allowed_owners = {os.geteuid(), 0} if allow_root_owner else {os.geteuid()}
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_uid not in allowed_owners
        or not 1 <= before.st_size <= maximum
        or (private and stat.S_IMODE(before.st_mode) != 0o600)
    ):
        fail("SEMANTIC_EXECUTOR_INPUT_UNSAFE", "A required input has unsafe metadata")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        fail("SEMANTIC_EXECUTOR_INPUT_UNAVAILABLE", "A required input is unavailable")
    try:
        opened = os.fstat(descriptor)
        if _stable_metadata(opened) != _stable_metadata(before):
            fail("SEMANTIC_EXECUTOR_INPUT_CHANGED", "A required input changed during validation")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            len(raw) != opened.st_size
            or len(raw) > maximum
            or _stable_metadata(after) != _stable_metadata(opened)
        ):
            fail("SEMANTIC_EXECUTOR_INPUT_CHANGED", "A required input changed during validation")
        return raw
    finally:
        os.close(descriptor)


def read_secret_file_once(path: Path) -> bytes:
    """Capture one validated secret exactly once without hashing or persisting it."""

    raw = secure_read(
        path,
        maximum=MAX_SECRET_BYTES,
        private=True,
        allow_root_owner=True,
    )
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        fail("SEMANTIC_EXECUTOR_SECRET_INVALID", "A credential file is invalid")
    if not re.fullmatch(r"[^\r\n\x00-\x1f\x7f]+(?:\r?\n)?", decoded):
        fail("SEMANTIC_EXECUTOR_SECRET_INVALID", "A credential file is invalid")
    return decoded.rstrip("\r\n").encode("utf-8")


def _fixed_git_environment() -> dict[str, str]:
    """Return a minimal environment for the read-only Git attestation client."""

    return {
        "PATH": "/usr/bin:/bin",
        "HOME": "/nonexistent",
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_PROTOCOL_FROM_USER": "0",
        "GIT_ASKPASS": "/bin/false",
        "SSH_ASKPASS": "/bin/false",
    }


def _wait_for_process_until(
    process: subprocess.Popen[bytes],
    *,
    cleanup_deadline_monotonic: float,
) -> bool:
    """Reap the direct child without ever waiting past one cleanup deadline."""

    remaining = cleanup_deadline_monotonic - time.monotonic()
    if remaining <= 0:
        return process.poll() is not None
    try:
        process.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        return process.poll() is not None
    return True


def _terminate_process_group(
    process: subprocess.Popen[bytes],
    *,
    cleanup_deadline_monotonic: float | None = None,
) -> None:
    """Kill the private process group and boundedly reap its direct Git child."""

    cleanup_deadline = (
        time.monotonic() + GIT_CLEANUP_TIMEOUT_SECONDS
        if cleanup_deadline_monotonic is None
        else cleanup_deadline_monotonic
    )
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    if _wait_for_process_until(
        process,
        cleanup_deadline_monotonic=cleanup_deadline,
    ):
        return
    try:
        process.kill()
    except ProcessLookupError:
        pass
    if not _wait_for_process_until(
        process,
        cleanup_deadline_monotonic=cleanup_deadline,
    ):
        fail(
            "SEMANTIC_EXECUTOR_GIT_INVALID",
            "Git attestation child did not exit within its cleanup deadline",
        )


def _run_fixed_git(
    repo_root: Path,
    arguments: Sequence[str],
    *,
    maximum_stdout: int,
    timeout_seconds: float = 5.0,
    accepted_return_codes: frozenset[int] = frozenset({0}),
) -> bytes:
    """Run one bounded, read-only Git query without a shell or inherited config."""

    if (
        maximum_stdout < 1
        or timeout_seconds <= 0
        or not accepted_return_codes
        or any(type(code) is not int or code < 0 or code > 255 for code in accepted_return_codes)
    ):
        fail("SEMANTIC_EXECUTOR_GIT_INVALID", "Git attestation bounds are invalid")
    allowed = False
    if list(arguments) in (
        ["rev-parse", "--verify", "HEAD^{commit}"],
        ["rev-parse", "--show-object-format"],
        ["config", "--local", "--no-includes", "--get-regexp", PARTIAL_CLONE_CONFIG_RE],
    ):
        allowed = True
    elif len(arguments) == 3 and arguments[0] == "cat-file" and arguments[1] in {"-s", "blob"}:
        object_spec = arguments[2]
        if isinstance(object_spec, str) and ":" in object_spec:
            commit, relative_path = object_spec.split(":", 1)
            allowed = GIT_COMMIT_RE.fullmatch(commit) is not None and relative_path in SOURCE_CLOSURE
    if not allowed:
        fail("SEMANTIC_EXECUTOR_GIT_INVALID", "Git attestation query is not allowlisted")
    command = [
        "/usr/bin/git",
        "--no-optional-locks",
        "-C",
        str(repo_root),
        *arguments,
    ]
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd="/",
            env=_fixed_git_environment(),
            shell=False,
            close_fds=True,
            start_new_session=True,
        )
    except OSError:
        fail("SEMANTIC_EXECUTOR_GIT_INVALID", "Fixed Git attestation client is unavailable")
    assert process.stdout is not None and process.stderr is not None
    cleanup_deadline_monotonic: float | None = None

    def terminate_git() -> None:
        nonlocal cleanup_deadline_monotonic
        if cleanup_deadline_monotonic is None:
            cleanup_deadline_monotonic = (
                time.monotonic() + GIT_CLEANUP_TIMEOUT_SECONDS
            )
        _terminate_process_group(
            process,
            cleanup_deadline_monotonic=cleanup_deadline_monotonic,
        )

    stdout_fd = process.stdout.fileno()
    stderr_fd = process.stderr.fileno()
    streams = {stdout_fd: bytearray(), stderr_fd: bytearray()}
    limits = {stdout_fd: maximum_stdout, stderr_fd: 4096}
    selector = selectors.DefaultSelector()
    try:
        for stream in (process.stdout, process.stderr):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout_seconds
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                terminate_git()
                fail("SEMANTIC_EXECUTOR_GIT_INVALID", "Git attestation timed out")
            events = selector.select(min(remaining, 0.25))
            if not events and process.poll() is not None:
                events = [(key, selectors.EVENT_READ) for key in selector.get_map().values()]
            for key, _mask in events:
                descriptor = key.fileobj.fileno()
                try:
                    remaining_output = limits[descriptor] - len(streams[descriptor]) + 1
                    chunk = os.read(descriptor, min(65_536, remaining_output))
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                streams[descriptor].extend(chunk)
                if len(streams[descriptor]) > limits[descriptor]:
                    terminate_git()
                    fail("SEMANTIC_EXECUTOR_GIT_INVALID", "Git attestation output exceeded its bound")
        remaining = max(0.001, deadline - time.monotonic())
        try:
            return_code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            terminate_git()
            fail("SEMANTIC_EXECUTOR_GIT_INVALID", "Git attestation timed out")
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()
        if process.poll() is None:
            terminate_git()
    if return_code not in accepted_return_codes:
        terminate_git()
        fail("SEMANTIC_EXECUTOR_GIT_INVALID", "Git attestation query failed")
    return bytes(streams[stdout_fd])


def _reject_partial_or_promisor_repository(repo_root: Path) -> None:
    """Reject repositories whose missing objects could trigger lazy retrieval."""

    partial_config = _run_fixed_git(
        repo_root,
        ["config", "--local", "--no-includes", "--get-regexp", PARTIAL_CLONE_CONFIG_RE],
        maximum_stdout=4096,
        accepted_return_codes=frozenset({0, 1}),
    )
    if partial_config.strip():
        fail(
            "SEMANTIC_EXECUTOR_GIT_PARTIAL_CLONE_UNSUPPORTED",
            "Git attestation requires a complete local non-promisor repository",
        )


def hash_source(path: Path, *, maximum: int = MAX_SCHEMA_BYTES) -> str:
    return hashlib.sha256(
        secure_read(path, maximum=maximum, private=False, allow_root_owner=False)
    ).hexdigest()


def _verify_git_source_closure(
    repo_root: Path,
    executor_pins: Mapping[str, Any],
) -> tuple[str, Mapping[str, bytes]]:
    """Bind reviewed dependency bytes to the pinned HEAD before any local import.

    The entrypoint is already executing and therefore cannot establish its own
    pre-execution trust.  Its self comparison is only an operational drift
    check; a release launcher must attest the entrypoint before invoking it.
    """

    repo_root = _normalized_absolute(repo_root)
    _reject_symlink_components(repo_root)
    if tuple(SOURCE_PIN_PATHS.values()) != SOURCE_CLOSURE:
        fail("SEMANTIC_EXECUTOR_SOURCE_PIN_MISMATCH", "Source closure and pin contract differ")
    actual_entrypoint = Path(os.path.abspath(__file__))
    if actual_entrypoint != repo_root / SOURCE_PIN_PATHS["executor_source_sha256"]:
        fail("SEMANTIC_EXECUTOR_SOURCE_PIN_MISMATCH", "Executor is outside the pinned repository")
    _reject_partial_or_promisor_repository(repo_root)
    commit_raw = _run_fixed_git(
        repo_root,
        ["rev-parse", "--verify", "HEAD^{commit}"],
        maximum_stdout=128,
    )
    try:
        commit = commit_raw.decode("ascii").strip()
    except UnicodeDecodeError:
        fail("SEMANTIC_EXECUTOR_GIT_INVALID", "Git HEAD is invalid")
    if GIT_COMMIT_RE.fullmatch(commit) is None or commit != executor_pins["git_commit"]:
        fail("SEMANTIC_EXECUTOR_GIT_PIN_MISMATCH", "Executor Git HEAD differs from the reviewed commit")
    object_format_raw = _run_fixed_git(
        repo_root,
        ["rev-parse", "--show-object-format"],
        maximum_stdout=32,
    )
    try:
        object_format = object_format_raw.decode("ascii").strip()
    except UnicodeDecodeError:
        fail("SEMANTIC_EXECUTOR_GIT_INVALID", "Git object format is invalid")
    if object_format not in {"sha1", "sha256"} or object_format != executor_pins["git_object_format"]:
        fail("SEMANTIC_EXECUTOR_GIT_PIN_MISMATCH", "Git object format differs from the reviewed plan")
    if len(commit) != (40 if object_format == "sha1" else 64):
        fail("SEMANTIC_EXECUTOR_GIT_INVALID", "Git commit length does not match its object format")

    verified: dict[str, bytes] = {}
    for pin_key, relative_path in SOURCE_PIN_PATHS.items():
        worktree_bytes = secure_read(
            repo_root / relative_path,
            maximum=MAX_SCHEMA_BYTES,
            private=False,
            allow_root_owner=False,
        )
        object_spec = f"{commit}:{relative_path}"
        size_raw = _run_fixed_git(
            repo_root,
            ["cat-file", "-s", object_spec],
            maximum_stdout=32,
        )
        try:
            object_size = int(size_raw.decode("ascii").strip())
        except (UnicodeDecodeError, ValueError):
            fail("SEMANTIC_EXECUTOR_GIT_INVALID", "A source object size is invalid")
        if not 1 <= object_size <= MAX_SCHEMA_BYTES or object_size != len(worktree_bytes):
            fail("SEMANTIC_EXECUTOR_SOURCE_PIN_MISMATCH", "A dependency differs from pinned HEAD")
        head_bytes = _run_fixed_git(
            repo_root,
            ["cat-file", "blob", object_spec],
            maximum_stdout=object_size,
        )
        if head_bytes != worktree_bytes:
            fail("SEMANTIC_EXECUTOR_SOURCE_PIN_MISMATCH", "A dependency differs from pinned HEAD")
        observed_sha256 = hashlib.sha256(worktree_bytes).hexdigest()
        if observed_sha256 != executor_pins[pin_key]:
            fail("SEMANTIC_EXECUTOR_SOURCE_PIN_MISMATCH", "A dependency differs from its reviewed SHA-256")
        verified[relative_path] = worktree_bytes
    return commit, verified


def _approval_hash(value: str) -> str:
    if APPROVAL_RE.fullmatch(value) is None:
        fail("SEMANTIC_EXECUTOR_APPROVAL_INVALID", "Execution requires a non-secret approval reference")
    return hashlib.sha256(
        b"simworld-semantic-index-execution-approval/v1\0" + value.encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class ValidatedBundle:
    job: Mapping[str, Any]
    receipt: Mapping[str, Any]
    plan: Mapping[str, Any]
    job_sha256: str
    receipt_sha256: str
    plan_sha256: str
    observed_git_commit: str
    repo_root: Path
    adapter_source_bytes: bytes = field(repr=False)


_MAX_REGISTERED_BUNDLES = 4096
_VALIDATED_BUNDLE_REGISTRY: dict[int, ValidatedBundle] = {}


def _register_validated_bundle(bundle: ValidatedBundle) -> ValidatedBundle:
    """Record the exact object returned by this process's validation pass."""

    if len(_VALIDATED_BUNDLE_REGISTRY) >= _MAX_REGISTERED_BUNDLES:
        fail(
            "SEMANTIC_EXECUTOR_VALIDATION_CAPACITY_EXCEEDED",
            "Validated bundle capacity is exhausted; launch a fresh executor process",
        )
    _VALIDATED_BUNDLE_REGISTRY[id(bundle)] = bundle
    return bundle


def _require_registered_bundle(bundle: ValidatedBundle) -> None:
    if (
        type(bundle) is not ValidatedBundle
        or _VALIDATED_BUNDLE_REGISTRY.get(id(bundle)) is not bundle
    ):
        fail(
            "SEMANTIC_EXECUTOR_BUNDLE_NOT_VALIDATED",
            "Execution requires the opaque result of this process's validation pass",
        )


@dataclass(frozen=True, slots=True)
class SecretPathArguments:
    caption: str | None
    postgres: str | None
    qdrant: str | None
    embedding: str | None


@dataclass(frozen=True, slots=True)
class AdapterRuntime:
    module: ModuleType = field(repr=False)
    instance: Any = field(repr=False)
    phase_request_type: type[Any] = field(repr=False)
    phase_result_type: type[Any] = field(repr=False)
    secret_snapshot_type: type[Any] = field(repr=False)

    @property
    def adapter_id(self) -> str:
        return str(self.instance.adapter_id)

    @property
    def adapter_contract_revision(self) -> str:
        return str(self.instance.adapter_contract_revision)

    @property
    def production_capable(self) -> bool:
        return bool(self.instance.production_capable)

    def run_phase(self, request: Any) -> Any:
        return self.instance.run_phase(request)


def _validate_job(job_raw: bytes) -> dict[str, Any]:
    job = _exact_object(strict_json(job_raw), JOB_TOP_KEYS)
    if canonical_json(job) != job_raw or job["schema"] != JOB_SCHEMA:
        fail("SEMANTIC_EXECUTOR_JOB_INVALID", "Job bytes are not the canonical supported contract")
    revision_basis = dict(job)
    revision_basis.pop("job_revision")
    if job["job_revision"] != "sha256:" + json_sha256(revision_basis):
        fail("SEMANTIC_EXECUTOR_JOB_INVALID", "Job revision does not match its canonical content")

    input_contract = _exact_object(
        job["input_contract"],
        {
            "bootstrap_receipt_schema", "bootstrap_receipt_sha256",
            "bootstrap_bundle_revision", "object_manifest_schema",
            "object_manifest_revision", "object_manifest_sha256", "object_count",
            "project", "content", "archive_receipt_sha256", "registry_audit_sha256",
            "manifest_kind", "bootstrap_bundle_complete", "candidate_manifest_reviewed",
            "catalog_complete", "snapshot_complete",
        },
    )
    if (
        input_contract["bootstrap_receipt_schema"] != BOOTSTRAP_RECEIPT_SCHEMA
        or input_contract["object_manifest_schema"] != OBJECT_MANIFEST_SCHEMA
        or input_contract["manifest_kind"] != "asset_registry_object_candidates_not_catalog"
        or input_contract["bootstrap_bundle_complete"] is not True
        or input_contract["candidate_manifest_reviewed"] is not False
        or input_contract["catalog_complete"] is not False
        or input_contract["snapshot_complete"] is not False
    ):
        fail("SEMANTIC_EXECUTOR_JOB_INVALID", "Job input state is not executable")
    for key in ("bootstrap_receipt_sha256", "object_manifest_sha256", "archive_receipt_sha256", "registry_audit_sha256"):
        _sha256(input_contract[key])
    for key in ("bootstrap_bundle_revision", "object_manifest_revision"):
        _sha256_revision(input_contract[key])
    count = _positive_int(input_contract["object_count"], maximum=MAX_OBJECTS)
    project = _exact_object(input_contract["project"], {"name", "revision", "engine_version"})
    content = _exact_object(input_contract["content"], {"mount_point", "revision"})
    if (
        not isinstance(project["name"], str)
        or SAFE_SLUG_RE.fullmatch(project["name"]) is None
        or not isinstance(project["revision"], str)
        or not isinstance(project["engine_version"], str)
        or not project["engine_version"].strip()
        or len(project["engine_version"]) > 160
        or any(ord(character) < 32 for character in project["engine_version"])
        or content["mount_point"] != "/Game"
    ):
        fail("SEMANTIC_EXECUTOR_JOB_INVALID", "Job project binding is invalid")
    try:
        _pinned_revision(project["revision"])
    except SemanticIndexExecutorError:
        fail("SEMANTIC_EXECUTOR_JOB_INVALID", "Job project revision is invalid")
    _sha256_revision(content["revision"])

    recipe_contract = _exact_object(
        job["recipe_contract"],
        {"recipe_schema", "recipe_sha256", "caption_recipe_sha256", "caption", "embedding_recipe_sha256", "embedding", "storage"},
    )
    for key in ("recipe_sha256", "caption_recipe_sha256", "embedding_recipe_sha256"):
        _sha256(recipe_contract[key])
    if (
        recipe_contract["recipe_schema"] != RECIPE_SCHEMA
        or recipe_contract["caption_recipe_sha256"] != json_sha256(recipe_contract["caption"])
        or recipe_contract["embedding_recipe_sha256"] != json_sha256(recipe_contract["embedding"])
    ):
        fail("SEMANTIC_EXECUTOR_JOB_INVALID", "Job recipe binding is invalid")

    resource = _exact_object(job["resource_contract"], {"limits", "estimates", "cost_authorized"})
    if resource["cost_authorized"] is not False:
        fail("SEMANTIC_EXECUTOR_JOB_INVALID", "Prepared job cannot carry implicit cost approval")
    try:
        _validate_recipe(
            {
                "schema": RECIPE_SCHEMA,
                "caption": recipe_contract["caption"],
                "embedding": recipe_contract["embedding"],
                "storage": recipe_contract["storage"],
                "limits": resource["limits"],
            }
        )
    except SemanticIndexExecutorError:
        fail("SEMANTIC_EXECUTOR_JOB_INVALID", "Job recipe contract is invalid")

    snapshot = _exact_object(
        job["snapshot_target"],
        {"asset_snapshot_revision", "postgres", "qdrant", "row_point_parity_required", "live_audit_receipt_required", "snapshot_complete"},
    )
    postgres = _exact_object(snapshot["postgres"], {"schema_revision", "required_asset_snapshot_revision", "planned_candidate_rows", "observed_live"})
    qdrant = _exact_object(snapshot["qdrant"], {"collection", "dense_vector_name", "sparse_vector_name", "required_payload_asset_snapshot_revision", "planned_candidate_points", "observed_live"})
    snapshot_revision = snapshot["asset_snapshot_revision"]
    if (
        not isinstance(snapshot_revision, str)
        or ASSET_SNAPSHOT_RE.fullmatch(snapshot_revision) is None
        or snapshot["row_point_parity_required"] is not True
        or snapshot["live_audit_receipt_required"] is not True
        or snapshot["snapshot_complete"] is not False
        or type(postgres["schema_revision"]) is not int
        or postgres["schema_revision"] != 2
        or postgres["required_asset_snapshot_revision"] != snapshot_revision
        or type(postgres["planned_candidate_rows"]) is not int
        or postgres["planned_candidate_rows"] != count
        or postgres["observed_live"] is not False
        or qdrant["required_payload_asset_snapshot_revision"] != snapshot_revision
        or type(qdrant["planned_candidate_points"]) is not int
        or qdrant["planned_candidate_points"] != count
        or qdrant["observed_live"] is not False
        or qdrant["collection"] != recipe_contract["storage"]["qdrant_collection"]
        or qdrant["dense_vector_name"] != recipe_contract["storage"]["qdrant_dense_vector_name"]
        or qdrant["sparse_vector_name"] != recipe_contract["storage"]["qdrant_sparse_vector_name"]
    ):
        fail("SEMANTIC_EXECUTOR_JOB_INVALID", "Job snapshot target is invalid")

    pending = _exact_object(job["pending_objects"], {"count", "selection", "assets_sha256", "assets"})
    assets = pending["assets"]
    if (
        type(pending["count"]) is not int
        or pending["count"] != count
        or pending["selection"] != "all_unindexed_objects_in_pinned_bootstrap_manifest"
        or not isinstance(assets, list)
        or len(assets) != count
        or pending["assets_sha256"] != json_sha256(assets)
    ):
        fail("SEMANTIC_EXECUTOR_JOB_INVALID", "Job pending set is invalid")
    asset_ids: set[str] = set()
    ue_paths: set[str] = set()
    previous_key: tuple[str, str, str] | None = None
    for asset in assets:
        asset = _exact_object(asset, {"asset_id", "ue_name", "ue_path", "asset_type", "source_pack", "content_revision", "status"})
        if (
            not isinstance(asset["asset_id"], str)
            or ASSET_ID_RE.fullmatch(asset["asset_id"]) is None
            or not isinstance(asset["ue_name"], str)
            or UE_NAME_RE.fullmatch(asset["ue_name"]) is None
            or not isinstance(asset["ue_path"], str)
            or UE_PATH_RE.fullmatch(asset["ue_path"]) is None
            or asset["asset_type"] not in OBJECT_TYPES
            or not isinstance(asset["source_pack"], str)
            or SOURCE_PACK_RE.fullmatch(asset["source_pack"]) is None
            or asset["content_revision"] != content["revision"]
            or asset["status"] != "pending_live_semantic_index"
        ):
            fail("SEMANTIC_EXECUTOR_JOB_INVALID", "A pending object is invalid")
        order_key = (asset["source_pack"], asset["ue_path"], asset["asset_id"])
        if previous_key is not None and order_key < previous_key:
            fail("SEMANTIC_EXECUTOR_JOB_INVALID", "Pending objects are not deterministically ordered")
        previous_key = order_key
        asset_ids.add(asset["asset_id"])
        ue_paths.add(asset["ue_path"])
    if len(asset_ids) != count or len(ue_paths) != count:
        fail("SEMANTIC_EXECUTOR_JOB_INVALID", "Pending object identities are not unique")

    caption = recipe_contract["caption"]
    embedding = recipe_contract["embedding"]
    limits = resource["limits"]
    expected_estimates = {
        "pending_assets": count,
        "rendered_views_upper_bound": count * caption["views_per_asset"],
        "render_pixels_upper_bound": count * caption["views_per_asset"] * caption["image_width_px"] * caption["image_height_px"],
        "caption_output_tokens_upper_bound": count * caption["max_output_tokens_per_asset"],
        "catalog_json_bytes_upper_bound": count * limits["max_catalog_record_bytes"],
        "embedding_batches_upper_bound": math.ceil(count / embedding["batch_size"]),
        "postgres_rows_upper_bound": count,
        "qdrant_points_upper_bound": count,
        "dense_vector_payload_bytes_estimate": count * embedding["dense_size"] * 4,
        "dense_vector_estimate_excludes": ["qdrant_payload", "sparse_vectors", "indexes", "replication", "storage_engine_overhead"],
    }
    if resource["estimates"] != expected_estimates:
        fail("SEMANTIC_EXECUTOR_JOB_INVALID", "Job resource estimates are invalid")
    if any(
        type(resource["estimates"][key]) is not int
        for key in set(expected_estimates) - {"dense_vector_estimate_excludes"}
    ):
        fail("SEMANTIC_EXECUTOR_JOB_INVALID", "Job resource estimate types are invalid")
    estimate_limits = {
        "pending_assets": "max_assets",
        "rendered_views_upper_bound": "max_rendered_views",
        "render_pixels_upper_bound": "max_render_pixels",
        "caption_output_tokens_upper_bound": "max_total_caption_output_tokens",
        "catalog_json_bytes_upper_bound": "max_total_catalog_bytes",
        "postgres_rows_upper_bound": "max_postgres_rows",
        "qdrant_points_upper_bound": "max_qdrant_points",
        "dense_vector_payload_bytes_estimate": "max_dense_vector_payload_bytes_estimate",
    }
    if any(expected_estimates[estimate] > limits[limit] for estimate, limit in estimate_limits.items()):
        fail("SEMANTIC_EXECUTOR_JOB_INVALID", "Job exceeds a sealed resource ceiling")

    expected_gates = [
        {
            "gate": "object_candidate_review",
            "category": "data",
            "status": "pending",
            "required_evidence": "approved bounded asset slice and rejection ledger",
        },
        {
            "gate": "ue_load_spawn_render",
            "category": "state_change",
            "status": "pending",
            "required_evidence": "loadability, class, bounds, collision, materials, render hashes",
        },
        {
            "gate": "caption_provider",
            "category": "cost",
            "status": "pending",
            "required_evidence": "approved provider/model/budget and schema-constrained outputs",
        },
        {
            "gate": "embedding_artifacts",
            "category": "data_admin_cost",
            "status": "pending",
            "required_evidence": "offline model artifact manifests and dimension probe",
        },
        {
            "gate": "postgres_migration",
            "category": "admin_state_change",
            "status": "pending",
            "required_evidence": "schema revision, revision-bound rows, backup and restore plan",
        },
        {
            "gate": "qdrant_build",
            "category": "admin_state_change_cost",
            "status": "pending",
            "required_evidence": "revision-bound points, named vectors and auth probes",
        },
        {
            "gate": "snapshot_live_audit",
            "category": "data_admin",
            "status": "pending",
            "required_evidence": "fresh digest-bound snapshot live-audit receipt with row/point parity",
        },
    ]
    gates = job["live_gates"]
    if (
        not isinstance(gates, list) or gates != expected_gates
    ):
        fail("SEMANTIC_EXECUTOR_JOB_INVALID", "Job live-gate contract is invalid")

    execution = _exact_object(
        job["execution_contract"],
        {
            "prepared_only", "default_behavior", "apply_effect",
            "explicit_operator_execution_required", "legacy_full_asset_index_runner_authorized",
            "reviewed_adapter_required_for_legacy_runner", "prohibited_production_default_args",
            "network_allowed_during_preparation", "unreal_allowed_during_preparation",
            "caption_provider_allowed_during_preparation", "postgres_allowed_during_preparation",
            "qdrant_allowed_during_preparation", "embedding_allowed_during_preparation",
        },
    )
    if (
        execution["prepared_only"] is not True
        or execution["default_behavior"] != "dry_run"
        or execution["apply_effect"] != "atomic_private_job_bundle_only"
        or execution["explicit_operator_execution_required"] is not True
        or execution["legacy_full_asset_index_runner_authorized"] is not False
        or execution["reviewed_adapter_required_for_legacy_runner"] is not True
        or execution["prohibited_production_default_args"] != ["--dangerously-bypass-approvals-and-sandbox"]
        or any(execution[key] is not False for key in (
            "network_allowed_during_preparation", "unreal_allowed_during_preparation",
            "caption_provider_allowed_during_preparation", "postgres_allowed_during_preparation",
            "qdrant_allowed_during_preparation", "embedding_allowed_during_preparation",
        ))
        or job["catalog_complete"] is not False
        or job["snapshot_complete"] is not False
    ):
        fail("SEMANTIC_EXECUTOR_JOB_INVALID", "Job execution boundary is invalid")
    return job


def _validate_preparation_receipt(receipt_raw: bytes, job: Mapping[str, Any], job_sha256: str) -> dict[str, Any]:
    receipt = _exact_object(strict_json(receipt_raw), PREPARATION_RECEIPT_KEYS)
    if canonical_json(receipt) != receipt_raw:
        fail("SEMANTIC_EXECUTOR_RECEIPT_INVALID", "Preparation receipt is not canonical")
    input_contract = job["input_contract"]
    pending = job["pending_objects"]
    if (
        receipt["schema"] != PREPARATION_RECEIPT_SCHEMA
        or receipt["job_schema"] != JOB_SCHEMA
        or receipt["job_revision"] != job["job_revision"]
        or receipt["job_sha256"] != job_sha256
        or receipt["bootstrap_receipt_sha256"] != input_contract["bootstrap_receipt_sha256"]
        or receipt["bootstrap_bundle_revision"] != input_contract["bootstrap_bundle_revision"]
        or receipt["object_manifest_sha256"] != input_contract["object_manifest_sha256"]
        or receipt["recipe_sha256"] != job["recipe_contract"]["recipe_sha256"]
        or receipt["asset_snapshot_revision"] != job["snapshot_target"]["asset_snapshot_revision"]
        or type(receipt["pending_object_count"]) is not int
        or receipt["pending_object_count"] != pending["count"]
        or receipt["pending_objects_sha256"] != pending["assets_sha256"]
        or receipt["publication_policy"] != "atomic_non_overwriting_private"
        or receipt["bundle_complete"] is not True
        or any(receipt[key] is not False for key in (
            "execution_started", "network_used", "unreal_started", "caption_provider_called",
            "postgres_contacted", "qdrant_contacted", "embedding_called",
            "catalog_complete", "snapshot_complete",
        ))
    ):
        fail("SEMANTIC_EXECUTOR_RECEIPT_INVALID", "Preparation receipt does not seal this pending job")
    for key in ("job_sha256", "bootstrap_receipt_sha256", "object_manifest_sha256", "recipe_sha256", "pending_objects_sha256", "approval_ref_sha256"):
        _sha256(receipt[key])
    for key in ("job_revision", "bootstrap_bundle_revision"):
        _sha256_revision(receipt[key])
    _rfc3339(receipt["prepared_at_utc"])
    return receipt


def _validate_plan_shape(plan_raw: bytes) -> dict[str, Any]:
    plan = _exact_object(strict_json(plan_raw), PLAN_KEYS)
    if canonical_json(plan) != plan_raw or plan["schema"] != EXECUTION_PLAN_SCHEMA:
        fail("SEMANTIC_EXECUTOR_PLAN_INVALID", "Execution plan is not the canonical supported contract")
    if plan["profile"] not in {"offline_fixture", "production"}:
        fail("SEMANTIC_EXECUTOR_PLAN_INVALID", "Execution profile is unsupported")

    plan_job = _exact_object(
        plan["job"],
        {
            "job_schema", "job_sha256", "job_revision", "preparation_receipt_sha256",
            "bootstrap_receipt_sha256", "bootstrap_bundle_revision",
            "object_manifest_sha256", "recipe_sha256", "project_revision",
            "content_revision", "pending_objects_sha256", "pending_object_count",
            "asset_snapshot_revision",
        },
    )
    try:
        if type(plan_job["job_schema"]) is not str or plan_job["job_schema"] != JOB_SCHEMA:
            fail("SEMANTIC_EXECUTOR_SCHEMA_INVALID", "Execution-plan job schema is invalid")
        for key in (
            "job_sha256", "preparation_receipt_sha256", "bootstrap_receipt_sha256",
            "object_manifest_sha256", "recipe_sha256", "pending_objects_sha256",
        ):
            _sha256(plan_job[key])
        for key in (
            "job_revision", "bootstrap_bundle_revision", "content_revision",
        ):
            _sha256_revision(plan_job[key])
        _pinned_revision(plan_job["project_revision"])
        _positive_int(plan_job["pending_object_count"], maximum=MAX_OBJECTS)
        if (
            type(plan_job["asset_snapshot_revision"]) is not str
            or ASSET_SNAPSHOT_RE.fullmatch(plan_job["asset_snapshot_revision"]) is None
        ):
            fail("SEMANTIC_EXECUTOR_SCHEMA_INVALID", "Execution-plan snapshot revision is invalid")
    except SemanticIndexExecutorError:
        fail("SEMANTIC_EXECUTOR_PLAN_INVALID", "Execution-plan job binding is invalid")
    _exact_object(
        plan["executor"],
        {
            "git_commit", "git_object_format", "executor_source_sha256",
            "adapter_source_sha256", "preparer_source_sha256",
            "bootstrap_source_sha256", "job_schema_sha256",
            "execution_plan_schema_sha256",
        },
    )
    if (
        not isinstance(plan["executor"]["git_commit"], str)
        or GIT_COMMIT_RE.fullmatch(plan["executor"]["git_commit"]) is None
        or plan["executor"]["git_object_format"] not in {"sha1", "sha256"}
    ):
        fail("SEMANTIC_EXECUTOR_PLAN_INVALID", "Executor Git pin is invalid")
    for key in SOURCE_PIN_PATHS:
        _sha256(plan["executor"][key])
    adapter_plan = _exact_object(
        plan["adapter"],
        {"adapter_id", "adapter_contract_revision", "production_capable"},
    )
    try:
        _safe_string(adapter_plan["adapter_id"], pattern=SAFE_ADAPTER_RE)
        _pinned_revision(adapter_plan["adapter_contract_revision"])
        if type(adapter_plan["production_capable"]) is not bool:
            fail("SEMANTIC_EXECUTOR_SCHEMA_INVALID", "Adapter capability must be Boolean")
        _validate_caption_contract(plan["caption"])
        _validate_embedding_contract(plan["embedding"])
        _validate_storage_contract(plan["storage"])
    except SemanticIndexExecutorError:
        fail("SEMANTIC_EXECUTOR_PLAN_INVALID", "Execution-plan scalar contract is invalid")
    images = _exact_object(
        plan["runtime_images"],
        {"unreal", "postgres", "qdrant", "embedding"},
    )
    for value in images.values():
        if (
            not isinstance(value, str)
            or not 1 <= len(value) <= 512
            or IMAGE_DIGEST_RE.fullmatch(value) is None
        ):
            fail("SEMANTIC_EXECUTOR_IMAGE_PIN_INVALID", "Every runtime image must use an immutable digest")
    _exact_object(
        plan["storage"],
        STORAGE_CONTRACT_KEYS,
    )
    limits = _exact_object(
        plan["limits"],
        {
            "phase_timeout_seconds", "max_total_elapsed_seconds", "expected_catalog_records",
            "expected_postgres_rows", "expected_qdrant_points", "expected_dense_vectors",
            "expected_sparse_vectors", "expected_rendered_views",
        },
    )
    _positive_int(limits["phase_timeout_seconds"], maximum=86_400)
    _positive_int(limits["max_total_elapsed_seconds"], maximum=604_800)
    for key in set(limits) - {"phase_timeout_seconds", "max_total_elapsed_seconds"}:
        _positive_int(limits[key], maximum=MAX_SAFE_INTEGER)
    credentials = plan["credential_files_required"]
    if (
        not isinstance(credentials, list)
        or any(label not in SECRET_LABELS for label in credentials)
        or credentials != sorted(set(credentials), key=SECRET_LABELS.index)
    ):
        fail("SEMANTIC_EXECUTOR_PLAN_INVALID", "Credential requirements are invalid")
    if (
        plan["profile"] == "offline_fixture" and credentials != []
    ) or (
        plan["profile"] == "production" and credentials != list(SECRET_LABELS)
    ):
        fail("SEMANTIC_EXECUTOR_PLAN_INVALID", "Execution profile has the wrong credential-file contract")
    credential_bindings = plan["credential_bindings"]
    if not isinstance(credential_bindings, dict):
        fail("SEMANTIC_EXECUTOR_PLAN_INVALID", "Credential bindings are invalid")
    if plan["profile"] == "offline_fixture":
        if credential_bindings != {}:
            fail(
                "SEMANTIC_EXECUTOR_PLAN_INVALID",
                "Offline fixture plans cannot bind credential identities",
            )
    else:
        _exact_object(credential_bindings, set(SECRET_LABELS))
        for label in SECRET_LABELS:
            binding = _exact_object(
                credential_bindings[label],
                {"credential_generation", "target_identity"},
            )
            if (
                not isinstance(binding["credential_generation"], str)
                or CREDENTIAL_GENERATION_RE.fullmatch(binding["credential_generation"])
                is None
                or not isinstance(binding["target_identity"], str)
                or TARGET_IDENTITY_PATTERNS[label].fullmatch(binding["target_identity"])
                is None
            ):
                fail(
                    "SEMANTIC_EXECUTOR_PLAN_INVALID",
                    "A production credential binding is invalid",
                )
    _sha256(plan["approval_ref_sha256"])
    return plan


def validate_bundle(
    *,
    job_path: Path,
    preparation_receipt_path: Path,
    execution_plan_path: Path,
    expected_job_sha256: str,
    expected_preparation_receipt_sha256: str,
    expected_execution_plan_sha256: str,
    expected_job_schema_sha256: str,
    expected_execution_plan_schema_sha256: str,
    repo_root: Path,
) -> ValidatedBundle:
    """Validate all sealed bytes and independently supplied pins without writes."""

    repo_root = _normalized_absolute(repo_root)
    expected_job_sha256 = _sha256(expected_job_sha256)
    expected_preparation_receipt_sha256 = _sha256(expected_preparation_receipt_sha256)
    expected_execution_plan_sha256 = _sha256(expected_execution_plan_sha256)
    expected_job_schema_sha256 = _sha256(expected_job_schema_sha256)
    expected_execution_plan_schema_sha256 = _sha256(expected_execution_plan_schema_sha256)

    if (
        job_path.name != "semantic-index-job.json"
        or preparation_receipt_path.name != "preparation-receipt.json"
        or job_path.parent != preparation_receipt_path.parent
    ):
        fail("SEMANTIC_EXECUTOR_BUNDLE_INVALID", "Job and receipt must be the original sealed bundle pair")
    bundle_dir = _normalized_absolute(job_path.parent)
    _reject_symlink_components(bundle_dir)
    try:
        bundle_info = os.lstat(bundle_dir)
    except OSError:
        fail("SEMANTIC_EXECUTOR_BUNDLE_INVALID", "Sealed bundle directory is unavailable")
    if (
        not stat.S_ISDIR(bundle_info.st_mode)
        or bundle_info.st_uid != os.geteuid()
        or stat.S_IMODE(bundle_info.st_mode) != 0o700
    ):
        fail("SEMANTIC_EXECUTOR_BUNDLE_INVALID", "Sealed bundle directory is not private")

    job_raw = secure_read(job_path, maximum=MAX_JOB_BYTES, private=True)
    receipt_raw = secure_read(preparation_receipt_path, maximum=MAX_JSON_BYTES, private=True)
    plan_raw = secure_read(execution_plan_path, maximum=MAX_JSON_BYTES, private=True)
    job_sha256 = hashlib.sha256(job_raw).hexdigest()
    receipt_sha256 = hashlib.sha256(receipt_raw).hexdigest()
    plan_sha256 = hashlib.sha256(plan_raw).hexdigest()
    if (
        job_sha256 != expected_job_sha256
        or receipt_sha256 != expected_preparation_receipt_sha256
        or plan_sha256 != expected_execution_plan_sha256
    ):
        fail("SEMANTIC_EXECUTOR_EXTERNAL_PIN_MISMATCH", "A sealed input differs from its independent pin")

    job = _validate_job(job_raw)
    receipt = _validate_preparation_receipt(receipt_raw, job, job_sha256)
    plan = _validate_plan_shape(plan_raw)
    plan_job = plan["job"]
    input_contract = job["input_contract"]
    pending = job["pending_objects"]
    exact_job_pins = {
        "job_schema": job["schema"],
        "job_sha256": job_sha256,
        "job_revision": job["job_revision"],
        "preparation_receipt_sha256": receipt_sha256,
        "bootstrap_receipt_sha256": input_contract["bootstrap_receipt_sha256"],
        "bootstrap_bundle_revision": input_contract["bootstrap_bundle_revision"],
        "object_manifest_sha256": input_contract["object_manifest_sha256"],
        "recipe_sha256": job["recipe_contract"]["recipe_sha256"],
        "project_revision": input_contract["project"]["revision"],
        "content_revision": input_contract["content"]["revision"],
        "pending_objects_sha256": pending["assets_sha256"],
        "pending_object_count": pending["count"],
        "asset_snapshot_revision": job["snapshot_target"]["asset_snapshot_revision"],
    }
    if canonical_json(plan_job, pretty=False) != canonical_json(exact_job_pins, pretty=False):
        fail("SEMANTIC_EXECUTOR_PLAN_PIN_MISMATCH", "Execution plan does not bind the sealed job exactly")

    if canonical_json(plan["caption"], pretty=False) != canonical_json(
        job["recipe_contract"]["caption"], pretty=False
    ):
        fail("SEMANTIC_EXECUTOR_MODEL_PIN_MISMATCH", "Caption, prompt, image, or schema pins differ")
    if canonical_json(plan["embedding"], pretty=False) != canonical_json(
        job["recipe_contract"]["embedding"], pretty=False
    ):
        fail("SEMANTIC_EXECUTOR_MODEL_PIN_MISMATCH", "Embedding model pins differ")
    if canonical_json(plan["storage"], pretty=False) != canonical_json(
        job["recipe_contract"]["storage"], pretty=False
    ):
        fail("SEMANTIC_EXECUTOR_STORAGE_PIN_MISMATCH", "Storage pins differ")

    count = pending["count"]
    expected_limits = {
        "expected_catalog_records": count,
        "expected_postgres_rows": count,
        "expected_qdrant_points": count,
        "expected_dense_vectors": count,
        "expected_sparse_vectors": count,
        "expected_rendered_views": count * job["recipe_contract"]["caption"]["views_per_asset"],
    }
    if any(plan["limits"][key] != value for key, value in expected_limits.items()):
        fail("SEMANTIC_EXECUTOR_ROW_LIMIT_MISMATCH", "Execution row/point limits differ from the sealed job")

    git_commit, verified_sources = _verify_git_source_closure(repo_root, plan["executor"])
    if (
        plan["executor"]["job_schema_sha256"] != expected_job_schema_sha256
        or plan["executor"]["execution_plan_schema_sha256"]
        != expected_execution_plan_schema_sha256
    ):
        fail("SEMANTIC_EXECUTOR_SOURCE_PIN_MISMATCH", "Schema source differs from its independent pin")

    job_schema_raw = verified_sources[SOURCE_PIN_PATHS["job_schema_sha256"]]
    plan_schema_raw = verified_sources[SOURCE_PIN_PATHS["execution_plan_schema_sha256"]]
    job_schema = strict_json(job_schema_raw)
    plan_schema = strict_json(plan_schema_raw)
    plan_schema_properties = plan_schema.get("properties", {}) if isinstance(plan_schema, dict) else {}
    plan_schema_defs = plan_schema.get("$defs", {}) if isinstance(plan_schema, dict) else {}
    job_schema_properties = job_schema.get("properties", {}) if isinstance(job_schema, dict) else {}
    job_schema_defs = job_schema.get("$defs", {}) if isinstance(job_schema, dict) else {}
    plan_job_schema = plan_schema_properties.get("job", {})
    adapter_schema = plan_schema_properties.get("adapter", {})
    executor_schema = plan_schema_properties.get("executor", {})
    runtime_image_schema = plan_schema_defs.get("image", {})
    credential_schema = plan_schema_properties.get("credential_files_required", {})
    credential_binding_schema = plan_schema_defs.get("credentialBindings", {})
    credential_binding_item_schema = plan_schema_defs.get("credentialBinding", {})
    credential_binding_properties = credential_binding_schema.get("properties", {})
    credential_target_contracts: dict[str, Any] = {}
    if isinstance(credential_binding_properties, dict):
        for label, value in credential_binding_properties.items():
            if not isinstance(value, dict):
                continue
            all_of = value.get("allOf")
            if not isinstance(all_of, list) or len(all_of) != 2:
                continue
            base_ref, target_contract = all_of
            if base_ref != {"$ref": "#/$defs/credentialBinding"} or not isinstance(
                target_contract,
                dict,
            ):
                continue
            credential_target_contracts[label] = (
                target_contract.get("properties", {})
                .get("target_identity", {})
            )
    expected_credential_profile_contract = [
        {
            "if": {
                "properties": {"profile": {"const": "offline_fixture"}},
                "required": ["profile"],
            },
            "then": {
                "properties": {
                    "credential_files_required": {"maxItems": 0},
                    "credential_bindings": {"maxProperties": 0},
                }
            },
            "else": {
                "properties": {
                    "credential_files_required": {
                        "prefixItems": [
                            {"const": label} for label in SECRET_LABELS
                        ],
                        "items": False,
                        "minItems": len(SECRET_LABELS),
                        "maxItems": len(SECRET_LABELS),
                    },
                    "credential_bindings": {"required": list(SECRET_LABELS)},
                }
            },
        }
    ]
    job_snapshot_revision_schema = (
        job_schema_properties.get("snapshot_target", {})
        .get("properties", {})
        .get("asset_snapshot_revision", {})
    )
    job_pending_count_schema = (
        job_schema_properties.get("pending_objects", {})
        .get("properties", {})
        .get("count", {})
    )
    if (
        not isinstance(job_schema, dict)
        or job_schema.get("properties", {}).get("schema", {}).get("const") != JOB_SCHEMA
        or set(job_schema.get("required", [])) != set(JOB_TOP_KEYS)
        or job_schema.get("additionalProperties") is not False
        or not isinstance(plan_schema, dict)
        or plan_schema.get("properties", {}).get("schema", {}).get("const") != EXECUTION_PLAN_SCHEMA
        or set(plan_schema.get("required", [])) != set(PLAN_KEYS)
        or plan_schema.get("additionalProperties") is not False
        or plan_job_schema.get("additionalProperties") is not False
        or set(plan_job_schema.get("required", [])) != set(plan["job"])
        or plan_job_schema.get("properties", {}).get("project_revision")
        != {"$ref": "#/$defs/revision"}
        or plan_job_schema.get("properties", {}).get("pending_object_count")
        != job_pending_count_schema
        or plan_job_schema.get("properties", {}).get("asset_snapshot_revision")
        != job_snapshot_revision_schema
        or plan_schema_defs.get("revision") != job_schema_defs.get("revision")
        or plan_schema_defs.get("id") != job_schema_defs.get("id")
        or plan_schema_defs.get("caption") != job_schema_defs.get("captionRecipe")
        or plan_schema_defs.get("embedding") != job_schema_defs.get("embeddingRecipe")
        or plan_schema_defs.get("storage") != job_schema_defs.get("storageRecipe")
        or adapter_schema
        != {
            "type": "object",
            "additionalProperties": False,
            "required": ["adapter_id", "adapter_contract_revision", "production_capable"],
            "properties": {
                "adapter_id": {"$ref": "#/$defs/adapterId"},
                "adapter_contract_revision": {"$ref": "#/$defs/revision"},
                "production_capable": {"type": "boolean"},
            },
        }
        or plan_schema_defs.get("adapterId")
        != {
            "type": "string",
            "minLength": 3,
            "maxLength": 128,
            "pattern": SAFE_ADAPTER_RE.pattern,
        }
        or executor_schema.get("additionalProperties") is not False
        or set(executor_schema.get("required", []))
        != {
            "git_commit", "git_object_format", "executor_source_sha256",
            "adapter_source_sha256", "preparer_source_sha256",
            "bootstrap_source_sha256", "job_schema_sha256",
            "execution_plan_schema_sha256",
        }
        or runtime_image_schema.get("minLength") != 72
        or runtime_image_schema.get("maxLength") != 512
        or runtime_image_schema.get("pattern") != r"^[^\s@]+@sha256:[a-f0-9]{64}$"
        or credential_schema.get("maxItems") != 4
        or credential_schema.get("uniqueItems") is not True
        or credential_schema.get("items", {}).get("enum") != list(SECRET_LABELS)
        or plan_schema.get("allOf") != expected_credential_profile_contract
        or credential_binding_schema.get("additionalProperties") is not False
        or credential_binding_schema.get("maxProperties") != 4
        or set(credential_binding_properties) != set(SECRET_LABELS)
        or credential_binding_item_schema.get("additionalProperties") is not False
        or set(credential_binding_item_schema.get("required", []))
        != {"credential_generation", "target_identity"}
        or credential_binding_item_schema.get("properties", {})
        .get("credential_generation", {})
        != {
            "type": "string",
            "minLength": 23,
            "maxLength": 213,
            "pattern": CREDENTIAL_GENERATION_RE.pattern,
        }
        or credential_binding_item_schema.get("properties", {})
        .get("target_identity")
        != {"type": "string"}
        or credential_target_contracts
        != {
            label: {
                "type": "string",
                "minLength": TARGET_IDENTITY_LENGTHS[label][0],
                "maxLength": TARGET_IDENTITY_LENGTHS[label][1],
                "pattern": TARGET_IDENTITY_PATTERNS[label].pattern,
            }
            for label in SECRET_LABELS
        }
    ):
        fail("SEMANTIC_EXECUTOR_SCHEMA_PIN_INVALID", "A pinned schema does not describe the supported closed contract")
    return _register_validated_bundle(ValidatedBundle(
        job=job,
        receipt=receipt,
        plan=plan,
        job_sha256=job_sha256,
        receipt_sha256=receipt_sha256,
        plan_sha256=plan_sha256,
        observed_git_commit=git_commit,
        repo_root=repo_root,
        adapter_source_bytes=verified_sources[SOURCE_PIN_PATHS["adapter_source_sha256"]],
    ))


def _validate_requested_adapter_plan(
    bundle: ValidatedBundle,
    *,
    requested_adapter_id: str,
) -> None:
    _require_registered_bundle(bundle)
    adapter_plan = bundle.plan["adapter"]
    if (
        SAFE_ADAPTER_RE.fullmatch(requested_adapter_id) is None
        or requested_adapter_id != OFFLINE_ADAPTER_ID
        or adapter_plan
        != {
            "adapter_id": OFFLINE_ADAPTER_ID,
            "adapter_contract_revision": OFFLINE_ADAPTER_REVISION,
            "production_capable": False,
        }
    ):
        fail("SEMANTIC_EXECUTOR_ADAPTER_UNAVAILABLE", "Requested adapter is not in the reviewed static registry")
    profile = bundle.plan["profile"]
    if profile == "production":
        fail("SEMANTIC_EXECUTOR_ADAPTER_NOT_PRODUCTION", "No reviewed production adapter is selected")
    if profile != "offline_fixture":
        fail("SEMANTIC_EXECUTOR_ADAPTER_PROFILE_MISMATCH", "Production adapters cannot run under the fixture profile")


def _load_adapter_runtime(
    bundle: ValidatedBundle,
    *,
    requested_adapter_id: str,
) -> AdapterRuntime:
    """Execute only the dependency bytes attested during bundle validation."""

    _validate_requested_adapter_plan(bundle, requested_adapter_id=requested_adapter_id)
    adapter_source_bytes = bundle.adapter_source_bytes
    if (
        type(adapter_source_bytes) is not bytes
        or hashlib.sha256(adapter_source_bytes).hexdigest()
        != bundle.plan["executor"]["adapter_source_sha256"]
    ):
        fail(
            "SEMANTIC_EXECUTOR_SOURCE_PIN_MISMATCH",
            "Adapter bytes changed after source attestation",
        )
    module_name = (
        "_simworld_semantic_index_job_adapters_"
        + bundle.plan["executor"]["adapter_source_sha256"][:16]
    )
    module = ModuleType(module_name)
    module.__file__ = str(bundle.repo_root / SOURCE_PIN_PATHS["adapter_source_sha256"])
    module.__package__ = ""
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        code = compile(adapter_source_bytes, module.__file__, "exec", dont_inherit=True)
        exec(code, module.__dict__)
    except Exception:
        fail("SEMANTIC_EXECUTOR_ADAPTER_INVALID", "Reviewed adapter module could not be loaded safely")
    finally:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous

    if (
        getattr(module, "PHASES", None) != PHASES
        or getattr(module, "PHASE_METRIC_KEYS", None) != PHASE_METRIC_KEYS
    ):
        fail("SEMANTIC_EXECUTOR_ADAPTER_INVALID", "Adapter phase contract differs from the executor")
    try:
        registry = module.registered_adapters()
        fixture_type = module.OfflineFixtureAdapter
        instance = registry[requested_adapter_id]
        phase_request_type = module.PhaseRequest
        phase_result_type = module.PhaseResult
        secret_snapshot_type = module.SecretSnapshot
    except (AttributeError, KeyError, TypeError):
        fail("SEMANTIC_EXECUTOR_ADAPTER_INVALID", "Adapter registry is incomplete")
    if (
        type(registry) is not dict
        or set(registry) != {OFFLINE_ADAPTER_ID}
        or type(instance) is not fixture_type
        or getattr(fixture_type, "__slots__", None) != ()
        or hasattr(instance, "__dict__")
        or type(instance).run_phase is not fixture_type.run_phase
        or instance.adapter_id != OFFLINE_ADAPTER_ID
        or instance.adapter_contract_revision != OFFLINE_ADAPTER_REVISION
        or instance.production_capable is not False
        or not all(
            isinstance(value, type)
            and value.__module__ == module_name
            for value in (phase_request_type, phase_result_type, secret_snapshot_type)
        )
    ):
        fail("SEMANTIC_EXECUTOR_ADAPTER_INVALID", "Adapter registry is not the reviewed sealed registry")
    return AdapterRuntime(
        module=module,
        instance=instance,
        phase_request_type=phase_request_type,
        phase_result_type=phase_result_type,
        secret_snapshot_type=secret_snapshot_type,
    )


def _validate_secret_path_contract(
    plan: Mapping[str, Any],
    paths: SecretPathArguments,
) -> Mapping[str, str | None]:
    supplied = {
        "caption": paths.caption,
        "postgres": paths.postgres,
        "qdrant": paths.qdrant,
        "embedding": paths.embedding,
    }
    required = set(plan["credential_files_required"])
    if plan["profile"] == "offline_fixture" and (required or any(supplied.values())):
        fail("SEMANTIC_EXECUTOR_FIXTURE_SECRET_PROHIBITED", "Offline fixture execution must not receive credentials")
    if set(label for label, path in supplied.items() if path is not None) != required:
        fail("SEMANTIC_EXECUTOR_SECRET_FILE_REQUIRED", "Supply exactly the credential files sealed by the plan")
    supplied_paths = [os.path.abspath(path) for path in supplied.values() if path is not None]
    if len(supplied_paths) != len(set(supplied_paths)):
        fail("SEMANTIC_EXECUTOR_SECRET_FILE_INVALID", "Credential files must use distinct absolute paths")
    return supplied


def _load_secret_snapshot(
    plan: Mapping[str, Any],
    paths: SecretPathArguments,
    adapter: AdapterRuntime,
) -> Any:
    supplied = _validate_secret_path_contract(plan, paths)
    captured = {
        label: read_secret_file_once(Path(path)) if path is not None else None
        for label, path in supplied.items()
    }
    try:
        return adapter.secret_snapshot_type(**captured)
    except (TypeError, ValueError):
        fail("SEMANTIC_EXECUTOR_ADAPTER_INVALID", "Adapter secret contract is invalid")


def dry_run_result(bundle: ValidatedBundle) -> dict[str, Any]:
    return {
        "schema": RESULT_SCHEMA,
        "valid": True,
        "status": "dry_run",
        "profile": bundle.plan["profile"],
        "job_revision": bundle.job["job_revision"],
        "job_sha256": bundle.job_sha256,
        "preparation_receipt_sha256": bundle.receipt_sha256,
        "execution_plan_sha256": bundle.plan_sha256,
        "adapter_id": bundle.plan["adapter"]["adapter_id"],
        "phase_order": list(PHASES),
        "execution_started": False,
        "network_used": False,
        "unreal_started": False,
        "caption_provider_called": False,
        "postgres_contacted": False,
        "qdrant_contacted": False,
        "embedding_called": False,
        "catalog_complete": False,
        "snapshot_complete": False,
        "production_complete": False,
    }


def _isolated_python_runtime_supported(
    *,
    version: tuple[int, int],
    flags: Any,
    executable: str,
    prefix: str,
    base_prefix: str,
    loaded_modules: Mapping[str, Any],
) -> bool:
    """Check the exact system-Python ``-I -S`` execution contract.

    Python 3.10 does not expose ``sys.flags.safe_path``.  Python 3.11 and
    newer do, so those versions additionally require it to be true.
    """

    if version not in SUPPORTED_EXECUTION_PYTHON_MINORS:
        return False
    safe_path = getattr(flags, "safe_path", None)
    if version >= (3, 11) and safe_path is not True:
        return False
    if version == (3, 10) and safe_path not in {None, True}:
        return False
    return bool(
        executable == SYSTEM_PYTHON
        and prefix == base_prefix
        and getattr(flags, "isolated", 0) == 1
        and getattr(flags, "ignore_environment", 0) == 1
        and getattr(flags, "no_user_site", 0) == 1
        and getattr(flags, "no_site", 0) == 1
        and not {"site", "sitecustomize", "usercustomize"}.intersection(loaded_modules)
    )


def _require_isolated_python() -> None:
    """Reject state-changing execution outside exact system Python ``-I -S``."""

    if not _isolated_python_runtime_supported(
        version=(sys.version_info.major, sys.version_info.minor),
        flags=sys.flags,
        executable=sys.executable,
        prefix=sys.prefix,
        base_prefix=sys.base_prefix,
        loaded_modules=sys.modules,
    ):
        fail(
            "SEMANTIC_EXECUTOR_PYTHON_NOT_ISOLATED",
            "Execution requires an externally attested /usr/bin/python3 -I -S launcher",
        )


@dataclass(slots=True)
class RunDirectoryHandle:
    """Held directory descriptors and inode bindings for one executor run."""

    path: Path
    parent_path: Path
    name: str
    parent_fd: int
    run_fd: int
    parent_identity: tuple[int, int]
    run_identity: tuple[int, int]
    lock_fd: int | None = None
    lock_identity: tuple[int, int] | None = None


def _object_identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _private_directory(metadata: os.stat_result, *, exact_mode: int | None) -> bool:
    mode = stat.S_IMODE(metadata.st_mode)
    return bool(
        stat.S_ISDIR(metadata.st_mode)
        and metadata.st_uid == os.geteuid()
        and metadata.st_nlink >= 2
        and (mode == exact_mode if exact_mode is not None else mode & 0o077 == 0)
    )


def _private_evidence_file(metadata: os.stat_result) -> bool:
    return bool(
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_uid == os.geteuid()
        and stat.S_IMODE(metadata.st_mode) == 0o600
        and metadata.st_nlink == 1
    )


def _safe_entry_name(name: str) -> str:
    if not name or name in {".", ".."} or "/" in name or "\x00" in name:
        fail("SEMANTIC_EXECUTOR_RUN_DIR_INVALID", "Run-directory entry name is invalid")
    return name


def _fsync_directory_fd(descriptor: int) -> None:
    os.fsync(descriptor)


def _close_descriptor(descriptor: int) -> bool:
    """Close once; callers decide whether a cleanup error may replace a primary error."""

    try:
        os.close(descriptor)
    except OSError:
        return False
    return True


def _assert_run_directory_binding(
    handle: RunDirectoryHandle,
    *,
    require_lock: bool = False,
) -> None:
    """Fail if the held parent/run/lock inodes no longer own their path names."""

    try:
        _reject_symlink_components(handle.parent_path)
    except SemanticIndexExecutorError:
        fail("SEMANTIC_EXECUTOR_RUN_DIR_INVALID", "Run-directory parent binding changed")
    try:
        parent_fd_metadata = os.fstat(handle.parent_fd)
        run_fd_metadata = os.fstat(handle.run_fd)
        parent_path_metadata = os.lstat(handle.parent_path)
        run_name_metadata = os.stat(
            handle.name,
            dir_fd=handle.parent_fd,
            follow_symlinks=False,
        )
    except OSError:
        fail("SEMANTIC_EXECUTOR_RUN_DIR_INVALID", "Run-directory binding is unavailable")
    if (
        _object_identity(parent_fd_metadata) != handle.parent_identity
        or _object_identity(parent_path_metadata) != handle.parent_identity
        or not _private_directory(parent_fd_metadata, exact_mode=None)
        or not _private_directory(parent_path_metadata, exact_mode=None)
        or _object_identity(run_fd_metadata) != handle.run_identity
        or _object_identity(run_name_metadata) != handle.run_identity
        or not _private_directory(run_fd_metadata, exact_mode=0o700)
        or not _private_directory(run_name_metadata, exact_mode=0o700)
    ):
        fail("SEMANTIC_EXECUTOR_RUN_DIR_INVALID", "Run-directory binding changed")
    if require_lock:
        if handle.lock_fd is None or handle.lock_identity is None:
            fail("SEMANTIC_EXECUTOR_RUN_BUSY", "Run lock is not held", retryable=True)
        try:
            lock_fd_metadata = os.fstat(handle.lock_fd)
            lock_name_metadata = os.stat(
                "executor.lock",
                dir_fd=handle.run_fd,
                follow_symlinks=False,
            )
        except OSError:
            fail("SEMANTIC_EXECUTOR_RUN_BUSY", "Run lock binding changed", retryable=True)
        if (
            _object_identity(lock_fd_metadata) != handle.lock_identity
            or _object_identity(lock_name_metadata) != handle.lock_identity
            or not _private_evidence_file(lock_fd_metadata)
            or not _private_evidence_file(lock_name_metadata)
        ):
            fail("SEMANTIC_EXECUTOR_RUN_BUSY", "Run lock binding changed", retryable=True)


@contextlib.contextmanager
def _open_run_directory(path: Path, *, create: bool):
    """Open a private run directory through a held private-parent descriptor."""

    path = _normalized_absolute(path)
    parent_path = path.parent
    name = _safe_entry_name(path.name)
    if parent_path == path:
        fail("SEMANTIC_EXECUTOR_RUN_DIR_INVALID", "Run directory cannot be a filesystem root")
    _reject_symlink_components(parent_path)
    try:
        parent_before = os.lstat(parent_path)
    except OSError:
        fail("SEMANTIC_EXECUTOR_RUN_DIR_INVALID", "Run-directory parent is unavailable")
    if not _private_directory(parent_before, exact_mode=None):
        fail("SEMANTIC_EXECUTOR_RUN_DIR_INVALID", "Run-directory parent must be private")

    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    parent_fd = -1
    run_fd = -1
    try:
        parent_fd = os.open(parent_path, directory_flags)
        parent_opened = os.fstat(parent_fd)
        parent_after = os.lstat(parent_path)
        if (
            _object_identity(parent_opened) != _object_identity(parent_before)
            or _object_identity(parent_after) != _object_identity(parent_before)
            or not _private_directory(parent_opened, exact_mode=None)
            or not _private_directory(parent_after, exact_mode=None)
        ):
            fail("SEMANTIC_EXECUTOR_RUN_DIR_INVALID", "Run-directory parent binding changed")
        created = False
        if create:
            try:
                os.mkdir(name, 0o700, dir_fd=parent_fd)
                created = True
            except FileExistsError:
                pass
            except OSError:
                fail("SEMANTIC_EXECUTOR_RUN_DIR_INVALID", "Run directory could not be created")
        if created:
            try:
                _fsync_directory_fd(parent_fd)
            except OSError:
                fail("SEMANTIC_EXECUTOR_RUN_DIR_INVALID", "Run-directory creation is not durable")
        try:
            run_before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            run_fd = os.open(name, directory_flags, dir_fd=parent_fd)
            run_opened = os.fstat(run_fd)
        except OSError:
            fail("SEMANTIC_EXECUTOR_RUN_DIR_INVALID", "Run directory is unavailable")
        if (
            not _private_directory(run_before, exact_mode=0o700)
            or not _private_directory(run_opened, exact_mode=0o700)
            or _object_identity(run_before) != _object_identity(run_opened)
        ):
            fail("SEMANTIC_EXECUTOR_RUN_DIR_INVALID", "Run directory has unsafe metadata")
        handle = RunDirectoryHandle(
            path=path,
            parent_path=parent_path,
            name=name,
            parent_fd=parent_fd,
            run_fd=run_fd,
            parent_identity=_object_identity(parent_opened),
            run_identity=_object_identity(run_opened),
        )
        _assert_run_directory_binding(handle)
        yield handle
    finally:
        primary_error_active = sys.exc_info()[0] is not None
        cleanup_failed = False
        if run_fd >= 0:
            cleanup_failed = not _close_descriptor(run_fd)
        if parent_fd >= 0:
            cleanup_failed = not _close_descriptor(parent_fd) or cleanup_failed
        if cleanup_failed and not primary_error_active:
            fail("SEMANTIC_EXECUTOR_RUN_DIR_INVALID", "Run-directory descriptors could not be closed")


@contextlib.contextmanager
def _run_lock(handle: RunDirectoryHandle):
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open("executor.lock", flags, 0o600, dir_fd=handle.run_fd)
    except OSError:
        fail("SEMANTIC_EXECUTOR_RUN_BUSY", "Run lock is unavailable", retryable=True)
    try:
        metadata = os.fstat(descriptor)
        if not _private_evidence_file(metadata):
            fail("SEMANTIC_EXECUTOR_RUN_BUSY", "Run lock has unsafe metadata")
        try:
            named_metadata = os.stat(
                "executor.lock",
                dir_fd=handle.run_fd,
                follow_symlinks=False,
            )
        except OSError:
            fail("SEMANTIC_EXECUTOR_RUN_BUSY", "Run lock binding changed", retryable=True)
        if _object_identity(named_metadata) != _object_identity(metadata):
            fail("SEMANTIC_EXECUTOR_RUN_BUSY", "Run lock binding changed", retryable=True)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fail("SEMANTIC_EXECUTOR_RUN_BUSY", "Another executor owns this run", retryable=True)
        handle.lock_fd = descriptor
        handle.lock_identity = _object_identity(metadata)
        _assert_run_directory_binding(handle, require_lock=True)
        yield
    finally:
        primary_error_active = sys.exc_info()[0] is not None
        handle.lock_fd = None
        handle.lock_identity = None
        cleanup_failed = False
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            cleanup_failed = True
        cleanup_failed = not _close_descriptor(descriptor) or cleanup_failed
        if cleanup_failed and not primary_error_active:
            fail("SEMANTIC_EXECUTOR_RUN_BUSY", "Run lock could not be released", retryable=True)


def _rename_no_replace_at(
    source_fd: int,
    source_name: str,
    destination_fd: int,
    destination_name: str,
) -> None:
    source_name = _safe_entry_name(source_name)
    destination_name = _safe_entry_name(destination_name)
    renameat2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if renameat2 is None:
        fail(
            "SEMANTIC_EXECUTOR_ATOMIC_RENAME_UNAVAILABLE",
            "Host does not expose atomic no-replace rename",
        )
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        source_fd,
        os.fsencode(source_name),
        destination_fd,
        os.fsencode(destination_name),
        1,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise FileExistsError(error_number, os.strerror(error_number), destination_name)
    if error_number in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP}:
        fail(
            "SEMANTIC_EXECUTOR_ATOMIC_RENAME_UNAVAILABLE",
            "Filesystem does not support atomic no-replace rename",
        )
    raise OSError(error_number, os.strerror(error_number), destination_name)


def _safe_recovery_file(metadata: os.stat_result) -> bool:
    return bool(
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_uid == os.geteuid()
        and stat.S_IMODE(metadata.st_mode) == 0o600
    )


def _validate_recovery_quarantine_at(descriptor: int) -> None:
    try:
        metadata = os.fstat(descriptor)
        entries = os.listdir(descriptor)
    except OSError:
        fail("SEMANTIC_EXECUTOR_RUN_DIR_INVALID", "Recovery quarantine cannot be inspected")
    if not _private_directory(metadata, exact_mode=0o700):
        fail("SEMANTIC_EXECUTOR_RUN_DIR_INVALID", "Recovery quarantine has unsafe metadata")
    for name in entries:
        try:
            item = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except OSError:
            fail("SEMANTIC_EXECUTOR_RUN_DIR_INVALID", "Recovery evidence is unavailable")
        if (
            re.fullmatch(
                r"(?:execution-state|terminal-receipt)\.json\.tmp-[A-Za-z0-9_-]+\.recovered-[a-f0-9]{16}",
                name,
            )
            is None
            or not _safe_recovery_file(item)
            or item.st_nlink != 1
        ):
            fail("SEMANTIC_EXECUTOR_RUN_DIR_INVALID", "Recovery quarantine contains unsafe evidence")


def _quarantine_orphan_at(handle: RunDirectoryHandle, temporary_name: str) -> None:
    try:
        os.mkdir(".recovery-quarantine", 0o700, dir_fd=handle.run_fd)
        _fsync_directory_fd(handle.run_fd)
    except FileExistsError:
        pass
    except OSError:
        fail("SEMANTIC_EXECUTOR_RUN_DIR_INVALID", "Recovery quarantine could not be created")
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    quarantine_fd = -1
    try:
        quarantine_before = os.stat(
            ".recovery-quarantine",
            dir_fd=handle.run_fd,
            follow_symlinks=False,
        )
        quarantine_fd = os.open(
            ".recovery-quarantine",
            directory_flags,
            dir_fd=handle.run_fd,
        )
        quarantine_opened = os.fstat(quarantine_fd)
        if (
            not _private_directory(quarantine_before, exact_mode=0o700)
            or not _private_directory(quarantine_opened, exact_mode=0o700)
            or _object_identity(quarantine_before) != _object_identity(quarantine_opened)
        ):
            fail("SEMANTIC_EXECUTOR_RUN_DIR_INVALID", "Recovery quarantine has unsafe metadata")
        _validate_recovery_quarantine_at(quarantine_fd)
        base = temporary_name.removeprefix(".")
        for _attempt in range(8):
            destination_name = f"{base}.recovered-{os.urandom(8).hex()}"
            try:
                _assert_run_directory_binding(handle, require_lock=True)
                current_quarantine = os.stat(
                    ".recovery-quarantine",
                    dir_fd=handle.run_fd,
                    follow_symlinks=False,
                )
                if _object_identity(current_quarantine) != _object_identity(quarantine_opened):
                    fail("SEMANTIC_EXECUTOR_RUN_DIR_INVALID", "Recovery quarantine binding changed")
                _rename_no_replace_at(
                    handle.run_fd,
                    temporary_name,
                    quarantine_fd,
                    destination_name,
                )
                _fsync_directory_fd(quarantine_fd)
                _fsync_directory_fd(handle.run_fd)
                return
            except FileExistsError:
                continue
            except SemanticIndexExecutorError:
                raise
            except OSError:
                fail("SEMANTIC_EXECUTOR_RUN_DIR_INVALID", "Recovery evidence could not be quarantined")
        fail("SEMANTIC_EXECUTOR_RUN_DIR_INVALID", "Recovery evidence name could not be reserved")
    except SemanticIndexExecutorError:
        raise
    except OSError:
        fail("SEMANTIC_EXECUTOR_RUN_DIR_INVALID", "Recovery quarantine is unavailable")
    finally:
        if quarantine_fd >= 0:
            primary_error_active = sys.exc_info()[0] is not None
            if not _close_descriptor(quarantine_fd) and not primary_error_active:
                fail("SEMANTIC_EXECUTOR_RUN_DIR_INVALID", "Recovery quarantine could not be closed")


def _entry_metadata_at(descriptor: int, name: str) -> os.stat_result | None:
    _safe_entry_name(name)
    try:
        return os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError:
        fail("SEMANTIC_EXECUTOR_RUN_DIR_INVALID", "Run-directory entry is unavailable")


def _entry_exists_at(handle: RunDirectoryHandle, name: str) -> bool:
    _assert_run_directory_binding(handle, require_lock=True)
    return _entry_metadata_at(handle.run_fd, name) is not None


def _recover_run_directory_at(handle: RunDirectoryHandle) -> None:
    """Repair only recognized interrupted writes while holding the run lock."""

    allowed = {
        "executor.lock",
        "execution-state.json",
        "terminal-receipt.json",
        ".recovery-quarantine",
    }
    _assert_run_directory_binding(handle, require_lock=True)
    try:
        entries = os.listdir(handle.run_fd)
    except OSError:
        fail("SEMANTIC_EXECUTOR_RUN_DIR_INVALID", "Run directory cannot be inspected")
    changed = False
    for name in entries:
        if name in allowed:
            continue
        match = re.fullmatch(
            r"\.(execution-state|terminal-receipt)\.json\.tmp-[A-Za-z0-9_-]+",
            name,
        )
        if match is None:
            fail("SEMANTIC_EXECUTOR_RUN_DIR_INVALID", "Run directory contains unexpected files")
        try:
            metadata = os.stat(name, dir_fd=handle.run_fd, follow_symlinks=False)
        except OSError:
            fail("SEMANTIC_EXECUTOR_RUN_DIR_INVALID", "Recovery evidence is unavailable")
        if not _safe_recovery_file(metadata) or metadata.st_nlink not in {1, 2}:
            fail("SEMANTIC_EXECUTOR_RUN_DIR_INVALID", "Recovery evidence has unsafe metadata")
        target_name = f"{match.group(1)}.json"
        if metadata.st_nlink == 2:
            try:
                target_metadata = os.stat(
                    target_name,
                    dir_fd=handle.run_fd,
                    follow_symlinks=False,
                )
            except OSError:
                fail("SEMANTIC_EXECUTOR_RUN_DIR_INVALID", "Interrupted linked write cannot be reconciled")
            if (
                not _safe_recovery_file(target_metadata)
                or target_metadata.st_nlink != 2
                or (target_metadata.st_dev, target_metadata.st_ino)
                != (metadata.st_dev, metadata.st_ino)
            ):
                fail("SEMANTIC_EXECUTOR_RUN_DIR_INVALID", "Interrupted linked write cannot be reconciled")
            try:
                os.unlink(name, dir_fd=handle.run_fd)
            except OSError:
                fail("SEMANTIC_EXECUTOR_RUN_DIR_INVALID", "Interrupted linked write could not be repaired")
            changed = True
        else:
            _quarantine_orphan_at(handle, name)
            changed = True
    if changed:
        try:
            _fsync_directory_fd(handle.run_fd)
        except OSError:
            fail("SEMANTIC_EXECUTOR_RUN_DIR_INVALID", "Recovered run directory is not durable")
    quarantine_metadata = _entry_metadata_at(handle.run_fd, ".recovery-quarantine")
    if quarantine_metadata is not None:
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            quarantine_fd = os.open(
                ".recovery-quarantine",
                directory_flags,
                dir_fd=handle.run_fd,
            )
        except OSError:
            fail("SEMANTIC_EXECUTOR_RUN_DIR_INVALID", "Recovery quarantine is unavailable")
        try:
            quarantine_opened = os.fstat(quarantine_fd)
            if (
                _object_identity(quarantine_opened) != _object_identity(quarantine_metadata)
                or not _private_directory(quarantine_opened, exact_mode=0o700)
            ):
                fail("SEMANTIC_EXECUTOR_RUN_DIR_INVALID", "Recovery quarantine binding changed")
            _validate_recovery_quarantine_at(quarantine_fd)
        finally:
            primary_error_active = sys.exc_info()[0] is not None
            if not _close_descriptor(quarantine_fd) and not primary_error_active:
                fail("SEMANTIC_EXECUTOR_RUN_DIR_INVALID", "Recovery quarantine could not be closed")
    try:
        remaining = set(os.listdir(handle.run_fd))
    except OSError:
        fail("SEMANTIC_EXECUTOR_RUN_DIR_INVALID", "Run directory cannot be inspected")
    if not remaining.issubset(allowed):
        fail("SEMANTIC_EXECUTOR_RUN_DIR_INVALID", "Run directory contains unexpected files")
    for name in ("execution-state.json", "terminal-receipt.json"):
        metadata = _entry_metadata_at(handle.run_fd, name)
        if metadata is None:
            continue
        if not _safe_recovery_file(metadata) or metadata.st_nlink != 1:
            fail("SEMANTIC_EXECUTOR_RUN_DIR_INVALID", "Executor evidence has unsafe metadata")
    _assert_run_directory_binding(handle, require_lock=True)


def _atomic_write_at(
    handle: RunDirectoryHandle,
    name: str,
    raw: bytes,
    *,
    replace: bool,
) -> None:
    name = _safe_entry_name(name)
    if not raw or len(raw) > MAX_JSON_BYTES:
        fail("SEMANTIC_EXECUTOR_STATE_INVALID", "Executor evidence exceeds its bound")
    _assert_run_directory_binding(handle, require_lock=True)
    existing = _entry_metadata_at(handle.run_fd, name)
    if existing is not None:
        if (
            not replace
            or not _private_evidence_file(existing)
        ):
            fail("SEMANTIC_EXECUTOR_STATE_INVALID", "Executor evidence cannot be replaced safely")
    descriptor = -1
    temporary_name: str | None = None
    committed = False
    try:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        for _attempt in range(8):
            candidate = f".{name}.tmp-{os.urandom(12).hex()}"
            try:
                descriptor = os.open(candidate, flags, 0o600, dir_fd=handle.run_fd)
                temporary_name = candidate
                break
            except FileExistsError:
                continue
        if descriptor < 0 or temporary_name is None:
            fail("SEMANTIC_EXECUTOR_EVIDENCE_WRITE_FAILED", "Executor evidence name could not be reserved")
        os.fchmod(descriptor, 0o600)
        if not _private_evidence_file(os.fstat(descriptor)):
            fail("SEMANTIC_EXECUTOR_EVIDENCE_WRITE_FAILED", "Executor evidence temporary file is unsafe")
        offset = 0
        while offset < len(raw):
            written = os.write(descriptor, raw[offset:])
            if written <= 0:
                raise OSError("short executor evidence write")
            offset += written
        os.fsync(descriptor)
        closing_descriptor = descriptor
        descriptor = -1
        os.close(closing_descriptor)
        _assert_run_directory_binding(handle, require_lock=True)
        if replace:
            os.replace(
                temporary_name,
                name,
                src_dir_fd=handle.run_fd,
                dst_dir_fd=handle.run_fd,
            )
            committed = True
            temporary_name = None
        else:
            try:
                _rename_no_replace_at(
                    handle.run_fd,
                    temporary_name,
                    handle.run_fd,
                    name,
                )
            except FileExistsError:
                fail("SEMANTIC_EXECUTOR_TERMINAL_EXISTS", "Terminal receipt already exists")
            committed = True
            temporary_name = None
        _fsync_directory_fd(handle.run_fd)
        _assert_run_directory_binding(handle, require_lock=True)
    except SemanticIndexExecutorError:
        raise
    except OSError:
        if committed:
            fail(
                "SEMANTIC_EXECUTOR_EVIDENCE_COMMITTED_NOT_DURABLE",
                "Executor evidence was committed but directory durability is uncertain",
                committed=True,
                durability_uncertain=True,
            )
        fail("SEMANTIC_EXECUTOR_EVIDENCE_WRITE_FAILED", "Executor evidence could not be written")
    finally:
        if descriptor >= 0:
            _close_descriptor(descriptor)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=handle.run_fd)
            except OSError:
                pass


def _secure_read_at(handle: RunDirectoryHandle, name: str, *, maximum: int) -> bytes:
    name = _safe_entry_name(name)
    _assert_run_directory_binding(handle, require_lock=True)
    before = _entry_metadata_at(handle.run_fd, name)
    if (
        before is None
        or not _private_evidence_file(before)
        or not 1 <= before.st_size <= maximum
    ):
        fail("SEMANTIC_EXECUTOR_STATE_INVALID", "Executor evidence has unsafe metadata")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=handle.run_fd)
    except OSError:
        fail("SEMANTIC_EXECUTOR_STATE_INVALID", "Executor evidence is unavailable")
    try:
        opened = os.fstat(descriptor)
        if _stable_metadata(opened) != _stable_metadata(before):
            fail("SEMANTIC_EXECUTOR_STATE_INVALID", "Executor evidence changed during validation")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        named_after = _entry_metadata_at(handle.run_fd, name)
        if (
            named_after is None
            or len(raw) != opened.st_size
            or len(raw) > maximum
            or _stable_metadata(after) != _stable_metadata(opened)
            or _stable_metadata(named_after) != _stable_metadata(opened)
        ):
            fail("SEMANTIC_EXECUTOR_STATE_INVALID", "Executor evidence changed during validation")
        _assert_run_directory_binding(handle, require_lock=True)
        return raw
    finally:
        primary_error_active = sys.exc_info()[0] is not None
        if not _close_descriptor(descriptor) and not primary_error_active:
            fail("SEMANTIC_EXECUTOR_STATE_INVALID", "Executor evidence descriptor could not be closed")


def _read_private_evidence_at(handle: RunDirectoryHandle, name: str) -> dict[str, Any]:
    raw = _secure_read_at(handle, name, maximum=MAX_JSON_BYTES)
    value = strict_json(raw)
    if canonical_json(value) != raw or not isinstance(value, dict):
        fail("SEMANTIC_EXECUTOR_STATE_INVALID", "Executor evidence is not canonical")
    return value


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _aware_utc(value: Any) -> dt.datetime:
    if (
        not isinstance(value, dt.datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        fail("SEMANTIC_EXECUTOR_CLOCK_INVALID", "Executor clock must be timezone-aware")
    return value.astimezone(dt.timezone.utc)


def _timestamp(value: dt.datetime) -> str:
    return _aware_utc(value).isoformat()


@dataclass(slots=True)
class _ExecutionWallClock:
    source: Callable[[], dt.datetime] = field(repr=False)
    last: dt.datetime | None = None

    def sample(self) -> dt.datetime:
        current = _aware_utc(self.source())
        if self.last is not None and current < self.last:
            fail(
                "SEMANTIC_EXECUTOR_CLOCK_INVALID",
                "Executor wall clock moved backwards during execution",
            )
        self.last = current
        return current


def _assert_total_deadline(
    *,
    wall_time: dt.datetime,
    wall_deadline: dt.datetime,
    monotonic_deadline_ns: int,
) -> None:
    if wall_time >= wall_deadline or time.monotonic_ns() >= monotonic_deadline_ns:
        fail(
            "SEMANTIC_EXECUTOR_TOTAL_TIMEOUT",
            "Execution exceeded its sealed total deadline",
        )


def _identity(bundle: ValidatedBundle, adapter: AdapterRuntime) -> dict[str, Any]:
    return {
        "job_revision": bundle.job["job_revision"],
        "job_sha256": bundle.job_sha256,
        "preparation_receipt_sha256": bundle.receipt_sha256,
        "execution_plan_sha256": bundle.plan_sha256,
        "executor_git_commit": bundle.observed_git_commit,
        "adapter_id": adapter.adapter_id,
        "adapter_contract_revision": adapter.adapter_contract_revision,
        "profile": bundle.plan["profile"],
    }


def _phase_key(bundle: ValidatedBundle, adapter: AdapterRuntime, phase: str) -> str:
    basis = {
        "schema": "simworld-semantic-index-phase-idempotency-key/v1",
        **_identity(bundle, adapter),
        "phase": phase,
    }
    return hashlib.sha256(canonical_json(basis, pretty=False)).hexdigest()


def _new_state(
    bundle: ValidatedBundle,
    adapter: AdapterRuntime,
    *,
    now: dt.datetime,
) -> dict[str, Any]:
    timeout = bundle.plan["limits"]["max_total_elapsed_seconds"]
    return {
        "schema": STATE_SCHEMA,
        "identity": _identity(bundle, adapter),
        "credential_bindings": copy.deepcopy(bundle.plan["credential_bindings"]),
        "phase_order": list(PHASES),
        "phases": {
            phase: {
                "status": "pending",
                "attempts": 0,
                "idempotency_key": _phase_key(bundle, adapter, phase),
                "result": None,
                "last_error_code": None,
            }
            for phase in PHASES
        },
        "started_at_utc": _timestamp(now),
        "deadline_at_utc": _timestamp(now + dt.timedelta(seconds=timeout)),
        "updated_at_utc": _timestamp(now),
        "terminal_receipt_sha256": None,
    }


def _validate_phase_result_document(
    value: Any,
    *,
    phase: str,
    idempotency_key: str,
    bundle: ValidatedBundle,
    adapter: AdapterRuntime,
) -> dict[str, Any]:
    result = _exact_object(
        value,
        {
            "phase", "idempotency_key", "metrics", "evidence_sha256",
            "observed_live", "snapshot_revision", "live_audit_receipt_sha256",
            "observed_runtime_images", "credential_target_identities",
        },
    )
    if result["phase"] != phase or result["idempotency_key"] != idempotency_key:
        fail("SEMANTIC_EXECUTOR_PHASE_RESULT_INVALID", "Adapter returned a result for the wrong phase")
    if not isinstance(result["metrics"], dict) or set(result["metrics"]) != set(PHASE_METRIC_KEYS[phase]):
        fail("SEMANTIC_EXECUTOR_PHASE_RESULT_INVALID", "Adapter returned unexpected phase metrics")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in result["metrics"].values()):
        fail("SEMANTIC_EXECUTOR_PHASE_RESULT_INVALID", "Adapter returned invalid phase metrics")
    _sha256(result["evidence_sha256"])
    if type(result["observed_live"]) is not bool:
        fail("SEMANTIC_EXECUTOR_PHASE_RESULT_INVALID", "Adapter live-observation state is invalid")
    if not adapter.production_capable and result["observed_live"] is not False:
        fail("SEMANTIC_EXECUTOR_PHASE_RESULT_INVALID", "A fixture adapter cannot claim live observations")
    if adapter.production_capable and result["observed_live"] is not True:
        fail("SEMANTIC_EXECUTOR_RECONCILIATION_FAILED", "A production phase was not observed live")
    expected_targets = {
        label: binding["target_identity"]
        for label, binding in bundle.plan["credential_bindings"].items()
    }
    if (
        not isinstance(result["credential_target_identities"], dict)
        or result["credential_target_identities"] != expected_targets
    ):
        fail(
            "SEMANTIC_EXECUTOR_CREDENTIAL_TARGET_MISMATCH",
            "Adapter credential targets differ from the sealed execution target",
        )
    count = bundle.job["pending_objects"]["count"]
    views = bundle.plan["limits"]["expected_rendered_views"]
    expected_metrics = {
        "inspect": {"assets_inspected": count},
        "render": {"assets_rendered": count, "rendered_views": views},
        "caption": {"catalog_records": count},
        "embed": {"dense_vectors": count, "sparse_vectors": count},
        "postgres": {"postgres_rows": count},
        "qdrant": {"qdrant_points": count},
        "reconcile": {"catalog_records": count, "postgres_rows": count, "qdrant_points": count},
    }[phase]
    if result["metrics"] != expected_metrics:
        fail("SEMANTIC_EXECUTOR_RECONCILIATION_FAILED", "Phase counts do not reconcile with the sealed pending set")
    snapshot_revision = bundle.job["snapshot_target"]["asset_snapshot_revision"]
    if phase == "reconcile":
        if result["snapshot_revision"] != snapshot_revision:
            fail("SEMANTIC_EXECUTOR_RECONCILIATION_FAILED", "Snapshot revision does not reconcile")
        receipt_hash = result["live_audit_receipt_sha256"]
        if adapter.production_capable:
            _sha256(receipt_hash)
            if result["observed_live"] is not True:
                fail("SEMANTIC_EXECUTOR_RECONCILIATION_FAILED", "Production reconciliation was not observed live")
            if result["observed_runtime_images"] != bundle.plan["runtime_images"]:
                fail("SEMANTIC_EXECUTOR_RECONCILIATION_FAILED", "Observed runtime images differ from the sealed image pins")
        elif receipt_hash is not None or result["observed_runtime_images"] is not None:
            fail("SEMANTIC_EXECUTOR_PHASE_RESULT_INVALID", "Fixture reconciliation cannot claim live evidence")
    elif (
        result["snapshot_revision"] is not None
        or result["live_audit_receipt_sha256"] is not None
        or result["observed_runtime_images"] is not None
    ):
        fail("SEMANTIC_EXECUTOR_PHASE_RESULT_INVALID", "Only reconciliation may report snapshot evidence")
    return result


def _phase_result_dict(result: Any, adapter: AdapterRuntime) -> dict[str, Any]:
    if type(result) is not adapter.phase_result_type:
        fail("SEMANTIC_EXECUTOR_PHASE_RESULT_INVALID", "Adapter returned an unsupported result type")
    return {
        "phase": result.phase,
        "idempotency_key": result.idempotency_key,
        "metrics": dict(result.metrics),
        "evidence_sha256": result.evidence_sha256,
        "observed_live": result.observed_live,
        "snapshot_revision": result.snapshot_revision,
        "live_audit_receipt_sha256": result.live_audit_receipt_sha256,
        "observed_runtime_images": (
            dict(result.observed_runtime_images)
            if result.observed_runtime_images is not None
            else None
        ),
        "credential_target_identities": dict(result.credential_target_identities),
    }


def _validate_state(
    state: Any,
    bundle: ValidatedBundle,
    adapter: AdapterRuntime,
) -> dict[str, Any]:
    state = _exact_object(
        state,
        {
            "schema", "identity", "credential_bindings", "phase_order", "phases",
            "started_at_utc", "deadline_at_utc", "updated_at_utc",
            "terminal_receipt_sha256",
        },
    )
    if (
        state["schema"] != STATE_SCHEMA
        or state["identity"] != _identity(bundle, adapter)
        or state["credential_bindings"] != bundle.plan["credential_bindings"]
        or state["phase_order"] != list(PHASES)
        or not isinstance(state["phases"], dict)
        or set(state["phases"]) != set(PHASES)
    ):
        fail("SEMANTIC_EXECUTOR_STATE_MISMATCH", "Existing run state belongs to a different execution")
    started = _rfc3339(state["started_at_utc"])
    deadline = _rfc3339(state["deadline_at_utc"])
    updated = _rfc3339(state["updated_at_utc"])
    expected_deadline = started + dt.timedelta(
        seconds=bundle.plan["limits"]["max_total_elapsed_seconds"]
    )
    if updated < started or deadline != expected_deadline:
        fail("SEMANTIC_EXECUTOR_STATE_INVALID", "Existing run timestamps are invalid")
    first_incomplete_seen = False
    active_incomplete_seen = False
    for phase in PHASES:
        phase_state = _exact_object(
            state["phases"][phase],
            {"status", "attempts", "idempotency_key", "result", "last_error_code"},
        )
        if phase_state["status"] not in {"pending", "running", "completed", "failed"}:
            fail("SEMANTIC_EXECUTOR_STATE_INVALID", "Existing phase state is invalid")
        if isinstance(phase_state["attempts"], bool) or not isinstance(phase_state["attempts"], int) or not 0 <= phase_state["attempts"] <= 1_000_000:
            fail("SEMANTIC_EXECUTOR_STATE_INVALID", "Existing phase attempt count is invalid")
        if phase_state["status"] == "completed":
            if first_incomplete_seen or phase_state["attempts"] < 1:
                fail("SEMANTIC_EXECUTOR_STATE_INVALID", "Completed phases must form one ordered prefix")
        else:
            if phase_state["status"] in {"running", "failed"}:
                if active_incomplete_seen or first_incomplete_seen:
                    fail("SEMANTIC_EXECUTOR_STATE_INVALID", "Only the first incomplete phase may have started")
                active_incomplete_seen = True
            first_incomplete_seen = True
            if (
                (phase_state["status"] == "pending" and phase_state["attempts"] != 0)
                or (phase_state["status"] in {"running", "failed"} and phase_state["attempts"] < 1)
            ):
                fail("SEMANTIC_EXECUTOR_STATE_INVALID", "Phase attempts do not match phase status")
        if phase_state["idempotency_key"] != _phase_key(bundle, adapter, phase):
            fail("SEMANTIC_EXECUTOR_STATE_MISMATCH", "Existing phase identity differs")
        if phase_state["status"] == "completed":
            _validate_phase_result_document(
                phase_state["result"],
                phase=phase,
                idempotency_key=phase_state["idempotency_key"],
                bundle=bundle,
                adapter=adapter,
            )
            if phase_state["last_error_code"] is not None:
                fail("SEMANTIC_EXECUTOR_STATE_INVALID", "Completed phase contains an error")
        elif phase_state["result"] is not None:
            fail("SEMANTIC_EXECUTOR_STATE_INVALID", "Incomplete phase contains a result")
        if phase_state["last_error_code"] is not None and (
            not isinstance(phase_state["last_error_code"], str)
            or re.fullmatch(r"SEMANTIC_EXECUTOR_[A-Z0-9_]{1,96}", phase_state["last_error_code"]) is None
        ):
            fail("SEMANTIC_EXECUTOR_STATE_INVALID", "Existing phase error code is invalid")
    if state["terminal_receipt_sha256"] is not None:
        _sha256(state["terminal_receipt_sha256"])
    return state


class _PhaseAlarm(BaseException):
    pass


def _alarm_handler(_signum: int, _frame: FrameType | None) -> None:
    raise _PhaseAlarm("phase deadline expired")


def _invoke_phase(
    adapter: AdapterRuntime,
    request: Any,
    *,
    timeout_seconds: float,
) -> Any:
    if timeout_seconds <= 0:
        fail("SEMANTIC_EXECUTOR_TIMEOUT_INVALID", "Phase timeout is invalid")
    if threading.current_thread() is not threading.main_thread():
        fail("SEMANTIC_EXECUTOR_TIMEOUT_UNAVAILABLE", "Phase deadlines require the main executor thread")
    previous = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    if previous_timer != (0.0, 0.0):
        fail("SEMANTIC_EXECUTOR_TIMEOUT_UNAVAILABLE", "Another deadline already owns the executor timer")
    try:
        signal.signal(signal.SIGALRM, _alarm_handler)
        signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
        result = adapter.run_phase(request)
        if time.monotonic_ns() >= request.deadline_monotonic_ns:
            raise _PhaseAlarm("phase deadline expired")
        return result
    except (_PhaseAlarm, TimeoutError):
        fail("SEMANTIC_EXECUTOR_PHASE_TIMEOUT", "Adapter phase exceeded its reviewed deadline", retryable=True)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous)


def _terminal_receipt(
    bundle: ValidatedBundle,
    adapter: AdapterRuntime,
    state: Mapping[str, Any],
    *,
    completed_at: dt.datetime,
) -> dict[str, Any]:
    phase_results = [state["phases"][phase]["result"] for phase in PHASES]
    if any(result is None for result in phase_results):
        fail("SEMANTIC_EXECUTOR_STATE_INVALID", "Cannot issue a terminal receipt before every phase completes")
    reconcile = phase_results[-1]
    production_complete = bool(
        adapter.production_capable
        and bundle.plan["profile"] == "production"
        and all(result["observed_live"] is True for result in phase_results)
        and reconcile["live_audit_receipt_sha256"] is not None
    )
    return {
        "schema": TERMINAL_RECEIPT_SCHEMA,
        "status": "production_complete" if production_complete else "offline_fixture_complete",
        "identity": _identity(bundle, adapter),
        "phase_order": list(PHASES),
        "phase_results": phase_results,
        "asset_snapshot_revision": bundle.job["snapshot_target"]["asset_snapshot_revision"],
        "pending_object_count": bundle.job["pending_objects"]["count"],
        "approval_ref_sha256": bundle.plan["approval_ref_sha256"],
        "started_at_utc": state["started_at_utc"],
        "completed_at_utc": _timestamp(completed_at),
        "credential_file_labels": list(bundle.plan["credential_files_required"]),
        "legacy_runner_used": False,
        "shell_used": False,
        "production_complete": production_complete,
        "catalog_complete": production_complete,
        "snapshot_complete": production_complete,
        "live_audit_receipt_sha256": (
            reconcile["live_audit_receipt_sha256"] if production_complete else None
        ),
    }


def _validate_terminal_receipt(
    receipt: Any,
    bundle: ValidatedBundle,
    adapter: AdapterRuntime,
    state: Mapping[str, Any],
) -> dict[str, Any]:
    receipt = _exact_object(
        receipt,
        {
            "schema", "status", "identity", "phase_order", "phase_results",
            "asset_snapshot_revision", "pending_object_count", "approval_ref_sha256",
            "started_at_utc", "completed_at_utc", "credential_file_labels",
            "legacy_runner_used", "shell_used", "production_complete", "catalog_complete",
            "snapshot_complete", "live_audit_receipt_sha256",
        },
    )
    if (
        receipt["schema"] != TERMINAL_RECEIPT_SCHEMA
        or receipt["identity"] != _identity(bundle, adapter)
        or receipt["phase_order"] != list(PHASES)
        or not isinstance(receipt["phase_results"], list)
        or len(receipt["phase_results"]) != len(PHASES)
        or receipt["asset_snapshot_revision"] != bundle.job["snapshot_target"]["asset_snapshot_revision"]
        or type(receipt["pending_object_count"]) is not int
        or receipt["pending_object_count"] != bundle.job["pending_objects"]["count"]
        or receipt["approval_ref_sha256"] != bundle.plan["approval_ref_sha256"]
        or receipt["started_at_utc"] != state["started_at_utc"]
        or receipt["credential_file_labels"] != bundle.plan["credential_files_required"]
        or receipt["legacy_runner_used"] is not False
        or receipt["shell_used"] is not False
    ):
        fail("SEMANTIC_EXECUTOR_TERMINAL_INVALID", "Terminal receipt does not match this execution")
    started = _rfc3339(receipt["started_at_utc"])
    completed = _rfc3339(receipt["completed_at_utc"])
    updated = _rfc3339(state["updated_at_utc"])
    deadline = _rfc3339(state["deadline_at_utc"])
    if (
        completed < started
        or completed >= deadline
        or (
            state["terminal_receipt_sha256"] is None
            and completed < updated
        )
        or (
            state["terminal_receipt_sha256"] is not None
            and updated < completed
        )
    ):
        fail("SEMANTIC_EXECUTOR_TERMINAL_INVALID", "Terminal timestamps are invalid")
    for phase, result in zip(PHASES, receipt["phase_results"], strict=True):
        validated = _validate_phase_result_document(
            result,
            phase=phase,
            idempotency_key=_phase_key(bundle, adapter, phase),
            bundle=bundle,
            adapter=adapter,
        )
        if validated != state["phases"][phase]["result"]:
            fail("SEMANTIC_EXECUTOR_TERMINAL_INVALID", "Terminal phase result differs from durable state")
    expected_production = bool(
        adapter.production_capable
        and bundle.plan["profile"] == "production"
        and all(result["observed_live"] is True for result in receipt["phase_results"])
        and receipt["phase_results"][-1]["live_audit_receipt_sha256"] is not None
    )
    if (
        receipt["production_complete"] is not expected_production
        or receipt["catalog_complete"] is not expected_production
        or receipt["snapshot_complete"] is not expected_production
        or receipt["status"] != ("production_complete" if expected_production else "offline_fixture_complete")
        or receipt["live_audit_receipt_sha256"]
        != (receipt["phase_results"][-1]["live_audit_receipt_sha256"] if expected_production else None)
    ):
        fail("SEMANTIC_EXECUTOR_TERMINAL_INVALID", "Terminal completion claim is invalid")
    return receipt


def execute_bundle(
    *,
    bundle: ValidatedBundle,
    requested_adapter_id: str,
    run_dir: Path,
    approval_ref: str,
    secret_paths: SecretPathArguments,
    now: Callable[[], dt.datetime] = _now_utc,
) -> dict[str, Any]:
    """Execute/resume using only the internally loaded sealed adapter registry."""

    _require_isolated_python()
    adapter = _load_adapter_runtime(bundle, requested_adapter_id=requested_adapter_id)
    secret_snapshot = _load_secret_snapshot(bundle.plan, secret_paths, adapter)
    return _execute_with_adapter(
        bundle=bundle,
        adapter=adapter,
        run_dir=run_dir,
        approval_ref=approval_ref,
        secret_snapshot=secret_snapshot,
        now=now,
    )


def _execute_with_adapter(
    *,
    bundle: ValidatedBundle,
    adapter: AdapterRuntime,
    run_dir: Path,
    approval_ref: str,
    secret_snapshot: Any,
    now: Callable[[], dt.datetime] = _now_utc,
) -> dict[str, Any]:
    if _approval_hash(approval_ref) != bundle.plan["approval_ref_sha256"]:
        fail("SEMANTIC_EXECUTOR_APPROVAL_MISMATCH", "Approval reference differs from the sealed plan")
    wall_clock = _ExecutionWallClock(now)
    with _open_run_directory(run_dir, create=True) as run_handle, _run_lock(run_handle):
        _assert_run_directory_binding(run_handle, require_lock=True)
        _recover_run_directory_at(run_handle)
        process_anchor_monotonic_ns = time.monotonic_ns()
        current_time = wall_clock.sample()
        if _entry_exists_at(run_handle, "execution-state.json"):
            state = _validate_state(
                _read_private_evidence_at(run_handle, "execution-state.json"),
                bundle,
                adapter,
            )
        else:
            state = _new_state(bundle, adapter, now=current_time)
            _atomic_write_at(
                run_handle,
                "execution-state.json",
                canonical_json(state),
                replace=False,
            )
        state_started = _rfc3339(state["started_at_utc"])
        state_updated = _rfc3339(state["updated_at_utc"])
        if current_time < state_started or current_time < state_updated:
            fail(
                "SEMANTIC_EXECUTOR_CLOCK_INVALID",
                "Executor clock predates durable run state",
            )

        if _entry_exists_at(run_handle, "terminal-receipt.json"):
            terminal_raw = _secure_read_at(
                run_handle,
                "terminal-receipt.json",
                maximum=MAX_JSON_BYTES,
            )
            terminal_value = strict_json(terminal_raw)
            if canonical_json(terminal_value) != terminal_raw:
                fail("SEMANTIC_EXECUTOR_TERMINAL_INVALID", "Terminal receipt is not canonical")
            terminal = _validate_terminal_receipt(terminal_value, bundle, adapter, state)
            if current_time < _rfc3339(terminal["completed_at_utc"]):
                fail(
                    "SEMANTIC_EXECUTOR_CLOCK_INVALID",
                    "Executor clock predates durable terminal evidence",
                )
            terminal_sha256 = hashlib.sha256(terminal_raw).hexdigest()
            if state["terminal_receipt_sha256"] not in {None, terminal_sha256}:
                fail("SEMANTIC_EXECUTOR_TERMINAL_INVALID", "Terminal receipt digest differs from state")
            if state["terminal_receipt_sha256"] is None:
                state["terminal_receipt_sha256"] = terminal_sha256
                state["updated_at_utc"] = _timestamp(current_time)
                _validate_state(copy.deepcopy(state), bundle, adapter)
                _validate_terminal_receipt(
                    copy.deepcopy(terminal),
                    bundle,
                    adapter,
                    state,
                )
                _atomic_write_at(
                    run_handle,
                    "execution-state.json",
                    canonical_json(state),
                    replace=True,
                )
            _assert_run_directory_binding(run_handle, require_lock=True)
            return terminal

        if state["terminal_receipt_sha256"] is not None:
            fail("SEMANTIC_EXECUTOR_TERMINAL_INVALID", "State names a missing terminal receipt")
        deadline = _rfc3339(state["deadline_at_utc"])
        if current_time >= deadline:
            fail("SEMANTIC_EXECUTOR_TOTAL_TIMEOUT", "Execution exceeded its sealed total deadline", retryable=False)
        remaining_total_ns = int((deadline - current_time).total_seconds() * 1_000_000_000)
        if remaining_total_ns <= 0:
            fail("SEMANTIC_EXECUTOR_TOTAL_TIMEOUT", "Execution exceeded its sealed total deadline")
        total_deadline_monotonic_ns = process_anchor_monotonic_ns + remaining_total_ns

        phase_timeout = bundle.plan["limits"]["phase_timeout_seconds"]
        for phase in PHASES:
            phase_state = state["phases"][phase]
            if phase_state["status"] == "completed":
                continue
            phase_started_at = wall_clock.sample()
            _assert_total_deadline(
                wall_time=phase_started_at,
                wall_deadline=deadline,
                monotonic_deadline_ns=total_deadline_monotonic_ns,
            )
            phase_state["status"] = "running"
            phase_state["attempts"] += 1
            phase_state["result"] = None
            phase_state["last_error_code"] = None
            state["updated_at_utc"] = _timestamp(phase_started_at)
            _atomic_write_at(
                run_handle,
                "execution-state.json",
                canonical_json(state),
                replace=True,
            )
            try:
                phase_call_started_ns = time.monotonic_ns()
                phase_deadline_monotonic_ns = min(
                    total_deadline_monotonic_ns,
                    phase_call_started_ns + phase_timeout * 1_000_000_000,
                )
                if phase_call_started_ns >= phase_deadline_monotonic_ns:
                    fail("SEMANTIC_EXECUTOR_TOTAL_TIMEOUT", "Execution exceeded its sealed total deadline")
                effective_timeout = (
                    phase_deadline_monotonic_ns - phase_call_started_ns
                ) / 1_000_000_000
                request = adapter.phase_request_type(
                    phase=phase,
                    job=copy.deepcopy(bundle.job),
                    execution_plan=copy.deepcopy(bundle.plan),
                    idempotency_key=phase_state["idempotency_key"],
                    deadline_monotonic_ns=phase_deadline_monotonic_ns,
                    secret_snapshot=secret_snapshot,
                )
                _assert_run_directory_binding(run_handle, require_lock=True)
                adapter_result = _invoke_phase(adapter, request, timeout_seconds=effective_timeout)
                _assert_run_directory_binding(run_handle, require_lock=True)
                phase_completed_at = wall_clock.sample()
                _assert_total_deadline(
                    wall_time=phase_completed_at,
                    wall_deadline=deadline,
                    monotonic_deadline_ns=total_deadline_monotonic_ns,
                )
                result_document = _validate_phase_result_document(
                    _phase_result_dict(adapter_result, adapter),
                    phase=phase,
                    idempotency_key=phase_state["idempotency_key"],
                    bundle=bundle,
                    adapter=adapter,
                )
            except SemanticIndexExecutorError as error:
                failure_time = wall_clock.sample()
                if error.code == "SEMANTIC_EXECUTOR_PHASE_TIMEOUT" and (
                    failure_time >= deadline
                    or time.monotonic_ns() >= total_deadline_monotonic_ns
                ):
                    error = SemanticIndexExecutorError(
                        "SEMANTIC_EXECUTOR_TOTAL_TIMEOUT",
                        "Execution exceeded its sealed total deadline",
                    )
                phase_state["status"] = "failed"
                phase_state["last_error_code"] = error.code
                state["updated_at_utc"] = _timestamp(failure_time)
                _atomic_write_at(
                    run_handle,
                    "execution-state.json",
                    canonical_json(state),
                    replace=True,
                )
                raise error
            except Exception:
                failure_time = wall_clock.sample()
                phase_state["status"] = "failed"
                phase_state["last_error_code"] = "SEMANTIC_EXECUTOR_ADAPTER_FAILED"
                state["updated_at_utc"] = _timestamp(failure_time)
                _atomic_write_at(
                    run_handle,
                    "execution-state.json",
                    canonical_json(state),
                    replace=True,
                )
                fail("SEMANTIC_EXECUTOR_ADAPTER_FAILED", "Adapter phase failed without a safe result", retryable=True)
            phase_state["status"] = "completed"
            phase_state["result"] = result_document
            phase_state["last_error_code"] = None
            state["updated_at_utc"] = _timestamp(phase_completed_at)
            _atomic_write_at(
                run_handle,
                "execution-state.json",
                canonical_json(state),
                replace=True,
            )

        completed_at = wall_clock.sample()
        _assert_total_deadline(
            wall_time=completed_at,
            wall_deadline=deadline,
            monotonic_deadline_ns=total_deadline_monotonic_ns,
        )
        terminal = _terminal_receipt(bundle, adapter, state, completed_at=completed_at)
        terminal = _validate_terminal_receipt(
            copy.deepcopy(terminal),
            bundle,
            adapter,
            state,
        )
        terminal_raw = canonical_json(terminal)
        _atomic_write_at(
            run_handle,
            "terminal-receipt.json",
            terminal_raw,
            replace=False,
        )
        state["terminal_receipt_sha256"] = hashlib.sha256(terminal_raw).hexdigest()
        state["updated_at_utc"] = _timestamp(completed_at)
        _validate_state(copy.deepcopy(state), bundle, adapter)
        _atomic_write_at(
            run_handle,
            "execution-state.json",
            canonical_json(state),
            replace=True,
        )
        _assert_run_directory_binding(run_handle, require_lock=True)
        return terminal


class SafeParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        fail("SEMANTIC_EXECUTOR_ARGUMENT_INVALID", "Required executor arguments are missing or invalid")


def build_parser() -> argparse.ArgumentParser:
    parser = SafeParser(description="Validate or explicitly execute a sealed semantic-index job")
    parser.add_argument("--job", required=True)
    parser.add_argument("--preparation-receipt", required=True)
    parser.add_argument("--execution-plan", required=True)
    parser.add_argument("--expected-job-sha256", required=True)
    parser.add_argument("--expected-preparation-receipt-sha256", required=True)
    parser.add_argument("--expected-execution-plan-sha256", required=True)
    parser.add_argument("--expected-job-schema-sha256", required=True)
    parser.add_argument("--expected-execution-plan-schema-sha256", required=True)
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--adapter", required=True)
    parser.add_argument("--run-dir")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--approval-ref", default="")
    parser.add_argument("--caption-secret-file")
    parser.add_argument("--postgres-secret-file")
    parser.add_argument("--qdrant-secret-file")
    parser.add_argument("--embedding-secret-file")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    os.umask(0o077)
    try:
        args = build_parser().parse_args(argv)
        if args.execute and (not args.run_dir or not args.approval_ref):
            fail("SEMANTIC_EXECUTOR_ARGUMENT_INVALID", "--execute requires --run-dir and --approval-ref")
        if args.execute:
            _require_isolated_python()
        if not args.execute and (args.run_dir or args.approval_ref):
            fail("SEMANTIC_EXECUTOR_ARGUMENT_INVALID", "Dry run does not accept output or approval arguments")
        bundle = validate_bundle(
            job_path=Path(args.job),
            preparation_receipt_path=Path(args.preparation_receipt),
            execution_plan_path=Path(args.execution_plan),
            expected_job_sha256=args.expected_job_sha256,
            expected_preparation_receipt_sha256=args.expected_preparation_receipt_sha256,
            expected_execution_plan_sha256=args.expected_execution_plan_sha256,
            expected_job_schema_sha256=args.expected_job_schema_sha256,
            expected_execution_plan_schema_sha256=args.expected_execution_plan_schema_sha256,
            repo_root=Path(args.repo_root),
        )
        _validate_requested_adapter_plan(bundle, requested_adapter_id=args.adapter)
        secret_paths = SecretPathArguments(
            caption=args.caption_secret_file,
            postgres=args.postgres_secret_file,
            qdrant=args.qdrant_secret_file,
            embedding=args.embedding_secret_file,
        )
        _validate_secret_path_contract(bundle.plan, secret_paths)
        if args.execute:
            terminal = execute_bundle(
                bundle=bundle,
                requested_adapter_id=args.adapter,
                run_dir=Path(args.run_dir),
                approval_ref=args.approval_ref,
                secret_paths=secret_paths,
            )
            result = {
                "schema": RESULT_SCHEMA,
                "valid": True,
                "status": terminal["status"],
                "job_revision": bundle.job["job_revision"],
                "terminal_receipt_sha256": hashlib.sha256(canonical_json(terminal)).hexdigest(),
                "production_complete": terminal["production_complete"],
                "catalog_complete": terminal["catalog_complete"],
                "snapshot_complete": terminal["snapshot_complete"],
            }
        else:
            result = dry_run_result(bundle)
        print(compact_json(result))
        return 0
    except SemanticIndexExecutorError as error:
        print(
            compact_json(
                {
                    "schema": RESULT_SCHEMA,
                    "valid": False,
                    "status": "failed",
                    "error": error.public_dict(),
                }
            ),
            file=sys.stderr,
        )
        return 2
    except Exception:
        print(
            compact_json(
                {
                    "schema": RESULT_SCHEMA,
                    "valid": False,
                    "status": "failed",
                    "error": {
                        "code": "SEMANTIC_EXECUTOR_INTERNAL_ERROR",
                        "message": "Executor failed before a safe result was produced",
                    },
                }
            ),
            file=sys.stderr,
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
