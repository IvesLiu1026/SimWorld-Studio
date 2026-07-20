from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
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
    def __init__(self, schema_version=1, row_count=2, matching_count=2):
        self.rows = [(schema_version,), (row_count, matching_count)]
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
        self, schema_version=1, row_count=2, matching_count=2, holder=None
    ):
        def connect(dsn, **kwargs):
            connection = FakeConnection(
                FakeCursor(schema_version, row_count, matching_count)
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
        self.assertRegex(manifest["snapshot_id"], r"^asset-[a-f0-9]{24}$")
        self.assertEqual(manifest["catalog"]["count"], 2)
        self.assertEqual(manifest["postgres"], {"schema_version": 1, "row_count": 2})
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
            stale_qdrant.exception.code, "ASSET_QDRANT_EMBEDDING_REVISION_MISMATCH"
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

    def test_failed_capture_does_not_create_or_replace_receipt(self):
        output = self.root / "snapshot-manifest.json"
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
            replace=False,
        )
        environment = {
            "POSTGRES_URL": "postgresql://user:secret@db/assets",
            "QDRANT_URL": "http://qdrant:6333",
            "EMBED_SERVICE_URL": "http://embed:7777",
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
        environment = {
            **os.environ,
            "ASSET_DB_DIR": str(self.root / "missing-assets"),
            "POSTGRES_URL": "postgresql://user:subprocess-secret@db.internal/assets",
            "QDRANT_URL": "http://127.0.0.1:6333",
            "EMBED_SERVICE_URL": "http://127.0.0.1:7777",
            "QDRANT_COLLECTION": "assets-v1",
            "UE_CONTENT_REVISION": "ue-content-abc123",
        }
        completed = subprocess.run(
            [
                sys.executable,
                str(TOOLS_DIR / "verify_asset_snapshot.py"),
                "capture",
                "--output",
                str(output),
            ],
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(completed.returncode, 1)
        self.assertFalse(output.exists())
        self.assertNotIn("subprocess-secret", completed.stderr)
        payload = json.loads(completed.stderr)
        self.assertEqual(payload["status"], "not_ready")


if __name__ == "__main__":
    unittest.main()
