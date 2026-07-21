#!/usr/bin/env python3
"""Capture or verify an offline semantic asset backup bundle manifest.

This tool never calls PostgreSQL or Qdrant.  Administrators first create the
database and collection snapshots during an approved window, then use this
tool to bind those files to the exact asset snapshot and deployment preflight.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import stat
import sys
from typing import Any

from asset_stack_config import (
    AssetStackConfigError,
    require_immutable_revision,
    require_safe_name,
)


MANIFEST_SCHEMA = "simworld-asset-stack-backup/v1"
VERIFY_SCHEMA = "simworld-asset-stack-backup-verification/v1"
RESTORE_PLAN_SCHEMA = "simworld-asset-stack-restore-plan/v1"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
REQUIRED_ROLES = (
    "postgres_dump",
    "qdrant_snapshot",
    "asset_snapshot_manifest",
    "embedding_cache_manifest",
    "ue_content_manifest",
)


class BackupBundleError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_file(path: pathlib.Path) -> tuple[str, int]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise BackupBundleError(
            "ASSET_BACKUP_ARTIFACT_UNAVAILABLE", "backup artifact is unavailable"
        ) from error
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
            raise BackupBundleError(
                "ASSET_BACKUP_ARTIFACT_INVALID",
                "backup artifacts must be non-empty regular files",
            )
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
        after = os.fstat(fd)
        identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if identity_before != identity_after or total != after.st_size:
            raise BackupBundleError(
                "ASSET_BACKUP_ARTIFACT_CHANGED",
                "backup artifact changed while it was being hashed",
            )
        return digest.hexdigest(), total
    finally:
        os.close(fd)


def _relative_artifact(bundle_dir: pathlib.Path, path: pathlib.Path) -> str:
    if path.is_symlink():
        raise BackupBundleError(
            "ASSET_BACKUP_SYMLINK_REJECTED", "backup artifacts may not be symlinks"
        )
    try:
        relative = path.resolve(strict=True).relative_to(bundle_dir.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise BackupBundleError(
            "ASSET_BACKUP_PATH_ESCAPE",
            "every backup artifact must be inside the bundle directory",
        ) from error
    if relative == pathlib.Path(".") or ".." in relative.parts:
        raise BackupBundleError("ASSET_BACKUP_PATH_ESCAPE", "invalid artifact path")
    return relative.as_posix()


def capture_manifest(
    *,
    bundle_dir: pathlib.Path,
    backup_id: str,
    snapshot_revision: str,
    preflight_sha256: str,
    artifacts: dict[str, pathlib.Path],
) -> dict[str, Any]:
    backup_id = require_safe_name(backup_id, "backup_id")
    snapshot_revision = require_immutable_revision(
        snapshot_revision, "snapshot_revision"
    )
    if not SHA256.fullmatch(preflight_sha256):
        raise BackupBundleError(
            "ASSET_BACKUP_PREFLIGHT_INVALID", "preflight_sha256 must be SHA-256"
        )
    if set(artifacts) != set(REQUIRED_ROLES):
        raise BackupBundleError(
            "ASSET_BACKUP_ARTIFACT_SET_INVALID",
            "the backup bundle does not contain every required artifact role",
        )
    if not bundle_dir.is_dir() or bundle_dir.is_symlink():
        raise BackupBundleError(
            "ASSET_BACKUP_DIR_INVALID", "bundle_dir must be a real directory"
        )
    entries = []
    for role in REQUIRED_ROLES:
        path = artifacts[role]
        relative = _relative_artifact(bundle_dir, path)
        digest, size = sha256_file(path)
        entries.append(
            {"role": role, "path": relative, "sha256": digest, "size": size}
        )
    return {
        "schema": MANIFEST_SCHEMA,
        "backup_id": backup_id,
        "snapshot_revision": snapshot_revision,
        "preflight_sha256": preflight_sha256,
        "artifacts": entries,
    }


def _load_manifest(path: pathlib.Path) -> tuple[dict[str, Any], bytes]:
    digest, size = sha256_file(path)
    if size > 1_000_000:
        raise BackupBundleError(
            "ASSET_BACKUP_MANIFEST_INVALID", "backup manifest exceeds the size limit"
        )
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != digest:
        raise BackupBundleError(
            "ASSET_BACKUP_MANIFEST_CHANGED", "backup manifest changed while loading"
        )
    try:
        parsed = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise BackupBundleError(
            "ASSET_BACKUP_MANIFEST_INVALID", "backup manifest is invalid JSON"
        ) from error
    if not isinstance(parsed, dict) or parsed.get("schema") != MANIFEST_SCHEMA:
        raise BackupBundleError(
            "ASSET_BACKUP_MANIFEST_INVALID", "backup manifest schema is invalid"
        )
    return parsed, payload


def verify_manifest(path: pathlib.Path) -> dict[str, Any]:
    manifest, payload = _load_manifest(path)
    try:
        backup_id = require_safe_name(manifest.get("backup_id"), "backup_id")
        snapshot_revision = require_immutable_revision(
            manifest.get("snapshot_revision"), "snapshot_revision"
        )
    except AssetStackConfigError as error:
        raise BackupBundleError(error.code, str(error)) from error
    preflight_sha256 = manifest.get("preflight_sha256")
    if not isinstance(preflight_sha256, str) or not SHA256.fullmatch(preflight_sha256):
        raise BackupBundleError(
            "ASSET_BACKUP_PREFLIGHT_INVALID", "preflight_sha256 is invalid"
        )
    entries = manifest.get("artifacts")
    if not isinstance(entries, list) or len(entries) != len(REQUIRED_ROLES):
        raise BackupBundleError(
            "ASSET_BACKUP_ARTIFACT_SET_INVALID", "artifact set is incomplete"
        )
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise BackupBundleError(
                "ASSET_BACKUP_MANIFEST_INVALID", "artifact entry is invalid"
            )
        role = entry.get("role")
        if role not in REQUIRED_ROLES or role in seen:
            raise BackupBundleError(
                "ASSET_BACKUP_ARTIFACT_SET_INVALID", "artifact roles are invalid"
            )
        seen.add(role)
        relative = pathlib.PurePosixPath(str(entry.get("path") or ""))
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise BackupBundleError(
                "ASSET_BACKUP_PATH_ESCAPE", "artifact path escapes the bundle"
            )
        artifact = path.parent.joinpath(*relative.parts)
        observed_relative = _relative_artifact(path.parent, artifact)
        if observed_relative != relative.as_posix():
            raise BackupBundleError(
                "ASSET_BACKUP_PATH_ESCAPE", "artifact path escapes the bundle"
            )
        digest, size = sha256_file(artifact)
        if digest != entry.get("sha256") or size != entry.get("size"):
            raise BackupBundleError(
                "ASSET_BACKUP_CHECKSUM_MISMATCH",
                "backup artifact does not match its manifest",
            )
    if seen != set(REQUIRED_ROLES):
        raise BackupBundleError(
            "ASSET_BACKUP_ARTIFACT_SET_INVALID", "artifact set is incomplete"
        )
    return {
        "schema": VERIFY_SCHEMA,
        "status": "verified",
        "backup_id": backup_id,
        "snapshot_revision": snapshot_revision,
        "preflight_sha256": preflight_sha256,
        "manifest_sha256": hashlib.sha256(payload).hexdigest(),
    }


def restore_plan(path: pathlib.Path) -> dict[str, Any]:
    verified = verify_manifest(path)
    return {
        "schema": RESTORE_PLAN_SCHEMA,
        "status": "requires_admin_state_change",
        "backup_id": verified["backup_id"],
        "snapshot_revision": verified["snapshot_revision"],
        "manifest_sha256": verified["manifest_sha256"],
        "steps": [
            "provision_empty_disposable_postgres_and_qdrant_targets",
            "restore_postgres_dump_without_owner_or_acl",
            "restore_qdrant_collection_snapshot_under_the_pinned_collection_name",
            "mount_the_exact_catalog_embedding_cache_and_ue_content_manifests",
            "run_verify_asset_snapshot_capture_and_verify",
            "run_shadow_queries_and_blueprint_staticmesh_spawn_smoke",
            "destroy_the_disposable_targets_after_recording_evidence",
        ],
    }


def atomic_write(path: pathlib.Path, value: dict[str, Any]) -> None:
    if path.exists() or path.is_symlink():
        raise BackupBundleError(
            "ASSET_BACKUP_MANIFEST_EXISTS",
            "backup manifests are immutable and may not be replaced",
        )
    payload = canonical_json(value) + b"\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(temporary, flags, 0o644)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            raise BackupBundleError(
                "ASSET_BACKUP_MANIFEST_EXISTS",
                "backup manifests are immutable and may not be replaced",
            ) from error
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    capture = sub.add_parser("capture")
    capture.add_argument("--bundle-dir", type=pathlib.Path, required=True)
    capture.add_argument("--backup-id", required=True)
    capture.add_argument("--snapshot-revision", required=True)
    capture.add_argument("--preflight-sha256", required=True)
    for role in REQUIRED_ROLES:
        capture.add_argument(f"--{role.replace('_', '-')}", type=pathlib.Path, required=True)
    capture.add_argument("--output", type=pathlib.Path, required=True)
    for name in ("verify", "restore-plan"):
        command = sub.add_parser(name)
        command.add_argument("--manifest", type=pathlib.Path, required=True)
    return parser


def main() -> int:
    args = make_parser().parse_args()
    try:
        if args.command == "capture":
            if args.output.parent.resolve() != args.bundle_dir.resolve():
                raise BackupBundleError(
                    "ASSET_BACKUP_PATH_ESCAPE",
                    "backup manifest output must be directly inside bundle_dir",
                )
            artifacts = {role: getattr(args, role) for role in REQUIRED_ROLES}
            result = capture_manifest(
                bundle_dir=args.bundle_dir,
                backup_id=args.backup_id,
                snapshot_revision=args.snapshot_revision,
                preflight_sha256=args.preflight_sha256,
                artifacts=artifacts,
            )
            atomic_write(args.output, result)
        elif args.command == "verify":
            result = verify_manifest(args.manifest)
        else:
            result = restore_plan(args.manifest)
    except (AssetStackConfigError, BackupBundleError) as error:
        print(
            canonical_json(
                {
                    "schema": VERIFY_SCHEMA,
                    "status": "not_ready",
                    "code": getattr(error, "code", "ASSET_BACKUP_FAILED"),
                    "message": str(error),
                }
            ).decode("utf-8"),
            file=sys.stderr,
        )
        return 2
    print(canonical_json(result).decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
