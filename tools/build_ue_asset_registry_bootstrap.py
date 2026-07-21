#!/usr/bin/env python3
"""Build a revision-bound UE AssetRegistry bootstrap bundle.

The default/offline path consumes a previously captured registry audit and does
not contact Unreal Engine.  A live newline-JSON TCP bridge query is available
only behind ``--live-query`` and requires an explicit transport, host, port,
and output directory.  The fixed UE request enumerates AssetRegistry metadata;
it never reads, loads, or saves files below the project's Content directory.

The published bundle deliberately separates spawnable semantic object
candidates from character/animation/rig capability candidates.  Only
``object-manifest.json`` has the top-level ``assets`` list expected by the
semantic indexing runners.
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import shutil
import socket
import stat
import sys
from typing import Any, Iterable, Mapping, Sequence


AUDIT_SCHEMA = "simworld-ue-asset-registry-audit/v1"
OBJECT_MANIFEST_SCHEMA = "simworld-ue-object-manifest/v2"
CAPABILITY_SCHEMA = "simworld-ue-content-capabilities/v1"
BUNDLE_RECEIPT_SCHEMA = "simworld-ue-asset-bootstrap-receipt/v1"
ARCHIVE_RECEIPT_SCHEMA = "vista-simworld-archive-receipt/v1"
FILTER_POLICY_REVISION = "simworld-object-filter/2"
LIVE_TRANSPORT = "legacy-tcp"
LIVE_RESULT_TAG = "SIMWORLD_ASSET_REGISTRY_AUDIT_V1:"
MOUNT_POINT = "/Game"

MAX_AUDIT_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_RECEIPT_BYTES = 1024 * 1024
MAX_REGISTRY_ROWS = 100_000
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_INVENTORY_LIMIT = 5_000

# Query only registry classes useful to either semantic object discovery or a
# bounded animation/content capability audit.  Materials, textures, worlds,
# and other package types never enter the returned audit.
OBJECT_CLASSES = frozenset({"Blueprint", "StaticMesh"})
CAPABILITY_CLASS_GROUPS: Mapping[str, frozenset[str]] = {
    "character_blueprints": frozenset({"Blueprint"}),
    "animation_clips": frozenset({"AnimMontage", "AnimSequence"}),
    "skeletal_meshes": frozenset({"SkeletalMesh"}),
    "animation_blueprints": frozenset({"AnimBlueprint"}),
    "ik_control_rigs": frozenset(
        {
            "ControlRigBlueprint",
            "IKRetargeter",
            "IKRigDefinition",
            "RigVMBlueprint",
        }
    ),
    "skeletons": frozenset({"Skeleton"}),
}
QUERY_CLASSES = frozenset(
    OBJECT_CLASSES.union(*(classes for classes in CAPABILITY_CLASS_GROUPS.values()))
)

NOISE_SEGMENT_TOKENS = frozenset(
    {
        "builtdata",
        "demo",
        "editor",
        "externalactors",
        "externalobjects",
        "function",
        "functions",
        "map",
        "maps",
        "material",
        "materials",
        "overview",
        "render",
        "renders",
        "test",
        "tests",
        "texture",
        "textures",
        "thumbnail",
        "thumbnails",
    }
)
SURFACE_TOKENS = frozenset(
    {
        "arch",
        "asphalt",
        "baseboard",
        "beam",
        "ceiling",
        "column",
        "corner",
        "curb",
        "decal",
        "doorframe",
        "facade",
        "floor",
        "grass",
        "ground",
        "landscape",
        "ledge",
        "line",
        "marking",
        "molding",
        "pavement",
        "plane",
        "platform",
        "railing",
        "road",
        "roof",
        "sidewalk",
        "skirting",
        "stair",
        "stairs",
        "terrain",
        "tile",
        "tiles",
        "trim",
        "wall",
        "window",
    }
)
POSITIVE_OBJECT_TOKENS = frozenset(
    {
        "barrel",
        "barrier",
        "bench",
        "bin",
        "bollard",
        "bottle",
        "box",
        "bus",
        "cabinet",
        "can",
        "car",
        "cart",
        "chair",
        "cone",
        "couch",
        "crate",
        "fence",
        "fountain",
        "hydrant",
        "lamp",
        "light",
        "plant",
        "pot",
        "prop",
        "rack",
        "rock",
        "scooter",
        "shelf",
        "sign",
        "sofa",
        "statue",
        "stool",
        "table",
        "trash",
        "tree",
        "vehicle",
    }
)
HELPER_TOKENS = frozenset(
    {
        "abstract",
        "baker",
        "controller",
        "customizer",
        "foliagetype",
        "generator",
        "helper",
        "manager",
        "master",
        "parent",
        "preview",
        "proxy",
        "spawner",
        "template",
    }
)
CHARACTER_TOKENS = frozenset(
    {
        "anim",
        "body",
        "character",
        "citizennpc",
        "cloth",
        "clothes",
        "crowd",
        "groom",
        "hair",
        "head",
        "human",
        "humanoid",
        "mannequin",
        "metahuman",
        "npc",
        "pedestrian",
        "skeleton",
        "vrhasian",
    }
)
CHARACTER_ROOT_TOKENS = frozenset(
    {
        "characters",
        "citizennpc",
        "citysamplecrowd",
        "human_avatar",
        "vrhasian",
        "vrhm_urban_npc",
    }
)
CAPABILITY_SIGNAL_TOKENS: Mapping[str, frozenset[str]] = {
    "character": frozenset({"character", "human", "humanoid", "manny", "pawn", "quinn"}),
    "fall": frozenset({"fall", "fallen", "falling"}),
    "foot_ik": frozenset({"footik", "foot_ik"}),
    "ik": frozenset({"ik", "ikrig"}),
    "lift": frozenset({"lift", "lifting", "pickup", "pick", "put", "throw"}),
    "locomotion": frozenset({"idle", "jump", "run", "walk"}),
    "rig": frozenset({"controlrig", "control_rig", "rig"}),
}

SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+\-]{0,239}$")
SAFE_PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,127}$")
SAFE_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/]{0,239}$")
SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+\-]{0,239}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
PACKAGE_RE = re.compile(r"^/Game(?:/[A-Za-z0-9_+\-]+)+$")
ASSET_NAME_RE = re.compile(r"^[A-Za-z0-9_+\-]{1,240}$")
CLASS_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")
GIT_COMMIT_RE = re.compile(r"^[a-f0-9]{40,64}$")
FLOATING_REVISIONS = frozenset({"dev", "head", "latest", "main", "master", "trunk"})


class BootstrapError(RuntimeError):
    """Fail-closed input, transport, or publication error."""


@dataclasses.dataclass(frozen=True)
class SourceBinding:
    project_name: str
    project_revision: str
    content_revision: str
    archive: Mapping[str, Any]

    def __post_init__(self) -> None:
        expected_project_revision = self.archive.get("expected_project_revision")
        expected_content_revision = self.archive.get("expected_content_revision")
        if self.project_revision != expected_project_revision:
            raise BootstrapError(
                "project revision does not match the verified archive receipt"
            )
        if self.content_revision != expected_content_revision:
            raise BootstrapError(
                "content revision does not match the verified archive receipt"
            )


def canonical_json_bytes(value: Any, *, pretty: bool = False) -> bytes:
    if pretty:
        text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    else:
        text = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    return text.encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _reject_duplicate_pairs(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise BootstrapError(f"JSON contains a duplicate key: {key!r}")
        value[key] = item
    return value


def _strict_json_loads(raw: str, label: str) -> Any:
    try:
        return json.loads(raw, object_pairs_hook=_reject_duplicate_pairs)
    except BootstrapError:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise BootstrapError(f"{label} is not valid JSON") from exc


def _require_plain_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BootstrapError(f"{label} must be a JSON object")
    return value


def _require_exact_keys(
    value: Mapping[str, Any], *, required: Iterable[str], optional: Iterable[str], label: str
) -> None:
    required_set = set(required)
    allowed = required_set.union(optional)
    missing = sorted(required_set.difference(value))
    extra = sorted(set(value).difference(allowed))
    if missing:
        raise BootstrapError(f"{label} is missing required keys: {', '.join(missing)}")
    if extra:
        raise BootstrapError(f"{label} has unsupported keys: {', '.join(extra)}")


def validate_pinned_revision(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SAFE_ID_RE.fullmatch(value):
        raise BootstrapError(f"{label} must be a bounded revision identifier")
    revision_tokens = set(filter(None, re.split(r"[:/@._+\-]+", value.lower())))
    if revision_tokens.intersection(FLOATING_REVISIONS):
        raise BootstrapError(f"{label} must be immutable, not {value!r}")
    return value


def _parse_utc_timestamp(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) > 64:
        raise BootstrapError(f"{label} must be a bounded RFC3339 timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BootstrapError(f"{label} must be a valid RFC3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BootstrapError(f"{label} must include a timezone")
    return value


def _read_regular_json(path: pathlib.Path, *, max_bytes: int, label: str) -> Any:
    if not path.is_absolute() or path != pathlib.Path(os.path.abspath(path)):
        raise BootstrapError(f"{label} path must be absolute: {path}")
    current = pathlib.Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            component_metadata = current.lstat()
        except OSError as exc:
            raise BootstrapError(f"cannot stat {label}: {path}: {exc}") from exc
        if stat.S_ISLNK(component_metadata.st_mode):
            raise BootstrapError(f"{label} path must not traverse symlinks: {path}")
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise BootstrapError(f"cannot stat {label}: {path}: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise BootstrapError(f"{label} must be a regular, non-symlink file: {path}")
    if metadata.st_nlink != 1:
        raise BootstrapError(f"{label} must not be hard-linked: {path}")
    if metadata.st_size < 2 or metadata.st_size > max_bytes:
        raise BootstrapError(f"{label} is outside the {max_bytes}-byte bound: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_dev != metadata.st_dev
                or opened.st_ino != metadata.st_ino
                or opened.st_size != metadata.st_size
            ):
                raise BootstrapError(f"{label} changed while it was being opened: {path}")
            raw = handle.read(max_bytes + 1)
    except OSError as exc:
        raise BootstrapError(f"cannot read {label}: {path}: {exc}") from exc
    if len(raw) > max_bytes:
        raise BootstrapError(f"{label} exceeds the {max_bytes}-byte bound: {path}")
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BootstrapError(f"{label} is not valid UTF-8 JSON: {path}") from exc
    return _strict_json_loads(decoded, label)


def validate_archive_receipt(raw: Any) -> dict[str, Any]:
    receipt = _require_plain_mapping(raw, "archive receipt")
    required = {
        "schema",
        "repository",
        "repository_type",
        "dataset_revision",
        "filename",
        "expected_size_bytes",
        "actual_size_bytes",
        "expected_sha256",
        "actual_sha256",
        "verified",
    }
    # Preserve compatibility with the existing receipt while preventing any of
    # its host-local canonical paths or download notes from entering outputs.
    allowed_extras = {
        "verified_at",
        "canonical_path",
        "download_method",
        "source_patch_commit",
        "notes",
    }
    _require_exact_keys(receipt, required=required, optional=allowed_extras, label="archive receipt")
    if receipt["schema"] != ARCHIVE_RECEIPT_SCHEMA:
        raise BootstrapError(f"archive receipt schema must be {ARCHIVE_RECEIPT_SCHEMA}")
    repository = receipt["repository"]
    if not isinstance(repository, str) or not SAFE_REPOSITORY_RE.fullmatch(repository):
        raise BootstrapError("archive receipt repository is invalid")
    repository_type = receipt["repository_type"]
    if repository_type not in {"dataset", "model"}:
        raise BootstrapError("archive receipt repository_type must be dataset or model")
    revision = validate_pinned_revision(receipt["dataset_revision"], "archive revision")
    filename = receipt["filename"]
    if not isinstance(filename, str) or not SAFE_FILENAME_RE.fullmatch(filename):
        raise BootstrapError("archive receipt filename is invalid")
    expected_size = receipt["expected_size_bytes"]
    actual_size = receipt["actual_size_bytes"]
    if (
        not isinstance(expected_size, int)
        or isinstance(expected_size, bool)
        or expected_size <= 0
        or not isinstance(actual_size, int)
        or isinstance(actual_size, bool)
        or expected_size != actual_size
    ):
        raise BootstrapError("archive receipt size verification is invalid")
    expected_sha = receipt["expected_sha256"]
    actual_sha = receipt["actual_sha256"]
    if (
        not isinstance(expected_sha, str)
        or not SHA256_RE.fullmatch(expected_sha)
        or expected_sha != actual_sha
    ):
        raise BootstrapError("archive receipt SHA-256 verification is invalid")
    if receipt["verified"] is not True:
        raise BootstrapError("archive receipt must be verified")
    source_patch_commit = receipt.get("source_patch_commit")
    if source_patch_commit is not None:
        if not isinstance(source_patch_commit, str) or not GIT_COMMIT_RE.fullmatch(
            source_patch_commit
        ):
            raise BootstrapError("archive receipt source_patch_commit is invalid")
        expected_project_revision = f"source-patch:{source_patch_commit}"
    else:
        expected_project_revision = f"archive-revision:{revision}"
    return {
        "receipt_schema": ARCHIVE_RECEIPT_SCHEMA,
        "receipt_sha256": sha256_json(receipt),
        "repository": repository,
        "repository_type": repository_type,
        "revision": revision,
        "filename": filename,
        "size_bytes": expected_size,
        "sha256": expected_sha,
        "verified": True,
        "expected_project_revision": expected_project_revision,
        "expected_content_revision": f"sha256:{expected_sha}",
    }


def load_archive_receipt(path: pathlib.Path) -> dict[str, Any]:
    return validate_archive_receipt(
        _read_regular_json(path, max_bytes=MAX_ARCHIVE_RECEIPT_BYTES, label="archive receipt")
    )


def validate_registry_audit(raw: Any, *, expected_project_name: str) -> dict[str, Any]:
    audit = _require_plain_mapping(raw, "registry audit")
    _require_exact_keys(
        audit,
        required={
            "schema",
            "generated_at_utc",
            "project_name",
            "engine_version",
            "mount_point",
            "selected_classes",
            "asset_count",
            "assets",
        },
        optional=set(),
        label="registry audit",
    )
    if audit["schema"] != AUDIT_SCHEMA:
        raise BootstrapError(f"registry audit schema must be {AUDIT_SCHEMA}")
    generated_at = _parse_utc_timestamp(audit["generated_at_utc"], "generated_at_utc")
    project_name = audit["project_name"]
    if not isinstance(project_name, str) or not SAFE_PROJECT_RE.fullmatch(project_name):
        raise BootstrapError("registry audit project_name is invalid")
    if project_name != expected_project_name:
        raise BootstrapError(
            f"registry audit project_name {project_name!r} does not match {expected_project_name!r}"
        )
    engine_version = audit["engine_version"]
    if (
        not isinstance(engine_version, str)
        or not engine_version.strip()
        or len(engine_version) > 160
        or any(ord(char) < 32 for char in engine_version)
    ):
        raise BootstrapError("registry audit engine_version is invalid")
    if audit["mount_point"] != MOUNT_POINT:
        raise BootstrapError(f"registry audit mount_point must be {MOUNT_POINT}")
    selected_classes = audit["selected_classes"]
    if (
        not isinstance(selected_classes, list)
        or any(not isinstance(value, str) for value in selected_classes)
        or selected_classes != sorted(QUERY_CLASSES)
    ):
        raise BootstrapError("registry audit selected_classes do not match the fixed query policy")
    assets = audit["assets"]
    if not isinstance(assets, list) or len(assets) > MAX_REGISTRY_ROWS:
        raise BootstrapError(f"registry audit assets must be a list of at most {MAX_REGISTRY_ROWS}")
    if (
        not isinstance(audit["asset_count"], int)
        or isinstance(audit["asset_count"], bool)
        or audit["asset_count"] != len(assets)
    ):
        raise BootstrapError("registry audit asset_count does not match assets")

    canonical_rows: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for index, raw_row in enumerate(assets):
        row = _require_plain_mapping(raw_row, f"registry audit asset[{index}]")
        _require_exact_keys(
            row,
            required={"package", "name", "class"},
            optional=set(),
            label=f"registry audit asset[{index}]",
        )
        package = row["package"]
        name = row["name"]
        asset_class = row["class"]
        if not isinstance(package, str) or not PACKAGE_RE.fullmatch(package):
            raise BootstrapError(f"registry audit asset[{index}].package is not a /Game path")
        if ".." in package.split("/") or "\\" in package:
            raise BootstrapError(f"registry audit asset[{index}].package is unsafe")
        if not isinstance(name, str) or not ASSET_NAME_RE.fullmatch(name):
            raise BootstrapError(f"registry audit asset[{index}].name is invalid")
        if (
            not isinstance(asset_class, str)
            or not CLASS_NAME_RE.fullmatch(asset_class)
            or asset_class not in QUERY_CLASSES
        ):
            raise BootstrapError(f"registry audit asset[{index}].class is outside the query policy")
        key = (package, name, asset_class)
        if key in seen:
            raise BootstrapError(f"registry audit contains a duplicate asset row: {key!r}")
        seen.add(key)
        canonical_rows.append({"package": package, "name": name, "class": asset_class})

    canonical_rows.sort(key=lambda row: (row["package"], row["name"], row["class"]))
    return {
        "schema": AUDIT_SCHEMA,
        "generated_at_utc": generated_at,
        "project_name": project_name,
        "engine_version": engine_version.strip(),
        "mount_point": MOUNT_POINT,
        "selected_classes": sorted(QUERY_CLASSES),
        "asset_count": len(canonical_rows),
        "assets": canonical_rows,
    }


def load_registry_audit(path: pathlib.Path, *, expected_project_name: str) -> dict[str, Any]:
    return validate_registry_audit(
        _read_regular_json(path, max_bytes=MAX_AUDIT_BYTES, label="registry audit"),
        expected_project_name=expected_project_name,
    )


def tokenize(value: str) -> frozenset[str]:
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    separated = re.sub(r"([A-Za-z])([0-9])", r"\1_\2", separated)
    separated = re.sub(r"([0-9])([A-Za-z])", r"\1_\2", separated)
    return frozenset(re.findall(r"[a-z0-9]+", separated.lower()))


def source_pack(package: str) -> str:
    parts = package.split("/")
    return parts[2] if len(parts) > 2 else "Game"


def _segment_tokens(package: str) -> frozenset[str]:
    values: set[str] = set()
    for segment in package.split("/"):
        compact = re.sub(r"[^a-z0-9]", "", segment.lower())
        if compact:
            values.add(compact)
        values.update(tokenize(segment))
    return frozenset(values)


def classify_object_candidate(row: Mapping[str, str]) -> tuple[bool, str | None]:
    """Pure, deterministic object filter for one validated registry row."""

    asset_class = row["class"]
    if asset_class not in OBJECT_CLASSES:
        return False, "non_object_class"
    package = row["package"]
    root = source_pack(package)
    tokens = tokenize(f"{package}/{row['name']}")
    segments = _segment_tokens(package)
    if segments.intersection(NOISE_SEGMENT_TOKENS):
        return False, "noise_path"
    if tokens.intersection(HELPER_TOKENS):
        return False, "helper_system"
    root_key = root.lower()
    if root_key in CHARACTER_ROOT_TOKENS or tokens.intersection(CHARACTER_TOKENS):
        return False, "character_or_bodypart"
    if tokens.intersection(SURFACE_TOKENS) and not tokens.intersection(POSITIVE_OBJECT_TOKENS):
        return False, "surface_modular_shell"
    return True, None


def _safe_asset_id(row: Mapping[str, str]) -> str:
    root = re.sub(r"[^a-z0-9]+", "_", source_pack(row["package"]).lower()).strip("_")
    name = re.sub(r"[^a-z0-9]+", "_", row["name"].lower()).strip("_")
    digest = hashlib.sha256(f"{row['package']}.{row['name']}".encode("utf-8")).hexdigest()[:10]
    prefix = f"{root or 'game'}_{name or 'asset'}"[:229].rstrip("_")
    return f"{prefix}_{digest}"


def _source_binding_dict(binding: SourceBinding, audit: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "project": {
            "name": binding.project_name,
            "revision": binding.project_revision,
            "engine_version": audit["engine_version"],
        },
        "content": {
            "mount_point": MOUNT_POINT,
            "revision": binding.content_revision,
        },
        "archive": dict(binding.archive),
    }


def _object_filter_description() -> dict[str, Any]:
    return {
        "revision": FILTER_POLICY_REVISION,
        "include_classes": sorted(OBJECT_CLASSES),
        "capability_classes_excluded_from_semantic_index": sorted(
            QUERY_CLASSES.difference(OBJECT_CLASSES)
        ),
        "character_blueprints_separated_by_filter": True,
        "noise_segment_tokens": sorted(NOISE_SEGMENT_TOKENS),
        "surface_tokens": sorted(SURFACE_TOKENS),
        "positive_object_tokens": sorted(POSITIVE_OBJECT_TOKENS),
        "helper_tokens": sorted(HELPER_TOKENS),
        "character_tokens": sorted(CHARACTER_TOKENS),
        "character_roots": sorted(CHARACTER_ROOT_TOKENS),
    }


def build_object_manifest(audit: Mapping[str, Any], binding: SourceBinding) -> dict[str, Any]:
    accepted: list[dict[str, Any]] = []
    reject_counts: collections.Counter[str] = collections.Counter()
    class_counts: collections.Counter[str] = collections.Counter()
    pack_counts: collections.Counter[str] = collections.Counter()
    object_class_rows = 0

    for row in audit["assets"]:
        if row["class"] in OBJECT_CLASSES:
            object_class_rows += 1
        keep, reason = classify_object_candidate(row)
        if not keep:
            reject_counts[reason or "unknown"] += 1
            continue
        pack = source_pack(row["package"])
        asset = {
            "asset_id": _safe_asset_id(row),
            "ue_name": row["name"],
            "ue_path": f"{row['package']}.{row['name']}",
            "asset_type": row["class"],
            "source_pack": pack,
            "indexed": False,
            "filter_tags": ["asset_registry", "actual_object_candidate", "objects_only"],
            "content_revision": binding.content_revision,
        }
        accepted.append(asset)
        class_counts[row["class"]] += 1
        pack_counts[pack] += 1

    accepted.sort(key=lambda asset: (asset["source_pack"], asset["ue_path"], asset["asset_id"]))
    if len({asset["asset_id"] for asset in accepted}) != len(accepted):
        raise BootstrapError("deterministic asset-id collision in object manifest")
    source_binding = _source_binding_dict(binding, audit)
    filter_policy = _object_filter_description()
    revision_basis = {
        "schema": OBJECT_MANIFEST_SCHEMA,
        "source_binding": source_binding,
        "filter_policy": filter_policy,
        "assets": accepted,
    }
    return {
        "schema": OBJECT_MANIFEST_SCHEMA,
        "schema_version": "2.0",
        "manifest_kind": "ue_asset_registry_actual_object_candidates",
        "manifest_revision": f"sha256:{sha256_json(revision_basis)}",
        "generated_at_utc": audit["generated_at_utc"],
        "source_binding": source_binding,
        "source_registry_audit": {
            "schema": AUDIT_SCHEMA,
            "sha256": sha256_json(audit),
            "row_count": audit["asset_count"],
        },
        "count": len(accepted),
        "packs": dict(sorted(pack_counts.items())),
        "asset_class_counts": dict(sorted(class_counts.items())),
        "object_filter_audit": {
            "registry_row_count": audit["asset_count"],
            "object_class_row_count": object_class_rows,
            "accepted_count": len(accepted),
            "rejected_count": audit["asset_count"] - len(accepted),
            "reject_counts": dict(sorted(reject_counts.items())),
        },
        "filter_policy": filter_policy,
        "assets": accepted,
    }


def _capability_signals(row: Mapping[str, str]) -> list[str]:
    identity = f"{row['package']}/{row['name']}"
    tokens = set(tokenize(identity))
    ordered_tokens = re.findall(
        r"[a-z0-9]+",
        re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", identity).lower(),
    )
    terms = set(tokens)
    for width in (2, 3):
        for index in range(0, len(ordered_tokens) - width + 1):
            terms.add("".join(ordered_tokens[index : index + width]))
    signals: list[str] = []
    for signal, candidates in CAPABILITY_SIGNAL_TOKENS.items():
        if terms.intersection(candidates):
            signals.append(signal)
    return sorted(signals)


def build_capability_inventory(
    audit: Mapping[str, Any], binding: SourceBinding, *, limit_per_group: int
) -> dict[str, Any]:
    if not isinstance(limit_per_group, int) or not 1 <= limit_per_group <= MAX_INVENTORY_LIMIT:
        raise BootstrapError(
            f"capability inventory limit must be between 1 and {MAX_INVENTORY_LIMIT}"
        )
    groups: dict[str, Any] = {}
    for group_name, classes in CAPABILITY_CLASS_GROUPS.items():
        candidates = []
        for row in audit["assets"]:
            if row["class"] not in classes:
                continue
            if group_name == "character_blueprints":
                keep_as_object, reject_reason = classify_object_candidate(row)
                if keep_as_object or reject_reason != "character_or_bodypart":
                    continue
            candidates.append(
                {
                    "ue_name": row["name"],
                    "ue_path": f"{row['package']}.{row['name']}",
                    "asset_class": row["class"],
                    "source_pack": source_pack(row["package"]),
                    "signals": _capability_signals(row),
                }
            )
        candidates.sort(
            key=lambda item: (
                0 if item["signals"] else 1,
                item["asset_class"],
                item["ue_path"],
            )
        )
        total_count = len(candidates)
        returned = candidates[:limit_per_group]
        groups[group_name] = {
            "classes": sorted(classes),
            "total_count": total_count,
            "returned_count": len(returned),
            "truncated": total_count > len(returned),
            "selection_policy": "signal_prioritized_then_class_path",
            "candidates": returned,
        }
    source_binding = _source_binding_dict(binding, audit)
    revision_basis = {
        "schema": CAPABILITY_SCHEMA,
        "source_binding": source_binding,
        "limit_per_group": limit_per_group,
        "groups": groups,
    }
    return {
        "schema": CAPABILITY_SCHEMA,
        "inventory_kind": "asset_registry_animation_and_rig_candidates",
        "inventory_revision": f"sha256:{sha256_json(revision_basis)}",
        "generated_at_utc": audit["generated_at_utc"],
        "purpose": "runtime_capability_audit_only_not_semantic_object_index",
        "load_compatibility": "unverified",
        "source_binding": source_binding,
        "source_registry_audit": {
            "schema": AUDIT_SCHEMA,
            "sha256": sha256_json(audit),
            "row_count": audit["asset_count"],
        },
        "limit_per_group": limit_per_group,
        "groups": groups,
    }


def validate_object_manifest(manifest: Any) -> dict[str, Any]:
    value = _require_plain_mapping(manifest, "object manifest")
    if value.get("schema") != OBJECT_MANIFEST_SCHEMA:
        raise BootstrapError("object manifest schema is invalid")
    assets = value.get("assets")
    if not isinstance(assets, list) or value.get("count") != len(assets):
        raise BootstrapError("object manifest count does not match assets")
    seen: set[str] = set()
    for index, asset in enumerate(assets):
        row = _require_plain_mapping(asset, f"object manifest asset[{index}]")
        required = {
            "asset_id",
            "ue_name",
            "ue_path",
            "asset_type",
            "source_pack",
            "indexed",
            "filter_tags",
            "content_revision",
        }
        _require_exact_keys(row, required=required, optional=set(), label=f"object manifest asset[{index}]")
        if row["asset_type"] not in OBJECT_CLASSES:
            raise BootstrapError("non-object class entered object manifest")
        if row["asset_id"] in seen:
            raise BootstrapError("duplicate object manifest asset_id")
        seen.add(row["asset_id"])
        if not isinstance(row["ue_path"], str) or "." not in row["ue_path"]:
            raise BootstrapError("object manifest ue_path is invalid")
    return value


def validate_capability_inventory(inventory: Any) -> dict[str, Any]:
    value = _require_plain_mapping(inventory, "capability inventory")
    if value.get("schema") != CAPABILITY_SCHEMA:
        raise BootstrapError("capability inventory schema is invalid")
    if "assets" in value:
        raise BootstrapError("capability inventory must not expose a semantic assets list")
    groups = value.get("groups")
    if not isinstance(groups, dict) or set(groups) != set(CAPABILITY_CLASS_GROUPS):
        raise BootstrapError("capability inventory groups are invalid")
    for group_name, allowed_classes in CAPABILITY_CLASS_GROUPS.items():
        group = _require_plain_mapping(groups[group_name], f"capability group {group_name}")
        if group.get("classes") != sorted(allowed_classes):
            raise BootstrapError(f"capability group {group_name} class policy is invalid")
        candidates = group.get("candidates")
        if not isinstance(candidates, list) or len(candidates) != group.get("returned_count"):
            raise BootstrapError(f"capability group {group_name} count is invalid")
        total_count = group.get("total_count")
        if (
            not isinstance(total_count, int)
            or isinstance(total_count, bool)
            or total_count < len(candidates)
            or group.get("truncated") is not (total_count > len(candidates))
        ):
            raise BootstrapError(f"capability group {group_name} truncation audit is invalid")
        if group.get("selection_policy") != "signal_prioritized_then_class_path":
            raise BootstrapError(f"capability group {group_name} selection policy is invalid")
        if len(candidates) > value.get("limit_per_group", 0):
            raise BootstrapError(f"capability group {group_name} exceeds its bound")
        for candidate in candidates:
            _require_exact_keys(
                candidate,
                required={"ue_name", "ue_path", "asset_class", "source_pack", "signals"},
                optional=set(),
                label=f"capability group {group_name} candidate",
            )
            if candidate.get("asset_class") not in allowed_classes:
                raise BootstrapError(f"capability group {group_name} contains the wrong class")
    return value


def build_ue_registry_script(*, max_rows: int) -> str:
    """Return the fixed AssetRegistry-only live query script."""

    if not 1 <= max_rows <= MAX_REGISTRY_ROWS:
        raise BootstrapError(f"max registry rows must be between 1 and {MAX_REGISTRY_ROWS}")
    return f'''import datetime, json, unreal

QUERY_CLASSES = {sorted(QUERY_CLASSES)!r}
MAX_ROWS = {max_rows}
RESULT_TAG = {LIVE_RESULT_TAG!r}

registry = unreal.AssetRegistryHelpers.get_asset_registry()
try:
    registry.search_all_assets(True)
except Exception:
    pass

rows = []
assets = registry.get_assets_by_path(
    {MOUNT_POINT!r}, recursive=True, include_only_on_disk_assets=True
)
for asset in assets:
    package = str(asset.package_name)
    name = str(asset.asset_name)
    try:
        asset_class = str(asset.asset_class_path.asset_name)
    except Exception:
        asset_class = str(asset.asset_class)
    if asset_class not in QUERY_CLASSES:
        continue
    rows.append({{"package": package, "name": name, "class": asset_class}})
    if len(rows) > MAX_ROWS:
        raise RuntimeError("selected AssetRegistry row count exceeds fixed safety bound")

rows.sort(key=lambda row: (row["package"], row["name"], row["class"]))
project_file = str(unreal.Paths.get_project_file_path()).replace("\\\\", "/")
project_name = project_file.rsplit("/", 1)[-1].rsplit(".", 1)[0]
audit = {{
    "schema": {AUDIT_SCHEMA!r},
    "generated_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "project_name": project_name,
    "engine_version": str(unreal.SystemLibrary.get_engine_version()),
    "mount_point": {MOUNT_POINT!r},
    "selected_classes": QUERY_CLASSES,
    "asset_count": len(rows),
    "assets": rows,
}}
print(RESULT_TAG + json.dumps(audit, ensure_ascii=False, separators=(",", ":")))
'''


def _validate_host(host: Any) -> str:
    if (
        not isinstance(host, str)
        or not host
        or len(host) > 255
        or any(ord(char) < 33 for char in host)
        or any(char in host for char in "/@?#")
    ):
        raise BootstrapError("live bridge host is invalid")
    return host


def send_bridge_request(
    *, host: str, port: int, max_rows: int, timeout: int, max_response_bytes: int
) -> dict[str, Any]:
    host = _validate_host(host)
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise BootstrapError("live bridge port is invalid")
    if not isinstance(timeout, int) or not 1 <= timeout <= 600:
        raise BootstrapError("live bridge timeout must be between 1 and 600 seconds")
    if not 1024 <= max_response_bytes <= MAX_RESPONSE_BYTES:
        raise BootstrapError(f"max response bytes must be between 1024 and {MAX_RESPONSE_BYTES}")
    payload = {
        "type": "execute_python_script",
        "params": {"script": build_ue_registry_script(max_rows=max_rows)},
    }
    try:
        with socket.create_connection((host, port), timeout=min(timeout, 10)) as connection:
            connection.settimeout(timeout)
            connection.sendall(canonical_json_bytes(payload) + b"\n")
            response = bytearray()
            while True:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                response.extend(chunk)
                if len(response) > max_response_bytes:
                    raise BootstrapError("live bridge response exceeds its fixed byte bound")
                try:
                    decoded_text = response.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                try:
                    decoded = json.loads(
                        decoded_text, object_pairs_hook=_reject_duplicate_pairs
                    )
                except json.JSONDecodeError:
                    continue
                except RecursionError as exc:
                    raise BootstrapError("live bridge response is too deeply nested") from exc
                if not isinstance(decoded, dict):
                    raise BootstrapError("live bridge response must be a JSON object")
                return decoded
    except BootstrapError:
        raise
    except OSError as exc:
        raise BootstrapError(f"live bridge query failed: {exc}") from exc
    raise BootstrapError("live bridge closed without a complete JSON response")


def _extract_log_strings(response: Mapping[str, Any]) -> list[str]:
    candidates = [
        response.get("python_logs"),
        (response.get("result") or {}).get("python_logs")
        if isinstance(response.get("result"), dict)
        else None,
        ((response.get("result") or {}).get("result") or {}).get("python_logs")
        if isinstance(response.get("result"), dict)
        and isinstance((response.get("result") or {}).get("result"), dict)
        else None,
    ]
    for candidate in candidates:
        if isinstance(candidate, list) and all(isinstance(line, str) for line in candidate):
            return candidate
    return []


def extract_live_audit(response: Any, *, expected_project_name: str) -> dict[str, Any]:
    payload = _require_plain_mapping(response, "live bridge response")
    if payload.get("status") not in {None, "success"}:
        raise BootstrapError("live bridge reported an unsuccessful request")
    for line in _extract_log_strings(payload):
        if LIVE_RESULT_TAG not in line:
            continue
        encoded = line.split(LIVE_RESULT_TAG, 1)[1]
        raw = _strict_json_loads(encoded, "live registry audit log")
        return validate_registry_audit(raw, expected_project_name=expected_project_name)
    raise BootstrapError("live bridge response did not contain the registry audit marker")


def query_live_registry(
    *,
    transport: str,
    host: str,
    port: int,
    timeout: int,
    max_rows: int,
    max_response_bytes: int,
    expected_project_name: str,
) -> dict[str, Any]:
    if transport != LIVE_TRANSPORT:
        raise BootstrapError(f"live transport must be explicitly set to {LIVE_TRANSPORT}")
    response = send_bridge_request(
        host=host,
        port=port,
        max_rows=max_rows,
        timeout=timeout,
        max_response_bytes=max_response_bytes,
    )
    return extract_live_audit(response, expected_project_name=expected_project_name)


def _write_atomic_new(path: pathlib.Path, payload: bytes) -> None:
    if path.exists() or path.is_symlink():
        raise BootstrapError(f"refusing to overwrite existing output: {path}")
    temp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    owns_temp = False
    try:
        fd = os.open(temp, flags, 0o600)
        owns_temp = True
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temp, path)
        temp.unlink()
    except FileExistsError as exc:
        raise BootstrapError(f"refusing to overwrite existing output: {path}") from exc
    finally:
        if owns_temp:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass


def publish_bundle(
    output_dir: pathlib.Path,
    *,
    audit: Mapping[str, Any],
    manifest: Mapping[str, Any],
    inventory: Mapping[str, Any],
) -> dict[str, Any]:
    if not output_dir.is_absolute() or output_dir != pathlib.Path(os.path.abspath(output_dir)):
        raise BootstrapError(f"output directory must be absolute: {output_dir}")
    parent = output_dir.parent
    current = pathlib.Path(parent.anchor)
    for component in parent.parts[1:]:
        current /= component
        try:
            component_metadata = current.lstat()
        except OSError as exc:
            raise BootstrapError(f"output parent must already exist: {parent}") from exc
        if stat.S_ISLNK(component_metadata.st_mode):
            raise BootstrapError(f"output parent must not traverse symlinks: {parent}")
    try:
        parent_stat = parent.lstat()
    except OSError as exc:
        raise BootstrapError(f"output parent must already exist: {parent}") from exc
    if (
        not stat.S_ISDIR(parent_stat.st_mode)
        or stat.S_ISLNK(parent_stat.st_mode)
        or parent_stat.st_uid != os.geteuid()
        or stat.S_IMODE(parent_stat.st_mode) & 0o077
    ):
        raise BootstrapError(
            f"output parent must be a private current-user-owned directory: {parent}"
        )
    try:
        output_dir.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise BootstrapError(f"refusing to overwrite existing output directory: {output_dir}") from exc
    except OSError as exc:
        raise BootstrapError(f"cannot create output directory {output_dir}: {exc}") from exc

    created_stat = output_dir.lstat()
    if (
        not stat.S_ISDIR(created_stat.st_mode)
        or stat.S_ISLNK(created_stat.st_mode)
        or created_stat.st_uid != os.geteuid()
        or stat.S_IMODE(created_stat.st_mode) != 0o700
    ):
        try:
            output_dir.rmdir()
        except OSError:
            pass
        raise BootstrapError(f"output directory was not created privately: {output_dir}")

    try:
        files = {
            "registry-audit.json": canonical_json_bytes(audit, pretty=True),
            "object-manifest.json": canonical_json_bytes(manifest, pretty=True),
            "content-capabilities.json": canonical_json_bytes(inventory, pretty=True),
        }
        descriptors: dict[str, Any] = {}
        for filename, payload in files.items():
            _write_atomic_new(output_dir / filename, payload)
            descriptors[filename] = {
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        receipt_basis = {
            "schema": BUNDLE_RECEIPT_SCHEMA,
            "source_binding": manifest["source_binding"],
            "registry_audit_sha256": sha256_json(audit),
            "object_manifest_revision": manifest["manifest_revision"],
            "capability_inventory_revision": inventory["inventory_revision"],
            "object_count": manifest["count"],
            "capability_counts": {
                name: group["total_count"] for name, group in inventory["groups"].items()
            },
            "files": descriptors,
            "bundle_complete": True,
            "snapshot_complete": False,
        }
        receipt = dict(receipt_basis)
        receipt["bundle_revision"] = f"sha256:{sha256_json(receipt_basis)}"
        _write_atomic_new(
            output_dir / "bootstrap-receipt.json", canonical_json_bytes(receipt, pretty=True)
        )
        directory_fd = os.open(output_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return receipt
    except Exception:
        # The directory was created exclusively by this call.  A missing final
        # receipt is never left behind as a misleading complete bundle.
        shutil.rmtree(output_dir, ignore_errors=True)
        raise


def summarize(
    *, audit: Mapping[str, Any], manifest: Mapping[str, Any], inventory: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "project_name": audit["project_name"],
        "engine_version": audit["engine_version"],
        "registry_rows": audit["asset_count"],
        "object_candidates": manifest["count"],
        "object_reject_counts": manifest["object_filter_audit"]["reject_counts"],
        "capability_counts": {
            name: {
                "total": group["total_count"],
                "returned": group["returned_count"],
                "truncated": group["truncated"],
            }
            for name, group in inventory["groups"].items()
        },
        "snapshot_complete": False,
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--audit-input", type=pathlib.Path, help="absolute offline audit JSON")
    source.add_argument(
        "--live-query",
        action="store_true",
        help="explicitly authorize one read-only AssetRegistry bridge query",
    )
    parser.add_argument("--project-name", required=True)
    parser.add_argument("--project-revision", required=True)
    parser.add_argument("--content-revision", required=True)
    parser.add_argument("--archive-receipt", type=pathlib.Path, required=True)
    parser.add_argument("--output-dir", type=pathlib.Path)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate/filter an offline audit and print a summary without writing",
    )
    parser.add_argument("--inventory-limit", type=int, default=250)
    parser.add_argument("--transport", choices=[LIVE_TRANSPORT])
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--max-registry-rows", type=int, default=MAX_REGISTRY_ROWS)
    parser.add_argument("--max-response-bytes", type=int, default=MAX_RESPONSE_BYTES)
    args = parser.parse_args(argv)

    if not SAFE_PROJECT_RE.fullmatch(args.project_name):
        parser.error("--project-name must be a bounded project identifier")
    try:
        validate_pinned_revision(args.project_revision, "project revision")
        validate_pinned_revision(args.content_revision, "content revision")
    except BootstrapError as exc:
        parser.error(str(exc))
    if not 1 <= args.inventory_limit <= MAX_INVENTORY_LIMIT:
        parser.error(f"--inventory-limit must be between 1 and {MAX_INVENTORY_LIMIT}")
    if not 1 <= args.max_registry_rows <= MAX_REGISTRY_ROWS:
        parser.error(f"--max-registry-rows must be between 1 and {MAX_REGISTRY_ROWS}")
    if not 1024 <= args.max_response_bytes <= MAX_RESPONSE_BYTES:
        parser.error(f"--max-response-bytes must be between 1024 and {MAX_RESPONSE_BYTES}")
    if not 1 <= args.timeout <= 600:
        parser.error("--timeout must be between 1 and 600")

    if args.live_query:
        if args.dry_run:
            parser.error("--live-query cannot be combined with --dry-run")
        if not args.output_dir:
            parser.error("--live-query requires an explicit --output-dir")
        if not args.transport or not args.host or args.port is None:
            parser.error("--live-query requires explicit --transport, --host, and --port")
    else:
        if args.transport or args.host or args.port is not None:
            parser.error("bridge endpoint options require --live-query")
        if args.dry_run and args.output_dir:
            parser.error("--dry-run does not accept --output-dir")
        if not args.dry_run and not args.output_dir:
            parser.error("offline publication requires --output-dir or use --dry-run")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    archive = load_archive_receipt(args.archive_receipt)
    binding = SourceBinding(
        project_name=args.project_name,
        project_revision=validate_pinned_revision(args.project_revision, "project revision"),
        content_revision=validate_pinned_revision(args.content_revision, "content revision"),
        archive=archive,
    )
    if args.live_query:
        audit = query_live_registry(
            transport=args.transport,
            host=args.host,
            port=args.port,
            timeout=args.timeout,
            max_rows=args.max_registry_rows,
            max_response_bytes=args.max_response_bytes,
            expected_project_name=args.project_name,
        )
    else:
        audit = load_registry_audit(
            args.audit_input,
            expected_project_name=args.project_name,
        )

    manifest = validate_object_manifest(build_object_manifest(audit, binding))
    inventory = validate_capability_inventory(
        build_capability_inventory(audit, binding, limit_per_group=args.inventory_limit)
    )
    summary = summarize(audit=audit, manifest=manifest, inventory=inventory)
    if args.dry_run:
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    receipt = publish_bundle(
        args.output_dir,
        audit=audit,
        manifest=manifest,
        inventory=inventory,
    )
    print(f"wrote bootstrap bundle: {args.output_dir}")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    print(f"bundle revision: {receipt['bundle_revision']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BootstrapError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
