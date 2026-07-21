from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MIGRATOR_PATH = REPO_ROOT / "tools" / "migrate_to_postgres.py"
SPEC = importlib.util.spec_from_file_location("migrate_to_postgres", MIGRATOR_PATH)
assert SPEC and SPEC.loader
migrator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migrator)


class RecordingCursor:
    def __init__(self):
        self.calls = []

    def execute(self, query, parameters):
        self.calls.append((query, parameters))


class AssetSnapshotRevisionBindingTests(unittest.TestCase):
    def test_schema_v2_stages_and_enforces_snapshot_column(self):
        source = (REPO_ROOT / "tools" / "schema.sql").read_text(encoding="utf-8")

        self.assertIn("asset_snapshot_revision TEXT NOT NULL", source)
        self.assertIn("ADD COLUMN IF NOT EXISTS asset_snapshot_revision TEXT", source)
        self.assertIn("assets_snapshot_revision_present", source)
        self.assertIn("VALUES ('asset_catalog', 2)", source)
        self.assertIn("NOT VALID", source)
        self.assertLess(
            source.index("UPDATE assets SET search_tsv"),
            source.index("ADD CONSTRAINT assets_snapshot_revision_present"),
        )

        migrator_source = MIGRATOR_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "count(*) FILTER (WHERE asset_snapshot_revision = %s)",
            migrator_source,
        )
        self.assertIn("VALIDATE CONSTRAINT assets_snapshot_revision_present", migrator_source)
        self.assertIn("ALTER COLUMN asset_snapshot_revision SET NOT NULL", migrator_source)

    def test_migration_upsert_stamps_every_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            record_path = pathlib.Path(tmp) / "chair.json"
            record_path.write_text(
                json.dumps(
                    {
                        "identity": {
                            "asset_id": "chair",
                            "name": "Office chair",
                            "category": "office",
                        },
                        "technical": {
                            "unreal_asset_path": "/Game/Office/SM_Chair.SM_Chair"
                        },
                    }
                ),
                encoding="utf-8",
            )
            cursor = RecordingCursor()
            asset_id = migrator.upsert_record(
                cursor,
                str(record_path),
                "asset-snapshot-20260721-r1",
            )

        query, parameters = cursor.calls[0]
        self.assertEqual(asset_id, "chair")
        self.assertEqual(query.count("%s"), len(parameters))
        self.assertIn("asset_snapshot_revision", query)
        self.assertIn(
            "asset_snapshot_revision=EXCLUDED.asset_snapshot_revision", query
        )
        self.assertEqual(parameters[2], "asset-snapshot-20260721-r1")

    def test_unpinned_or_missing_revision_is_rejected(self):
        for value in (None, "", "latest", "main", "bad value"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    migrator.require_snapshot_revision(value)

    def test_migration_dry_run_never_connects_to_postgres(self):
        with tempfile.TemporaryDirectory() as tmp:
            asset_db = pathlib.Path(tmp)
            catalog = asset_db / "catalog" / "office"
            catalog.mkdir(parents=True)
            (catalog / "chair.json").write_text(
                json.dumps(
                    {
                        "identity": {
                            "asset_id": "chair",
                            "category": "office",
                        }
                    }
                ),
                encoding="utf-8",
            )
            argv = [
                str(MIGRATOR_PATH),
                "--asset-db-dir",
                str(asset_db),
                "--snapshot-revision",
                "asset-snapshot-20260721-r1",
                "--dry-run",
            ]
            stdout = io.StringIO()
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(
                    migrator.psycopg2,
                    "connect",
                    side_effect=AssertionError("dry-run attempted a DB connection"),
                ),
                contextlib.redirect_stdout(stdout),
            ):
                migrator.main()

        self.assertIn("Asset snapshot revision: asset-snapshot-20260721-r1", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
