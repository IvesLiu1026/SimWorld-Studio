from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import os
import pathlib
import stat
import sys
import tempfile
import unittest


TOOLS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

import fetch_vista_bootstrap_assets as acquisition  # noqa: E402


def digest(value: bytes, algorithm: str) -> str:
    return hashlib.new(algorithm, value).hexdigest()


class Fixture:
    def __init__(self, root: pathlib.Path) -> None:
        self.root = root
        self.root.chmod(0o700)
        self.output_parent = root / "outputs"
        self.output_parent.mkdir(mode=0o700)
        self.output = self.output_parent / "fixture-assets"
        self.payloads = {
            acquisition.DOWNLOAD_PREFIX + "fixture_asset/model.gltf": b'{"asset":{"version":"2.0"}}',
            acquisition.DOWNLOAD_PREFIX + "fixture_asset/model.bin": b"fixture-binary",
        }
        self.manifest_path = root / "source.json"
        self.manifest = {
            "schema": acquisition.SCHEMA,
            "revision": "fixture-v1",
            "provider": "Poly Haven",
            "provider_url": "https://polyhaven.com/",
            "license": acquisition.EXPECTED_LICENSE,
            "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
            "attribution": "3D assets sourced from Poly Haven",
            "assets": [
                {
                    "asset_id": "fixture_asset",
                    "source_asset_id": "fixture_asset",
                    "source_page": acquisition.SOURCE_PREFIX + "fixture_asset",
                    "semantic_roles": ["stable_step_stool"],
                    "format": "gltf-2.0",
                    "files": [
                        {
                            "path": "fixture_asset/model.gltf",
                            "url": acquisition.DOWNLOAD_PREFIX + "fixture_asset/model.gltf",
                            "bytes": len(self.payloads[acquisition.DOWNLOAD_PREFIX + "fixture_asset/model.gltf"]),
                            "md5": digest(self.payloads[acquisition.DOWNLOAD_PREFIX + "fixture_asset/model.gltf"], "md5"),
                            "sha256": digest(self.payloads[acquisition.DOWNLOAD_PREFIX + "fixture_asset/model.gltf"], "sha256"),
                        },
                        {
                            "path": "fixture_asset/model.bin",
                            "url": acquisition.DOWNLOAD_PREFIX + "fixture_asset/model.bin",
                            "bytes": len(self.payloads[acquisition.DOWNLOAD_PREFIX + "fixture_asset/model.bin"]),
                            "md5": digest(self.payloads[acquisition.DOWNLOAD_PREFIX + "fixture_asset/model.bin"], "md5"),
                            "sha256": digest(self.payloads[acquisition.DOWNLOAD_PREFIX + "fixture_asset/model.bin"], "sha256"),
                        },
                    ],
                }
            ],
        }
        self.write_manifest()

    def write_manifest(self) -> None:
        self.manifest_path.write_text(json.dumps(self.manifest, indent=2) + "\n", encoding="utf-8")
        self.manifest_path.chmod(0o600)

    def opener(self, url: str):
        @contextlib.contextmanager
        def opened():
            yield io.BytesIO(self.payloads[url])

        return opened()


class AcquisitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.fixture = Fixture(pathlib.Path(self.temporary.name))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def assert_error(self, code: str, callback) -> acquisition.AcquisitionError:
        with self.assertRaises(acquisition.AcquisitionError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_checked_in_manifest_is_pinned_and_bounded(self) -> None:
        path = TOOLS_DIR / "assets" / "vista_mmg_040_cc0_bootstrap.json"
        manifest, _raw = acquisition.read_manifest(path)
        entries = [entry for asset in manifest["assets"] for entry in asset["files"]]
        self.assertEqual(
            [asset["source_asset_id"] for asset in manifest["assets"]],
            ["painted_wooden_stool", "Shelf_01", "cardboard_box_01"],
        )
        self.assertEqual(len(entries), 15)
        self.assertEqual(sum(entry["bytes"] for entry in entries), 4_648_718)
        self.assertEqual({asset["format"] for asset in manifest["assets"]}, {"gltf-2.0"})

    def test_dry_run_builds_a_plan_without_network_or_output(self) -> None:
        plan = acquisition.build_plan(self.fixture.manifest_path, self.fixture.output)
        self.assertEqual(plan.total_bytes, sum(map(len, self.fixture.payloads.values())))
        self.assertFalse(self.fixture.output.exists())
        self.assertEqual(acquisition.result(plan, "dry_run")["network_used"], False)

    def test_apply_is_private_atomic_and_idempotent(self) -> None:
        plan = acquisition.build_plan(self.fixture.manifest_path, self.fixture.output)
        self.assertEqual(acquisition.apply_plan(plan, stream_opener=self.fixture.opener), "created")
        self.assertEqual(stat.S_IMODE(self.fixture.output.stat().st_mode), 0o700)
        for path in self.fixture.output.rglob("*"):
            expected = 0o700 if path.is_dir() else 0o600
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), expected)
        self.assertEqual((self.fixture.output / "fixture_asset/model.bin").read_bytes(), b"fixture-binary")

        def unexpected_network(_url: str):
            raise AssertionError("idempotent acquisition must not use the network")

        self.assertEqual(acquisition.apply_plan(plan, stream_opener=unexpected_network), "idempotent")
        receipt = json.loads((self.fixture.output / "acquisition-receipt.json").read_text())
        self.assertEqual(receipt["schema"], acquisition.RECEIPT_SCHEMA)
        self.assertEqual(receipt["tree_sha256"], acquisition._tree_digest(plan.entries))

    def test_integrity_failure_leaves_no_partial_output(self) -> None:
        plan = acquisition.build_plan(self.fixture.manifest_path, self.fixture.output)

        def corrupt_opener(url: str):
            @contextlib.contextmanager
            def opened():
                yield io.BytesIO(self.fixture.payloads[url] + b"corrupt")

            return opened()

        self.assert_error(
            "ASSET_SOURCE_INTEGRITY_FAILED",
            lambda: acquisition.apply_plan(plan, stream_opener=corrupt_opener),
        )
        self.assertFalse(self.fixture.output.exists())
        self.assertFalse((self.fixture.output_parent / ".fixture-assets.lock").exists())

    def test_schema_drift_is_rejected(self) -> None:
        self.fixture.manifest["assets"][0]["files"][0]["token"] = "secret"
        self.fixture.write_manifest()
        self.assert_error(
            "ASSET_SOURCE_SCHEMA_INVALID",
            lambda: acquisition.build_plan(self.fixture.manifest_path, self.fixture.output),
        )

    def test_unpinned_origin_and_traversal_are_rejected(self) -> None:
        original = copy.deepcopy(self.fixture.manifest)
        self.fixture.manifest["assets"][0]["files"][0]["url"] = "https://example.com/model.gltf"
        self.fixture.write_manifest()
        self.assert_error(
            "ASSET_SOURCE_URL_INVALID",
            lambda: acquisition.build_plan(self.fixture.manifest_path, self.fixture.output),
        )
        self.fixture.manifest = original
        self.fixture.manifest["assets"][0]["files"][0]["path"] = "../model.gltf"
        self.fixture.write_manifest()
        self.assert_error(
            "ASSET_SOURCE_PATH_INVALID",
            lambda: acquisition.build_plan(self.fixture.manifest_path, self.fixture.output),
        )

    def test_encoded_url_traversal_is_rejected(self) -> None:
        entry = self.fixture.manifest["assets"][0]["files"][0]
        entry["url"] = acquisition.DOWNLOAD_PREFIX + "%2e%2e/private"
        self.fixture.write_manifest()
        self.assert_error(
            "ASSET_SOURCE_URL_INVALID",
            lambda: acquisition.build_plan(self.fixture.manifest_path, self.fixture.output),
        )

    def test_symlinked_manifest_is_rejected(self) -> None:
        linked = self.fixture.root / "linked.json"
        linked.symlink_to(self.fixture.manifest_path)
        self.assert_error(
            "ASSET_SOURCE_SYMLINK_REJECTED",
            lambda: acquisition.build_plan(linked, self.fixture.output),
        )

    def test_existing_extra_or_modified_file_fails_closed(self) -> None:
        plan = acquisition.build_plan(self.fixture.manifest_path, self.fixture.output)
        acquisition.apply_plan(plan, stream_opener=self.fixture.opener)
        extra = self.fixture.output / "extra.txt"
        extra.write_text("unexpected", encoding="utf-8")
        extra.chmod(0o600)
        self.assert_error("ASSET_SOURCE_OUTPUT_CONFLICT", lambda: acquisition.verify_existing(plan))

    def test_foreign_lock_is_not_removed(self) -> None:
        plan = acquisition.build_plan(self.fixture.manifest_path, self.fixture.output)
        lock = self.fixture.output_parent / ".fixture-assets.lock"
        lock.write_text("owner", encoding="utf-8")
        lock.chmod(0o600)
        self.assert_error(
            "ASSET_SOURCE_OUTPUT_BUSY",
            lambda: acquisition.apply_plan(plan, stream_opener=self.fixture.opener),
        )
        self.assertEqual(lock.read_text(encoding="utf-8"), "owner")

    def test_dangling_output_symlink_is_a_conflict(self) -> None:
        plan = acquisition.build_plan(self.fixture.manifest_path, self.fixture.output)
        self.fixture.output.symlink_to(self.fixture.root / "missing")
        self.assert_error("ASSET_SOURCE_OUTPUT_CONFLICT", lambda: acquisition.verify_existing(plan))

    def test_apply_requires_explicit_license_acceptance(self) -> None:
        result = acquisition.main(
            [
                "--manifest",
                str(self.fixture.manifest_path),
                "--output-dir",
                str(self.fixture.output),
                "--apply",
            ]
        )
        self.assertEqual(result, 2)
        self.assertFalse(self.fixture.output.exists())


if __name__ == "__main__":
    unittest.main()
