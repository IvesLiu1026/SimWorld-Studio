from __future__ import annotations

import json
import hashlib
import os
import pathlib
import stat
import sys
import tempfile
import unittest
from unittest import mock


TOOLS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

from asset_stack_config import (  # noqa: E402
    AssetStackConfigError,
    load_secret,
    require_digest_image,
)
import asset_stack_preflight as preflight  # noqa: E402


DIGEST = "a" * 64


class AssetStackFixture:
    def __init__(self, root: pathlib.Path):
        self.root = root
        self.asset_db = root / "asset-db"
        catalog = self.asset_db / "catalog" / "office"
        catalog.mkdir(parents=True)
        records = {
            "chair": {
                "identity": {"asset_id": "chair", "name": "Office chair"},
                "technical": {
                    "unreal_asset_path": "/Game/Office/SM_Chair.SM_Chair"
                },
            },
            "cabinet": {
                "identity": {"asset_id": "cabinet", "name": "Cabinet"},
                "technical": {
                    "unreal_asset_path": "/Game/Office/SM_Cabinet.SM_Cabinet"
                },
            },
        }
        for asset_id, record in records.items():
            (catalog / f"{asset_id}.json").write_text(
                json.dumps(record), encoding="utf-8"
            )
        (self.asset_db / "category_index.json").write_text(
            json.dumps(
                {
                    "total_assets": 2,
                    "categories": [
                        {
                            "id": "office",
                            "count": 2,
                            "assets": [
                                {"asset_id": "chair"},
                                {"asset_id": "cabinet"},
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.postgres_secret = root / "postgres_url"
        self.postgres_secret.write_text(
            "postgresql://simworld:never-render-this@127.0.0.1:55432/asset_db\n",
            encoding="utf-8",
        )
        self.postgres_secret.chmod(0o600)
        self.postgres_password = root / "postgres_password"
        self.postgres_password.write_text("test-only-password\n", encoding="utf-8")
        self.postgres_password.chmod(0o600)
        self.qdrant_api_key = root / "qdrant_api_key"
        self.qdrant_api_key.write_text("q" * 40 + "\n", encoding="utf-8")
        self.qdrant_api_key.chmod(0o600)
        self.embed_service_token = root / "embed_service_token"
        self.embed_service_token.write_text("e" * 40 + "\n", encoding="utf-8")
        self.embed_service_token.chmod(0o600)
        self.dense_model_dir, self.dense_revision = self._model_artifact(
            "dense", "BAAI/bge-large-en-v1.5", dense_size=1024
        )
        self.sparse_model_dir, self.sparse_revision = self._model_artifact(
            "sparse", "Qdrant/bm25"
        )

    def _model_artifact(
        self, kind: str, model_id: str, *, dense_size: int | None = None
    ) -> tuple[pathlib.Path, str]:
        root = self.root / f"{kind}-model"
        root.mkdir()
        model_file = root / "model.bin"
        model_file.write_bytes(f"verified-{kind}-model".encode())
        manifest = {
            "schema": "simworld-embedding-model-artifact/v1",
            "kind": kind,
            "model_id": model_id,
            "files": [
                {
                    "path": "model.bin",
                    "sha256": hashlib.sha256(model_file.read_bytes()).hexdigest(),
                    "size": model_file.stat().st_size,
                }
            ],
        }
        if dense_size is not None:
            manifest["dense_size"] = dense_size
        manifest_path = root / "artifact-manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        revision = f"sha256:{hashlib.sha256(manifest_path.read_bytes()).hexdigest()}"
        return root, revision

    def env(self) -> dict[str, str]:
        image = f"registry.example/simworld/component@sha256:{DIGEST}"
        return {
            "ASSET_STACK_PROFILE": "aws",
            "ASSET_DB_DIR": str(self.asset_db),
            "ASSET_SNAPSHOT_REVISION": "asset-snapshot-20260721-r1",
            "UE_CONTENT_REVISION": "ue-content-20260721-a1",
            "VISTA_UE_CONTENT_REVISION": "ue-content-20260721-a1",
            "POSTGRES_DB": "asset_db",
            "POSTGRES_USER": "simworld",
            "POSTGRES_URL_FILE": str(self.postgres_secret),
            "POSTGRES_PASSWORD_FILE_HOST": str(self.postgres_password),
            "QDRANT_URL": "http://127.0.0.1:6333",
            "QDRANT_COLLECTION": "assets_20260721_r1",
            "QDRANT_API_KEY_FILE": str(self.qdrant_api_key),
            "EMBED_SERVICE_URL": "http://127.0.0.1:7777",
            "EMBED_SERVICE_TOKEN_FILE": str(self.embed_service_token),
            "EMBED_VERSION": "embed-recipe-20260721-r1",
            "EMBED_DENSE_MODEL": "BAAI/bge-large-en-v1.5",
            "EMBED_DENSE_REVISION": self.dense_revision,
            "EMBED_DENSE_MODEL_DIR_HOST": str(self.dense_model_dir),
            "EMBED_DENSE_SIZE": "1024",
            "EMBED_SPARSE_MODEL": "Qdrant/bm25",
            "EMBED_SPARSE_REVISION": self.sparse_revision,
            "EMBED_SPARSE_MODEL_DIR_HOST": str(self.sparse_model_dir),
            "POSTGRES_IMAGE": image,
            "QDRANT_IMAGE": image,
            "EMBED_SERVICE_IMAGE": image,
            "ASSET_TOOLS_PYTHON_IMAGE": image,
            "ASSET_TOOLS_UV_IMAGE": image,
            "ASSET_BACKUP_ROOT": str(self.root / "backups"),
            "ASSET_BACKUP_RETENTION_DAYS": "30",
            "ASSET_BACKUP_MIN_FREE_BYTES": str(10 * 1024**3),
        }


class AssetStackPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.fixture = AssetStackFixture(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def test_preflight_is_deterministic_offline_and_secret_free(self):
        env = self.fixture.env()
        with (
            mock.patch(
                "urllib.request.urlopen",
                side_effect=AssertionError("offline preflight opened the network"),
            ),
            mock.patch(
                "psycopg2.connect",
                side_effect=AssertionError("offline preflight connected to Postgres"),
            ),
        ):
            first = preflight.build_preflight(env)
            second = preflight.build_preflight(env)

        self.assertEqual(first, second)
        self.assertEqual(first["schema"], "simworld-asset-stack-preflight/v1")
        self.assertEqual(first["status"], "ready_for_admin_gates")
        self.assertEqual(first["config"]["catalog"]["count"], 2)
        self.assertEqual(
            first["config"]["postgres"]["dsn_secret_source"], "POSTGRES_URL_FILE"
        )
        self.assertEqual(
            first["config"]["qdrant"]["api_key_secret_source"],
            "QDRANT_API_KEY_FILE",
        )
        self.assertEqual(
            first["config"]["embedding_service"]["token_secret_source"],
            "EMBED_SERVICE_TOKEN_FILE",
        )
        encoded = json.dumps(first)
        self.assertNotIn("never-render-this", encoded)
        self.assertNotIn("postgresql://", encoded)
        self.assertNotIn("q" * 40, encoded)
        self.assertNotIn("e" * 40, encoded)
        self.assertEqual(
            [stage["id"] for stage in first["plan"]],
            [
                "catalog_offline_audit",
                "postgres_catalog_dry_run",
                "qdrant_pending_dry_run",
                "schema_migration_and_full_index",
                "snapshot_live_audit",
                "backup_restore_drill",
            ],
        )

    def test_requires_file_backed_postgres_and_exact_ue_revision(self):
        inline = self.fixture.env()
        inline["POSTGRES_URL"] = self.fixture.postgres_secret.read_text().strip()
        inline.pop("POSTGRES_URL_FILE")
        with self.assertRaises(AssetStackConfigError) as inline_error:
            preflight.build_preflight(inline)
        self.assertEqual(inline_error.exception.code, "ASSET_SECRET_FILE_REQUIRED")

        mismatch = self.fixture.env()
        mismatch["VISTA_UE_CONTENT_REVISION"] = "ue-content-different-r2"
        with self.assertRaises(AssetStackConfigError) as mismatch_error:
            preflight.build_preflight(mismatch)
        self.assertEqual(mismatch_error.exception.code, "ASSET_UE_REVISION_MISMATCH")

    def test_rejects_unpinned_images_and_credential_urls(self):
        env = self.fixture.env()
        env["QDRANT_IMAGE"] = "qdrant/qdrant:latest"
        with self.assertRaises(AssetStackConfigError) as image_error:
            preflight.build_preflight(env)
        self.assertEqual(image_error.exception.code, "ASSET_IMAGE_NOT_PINNED")

        env = self.fixture.env()
        env["QDRANT_URL"] = "http://token@127.0.0.1:6333"
        with self.assertRaises(AssetStackConfigError) as url_error:
            preflight.build_preflight(env)
        self.assertEqual(url_error.exception.code, "ASSET_URL_CONTAINS_SECRET")

        env = self.fixture.env()
        env["QDRANT_COLLECTION"] = "assets"
        with self.assertRaises(AssetStackConfigError) as collection_error:
            preflight.build_preflight(env)
        self.assertEqual(
            collection_error.exception.code, "ASSET_COLLECTION_NOT_PINNED"
        )

    def test_secret_reader_rejects_conflict_symlink_and_weak_permissions(self):
        with self.assertRaises(AssetStackConfigError) as conflict:
            load_secret(
                {
                    "POSTGRES_URL": "postgresql://inline/db",
                    "POSTGRES_URL_FILE": str(self.fixture.postgres_secret),
                },
                "POSTGRES_URL",
                "POSTGRES_URL_FILE",
            )
        self.assertEqual(conflict.exception.code, "ASSET_SECRET_SOURCE_CONFLICT")

        link = self.root / "postgres-link"
        link.symlink_to(self.fixture.postgres_secret)
        with self.assertRaises(AssetStackConfigError) as symlink:
            load_secret(
                {"POSTGRES_URL_FILE": str(link)},
                "POSTGRES_URL",
                "POSTGRES_URL_FILE",
            )
        self.assertEqual(symlink.exception.code, "ASSET_SECRET_FILE_UNAVAILABLE")

        self.fixture.postgres_secret.chmod(0o640)
        with self.assertRaises(AssetStackConfigError) as permissions:
            load_secret(
                {"POSTGRES_URL_FILE": str(self.fixture.postgres_secret)},
                "POSTGRES_URL",
                "POSTGRES_URL_FILE",
            )
        self.assertEqual(
            permissions.exception.code, "ASSET_SECRET_FILE_PERMISSIONS"
        )

        self.fixture.postgres_secret.chmod(0o600)
        self.fixture.postgres_secret.write_text(
            "postgresql://simworld:secret@127.0.0.1:55432/asset_db\n\n",
            encoding="utf-8",
        )
        with self.assertRaises(AssetStackConfigError) as multiline:
            load_secret(
                {"POSTGRES_URL_FILE": str(self.fixture.postgres_secret)},
                "POSTGRES_URL",
                "POSTGRES_URL_FILE",
            )
        self.assertEqual(multiline.exception.code, "ASSET_SECRET_FILE_INVALID")

    def test_image_digest_contract_rejects_uppercase_or_short_hashes(self):
        self.assertEqual(
            require_digest_image(f"postgres:16@sha256:{DIGEST}", "POSTGRES_IMAGE"),
            f"postgres:16@sha256:{DIGEST}",
        )
        for value in ("postgres:16", "postgres:latest", "postgres@sha256:abc"):
            with self.subTest(value=value):
                with self.assertRaises(AssetStackConfigError):
                    require_digest_image(value, "POSTGRES_IMAGE")

    def test_model_artifact_revision_is_bound_to_exact_local_files(self):
        env = self.fixture.env()
        (self.fixture.dense_model_dir / "model.bin").write_bytes(b"tampered")
        with self.assertRaises(AssetStackConfigError) as tampered:
            preflight.build_preflight(env)
        self.assertEqual(tampered.exception.code, "ASSET_MODEL_CHECKSUM_MISMATCH")

        env = self.fixture.env()
        env["EMBED_DENSE_REVISION"] = "dense-label-only-r1"
        with self.assertRaises(AssetStackConfigError) as label_only:
            preflight.build_preflight(env)
        self.assertEqual(
            label_only.exception.code, "ASSET_MODEL_REVISION_NOT_CONTENT_PINNED"
        )


if __name__ == "__main__":
    unittest.main()
