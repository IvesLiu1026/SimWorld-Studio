from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest


TOOLS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

import asset_stack_backup_bundle as bundle  # noqa: E402


class AssetStackBackupBundleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.artifacts = {}
        for role in bundle.REQUIRED_ROLES:
            path = self.root / f"{role}.bin"
            path.write_bytes(f"immutable-{role}".encode())
            self.artifacts[role] = path

    def tearDown(self):
        self.temporary.cleanup()

    def capture(self):
        return bundle.capture_manifest(
            bundle_dir=self.root,
            backup_id="asset-backup-20260721-r1",
            snapshot_revision="asset-snapshot-20260721-r1",
            preflight_sha256="a" * 64,
            artifacts=self.artifacts,
        )

    def test_capture_verify_and_restore_plan_are_deterministic(self):
        first = self.capture()
        second = self.capture()
        self.assertEqual(first, second)
        manifest = self.root / "backup-manifest.json"
        bundle.atomic_write(manifest, first)
        with self.assertRaises(bundle.BackupBundleError) as immutable:
            bundle.atomic_write(manifest, first)
        self.assertEqual(immutable.exception.code, "ASSET_BACKUP_MANIFEST_EXISTS")

        verified = bundle.verify_manifest(manifest)
        repeated = bundle.verify_manifest(manifest)
        self.assertEqual(verified, repeated)
        self.assertEqual(verified["status"], "verified")
        plan = bundle.restore_plan(manifest)
        self.assertEqual(plan["status"], "requires_admin_state_change")
        self.assertEqual(plan["manifest_sha256"], verified["manifest_sha256"])

    def test_modified_or_symlinked_artifact_fails_closed(self):
        manifest = self.root / "backup-manifest.json"
        bundle.atomic_write(manifest, self.capture())
        self.artifacts["postgres_dump"].write_text("modified", encoding="utf-8")
        with self.assertRaises(bundle.BackupBundleError) as changed:
            bundle.verify_manifest(manifest)
        self.assertEqual(changed.exception.code, "ASSET_BACKUP_CHECKSUM_MISMATCH")

        outside = self.root.parent / "outside-backup-artifact"
        outside.write_text("outside", encoding="utf-8")
        try:
            self.artifacts["postgres_dump"].unlink()
            self.artifacts["postgres_dump"].symlink_to(outside)
            with self.assertRaises(bundle.BackupBundleError) as symlink:
                bundle.capture_manifest(
                    bundle_dir=self.root,
                    backup_id="asset-backup-20260721-r2",
                    snapshot_revision="asset-snapshot-20260721-r2",
                    preflight_sha256="b" * 64,
                    artifacts=self.artifacts,
                )
            self.assertEqual(symlink.exception.code, "ASSET_BACKUP_SYMLINK_REJECTED")
        finally:
            outside.unlink(missing_ok=True)

    def test_absolute_or_incomplete_manifest_paths_are_rejected(self):
        value = self.capture()
        value["artifacts"][0]["path"] = "/etc/passwd"
        manifest = self.root / "backup-manifest.json"
        manifest.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(bundle.BackupBundleError) as escaped:
            bundle.verify_manifest(manifest)
        self.assertEqual(escaped.exception.code, "ASSET_BACKUP_PATH_ESCAPE")


if __name__ == "__main__":
    unittest.main()
