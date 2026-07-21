#!/usr/bin/env python3
"""Prepare a deterministic, offline semantic object-index job bundle.

The default behavior is a validation-only dry run.  ``--apply`` publishes only
private JSON evidence; this module never contacts Unreal Engine, a caption
provider, PostgreSQL, Qdrant, an embedding service, or the network.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import errno
import hashlib
import json
import math
import os
import re
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    import build_ue_asset_registry_bootstrap as bootstrap_contract
except ModuleNotFoundError:  # Imported as tools.prepare_semantic_asset_index_job.
    from tools import build_ue_asset_registry_bootstrap as bootstrap_contract


RECIPE_SCHEMA = "simworld-semantic-asset-index-recipe/v1"
JOB_SCHEMA = "simworld-semantic-asset-index-job/v1"
RESULT_SCHEMA = "simworld-semantic-asset-index-job-result/v1"
RECEIPT_SCHEMA = "simworld-semantic-asset-index-job-preparation-receipt/v1"
BOOTSTRAP_RECEIPT_SCHEMA = "simworld-ue-asset-bootstrap-receipt/v1"
OBJECT_MANIFEST_SCHEMA = "simworld-ue-object-manifest/v2"
OBJECT_FILTER_REVISION = "simworld-object-filter/2"
REGISTRY_AUDIT_SCHEMA = "simworld-ue-asset-registry-audit/v1"
CAPABILITY_INVENTORY_SCHEMA = "simworld-ue-content-capabilities/v1"

MAX_RECEIPT_BYTES = 2 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_RECIPE_BYTES = 128 * 1024
MAX_OBJECTS = 100_000
MAX_SAFE_INTEGER = 9_007_199_254_740_991

SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
SHA256_REVISION_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
SAFE_REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+\-]{2,239}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+\-]{0,239}$")
SAFE_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,127}$")
ASSET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,239}$")
UE_NAME_RE = re.compile(r"^[A-Za-z0-9_+\-]{1,240}$")
SOURCE_PACK_RE = re.compile(r"^[A-Za-z0-9_+\-]{1,128}$")
UE_PATH_RE = re.compile(
    r"^(?P<package>/Game(?:/[A-Za-z0-9_+\-]+)+)\.(?P<name>[A-Za-z0-9_+\-]{1,240})$"
)
OUTPUT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
APPROVAL_RE = re.compile(
    r"^(?:APPROVAL|CHANGE|TICKET|VISTA)-[A-Za-z0-9][A-Za-z0-9._-]{2,95}$"
)
PROVIDER_SNAPSHOT_RE = re.compile(
    r"^provider-snapshot:[A-Za-z0-9][A-Za-z0-9._:@/+\-]{0,221}$"
)
ASSET_SNAPSHOT_RE = re.compile(
    r"^asset-snapshot-[A-Za-z0-9][A-Za-z0-9._:@/+\-]{0,223}$"
)
FLOATING_TOKENS = frozenset({"dev", "head", "latest", "main", "master", "trunk", "unknown"})
OBJECT_TYPES = frozenset({"Blueprint", "StaticMesh"})
EXPECTED_FILTER_TAGS = ["asset_registry", "actual_object_candidate", "objects_only"]
BOOTSTRAP_FILENAMES = frozenset(
    {"registry-audit.json", "object-manifest.json", "content-capabilities.json"}
)
CAPABILITY_GROUPS = frozenset(
    {
        "character_blueprints",
        "animation_clips",
        "skeletal_meshes",
        "animation_blueprints",
        "ik_control_rigs",
        "skeletons",
    }
)


class SemanticIndexJobError(RuntimeError):
    """A bounded, public-safe preparation failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        pointer: str | None = None,
        committed: bool = False,
        durability_uncertain: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.pointer = pointer
        self.committed = committed
        self.durability_uncertain = durability_uncertain

    def public_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.pointer:
            value["pointer"] = self.pointer
        if self.committed:
            value["committed"] = True
        if self.durability_uncertain:
            value["durability_uncertain"] = True
        return value


def fail(
    code: str,
    message: str,
    *,
    pointer: str | None = None,
    committed: bool = False,
    durability_uncertain: bool = False,
) -> None:
    raise SemanticIndexJobError(
        code,
        message,
        pointer=pointer,
        committed=committed,
        durability_uncertain=durability_uncertain,
    )


def canonical_json(value: Any, *, pretty: bool = True) -> bytes:
    if pretty:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return text.encode("utf-8")


def json_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value, pretty=False)).hexdigest()


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _strict_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            fail(
                "SEMANTIC_INDEX_JSON_DUPLICATE_KEY",
                "Input JSON contains a duplicate object key",
                pointer="$",
            )
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    fail(
        "SEMANTIC_INDEX_JSON_NONFINITE",
        "Input JSON contains a non-finite number",
        pointer=value,
    )


def _strict_json(raw: bytes, *, pointer: str) -> Any:
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_strict_pairs,
            parse_constant=_reject_json_constant,
        )
    except SemanticIndexJobError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        fail(
            "SEMANTIC_INDEX_JSON_INVALID",
            "Input must be bounded UTF-8 JSON",
            pointer=pointer,
        )


