from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock


TOOLS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))
import verify_asset_snapshot as verifier  # noqa: E402


class FakeResponse:
    def __init__(self, payload, status=200):
        self.status = status
        self._data = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class FakeCursor:
    def __init__(
        self,
        schema_version=2,
        row_count=2,
        matching_count=2,
        matching_snapshot_count=2,
    ):
        self.rows = [
            (schema_version,),
            (row_count, matching_count, matching_snapshot_count),
        ]
        self.calls = []

    def execute(self, query, parameters):
        self.calls.append((query, parameters))

    def fetchone(self):
        return self.rows[len(self.calls) - 1]


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False
        self.session = None

    def set_session(self, **kwargs):
        self.session = kwargs

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


class SnapshotFixture:
    def __init__(self, root: pathlib.Path):
        self.root = root
        self.asset_db = root / "asset-db"
        self.catalog = self.asset_db / "catalog" / "office"
        self.catalog.mkdir(parents=True)
        self.records = {
            "chair": {
                "identity": {"asset_id": "chair", "name": "Office chair"},
                "technical": {"unreal_asset_path": "/Game/Office/SM_Chair.SM_Chair"},
            },
            "cabinet": {
                "identity": {"asset_id": "cabinet", "name": "Tall cabinet"},
                "technical": {
                    "unreal_asset_path": "/Game/Office/SM_Cabinet.SM_Cabinet"
                },
            },
        }
        for asset_id, record in self.records.items():
            (self.catalog / f"{asset_id}.json").write_text(
                json.dumps(record, indent=2), encoding="utf-8"
            )
        index = {
            "schema_version": "1.0",
            "total_assets": 2,
            "categories": [
                {
                    "id": "office",
                    "count": 2,
                    "assets": [{"asset_id": "chair"}, {"asset_id": "cabinet"}],
                }
            ],
        }
        (self.asset_db / "category_index.json").write_text(
            json.dumps(index), encoding="utf-8"
        )

    def config(self):
        return verifier.ProbeConfig(
            catalog_dir=self.asset_db / "catalog",
            category_index=self.asset_db / "category_index.json",
            postgres_url="postgresql://asset_user:secret-value@db.internal/assets",
            qdrant_url="http://qdrant.internal:6333",
            qdrant_api_key="qdrant-secret",
            embedding_url="http://embed.internal:7777",
            embedding_token="embed-secret",
            qdrant_collection="assets-v1",
            dense_name="text_dense",
            sparse_name="text_sparse",
            asset_snapshot_revision="asset-snapshot-20260721-r1",
            ue_content_revision="ue-content-abc123",
            timeout_sec=3,
        )


def embedding_health(**overrides):
    value = {
        "schema": "simworld-embedding-health/v1",
        "status": "ready",
        "models_loaded": True,
        "version": "embed-v1",
        "dense_model": "BAAI/bge-large-en-v1.5",
        "dense_revision": "dense-sha256-abc",
        "dense_size": 1024,
        "sparse_model": "Qdrant/bm25",
        "sparse_revision": "sparse-sha256-def",
    }
    value.update(overrides)
    return value


def qdrant_collection(**overrides):
    result = {
        "status": "green",
        "points_count": 2,
        "config": {
            "params": {
                "vectors": {"text_dense": {"size": 1024, "distance": "Cosine"}},
                "sparse_vectors": {"text_sparse": {}},
            }
        },
    }
    result.update(overrides)
    return {"status": "ok", "result": result}


def fake_opener_factory(embedding=None, qdrant=None, revision_count=2, requests=None):
    embedding = embedding or embedding_health()
    qdrant = qdrant or qdrant_collection()

    def open_request(request, timeout):
        if requests is not None:
            requests.append((request, timeout))
        if request.full_url.endswith("/health"):
            return FakeResponse(embedding)
        if request.full_url.endswith("/points/count"):
            return FakeResponse({"status": "ok", "result": {"count": revision_count}})
        if "/collections/" in request.full_url:
            return FakeResponse(qdrant)
        raise AssertionError(f"unexpected URL: {request.full_url}")

    return open_request


class VerifyAssetSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.fixture = SnapshotFixture(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def db_connector(
        self,
        schema_version=2,
        row_count=2,
        matching_count=2,
        matching_snapshot_count=2,
        holder=None,
    ):
        def connect(dsn, **kwargs):
            connection = FakeConnection(
                FakeCursor(
                    schema_version,
                    row_count,
                    matching_count,
                    matching_snapshot_count,
                )
            )
            if holder is not None:
                holder.append((dsn, kwargs, connection))
            return connection

        return connect

    def test_collects_complete_reproducible_receipt_from_fake_dependencies(self):
        requests = []
        connections = []
        manifest = verifier.collect_manifest(
            self.fixture.config(),
            opener=fake_opener_factory(requests=requests),
            db_connect=self.db_connector(holder=connections),
        )

        self.assertEqual(manifest["schema"], "simworld-asset-snapshot/v1")
        self.assertEqual(manifest["snapshot_id"], "asset-snapshot-20260721-r1")
        self.assertEqual(manifest["catalog"]["count"], 2)
        self.assertEqual(manifest["postgres"], {"schema_version": 2, "row_count": 2})
        self.assertEqual(manifest["qdrant"]["point_count"], 2)
        self.assertEqual(manifest["embedding"]["version"], "embed-v1")
        self.assertEqual(
            manifest["embedding"]["dense_model"],
            "BAAI/bge-large-en-v1.5@dense-sha256-abc",
        )
        self.assertEqual(
            requests[0][0].get_header("Authorization"), "Bearer embed-secret"
        )
        self.assertEqual(requests[1][0].get_header("Api-key"), "qdrant-secret")
        self.assertEqual(requests[2][0].method, "POST")
        revision_filter = json.loads(requests[2][0].data)
        filter_values = {
            item["key"]: item["match"]["value"]
            for item in revision_filter["filter"]["must"]
        }
        self.assertEqual(filter_values["dense_revision"], "dense-sha256-abc")
        self.assertEqual(filter_values["sparse_revision"], "sparse-sha256-def")
        self.assertEqual(
            filter_values["asset_snapshot_revision"],
            "asset-snapshot-20260721-r1",
        )
        self.assertEqual(
            connections[0][2]._cursor.calls[1][1],
            ("embed-v1", "asset-snapshot-20260721-r1"),
        )
        self.assertEqual(
            connections[0][1]["application_name"], "simworld_asset_snapshot_audit"
        )
        self.assertIn("default_transaction_read_only=on", connections[0][1]["options"])
        self.assertEqual(
            connections[0][2].session, {"readonly": True, "autocommit": True}
        )
        self.assertTrue(connections[0][2].closed)

        # JSON formatting changes do not alter the canonical catalog checksum/id.
        chair = self.fixture.catalog / "chair.json"
        chair.write_text(
            json.dumps(self.fixture.records["chair"], separators=(",", ":")),
            encoding="utf-8",
        )
        repeated = verifier.collect_manifest(
            self.fixture.config(),
            opener=fake_opener_factory(),
            db_connect=self.db_connector(),
        )
        self.assertEqual(repeated, manifest)

    def test_count_or_vector_mismatch_fails_closed(self):
        with self.assertRaises(verifier.SnapshotAuditError) as count_error:
            verifier.collect_manifest(
                self.fixture.config(),
                opener=fake_opener_factory(
                    qdrant=qdrant_collection(points_count=1), revision_count=1
                ),
                db_connect=self.db_connector(),
            )
        self.assertEqual(count_error.exception.code, "ASSET_SNAPSHOT_COUNT_MISMATCH")

        with self.assertRaises(verifier.SnapshotAuditError) as vector_error:
            verifier.collect_manifest(
                self.fixture.config(),
                opener=fake_opener_factory(embedding=embedding_health(dense_size=768)),
                db_connect=self.db_connector(),
            )
        self.assertEqual(
            vector_error.exception.code, "ASSET_SNAPSHOT_VECTOR_SIZE_MISMATCH"
        )

        with self.assertRaises(verifier.SnapshotAuditError) as stale_qdrant:
            verifier.collect_manifest(
                self.fixture.config(),
                opener=fake_opener_factory(revision_count=1),
                db_connect=self.db_connector(),
            )
        self.assertEqual(
            stale_qdrant.exception.code, "ASSET_QDRANT_SNAPSHOT_REVISION_MISMATCH"
        )

    def test_embedding_health_and_postgres_revision_are_required(self):
        with self.assertRaises(verifier.SnapshotAuditError) as unloaded:
            verifier.collect_manifest(
                self.fixture.config(),
                opener=fake_opener_factory(
                    embedding=embedding_health(status="starting", models_loaded=False)
                ),
                db_connect=self.db_connector(),
            )
        self.assertEqual(unloaded.exception.code, "ASSET_EMBEDDING_NOT_READY")

        with self.assertRaises(verifier.SnapshotAuditError) as stale_rows:
            verifier.collect_manifest(
                self.fixture.config(),
                opener=fake_opener_factory(),
                db_connect=self.db_connector(matching_count=1),
            )
        self.assertEqual(
            stale_rows.exception.code, "ASSET_POSTGRES_EMBEDDING_REVISION_MISMATCH"
        )

        with self.assertRaises(verifier.SnapshotAuditError) as no_matching_rows:
            verifier.collect_manifest(
                self.fixture.config(),
                opener=fake_opener_factory(),
                db_connect=self.db_connector(matching_count=0),
            )
        self.assertEqual(
            no_matching_rows.exception.code,
            "ASSET_POSTGRES_EMBEDDING_REVISION_MISMATCH",
        )

        with self.assertRaises(verifier.SnapshotAuditError) as stale_snapshot_rows:
            verifier.collect_manifest(
                self.fixture.config(),
                opener=fake_opener_factory(),
                db_connect=self.db_connector(matching_snapshot_count=1),
            )
        self.assertEqual(
            stale_snapshot_rows.exception.code,
            "ASSET_POSTGRES_SNAPSHOT_REVISION_MISMATCH",
        )

        with self.assertRaises(verifier.SnapshotAuditError) as stale_schema:
            verifier.collect_manifest(
                self.fixture.config(),
                opener=fake_opener_factory(),
                db_connect=self.db_connector(schema_version=1),
            )
        self.assertEqual(
            stale_schema.exception.code,
            "ASSET_POSTGRES_SCHEMA_REVISION_MISMATCH",
        )

        with self.assertRaises(verifier.SnapshotAuditError) as unpinned_model:
            verifier.collect_manifest(
                self.fixture.config(),
                opener=fake_opener_factory(
                    embedding=embedding_health(dense_revision="latest")
                ),
                db_connect=self.db_connector(),
            )
        self.assertEqual(unpinned_model.exception.code, "ASSET_REVISION_NOT_IMMUTABLE")

        config = self.fixture.config()
        unpinned_ue = verifier.ProbeConfig(
            **{**config.__dict__, "ue_content_revision": "main"}
        )
        with self.assertRaises(verifier.SnapshotAuditError) as ue_revision:
            verifier.collect_manifest(
                unpinned_ue,
                opener=fake_opener_factory(),
                db_connect=self.db_connector(),
            )
        self.assertEqual(ue_revision.exception.code, "ASSET_REVISION_NOT_IMMUTABLE")

    def test_category_index_must_name_exact_catalog_corpus(self):
        index_path = self.fixture.asset_db / "category_index.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        index["categories"][0]["assets"][1]["asset_id"] = "missing"
        index_path.write_text(json.dumps(index), encoding="utf-8")

        with self.assertRaises(verifier.SnapshotAuditError) as error:
            verifier.collect_catalog(self.fixture.asset_db / "catalog", index_path)
        self.assertEqual(error.exception.code, "ASSET_CATEGORY_INDEX_CATALOG_MISMATCH")

    def test_configured_snapshot_revision_must_exactly_match_manifest(self):
        manifest = verifier.collect_manifest(
            self.fixture.config(),
            opener=fake_opener_factory(),
            db_connect=self.db_connector(),
        )
        args = argparse.Namespace(
            asset_db_dir=str(self.fixture.asset_db),
            catalog_dir="",
            category_index="",
            collection="assets-v1",
            dense_name="text_dense",
            sparse_name="text_sparse",
            ue_content_revision="ue-content-abc123",
            timeout=3,
        )
        environment = {
            "POSTGRES_URL": "postgresql://user:secret@db/assets",
            "QDRANT_URL": "http://qdrant:6333",
            "EMBED_SERVICE_URL": "http://embed:7777",
            "ASSET_SNAPSHOT_REVISION": "asset-snapshot-20260721-r0",
        }
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            self.assertRaises(verifier.SnapshotAuditError) as mismatch,
        ):
            verifier.build_config(args, manifest)
        self.assertEqual(
            mismatch.exception.code, "ASSET_SNAPSHOT_REVISION_MISMATCH"
        )

    def test_config_accepts_mode_0600_secret_files_and_runtime_ue_alias(self):
        args = argparse.Namespace(
            asset_db_dir=str(self.fixture.asset_db),
            catalog_dir="",
            category_index="",
            collection="assets-v1",
            dense_name="text_dense",
            sparse_name="text_sparse",
            ue_content_revision="",
            timeout=3,
        )
        secret_values = {
            "POSTGRES_URL_FILE": "postgresql://user:secret@db/assets",
            "QDRANT_API_KEY_FILE": "qdrant-key",
            "EMBED_SERVICE_TOKEN_FILE": "embed-token",
        }
        environment = {
            "QDRANT_URL": "http://qdrant:6333",
            "EMBED_SERVICE_URL": "http://embed:7777",
            "ASSET_SNAPSHOT_REVISION": "asset-snapshot-20260721-r1",
            "VISTA_UE_CONTENT_REVISION": "ue-content-abc123",
        }
        for name, value in secret_values.items():
            path = self.root / name.casefold()
            path.write_text(value + "\n", encoding="utf-8")
            path.chmod(0o600)
            environment[name] = str(path)

        with mock.patch.dict(os.environ, environment, clear=True):
            config = verifier.build_config(args)

        self.assertEqual(config.postgres_url, secret_values["POSTGRES_URL_FILE"])
        self.assertEqual(config.qdrant_api_key, "qdrant-key")
        self.assertEqual(config.embedding_token, "embed-token")
        self.assertEqual(config.ue_content_revision, "ue-content-abc123")

    def test_failed_capture_does_not_create_or_replace_receipt(self):
        output = self.root / "snapshot-manifest.json"
        receipt_output = self.root / "snapshot-live-audit.json"
        args = argparse.Namespace(
            command="capture",
            timeout=3,
            asset_db_dir=str(self.fixture.asset_db),
            catalog_dir="",
            category_index="",
            collection="assets-v1",
            dense_name="text_dense",
            sparse_name="text_sparse",
            ue_content_revision="ue-content-abc123",
            snapshot_id="",
            output=output,
            receipt_output=receipt_output,
            receipt_ttl_seconds=300,
            replace=False,
        )
        environment = {
            "POSTGRES_URL": "postgresql://user:secret@db/assets",
            "QDRANT_URL": "http://qdrant:6333",
            "EMBED_SERVICE_URL": "http://embed:7777",
            "ASSET_SNAPSHOT_REVISION": "asset-snapshot-20260721-r1",
        }
        failure = verifier.SnapshotAuditError(
            "ASSET_SNAPSHOT_COUNT_MISMATCH", "asset_stack", "Counts differ."
        )
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch.object(verifier, "collect_manifest", side_effect=failure),
        ):
            with self.assertRaises(verifier.SnapshotAuditError):
                verifier.run(args)
        self.assertFalse(output.exists())
        self.assertFalse(receipt_output.exists())

        original = b'{"do_not_replace":true}\n'
        output.write_bytes(original)
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch.object(verifier, "collect_manifest", side_effect=failure),
        ):
            with self.assertRaises(verifier.SnapshotAuditError):
                verifier.run(args)
        self.assertEqual(output.read_bytes(), original)

    def test_exact_receipt_verification_detects_live_revision_change(self):
        expected = verifier.collect_manifest(
            self.fixture.config(),
            opener=fake_opener_factory(),
            db_connect=self.db_connector(),
        )
        changed = verifier.collect_manifest(
            self.fixture.config(),
            snapshot_id=expected["snapshot_id"],
            opener=fake_opener_factory(embedding=embedding_health(version="embed-v2")),
            db_connect=self.db_connector(),
        )
        self.assertNotEqual(
            verifier.canonical_json(expected), verifier.canonical_json(changed)
        )

    def test_live_audit_receipt_binds_manifest_facts_and_expires(self):
        manifest = verifier.collect_manifest(
            self.fixture.config(),
            opener=fake_opener_factory(),
            db_connect=self.db_connector(),
        )
        manifest_bytes = (
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")
        issued = datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc)
        receipt = verifier.make_live_audit_receipt(
            manifest,
            manifest_bytes,
            ttl_sec=300,
            clock=lambda: issued,
        )

        self.assertEqual(receipt["schema"], "simworld-asset-live-audit/v1")
        self.assertEqual(receipt["snapshot_id"], manifest["snapshot_id"])
        self.assertEqual(
            receipt["manifest_sha256"], verifier.sha256_bytes(manifest_bytes)
        )
        self.assertEqual(receipt["issued_at"], "2026-07-21T12:00:00Z")
        self.assertEqual(receipt["expires_at"], "2026-07-21T12:05:00Z")
        self.assertEqual(
            receipt["observations"]["asset_snapshot_revision"],
            "asset-snapshot-20260721-r1",
        )
        self.assertEqual(receipt["observations"]["postgres"]["schema_version"], 2)
        self.assertEqual(
            verifier.validate_live_audit_receipt(
                receipt,
                manifest,
                manifest_bytes,
                clock=lambda: issued + timedelta(seconds=299),
            ),
            receipt,
        )

        with self.assertRaises(verifier.SnapshotAuditError) as expired:
            verifier.validate_live_audit_receipt(
                receipt,
                manifest,
                manifest_bytes,
                clock=lambda: issued + timedelta(seconds=300),
            )
        self.assertEqual(expired.exception.code, "ASSET_LIVE_AUDIT_EXPIRED")

        changed_bytes = manifest_bytes + b" "
        with self.assertRaises(verifier.SnapshotAuditError) as changed_manifest:
            verifier.validate_live_audit_receipt(
                receipt,
                manifest,
                changed_bytes,
                clock=lambda: issued,
            )
        self.assertEqual(
            changed_manifest.exception.code, "ASSET_LIVE_AUDIT_MANIFEST_MISMATCH"
        )

        tampered = json.loads(json.dumps(receipt))
        tampered["observations"]["postgres"]["row_count"] = 1
        with self.assertRaises(verifier.SnapshotAuditError) as changed_facts:
            verifier.validate_live_audit_receipt(
                tampered,
                manifest,
                manifest_bytes,
                clock=lambda: issued,
            )
        self.assertEqual(
            changed_facts.exception.code,
            "ASSET_LIVE_AUDIT_OBSERVATIONS_MISMATCH",
        )

    def test_capture_and_verify_atomically_emit_digest_bound_receipts(self):
        manifest = verifier.collect_manifest(
            self.fixture.config(),
            opener=fake_opener_factory(),
            db_connect=self.db_connector(),
        )
        issued = datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc)
        manifest_path = self.root / "snapshot-manifest.json"
        capture_receipt = self.root / "capture-live-audit.json"
        capture_args = argparse.Namespace(
            command="capture",
            timeout=3,
            asset_db_dir=str(self.fixture.asset_db),
            catalog_dir="",
            category_index="",
            collection="assets-v1",
            dense_name="text_dense",
            sparse_name="text_sparse",
            ue_content_revision="ue-content-abc123",
            snapshot_id="",
            output=manifest_path,
            receipt_output=capture_receipt,
            receipt_ttl_seconds=300,
            replace=False,
        )
        with (
            mock.patch.object(verifier, "build_config", return_value=self.fixture.config()),
            mock.patch.object(verifier, "collect_manifest", return_value=manifest),
        ):
            captured = verifier.run(capture_args, clock=lambda: issued)

        self.assertEqual(
            captured["manifest_sha256"],
            verifier.sha256_bytes(manifest_path.read_bytes()),
        )
        self.assertEqual(
            captured["live_audit_receipt_sha256"],
            verifier.sha256_bytes(capture_receipt.read_bytes()),
        )
        persisted_capture = json.loads(capture_receipt.read_text(encoding="utf-8"))
        self.assertNotIn("postgresql://", json.dumps(persisted_capture))
        self.assertNotIn("secret", json.dumps(persisted_capture))

        verify_receipt = self.root / "verify-live-audit.json"
        verify_args = argparse.Namespace(
            command="verify",
            timeout=3,
            asset_db_dir=str(self.fixture.asset_db),
            catalog_dir="",
            category_index="",
            collection="assets-v1",
            dense_name="text_dense",
            sparse_name="text_sparse",
            ue_content_revision="ue-content-abc123",
            manifest=manifest_path,
            receipt_output=verify_receipt,
            receipt_ttl_seconds=120,
            replace=False,
        )
        with (
            mock.patch.object(verifier, "build_config", return_value=self.fixture.config()),
            mock.patch.object(verifier, "collect_manifest", return_value=manifest),
        ):
            verified = verifier.run(verify_args, clock=lambda: issued)

        self.assertEqual(verified["snapshot_id"], manifest["snapshot_id"])
        self.assertEqual(
            verified["live_audit_receipt_sha256"],
            verifier.sha256_bytes(verify_receipt.read_bytes()),
        )
        self.assertEqual(
            json.loads(verify_receipt.read_text(encoding="utf-8"))["ttl_seconds"],
            120,
        )

    def test_secret_bearing_dependency_errors_are_publicly_redacted(self):
        secret_dsn = "postgresql://asset_user:do-not-print@db.internal/assets"

        def broken_connect(*_args, **_kwargs):
            raise RuntimeError(f"could not connect to {secret_dsn}")

        config = self.fixture.config()
        config = verifier.ProbeConfig(**{**config.__dict__, "postgres_url": secret_dsn})
        with self.assertRaises(verifier.SnapshotAuditError) as error:
            verifier.collect_manifest(
                config, opener=fake_opener_factory(), db_connect=broken_connect
            )
        encoded = json.dumps(verifier._public_error(error.exception))
        self.assertNotIn("do-not-print", encoded)
        self.assertNotIn(secret_dsn, encoded)
        self.assertEqual(error.exception.code, "ASSET_POSTGRES_AUDIT_FAILED")

    def test_cli_subprocess_fails_without_leaking_environment_secret(self):
        output = self.root / "must-not-exist.json"
        receipt = self.root / "must-not-exist-receipt.json"
        environment = {
            **os.environ,
            "ASSET_DB_DIR": str(self.root / "missing-assets"),
            "POSTGRES_URL": "postgresql://user:subprocess-secret@db.internal/assets",
            "QDRANT_URL": "http://127.0.0.1:6333",
            "EMBED_SERVICE_URL": "http://127.0.0.1:7777",
            "QDRANT_COLLECTION": "assets-v1",
            "ASSET_SNAPSHOT_REVISION": "asset-snapshot-20260721-r1",
            "UE_CONTENT_REVISION": "ue-content-abc123",
        }
        completed = subprocess.run(
            [
                sys.executable,
                str(TOOLS_DIR / "verify_asset_snapshot.py"),
                "capture",
                "--output",
                str(output),
                "--receipt-output",
                str(receipt),
            ],
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(completed.returncode, 1)
        self.assertFalse(output.exists())
        self.assertFalse(receipt.exists())
        self.assertNotIn("subprocess-secret", completed.stderr)
        payload = json.loads(completed.stderr)
        self.assertEqual(payload["status"], "not_ready")


if __name__ == "__main__":
    unittest.main()
