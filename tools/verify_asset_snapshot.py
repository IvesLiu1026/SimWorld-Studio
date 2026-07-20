#!/usr/bin/env python3
"""Capture or verify a fail-closed SimWorld semantic asset snapshot.

This command is intentionally read-only with respect to catalog, PostgreSQL,
Qdrant, and the embedding service.  ``capture`` writes a receipt atomically
only after every dependency agrees.  ``verify`` compares live observations to
an existing receipt and never rewrites it.

POSTGRES_URL, QDRANT_URL, EMBED_SERVICE_URL, and optional service credentials
are accepted from the environment only so secrets cannot leak through argv.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable


SNAPSHOT_SCHEMA = "simworld-asset-snapshot/v1"
HEALTH_SCHEMA = "simworld-embedding-health/v1"
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,159}$")
SHA256 = re.compile(r"^[a-f0-9]{64}$")
POSTGRES_DSN = re.compile(r"\bpostgres(?:ql)?://[^\s\"'<>]+", re.IGNORECASE)
SCHEMA_COMPONENT = "asset_catalog"
CATALOG_DIGEST_DOMAIN = b"simworld-catalog-canonical-json/v1\0"
UNPINNED_REVISIONS = {"dev", "latest", "main", "master", "unknown", "unversioned"}


class SnapshotAuditError(RuntimeError):
    """A public, secret-free operational audit failure."""

    def __init__(self, code: str, dependency: str, message: str):
        super().__init__(message)
        self.code = code
        self.dependency = dependency
        self.public_message = message


@dataclass(frozen=True)
class ProbeConfig:
    catalog_dir: pathlib.Path
    category_index: pathlib.Path
    postgres_url: str
    qdrant_url: str
    qdrant_api_key: str
    embedding_url: str
    embedding_token: str
    qdrant_collection: str
    dense_name: str
    sparse_name: str
    ue_content_revision: str
    timeout_sec: float


def fail(code: str, dependency: str, message: str) -> None:
    raise SnapshotAuditError(code, dependency, message)


def require_nonempty(value: Any, field: str, dependency: str) -> str:
    if not isinstance(value, str) or not value.strip():
        fail("ASSET_AUDIT_FIELD_MISSING", dependency, f"{field} is required.")
    return value.strip()


def require_safe_id(value: Any, field: str, dependency: str) -> str:
    text = require_nonempty(value, field, dependency)
    if not SAFE_ID.fullmatch(text):
        fail("ASSET_AUDIT_FIELD_INVALID", dependency, f"{field} is invalid.")
    return text


def require_positive_int(
    value: Any, field: str, dependency: str, maximum: int = 1_000_000_000
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
        or value > maximum
    ):
        fail(
            "ASSET_AUDIT_FIELD_INVALID",
            dependency,
            f"{field} must be a positive integer.",
        )
    return value


def require_nonnegative_int(
    value: Any, field: str, dependency: str, maximum: int = 1_000_000_000
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > maximum
    ):
        fail(
            "ASSET_AUDIT_FIELD_INVALID",
            dependency,
            f"{field} must be a non-negative integer.",
        )
    return value


def require_immutable_revision(value: Any, field: str, dependency: str) -> str:
    revision = require_safe_id(value, field, dependency)
    if revision.casefold() in UNPINNED_REVISIONS:
        fail(
            "ASSET_REVISION_NOT_IMMUTABLE",
            dependency,
            f"{field} must identify an immutable artifact revision.",
        )
    return revision


def require_model(value: Any, field: str) -> str:
    text = require_nonempty(value, field, "embedding")
    if len(text) > 240 or re.search(r"[\x00-\x1f\x7f]", text):
        fail("ASSET_EMBEDDING_METADATA_INVALID", "embedding", f"{field} is invalid.")
    return text


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _frame(digest: Any, value: bytes) -> None:
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def collect_catalog(
    catalog_dir: pathlib.Path, category_index: pathlib.Path
) -> dict[str, Any]:
    """Return a deterministic digest and validate index-to-catalog membership."""
    if not catalog_dir.is_dir():
        fail(
            "ASSET_CATALOG_UNAVAILABLE", "catalog", "The catalog directory is missing."
        )
    files = sorted(
        (path for path in catalog_dir.rglob("*.json") if path.is_file()),
        key=lambda path: path.relative_to(catalog_dir).as_posix(),
    )
    if not files:
        fail("ASSET_CATALOG_EMPTY", "catalog", "The catalog contains no JSON records.")

    digest = hashlib.sha256(CATALOG_DIGEST_DOMAIN)
    catalog_ids: set[str] = set()
    for path in files:
        relative = path.relative_to(catalog_dir).as_posix()
        if path.is_symlink():
            fail(
                "ASSET_CATALOG_SYMLINK_REJECTED",
                "catalog",
                f"Catalog record {relative} must not be a symbolic link.",
            )
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            fail(
                "ASSET_CATALOG_INVALID",
                "catalog",
                f"Catalog record {relative} is unreadable or invalid JSON.",
            )
        if not isinstance(record, dict):
            fail(
                "ASSET_CATALOG_INVALID",
                "catalog",
                f"Catalog record {relative} must be an object.",
            )
        identity = record.get("identity")
        asset_id = identity.get("asset_id") if isinstance(identity, dict) else None
        asset_id = require_safe_id(
            asset_id or path.stem, f"catalog[{relative}].asset_id", "catalog"
        )
        if asset_id in catalog_ids:
            fail(
                "ASSET_CATALOG_DUPLICATE_ID",
                "catalog",
                "The catalog contains duplicate asset IDs.",
            )
        catalog_ids.add(asset_id)
        _frame(digest, relative.encode("utf-8"))
        _frame(digest, canonical_json(record))

    try:
        index = json.loads(category_index.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        fail(
            "ASSET_CATEGORY_INDEX_UNAVAILABLE",
            "catalog",
            "The category index is missing or invalid.",
        )
    categories = index.get("categories") if isinstance(index, dict) else None
    if not isinstance(categories, list):
        fail(
            "ASSET_CATEGORY_INDEX_INVALID",
            "catalog",
            "The category index has no categories array.",
        )
    index_ids: list[str] = []
    declared_count = 0
    for category in categories:
        if not isinstance(category, dict) or not isinstance(
            category.get("assets"), list
        ):
            fail(
                "ASSET_CATEGORY_INDEX_INVALID",
                "catalog",
                "A category index entry is malformed.",
            )
        assets = category["assets"]
        count = category.get("count")
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or count != len(assets)
        ):
            fail(
                "ASSET_CATEGORY_INDEX_COUNT_MISMATCH",
                "catalog",
                "A category index count does not match its assets array.",
            )
        declared_count += count
        for asset in assets:
            if not isinstance(asset, dict):
                fail(
                    "ASSET_CATEGORY_INDEX_INVALID",
                    "catalog",
                    "A category index asset entry is malformed.",
                )
            index_ids.append(
                require_safe_id(
                    asset.get("asset_id"), "category_index.asset_id", "catalog"
                )
            )
    if len(index_ids) != len(set(index_ids)):
        fail(
            "ASSET_CATEGORY_INDEX_DUPLICATE_ID",
            "catalog",
            "The category index contains duplicate asset IDs.",
        )
    if declared_count != len(files) or set(index_ids) != catalog_ids:
        fail(
            "ASSET_CATEGORY_INDEX_CATALOG_MISMATCH",
            "catalog",
            "The category index does not describe the exact catalog corpus.",
        )
    total_assets = index.get("total_assets")
    if total_assets is not None and total_assets != declared_count:
        fail(
            "ASSET_CATEGORY_INDEX_COUNT_MISMATCH",
            "catalog",
            "category_index.total_assets is inconsistent.",
        )
    return {"count": len(files), "sha256": digest.hexdigest()}


def http_json(
    url: str,
    *,
    dependency: str,
    headers: dict[str, str],
    timeout_sec: float,
    json_body: dict[str, Any] | None = None,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    request_headers = {"Accept": "application/json", **headers}
    data = None
    if json_body is not None:
        request_headers["Content-Type"] = "application/json"
        data = canonical_json(json_body)
    request = urllib.request.Request(
        url,
        data=data,
        headers=request_headers,
        method="POST" if data is not None else "GET",
    )
    try:
        response = opener(request, timeout=timeout_sec)
        if hasattr(response, "__enter__"):
            with response as active:
                status = getattr(active, "status", 200)
                payload = active.read()
        else:
            status = getattr(response, "status", 200)
            payload = response.read()
    except (OSError, TimeoutError, urllib.error.URLError):
        fail(
            "ASSET_DEPENDENCY_UNAVAILABLE",
            dependency,
            f"The {dependency} dependency did not answer its health probe.",
        )
    if not isinstance(status, int) or status < 200 or status >= 300:
        fail(
            "ASSET_DEPENDENCY_HTTP_ERROR",
            dependency,
            f"The {dependency} dependency returned a non-success status.",
        )
    if len(payload) > 1_000_000:
        fail(
            "ASSET_DEPENDENCY_RESPONSE_INVALID",
            dependency,
            f"The {dependency} response exceeded the audit limit.",
        )
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        fail(
            "ASSET_DEPENDENCY_RESPONSE_INVALID",
            dependency,
            f"The {dependency} dependency returned invalid JSON.",
        )
    if not isinstance(value, dict):
        fail(
            "ASSET_DEPENDENCY_RESPONSE_INVALID",
            dependency,
            f"The {dependency} dependency returned an invalid response shape.",
        )
    return value


def _join_url(base_url: str, suffix: str, dependency: str) -> str:
    base = require_nonempty(base_url, f"{dependency}_url", dependency)
    parsed = urllib.parse.urlsplit(base)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        fail(
            "ASSET_DEPENDENCY_URL_INVALID",
            dependency,
            f"The {dependency} URL must be an HTTP(S) origin without credentials or query parameters.",
        )
    return base.rstrip("/") + suffix


def collect_embedding(
    config: ProbeConfig, *, opener: Callable[..., Any]
) -> dict[str, Any]:
    headers = (
        {"Authorization": f"Bearer {config.embedding_token}"}
        if config.embedding_token
        else {}
    )
    payload = http_json(
        _join_url(config.embedding_url, "/health", "embedding"),
        dependency="embedding",
        headers=headers,
        timeout_sec=config.timeout_sec,
        opener=opener,
    )
    if (
        payload.get("schema") != HEALTH_SCHEMA
        or payload.get("status") != "ready"
        or payload.get("models_loaded") is not True
    ):
        fail(
            "ASSET_EMBEDDING_NOT_READY",
            "embedding",
            "The embedding service has not loaded both configured models.",
        )
    version = require_safe_id(payload.get("version"), "embedding.version", "embedding")
    dense_model = require_model(payload.get("dense_model"), "embedding.dense_model")
    dense_revision = require_immutable_revision(
        payload.get("dense_revision"), "embedding.dense_revision", "embedding"
    )
    sparse_model = require_model(payload.get("sparse_model"), "embedding.sparse_model")
    sparse_revision = require_immutable_revision(
        payload.get("sparse_revision"), "embedding.sparse_revision", "embedding"
    )
    dense_size = require_positive_int(
        payload.get("dense_size"), "embedding.dense_size", "embedding", 65_536
    )
    dense_ref = require_model(
        f"{dense_model}@{dense_revision}", "embedding.dense_model_ref"
    )
    sparse_ref = require_model(
        f"{sparse_model}@{sparse_revision}", "embedding.sparse_model_ref"
    )
    return {
        "version": version,
        "dense_model_id": dense_model,
        "dense_revision": dense_revision,
        "dense_model": dense_ref,
        "sparse_model_id": sparse_model,
        "sparse_revision": sparse_revision,
        "sparse_model": sparse_ref,
        "dense_size": dense_size,
    }


def default_db_connect(dsn: str, **kwargs: Any) -> Any:
    import psycopg2

    return psycopg2.connect(dsn, **kwargs)


def collect_postgres(
    config: ProbeConfig,
    embedding_version: str,
    *,
    db_connect: Callable[..., Any],
) -> dict[str, int]:
    if not config.postgres_url:
        fail(
            "ASSET_POSTGRES_CONFIG_MISSING",
            "postgres",
            "POSTGRES_URL is required in the service environment.",
        )
    connection = None
    try:
        connection = db_connect(
            config.postgres_url,
            connect_timeout=max(1, int(config.timeout_sec)),
            application_name="simworld_asset_snapshot_audit",
            options=(
                f"-c statement_timeout={max(1, int(config.timeout_sec * 1000))} "
                "-c default_transaction_read_only=on"
            ),
        )
        connection.set_session(readonly=True, autocommit=True)
        cursor = connection.cursor()
        cursor.execute(
            "SELECT schema_version FROM simworld_schema_metadata WHERE component = %s",
            (SCHEMA_COMPONENT,),
        )
        schema_row = cursor.fetchone()
        cursor.execute(
            "SELECT count(*), count(*) FILTER (WHERE embedding_version = %s) FROM assets",
            (embedding_version,),
        )
        count_row = cursor.fetchone()
    except Exception:
        fail(
            "ASSET_POSTGRES_AUDIT_FAILED",
            "postgres",
            "The PostgreSQL asset catalog could not be audited.",
        )
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
    if not schema_row or len(schema_row) < 1:
        fail(
            "ASSET_POSTGRES_SCHEMA_MISSING",
            "postgres",
            "The asset catalog schema version is not recorded.",
        )
    if not count_row or len(count_row) < 2:
        fail(
            "ASSET_POSTGRES_RESPONSE_INVALID",
            "postgres",
            "The PostgreSQL audit returned an invalid result.",
        )
    schema_version = require_positive_int(
        schema_row[0], "postgres.schema_version", "postgres", 1_000_000
    )
    row_count = require_positive_int(count_row[0], "postgres.row_count", "postgres")
    matching_embedding_rows = require_nonnegative_int(
        count_row[1], "postgres.matching_embedding_rows", "postgres"
    )
    if matching_embedding_rows != row_count:
        fail(
            "ASSET_POSTGRES_EMBEDDING_REVISION_MISMATCH",
            "postgres",
            "Not every PostgreSQL asset row uses the live embedding revision.",
        )
    return {"schema_version": schema_version, "row_count": row_count}


def collect_qdrant(
    config: ProbeConfig,
    embedding: dict[str, Any],
    *,
    opener: Callable[..., Any],
) -> dict[str, Any]:
    collection = require_safe_id(
        config.qdrant_collection, "qdrant.collection", "qdrant"
    )
    encoded_collection = urllib.parse.quote(collection, safe="")
    headers = {"api-key": config.qdrant_api_key} if config.qdrant_api_key else {}
    payload = http_json(
        _join_url(config.qdrant_url, f"/collections/{encoded_collection}", "qdrant"),
        dependency="qdrant",
        headers=headers,
        timeout_sec=config.timeout_sec,
        opener=opener,
    )
    if payload.get("status") != "ok" or not isinstance(payload.get("result"), dict):
        fail(
            "ASSET_QDRANT_RESPONSE_INVALID",
            "qdrant",
            "Qdrant returned an invalid collection response.",
        )
    result = payload["result"]
    if result.get("status") != "green":
        fail("ASSET_QDRANT_NOT_READY", "qdrant", "The Qdrant collection is not green.")
    point_count = require_positive_int(
        result.get("points_count"), "qdrant.point_count", "qdrant"
    )
    config_value = result.get("config")
    params = config_value.get("params") if isinstance(config_value, dict) else None
    vectors = params.get("vectors") if isinstance(params, dict) else None
    sparse_vectors = params.get("sparse_vectors") if isinstance(params, dict) else None
    dense = vectors.get(config.dense_name) if isinstance(vectors, dict) else None
    if not isinstance(dense, dict):
        fail(
            "ASSET_QDRANT_DENSE_VECTOR_MISSING",
            "qdrant",
            "The required named dense vector is missing.",
        )
    dense_size = require_positive_int(
        dense.get("size"), "qdrant.dense_size", "qdrant", 65_536
    )
    if not isinstance(sparse_vectors, dict) or config.sparse_name not in sparse_vectors:
        fail(
            "ASSET_QDRANT_SPARSE_VECTOR_MISSING",
            "qdrant",
            "The required named sparse vector is missing.",
        )
    revision_filter = {
        "filter": {
            "must": [
                {"key": "embedding_version", "match": {"value": embedding["version"]}},
                {"key": "dense_model", "match": {"value": embedding["dense_model_id"]}},
                {
                    "key": "dense_revision",
                    "match": {"value": embedding["dense_revision"]},
                },
                {
                    "key": "sparse_model",
                    "match": {"value": embedding["sparse_model_id"]},
                },
                {
                    "key": "sparse_revision",
                    "match": {"value": embedding["sparse_revision"]},
                },
                {"key": "dense_size", "match": {"value": dense_size}},
            ]
        },
        "exact": True,
    }
    revision_count_payload = http_json(
        _join_url(
            config.qdrant_url,
            f"/collections/{encoded_collection}/points/count",
            "qdrant",
        ),
        dependency="qdrant",
        headers=headers,
        timeout_sec=config.timeout_sec,
        json_body=revision_filter,
        opener=opener,
    )
    revision_result = (
        revision_count_payload.get("result")
        if revision_count_payload.get("status") == "ok"
        else None
    )
    matching_revision_count = (
        revision_result.get("count") if isinstance(revision_result, dict) else None
    )
    matching_revision_count = require_nonnegative_int(
        matching_revision_count,
        "qdrant.matching_embedding_revision_count",
        "qdrant",
    )
    if matching_revision_count != point_count:
        fail(
            "ASSET_QDRANT_EMBEDDING_REVISION_MISMATCH",
            "qdrant",
            "Not every Qdrant point uses the live embedding model revisions.",
        )
    return {
        "collection": collection,
        "point_count": point_count,
        "dense_name": require_safe_id(config.dense_name, "qdrant.dense_name", "qdrant"),
        "dense_size": dense_size,
        "sparse_name": require_safe_id(
            config.sparse_name, "qdrant.sparse_name", "qdrant"
        ),
    }


def deterministic_snapshot_id(fields: dict[str, Any]) -> str:
    digest = hashlib.sha256(canonical_json(fields)).hexdigest()[:24]
    return f"asset-{digest}"


def collect_manifest(
    config: ProbeConfig,
    *,
    snapshot_id: str | None = None,
    opener: Callable[..., Any] = urllib.request.urlopen,
    db_connect: Callable[..., Any] = default_db_connect,
) -> dict[str, Any]:
    ue_revision = require_immutable_revision(
        config.ue_content_revision, "ue_content_revision", "unreal"
    )
    catalog = collect_catalog(config.catalog_dir, config.category_index)
    embedding_observed = collect_embedding(config, opener=opener)
    postgres = collect_postgres(
        config, embedding_observed["version"], db_connect=db_connect
    )
    qdrant = collect_qdrant(config, embedding_observed, opener=opener)
    if (
        catalog["count"] != postgres["row_count"]
        or catalog["count"] != qdrant["point_count"]
    ):
        fail(
            "ASSET_SNAPSHOT_COUNT_MISMATCH",
            "asset_stack",
            "Catalog, PostgreSQL, and Qdrant counts do not all match.",
        )
    if embedding_observed["dense_size"] != qdrant["dense_size"]:
        fail(
            "ASSET_SNAPSHOT_VECTOR_SIZE_MISMATCH",
            "asset_stack",
            "Embedding and Qdrant dense vector sizes do not match.",
        )
    fields = {
        "schema": SNAPSHOT_SCHEMA,
        "ue_content_revision": ue_revision,
        "catalog": catalog,
        "postgres": postgres,
        "qdrant": qdrant,
        "embedding": {
            "version": embedding_observed["version"],
            "dense_model": embedding_observed["dense_model"],
            "sparse_model": embedding_observed["sparse_model"],
        },
    }
    receipt_id = (
        require_safe_id(snapshot_id, "snapshot_id", "snapshot")
        if snapshot_id
        else deterministic_snapshot_id(fields)
    )
    return {
        "schema": SNAPSHOT_SCHEMA,
        "snapshot_id": receipt_id,
        **{key: value for key, value in fields.items() if key != "schema"},
    }


def validate_manifest(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        fail(
            "ASSET_SNAPSHOT_INVALID",
            "snapshot",
            "The asset snapshot must be an object.",
        )
    expected = {
        "schema",
        "snapshot_id",
        "ue_content_revision",
        "catalog",
        "postgres",
        "qdrant",
        "embedding",
    }
    if set(value) != expected or value.get("schema") != SNAPSHOT_SCHEMA:
        fail(
            "ASSET_SNAPSHOT_INVALID",
            "snapshot",
            "The asset snapshot has an unsupported shape or schema.",
        )
    require_safe_id(value.get("snapshot_id"), "snapshot_id", "snapshot")
    require_safe_id(value.get("ue_content_revision"), "ue_content_revision", "snapshot")
    catalog = value.get("catalog")
    if not isinstance(catalog, dict) or set(catalog) != {"count", "sha256"}:
        fail("ASSET_SNAPSHOT_INVALID", "snapshot", "snapshot.catalog is invalid.")
    require_positive_int(catalog.get("count"), "catalog.count", "snapshot")
    if not isinstance(catalog.get("sha256"), str) or not SHA256.fullmatch(
        catalog["sha256"]
    ):
        fail("ASSET_SNAPSHOT_INVALID", "snapshot", "catalog.sha256 is invalid.")
    postgres = value.get("postgres")
    if not isinstance(postgres, dict) or set(postgres) != {
        "schema_version",
        "row_count",
    }:
        fail("ASSET_SNAPSHOT_INVALID", "snapshot", "snapshot.postgres is invalid.")
    require_positive_int(
        postgres.get("schema_version"), "postgres.schema_version", "snapshot", 1_000_000
    )
    require_positive_int(postgres.get("row_count"), "postgres.row_count", "snapshot")
    qdrant = value.get("qdrant")
    qdrant_keys = {
        "collection",
        "point_count",
        "dense_name",
        "dense_size",
        "sparse_name",
    }
    if not isinstance(qdrant, dict) or set(qdrant) != qdrant_keys:
        fail("ASSET_SNAPSHOT_INVALID", "snapshot", "snapshot.qdrant is invalid.")
    require_safe_id(qdrant.get("collection"), "qdrant.collection", "snapshot")
    require_positive_int(qdrant.get("point_count"), "qdrant.point_count", "snapshot")
    require_safe_id(qdrant.get("dense_name"), "qdrant.dense_name", "snapshot")
    require_positive_int(
        qdrant.get("dense_size"), "qdrant.dense_size", "snapshot", 65_536
    )
    require_safe_id(qdrant.get("sparse_name"), "qdrant.sparse_name", "snapshot")
    embedding = value.get("embedding")
    if not isinstance(embedding, dict) or set(embedding) != {
        "version",
        "dense_model",
        "sparse_model",
    }:
        fail("ASSET_SNAPSHOT_INVALID", "snapshot", "snapshot.embedding is invalid.")
    require_safe_id(embedding.get("version"), "embedding.version", "snapshot")
    require_model(embedding.get("dense_model"), "embedding.dense_model")
    require_model(embedding.get("sparse_model"), "embedding.sparse_model")
    return value


def atomic_write_json(
    path: pathlib.Path, value: dict[str, Any], *, replace: bool = False
) -> None:
    if path.exists() and not replace:
        fail(
            "ASSET_SNAPSHOT_OUTPUT_EXISTS",
            "filesystem",
            "The output already exists; use --replace to update it explicitly.",
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _read_manifest(path: pathlib.Path) -> dict[str, Any]:
    try:
        return validate_manifest(json.loads(path.read_text(encoding="utf-8")))
    except SnapshotAuditError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError):
        fail(
            "ASSET_SNAPSHOT_INVALID",
            "snapshot",
            "The asset snapshot is missing or invalid JSON.",
        )


def _required_env(name: str, dependency: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        fail(
            "ASSET_AUDIT_CONFIG_MISSING",
            dependency,
            f"{name} is required in the service environment.",
        )
    return value


def build_config(
    args: argparse.Namespace, expected: dict[str, Any] | None = None
) -> ProbeConfig:
    asset_db_dir = pathlib.Path(
        args.asset_db_dir or _required_env("ASSET_DB_DIR", "catalog")
    ).expanduser()
    expected_qdrant = expected.get("qdrant", {}) if expected else {}
    configured_collection = (
        args.collection or os.environ.get("QDRANT_COLLECTION", "").strip()
    )
    if (
        expected
        and configured_collection
        and configured_collection != expected_qdrant["collection"]
    ):
        fail(
            "ASSET_QDRANT_COLLECTION_MISMATCH",
            "qdrant",
            "QDRANT_COLLECTION does not match the snapshot receipt.",
        )
    collection = configured_collection or expected_qdrant.get("collection", "")
    dense_name = args.dense_name or expected_qdrant.get("dense_name", "text_dense")
    sparse_name = args.sparse_name or expected_qdrant.get("sparse_name", "text_sparse")
    return ProbeConfig(
        catalog_dir=pathlib.Path(args.catalog_dir).expanduser()
        if args.catalog_dir
        else asset_db_dir / "catalog",
        category_index=pathlib.Path(args.category_index).expanduser()
        if args.category_index
        else asset_db_dir / "category_index.json",
        postgres_url=_required_env("POSTGRES_URL", "postgres"),
        qdrant_url=_required_env("QDRANT_URL", "qdrant"),
        qdrant_api_key=os.environ.get("QDRANT_API_KEY", ""),
        embedding_url=_required_env("EMBED_SERVICE_URL", "embedding"),
        embedding_token=os.environ.get("EMBED_SERVICE_TOKEN", ""),
        qdrant_collection=collection,
        dense_name=dense_name,
        sparse_name=sparse_name,
        ue_content_revision=args.ue_content_revision
        or _required_env("UE_CONTENT_REVISION", "unreal"),
        timeout_sec=args.timeout,
    )


def _public_error(error: SnapshotAuditError) -> dict[str, Any]:
    return {
        "schema": "simworld-asset-snapshot-audit/v1",
        "status": "not_ready",
        "code": error.code,
        "dependency": error.dependency,
        "message": POSTGRES_DSN.sub("<redacted-postgres-dsn>", error.public_message),
    }


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("capture", "verify"):
        sub = subparsers.add_parser(command)
        sub.add_argument(
            "--asset-db-dir", default="", help="Asset DB root (default: ASSET_DB_DIR)"
        )
        sub.add_argument(
            "--catalog-dir",
            default="",
            help="Catalog corpus root (default: <asset-db-dir>/catalog)",
        )
        sub.add_argument(
            "--category-index",
            default="",
            help="Category index path (default: <asset-db-dir>/category_index.json)",
        )
        sub.add_argument(
            "--collection",
            default="",
            help="Qdrant collection (default: QDRANT_COLLECTION or receipt value)",
        )
        sub.add_argument("--dense-name", default="", help="Named Qdrant dense vector")
        sub.add_argument("--sparse-name", default="", help="Named Qdrant sparse vector")
        sub.add_argument(
            "--ue-content-revision",
            default="",
            help="Audited UE Content revision (default: UE_CONTENT_REVISION)",
        )
        sub.add_argument(
            "--timeout",
            type=float,
            default=5.0,
            help="Per-dependency timeout in seconds",
        )
    capture = subparsers.choices["capture"]
    capture.add_argument(
        "--snapshot-id",
        default="",
        help="Explicit safe snapshot id (default: deterministic content id)",
    )
    capture.add_argument(
        "--output",
        type=pathlib.Path,
        required=True,
        help="Atomic snapshot receipt output",
    )
    capture.add_argument(
        "--replace",
        action="store_true",
        help="Explicitly replace an existing output after successful verification",
    )
    verify = subparsers.choices["verify"]
    verify.add_argument(
        "--manifest",
        type=pathlib.Path,
        required=True,
        help="Existing simworld-asset-snapshot/v1 receipt",
    )
    return parser


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.timeout <= 0 or args.timeout > 60:
        fail(
            "ASSET_AUDIT_CONFIG_INVALID",
            "configuration",
            "--timeout must be greater than zero and at most 60 seconds.",
        )
    if args.command == "capture":
        if args.output.exists() and not args.replace:
            fail(
                "ASSET_SNAPSHOT_OUTPUT_EXISTS",
                "filesystem",
                "The output already exists; use --replace to update it explicitly.",
            )
        config = build_config(args)
        manifest = validate_manifest(
            collect_manifest(config, snapshot_id=args.snapshot_id or None)
        )
        atomic_write_json(args.output, manifest, replace=args.replace)
        return {
            "schema": "simworld-asset-snapshot-audit/v1",
            "status": "ready",
            "snapshot_id": manifest["snapshot_id"],
            "receipt": str(args.output),
        }
    expected = _read_manifest(args.manifest)
    config = build_config(args, expected)
    observed = validate_manifest(
        collect_manifest(config, snapshot_id=expected["snapshot_id"])
    )
    if canonical_json(observed) != canonical_json(expected):
        fail(
            "ASSET_SNAPSHOT_LIVE_MISMATCH",
            "asset_stack",
            "Live asset dependencies do not exactly match the snapshot receipt.",
        )
    return {
        "schema": "simworld-asset-snapshot-audit/v1",
        "status": "ready",
        "snapshot_id": expected["snapshot_id"],
        "receipt": str(args.manifest),
    }


def main(argv: list[str] | None = None) -> int:
    parser = make_parser()
    try:
        result = run(parser.parse_args(argv))
    except SnapshotAuditError as error:
        print(
            json.dumps(_public_error(error), ensure_ascii=False, sort_keys=True),
            file=sys.stderr,
        )
        return 1
    except Exception:
        error = SnapshotAuditError(
            "ASSET_AUDIT_INTERNAL_ERROR",
            "audit",
            "The asset snapshot audit failed without producing a receipt.",
        )
        print(
            json.dumps(_public_error(error), ensure_ascii=False, sort_keys=True),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
