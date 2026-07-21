#!/usr/bin/env python3
"""Offline production preflight for the semantic asset stack.

This command never opens a network connection and never writes catalog or
database state.  It validates the exact catalog, immutable image/model pins,
loopback endpoints, file-backed PostgreSQL secret, backup policy, and emits a
deterministic execution plan for the administrator-controlled live gates.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import sys
from collections.abc import Mapping
from typing import Any

from asset_stack_config import (
    AssetStackConfigError,
    load_secret,
    require_absolute_path,
    require_digest_image,
    require_immutable_revision,
    require_loopback_http_url,
    require_safe_name,
    require_secret_minimum_bytes,
    require_text,
    validate_postgres_dsn,
    verify_embedding_model_artifact,
)
from verify_asset_snapshot import SnapshotAuditError, collect_catalog


SCHEMA = "simworld-asset-stack-preflight/v1"
GENERIC_COLLECTIONS = {"asset", "assets", "assets-v1", "assets_v1", "collection", "default"}


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def positive_int(env: Mapping[str, str], name: str, *, minimum: int = 1) -> int:
    raw = require_text(env.get(name), name, limit=32)
    try:
        value = int(raw)
    except ValueError as error:
        raise AssetStackConfigError(
            "ASSET_CONFIG_INVALID", f"{name} must be an integer"
        ) from error
    if value < minimum:
        raise AssetStackConfigError(
            "ASSET_CONFIG_INVALID", f"{name} must be at least {minimum}"
        )
    return value


def _ue_revision(env: Mapping[str, str]) -> str:
    audit = str(env.get("UE_CONTENT_REVISION", "") or "").strip()
    runtime = str(env.get("VISTA_UE_CONTENT_REVISION", "") or "").strip()
    if audit and runtime and audit != runtime:
        raise AssetStackConfigError(
            "ASSET_UE_REVISION_MISMATCH",
            "UE_CONTENT_REVISION and VISTA_UE_CONTENT_REVISION must match",
        )
    return require_immutable_revision(
        audit or runtime, "UE_CONTENT_REVISION/VISTA_UE_CONTENT_REVISION"
    )


def build_preflight(env: Mapping[str, str]) -> dict[str, Any]:
    profile = require_text(env.get("ASSET_STACK_PROFILE"), "ASSET_STACK_PROFILE")
    if profile not in {"local", "aws"}:
        raise AssetStackConfigError(
            "ASSET_CONFIG_INVALID", "ASSET_STACK_PROFILE must be local or aws"
        )

    asset_db_dir = require_absolute_path(env.get("ASSET_DB_DIR"), "ASSET_DB_DIR")
    catalog_dir = asset_db_dir / "catalog"
    category_index = asset_db_dir / "category_index.json"
    catalog = collect_catalog(catalog_dir, category_index)
    snapshot_revision = require_immutable_revision(
        env.get("ASSET_SNAPSHOT_REVISION"), "ASSET_SNAPSHOT_REVISION"
    )
    ue_revision = _ue_revision(env)

    postgres_db = require_safe_name(env.get("POSTGRES_DB"), "POSTGRES_DB")
    postgres_user = require_safe_name(env.get("POSTGRES_USER"), "POSTGRES_USER")
    postgres_dsn, postgres_source = load_secret(
        env,
        "POSTGRES_URL",
        "POSTGRES_URL_FILE",
        require_file=True,
    )
    validate_postgres_dsn(
        postgres_dsn, expected_database=postgres_db, require_loopback=True
    )
    _postgres_password, postgres_password_source = load_secret(
        env,
        "POSTGRES_PASSWORD",
        "POSTGRES_PASSWORD_FILE_HOST",
        require_file=True,
    )
    _qdrant_api_key, qdrant_api_key_source = load_secret(
        env,
        "QDRANT_API_KEY",
        "QDRANT_API_KEY_FILE",
        require_file=True,
    )
    require_secret_minimum_bytes(_qdrant_api_key, qdrant_api_key_source)
    _embed_service_token, embed_service_token_source = load_secret(
        env,
        "EMBED_SERVICE_TOKEN",
        "EMBED_SERVICE_TOKEN_FILE",
        require_file=True,
    )
    require_secret_minimum_bytes(_embed_service_token, embed_service_token_source)
    qdrant_url = require_loopback_http_url(env.get("QDRANT_URL"), "QDRANT_URL")
    embedding_url = require_loopback_http_url(
        env.get("EMBED_SERVICE_URL"), "EMBED_SERVICE_URL"
    )
    qdrant_collection = require_safe_name(
        env.get("QDRANT_COLLECTION"), "QDRANT_COLLECTION"
    )
    if qdrant_collection.casefold() in GENERIC_COLLECTIONS:
        raise AssetStackConfigError(
            "ASSET_COLLECTION_NOT_PINNED",
            "QDRANT_COLLECTION must identify the immutable snapshot, not a generic collection",
        )

    dense_size = positive_int(env, "EMBED_DENSE_SIZE")
    backup_root = require_absolute_path(
        env.get("ASSET_BACKUP_ROOT"), "ASSET_BACKUP_ROOT"
    )
    retention_days = positive_int(env, "ASSET_BACKUP_RETENTION_DAYS", minimum=7)
    minimum_free_bytes = positive_int(
        env, "ASSET_BACKUP_MIN_FREE_BYTES", minimum=1_073_741_824
    )

    images = {
        "postgres": require_digest_image(env.get("POSTGRES_IMAGE"), "POSTGRES_IMAGE"),
        "qdrant": require_digest_image(env.get("QDRANT_IMAGE"), "QDRANT_IMAGE"),
        "embedding": require_digest_image(
            env.get("EMBED_SERVICE_IMAGE"), "EMBED_SERVICE_IMAGE"
        ),
        "python_base": require_digest_image(
            env.get("ASSET_TOOLS_PYTHON_IMAGE"), "ASSET_TOOLS_PYTHON_IMAGE"
        ),
        "uv": require_digest_image(
            env.get("ASSET_TOOLS_UV_IMAGE"), "ASSET_TOOLS_UV_IMAGE"
        ),
    }
    dense_model = require_text(env.get("EMBED_DENSE_MODEL"), "EMBED_DENSE_MODEL")
    sparse_model = require_text(env.get("EMBED_SPARSE_MODEL"), "EMBED_SPARSE_MODEL")
    dense_revision = require_immutable_revision(
        env.get("EMBED_DENSE_REVISION"), "EMBED_DENSE_REVISION"
    )
    sparse_revision = require_immutable_revision(
        env.get("EMBED_SPARSE_REVISION"), "EMBED_SPARSE_REVISION"
    )
    dense_model_root = require_absolute_path(
        env.get("EMBED_DENSE_MODEL_DIR_HOST") or env.get("EMBED_DENSE_MODEL_PATH"),
        "EMBED_DENSE_MODEL_DIR_HOST",
    )
    sparse_model_root = require_absolute_path(
        env.get("EMBED_SPARSE_MODEL_DIR_HOST") or env.get("EMBED_SPARSE_MODEL_PATH"),
        "EMBED_SPARSE_MODEL_DIR_HOST",
    )
    dense_artifact = verify_embedding_model_artifact(
        dense_model_root,
        model_id=dense_model,
        revision=dense_revision,
        kind="dense",
        dense_size=dense_size,
    )
    sparse_artifact = verify_embedding_model_artifact(
        sparse_model_root,
        model_id=sparse_model,
        revision=sparse_revision,
        kind="sparse",
    )
    embedding = {
        "version": require_immutable_revision(env.get("EMBED_VERSION"), "EMBED_VERSION"),
        "dense_model": dense_model,
        "dense_revision": dense_revision,
        "dense_size": dense_size,
        "dense_artifact": {"root": str(dense_model_root), **dense_artifact},
        "sparse_model": sparse_model,
        "sparse_revision": sparse_revision,
        "sparse_artifact": {"root": str(sparse_model_root), **sparse_artifact},
    }

    safe_config = {
        "profile": profile,
        "snapshot_revision": snapshot_revision,
        "ue_content_revision": ue_revision,
        "asset_db_dir": str(asset_db_dir),
        "catalog": catalog,
        "postgres": {
            "database": postgres_db,
            "user": postgres_user,
            "dsn_secret_source": postgres_source,
            "password_secret_source": postgres_password_source,
        },
        "qdrant": {
            "url": qdrant_url,
            "collection": qdrant_collection,
            "api_key_secret_source": qdrant_api_key_source,
        },
        "embedding_service": {
            "url": embedding_url,
            "token_secret_source": embed_service_token_source,
        },
        "embedding": embedding,
        "images": images,
        "backup": {
            "root": str(backup_root),
            "retention_days": retention_days,
            "minimum_free_bytes": minimum_free_bytes,
        },
    }
    config_sha256 = hashlib.sha256(canonical_json(safe_config)).hexdigest()
    plan = [
        {
            "id": "catalog_offline_audit",
            "gate": "satisfied",
            "network": False,
            "mutation": False,
        },
        {
            "id": "postgres_catalog_dry_run",
            "gate": "operator_read_only",
            "network": False,
            "mutation": False,
            "argv": [
                "uv",
                "run",
                "--project",
                "tools",
                "--frozen",
                "python",
                "tools/migrate_to_postgres.py",
                "--dry-run",
            ],
        },
        {
            "id": "qdrant_pending_dry_run",
            "gate": "admin_read_only_network",
            "network": True,
            "mutation": False,
            "argv": [
                "uv",
                "run",
                "--project",
                "tools",
                "--frozen",
                "python",
                "tools/build_qdrant_index.py",
                "--dry-run",
            ],
        },
        {
            "id": "schema_migration_and_full_index",
            "gate": "admin_data_state_change",
            "network": True,
            "mutation": True,
        },
        {
            "id": "snapshot_live_audit",
            "gate": "admin_read_only_network",
            "network": True,
            "mutation": False,
        },
        {
            "id": "backup_restore_drill",
            "gate": "admin_state_change",
            "network": True,
            "mutation": True,
        },
    ]
    return {
        "schema": SCHEMA,
        "status": "ready_for_admin_gates",
        "config_sha256": config_sha256,
        "config": safe_config,
        "plan": plan,
    }


def _atomic_write(path: pathlib.Path, payload: bytes) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(temporary, flags, 0o644)
    try:
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.close(fd)
        fd = -1
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=pathlib.Path, help="Optional atomic JSON plan output"
    )
    return parser


def main() -> int:
    args = make_parser().parse_args()
    try:
        result = build_preflight(os.environ)
    except (AssetStackConfigError, SnapshotAuditError) as error:
        payload = {
            "schema": SCHEMA,
            "status": "not_ready",
            "code": getattr(error, "code", "ASSET_PREFLIGHT_FAILED"),
            "message": str(error),
        }
        print(canonical_json(payload).decode("utf-8"), file=sys.stderr)
        return 2
    rendered = canonical_json(result) + b"\n"
    if args.output:
        _atomic_write(args.output, rendered)
    sys.stdout.buffer.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
