#!/usr/bin/env python3
"""Stage one verified VISTA selection as a curated Studio importer bundle.

This adapter is deliberately offline and path-explicit.  It accepts only the
documented verified-source projection, reads only operator-selected regular
files below one dataset root, and defaults to a read-only validation run.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import math
import os
import re
import shutil
import stat
import struct
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterator, Mapping, Sequence

import yaml
from yaml.tokens import AliasToken, AnchorToken, DirectiveToken, TagToken


VERIFIED_MANIFEST_SCHEMA = "vista-verified-source-manifest/v1"
VERIFIED_RECORD_SCHEMA = "vista-verified-sample/v1"
SOURCE_SCHEMA = "vista-import-source/v1"
DIALOGUE_SCHEMA = "vista-dialogue-no-oracle/v1"
MEDIA_SCHEMA = "vista-media-reference/v1"
REPORT_SCHEMA = "vista-import-staging-validation/v1"
RESULT_SCHEMA = "vista-import-staging-result/v1"
BUNDLE_DIGEST_ALGORITHM = "vista-import-bundle-tree-sha256/v1"

MAX_VERIFIED_SOURCE_BYTES = 16 * 1024 * 1024
MAX_RENDER_SCRIPT_BYTES = 8 * 1024 * 1024
MAX_DIALOGUE_BYTES = 8 * 1024 * 1024
MAX_MEDIA_BYTES = 1024 * 1024 * 1024
MAX_JSON_NODES = 50_000
MAX_JSON_DEPTH = 48
MAX_RECORDS = 10_000
MAX_MP4_BOXES = 20_000

SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SAFE_SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,511}$")
SAFE_VISUAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
SAFE_PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
SCENARIO_TYPES = frozenset(
    {
        "multimodal_grounded",
        "contradictory",
        "video_grounded_ontopic",
        "video_grounded_offtopic",
        "text_grounded",
        "no_assistance",
    }
)
RESTRICTED_KEY_RE = re.compile(
    r"^(?:oracle(?:_|$)|ground[_-]?truth|target(?:_|$)|target[_-]?label|label(?:_|$)|"
    r"answer(?:_|$)|review(?:_|$)|review[_-]?(?:note|decision)|seed(?:_|$)|"
    r"visible[_-]?evidence|dialogue[_-]?evidence|evidence[_-]?atom|assist[_-]?steps?|"
    r"intervention(?:_|$)|issue[_-]?summary|prediction(?:_|$))",
    re.IGNORECASE,
)
RECONSTRUCTION_RESTRICTED_KEY_RE = re.compile(
    r"^(?:oracle(?:_|$)|ground[_-]?truth|target[_-]?label|review(?:_|$)|"
    r"review[_-]?(?:note|decision)|seed(?:_|$)|visible[_-]?evidence|"
    r"dialogue[_-]?evidence|evidence[_-]?atom|assist[_-]?steps?|"
    r"intervention(?:_|$)|issue[_-]?summary|prediction(?:_|$))",
    re.IGNORECASE,
)
TIMESTAMP_ACTION_RE = re.compile(r"^\[\s*([^\]]+)\s*\]\s*(.+)$", re.DOTALL)
SECONDS_ACTION_RE = re.compile(
    r"^(\d+(?:\.\d+)?)\s*s(?:ec(?:onds?)?)?\s*[:\-]\s*(.+)$",
    re.IGNORECASE | re.DOTALL,
)


class VistaStagingError(RuntimeError):
    """A typed, safe-to-report staging validation failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        pointer: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.pointer = pointer
        self.details = dict(details or {})

    def public_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.pointer:
            result["pointer"] = self.pointer
        if self.details:
            result["details"] = self.details
        return result