def _require_exact_keys(
    value: Any,
    *,
    required: set[str] | frozenset[str],
    pointer: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        fail("SEMANTIC_INDEX_SCHEMA_INVALID", "Expected a JSON object", pointer=pointer)
    missing = sorted(set(required) - set(value))
    extra = sorted(set(value) - set(required))
    if missing or extra:
        fail(
            "SEMANTIC_INDEX_SCHEMA_INVALID",
            "JSON object does not match the closed contract",
            pointer=pointer,
        )
    return value


def _bounded_integer(value: Any, *, minimum: int, maximum: int, pointer: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        fail(
            "SEMANTIC_INDEX_SCHEMA_INVALID",
            f"Expected an integer from {minimum} through {maximum}",
            pointer=pointer,
        )
    return value


def _safe_string(value: Any, *, pattern: re.Pattern[str], pointer: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        fail(
            "SEMANTIC_INDEX_SCHEMA_INVALID",
            "Expected a bounded safe identifier",
            pointer=pointer,
        )
    return value


def _sha256(value: Any, *, pointer: str, prefixed: bool = False) -> str:
    pattern = SHA256_REVISION_RE if prefixed else SHA256_RE
    if not isinstance(value, str) or not pattern.fullmatch(value):
        fail(
            "SEMANTIC_INDEX_SCHEMA_INVALID",
            "Expected a lowercase SHA-256 binding",
            pointer=pointer,
        )
    return value


def _reject_floating_tokens(value: str, *, pointer: str) -> str:
    tokens = set(filter(None, re.split(r"[:/@._+\-]+", value.lower())))
    if tokens.intersection(FLOATING_TOKENS):
        fail(
            "SEMANTIC_INDEX_REVISION_FLOATING",
            "Revision must be immutable and must not use a floating label",
            pointer=pointer,
        )
    return value


def _pinned_revision(value: Any, *, pointer: str) -> str:
    revision = _safe_string(value, pattern=SAFE_REVISION_RE, pointer=pointer)
    return _reject_floating_tokens(revision, pointer=pointer)


def _rfc3339(value: Any, *, pointer: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        fail("SEMANTIC_INDEX_SCHEMA_INVALID", "Expected a bounded timestamp", pointer=pointer)
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        fail("SEMANTIC_INDEX_SCHEMA_INVALID", "Expected an RFC3339 timestamp", pointer=pointer)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        fail("SEMANTIC_INDEX_SCHEMA_INVALID", "Timestamp must include a timezone", pointer=pointer)
    return value


def _reject_symlink_components(path: Path, *, pointer: str) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            metadata = os.lstat(current)
        except OSError:
            fail(
                "SEMANTIC_INDEX_INPUT_UNSAFE",
                "Input path contains a missing or inaccessible component",
                pointer=pointer,
            )
        if stat.S_ISLNK(metadata.st_mode):
            fail(
                "SEMANTIC_INDEX_INPUT_UNSAFE",
                "Input path must not traverse symlinks",
                pointer=pointer,
            )


def _stable_file_metadata(metadata: os.stat_result) -> tuple[int, ...]:
    """Return every security- and stability-relevant regular-file field."""

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


def _secure_read(path: Path, *, max_bytes: int, pointer: str) -> bytes:
    if not path.is_absolute() or path != Path(os.path.abspath(path)):
        fail(
            "SEMANTIC_INDEX_INPUT_UNSAFE",
            "Input path must be absolute and normalized",
            pointer=pointer,
        )
    _reject_symlink_components(path, pointer=pointer)
    try:
        before = os.lstat(path)
    except OSError:
        fail("SEMANTIC_INDEX_INPUT_UNSAFE", "Input file is unavailable", pointer=pointer)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_uid != os.geteuid()
        or stat.S_IMODE(before.st_mode) != 0o600
        or not 2 <= before.st_size <= max_bytes
    ):
        fail(
            "SEMANTIC_INDEX_INPUT_UNSAFE",
            "Input must be a current-user-owned mode-0600 regular file with one link",
            pointer=pointer,
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        fail("SEMANTIC_INDEX_INPUT_UNSAFE", "Input cannot be opened safely", pointer=pointer)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) != 0o600
            or _stable_file_metadata(opened) != _stable_file_metadata(before)
        ):
            fail(
                "SEMANTIC_INDEX_INPUT_CHANGED",
                "Input metadata changed while it was opened",
                pointer=pointer,
            )
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    raw = b"".join(chunks)
    if (
        remaining
        or len(raw) != before.st_size
        or _stable_file_metadata(after) != _stable_file_metadata(before)
    ):
        fail(
            "SEMANTIC_INDEX_INPUT_CHANGED",
            "Input changed while it was read",
            pointer=pointer,
        )
    return raw


def _secure_read_json(path: Path, *, max_bytes: int, pointer: str) -> tuple[bytes, Any]:
    raw = _secure_read(path, max_bytes=max_bytes, pointer=pointer)
    return raw, _strict_json(raw, pointer=pointer)


def _validate_source_binding(value: Any) -> dict[str, Any]:
    binding = _require_exact_keys(
        value,
        required={"project", "content", "archive"},
        pointer="$/source_binding",
    )
    project = _require_exact_keys(
        binding["project"],
        required={"name", "revision", "engine_version"},
        pointer="$/source_binding/project",
    )
    _safe_string(project["name"], pattern=SAFE_SLUG_RE, pointer="$/source_binding/project/name")
    _pinned_revision(project["revision"], pointer="$/source_binding/project/revision")
    if not isinstance(project["engine_version"], str) or not 1 <= len(project["engine_version"]) <= 160:
        fail(
            "SEMANTIC_INDEX_SCHEMA_INVALID",
            "Engine version must be a bounded string",
            pointer="$/source_binding/project/engine_version",
        )

    content = _require_exact_keys(
        binding["content"],
        required={"mount_point", "revision"},
        pointer="$/source_binding/content",
    )
    if content["mount_point"] != "/Game":
        fail(
            "SEMANTIC_INDEX_SCHEMA_INVALID",
            "Only the verified /Game mount is accepted",
            pointer="$/source_binding/content/mount_point",
        )
    _sha256(content["revision"], pointer="$/source_binding/content/revision", prefixed=True)

    archive = _require_exact_keys(
        binding["archive"],
        required={
            "receipt_schema",
            "receipt_sha256",
            "repository",
            "repository_type",
            "revision",
            "filename",
            "size_bytes",
            "sha256",
            "verified",
            "expected_project_revision",
            "expected_content_revision",
        },
        pointer="$/source_binding/archive",
    )
    if archive["receipt_schema"] != "vista-simworld-archive-receipt/v1":
        fail(
            "SEMANTIC_INDEX_SCHEMA_INVALID",
            "Archive receipt schema is unsupported",
            pointer="$/source_binding/archive/receipt_schema",
        )
    _sha256(archive["receipt_sha256"], pointer="$/source_binding/archive/receipt_sha256")
    _safe_string(archive["repository"], pattern=SAFE_ID_RE, pointer="$/source_binding/archive/repository")
    if archive["repository_type"] not in {"dataset", "model"}:
        fail(
            "SEMANTIC_INDEX_SCHEMA_INVALID",
            "Archive repository type is unsupported",
            pointer="$/source_binding/archive/repository_type",
        )
    _pinned_revision(archive["revision"], pointer="$/source_binding/archive/revision")
    _safe_string(archive["filename"], pattern=SAFE_ID_RE, pointer="$/source_binding/archive/filename")
    _bounded_integer(
        archive["size_bytes"], minimum=1, maximum=MAX_SAFE_INTEGER, pointer="$/source_binding/archive/size_bytes"
    )
    _sha256(archive["sha256"], pointer="$/source_binding/archive/sha256")
    if archive["verified"] is not True:
        fail(
            "SEMANTIC_INDEX_BOOTSTRAP_UNVERIFIED",
            "Archive binding must remain verified",
            pointer="$/source_binding/archive/verified",
        )
    _pinned_revision(
        archive["expected_project_revision"],
        pointer="$/source_binding/archive/expected_project_revision",
    )
    _sha256(
        archive["expected_content_revision"],
        pointer="$/source_binding/archive/expected_content_revision",
        prefixed=True,
    )
    if project["revision"] != archive["expected_project_revision"]:
        fail(
            "SEMANTIC_INDEX_PROJECT_REVISION_MISMATCH",
            "Project revision differs from the verified archive binding",
            pointer="$/source_binding/project/revision",
        )
    if content["revision"] != archive["expected_content_revision"]:
        fail(
            "SEMANTIC_INDEX_CONTENT_REVISION_MISMATCH",
            "Content revision differs from the verified archive binding",
            pointer="$/source_binding/content/revision",
        )
    return binding


def validate_bootstrap_receipt(value: Any) -> dict[str, Any]:
    receipt = _require_exact_keys(
        value,
        required={
            "schema",
            "source_binding",
            "registry_audit_sha256",
            "object_manifest_revision",
            "capability_inventory_revision",
            "object_count",
            "capability_counts",
            "files",
            "bundle_complete",
            "snapshot_complete",
            "bundle_revision",
        },
        pointer="$",
    )
    if receipt["schema"] != BOOTSTRAP_RECEIPT_SCHEMA:
        fail(
            "SEMANTIC_INDEX_BOOTSTRAP_SCHEMA_INVALID",
            "Bootstrap receipt schema is unsupported",
            pointer="$/schema",
        )
    _validate_source_binding(receipt["source_binding"])
    _sha256(receipt["registry_audit_sha256"], pointer="$/registry_audit_sha256")
    _sha256(receipt["object_manifest_revision"], pointer="$/object_manifest_revision", prefixed=True)
    _sha256(
        receipt["capability_inventory_revision"],
        pointer="$/capability_inventory_revision",
        prefixed=True,
    )
    _bounded_integer(receipt["object_count"], minimum=1, maximum=MAX_OBJECTS, pointer="$/object_count")
    capability_counts = _require_exact_keys(
        receipt["capability_counts"],
        required=CAPABILITY_GROUPS,
        pointer="$/capability_counts",
    )
    for name, count in capability_counts.items():
        _bounded_integer(count, minimum=0, maximum=MAX_OBJECTS, pointer=f"$/capability_counts/{name}")
    files = _require_exact_keys(receipt["files"], required=BOOTSTRAP_FILENAMES, pointer="$/files")
    for filename, descriptor_value in files.items():
        descriptor = _require_exact_keys(
            descriptor_value,
            required={"bytes", "sha256"},
            pointer=f"$/files/{filename}",
        )
        _bounded_integer(
            descriptor["bytes"], minimum=2, maximum=MAX_MANIFEST_BYTES, pointer=f"$/files/{filename}/bytes"
        )
        _sha256(descriptor["sha256"], pointer=f"$/files/{filename}/sha256")
    if receipt["bundle_complete"] is not True or receipt["snapshot_complete"] is not False:
        fail(
            "SEMANTIC_INDEX_BOOTSTRAP_STATE_INVALID",
            "Bootstrap must be complete while its semantic snapshot remains incomplete",
            pointer="$",
        )
    _sha256(receipt["bundle_revision"], pointer="$/bundle_revision", prefixed=True)
    revision_basis = dict(receipt)
    revision_basis.pop("bundle_revision")
    expected = "sha256:" + json_sha256(revision_basis)
    if receipt["bundle_revision"] != expected:
        fail(
            "SEMANTIC_INDEX_BOOTSTRAP_REVISION_INVALID",
            "Bootstrap bundle revision does not match its canonical contract",
            pointer="$/bundle_revision",
        )
    return receipt


def _sorted_unique_strings(value: Any, *, pointer: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not all(isinstance(item, str) and SAFE_SLUG_RE.fullmatch(item) for item in value)
        or value != sorted(set(value))
    ):
        fail(
            "SEMANTIC_INDEX_SCHEMA_INVALID",
            "Expected a sorted unique list of bounded identifiers",
            pointer=pointer,
        )
    return value


def validate_object_manifest(value: Any, *, source_binding: Mapping[str, Any]) -> dict[str, Any]:
    manifest = _require_exact_keys(
        value,
        required={
            "schema",
            "schema_version",
            "manifest_kind",
            "manifest_revision",
            "generated_at_utc",
            "source_binding",
            "source_registry_audit",
            "count",
            "packs",
            "asset_class_counts",
            "object_filter_audit",
            "filter_policy",
            "assets",
        },
        pointer="$",
    )
    if (
        manifest["schema"] != OBJECT_MANIFEST_SCHEMA
        or manifest["schema_version"] != "2.0"
        or manifest["manifest_kind"] != "ue_asset_registry_actual_object_candidates"
    ):
        fail(
            "SEMANTIC_INDEX_MANIFEST_SCHEMA_INVALID",
            "Object manifest schema or kind is unsupported",
            pointer="$",
        )
    if manifest["source_binding"] != source_binding:
        fail(
            "SEMANTIC_INDEX_SOURCE_BINDING_MISMATCH",
            "Object manifest and bootstrap receipt bind different sources",
            pointer="$/source_binding",
        )
    _validate_source_binding(manifest["source_binding"])
    _rfc3339(manifest["generated_at_utc"], pointer="$/generated_at_utc")
    source_audit = _require_exact_keys(
        manifest["source_registry_audit"],
        required={"schema", "sha256", "row_count"},
        pointer="$/source_registry_audit",
    )
    if source_audit["schema"] != REGISTRY_AUDIT_SCHEMA:
        fail(
            "SEMANTIC_INDEX_MANIFEST_SCHEMA_INVALID",
            "Registry audit schema is unsupported",
            pointer="$/source_registry_audit/schema",
        )
    _sha256(source_audit["sha256"], pointer="$/source_registry_audit/sha256")
    _bounded_integer(
        source_audit["row_count"], minimum=1, maximum=MAX_OBJECTS, pointer="$/source_registry_audit/row_count"
    )
    assets = manifest["assets"]
    if not isinstance(assets, list) or not 1 <= len(assets) <= MAX_OBJECTS:
        fail(
            "SEMANTIC_INDEX_MANIFEST_EMPTY",
            "Object candidate manifest must contain a bounded non-empty asset list",
            pointer="$/assets",
        )
    if manifest["count"] != len(assets):
        fail(
            "SEMANTIC_INDEX_MANIFEST_COUNT_MISMATCH",
            "Object manifest count does not match its asset list",
            pointer="$/count",
        )

    content_revision = source_binding["content"]["revision"]
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    class_counts: dict[str, int] = {}
    pack_counts: dict[str, int] = {}
    previous_key: tuple[str, str, str] | None = None
    for index, raw_asset in enumerate(assets):
        pointer = f"$/assets/{index}"
        asset = _require_exact_keys(
            raw_asset,
            required={
                "asset_id",
                "ue_name",
                "ue_path",
                "asset_type",
                "source_pack",
                "indexed",
                "filter_tags",
                "content_revision",
            },
            pointer=pointer,
        )
        asset_id = _safe_string(asset["asset_id"], pattern=ASSET_ID_RE, pointer=pointer + "/asset_id")
        ue_name = _safe_string(asset["ue_name"], pattern=UE_NAME_RE, pointer=pointer + "/ue_name")
        if not isinstance(asset["ue_path"], str):
            fail("SEMANTIC_INDEX_SCHEMA_INVALID", "UE path must be a string", pointer=pointer + "/ue_path")
        path_match = UE_PATH_RE.fullmatch(asset["ue_path"])
        if path_match is None or path_match.group("name") != ue_name:
            fail(
                "SEMANTIC_INDEX_SCHEMA_INVALID",
                "UE object path must be an unambiguous /Game package.object path",
                pointer=pointer + "/ue_path",
            )
        if asset["asset_type"] not in OBJECT_TYPES:
            fail(
                "SEMANTIC_INDEX_NON_OBJECT_ASSET",
                "Only Blueprint and StaticMesh candidates may enter this job",
                pointer=pointer + "/asset_type",
            )
        source_pack = _safe_string(
            asset["source_pack"], pattern=SOURCE_PACK_RE, pointer=pointer + "/source_pack"
        )
        if asset["indexed"] is not False or asset["filter_tags"] != EXPECTED_FILTER_TAGS:
            fail(
                "SEMANTIC_INDEX_MANIFEST_STATE_INVALID",
                "Bootstrap assets must remain unindexed object candidates",
                pointer=pointer,
            )
        if asset["content_revision"] != content_revision:
            fail(
                "SEMANTIC_INDEX_CONTENT_REVISION_MISMATCH",
                "Asset content revision differs from the source binding",
                pointer=pointer + "/content_revision",
            )
        if asset_id in seen_ids or asset["ue_path"] in seen_paths:
            fail(
                "SEMANTIC_INDEX_MANIFEST_DUPLICATE",
                "Object manifest contains a duplicate asset identity",
                pointer=pointer,
            )
        seen_ids.add(asset_id)
        seen_paths.add(asset["ue_path"])
        sort_key = (source_pack, asset["ue_path"], asset_id)
        if previous_key is not None and sort_key <= previous_key:
            fail(
                "SEMANTIC_INDEX_MANIFEST_ORDER_INVALID",
                "Object candidates must use deterministic source/path/id order",
                pointer=pointer,
            )
        previous_key = sort_key
        class_counts[asset["asset_type"]] = class_counts.get(asset["asset_type"], 0) + 1
        pack_counts[source_pack] = pack_counts.get(source_pack, 0) + 1

    if manifest["packs"] != dict(sorted(pack_counts.items())):
        fail(
            "SEMANTIC_INDEX_MANIFEST_COUNT_MISMATCH",
            "Object manifest pack counts do not match its assets",
            pointer="$/packs",
        )
    if manifest["asset_class_counts"] != dict(sorted(class_counts.items())):
        fail(
            "SEMANTIC_INDEX_MANIFEST_COUNT_MISMATCH",
            "Object manifest class counts do not match its assets",
            pointer="$/asset_class_counts",
        )

    audit = _require_exact_keys(
        manifest["object_filter_audit"],
        required={
            "registry_row_count",
            "object_class_row_count",
            "accepted_count",
            "rejected_count",
            "reject_counts",
        },
        pointer="$/object_filter_audit",
    )
    registry_count = _bounded_integer(
        audit["registry_row_count"],
        minimum=len(assets),
        maximum=MAX_OBJECTS,
        pointer="$/object_filter_audit/registry_row_count",
    )
    _bounded_integer(
        audit["object_class_row_count"],
        minimum=len(assets),
        maximum=registry_count,
        pointer="$/object_filter_audit/object_class_row_count",
    )
    if (
        audit["accepted_count"] != len(assets)
        or audit["rejected_count"] != registry_count - len(assets)
    ):
        fail(
            "SEMANTIC_INDEX_MANIFEST_COUNT_MISMATCH",
            "Object filter audit counts are inconsistent",
            pointer="$/object_filter_audit",
        )
    if source_audit["row_count"] != registry_count:
        fail(
            "SEMANTIC_INDEX_MANIFEST_COUNT_MISMATCH",
            "Source registry and filter audit counts differ",
            pointer="$/source_registry_audit/row_count",
        )
    reject_counts = audit["reject_counts"]
    if (
        not isinstance(reject_counts, dict)
        or not all(isinstance(key, str) and SAFE_SLUG_RE.fullmatch(key) for key in reject_counts)
        or not all(
            isinstance(count, int) and not isinstance(count, bool) and count >= 0
            for count in reject_counts.values()
        )
        or sum(reject_counts.values()) != audit["rejected_count"]
    ):
        fail(
            "SEMANTIC_INDEX_MANIFEST_COUNT_MISMATCH",
            "Object filter rejection counts are inconsistent",
            pointer="$/object_filter_audit/reject_counts",
        )

    filter_policy = _require_exact_keys(
        manifest["filter_policy"],
        required={
            "revision",
            "include_classes",
            "capability_classes_excluded_from_semantic_index",
            "character_blueprints_separated_by_filter",
            "noise_segment_tokens",
            "surface_tokens",
            "positive_object_tokens",
            "helper_tokens",
            "character_tokens",
            "character_roots",
        },
        pointer="$/filter_policy",
    )
    if (
        filter_policy["revision"] != OBJECT_FILTER_REVISION
        or filter_policy["include_classes"] != sorted(OBJECT_TYPES)
        or filter_policy["character_blueprints_separated_by_filter"] is not True
    ):
        fail(
            "SEMANTIC_INDEX_FILTER_POLICY_INVALID",
            "Object filter policy differs from the accepted bootstrap revision",
            pointer="$/filter_policy",
        )
    for key in (
        "capability_classes_excluded_from_semantic_index",
        "noise_segment_tokens",
        "surface_tokens",
        "positive_object_tokens",
        "helper_tokens",
        "character_tokens",
        "character_roots",
    ):
        _sorted_unique_strings(filter_policy[key], pointer=f"$/filter_policy/{key}")

    _sha256(manifest["manifest_revision"], pointer="$/manifest_revision", prefixed=True)
    revision_basis = {
        "schema": manifest["schema"],
        "source_binding": manifest["source_binding"],
        "filter_policy": manifest["filter_policy"],
        "assets": manifest["assets"],
    }
    if manifest["manifest_revision"] != "sha256:" + json_sha256(revision_basis):
        fail(
            "SEMANTIC_INDEX_MANIFEST_REVISION_INVALID",
            "Object manifest revision does not match its canonical content",
            pointer="$/manifest_revision",
        )
    return manifest


def validate_recipe(value: Any) -> dict[str, Any]:
    recipe = _require_exact_keys(
        value,
        required={"schema", "caption", "embedding", "storage", "limits"},
        pointer="$",
    )
    if recipe["schema"] != RECIPE_SCHEMA:
        fail(
            "SEMANTIC_INDEX_RECIPE_SCHEMA_INVALID",
            "Semantic index recipe schema is unsupported",
            pointer="$/schema",
        )
    caption = _require_exact_keys(
        recipe["caption"],
        required={
            "provider_id",
            "model_id",
            "model_revision",
            "prompt_revision",
            "output_schema_revision",
            "render_recipe_revision",
            "views_per_asset",
            "image_width_px",
            "image_height_px",
            "max_output_tokens_per_asset",
            "temperature_milli",
        },
        pointer="$/caption",
    )
    _safe_string(caption["provider_id"], pattern=SAFE_ID_RE, pointer="$/caption/provider_id")
    _safe_string(caption["model_id"], pattern=SAFE_ID_RE, pointer="$/caption/model_id")
    model_revision = _pinned_revision(caption["model_revision"], pointer="$/caption/model_revision")
    if not (
        SHA256_REVISION_RE.fullmatch(model_revision)
        or PROVIDER_SNAPSHOT_RE.fullmatch(model_revision)
    ):
        fail(
            "SEMANTIC_INDEX_CAPTION_REVISION_INVALID",
            "Caption model revision must be a SHA-256 or provider-snapshot binding",
            pointer="$/caption/model_revision",
        )
    for key in ("prompt_revision", "output_schema_revision", "render_recipe_revision"):
        _sha256(caption[key], pointer=f"$/caption/{key}", prefixed=True)
    _bounded_integer(
        caption["views_per_asset"],
        minimum=1,
        maximum=32,
        pointer="$/caption/views_per_asset",
    )
    _bounded_integer(
        caption["image_width_px"],
        minimum=64,
        maximum=8192,
        pointer="$/caption/image_width_px",
    )
    _bounded_integer(
        caption["image_height_px"],
        minimum=64,
        maximum=8192,
        pointer="$/caption/image_height_px",
    )
    _bounded_integer(
        caption["max_output_tokens_per_asset"],
        minimum=1,
        maximum=65_536,
        pointer="$/caption/max_output_tokens_per_asset",
    )
    _bounded_integer(
        caption["temperature_milli"],
        minimum=0,
        maximum=2_000,
        pointer="$/caption/temperature_milli",
    )

    embedding = _require_exact_keys(
        recipe["embedding"],
        required={
            "recipe_revision",
            "dense_model_id",
            "dense_model_revision",
            "dense_size",
            "sparse_model_id",
            "sparse_model_revision",
            "batch_size",
        },
        pointer="$/embedding",
    )
    _pinned_revision(embedding["recipe_revision"], pointer="$/embedding/recipe_revision")
    _safe_string(embedding["dense_model_id"], pattern=SAFE_ID_RE, pointer="$/embedding/dense_model_id")
    _sha256(embedding["dense_model_revision"], pointer="$/embedding/dense_model_revision", prefixed=True)
    _bounded_integer(embedding["dense_size"], minimum=1, maximum=65_536, pointer="$/embedding/dense_size")
    _safe_string(embedding["sparse_model_id"], pattern=SAFE_ID_RE, pointer="$/embedding/sparse_model_id")
    _sha256(embedding["sparse_model_revision"], pointer="$/embedding/sparse_model_revision", prefixed=True)
    _bounded_integer(embedding["batch_size"], minimum=1, maximum=4096, pointer="$/embedding/batch_size")

    storage = _require_exact_keys(
        recipe["storage"],
        required={
            "postgres_schema_revision",
            "qdrant_collection",
            "qdrant_dense_vector_name",
            "qdrant_sparse_vector_name",
        },
        pointer="$/storage",
    )
    if storage["postgres_schema_revision"] != 2:
        fail(
            "SEMANTIC_INDEX_POSTGRES_SCHEMA_INVALID",
            "Production semantic jobs require PostgreSQL asset schema revision 2",
            pointer="$/storage/postgres_schema_revision",
        )
    collection = _safe_string(
        storage["qdrant_collection"], pattern=SAFE_SLUG_RE, pointer="$/storage/qdrant_collection"
    )
    _reject_floating_tokens(collection, pointer="$/storage/qdrant_collection")
    if collection.lower() in {"asset", "assets", "default", "latest", "main"}:
        fail(
            "SEMANTIC_INDEX_QDRANT_COLLECTION_UNPINNED",
            "Qdrant collection must be an explicitly versioned production identifier",
            pointer="$/storage/qdrant_collection",
        )
    _safe_string(
        storage["qdrant_dense_vector_name"], pattern=SAFE_SLUG_RE, pointer="$/storage/qdrant_dense_vector_name"
    )
    _safe_string(
        storage["qdrant_sparse_vector_name"], pattern=SAFE_SLUG_RE, pointer="$/storage/qdrant_sparse_vector_name"
    )
    if storage["qdrant_dense_vector_name"] == storage["qdrant_sparse_vector_name"]:
        fail(
            "SEMANTIC_INDEX_QDRANT_VECTOR_INVALID",
            "Dense and sparse vector names must differ",
            pointer="$/storage",
        )

    limits = _require_exact_keys(
        recipe["limits"],
        required={
            "max_assets",
            "max_rendered_views",
            "max_render_pixels",
            "max_total_caption_output_tokens",
            "max_catalog_record_bytes",
            "max_total_catalog_bytes",
            "max_postgres_rows",
            "max_qdrant_points",
            "max_dense_vector_payload_bytes_estimate",
        },
        pointer="$/limits",
    )
    limit_bounds = {
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
    for key, (minimum, maximum) in limit_bounds.items():
        _bounded_integer(limits[key], minimum=minimum, maximum=maximum, pointer=f"$/limits/{key}")
    return recipe


def validate_bootstrap_cross_references(
    *,
    receipt: Mapping[str, Any],
    registry_audit: Any,
    object_manifest: Mapping[str, Any],
    capability_inventory: Any,
) -> None:
    """Require canonical producer outputs plus registry/receipt reconciliation."""

    try:
        source_binding = receipt["source_binding"]
        project = source_binding["project"]
        content = source_binding["content"]
        archive = source_binding["archive"]
        validated_registry = bootstrap_contract.validate_registry_audit(
            registry_audit,
            expected_project_name=project["name"],
        )
        if validated_registry != registry_audit:
            fail(
                "SEMANTIC_INDEX_REGISTRY_CONTRACT_INVALID",
                "Registry audit is not the canonical validated producer output",
                pointer="registry-audit.json",
            )
        validated_manifest = bootstrap_contract.validate_object_manifest(object_manifest)
        validated_inventory = bootstrap_contract.validate_capability_inventory(
            capability_inventory
        )

        registry_members = {
            (f"{row['package']}.{row['name']}", row["class"])
            for row in validated_registry["assets"]
        }
        object_members = {
            (asset["ue_path"], asset["asset_type"])
            for asset in validated_manifest["assets"]
        }
        capability_members = {
            (candidate["ue_path"], candidate["asset_class"])
            for group in validated_inventory["groups"].values()
            for candidate in group["candidates"]
        }
        if not object_members.issubset(registry_members):
            fail(
                "SEMANTIC_INDEX_MANIFEST_REGISTRY_MISMATCH",
                "An object manifest path or class is absent from the exact registry audit",
                pointer="object-manifest.json",
            )
        if not capability_members.issubset(registry_members):
            fail(
                "SEMANTIC_INDEX_CAPABILITY_REGISTRY_MISMATCH",
                "A capability path or class is absent from the exact registry audit",
                pointer="content-capabilities.json",
            )
        object_paths = {path for path, _asset_class in object_members}
        capability_paths = {path for path, _asset_class in capability_members}
        if object_paths.intersection(capability_paths):
            fail(
                "SEMANTIC_INDEX_CAPABILITY_OBJECT_OVERLAP",
                "Capability candidates and semantic object candidates must be path-disjoint",
                pointer="bootstrap bundle",
            )

        binding = bootstrap_contract.SourceBinding(
            project_name=project["name"],
            project_revision=project["revision"],
            content_revision=content["revision"],
            archive=archive,
        )
        expected_manifest = bootstrap_contract.validate_object_manifest(
            bootstrap_contract.build_object_manifest(validated_registry, binding)
        )
        if validated_manifest != expected_manifest:
            fail(
                "SEMANTIC_INDEX_MANIFEST_REGISTRY_MISMATCH",
                "Object manifest is not the exact deterministic projection of the registry audit",
                pointer="object-manifest.json",
            )
        limit_per_group = validated_inventory.get("limit_per_group")
        if (
            isinstance(limit_per_group, bool)
            or not isinstance(limit_per_group, int)
            or not 1 <= limit_per_group <= bootstrap_contract.MAX_INVENTORY_LIMIT
        ):
            fail(
                "SEMANTIC_INDEX_CAPABILITY_CONTRACT_INVALID",
                "Capability inventory limit is outside the producer contract",
                pointer="content-capabilities.json",
            )
        expected_inventory = bootstrap_contract.validate_capability_inventory(
            bootstrap_contract.build_capability_inventory(
                validated_registry,
                binding,
                limit_per_group=limit_per_group,
            )
        )
        if validated_inventory != expected_inventory:
            fail(
                "SEMANTIC_INDEX_CAPABILITY_CONTRACT_INVALID",
                "Capability inventory is not the exact deterministic projection of the registry audit",
                pointer="content-capabilities.json",
            )
    except SemanticIndexJobError:
        raise
    except bootstrap_contract.BootstrapError:
        fail(
            "SEMANTIC_INDEX_BOOTSTRAP_CONTRACT_INVALID",
            "Bootstrap producer contract validation failed",
            pointer="bootstrap bundle",
        )
    except (AttributeError, KeyError, TypeError, ValueError, RecursionError):
        fail(
            "SEMANTIC_INDEX_BOOTSTRAP_CONTRACT_INVALID",
            "Bootstrap cross-reference structure is invalid",
            pointer="bootstrap bundle",
        )

    expected_capability_counts = {
        name: group["total_count"]
        for name, group in expected_inventory["groups"].items()
    }
    if receipt["capability_counts"] != expected_capability_counts:
        fail(
            "SEMANTIC_INDEX_CAPABILITY_COUNT_MISMATCH",
            "Bootstrap receipt capability counts differ from the canonical inventory",
            pointer="bootstrap-receipt.json",
        )
    if (
        receipt["object_count"] != expected_manifest["count"]
        or receipt["object_manifest_revision"] != expected_manifest["manifest_revision"]
        or receipt["capability_inventory_revision"]
        != expected_inventory["inventory_revision"]
    ):
        fail(
            "SEMANTIC_INDEX_BOOTSTRAP_CROSS_REFERENCE_MISMATCH",
            "Bootstrap receipt revisions or counts differ from canonical members",
            pointer="bootstrap-receipt.json",
        )


@dataclass(frozen=True)
class PreparedJob:
    job_bytes: bytes
    bootstrap_receipt_sha256: str
    object_manifest_sha256: str
    recipe_sha256: str
    pending_objects_sha256: str

    @property
    def job(self) -> dict[str, Any]:
        """Return a newly parsed, fully revalidated copy of the bound job."""

        return _validated_prepared_job(self)

    @property
    def job_sha256(self) -> str:
        return hashlib.sha256(self.job_bytes).hexdigest()


def _validated_prepared_job(prepared: PreparedJob) -> dict[str, Any]:
    """Fail closed if bytes or immutable identity scalars are inconsistent."""

    if (
        not isinstance(prepared.job_bytes, bytes)
        or not 2 <= len(prepared.job_bytes) <= 128 * 1024 * 1024
    ):
        fail(
            "SEMANTIC_INDEX_PREPARED_JOB_INVALID",
            "Prepared job bytes are missing or outside the publication bound",
        )
    job = _strict_json(prepared.job_bytes, pointer="prepared job bytes")
    job = _require_exact_keys(
        job,
        required={
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
        },
        pointer="$",
    )
    if job["schema"] != JOB_SCHEMA or canonical_json(job) != prepared.job_bytes:
        fail(
            "SEMANTIC_INDEX_PREPARED_JOB_INVALID",
            "Prepared job bytes are not the canonical supported job schema",
        )
    revision_basis = dict(job)
    revision_basis.pop("job_revision")
    if (
        not SHA256_REVISION_RE.fullmatch(str(job["job_revision"]))
        or job["job_revision"] != "sha256:" + json_sha256(revision_basis)
    ):
        fail(
            "SEMANTIC_INDEX_PREPARED_JOB_INVALID",
            "Prepared job canonical revision is invalid",
        )

    input_contract = _require_exact_keys(
        job["input_contract"],
        required={
            "bootstrap_receipt_schema",
            "bootstrap_receipt_sha256",
            "bootstrap_bundle_revision",
            "object_manifest_schema",
            "object_manifest_revision",
            "object_manifest_sha256",
            "object_count",
            "project",
            "content",
            "archive_receipt_sha256",
            "registry_audit_sha256",
            "manifest_kind",
            "bootstrap_bundle_complete",
            "candidate_manifest_reviewed",
            "catalog_complete",
            "snapshot_complete",
        },
        pointer="$/input_contract",
    )
    if (
        input_contract["bootstrap_receipt_schema"] != BOOTSTRAP_RECEIPT_SCHEMA
        or input_contract["object_manifest_schema"] != OBJECT_MANIFEST_SCHEMA
        or input_contract["bootstrap_receipt_sha256"]
        != prepared.bootstrap_receipt_sha256
        or input_contract["object_manifest_sha256"]
        != prepared.object_manifest_sha256
        or input_contract["manifest_kind"]
        != "asset_registry_object_candidates_not_catalog"
        or input_contract["bootstrap_bundle_complete"] is not True
        or input_contract["candidate_manifest_reviewed"] is not False
        or input_contract["catalog_complete"] is not False
        or input_contract["snapshot_complete"] is not False
    ):
        fail(
            "SEMANTIC_INDEX_PREPARED_JOB_INVALID",
            "Prepared job input identity or candidate state is inconsistent",
        )
    _sha256(
        input_contract["bootstrap_receipt_sha256"],
        pointer="$/input_contract/bootstrap_receipt_sha256",
    )
    _sha256(
        input_contract["bootstrap_bundle_revision"],
        pointer="$/input_contract/bootstrap_bundle_revision",
        prefixed=True,
    )
    _sha256(
        input_contract["object_manifest_revision"],
        pointer="$/input_contract/object_manifest_revision",
        prefixed=True,
    )
    _sha256(input_contract["object_manifest_sha256"], pointer="$/input_contract/object_manifest_sha256")
    _bounded_integer(
        input_contract["object_count"],
        minimum=1,
        maximum=MAX_OBJECTS,
        pointer="$/input_contract/object_count",
    )
    project = _require_exact_keys(
        input_contract["project"],
        required={"name", "revision", "engine_version"},
        pointer="$/input_contract/project",
    )
    content = _require_exact_keys(
        input_contract["content"],
        required={"mount_point", "revision"},
        pointer="$/input_contract/content",
    )
    _safe_string(project["name"], pattern=SAFE_SLUG_RE, pointer="$/input_contract/project/name")
    _pinned_revision(project["revision"], pointer="$/input_contract/project/revision")
    if (
        not isinstance(project["engine_version"], str)
        or not project["engine_version"].strip()
        or len(project["engine_version"]) > 160
        or any(ord(character) < 32 for character in project["engine_version"])
        or content["mount_point"] != "/Game"
    ):
        fail(
            "SEMANTIC_INDEX_PREPARED_JOB_INVALID",
            "Prepared job project or content identity is invalid",
        )
    _sha256(content["revision"], pointer="$/input_contract/content/revision", prefixed=True)

    recipe_contract = _require_exact_keys(
        job["recipe_contract"],
        required={
            "recipe_schema",
            "recipe_sha256",
            "caption_recipe_sha256",
            "caption",
            "embedding_recipe_sha256",
            "embedding",
            "storage",
        },
        pointer="$/recipe_contract",
    )
    if (
        recipe_contract["recipe_schema"] != RECIPE_SCHEMA
        or recipe_contract["recipe_sha256"] != prepared.recipe_sha256
        or json_sha256(recipe_contract["caption"])
        != recipe_contract["caption_recipe_sha256"]
        or json_sha256(recipe_contract["embedding"])
        != recipe_contract["embedding_recipe_sha256"]
    ):
        fail(
            "SEMANTIC_INDEX_PREPARED_JOB_INVALID",
            "Prepared job recipe identity is inconsistent",
        )
    _sha256(recipe_contract["recipe_sha256"], pointer="$/recipe_contract/recipe_sha256")
    _sha256(
        recipe_contract["caption_recipe_sha256"],
        pointer="$/recipe_contract/caption_recipe_sha256",
    )
    _sha256(
        recipe_contract["embedding_recipe_sha256"],
        pointer="$/recipe_contract/embedding_recipe_sha256",
    )
    validate_recipe(
        {
            "schema": RECIPE_SCHEMA,
            "caption": recipe_contract["caption"],
            "embedding": recipe_contract["embedding"],
            "storage": recipe_contract["storage"],
            "limits": job["resource_contract"].get("limits")
            if isinstance(job["resource_contract"], dict)
            else None,
        }
    )

    snapshot = _require_exact_keys(
        job["snapshot_target"],
        required={
            "asset_snapshot_revision",
            "postgres",
            "qdrant",
            "row_point_parity_required",
            "live_audit_receipt_required",
            "snapshot_complete",
        },
        pointer="$/snapshot_target",
    )
    snapshot_revision = _pinned_revision(
        snapshot["asset_snapshot_revision"], pointer="$/snapshot_target/asset_snapshot_revision"
    )
    if (
        ASSET_SNAPSHOT_RE.fullmatch(snapshot_revision) is None
        or snapshot["row_point_parity_required"] is not True
        or snapshot["live_audit_receipt_required"] is not True
        or snapshot["snapshot_complete"] is not False
    ):
        fail(
            "SEMANTIC_INDEX_PREPARED_JOB_INVALID",
            "Prepared job snapshot target is inconsistent",
        )
    postgres_target = _require_exact_keys(
        snapshot["postgres"],
        required={
            "schema_revision",
            "required_asset_snapshot_revision",
            "planned_candidate_rows",
            "observed_live",
        },
        pointer="$/snapshot_target/postgres",
    )
    qdrant_target = _require_exact_keys(
        snapshot["qdrant"],
        required={
            "collection",
            "dense_vector_name",
            "sparse_vector_name",
            "required_payload_asset_snapshot_revision",
            "planned_candidate_points",
            "observed_live",
        },
        pointer="$/snapshot_target/qdrant",
    )
    if (
        postgres_target["schema_revision"] != 2
        or postgres_target["required_asset_snapshot_revision"] != snapshot_revision
        or postgres_target["observed_live"] is not False
        or qdrant_target["required_payload_asset_snapshot_revision"]
        != snapshot_revision
        or qdrant_target["observed_live"] is not False
        or qdrant_target["collection"]
        != recipe_contract["storage"]["qdrant_collection"]
        or qdrant_target["dense_vector_name"]
        != recipe_contract["storage"]["qdrant_dense_vector_name"]
        or qdrant_target["sparse_vector_name"]
        != recipe_contract["storage"]["qdrant_sparse_vector_name"]
    ):
        fail(
            "SEMANTIC_INDEX_PREPARED_JOB_INVALID",
            "Prepared job storage snapshot bindings are inconsistent",
        )
    _bounded_integer(
        postgres_target["planned_candidate_rows"],
        minimum=1,
        maximum=MAX_OBJECTS,
        pointer="$/snapshot_target/postgres/planned_candidate_rows",
    )
    _bounded_integer(
        qdrant_target["planned_candidate_points"],
        minimum=1,
        maximum=MAX_OBJECTS,
        pointer="$/snapshot_target/qdrant/planned_candidate_points",
    )

    pending = _require_exact_keys(
        job["pending_objects"],
        required={"count", "selection", "assets_sha256", "assets"},
        pointer="$/pending_objects",
    )
    if (
        isinstance(pending["count"], bool)
        or not isinstance(pending["count"], int)
        or not 1 <= pending["count"] <= MAX_OBJECTS
        or pending["selection"]
        != "all_unindexed_objects_in_pinned_bootstrap_manifest"
        or not isinstance(pending["assets"], list)
        or len(pending["assets"]) != pending["count"]
        or pending["assets_sha256"] != prepared.pending_objects_sha256
        or pending["assets_sha256"] != json_sha256(pending["assets"])
        or pending["count"] != input_contract["object_count"]
        or postgres_target["planned_candidate_rows"] != pending["count"]
        or qdrant_target["planned_candidate_points"] != pending["count"]
        or any(
            not isinstance(asset, dict)
            or set(asset)
            != {
                "asset_id",
                "ue_name",
                "ue_path",
                "asset_type",
                "source_pack",
                "content_revision",
                "status",
            }
            or asset.get("status") != "pending_live_semantic_index"
            or asset.get("content_revision") != content["revision"]
            or not isinstance(asset.get("asset_id"), str)
            or not ASSET_ID_RE.fullmatch(asset["asset_id"])
            or not isinstance(asset.get("ue_name"), str)
            or not UE_NAME_RE.fullmatch(asset["ue_name"])
            or not isinstance(asset.get("ue_path"), str)
            or UE_PATH_RE.fullmatch(asset["ue_path"]) is None
            or asset.get("asset_type") not in OBJECT_TYPES
            or not isinstance(asset.get("source_pack"), str)
            or not SOURCE_PACK_RE.fullmatch(asset["source_pack"])
            for asset in pending["assets"]
        )
    ):
        fail(
            "SEMANTIC_INDEX_PREPARED_JOB_INVALID",
            "Prepared job pending object set is inconsistent",
        )
    if (
        len({asset["asset_id"] for asset in pending["assets"]}) != pending["count"]
        or len({asset["ue_path"] for asset in pending["assets"]}) != pending["count"]
    ):
        fail(
            "SEMANTIC_INDEX_PREPARED_JOB_INVALID",
            "Prepared job pending object identities are not unique",
        )

    resource = _require_exact_keys(
        job["resource_contract"],
        required={"limits", "estimates", "cost_authorized"},
        pointer="$/resource_contract",
    )
    estimates = _require_exact_keys(
        resource["estimates"],
        required={
            "pending_assets",
            "rendered_views_upper_bound",
            "render_pixels_upper_bound",
            "caption_output_tokens_upper_bound",
            "catalog_json_bytes_upper_bound",
            "embedding_batches_upper_bound",
            "postgres_rows_upper_bound",
            "qdrant_points_upper_bound",
            "dense_vector_payload_bytes_estimate",
            "dense_vector_estimate_excludes",
        },
        pointer="$/resource_contract/estimates",
    )
    if resource["cost_authorized"] is not False:
        fail(
            "SEMANTIC_INDEX_PREPARED_JOB_INVALID",
            "Prepared job resource or cost state is inconsistent",
        )
    caption = recipe_contract["caption"]
    embedding = recipe_contract["embedding"]
    limits = resource["limits"]
    expected_estimates = {
        "pending_assets": pending["count"],
        "rendered_views_upper_bound": pending["count"] * caption["views_per_asset"],
        "render_pixels_upper_bound": pending["count"]
        * caption["views_per_asset"]
        * caption["image_width_px"]
        * caption["image_height_px"],
        "caption_output_tokens_upper_bound": pending["count"]
        * caption["max_output_tokens_per_asset"],
        "catalog_json_bytes_upper_bound": pending["count"]
        * limits["max_catalog_record_bytes"],
        "embedding_batches_upper_bound": math.ceil(
            pending["count"] / embedding["batch_size"]
        ),
        "postgres_rows_upper_bound": pending["count"],
        "qdrant_points_upper_bound": pending["count"],
        "dense_vector_payload_bytes_estimate": pending["count"]
        * embedding["dense_size"]
        * 4,
        "dense_vector_estimate_excludes": [
            "qdrant_payload",
            "sparse_vectors",
            "indexes",
            "replication",
            "storage_engine_overhead",
        ],
    }
    numeric_estimates = set(expected_estimates) - {"dense_vector_estimate_excludes"}
    if (
        any(type(estimates[key]) is not int for key in numeric_estimates)
        or estimates != expected_estimates
    ):
        fail(
            "SEMANTIC_INDEX_PREPARED_JOB_INVALID",
            "Prepared job resource estimates differ from the pending set",
        )
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
    if any(estimates[estimate] > limits[limit] for estimate, limit in estimate_limits.items()):
        fail(
            "SEMANTIC_INDEX_PREPARED_JOB_INVALID",
            "Prepared job exceeds its resource limit contract",
        )

    if (
        not isinstance(job["live_gates"], list)
        or len(job["live_gates"]) != 7
        or any(
            not isinstance(gate, dict)
            or set(gate) != {"gate", "category", "status", "required_evidence"}
            or gate.get("status") != "pending"
            for gate in job["live_gates"]
        )
    ):
        fail(
            "SEMANTIC_INDEX_PREPARED_JOB_INVALID",
            "Prepared job live gates are inconsistent",
        )
    execution = _require_exact_keys(
        job["execution_contract"],
        required={
            "prepared_only",
            "default_behavior",
            "apply_effect",
            "explicit_operator_execution_required",
            "legacy_full_asset_index_runner_authorized",
            "reviewed_adapter_required_for_legacy_runner",
            "prohibited_production_default_args",
            "network_allowed_during_preparation",
            "unreal_allowed_during_preparation",
            "caption_provider_allowed_during_preparation",
            "postgres_allowed_during_preparation",
            "qdrant_allowed_during_preparation",
            "embedding_allowed_during_preparation",
        },
        pointer="$/execution_contract",
    )
    if (
        execution["prepared_only"] is not True
        or execution["default_behavior"] != "dry_run"
        or execution["apply_effect"] != "atomic_private_job_bundle_only"
        or execution["explicit_operator_execution_required"] is not True
        or execution["legacy_full_asset_index_runner_authorized"] is not False
        or execution["reviewed_adapter_required_for_legacy_runner"] is not True
        or execution["prohibited_production_default_args"]
        != ["--dangerously-bypass-approvals-and-sandbox"]
        or any(
            execution[key] is not False
            for key in (
                "network_allowed_during_preparation",
                "unreal_allowed_during_preparation",
                "caption_provider_allowed_during_preparation",
                "postgres_allowed_during_preparation",
                "qdrant_allowed_during_preparation",
                "embedding_allowed_during_preparation",
            )
        )
        or job["catalog_complete"] is not False
        or job["snapshot_complete"] is not False
    ):
        fail(
            "SEMANTIC_INDEX_PREPARED_JOB_INVALID",
            "Prepared job execution or completion state is inconsistent",
        )
    return job


def _assert_expected_pin(actual: Any, expected: Any, *, code: str, message: str, pointer: str) -> None:
    if actual != expected:
        fail(code, message, pointer=pointer)


def build_job(
    *,
    bootstrap_receipt_path: Path,
    object_manifest_path: Path,
    recipe_path: Path,
    expected_bootstrap_receipt_sha256: str,
    expected_bundle_revision: str,
    expected_object_manifest_sha256: str,
    expected_object_count: int,
    expected_project_revision: str,
    expected_content_revision: str,
    expected_recipe_sha256: str,
    asset_snapshot_revision: str,
) -> PreparedJob:
    _sha256(expected_bootstrap_receipt_sha256, pointer="--expected-bootstrap-receipt-sha256")
    _sha256(expected_bundle_revision, pointer="--expected-bundle-revision", prefixed=True)
    _sha256(expected_object_manifest_sha256, pointer="--expected-object-manifest-sha256")
    _bounded_integer(expected_object_count, minimum=1, maximum=MAX_OBJECTS, pointer="--expected-object-count")
    _pinned_revision(expected_project_revision, pointer="--expected-project-revision")
    _sha256(expected_content_revision, pointer="--expected-content-revision", prefixed=True)
    _sha256(expected_recipe_sha256, pointer="--expected-recipe-sha256")
    snapshot_revision = _pinned_revision(asset_snapshot_revision, pointer="--asset-snapshot-revision")
    if ASSET_SNAPSHOT_RE.fullmatch(snapshot_revision) is None:
        fail(
            "SEMANTIC_INDEX_SNAPSHOT_REVISION_INVALID",
            "Asset snapshot revision must use the asset-snapshot- namespace",
            pointer="--asset-snapshot-revision",
        )

    if bootstrap_receipt_path.parent != object_manifest_path.parent:
        fail(
            "SEMANTIC_INDEX_BOOTSTRAP_LAYOUT_INVALID",
            "Bootstrap receipt and object manifest must come from the same bundle directory",
            pointer="--object-manifest",
        )
    if (
        bootstrap_receipt_path.name != "bootstrap-receipt.json"
        or object_manifest_path.name != "object-manifest.json"
    ):
        fail(
            "SEMANTIC_INDEX_BOOTSTRAP_LAYOUT_INVALID",
            "Bootstrap inputs must retain their fixed bundle filenames",
            pointer="--bootstrap-receipt",
        )
    bundle_dir = bootstrap_receipt_path.parent
    if not bundle_dir.is_absolute() or bundle_dir != Path(os.path.abspath(bundle_dir)):
        fail(
            "SEMANTIC_INDEX_BOOTSTRAP_LAYOUT_INVALID",
            "Bootstrap bundle directory must be absolute and normalized",
            pointer="--bootstrap-receipt",
        )
    _reject_symlink_components(bundle_dir, pointer="--bootstrap-receipt")
    bundle_stat = os.lstat(bundle_dir)
    if (
        not stat.S_ISDIR(bundle_stat.st_mode)
        or bundle_stat.st_uid != os.geteuid()
        or stat.S_IMODE(bundle_stat.st_mode) != 0o700
    ):
        fail(
            "SEMANTIC_INDEX_BOOTSTRAP_LAYOUT_INVALID",
            "Bootstrap bundle directory must be current-user-owned mode 0700",
            pointer="--bootstrap-receipt",
        )

    receipt_raw, receipt_value = _secure_read_json(
        bootstrap_receipt_path,
        max_bytes=MAX_RECEIPT_BYTES,
        pointer="--bootstrap-receipt",
    )
    receipt_sha256 = hashlib.sha256(receipt_raw).hexdigest()
    _assert_expected_pin(
        receipt_sha256,
        expected_bootstrap_receipt_sha256,
        code="SEMANTIC_INDEX_BOOTSTRAP_RECEIPT_PIN_MISMATCH",
        message="Bootstrap receipt SHA-256 differs from the operator pin",
        pointer="--expected-bootstrap-receipt-sha256",
    )
    receipt = validate_bootstrap_receipt(receipt_value)
    _assert_expected_pin(
        receipt["bundle_revision"],
        expected_bundle_revision,
        code="SEMANTIC_INDEX_BUNDLE_PIN_MISMATCH",
        message="Bootstrap bundle revision differs from the operator pin",
        pointer="--expected-bundle-revision",
    )
    bootstrap_member_bytes: dict[str, bytes] = {}
    for member_name in ("registry-audit.json", "content-capabilities.json"):
        member_raw = _secure_read(
            bundle_dir / member_name,
            max_bytes=MAX_MANIFEST_BYTES,
            pointer=f"bootstrap member {member_name}",
        )
        member_descriptor = receipt["files"][member_name]
        if (
            len(member_raw) != member_descriptor["bytes"]
            or hashlib.sha256(member_raw).hexdigest() != member_descriptor["sha256"]
        ):
            fail(
                "SEMANTIC_INDEX_BOOTSTRAP_MEMBER_MISMATCH",
                "Bootstrap bundle member bytes do not match the receipt",
                pointer=member_name,
            )
        bootstrap_member_bytes[member_name] = member_raw
    registry_audit = _strict_json(
        bootstrap_member_bytes["registry-audit.json"],
        pointer="bootstrap member registry-audit.json",
    )
    if json_sha256(registry_audit) != receipt["registry_audit_sha256"]:
        fail(
            "SEMANTIC_INDEX_REGISTRY_BINDING_MISMATCH",
            "Registry audit canonical digest differs from the bootstrap receipt",
            pointer="registry-audit.json",
        )
    capability_inventory = _strict_json(
        bootstrap_member_bytes["content-capabilities.json"],
        pointer="bootstrap member content-capabilities.json",
    )
    if (
        not isinstance(capability_inventory, dict)
        or capability_inventory.get("schema") != CAPABILITY_INVENTORY_SCHEMA
        or "assets" in capability_inventory
        or capability_inventory.get("source_binding") != receipt["source_binding"]
        or capability_inventory.get("inventory_revision")
        != receipt["capability_inventory_revision"]
        or not isinstance(capability_inventory.get("limit_per_group"), int)
        or isinstance(capability_inventory.get("limit_per_group"), bool)
        or not isinstance(capability_inventory.get("groups"), dict)
    ):
        fail(
            "SEMANTIC_INDEX_CAPABILITY_SEPARATION_INVALID",
            "Capability inventory is not a separate revision-bound non-semantic inventory",
            pointer="content-capabilities.json",
        )
    capability_revision_basis = {
        "schema": capability_inventory["schema"],
        "source_binding": capability_inventory["source_binding"],
        "limit_per_group": capability_inventory["limit_per_group"],
        "groups": capability_inventory["groups"],
    }
    if capability_inventory["inventory_revision"] != "sha256:" + json_sha256(
        capability_revision_basis
    ):
        fail(
            "SEMANTIC_INDEX_CAPABILITY_SEPARATION_INVALID",
            "Capability inventory canonical revision is invalid",
            pointer="content-capabilities.json",
        )

    manifest_raw, manifest_value = _secure_read_json(
        object_manifest_path,
        max_bytes=MAX_MANIFEST_BYTES,
        pointer="--object-manifest",
    )
    manifest_sha256 = hashlib.sha256(manifest_raw).hexdigest()
    descriptor = receipt["files"]["object-manifest.json"]
    if len(manifest_raw) != descriptor["bytes"] or manifest_sha256 != descriptor["sha256"]:
        fail(
            "SEMANTIC_INDEX_MANIFEST_RECEIPT_MISMATCH",
            "Object manifest bytes do not match the bootstrap receipt",
            pointer="--object-manifest",
        )
    _assert_expected_pin(
        manifest_sha256,
        expected_object_manifest_sha256,
        code="SEMANTIC_INDEX_MANIFEST_PIN_MISMATCH",
        message="Object manifest SHA-256 differs from the operator pin",
        pointer="--expected-object-manifest-sha256",
    )
    manifest = validate_object_manifest(manifest_value, source_binding=receipt["source_binding"])
    if (
        not isinstance(registry_audit, dict)
        or registry_audit.get("schema") != REGISTRY_AUDIT_SCHEMA
        or registry_audit.get("project_name") != receipt["source_binding"]["project"]["name"]
        or registry_audit.get("engine_version") != receipt["source_binding"]["project"]["engine_version"]
        or registry_audit.get("mount_point") != "/Game"
        or isinstance(registry_audit.get("asset_count"), bool)
        or not isinstance(registry_audit.get("asset_count"), int)
        or not isinstance(registry_audit.get("assets"), list)
        or registry_audit["asset_count"] != len(registry_audit["assets"])
        or registry_audit["asset_count"] != manifest["source_registry_audit"]["row_count"]
    ):
        fail(
            "SEMANTIC_INDEX_REGISTRY_BINDING_MISMATCH",
            "Registry audit identity or count differs from the object manifest",
            pointer="registry-audit.json",
        )
    validate_bootstrap_cross_references(
        receipt=receipt,
        registry_audit=registry_audit,
        object_manifest=manifest,
        capability_inventory=capability_inventory,
    )
    _assert_expected_pin(
        manifest["manifest_revision"],
        receipt["object_manifest_revision"],
        code="SEMANTIC_INDEX_MANIFEST_RECEIPT_MISMATCH",
        message="Object manifest revision differs from the bootstrap receipt",
        pointer="$/manifest_revision",
    )
    _assert_expected_pin(
        manifest["count"],
        receipt["object_count"],
        code="SEMANTIC_INDEX_MANIFEST_RECEIPT_MISMATCH",
        message="Object manifest count differs from the bootstrap receipt",
        pointer="$/count",
    )
    _assert_expected_pin(
        manifest["count"],
        expected_object_count,
        code="SEMANTIC_INDEX_OBJECT_COUNT_PIN_MISMATCH",
        message="Object count differs from the operator pin",
        pointer="--expected-object-count",
    )
    source_binding = receipt["source_binding"]
    _assert_expected_pin(
        source_binding["project"]["revision"],
        expected_project_revision,
        code="SEMANTIC_INDEX_PROJECT_REVISION_MISMATCH",
        message="Project revision differs from the operator pin",
        pointer="--expected-project-revision",
    )
    _assert_expected_pin(
        source_binding["content"]["revision"],
        expected_content_revision,
        code="SEMANTIC_INDEX_CONTENT_REVISION_MISMATCH",
        message="Content revision differs from the operator pin",
        pointer="--expected-content-revision",
    )
    _assert_expected_pin(
        manifest["source_registry_audit"]["sha256"],
        receipt["registry_audit_sha256"],
        code="SEMANTIC_INDEX_REGISTRY_BINDING_MISMATCH",
        message="Object manifest and receipt bind different registry audits",
        pointer="$/source_registry_audit/sha256",
    )

    recipe_raw, recipe_value = _secure_read_json(
        recipe_path,
        max_bytes=MAX_RECIPE_BYTES,
        pointer="--recipe",
    )
    recipe_sha256 = hashlib.sha256(recipe_raw).hexdigest()
    _assert_expected_pin(
        recipe_sha256,
        expected_recipe_sha256,
        code="SEMANTIC_INDEX_RECIPE_PIN_MISMATCH",
        message="Recipe SHA-256 differs from the operator pin",
        pointer="--expected-recipe-sha256",
    )
    recipe = validate_recipe(recipe_value)

    count = manifest["count"]
    caption = recipe["caption"]
    embedding = recipe["embedding"]
    limits = recipe["limits"]
    rendered_views = count * caption["views_per_asset"]
    render_pixels = rendered_views * caption["image_width_px"] * caption["image_height_px"]
    caption_tokens = count * caption["max_output_tokens_per_asset"]
    catalog_bytes = count * limits["max_catalog_record_bytes"]
    dense_vector_bytes = count * embedding["dense_size"] * 4
    estimates = {
        "pending_assets": count,
        "rendered_views_upper_bound": rendered_views,
        "render_pixels_upper_bound": render_pixels,
        "caption_output_tokens_upper_bound": caption_tokens,
        "catalog_json_bytes_upper_bound": catalog_bytes,
        "embedding_batches_upper_bound": math.ceil(count / embedding["batch_size"]),
        "postgres_rows_upper_bound": count,
        "qdrant_points_upper_bound": count,
        "dense_vector_payload_bytes_estimate": dense_vector_bytes,
        "dense_vector_estimate_excludes": [
            "qdrant_payload",
            "sparse_vectors",
            "indexes",
            "replication",
            "storage_engine_overhead",
        ],
    }
    comparisons = {
        "max_assets": count,
        "max_rendered_views": rendered_views,
        "max_render_pixels": render_pixels,
        "max_total_caption_output_tokens": caption_tokens,
        "max_total_catalog_bytes": catalog_bytes,
        "max_postgres_rows": count,
        "max_qdrant_points": count,
        "max_dense_vector_payload_bytes_estimate": dense_vector_bytes,
    }
    for limit_name, actual in comparisons.items():
        if actual > limits[limit_name]:
            fail(
                "SEMANTIC_INDEX_RESOURCE_LIMIT_EXCEEDED",
                f"Prepared job exceeds {limit_name}",
                pointer=f"$/limits/{limit_name}",
            )

    pending_assets = [
        {
            "asset_id": asset["asset_id"],
            "ue_name": asset["ue_name"],
            "ue_path": asset["ue_path"],
            "asset_type": asset["asset_type"],
            "source_pack": asset["source_pack"],
            "content_revision": asset["content_revision"],
            "status": "pending_live_semantic_index",
        }
        for asset in manifest["assets"]
    ]
    caption_recipe = dict(caption)
    embedding_recipe = dict(embedding)
    job_basis = {
        "schema": JOB_SCHEMA,
        "input_contract": {
            "bootstrap_receipt_schema": receipt["schema"],
            "bootstrap_receipt_sha256": receipt_sha256,
            "bootstrap_bundle_revision": receipt["bundle_revision"],
            "object_manifest_schema": manifest["schema"],
            "object_manifest_revision": manifest["manifest_revision"],
            "object_manifest_sha256": manifest_sha256,
            "object_count": count,
            "project": source_binding["project"],
            "content": source_binding["content"],
            "archive_receipt_sha256": source_binding["archive"]["receipt_sha256"],
            "registry_audit_sha256": receipt["registry_audit_sha256"],
            "manifest_kind": "asset_registry_object_candidates_not_catalog",
            "bootstrap_bundle_complete": True,
            "candidate_manifest_reviewed": False,
            "catalog_complete": False,
            "snapshot_complete": False,
        },
        "recipe_contract": {
            "recipe_schema": recipe["schema"],
            "recipe_sha256": recipe_sha256,
            "caption_recipe_sha256": json_sha256(caption_recipe),
            "caption": caption_recipe,
            "embedding_recipe_sha256": json_sha256(embedding_recipe),
            "embedding": embedding_recipe,
            "storage": recipe["storage"],
        },
        "snapshot_target": {
            "asset_snapshot_revision": snapshot_revision,
            "postgres": {
                "schema_revision": recipe["storage"]["postgres_schema_revision"],
                "required_asset_snapshot_revision": snapshot_revision,
                "planned_candidate_rows": count,
                "observed_live": False,
            },
            "qdrant": {
                "collection": recipe["storage"]["qdrant_collection"],
                "dense_vector_name": recipe["storage"]["qdrant_dense_vector_name"],
                "sparse_vector_name": recipe["storage"]["qdrant_sparse_vector_name"],
                "required_payload_asset_snapshot_revision": snapshot_revision,
                "planned_candidate_points": count,
                "observed_live": False,
            },
            "row_point_parity_required": True,
            "live_audit_receipt_required": True,
            "snapshot_complete": False,
        },
        "pending_objects": {
            "count": count,
            "selection": "all_unindexed_objects_in_pinned_bootstrap_manifest",
            "assets_sha256": json_sha256(pending_assets),
            "assets": pending_assets,
        },
        "resource_contract": {
            "limits": limits,
            "estimates": estimates,
            "cost_authorized": False,
        },
        "live_gates": [
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
        ],
        "execution_contract": {
            "prepared_only": True,
            "default_behavior": "dry_run",
            "apply_effect": "atomic_private_job_bundle_only",
            "explicit_operator_execution_required": True,
            "legacy_full_asset_index_runner_authorized": False,
            "reviewed_adapter_required_for_legacy_runner": True,
            "prohibited_production_default_args": [
                "--dangerously-bypass-approvals-and-sandbox"
            ],
            "network_allowed_during_preparation": False,
            "unreal_allowed_during_preparation": False,
            "caption_provider_allowed_during_preparation": False,
            "postgres_allowed_during_preparation": False,
            "qdrant_allowed_during_preparation": False,
            "embedding_allowed_during_preparation": False,
        },
        "catalog_complete": False,
        "snapshot_complete": False,
    }
    job = dict(job_basis)
    job["job_revision"] = "sha256:" + json_sha256(job_basis)
    job_bytes = canonical_json(job)
    return PreparedJob(
        job_bytes=job_bytes,
        bootstrap_receipt_sha256=hashlib.sha256(receipt_raw).hexdigest(),
        object_manifest_sha256=manifest_sha256,
        recipe_sha256=recipe_sha256,
        pending_objects_sha256=json_sha256(pending_assets),
    )


def validate_approval_ref(value: str) -> str:
    if (
        not APPROVAL_RE.fullmatch(value)
        or any(word in value.lower() for word in ("token", "secret", "password", "bearer", "apikey"))
    ):
        fail(
            "SEMANTIC_INDEX_APPROVAL_INVALID",
            "Approval must be a non-secret APPROVAL-/CHANGE-/TICKET-/VISTA- identifier",
            pointer="--approval-ref",
        )
    return hashlib.sha256(b"simworld-semantic-index-approval/v1\0" + value.encode("utf-8")).hexdigest()


def validate_output_dir(output_dir: Path) -> Path:
    if (
        not output_dir.is_absolute()
        or output_dir != Path(os.path.abspath(output_dir))
        or output_dir == Path(output_dir.anchor)
        or not OUTPUT_NAME_RE.fullmatch(output_dir.name)
    ):
        fail(
            "SEMANTIC_INDEX_OUTPUT_INVALID",
            "Output must be an absolute normalized non-root safe directory",
            pointer="--output-dir",
        )
    parent = output_dir.parent
    try:
        _reject_symlink_components(parent, pointer="--output-dir")
    except SemanticIndexJobError:
        fail(
            "SEMANTIC_INDEX_OUTPUT_INVALID",
            "Output parent must exist without symlink components",
            pointer="--output-dir",
        )
    try:
        metadata = os.lstat(parent)
    except OSError:
        fail(
            "SEMANTIC_INDEX_OUTPUT_INVALID",
            "Output parent must already exist",
            pointer="--output-dir",
        )
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        fail(
            "SEMANTIC_INDEX_OUTPUT_INVALID",
            "Output parent must be current-user-owned and private",
            pointer="--output-dir",
        )
    try:
        os.lstat(output_dir)
    except FileNotFoundError:
        return output_dir
    fail(
        "SEMANTIC_INDEX_OUTPUT_EXISTS",
        "Job publication is non-overwriting; choose a new output directory",
        pointer="--output-dir",
    )


def _write_private(path: Path, raw: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rename_directory_no_replace(source: Path, destination: Path) -> None:
    renameat2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if renameat2 is None:
        fail(
            "SEMANTIC_INDEX_ATOMIC_PUBLISH_UNAVAILABLE",
            "Host does not expose renameat2(RENAME_NOREPLACE)",
        )
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1)
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        fail("SEMANTIC_INDEX_OUTPUT_EXISTS", "Job publication is non-overwriting")
    if error_number in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP}:
        fail(
            "SEMANTIC_INDEX_ATOMIC_PUBLISH_UNAVAILABLE",
            "Filesystem does not support atomic no-replace publication",
        )
    raise OSError(error_number, os.strerror(error_number), str(destination))


def publish_job(prepared: PreparedJob, output_dir: Path, approval_ref: str) -> dict[str, Any]:
    job = _validated_prepared_job(prepared)
    output_dir = validate_output_dir(output_dir)
    approval_sha256 = validate_approval_ref(approval_ref)
    lock = output_dir.parent / f".{output_dir.name}.lock"
    lock_descriptor: int | None = None
    lock_identity: tuple[int, ...] | None = None
    owns_lock = False
    temporary: Path | None = None
    try:
        lock_descriptor = os.open(
            lock,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        owns_lock = True
        lock_metadata = os.fstat(lock_descriptor)
        if (
            not stat.S_ISREG(lock_metadata.st_mode)
            or lock_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(lock_metadata.st_mode) != 0o600
            or lock_metadata.st_nlink != 1
        ):
            fail(
                "SEMANTIC_INDEX_OUTPUT_BUSY",
                "Output lock metadata is unsafe",
            )
        lock_identity = _stable_file_metadata(lock_metadata)
        validate_output_dir(output_dir)
        temporary = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.tmp-", dir=output_dir.parent))
        os.chmod(temporary, 0o700)
        _write_private(temporary / "semantic-index-job.json", prepared.job_bytes)
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "job_schema": JOB_SCHEMA,
            "job_revision": job["job_revision"],
            "job_sha256": prepared.job_sha256,
            "bootstrap_receipt_sha256": prepared.bootstrap_receipt_sha256,
            "bootstrap_bundle_revision": job["input_contract"]["bootstrap_bundle_revision"],
            "object_manifest_sha256": prepared.object_manifest_sha256,
            "recipe_sha256": prepared.recipe_sha256,
            "asset_snapshot_revision": job["snapshot_target"]["asset_snapshot_revision"],
            "pending_object_count": job["pending_objects"]["count"],
            "pending_objects_sha256": prepared.pending_objects_sha256,
            "approval_ref_sha256": approval_sha256,
            "prepared_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "publication_policy": "atomic_non_overwriting_private",
            "bundle_complete": True,
            "execution_started": False,
            "network_used": False,
            "unreal_started": False,
            "caption_provider_called": False,
            "postgres_contacted": False,
            "qdrant_contacted": False,
            "embedding_called": False,
            "catalog_complete": False,
            "snapshot_complete": False,
        }
        _write_private(temporary / "preparation-receipt.json", canonical_json(receipt))
        _fsync_directory(temporary)
        _rename_directory_no_replace(temporary, output_dir)
        temporary = None
        try:
            _fsync_directory(output_dir.parent)
        except OSError:
            fail(
                "SEMANTIC_INDEX_OUTPUT_COMMITTED_NOT_DURABLE",
                "Bundle was atomically committed but parent directory durability is "
                "uncertain; inspect the committed path and do not retry it",
                committed=True,
                durability_uncertain=True,
            )
        return receipt
    except FileExistsError:
        fail("SEMANTIC_INDEX_OUTPUT_BUSY", "Another preparation owns the output lock")
    finally:
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)
        if owns_lock and lock_identity is not None:
            try:
                current_lock = os.lstat(lock)
                if _stable_file_metadata(current_lock) == lock_identity:
                    lock.unlink()
            except OSError:
                pass
        if lock_descriptor is not None:
            os.close(lock_descriptor)


def result(prepared: PreparedJob, *, status: str) -> dict[str, Any]:
    job = _validated_prepared_job(prepared)
    return {
        "schema": RESULT_SCHEMA,
        "valid": True,
        "status": status,
        "job_revision": job["job_revision"],
        "job_sha256": prepared.job_sha256,
        "bootstrap_bundle_revision": job["input_contract"]["bootstrap_bundle_revision"],
        "object_manifest_sha256": prepared.object_manifest_sha256,
        "recipe_sha256": prepared.recipe_sha256,
        "asset_snapshot_revision": job["snapshot_target"]["asset_snapshot_revision"],
        "pending_object_count": job["pending_objects"]["count"],
        "pending_objects_sha256": prepared.pending_objects_sha256,
        "resource_estimates": job["resource_contract"]["estimates"],
        "execution_started": False,
        "network_used": False,
        "unreal_started": False,
        "caption_provider_called": False,
        "postgres_contacted": False,
        "qdrant_contacted": False,
        "embedding_called": False,
        "catalog_complete": False,
        "snapshot_complete": False,
    }


class SafeParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        fail(
            "SEMANTIC_INDEX_ARGUMENT_INVALID",
            "CLI arguments are missing or invalid; consult --help",
        )


def build_parser() -> argparse.ArgumentParser:
    parser = SafeParser(description=__doc__)
    parser.add_argument("--bootstrap-receipt", required=True)
    parser.add_argument("--object-manifest", required=True)
    parser.add_argument("--recipe", required=True)
    parser.add_argument("--expected-bootstrap-receipt-sha256", required=True)
    parser.add_argument("--expected-bundle-revision", required=True)
    parser.add_argument("--expected-object-manifest-sha256", required=True)
    parser.add_argument("--expected-object-count", type=int, required=True)
    parser.add_argument("--expected-project-revision", required=True)
    parser.add_argument("--expected-content-revision", required=True)
    parser.add_argument("--expected-recipe-sha256", required=True)
    parser.add_argument("--asset-snapshot-revision", required=True)
    parser.add_argument("--output-dir")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approval-ref", default="")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    os.umask(0o077)
    try:
        args = build_parser().parse_args(argv)
        if args.apply and (not args.output_dir or not args.approval_ref):
            fail(
                "SEMANTIC_INDEX_ARGUMENT_INVALID",
                "--apply requires --output-dir and --approval-ref",
            )
        if not args.apply and args.approval_ref:
            fail(
                "SEMANTIC_INDEX_ARGUMENT_INVALID",
                "--approval-ref is accepted only with --apply",
            )
        output_dir = Path(args.output_dir) if args.output_dir else None
        if output_dir is not None:
            validate_output_dir(output_dir)
        prepared = build_job(
            bootstrap_receipt_path=Path(args.bootstrap_receipt),
            object_manifest_path=Path(args.object_manifest),
            recipe_path=Path(args.recipe),
            expected_bootstrap_receipt_sha256=args.expected_bootstrap_receipt_sha256,
            expected_bundle_revision=args.expected_bundle_revision,
            expected_object_manifest_sha256=args.expected_object_manifest_sha256,
            expected_object_count=args.expected_object_count,
            expected_project_revision=args.expected_project_revision,
            expected_content_revision=args.expected_content_revision,
            expected_recipe_sha256=args.expected_recipe_sha256,
            asset_snapshot_revision=args.asset_snapshot_revision,
        )
        if args.apply:
            assert output_dir is not None
            publish_job(prepared, output_dir, args.approval_ref)
            status = "published"
        else:
            status = "dry_run"
        print(compact_json(result(prepared, status=status)))
        return 0
    except SemanticIndexJobError as error:
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
                        "code": "SEMANTIC_INDEX_INTERNAL_ERROR",
                        "message": "Preparation failed before a safe result was produced",
                    },
                }
            ),
            file=sys.stderr,
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