def fail(
    code: str,
    message: str,
    *,
    pointer: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> None:
    raise VistaStagingError(code, message, pointer=pointer, details=details)


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def require_object(value: Any, pointer: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        fail("VISTA_STAGING_SCHEMA_INVALID", "Expected an object", pointer=pointer)
    return value


def exact_keys(
    value: Any,
    *,
    allowed: Sequence[str],
    required: Sequence[str],
    pointer: str,
) -> dict[str, Any]:
    obj = require_object(value, pointer)
    allowed_set = set(allowed)
    unknown = sorted(key for key in obj if key not in allowed_set)
    if unknown:
        fail(
            "VISTA_STAGING_SCHEMA_INVALID",
            "Object contains unsupported fields",
            pointer=pointer,
            details={"fields": unknown},
        )
    missing = [key for key in required if key not in obj]
    if missing:
        fail(
            "VISTA_STAGING_SCHEMA_INVALID",
            "Object is missing required fields",
            pointer=pointer,
            details={"fields": missing},
        )
    return obj


def require_string(
    value: Any,
    pointer: str,
    *,
    pattern: re.Pattern[str] | None = None,
    maximum: int = 16_384,
) -> str:
    if not isinstance(value, str):
        fail("VISTA_STAGING_SCHEMA_INVALID", "Expected a string", pointer=pointer)
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        fail("VISTA_STAGING_SCHEMA_INVALID", "String is empty or too long", pointer=pointer)
    if pattern and not pattern.fullmatch(normalized):
        fail("VISTA_STAGING_SCHEMA_INVALID", "String has an invalid format", pointer=pointer)
    return normalized


def require_number(
    value: Any,
    pointer: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        fail("VISTA_STAGING_SCHEMA_INVALID", "Expected a finite number", pointer=pointer)
    number = float(value)
    if not math.isfinite(number) or number < minimum or number > maximum:
        fail("VISTA_STAGING_SCHEMA_INVALID", "Number is outside its allowed range", pointer=pointer)
    return number


def guard_tree(value: Any, pointer: str = "$", depth: int = 0, state: list[int] | None = None) -> None:
    counter = state if state is not None else [0]
    counter[0] += 1
    if counter[0] > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
        fail("VISTA_STAGING_SOURCE_TOO_COMPLEX", "Structured source exceeds safety limits", pointer=pointer)
    if isinstance(value, list):
        for index, item in enumerate(value):
            guard_tree(item, f"{pointer}/{index}", depth + 1, counter)
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or key in {"__proto__", "prototype", "constructor"}:
                fail("VISTA_STAGING_SCHEMA_INVALID", "Object contains an unsafe key", pointer=pointer)
            guard_tree(item, f"{pointer}/{key}", depth + 1, counter)
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        fail("VISTA_STAGING_SCHEMA_INVALID", "Structured source contains an unsupported value", pointer=pointer)


def assert_no_restricted_fields(
    value: Any,
    pointer: str,
    *,
    pattern: re.Pattern[str] = RESTRICTED_KEY_RE,
) -> None:
    if isinstance(value, list):
        for index, item in enumerate(value):
            assert_no_restricted_fields(item, f"{pointer}/{index}", pattern=pattern)
        return
    if not isinstance(value, dict):
        return
    for key, item in value.items():
        if pattern.match(key):
            fail(
                "VISTA_STAGING_RESTRICTED_FIELD",
                "Restricted oracle, review, seed, or visible-evidence field is not accepted",
                pointer=f"{pointer}/{key}",
            )
        assert_no_restricted_fields(item, f"{pointer}/{key}", pattern=pattern)


def safe_relative_path(value: str, pointer: str) -> str:
    candidate = require_string(value, pointer, maximum=512)
    if "\x00" in candidate or "\\" in candidate or candidate.startswith("/"):
        fail("VISTA_STAGING_PATH_INVALID", "Path must be relative POSIX syntax", pointer=pointer)
    pure = PurePosixPath(candidate)
    if candidate != pure.as_posix() or any(part in {"", ".", ".."} for part in pure.parts):
        fail("VISTA_STAGING_PATH_INVALID", "Path cannot escape or alias the dataset root", pointer=pointer)
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", candidate):
        fail("VISTA_STAGING_PATH_INVALID", "URI-like paths are not accepted", pointer=pointer)
    return candidate


def _reject_symlink_components(path: Path, pointer: str) -> None:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            fail("VISTA_STAGING_SOURCE_UNAVAILABLE", "Required path does not exist", pointer=pointer)
        if stat.S_ISLNK(metadata.st_mode):
            fail("VISTA_STAGING_SYMLINK_REJECTED", "Symlinks are not allowed", pointer=pointer)


@dataclass(frozen=True)
class FileEvidence:
    relative_path: str
    sha256: str
    bytes: int
    device: int
    inode: int
    mtime_ns: int

    def public_dict(self) -> dict[str, Any]:
        return {
            "path": self.relative_path,
            "sha256": self.sha256,
            "bytes": self.bytes,
        }


class DatasetRoot:
    """Explicit, no-symlink reader for one operator-selected dataset root."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(os.path.abspath(os.fspath(root)))
        _reject_symlink_components(self.root, "--dataset-root")
        metadata = os.lstat(self.root)
        if not stat.S_ISDIR(metadata.st_mode):
            fail("VISTA_STAGING_DATASET_ROOT_INVALID", "Dataset root must be a directory", pointer="--dataset-root")

    def path(self, relative: str, pointer: str) -> Path:
        safe = safe_relative_path(relative, pointer)
        target = self.root.joinpath(*PurePosixPath(safe).parts)
        _reject_symlink_components(target, pointer)
        try:
            target.relative_to(self.root)
        except ValueError:
            fail("VISTA_STAGING_PATH_INVALID", "Path escapes the dataset root", pointer=pointer)
        return target

    def _open_relative_no_symlink(self, relative: str, pointer: str) -> int:
        parts = PurePosixPath(safe_relative_path(relative, pointer)).parts
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        file_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        directories: list[int] = []
        try:
            directories.append(os.open(self.root, directory_flags))
            for part in parts[:-1]:
                directories.append(os.open(part, directory_flags, dir_fd=directories[-1]))
            return os.open(parts[-1], file_flags, dir_fd=directories[-1])
        except OSError as error:
            if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                fail("VISTA_STAGING_SYMLINK_REJECTED", "Symlinks are not allowed", pointer=pointer)
            fail("VISTA_STAGING_SOURCE_UNAVAILABLE", "Selected source cannot be opened", pointer=pointer)
        finally:
            for descriptor in reversed(directories):
                os.close(descriptor)

    @contextmanager
    def open_regular(self, relative: str, pointer: str, maximum: int) -> Iterator[tuple[BinaryIO, os.stat_result]]:
        # The lstat walk gives a clear diagnostic; descriptor-relative openat
        # traversal closes the check/open race for every parent component.
        self.path(relative, pointer)
        descriptor = self._open_relative_no_symlink(relative, pointer)
        handle = os.fdopen(descriptor, "rb", closefd=True)
        try:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size < 1 or before.st_size > maximum:
                fail(
                    "VISTA_STAGING_SOURCE_SIZE_INVALID",
                    "Selected source must be a bounded regular file",
                    pointer=pointer,
                    details={"maximum_bytes": maximum},
                )
            yield handle, before
            after = os.fstat(handle.fileno())
            if (
                after.st_dev != before.st_dev
                or after.st_ino != before.st_ino
                or after.st_size != before.st_size
                or after.st_mtime_ns != before.st_mtime_ns
            ):
                fail("VISTA_STAGING_SOURCE_CHANGED", "Selected source changed while being read", pointer=pointer)
        finally:
            handle.close()

    def read(self, relative: str, pointer: str, maximum: int) -> tuple[bytes, FileEvidence]:
        with self.open_regular(relative, pointer, maximum) as (handle, metadata):
            value = handle.read(maximum + 1)
            if len(value) != metadata.st_size:
                fail("VISTA_STAGING_SOURCE_CHANGED", "Selected source size changed while being read", pointer=pointer)
        return value, FileEvidence(
            relative_path=relative,
            sha256=sha256_bytes(value),
            bytes=len(value),
            device=metadata.st_dev,
            inode=metadata.st_ino,
            mtime_ns=metadata.st_mtime_ns,
        )

    def hash(self, relative: str, pointer: str, maximum: int) -> FileEvidence:
        digest = hashlib.sha256()
        total = 0
        with self.open_regular(relative, pointer, maximum) as (handle, metadata):
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                digest.update(chunk)
            if total != metadata.st_size:
                fail("VISTA_STAGING_SOURCE_CHANGED", "Selected source size changed while being hashed", pointer=pointer)
        return FileEvidence(
            relative_path=relative,
            sha256=digest.hexdigest(),
            bytes=total,
            device=metadata.st_dev,
            inode=metadata.st_ino,
            mtime_ns=metadata.st_mtime_ns,
        )


def parse_json_bytes(value: bytes, pointer: str) -> Any:
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError:
        fail("VISTA_STAGING_JSON_INVALID", "JSON source must be UTF-8", pointer=pointer)
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, RecursionError):
        fail("VISTA_STAGING_JSON_INVALID", "JSON source is invalid", pointer=pointer)
    guard_tree(parsed, pointer)
    return parsed


def validate_file_declaration(value: Any, pointer: str) -> dict[str, Any]:
    item = exact_keys(
        value,
        allowed=["path", "sha256", "bytes"],
        required=["path", "sha256", "bytes"],
        pointer=pointer,
    )
    relative = safe_relative_path(item["path"], f"{pointer}/path")
    digest = require_string(item["sha256"], f"{pointer}/sha256", pattern=SHA256_RE, maximum=64)
    size = require_number(item["bytes"], f"{pointer}/bytes", minimum=1, maximum=MAX_MEDIA_BYTES)
    if not float(size).is_integer():
        fail("VISTA_STAGING_SCHEMA_INVALID", "Declared byte count must be an integer", pointer=f"{pointer}/bytes")
    return {"path": relative, "sha256": digest, "bytes": int(size)}


def validate_media_declaration(value: Any, pointer: str) -> dict[str, Any]:
    item = exact_keys(
        value,
        allowed=["path", "media_id", "media_type", "sha256", "bytes", "duration_sec", "width", "height"],
        required=["path", "media_id", "media_type", "sha256", "bytes", "duration_sec", "width", "height"],
        pointer=pointer,
    )
    base = validate_file_declaration(
        {key: item[key] for key in ("path", "sha256", "bytes")}, pointer
    )
    media_id = require_string(item["media_id"], f"{pointer}/media_id", pattern=SAFE_SOURCE_ID_RE, maximum=160)
    if item["media_type"] != "video/mp4":
        fail("VISTA_STAGING_MEDIA_INVALID", "Only selected video/mp4 media is accepted", pointer=f"{pointer}/media_type")
    duration = require_number(item["duration_sec"], f"{pointer}/duration_sec", minimum=0.001, maximum=3600)
    width = require_number(item["width"], f"{pointer}/width", minimum=1, maximum=16384)
    height = require_number(item["height"], f"{pointer}/height", minimum=1, maximum=16384)
    if not width.is_integer() or not height.is_integer():
        fail("VISTA_STAGING_MEDIA_INVALID", "Media dimensions must be integers", pointer=pointer)
    return {
        **base,
        "media_id": media_id,
        "media_type": "video/mp4",
        "duration_sec": duration,
        "width": int(width),
        "height": int(height),
    }


def validate_verified_record(value: Any, pointer: str) -> dict[str, Any]:
    guard_tree(value, pointer)
    assert_no_restricted_fields(value, pointer)
    record = exact_keys(
        value,
        allowed=[
            "schema",
            "dataset_revision",
            "source_row_id",
            "visual_id",
            "case_scope",
            "scenario_type",
            "duration_sec",
            "selected_attempt",
        ],
        required=[
            "schema",
            "dataset_revision",
            "source_row_id",
            "visual_id",
            "case_scope",
            "scenario_type",
            "duration_sec",
            "selected_attempt",
        ],
        pointer=pointer,
    )
    if record["schema"] != VERIFIED_RECORD_SCHEMA:
        fail("VISTA_STAGING_SCHEMA_INVALID", "Unsupported verified sample schema", pointer=f"{pointer}/schema")
    revision = require_string(record["dataset_revision"], f"{pointer}/dataset_revision", pattern=SAFE_ID_RE, maximum=128)
    source_row_id = require_string(record["source_row_id"], f"{pointer}/source_row_id", pattern=SAFE_SOURCE_ID_RE, maximum=512)
    visual_id = require_string(record["visual_id"], f"{pointer}/visual_id", pattern=SAFE_VISUAL_ID_RE, maximum=80)
    case_scope = require_string(record["case_scope"], f"{pointer}/case_scope", pattern=SAFE_ID_RE, maximum=128)
    scenario_type = require_string(record["scenario_type"], f"{pointer}/scenario_type", pattern=SAFE_ID_RE, maximum=128)
    if scenario_type not in SCENARIO_TYPES:
        fail("VISTA_STAGING_SCHEMA_INVALID", "Scenario type is not allowlisted", pointer=f"{pointer}/scenario_type")
    duration = require_number(record["duration_sec"], f"{pointer}/duration_sec", minimum=0.001, maximum=3600)
    attempt = exact_keys(
        record["selected_attempt"],
        allowed=["provider", "index", "selected", "render_script", "dialogue_no_oracle", "media"],
        required=["provider", "index", "selected", "render_script", "dialogue_no_oracle", "media"],
        pointer=f"{pointer}/selected_attempt",
    )
    provider = require_string(attempt["provider"], f"{pointer}/selected_attempt/provider", pattern=SAFE_PROVIDER_RE, maximum=64)
    index = require_number(attempt["index"], f"{pointer}/selected_attempt/index", minimum=1, maximum=1_000_000)
    if not index.is_integer() or attempt["selected"] is not True:
        fail("VISTA_STAGING_ATTEMPT_INVALID", "Attempt must be the explicitly selected positive integer attempt", pointer=f"{pointer}/selected_attempt")
    render = validate_file_declaration(attempt["render_script"], f"{pointer}/selected_attempt/render_script")
    dialogue = validate_file_declaration(attempt["dialogue_no_oracle"], f"{pointer}/selected_attempt/dialogue_no_oracle")
    media = validate_media_declaration(attempt["media"], f"{pointer}/selected_attempt/media")
    if render["bytes"] > MAX_RENDER_SCRIPT_BYTES or dialogue["bytes"] > MAX_DIALOGUE_BYTES or media["bytes"] > MAX_MEDIA_BYTES:
        fail("VISTA_STAGING_SOURCE_SIZE_INVALID", "Selected artifact exceeds its staging limit", pointer=f"{pointer}/selected_attempt")
    paths = [render["path"], dialogue["path"], media["path"]]
    if len(set(paths)) != len(paths):
        fail("VISTA_STAGING_PATH_INVALID", "Selected artifact paths must be unique", pointer=f"{pointer}/selected_attempt")
    return {
        "schema": VERIFIED_RECORD_SCHEMA,
        "dataset_revision": revision,
        "source_row_id": source_row_id,
        "visual_id": visual_id,
        "case_scope": case_scope,
        "scenario_type": scenario_type,
        "duration_sec": duration,
        "selected_attempt": {
            "provider": provider,
            "index": int(index),
            "selected": True,
            "render_script": render,
            "dialogue_no_oracle": dialogue,
            "media": media,
        },
    }


def load_verified_records(value: bytes, source_format: str) -> list[dict[str, Any]]:
    if source_format == "manifest":
        parsed = parse_json_bytes(value, "verified_source")
        assert_no_restricted_fields(parsed, "verified_source")
        manifest = exact_keys(
            parsed,
            allowed=["schema", "dataset_revision", "records"],
            required=["schema", "dataset_revision", "records"],
            pointer="verified_source",
        )
        if manifest["schema"] != VERIFIED_MANIFEST_SCHEMA:
            fail("VISTA_STAGING_SCHEMA_INVALID", "Unsupported verified manifest schema", pointer="verified_source/schema")
        revision = require_string(manifest["dataset_revision"], "verified_source/dataset_revision", pattern=SAFE_ID_RE, maximum=128)
        records_raw = manifest["records"]
        if not isinstance(records_raw, list) or not records_raw or len(records_raw) > MAX_RECORDS:
            fail("VISTA_STAGING_SCHEMA_INVALID", "Verified manifest records must be a bounded non-empty array", pointer="verified_source/records")
        records = [validate_verified_record(item, f"verified_source/records/{index}") for index, item in enumerate(records_raw)]
        if any(record["dataset_revision"] != revision for record in records):
            fail("VISTA_STAGING_IDENTITY_MISMATCH", "Manifest and record dataset revisions differ", pointer="verified_source/records")
        return records
    if source_format != "jsonl":
        fail("VISTA_STAGING_CONFIG_INVALID", "Verified source format must be explicit")
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError:
        fail("VISTA_STAGING_JSON_INVALID", "Verified JSONL source must be UTF-8", pointer="verified_source")
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        if len(line.encode("utf-8")) > 1024 * 1024:
            fail("VISTA_STAGING_SOURCE_TOO_COMPLEX", "Verified JSONL row exceeds its size limit", pointer=f"verified_source/line/{line_number}")
        try:
            item = json.loads(line)
        except (json.JSONDecodeError, RecursionError):
            fail("VISTA_STAGING_JSON_INVALID", "Verified JSONL row is invalid", pointer=f"verified_source/line/{line_number}")
        records.append(validate_verified_record(item, f"verified_source/line/{line_number}"))
        if len(records) > MAX_RECORDS:
            fail("VISTA_STAGING_SOURCE_TOO_COMPLEX", "Verified JSONL contains too many records", pointer="verified_source")
    if not records:
        fail("VISTA_STAGING_SCHEMA_INVALID", "Verified JSONL has no records", pointer="verified_source")
    return records


def select_verified_record(
    records: Sequence[dict[str, Any]],
    *,
    dataset_revision: str,
    sample_id: str,
    provider: str,
    attempt_index: int,
) -> dict[str, Any]:
    matches = [
        record
        for record in records
        if record["dataset_revision"] == dataset_revision
        and record["visual_id"] == sample_id
        and record["selected_attempt"]["provider"] == provider
        and record["selected_attempt"]["index"] == attempt_index
    ]
    if not matches:
        fail(
            "VISTA_STAGING_SELECTION_NOT_FOUND",
            "No exact verified selected attempt matches the requested identity",
            details={
                "dataset_revision": dataset_revision,
                "sample_id": sample_id,
                "provider": provider,
                "attempt": attempt_index,
            },
        )
    if len(matches) != 1:
        fail("VISTA_STAGING_SELECTION_AMBIGUOUS", "Verified source contains duplicate selected identities")
    return matches[0]


def verify_declared_evidence(actual: FileEvidence, declared: Mapping[str, Any], pointer: str) -> None:
    if actual.sha256 != declared["sha256"] or actual.bytes != declared["bytes"]:
        fail(
            "VISTA_STAGING_CHECKSUM_MISMATCH",
            "Selected source checksum or byte count does not match verified evidence",
            pointer=pointer,
            details={"role": pointer.rsplit("/", 1)[-1]},
        )


def validate_identity_source(source: Any, record: Mapping[str, Any], pointer: str) -> None:
    value = exact_keys(
        source,
        allowed=["dataset_revision", "source_row_id", "visual_id", "case_scope", "scenario_type", "attempt"],
        required=["dataset_revision", "source_row_id", "visual_id", "case_scope", "scenario_type", "attempt"],
        pointer=pointer,
    )
    for field in ("dataset_revision", "source_row_id", "visual_id", "case_scope", "scenario_type"):
        if value[field] != record[field]:
            fail("VISTA_STAGING_IDENTITY_MISMATCH", "Joined source identity does not match selected record", pointer=f"{pointer}/{field}")
    attempt = exact_keys(
        value["attempt"],
        allowed=["provider", "index", "selected"],
        required=["provider", "index", "selected"],
        pointer=f"{pointer}/attempt",
    )
    selected = record["selected_attempt"]
    if attempt != {"provider": selected["provider"], "index": selected["index"], "selected": True}:
        fail("VISTA_STAGING_IDENTITY_MISMATCH", "Joined attempt identity does not match selected record", pointer=f"{pointer}/attempt")


def validate_dialogue(value: bytes, record: Mapping[str, Any]) -> dict[str, Any]:
    parsed = parse_json_bytes(value, "dialogue_no_oracle")
    assert_no_restricted_fields(parsed, "dialogue_no_oracle")
    dialogue = exact_keys(
        parsed,
        allowed=["schema", "profile", "privilege", "source", "turns"],
        required=["schema", "profile", "privilege", "source", "turns"],
        pointer="dialogue_no_oracle",
    )
    if dialogue["schema"] != DIALOGUE_SCHEMA or dialogue["profile"] != "evaluation_safe":
        fail("VISTA_STAGING_DIALOGUE_INVALID", "Dialogue schema/profile is not evaluation-safe")
    privilege = exact_keys(
        dialogue["privilege"],
        allowed=["classification", "evaluation_input_allowed"],
        required=["classification", "evaluation_input_allowed"],
        pointer="dialogue_no_oracle/privilege",
    )
    if privilege != {"classification": "evaluation_safe", "evaluation_input_allowed": True}:
        fail("VISTA_STAGING_DIALOGUE_INVALID", "Dialogue privilege boundary is invalid")
    validate_identity_source(dialogue["source"], record, "dialogue_no_oracle/source")
    turns = dialogue["turns"]
    if not isinstance(turns, list) or len(turns) > 200:
        fail("VISTA_STAGING_DIALOGUE_INVALID", "Dialogue turns must be a bounded array", pointer="dialogue_no_oracle/turns")
    ids: set[int] = set()
    for index, item in enumerate(turns):
        pointer = f"dialogue_no_oracle/turns/{index}"
        turn = exact_keys(
            item,
            allowed=["turn_id", "role", "speaker", "text"],
            required=["turn_id", "role", "speaker", "text"],
            pointer=pointer,
        )
        turn_id = turn["turn_id"]
        if isinstance(turn_id, bool) or not isinstance(turn_id, int) or turn_id < 1 or turn_id in ids:
            fail("VISTA_STAGING_DIALOGUE_INVALID", "Dialogue turn ids must be positive and unique", pointer=f"{pointer}/turn_id")
        ids.add(turn_id)
        if turn["role"] != "context" or turn["speaker"] not in {"other_person", "user"}:
            fail("VISTA_STAGING_DIALOGUE_INVALID", "Dialogue role/speaker is not allowlisted", pointer=pointer)
        require_string(turn["text"], f"{pointer}/text", maximum=8000)
    return dialogue


def parse_timestamp_token(token: str) -> float:
    normalized = token.strip().lower()
    if re.fullmatch(r"\d+(?:\.\d+)?s?", normalized):
        return float(normalized.rstrip("s"))
    parts = normalized.split(":")
    if len(parts) not in {2, 3} or not all(re.fullmatch(r"\d+(?:\.\d+)?", part) for part in parts):
        return math.nan
    numbers = [float(part) for part in parts]
    if any(part >= 60 for part in numbers[1:]):
        return math.nan
    return numbers[0] * 60 + numbers[1] if len(parts) == 2 else numbers[0] * 3600 + numbers[1] * 60 + numbers[2]


def parse_timestamped_action(value: Any, pointer: str) -> float:
    text = require_string(value, pointer, maximum=32_000)
    matched = TIMESTAMP_ACTION_RE.match(text) or SECONDS_ACTION_RE.match(text)
    if not matched:
        fail("VISTA_STAGING_ACTION_INVALID", "Action must begin with an absolute timestamp", pointer=pointer)
    at_sec = parse_timestamp_token(matched.group(1))
    if not math.isfinite(at_sec) or at_sec < 0 or not matched.group(2).strip():
        fail("VISTA_STAGING_ACTION_INVALID", "Action timestamp or description is invalid", pointer=pointer)
    return round(at_sec, 3)


def validate_render_script(value: bytes, record: Mapping[str, Any]) -> list[float]:
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError:
        fail("VISTA_STAGING_YAML_INVALID", "Render script must be UTF-8", pointer="render_script")
    if "\x00" in text:
        fail("VISTA_STAGING_YAML_INVALID", "Render script contains a NUL byte", pointer="render_script")
    try:
        for token in yaml.scan(text):
            if isinstance(token, (AliasToken, AnchorToken, DirectiveToken, TagToken)):
                fail("VISTA_STAGING_YAML_UNSAFE", "YAML aliases, anchors, directives, and tags are not accepted", pointer="render_script")
        parsed = yaml.safe_load(text)
    except VistaStagingError:
        raise
    except (yaml.YAMLError, RecursionError):
        fail("VISTA_STAGING_YAML_INVALID", "Render script is not safe, valid YAML", pointer="render_script")
    guard_tree(parsed, "render_script")
    assert_no_restricted_fields(
        parsed,
        "render_script",
        pattern=RECONSTRUCTION_RESTRICTED_KEY_RE,
    )
    root = require_object(parsed, "render_script")
    for required in ("Global_Metadata", "Camera_Continuity", "Scene"):
        if required not in root:
            fail("VISTA_STAGING_YAML_INVALID", "Render script is missing a required section", pointer=f"render_script/{required}")
    metadata = require_object(root["Global_Metadata"], "render_script/Global_Metadata")
    for key in ("Perspective", "Environment", "Lighting", "Duration_sec"):
        if key not in metadata:
            fail("VISTA_STAGING_YAML_INVALID", "Global metadata is incomplete", pointer=f"render_script/Global_Metadata/{key}")
    perspective = require_string(metadata["Perspective"], "render_script/Global_Metadata/Perspective", maximum=256)
    if not re.search(r"(?:first[-_ ]person|egocentric)", perspective, re.IGNORECASE) or re.search(r"third[-_ ]person", perspective, re.IGNORECASE):
        fail("VISTA_STAGING_CAMERA_INVALID", "Render script must use a verified first-person camera contract")
    require_string(metadata["Environment"], "render_script/Global_Metadata/Environment", maximum=32_000)
    require_string(metadata["Lighting"], "render_script/Global_Metadata/Lighting", maximum=8000)
    duration = require_number(metadata["Duration_sec"], "render_script/Global_Metadata/Duration_sec", minimum=0.001, maximum=3600)
    if abs(duration - float(record["duration_sec"])) > 0.001:
        fail(
            "VISTA_STAGING_DURATION_MISMATCH",
            "Verified row and render script durations differ",
            details={"verified_duration_sec": record["duration_sec"], "render_duration_sec": duration},
        )
    camera = require_object(root["Camera_Continuity"], "render_script/Camera_Continuity")
    for key in ("Position", "Height", "Angle", "Motion", "Framing"):
        if key not in camera:
            fail("VISTA_STAGING_YAML_INVALID", "Camera continuity is incomplete", pointer=f"render_script/Camera_Continuity/{key}")
        require_string(camera[key], f"render_script/Camera_Continuity/{key}", maximum=32_000)
    scene = require_object(root["Scene"], "render_script/Scene")
    for key in ("Key_Visual_Elements", "Actions", "Dialogue"):
        if key not in scene:
            fail("VISTA_STAGING_YAML_INVALID", "Scene section is incomplete", pointer=f"render_script/Scene/{key}")
    elements = scene["Key_Visual_Elements"]
    if not isinstance(elements, list) or not elements or len(elements) > 200:
        fail("VISTA_STAGING_YAML_INVALID", "Key visual elements must be a bounded non-empty array", pointer="render_script/Scene/Key_Visual_Elements")
    for index, item in enumerate(elements):
        require_string(item, f"render_script/Scene/Key_Visual_Elements/{index}", maximum=8000)
    if scene["Dialogue"] != []:
        fail("VISTA_STAGING_DIALOGUE_INVALID", "Render script may not embed dialogue; use the no-oracle join", pointer="render_script/Scene/Dialogue")
    actions = scene["Actions"]
    if not isinstance(actions, list) or not actions or len(actions) > 500:
        fail("VISTA_STAGING_TIMELINE_INVALID", "Scene actions must be a bounded non-empty array", pointer="render_script/Scene/Actions")
    timestamps = [parse_timestamped_action(item, f"render_script/Scene/Actions/{index}") for index, item in enumerate(actions)]
    previous = -1.0
    for index, at_sec in enumerate(timestamps):
        if at_sec > duration + 0.001 or at_sec <= previous:
            fail(
                "VISTA_STAGING_TIMELINE_INVALID",
                "Action timestamps must be strictly ordered, unique, and within duration",
                pointer=f"render_script/Scene/Actions/{index}",
            )
        previous = at_sec
    return timestamps


@dataclass
class Mp4Metadata:
    duration_sec: float
    width: int
    height: int


def _read_exact(handle: BinaryIO, size: int, pointer: str) -> bytes:
    value = handle.read(size)
    if len(value) != size:
        fail("VISTA_STAGING_MEDIA_INVALID", "MP4 box is truncated", pointer=pointer)
    return value


def _iter_mp4_boxes(
    handle: BinaryIO,
    start: int,
    end: int,
    pointer: str,
    state: list[int],
) -> Iterator[tuple[bytes, int, int]]:
    position = start
    while position < end:
        state[0] += 1
        if state[0] > MAX_MP4_BOXES or end - position < 8:
            fail("VISTA_STAGING_MEDIA_INVALID", "MP4 box structure is invalid or too complex", pointer=pointer)
        handle.seek(position)
        header = _read_exact(handle, 8, pointer)
        size32, box_type = struct.unpack(">I4s", header)
        header_size = 8
        if size32 == 1:
            size = struct.unpack(">Q", _read_exact(handle, 8, pointer))[0]
            header_size = 16
        elif size32 == 0:
            size = end - position
        else:
            size = size32
        if size < header_size or position + size > end:
            fail("VISTA_STAGING_MEDIA_INVALID", "MP4 box size is invalid", pointer=pointer)
        yield box_type, position + header_size, position + size
        position += size
    if position != end:
        fail("VISTA_STAGING_MEDIA_INVALID", "MP4 boxes do not cover their container", pointer=pointer)


def _parse_mvhd(handle: BinaryIO, payload_start: int, box_end: int) -> float:
    handle.seek(payload_start)
    version = _read_exact(handle, 1, "media/mvhd")[0]
    if version == 0:
        if box_end - payload_start < 20:
            fail("VISTA_STAGING_MEDIA_INVALID", "mvhd box is truncated", pointer="media/mvhd")
        handle.seek(payload_start + 12)
        timescale, duration = struct.unpack(">II", _read_exact(handle, 8, "media/mvhd"))
    elif version == 1:
        if box_end - payload_start < 32:
            fail("VISTA_STAGING_MEDIA_INVALID", "mvhd box is truncated", pointer="media/mvhd")
        handle.seek(payload_start + 20)
        timescale = struct.unpack(">I", _read_exact(handle, 4, "media/mvhd"))[0]
        duration = struct.unpack(">Q", _read_exact(handle, 8, "media/mvhd"))[0]
    else:
        fail("VISTA_STAGING_MEDIA_INVALID", "Unsupported mvhd version", pointer="media/mvhd")
    if timescale == 0 or duration == 0:
        fail("VISTA_STAGING_MEDIA_INVALID", "MP4 duration metadata is invalid", pointer="media/mvhd")
    return duration / timescale


def _parse_tkhd_dimensions(handle: BinaryIO, payload_start: int, box_end: int) -> tuple[int, int] | None:
    if box_end - payload_start < 16:
        fail("VISTA_STAGING_MEDIA_INVALID", "tkhd box is truncated", pointer="media/tkhd")
    handle.seek(payload_start)
    version = _read_exact(handle, 1, "media/tkhd")[0]
    if version not in {0, 1}:
        fail("VISTA_STAGING_MEDIA_INVALID", "Unsupported tkhd version", pointer="media/tkhd")
    handle.seek(box_end - 8)
    width_fixed, height_fixed = struct.unpack(">II", _read_exact(handle, 8, "media/tkhd"))
    width = width_fixed / 65536.0
    height = height_fixed / 65536.0
    if width <= 0 or height <= 0:
        return None
    rounded_width, rounded_height = round(width), round(height)
    if abs(width - rounded_width) > 0.001 or abs(height - rounded_height) > 0.001:
        fail("VISTA_STAGING_MEDIA_INVALID", "MP4 track dimensions are not integral", pointer="media/tkhd")
    return rounded_width, rounded_height


def inspect_mp4(dataset: DatasetRoot, evidence: FileEvidence) -> Mp4Metadata:
    dimensions: list[tuple[int, int]] = []
    duration: float | None = None
    with dataset.open_regular(evidence.relative_path, "--media", MAX_MEDIA_BYTES) as (handle, metadata):
        if (
            metadata.st_dev != evidence.device
            or metadata.st_ino != evidence.inode
            or metadata.st_size != evidence.bytes
            or metadata.st_mtime_ns != evidence.mtime_ns
        ):
            fail("VISTA_STAGING_SOURCE_CHANGED", "Selected media changed after evidence hashing", pointer="--media")
        top_level = list(_iter_mp4_boxes(handle, 0, metadata.st_size, "media", [0]))
        if not any(box_type == b"ftyp" for box_type, _, _ in top_level):
            fail("VISTA_STAGING_MEDIA_INVALID", "Selected media has no MP4 ftyp box", pointer="--media")
        moov_boxes = [(start, end) for box_type, start, end in top_level if box_type == b"moov"]
        if len(moov_boxes) != 1:
            fail("VISTA_STAGING_MEDIA_INVALID", "Selected media must have exactly one MP4 moov box", pointer="--media")
        state = [len(top_level)]
        for box_type, payload_start, box_end in _iter_mp4_boxes(handle, moov_boxes[0][0], moov_boxes[0][1], "media/moov", state):
            if box_type == b"mvhd":
                if duration is not None:
                    fail("VISTA_STAGING_MEDIA_INVALID", "MP4 contains duplicate mvhd boxes", pointer="media/moov")
                duration = _parse_mvhd(handle, payload_start, box_end)
            elif box_type == b"trak":
                for child_type, child_start, child_end in _iter_mp4_boxes(handle, payload_start, box_end, "media/moov/trak", state):
                    if child_type == b"tkhd":
                        candidate = _parse_tkhd_dimensions(handle, child_start, child_end)
                        if candidate:
                            dimensions.append(candidate)
    if duration is None or not dimensions:
        fail("VISTA_STAGING_MEDIA_INVALID", "MP4 lacks duration or visual track dimensions", pointer="--media")
    width, height = max(dimensions, key=lambda item: item[0] * item[1])
    return Mp4Metadata(duration_sec=duration, width=width, height=height)


def bundle_tree_digest(entries: Sequence[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    digest.update(BUNDLE_DIGEST_ALGORITHM.encode("ascii") + b"\0")
    for entry in sorted(entries, key=lambda item: item["path"]):
        digest.update(entry["path"].encode("utf-8") + b"\0")
        digest.update(str(entry["bytes"]).encode("ascii") + b"\0")
        digest.update(bytes.fromhex(entry["sha256"]))
        digest.update(b"\0")
    return digest.hexdigest()


def file_entry(path: str, value: bytes) -> dict[str, Any]:
    return {"path": path, "sha256": sha256_bytes(value), "bytes": len(value)}


@dataclass
class StagePlan:
    dataset: DatasetRoot
    output_dir: Path
    small_files: dict[str, bytes]
    media_source: FileEvidence
    core_entries: list[dict[str, Any]]
    report: dict[str, Any]
    registry_snippet: dict[str, Any]


def normalized_output_dir(dataset: DatasetRoot, value: str) -> Path:
    if not value or not os.path.isabs(value):
        fail("VISTA_STAGING_OUTPUT_INVALID", "--output-dir must be an absolute path", pointer="--output-dir")
    output = Path(os.path.abspath(value))
    if output == Path(output.anchor):
        fail("VISTA_STAGING_OUTPUT_INVALID", "Output directory cannot be a filesystem root", pointer="--output-dir")
    if not SAFE_ID_RE.fullmatch(output.name):
        fail("VISTA_STAGING_OUTPUT_INVALID", "Output directory name must be a safe identifier", pointer="--output-dir")
    try:
        output.relative_to(dataset.root)
    except ValueError:
        pass
    else:
        fail("VISTA_STAGING_OUTPUT_INVALID", "Output directory must be outside the authoritative dataset root", pointer="--output-dir")
    return output


def build_stage_plan(args: argparse.Namespace) -> StagePlan:
    dataset = DatasetRoot(args.dataset_root)
    revision = require_string(args.dataset_revision, "--dataset-revision", pattern=SAFE_ID_RE, maximum=128)
    sample_id = require_string(args.sample_id, "--sample-id", pattern=SAFE_VISUAL_ID_RE, maximum=80)
    provider = require_string(args.provider, "--provider", pattern=SAFE_PROVIDER_RE, maximum=64)
    if not isinstance(args.attempt, int) or args.attempt < 1 or args.attempt > 1_000_000:
        fail("VISTA_STAGING_ATTEMPT_INVALID", "--attempt must be a positive integer", pointer="--attempt")
    verified_relative = safe_relative_path(args.verified_source, "--verified-source")
    render_relative = safe_relative_path(args.render_script, "--render-script")
    dialogue_relative = safe_relative_path(args.dialogue_no_oracle, "--dialogue-no-oracle")
    media_relative = safe_relative_path(args.media, "--media")
    if len({verified_relative, render_relative, dialogue_relative, media_relative}) != 4:
        fail("VISTA_STAGING_PATH_INVALID", "All selected input paths must be distinct")
    output_dir = normalized_output_dir(dataset, args.output_dir)
    _validate_private_parent(output_dir.parent)

    verified_bytes, verified_evidence = dataset.read(
        verified_relative, "--verified-source", MAX_VERIFIED_SOURCE_BYTES
    )
    records = load_verified_records(verified_bytes, args.verified_format)
    record = select_verified_record(
        records,
        dataset_revision=revision,
        sample_id=sample_id,
        provider=provider,
        attempt_index=args.attempt,
    )
    selected = record["selected_attempt"]
    requested_paths = {
        "render_script": render_relative,
        "dialogue_no_oracle": dialogue_relative,
        "media": media_relative,
    }
    for role, requested in requested_paths.items():
        if selected[role]["path"] != requested:
            fail(
                "VISTA_STAGING_IDENTITY_MISMATCH",
                "CLI-selected artifact path does not match the authoritative selected attempt",
                pointer=f"--{role.replace('_', '-')}",
                details={"role": role},
            )

    render_bytes, render_evidence = dataset.read(render_relative, "--render-script", MAX_RENDER_SCRIPT_BYTES)
    dialogue_bytes, dialogue_evidence = dataset.read(dialogue_relative, "--dialogue-no-oracle", MAX_DIALOGUE_BYTES)
    media_evidence = dataset.hash(media_relative, "--media", MAX_MEDIA_BYTES)
    verify_declared_evidence(render_evidence, selected["render_script"], "selected_attempt/render_script")
    verify_declared_evidence(dialogue_evidence, selected["dialogue_no_oracle"], "selected_attempt/dialogue_no_oracle")
    verify_declared_evidence(media_evidence, selected["media"], "selected_attempt/media")

    timestamps = validate_render_script(render_bytes, record)
    validate_dialogue(dialogue_bytes, record)
    media_metadata = inspect_mp4(dataset, media_evidence)
    for label, actual, expected in (
        ("duration_sec", media_metadata.duration_sec, float(record["duration_sec"])),
        ("verified_duration_sec", media_metadata.duration_sec, float(selected["media"]["duration_sec"])),
    ):
        if abs(actual - expected) > 0.001:
            fail(
                "VISTA_STAGING_DURATION_MISMATCH",
                "Selected media duration does not match verified duration",
                pointer="selected_attempt/media/duration_sec",
                details={"comparison": label, "media_duration_sec": actual, "expected_duration_sec": expected},
            )
    if (media_metadata.width, media_metadata.height) != (selected["media"]["width"], selected["media"]["height"]):
        fail(
            "VISTA_STAGING_MEDIA_MISMATCH",
            "Selected media dimensions do not match verified evidence",
            pointer="selected_attempt/media",
            details={
                "media_dimensions": [media_metadata.width, media_metadata.height],
                "verified_dimensions": [selected["media"]["width"], selected["media"]["height"]],
            },
        )

    identity = {
        "dataset_revision": record["dataset_revision"],
        "source_row_id": record["source_row_id"],
        "visual_id": record["visual_id"],
        "case_scope": record["case_scope"],
        "scenario_type": record["scenario_type"],
        "attempt": {"provider": provider, "index": args.attempt, "selected": True},
    }
    selection_digest = sha256_bytes(canonical_json_bytes(record))
    raw_bundle_id = f"{revision}:{sample_id}:{record['case_scope']}:{provider}:attempt_{args.attempt:03d}"
    bundle_id = raw_bundle_id if len(raw_bundle_id) <= 160 else f"vista:{selection_digest}"
    media_descriptor = {
        "schema": MEDIA_SCHEMA,
        "profile": "reconstruction",
        "privilege": {"classification": "reconstruction_source", "evaluation_input_allowed": False},
        "source": identity,
        "media": {
            "media_id": selected["media"]["media_id"],
            "role": "reference_video",
            "logical_ref": "media/reference.mp4",
            "media_type": "video/mp4",
            "sha256": media_evidence.sha256,
            "bytes": media_evidence.bytes,
            "duration_sec": float(record["duration_sec"]),
            "width": media_metadata.width,
            "height": media_metadata.height,
            # vista-import-source/v1 permits exactly three declared inputs.  A
            # sidecar is therefore not an importer-declared bundled source.
            "bundled": False,
            "integrity_status": "recorded_checksum",
            "privilege": "reconstruction_only",
        },
    }
    descriptor_bytes = canonical_json_bytes(media_descriptor)
    declarations = [
        {
            "path": "render_script.yaml",
            "role": "render_script",
            "media_type": "application/yaml",
            "sha256": sha256_bytes(render_bytes),
            "bytes": len(render_bytes),
            "required": True,
            "privilege": "reconstruction_only",
        },
        {
            "path": "dialogue.no-oracle.json",
            "role": "dialogue_no_oracle",
            "media_type": "application/json",
            "sha256": sha256_bytes(dialogue_bytes),
            "bytes": len(dialogue_bytes),
            "required": True,
            "privilege": "evaluation_safe",
        },
        {
            "path": "media.descriptor.json",
            "role": "media_descriptor",
            "media_type": "application/json",
            "sha256": sha256_bytes(descriptor_bytes),
            "bytes": len(descriptor_bytes),
            "required": True,
            "privilege": "reconstruction_only",
        },
    ]
    manifest = {
        "schema": SOURCE_SCHEMA,
        "bundle_id": bundle_id,
        "dataset_revision": revision,
        "profile": "reconstruction",
        "privilege": {
            "classification": "reconstruction_source",
            "evaluation_input_allowed": False,
            "allowed_consumers": ["vista_importer", "scene_normalizer"],
        },
        "sample": {
            "source_row_id": record["source_row_id"],
            "visual_id": sample_id,
            "case_scope": record["case_scope"],
            "scenario_type": record["scenario_type"],
            "duration_sec": float(record["duration_sec"]),
            "attempt": {"provider": provider, "index": args.attempt, "selected": True},
        },
        "files": declarations,
    }
    manifest_bytes = canonical_json_bytes(manifest)
    core_entries = [
        file_entry("manifest.json", manifest_bytes),
        file_entry("render_script.yaml", render_bytes),
        file_entry("dialogue.no-oracle.json", dialogue_bytes),
        file_entry("media.descriptor.json", descriptor_bytes),
        {"path": "media/reference.mp4", "sha256": media_evidence.sha256, "bytes": media_evidence.bytes},
    ]
    bundle_digest = bundle_tree_digest(core_entries)
    registry_snippet = {revision: {"root": str(output_dir), "manifestPath": "manifest.json"}}
    report = {
        "schema": REPORT_SCHEMA,
        "valid": True,
        "policy": {
            "network": "forbidden",
            "directory_scan": "forbidden",
            "symlink_follow": "forbidden",
            "default_mode": "dry_run",
            "restricted_fields": "rejected",
            "media_sidecar_privilege": "reconstruction_only",
        },
        "selection": {
            **identity,
            "verified_source": {
                **verified_evidence.public_dict(),
                "format": args.verified_format,
                "schema": VERIFIED_MANIFEST_SCHEMA if args.verified_format == "manifest" else VERIFIED_RECORD_SCHEMA,
            },
            "selection_sha256": selection_digest,
        },
        "validation": {
            "identity_join": "passed",
            "selected_attempt": "passed",
            "source_checksums": "passed",
            "render_duration": "passed",
            "media_binary_metadata": "passed",
            "dialogue_evaluation_boundary": "passed",
            "scene_action_timestamps_sec": timestamps,
        },
        "source_evidence": {
            "render_script": render_evidence.public_dict(),
            "dialogue_no_oracle": dialogue_evidence.public_dict(),
            "media": media_evidence.public_dict(),
        },
        "bundle": {
            "root": str(output_dir),
            "bundle_id": bundle_id,
            "schema": SOURCE_SCHEMA,
            "digest_algorithm": BUNDLE_DIGEST_ALGORITHM,
            "digest_sha256": bundle_digest,
            "entries": sorted(core_entries, key=lambda item: item["path"]),
            "declared_importer_file_count": 3,
            "media_sidecar_declared_to_importer": False,
        },
        "registry_snippet": registry_snippet,
    }
    report_bytes = canonical_json_bytes(report)
    registry_bytes = canonical_json_bytes(registry_snippet)
    small_files = {
        "manifest.json": manifest_bytes,
        "render_script.yaml": render_bytes,
        "dialogue.no-oracle.json": dialogue_bytes,
        "media.descriptor.json": descriptor_bytes,
        "validation-report.json": report_bytes,
        "registry-snippet.json": registry_bytes,
    }
    return StagePlan(
        dataset=dataset,
        output_dir=output_dir,
        small_files=small_files,
        media_source=media_evidence,
        core_entries=core_entries,
        report=report,
        registry_snippet=registry_snippet,
    )


EXPECTED_DIRS = frozenset({"media"})


def _output_expected_entries(plan: StagePlan) -> dict[str, tuple[str, int]]:
    entries = {
        relative: (sha256_bytes(value), len(value))
        for relative, value in plan.small_files.items()
    }
    entries["media/reference.mp4"] = (plan.media_source.sha256, plan.media_source.bytes)
    return entries


def _verify_existing_output(plan: StagePlan) -> bool:
    root = plan.output_dir
    try:
        root_metadata = os.lstat(root)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        fail("VISTA_STAGING_OUTPUT_CONFLICT", "Existing output is not a private regular directory", pointer="--output-dir")
    if stat.S_IMODE(root_metadata.st_mode) != 0o700:
        fail("VISTA_STAGING_OUTPUT_CONFLICT", "Existing output directory mode is not 0700", pointer="--output-dir")
    expected_files = _output_expected_entries(plan)
    observed_files: set[str] = set()
    observed_dirs: set[str] = set()
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        relative_current = current_path.relative_to(root)
        for directory in dirs:
            candidate = current_path / directory
            relative = (relative_current / directory).as_posix()
            metadata = os.lstat(candidate)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700:
                fail("VISTA_STAGING_OUTPUT_CONFLICT", "Existing output contains an unsafe directory", pointer=relative)
            observed_dirs.add(relative)
        for filename in files:
            candidate = current_path / filename
            relative = (relative_current / filename).as_posix()
            metadata = os.lstat(candidate)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
                fail("VISTA_STAGING_OUTPUT_CONFLICT", "Existing output contains an unsafe file", pointer=relative)
            observed_files.add(relative)
    if observed_dirs != EXPECTED_DIRS or observed_files != set(expected_files):
        fail("VISTA_STAGING_OUTPUT_CONFLICT", "Existing output file set differs from the validated bundle", pointer="--output-dir")
    for relative, (expected_digest, expected_size) in expected_files.items():
        path = root.joinpath(*PurePosixPath(relative).parts)
        digest = hashlib.sha256()
        total = 0
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
                total += len(chunk)
        if total != expected_size or digest.hexdigest() != expected_digest:
            fail("VISTA_STAGING_OUTPUT_CONFLICT", "Existing output content differs from the validated bundle", pointer=relative)
    return True


def _validate_private_parent(parent: Path) -> None:
    _reject_symlink_components(parent, "--output-dir")
    metadata = os.lstat(parent)
    if not stat.S_ISDIR(metadata.st_mode):
        fail("VISTA_STAGING_OUTPUT_INVALID", "Output parent must be an existing directory", pointer="--output-dir")
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        fail("VISTA_STAGING_OUTPUT_INVALID", "Output parent must be owned by the current user and private", pointer="--output-dir")


def _write_private(path: Path, value: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    os.chmod(path, 0o600, follow_symlinks=False)


def _copy_verified_media(plan: StagePlan, destination: Path) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    output_descriptor = os.open(destination, flags, 0o600)
    digest = hashlib.sha256()
    total = 0
    try:
        with plan.dataset.open_regular(plan.media_source.relative_path, "--media", MAX_MEDIA_BYTES) as (source, metadata):
            if (
                metadata.st_dev != plan.media_source.device
                or metadata.st_ino != plan.media_source.inode
                or metadata.st_size != plan.media_source.bytes
                or metadata.st_mtime_ns != plan.media_source.mtime_ns
            ):
                fail("VISTA_STAGING_SOURCE_CHANGED", "Selected media changed before apply", pointer="--media")
            with os.fdopen(output_descriptor, "wb", closefd=False) as target:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    target.write(chunk)
                    digest.update(chunk)
                    total += len(chunk)
                target.flush()
                os.fsync(target.fileno())
    finally:
        os.close(output_descriptor)
    os.chmod(destination, 0o600, follow_symlinks=False)
    if total != plan.media_source.bytes or digest.hexdigest() != plan.media_source.sha256:
        fail("VISTA_STAGING_SOURCE_CHANGED", "Selected media evidence changed during apply", pointer="--media")


def apply_stage_plan(plan: StagePlan) -> str:
    parent = plan.output_dir.parent
    _validate_private_parent(parent)
    if _verify_existing_output(plan):
        return "idempotent"
    lock = parent / f".{plan.output_dir.name}.stage.lock"
    lock_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        lock_descriptor = os.open(lock, lock_flags, 0o600)
    except FileExistsError:
        fail("VISTA_STAGING_OUTPUT_BUSY", "Another staging operation owns the output lock", pointer="--output-dir")
    os.close(lock_descriptor)
    temp_root: Path | None = None
    try:
        if _verify_existing_output(plan):
            return "idempotent"
        temp_root = Path(tempfile.mkdtemp(prefix=f".{plan.output_dir.name}.tmp-", dir=parent))
        os.chmod(temp_root, 0o700)
        media_dir = temp_root / "media"
        media_dir.mkdir(mode=0o700)
        os.chmod(media_dir, 0o700)
        for relative, value in plan.small_files.items():
            _write_private(temp_root.joinpath(*PurePosixPath(relative).parts), value)
        _copy_verified_media(plan, media_dir / "reference.mp4")
        for directory in (media_dir, temp_root):
            descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        if plan.output_dir.exists() or plan.output_dir.is_symlink():
            fail("VISTA_STAGING_OUTPUT_CONFLICT", "Output appeared during atomic staging", pointer="--output-dir")
        os.rename(temp_root, plan.output_dir)
        temp_root = None
        parent_descriptor = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
        return "created"
    finally:
        if temp_root is not None and temp_root.exists():
            shutil.rmtree(temp_root)
        try:
            lock.unlink()
        except FileNotFoundError:
            pass


class MachineReadableArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        fail(
            "VISTA_STAGING_ARGUMENT_INVALID",
            "CLI arguments are missing or invalid; consult --help",
        )


def build_parser() -> argparse.ArgumentParser:
    parser = MachineReadableArgumentParser(
        description="Validate and optionally stage one explicit verified VISTA selection (offline)."
    )
    parser.add_argument("--dataset-root", required=True, help="Authoritative dataset root; no symlinks allowed")
    parser.add_argument("--verified-source", required=True, help="Relative verified manifest/JSONL path")
    parser.add_argument("--verified-format", required=True, choices=("manifest", "jsonl"))
    parser.add_argument("--dataset-revision", required=True)
    parser.add_argument("--sample-id", required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--attempt", required=True, type=int)
    parser.add_argument("--render-script", required=True, help="Relative selected render_script.yaml path")
    parser.add_argument("--dialogue-no-oracle", required=True, help="Relative selected evaluation-safe dialogue path")
    parser.add_argument("--media", required=True, help="Relative selected video/mp4 path")
    parser.add_argument("--output-dir", required=True, help="Absolute final bundle directory outside dataset root")
    parser.add_argument("--apply", action="store_true", help="Atomically create the private output directory")
    return parser


def result_for_plan(plan: StagePlan, status: str) -> dict[str, Any]:
    return {
        "schema": RESULT_SCHEMA,
        "valid": True,
        "status": status,
        "bundle_digest_sha256": plan.report["bundle"]["digest_sha256"],
        "validation_report": plan.report,
        "registry_snippet": plan.registry_snippet,
    }


def main(argv: Sequence[str] | None = None) -> int:
    os.umask(0o077)
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        plan = build_stage_plan(args)
        status = apply_stage_plan(plan) if args.apply else "dry_run"
        print(compact_json(result_for_plan(plan, status)))
        return 0
    except VistaStagingError as error:
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
                        "code": "VISTA_STAGING_INTERNAL_ERROR",
                        "message": "Staging failed before a safe validation result could be produced",
                    },
                }
            ),
            file=sys.stderr,
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
